"""
skills/browse_obs.py — browse session observability (live state + per-run artifact)
===================================================================================
Built 20 Nov 2026. Answers the two questions Chip could not previously answer
about its own browser:

  LIVE     — is a browse session running RIGHT NOW, what is it doing, on which
             route? Answered from PROCESS state (systemd + the cgroup's own
             procs list), not from tailing a log. Surfaced through the existing
             system_health tool; no new tool, no new permission surface.
  POST-HOC — what actually happened during a run that capped out, aborted or
             died? Answered by a per-run markdown artifact written into the
             read_file whitelist.

Why this exists: on 20 Nov 2026 a "home-route lease force-released
(holder-died)" notification was misdiagnosed as an orphaned session when it was
in fact verification run #3 of the home_route healer. Step counts and Signal
notifications were the only evidence available. That ambiguity is what this
module removes.

Design constraints, all load-bearing:
  * NON-BLOCKING. Every public entry point swallows its own exceptions and
    records them in OBS_ERRORS. Observability must never be able to take down
    browsing itself — a full disk degrades the artifact, it does not abort the
    run. health_block() reports the degradation.
  * ATOMIC heartbeat. Written to a temp file in the same directory and
    os.replace()d, so a concurrent reader can never see a torn JSON file. The
    heartbeat lives on /run (tmpfs): per-step writes don't touch the NVMe, and
    a reboot correctly erases "a session is running".
  * REDACT AT WRITE TIME, never as a cleanup pass. Nothing sensitive is ever
    on disk for even a moment. redact() is applied to URLs, typed text, page
    excerpts and outcome strings before they enter the record.
  * BOUNDED. 2KB of page excerpt per step, 256KB per artifact, 30 artifacts
    retained (enforced on write, not by a cron job). A 30-step run cannot fill
    the disk; 30 runs cannot either.

This module reads home_route.py's lease state. It does not change its
behaviour and holds no privileged logic of its own.
"""
import glob
import json
import os
import re
import time
from datetime import datetime

import os as _os, sys as _sys
_cc = _os.path.dirname(_os.path.abspath(__file__))
while _cc != "/" and not _os.path.exists(_os.path.join(_cc, "chip_config.py")):
    _cc = _os.path.dirname(_cc)
if _cc not in _sys.path:
    _sys.path.insert(0, _cc)
from chip_config import WORKSPACE

HEARTBEAT    = "/run/chip/browse_current.json"
ARTIFACT_DIR = f"{WORKSPACE}/data/archive/reports/browse"
BROWSER_UNIT = "chip-browser.service"
CGROUP_PROCS = "/sys/fs/cgroup/system.slice/chip-browser.service/cgroup.procs"

STALE_AFTER   = int(os.environ.get("CHIP_BROWSE_STALE_S", "90"))  # heartbeat staleness
EXCERPT_CAP   = 2048            # chars of page text kept per step
ARTIFACT_CAP  = 256 * 1024      # hard cap per artifact file
KEEP_RUNS     = 30              # artifacts retained; oldest rolled off on write
# Verification scripts write throwaway artifacts to prove the recorder works.
# They are real files but they are not runs, and at KEEP_RUNS=10 a single
# verification pass could roll every genuine run off the ring. They are marked
# on write and excluded from the retention count (raised to 30, 20 Nov 2026).
SYNTHETIC_MARK = "<!-- synthetic: verification filler, not a real run -->"

# Observability write failures, in-memory. The browse loop and system_health
# run in the same process (coordinator), so this is the honest channel for
# "the artifact could not be written" — reporting it through the artifact
# itself would be circular.
OBS_ERRORS = []


def _oops(where, exc):
    msg = f"{datetime.now().isoformat(timespec='seconds')} {where}: {type(exc).__name__}: {str(exc)[:160]}"
    OBS_ERRORS.append(msg)
    del OBS_ERRORS[:-10]
    print(f"⚠️ browse_obs {msg}", flush=True)


# ─── Redaction ────────────────────────────────────────────────────────────────
# Applied at write time to every string that reaches disk. Ordered broad-to-
# narrow: header forms first (they carry the most material), then named
# key=value / form fields, then bare token shapes.

_SENSITIVE_NAME = (r"pass(?:word|wd|phrase)?|pwd|secret|token|api[_-]?key|apikey|"
                   r"auth|authorization|bearer|session(?:[_-]?id)?|sessid|sid|"
                   r"csrf|otp|mfa|2fa|ssn|card(?:number)?|cc[_-]?num|cvv|cvc|pan|pin")

_REDACTIONS = [
    # Header forms, including the folded/JSON spellings a page dump can contain.
    (re.compile(r"(?i)\b(set-cookie|cookie|authorization|proxy-authorization|"
                r"x-api-key|x-auth-token|x-csrf-token)\b\s*[:=]\s*[^\r\n]*"),
     r"\1: [REDACTED]"),
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}"), "Bearer [REDACTED]"),
    (re.compile(r"(?i)\bbasic\s+[A-Za-z0-9+/=]{12,}"), "Basic [REDACTED]"),
    # Named field / query param / JSON key, whatever the separator.
    # (?!\[REDACTED) so a value the header rule already blanked isn't matched
    # again and left with a dangling bracket.
    (re.compile(r"(?i)\b(" + _SENSITIVE_NAME + r")\b(\"?\s*[:=]\s*\"?)"
                r"(?!\[REDACTED)[^\s,&;\"'\]}]{1,400}"), r"\1\2[REDACTED]"),
    # Bare provider key shapes, in case they appear with no name attached.
    (re.compile(r"\bsk-(?:ant-)?[A-Za-z0-9_-]{16,}"), "[REDACTED-KEY]"),
    (re.compile(r"\bAIza[0-9A-Za-z_-]{16,}"), "[REDACTED-KEY]"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}"), "[REDACTED-KEY]"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]+"),
     "[REDACTED-JWT]"),
]


