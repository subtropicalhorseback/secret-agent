"""
skills/browse_capture.py — the durable visual record of a browse run
=====================================================================
Built 20 Nov 2026 (FIX-0006, phases 1 and 7). Two failures motivated this:

  1. Screenshots died with the sub-agent. op_screenshot() returned base64,
     the loop handed it straight to the model, and `r.pop("image_b64")` threw
     it away. Nothing crossed the return boundary but text, so there was no
     way to look at what the run actually saw after it ended.
  2. A worker that died mid-run left no readable trace of where it got to.

So: every screenshot is written to

    <CAPTURE_ROOT>/<run_id>/stepNNN.png

zero-padded and monotonically increasing, alongside

    <CAPTURE_ROOT>/<run_id>/manifest.json

which is the durable record of the run. The manifest is rewritten (atomically,
fsynced) on EVERY step, not assembled at the end, because the run worth having
a trace of is precisely the one that dies before it can finalise. A killed run
leaves a manifest whose status is still "running" and whose last_step names how
far it got — that is the signal, not a defect.

Contract for callers: a capture is referenced by PATH STRING. Image bytes are
never returned from here and must never be passed across an agent boundary.

The heartbeat field (phase 7) lives in the same manifest. browse_obs keeps its
own /run tmpfs heartbeat for "is something running right now"; this one is on
disk and survives a reboot, so the staleness check can still name a run that
died with the box.
"""
import glob
import json
import os
import shutil
import time
from datetime import datetime

import os as _os, sys as _sys
_cc = _os.path.dirname(_os.path.abspath(__file__))
while _cc != "/" and not _os.path.exists(_os.path.join(_cc, "chip_config.py")):
    _cc = _os.path.dirname(_cc)
if _cc not in _sys.path:
    _sys.path.insert(0, _cc)
from chip_config import WORKSPACE

# Same reports/browse root browse_obs writes its markdown artifact into, so a
# run's prose and its pictures sit side by side. CHIP_BROWSE_CAPTURE_ROOT is
# the test seam — verification runs redirect it at a temp dir rather than
# littering the real archive.
CAPTURE_ROOT = os.environ.get(
    "CHIP_BROWSE_CAPTURE_ROOT", f"{WORKSPACE}/data/archive/reports/browse")

KEEP_RUNS = 30          # capture directories retained; matches browse_obs


# ─── Config constants ─────────────────────────────────────────────────────────
# The tunables live in config/routing_constants.py (falling back to the tracked
# .example.py, then to the defaults below) so timing and deadlines are config,
# not literals buried in the loop. Same loader shape coordinator.py uses.

_DEFAULTS = {
    "BROWSE_ACTION_DELAY_MIN":  0.3,   # seconds, uniform, before an interaction
    "BROWSE_ACTION_DELAY_MAX":  1.3,
    "BROWSE_VISION_CANDIDATES": 3,     # ranked readings the oracle returns
    "BROWSE_REPLY_DEADLINE_S":  90,    # worker's hard wait for a reply DOWN
    "BROWSE_STALL_AFTER_S":     180,   # heartbeat age that counts as stalled
}


def _load_rc():
    import importlib.util
    for fname in ("routing_constants.py", "routing_constants.example.py"):
        path = os.path.join(WORKSPACE, "config", fname)
        if not os.path.exists(path):
            continue
        try:
            spec = importlib.util.spec_from_file_location("browse_rc", path)
            mod  = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return mod
        except Exception:
            continue
    return None


_RC = _load_rc()


def conf(name):
    """A browse tunable: config first, built-in default second. Never raises —
    a broken config file degrades to the defaults rather than killing a run."""
    default = _DEFAULTS[name]
    try:
        value = getattr(_RC, name, default)
        return type(default)(value)
    except Exception:
        return default


# ─── Capture store ────────────────────────────────────────────────────────────

_EXT = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}


