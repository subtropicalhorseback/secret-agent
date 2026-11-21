"""
skills/skill_loader.py — new capabilities as self-describing skills
====================================================================
Added 20 Nov 2026 (One Voice spec, D.2: the build track).

Before this, adding a tool meant editing skills/tool_calling.py — an edit to
existing code, which is exactly what the additive build track must not need.
Now a skill is one new file, skills/<name>.py, that declares its own tools:

    CHIP_TOOLS = [{"name": "...", "description": "...", "input_schema": {...},
                   "any_user": False}]          # optional; default admin-only
    async def chip_tool(name, tool_input, *, sender, tier, session):
        return "result text for the model"

plus its own tests/test_<name>.py. The loader finds candidates by reading the
files (a file is a skill if it defines CHIP_TOOLS), and:
  * never imports a disabled skill — importing runs its code;
  * imports each enabled skill inside its own try, so one broken skill cannot
    take the others (or tool_calling) down;
  * refuses tool names that collide with core tools, explicit-only tools, or
    another skill's;
  * makes skill tools admin-only unless the definition says any_user.

`skill: off <name>` takes effect at once (the tools disappear from the next
call and execution is refused) and, from the next restart, the file is not
imported at all. State: data/skills_disabled.json (gitignored data/).
"""
import importlib
import json
import os
import re
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
from chip_config import WORKSPACE  # noqa: E402

DISABLED_FILE = f"{WORKSPACE}/data/skills_disabled.json"
_DECLARES = re.compile(r"^CHIP_TOOLS\s*=", re.M)

DEFS = []          # tool definitions from enabled skills (API-ready)
HANDLERS = {}      # tool name -> (skill name, coroutine function)
ANY_USER = set()   # tool names a non-admin may see
ERRORS = {}        # skill name -> why it was not loaded
LOADED = []        # skill names loaded in this process


def _disabled_path():
    return os.getenv("CHIP_SKILLS_DISABLED") or DISABLED_FILE


def disabled():
    try:
        return set(json.load(open(_disabled_path())))
    except Exception:
        return set()


def _write_disabled(names):
    p = _disabled_path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w") as f:
        json.dump(sorted(names), f)
    os.replace(tmp, p)


def candidates(skills_dir=None):
    d = skills_dir or _HERE
    out = []
    for fn in sorted(os.listdir(d)):
        if not fn.endswith(".py") or fn.startswith("_") or fn == "skill_loader.py":
            continue
        try:
            if _DECLARES.search(open(os.path.join(d, fn), errors="replace").read()):
                out.append(fn[:-3])
        except Exception:
            continue
    return out


def load(reserved_names, skills_dir=None, package="skills"):
    """Import enabled skills and collect their tools. Safe to call once at
    tool_calling import. Returns (DEFS, HANDLERS)."""
    DEFS.clear(); HANDLERS.clear(); ANY_USER.clear(); ERRORS.clear(); LOADED.clear()
    off = disabled()
    taken = set(reserved_names)
    for name in candidates(skills_dir):
        if name in off:
            ERRORS[name] = "disabled"
            continue
        try:
            mod = importlib.import_module(f"{package}.{name}")
            tools = getattr(mod, "CHIP_TOOLS", None)
            handler = getattr(mod, "chip_tool", None)
            if not isinstance(tools, list) or not tools or handler is None:
                raise ValueError("needs a non-empty CHIP_TOOLS list and async chip_tool()")
            staged = []
            for t in tools:
                tn = t.get("name")
                if not (isinstance(t, dict) and tn and t.get("description")
                        and isinstance(t.get("input_schema"), dict)):
                    raise ValueError(f"malformed tool definition {tn!r}")
                if tn in taken:
                    raise ValueError(f"tool name {tn!r} is already taken")
                staged.append(t)
            for t in staged:
                taken.add(t["name"])
                api = {k: v for k, v in t.items() if k in ("name", "description", "input_schema")}
                DEFS.append(api)
                HANDLERS[t["name"]] = (name, handler)
                if t.get("any_user"):
                    ANY_USER.add(t["name"])
            LOADED.append(name)
        except Exception as e:
            ERRORS[name] = f"{type(e).__name__}: {str(e)[:200]}"
            print(f"⚠️ skill {name} not loaded: {ERRORS[name]}")
    if LOADED:
        print(f"🧩 skills loaded: {', '.join(LOADED)}")
    return DEFS, HANDLERS


def visible_defs(tier):
    off = disabled()
    out = []
    for d in DEFS:
        skill = HANDLERS.get(d["name"], ("?",))[0]
        if skill in off:
            continue
        if tier != "admin" and d["name"] not in ANY_USER:
            continue
        out.append(d)
    return out


async def execute(tool_name, tool_input, *, sender, tier, session):
    skill, handler = HANDLERS[tool_name]
    if skill in disabled():
        return f"The {skill} skill is switched off."
    if tier != "admin" and tool_name not in ANY_USER:
        return f"{tool_name} is admin-only."
    try:
        return str(await handler(tool_name, tool_input, sender=sender, tier=tier,
                                 session=session))
    except Exception as e:
        return f"The {skill} skill failed: {type(e).__name__}: {str(e)[:200]}"


def set_enabled(name, on):
    name = re.sub(r"[^a-z0-9_]", "", (name or "").lower())
    if not name:
        return "Name the skill: 'skill: off <name>'."
    known = set(candidates())
    if name not in known:
        return f"No skill called {name}. Skills: {', '.join(sorted(known)) or 'none yet'}."
    off = disabled()
    if on:
        off.discard(name)
        _write_disabled(off)
        loaded = name in LOADED
        return (f"{name} is on." if loaded else
                f"{name} is on; it loads at the next restart.")
    off.add(name)
    _write_disabled(off)
    return f"{name} is off. Its tools are gone now, and it won't be loaded at the next restart."


def listing():
    known = candidates()
    if not known:
        return "No add-on skills yet."
    off = disabled()
    lines = []
    for n in known:
        state = ("off" if n in off else "on" if n in LOADED else
                 f"not loaded ({ERRORS.get(n, 'loads at next restart')})")
        tools = [t for t, (s, _) in HANDLERS.items() if s == n]
        lines.append(f"{n}: {state}" + (f" — tools: {', '.join(tools)}" if tools else ""))
    return "\n".join(lines)
