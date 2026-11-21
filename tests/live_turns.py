"""Live turn check: real worker loop, real models, captured Signal transport.

NOT part of the standalone suite (no test_ prefix): it calls the real model
APIs (a few cents) and needs the network. It is the check the stubbed tests
cannot make — the FIX-0006 404 passed every stubbed test.

For each inbound message it counts what would have reached the phone and
fails if any message produced more than one outbound message (reactions and
typing indicators excluded). Conversation history, buffers, fact extraction
and taxonomy are stubbed so nothing here lands in a real user's record.

    venv/bin/python3 tests/live_turns.py            # default cases
    venv/bin/python3 tests/live_turns.py "msg" ...  # your own messages, as the admin
"""
import asyncio
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
os.environ["CHIP_WORKLOG_DIR"] = tempfile.mkdtemp(prefix="live_turns_")
os.environ["CHIP_WORK_DB"] = os.path.join(os.environ["CHIP_WORKLOG_DIR"], "work.db")

import aiohttp  # noqa: E402
import coordinator as c  # noqa: E402
import chip_config  # noqa: E402
from skills import worklog  # noqa: E402

ADMIN = chip_config.ADMIN_PHONE
OUT = []          # (recipient, text)
REACT = []


async def raw(session, text, target):
    OUT.append((target, text)); return True


async def file_send(session, filepath, caption, recipient=None, kind=None, _gated=False):
    if not _gated:
        from skills import send_gate
        ok, _k, _w = send_gate.decide(recipient, caption, kind=kind or "file")
        if not ok:
            return False
    OUT.append((recipient, f"[file] {os.path.basename(filepath)} {caption}")); return True


async def reaction(recipient, ts, emoji, remove=False):
    REACT.append(emoji)


async def nothing(*a, **k):
    return None


c._send_signal_raw = raw
c.send_signal_file = file_send
c.send_reaction = reaction
c.send_typing = nothing
c.log_to_buffer = lambda *a, **k: None
c.maybe_extract_facts = nothing
c.taxonomy.enqueue_work = lambda *a, **k: None
import skills.history as _h  # noqa: E402
_h.maybe_summarize = lambda *a, **k: False
_h.log_turn = lambda *a, **k: None      # every history write, deferred turns included

DEFAULT = ["quick: what is 17 times 23",
           "fix: status",
           "fix: status FIX-0003"]


FAILURE_WORDING = ("failed on my end", "It's in the log", "didn't get a usable answer",
                   "couldn't put an answer together")


async def main(msgs, expect=None):
    fails = []
    async with aiohttp.ClientSession() as s:
        w = asyncio.create_task(c.worker(s))
        for i, m in enumerate(msgs):
            OUT.clear(); REACT.clear()
            await c.message_queue.put({"sender": ADMIN, "text": m, "ts": str(1000 + i)})
            await c.message_queue.join()
            await asyncio.sleep(0.5)
            if "⏳" in REACT:          # deferred: wait for the result turn
                for _ in range(1500):
                    if REACT[-1] in ("✅", "❌"):
                        break
                    await asyncio.sleep(1)
            n = sum(1 for _, t in OUT if not t.startswith("[file]"))
            nf = len(OUT) - n
            print(f"\n>>> {m}\n    {n} message(s) + {nf} attachment(s), reactions {REACT or '-'}")
            for _, t in OUT:
                print("    | " + t[:300].replace("\n", "\n    | "))
            if n != 1:
                fails.append(f"{m!r}: {n} text messages")
            texts = " ".join(t for _, t in OUT if not t.startswith("[file]"))
            if any(w in texts for w in FAILURE_WORDING):
                fails.append(f"{m!r}: the reply is a failure message")
            # --expect "4|four": any alternative, case-insensitive
            if expect and not any(alt.lower() in texts.lower() for alt in expect.split("|")):
                fails.append(f"{m!r}: the reply does not contain {expect!r}")
        w.cancel()
    v = [r for r in worklog.read(event="send") if r.get("violation")]
    print(f"\ngate violations logged: {len(v)}")
    for r in v:
        print(f"    {r['violation']} | {r['preview'][:80]}")
    if fails or v:
        print("\nFAILED: " + "; ".join(fails + [f"{len(v)} gate violation(s)"]))
        sys.exit(1)
    print("\nall messages answered exactly once")


_args = sys.argv[1:]
_expect = None
if _args[:1] == ["--expect"] and len(_args) >= 2:
    _expect, _args = _args[1], _args[2:]
asyncio.run(main(_args or DEFAULT, _expect))
