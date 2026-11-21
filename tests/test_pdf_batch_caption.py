#!/usr/bin/env python3
"""media_dispatch_batch: a caption sent with several PDFs reaches the model.

Run:  venv/bin/python3 tests/test_pdf_batch_caption.py

Before 20 Nov 2026 the batch path called process_pdf_upload with no caption,
no domain and no info dict, so two PDFs sent under one instruction came back
as two bare ingest receipts and the instruction was dropped on the floor. The
single-attachment path (media_dispatch) had always handled it.

These drive the real media_dispatch_batch with the network, the daemon and the
PDF reader stubbed out, and assert on what the chat model was asked.
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

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import coordinator


class Harness:
    """Replace everything media_dispatch_batch reaches outside itself, and
    record what it tried to do."""

    def __init__(self):
        self.asked     = []      # (prompt, tier) handed to the chat model
        self.delivered = []      # (text, tag) sent back to the user
        self.ingested  = []      # (filename, domain) handed to the PDF ingest

    def __enter__(self):
        self.dir = tempfile.TemporaryDirectory()
        att_dir  = os.path.join(self.dir.name, "attachments")
        os.makedirs(att_dir, exist_ok=True)
        self._saved = {k: getattr(coordinator, k) for k in
                       ("SIGNAL_CONFIG_DIR", "SYNCTHING_DIR", "send_signal",
                        "process_pdf_upload", "ask_cloud", "deliver_response",
                        "log_to_buffer", "load_history", "load_context")}
        coordinator.SIGNAL_CONFIG_DIR = self.dir.name
        coordinator.SYNCTHING_DIR     = os.path.join(self.dir.name, "sync")

        async def send_signal(session, text, recipient=None):
            return True

        async def process_pdf_upload(session, sender, save_path, raw_filename,
                                     domain="unclassified", info=None):
            assert save_path.endswith(".part"), \
                f"an upload must land on .part, not in the watched folder: {save_path}"
            self.ingested.append((raw_filename, domain))
            name = "20261120_" + raw_filename.split("_")[-1].replace(".pdf", "") + ".pdf"
            if info is not None:
                info["filename"] = name
                info["text"]     = f"FULL TEXT OF {raw_filename}"
                info["path"]     = save_path[:-5]
            return f"PDF ingested: {name}"

        async def ask_cloud(session, prompt, sender, tier=None, history=None, **kw):
            self.asked.append((prompt, tier))
            return "the model's answer", "[Opus]"

        async def deliver_response(session, text, sender, tag=None, **kw):
            self.delivered.append((text, tag))

        coordinator.send_signal        = send_signal
        coordinator.process_pdf_upload = process_pdf_upload
        coordinator.ask_cloud          = ask_cloud
        coordinator.deliver_response   = deliver_response
        coordinator.log_to_buffer      = lambda *a, **k: None
        coordinator.load_history       = lambda *a, **k: []
        coordinator.load_context       = lambda *a, **k: "system"
        return self

    def __exit__(self, *exc):
        for k, v in self._saved.items():
            setattr(coordinator, k, v)
        self.dir.cleanup()

    def attach(self, att_id, name):
        """Write a fake inbound attachment where the coordinator will find it."""
        path = os.path.join(self.dir.name, "attachments", att_id)
        open(path, "wb").write(b"%PDF-1.4 fake " + name.encode())
        return {"contentType": "application/pdf", "id": att_id, "filename": name}


SENDER = "+15555550199"


async def case_caption_reaches_both_pdfs():
    with Harness() as h:
        atts = [h.attach("aaaa1111", "a.pdf"), h.attach("bbbb2222", "b.pdf")]
        await coordinator.media_dispatch_batch(None, SENDER,
                                               "which of these two quotes is cheaper?",
                                               atts, "1795195204322")
    assert len(h.asked) == 1, f"expected one chat call for the pair, got {len(h.asked)}"
    prompt, _tier = h.asked[0]
    assert prompt.startswith("which of these two quotes is cheaper?"), prompt[:120]
    assert "FULL TEXT OF 1795195204322_0199_1111.pdf" in prompt, "first PDF's text missing"
    assert "FULL TEXT OF 1795195204322_0199_2222.pdf" in prompt, "second PDF's text missing"
    assert "2 PDFs" in prompt, "the model was not told how many documents it has"
    assert any("Saved as" in t for t, _ in h.delivered), "no record of what was saved"
    return "a caption with two PDFs reaches the model with both documents"


async def case_no_caption_keeps_receipts():
    """Unchanged behaviour: nothing typed, so the ingest receipts are the reply
    and no chat call is made."""
    with Harness() as h:
        atts = [h.attach("aaaa1111", "a.pdf"), h.attach("bbbb2222", "b.pdf")]
        await coordinator.media_dispatch_batch(None, SENDER, "", atts, "1795195204322")
    assert not h.asked, "a chat call was made with no caption to act on"
    assert len(h.delivered) == 2, f"expected two receipts, got {h.delivered}"
    return "with no caption the ingest receipts are still the reply"


async def case_single_pdf_in_a_batch():
    """A batch of one captioned PDF reads like the single-attachment path."""
    with Harness() as h:
        atts = [h.attach("aaaa1111", "a.pdf")]
        await coordinator.media_dispatch_batch(None, SENDER, "add this to recipes",
                                               atts, "1795195204322")
    prompt, _ = h.asked[0]
    assert "a PDF" in prompt and "1 PDFs" not in prompt, prompt[:200]
    return "one captioned PDF in a batch is described in the singular"


async def case_domain_and_model_prefixes():
    # The domain comes from the household's own config, never hardcoded here.
    import chip_config
    domain = chip_config.PREFIX_DOMAINS[0]
    with Harness() as h:
        atts = [h.attach("aaaa1111", "a.pdf"), h.attach("bbbb2222", "b.pdf")]
        await coordinator.media_dispatch_batch(None, SENDER, f"o: {domain}: file these",
                                               atts, "1795195204322")
    prompt, tier = h.asked[0]
    assert prompt.startswith("file these"), f"prefixes left in the caption: {prompt[:60]!r}"
    assert tier == "cloud_opus", f"o: prefix ignored, tier={tier}"
    assert all(d == domain for _, d in h.ingested), \
        f"domain prefix not passed to ingest: {h.ingested}"
    return "model and domain prefixes are honoured, not pasted into the caption"


async def case_mixed_pdf_and_image():
    """A PDF and an image together: the PDF still gets the caption, and the
    image path is left to its own call."""
    with Harness() as h:
        atts = [h.attach("aaaa1111", "a.pdf"),
                {"contentType": "image/jpeg", "id": "cccc3333"}]
        open(os.path.join(h.dir.name, "attachments", "cccc3333"), "wb").write(b"\xff\xd8fake")
        try:
            await coordinator.media_dispatch_batch(None, SENDER, "what is in these?",
                                                   atts, "1795195204322")
        except Exception:
            pass          # the image call reaches out to Gemini; only the PDF half is under test
    assert h.asked, "the PDF's caption was dropped when an image shared the message"
    assert "FULL TEXT OF" in h.asked[0][0]
    return "a PDF keeps its caption when an image shares the message"


async def case_budget_is_shared():
    """Many PDFs must not blow past the single-document character budget."""
    with Harness() as h:
        async def big(session, sender, save_path, raw_filename, domain="unclassified", info=None):
            if info is not None:
                info["filename"] = raw_filename
                info["text"]     = "x" * 100000
            return "ok"
        coordinator.process_pdf_upload = big
        atts = [h.attach(f"dddd{i}{i}{i}{i}", f"{i}.pdf") for i in range(4)]
        await coordinator.media_dispatch_batch(None, SENDER, "summarise these",
                                               atts, "1795195204322")
    prompt, _ = h.asked[0]
    assert len(prompt) < 70000, f"prompt ran to {len(prompt)} chars"
    assert prompt.count("truncated at") == 4, "each oversized document should say it was cut"
    return "four large PDFs share the 60k budget instead of multiplying it"


CASES = [
    case_caption_reaches_both_pdfs,
    case_no_caption_keeps_receipts,
    case_single_pdf_in_a_batch,
    case_domain_and_model_prefixes,
    case_mixed_pdf_and_image,
    case_budget_is_shared,
]


async def main():
    failures = 0
    for case in CASES:
        try:
            note = await asyncio.wait_for(case(), timeout=30)
            print(f"  ok   {case.__name__}: {note}")
        except AssertionError as e:
            failures += 1
            print(f"  FAIL {case.__name__}: {e}")
        except asyncio.TimeoutError:
            failures += 1
            print(f"  FAIL {case.__name__}: the case itself never finished")
    print(f"\n{len(CASES) - failures}/{len(CASES)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