class CaptureStore:
    """Per-run directory of screenshots plus the manifest that indexes them.

    Fail-safe in the same sense as browse_obs.RunRecorder: every public method
    swallows its own exceptions and records them in the manifest's `errors`
    list. A full disk degrades the record; it never aborts the run.
    """

    def __init__(self, run_id, task="", route="", user="?"):
        self.run_id = str(run_id)
        self.dir    = os.path.join(CAPTURE_ROOT, self.run_id)
        self.captures = []
        self.events   = []
        self.errors   = []
        self.status   = "running"
        self.reason   = ""
        self.last_step = 0
        self.started   = datetime.now()
        self.heartbeat = self.started
        self._pruned   = False
        self._mt       = "image/png"   # media type of the last capture
        self._last     = {}            # last step's url/action/outcome
        try:
            from skills import browse_obs
            self._redact = browse_obs.redact
        except Exception:
            self._redact = lambda v: str(v)
        self.task  = self._redact(task)[:500]
        self.route = str(route or "")
        self.user  = str(user or "?")

    # ── paths ────────────────────────────────────────────────────────────────
    @property
    def manifest_path(self):
        return os.path.join(self.dir, "manifest.json")

    def step_name(self, step, media_type="image/png"):
        return "step%03d.%s" % (int(step), _EXT.get(media_type, "png"))

    # ── writing ──────────────────────────────────────────────────────────────
    def capture(self, step, png_bytes, url="", action="screenshot",
                description=""):
        """Write one screenshot and index it. Returns the absolute path, or
        None if it could not be written. NEVER returns bytes."""
        try:
            os.makedirs(self.dir, exist_ok=True)
            self._prune_once()
            path = os.path.join(self.dir, self.step_name(step, self._mt))
            tmp  = path + ".tmp"
            with open(tmp, "wb") as f:
                f.write(png_bytes)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
            self.captures.append({
                "step":        int(step),
                "path":        os.path.abspath(path),
                "ts":          datetime.now().isoformat(timespec="seconds"),
                "url":         self._redact(url)[:500],
                "action":      self._redact(action)[:200],
                "description": self._redact(description)[:300],
            })
            self.last_step = max(self.last_step, int(step))
            self._write()
            return os.path.abspath(path)
        except Exception as e:
            self._oops("capture", e)
            return None

    def save_b64(self, step, image_b64, media_type="image/png", url="",
                 action="screenshot", description=""):
        """The browse loop's entry point: the service hands back base64, we put
        it on disk and hand back a path. Decoding happens here so no caller has
        to hold bytes."""
        import base64
        try:
            self._mt = media_type or "image/png"
            return self.capture(step, base64.b64decode(image_b64), url=url,
                                action=action, description=description)
        except Exception as e:
            self._oops("save_b64", e)
            return None

    def touch(self, step, url="", action="", outcome=""):
        """Phase 7 heartbeat: advance the run's durable liveness field. Called
        on every step, screenshot or not, so a stalled run is one whose
        manifest heartbeat has stopped moving."""
        try:
            self.heartbeat = datetime.now()
            self.last_step = max(self.last_step, int(step or 0))
            self._last = {"url": self._redact(url)[:300],
                          "action": self._redact(action)[:200],
                          "outcome": self._redact(outcome)[:300]}
            self._write()
        except Exception as e:
            self._oops("touch", e)

    def event(self, text):
        """Something that happened to the run but is not a step — a reply
        deadline expiring, a stall notice, a block. Lands in the manifest."""
        try:
            self.events.append(
                f"{datetime.now().isoformat(timespec='seconds')} "
                f"{self._redact(text)[:400]}")
            self._write()
        except Exception as e:
            self._oops("event", e)

    def finalize(self, status, reason=""):
        try:
            self.status = str(status)
            self.reason = self._redact(reason)[:600]
            self.heartbeat = datetime.now()
            self._write()
        except Exception as e:
            self._oops("finalize", e)
        return self.manifest_path

    # ── internals ────────────────────────────────────────────────────────────
    def _payload(self):
        return {
            "run_id":    self.run_id,
            "pid":       os.getpid(),
            "task":      self.task,
            "route":     self.route,
            "user":      self.user,
            "status":    self.status,
            "reason":    self.reason,
            "started":   self.started.isoformat(timespec="seconds"),
            "heartbeat": self.heartbeat.isoformat(timespec="seconds"),
            "last_step": self.last_step,
            "last":      dict(self._last or {}),
            "captures":  list(self.captures),
            "events":    list(self.events),
            "errors":    self.errors[-10:],
        }

    def _write(self):
        """Atomic rewrite. tmp in the same directory, fsync, rename — a reader
        sees the previous manifest or the new one, never a half-written one."""
        os.makedirs(self.dir, exist_ok=True)
        tmp = self.manifest_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self._payload(), f, indent=1)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.manifest_path)

    def _oops(self, where, exc):
        msg = f"{where}: {type(exc).__name__}: {str(exc)[:160]}"
        self.errors.append(msg)
        print(f"⚠️ browse_capture {msg}", flush=True)

    def _prune_once(self):
        if self._pruned:
            return
        self._pruned = True
        try:
            dirs = sorted(d for d in glob.glob(os.path.join(CAPTURE_ROOT, "*"))
                          if os.path.isdir(d))
            for old in dirs[:-KEEP_RUNS]:
                if os.path.abspath(old) != os.path.abspath(self.dir):
                    shutil.rmtree(old, ignore_errors=True)
        except Exception as e:
            self._oops("prune", e)


