"""
skills/route_block.py — is this host blocking the route, or is the page just broken?
===============================================================================
Added 20 Nov 2026 (Phase 2 of the comprehensive heal order).

A browse step can fail for two very different reasons, and until now the loop
treated them the same: the page is genuinely broken/missing, or the exit we are
coming from is being refused. Only the second one is fixable by changing route,
and changing route is expensive (lease, teardown, relaunch), so guessing in
either direction costs something.

WHAT WE LEARNED ON 2026-11-20. A retailer's home page and its search results
returned clean 200s over the Mullvad exit while one product path failed over
and over. Every individual failure looked transient — a protocol error, a
reset, an empty body — so the loop retried, burned its step budget, and
finally handed off to the admin for something no human could fix. The asymmetry was
the tell: an exit that can load a site's home page but never one specific path
is not having a bad minute, it is being filtered. That asymmetry is treated
here as a POSITIVE block signal, not as noise.

Two layers:

  classify()  — stateless. Reads one step's outcome for transport-layer and
                application-layer block signatures. Cheap, no memory.

  BlockLedger — stateful, per run. Remembers what each host has done on each
                route, so it can see the asymmetry that no single step shows.
                It is also what enforces "two strikes on the same host and
                route, then escalate" and "exactly one escalation per run".

Deliberately NOT here: the escalation itself. This module only ever answers
questions. skills/browse.py decides what to do about the answer.
"""
import re
from urllib.parse import urlparse

# Two classified blocks for the same host on the same route is the trigger. One
# is indistinguishable from a bad minute; two with a working control path is a
# pattern.
BLOCK_STRIKES = 2

# Transport layer: the connection itself was refused, reset or truncated.
# These are Chromium's own net-stack strings, so they arrive in the error text
# rather than in any page body.
_TRANSPORT = [
    (r"ERR_HTTP2_PROTOCOL_ERROR",        "http2-protocol-error"),
    (r"ERR_QUIC_PROTOCOL_ERROR",         "quic-protocol-error"),
    (r"ERR_CONNECTION_RESET|ECONNRESET", "connection-reset"),
    (r"ERR_CONNECTION_CLOSED",           "connection-closed"),
    (r"ERR_EMPTY_RESPONSE",              "empty-response"),
    (r"ERR_SSL_PROTOCOL_ERROR",          "ssl-protocol-error"),
    (r"ERR_TOO_MANY_REDIRECTS",          "redirect-loop"),
    (r"ERR_TUNNEL_CONNECTION_FAILED",    "tunnel-failed"),
    (r"ERR_ADDRESS_UNREACHABLE|ERR_CONNECTION_REFUSED", "unreachable"),
]

# Application layer: we got a response, and the response is a wall. Keep these
# anchored to phrases the vendors actually emit — a bare "access denied" is
# common enough in ordinary copy to be worth pairing with something else.
_INTERSTITIAL = [
    (r"cf-browser-verification|__cf_chl|cf_chl_opt|cdn-cgi/challenge",
     "cloudflare-challenge"),
    (r"attention required!?\s*\|\s*cloudflare", "cloudflare-block"),
    (r"checking your browser before accessing", "cloudflare-challenge"),
    (r"enable javascript and cookies to continue", "cloudflare-challenge"),
    (r"akamaighost|reference\s*#\s*\d+\.\w+", "akamai-block"),
    (r"<title>\s*access denied\s*</title>|you don't have permission to access",
     "access-denied"),
    (r"request unsuccessful\.\s*incapsula|_incapsula_|imperva", "imperva-block"),
    (r"pardon our interruption|bot detection|are you a robot",
     "bot-detection"),
    (r"datadome|perimeterx|px-captcha", "bot-detection"),
]

_CAPTCHA = [
    (r"\brecaptcha\b|\bhcaptcha\b|g-recaptcha", "captcha"),
    (r"press\s*(&|and)\s*hold", "captcha"),
    (r"verify (you are|you're) (a )?human|confirm you are human", "captcha"),
    (r"unusual traffic from your (computer )?network", "captcha"),
    (r"solve (this|the) (puzzle|challenge)", "captcha"),
]

# A 403/429 is only a block signal when it is not an ordinary application
# refusal. Paired with any of the above, or standing alone on a route where the
# same host works elsewhere, it counts.
_BLOCK_STATUSES = (403, 429, 503)


def host_of(url):
    try:
        h = (urlparse(url or "").hostname or "").lower()
        return h[4:] if h.startswith("www.") else h
    except Exception:
        return ""


def _match(patterns, blob):
    for pat, name in patterns:
        if re.search(pat, blob, re.I):
            return name
    return None


def classify(outcome="", error="", page_text="", status=None, url=""):
    """Stateless read of one step. Returns None, or a dict:

        {"signature": short name, "layer": transport|application,
         "confidence": high|medium, "detail": the matched evidence}

    `confidence` matters: a transport error or a named vendor interstitial is
    high (act on two of them). A bare 4xx is medium — real on its own only
    once the ledger can see the host working elsewhere on the same route.
    """
    blob = " ".join(str(x or "") for x in (outcome, error))
    body = str(page_text or "")

    sig = _match(_TRANSPORT, blob)
    if sig:
        return {"signature": sig, "layer": "transport", "confidence": "high",
                "detail": f"chromium net error on {host_of(url) or 'the target'}"}

    sig = _match(_CAPTCHA, body) or _match(_CAPTCHA, blob)
    if sig:
        return {"signature": sig, "layer": "application", "confidence": "high",
                "detail": "challenge page served instead of content"}

    sig = _match(_INTERSTITIAL, body) or _match(_INTERSTITIAL, blob)
    if sig:
        return {"signature": sig, "layer": "application", "confidence": "high",
                "detail": f"interstitial served to this exit ({sig})"}

    if status in _BLOCK_STATUSES:
        return {"signature": f"http-{status}", "layer": "application",
                "confidence": "medium",
                "detail": f"HTTP {status} with no block signature in the body"}
    return None


