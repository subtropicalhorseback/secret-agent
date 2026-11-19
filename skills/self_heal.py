"""
skills/self_heal.py — Chip repairs itself via Claude Code, with the admin's approval
===============================================================================
Added 19 Nov 2026. Replaces the in-band patcher the audit wrote off (M5: it
waited on log strings that no longer print, called functions with the wrong
arity, and showed the model 8,000 characters of a 160KB file). Instead of
generating patches from a keyhole, Chip now delegates repair to the Claude
Code CLI already installed on this Pi — the same tooling the admin uses, with the
whole repo, real file tools, and CLAUDE.md context.

Flow (admin-only, two-step by design):
  fix: <description>     stage a repair job (description + recent log tail)
  fix: approve           launch it — detached, so it survives a coordinator
                         restart mid-repair; results arrive on Signal
  fix: reject            discard the staged job
  fix: status            show staged/last job

The launched job runs `claude -p` with --dangerously-skip-permissions: that is
deliberate and bounded — nothing runs until the admin has read the staged job and
typed approve, the process works as the same user with the same access the
patcher had, and the prompt pins house rules (timestamped backups, anchored
edits, compile checks, never print .env). The explicit human gate IS the
permission model.
"""
import json
import os
import shutil
import socket
import subprocess
import sys
from datetime import datetime

import os as _os, sys as _sys
_cc = _os.path.dirname(_os.path.abspath(__file__))
while _cc != "/" and not _os.path.exists(_os.path.join(_cc, "chip_config.py")):
    _cc = _os.path.dirname(_cc)
if _cc not in _sys.path:
    _sys.path.insert(0, _cc)
from chip_config import WORKSPACE
PENDING_FILE  = f"{WORKSPACE}/data/patches/pending_heal.json"
HEAL_LOG_DIR  = f"{WORKSPACE}/data/patches"
SIGNAL_SOCKET = os.getenv("SIGNAL_SOCKET", "/run/signal-cli/signal.sock")
import sys as _sys
_sys.path.insert(0, WORKSPACE)
from chip_config import ADMIN_PHONE
TIMEOUT_SECS  = 1800

CLAUDE_BIN = (shutil.which("claude")
              or os.path.expanduser("~/.npm-global/bin/claude"))

HOUSE_RULES = """You are repairing Chip, the live assistant on this Pi, at the admin's request.
Rules:
- Timestamped backup (cp file file.bak_$(date +%Y%m%d_%H%M%S)) before editing any file.
- Anchored, minimal edits; python -m py_compile every file you change (venv/bin/python3).
- NEVER print, log, or send the contents of .env, API keys, or tokens.
- If you changed coordinator.py or skills/, finish with: sudo systemctl restart chip.service
  and confirm `systemctl is-active chip` says active.
- If the fix is unsafe or unclear, STOP and say so rather than guessing.
- End with a 3-6 line plain-text summary of what you changed and how you verified it."""


def _signal_send(message, recipient=ADMIN_PHONE):
    """Standalone Signal send (same pattern as vpn_guard) — must not depend
    on the coordinator, which may be mid-restart during a heal."""
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(30)
        s.connect(SIGNAL_SOCKET)
        s.sendall((json.dumps({
            "jsonrpc": "2.0", "method": "send", "id": 1,
            "params": {"recipient": [recipient], "message": message},
        }) + "\n").encode())
        buf = b""
        while b"\n" not in buf:
            chunk = s.recv(4096)
            if not chunk:
                break
            buf += chunk
        s.close()
        return True
    except Exception as e:
        print(f"self_heal signal send failed: {e}")
        return False


def _log_tail(lines=80):
    try:
        out = subprocess.check_output(
            ["tail", "-n", str(lines), f"{WORKSPACE}/coordinator.log"],
            text=True, timeout=10)
        return out[-6000:]
    except Exception:
        return "(log unavailable)"


# ─── Coordinator-side: staging and launch ─────────────────────────────────────

def stage(description, staged_by="the admin"):
    os.makedirs(HEAL_LOG_DIR, exist_ok=True)
    spec = {
        "description": description.strip(),
        "staged_by":   staged_by,
        "staged_at":   datetime.now().isoformat(),
        "log_tail":    _log_tail(),
    }
    json.dump(spec, open(PENDING_FILE, "w"), indent=2)
    return (f"Heal job staged:\n«{spec['description']}»\n\n"
            f"Claude Code will get the repo, CLAUDE.md context, and the last "
            f"80 log lines, with full edit + restart permissions.\n"
            f"Reply 'fix: approve' to launch, 'fix: reject' to discard.")


