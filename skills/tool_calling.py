"""
skills/tool_calling.py
========================
Native tool-calling definitions and execution for Chip's conversational
models (both Claude and Gemini get the same tools on every turn).

Why this is separate from the round-trip loop itself:
The actual back-and-forth with each API (send → check for tool_use/
functionCall → execute → send result back → get final text) is tightly
coupled to each API's specific request/response shape, so that logic stays
inside coordinator.py's ask_cloud(). What's extracted here is everything
that ISN'T API-shape-specific: the tool schemas themselves and what
executing each one actually does.

Dependency injection, not coordinator import:
execute_tool() takes active_voices (a dict) and retrieve_rag_fn (a function)
as parameters rather than importing them from coordinator.py. This avoids a
circular import (coordinator imports this module to get the tools) and
matches the same self-contained pattern skills/taxonomy.py already uses —
dicts are mutable, so passing active_voices in and writing to it here
correctly mutates the same object coordinator.py reads from elsewhere.
"""

import os
import re
import json
import asyncio
import sqlite3
from datetime import datetime, timedelta

import os as _os, sys as _sys
_cc = _os.path.dirname(_os.path.abspath(__file__))
while _cc != "/" and not _os.path.exists(_os.path.join(_cc, "chip_config.py")):
    _cc = _os.path.dirname(_cc)
if _cc not in _sys.path:
    _sys.path.insert(0, _cc)
from chip_config import WORKSPACE
CONTEXT_DB = f"{WORKSPACE}/data/memory/chip_context.db"

TOOL_VISIBILITY_NOTE = (
    "\n\n---\nTOOL CALLS ARE INVISIBLE TO THE USER. Calling a tool only "
    "performs a backend action (saving, adjusting settings, searching) — it "
    "is never shown to the person you're talking to. If something should be "
    "SEEN by them (a compiled recipe, a summary, an answer), it must be in "
    "your visible text reply, in addition to or separate from any tool call. "
    "Never treat a tool call as a substitute for showing someone something."
)

