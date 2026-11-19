"""
skills/media_review.py
=======================
Chip media catalog skill — books, movies, TV.

Prefix: review:
  review: <any natural language query about your media>

Examples:
  review: how many books have I read this year
  review: list my top rated books
  review: suggest something like Pines
  review: log book The Maid by Nita Prose 7/10 quick read
  review: what movies have I watched
  review: stats

Storage: chip_media.db, media_reviews table, user-scoped.
All queries answered by Gemini Flash against live DB — no subcommand parsing.
New entries extracted from natural language by Gemini Flash then inserted.
"""

import os, re, json, sqlite3, requests
from datetime import datetime
from dotenv import load_dotenv
from skills.registry import register, cancel_flags

import os as _os, sys as _sys
_cc = _os.path.dirname(_os.path.abspath(__file__))
while _cc != "/" and not _os.path.exists(_os.path.join(_cc, "chip_config.py")):
    _cc = _os.path.dirname(_cc)
if _cc not in _sys.path:
    _sys.path.insert(0, _cc)
from chip_config import WORKSPACE
load_dotenv(f"{WORKSPACE}/.env")

GEMINI_KEY = os.getenv("GEMINI_API_KEY")
DB_PATH    = f"{WORKSPACE}/data/memory/chip_media.db"

GEMINI_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    "gemini-3.5-flash:generateContent"
)

# ── Gemini helper ─────────────────────────────────────────────────────────────

def _gemini(prompt, max_tokens=1024):
    payload = {
        "contents":         [{"parts": [{"text": prompt}]}],
        "generationConfig": {"maxOutputTokens": max_tokens, "temperature": 0.2},
    }
    try:
        r = requests.post(
            GEMINI_URL, params={"key": GEMINI_KEY}, json=payload, timeout=45
        )
        r.raise_for_status()
        return r.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
    except Exception as e:
        return f"[Gemini error: {e}]"

# ── DB helpers ────────────────────────────────────────────────────────────────

def _db():
    if not os.path.exists(DB_PATH):
        raise FileNotFoundError(f"chip_media.db not found — run build_media_db.py")
    return sqlite3.connect(DB_PATH)

def _get_user(sender):
    try:
        from coordinator import USERS
        return USERS.get(sender, {}).get("name", "user").lower()
    except Exception:
        return "user"

def _load_catalog(user):
    """Load full catalog for user as list of row dicts."""
    con = _db()
    rows = con.execute(
        """SELECT media_type, title, creator, rating, date_reviewed, notes
           FROM media_reviews WHERE user=?
           ORDER BY date_reviewed, rating DESC""",
        (user,)
    ).fetchall()
    con.close()
    return rows

def _format_catalog(rows):
    """Format catalog rows as plain text for LLM context."""
    lines = []
    for mtype, title, creator, rating, date_r, notes in rows:
        line = f"[{mtype.upper()}] {title}"
        if creator: line += f" by {creator}"
        if rating is not None: line += f" — {rating}/10"
        if date_r:  line += f" ({date_r})"
        if notes:   line += f" | {notes}"
        lines.append(line)
    return "\n".join(lines)

