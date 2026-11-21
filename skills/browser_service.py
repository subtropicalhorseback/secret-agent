#!/usr/bin/env python3
"""
skills/browser_service.py — Chip's live browser (chip-browser.service)
=======================================================================
Built 19 Nov 2026 from the Live Browser Spec (Tab 2, Chip_Audit_2026-11-19.txt).
Design-intent deviations from the draft, each deliberate:

- System Chromium via executable_path, not Playwright's bundle: this Pi runs
  a 16K-page kernel (getconf PAGESIZE → 16384) and the Pi-patched
  /usr/bin/chromium is the build that works there. (The spec itself flagged
  this exact verification.)
- Isolation via systemd hardening instead of a separate chipbrowser user:
  the unit runs with InaccessiblePaths over .env and data/memory plus
  MemoryMax/CPUQuota/NoNewPrivileges — the spec's goal (browser can't read
  keys or the databases) without a second Playwright install and socket
  permission dance.
- Routing: the whole Pi egresses through Mullvad again (restored this
  morning), so "route=mullvad" is the box default and "route=home" is the
  spec's whole-box fallback (mullvad: off). The browser-only SOCKS route
  proxy is deferred — it needs a second Tailscale node auth from the admin and an
  answer to the Mullvad device-limit question. Profiles stay split per route
  (mullvad/ vs direct/) exactly as specced, so cookies never link the two.

Tabs (20 Nov 2026, One Voice spec C.2): up to several workers share one
Chromium, one TAB each. Every op names its tab (default "main"). The route is
a property of the whole browser — the home-route lease marks this unit's
cgroup — so a route change is refused while any other tab is open; the
coordinator's gateway (skills/gateway.py) makes the home route exclusive so
this refusal is a backstop, never the normal path. Per-tab locks, so one slow
page no longer stalls every other tab; a context lock guards route changes.

Otherwise as drafted: 10-min idle timeout (per tab),
DOM snapshot with element refs, coordinate-mode actions for the cursor
fallback, headed Chromium under Xvfb with x11vnc + noVNC bound to the
Tailscale interface for watch/take-over, downloads refused, visit log.

Protocol: newline-delimited JSON over /run/chip/browser.sock.
Ops: open(url, route) · read() · act(ref, action, text?) · screenshot() ·
     click_xy(x,y) · type_text(text) · key(key) · scroll(dy) ·
     status() · close()
"""
import asyncio
import base64
import json
import os
import sqlite3
import subprocess
import time
from datetime import datetime

import os as _os, sys as _sys
_cc = _os.path.dirname(_os.path.abspath(__file__))
while _cc != "/" and not _os.path.exists(_os.path.join(_cc, "chip_config.py")):
    _cc = _os.path.dirname(_cc)
if _cc not in _sys.path:
    _sys.path.insert(0, _cc)
from chip_config import WORKSPACE
SOCK_PATH    = "/run/chip/browser.sock"
PROFILE_DIR  = f"{WORKSPACE}/data/browser_profiles"
VISIT_DB     = f"{PROFILE_DIR}/visits.db"
CHROMIUM     = "/usr/bin/chromium"
DISPLAY      = ":99"
VNC_PORT     = 5900
NOVNC_PORT   = 6080
IDLE_SECS    = 600
INTERACTIVE  = ("a", "button", "input", "select", "textarea",
                "[role='button']", "[role='link']", "[role='tab']",
                "[role='menuitem']", "[onclick]")


def _visit_db():
    os.makedirs(PROFILE_DIR, exist_ok=True)
    c = sqlite3.connect(VISIT_DB)
    c.execute("""CREATE TABLE IF NOT EXISTS visits (
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, user TEXT,
        url TEXT, route TEXT, outcome TEXT)""")
    return c


def log_visit(user, url, route, outcome="opened"):
    try:
        c = _visit_db()
        c.execute("INSERT INTO visits (ts,user,url,route,outcome) VALUES (?,?,?,?,?)",
                  (datetime.now().isoformat(), user, url, route, outcome))
        c.commit(); c.close()
    except Exception as e:
        print(f"visit log: {e}")


def _tailscale_ip():
    try:
        return subprocess.check_output(["tailscale", "ip", "-4"],
                                       text=True, timeout=10).strip().split()[0]
    except Exception:
        return "127.0.0.1"


class RouteConflict(Exception):
    pass


class Tab:
    def __init__(self, page, user="?"):
        self.page = page
        self.refs = []          # ref index -> locator index
        self.user = user
        self.last = time.time()
        self.lock = asyncio.Lock()


