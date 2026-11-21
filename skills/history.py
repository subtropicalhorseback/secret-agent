"""
skills/history.py — persistent conversation history with a rolling summary
===========================================================================
Added 19 Nov 2026 (audit M3). Before this, history was flat buffer files that
the dream cycle wiped at 0100 — Chip woke up every morning with amnesia, and
within a day 30 raw turns were re-sent on every message with no size budget
(a pasted document got re-billed every turn until midnight).

Now:
  - every turn lands in data/memory/chip_history.db (survives the night;
    buffers still exist for the dream-cycle archive, unchanged);
  - the model sees the last N turns verbatim under a character budget, plus
    a per-user rolling summary of everything older;
  - the summary is folded forward by Gemini Flash-Lite in the background
    whenever enough un-summarized turns accumulate — never on the hot path.

Buffer-file seeding happens once at startup so the cutover loses nothing.
"""
import os
import re
import sqlite3
from datetime import datetime, timedelta

import os as _os, sys as _sys
_cc = _os.path.dirname(_os.path.abspath(__file__))
while _cc != "/" and not _os.path.exists(_os.path.join(_cc, "chip_config.py")):
    _cc = _os.path.dirname(_cc)
if _cc not in _sys.path:
    _sys.path.insert(0, _cc)
from chip_config import WORKSPACE
HISTORY_DB = f"{WORKSPACE}/data/memory/chip_history.db"
BUFFER_DIR = f"{WORKSPACE}/data/memory"   # matches coordinator's BUFFER_DIR

SUMMARIZE_AFTER   = 24   # un-summarized turns that trigger a fold
KEEP_VERBATIM     = 12   # newest turns never folded into the summary
TURN_CHAR_CAP     = 1500 # single-turn truncation in model-facing history
HISTORY_CHAR_CAP  = 9000 # total verbatim budget per request
SUMMARY_WORD_CAP  = 300


