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
  fix: approve [ID]      launch it — detached, so it survives a coordinator
                         restart mid-repair; results arrive on Signal
  fix: reject [ID]       reject the staged job (kept as a record, restorable)
  fix: restore ID        put a rejected job back on the queue
  fix: status            show the staged-fix index

Since 20 Nov 2026 the queue itself lives in skills/fix_index.py — a durable,
multi-slot SQLite store where every order carries a stable FIX-NNNN id and
nothing is ever deleted. This module keeps the launch, the sandbox decisions
and the Signal reporting; it no longer owns the state. See fix_index.py for
why that split exists.

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
from skills import fix_index
PENDING_FILE  = f"{WORKSPACE}/data/patches/pending_heal.json"   # legacy; migrated by fix_index
HEAL_LOG_DIR  = f"{WORKSPACE}/data/patches"
SIGNAL_SOCKET = os.getenv("SIGNAL_SOCKET", "/run/signal-cli/signal.sock")
import sys as _sys
_sys.path.insert(0, WORKSPACE)
from chip_config import ADMIN_PHONE
TIMEOUT_SECS  = 1800
REPAIR_MAX_CHARS = 3000      # one problem per repair order (spec D.1)
PER_RUN_CAP_USD  = 6.0       # passed to claude --max-budget-usd
DAILY_CAP_USD    = 10.0      # past this, approval needs 'override' (spec D.7)
BUILD_RETRIES    = 1         # build track: resume the same session once after a failed gate
RESTART_AFTER_ROLLBACK = True  # tests switch this off
GATE_FLAGS = []                # tests pass ["--no-restart", "--no-live"]
# Gitignored household/runtime files a heal can touch but git cannot revert.
SNAPSHOT_FILES = ["config/users.json", "config/routing_constants.py", "config/domains.py",
                  "config/jobs.yaml", "agents/chip/SOUL.md", "CLAUDE.md"]


def _cap(name, default):
    try:
        from skills.browse_capture import _RC
        return float(getattr(_RC, name, default))
    except Exception:
        return default
# Pinned explicitly: the CLI's saved default can be an alias a headless run
# cannot resolve (a job failed that way on 19 Nov 2026).
HEAL_MODEL    = os.getenv("CHIP_HEAL_MODEL", "claude-opus-5")

CLAUDE_BIN = (shutil.which("claude")
              or os.path.expanduser("~/.npm-global/bin/claude"))

HOUSE_RULES = """You are repairing Chip, the live assistant on this Pi, at the admin's request.
Rules:
- Timestamped backup before editing any existing file, into backups/<source>/
  (e.g. cp skills/x.py backups/x/x.py.bak.$(date +%Y%m%d_%H%M%S)). Leave no stray
  files in the tree: the gate fails on anything untracked.
- Anchored, minimal edits; python -m py_compile every file you change (venv/bin/python3).
- NEVER print, log, or send the contents of .env, API keys, or tokens.
- If you changed coordinator.py or skills/, restart with
  `venv/bin/python3 skills/safe_restart.py` (it waits for running work to finish,
  up to 10 minutes, then restarts and confirms chip is active). Never call
  `systemctl restart chip` directly: it kills in-flight requests.
- If the fix is unsafe or unclear, STOP and say so rather than guessing.
- Git: if the job asks for a commit, commit LOCALLY only. Never push, never add or
  change a remote, never rewrite history. Report the commit hash.
- Keep household data out of code: names, numbers, places and vocabulary belong in
  the gitignored files under config/, read through chip_config. That includes
  comments and tests: no household names in any tracked file.
- End with a 3-6 line plain-text summary of what you changed and how you verified it,
  then ONE final line in exactly this form (the admin's phone shows only this):
  RESULT: <one sentence, plain words, what changed> | STATUS: <verified live | built, not verified | failed> | COMMIT: <hash or none>
  "verified live" means you exercised the real path and saw it work; tests against
  stubs are "built, not verified". Your STATUS is for the log only: the status the
  admin sees comes from the gate that runs after you (skills/fix_gate.py)."""


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