def reject():
    if os.path.exists(PENDING_FILE):
        os.remove(PENDING_FILE)
        return "Staged heal job discarded."
    return "Nothing staged."


def status():
    if os.path.exists(PENDING_FILE):
        spec = json.load(open(PENDING_FILE))
        return (f"Staged (awaiting 'fix: approve'): «{spec['description']}» "
                f"at {spec['staged_at'][:16]}")
    logs = sorted(f for f in os.listdir(HEAL_LOG_DIR)
                  if f.startswith("heal_2") and f.endswith(".log"))
    if logs:
        return f"Nothing staged. Last run: {logs[-1]} (cat: data/patches/{logs[-1]})"
    return "Nothing staged; no past runs."


def approve():
    if not os.path.exists(PENDING_FILE):
        return "Nothing staged — use fix: <description> first."
    # Detached (new session): survives the coordinator restarting under it,
    # which a successful heal of coordinator.py will do.
    subprocess.Popen(
        [f"{WORKSPACE}/venv/bin/python3", f"{WORKSPACE}/skills/self_heal.py", "--run"],
        cwd=WORKSPACE, start_new_session=True,
        stdout=open(f"{HEAL_LOG_DIR}/heal_launcher.log", "a"),
        stderr=subprocess.STDOUT,
    )
    return ("Healer launched (detached — it survives a service restart). "
            "Progress and the result will arrive here. Typical run: 2-10 min.")


# ─── Detached runner ──────────────────────────────────────────────────────────

def run_job():
    if not os.path.exists(PENDING_FILE):
        print("no pending job")
        return 1
    spec = json.load(open(PENDING_FILE))
    os.remove(PENDING_FILE)                      # consume — no double runs
    ts       = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = f"{HEAL_LOG_DIR}/heal_{ts}.log"

    prompt = (f"{HOUSE_RULES}\n\n"
              f"PROBLEM (from {spec['staged_by']} over Signal):\n"
              f"{spec['description']}\n\n"
              f"RECENT coordinator.log TAIL:\n{spec['log_tail']}")

    _signal_send(f"🔧 Healer running: «{spec['description'][:120]}»")

    env = dict(os.environ)
    env["PATH"] = (os.path.expanduser("~/.npm-global/bin") + ":/usr/local/bin:/usr/bin:/bin:"
                   + env.get("PATH", ""))
    env.setdefault("HOME", os.path.expanduser("~"))

    try:
        result = subprocess.run(
            [CLAUDE_BIN, "-p", prompt, "--dangerously-skip-permissions"],
            cwd=WORKSPACE, env=env, timeout=TIMEOUT_SECS,
            capture_output=True, text=True)
        output = (result.stdout or "") + ("\n--- stderr ---\n" + result.stderr
                                          if result.stderr else "")
        verdict = "✅ Healer finished" if result.returncode == 0 else \
                  f"⚠️ Healer exited {result.returncode}"
    except subprocess.TimeoutExpired as e:
        output  = (e.stdout or "") if isinstance(e.stdout, str) else ""
        verdict = f"⏱️ Healer timed out after {TIMEOUT_SECS//60} min"
    except Exception as e:
        output, verdict = str(e), "❌ Healer failed to run"

    open(log_path, "w").write(
        f"JOB: {spec['description']}\nSTAGED: {spec['staged_at']}\n"
        f"VERDICT: {verdict}\n\n{output}")

    # Service state is the ground truth after any heal.
    try:
        svc = subprocess.check_output(
            ["systemctl", "is-active", "chip"], text=True, timeout=10).strip()
    except Exception:
        svc = "unknown"

    tail = output.strip()[-1100:] if output.strip() else "(no output)"
    _signal_send(f"{verdict} — chip.service: {svc}\n\n…{tail}\n\n"
                 f"Full log: cat: {os.path.basename(log_path)} "
                 f"(data/patches/)")
    return 0


if __name__ == "__main__":
    if "--run" in sys.argv:
        sys.exit(run_job())
    print(status())
