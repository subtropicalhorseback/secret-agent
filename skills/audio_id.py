"""
audio_id.py - Chip Field Audio ID Skill
Wildlife sound identification from Signal voice memos.

Queue flow:
  Audio received  -> "Audio #N saved (N in queue)"
  User sends      -> "use 1,3,4 - bird in tree near pond"
                  -> "analyze all - not sure if frog or insect"
  Result          -> "It's a Chipping Sparrow, High confidence" + .txt attachment

Queue persists 7 days across restarts. Reports retained indefinitely.
"""

import os
import re
import json
import subprocess
import asyncio
import aiohttp
import anthropic
import sqlite3
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

GEMINI_KEY    = os.getenv("GEMINI_API_KEY")
ANTHROPIC_KEY = os.getenv("ANTHROPIC_API_KEY")
REPORT_DIR    = f"{WORKSPACE}/data/archive/reports"
AUDIO_DIR     = f"{WORKSPACE}/data/audio"
AUDIO_DB      = f"{WORKSPACE}/data/memory/audio_log.db"
QUEUE_DIR     = f"{WORKSPACE}/data/audio/queues"
QUEUE_MAX     = 5
QUEUE_TTL     = 7

from skills.registry import register
from skills import call_logger

# Queue management

def queue_path(sender):
    os.makedirs(QUEUE_DIR, exist_ok=True)
    return f"{QUEUE_DIR}/queue_{sender[-4:]}.json"

def load_queue(sender):
    path = queue_path(sender)
    if not os.path.exists(path):
        return []
    try:
        entries = json.load(open(path))
        cutoff  = (datetime.now() - timedelta(days=QUEUE_TTL)).isoformat()
        entries = [e for e in entries if e.get("ts", "") > cutoff]
        return entries
    except Exception:
        return []

def save_queue(sender, entries):
    json.dump(entries, open(queue_path(sender), "w"), indent=2)

def add_to_queue(sender, audio_path, content_type):
    entries = load_queue(sender)
    if len(entries) >= QUEUE_MAX:
        return None
    entries.append({
        "n":            len(entries) + 1,
        "path":         audio_path,
        "content_type": content_type,
        "ts":           datetime.now().isoformat(),
    })
    save_queue(sender, entries)
    return len(entries)

def clear_queue(sender):
    save_queue(sender, [])

def has_queue(sender):
    return len(load_queue(sender)) > 0

def parse_selection(text, queue):
    t       = text.strip().lower()
    context = text
    for prefix in ("use ", "analyze all", "analyze "):
        if t.startswith(prefix):
            context = text[len(prefix):].strip()
            break
    idx_match = re.match(r'^([\d,\s]+)[\s\-]+(.+)', context, re.DOTALL)
    if idx_match:
        raw_idx  = idx_match.group(1)
        context  = idx_match.group(2).strip()
        indices  = [int(x.strip()) for x in raw_idx.split(',') if x.strip().isdigit()]
        selected = [e for e in queue if e["n"] in indices]
    else:
        selected = queue
    return (selected or queue), context

# Audio log DB

