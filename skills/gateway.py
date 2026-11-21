"""
skills/gateway.py — the one way out to the internet, owned by the coordinator
==============================================================================
Added 20 Nov 2026 (One Voice spec, C.2–C.5).

ROUTE OWNERSHIP. The browser is one Chromium (chip-browser.service). Its
network route is a property of the whole process: the home-route lease marks
the unit's cgroup. Profiles are split per route so cookies never link the VPN
identity to the home IP. So:
  * VPN tabs hold the route lock SHARED — several workers, one tab each;
  * the home route holds it EXCLUSIVE — while it is up, no VPN-profile request
    can be in flight;
  * the lock is writer-preferring: once a home request waits, new VPN tabs
    queue behind it, so a stream of VPN work cannot starve an escalation;
  * the gateway takes and releases the home-route lease, not the worker.
The browser service refuses a route change while other tabs are open; this
lock is what makes that refusal a backstop rather than the normal path.

REAPING. browse_obs.reap() (stray Chromium, stale profile locks) runs when the
number of active holds goes 0→1 and 1→0 — never in the middle of another
worker's run.

RECORDS (all written from the coordinator side; the browser unit cannot read
or write data/memory):
  * web log   — data/weblog/YYYY-MM-DD.jsonl, one line per search, fetch and
                browser step; rolled nightly with the work log, 30 days.
  * pages     — data/memory/chip_web.db: text of every page fetched or read,
                with when; 30 days.
  * observations — the small facts a request was after (a price, a close, a
                due date), with source and time; 12 months.

FRESHNESS is decided by code (FRESH_FOR, overridable as WEB_FRESHNESS in
config/routing_constants.py). Inside the window an answer may come from the
cache and must state its age; "now", "current", "check again", "refresh" in
the user's message force a live fetch.
"""
import asyncio
import hashlib
import json
import os
import re
import sqlite3
import sys
import threading
import time
from datetime import datetime, timedelta
from urllib.parse import urlparse

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
from chip_config import WORKSPACE  # noqa: E402

WEB_DB = f"{WORKSPACE}/data/memory/chip_web.db"
PAGE_KEEP_DAYS = 30
OBS_KEEP_DAYS = 365
HOME_TTL = 900

# Seconds a cached page / observation counts as fresh, by kind.
FRESH_FOR = {
    "quote":     15 * 60,        # share prices while markets are open
    "price":     24 * 3600,      # store prices and stock
    "course":    24 * 3600,      # course pages, assignments
    "news":      3600,
    "reference": 7 * 24 * 3600,
    "default":   6 * 3600,
}
REFRESH_WORDS = re.compile(r"\b(now|current(ly)?|latest|right now|today|check again|"
                           r"refresh|re-?check|live|up to date)\b", re.I)


# ─── route lock ───────────────────────────────────────────────────────────────

class RouteLock:
    """Writer-preferring read/write lock on asyncio."""

    def __init__(self):
        self._readers = 0
        self._writer = False
        self._writers_waiting = 0
        self._cond = None

    def _c(self):
        if self._cond is None:
            self._cond = asyncio.Condition()
        return self._cond

    async def acquire_shared(self):
        c = self._c()
        async with c:
            await c.wait_for(lambda: not self._writer and self._writers_waiting == 0)
            self._readers += 1

    async def release_shared(self):
        c = self._c()
        async with c:
            self._readers -= 1
            c.notify_all()

    async def acquire_exclusive(self):
        c = self._c()
        async with c:
            self._writers_waiting += 1
            try:
                await c.wait_for(lambda: not self._writer and self._readers == 0)
            finally:
                self._writers_waiting -= 1
            self._writer = True

    async def release_exclusive(self):
        c = self._c()
        async with c:
            self._writer = False
            c.notify_all()

    def state(self):
        return {"readers": self._readers, "writer": self._writer,
                "writers_waiting": self._writers_waiting}


LOCK = RouteLock()
_active = 0                       # holds currently granted
_tab_seq = 0


class Hold:
    def __init__(self, route, exclusive, lease, owner, purpose):
        self.route = route
        self.exclusive = exclusive
        self.lease = lease
        self.owner = owner
        self.purpose = purpose
        self.released = False
        self.t0 = time.time()

    def lease_left(self):
        if not self.lease:
            return None
        return max(0, HOME_TTL - (time.time() - self.t0))


def whole_box_home():
    return os.path.exists("/run/chip/mullvad_off")


def new_tab_id(purpose="tab"):
    global _tab_seq
    _tab_seq += 1
    return f"{purpose}-{_tab_seq}"


