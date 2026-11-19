"""
dream_cycle.py — Chip Nightly (slim)
============================================
Runs at 0100 daily via cron.

Fact extraction removed — coordinator handles it in real-time every 2500 words.

Nightly:
  - Vault: classify unclassified docs (Gemini Flash + sensitivity filter)
  - Overwatch: Gemini Flash system log review
  - Archive + wipe conversation buffers (gzip, optional Fernet encrypt)

Saturday nights:
  - Profile synthesis: Claude rewrites each user's identity .md from active facts
  - Services manifest rebuild

Encryption:
  DREAM_ENCRYPT_KEY in .env
  python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
"""

import os
import re
import json
import gzip
import shutil
import sqlite3
import requests
import subprocess
import anthropic
from datetime import datetime, date, timedelta
from dotenv import load_dotenv

import os as _os, sys as _sys
_cc = _os.path.dirname(_os.path.abspath(__file__))
while _cc != "/" and not _os.path.exists(_os.path.join(_cc, "chip_config.py")):
    _cc = _os.path.dirname(_cc)
if _cc not in _sys.path:
    _sys.path.insert(0, _cc)
from chip_config import WORKSPACE
BUFFER_DIR  = f"{WORKSPACE}/data/memory"
CONTEXT_DB  = f"{WORKSPACE}/data/memory/chip_context.db"
VAULT_DB    = f"{WORKSPACE}/data/memory/chip_vault.db"
ARCHIVE_DIR = f"{WORKSPACE}/data/memory/archive"
IDENTITY_DIR = f"{WORKSPACE}/config/identities"
OLLAMA_URL  = "http://localhost:11434/api/generate"

load_dotenv(f"{WORKSPACE}/.env")

import chip_config
USERS        = chip_config.USERS          # identity data: config/users.json

SENDER       = chip_config.BOT_NUMBER
SIGNAL_SOCKET = os.getenv("SIGNAL_SOCKET", "/run/signal-cli/signal.sock")
GEMINI_KEY   = os.getenv("GEMINI_API_KEY")
ANTHROPIC_KEY = os.getenv("ANTHROPIC_API_KEY")
CLAUDE_MAX_TOKENS = int(os.getenv("CLAUDE_MAX_TOKENS", "7500"))

from skills import call_logger
ENCRYPT_KEY  = os.getenv("DREAM_ENCRYPT_KEY")
ADMIN_PHONE    = chip_config.ADMIN_PHONE

VALID_DOMAINS      = set(chip_config.VAULT_DOMAINS)
SENSITIVITY_IMMUNE = chip_config.SENSITIVITY_IMMUNE
ALWAYS_SENSITIVE   = chip_config.ALWAYS_SENSITIVE

SENSITIVITY_PATTERNS = [
    r'\b\d{3}-\d{2}-\d{4}\b',
    r'\b(?:paystub|payslip|pay stub|pay slip)\b',
    r'\b(?:W-2|W2|1099|1040|tax return)\b',
    r'\b(?:verification of employment|employment verification)\b',
    r'\b(?:offer letter|onboarding)\b',
    r'\b(?:transcript|academic record)\b',
    r'\b(?:lease agreement|rental agreement)\b',
    r'\b(?:settlement|attorney.client)\b',
    r'\b(?:medical record|diagnosis|prescription)\b',
    r'\b(?:bank statement|account statement|routing number)\b',
    r'\b(?:salary|compensation|annual pay|base pay)\b',
    r'\b(?:background check|background report|profile report)\b',
] + chip_config.EXTRA_SENSITIVITY_PATTERNS

# ─── Model calls ──────────────────────────────────────────────────────────────

def ask_gemini_flash(prompt, timeout=30):
    try:
        url     = (f"https://generativelanguage.googleapis.com/v1beta/models/"
                   f"gemini-3.5-flash:generateContent?key={GEMINI_KEY}")
        payload = {"contents": [{"parts": [{"text": prompt}]}]}
        resp    = requests.post(url, json=payload, timeout=timeout)
        resp.raise_for_status()
        return resp.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
    except Exception as e:
        print(f"⚠️ Gemini Flash error: {e}")
        return ""

