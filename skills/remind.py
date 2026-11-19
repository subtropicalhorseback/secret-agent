"""
remind.py — Chip Reminder Skill
================================
Claude parses all time/recipient/topic from natural language.
No regex time parser. No brittle token splitting.

Usage (all natural language):
  !remind in 2 hours
  !remind tuesday 3pm water the garden
  !remind me and <name> tomorrow at noon toothpaste
  !remind all friday 9am dentist
  !remind june 5 2pm doctor -- 1 week before, 2 hours before

Multi-reminder syntax: append -- then offsets
  "-- 1 week before, 2 hours before, 30 minutes before"

Reminders persist in data/reminders.json.
Checker runs as a single background task in coordinator.
"""

import os
import json
import asyncio
import anthropic
from datetime import datetime, timedelta
from dotenv import load_dotenv

import os as _os, sys as _sys
_cc = _os.path.dirname(_os.path.abspath(__file__))
while _cc != "/" and not _os.path.exists(_os.path.join(_cc, "chip_config.py")):
    _cc = _os.path.dirname(_cc)
if _cc not in _sys.path:
    _sys.path.insert(0, _cc)
from chip_config import WORKSPACE
load_dotenv(f"{WORKSPACE}/.env")

ANTHROPIC_KEY  = os.getenv("ANTHROPIC_API_KEY")
REMINDERS_FILE = f"{WORKSPACE}/data/reminders.json"

from skills.registry import register
from skills import call_logger

CLAUDE_MAX_TOKENS = int(os.getenv("CLAUDE_MAX_TOKENS", "7500"))

# ─── Persistence ──────────────────────────────────────────────────────────────
# flock on every read/write (audit M7): three workers plus the checker share
# this file, and an unlocked concurrent write could drop a reminder silently.

import fcntl

_LOCK_FILE = REMINDERS_FILE + ".lock"


