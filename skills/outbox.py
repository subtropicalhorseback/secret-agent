"""
skills/outbox.py — background scripts tell Chip, and Chip decides what reaches a phone
======================================================================================
Added 20 Nov 2026 (One Voice spec, A.3).

vpn_guard, the dispatcher, the healer and the pip report run outside the
coordinator process (cron, systemd units). Each used to open the signal-cli
socket and message the admin directly: lease releases, all-clears, queue
notices, a 3,000-character healer transcript. Now they call notify(), which
writes one small JSON file to data/outbox/. Chip's 60-second beat drains it:

  urgent     one message per beat, all urgent events together, prefixed
             [Chip] — VPN down and staying down, a job failing repeatedly, a
             heal's result, a request lost to a restart.
  routine    appended to data/outbox/digest.jsonl and reported in the nightly
             summary the admin already gets (dream cycle). No message at the
             time: a normal lease release or a monthly package list is not
             worth a buzz.

The beat writes data/outbox/.heartbeat. If that is more than STALE_AFTER old
when notify() is called, Chip is not consuming, so an URGENT event is sent
directly instead (the emergency path) and recorded as sent. Routine events
always wait. signal_watchdog keeps its own direct path: its whole job is the
case where Chip cannot speak.

Standalone on purpose: no coordinator import, so it works mid-restart.
"""
import glob
import json
import os
import socket
import sys
import time
import uuid
from datetime import datetime

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
from chip_config import WORKSPACE  # noqa: E402

STALE_AFTER = 180   # seconds; the beat runs every 60
SIGNAL_SOCKET = os.getenv("SIGNAL_SOCKET", "/run/signal-cli/signal.sock")


def _dir():
    return os.getenv("CHIP_OUTBOX_DIR") or f"{WORKSPACE}/data/outbox"


def _heartbeat_path():
    return os.path.join(_dir(), ".heartbeat")


def beat():
    """Called by Chip's 60-second loop: 'I am consuming the outbox'."""
    os.makedirs(_dir(), exist_ok=True)
    with open(_heartbeat_path(), "w") as f:
        f.write(datetime.now().isoformat(timespec="seconds"))


def heartbeat_age():
    try:
        return time.time() - os.path.getmtime(_heartbeat_path())
    except OSError:
        return None


def chip_consuming():
    age = heartbeat_age()
    return age is not None and age <= STALE_AFTER


def direct_send(text, recipient=None):
    """The emergency path: straight to signal-cli. Returns True on a matched,
    error-free reply."""
    if recipient is None:
        from chip_config import ADMIN_PHONE
        recipient = ADMIN_PHONE
    req_id = 7000 + int(time.time()) % 1000
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(20)
        s.connect(SIGNAL_SOCKET)
        s.sendall((json.dumps({"jsonrpc": "2.0", "method": "send", "id": req_id,
                               "params": {"recipient": [recipient], "message": text}})
                   + "\n").encode())
        buf, deadline = b"", time.time() + 20
        while time.time() < deadline:
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
            for line in buf.split(b"\n"):
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                if r.get("id") == req_id:
                    s.close()
                    return not r.get("error")
        s.close()
    except Exception as e:
        print(f"outbox direct send failed: {e}")
    return False


def notify(kind, text, urgent=False, source=None, **fields):
    """Hand an event to Chip. Returns 'queued', 'sent-direct' or 'failed'."""
    ev = {"id": uuid.uuid4().hex[:10], "ts": datetime.now().isoformat(timespec="seconds"),
          "kind": kind, "text": str(text).strip(), "urgent": bool(urgent),
          "source": source or os.path.basename(sys.argv[0] or "?")}
    ev.update(fields)
    os.makedirs(_dir(), exist_ok=True)
    if urgent and not chip_consuming():
        ok = direct_send(f"[Chip] {ev['text']}")
        ev["sent_direct"] = bool(ok)
        _append_digest(ev)        # the nightly summary still records it
        return "sent-direct" if ok else "failed"
    tmp = os.path.join(_dir(), f".{ev['id']}.tmp")
    final = os.path.join(_dir(), f"{ev['ts'].replace(':', '')}-{ev['id']}.json")
    try:
        with open(tmp, "w") as f:
            json.dump(ev, f)
        os.replace(tmp, final)                # atomic: the drain never sees half a file
        return "queued"
    except Exception as e:
        print(f"outbox write failed: {e}")
        if urgent:
            return "sent-direct" if direct_send(f"[Chip] {ev['text']}") else "failed"
        return "failed"


def _append_digest(ev):
    try:
        with open(os.path.join(_dir(), "digest.jsonl"), "a") as f:
            f.write(json.dumps(ev) + "\n")
    except Exception as e:
        print(f"outbox digest write failed: {e}")


def drain():
    """Take every queued event (oldest first). Routine ones go to the digest
    here; urgent ones are returned for Chip to send as one message."""
    urgent = []
    for p in sorted(glob.glob(os.path.join(_dir(), "*.json"))):
        try:
            ev = json.load(open(p))
        except Exception:
            ev = None
        try:
            os.remove(p)
        except OSError:
            pass
        if not ev:
            continue
        if ev.get("urgent"):
            urgent.append(ev)
        _append_digest(ev)
    return urgent


def compose(urgent_events):
    """One message for everything urgent in this beat."""
    if not urgent_events:
        return ""
    if len(urgent_events) == 1:
        return urgent_events[0]["text"]
    return "\n\n".join(f"• {e['text']}" for e in urgent_events)


def take_digest():
    """For the nightly summary: today's events as short lines, then clear."""
    p = os.path.join(_dir(), "digest.jsonl")
    if not os.path.exists(p):
        return []
    lines = []
    try:
        for raw in open(p):
            try:
                ev = json.loads(raw)
            except Exception:
                continue
            first = ev.get("text", "").splitlines()[0][:140] if ev.get("text") else ""
            flag = " (sent directly — Chip was down)" if ev.get("sent_direct") else ""
            lines.append(f"{ev.get('ts', '')[11:16]} {first}{flag}")
        os.replace(p, p + ".prev")
    except Exception as e:
        print(f"outbox digest read failed: {e}")
    return lines