TOOL_DEFS = [
    {
        "name": "remember_fact",
        "description": (
            "Save one or more durable facts to long-term memory. Use when the "
            "user explicitly asks you to remember something, or when something "
            "clearly important and lasting comes up that should persist beyond "
            "this conversation — a decision, a preference, a status change. "
            "Write each fact as a complete, self-contained sentence — it will "
            "be read later with no access to this conversation's context. For "
            "a finalized recipe, use save_recipe instead. For a finished book, "
            "movie, or TV show with a rating, use log_media instead."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "facts": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "One or more durable facts, each a complete standalone sentence.",
                }
            },
            "required": ["facts"],
        },
    },
    {
        "name": "adjust_voice",
        "description": (
            "Adjust your conversational tone or register for the next several "
            "turns. Use when the user explicitly asks you to change how you're "
            "talking — more formal, more casual, drop the humor, be more "
            "clipped, etc."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "modifier": {
                    "type": "string",
                    "description": "Short description of the tone adjustment, e.g. 'more formal, no humor' or 'extremely concise, no elaboration'.",
                },
                "duration_turns": {
                    "type": "integer",
                    "description": "How many turns this should persist. Default 5 if the user doesn't specify.",
                },
            },
            "required": ["modifier"],
        },
    },
    {
        "name": "search_vault",
        "description": (
            "Search the knowledge vault for relevant documents, reports, or "
            "saved content. Use when you need information that might be in "
            "past research, saved documents, or prior analysis that wasn't "
            "already provided in your context."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search query — specific terms work better than vague descriptions.",
                }
            },
            "required": ["query"],
        },
    },
    {
        "name": "search_media",
        "description": (
            "Search the household media catalog — books, movies, and TV "
            "shows that another household user have finished and rated. Use for "
            "any question about what someone has watched, read, rated, or "
            "enjoyed. Leave all filters empty to list the whole catalog. "
            "This catalog is SEPARATE from the document vault — do not use "
            "search_vault for watched/read/rated questions."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Optional title or creator substring to match. Empty string returns everything.",
                },
                "media_type": {
                    "type": "string",
                    "description": "Optional filter: 'book', 'movie', or 'tv'. Empty string for all types.",
                },
                "user": {
                    "type": "string",
                    "description": "Optional filter: a user's lowercase name. Empty string for all users.",
                },
            },
            "required": [],
        },
    },
    {
        "name": "save_recipe",
        "description": (
            "Save a FINAL, complete recipe to the dedicated recipe store. Use "
            "this instead of remember_fact when the user asks you to save or "
            "remember a recipe, cooking technique, or similar structured "
            "ingredients+method content. Only call this for a converged final "
            "version — not exploratory drafts. Still give a normal visible "
            "text reply showing the recipe (or confirming it's saved) — this "
            "tool call alone is not visible to the user."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Recipe title."},
                "ingredients": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Ingredient list, one item per entry.",
                },
                "method": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Method steps, one step per entry, in order.",
                },
                "tags": {
                    "type": "string",
                    "description": "Taxonomy-style tag, e.g. 'culinary-poultry-bresse'.",
                },
            },
            "required": ["title", "ingredients", "method"],
        },
    },
    {
        "name": "log_media",
        "description": (
            "Log a book, movie, or TV show with a rating to the media catalog. "
            "Use when the user mentions finishing or rating something in plain "
            "conversation — 'just finished X, I'd give it an 8', 'watched X "
            "last night, loved it'. This is a SECOND path into the catalog "
            "alongside the screenshot-based one; don't wait for a screenshot "
            "if the user already told you the title and rating directly."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "title":      {"type": "string", "description": "Title of the book/movie/show."},
                "media_type": {"type": "string", "enum": ["book", "movie", "tv"]},
                "creator":    {"type": "string", "description": "Author/director/showrunner, if known. Empty string if not."},
                "rating":     {"type": "number", "description": "Rating out of 10, if given."},
                "notes":      {"type": "string", "description": "Any brief commentary the user gave. Empty string if none."},
            },
            "required": ["title", "media_type"],
        },
    },
    {
        "name": "get_weather",
        "description": (
            "Get current weather conditions and forecast. Use for any "
            "question about the weather, temperature, rain, or outdoor "
            "conditions. Defaults to the home location if none given."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "location": {
                    "type": "string",
                    "description": "Optional location, e.g. 'Chicago, IL'. Empty string for the default (home) location.",
                },
            },
            "required": [],
        },
    },
    {
        "name": "get_usage_report",
        "description": (
            "Get the API usage and cost report with PER-MODEL and PER-USER "
            "breakdowns — calls, tokens, and dollar cost for each model "
            "(Opus 5, Fable 5, Sonnet, Gemini variants). Use when asked "
            "about API spend, what a model costs, who's spending what, or "
            "usage generally."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "period": {
                    "type": "string",
                    "description": "'today' (default), 'month', or a specific YYYY-MM.",
                },
            },
            "required": [],
        },
    },
    {
        "name": "system_health",
        "description": (
            "Get current system vitals for the Pi: CPU load, temperature, "
            "RAM, disk usage, and loaded skills. Use when asked how the "
            "system/Chip is doing, running hot, disk space, etc."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "list_vault",
        "description": (
            "List documents in the knowledge vault. No arguments: summary "
            "of all domains with document/chunk counts. With a domain name "
            "(any domain the no-argument summary lists): "
            "recent documents in that domain. With 'recent': the 10 most "
            "recently ingested documents across all domains. This LISTS "
            "documents — use search_vault to search their contents."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "domain": {
                    "type": "string",
                    "description": "Optional: a domain name, or 'recent'. Empty string for the all-domains summary.",
                },
            },
            "required": [],
        },
    },
    {
        "name": "read_facts",
        "description": (
            "List the confirmed facts stored in long-term memory about the "
            "current user. Use when asked what you know or remember about "
            "them, or to check whether something specific is on record. "
            "Optionally filter by a search term."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "search": {
                    "type": "string",
                    "description": "Optional case-insensitive substring filter. Empty string lists recent facts.",
                },
            },
            "required": [],
        },
    },
    {
        "name": "list_reminders",
        "description": (
            "List pending (not yet fired) reminders. Use when asked what "
            "reminders are set or what's coming up. Call this first to get "
            "a reminder's id before modifying or cancelling it."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "include_fired": {
                    "type": "boolean",
                    "description": "also show already-fired and cancelled reminders",
                },
            },
            "required": [],
        },
    },
    {
        "name": "read_file",
        "description": (
            "Read a whitelisted file from the system — skill source files, "
            "report outputs, or patch files. Give the filename (e.g. "
            "'taxonomy.py' or a report filename). Admin users only."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "filename": {
                    "type": "string",
                    "description": "Filename to read, optionally with a relative dir like 'skills/wx.py'.",
                },
            },
            "required": ["filename"],
        },
    },
    {
        "name": "create_reminder",
        "description": (
            "Set a reminder that fires at a future time and delivers a "
            "message over Signal. Use whenever the user asks to be "
            "reminded, pinged, or nudged about something later — for "
            "themselves, another user (another household user), or everyone. "
            "Handles absolute times ('tuesday 3pm'), relative times "
            "('in 20 minutes', 'in 2 hours'), and vague durations ('in a "
            "bit', 'shortly'). Also handles multiple lead-time offsets "
            "before an event (e.g. '1 week before and 2 hours before')."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "request": {
                    "type": "string",
                    "description": (
                        "The reminder rewritten as a clear, self-contained "
                        "natural-language instruction, as if typed fresh to "
                        "a scheduling assistant with no other context — "
                        "include who it's for, when, and what it's about. "
                        "e.g. 'remind the partner tomorrow at noon to call the "
                        "dentist about the appointment'."
                    ),
                },
            },
            "required": ["request"],
        },
    },
    # ── Capability tools added 19 Nov 2026 — plain language replaces the
    # prefix-command CLI for most requests ("commands → tools").
    {
        "name": "web_search",
        "description": (
            "Search the live web (DuckDuckGo). Use for anything involving "
            "current events, prices, weather beyond the local forecast, "
            "facts you are unsure of, or anything after your knowledge "
            "cutoff. Returns titles, URLs and snippets."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    },
    {
        "name": "set_effort",
        "description": (
            "Change how hard you think on upcoming replies when the user "
            "asks for deeper or quicker reasoning in plain language "
            "('really think about this', 'stop overanalyzing'). Levels: "
            "low, medium, high (default), xhigh, max. Sticky for 5 turns."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"level": {"type": "string",
                           "enum": ["low", "medium", "high", "xhigh", "max"]}},
            "required": ["level"],
        },
    },
    {
        "name": "run_research",
        "description": (
            "Launch the deep research pipeline (multi-round web search, "
            "dual-model crosscheck, cited report — takes several minutes, "
            "delivered when done). Use when the user wants something "
            "researched, investigated, or dug into properly. Admin only."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"topic": {"type": "string"}},
            "required": ["topic"],
        },
    },
    {
        "name": "run_analyze",
        "description": (
            "Launch the structured analysis pipeline on a "
            "company or country (multi-mode, crosschecked — takes several "
            "minutes, delivered when done). Admin only."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"subject": {"type": "string",
                           "description": "entity plus optional focus, e.g. 'Acme Corp AI strategy'"}},
            "required": ["subject"],
        },
    },
    {
        "name": "run_crosscheck",
        "description": (
            "Launch the dual-model arbitration pipeline on a question or "
            "claim: two models answer independently with web search, a "
            "third adjudicates with calibrated confidence language. Use when "
            "the user wants a claim verified or a high-stakes answer "
            "double-checked. Admin only."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"question": {"type": "string"}},
            "required": ["question"],
        },
    },
    {
        "name": "msg_user",
        "description": (
            "Send a Signal message to another household user (the admin, the partner, or "
            "the child user) on the sender's behalf. The relay summarizes and "
            "attributes it. Use when asked to tell, ask, or pass something "
            "along to someone."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "recipient": {"type": "string", "description": "another household user"},
                "message":   {"type": "string"},
            },
            "required": ["recipient", "message"],
        },
    },
    {
        "name": "schedule_followup",
        "description": (
            "Schedule yourself to follow up with this user later. When it "
            "fires, you'll be re-invoked with tools and the note, so you "
            "can check on things before messaging them. Use when the user "
            "mentions something worth circling back on ('vet results come "
            "Thursday', 'I'll plant Saturday') — propose it, don't spam. "
            "Max 3 per user per day."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "when": {"type": "string",
                         "description": "natural language time, e.g. 'tomorrow 9am', 'in 3 hours'"},
                "note": {"type": "string",
                         "description": "self-contained context for future-you, e.g. "
                                        "'the admin planted rye Saturday — ask how it went, check weather first'"},
            },
            "required": ["when", "note"],
        },
    },
    {
        "name": "create_document",
        "description": (
            "Produce a real PDF or DOCX document and send it to the user "
            "as a Signal attachment. It is also saved locally and indexed "
            "into the vault. Use for reports, letters, plans, summaries — "
            "anything the user wants as a document rather than a message. "
            "PDF content is full Markdown — headings, **bold**, lists, "
            "tables, blockquotes all render with real typography. Write it "
            "like a well-structured document, not plain text."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "title":   {"type": "string"},
                "content": {"type": "string", "description": "full document body"},
                "format":  {"type": "string", "enum": ["pdf", "docx"]},
                "domain":  {"type": "string",
                            "description": "vault domain tag, e.g. professional, personal, general"},
            },
            "required": ["title", "content", "format"],
        },
    },
    {
        "name": "stage_fix",
        "description": (
            "Stage a self-repair or self-modification job for the admin to "
            "approve. Use when an admin asks you to change your own code, "
            "config, scheduled jobs (cron), or to make a git commit - "
            "anything you have no direct tool for. This only STAGES the "
            "job: nothing runs until the admin personally replies "
            "'fix: approve' (or 'fix: reject'). You cannot approve it. "
            "Write the description as a complete, self-contained work "
            "order for an autonomous coding agent. Admin only."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"description": {"type": "string",
                "description": "the full work order: what to change, limits, "
                               "and whether to commit"}},
            "required": ["description"],
        },
    },
    {
        "name": "browse_task",
        "description": (
            "Drive a real Chromium browser to complete a task on live "
            "websites — JavaScript pages, multi-step navigation, reading "
            "content that plain web_search snippets can't reach. Runs as "
            "its own session (up to 15 min), results arrive as a separate "
            "message; the admin can watch live and take over for logins. Use "
            "when web_search snippets aren't enough. Admin only."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"task": {"type": "string",
                "description": "self-contained instruction, e.g. 'find the "
                               "current price of a cordless drill at a "
                               "hardware store and note local stock'"}},
            "required": ["task"],
        },
    },
    {
        "name": "read_session_notes",
        "description": (
            "Read the development notes Claude Code keeps about YOU (Chip): "
            "what was changed in recent upgrade sessions, your current "
            "capabilities, known issues, and architecture decisions. Call "
            "with no filename to list available notes. ALWAYS read "
            "chip_current_state.md first: it is the current truth, and the "
            "dated Chip_*.md reports are history that it overrides. Use whenever the admin or the partner asks what changed about "
            "you, what you can do now, why something was done, or about "
            "your own development history. Read-only; admin only."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "filename": {"type": "string",
                             "description": "omit to list; else a name from the list"},
            },
            "required": [],
        },
    },
    {
        "name": "read_document",
        "description": (
            "Read a PDF, DOCX, TXT or MD file by filename from Chip's "
            "document store, report archive, or synced folders. Use to "
            "review, summarize, or edit an existing document (edit = read "
            "it, rewrite the content, then create_document a new version)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"filename": {"type": "string"}},
            "required": ["filename"],
        },
    },

    # ─── Self-scheduling (admin only, 19 Nov 2026) ───────────────────────────
    # These operate on schedule metadata and reminder payloads ONLY. They do
    # not read or write facts, history, the rolling summary, the vault, the
    # media catalog, or recipes. Creating a job never creates a command —
    # see skills/scheduler.py for why there is no crontab write path.
    {
        "name": "list_scheduled_jobs",
        "description": (
            "List everything that runs on a schedule: your own managed "
            "jobs, and the system jobs (vitals, backups, nightly cycle, "
            "updates and so on) with each one's schedule, on/off state, "
            "last successful run and any failures. Use when asked what is "
            "scheduled, what runs automatically, or whether something is "
            "failing. Admin only."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "create_scheduled_job",
        "description": (
            "Create a recurring scheduled job that wakes you up on a "
            "schedule to do something for a household user — e.g. a morning "
            "weather check or a Friday summary. When it fires you receive "
            "the note and act on it with tools, then message the user. This "
            "schedules a RECURRING task; for a one-off use create_reminder. "
            "It cannot run shell commands or scripts. Admin only."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string",
                         "description": "short unique label, e.g. 'morning-weather'"},
                "schedule": {"type": "string",
                             "description": "5-field cron: minute hour day-of-month "
                                            "month day-of-week. e.g. '0 7 * * *' "
                                            "daily at 7am, '*/30 * * * *' every 30 "
                                            "minutes, '0 17 * * 5' Fridays at 5pm."},
                "target_user": {"type": "string",
                                "description": "name of the household user this job "
                                               "acts for and messages"},
                "note": {"type": "string",
                         "description": "what to do when it fires, written so a "
                                        "future you with no memory of this "
                                        "conversation can act on it alone"},
            },
            "required": ["name", "schedule", "target_user", "note"],
        },
    },
    {
        "name": "modify_scheduled_job",
        "description": (
            "Change an existing scheduled job's schedule, target user, or "
            "note. Give the job id from list_scheduled_jobs plus only the "
            "fields you are changing. Cannot modify system cron entries. "
            "Admin only."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "job_id": {"type": "string", "description": "id from list_scheduled_jobs, e.g. 'job001'"},
                "schedule": {"type": "string", "description": "new 5-field cron expression"},
                "target_user": {"type": "string", "description": "new target household user"},
                "note": {"type": "string", "description": "new instruction text"},
            },
            "required": ["job_id"],
        },
    },
    {
        "name": "set_scheduled_job_enabled",
        "description": (
            "Turn a scheduled job on or off: one of your managed jobs (by "
            "id) or an unlocked system job (by name, e.g. vitals, "
            "dream_cycle). Disabling is reversible and keeps settings; "
            "nothing can be deleted. Locked system jobs cannot be changed. "
            "Admin only."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "job_id": {"type": "string", "description": "id from list_scheduled_jobs"},
                "enabled": {"type": "boolean", "description": "true to enable, false to disable"},
            },
            "required": ["job_id", "enabled"],
        },
    },
    {
        "name": "modify_reminder",
        "description": (
            "Change a pending reminder's fire time and/or its message. Get "
            "the reminder id from list_reminders first. Use when someone "
            "asks to move, delay, or reword a reminder that is already set, "
            "instead of creating a second one. Admin only."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "reminder_id": {"type": "string", "description": "id from list_reminders"},
                "when": {"type": "string",
                         "description": "new time in natural language, e.g. "
                                        "'tomorrow at 9am' or 'in 3 hours'. Omit to "
                                        "keep the current time."},
                "message": {"type": "string",
                            "description": "new reminder text. Omit to keep the current text."},
            },
            "required": ["reminder_id"],
        },
    },
    {
        "name": "cancel_reminder",
        "description": (
            "Cancel a pending reminder so it will not fire. Reversible — "
            "pass restore=true to bring a cancelled reminder back. Get the "
            "id from list_reminders. Admin only."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "reminder_id": {"type": "string", "description": "id from list_reminders"},
                "restore": {"type": "boolean",
                            "description": "true to un-cancel a previously cancelled reminder"},
            },
            "required": ["reminder_id"],
        },
    },
]

