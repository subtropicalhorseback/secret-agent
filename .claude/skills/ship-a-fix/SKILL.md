---
name: ship-a-fix
description: The checklist every Chip heal (repair or build) follows and reports against. Use when you are running as a healer on this repo — a repair to existing behaviour or a new skill on the build track.
---

# Ship a fix — the healer's checklist

You are working on Chip, a live household assistant, without anyone watching.
A gate script (`skills/fix_gate.py`) runs after you and decides the status the
admin sees. It runs the gate **as it was before you started**, so editing it
does not help; neither does editing or deleting tests. Work so that the gate
passes honestly.

## Before you change anything

1. **Verify before you build** (CLAUDE.md Heal Rule 1). Check the order against
   the current code. If it is already done, say so with the commit that did it,
   make no change, and stop.
2. **One problem.** If the order is really several problems or a redesign, do
   the smallest piece that stands alone and say what is left.
3. **Scope.** The prompt lists the files you may touch.
   - Repair track: only those files.
   - Build track: only `skills/<name>.py` and `tests/test_<name>.py`, both
     new. Read `skills/skill_loader.py` for the contract (`CHIP_TOOLS` and
     `async chip_tool`).
   - Never: `skills/fix_gate.py`, `skills/self_heal.py`, `skills/fix_index.py`,
     `skills/model_check.py`, `skills/safe_restart.py`, `tests/live_turns.py`,
     this file, or any existing test.
4. Register your phases (`skills/fix_index.py --phase`). That is the progress
   Chip reads.

## While you work

5. Timestamped backup before editing any existing file, into
   `backups/<source>/` (e.g. `backups/remind/remind.py.bak.<YYYYmmdd_HHMMSS>`).
   Leave no stray or untracked files in the tree — the gate fails on them.
6. Small, anchored edits. `venv/bin/python3 -m py_compile` every changed file.
7. Add a test for what you changed, as a NEW `tests/test_<topic>.py`. It
   must be standalone (no pytest), stub Signal and the network, and exit
   non-zero on failure (see existing tests). It must also keep its records
   out of real data: set `CHIP_WORKLOG_DIR`, `CHIP_WEBLOG_DIR`, `CHIP_WEB_DB`
   and `CHIP_WORK_DB` to temp paths before importing the coordinator.
8. No household names, phone numbers or places in any tracked file, comments
   and tests included. They live in the gitignored `config/` files.
9. Never print `.env`, keys or tokens.

## Before you finish

10. Run the whole suite: `for t in tests/test_*.py; do venv/bin/python3 $t; done`.
    Every file must exit 0.
11. Repair track: restart with `venv/bin/python3 skills/safe_restart.py` and
    exercise the real path you changed (send a real request through
    `venv/bin/python3 tests/live_turns.py "<message>"`, load a real page, read
    the log). Build track: don't restart; the gate does.
12. Update `chip_current_state.md` in this workspace's Claude Code memory directory
    (`~/.claude/projects/<workspace path, each / as ->/memory/`)
    (Heal Rule 2): what changed, and what you verified live vs only in tests.
13. Commit locally with a plain-English message. Never push, never touch
    remotes, never rewrite history, leave no uncommitted changes.
14. End with the RESULT line from the house rules. Say plainly what you did
    not verify.
