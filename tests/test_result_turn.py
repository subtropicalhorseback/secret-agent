"""Result turn (One Voice spec B.2): workers report to Chip; the user gets ONE reply.

Standalone, no pytest. Runs the real coordinator worker loop with the Signal
transport captured, the model calls stubbed (the live check for real models is
tests/live_turns.py), the browser worker replaced by a fake, and the ledger,
work log and history pointed at temp locations. Exits non-zero on failure.
"""
import asyncio
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
TMP = tempfile.mkdtemp(prefix="result_turn_test_")
os.environ["CHIP_WORKLOG_DIR"] = os.path.join(TMP, "worklog")
os.environ.setdefault("CHIP_WEBLOG_DIR", tempfile.mkdtemp(prefix="weblog_test_"))
os.environ.setdefault("CHIP_WEB_DB", tempfile.mkdtemp(prefix="webdb_test_") + "/web.db")
os.environ["CHIP_WORK_DB"] = os.path.join(TMP, "work.db")

import coordinator as c  # noqa: E402
import chip_config  # noqa: E402
from skills import ledger, tool_calling, workers, browse  # noqa: E402
import skills.history as hist  # noqa: E402

ADMIN = chip_config.ADMIN_PHONE
OUT, REACT, HIST, PROMPTS, ALLOWED = [], [], [], [], []
FAILS = []


def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")
    if not cond:
        FAILS.append(f"{name}: {detail}")


async def raw(session, text, target):
    OUT.append(text); return True


async def reaction(recipient, ts, emoji, remove=False):
    REACT.append(emoji)


async def nothing(*a, **k):
    return None


c._send_signal_raw = raw
c.send_reaction = reaction
c.send_typing = nothing
c.maybe_extract_facts = nothing
c.taxonomy.enqueue_work = lambda *a, **k: None
c.load_history = lambda *a, **k: []
hist.log_turn = lambda user, role, content: HIST.append((role, content[:80]))
hist.maybe_summarize = lambda *a, **k: False
c.REPORT_DIR = TMP

# ── fake browser worker ──────────────────────────────────────────────────────
BROWSE_DELAY = {"s": 0.05}


async def fake_browse_session(session, sender, task, via=None, route=None,
                              send_signal=None, cancel_flags=None, out=None):
    out.update({"run_id": f"run-{len(task)}", "manifest": os.path.join(TMP, "m.json"),
                "status": "running"})
    await asyncio.sleep(BROWSE_DELAY["s"])
    price = "$499" if "best buy" in task.lower() else "$479"
    out.update({"report": f"✅ Browse finished — task: {task[:60]}", "status": "completed",
                "captures": []})
    return f"Found {price} for the console.", "via Mullvad"

browse.browse_session = fake_browse_session
browse.service_up = lambda: True

# ── fake models ──────────────────────────────────────────────────────────────
SCRIPT = {"normal": None, "result": []}


async def fake_semantic_dispatch(session, sender, payload, history, length, ctx=""):
    """The normal turn: the model starts workers via the real tool path."""
    return await SCRIPT["normal"](sender), "[Opus]"


async def fake_ask_cloud(session, user_text, sender, tier="cloud_default", rag_context=None,
                         rag_facts=None, history=None, sections=None, length="default",
                         fallback=False, allowed_tools=None):
    """Only the result turn calls ask_cloud directly in these tests."""
    PROMPTS.append(user_text)
    ALLOWED.append(allowed_tools)
    tok = tool_calling.ALLOWED_TOOLS.set(frozenset(allowed_tools) if allowed_tools else None)
    try:
        step = SCRIPT["result"].pop(0)
        return await step(sender), "[Opus]"
    finally:
        tool_calling.ALLOWED_TOOLS.reset(tok)

c.semantic_dispatch = fake_semantic_dispatch
c.ask_cloud = fake_ask_cloud


