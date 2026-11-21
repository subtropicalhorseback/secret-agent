"""
skills/fix_index.py — the staged-fix index: a durable, multi-slot heal queue
===============================================================================
Added 20 Nov 2026 (Phase 1 of the comprehensive heal order).

WHY THIS EXISTS. The old store was one JSON file, `data/patches/pending_heal.json`.
It worked, but it had two properties that produced a visible failure on
2026-11-20: Chip had no read path to it (`fix: status` was a human-only
command, never a tool), and `run_job()` deleted the file before launching, so a
crashed heal left no record of what it had been running. Twice that morning
Chip asserted queue state it could not observe — a stale probe job still
occupying the slot after the admin had rejected it, and a staged comprehensive
order that was never staged at all. Neither was a lie about the data; there was
no data to read. This module is the read path, and the write confirmation.

DESIGN NOTES, so the next reader does not re-litigate them:

  * IDs are `FIX-0001`, monotonic, never reused. The table uses AUTOINCREMENT
    precisely so SQLite will not recycle a rowid after a delete — and nothing
    here ever deletes, so that is belt and suspenders.

  * Nothing is ever deleted. Rejection and supersession are status changes.
    A rejected record can be restored. This is the whole point: the admin
    needs to be able to ask "what happened to that order I rejected" and get
    an answer, not a silence.

  * `stale` is in the status vocabulary but is NOT written to the table. A
    record staged more than STALE_HOURS ago is *flagged* stale in listings
    while keeping status `staged`. Writing it as a status would mean approve
    and reject had to special-case it, and the order is explicit that stale
    records still take an ordinary human approve or reject.

  * Serialization is enforced at claim time, not at approve time. Approving a
    second order while one runs leaves it `approved`; the runner claims the
    oldest `approved` record only when nothing is `running`. That makes the
    queue correct even if two approvals race, because the claim is a single
    conditional UPDATE inside an IMMEDIATE transaction.

  * Chip can stage and read. Chip cannot approve, reject, restore, or delete:
    those live behind the coordinator's `fix:` command, which only fires for a
    message an admin actually typed.
"""
import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta

_cc = os.path.dirname(os.path.abspath(__file__))
while _cc != "/" and not os.path.exists(os.path.join(_cc, "chip_config.py")):
    _cc = os.path.dirname(_cc)
if _cc not in sys.path:
    sys.path.insert(0, _cc)
from chip_config import WORKSPACE

DB_PATH     = f"{WORKSPACE}/data/memory/chip_fixes.db"
LEGACY_JSON = f"{WORKSPACE}/data/patches/pending_heal.json"

STALE_HOURS    = 72
TERMINAL       = ("done", "failed", "superseded", "rejected")
ACTIVE         = ("staged", "approved", "running")
STATUSES       = ACTIVE + TERMINAL + ("stale",)
TERMINAL_WINDOW_DAYS = 7


def _now():
    return datetime.now().isoformat(timespec="seconds")


