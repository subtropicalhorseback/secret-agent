"""Human-only steps through Chip, the look tool, and attach_file (One Voice spec C.6–C.9).

Standalone, no pytest. The browser service, the model, the vision reader and
Signal are faked; captures go to a temp dir. Exits non-zero on failure.
"""
import asyncio
import base64
import json
import os
import sys
import tempfile
import types

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
TMP = tempfile.mkdtemp(prefix="asks_test_")
for k, v in {"CHIP_WORKLOG_DIR": "worklog", "CHIP_WEBLOG_DIR": "weblog",
             "CHIP_WEB_DB": "web.db", "CHIP_WORK_DB": "work.db",
             "CHIP_BROWSE_CAPTURE_ROOT": "captures"}.items():
    os.environ[k] = os.path.join(TMP, v)

import coordinator as c  # noqa: E402
import chip_config  # noqa: E402
from skills import browse, browse_obs, browse_vision, asks, ledger, send_gate, tool_calling  # noqa: E402
import skills.llm as llm  # noqa: E402

ADMIN = chip_config.ADMIN_PHONE
FAILS = []
SENT, FILES = [], []
PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmM"
                       "IQAAAABJRU5ErkJggg==")
PAGE = {"text": "console $499"}


def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")
    if not cond:
        FAILS.append(f"{name}: {detail}")


class _Blk:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class _Resp:
    def __init__(self, blocks):
        self.content, self.stop_reason, self.usage = blocks, "tool_use", None


def fake_service(payload, timeout=90):
    op = payload.get("op")
    if op == "screenshot":
        return {"ok": True, "image_b64": base64.b64encode(PNG).decode(),
                "media_type": "image/png", "url": "https://shop.example.com/p"}
    if op in ("open", "read"):
        return {"ok": True, "status": 200, "url": "https://shop.example.com/p",
                "title": "Shop", "text": PAGE["text"], "elements": "[1] buy"}
    return {"ok": True}


def install(script, tool_results):
    steps = {"i": 0}

    def fake_llm(messages, *a, **kw):
        # record the tool results the worker received
        last = messages[-1]["content"] if messages else []
        if isinstance(last, list):
            for b in last:
                if isinstance(b, dict) and b.get("type") == "tool_result":
                    tool_results.append(str(b.get("content")))
        i = steps["i"]; steps["i"] += 1
        name, inp = script[i] if i < len(script) else ("finish", {"summary": "done"})
        return _Resp([_Blk(type="tool_use", name=name, input=inp, id=f"t{i}")])

    llm.claude_raw_sync = fake_llm
    browse.service_request = fake_service
    browse.service_up = lambda: True
    browse.current_route = lambda: "mullvad"
    browse.exit_node_label = lambda: "test-exit"
    browse_obs.reap = lambda dry_run=False: {"killed": [], "locks_cleared": [], "survivors": []}
    browse_obs.RunRecorder._write_artifact = lambda self, r: "(stubbed)"
    browse_obs.RunRecorder._beat = lambda self: None
    browse._human_pause = lambda: asyncio.sleep(0)


async def fake_send_signal(session, text, recipient=None, kind=None):
    SENT.append((kind, text)); return True


async def fake_send_file(session, path, caption, recipient=None, kind=None, _gated=False):
    FILES.append((kind, path, caption)); return True


c.send_signal = fake_send_signal
c.send_signal_file = fake_send_file


async def case_look():
    print("case_look")
    calls = []
    browse_vision.read_capture = lambda path, q, **k: calls.append((path, q)) or [
        {"rank": 1, "reading": "$449.99", "confidence": 0.91, "why": "price badge"},
        {"rank": 2, "reading": "$449.00", "confidence": 0.05, "why": "blur"}]
    results = []
    PAGE["text"] = "console page, price shown in an image"
    install([("open", {"url": "https://shop.example.com/p"}),
             ("look", {"question": "What price is in the badge?"}),
             ("finish", {"summary": "It is $449.99"})], results)
    out = {}
    await browse.browse_session(None, ADMIN, "price of the console", route="vpn",
                                send_signal=fake_send_signal, out=out)
    check("look consulted the vision reader with the worker's question",
          calls and calls[0][1] == "What price is in the badge?" and os.path.exists(calls[0][0]),
          calls)
    check("the worker got ranked readings back",
          any("$449.99" in r and "readings" in r for r in results), results)
    check("the look is visible in the run report",
          "Vision look" in (out.get("report") or "") and "$449.99" in out["report"], out.get("report"))
    check("look sent nothing to Signal", not SENT and not FILES, (SENT, FILES))


