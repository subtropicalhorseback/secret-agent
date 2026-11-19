"""
skills/llm.py — the single cloud-LLM client for the whole codebase
===================================================================
Written 19 Nov 2026 during the premier-model upgrade. Before this, five files
each carried their own claude_sync/gemini wrapper with its own retry, auth and
logging — which is how the audit's H5 (untracked spend) and the stale Opus 4.6
references happened. Every model migration was a patch script across 27 call
sites; now it's an edit to the constants below.

Model policy (the admin, 19 Nov 2026):
  - Claude surfaces run Opus 5, default effort HIGH.
  - Fable 5 is the adjudicator/advisor everywhere, default effort XHIGH:
    pipelines call it directly for arbitration steps; chat attaches it as a
    server-side advisor tool so Opus consults it mid-turn when stuck.
  - Gemini surfaces run 3.5 Flash (3.5 Flash-Lite for cheap classification).

API notes that bit us or will (verified live on this Pi, SDK 0.97.0):
  - Opus 5 / Fable 5: budget_tokens and temperature/top_p/top_k are REMOVED
    (400 if sent). Effort lives in output_config.
  - Fable 5: thinking is always on — omit the thinking param entirely.
    {"type": "disabled"} is a 400.
  - Advisor tool is beta "advisor-tool-2026-03-01"; Opus5→Fable5 is a valid
    pairing. The Fable advisor's output comes back ENCRYPTED
    (advisor_redacted_result) — replay it, never try to read it.
  - max_tokens exhaustion with adaptive thinking can eat the whole budget and
    return zero text (the Patch D failure). claude_sync retries once at double
    cap — this closes audit gap M4 for every caller at once.
"""
import os
import asyncio
import json
import requests

import skills.call_logger as call_logger

import os as _os, sys as _sys
_cc = _os.path.dirname(_os.path.abspath(__file__))
while _cc != "/" and not _os.path.exists(_os.path.join(_cc, "chip_config.py")):
    _cc = _os.path.dirname(_cc)
if _cc not in _sys.path:
    _sys.path.insert(0, _cc)
from chip_config import WORKSPACE
def _load_env():
    """Coordinator loads dotenv; cron/standalone callers may not."""
    if os.getenv("ANTHROPIC_API_KEY") and os.getenv("GEMINI_API_KEY"):
        return
    try:
        for line in open(f"{WORKSPACE}/.env"):
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.strip().split("=", 1)
                os.environ.setdefault(k, v.strip().strip('"').strip("'"))
    except Exception:
        pass


_load_env()

# ─── Model constants — THE place model strings live ──────────────────────────
OPUS   = "claude-opus-5"
FABLE  = "claude-fable-5"
SONNET = "claude-sonnet-5"
GFLASH = "gemini-3.5-flash"
GLITE  = "gemini-3.5-flash-lite"
REFUSAL_FALLBACK = SONNET

DEFAULT_EFFORT = "high"    # house default per the admin, 19 Nov 2026
FABLE_EFFORT   = "xhigh"   # Fable always runs hotter
MAX_TOKENS     = 16000
ADVISOR_BETA   = "advisor-tool-2026-03-01"

EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")


def advisor_tool(max_uses=2, max_tokens=3000):
    """Fable-5 advisor tool definition — attach to any Opus executor call."""
    return {
        "type": "advisor_20260301", "name": "advisor", "model": FABLE,
        "max_uses": max_uses, "max_tokens": max_tokens,
        "caching": {"type": "ephemeral", "ttl": "5m"},
    }


# ─── Claude ───────────────────────────────────────────────────────────────────