# Tools hidden from non-admin users entirely. Execution is ALSO tier-checked
# in the handlers below (same mechanism as read_file / stage_fix) — this set
# is the belt to that suspenders: a non-admin never sees the schedule tools
# in their tool list, so the model cannot be talked into reaching for one.
ADMIN_ONLY_TOOLS = {
    "list_scheduled_jobs",
    "create_scheduled_job",
    "modify_scheduled_job",
    "set_scheduled_job_enabled",
    "modify_reminder",
    "cancel_reminder",
}


def tools_for(tier):
    """TOOL_DEFS filtered for a user tier."""
    if tier == "admin":
        return TOOL_DEFS
    return [t for t in TOOL_DEFS if t["name"] not in ADMIN_ONLY_TOOLS]


def gemini_tools(tier="admin"):
    """Same tool definitions, reshaped for Gemini's function_declarations format."""
    decls = []
    for t in tools_for(tier):
        decls.append({
            "name": t["name"],
            "description": t["description"],
            "parameters": t["input_schema"],
        })
    return [{"function_declarations": decls}]


async def execute_tool(tool_name, tool_input, *, sender, name, active_voices,
                       set_effort_fn=None, **_extra_fns):
    return await _execute_tool_inner(
        tool_name, tool_input, sender=sender, name=name,
        active_voices=active_voices, set_effort_fn=set_effort_fn,
        **_extra_fns)