def ask_claude(prompt, max_tokens=None):
    client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
    resp   = client.messages.create(
        model="claude-sonnet-5",
        max_tokens=max_tokens or CLAUDE_MAX_TOKENS,
        messages=[{"role": "user", "content": prompt}]
    )
    call_logger.log_model_call(
        model="claude-sonnet-5", thinking=True,
        tokens_in=resp.usage.input_tokens, tokens_out=resp.usage.output_tokens,
        stop_reason=resp.stop_reason, tools=[],
    )
    return "\n".join(b.text for b in resp.content if b.type == "text").strip()

# ─── Signal ───────────────────────────────────────────────────────────────────

def send_signal(text, recipient):
    """Synchronous JSON-RPC send over the signal-cli Unix socket. Replaces
    the old HTTP REST call (localhost:8080/v2/send) — that endpoint has been
    dead since the migration off signal-cli-rest-api/Docker; this script
    just never got updated to match, so nightly Signal notifications from
    here have been silently failing (caught by the try/except below, so the
    rest of the run always completed normally — the report just never
    actually reached Signal)."""
    try:
        import socket
        req = json.dumps({
            "jsonrpc": "2.0", "method": "send", "id": 1,
            "params": {"recipient": [recipient], "message": text}
        }) + "\n"
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(15)
        sock.connect(SIGNAL_SOCKET)
        sock.sendall(req.encode())
        sock.recv(4096)  # ack — content unused, just drains the response
        sock.close()
        print(f"📤 → {recipient[-4:]}: {text[:80]}")
    except Exception as e:
        print(f"❌ Send error: {e}")

# ─── Fact DB operations ───────────────────────────────────────────────────────

def get_active_facts(conn, user=None):
    """Return active confirmed facts, optionally filtered by user."""
    if user:
        return conn.execute(
            "SELECT id, content, created_at FROM facts "
            "WHERE confirmed=1 AND (status='active' OR status IS NULL) "
            "AND (user=? OR user IS NULL) "
            "ORDER BY created_at DESC",
            (user.lower(),)
        ).fetchall()
    return conn.execute(
        "SELECT id, content, created_at FROM facts "
        "WHERE confirmed=1 AND (status='active' OR status IS NULL) "
        "ORDER BY created_at DESC"
    ).fetchall()

def supersede_fact(conn, old_id, new_content, reason, changed_by="dream_cycle"):
    """Archive old fact, insert new one, record history."""
    old = conn.execute("SELECT content FROM facts WHERE id=?", (old_id,)).fetchone()
    if not old:
        return None
    old_content = old[0]

    # Insert new fact
    # Inherit user from old fact
    old_user = conn.execute("SELECT user FROM facts WHERE id=?", (old_id,)).fetchone()
    _user = old_user[0] if old_user else None
    now = datetime.now().isoformat()
    conn.execute(
        "INSERT INTO facts (content, confirmed, created_at, valid_from, status, source, user) "
        "VALUES (?, 1, ?, ?, 'active', ?, ?)",
        (new_content, now, now, changed_by, _user)
    )
    new_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    # Archive old fact
    conn.execute(
        "UPDATE facts SET status='superseded', superseded_by=? WHERE id=?",
        (new_id, old_id)
    )

    # Record history
    conn.execute(
        "INSERT INTO fact_history (fact_id, old_content, new_content, changed_at, change_type, changed_by) "
        "VALUES (?, ?, ?, ?, 'supersede', ?)",
        (old_id, old_content, new_content, datetime.now().isoformat(), changed_by)
    )
    return new_id

def insert_fact(conn, content, source="dream_cycle", user=None):
    """Insert new active fact."""
    now = datetime.now().isoformat()
    conn.execute(
        "INSERT INTO facts (content, confirmed, created_at, valid_from, status, source, user) "
        "VALUES (?, 1, ?, ?, 'active', ?, ?)",
        (content, now, now, source, user)
    )
    conn.execute(
        "INSERT INTO fact_history (fact_id, old_content, new_content, changed_at, change_type, changed_by) "
        "VALUES (NULL, NULL, ?, ?, 'insert', ?)",
        (content, now, source)
    )