def claude_raw_sync(messages, system=None, model=OPUS, effort=None,
                    max_tokens=MAX_TOKENS, tools=None, advisor=False):
    """Full-control Claude call returning the raw response object.

    Handles the per-model parameter quirks and the max-tokens backstop;
    logs usage through call_logger (the single H5 funnel). Callers that just
    want text should use claude_sync/claude below.
    """
    import anthropic
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    if effort is None:
        effort = FABLE_EFFORT if model == FABLE else DEFAULT_EFFORT

    kw = dict(model=model, max_tokens=max_tokens, messages=messages,
              output_config={"effort": effort})
    if model != FABLE:               # Fable: thinking always on, param omitted
        kw["thinking"] = {"type": "adaptive"}
    if system is not None:
        # Long string systems get a cache breakpoint automatically; callers
        # that pass a pre-built block list (chat path) keep their own layout.
        if isinstance(system, str) and len(system) > 4000:
            system = [{"type": "text", "text": system,
                       "cache_control": {"type": "ephemeral"}}]
        kw["system"] = system

    tool_list = list(tools) if tools else []
    use_beta  = False
    if advisor and model != FABLE:   # Fable advising itself is pointless
        tool_list.append(advisor_tool())
        use_beta = True
    if tool_list:
        kw["tools"] = tool_list

    def _call():
        if use_beta:
            return client.beta.messages.create(betas=[ADVISOR_BETA], **kw)
        return client.messages.create(**kw)

    resp = _call()
    text_blocks = [b for b in resp.content if b.type == "text"]

    # Backstop (audit M4): adaptive thinking can exhaust max_tokens before any
    # text lands — the Patch D empty-reply failure. One retry at double cap.
    if resp.stop_reason == "max_tokens" and not text_blocks and max_tokens < 64000:
        call_logger.log_model_call(
            model=model, thinking=True,
            tokens_in=resp.usage.input_tokens, tokens_out=resp.usage.output_tokens,
            stop_reason="max_tokens_empty_retry", tools=[])
        kw["max_tokens"] = max_tokens * 2
        resp = _call()

    # A safety-classifier decline (stop_reason "refusal") returns HTTP 200 with
    # no text. Unhandled, the chat path delivered a bare "..." to the user
    # (19 Nov 2026). Retry once on the fallback model before giving up.
    if resp.stop_reason == "refusal" and model != REFUSAL_FALLBACK:
        call_logger.log_model_call(
            model=model, thinking=True,
            tokens_in=resp.usage.input_tokens, tokens_out=resp.usage.output_tokens,
            stop_reason="refusal_fallback", tools=[])
        return claude_raw_sync(messages, system=system, model=REFUSAL_FALLBACK,
                               effort=effort if effort != "xhigh" else "high",
                               max_tokens=max_tokens, tools=tools, advisor=False)

    tool_names = [b.name for b in resp.content if b.type in ("tool_use", "server_tool_use")]
    call_logger.log_model_call(
        model=model, thinking=True,
        tokens_in=resp.usage.input_tokens, tokens_out=resp.usage.output_tokens,
        stop_reason=resp.stop_reason, tools=tool_names)
    return resp


def claude_sync(prompt=None, system=None, model=OPUS, effort=None,
                max_tokens=MAX_TOKENS, messages=None, advisor=False):
    """Claude call returning joined text. Drop-in for the five old wrappers."""
    msgs = messages or [{"role": "user", "content": prompt}]
    resp = claude_raw_sync(msgs, system=system, model=model, effort=effort,
                           max_tokens=max_tokens, advisor=advisor)
    return "\n".join(b.text for b in resp.content if b.type == "text")


async def claude(prompt=None, system=None, model=OPUS, effort=None,
                 max_tokens=MAX_TOKENS, messages=None, advisor=False):
    return await asyncio.to_thread(
        claude_sync, prompt, system, model, effort, max_tokens, messages, advisor)


def adjudicate_sync(prompt, system=None, max_tokens=MAX_TOKENS):
    """The house arbitration call: Fable 5 at xhigh. Use for every crosscheck,
    arbitration, review and synthesis step — 'Fable as adjudicator universally'
    (the admin, 19 Nov 2026)."""
    return claude_sync(prompt, system=system, model=FABLE,
                       effort=FABLE_EFFORT, max_tokens=max_tokens)


async def adjudicate(prompt, system=None, max_tokens=MAX_TOKENS):
    return await asyncio.to_thread(adjudicate_sync, prompt, system, max_tokens)


# ─── Gemini ───────────────────────────────────────────────────────────────────

def gemini_sync(prompt, model=GFLASH, system=None, timeout=90, json_mode=False):
    """Simple Gemini generateContent returning text. systemInstruction is the
    real field (audit M8 — the old path smuggled system prompts in as a fake
    user turn)."""
    key = os.environ["GEMINI_API_KEY"]
    url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
           f"{model}:generateContent?key={key}")
    payload = {"contents": [{"role": "user", "parts": [{"text": prompt}]}]}
    if system:
        payload["systemInstruction"] = {"parts": [{"text": system}]}
    if json_mode:
        payload["generationConfig"] = {"responseMimeType": "application/json"}
    resp = requests.post(url, json=payload, timeout=timeout)
    resp.raise_for_status()
    data  = resp.json()
    usage = data.get("usageMetadata", {})
    call_logger.log_model_call(
        model=model, thinking=False,
        tokens_in=usage.get("promptTokenCount", 0),
        tokens_out=usage.get("candidatesTokenCount", 0),
        stop_reason=data["candidates"][0].get("finishReason", "?"), tools=[])
    parts = data["candidates"][0].get("content", {}).get("parts", [])
    return "\n".join(p["text"] for p in parts if "text" in p).strip()


async def gemini(prompt, model=GFLASH, system=None, timeout=90, json_mode=False):
    return await asyncio.to_thread(
        gemini_sync, prompt, model, system, timeout, json_mode)
