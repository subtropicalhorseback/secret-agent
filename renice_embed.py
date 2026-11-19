#!/usr/bin/env python3
"""
renice_embed.py
================
Keeps the nomic-embed-text Ollama runner at a higher nice value (lower CPU
priority) than phi4-mini. Runs via cron every 5 minutes.

Why: both models are pinned in RAM as persistent runners (keep_alive=-1).
When embedding and phi4-mini inference happen simultaneously, they split the
Pi5's four CPU cores and phi4-mini's generation balloons from ~3s to ~28s.
Renicing the embedder to +10 tells Linux's scheduler to yield CPU to
phi4-mini whenever both want to run at once — embedding calls slow slightly
under contention instead of stealing cycles from core conversational ops.

Identification: nomic-embed-text's runner uses --ollama-engine flag;
phi4-mini's does not. More reliable than blob hash, more specific than size.

PID stability: Ollama assigns a fresh runner PID on every model reload, so
this runs periodically rather than once. Already-niced PIDs are skipped (no
double-nice, no spurious output).
"""

import subprocess
import sys

EMBED_NICE  = 10   # +10 = background worker, yields to normal-priority processes
RENICE_BIN  = "/usr/bin/renice"


def find_embed_pid():
    r = subprocess.run(["ps", "aux"], capture_output=True, text=True)
    for line in r.stdout.splitlines():
        if "ollama runner" in line and "--ollama-engine" in line:
            try:
                return int(line.split()[1])
            except (IndexError, ValueError):
                continue
    return None


def get_nice(pid):
    try:
        r = subprocess.run(
            ["ps", "-o", "ni=", "-p", str(pid)],
            capture_output=True, text=True
        )
        return int(r.stdout.strip())
    except Exception:
        return None


def main():
    pid = find_embed_pid()
    if pid is None:
        print("nomic-embed-text runner not found — may not be loaded yet")
        return

    current = get_nice(pid)
    if current is None:
        print(f"Could not read nice value for PID {pid}")
        return

    if current >= EMBED_NICE:
        # Already at or below our target priority — nothing to do
        return

    r = subprocess.run(
        [RENICE_BIN, "-n", str(EMBED_NICE), "-p", str(pid)],
        capture_output=True, text=True
    )
    if r.returncode == 0:
        print(f"Reniced PID {pid} (nomic-embed-text): nice {current} -> {EMBED_NICE}")
    else:
        print(f"renice failed for PID {pid}: {r.stderr.strip()}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
