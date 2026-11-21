"""
analyze.py — Chip Analysis Skill
==============================================
Registered intents: analyze, crosscheck

Usage:
  !analyze <target> [focus]
    !analyze Acme Corp AI strategy and capex
    !analyze Freedonia energy policy
    !analyze Acme Corp regulatory pressure
    !analyze Acme Corp compare to Globex on AI infrastructure

  !crosscheck <question or claim>
  !stop — cancel in-flight analysis

Features:
  - Entity + focus extraction via Gemini Flash
  - Mode 0-3 sequential pipeline with per-mode checkpointing
  - Each mode saved to disk as it completes (restart-safe)
  - Indexed in chip_context.db for !read search
  - Active report context per sender (2hr expiry) for follow-up questions
  - Focus directive threads through all mode prompts
"""

import os
import re
import json
import asyncio
import aiohttp
import sqlite3
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
from chip_config import ANALYSIS_LANGUAGE as _ANALYSIS_LANG
load_dotenv(f"{WORKSPACE}/.env")

GEMINI_KEY    = os.getenv("GEMINI_API_KEY")
ANTHROPIC_KEY = os.getenv("ANTHROPIC_API_KEY")
MODES_DIR     = f"{WORKSPACE}/agents/chip/modes"
REPORT_DIR    = f"{WORKSPACE}/data/archive/reports"
ANALYZE_DIR   = f"{WORKSPACE}/data/archive/analyze"
CONTEXT_DB    = f"{WORKSPACE}/data/memory/chip_context.db"

from skills.registry import register, cancel_flags
from skills import call_logger
import re as _strip_re

def clean(text):
    text = _strip_re.sub(r'#{1,6}\s+', '', text, flags=_strip_re.MULTILINE)
    text = _strip_re.sub(r'\*{1,3}(.*?)\*{1,3}', r'\1', text, flags=_strip_re.DOTALL)
    text = _strip_re.sub(r'_{1,3}(.*?)_{1,3}', r'\1', text, flags=_strip_re.DOTALL)
    text = _strip_re.sub(r'`{1,3}.*?`{1,3}', '', text, flags=_strip_re.DOTALL)
    text = _strip_re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', text)
    text = _strip_re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


# Active report context — per sender, expires 2 hours
# {sender: {target, focus, report_dir, ts}}
active_reports = {}

ACTIVE_REPORT_TTL = 2  # hours

def set_active_report(sender, target, focus, report_dir):
    print(f'DEBUG set_active_report: {sender} -> {target}')
    active_reports[sender] = {
        "target":     target,
        "focus":      focus,
        "report_dir": report_dir,
        "ts":         datetime.now(),
    }

def get_active_report(sender):
    r = active_reports.get(sender)
    if not r:
        return None
    if (datetime.now() - r["ts"]) > timedelta(hours=ACTIVE_REPORT_TTL):
        active_reports.pop(sender, None)
        return None
    return r

def clear_active_report(sender):
    active_reports.pop(sender, None)

# ── Storage and indexing ──────────────────────────────────────────────────────

