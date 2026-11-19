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
ALERT_AT  = 3                                        # consecutive failures before telling the admin
MAX_CATCHUP = timedelta(hours=S.MAX_CATCHUP_HOURS)


def log(msg):
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


def alert(message):
    """Tell the admin directly over the signal-cli socket. Independent of the
    coordinator, which may be the thing that is broken."""
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
    lock = open(f"{LOCK_DIR}/{name}.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        log(f"{name}: previous run still going - skipped")
        _record(name, exit_code=-1, note="overrun")
        return 1

    env = dict(os.environ)
    env.update({"CHIP_WORKSPACE": WORKSPACE, "HOME": os.path.expanduser("~")})
    env.setdefault("PATH", "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin")
    env.update(job["env"])
    os.makedirs(os.path.dirname(job["log"]), exist_ok=True)
    started = datetime.now()
    try:
        with open(job["log"], "a") as lf:
            proc = subprocess.Popen(job["command"], shell=True, cwd=WORKSPACE, env=env,
                                    stdout=lf, stderr=subprocess.STDOUT,
                                    start_new_session=True)
            try:
                code, note = proc.wait(timeout=job["timeout_seconds"]), ""
            except subprocess.TimeoutExpired:
                import signal as _sig                # take the shell's children down too
                try:
                    os.killpg(proc.pid, _sig.SIGTERM); time.sleep(3); os.killpg(proc.pid, _sig.SIGKILL)
                except ProcessLookupError:
                    pass
                proc.wait()
                raise
    except subprocess.TimeoutExpired:
        code, note = -2, f"timed out after {job['timeout_seconds']}s"
    except Exception as e:
        code, note = -3, str(e)[:200]
    secs = (datetime.now() - started).total_seconds()
    log(f"{name}: exit {code} in {secs:.0f}s {note}")
    _record(name, exit_code=code, note=note)
    return 0 if code == 0 else 1


def _record(name, exit_code, note=""):
    now = datetime.now().isoformat()
    hit = {}

    def _m(d):
        s = d.setdefault(name, {})
        s["last_run"], s["last_exit_code"], s["last_note"] = now, exit_code, note
        if exit_code == 0:
            s["last_success"], s["consecutive_failures"] = now, 0
        else:
            s["consecutive_failures"] = s.get("consecutive_failures", 0) + 1
        hit["fails"] = s["consecutive_failures"]
    S._json_rw(S.STATE_FILE, _m, default={})
    if hit.get("fails") == ALERT_AT:                 # once, at the threshold - not every tick
        alert(f"[Chip] Scheduled job '{name}' has failed {ALERT_AT} times in a row "
              f"(last exit {exit_code}{', ' + note if note else ''}). "
              f"Its log is data/logs/. Ask me to list scheduled jobs for details.")


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
