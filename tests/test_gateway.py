"""Gateway (One Voice spec C.2–C.5): one route at a time, tabs apart, a cache with ages.

Standalone, no pytest. A fake browser service records every op with the
route the browser was on, and FAILS the test if a VPN-profile request is ever
in flight while the home route is up. No network, no Chromium, no signal-cli.
Exits non-zero on failure.
"""
import asyncio
import os
import sys
import tempfile
import types

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
TMP = tempfile.mkdtemp(prefix="gateway_test_")
os.environ["CHIP_WEBLOG_DIR"] = os.path.join(TMP, "weblog")
os.environ["CHIP_WEB_DB"] = os.path.join(TMP, "web.db")
os.environ["CHIP_WORKLOG_DIR"] = os.path.join(TMP, "worklog")

from skills import gateway, browse_obs, send_gate  # noqa: E402

FAILS = []
REAPS = []
LEASE = {"on": False, "acquired": 0, "released": []}
OPS = []          # (tab, op, route_in_force)


def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")
    if not cond:
        FAILS.append(f"{name}: {detail}")


browse_obs.reap = lambda dry_run=False: REAPS.append(1) or {"killed": [], "locks_cleared": [],
                                                             "survivors": []}
home_route = types.ModuleType("home_route")


def _acq(user, task, ttl):
    LEASE["on"] = True; LEASE["acquired"] += 1


def _rel(reason="session-end"):
    LEASE["on"] = False; LEASE["released"].append(reason)


home_route.acquire, home_route.release = _acq, _rel
sys.modules["home_route"] = home_route


async def fake_op(tab, op, route):
    """An op issued by a worker on `route`. A mullvad op while the lease is on
    is exactly the cookie/IP leak the lock exists to prevent."""
    if route == "mullvad" and LEASE["on"]:
        FAILS.append(f"LEAK: {tab} issued {op} on the VPN profile while home was up")
    OPS.append((tab, op, route))
    await asyncio.sleep(0.02)


async def worker(name, route, n_ops=5, delay=0.0, escalate_after=None):
    await asyncio.sleep(delay)
    tab = gateway.new_tab_id(name)
    hold = await gateway.acquire(route, owner="+15555550101", task=name)
    try:
        for i in range(n_ops):
            if escalate_after is not None and i == escalate_after and hold.route == "mullvad":
                await gateway.release(hold, reason="escalation")
                hold = await gateway.acquire("home", owner="+15555550101", task=name)
            await fake_op(tab, f"op{i}", hold.route)
    finally:
        await gateway.release(hold)
    return tab


async def case_lock_writer_preference():
    print("case_lock_writer_preference")
    lock = gateway.RouteLock()
    order = []
    await lock.acquire_shared()                  # reader A holds

    async def writer():
        await lock.acquire_exclusive(); order.append("W")
        await asyncio.sleep(0.05); await lock.release_exclusive()

    async def late_reader():
        await asyncio.sleep(0.01)                # arrives after the writer queued
        await lock.acquire_shared(); order.append("R2"); await lock.release_shared()

    tw = asyncio.create_task(writer()); await asyncio.sleep(0.005)
    tr = asyncio.create_task(late_reader())
    await asyncio.sleep(0.05)
    check("the writer waits while a reader holds", order == [], order)
    await lock.release_shared()                  # reader A done
    await asyncio.gather(tw, tr)
    check("a waiting writer goes before a reader that arrived later", order == ["W", "R2"], order)


