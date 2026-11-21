#!/usr/bin/env python3
"""
home_route.py — scoped, self-expiring home-route lease for the browse pipeline
===============================================================================
Built 20 Nov 2026. Lets a single browse_task(route="home") exit through the
residential connection WITHOUT taking the rest of the Pi off Mullvad.

Why this exists
---------------
Large retailers (Best Buy especially) fingerprint and soft-block known
datacenter/VPN exit ranges. The only prior way to reach them was
`mullvad: off` — a whole-box, all-or-nothing off-window that took every
process on the Pi off the VPN. This is the scoped replacement.

How the scoping works (implementation path (a): real split tunnel)
------------------------------------------------------------------
Tailscale's own policy routing does the heavy lifting. It installs:

    5210: from all fwmark 0x80000/0xff0000 lookup main   <- bypass
    5270: from all                        lookup 52      <- default dev tailscale0

Everything goes to table 52 (the tunnel) except packets carrying Tailscale's
bypass mark 0x80000, which go to the main table — i.e. out the LAN gateway and
through the home ISP. So a per-process split tunnel is just: mark that one
process's packets.

/etc/chip/homeroute-on.nft marks exactly the chip-browser.service cgroup
(`socket cgroupv2 level 2 "system.slice/chip-browser.service"`) and nothing
else. Tailnet/LAN/loopback destinations are excluded from the mark so MagicDNS
(100.100.100.100) and the noVNC live view keep working over tailscale0.

One wrinkle, handled: marking in the output hook reroutes the packet but does
NOT change its source address — connect() already picked the tailscale0
address, and ip_route_me_harder keeps it because it is still a valid local IP.
A 100.x source on the LAN is unanswerable, so the ruleset also masquerades the
marked flows to the outgoing LAN address. (Verified: without the masquerade the
connection just times out.)

Fail-safe design
----------------
The safe state is the ABSENCE of state. Full-VPN is restored by DELETING the
nft tables, so restoration can never half-apply, and a reboot restores it for
free (nftables.service is disabled; nothing persists these tables).

Three independent layers put it back, in order of how much has to be working:

  1. happy path   — browse.py releases in a finally:
  2. FAIL-SAFE    — chip-homeroute-expire.timer, a systemd timer armed at
                    acquire with a hard OnActiveSec=900. Fires even if the
                    coordinator is SIGKILLed, so a crashed or killed browse
                    session cannot leave the box on the home route. This is
                    the guarantee, not the cleanup handler.
  3. reconciler   — vpn_guard.enforce_home_route() on its 5-min cron catches
                    an orphaned rule whose lease is gone or expired.

Privilege
---------
This module runs as the service user and holds no privileged logic itself. It shells out
to seven fixed, wildcard-free sudo commands (/etc/sudoers.d/chip-homeroute):
`nft -f <fixed root-owned file>`, three fixed `nft` table ops, and
start/stop/query of one fixed timer unit. Nothing here can exec arbitrary code
as root, and the task string is only ever written to a log — never to a shell.

CLI: home_route.py acquire --user U --task T [--ttl N] | release [--reason R]
     | status | enforce
"""
import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta

import os as _os, sys as _sys
_cc = _os.path.dirname(_os.path.abspath(__file__))
while _cc != "/" and not _os.path.exists(_os.path.join(_cc, "chip_config.py")):
    _cc = _os.path.dirname(_cc)
if _cc not in _sys.path:
    _sys.path.insert(0, _cc)
from chip_config import WORKSPACE

RUN_DIR      = "/run/chip"
LEASE        = f"{RUN_DIR}/home_route_lease"
ROTATE_PAUSE = f"{RUN_DIR}/rotate_paused"
LOG          = f"{WORKSPACE}/data/logs/home_route.log"
NFT_RULES    = "/etc/chip/homeroute-on.nft"
TIMER        = "chip-homeroute-expire.timer"

# Hard ceiling. Must never exceed browse.py TIME_CAP (900) — the spec caps the
# exposure window at the longest a browse session can possibly run.
HARD_CAP = 900
MIN_TTL  = 60


