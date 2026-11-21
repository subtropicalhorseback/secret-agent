"""
wx.py — Chip Weather Skill
===========================
Merged !wx command: lookup, scheduled location override, rain alerts.

Usage:
  !wx                      current weather at the home location
  !wx Chicago              lookup Chicago
  !wx Chicago 7            set Chicago as scheduled location for 7 days
  !wx reset                reset scheduled location to home

NOTE: wttr.in has known accuracy issues (backlog May 2026).
TODO: Evaluate Tomorrow.io or OpenWeatherMap as replacement.

Rain alert checker runs as background task in coordinator.
Call start_rain_checker(session) from main().
"""

import os
import asyncio
import aiohttp
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

SIGNAL_URL = os.getenv("SIGNAL_API_URL", "http://localhost:8080").rstrip("/")
SENDER     = os.getenv("SENDER_NUMBER")

from chip_config import HOME_LOCATION as DEFAULT_LOCATION

# Per-sender location overrides — in-memory, resets on restart
# Populated by coordinator on !wx <location> <days>
wx_overrides = {}

# Rain alert state
rain_state = {
    "state":       "CLEAR",   # CLEAR | WARNED | RAINING
    "warned_at":   None,
    "clear_since": None,
}

# ─── wttr.in fetch ────────────────────────────────────────────────────────────

async def fetch_wttr(session, location):
    loc = location.replace(" ", "+")
    url = f"https://wttr.in/{loc}?format=j1"
    async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
        resp.raise_for_status()
        return await resp.json(content_type=None)

def format_summary(data, location):
    cur   = data["current_condition"][0]
    today = data["weather"][0]
    tom   = data["weather"][1]

    desc        = cur["weatherDesc"][0]["value"]
    temp        = cur["temp_F"]
    feels       = cur["FeelsLikeF"]
    humid       = cur["humidity"]
    wind        = cur["windspeedMiles"]
    wdir        = cur["winddir16Point"]

    hi          = today["maxtempF"]
    lo          = today["mintempF"]
    hourly      = today["hourly"]
    rain_pct    = max(int(h["chanceofrain"])    for h in hourly)
    thunder_pct = max(int(h["chanceofthunder"]) for h in hourly)
    precip      = sum(float(h["precipInches"])  for h in hourly)

    tom_hi      = tom["maxtempF"]
    tom_lo      = tom["mintempF"]
    tom_hourly  = tom["hourly"]
    tom_rain    = max(int(h["chanceofrain"])   for h in tom_hourly)
    tom_precip  = sum(float(h["precipInches"]) for h in tom_hourly)

    rain_flag    = " ⚠️" if rain_pct    >= 70 else ""
    thunder_flag = " ⚡" if thunder_pct >= 30 else ""
    tom_flag     = " ⚠️" if tom_rain    >= 70 else ""

    today_line = f"Today: Hi {hi}° / Lo {lo}° | Rain: {rain_pct}%{rain_flag}{thunder_flag}"
    if precip > 0.01:
        today_line += f" ~{precip:.2f}in"

    tom_line = f"Tomorrow: Hi {tom_hi}° / Lo {tom_lo}° | Rain: {tom_rain}%{tom_flag}"
    if tom_precip > 0.01:
        tom_line += f" ~{tom_precip:.2f}in"

    return "\n".join([
        f"{location}",
        f"Now: {desc} {temp}°F (feels {feels}°F) | {humid}% humidity | {wind}mph {wdir}",
        today_line,
        tom_line,
    ])

# ─── Location override management ─────────────────────────────────────────────

def get_location(sender):
    ov = wx_overrides.get(sender, {})
    if ov.get("location") and ov.get("expires"):
        if datetime.now() < ov["expires"]:
            return ov["location"]
        else:
            wx_overrides.pop(sender, None)
    return DEFAULT_LOCATION

def set_location(sender, location, days):
    wx_overrides[sender] = {
        "location": location,
        "expires":  datetime.now() + timedelta(days=days),
    }

# ─── Main handler ─────────────────────────────────────────────────────────────

async def get_weather(session, sender, args):
    """
    Called by coordinator handle_wx().
    Returns (answer, tag).
    """
    from coordinator import TAGS
    tag = TAGS["chip"]

    args = (args or "").strip()

    # !wx reset
    if args.lower() == "reset":
        wx_overrides.pop(sender, None)
        return f"Scheduled weather reset to {DEFAULT_LOCATION}.", tag

    # !wx <location> <days> — set override
    parts = args.split()
    if len(parts) >= 2 and parts[-1].isdigit():
        location = " ".join(parts[:-1])
        days     = int(parts[-1])
        set_location(sender, location, days)
        try:
            data   = await fetch_wttr(session, location)
            answer = format_summary(data, location)
            answer = f"Location set to {location} for {days} day(s).\n\n{answer}"
        except Exception as e:
            answer = f"Location set to {location} for {days} days. Lookup failed: {e}"
        return answer, tag

    # !wx <location> — one-time lookup
    if args and not args[-1].isdigit():
        location = args
    else:
        location = get_location(sender)

    try:
        data   = await fetch_wttr(session, location)
        answer = format_summary(data, location)
    except Exception as e:
        answer = f"Weather lookup failed for {location}: {e}"

    return answer, tag

# ─── Rain alert background task ───────────────────────────────────────────────

async def rain_alert_checker(session):
    """
    Background task — call start_rain_checker(session) from coordinator main().
    Checks every 30 min. Alerts on crossing thresholds.
    """
    from coordinator import send_signal, USERS
    print("🌧️ Rain alert checker online.")
    await asyncio.sleep(90)  # let coordinator settle

    while True:
        try:
            data   = await fetch_wttr(session, DEFAULT_LOCATION)
            today  = data["weather"][0]
            now    = datetime.now()
            hourly = today["hourly"]

            # Find next 3-hour slot
            next_slot = hourly[-1]
            for h in hourly:
                if int(h["time"]) // 100 >= now.hour:
                    next_slot = h
                    break

            pct   = int(next_slot["chanceofrain"])
            state = rain_state["state"]

            if state == "CLEAR" and pct >= 70:
                rain_state["state"]    = "WARNED"
                rain_state["warned_at"] = now
                msg = (f"🌧️ Rain alert — {DEFAULT_LOCATION}\n"
                       f"Rain probability next few hours: {pct}%")
                for phone in USERS:
                    await send_signal(session, msg, recipient=phone, kind="scheduled")
                    await asyncio.sleep(1)

            elif state == "WARNED":
                if pct > 90:
                    rain_state["state"] = "RAINING"
                elif pct >= 70:
                    warned_at = rain_state.get("warned_at")
                    if warned_at and (now - warned_at) >= timedelta(hours=4):
                        rain_state["warned_at"] = now
                        msg = f"🌧️ Rain still likely — {pct}% chance."
                        for phone in USERS:
                            await send_signal(session, msg, recipient=phone, kind="scheduled")
                            await asyncio.sleep(1)
                else:
                    rain_state["state"]    = "CLEAR"
                    rain_state["warned_at"] = None

            elif state == "RAINING":
                if pct < 50:
                    if rain_state["clear_since"] is None:
                        rain_state["clear_since"] = now
                    elif (now - rain_state["clear_since"]) >= timedelta(hours=6):
                        rain_state["state"]       = "CLEAR"
                        rain_state["clear_since"] = None
                else:
                    rain_state["clear_since"] = None

        except Exception as e:
            print(f"⚠️ Rain checker error: {e}")

        await asyncio.sleep(1800)

async def start_rain_checker(session):
    asyncio.create_task(rain_alert_checker(session))
