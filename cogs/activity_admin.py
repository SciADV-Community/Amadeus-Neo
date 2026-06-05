import discord
from discord import app_commands
from discord.ext import commands

from amadeus.activity_store import ActivityStore
from amadeus.database import ConfigStore
from amadeus.discord_utils import check_role_hierarchy
from amadeus.logging_utils import log
from amadeus.module_guard import require_module_enabled_for_interaction
from amadeus.permissions import require_amadeus_access


class ActivityAdmin(commands.Cog):
    """
    Admin cog for the activity module.

    Commands:
      /activity tier     add, remove, list
      /activity channel  include, exclude, remove, list
      /activity settings cooldown
      /activity admin    status
    """

    activity = app_commands.Group(
        name="activity",
        description="Activity role tracking commands.",
        guild_only=True,
        default_permissions=discord.Permissions(manage_roles=True),
    )

    tier = app_commands.Group(
        name="tier",
        description="Configure activity role tiers.",
        parent=activity,
    )

    channel = app_commands.Group(
        name="channel",
        description="Configure which channels count toward activity.",
        parent=activity,
    )

    settings = app_commands.Group(
        name="settings",
        description="Tune activity tracking behavior.",
        parent=activity,
    )

    admin = app_commands.Group(
        name="admin",
        description="Inspect member activity.",
        parent=activity,
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
        self.activity_store.close()
        self.module_store.close()

    def _invalidate(self, guild_id: int) -> None:
        cog = self.bot.get_cog("Activity")
        if cog is not None:
            cog.invalidate_cache(guild_id)

    # ========================================================
    # /activity tier
    # ========================================================

    @tier.command(name="add", description="Add a role tier at a message count threshold.")
    @app_commands.describe(
        threshold="Number of messages required.",
        role="Role to assign when the threshold is reached.",
    )
    async def tier_add(
        self,
        interaction: discord.Interaction,
        threshold: int,
        role: discord.Role,
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

        self.activity_store.add_tier(interaction.guild.id, threshold, role.id)
        self._invalidate(interaction.guild.id)
        log(
            f"ACTIVITY // TIER ADDED 『 THRESHOLD {threshold} 』 ROLE 『 {role.id} 』 "
            f"GUILD 『 {interaction.guild.id} 』",
            level="debug",
            logger_name="activity",
        )

        await interaction.response.send_message(
            f"Tier added: **{threshold} messages** → {role.mention}.",
            ephemeral=True,
        )

    @tier.command(name="remove", description="Remove the tier at the given message threshold.")
    @app_commands.describe(threshold="The threshold of the tier to remove.")
    async def tier_remove(
        self,
        interaction: discord.Interaction,
        threshold: int,
    ):
        config = await require_amadeus_access(interaction, self.module_store)

        if config is None or interaction.guild is None:
            return

        removed = self.activity_store.remove_tier(interaction.guild.id, threshold)

        if not removed:
            await interaction.response.send_message(
                f"No tier found at **{threshold} messages**.",
                ephemeral=True,
            )
            return

        self._invalidate(interaction.guild.id)
        log(
            f"ACTIVITY // TIER REMOVED 『 THRESHOLD {threshold} 』 GUILD 『 {interaction.guild.id} 』",
            level="debug",
            logger_name="activity",
        )

        await interaction.response.send_message(
            f"Tier at **{threshold} messages** removed.",
            ephemeral=True,
        )

    @tier.command(name="list", description="List all configured activity tiers.")
    async def tier_list(self, interaction: discord.Interaction):
        config = await require_amadeus_access(interaction, self.module_store)

        if config is None or interaction.guild is None:
            return

        tiers = self.activity_store.get_tiers(interaction.guild.id)

        if not tiers:
            await interaction.response.send_message(
                "No tiers configured. Add one with `/activity tier add`.",
                ephemeral=True,
            )
            return

        lines = []
        for threshold, role_id in tiers:
            role = interaction.guild.get_role(role_id)
            role_text = role.mention if role else f"~~<deleted role {role_id}>~~"
            lines.append(f"**{threshold}** messages → {role_text}")

        embed = discord.Embed(
            title="Activity Tiers",
            description="\n".join(lines),
            color=discord.Color.blurple(),
        )

        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ========================================================
    # /activity channel
    # ========================================================

    @channel.command(
        name="include",
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

    @channel.command(name="exclude", description="Don't count messages from this channel.")
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

    @channel.command(name="remove", description="Remove a channel from the include or exclude list.")
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

    @channel.command(name="list", description="Show channel filter configuration.")
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
    # /activity settings
    # ========================================================

    @settings.command(
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
    # /activity admin
    # ========================================================

    @admin.command(name="status", description="Check a member's activity count and tier progress.")
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

        embed = discord.Embed(
            title=f"Activity — {member.display_name}",
            color=discord.Color.blurple(),
        )

        embed.add_field(name="Messages counted", value=str(count), inline=True)
        embed.add_field(name="Cooldown", value=f"{cooldown}s", inline=True)

        if tiers:
            tier_lines = []
            for threshold, role_id in tiers:
                role = interaction.guild.get_role(role_id)
                role_text = role.mention if role else f"<deleted {role_id}>"
                if role is not None and role in member.roles:
                    icon = "✅"
                elif count >= threshold:
                    # Count passed threshold but role missing — added after the fact
                    icon = "⚠️"
                else:
                    icon = "🔒"
                tier_lines.append(f"{icon} **{threshold}** → {role_text}")
            embed.add_field(name="Tiers", value="\n".join(tier_lines), inline=False)
        else:
            embed.add_field(name="Tiers", value="None configured.", inline=False)

        next_tier = next(((t, r) for t, r in tiers if count < t), None) if tiers else None

        if next_tier is not None:
            needed = next_tier[0] - count
            role = interaction.guild.get_role(next_tier[1])
            role_text = role.mention if role else f"<deleted {next_tier[1]}>"
            embed.add_field(
                name="Next tier",
                value=f"**{needed}** more messages for {role_text}",
                inline=False,
            )

        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(ActivityAdmin(bot))