async def _execute_tool_inner(tool_name, tool_input, *, sender, name, active_voices,
                              set_effort_fn=None,
                       retrieve_rag_fn, session=None, cat_whitelist=None,
                       tier="user"):
    """
    Execute one tool call, return a string result for feeding back to the
    model. Never raises — failures become part of the result text so the
    model can decide how to handle them in its final answer, rather than the
    whole conversation breaking.

    active_voices: the literal dict object from coordinator.py's module
    scope, passed in so mutations here are visible there too (dicts are
    mutable / passed by reference).
    retrieve_rag_fn: coordinator.py's retrieve_rag function, passed in
    rather than imported to avoid a circular import.
    """
    try:
        if tool_name == "remember_fact":
            facts = tool_input.get("facts", [])
            if not facts:
                return "No facts provided."
            conn = sqlite3.connect(CONTEXT_DB)
            now  = datetime.now().isoformat()
            inserted = []
            for fact in facts:
                fact = str(fact).strip()
                if fact and len(fact) > 5:
                    conn.execute(
                        "INSERT INTO facts (content, confirmed, created_at, valid_from, status, source, user) "
                        "VALUES (?, 1, ?, ?, 'active', 'tool_call', ?)",
                        (fact, now, now, name)
                    )
                    inserted.append(fact)
            conn.commit()
            conn.close()
            return f"Saved {len(inserted)} fact(s): {'; '.join(inserted[:3])}" if inserted else "No valid facts to save."

        elif tool_name == "adjust_voice":
            modifier = str(tool_input.get("modifier", "")).strip()
            duration = int(tool_input.get("duration_turns", 5) or 5)
            if not modifier:
                return "No modifier provided."
            active_voices[sender] = {"prompt": modifier, "remaining": duration}
            return f"Voice adjusted: {modifier} (next {duration} turns)."

        elif tool_name == "search_vault":
            query = str(tool_input.get("query", "")).strip()
            if not query:
                return "No query provided."
            context, score, filenames = retrieve_rag_fn(query)
            if not context:
                return "No relevant results found in vault."
            source_note = f" (source: {', '.join(filenames[:3])})" if filenames else ""
            return f"{context[:3000]}{source_note}"

        elif tool_name == "search_media":
            query      = str(tool_input.get("query", "")).strip()
            media_type = str(tool_input.get("media_type", "")).strip().lower()
            for_user   = str(tool_input.get("user", "")).strip().lower()
            media_db   = f"{WORKSPACE}/data/memory/chip_media.db"
            if not os.path.exists(media_db):
                return "Media catalog not found."
            conn   = sqlite3.connect(media_db)
            sql    = ("SELECT user, media_type, title, creator, rating, date_reviewed "
                      "FROM media_reviews WHERE 1=1")
            params = []
            if query:
                sql += " AND (title LIKE ? OR creator LIKE ?)"
                params += [f"%{query}%", f"%{query}%"]
            if media_type in ("book", "movie", "tv"):
                sql += " AND media_type = ?"
                params.append(media_type)
            if for_user:
                sql += " AND lower(user) = ?"
                params.append(for_user)
            sql += " ORDER BY rating DESC, date_reviewed DESC LIMIT 40"
            rows = conn.execute(sql, params).fetchall()
            conn.close()
            if not rows:
                return "No matching entries in the media catalog."
            lines = []
            for u, mt, title, creator, rating, dr in rows:
                line = f"{u}: [{(mt or '?').upper()}] {title}"
                if creator:
                    line += f" by {creator}"
                if rating is not None:
                    line += f" — {rating}/10"
                if dr:
                    line += f" ({dr})"
                lines.append(line)
            return "\n".join(lines)

        elif tool_name == "save_recipe":
            title       = str(tool_input.get("title", "")).strip()
            ingredients = tool_input.get("ingredients", [])
            method      = tool_input.get("method", [])
            tags        = str(tool_input.get("tags", "")).strip()
            if not title or not ingredients or not method:
                return "Missing title, ingredients, or method — recipe not saved."
            from skills.taxonomy import _commit_recipe
            recipe_json = json.dumps({
                "title": title, "ingredients": ingredients,
                "method": method, "tags": tags,
            })
            saved_title = _commit_recipe(name, recipe_json, title)
            if saved_title:
                return f"Recipe saved: {saved_title}"
            return "Recipe save failed — check logs."

        elif tool_name == "log_media":
            title      = str(tool_input.get("title", "")).strip()
            media_type = str(tool_input.get("media_type", "book")).strip().lower()
            if media_type not in ("book", "movie", "tv"):
                media_type = "book"
            creator = str(tool_input.get("creator", "")).strip() or None
            rating  = tool_input.get("rating")
            notes   = str(tool_input.get("notes", "")).strip() or None
            if not title:
                return "No title provided — not saved."
            from skills.media_review import DB_PATH as MEDIA_DB_PATH, _insert as _media_insert
            conn   = sqlite3.connect(MEDIA_DB_PATH)
            action = _media_insert(conn, name, media_type, title, creator, rating, notes, source="tool_call")
            conn.close()
            verb = "Updated" if action == "updated" else "Logged"
            return f"{verb}: {media_type} — {title}" + (f" ({rating}/10)" if rating is not None else "")

        elif tool_name == "get_weather":
            if session is None:
                return "Weather unavailable (no session)."
            try:
                from skills.wx import get_weather
            except ImportError:
                return "wx skill not installed."
            location = str(tool_input.get("location", "")).strip()
            answer, _tag = await get_weather(session, sender, location)
            return answer

        elif tool_name == "get_usage_report":
            from skills.usage_tracker import get_usage_breakdown
            period = (str(tool_input.get("month", "")).strip()
                      or str(tool_input.get("period", "")).strip() or "today")
            if period not in ("today", "month") and not re.match(r"^\d{4}-\d{2}$", period):
                return f"Bad period '{period}' — use today, month, or YYYY-MM."
            return await asyncio.to_thread(get_usage_breakdown, period)

        elif tool_name == "system_health":
            def _vitals():
                import psutil as _ps
                cpu  = _ps.cpu_percent(interval=0.5)
                temp = (os.popen("vcgencmd measure_temp 2>/dev/null")
                        .readline().replace("temp=", "").strip() or "N/A")
                ram  = _ps.virtual_memory()
                disk = _ps.disk_usage(WORKSPACE)
                return (f"Temp: {temp}  CPU: {cpu}%\n"
                        f"RAM: {ram.used//1024**2}MB / {ram.total//1024**2}MB\n"
                        f"Disk: {disk.used//1024**3}GB / {disk.total//1024**3}GB")
            return await asyncio.to_thread(_vitals)

        elif tool_name == "list_vault":
            vault_db = f"{WORKSPACE}/data/memory/chip_vault.db"
            domain   = str(tool_input.get("domain", "")).strip().lower()
            conn     = sqlite3.connect(vault_db)
            if domain == "recent":
                rows = conn.execute(
                    "SELECT filename, domain, chunk_count, ingested_at "
                    "FROM documents ORDER BY ingested_at DESC LIMIT 10"
                ).fetchall()
                conn.close()
                if not rows:
                    return "Vault is empty."
                return "Recent:\n" + "\n".join(
                    f"  {f}  [{d}]  {c}ch  {str(t)[:10]}" for f, d, c, t in rows)
            elif domain:
                rows = conn.execute(
                    "SELECT filename, chunk_count, ingested_at FROM documents "
                    "WHERE domain=? ORDER BY ingested_at DESC LIMIT 20", (domain,)
                ).fetchall()
                conn.close()
                if not rows:
                    return f"No documents in domain '{domain}'."
                return f"{domain}:\n" + "\n".join(
                    f"  {f}  {c}ch  {str(t)[:10]}" for f, c, t in rows)
            else:
                rows = conn.execute(
                    "SELECT domain, COUNT(*), SUM(chunk_count) "
                    "FROM documents GROUP BY domain ORDER BY COUNT(*) DESC"
                ).fetchall()
                conn.close()
                if not rows:
                    return "Vault is empty."
                return "Vault:\n" + "\n".join(
                    f"  {d}  {n} docs  {c or 0} chunks" for d, n, c in rows)

        elif tool_name == "read_facts":
            search = str(tool_input.get("search", "")).strip().lower()
            conn   = sqlite3.connect(CONTEXT_DB)
            rows   = conn.execute(
                "SELECT content, created_at FROM facts WHERE confirmed=1 "
                "AND (user=? OR user IS NULL) "
                "ORDER BY created_at DESC LIMIT 200",
                (name,)
            ).fetchall()
            conn.close()
            if search:
                rows = [(c, t) for c, t in rows if search in c.lower()]
            if not rows:
                return ("No matching facts on record." if search
                        else "No confirmed facts on record.")
            rows = rows[:60]
            return f"Facts ({len(rows)} shown):\n" + "\n".join(
                f"• {c}  [{str(t)[:10]}]" for c, t in rows)

        elif tool_name == "list_reminders":
            from skills import remind as _rem
            data = _rem.load_reminders()
            if not data:
                return "No reminders set."
            # Admins get stable ids (needed by modify_reminder /
            # cancel_reminder) and can opt into seeing fired/cancelled ones.
            show_all = (tier == "admin"
                        and bool(tool_input.get("include_fired")))
            rows = data if show_all else [r for r in data if _rem.is_active(r)]
            if not rows:
                return "No pending reminders."
            out = []
            for r in rows:
                if r.get("fired"):      state = "[fired]     "
                elif r.get("cancelled"): state = "[cancelled] "
                else:                   state = ""
                prefix = f"  [{_rem.reminder_id(r)}] " if tier == "admin" else "  "
                out.append(f"{prefix}{r['fire_at'][:16]}  {state}"
                           f"{r['recipient_name']}  {r['message'][:60]}")
            label = "Reminders" if show_all else "Pending"
            return f"{label} ({len(rows)}):\n" + "\n".join(out)

        elif tool_name == "read_file":
            if tier != "admin":
                return "read_file is not available for this user."
            if not cat_whitelist:
                return "File access unavailable (no whitelist configured)."
            target = str(tool_input.get("filename", "")).strip()
            if not target or ".." in target:
                return "Invalid filename."
            resolved = None
            for d in cat_whitelist:
                candidate = os.path.realpath(os.path.join(d, os.path.basename(target)
                                             if "/" not in target else target.split("/", 1)[-1]))
                if candidate.startswith(os.path.realpath(d)) and os.path.isfile(candidate):
                    resolved = candidate
                    break
            if not resolved:
                return (f"'{target}' not found in whitelisted locations "
                        f"(skills/, reports/, patches/).")
            try:
                content = open(resolved).read()
            except Exception as e:
                return f"Read error: {e}"
            if len(content) > 6000:
                content = content[:6000] + f"\n\n[truncated — {len(content)} chars total]"
            return f"{os.path.basename(resolved)}:\n{content}"

        elif tool_name == "create_reminder":
            from skills.remind import (parse_reminder, build_message,
                                        add_reminder, iso_duration_to_seconds)
            from coordinator import USERS
            request = str(tool_input.get("request", "")).strip()
            if not request:
                return "No reminder request provided."
            sender_name  = USERS.get(sender, {}).get("name", "User")
            sender_phone = sender
            try:
                parsed = await parse_reminder(request, sender_name, sender_phone)
            except Exception as e:
                return f"Could not parse reminder: {e}"
            try:
                event_time = datetime.fromisoformat(parsed["event_time"])
            except Exception:
                return "Could not determine event time from that request — try being more specific."
            topic      = parsed.get("topic", "")
            recipients = parsed.get("recipients") or [{"name": sender_name, "phone": sender_phone}]
            offsets    = parsed.get("offsets") or ["PT0S"]
            scheduled_names = []
            skipped_past    = 0
            for offset_str in offsets:
                offset_seconds = iso_duration_to_seconds(offset_str)
                fire_at = event_time - timedelta(seconds=offset_seconds)
                if fire_at <= datetime.now():
                    skipped_past += 1
                    continue
                for r in recipients:
                    r_name  = r.get("name", sender_name)
                    r_phone = r.get("phone", sender_phone)
                    message = await build_message(r_name, sender_name, topic, event_time, offset_seconds)
                    add_reminder(fire_at, r_phone, r_name, sender_name, message)
                    scheduled_names.append(r_name)
            if not scheduled_names:
                return "All reminder times are in the past — nothing scheduled."
            names_str = ", ".join(dict.fromkeys(scheduled_names))
            event_fmt = event_time.strftime("%a %b %-d at %-I:%M %p")
            note = f" ({skipped_past} offset(s) skipped — already past)" if skipped_past else ""
            return f"Reminder set for {names_str}: {event_fmt} — {topic}{note}"

        elif tool_name == "web_search":
            from skills.web_utils import quick_search
            query   = str(tool_input.get("query", "")).strip()
            if not query:
                return "No search query provided."
            results = await asyncio.to_thread(quick_search, query, 5)
            if isinstance(results, str):          # error or VPN refusal text
                return results
            if not results:
                return "No results found."
            return "\n---\n".join(
                f"{r.get('title','')}\n{r.get('href','')}\n{r.get('body','')}"
                for r in results)

        elif tool_name == "set_effort":
            level = str(tool_input.get("level", "")).strip().lower()
            if level not in ("low", "medium", "high", "xhigh", "max"):
                return "Invalid level — use low/medium/high/xhigh/max."
            if tier != "admin" and level in ("xhigh", "max"):
                level = "high"                    # cost cap for the user tier
            if set_effort_fn is None:
                return "Effort control unavailable in this context."
            set_effort_fn(sender, level)
            return f"Thinking effort set to {level} for the next 5 turns."

        elif tool_name in ("run_research", "run_analyze", "run_crosscheck"):
            if tier != "admin":
                return "That pipeline is admin-only."
            skill_key = {"run_research": "research", "run_analyze": "analyze",
                         "run_crosscheck": "crosscheck"}[tool_name]
            payload = str(tool_input.get("topic") or tool_input.get("subject")
                          or tool_input.get("question") or "").strip()
            if not payload:
                return "Nothing to run — missing the topic."

            async def _bg():
                from skills.registry import SKILL_REGISTRY
                from coordinator import deliver_response, send_signal, TAGS
                try:
                    answer, force_file = await SKILL_REGISTRY[skill_key](
                        session, sender, payload, "", [])
                    if answer and force_file != "direct":
                        await deliver_response(session, answer, sender,
                                               tag=TAGS["cloud_heavy"],
                                               force_file=bool(force_file))
                except Exception as e:
                    await send_signal(session,
                        f"{TAGS['chip']} {skill_key} failed: {e}", recipient=sender)
            asyncio.create_task(_bg())
            return (f"{skill_key} pipeline started on: {payload}. It runs a few "
                    f"minutes; results arrive as a separate message. Tell the "
                    f"user this plainly.")

        elif tool_name == "msg_user":
            recipient = str(tool_input.get("recipient", "")).strip()
            message   = str(tool_input.get("message", "")).strip()
            if recipient.lower() in ("all", "everyone") and tier != "admin":
                return "Broadcast is admin-only — name one person."
            if not recipient or not message:
                return "Need both a recipient and a message."
            from skills.registry import SKILL_REGISTRY
            try:
                answer, _ff = await SKILL_REGISTRY["msg"](
                    session, sender, f"{recipient} {message}", "", [])
                return answer or f"Relayed to {recipient}."
            except Exception as e:
                return f"Relay failed: {e}"

        elif tool_name == "schedule_followup":
            from skills.remind import parse_reminder, add_reminder, load_reminders
            from coordinator import USERS
            when = str(tool_input.get("when", "")).strip()
            note = str(tool_input.get("note", "")).strip()
            if not when or not note:
                return "Need both a time and a note."
            sender_name = USERS.get(sender, {}).get("name", "User")
            # daily cap + dedup (normalized note prefix)
            today = datetime.now().strftime("%Y-%m-%d")
            norm  = re.sub(r"\W+", "", note.lower())[:60]
            existing = [r for r in load_reminders()
                        if r.get("type") == "followup"
                        and r.get("recipient_phone") == sender
                        and not r.get("fired")]
            if any(re.sub(r"\W+", "", r.get("context", "").lower())[:60] == norm
                   for r in existing):
                return "A follow-up on that is already scheduled."
            created_today = [r for r in existing
                             if str(r.get("created_at", "")).startswith(today)]
            if len(created_today) >= 3:
                return "Follow-up limit reached for today (3) — not scheduling."
            try:
                parsed = await parse_reminder(
                    f"remind {sender_name} {when} about: follow-up",
                    sender_name, sender)
                fire_at = datetime.fromisoformat(parsed["event_time"])
            except Exception as e:
                return f"Couldn't parse that time: {e}"
            if fire_at <= datetime.now():
                return "That time is in the past."
            add_reminder(fire_at, sender, sender_name, "Chip",
                         f"[follow-up] {note}",
                         rtype="followup", context=note)
            return (f"Follow-up scheduled for "
                    f"{fire_at.strftime('%a %b %-d at %-I:%M %p')}.")

        elif tool_name == "create_document":
            if tier != "admin":
                return "Document production is admin-only."
            from skills import documents
            from coordinator import send_signal_file
            title   = str(tool_input.get("title", "")).strip() or "Document"
            content = str(tool_input.get("content", "")).strip()
            fmt     = str(tool_input.get("format", "pdf")).strip().lower()
            domain  = str(tool_input.get("domain", "general")).strip() or "general"
            if not content:
                return "No content provided."
            try:
                maker = documents.make_pdf if fmt == "pdf" else documents.make_docx
                path  = await asyncio.to_thread(maker, title, content)
            except Exception as e:
                return f"Document build failed: {e}"
            await send_signal_file(session, path, f"[Chip] {title}", sender)
            ingested = await asyncio.to_thread(
                documents.ingest_produced, path, title, content, domain)
            return (f"Sent {os.path.basename(path)} as an attachment; saved to "
                    f"data/documents/ and {'indexed into the vault' if ingested else 'vault indexing failed'}. "
                    f"Confirm briefly — do not repeat the document body in chat.")

        elif tool_name == "stage_fix":
            # Chip may STAGE a job; approval is a separate message typed by an
            # admin and handled by the coordinator, never by a tool. The exact
            # staged text is sent to the admin by this handler, so what gets
            # approved is what was staged, not the model's paraphrase of it.
            if tier != "admin":
                return "Staging repair jobs is admin-only."
            desc = str(tool_input.get("description", "")).strip()
            if len(desc) < 20:
                return "Describe the job fully - that description is the work order."
            from skills import self_heal
            from coordinator import send_signal, USERS
            if "awaiting" in self_heal.status():
                return ("A job is already staged and waiting. The admin must "
                        "reply 'fix: approve' or 'fix: reject' before another.")
            staged = self_heal.stage(desc, staged_by=USERS.get(sender, {}).get("name", "admin"))
            await send_signal(session, f"[Chip] {staged}", recipient=sender)
            return ("Staged, and the exact job text was just sent to the admin. "
                    "It will NOT run until they reply 'fix: approve'. Tell them "
                    "that briefly; do not repeat the job text.")

        elif tool_name == "browse_task":
            if tier != "admin":
                return "Browsing is admin-only."
            task = str(tool_input.get("task", "")).strip()
            if not task:
                return "No task given."
            from skills import browse as _browse
            if not _browse.service_up():
                return "Browser service is down (sudo systemctl status chip-browser)."

            async def _bg():
                from coordinator import deliver_response, send_signal, TAGS, cancel_flags
                try:
                    res, rt = await _browse.browse_session(
                        session, sender, task, send_signal=send_signal,
                        cancel_flags=cancel_flags)
                    await deliver_response(session, f"{res}\n\n[{rt}]", sender,
                                           tag=TAGS["cloud_heavy"])
                except Exception as e:
                    await send_signal(session, f"{TAGS['chip']} Browse failed: {e}",
                                      recipient=sender)
            asyncio.create_task(_bg())
            return ("Browser session started — it runs up to 15 minutes and "
                    "the result arrives as a separate message. Tell the user "
                    "this plainly, including that they can say 'stop:' to cancel.")

        elif tool_name == "read_session_notes":
            # Chip reading its own development record (19 Nov 2026, the admin's
            # ask). Two curated sources, live-read so never stale:
            #   - Claude Code's persistent memory dir (MEMORY.md index +
            #     project notes, updated every dev session)
            #   - the Chip_*.md session reports in the workspace
            # Raw session transcripts are deliberately excluded (huge, noisy,
            # and they contain security specifics). Admin-gated because the
            # notes discuss vulnerabilities and family-member permissions.
            if tier != "admin":
                return "Session notes are admin-only."
            _NOTES_DIRS = [
                os.path.expanduser("~/.claude/projects/-" + WORKSPACE.strip("/").replace("/", "-") + "/memory"),
                WORKSPACE,
            ]

            def _notes_inventory():
                inv = []  # (display_name, path)
                mem = _NOTES_DIRS[0]
                if os.path.isdir(mem):
                    for f in sorted(os.listdir(mem)):
                        if f.endswith(".md"):
                            inv.append((f, os.path.join(mem, f)))
                import glob as _glob
                for p in sorted(_glob.glob(f"{WORKSPACE}/Chip_*.md")) + \
                         sorted(_glob.glob(f"{WORKSPACE}/Chip_*.txt")) + \
                         [f"{WORKSPACE}/CLAUDE.md"]:
                    if os.path.isfile(p):
                        inv.append((os.path.basename(p), p))
                return inv

            inv = _notes_inventory()
            fname = str(tool_input.get("filename", "")).strip()
            if not fname:
                lines = ["Development notes available (read with filename). "
                         "chip_current_state.md is the CURRENT truth - read it "
                         "first. Dated reports are history:"]
                for name_, path_ in inv:
                    ts_ = datetime.fromtimestamp(os.path.getmtime(path_))
                    lines.append(f"  {name_}  ({os.path.getsize(path_)//1024}KB, "
                                 f"{ts_:%b %-d})")
                return "\n".join(lines)
            match = next((p for n, p in inv
                          if n == fname or n.lower() == fname.lower()), None)
            if not match:
                return f"No note named '{fname}' — call without a filename to list."
            try:
                text = open(match, encoding="utf-8", errors="replace").read()
            except Exception as e:
                return f"Read failed: {e}"
            if len(text) > 14000:
                text = text[:14000] + "\n…[truncated — ask for a specific section]"
            return f"[{fname}]\n{text}"

        elif tool_name == "read_document":
            from skills import documents
            filename = str(tool_input.get("filename", "")).strip()
            path = await asyncio.to_thread(documents.find_readable, filename)
            if not path:
                return (f"No file named '{filename}' in the document store, "
                        f"report archive, or synced folders.")
            try:
                text, note = await asyncio.to_thread(documents.read_any, path)
            except Exception as e:
                return f"Read failed: {e}"
            if not text.strip():
                return f"Found {os.path.basename(path)} but extracted no text ({note})."
            return f"[{os.path.basename(path)} — {note}]\n{text}"

        # ─── Self-scheduling (admin only) ───────────────────────────────────
        elif tool_name in ("list_scheduled_jobs", "create_scheduled_job",
                           "modify_scheduled_job", "set_scheduled_job_enabled",
                           "modify_reminder", "cancel_reminder"):
            if tier != "admin":
                return "Schedule management is admin-only."
            return await _handle_schedule_tool(tool_name, tool_input, sender)

        else:
            return f"Unknown tool: {tool_name}"

    except Exception as e:
        return f"Tool execution error: {e}"


