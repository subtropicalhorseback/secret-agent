"""
skills/taxonomy.py
====================
Lightweight real-time taxonomy tagging + shift-triggered deep extraction.

Design (per the admin, June 2026):
  - phi4-mini is fast/cheap and good at CLASSIFICATION, not inference. It tags
    every message in real time with domain-subdomain-topic and that's it.
  - Sonnet is slower/costlier but good at INFERENCE. It runs the actual fact
    extraction, triggered either by the existing word-count threshold OR by
    a detected domain shift (e.g. cooking -> finance), whichever comes first.
  - Culinary threads that converge on a final recipe (ingredients + method)
    also get extracted into a separate chip_recipes.db — only the final
    version, not the exploration steps along the way.

Two SQLite DBs owned by this module:
  chip_taxonomy.db  — message_tags table (domain/subdomain/topic per message)
  chip_recipes.db   — recipes table (final recipe artifacts)

This module is intentionally self-contained (no import of coordinator.py) to
avoid circular imports. It re-implements the small pieces of coordinator
logic it needs (existing-facts lookup, fact commit) rather than importing
them.
"""

import os
import re
import json
import sqlite3
import asyncio
import requests
from datetime import datetime, timedelta

import os as _os, sys as _sys
_cc = _os.path.dirname(_os.path.abspath(__file__))
while _cc != "/" and not _os.path.exists(_os.path.join(_cc, "chip_config.py")):
    _cc = _os.path.dirname(_cc)
if _cc not in _sys.path:
    _sys.path.insert(0, _cc)
from chip_config import WORKSPACE
TAXONOMY_DB = f"{WORKSPACE}/data/memory/chip_taxonomy.db"
RECIPES_DB  = f"{WORKSPACE}/data/memory/chip_recipes.db"
CONTEXT_DB  = f"{WORKSPACE}/data/memory/chip_context.db"
BUFFER_DIR  = f"{WORKSPACE}/data/memory"

OLLAMA_URL    = os.getenv("OLLAMA_API_URL", "http://localhost:11434")
ANTHROPIC_KEY = os.getenv("ANTHROPIC_API_KEY")
ROUTER_MODEL  = "phi4-mini"
SONNET_MODEL  = "claude-opus-5"   # fact extraction on the premier model (19 Nov 2026)
CLAUDE_MAX_TOKENS = int(os.getenv("CLAUDE_MAX_TOKENS", "7500"))

from skills import call_logger

# Per-message tagging must never hang the pipeline. Short timeout, silent skip on failure.
TAG_TIMEOUT_SECONDS = 60

# How many of a user's most recent tags to consider "the current conversation"
# when deciding if a new message represents a domain shift.
SHIFT_WINDOW = 5

# In-memory recent-domain cache per user, avoids a DB read on every message.
# Format: {user: [domain, domain, ...]} most recent last.
_recent_domains = {}

# ─── DB init ──────────────────────────────────────────────────────────────────

