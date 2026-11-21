"""
skills/send_gate.py — one in, one out
=======================================
Added 20 Nov 2026 (One Voice spec, Part A.1).

On 20 Nov the admin's phone received 131 messages for 65 he sent: step
pings, work-order dumps, "healer launched", second and third messages about
the same request. Every outbound message from the coordinator process now
passes through decide() with a KIND, and code — not the model — decides
whether it leaves.

Kinds
    reply      Chip's answer to an inbound message. One per inbound message;
               a long reply split into chunks counts as one.
    file       an attachment that is part of an answer or was asked for
    ask        a question only the human can answer (approval, CAPTCHA picture)
    scheduled  a delivery the user set up (reminders, weather, weekly usage,
               scheduled jobs and follow-ups)
    relay      a message a user asked Chip to send to someone else
    alert      a system event the user must know about, worded by Chip
    emergency  direct send from a background script while Chip is down
               (out-of-process senders; listed for completeness)

How a send is classified when the caller names no kind: within a turn — the
open turn, or a closed one for background tasks it started — a message to the
turn's own sender is that turn's reply (or `scheduled` when the turn is an
internal scheduled job), and a message to anyone else is a `relay`. Outside
any turn it is `unclassified`, which is a defect.

Turns ride a contextvar set by the worker loop. asyncio.create_task copies
the context, so a background task started during a turn carries a reference
to the SAME Turn object. When the worker closes the turn, the background task
is no longer "inside" it (current() returns None), but its sends are still
attributed to it — which is how a late second reply is caught and counted.

Modes (SEND_GATE_MODE in config/routing_constants, default "observe")
    observe   every message is sent; would-be refusals are logged as
              violations. This is how the gate is proven before it bites:
              a gate that silently drops a legitimate command reply produces
              exactly the silence the admin complained about.
    enforce   violations are not sent; they are logged.

Every decision is written to the work log (skills/worklog.py).
"""
import contextvars
import itertools
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from skills import worklog  # noqa: E402

KINDS = ("reply", "file", "ask", "scheduled", "relay", "alert", "emergency")
DEFAULT_MODE = "observe"

_ids = itertools.count(1)
_turn = contextvars.ContextVar("chip_turn", default=None)


def mode():
    """observe | enforce, from config/routing_constants (re-read each call so
    flipping the setting needs no code change; a restart picks it up)."""
    try:
        from skills.browse_capture import _RC
        m = str(getattr(_RC, "SEND_GATE_MODE", DEFAULT_MODE)).strip().lower()
        return m if m in ("observe", "enforce") else DEFAULT_MODE
    except Exception:
        return DEFAULT_MODE


class Turn:
    __slots__ = ("id", "sender", "ts", "internal", "replies", "closed",
                 "notes", "reacted", "deferred", "text", "request_id", "workers",
                 "attachments")

    def __init__(self, sender, ts=None, internal=False, text=""):
        self.id = next(_ids)
        self.sender = sender
        self.ts = ts
        self.text = text or ""
        self.request_id = None    # set when the turn launches its first worker
        self.workers = []         # work ids launched during this turn
        self.attachments = []     # files a result turn chose to send (attach_file)
        self.internal = bool(internal)
        self.replies = 0
        self.closed = False
        self.notes = []        # short lines folded into this turn's reply
        self.reacted = False   # ⏳ set on the inbound message
        self.deferred = False  # the answer will come from a result turn


def begin(sender, ts=None, internal=False, text=""):
    """Open a turn for one inbound message. Returns a token for end()."""
    t = Turn(sender, ts, internal, text)
    worklog.log("turn_begin", turn=t.id, who=_tail(sender), internal=t.internal)
    return _turn.set(t)


def end(token):
    t = _turn.get()
    if t is not None:
        t.closed = True
        if t.notes and not t.deferred:
            # Notes nobody delivered (the reply went out inline, before they
            # were added). Logged rather than sent as a second message. A
            # deferred turn keeps them: they ride on the result-turn reply.
            worklog.log("notes_dropped", turn=t.id, notes=t.notes)
        worklog.log("turn_end", turn=t.id, replies=t.replies, deferred=t.deferred)
    try:
        _turn.reset(token)
    except Exception:
        _turn.set(None)


def raw():
    """The turn this context belongs to, open or closed (or None)."""
    return _turn.get()


def current():
    """The open turn, or None once the worker has closed it."""
    t = _turn.get()
    return t if (t is not None and not t.closed) else None


def adopt(turn):
    """Run the rest of this context as part of `turn` (used by result turns,
    which answer an inbound message that has already been closed)."""
    return _turn.set(turn)


def detach():
    _turn.set(None)


def add_note(text):
    """A short line to fold into the current turn's reply instead of sending
    it as its own message (a staging id, 'effort back to high')."""
    t = current() or raw()
    if t is not None and text:
        t.notes.append(str(text).strip())
        return True
    return False


def take_notes():
    t = raw()
    if t is None or not t.notes:
        return []
    notes, t.notes = t.notes, []
    return notes


def _tail(who):
    return str(who or "?")[-4:]


def classify(recipient, kind=None):
    if kind:
        return kind
    # The turn this context belongs to, open OR closed: a background task
    # started during a turn still answers that turn's message, so its send is
    # that turn's (possibly second) reply — not an orphan.
    t = raw()
    if t is not None:
        if recipient == t.sender:
            return "scheduled" if t.internal else "reply"
        return "relay"
    return "unclassified"


def decide(recipient, text="", kind=None, parts=1):
    """Decide one outbound message (or one multi-part reply). Returns
    (send: bool, kind, reason). Always logs."""
    k = classify(recipient, kind)
    t = raw()
    reason = ""
    if k not in KINDS:
        reason = "unclassified send (no kind, no open turn)"
    elif k == "reply":
        if t is None:
            reason = "reply with no inbound message"
        elif recipient == t.sender:
            t.replies += 1
            if t.replies > 1:
                reason = f"reply #{t.replies} to one inbound message"
    violation = bool(reason)
    m = mode()
    send = not (violation and m == "enforce")
    worklog.log("send", kind=k, to=_tail(recipient), chars=len(text or ""),
                parts=parts, turn=(t.id if t else None),
                verdict=("sent" if send else "refused"),
                violation=reason or None, mode=m,
                preview=(text or "")[:100])
    if violation:
        print(f"🚦 send gate ({m}): {reason} — kind={k} to {_tail(recipient)}: "
              f"{(text or '')[:60]!r}")
    return send, k, reason
