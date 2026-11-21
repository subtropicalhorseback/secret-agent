"""
coordinator.py — Chip
=============================
Command syntax: cmd [flags]: payload
  g:          Gemini Flash (default)
  g -v:       verbose
  g -c:       concise
  gp:         Gemini Pro
  s:          Claude Sonnet
  m:          local Phi (phi4-mini) (slow)
  vault:      FTS5 vault search
  analyze:    structured assessment
  chk:        second-reader crosscheck
  research:   deep iterative research
  remind:     set reminder
  msg:        relay to user
  usage:      API cost report
  wx:         weather
  ls:         vault directory
  ps:         system health
  cat:        read file
  facts:      memory operations
  voice:      set persona
  save/skip:  memory commits
  forget:     delete facts

Pipeline operators:
  g: payload | chk:          pipe output as context
  s: payload >> msg: Name     deliver to Name (cc sender)
  s: payload >> facts:        save to memory

Plain text (no colon) goes through semantic router.
Legacy !commands still accepted during transition.
"""

import os
import re
import json
import asyncio
import sys as _sys_alias
# Run as a script (systemd: `python3 coordinator.py`), this module is
# __main__, and every skill's `from coordinator import send_signal` used to
# load a SECOND copy of it — its own per-recipient send locks, its own state.
# Registering the running module under its own name first makes those imports
# return this one (20 Nov 2026).
if __name__ == "__main__":
    _sys_alias.modules.setdefault("coordinator", _sys_alias.modules["__main__"])
import aiohttp
import sqlite3
import numpy as np
import psutil
import anthropic
import base64
import requests
from datetime import datetime, timedelta
from collections import deque
from dotenv import load_dotenv
from skills.registry import SKILL_REGISTRY, cancel_flags
import skills.analyze
import skills.audio_id
import skills.remind
import skills.msg_relay
import skills.usage_tracker
import skills.research
import skills.media_review
try:                      # optional, household-specific skill (not shipped)
    import skills.world_cup
except ImportError:
    pass
import skills.taxonomy as taxonomy
import skills.tool_calling as tool_calling
import skills.call_logger as call_logger

import os as _os, sys as _sys
_cc = _os.path.dirname(_os.path.abspath(__file__))
while _cc != "/" and not _os.path.exists(_os.path.join(_cc, "chip_config.py")):
    _cc = _os.path.dirname(_cc)
if _cc not in _sys.path:
    _sys.path.insert(0, _cc)
from chip_config import WORKSPACE
load_dotenv(f"{WORKSPACE}/.env")

SENDER        = os.getenv("SENDER_NUMBER")
SIGNAL_SOCKET     = os.getenv("SIGNAL_SOCKET", "/run/signal-cli/signal.sock")
SIGNAL_CONFIG_DIR = os.getenv("SIGNAL_CONFIG_DIR", f"{WORKSPACE}/docker/signal")
OLLAMA_URL    = os.getenv("OLLAMA_API_URL", "http://localhost:11434")
EMBED_URL     = os.getenv("EMBED_SERVICE_URL", "http://127.0.0.1:11435")
GEMINI_KEY    = os.getenv("GEMINI_API_KEY")
ANTHROPIC_KEY = os.getenv("ANTHROPIC_API_KEY")
CLAUDE_MAX_TOKENS = int(os.getenv("CLAUDE_MAX_TOKENS", "16000"))
# POLL_SEC removed — daemon mode uses push, not polling

CONTEXT_DB    = f"{WORKSPACE}/data/memory/chip_context.db"
VAULT_DB      = f"{WORKSPACE}/data/memory/chip_vault.db"
REPORT_DIR    = f"{WORKSPACE}/data/archive/reports"
BUFFER_DIR    = f"{WORKSPACE}/data/memory"
SYNCTHING_DIR = f"{WORKSPACE}/syncthing"

def get_search_dirs():
    """All dirs where files might live — recursive walk of syncthing + archive."""
    dirs = [SYNCTHING_DIR, f"{WORKSPACE}/data/archive/", f"{WORKSPACE}/data/archive/reports/"]
    try:
        for root, subdirs, _ in os.walk(SYNCTHING_DIR):
            subdirs[:] = [d for d in subdirs if not d.startswith(".")]
            dirs.append(root)
    except Exception:
        pass
    return dirs

# ─── Models ───────────────────────────────────────────────────────────────────

# Premier-model policy (the admin, 19 Nov 2026): Claude surfaces run Opus 5 at
# effort high with Fable 5 attached as advisor; Fable 5 solo (xhigh) is the
# adjudicator in every pipeline and available directly via f:.
MODELS = {
    "router":        "phi4-mini",             # m: and offline fallback only
    "embed":          "nomic-embed-text",
    "cloud_default": "gemini-3.5-flash",
    "cloud_heavy":   "claude-opus-5",          # the admin default
    "cloud_sonnet":  "claude-sonnet-5",        # s: — cheaper Claude tier
    "cloud_fable":   "claude-fable-5",         # f: — admin only
    "cloud_top":     "gemini-3.5-flash",      # placeholder until Gemini 3.5 Pro GA
    "cloud_opus":    "claude-opus-5",          # o: kept as alias for muscle memory
    "cloud_basic":    "gemini-3.5-flash",
}

TAGS = {
    "cloud_default": "[G·3.5F]",
    "cloud_heavy":   "[Opus]",
    "cloud_sonnet":  "[Sonnet]",
    "cloud_fable":   "[Fable]",
    "cloud_top":     "[G·3.5F]",
    "cloud_opus":    "[Opus]",
    "cloud_basic":    "[G·Flash]",
    "vault":         "[Vault]",
    "local":         "[Local]",
    "chip":          "[Chip]",
    "fallback":      "[G·3.5F·fb]",
}

RAG_THRESHOLD = 0.55


def fail_line(what, e):
    """One plain line for a failure the user should hear about. The exception
    itself goes to the log, never to the phone (One Voice spec A.1)."""
    print(f"⚠️ {what} failed: {type(e).__name__}: {e}")
    return f"{what} failed ({type(e).__name__}). It's in the log."

# Sent when a model returns no text at all (refusal, exhausted budget) so the
# user never just gets "..." with no explanation.
EMPTY_REPLY = ("I didn't get a usable answer back from the model on that one "
               "(it returned nothing). Send it again, or rephrase and I'll retry.")

# ─── Users & Permissions ──────────────────────────────────────────────────────

# All identity data lives in config/users.json via chip_config (19 Nov 2026) —
# the codebase itself carries no phone numbers, names, or personas.
import chip_config
USERS = chip_config.USERS

RESTRICTED = {"archive"}

# Audit H2: RESTRICTED only ever covered 5 commands and was only consulted on
# the "skill"/"builtin" branches, so the user tier (the child user) could run sticky
# Opus, the heavy pipelines, and every destructive memory command. Checked once
# at dispatch so it covers model/shell/vault/skill/builtin alike.
ADMIN_ONLY_CMDS = {
    "o", "f",                             # sticky Opus / Fable (cost)
    "analyze", "research", "chk",         # heavy multi-model pipelines (cost)
    "memory", "forget", "skip",           # destructive, cross-user memory edits
    "facts", "cat",                       # read others' facts / whitelisted files
    "rechunk",                            # heavy, stops ingest
    "archive",
    "fix",                                # Claude Code self-heal (19 Nov 2026)
    "build", "skill",                     # build track + add-on skill switches (20 Nov 2026)
    "browse", "mullvad",                  # live browser + VPN control (spec: the child user refused)
}

# Commands whose "all" / broadcast form is admin-only even though the plain
# form is fine for the user tier.
BROADCAST_CMDS = {"msg", "remind"}


def _denied_reason(cmd, payload, sender):
    """Return a refusal string if `sender` may not run `cmd`, else None."""
    if USERS.get(sender, {}).get("tier") == "admin":
        return None
    if cmd in ADMIN_ONLY_CMDS:
        return "⛔ Admin only."
    if cmd in BROADCAST_CMDS:
        first = (payload or "").strip().split()
        if first and first[0].lower().strip(":,") in ("all", "everyone"):
            return "⛔ Broadcast is admin only."
    return None

active_voices = {}
active_model  = {}  # sender -> {"tier": str, "remaining": int} — sticky model after s:/g:/gp:

# ─── Per-user thinking effort (the admin, 19 Nov 2026) ─────────────────────────────
# Plain-language control over how hard the Claude models think. Default is
# "high" house-wide; "think hard: <question>" bumps to max for this turn plus
# EFFORT_STICKY_TURNS more (mirrors the sticky-model mechanic), "quick:" drops
# to low. Applies to Claude calls; for an admin Gemini user (the partner) a max-effort
# trigger also escalates the turn onto Opus so the effort actually lands.
active_effort       = {}  # sender -> {"level": str, "remaining": int}
EFFORT_STICKY_TURNS = 5
EFFORT_LEVELS       = ("low", "medium", "high", "xhigh", "max")
DEFAULT_EFFORT      = "high"

EFFORT_TRIGGERS = [
    (re.compile(r"^(?:for this (?:topic|one)[,:]?\s*)?think\s+(?:really\s+)?"
                r"hard(?:er)?(?:\s+about this)?\s*[:,\-]\s*", re.I), "max"),
    (re.compile(r"^(?:deep\s*think|think\s+deep(?:ly)?|ultrathink)\s*[:,\-]\s*", re.I), "max"),
    (re.compile(r"^(?:quick(?:ie|ly)?|fast one|don'?t overthink(?: this)?)\s*[:,\-]\s*", re.I), "low"),
]

EFFORT_WORDS = {  # for the effort:/think: command
    "max": "max", "maximum": "max", "hard": "max", "harder": "max", "deep": "max",
    "xhigh": "xhigh", "extra": "xhigh",
    "high": "high", "normal": "high", "default": "high", "reset": "high",
    "medium": "medium", "med": "medium",
    "low": "low", "quick": "low", "fast": "low", "light": "low",
}


def get_effort(sender):
    return active_effort.get(sender, {}).get("level", DEFAULT_EFFORT)


def set_effort_sticky(sender, level, turns=EFFORT_STICKY_TURNS):
    if level == DEFAULT_EFFORT:
        active_effort.pop(sender, None)
    else:
        active_effort[sender] = {"level": level, "remaining": turns}


def decrement_effort(sender):
    """Returns True when a non-default effort just expired."""
    s = active_effort.get(sender, {})
    if s.get("level") and s.get("remaining"):
        s["remaining"] -= 1
        if s["remaining"] <= 0:
            active_effort.pop(sender, None)
            return True
    return False
MODEL_STICKY_TURNS = 5  # how many turns a model override persists

# ─── Command map ──────────────────────────────────────────────────────────────

COMMAND_MAP = {
    "g":        ("model", "cloud_default"),
    "gp":       ("model", "cloud_top"),
    "s":        ("model", "cloud_sonnet"),
    "o":        ("model", "cloud_opus"),
    "f":        ("model", "cloud_fable"),
    "m":        ("model", "local"),
    "effort":   ("builtin", "effort"),
    "think":    ("builtin", "effort"),
    "fix":      ("builtin", "fix"),
    "build":    ("builtin", "build"),
    "skill":    ("builtin", "skill"),
    "browse":   ("builtin", "browse"),
    "mullvad":  ("builtin", "mullvad"),
    "ls":       ("shell", "ls"),
    "ps":       ("shell", "ps"),
    "cat":      ("shell", "cat"),
    "facts":    ("shell", "facts"),
    "vault":    ("vault", None),
    "analyze":  ("skill", "analyze"),
    "chk":      ("skill", "crosscheck"),
    "research": ("skill", "research"),
    "remind":   ("skill", "remind"),
    "msg":      ("skill", "msg"),
    "usage":    ("skill", "usage"),
    "review":   ("skill", "review"),
    "wc":       ("skill", "wc"),
    "wx":       ("builtin", "wx"),
    "archive":  ("builtin", "archive"),
    "voice":    ("builtin", "voice"),
    "save":     ("builtin", "save"),
    "skip":     ("builtin", "skip"),
    "stop":     ("builtin", "stop"),
    "forget":   ("builtin", "forget"),
    "fetch":    ("builtin", "fetch"),
    "memory":   ("builtin", "memory"),
    "rechunk":  ("builtin", "rechunk"),
    "help":     ("builtin", "help"),
}

# Legacy !cmd → new cmd name
LEGACY_MAP = [
    ("gemini pro", "gp"),
    ("gemini",     "g"),
    ("claude",     "s"),
    ("opus",       "o"),
    ("mistral",    "m"),
    ("local",      "m"),
    ("fast",       "g"),
    ("heavy",      "s"),
    ("analyze",    "analyze"),
    ("chk",        "chk"),
    ("crosscheck", "chk"),
    ("chk",        "chk"),
    ("research",   "research"),
    ("remind",     "remind"),
    ("msg",        "msg"),
    ("usage",      "usage"),
    ("wx",         "wx"),
    ("weather",    "wx"),
    ("archive",    "archive"),
    ("voice",      "voice"),
    ("soul",       "voice"),
    ("save",       "save"),
    ("remember",   "save"),
    ("skip",       "skip"),
    ("stop",       "stop"),
    ("vault",      "vault"),
    ("read",       "vault"),
    ("ls",         "ls"),
    ("ps",         "ps"),
    ("cat",        "cat"),
    ("facts",      "facts"),
    ("fetch",      "fetch"),
    ("retrieve",   "fetch"),
    ("memory",     "memory"),
    ("rechunk",    "rechunk"),
]

# ─── Help ─────────────────────────────────────────────────────────────────────

HELP_TEXT = """Chip commands

Models:
  g:    Gemini 3.5 Flash — fast, multimodal, all media
  gp:   Gemini Pro (3.5 Flash until Pro ships)
  s:    Claude Sonnet 5 — cheaper Claude tier
  o:    Claude Opus 5 — the default for Claude chat
  f:    Claude Fable 5 — frontier model, admin only
  m:    local Phi (phi4-mini) — slow, private, cold-loads
  Flags: -v verbose  -c concise  |  Example: s -v: analyze this contract
  Works on images too: s: [caption]  or  o: [caption]

Thinking effort (how hard Claude thinks; default high):
  think hard: <question>     max effort, sticks 5 turns
  quick: <question>          low effort, fast answers
  effort: max|high|low       set it explicitly (think: works too)
  Plain language works mid-chat too: "really think about this one"

Just ask (no prefix needed) — Chip can now do these itself in conversation:
  search the web, make/read PDF & Word documents and send them as
  attachments, message household users, set reminders, schedule its own
  follow-ups ("check on me tomorrow about X"), search the vault & memory,
  report per-model API costs ("what did Opus cost today").

Browser (admin):
  browse: <task> [via home]    drive a real browser on live sites; up to
                               15 min, one reply when done. If a site wants
                               a human check you get the picture; just reply
                               with what it says (or skip). stop: cancels.
                               'via home' sends ONLY this browser session out
                               the home IP (for retailers that block VPN
                               ranges) — the rest of the box stays on Mullvad.
                               Self-expires; 15 min hard cap; logged.
  mullvad: off [min] | on | status   whole-box VPN toggle (auto re-enables)

Self-repair (admin):
  fix: <problem description>   stage a repair (one problem, needs approval)
  fix: supersedes FIX-0002: <new order>   replace a staged order
  fix: continue FIX-0003: <note>   follow up in the same healer session
  fix: status [FIX-0003]       the index, or one record in full
  fix: approve [FIX-0003] [override] | reject [FIX-0003] | restore FIX-0003
  build: <name>: <what it does>   stage a new skill (adds files only)
  skill: list | off <name> | on <name>   add-on skills; off works at once
  Every order gets a stable FIX-NNNN id and nothing is ever deleted —
  reject and supersede are status changes, and a rejected order can be
  restored. Jobs run one at a time; approving a second queues it.
  Runs detached with full repo access; results arrive here. Nothing
  executes without the explicit approve, which only a human can type.

Scheduling (admin) — the line is ownership, not topic:
  Just ask in plain language ("check the mail every morning at six",
  "what runs automatically on this box").
  Chip's own jobs   it can list, create, retime, and switch off. A job
                    carries a note it acts on when it fires — never a
                    shell command — so firing one only wakes Chip up.
                    Off is a reversible flag; jobs are never deleted.
  System cron       read-only. Chip can tell you every entry and when it
                    next runs, and cannot add, edit, or remove any of
                    them. Those stay a crontab -e job for you.
  So "can you turn off that heartbeat" depends on who created it, not on
  what it does. Ask Chip to list the schedule and it will say which side
  of the line each one sits on.

Analysis pipelines:
  analyze: <entity>
    Deep 4-mode profile — quantitative baseline, historical context,
    leadership, strategic synthesis, then crosscheck. Countries and companies.
    Use when you want a complete picture, not just an answer. 15-30 min.
    Reply stop: to cancel.

  research: <query>
    Iterative web research — decomposes query into sub-questions, parallel
    search legs, gap-fill rounds, final synthesis with citations and
    confidence ratings, then crosscheck. Use for current events and open
    questions. 10-20 min. Reply stop: to cancel.

  chk: <claim or question>
    Dual-model arbitration. Opus and Gemini independently assess with live
    web search — no shared context between them. Opus arbitrates: agreement,
    divergence, unsupported claims. Calibrated probabilistic language.
    High-confidence answers. ~2-3 min.

Files & memory:
  vault: <query>           search knowledge base
  save:                    commit pending facts
  skip:                    discard pending facts
  forget: <keywords>       delete matching facts
  archive: <url>           save webpage

Comms:
  remind: [who] <time> [topic]
  msg: <name|all> [topic]
  wx: [location] [days] | reset

System:
  voice: <prompt> | reset
  usage: [YYYY-MM]
  stop:

Shell:
  ls:              vault domains
  ls: <domain>     docs in domain
  ls: recent       last 10 ingested
  ps:              system health
  cat: facts       confirmed memory
  cat: reminders   pending reminders
  cat: <file>      read whitelisted file
  facts: >> "text" append fact directly
  facts: | grep <term>

Pipeline operators:
  analyze: X | chk:          force crosscheck on any output
  s: X >> msg: the partner            send to the partner (you get copy)
  s: X >> facts:              save to memory
  ls: <domain> | vault: <term>   search <term> in one domain"""

# ─── Whitelisted dirs for cat ─────────────────────────────────────────────────

CAT_WHITELIST_DIRS = [
    f"{WORKSPACE}/skills/",
    f"{WORKSPACE}/data/archive/reports/",
    f"{WORKSPACE}/data/patches/",
    # Browse run artifacts. Already inside reports/ for `cat:`, but read_file
    # resolves a bare filename against each root, so listing the subdirectory
    # is what lets Chip read an artifact by the name system_health gives it.
    f"{WORKSPACE}/data/archive/reports/browse/",
]

def _resolve_whitelisted(target):
    """Resolve `target` to a real file inside CAT_WHITELIST_DIRS, or None.

    Audit H1: the old resolver used os.path.join() + a startswith() string
    compare, so `cat: ../.env` escaped the whitelist and returned API keys to
    any of the three users. realpath() both sides and require true containment
    so symlinks and ../ traversal can't walk out.
    """
    roots = [os.path.realpath(d) for d in CAT_WHITELIST_DIRS]

    def contained(p):
        return any(p == r or p.startswith(r + os.sep) for r in roots)

    target = (target or "").strip()
    if not target:
        return None
    for r in roots:
        cand = os.path.realpath(os.path.join(r, target))
        if contained(cand) and os.path.isfile(cand):
            return cand
    cand = os.path.realpath(target)
    if contained(cand) and os.path.isfile(cand):
        return cand
    return None


# ─── Buffer ───────────────────────────────────────────────────────────────────

hot_buffer = deque(maxlen=60)

def log_to_buffer(sender, user_text, response, history_roles=("user", "assistant")):
    ts   = datetime.now().strftime("%H:%M")
    name = USERS.get(sender, {}).get("name", sender)
    # Persistent history store (survives the nightly wipe — audit M3).
    # Buffers below stay as-is: the dream-cycle archive reads them.
    # history_roles: a deferred answer logged the user's turn when it deferred,
    # so only Chip's turn is written here (One Voice spec B.2).
    try:
        import skills.history as history
        if "user" in history_roles:
            history.log_turn(name.lower(), "user", user_text)
        if "assistant" in history_roles:
            history.log_turn(name.lower(), "assistant",
                             re.sub(r"^\[[^\]]+\]\s*", "", response or ""))
    except Exception as e:
        print(f"⚠️ history write: {e}")
    hot_buffer.append(f"[{ts}] {name}: {user_text}")
    hot_buffer.append(f"[{ts}] Chip: {response}")
    os.makedirs(BUFFER_DIR, exist_ok=True)
    # Collapse internal newlines to \n literal so each turn stays one line
    # load_history restores them on read
    u = user_text.replace("\n", " \\n ")
    r = response.replace("\n", " \\n ")
    for path in [
        f"{BUFFER_DIR}/chat_buffer.txt",
        f"{BUFFER_DIR}/buffer_{name.lower()}.txt",
    ]:
        with open(path, "a") as f:
            f.write(f"[{ts}] {name}: {u}\n")
            f.write(f"[{ts}] Chip: {r}\n\n")

# ─── Real-time fact extraction ───────────────────────────────────────────────

EXTRACTION_STATE_PATH = f"{WORKSPACE}/data/memory/extraction_state.json"

COMMAND_PREFIXES = {
    "analyze:", "research:", "chk:", "review:", "wc:", "o:", "gp:", "s:", "g:", "m:",
    "vault:", "ls:", "ps:", "cat:", "remind:", "memory:", "save:", "skip:", "forget:",
    "voice:", "usage:", "wx:", "msg:", "rechunk:", "ctx:",
}

def _load_extraction_state():
    try:
        with open(EXTRACTION_STATE_PATH) as f:
            return json.load(f)
    except Exception:
        return {}

def _save_extraction_state(state):
    try:
        os.makedirs(os.path.dirname(EXTRACTION_STATE_PATH), exist_ok=True)
        with open(EXTRACTION_STATE_PATH, "w") as f:
            json.dump(state, f)
    except Exception:
        pass

def _is_command_exchange(user_text):
    lower = user_text.lower().strip()
    return any(lower.startswith(p) for p in COMMAND_PREFIXES)

async def maybe_extract_facts(sender, user_text, response):
    """
    Word-count-threshold trigger for fact extraction — triggers at 300 input
    words OR 1000 output words, whichever comes first. Skips command exchanges.

    The actual extraction (buffer pull, Sonnet call, parse, commit) is
    delegated to skills.taxonomy.run_fact_extraction — this function only
    owns the threshold/state-tracking. Shift-triggered extraction (the other
    way run_fact_extraction gets called) lives in skills.taxonomy and fires
    independently on detected domain shifts, regardless of word count.

    State format: {"<user>": {"input": N, "output": N}, ...}
    """
    try:
        name = USERS.get(sender, {}).get("name", "").lower()
        if not name:
            return

        # Skip structured command outputs for THIS trigger only — taxonomy
        # tagging and shift-detection still see command exchanges (recipe
        # threads routinely include chk:/research: detours worth capturing).
        if _is_command_exchange(user_text):
            return

        input_words  = len(user_text.split())
        output_words = len(response.split())

        state     = _load_extraction_state()
        user_state = state.get(name, {})
        if not isinstance(user_state, dict):
            user_state = {"input": 0, "output": 0}

        user_state["input"]  = user_state.get("input",  0) + input_words
        user_state["output"] = user_state.get("output", 0) + output_words
        state[name] = user_state
        _save_extraction_state(state)

        if user_state["input"] < 300 and user_state["output"] < 1000:
            return

        print(f"💡 [{name}] extraction threshold hit "
              f"(input={user_state['input']}w output={user_state['output']}w) — extracting facts...")

        user_state["input"]  = 0
        user_state["output"] = 0
        state[name] = user_state
        _save_extraction_state(state)

        taxonomy.enqueue_work(kind="threshold_extract", sender=sender, name=name)

    except Exception as e:
        print(f"⚠️ maybe_extract_facts error: {e}")

# ─── Context loading ──────────────────────────────────────────────────────────

# Household and persona content comes from config/users.json (chip_config) —
# nothing personal is hardcoded here (19 Nov 2026).
SOUL_FALLBACK = "\n\n".join(filter(None, [
    """# CHIP

Direct without being curt. No hedging unless the hedge is the content.
Dry humor when the situation earns it, not on a schedule. Push back when
something's wrong — plainly, without softening it into uselessness.
Don't moralize. Don't apologize for knowing things.""",
    chip_config.HOUSE_MOTTO,
    chip_config.HOUSEHOLD_BLURB,
] + [chip_config.persona(p) for p in chip_config.USERS_FULL]))

LENGTH_INSTRUCTIONS = {
    "default": (
        "\nRESPONSE LENGTH: Answer the question; two or three sentences unless "
        "more is asked for. No preamble, no recap, no narration of your own "
        "process or tools."
    ),
    "verbose": "\nRESPONSE LENGTH: Comprehensive. Full detail, do not truncate.",
    "concise": "\nRESPONSE LENGTH: Maximum 2 sentences. Direct answer only.",
}

FORMAT_INSTRUCTION = (
    "\nFORMATTING: Plain prose only. No markdown. No asterisks. No bold. "
    "No headers. No HTML. Bullet points only when listing 3+ distinct items. "
    "Output must render cleanly as plain text on a mobile screen."
)