def audit(event, **fields):
    """Every home-route session is logged: these runs expose the residential
    IP, so there has to be a trail. Tab-separated, append-only."""
    parts = [datetime.now().isoformat(timespec="seconds"), event]
    parts += [f"{k}={v}" for k, v in fields.items()]
    line = "\t".join(str(p).replace("\t", " ").replace("\n", " ") for p in parts)
    print(line)
    try:
        os.makedirs(os.path.dirname(LOG), exist_ok=True)
        with open(LOG, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


def _sudo(args, check=False):
    try:
        p = subprocess.run(["sudo", "-n"] + args, capture_output=True,
                           text=True, timeout=30)
        if check and p.returncode != 0:
            raise RuntimeError(f"{' '.join(args)}: {(p.stderr or '').strip()[:200]}")
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired:
        if check:
            raise RuntimeError(f"{' '.join(args)}: timed out")
        return 1, "timeout"


def rule_active():
    """Ground truth, straight from the kernel — not from our own lease file."""
    rc, _ = _sudo(["/usr/sbin/nft", "list", "table", "inet", "chip_homeroute"])
    return rc == 0


def read_lease():
    try:
        with open(LEASE) as f:
            return json.load(f)
    except Exception:
        return None


def lease_expired(lease):
    if not lease:
        return True
    try:
        return datetime.now() >= datetime.fromisoformat(lease["expires"])
    except Exception:
        return True


def acquire(user, task, ttl=HARD_CAP):
    """Install the scoped home route and arm its fail-safe. Returns the lease."""
    try:
        ttl = int(ttl)
    except (TypeError, ValueError):
        ttl = HARD_CAP
    ttl = max(MIN_TTL, min(ttl, HARD_CAP))

    os.makedirs(RUN_DIR, exist_ok=True)

    # Arm the fail-safe FIRST. If the ruleset then fails to apply, the timer
    # fires later and deletes a table that was never created — harmless. The
    # reverse order would leave a window where the route is up and unbounded.
    _sudo(["/usr/bin/systemctl", "restart", TIMER], check=True)
    try:
        _sudo(["/usr/sbin/nft", "-f", NFT_RULES], check=True)
    except Exception:
        release("apply-failed")
        raise

    now     = datetime.now()
    expires = now + timedelta(seconds=ttl)
    lease = {"started": now.isoformat(timespec="seconds"),
             "expires": expires.isoformat(timespec="seconds"),
             "ttl": ttl, "user": user, "task": str(task)[:300],
             "pid": os.getpid()}
    with open(LEASE, "w") as f:
        json.dump(lease, f)

    # Pause Mullvad rotation: re-pointing the exit node mid-session churns the
    # routing table underneath a live browser. Flag carries its own expiry so a
    # pause can never become permanent (same contract as mullvad_off).
    with open(ROTATE_PAUSE, "w") as f:
        f.write(expires.isoformat())

    audit("ACQUIRE", route="home", user=user, ttl=ttl,
          expires=lease["expires"], task=lease["task"])
    return lease


def release(reason="session-end"):
    """Restore the full-VPN default. Safe to call when nothing is held."""
    lease = read_lease()
    _sudo(["/usr/bin/systemctl", "stop", TIMER])
    _sudo(["/usr/sbin/nft", "delete", "table", "inet", "chip_homeroute"])
    _sudo(["/usr/sbin/nft", "delete", "table", "ip", "chip_homeroute_nat"])
    for path in (LEASE, ROTATE_PAUSE):
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
        except Exception:
            pass

    still_up = rule_active()
    if lease:
        try:
            dur = int((datetime.now() -
                       datetime.fromisoformat(lease["started"])).total_seconds())
        except Exception:
            dur = -1
        audit("RELEASE", route="home", user=lease.get("user", "?"),
              duration_s=dur, reason=reason,
              restored=("NO" if still_up else "yes"),
              task=lease.get("task", ""))
    elif still_up:
        audit("RELEASE", route="home", reason=reason, restored="NO",
              note="no lease on record")

    if still_up:
        audit("ALERT", note="chip_homeroute table SURVIVED release — box may "
                            "still be on the home route")
    return not still_up


def holder_alive(lease):
    """Is the process that took the lease still running? A SIGKILLed browse
    session leaves a lease that has not yet expired, so expiry alone would hold
    the home route open for the full 900s. (PID reuse could in principle mask a
    dead holder; the hard timer is the backstop for that.)"""
    pid = (lease or {}).get("pid")
    if not pid:
        return True          # unknown holder — fall back to expiry only
    try:
        os.kill(int(pid), 0)
        return True
    except (ProcessLookupError, ValueError):
        return False
    except PermissionError:
        return True          # alive, just not ours to signal


def enforce():
    """Reconciler for vpn_guard's 5-min cron. Compares kernel truth against the
    lease and force-releases anything orphaned, expired or abandoned."""
    active, lease = rule_active(), read_lease()
    if active and (lease_expired(lease) or not holder_alive(lease)):
        if not lease:
            why = "orphaned-no-lease"
        elif lease_expired(lease):
            why = "expired"
        else:
            why = "holder-died"
        audit("ENFORCE", action="force-release", reason=why)
        release(f"enforce-{why}")
        return f"force-released ({why})"
    if not active and lease:
        # Rule gone (timer fired, or a reboot) but the lease outlived it.
        try:
            os.remove(LEASE)
        except Exception:
            pass
        try:
            os.remove(ROTATE_PAUSE)
        except Exception:
            pass
        audit("ENFORCE", action="cleared-stale-lease")
        return "cleared stale lease"
    return "ok"


def status():
    lease = read_lease()
    rc, timer = _sudo(["/usr/bin/systemctl", "is-active", TIMER])
    remaining = None
    if lease and not lease_expired(lease):
        remaining = int((datetime.fromisoformat(lease["expires"])
                         - datetime.now()).total_seconds())
    return {"rule_active": rule_active(),
            "route": "home" if rule_active() else "vpn (full tunnel)",
            "lease": lease,
            "expired": lease_expired(lease) if lease else None,
            "remaining_s": remaining,
            "failsafe_timer": timer.strip(),
            "scope": "system.slice/chip-browser.service"}


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("acquire")
    a.add_argument("--user", default="?")
    a.add_argument("--task", default="")
    a.add_argument("--ttl", type=int, default=HARD_CAP)
    r = sub.add_parser("release")
    r.add_argument("--reason", default="manual")
    sub.add_parser("status")
    sub.add_parser("enforce")
    args = ap.parse_args()

    if args.cmd == "acquire":
        print(json.dumps(acquire(args.user, args.task, args.ttl), indent=2))
    elif args.cmd == "release":
        print("restored" if release(args.reason) else "STILL ACTIVE")
    elif args.cmd == "status":
        print(json.dumps(status(), indent=2))
    elif args.cmd == "enforce":
        print(enforce())
    return 0


if __name__ == "__main__":
    sys.exit(main())
