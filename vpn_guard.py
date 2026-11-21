#!/usr/bin/env python3
"""
vpn_guard.py — Mullvad / Tailscale watchdog with an actual alert path
======================================================================
Written 19 Nov 2026 after Chip spent ~26 days (24 Oct - 19 Nov) off the
tailnet and therefore off Mullvad, routing every search, research:, analyze:,
archive: and model API call out the home IP. Nothing noticed, because:

  - the node key expired, tailscaled auto-regenerated one, and the new key
    needed an interactive login nobody was there to complete;
  - rotate_exit.py failed every 4 hours into its own logfile and nothing read it;
  - signal_watchdog deliberately filters Tailscale noise as a false positive;
  - overwatch read the failures as routine.

So this checks the three things that actually matter and pages the admin on Signal.

Checks (every 5 min via cron):
  1. tailscaled backend is Running (not NeedsLogin / Stopped)
  2. an exit node is actually selected
  3. am.i.mullvad.net agrees we are egressing through Mullvad

On failure: write /run/chip/vpn_down, attempt one rotate_exit.py self-heal,
alert the admin once, then re-alert hourly while still down.
On recovery: clear the flag and send an all-clear.
Deliberate offs (/run/chip/mullvad_off) are respected silently.

The flag file is what coordinator.py reads to fail closed on outbound web
requests (audit Phase 0 step 6).
"""
import json
import os
import socket
import subprocess
import sys
import time
from datetime import datetime

import os as _os, sys as _sys
_cc = _os.path.dirname(_os.path.abspath(__file__))
while _cc != "/" and not _os.path.exists(_os.path.join(_cc, "chip_config.py")):
    _cc = _os.path.dirname(_cc)
if _cc not in _sys.path:
    _sys.path.insert(0, _cc)
from chip_config import WORKSPACE
SIGNAL_SOCKET = os.getenv("SIGNAL_SOCKET", "/run/signal-cli/signal.sock")
import sys as _sys
_sys.path.insert(0, WORKSPACE)
from chip_config import ADMIN_PHONE
RUN_DIR       = "/run/chip"
DOWN_FLAG     = f"{RUN_DIR}/vpn_down"
OFF_FLAG      = f"{RUN_DIR}/mullvad_off"
LOG           = f"{WORKSPACE}/data/logs/vpn_guard.log"
REALERT_SECS  = 3600


