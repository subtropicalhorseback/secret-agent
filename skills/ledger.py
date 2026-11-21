"""
skills/ledger.py — the work ledger: every piece of delegated work, on disk
===========================================================================
Added 20 Nov 2026 (One Voice spec, B.1).

One row per piece of work Chip hands off (a browse run, a research or
analysis pipeline, later heals and builds). It ties the work to the inbound
message that started it, so that when the work reports back, or goes silent,
Chip knows whose request it was, what they asked, and which message to answer.

SQLite (data/memory/chip_work.db) so it survives a restart: at startup any
row still marked running whose process is gone is marked `lost`, and Chip can
say so instead of going silent (spec B.4).

Designed for several workers per request from the start: rows that answer the
same inbound message share a request_id; a follow-up instruction from the
result turn is a new row with parent_id set, and code caps follow-ups per
request (MAX_FOLLOWUPS).

Statuses: running → done | failed | timeout | stalled | cancelled | lost
"""
import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
from chip_config import WORKSPACE  # noqa: E402

DB = f"{WORKSPACE}/data/memory/chip_work.db"
MAX_FOLLOWUPS = 2
TERMINAL = ("done", "failed", "timeout", "stalled", "cancelled", "lost")


def _db_path():
    return os.getenv("CHIP_WORK_DB") or DB


def _conn():
    p = _db_path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    c = sqlite3.connect(p, timeout=10)
    c.row_factory = sqlite3.Row
    c.execute("""CREATE TABLE IF NOT EXISTS work (
        id           TEXT PRIMARY KEY,
        request_id   TEXT NOT NULL,
        parent_id    TEXT,
        kind         TEXT NOT NULL,
        owner        TEXT NOT NULL,
        inbound_ts   TEXT,
        request_text TEXT,
        task         TEXT,
        started_at   TEXT NOT NULL,
        deadline_at  TEXT,
        heartbeat_at TEXT,
        status       TEXT NOT NULL,
        result       TEXT,
        files        TEXT,
        record_path  TEXT,
        run_id       TEXT,
        cost_usd     REAL,
        pid          INTEGER,
        note         TEXT)""")
    cols = {r[1] for r in c.execute("PRAGMA table_info(work)").fetchall()}
    if "rerun_of" not in cols:     # added with restart recovery (spec B.4)
        c.execute("ALTER TABLE work ADD COLUMN rerun_of TEXT")
    c.execute("CREATE INDEX IF NOT EXISTS idx_work_req ON work(request_id)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_work_status ON work(status)")
    return c


def _now():
    return datetime.now().isoformat(timespec="seconds")


def _new_id(c):
    day = datetime.now().strftime("%m%d")
    n = c.execute("SELECT COUNT(*) FROM work WHERE id LIKE ?", (f"W{day}-%",)).fetchone()[0]
    return f"W{day}-{n + 1:03d}"


def start(request_id, kind, owner, task, request_text="", inbound_ts=None,
          deadline_s=900, parent_id=None, rerun_of=None):
    c = _conn()
    try:
        wid = _new_id(c)
        now = datetime.now()
        c.execute("INSERT INTO work (id, request_id, parent_id, kind, owner, inbound_ts, "
                  "request_text, task, started_at, deadline_at, heartbeat_at, status, pid, "
                  "rerun_of) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  (wid, request_id, parent_id, kind, owner, str(inbound_ts or ""),
                   request_text, task, now.isoformat(timespec="seconds"),
                   (now + timedelta(seconds=deadline_s)).isoformat(timespec="seconds"),
                   now.isoformat(timespec="seconds"), "running", os.getpid(), rerun_of))
        c.commit()
        return wid
    finally:
        c.close()


def update(wid, **fields):
    if not fields:
        return
    if "files" in fields and not isinstance(fields["files"], str):
        fields["files"] = json.dumps(fields["files"])
    cols = ", ".join(f"{k}=?" for k in fields)
    c = _conn()
    try:
        c.execute(f"UPDATE work SET {cols} WHERE id=?", (*fields.values(), wid))
        c.commit()
    finally:
        c.close()


def heartbeat(wid, note=None):
    f = {"heartbeat_at": _now()}
    if note:
        f["note"] = note
    update(wid, **f)


def finish(wid, status, result="", files=None, record_path=None, run_id=None,
           cost_usd=None, note=None):
    f = {"status": status, "result": result or "", "heartbeat_at": _now()}
    if files is not None:
        f["files"] = files
    for k, v in (("record_path", record_path), ("run_id", run_id),
                 ("cost_usd", cost_usd), ("note", note)):
        if v is not None:
            f[k] = v
    update(wid, **f)


def get(wid):
    c = _conn()
    try:
        r = c.execute("SELECT * FROM work WHERE id=?", (wid,)).fetchone()
        return _row(r)
    finally:
        c.close()


def _row(r):
    if r is None:
        return None
    d = dict(r)
    try:
        d["files"] = json.loads(d.get("files") or "[]")
    except Exception:
        d["files"] = []
    return d


def for_request(request_id):
    c = _conn()
    try:
        return [_row(r) for r in c.execute(
            "SELECT * FROM work WHERE request_id=? ORDER BY started_at, id",
            (request_id,)).fetchall()]
    finally:
        c.close()


def followups_used(request_id):
    c = _conn()
    try:
        return c.execute("SELECT COUNT(*) FROM work WHERE request_id=? AND parent_id IS NOT NULL",
                         (request_id,)).fetchone()[0]
    finally:
        c.close()


def running(owner=None):
    c = _conn()
    try:
        q, a = "SELECT * FROM work WHERE status='running'", ()
        if owner:
            q, a = q + " AND owner=?", (owner,)
        return [_row(r) for r in c.execute(q + " ORDER BY started_at", a).fetchall()]
    finally:
        c.close()


def recent(owner=None, hours=24, limit=20):
    c = _conn()
    try:
        cutoff = (datetime.now() - timedelta(hours=hours)).isoformat(timespec="seconds")
        q, a = "SELECT * FROM work WHERE started_at >= ?", [cutoff]
        if owner:
            q += " AND owner=?"
            a.append(owner)
        q += " ORDER BY started_at DESC LIMIT ?"
        a.append(limit)
        return [_row(r) for r in c.execute(q, a).fetchall()]
    finally:
        c.close()


def mark_lost_on_startup():
    """Rows still `running` from a previous process cannot be running now:
    the work lived inside that process. Returns the rows marked lost."""
    c = _conn()
    try:
        rows = [_row(r) for r in c.execute(
            "SELECT * FROM work WHERE status='running' AND (pid IS NULL OR pid != ?)",
            (os.getpid(),)).fetchall()]
        for r in rows:
            c.execute("UPDATE work SET status='lost', note=? WHERE id=?",
                      ("the coordinator restarted while this was running", r["id"]))
        c.commit()
        return rows
    finally:
        c.close()


def render(owner=None, hours=24):
    """Plain-text view for Chip's read-only work_status tool."""
    rows = recent(owner, hours=hours)
    if not rows:
        return f"No delegated work in the last {hours}h."
    out = []
    for r in rows:
        line = (f"{r['id']} {r['kind']} {r['status']} — started {r['started_at'][11:16]}"
                f" — {(r['task'] or '')[:90]}")
        if r.get("note"):
            line += f" [{r['note'][:80]}]"
        if r.get("record_path"):
            line += f" record: {os.path.relpath(r['record_path'], WORKSPACE) if r['record_path'].startswith(WORKSPACE) else r['record_path']}"
        out.append(line)
    return "\n".join(out)
