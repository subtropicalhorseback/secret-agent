"""
skills/safe_restart.py — restart Chip without killing what it is doing
======================================================================
Added 20 Nov 2026 (One Voice spec, B.4).

Workers (browser runs, research) live inside the coordinator process, so a
restart kills them mid-flight. Every heal restarts Chip. This waits until the
work ledger shows nothing running for the live process (up to MAX_WAIT),
then restarts chip.service and confirms it is active.

    venv/bin/python3 skills/safe_restart.py [--max-wait SECONDS] [--now]

Exit 0 when chip is active after the restart, 1 otherwise. --now skips the
wait (for an emergency; anything running is marked lost at startup and the
user is told).
"""
import argparse
import os
import subprocess
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from skills import ledger  # noqa: E402

MAX_WAIT = 600


def chip_pid():
    try:
        out = subprocess.check_output(["systemctl", "show", "-p", "MainPID", "--value", "chip"],
                                      text=True, timeout=10).strip()
        return int(out) if out and out != "0" else None
    except Exception:
        return None


def busy():
    pid = chip_pid()
    rows = [r for r in ledger.running() if pid is None or r.get("pid") == pid]
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-wait", type=int, default=MAX_WAIT)
    ap.add_argument("--now", action="store_true")
    a = ap.parse_args()
    waited = 0
    if not a.now:
        while waited < a.max_wait:
            rows = busy()
            if not rows:
                break
            if waited % 60 == 0:
                print(f"waiting for {len(rows)} running worker(s): "
                      + ", ".join(f"{r['id']} {r['kind']}" for r in rows), flush=True)
            time.sleep(10)
            waited += 10
        rows = busy()
        if rows:
            print(f"still {len(rows)} running after {waited}s — restarting anyway; "
                  f"they will be marked lost and the user told", flush=True)
    r = subprocess.run(["sudo", "systemctl", "restart", "chip.service"], timeout=60)
    if r.returncode != 0:
        print("restart command failed")
        return 1
    for _ in range(15):
        time.sleep(2)
        state = subprocess.run(["systemctl", "is-active", "chip"], capture_output=True,
                               text=True).stdout.strip()
        if state == "active":
            print(f"chip active (waited {waited}s for running work)")
            return 0
    print(f"chip is {state} after restart")
    return 1


if __name__ == "__main__":
    sys.exit(main())