class _reminders_lock:
    def __enter__(self):
        os.makedirs(os.path.dirname(REMINDERS_FILE), exist_ok=True)
        self._fh = open(_LOCK_FILE, "w")
        fcntl.flock(self._fh, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        try:
            fcntl.flock(self._fh, fcntl.LOCK_UN)
            self._fh.close()
        except Exception:
            pass


def load_reminders():
    if not os.path.exists(REMINDERS_FILE):
        return []
    try:
        with _reminders_lock():
            return json.load(open(REMINDERS_FILE))
    except Exception:
        return []


def save_reminders(reminders):
    with _reminders_lock():
        tmp = REMINDERS_FILE + ".tmp"
        json.dump(reminders, open(tmp, "w"), indent=2)
        os.replace(tmp, REMINDERS_FILE)


def modify_reminders(mutate):
    """Atomic read-modify-write under ONE lock — the only safe way to update
    the list concurrently (a load→edit→save pair can drop an entry appended
    between the two). `mutate` receives the list and edits in place or
    returns a replacement."""
    with _reminders_lock():
        try:
            rs = (json.load(open(REMINDERS_FILE))
                  if os.path.exists(REMINDERS_FILE) else [])
        except Exception:
            rs = []
        out = mutate(rs)
        if out is None:
            out = rs
        tmp = REMINDERS_FILE + ".tmp"
        json.dump(out, open(tmp, "w"), indent=2)
        os.replace(tmp, REMINDERS_FILE)
        return out

def add_reminder(fire_at, recipient_phone, recipient_name, sender_name, message,
                 rtype=None, context=None):
    """rtype='followup' entries re-enter the worker loop with tools when they
    fire (proactive follow-ups, 19 Nov 2026) instead of being sent verbatim;
    `context` carries the note future-Chip acts on."""
    entry = {
        "fire_at":         fire_at.isoformat(),
        "recipient_phone": recipient_phone,
        "recipient_name":  recipient_name,
        "sender_name":     sender_name,
        "message":         message,
        "fired":           False,
        "created_at":      datetime.now().isoformat(),
    }
    if rtype:
        entry["type"] = rtype
    if context:
        entry["context"] = context
    entry["id"] = reminder_id(entry)
    modify_reminders(lambda rs: rs.append(entry))


# ─── Stable ids + reversible cancel (19 Nov 2026) ─────────────────────────────
# The admin schedule tools need to name one specific reminder. Entries written
# before this change have no "id", so derive one deterministically from the
# fields that identify it; the derived value is stamped onto the entry the
# first time it is edited, which keeps it stable afterwards even if the time
# or message changes.

def reminder_id(r):
    import hashlib
    if r.get("id"):
        return r["id"]
    seed = f"{r.get('fire_at','')}|{r.get('recipient_phone','')}|{r.get('message','')}"
    return "rem" + hashlib.sha1(seed.encode()).hexdigest()[:6]


def is_active(r):
    """Pending = not yet fired and not cancelled."""
    return not r.get("fired") and not r.get("cancelled")


def find_reminder(rid):
    rid = (rid or "").strip().lower()
    for r in load_reminders():
        if reminder_id(r).lower() == rid:
            return r
    return None


def _edit_reminder(rid, apply_fn):
    """Locate by id under one lock, stamp the id, apply apply_fn(entry)."""
    rid = (rid or "").strip().lower()
    result = {}

    def _m(rs):
        for r in rs:
            if reminder_id(r).lower() != rid:
                continue
            r["id"] = reminder_id(r)      # freeze before mutating the seed
            apply_fn(r)
            result.update(r)
            return rs
        raise ValueError(f"no reminder with id '{rid}'")

    modify_reminders(_m)
    return result


def update_reminder(rid, fire_at=None, message=None):
    """Change a pending reminder's time and/or message."""
    if fire_at is not None and fire_at <= datetime.now():
        raise ValueError("that time is in the past")

    def _apply(r):
        if r.get("fired"):
            raise ValueError("that reminder has already fired")
        if fire_at is not None:
            r["fire_at"] = fire_at.isoformat()
        if message is not None and message.strip():
            r["message"] = message.strip()
            if r.get("type") == "followup":
                r["context"] = message.strip()

    return _edit_reminder(rid, _apply)


def set_cancelled(rid, cancelled=True):
    """Reversible flag — the checker skips cancelled entries. Not a delete."""
    def _apply(r):
        r["cancelled"] = bool(cancelled)
    return _edit_reminder(rid, _apply)

# ─── Claude parser ────────────────────────────────────────────────────────────

PARSE_PROMPT = """You are parsing a reminder request for an AI assistant called Chip.
Extract all details and return ONLY valid JSON. No other text.

Current datetime: {now}
Known users: {known_users}
Sender: {sender_name} ({sender_phone})

Schema:
{{
  "event_time": "YYYY-MM-DDTHH:MM:SS",
  "recipients": [
    {{"name": "Name", "phone": "+1xxxxxxxxxx"}}
  ],
  "topic": "concise topic description",
  "offsets": ["PT0S"]
}}

Rules:
- event_time: the actual event time in ISO 8601. Infer from context.
  Default time if not specified: 09:00. Default day if not specified: today.
  "afternoon" = 14:00, "morning" = 08:00, "evening" = 18:00, "noon" = 12:00.
  Relative durations: "in N minutes/hours" → add exactly N to the current
  datetime given above. Vague relative durations with no precise number
  ("a few minutes", "shortly", "in a bit", "in a little while") → treat as
  5 minutes from the current datetime. NEVER return the current datetime
  unchanged as event_time for a relative/future request — always add the
  offset, even a small one.
- recipients: who should receive the reminder. "me" = sender. "all" = all users.
  If no recipient specified, default to sender only.
- topic: a natural, complete sentence describing what the reminder is for.
  Do NOT just repeat the time. Write as if composing the reminder message.
  Good: "pick up toothpaste on the way home"
  Bad: "toothpaste"
- offsets: ISO 8601 durations before event_time to fire reminder.
  If user says "-- 1 week before, 2 hours before" → ["P7D", "PT2H"]
  If no offsets specified → ["PT0S"] (fire at event_time)
  Always include PT0S if the user wants a reminder AT the event time.

Example:
Current datetime: 2026-07-29T07:33:00
Request: "remind me in a few minutes what the schedule is"
→ event_time: "2026-07-29T07:38:00" (5 minutes added, NOT 07:33:00)

Request: {request}"""

def parse_reminder_sync(request, sender_name, sender_phone):
    """Call Claude to parse reminder. Returns parsed dict or None."""
    client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
    now    = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    from chip_config import known_users_line
    prompt = PARSE_PROMPT.format(
        now=now,
        known_users=known_users_line(),
        sender_name=sender_name,
        sender_phone=sender_phone,
        request=request,
    )
    resp = client.messages.create(
        model="claude-sonnet-5",
        max_tokens=CLAUDE_MAX_TOKENS,
        thinking={"type": "disabled"},
        messages=[{"role": "user", "content": prompt}]
    )
    call_logger.log_model_call(
        model="claude-sonnet-5", thinking=False,
        tokens_in=resp.usage.input_tokens, tokens_out=resp.usage.output_tokens,
        stop_reason=resp.stop_reason, tools=[],
    )
    raw = resp.content[0].text.strip()
    # Strip any accidental markdown fences
    raw = raw.replace("```json", "").replace("```", "").strip()
    return json.loads(raw)

async def parse_reminder(request, sender_name, sender_phone):
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        None, parse_reminder_sync, request, sender_name, sender_phone)