def _init_taxonomy_db():
    os.makedirs(os.path.dirname(TAXONOMY_DB), exist_ok=True)
    conn = sqlite3.connect(TAXONOMY_DB)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS message_tags (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            user      TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            domain    TEXT NOT NULL,
            subdomain TEXT,
            topic     TEXT,
            excerpt   TEXT,
            source    TEXT
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tags_user_time ON message_tags(user, timestamp)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tags_user_domain ON message_tags(user, domain)")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS pending_work (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            kind            TEXT NOT NULL,
            sender          TEXT NOT NULL,
            name            TEXT NOT NULL,
            user_text       TEXT,
            response_text   TEXT,
            source          TEXT,
            status          TEXT NOT NULL DEFAULT 'pending',
            attempts        INTEGER NOT NULL DEFAULT 0,
            created_at      TEXT NOT NULL,
            last_attempt_at TEXT,
            error           TEXT
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_pending_status ON pending_work(status, created_at)")
    conn.commit()
    conn.close()

def _init_recipes_db():
    os.makedirs(os.path.dirname(RECIPES_DB), exist_ok=True)
    conn = sqlite3.connect(RECIPES_DB)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS recipes (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            user            TEXT NOT NULL,
            title           TEXT NOT NULL,
            ingredients     TEXT NOT NULL,
            method          TEXT NOT NULL,
            tags            TEXT,
            source_excerpt  TEXT,
            created_at      TEXT NOT NULL
        )
    """)
    conn.commit()
    conn.close()

_init_taxonomy_db()
_init_recipes_db()

# ─── Durable work queue (heartbeat-driven, replaces fire-and-forget tasks) ───
# Real-time tagging + fact extraction used to be asyncio.create_task() fire-
# and-forget calls at 4 call sites in coordinator.py. Any of them dying to a
# phi4/Ollama timeout under router contention just silently vanished — no
# row, no retry, no trace. This queue makes that durable: call sites do a
# cheap synchronous insert instead, and a single heartbeat loop (started
# alongside reminder_checker in coordinator.main()) processes one row at a
# time, which also naturally throttles against phi4 contention rather than
# stacking concurrent fire-and-forget tasks that fight the router for the
# same model.

HEARTBEAT_INTERVAL_SECONDS = 12
MAX_ATTEMPTS                = 3
RETRY_BACKOFF_SECONDS       = 120  # don't immediately re-hammer a failed row

def enqueue_work(kind, sender, name, user_text="", response_text="", source=""):
    """Durable, synchronous insert — replaces asyncio.create_task() at the
    four real-time tagging/extraction call sites. kind is 'tag_extract'
    (-> tag_and_maybe_extract) or 'threshold_extract' (-> run_fact_extraction,
    which reads its own recent-buffer and needs no text params)."""
    try:
        conn = sqlite3.connect(TAXONOMY_DB)
        conn.execute(
            "INSERT INTO pending_work "
            "(kind, sender, name, user_text, response_text, source, status, attempts, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 'pending', 0, ?)",
            (kind, sender, name, user_text, response_text, source, datetime.now().isoformat())
        )
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"⚠️ enqueue_work error: {e}")

def _get_next_pending_row():
    cutoff = (datetime.now() - timedelta(seconds=RETRY_BACKOFF_SECONDS)).isoformat()
    conn = sqlite3.connect(TAXONOMY_DB)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM pending_work WHERE status='pending' "
        "AND (last_attempt_at IS NULL OR last_attempt_at <= ?) "
        "ORDER BY created_at ASC LIMIT 1",
        (cutoff,)
    ).fetchone()
    conn.close()
    return dict(row) if row else None

def _mark_attempt(row_id):
    conn = sqlite3.connect(TAXONOMY_DB)
    conn.execute(
        "UPDATE pending_work SET attempts = attempts + 1, last_attempt_at = ? WHERE id = ?",
        (datetime.now().isoformat(), row_id)
    )
    conn.commit()
    conn.close()

def _mark_done(row_id):
    conn = sqlite3.connect(TAXONOMY_DB)
    conn.execute("UPDATE pending_work SET status='done' WHERE id=?", (row_id,))
    conn.commit()
    conn.close()

def _mark_failed(row_id, error):
    conn = sqlite3.connect(TAXONOMY_DB)
    conn.execute("UPDATE pending_work SET status='failed', error=? WHERE id=?",
                 (str(error)[:500], row_id))
    conn.commit()
    conn.close()

# ─── Classification (phi4-mini, fast/cheap, every message) ───────────────────

def _call_classify(user_text, response_text):
    """Classification moved from phi4-mini to Gemini 3.5 Flash-Lite
    (19 Nov 2026): better JSON discipline (no more bare-word repair),
    no contention with the embedding model on the Pi, ~$0.0002/tag."""
    from skills import llm as _llm
    combined = f"USER: {user_text[:500]}\nRESPONSE: {response_text[:500]}"
    prompt = (
        "Classify this exchange into a taxonomy. Return ONLY JSON, no markdown:\n"
        '{"domain": "...", "subdomain": "...", "topic": "..."}\n'
        "Rules: one lowercase word per field (hyphenate if truly needed). "
        f"domain = broad life area (e.g. {__import__('chip_config').TAXONOMY_EXAMPLES}). "
        "subdomain = narrower category "
        "within that domain. topic = specific subject of this exchange.\n\n"
        f"{combined}"
    )
    return _llm.gemini_sync(prompt, model=_llm.GLITE, json_mode=True,
                            timeout=TAG_TIMEOUT_SECONDS)

async def classify_taxonomy(user_text, response_text="", source="text"):
    """
    Classify one exchange. Returns {"domain":..,"subdomain":..,"topic":..} or
    None on any failure (timeout, bad JSON, etc.) — tagging is best-effort and
    must never raise into the caller.
    """
    if not user_text and not response_text:
        return None
    try:
        raw = await asyncio.wait_for(
            asyncio.to_thread(_call_classify, user_text, response_text),
            timeout=TAG_TIMEOUT_SECONDS + 2,
        )
        raw = raw.replace("```json", "").replace("```", "").strip()
        m = re.search(r'\{.*\}', raw, re.DOTALL)
        if m:
            raw = m.group(0)
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            # phi4 occasionally emits bare-word values ({"domain": home}) —
            # quote them and retry before giving up.
            repaired = re.sub(r':\s*([A-Za-z][A-Za-z0-9 \-]*?)\s*([,}])',
                              r': "\1"\2', raw)
            data = json.loads(repaired)

        def _slug(v):
            # Whitespace -> hyphen FIRST, then strip disallowed chars, so
            # multi-word topics hyphenate instead of concatenating.
            v = re.sub(r'\s+', '-', str(v).strip().lower())
            return re.sub(r'[^a-z0-9\-]', '', v)[:30]

        domain    = _slug(data.get("domain", ""))
        subdomain = _slug(data.get("subdomain", ""))
        topic     = _slug(data.get("topic", ""))
        if not domain:
            return None
        return {"domain": domain, "subdomain": subdomain, "topic": topic}
    except Exception as e:
        print(f"⚠️ Taxonomy classify skipped: {e}")
        return None

def _save_tag(user, tag, excerpt, source):
    try:
        conn = sqlite3.connect(TAXONOMY_DB)
        conn.execute(
            "INSERT INTO message_tags (user, timestamp, domain, subdomain, topic, excerpt, source) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (user, datetime.now().isoformat(), tag["domain"], tag["subdomain"],
             tag["topic"], excerpt[:200], source)
        )
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"⚠️ Tag save error: {e}")

# ─── Shift detection ───────────────────────────────────────────────────────────

def _check_and_record_shift(user, new_domain):
    """
    Compare new_domain against the user's recent domain window.
    Returns True (major shift) if new_domain hasn't appeared in the window.
    Updates the window regardless.
    """
    window = _recent_domains.setdefault(user, [])
    is_shift = bool(window) and new_domain not in window

    window.append(new_domain)
    if len(window) > SHIFT_WINDOW:
        window.pop(0)

    return is_shift

# ─── Buffer reading (mirrors coordinator's maybe_extract_facts buffer pull) ──

def _read_recent_buffer(name, max_words=3000):
    buffer_path = f"{BUFFER_DIR}/buffer_{name}.txt"
    try:
        with open(buffer_path) as f:
            lines = f.readlines()
    except Exception:
        return ""
    chunk_lines = []
    word_count  = 0
    for line in reversed(lines):
        chunk_lines.insert(0, line)
        word_count += len(line.split())
        if word_count >= max_words:
            break
    return "".join(chunk_lines).strip()

def _existing_facts_for(name, query_hint):
    """Lightweight local reimplementation of coordinator's retrieve_facts_rag,
    to avoid a circular import. Same table, same logic, simplified."""
    try:
        conn = sqlite3.connect(CONTEXT_DB)
        rows = conn.execute(
            "SELECT content FROM facts WHERE confirmed=1 AND (user=? OR user IS NULL) "
            "ORDER BY created_at DESC LIMIT 60",
            (name,)
        ).fetchall()
        conn.close()
        if not rows:
            return ""
        clean  = re.sub(r'[^\w\s]', ' ', query_hint)
        tokens = [t for t in clean.split() if len(t) > 2]
        if tokens:
            matching = [r[0] for r in rows if any(t in r[0].lower() for t in tokens)]
        else:
            matching = [r[0] for r in rows[:8]]
        return "\n".join(matching[:8])
    except Exception:
        return ""

# ─── Core extraction (Sonnet — inference, not classification) ────────────────

def _call_sonnet_extract(prompt):
    import anthropic
    client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
    resp = client.messages.create(
        model=SONNET_MODEL, max_tokens=CLAUDE_MAX_TOKENS,
        messages=[{"role": "user", "content": prompt}],
    )
    call_logger.log_model_call(
        model=SONNET_MODEL, thinking=True,
        tokens_in=resp.usage.input_tokens, tokens_out=resp.usage.output_tokens,
        stop_reason=resp.stop_reason, tools=[],
    )
    return "\n".join(b.text for b in resp.content if b.type == "text").strip()

def _commit_facts(name, raw):
    """Parse INSERT:/SUPERSEDE: lines and commit to CONTEXT_DB. Returns list of summaries."""
    conn     = sqlite3.connect(CONTEXT_DB)
    now      = datetime.now().isoformat()
    inserted = []
    try:
        for line in raw.splitlines():
            line = line.strip()
            if line.startswith("INSERT:"):
                fact = line[7:].strip()
                if fact and len(fact) > 10:
                    conn.execute(
                        "INSERT INTO facts (content, confirmed, created_at, valid_from, status, source, user) "
                        "VALUES (?, 1, ?, ?, 'active', 'realtime', ?)",
                        (fact, now, now, name)
                    )
                    inserted.append(f"+ {fact[:60]}")
            elif line.startswith("SUPERSEDE:"):
                parts = line[10:].split("->")
                if len(parts) == 2:
                    old_text = parts[0].strip()
                    new_fact = parts[1].strip()
                    if not new_fact or len(new_fact) < 5:
                        continue
                    old_row = conn.execute(
                        "SELECT id FROM facts WHERE user=? AND status='active' AND content LIKE ? LIMIT 1",
                        (name, f"%{old_text[:40]}%")
                    ).fetchone()
                    if old_row:
                        conn.execute(
                            "UPDATE facts SET status='superseded', valid_until=? WHERE id=?",
                            (now, old_row[0])
                        )
                    conn.execute(
                        "INSERT INTO facts (content, confirmed, created_at, valid_from, status, source, user) "
                        "VALUES (?, 1, ?, ?, 'active', 'realtime', ?)",
                        (new_fact, now, now, name)
                    )
                    inserted.append(f"↻ {new_fact[:60]}")
        conn.commit()
    finally:
        conn.close()
    return inserted

def _commit_recipe(name, recipe_json, source_excerpt):
    try:
        data = json.loads(recipe_json)
        title       = str(data.get("title", "")).strip()
        ingredients = data.get("ingredients", [])
        method      = data.get("method", [])
        tags        = str(data.get("tags", "")).strip()
        if not title or not ingredients or not method:
            return None
        conn = sqlite3.connect(RECIPES_DB)
        conn.execute(
            "INSERT INTO recipes (user, title, ingredients, method, tags, source_excerpt, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (name, title, json.dumps(ingredients), json.dumps(method),
             tags, source_excerpt[:300], datetime.now().isoformat())
        )
        conn.commit()
        conn.close()
        return title
    except Exception as e:
        print(f"⚠️ Recipe commit error: {e}")
        return None

async def run_fact_extraction(sender, name, reason="threshold", domain_hint=""):
    """
    Pull recent buffer, run Sonnet extraction, commit facts. If domain_hint
    is 'culinary' (or contains it), also asks for a RECIPE: block and commits
    to chip_recipes.db if the conversation converged on a final version.
    reason is just for logging ("threshold" | "shift").
    """
    try:
        chunk = _read_recent_buffer(name)
        if not chunk:
            return

        existing_facts = _existing_facts_for(name, chunk[:400])
        is_culinary    = "culinary" in (domain_hint or "").lower()

        recipe_instruction = (
            "\n\nIf this conversation arrived at a FINAL recipe (a complete "
            "ingredients list and method, not just exploration or 'what if' "
            "questions), also output one line:\n"
            'RECIPE: {"title":"...","ingredients":["..."],"method":["..."],"tags":"culinary-subdomain-topic"}\n'
            "Only the FINAL converged version — not intermediate drafts. "
            "If no final recipe was reached, omit this line entirely."
            if is_culinary else ""
        )

        prompt = (
            f"You are updating the personal memory for {name.title()}.\n\n"
            f"EXISTING RELEVANT FACTS:\n{existing_facts or '(none yet)'}\n\n"
            f"RECENT CONVERSATION:\n{chunk[:8000]}\n\n"
            f"Extract durable personal facts from this conversation. For each fact:\n"
            f"- New fact not in existing: output 'INSERT: <fact>'\n"
            f"- Updates/corrects an existing fact: output 'SUPERSEDE: <old fact text> -> <new fact>'\n"
            f"- Nothing new or durable: output nothing\n\n"
            f"EXCLUDE: AI commands, task outputs, transient moods, anything about using this AI.\n"
            f"INCLUDE: personal decisions, relationships, events, preferences, status changes, "
            f"and anything that would help recall this conversation later.\n"
            f"Be generous — when in doubt, extract it. Only extract facts that would still matter in 3 months.\n"
            f"{recipe_instruction}\n\n"
            f"If nothing at all to extract, output exactly: NONE"
        )

        raw = await asyncio.to_thread(_call_sonnet_extract, prompt)

        if not raw or raw.upper().startswith("NONE"):
            print(f"  [{name}] No new facts ({reason} trigger)")
            return

        recipe_line = None
        fact_lines  = []
        for line in raw.splitlines():
            if line.strip().startswith("RECIPE:"):
                recipe_line = line.strip()[7:].strip()
            else:
                fact_lines.append(line)

        inserted = _commit_facts(name, "\n".join(fact_lines))
        if inserted:
            print(f"  [{name}] {len(inserted)} facts ({reason}): {', '.join(inserted[:3])}")

        if recipe_line:
            title = _commit_recipe(name, recipe_line, chunk[-300:])
            if title:
                print(f"  [{name}] Recipe saved: {title}")

    except Exception as e:
        print(f"⚠️ run_fact_extraction error ({reason}): {e}")

# ─── Public entry point — called once per message from coordinator ──────────

async def tag_and_maybe_extract(sender, name, user_text, response_text, source="text"):
    """
    Call once per message (text or vision). Tags the exchange, checks for a
    domain shift, and fires Sonnet extraction if a major shift is detected.
    Fully best-effort — any failure here is logged and swallowed, never
    raised, since this must not affect message delivery.
    """
    try:
        tag = await classify_taxonomy(user_text, response_text, source=source)
        if not tag:
            return

        excerpt = (user_text or response_text)[:200]
        _save_tag(name, tag, excerpt, source)

        is_shift = _check_and_record_shift(name, tag["domain"])
        if is_shift:
            print(f"🔀 Domain shift for {name}: -> {tag['domain']} — triggering extraction")
            await run_fact_extraction(sender, name, reason="shift", domain_hint=tag["domain"])

    except Exception as e:
        print(f"⚠️ tag_and_maybe_extract error: {e}")

# ─── Heartbeat — single background consumer of the durable queue ────────────

async def taxonomy_heartbeat():
    """Started once in coordinator.main() alongside reminder_checker. Ticks
    every HEARTBEAT_INTERVAL_SECONDS, processes ONE pending row per tick —
    deliberately serial, since that's the throttle against phi4/Ollama
    contention with the router's own classify calls."""
    print("🫀 Taxonomy heartbeat online.")
    while True:
        await asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS)
        try:
            row = _get_next_pending_row()
            if not row:
                continue
            _mark_attempt(row["id"])
            try:
                if row["kind"] == "tag_extract":
                    await tag_and_maybe_extract(
                        row["sender"], row["name"], row["user_text"], row["response_text"],
                        source=row["source"] or "text",
                    )
                elif row["kind"] == "threshold_extract":
                    await run_fact_extraction(row["sender"], row["name"], reason="threshold")
                else:
                    raise ValueError(f"unknown kind: {row['kind']}")
                _mark_done(row["id"])
            except Exception as e:
                attempts_now = row["attempts"] + 1
                if attempts_now >= MAX_ATTEMPTS:
                    _mark_failed(row["id"], e)
                    print(f"⚠️ pending_work #{row['id']} dead-lettered after {attempts_now} attempts: {e}")
                else:
                    print(f"⚠️ pending_work #{row['id']} attempt {attempts_now} failed, will retry: {e}")
        except Exception as e:
            print(f"⚠️ taxonomy_heartbeat loop error: {e}")
