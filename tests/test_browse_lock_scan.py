#!/usr/bin/env python3
"""Regression drill for the profile-lock scanner (FIX-0003, 2026-11-20).

Run:  venv/bin/python3 tests/test_browse_lock_scan.py

What went wrong: Chromium's SingletonCookie symlink points at a large RANDOM
number, not a pid. The scanner int()'d it and handed it to os.kill(), which
raised `OverflowError: signed integer is greater than maximum` (pid_t is a C
int) on every system_health call. OverflowError is not a ValueError, so it
escaped alive()'s except clause, aborted the whole scan — and system_health
still printed "orphans: none". A false all-clear that was believed three
times in eight minutes.

These drills pin both halves: the scanner returns stale-unknown instead of
raising, and the health line never says "none" for a scan that did not run.

Nothing here touches the real profile directory, the real /proc, or systemd.
"""
import os
import shutil
import sys
import tempfile
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from skills import browse_obs   # noqa: E402

failures = []


def check(label, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {label}" + (f"  — {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(label)


def make_profile(root, name, links):
    """links: {lock name: symlink target}. A target of None makes a plain file
    (SingletonSocket on disk is a socket, not a symlink — readlink fails)."""
    prof = os.path.join(root, name)
    os.makedirs(prof, exist_ok=True)
    for lock, target in links.items():
        path = os.path.join(prof, lock)
        if target is None:
            with open(path, "w") as f:
                f.write("")
        else:
            os.symlink(target, path)
    return prof


def scan_with(links_by_profile):
    """Point the scanner at a throwaway profile tree and scan it."""
    root = tempfile.mkdtemp(prefix="chip-locktest-")
    saved = browse_obs.PROFILE_DIR
    before = list(browse_obs.OBS_ERRORS)
    try:
        for name, links in links_by_profile.items():
            make_profile(root, name, links)
        browse_obs.PROFILE_DIR = root
        res = browse_obs.scan_profile_locks()
        res["_root"] = root
        res["_new_errors"] = browse_obs.OBS_ERRORS[len(before):]
        return res
    finally:
        browse_obs.PROFILE_DIR = saved
        shutil.rmtree(root, ignore_errors=True)


def names(paths):
    return sorted(os.path.basename(p) for p in paths)


# ─── Drill 1: the exact 2026-11-20 crash ──────────────────────────────────────

def drill_1_oversized_pid():
    print("\ndrill 1 — oversized pid in the lock value (the logged OverflowError)")
    # 3000000000 > INT_MAX: this is the literal value shape that crashed it.
    try:
        res = scan_with({"mullvad": {"SingletonLock": "chip-3000000000",
                                     "SingletonCookie": "17029384756102938475"}})
        raised = None
    except Exception as e:                                   # noqa: BLE001
        res, raised = None, f"{type(e).__name__}: {e}"
    check("the scanner returned instead of raising", raised is None, raised)
    if not res:
        return
    check("scan reports ok (it completed)", res["ok"] is True, res)
    check("both locks are stale-unknown, not asserted clean",
          names(res["unknown"]) == ["SingletonCookie", "SingletonLock"], res)
    check("nothing was classified live or stale",
          not res["live"] and not res["stale"], res)
    check("no OverflowError was logged to OBS_ERRORS",
          not any("OverflowError" in e for e in res["_new_errors"]),
          res["_new_errors"])
    check("stale_profile_locks() offers them to the reaper",
          len(res["unknown"]) == 2, res)


# ─── Drill 2: every other malformed shape ─────────────────────────────────────

def drill_2_malformed_values():
    print("\ndrill 2 — malformed lock values of every shape")
    cases = {
        "huge-beyond-c-long": "chip-99999999999999999999999999",
        "not-a-number":       "chip-notapid",
        "zero":               "chip-0",        # os.kill(0) signals our own group
        "empty":              "chip-",
        "no-separator":       "chip",
        "float":              "chip-1.5",
        "hex":                "chip-0x1f",
    }
    # A negative value cannot reach here through a lock target — rsplit("-", 1)
    # never returns a string containing "-", so "chip--1" parses as pid 1. The
    # guard against os.kill(-N) signalling a whole process GROUP therefore has
    # to be tested at valid_pid()/alive(), which is what follows.
    for label, target in cases.items():
        try:
            res = scan_with({label: {"SingletonLock": target}})
            raised = None
        except Exception as e:                               # noqa: BLE001
            res, raised = None, f"{type(e).__name__}: {e}"
        check(f"{label!r} did not raise", raised is None, raised)
        if res:
            check(f"{label!r} → stale-unknown",
                  names(res["unknown"]) == ["SingletonLock"]
                  and not res["live"] and not res["stale"], res)

    check("valid_pid rejects out-of-range and junk",
          all(browse_obs.valid_pid(v) is None
              for v in (3000000000, -1, 0, "", "abc", None, 10**30)),
          [browse_obs.valid_pid(v) for v in (3000000000, -1, 0, "", "abc", None, 10**30)])
    check("valid_pid accepts a real pid",
          browse_obs.valid_pid(str(os.getpid())) == os.getpid())
    for junk in (3000000000, -1, 0, "abc", None, 10**30):
        try:
            got, raised = browse_obs.alive(junk), None
        except Exception as e:                               # noqa: BLE001
            got, raised = None, f"{type(e).__name__}: {e}"
        check(f"alive({junk!r}) returns False, never raises",
              raised is None and got is False, raised or got)


# ─── Drill 3: the scanner still has to be right about real locks ──────────────

def drill_3_live_and_dead():
    print("\ndrill 3 — real owners are still classified correctly")
    live = scan_with({"mullvad": {"SingletonLock": f"chip-{os.getpid()}",
                                  "SingletonCookie": "17029384756102938475",
                                  "SingletonSocket": None}})
    check("a live owner marks the whole profile live",
          names(live["live"]) == ["SingletonCookie", "SingletonLock",
                                  "SingletonSocket"], live)
    check("a live session's locks are NOT offered to the reaper",
          not live["stale"] and not live["unknown"], live)

    dead_pid = browse_obs._pid_max() - 1          # in range, certainly not running
    while browse_obs.alive(dead_pid):
        dead_pid -= 1
    dead = scan_with({"direct": {"SingletonLock": f"chip-{dead_pid}",
                                 "SingletonCookie": "99887766554433"}})
    check("a dead owner marks the profile stale",
          names(dead["stale"]) == ["SingletonCookie", "SingletonLock"], dead)
    check("empty profile tree scans clean",
          scan_with({"empty": {}})["ok"] is True)


# ─── Drill 4: system_health must not spell a failed scan "none" ───────────────

def _stub_health_env():
    """health_block() with systemd, /proc, the route and the heartbeat stubbed,
    so only the orphan line is under test."""
    browse_obs._unit = lambda n: {"ActiveState": "active", "SubState": "running",
                                  "MainPID": "4242", "uptime_s": 10}
    browse_obs.browser_procs = lambda: []
    browse_obs.read_heartbeat = lambda: None
    fake_route = types.ModuleType("home_route")
    fake_route.status = lambda: {"rule_active": False, "lease": None}
    sys.modules["home_route"] = fake_route


def drill_4_health_line():
    print("\ndrill 4 — the three orphan states in system_health")
    saved = {k: getattr(browse_obs, k) for k in
             ("_unit", "browser_procs", "read_heartbeat",
              "scan_strays", "scan_profile_locks")}
    try:
        _stub_health_env()

        browse_obs.scan_strays = lambda: {"ok": True, "error": None, "strays": []}
        browse_obs.scan_profile_locks = lambda: {"ok": True, "error": None,
                                                 "stale": [], "unknown": [], "live": []}
        clean = browse_obs.health_block()
        check("clean scan says 'orphans: none (scanned OK'",
              "orphans: none (scanned OK" in clean,
              [l for l in clean.splitlines() if "orphans" in l])

        browse_obs.scan_profile_locks = lambda: {
            "ok": False, "error": "OverflowError: signed integer is greater than maximum",
            "stale": [], "unknown": [], "live": []}
        broken = browse_obs.health_block()
        line = next((l for l in broken.splitlines() if "orphans:" in l), "")
        check("failed scan says 'orphans: UNKNOWN — scanner failed'",
              "orphans: UNKNOWN — scanner failed:" in line, line)
        check("failed scan names the error", "OverflowError" in line, line)
        check("failed scan NEVER says none", "orphans: none" not in broken,
              [l for l in broken.splitlines() if "orphans" in l])

        browse_obs.scan_strays = lambda: {"ok": True, "error": None,
                                          "strays": [{"pid": 11, "comm": "chrome",
                                                      "rss_mb": 9}]}
        browse_obs.scan_profile_locks = lambda: {"ok": True, "error": None,
                                                 "stale": ["/p/SingletonLock"],
                                                 "unknown": ["/p/SingletonCookie"],
                                                 "live": []}
        found = browse_obs.health_block()
        line = next((l for l in found.splitlines() if "orphans:" in l), "")
        check("findings say 'orphans: N found'", "orphans: 3 found" in line, line)
        check("findings still list the stray", "ORPHANS:" in found)
    finally:
        for k, v in saved.items():
            setattr(browse_obs, k, v)
        sys.modules.pop("home_route", None)


def main():
    print("browse profile-lock scanner drills (no real /proc, systemd or profiles)")
    drill_1_oversized_pid()
    drill_2_malformed_values()
    drill_3_live_and_dead()
    drill_4_health_line()
    print(f"\n{'FAILED: ' + ', '.join(failures) if failures else 'all checks passed'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
