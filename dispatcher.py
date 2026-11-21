#!/usr/bin/env python3
"""
dispatcher.py — runs the system job registry (config/jobs.yaml)
================================================================
Added 19 Nov 2026. One crontab line, every minute:

    * * * * * <venv>/bin/python3 <workspace>/dispatcher.py >> data/logs/dispatcher.log 2>&1

Why it exists: the old crontab accumulated jobs nobody could see. One of them
fired every five minutes for four months after it stopped mattering, and its
output went to a log nobody read. Here every job is declared in one file, its
last run and failures are recorded, and a job that keeps failing tells the
admin instead of failing quietly.

How a tick works
  - Load the registry (skills.scheduler.load_registry: overrides applied,
    protected jobs forced on, bad files rejected).
  - For each enabled dispatcher-run job, ask the existing cron parser whether
    a fire time has passed since the job's cursor. The cursor moves BEFORE
    the job starts, under the state lock, so a crash cannot double-fire.
  - Each due job starts as its own detached process (`--run NAME`), so a
    long job never blocks the tick or the other jobs.
  - A per-job lock means a job still running from last time is skipped and
    counted as a failure, never stacked.
  - A job missed by more than MAX_CATCHUP_HOURS is skipped, so a box that was
    off does not replay a day of jobs at boot.

signal_watchdog and vpn_guard never run from here. They stay in the crontab
on purpose: they are what notices, and recovers, if this file breaks.
"""
import fcntl
import json
import os
import socket
import subprocess
import sys
import time
import traceback
from datetime import datetime, timedelta

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
import chip_config                                   # noqa: E402
from skills import scheduler as S                    # noqa: E402

WORKSPACE = chip_config.WORKSPACE
LOCK_DIR  = f"{WORKSPACE}/data/job_locks"
ALERT_AT  = 3                                        # default consecutive failures before telling the admin
MAX_CATCHUP = timedelta(hours=S.MAX_CATCHUP_HOURS)
TAIL_LINES  = 50                                     # how much of a failing run is worth keeping
TAIL_CHARS  = 4000                                   # hard cap: a chatty job must not fill job_state.json

# Outcomes recorded in run state. A timeout is NOT the same event as a job
# that chose to exit non-zero, and neither is an overrun skip — they want
# different fixes, so they are named separately rather than all being "exit 1".
OUT_OK, OUT_FAILED, OUT_TIMEOUT, OUT_OVERRUN, OUT_ERROR = (
    "ok", "failed", "timeout", "overrun", "error")


def log(msg):
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


def alert(message):
    """Urgent: through Chip's outbox (One Voice spec A.3), which sends it
    directly when Chip is not consuming — so it still arrives if the
    coordinator is the thing that is broken."""
    try:
        from skills import outbox
        r = outbox.notify("job_failure", message.removeprefix("[Chip] "),
                          urgent=True, source="dispatcher")
        if r != "failed":
            return True
    except Exception as e:
        log(f"outbox failed ({e}) — sending directly")
    return _alert_direct(message)


def _alert_direct(message):
    """Last resort: straight to the signal-cli socket."""
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(30)
        s.connect(os.getenv("SIGNAL_SOCKET", "/run/signal-cli/signal.sock"))
        s.sendall((json.dumps({"jsonrpc": "2.0", "method": "send", "id": 1,
                               "params": {"recipient": [chip_config.ADMIN_PHONE],
                                          "message": message}}) + "\n").encode())
        buf = b""
        while b"\n" not in buf:
            chunk = s.recv(4096)
            if not chunk:
                break
            buf += chunk
        s.close()
    except Exception as e:
        log(f"alert failed: {e}")


def update_state(name, **fields):
    def _m(d):
        d.setdefault(name, {}).update(fields)
    S._json_rw(S.STATE_FILE, _m, default={})


# ─── one job ──────────────────────────────────────────────────────────────────

