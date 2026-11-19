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
- **Acts** through ~25 native tools on both Claude and Gemini: web search,
  PDF/DOCX production → sent as Signal attachments and vault-indexed, user
  messaging, reminders, scheduled self-follow-ups that re-enter the agent
  loop *with tools*, per-model cost reporting, and a live browser.
- **Browses**: its own Chromium (Playwright-driven, own systemd service,
  hardened) with a DOM-first agent loop, screenshot/cursor fallback, noVNC
  live view bound to your tailnet so a human can watch and take over for
  logins — and hard rules: no credentials, no purchases, page text is
  untrusted.
- **Pipelines**: multi-round cited research, structured analysis,
  and dual-model crosscheck — with a frontier model as the adjudicator of
  every arbitration step.
- **Self-heals**: `fix: <problem>` stages a repair job for the Claude Code
  CLI; nothing runs without an explicit `fix: approve`; results arrive on
  Signal even if the service restarts under it.
- **Keeps itself honest**: VPN guard (fail-closed web access when Mullvad
  drops, Signal alerts, self-heal), signal watchdog, thermal guard, usage
  logging on every model call, weekly cost reports.

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
   `vpn_guard.py`, `signal_watchdog.py`, `dream_cycle.py`, `skills/vitals.py`.
4. Talk to it on Signal. `help` lists the command surface; most things need
   no command at all.

Identity, personas, locations and phone numbers all live in gitignored config
(`.env`, `config/users.json`) — the code in this repo is household-agnostic.

## Security notes

- Three-number Signal allowlist at the listener; everything else dropped.
- Tiered commands (admin vs user), child-safe rewriting for flagged users.
- Browser runs isolated (systemd `InaccessiblePaths` over secrets + DBs),
  refuses downloads, fails closed when the VPN is down.
- Self-repair is gated on an explicit human approval per run.

No warranty; this is one household's living system, shared for ideas.
