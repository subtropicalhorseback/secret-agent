"""
skills/workers.py — delegated work: launch it, track it, join it, hand it back
===============================================================================
Added 20 Nov 2026 (One Voice spec, Part B).

Chip hands work to workers — a browser run, a research or analysis pipeline —
the way it consults an advisor, in reverse. Before this, each tool started a
background task that sent its own result straight to the user, after Chip had
already replied "started": two or more messages per request, and the result
never entered the conversation, so "what about target?" had nothing to refer to.

Now:
  launch()  records the work in the ledger (skills/ledger.py), starts it, and
            attaches it to the current turn (send_gate.Turn.workers).
  join()    waits for one request's workers, with a deadline, and returns
            their results. A worker past its deadline, or one the stall check
            catches, is cancelled and comes back as `timeout` / `stalled`
            with whatever it recorded — never silence.
  Nothing here sends a message. The coordinator's result turn turns the
  results into the ONE reply (coordinator.answer_deferred).

Up to MAX_BROWSE browser workers run at once, one tab each; the gateway
(skills/gateway.py) makes the home route exclusive, so a worker that escalates
waits for the VPN tabs to finish. Research and analysis run alongside freely.
"""
import asyncio
import os
import sys
import time
from datetime import datetime

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from skills import ledger, send_gate, worklog  # noqa: E402

# Seconds. A browse run can be two loops (VPN, then one escalation to the home
# route), each capped at browse.TIME_CAP, plus queueing behind another run.
DEADLINES = {"browse": 2 * 900 + 300, "research": 900, "analyze": 1500,
             "crosscheck": 600}
SKILL_KEYS = {"research": "research", "analyze": "analyze", "crosscheck": "crosscheck"}
RESULT_CHARS = 12000          # per worker, in the result turn's prompt

_running = {}                 # work_id -> {"task", "out", "cancel_reason", "kind"}
MAX_BROWSE = 3                # browser workers at once, one tab each
_browse_slot = None           # asyncio.Semaphore(MAX_BROWSE), created on first use


def _slot():
    global _browse_slot
    if _browse_slot is None:
        _browse_slot = asyncio.Semaphore(MAX_BROWSE)
    return _browse_slot


def launch(session, kind, task, parent_id=None, via=None, owner=None, rerun_of=None):
    """Start one worker for the current turn's request. Returns the work id.
    Must be called inside a turn (a tool call, a command, a result turn)."""
    if kind not in DEADLINES:
        raise ValueError(f"unknown worker kind {kind!r}")
    t = send_gate.raw()
    if t is None:
        raise RuntimeError("workers.launch called outside any turn")
    owner = owner or t.sender
    if not t.request_id:
        t.request_id = f"R{datetime.now():%m%d%H%M%S}-{t.id}"
    wid = ledger.start(t.request_id, kind, owner, task, request_text=t.text,
                       inbound_ts=t.ts, deadline_s=DEADLINES[kind], parent_id=parent_id,
                       rerun_of=rerun_of)
    out = {}
    entry = {"task": None, "out": out, "cancel_reason": None, "kind": kind,
             "started": time.time()}
    _running[wid] = entry
    entry["task"] = asyncio.create_task(_run(session, wid, kind, task, owner, out, via))
    t.workers.append(wid)
    worklog.log("worker_start", work=wid, kind=kind, task=task[:500],
                request=t.request_id, parent=parent_id, turn=t.id)
    print(f"🧰 worker {wid} ({kind}) started: {' '.join(task.split())[:80]}")
    return wid


def _browse_status(rec_status):
    if rec_status in ("crashed", "error"):
        return "failed"
    if rec_status == "cancelled":
        return "cancelled"
    return "done"