def ensure_audio_db():
    os.makedirs(os.path.dirname(AUDIO_DB), exist_ok=True)
    conn = sqlite3.connect(AUDIO_DB)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS audio_sightings (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            recorded_at TEXT, location TEXT, season TEXT,
            moon_phase  TEXT, local_time TEXT, user_note TEXT,
            species     TEXT, confidence TEXT,
            gemini_raw  TEXT, claude_raw TEXT, audio_paths TEXT
        )
    """)
    conn.execute("""
        CREATE VIRTUAL TABLE IF NOT EXISTS audio_fts
        USING fts5(species, user_note, location, season,
                   content='audio_sightings', content_rowid='id')
    """)
    conn.commit()
    conn.close()

def log_sighting(ctx, user_note, species, confidence,
                 gemini_raw, claude_raw, audio_paths):
    try:
        ensure_audio_db()
        conn = sqlite3.connect(AUDIO_DB)
        conn.execute("""
            INSERT INTO audio_sightings
            (recorded_at, location, season, moon_phase, local_time,
             user_note, species, confidence, gemini_raw, claude_raw, audio_paths)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """, (
            datetime.now().isoformat(),
            ctx.get("location", ""), ctx.get("season", ""),
            ctx.get("moon_phase", ""), ctx.get("local_time", ""),
            user_note, species, confidence,
            gemini_raw, claude_raw, json.dumps(audio_paths),
        ))
        rowid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute("""
            INSERT INTO audio_fts(rowid, species, user_note, location, season)
            VALUES (?,?,?,?,?)
        """, (rowid, species, user_note,
              ctx.get("location", ""), ctx.get("season", "")))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"Audio log error: {e}")

# Utilities

def strip_markdown(text):
    text = re.sub(r'^#{1,6}\s+', '', text, flags=re.MULTILINE)
    text = re.sub(r'\*{1,3}(.*?)\*{1,3}', r'\1', text, flags=re.DOTALL)
    text = re.sub(r'_{1,3}(.*?)_{1,3}', r'\1', text, flags=re.DOTALL)
    text = re.sub(r'^[-*+]\s+', '- ', text, flags=re.MULTILINE)
    text = re.sub(r'`{1,3}.*?`{1,3}', '', text, flags=re.DOTALL)
    text = re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()

def convert_to_wav(input_path):
    os.makedirs(AUDIO_DIR, exist_ok=True)
    base     = os.path.splitext(os.path.basename(input_path))[0]
    out_path = f"{AUDIO_DIR}/{base}.wav"
    try:
        r = subprocess.run(
            ["ffmpeg", "-y", "-i", input_path,
             "-ar", "22050", "-ac", "1", "-f", "wav", out_path],
            capture_output=True, text=True, timeout=60
        )
        if r.returncode == 0:
            return out_path, "audio/wav"
        print(f"ffmpeg: {r.stderr[-200:]}")
    except Exception as e:
        print(f"Conversion failed: {e}")
    return input_path, "audio/aac"

def get_local_time(lat, lon):
    try:
        from timezonefinder import TimezoneFinder
        import pytz
        tf  = TimezoneFinder()
        tz  = tf.timezone_at(lat=float(lat), lng=float(lon))
        now = datetime.now(pytz.timezone(tz))
        return now.strftime("%Y-%m-%d %H:%M %Z"), now.strftime("%I:%M %p"), tz
    except Exception:
        return (datetime.now().strftime("%Y-%m-%d %H:%M"),
                datetime.now().strftime("%I:%M %p"), "UTC")

# Weather context

async def get_weather_context(session, location):
    try:
        loc = location.replace(" ", "+")
        async with session.get(
            f"https://wttr.in/{loc}?format=j1",
            timeout=aiohttp.ClientTimeout(total=15)
        ) as resp:
            resp.raise_for_status()
            data = await resp.json(content_type=None)
        cur    = data["current_condition"][0]
        ast    = data["weather"][0]["astronomy"][0]
        lat    = data["nearest_area"][0]["latitude"]
        lon    = data["nearest_area"][0]["longitude"]
        _, local_time, tz = get_local_time(lat, lon)
        month  = datetime.now().month
        season = ("Winter" if month in (12,1,2) else
                  "Spring" if month in (3,4,5) else
                  "Summer" if month in (6,7,8) else "Fall")
        return {
            "location":   location, "local_time": local_time,
            "timezone":   tz,       "season":     season,
            "conditions": cur["weatherDesc"][0]["value"],
            "temp_f":     cur["temp_F"],   "humidity":  cur["humidity"],
            "wind_mph":   cur["windspeedMiles"], "wind_dir": cur["winddir16Point"],
            "moon_phase": ast["moon_phase"],
            "sunrise":    ast["sunrise"],  "sunset":    ast["sunset"],
            "lat": lat, "lon": lon,
        }
    except Exception as e:
        return {"error": str(e), "location": location,
                "local_time": datetime.now().strftime("%I:%M %p")}

def format_context(ctx, user_caption, clip_count):
    if "error" in ctx:
        return (f"Location: {ctx['location']}\n"
                f"Clips: {clip_count}\nObserver note: {user_caption}")
    return (
        f"You are analyzing {clip_count} audio recording(s) captured in the field.\n\n"
        f"RECORDING CONTEXT\n"
        f"Location: {ctx['location']} (lat {ctx['lat']}, lon {ctx['lon']})\n"
        f"Local time: {ctx['local_time']} ({ctx['timezone']})\n"
        f"Season: {ctx['season']}\n"
        f"Conditions: {ctx['conditions']}, {ctx['temp_f']}F, "
        f"{ctx['humidity']}% humidity, {ctx['wind_mph']}mph {ctx['wind_dir']}\n"
        f"Moon: {ctx['moon_phase']} | Sunrise: {ctx['sunrise']} | Sunset: {ctx['sunset']}\n\n"
        f"OBSERVER NOTE: {user_caption}\n"
    )

# Gemini multi-clip analysis

GEMINI_PROMPT = (
    "TASK: Identify every distinct biological sound across all submitted recordings.\n\n"
    "The observer note is context only. The AUDIO is the primary evidence.\n"
    "Describe acoustic features before naming a species (pitch, rhythm, timbre, pattern).\n\n"
    "For each identified sound:\n"
    "- Species (common name + scientific name)\n"
    "- Acoustic evidence (specific features you hear)\n"
    "- Confidence: High / Medium / Low with reasoning\n"
    "- Behavioral context\n"
    "- Consistency with location, season, time, and weather\n"
    "- Top 2 alternatives and how to distinguish them\n\n"
    "If multiple clips contain the same sound, note whether they confirm each other.\n"
    "State clearly if you hear no biological sounds.\n"
    "Do not guess from location alone."
)

async def upload_audio(session, path, content_type):
    with open(path, "rb") as f:
        raw = f.read()
    headers = {
        "X-Goog-Upload-Command":  "start, upload, finalize",
        "X-Goog-Upload-Protocol": "raw",
        "X-Goog-Upload-Header-Content-Length": str(len(raw)),
        "X-Goog-Upload-Header-Content-Type":   content_type,
        "Content-Type": content_type,
    }
    async with session.post(
        f"https://generativelanguage.googleapis.com/upload/v1beta/files?key={GEMINI_KEY}",
        data=raw, headers=headers, timeout=aiohttp.ClientTimeout(total=120)
    ) as r:
        r.raise_for_status()
        data = await r.json()
    return data.get("file", {}).get("uri")

async def gemini_audio_id(session, clips, context_str):
    parts = [{"text": context_str + "\n\n" + GEMINI_PROMPT}]
    for i, (path, ct) in enumerate(clips, 1):
        uri = await upload_audio(session, path, ct)
        if uri:
            parts.append({"text": f"Recording {i}:"})
            parts.append({"file_data": {"mime_type": ct, "file_uri": uri}})
        else:
            parts.append({"text": f"[Recording {i} upload failed]"})

    url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
           f"gemini-3.5-flash:generateContent?key={GEMINI_KEY}")
    async with session.post(
        url,
        json={"contents": [{"parts": parts}],
              "generationConfig": {"maxOutputTokens": 4096}},
        timeout=aiohttp.ClientTimeout(total=180)
    ) as r:
        r.raise_for_status()
        data = await r.json()
    usage = data.get("usageMetadata", {})
    call_logger.log_model_call(
        model="gemini-3.5-flash", thinking=False,
        tokens_in=usage.get("promptTokenCount", 0),
        tokens_out=usage.get("candidatesTokenCount", 0),
        stop_reason=data.get("candidates", [{}])[0].get("finishReason", "?"),
        tools=[],
    )
    return data["candidates"][0]["content"]["parts"][0]["text"]

# Claude crosscheck

CLAUDE_PROMPT = (
    "A Gemini 2.5 Pro assessment of field audio recordings follows.\n"
    "You cannot hear the audio. Evaluate based on field context only.\n\n"
    "1. Is each species plausible for this location, season, time, and conditions?\n"
    "2. What alternatives should be considered?\n"
    "3. Are confidence levels appropriate?\n"
    "4. Does the overall identification hold up?\n\n"
    "Be direct. State agreement or disagreement explicitly with reasons."
)

def claude_sync(gemini_assessment, context_str):
    # Crosscheck is the adjudication step — Fable 5 at xhigh (house policy).
    # Usage logged inside skills.llm.
    from skills import llm as _llm
    return _llm.adjudicate_sync(
        f"{context_str}\n\nGEMINI ASSESSMENT:\n"
        f"{gemini_assessment}\n\n{CLAUDE_PROMPT}",
        max_tokens=4096,
    )

async def claude_crosscheck(gemini_assessment, context_str):
    return await asyncio.to_thread(claude_sync, gemini_assessment, context_str)

# Species extraction

async def extract_species(session, gemini_resp):
    try:
        url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
               f"gemini-3.5-flash:generateContent?key={GEMINI_KEY}")
        payload = {
            "contents": [{"parts": [{"text":
                f"What is the primary species identified in this assessment? "
                f"Reply with common name only, 1-5 words, nothing else.\n\n"
                f"{gemini_resp[:2000]}"
            }]}],
            "generationConfig": {"maxOutputTokens": 20},
        }
        async with session.post(url, json=payload,
                                timeout=aiohttp.ClientTimeout(total=10)) as r:
            r.raise_for_status()
            data  = await r.json()
            usage = data.get("usageMetadata", {})
            call_logger.log_model_call(
                model="gemini-3.5-flash", thinking=False,
                tokens_in=usage.get("promptTokenCount", 0),
                tokens_out=usage.get("candidatesTokenCount", 0),
                stop_reason=data.get("candidates", [{}])[0].get("finishReason", "?"),
                tools=[],
            )
            return data["candidates"][0]["content"]["parts"][0]["text"].strip()
    except Exception:
        return "Unknown species"

# Confidence synthesis

def synthesize_confidence(gemini_resp, claude_resp):
    c = claude_resp.lower()
    g = gemini_resp.lower()
    disagree = ["disagree", "incorrect", "unlikely", "inconsistent",
                "implausible", "does not hold", "cannot confirm"]
    agree    = ["consistent", "agree", "plausible", "confirms",
                "holds up", "reasonable"]
    uncertain = ["uncertain", "low confidence", "cannot determine"]
    d = sum(1 for s in disagree if s in c)
    a = sum(1 for s in agree if s in c)
    u = any(s in g for s in uncertain)
    if d >= 2:            return "Low"
    if d == 1 and a == 0: return "Medium"
    if u:                 return "Medium"
    if a >= 2 and d == 0: return "Confirmed"
    return "High"

# Report delivery

async def deliver_report(session, sender, species, confidence,
                         gemini_resp, claude_resp, ctx, user_note, clip_count):
    from coordinator import send_signal_file, USERS
    loc    = ctx.get("location", "unknown")
    ltime  = ctx.get("local_time", "unknown")
    moon   = ctx.get("moon_phase", "unknown")
    season = ctx.get("season", "unknown")
    report = (
        f"FIELD AUDIO ID REPORT\n"
        f"{loc} | {ltime} | {season} | Moon: {moon}\n"
        f"Clips analyzed: {clip_count}\n"
        f"Primary ID: {species}\n"
        f"Confidence: {confidence}\n"
        f"{'='*50}\n\n"
        f"GEMINI 2.5 PRO:\n{strip_markdown(gemini_resp)}\n\n"
        f"{'='*50}\n\n"
        f"CLAUDE OPUS CROSSCHECK:\n{strip_markdown(claude_resp)}\n\n"
        f"{'='*50}\n\n"
        f"CLEAN CALL: {species} -- {confidence}\n"
    )
    if confidence == "Low":
        report += "FLAG: Significant divergence. Manual review recommended.\n"
    os.makedirs(REPORT_DIR, exist_ok=True)
    ts   = datetime.now().strftime("%Y%m%d_%H%M%S")
    name = USERS.get(sender, {}).get("name", "user").lower()
    safe = re.sub(r'[^\w]', '_', species)[:30]
    path = f"{REPORT_DIR}/{ts}_{name}_{safe}.txt"
    with open(path, "w") as f:
        f.write(strip_markdown(report))
    async with aiohttp.ClientSession() as s:
        await send_signal_file(s, path,
            f"Audio ID: {species} -- {confidence}", sender)

# Coordinator-facing helpers

def handle_audio_attachment(sender, save_path, content_type):
    n = add_to_queue(sender, save_path, content_type)
    if n is None:
        return (f"Audio queue full ({QUEUE_MAX} clips). "
                f"Send 'analyze all' or 'use N,N' to process first.")
    total = len(load_queue(sender))
    return f"Audio #{n} saved ({total} in queue)."

def is_audio_trigger(text, sender):
    if not has_queue(sender):
        return False
    t = text.strip().lower()
    triggers = ["use ", "analyze ", "let's use", "lets use",
                "use clips", "use audio", "use recordings",
                "analyze all", "run all", "process all",
                "use 1", "use 2", "use 3", "use 4", "use 5"]
    return any(t.startswith(tr) for tr in triggers)

# Main handler

@register("audio_id", help="Wildlife audio ID — queue voice memos then: use 1,2,3 [context]")
async def handle_audio_id(session, sender, clean_text, rag_context, history):
    from coordinator import send_signal
    from skills.wx import wx_overrides

    queue = load_queue(sender)
    if not queue:
        return "No audio in queue. Send voice memos first.", False

    selected, user_note = parse_selection(clean_text, queue)
    clip_count          = len(selected)

    from coordinator import working as _working
    await _working()   # ⏳ instead of a progress message (One Voice A.2)
    print('⋯ progress:', f"Analyzing {clip_count} clip(s)...")

    clips    = [convert_to_wav(e["path"]) for e in selected]
    from chip_config import HOME_LOCATION
    location = wx_overrides.get(sender, {}).get("location") or HOME_LOCATION
    ctx      = await get_weather_context(session, location)
    ctx_str  = format_context(ctx, user_note or "No context provided.", clip_count)

    gemini_resp = await gemini_audio_id(session, clips, ctx_str)
    claude_resp = await claude_crosscheck(gemini_resp, ctx_str)
    species     = await extract_species(session, gemini_resp)
    confidence  = synthesize_confidence(gemini_resp, claude_resp)

    log_sighting(ctx, user_note, species, confidence,
                 gemini_resp, claude_resp, [e["path"] for e in selected])

    await deliver_report(session, sender, species, confidence,
                         gemini_resp, claude_resp, ctx, user_note, clip_count)

    clear_queue(sender)
    return f"It's a {species}, {confidence} confidence.", False