def _insert(con, user, media_type, title, creator, rating, notes, date_reviewed=None, source="skill"):
    cur = con.cursor()
    cur.execute(
        "SELECT id FROM media_reviews WHERE user=? AND LOWER(title)=? AND media_type=?",
        (user, title.lower(), media_type)
    )
    row = cur.fetchone()
    if row:
        cur.execute(
            "UPDATE media_reviews SET rating=?, notes=?, created_at=datetime('now') WHERE id=?",
            (rating, notes, row[0])
        )
        con.commit()
        return "updated"
    cur.execute(
        """INSERT INTO media_reviews
           (user, media_type, title, creator, rating, notes, date_reviewed, source)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (user, media_type, title, creator or None, rating,
         notes or None, date_reviewed or datetime.now().strftime("%B %Y"), source)
    )
    con.commit()
    return "inserted"

def _ingest(title, creator, media_type, rating, notes, user):
    try:
        from skills.vault_ingest import ingest_text
        line = f"{media_type.upper()}: {title}"
        if creator: line += f" by {creator}"
        if rating is not None: line += f" — {rating}/10"
        if notes: line += f" — {notes}"
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        ingest_text(line, f"media_review_{user}_{ts}.txt",
                    domain="media", source_type="media_review")
    except Exception as e:
        print(f"Media vault ingest error: {e}")

# ── Log detection ─────────────────────────────────────────────────────────────

LOG_PATTERNS = [
    r'\b(log|add|record|save|just (finished|read|watched|saw))\b',
    r'\b\d+(\.\d+)?/10\b',
    r'\b(rated?|rating)\b',
]

def _looks_like_log(text):
    t = text.lower()
    return any(re.search(p, t) for p in LOG_PATTERNS)

def _extract_and_log(user, text):
    """Use Gemini to parse a natural-language log entry and insert it."""
    prompt = f"""\
Extract a media review from this text. Return ONLY a JSON object:
  title       (string, required)
  creator     (string — author/director/showrunner, empty if unknown)
  media_type  ("book", "movie", or "tv")
  rating      (number 0-10, or null)
  notes       (string, empty if none)

TEXT: {text}"""
    raw = _gemini(prompt, max_tokens=256)
    raw = raw.strip().replace("```json","").replace("```","").strip()
    try:
        parsed = json.loads(raw)
    except Exception:
        return None

    title = (parsed.get("title") or "").strip()
    if not title:
        return None

    media_type = parsed.get("media_type", "book").lower()
    if media_type not in ("book","movie","tv"):
        media_type = "book"
    creator = (parsed.get("creator") or "").strip() or None
    rating  = parsed.get("rating")
    notes   = (parsed.get("notes") or "").strip() or None

    try:
        con    = _db()
        action = _insert(con, user, media_type, title, creator, rating, notes)
        con.close()
        _ingest(title, creator, media_type, rating, notes, user)
        verb = "Updated" if action == "updated" else "Logged"
        line = f"{verb}: {media_type} — {title}"
        if creator: line += f" by {creator}"
        if rating is not None: line += f" ({rating}/10)"
        return line
    except Exception as e:
        return f"DB error: {e}"

# ── Auto-extraction from vision descriptions ──────────────────────────────────

def maybe_extract_from_vision(sender, vision_text):
    """
    Called after vision response. If it contains ratings, extract and store.
    Returns count inserted.
    """
    # Always attempt extraction — Gemini returns [] if no ratings found
    if len(vision_text) < 50:
        return 0

    prompt = f"""\
Extract all book, movie, and TV ratings from this text.
Return ONLY a JSON array. Each element:
  title, creator, media_type ("book"/"movie"/"tv"), rating (0-10), notes, date_approx

Only include items with a clear rating. Return [] if none found.

TEXT: {vision_text[:3000]}"""

    raw = _gemini(prompt, max_tokens=1024)
    if not raw:
        return 0
    raw = raw.strip().replace("```json","").replace("```","").strip()
    try:
        reviews = json.loads(raw)
        if not isinstance(reviews, list) or not reviews:
            return 0
    except Exception:
        return 0

    user = _get_user(sender)
    try:
        con = _db()
        inserted = 0
        for r in reviews:
            title = (r.get("title") or "").strip()
            if not title: continue
            mtype = r.get("media_type","book").lower()
            if mtype not in ("book","movie","tv"): mtype = "book"
            action = _insert(
                con, user, mtype, title,
                (r.get("creator") or "").strip() or None,
                r.get("rating"),
                (r.get("notes") or "").strip() or None,
                date_reviewed=(r.get("date_approx") or "").strip() or None,
                source="vision"
            )
            if action == "inserted":
                inserted += 1
        con.close()
        if inserted:
            print(f"📚 Auto-extracted {inserted} media reviews from vision for {user}")
        return inserted
    except Exception as e:
        print(f"Auto-extract DB error: {e}")
        return 0

# ── Skill entry point ─────────────────────────────────────────────────────────

@register("review", help="Media catalog: review: <any question about your books/movies/tv>")
async def handle_review(session, sender, clean_text, rag_context, history):
    user = _get_user(sender)

    if not clean_text:
        return (
            "review: how many books have I read this year\n"
            "review: list my top rated books\n"
            "review: suggest something like Pines\n"
            "review: log book The Maid by Nita Prose 7/10"
        ), False

    # If it looks like a log entry, extract and insert
    if _looks_like_log(clean_text):
        result = _extract_and_log(user, clean_text)
        if result:
            return result, False
        # Fall through to query if extraction failed

    # Load full catalog and answer with Gemini
    try:
        rows    = _load_catalog(user)
        catalog = _format_catalog(rows)
    except Exception as e:
        return f"DB error: {e}", False

    if not rows:
        return "No media entries on record yet. Send your rating screenshots or use review: log to add entries.", False

    prompt = f"""You are a media catalog assistant. Answer the query below using ONLY the catalog provided. Do not invent entries, titles, or counts — every number must come directly from the catalog.

Query: {clean_text}

CATALOG ({len(rows)} total entries):
{catalog}

Answer concisely and accurately. For counts, count the actual entries. For suggestions, explain briefly why each fits based on ratings patterns."""

    answer = _gemini(prompt, max_tokens=1024)
    return answer, False