def _conn():
    os.makedirs(os.path.dirname(HISTORY_DB), exist_ok=True)
    c = sqlite3.connect(HISTORY_DB, timeout=10)
    c.execute("""CREATE TABLE IF NOT EXISTS turns (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user TEXT NOT NULL, role TEXT NOT NULL,
        content TEXT NOT NULL, ts TEXT NOT NULL)""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_turns_user ON turns(user, id)")
    c.execute("""CREATE TABLE IF NOT EXISTS summaries (
        user TEXT PRIMARY KEY, summary TEXT, last_turn_id INTEGER,
        updated_at TEXT)""")
    return c


def log_turn(user, role, content):
    """Append one turn. Called from coordinator's log path — cheap insert.

    Machine-origin turns are refused outright (FIX-0006). A browse worker's
    report is webpage text arriving under a machine principal, and the rolling
    summary is a record of what a PERSON said. Folding scrape into it would
    have the model recall page content as if the user had typed it. The guard
    lives here rather than at the call site so the invariant holds for every
    caller, present and future — there is no path that can log it by accident.
    """
    try:
        from skills.browse_bus import PRINCIPAL as _MACHINE
        if str(user or "").lower() == _MACHINE.lower():
            return
    except Exception:
        pass
    try:
        c = _conn()
        c.execute("INSERT INTO turns (user, role, content, ts) VALUES (?,?,?,?)",
                  (user.lower(), role, content, datetime.now().isoformat()))
        c.commit(); c.close()
    except Exception as e:
        print(f"⚠️ history log_turn: {e}")


def get_history(user, max_turns=30, max_chars=HISTORY_CHAR_CAP):
    """Model-facing history: newest turns, oldest-first, char-budgeted,
    starting on a user turn (API requirement)."""
    try:
        c = _conn()
        rows = c.execute(
            "SELECT role, content FROM turns WHERE user=? ORDER BY id DESC LIMIT ?",
            (user.lower(), max_turns)).fetchall()
        c.close()
    except Exception as e:
        print(f"⚠️ history get: {e}")
        return []
    history, used = [], 0
    for role, content in rows:                      # newest → oldest
        text = content if len(content) <= TURN_CHAR_CAP else \
               content[:TURN_CHAR_CAP] + " …[trimmed]"
        if used + len(text) > max_chars and history:
            break
        used += len(text)
        history.append({"role": role, "content": text})
    history.reverse()
    while history and history[0]["role"] != "user":
        history.pop(0)
    # Merge consecutive same-role turns. A deferred answer (One Voice spec
    # B.2) logs the user's message when work starts and Chip's answer when it
    # ends, so a message sent in between leaves two user turns in a row; the
    # model APIs want roles to alternate.
    merged = []
    for h in history:
        if merged and merged[-1]["role"] == h["role"]:
            merged[-1] = {"role": h["role"],
                          "content": merged[-1]["content"] + "\n\n" + h["content"]}
        else:
            merged.append(dict(h))
    return merged


def get_summary(user):
    try:
        c = _conn()
        row = c.execute("SELECT summary FROM summaries WHERE user=?",
                        (user.lower(),)).fetchone()
        c.close()
        return (row[0] or "").strip() if row else ""
    except Exception:
        return ""


def maybe_summarize(user):
    """Fold older turns into the rolling summary when enough accumulate.
    Runs in a background thread (asyncio.to_thread) — LLM call inside."""
    user = user.lower()
    try:
        c = _conn()
        row = c.execute("SELECT summary, last_turn_id FROM summaries WHERE user=?",
                        (user,)).fetchone()
        prev_summary, last_id = (row or ("", 0))
        last_id = last_id or 0
        pending = c.execute(
            "SELECT id, role, content FROM turns WHERE user=? AND id>? ORDER BY id",
            (user, last_id)).fetchall()
        if len(pending) < SUMMARIZE_AFTER:
            c.close(); return False
        fold  = pending[:-KEEP_VERBATIM]            # newest stay verbatim
        if not fold:
            c.close(); return False
        convo = "\n".join(f"{'USER' if r=='user' else 'CHIP'}: {t[:800]}"
                          for _id, r, t in fold)
        from skills import llm
        new_summary = llm.gemini_sync(
            "Maintain a rolling memory summary of an assistant's conversation "
            f"with one user. Fold the NEW TURNS into the EXISTING SUMMARY — "
            f"keep decisions, open questions, preferences, ongoing projects and "
            f"concrete facts; drop pleasantries. Plain prose, max "
            f"{SUMMARY_WORD_CAP} words, most recent context last.\n\n"
            f"EXISTING SUMMARY:\n{prev_summary or '(none)'}\n\n"
            f"NEW TURNS:\n{convo}",
            model=llm.GLITE, timeout=60)
        if not new_summary.strip():
            c.close(); return False
        c.execute("INSERT INTO summaries (user, summary, last_turn_id, updated_at) "
                  "VALUES (?,?,?,?) ON CONFLICT(user) DO UPDATE SET "
                  "summary=excluded.summary, last_turn_id=excluded.last_turn_id, "
                  "updated_at=excluded.updated_at",
                  (user, new_summary.strip(), fold[-1][0],
                   datetime.now().isoformat()))
        c.commit(); c.close()
        print(f"🧵 [{user}] summary folded ({len(fold)} turns → "
              f"{len(new_summary.split())} words)")
        return True
    except Exception as e:
        print(f"⚠️ history summarize: {e}")
        return False


def seed_from_buffers(users):
    """One-time cutover: import existing buffer files so the switch to the DB
    loses no context. No-op whenever the turns table already has rows.
    `users` = iterable of lowercase names."""
    try:
        c = _conn()
        if c.execute("SELECT COUNT(*) FROM turns").fetchone()[0] > 0:
            c.close(); return 0
        n = 0
        for name in users:
            path = f"{BUFFER_DIR}/buffer_{name}.txt"
            if not os.path.exists(path):
                continue
            cap = name.capitalize()
            for line in open(path).read().strip().splitlines():
                line = line.strip()
                if f"] {cap}:" in line:
                    text = line.split(f"] {cap}:", 1)[-1].strip().replace("\\n", "\n")
                    role = "user"
                elif "] Chip:" in line:
                    text = line.split("] Chip:", 1)[-1].strip().replace("\\n", "\n")
                    text = re.sub(r"^\[[^\]]+\]\s*", "", text)
                    role = "assistant"
                else:
                    continue
                if text:
                    c.execute("INSERT INTO turns (user, role, content, ts) "
                              "VALUES (?,?,?,?)",
                              (name, role, text, datetime.now().isoformat()))
                    n += 1
        c.commit(); c.close()
        if n:
            print(f"🧵 history seeded from buffers: {n} turns")
        return n
    except Exception as e:
        print(f"⚠️ history seed: {e}")
        return 0


def prune(days=60, keep_per_user=2000):
    """Maintenance: drop ancient turns (their content lives on in the
    summary). Called from the nightly memory maintenance job."""
    try:
        c = _conn()
        cutoff = (datetime.now() - timedelta(days=days)).isoformat()
        cur = c.execute("DELETE FROM turns WHERE ts < ?", (cutoff,))
        dropped = cur.rowcount
        for (u,) in c.execute("SELECT DISTINCT user FROM turns").fetchall():
            ids = c.execute("SELECT id FROM turns WHERE user=? ORDER BY id DESC "
                            "LIMIT 1 OFFSET ?", (u, keep_per_user)).fetchone()
            if ids:
                cur = c.execute("DELETE FROM turns WHERE user=? AND id<=?",
                                (u, ids[0]))
                dropped += cur.rowcount
        c.commit(); c.close()
        return dropped
    except Exception as e:
        print(f"⚠️ history prune: {e}")
        return 0
