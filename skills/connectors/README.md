# Chip Connectors — Discord & Gmail

Scaffolded 19 Nov 2026. Signal stays the primary transport; these make it one
of several. Both connectors are credential-gated — the code is here and wired,
but each needs ~10 minutes of one-time setup by the admin that cannot be done from
the Pi.

## Architecture

The coordinator's `message_queue` is already transport-agnostic in practice: a
packet is `{sender, text, ts, [attachment]}` and delivery resolves the sender.
Connectors follow the same pattern as `listen()`:

- an asyncio task per transport, started in `main()` only when its credential
  exists in `.env`;
- inbound messages map a transport identity (Discord user ID, email address)
  to an existing Chip user (phone number) via the maps below — unknown
  identities are dropped, same as the Signal allowlist;
- a `channel` field on the packet routes the reply back out the same
  transport instead of Signal.

Gmail is different: it's not a chat transport, it's a **capability** — so it
lands as tools (`gmail_unread`, `gmail_send`) rather than a message loop.

## Discord setup checklist (admin, ~10 min)

1. https://discord.com/developers/applications → New Application → "Chip".
2. Bot tab → Add Bot → enable **Message Content Intent** → copy the token.
3. Add to `.env`: `DISCORD_BOT_TOKEN=...`
4. OAuth2 → URL Generator → scope `bot`, permission `Send Messages` →
   open the URL, add the bot to your server (or just DM it).
5. Get your Discord user ID (Settings → Advanced → Developer Mode →
   right-click your name → Copy User ID) and put it in `DISCORD_USER_MAP`
   in `discord_relay.py`.
6. `venv/bin/pip install discord.py` then restart chip.service.
   Startup log will show `🎮 Discord relay online.`

## Gmail setup checklist (admin, ~15 min)

1. https://console.cloud.google.com → new project "chip-gmail".
2. Enable the **Gmail API**.
3. OAuth consent screen → External → test users: your Gmail address.
4. Credentials → Create → OAuth client ID → Desktop app → download JSON →
   save as `config/gmail_credentials.json` on the Pi.
5. `venv/bin/pip install google-api-python-client google-auth-oauthlib`
6. Run `venv/bin/python3 skills/connectors/gmail_connector.py --auth` on the
   Pi; it prints a URL — open on your phone, approve, paste the code back.
   Token lands in `config/gmail_token.json`.
7. Tell Chip (in a Claude Code session): "wire the gmail tools" — the
   handlers in gmail_connector.py are ready; they just need registering in
   tool_calling.py once auth works.

## Security posture

- Discord: allowlist by user ID; everything else ignored. The bot token
  grants only what the bot can see — keep it to DMs or a private server.
- Gmail: `gmail_send` always drafts first and requires an explicit yes over
  the originating channel before sending. Read scope starts with
  `gmail.readonly`; widen only if drafting-in-Gmail is wanted later.
- Both tokens live in `.env`/`config/` with the existing keys — same
  plaintext-at-rest caveat as everything else until TPM LUKS happens.