# ─── Reading the record back (phase 7) ────────────────────────────────────────

def manifests():
    """Every run manifest on disk, newest last. Unreadable files are skipped,
    not raised — a torn manifest must not break the staleness check."""
    out = []
    for path in sorted(glob.glob(os.path.join(CAPTURE_ROOT, "*", "manifest.json"))):
        try:
            with open(path) as f:
                m = json.load(f)
            m["_path"] = path
            out.append(m)
        except Exception:
            continue
    return out


def heartbeat_age(manifest, now=None):
    """Seconds since this run's heartbeat last advanced, or None if unreadable."""
    try:
        beat = datetime.fromisoformat(manifest["heartbeat"])
        return (now or datetime.now()).timestamp() - beat.timestamp()
    except Exception:
        return None


def find_stalled(threshold=None, now=None, pid_alive=None):
    """Runs whose heartbeat has not advanced past `threshold` seconds.

    Only runs still claiming "running" qualify — a finalised manifest is a
    finished run no matter how old it is. A run whose recording process is
    demonstrably gone is stalled regardless, which is the whole point: the
    worker that dies is the one that sends nothing.
    """
    threshold = conf("BROWSE_STALL_AFTER_S") if threshold is None else threshold
    if pid_alive is None:
        def pid_alive(pid):
            try:
                os.kill(int(pid), 0)
                return True
            except (ProcessLookupError, ValueError, OverflowError, TypeError):
                return False
            except PermissionError:
                return True
    out = []
    for m in manifests():
        if m.get("status") != "running":
            continue
        age = heartbeat_age(m, now=now)
        if age is None:
            continue
        if age < threshold and pid_alive(m.get("pid")):
            continue
        out.append({
            "run_id":    m.get("run_id", "?"),
            "last_step": m.get("last_step", 0),
            "age_s":     int(age),
            "task":      m.get("task", ""),
            "route":     m.get("route", ""),
            "url":       (m.get("last") or {}).get("url", ""),
            "action":    (m.get("last") or {}).get("action", ""),
            "captures":  len(m.get("captures") or []),
            "manifest":  m.get("_path"),
            "dead_pid":  not pid_alive(m.get("pid")),
        })
    return out


def mark_stalled(manifest_path, note):
    """Close out a stalled run's manifest so the next sweep does not re-report
    it. Idempotent by construction: status stops being "running"."""
    try:
        with open(manifest_path) as f:
            m = json.load(f)
        if m.get("status") != "running":
            return False
        m["status"] = "stalled"
        m["reason"] = str(note)[:600]
        m.setdefault("events", []).append(
            f"{datetime.now().isoformat(timespec='seconds')} {str(note)[:400]}")
        tmp = manifest_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(m, f, indent=1)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, manifest_path)
        return True
    except Exception as e:
        print(f"⚠️ browse_capture mark_stalled: {e}", flush=True)
        return False


if __name__ == "__main__":
    stalled = find_stalled()
    print(f"capture root : {CAPTURE_ROOT}")
    print(f"manifests    : {len(manifests())}")
    print(f"stall after  : {conf('BROWSE_STALL_AFTER_S')}s")
    for s in stalled:
        print(f"  STALLED {s['run_id']} step {s['last_step']} "
              f"({s['age_s']}s since heartbeat)")
    if not stalled:
        print("  no stalled runs")
