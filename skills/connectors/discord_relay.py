"""
skills/connectors/discord_relay.py — Discord as a second Chip transport
========================================================================
Scaffolded 19 Nov 2026; dormant until DISCORD_BOT_TOKEN exists in .env and
discord.py is installed (see connectors/README.md for the 10-minute setup).

Design: a DM from an allowlisted Discord user ID is translated into a normal
coordinator packet — the same worker loop, RAG, tools, and permissions apply,
because the Discord identity maps onto an existing Chip user. Replies route
back out through Discord via the packet's `channel` field.
"""
import os
import asyncio

# Discord user ID (string) -> Chip user phone. Fill in after setup step 5.
DISCORD_USER_MAP = {
    # "123456789012345678": "+15550100001",   # admin example — see config/users.json
}

_client = None          # discord.Client, set by start()
_dm_channels = {}       # chip phone -> discord channel, for replies
PREFER = {}             # chip phone -> "discord"|"signal": last inbound transport wins


def available():
    if not os.getenv("DISCORD_BOT_TOKEN"):
        return False
    try:
        import discord  # noqa: F401
        return True
    except ImportError:
        return False


async def deliver(phone, text):
    """Send a reply back over Discord to the mapped user, if we have an open
    DM channel from them this session."""
    ch = _dm_channels.get(phone)
    if ch is None:
        return False
    for i in range(0, len(text), 1900):          # Discord caps at 2000 chars
        await ch.send(text[i:i + 1900])
    return True


async def start(message_queue):
    """Coordinator main() calls this as a task when available() is True."""
    import discord

    intents = discord.Intents.default()
    intents.message_content = True
    client = discord.Client(intents=intents)
    global _client
    _client = client

    @client.event
    async def on_ready():
        print(f"🎮 Discord relay online as {client.user}.")

    @client.event
    async def on_message(message):
        if message.author == client.user:
            return
        phone = DISCORD_USER_MAP.get(str(message.author.id))
        if not phone:
            return                                 # not allowlisted → drop
        _dm_channels[phone] = message.channel
        PREFER[phone] = "discord"
        await message_queue.put({
            "sender": phone,
            "text": message.content,
            "ts": str(int(message.created_at.timestamp() * 1000)),
            "channel": "discord",
        })

    await client.start(os.environ["DISCORD_BOT_TOKEN"])