def redact(value):
    """Strip credential material. Safe on any type; always returns a string."""
    try:
        s = value if isinstance(value, str) else json.dumps(value, default=str)
    except Exception:
        s = str(value)
    for pattern, repl in _REDACTIONS:
        s = pattern.sub(repl, s)
    return s


def _is_sensitive_field(label):
    """True when an element's label/placeholder/name names a secret — used to
    blank typed text wholesale rather than trusting the shape heuristics."""
    return bool(re.search(r"(?i)\b(" + _SENSITIVE_NAME + r")\b", label or ""))


# ─── Per-step detectors ───────────────────────────────────────────────────────
# What a step ran into, in the vocabulary the bail-condition accounting uses.

_DETECTORS = [
    ("captcha", re.compile(
        r"(?i)captcha|recaptcha|hcaptcha|cf[- ]challenge|are you a (?:human|robot)|"
        r"verify (?:you are|you're) (?:a )?human|press and hold|cloudflare")),
    ("access_denied", re.compile(
        r"(?i)access denied|403 forbidden|you (?:have been|are) blocked|"
        r"unusual traffic|pardon our interruption|bot detect|request blocked|"
        r"not authorized|rate ?limit")),
    ("chrome_error", re.compile(
        r"(?i)chrome-error://|ERR_[A-Z_]{3,}|this site can'?t be reached|"
        r"net::|dns_probe|connection (?:refused|reset)")),
    ("timeout", re.compile(r"(?i)\btimed? ?out\b|timeouterror|exceeded.*timeout")),
    ("login_wall", re.compile(
        r"(?i)sign in to continue|log ?in to continue|create an account to|"
        r"please sign in|members only")),
    ("stale_ref", re.compile(r"(?i)stale ref|call read again|unknown action")),
]


def detect(*texts):
    """Which failure conditions this step's material exhibits."""
    blob = " ".join(t for t in texts if t)[:20000]
    return [name for name, rx in _DETECTORS if rx.search(blob)]


# ─── Bail-condition accounting ────────────────────────────────────────────────
# The key diagnostic. A 30-step cap-out with no clean refusal means the bail
# conditions the admin wrote into the task never fired; the artifact has to
# make that legible instead of leaving it to be inferred from step counts.

_BAIL_LINE = re.compile(
    r"(?i)\b(bail|abort|stop(?:ping)? if|stop and|give up|don'?t keep|"
    r"do not keep|don'?t continue|do not continue|quit if|refuse|"
    r"if (?:you )?(?:hit|get|see|encounter|can'?t|cannot)|"
    r"after \d+ (?:tries|attempts|steps))\b")

# Which detector a stated condition is talking about, by its own words.
_BAIL_HINTS = [
    ("captcha",       re.compile(r"(?i)captcha|robot|human|cloudflare")),
    ("access_denied", re.compile(r"(?i)block|denied|403|forbidden|bot")),
    ("chrome_error",  re.compile(r"(?i)error page|unreachable|can'?t load|chrome-error|dns")),
    ("timeout",       re.compile(r"(?i)times? ?out|timed out|hang|slow|stall")),
    ("login_wall",    re.compile(r"(?i)log ?in|sign ?in|account|paywall")),
    ("empty_locator", re.compile(r"(?i)no (?:results|elements|links|products)|empty|nothing (?:on|found)")),
]


def parse_bail_conditions(task):
    """Pull stated bail conditions out of the admin's task text. Heuristic by
    necessity — the task is free prose — so the artifact prints the condition
    verbatim next to the verdict and lets the reader audit the mapping."""
    out = []
    for raw in re.split(r"[\n;]|(?<=[.!?])\s+", task or ""):
        line = " ".join(raw.split())
        if len(line) < 8 or not _BAIL_LINE.search(line):
            continue
        hits = [name for name, rx in _BAIL_HINTS if rx.search(line)]
        out.append({"text": line[:300], "detectors": hits})
        if len(out) >= 12:
            break
    return out


# ─── Cost ─────────────────────────────────────────────────────────────────────

def call_cost(model, usage):
    """USD for one API call. Cached reads bill at 0.1x input, cache writes at
    1.25x — breaking them out is what makes a cold-prefix run visible."""
    try:
        from skills.usage_tracker import PRICING
        p = PRICING.get(model) or {}
        pin, pout = float(p.get("input", 0.0)), float(p.get("output", 0.0))
        return ((usage.get("input", 0) * pin
                 + usage.get("cache_read", 0) * pin * 0.10
                 + usage.get("cache_write", 0) * pin * 1.25
                 + usage.get("output", 0) * pout) / 1_000_000.0)
    except Exception:
        return 0.0


