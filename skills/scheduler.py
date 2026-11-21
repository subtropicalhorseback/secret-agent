"""
skills/scheduler.py — Chip's own managed recurring-job store
=============================================================
Added 19 Nov 2026 so Chip can manage its own scheduled heartbeats without
touching the system crontab.

WHY THIS IS NOT THE SYSTEM CRONTAB
A crontab line is a shell command, and chip.service runs as the same user
that owns the crontab — so a tool that wrote crontab lines would hand the
model arbitrary shell execution. It must not have that. Instead:

  * Managed jobs live here, in a JSON store. A job carries a schedule, a
    target household user, and a NOTE. Firing a job never runs a command —
    it re-enters the coordinator's worker loop as an internal message, the
    exact same path a `schedule_followup` reminder already uses. The model
    can author the note; it can never author a command.
  * The real crontab is exposed READ-ONLY (`system_cron_entries`) so Chip
    can answer "what's scheduled on this box" accurately. There is no write
    path to it anywhere in this module.

Disable is a reversible flag. There is deliberately no hard-delete.

Schedules are validated BEFORE they are written (`parse_cron` raises on
anything malformed), so a bad expression is rejected at the tool boundary
rather than poisoning the store and failing at load.
"""

import os
import json
import fcntl
import subprocess
from datetime import datetime, timedelta

import os as _os, sys as _sys
_cc = _os.path.dirname(_os.path.abspath(__file__))
while _cc != "/" and not _os.path.exists(_os.path.join(_cc, "chip_config.py")):
    _cc = _os.path.dirname(_cc)
if _cc not in _sys.path:
    _sys.path.insert(0, _cc)
from chip_config import WORKSPACE

JOBS_FILE = f"{WORKSPACE}/data/scheduled_jobs.json"
_LOCK_FILE = JOBS_FILE + ".lock"

# A job overdue by more than this (service was down, box was off) has its
# cursor advanced WITHOUT firing — nobody wants six hours of backlogged
# heartbeats landing on their phone at once.
MAX_CATCHUP_HOURS = 6

MAX_JOBS = 50
MAX_NOTE_CHARS = 500


# ─── Cron parsing / validation ────────────────────────────────────────────────
# Standard 5-field vixie syntax, numeric only: minute hour dom month dow.
# No @reboot, no names, no 6-field seconds variant — if it isn't one of
# these, it is rejected rather than guessed at.

_FIELDS = [
    ("minute",      0, 59),
    ("hour",        0, 23),
    ("day-of-month", 1, 31),
    ("month",       1, 12),
    ("day-of-week", 0, 7),    # 7 is a legal alias for Sunday; folded to 0 below
]


def _parse_field(expr, lo, hi, label):
    """Expand one cron field into a set of ints. Raises ValueError."""
    values = set()
    for part in expr.split(","):
        part = part.strip()
        if not part:
            raise ValueError(f"{label}: empty element in '{expr}'")

        step = 1
        if "/" in part:
            base, _, step_s = part.partition("/")
            if not step_s.isdigit() or int(step_s) < 1:
                raise ValueError(f"{label}: bad step '/{step_s}'")
            step = int(step_s)
            part = base.strip()

        if part == "*":
            start, end = lo, hi
        elif "-" in part:
            a, _, b = part.partition("-")
            if not (a.strip().isdigit() and b.strip().isdigit()):
                raise ValueError(f"{label}: bad range '{part}'")
            start, end = int(a), int(b)
            if start > end:
                raise ValueError(f"{label}: reversed range '{part}'")
        elif part.isdigit():
            start = end = int(part)
        else:
            raise ValueError(
                f"{label}: '{part}' is not a number, range, or *")

        if start < lo or end > hi:
            raise ValueError(
                f"{label}: {part} outside allowed {lo}-{hi}")
        values.update(range(start, end + 1, step))

    if not values:
        raise ValueError(f"{label}: matches nothing")
    return values