# ─── Schedule-management handlers (19 Nov 2026) ───────────────────────────────
# Split out of the main dispatch chain purely for length. Every entry point is
# tier-gated by the caller above. These touch schedule metadata and reminder
# payloads only.

def _resolve_user(who):
    """Map a household name to (phone, name). Returns (None, who) if unknown."""
    from chip_config import phone_by_name, name_by_phone
    who = (who or "").strip()
    if not who:
        return None, who
    if who.startswith("+"):
        return who, name_by_phone(who, who)
    phone = phone_by_name(who)
    if phone:
        return phone, name_by_phone(phone, who)
    return None, who


def _cron_label(command):
    """Short human label for a system cron line — the script or program it
    runs, not whatever flag happens to be last before the redirect."""
    head = command.split(">>")[0].split("2>&1")[0].strip()
    toks = [t for t in head.split() if not t.startswith("-")]
    for t in reversed(toks):
        if t.endswith((".py", ".sh")):
            return os.path.basename(t)
    for t in toks:
        base = os.path.basename(t)
        if base and base not in ("sudo", "env") and "=" not in base \
                and not base.startswith("python"):
            return base
    return head[:40]


def _fmt_next(cron):
    from skills import scheduler
    try:
        nxt = scheduler.next_fire(cron)
    except ValueError:
        return "invalid schedule"
    if nxt is None:
        return "never (no matching date)"
    return nxt.strftime("%a %b %-d %H:%M")