def stage(description, staged_by="the admin", supersedes=None, track="repair",
          files=None, acceptance="", acceptance_cmd="", skill_name=None, continue_of=None):
    """Stage an order. Returns (info_dict, message). Refuses (ValueError) an
    order the gate could not judge (spec D.5 'before' — code first):

      repair  one problem: at most REPAIR_MAX_CHARS of description; `files` is
              the list of paths/globs it may touch (default: any tracked file
              except the protected gate machinery).
      build   a NEW skill: skill_name is required and skills/<name>.py must
              not exist yet (unless continuing an earlier build). It may only
              add skills/<name>.py and tests/test_<name>.py.
    """
    import re as _re
    track = (track or "repair").lower()
    description = (description or "").strip()
    if track not in ("repair", "build"):
        raise ValueError("track must be repair or build")
    if continue_of:
        prev = fix_index.get(continue_of)
        if not prev:
            raise ValueError(f"{continue_of} does not exist")
        if not prev.get("session_id"):
            raise ValueError(f"{continue_of} has no session to continue")
        track = prev.get("track") or track
        skill_name = skill_name or prev.get("skill_name")
        if not files:
            try:
                files = json.loads(prev.get("allowed_paths") or "[]")
            except Exception:
                files = None
    if track == "repair":
        if len(description) > REPAIR_MAX_CHARS:
            raise ValueError(f"that order is {len(description)} characters; a repair is one "
                             f"problem in at most {REPAIR_MAX_CHARS}. Split it, or make it a "
                             f"redesign for an interactive session.")
        files = [f.strip() for f in (files or ["*"]) if f and f.strip()]
    else:
        skill_name = _re.sub(r"[^a-z0-9_]", "", (skill_name or "").lower())
        if not skill_name:
            raise ValueError("a build order needs a skill name (letters, digits, underscores)")
        if not continue_of and os.path.exists(os.path.join(WORKSPACE, "skills", f"{skill_name}.py")):
            raise ValueError(f"skills/{skill_name}.py already exists — a change to it is a repair")
        files = [f"skills/{skill_name}.py", f"tests/test_{skill_name}.py"]
    os.makedirs(HEAL_LOG_DIR, exist_ok=True)
    info = fix_index.stage(description, staged_by=staged_by, supersedes=supersedes,
                           log_tail=_log_tail(), track=track, allowed_paths=files,
                           acceptance=acceptance, acceptance_cmd=acceptance_cmd,
                           skill_name=skill_name, continue_of=continue_of)
    # One line (One Voice spec D.6). The full work order is one command away
    # ('fix: status FIX-NNNN') instead of being dumped on the phone; the id
    # and title here come from the stored record, so what the admin approves
    # is what was staged, not a model's paraphrase of it.
    extra = f" Replaces {info['supersedes']}." if info.get("supersedes") else ""
    kind = f"new skill '{skill_name}'" if track == "build" else "repair"
    cont = f", continuing {continue_of}" if continue_of else ""
    # What is being approved, visibly: the files it may touch and any command
    # the gate will run (Chip writes these; the admin should see them).
    scope = (f" Files: {', '.join(files)}." if track == "repair" else "")
    check_cmd = f" Gate will run: {acceptance_cmd}." if acceptance_cmd else ""
    msg = (f"Staged {info['fix_id']} ({kind}{cont}): {fix_index._summary(description, 110)}{extra}"
           f"{scope}{check_cmd} Approve with 'fix: approve {info['fix_id']}'; "
           f"full text: 'fix: status {info['fix_id']}'.")
    return info, msg


def reject(fix_id=None):
    _, msg = fix_index.reject(fix_id)
    return msg


def restore(fix_id=None):
    _, msg = fix_index.restore(fix_id)
    return msg


def status():
    return fix_index.render()


