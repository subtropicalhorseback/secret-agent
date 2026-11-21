#!/usr/bin/env python3
"""Acceptance drills 3 and 4 for browser route escalation (2026-11-20 order).

Run:  venv/bin/python3 tests/test_route_escalation.py

Drill 3 — a host that blocks on the VPN exit:
    the classifier fires, EXACTLY ONE escalation to the home route happens,
    task state carries forward into the relaunch, and no handoff prompt is
    ever sent to the admin for what is an infrastructure problem.

Drill 4 — an escalated session killed mid-run:
    full VPN routing is restored, the home-route lease is released, children
    are reaped, and a terminus message still arrives.

Everything external is stubbed: no network, no browser, no signal-cli, no
model call, no nftables. The fake retailer reproduces the asymmetric
signature seen on 2026-11-20 — home page and search return clean 200s on the
VPN exit while one product path fails every time — because that asymmetry is
the specific thing the classifier has to notice.
"""
import asyncio
import os
import sys
import os as _os_wl, tempfile as _tf_wl
_os_wl.environ.setdefault("CHIP_WORKLOG_DIR", _tf_wl.mkdtemp(prefix="worklog_test_"))  # keep test traffic out of data/worklog
_os_wl.environ.setdefault("CHIP_WEBLOG_DIR", _tf_wl.mkdtemp(prefix="weblog_test_"))
_os_wl.environ.setdefault("CHIP_WEB_DB", _tf_wl.mkdtemp(prefix="webdb_test_") + "/web.db")
_os_wl.environ.setdefault("CHIP_WORK_DB", _tf_wl.mkdtemp(prefix="workdb_test_") + "/work.db")
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Browse runs now write screenshots and a manifest to disk (FIX-0006 phase 1).
# Redirect that at a temp dir BEFORE browse_capture is imported — the root is
# read at import time — so a verification pass never litters the real archive.
import atexit, shutil, tempfile                      # noqa: E402
_CAP_TMP = tempfile.mkdtemp(prefix="route_escalation_captures_")
os.environ["CHIP_BROWSE_CAPTURE_ROOT"] = _CAP_TMP
atexit.register(shutil.rmtree, _CAP_TMP, ignore_errors=True)

from skills import browse, browse_obs, route_block   # noqa: E402

failures = []
SENT     = []          # every Signal message the session tried to send
RELEASED = []          # every home_route.release() reason
ACQUIRED = []          # every home_route.acquire() call
REAPED   = []          # every browse_obs.reap() call
TERMINUS = []          # the run report the caller receives (out["report"])
OUT      = {}          # the `out` dict browse_session fills for its caller


