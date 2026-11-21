"""The gate after every heal, rollback, and the skill loader (One Voice spec D.2/D.5).

Standalone, no pytest. Builds a small throwaway git repo per case, so nothing
touches the real repository, the real service or a real healer. The gate runs
with --no-restart --no-live (the live half is exercised by tests/live_turns.py).
Exits non-zero on failure.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
TMP = tempfile.mkdtemp(prefix="fix_gate_test_")
os.environ.setdefault("CHIP_WORKLOG_DIR", os.path.join(TMP, "worklog"))
os.environ["CHIP_SKILLS_DISABLED"] = os.path.join(TMP, "skills_disabled.json")

from skills import fix_gate, skill_loader  # noqa: E402
import skills.self_heal as self_heal  # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")
    if not cond:
        FAILS.append(f"{name}: {str(detail)[:400]}")


def git(repo, *a):
    return subprocess.run(["git", "-C", repo, *a], capture_output=True, text=True)


def w(repo, rel, text):
    p = os.path.join(repo, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    open(p, "w").write(text)


def base_repo():
    """skills/a.py, skills/fix_gate.py, one passing test, a household config."""
    repo = tempfile.mkdtemp(prefix="repo_", dir=TMP)
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "t@example.invalid")
    git(repo, "config", "user.name", "test")
    w(repo, "skills/__init__.py", "")
    w(repo, "skills/a.py", "def a():\n    return 1\n")
    w(repo, "skills/fix_gate.py", "# the gate\n")
    w(repo, "tests/test_ok.py", "import sys\nsys.exit(0)\n")
    w(repo, ".gitignore", "config/\n__pycache__/\n*.pyc\n*.bak*\n")   # as the real repo
    w(repo, "config/users.json", json.dumps({
        "users": {"+15555550142": {"name": "Zephyrine"}},
        "home_location": "Quillfield,ZZ"}))
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "start")
    start = git(repo, "rev-parse", "HEAD").stdout.strip()
    return repo, start


def commit(repo, msg="change"):
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", msg)


def gate(repo, start, **kw):
    return fix_gate.run(repo, start, restart=False, live=False, **kw)


print("repair track")
repo, start = base_repo()
w(repo, "skills/a.py", "def a():\n    return 2\n"); commit(repo)
v = gate(repo, start, track="repair", allowed=["skills/a.py"])
check("an allowed, committed, tested change passes as 'built, not verified'",
      v["status"] == "built, not verified", v)

repo, start = base_repo()
w(repo, "skills/a.py", "def a():\n    return 2\n"); commit(repo)
v = gate(repo, start, track="repair", allowed=["skills/a.py"], acceptance_cmd="true")
check("with a passing acceptance command it is 'verified live'", v["status"] == "verified live", v)
v = gate(repo, start, track="repair", allowed=["skills/a.py"], acceptance_cmd="false")
check("a failing acceptance command fails the gate",
      v["status"] == "failed" and "acceptance" in v["reason"], v)

repo, start = base_repo()
w(repo, "skills/b.py", "x = 1\n"); commit(repo)
v = gate(repo, start, track="repair", allowed=["skills/a.py"])
check("touching a file the order did not allow fails", v["status"] == "failed"
      and "did not allow" in v["reason"], v)

repo, start = base_repo()
w(repo, "skills/fix_gate.py", "# weakened\n"); commit(repo)
v = gate(repo, start, track="repair", allowed=["*"])
check("editing the gate fails even with allowed=['*']", v["status"] == "failed"
      and "gate machinery" in v["reason"], v)

repo, start = base_repo()
w(repo, "tests/test_ok.py", "import sys\nsys.exit(0)  # edited\n"); commit(repo)
v = gate(repo, start, track="repair", allowed=["*"])
check("editing an existing test fails", v["status"] == "failed" and "existing test" in v["reason"], v)

repo, start = base_repo()
os.remove(os.path.join(repo, "tests/test_ok.py")); commit(repo)
v = gate(repo, start, track="repair", allowed=["*"])
check("deleting an existing test fails", v["status"] == "failed", v)

repo, start = base_repo()
w(repo, "skills/a.py", "def a():\n    return 3\n")
v = gate(repo, start, track="repair", allowed=["skills/a.py"])
check("uncommitted work fails", v["status"] == "failed" and "uncommitted" in v["reason"], v)

repo, start = base_repo()
w(repo, "tests/test_new.py", "import sys\nsys.exit(1)\n"); commit(repo)
v = gate(repo, start, track="repair", allowed=["tests/test_new.py"])
check("a failing new test fails the gate", v["status"] == "failed" and v["reason"] == "tests failed", v)

repo, start = base_repo()
w(repo, "skills/a.py", "def a():\n    return 'syntax error'\n  oops\n"); commit(repo)
v = gate(repo, start, track="repair", allowed=["skills/a.py"])
check("a file that does not compile fails", v["status"] == "failed" and "compile" in v["reason"], v)

repo, start = base_repo()
w(repo, "skills/a.py", "# asked by Zephyrine at 555-555-0142\ndef a():\n    return 1\n"); commit(repo)
v = gate(repo, start, track="repair", allowed=["skills/a.py"])
check("adding a household name or number to a tracked file fails",
      v["status"] == "failed" and "household" in v["reason"], v)
check("the report does not print the name", "Zephyrine" not in json.dumps(v), v)

repo, start = base_repo()
w(repo, "skills/a.py", "def a():\n    return 9\n")
git(repo, "add", "-A"); git(repo, "commit", "-q", "--amend", "-m", "rewritten")
v = gate(repo, start, track="repair", allowed=["*"])
check("rewritten history fails", v["status"] == "failed" and "history" in v["reason"], v)

repo, start = base_repo()
w(repo, "skills/a.py", "def a():\n    return 2\n"); commit(repo)
w(repo, "skills/sneaky.py", "CHIP_TOOLS = []\n")          # never committed
v = gate(repo, start, track="repair", allowed=["skills/a.py"])
check("an untracked file in the tree fails the gate",
      v["status"] == "failed" and "untracked" in v["reason"], v)

print("build track")
repo, start = base_repo()
w(repo, "skills/coffee_prices.py", "CHIP_TOOLS = []\n")
w(repo, "tests/test_coffee_prices.py", "import sys\nsys.exit(0)\n"); commit(repo)
v = gate(repo, start, track="build", skill="coffee_prices")
check("a build that only adds its two files passes", v["status"] == "built, not verified", v)

repo, start = base_repo()
w(repo, "skills/coffee_prices.py", "CHIP_TOOLS = []\n")
w(repo, "skills/a.py", "def a():\n    return 5\n"); commit(repo)
v = gate(repo, start, track="build", skill="coffee_prices")
check("a build that edits an existing file fails", v["status"] == "failed"
      and "build track" in v["reason"], v)

repo, start = base_repo()
v = gate(repo, start, track="build", skill="coffee_prices")
check("a build that added nothing fails", v["status"] == "failed"
      and "added nothing" in v["reason"], v)

print("rollback")
repo, start = base_repo()
w(repo, "skills/a.py", "def a():\n    return 'bad'\n"); commit(repo, "bad 1")
w(repo, "skills/c.py", "y = 2\n"); commit(repo, "bad 2")
w(repo, "skills/a.py", "def a():\n    return 'uncommitted'\n")
what = self_heal.rollback(start, "FIX-9999", root=repo)
check("rollback stashes uncommitted work and reverts both commits",
      "stashed" in what and "2 commit(s) reverted" in what, what)
check("the tree matches the starting commit afterwards",
      git(repo, "diff", "--quiet", start, "HEAD").returncode == 0, git(repo, "diff", "--stat", start).stdout)
check("nothing was lost: the uncommitted work is in the stash",
      "FIX-9999" in git(repo, "stash", "list").stdout, git(repo, "stash", "list").stdout)
check("history was not rewritten (start is still an ancestor)",
      git(repo, "merge-base", "--is-ancestor", start, "HEAD").returncode == 0)

repo, start = base_repo()
w(repo, "skills/a.py", "def a():\n    return 7\n")
git(repo, "add", "-A"); git(repo, "commit", "-q", "--amend", "-m", "rewritten")
what = self_heal.rollback(start, "FIX-9998", root=repo)
check("rollback refuses to act on rewritten history", "needs a look" in what, what)

print("skill loader")
sk = tempfile.mkdtemp(prefix="skills_pkg_", dir=TMP)
pkg = os.path.join(sk, "sktest")
os.makedirs(pkg)
open(os.path.join(pkg, "__init__.py"), "w").write("")
open(os.path.join(pkg, "good.py"), "w").write(
    "CHIP_TOOLS = [{'name': 'good_tool', 'description': 'd', 'input_schema': {'type': 'object'}}]\n"
    "async def chip_tool(name, tool_input, *, sender, tier, session):\n    return 'ok'\n")
open(os.path.join(pkg, "broken.py"), "w").write("CHIP_TOOLS = [\nraise_here(\n")
open(os.path.join(pkg, "clash.py"), "w").write(
    "CHIP_TOOLS = [{'name': 'web_search', 'description': 'd', 'input_schema': {'type': 'object'}}]\n"
    "async def chip_tool(name, tool_input, *, sender, tier, session):\n    return 'x'\n")
open(os.path.join(pkg, "sideeffect.py"), "w").write(
    f"open({os.path.join(TMP, 'imported.flag')!r}, 'w').write('1')\n"
    "CHIP_TOOLS = [{'name': 'se_tool', 'description': 'd', 'input_schema': {'type': 'object'}}]\n"
    "async def chip_tool(name, tool_input, *, sender, tier, session):\n    return 'x'\n")
json.dump(["sideeffect"], open(os.environ["CHIP_SKILLS_DISABLED"], "w"))
sys.path.insert(0, sk)
defs, handlers = skill_loader.load({"web_search"}, skills_dir=pkg, package="sktest")
check("a good skill's tool is loaded", "good_tool" in handlers, list(handlers))
check("a broken skill is isolated, not fatal", "broken" in skill_loader.ERRORS, skill_loader.ERRORS)
check("a tool name that collides with a core tool is refused",
      "clash" in skill_loader.ERRORS and "web_search" not in handlers, skill_loader.ERRORS)
check("a disabled skill is never imported", not os.path.exists(os.path.join(TMP, "imported.flag")))
check("skill tools are admin-only by default",
      [d["name"] for d in skill_loader.visible_defs("user")] == [], skill_loader.visible_defs("user"))
skill_loader.candidates = lambda skills_dir=None: ["good", "broken", "clash", "sideeffect"]
r = skill_loader.set_enabled("good", False)
check("'skill: off' hides the tools at once",
      "off" in r and [d["name"] for d in skill_loader.visible_defs("admin")] == [], r)

shutil.rmtree(TMP, ignore_errors=True)
if FAILS:
    print("\nFAILED:\n  " + "\n  ".join(FAILS))
    sys.exit(1)
print("\nall checks passed")
