"""
skills/memory_maint.py — nightly memory & disk hygiene
=======================================================
Added 19 Nov 2026. The audit's self-assessment line "nothing prunes stale
facts" was accurate — facts only ever accumulated, superseded rows sat
alongside live ones, and workspace roots were collecting .bak files from
every patch session.

Deterministic only — no LLM decides what to forget:
  1. expire   facts whose valid_until has passed → archived
  2. dedup    exact-normalized duplicate active facts per user → older
              copies superseded by the newest
  3. prune    conversation turns older than 60 days / beyond 2000 per user
              (their content lives on in the rolling summary)
  4. tidy     *.bak_* files older than 7 days in the workspace root and
              skills/ → moved to backups/auto/ (never deleted)
  5. vacuum   the SQLite databases that churn

Called by dream_cycle at 0100; safe to run by hand:
  venv/bin/python3 -c "from skills.memory_maint import run_maintenance; print(run_maintenance())"
"""
import glob
import os
import re
import shutil
import sqlite3
from datetime import datetime, timedelta

import os as _os, sys as _sys
_cc = _os.path.dirname(_os.path.abspath(__file__))
while _cc != "/" and not _os.path.exists(_os.path.join(_cc, "chip_config.py")):
    _cc = _os.path.dirname(_cc)
if _cc not in _sys.path:
    _sys.path.insert(0, _cc)
from chip_config import WORKSPACE
CONTEXT_DB = f"{WORKSPACE}/data/memory/chip_context.db"
AUTO_BAK   = f"{WORKSPACE}/backups/auto"


def _norm(text):
    return re.sub(r"\W+", " ", (text or "").lower()).strip()


def expire_facts(conn):
    """valid_until in the past → archived (audit trail kept in fact_history
    by the existing triggers/conventions; we just flip status)."""
    now = datetime.now().isoformat()
    cur = conn.execute(
        "UPDATE facts SET status='archived', updated_at=? "
        "WHERE status='active' AND valid_until IS NOT NULL "
        "AND valid_until != '' AND valid_until < ?", (now, now))
    return cur.rowcount


def dedup_facts(conn):
    """Exact-normalized duplicates within one user's active facts: keep the
    newest, mark older copies superseded by it."""
    rows = conn.execute(
        "SELECT id, user, content FROM facts "
        "WHERE confirmed=1 AND status='active' ORDER BY id DESC").fetchall()
    seen, dups = {}, 0
    now = datetime.now().isoformat()
    for fid, user, content in rows:          # newest first
        key = (user or "", _norm(content))
        if not key[1]:
            continue
        if key in seen:
            conn.execute(
                "UPDATE facts SET status='superseded', superseded_by=?, "
                "updated_at=? WHERE id=?", (seen[key], now, fid))
            dups += 1
        else:
            seen[key] = fid
    return dups


def tidy_backups(days=7):
    """Sweep aging .bak_* clutter from the working tree into backups/auto/."""
    os.makedirs(AUTO_BAK, exist_ok=True)
    cutoff, moved = datetime.now() - timedelta(days=days), 0
    for pattern in (f"{WORKSPACE}/*.bak*", f"{WORKSPACE}/skills/*.bak*",
                    f"{WORKSPACE}/skills/*.pre_lock"):
        for path in glob.glob(pattern):
            try:
                if datetime.fromtimestamp(os.path.getmtime(path)) < cutoff:
                    shutil.move(path, f"{AUTO_BAK}/{os.path.basename(path)}")
                    moved += 1
            except Exception:
                pass
    return moved


def vacuum_dbs():
    done = []
    for name in ("chip_context.db", "chip_history.db", "usage_log.db"):
        path = f"{WORKSPACE}/data/memory/{name}"
        if os.path.exists(path):
            try:
                c = sqlite3.connect(path)
                c.execute("VACUUM")
                c.close()
                done.append(name)
            except Exception as e:
                print(f"⚠️ vacuum {name}: {e}")
    return done


def run_maintenance():
    """Returns a one-line report for the dream-cycle log / Signal digest."""
    parts = []
    try:
        conn = sqlite3.connect(CONTEXT_DB)
        expired = expire_facts(conn)
        duped   = dedup_facts(conn)
        conn.commit(); conn.close()
        parts.append(f"facts: {expired} expired, {duped} deduped")
    except Exception as e:
        parts.append(f"facts: error {e}")
    try:
        from skills import history
        parts.append(f"turns pruned: {history.prune()}")
    except Exception as e:
        parts.append(f"prune: error {e}")
    parts.append(f"baks tidied: {tidy_backups()}")
    parts.append(f"vacuumed: {len(vacuum_dbs())} dbs")
    report = "Memory maintenance — " + "; ".join(parts)
    print(report)
    return report
