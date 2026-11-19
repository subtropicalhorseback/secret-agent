#!/usr/bin/env python3
"""
signal_watchdog.py
==================
Self-healing signal-cli watchdog for Chip.

Trigger options (both recommended):
  1. systemd OnFailure on signal-cli-daemon.service
  2. cron every 5 minutes: */5 * * * * $CHIP_WORKSPACE/signal_watchdog.py

Logic:
  1. Filter false positives (internet, tailscale, mullvad rotation)
  2. Active self-test via JSON-RPC socket (Chip messages itself)
  3. If broken: attempt fixes in order, retest after each
  4. Confirm fix with "did you get this? HH:MM" → 60s wait for the admin's reply
  5. On success: write report, send to the admin, archive log
  6. On exhausted fixes: write diagnostic, wait for SSH

Known fixes in order:
  1. Restart signal-cli-daemon
  2. Upgrade signal-cli to latest AsamK release
  3. Replace libsignal_jni.so from latest bbernhard image

All file ops are backed up before changes. Coordinator is never touched.
"""

import os, sys, json, time, socket, subprocess, struct, shutil, re
import urllib.request, urllib.error
from datetime import datetime
from pathlib import Path

# ── Config ────────────────────────────────────────────────────────────────────

import os as _os, sys as _sys
_cc = _os.path.dirname(_os.path.abspath(__file__))
while _cc != "/" and not _os.path.exists(_os.path.join(_cc, "chip_config.py")):
    _cc = _os.path.dirname(_cc)
if _cc not in _sys.path:
    _sys.path.insert(0, _cc)
from chip_config import WORKSPACE
SIGNAL_SOCKET   = "/run/signal-cli/signal.sock"
SIGNAL_CONFIG   = f"{WORKSPACE}/docker/signal"
SIGNAL_BIN      = "/usr/local/bin/signal-cli"
SIGNAL_OPT      = "/opt"
LIBSIGNAL_DIR   = "/usr/lib/signal-cli"
from chip_config import BOT_NUMBER as CHIP_NUMBER
import sys as _sys, os as _os
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
from chip_config import ADMIN_PHONE
LOG_PATH        = f"{WORKSPACE}/data/logs/signal_watchdog.log"
ARCHIVE_DIR     = f"{WORKSPACE}/data/archive/watchdog"
LOCK_FILE       = "/tmp/signal_watchdog.lock"
SELF_TEST_MSG   = f"[watchdog self-test {datetime.now().strftime('%H:%M')}]"
SELF_TEST_WAIT  = 30   # seconds to wait for self-test reply
CONFIRM_WAIT    = 60   # seconds to wait for the admin's confirmation
MULLVAD_ROTATE_WINDOW = 300  # seconds — ignore failures if exit node rotated recently

os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
os.makedirs(ARCHIVE_DIR, exist_ok=True)

# ── Logging ───────────────────────────────────────────────────────────────────

_log_lines = []

def log(msg, also_print=True):
    ts   = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    _log_lines.append(line)
    if also_print:
        print(line, flush=True)
    with open(LOG_PATH, "a") as f:
        f.write(line + "\n")

# ── Lock ──────────────────────────────────────────────────────────────────────

def acquire_lock():
    if os.path.exists(LOCK_FILE):
        age = time.time() - os.path.getmtime(LOCK_FILE)
        if age < 600:  # 10 minute stale lock timeout
            log(f"Already running (lock age {age:.0f}s) — exiting")
            sys.exit(0)
        log(f"Stale lock ({age:.0f}s old) — removing")
    Path(LOCK_FILE).touch()

def release_lock():
    try: os.unlink(LOCK_FILE)
    except: pass

# ── JSON-RPC socket helper ────────────────────────────────────────────────────

