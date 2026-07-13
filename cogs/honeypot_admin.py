import re

import discord
from discord import app_commands
from discord.ext import commands

from amadeus.database import ConfigStore
from amadeus.discord_utils import NO_MENTIONS, SLOWMODE_HONEYPOT
from amadeus.honeypot_config import HoneypotConfigStore
from amadeus.logging_utils import log
from amadeus.models.honeypot import HoneypotConfig
from amadeus.module_guard import require_module_enabled_for_interaction
from amadeus.permissions import require_amadeus_access


def honeypot_action_label(action: str, role: discord.Role | None = None) -> str:
    """Returns a display label for configured honeypot actions."""
    if action == "remove_role":
        if role is None:
            raise ValueError("remove_role requires a role")
        return f"Remove role ({role.mention})"

    labels = {
        "mute": "Mute (28-day timeout)",
        "kick": "Kick",
        "ban": "Ban",
    }

    return labels[action]


def honeypot_alerts_hint(enabled: bool, alert_channel_id: int | None) -> str:
    if enabled and alert_channel_id is None:
        return "\n\nMake sure `/amadeus set-admin-channel` is configured so alerts have somewhere to go."
    return ""


DELETE_HISTORY_LABELS = {
    3600: "1 hour",
    21600: "6 hours",
    43200: "12 hours",
    86400: "24 hours",
}

HONEYPOT_POST_MESSAGE_MAX_LENGTH = 2000
DEFAULT_HONEYPOT_POST_MESSAGE = (
    "**Restricted Channel**\n\n"
    "This channel is monitored.\n\n"
    "Posting here will result in immediate moderation action against your account."
)
URL_RE = re.compile(
    r"(?i)(?:https?://|www\.|discord(?:\.gg|app\.com/invite)/)\S+"
    r"|\b[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.[a-z]{2,}(?:[/?#]\S*)?"
)


def delete_history_label(seconds: int | None) -> str:
    return DELETE_HISTORY_LABELS.get(seconds, f"{seconds} seconds")


def honeypot_post_message_error(message: str) -> str | None:
    message = message.strip()
    if not message:
        return "Honeypot message cannot be empty."
    if len(message) > HONEYPOT_POST_MESSAGE_MAX_LENGTH:
        return f"Honeypot message must be {HONEYPOT_POST_MESSAGE_MAX_LENGTH} characters or fewer."
    if URL_RE.search(message):
        return "Honeypot message cannot contain URLs."
    return None


