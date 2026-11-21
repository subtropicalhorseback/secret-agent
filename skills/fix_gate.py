"""
skills/fix_gate.py — code, inference, code: the check after every heal
=======================================================================
Added 20 Nov 2026 (One Voice spec, D.5). the admin's "deterministic sandwich":
code sets the limits before a healer runs, the healer works inside them, and
this script — not the healer's own summary — decides the status after.

The runner (skills/self_heal.py) executes THIS FILE AS IT WAS AT THE STARTING
COMMIT (git show <start>:skills/fix_gate.py), so a healer cannot weaken the
check it is judged by. The files that make up the check are protected: a
change to any of them fails the gate unless the order named that exact file.

Checks, in order (any failure → "failed"):
  1. history     the starting commit is still an ancestor of HEAD
  2. committed   no uncommitted changes to tracked files
  3. scope       repair: only the files the order allowed; build: only NEW
                 files skills/<name>.py and tests/test_<name>.py
  4. protected   the gate machinery and every pre-existing test unchanged
  5. compile     every changed .py compiles
  6. tests       every tests/test_*.py passes (old ones and new ones)
  7. names       no new household names, numbers or places in tracked files
  8. restart     chip restarts through safe_restart and comes back up
  9. live        every model name resolves on its own client; a real message
                 gets exactly one reply (tests/live_turns.py); a real page
                 loads through the gateway
 10. acceptance  the order's own acceptance command, if it has one

Status: all pass and the acceptance command passed → "verified live";
all pass and no acceptance command → "built, not verified"; else "failed".

CLI (prints one JSON object on the last line; exit 0 pass, 1 fail):
  python fix_gate.py --root WS --start SHA --track repair|build
                     [--allowed '["skills/x.py", "docs/*"]'] [--skill NAME]
                     [--acceptance-cmd CMD] [--no-restart] [--no-live]
"""
import argparse
import fnmatch
import json
import os
import re
import subprocess
import sys
import time

PROTECTED = [
    "skills/fix_gate.py", "skills/self_heal.py", "skills/fix_index.py",
    "skills/model_check.py", "skills/safe_restart.py", "tests/live_turns.py",
    ".claude/skills/ship-a-fix/SKILL.md",
]
TEST_TIMEOUT = 300


def sh(args, root, timeout=120, env=None):
    r = subprocess.run(args, cwd=root, capture_output=True, text=True, timeout=timeout, env=env)
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def git(root, *args, timeout=60):
    code, out = sh(["git", *args], root, timeout=timeout)
    return code, out.strip()


def changed_files(root, start):
    """{path: status} for committed changes start..HEAD (A/M/D/R...)."""
    code, out = git(root, "diff", "--name-status", "-M", f"{start}..HEAD")
    files = {}
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) >= 2:
            st = parts[0][0]
            files[parts[-1]] = st
            if st == "R" and len(parts) == 3:
                files[parts[1]] = "D"
    return files


def _names_tokens(root):
    """Household identifiers from the gitignored config, never printed."""
    toks = {"case": set(), "nocase": set(), "digits": set()}
    try:
        cfg = json.load(open(os.path.join(root, "config", "users.json")))
    except Exception:
        return toks
    for phone, u in (cfg.get("users") or {}).items():
        d = re.sub(r"\D", "", phone)
        if len(d) >= 10:
            toks["digits"].add(d[-10:])
        n = (u.get("name") or "").strip()
        if len(n) >= 3:
            toks["case"].add(n)
    bot = re.sub(r"\D", "", cfg.get("bot_number", ""))
    if len(bot) >= 10:
        toks["digits"].add(bot[-10:])
    for part in re.split(r"[,\s]+", cfg.get("home_location", "")):
        if len(part) >= 4 and part.isalpha():
            toks["nocase"].add(part.lower())
    return toks


def _count(text, toks):
    n = 0
    for t in toks["case"]:
        n += len(re.findall(rf"(?<![A-Za-z]){re.escape(t)}(?![a-z])", text))
    low = text.lower()
    for t in toks["nocase"]:
        n += len(re.findall(rf"(?<![a-z]){re.escape(t)}(?![a-z])", low))
    digits = re.sub(r"\D", "", text)
    for t in toks["digits"]:
        n += digits.count(t)
    return n


