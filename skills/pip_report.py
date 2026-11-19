#!/usr/bin/env python3
"""
skills/pip_report.py — monthly outdated-package report that someone reads
==========================================================================
The old crontab line wrote `pip list --outdated` to a log nobody opened.
This runs the same read-only check and messages the admin only when the list
is non-empty. It never installs or upgrades anything.
"""
import json
import os
import socket
import subprocess
import sys
from datetime import datetime

_cc = os.path.dirname(os.path.abspath(__file__))
while _cc != "/" and not os.path.exists(os.path.join(_cc, "chip_config.py")):
    _cc = os.path.dirname(_cc)
sys.path.insert(0, _cc)
import chip_config                                   # noqa: E402

PIP = f"{chip_config.WORKSPACE}/venv/bin/pip"


def main():
    r = subprocess.run([PIP, "list", "--outdated", "--format=json"],
                       capture_output=True, text=True, timeout=300)
    if r.returncode != 0:
        print(f"[{datetime.now():%F %T}] pip failed: {r.stderr.strip()[:300]}")
        return 1
    pkgs = json.loads(r.stdout or "[]")
    print(f"[{datetime.now():%F %T}] {len(pkgs)} outdated package(s)")
    for p in pkgs:
        print(f"  {p['name']} {p['version']} -> {p['latest_version']}")
    if not pkgs:
        return 0
    shown = ", ".join(f"{p['name']} {p['version']}→{p['latest_version']}" for p in pkgs[:12])
    more  = f" and {len(pkgs) - 12} more" if len(pkgs) > 12 else ""
    msg = (f"[Chip] Monthly package check: {len(pkgs)} Python package(s) have updates. "
           f"Nothing was changed. {shown}{more}. Full list: data/logs/pip_outdated.log")
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(30)
        s.connect(os.getenv("SIGNAL_SOCKET", "/run/signal-cli/signal.sock"))
        s.sendall((json.dumps({"jsonrpc": "2.0", "method": "send", "id": 1,
                               "params": {"recipient": [chip_config.ADMIN_PHONE],
                                          "message": msg}}) + "\n").encode())
        s.recv(4096)
        s.close()
    except Exception as e:
        print(f"signal send failed: {e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main() if "--dry-run" not in sys.argv else 0)