def detail(fix_id):
    """'fix: status FIX-NNNN' — one record in full: the exact staged text,
    its status, phases and log. This is where the work order lives now that
    staging replies are one line."""
    rec = fix_index.get(fix_id)
    if not rec:
        return f"No record {fix_id.strip().upper()}."
    try:
        phases = json.loads(rec.get("phases") or "[]")
    except Exception:
        phases = []
    lines = [f"{rec['fix_id']} — {rec['status']} (requested by {rec['requested_by']}, "
             f"{rec['created_at'][:16].replace('T', ' ')})"]
    if rec.get("supersedes"):
        lines.append(f"Replaces {rec['supersedes']}.")
    if rec.get("superseded_by"):
        lines.append(f"Superseded by {rec['superseded_by']}.")
    lines.append(f"Track: {rec.get('track') or 'repair'}"
                 + (f" (skill {rec['skill_name']})" if rec.get("skill_name") else ""))
    try:
        _files = json.loads(rec.get("allowed_paths") or "[]")
    except Exception:
        _files = []
    if _files:
        lines.append(f"May touch: {', '.join(_files)}")
    if rec.get("acceptance"):
        lines.append(f"Acceptance: {rec['acceptance']}")
    if rec.get("acceptance_cmd"):
        lines.append(f"Gate will run: {rec['acceptance_cmd']}")
    if rec.get("gate_status"):
        lines.append(f"Gate: {rec['gate_status']}"
                     + (f" · ${rec['cost_usd']:.2f}" if rec.get("cost_usd") is not None else ""))
    if rec.get("session_id"):
        lines.append(f"Session: {rec['session_id']} (claude --resume {rec['session_id']})")
    lines += ["", rec["description"].strip()]
    if phases:
        lines += ["", "Phases:"] + [f"- {p.get('at', '')[11:16]} {p.get('note', '')}"
                                    for p in phases[-8:]]
    if rec.get("log_path"):
        lines += ["", f"Log: {os.path.basename(rec['log_path'])}"]
    if rec.get("note"):
        lines += [f"Note: {rec['note']}"]
    return "\n".join(lines)


def _launch_unit():
    """Start a detached healer unit. Returns (ok, detail).

    Launched as its own transient systemd unit. A plain detached child stays
    inside chip.service's control group, and systemd kills the whole group on
    restart - which is exactly how a healer died mid-job on 19 Nov 2026, the
    moment it restarted the service it had just repaired."""
    unit = f"chip-heal-{datetime.now():%Y%m%d-%H%M%S}"
    try:
        r = subprocess.run(
            ["sudo", "-n", "systemd-run", f"--unit={unit}", "--collect",
             "--uid", str(os.getuid()), "--gid", str(os.getgid()),
             f"--working-directory={WORKSPACE}",
             f"--setenv=HOME={os.path.expanduser('~')}",
             f"--setenv=CHIP_WORKSPACE={WORKSPACE}",
             f"{WORKSPACE}/venv/bin/python3", f"{WORKSPACE}/skills/self_heal.py", "--run"],
            capture_output=True, text=True, timeout=30)
    except Exception as e:
        return False, str(e)[:300]
    if r.returncode != 0:
        return False, (r.stderr or r.stdout).strip()[:300]
    return True, unit


def approve(fix_id=None, override=False):
    """Human-only. Marks the record approved; launches it only if the runner
    is free. A second approval while one runs stays queued rather than racing.
    Past the daily heal spend cap, approval needs 'override' (spec D.7)."""
    spent, runs = fix_index.spend_today()
    cap = _cap("HEAL_DAILY_CAP_USD", DAILY_CAP_USD)
    if spent >= cap and not override:
        target = (fix_id or "").strip().upper() or "the staged job"
        return (f"Heal spend today is ${spent:.2f} over {runs} run(s), past the ${cap:.0f} "
                f"cap. Approve anyway with 'fix: approve {target} override'.")
    approved, msg = fix_index.approve(fix_id)
    if not approved:
        return msg
    if fix_index.running():
        return msg                       # already explains the queueing
    ok, detail = _launch_unit()
    if not ok:
        fix_index.set_status(approved, "failed", note=f"launch failed: {detail}")
        return f"{approved} approved but the healer failed to launch: {detail}"
    return f"{approved} approved and running. I'll tell you when it's done, usually 2-10 min."