def get_sticky_tier(sender):
    """Return persisted model tier if set, else None."""
    s = active_model.get(sender, {})
    if s.get("tier") and s.get("remaining", 0) > 0:
        return s["tier"]
    return None

def set_sticky_tier(sender, tier):
    """Set model tier to persist for MODEL_STICKY_TURNS turns."""
    active_model[sender] = {"tier": tier, "remaining": MODEL_STICKY_TURNS}

def decrement_sticky(sender):
    """Decrement sticky counter. Returns True if expired."""
    s = active_model.get(sender, {})
    if s.get("tier") and s.get("remaining"):
        s["remaining"] -= 1
        if s["remaining"] <= 0:
            active_model.pop(sender, None)
            return True
    return False

def load_soul():
    try:
        return open(f"{WORKSPACE}/agents/chip/SOUL.md").read()
    except Exception:
        return SOUL_FALLBACK

def load_user_identity(sender):
    name = USERS.get(sender, {}).get("name", "").lower()
    path = f"{WORKSPACE}/config/identities/{name}.md"
    if os.path.exists(path):
        try:
            content = open(path).read().strip()
            if content:
                return content
        except Exception:
            pass
    # Persona fallbacks come from config/users.json (chip_config) — nothing
    # personal hardcoded (19 Nov 2026). The identity .md file above, rebuilt by
    # the dream cycle, remains the richer primary source.
    _p = chip_config.persona(sender)
    if _p:
        return _p
    return f"You are speaking with {name.title()}."


def load_context_sections(sections):
    parts = []
    for sec in (sections or []):
        path = f"{WORKSPACE}/config/context/{sec}.md"
        if os.path.exists(path):
            try:
                content = open(path).read().strip()
                if content:
                    parts.append(content)
            except Exception:
                pass
    return "\n\n".join(parts)

def _get_media_catalog(sender):
    """Full media catalog for users flagged media_catalog in config/users.json,
    formatted for system-prompt injection. Empty for everyone else."""
    if not chip_config.media_catalog_user(sender):
        return ""
    try:
        import sqlite3 as _sq
        _db = f"{WORKSPACE}/data/memory/chip_media.db"
        if not os.path.exists(_db):
            return ""
        _con = _sq.connect(_db)
        _rows = _con.execute(
            "SELECT media_type, title, creator, rating, date_reviewed "
            "FROM media_reviews WHERE user=? "
            "ORDER BY date_reviewed, rating DESC",
            (USERS.get(sender, {}).get("name", "").lower(),)
        ).fetchall()
        _con.close()
        if not _rows:
            return ""
        _lines = []
        for _mtype, _title, _creator, _rating, _date_r in _rows:
            _line = f"[{_mtype.upper()}] {_title}"
            if _creator: _line += f" by {_creator}"
            if _rating is not None: _line += f" — {_rating}/10"
            if _date_r:  _line += f" ({_date_r})"
            _lines.append(_line)
        return "\n".join(_lines)
    except Exception as _e:
        print(f"Media catalog inject error: {_e}")
        return ""

def _get_world_cup_context(sender):
    """Inject recent World Cup state into the admin's system prompt during tournament."""
    if sender != chip_config.ADMIN_PHONE:
        return ""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    if not (datetime(2026, 6, 11, tzinfo=timezone.utc) <=
            now <= datetime(2026, 7, 20, tzinfo=timezone.utc)):
        return ""
    try:
        import json as _json
        _sp = f"{WORKSPACE}/data/world_cup_state.json"
        if not os.path.exists(_sp):
            return ""
        _state = _json.load(open(_sp))
        _lines = ["WORLD CUP 2026 CONTEXT (live event data — do not treat as personal facts):"]
        # Opted-in matches
        _opted = _state.get("opted_in", [])
        _e2c   = _state.get("espn_to_chip", {})
        _ls    = _state.get("last_scores", {})
        if _opted:
            _live = []
            for _eid in _opted:
                _n = _e2c.get(_eid, "?")
                _sc = _ls.get(_eid, {})
                if _sc:
                    _live.append(
                        f"#{_n} {_sc.get('home_name','')} "
                        f"{_sc.get('home_score',0)}-{_sc.get('away_score',0)} "
                        f"{_sc.get('away_name','')} ({_sc.get('status','').replace('STATUS_','')})"
                    )
            if _live:
                _lines.append("Tracking: " + " | ".join(_live))
        # Recent finished matches from last_scores
        _recent = [
            f"#{_e2c.get(_eid,'?')} {_d['home_name']} {_d['home_score']}-{_d['away_score']} {_d['away_name']}"
            for _eid, _d in _ls.items()
            if "FINAL" in _d.get("status","") and _e2c.get(_eid)
        ]
        if _recent:
            _lines.append("Recent results: " + ", ".join(_recent[-5:]))
        _muted = _state.get("mute_until")
        if _muted:
            _lines.append(f"Alerts muted until {_muted}")
        return "\n".join(_lines) if len(_lines) > 1 else ""
    except Exception as _e:
        return ""

def load_context_blocks(sender, sections=None, rag_context=None, rag_facts=None,
                        length="default"):
    """System prompt as three blocks, stable-first, for prompt caching.

    Caching is a prefix match, and the old single-string builder opened with
    the CURRENT TIMESTAMP — which invalidated the entire cache on every single
    message. Order now: [stable: soul + identity + formatting] [semi-stable:
    the partner's catalog, only re-caches when she logs media] [volatile: time,
    sections, facts, RAG, voice, length]. The Claude path puts cache
    breakpoints after the first two; Gemini joins them into one string.
    """
    from datetime import datetime as _dt
    stable = "\n\n".join([load_soul(),
                          f"---\n{load_user_identity(sender)}",
                          FORMAT_INSTRUCTION])
    # Per-user communication preferences (One Voice spec, Part E, 20 Nov
    # 2026). Stable per sender, so it lives in the cached block.
    _style = chip_config.style(sender)
    if _style:
        _sname = USERS.get(sender, {}).get("name", "this user").upper()
        stable += f"\n\n---\nHOW TO TALK WITH {_sname} (overrides any general length guidance):\n{_style}"
    if USERS.get(sender, {}).get("tier") == "admin":
        # Self-knowledge pointer (19 Nov 2026): the dev notes Claude Code
        # keeps about Chip are readable via the read_session_notes tool.
        # Per-sender stable block, so this stays cache-friendly.
        stable += ("\n\nSELF-KNOWLEDGE: Your own development history, recent "
                   "upgrades, and current capability notes are readable via "
                   "the read_session_notes tool (read chip_current_state.md "
                   "first; it overrides the older dated reports). "
                   "Consult it before answering questions about what changed "
                   "about you, what you can do, or why you work the way you do "
                   "— don't guess from training data.")

    semi = ""
    _media_catalog = _get_media_catalog(sender)
    if _media_catalog:
        _cname = USERS.get(sender, {}).get("name", "USER").upper()
        semi = (f"{_cname}'S COMPLETE MEDIA CATALOG (ground truth — use for all "
                f"book/movie/tv questions):\n{_media_catalog}")

    _now = _dt.now().strftime("%A, %B %-d, %Y at %-I:%M %p CDT")
    vol = [f"Current date and time: {_now}"]
    # Dated system status (20 Nov 2026): when Chip itself changes, earlier
    # conversation about it goes stale, and the history window and rolling
    # summary keep repeating it until they fold forward. A short, expiring
    # status line in data/system_status.json corrects that for admins.
    if sender == chip_config.ADMIN_PHONE:
        try:
            _st = json.load(open(f"{WORKSPACE}/data/system_status.json"))
            if _dt.now().isoformat() < _st.get("until", ""):
                vol.append("SYSTEM STATUS (authoritative; where earlier conversation "
                           f"disagrees, this wins): {_st['text']}")
        except Exception:
            pass
    try:  # rolling summary of conversation older than the verbatim window
        import skills.history as _h
        _summ = _h.get_summary(USERS.get(sender, {}).get("name", "").lower())
        if _summ:
            vol.append("---\nCONVERSATION MEMORY (summary of earlier exchanges "
                       f"with this user):\n{_summ}")
    except Exception:
        pass
    if sections:
        sec_content = load_context_sections(sections)
        if sec_content:
            vol.append(f"---\n{sec_content}")
    if rag_facts:
        vol.append(f"---\nCONFIRMED FACTS FROM MEMORY:\n{rag_facts}")
    _wc_context = _get_world_cup_context(sender)
    if _wc_context:
        vol.append(f"---\n{_wc_context}")
    if rag_context:
        vol.append(f"---\nRELEVANT CONTEXT FROM KNOWLEDGE BASE:\n{rag_context}")
    voice = active_voices.get(sender, {}).get("prompt")
    if voice:
        vol.append(f"---\nVOICE MODIFIER: {voice}")
    vol.append(LENGTH_INSTRUCTIONS.get(length, LENGTH_INSTRUCTIONS["default"]))
    return stable, semi, "\n\n".join(vol)


def load_context(sender, sections=None, rag_context=None, rag_facts=None, length="default"):
    stable, semi, volatile = load_context_blocks(
        sender, sections, rag_context, rag_facts, length)
    return "\n\n".join([stable] + ([semi] if semi else []) + [volatile])

def load_history(sender, max_turns=30):
    """History now comes from the persistent store (skills/history.py,
    audit M3): char-budgeted verbatim turns that SURVIVE the nightly buffer
    wipe. The rolling summary of older context rides in the system prompt
    via load_context_blocks, not here."""
    import skills.history as history
    name = USERS.get(sender, {}).get("name", "user")
    return history.get_history(name.lower(), max_turns=max_turns)

def decrement_voice(sender):
    v = active_voices.get(sender)
    if v and v.get("remaining") is not None:
        v["remaining"] -= 1
        if v["remaining"] <= 0:
            active_voices[sender] = {"prompt": None, "remaining": None}
            return True
    return False

# ─── RAG ──────────────────────────────────────────────────────────────────────

_EMB_CACHE = {"sig": None, "M": None, "meta": None}


def _embedding_matrix():
    """(normalised float32 matrix, meta) for every embedded vault chunk.

    Cached in-process and invalidated by (row count, max id), so ingest and
    rechunk pick themselves up on the next query. ~34MB for the current vault;
    the rebuild costs ~4s and only ever runs off the event loop because every
    caller reaches retrieve_rag() through asyncio.to_thread (audit M1/M2).
    """
    conn = sqlite3.connect(VAULT_DB)
    try:
        sig = conn.execute(
            "SELECT COUNT(*), IFNULL(MAX(id), 0) FROM chunks WHERE embedding IS NOT NULL"
        ).fetchone()
        if _EMB_CACHE["sig"] == sig and _EMB_CACHE["M"] is not None:
            return _EMB_CACHE["M"], _EMB_CACHE["meta"]

        print(f"🧮 Building vault embedding matrix ({sig[0]} chunks)...")
        # One query, not two zipped together — pairing rows from separate
        # SELECTs relies on unspecified ordering, and a divergence would
        # silently attach every chunk to the wrong vector.
        rows = conn.execute(
            "SELECT c.chunk_text, d.filename, c.id, c.domain, c.embedding FROM chunks c "
            "JOIN documents d ON c.document_id = d.id "
            "WHERE c.embedding IS NOT NULL "
            "AND (c.sensitive IS NULL OR c.sensitive = 0)"
        ).fetchall()
    finally:
        conn.close()

    import json as _json
    vecs, meta = [], []
    for chunk_text, fname, cid, dom, emb_json in rows:
        try:
            vecs.append(_json.loads(emb_json))
            meta.append((chunk_text, fname, cid, dom))
        except Exception:
            continue
    if not vecs:
        _EMB_CACHE.update({"sig": sig, "M": None, "meta": []})
        return None, []

    M = np.asarray(vecs, dtype=np.float32)
    M /= (np.linalg.norm(M, axis=1, keepdims=True) + 1e-9)
    _EMB_CACHE.update({"sig": sig, "M": M, "meta": meta})
    print(f"🧮 Vault matrix ready: {M.shape[0]} x {M.shape[1]}")
    return M, meta


def retrieve_rag(query, domain=None, top_k=5):
    """
    True independent RRF (Reciprocal Rank Fusion).

    Previously: FTS5 found candidates first, then cosine only re-ranked that
    same narrow set — if FTS5's keyword MATCH missed a document entirely
    (different wording, stop-word filtering, etc.), cosine never got an
    independent chance at the full corpus and retrieval silently failed.

    Now: FTS5 and cosine each search independently over their own candidate
    pools, then get fused by reciprocal rank. A document semantically close
    to the query can surface even with zero keyword overlap, and vice versa.
    """
    try:
        conn   = sqlite3.connect(VAULT_DB)
        clean  = re.sub(r'[^\w\s]', ' ', query)
        tokens = [t for t in clean.split() if len(t) > 2]

        domain_clause_fts    = "AND c.domain = ?" if domain else "AND c.domain != 'private'"
        domain_clause_cosine = "AND c.domain = ?" if domain else "AND c.domain != 'private'"

        # ── Pass 1: FTS5 keyword candidates (independent) ──────────────────
        fts_rows = []
        if tokens:
            fts_q  = " OR ".join(tokens[:10])
            params = (fts_q, domain, top_k * 6) if domain else (fts_q, top_k * 6)
            try:
                fts_rows = conn.execute(
                    f"SELECT c.chunk_text, d.filename, c.document_id FROM chunks c "
                    f"JOIN chunks_fts f ON c.id = f.rowid "
                    f"JOIN documents d ON c.document_id = d.id "
                    f"WHERE chunks_fts MATCH ? {domain_clause_fts} "
                    f"AND (c.sensitive IS NULL OR c.sensitive = 0) "
                    f"ORDER BY rank LIMIT ?",
                    params
                ).fetchall()
            except Exception as e:
                print(f"⚠️ FTS5 pass failed: {e}")
                fts_rows = []

        # Cap at 2 chunks per document, preserve FTS5 rank order
        doc_chunk_count = {}
        fts_deduped = []
        for chunk_text, fname, doc_id in fts_rows:
            count = doc_chunk_count.get(doc_id, 0)
            if count < 2:
                doc_chunk_count[doc_id] = count + 1
                fts_deduped.append((chunk_text, fname))
        fts_rank = {ct: i for i, (ct, _) in enumerate(fts_deduped)}
        fts_fname = {ct: fn for ct, fn in fts_deduped}

        # ── Pass 2: cosine over the WHOLE vault (audit M2) ─────────────────
        # Was LIMIT 500 with no ORDER BY against ~11.9k embedded chunks, so
        # ~96% of the vault was unreachable by semantic search and which 500
        # you got was down to SQLite page order. Now scored from a cached
        # numpy matrix: ~0.05s for the full corpus once warm.
        cos_rank  = {}
        cos_fname = {}
        cos_sim   = {}
        try:
            M, meta = _embedding_matrix()
            if M is not None and len(meta):
                emb_resp = requests.post(
                    f"{OLLAMA_URL}/api/embeddings",
                    json={"model": MODELS["embed"], "prompt": query[:2000]},
                    timeout=30,
                )
                query_emb = emb_resp.json().get("embedding", [])

                if query_emb:
                    q = np.asarray(query_emb, dtype=np.float32)
                    q /= (np.linalg.norm(q) + 1e-9)

                    if domain:
                        mask = np.array([m[3] == domain for m in meta], dtype=bool)
                    else:
                        mask = np.array([m[3] != "private" for m in meta], dtype=bool)

                    sims = M @ q
                    sims[~mask] = -2.0

                    # Only the head can survive the 2-per-document cap.
                    want  = min(len(sims), max(top_k * 40, 200))
                    order = np.argpartition(-sims, want - 1)[:want]
                    order = order[np.argsort(-sims[order])]

                    fname_count = {}
                    rank_i = 0
                    for idx in order:
                        if sims[idx] <= -2.0:
                            break
                        chunk_text, fname, _cid, _dom = meta[idx]
                        count = fname_count.get(fname, 0)
                        if count < 2:
                            fname_count[fname] = count + 1
                            cos_rank[chunk_text]  = rank_i
                            cos_fname[chunk_text] = fname
                            cos_sim[chunk_text]   = float(sims[idx])
                            rank_i += 1
                        if rank_i >= top_k * 6:
                            break
        except Exception as e:
            print(f"⚠️ Cosine pass failed: {e}")
        finally:
            try:
                conn.close()
            except Exception:
                pass

        if not fts_rank and not cos_rank:
            return "", 0.0, []

        # ── Fuse: true RRF over the union of both independent rankings ─────
        K = 60
        rrf_scores = {}
        all_fname  = {**fts_fname, **cos_fname}
        for chunk_text in set(list(fts_rank.keys()) + list(cos_rank.keys())):
            rrf = 0.0
            if chunk_text in fts_rank:
                rrf += 1.0 / (K + fts_rank[chunk_text])
            if chunk_text in cos_rank:
                rrf += 1.0 / (K + cos_rank[chunk_text])
            rrf_scores[chunk_text] = rrf

        ranked = sorted(rrf_scores.items(), key=lambda x: x[1], reverse=True)

        seen = set()
        rows = []
        for chunk_text, _ in ranked:
            fname = all_fname.get(chunk_text, "")
            if fname and fname not in seen:
                seen.add(fname)
                rows.append((chunk_text, fname))
            if len(rows) >= top_k:
                break

        if not rows:
            return "", 0.0, []

        filenames = list(dict.fromkeys(r[1] for r in rows))
        context   = "\n---\n".join(r[0] for r in rows)

        # Confidence = real semantic similarity when we have it (the full-
        # vault cosine pass now computes it anyway), with the old token-
        # overlap formula as the floor so FTS-only hits still score. The old
        # gate was overlap-only (audit M2: "confidence gate is token overlap,
        # not similarity"), which zero-scored perfectly good semantic matches
        # phrased with different words.
        top_text = rows[0][0].lower()
        hits     = sum(1 for t in tokens if t in top_text)
        overlap  = hits / max(len(tokens), 1)
        score    = max(cos_sim.get(rows[0][0], 0.0), overlap)

        return context, score, filenames

    except Exception as e:
        print(f"⚠️ retrieve_rag error: {e}")
        return "", 0.0, []

def retrieve_facts_rag(query, top_k=8, sender=None):
    try:
        conn   = sqlite3.connect(CONTEXT_DB)
        clean  = re.sub(r'[^\w\s]', ' ', query)
        tokens = [t for t in clean.split() if len(t) > 2]
        # Audit H3: with no sender this used to return EVERY user's facts.
        # Fail closed instead, so a future caller that forgets to thread the
        # sender through leaks nothing.
        user = USERS.get(sender, {}).get("name", "").lower() if sender else None
        if not user:
            conn.close()
            return ""
        # FTS over the user's WHOLE fact store (19 Nov 2026). The old path
        # substring-matched only the 60 most recent rows, so the partner's older
        # facts (197 active) were unreachable regardless of relevance. Also
        # excludes superseded/archived rows — status was never filtered, so
        # stale versions of corrected facts were leaking into context.
        rows, use_recency = [], not tokens
        if tokens:
            try:
                fts_q = " OR ".join(t.replace('"', '') for t in tokens[:10])
                rows = conn.execute(
                    "SELECT f.content FROM facts f "
                    "JOIN facts_fts ft ON f.id = ft.rowid "
                    "WHERE facts_fts MATCH ? AND f.confirmed=1 "
                    "AND (f.status IS NULL OR f.status='active') AND f.user=? "
                    "ORDER BY rank LIMIT ?",
                    (fts_q, user, top_k)
                ).fetchall()
                # No FTS match = no relevant facts — inject nothing rather
                # than padding the prompt with unrelated recent facts.
            except Exception as fe:
                print(f"⚠️ Facts FTS failed ({fe}) — recency fallback")
                use_recency = True
        if use_recency:
            rows = conn.execute(
                "SELECT content FROM facts WHERE confirmed=1 "
                "AND (status IS NULL OR status='active') AND user=? "
                "ORDER BY created_at DESC LIMIT ?",
                (user, top_k)
            ).fetchall()
        conn.close()
        return "\n".join(r[0] for r in rows)
    except Exception as e:
        print(f"⚠️ Facts RAG error: {e}")
        return ""

def get_rag_context(query, domain=None, sender=None):
    vault_txt, score, filenames = retrieve_rag(query, domain=domain)
    facts_txt = retrieve_facts_rag(query, sender=sender)
    parts = []
    if facts_txt:
        parts.append(f"CONFIRMED FACTS:\n{facts_txt}")
    if vault_txt:
        parts.append(f"KNOWLEDGE VAULT:\n{vault_txt}")
    return "\n\n".join(parts), score, filenames

# ─── Deterministic pre-router ────────────────────────────────────────────────

def _load_routing_constants():
    """Load config/routing_constants.py if present (the household's real,
    gitignored vocabulary), else the shipped generic
    config/routing_constants.example.py, else None (inline defaults)."""
    import importlib.util
    for fname in ("routing_constants.py", "routing_constants.example.py"):
        path = f"{WORKSPACE}/config/{fname}"
        if not os.path.exists(path):
            continue
        try:
            spec = importlib.util.spec_from_file_location("routing_constants", path)
            mod  = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            print(f"routing constants loaded from config/{fname}")
            return mod
        except Exception as e:
            print(f"WARNING routing constants: {fname} failed to load ({e})")
    print("WARNING routing constants: none found - using inline defaults")
    return None

_RC = _load_routing_constants()

def quick_route(text: str, sender: str):
    """
    Deterministic pre-router. Returns decision dict or None (→ use phi4-mini).
    Runs in microseconds. Handles ~80% of messages without LLM.
    """
    words  = text.strip().split()
    nwords = len(words)
    tlower = text.lower().strip()
    default = USERS.get(sender, {}).get("default_model", "cloud_default")

    # 1. Exact match fast-path (single acks, greetings)
    if _RC and tlower in getattr(_RC, "FAST_PATH_EXACT", set()):
        return {"route": default, "rag_domains": [], "context_sections": [],
                "doc_hints": [], "intent": "ack", "confidence": 1.0,
                "_skip_rag": True, "_fast": True}

    # 2. Short message fast-path
    fast_threshold = getattr(_RC, "FAST_PATH_WORDS", 7) if _RC else 7
    domain_words   = set()
    if _RC:
        for d, kws in getattr(_RC, "DOMAIN_KEYWORDS", {}).items():
            for kw in kws:
                if kw in tlower:
                    domain_words.add(d)
                    break

    if nwords <= fast_threshold and not domain_words:
        # Don't fast-path skill commands — let pipeline handle them
        if any(tlower.startswith(p) for p in COMMAND_PREFIXES):
            return None
        conv_starts = getattr(_RC, "CONVERSATIONAL_STARTS", []) if _RC else []
        analytical  = getattr(_RC, "ANALYTICAL_TRIGGERS", []) if _RC else []
        is_analytical = any(t in tlower for t in analytical)
        if not is_analytical:
            skip_rag = nwords <= fast_threshold and not any(
                w in tlower for w in ["vault", "doc", "find", "search", "saved"]
            )
            return {"route": default, "rag_domains": [], "context_sections": [],
                    "doc_hints": [], "intent": "conversational", "confidence": 1.0,
                    "_skip_rag": skip_rag, "_fast": True}

    # 3. Vault search intent
    vault_phrases = getattr(_RC, "VAULT_PHRASES", []) if _RC else []
    if any(p in tlower for p in vault_phrases) or "vault:" in tlower:
        domains = list(domain_words)
        return {"route": "vault_only", "rag_domains": domains,
                "context_sections": [], "doc_hints": [],
                "intent": "vault_search", "confidence": 0.9, "_fast": True}

    # 4. Domain-scoped query (has domain keywords, not vault search)
    if domain_words:
        domains = list(domain_words)
        return {"route": default, "rag_domains": domains,
                "context_sections": domains, "doc_hints": [],
                "intent": f"domain:{'+'.join(domains)}", "confidence": 0.85,
                "_fast": True}

    # 5. the partner topics — skip domain routing
    partner_topics = getattr(_RC, "PARTNER_TOPICS", set()) if _RC else set()
    if any(t in tlower for t in partner_topics):
        return {"route": default, "rag_domains": ["media"], "context_sections": ["media"],
                "doc_hints": [], "intent": "media_lifestyle", "confidence": 0.85,
                "_fast": True}

    # 6. Fall through → phi4-mini
    return None

# ─── Ministral Router ─────────────────────────────────────────────────────────

# (The phi4 ROUTER_PROMPT lived here; removed with the LLM-router hop.)

async def route(session, text, sender):
    """quick_route() missed — go straight to the sender's default model.

    The phi4-mini LLM-router hop was removed 19 Nov 2026: it added up to 25s
    before the real model even started, and with Opus 5 + native tools as the
    default, the conversational model is a better router than a 3.8B
    classifier ever was. quick_route (microseconds) still handles the ~80% of
    messages with keyword/length signals; everything else lands here. phi4
    now serves only the explicit m: prefix.
    """
    default = USERS.get(sender, {}).get("default_model", "cloud_default")
    call_logger.log_router("default", default)
    return {
        "route": default, "rag_domains": [],
        "context_sections": [], "doc_hints": [],
        "intent": "conversational", "confidence": 0.5,
    }

# ─── Markdown stripper ────────────────────────────────────────────────────────