def usage_of(resp):
    """Normalise an Anthropic response's usage into a plain dict."""
    u = getattr(resp, "usage", None)
    if u is None:
        return {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}
    return {"input": getattr(u, "input_tokens", 0) or 0,
            "output": getattr(u, "output_tokens", 0) or 0,
            "cache_read": getattr(u, "cache_read_input_tokens", 0) or 0,
            "cache_write": getattr(u, "cache_creation_input_tokens", 0) or 0}


# ─── Recorder ─────────────────────────────────────────────────────────────────

class RunRecorder:
    """One browse run. Owns the heartbeat and the artifact.

    Every method is individually fail-safe: a write error is recorded and the
    run carries on. The caller never has to guard a call to this class.
    """

    def __init__(self, task, route, user="?", step_cap=0, time_cap=0):
        self.run_id   = "%s%04x" % (datetime.now().strftime("%H%M"),
                                    int.from_bytes(os.urandom(2), "big"))
        self.task     = str(task or "")
        self.route    = route
        self.user     = user
        self.step_cap = step_cap
        self.time_cap = time_cap
        self.t0       = time.time()
        self.started  = datetime.now()
        self.exit_label = None
        self.steps    = []          # one dict per completed step
        self.cur      = None        # the step in flight
        self.cost     = 0.0
        self.tok      = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}
        self.bail     = parse_bail_conditions(self.task)
        self.fired    = {}          # detector -> [step numbers]
        self.finish_called = False
        self.handoffs = 0
        self.status   = "running"
        self.reason   = ""
        self.artifact = None
        # Route escalation and budget accounting (20 Nov 2026). These must
        # reach the artifact: "which route was this on, did we switch, and
        # why" is unanswerable afterwards if the run does not say so itself.
        self.notes      = []        # free-text events: suppressed handoffs, budget
        self.routes     = [route]   # every route this run used, in order
        self.escalation = None      # dict describing the one escalation, if any
        self.budget_lines = []      # StepBudget.lines() at finalize
        self.block_lines  = []      # BlockLedger.summary() at finalize
        self._beat()

    def step_note(self, text):
        """Record something that happened inside a step but is not the step's
        own action — a suppressed handoff, a budget closure, an escalation."""
        try:
            n = (self.cur or {}).get("n", len(self.steps))
            self.notes.append(f"step {n}: {text}")
        except Exception as e:
            _oops("step_note", e)

    def note_escalation(self, frm, to, host, events):
        try:
            self.escalation = {"from": frm, "to": to, "host": host,
                               "signatures": sorted({e["signature"] for e in events}),
                               "detail": events[-1]["detail"] if events else ""}
            self.routes.append(to)
            self.route = to
            self.step_note(f"route escalation {frm} -> {to} for {host}")
        except Exception as e:
            _oops("note_escalation", e)

    # ── heartbeat ────────────────────────────────────────────────────────────
    def _payload(self):
        s = self.cur or (self.steps[-1] if self.steps else {})
        return {
            "run_id": self.run_id, "pid": os.getpid(),
            "status": self.status, "route": self.route,
            "started": self.started.isoformat(timespec="seconds"),
            "updated": datetime.now().isoformat(timespec="seconds"),
            "step": s.get("n", 0), "step_cap": self.step_cap,
            "url": redact(s.get("url", ""))[:300],
            "last_action": redact(s.get("action", ""))[:200],
            "last_outcome": redact(s.get("outcome", ""))[:200],
            "cost_usd": round(self.cost, 4),
            "tokens": dict(self.tok),
            "task": redact(self.task)[:200],
            "artifact": os.path.basename(self.artifact) if self.artifact else None,
            "obs_errors": OBS_ERRORS[-3:],
        }

    def _beat(self):
        """Atomic: temp file in the same directory, then rename. A concurrent
        reader sees either the old file or the new one, never a torn one."""
        try:
            os.makedirs(os.path.dirname(HEARTBEAT), exist_ok=True)
            tmp = f"{HEARTBEAT}.{os.getpid()}.tmp"
            with open(tmp, "w") as f:
                json.dump(self._payload(), f)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, HEARTBEAT)
        except Exception as e:
            _oops("heartbeat", e)

    # ── steps ────────────────────────────────────────────────────────────────
    def step_start(self, n):
        self.cur = {"n": n, "t": time.time(), "url": "", "action": "(deciding)",
                    "outcome": "", "flags": [], "cost": 0.0, "excerpt": ""}
        self._beat()

    def note_model(self, model, usage):
        """Account one loop call. Cost is attributed to the step in flight."""
        try:
            c = call_cost(model, usage)
            self.cost += c
            for k in self.tok:
                self.tok[k] += usage.get(k, 0)
            if self.cur:
                self.cur["cost"] += c
                self.cur["model"] = model
        except Exception as e:
            _oops("note_model", e)

    def step_end(self, action="", url="", outcome="", status=None,
                 page_text="", extra_flags=()):
        try:
            s = self.cur or {"n": len(self.steps) + 1, "t": time.time()}
            s["action"]  = redact(action)[:300]
            s["url"]     = redact(url)[:300]
            s["outcome"] = redact(outcome)[:400]
            s["http"]    = status
            s["secs"]    = round(time.time() - s.get("t", time.time()), 1)
            text = redact(page_text or "")
            if len(text) > EXCERPT_CAP:
                text = text[:EXCERPT_CAP] + f"\n…[excerpt truncated — {len(text)} chars on page]"
            s["excerpt"] = text
            flags = set(detect(s["outcome"], s["url"], page_text)) | set(extra_flags)
            if isinstance(status, int) and status >= 400:
                flags.add("http_error")
            s["flags"] = sorted(flags)
            for f in s["flags"]:
                self.fired.setdefault(f, []).append(s["n"])
            self.steps.append(s)
            self.cur = None
            # Rewrite the artifact every step rather than only at the end: a
            # SIGKILLed run never reaches finalize(), and a run that dies is
            # exactly the one whose trace is worth having. The cost is one
            # bounded file write per step, against a step that just made an
            # API call and a page load.
            self._snapshot()
            self._beat()
        except Exception as e:
            _oops("step_end", e)

    def _snapshot(self):
        try:
            self.artifact = self._write_artifact("")
        except Exception as e:
            _oops("artifact-snapshot", e)

    # ── finalisation ─────────────────────────────────────────────────────────
    def finalize(self, status, reason, result_text="", exit_label=None):
        """Terminal status + artifact + heartbeat cleanup. Called from browse's
        finally:, so it runs on success, cap-out, abort and crash alike."""
        self.status = status
        self.reason = reason or status
        if exit_label:
            self.exit_label = exit_label
        try:
            self.artifact = self._write_artifact(result_text)
        except Exception as e:
            _oops("artifact", e)
        try:
            self._beat()             # final beat carries the terminal status
            os.remove(HEARTBEAT)     # no live session → no current.json
        except FileNotFoundError:
            pass
        except Exception as e:
            _oops("heartbeat-clear", e)
        return self.artifact

    def _route_line(self):
        if self.route == "home":
            return "home ISP (scoped to the browser cgroup; rest of box on Mullvad)"
        return f"vpn / Mullvad ({self.exit_label or 'exit unknown'})"

    def _bail_section(self):
        out = []
        if not self.bail:
            out.append("  (no explicit bail conditions parsed from the task text)")
        for i, b in enumerate(self.bail, 1):
            out.append(f"  {i}. \"{b['text']}\"")
            if not b["detectors"]:
                out.append("     → no detector maps to this condition — "
                           "judge it from the step trace above.")
                continue
            for d in b["detectors"]:
                hits = self.fired.get(d)
                if hits:
                    out.append(f"     → TRIGGERED: {d} fired at step(s) "
                               f"{', '.join(map(str, hits))}.")
                else:
                    out.append(f"     → NOT TRIGGERED: {d} was never detected "
                               f"in {len(self.steps)} step(s).")
        out.append("")
        out.append("  Detector tally across the run:")
        for name, _rx in _DETECTORS:
            hits = self.fired.get(name)
            out.append(f"    {name:<15} " + (f"steps {', '.join(map(str, hits))}"
                                             if hits else "never fired"))
        for extra in sorted(set(self.fired) - {n for n, _ in _DETECTORS}):
            out.append(f"    {extra:<15} steps "
                       f"{', '.join(map(str, self.fired[extra]))}")
        out.append("")
        if self.finish_called:
            out.append("  Clean refusal/completion: YES — the agent called finish().")
        else:
            out.append(f"  Clean refusal/completion: NO — finish() was never called; "
                       f"the run ended as '{self.status}'. If bail conditions were "
                       f"stated and none fired above, they did not match what the "
                       f"pages actually returned.")
        return "\n".join(out)

    def _cost_section(self):
        t = self.tok
        billed_in = t["input"] + t["cache_read"] + t["cache_write"]
        ratio = (100.0 * t["cache_read"] / billed_in) if billed_in else 0.0
        return (f"  input (uncached) : {t['input']:,}\n"
                f"  cache read       : {t['cache_read']:,}\n"
                f"  cache write      : {t['cache_write']:,}\n"
                f"  output           : {t['output']:,}\n"
                f"  cached-read ratio: {ratio:.1f}%  "
                + ("(cold prefix — little or no cache reuse)" if ratio < 20
                   else "(warm prefix)") + "\n"
                f"  total cost       : ${self.cost:.4f}")

    def _write_artifact(self, result_text):
        os.makedirs(ARTIFACT_DIR, exist_ok=True)
        name = f"{self.started.strftime('%Y%m%d_%H%M%S')}_{self.run_id}.md"
        path = os.path.join(ARTIFACT_DIR, name)
        end  = datetime.now()

        running = self.status == "running"
        L = [f"# browse run {self.run_id}", ""]
        L.append(f"- status      : **{self.status}**")
        L.append("- abort reason: " + (self.reason if self.reason else
                 ("IN FLIGHT — this artifact is rewritten after every step and "
                  "finalised when the run ends. If it still says 'running', the "
                  "run was killed before it could finalise; the last row of the "
                  "step trace is the last step that completed."
                  if running else "(none recorded)")))
        L.append(f"- task        : {redact(self.task)[:1000]}")
        L.append(f"- route       : {self._route_line()}")
        L.append(f"- user        : {self.user}")
        L.append(f"- started     : {self.started.isoformat(timespec='seconds')}")
        L.append(f"- ended       : {end.isoformat(timespec='seconds')}")
        L.append(f"- wall        : {int(end.timestamp() - self.t0)}s "
                 f"(cap {self.time_cap}s)")
        L.append(f"- steps       : {len(self.steps)} of {self.step_cap}")
        L.append(f"- handoffs    : {self.handoffs}")
        L.append(f"- cost        : ${self.cost:.4f}")
        if OBS_ERRORS:
            L.append(f"- obs errors  : {'; '.join(OBS_ERRORS[-3:])}")
        L += ["", "## Step trace", ""]
        L.append("| # | action | url | outcome | http | flags | s | $ |")
        L.append("|---|--------|-----|---------|------|-------|---|---|")
        for s in self.steps:
            L.append("| {n} | {a} | {u} | {o} | {h} | {f} | {t} | {c:.4f} |".format(
                n=s.get("n"), a=s.get("action", "")[:60].replace("|", "/"),
                u=s.get("url", "")[:70].replace("|", "/"),
                o=s.get("outcome", "")[:80].replace("|", "/").replace("\n", " "),
                h=s.get("http") if s.get("http") is not None else "-",
                f=",".join(s.get("flags") or []) or "-",
                t=s.get("secs", 0), c=s.get("cost", 0.0)))
        L += ["", "## Route and blocks", ""]
        L.append(f"- routes used : {' -> '.join(self.routes)}")
        if self.escalation:
            e = self.escalation
            L.append(f"- escalation  : **{e['from']} -> {e['to']}** for "
                     f"{e['host']} ({', '.join(e['signatures'])})")
            L.append(f"- why         : {e['detail']}")
        else:
            L.append("- escalation  : none — the run stayed on one route")
        for line in (self.block_lines or ["No route blocks classified."]):
            L.append(f"  {line}")

        L += ["", "## Step budget", ""]
        for line in (self.budget_lines or ["(not recorded)"]):
            L.append(f"  {line}")

        if self.notes:
            L += ["", "## Notes", ""]
            L += [f"  {n}" for n in self.notes[-40:]]

        L += ["", "## Bail-condition accounting", "", self._bail_section()]
        L += ["", "## Cost accounting", "", self._cost_section()]
        L += ["", "## Result", "", redact(result_text or "(none)")[:4000]]
        L += ["", "## Page excerpts", ""]
        for s in self.steps:
            if s.get("excerpt"):
                L.append(f"### step {s['n']} — {s.get('url', '')[:120]}")
                L.append("```")
                L.append(s["excerpt"])
                L.append("```")
                L.append("")

        body = "\n".join(L)
        if len(body) > ARTIFACT_CAP:
            body = body[:ARTIFACT_CAP - 80] + \
                   f"\n\n[ARTIFACT TRUNCATED at {ARTIFACT_CAP} bytes]\n"
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            f.write(body)
        os.replace(tmp, path)
        _prune()
        return path


