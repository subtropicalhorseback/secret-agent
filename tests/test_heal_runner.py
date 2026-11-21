"""The heal runner end to end, with a stand-in for Claude Code (One Voice spec D.4/D.5).

Standalone, no pytest. Each case builds a throwaway git repo carrying a copy
of the real skills/fix_gate.py, points the runner at it, and replaces the
Claude Code binary with a script that edits and commits like a healer would
and prints the same stream-json events. No real healer, no real restart, no
real repository, no message sent. Exits non-zero on failure.
"""
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
TMP = tempfile.mkdtemp(prefix="heal_runner_test_")
os.environ.setdefault("CHIP_WORKLOG_DIR", os.path.join(TMP, "worklog"))
os.environ["CHIP_SKILLS_DISABLED"] = os.path.join(TMP, "skills_disabled.json")

from skills import fix_index, outbox  # noqa: E402
import skills.self_heal as sh  # noqa: E402

FAILS = []
EVENTS = []
NOTED = []


def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")
    if not cond:
        FAILS.append(f"{name}: {str(detail)[:500]}")


def git(repo, *a):
    return subprocess.run(["git", "-C", repo, *a], capture_output=True, text=True)


def make_repo():
    repo = tempfile.mkdtemp(prefix="repo_", dir=TMP)
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "t@example.invalid")
    git(repo, "config", "user.name", "test")
    os.makedirs(os.path.join(repo, "skills")); os.makedirs(os.path.join(repo, "tests"))
    open(os.path.join(repo, "skills", "__init__.py"), "w").write("")
    open(os.path.join(repo, "skills", "a.py"), "w").write("def a():\n    return 1\n")
    shutil.copy(os.path.join(ROOT, "skills", "fix_gate.py"), os.path.join(repo, "skills"))
    open(os.path.join(repo, "tests", "test_ok.py"), "w").write("import sys\nsys.exit(0)\n")
    open(os.path.join(repo, ".gitignore"), "w").write("__pycache__/\n*.pyc\n*.bak*\n")
    git(repo, "add", "-A"); git(repo, "commit", "-qm", "start")
    return repo


def stand_in(repo, behaviour):
    """A fake `claude` that acts out `behaviour` (a bash snippet per call
    number) and prints stream-json like the real one."""
    p = os.path.join(TMP, f"claude_{os.path.basename(repo)}")
    calls = os.path.join(TMP, f"calls_{os.path.basename(repo)}")
    cases = "\n".join(f"  {n}) {snippet} ;;" for n, snippet in behaviour.items())
    open(p, "w").write(f"""#!/bin/bash
printf '%s ' "$@" | tr '\n' ' ' >> {calls}; echo >> {calls}
N=$(( $(cat {calls}.n 2>/dev/null || echo 0) + 1 )); echo $N > {calls}.n
SID=""
for ((i=1; i<=$#; i++)); do
  if [ "${{!i}}" = "--session-id" ] || [ "${{!i}}" = "--resume" ]; then j=$((i+1)); SID="${{!j}}"; fi
done
cd {repo}
case $N in
{cases}
esac
echo '{{"type":"assistant","message":{{"content":[{{"type":"text","text":"working on it"}}]}}}}'
echo '{{"type":"result","subtype":"success","num_turns":3,"total_cost_usd":0.25,"session_id":"'$SID'","result":"Done.\\nRESULT: a now returns 2 | STATUS: verified live | COMMIT: none"}}'
""")
    os.chmod(p, os.stat(p).st_mode | stat.S_IEXEC)
    return p, calls


def setup(repo, claude):
    fix_index.DB_PATH = os.path.join(repo + ".fixes.db")
    sh.fix_index = fix_index
    sh.WORKSPACE = repo
    sh.HEAL_LOG_DIR = os.path.join(repo + ".patches")
    os.makedirs(sh.HEAL_LOG_DIR, exist_ok=True)
    sh.CLAUDE_BIN = claude
    sh.GATE_FLAGS = ["--no-restart", "--no-live"]
    sh.RESTART_AFTER_ROLLBACK = False
    sh.SNAPSHOT_FILES = []
    sh._note_state = lambda line: NOTED.append(line)
    sh._launch_unit = lambda: (False, "test: no chaining")
    outbox.notify = lambda kind, text, urgent=False, **kw: EVENTS.append((kind, text, kw)) or "queued"
    EVENTS.clear(); NOTED.clear()


def stage_approve(desc, **kw):
    info, _msg = sh.stage(desc, staged_by="tester", **kw)
    fix_index.approve(info["fix_id"])
    return info["fix_id"]


print("case: a good repair")
repo = make_repo()
claude, calls = stand_in(repo, {1: "sed -i 's/return 1/return 2/' skills/a.py && git commit -qam 'a returns 2'"})
setup(repo, claude)
fid = stage_approve("Make a() return 2.", files=["skills/a.py"], acceptance="a() returns 2")
sh.run_job()
rec = fix_index.get(fid)
check("status comes from the gate: 'built, not verified' (no acceptance command)",
      rec["gate_status"] == "built, not verified" and rec["status"] == "done", rec)
check("the healer's own 'verified live' claim was NOT used",
      EVENTS and "Status: built, not verified" in EVENTS[-1][1], EVENTS)