def check(label, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {label}" + (f"  — {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(label)


# ─── fakes ────────────────────────────────────────────────────────────────────

class _Blk:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class _Resp:
    """Minimal stand-in for an Anthropic response."""
    def __init__(self, blocks, stop_reason="tool_use"):
        self.content, self.stop_reason, self.usage = blocks, stop_reason, None


def tool_block(name, inp, bid="t1"):
    return _Blk(type="tool_use", name=name, input=inp, id=bid)


BLOCKED_HOST = "shop.example.com"
HOME_OK      = {"ok": True, "status": 200, "url": f"https://{BLOCKED_HOST}/",
                "title": "Shop — home", "text": "welcome to the shop",
                "elements": "[1] search"}


def fake_service_request(payload, timeout=90):
    """The fake retailer. Blocks /p/console on mullvad; serves it on home."""
    op = payload.get("op")
    if op == "status":
        return {"ok": True, "novnc": "http://pi:6080/"}
    if op == "close":
        return {"ok": True}
    if op == "open":
        url   = payload.get("url", "")
        route = payload.get("route", "")
        if "/p/console" in url:
            if route == "home":
                return {"ok": True, "status": 200, "url": url,
                        "title": "Console — $499", "text": "Console in stock $499",
                        "elements": "[1] add to cart"}
            # The 2026-11-20 signature: a transport error, every single time,
            # on exactly one path, while the rest of the site is fine.
            return {"ok": False, "url": url,
                    "error": "net::ERR_HTTP2_PROTOCOL_ERROR at " + url}
        return dict(HOME_OK, url=url)
    if op == "read":
        return dict(HOME_OK)
    return {"ok": True}


def install_fakes(script):
    """Patch every external edge. `script` is a list of per-step tool calls."""
    steps = {"i": 0}

    def fake_llm(messages, system=None, model=None, effort=None, max_tokens=None,
                 tools=None, *a, **kw):
        i = steps["i"]
        steps["i"] += 1
        if i < len(script):
            name, inp = script[i]
            return _Resp([tool_block(name, inp, bid=f"t{i}")])
        return _Resp([tool_block("finish", {"summary": "done"}, bid="tz")])

    import skills.llm as llm
    llm.claude_raw_sync = fake_llm

    browse.service_request   = fake_service_request
    browse.service_up        = lambda: True
    browse.current_route     = lambda: "mullvad"
    browse.exit_node_label   = lambda: "us-chi-wg-301"

    home_route = types.ModuleType("home_route")
    home_route.acquire = lambda user, task, ttl: (
        ACQUIRED.append((user, ttl)) or {"pid": os.getpid(), "ttl": ttl})
    home_route.release = lambda reason="session-end": (
        RELEASED.append(reason) or True)
    home_route.status  = lambda: {"rule_active": bool(ACQUIRED) and not RELEASED}
    sys.modules["home_route"] = home_route

    browse_obs.reap = lambda dry_run=False: (
        REAPED.append("reap") or {"killed": [], "locks_cleared": [],
                                  "wrapper_without_child": False,
                                  "survivors": [], "errors": []})
    # Keep the real recorder (we assert on its fields) but never write to disk.
    browse_obs.RunRecorder._write_artifact = lambda self, result_text: "(stubbed)"
    browse_obs.RunRecorder._beat           = lambda self: None
    # The terminus report is built for real; only the socket send is faked.
    return steps


async def _async(v):
    return v


async def send_signal(session, text, recipient=None, kind=None):
    SENT.append(text)
    return True


def reset():
    SENT.clear(); RELEASED.clear(); ACQUIRED.clear(); REAPED.clear(); TERMINUS.clear()
    OUT.clear()


def check_terminus(expect_verdict):
    """Phase 3 contract: exactly one terminus, and it reports routes,
    escalation, budgets, time, spend and the artifact name."""
    check("exactly one terminus message", len(TERMINUS) == 1, TERMINUS)
    if len(TERMINUS) != 1:
        return
    t = TERMINUS[0]
    check(f"terminus verdict is {expect_verdict}", expect_verdict in t, t[:120])
    for field in ("Routes:", "Escalation:", "Steps:", "Time:", "Spend:", "Artifact:"):
        check(f"terminus reports {field.strip(':')}", field in t, t)
    check("terminus names the per-target spend", BLOCKED_HOST in t, t)


# ─── drill 3 ──────────────────────────────────────────────────────────────────

def drill_3_escalation():
    print("\nDrill 3 — blocked on the VPN exit, escalate once to home")
    reset()
    # Two clean loads establish the control path, then the product path fails
    # repeatedly. The agent also tries to hand off, which must be suppressed.
    script = [
        ("open", {"url": f"https://{BLOCKED_HOST}/"}),
        ("open", {"url": f"https://{BLOCKED_HOST}/search?q=console"}),
        ("open", {"url": f"https://{BLOCKED_HOST}/p/console"}),
        ("handoff", {"reason": "The product page is blocked / 403, I cannot reach it"}),
        ("open", {"url": f"https://{BLOCKED_HOST}/p/console"}),
        ("open", {"url": f"https://{BLOCKED_HOST}/p/console"}),
        # after escalation the home route serves it:
        ("open", {"url": f"https://{BLOCKED_HOST}/p/console"}),
        ("finish", {"summary": "Console is $499"}),
    ]
    install_fakes(script)
    result, label = asyncio.run(
        browse.browse_session(None, "+1555550182", f"find the console price at {BLOCKED_HOST}",
                              route="vpn", send_signal=send_signal, out=OUT))
    if OUT.get("report"):
        TERMINUS.append(OUT["report"])

    handoffs = [m for m in SENT if "handoff" in m.lower()]
    escal    = [m for m in SENT if "🔀" in m or "home route" in m.lower()]
    check("no handoff prompt reached the admin", not handoffs, handoffs)
    # One Voice spec A.2 (20 Nov 2026): the route switch is plumbing. It is
    # recorded (label below, run record) and never sent as its own message.
    check("no escalation message reached the admin", not escal, SENT)
    check("exactly one home-route lease acquired", len(ACQUIRED) == 1, ACQUIRED)
    check("the lease was released", RELEASED == ["session-end"], RELEASED)
    check("the task completed on the home route", "499" in (result or ""), result)
    check("label names the escalation", "escalated" in (label or ""), label)
    check("pre-flight and post-run reaps both ran", len(REAPED) >= 2, REAPED)
    check_terminus("✅ Browse finished")
    check("terminus explains the escalation",
          TERMINUS and "mullvad -> home" in TERMINUS[0], TERMINUS)


def drill_3b_no_flapping():
    print("\nDrill 3b — the ledger permits exactly one escalation, ever")
    L = route_block.BlockLedger()
    L.record("mullvad", f"https://{BLOCKED_HOST}/", True, 200)
    for _ in range(4):
        L.record("mullvad", f"https://{BLOCKED_HOST}/p/console", False,
                 error="net::ERR_HTTP2_PROTOCOL_ERROR")
    check("escalation offered once", L.should_escalate("mullvad", BLOCKED_HOST))
    L.mark_escalated()
    check("never offered again", not L.should_escalate("mullvad", BLOCKED_HOST))
    check("and never from the home route", not L.should_escalate("home", BLOCKED_HOST))

    print("\n  classifier signatures")
    cases = [
        ("http2",       dict(error="net::ERR_HTTP2_PROTOCOL_ERROR"),        "http2-protocol-error"),
        ("reset",       dict(error="ERR_CONNECTION_RESET"),                 "connection-reset"),
        ("empty",       dict(error="ERR_EMPTY_RESPONSE"),                   "empty-response"),
        ("cloudflare",  dict(page_text="Checking your browser before accessing"), "cloudflare-challenge"),
        ("akamai",      dict(page_text="AkamaiGHost Reference #18.abc"),    "akamai-block"),
        ("captcha",     dict(page_text="Please verify you are human"),      "captcha"),
        ("403",         dict(status=403),                                   "http-403"),
    ]
    for label, kw, want in cases:
        got = (route_block.classify(**kw) or {}).get("signature")
        check(f"classifies {label}", got == want, f"got {got}")
    check("a plain 404 is not a block", route_block.classify(status=404) is None)
    check("a clean page is not a block",
          route_block.classify(page_text="in stock, $499", status=200) is None)

    print("\n  handoff narrowing")
    for reason, want_human in [
            ("I need you to log in to the account", True),
            ("There is a CAPTCHA I cannot solve", True),
            ("Confirm the payment / credit card details", True),
            ("The page is blocked by Cloudflare", False),
            ("Connection reset on every attempt, the exit looks blocked", False),
            ("The browser profile has a stale lock", False)]:
        got, _why = route_block.handoff_is_human_only(reason)
        check(f"handoff {'asks the admin' if want_human else 'is suppressed'}: {reason[:38]}",
              got == want_human)


# ─── drill 4 ──────────────────────────────────────────────────────────────────

def drill_4_killed_mid_run():
    print("\nDrill 4 — escalated session killed mid-run")
    reset()
    script = [
        ("open", {"url": f"https://{BLOCKED_HOST}/"}),
        ("open", {"url": f"https://{BLOCKED_HOST}/search?q=console"}),
        ("open", {"url": f"https://{BLOCKED_HOST}/p/console"}),
        ("open", {"url": f"https://{BLOCKED_HOST}/p/console"}),
        ("open", {"url": f"https://{BLOCKED_HOST}/p/console"}),
        ("open", {"url": f"https://{BLOCKED_HOST}/p/console"}),   # now on home
        ("__kill__", {}),
    ]
    install_fakes(script)

    import skills.llm as llm
    inner = llm.claude_raw_sync

    def killing_llm(*a, **kw):
        r = inner(*a, **kw)
        blk = r.content[0]
        if getattr(blk, "name", "") == "__kill__":
            # What a watchdog kill looks like from inside the loop.
            raise KeyboardInterrupt("watchdog killed the session")
        return r
    llm.claude_raw_sync = killing_llm

    terminus = {}
    try:
        asyncio.run(
            browse.browse_session(None, "+1555550182",
                                  f"find the console price at {BLOCKED_HOST}",
                                  route="vpn", send_signal=send_signal, out=OUT))
        terminus["raised"] = False
    except BaseException as e:
        terminus["raised"] = type(e).__name__
    if OUT.get("report"):
        TERMINUS.append(OUT["report"])

    check("the kill propagated (caller sees it)", terminus["raised"] == "KeyboardInterrupt",
          terminus)
    check("escalation had happened first", len(ACQUIRED) == 1, ACQUIRED)
    check("the home-route lease was still released", RELEASED == ["session-end"],
          RELEASED)
    check("children were reaped on the way out", len(REAPED) >= 2, REAPED)
    check("no escalation message was sent on the kill path either",
          not any("🔀" in m for m in SENT), SENT)
    # The kill is not a silent ending: the caller still holds a report.
    check_terminus("💥 Browse crashed")
    check("terminus says it was killed before recording a reason",
          TERMINUS and "killed" in TERMINUS[0], TERMINUS)


def main():
    print("browser route-escalation drills (all externals stubbed)")
    drill_3_escalation()
    drill_3b_no_flapping()
    drill_4_killed_mid_run()
    print(f"\n{'FAILED: ' + ', '.join(failures) if failures else 'all checks passed'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
