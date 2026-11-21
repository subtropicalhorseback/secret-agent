# secret-agent (Chip)

A self-hosted household AI assistant that lives on a Raspberry Pi 5 and talks
to your family over **Signal**. Premier cloud models do the thinking; the Pi
owns the memory, the files, the browser, and the keys.

Built and maintained with [Claude Code](https://claude.com/claude-code) — to
the point that Chip can now stage its own repairs through it (`fix:`).

## What it does

- **Converses** per-user over Signal with personas, tiered permissions, sticky
  model switching (`o:` Opus, `f:` Fable, `g:` Gemini, `m:` local), and
  plain-language thinking-effort control ("think hard: …" → max for 5 turns).
- **Remembers**: per-user fact store with supersession + audit history, a
  persistent conversation store with a rolling summary (no context loss
  overnight), a 10k-chunk RAG vault (SQLite FTS5 + full-corpus cosine over a
  cached numpy matrix, fused by RRF), and nightly deterministic memory
  maintenance.
- **Answers once.** One message in, one reply out. Delegated work — browser
  runs, research, analysis — runs as workers recorded in an on-disk ledger
  and reports back to the coordinator, which writes the single reply (and can
  send a worker back out, code-capped). No progress pings; a send gate checks
  every outbound message against its kind and the one-reply rule.
- **Acts** through ~35 native tools on both Claude and Gemini: web search,
  PDF/DOCX production → sent as Signal attachments and vault-indexed, user
  messaging, reminders, scheduled self-follow-ups that re-enter the agent
  loop *with tools*, per-model cost reporting, and a live browser.
- **Browses**: its own Chromium (Playwright-driven, own systemd service,
  hardened) with a DOM-first agent loop and screenshot/cursor fallback. Up
  to three workers at once, one tab each. A gateway owns the network route
  (VPN tabs shared, the home route exclusive so cookies and IPs never mix),
  logs every search, fetch and step, and caches pages and observed facts with
  their age. A worker can ask a vision model about the screen; human checks
  (CAPTCHAs, logins) come to the user as a picture over Signal and their reply
  is routed back. Hard rules: no credentials, no purchases, page text is
  untrusted.
- **Pipelines**: multi-round cited research, structured analysis,
  and dual-model crosscheck — with a frontier model as the adjudicator of
  every arbitration step.
- **Changes itself, carefully**: `fix: <problem>` stages a one-problem repair
  limited to named files; `build: <name>: <what>` stages a new skill that may
  only add files (skills declare their own tools). Nothing runs without an
  explicit `fix: approve`. Each run is a Claude Code session that can be
  resumed, with a per-run dollar cap. Afterwards a gate script — run as it was
  at the starting commit — checks scope, protected files, every test,
  household identifiers, a restart and live behaviour, and decides the status:
  failed repairs are reverted, failed builds switched off. The result is one
  line.
- **Keeps itself honest**: VPN guard (fail-closed web access when Mullvad
  drops, self-heal), signal watchdog, thermal guard, usage logging on every
  model call, weekly cost reports. Background scripts report through an
  outbox: urgent events become one message, routine ones a nightly digest.
  A startup check looks up every model name on its own client.

## Hardware & stack

Raspberry Pi 5 (16GB) · Debian (16K-page kernel — the browser uses the
system Chromium for exactly this reason) · signal-cli daemon over a Unix
socket · Ollama (embeddings + optional local model) · Tailscale (+ Mullvad
exit nodes) · SQLite everywhere · Claude Opus/Fable + Gemini Flash.

## Setup (abridged)

1. `cp .env.example .env` and fill in keys; `cp config/users.example.json
   config/users.json` and describe your household (phones, tiers, personas).
2. Install: python venv with `requirements` of your choosing (`anthropic`,
   `aiohttp`, `requests`, `playwright`, `fpdf2`, `python-docx`, `pdfplumber`,
   `numpy`, `psutil`, `ddgs`, `bs4`), plus a linked `signal-cli` daemon and
   Ollama with `nomic-embed-text`.
3. systemd units: `chip.service` (coordinator), `chip-browser.service`
   (browser; see skills/browser_service.py header), plus cron entries for
   `vpn_guard.py`, `signal_watchdog.py` and `dispatcher.py`. The scoped
   home-route templates in `deploy/homeroute/` use `/opt/chip` and a `chip`
   user as placeholders.
4. Talk to it on Signal. `help` lists the command surface; most things need
   no command at all.

Identity, personas, locations and phone numbers all live in gitignored config
(`.env`, `config/users.json`) — the code in this repo is household-agnostic.

## Scheduled jobs

Every recurring system job is declared in `config/jobs.yaml` and run by
`dispatcher.py`, which the crontab invokes once a minute. Per-job state (last
start/finish, duration, outcome, consecutive failures and a capped tail of the
run's output) lands in `data/job_state.json`; `list_scheduled_jobs` reads it.

A job that fails `max_failures` times in a row sends the admin one Signal alert
and then stays quiet until it succeeds again. Timeouts are hard-killed and
recorded as `timeout`, distinct from a job that exited non-zero (`failed`) and
from one skipped because the previous run was still going (`overrun`).

`signal_watchdog` and `vpn_guard` deliberately stay as their own crontab lines
rather than moving under the dispatcher: they are what notices and recovers if
the dispatcher itself breaks, so they must not depend on it.

### Rolling back the job dispatcher

The seven migrated jobs' original crontab lines are kept, commented out, at the
bottom of `crontab -l`. To roll back: `crontab -e`, comment out the single
`dispatcher.py` line, uncomment the seven below it. `jobs.yaml` and
`data/job_state.json` can stay where they are. Do not run both at once — every
job would fire twice.

## Tests

`tests/test_*.py` are standalone scripts (no pytest): they stub Signal and the
network and exit non-zero on failure. `tests/live_turns.py` is the live check:
real models, captured Signal transport, one reply per message expected.

## Security notes

- Three-number Signal allowlist at the listener; everything else dropped.
- Tiered commands (admin vs user), child-safe rewriting for flagged users.
- Browser runs isolated (systemd `InaccessiblePaths` over secrets + DBs),
  refuses downloads, fails closed when the VPN is down.
- Worker output is fenced as untrusted data; the turn that reads it gets a
  fixed read-only tool list enforced in code.
- Self-repair is gated on an explicit human approval per run, and its result
  is judged by code the healer cannot edit.

No warranty; this is one household's living system, shared for ideas.