def run_one(name):
    job = next((j for j in S.load_registry() if j["name"] == name), None)
    if job is None or job["runner"] != "dispatcher":
        log(f"{name}: not a dispatcher job")
        return 2
    os.makedirs(LOCK_DIR, exist_ok=True)
    # flock, not a PID file: the kernel drops the lock when the holder dies, so
    # a dispatcher killed mid-job leaves nothing stale to clean up by hand.
    lock = open(f"{LOCK_DIR}/{name}.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        log(f"{name}: previous run still going - skipped")
        _record(name, exit_code=-1, outcome=OUT_OVERRUN,
                note="still running when the next slot came round - skipped, not queued")
        return 1

    env = dict(os.environ)
    env.update({"CHIP_WORKSPACE": WORKSPACE, "HOME": os.path.expanduser("~")})
    env.setdefault("PATH", "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin")
    env.update(job["env"])
    os.makedirs(os.path.dirname(job["log"]), exist_ok=True)

    # Where the job's log ends before we start. Everything appended past this
    # point is this run's output — which catches BOTH what we capture off the
    # pipe AND what the job writes to the same log itself. backup.sh sends tar's
    # stderr straight to the log with `2>> "$LOG"`, so a pipe-only capture saw
    # nothing and recorded a bare "exit 1" for six nights running.
    try:
        offset = os.path.getsize(job["log"])
    except OSError:
        offset = 0

    started = datetime.now()
    try:
        with open(job["log"], "a") as lf:
            proc = subprocess.Popen(job["command"], shell=True, cwd=WORKSPACE, env=env,
                                    stdout=lf, stderr=subprocess.STDOUT,
                                    start_new_session=True)
            try:
                code, outcome, note = proc.wait(timeout=job["timeout_seconds"]), None, ""
                outcome = OUT_OK if code == 0 else OUT_FAILED
            except subprocess.TimeoutExpired:
                import signal as _sig                # take the shell's children down too
                try:
                    os.killpg(proc.pid, _sig.SIGTERM); time.sleep(3); os.killpg(proc.pid, _sig.SIGKILL)
                except ProcessLookupError:
                    pass
                proc.wait()
                raise
    except subprocess.TimeoutExpired:
        code, outcome, note = -2, OUT_TIMEOUT, f"hard killed after {job['timeout_seconds']}s"
    except Exception as e:
        code, outcome, note = -3, OUT_ERROR, str(e)[:200]

    finished = datetime.now()
    secs = (finished - started).total_seconds()
    log(f"{name}: {outcome} (exit {code}) in {secs:.0f}s {note}")
    _record(name, exit_code=code, outcome=outcome, note=note,
            started=started, finished=finished, duration=secs,
            tail=_tail_since(job["log"], offset))
    return 0 if code == 0 else 1


def _tail_since(path, offset):
    """The last TAIL_LINES of whatever this run appended to its log. Capped on
    both ends: we seek to the run's own start so an old log can't be mistaken
    for fresh output, and we keep only the tail so a chatty job can't bloat
    state. TAIL_CHARS is the backstop for a job that writes few but enormous
    lines, so one run can never add more than ~4KB to job_state.json."""
    try:
        size = os.path.getsize(path)
        if size <= offset:
            return ""
        with open(path, "r", errors="replace") as fh:
            fh.seek(max(offset, size - TAIL_CHARS))
            lines = fh.read(TAIL_CHARS).splitlines()
        return "\n".join(lines[-TAIL_LINES:]).strip()
    except Exception:
        return ""


def _record(name, exit_code, outcome=None, note="", started=None,
            finished=None, duration=None, tail=""):
    now = datetime.now().isoformat()
    if outcome is None:
        outcome = OUT_OK if exit_code == 0 else OUT_FAILED
    try:
        limit = next(j["max_failures"] for j in S.load_registry() if j["name"] == name)
    except Exception:
        limit = ALERT_AT
    hit = {}

    def _m(d):
        s = d.setdefault(name, {})
        s["last_run"] = s["last_finish"] = finished.isoformat() if finished else now
        if started:
            s["last_start"] = started.isoformat()
        if duration is not None:
            s["duration_seconds"] = round(duration, 1)
        s["last_exit_code"], s["last_outcome"], s["last_note"] = exit_code, outcome, note
        s["last_output_tail"] = tail
        if exit_code == 0:
            s["last_success"], s["consecutive_failures"] = now, 0
            s["alerted"] = False                     # recovered: re-arm the alert
        else:
            s["consecutive_failures"] = s.get("consecutive_failures", 0) + 1
        # Decide to alert INSIDE the locked write, so two jobs finishing in the
        # same second cannot both read "not yet alerted" and both page.
        hit["fire"] = (exit_code != 0
                       and s["consecutive_failures"] >= limit
                       and not s.get("alerted"))
        if hit["fire"]:
            s["alerted"] = True                      # silent until it succeeds again
            hit["fails"] = s["consecutive_failures"]
    S._json_rw(S.STATE_FILE, _m, default={})
    if hit.get("fire"):
        snippet = f"\n\nLast output:\n{tail[-400:]}" if tail else ""
        alert(f"[Chip] Scheduled job '{name}' has failed {hit['fails']} times in a row "
              f"({outcome}, exit {exit_code}{', ' + note if note else ''}). "
              f"No further alerts for this job until it succeeds. "
              f"Ask me to list scheduled jobs for details.{snippet}")


# ─── the tick ─────────────────────────────────────────────────────────────────

def tick(only=None, dry_run=False):
    os.makedirs(LOCK_DIR, exist_ok=True)
    tick_lock = open(f"{LOCK_DIR}/_dispatcher.lock", "w")
    try:
        fcntl.flock(tick_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return                                       # previous tick still deciding
    now = datetime.now()
    due = []

    def _decide(state):
        for job in S.load_registry():
            name = job["name"]
            if job["runner"] != "dispatcher" or (only and name not in only):
                continue
            s = state.setdefault(name, {})
            s.setdefault("first_seen", now.isoformat())
            if not job["enabled"]:
                s["cursor"] = now.isoformat()        # so re-enabling never replays the gap
                continue
            if "cursor" not in s:
                s["cursor"] = now.isoformat()        # first sight: start the clock, do not fire
                continue
            fire = S.next_fire(job["schedule"], datetime.fromisoformat(s["cursor"]))
            if fire is None or fire > now:
                continue
            if not dry_run:
                s["cursor"] = now.isoformat()        # advance BEFORE running
            if now - fire > MAX_CATCHUP:
                log(f"{name}: missed {fire:%m-%d %H:%M} by more than "
                    f"{S.MAX_CATCHUP_HOURS}h - skipped")
                continue
            due.append(name)

    if dry_run:
        _decide(S._json_rw(S.STATE_FILE, default={}))
        log(f"dry run: due now = {due or 'nothing'}")
        return
    S._json_rw(S.STATE_FILE, _decide, default={})
    for name in due:
        subprocess.Popen([sys.executable, os.path.abspath(__file__), "--run", name],
                         start_new_session=True, cwd=WORKSPACE,
                         stdout=open(f"{WORKSPACE}/data/logs/dispatcher.log", "a"),
                         stderr=subprocess.STDOUT)
        log(f"started {name}")


if __name__ == "__main__":
    try:
        if "--run" in sys.argv:
            sys.exit(run_one(sys.argv[sys.argv.index("--run") + 1]))
        only = None
        if "--only" in sys.argv:
            only = set(sys.argv[sys.argv.index("--only") + 1].split(","))
        tick(only=only, dry_run="--dry-run" in sys.argv)
    except Exception:
        log("DISPATCHER ERROR\n" + traceback.format_exc())
        # At most one alert an hour: a broken registry would otherwise page every minute.
        stamp = f"{LOCK_DIR}/_last_error_alert"
        try:
            fresh = time.time() - os.path.getmtime(stamp) < 3600
        except OSError:
            fresh = False
        if not fresh:
            os.makedirs(LOCK_DIR, exist_ok=True)
            open(stamp, "w").write(datetime.now().isoformat())
            alert("[Chip] The job dispatcher hit an error and scheduled jobs may not be "
                  "running. Details are in data/logs/dispatcher.log.")
        sys.exit(1)