# ─── Detached runner ──────────────────────────────────────────────────────────

def run_job():
    """One heal, start to finish (One Voice spec D.4/D.5):
    code before (snapshot, start commit, budget, session) → the healer →
    code after (the gate, run from the starting commit) → status, rollback or
    switch-off, one result line. Every exit reaches _finish_job."""
    spec = fix_index.claim_next()
    if spec is None:
        print("no approved job to claim")
        return 1
    fix_id   = spec["fix_id"]
    track    = spec.get("track") or "repair"
    ts       = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = f"{HEAL_LOG_DIR}/heal_{ts}.log"
    fix_index.set_status(fix_id, "running", log_path=os.path.basename(log_path))
    start_head = _git_head(full=True)
    if not start_head:
        # Without a starting commit the gate has nothing to compare against
        # and a rollback has nothing to return to. Refuse to run at all.
        open(log_path, "w").write(f"JOB: {fix_id}\nREFUSED: could not read the starting commit "
                                  f"(git rev-parse HEAD failed)\n")
        _finish_job(fix_id, spec, track, {"status": "failed", "checks": [],
                    "reason": "could not read the starting commit; the healer was not run"},
                    0.0, "", log_path, None, {}, 0)
        return 1
    snap = _snapshot(fix_id)
    session, resume = _session_for(spec)
    fix_index.set_fields(fix_id, session_id=session, start_head=start_head)
    try:
        allowed = json.loads(spec.get("allowed_paths") or "[]")
    except Exception:
        allowed = []
    # Per-run spend cap, enforced by Claude Code itself; the daily cap is
    # enforced when the admin approves (approve()).
    budget = round(_cap("HEAL_RUN_CAP_USD", PER_RUN_CAP_USD), 2)

    header = (f"JOB: {fix_id} ({track})\nORDER: {spec['description']}\n"
              f"STAGED: {spec['created_at']}\nSTARTED: {datetime.now().isoformat()}\n"
              f"SESSION: {session} ({'resumed' if resume else 'new'})  BUDGET: ${budget}\n"
              f"START: {start_head}\n\n")
    open(log_path, "w").write(header)

    gate, cost_total, result_text, iterations = None, 0.0, "", 0
    note = ""
    try:
        rc, cost, result_text = _run_claude(_prompt(spec, fix_id, track, allowed),
                                            session, resume, budget, log_path)
        cost_total += cost
        iterations = 1
        if rc != 0:
            note = f"healer exited {rc}"
        while True:
            gate = _run_gate(fix_id, start_head, track, allowed, spec, log_path)
            changed_snap = _snapshot_changed(snap)
            if track == "build" and changed_snap and gate["status"] != "failed":
                gate["status"] = "failed"
                gate["reason"] = f"a build may not change household config: {changed_snap}"
            if gate["status"] != "failed" or track != "build" or iterations > BUILD_RETRIES:
                break
            # Build track: inside the one approval, the builder gets one more
            # go in the same session, with the gate's findings (spec D.2).
            left = max(0.5, budget - cost_total)
            failed = [c for c in gate.get("checks", []) if not c["ok"]]
            rc, cost, more = _run_claude(
                "The gate failed: " + gate.get("reason", "") + "\n" +
                "\n".join(f"- {c['check']}: {c['detail']}" for c in failed) +
                "\nFix it within the same limits (only add skills/" +
                f"{spec.get('skill_name')}.py and tests/test_{spec.get('skill_name')}.py), "
                "commit, and end with the RESULT line.",
                session, True, round(left, 2), log_path)
            cost_total += cost
            result_text = more or result_text
            iterations += 1
    except Exception as e:
        note = f"runner error: {type(e).__name__}: {str(e)[:200]}"
        print(f"self_heal: {note}")
    finally:
        _finish_job(fix_id, spec, track, gate, cost_total, result_text, log_path,
                    start_head, snap, iterations, note)
    return 0


