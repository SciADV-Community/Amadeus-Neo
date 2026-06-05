import asyncio
import os

import discord
from discord.ext import commands

from amadeus.ascii import build_art_block, build_divider
from amadeus.cogs import load_extensions, sync_application_commands
from amadeus.discord_utils import DEFAULT_ALLOWED_MENTIONS
from amadeus.logging_utils import log, setup_logging


setup_logging()

TOKEN = os.environ["DISCORD_TOKEN"]


class MyBot(commands.Bot):
    async def setup_hook(self):
        # Keep this as print so the ASCII art is not timestamped/log-formatted.
        print(build_art_block())
        print("\n")

        await load_extensions(self)
        await sync_application_commands(self)


intents = discord.Intents.default()

# Required for /bouncer verify-all and guild join events.
# Also enable "Server Members Intent" in the Discord Developer Portal.
intents.members = True

# Required for on_message handlers (honeypot, bouncer, activity).
# Also enable "Message Content Intent" in the Discord Developer Portal.
intents.message_content = True

bot = MyBot(
    command_prefix=commands.when_mentioned,
    intents=intents,
    allowed_mentions=DEFAULT_ALLOWED_MENTIONS,
)


@bot.event
async def on_ready():
    print(build_divider())

    log(f"SYSTEM THREAD {bot.user}".upper(), logger_name="core")
    log("SYSTEM CONNECTIONS »", logger_name="core")
    async for guild in bot.fetch_guilds(limit=150):
        log(f"  « {guild} »", logger_name="core")

    log(f"AMADEUS // SYSTEM {bot.status}".upper(), logger_name="core")


async def main():
    async with bot:
        await bot.start(TOKEN)


asyncio.run(main())