async def tool(name, inp, sender):
    return await tool_calling.execute_tool(
        name, inp, sender=sender, name="admin", active_voices={}, retrieve_rag_fn=None,
        session=None, cat_whitelist=[], tier="admin", set_effort_fn=None)


def reset():
    OUT.clear(); REACT.clear(); HIST.clear(); PROMPTS.clear(); ALLOWED.clear()


async def run_message(text, wait=8.0):
    w = asyncio.create_task(c.worker(None))
    await c.message_queue.put({"sender": ADMIN, "text": text, "ts": "777"})
    await c.message_queue.join()
    # the deferred answer runs as its own task
    for _ in range(int(wait / 0.05)):
        if REACT and REACT[-1] in ("✅", "❌"):
            break
        await asyncio.sleep(0.05)
    w.cancel()


async def case_two_lookups_one_reply():
    print("case_two_lookups_one_reply")
    reset()

    async def normal(sender):
        r1 = await tool("browse_task", {"task": "xbox price at best buy"}, sender)
        r2 = await tool("browse_task", {"task": "xbox price at target"}, sender)
        assert "started" in r1 and "started" in r2, (r1, r2)
        return "Checking both stores now."          # must NOT be sent

    async def result(sender):
        return "Best Buy has it at $499 and Target at $479."

    SCRIPT["normal"], SCRIPT["result"] = normal, [result]
    await run_message("find xbox series x prices at best buy and target")
    check("exactly one message reached the phone", len(OUT) == 1, OUT)
    check("it is the result-turn answer", OUT and "$479" in OUT[0], OUT)
    check("the 'checking both stores' draft was not sent",
          not any("Checking both" in o for o in OUT), OUT)
    check("one result turn saw both workers' results",
          len(PROMPTS) == 1 and PROMPTS[0].count("```worker-report") == 2, PROMPTS[:1])
    check("the result turn quotes the user's message",
          PROMPTS and "best buy and target" in PROMPTS[0], PROMPTS[:1])
    check("the result turn ran with the read-only tool list",
          ALLOWED and ALLOWED[0] == tool_calling.RESULT_TURN_TOOLS, ALLOWED)
    check("⏳ then ✅ on the user's message", REACT[:1] == ["⏳"] and REACT[-1] == "✅", REACT)
    check("history: the user's turn, then Chip's answer",
          [r for r, _ in HIST] == ["user", "assistant"], HIST)
    rows = [r for r in ledger.recent(hours=1)
            if r["request_text"] == "find xbox series x prices at best buy and target"]
    check("the ledger recorded both workers as done",
          len(rows) == 2 and all(r["status"] == "done" for r in rows), rows)


async def case_followup_still_one_reply():
    print("case_followup_still_one_reply")
    reset()

    async def normal(sender):
        await tool("browse_task", {"task": "xbox price at target"}, sender)
        return ""

    async def result_1(sender):
        r = await tool("instruct_worker", {"instruction": "try best buy instead for the same console"},
                       sender)
        assert "sent back out" in r, r
        return "Trying another store."              # must NOT be sent

    async def result_2(sender):
        return "Target didn't have it; Best Buy has it at $499."

    SCRIPT["normal"], SCRIPT["result"] = normal, [result_1, result_2]
    await run_message("where can i get an xbox series x")
    check("exactly one message reached the phone", len(OUT) == 1, OUT)
    check("it is the final answer", OUT and "$499" in OUT[0], OUT)
    check("two result turns ran", len(PROMPTS) == 2, len(PROMPTS))
    fu = [r for r in ledger.recent(hours=1) if r["parent_id"]]
    check("the follow-up is one ledger row with a parent",
          len(fu) == 1 and fu[0]["status"] == "done", fu)
    check("the follow-up was counted against its request",
          fu and ledger.followups_used(fu[0]["request_id"]) == 1, fu)