def parse_cron(expr):
    """Validate a 5-field cron expression.

    Returns (minutes, hours, doms, months, dows, dom_is_star, dow_is_star).
    Raises ValueError with a human-readable reason on anything malformed.
    """
    if not isinstance(expr, str):
        raise ValueError("schedule must be a string")
    expr = expr.strip()
    if not expr:
        raise ValueError("schedule is empty")
    if expr.startswith("@"):
        raise ValueError(
            "@reboot/@daily shorthands are not supported — "
            "use 5 numeric fields, e.g. '0 7 * * *'")
    fields = expr.split()
    if len(fields) != 5:
        raise ValueError(
            f"expected 5 fields (minute hour day-of-month month day-of-week), "
            f"got {len(fields)}")

    sets = [_parse_field(f, lo, hi, label)
            for f, (label, lo, hi) in zip(fields, _FIELDS)]

    # Fold the dow=7 Sunday alias into 0 AFTER expansion, so ranges that
    # legitimately span it ('1-7' = Mon..Sun) survive intact.
    if 7 in sets[4]:
        sets[4].discard(7)
        sets[4].add(0)

    return (*sets, fields[2].strip() == "*", fields[4].strip() == "*")


def describe_cron(expr):
    """Best-effort plain-English gloss. Falls back to the raw expression."""
    try:
        parse_cron(expr)
    except ValueError:
        return expr
    common = {
        "* * * * *":     "every minute",
        "*/5 * * * *":   "every 5 minutes",
        "*/10 * * * *":  "every 10 minutes",
        "*/15 * * * *":  "every 15 minutes",
        "*/30 * * * *":  "every 30 minutes",
        "0 * * * *":     "hourly",
        "0 0 * * *":     "daily at midnight",
    }
    norm = " ".join(expr.split())
    if norm in common:
        return common[norm]
    m, h, dom, mon, dow = norm.split()
    if m.isdigit() and h.isdigit() and dom == "*" and mon == "*":
        at = f"{int(h):02d}:{int(m):02d}"
        if dow == "*":
            return f"daily at {at}"
        days = {"0": "Sunday", "1": "Monday", "2": "Tuesday", "3": "Wednesday",
                "4": "Thursday", "5": "Friday", "6": "Saturday"}
        if dow in days:
            return f"every {days[dow]} at {at}"
    return norm


def next_fire(expr, after=None):
    """Next datetime strictly after `after` that matches `expr`, or None if
    nothing matches inside ~400 days (e.g. '0 0 30 2 *')."""
    minutes, hours, doms, months, dows, dom_star, dow_star = parse_cron(expr)
    t = (after or datetime.now()).replace(second=0, microsecond=0) \
        + timedelta(minutes=1)
    limit = t + timedelta(days=400)

    while t < limit:
        if t.month not in months:
            t = (t.replace(day=1) + timedelta(days=32)).replace(
                day=1, hour=0, minute=0)
            continue
        if not _day_matches(t, doms, dows, dom_star, dow_star):
            t = (t + timedelta(days=1)).replace(hour=0, minute=0)
            continue
        if t.hour not in hours:
            t = (t + timedelta(hours=1)).replace(minute=0)
            continue
        if t.minute not in minutes:
            t += timedelta(minutes=1)
            continue
        return t
    return None


def _day_matches(t, doms, dows, dom_star, dow_star):
    """Vixie semantics: when BOTH day-of-month and day-of-week are
    restricted, a day matching EITHER one fires."""
    cron_dow = (t.weekday() + 1) % 7          # python Mon=0 → cron Sun=0
    if dom_star and dow_star:
        return True
    if dom_star:
        return cron_dow in dows
    if dow_star:
        return t.day in doms
    return t.day in doms or cron_dow in dows


# ─── Store ────────────────────────────────────────────────────────────────────
# Same flock discipline as skills/remind.py: three workers plus the checker
# share this file and an unlocked concurrent write would drop a job.