def _prompt(spec, fix_id, track, allowed):
    try:
        from skills.fix_gate import PROTECTED
    except Exception:
        PROTECTED = []
    if track == "build":
        name = spec.get("skill_name")
        scope = (f"TRACK: build — a NEW skill. Create exactly skills/{name}.py and "
                 f"tests/test_{name}.py and nothing else. The skill declares its own tools "
                 f"(CHIP_TOOLS list + async chip_tool(name, tool_input, *, sender, tier, "
                 f"session)); read skills/skill_loader.py for the contract. Do not edit any "
                 f"existing file. Do not restart Chip yourself; the gate does.")
    else:
        scope = (f"TRACK: repair — one problem. You may change only: {allowed or ['*']}. "
                 f"Never touch: {PROTECTED}. Do not modify or delete existing tests; add "
                 f"new ones as tests/test_<topic>.py.")
    acc = spec.get("acceptance") or ""
    if spec.get("acceptance_cmd"):
        acc += f"\nThe gate will run this acceptance command: {spec['acceptance_cmd']}"
    cont = (f"CONTINUING {spec['continue_of']} in the same session.\n"
            if spec.get("continue_of") else "")
    return (f"{HOUSE_RULES}\n\n"
            f"Follow the checklist in .claude/skills/ship-a-fix/SKILL.md.\n"
            f"{scope}\n"
            f"After you finish, a gate script re-runs every test, checks scope and "
            f"protected files, restarts Chip, checks it live and decides the status. "
            f"You do not decide it.\n\n"
            f"YOUR FIX ID IS {fix_id}. Register each phase as you start it:\n"
            f"  {WORKSPACE}/venv/bin/python3 {WORKSPACE}/skills/fix_index.py "
            f"--phase {fix_id} \"Phase N started: <what>\"\n\n"
            f"{cont}ORDER (from {spec['requested_by']}):\n{spec['description']}\n\n"
            + (f"ACCEPTANCE (how it will be checked live):\n{acc}\n\n" if acc else "")
            + f"RECENT coordinator.log TAIL:\n{spec['log_tail'] or '(none)'}")


def _session_for(spec):
    import uuid
    if spec.get("continue_of"):
        prev = fix_index.get(spec["continue_of"]) or {}
        if prev.get("session_id"):
            return prev["session_id"], True
    return str(uuid.uuid4()), False


def _run_claude(prompt, session, resume, budget, log_path):
    """Run Claude Code headless, streaming its events into a readable log.
    Returns (returncode, cost_usd, final_result_text)."""
    import threading
    env = dict(os.environ)
    env["PATH"] = (os.path.expanduser("~/.npm-global/bin") + ":/usr/local/bin:/usr/bin:/bin:"
                   + env.get("PATH", ""))
    env.setdefault("HOME", os.path.expanduser("~"))
    args = [CLAUDE_BIN, "-p", prompt, "--model", HEAL_MODEL,
            "--dangerously-skip-permissions", "--output-format", "stream-json", "--verbose",
            "--max-budget-usd", str(budget)]
    args += ["--resume", session] if resume else ["--session-id", session]
    cost, result = 0.0, ""
    with open(log_path, "a") as lf:
        proc = subprocess.Popen(args, cwd=WORKSPACE, env=env, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True)
        killer = threading.Timer(TIMEOUT_SECS, proc.kill)
        killer.start()
        try:
            for line in proc.stdout:
                try:
                    ev = json.loads(line)
                except Exception:
                    lf.write(line); lf.flush()
                    continue
                t = ev.get("type")
                if t == "assistant":
                    for b in ev.get("message", {}).get("content", []):
                        if b.get("type") == "text" and b.get("text", "").strip():
                            lf.write(b["text"].rstrip() + "\n")
                        elif b.get("type") == "tool_use":
                            lf.write(f"→ {b.get('name')}: {json.dumps(b.get('input'))[:240]}\n")
                elif t == "result":
                    cost = float(ev.get("total_cost_usd") or 0)
                    result = str(ev.get("result") or "")
                    lf.write(f"\n[session {ev.get('session_id')} · ${cost:.2f} · "
                             f"{ev.get('num_turns')} turns · {ev.get('subtype')}]\n")
                lf.flush()
            proc.wait()
        finally:
            killer.cancel()
        if proc.returncode and proc.returncode < 0:
            lf.write(f"\n[killed after {TIMEOUT_SECS // 60} min]\n")
    return proc.returncode, cost, result