class BlockLedger:
    """Per-run memory of how each host behaves on each route.

    The point of keeping state is the asymmetry test. A host that has served
    this route at least one clean page, and is now failing a *different* path
    repeatedly, is filtering — even when every individual failure looks like a
    transient network hiccup. Without the ledger that pattern is invisible.
    """

    def __init__(self, strikes=BLOCK_STRIKES):
        self.strikes   = strikes
        self.clean     = {}   # (route, host) -> set of paths that loaded cleanly
        self.failed    = {}   # (route, host, path) -> consecutive failure count
        self.blocks    = {}   # (route, host) -> list of block event dicts
        self.escalated = False   # exactly one escalation per run, ever

    @staticmethod
    def _path(url):
        try:
            return urlparse(url or "").path or "/"
        except Exception:
            return "/"

    def record(self, route, url, ok, status=None, outcome="", error="",
               page_text=""):
        """Log one step. Returns a block event dict if this step counted as a
        block for (route, host), else None."""
        host = host_of(url)
        if not host:
            return None
        key, path = (route, host), self._path(url)

        if ok and (status is None or status < 400):
            self.clean.setdefault(key, set()).add(path)
            self.failed.pop((route, host, path), None)
            return None

        verdict = classify(outcome=outcome, error=error, page_text=page_text,
                           status=status, url=url)
        n = self.failed.get((route, host, path), 0) + 1
        self.failed[(route, host, path)] = n

        # The 2026-11-20 signature: this exit demonstrably reaches the host on
        # some other path, but never this one. Promote it to a real block even
        # if the individual failure looked transient or carried no signature.
        working = self.clean.get(key, set()) - {path}
        asymmetric = bool(working) and n >= 2
        if verdict is None and not asymmetric:
            return None
        if verdict is None:
            verdict = {"signature": "path-asymmetry", "layer": "transport",
                       "confidence": "high", "detail": "no explicit signature"}
        if asymmetric:
            verdict = dict(verdict, confidence="high", asymmetric=True,
                           detail=(f"{verdict['detail']}; this exit loads "
                                   f"{sorted(working)[0]} on {host} but has failed "
                                   f"{path} {n}x — asymmetry, not a bad minute"))
        event = dict(verdict, host=host, path=path, route=route, url=url,
                     status=status, failures=n)
        self.blocks.setdefault(key, []).append(event)
        return event

    def block_count(self, route, host):
        return len(self.blocks.get((route, host), []))

    def should_escalate(self, route, host):
        """True once a host has struck out on this route — and only if we have
        not already escalated. One escalation per run, no flapping."""
        if self.escalated or route == "home":
            return False
        return self.block_count(route, host) >= self.strikes

    def mark_escalated(self):
        self.escalated = True

    def summary(self):
        """Lines for the run artifact. Silence here would read as 'no blocks',
        so this is written even when nothing fired."""
        if not self.blocks:
            return ["No route blocks classified."]
        out = []
        for (route, host), events in sorted(self.blocks.items()):
            sigs = ", ".join(sorted({e["signature"] for e in events}))
            out.append(f"{host} on {route}: {len(events)} block(s) — {sigs}")
            for e in events:
                out.append(f"    {e['path']} — {e['detail']}")
        return out


# ─── Handoff narrowing (design decision of 2026-11-20) ────────────────────────
#
# Prompting the admin is reserved for what only a human can do. Everything else the
# supervisor fixes itself. The 12:06 Xbox handoff was the bug this closes: the
# agent asked a human to solve a routing problem, and sat waiting ten minutes
# for help that could not have arrived.

_HUMAN_ONLY = re.compile(
    r"log\s*in|login|sign\s*in|password|credential|account|2fa|two.factor|"
    r"otp|one.time code|verification code|captcha|recaptcha|press\s*(&|and)\s*hold|"
    r"payment|credit card|checkout|purchase|billing|confirm (the )?order",
    re.I)

_INFRASTRUCTURE = re.compile(
    r"block(ed|ing)?|forbidden|403|429|denied|refus|cloudflare|akamai|"
    r"bot.detect|rate.limit|protocol error|connection reset|empty response|"
    r"unreachable|timed? out|timeout|vpn|proxy|exit node|route|"
    r"orphan|stale lock|profile lock|lease|crash(ed)?|browser (is )?(down|dead)",
    re.I)


def handoff_is_human_only(reason):
    """Should this handoff actually reach the admin?

    Human-only wins ties. A login wall behind Cloudflare still needs a human,
    and the cost of wrongly prompting is a question the admin can ignore, while the
    cost of wrongly suppressing is a session that stalls in silence.
    """
    text = str(reason or "")
    if _HUMAN_ONLY.search(text):
        return True, "needs a human: credentials, challenge, or payment"
    if _INFRASTRUCTURE.search(text):
        return False, "infrastructure — the supervisor handles this, not the admin"
    return True, "unclassified; defaulting to asking rather than stalling"