async def case_look_refuses_captcha():
    print("case_look_refuses_captcha")
    calls = []
    browse_vision.read_capture = lambda path, q, **k: calls.append(q) or []
    results = []
    PAGE["text"] = "Please verify you are human. <div class='g-recaptcha'></div>"
    install([("open", {"url": "https://shop.example.com/p"}),
             ("read", {}),
             ("look", {"question": "What are the letters?"}),
             ("finish", {"summary": "blocked by a check"})], results)
    await browse.browse_session(None, ADMIN, "price", route="vpn",
                                send_signal=fake_send_signal, out={})
    check("look is refused on a human-check page", not calls and
          any("human check" in r and "handoff" in r for r in results), (calls, results))


async def case_handoff_round_trip():
    print("case_handoff_round_trip")
    SENT.clear(); FILES.clear()
    results = []
    PAGE["text"] = "Please verify you are human. <div class='g-recaptcha'></div>"
    install([("open", {"url": "https://shop.example.com/p"}),
             ("handoff", {"reason": "captcha: the site shows a reCAPTCHA image"}),
             ("finish", {"summary": "done after the check"})], results)

    async def owner_answers():
        for _ in range(200):
            if asks.pending(ADMIN):
                break
            await asyncio.sleep(0.01)
        a = asks.pending(ADMIN)
        # Chip's decision on the real inbound message
        ok = await c._ask_answer(a, "XK7P2")
        asks.answer(ADMIN, "XK7P2")
        return a, ok

    t = asyncio.create_task(owner_answers())
    out = {}
    await browse.browse_session(None, ADMIN, "price", route="vpn",
                                send_signal=fake_send_signal, out=out)
    a, ok = await t
    check("the ask went out as a picture with one line, kind=ask",
          len(FILES) == 1 and FILES[0][0] == "ask" and FILES[0][1].endswith(".png")
          and "human check" in FILES[0][2], FILES)
    check("no live-view link and no 'resume' in anything sent",
          not any("6080" in str(x) or "resume" in str(x).lower() for x in FILES + SENT),
          (FILES, SENT))
    check("Chip judged the short reply to be the answer", ok, ok)
    check("the worker received the answer as the handoff's result",
          any("The admin answered: 'XK7P2'" in r for r in results), results)
    check("the ask is closed afterwards", asks.pending(ADMIN) is None)


async def case_not_the_answer():
    print("case_not_the_answer")
    a = types.SimpleNamespace(question="The site wants a human check. What does the picture say?")
    llm.gemini_sync = lambda *a_, **k: "no"
    ok = await c._ask_answer(a, "what's the weather supposed to do tomorrow afternoon?")
    check("a different question is not treated as the answer", ok is False, ok)
    llm.gemini_sync = lambda *a_, **k: (_ for _ in ()).throw(RuntimeError("offline"))
    ok = await c._ask_answer(a, "the letters look like XK7P2 but I'm not sure about the 7")
    check("if the classifier fails, a long message is NOT typed into the site", ok is False, ok)
    check("'skip' always counts as an answer", await c._ask_answer(a, "skip"))
    check("a single token like a CAPTCHA answer skips the classifier",
          await c._ask_answer(a, "XK7P2"))
    check("'hows it going' is not typed into the site when the classifier is down",
          await c._ask_answer(a, "hows it going") is False)
    check("'stop:' counts as stop, not as text to type", await c._ask_answer(a, "stop:"))


async def case_attach_file():
    print("case_attach_file")
    shot = os.path.join(TMP, "step006.png")
    open(shot, "wb").write(PNG)
    tok = send_gate.begin(ADMIN, "1", text="screenshot of the shop page")
    try:
        t = send_gate.raw()
        t.request_id = "R-attach"
        wid = ledger.start("R-attach", "browse", ADMIN, "screenshot the shop page")
        ledger.finish(wid, "done", result="ok", files=[shot])
        allow = tool_calling.ALLOWED_TOOLS.set(tool_calling.RESULT_TURN_TOOLS)
        try:
            r1 = await tool_calling.execute_tool("attach_file", {"name": "step006.png"},
                sender=ADMIN, name="x", active_voices={}, retrieve_rag_fn=None, tier="admin")
            r2 = await tool_calling.execute_tool("attach_file", {"name": "../../.env"},
                sender=ADMIN, name="x", active_voices={}, retrieve_rag_fn=None, tier="admin")
        finally:
            tool_calling.ALLOWED_TOOLS.reset(allow)
        check("a result file can be attached", "will be attached" in r1 and t.attachments == [shot],
              (r1, t.attachments))
        check("anything else is refused", "not one of this request's files" in r2, r2)
    finally:
        send_gate.end(tok)
    r3 = await tool_calling.execute_tool("attach_file", {"name": "step006.png"},
        sender=ADMIN, name="x", active_voices={}, retrieve_rag_fn=None, tier="admin")
    check("attach_file is not available outside a result turn", "not available" in r3, r3)


async def main():
    await case_look()
    await case_look_refuses_captcha()
    await case_handoff_round_trip()
    await case_not_the_answer()
    await case_attach_file()


asyncio.run(main())
if FAILS:
    print("\nFAILED:\n  " + "\n  ".join(FAILS))
    sys.exit(1)
print("\nall checks passed")
