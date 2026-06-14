from datetime import timedelta

import discord
from discord.ext import commands

from amadeus.alerts import send_alert
from amadeus.constants import OWNER_ID
from amadeus.database import ConfigStore
from amadeus.discord_utils import escape_untrusted_text
from amadeus.honeypot_config import HoneypotConfigStore
from amadeus.logging_utils import log
from amadeus.moderation import action_display, execute_action


# ============================================================
# Honeypot cog
# ============================================================

DELETE_HISTORY_LABELS = {
    3600: "1 hour",
    21600: "6 hours",
    43200: "12 hours",
    86400: "24 hours",
}


def honeypot_action_exemption_reason(
    member: discord.Member,
    action: str | None,
    admin_role_id: int | None,
    *,
    owner_id: int | None,
) -> str | None:
    if admin_role_id is not None and any(role.id == admin_role_id for role in member.roles):
        return "Skipped — member has the configured Amadeus admin role."

    if action == "ban" and owner_id is not None and member.id == owner_id:
        return "Skipped — configured Amadeus owner cannot be banned."

    return None


def honeypot_action_succeeded(action: str | None, result: str) -> bool:
    if action == "remove_role":
        return result.startswith("Removed role ")
    if action == "mute":
        return result == "Timed out for 28 days."
    if action == "kick":
        return result == "Kicked."
    if action == "ban":
        return result == "Banned."
    return False


def delete_history_label(seconds: int | None) -> str:
    return DELETE_HISTORY_LABELS.get(seconds, f"{seconds} seconds")


async def delete_member_recent_messages(
    guild: discord.Guild,
    member: discord.Member,
    delete_history_seconds: int,
) -> str:
    cutoff = discord.utils.utcnow() - timedelta(seconds=delete_history_seconds)
    deleted = 0
    failed_channels = 0

    def is_member_message(message: discord.Message) -> bool:
        return message.author.id == member.id

    for channel in guild.text_channels:
        try:
            deleted_messages = await channel.purge(
                limit=None,
                after=cutoff,
                check=is_member_message,
                reason="Honeypot triggered message cleanup",
            )
            deleted += len(deleted_messages)
        except (discord.Forbidden, discord.HTTPException) as e:
            failed_channels += 1
            log(
                f"HONEYPOT // HISTORY DELETE FAILED 『 CHANNEL {channel.id} 』 USER 『 {member.id} 』 // {e}",
                level="debug",
                logger_name="honeypot",
            )

    window = delete_history_label(delete_history_seconds)
    if failed_channels:
        return f"Deleted {deleted} message(s) from the last {window}; failed in {failed_channels} channel(s)."
    return f"Deleted {deleted} message(s) from the last {window}."


class Honeypot(commands.Cog):
    """
    Honeypot channel monitor.

    Deletes any message posted in the configured honeypot channel and
    takes the configured action (remove role / mute / kick / ban) against
    the sender. Optionally alerts the configured admin channel.
    """

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.honeypot_store = HoneypotConfigStore()
        self.module_store = ConfigStore()

    def cog_unload(self):
        self.honeypot_store.close()
        self.module_store.close()

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.guild is None:
            return

        # Allow the bot's own messages (e.g. the warning embed); block everyone else including other bots.
        if message.author.id == self.bot.user.id:
            return

        guild_id = message.guild.id

        if not self.module_store.is_module_enabled(guild_id, "honeypot"):
            log(f"HONEYPOT // ON_MESSAGE // MODULE DISABLED 『 GUILD {guild_id} 』", level="debug", logger_name="honeypot")
            return

        config = self.honeypot_store.get_config(guild_id)

        if config is None or config.channel_id is None:
            log(f"HONEYPOT // ON_MESSAGE // NO CHANNEL CONFIGURED 『 GUILD {guild_id} 』", level="debug", logger_name="honeypot")
            return

        delete_history_seconds = getattr(config, "delete_history_seconds", None)

        if message.channel.id != config.channel_id:
            return

        log(
            f"HONEYPOT // TRIGGERED 『 USER {message.author.id} 』 GUILD 『 {guild_id} 』",
            level="debug",
            logger_name="honeypot",
        )

        # Delete the message first.
        try:
            await message.delete()
        except discord.Forbidden:
            log(
                f"HONEYPOT // DELETE FORBIDDEN 『 GUILD {guild_id} 』",
                level="debug",
                logger_name="honeypot",
            )
            await send_alert(
                self.bot,
                self.module_store,
                guild_id,
                f"⚠ **Honeypot:** Missing **Manage Messages** permission in <#{config.channel_id}>. "
                "Could not delete a message from "
                f"{message.author.mention} (`{message.author}` | `{message.author.id}`). "
                "Grant the permission or re-run `/honeypot set-channel`.",
            )
        except discord.HTTPException as e:
            log(
                f"HONEYPOT // DELETE FAILED 『 {e} 』 GUILD 『 {guild_id} 』",
                level="debug",
                logger_name="honeypot",
            )

        member = message.author

        if not isinstance(member, discord.Member):
            return

        admin_role_id = None
        try:
            guild_config = self.module_store.get_guild_config(guild_id)
            admin_role_id = guild_config.admin_role_id
        except RuntimeError:
            log(
                f"HONEYPOT // GUILD CONFIG MISSING 『 GUILD {guild_id} 』",
                level="debug",
                logger_name="honeypot",
            )

        result = honeypot_action_exemption_reason(
            member,
            config.action,
            admin_role_id,
            owner_id=OWNER_ID,
        )
        if result is None:
            result = await execute_action(
                message.guild,
                member,
                config.action,
                config.action_role_id,
                config.action_reason,
                delete_history_seconds,
            )
            if (
                delete_history_seconds is not None
                and config.action != "ban"
                and honeypot_action_succeeded(config.action, result)
            ):
                cleanup_result = await delete_member_recent_messages(
                    message.guild,
                    member,
                    delete_history_seconds,
                )
                result = f"{result} {cleanup_result}"

        if config.alerts_enabled:
            content_preview = (
                escape_untrusted_text(message.content, max_length=200)
                if message.content
                else "*[no text content]*"
            )
            await send_alert(
                self.bot,
                self.module_store,
                guild_id,
                (
                    f"🍯 **Honeypot triggered** in <#{config.channel_id}>\n"
                    f"**Member:** {member.mention} (`{member}` | `{member.id}`)\n"
                    f"**Action:** {action_display(config.action, config.action_role_id, message.guild)}\n"
                    f"**Result:** {result}\n"
                    f"**Message:** {content_preview}"
                ),
            )


async def setup(bot: commands.Bot):
    await bot.add_cog(Honeypot(bot))
