#!/usr/bin/env python3
"""Fake-daemon tests for send_signal_file's reply confirmation.

Run:  venv/bin/python3 tests/test_signal_file_send.py

signal-cli 0.14.5 runs here as a daemon in the default receive mode, which
broadcasts inbound-message notifications to EVERY connected client — including
the short-lived socket each send opens. So the first line that comes back on a
send's own connection is frequently an unrelated inbound message, not the
answer to the send.

That is what made the old code dangerous in two different ways:
  - it never read the reply at all, so a rejection was invisible, and
  - had it read only the first line, a notification (which carries no "error"
    key) would have stood in for the answer and a REJECTED send would have been
    reported delivered.

The cases below reproduce that interleave against a fake daemon. The one that
matters most is `rejected_after_notification`: signal-cli refuses the send with
"Untrusted identity key" and the assertion is that we report failure, never
delivered.
"""

import asyncio
import json
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


# An inbound message arriving on our send's socket. No "id", no "error" —
# indistinguishable from a successful reply if you only look for an error.
NOTIFICATION = {
    "jsonrpc": "2.0",
    "method": "receive",
    "params": {"envelope": {"source": "+15555550123",
                            "dataMessage": {"message": "unrelated inbound"}}},
}


class FakeDaemon:
    """A unix socket that answers one request per connection with a scripted
    sequence of lines, then (optionally) hangs or closes."""

    def __init__(self, script, hang=False, close_without_reply=False):
        self.script   = script       # callable: request dict -> list of dicts
        self.hang     = hang
        self.close    = close_without_reply
        self.requests = []
        self.handlers = []           # cancelled on exit, so a hung case cannot wedge teardown

    async def __aenter__(self):
        self.dir  = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "signal.sock")
        self.server = await asyncio.start_unix_server(self._handle, path=self.path)
        self._saved_socket     = coordinator.SIGNAL_SOCKET
        coordinator.SIGNAL_SOCKET = self.path
        return self

    async def __aexit__(self, *exc):
        coordinator.SIGNAL_SOCKET = self._saved_socket
        for task in self.handlers:
            task.cancel()
        self.server.close()
        self.dir.cleanup()

    async def _handle(self, reader, writer):
        self.handlers.append(asyncio.current_task())
        raw = await reader.readline()
        if not raw:
            return
        req = json.loads(raw.decode())
        self.requests.append(req)
        if self.close:
            writer.close()
            return
        for line in self.script(req):
            writer.write((json.dumps(line) + "\n").encode())
            await writer.drain()
        if self.hang:
            await asyncio.sleep(3600)        # never answers; caller must time out
        await writer.drain()


def _ok(req):
    return {"jsonrpc": "2.0", "id": req["id"],
            "result": {"timestamp": 1795195204322}}


def _rejected(req):
    return {"jsonrpc": "2.0", "id": req["id"],
            "error": {"code": -32602,
                      "message": "Untrusted identity key for +15555550123"}}


# ── the cases ────────────────────────────────────────────────────────────────

async def case_rejected_after_notification():
    """THE regression. signal-cli rejects the attachment, but an unrelated
    inbound notification arrives first on the same socket. Must be reported as
    a failure — never as delivered."""
    async with FakeDaemon(lambda req: [NOTIFICATION, _rejected(req)]) as d:
        result = await coordinator.send_signal_file(None, "/tmp/report.pdf",
                                                    "Report attached", "+15555550199")
    assert result is not True, "a rejected send was reported as delivered"
    assert result is False, f"expected a reported failure, got {result!r}"
    assert d.requests[0]["params"]["attachments"] == ["/tmp/report.pdf"]
    return "rejection behind a notification is surfaced as failure"


async def case_rejected_plainly():
    async with FakeDaemon(lambda req: [_rejected(req)]):
        result = await coordinator.send_signal_file(None, "/tmp/report.pdf",
                                                    "Report attached", "+15555550199")
    assert result is False, f"expected False, got {result!r}"
    return "a bare rejection is surfaced as failure"


async def case_success_behind_noise():
    """Notifications and another client's answer precede ours; still delivered."""
    def script(req):
        other = {"jsonrpc": "2.0", "id": req["id"] + 1000, "result": {"timestamp": 1}}
        return [NOTIFICATION, other, NOTIFICATION, _ok(req)]

    async with FakeDaemon(script):
        result = await coordinator.send_signal_file(None, "/tmp/report.pdf",
                                                    "Report attached", "+15555550199")
    assert result is True, f"expected True, got {result!r}"
    return "the real answer is found behind notifications and a foreign id"


async def case_timeout_is_unknown():
    """No answer in time is unknown, not success and not a hard failure."""
    async with FakeDaemon(lambda req: [NOTIFICATION], hang=True):
        orig = coordinator._read_rpc_reply

        async def quick(reader, want_id, timeout):      # keep the test fast
            return await orig(reader, want_id, 0.4)

        coordinator._read_rpc_reply = quick
        try:
            result = await coordinator.send_signal_file(None, "/tmp/report.pdf",
                                                        "Report attached", "+15555550199")
        finally:
            coordinator._read_rpc_reply = orig
    assert result is None, f"timeout must read as unknown (None), got {result!r}"
    return "a timeout reads as unknown, not success"


async def case_closed_connection():
    async with FakeDaemon(lambda req: [], close_without_reply=True):
        result = await coordinator.send_signal_file(None, "/tmp/report.pdf",
                                                    "Report attached", "+15555550199")
    assert result is False, f"expected False, got {result!r}"
    return "a connection closed without a reply is a failure"


async def case_ids_are_distinct():
    """Correlation only works if two sends never share an id."""
    async with FakeDaemon(lambda req: [_ok(req)]) as d:
        for _ in range(3):
            await coordinator.send_signal_file(None, "/tmp/report.pdf", "x", "+15555550199")
    ids = [r["id"] for r in d.requests]
    assert len(set(ids)) == len(ids), f"ids repeated across sends: {ids}"
    return "each send carries a distinct request id"


CASES = [
    case_rejected_after_notification,
    case_rejected_plainly,
    case_success_behind_noise,
    case_timeout_is_unknown,
    case_closed_connection,
    case_ids_are_distinct,
]


async def main():
    # Keep the live call log clean, and make the reported status assertable.
    statuses = []
    coordinator.call_logger.log_signal_out = (
        lambda target, status="delivered": statuses.append(status))

    failures = 0
    for case in CASES:
        try:
            note = await asyncio.wait_for(case(), timeout=20)
            print(f"  ok   {case.__name__}: {note}")
        except AssertionError as e:
            failures += 1
            print(f"  FAIL {case.__name__}: {e}")
        except asyncio.TimeoutError:
            failures += 1
            print(f"  FAIL {case.__name__}: the case itself never finished")

    delivered_on_rejection = [s for s in statuses if s == "delivered"]
    print(f"\nlogged statuses: {statuses}")
    print(f"{len(CASES) - failures}/{len(CASES)} passed")
    # Four of the six cases are non-deliveries; only the two success paths
    # (noise-then-success, and three sends in ids_are_distinct) may log it.
    assert len(delivered_on_rejection) == 4, delivered_on_rejection
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