async def _reap(when):
    try:
        from skills import browse_obs
        r = await asyncio.to_thread(browse_obs.reap)
        if r.get("killed") or r.get("locks_cleared") or r.get("survivors"):
            print(f"🧹 gateway reap ({when}): {r}", flush=True)
        return r
    except Exception as e:
        print(f"⚠️ gateway reap ({when}) failed: {e}", flush=True)
        return None


async def acquire(route, owner="?", task="", purpose="browse"):
    """route: "mullvad" or "home". Returns a Hold. Raises if the home-route
    lease cannot be taken (the lock is released first)."""
    global _active
    exclusive = (route == "home") and not whole_box_home()
    if exclusive:
        await LOCK.acquire_exclusive()
    else:
        await LOCK.acquire_shared()
    lease = False
    try:
        if _active == 0:
            await _reap("first tab")
        _active += 1
        if exclusive:
            import home_route
            await asyncio.to_thread(home_route.acquire, str(owner)[-4:], task, HOME_TTL)
            lease = True
    except BaseException:
        _active = max(0, _active - 1)
        if exclusive:
            await LOCK.release_exclusive()
        else:
            await LOCK.release_shared()
        raise
    hold = Hold(route, exclusive, lease, owner, purpose)
    log_web("route_acquire", route=route, exclusive=exclusive, owner=str(owner)[-4:],
            purpose=purpose, lock=LOCK.state())
    return hold


async def release(hold, reason="session-end"):
    global _active
    if hold is None or hold.released:
        return
    hold.released = True
    try:
        if hold.lease:
            try:
                import home_route
                await asyncio.to_thread(home_route.release, reason)
            except Exception as e:
                print(f"⚠️ home-route release failed: {e}", flush=True)
    finally:
        _active = max(0, _active - 1)
        if _active == 0:
            await _reap("last tab")
        if hold.exclusive:
            await LOCK.release_exclusive()
        else:
            await LOCK.release_shared()
        log_web("route_release", route=hold.route, purpose=hold.purpose,
                held_s=int(time.time() - hold.t0), lock=LOCK.state())


async def close_tab(tab):
    try:
        from skills import browse
        await asyncio.to_thread(browse.service_request, {"op": "tab_close", "tab": tab}, 30)
    except Exception as e:
        print(f"⚠️ tab_close {tab} failed: {e}", flush=True)


# ─── web log ──────────────────────────────────────────────────────────────────

_wl_lock = threading.Lock()


def weblog_dir():
    return os.getenv("CHIP_WEBLOG_DIR") or f"{WORKSPACE}/data/weblog"


def log_web(event, **fields):
    try:
        rec = {"ts": datetime.now().isoformat(timespec="seconds"), "event": event}
        try:
            from skills import send_gate
            t = send_gate.raw()
            if t is not None:
                rec.setdefault("owner", str(t.sender)[-4:])
                if t.request_id:
                    rec.setdefault("request", t.request_id)
        except Exception:
            pass
        rec.update(fields)
        d = weblog_dir()
        os.makedirs(d, exist_ok=True)
        with _wl_lock:
            with open(os.path.join(d, f"{datetime.now():%Y-%m-%d}.jsonl"), "a") as f:
                f.write(json.dumps(rec, default=str, ensure_ascii=False) + "\n")
    except Exception as e:
        print(f"⚠️ weblog: {e}")


def read_weblog(days=1, query=None, limit=40):
    from skills import worklog
    rows = []
    for back in range(days):
        day = datetime.now() - timedelta(days=back)
        p = os.path.join(weblog_dir(), f"{day:%Y-%m-%d}.jsonl")
        import gzip
        fh = None
        if os.path.exists(p):
            fh = open(p)
        elif os.path.exists(p + ".gz"):
            fh = gzip.open(p + ".gz", "rt")
        if not fh:
            continue
        with fh:
            for line in fh:
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                if r.get("event") in ("route_acquire", "route_release"):
                    continue
                if query and query.lower() not in json.dumps(r).lower():
                    continue
                rows.append(r)
    rows.sort(key=lambda r: r.get("ts", ""))
    return rows[-limit:]


# ─── page cache and observations ──────────────────────────────────────────────

def _db_path():
    return os.getenv("CHIP_WEB_DB") or WEB_DB


