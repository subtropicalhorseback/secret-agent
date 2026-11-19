"""
usage_tracker.py — Chip API Usage Tracker
==========================================
Tracks token usage per model. Called by coordinator after every cloud API call.

Usage:
  !usage            current month
  !usage YYYY-MM    specific month
  !usage -v         verbose breakdown by model

Output: single clean summary. No duplication.
log_usage() is called by coordinator — import and use directly.
"""

import os
import sqlite3
import asyncio
from datetime import datetime
from dotenv import load_dotenv

import os as _os, sys as _sys
_cc = _os.path.dirname(_os.path.abspath(__file__))
while _cc != "/" and not _os.path.exists(_os.path.join(_cc, "chip_config.py")):
    _cc = _os.path.dirname(_cc)
if _cc not in _sys.path:
    _sys.path.insert(0, _cc)
from chip_config import WORKSPACE
load_dotenv(f"{WORKSPACE}/.env")

USAGE_DB = f"{WORKSPACE}/data/memory/usage_log.db"

from skills.registry import register

# Pricing per 1M tokens (input / output) — update as models change
PRICING = {
    # Anthropic
    "claude-sonnet-4-6": {"input": 3.00,  "output": 15.00, "provider": "anthropic"},
    "claude-sonnet-5": {"input": 3.00,   "output": 15.00, "provider": "anthropic"},  # intro $2/$10 expired 2026-08-31
    "claude-opus-5":   {"input": 5.00,   "output": 25.00, "provider": "anthropic"},
    "claude-fable-5":  {"input": 10.00,  "output": 50.00, "provider": "anthropic"},
    "claude-opus-4-6":   {"input": 5.00,  "output": 25.00, "provider": "anthropic"},
    "claude-haiku-4-5":  {"input": 0.80,  "output": 4.00,  "provider": "anthropic"},
    # Gemini — update to gemini-3 when GA
    "gemini-2.5-flash":  {"input": 0.30,  "output": 2.50,  "provider": "gemini"},
    "gemini-2.5-pro":    {"input": 1.25,  "output": 10.00, "provider": "gemini"},
    "gemini-3-flash":    {"input": 0.50,  "output": 3.00,  "provider": "gemini"},
    "gemini-3.5-flash":  {"input": 0.75,  "output": 3.75,  "provider": "gemini"},
    "gemini-3.5-flash-lite": {"input": 0.30, "output": 2.50, "provider": "gemini"},
    "gemini-3.1-pro":    {"input": 1.25,  "output": 10.00, "provider": "gemini"},
}

# ─── DB ───────────────────────────────────────────────────────────────────────

