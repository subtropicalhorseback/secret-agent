"""
skills/call_logger.py — Structured, sanitized pipeline trace logging
========================================================================
Replaces bare print() with Python's logging module so coordinator.log
becomes an actual traceable record instead of a decorative stream.

Every stage of one inbound message's journey — Signal receive, router,
routing decision, model call(s), Signal delivery — is tagged with the
same correlation ID, so the whole thing is reconstructable with one grep:

    grep "id=a3f8" coordinator.log

Sanitized by design: never logs message content, only sender labels and
structured metadata (model, tier, tokens, thinking on/off, stop_reason,
tools fired). This is independent from the buffer_<name>.txt conversational
logs, which do store raw content — that's a separate, unrelated concern.

Self-contained (no coordinator.py import) to avoid circular imports,
matching the existing pattern in skills/taxonomy.py. Uses a contextvar
rather than threading a trace_id through every function signature —
set once per message at the top of worker()'s loop, readable from
anywhere downstream (including other skill modules) without touching
every call site's parameters. Contextvars are asyncio-task-aware, so
concurrent messages from different users stay correctly isolated.
"""

import logging
import uuid
import contextvars

import os as _os, sys as _sys
_cc = _os.path.dirname(_os.path.abspath(__file__))
while _cc != "/" and not _os.path.exists(_os.path.join(_cc, "chip_config.py")):
    _cc = _os.path.dirname(_cc)
if _cc not in _sys.path:
    _sys.path.insert(0, _cc)
from chip_config import WORKSPACE
_trace_ctx = contextvars.ContextVar("chip_trace", default=("-", "?"))

logger = logging.getLogger("chip_trace")
logger.setLevel(logging.INFO)
if not logger.handlers:
    _handler = logging.FileHandler(f"{WORKSPACE}/coordinator.log")
    _handler.setFormatter(logging.Formatter(
        "%(asctime)s %(message)s", datefmt="%Y-%m-%dT%H:%M:%S"
    ))
    logger.addHandler(_handler)
    logger.propagate = False  # don't also duplicate onto root/stdout


def new_trace_id():
    """8-char correlation ID, generated once per inbound message."""
    return uuid.uuid4().hex[:8]


def set_trace(trace_id, name):
    """Call once at message intake (top of worker()'s loop body)."""
    _trace_ctx.set((trace_id, name))


def get_trace():
    """Returns (trace_id, name) for whoever's message is currently
    being processed in this asyncio task's context."""
    return _trace_ctx.get()


def _prefix():
    trace_id, name = get_trace()
    return f"id={trace_id} [{name}]"


def log_signal_in():
    logger.info(f"{_prefix()} stage=signal_in")


def log_router(engine, result):
    """engine: 'quick_route' (deterministic, no LLM) or 'phi4-mini'.
    result: the route/intent decided, or 'fallback' on router failure."""
    logger.info(f"{_prefix()} stage=router engine={engine} result={result}")


def log_route_decision(tier, score, sections):
    logger.info(
        f"{_prefix()} stage=route_decision tier={tier} score={score:.2f} "
        f"sections={sections}"
    )


def log_model_call(model, thinking, tokens_in, tokens_out, stop_reason,
                    tools=None, round_num=1):
    """One line per actual API call. For multi-round tool-calling loops,
    call this once per round with the same round_num context incrementing.

    Also the single funnel into usage_log.db — every cloud call in the
    codebase already reports here, so cost tracking rides along for free
    rather than needing a log_usage() call bolted onto each site (audit H5).
    Callers must NOT also call log_usage() or spend double-counts."""
    tools = tools or []
    logger.info(
        f"{_prefix()} stage=model round={round_num} model={model} "
        f"thinking={'on' if thinking else 'off'} tokens_in={tokens_in} "
        f"tokens_out={tokens_out} stop_reason={stop_reason} tools={tools}"
    )
    try:
        from skills.usage_tracker import log_usage
        log_usage(model, tokens_in or 0, tokens_out or 0, get_trace()[1])
    except Exception as e:  # never let cost logging break a live call
        logger.info(f"{_prefix()} stage=usage_log_error err={e}")


def log_signal_out(target_suffix, status="delivered"):
    logger.info(f"{_prefix()} stage=signal_out target={target_suffix} status={status}")