async def case_vpn_tabs_share_home_excludes():
    print("case_vpn_tabs_share_home_excludes")
    OPS.clear(); REAPS.clear(); LEASE.update(on=False, acquired=0, released=[])
    tabs = await asyncio.gather(
        worker("a", "mullvad", 6),
        worker("b", "mullvad", 6, delay=0.01),
        worker("h", "home", 3, delay=0.03),      # must wait for a and b
        worker("c", "mullvad", 3, delay=0.05),   # arrives while home waits: must queue behind it
    )
    check("no VPN-profile op while home was up", not [f for f in FAILS if "LEAK" in f], FAILS)
    a_b_overlap = any(t.startswith("a-") for t, _, _ in OPS[:4]) and \
                  any(t.startswith("b-") for t, _, _ in OPS[:4])
    check("two VPN tabs ran at the same time", a_b_overlap, OPS[:6])
    home_ops = [i for i, (t, _, r) in enumerate(OPS) if r == "home"]
    c_ops = [i for i, (t, _, _) in enumerate(OPS) if t.startswith("c-")]
    check("the home tab ran after the earlier VPN tabs",
          home_ops and min(home_ops) > max(i for i, (t, _, _) in enumerate(OPS) if t[0] in "ab"),
          OPS)
    check("a VPN tab arriving while home waited queued behind it",
          home_ops and c_ops and min(c_ops) > max(home_ops), OPS)
    check("the lease was taken once and released", LEASE["acquired"] == 1
          and LEASE["released"] == ["session-end"], LEASE)
    check("each worker had its own tab", len(set(tabs)) == 4, tabs)


async def case_escalation_waits_for_vpn_tabs():
    print("case_escalation_waits_for_vpn_tabs")
    OPS.clear(); REAPS.clear(); LEASE.update(on=False, acquired=0, released=[])
    await asyncio.gather(
        worker("x", "mullvad", 6, escalate_after=2),   # escalates mid-run
        worker("y", "mullvad", 8, delay=0.01),
    )
    check("no VPN-profile op while the escalated tab was home",
          not [f for f in FAILS if "LEAK" in f], FAILS)
    check("the escalated worker finished on home",
          any(t.startswith("x-") and r == "home" for t, _, r in OPS), OPS)


async def case_reap_only_at_edges():
    print("case_reap_only_at_edges")
    REAPS.clear()
    await asyncio.gather(worker("p", "mullvad", 4), worker("q", "mullvad", 4, delay=0.01))
    check("reaped once at the first hold and once at the last, not per worker",
          len(REAPS) == 2, len(REAPS))


async def case_cache_and_freshness():
    print("case_cache_and_freshness")
    import skills.web_utils as wu
    calls = []
    wu.scrape_url = lambda url: calls.append(url) or "Coffee $14.99/lb at the store"
    url = "https://store.example/coffee/beans"
    text, meta = gateway.fetch(url, kind="price")
    check("first fetch goes live", calls == [url] and not meta["cached"], (calls, meta))
    text, meta = gateway.fetch(url, kind="price")
    check("second fetch inside the window comes from the cache, with its age",
          len(calls) == 1 and meta["cached"] and "ago" in meta["age"], (calls, meta))
    tok = send_gate.begin("+15555550101", "1", text="check again on the coffee price")
    try:
        text, meta = gateway.fetch(url, kind="price")
    finally:
        send_gate.end(tok)
    check("'check again' in the message forces a live fetch", len(calls) == 2 and not meta["cached"],
          (calls, meta))
    wu.scrape_url = lambda url: "Scrape Error: timed out"
    text, meta = gateway.fetch(url, kind="price", fresh=True)
    check("a failed live fetch falls back to the cache and says so",
          meta.get("cached") and "live_failed" in meta and "14.99" in text, meta)
    check("reference pages stay fresh for a week, quotes for minutes",
          gateway.freshness("reference") > gateway.freshness("price") > gateway.freshness("quote"))


async def case_observations_and_history():
    print("case_observations_and_history")
    gateway.record_observation("coffee price at local store", "14.99", "USD/lb",
                               source_url="https://store.example/coffee/beans",
                               owner="+15555550101", request_id="R1")
    obs = gateway.find_observations("coffee price")
    check("an observation is found by its words", obs and obs[0]["value"] == "14.99", obs)
    h = gateway.history_text("coffee")
    check("web history shows the observation, the cached page and the log, with ages",
          "OBSERVATIONS" in h and "PAGES ON FILE" in h and "WEB LOG" in h and "ago" in h, h)


async def main():
    await case_lock_writer_preference()
    await case_vpn_tabs_share_home_excludes()
    await case_escalation_waits_for_vpn_tabs()
    await case_reap_only_at_edges()
    await case_cache_and_freshness()
    await case_observations_and_history()


asyncio.run(main())
if FAILS:
    print("\nFAILED:\n  " + "\n  ".join(FAILS))
    sys.exit(1)
print("\nall checks passed")