def is_synthetic(path):
    """Cheap check — the marker is written into the first bytes of the file."""
    try:
        with open(path) as f:
            return SYNTHETIC_MARK in f.read(4096)
    except Exception:
        return False


def _prune():
    """Retention, enforced on write. No cron, nothing to forget to install.

    Synthetic filler is pruned on its own much shorter ring, so a verification
    pass can never evict a real run from the 30 it is supposed to keep."""
    try:
        files  = sorted(glob.glob(os.path.join(ARTIFACT_DIR, "*.md")))
        synth  = [f for f in files if is_synthetic(f)]
        real   = [f for f in files if f not in set(synth)]
        for old in real[:-KEEP_RUNS] + synth[:-5]:
            try:
                os.remove(old)
            except Exception:
                pass
    except Exception as e:
        _oops("prune", e)


# ─── Live state (for system_health) ───────────────────────────────────────────

def _unit(name):
    """systemd state without shelling out to a parser: one show call."""
    import subprocess
    try:
        out = subprocess.run(
            ["systemctl", "show", name, "--property=ActiveState",
             "--property=SubState", "--property=MainPID",
             "--property=ExecMainStartTimestampMonotonic"],
            capture_output=True, text=True, timeout=10).stdout
        d = dict(l.split("=", 1) for l in out.strip().splitlines() if "=" in l)
    except Exception:
        return {}
    try:
        with open("/proc/uptime") as f:
            now_mono = float(f.read().split()[0])
        started = int(d.get("ExecMainStartTimestampMonotonic", 0)) / 1_000_000.0
        d["uptime_s"] = int(now_mono - started) if started else None
    except Exception:
        d["uptime_s"] = None
    return d


