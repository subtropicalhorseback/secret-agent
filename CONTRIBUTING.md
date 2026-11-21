# Working on Chip

Most changes here are made by Claude Code, launched from Signal with `fix:` and
approved by a human. This file is the part of that process that belongs in the
repo. `CLAUDE.md` carries the same rules plus the machine-specific context, but
it is gitignored, so this is the copy that survives a clone.

## Heal rules

**1. Verify a deliverable against the current code before building it.**

Work orders are written from memory, and dev sessions move faster than anything
written down. Three orders in two days specified work that had already shipped.
Before writing a line, read the code the order is about. If it is already
implemented, say so — name the commit that shipped it — and move on. Rebuilding
something that works is not a neutral act: it costs a review, it risks a
regression, and it buries the one deliverable that was real.

**2. Every heal ends by updating the state note, before its closing commit.**

The note lives outside the repo, at
`~/.claude/projects/<workspace path, with each / as ->/memory/chip_current_state.md`. It is
the authoritative description of what Chip can do and what is outstanding, and
it outranks every dated `Chip_*.md` session report where they disagree.

Updating it is not optional and not a courtesy to the next session — it is the
mechanism that stops rule 1 from being needed. A heal that fixes two bugs and
leaves the note stale has created the next wasted order. Record what changed,
and delete any open item that is now closed.

**3. Nothing is absent merely because git does not track it.**

`agents/`, `config/`, `data/` and `CLAUDE.md` are gitignored runtime and
household content. `agents/chip/SOUL.md` was reported missing for weeks and
blamed for six consecutive nightly backup failures; it had never moved, and had
never been in git. Search the filesystem, not `git ls-files`, and search the
whole tree rather than the workspace root — a root-only check is what produced
that false report in the first place.

## House conventions

- Take a timestamped backup (`cp f f.bak_$(date +%Y%m%d_%H%M%S)`) before
  editing a file in place, and `python -m py_compile` everything you change.
- Household data — names, numbers, places, domain vocabulary — lives in the
  gitignored files under `config/` and is read through `chip_config`. That
  applies to tests too: pull the value from config at run time rather than
  typing it into a test fixture.
- Commits here are local only. The public mirror is produced separately by a
  scanned export, on the admin's say-so. Never push from the Pi.
- Anything touching `coordinator.py` or `skills/` ends with
  `sudo systemctl restart chip.service` and a check that it came back active.

## Tests

Standalone scripts under `tests/`, no pytest:

    venv/bin/python3 tests/test_signal_file_send.py
    venv/bin/python3 tests/test_pdf_batch_caption.py

Each stubs the signal-cli daemon and the network, drives the real coordinator
function, and exits non-zero on failure. They are fast enough to run before
every restart; do that.
