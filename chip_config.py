"""
chip_config.py — single source of truth for identity & household config
========================================================================
Added 19 Nov 2026 so the codebase carries NO personal data: phone numbers,
names, personas, and locations live in config/users.json (gitignored), with
config/users.example.json as the shareable placeholder shape. The audit had
flagged the user/phone map duplicated across 4+ files; it was actually in 9.

Resolution order: $CHIP_USERS_FILE → config/users.json → config/users.example.json.
Env overrides: SENDER_NUMBER (bot's own number), CHIP_HOME_LOCATION.
"""
import json
import os
import re

_HERE = os.path.dirname(os.path.abspath(__file__))


def _resolve_workspace():
    """CHIP_WORKSPACE env var → CHIP_WORKSPACE in .env beside this file →
    /opt/chip. Cron jobs and some systemd units start with a bare environment
    (and the browser unit cannot read .env at all), so callers that matter set
    the variable explicitly; the .env fallback covers everything else."""
    ws = os.getenv("CHIP_WORKSPACE")
    if not ws:
        try:
            for line in open(os.path.join(_HERE, ".env")):
                if line.startswith("CHIP_WORKSPACE="):
                    ws = line.split("=", 1)[1].strip().strip('"').strip("'")
                    break
        except Exception:
            pass
    ws = ws or "/opt/chip"
    if not os.path.isdir(ws):
        print(f"WARNING chip_config: workspace {ws} does not exist — "
              f"set CHIP_WORKSPACE")
    return ws


WORKSPACE = _resolve_workspace()

_cfg_path = (os.getenv("CHIP_USERS_FILE")
             or (p := f"{WORKSPACE}/config/users.json") and (p if os.path.exists(p) else None)
             or f"{WORKSPACE}/config/users.example.json")

try:
    _cfg = json.load(open(_cfg_path))
except Exception as e:
    print(f"⚠️ chip_config: could not load {_cfg_path}: {e} — empty config")
    _cfg = {"users": {}}

BOT_NUMBER      = os.getenv("SENDER_NUMBER") or _cfg.get("bot_number", "")
HOME_LOCATION   = os.getenv("CHIP_HOME_LOCATION") or _cfg.get("home_location", "Springfield,IL")
HOUSEHOLD_BLURB = _cfg.get("household_blurb", "")

# phone -> {"name","tier","history","default_model", optional: "persona",
#            "profile_focus","media_catalog","child"}
USERS_FULL = _cfg.get("users", {})

# The compact shape most of the codebase consumes.
USERS = {phone: {"name": u.get("name", "User"),
                 "tier": u.get("tier", "user"),
                 "history": u.get("history", 10),
                 "default_model": u.get("default_model", "cloud_default")}
         for phone, u in USERS_FULL.items()}

ADMIN_PHONE = next((p for p, u in USERS_FULL.items()
                    if u.get("tier") == "admin"), BOT_NUMBER)
ADMIN_NAME  = USERS_FULL.get(ADMIN_PHONE, {}).get("name", "Admin")


def name_by_phone(phone, default=""):
    return USERS_FULL.get(phone, {}).get("name", default)


def phone_by_name(name):
    name = (name or "").lower()
    for p, u in USERS_FULL.items():
        if u.get("name", "").lower() == name:
            return p
    return None


def persona(phone):
    return USERS_FULL.get(phone, {}).get("persona", "")


def profile_focus(name):
    for u in USERS_FULL.values():
        if u.get("name", "").lower() == (name or "").lower():
            return u.get("profile_focus", "")
    return ""


def is_child(phone_or_name):
    u = USERS_FULL.get(phone_or_name)
    if u is None:
        p = phone_by_name(phone_or_name)
        u = USERS_FULL.get(p, {}) if p else {}
    return bool(u.get("child"))


def media_catalog_user(phone):
    return bool(USERS_FULL.get(phone, {}).get("media_catalog"))


def known_users_line():
    """e.g. 'the admin (+1555...), the partner (+1555...)' — for parser prompts."""
    return ", ".join(f"{u.get('name','?')} ({p})" for p, u in USERS_FULL.items())


HOUSE_MOTTO = _cfg.get("house_motto", "")


def load_config_module(name):
    """config/<name>.py (the household's real, gitignored file) if present,
    else the shipped config/<name>.example.py, else None."""
    import importlib.util
    for fname in (f"{name}.py", f"{name}.example.py"):
        path = f"{WORKSPACE}/config/{fname}"
        if not os.path.exists(path):
            continue
        try:
            spec = importlib.util.spec_from_file_location(f"chip_cfg_{name}", path)
            mod  = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return mod
        except Exception as e:
            print(f"WARNING chip_config: config/{fname} failed to load ({e})")
    print(f"WARNING chip_config: no config/{name}(.example).py - using inline defaults")
    return None


_dom = load_config_module("domains")
VAULT_DOMAINS        = dict(getattr(_dom, "VAULT_DOMAINS", {"general": "everything else"}))
PREFIX_DOMAINS       = list(getattr(_dom, "PREFIX_DOMAINS", ["general", "private"]))
SENSITIVITY_IMMUNE   = set(getattr(_dom, "SENSITIVITY_IMMUNE", set()))
ALWAYS_SENSITIVE     = set(getattr(_dom, "ALWAYS_SENSITIVE", {"personal"}))
OCR_PRIORITY_DOMAINS = set(getattr(_dom, "OCR_PRIORITY_DOMAINS", {"personal", "private"}))
EXTRA_SENSITIVITY_PATTERNS = list(getattr(_dom, "EXTRA_SENSITIVITY_PATTERNS", []))
FOLDER_MAP           = dict(getattr(_dom, "FOLDER_MAP", {"general": ("general", 0)}))
TAXONOMY_EXAMPLES    = getattr(_dom, "TAXONOMY_EXAMPLES", "home, work, personal, media, finance")
ANALYSIS_LANGUAGE    = getattr(_dom, "ANALYSIS_LANGUAGE", "calibrated probabilistic language")
DOMAIN_LIST_TEXT     = ", ".join(VAULT_DOMAINS)
PREFIX_DOMAIN_REGEX  = "|".join(re.escape(d) for d in PREFIX_DOMAINS)