def _socket_send(payload: dict, timeout=35) -> dict | None:
    """Send one JSON-RPC request, return parsed response or None."""
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        sock.connect(SIGNAL_SOCKET)
        sock.sendall((json.dumps(payload) + "\n").encode())
        buf = b""
        while b"\n" not in buf:
            chunk = sock.recv(4096)
            if not chunk:
                break
            buf += chunk
        sock.close()
        return json.loads(buf.split(b"\n")[0])
    except Exception as e:
        log(f"Socket error: {e}")
        return None

def signal_send(recipient, message) -> bool:
    """Send a Signal message via JSON-RPC. Returns True on success."""
    resp = _socket_send({
        "jsonrpc": "2.0", "method": "send", "id": 1,
        "params": {"recipient": [recipient], "message": message}
    })
    return bool(resp and "result" in resp)

def socket_alive() -> bool:
    """Check if the signal-cli socket is accepting connections."""
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(5)
        sock.connect(SIGNAL_SOCKET)
        sock.close()
        return True
    except:
        return False

# ── False positive filters ────────────────────────────────────────────────────

def internet_ok() -> bool:
    try:
        sock = socket.create_connection(("8.8.8.8", 53), timeout=5)
        sock.close()
        return True
    except:
        return False

def tailscale_ok() -> bool:
    try:
        r = subprocess.run(["tailscale", "status", "--json"],
                           capture_output=True, text=True, timeout=10)
        data = json.loads(r.stdout)
        return data.get("BackendState") == "Running"
    except:
        return True  # if tailscale cmd unavailable, don't block on it

def mullvad_rotated_recently() -> bool:
    """Check if Mullvad exit node rotated in the last MULLVAD_ROTATE_WINDOW seconds."""
    try:
        r = subprocess.run(["journalctl", "-u", "tailscaled",
                            "--since", f"{MULLVAD_ROTATE_WINDOW} seconds ago",
                            "--no-pager", "-q"],
                           capture_output=True, text=True, timeout=10)
        return "exit node" in r.stdout.lower() or "mullvad" in r.stdout.lower()
    except:
        return False

def false_positive_check() -> str | None:
    """Returns reason string if this is a false positive, else None."""
    if not internet_ok():
        return "internet outage"
    if not tailscale_ok():
        return "tailscale down"
    if mullvad_rotated_recently():
        return f"mullvad exit node rotated within last {MULLVAD_ROTATE_WINDOW}s"
    return None

# ── Self-test ─────────────────────────────────────────────────────────────────

def self_test() -> bool:
    """
    Send Chip a message to itself and check socket response.
    True = signal-cli is working end-to-end.
    """
    if not socket_alive():
        log("Socket not alive — daemon not running")
        return False

    msg = f"[watchdog-ping {time.time():.0f}]"
    log(f"Self-test: sending to self ({CHIP_NUMBER})")
    result = signal_send(CHIP_NUMBER, msg)
    if result:
        log("Self-test: send succeeded")
        return True
    else:
        log("Self-test: send failed")
        return False

# ── Signal-cli version helpers ────────────────────────────────────────────────

def current_signal_cli_version() -> str | None:
    try:
        r = subprocess.run(["signal-cli", "--version"],
                           capture_output=True, text=True, timeout=10)
        m = re.search(r"signal-cli (\S+)", r.stdout)
        return m.group(1) if m else None
    except:
        return None