def ensure_analyze_db():
    conn = sqlite3.connect(CONTEXT_DB)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS analyze_reports (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            target      TEXT,
            target_type TEXT,
            focus       TEXT,
            run_ts      TEXT,
            report_dir  TEXT,
            mode3_summary TEXT,
            completed   INTEGER DEFAULT 0
        )
    """)
    conn.execute("""
        CREATE VIRTUAL TABLE IF NOT EXISTS analyze_fts
        USING fts5(target, focus, mode3_summary,
                   content='analyze_reports', content_rowid='id')
    """)
    conn.commit()
    conn.close()

def index_report(target, target_type, focus, run_ts, report_dir, mode3_summary, completed=1):
    try:
        ensure_analyze_db()
        conn = sqlite3.connect(CONTEXT_DB)
        # Upsert by report_dir
        existing = conn.execute(
            "SELECT id FROM analyze_reports WHERE report_dir=?", (report_dir,)
        ).fetchone()
        if existing:
            conn.execute("""
                UPDATE analyze_reports
                SET completed=?, mode3_summary=?
                WHERE report_dir=?
            """, (completed, mode3_summary, report_dir))
            rowid = existing[0]
        else:
            conn.execute("""
                INSERT INTO analyze_reports
                (target, target_type, focus, run_ts, report_dir, mode3_summary, completed)
                VALUES (?,?,?,?,?,?,?)
            """, (target, target_type, focus, run_ts, report_dir, mode3_summary, completed))
            rowid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.execute("""
                INSERT INTO analyze_fts(rowid, target, focus, mode3_summary)
                VALUES (?,?,?,?)
            """, (rowid, target, focus or "", mode3_summary or ""))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"Analyze index error: {e}")

def get_prior_reports(target, max_reports=3):
    """Fetch up to N completed prior reports for a target."""
    try:
        ensure_analyze_db()
        conn  = sqlite3.connect(CONTEXT_DB)
        safe  = re.sub(r'[^\w\s]', '', target.lower())
        rows  = conn.execute("""
            SELECT run_ts, report_dir, focus, mode3_summary
            FROM analyze_reports
            WHERE lower(target) LIKE ? AND completed=1
            ORDER BY run_ts DESC LIMIT ?
        """, (f"%{safe[:20]}%", max_reports)).fetchall()
        conn.close()
        return rows
    except Exception:
        return []

def get_report_dir(target, run_ts):
    safe = re.sub(r'[^\w]', '_', target)[:40]
    path = f"{ANALYZE_DIR}/{safe}/{run_ts}"
    os.makedirs(path, exist_ok=True)
    return path

def save_checkpoint(report_dir, mode_name, content):
    with open(f"{report_dir}/{mode_name}.txt", "w") as f:
        f.write(content)

def load_checkpoint(report_dir, mode_name):
    path = f"{report_dir}/{mode_name}.txt"
    if os.path.exists(path):
        return open(path).read()
    return None

def find_incomplete_run(target):
    """Check if there's an interrupted run for this target."""
    safe = re.sub(r'[^\w]', '_', target)[:40]
    base = f"{ANALYZE_DIR}/{safe}"
    if not os.path.exists(base):
        return None
    runs = sorted(os.listdir(base), reverse=True)
    for run in runs:
        run_dir = f"{base}/{run}"
        if not os.path.exists(f"{run_dir}/full_report.txt"):
            # Incomplete — check what we have
            completed = [m for m in ["mode0","mode1","mode2","mode3","crosscheck"]
                        if os.path.exists(f"{run_dir}/{m}.txt")]
            if completed:
                return run_dir, completed
    return None

# ── Model helpers ─────────────────────────────────────────────────────────────

