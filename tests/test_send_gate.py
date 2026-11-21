"""Send gate (One Voice spec A.1): one inbound message, one reply.

Standalone, no pytest. Imports the real coordinator with its Signal transport
replaced by a capture list, and the work log pointed at a temp dir.
Exits non-zero on failure.
"""
import asyncio
import os
import sys
import os as _os_wl, tempfile as _tf_wl
_os_wl.environ.setdefault("CHIP_WORKLOG_DIR", _tf_wl.mkdtemp(prefix="worklog_test_"))  # keep test traffic out of data/worklog
_os_wl.environ.setdefault("CHIP_WEBLOG_DIR", _tf_wl.mkdtemp(prefix="weblog_test_"))
_os_wl.environ.setdefault("CHIP_WEB_DB", _tf_wl.mkdtemp(prefix="webdb_test_") + "/web.db")
_os_wl.environ.setdefault("CHIP_WORK_DB", _tf_wl.mkdtemp(prefix="workdb_test_") + "/work.db")
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from skills import send_gate, worklog  # noqa: E402

TMP = tempfile.mkdtemp(prefix="gate_test_")
os.environ["CHIP_WORKLOG_DIR"] = TMP
worklog.WORKLOG_DIR = TMP

import coordinator  # noqa: E402

SENT = []    # (recipient, text)
FILES = []   # (recipient, caption)


async def fake_raw(session, text, target):
    SENT.append((target, text))
    return True


async def fake_file(session, filepath, caption, recipient=None, kind=None, _gated=False):
    if not _gated:
        ok, _k, _w = send_gate.decide(recipient, caption, kind=kind or "file")
        if not ok:
            return False
    FILES.append((recipient, caption))
    return True


coordinator._send_signal_raw = fake_raw
coordinator.send_signal_file = fake_file
coordinator.REPORT_DIR = TMP

A = "+15555550101"
B = "+15555550102"
FAILS = []


def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")
    if not cond:
        FAILS.append(f"{name}: {detail}")


def violations():
    return [r for r in worklog.read(event="send") if r.get("violation")]


def reset(mode="observe"):
    SENT.clear(); FILES.clear()
    for f in os.listdir(TMP):
        os.remove(os.path.join(TMP, f))
    send_gate.mode = lambda: mode


async def case_one_reply():
    reset()
    tok = send_gate.begin(A, "123")
    await coordinator.deliver_response(None, "the answer", A, tag="[Opus]")
    send_gate.end(tok)
    check("one reply is sent", len(SENT) == 1, SENT)
    check("one reply is not a violation", not violations(), violations())


async def case_second_reply_observe():
    reset("observe")
    tok = send_gate.begin(A, "123")
    await coordinator.deliver_response(None, "the answer", A)
    await coordinator.send_signal(None, "and another thing", recipient=A)
    send_gate.end(tok)
    v = violations()
    check("observe: both messages still go out", len(SENT) == 2, SENT)
    check("observe: the second reply is logged as a violation",
          len(v) == 1 and "reply #2" in v[0]["violation"], v)


async def case_second_reply_enforce():
    reset("enforce")
    tok = send_gate.begin(A, "123")
    await coordinator.deliver_response(None, "the answer", A)
    await coordinator.send_signal(None, "and another thing", recipient=A)
    send_gate.end(tok)
    check("enforce: the second reply is refused", len(SENT) == 1 and SENT[0][1] == "the answer", SENT)


async def case_notes_fold():
    reset()
    tok = send_gate.begin(A, "123")
    send_gate.add_note("Staged FIX-0099: test. Approve with 'fix: approve FIX-0099'.")
    send_gate.add_note("Thinking effort back to high.")
    await coordinator.deliver_response(None, "Done.", A)
    send_gate.end(tok)
    check("notes ride on the one reply", len(SENT) == 1 and "FIX-0099" in SENT[0][1]
          and "effort back" in SENT[0][1], SENT)
    check("notes are not a violation", not violations(), violations())


async def case_chunked_counts_once():
    reset()
    long = ("word " * 1200).strip()          # ~6000 chars: chunked or attached
    tok = send_gate.begin(A, "123")
    await coordinator.deliver_response(None, long[:5000], A)
    send_gate.end(tok)
    check("a chunked reply is several sends", len(SENT) >= 2, len(SENT))
    check("a chunked reply is one reply", not violations(), violations())


async def case_late_reply_is_caught():
    reset()
    started = asyncio.Event()

    async def bg():
        await started.wait()
        await coordinator.deliver_response(None, "late result", A)

    tok = send_gate.begin(A, "123")
    task = asyncio.create_task(bg())             # copies the turn context
    await coordinator.send_signal(None, "browser started", recipient=A)
    send_gate.end(tok)
    started.set()
    await task
    v = violations()
    check("a late result after an immediate reply is reply #2",
          len(v) == 1 and "reply #2" in v[0]["violation"], v)


async def case_outside_turn():
    reset()
    send_gate.detach()
    await coordinator.send_signal(None, "orphan", recipient=A)
    await coordinator.send_signal(None, "rain at 4pm", recipient=A, kind="scheduled")
    v = violations()
    check("an unclassified send outside a turn is a violation",
          len(v) == 1 and "unclassified" in v[0]["violation"], v)
    check("a scheduled send outside a turn is fine", len(SENT) == 2, SENT)


async def case_relay():
    reset()
    tok = send_gate.begin(A, "123")
    await coordinator.deliver_response(None, "message for B", B, cc_sender=A)
    send_gate.end(tok)
    kinds = [r["kind"] for r in worklog.read(event="send")]
    check("a message to someone else is a relay; the copy is the reply",
          kinds == ["relay", "reply"] and not violations(), (kinds, violations()))


async def case_internal_turn_is_scheduled():
    reset()
    tok = send_gate.begin(A, "123", internal=True)
    await coordinator.deliver_response(None, "your 9am briefing", A)
    send_gate.end(tok)
    kinds = [r["kind"] for r in worklog.read(event="send")]
    check("a scheduled job's message is `scheduled`", kinds == ["scheduled"], kinds)


async def main():
    for c in (case_one_reply, case_second_reply_observe, case_second_reply_enforce,
              case_notes_fold, case_chunked_counts_once, case_late_reply_is_caught,
              case_outside_turn, case_relay, case_internal_turn_is_scheduled):
        print(c.__name__)
        await c()


asyncio.run(main())
if FAILS:
    print("\nFAILED:\n  " + "\n  ".join(FAILS))
    sys.exit(1)
print("\nall checks passed")
