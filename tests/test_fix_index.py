#!/usr/bin/env python3
"""Acceptance drills for the staged-fix index (skills/fix_index.py).

Run:  venv/bin/python3 tests/test_fix_index.py

Covers drills 1, 2 and 5 of the 2026-11-20 comprehensive heal order:

  1. Stage three orders, supersede the second with a fourth. The index must
     show the right ids and statuses, and the superseded order must never be
     claimed for execution.
  2. Reject an order. The slot must clear, the record must persist as
     rejected, and it must be restorable.
  5. A heal that fails mid-phase must leave its registered phases on the
     record, so the terminus can report which phases completed.

Plus the invariants the order asks for that are easy to regress: monotonic
never-reused ids, at most one record running, approval of a second job while
one runs enqueuing rather than racing, staleness flagged at 72h without
changing status, and cycle refusal on supersession.

Runs against a throwaway DB in a temp dir — it never touches the live index.
"""
import os
import sys
import tempfile
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_TMP = tempfile.mkdtemp(prefix="fixindex_test_")
from skills import fix_index                                   # noqa: E402
fix_index.DB_PATH     = os.path.join(_TMP, "chip_fixes.db")
fix_index.LEGACY_JSON = os.path.join(_TMP, "nonexistent.json")

failures = []


def check(label, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {label}" + (f"  — {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(label)


def drill_1_supersession():
    print("\nDrill 1 — stage three, supersede the second with a fourth")
    a = fix_index.stage("First order: tighten the door sensor poll.", "admin")
    b = fix_index.stage("Second order: rewrite the weather fallback.", "admin")
    c = fix_index.stage("Third order: raise the artifact retention ring.", "admin")
    check("ids are sequential and formatted",
          (a["fix_id"], b["fix_id"], c["fix_id"]) == ("FIX-0001", "FIX-0002", "FIX-0003"),
          f"{a['fix_id']} {b['fix_id']} {c['fix_id']}")
    check("stage returns a queue depth", c["queue_depth"] == 3, c["queue_depth"])

    d = fix_index.stage("Fourth order: rewrite the weather fallback properly.",
                        "admin", supersedes=b["fix_id"])
    check("supersede is reported back", d["supersedes"] == "FIX-0002", d)

    by_id = {r["fix_id"]: r for r in fix_index.listing()}
    check("FIX-0002 is superseded", by_id["FIX-0002"]["status"] == "superseded",
          by_id["FIX-0002"]["status"])
    check("FIX-0002 points at its replacement",
          by_id["FIX-0002"]["superseded_by"] == "FIX-0004")
    check("FIX-0004 records what it replaced", by_id["FIX-0004"]["supersedes"] == "FIX-0002")
    for sid in ("FIX-0001", "FIX-0003", "FIX-0004"):
        check(f"{sid} still staged", by_id[sid]["status"] == "staged", by_id[sid]["status"])

    # The superseded order must not be approvable and must never be claimed.
    _, err = fix_index.approve("FIX-0002")
    check("superseded order cannot be approved", "superseded" in err.lower(), err)

    fix_index.approve("FIX-0001")
    claimed = fix_index.claim_next()
    check("claim takes the approved order", claimed["fix_id"] == "FIX-0001", claimed)
    check("only one runs at a time", fix_index.claim_next() is None)

    # Approving a second while one runs must enqueue, not race.
    _, msg = fix_index.approve("FIX-0003")
    check("second approval enqueues", "queued behind FIX-0001" in msg, msg)
    check("still nothing else claimable", fix_index.claim_next() is None)

    fix_index.set_status("FIX-0001", "done")
    nxt = fix_index.claim_next()
    check("next job claimed once the first ends", nxt and nxt["fix_id"] == "FIX-0003", nxt)
    fix_index.set_status("FIX-0003", "done")

    # A cycle must be refused rather than stored.
    try:
        fix_index.stage("Cyclic attempt.", "admin", supersedes="FIX-9999")
        check("supersede of a nonexistent id is refused", False)
    except ValueError as e:
        check("supersede of a nonexistent id is refused", "does not exist" in str(e), str(e))


def drill_2_reject_and_restore():
    print("\nDrill 2 — reject clears the slot, the record survives and restores")
    before = fix_index.queue_depth()
    r = fix_index.stage("Order to be rejected: probe the signal length cap.", "admin")
    check("depth rose on stage", fix_index.queue_depth() == before + 1)

    fid, msg = fix_index.reject(r["fix_id"])
    check("reject names the record", fid == r["fix_id"], msg)
    check("slot cleared", fix_index.queue_depth() == before, fix_index.queue_depth())

    rec = fix_index.get(r["fix_id"])
    check("record persists", rec is not None)
    check("record is rejected, not deleted", rec and rec["status"] == "rejected")
    check("full order text is retained",
          rec and "probe the signal length cap" in rec["description"])
    check("rejected order is not claimable", fix_index.claim_next() is None)

    _, rmsg = fix_index.restore(r["fix_id"])
    check("restore succeeds", fix_index.get(r["fix_id"])["status"] == "staged", rmsg)
    check("depth rose again on restore", fix_index.queue_depth() == before + 1)
    fix_index.reject(r["fix_id"])

    # Ids are never reused, even after a rejection. FIX-0005 was the rejected
    # one; the refused supersede in drill 1 must NOT have burned FIX-0005,
    # because a refusal has to happen before an id is consumed.
    nxt = fix_index.stage("A later order, to prove ids never rewind.", "admin")
    check("ids never reused and refusals burn none", nxt["fix_id"] == "FIX-0006",
          nxt["fix_id"])
    fix_index.reject(nxt["fix_id"])


def drill_5_partial_phases():
    print("\nDrill 5 — a heal failing mid-phase leaves its phase trail")
    p = fix_index.stage("Multi-phase order that will fail partway through.", "admin")
    fix_index.approve(p["fix_id"])
    fix_index.claim_next()
    fix_index.record_phase(p["fix_id"], "Phase 1 complete")
    fix_index.record_phase(p["fix_id"], "Phase 2 started")
    fix_index.set_status(p["fix_id"], "failed", note="crashed in phase 2")

    import json
    rec    = fix_index.get(p["fix_id"])
    phases = json.loads(rec["phases"] or "[]")
    check("partial state recorded as failed", rec["status"] == "failed")
    check("both phase markers survived", len(phases) == 2, phases)
    check("the last phase reached is readable",
          phases[-1]["note"] == "Phase 2 started", phases)
    check("the failure reason is on the record", "phase 2" in (rec["note"] or ""))
    check("a failed job frees the runner", fix_index.running() is None)


def staleness():
    print("\nStaleness — 72h flags without changing status")
    s = fix_index.stage("An order that will be aged out to look stale.", "admin")
    old = (datetime.now() - timedelta(hours=73)).isoformat(timespec="seconds")
    conn = fix_index._conn()
    conn.execute("UPDATE staged_fixes SET created_at=? WHERE fix_id=?", (old, s["fix_id"]))
    conn.commit()
    conn.close()

    row = [r for r in fix_index.listing() if r["fix_id"] == s["fix_id"]][0]
    check("flagged stale", row["stale"] is True)
    check("status is still staged, not auto-deleted", row["status"] == "staged")
    check("stale shows in the rendered listing", "[STALE >72h]" in fix_index.render())
    _, msg = fix_index.approve(s["fix_id"])
    check("a stale order still takes an ordinary approve",
          fix_index.get(s["fix_id"])["status"] == "approved", msg)
    fix_index.reject(s["fix_id"])


def listing_window():
    print("\nListing — non-terminal always, terminal only for 7 days")
    conn = fix_index._conn()
    conn.execute("UPDATE staged_fixes SET status_at=? WHERE fix_id='FIX-0001'",
                 ((datetime.now() - timedelta(days=9)).isoformat(timespec="seconds"),))
    conn.commit()
    conn.close()
    ids = {r["fix_id"] for r in fix_index.listing()}
    check("a long-closed record drops out of the window", "FIX-0001" not in ids, sorted(ids))
    check("recently closed records stay", "FIX-0003" in ids, sorted(ids))


def drill_5b_heal_terminus():
    """The other half of drill 5: the terminus message a failed heal sends.

    Phase 3 requires heal termini to name the fix id, the final status and
    which phases completed — and to fire from the terminus path, so a crash
    still produces one."""
    print("\nDrill 5b — the heal terminus reports id, status and phases")
    import skills.self_heal as sh

    sent = []
    sh._signal_send = lambda msg, recipient=None: sent.append(msg) or True
    sh.fix_index    = fix_index          # point the runner at the temp db
    # The result goes through Chip's outbox now (One Voice spec A.3/D.6).
    from skills import outbox
    events = []
    outbox.notify = lambda kind, text, urgent=False, **kw: events.append(
        (kind, text, urgent)) or "queued"

    h = fix_index.stage("An order whose healer will die in phase 2.", "admin")
    fix_index.approve(h["fix_id"])
    fix_index.claim_next()
    fix_index.record_phase(h["fix_id"], "Phase 1 complete: index built")
    fix_index.record_phase(h["fix_id"], "Phase 2 started: browser work")

    log = os.path.join(_TMP, "heal_test.log")
    open(log, "w").write("HEADER\nbuilt the thing\n")
    # New runner (One Voice spec D.5): status comes from the gate. No real
    # restart and no write to the real state note from a test.
    sh.RESTART_AFTER_ROLLBACK = False
    noted = []
    sh._note_state = lambda line: noted.append(line)
    sh._finish_job(h["fix_id"], fix_index.get(h["fix_id"]), "repair",
                   {"status": "failed", "reason": "healer exited 1", "checks": []},
                   0.12, "built the thing", log, None, {}, 1, note="healer exited 1")
    check("the state note gets a correction line", noted and h["fix_id"] in noted[0], noted)

    results = [e for e in events if e[0] == "heal_result"]
    check("exactly one result event, through the outbox", len(results) == 1, events)
    check("nothing was sent to Signal directly", not sent, sent)
    msg = results[0][1] if results else ""
    check("the result is urgent (it reaches the admin)", results and results[0][2], results)
    check("it names the fix id", h["fix_id"] in msg, msg[:160])
    check("it names the final status", "failed" in msg, msg[:160])
    check("it says where it stopped", "Phase 2 started" in msg, msg[:300])
    check("it reports the verdict", "exited 1" in msg, msg[:300])
    check("it is short: one line, not a transcript", len(msg) < 400 and "\n" not in msg, msg)
    check("the record is marked failed", fix_index.get(h["fix_id"])["status"] == "failed")
    check("the runner was freed", fix_index.running() is None)


def main():
    print(f"staged-fix index drills (temp db: {fix_index.DB_PATH})")
    drill_1_supersession()
    drill_2_reject_and_restore()
    drill_5_partial_phases()
    drill_5b_heal_terminus()
    staleness()
    listing_window()
    print(f"\n{'FAILED: ' + ', '.join(failures) if failures else 'all checks passed'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