async def gemini(session, prompt, model="gemini-3.5-flash", timeout=180):
    url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
           f"{model}:generateContent?key={GEMINI_KEY}")
    payload = {
        "contents":       [{"parts": [{"text": prompt}]}],
        "generationConfig": {"maxOutputTokens": 8192},
    }
    for attempt in range(3):
        try:
            async with session.post(url, json=payload,
                                    timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
                resp.raise_for_status()
                data  = await resp.json()
                usage = data.get("usageMetadata", {})
                call_logger.log_model_call(
                    model=model, thinking=False,
                    tokens_in=usage.get("promptTokenCount", 0),
                    tokens_out=usage.get("candidatesTokenCount", 0),
                    stop_reason=data.get("candidates", [{}])[0].get("finishReason", "?"),
                    tools=[],
                )
                return data["candidates"][0]["content"]["parts"][0]["text"]
        except Exception as e:
            if attempt < 2:
                print(f"Gemini attempt {attempt+1} failed: {e} — retrying in 15s")
                await asyncio.sleep(15)
            else:
                call_logger.log_model_call(
                    model=model, thinking=False, tokens_in=0, tokens_out=0,
                    stop_reason=f"error:{e}", tools=[],
                )
                raise

from skills import llm as _llm

def claude_sync(prompt, model=_llm.OPUS, system=None):
    """Delegates to skills.llm — Opus 5, effort high, usage logged there."""
    return _llm.claude_sync(prompt, system=system, model=model)

async def claude(prompt, model=_llm.OPUS, system=None):
    return await _llm.claude(prompt, system=system, model=model)

# ── Entity + focus extraction ─────────────────────────────────────────────────

async def detect_entity_and_focus(session, raw_input):
    """
    Extract entity type, full name, focus directive, and optional comparison target.
    Handles:
      'Acme AI strategy'           -> company, Acme Corporation, 'AI strategy', None
      'Freedonia energy policy'    -> country, Freedonia, 'energy policy', None
      'Acme compare to Globex...'  -> company, Acme Corporation, '...', Globex
    """
    prompt = (
        f"Parse this analysis request and return JSON only, no other text:\n\n"
        f"Input: {raw_input}\n\n"
        f"Return: {{\"type\": \"country\" or \"company\", "
        f"\"full_name\": \"expanded full name\", "
        f"\"focus\": \"analytical focus if any, else null\", "
        f"\"comparison_target\": \"second entity if comparison requested, else null\"}}"
    )
    try:
        result = await gemini(session, prompt, model="gemini-3.5-flash", timeout=15)
        result = result.strip().replace("```json","").replace("```","").strip()
        return json.loads(result)
    except Exception as e:
        print(f"Entity detection failed: {e}")
        words = raw_input.split()
        return {
            "type":               "company" if len(words[0]) <= 5 and words[0].isupper() else "country",
            "full_name":          words[0].title(),
            "focus":              " ".join(words[1:]) if len(words) > 1 else None,
            "comparison_target":  None,
        }

# ── Mode prompt loader ────────────────────────────────────────────────────────

def load_mode(filename):
    path = os.path.join(MODES_DIR, filename)
    try:
        return open(path).read()
    except Exception as e:
        return f"[Mode prompt not found: {filename} — {e}]"

def inject_focus(prompt, focus):
    if not focus:
        return prompt
    return f"ANALYTICAL FOCUS: {focus}\n\nThis focus should shape emphasis throughout your analysis.\n\n{prompt}"

def inject_prior_reports(prompt, prior_rows, mode_name):
    if not prior_rows:
        return prompt
    prior_text = "\n\n".join([
        f"PRIOR ASSESSMENT ({row[0][:10]}, focus: {row[2] or 'general'}):\n{row[3][:1000]}"
        for row in prior_rows
    ])
    return (
        f"{prompt}\n\n"
        f"PRIOR ASSESSMENTS FOR CONTEXT (note continuity and divergence):\n"
        f"{prior_text}"
    )

# ── Mode 0 — Quantitative ─────────────────────────────────────────────────────

async def run_mode0(session, full_name, focus):
    try:
        import yfinance as yf
        ticker_guess = full_name.upper().split()[0]
        t    = yf.Ticker(ticker_guess)
        info = t.info

        price     = info.get("currentPrice") or info.get("regularMarketPrice", "N/A")
        week52hi  = info.get("fiftyTwoWeekHigh", "N/A")
        week52lo  = info.get("fiftyTwoWeekLow", "N/A")
        pe_ttm    = info.get("trailingPE", "N/A")
        pe_fwd    = info.get("forwardPE", "N/A")
        ps        = info.get("priceToSalesTrailing12Months", "N/A")
        pb        = info.get("priceToBook", "N/A")
        de        = info.get("debtToEquity", "N/A")
        cash      = info.get("totalCash", "N/A")
        short_pct = info.get("shortPercentOfFloat", "N/A")
        rec       = info.get("recommendationKey", "N/A")
        sector    = info.get("sector", "N/A")
        capex     = info.get("capitalExpenditures", "N/A")
        rd        = info.get("researchAndDevelopment", "N/A")
        rev       = info.get("totalRevenue", "N/A")
        ebitda    = info.get("ebitda", "N/A")

        data_str = (
            f"Ticker: {ticker_guess} | Sector: {sector}\n"
            f"Price: {price} | 52W: {week52lo} - {week52hi}\n"
            f"P/E TTM: {pe_ttm} | Fwd P/E: {pe_fwd} | P/S: {ps} | P/B: {pb}\n"
            f"D/E: {de} | Cash: {cash} | EBITDA: {ebitda}\n"
            f"Revenue: {rev} | Capex: {capex} | R&D: {rd}\n"
            f"Short %: {short_pct} | Analyst: {rec}\n"
        )

        template = load_mode("mode0_quant.md")
        prompt   = inject_focus(
            template.replace("[TARGET]", full_name).replace("[MODE0_DATA]", data_str),
            focus
        )
        return await claude(prompt)
    except Exception as e:
        return f"[Mode 0 skipped — {e}]"

# ── Web search helper ─────────────────────────────────────────────────────────

def web_search_sync(query, max_results=5):
    from skills import gateway        # logged in the web log (spec C.3)
    results = gateway.search(query, max_results=max_results)
    if isinstance(results, str):
        return results
    return "\n---\n".join([
        f"Source: {r.get('title')}\nLink: {r.get('href')}\nSnippet: {r.get('body')}"
        for r in results
    ])

async def web_search(query, max_results=5):
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, web_search_sync, query, max_results)

