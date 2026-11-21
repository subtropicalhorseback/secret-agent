"""
skills/worklog.py — the daily work log, rolled off like the chat buffer
========================================================================
Added 20 Nov 2026 (One Voice spec, B.5 and A.1).

One JSON line per event, one file per day under data/worklog/. It holds what
passes between Chip and its workers and every decision the send gate makes,
so there is a local record of everything without any of it reaching a phone
or the model-facing conversation history.

Retention mirrors the chat buffer: the dream cycle calls roll() nightly,
which gzips every day before today and deletes anything older than
KEEP_DAYS. data/ is gitignored and never exported.

Never raises. A full disk degrades the record; it must never break a turn.
"""
import gzip
import json
import os
import shutil
import sys
import threading
from datetime import datetime, timedelta

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
from chip_config import WORKSPACE  # noqa: E402

WORKLOG_DIR = f"{WORKSPACE}/data/worklog"
KEEP_DAYS = 30
_lock = threading.Lock()


def _dir():
    # CHIP_WORKLOG_DIR lets tests keep their traffic out of the real record.
    return os.getenv("CHIP_WORKLOG_DIR") or WORKLOG_DIR


def _path(day=None):
    return f"{_dir()}/{(day or datetime.now()).strftime('%Y-%m-%d')}.jsonl"


def log(event, **fields):
    """Append one event. Values that aren't JSON-serialisable are stringified."""
    try:
        rec = {"ts": datetime.now().isoformat(timespec="seconds"), "event": event}
        rec.update(fields)
        line = json.dumps(rec, default=str, ensure_ascii=False)
        os.makedirs(_dir(), exist_ok=True)
        with _lock:
            with open(_path(), "a") as f:
                f.write(line + "\n")
    except Exception as e:
        print(f"⚠️ worklog: {e}")


def read(day=None, event=None, limit=None):
    """Events for one day (today by default), oldest first. Reads .gz too."""
    p = _path(day)
    rows = []
    try:
        if os.path.exists(p):
            fh = open(p)
        elif os.path.exists(p + ".gz"):
            fh = gzip.open(p + ".gz", "rt")
        else:
            return []
        with fh:
            for line in fh:
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                if event and r.get("event") != event:
                    continue
                rows.append(r)
    except Exception as e:
        print(f"⚠️ worklog read: {e}")
    return rows[-limit:] if limit else rows


def roll(keep_days=KEEP_DAYS, directory=None):
    """Nightly: gzip every plain file before today, delete files older than
    keep_days. Returns (compressed, deleted). Also used for other rolling
    record directories (web log) by passing `directory`."""
    d = directory or WORKLOG_DIR
    compressed = deleted = 0
    if not os.path.isdir(d):
        return 0, 0
    today = datetime.now().strftime("%Y-%m-%d")
    cutoff = (datetime.now() - timedelta(days=keep_days)).strftime("%Y-%m-%d")
    for name in sorted(os.listdir(d)):
        p = os.path.join(d, name)
        day = name.split(".")[0]
        if len(day) != 10 or day.count("-") != 2:
            continue
        try:
            if day < cutoff:
                os.remove(p)
                deleted += 1
            elif name.endswith(".jsonl") and day < today:
                with open(p, "rb") as src, gzip.open(p + ".gz", "wb") as dst:
                    shutil.copyfileobj(src, dst)
                os.remove(p)
                compressed += 1
        except Exception as e:
            print(f"⚠️ worklog roll {name}: {e}")
    return compressed, deleted