def archive_fact(conn, fact_id, reason, changed_by="dream_cycle"):
    """Archive a fact that is no longer relevant."""
    old = conn.execute("SELECT content FROM facts WHERE id=?", (fact_id,)).fetchone()
    if not old:
        return
    conn.execute("UPDATE facts SET status='archived' WHERE id=?", (fact_id,))
    conn.execute(
        "INSERT INTO fact_history (fact_id, old_content, new_content, changed_at, change_type, changed_by) "
        "VALUES (?, ?, ?, ?, 'archive', ?)",
        (fact_id, old[0], reason, datetime.now().isoformat(), changed_by)
    )

# ─── Apply operations to DB ───────────────────────────────────────────────────

_current_user = None  # set by caller before apply_operations

def apply_operations(operations, conn):
    """Execute list of operations from Claude consolidation pass."""
    inserted   = []
    superseded = []
    archived   = []
    skipped    = []

    for op in operations:
        action = op.get("op")
        try:
            if action == "insert":
                insert_fact(conn, op["content"], user=_current_user)
                inserted.append(op["content"])

            elif action == "supersede":
                old_id      = op["old_id"]
                new_content = op["new_content"]
                supersede_fact(conn, old_id, new_content, "claude_consolidation")
                superseded.append((old_id, new_content))

            elif action == "archive":
                archive_fact(conn, op["old_id"], op.get("reason", "dream_cycle"))
                archived.append(op["old_id"])

            elif action == "skip":
                skipped.append(op.get("content", ""))

        except Exception as e:
            print(f"⚠️ Operation error ({action}): {e}")

    conn.commit()
    return inserted, superseded, archived, skipped

# ─── Weekly profile synthesis ─────────────────────────────────────────────────

# Per-user synthesis configs — built from config/users.json (19 Nov 2026):
# phones, names and the personal prompt_focus text no longer live in code.
PROFILE_CONFIGS = {
    u["name"].lower(): {
        "phone":         phone,
        "identity_path": f"{IDENTITY_DIR}/{u['name'].lower()}.md",
        "prompt_focus":  u.get("profile_focus",
                               f"Capture who {u['name']} is: role, interests, "
                               f"active projects, recent decisions."),
    }
    for phone, u in chip_config.USERS_FULL.items()
}

def synthesize_profile_for(name, conn):
    """Synthesize and write identity file for one user."""
    cfg = PROFILE_CONFIGS.get(name.lower())
    if not cfg:
        return 0

    os.makedirs(IDENTITY_DIR, exist_ok=True)
    identity_path = cfg["identity_path"]

    active_facts = get_active_facts(conn, user=name.lower())
    if not active_facts:
        print(f"  No active facts for {name}.")
        return 0

    facts_str = "\n".join(f"- {content}" for _, content, _ in active_facts)
    existing  = open(identity_path).read() if os.path.exists(identity_path) else ""

    prompt = (
        f"Update the identity file for {name.title()} for their family AI assistant (Chip).\n"
        f"This is injected as context in AI conversations. Concise, factual, current.\n"
        f"Third person. Plain prose. No markdown. 2-3 paragraphs max.\n\n"
        f"FOCUS: {cfg['prompt_focus']}\n\n"
        f"ACTIVE FACTS:\n{facts_str}\n\n"
        f"PREVIOUS VERSION (update, don't copy):\n{existing[:2000] if existing else '(none yet)'}"
    )

    try:
        call_logger.set_trace(f"dream-{date.today().isoformat()}", name)
        new_content = ask_claude(prompt)

        conn.execute(
            "INSERT INTO profile_history (generated_at, content, fact_count) VALUES (?, ?, ?)",
            (datetime.now().isoformat(), existing, len(active_facts))
        )
        conn.commit()

        open(identity_path, "w").write(new_content)
        print(f"  ✅ {name}.md updated ({len(active_facts)} facts → {len(new_content)} chars)")
        return len(active_facts)

    except Exception as e:
        print(f"⚠️ Profile synthesis error for {name}: {e}")
        return 0

def synthesize_profile(conn):
    """Run Saturday synthesis for all users."""
    total = 0
    for name in PROFILE_CONFIGS:
        print(f"  Synthesizing {name}.md...")
        total += synthesize_profile_for(name, conn)
    return total