# ── Report delivery ───────────────────────────────────────────────────────────

def _save_report_to_disk(content, sender, title, report_dir):
    """Save report to REPORT_DIR and checkpoint. No Signal send."""
    from coordinator import USERS
    os.makedirs(REPORT_DIR, exist_ok=True)
    ts       = datetime.now().strftime("%Y%m%d_%H%M%S")
    name     = USERS.get(sender, {}).get("name", "user").lower()
    filepath = f"{REPORT_DIR}/{ts}_{name}_{title[:30]}.md"
    with open(filepath, "w") as f:
        f.write(f"{title}\n{datetime.now().strftime('%Y-%m-%d %H:%M')}\n\n")
        f.write(clean(content))
    save_checkpoint(report_dir, "full_report", content)
    return filepath

async def deliver_report(session, content, sender, title, report_dir):
    """Save report and send as Signal attachment (legacy path, not called internally)."""
    filepath = _save_report_to_disk(content, sender, title, report_dir)
    from coordinator import send_signal_file
    async with aiohttp.ClientSession() as s:
        await send_signal_file(s, filepath, f"Analysis: {title}", sender)

# ── !analyze handler ──────────────────────────────────────────────────────────

@register("analyze", help="Structured analysis: !analyze <target> [focus]")
async def handle_analyze(session, sender, clean_text, rag_context, history):
    from coordinator import send_signal

    if not clean_text:
        return "Usage: !analyze <target> [focus]", False

    cancel_flags[sender] = False

    # Check for explicit diff request
    if clean_text.strip().lower().endswith(' diff') or 'diff' == clean_text.strip().lower().split()[-1]:
        base_target = ' '.join(clean_text.strip().split()[:-1])
        entity      = await detect_entity_and_focus(session, base_target)
        full_name   = entity.get('full_name', base_target)
        print('⋯ progress:', f'Diffing last two {full_name} reports...')
        answer, force_file = await run_diff(session, sender, full_name, None)
        _save_report_to_disk(answer, sender,
            f'Diff_{full_name.replace(" ","_")}',
            get_report_dir(full_name, datetime.now().strftime('%Y%m%d_%H%M%S')))
        cancel_flags.pop(sender, None)
        return answer, True

    # Extract entity and focus
    entity        = await detect_entity_and_focus(session, clean_text)
    etype         = entity.get("type", "country")
    full_name     = entity.get("full_name", clean_text)
    focus         = entity.get("focus")
    comp_target   = entity.get("comparison_target")

    run_ts     = datetime.now().strftime("%Y%m%d_%H%M%S")
    report_dir = get_report_dir(full_name, run_ts)

    # Check for incomplete prior run
    incomplete = find_incomplete_run(full_name)
    resume_from = []
    if incomplete:
        inc_dir, completed_modes = incomplete
        print('⋯ progress:', f"Found incomplete run with {len(completed_modes)} mode(s) completed " f"({', '.join(completed_modes)}). Resuming...")
        report_dir  = inc_dir
        resume_from = completed_modes
        run_ts      = os.path.basename(inc_dir)

    focus_str = f" — {focus}" if focus else ""
    modes_str = "Modes 0-3" if etype == "company" else "Modes 1-3"
    from coordinator import working as _working
    await _working()   # ⏳ instead of a progress message (One Voice A.2)
    print('⋯ progress:', f"Analyzing {full_name}{focus_str} as a {etype}. " f"{modes_str} + crosscheck. ~4-8 min. Reply !stop to cancel.")

    # Index as in-progress
    index_report(full_name, etype, focus, run_ts, report_dir, None, completed=0)

    outputs      = {}
    prior_rows   = get_prior_reports(full_name)

    # Mode 0
    if etype == "company":
        if "mode0" in resume_from:
            outputs["mode0"] = load_checkpoint(report_dir, "mode0")
        else:
            print('⋯ progress:', "Mode 0: Quantitative baseline...")
            outputs["mode0"] = await run_mode0(session, full_name, focus)
            save_checkpoint(report_dir, "mode0", outputs["mode0"])
        if cancel_flags.get(sender):
            return "Analysis cancelled after Mode 0.", False

    # Mode 1
    if "mode1" in resume_from:
        outputs["mode1"] = load_checkpoint(report_dir, "mode1")
    else:
        print('⋯ progress:', "Mode 1: Historical context...")
        m1_template = load_mode("mode1_historical.md")
        m1_prompt   = inject_focus(
            inject_prior_reports(
                m1_template.replace("[TARGET]", full_name)
                           .replace("[RAG_CONTEXT]", rag_context or "None available."),
                prior_rows, "mode1"
            ),
            focus
        )
        outputs["mode1"] = await claude(m1_prompt)
        save_checkpoint(report_dir, "mode1", outputs["mode1"])
    if cancel_flags.get(sender):
        return "Analysis cancelled after Mode 1.", False

    # Mode 2
    if "mode2" in resume_from:
        outputs["mode2"] = load_checkpoint(report_dir, "mode2")
    else:
        print('⋯ progress:', "Mode 2: Leadership profiles...")
        m2_template = load_mode("mode2_leadership.md")
        m2_prompt   = inject_focus(
            m2_template.replace("[TARGET]", full_name)
                       .replace("[MODE1_OUTPUT]", outputs["mode1"]),
            focus
        )
        outputs["mode2"] = await claude(m2_prompt)
        save_checkpoint(report_dir, "mode2", outputs["mode2"])
    if cancel_flags.get(sender):
        return "Analysis cancelled after Mode 2.", False

    # Mode 3
    if "mode3" in resume_from:
        outputs["mode3"] = load_checkpoint(report_dir, "mode3")
    else:
        print('⋯ progress:', "Mode 3: Strategic synthesis...")
        m3_template = load_mode("mode3_synthesis.md")
        m3_prompt   = inject_focus(
            inject_prior_reports(
                m3_template.replace("[TARGET]", full_name)
                           .replace("[MODE0_OUTPUT]", outputs.get("mode0", "N/A"))
                           .replace("[MODE1_OUTPUT]", outputs["mode1"])
                           .replace("[MODE2_OUTPUT]", outputs["mode2"]),
                prior_rows, "mode3"
            ),
            focus
        )
        outputs["mode3"] = await claude(m3_prompt)
        save_checkpoint(report_dir, "mode3", outputs["mode3"])
    if cancel_flags.get(sender):
        return "Analysis cancelled after Mode 3.", False

    # Crosscheck
    if "crosscheck" in resume_from:
        outputs["crosscheck"] = load_checkpoint(report_dir, "crosscheck")
    else:
        print('⋯ progress:', "Crosscheck: Independent validation...")
        outputs["crosscheck"] = await run_crosscheck_internal(
            session,
            f"Validate and critique this Mode 3 assessment of {full_name}:\n\n{outputs['mode3']}",
            web_results=False
        )
        save_checkpoint(report_dir, "crosscheck", outputs["crosscheck"])

    # Compile full report
    report = ""
    if "mode0" in outputs:
        report += f"## Mode 0 — Quantitative Baseline\n\n{outputs['mode0']}\n\n---\n\n"
    report += f"## Mode 1 — Historical Context\n\n{outputs['mode1']}\n\n---\n\n"
    report += f"## Mode 2 — Leadership Profiles\n\n{outputs['mode2']}\n\n---\n\n"
    report += f"## Mode 3 — Strategic Synthesis\n\n{outputs['mode3']}\n\n---\n\n"
    report += f"## Crosscheck\n\n{outputs['crosscheck']}\n"

    title = f"Analysis_{full_name.replace(' ','_')}"
    if focus:
        title += f"_{focus[:20].replace(' ','_')}"

    _save_report_to_disk(report, sender, title, report_dir)

    # Index completed report
    m3_summary = outputs["mode3"][:500]
    index_report(full_name, etype, focus, run_ts, report_dir, m3_summary, completed=1)

    # Ingest report into vault for future RAG retrieval
    try:
        from skills.vault_ingest import ingest_analyze_report
        # Use actual disk filename so fetch: can find it later
        import glob, os
        pattern = f"{REPORT_DIR}/*_{full_name.replace(' ','_')[:30]}*.md"
        matches = sorted(glob.glob(pattern))
        disk_filename = os.path.basename(matches[-1]) if matches else None
        ingest_analyze_report(full_name, run_ts, report, filename=disk_filename)
    except Exception as e:
        print(f'Vault ingest error: {e}')

    # Set active report context for follow-ups
    set_active_report(sender, full_name, focus, report_dir)

    cancel_flags.pop(sender, None)
    # Return full report — coordinator's deliver_response sends it + log_to_buffer logs it
    return report, True