async def find_existing_honeypot_post(
    channel: discord.TextChannel,
    bot_user_id: int,
    message_id: int | None,
) -> discord.Message | None:
    if message_id is not None:
        try:
            message = await channel.fetch_message(message_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            message = None
        else:
            if message.author.id == bot_user_id:
                return message

    try:
        async for message in channel.history(limit=50, oldest_first=True):
            if message.author.id == bot_user_id:
                return message
    except (discord.Forbidden, discord.HTTPException):
        return None

    return None


# ============================================================
# HoneypotAdmin cog
# ============================================================

class HoneypotAdmin(commands.Cog):
    """
    Admin cog for the honeypot module.

    Commands: /honeypot set-channel, set-action, enable-alerts, message, post
    """

    honeypot = app_commands.Group(
        name="honeypot",
        description="Honeypot admin commands.",
        guild_only=True,
        default_permissions=discord.Permissions(manage_roles=True),
    )

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.honeypot_store = HoneypotConfigStore()
        self.module_store = ConfigStore()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await require_module_enabled_for_interaction(
            interaction, self.module_store, "honeypot"
        )

    def cog_unload(self):
        self.honeypot_store.close()
        self.module_store.close()

    def _get_config(self, guild: discord.Guild) -> HoneypotConfig:
        config = self.honeypot_store.get_config(guild.id)
        return config if config is not None else HoneypotConfig(guild_id=guild.id)

    # ========================================================
    # Commands
    # ========================================================

    @honeypot.command(
        name="set-channel",
        description="Set the channel to monitor as a honeypot trap.",
    )
    @app_commands.describe(channel="Any message posted here will trigger the configured action.")
    async def honeypot_set_channel(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
    ):
        config = await require_amadeus_access(interaction, self.module_store)

        if config is None or interaction.guild is None:
            return

        # Check permissions before saving so we can warn about anything missing.
        bot_perms = channel.permissions_for(interaction.guild.me)
        missing = []
        if not bot_perms.manage_messages:
            missing.append("**Manage Messages** (required to delete messages)")
        if not bot_perms.manage_channels:
            missing.append("**Manage Channel** (required to set slow-mode)")

        self.honeypot_store.set_channel(interaction.guild.id, channel.id)
        log(
            f"HONEYPOT // CHANNEL SET 『 {channel.id} 』 GUILD 『 {interaction.guild.id} 』",
            level="debug",
            logger_name="honeypot",
        )

        slowmode_note = ""
        if missing:
            slowmode_note = "\n\n⚠ Missing permissions on that channel:\n" + "\n".join(f"- {m}" for m in missing)
        else:
            try:
                await channel.edit(slowmode_delay=SLOWMODE_HONEYPOT, reason="Honeypot: 1-minute slow-mode")
                log(
                    f"HONEYPOT // SLOWMODE SET 『 CHANNEL {channel.id} 』 GUILD 『 {interaction.guild.id} 』",
                    level="debug",
                    logger_name="honeypot",
                )
            except discord.HTTPException as e:
                log(f"HONEYPOT // SLOWMODE FAILED 『 {e} 』", level="debug", logger_name="honeypot")
                slowmode_note = "\n\n⚠ Could not set slow-mode due to an unexpected error."

        await interaction.response.send_message(
            f"Honeypot channel set to {channel.mention}.{slowmode_note}",
            ephemeral=True,
        )

    @honeypot.command(
        name="set-action",
        description="Set the action taken against anyone who posts in the honeypot.",
    )
    @app_commands.describe(
        action="The moderation action to take.",
        role="Role to remove — only required for the remove-role action.",
        reason="Audit log reason for mute, kick, or ban.",
        delete_history="Delete this member's recent messages when the action succeeds.",
    )
    @app_commands.choices(action=[
        app_commands.Choice(name="Remove role", value="remove_role"),
        app_commands.Choice(name="Mute (28-day timeout)", value="mute"),
        app_commands.Choice(name="Kick", value="kick"),
        app_commands.Choice(name="Ban", value="ban"),
    ])
    @app_commands.choices(delete_history=[
        app_commands.Choice(name="1 hour", value=3600),
        app_commands.Choice(name="6 hours", value=21600),
        app_commands.Choice(name="12 hours", value=43200),
        app_commands.Choice(name="24 hours", value=86400),
    ])
    async def honeypot_set_action(
        self,
        interaction: discord.Interaction,
        action: str,
        role: discord.Role | None = None,
        reason: app_commands.Range[str, 1, 512] | None = None,
        delete_history: int | None = None,
    ):
        config = await require_amadeus_access(interaction, self.module_store)

        if config is None or interaction.guild is None:
            return

        if action == "remove_role":
            if role is None:
                await interaction.response.send_message(
                    "You must specify a **role** when using the remove-role action.",
                    ephemeral=True,
                )
                return

            if role == interaction.guild.default_role:
                await interaction.response.send_message(
                    "You cannot target @everyone as the removal role.",
                    ephemeral=True,
                )
                return

        self.honeypot_store.set_action(
            interaction.guild.id,
            action,
            role.id if role else None,
            reason if action in {"mute", "kick", "ban"} else None,
            delete_history,
        )
        log(
            f"HONEYPOT // ACTION SET 『 {action} 』 ROLE 『 {role.id if role else None} 』 GUILD 『 {interaction.guild.id} 』",
            level="debug",
            logger_name="honeypot",
        )

        reason_note = f"\nAudit log reason: `{reason}`" if action in {"mute", "kick", "ban"} and reason else ""
        delete_note = (
            f"\nMessage cleanup window: **{delete_history_label(delete_history)}**"
            if delete_history is not None
            else ""
        )
        await interaction.response.send_message(
            f"Honeypot action set to **{honeypot_action_label(action, role)}**.{reason_note}{delete_note}",
            ephemeral=True,
        )

    @honeypot.command(
        name="enable-alerts",
        description="Enable or disable alerts to the admin channel when the honeypot is triggered.",
    )
    @app_commands.describe(enabled="Whether to send an alert to the admin channel on each trigger.")
    async def honeypot_enable_alerts(
        self,
        interaction: discord.Interaction,
        enabled: bool,
    ):
        config = await require_amadeus_access(interaction, self.module_store)

        if config is None or interaction.guild is None:
            return

        self.honeypot_store.set_alerts_enabled(interaction.guild.id, enabled)
        log(
            f"HONEYPOT // ALERTS {'ENABLED' if enabled else 'DISABLED'} 『 GUILD {interaction.guild.id} 』",
            level="debug",
            logger_name="honeypot",
        )

        state = "enabled" if enabled else "disabled"
        hint = honeypot_alerts_hint(enabled, config.alert_channel_id)

        await interaction.response.send_message(
            f"Honeypot alerts **{state}**.{hint}",
            ephemeral=True,
        )

    @honeypot.command(
        name="message",
        description="Set the message posted by /honeypot post.",
    )
    @app_commands.describe(
        message=(
            "Message to post in the honeypot channel. URLs are not allowed. "
            f"Max {HONEYPOT_POST_MESSAGE_MAX_LENGTH} characters."
        )
    )
    async def honeypot_message(
        self,
        interaction: discord.Interaction,
        message: app_commands.Range[str, 1, HONEYPOT_POST_MESSAGE_MAX_LENGTH],
    ):
        config = await require_amadeus_access(interaction, self.module_store)

        if config is None or interaction.guild is None:
            return

        message = message.strip()
        error = honeypot_post_message_error(message)
        if error is not None:
            await interaction.response.send_message(error, ephemeral=True)
            return

        self.honeypot_store.set_post_message(interaction.guild.id, message)
        log(
            f"HONEYPOT // POST MESSAGE SET 『 GUILD {interaction.guild.id} 』",
            level="debug",
            logger_name="honeypot",
        )

        await interaction.response.send_message(
            "Honeypot post message updated.",
            ephemeral=True,
        )

    @honeypot.command(
        name="post",
        description="Post the configured warning message in the honeypot channel.",
    )
    async def honeypot_post(self, interaction: discord.Interaction):
        config = await require_amadeus_access(interaction, self.module_store)

        if config is None or interaction.guild is None:
            return

        if not self.module_store.is_module_enabled(interaction.guild.id, "honeypot"):
            await interaction.response.send_message(
                (
                    "The **honeypot** module is not enabled for this server.\n"
                    "Enable it first with `/amadeus module enable honeypot`."
                ),
                ephemeral=True,
            )
            return

        honeypot_config = self._get_config(interaction.guild)

        if honeypot_config.channel_id is None:
            await interaction.response.send_message(
                "Set a honeypot channel first with `/honeypot set-channel`.",
                ephemeral=True,
            )
            return

        channel = interaction.guild.get_channel(honeypot_config.channel_id)

        if not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message(
                "The configured honeypot channel no longer exists. Set a new one with `/honeypot set-channel`.",
                ephemeral=True,
            )
            return

        bot_user = self.bot.user
        if bot_user is None:
            await interaction.response.send_message(
                "Cannot post the warning message because the bot user is not ready.",
                ephemeral=True,
            )
            return

        post_message = honeypot_config.post_message or DEFAULT_HONEYPOT_POST_MESSAGE
        existing_message = await find_existing_honeypot_post(
            channel,
            bot_user.id,
            honeypot_config.post_message_id,
        )
        try:
            if existing_message is not None:
                sent_message = await existing_message.edit(
                    content=post_message,
                    embeds=[],
                    allowed_mentions=NO_MENTIONS,
                )
                action = "updated"
            else:
                sent_message = await channel.send(post_message, allowed_mentions=NO_MENTIONS)
                action = "posted"
        except (discord.Forbidden, discord.HTTPException) as e:
            log(f"HONEYPOT // POST // SEND FAILED 『 CHANNEL {channel.id} 』 GUILD 『 {interaction.guild.id} 』 // {e}", level="debug", logger_name="honeypot")
            await interaction.response.send_message(
                f"Failed to post the warning message in {channel.mention} — {e}",
                ephemeral=True,
            )
            return

        self.honeypot_store.set_post_message_id(interaction.guild.id, sent_message.id)
        await interaction.response.send_message(
            f"Warning message {action} in {channel.mention}.",
            ephemeral=True,
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(HoneypotAdmin(bot))