def log(msg):
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    print(line)
    try:
        os.makedirs(os.path.dirname(LOG), exist_ok=True)
        with open(LOG, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


def _tell(text, urgent=False, kind="vpn"):
    """Through Chip's outbox (One Voice spec A.3): urgent events reach the
    admin as one message worded by Chip; routine ones go in the nightly
    summary. If Chip is down, the outbox sends urgent events directly."""
    try:
        sys.path.insert(0, WORKSPACE)
        from skills import outbox
        r = outbox.notify(kind, text, urgent=urgent, source="vpn_guard")
        log(f"outbox {kind} ({'urgent' if urgent else 'routine'}): {r}")
        return r != "failed"
    except Exception as e:
        log(f"outbox failed ({e}) — sending directly")
        return signal_send(f"[Chip] {text}") if urgent else False


def signal_send(message) -> bool:
    """Alert the admin over the signal-cli JSON-RPC socket (same path as
    signal_watchdog.py — deliberately not importing coordinator)."""
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(35)
        sock.connect(SIGNAL_SOCKET)
        sock.sendall((json.dumps({
            "jsonrpc": "2.0", "method": "send", "id": 1,
            "params": {"recipient": [ADMIN_PHONE], "message": message},
        }) + "\n").encode())
        buf = b""
        while b"\n" not in buf:
            chunk = sock.recv(4096)
            if not chunk:
                break
            buf += chunk
        sock.close()
        return b'"result"' in buf
    except Exception as e:
        log(f"Signal send failed: {e}")
        return False


def check_tailscale():
    """(backend_state, exit_node_hostname|None)."""
    try:
        raw = subprocess.check_output(
            ["tailscale", "status", "--json"], text=True, timeout=30)
        d = json.loads(raw)
        state = d.get("BackendState", "?")
        exit_node = next(
            (p.get("HostName") for p in (d.get("Peer") or {}).values() if p.get("ExitNode")),
            None)
        return state, exit_node
    except Exception as e:
        return f"error:{e}", None


def check_mullvad():
    """True if Mullvad confirms we are egressing through them."""
    try:
        out = subprocess.check_output(
            ["curl", "-s", "--max-time", "20", "https://am.i.mullvad.net/connected"],
            text=True, timeout=30)
        return "You are connected to Mullvad" in out, out.strip()
    except Exception as e:
        return False, f"check failed: {e}"


def try_self_heal():
    try:
        out = subprocess.run(
            [f"{WORKSPACE}/venv/bin/python3", f"{WORKSPACE}/skills/rotate_exit.py"],
            capture_output=True, text=True, timeout=120)
        log(f"self-heal rotate_exit: {(out.stdout or out.stderr).strip()[:200]}")
    except Exception as e:
        log(f"self-heal failed: {e}")


def flag_age():
    try:
        return time.time() - os.path.getmtime(DOWN_FLAG)
    except Exception:
        return None


def enforce_home_route():
    """Third and last line of defence over a scoped home-route lease
    (home_route.py). The browse session releases its own lease, and a systemd
    timer force-expires it at the 900s hard cap; this catches whatever both of
    those missed — an orphaned rule with no lease, or a lease that outlived its
    rule. Scoped home routing does NOT disturb the checks below: the box itself
    is still on Mullvad, so this function is the only part of the guard that
    needs to know about it."""
    try:
        sys.path.insert(0, WORKSPACE)
        import home_route
        verdict = home_route.enforce()
        if verdict != "ok":
            log(f"home-route enforce: {verdict}")
            _tell(f"Home-route lease {verdict} — full VPN restored.", urgent=False, kind="lease")
    except Exception as e:
        log(f"home-route enforce failed: {e}")


def main():
    os.makedirs(RUN_DIR, exist_ok=True)
    enforce_home_route()

    if os.path.exists(OFF_FLAG):
        # Deliberate off-window (mullvad: off [minutes]). The flag body is the
        # expiry timestamp; past it, we re-enable and tell the admin — a temporary
        # off can never silently become permanent (spec, 19 Nov 2026).
        try:
            expiry = open(OFF_FLAG).read().strip()
            if expiry and datetime.now().isoformat() > expiry:
                os.remove(OFF_FLAG)
                try_self_heal()
                log("mullvad_off expired — re-enabled")
                _tell("Mullvad off-window expired — VPN re-enabled.", urgent=False, kind="vpn_offwindow")
                return 0
        except Exception as e:
            log(f"off-flag parse: {e}")
        log("mullvad_off flag present — deliberate, staying quiet.")
        return 0

    state, exit_node = check_tailscale()
    mullvad_ok, detail = check_mullvad()
    healthy = (state == "Running") and bool(exit_node) and mullvad_ok

    if healthy:
        if os.path.exists(DOWN_FLAG):
            os.remove(DOWN_FLAG)
            log(f"RECOVERED — exit={exit_node} | {detail}")
            _tell(f"VPN all-clear. Mullvad restored via {exit_node}.", urgent=True, kind="vpn_up")
        else:
            log(f"ok — state={state} exit={exit_node}")
        return 0

    # ── unhealthy ──────────────────────────────────────────────────────────
    if state != "Running":
        reason = f"Tailscale backend is {state} (needs interactive login?)"
    elif not exit_node:
        reason = "Tailscale is up but NO exit node is selected — traffic is going out the home IP"
    else:
        reason = f"Exit node {exit_node} set but Mullvad check failed: {detail}"

    log(f"DOWN — {reason}")

    first_time = not os.path.exists(DOWN_FLAG)
    if first_time:
        with open(DOWN_FLAG, "w") as f:
            f.write(f"{datetime.now().isoformat()} {reason}\n")
        try_self_heal()
        # re-check after the self-heal attempt
        state2, exit2 = check_tailscale()
        ok2, detail2 = check_mullvad()
        if state2 == "Running" and exit2 and ok2:
            os.remove(DOWN_FLAG)
            log(f"self-heal worked — exit={exit2}")
            return 0

    age = flag_age()
    if first_time or (age is not None and age >= REALERT_SECS):
        if not first_time:
            os.utime(DOWN_FLAG, None)  # reset the re-alert clock
        down_for = "" if first_time else f" (down {age/3600:.1f}h)"
        _tell(
            f"⚠️ VPN DOWN{down_for}\n{reason}\n\n"
            f"Outbound web lookups are refusing until this clears. "
            f"Reply 'home ok' to allow home routing for 30 minutes.",
            urgent=True, kind="vpn_down")
    return 1


if __name__ == "__main__":
    sys.exit(main())
