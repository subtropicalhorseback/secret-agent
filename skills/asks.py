"""
skills/asks.py — a worker waiting on a human answer, routed through Chip
=========================================================================
Added 20 Nov 2026 (One Voice spec, C.8).

When a browser worker reaches something only a person can do (a CAPTCHA, a
login decision, a payment confirmation), it pauses and opens an Ask: the
admin gets the screenshot and one line from Chip. The admin's next message
goes to Chip first; Chip decides whether it is the answer (coordinator
_ask_answer). If it is, the waiting worker gets it as its tool result and
carries on in the same tab. If it is something else, Chip handles it normally
and the worker keeps waiting until its deadline.

In-process only: an Ask lives as long as the worker that opened it.
"""
import asyncio
import time

_pending = {}      # owner -> [Ask, ...] oldest first


class Ask:
    def __init__(self, owner, question, kind, run_id=None):
        self.owner = owner
        self.question = question
        self.kind = kind            # captcha | login | payment | other
        self.run_id = run_id
        self.created = time.time()
        self.future = asyncio.get_running_loop().create_future()


def open_ask(owner, question, kind="other", run_id=None):
    a = Ask(owner, question, kind, run_id)
    _pending.setdefault(owner, []).append(a)
    return a


def pending(owner):
    lst = [a for a in _pending.get(owner, []) if not a.future.done()]
    _pending[owner] = lst
    return lst[0] if lst else None


def answer(owner, text):
    a = pending(owner)
    if a is None:
        return False
    a.future.set_result(text)
    return True


def close(ask):
    lst = _pending.get(ask.owner, [])
    if ask in lst:
        lst.remove(ask)
    if not ask.future.done():
        ask.future.cancel()


async def wait(ask, timeout):
    try:
        return await asyncio.wait_for(asyncio.shield(ask.future), timeout=timeout)
    except asyncio.TimeoutError:
        return None
    finally:
        close(ask)