def latest_signal_cli_version() -> str | None:
    try:
        req = urllib.request.Request(
            "https://api.github.com/repos/AsamK/signal-cli/releases/latest",
            headers={"User-Agent": "chip-watchdog/1.0"}
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
            tag = data.get("tag_name", "")
            return tag.lstrip("v") if tag else None
    except:
        return None

def latest_libsignal_version_needed(signal_cli_version) -> str | None:
    """Parse the libsignal version signal-cli needs from its lib dir."""
    try:
        lib_dir = f"{SIGNAL_OPT}/signal-cli-{signal_cli_version}/lib"
        for f in os.listdir(lib_dir):
            m = re.match(r"libsignal-client-([0-9.]+)\.jar", f)
            if m:
                return m.group(1)
    except:
        pass
    return None

def current_libsignal_version() -> str | None:
    """Get version of currently installed libsignal_jni.so (from filename in any matching jar)."""
    try:
        # Check what version is in the active signal-cli
        ver = current_signal_cli_version()
        if ver:
            return latest_libsignal_version_needed(ver)
    except:
        pass
    return None

# ── Fix procedures ────────────────────────────────────────────────────────────

def backup(path, label=""):
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    dst = f"{path}.watchdog_bak_{ts}{('_' + label) if label else ''}"
    if os.path.isdir(path):
        shutil.copytree(path, dst)
    else:
        shutil.copy2(path, dst)
    log(f"Backed up {path} → {dst}")
    return dst

def daemon_restart() -> bool:
    log("Fix 1: restarting signal-cli-daemon")
    r = subprocess.run(["systemctl", "restart", "signal-cli-daemon"],
                       capture_output=True, text=True, timeout=30)
    time.sleep(8)
    ok = socket_alive()
    log(f"Fix 1: daemon {'running' if ok else 'still down'}")
    return ok

def upgrade_signal_cli() -> bool:
    log("Fix 2: attempting signal-cli upgrade")
    latest = latest_signal_cli_version()
    current = current_signal_cli_version()

    if not latest:
        log("Fix 2: could not fetch latest version — skipping")
        return False

    log(f"Fix 2: current={current} latest={latest}")

    if current == latest:
        log("Fix 2: already on latest version — skipping")
        return False

    # Download new version
    url = (f"https://github.com/AsamK/signal-cli/releases/download/"
           f"v{latest}/signal-cli-{latest}.tar.gz")
    dest = f"/tmp/signal-cli-{latest}.tar.gz"
    log(f"Fix 2: downloading {url}")
    try:
        urllib.request.urlretrieve(url, dest)
    except Exception as e:
        log(f"Fix 2: download failed: {e}")
        return False

    # Extract
    new_dir = f"{SIGNAL_OPT}/signal-cli-{latest}"
    if os.path.exists(new_dir):
        log(f"Fix 2: {new_dir} already exists, removing")
        shutil.rmtree(new_dir)

    r = subprocess.run(["tar", "xf", dest, "-C", SIGNAL_OPT],
                       capture_output=True, timeout=60)
    if r.returncode != 0:
        log(f"Fix 2: extract failed: {r.stderr}")
        return False

    # Backup and update wrapper
    backup(SIGNAL_BIN, "pre_upgrade")
    wrapper = open(SIGNAL_BIN).read()
    # Update the lib path in the wrapper
    updated = re.sub(
        r'signal-cli-[0-9.]+/lib',
        f'signal-cli-{latest}/lib',
        wrapper
    )
    open(SIGNAL_BIN, "w").write(updated)
    log(f"Fix 2: wrapper updated to signal-cli-{latest}")

    # Restart daemon
    subprocess.run(["systemctl", "restart", "signal-cli-daemon"],
                   capture_output=True, timeout=30)
    time.sleep(8)

    ok = socket_alive()
    log(f"Fix 2: daemon {'running' if ok else 'still down'} after upgrade")
    return ok

def replace_libsignal() -> bool:
    log("Fix 3: attempting libsignal replacement from bbernhard image")
    try:
        # Pull latest bbernhard image
        log("Fix 3: pulling bbernhard/signal-cli-rest-api:latest")
        r = subprocess.run(
            ["docker", "pull", "bbernhard/signal-cli-rest-api:latest"],
            capture_output=True, text=True, timeout=120
        )
        if r.returncode != 0:
            log(f"Fix 3: docker pull failed: {r.stderr}")
            return False

        # Start temp container
        r = subprocess.run(
            ["docker", "run", "-d", "--name", "watchdog-signal-tmp",
             "bbernhard/signal-cli-rest-api:latest", "sleep", "60"],
            capture_output=True, text=True, timeout=30
        )
        if r.returncode != 0:
            log(f"Fix 3: container start failed: {r.stderr}")
            return False

        time.sleep(5)

        # Find libsignal_jni.so in container
        r = subprocess.run(
            ["docker", "exec", "watchdog-signal-tmp",
             "find", "/tmp", "-name", "libsignal_jni.so"],
            capture_output=True, text=True, timeout=15
        )
        paths = [p.strip() for p in r.stdout.strip().splitlines() if p.strip()]

        if not paths:
            log("Fix 3: libsignal_jni.so not found in container — may need to trigger extraction")
            # Start signal-cli briefly to trigger extraction
            subprocess.run(
                ["docker", "exec", "watchdog-signal-tmp",
                 "signal-cli", "--version"],
                capture_output=True, timeout=15
            )
            time.sleep(3)
            r = subprocess.run(
                ["docker", "exec", "watchdog-signal-tmp",
                 "find", "/tmp", "-name", "libsignal_jni.so"],
                capture_output=True, text=True, timeout=10
            )
            paths = [p.strip() for p in r.stdout.strip().splitlines() if p.strip()]

        if not paths:
            log("Fix 3: still no libsignal_jni.so found — aborting")
            subprocess.run(["docker", "rm", "-f", "watchdog-signal-tmp"],
                           capture_output=True)
            return False

        so_path = paths[0]
        log(f"Fix 3: found {so_path}")

        # Back up current
        current_so = f"{LIBSIGNAL_DIR}/libsignal_jni.so"
        if os.path.exists(current_so):
            backup(current_so, "pre_libsignal_replace")

        # Copy out
        r = subprocess.run(
            ["docker", "cp",
             f"watchdog-signal-tmp:{so_path}",
             current_so],
            capture_output=True, text=True, timeout=30
        )
        subprocess.run(["docker", "rm", "-f", "watchdog-signal-tmp"],
                       capture_output=True)

        if r.returncode != 0:
            log(f"Fix 3: docker cp failed: {r.stderr}")
            return False

        log(f"Fix 3: libsignal_jni.so replaced")

        # Restart daemon
        subprocess.run(["systemctl", "restart", "signal-cli-daemon"],
                       capture_output=True, timeout=30)
        time.sleep(8)

        ok = socket_alive()
        log(f"Fix 3: daemon {'running' if ok else 'still down'} after libsignal replace")
        return ok

    except Exception as e:
        log(f"Fix 3: exception: {e}")
        subprocess.run(["docker", "rm", "-f", "watchdog-signal-tmp"],
                       capture_output=True)
        return False

# ── Confirmation ──────────────────────────────────────────────────────────────

def send_confirm_and_wait(fix_label: str) -> bool:
    """
    Send the admin a confirmation message and wait CONFIRM_WAIT seconds for a reply
    via coordinator (which will write to buffer). Simple existence check.
    """
    ts_str = datetime.now().strftime("%H:%M")
    msg = (f"Signal restored ({fix_label}) at {ts_str}. "
           f"Did you get this? Reply anything to confirm.")
    log(f"Sending confirmation to the admin: {msg}")
    signal_send(ADMIN_PHONE, msg)

    # Watch for the admin's reply in buffer
    from chip_config import ADMIN_NAME
    buffer_path = f"{WORKSPACE}/data/memory/buffer_{ADMIN_NAME.lower()}.txt"
    deadline    = time.time() + CONFIRM_WAIT
    last_size   = os.path.getsize(buffer_path) if os.path.exists(buffer_path) else 0

    while time.time() < deadline:
        time.sleep(3)
        try:
            size = os.path.getsize(buffer_path)
            if size > last_size:
                log("Confirmation received from the admin")
                return True
        except:
            pass

    log(f"No confirmation within {CONFIRM_WAIT}s")
    return False

# ── Report ────────────────────────────────────────────────────────────────────

def write_report(fix_label: str, duration_s: float, confirmed: bool):
    ts       = datetime.now().strftime("%Y%m%d_%H%M%S")
    report   = (
        f"Signal Watchdog Report — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"{'='*50}\n"
        f"Fix applied : {fix_label}\n"
        f"Duration    : {duration_s:.0f}s downtime\n"
        f"Confirmed   : {'yes' if confirmed else 'no (timeout)'}\n\n"
        f"Log extract:\n" +
        "\n".join(_log_lines[-30:])
    )
    report_path = f"{ARCHIVE_DIR}/{ts}_watchdog_report.txt"
    open(report_path, "w").write(report)
    log(f"Report written: {report_path}")

    if confirmed:
        signal_send(ADMIN_PHONE, f"Watchdog report:\n{report[:1000]}")

    # Archive full log
    archive_path = f"{ARCHIVE_DIR}/{ts}_watchdog.log"
    shutil.copy2(LOG_PATH, archive_path)
    open(LOG_PATH, "w").write("")  # roll the log
    log(f"Log archived: {archive_path}")

def write_failure_diagnostic():
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    diag = (
        f"Signal Watchdog — ALL FIXES EXHAUSTED — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"{'='*50}\n"
        f"signal-cli version : {current_signal_cli_version()}\n"
        f"Latest available   : {latest_signal_cli_version()}\n"
        f"Socket alive       : {socket_alive()}\n\n"
        f"Recent log:\n" +
        "\n".join(_log_lines)
    )
    diag_path = f"{ARCHIVE_DIR}/{ts}_watchdog_failure.txt"
    open(diag_path, "w").write(diag)
    log(f"Diagnostic written: {diag_path}")
    log("Manual intervention required. SSH and check log.")

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    acquire_lock()
    start_time = time.time()

    try:
        log("=== signal_watchdog starting ===")

        # Step 1: false positive check
        fp = false_positive_check()
        if fp:
            log(f"False positive: {fp} — exiting without action")
            return

        # Guard (19 Nov 2026): a blank bot number is a CONFIG fault, not a
        # signal-cli fault. That exact condition once drove 14 consecutive
        # upgrade + libsignal-replace attempts against a healthy daemon.
        if not (CHIP_NUMBER or "").strip():
            log("CONFIG ERROR: bot number is empty (SENDER_NUMBER / "
                "config/users.json bot_number) — no self-test, no recovery")
            return

        # Step 2: self-test
        if self_test():
            log("Self-test passed — signal-cli healthy, exiting")
            return

        log("Self-test failed — signal-cli appears broken, starting recovery")

        # Step 3: fixes in order
        fixes = [
            ("daemon restart",      daemon_restart),
            ("signal-cli upgrade",  upgrade_signal_cli),
            ("libsignal replace",   replace_libsignal),
        ]

        for fix_label, fix_fn in fixes:
            # Guard: the invasive fixes (upgrade, libsignal replace) are only
            # reachable when the daemon is actually DOWN. If the socket
            # answers but the self-test still fails, the cause is something
            # replacing binaries will not fix blindly — stop and report.
            if fix_fn is not daemon_restart and socket_alive():
                log(f"Skipping {fix_label}: daemon socket is alive, so the "
                    f"self-test failure is not a dead daemon — not escalating")
                break
            log(f"Attempting: {fix_label}")
            if fix_fn():
                # Daemon is up, verify with self-test
                time.sleep(5)
                if self_test():
                    log(f"Recovery successful via: {fix_label}")
                    duration = time.time() - start_time
                    confirmed = send_confirm_and_wait(fix_label)
                    write_report(fix_label, duration, confirmed)
                    return
                else:
                    log(f"{fix_label} started daemon but self-test still failing")
            else:
                log(f"{fix_label} did not bring daemon up")

        # All fixes exhausted
        log("All fixes exhausted")
        write_failure_diagnostic()

    finally:
        release_lock()
        log("=== signal_watchdog done ===")

if __name__ == "__main__":
    main()