def _conn():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS staged_fixes (
            rowid_pk       INTEGER PRIMARY KEY AUTOINCREMENT,
            fix_id         TEXT UNIQUE,
            description    TEXT NOT NULL,
            requested_by   TEXT NOT NULL,
            created_at     TEXT NOT NULL,
            status_at      TEXT NOT NULL,
            status         TEXT NOT NULL,
            supersedes     TEXT,
            superseded_by  TEXT,
            log_tail       TEXT,
            log_path       TEXT,
            phases         TEXT,
            note           TEXT
        )
    """)
    # Two tracks and the gate (One Voice spec Part D, 20 Nov 2026).
    cols = {r[1] for r in conn.execute("PRAGMA table_info(staged_fixes)").fetchall()}
    for col, typ in (("track", "TEXT"), ("allowed_paths", "TEXT"), ("acceptance", "TEXT"),
                     ("acceptance_cmd", "TEXT"), ("skill_name", "TEXT"),
                     ("continue_of", "TEXT"), ("session_id", "TEXT"),
                     ("start_head", "TEXT"), ("cost_usd", "REAL"),
                     ("gate_status", "TEXT"), ("gate_report", "TEXT"),
                     ("iterations", "INTEGER"), ("summary", "TEXT")):
        if col not in cols:
            conn.execute(f"ALTER TABLE staged_fixes ADD COLUMN {col} {typ}")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_fix_status ON staged_fixes(status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_fix_created ON staged_fixes(created_at)")
    conn.commit()
    _migrate_legacy(conn)
    return conn


def _migrate_legacy(conn):
    """One-shot import of a pending_heal.json left over from the single-slot
    store. Renamed rather than deleted, so the old file is still on disk if
    this ever needs auditing."""
    if not os.path.exists(LEGACY_JSON):
        return
    try:
        spec = json.load(open(LEGACY_JSON))
        _insert(conn,
                description=spec.get("description", "(no description)"),
                requested_by=spec.get("staged_by", "the admin"),
                created_at=spec.get("staged_at") or _now(),
                log_tail=spec.get("log_tail", ""),
                note="migrated from pending_heal.json")
        os.rename(LEGACY_JSON, LEGACY_JSON + ".migrated")
    except Exception as e:
        print(f"fix_index: legacy migration skipped: {e}")


def _insert(conn, description, requested_by, created_at=None, log_tail="",
            supersedes=None, note=None):
    ts = created_at or _now()
    cur = conn.execute(
        "INSERT INTO staged_fixes (fix_id, description, requested_by, created_at,"
        " status_at, status, supersedes, log_tail, note)"
        " VALUES (NULL,?,?,?,?,'staged',?,?,?)",
        (description, requested_by, ts, ts, supersedes, log_tail, note))
    fid = f"FIX-{cur.lastrowid:04d}"
    conn.execute("UPDATE staged_fixes SET fix_id=? WHERE rowid_pk=?", (fid, cur.lastrowid))
    conn.commit()
    return fid


def _row(conn, fix_id):
    return conn.execute("SELECT * FROM staged_fixes WHERE fix_id=?", (fix_id,)).fetchone()


def _is_stale(row):
    if row["status"] != "staged":
        return False
    try:
        return datetime.fromisoformat(row["created_at"]) < datetime.now() - timedelta(hours=STALE_HOURS)
    except Exception:
        return False


def _age(row):
    try:
        secs = (datetime.now() - datetime.fromisoformat(row["created_at"])).total_seconds()
    except Exception:
        return "?"
    if secs < 3600:
        return f"{int(secs // 60)}m"
    if secs < 86400:
        return f"{int(secs // 3600)}h"
    return f"{int(secs // 86400)}d"


def _summary(text, width=90):
    one = " ".join((text or "").split())
    return one if len(one) <= width else one[:width - 1] + "…"


# ─── Writes Chip is allowed to make ───────────────────────────────────────────

def stage(description, staged_by="the admin", supersedes=None, log_tail="",
          track="repair", allowed_paths=None, acceptance="", acceptance_cmd="",
          skill_name=None, continue_of=None):
    """Stage an order. Returns a dict — the caller MUST surface fix_id and
    depth, because a stage with no visible confirmation is how this whole
    class of failure started. Raises on a supersede target that cannot be
    superseded, so a bad supersede is an error and never a silent plain stage.
    """
    description = (description or "").strip()
    if not description:
        raise ValueError("empty description")
    conn = _conn()
    try:
        if supersedes:
            supersedes = supersedes.strip().upper()
            target = _row(conn, supersedes)
            if target is None:
                raise ValueError(f"{supersedes} does not exist")
            if target["status"] in TERMINAL:
                raise ValueError(f"{supersedes} is already {target['status']}; nothing to supersede")
            if target["status"] == "running":
                raise ValueError(f"{supersedes} is running; let it finish or kill it first")
            # A new record always gets a higher id than anything it can point
            # at, so a cycle is structurally impossible — but walk the target's
            # ancestry first anyway, both as a corrupted-table guard and so the
            # refusal happens BEFORE an id is consumed. Refusing after the
            # insert would burn an id and leave an orphan staged record.
            seen, cur_id, depth = set(), supersedes, 0
            while cur_id and depth < 64:
                if cur_id in seen:
                    raise ValueError(f"supersession chain through {supersedes} is "
                                     f"cyclic — index is corrupt, nothing staged")
                seen.add(cur_id)
                nxt = _row(conn, cur_id)
                cur_id = nxt["supersedes"] if nxt else None
                depth += 1
            if depth >= 64:
                raise ValueError("supersession chain too deep; nothing staged")
        fid = _insert(conn, description, staged_by, log_tail=log_tail, supersedes=supersedes)
        conn.execute("UPDATE staged_fixes SET track=?, allowed_paths=?, acceptance=?, "
                     "acceptance_cmd=?, skill_name=?, continue_of=? WHERE fix_id=?",
                     (track, json.dumps(allowed_paths or []), acceptance or "",
                      acceptance_cmd or "", skill_name, continue_of, fid))
        conn.commit()
        if supersedes:
            conn.execute(
                "UPDATE staged_fixes SET status='superseded', status_at=?, superseded_by=?"
                " WHERE fix_id=?", (_now(), fid, supersedes))
            conn.commit()
        depth_now = conn.execute(
            "SELECT COUNT(*) FROM staged_fixes WHERE status IN ('staged','approved','running')"
        ).fetchone()[0]
        return {"fix_id": fid, "queue_depth": depth_now, "supersedes": supersedes,
                "status": "staged"}
    finally:
        conn.close()


def record_phase(fix_id, note):
    """Append a phase marker to a record. A running heal calls this as the
    first act of each phase, so a crash leaves a readable trail instead of an
    unknown state."""
    conn = _conn()
    try:
        row = _row(conn, fix_id)
        if row is None:
            return False
        phases = json.loads(row["phases"] or "[]")
        phases.append({"at": _now(), "note": note})
        conn.execute("UPDATE staged_fixes SET phases=? WHERE fix_id=?",
                     (json.dumps(phases), fix_id))
        conn.commit()
        return True
    finally:
        conn.close()


def get(fix_id):
    conn = _conn()
    try:
        row = _row(conn, (fix_id or "").strip().upper())
        return dict(row) if row else None
    finally:
        conn.close()


def listing(window_days=TERMINAL_WINDOW_DAYS):
    """Every non-terminal record, plus terminal records from the last N days."""
    conn = _conn()
    try:
        cutoff = (datetime.now() - timedelta(days=window_days)).isoformat()
        rows = conn.execute(
            "SELECT * FROM staged_fixes WHERE status IN ('staged','approved','running')"
            "   OR status_at >= ? ORDER BY rowid_pk", (cutoff,)).fetchall()
        out = []
        for r in rows:
            out.append({
                "fix_id":        r["fix_id"],
                "status":        r["status"],
                "stale":         _is_stale(r),
                "age":           _age(r),
                "requested_by":  r["requested_by"],
                "created_at":    r["created_at"],
                "status_at":     r["status_at"],
                "supersedes":    r["supersedes"],
                "superseded_by": r["superseded_by"],
                "log_path":      r["log_path"],
                "summary":       _summary(r["description"]),
            })
        return out
    finally:
        conn.close()


def render(window_days=TERMINAL_WINDOW_DAYS):
    """Plain-text listing — all Signal output is plain text regardless of
    markdown, so this is deliberately a flat list and not a table."""
    rows = listing(window_days)
    if not rows:
        return "Staged-fix index is empty — nothing queued and nothing run in the last week."
    active = [r for r in rows if r["status"] in ACTIVE]
    past   = [r for r in rows if r["status"] in TERMINAL]
    lines  = [f"Staged-fix index — {len(active)} open, {len(past)} closed in {window_days}d"]
    for r in active:
        flag = " [STALE >72h]" if r["stale"] else ""
        lines.append(f"\n{r['fix_id']}  {r['status'].upper()}{flag}  {r['age']} old  "
                     f"by {r['requested_by']}\n  {r['summary']}")
        if r["supersedes"]:
            lines.append(f"  replaces {r['supersedes']}")
    for r in past:
        tail = f" -> {r['superseded_by']}" if r["superseded_by"] else ""
        lines.append(f"\n{r['fix_id']}  {r['status']}{tail}  {r['age']} old\n  {r['summary']}")
    return "\n".join(lines)


# ─── Human-only transitions (coordinator `fix:` command) ──────────────────────

def _target_for_action(conn, fix_id, allowed, label):
    """Resolve a bare `fix: approve` to the single obvious candidate, or
    require an explicit ID when there is more than one."""
    if fix_id:
        row = _row(conn, fix_id.strip().upper())
        if row is None:
            return None, f"No such record: {fix_id.strip().upper()}"
        if row["status"] not in allowed:
            return None, f"{row['fix_id']} is {row['status']} — cannot {label} it."
        return row, None
    rows = conn.execute(
        "SELECT * FROM staged_fixes WHERE status IN (%s) ORDER BY rowid_pk"
        % ",".join("?" * len(allowed)), allowed).fetchall()
    if not rows:
        return None, None
    if len(rows) > 1:
        ids = ", ".join(r["fix_id"] for r in rows)
        return None, (f"{len(rows)} records can be {label}d ({ids}). "
                      f"Name one: 'fix: {label} {rows[0]['fix_id']}'.")
    return rows[0], None


def set_fields(fix_id, **fields):
    """Runner-side writes of gate results, cost, session (spec Part D)."""
    if not fields:
        return
    if "allowed_paths" in fields and not isinstance(fields["allowed_paths"], str):
        fields["allowed_paths"] = json.dumps(fields["allowed_paths"])
    conn = _conn()
    try:
        cols = ", ".join(f"{k}=?" for k in fields)
        conn.execute(f"UPDATE staged_fixes SET {cols} WHERE fix_id=?",
                     (*fields.values(), fix_id))
        conn.commit()
    finally:
        conn.close()


def spend_since(iso_start):
    conn = _conn()
    try:
        r = conn.execute("SELECT COALESCE(SUM(cost_usd),0), COUNT(cost_usd) FROM staged_fixes "
                         "WHERE status_at >= ? AND cost_usd IS NOT NULL", (iso_start,)).fetchone()
        return float(r[0] or 0), int(r[1] or 0)
    finally:
        conn.close()


def spend_today():
    return spend_since(datetime.now().strftime("%Y-%m-%dT00:00:00"))


def set_status(fix_id, status, note=None, log_path=None):
    if status not in STATUSES:
        raise ValueError(f"bad status {status}")
    conn = _conn()
    try:
        sets, vals = ["status=?", "status_at=?"], [status, _now()]
        if note is not None:
            sets.append("note=?"); vals.append(note)
        if log_path is not None:
            sets.append("log_path=?"); vals.append(log_path)
        vals.append(fix_id)
        conn.execute(f"UPDATE staged_fixes SET {','.join(sets)} WHERE fix_id=?", vals)
        conn.commit()
    finally:
        conn.close()


def approve(fix_id=None):
    """Mark a record approved. Does NOT launch — the caller launches, and only
    if nothing is already running. Returns (fix_id|None, message)."""
    conn = _conn()
    try:
        row, err = _target_for_action(conn, fix_id, ("staged",), "approve")
        if err:
            return None, err
        if row is None:
            return None, "Nothing staged — use fix: <description> first."
        conn.execute("UPDATE staged_fixes SET status='approved', status_at=? WHERE fix_id=?",
                     (_now(), row["fix_id"]))
        conn.commit()
        running = conn.execute(
            "SELECT fix_id FROM staged_fixes WHERE status='running'").fetchone()
        if running:
            return row["fix_id"], (f"{row['fix_id']} approved and queued behind "
                                   f"{running['fix_id']}, which is still running. "
                                   f"It starts automatically when that one ends.")
        return row["fix_id"], f"{row['fix_id']} approved."
    finally:
        conn.close()


def reject(fix_id=None):
    conn = _conn()
    try:
        row, err = _target_for_action(conn, fix_id, ("staged", "approved"), "reject")
        if err:
            return None, err
        if row is None:
            return None, "Nothing staged."
        conn.execute("UPDATE staged_fixes SET status='rejected', status_at=? WHERE fix_id=?",
                     (_now(), row["fix_id"]))
        conn.commit()
        return row["fix_id"], (f"{row['fix_id']} rejected. The record is kept — "
                               f"'fix: restore {row['fix_id']}' puts it back on the queue.")
    finally:
        conn.close()


def restore(fix_id):
    if not fix_id:
        return None, "Name the record: 'fix: restore FIX-0003'."
    conn = _conn()
    try:
        row = _row(conn, fix_id.strip().upper())
        if row is None:
            return None, f"No such record: {fix_id.strip().upper()}"
        if row["status"] != "rejected":
            return None, f"{row['fix_id']} is {row['status']}, not rejected."
        conn.execute("UPDATE staged_fixes SET status='staged', status_at=? WHERE fix_id=?",
                     (_now(), row["fix_id"]))
        conn.commit()
        return row["fix_id"], f"{row['fix_id']} restored to staged, awaiting approval."
    finally:
        conn.close()


# ─── Serialized execution ─────────────────────────────────────────────────────

def claim_next():
    """Atomically move the oldest approved record to running, but only if
    nothing is running. Returns the claimed row as a dict, or None.

    BEGIN IMMEDIATE takes the write lock before the SELECT, so two runners
    starting at once cannot both see an empty running set."""
    conn = _conn()
    try:
        conn.isolation_level = None
        conn.execute("BEGIN IMMEDIATE")
        try:
            if conn.execute("SELECT 1 FROM staged_fixes WHERE status='running'").fetchone():
                conn.execute("ROLLBACK")
                return None
            row = conn.execute(
                "SELECT * FROM staged_fixes WHERE status='approved' ORDER BY rowid_pk LIMIT 1"
            ).fetchone()
            if row is None:
                conn.execute("ROLLBACK")
                return None
            conn.execute("UPDATE staged_fixes SET status='running', status_at=? WHERE fix_id=?",
                         (_now(), row["fix_id"]))
            conn.execute("COMMIT")
            return dict(row)
        except Exception:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()


def running():
    conn = _conn()
    try:
        row = conn.execute("SELECT * FROM staged_fixes WHERE status='running'").fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def queue_depth():
    conn = _conn()
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM staged_fixes WHERE status IN ('staged','approved','running')"
        ).fetchone()[0]
    finally:
        conn.close()


def pending_approved():
    conn = _conn()
    try:
        return [r["fix_id"] for r in conn.execute(
            "SELECT fix_id FROM staged_fixes WHERE status='approved' ORDER BY rowid_pk")]
    finally:
        conn.close()


if __name__ == "__main__":
    args = sys.argv[1:]
    if args and args[0] == "--phase" and len(args) >= 3:
        print("ok" if record_phase(args[1].upper(), " ".join(args[2:])) else "no such record")
    else:
        print(render())
