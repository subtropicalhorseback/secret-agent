"""
msg_relay.py - Chip Message Relay Skill
========================================
Registered intent: msg

Summarizes context and relays to named user(s).

Usage:
  !msg <name>                        -> summarize recent conversation, send to them
  !msg <name> about the France analysis -> scoped summary of that topic
  !msg <child> the weekend chore list -> age-appropriate rewrite
  !msg all                           -> broadcast to all except sender
"""

import os
import asyncio
import aiohttp
import anthropic
from dotenv import load_dotenv

import os as _os, sys as _sys
_cc = _os.path.dirname(_os.path.abspath(__file__))
while _cc != "/" and not _os.path.exists(_os.path.join(_cc, "chip_config.py")):
    _cc = _os.path.dirname(_cc)
if _cc not in _sys.path:
    _sys.path.insert(0, _cc)
from chip_config import WORKSPACE
load_dotenv(f"{WORKSPACE}/.env")

ANTHROPIC_KEY = os.getenv("ANTHROPIC_API_KEY")

from skills.registry import register

# Name -> phone lookup (inverse of USERS)
def get_phone(name_query):
    from coordinator import USERS
    name_query = name_query.strip().lower()
    for phone, info in USERS.items():
        if info["name"].lower() == name_query:
            return phone, info["name"]
    return None, None

def get_all_phones(exclude_sender):
    from coordinator import USERS
    return [(phone, info["name"])
            for phone, info in USERS.items()
            if phone != exclude_sender]

# ── Summarize for recipient ───────────────────────────────────────────────────

def summarize_sync(sender_name, recipient_name, topic, history, active_report):
    client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)

    # Build context from history and active report
    history_text = ""
    if history:
        lines = [
            f"{'User' if t['role']=='user' else 'Chip'}: {t['content']}"
            for t in history[-10:]
        ]
        history_text = "\n".join(lines)

    report_context = ""
    if active_report and active_report.get("report_dir"):
        m3_path = f"{active_report['report_dir']}/mode3.txt"
        if os.path.exists(m3_path):
            report_context = open(m3_path).read()[:2000]

    topic_instruction = f"Focus specifically on: {topic}" if topic else \
                        "Summarize the most important points from the conversation."

    # Age-appropriate flag for child users (config/users.json "child": true)
    from chip_config import is_child
    child_flag = ""
    if is_child(recipient_name):
        child_flag = (
            f"\nIMPORTANT: {recipient_name} is a child. Use simple, clear language. "
            "Skip any adult/professional context. Keep it friendly and brief."
        )

    prompt = (
        f"{sender_name} asked Chip to relay information to {recipient_name}.\n\n"
        f"RECENT CONVERSATION:\n{history_text}\n\n"
        f"{'ANALYSIS CONTEXT:\\n' + report_context if report_context else ''}\n\n"
        f"{topic_instruction}\n\n"
        f"Write a clear, concise summary (2-4 sentences) that {recipient_name} "
        f"can act on or understand without needing the full context. "
        f"Write in first person as if {sender_name} is speaking."
        f"{child_flag}"
    )

    resp = client.messages.create(
        model="claude-sonnet-5",
        max_tokens=500,
        messages=[{"role": "user", "content": prompt}]
    )
    return resp.content[0].text.strip()

async def summarize(sender_name, recipient_name, topic, history, active_report):
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        None, summarize_sync, sender_name, recipient_name, topic, history, active_report)

# ── Main handler ──────────────────────────────────────────────────────────────

@register("msg", help="Relay summary to user: !msg <name|all> [topic]")
async def handle_msg(session, sender, clean_text, rag_context, history):
    from coordinator import send_signal, USERS
    from skills.analyze import get_active_report

    if not clean_text:
        return "Usage: !msg <name or all> [topic or instruction]", False

    sender_name = USERS.get(sender, {}).get("name", "Someone")
    active      = get_active_report(sender)

    # Parse recipient and optional topic
    parts     = clean_text.split(None, 1)
    recipient = parts[0].strip().lower()
    topic     = parts[1].strip() if len(parts) > 1 else ""

    # Determine targets
    if recipient == "all":
        targets = get_all_phones(sender)
    else:
        phone, name = get_phone(recipient)
        if not phone:
            return f"Unknown recipient '{recipient}'. Known users: the admin, the partner, the child user.", False
        targets = [(phone, name)]

    if not targets:
        return "No recipients found.", False

    # Generate summary for first recipient (reuse for broadcast unless a child recipient needs rewrite)
    results = []
    for phone, name in targets:
        summary = await summarize(sender_name, name, topic, history, active)
        intro   = f"{sender_name} asked me to share this with you:\n\n"
        message = intro + summary
        await send_signal(session, message, recipient=phone)
        results.append(name)

    return f"Sent to {', '.join(results)}.", False