CODE_FENCE_RE = re.compile(r'```.*?(?:```|\Z)', re.DOTALL)


def strip_markdown(text):
    """Flatten markdown for Signal, which renders none of it.

    Fenced blocks are pulled out before the emphasis passes and put back
    after. They used to be deleted outright (`re.sub(r'```.*?```', '')`), so
    any code Chip sent arrived as a hole in the message. The emphasis patterns
    also ran with DOTALL, which meant two snake_case identifiers — or a stray
    '*' used as multiplication — could pair up several paragraphs apart and
    swallow everything between them. Both read to the user as a reply that
    tailed off. Fences are kept so the splitter can see where code starts and
    ends and avoid cutting through it.
    """
    vault = []

    def _stash(value):
        vault.append(value)
        return f"\x00{len(vault) - 1}\x00"

    text = CODE_FENCE_RE.sub(lambda m: _stash(m.group(0)), text)
    # Inline code loses its backticks but keeps its content, and is protected
    # from the emphasis passes for the same reason as a fenced block.
    text = re.sub(r'`([^`\n]+)`', lambda m: _stash(m.group(1)), text)

    # Line-bounded, and the delimiters must hug non-space, so emphasis can no
    # longer reach across a paragraph. The underscore form additionally
    # refuses to fire mid-identifier, which is what mangled snake_case.
    text = re.sub(r'\*{1,3}([^*\s](?:[^*\n]*[^*\s])?)\*{1,3}', r'\1', text)
    text = re.sub(r'(?<![A-Za-z0-9_])_{1,3}([^_\s](?:[^_\n]*[^_\s])?)_{1,3}(?![A-Za-z0-9_])',
                  r'\1', text)
    text = re.sub(r'^#{1,6}\s+', '', text, flags=re.MULTILINE)
    text = re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', text)
    text = re.sub(r'^\s*[\*\-]\s+', '• ', text, flags=re.MULTILINE)
    text = re.sub(r'\n{3,}', '\n\n', text)

    for i, original in enumerate(vault):
        text = text.replace(f"\x00{i}\x00", original)
    return text.strip()

# ─── Cloud dispatch ───────────────────────────────────────────────────────────

def _model_tag(model_name, fallback=False):
    if "fable" in model_name:
        return TAGS["cloud_fable"]
    if "opus" in model_name:
        return TAGS["cloud_opus"]
    if "sonnet" in model_name:
        return TAGS["cloud_sonnet"]
    if "claude" in model_name:
        return TAGS["cloud_heavy"]
    if model_name == MODELS.get("cloud_basic"):
        # Only tag as cloud_basic if we're actually using that tier explicitly;
        # gemini-2.5-flash used as fallback still gets fallback tag
        return TAGS["cloud_basic"]
    return TAGS["fallback"] if fallback else TAGS["cloud_default"]

# ─── Native tool-calling ───────────────────────────────────────────────────────
# Both Claude and Gemini get the same three tools, model decides when to use
# them. Replaces what would otherwise be a separate phi4 classification pass
# for "remember this" / "adjust your voice" / mid-conversation vault lookups —
# the conversational model already has full live context, no second call
# needed. Capped at 3 rounds to prevent a runaway tool-chain.

# Tool definitions and execution moved to skills/tool_calling.py
# (TOOL_DEFS, TOOL_VISIBILITY_NOTE, gemini_tools(), execute_tool())
# ─── Cloud model calls ──────────────────────────────────────────────────────────

def _claude_sync_raw(messages, system, model, tools=None, effort=DEFAULT_EFFORT):
    """Returns the raw response object — needed so the caller can inspect
    stop_reason and content blocks for tool calls.

    Since 19 Nov 2026 this delegates to skills.llm, which owns the per-model
    parameter quirks (Fable's always-on thinking, removed sampling params),
    logs usage, and carries the max-tokens empty-reply backstop — so the old
    Patch D failure (adaptive thinking exhausting CLAUDE_MAX_TOKENS and
    returning 0 text blocks) retries itself instead of going silent. Default
    effort is now 'high' per the premier-model policy; the per-user sticky
    effort ('think hard: …' → max, 'quick: …' → low) arrives via `effort`.

    Fable 5 rides along as a server-side ADVISOR on every chat call — the
    executor consults it mid-turn for planning when it decides to (max 2
    consultations, cached advisor prompt). NOTE: callers must NOT also call
    call_logger.log_model_call for these responses — llm already does.
    """
    from skills import llm as _llm
    return _llm.claude_raw_sync(
        messages, system=system, model=model, effort=effort,
        max_tokens=CLAUDE_MAX_TOKENS, tools=tools,
        advisor=(model != _llm.FABLE),
    )

async def ask_cloud_with_pdf(session, user_text, sender, file_path, history=None, length="default"):
    """Send a PDF file directly to Claude for analysis."""
    system  = load_context(sender, sections=[], length=length)
    history = history or []
    with open(file_path, "rb") as f:
        pdf_b64 = base64.b64encode(f.read()).decode()

    messages = [{"role": t["role"], "content": t["content"]} for t in history]
    messages.append({"role": "user", "content": [
        {"type": "document", "source": {"type": "base64",
                                        "media_type": "application/pdf", "data": pdf_b64}},
        {"type": "text", "text": user_text},
    ]})

    loop   = asyncio.get_event_loop()
    def _call():
        client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
        resp   = client.messages.create(
            model=MODELS["cloud_heavy"], max_tokens=CLAUDE_MAX_TOKENS,
            system=system, messages=messages
        )
        text_blocks = [b.text for b in resp.content if b.type == "text"]
        call_logger.log_model_call(
            model=MODELS["cloud_heavy"], thinking=True,
            tokens_in=resp.usage.input_tokens, tokens_out=resp.usage.output_tokens,
            stop_reason=resp.stop_reason, tools=[],
        )
        return "\n".join(text_blocks), resp.usage.input_tokens, resp.usage.output_tokens
    answer, in_tok, out_tok = await asyncio.to_thread(_call)
    # usage is logged inside call_logger.log_model_call() — see audit H5
    return answer

TOOL_ROUND_CAP = 3

async def ask_cloud(session, user_text, sender, tier="cloud_default",
                    rag_context=None, rag_facts=None, history=None,
                    sections=None, length="default", fallback=False,
                    allowed_tools=None):
    model_name = MODELS.get(tier, MODELS["cloud_default"])
    tag        = _model_tag(model_name, fallback=fallback)
    # allowed_tools (One Voice spec B.2): a result turn carries untrusted web
    # text, so it gets a fixed read-only tool list. Enforced twice — the model
    # only sees these tools, and execute_tool refuses anything else for the
    # rest of this call (a contextvar, inherited by the parallel tool tasks).
    _allow_tok = tool_calling.ALLOWED_TOOLS.set(
        frozenset(allowed_tools) if allowed_tools is not None else None)
    try:
        return await _ask_cloud_inner(session, user_text, sender, tier, model_name, tag,
                                      rag_context, rag_facts, history, sections, length)
    finally:
        tool_calling.ALLOWED_TOOLS.reset(_allow_tok)


async def _ask_cloud_inner(session, user_text, sender, tier, model_name, tag,
                           rag_context, rag_facts, history, sections, length):
    # `tier` above is the ROUTING tier (cloud_default/cloud_heavy). The
    # user's permission tier is separate, and decides which tools are even
    # visible — admin-only tools are withheld from non-admins' tool list
    # entirely, on top of the per-tool check inside execute_tool().
    user_tier  = USERS.get(sender, {}).get("tier", "user")
    user_tools = tool_calling.tools_for(user_tier)   # honours ALLOWED_TOOLS
    _stable, _semi, _volatile = load_context_blocks(
        sender, sections=sections, rag_context=rag_context,
        rag_facts=rag_facts, length=length)
    # Gemini takes the joined string; Claude takes blocks with cache
    # breakpoints after the stable and semi-stable sections.
    system = "\n\n".join([_stable] + ([_semi] if _semi else []) + [_volatile]) \
             + tool_calling.TOOL_VISIBILITY_NOTE
    system_blocks = (
        [{"type": "text", "text": _stable + tool_calling.TOOL_VISIBILITY_NOTE,
          "cache_control": {"type": "ephemeral"}}]
        + ([{"type": "text", "text": _semi,
             "cache_control": {"type": "ephemeral"}}] if _semi else [])
        + [{"type": "text", "text": _volatile}]
    )
    history    = history or []
    name       = USERS.get(sender, {}).get("name", "").lower()

    for attempt in range(3):
        try:
            if "gemini" in model_name:
                print(f"☁️  {tag} ({model_name})")
                url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
                       f"{model_name}:generateContent?key={GEMINI_KEY}")
                # System prompt goes in systemInstruction (the real field),
                # not smuggled in as a fake user turn (audit M8).
                sys_instruction = {"parts": [{"text": system}]}
                contents = []
                for turn in history:
                    role = "user" if turn["role"] == "user" else "model"
                    contents.append({"role": role, "parts": [{"text": turn["content"]}]})
                contents.append({"role": "user", "parts": [{"text": user_text}]})

                total_in, total_out = 0, 0
                final_text = ""

                for round_i in range(TOOL_ROUND_CAP):
                    payload = {"contents": contents,
                               "systemInstruction": sys_instruction,
                               "tools": tool_calling.gemini_tools(user_tier)}
                    async with session.post(
                        url, json=payload,
                        timeout=aiohttp.ClientTimeout(total=60),
                    ) as resp:
                        resp.raise_for_status()
                        data = await resp.json()

                    usage = data.get("usageMetadata", {})
                    total_in  += usage.get("promptTokenCount", 0)
                    total_out += usage.get("candidatesTokenCount", 0)

                    candidate = data["candidates"][0]
                    parts     = candidate.get("content", {}).get("parts", [])

                    text_parts = [p["text"] for p in parts if "text" in p]
                    if text_parts:
                        final_text = "\n".join(text_parts)

                    fn_calls = [p["functionCall"] for p in parts if "functionCall" in p]
                    print(f"   ↳ round {round_i+1}: {len(text_parts)} text part(s) "
                          f"({sum(len(t) for t in text_parts)} chars), "
                          f"tools: {[fc.get('name') for fc in fn_calls] or 'none'}")
                    call_logger.log_model_call(
                        model=model_name, thinking=False,
                        tokens_in=usage.get("promptTokenCount", 0),
                        tokens_out=usage.get("candidatesTokenCount", 0),
                        stop_reason=candidate.get("finishReason", "?"),
                        tools=[fc.get("name") for fc in fn_calls],
                        round_num=round_i + 1,
                    )
                    if not fn_calls:
                        break  # done — no tool calls this round

                    # Echo the model's turn back, then provide function
                    # responses — executed concurrently (parallel tool calls
                    # were running sequentially before, pure added latency).
                    contents.append({"role": "model", "parts": parts})

                    async def _run_fn(fc):
                        result_text = await tool_calling.execute_tool(
                            fc.get("name", ""), fc.get("args", {}) or {},
                            sender=sender, name=name,
                            active_voices=active_voices, retrieve_rag_fn=retrieve_rag,
                            session=session, cat_whitelist=CAT_WHITELIST_DIRS,
                            tier=USERS.get(sender, {}).get("tier", "user"),
                            set_effort_fn=set_effort_sticky,
                        )
                        return {"functionResponse": {
                            "name": fc.get("name", ""),
                            "response": {"result": result_text},
                        }}
                    response_parts = list(await asyncio.gather(
                        *[_run_fn(fc) for fc in fn_calls]))
                    contents.append({"role": "user", "parts": response_parts})

                if fn_calls:
                    # Round cap exhausted mid tool-chain — force one final
                    # text answer instead of swallowing the last tool result
                    # and returning "...". Nudge goes into the existing
                    # functionResponse turn (roles must alternate).
                    contents[-1]["parts"].append({"text": (
                        """Tool budget exhausted. Do not call any more tools — answer the user now in plain text using the information you already have. If the tools returned nothing useful, say so plainly instead of guessing."""
                    )})
                    payload = {
                        "contents": contents,
                        "systemInstruction": sys_instruction,
                        "tools": tool_calling.gemini_tools(user_tier),
                        "toolConfig": {"functionCallingConfig": {"mode": "NONE"}},
                    }
                    async with session.post(
                        url, json=payload,
                        timeout=aiohttp.ClientTimeout(total=60),
                    ) as resp:
                        resp.raise_for_status()
                        data = await resp.json()
                    usage = data.get("usageMetadata", {})
                    total_in  += usage.get("promptTokenCount", 0)
                    total_out += usage.get("candidatesTokenCount", 0)
                    parts = data["candidates"][0].get("content", {}).get("parts", [])
                    text_parts = [p["text"] for p in parts if "text" in p]
                    if text_parts:
                        final_text = "\n".join(text_parts)
                    print(f"   ↳ forced final: {len(text_parts)} text part(s) "
                          f"({sum(len(t) for t in text_parts)} chars)")
                    call_logger.log_model_call(
                        model=model_name, thinking=False,
                        tokens_in=usage.get("promptTokenCount", 0),
                        tokens_out=usage.get("candidatesTokenCount", 0),
                        stop_reason="forced_final", tools=[], round_num=99,
                    )

                # usage is logged per-round inside call_logger.log_model_call()
                return final_text or EMPTY_REPLY, tag

            elif "claude" in model_name:
                effort = get_effort(sender)
                print(f"☁️  {tag} ({model_name}, effort={effort})")
                messages = [{"role": t["role"], "content": t["content"]} for t in history]
                messages.append({"role": "user", "content": user_text})

                total_in, total_out = 0, 0
                final_text = ""

                for round_i in range(TOOL_ROUND_CAP):
                    # Usage + trace logging happens inside skills.llm now —
                    # do not re-log here or spend double-counts (H5 funnel).
                    resp = await asyncio.to_thread(
                        _claude_sync_raw, messages, system_blocks, model_name,
                        user_tools, effort,
                    )
                    total_in  += resp.usage.input_tokens
                    total_out += resp.usage.output_tokens

                    text_blocks = [b.text for b in resp.content if b.type == "text"]
                    if text_blocks:
                        final_text = "\n".join(text_blocks)

                    tool_blocks = [b for b in resp.content if b.type == "tool_use"]
                    _advised = any(b.type == "advisor_tool_result" for b in resp.content)
                    _tt = getattr(getattr(resp.usage, "output_tokens_details", None),
                                  "thinking_tokens", None)
                    print(f"   ↳ round {round_i+1}: {len(text_blocks)} text block(s) "
                          f"({sum(len(t) for t in text_blocks)} chars), "
                          f"thinking_tokens={_tt}, "
                          f"stop_reason={resp.stop_reason}, "
                          f"{'advisor consulted, ' if _advised else ''}"
                          f"tools: {[b.name for b in tool_blocks] or 'none'}")

                    if resp.stop_reason != "tool_use":
                        break  # done — no more tool calls

                    messages.append({"role": "assistant", "content": resp.content})

                    # Execute this round's tool calls CONCURRENTLY — Claude
                    # batches parallel tool_use blocks in one response, and
                    # running them sequentially was pure added latency.
                    async def _run_tool(block):
                        result_text = await tool_calling.execute_tool(
                            block.name, block.input,
                            sender=sender, name=name,
                            active_voices=active_voices, retrieve_rag_fn=retrieve_rag,
                            session=session, cat_whitelist=CAT_WHITELIST_DIRS,
                            tier=USERS.get(sender, {}).get("tier", "user"),
                            set_effort_fn=set_effort_sticky,
                        )
                        return {"type": "tool_result", "tool_use_id": block.id,
                                "content": result_text}
                    tool_results = list(await asyncio.gather(
                        *[_run_tool(b) for b in tool_blocks]))
                    messages.append({"role": "user", "content": tool_results})

                if resp.stop_reason == "tool_use":
                    # Round cap exhausted mid tool-chain — force one final
                    # text answer. Tools must remain defined (the API rejects
                    # transcripts containing tool_use blocks otherwise); the
                    # nudge appended to the final tool_result turn does the
                    # forcing.
                    messages[-1]["content"].append({"type": "text", "text": (
                        """Tool budget exhausted. Do not call any more tools — answer the user now in plain text using the information you already have. If the tools returned nothing useful, say so plainly instead of guessing."""
                    )})
                    # usage logged inside skills.llm — no re-log here
                    resp = await asyncio.to_thread(
                        _claude_sync_raw, messages, system_blocks, model_name,
                        user_tools, effort,
                    )
                    total_in  += resp.usage.input_tokens
                    total_out += resp.usage.output_tokens
                    text_blocks = [b.text for b in resp.content if b.type == "text"]
                    if text_blocks:
                        final_text = "\n".join(text_blocks)
                    _tt = getattr(getattr(resp.usage, "output_tokens_details", None),
                                  "thinking_tokens", None)
                    print(f"   ↳ forced final: {len(text_blocks)} text block(s) "
                          f"({sum(len(t) for t in text_blocks)} chars), "
                          f"thinking_tokens={_tt}, "
                          f"stop_reason={resp.stop_reason}")

                return final_text or EMPTY_REPLY, tag

        except Exception as e:
            print(f"⚠️ Cloud error attempt {attempt+1}: {e}")
            if attempt < 2:
                await asyncio.sleep(10)
            else:
                return fail_line("The model call", e), tag

    return "Cloud API unavailable.", tag


# Largest single Signal message Chip sends before splitting. 2,800 is the
# longest message confirmed to arrive whole on the admin handset (a healer
# result pasted back intact, 19 Nov 2026). Raise only after confirming a
# longer one on a phone: past the client limit a failure is silent.
SIGNAL_MAX_CHARS   = 2800
SIGNAL_ATTACH_OVER = 2 * SIGNAL_MAX_CHARS    # longer than two messages -> file
SIGNAL_MIN_TAIL    = 200                     # never send a runt final chunk

_send_locks = {}
_rpc_seq    = 0


def _next_rpc_id():
    """A distinct JSON-RPC id per request, so a reply can be matched to the
    request that earned it (see _read_rpc_reply)."""
    global _rpc_seq
    _rpc_seq += 1
    return _rpc_seq


async def _read_rpc_reply(reader, want_id, timeout):
    """Read until signal-cli's answer to OUR request, discarding anything else.

    The daemon runs with the default receive mode, which broadcasts `receive`
    notifications to EVERY connected client — including the short-lived
    connection a send opens. So the first line back on this socket is often
    an unrelated inbound message rather than the send's response.

    Taking that first line as the reply is the send-buffer bug: a send was
    reported delivered on the strength of someone else's inbound message,
    and the rejection that arrived on the next line was never read. That
    defeats the whole point of confirming each chunk before sending the next.

    Returns the parsed response, or None when the line was unparseable
    (unchanged from before: present but unreadable is treated as sent).
    """
    loop     = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while True:
        remaining = deadline - loop.time()
        if remaining <= 0:
            raise asyncio.TimeoutError()
        raw = await asyncio.wait_for(reader.readline(), timeout=remaining)
        if not raw:
            raise RuntimeError("signal-cli closed the connection without replying")
        try:
            msg = json.loads(raw.decode())
        except Exception:
            return None
        if not isinstance(msg, dict):
            return None
        if msg.get("method"):                    # a notification, not an answer
            continue
        if "id" in msg and msg["id"] != want_id:
            continue                             # an answer, but not to us
        return msg


def _recipient_lock(target):
    """One asyncio.Lock per recipient. Three workers plus the reminder
    checker share the queue, so without this two turns to the same person
    could interleave their messages."""
    lock = _send_locks.get(target)
    if lock is None:
        lock = _send_locks[target] = asyncio.Lock()
    return lock


def _protected_spans(text):
    """(start, end) of every fenced code block — regions a cut must not land
    inside. A code block arriving in two pieces is unreadable, and the second
    piece reads as a new message rather than a continuation."""
    return [m.span() for m in CODE_FENCE_RE.finditer(text)]


def _respect_code_blocks(rest, cut, lo, budget):
    """Move `cut` off the inside of a code block. Prefer closing before the
    block starts; failing that, after it ends. A block too big to keep whole
    is cut as-is — the caller has already decided this is a message, not a
    file attachment."""
    for start, end in _protected_spans(rest):
        if not (start < cut < end):
            continue
        if start >= lo:                           # close the chunk before the fence
            return start
        if end <= budget:                         # or carry the whole block
            return end
        return cut                                # block alone exceeds a message
    return cut


def _rebalance_fences(chunks):
    """Make every chunk independently well-formed with respect to code fences.

    A fenced block longer than one Signal message has to be cut somewhere.
    Cutting it plainly left chunk N ending on an unterminated ``` and chunk
    N+1 opening with bare code, which on a handset reads as two broken
    messages rather than one listing continued. Close the fence at the end of
    the chunk that opened it and reopen it at the start of the next, so a
    split block still arrives as code on both sides of the break.

    The reopened fence carries no language hint: Signal renders no markdown
    anyway, and guessing one would be a second thing to get wrong.
    """
    out, carry = [], ""
    for chunk in chunks:
        body = carry + chunk
        if body.count("```") % 2:                # still open where this chunk ends
            out.append(body + "\n```")
            carry = "```\n"
        else:
            out.append(body)
            carry = ""
    return out


