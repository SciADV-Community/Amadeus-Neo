"""
Boost perks cog.

Listens for server boosts and walks the booster through a DM conversation
to collect custom role and emoji preferences. The completed request is sent
to the admin alert channel for review.

Flow states (stored in dm_flow_state.state)
-------------------------------------------
ROLE_NAME    → ROLE_IMAGE → (tier 2) ROLE_COLOR
  → (slots OK) EMOJI_1_NAME → EMOJI_1_IMAGE
  → (tier 2)   EMOJI_2_NAME → EMOJI_2_IMAGE
  → CONFIRMATION → PENDING

Data keys in dm_flow_state.data (JSON)
---------------------------------------
tier              int         1 or 2
role_name         str
role_image_b64    str         base64-encoded bytes
role_color_hex    str         "#RRGGBB" (tier 2 only)
emoji_1_name      str
emoji_1_b64       str         base64-encoded bytes
emoji_2_name      str         (tier 2 only)
emoji_2_b64       str         (tier 2 only)
emoji_skipped     bool        True if slots were insufficient at collection time
"""
import base64
import io
import re
import secrets

import aiohttp
import discord
from discord.ext import commands, tasks

from amadeus.approval import (
    add_dynamic_items,
    post_approval_request,
    register_approval_callback,
    unregister_approval_callback,
)
from amadeus.boost_store import BoostStore
from amadeus.boost_utils import check_emoji_slots, get_proposed_tier, is_active_booster
from amadeus.database import ConfigStore
from amadeus.discord_utils import (
    EMOJI_IMAGE_EXTENSIONS,
    ROLE_ICON_EXTENSIONS,
    escape_untrusted_text,
    image_extension_from_url,
    safe_dm,
    validate_discord_attachment_image,
    validate_safe_role_name,
)
from amadeus.dm_flow import DmFlowStore
from amadeus.logging_utils import log
from amadeus.models.boost import BoostGrant
from amadeus.models.dm_flow import DmFlow

FLOW_TYPE = "boost"
FLOW_TIMEOUT_SECONDS = 48 * 3600  # 48 hours before an idle flow is expired

# Discord emoji name: letters, digits, underscores, 2–32 chars
_EMOJI_NAME_RE = re.compile(r"^[a-zA-Z0-9_]{2,32}$")
# Hex color: optional leading #, exactly 6 hex digits
_HEX_COLOR_RE = re.compile(r"^#?([0-9A-Fa-f]{6})$")
# Discord emoji file size limit
_EMOJI_MAX_BYTES = 256 * 1024  # 256 KB
_ROLE_NAME_MAX_LENGTH = 32
# Small emoji/icon images make Discord render very narrow embeds. Padding the
# preview asset itself is the only reliable way to force a minimum visual width.
_PREVIEW_MIN_WIDTH = 220


# ============================================================
# Flow state name constants
# ============================================================

class S:
    ROLE_NAME    = "awaiting_role_name"
    ROLE_IMAGE   = "awaiting_role_image"
    ROLE_COLOR   = "awaiting_role_color"
    EMOJI_1_NAME = "awaiting_emoji_1_name"
    EMOJI_1_IMAGE = "awaiting_emoji_1_image"
    EMOJI_2_NAME = "awaiting_emoji_2_name"
    EMOJI_2_IMAGE = "awaiting_emoji_2_image"
    CONFIRMATION = "awaiting_confirmation"
    PENDING      = "pending_approval"
    PROCESSING   = "processing_approval"
    DENIED       = "denied_awaiting_restart"


# ============================================================
# Persistent confirmation buttons (DynamicItem)
#
# custom_id: amadeus_boost:{action}:{guild_id}:{user_id}
# ============================================================

_CONFIRM_RE = re.compile(r"amadeus_boost:confirm:(?P<guild_id>\d+):(?P<user_id>\d+)")
_RESTART_RE = re.compile(r"amadeus_boost:restart:(?P<guild_id>\d+):(?P<user_id>\d+)")


class _ConfirmButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"amadeus_boost:confirm:\d+:\d+",
):
    def __init__(self, guild_id: int, user_id: int) -> None:
        super().__init__(
            discord.ui.Button(
                label="Submit for Review",
                style=discord.ButtonStyle.success,
                custom_id=f"amadeus_boost:confirm:{guild_id}:{user_id}",
            )
        )
        self.guild_id = guild_id
        self.user_id = user_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        m = _CONFIRM_RE.match(item.custom_id)
        return cls(int(m.group("guild_id")), int(m.group("user_id")))

    async def callback(self, interaction: discord.Interaction) -> None:
        cog: Boost | None = interaction.client.get_cog("Boost")
        if cog is None:
            await interaction.response.send_message(
                "The boost module is currently unavailable. Please try again later.",
                ephemeral=True,
            )
            return
        await cog.handle_confirm(interaction, self.guild_id, self.user_id)


class _RestartButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"amadeus_boost:restart:\d+:\d+",
):
    def __init__(self, guild_id: int, user_id: int) -> None:
        super().__init__(
            discord.ui.Button(
                label="Start Over",
                style=discord.ButtonStyle.secondary,
                custom_id=f"amadeus_boost:restart:{guild_id}:{user_id}",
            )
        )
        self.guild_id = guild_id
        self.user_id = user_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        m = _RESTART_RE.match(item.custom_id)
        return cls(int(m.group("guild_id")), int(m.group("user_id")))

    async def callback(self, interaction: discord.Interaction) -> None:
        cog: Boost | None = interaction.client.get_cog("Boost")
        if cog is None:
            await interaction.response.send_message(
                "The boost module is currently unavailable. Please try again later.",
                ephemeral=True,
            )
            return
        await cog.handle_restart(interaction, self.guild_id, self.user_id)


# ============================================================
# Boost cog
# ============================================================

