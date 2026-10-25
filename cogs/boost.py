"""
Boost perks cog.

Listens for server boosts and sends the booster a DM dashboard. Buttons open
modals that collect custom role and emoji preferences. The completed request
is sent to the admin alert channel for review.

Flow states (stored in dm_flow_state.state)
-------------------------------------------
COLLECTING → PENDING

Every perk is optional: the member selects any subset of the items their
boost tier unlocks, then presses **Submit for Review**. Unselected perks are
omitted from the member confirmation and the admin approval entirely.

Data keys in dm_flow_state.data (JSON)
---------------------------------------
tier              int         1 or 2
role_name         str         (present only when a role is requested)
role_image_b64    str         base64-encoded bytes
role_color_hex    str         "#RRGGBB" (tier 2 only)
emoji_1_name      str         (present only when emoji 1 is requested)
emoji_1_b64       str         base64-encoded bytes
emoji_2_name      str         (tier 2 only)
emoji_2_b64       str         (tier 2 only)
"""
import base64
import binascii
import io
import re
import secrets

import discord
from discord.ext import commands, tasks

from amadeus.approval import (
    ApprovalResult,
    add_dynamic_items,
    post_approval_request,
    register_approval_callback,
    unregister_approval_callback,
)
from amadeus.alerts import send_alert
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
_ROLE_ICON_MAX_BYTES = 256 * 1024  # Discord role icon upload limit
_ROLE_NAME_MAX_LENGTH = 32
_DISCORD_ROLE_LIMIT = 250
# Discord may arrange narrow preview embeds into a horizontal grid. Give each
# transparent preview canvas the practical full embed width so role and emoji
# previews remain stacked in one column on standard clients.
_PREVIEW_MIN_WIDTH = 520
# Discord embeds have no markdown horizontal rule, so use a box-drawing line.
_EMBED_DIVIDER = "━" * 32


# ============================================================
# Flow state name constants
# ============================================================

class S:
    COLLECTING   = "collecting_perks"
    PENDING      = "pending_approval"
    PROCESSING   = "processing_approval"
    DENIED       = "denied_awaiting_restart"


# ============================================================
# Persistent confirmation buttons (DynamicItem)
#
# custom_id: amadeus_boost:{action}:{guild_id}:{user_id}
# ============================================================

_CONFIRM_RE = re.compile(r"amadeus_boost:confirm:(?P<guild_id>\d+):(?P<user_id>\d+)")
_PERK_RE = re.compile(
    r"amadeus_boost:perk:(?P<perk>role|emoji1|emoji2):(?P<guild_id>\d+):(?P<user_id>\d+)"
)
_CLEAR_RE = re.compile(r"amadeus_boost:clear:(?P<guild_id>\d+):(?P<user_id>\d+)")


class _ConfirmButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"amadeus_boost:confirm:\d+:\d+",
):
    def __init__(self, guild_id: int, user_id: int, *, row: int | None = None) -> None:
        super().__init__(
            discord.ui.Button(
                label="Submit for Review",
                style=discord.ButtonStyle.success,
                custom_id=f"amadeus_boost:confirm:{guild_id}:{user_id}",
            ),
            row=row,
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


class _PerkButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"amadeus_boost:perk:(?:role|emoji1|emoji2):\d+:\d+",
):
    LABELS = {
        "role": "Custom Role",
        "emoji1": "Emoji 1",
        "emoji2": "Emoji 2",
    }

    def __init__(
        self,
        perk: str,
        guild_id: int,
        user_id: int,
        *,
        completed: bool = False,
        row: int | None = None,
    ) -> None:
        label = self.LABELS[perk]
        if completed:
            label = f"✓ {label}"
        super().__init__(
            discord.ui.Button(
                label=label,
                style=discord.ButtonStyle.success if completed else discord.ButtonStyle.primary,
                custom_id=f"amadeus_boost:perk:{perk}:{guild_id}:{user_id}",
            ),
            row=row,
        )
        self.perk = perk
        self.guild_id = guild_id
        self.user_id = user_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        matched = _PERK_RE.fullmatch(item.custom_id)
        return cls(
            matched.group("perk"),
            int(matched.group("guild_id")),
            int(matched.group("user_id")),
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        cog: Boost | None = interaction.client.get_cog("Boost")
        if cog is None:
            await interaction.response.send_message(
                "The boost module is currently unavailable. Please try again later.",
                ephemeral=True,
            )
            return
        await cog.handle_perk_button(
            interaction,
            self.guild_id,
            self.user_id,
            self.perk,
        )


class _ClearButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"amadeus_boost:clear:\d+:\d+",
):
    def __init__(self, guild_id: int, user_id: int, *, row: int | None = None) -> None:
        super().__init__(
            discord.ui.Button(
                label="Clear",
                style=discord.ButtonStyle.danger,
                custom_id=f"amadeus_boost:clear:{guild_id}:{user_id}",
            ),
            row=row,
        )
        self.guild_id = guild_id
        self.user_id = user_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        m = _CLEAR_RE.match(item.custom_id)
        return cls(int(m.group("guild_id")), int(m.group("user_id")))

    async def callback(self, interaction: discord.Interaction) -> None:
        cog: Boost | None = interaction.client.get_cog("Boost")
        if cog is None:
            await interaction.response.send_message(
                "The boost module is currently unavailable. Please try again later.",
                ephemeral=True,
            )
            return
        await cog.handle_clear(interaction, self.guild_id, self.user_id)