def ensure_db():
    os.makedirs(os.path.dirname(USAGE_DB), exist_ok=True)
    conn = sqlite3.connect(USAGE_DB)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS usage_log (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            ts            TEXT,
            model         TEXT,
            provider      TEXT,
            input_tokens  INTEGER,
            output_tokens INTEGER,
            sender        TEXT
        )
    """)
    conn.commit()
    conn.close()

# ─── Log (called by coordinator after every API call) ─────────────────────────

def log_usage(model, input_tokens, output_tokens, sender=""):
    """
    Call this after every cloud API response.
    Provider is inferred from model name.
    """
    try:
        ensure_db()
        p        = PRICING.get(model, {})
        provider = p.get("provider", "gemini" if "gemini" in model else "anthropic")
        conn     = sqlite3.connect(USAGE_DB)
        conn.execute(
            "INSERT INTO usage_log (ts, model, provider, input_tokens, output_tokens, sender) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (datetime.now().isoformat(), model, provider, input_tokens, output_tokens, sender)
        )
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"Usage log error: {e}")

# ─── Report ───────────────────────────────────────────────────────────────────

def get_usage_breakdown(period="today"):
    """Per-model and per-user spend for 'today', 'month', or 'YYYY-MM'.
    This is what Chip's get_usage_report tool returns (19 Nov 2026) — the
    old report hid the per-model lines the models needed to answer
    'what did Opus cost me this week'."""
    now = datetime.now()
    if period == "today":
        prefix, label = now.strftime("%Y-%m-%d"), f"today ({now:%b %-d})"
    elif period == "month":
        prefix, label = now.strftime("%Y-%m"), now.strftime("%B %Y")
    else:
        prefix, label = period, period

    ensure_db()
    conn = sqlite3.connect(USAGE_DB)
    by_model = conn.execute(
        "SELECT model, COUNT(*), SUM(input_tokens), SUM(output_tokens) "
        "FROM usage_log WHERE ts LIKE ? GROUP BY model "
        "ORDER BY SUM(input_tokens+output_tokens) DESC",
        (f"{prefix}%",)).fetchall()
    by_user = conn.execute(
        "SELECT sender, model, SUM(input_tokens), SUM(output_tokens) "
        "FROM usage_log WHERE ts LIKE ? GROUP BY sender, model",
        (f"{prefix}%",)).fetchall()
    conn.close()
    if not by_model:
        return f"No usage recorded for {label}."

    def _cost(model, inp, out):
        p = PRICING.get(model, {"input": 0, "output": 0})
        return (inp or 0) / 1e6 * p["input"] + (out or 0) / 1e6 * p["output"]

    lines, total = [f"API usage — {label}", "", "BY MODEL:"], 0.0
    for model, calls, inp, out in by_model:
        c = _cost(model, inp, out)
        total += c
        lines.append(f"  {model}: {calls} calls · "
                     f"{(inp or 0)/1000:.1f}k in / {(out or 0)/1000:.1f}k out · ${c:.3f}")
    lines.append(f"  TOTAL: ${total:.3f}")

    # sender column holds names (trace label) or phones depending on path
    user_costs = {}
    for sender, model, inp, out in by_user:
        from chip_config import name_by_phone
        key = (name_by_phone(sender) or sender or "system").lower()
        user_costs[key] = user_costs.get(key, 0.0) + _cost(model, inp, out)
    lines.append("")
    lines.append("BY USER: " + "  ".join(
        f"{u}: ${c:.3f}" for u, c in
        sorted(user_costs.items(), key=lambda x: -x[1])))
    return "\n".join(lines)


def get_monthly_report(year=None, month=None, verbose=False):
    now    = datetime.now()
    year   = year or now.year
    month  = month or now.month
    prefix = f"{year}-{month:02d}"

    ensure_db()
    conn = sqlite3.connect(USAGE_DB)
    rows = conn.execute("""
        SELECT model, provider, SUM(input_tokens), SUM(output_tokens), COUNT(*)
        FROM usage_log WHERE ts LIKE ?
        GROUP BY model ORDER BY SUM(input_tokens + output_tokens) DESC
    """, (f"{prefix}%",)).fetchall()
    conn.close()

    if not rows:
        return f"No usage recorded for {prefix}."

    # Aggregate by provider
    totals = {"anthropic": {"cost": 0.0, "tokens": 0, "calls": 0},
              "gemini":    {"cost": 0.0, "tokens": 0, "calls": 0}}
    model_lines = []

    for model, provider, inp, out, calls in rows:
        inp      = inp or 0
        out      = out or 0
        p        = PRICING.get(model, {"input": 0, "output": 0})
        cost     = (inp / 1_000_000 * p["input"]) + (out / 1_000_000 * p["output"])
        bucket   = provider if provider in totals else "gemini"
        totals[bucket]["cost"]   += cost
        totals[bucket]["tokens"] += inp + out
        totals[bucket]["calls"]  += calls
        model_lines.append(
            f"  {model}: {calls} calls · {(inp+out):,} tok · ${cost:.4f}"
        )

    total_cost  = totals["anthropic"]["cost"] + totals["gemini"]["cost"]
    total_calls = totals["anthropic"]["calls"] + totals["gemini"]["calls"]

    # Single clean summary — no duplication
    lines = [
        f"{prefix}  {total_calls} calls  ${total_cost:.4f} est.",
        f"  Claude:  {totals['anthropic']['calls']} calls · "
        f"{totals['anthropic']['tokens']:,} tok · ${totals['anthropic']['cost']:.4f}",
        f"  Gemini:  {totals['gemini']['calls']} calls · "
        f"{totals['gemini']['tokens']:,} tok · ${totals['gemini']['cost']:.4f}",
    ]

    if verbose:
        lines.append("")
        lines.append("By model:")
        lines.extend(model_lines)

    return "\n".join(lines)

# ─── Scheduled weekly report (Friday 0900 CST) ────────────────────────────────

async def weekly_report_task(session):
    """Background task — started in coordinator main()."""
    import pytz
    from coordinator import send_signal, USERS
    cst = pytz.timezone("America/Chicago")
    print("📊 Weekly usage reporter online.")
    while True:
        now    = datetime.now(cst)
        # Next Friday 09:00
        days_ahead = (4 - now.weekday()) % 7 or 7  # 4 = Friday
        target = now.replace(hour=9, minute=0, second=0, microsecond=0)
        target = target.replace(day=(now + __import__('datetime').timedelta(days=days_ahead)).day)
        wait   = (target - now).total_seconds()
        await asyncio.sleep(wait)
        report = get_monthly_report(verbose=False)
        for phone, info in USERS.items():
            if info["tier"] == "admin":
                await send_signal(session, f"[Chip] Weekly usage:\n{report}",
                                  recipient=phone)

# ─── Handler ──────────────────────────────────────────────────────────────────

@register("usage", help="API usage: !usage [YYYY-MM] [-v]")
async def handle_usage(session, sender, clean_text, rag_context, history):
    verbose = "-v" in (clean_text or "")
    text    = (clean_text or "").replace("-v", "").strip()

    year, month = None, None
    if text:
        try:
            parts = text.split("-")
            year  = int(parts[0])
            month = int(parts[1]) if len(parts) > 1 else None
        except Exception:
            pass

    loop   = asyncio.get_event_loop()
    report = await loop.run_in_executor(None, get_monthly_report, year, month, verbose)
    return report, False