class Boost(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.flow_store = DmFlowStore()
        self.boost_store = BoostStore()
        self.module_store = ConfigStore()

    def cog_unload(self):
        self._expire_flows.cancel()
        unregister_approval_callback(FLOW_TYPE)
        self.flow_store.close()
        self.boost_store.close()
        self.module_store.close()

    # ========================================================
    # Boost detection
    # ========================================================

    @commands.Cog.listener()
    async def on_ready(self):
        """Sync subscription count cache on startup so the first tier inference is accurate."""
        for guild in self.bot.guilds:
            if self.module_store.is_module_enabled(guild.id, "boost"):
                self.boost_store.set_subscription_count(guild.id, guild.premium_subscription_count or 0)

        if not self._expire_flows.is_running():
            self._expire_flows.start()

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member) -> None:
        if before.premium_since == after.premium_since:
            return

        if not self.module_store.is_module_enabled(after.guild.id, "boost"):
            return

        if before.premium_since is None and after.premium_since is not None:
            await self._on_boost_start(after)
        elif before.premium_since is not None and after.premium_since is None:
            await self._on_boost_end(after)

    async def _on_boost_start(self, member: discord.Member) -> None:
        guild = member.guild
        # Note: guild.premium_subscription_count may not be updated yet when
        # on_member_update fires — it arrives in a separate GUILD_UPDATE event.
        # This means tier inference can be off by a small window. Admins confirm
        # the actual tier at approval time.
        current_count = guild.premium_subscription_count or 0
        cached = self.boost_store.get_subscription_count(guild.id)
        # If we have no cached value, assume this is the only new boost (tier 1).
        tier = get_proposed_tier(
            cached if cached is not None else max(0, current_count - 1),
            current_count,
        )
        self.boost_store.set_subscription_count(guild.id, current_count)

        log(
            f"BOOST // START 『 USER {member.id} 』 GUILD 『 {guild.id} 』 "
            f"TIER {tier} CACHED {cached} CURRENT {current_count}",
            level="debug",
            logger_name="boost",
        )

        # Skip if a grant already exists (bot added to a server with existing boosters).
        if self.boost_store.get_grant(guild.id, member.id) is not None:
            log(f"BOOST // GRANT EXISTS, SKIPPING 『 USER {member.id} 』", level="debug", logger_name="boost")
            return

        # Skip if a flow is already in progress for this member.
        if self.flow_store.get(guild.id, member.id, FLOW_TYPE) is not None:
            log(f"BOOST // FLOW EXISTS, SKIPPING 『 USER {member.id} 』", level="debug", logger_name="boost")
            return

        await self._start_flow(guild, member, tier)

    async def _on_boost_end(self, member: discord.Member) -> None:
        guild = member.guild
        self.boost_store.set_subscription_count(guild.id, guild.premium_subscription_count or 0)

        log(f"BOOST // END 『 USER {member.id} 』 GUILD 『 {guild.id} 』", level="debug", logger_name="boost")

        # Discard any in-progress flow.
        self.flow_store.delete(guild.id, member.id, FLOW_TYPE)

        grant = self.boost_store.get_grant(guild.id, member.id)
        if grant is None:
            return

        await self._teardown_grant(guild, grant)

        await safe_dm(
            member,
            content=(
                f"Your boost perks from **{guild.name}** have been removed since you're no longer boosting. "
                "Thank you for your support! Boost again any time to reclaim your perks."
            ),
        )

    # ========================================================
    # Flow lifecycle
    # ========================================================

    async def _start_flow(
        self, guild: discord.Guild, member: discord.Member, tier: int, forced: bool = False
    ) -> None:
        """Creates a fresh flow and sends the welcome DM + first prompt."""
        data: dict = {"tier": tier}
        if forced:
            data["forced"] = True

        flow = DmFlow(
            guild_id=guild.id,
            user_id=member.id,
            flow_type=FLOW_TYPE,
            state=S.ROLE_NAME,
            data=data,
        )
        self.flow_store.save(flow)

        welcome = discord.Embed(
            title=f"🎉 Thanks for boosting {guild.name}!",
            description=(
                f"You've added **{tier} boost{'s' if tier > 1 else ''}** and unlocked:\n\n"
                f"{self._tier_perks_text(tier)}\n"
                "I'll guide you through setting them up now. Type `restart` to start over, or `cancel` to stop. "
                f"You can also use `/boost status` in **{guild.name}** to come back later."
            ),
            color=discord.Color.from_rgb(255, 115, 250),
        )
        await safe_dm(member, embed=welcome)
        await self._send_prompt(member, flow)

    async def _reset_flow(self, guild_id: int, user_id: int, tier: int, forced: bool = False) -> DmFlow:
        """Resets the flow to the first step, preserving the tier."""
        data: dict = {"tier": tier}
        if forced:
            data["forced"] = True

        flow = DmFlow(
            guild_id=guild_id,
            user_id=user_id,
            flow_type=FLOW_TYPE,
            state=S.ROLE_NAME,
            data=data,
        )
        self.flow_store.save(flow)
        return flow

    # ========================================================
    # DM routing
    # ========================================================

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if not isinstance(message.channel, discord.DMChannel) or message.author.bot:
            return

        flows = self.flow_store.get_all_for_user(message.author.id)
        command = message.content.strip().lower()
        commandable = [
            f for f in flows
            if f.state not in (S.PENDING, S.PROCESSING)
        ]

        if command in {"boost", "cancel", "restart"}:
            if command == "boost":
                commandable = [f for f in commandable if not f.data.get("forced")]
                if not commandable:
                    if await self._start_self_service_flow_from_dm(message.author):
                        return
                    await safe_dm(
                        message.author,
                        content="I don't see an active boost perks request I can restart. Use `/boost status` in the server to check your status.",
                    )
                    return
            elif not commandable:
                return

            # get_all_for_user returns flows sorted by updated_at DESC; use the freshest.
            flow = commandable[0]
            tier = flow.data.get("tier", 1)
            forced = bool(flow.data.get("forced"))
            guild = self.bot.get_guild(flow.guild_id)
            guild_name = guild.name if guild else "the server"

            if command == "cancel":
                self.flow_store.delete(flow.guild_id, flow.user_id, FLOW_TYPE)
                await safe_dm(
                    message.author,
                    content=(
                        "Your boost perks setup has been cancelled. "
                        f"Type 'boost' if you would like to start again. "
                        f"Alternatively, use `/boost status` in **{guild_name}** if you want to start again later."
                    ),
                )
                return

            flow = await self._reset_flow(flow.guild_id, flow.user_id, tier, forced=forced)
            await safe_dm(
                message.author,
                content=(
                    "Restarting from the beginning. "
                    f"You can also use `/boost status` in **{guild_name}** to manage your request."
                ),
            )
            await self._send_prompt(message.author, flow)
            return

        # Only handle flows that are actively waiting for a text/image reply.
        active = [
            f for f in flows
            if f.state not in (S.PENDING, S.PROCESSING, S.CONFIRMATION, S.DENIED)
        ]
        if not active:
            return

        # get_all_for_user returns flows sorted by updated_at DESC; use the freshest.
        flow = active[0]

        handlers = {
            S.ROLE_NAME:     self._step_role_name,
            S.ROLE_IMAGE:    self._step_role_image,
            S.ROLE_COLOR:    self._step_role_color,
            S.EMOJI_1_NAME:  self._step_emoji_name,
            S.EMOJI_1_IMAGE: self._step_emoji_image,
            S.EMOJI_2_NAME:  self._step_emoji_2_name,
            S.EMOJI_2_IMAGE: self._step_emoji_2_image,
        }
        handler = handlers.get(flow.state)
        if handler:
            await handler(flow, message)

    async def _start_self_service_flow_from_dm(self, user: discord.User | discord.Member) -> bool:
        """
        Starts a normal boost flow from a DM keyword if the user is currently boosting.

        Admin-forced flows are intentionally excluded; those must be started by admins.
        """
        for guild in self.bot.guilds:
            if not self.module_store.is_module_enabled(guild.id, "boost"):
                continue
            if self.boost_store.get_grant(guild.id, user.id) is not None:
                continue
            existing_flow = self.flow_store.get(guild.id, user.id, FLOW_TYPE)
            if existing_flow is not None:
                continue

            member = guild.get_member(user.id)
            if member is None:
                try:
                    member = await guild.fetch_member(user.id)
                except (discord.NotFound, discord.HTTPException):
                    continue
            if not is_active_booster(member):
                continue

            current_count = guild.premium_subscription_count or 0
            cached = self.boost_store.get_subscription_count(guild.id)
            tier = get_proposed_tier(
                cached if cached is not None else max(0, current_count - 1),
                current_count,
            )
            await self._start_flow(guild, member, tier)
            log(
                f"BOOST // DM KEYWORD START 『 USER {user.id} 』 GUILD 『 {guild.id} 』 TIER {tier}",
                level="debug",
                logger_name="boost",
            )
            return True

        return False

    # ========================================================
    # Step handlers
    # ========================================================

    async def _step_role_name(self, flow: DmFlow, message: discord.Message) -> None:
        name = message.content.strip()
        if not name:
            await safe_dm(message.author, content="Please send a name for your role.")
            return
        if len(name) > _ROLE_NAME_MAX_LENGTH:
            await safe_dm(
                message.author,
                content=f"Role names must be {_ROLE_NAME_MAX_LENGTH} characters or fewer. Please try again.",
            )
            return
        role_name_error = validate_safe_role_name(name)
        if role_name_error is not None:
            await safe_dm(message.author, content=f"{role_name_error} Please try again.")
            return

        flow.data["role_name"] = name
        flow.state = S.ROLE_IMAGE
        self.flow_store.save(flow)
        await self._send_prompt(message.author, flow)

    async def _step_role_image(self, flow: DmFlow, message: discord.Message) -> None:
        attachment = self._first_image_attachment(message)
        if attachment is None:
            await safe_dm(message.author, content="Please upload an image file (PNG, JPEG, or WebP).")
            return

        validation_error = validate_discord_attachment_image(attachment, ROLE_ICON_EXTENSIONS)
        if validation_error is not None:
            await safe_dm(message.author, content=validation_error)
            return

        data = await self._download_attachment(message.author, attachment, max_size=4 * 1024 * 1024)
        if data is None:
            return

        flow.data["role_image_b64"] = base64.b64encode(data).decode()
        flow.data["role_image_ext"] = image_extension_from_url(attachment.url) or ".png"

        if flow.data.get("tier", 1) == 2:
            flow.state = S.ROLE_COLOR
        else:
            await self._advance_past_role(flow, message.author)
            return

        self.flow_store.save(flow)
        await self._send_prompt(message.author, flow)

    async def _step_role_color(self, flow: DmFlow, message: discord.Message) -> None:
        m = _HEX_COLOR_RE.match(message.content.strip())
        if not m:
            await safe_dm(
                message.author,
                content="Please send a valid hex color code, e.g. `#FF5733` or `A3C2FF`.",
            )
            return

        flow.data["role_color_hex"] = f"#{m.group(1).upper()}"
        await self._advance_past_role(flow, message.author)

    async def _advance_past_role(self, flow: DmFlow, user: discord.User) -> None:
        """
        After role details are collected, decide whether to collect emojis.

        Checks available emoji slots for the guild. If there aren't enough,
        skips emoji steps and goes straight to the confirmation summary.
        """
        tier = flow.data.get("tier", 1)
        needed = tier  # 1 slot for tier 1, 2 for tier 2
        guild = self.bot.get_guild(flow.guild_id)

        if guild is not None:
            sufficient, available, capacity = check_emoji_slots(guild, needed)
            if not sufficient:
                flow.data["emoji_skipped"] = True
                self.flow_store.save(flow)
                await safe_dm(
                    user,
                    content=(
                        f"⚠ There are currently only **{available}/{capacity}** emoji slots available, "
                        f"which isn't enough for your perk{'s' if needed > 1 else ''}. "
                        "The emoji step will be skipped — you can use `/boost status` later to add them "
                        "once the server has more free slots."
                    ),
                )
                await self._send_confirmation(user, flow)
                return
        # Enough slots (or guild not cached — proceed and let admin verify)
        flow.state = S.EMOJI_1_NAME
        self.flow_store.save(flow)
        await self._send_prompt(user, flow)

    async def _step_emoji_name(self, flow: DmFlow, message: discord.Message) -> None:
        name = message.content.strip()
        if not _EMOJI_NAME_RE.match(name):
            await safe_dm(
                message.author,
                content="Emoji names must be 2–32 characters, letters/numbers/underscores only. Please try again.",
            )
            return

        flow.data["emoji_1_name"] = name
        flow.state = S.EMOJI_1_IMAGE
        self.flow_store.save(flow)
        await self._send_prompt(message.author, flow)

    async def _step_emoji_image(self, flow: DmFlow, message: discord.Message) -> None:
        attachment = self._first_image_attachment(message)
        if attachment is None:
            await safe_dm(message.author, content="Please upload an image file (PNG, JPEG, GIF, or WebP).")
            return

        validation_error = validate_discord_attachment_image(attachment, EMOJI_IMAGE_EXTENSIONS)
        if validation_error is not None:
            await safe_dm(message.author, content=validation_error)
            return

        data = await self._download_attachment(message.author, attachment, max_size=_EMOJI_MAX_BYTES)
        if data is None:
            return

        flow.data["emoji_1_b64"] = base64.b64encode(data).decode()
        flow.data["emoji_1_ext"] = image_extension_from_url(attachment.url) or ".png"

        if flow.data.get("tier", 1) == 2:
            flow.state = S.EMOJI_2_NAME
            self.flow_store.save(flow)
            await self._send_prompt(message.author, flow)
        else:
            self.flow_store.save(flow)
            await self._send_confirmation(message.author, flow)

    async def _step_emoji_2_name(self, flow: DmFlow, message: discord.Message) -> None:
        name = message.content.strip()
        if not _EMOJI_NAME_RE.match(name):
            await safe_dm(
                message.author,
                content="Emoji names must be 2–32 characters, letters/numbers/underscores only. Please try again.",
            )
            return

        flow.data["emoji_2_name"] = name
        flow.state = S.EMOJI_2_IMAGE
        self.flow_store.save(flow)
        await self._send_prompt(message.author, flow)

    async def _step_emoji_2_image(self, flow: DmFlow, message: discord.Message) -> None:
        attachment = self._first_image_attachment(message)
        if attachment is None:
            await safe_dm(message.author, content="Please upload an image file (PNG, JPEG, GIF, or WebP).")
            return

        validation_error = validate_discord_attachment_image(attachment, EMOJI_IMAGE_EXTENSIONS)
        if validation_error is not None:
            await safe_dm(message.author, content=validation_error)
            return

        data = await self._download_attachment(message.author, attachment, max_size=_EMOJI_MAX_BYTES)
        if data is None:
            return

        flow.data["emoji_2_b64"] = base64.b64encode(data).decode()
        flow.data["emoji_2_ext"] = image_extension_from_url(attachment.url) or ".png"
        self.flow_store.save(flow)
        await self._send_confirmation(message.author, flow)

    # ========================================================
    # Confirmation
    # ========================================================

    async def _send_confirmation(self, user: discord.User, flow: DmFlow) -> None:
        flow.state = S.CONFIRMATION
        self.flow_store.save(flow)

        guild = self.bot.get_guild(flow.guild_id)
        guild_name = guild.name if guild else f"Server {flow.guild_id}"
        data = flow.data
        tier = data.get("tier", 1)

        embed = discord.Embed(
            title="Review Your Request",
            description=(
                f"Here's everything you've set up for **{guild_name}**.\n"
                "Click **Submit for Review** to send it to the admins, "
                "or **Start Over** to redo your choices."
            ),
            color=self._flow_accent_color(flow, discord.Color.orange()),
        )
        embed.add_field(name="Tier", value=str(tier), inline=False)
        embed.add_field(name="Tier Includes", value=self._tier_perks_text(tier), inline=False)

        view = discord.ui.View(timeout=None)
        view.add_item(_ConfirmButton(flow.guild_id, flow.user_id))
        view.add_item(_RestartButton(flow.guild_id, flow.user_id))

        embeds, files = self._build_preview_payload(flow, embed)
        await safe_dm(user, embeds=embeds, files=files, view=view)

    async def handle_confirm(
        self,
        interaction: discord.Interaction,
        guild_id: int,
        user_id: int,
    ) -> None:
        """Invoked when the user clicks 'Submit for Review' in their DM."""
        if interaction.user.id != user_id:
            await interaction.response.send_message("This button isn't for you.", ephemeral=True)
            return

        flow = self.flow_store.get(guild_id, user_id, FLOW_TYPE)
        if flow is None or flow.state != S.CONFIRMATION:
            await interaction.response.send_message(
                "This session has expired. Use `/boost status` in the server to start a new one.",
                ephemeral=True,
            )
            return

        # Re-verify the member is still boosting at submission time,
        # unless the flow was force-started by an admin.
        guild = self.bot.get_guild(guild_id)
        if guild is not None and not flow.data.get("forced"):
            try:
                member = await guild.fetch_member(user_id)
                if not is_active_booster(member):
                    await interaction.response.send_message(
                        "It looks like you're no longer boosting — the request cannot be submitted.",
                        ephemeral=True,
                    )
                    self.flow_store.delete(guild_id, user_id, FLOW_TYPE)
                    return
            except (discord.NotFound, discord.HTTPException) as e:
                # Can't verify — proceed optimistically; admin can review at approval time.
                log(
                    f"BOOST // MEMBER FETCH FAILED AT SUBMISSION 『 USER {user_id} 』 GUILD 『 {guild_id} 』 // {e}",
                    level="debug",
                    logger_name="boost",
                )

        request_id = secrets.token_urlsafe(16)
        flow.data["approval_request_id"] = request_id
        self.flow_store.save(flow)

        embed = self._build_approval_embed(flow, guild)
        approval_embeds, approval_files = self._build_preview_payload(flow, embed)
        posted = await post_approval_request(
            self.bot,
            self.module_store,
            guild_id,
            approval_embeds[0],
            FLOW_TYPE,
            user_id,
            request_id,
            extra_embeds=approval_embeds[1:],
            files=approval_files,
        )

        if not posted:
            flow.data.pop("approval_request_id", None)
            self.flow_store.save(flow)
            await interaction.response.send_message(
                "⚠ Could not reach the admin alert channel. Ask an admin to run `/boost admin start` to manually trigger the flow.",
                ephemeral=True,
            )
            return

        flow.state = S.PENDING
        self.flow_store.save(flow)

        await interaction.response.send_message(
            "✅ Your request has been submitted for admin review. You'll receive a DM once a decision is made!",
            ephemeral=True,
        )

    async def handle_restart(
        self,
        interaction: discord.Interaction,
        guild_id: int,
        user_id: int,
    ) -> None:
        """Invoked when the user clicks 'Start Over' in their DM."""
        if interaction.user.id != user_id:
            await interaction.response.send_message("This button isn't for you.", ephemeral=True)
            return

        flow = self.flow_store.get(guild_id, user_id, FLOW_TYPE)
        tier = flow.data.get("tier", 1) if flow else 1
        forced = bool(flow.data.get("forced")) if flow else False
        flow = await self._reset_flow(guild_id, user_id, tier, forced=forced)

        await interaction.response.send_message("Starting over!", ephemeral=True)
        await self._send_prompt(interaction.user, flow)

    # ========================================================
    # Approval callback (registered with amadeus.approval)
    # ========================================================

    async def _handle_approval(
        self,
        *,
        interaction: discord.Interaction,
        bot: commands.Bot,
        guild_id: int,
        user_id: int,
        request_id: str,
        approved: bool,
        comment: str | None,
    ) -> None:
        flow = self.flow_store.get(guild_id, user_id, FLOW_TYPE)
        if flow is None:
            await interaction.followup.send("Flow not found — the user may have cancelled.", ephemeral=True)
            return

        if flow.state != S.PENDING:
            await interaction.followup.send("This request has already been processed or is not pending.", ephemeral=True)
            return

        if flow.data.get("approval_request_id") != request_id:
            await interaction.followup.send("This approval button is stale. Use the latest approval request.", ephemeral=True)
            return

        # Claim synchronously before the first await so concurrent modal submits
        # cannot both create roles/emojis for the same pending request.
        flow.state = S.PROCESSING
        self.flow_store.save(flow)

        guild = self.bot.get_guild(guild_id)

        try:
            user = await self.bot.fetch_user(user_id)
        except (discord.NotFound, discord.HTTPException):
            user = None

        guild_name = guild.name if guild else f"Server {guild_id}"

        if not approved:
            tier = flow.data.get("tier", 1)
            forced = bool(flow.data.get("forced"))
            flow.state = S.DENIED
            flow.data = {"tier": tier}
            if forced:
                flow.data["forced"] = True
            self.flow_store.save(flow)

            if user is not None:
                msg = f"Your boost perks request for {guild_name} was not approved."
                msg += (
                    "\n\nPlease type 'boost' if you would like to start again. "
                    f"Alternatively, use `/boost status` in {guild_name} if you want to start again at a later time."
                )
                await safe_dm(user, content=msg)

            await interaction.followup.send("Decision recorded. The user can type `boost` to start again.", ephemeral=True)
            return

        # ---- Approved ----
        if guild is None:
            await interaction.followup.send("Guild not found.", ephemeral=True)
            flow.state = S.PENDING
            self.flow_store.save(flow)
            return

        try:
            member = await guild.fetch_member(user_id)
        except (discord.NotFound, discord.HTTPException):
            await interaction.followup.send("Member is no longer in the server.", ephemeral=True)
            self.flow_store.delete(guild_id, user_id, FLOW_TYPE)
            return

        if not flow.data.get("forced") and not is_active_booster(member):
            await interaction.followup.send("Member is no longer boosting — perks not applied.", ephemeral=True)
            self.flow_store.delete(guild_id, user_id, FLOW_TYPE)
            return

        grant = await self._apply_grant(guild, member, flow)

        if grant is None:
            await interaction.followup.send(
                "Grant application failed. Check bot permissions and try again via `/boost admin start`.",
                ephemeral=True,
            )
            flow.state = S.PENDING
            self.flow_store.save(flow)
            return

        self.boost_store.save_grant(grant)
        self.flow_store.delete(guild_id, user_id, FLOW_TYPE)

        if user is not None:
            msg = f"✅ Your boost perks for **{guild_name}** have been applied!"
            if comment:
                msg += f"\n\n**Admin note:** {comment}"
            await safe_dm(user, content=msg)

        await interaction.followup.send("Grant applied successfully!", ephemeral=True)

    # ========================================================
    # Grant application
    # ========================================================

    async def _apply_grant(
        self,
        guild: discord.Guild,
        member: discord.Member,
        flow: DmFlow,
    ) -> BoostGrant | None:
        """
        Creates the custom role, applies its icon and color, uploads emojis,
        and assigns the role to the member.

        Returns a populated BoostGrant on success, None if role creation fails.
        Role icon and emoji failures are non-fatal and are logged.
        """
        data = flow.data
        tier = data.get("tier", 1)
        grant = BoostGrant(guild_id=guild.id, user_id=member.id, tier=tier)

        # -- Create role --
        role_color = discord.Color.default()
        if tier == 2 and "role_color_hex" in data:
            try:
                role_color = discord.Color(int(data["role_color_hex"].lstrip("#"), 16))
            except ValueError:
                pass

        try:
            role_name = str(data.get("role_name", "Booster"))[:_ROLE_NAME_MAX_LENGTH]
            role = await guild.create_role(
                name=role_name,
                color=role_color,
                reason=f"Boost perk for {member} (tier {tier})",
            )
            grant.role_id = role.id
            log(f"BOOST // ROLE CREATED 『 {role.id} 』 GUILD 『 {guild.id} 』", level="debug", logger_name="boost")
        except (discord.Forbidden, discord.HTTPException) as e:
            log(f"BOOST // ROLE CREATE FAILED 『 {e} 』 GUILD 『 {guild.id} 』", level="debug", logger_name="boost")
            return None

        # -- Set role icon (requires Guild Boost Level 2+) --
        if "role_image_b64" in data and guild.premium_tier >= 2:
            try:
                await role.edit(
                    icon=base64.b64decode(data["role_image_b64"]),
                    reason="Boost perk role icon",
                )
                log(f"BOOST // ROLE ICON SET 『 {role.id} 』", level="debug", logger_name="boost")
            except (discord.Forbidden, discord.HTTPException) as e:
                log(f"BOOST // ROLE ICON FAILED 『 {e} 』 — continuing without icon", level="debug", logger_name="boost")

        # -- Assign role --
        try:
            await member.add_roles(role, reason="Boost perk role assignment")
        except (discord.Forbidden, discord.HTTPException) as e:
            log(f"BOOST // ROLE ASSIGN FAILED 『 {e} 』 GUILD 『 {guild.id} 』", level="debug", logger_name="boost")

        # -- Add emojis --
        if "emoji_1_b64" in data and "emoji_1_name" in data:
            emoji = await self._create_emoji(guild, data["emoji_1_name"], data["emoji_1_b64"])
            if emoji:
                grant.emoji_1_id = emoji.id

        if tier == 2 and "emoji_2_b64" in data and "emoji_2_name" in data:
            emoji = await self._create_emoji(guild, data["emoji_2_name"], data["emoji_2_b64"])
            if emoji:
                grant.emoji_2_id = emoji.id

        return grant

    async def _create_emoji(
        self,
        guild: discord.Guild,
        name: str,
        image_b64: str,
    ) -> discord.Emoji | None:
        try:
            emoji = await guild.create_custom_emoji(
                name=name,
                image=base64.b64decode(image_b64),
                reason="Boost perk emoji",
            )
            log(f"BOOST // EMOJI CREATED :{name}: GUILD 『 {guild.id} 』", level="debug", logger_name="boost")
            return emoji
        except (discord.Forbidden, discord.HTTPException) as e:
            log(f"BOOST // EMOJI FAILED :{name}: GUILD 『 {guild.id} 』 // {e}", level="debug", logger_name="boost")
            return None

    # ========================================================
    # Grant teardown
    # ========================================================

    async def _teardown_grant(self, guild: discord.Guild, grant: BoostGrant) -> None:
        """Removes the custom role and emojis associated with a boost grant."""
        if grant.role_id is not None:
            role = guild.get_role(grant.role_id)
            if role is not None:
                try:
                    await role.delete(reason="Boost perk removed — member stopped boosting")
                    log(f"BOOST // ROLE DELETED 『 {grant.role_id} 』 GUILD 『 {guild.id} 』", level="debug", logger_name="boost")
                except (discord.Forbidden, discord.HTTPException) as e:
                    log(f"BOOST // ROLE DELETE FAILED 『 {e} 』", level="debug", logger_name="boost")

        for emoji_id in (grant.emoji_1_id, grant.emoji_2_id):
            if emoji_id is None:
                continue
            emoji = discord.utils.get(guild.emojis, id=emoji_id)
            if emoji is not None:
                try:
                    await emoji.delete(reason="Boost perk removed — member stopped boosting")
                    log(f"BOOST // EMOJI DELETED 『 {emoji_id} 』 GUILD 『 {guild.id} 』", level="debug", logger_name="boost")
                except (discord.Forbidden, discord.HTTPException) as e:
                    log(f"BOOST // EMOJI DELETE FAILED 『 {e} 』", level="debug", logger_name="boost")

        self.boost_store.delete_grant(guild.id, grant.user_id)

    # ========================================================
    # Review preview attachments
    # ========================================================

    def _build_preview_payload(
        self,
        flow: DmFlow,
        embed: discord.Embed,
    ) -> tuple[list[discord.Embed], list[discord.File]]:
        """
        Adds collected role/emoji image previews to review embeds.

        The returned files are fresh objects and must only be sent once.
        """
        data = flow.data
        embeds = [embed]
        files: list[discord.File] = []
        preview_color = self._flow_accent_color(flow, embed.color)
        role_preview_color = preview_color
        role_description = f"Role Name: {data.get('role_name', 'Role')}\n"
        if data.get("tier") == 2 and "role_color_hex" in data:
            hex_color = data["role_color_hex"]
            role_description += f"Role Color: `{hex_color}`\n"
            try:
                role_preview_color = discord.Color(int(hex_color.lstrip("#"), 16))
            except ValueError:
                role_preview_color = preview_color
        role_description += "Role Icon:"

        previews = [
            (
                "Role Preview",
                role_description,
                "role_image",
                "role_icon",
                role_preview_color,
            ),
            (
                "Emoji 1 Preview",
                f"Emoji Name: :{data['emoji_1_name']}:\nEmoji Image:" if "emoji_1_name" in data else "Emoji Image:",
                "emoji_1",
                "emoji_1",
                preview_color,
            ),
            (
                "Emoji 2 Preview",
                f"Emoji Name: :{data['emoji_2_name']}:\nEmoji Image:" if "emoji_2_name" in data else "Emoji Image:",
                "emoji_2",
                "emoji_2",
                preview_color,
            ),
        ]

        for title, description, data_prefix, filename_stem, preview_color in previews:
            image_file = self._image_file_from_flow(data, data_prefix, filename_stem)
            if image_file is None:
                continue

            preview_embed = discord.Embed(
                title=title,
                description=escape_untrusted_text(description),
                color=preview_color,
            )
            preview_embed.set_image(url=f"attachment://{image_file.filename}")
            embeds.append(preview_embed)
            files.append(image_file)

        return embeds, files

    @staticmethod
    def _flow_accent_color(flow: DmFlow, fallback: discord.Color) -> discord.Color:
        if flow.data.get("tier") != 2:
            return fallback
        hex_color = flow.data.get("role_color_hex")
        if not isinstance(hex_color, str):
            return fallback
        try:
            return discord.Color(int(hex_color.lstrip("#"), 16))
        except ValueError:
            return fallback

    @staticmethod
    def _tier_perks_text(tier: int) -> str:
        if tier == 2:
            return (
                "**Custom, colored role with an icon of your favorite character**\n"
                "• Icon cannot be a spoiler\n"
                "• Color cannot clash with Admin/Mod role color\n\n"
                "**Emote suggestion**\n"
                "• You may suggest 2 sciadv-related emotes, each with moderator approval\n"
                "• If your boosting lapses, your emoji will be removed\n"
            )

        return (
            "**Custom role with an icon of your favorite character**\n"
            "• Icon cannot be a spoiler\n\n"
            "**Emote suggestion**\n"
            "• You may suggest 1 sciadv-related emote, with moderator approval\n"
            "• If your boosting lapses, your emoji will be removed\n"
        )

    @staticmethod
    def _image_file_from_flow(
        data: dict,
        data_prefix: str,
        filename_stem: str,
    ) -> discord.File | None:
        image_b64 = data.get(f"{data_prefix}_b64")
        if not image_b64:
            return None

        ext = data.get(f"{data_prefix}_ext") or ".png"
        if not isinstance(ext, str) or not ext.startswith("."):
            ext = ".png"

        try:
            image = base64.b64decode(image_b64)
        except (TypeError, ValueError):
            return None

        image, ext = Boost._pad_preview_image(image, ext)

        return discord.File(
            fp=io.BytesIO(image),
            filename=f"{filename_stem}{ext}",
        )

    @staticmethod
    def _pad_preview_image(image: bytes, ext: str) -> tuple[bytes, str]:
        try:
            from PIL import Image, ImageSequence
        except ImportError:
            return image, ext

        try:
            with Image.open(io.BytesIO(image)) as source:
                if getattr(source, "is_animated", False):
                    frames = []
                    durations = []
                    for frame in ImageSequence.Iterator(source):
                        durations.append(frame.info.get("duration", source.info.get("duration", 100)))
                        frames.append(Boost._pad_preview_frame(frame.convert("RGBA"), Image))
                    if not frames:
                        return image, ext
                    output = io.BytesIO()
                    frames[0].save(
                        output,
                        format="GIF",
                        save_all=True,
                        append_images=frames[1:],
                        duration=durations,
                        loop=source.info.get("loop", 0),
                        disposal=2,
                    )
                    return output.getvalue(), ".gif"

                output = io.BytesIO()
                Boost._pad_preview_frame(source.convert("RGBA"), Image).save(output, format="PNG")
                return output.getvalue(), ".png"
        except Exception as e:
            log(f"BOOST // PREVIEW PAD FAILED 『 {e} 』", level="debug", logger_name="boost")
            return image, ext

    @staticmethod
    def _pad_preview_frame(frame, image_module):
        width, height = frame.size
        canvas_width = max(width, _PREVIEW_MIN_WIDTH)
        if canvas_width == width:
            return frame

        canvas = image_module.new("RGBA", (canvas_width, height), (0, 0, 0, 0))
        canvas.alpha_composite(frame, ((canvas_width - width) // 2, 0))
        return canvas

    # ========================================================
    # Approval embed builder
    # ========================================================

    def _build_approval_embed(self, flow: DmFlow, guild: discord.Guild | None) -> discord.Embed:
        data = flow.data
        tier = data.get("tier", 1)
        guild_name = guild.name if guild else f"Guild {flow.guild_id}"

        embed = discord.Embed(
            title=f"🔔 Boost Perks Request — Tier {tier}",
            description=(
                f"A member of **{guild_name}** has submitted their boost perk preferences.\n"
                "Review the details and approve or deny."
            ),
            color=self._flow_accent_color(flow, discord.Color.from_rgb(255, 115, 250)),
        )
        embed.add_field(name="Member", value=f"<@{flow.user_id}> (`{flow.user_id}`)", inline=False)

        if guild and guild.premium_tier < 2 and "role_image_b64" in data:
            embed.add_field(
                name="⚠ Role Icon",
                value="Role icons require Guild Boost Level 2. The image was saved and will be applied if the server reaches that level.",
                inline=False,
            )

        embed.set_footer(text=f"Guild ID: {flow.guild_id} | User ID: {flow.user_id}")
        return embed

    # ========================================================
    # Step prompt helper
    # ========================================================

    def _build_prompt_embed(self, flow: DmFlow) -> discord.Embed:
        """Returns the embed appropriate for the flow's current state."""
        tier = flow.data.get("tier", 1)
        emoji_skipped = flow.data.get("emoji_skipped", False)

        # Compute step X/Y for the title.
        order_t1 = [S.ROLE_NAME, S.ROLE_IMAGE, S.EMOJI_1_NAME, S.EMOJI_1_IMAGE]
        order_t2 = [S.ROLE_NAME, S.ROLE_IMAGE, S.ROLE_COLOR, S.EMOJI_1_NAME, S.EMOJI_1_IMAGE, S.EMOJI_2_NAME, S.EMOJI_2_IMAGE]
        order = order_t2 if tier == 2 else order_t1
        if emoji_skipped:
            order = [s for s in order if "emoji" not in s]

        try:
            step_num = order.index(flow.state) + 1
        except ValueError:
            step_num = 0
        total = len(order)

        def title(label: str) -> str:
            return f"Step {step_num}/{total} — {label}" if step_num else label

        state = flow.state
        c = self._flow_accent_color(flow, discord.Color.blurple())
        footer = "Type 'restart' to start over, or 'cancel' to stop."

        if state == S.ROLE_NAME:
            e = discord.Embed(
                title=title("Role Name"),
                description=(
                    "What would you like your custom role to be called?\n\n"
                    f"• {_ROLE_NAME_MAX_LENGTH} characters max\n"
                    "Just type the name and send it."
                ),
                color=c,
            )
        elif state == S.ROLE_IMAGE:
            e = discord.Embed(title=title("Role Icon"), description="Upload an image for your role icon.\n\n• PNG recommended, square images look best\n• Accepted: PNG, JPEG, or WebP\n• Recommended: 64×64 px\n• *(Role icons require the server to be at Boost Level 2 — your image is saved and applied when eligible)*", color=c)
        elif state == S.ROLE_COLOR:
            e = discord.Embed(title=title("Role Color"), description="What color should your role be?\n\nSend a hex color code — e.g. `#FF5733` or `A3C2FF`.", color=c)
        elif state == S.EMOJI_1_NAME:
            e = discord.Embed(title=title("Emoji 1 — Name"), description="What should your emoji be called?\n\n• 2–32 characters\n• Letters, numbers, and underscores only\n• Example: `my_emoji`", color=c)
        elif state == S.EMOJI_1_IMAGE:
            e = discord.Embed(title=title("Emoji 1 — Image"), description="Upload the image for your emoji.\n\n• PNG, JPEG, GIF, or WebP\n• Max **256 KB**\n• Recommended: 128×128 px", color=c)
        elif state == S.EMOJI_2_NAME:
            e = discord.Embed(title=title("Emoji 2 — Name"), description="What should your second emoji be called?\n\n• 2–32 characters\n• Letters, numbers, and underscores only", color=c)
        elif state == S.EMOJI_2_IMAGE:
            e = discord.Embed(title=title("Emoji 2 — Image"), description="Upload the image for your second emoji.\n\n• PNG, JPEG, GIF, or WebP\n• Max **256 KB**\n• Recommended: 128×128 px", color=c)
        else:
            return discord.Embed(title="Unknown step", color=c)

        e.set_footer(text=footer)
        return e

    async def _send_prompt(self, user: discord.User | discord.Member, flow: DmFlow) -> None:
        """Sends the prompt embed for the current flow step."""
        await safe_dm(user, embed=self._build_prompt_embed(flow))

    # ========================================================
    # Timeout task
    # ========================================================

    @tasks.loop(hours=1)
    async def _expire_flows(self) -> None:
        """Clears flows that have been idle for more than 48 hours."""
        expired = self.flow_store.get_expired(
            FLOW_TYPE, FLOW_TIMEOUT_SECONDS, exclude_state=S.PENDING
        )
        for flow in expired:
            self.flow_store.delete(flow.guild_id, flow.user_id, FLOW_TYPE)
            log(
                f"BOOST // FLOW EXPIRED 『 USER {flow.user_id} 』 GUILD 『 {flow.guild_id} 』",
                level="debug",
                logger_name="boost",
            )
            guild = self.bot.get_guild(flow.guild_id)
            guild_name = guild.name if guild else "the server"
            try:
                user = await self.bot.fetch_user(flow.user_id)
                await safe_dm(
                    user,
                    content=(
                        f"Your boost perks request for **{guild_name}** expired after 48 hours of inactivity.\n"
                        "Use `/boost status` in the server to start a new one."
                    ),
                )
            except (discord.Forbidden, discord.HTTPException) as e:
                log(
                    f"BOOST // EXPIRY DM FAILED 『 USER {flow.user_id} 』 // {e}",
                    level="debug",
                    logger_name="boost",
                )

    @_expire_flows.before_loop
    async def _before_expire(self):
        await self.bot.wait_until_ready()

    # ========================================================
    # Utility helpers
    # ========================================================

    @staticmethod
    def _first_image_attachment(message: discord.Message) -> discord.Attachment | None:
        for att in message.attachments:
            if att.content_type and att.content_type.startswith("image/"):
                return att
        return None

    async def _download_attachment(
        self,
        user: discord.User | discord.Member,
        attachment: discord.Attachment,
        max_size: int,
    ) -> bytes | None:
        if attachment.size > max_size:
            await safe_dm(
                user,
                content=f"That file is too large ({attachment.size // 1024} KB). Max allowed: {max_size // 1024} KB.",
            )
            return None
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(attachment.url) as resp:
                    if resp.status != 200:
                        await safe_dm(user, content="Could not download the image. Please try uploading again.")
                        return None
                    return await resp.read()
        except Exception as e:
            log(f"BOOST // DOWNLOAD ERROR 『 {e} 』", level="debug", logger_name="boost")
            await safe_dm(user, content="Something went wrong downloading the image. Please try again.")
            return None

async def setup(bot: commands.Bot) -> None:
    cog = Boost(bot)
    await bot.add_cog(cog)

    # Register the approval callback so the generic approval system can dispatch to us.
    register_approval_callback(FLOW_TYPE, cog._handle_approval)

    # Register DynamicItems so approval buttons and confirmation buttons survive restarts.
    add_dynamic_items(bot)
    bot.add_dynamic_items(_ConfirmButton, _RestartButton)
