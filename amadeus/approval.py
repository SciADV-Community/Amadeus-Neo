"""
Generic admin-channel approval system.

Approval requests post an embed to the configured alert channel with
persistent Approve / Deny buttons. Both buttons open a modal so the admin
can attach an optional comment before the decision is processed.

Usage
-----
1. Register a callback when your cog loads::

    from amadeus.approval import register_approval_callback, add_dynamic_items

    register_approval_callback("boost", my_handler)
    add_dynamic_items(bot)  # in cog setup(), not __init__

2. Post an approval request::

    from amadeus.approval import post_approval_request

    await post_approval_request(bot, store, guild_id, embed, "boost", user_id, request_id)

Callback signature
------------------
::

    async def handler(
        *,
        interaction: discord.Interaction,   # modal-submit interaction
        bot: commands.Bot,
        guild_id: int,
        user_id: int,
        request_id: str,
        approved: bool,
        comment: str | None,
    ) -> None
"""
import re
from collections.abc import Callable, Coroutine
from typing import Any

import discord
from discord.ext import commands

from amadeus.database import ConfigStore
from amadeus.discord_utils import NO_MENTIONS
from amadeus.logging_utils import log

# Maps flow_type → async callback.
_registry: dict[str, Callable[..., Coroutine[Any, Any, None]]] = {}


def register_approval_callback(
    flow_type: str,
    callback: Callable[..., Coroutine[Any, Any, None]],
) -> None:
    """Register a handler to be invoked when an approve/deny decision is made."""
    _registry[flow_type] = callback
    log(f"APPROVAL // REGISTERED 『 {flow_type} 』", level="debug", logger_name="approval")


def unregister_approval_callback(flow_type: str) -> None:
    _registry.pop(flow_type, None)


# ============================================================
# Comment modal
# ============================================================

class _CommentModal(discord.ui.Modal, title="Add a comment (optional)"):
    comment = discord.ui.TextInput(
        label="Comment for the user",
        placeholder="Included in the DM notification sent to the user.",
        required=False,
        max_length=500,
        style=discord.TextStyle.paragraph,
    )

    def __init__(
        self,
        *,
        approved: bool,
        flow_type: str,
        guild_id: int,
        user_id: int,
        request_id: str,
        bot: commands.Bot,
        approval_message: discord.Message,
    ):
        super().__init__()
        self.approved = approved
        self.flow_type = flow_type
        self.guild_id = guild_id
        self.user_id = user_id
        self.request_id = request_id
        self.bot = bot
        self.approval_message = approval_message

    async def on_submit(self, interaction: discord.Interaction) -> None:
        comment = self.comment.value.strip() or None
        callback = _registry.get(self.flow_type)

        if callback is None:
            await interaction.response.send_message(
                "This approval type has no registered handler. The module may be unloaded.",
                ephemeral=True,
            )
            return

        if not await _require_approval_role(interaction, self.guild_id):
            return

        # Defer before doing async work (role creation, DMs, etc. can be slow).
        await interaction.response.defer(ephemeral=True, thinking=True)

        # Edit the approval message to reflect the decision and disable the buttons,
        # preventing a second admin from inadvertently processing it twice.
        decision_label = "✅ Approved" if self.approved else "❌ Denied"
        try:
            await self.approval_message.edit(
                content=f"{decision_label} by {interaction.user.mention}",
                view=None,
                allowed_mentions=NO_MENTIONS,
            )
        except (discord.Forbidden, discord.HTTPException) as e:
            log(
                f"APPROVAL // FAILED TO UPDATE APPROVAL MSG 『 {e} 』",
                level="debug",
                logger_name="approval",
            )

        try:
            await callback(
                interaction=interaction,
                bot=self.bot,
                guild_id=self.guild_id,
                user_id=self.user_id,
                request_id=self.request_id,
                approved=self.approved,
                comment=comment,
            )
        except Exception:
            log(
                f"APPROVAL // CALLBACK ERROR 『 {self.flow_type} 』 "
                f"GUILD 『 {self.guild_id} 』 USER 『 {self.user_id} 』",
                level="exception",
                logger_name="approval",
            )
            await interaction.followup.send(
                "An error occurred while processing the decision.",
                ephemeral=True,
            )