# ── Crosscheck core ───────────────────────────────────────────────────────────

async def run_crosscheck_internal(session, question, web_results=True):
    web_context = ""
    if web_results:
        web_data    = await web_search(question)
        web_context = f"LIVE WEB CONTEXT:\n{web_data}\n\n"

    full_prompt = f"{web_context}QUESTION/CLAIM:\n{question}"
    system      = (
        "You are a rigorous senior research analyst. "
        "Provide your independent assessment. "
        f"Be direct about uncertainty. Use {_ANALYSIS_LANG}."
    )

    claude_task  = claude(full_prompt, system=system)
    gemini_task  = gemini(session, f"{system}\n\n{full_prompt}")
    claude_resp, gemini_resp = await asyncio.gather(claude_task, gemini_task)

    arb_prompt = (
        f"You are arbitrating two independent assessments of the same question.\n\n"
        f"QUESTION:\n{question}\n\n"
        f"CLAUDE OPUS ASSESSMENT:\n{claude_resp}\n\n"
        f"GEMINI ASSESSMENT:\n{gemini_resp}\n\n"
        f"1. Where do they agree? Note confidence level.\n"
        f"2. Where do they diverge? Which is better supported and why?\n"
        f"3. Produce a final arbitrated assessment with the strongest elements of both.\n"
        f"4. Flag any claims neither model adequately supports.\n\n"
        f"Use {_ANALYSIS_LANG} throughout."
    )
    # Arbitration is the adjudication step — Fable 5 at xhigh (house policy)
    return await _llm.adjudicate(arb_prompt)