class _RoleModal(discord.ui.Modal):
    def __init__(self, cog: "Boost", flow: DmFlow) -> None:
        super().__init__(title="Set Up Your Custom Role", timeout=15 * 60)
        self.cog = cog
        self.guild_id = flow.guild_id
        self.user_id = flow.user_id
        self.tier = int(flow.data.get("tier", 1))

        self.role_name = discord.ui.TextInput(
            custom_id="role_name",
            placeholder="Name Subject to Change",
            default=flow.data.get("role_name"),
            max_length=_ROLE_NAME_MAX_LENGTH,
        )
        self.role_icon = discord.ui.FileUpload(
            custom_id="role_icon",
            required=True,
            min_values=1,
            max_values=1,
        )

        self.add_item(discord.ui.TextDisplay("Please enter your custom role details below."))
        self.add_item(discord.ui.Label(text="Custom Role", component=self.role_name))
        if self.tier == 2:
            self.role_color = discord.ui.TextInput(
                custom_id="role_color",
                placeholder="FFFFFF",
                default=str(flow.data.get("role_color_hex", "")).lstrip("#") or None,
                min_length=6,
                max_length=6,
            )
            self.add_item(discord.ui.Label(text="Custom Role Color", component=self.role_color))
        else:
            self.role_color = None
        self.add_item(
            discord.ui.Label(
                text="Role Icon",
                description="PNG, JPEG, or WebP; max 256 KB; square 64×64 recommended.",
                component=self.role_icon,
            )
        )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.handle_role_modal(interaction, self)