# ============================================================
# Persistent dynamic buttons
#
# custom_id format:
#   amadeus_approval:approve:{flow_type}:{guild_id}:{user_id}:{request_id}
#   amadeus_approval:deny:{flow_type}:{guild_id}:{user_id}:{request_id}
# ============================================================

_REQUEST_ID_PATTERN = r"[A-Za-z0-9_-]{16,64}"
_APPROVE_RE = re.compile(
    rf"amadeus_approval:approve:(?P<flow_type>[a-z_]+):(?P<guild_id>\d+):(?P<user_id>\d+):(?P<request_id>{_REQUEST_ID_PATTERN})"
)
_DENY_RE = re.compile(
    rf"amadeus_approval:deny:(?P<flow_type>[a-z_]+):(?P<guild_id>\d+):(?P<user_id>\d+):(?P<request_id>{_REQUEST_ID_PATTERN})"
)


async def _require_approval_role(interaction: discord.Interaction, guild_id: int) -> bool:
    if interaction.guild is None or interaction.guild.id != guild_id:
        await interaction.response.send_message(
            "This approval can only be processed inside its original server.",
            ephemeral=True,
        )
        return False

    if not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message(
            "Could not read your server member data.",
            ephemeral=True,
        )
        return False

    store = ConfigStore()
    try:
        config = store.get_guild_config(guild_id)
    except RuntimeError:
        await interaction.response.send_message(
            "This server is missing Amadeus configuration.",
            ephemeral=True,
        )
        return False
    finally:
        store.close()

    if config.alert_channel_id != interaction.channel_id:
        await interaction.response.send_message(
            "This approval can only be processed from the configured admin alert channel.",
            ephemeral=True,
        )
        return False

    if config.admin_role_id is None:
        await interaction.response.send_message(
            "Approvals require an Amadeus admin role. Run `/amadeus set-admin-role` first.",
            ephemeral=True,
        )
        return False

    admin_role = interaction.guild.get_role(config.admin_role_id)
    if admin_role is None or admin_role not in interaction.user.roles:
        await interaction.response.send_message(
            "You need the configured Amadeus admin role to process approvals.",
            ephemeral=True,
        )
        return False

    return True