def _proc(pid):
    try:
        with open(f"/proc/{pid}/comm") as f:
            comm = f.read().strip()
        rss = 0
        with open(f"/proc/{pid}/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    rss = int(line.split()[1]) // 1024
                    break
        return {"pid": pid, "comm": comm, "rss_mb": rss}
    except Exception:
        return None


def cgroup_procs():
    """Every process in the browser unit's cgroup, Chromium or not."""
    procs = []
    try:
        with open(CGROUP_PROCS) as f:
            pids = [int(p) for p in f.read().split()]
    except Exception:
        return procs
    for pid in pids:
        p = _proc(pid)
        if p:
            procs.append(p)
    return procs


def _is_chromium(comm):
    return bool(re.search(r"(?i)chrom|headless_shell", comm or ""))


def browser_procs():
    """The real browser, from the unit's own cgroup. A live wrapper with a dead
    Chromium is exactly the condition that log-tailing cannot see, so this
    looks for the renderer binary rather than trusting MainPID."""
    return [p for p in cgroup_procs() if _is_chromium(p["comm"])]


PROFILE_DIR   = f"{WORKSPACE}/data/browser_profiles"
_LOCK_NAMES   = ("SingletonLock", "SingletonCookie", "SingletonSocket")


def _cmdline(pid):
    """Full argv. _proc() deliberately reads only comm (it is called per-pid
    on every health check), but telling our Chromium from any other Chromium
    needs the profile path, which only appears in argv."""
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            return f.read().replace(b"\0", b" ").decode("utf-8", "replace")
    except Exception:
        return ""


def stray_chromium():
    """Chromium processes that are NOT in the browser unit's cgroup.

    This is the child-without-wrapper case: the unit died or was restarted and
    left a renderer behind, so the next launch fights it for the profile. The
    cgroup list is the definition of 'ours', so anything matching the binary
    outside it is by construction an escapee."""
    return scan_strays()["strays"]


def scan_strays():
    """stray_chromium() with its failure visible. NEVER raises.
    Returns {"ok", "error", "strays"}."""
    res = {"ok": True, "error": None, "strays": []}
    ours = {p["pid"] for p in browser_procs()}
    strays = res["strays"]
    try:
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            pid = int(entry)
            if pid in ours:
                continue
            p = _proc(pid)
            if not p:
                continue
            # Match on the profile path, not just the binary name: the box may
            # legitimately run other Chromium-based things.
            if re.search(r"(?i)chrom|headless_shell", p["comm"]) and \
               PROFILE_DIR in _cmdline(pid):
                strays.append(p)
    except Exception as e:
        res["ok"] = False
        res["error"] = f"{type(e).__name__}: {str(e)[:120]}"
        _oops("stray_chromium", e)
    return res


def _pid_max():
    try:
        with open("/proc/sys/kernel/pid_max") as f:
            return int(f.read().strip())
    except Exception:
        return 4194304


def valid_pid(value):
    """A pid we are willing to hand to os.kill(), or None.

    Load-bearing range check. Only SingletonLock's symlink target encodes a
    pid; SingletonCookie's encodes a large RANDOM number, and on 2026-11-20
    feeding one of those to os.kill() raised
    `OverflowError: signed integer is greater than maximum` (pid_t is a C int)
    on every system_health call. The scanner aborted, reported no orphans, and
    the false all-clear was believed. Anything outside 1..pid_max is not a pid
    and never reaches the kernel."""
    try:
        pid = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return pid if 1 <= pid <= _pid_max() else None


def scan_profile_locks():
    """Classify Chromium singleton lock files per profile. NEVER raises.

    Returns {"ok", "error", "stale", "unknown", "live"} where the three lists
    hold lock paths:
      live    — owner pid parsed and still running; leave it alone.
      stale   — owner pid parsed and gone; what a hard kill leaves behind,
                and what Chromium refuses to launch against.
      unknown — owner could not be established (no SingletonLock, unreadable
                link, or a value that is not a plausible pid). Treated as
                stale by the reaper, but reported as unknown rather than
                asserted clean.

    Ownership is decided PER PROFILE from SingletonLock, the only one of the
    three that carries a pid, and applied to its siblings. Judging
    SingletonCookie/SingletonSocket on their own is what both crashed the
    scanner and risked unlinking a live session's socket."""
    res = {"ok": True, "error": None, "stale": [], "unknown": [], "live": []}
    try:
        for prof in sorted(glob.glob(os.path.join(PROFILE_DIR, "*"))):
            if not os.path.isdir(prof):
                continue
            present = [os.path.join(prof, n) for n in _LOCK_NAMES
                       if os.path.lexists(os.path.join(prof, n))]
            if not present:
                continue
            owner = None
            try:                       # SingletonLock is a symlink: host-pid
                target = os.readlink(os.path.join(prof, "SingletonLock"))
                owner = valid_pid(target.rsplit("-", 1)[-1])
            except Exception:
                owner = None
            if owner is None:
                res["unknown"] += present
            else:
                try:
                    bucket = "live" if alive(owner) else "stale"
                except Exception:      # defence in depth: alive() must not decide
                    bucket = "unknown"
                res[bucket] += present
    except Exception as e:
        res["ok"] = False
        res["error"] = f"{type(e).__name__}: {str(e)[:120]}"
        _oops("stale_profile_locks", e)
    return res


def stale_profile_locks():
    """Lock paths the reaper should clear: owner dead, or owner unknowable.
    Live locks are excluded. Never raises; see scan_profile_locks()."""
    res = scan_profile_locks()
    return res["stale"] + res["unknown"]


def reap(dry_run=False):
    """Pre-flight reaper and post-run verifier.

    Handles BOTH asymmetric cases seen on 2026-11-20:
      * child without wrapper — a Chromium holding our profile while the unit
        is not running it. Killed.
      * wrapper without child — the unit is active but owns no renderer. NOT
        killed here: that is the service's own idle state between sessions and
        killing it would restart the service under every browse. It is
        reported so system_health can show it.

    Returns a dict; never raises.
    """
    report = {"killed": [], "locks_cleared": [], "wrapper_without_child": False,
              "survivors": [], "errors": []}
    try:
        sres = scan_strays()
        if not sres["ok"]:
            report["errors"].append(f"stray scan failed: {sres['error']}")
        for p in sres["strays"]:
            if dry_run:
                report["survivors"].append(p["pid"])
                continue
            for sig in (15, 9):
                try:
                    os.kill(p["pid"], sig)
                except ProcessLookupError:
                    break
                except Exception as e:
                    report["errors"].append(f"kill {p['pid']}: {e}")
                    break
                time.sleep(0.4 if sig == 15 else 0.2)
                if not alive(p["pid"]):
                    break
            (report["killed"] if not alive(p["pid"]) else report["survivors"]
             ).append(p["pid"])

        lockscan = scan_profile_locks()
        if not lockscan["ok"]:
            report["errors"].append(f"lock scan failed: {lockscan['error']}")
        for lock in lockscan["stale"] + lockscan["unknown"]:
            if dry_run:
                continue
            try:
                os.unlink(lock)
                report["locks_cleared"].append(os.path.basename(
                    os.path.dirname(lock)) + "/" + os.path.basename(lock))
            except Exception as e:
                report["errors"].append(f"unlink {lock}: {e}")

        report["wrapper_without_child"] = (
            _unit(BROWSER_UNIT).get("ActiveState") == "active"
            and not browser_procs())
    except Exception as e:
        _oops("reap", e)
        report["errors"].append(str(e)[:200])
    return report


def alive(pid):
    """Is this pid running? False for anything that is not a plausible pid.

    The range check happens before os.kill, not after: an out-of-range value
    raises OverflowError, which is not a subclass of ValueError and so used to
    escape every caller's except clause."""
    pid = valid_pid(pid)
    if pid is None:
        return False
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, ValueError, OverflowError):
        return False
    except PermissionError:
        return True