# ─── Message builder ──────────────────────────────────────────────────────────

BUILD_PROMPT = """Write a reminder message for {recipient_name}.

Event: {event_time_human}
Topic: {topic}
Sender: {sender_name}
Lead time: {lead_description}

Rules:
- 1-2 sentences maximum
- Natural, warm tone
- If sender != recipient, mention who set it: "{sender_name} wanted to remind you:"
- If sender == recipient, just state the reminder directly
- Include the event time naturally in the message
- Do NOT include any prefix like "Reminder:" — just write the message
- Plain text only"""

def build_message_sync(recipient_name, sender_name, topic, event_time, offset_seconds):
    client          = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
    event_time_human = event_time.strftime("%A %B %-d at %-I:%M %p")

    if offset_seconds == 0:
        lead_description = "at event time"
    elif offset_seconds >= 604800:
        weeks = offset_seconds // 604800
        lead_description = f"{weeks} week(s) before"
    elif offset_seconds >= 86400:
        days = offset_seconds // 86400
        lead_description = f"{days} day(s) before"
    elif offset_seconds >= 3600:
        hours = offset_seconds // 3600
        lead_description = f"{hours} hour(s) before"
    else:
        minutes = offset_seconds // 60
        lead_description = f"{minutes} minute(s) before"

    prompt = BUILD_PROMPT.format(
        recipient_name=recipient_name,
        event_time_human=event_time_human,
        topic=topic,
        sender_name=sender_name,
        lead_description=lead_description,
    )
    resp = client.messages.create(
        model="claude-sonnet-5",
        max_tokens=CLAUDE_MAX_TOKENS,
        thinking={"type": "disabled"},
        messages=[{"role": "user", "content": prompt}]
    )
    call_logger.log_model_call(
        model="claude-sonnet-5", thinking=False,
        tokens_in=resp.usage.input_tokens, tokens_out=resp.usage.output_tokens,
        stop_reason=resp.stop_reason, tools=[],
    )
    return resp.content[0].text.strip()

async def build_message(recipient_name, sender_name, topic, event_time, offset_seconds):
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        None, build_message_sync,
        recipient_name, sender_name, topic, event_time, offset_seconds)

# ─── Duration parser ──────────────────────────────────────────────────────────