class _jobs_lock:
    def __enter__(self):
        os.makedirs(os.path.dirname(JOBS_FILE), exist_ok=True)
        self._fh = open(_LOCK_FILE, "w")
        fcntl.flock(self._fh, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        try:
            fcntl.flock(self._fh, fcntl.LOCK_UN)
            self._fh.close()
        except Exception:
            pass


def load_jobs():
    if not os.path.exists(JOBS_FILE):
        return []
    try:
        with _jobs_lock():
            data = json.load(open(JOBS_FILE))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def modify_jobs(mutate):
    """Atomic read-modify-write under ONE lock (see remind.modify_reminders)."""
    with _jobs_lock():
        try:
            jobs = (json.load(open(JOBS_FILE))
                    if os.path.exists(JOBS_FILE) else [])
            if not isinstance(jobs, list):
                jobs = []
        except Exception:
            jobs = []
        out = mutate(jobs)
        if out is None:
            out = jobs
        tmp = JOBS_FILE + ".tmp"
        json.dump(out, open(tmp, "w"), indent=2)
        os.replace(tmp, JOBS_FILE)
        return out


def _new_id(jobs):
    n = 1
    existing = {j.get("id") for j in jobs}
    while f"job{n:03d}" in existing:
        n += 1
    return f"job{n:03d}"


def find_job(job_id):
    jid = (job_id or "").strip().lower()
    for j in load_jobs():
        if j.get("id", "").lower() == jid:
            return j
    return None


def create_job(name, cron, target_phone, target_name, note, created_by):
    """Validates before writing. Raises ValueError on bad input."""
    name = (name or "").strip()
    note = (note or "").strip()
    if not name:
        raise ValueError("job needs a name")
    if not note:
        raise ValueError("job needs a note describing what to do when it fires")
    if len(note) > MAX_NOTE_CHARS:
        raise ValueError(f"note too long (max {MAX_NOTE_CHARS} chars)")
    parse_cron(cron)                           # raises if malformed
    if not target_phone:
        raise ValueError("job needs a known target user")

    created = {}

    def _m(jobs):
        if len(jobs) >= MAX_JOBS:
            raise ValueError(f"job limit reached ({MAX_JOBS})")
        if any(j.get("name", "").lower() == name.lower() for j in jobs):
            raise ValueError(f"a job named '{name}' already exists")
        now = datetime.now().isoformat(timespec="seconds")
        job = {
            "id":          _new_id(jobs),
            "name":        name,
            "cron":        " ".join(cron.split()),
            "target_phone": target_phone,
            "target_name": target_name,
            "note":        note,
            "enabled":     True,
            "created_at":  now,
            "created_by":  created_by,
            "cursor":      now,
            "last_fired":  None,
        }
        jobs.append(job)
        created.update(job)
        return jobs

    modify_jobs(_m)
    return created


def update_job(job_id, *, cron=None, target_phone=None, target_name=None,
               name=None, note=None):
    """Change schedule / target / name / note. Validates before writing."""
    if cron is not None:
        parse_cron(cron)                       # raises if malformed
    if note is not None and len(note.strip()) > MAX_NOTE_CHARS:
        raise ValueError(f"note too long (max {MAX_NOTE_CHARS} chars)")

    result = {}

    def _m(jobs):
        for j in jobs:
            if j.get("id", "").lower() != job_id.strip().lower():
                continue
            if cron is not None:
                j["cron"] = " ".join(cron.split())
                # Re-base the cursor so the new schedule takes effect from
                # now instead of instantly firing against the old one.
                j["cursor"] = datetime.now().isoformat(timespec="seconds")
            if target_phone is not None:
                j["target_phone"] = target_phone
                j["target_name"] = target_name
            if name is not None and name.strip():
                j["name"] = name.strip()
            if note is not None and note.strip():
                j["note"] = note.strip()
            result.update(j)
            return jobs
        raise ValueError(f"no job with id '{job_id}'")

    modify_jobs(_m)
    return result


def set_enabled(job_id, enabled):
    """Reversible state flag. There is no delete path by design."""
    result = {}

    def _m(jobs):
        for j in jobs:
            if j.get("id", "").lower() != job_id.strip().lower():
                continue
            j["enabled"] = bool(enabled)
            if enabled:
                # Don't fire for the whole disabled window on re-enable.
                j["cursor"] = datetime.now().isoformat(timespec="seconds")
            result.update(j)
            return jobs
        raise ValueError(f"no job with id '{job_id}'")

    modify_jobs(_m)
    return result


def due_and_advance(now=None):
    """Return the jobs that should fire now, advancing their cursors in the
    same locked write so a crash can't double-fire them."""
    now = now or datetime.now()
    fired = []

    def _m(jobs):
        for j in jobs:
            if not j.get("enabled", True):
                continue
            try:
                after = datetime.fromisoformat(
                    j.get("cursor") or j.get("created_at"))
            except Exception:
                after = now
            try:
                nxt = next_fire(j.get("cron", ""), after)
            except ValueError:
                continue                       # malformed: never fire it
            if nxt is None or nxt > now:
                continue
            j["cursor"] = now.isoformat(timespec="seconds")
            if (now - nxt) <= timedelta(hours=MAX_CATCHUP_HOURS):
                j["last_fired"] = now.isoformat(timespec="seconds")
                fired.append(dict(j))
        return jobs

    modify_jobs(_m)
    return fired


# ─── Read-only view of the system crontab ─────────────────────────────────────

def system_cron_entries():
    """Parse `crontab -l` into display rows. READ ONLY — this module never
    writes the crontab. Returns [] if crontab is unavailable."""
    try:
        out = subprocess.run(["crontab", "-l"], capture_output=True,
                             text=True, timeout=10)
        if out.returncode != 0:
            return []
        raw = out.stdout
    except Exception:
        return []

    rows = []
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        enabled = not stripped.startswith("#")
        body = stripped.lstrip("#").strip() if not enabled else stripped
        fields = body.split()
        if len(fields) < 6:
            continue                           # comment prose or an env line
        schedule = " ".join(fields[:5])
        try:
            parse_cron(schedule)
        except ValueError:
            continue                           # not a cron line
        command = " ".join(fields[5:])
        rows.append({
            "schedule": schedule,
            "command":  command,
            "enabled":  enabled,
        })
    return rows


# ═══ System job registry (config/jobs.yaml) ═════════════════════════════════
# Added 19 Nov 2026. Registry jobs run COMMANDS (via dispatcher.py); managed
# jobs above fire internal messages. The two stores never merge.
#
# The invariant: a tool can never author or alter a command. So tools do not
# write the registry file at all. The only tool-writable settings are
# `enabled` and `schedule`, kept in a separate overrides file, and only for
# unprotected jobs.

REGISTRY_FILE  = f"{WORKSPACE}/config/jobs.yaml"
OVERRIDES_FILE = f"{WORKSPACE}/data/job_overrides.json"
STATE_FILE     = f"{WORKSPACE}/data/job_state.json"
OVERRIDE_FIELDS = ("enabled", "schedule")           # the whole tool-writable surface
PROTECTED_JOBS  = frozenset({"signal_watchdog", "vpn_guard", "backup"})


def _json_rw(path, mutate=None, default=None):
    """Locked read (and optional atomic rewrite) of a small JSON file."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + ".lock", "w") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        try:
            data = json.load(open(path)) if os.path.exists(path) else default
        except Exception:
            data = default
        if data is None:
            data = {}
        if mutate is not None:
            out = mutate(data)
            data = data if out is None else out
            tmp = path + ".tmp"
            json.dump(data, open(tmp, "w"), indent=2)
            os.replace(tmp, path)
        return data


def load_registry():
    """Registry entries with overrides applied and paths filled in.
    Raises ValueError on a malformed file rather than running a bad job."""
    import yaml
    if not os.path.exists(REGISTRY_FILE):
        return []
    raw = yaml.safe_load(open(REGISTRY_FILE)) or []
    if not isinstance(raw, list):
        raise ValueError("config/jobs.yaml must be a list of jobs")
    overrides = _json_rw(OVERRIDES_FILE, default={})
    venv = f"{WORKSPACE}/venv"
    jobs, seen = [], set()
    for e in raw:
        name = str(e.get("name", "")).strip()
        if not name or name in seen:
            raise ValueError(f"jobs.yaml: missing or duplicate name {name!r}")
        seen.add(name)
        protected = bool(e.get("protected")) or name in PROTECTED_JOBS
        job = {
            "name": name,
            "schedule": str(e.get("schedule", "")),
            "command": str(e.get("command", "")).replace("{WORKSPACE}", WORKSPACE).replace("{VENV}", venv),
            "log": os.path.join(WORKSPACE, str(e.get("log", f"data/logs/{name}.log"))),
            "enabled": bool(e.get("enabled", True)),
            "protected": protected,
            "runner": str(e.get("runner", "dispatcher")),
            "timeout_seconds": int(e.get("timeout_seconds", 300)),
            "max_failures": max(1, int(e.get("max_failures", 3))),
            "env": {str(k): str(v) for k, v in (e.get("env") or {}).items()},
            "warn_on_disable": e.get("warn_on_disable"),
        }
        if protected:
            job["enabled"] = True                      # forced, whatever the file says
        else:
            ov = overrides.get(name, {})
            if "enabled" in ov:
                job["enabled"] = bool(ov["enabled"])
            if ov.get("schedule"):
                job["schedule"] = str(ov["schedule"])
        parse_cron(job["schedule"])                    # raises on a bad expression
        if not job["command"]:
            raise ValueError(f"jobs.yaml: {name} has no command")
        jobs.append(job)
    return jobs


_ERRORISH = ("error", "no such", "cannot", "not found", "denied", "refused",
             "traceback", "failed to", "fatal", "exception", "timed out")

# Lines that announce a failure without saying what it was. tar signs off with
# "Exiting with failure status due to previous errors" and backup.sh adds
# "Backup FAILED" — both match an error keyword, neither is the cause.
_GENERIC = ("previous error", "exiting with failure", "see above",
            "error(s) occurred", "errors occurred")


def tail_highlight(tail):
    """The most useful single line of a captured output tail.

    The literal last line is usually the script's own epilogue, which says
    nothing the exit code didn't. Prefer the last line that names a cause
    ("tar: ... Cannot stat: No such file or directory"), ignoring generic
    wrap-up lines, and fall back only if nothing better is there.
    """
    lines = [ln.strip() for ln in (tail or "").splitlines() if ln.strip()]
    if not lines:
        return ""
    errorish = [ln for ln in lines if any(k in ln.lower() for k in _ERRORISH)]
    specific = [ln for ln in errorish
                if not any(g in ln.lower() for g in _GENERIC)]
    for candidates in (specific, errorish, lines):
        if candidates:
            return candidates[-1]
    return ""


def _interval(expr, now=None):
    a = next_fire(expr, now)
    b = next_fire(expr, a) if a else None
    return (b - a) if (a and b) else timedelta(days=1)


def registry_entries():
    """Display rows: registry + run state, for list_scheduled_jobs."""
    state = _json_rw(STATE_FILE, default={})
    rows = []
    for j in load_registry():
        s = state.get(j["name"], {})
        rows.append({**{k: j[k] for k in ("name", "schedule", "enabled", "protected",
                                          "runner", "warn_on_disable", "max_failures")},
                     "last_run": s.get("last_run"), "last_success": s.get("last_success"),
                     "last_start": s.get("last_start"), "last_finish": s.get("last_finish"),
                     "duration_seconds": s.get("duration_seconds"),
                     "last_exit_code": s.get("last_exit_code"),
                     "last_outcome": s.get("last_outcome"),
                     "last_note": s.get("last_note"),
                     # The diagnosable bit: why it failed, not just that it did.
                     "last_output_tail": s.get("last_output_tail"),
                     "alerted": bool(s.get("alerted")),
                     "consecutive_failures": s.get("consecutive_failures", 0)})
    return rows


def registry_problems(now=None):
    """Jobs that need a human: 3+ failures in a row, or no success within
    three expected intervals. Surfaced unprompted in the nightly report -
    a job nobody can see failing is the real hazard, not a job nobody can
    switch off."""
    now = now or datetime.now()
    out = []
    state = _json_rw(STATE_FILE, default={})
    for j in load_registry():
        if not j["enabled"] or j["runner"] != "dispatcher":
            continue
        s = state.get(j["name"], {})
        fails = s.get("consecutive_failures", 0)
        if fails >= j["max_failures"]:
            why = s.get("last_outcome") or "exit"
            hint = tail_highlight(s.get("last_output_tail"))
            because = f" - {hint[:120]}" if hint else ""
            out.append(f"{j['name']}: {fails} failures in a row "
                       f"({why}, exit {s.get('last_exit_code')}){because}")
            continue
        since = s.get("last_success") or s.get("first_seen")
        if since:
            age = now - datetime.fromisoformat(since)
            if age > 3 * _interval(j["schedule"], now) + timedelta(minutes=10):
                what = "no successful run" if not s.get("last_success") else "last success"
                out.append(f"{j['name']}: {what} for {int(age.total_seconds() // 3600)}h "
                           f"(expected {describe_cron(j['schedule'])})")
    return out


def _set_override(name, field, value):
    if field not in OVERRIDE_FIELDS:
        raise ValueError(f"{field!r} cannot be changed by a tool")
    job = next((j for j in load_registry() if j["name"] == name), None)
    if job is None:
        raise ValueError(f"no system job named {name!r}")
    if job["protected"]:
        raise ValueError(f"{name} is protected and cannot be changed")
    if job["runner"] != "dispatcher":
        raise ValueError(f"{name} runs from the system crontab and cannot be changed here")

    def _m(d):
        d.setdefault(name, {})[field] = value
    _json_rw(OVERRIDES_FILE, _m, default={})
    return next(j for j in load_registry() if j["name"] == name)


def set_registry_enabled(name, enabled):
    return _set_override(name, "enabled", bool(enabled))


def reschedule_registry_job(name, cron):
    parse_cron(cron)                                   # validate before writing
    return _set_override(name, "schedule", str(cron).strip())