def read_heartbeat():
    try:
        with open(HEARTBEAT) as f:
            hb = json.load(f)
        hb["_age_s"] = int(time.time() - os.path.getmtime(HEARTBEAT))
        return hb
    except Exception:
        return None


def health_block(stale_after=None):
    """The browse_session block appended to system_health. Plain text, and it
    states the verdict — stalled / died mid-run / idle — rather than printing
    raw timestamps for the reader to difference."""
    stale = STALE_AFTER if stale_after is None else stale_after
    L = ["", "browse_session:"]
    try:
        u = _unit(BROWSER_UNIT)
        state = u.get("ActiveState", "unknown")
        pid   = u.get("MainPID", "0")
        up    = u.get("uptime_s")
        L.append(f"  unit {BROWSER_UNIT}: {state}/{u.get('SubState','?')}"
                 + (f"  pid {pid}" if pid and pid != "0" else "")
                 + (f"  up {up}s" if state == "active" and up is not None else ""))

        # Every process in the cgroup, named. systemd's own "Tasks: N" counts
        # THREADS, and on 2026-11-20 its Tasks:15 was read as "16 orphaned
        # Chromium children" when the cgroup in fact held five healthy support
        # processes and no Chromium at all. Killing them would have taken the
        # service down. Name them so that misread cannot repeat.
        members = cgroup_procs()
        if members:
            L.append(f"  unit cgroup: {len(members)} processes — " + ", ".join(
                f"{p['comm']}({p['pid']})" for p in members[:8])
                + (f" (+{len(members)-8} more)" if len(members) > 8 else "")
                + f"; {sum(1 for p in members if _is_chromium(p['comm']))} are "
                  "Chromium. systemd 'Tasks:' counts threads, not children.")

        procs = browser_procs()
        if procs:
            L.append("  browser process: ALIVE — " + ", ".join(
                f"{p['comm']} pid {p['pid']} rss {p['rss_mb']}MB" for p in procs[:4])
                + (f" (+{len(procs)-4} more)" if len(procs) > 4 else ""))
        elif state == "active":
            L.append("  browser process: none — wrapper is up but no Chromium is "
                     "running (idle, or the browser died under its parent)")
        else:
            L.append("  browser process: none (unit not active)")

        # ── orphans the reaper would have to deal with ───────────────────
        # Reported, never reaped from here: system_health is a read.
        #
        # The summary line states which of three things is true, and a failed
        # scan is never spelled "none". On 2026-11-20 the scanner crashed on
        # every call and this line still read "orphans: none" — an all-clear
        # nothing had verified.
        try:
            sres  = scan_strays()
            lres  = scan_profile_locks()
            strays, stale, unknown = sres["strays"], lres["stale"], lres["unknown"]
            if strays:
                L.append("  ⚠️ ORPHANS: " + ", ".join(
                    f"{p['comm']} pid {p['pid']}" for p in strays[:4])
                    + (f" (+{len(strays)-4} more)" if len(strays) > 4 else "")
                    + " — Chromium holding our profile from outside the unit's "
                      "cgroup. The pre-flight reaper kills these before the "
                      "next launch.")
            if stale:
                L.append(f"  ⚠️ STALE PROFILE LOCKS: {', '.join(stale[:4])} — "
                         f"owner pid gone; cleared by the pre-flight reaper.")
            if unknown:
                L.append(f"  ⚠️ PROFILE LOCKS, OWNER UNKNOWN: "
                         f"{', '.join(unknown[:4])} — no readable owner pid. "
                         f"Treated as stale by the pre-flight reaper.")
            failures = [e for e in (sres["error"], lres["error"]) if e]
            if failures:
                L.append("  orphans: UNKNOWN — scanner failed: "
                         + "; ".join(failures))
            elif strays or stale or unknown:
                L.append(f"  orphans: {len(strays) + len(stale) + len(unknown)} "
                         f"found ({len(strays)} stray chromium, {len(stale)} "
                         f"stale locks, {len(unknown)} unknown-owner locks)")
            else:
                L.append("  orphans: none (scanned OK — no stray Chromium, "
                         "no stale profile locks)")
        except Exception as e:
            L.append(f"  orphans: UNKNOWN — scanner failed: "
                     f"{type(e).__name__}: {str(e)[:120]}")

        # ── home-route lease ─────────────────────────────────────────────
        try:
            import home_route
            st    = home_route.status()
            lease = st.get("lease")
            if not st.get("rule_active") and not lease:
                L.append("  route: vpn (full tunnel) — no home-route lease held")
            else:
                held = "held" if st.get("rule_active") else "NOT held (lease file only)"
                L.append(f"  route: {st.get('route')} — lease {held}")
                if lease:
                    hp = lease.get("pid")
                    try:
                        age = int(time.time() - datetime.fromisoformat(
                            lease["started"]).timestamp())
                    except Exception:
                        age = -1
                    rem = st.get("remaining_s")
                    L.append(f"    holder pid {hp} ({'ALIVE' if alive(hp) else 'DEAD'})"
                             f"  age {age}s  remaining "
                             f"{rem if rem is not None else 'EXPIRED'}s"
                             f"  user {lease.get('user','?')}")
                    if hp and not alive(hp):
                        L.append("    ⚠️ HOLDER-DIED: the lease is held by a dead pid. "
                                 "vpn_guard's reconciler will force-release it "
                                 "(or run home_route.py enforce).")
                    elif hp and str(hp) != str(pid):
                        L.append("    note: the holder is the coordinator process "
                                 "(the browse loop runs in chip.service, not in "
                                 f"{BROWSER_UNIT}), so holder pid != browser pid "
                                 "is normal.")
                    L.append(f"    failsafe timer: {st.get('failsafe_timer','?')}")
        except Exception as e:
            L.append(f"  route: lease state unavailable ({type(e).__name__})")

        try:
            from skills import browse as _b
            node = _b.exit_node_label()
            if node:
                L.append(f"  mullvad exit: {node}")
        except Exception:
            pass

        # ── heartbeat + staleness verdict ────────────────────────────────
        hb = read_heartbeat()
        if not hb:
            L.append("  run: idle — no heartbeat file, no browse session in flight")
        else:
            age = hb.get("_age_s", 0)
            L.append(f"  run {hb.get('run_id')}: step {hb.get('step')}"
                     f"/{hb.get('step_cap')}  route {hb.get('route')}  "
                     f"${hb.get('cost_usd', 0)}  updated {age}s ago")
            L.append(f"    task   : {str(hb.get('task',''))[:120]}")
            L.append(f"    url    : {str(hb.get('url',''))[:120]}")
            L.append(f"    action : {hb.get('last_action','')} → "
                     f"{str(hb.get('last_outcome',''))[:80]}")
            terminal = hb.get("status") not in (None, "", "running")
            if terminal:
                L.append(f"    VERDICT: finished ({hb.get('status')}) — stale "
                         f"heartbeat left behind; artifact "
                         f"{hb.get('artifact') or '(none)'}")
            elif state != "active":
                L.append(f"    VERDICT: DIED MID-RUN at step {hb.get('step')} — "
                         f"heartbeat has no terminal status and "
                         f"{BROWSER_UNIT} is {state}.")
            elif not alive(hb.get("pid", 0)):
                L.append(f"    VERDICT: DIED MID-RUN at step {hb.get('step')} — "
                         f"the browse loop (pid {hb.get('pid')}) is gone.")
            elif age > stale:
                L.append(f"    VERDICT: STALLED — unit active but no step "
                         f"progress for {age}s (threshold {stale}s).")
            else:
                L.append("    VERDICT: running normally.")
            if hb.get("obs_errors"):
                L.append(f"    obs errors: {'; '.join(hb['obs_errors'])}")

        # ── artifacts + observability failures ───────────────────────────
        try:
            arts = sorted(glob.glob(os.path.join(ARTIFACT_DIR, "*.md")))
            L.append(f"  artifacts: {len(arts)} kept (max {KEEP_RUNS}) in "
                     f"reports/browse/"
                     + (f"; latest {os.path.basename(arts[-1])}" if arts else ""))
        except Exception:
            pass
        if OBS_ERRORS:
            L.append("  ⚠️ observability degraded (browsing unaffected): "
                     + "; ".join(OBS_ERRORS[-3:]))
    except Exception as e:
        L.append(f"  (browse state unavailable: {type(e).__name__}: {str(e)[:120]})")
    return "\n".join(L)


if __name__ == "__main__":
    print(health_block())
