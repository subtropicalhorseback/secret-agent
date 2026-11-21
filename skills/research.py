"""
research.py - Chip Deep Research Skill
========================================
Registered intent: research

Multi-round iterative research:
  1. Query decomposition into sub-questions
  2. Parallel search legs per sub-question
  3. First synthesis + gap identification
  4. Follow-up searches on gaps
  5. Crosscheck (Claude + Gemini independent)
  6. Final synthesis with source citations and confidence ratings

Output always as .txt attachment (plain text, no markdown).
Ingested into vault on completion for future RAG retrieval.
"""

import os
import re
import json
import asyncio
import aiohttp
import anthropic
import sqlite3
import hashlib
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

GEMINI_KEY    = os.getenv("GEMINI_API_KEY")
ANTHROPIC_KEY = os.getenv("ANTHROPIC_API_KEY")
REPORT_DIR    = f"{WORKSPACE}/data/archive/reports"
VAULT_DB      = f"{WORKSPACE}/data/memory/chip_vault.db"

from skills.registry import register, cancel_flags
from skills import call_logger

# ── Model helpers ─────────────────────────────────────────────────────────────

async def gemini(session, prompt, model="gemini-3.5-flash", timeout=60):
    url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
           f"{model}:generateContent?key={GEMINI_KEY}")
    payload = {
        "contents":       [{"parts": [{"text": prompt}]}],
        "generationConfig": {"maxOutputTokens": 4096},
    }
    for attempt in range(3):
        try:
            async with session.post(url, json=payload,
                                    timeout=aiohttp.ClientTimeout(total=timeout)) as r:
                r.raise_for_status()
                data  = await r.json()
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
                await asyncio.sleep(10)
            else:
                call_logger.log_model_call(
                    model=model, thinking=False, tokens_in=0, tokens_out=0,
                    stop_reason=f"error:{e}", tools=[],
                )
                return f"[Gemini error: {e}]"

from skills import llm as _llm

def claude_sync(prompt, system=None, model=_llm.OPUS):
    """Delegates to skills.llm — Opus 5, effort high, usage logged there."""
    return _llm.claude_sync(prompt, system=system, model=model)

async def claude(prompt, system=None, model=_llm.OPUS):
    return await _llm.claude(prompt, system=system, model=model)

# ── Web search ────────────────────────────────────────────────────────────────

def search_sync(query, max_results=5):
    from skills import gateway        # logged in the web log (spec C.3)
    results = gateway.search(query, max_results=max_results)
    if isinstance(results, str):
        return []
    return results or []

async def search(query, max_results=5):
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, search_sync, query, max_results)

def format_results(results):
    if not results:
        return "No results found."
    return "\n---\n".join([
        f"Title: {r.get('title','')}\nURL: {r.get('href','')}\nSnippet: {r.get('body','')}"
        for r in results
    ])

def extract_sources(results):
    return [r.get('href','') for r in results if r.get('href')]

# ── Strip markdown ────────────────────────────────────────────────────────────

def strip(text):
    text = re.sub(r'#{1,6}\s+', '', text, flags=re.MULTILINE)
    text = re.sub(r'\*{1,3}(.*?)\*{1,3}', r'\1', text, flags=re.DOTALL)
    text = re.sub(r'_{1,3}(.*?)_{1,3}', r'\1', text, flags=re.DOTALL)
    text = re.sub(r'`{1,3}.*?`{1,3}', '', text, flags=re.DOTALL)
    text = re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()

# ── Vault ingest ──────────────────────────────────────────────────────────────

def ingest_to_vault(content, filename):
    try:
        from skills.vault_ingest import ingest_text
        ingest_text(content, filename, domain="research", source_type="research")
    except Exception as e:
        print(f"Vault ingest error: {e}")

# ── Delivery ──────────────────────────────────────────────────────────────────

def _save_to_disk(sender, content, query):
    """Save research report to disk and ingest to vault. No Signal send."""
    from coordinator import USERS
    os.makedirs(REPORT_DIR, exist_ok=True)
    ts      = datetime.now().strftime("%Y%m%d_%H%M%S")
    name    = USERS.get(sender, {}).get("name", "user").lower()
    safe    = re.sub(r'[^\w]', '_', query)[:30]
    path    = f"{REPORT_DIR}/{ts}_{name}_research_{safe}.txt"
    cleaned = strip(content)
    with open(path, "w") as f:
        f.write(cleaned)
    # Use actual disk filename so fetch: can find it later
    import glob as _glob
    matches = sorted(_glob.glob(f"{REPORT_DIR}/{ts}_{name}_research_*.txt"))
    disk_filename = os.path.basename(matches[-1]) if matches else f"research_{safe}_{ts}.txt"
    ingest_to_vault(cleaned, disk_filename)
    return cleaned

