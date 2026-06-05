import discord
from discord.ext import commands

from amadeus.database import ConfigStore
from amadeus.discord_utils import NO_MENTIONS
from amadeus.logging_utils import log


async def send_alert(
    bot: commands.Bot,
    store: ConfigStore,
    guild_id: int,
    content: str,
) -> bool:
    """
    Sends an alert message to the configured admin alert channel for the guild.

    Returns True if the message was sent successfully, False if no channel is
    configured or the send fails.
    """
    try:
        config = store.get_guild_config(guild_id)
    except RuntimeError:
        return False

    if config.alert_channel_id is None:
        log(
            f"ALERTS // NO CHANNEL CONFIGURED 『 GUILD {guild_id} 』",
            level="debug",
            logger_name="alerts",
        )
        return False

    channel = bot.get_channel(config.alert_channel_id)

    if not isinstance(channel, discord.TextChannel):
        log(
            f"ALERTS // CHANNEL NOT FOUND OR WRONG TYPE 『 CHANNEL {config.alert_channel_id} 』 GUILD 『 {guild_id} 』",
            level="debug",
            logger_name="alerts",
        )
        return False

    try:
        await channel.send(content, allowed_mentions=NO_MENTIONS)
        return True
    except (discord.Forbidden, discord.HTTPException) as e:
        log(
            f"ALERTS // SEND FAILED 『 CHANNEL {config.alert_channel_id} 』 GUILD 『 {guild_id} 』 // {e}",
            level="debug",
            logger_name="alerts",
        )
        return False