class BrowserService:
    def __init__(self):
        self.pw = None
        self.ctx = None
        self.route = None
        self.tabs = {}          # tab id -> Tab
        self.spare = []         # pages the persistent context opened on its own
        self.ctx_lock = asyncio.Lock()
        self.last_activity = time.time()
        self.display_procs = []

    # ── display stack (Xvfb + x11vnc + noVNC on the tailnet) ─────────────
    def start_display(self):
        ts_ip = _tailscale_ip()
        cmds = [
            ["Xvfb", DISPLAY, "-screen", "0", "1280x960x24", "-nolisten", "tcp"],
            ["x11vnc", "-display", DISPLAY, "-localhost", "-forever",
             "-shared", "-nopw", "-quiet", "-rfbport", str(VNC_PORT)],
            ["websockify", "--web", "/usr/share/novnc",
             f"{ts_ip}:{NOVNC_PORT}", f"localhost:{VNC_PORT}"],
        ]
        for cmd in cmds:
            try:
                self.display_procs.append(subprocess.Popen(
                    cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
                time.sleep(0.6)
            except Exception as e:
                print(f"display stack ({cmd[0]}): {e}")
        os.environ["DISPLAY"] = DISPLAY
        print(f"🖥️ display up — watch at http://{ts_ip}:{NOVNC_PORT}/vnc.html")

    # ── browser lifecycle ────────────────────────────────────────────────
    async def ensure(self, route):
        """Caller holds ctx_lock. Opens the context for `route`; refuses to
        switch routes while any tab is open (cookies must never cross)."""
        from playwright.async_api import async_playwright
        if self.pw is None:
            self.pw = await async_playwright().start()
        if self.ctx and self.route != route:
            if self.tabs:
                raise RouteConflict(
                    f"route change {self.route} -> {route} refused: "
                    f"{len(self.tabs)} tab(s) open ({', '.join(self.tabs)})")
            await self.close_ctx()
        if self.ctx is None:
            prof = f"{PROFILE_DIR}/{'direct' if route == 'home' else 'mullvad'}"
            os.makedirs(prof, exist_ok=True)
            self.ctx = await self.pw.chromium.launch_persistent_context(
                prof, executable_path=CHROMIUM, headless=False,
                viewport={"width": 1280, "height": 900},
                accept_downloads=False,
                args=["--no-sandbox", "--disable-dev-shm-usage"])
            self.spare = list(self.ctx.pages)
            self.route = route
        self.last_activity = time.time()

    async def get_tab(self, tab_id, route=None, create=False, user="?"):
        async with self.ctx_lock:
            t = self.tabs.get(tab_id)
            if t is not None and route and route != self.route:
                raise RouteConflict(f"tab {tab_id} is on {self.route}, asked for {route}")
            if t is None and create:
                await self.ensure(route or "mullvad")
                page = self.spare.pop() if self.spare else await self.ctx.new_page()
                t = Tab(page, user)
                self.tabs[tab_id] = t
            if t is not None:
                t.last = self.last_activity = time.time()
            return t

    async def close_tab(self, tab_id):
        async with self.ctx_lock:
            t = self.tabs.pop(tab_id, None)
            if t is not None:
                try:
                    await t.page.close()
                except Exception:
                    pass
            if not self.tabs:
                # Last tab gone: close the context too, so the next route
                # starts clean and no cookies linger in memory.
                await self.close_ctx()
            return t is not None

    async def close_ctx(self):
        try:
            if self.ctx:
                await self.ctx.close()
        except Exception:
            pass
        self.ctx = None
        self.route = None
        self.tabs = {}
        self.spare = []

    async def idle_reaper(self):
        while True:
            await asyncio.sleep(60)
            for tid, t in list(self.tabs.items()):
                if time.time() - t.last > IDLE_SECS:
                    print(f"⌛ idle timeout — closing tab {tid}")
                    await self.close_tab(tid)
            if self.ctx and not self.tabs and time.time() - self.last_activity > IDLE_SECS:
                async with self.ctx_lock:
                    await self.close_ctx()

    # ── ops ──────────────────────────────────────────────────────────────
    async def op_open(self, req, t):
        route = self.route
        url = req["url"]
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
        resp = await t.page.goto(url, timeout=30000,
                                 wait_until="domcontentloaded")
        await t.page.wait_for_timeout(1500)
        log_visit(t.user, url, route)
        # The HTTP status is the cheapest signal that a page was blocked rather
        # than merely unhelpful (403/429 vs a 200 with no results); the run
        # artifact records it per step. None for same-document navigations.
        return {"ok": True, "title": await t.page.title(),
                "url": t.page.url, "route": route,
                "status": resp.status if resp else None}

    async def op_read(self, req, t):
        els, out = [], []
        loc = t.page.locator(", ".join(INTERACTIVE))
        count = min(await loc.count(), 120)
        for i in range(count):
            el = loc.nth(i)
            try:
                if not await el.is_visible():
                    continue
                tag  = await el.evaluate("e => e.tagName.toLowerCase()")
                text = (await el.inner_text(timeout=400))[:80] if tag not in ("input", "select") \
                       else (await el.get_attribute("placeholder") or
                             await el.get_attribute("name") or "")[:80]
                text = " ".join(text.split())
                els.append(i)
                out.append(f"[{len(els)-1}] <{tag}> {text}")
            except Exception:
                continue
        t.refs = els
        body = ""
        # Default 5000 keeps fetch_page() (research:/archive:) as it was; the
        # browse loop asks for more and does its own budgeting + paging, so
        # read_more has something left to page through (browse.py).
        cap = int(req.get("max_chars") or 5000)
        try:
            body = (await t.page.inner_text("body", timeout=3000))[:cap]
        except Exception:
            pass
        return {"ok": True, "title": await t.page.title(), "url": t.page.url,
                "elements": "\n".join(out[:120]), "text": body}

    async def op_act(self, req, t):
        ref, action = int(req["ref"]), req["action"]
        if ref < 0 or ref >= len(t.refs):
            return {"ok": False, "error": f"stale ref {ref} — call read again"}
        el = t.page.locator(", ".join(INTERACTIVE)).nth(t.refs[ref])
        try:
            if action == "click":
                await el.click(timeout=8000)
            elif action == "type":
                await el.fill(req.get("text", ""), timeout=8000)
            elif action == "select":
                await el.select_option(label=req.get("text", ""), timeout=8000)
            elif action == "scroll":
                await el.scroll_into_view_if_needed(timeout=8000)
            else:
                return {"ok": False, "error": f"unknown action {action}"}
            await t.page.wait_for_timeout(1200)
            return {"ok": True, "title": await t.page.title(), "url": t.page.url}
        except Exception as e:
            return {"ok": False, "error": str(e)[:200]}

    async def op_screenshot(self, req, t):
        png = await t.page.screenshot(type="jpeg", quality=60)
        return {"ok": True, "image_b64": base64.b64encode(png).decode(),
                "media_type": "image/jpeg", "url": t.page.url}

    async def op_cursor(self, req, t):
        op = req["op"]
        try:
            if op == "click_xy":
                await t.page.mouse.click(float(req["x"]), float(req["y"]))
            elif op == "type_text":
                await t.page.keyboard.type(req.get("text", ""), delay=25)
            elif op == "key":
                await t.page.keyboard.press(req.get("key", "Enter"))
            elif op == "scroll":
                await t.page.mouse.wheel(0, float(req.get("dy", 600)))
            await t.page.wait_for_timeout(900)
            return {"ok": True, "url": t.page.url}
        except Exception as e:
            return {"ok": False, "error": str(e)[:200]}

    async def op_status(self, req):
        return {"ok": True,
                "open": bool(self.ctx),
                "tabs": {tid: t.page.url for tid, t in self.tabs.items()},
                "url": next((t.page.url for t in self.tabs.values()), None),
                "route": self.route,
                "novnc": f"http://{_tailscale_ip()}:{NOVNC_PORT}/vnc.html",
                "idle_secs": int(time.time() - self.last_activity)}

    async def op_close(self, req):
        """Close the whole browser. Refused while other tabs are open unless
        forced — a worker closes its own tab with tab_close."""
        async with self.ctx_lock:
            others = [tid for tid in self.tabs if tid != req.get("tab")]
            if others and not req.get("force"):
                return {"ok": False, "error": f"other tabs open: {', '.join(others)}"}
            await self.close_ctx()
        return {"ok": True}

    # ── server ───────────────────────────────────────────────────────────
    async def handle(self, reader, writer):
        try:
            line = await asyncio.wait_for(reader.readline(), timeout=120)
            req  = json.loads(line.decode())
            op   = req.get("op", "")
            tab_id = str(req.get("tab") or "main")
            if op == "status":
                resp = await self.op_status(req)
            elif op == "close":
                resp = await self.op_close(req)
            elif op == "tab_close":
                resp = {"ok": await self.close_tab(tab_id)}
            elif op in ("open", "read", "act", "screenshot",
                        "click_xy", "type_text", "key", "scroll"):
                t = await self.get_tab(tab_id, route=req.get("route"),
                                       create=(op == "open"),
                                       user=req.get("user", "?"))
                if t is None:
                    resp = {"ok": False, "error": f"no page open in tab {tab_id}"}
                else:
                    async with t.lock:
                        t.last = self.last_activity = time.time()
                        if op == "open":
                            resp = await self.op_open(req, t)
                        elif op == "read":
                            resp = await self.op_read(req, t)
                        elif op == "act":
                            resp = await self.op_act(req, t)
                        elif op == "screenshot":
                            resp = await self.op_screenshot(req, t)
                        else:
                            resp = await self.op_cursor(req, t)
            else:
                resp = {"ok": False, "error": f"unknown op {op}"}
        except RouteConflict as e:
            resp = {"ok": False, "error": f"route conflict: {e}", "route_conflict": True}
        except Exception as e:
            resp = {"ok": False, "error": f"{type(e).__name__}: {str(e)[:200]}"}
        try:
            writer.write((json.dumps(resp) + "\n").encode())
            await writer.drain()
            writer.close()
        except Exception:
            pass

    async def main(self):
        os.makedirs("/run/chip", exist_ok=True)
        if os.path.exists(SOCK_PATH):
            os.remove(SOCK_PATH)
        self.start_display()
        server = await asyncio.start_unix_server(self.handle, path=SOCK_PATH)
        os.chmod(SOCK_PATH, 0o660)
        asyncio.get_event_loop().create_task(self.idle_reaper())
        print(f"🌐 chip-browser online — socket {SOCK_PATH}")
        async with server:
            await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(BrowserService().main())