# ─── Vault classification ─────────────────────────────────────────────────────

def is_sensitive(domain, sample_text):
    if domain in ALWAYS_SENSITIVE:
        return True
    if domain in SENSITIVITY_IMMUNE:
        return False
    text_lower = sample_text.lower()
    for pattern in SENSITIVITY_PATTERNS:
        if re.search(pattern, text_lower, re.IGNORECASE):
            return True
    return False

def classify_unclassified():
    conn  = sqlite3.connect(VAULT_DB)
    rows  = conn.execute(
        "SELECT id, filename FROM documents WHERE domain='unclassified'"
    ).fetchall()
    classified = []
    for doc_id, filename in rows:
        samples = conn.execute(
            "SELECT chunk_text FROM chunks WHERE document_id=? "
            "ORDER BY chunk_index LIMIT 3", (doc_id,)
        ).fetchall()
        if not samples:
            continue
        sample_text = "\n".join(s[0] for s in samples)

        prompt = (
            f"Classify this document into ONE domain:\n"
            + "".join(f"  {d:<12} — {desc}\n" for d, desc in chip_config.VAULT_DOMAINS.items())
            + "\n"
            + f"Output only the single domain word.\n\n"
            f"FILENAME: {filename}\n\nFIRST CONTENT:\n{sample_text[:3000]}"
        )
        result = ask_gemini_flash(prompt, timeout=60)
        result = result.lower().strip().split()[0] if result else "general"
        if result not in VALID_DOMAINS:
            result = "general"

        sensitive = 1 if is_sensitive(result, sample_text) else 0
        conn.execute(
            "UPDATE documents SET domain=?, sensitive=? WHERE id=?",
            (result, sensitive, doc_id)
        )
        conn.execute(
            "UPDATE chunks SET domain=?, sensitive=? WHERE document_id=?",
            (result, sensitive, doc_id)
        )
        sens_flag = " 🔒" if sensitive else ""
        classified.append(f"{filename} → {result}{sens_flag}")
        print(f"📑 {filename} → {result}{sens_flag}")

    conn.commit()
    conn.close()
    return classified

# ─── Services Manifest ───────────────────────────────────────────────────────

SERVICES_MANIFEST = f"{WORKSPACE}/data/chip_services.md"

KNOWN_SERVICES = [
    {
        "unit":     "chip.service",
        "type":     "persistent",
        "expected": "always running",
        "skill":    "coordinator.py",
        "desc":     "Main coordinator — Signal polling, routing, cloud API dispatch",
    },
    {
        "unit":     "pdf_watcher.service",
        "type":     "persistent",
        "expected": "always running",
        "skill":    "skills/pdf_watcher.py",
        "desc":     "Monitors syncthing for new PDFs, rasterizes and embeds pages",
    },
    {
        "unit":     "ollama",
        "type":     "persistent",
        "expected": "always running",
        "skill":    "-",
        "desc":     "Local embeddings (nomic-embed-text) and the optional local chat model",
    },
    {
        "unit":     "dream_cycle (cron)",
        "type":     "cron",
        "expected": "runs at 0100, not persistent",
        "skill":    "dream_cycle.py",
        "desc":     "Nightly fact extraction, vault classification, overwatch, profile synthesis",
    },
    {
        "unit":     "vitals (cron)",
        "type":     "cron",
        "expected": "runs every 5 min, not persistent",
        "skill":    "skills/vitals.py",
        "desc":     "Thermal guard — pauses/resumes ollama based on CPU temp thresholds",
    },
]

def _read_skill_header(skill_path, lines=25):
    full = f"{WORKSPACE}/{skill_path}"
    try:
        with open(full) as f:
            return "".join(f.readlines()[:lines])
    except Exception:
        return "(file not found)"