async def _run(session, wid, kind, task, owner, out, via):
    status, result, files, record, run_id, cost = "failed", "", [], None, None, None
    try:
        if kind == "browse":
            from skills import browse
            from skills.registry import cancel_flags
            import coordinator
            async with _slot():
                ledger.heartbeat(wid)
                res, label = await browse.browse_session(
                    session, owner, task, via=via,
                    send_signal=coordinator.send_signal,
                    cancel_flags=cancel_flags, out=out)
            report = out.get("report") or ""
            result = (f"FINDINGS (the worker's own summary):\n{res}\n\n"
                      f"RUN REPORT:\n{report}\n\nRoute: {label}")
            status = _browse_status(out.get("status"))
            files = (out.get("captures") or [])[-4:]
            record, run_id, cost = out.get("manifest"), out.get("run_id"), out.get("cost")
        else:
            from skills.registry import SKILL_REGISTRY
            answer, force_file = await SKILL_REGISTRY[SKILL_KEYS[kind]](
                session, owner, task, "", [])
            result = str(answer or "")
            status = "done" if result else "failed"
            if force_file and result:
                # The full report rides along as a file on the reply; Chip
                # writes the short answer from it.
                import coordinator
                os.makedirs(coordinator.REPORT_DIR, exist_ok=True)
                path = (f"{coordinator.REPORT_DIR}/{datetime.now():%Y%m%d_%H%M%S}_"
                        f"{kind}_{wid}.txt")
                with open(path, "w") as f:
                    f.write(result)
                files = [path]
    except asyncio.CancelledError:
        reason = (_running.get(wid) or {}).get("cancel_reason") or "cancelled"
        status = ("stalled" if "stall" in reason else
                  "timeout" if "deadline" in reason else "cancelled")
        result = (out.get("report") or "(no report was recorded)") + f"\n\nSTOPPED: {reason}"
        record, run_id = out.get("manifest"), out.get("run_id")
        files = (out.get("captures") or [])[-4:]
        # Swallowed on purpose: the join gets a result, not an exception.
    except Exception as e:
        status = "failed"
        result = ((out.get("report") or "") +
                  f"\n\nWORKER ERROR: {type(e).__name__}: {str(e)[:300]}").strip()
        record, run_id = out.get("manifest"), out.get("run_id")
        print(f"⚠️ worker {wid} failed: {type(e).__name__}: {e}")
    finally:
        try:
            ledger.finish(wid, status, result=result, files=files, record_path=record,
                          run_id=run_id, cost_usd=cost)
        except Exception as e:
            print(f"⚠️ ledger finish {wid}: {e}")
        worklog.log("worker_end", work=wid, kind=kind, status=status,
                    record=record, files=files, result=result[:4000])
        print(f"🧰 worker {wid} ({kind}) ended: {status}")
    return wid


async def join(wids, deadline_s=None):
    """Wait for these workers. Past the deadline, cancel what is left (each
    returns a `timeout` result). Returns ledger rows, in launch order."""
    entries = [(w, _running.get(w)) for w in wids]
    tasks = [e["task"] for _, e in entries if e and e.get("task")]
    if deadline_s is None:
        kinds = [e["kind"] for _, e in entries if e]
        n_browse = kinds.count("browse")
        deadline_s = max([DEADLINES[k] for k in kinds] or [900])
        if n_browse > MAX_BROWSE:             # the extras queue for a slot
            deadline_s = DEADLINES["browse"] * -(-n_browse // MAX_BROWSE)
    if tasks:
        done, pending = await asyncio.wait(tasks, timeout=deadline_s)
        for w, e in entries:
            if e and e["task"] in pending:
                cancel(w, f"deadline passed after {int(deadline_s)}s")
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
    rows = []
    for w in wids:
        r = ledger.get(w) or {"id": w, "status": "lost", "result": "", "kind": "?",
                              "task": "", "files": []}
        rows.append(r)
        _running.pop(w, None)
    return rows


def cancel(wid, reason):
    e = _running.get(wid)
    if not e or not e.get("task") or e["task"].done():
        return False
    e["cancel_reason"] = reason
    e["task"].cancel()
    worklog.log("worker_cancel", work=wid, reason=reason)
    print(f"🧰 worker {wid} cancelled: {reason}")
    return True


def cancel_by_run_id(run_id, reason):
    """The stall check finds a browse run by its manifest's run_id."""
    for wid, e in list(_running.items()):
        if (e.get("out") or {}).get("run_id") == run_id:
            return cancel(wid, reason)
    return False


def in_flight(owner=None):
    """Live view for Chip's work_status tool: ledger rows plus, for browse
    runs, how far the run has got according to its manifest."""
    rows = ledger.running(owner)
    lines = []
    for r in rows:
        e = _running.get(r["id"]) or {}
        age = int(time.time() - e["started"]) if e.get("started") else None
        line = (f"{r['id']} {r['kind']} running"
                + (f" {age // 60}m{age % 60:02d}s" if age is not None else "")
                + f" — {(r['task'] or '')[:100]}")
        man = (e.get("out") or {}).get("manifest")
        if man and os.path.exists(man):
            try:
                import json
                m = json.load(open(man))
                line += (f" | step {m.get('last_step')}, last action "
                         f"{str(m.get('last_action', ''))[:60]}")
            except Exception:
                pass
        if r["id"] not in _running:
            line += " | not in this process (lost on a restart?)"
        lines.append(line)
    return "\n".join(lines) if lines else "Nothing is running."