def split_for_signal(text, max_chars=None, min_tail=None):
    """Split `text` into the fewest balanced chunks that each fit one Signal
    message, breaking at a paragraph, then a line, then a sentence, then a
    space - never mid-word, and never through a fenced code block. The caller
    numbers chunks from len(result), so the "(i/n)" marker can never disagree
    with what was sent."""
    max_chars = max_chars or SIGNAL_MAX_CHARS
    min_tail  = SIGNAL_MIN_TAIL if min_tail is None else min_tail
    text = text.strip()
    if len(text) <= max_chars:
        return [text]
    budget = max_chars - 10                      # room for the " (i/n)" marker
    if "```" in text:
        budget -= 8                              # room for _rebalance_fences to close and reopen
    n      = -(-len(text) // budget)
    chunks, rest = [], text
    while len(rest) > budget:
        parts_left = max(n - len(chunks), 2)
        target = min(budget, max(-(-len(rest) // parts_left), min_tail))
        lo     = max(int(target * 0.6), 1)
        cut    = -1
        for sep in ("\n\n", "\n", ". ", "? ", "! ", " "):
            # nearest break to the balanced target, searching back then forward
            back = rest.rfind(sep, lo, target + len(sep))
            fwd  = rest.find(sep, target, budget)
            cand = [c for c in (back, fwd) if c != -1]
            if cand:
                cut = min(cand, key=lambda c: abs(c - target))
                cut += len(sep) if sep.strip() else 0   # keep punctuation with its sentence
                break
        if cut <= 0:
            cut = budget                          # one unbroken token longer than a message
        cut = _respect_code_blocks(rest, cut, lo, budget)
        if cut <= 0 or cut > len(rest):
            cut = min(budget, len(rest))          # never stall or overrun
        chunks.append(rest[:cut].rstrip())
        rest = rest[cut:].lstrip()
    if rest:
        if chunks and len(rest) < min_tail and len(chunks[-1]) + len(rest) + 2 <= budget:
            chunks[-1] = chunks[-1] + "\n\n" + rest      # fold a runt into its neighbour
        else:
            chunks.append(rest)
    return _rebalance_fences(chunks)


async def send_signal(session, text, recipient=None, kind=None):
    """Send one message, serialized per recipient (see _recipient_lock).
    Returns True if signal-cli confirmed it.

    Every message passes the send gate first (skills/send_gate.py, One Voice
    spec A.1): `kind` names what it is (reply, file, ask, scheduled, relay,
    alert). Left as None, a message inside an open turn is that turn's reply."""
    target = recipient or SENDER
    from skills import send_gate
    ok, _k, _why = send_gate.decide(target, text, kind=kind)
    if not ok:
        return False
    async with _recipient_lock(target):
        return await _send_signal_raw(session, text, target)


async def working():
    """'Still working' without a message: ⏳ on the inbound message of the
    turn this context belongs to. Replaces the old progress pings ("Processing
    PDF...", "Rechunking..."); the worker swaps it for ✅ at the end."""
    try:
        from skills import send_gate
        t = send_gate.raw()
        if t is not None and t.ts and t.ts != "0" and not t.reacted:
            t.reacted = True
            await send_reaction(t.sender, t.ts, "⏳")
    except Exception as e:
        print(f"⚠️ working(): {e}")


async def _send_signal_raw(session, text, target):
    """The actual send. Callers must hold the recipient's lock.

    Returns True only when signal-cli answered THIS request without an error.
    The reply line used to be read and thrown away, so a send signal-cli had
    rejected was still logged and printed as 'delivered'. Reading it was not
    enough on its own: the daemon broadcasts inbound-message notifications
    down this socket too, so the first line back is frequently not the answer
    to our send. The request now carries its own id and the reply is matched
    to it — see _read_rpc_reply.
    """
    # If this user's last inbound came over Discord, reply there instead
    # (connector scaffold, 19 Nov 2026 — no-ops until the relay is set up).
    try:
        from skills.connectors import discord_relay as _dr
        if _dr.PREFER.get(target) == "discord" and await _dr.deliver(target, text):
            call_logger.log_signal_out(target[-4:], status="discord")
            print(f"🎮 → {target[-4:]}: {text[:80]}")
            return True
    except Exception:
        pass
    try:
        req_id = _next_rpc_id()
        req = json.dumps({
            "jsonrpc": "2.0", "method": "send", "id": req_id,
            "params": {"recipient": [target], "message": text}
        }) + "\n"
        reader, writer = await asyncio.open_unix_connection(SIGNAL_SOCKET)
        writer.write(req.encode())
        await writer.drain()                     # the whole request, however long
        reply = await _read_rpc_reply(reader, req_id, timeout=15)
        writer.close()
        try: await writer.wait_closed()
        except Exception: pass

        err = (reply or {}).get("error")
        if err:
            detail = err.get("message", err) if isinstance(err, dict) else err
            raise RuntimeError(f"signal-cli rejected the send: {detail}")

        call_logger.log_signal_out(target[-4:], status="delivered")
        print(f"📤 → {target[-4:]}: {text[:80]}")
        return True
    except Exception as e:
        call_logger.log_signal_out(target[-4:], status=f"error:{e}")
        print(f"❌ Send error: {e}")
        return False

async def _signal_rpc(method, params):
    """Fire-and-forget JSON-RPC to signal-cli. Never raises — these are UX
    garnish (typing bubbles, reactions), not delivery."""
    try:
        req = json.dumps({"jsonrpc": "2.0", "method": method, "id": 1,
                          "params": params}) + "\n"
        reader, writer = await asyncio.open_unix_connection(SIGNAL_SOCKET)
        writer.write(req.encode())
        await writer.drain()
        await asyncio.wait_for(reader.readline(), timeout=10)
        writer.close()
        try: await writer.wait_closed()
        except Exception: pass
    except Exception as e:
        print(f"⚠️ {method} failed: {e}")


async def send_typing(recipient, stop=False):
    """'Chip is typing…' bubble — the single cheapest perceived-latency fix
    on a text-message interface (19 Nov 2026)."""
    params = {"recipient": [recipient]}
    if stop:
        params["stop"] = True
    await _signal_rpc("sendTyping", params)


async def send_reaction(recipient, target_ts, emoji, remove=False):
    """React to the user's own message (⏳ pipeline started, ✅ done, ❌ error).
    A later reaction from us replaces the earlier one, so ⏳→✅ just works."""
    try:
        ts_int = int(str(target_ts).split("_")[0])
    except Exception:
        return
    await _signal_rpc("sendReaction", {
        "recipient": [recipient], "reaction": emoji,
        "targetAuthor": recipient, "targetTimestamp": ts_int,
        "remove": remove,
    })


async def send_signal_file(session, filepath, caption, recipient=None, kind=None,
                           _gated=False):
    """Send one file as an attachment, and find out whether it arrived.

    Until 20 Nov 2026 this path wrote its request, read one line, and threw it
    away — so every file and document send was unconfirmed while text sends
    were confirmed. Reading that line naively would not have been enough: the
    daemon's default receive mode broadcasts inbound notifications to every
    connected client, including the socket this send opens, so the first line
    back is frequently someone else's mail. That is the same send-buffer bug
    fixed for text in 78d95a5, and the same cure applies — the request carries
    its own id and _read_rpc_reply skips to the answer that matches it.

    Returns True when signal-cli answered THIS request without an error,
    False when it rejected it or the connection failed, and None when no
    answer arrived in time: unknown delivery, never success.

    Locking is deliberately left to the caller, exactly as before
    (deliver_response holds the recipient's lock across a report delivery);
    taking the same non-reentrant lock here would deadlock that path.
    """
    target   = recipient or SENDER
    filename = os.path.basename(filepath)
    if not _gated:
        # Through the send gate like every other message. An attachment is
        # `file` unless the caller says otherwise (deliver_response's
        # long-reply attachment is the reply itself and gates on its own).
        from skills import send_gate
        ok, _k, _why = send_gate.decide(target, f"[file] {filename} {caption or ''}",
                                        kind=kind or "file")
        if not ok:
            return False
    try:
        req_id = _next_rpc_id()
        req = json.dumps({
            "jsonrpc": "2.0", "method": "send", "id": req_id,
            "params": {
                "recipient":   [target],
                "message":     caption,
                "attachments": [filepath],
            }
        }) + "\n"
        reader, writer = await asyncio.open_unix_connection(SIGNAL_SOCKET)
        writer.write(req.encode())
        await writer.drain()
        try:
            reply = await _read_rpc_reply(reader, req_id, timeout=30)
        finally:
            writer.close()
            try: await writer.wait_closed()
            except Exception: pass

        err = (reply or {}).get("error")
        if err:
            detail = err.get("message", err) if isinstance(err, dict) else err
            raise RuntimeError(f"signal-cli rejected the attachment: {detail}")

        call_logger.log_signal_out(target[-4:], status="delivered")
        print(f"📎 {filename} → {target[-4:]}")
        return True
    except asyncio.TimeoutError:
        # No answer inside the window. The send may or may not have gone out,
        # so say so rather than claiming either outcome.
        call_logger.log_signal_out(target[-4:], status="unknown:no reply")
        print(f"⚠️ {filename} → {target[-4:]}: no reply from signal-cli, delivery unknown")
        return None
    except Exception as e:
        call_logger.log_signal_out(target[-4:], status=f"error:{e}")
        print(f"❌ File send error: {e}")
        return False

async def deliver_response(session, text, sender, tag=None,
                            force_file=False, cc_sender=None, kind=None):
    """Deliver one reply as ONE decision: a single message when it fits, the
    fewest balanced chunks when it does not, a file when it is longer than
    two messages. The recipient's lock is held across the whole delivery so
    another turn's messages cannot land in the middle of this one.

    One gate decision covers the whole reply, chunks included. Short notes
    the turn collected (a staging id, 'effort back to high') are folded onto
    the end of it rather than sent as messages of their own."""
    from skills import send_gate
    text       = strip_markdown(text)
    _notes     = send_gate.take_notes()
    if _notes:
        text = text.rstrip() + "\n\n" + "\n".join(_notes)
    tag_prefix = f"{tag} " if tag else ""
    full_text  = f"{tag_prefix}{text}"
    cc         = cc_sender if (cc_sender and cc_sender != sender) else None

    if force_file or len(full_text) > SIGNAL_ATTACH_OVER:
        os.makedirs(REPORT_DIR, exist_ok=True)
        ts       = datetime.now().strftime("%Y%m%d_%H%M%S")
        name     = USERS.get(sender, {}).get("name", "user").lower()
        filepath = f"{REPORT_DIR}/{ts}_{name}.txt"
        with open(filepath, "w") as f:
            f.write(full_text)
        ok, _k, _why = send_gate.decide(sender, full_text, kind=kind)
        if not ok:
            return
        async with _recipient_lock(sender):
            await send_signal_file(session, filepath, f"{tag_prefix}Report attached",
                                   sender, _gated=True)
        if cc:
            ok, _k, _why = send_gate.decide(cc, full_text)
            if ok:
                await send_signal_file(session, filepath, "Copy of what was sent:", cc,
                                       _gated=True)
        return

    chunks = split_for_signal(full_text)
    total  = len(chunks)
    sent   = 0
    ok, _k, _why = send_gate.decide(sender, full_text, kind=kind, parts=total)
    if not ok:
        return
    if total > 1:
        # Logged so the real frequency of chunking is measurable rather than
        # anecdotal: how long the reply was, and how many messages it became.
        print(f"✂️  {len(full_text)} chars → {total} chunks for {sender[-4:]}")
        call_logger.log_signal_out(sender[-4:],
                                   status=f"chunked:{total}:{len(full_text)}chars")
    async with _recipient_lock(sender):
        for i, chunk in enumerate(chunks, 1):
            marker = f" ({i}/{total})" if total > 1 else ""
            # Confirmation of each before the next: stop on the first failure
            # instead of firing the rest into a broken socket, so the gap is
            # one reported break rather than a reply with a hole in it.
            if not await _send_signal_raw(session, chunk + marker, sender):
                print(f"❌ chunk {i}/{total} failed for {sender[-4:]} — "
                      f"aborting the remaining {total - i}")
                break
            sent = i
    if sent < total:
        os.makedirs(REPORT_DIR, exist_ok=True)
        ts       = datetime.now().strftime("%Y%m%d_%H%M%S")
        name     = USERS.get(sender, {}).get("name", "user").lower()
        filepath = f"{REPORT_DIR}/{ts}_{name}_undelivered.txt"
        with open(filepath, "w") as f:
            f.write(full_text)
        print(f"💾 undelivered remainder saved: {filepath}")
        async with _recipient_lock(sender):
            await _send_signal_raw(
                session,
                f"[delivery failed after part {sent} of {total} — "
                f"the full reply is saved as {os.path.basename(filepath)}]",
                sender)
    if cc:
        # The copy is chunked too. It used to be sliced to one message, which
        # silently dropped the tail of anything longer. One gate decision for
        # the whole copy, like the reply itself.
        _parts = split_for_signal(f"Sent:\n{full_text}")
        ok, _k, _why = send_gate.decide(cc, full_text, parts=len(_parts))
        if ok:
            async with _recipient_lock(cc):
                for part in _parts:
                    if not await _send_signal_raw(session, part, cc):
                        break

# ─── Command parser ───────────────────────────────────────────────────────────

def parse_command(segment):
    """
    Parse a single pipeline segment.
    Returns {"cmd": str|None, "flags": set, "payload": str}

    New: "cmd [flags]: payload"   e.g.  "g -c: tell me about X"
    Legacy: "!cmd payload"         e.g.  "!gemini tell me about X"
    Plain: no recognized command   →     cmd=None
    """
    segment = segment.strip()

    # New colon syntax
    colon_m = re.match(r'^([a-z][a-z0-9\-]*)((?:\s+-\w+)*)\s*:(.*)', segment,
                       re.IGNORECASE | re.DOTALL)
    if colon_m:
        raw_cmd = colon_m.group(1).lower()
        raw_flags = colon_m.group(2).strip()
        payload   = colon_m.group(3).strip()
        flags     = set(raw_flags.split()) if raw_flags else set()
        if raw_cmd in COMMAND_MAP:
            return {"cmd": raw_cmd, "flags": flags, "payload": payload.rstrip(":")}

    # Legacy !command syntax (longest match first)
    if segment.startswith("!"):
        inner = segment[1:].strip()
        for legacy, new_cmd in LEGACY_MAP:
            pattern = re.compile(r'^' + re.escape(legacy) + r'(\s+|$)', re.IGNORECASE)
            m = pattern.match(inner)
            if m:
                rest  = inner[m.end():].strip()
                flags = set()
                words = rest.split()
                pwords = []
                for w in words:
                    if w.startswith("-"):
                        flags.add(w)
                    else:
                        pwords.append(w)
                return {"cmd": new_cmd, "flags": flags, "payload": " ".join(pwords)}

    # Bare shell shorthand: "ls", "ls <domain>", "ls <domain>:", "ps", "cat facts", "vault <term>"
    # Handles both "ls <domain>" and "ls <domain>:" (trailing colon stripped)
    bare_m = re.match(r'^(ls|ps|cat|facts|vault)(?:\s+(.*))?$', segment, re.IGNORECASE)
    if bare_m:
        cmd     = bare_m.group(1).lower()
        content = (bare_m.group(2) or "").strip().rstrip(":")  # strip trailing colon
        return {"cmd": cmd, "flags": set(), "payload": content}

    # Plain text — semantic router
    return {"cmd": None, "flags": set(), "payload": segment}

def _looks_like_command(segment):
    """True if segment starts with a recognized command prefix (case-insensitive)."""
    s = segment.strip().lower()
    return any(s.startswith(p) for p in COMMAND_PREFIXES)

def parse_pipeline(text):
    """
    Split on | and >> operators, parse each segment.
    Returns {"stages": [...], "deliver_to": str|None}

    SAFETY: | and >> are only treated as pipeline operators when the message
    actually looks like a deliberate multi-stage command (starts with a
    recognized prefix). Plain conversational text containing these characters
    incidentally (tables, code, prose) is never split or truncated — this
    prevents a long pasted message with a stray "|" from being silently
    fragmented into N unrelated LLM calls, or a stray ">>" from silently
    discarding the rest of the message.
    """
    deliver_to = None
    is_command = _looks_like_command(text)

    if is_command and ">>" in text:
        main, _, dest = text.partition(">>")
        dest_stripped = dest.strip()
        dest_m = re.match(r'msg[:\s]\s*(\w+)', dest_stripped, re.IGNORECASE)
        if dest_m:
            text       = main.strip()
            deliver_to = dest_m.group(1)
        elif re.match(r'facts[:\s]?', dest_stripped, re.IGNORECASE):
            text       = main.strip()
            deliver_to = "__facts__"
        # else: ">>" wasn't a real deliver-to operator — leave text untouched

    if is_command and "|" in text:
        stages = [parse_command(seg) for seg in text.split("|") if seg.strip()]
        if not stages:
            stages = [parse_command(text)]
    else:
        # Not a recognized command, or no pipe present — never split.
        # Protects plain-text messages (tables, prose, code) that happen
        # to contain literal | or >> characters.
        stages = [parse_command(text)]

    return {"stages": stages, "deliver_to": deliver_to}

# ─── Fact helpers ─────────────────────────────────────────────────────────────

def _backup_context_db():
    try:
        import shutil
        bdir = f"{WORKSPACE}/data/patches/backups"
        os.makedirs(bdir, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        shutil.copy2(CONTEXT_DB, f"{bdir}/chip_context_{ts}.db")
    except Exception as e:
        print(f"⚠️ Backup warning: {e}")

# ─── Shell handler ────────────────────────────────────────────────────────────

async def handle_shell(session, sender, cmd, payload=""):
    tag = TAGS["chip"]

    if cmd == "ps":
        cpu  = psutil.cpu_percent(interval=1)
        temp = os.popen("vcgencmd measure_temp 2>/dev/null").readline().replace("temp=","").strip() or "N/A"
        ram  = psutil.virtual_memory()
        disk = psutil.disk_usage(WORKSPACE)
        answer = (
            f"Temp: {temp}  CPU: {cpu}%\n"
            f"RAM:  {ram.used//1024**2}MB / {ram.total//1024**2}MB\n"
            f"Disk: {disk.used//1024**3}GB / {disk.total//1024**3}GB\n"
            f"Skills: {', '.join(SKILL_REGISTRY.keys())}"
        )
        await send_signal(session, f"{tag} {answer}", recipient=sender)
        return answer

    if cmd == "ls":
        conn   = sqlite3.connect(VAULT_DB)
        domain = payload.strip() if payload else None

        if domain == "recent":
            rows = conn.execute(
                "SELECT filename, domain, chunk_count, ingested_at "
                "FROM documents ORDER BY ingested_at DESC LIMIT 10"
            ).fetchall()
            conn.close()
            lines = ["Recent:"] if rows else ["Vault is empty."]
            for fname, dom, chunks, ts in (rows or []):
                lines.append(f"  {fname}  [{dom}]  {chunks}ch  {str(ts)[:10]}")
        elif domain:
            rows = conn.execute(
                "SELECT filename, chunk_count, ingested_at FROM documents "
                "WHERE domain=? ORDER BY ingested_at DESC LIMIT 20", (domain,)
            ).fetchall()
            conn.close()
            lines = [f"{domain}:"] if rows else [f"No docs in '{domain}'."]
            for fname, chunks, ts in (rows or []):
                lines.append(f"  {fname}  {chunks}ch  {str(ts)[:10]}")
        else:
            rows = conn.execute(
                "SELECT domain, COUNT(*) docs, SUM(chunk_count) chunks "
                "FROM documents GROUP BY domain ORDER BY docs DESC"
            ).fetchall()
            conn.close()
            lines = ["Vault:"] if rows else ["Vault is empty."]
            for dom, docs, chunks in (rows or []):
                lines.append(f"  {dom:14s}  {docs} docs  {chunks or 0} chunks")

        answer = "\n".join(lines)
        await send_signal(session, f"{tag} {answer}", recipient=sender)
        return answer

    if cmd == "cat":
        target = payload.strip()

        if not target or target == "facts":
            conn = sqlite3.connect(CONTEXT_DB)
            user = USERS.get(sender, {}).get("name", "").lower()
            total = conn.execute(
                "SELECT COUNT(*) FROM facts WHERE confirmed=1 AND (user=? OR user IS NULL)",
                (user,)
            ).fetchone()[0]
            rows = conn.execute(
                "SELECT content, created_at FROM facts WHERE confirmed=1 "
                "AND (user=? OR user IS NULL) "
                "ORDER BY created_at DESC LIMIT 100",
                (user,)
            ).fetchall()
            conn.close()
            if not rows:
                answer = "No confirmed facts."
            else:
                lines = [f"Facts ({total} total, showing {len(rows)}):"]
                for content, ts in rows:
                    lines.append(f"• {content}  [{str(ts)[:10]}]")
                answer = "\n".join(lines)
            await send_signal(session, f"{tag} {answer}", recipient=sender)
            return answer

        if target == "reminders":
            rpath = f"{WORKSPACE}/data/reminders.json"
            if not os.path.exists(rpath):
                await send_signal(session, f"{tag} No reminders file.", recipient=sender)
                return ""
            data    = json.load(open(rpath))
            pending = [r for r in data if not r.get("fired")]
            if not pending:
                answer = "No pending reminders."
            else:
                lines = [f"Pending ({len(pending)}):"]
                for r in pending:
                    lines.append(f"  {r['fire_at'][:16]}  {r['recipient_name']}  {r['message'][:60]}")
                answer = "\n".join(lines)
            await send_signal(session, f"{tag} {answer}", recipient=sender)
            return answer

        resolved = _resolve_whitelisted(target)
        if not resolved:
            await send_signal(session,
                f"{tag} Not in whitelist. Try: cat: facts  cat: reminders  cat: skills/<name>.py",
                recipient=sender)
            return ""
        try:
            content = open(resolved).read()
            if len(content) > 1200:
                await send_signal_file(session, resolved,
                    f"{tag} {os.path.basename(resolved)}", sender)
            else:
                await send_signal(session, f"{tag}\n{content}", recipient=sender)
            return content
        except Exception as e:
            await send_signal(session, f"{tag} " + fail_line("Reading that file", e), recipient=sender)
            return ""

    if cmd == "facts":
        if ">>" in payload:
            fact_text = payload.split(">>", 1)[1].strip().strip('"').strip("'")
            if not fact_text:
                await send_signal(session, f"{tag} No fact text.", recipient=sender)
                return ""
            _backup_context_db()
            conn = sqlite3.connect(CONTEXT_DB)
            conn.execute(
                "INSERT INTO facts (content, confirmed, created_at, user) "
                "VALUES (?, 1, ?, ?)",
                (fact_text, datetime.now().isoformat(),
                 USERS.get(sender, {}).get("name", chip_config.ADMIN_NAME).lower())
            )
            conn.commit()
            conn.close()
            answer = f"Saved: {fact_text}"
            await send_signal(session, f"{tag} {answer}", recipient=sender)
            return answer

        if "grep" in payload:
            term = payload.split("grep", 1)[-1].strip().strip('"').strip("'").lower()
            conn = sqlite3.connect(CONTEXT_DB)
            rows = conn.execute(
                "SELECT content, created_at FROM facts WHERE confirmed=1 AND user=?",
                (USERS.get(sender, {}).get("name", "").lower(),)
            ).fetchall()
            conn.close()
            matches = [(c, ts) for c, ts in rows if term in c.lower()]
            if not matches:
                answer = f"No facts matching '{term}'."
            else:
                lines = [f"Matching '{term}':"]
                for content, ts in matches:
                    lines.append(f"• {content}  [{str(ts)[:10]}]")
                answer = "\n".join(lines)
            await send_signal(session, f"{tag} {answer}", recipient=sender)
            return answer

        # Bare facts: → show all
        return await handle_shell(session, sender, "cat", "facts")

    await send_signal(session, f"{tag} Unknown shell command.", recipient=sender)
    return ""

# ─── Vault search ─────────────────────────────────────────────────────────────

async def handle_vault(session, sender, query, domain=None):
    """
    Explicit vault: query.
    - Single source doc (best semantic match)
    - Contiguous chunks around best-scoring chunk
    - 0.5 semantic threshold — returns nothing if below
    - Gemini Flash summary pass on returned chunks
    - Differentiates "nothing found" vs "ambiguous/low confidence"
    """
    if not query.strip():
        return "Usage: vault: <search terms>", TAGS["chip"]

    # FTS5 first pass — get candidates
    conn  = sqlite3.connect(VAULT_DB)
    clean = re.sub(r'[^\w\s]', ' ', query)
    tokens = [t for t in clean.split() if len(t) > 2]
    if not tokens:
        conn.close()
        return f"Search term too short.", TAGS["chip"]

    fts_q = " OR ".join(tokens[:10])
    # Exclude books and private from explicit vault: searches
    candidates = conn.execute(
        "SELECT c.id, c.chunk_text, c.embedding, c.chunk_index, c.document_id, d.filename "
        "FROM chunks c JOIN chunks_fts f ON c.id = f.rowid "
        "JOIN documents d ON c.document_id = d.id "
        "WHERE chunks_fts MATCH ? "
        "AND c.domain NOT IN ('books', 'private') "
        "AND (c.sensitive IS NULL OR c.sensitive = 0) "
        "ORDER BY rank LIMIT 30",
        (fts_q,)
    ).fetchall()

    if not candidates:
        conn.close()
        return f"Nothing found for '{query}'.", TAGS["chip"]

    # Nomic semantic re-rank
    best_score    = 0.0
    best_doc_id   = None
    best_filename = None
    try:
        emb_resp  = requests.post(
            f"{OLLAMA_URL}/api/embeddings",
            json={"model": "nomic-embed-text", "prompt": query[:2000]},
            timeout=30,
        )
        query_emb = emb_resp.json().get("embedding", [])

        if query_emb:
            import json as _json, math
            def cosine(a, b):
                dot = sum(x*y for x,y in zip(a,b))
                na  = math.sqrt(sum(x*x for x in a))
                nb  = math.sqrt(sum(x*x for x in b))
                return dot / (na * nb + 1e-9)

            scored = []
            for cid, chunk_text, emb_json, chunk_idx, doc_id, fname in candidates:
                try:
                    sim = cosine(query_emb, _json.loads(emb_json)) if emb_json else 0.0
                except Exception:
                    sim = 0.0
                scored.append((sim, cid, chunk_text, chunk_idx, doc_id, fname))
            scored.sort(reverse=True)

            best_score    = scored[0][0] if scored else 0.0
            best_doc_id   = scored[0][4] if scored else None
            best_filename = scored[0][5] if scored else None
            best_chunk_idx = scored[0][3] if scored else 0

    except Exception as e:
        print(f"⚠️ Vault nomic error: {e}")
        # Fall back to FTS5 top result
        best_doc_id    = candidates[0][4]
        best_filename  = candidates[0][5]
        best_chunk_idx = candidates[0][3]
        best_score     = 0.6  # Assume ok on fallback

    # Threshold check
    SEMANTIC_THRESHOLD = 0.5
    if best_score < SEMANTIC_THRESHOLD and best_score > 0:
        conn.close()
        return (
            f"Low confidence results for '{query}' "
            f"(best match: {best_filename or 'unknown'}, score: {best_score:.2f}). "
            f"Try a more specific query or fetch: to retrieve a specific file."
        ), TAGS["chip"]

    if not best_doc_id:
        conn.close()
        return f"Nothing found for '{query}'.", TAGS["chip"]

    # Get contiguous chunks from best document around best-scoring chunk
    window_start = max(0, best_chunk_idx - 1)
    window_end   = best_chunk_idx + 4  # 5 chunks total
    chunk_rows   = conn.execute(
        "SELECT chunk_text FROM chunks WHERE document_id=? "
        "AND chunk_index >= ? AND chunk_index <= ? "
        "ORDER BY chunk_index",
        (best_doc_id, window_start, window_end)
    ).fetchall()
    conn.close()

    if not chunk_rows:
        return f"Nothing found for '{query}'.", TAGS["chip"]

    raw_chunks = "\n\n".join(r[0] for r in chunk_rows)

    # Gemini Flash summary pass
    try:
        summary_prompt = (
            f"The user asked: '{query}'\n\n"
            f"Here are relevant excerpts from {best_filename}:\n\n"
            f"{raw_chunks[:4000]}\n\n"
            f"Summarize what these excerpts say that is relevant to the query. "
            f"2-4 sentences. Be specific. If the excerpts don't actually address "
            f"the query, say so clearly."
        )
        url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
               f"gemini-3.5-flash:generateContent?key={GEMINI_KEY}")
        import aiohttp as _aio
        # Synchronous fallback since handle_vault may be called from sync context
        import requests as _req
        resp = _req.post(url, json={"contents": [{"parts": [{"text": summary_prompt}]}]}, timeout=20)
        resp.raise_for_status()
        summary = resp.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
        answer  = f"[{best_filename}]\n\n{summary}"
    except Exception as e:
        print(f"⚠️ Vault summary error: {e}")
        answer = f"[{best_filename}]\n\n{raw_chunks[:2000]}"

    return answer, f"{TAGS['vault']} [{best_filename}]"

# ─── PDF pipeline ─────────────────────────────────────────────────────────────

# Filename is "meaningless" if it looks like a timestamp/random ID
_MEANINGLESS_FILENAME = re.compile(r'^[\d_]{10,}\.pdf$', re.IGNORECASE)

def _extract_pdf_created_at(path):
    """Extract creation date from PDF metadata. Returns YYYY-MM-DD string or None."""
    try:
        import pdfplumber
        with pdfplumber.open(path) as pdf:
            raw = (pdf.metadata or {}).get("CreationDate", "")
            if not raw:
                return None
            raw = raw.strip()
            if raw.startswith("D:"):
                raw = raw[2:]
            m = re.match(r'(\d{4})(\d{2})(\d{2})', raw)
            if m:
                return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    except Exception:
        pass
    return None

async def process_pdf_upload(session, sender, save_path, raw_filename,
                             domain="unclassified", info=None):
    """Ingest an uploaded PDF.

    `save_path` is a temporary ".part" file. It is moved to its final name in
    the synced folder only AFTER extraction and vault ingest, with its hash
    recorded, so pdf_watcher never sees a half-processed upload. Before 19 Nov
    2026 the watcher renamed the file mid-processing: the coordinator then
    ingested a second copy under a name that no longer existed on disk, and
    every later attempt to read the file by that name failed.

    `info`, if given, receives the final filename, path and extracted text.
    """
    _dir = os.path.dirname(save_path)

    def _publish(name):
        target = os.path.join(_dir, name)
        if os.path.exists(target) and os.path.abspath(target) != os.path.abspath(save_path):
            stem, ext = os.path.splitext(name)
            target = os.path.join(_dir, f"{stem}-{datetime.now():%H%M%S}{ext}")
        try:
            os.replace(save_path, target)
        except Exception as e:
            print(f"⚠️ PDF publish failed: {e}")
            return save_path
        return target

    try:
        import pdfplumber
        pdf_text   = ""
        first_page = ""
        with pdfplumber.open(save_path) as pdf:
            for i, page in enumerate(pdf.pages[:150]):
                extracted = page.extract_text()
                if extracted:
                    pdf_text += extracted + "\n"
                    if i == 0:
                        first_page = extracted
    except Exception as e:
        _publish(raw_filename)
        return f"PDF saved as {raw_filename}, but " + fail_line("text extraction", e)

    if not pdf_text.strip():
        _publish(raw_filename)
        return (f"PDF saved as {raw_filename}, but it has no extractable text "
                f"(probably a scan), so I can't read it yet.")

    # Extract PDF creation date from metadata
    doc_created_at = _extract_pdf_created_at(save_path)

    # Determine new filename:
    # - If original name is human-readable, keep it (just prepend date if missing)
    # - If original name is a meaningless timestamp, generate a slug from content
    if _MEANINGLESS_FILENAME.match(raw_filename):
        # Generate slug from content
        try:
            def _summarize():
                client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
                resp   = client.messages.create(
                    model=MODELS["cloud_heavy"], max_tokens=CLAUDE_MAX_TOKENS,
                    thinking={"type": "disabled"},
                    messages=[{"role": "user", "content": (
                        "Read this document and return JSON only — no markdown:\n"
                        "{\"title\": \"3-5 word slug, lowercase-hyphenated\", "
                        "\"summary\": \"exactly 2 sentences describing this document\"}\n\n"
                        f"{first_page[:3000]}"
                    )}]
                )
                call_logger.log_model_call(
                    model=MODELS["cloud_heavy"], thinking=False,
                    tokens_in=resp.usage.input_tokens, tokens_out=resp.usage.output_tokens,
                    stop_reason=resp.stop_reason, tools=[],
                )
                raw = resp.content[0].text.strip().replace("```json","").replace("```","").strip()
                return json.loads(raw)

            loop    = asyncio.get_event_loop()
            meta    = await asyncio.to_thread(_summarize)
            slug    = re.sub(r'[^\w]', '-', meta.get("title", "document").lower()).strip("-")
            slug    = re.sub(r'-+', '-', slug)
            summary = meta.get("summary", "")
        except Exception as e:
            print(f"⚠️ PDF summarize: {e}")
            slug    = "document"
            summary = ""

        date_prefix  = (doc_created_at or datetime.now().strftime("%Y-%m-%d")).replace("-", "")
        new_filename = f"{date_prefix}_{slug}.pdf"
    else:
        # Human-readable filename — preserve it, just get summary
        new_filename = raw_filename
        try:
            def _summary_only():
                client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
                resp   = client.messages.create(
                    model=MODELS["cloud_heavy"], max_tokens=CLAUDE_MAX_TOKENS,
                    thinking={"type": "disabled"},
                    messages=[{"role": "user", "content": (
                        "Read this document and return JSON only — no markdown:\n"
                        "{\"summary\": \"exactly 2 sentences describing this document\"}\n\n"
                        f"{first_page[:3000]}"
                    )}]
                )
                call_logger.log_model_call(
                    model=MODELS["cloud_heavy"], thinking=False,
                    tokens_in=resp.usage.input_tokens, tokens_out=resp.usage.output_tokens,
                    stop_reason=resp.stop_reason, tools=[],
                )
                raw = resp.content[0].text.strip().replace("```json","").replace("```","").strip()
                return json.loads(raw).get("summary", "")
            loop    = asyncio.get_event_loop()
            summary = await asyncio.to_thread(_summary_only)
        except Exception:
            summary = ""

    # Name clash with a different file already on disk: keep both.
    if os.path.exists(os.path.join(_dir, new_filename)):
        _stem, _ext = os.path.splitext(new_filename)
        new_filename = f"{_stem}-{datetime.now():%H%M%S}{_ext}"

    try:
        from skills.vault_ingest import ingest_text
        chunks = ingest_text(pdf_text, new_filename, domain=domain, source_type="signal")
    except Exception as e:
        _publish(new_filename)
        return f"Saved as {new_filename}, but " + fail_line("indexing it", e)

    # Record the file hash so pdf_watcher recognises this document as already
    # ingested, THEN move the file into place under its final name.
    try:
        import hashlib
        _h = hashlib.sha256(open(save_path, "rb").read()).hexdigest()[:16]
        conn = sqlite3.connect(VAULT_DB)
        conn.execute("UPDATE documents SET file_hash=? WHERE filename=?", (_h, new_filename))
        conn.commit(); conn.close()
    except Exception as e:
        print(f"⚠️ PDF hash record: {e}")
    new_path = _publish(new_filename)
    if isinstance(info, dict):
        info.update(filename=new_filename, path=new_path, text=pdf_text, chunks=chunks)

    # Store doc_created_at in documents table
    if doc_created_at:
        try:
            conn = sqlite3.connect(VAULT_DB)
            conn.execute(
                "UPDATE documents SET doc_created_at=? WHERE filename=?",
                (doc_created_at, new_filename)
            )
            conn.commit()
            conn.close()
        except Exception:
            pass

    if summary:
        try:
            _backup_context_db()
            conn = sqlite3.connect(CONTEXT_DB)
            conn.execute(
                "INSERT INTO facts (content, confirmed, created_at, user) "
                "VALUES (?, 1, ?, ?)",
                (f"Document '{new_filename}': {summary}", datetime.now().isoformat(),
                 USERS.get(sender, {}).get("name", chip_config.ADMIN_NAME).lower())
            )
            conn.commit()
            conn.close()
        except Exception:
            pass

    result = f"PDF ingested: {new_filename}\n{chunks} chunks"
    if summary:
        result += f"\n\n{summary}"
    return result

# ─── Builtin handlers ─────────────────────────────────────────────────────────

async def handle_fetch(session, sender, query):
    """Fuzzy filename match in vault DB -> send file as Signal attachment."""
    tag = TAGS["chip"]
    if not query:
        await send_signal(session, f"{tag} Usage: fetch: filename or search term", recipient=sender)
        return

    conn  = sqlite3.connect(VAULT_DB)
    terms = [t for t in query.lower().split() if len(t) > 2]
    if not terms:
        conn.close()
        await send_signal(session, f"{tag} Search term too short.", recipient=sender)
        return

    # Try exact filename substring match first
    rows = conn.execute(
        "SELECT filename, domain FROM documents ORDER BY ingested_at DESC"
    ).fetchall()
    conn.close()

    # Score by how many query terms appear in filename
    scored = []
    for fname, domain in rows:
        fname_lower = fname.lower()
        hits = sum(1 for t in terms if t in fname_lower)
        if hits > 0:
            scored.append((hits, fname, domain))
    scored.sort(reverse=True)

    if not scored:
        await send_signal(session, f"{tag} No files matching '{query}'.", recipient=sender)
        return

    if len(scored) > 1 and scored[0][0] == scored[1][0]:
        # Ambiguous — list top matches
        lines = [f"{tag} Multiple matches for '{query}':"]
        for _, fname, domain in scored[:5]:
            lines.append(f"  {fname}  [{domain}]")
        lines.append("Be more specific.")
        await send_signal(session, "\n".join(lines), recipient=sender)
        return

    _, best_fname, best_domain = scored[0]

    # Find the actual file path
    search_dirs = get_search_dirs()
    found_path = None
    for d in search_dirs:
        candidate = os.path.join(d, best_fname)
        if os.path.exists(candidate):
            found_path = candidate
            break

    if not found_path:
        # File moved or deleted — return chunk text instead
        conn  = sqlite3.connect(VAULT_DB)
        chunk = conn.execute(
            "SELECT c.chunk_text FROM chunks c "
            "JOIN documents d ON c.document_id = d.id "
            "WHERE d.filename=? ORDER BY c.chunk_index LIMIT 3",
            (best_fname,)
        ).fetchall()
        conn.close()
        if chunk:
            text_out = "\n---\n".join(r[0] for r in chunk)
            await deliver_response(session, f"File not on disk. First chunks:\n\n{text_out}",
                                   sender, tag=tag)
        else:
            await send_signal(session, f"{tag} File not found on disk: {best_fname}", recipient=sender)
        return

    # Return path and content for pipeline use
    # Caller decides whether to send as file or pass forward
    return found_path, best_fname, best_domain

async def handle_memory(session, sender, args):
    """
    Natural language memory editing.
    memory: update X          — find and supersede relevant fact
    memory: rebuild profile   — rewrite the admin identity from active facts
    memory: show recent       — show changes from last 7 days
    memory: archive X         — archive a specific fact
    """
    tag = TAGS["chip"]
    if not args:
        await send_signal(session,
            f"{tag} Usage:\n"
            f"  memory: update <what changed>\n"
            f"  memory: rebuild profile\n"
            f"  memory: show recent\n"
            f"  memory: archive <fact to remove>",
            recipient=sender)
        return

    args_lower = args.lower().strip()

    # Show recent changes
    if args_lower.startswith("show recent") or args_lower.startswith("recent"):
        conn = sqlite3.connect(CONTEXT_DB)
        cutoff = (datetime.now() - timedelta(days=7)).isoformat()
        rows = conn.execute(
            "SELECT change_type, old_content, new_content, changed_at FROM fact_history "
            "WHERE changed_at > ? ORDER BY changed_at DESC LIMIT 20",
            (cutoff,)
        ).fetchall()
        conn.close()
        if not rows:
            await send_signal(session, f"{tag} No memory changes in the last 7 days.", recipient=sender)
            return
        lines = [f"{tag} Recent memory changes:"]
        for change_type, old, new, ts in rows:
            ts_short = ts[:10]
            if change_type == "insert":
                lines.append(f"  + {new[:60]}  [{ts_short}]")
            elif change_type == "supersede":
                lines.append(f"  → {new[:60]}  [{ts_short}]")
            elif change_type == "archive":
                lines.append(f"  - {old[:60]}  [{ts_short}]")
        await deliver_response(session, "\n".join(lines), sender, tag=None)
        return

    # Rebuild profile (admin identity synthesis)
    if "rebuild profile" in args_lower or "rebuild" in args_lower:
        await working()   # ⏳ instead of a "Rebuilding…" message
        try:
            import anthropic as _anth
            conn = sqlite3.connect(CONTEXT_DB)
            active_facts = conn.execute(
                "SELECT id, content, created_at FROM facts "
                "WHERE confirmed=1 AND (status='active' OR status IS NULL) "
                "ORDER BY created_at DESC"
            ).fetchall()
            facts_str = "\n".join(f"- {c}" for _, c, _ in active_facts)
            identity_path = f"{WORKSPACE}/config/identities/{chip_config.ADMIN_NAME.lower()}.md"
            existing = open(identity_path).read() if os.path.exists(identity_path) else ""

            prompt = (
                f"Update this personal identity summary for the admin's AI assistant (Chip).\n"
                f"Plain prose, 2-3 paragraphs, third person, no markdown.\n\n"
                f"ACTIVE FACTS:\n{facts_str}\n\n"
                f"PREVIOUS:\n{existing[:2000]}"
            )
            client = _anth.Anthropic(api_key=ANTHROPIC_KEY)
            resp   = client.messages.create(
                model=MODELS["cloud_heavy"], max_tokens=CLAUDE_MAX_TOKENS,
                messages=[{"role": "user", "content": prompt}]
            )
            call_logger.log_model_call(
                model=MODELS["cloud_heavy"], thinking=True,
                tokens_in=resp.usage.input_tokens, tokens_out=resp.usage.output_tokens,
                stop_reason=resp.stop_reason, tools=[],
            )
            new_content = "\n".join(b.text for b in resp.content if b.type == "text").strip()

            # Archive previous
            conn.execute(
                "INSERT INTO profile_history (generated_at, content, fact_count) VALUES (?, ?, ?)",
                (datetime.now().isoformat(), existing, len(active_facts))
            )
            conn.commit()
            conn.close()

            os.makedirs(os.path.dirname(identity_path), exist_ok=True)
            open(identity_path, "w").write(new_content)
            await send_signal(session,
                f"{tag} {chip_config.ADMIN_NAME.lower()}.md rebuilt from {len(active_facts)} active facts.",
                recipient=sender)
        except Exception as e:
            await send_signal(session, f"{tag} " + fail_line("The rebuild", e), recipient=sender)
        return

    # Natural language update/supersede/archive — ask Claude to interpret
    try:
        conn = sqlite3.connect(CONTEXT_DB)
        active_facts = conn.execute(
            "SELECT id, content FROM facts WHERE confirmed=1 AND (status='active' OR status IS NULL)"
        ).fetchall()
        conn.close()

        facts_str = "\n".join(f"[ID:{fid}] {content}" for fid, content in active_facts)
        client    = __import__("anthropic").Anthropic(api_key=ANTHROPIC_KEY)
        prompt    = (
            f"The user wants to update their memory database. Instruction: {args}\n\n"
            f"ACTIVE FACTS:\n{facts_str}\n\n"
            f"Return ONLY JSON array of operations:\n"
            f"[\n"
            f"  {{\"op\": \"supersede\", \"old_id\": N, \"new_content\": \"...\"}},\n"
            f"  {{\"op\": \"archive\", \"old_id\": N, \"reason\": \"...\"}},\n"
            f"  {{\"op\": \"insert\", \"content\": \"...\"}}\n"
            f"]"
        )
        resp = client.messages.create(
            model=MODELS["cloud_heavy"], max_tokens=CLAUDE_MAX_TOKENS,
            messages=[{"role": "user", "content": prompt}]
        )
        call_logger.log_model_call(
            model=MODELS["cloud_heavy"], thinking=True,
            tokens_in=resp.usage.input_tokens, tokens_out=resp.usage.output_tokens,
            stop_reason=resp.stop_reason, tools=[],
        )
        raw = "\n".join(b.text for b in resp.content if b.type == "text")
        raw = raw.strip().replace("```json","").replace("```","").strip()
        m   = re.search(r'\[.*\]', raw, re.DOTALL)
        if not m:
            await send_signal(session, f"{tag} Could not parse memory operation.", recipient=sender)
            return

        operations = json.loads(m.group(0))

        # Import and apply
        from dream_cycle import (apply_operations, supersede_fact,
                                  insert_fact, archive_fact)

        conn = sqlite3.connect(CONTEXT_DB)
        _backup_context_db()
        inserted, superseded, archived, _ = apply_operations(operations, conn)
        conn.close()

        lines = [f"{tag} Memory updated:"]
        for c in inserted:
            lines.append(f"  + {c[:60]}")
        for old_id, new_c in superseded:
            lines.append(f"  → {new_c[:60]}")
        for old_id in archived:
            lines.append(f"  - archived ID {old_id}")
        await send_signal(session, "\n".join(lines), recipient=sender)

    except Exception as e:
        await send_signal(session, f"{tag} " + fail_line("The memory update", e), recipient=sender)


async def handle_rechunk(session, sender, args):
    """Force re-ingest of a specific document."""
    tag = TAGS["chip"]
    if not args:
        await send_signal(session, f"{tag} Usage: rechunk: filename or search term", recipient=sender)
        return

    # Find the document
    conn  = sqlite3.connect(VAULT_DB)
    terms = [t for t in args.lower().split() if len(t) > 2]
    rows  = conn.execute("SELECT id, filename FROM documents ORDER BY ingested_at DESC").fetchall()

    scored = []
    for doc_id, fname in rows:
        hits = sum(1 for t in terms if t in fname.lower())
        if hits > 0:
            scored.append((hits, doc_id, fname))
    scored.sort(reverse=True)

    if not scored:
        conn.close()
        await send_signal(session, f"{tag} No docs matching '{args}'.", recipient=sender)
        return

    if len(scored) > 1 and scored[0][0] == scored[1][0]:
        lines = [f"{tag} Multiple matches:"]
        for _, _, fname in scored[:5]:
            lines.append(f"  {fname}")
        lines.append("Be more specific.")
        conn.close()
        await send_signal(session, "\n".join(lines), recipient=sender)
        return

    _, doc_id, filename = scored[0]

    # Find file on disk
    search_dirs = get_search_dirs()

    found_path = None
    for d in search_dirs:
        candidate = os.path.join(d, filename)
        if os.path.exists(candidate):
            found_path = candidate
            break

    if not found_path:
        conn.close()
        await send_signal(session,
            f"{tag} {filename} found in DB but not on disk — cannot rechunk.",
            recipient=sender)
        return

    # Delete existing chunks and re-ingest
    domain = conn.execute("SELECT domain FROM documents WHERE id=?", (doc_id,)).fetchone()[0]
    conn.execute("DELETE FROM chunks WHERE document_id=?", (doc_id,))
    conn.execute("DELETE FROM documents WHERE id=?", (doc_id,))
    conn.commit()
    conn.close()

    await working()   # ⏳ instead of a "Rechunking…" message

    try:
        from skills.vault_ingest import ingest_text

        _text_exts = {".md", ".txt", ".markdown", ".text"}
        _is_text   = any(filename.lower().endswith(e) for e in _text_exts)

        if _is_text:
            # Plain text file
            content = open(found_path, encoding="utf-8", errors="replace").read()
            if not content.strip():
                await send_signal(session, f"{tag} Empty file: {filename}.", recipient=sender)
                return
            chunks = ingest_text(content, filename, domain=domain, source_type="rechunk")
        else:
            # PDF
            import pdfplumber
            pdf_text = ""
            with pdfplumber.open(found_path) as pdf:
                for page in pdf.pages[:150]:
                    extracted = page.extract_text()
                    if extracted:
                        pdf_text += extracted + "\n"
            if not pdf_text.strip():
                await send_signal(session,
                    f"{tag} No extractable text in {filename}.", recipient=sender)
                return
            chunks = ingest_text(pdf_text, filename, domain=domain, source_type="rechunk")

        await send_signal(session,
            f"{tag} {filename} rechunked: {chunks} chunks, domain={domain}",
            recipient=sender)
    except Exception as e:
        await send_signal(session, f"{tag} " + fail_line("The rechunk", e), recipient=sender)


async def handle_voice(session, sender, args):
    if not args or args.lower() == "reset":
        active_voices[sender] = {"prompt": None, "remaining": None}
        await send_signal(session, f"{TAGS['chip']} Voice reset.", recipient=sender)
        return
    parts = args.rsplit(" ", 1)
    count = int(parts[1]) if len(parts) == 2 and parts[1].isdigit() else None
    prompt_text = parts[0] if count else args
    active_voices[sender] = {"prompt": prompt_text, "remaining": count}
    suffix = f" for {count} replies" if count else " until reset"
    await send_signal(session, f"{TAGS['chip']} Voice set{suffix}.", recipient=sender)

async def handle_save(session, sender, modifier=""):
    conn = sqlite3.connect(CONTEXT_DB)
    if modifier in ("replace", "both"):
        conflicts = conn.execute(
            "SELECT pf.id, pf.content, pf.conflict_with FROM pending_facts "
            "WHERE conflict_with IS NOT NULL"
        ).fetchall()
        for pf_id, content, old_id in conflicts:
            if modifier == "replace":
                conn.execute("UPDATE facts SET content=? WHERE id=?", (content, old_id))
            else:
                conn.execute(
                    "INSERT INTO facts (content, confirmed, created_at) VALUES (?, 1, ?)",
                    (content, datetime.now().isoformat())
                )
            conn.execute("DELETE FROM pending_facts WHERE id=?", (pf_id,))
        conn.commit()
        conn.close()
        await send_signal(session,
            f"{TAGS['chip']} {'Replaced.' if modifier == 'replace' else 'Both kept.'}",
            recipient=sender)
        return

    rows = conn.execute(
        "SELECT id, content FROM pending_facts WHERE conflict_with IS NULL ORDER BY proposed_at"
    ).fetchall()
    if not rows:
        conflicts_count = conn.execute("SELECT COUNT(*) FROM pending_facts").fetchone()[0]
        conn.close()
        if conflicts_count:
            await send_signal(session,
                f"{TAGS['chip']} Conflicts pending. Reply: save: replace / save: both / skip:",
                recipient=sender)
        else:
            await send_signal(session, f"{TAGS['chip']} No pending facts.", recipient=sender)
        return

    _backup_context_db()
    saved = 0
    for row_id, content in rows:
        existing = conn.execute(
            "SELECT id FROM facts WHERE confirmed=1 AND content LIKE ?",
            (f"%{content[:30]}%",)
        ).fetchone()
        if existing:
            conn.execute("UPDATE pending_facts SET conflict_with=? WHERE id=?",
                         (existing[0], row_id))
        else:
            conn.execute(
                "INSERT INTO facts (content, confirmed, created_at) VALUES (?, 1, ?)",
                (content, datetime.now().isoformat())
            )
            conn.execute("DELETE FROM pending_facts WHERE id=?", (row_id,))
            saved += 1

    conn.commit()
    conflicts = conn.execute(
        "SELECT pf.content, f.content FROM pending_facts pf "
        "JOIN facts f ON pf.conflict_with = f.id"
    ).fetchall()
    conn.close()

    if conflicts:
        lines = [f"{TAGS['chip']} {saved} saved. Conflicts:"]
        for new, old in conflicts:
            lines += [f"Existing: {old}", f"New:      {new}"]
        lines.append("Reply: save: replace / save: both / skip:")
        await send_signal(session, "\n".join(lines), recipient=sender)
    else:
        await send_signal(session, f"{TAGS['chip']} {saved} fact(s) saved.", recipient=sender)

async def handle_skip(session, sender):
    conn = sqlite3.connect(CONTEXT_DB)
    # NOTE: pending_facts has no user column, so this cannot be scoped by
    # sender. Access is gated instead — "skip" is in ADMIN_ONLY_CMDS.
    conn.execute("DELETE FROM pending_facts")
    conn.commit()
    conn.close()
    await send_signal(session, f"{TAGS['chip']} Pending facts discarded.", recipient=sender)

async def handle_forget(session, sender, query):
    if not query:
        await send_signal(session, f"{TAGS['chip']} Usage: forget: <keywords>", recipient=sender)
        return
    if query.strip().lower() == "confirm":
        await handle_forget_confirm(session, sender)
        return
    terms   = [t.lower() for t in query.split() if len(t) > 2]
    conn    = sqlite3.connect(CONTEXT_DB)
    _u      = USERS.get(sender, {}).get("name", "").lower()
    rows    = conn.execute(
        "SELECT id, content FROM facts WHERE confirmed=1 AND user=?", (_u,)
    ).fetchall()
    conn.close()
    matches = [(r_id, content) for r_id, content in rows
               if any(t in content.lower() for t in terms)]
    if not matches:
        await send_signal(session, f"{TAGS['chip']} No facts matching '{query}'.", recipient=sender)
        return
    lines = [f"{TAGS['chip']} Delete {len(matches)} fact(s)?"]
    for _, content in matches[:5]:
        lines.append(f"• {content}")
    if len(matches) > 5:
        lines.append(f"...and {len(matches)-5} more")
    lines.append("Reply: forget: confirm")
    active_voices[f"__forget_{sender}"] = [r_id for r_id, _ in matches]
    await send_signal(session, "\n".join(lines), recipient=sender)

async def handle_forget_confirm(session, sender):
    key = f"__forget_{sender}"
    ids = active_voices.pop(key, [])
    if not ids:
        await send_signal(session, f"{TAGS['chip']} No pending deletion.", recipient=sender)
        return
    _backup_context_db()
    conn = sqlite3.connect(CONTEXT_DB)
    conn.executemany("DELETE FROM facts WHERE id=?", [(i,) for i in ids])
    conn.commit()
    conn.close()
    await send_signal(session, f"{TAGS['chip']} Deleted {len(ids)} fact(s).", recipient=sender)

async def handle_wx(session, sender, args):
    try:
        from skills.wx import get_weather
        answer, tag = await get_weather(session, sender, args)
    except ImportError:
        answer, tag = "wx skill not installed.", TAGS["chip"]
    await deliver_response(session, answer, sender, tag=tag)

def _set_home_ok():
    """Operator override for the VPN fail-closed gate — 30 min, self-expiring."""
    try:
        os.makedirs("/run/chip", exist_ok=True)
        open("/run/chip/home_ok", "w").write(datetime.now().isoformat())
        return True
    except Exception:
        return False


async def handle_archive(session, sender, args):
    if not args:
        await send_signal(session, f"{TAGS['chip']} Usage: archive: <url> [prompt]", recipient=sender)
        return
    parts = args.split(None, 1)
    archive_url = parts[0]
    prompt_txt  = parts[1] if len(parts) > 1 else None
    archive_dir = f"{WORKSPACE}/data/archive"
    os.makedirs(archive_dir, exist_ok=True)
    ts_str   = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_file = f"{archive_dir}/{ts_str}.html"
    try:
        from curl_cffi import requests as cffi_requests
        from bs4 import BeautifulSoup

        def _fetch(url, path):
            for fp in ["chrome131", "chrome120", "safari17_0"]:
                try:
                    r = cffi_requests.get(url, impersonate=fp, timeout=30, allow_redirects=True)
                    if r.status_code == 200:
                        break
                except Exception:
                    continue
            else:
                raise Exception("All fingerprints failed")
            soup = BeautifulSoup(r.text, "html.parser")
            for el in soup(["script","style","nav","footer","header","aside"]):
                el.decompose()
            open(path, "w", encoding="utf-8").write(str(soup))
            return os.path.getsize(path), soup.get_text(separator=" ", strip=True)

        from skills.web_utils import vpn_blocked, VPN_REFUSAL
        if vpn_blocked():
            await send_signal(session, f"{TAGS['chip']} {VPN_REFUSAL}", recipient=sender)
            return VPN_REFUSAL
        loop = asyncio.get_event_loop()
        size, page_text = await loop.run_in_executor(None, _fetch, archive_url, out_file)
        # Phase 3 (browser spec): prefer the rendered page when the live
        # browser is up — JS-heavy pages give the static fetcher a shell.
        try:
            from skills.browse import fetch_page as _bfetch, service_up as _bup
            if _bup():
                rendered = await _bfetch(archive_url, user="archive")
                if len(rendered) > len(page_text) * 1.3:
                    page_text = rendered
        except Exception as _be:
            print(f"archive: browser render skipped ({_be})")
        if prompt_txt:
            history = load_history(sender)
            rag_ctx, _, _ = await asyncio.to_thread(
                get_rag_context, prompt_txt, None, sender)
            answer, tag = await ask_cloud(
                session, f"PAGE CONTENT:\n{page_text[:12000]}\n\nREQUEST: {prompt_txt}",
                sender, tier="cloud_default", rag_context=rag_ctx or None, history=history,
            )
            answer = f"Archived ({size//1024}KB)\n\n{answer}"
        else:
            answer = f"Archived: {os.path.basename(out_file)}\nSize: {size//1024}KB"
            tag    = TAGS["chip"]
        await deliver_response(session, answer, sender, tag=tag)
    except Exception as e:
        await deliver_response(session, fail_line("The archive", e), sender, tag=TAGS["chip"])

# ─── Media dispatch ───────────────────────────────────────────────────────────

async def media_dispatch_batch(session, sender, text, attachments, ts):
    """
    Handle multiple attachments sent in one Signal message.
    Downloads all images, sends them together in a single Gemini call.
    Non-image attachments (PDFs, audio) are processed individually.
    """
    tag        = TAGS["cloud_default"]
    clean_text = text.strip() or "Compare these and tell me which is best."
    history    = load_history(sender, USERS.get(sender, {}).get("history", 30))
    system     = load_context(sender, sections=[], length="default")

    # What the user actually typed, for any PDFs in this batch. media_dispatch
    # does this for a single attachment; here the prefixes were never parsed at
    # all, so "recipes: add these" sent with two PDFs lost both the caption and
    # the domain and came back as two bare ingest receipts.
    _batch_raw   = (text or "").strip()
    _mpfx_m      = re.match(r'^(s|o|g|gp)\s*:\s*(.*)', _batch_raw, re.IGNORECASE | re.DOTALL)
    _mpfx        = _mpfx_m.group(1).lower() if _mpfx_m else None
    _batch_rest  = _mpfx_m.group(2).strip() if _mpfx_m else _batch_raw
    pdf_tier     = {"s": "cloud_heavy", "o": "cloud_opus", "g": "cloud_default",
                    "gp": "cloud_top"}.get(
                        _mpfx, USERS.get(sender, {}).get("default_model", "cloud_default"))
    _dom_m       = re.match(r'^(' + chip_config.PREFIX_DOMAIN_REGEX + r')\s*:\s*(.*)',
                            _batch_rest, re.IGNORECASE)
    pdf_domain   = _dom_m.group(1).lower() if _dom_m else "unclassified"
    pdf_caption  = _dom_m.group(2).strip() if _dom_m else _batch_rest

    image_parts  = []
    other_results = []
    pdf_docs      = []            # (published name, extracted text) for the caption call
    save_dir     = f"{SYNCTHING_DIR}/media"
    os.makedirs(save_dir, exist_ok=True)

    for att in attachments:
        content_type = att.get("contentType", "")
        att_id       = att.get("id", "")
        ext          = content_type.split("/")[-1].split(";")[0].strip()
        ext          = {"jpeg": "jpg", "quicktime": "mov"}.get(ext, ext)
        filename     = f"{ts}_{sender[-4:]}_{att_id[-4:]}.{ext}"
        save_path    = f"{save_dir}/{filename}"

        try:
            att_file = f"{SIGNAL_CONFIG_DIR}/attachments/{att_id}"
            if not os.path.exists(att_file):
                other_results.append(f"Attachment not found: {att_id}")
                continue
            raw_bytes = open(att_file, "rb").read()
            if content_type == "application/pdf":
                save_path += ".part"        # hidden from pdf_watcher until processed
            open(save_path, "wb").write(raw_bytes)
            print(f"📁 Saved: {save_path}")
        except Exception as e:
            other_results.append(f"Attachment error for {filename}: {e}")
            continue

        if content_type == "application/pdf":
            await working()   # ⏳ instead of a "Processing PDF…" message
            _info  = {}
            result = await process_pdf_upload(session, sender, save_path, filename,
                                              domain=pdf_domain, info=_info)
            # With a caption, the ingest receipt is not the answer — hold the
            # document back and let the chat model reply to it below. Without
            # one, or when nothing could be extracted, the receipt is all there
            # is to say, exactly as before.
            if pdf_caption and _info.get("text"):
                pdf_docs.append((_info.get("filename", filename), _info["text"]))
            else:
                other_results.append(result)
        elif content_type.startswith("audio/"):
            from skills.audio_id import handle_audio_attachment
            other_results.append(handle_audio_attachment(sender, save_path, content_type))
        elif content_type.startswith("image/"):
            b64 = base64.b64encode(raw_bytes).decode()
            image_parts.append({"inline_data": {"mime_type": content_type, "data": b64}})
        else:
            other_results.append(f"Saved {filename} ({content_type})")

    # Send non-image results first
    for r in other_results:
        await deliver_response(session, r, sender, tag=TAGS["chip"])

    # One answer covering every captioned PDF in the message. They arrived
    # together under one instruction ("which of these is cheaper?"), so they
    # are asked about together; answering each in isolation cannot compare
    # them. The 60k character budget is the single-PDF limit, shared out.
    if pdf_docs:
        per_doc  = max(2000, 60000 // len(pdf_docs))
        sections = []
        for _name, _doc in pdf_docs:
            _cut = "" if len(_doc) <= per_doc else f"\n[{_name} truncated at {per_doc:,} characters]"
            sections.append(f"--- {_name} ---\n{_doc[:per_doc]}{_cut}")
        _count  = ("a PDF" if len(pdf_docs) == 1
                   else f"{len(pdf_docs)} PDFs")
        _prompt = (f"{pdf_caption}\n\n[The user attached {_count} with this message. "
                   f"They are saved and indexed under the names shown below. "
                   f"The full text of each follows.]\n\n" + "\n\n".join(sections))
        _saved  = ", ".join(n for n, _ in pdf_docs)
        try:
            reply, rtag = await ask_cloud(session, _prompt, sender, tier=pdf_tier,
                                          history=history)
            await deliver_response(session, f"(Saved as {_saved})\n\n{reply}", sender, tag=rtag)
            log_to_buffer(sender, f"[pdf {_saved}] {pdf_caption}", reply)
        except Exception as e:
            # The documents are already ingested; say so rather than losing them.
            await deliver_response(session, f"Saved as {_saved}, but " + fail_line("the reply", e),
                                   sender, tag=TAGS["chip"])

    if not image_parts:
        return

    # Send all images together in one Gemini call
    try:
        url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
               f"{MODELS['cloud_default']}:generateContent?key={GEMINI_KEY}")

        # Build contents with history + all images in one user turn
        contents = [
            {"role": "user",  "parts": [{"text": f"System:\n{system}"}]},
            {"role": "model", "parts": [{"text": "Understood."}]},
        ]
        for turn in history:
            role = "user" if turn["role"] == "user" else "model"
            contents.append({"role": role, "parts": [{"text": turn["content"]}]})

        # All images + prompt in one user turn
        user_parts = [{"text": f"{clean_text} ({len(image_parts)} images attached)"}]
        user_parts.extend(image_parts)
        contents.append({"role": "user", "parts": user_parts})

        async with session.post(url, json={"contents": contents},
                                timeout=aiohttp.ClientTimeout(total=90)) as resp:
            resp.raise_for_status()
            data   = await resp.json()
            answer = strip_markdown(data["candidates"][0]["content"]["parts"][0]["text"])
            # Auto-extract media reviews if vision response contains ratings
            try:
                from skills.media_review import maybe_extract_from_vision
                maybe_extract_from_vision(sender, answer)
            except Exception as _mre:
                print(f'Media review auto-extract error: {_mre}')

        await deliver_response(session, answer, sender, tag=tag)
        log_to_buffer(sender, f"[{len(image_parts)} images] {clean_text}", answer)
        _tax_name = USERS.get(sender, {}).get("name", "").lower()
        if _tax_name:
            taxonomy.enqueue_work(kind="tag_extract", sender=sender, name=_tax_name,
                                   user_text=clean_text, response_text=answer, source="vision")

    except Exception as e:
        await deliver_response(session, fail_line("Handling those attachments", e), sender, tag=TAGS["chip"])


async def media_dispatch(session, sender, text, attachment, ts):
    content_type = attachment.get("contentType", "")
    att_id       = attachment.get("id", "")

    # Parse command prefix — determines model for this media call
    # s:/o: → Claude (vision API); g:/gp: → Gemini; none → cloud_default (Gemini)
    # video always → cloud_default regardless of prefix
    # domain: prefix tags PDFs (domains from config/domains.py)
    # save: prefix (images only) → skip the VLM call, file it with auto-classification
    raw_text     = (text or "").strip()

    _save_m = re.match(r'^save\s*:\s*(.*)', raw_text, re.IGNORECASE | re.DOTALL)
    save_only = bool(_save_m)
    if save_only:
        raw_text = _save_m.group(1).strip()

    # Detect model override prefix
    _model_prefix_m = re.match(r'^(s|o|g|gp)\s*:\s*(.*)', raw_text, re.IGNORECASE | re.DOTALL)
    if _model_prefix_m:
        _mpfx   = _model_prefix_m.group(1).lower()
        raw_text_rest = _model_prefix_m.group(2).strip()
    else:
        _mpfx         = None
        raw_text_rest = raw_text

    # Map prefix → media tier
    _prefix_tier = {"s": "cloud_heavy", "o": "cloud_opus", "g": "cloud_default", "gp": "cloud_top"}
    media_tier   = _prefix_tier.get(_mpfx, USERS.get(sender, {}).get("default_model", "cloud_default"))
    # Gemini-only tiers for non-Claude path
    _claude_tiers = {"cloud_heavy", "cloud_opus"}
    force_claude  = media_tier in _claude_tiers

    # Extract domain prefix for PDFs
    domain_match = re.match(r'^(' + chip_config.PREFIX_DOMAIN_REGEX + r')\s*:\s*(.*)', raw_text_rest, re.IGNORECASE)
    pdf_domain   = domain_match.group(1).lower() if domain_match else "unclassified"

    # Clean prompt — strip any remaining prefixes
    clean_text = raw_text_rest
    if domain_match:
        clean_text = domain_match.group(2).strip()
    caption = clean_text          # what the user actually typed, before any default prompt

    # Richer default prompt so follow-on questions have good context
    if not clean_text:
        clean_text = (
            "Describe what you see in detail. Include: what the subject is, "
            "key identifying features, condition, context, and anything notable. "
            "Be specific — this description will be used as context for follow-up questions."
        )

    tag = TAGS.get(media_tier, TAGS["cloud_default"])

    try:
        save_dir  = f"{SYNCTHING_DIR}/media"
        os.makedirs(save_dir, exist_ok=True)
        ext       = content_type.split("/")[-1].split(";")[0].strip()
        ext       = {"jpeg": "jpg", "quicktime": "mov"}.get(ext, ext)
        filename  = f"{ts}_{sender[-4:]}.{ext}"
        save_path = f"{save_dir}/{filename}"

        att_file = f"{SIGNAL_CONFIG_DIR}/attachments/{att_id}"
        raw_bytes = open(att_file, "rb").read()
        if content_type == "application/pdf":
            save_path += ".part"            # hidden from pdf_watcher until processed
        open(save_path, "wb").write(raw_bytes)
        print(f"📁 Saved: {save_path}")

        # Text/markdown files — route as message or vault ingest
        _text_exts = {".md", ".txt", ".markdown", ".text"}
        _is_text   = (any(filename.lower().endswith(e) for e in _text_exts) or
                      content_type in ("text/plain", "text/markdown",
                                       "text/x-signal-plain",       # Signal's long-message attachment MIME
                                       "application/x-signal-plain", # kept for safety/older clients
                                       "application/octet-stream"))
        # Content-sniff: if declared MIME is non-text but content decodes clean, treat as text
        if not _is_text:
            try:
                _sniff = raw_bytes[:1024].decode("utf-8")
                if sum(c.isprintable() or c in "\n\r\t " for c in _sniff) / max(len(_sniff), 1) > 0.95:
                    _is_text = True
            except (UnicodeDecodeError, ZeroDivisionError):
                pass
        if _is_text:
            text_content = raw_bytes.decode("utf-8", errors="replace").strip()
            if not text_content:
                await deliver_response(session, f"Empty file: {filename}", sender, tag=TAGS["chip"])
                log_to_buffer(sender, f"[text] {filename}", "Empty")
                return
            # Domain prefix → vault ingest (explicit storage intent)
            if pdf_domain != "unclassified":
                try:
                    from skills.vault_ingest import ingest_text as _ingest_txt
                    chunks = _ingest_txt(text_content, filename,
                                         domain=pdf_domain, source_type="signal")
                    answer = f"Text ingested: {filename} ({chunks} chunks)"
                except Exception as e:
                    answer = fail_line("Indexing that text", e)
                await deliver_response(session, answer, sender, tag=TAGS["chip"])
                log_to_buffer(sender, f"[text] {filename}", answer)
                return
            # No domain prefix → re-queue as plain text message for full pipeline routing
            combined = f"{raw_text}\n\n{text_content}".strip() if raw_text else text_content
            await message_queue.put({"sender": sender, "text": combined, "ts": f"{ts}_t"})
            print(f"📄 Text attachment re-queued ({len(text_content)} chars)")
            return

        if content_type == "application/pdf":
            _info  = {}
            answer = await process_pdf_upload(session, sender, save_path, filename,
                                              domain=pdf_domain, info=_info)
            _fname = _info.get("filename", filename)
            if caption and _info.get("text"):
                # The caption is an instruction about this document ("add this
                # to recipes", "how does it look?"). Give the chat model the
                # document text and its tools so it can act on it, instead of
                # replying with a bare ingest receipt.
                _doc = _info["text"]
                _cut = "" if len(_doc) <= 60000 else "\n[document truncated at 60,000 characters]"
                _prompt = (f"{caption}\n\n[The user attached a PDF with this message. It is "
                           f"saved and indexed as {_fname}. Its full text follows.]\n\n"
                           f"{_doc[:60000]}{_cut}")
                reply, rtag = await ask_cloud(
                    session, _prompt, sender, tier=media_tier,
                    history=load_history(sender, USERS.get(sender, {}).get("history", 30)))
                await deliver_response(session, f"(Saved as {_fname})\n\n{reply}", sender, tag=rtag)
                log_to_buffer(sender, f"[pdf {_fname}] {caption}", reply)
                return
            await deliver_response(session, answer, sender, tag=TAGS["chip"])
            log_to_buffer(sender, f"[pdf {_fname}] {caption}".strip(), answer)
            return

        if content_type.startswith("audio/"):
            from skills.audio_id import handle_audio_attachment
            answer = handle_audio_attachment(sender, save_path, content_type)
            await deliver_response(session, answer, sender, tag=tag)
            log_to_buffer(sender, f"[audio] {filename}", answer)
            return

        if save_only and content_type.startswith("image/"):
            caption_source = clean_text if raw_text_rest else ""
            if not caption_source:
                # No caption provided — quick low-effort Gemini caption so
                # there is still something searchable. Not the rich
                # "describe in detail" call used for normal (non-save:) sends.
                try:
                    b64_quick = base64.b64encode(raw_bytes).decode()
                    url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
                           f"{MODELS['cloud_default']}:generateContent?key={GEMINI_KEY}")
                    contents = [{"role": "user", "parts": [
                        {"text": "One short sentence caption of this image — what it is, nothing more."},
                        {"inline_data": {"mime_type": content_type, "data": b64_quick}},
                    ]}]
                    async with session.post(url, json={"contents": contents},
                                            timeout=aiohttp.ClientTimeout(total=30)) as r:
                        r.raise_for_status()
                        data = await r.json()
                        caption_source = strip_markdown(
                            data["candidates"][0]["content"]["parts"][0]["text"]
                        )
                except Exception as _cap_e:
                    caption_source = f"Saved image (caption failed: {_cap_e})"

            domain_for_save = pdf_domain
            if domain_for_save == "unclassified":
                _tag = await taxonomy.classify_taxonomy(caption_source, source="media_save")
                if _tag and _tag.get("domain"):
                    domain_for_save = _tag["domain"]

            try:
                from skills.vault_ingest import ingest_text as _save_ingest
                ingest_content = f"[Image: {filename}]\n{caption_source}"
                _save_ingest(ingest_content, filename, domain=domain_for_save, source_type="media_save")
            except Exception as _ing_e:
                print(f"⚠️ Save-only ingest error: {_ing_e}")

            confirm = f"Saved: {domain_for_save} — {caption_source[:80]}"
            await deliver_response(session, confirm, sender, tag=TAGS["chip"])
            log_to_buffer(sender, f"[save] {filename}", confirm)
            return

        b64     = base64.b64encode(raw_bytes).decode()
        history = load_history(sender, USERS.get(sender, {}).get("history", 30))
        system  = load_context(sender, sections=[], length="default")

        if force_claude:
            # Build messages with history for context
            messages = [{"role": t["role"], "content": t["content"]} for t in history]
            messages.append({"role": "user", "content": [
                {"type": "image", "source": {"type": "base64",
                                              "media_type": content_type, "data": b64}},
                {"type": "text", "text": clean_text},
            ]})
            loop   = asyncio.get_event_loop()
            def _claude_vision():
                client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
                resp = client.messages.create(
                    model=MODELS["cloud_heavy"], max_tokens=CLAUDE_MAX_TOKENS,
                    system=system, messages=messages
                )
                call_logger.log_model_call(
                    model=MODELS["cloud_heavy"], thinking=True,
                    tokens_in=resp.usage.input_tokens, tokens_out=resp.usage.output_tokens,
                    stop_reason=resp.stop_reason, tools=[],
                )
                return "\n".join(b.text for b in resp.content if b.type == "text")
            answer = await asyncio.to_thread(_claude_vision)
            # Auto-extract media reviews if vision response contains ratings
            try:
                from skills.media_review import maybe_extract_from_vision
                maybe_extract_from_vision(sender, answer)
            except Exception as _mre:
                print(f'Media review auto-extract error: {_mre}')

        elif content_type.startswith("video/"):
            # Video always routes to cloud_default (Gemini) — Claude doesn't do video
            upload_url = (f"https://generativelanguage.googleapis.com/upload/v1beta/"
                          f"files?key={GEMINI_KEY}")
            headers = {
                "X-Goog-Upload-Command":  "start, upload, finalize",
                "X-Goog-Upload-Protocol": "raw",
                "X-Goog-Upload-Header-Content-Length": str(len(raw_bytes)),
                "X-Goog-Upload-Header-Content-Type":   content_type,
                "Content-Type": content_type,
            }
            async with session.post(upload_url, data=raw_bytes, headers=headers,
                                    timeout=aiohttp.ClientTimeout(total=120)) as r:
                r.raise_for_status()
                file_data = await r.json()
            file_uri = file_data.get("file", {}).get("uri")
            if not file_uri:
                answer = "Video upload failed."
            else:
                url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
                       f"{MODELS['cloud_default']}:generateContent?key={GEMINI_KEY}")
                # Build contents with history + video
                contents = [
                    {"role": "user",  "parts": [{"text": f"System:\n{system}"}]},
                    {"role": "model", "parts": [{"text": "Understood."}]},
                ]
                for turn in history:
                    role = "user" if turn["role"] == "user" else "model"
                    contents.append({"role": role, "parts": [{"text": turn["content"]}]})
                contents.append({"role": "user", "parts": [
                    {"text": clean_text},
                    {"file_data": {"mime_type": content_type, "file_uri": file_uri}},
                ]})
                async with session.post(url, json={"contents": contents},
                                        timeout=aiohttp.ClientTimeout(total=120)) as r:
                    r.raise_for_status()
                    data   = await r.json()
                    answer = strip_markdown(data["candidates"][0]["content"]["parts"][0]["text"])
        else:
            # Default: Gemini image with history context
            url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
                   f"{MODELS['cloud_default']}:generateContent?key={GEMINI_KEY}")
            contents = [
                {"role": "user",  "parts": [{"text": f"System:\n{system}"}]},
                {"role": "model", "parts": [{"text": "Understood."}]},
            ]
            for turn in history:
                role = "user" if turn["role"] == "user" else "model"
                contents.append({"role": role, "parts": [{"text": turn["content"]}]})
            contents.append({"role": "user", "parts": [
                {"text": clean_text},
                {"inline_data": {"mime_type": content_type, "data": b64}},
            ]})
            async with session.post(url, json={"contents": contents},
                                    timeout=aiohttp.ClientTimeout(total=60)) as r:
                r.raise_for_status()
                data   = await r.json()
                answer = strip_markdown(data["candidates"][0]["content"]["parts"][0]["text"])

        await deliver_response(session, answer, sender, tag=tag)
        log_to_buffer(sender, f"[media] {clean_text}", answer)
        _tax_name = USERS.get(sender, {}).get("name", "").lower()
        if _tax_name:
            taxonomy.enqueue_work(kind="tag_extract", sender=sender, name=_tax_name,
                                   user_text=clean_text, response_text=answer, source="vision")
        # No-prefix images: persist the VLM description to vault in the
        # background so it becomes searchable later, without slowing down
        # the immediate reply. PDFs already self-ingest in process_pdf_upload;
        # this closes the same gap for images.
        if content_type.startswith("image/") and not save_only:
            async def _background_image_ingest():
                try:
                    domain_for_ingest = pdf_domain
                    if domain_for_ingest == "unclassified":
                        _tag = await taxonomy.classify_taxonomy(answer, source="vision")
                        if _tag and _tag.get("domain"):
                            domain_for_ingest = _tag["domain"]
                    from skills.vault_ingest import ingest_text as _bg_ingest
                    ingest_content = f"[Image: {filename}]\n{answer}"
                    _bg_ingest(ingest_content, filename, domain=domain_for_ingest, source_type="media_auto")
                except Exception as _bg_e:
                    print(f"⚠️ Background image ingest error: {_bg_e}")
            asyncio.create_task(_background_image_ingest())

    except Exception as e:
        await deliver_response(session, fail_line("Handling that attachment", e), sender, tag=TAGS["chip"])

# ─── Semantic dispatch ────────────────────────────────────────────────────────

async def semantic_dispatch(session, sender, query, history, length, pipeline_context=""):
    if pipeline_context:
        query = f"{query}\n\nCONTEXT FROM PREVIOUS STEP:\n{pipeline_context}".strip()

    # Always inject facts RAG for any query
    rag_facts = retrieve_facts_rag(query, sender=sender)

    # Deterministic pre-router — skip phi4-mini when possible
    decision = quick_route(query, sender)
    if decision is not None:
        call_logger.log_router("quick_route", decision.get("route", "?"))
    else:
        decision = await route(session, query, sender)
    route_to  = decision.get("route", "cloud_default")
    if route_to == "cloud_default":
        route_to = USERS.get(sender, {}).get("default_model", "cloud_default")
    sections  = decision.get("context_sections", [])
    domains   = decision.get("rag_domains", [])
    hints     = decision.get("doc_hints", [])
    fallback  = decision.get("_fallback", False)
    if route_to == "cloud_default" or fallback:
        route_to = USERS.get(sender, {}).get("default_model", "cloud_default")
        fallback = False  # user default is not a fallback

    rag_query = (" ".join(hints) + " " + query).strip() if hints else query
    domain    = domains[0] if domains else None
    if decision.get("_skip_rag"):
        rag_ctx, score, filenames = "", 0.0, []
    else:
        rag_ctx, score, filenames = await asyncio.to_thread(
            get_rag_context, rag_query, domain, sender)

    intent_label = decision.get('intent', '?') if hasattr(decision, 'get') else '?'
    call_logger.log_route_decision(route_to, score, sections)
    print(f"🔀 {intent_label} → {route_to} | score={score:.2f} | sections={sections}")


    if route_to == "vault_only" and score >= RAG_THRESHOLD:
        source_hint = f" [{filenames[0]}]" if filenames else ""
        return rag_ctx, f"{TAGS['vault']}{source_hint}"

    if route_to == "vault_only" and score < RAG_THRESHOLD:
        route_to = USERS.get(sender, {}).get("default_model", "cloud_default")
        fallback  = True

    answer, tag = await ask_cloud(
        session, query, sender, tier=route_to,
        rag_context=rag_ctx or None,
        rag_facts=rag_facts or None,
        history=history, sections=sections,
        length=length, fallback=fallback,
    )
    return answer, tag

# ─── Worker ───────────────────────────────────────────────────────────────────


def get_help_text(sender):
    user     = USERS.get(sender, {})
    name     = user.get("name", "User")
    def_tier = user.get("default_model", "cloud_default")
    def_tag  = TAGS.get(def_tier, "[G·3.5F]")
    def_name = {
        "cloud_heavy":   "Claude Sonnet",
        "cloud_default": "Gemini 3.5 Flash",
        "cloud_basic":    "Gemini Flash",
    }.get(def_tier, "Gemini 3.5 Flash")
    return HELP_TEXT.replace(
        "  s:    Claude Sonnet — default for plain text",
        f"  s:    Claude Sonnet — default for plain text"
        f"\n  (Your default: {def_name} {def_tag})"
    )

message_queue = asyncio.Queue()

# ─── Result turns (One Voice spec B.2, 20 Nov 2026) ──────────────────────────
# When a turn hands work to workers (a browser run, research, analysis), the
# turn sends nothing: ⏳ stays on the user's message and the turn is DEFERRED.
# answer_deferred waits for every worker that turn started, then runs ONE
# result turn — through the normal chat path, so the model and its client
# always match (the 19:27 404 cannot recur as a class) — and sends the one
# reply. The result turn may send work back out once or twice (code-capped)
# instead of answering; then it waits again.
#
# Untrusted content: worker output is web-derived. It is fenced and labelled
# (browse_bus.fence), and the result turn runs with a fixed read-only tool
# list (tool_calling.RESULT_TURN_TOOLS) enforced by code.
#
# History: the user's message was logged when the turn deferred; Chip's answer
# is logged as Chip's turn when it is sent. The raw worker report never enters
# the conversation history — it is in the work log and the ledger.

def _result_turn_prompt(turn, draft, results, followups_left):
    from skills import browse_bus
    name = USERS.get(turn.sender, {}).get("name", "the user")
    L = ["[RESULT TURN — this is not a message from the user]",
         f"Work you started for {name}'s message has come back. Write the ONE "
         f"reply to that message now. It is the only thing {name} receives about it.",
         "",
         f'THEIR MESSAGE: "{turn.text.strip()[:2000]}"']
    if draft and draft.strip() and draft.strip() != EMPTY_REPLY:
        L += ["", "What you wrote when you started the work (never sent; it may "
                  "cover other parts of their message):", draft.strip()[:1500]]
    L += ["", f"RESULTS ({len(results)}):"]
    for r in results:
        files = [os.path.basename(f) for f in (r.get("files") or [])]
        L.append(f"--- {r['id']} · {r['kind']} · status {r['status']} · task: "
                 f"{(r.get('task') or '')[:300]}")
        body = (r.get("result") or "(no result recorded)")[:12000]
        L.append(browse_bus.fence(body, r["id"],
                                  kind="stall" if r["status"] in ("stalled", "lost") else "terminus"))
        if files:
            L.append(f"files: {', '.join(files)}")
    L += ["",
          "How to answer:",
          "- Answer their message directly, in your usual voice and length. Lead with the answer.",
          "- Use only what the results show. Name the source page or site for every figure. "
          "If a site blocked or failed, say so in a clause.",
          "- Never paste a report and never describe workers, tools, routes, steps or "
          "retries unless something failed and they need to know.",
          "- Do not comment on how this result reached you, on routing or delivery, or "
          "on whether the pipeline is working — even if earlier conversation was about "
          "that. Answer the message and stop.",
          "- Full report files (.txt) are attached to your reply automatically; don't "
          "list file names. Screenshots listed under files are NOT sent unless you "
          "call attach_file with the name — do that when the user asked for a "
          "picture or one makes the answer clearer, and only then say it's attached.",
          (f"- If the results don't answer it and a specific further attempt could, call "
           f"instruct_worker ({followups_left} left for this request); your text is then "
           f"not sent. Otherwise say plainly what's missing."
           if followups_left > 0 else
           "- No further attempts are available for this request: answer with what you "
           "have and say plainly what's missing."),
          "- If the answer includes a price, a share quote or a due date, call "
          "record_observation once per figure with its source page, so it can be "
          "answered from file later.",
          "- You may check state with the read-only tools before answering."]
    return "\n".join(L)


async def answer_deferred(session, turn, draft=""):
    """Wait for a deferred turn's workers, run the result turn, send the one
    reply. Runs as its own task; adopts the original turn so the send gate
    counts this as that message's reply."""
    from skills import send_gate, workers, ledger, worklog
    import skills.history as _hist
    tok = send_gate.adopt(turn)
    owner = turn.sender
    name = USERS.get(owner, {}).get("name", owner)
    call_logger.set_trace(call_logger.new_trace_id(), name)
    answer, tag, files = "", TAGS["chip"], []
    try:
        tier = get_sticky_tier(owner) or USERS.get(owner, {}).get("default_model", "cloud_default")
        for _round in range(1 + ledger.MAX_FOLLOWUPS):
            wids, turn.workers = list(turn.workers), []
            if not wids:
                break
            results = await workers.join(wids)
            for r in results:
                files += [f for f in (r.get("files") or [])
                          if f.endswith(".txt") and os.path.exists(f)]
            left = ledger.MAX_FOLLOWUPS - ledger.followups_used(turn.request_id)
            prompt = _result_turn_prompt(turn, draft, results, left)
            worklog.log("result_turn", turn=turn.id, request=turn.request_id,
                        workers=[r["id"] for r in results],
                        statuses=[r["status"] for r in results])
            history = load_history(owner, USERS.get(owner, {}).get("history", 30))
            if history and history[-1]["role"] == "user":
                history = history[:-1]        # the prompt quotes it
            answer, tag = await ask_cloud(
                session, prompt, owner, tier=tier, history=history, sections=[],
                length="default", allowed_tools=tool_calling.RESULT_TURN_TOOLS)
            if turn.workers:                  # it sent work back out
                draft = answer
                continue
            break
        if not answer or answer == EMPTY_REPLY:
            answer = ("I couldn't put an answer together from what came back. "
                      "The run records are saved if you want me to look again.")
        await deliver_response(session, answer, owner, tag=tag)
        for f in files[:2]:
            await send_signal_file(session, f, f"{TAGS['chip']} Full report", owner)
        for f in (turn.attachments or [])[:3]:
            # Pictures the result turn chose to send (attach_file), checked by
            # code to be files this request's workers produced.
            await send_signal_file(session, f, "", owner)
        log_to_buffer(owner, turn.text, answer, history_roles=("assistant",))
        asyncio.create_task(maybe_extract_facts(owner, turn.text, answer))
        _tname = name.lower()
        taxonomy.enqueue_work(kind="tag_extract", sender=owner, name=_tname,
                              user_text=turn.text, response_text=answer, source="text")
        asyncio.create_task(asyncio.to_thread(_hist.maybe_summarize, _tname))
        if turn.ts:
            await send_reaction(owner, turn.ts, "✅")
    except Exception as e:
        print(f"⚠️ deferred answer failed: {e}")
        import traceback; traceback.print_exc()
        await send_signal(session, f"{TAGS['chip']} " + fail_line("Putting that answer together", e),
                          recipient=owner)
        if turn.ts:
            await send_reaction(owner, turn.ts, "❌")
    finally:
        try:
            send_gate._turn.reset(tok)
        except Exception:
            pass


async def worker(session):
    print("👷 Worker online.")
    while True:
        packet     = await message_queue.get()
        sender     = packet["sender"]
        text       = packet["text"]
        attachment = packet.get("attachment")
        ts         = packet.get("ts", "0")
        name       = USERS.get(sender, {}).get("name", sender)
        call_logger.set_trace(call_logger.new_trace_id(), name)
        call_logger.log_signal_in()
        print(f"🧠 [{name}] {text[:60]}")
        # One in, one out: everything this message causes to be sent is
        # attributed to this turn by the send gate (skills/send_gate.py).
        from skills import send_gate as _gate
        _gate_tok = _gate.begin(sender, ts, internal=packet.get("internal"),
                                text=text)
        asyncio.create_task(send_typing(sender))  # phone shows life immediately
        _reacted = False
        _notices_folded = False

        try:
            # VPN fail-closed override (audit Phase 0 step 6). Admin-only, and
            # deliberately checked before routing so it still works while
            # outbound web lookups are refusing.
            if text.strip().lower() in ("home ok", "home ok.", "homeok"):
                if USERS.get(sender, {}).get("tier") == "admin":
                    ok = _set_home_ok()
                    msg = ("Home routing allowed for 30 minutes."
                           if ok else "Couldn't set the override flag.")
                else:
                    msg = "⛔ Admin only."
                await send_signal(session, f"{TAGS['chip']} {msg}", recipient=sender)
                message_queue.task_done()
                continue

            # A worker is waiting on this person (a CAPTCHA picture, a login
            # decision — One Voice spec C.8). Chip decides whether this
            # message is the answer: if it is, it goes to the waiting worker
            # and Chip says so in one line; if not, it is handled normally and
            # the worker keeps waiting. (Replaces the noVNC 'resume' flow.)
            if not packet.get("internal"):
                from skills import asks as _asks
                _ask = _asks.pending(sender)
                if _ask is not None and await _ask_answer(_ask, text):
                    _asks.answer(sender, text.strip())
                    await send_signal(session, f"{TAGS['chip']} Got it — entering that now.",
                                      recipient=sender)
                    log_to_buffer(sender, text, "[answer passed to the waiting browser task]")
                    message_queue.task_done()
                    continue

            # Plain-language effort control (the admin, 19 Nov 2026):
            # "think hard: <q>" → max for this turn + 5 sticky turns;
            # "quick: <q>" → low. Admin Gemini users escalate to Opus on max
            # so the effort actually lands somewhere that honors it.
            for _pat, _lvl in EFFORT_TRIGGERS:
                _m = _pat.match(text or "")
                if _m:
                    set_effort_sticky(sender, _lvl)
                    if (_lvl == "max"
                            and "gemini" in MODELS.get(USERS.get(sender, {}).get(
                                "default_model", "cloud_default"), "")
                            and USERS.get(sender, {}).get("tier") == "admin"
                            and not get_sticky_tier(sender)):
                        set_sticky_tier(sender, "cloud_heavy")
                    remainder = text[_m.end():].strip()
                    if remainder:
                        text = remainder
                        packet["text"] = text
                    else:
                        await send_signal(session,
                            f"{TAGS['chip']} Effort set to {_lvl} for the next "
                            f"{EFFORT_STICKY_TURNS} turns.", recipient=sender)
                        log_to_buffer(sender, packet.get('text', ''), f"[effort → {_lvl}]")
                        message_queue.task_done()
                        text = None
                    break
            if text is None:
                continue

            # Multi-attachment batch (e.g. 3 photos sent together)
            attachments_batch = packet.get("attachments")
            if attachments_batch:
                await media_dispatch_batch(session, sender, text, attachments_batch, ts)
                message_queue.task_done()
                continue

            if attachment:
                await media_dispatch(session, sender, text, attachment, ts)
                message_queue.task_done()
                continue

            from skills.audio_id import is_audio_trigger
            if is_audio_trigger(text, sender):
                history = load_history(sender, USERS.get(sender, {}).get("history", 30))
                try:
                    answer, force_file = await SKILL_REGISTRY["audio_id"](
                        session, sender, text, "", history)
                    await deliver_response(session, answer, sender,
                                           tag=TAGS["cloud_vision"], force_file=force_file)
                except Exception as e:
                    await deliver_response(session, fail_line("The audio step", e), sender, tag=TAGS["chip"])
                log_to_buffer(sender, text, "")
                message_queue.task_done()
                continue

            pipeline   = parse_pipeline(text)
            stages     = pipeline["stages"]
            deliver_to = pipeline["deliver_to"]

            # Long-running pipeline? React ⏳ to the triggering message so the
            # user knows it's cooking; ✅/❌ replaces it on completion.
            _LONG = {"analyze", "research", "chk", "archive", "rechunk", "memory"}
            if any((st.get("cmd") or "") in _LONG for st in stages):
                await send_reaction(sender, ts, "⏳")
                _reacted = True

            dest_phone = None
            cc_sender  = None
            if deliver_to and deliver_to != "__facts__":
                for phone, info in USERS.items():
                    if info["name"].lower() == deliver_to.lower():
                        dest_phone = phone
                        cc_sender  = sender
                        break

            history          = load_history(sender, USERS.get(sender, {}).get("history", 30))
            pipeline_context = ""
            final_answer     = ""
            final_tag        = TAGS["chip"]
            delivered_inline = False
            skip_facts       = False  # set True when skill returns force_file=True

            for stage in stages:
                cmd      = stage["cmd"]
                flags    = stage["flags"]
                payload  = stage["payload"]
                length   = "verbose" if "-v" in flags else "concise" if "-c" in flags else "default"
                debug    = "-d" in flags

                # Inject pipeline context (skip for shell commands)
                if pipeline_context and cmd not in ("ls", "ps", "cat", "facts"):
                    payload = (
                        f"{payload}\n\nCONTEXT FROM PREVIOUS STEP:\n{pipeline_context}".strip()
                        if payload else pipeline_context
                    )

                cmd_type = COMMAND_MAP.get(cmd, (None, None))[0] if cmd else None
                cmd_name = COMMAND_MAP.get(cmd, (None, None))[1] if cmd else None

                # Audit H2 — single tier gate covering every command type.
                if cmd:
                    _deny = _denied_reason(cmd, payload, sender)
                    if _deny:
                        await send_signal(session, _deny, recipient=sender)
                        final_answer = _deny
                        final_tag    = TAGS["chip"]
                        delivered_inline = True
                        break

                # Shell
                if cmd_type == "shell":
                    result = await handle_shell(session, sender, cmd_name or cmd, payload)
                    pipeline_context = result or ""
                    final_answer     = result or ""
                    final_tag        = TAGS["chip"]
                    delivered_inline = True
                    continue

                # Vault
                if cmd_type == "vault":
                    domain = None
                    if pipeline_context.strip() in (
                            chip_config.PREFIX_DOMAINS + ["unclassified"]):
                        domain           = pipeline_context.strip()
                        pipeline_context = ""
                    result, tag = await handle_vault(session, sender, payload, domain=domain)
                    if len(stages) == 1 and not deliver_to:
                        await deliver_response(session, result, sender, tag=tag)
                        log_to_buffer(sender, text, result)
                        delivered_inline = True
                    pipeline_context = result
                    final_answer     = result
                    final_tag        = tag
                    continue

                # Model
                if cmd_type == "model":
                    tier = cmd_name
                    set_sticky_tier(sender, tier)  # persist for MODEL_STICKY_TURNS turns
                    if tier == "local":
                        await send_signal(session,
                            f"{TAGS['local']} Local model — may take 60-120s...", recipient=sender)
                        try:
                            async with session.post(
                                f"{OLLAMA_URL}/api/generate",
                                json={"model": MODELS["router"], "prompt": payload, "stream": False},
                                timeout=aiohttp.ClientTimeout(total=120),
                            ) as resp:
                                data   = await resp.json(content_type=None)
                                answer = data.get("response", "").strip() or "[No response]"
                        except Exception as e:
                            answer = fail_line("The local model", e)
                        pipeline_context = answer
                        final_answer     = answer
                        final_tag        = TAGS["local"]
                    else:
                        # Check if previous stage was a file fetch
                        if pipeline_context and isinstance(pipeline_context, str) and pipeline_context.startswith("__file__"):
                            file_path = pipeline_context[8:]
                            if tier != "cloud_heavy":
                                await send_signal(session,
                                    f"{TAGS['chip']} PDF pipe only supported with s: (Claude).",
                                    recipient=sender)
                                delivered_inline = True
                                break
                            answer, tag = await ask_cloud_with_pdf(
                                session, payload or "Analyze this document.", sender,
                                file_path, history=history, length=length)
                        else:
                            rag_ctx, _, _ = await asyncio.to_thread(
                                get_rag_context, payload, None, sender)
                            rag_facts     = retrieve_facts_rag(payload, sender=sender)
                            answer, tag   = await ask_cloud(
                                session, payload, sender, tier=tier,
                                rag_context=rag_ctx or None, rag_facts=rag_facts or None,
                                history=history, length=length,
                            )
                        pipeline_context = answer
                        final_answer     = answer
                        final_tag        = tag
                    continue

                # Skill
                if cmd_type == "skill":
                    skill_key = cmd_name
                    if skill_key not in SKILL_REGISTRY:
                        final_answer = f"Skill '{skill_key}' not loaded."
                        final_tag    = TAGS["chip"]
                        continue
                    if skill_key in RESTRICTED and USERS.get(sender, {}).get("tier") != "admin":
                        await send_signal(session, "⛔ Admin only.", recipient=sender)
                        break
                    rag_ctx, _, _ = await asyncio.to_thread(
                        get_rag_context, payload, None, sender)
                    try:
                        answer, force_file = await SKILL_REGISTRY[skill_key](
                            session, sender, payload, rag_ctx, history)
                        pipeline_context = answer
                        final_answer     = answer
                        final_tag        = TAGS["cloud_heavy"]
                        if force_file == "direct":
                            CHUNK = 1500
                            remaining = answer
                            while remaining:
                                if len(remaining) <= CHUNK:
                                    await send_signal(session, remaining, recipient=sender)
                                    break
                                split = remaining.rfind('\n', 0, CHUNK)
                                if split == -1:
                                    split = CHUNK
                                await send_signal(session, remaining[:split], recipient=sender)
                                remaining = remaining[split:].lstrip('\n')
                            log_to_buffer(sender, text, answer)
                            delivered_inline = True
                            break
                        if force_file:
                            skip_facts = True
                        if force_file and len(stages) == 1 and not deliver_to:
                            await deliver_response(session, answer, sender,
                                                   tag=final_tag, force_file=True)
                            log_to_buffer(sender, text, answer)
                            delivered_inline = True
                            break
                    except Exception as e:
                        final_answer = fail_line("That skill", e)
                        final_tag    = TAGS["chip"]
                    continue

                # Builtin
                if cmd_type == "builtin":
                    bn = cmd_name
                    if bn in RESTRICTED and USERS.get(sender, {}).get("tier") != "admin":
                        await send_signal(session, "⛔ Admin only.", recipient=sender)
                        break
                    if   bn == "browse":
                        from skills import browse as _browse
                        _task = (payload or "").strip()
                        _via  = None
                        _m = re.search(r"\s+via\s+(mullvad|vpn|home)\s*$", _task, re.I)
                        if _m:
                            _via, _task = _m.group(1).lower(), _task[:_m.start()].strip()
                        if not _task:
                            await send_signal(session,
                                f"{TAGS['chip']} Usage: browse: <task> [via vpn|home]",
                                recipient=sender)
                        else:
                            # 'via home' exposes the residential IP for this one
                            # session (scoped — the rest of the box stays on
                            # Mullvad). It no longer needs a whole-box
                            # 'mullvad: off', but it is admin-only: the audit
                            # trail has to name someone accountable.
                            if _via == "home" and USERS.get(sender, {}).get("tier") != "admin":
                                await send_signal(session,
                                    f"{TAGS['chip']} Home routing is admin-only.",
                                    recipient=sender)
                            else:
                                # A worker like any other (One Voice spec B):
                                # the turn defers below and the result turn
                                # sends the one reply.
                                from skills import workers as _workers
                                _workers.launch(session, "browse", _task,
                                                via=_via)
                        delivered_inline = True
                        break
                    elif bn == "mullvad":
                        _arg = (payload or "").strip().lower()
                        if _arg.startswith("off"):
                            _mins = 30
                            _m = re.search(r"off\s+(\d+)", _arg)
                            if _m:
                                _mins = min(int(_m.group(1)), 240)   # spec: max 4h
                            import subprocess as _sp
                            _sp.run(["sudo", "tailscale", "set", "--exit-node="],
                                    check=True, timeout=30)
                            os.makedirs("/run/chip", exist_ok=True)
                            from datetime import timedelta as _td
                            open("/run/chip/mullvad_off", "w").write(
                                (datetime.now() + _td(minutes=_mins)).isoformat())
                            _resp = (f"Mullvad OFF for {_mins} min — traffic is on "
                                     f"the home IP. Auto re-enables at expiry.")
                        elif _arg.startswith("on"):
                            try:
                                os.remove("/run/chip/mullvad_off")
                            except FileNotFoundError:
                                pass
                            from skills.rotate_exit import rotate_node as _rn
                            await asyncio.to_thread(_rn)
                            _resp = "Mullvad back ON (exit node rotated)."
                        else:
                            from skills.browse import exit_node_label, current_route
                            _node = exit_node_label()
                            _exp  = ""
                            if os.path.exists("/run/chip/mullvad_off"):
                                _exp = f" (deliberate off until {open('/run/chip/mullvad_off').read()[:16]})"
                            _resp = (f"Route: {current_route()}"
                                     f"{' via ' + _node if _node else ''}{_exp}")
                        await send_signal(session, f"{TAGS['chip']} {_resp}",
                                          recipient=sender)
                        delivered_inline = True
                        break
                    elif bn == "fix":
                        from skills import self_heal
                        _arg = (payload or "").strip()
                        # "approve", "approve FIX-0003", "reject", "restore FIX-0003".
                        # The verb is the first word; anything after it is the id.
                        _parts = _arg.split(None, 1)
                        _verb  = _parts[0].lower() if _parts else ""
                        _id    = _parts[1].strip() if len(_parts) > 1 else None
                        _bare  = _id is None
                        if _verb in ("approve", "reject", "restore") and packet.get("internal"):
                            _resp = (f"{_verb.capitalize()} has to be typed by an admin - a "
                                     "scheduled follow-up cannot decide a job.")
                        elif _verb == "approve":
                            _ov = bool(_id) and _id.lower().endswith("override")
                            _aid = _id[: -len("override")].strip() if _ov else _id
                            _resp = self_heal.approve(_aid or None, override=_ov)
                        elif _verb == "reject":
                            _resp = self_heal.reject(_id)
                        elif _verb == "restore":
                            _resp = self_heal.restore(_id)
                        elif _verb in ("", "status", "list") and _bare:
                            _resp = self_heal.status()
                        elif _verb in ("status", "show") and re.fullmatch(r"FIX-\d+", _id or "", re.I):
                            # 'fix: status FIX-0007' used to fall through to the
                            # new-order branch and stage "status FIX-0007" as a job.
                            _resp = self_heal.detail(_id)
                        else:
                            # Anything else is a new work order. "supersedes FIX-0002:"
                            # at the front retargets it over an existing record.
                            _sup = _cont = None
                            _m = re.match(r"^supersedes?\s+(FIX-\d+)\s*[:,]\s*(.+)$",
                                          _arg, re.I | re.S)
                            if _m:
                                _sup, _arg = _m.group(1).upper(), _m.group(2).strip()
                            _m = re.match(r"^continue\s+(FIX-\d+)\s*[:,]\s*(.+)$",
                                          _arg, re.I | re.S)
                            if _m:
                                _cont, _arg = _m.group(1).upper(), _m.group(2).strip()
                            try:
                                _, _resp = self_heal.stage(_arg, staged_by=name,
                                                           supersedes=_sup,
                                                           continue_of=_cont)
                            except Exception as _e:
                                _resp = f"Could not stage that: {_e}"
                        await send_signal(session, f"{TAGS['chip']} {_resp}",
                                          recipient=sender)
                        delivered_inline = True
                        break
                    elif bn == "build":
                        # The build track (One Voice spec D.2): a new skill,
                        # files added only. Typed by an admin; approval is
                        # still a separate 'fix: approve' — except continuing
                        # an approved build, which this typed command is.
                        from skills import self_heal
                        _arg = (payload or "").strip()
                        _m = re.match(r"^continue\s+(FIX-\d+)\s*[:,]\s*(.+)$", _arg, re.I | re.S)
                        try:
                            if packet.get("internal"):
                                _resp = "A build has to be typed by an admin."
                            elif _m:
                                _cid = _m.group(1).upper()
                                info, _resp = self_heal.stage(_m.group(2).strip(), staged_by=name,
                                                              track="build", continue_of=_cid)
                                _resp = self_heal.approve(info["fix_id"])
                            else:
                                _m2 = re.match(r"^([A-Za-z][\w-]{1,40})\s*:\s*(.+)$", _arg, re.S)
                                if not _m2:
                                    _resp = "Usage: build: <skill_name>: <what it should do>"
                                else:
                                    _, _resp = self_heal.stage(
                                        _m2.group(2).strip(), staged_by=name, track="build",
                                        skill_name=_m2.group(1).replace("-", "_"))
                        except Exception as _e:
                            _resp = f"Could not stage that: {_e}"
                        await send_signal(session, f"{TAGS['chip']} {_resp}", recipient=sender)
                        delivered_inline = True
                        break
                    elif bn == "skill":
                        from skills import skill_loader
                        _parts = (payload or "").strip().split()
                        _verb = _parts[0].lower() if _parts else "list"
                        if _verb in ("off", "disable") and len(_parts) > 1:
                            _resp = skill_loader.set_enabled(_parts[1], False)
                        elif _verb in ("on", "enable") and len(_parts) > 1:
                            _resp = skill_loader.set_enabled(_parts[1], True)
                        else:
                            _resp = skill_loader.listing()
                        await send_signal(session, f"{TAGS['chip']} {_resp}", recipient=sender)
                        delivered_inline = True
                        break
                    elif bn == "effort":
                        _word = (payload or "").strip().lower().split()[0] if (payload or "").strip() else ""
                        _lvl  = EFFORT_WORDS.get(_word)
                        if not _lvl:
                            await send_signal(session,
                                f"{TAGS['chip']} Usage: effort: max|xhigh|high|medium|low "
                                f"(or: hard, quick, reset). Current: {get_effort(sender)}.",
                                recipient=sender)
                        else:
                            set_effort_sticky(sender, _lvl)
                            _note = ("back to default" if _lvl == DEFAULT_EFFORT
                                     else f"{_lvl} for the next {EFFORT_STICKY_TURNS} turns")
                            await send_signal(session,
                                f"{TAGS['chip']} Thinking effort {_note}.", recipient=sender)
                        delivered_inline = True
                        break
                    elif bn == "voice":   await handle_voice(session, sender, payload)
                    elif bn == "save":    await handle_save(session, sender, payload.strip().lower())
                    elif bn == "skip":    await handle_skip(session, sender)
                    elif bn == "stop":
                        cancel_flags[sender] = True
                        await send_signal(session, f"{TAGS['chip']} Stop sent.", recipient=sender)
                    elif bn == "wx":      await handle_wx(session, sender, payload)
                    elif bn == "archive": await handle_archive(session, sender, payload)
                    elif bn == "forget":  await handle_forget(session, sender, payload)
                    elif bn == "memory":  await handle_memory(session, sender, payload)
                    elif bn == "rechunk": await handle_rechunk(session, sender, payload)
                    elif bn == "fetch":
                        result = await handle_fetch(session, sender, payload)
                        if result and len(stages) > 1:
                            found_path, best_fname, best_domain = result
                            pipeline_context = f"__file__{found_path}"
                            final_answer     = f"__file__{found_path}"
                            final_tag        = TAGS["chip"]
                            continue
                        elif result:
                            found_path, best_fname, best_domain = result
                            await send_signal_file(session, found_path,
                                f"{TAGS['chip']} {best_fname}  [{best_domain}]", sender)
                            log_to_buffer(sender, text, f"[sent file: {best_fname}]")
                        # Always mark delivered — whether file found or not found msg was sent
                        delivered_inline = True
                        break
                    elif bn == "help":    await send_signal(session, get_help_text(sender), recipient=sender)
                    if bn != "fetch":
                        log_to_buffer(sender, text, "")
                        delivered_inline = True
                        break

                # Semantic router — check sticky model first
                if cmd is None:
                    sticky = get_sticky_tier(sender)
                    if sticky:
                        # Use persisted model, skip router
                        rag_ctx, _, _ = await asyncio.to_thread(
                            get_rag_context, payload, None, sender)
                        rag_facts     = retrieve_facts_rag(payload, sender=sender)
                        answer, tag   = await ask_cloud(
                            session, payload, sender, tier=sticky,
                            rag_context=rag_ctx or None, rag_facts=rag_facts or None,
                            history=history, length=length,
                        )
                        decrement_sticky(sender)
                        expired = not get_sticky_tier(sender)
                        if expired:
                            _def_tier = USERS.get(sender, {}).get("default_model", "cloud_default")
                            _def_tag   = TAGS.get(_def_tier, TAGS["cloud_default"])
                            _gate.add_note(f"Back to the default model {_def_tag}.")
                    else:
                        try:
                            answer, tag = await semantic_dispatch(
                                session, sender, payload, history, length, pipeline_context)
                        except Exception as route_err:
                            print(f"⚠️ Semantic dispatch error: {route_err}")
                            answer, tag = await ask_cloud(
                                session, payload, sender, tier="cloud_default",
                                history=history, length=length, fallback=True)
                    pipeline_context = answer
                    final_answer     = answer
                    final_tag        = tag
                    continue

                final_answer = f"Unknown command '{cmd}'. Try help:"
                final_tag    = TAGS["chip"]

            # Deferred turn (One Voice spec B.2): this message started workers,
            # so nothing is sent now. ⏳ stays on the message; the user's turn
            # goes into history now (so a follow-up message keeps its order);
            # answer_deferred sends the ONE reply when the workers are done.
            _dt = _gate.raw()
            if _dt is not None and _dt.workers:
                _dt.deferred = True
                await working()
                try:
                    import skills.history as _hist_d
                    _hist_d.log_turn(name.lower(), "user", text)
                except Exception as _he:
                    print(f"⚠️ history (deferred user turn): {_he}")
                asyncio.create_task(answer_deferred(
                    session, _dt, "" if delivered_inline else (final_answer or "")))
                print(f"⏳ turn {_dt.id} deferred on {len(_dt.workers)} worker(s)")
                final_answer, delivered_inline = None, True

            # Deliver
            if final_answer and not delivered_inline:
                # Fold end-of-turn notices into this reply (see below).
                _notices_folded = True
                if decrement_voice(sender):
                    _gate.add_note("Voice reset to default.")
                if decrement_effort(sender):
                    _gate.add_note(f"Thinking effort back to {DEFAULT_EFFORT}.")
                # File sentinel — send as attachment directly, no length truncation
                if isinstance(final_answer, str) and final_answer.startswith("__file__"):
                    file_path = final_answer[8:]
                    fname     = os.path.basename(file_path)
                    if os.path.exists(file_path):
                        await send_signal_file(session, file_path,
                                               f"{TAGS['chip']} {fname}", sender)
                        log_to_buffer(sender, text, f"[sent file: {fname}]")
                    else:
                        await send_signal(session,
                            f"{TAGS['chip']} File not found: {fname}", recipient=sender)
                elif deliver_to == "__facts__":
                    _backup_context_db()
                    conn = sqlite3.connect(CONTEXT_DB)
                    conn.execute(
                        "INSERT INTO facts (content, confirmed, created_at, user) "
                        "VALUES (?, 1, ?, ?)",
                        (final_answer[:500], datetime.now().isoformat(),
                         USERS.get(sender, {}).get("name", chip_config.ADMIN_NAME).lower())
                    )
                    conn.commit()
                    conn.close()
                    await send_signal(session, f"{TAGS['chip']} Saved to facts.", recipient=sender)
                elif dest_phone:
                    await deliver_response(session, final_answer, dest_phone,
                                           tag=final_tag, cc_sender=cc_sender)
                else:
                    await deliver_response(session, final_answer, sender, tag=final_tag)

                log_to_buffer(sender, text, final_answer)
                # Real-time fact extraction — skip for report outputs (force_file skills)
                if not skip_facts:
                    asyncio.create_task(maybe_extract_facts(sender, text, final_answer))
                    _tax_name = USERS.get(sender, {}).get("name", "").lower()
                    if _tax_name:
                        taxonomy.enqueue_work(kind="tag_extract", sender=sender, name=_tax_name,
                                               user_text=text, response_text=final_answer, source="text")
                        # fold older turns into the rolling summary off-loop
                        import skills.history as _hist
                        asyncio.create_task(
                            asyncio.to_thread(_hist.maybe_summarize, _tax_name))

            # End-of-turn notices: counted here so the turn counters still
            # tick, but never sent as a message of their own any more (one in,
            # one out). They were folded into the reply by _fold_turn_notices
            # before delivery when there was a reply to fold them into.
            if not _notices_folded:
                if decrement_voice(sender):
                    _gate.add_note("Voice reset to default.")
                if decrement_effort(sender):
                    _gate.add_note(f"Thinking effort back to {DEFAULT_EFFORT}.")
            _t = _gate.raw()
            if (_reacted or (_t is not None and _t.reacted)) and not (_t and _t.deferred):
                await send_reaction(sender, ts, "✅")

        except Exception as e:
            print(f"⚠️ Worker error: {e}")
            import traceback; traceback.print_exc()
            _t = _gate.raw()
            if _reacted or (_t is not None and _t.reacted):
                await send_reaction(sender, ts, "❌")
            # One plain line; the exception itself stays in the log.
            await send_signal(session,
                f"{TAGS['chip']} That failed on my end ({type(e).__name__}). "
                f"It's in the log.", recipient=sender)
        finally:
            _gate.end(_gate_tok)

        message_queue.task_done()

# ─── Listener ─────────────────────────────────────────────────────────────────

# ─── Multi-attachment debounce ────────────────────────────────────────────────
# Signal sometimes delivers a multi-photo send as several envelopes sharing
# the same logical timestamp instead of one envelope with N attachments.
# We accumulate attachments for a short window before dispatching, so a
# 3-photo send produces ONE vision call instead of 1-3 fragmented ones.

BATCH_DEBOUNCE_SECONDS = 1.5
_pending_batches        = {}   # (sender, ts) -> {"text": str, "attachments": [...]}
_seen_attachment_ids    = set()

async def _flush_attachment_batch(key, session):
    await asyncio.sleep(BATCH_DEBOUNCE_SECONDS)
    entry = _pending_batches.pop(key, None)
    if not entry:
        return
    sender, ts = key
    atts = entry["attachments"]
    if len(atts) == 1:
        await message_queue.put({
            "sender": sender, "text": entry["text"],
            "attachment": atts[0], "ts": ts,
        })
    else:
        await message_queue.put({
            "sender": sender, "text": entry["text"],
            "attachments": atts, "ts": ts,
        })
        print(f"📦 Batched {len(atts)} attachments for {sender[-4:]}")

async def listen(session):
    seen = set()
    print("🟢 Chip online.")

    while True:
        try:
            reader, writer = await asyncio.open_unix_connection(SIGNAL_SOCKET)
            print("📡 Connected to signal-cli daemon")

            # Subscribe to incoming message notifications
            sub = json.dumps({
                "jsonrpc": "2.0", "method": "subscribeReceive",
                "params": {}, "id": 0
            }) + "\n"
            writer.write(sub.encode())
            await writer.drain()
            await asyncio.wait_for(reader.readline(), timeout=10)  # ack

            while True:
                line = await reader.readline()
                if not line:
                    print("📡 Signal daemon disconnected — reconnecting in 5s")
                    break

                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue

                if msg.get("method") != "receive":
                    continue

                params      = msg.get("params", {})
                envelope    = params.get("envelope", {})
                ts          = envelope.get("timestamp")
                sender      = (envelope.get("sourceNumber")
                               or envelope.get("source", ""))
                data_msg    = envelope.get("dataMessage") or {}
                text        = (data_msg.get("message") or "").strip()
                attachments = data_msg.get("attachments", [])

                if not text and not attachments:
                    continue
                # Text-only envelopes: dedup on ts as before (covers reconnect
                # replays). Attachment-bearing envelopes are deduped at the
                # individual attachment-id level below, since sibling envelopes
                # for one multi-photo send legitimately share this timestamp.
                if not attachments:
                    if ts in seen:
                        continue
                    seen.add(ts)
                    if len(seen) > 1000:
                        seen.pop()

                if sender not in USERS:
                    print(f"🚫 Rejected: {sender}")
                    continue

                name = USERS[sender]["name"]
                print(f"📨 {name}: {text[:60]}")

                t = text.lower().strip()
                if t in ("help", "help:", "/help", "!help"):
                    await send_signal(session, get_help_text(sender), recipient=sender)
                    continue

                if attachments:
                    # Filter out attachments we've already batched (reconnect replay)
                    new_atts = [a for a in attachments
                                if a.get("id") not in _seen_attachment_ids]
                    for a in new_atts:
                        _seen_attachment_ids.add(a.get("id"))
                    if len(_seen_attachment_ids) > 2000:
                        _seen_attachment_ids.clear()

                    if not new_atts:
                        continue

                    key = (sender, ts)
                    if key in _pending_batches:
                        _pending_batches[key]["attachments"].extend(new_atts)
                        if not _pending_batches[key]["text"] and text:
                            _pending_batches[key]["text"] = text
                    else:
                        _pending_batches[key] = {"text": text or "", "attachments": list(new_atts)}
                        asyncio.create_task(_flush_attachment_batch(key, session))
                    # Don't add ts to `seen` here — sibling envelopes for the
                    # same logical send legitimately share this timestamp.
                    continue

                try:  # inbound over Signal → replies go back over Signal
                    from skills.connectors import discord_relay as _dr
                    _dr.PREFER[sender] = "signal"
                except Exception:
                    pass
                await message_queue.put({"sender": sender, "text": text, "ts": ts})

            writer.close()
            try: await writer.wait_closed()
            except Exception: pass

        except Exception as e:
            print(f"📡 Signal error [{type(e).__name__}]: {e}")

        await asyncio.sleep(5)

# ─── Reminder checker ─────────────────────────────────────────────────────────

async def reminder_checker(session):
    from skills.remind import load_reminders, modify_reminders
    from datetime import timedelta
    print("⏰ Reminder checker online.")
    while True:
        await asyncio.sleep(60)
        # The outbox (One Voice spec A.3): background scripts' events. Urgent
        # ones go to the admin as ONE message per beat; routine ones wait for
        # the nightly summary. The heartbeat tells those scripts Chip is
        # consuming; if it goes stale they send urgent events directly.
        try:
            from skills import outbox
            outbox.beat()
            _urgent = outbox.drain()
            if _urgent:
                await send_signal(session, f"{TAGS['chip']} {outbox.compose(_urgent)}",
                                  recipient=chip_config.ADMIN_PHONE, kind="alert")
        except Exception as e:
            print(f"⚠️ Outbox drain error: {e}")
        try:
            now = datetime.now()
            due = [r for r in load_reminders()
                   if not r.get("fired")
                   and not r.get("cancelled")
                   and now >= datetime.fromisoformat(r["fire_at"])]
            for r in due:
                if r.get("type") == "followup":
                    # Proactive follow-up (19 Nov 2026): re-enter the worker
                    # loop as a message WITH TOOLS, so Chip can check weather,
                    # vault, web etc. before messaging the user — instead of
                    # parroting a canned reminder line.
                    await message_queue.put({
                        "sender": r["recipient_phone"],
                        "internal": True,
                        "text": ("[Scheduled follow-up — you set this for "
                                 "yourself. Act on the note, using tools if "
                                 "helpful, then message the user naturally. "
                                 f"Note: {r.get('context', r['message'])}]"),
                        "ts": str(int(now.timestamp() * 1000)),
                    })
                    print(f"🔁 Follow-up re-queued → {r['recipient_name']}: "
                          f"{r.get('context', '')[:60]}")
                else:
                    await send_signal(session, r["message"],
                                      recipient=r["recipient_phone"], kind="scheduled")
                    print(f"⏰ Fired → {r['recipient_name']}: {r['message'][:60]}")

            if due:
                fired_keys = {(r["fire_at"], r["recipient_phone"], r["message"])
                              for r in due}
                cutoff = (now - timedelta(days=7)).isoformat()

                def _mark(rs):
                    for r in rs:
                        if ((r["fire_at"], r["recipient_phone"], r["message"])
                                in fired_keys):
                            r["fired"] = True
                    return [r for r in rs
                            if not (r.get("fired") or r.get("cancelled"))
                            or r["fire_at"] > cutoff]
                modify_reminders(_mark)
        except Exception as e:
            print(f"⚠️ Reminder checker error: {e}")

        # Managed recurring jobs (19 Nov 2026) ride the same 60s beat.
        # A job never runs a command — it re-enters the worker loop as an
        # internal message, exactly like a follow-up reminder. Isolated in
        # its own try so a scheduler fault can never take reminders down.
        try:
            from skills import scheduler
            for job in scheduler.due_and_advance(now=datetime.now()):
                await message_queue.put({
                    "sender": job["target_phone"],
                    "internal": True,
                    "text": ("[Scheduled job — a recurring task you were set "
                             "up to run. Act on the note, using tools if "
                             "helpful, then message the user naturally. "
                             f"Note: {job['note']}]"),
                    "ts": str(int(datetime.now().timestamp() * 1000)),
                })
                print(f"🗓️  Job fired → {job['target_name']}: {job['name']}")
        except Exception as e:
            print(f"⚠️ Scheduled job checker error: {e}")

        # Browse stall detection (FIX-0006 phase 7) rides the same 60s beat —
        # a scheduled check, not a new daemon. Rerouting the terminus only
        # saves reports that get published; a worker that dies publishes
        # nothing, and silence has to be impossible. A run whose manifest
        # heartbeat has stopped advancing is reported through the SAME
        # machine-origin pipe, so a stall notice reaches the user the same way
        # a finished run does. Own try block: this must never take the
        # reminder loop down.
        try:
            from skills import browse_capture, workers as _workers
            for st in browse_capture.find_stalled():
                # Close the manifest first: if anything below throws, the next
                # sweep must not report the same stall again.
                browse_capture.mark_stalled(
                    st["manifest"], f"stall detected after {st['age_s']}s "
                                    f"at step {st['last_step']}")
                # The run belongs to a worker: cancel it, and its result comes
                # back to Chip as `stalled` with what it recorded. The result
                # turn tells the user — one reply, never silence, never a dump.
                if not _workers.cancel_by_run_id(
                        st["run_id"], f"stall: heartbeat stopped for {st['age_s']}s "
                                      f"at step {st['last_step']}"):
                    from skills import worklog as _wl
                    _wl.log("stall_orphan", run_id=st["run_id"], task=st["task"][:200],
                            manifest=st["manifest"])
                print(f"🧟 stalled browse run {st['run_id']} "
                      f"(step {st['last_step']}, {st['age_s']}s stale)")
        except Exception as e:
            print(f"⚠️ Browse stall checker error: {e}")

# ─── Main ─────────────────────────────────────────────────────────────────────

async def _matrix_refresher():
    """Keep the vault embedding matrix warm. _embedding_matrix() is a cheap
    signature check when nothing changed and a ~4s rebuild when ingest ran —
    doing it here means no user ever eats the rebuild on their query."""
    while True:
        try:
            await asyncio.to_thread(_embedding_matrix)
        except Exception as e:
            print(f"⚠️ Matrix refresh error: {e}")
        await asyncio.sleep(600)


async def _ask_answer(ask, text):
    """Is this inbound message the answer to the pending ask? Code first: a
    short reply with no question in it is the answer. Anything longer goes to
    a cheap model for a yes/no. Fails toward 'not the answer' — typing a
    stray message into a site is worse than a worker waiting a bit longer."""
    t = (text or "").strip()
    if not t:
        return False
    if t.lower().strip(".!: ") in ("skip", "stop", "cancel", "no"):
        return True
    # Only a single alphanumeric token (what a CAPTCHA answer looks like) skips
    # the classifier; "hows it going" or "stop:" must not be typed into a site.
    if re.fullmatch(r"[A-Za-z0-9]{2,12}", t):
        return True
    try:
        from skills import llm
        verdict = await asyncio.to_thread(
            llm.gemini_sync,
            f"A browser task paused and asked the user:\n\"{ask.question}\"\n\n"
            f"The user then sent:\n\"{t[:500]}\"\n\n"
            "Is the user's message the answer to that question (for example the "
            "characters shown in a CAPTCHA, or an instruction about that step)? "
            "Reply with exactly yes or no.", model=llm.GLITE, timeout=20)
        return verdict.strip().lower().startswith("yes")
    except Exception as e:
        print(f"⚠️ ask classification failed ({e}) — treating as not the answer")
        return False


RERUN_WITHIN_S = 1800   # a lost request younger than this is run again, once


async def _recover_lost(session, lost_rows):
    """Work that died with the last process (One Voice spec B.4). A request
    lost within RERUN_WITHIN_S that has not already been rerun is started
    again, once, and answered normally; anything else gets one line telling
    its owner, so a restart is never silent."""
    from skills import ledger, send_gate, workers
    await asyncio.sleep(5)                      # let the workers come up
    by_req = {}
    for r in lost_rows:
        by_req.setdefault(r["request_id"], []).append(r)
    for req, rows in by_req.items():
        owner, text, ts = rows[0]["owner"], rows[0]["request_text"] or "", rows[0]["inbound_ts"]
        try:
            age = (datetime.now() - datetime.fromisoformat(rows[0]["started_at"])).total_seconds()
        except Exception:
            age = RERUN_WITHIN_S + 1
        prior_rerun = any(r.get("rerun_of") for r in ledger.for_request(req))
        if owner not in USERS:
            continue
        if age <= RERUN_WITHIN_S and not prior_rerun:
            tok = send_gate.begin(owner, ts, text=text)
            try:
                t = send_gate.raw()
                t.request_id = req
                for r in rows:
                    workers.launch(session, r["kind"], r["task"] or text, owner=owner,
                                   rerun_of=r["id"])
                t.deferred = True
                asyncio.create_task(answer_deferred(session, t, ""))
                print(f"🧰 rerunning request {req} after restart ({len(rows)} worker(s))")
            finally:
                send_gate.end(tok)
        else:
            short = " ".join(text.split())[:80]
            await send_signal(session,
                f"{TAGS['chip']} I restarted while working on \"{short}\" and lost it. "
                f"Send it again if you still want it.", recipient=owner, kind="alert")


async def _startup_model_check(session):
    """Every configured model name, looked up on its own client (One Voice
    build step 1). Free lookups, off the hot path. A name the client does not
    know, or a refused key, is a real defect and the admin hears about it once.
    'Unreachable' is not: the box fails closed when the VPN drops, so that is
    logged and nothing more. Never allowed to take startup down."""
    try:
        from skills import model_check
        results = await asyncio.to_thread(model_check.check_all)
        line = model_check.summary_line(results)
        print(f"🔎 {line}")
        if model_check.problems(results):
            await send_signal(session, f"{TAGS['chip']} Startup check failed — {line}",
                              recipient=chip_config.ADMIN_PHONE, kind="alert")
    except Exception as e:
        print(f"⚠️ startup model check skipped: {e}")


async def main():
    async with aiohttp.ClientSession() as session:
        # phi4 is no longer pinned warm (19 Nov 2026): the LLM-router hop is
        # gone and taxonomy runs on Gemini Flash-Lite, so the only remaining
        # consumer is the explicit m: prefix, which can afford a cold load.
        # That frees ~2.5GB RAM for the embedding model and the OS cache.
        asyncio.create_task(_matrix_refresher())

        # One-time history cutover: import buffer files into the persistent
        # store (no-op once the turns table has rows).
        try:
            import skills.history as _hist
            await asyncio.to_thread(
                _hist.seed_from_buffers,
                [u["name"].lower() for u in USERS.values()])
        except Exception as e:
            print(f"⚠️ history seed skipped: {e}")

        # Discord relay (connector scaffold) — only if token + lib present.
        try:
            from skills.connectors import discord_relay
            if discord_relay.available():
                asyncio.create_task(discord_relay.start(message_queue))
            else:
                print("🎮 Discord relay dormant (no token — see skills/connectors/README.md)")
        except Exception as e:
            print(f"⚠️ Discord relay skipped: {e}")

        # Work that was running when the last process died died with it
        # (workers live inside this process). Mark it lost in the ledger so
        # nothing reports it as still running (One Voice spec B.4).
        try:
            from skills import ledger as _ledger
            _lost = _ledger.mark_lost_on_startup()
            if _lost:
                print(f"🧰 {len(_lost)} worker(s) lost to the restart: "
                      + ", ".join(r["id"] for r in _lost))
                asyncio.create_task(_recover_lost(session, _lost))
        except Exception as e:
            print(f"⚠️ ledger recovery skipped: {e}")

        asyncio.create_task(_startup_model_check(session))
        for _ in range(3):
            asyncio.create_task(worker(session))
        asyncio.create_task(reminder_checker(session))
        asyncio.create_task(taxonomy.taxonomy_heartbeat())
        # audit H5: defined in usage_tracker but never started, so the weekly
        # cost report has never once fired.
        try:
            from skills.usage_tracker import weekly_report_task
            asyncio.create_task(weekly_report_task(session))
        except Exception as e:
            print(f"⚠️ weekly usage reporter failed to start: {e}")

        await listen(session)

if __name__ == "__main__":
    asyncio.run(main())