def generate_services_manifest():
    """Generate chip_services.md — called Saturday nights in dream cycle."""
    print("📋 Generating chip_services.md...")
    ts    = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [f"# Chip Services Manifest\nGenerated: {ts}\n"]

    for svc in KNOWN_SERVICES:
        lines.append(f"## {svc['unit']}")
        lines.append(f"- **Type:** {svc['type']}")
        lines.append(f"- **Expected:** {svc['expected']}")
        lines.append(f"- **Description:** {svc['desc']}")
        header = _read_skill_header(svc["skill"])
        lines.append("\n```python\n" + header + "\n```\n")

    manifest = "\n".join(lines)
    os.makedirs(os.path.dirname(SERVICES_MANIFEST), exist_ok=True)
    with open(SERVICES_MANIFEST, "w") as f:
        f.write(manifest)
    print(f"✅ chip_services.md written ({len(manifest)} chars)")

# ─── Overwatch ────────────────────────────────────────────────────────────────

def run_overwatch():
    try:
        logs = subprocess.check_output(
            ["journalctl", "-p", "4", "--no-pager",
             "--since", "24 hours ago"],
            timeout=10,
        ).decode("utf-8")
    except Exception as e:
        return f"Log retrieval error: {e}"
    if not logs.strip():
        return "NOMINAL"
    services_ctx = ""
    try:
        with open(SERVICES_MANIFEST) as f:
            services_ctx = f.read()[:3000]
    except Exception:
        pass
    prompt = "Security analyst reviewing Raspberry Pi home server logs.\n"
    if services_ctx:
        prompt += f"KNOWN SERVICES AND EXPECTED STATES:\n{services_ctx}\n\n"
    prompt += (
        "Routine (UFW multicast, normal cron, standard restarts, "
        "known services behaving as expected) → respond exactly: NOMINAL\n"
        "Flag only: SSH brute force, repeated auth failures from unknown IPs, "
        "unexpected service crashes, unusual outbound connections.\n"
        "If flagging, give a 1-2 sentence plain-text summary. "
        "Be conservative — do not flag local sudo noise, known cron activity, "
        "or normal firewall entries.\n"
        f"LOGS:\n{logs[:20000]}"
    )
    result = ask_gemini_flash(prompt, timeout=60)
    return result if result else "Overwatch unavailable."

# ─── Archive & encrypt buffer ─────────────────────────────────────────────────

def archive_buffer(name, buf_path):
    os.makedirs(ARCHIVE_DIR, exist_ok=True)
    ts      = datetime.now().strftime("%Y%m%d_%H%M%S")
    gz_name = f"{ARCHIVE_DIR}/buffer_{name.lower()}_{ts}.txt.gz"

    with open(buf_path, "rb") as f_in:
        with gzip.open(gz_name, "wb") as f_out:
            shutil.copyfileobj(f_in, f_out)

    if ENCRYPT_KEY:
        try:
            from cryptography.fernet import Fernet
            fernet    = Fernet(ENCRYPT_KEY.encode())
            data      = open(gz_name, "rb").read()
            encrypted = fernet.encrypt(data)
            enc_name  = gz_name + ".enc"
            open(enc_name, "wb").write(encrypted)
            os.remove(gz_name)
            print(f"🔒 {os.path.basename(enc_name)}")
        except Exception as e:
            print(f"⚠️ Encryption failed: {e}")

    # Prune archives > 30 days
    cutoff = datetime.now() - timedelta(days=30)
    pruned = 0
    for fname in os.listdir(ARCHIVE_DIR):
        fpath = os.path.join(ARCHIVE_DIR, fname)
        if os.path.isfile(fpath):
            if datetime.fromtimestamp(os.path.getmtime(fpath)) < cutoff:
                os.remove(fpath)
                pruned += 1
    if pruned:
        print(f"🗑️ Pruned {pruned} old archive(s).")

# ─── Buffer message count ─────────────────────────────────────────────────────

def count_buffer_msgs(name, buf_path):
    """Count user messages in buffer without any API call."""
    if not os.path.exists(buf_path):
        return 0
    with open(buf_path) as f:
        return sum(1 for line in f
                   if f"{name}:" in line and "Chip:" not in line)

# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    print(f"\n🌙 Dream Cycle (slim) starting at {datetime.now().isoformat()}")
    today    = date.today()
    saturday = today.weekday() == 5  # 0=Monday, 5=Saturday

    conn = sqlite3.connect(CONTEXT_DB)

    # 1. Vault classification
    print("\n📑 Classifying unclassified docs...")
    classified = classify_unclassified()

    # 2. Overwatch
    print("\n🛡️ Running overwatch...")
    overwatch = run_overwatch()

    # 3. Weekly profile synthesis (Saturday nights)
    profile_updated = 0
    if saturday:
        print("\n📝 Saturday synthesis — rewriting identity files...")
        profile_updated = synthesize_profile(conn)
        generate_services_manifest()

    # 3.5 Memory & disk maintenance (19 Nov 2026): fact expiry/dedup,
    # conversation-turn pruning, .bak tidy-up, DB vacuum. Deterministic only.
    # NOTE: the buffer wipe below no longer causes amnesia — history lives in
    # chip_history.db with a rolling summary; buffers are just the archive.
    print("\n🧹 Memory maintenance...")
    try:
        from skills.memory_maint import run_maintenance
        maint_report = run_maintenance()
    except Exception as e:
        maint_report = f"Memory maintenance failed: {e}"
        print(f"⚠️ {maint_report}")

    # 4. Count activity + archive buffers
    print("\n📦 Archiving buffers...")
    activity = []
    for phone, info in USERS.items():
        name     = info["name"]
        buf_path = f"{BUFFER_DIR}/buffer_{name.lower()}.txt"
        msgs     = count_buffer_msgs(name, buf_path)
        if msgs > 0:
            activity.append(f"• {name}: {msgs} msgs")
        if os.path.exists(buf_path) and os.path.getsize(buf_path) > 0:
            archive_buffer(name, buf_path)
            open(buf_path, "w").close()

    combined = f"{BUFFER_DIR}/chat_buffer.txt"
    if os.path.exists(combined):
        open(combined, "w").close()

    active_count = conn.execute(
        "SELECT COUNT(*) FROM facts WHERE confirmed=1 AND status='active'"
    ).fetchone()[0]
    conn.close()

    # 5. Signal report
    msg_parts = ["🌙 Dream Cycle\n"]

    if "NOMINAL" in overwatch.upper():
        msg_parts.append("🛡️ Overwatch: NOMINAL")
    else:
        msg_parts.append(f"🛡️ ⚠️ {overwatch}")

    if classified:
        msg_parts.append(f"\n📑 Classified {len(classified)} doc(s):")
        msg_parts.extend(f"• {c}" for c in classified[:6])
        if len(classified) > 6:
            msg_parts.append(f"  ...and {len(classified)-6} more")

    if profile_updated:
        msg_parts.append(f"\n📝 Identity files rebuilt ({', '.join(PROFILE_CONFIGS)}) from {profile_updated} active facts.")

    if activity:
        msg_parts.append("\n👥 Activity:")
        msg_parts.extend(activity)

    # Scheduled jobs that need a human (19 Nov 2026): 3+ failures in a row, or
    # no successful run within three expected intervals. Reported unprompted.
    try:
        from skills.scheduler import registry_problems
        _problems = registry_problems()
        if _problems:
            msg_parts.append("\n⏱️ Scheduled jobs needing attention:")
            msg_parts.extend(f"  • {p}" for p in _problems)
    except Exception as _e:
        msg_parts.append(f"\n⏱️ Could not check scheduled jobs: {_e}")

    msg_parts.append(f"\n📊 Active memory: {active_count} facts")
    msg_parts.append("Use memory: to correct anything.")

    send_signal("\n".join(msg_parts), ADMIN_PHONE)

    # 6. Log run
    db = sqlite3.connect(CONTEXT_DB)
    db.execute(
        "INSERT INTO dream_log (run_date, facts_found, status, notes) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(run_date) DO UPDATE SET "
        "facts_found=excluded.facts_found, status=excluded.status, notes=excluded.notes",
        (
            today.isoformat(), 0, "complete",
            f"classified={len(classified)}, overwatch={overwatch[:50]}, "
            f"saturday_synthesis={'yes' if saturday else 'no'}, "
            f"encrypted={'yes' if ENCRYPT_KEY else 'no'}"
        )
    )
    db.commit()
    db.close()

    print(f"\n✅ Dream Cycle (slim) complete: {len(classified)} docs classified.\n")

if __name__ == "__main__":
    main()