class _ApproveButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=rf"amadeus_approval:approve:[a-z_]+:\d+:\d+:{_REQUEST_ID_PATTERN}",
):
    def __init__(self, flow_type: str, guild_id: int, user_id: int, request_id: str) -> None:
        super().__init__(
            discord.ui.Button(
                label="Approve",
                style=discord.ButtonStyle.success,
                custom_id=f"amadeus_approval:approve:{flow_type}:{guild_id}:{user_id}:{request_id}",
            )
        )
        self.flow_type = flow_type
        self.guild_id = guild_id
        self.user_id = user_id
        self.request_id = request_id

    @classmethod
    async def from_custom_id(
        cls,
        interaction: discord.Interaction,
        item: discord.ui.Button,
        match: re.Match,
    ) -> "_ApproveButton":
        m = _APPROVE_RE.fullmatch(item.custom_id)
        return cls(
            flow_type=m.group("flow_type"),
            guild_id=int(m.group("guild_id")),
            user_id=int(m.group("user_id")),
            request_id=m.group("request_id"),
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if not await _require_approval_role(interaction, self.guild_id):
            return
        await interaction.response.send_modal(
            _CommentModal(
                approved=True,
                flow_type=self.flow_type,
                guild_id=self.guild_id,
                user_id=self.user_id,
                request_id=self.request_id,
                bot=interaction.client,
                approval_message=interaction.message,
            )
        )


class _DenyButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=rf"amadeus_approval:deny:[a-z_]+:\d+:\d+:{_REQUEST_ID_PATTERN}",
):
    def __init__(self, flow_type: str, guild_id: int, user_id: int, request_id: str) -> None:
        super().__init__(
            discord.ui.Button(
                label="Deny",
                style=discord.ButtonStyle.danger,
                custom_id=f"amadeus_approval:deny:{flow_type}:{guild_id}:{user_id}:{request_id}",
            )
        )
        self.flow_type = flow_type
        self.guild_id = guild_id
        self.user_id = user_id
        self.request_id = request_id

    @classmethod
    async def from_custom_id(
        cls,
        interaction: discord.Interaction,
        item: discord.ui.Button,
        match: re.Match,
    ) -> "_DenyButton":
        m = _DENY_RE.fullmatch(item.custom_id)
        return cls(
            flow_type=m.group("flow_type"),
            guild_id=int(m.group("guild_id")),
            user_id=int(m.group("user_id")),
            request_id=m.group("request_id"),
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if not await _require_approval_role(interaction, self.guild_id):
            return
        await interaction.response.send_modal(
            _CommentModal(
                approved=False,
                flow_type=self.flow_type,
                guild_id=self.guild_id,
                user_id=self.user_id,
                request_id=self.request_id,
                bot=interaction.client,
                approval_message=interaction.message,
            )
        )


class ApprovalView(discord.ui.View):
    """Persistent view holding Approve and Deny buttons for one request."""

    def __init__(self, flow_type: str, guild_id: int, user_id: int, request_id: str) -> None:
        super().__init__(timeout=None)
        self.add_item(_ApproveButton(flow_type, guild_id, user_id, request_id))
        self.add_item(_DenyButton(flow_type, guild_id, user_id, request_id))


def add_dynamic_items(bot: commands.Bot) -> None:
    """
    Registers the DynamicItem classes so approval buttons survive bot restarts.

    Call this once from each cog's setup() that uses this module.
    """
    bot.add_dynamic_items(_ApproveButton, _DenyButton)


async def post_approval_request(
    bot: commands.Bot,
    store: ConfigStore,
    guild_id: int,
    embed: discord.Embed,
    flow_type: str,
    user_id: int,
    request_id: str,
    *,
    extra_embeds: list[discord.Embed] | None = None,
    files: list[discord.File] | None = None,
) -> bool:
    """
    Posts an approval embed to the guild's configured admin alert channel.

    Returns True on success, False if no channel is configured or the post fails.
    """
    try:
        config = store.get_guild_config(guild_id)
    except RuntimeError:
        return False

    if config.alert_channel_id is None:
        log(f"APPROVAL // NO ALERT CHANNEL 『 GUILD {guild_id} 』", level="debug", logger_name="approval")
        return False

    channel = bot.get_channel(config.alert_channel_id)

    if not isinstance(channel, discord.TextChannel):
        log(
            f"APPROVAL // CHANNEL NOT FOUND 『 {config.alert_channel_id} 』 GUILD 『 {guild_id} 』",
            level="debug",
            logger_name="approval",
        )
        return False

    try:
        embeds = [embed]
        if extra_embeds:
            embeds.extend(extra_embeds)
        await channel.send(
            embeds=embeds,
            files=files or None,
            view=ApprovalView(flow_type, guild_id, user_id, request_id),
            allowed_mentions=NO_MENTIONS,
        )
        log(
            f"APPROVAL // REQUEST POSTED 『 FLOW {flow_type} 』 USER {user_id} GUILD {guild_id}",
            level="debug",
            logger_name="approval",
        )
        return True
    except (discord.Forbidden, discord.HTTPException) as e:
        log(f"APPROVAL // SEND FAILED 『 {e} 』 GUILD {guild_id}", level="debug", logger_name="approval")
        return False