def _conn():
    p = _db_path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    c = sqlite3.connect(p, timeout=10)
    c.row_factory = sqlite3.Row
    c.execute("""CREATE TABLE IF NOT EXISTS pages (
        url TEXT PRIMARY KEY, host TEXT, title TEXT, text TEXT, sha TEXT,
        route TEXT, status INTEGER, fetched_at TEXT NOT NULL, via TEXT)""")
    c.execute("""CREATE TABLE IF NOT EXISTS observations (
        id INTEGER PRIMARY KEY AUTOINCREMENT, what TEXT NOT NULL, value TEXT NOT NULL,
        unit TEXT, kind TEXT, source_url TEXT, seen_at TEXT NOT NULL,
        owner TEXT, request_id TEXT)""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_obs_what ON observations(what)")
    return c


def _norm_url(url):
    u = (url or "").strip()
    return u[:-1] if u.endswith("/") and u.count("/") > 3 else u


def store_page(url, text, title="", route=None, status=None, via="browser"):
    if not url or not text:
        return
    try:
        c = _conn()
        c.execute("INSERT INTO pages (url, host, title, text, sha, route, status, fetched_at, via) "
                  "VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(url) DO UPDATE SET "
                  "title=excluded.title, text=excluded.text, sha=excluded.sha, "
                  "route=excluded.route, status=excluded.status, "
                  "fetched_at=excluded.fetched_at, via=excluded.via",
                  (_norm_url(url), urlparse(url).netloc.lower(), title or "", text[:60000],
                   hashlib.sha1(text.encode("utf-8", "ignore")).hexdigest()[:16],
                   route, status, datetime.now().isoformat(timespec="seconds"), via))
        c.commit(); c.close()
    except Exception as e:
        print(f"⚠️ page cache: {e}")


def cached_page(url):
    try:
        c = _conn()
        r = c.execute("SELECT * FROM pages WHERE url=?", (_norm_url(url),)).fetchone()
        c.close()
        return dict(r) if r else None
    except Exception:
        return None


def age_s(iso):
    try:
        return int((datetime.now() - datetime.fromisoformat(iso)).total_seconds())
    except Exception:
        return None


def age_words(iso):
    a = age_s(iso)
    if a is None:
        return "unknown age"
    if a < 3600:
        return f"{max(1, a // 60)} min ago"
    if a < 86400:
        return f"{a // 3600} h ago"
    when = datetime.fromisoformat(iso)
    return f"{a // 86400} day(s) ago ({when:%a %-d %b %-I:%M %p})"


def freshness(kind):
    windows = dict(FRESH_FOR)
    try:
        from skills.browse_capture import _RC
        windows.update(getattr(_RC, "WEB_FRESHNESS", {}) or {})
    except Exception:
        pass
    return int(windows.get(kind or "default", windows["default"]))


def kind_for(url_or_query=""):
    s = (url_or_query or "").lower()
    if re.search(r"\b(stock|share|quote|ticker|nasdaq|nyse)\b|finance\.", s):
        return "quote"
    if re.search(r"canvas|instructure|\.edu\b|syllabus|assignment", s):
        return "course"
    if re.search(r"news|cnn|reuters|apnews|nytimes|bbc", s):
        return "news"
    if re.search(r"wikipedia|docs\.|manual|reference", s):
        return "reference"
    if re.search(r"price|stock\b|in stock|cart|product|\$|walmart|target|bestbuy|costco|heb", s):
        return "price"
    return "default"


def force_refresh():
    """The user's own words decide: 'now', 'current', 'check again'…"""
    try:
        from skills import send_gate
        t = send_gate.raw()
        return bool(t and REFRESH_WORDS.search(t.text or ""))
    except Exception:
        return False


def record_observation(what, value, unit="", kind="", source_url="", owner=None,
                       request_id=None):
    try:
        if owner is None or request_id is None:
            from skills import send_gate
            t = send_gate.raw()
            owner = owner or (t.sender if t else None)
            request_id = request_id or (t.request_id if t else None)
        c = _conn()
        c.execute("INSERT INTO observations (what, value, unit, kind, source_url, seen_at, "
                  "owner, request_id) VALUES (?,?,?,?,?,?,?,?)",
                  (what.strip()[:200], str(value).strip()[:100], (unit or "")[:30],
                   kind or kind_for(what + " " + (source_url or "")), (source_url or "")[:500],
                   datetime.now().isoformat(timespec="seconds"), owner, request_id))
        c.commit(); c.close()
        return True
    except Exception as e:
        print(f"⚠️ observation: {e}")
        return False


def find_observations(query, limit=12):
    words = [w for w in re.findall(r"[a-z0-9]{3,}", (query or "").lower())][:6]
    try:
        c = _conn()
        if words:
            where = " AND ".join("lower(what) LIKE ?" for _ in words)
            rows = c.execute(f"SELECT * FROM observations WHERE {where} "
                             f"ORDER BY seen_at DESC LIMIT ?",
                             [f"%{w}%" for w in words] + [limit]).fetchall()
        else:
            rows = c.execute("SELECT * FROM observations ORDER BY seen_at DESC LIMIT ?",
                             (limit,)).fetchall()
        c.close()
        return [dict(r) for r in rows]
    except Exception:
        return []


def find_pages(query, limit=8):
    words = [w for w in re.findall(r"[a-z0-9]{3,}", (query or "").lower())][:6]
    try:
        c = _conn()
        if words:
            where = " AND ".join("(lower(title) LIKE ? OR lower(url) LIKE ? OR lower(text) LIKE ?)"
                                 for _ in words)
            args = []
            for w in words:
                args += [f"%{w}%"] * 3
            rows = c.execute(f"SELECT url, title, fetched_at, route, substr(text,1,400) AS snip "
                             f"FROM pages WHERE {where} ORDER BY fetched_at DESC LIMIT ?",
                             args + [limit]).fetchall()
        else:
            rows = c.execute("SELECT url, title, fetched_at, route, substr(text,1,400) AS snip "
                             "FROM pages ORDER BY fetched_at DESC LIMIT ?", (limit,)).fetchall()
        c.close()
        return [dict(r) for r in rows]
    except Exception:
        return []


def prune(page_days=PAGE_KEEP_DAYS, obs_days=OBS_KEEP_DAYS):
    """Nightly retention (dream cycle)."""
    try:
        c = _conn()
        pc = (datetime.now() - timedelta(days=page_days)).isoformat()
        oc = (datetime.now() - timedelta(days=obs_days)).isoformat()
        n1 = c.execute("DELETE FROM pages WHERE fetched_at < ?", (pc,)).rowcount
        n2 = c.execute("DELETE FROM observations WHERE seen_at < ?", (oc,)).rowcount
        c.commit(); c.close()
        return n1, n2
    except Exception as e:
        print(f"⚠️ web prune: {e}")
        return 0, 0


# ─── search and fetch through the gateway ─────────────────────────────────────

def search(query, max_results=5):
    """DuckDuckGo search, logged. (Search results are not cached: the cache is
    for pages and the facts on them.)"""
    from skills.web_utils import quick_search
    t0 = time.time()
    res = quick_search(query, max_results)
    ok = not isinstance(res, str)
    log_web("search", query=query[:200], results=(len(res) if ok else 0),
            outcome=("ok" if ok else str(res)[:120]), ms=int((time.time() - t0) * 1000))
    return res


def fetch(url, kind=None, fresh=None):
    """Plain HTTP fetch with the page cache in front. Returns
    (text, meta) where meta says whether it came from the cache and its age."""
    from skills.web_utils import scrape_url
    kind = kind or kind_for(url)
    fresh = force_refresh() if fresh is None else fresh
    cached = cached_page(url)
    if cached and not fresh:
        a = age_s(cached["fetched_at"])
        if a is not None and a <= freshness(kind):
            log_web("fetch", url=url[:300], via="cache", age_s=a, kind=kind)
            return cached["text"], {"cached": True, "age": age_words(cached["fetched_at"]),
                                    "fetched_at": cached["fetched_at"]}
    t0 = time.time()
    text = scrape_url(url)
    failed = (not text) or text.startswith(("Scrape Error", "Mullvad is down"))
    log_web("fetch", url=url[:300], via="http", kind=kind,
            outcome=("ok" if not failed else text[:120]), ms=int((time.time() - t0) * 1000))
    if failed:
        if cached:
            return cached["text"], {"cached": True, "age": age_words(cached["fetched_at"]),
                                    "fetched_at": cached["fetched_at"],
                                    "live_failed": text[:160]}
        return text, {"cached": False, "failed": True}
    store_page(url, text, via="http")
    return text, {"cached": False}


def history_text(query="", days=2):
    """For the read-only web_history tool: what Chip looked at, what it has on
    file, and what it observed — each with its age."""
    L = []
    obs = find_observations(query) if query else find_observations("", limit=8)
    if obs:
        L.append("OBSERVATIONS ON FILE:")
        for o in obs:
            L.append(f"- {o['what']}: {o['value']}{(' ' + o['unit']) if o['unit'] else ''} "
                     f"({age_words(o['seen_at'])}; source {o['source_url'] or 'n/a'})")
    pages = find_pages(query) if query else []
    if pages:
        L.append("PAGES ON FILE (cached text you can quote, with its age):")
        for p in pages:
            L.append(f"- {p['title'] or p['url']} — {p['url']} ({age_words(p['fetched_at'])}, "
                     f"route {p['route'] or '?'})")
    log = read_weblog(days=days, query=query or None, limit=25)
    if log:
        L.append(f"WEB LOG (last {days} day(s){', matching ' + repr(query) if query else ''}):")
        for r in log:
            what = r.get("url") or r.get("query") or ""
            L.append(f"- {r.get('ts', '')[5:16]} {r.get('event')} {what[:120]} "
                     f"{('[' + str(r.get('outcome')) + ']') if r.get('outcome') not in (None, 'ok') else ''}"
                     f"{(' via ' + r['via']) if r.get('via') else ''}")
    return "\n".join(L) if L else "Nothing on file for that."