class _EmojiModal(discord.ui.Modal):
    def __init__(self, cog: "Boost", flow: DmFlow, index: int) -> None:
        super().__init__(title=f"Set Up Emoji {index}", timeout=15 * 60)
        self.cog = cog
        self.guild_id = flow.guild_id
        self.user_id = flow.user_id
        self.index = index
        prefix = f"emoji_{index}"

        self.emoji_name = discord.ui.TextInput(
            custom_id=f"{prefix}_name",
            placeholder="my_emoji",
            default=flow.data.get(f"{prefix}_name"),
            min_length=2,
            max_length=32,
        )
        self.emoji_image = discord.ui.FileUpload(
            custom_id=f"{prefix}_image",
            required=True,
            min_values=1,
            max_values=1,
        )

        self.add_item(
            discord.ui.TextDisplay(f"What would you like your custom emoji {index} to be named?")
        )
        self.add_item(
            discord.ui.Label(
                text=f"Emoji {index} Name",
                description="2–32 letters, numbers, or underscores.",
                component=self.emoji_name,
            )
        )
        self.add_item(
            discord.ui.Label(
                text=f"Emoji {index} Image",
                description="PNG, JPEG, GIF, or WebP; maximum 256 KB.",
                component=self.emoji_image,
            )
        )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.handle_emoji_modal(interaction, self)


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

        removed = await self._teardown_grant(guild, grant)

        if removed:
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
    ) -> bool:
        """Creates a fresh flow and sends the modal setup dashboard."""
        data: dict = {"tier": tier}
        if forced:
            data["forced"] = True

        flow = DmFlow(
            guild_id=guild.id,
            user_id=member.id,
            flow_type=FLOW_TYPE,
            state=S.COLLECTING,
            data=data,
        )
        self.flow_store.save(flow)
        sent = await self._send_prompt(member, flow)
        if not sent:
            await send_alert(
                self.bot,
                self.module_store,
                guild.id,
                f"⚠ **Boost setup DM failed for <@{member.id}>** (`{member.id}`). "
                "The flow was saved, but an admin must contact the member and ask them to run `/boost status`.",
            )
        return sent

    def _request_capacity_error(self, guild: discord.Guild, data: dict) -> str | None:
        """
        Checks that the guild can create only the resources this request asks for.

        Perks are optional, so a request that skips the role is not blocked by the
        role limit, and an emoji-only request is not blocked by the role-icon
        Boost Level 2 requirement.
        """
        if data.get("role_name") and len(guild.roles) >= _DISCORD_ROLE_LIMIT:
            return f"The server has reached Discord's {_DISCORD_ROLE_LIMIT}-role limit."

        emoji_count = sum(1 for index in (1, 2) if data.get(f"emoji_{index}_b64"))
        if emoji_count:
            sufficient, available, capacity = check_emoji_slots(guild, emoji_count)
            if not sufficient:
                return (
                    f"The server needs {emoji_count} free emoji slot(s), but only has "
                    f"{available} of {capacity} available."
                )

        if data.get("role_image_b64") and guild.premium_tier < 2:
            return "The server is not eligible to apply custom role icons (Boost Level 2 is required)."
        return None

    async def _reset_flow(
        self, guild_id: int, user_id: int, tier: int, forced: bool = False
    ) -> DmFlow | None:
        """Resets the flow to the first step, preserving the tier."""
        data: dict = {"tier": tier}
        if forced:
            data["forced"] = True

        flow = DmFlow(
            guild_id=guild_id,
            user_id=user_id,
            flow_type=FLOW_TYPE,
            state=S.COLLECTING,
            data=data,
        )
        self.flow_store.save(flow)
        return flow

    @staticmethod
    def _role_complete(flow: DmFlow) -> bool:
        data = flow.data
        required = ("role_name", "role_image_b64")
        if int(data.get("tier", 1)) == 2:
            required += ("role_color_hex",)
        return all(data.get(key) for key in required)

    @staticmethod
    def _emoji_complete(flow: DmFlow, index: int) -> bool:
        return bool(
            flow.data.get(f"emoji_{index}_name")
            and flow.data.get(f"emoji_{index}_b64")
        )

    @classmethod
    def _has_selection(cls, flow: DmFlow) -> bool:
        """True when at least one perk has been fully filled in."""
        if cls._role_complete(flow):
            return True
        tier = int(flow.data.get("tier", 1))
        return any(cls._emoji_complete(flow, index) for index in range(1, tier + 1))

    @classmethod
    def _selection_valid(cls, flow: DmFlow) -> bool:
        """
        True when at least one perk is selected and every started perk is complete.

        Perks are optional, but a perk that was begun (for example a role name
        without an icon) cannot be submitted half-finished.
        """
        data = flow.data
        role_started = bool(data.get("role_name") or data.get("role_image_b64"))
        if role_started and not cls._role_complete(flow):
            return False

        tier = int(data.get("tier", 1))
        for index in range(1, tier + 1):
            name = data.get(f"emoji_{index}_name")
            image = data.get(f"emoji_{index}_b64")
            if bool(name) != bool(image):
                return False

        return cls._has_selection(flow)

    @classmethod
    def _selected_items_text(cls, flow: DmFlow) -> list[str]:
        """Human-readable summary of only the perks the member actually requested."""
        data = flow.data
        items: list[str] = []
        if cls._role_complete(flow):
            line = f"**Custom Role:** {escape_untrusted_text(str(data.get('role_name')))}"
            if int(data.get("tier", 1)) == 2 and data.get("role_color_hex"):
                line += f" (`{data['role_color_hex']}`)"
            items.append(line)
        tier = int(data.get("tier", 1))
        for index in range(1, tier + 1):
            if cls._emoji_complete(flow, index):
                name = escape_untrusted_text(str(data.get(f"emoji_{index}_name")))
                items.append(f"**Emoji {index}:** {name}")
        return items

    def _build_setup_embed(self, flow: DmFlow) -> discord.Embed:
        guild = self.bot.get_guild(flow.guild_id)
        guild_name = guild.name if guild else f"Server {flow.guild_id}"
        tier = int(flow.data.get("tier", 1))
        embed = discord.Embed(
            title=f"🎉 Thanks for boosting {guild_name}!",
            description=(
                f"You've added **{tier} boost{'s' if tier > 1 else ''}** and unlocked perks!\n\n"
                f"{self._tier_perks_text(tier)}\n"
                "Every perk is optional. Set up only the ones you want, or ignore this message all together.\n\n"
                "Any entry left blank is skipped, and completed entries can redone by clicking them.\n"
                "Press **Submit for Review** when you're ready, or press **Clear** to start over. "
                f"{_EMBED_DIVIDER}"
            ),
            color=self._flow_accent_color(flow, discord.Color.from_rgb(255, 115, 250)),
        )

        statuses = [
            f"{'✅' if self._role_complete(flow) else '⬜'} Custom role",
        ]
        for index in range(1, tier + 1):
            statuses.append(
                f"{'✅' if self._emoji_complete(flow, index) else '⬜'} Emoji {index}"
            )
        embed.add_field(name="Setup Progress", value="\n".join(statuses), inline=False)
        embed.set_footer(
            text="Type 'restart' to clear your choices or 'cancel' to stop. "
            f"You can also use /boost status in {guild_name}."
        )
        return embed

    def _build_setup_view(self, flow: DmFlow) -> discord.ui.View:
        tier = int(flow.data.get("tier", 1))
        view = discord.ui.View(timeout=None)
        # Row 0: one button per unlocked perk.
        view.add_item(
            _PerkButton(
                "role",
                flow.guild_id,
                flow.user_id,
                completed=self._role_complete(flow),
                row=0,
            )
        )
        for index in range(1, tier + 1):
            view.add_item(
                _PerkButton(
                    f"emoji{index}",
                    flow.guild_id,
                    flow.user_id,
                    completed=self._emoji_complete(flow, index),
                    row=0,
                )
            )
        # Row 1: submit, plus a clear button to wipe the current choices.
        view.add_item(_ConfirmButton(flow.guild_id, flow.user_id, row=1))
        view.add_item(_ClearButton(flow.guild_id, flow.user_id, row=1))
        return view

    async def _retire_dashboard(self, interaction: discord.Interaction) -> None:
        """
        Remove the buttons from a setup dashboard that can no longer be used.

        Without this, a stale dashboard keeps its controls and a member pressing
        one just gets a confusing "no longer active" reply.
        """
        message = getattr(interaction, "message", None)
        if message is None:
            return
        try:
            await message.edit(view=None)
        except (discord.Forbidden, discord.HTTPException) as error:
            log(
                f"BOOST // DASHBOARD RETIRE FAILED 『 {error} 』",
                level="debug",
                logger_name="boost",
            )

    async def handle_perk_button(
        self,
        interaction: discord.Interaction,
        guild_id: int,
        user_id: int,
        perk: str,
    ) -> None:
        if interaction.user.id != user_id:
            await interaction.response.send_message("This button isn't for you.", ephemeral=True)
            return

        flow = self.flow_store.get(guild_id, user_id, FLOW_TYPE)
        if flow is None or flow.state != S.COLLECTING:
            await interaction.response.send_message(
                "This setup panel is no longer active. Use `/boost status` in the server to resume.",
                ephemeral=True,
            )
            await self._retire_dashboard(interaction)
            return

        tier = int(flow.data.get("tier", 1))
        if perk == "role":
            await interaction.response.send_modal(_RoleModal(self, flow))
            return
        if perk == "emoji1":
            await interaction.response.send_modal(_EmojiModal(self, flow, 1))
            return
        if perk == "emoji2" and tier == 2:
            await interaction.response.send_modal(_EmojiModal(self, flow, 2))
            return
        await interaction.response.send_message("That perk is not available for this request.", ephemeral=True)

    async def handle_clear(
        self,
        interaction: discord.Interaction,
        guild_id: int,
        user_id: int,
    ) -> None:
        if interaction.user.id != user_id:
            await interaction.response.send_message("This button isn't for you.", ephemeral=True)
            return

        flow = self.flow_store.get(guild_id, user_id, FLOW_TYPE)
        if flow is None or flow.state != S.COLLECTING:
            await interaction.response.send_message(
                "This setup panel is no longer active. Use `/boost status` in the server to resume.",
                ephemeral=True,
            )
            await self._retire_dashboard(interaction)
            return

        for key in (
            "role_name",
            "role_image_b64",
            "role_image_ext",
            "role_color_hex",
            "emoji_1_name",
            "emoji_1_b64",
            "emoji_1_ext",
            "emoji_2_name",
            "emoji_2_b64",
            "emoji_2_ext",
        ):
            flow.data.pop(key, None)

        flow.state = S.COLLECTING
        self.flow_store.save(flow)
        await interaction.response.edit_message(
            embed=self._build_setup_embed(flow),
            view=self._build_setup_view(flow),
        )

    async def handle_role_modal(
        self,
        interaction: discord.Interaction,
        modal: _RoleModal,
    ) -> None:
        if interaction.user.id != modal.user_id:
            await interaction.response.send_message("This modal isn't for you.", ephemeral=True)
            return

        role_name = modal.role_name.value.strip()
        if not role_name:
            await interaction.response.send_message("Enter a name for the custom role.", ephemeral=True)
            return
        role_name_error = validate_safe_role_name(role_name)
        if role_name_error is not None:
            await interaction.response.send_message(role_name_error, ephemeral=True)
            return

        role_color_hex = None
        if modal.role_color is not None:
            match = _HEX_COLOR_RE.fullmatch(modal.role_color.value.strip())
            if match is None:
                await interaction.response.send_message(
                    "Enter a six-character hex color such as `FF5733`.",
                    ephemeral=True,
                )
                return
            role_color_hex = f"#{match.group(1).upper()}"

        attachment = modal.role_icon.values[0] if modal.role_icon.values else None
        if attachment is None:
            await interaction.response.send_message("Upload an image for the role icon.", ephemeral=True)
            return
        validation_error = validate_discord_attachment_image(attachment, ROLE_ICON_EXTENSIONS)
        if validation_error is not None:
            await interaction.response.send_message(validation_error, ephemeral=True)
            return
        if attachment.size > _ROLE_ICON_MAX_BYTES:
            await interaction.response.send_message("Role icons must be 256 KB or smaller.", ephemeral=True)
            return

        await interaction.response.defer()
        image = await self._read_modal_attachment(interaction, attachment)
        if image is None:
            return
        if len(image) > _ROLE_ICON_MAX_BYTES:
            await interaction.followup.send("Role icons must be 256 KB or smaller.", ephemeral=True)
            return

        flow = self.flow_store.get(modal.guild_id, modal.user_id, FLOW_TYPE)
        if flow is None or flow.state != S.COLLECTING:
            await interaction.followup.send("This setup is no longer active.", ephemeral=True)
            return
        flow.data["role_name"] = role_name
        flow.data["role_image_b64"] = base64.b64encode(image).decode()
        flow.data["role_image_ext"] = image_extension_from_url(attachment.url) or ".png"
        if role_color_hex is not None:
            flow.data["role_color_hex"] = role_color_hex
        await self._finish_modal_submission(interaction, flow)

    async def handle_emoji_modal(
        self,
        interaction: discord.Interaction,
        modal: _EmojiModal,
    ) -> None:
        if interaction.user.id != modal.user_id:
            await interaction.response.send_message("This modal isn't for you.", ephemeral=True)
            return

        emoji_name = modal.emoji_name.value.strip()
        if not _EMOJI_NAME_RE.fullmatch(emoji_name):
            await interaction.response.send_message(
                "Emoji names must be 2–32 characters using only letters, numbers, and underscores.",
                ephemeral=True,
            )
            return

        attachment = modal.emoji_image.values[0] if modal.emoji_image.values else None
        if attachment is None:
            await interaction.response.send_message("Upload an image for the emoji.", ephemeral=True)
            return
        validation_error = validate_discord_attachment_image(attachment, EMOJI_IMAGE_EXTENSIONS)
        if validation_error is not None:
            await interaction.response.send_message(validation_error, ephemeral=True)
            return
        if attachment.size > _EMOJI_MAX_BYTES:
            await interaction.response.send_message(
                f"Emoji images must be {_EMOJI_MAX_BYTES // 1024} KB or smaller.",
                ephemeral=True,
            )
            return

        await interaction.response.defer()
        image = await self._read_modal_attachment(interaction, attachment)
        if image is None:
            return
        if len(image) > _EMOJI_MAX_BYTES:
            await interaction.followup.send(
                f"Emoji images must be {_EMOJI_MAX_BYTES // 1024} KB or smaller.",
                ephemeral=True,
            )
            return

        flow = self.flow_store.get(modal.guild_id, modal.user_id, FLOW_TYPE)
        if flow is None or flow.state != S.COLLECTING:
            await interaction.followup.send("This setup is no longer active.", ephemeral=True)
            return
        tier = int(flow.data.get("tier", 1))
        if modal.index > tier:
            await interaction.followup.send("That emoji perk is no longer available.", ephemeral=True)
            return
        prefix = f"emoji_{modal.index}"
        flow.data[f"{prefix}_name"] = emoji_name
        flow.data[f"{prefix}_b64"] = base64.b64encode(image).decode()
        flow.data[f"{prefix}_ext"] = image_extension_from_url(attachment.url) or ".png"
        await self._finish_modal_submission(interaction, flow)

    async def _read_modal_attachment(
        self,
        interaction: discord.Interaction,
        attachment: discord.Attachment,
    ) -> bytes | None:
        try:
            return await attachment.read()
        except discord.HTTPException as error:
            log(
                f"BOOST // MODAL FILE DOWNLOAD FAILED 『 USER {interaction.user.id} 』 // {error}",
                level="warning",
                logger_name="boost",
            )
            await interaction.followup.send(
                "I couldn't download that file. Please reopen the modal and try again.",
                ephemeral=True,
            )
            return None

    async def _finish_modal_submission(
        self,
        interaction: discord.Interaction,
        flow: DmFlow,
    ) -> None:
        self.flow_store.save(flow)
        try:
            await interaction.edit_original_response(
                embed=self._build_setup_embed(flow),
                view=self._build_setup_view(flow),
            )
        except discord.HTTPException as error:
            log(
                f"BOOST // SETUP DASHBOARD UPDATE FAILED 『 USER {flow.user_id} 』 // {error}",
                level="warning",
                logger_name="boost",
            )
            await interaction.followup.send(
                "I saved your selection, but couldn't update the setup dashboard. "
                "Use `/boost status` in the server to refresh it.",
                ephemeral=True,
            )
            return

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

            if forced:
                # Admin-forced setups are admin-managed: don't restart them. Drop
                # the leftover state so a fresh admin force is required.
                self.flow_store.delete(flow.guild_id, flow.user_id, FLOW_TYPE)
                await safe_dm(
                    message.author,
                    content=(
                        "This boost setup was started by an admin and can't be restarted here. "
                        "Ask an admin to start a new one."
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

        # Free-form messages can't fill the dashboard; point the member at it.
        if not any(f.state == S.COLLECTING for f in flows):
            return
        await safe_dm(
            message.author,
            content="Use the buttons in your boost setup dashboard to enter your role and emoji details.",
        )

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
    # Submission
    # ========================================================

    async def handle_confirm(
        self,
        interaction: discord.Interaction,
        guild_id: int,
        user_id: int,
    ) -> None:
        """Invoked when the member presses 'Submit for Review' on the dashboard."""
        if interaction.user.id != user_id:
            await interaction.response.send_message("This button isn't for you.", ephemeral=True)
            return

        flow = self.flow_store.get(guild_id, user_id, FLOW_TYPE)
        if flow is None or flow.state != S.COLLECTING:
            await interaction.response.send_message(
                "This session has expired. Use `/boost status` in the server to start a new one.",
                ephemeral=True,
            )
            await self._retire_dashboard(interaction)
            return

        if not self._selection_valid(flow):
            await interaction.response.send_message(
                "Select at least one perk before submitting. "
                "Any perk you've started must be finished first.",
                ephemeral=True,
            )
            return

        # Claim the flow synchronously, before the first await, so a double
        # click can't post the same request to the admins twice.
        flow.state = S.PROCESSING
        self.flow_store.save(flow)

        # Posting to the admin channel can take a moment, so acknowledge the
        # button up front and reply through the followup.
        await interaction.response.defer(ephemeral=True, thinking=True)

        # Re-verify the member is still boosting at submission time,
        # unless the flow was force-started by an admin.
        guild = self.bot.get_guild(guild_id)
        if guild is not None and not flow.data.get("forced"):
            try:
                member = await guild.fetch_member(user_id)
                if not is_active_booster(member):
                    self.flow_store.delete(guild_id, user_id, FLOW_TYPE)
                    await interaction.followup.send(
                        "It looks like you're no longer boosting — the request cannot be submitted.",
                        ephemeral=True,
                    )
                    await self._retire_dashboard(interaction)
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
            # Release the claim so the member can retry the submission.
            flow.state = S.COLLECTING
            self.flow_store.save(flow)
            await interaction.followup.send(
                "⚠ Could not reach the admin alert channel. Ask an admin to run `/amadeus boost start` to manually trigger the flow.",
                ephemeral=True,
            )
            return

        flow.state = S.PENDING
        self.flow_store.save(flow)

        # The dashboard is spent once submitted; drop its buttons so they can't
        # be pressed again and give a confusing "no longer active" reply.
        await self._retire_dashboard(interaction)

        await interaction.followup.send(
            "✅ Your request has been submitted for admin review. You'll receive a DM once a decision is made!",
            ephemeral=True,
        )

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
    ) -> ApprovalResult:
        flow = self.flow_store.get(guild_id, user_id, FLOW_TYPE)
        if flow is None:
            return ApprovalResult(False, "Flow not found — the user may have cancelled.")

        if flow.state != S.PENDING:
            return ApprovalResult(False, "This request has already been processed or is not pending.")

        if flow.data.get("approval_request_id") != request_id:
            return ApprovalResult(False, "This approval button is stale. Use the latest approval request.")

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
                if comment:
                    msg += f"\n\n**Reason:** {escape_untrusted_text(comment)}"
                msg += (
                    "\n\nPlease type 'boost' if you would like to start again. "
                    f"Alternatively, use `/boost status` in {guild_name} if you want to start again at a later time."
                )
                await safe_dm(user, content=msg)

            return ApprovalResult(True, "Decision recorded. The user can type `boost` to start again.")

        # ---- Approved ----
        if guild is None:
            flow.state = S.PENDING
            self.flow_store.save(flow)
            return ApprovalResult(False, "Guild not found.")

        try:
            member = await guild.fetch_member(user_id)
        except (discord.NotFound, discord.HTTPException):
            flow.state = S.PENDING
            self.flow_store.save(flow)
            return ApprovalResult(False, "Member is no longer in the server. The request was left pending for review.")

        if not flow.data.get("forced") and not is_active_booster(member):
            flow.state = S.PENDING
            self.flow_store.save(flow)
            return ApprovalResult(False, "Member is no longer boosting — perks were not applied.")

        capacity_error = self._request_capacity_error(guild, flow.data)
        if capacity_error is not None:
            flow.state = S.PENDING
            self.flow_store.save(flow)
            await send_alert(
                self.bot,
                self.module_store,
                guild_id,
                f"⚠ **Boost approval failed for <@{user_id}>** (`{user_id}`). {capacity_error}",
            )
            return ApprovalResult(False, f"Grant application cannot continue: {capacity_error}")

        grant = await self._apply_grant(guild, member, flow)

        if grant is None:
            await send_alert(
                self.bot,
                self.module_store,
                guild_id,
                f"⚠ **Boost approval failed for <@{user_id}>** (`{user_id}`). "
                "Discord rejected role or emoji creation. Check permissions, hierarchy, and capacity.",
            )
            flow.state = S.PENDING
            self.flow_store.save(flow)
            return ApprovalResult(
                False,
                "Grant application failed. The request remains pending; check permissions and try again.",
            )

        previous_grant = self.boost_store.get_grant(guild_id, user_id)
        cleanup_job_id = (
            self.boost_store.queue_cleanup(previous_grant) if previous_grant is not None else None
        )
        self.boost_store.save_grant(grant)
        self.flow_store.delete(guild_id, user_id, FLOW_TYPE)

        if previous_grant is not None and cleanup_job_id is not None:
            if await self._delete_grant_resources(guild, previous_grant):
                self.boost_store.delete_cleanup_job(cleanup_job_id)
            else:
                await send_alert(
                    self.bot,
                    self.module_store,
                    guild_id,
                    f"⚠ New boost perks were applied for <@{user_id}>, but one or more old "
                    "resources could not be removed. Cleanup will retry automatically.",
                )

        if user is not None:
            msg = f"✅ Your boost perks for **{guild_name}** have been applied!"
            if comment:
                msg += f"\n\n**Admin note:** {escape_untrusted_text(comment)}"
            await safe_dm(user, content=msg)

        return ApprovalResult(True, "Grant applied successfully!")

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
        Creates only the resources the member requested, applies the role icon
        and color when applicable, and assigns the role.

        Returns a populated BoostGrant once every requested resource exists. Any
        partial work is rolled back so the grant is all-or-nothing.
        """
        data = flow.data
        tier = data.get("tier", 1)
        if not self._selection_valid(flow):
            log(
                f"BOOST // INVALID FLOW REJECTED 『 USER {member.id} 』 GUILD 『 {guild.id} 』",
                level="warning",
                logger_name="boost",
            )
            return None
        grant = BoostGrant(guild_id=guild.id, user_id=member.id, tier=tier)

        # -- Create role (only if requested) --
        if self._role_complete(flow):
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
            if "role_image_b64" in data:
                if guild.premium_tier < 2:
                    # Normally caught by the item-aware capacity check; keep
                    # application atomic if eligibility changed in between.
                    await self._rollback_uncommitted_grant(guild, grant)
                    return None
                try:
                    await role.edit(
                        display_icon=base64.b64decode(data["role_image_b64"], validate=True),
                        reason="Boost perk role icon",
                    )
                    log(f"BOOST // ROLE ICON SET 『 {role.id} 』", level="debug", logger_name="boost")
                except (discord.Forbidden, discord.HTTPException, ValueError, binascii.Error) as e:
                    log(f"BOOST // ROLE ICON FAILED 『 {e} 』", level="debug", logger_name="boost")
                    await self._rollback_uncommitted_grant(guild, grant)
                    return None

            # -- Assign role --
            try:
                await member.add_roles(role, reason="Boost perk role assignment")
            except (discord.Forbidden, discord.HTTPException, ValueError, binascii.Error) as e:
                log(f"BOOST // ROLE ASSIGN FAILED 『 {e} 』 GUILD 『 {guild.id} 』", level="debug", logger_name="boost")
                await self._rollback_uncommitted_grant(guild, grant)
                return None

        # -- Add emojis (only the ones requested) --
        for index in (1, 2):
            if tier < 2 and index == 2:
                break
            if not self._emoji_complete(flow, index):
                continue
            emoji = await self._create_emoji(
                guild, data[f"emoji_{index}_name"], data[f"emoji_{index}_b64"]
            )
            if emoji:
                setattr(grant, f"emoji_{index}_id", emoji.id)
            else:
                await self._rollback_uncommitted_grant(guild, grant)
                return None

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
                image=base64.b64decode(image_b64, validate=True),
                reason="Boost perk emoji",
            )
            log(f"BOOST // EMOJI CREATED :{name}: GUILD 『 {guild.id} 』", level="debug", logger_name="boost")
            return emoji
        except (discord.Forbidden, discord.HTTPException, ValueError, binascii.Error) as e:
            log(f"BOOST // EMOJI FAILED :{name}: GUILD 『 {guild.id} 』 // {e}", level="debug", logger_name="boost")
            return None

    # ========================================================
    # Grant teardown
    # ========================================================

    async def _delete_grant_resources(self, guild: discord.Guild, grant: BoostGrant) -> bool:
        """Best-effort resource deletion; returns whether every resource is gone."""
        succeeded = True
        if grant.role_id is not None:
            role = guild.get_role(grant.role_id)
            if role is not None:
                try:
                    await role.delete(reason="Boost perk removed — member stopped boosting")
                    log(f"BOOST // ROLE DELETED 『 {grant.role_id} 』 GUILD 『 {guild.id} 』", level="debug", logger_name="boost")
                except (discord.Forbidden, discord.HTTPException) as e:
                    log(f"BOOST // ROLE DELETE FAILED 『 {e} 』", level="debug", logger_name="boost")
                    succeeded = False

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
                    succeeded = False

        return succeeded

    async def _rollback_uncommitted_grant(
        self, guild: discord.Guild, grant: BoostGrant
    ) -> None:
        if await self._delete_grant_resources(guild, grant):
            return
        self.boost_store.queue_cleanup(grant)
        await send_alert(
            self.bot,
            self.module_store,
            guild.id,
            f"⚠ **Boost rollback was incomplete for <@{grant.user_id}>** (`{grant.user_id}`). "
            "The remaining resources were queued for automatic cleanup.",
        )

    async def _teardown_grant(self, guild: discord.Guild, grant: BoostGrant) -> bool:
        """Remove a grant, retaining its database record when cleanup fails."""
        if not await self._delete_grant_resources(guild, grant):
            await send_alert(
                self.bot,
                self.module_store,
                guild.id,
                f"⚠ **Boost cleanup failed for <@{grant.user_id}>** (`{grant.user_id}`). "
                "The grant remains recorded so cleanup can be retried.",
            )
            return False
        self.boost_store.delete_grant(guild.id, grant.user_id)
        return True

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

        # Only the perks the member actually requested — skipped perks are not
        # shown so admins aren't asked to review options nobody picked.
        items = self._selected_items_text(flow)
        if items:
            embed.add_field(name="Requested Perks", value="\n".join(items), inline=False)

        embed.set_footer(text=f"Guild ID: {flow.guild_id} | User ID: {flow.user_id}")
        return embed

    # ========================================================
    # Prompt helper
    # ========================================================

    async def _send_prompt(self, user: discord.User | discord.Member, flow: DmFlow) -> bool:
        """Send the modal setup dashboard."""
        return await safe_dm(
            user,
            embed=self._build_setup_embed(flow),
            view=self._build_setup_view(flow),
        )

    # ========================================================
    # Timeout task
    # ========================================================

    @tasks.loop(hours=1)
    async def _expire_flows(self) -> None:
        """Clears flows that have been idle for more than 48 hours."""
        for job_id, grant in self.boost_store.get_cleanup_jobs():
            guild = self.bot.get_guild(grant.guild_id)
            if guild is None:
                continue
            if await self._delete_grant_resources(guild, grant):
                self.boost_store.delete_cleanup_job(job_id)
                log(
                    f"BOOST // RETRIED CLEANUP COMPLETE 『 JOB {job_id} 』 GUILD 『 {guild.id} 』",
                    level="debug",
                    logger_name="boost",
                )

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


async def setup(bot: commands.Bot) -> None:
    cog = Boost(bot)
    await bot.add_cog(cog)

    # Register the approval callback so the generic approval system can dispatch to us.
    register_approval_callback(FLOW_TYPE, cog._handle_approval)

    # Register DynamicItems so approval, setup, and clear buttons survive restarts.
    add_dynamic_items(bot)
    bot.add_dynamic_items(_ConfirmButton, _PerkButton, _ClearButton)