def _run_gate(fix_id, start_head, track, allowed, spec, log_path):
    """Run fix_gate.py AS IT WAS AT THE STARTING COMMIT, so the healer cannot
    weaken the check it is judged by."""
    gate_src = None
    if start_head:
        code = subprocess.run(["git", "-C", WORKSPACE, "show", f"{start_head}:skills/fix_gate.py"],
                              capture_output=True, text=True)
        if code.returncode == 0:
            gate_src = code.stdout
    if gate_src is None:
        gate_src = open(os.path.join(WORKSPACE, "skills", "fix_gate.py")).read()
    gate_path = os.path.join(HEAL_LOG_DIR, f".gate_{fix_id}.py")
    open(gate_path, "w").write(gate_src)
    args = [sys.executable, gate_path, "--root", WORKSPACE,
            "--start", start_head or "HEAD", "--track", track,
            "--allowed", json.dumps(allowed or [])] + list(GATE_FLAGS)
    if spec.get("skill_name"):
        args += ["--skill", spec["skill_name"]]
    if spec.get("acceptance_cmd"):
        args += ["--acceptance-cmd", spec["acceptance_cmd"]]
    try:
        r = subprocess.run(args, cwd=WORKSPACE, capture_output=True, text=True, timeout=2400)
        out = (r.stdout or "").strip().splitlines()
        gate = json.loads(out[-1]) if out else {"status": "failed", "reason": "gate printed nothing"}
    except Exception as e:
        gate = {"status": "failed", "reason": f"gate did not run: {type(e).__name__}: {e}",
                "checks": []}
    try:
        with open(log_path, "a") as lf:
            lf.write(f"\nGATE: {gate.get('status')} — {gate.get('reason', '')}\n")
            for c in gate.get("checks", []):
                lf.write(f"  {'✓' if c['ok'] else '✗'} {c['check']}: {c['detail'][:300]}\n")
    except Exception:
        pass
    return gate


