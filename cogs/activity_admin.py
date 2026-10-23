import sqlite3

import discord
from discord import app_commands
from discord.ext import commands

from amadeus.alerts import send_alert
from amadeus.activity_store import ActivityStore
from amadeus.database import ConfigStore
from amadeus.discord_utils import check_role_hierarchy
from amadeus.logging_utils import log
from amadeus.module_guard import require_module_enabled_for_interaction
from amadeus.permissions import require_amadeus_access
from cogs.amadeus_admin import attach_amadeus_subgroup, detach_amadeus_subgroup


_LEADERBOARD_LIMIT = 30
_LEADERBOARD_PAGE_SIZE = 10


class _LeaderboardView(discord.ui.View):
    """Button pagination for a single 30-member leaderboard snapshot."""

    def __init__(
        self,
        cog: "ActivityAdmin",
        guild: discord.Guild,
        leaderboard: list[tuple[int, int]],
        *,
        monthly: bool,
    ) -> None:
        super().__init__(timeout=180)
        self.cog = cog
        self.guild = guild
        self.leaderboard = leaderboard
        self.monthly = monthly
        self.page = 0
        self.page_count = max(1, (len(leaderboard) + _LEADERBOARD_PAGE_SIZE - 1) // _LEADERBOARD_PAGE_SIZE)
        self._sync_buttons()

    def _sync_buttons(self) -> None:
        self.previous.disabled = self.page == 0
        self.next.disabled = self.page >= self.page_count - 1

    async def _show_page(self, interaction: discord.Interaction) -> None:
        self._sync_buttons()
        embed = self.cog._build_leaderboard_embed(
            self.guild,
            self.leaderboard,
            page=self.page,
            monthly=self.monthly,
        )
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="Previous", emoji="◀️", style=discord.ButtonStyle.secondary)
    async def previous(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        self.page = max(0, self.page - 1)
        await self._show_page(interaction)

    @discord.ui.button(label="Next", emoji="▶️", style=discord.ButtonStyle.secondary)
    async def next(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        self.page = min(self.page_count - 1, self.page + 1)
        await self._show_page(interaction)


class ActivityAdmin(commands.Cog):
    """
    Admin cog for the activity module.

    Commands:
      /activity status
      /activity leaderboard
      /amadeus activity tier-add, tier-remove, tier-list, role-swap
      /amadeus activity channel-include, channel-exclude, channel-remove, channel-list
      /amadeus activity cooldown
      /amadeus activity status
    """

    activity = app_commands.Group(
        name="activity",
        description="Activity role tracking commands.",
        guild_only=True,
    )

    activity_monthly = app_commands.Group(
        name="monthly",
        description="Current UTC calendar month activity commands.",
        parent=activity,
    )

    amadeus_activity = app_commands.Group(
        name="activity",
        description="Activity admin commands.",
    )

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.activity_store = ActivityStore()
        self.module_store = ConfigStore()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await require_module_enabled_for_interaction(
            interaction, self.module_store, "activity"
        )

    def cog_unload(self):
        detach_amadeus_subgroup(self.bot, self.amadeus_activity.name)
        self.activity_store.close()
        self.module_store.close()

    def _invalidate(self, guild_id: int) -> None:
        cog = self.bot.get_cog("Activity")
        if cog is not None:
            cog.invalidate_cache(guild_id)

    async def _remove_role_from_members(
        self,
        guild: discord.Guild,
        role: discord.Role,
        *,
        reason: str,
    ) -> tuple[int, int]:
        """Remove a retired tier role, returning (removed, failed)."""
        removed = 0
        failed = 0
        for member in list(role.members):
            try:
                await member.remove_roles(role, reason=reason)
                removed += 1
            except (discord.Forbidden, discord.HTTPException) as error:
                failed += 1
                log(
                    f"ACTIVITY // TIER CLEANUP FAILED 『 USER {member.id} 』 "
                    f"ROLE 『 {role.id} 』 GUILD 『 {guild.id} 』 // {error}",
                    level="warning",
                    logger_name="activity",
                )

        if failed:
            await send_alert(
                self.bot,
                self.module_store,
                guild.id,
                f"⚠ **Activity:** Failed to remove <@&{role.id}> from "
                f"**{failed}** member(s) while changing a tier. The tier configuration "
                "was kept so cleanup can be retried. Check the bot's role hierarchy "
                "and Manage Roles permission.",
            )
        return removed, failed

    @staticmethod
    def _format_role_use(scope: str, threshold: int) -> str:
        label = "monthly" if scope == "monthly" else "all-time"
        return f"the **{threshold}-message {label} tier**"

    def _build_status_embed(
        self,
        guild: discord.Guild,
        member: discord.Member,
        *,
        count: int,
        tiers: list[tuple[int, int]],
        title_prefix: str = "Activity Status",
        monthly: bool = False,
    ) -> discord.Embed:
        embed = discord.Embed(
            title=f"{title_prefix} — {member.display_name}",
            color=discord.Color.blurple(),
        )
        embed.add_field(
            name="Messages this month" if monthly else "Messages counted",
            value=f"**{count}**",
            inline=True,
        )

        current_tier = None
        for threshold, role_id in tiers:
            if count >= threshold:
                current_tier = (threshold, role_id)
            else:
                break

        if current_tier is None:
            current_text = "No monthly tier reached yet." if monthly else "No activity role yet."
        else:
            threshold, role_id = current_tier
            role = guild.get_role(role_id)
            role_text = role.mention if role else f"<deleted {role_id}>"
            timing = " Will be awarded at the next rollover." if monthly else ""
            current_text = f"{role_text}\nReached at **{threshold}** messages.{timing}"
        embed.add_field(
            name="This month's result" if monthly else "Current role",
            value=current_text,
            inline=False,
        )

        next_tier = next(((threshold, role_id) for threshold, role_id in tiers if count < threshold), None)
        if next_tier is not None:
            threshold, role_id = next_tier
            role = guild.get_role(role_id)
            role_text = role.mention if role else f"<deleted {role_id}>"
            embed.add_field(
                name="Next role",
                value=f"**{threshold - count}** more messages for {role_text}.",
                inline=False,
            )
        elif tiers:
            embed.add_field(name="Next role", value="Top configured role reached.", inline=False)
        else:
            embed.add_field(name="Activity tiers", value="No tiers configured.", inline=False)

        return embed

    def _build_leaderboard_embed(
        self,
        guild: discord.Guild,
        leaderboard: list[tuple[int, int]],
        *,
        page: int = 0,
        monthly: bool = False,
    ) -> discord.Embed:
        if not leaderboard:
            description = "No activity has been counted yet."
        else:
            medals = ["🥇", "🥈", "🥉"]
            lines = []
            start = page * _LEADERBOARD_PAGE_SIZE
            page_rows = leaderboard[start : start + _LEADERBOARD_PAGE_SIZE]
            for index, (user_id, count) in enumerate(page_rows, start=start + 1):
                member = guild.get_member(user_id)
                name = member.mention if member else f"<@{user_id}>"
                rank = medals[index - 1] if index <= len(medals) else f"`#{index}`"
                lines.append(f"{rank} {name} — **{count}** messages")
            description = "\n".join(lines)

        embed = discord.Embed(
            title="Monthly Activity Leaderboard" if monthly else "Activity Leaderboard",
            description=description,
            color=discord.Color.gold(),
        )
        page_count = max(1, (len(leaderboard) + _LEADERBOARD_PAGE_SIZE - 1) // _LEADERBOARD_PAGE_SIZE)
        if page_count > 1:
            embed.set_footer(text=f"Page {page + 1}/{page_count} • Top {_LEADERBOARD_LIMIT}")
        elif monthly:
            embed.set_footer(text="Current UTC calendar month")
        return embed

    async def _send_leaderboard(self, interaction: discord.Interaction, *, monthly: bool) -> None:
        if interaction.guild is None:
            return
        leaderboard = self.activity_store.get_leaderboard(
            interaction.guild.id,
            limit=_LEADERBOARD_LIMIT,
            monthly=monthly,
        )
        embed = self._build_leaderboard_embed(
            interaction.guild,
            leaderboard,
            monthly=monthly,
        )
        view = None
        if len(leaderboard) > _LEADERBOARD_PAGE_SIZE:
            view = _LeaderboardView(self, interaction.guild, leaderboard, monthly=monthly)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    # ========================================================
    # /activity public
    # ========================================================

    @activity.command(name="status", description="Show your activity count and role progress.")
    async def activity_status(self, interaction: discord.Interaction):
        if interaction.guild is None:
            return

        if not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message(
                "Could not read your server member data.",
                ephemeral=True,
            )
            return

        count = self.activity_store.get_count(interaction.guild.id, interaction.user.id)
        tiers = self.activity_store.get_tiers(interaction.guild.id)
        embed = self._build_status_embed(interaction.guild, interaction.user, count=count, tiers=tiers)

        await interaction.response.send_message(embed=embed, ephemeral=True)

    @activity.command(name="leaderboard", description="Show the top 30 all-time members.")
    async def activity_leaderboard(self, interaction: discord.Interaction):
        await self._send_leaderboard(interaction, monthly=False)

    @activity_monthly.command(name="status", description="Show your activity progress for this UTC month.")
    async def monthly_status(self, interaction: discord.Interaction):
        if interaction.guild is None:
            return
        if not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message(
                "Could not read your server member data.",
                ephemeral=True,
            )
            return
        count = self.activity_store.get_count(
            interaction.guild.id,
            interaction.user.id,
            monthly=True,
        )
        tiers = self.activity_store.get_tiers(interaction.guild.id, monthly=True)
        embed = self._build_status_embed(
            interaction.guild,
            interaction.user,
            count=count,
            tiers=tiers,
            title_prefix="Monthly Activity Status",
            monthly=True,
        )
        embed.add_field(
            name="Monthly role timing",
            value="Roles earned this month are assigned at 00:00 UTC on the first of next month.",
            inline=False,
        )
        embed.set_footer(text="Current UTC calendar month")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @activity_monthly.command(name="leaderboard", description="Show the top 30 members this UTC month.")
    async def monthly_leaderboard(self, interaction: discord.Interaction):
        await self._send_leaderboard(interaction, monthly=True)

    # ========================================================
    # /amadeus activity tier
    # ========================================================

    @amadeus_activity.command(name="tier-add", description="Add a role tier at a message count threshold.")
    @app_commands.describe(
        threshold="Number of messages required.",
        role="Role to assign when the threshold is reached.",
        monthly="Use the current UTC month's count instead of the all-time count.",
    )
    async def tier_add(
        self,
        interaction: discord.Interaction,
        threshold: int,
        role: discord.Role,
        monthly: bool = False,
    ):
        config = await require_amadeus_access(interaction, self.module_store)

        if config is None or interaction.guild is None:
            return

        if threshold < 1:
            await interaction.response.send_message(
                "Threshold must be at least 1.",
                ephemeral=True,
            )
            return

        if role == interaction.guild.default_role:
            await interaction.response.send_message(
                "You cannot use @everyone as a tier role.",
                ephemeral=True,
            )
            return

        bot_member = interaction.guild.me

        if bot_member is None:
            await interaction.response.send_message(
                "Could not read my server member data.",
                ephemeral=True,
            )
            return

        hierarchy_error = check_role_hierarchy(bot_member, role)

        if hierarchy_error is not None:
            await interaction.response.send_message(hierarchy_error, ephemeral=True)
            return

        role_uses = self.activity_store.get_tier_uses_for_role(
            interaction.guild.id,
            role.id,
        )
        if role_uses:
            scope, configured_threshold = role_uses[0]
            await interaction.response.send_message(
                f"{role.mention} is already used by "
                f"{self._format_role_use(scope, configured_threshold)}. "
                "Each activity tier must use a unique role.",
                ephemeral=True,
            )
            return

        existing_tier = self.activity_store.get_tier(
            interaction.guild.id,
            threshold,
            monthly=monthly,
        )
        removed_members = 0
        deferred = False
        if existing_tier is not None:
            _, old_role_id = existing_tier
            old_role_uses = self.activity_store.get_tier_uses_for_role(
                interaction.guild.id,
                old_role_id,
            )
            old_role = interaction.guild.get_role(old_role_id)
            # Legacy configurations could reuse a role. Only clean it when this
            # is its sole tier, otherwise another configured tier still owns it.
            if old_role is not None and len(old_role_uses) == 1:
                await interaction.response.defer(ephemeral=True, thinking=True)
                deferred = True
                removed_members, failed = await self._remove_role_from_members(
                    interaction.guild,
                    old_role,
                    reason=f"Activity tier replaced at {threshold} messages",
                )
                if failed:
                    await interaction.edit_original_response(
                        content=(
                            f"Could not remove {old_role.mention} from **{failed}** member(s). "
                            "The existing tier was kept; fix my role permissions and try again."
                        )
                    )
                    return

        try:
            self.activity_store.add_tier(
                interaction.guild.id,
                threshold,
                role.id,
                monthly=monthly,
            )
        except sqlite3.IntegrityError:
            message = (
                f"{role.mention} was assigned to another activity tier while this command "
                "was running. Each activity tier must use a unique role."
            )
            if deferred:
                await interaction.edit_original_response(content=message)
            else:
                await interaction.response.send_message(message, ephemeral=True)
            return
        self._invalidate(interaction.guild.id)
        log(
            f"ACTIVITY // TIER ADDED 『 THRESHOLD {threshold} 』 ROLE 『 {role.id} 』 "
            f"GUILD 『 {interaction.guild.id} 』 MONTHLY 『 {monthly} 』",
            level="debug",
            logger_name="activity",
        )

        response = (
            f"{'Monthly' if monthly else 'All-time'} tier added: "
            f"**{threshold} messages** → {role.mention}."
        )
        if removed_members:
            response += f" Removed the previous tier role from **{removed_members}** member(s)."
        if deferred:
            await interaction.edit_original_response(content=response)
        else:
            await interaction.response.send_message(response, ephemeral=True)

    @amadeus_activity.command(name="tier-remove", description="Remove the tier at the given message threshold.")
    @app_commands.describe(
        threshold="The threshold of the tier to remove.",
        monthly="Remove a monthly tier instead of an all-time tier.",
    )
    async def tier_remove(
        self,
        interaction: discord.Interaction,
        threshold: int,
        monthly: bool = False,
    ):
        config = await require_amadeus_access(interaction, self.module_store)

        if config is None or interaction.guild is None:
            return

        existing_tier = self.activity_store.get_tier(
            interaction.guild.id,
            threshold,
            monthly=monthly,
        )

        if existing_tier is None:
            await interaction.response.send_message(
                f"No {'monthly' if monthly else 'all-time'} tier found at **{threshold} messages**.",
                ephemeral=True,
            )
            return

        _, role_id = existing_tier
        role_uses = self.activity_store.get_tier_uses_for_role(
            interaction.guild.id,
            role_id,
        )
        role = interaction.guild.get_role(role_id)
        removed_members = 0
        deferred = False
        # See tier-add: preserve a role still referenced by a legacy duplicate.
        if role is not None and len(role_uses) == 1:
            await interaction.response.defer(ephemeral=True, thinking=True)
            deferred = True
            removed_members, failed = await self._remove_role_from_members(
                interaction.guild,
                role,
                reason=f"Activity tier removed at {threshold} messages",
            )
            if failed:
                await interaction.edit_original_response(
                    content=(
                        f"Could not remove {role.mention} from **{failed}** member(s). "
                        "The tier was kept; fix my role permissions and try again."
                    )
                )
                return

        removed = self.activity_store.remove_tier(
            interaction.guild.id,
            threshold,
            monthly=monthly,
        )
        if not removed:
            # Defensive: another command changed it after the initial lookup.
            message = f"No {'monthly' if monthly else 'all-time'} tier found at **{threshold} messages**."
            if deferred:
                await interaction.edit_original_response(content=message)
            else:
                await interaction.response.send_message(message, ephemeral=True)
            return

        self._invalidate(interaction.guild.id)
        log(
            f"ACTIVITY // TIER REMOVED 『 THRESHOLD {threshold} 』 GUILD 『 {interaction.guild.id} 』 "
            f"MONTHLY 『 {monthly} 』",
            level="debug",
            logger_name="activity",
        )

        response = f"{'Monthly' if monthly else 'All-time'} tier at **{threshold} messages** removed."
        if removed_members:
            response += f" Removed its role from **{removed_members}** member(s)."
        if deferred:
            await interaction.edit_original_response(content=response)
        else:
            await interaction.response.send_message(response, ephemeral=True)

    @amadeus_activity.command(name="tier-list", description="List all configured activity tiers.")
    async def tier_list(self, interaction: discord.Interaction):
        config = await require_amadeus_access(interaction, self.module_store)

        if config is None or interaction.guild is None:
            return

        tiers = self.activity_store.get_tiers(interaction.guild.id)
        monthly_tiers = self.activity_store.get_tiers(interaction.guild.id, monthly=True)

        if not tiers and not monthly_tiers:
            await interaction.response.send_message(
                "No tiers configured. Add one with `/amadeus activity tier-add`.",
                ephemeral=True,
            )
            return

        lines = []
        for heading, configured_tiers in (("All-time", tiers), ("Monthly (UTC)", monthly_tiers)):
            if not configured_tiers:
                continue
            if lines:
                lines.append("")
            lines.append(f"**{heading}**")
            for threshold, role_id in configured_tiers:
                role = interaction.guild.get_role(role_id)
                role_text = role.mention if role else f"~~<deleted role {role_id}>~~"
                lines.append(f"**{threshold}** messages → {role_text}")

        embed = discord.Embed(
            title="Activity Tiers",
            description="\n".join(lines),
            color=discord.Color.blurple(),
        )

        await interaction.response.send_message(embed=embed, ephemeral=True)

    @amadeus_activity.command(name="role-swap", description="Only keep the member's highest earned activity role.")
    @app_commands.describe(enabled="True removes older tier roles when a newer tier is earned.")
    async def tier_role_swap(
        self,
        interaction: discord.Interaction,
        enabled: bool,
    ):
        config = await require_amadeus_access(interaction, self.module_store)

        if config is None or interaction.guild is None:
            return

        self.activity_store.set_role_swap_enabled(interaction.guild.id, enabled)
        self._invalidate(interaction.guild.id)
        state = "enabled" if enabled else "disabled"

        await interaction.response.send_message(
            f"Activity role swap **{state}**.",
            ephemeral=True,
        )

    # ========================================================
    # /amadeus activity channel
    # ========================================================

    @amadeus_activity.command(
        name="channel-include",
        description="Only count messages from this channel (switches to whitelist mode).",
    )
    @app_commands.describe(channel="Channel to include.")
    async def channel_include(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
    ):
        config = await require_amadeus_access(interaction, self.module_store)

        if config is None or interaction.guild is None:
            return

        self.activity_store.add_channel(interaction.guild.id, channel.id, "include")
        self._invalidate(interaction.guild.id)
        log(
            f"ACTIVITY // CHANNEL INCLUDE 『 {channel.id} 』 GUILD 『 {interaction.guild.id} 』",
            level="debug",
            logger_name="activity",
        )

        await interaction.response.send_message(
            f"{channel.mention} added to include list.\n\n"
            "When any channel is on the include list, only those channels count.",
            ephemeral=True,
        )

    @amadeus_activity.command(name="channel-exclude", description="Don't count messages from this channel.")
    @app_commands.describe(channel="Channel to exclude.")
    async def channel_exclude(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
    ):
        config = await require_amadeus_access(interaction, self.module_store)

        if config is None or interaction.guild is None:
            return

        self.activity_store.add_channel(interaction.guild.id, channel.id, "exclude")
        self._invalidate(interaction.guild.id)
        log(
            f"ACTIVITY // CHANNEL EXCLUDE 『 {channel.id} 』 GUILD 『 {interaction.guild.id} 』",
            level="debug",
            logger_name="activity",
        )

        await interaction.response.send_message(
            f"{channel.mention} added to exclude list.",
            ephemeral=True,
        )

    @amadeus_activity.command(name="channel-remove", description="Remove a channel from the include or exclude list.")
    @app_commands.describe(channel="Channel to remove.")
    async def channel_remove(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
    ):
        config = await require_amadeus_access(interaction, self.module_store)

        if config is None or interaction.guild is None:
            return

        removed = self.activity_store.remove_channel(interaction.guild.id, channel.id)

        if not removed:
            await interaction.response.send_message(
                f"{channel.mention} is not on any filter list.",
                ephemeral=True,
            )
            return

        self._invalidate(interaction.guild.id)
        log(
            f"ACTIVITY // CHANNEL REMOVED 『 {channel.id} 』 GUILD 『 {interaction.guild.id} 』",
            level="debug",
            logger_name="activity",
        )

        await interaction.response.send_message(
            f"{channel.mention} removed from filter list.",
            ephemeral=True,
        )

    @amadeus_activity.command(name="channel-list", description="Show channel filter configuration.")
    async def channel_list(self, interaction: discord.Interaction):
        config = await require_amadeus_access(interaction, self.module_store)

        if config is None or interaction.guild is None:
            return

        rows = self.activity_store.get_channels_raw(interaction.guild.id)

        if not rows:
            await interaction.response.send_message(
                "No channel filters set. All channels count toward activity.",
                ephemeral=True,
            )
            return

        includes = [r for r in rows if r["mode"] == "include"]
        excludes = [r for r in rows if r["mode"] == "exclude"]
        lines = []

        if includes:
            lines.append("**Include (only these channels count):**")
            for r in includes:
                cid = r["channel_id"]
                ch = interaction.guild.get_channel(cid)
                lines.append(f"  {ch.mention if ch else f'<deleted {cid}>'}")

        if excludes:
            if lines:
                lines.append("")
            lines.append("**Exclude:**")
            for r in excludes:
                cid = r["channel_id"]
                ch = interaction.guild.get_channel(cid)
                lines.append(f"  {ch.mention if ch else f'<deleted {cid}>'}")

        embed = discord.Embed(
            title="Activity Channel Filters",
            description="\n".join(lines),
            color=discord.Color.blurple(),
        )

        if includes:
            embed.set_footer(text="Include mode active — only listed channels count.")

        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ========================================================
    # /amadeus activity settings
    # ========================================================

    @amadeus_activity.command(
        name="cooldown",
        description="Minimum seconds between counted messages per user.",
    )
    @app_commands.describe(seconds="1–3600 seconds. Default: 5.")
    async def settings_cooldown(
        self,
        interaction: discord.Interaction,
        seconds: int,
    ):
        config = await require_amadeus_access(interaction, self.module_store)

        if config is None or interaction.guild is None:
            return

        if not 1 <= seconds <= 3600:
            await interaction.response.send_message(
                "Value must be between **1** and **3600** seconds.",
                ephemeral=True,
            )
            return

        self.activity_store.set_cooldown(interaction.guild.id, seconds)
        self._invalidate(interaction.guild.id)
        log(
            f"ACTIVITY // COOLDOWN SET {seconds}s 『 GUILD {interaction.guild.id} 』",
            level="debug",
            logger_name="activity",
        )

        await interaction.response.send_message(
            f"Activity cooldown set to **{seconds} seconds**.",
            ephemeral=True,
        )

    # ========================================================
    # /amadeus activity admin
    # ========================================================

    @amadeus_activity.command(name="status", description="Check a member's activity count and tier progress.")
    @app_commands.describe(member="The member to inspect.")
    async def admin_status(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
    ):
        config = await require_amadeus_access(interaction, self.module_store)

        if config is None or interaction.guild is None:
            return

        count = self.activity_store.get_count(interaction.guild.id, member.id)
        tiers = self.activity_store.get_tiers(interaction.guild.id)
        cooldown = self.activity_store.get_cooldown(interaction.guild.id)

        embed = self._build_status_embed(
            interaction.guild,
            member,
            count=count,
            tiers=tiers,
            title_prefix="Activity",
        )
        embed.add_field(name="Cooldown", value=f"{cooldown}s", inline=True)

        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot):
    cog = ActivityAdmin(bot)
    attach_amadeus_subgroup(bot, cog, cog.amadeus_activity)

    try:
        await bot.add_cog(cog)
    except Exception:
        detach_amadeus_subgroup(bot, cog.amadeus_activity.name)
        raise