async def deliver(session, sender, content, query):
    """Legacy: save + send. Kept for any external callers."""
    from coordinator import send_signal_file
    cleaned = _save_to_disk(sender, content, query)
    safe    = re.sub(r'[^\w]', '_', query)[:30]
    ts      = datetime.now().strftime("%Y%m%d_%H%M%S")
    from coordinator import USERS
    name    = USERS.get(sender, {}).get("name", "user").lower()
    path    = f"{REPORT_DIR}/{ts}_{name}_research_{safe}.txt"
    async with aiohttp.ClientSession() as s:
        await send_signal_file(s, path, f"Research: {query[:50]}", sender)

# ── Research pipeline ─────────────────────────────────────────────────────────

@register("research", help="Deep iterative research: !research <query>")
async def handle_research(session, sender, clean_text, rag_context, history):
    from coordinator import send_signal

    if not clean_text:
        return "Usage: !research <query>", False

    cancel_flags[sender] = False

    # Pipeline context separator — split query from piped stage output
    # e.g. "{question} | research:" passes stage-1 answer as context blob
    _CTX_MARKER = "\n\nCONTEXT FROM PREVIOUS STEP:\n"
    if _CTX_MARKER in clean_text:
        _query_part, _pipe_ctx = clean_text.split(_CTX_MARKER, 1)
        query = _query_part.strip()
        rag_context = (_pipe_ctx + "\n\n" + rag_context).strip() if rag_context else _pipe_ctx.strip()
    else:
        query = clean_text

    all_sources = []

    from coordinator import working as _working
    await _working()   # ⏳ instead of a progress message (One Voice A.2)
    print('⋯ progress:', f"Starting deep research: {query[:60]}...\n" f"This takes 3-6 min. Reply !stop to cancel.")

    # ── Round 1: Query decomposition ──────────────────────────────────────────
    print('⋯ progress:', "Round 1: Decomposing query...")

    decomp_prompt = (
        f"Break this research query into 4-5 specific sub-questions that together "
        f"would fully answer it. Output ONLY a JSON array of strings, nothing else.\n\n"
        f"Query: {query}"
    )
    decomp_raw = await gemini(session, decomp_prompt)
    try:
        decomp_raw = decomp_raw.strip().replace("```json","").replace("```","").strip()
        sub_questions = json.loads(decomp_raw)
        if not isinstance(sub_questions, list):
            sub_questions = [query]
    except Exception:
        sub_questions = [query]

    if cancel_flags.get(sender):
        return "Research cancelled.", False

    # ── Round 1: Search ───────────────────────────────────────────────────────
    print('⋯ progress:', f"Searching {len(sub_questions)} sub-questions...")

    round1_results = {}
    for sq in sub_questions[:5]:
        # Enrich query
        enrich_prompt = (
            f"Rewrite as a self-contained web search query (1 line, no quotes):\n{sq}"
        )
        enriched = await gemini(session, enrich_prompt, timeout=10)
        enriched = enriched.strip().split('\n')[0]
        results  = await search(enriched, max_results=5)
        all_sources.extend(extract_sources(results))
        block = format_results(results)
        # Phase 3 (browser spec, 19 Nov 2026): research reads the TOP PAGE
        # per sub-question through the live browser instead of synthesizing
        # from snippets alone. Static scraper fallback; silent skip on failure.
        top = next((r.get("href") for r in results
                    if isinstance(r, dict) and r.get("href")), None)
        if top:
            page_text = ""
            try:
                from skills.browse import fetch_page, service_up
                if service_up():
                    page_text = await fetch_page(top, user="research")
            except Exception:
                page_text = ""
            if not page_text:
                try:
                    from skills import gateway
                    page_text, _meta = await asyncio.to_thread(gateway.fetch, top)
                except Exception:
                    page_text = ""
            if page_text and "Search Error" not in page_text \
                    and "Mullvad is down" not in page_text:
                block += f"\n\nFULL PAGE ({top}):\n{page_text[:3000]}"
        round1_results[sq] = block

    if cancel_flags.get(sender):
        return "Research cancelled.", False

    # ── Round 1: First synthesis ──────────────────────────────────────────────
    print('⋯ progress:', "Synthesizing round 1 findings...")

    search_dump = "\n\n".join([
        f"SUB-QUESTION: {sq}\nRESULTS:\n{res}"
        for sq, res in round1_results.items()
    ])

    # Include vault RAG context if available
    vault_section = f"\nVAULT CONTEXT:\n{rag_context}\n" if rag_context else ""

    synthesis1_prompt = (
        f"You are a research analyst. Based on the search results below, "
        f"write a thorough initial assessment of: {query}\n\n"
        f"After the assessment, list 2-3 specific GAPS or unresolved questions "
        f"that need further research. Label them clearly as GAPS.\n\n"
        f"{vault_section}"
        f"SEARCH RESULTS:\n{search_dump}"
    )
    synthesis1 = await claude(synthesis1_prompt)

    if cancel_flags.get(sender):
        return "Research cancelled.", False

    # ── Round 2: Gap identification and follow-up search ─────────────────────
    print('⋯ progress:', "Round 2: Filling gaps...")

    gap_prompt = (
        f"Extract the GAPS from this research draft as a JSON array of search queries. "
        f"Output ONLY the JSON array, nothing else.\n\n{synthesis1}"
    )
    gap_raw = await gemini(session, gap_prompt)
    try:
        gap_raw  = gap_raw.strip().replace("```json","").replace("```","").strip()
        gap_queries = json.loads(gap_raw)
        if not isinstance(gap_queries, list):
            gap_queries = []
    except Exception:
        gap_queries = []

    round2_results = {}
    for gq in gap_queries[:3]:
        results = await search(str(gq), max_results=5)
        all_sources.extend(extract_sources(results))
        round2_results[str(gq)] = format_results(results)

    if cancel_flags.get(sender):
        return "Research cancelled.", False

    # ── Round 2: Refined synthesis ────────────────────────────────────────────
    print('⋯ progress:', "Refining synthesis...")

    gap_dump = "\n\n".join([
        f"GAP QUERY: {gq}\nRESULTS:\n{res}"
        for gq, res in round2_results.items()
    ]) if round2_results else "No additional results."

    synthesis2_prompt = (
        f"You are a research analyst refining an initial assessment.\n\n"
        f"ORIGINAL QUERY: {query}\n\n"
        f"INITIAL ASSESSMENT:\n{synthesis1}\n\n"
        f"ADDITIONAL RESEARCH (gap-filling):\n{gap_dump}\n\n"
        f"Produce a refined, comprehensive assessment that:\n"
        f"1. Integrates all findings\n"
        f"2. Resolves gaps where possible\n"
        f"3. Uses confidence ratings: Confirmed, High, Medium, Low, Uncertain\n"
        f"4. Flags where sources conflict\n"
        f"5. Notes what remains unknown"
    )
    synthesis2 = await claude(synthesis2_prompt)

    if cancel_flags.get(sender):
        return "Research cancelled.", False

    # ── Crosscheck ────────────────────────────────────────────────────────────
    print('⋯ progress:', "Crosscheck: Independent validation...")

    crosscheck_prompt = (
        f"You are independently reviewing a research assessment. "
        f"Evaluate critically:\n\n"
        f"QUERY: {query}\n\n"
        f"ASSESSMENT:\n{synthesis2}\n\n"
        f"1. What claims are well-supported?\n"
        f"2. What claims are overstated or under-evidenced?\n"
        f"3. What important angles are missing?\n"
        f"4. Overall: does this assessment hold up?\n\n"
        f"Be direct and specific."
    )

    claude_check  = asyncio.create_task(claude(crosscheck_prompt))
    gemini_check  = asyncio.create_task(
        gemini(session, crosscheck_prompt, model="gemini-3.5-flash", timeout=120)
    )
    claude_result, gemini_result = await asyncio.gather(claude_check, gemini_check)

    if cancel_flags.get(sender):
        return "Research cancelled.", False

    # ── Final synthesis ───────────────────────────────────────────────────────
    print('⋯ progress:', "Final synthesis...")

    final_prompt = (
        f"You are producing a final research report. Integrate all findings.\n\n"
        f"ORIGINAL QUERY: {query}\n\n"
        f"RESEARCH ASSESSMENT:\n{synthesis2}\n\n"
        f"CLAUDE CROSSCHECK:\n{claude_result}\n\n"
        f"GEMINI CROSSCHECK:\n{gemini_result}\n\n"
        f"Produce the final report:\n"
        f"- Clear direct answer to the query upfront\n"
        f"- Key findings with confidence ratings\n"
        f"- Where crosschecks agreed vs diverged\n"
        f"- What remains uncertain and why\n"
        f"- 3-5 concrete implications or recommendations\n\n"
        f"Plain prose. No markdown headers. No bullet overload."
    )
    # Final synthesis is the adjudication step — Fable 5 at xhigh (house policy)
    final = await _llm.adjudicate(final_prompt)

    # ── Assemble report ───────────────────────────────────────────────────────
    unique_sources = list(dict.fromkeys(s for s in all_sources if s))[:15]
    sources_text   = "\n".join([f"- {s}" for s in unique_sources])

    report = (
        f"DEEP RESEARCH REPORT\n"
        f"Query: {query}\n"
        f"Date: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n"
        f"Rounds: 2 search rounds, {len(all_sources)} sources consulted\n"
        f"{'='*50}\n\n"
        f"{strip(final)}\n\n"
        f"{'='*50}\n"
        f"SOURCES CONSULTED:\n{sources_text}\n"
    )

    cleaned = _save_to_disk(sender, report, query)
    cancel_flags.pop(sender, None)
    # Return full content — coordinator's deliver_response sends it + log_to_buffer logs it
    return cleaned, True