def iso_duration_to_seconds(duration):
    """Convert ISO 8601 duration string to seconds. PT0S → 0."""
    import re
    if duration == "PT0S" or duration == "P0D":
        return 0
    total = 0
    # Weeks
    m = re.search(r'(\d+)W', duration)
    if m: total += int(m.group(1)) * 604800
    # Days
    m = re.search(r'(\d+)D', duration)
    if m: total += int(m.group(1)) * 86400
    # Hours
    m = re.search(r'(\d+)H', duration)
    if m: total += int(m.group(1)) * 3600
    # Minutes
    m = re.search(r'(\d+)M', duration)
    if m: total += int(m.group(1)) * 60
    # Seconds
    m = re.search(r'(\d+)S', duration)
    if m: total += int(m.group(1))
    return total

# ─── All users lookup ──────────────────────────────────────────────────────────

def get_all_users():
    """Import USERS from coordinator without circular import."""
    try:
        from coordinator import USERS
        return USERS
    except Exception:
        # Fallback if called standalone
        return {
            **__import__("chip_config").USERS,
        }

# ─── Main handler ─────────────────────────────────────────────────────────────

@register("remind", help="Set reminder: !remind [who] <time> [topic] [-- offsets]")
async def handle_remind(session, sender, clean_text, rag_context, history):
    from coordinator import send_signal, USERS

    if not clean_text:
        return (
            "Usage: !remind [who] <time> [topic] [-- offsets]\n\n"
            "Examples:\n"
            "  !remind in 2 hours\n"
            "  !remind tuesday 3pm water the garden\n"
            "  !remind me and <name> tomorrow noon toothpaste\n"
            "  !remind all friday 9am dentist\n"
            "  !remind june 5 2pm doctor -- 1 week before, 2 hours before"
        ), False

    sender_name  = USERS.get(sender, {}).get("name", "the admin")
    sender_phone = sender

    # Parse via Claude
    await send_signal(session, "Parsing reminder...", recipient=sender)
    try:
        parsed = await parse_reminder(clean_text, sender_name, sender_phone)
    except Exception as e:
        return f"Could not parse reminder: {e}", False

    # Validate event_time
    try:
        event_time = datetime.fromisoformat(parsed["event_time"])
    except Exception:
        return "Could not determine event time. Try being more specific.", False

    topic      = parsed.get("topic", "")
    recipients = parsed.get("recipients", [{"name": sender_name, "phone": sender_phone}])
    offsets    = parsed.get("offsets", ["PT0S"])

    if not recipients:
        recipients = [{"name": sender_name, "phone": sender_phone}]

    # Build messages and schedule
    scheduled    = []
    skipped_past = 0

    for offset_str in offsets:
        offset_seconds = iso_duration_to_seconds(offset_str)
        fire_at        = event_time - timedelta(seconds=offset_seconds)

        if fire_at <= datetime.now():
            skipped_past += 1
            continue

        for r in recipients:
            r_name  = r.get("name", sender_name)
            r_phone = r.get("phone", sender_phone)

            message = await build_message(
                r_name, sender_name, topic, event_time, offset_seconds)

            add_reminder(fire_at, r_phone, r_name, sender_name, message)
            scheduled.append((fire_at, r_name, message))

    if not scheduled:
        return "All reminder times are in the past — nothing scheduled.", False

    # Confirmation
    event_fmt = event_time.strftime("%a %b %-d at %-I:%M %p")
    names     = list(dict.fromkeys(r.get("name") for r in recipients))
    names_str = ", ".join(names)

    notify_times = []
    for offset_str in offsets:
        secs = iso_duration_to_seconds(offset_str)
        if secs == 0:
            notify_times.append("at event time")
        elif secs >= 604800:
            notify_times.append(f"{secs//604800}wk before")
        elif secs >= 86400:
            notify_times.append(f"{secs//86400}d before")
        elif secs >= 3600:
            notify_times.append(f"{secs//3600}hr before")
        else:
            notify_times.append(f"{secs//60}min before")

    lines = [
        f"Set for: {names_str}",
        f"Event:   {event_fmt}",
        f"Topic:   {topic}",
        f"Notify:  {', '.join(notify_times)}",
    ]
    if skipped_past:
        lines.append(f"({skipped_past} offset(s) skipped — already past)")

    # Show preview of first message
    if scheduled:
        lines.append(f"\nPreview: {scheduled[0][2]}")

    return "\n".join(lines), False