def _snapshot(fix_id):
    import hashlib
    import shutil
    snap = {}
    d = os.path.join(HEAL_LOG_DIR, f"snap_{fix_id}")
    for rel in SNAPSHOT_FILES:
        src = os.path.join(WORKSPACE, rel)
        if not os.path.exists(src):
            snap[rel] = None
            continue
        dst = os.path.join(d, rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(src, dst)
        snap[rel] = (dst, hashlib.sha256(open(src, "rb").read()).hexdigest())
    return snap


def _snapshot_changed(snap):
    import hashlib
    changed = []
    for rel, v in (snap or {}).items():
        p = os.path.join(WORKSPACE, rel)
        now = hashlib.sha256(open(p, "rb").read()).hexdigest() if os.path.exists(p) else None
        if (v is None and now is not None) or (v is not None and now != v[1]):
            changed.append(rel)
    return changed


def _restore_snapshot(snap, only=None):
    import shutil
    restored = []
    for rel, v in (snap or {}).items():
        if only is not None and rel not in only:
            continue
        p = os.path.join(WORKSPACE, rel)
        if v is None:
            if os.path.exists(p):
                os.remove(p); restored.append(rel)
        else:
            shutil.copy2(v[0], p); restored.append(rel)
    return restored


def rollback(start_head, fix_id, root=None):
    """Undo a failed repair without losing anything: never reset, never
    force. Returns a one-clause description of what was done."""
    root = root or WORKSPACE
    def g(*a):
        return subprocess.run(["git", "-C", root, *a], capture_output=True, text=True)
    if not start_head:
        return "no starting commit recorded, nothing reverted"
    if g("merge-base", "--is-ancestor", start_head, "HEAD").returncode != 0:
        return "history was rewritten, so nothing was reverted automatically — needs a look"
    done = []
    if g("status", "--porcelain", "--untracked-files=no").stdout.strip():
        g("stash", "push", "-m", f"{fix_id} uncommitted changes (gate failed)")
        done.append(f"uncommitted changes stashed as '{fix_id} uncommitted changes'")
    commits = g("rev-list", f"{start_head}..HEAD").stdout.split()
    if commits:
        r = g("revert", "--no-edit", f"{start_head}..HEAD")
        if r.returncode != 0:
            g("revert", "--abort")
            return "the revert hit a conflict and was aborted — needs a look"
        done.append(f"{len(commits)} commit(s) reverted")
    return "; ".join(done) or "nothing to undo"


def _note_state(line):
    """The healer may have written the state note about work that was then
    undone. The runner appends the correction (Heal Rule 2)."""
    # Claude Code keeps per-project memory under a directory named after the
    # workspace path with '/' replaced by '-'.
    p = os.path.expanduser("~/.claude/projects/" + WORKSPACE.replace("/", "-")
                           + "/memory/chip_current_state.md")
    try:
        with open(p, "a") as f:
            f.write(f"\n- {datetime.now():%Y-%m-%d %H:%M} (heal runner) {line}\n")
    except Exception as e:
        print(f"self_heal: could not note state: {e}")


def _finish_job(fix_id, spec, track, gate, cost, result_text, log_path, start_head,
                snap, iterations, note=""):
    """The single terminus for a heal: the gate's status (never the healer's
    own), rollback or switch-off on failure, one result line through the
    outbox, then the next queued job."""
    status = (gate or {}).get("status") or "failed"
    reason = (gate or {}).get("reason") or note or "no gate result"
    what, _claimed, commit = _parse_result(result_text)
    if not what:
        what = _first_sentence(result_text) or "no summary given"
    new_commits = _git_commits_since(start_head)
    commit = commit or (new_commits[0] if new_commits else "")
    undo = ""
    if status == "failed":
        if track == "build":
            try:
                from skills import skill_loader
                if spec.get("skill_name"):
                    skill_loader.set_enabled(spec["skill_name"], False)
                undo = f"skill {spec.get('skill_name')} switched off"
            except Exception as e:
                undo = f"could not switch the skill off: {e}"
            restored = _restore_snapshot(snap, only=_snapshot_changed(snap))
            if restored:
                undo += f"; restored {', '.join(restored)}"
            final_status = "built, not verified" if new_commits else "failed"
        else:
            undo = rollback(start_head, fix_id)
            restored = _restore_snapshot(snap, only=_snapshot_changed(snap))
            if restored:
                undo += f"; restored {', '.join(restored)}"
            final_status = "failed"
            try:
                if RESTART_AFTER_ROLLBACK:
                    subprocess.run([f"{WORKSPACE}/venv/bin/python3",
                                    f"{WORKSPACE}/skills/safe_restart.py"], timeout=900)
            except Exception as e:
                undo += f"; restart after rollback failed: {e}"
        _note_state(f"{fix_id} ({track}) did not pass the gate ({reason}); {undo}. "
                    f"Anything written above about {fix_id} as done does not apply.")
    else:
        final_status = status
    try:
        svc = subprocess.check_output(["systemctl", "is-active", "chip"], text=True,
                                      timeout=10).strip()
    except Exception:
        svc = "unknown"
    fix_index.set_fields(fix_id, gate_status=final_status,
                         gate_report=json.dumps(gate or {})[:20000],
                         cost_usd=round(cost, 4), iterations=iterations,
                         summary=what[:500])
    fix_index.set_status(fix_id, "done" if final_status != "failed" else "failed",
                         note=reason[:300], log_path=os.path.basename(log_path))
    try:
        open(log_path, "a").write(f"\nFINAL: {final_status} — {reason}"
                                  + (f" — {undo}" if undo else "") + f" — ${cost:.2f}\n")
    except Exception:
        pass

    if final_status == "failed" or (status == "failed" and track == "build"):
        try:
            phases = json.loads((fix_index.get(fix_id) or {}).get("phases") or "[]")
        except Exception:
            phases = []
        last = phases[-1]["note"] if phases else "no phase registered"
        text = (f"{fix_id} failed: {what} The gate stopped it ({reason}); {undo}. "
                f"Last phase: {last}. Details: 'fix: status {fix_id}'.")
    else:
        text = (f"{fix_id} done: {what} Status: {final_status}. Commit: {commit or 'none'}."
                + (f" ({undo})" if undo else "")
                + ("" if svc == "active" else f" chip.service is {svc}.")
                + f" Details: 'fix: status {fix_id}'.")
    try:
        from skills import outbox
        if outbox.notify("heal_result", text, urgent=True, source="self_heal",
                         fix_id=fix_id, status=final_status, commit=commit,
                         cost=round(cost, 2)) == "failed":
            _signal_send(f"[Chip] {text}")
    except Exception as e:
        print(f"self_heal: outbox failed ({e}) — sending directly")
        _signal_send(f"[Chip] {text}")

    # Serialized execution: hand off to whatever was approved while we ran.
    try:
        if fix_index.pending_approved() and not fix_index.running():
            ok, detail = _launch_unit()
            nxt = fix_index.pending_approved()[0]
            from skills import outbox
            outbox.notify("heal_queue",
                          f"Started queued heal {nxt}." if ok else
                          f"Queued heal {nxt} failed to launch: {detail}",
                          urgent=not ok, source="self_heal")
    except Exception as e:
        print(f"self_heal: could not chain next job: {e}")


def _git_head(full=False):
    try:
        return subprocess.check_output(["git", "-C", WORKSPACE, "rev-parse",
                                        *([] if full else ["--short"]), "HEAD"],
                                       text=True, timeout=10).strip()
    except Exception:
        return None


def _git_commits_since(start):
    if not start:
        return []
    try:
        out = subprocess.check_output(["git", "-C", WORKSPACE, "log", "--format=%h",
                                       f"{start}..HEAD"], text=True, timeout=10)
        return [l.strip() for l in out.splitlines() if l.strip()]
    except Exception:
        return []


def _parse_result(output):
    """The healer's last 'RESULT: ... | STATUS: ... | COMMIT: ...' line."""
    import re as _re
    for line in reversed((output or "").splitlines()):
        if line.strip().upper().startswith("RESULT:"):
            parts = {k.strip().upper(): v.strip() for k, v in
                     (p.split(":", 1) for p in line.split("|") if ":" in p)}
            status = parts.get("STATUS", "").lower()
            status = next((w for w in ("verified live", "built, not verified", "failed")
                           if w in status), "")
            commit = parts.get("COMMIT", "")
            commit = "" if commit.lower() in ("", "none", "n/a") else commit[:12]
            what = parts.get("RESULT", "").rstrip(".") + "."
            return what, status, commit
    return "", "", ""


def _first_sentence(output):
    text = " ".join((output or "").split())
    if not text:
        return ""
    end = text.find(". ")
    return (text[:end + 1] if 0 < end < 220 else text[:200] + "…")


if __name__ == "__main__":
    if "--run" in sys.argv:
        sys.exit(run_job())
    print(status())