async def case_instruct_worker_refused_in_normal_turn():
    print("case_instruct_worker_refused_in_normal_turn")
    from skills import send_gate
    tok = send_gate.begin(ADMIN, "1", text="x")
    try:
        r = await tool("instruct_worker", {"instruction": "go do something unbounded"}, ADMIN)
    finally:
        send_gate.end(tok)
    check("instruct_worker is refused outside a result turn", "not available" in r, r)
    names = [t["name"] for t in tool_calling.tools_for("admin")]
    check("instruct_worker is not in the normal tool list", "instruct_worker" not in names)


async def case_hung_worker_times_out():
    print("case_hung_worker_times_out")
    reset()
    BROWSE_DELAY["s"] = 60
    old = dict(workers.DEADLINES)
    workers.DEADLINES["browse"] = 1

    async def normal(sender):
        await tool("browse_task", {"task": "a page that never loads"}, sender)
        return ""

    async def result(sender):
        return "The page never loaded; I stopped after the time limit."

    SCRIPT["normal"], SCRIPT["result"] = normal, [result]
    try:
        await run_message("check the slow page", wait=10)
    finally:
        BROWSE_DELAY["s"] = 0.05
        workers.DEADLINES.update(old)
    check("one reply, not silence", len(OUT) == 1, OUT)
    check("the result turn was told it timed out",
          PROMPTS and "status timeout" in PROMPTS[0], PROMPTS[:1])


async def case_restart_recovery():
    print("case_restart_recovery")
    from datetime import datetime, timedelta
    reset()
    # A recent request and an old one, both 'running' under a dead process.
    recent = ledger.start("R-recent", "browse", ADMIN, "xbox price at best buy",
                          request_text="find an xbox", inbound_ts="901")
    old = ledger.start("R-old", "browse", ADMIN, "coffee price at the store",
                       request_text="what's coffee going for", inbound_ts="902")
    ledger.update(recent, pid=1)
    ledger.update(old, pid=1, started_at=(datetime.now() - timedelta(hours=2)).isoformat(timespec="seconds"))
    lost = ledger.mark_lost_on_startup()
    check("both are marked lost at startup", sorted(r["id"] for r in lost) == sorted([recent, old]), lost)

    async def result(sender):
        return "Best Buy has it at $499."
    SCRIPT["result"] = [result]
    c.asyncio.sleep = _fast_sleep
    try:
        await c._recover_lost(None, lost)
        for _ in range(100):
            if REACT and REACT[-1] == "✅":
                break
            await asyncio.sleep(0.05)
    finally:
        c.asyncio.sleep = _real_sleep
    texts = " | ".join(OUT)
    check("the recent request was rerun and answered once",
          sum("$499" in o for o in OUT) == 1, OUT)
    check("the old request got one line saying it was lost",
          sum("lost it" in o and "coffee" in o for o in OUT) == 1, OUT)
    check("exactly two messages in total", len(OUT) == 2, OUT)
    reruns = [r for r in ledger.for_request("R-recent") if r.get("rerun_of")]
    check("the rerun is marked in the ledger", len(reruns) == 1, ledger.for_request("R-recent"))
    # A second restart must not rerun it again.
    ledger.update(reruns[0]["id"], status="running", pid=1)
    lost2 = ledger.mark_lost_on_startup()
    OUT.clear()
    c.asyncio.sleep = _fast_sleep
    try:
        await c._recover_lost(None, lost2)
    finally:
        c.asyncio.sleep = _real_sleep
    check("a request already rerun once is not rerun again",
          len(OUT) == 1 and "lost it" in OUT[0], OUT)


_real_sleep = asyncio.sleep


async def _fast_sleep(t, *a, **k):
    return await _real_sleep(min(t, 0.01))


async def main():
    await case_restart_recovery()
    await case_two_lookups_one_reply()
    await case_followup_still_one_reply()
    await case_instruct_worker_refused_in_normal_turn()
    await case_hung_worker_times_out()


asyncio.run(main())
if FAILS:
    print("\nFAILED:\n  " + "\n  ".join(FAILS))
    sys.exit(1)
print("\nall checks passed")