check("one result event, short, with the commit", len([e for e in EVENTS if e[0] == "heal_result"]) == 1
      and "Commit: " in EVENTS[-1][1] and "\n" not in EVENTS[-1][1], EVENTS)
check("the healer's commit is there", "a returns 2" in git(repo, "log", "--oneline").stdout,
      git(repo, "log", "--oneline").stdout)
check("cost and session are recorded", rec["cost_usd"] == 0.25 and rec["session_id"], rec)
check("a new session was named", "--session-id " + rec["session_id"] in open(calls).read(),
      open(calls).read()[:300])
log = open(os.path.join(sh.HEAL_LOG_DIR, rec["log_path"])).read()
check("the heal log is readable text with the gate's findings",
      "working on it" in log and "GATE: built, not verified" in log and "✓ tests" in log, log[-600:])

print("case: a repair that edits an existing test is rolled back")
repo = make_repo()
start = git(repo, "rev-parse", "HEAD").stdout.strip()
claude, calls = stand_in(repo, {1: "echo '# loosened' >> tests/test_ok.py && "
                                   "sed -i 's/return 1/return 2/' skills/a.py && git commit -qam 'cheat'"})
setup(repo, claude)
fid = stage_approve("Make a() return 2.", files=["*"], acceptance="a() returns 2")
sh.run_job()
rec = fix_index.get(fid)
check("the gate failed it", rec["status"] == "failed" and rec["gate_status"] == "failed", rec)
check("the change was reverted (tree equals the start)",
      git(repo, "diff", "--quiet", start, "HEAD").returncode == 0, git(repo, "log", "--oneline").stdout)
check("history kept (revert commit, not a reset)",
      len(git(repo, "log", "--oneline").stdout.splitlines()) == 3, git(repo, "log", "--oneline").stdout)
check("the admin's line says it failed and was rolled back",
      EVENTS and "failed" in EVENTS[-1][1] and "reverted" in EVENTS[-1][1], EVENTS)
check("the state note got a correction", NOTED and fid in NOTED[0], NOTED)

print("case: a build that strays, then fixes itself in the same session")
repo = make_repo()
claude, calls = stand_in(repo, {
    1: "printf 'CHIP_TOOLS = []\\n' > skills/coffee.py && printf 'import sys\\nsys.exit(0)\\n' > tests/test_coffee.py"
       " && sed -i 's/return 1/return 3/' skills/a.py && git add -A && git commit -qm 'coffee + stray edit'",
    2: "git checkout -q HEAD~1 -- skills/a.py && git commit -qam 'undo stray edit'"})
setup(repo, claude)
fid = stage_approve("A skill that reports coffee prices.", track="build", skill_name="coffee",
                    acceptance="the tool answers")
sh.run_job()
rec = fix_index.get(fid)
log_calls = open(calls).read().splitlines()
check("the builder got a second go after the gate failed", rec["iterations"] == 2, rec)
check("the second go resumed the same session",
      len(log_calls) == 2 and f"--resume {rec['session_id']}" in log_calls[1], log_calls)
check("the second go was told why the gate failed", "gate failed" in log_calls[1].lower(), log_calls[1][:300])
check("after the fix it passes", rec["gate_status"] == "built, not verified", rec)

print("case: continuing a fix resumes its session")
prev_sid = rec["session_id"]
claude, calls = stand_in(repo, {3: "true"})
sh.CLAUDE_BIN = claude
fid2 = stage_approve("Also list espresso.", continue_of=fid)
sh.run_job()
last = open(calls).read().splitlines()[-1]
check("the follow-up resumed the earlier session", f"--resume {prev_sid}" in last, last[:200])

print("case: no starting commit, no heal")
repo = make_repo()
claude, calls = stand_in(repo, {1: "echo should-not-run > /dev/null"})
setup(repo, claude)
fid3 = stage_approve("Make a() return 2.", files=["skills/a.py"])
real_head = sh._git_head
sh._git_head = lambda full=False: None
try:
    sh.run_job()
finally:
    sh._git_head = real_head
rec3 = fix_index.get(fid3)
check("without a starting commit the healer is not run",
      not os.path.exists(calls) and rec3["status"] == "failed", (rec3, os.path.exists(calls)))
check("and the admin is told why", EVENTS and "starting commit" in EVENTS[-1][1], EVENTS)

print("case: the daily spend cap")
fix_index.set_fields(fid, cost_usd=50.0)
info, _ = sh.stage("One more.", staged_by="tester", files=["skills/a.py"])
msg = sh.approve(info["fix_id"])
check("past the cap, approval asks for an override", "override" in msg and "cap" in msg, msg)
check("the record stays staged", fix_index.get(info["fix_id"])["status"] == "staged")

print("case: staging refuses what the gate could not judge")
try:
    sh.stage("x" * 4000, staged_by="tester", files=["skills/a.py"])
    check("an oversized repair is refused", False)
except ValueError as e:
    check("an oversized repair is refused", "one problem" in str(e), str(e))
try:
    sh.stage("A new thing.", staged_by="tester", track="build", skill_name="a")
    check("a 'build' over an existing file is refused", False)
except ValueError as e:
    check("a 'build' over an existing file is refused", "already exists" in str(e), str(e))

shutil.rmtree(TMP, ignore_errors=True)
if FAILS:
    print("\nFAILED:\n  " + "\n  ".join(FAILS))
    sys.exit(1)
print("\nall checks passed")