async def run_diff(session, sender, full_name, focus):
    """Compare two most recent Mode 3 reports for a target."""
    rows = get_prior_reports(full_name, max_reports=2)
    if len(rows) < 2:
        return f"Only {len(rows)} completed report(s) for {full_name} — need at least 2 to diff.", False

    newer_ts,  newer_dir,  newer_focus,  _  = rows[0]
    older_ts,  older_dir,  older_focus,  _  = rows[1]

    newer_m3 = open(f"{newer_dir}/mode3.txt").read() if os.path.exists(f"{newer_dir}/mode3.txt") else "[not found]"
    older_m3 = open(f"{older_dir}/mode3.txt").read() if os.path.exists(f"{older_dir}/mode3.txt") else "[not found]"

    prompt = (
        f"You are comparing two analytic assessments of {full_name} separated by time.\n\n"
        f"OLDER ASSESSMENT ({older_ts[:10]}, focus: {older_focus or 'general'}):\n"
        f"{older_m3[:4000]}\n\n"
        f"NEWER ASSESSMENT ({newer_ts[:10]}, focus: {newer_focus or 'general'}):\n"
        f"{newer_m3[:4000]}\n\n"
        f"{'DIFF FOCUS: ' + focus + chr(10) if focus else ''}"
        f"Analyze what changed between these assessments:\n"
        f"1. Key shifts in the strategic situation\n"
        f"2. Leadership or policy changes noted\n"
        f"3. Predictions from the older assessment that proved correct or incorrect\n"
        f"4. New risks or opportunities that emerged\n"
        f"5. What the delta implies for current assessment\n\n"
        f"Be specific about causation where possible. Use {_ANALYSIS_LANG}."
    )
    diff_result = await claude(prompt)

    report = (
        f"ANALYSIS DIFF: {full_name}\n"
        f"Comparing: {older_ts[:10]} vs {newer_ts[:10]}\n"
        f"{'='*50}\n\n"
        f"{diff_result}"
    )
    return report, True