def _registry_names():
    try:
        from skills import scheduler
        return {j["name"] for j in scheduler.load_registry()}
    except Exception:
        return set()


async def _handle_schedule_tool(tool_name, tool_input, sender):
    from skills import scheduler
    from skills import remind
    from coordinator import USERS

    actor = USERS.get(sender, {}).get("name", "admin")

    # ── Jobs ────────────────────────────────────────────────────────────────
    if tool_name == "list_scheduled_jobs":
        jobs = scheduler.load_jobs()
        lines = []
        if jobs:
            lines.append(f"Managed jobs ({len(jobs)}):")
            for j in jobs:
                state = "on " if j.get("enabled", True) else "OFF"
                nxt = (_fmt_next(j["cron"]) if j.get("enabled", True)
                       else "— disabled")
                lines.append(
                    f"  [{j['id']}] {state} {j['name']} — "
                    f"{scheduler.describe_cron(j['cron'])} ({j['cron']}) "
                    f"→ {j.get('target_name','?')} | next: {nxt}")
        else:
            lines.append("Managed jobs: none yet.")

        try:
            reg = await asyncio.to_thread(scheduler.registry_entries)
        except Exception as e:
            reg = []
            lines.append(f"\nSystem jobs: registry failed to load ({e}).")
        if reg:
            lines.append(f"\nSystem jobs ({len(reg)}). Use the job's name as job_id "
                         f"to switch it on/off or change its schedule; locked "
                         f"ones cannot be changed:")
            for r in reg:
                state = "on " if r["enabled"] else "OFF"
                lock  = " [locked]" if (r["protected"] or r["runner"] != "dispatcher") else ""
                if r["runner"] != "dispatcher":
                    status = "runs from the system crontab"
                elif r["last_success"]:
                    status = f"last ok {r['last_success'][5:16].replace('T', ' ')}"
                elif r["last_run"]:
                    status = f"last run failed (exit {r['last_exit_code']})"
                else:
                    status = "not run yet"
                if r["consecutive_failures"]:
                    status += f", {r['consecutive_failures']} failure(s) in a row"
                lines.append(f"  {state} {r['name']:<16} {r['schedule']:<13} "
                             f"{status}{lock}")
            problems = await asyncio.to_thread(scheduler.registry_problems)
            if problems:
                lines.append("NEEDS ATTENTION: " + "; ".join(problems))
        return "\n".join(lines)

    if tool_name == "create_scheduled_job":
        phone, tname = _resolve_user(tool_input.get("target_user"))
        if not phone:
            return (f"'{tool_input.get('target_user')}' isn't a known "
                    f"household user — can't target a job at them.")
        try:
            job = await asyncio.to_thread(
                scheduler.create_job,
                str(tool_input.get("name", "")),
                str(tool_input.get("schedule", "")),
                phone, tname,
                str(tool_input.get("note", "")),
                actor)
        except ValueError as e:
            return f"Job not created — {e}"
        return (f"Created [{job['id']}] {job['name']}: "
                f"{scheduler.describe_cron(job['cron'])} for {tname}. "
                f"Next fire {_fmt_next(job['cron'])}.")

    if tool_name == "modify_scheduled_job":
        job_id = str(tool_input.get("job_id", "")).strip()
        if job_id in _registry_names():
            # A system job: the schedule is the ONLY thing a tool may change.
            if not tool_input.get("schedule"):
                return ("For a system job only the schedule can be changed "
                        "(its command is fixed). Give a schedule.")
            try:
                job = await asyncio.to_thread(
                    scheduler.reschedule_registry_job, job_id,
                    str(tool_input["schedule"]))
            except ValueError as e:
                return f"Job not modified — {e}"
            return (f"Rescheduled system job {job['name']}: "
                    f"{scheduler.describe_cron(job['schedule'])} ({job['schedule']}).")
        kwargs = {}
        if tool_input.get("schedule"):
            kwargs["cron"] = str(tool_input["schedule"])
        if tool_input.get("note"):
            kwargs["note"] = str(tool_input["note"])
        if tool_input.get("target_user"):
            phone, tname = _resolve_user(tool_input["target_user"])
            if not phone:
                return (f"'{tool_input['target_user']}' isn't a known "
                        f"household user.")
            kwargs["target_phone"], kwargs["target_name"] = phone, tname
        if not kwargs:
            return "Nothing to change — give a schedule, target_user, or note."
        try:
            job = await asyncio.to_thread(
                lambda: scheduler.update_job(job_id, **kwargs))
        except ValueError as e:
            return f"Job not modified — {e}"
        return (f"Updated [{job['id']}] {job['name']}: "
                f"{scheduler.describe_cron(job['cron'])} → "
                f"{job.get('target_name','?')}. "
                f"Next fire {_fmt_next(job['cron'])}.")

    if tool_name == "set_scheduled_job_enabled":
        job_id = str(tool_input.get("job_id", "")).strip()
        want = bool(tool_input.get("enabled"))
        if job_id in _registry_names():
            try:
                job = await asyncio.to_thread(
                    scheduler.set_registry_enabled, job_id, want)
            except ValueError as e:
                return f"Job not changed — {e}"
            if job["enabled"]:
                return f"System job {job['name']} is ON ({job['schedule']})."
            warn = f" Consequence: {job['warn_on_disable']}" if job.get("warn_on_disable") else ""
            return (f"System job {job['name']} is OFF. Its settings are kept; "
                    f"switch it back on any time.{warn} Tell the admin this consequence.")
        try:
            job = await asyncio.to_thread(scheduler.set_enabled, job_id, want)
        except ValueError as e:
            return f"Job not changed — {e}"
        if job["enabled"]:
            return (f"Enabled [{job['id']}] {job['name']}. "
                    f"Next fire {_fmt_next(job['cron'])}.")
        return (f"Disabled [{job['id']}] {job['name']} — settings kept, "
                f"re-enable any time.")

    # ── Reminders (same store as create_reminder, no parallel storage) ──────
    if tool_name == "modify_reminder":
        rid = str(tool_input.get("reminder_id", "")).strip()
        target = remind.find_reminder(rid)
        if not target:
            return f"No reminder with id '{rid}' — run list_reminders first."

        fire_at = None
        if tool_input.get("when"):
            tname = target.get("recipient_name", actor)
            try:
                parsed = await remind.parse_reminder(
                    f"remind {tname} {tool_input['when']} about: reschedule",
                    actor, sender)
                fire_at = datetime.fromisoformat(parsed["event_time"])
            except Exception as e:
                return f"Couldn't parse that time: {e}"

        message = tool_input.get("message")
        if fire_at is None and not message:
            return "Nothing to change — give a new time, a new message, or both."
        try:
            r = await asyncio.to_thread(
                remind.update_reminder, rid, fire_at,
                str(message) if message else None)
        except ValueError as e:
            return f"Reminder not modified — {e}"
        return (f"Reminder [{r['id']}] now fires "
                f"{datetime.fromisoformat(r['fire_at']).strftime('%a %b %-d at %-I:%M %p')} "
                f"for {r.get('recipient_name','?')}.")

    if tool_name == "cancel_reminder":
        rid = str(tool_input.get("reminder_id", "")).strip()
        restore = bool(tool_input.get("restore"))
        try:
            r = await asyncio.to_thread(remind.set_cancelled, rid, not restore)
        except ValueError as e:
            return f"Reminder not changed — {e}"
        when = datetime.fromisoformat(r["fire_at"]).strftime("%a %b %-d at %-I:%M %p")
        if restore:
            return f"Restored reminder [{r['id']}] — fires {when}."
        return f"Cancelled reminder [{r['id']}] (was {when}). Reversible."

    return f"Unknown schedule tool: {tool_name}"