def run(root, start, track="repair", allowed=None, skill=None, acceptance_cmd=None,
        restart=True, live=True):
    checks = []

    def add(name, ok, detail=""):
        checks.append({"check": name, "ok": bool(ok), "detail": str(detail)[:600]})
        return ok

    def verdict(status, reason=""):
        return {"status": status, "reason": reason, "checks": checks,
                "changed": changed, "start": start}

    changed = {}
    # 1. history
    code, _ = git(root, "merge-base", "--is-ancestor", start, "HEAD")
    if not add("history", code == 0, "" if code == 0 else
               f"{start} is no longer an ancestor of HEAD — history was rewritten"):
        return verdict("failed", "history rewritten")
    changed = changed_files(root, start)

    # 2. committed — untracked (non-ignored) files count too: an uncommitted
    # skills/x.py would otherwise skip every check below yet still be found
    # by the skill loader and the test runner on restart.
    code, out = git(root, "status", "--porcelain", "--untracked-files=all")
    if not add("committed", not out.strip(), out[:300]):
        return verdict("failed", "uncommitted or untracked files in the tree")

    # 3. scope
    if track == "build":
        name = re.sub(r"[^a-z0-9_]", "", (skill or "").lower())
        ok_paths = {f"skills/{name}.py", f"tests/test_{name}.py"} if name else set()
        bad = [f"{p} ({s})" for p, s in changed.items()
               if not (s == "A" and p in ok_paths)]
        made = changed.get(f"skills/{name}.py") == "A" if name else False
        if not add("scope", name and made and not bad,
                   ("no skill name" if not name else
                    f"the build did not add skills/{name}.py" if not made else
                    f"build track may only ADD {sorted(ok_paths)}; also touched: {bad}")):
            return verdict("failed", "outside the build track's scope" if bad or not name
                           else "the build added nothing")
    else:
        pats = allowed or ["*"]
        bad = [p for p in changed if not any(fnmatch.fnmatch(p, g) for g in pats)]
        if not add("scope", not bad, f"not in the order's allowed files {pats}: {bad}"):
            return verdict("failed", "touched files the order did not allow")

    # 4. protected (exact paths only can lift protection)
    explicit = set(allowed or [])
    hit = [p for p in changed if p in PROTECTED and p not in explicit]
    hit += [f"{p} ({s})" for p, s in changed.items()
            if p.startswith("tests/") and s in ("M", "D", "R") and p not in explicit]
    if not add("protected", not hit, f"changed protected files: {hit}"):
        return verdict("failed", "changed the gate machinery or an existing test")

    # 5. compile
    py = [p for p, s in changed.items() if s != "D" and p.endswith(".py")]
    errs = []
    for p in py:
        code, out = sh([sys.executable, "-m", "py_compile", p], root, timeout=60)
        if code:
            errs.append(f"{p}: {out[-200:]}")
    if not add("compile", not errs, "; ".join(errs)):
        return verdict("failed", "a changed file does not compile")

    # 6. tests
    tdir = os.path.join(root, "tests")
    fails = []
    tests = sorted(f for f in os.listdir(tdir) if f.startswith("test_") and f.endswith(".py")) \
        if os.path.isdir(tdir) else []
    for t in tests:
        try:
            code, out = sh([sys.executable, os.path.join("tests", t)], root, timeout=TEST_TIMEOUT)
        except subprocess.TimeoutExpired:
            code, out = 124, "timed out"
        if code:
            fails.append(f"{t}: {out.strip()[-240:]}")
    if not add("tests", not fails, (f"{len(tests)} passed" if not fails else
                                    f"{len(fails)}/{len(tests)} failed: " + " | ".join(fails))):
        return verdict("failed", "tests failed")

    # 7. names (new occurrences only, so existing text never blocks a fix)
    toks = _names_tokens(root)
    grew = []
    for p, s in changed.items():
        if s == "D":
            continue
        try:
            now = open(os.path.join(root, p), errors="replace").read()
        except Exception:
            continue
        code, before = git(root, "show", f"{start}:{p}")
        before = before if code == 0 else ""
        if _count(now, toks) > _count(before, toks):
            grew.append(p)
    if not add("names", not grew, f"new household identifiers in: {grew}"):
        return verdict("failed", "household names added to tracked files")

    # 8. restart
    if restart:
        code, out = sh([sys.executable, "skills/safe_restart.py"], root, timeout=900)
        up = code == 0
        if up:
            # the coordinator's own startup check has to have run
            log = os.path.join(root, "coordinator.log")
            t0 = time.time()
            seen = False
            while time.time() - t0 < 90 and not seen:
                try:
                    with open(log, errors="replace") as f:
                        f.seek(max(0, os.path.getsize(log) - 40000))
                        seen = "model check:" in f.read()
                except Exception:
                    pass
                if not seen:
                    time.sleep(3)
            up = seen
        if not add("restart", up, out[-300:]):
            return verdict("failed", "chip did not come back up")

    # 9. live
    if live:
        code, out = sh([sys.executable, "skills/model_check.py"], root, timeout=120)
        if not add("live:models", code == 0, out.strip().splitlines()[-1] if out.strip() else ""):
            return verdict("failed", "a model name does not resolve on its client")
        code, out = sh([sys.executable, "tests/live_turns.py", "--expect", "4|four",
                        "quick: what is 2 plus 2"], root, timeout=300)
        if not add("live:message", code == 0, out.strip()[-300:]):
            return verdict("failed", "a real message did not get exactly one reply")
        code, out = sh([sys.executable, "-c",
                        "import sys; sys.path.insert(0,'.'); from skills import gateway; "
                        "t,m=gateway.fetch('https://example.com', fresh=True); "
                        "sys.exit(0 if 'Example Domain' in t else 1)"], root, timeout=120)
        if not add("live:web", code == 0, out.strip()[-200:]):
            return verdict("failed", "a real page did not load through the gateway")

    # 10. acceptance
    if acceptance_cmd:
        try:
            code, out = sh(["bash", "-lc", acceptance_cmd], root, timeout=600)
        except subprocess.TimeoutExpired:
            code, out = 124, "timed out"
        if not add("acceptance", code == 0, out.strip()[-400:]):
            return verdict("failed", "the order's acceptance check failed")
        return verdict("verified live")
    add("acceptance", True, "no acceptance command — the change itself was not exercised")
    return verdict("built, not verified")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--start", required=True)
    ap.add_argument("--track", default="repair", choices=["repair", "build"])
    ap.add_argument("--allowed", default="[]")
    ap.add_argument("--skill")
    ap.add_argument("--acceptance-cmd")
    ap.add_argument("--no-restart", action="store_true")
    ap.add_argument("--no-live", action="store_true")
    a = ap.parse_args()
    v = run(a.root, a.start, a.track, json.loads(a.allowed or "[]") or None, a.skill,
            a.acceptance_cmd, restart=not a.no_restart, live=not a.no_live)
    print(json.dumps(v))
    sys.exit(0 if v["status"] != "failed" else 1)


if __name__ == "__main__":
    main()