# ── !crosscheck handler ───────────────────────────────────────────────────────

@register("crosscheck", help="Dual-model arbitration: !crosscheck <question or claim>")
async def handle_crosscheck(session, sender, clean_text, rag_context, history):
    from coordinator import send_signal

    if not clean_text:
        return "Usage: !crosscheck <question or claim>", False

    # Check if there's an active report — offer to crosscheck against it
    active = get_active_report(sender)
    if active and not clean_text.lower().startswith("!"):
        context_hint = (
            f"\n\nNote: the user has an active analysis of {active['target']} "
            f"(focus: {active['focus'] or 'general'}). "
            f"If this question relates to that analysis, incorporate it."
        )
        clean_text = clean_text + context_hint

    from coordinator import working as _working
    await _working()   # ⏳ instead of a progress message (One Voice A.2)
    print('⋯ progress:', "Crosscheck: Claude + Gemini independent assessment + arbitration. " "~2-3 min. Reply !stop to cancel.")

    cancel_flags[sender] = False
    result = await run_crosscheck_internal(session, clean_text, web_results=True)

    if cancel_flags.get(sender):
        return "Crosscheck cancelled.", False

    # Save to disk for archive/fetch; coordinator handles delivery + history logging
    os.makedirs(REPORT_DIR, exist_ok=True)
    ts          = datetime.now().strftime("%Y%m%d_%H%M%S")
    result_text = clean(result)
    filepath    = f"{REPORT_DIR}/{ts}_crosscheck.md"
    with open(filepath, "w") as f:
        f.write(f"Crosscheck\n{datetime.now().strftime('%Y-%m-%d %H:%M')}\n\n")
        f.write(result_text)

    # Ingest into vault — analyze: and research: already did this, chk: never did,
    # which is exactly why past crosscheck output (e.g. recipe research) was
    # recallable same-day via buffer history but never findable via vault: later.
    try:
        from skills.vault_ingest import ingest_text
        disk_filename = os.path.basename(filepath)
        ingest_text(result_text, disk_filename, domain="research", source_type="crosscheck")
    except Exception as e:
        print(f"Crosscheck vault ingest error: {e}")

    cancel_flags.pop(sender, None)
    # Return full content — coordinator's deliver_response sends it + log_to_buffer logs it
    return result_text, True
