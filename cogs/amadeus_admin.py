import discord
from discord import app_commands
from discord.ext import commands, tasks

from amadeus.cogs import (
    STATIC_COG_EXTENSIONS,
    extension_to_module_name,
    get_configured_cog_extensions,
)
from amadeus.database import ConfigStore
from amadeus.discord_utils import NO_MENTIONS
from amadeus.logging_utils import log
from amadeus.permissions import get_admin_role, require_amadeus_access
from cogs import debug


_ADMIN_CHANNEL_REQUIRED_PERMS: tuple[tuple[str, str], ...] = (
    ("view_channel", "View Channel"),
    ("send_messages", "Send Messages"),
    ("embed_links", "Embed Links"),
    ("attach_files", "Attach Files"),
)


def get_amadeus_command_group(bot: commands.Bot) -> app_commands.Group:
    admin_cog = bot.get_cog("AmadeusAdmin")

    if admin_cog is None:
        raise RuntimeError("AmadeusAdmin must be loaded before module admin cogs.")

    group = getattr(admin_cog, "amadeus", None)

    if not isinstance(group, app_commands.Group):
        raise RuntimeError("AmadeusAdmin does not expose an /amadeus command group.")

    return group


def attach_amadeus_subgroup(
    bot: commands.Bot,
    cog: commands.Cog,
    group: app_commands.Group,
) -> None:
    amadeus = get_amadeus_command_group(bot)
    amadeus.add_command(group)
    # The group stays bound to its module cog, but should only sync as an
    # /amadeus child rather than as its own top-level slash command.
    cog.__cog_app_commands__ = [
        command for command in cog.__cog_app_commands__ if command is not group
    ]


def detach_amadeus_subgroup(bot: commands.Bot, name: str) -> None:
    try:
        amadeus = get_amadeus_command_group(bot)
    except RuntimeError:
        return

    amadeus.remove_command(name)


def _missing_admin_channel_permissions(channel: discord.TextChannel, member: discord.Member) -> list[str]:
    permissions = channel.permissions_for(member)
    return [
        label
        for attr, label in _ADMIN_CHANNEL_REQUIRED_PERMS
        if not getattr(permissions, attr)
    ]


class _AdminChannelConfirmView(discord.ui.View):
    def __init__(
        self,
        *,
        store: ConfigStore,
        guild_id: int,
        channel_id: int,
        requester_id: int,
    ) -> None:
        super().__init__(timeout=120)
        self.store = store
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.requester_id = requester_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message(
                "Only the user who started this confirmation can use these buttons.",
                ephemeral=True,
            )
            return False
        return True

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.success)
    async def confirm(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        if interaction.guild is None or interaction.guild.id != self.guild_id:
            await interaction.response.send_message(
                "This confirmation is no longer valid.",
                ephemeral=True,
            )
            return

        channel = interaction.guild.get_channel(self.channel_id)
        if not isinstance(channel, discord.TextChannel):
            await interaction.response.edit_message(
                content="That channel no longer exists. No changes were made.",
                view=None,
            )
            return

        bot_member = interaction.guild.me
        if bot_member is None:
            await interaction.response.edit_message(
                content="Could not read my server member data. No changes were made.",
                view=None,
            )
            return

        missing_permissions = _missing_admin_channel_permissions(channel, bot_member)
        if missing_permissions:
            needed = ", ".join(f"**{permission}**" for permission in missing_permissions)
            await interaction.response.edit_message(
                content=(
                    f"Alert channel was not changed. I do not have the required permissions in {channel.mention}.\n\n"
                    f"Grant me: {needed}."
                ),
                view=None,
            )
            return

        await interaction.response.defer()

        try:
            await channel.send(
                embed=discord.Embed(
                    title="Amadeus Alert Channel Check",
                    description="This channel can receive Amadeus approval requests and internal alerts.",
                    color=discord.Color.orange(),
                ),
                allowed_mentions=NO_MENTIONS,
            )
        except discord.Forbidden:
            needed = ", ".join(f"**{permission}**" for _, permission in _ADMIN_CHANNEL_REQUIRED_PERMS)
            await interaction.edit_original_response(
                content=(
                    f"Alert channel was not changed. I could not post in {channel.mention}.\n\n"
                    f"Grant me: {needed}."
                ),
                view=None,
            )
            return
        except discord.HTTPException as e:
            await interaction.edit_original_response(
                content=(
                    f"Alert channel was not changed. I tried to post in {channel.mention}, but Discord rejected the message.\n\n"
                    f"Error: `{e}`"
                ),
                view=None,
            )
            return

        self.store.set_alert_channel(interaction.guild, channel.id)
        log(
            f"ADMIN // ALERT CHANNEL SET 『 {channel.id} 』 GUILD 『 {interaction.guild.id} 』",
            level="debug",
            logger_name="admin",
        )
        await interaction.edit_original_response(
            content=f"Permission check posted successfully. Alert channel set to {channel.mention}.",
            view=None,
        )

    @discord.ui.button(label="Deny", style=discord.ButtonStyle.danger)
    async def deny(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        await interaction.response.edit_message(
            content="Alert channel was not changed.",
            view=None,
        )


class AmadeusAdmin(commands.Cog):
    """
    Static admin and setup cog.

    Always loaded by main.py so it can be used to troubleshoot and manage
    other cogs regardless of server state.

    Commands: /amadeus
    """

    amadeus = app_commands.Group(
        name="amadeus",
        description="Amadeus admin commands.",
        guild_only=True,
        default_permissions=discord.Permissions(manage_roles=True),
    )

    amadeus_module = app_commands.Group(
        name="module",
        description="Enable or disable modules for this server.",
        parent=amadeus,
    )

    amadeus_debug = app_commands.Group(
        name="debug",
        description="Debug utilities.",
        parent=amadeus,
    )

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.store = ConfigStore()

    def cog_unload(self):
        self._purge_departed_guilds.cancel()
        self.store.close()

    # ========================================================
    # Guild lifecycle events
    # ========================================================

    @commands.Cog.listener()
    async def on_guild_join(self, guild: discord.Guild):
        log(f"ADMIN // GUILD JOIN 『 {guild.name} | {guild.id} 』", level="debug", logger_name="admin")
        self.store.cancel_guild_deletion(guild.id)
        self.store.ensure_guild_config(guild)
        await self._send_setup_prompt(guild)

    @commands.Cog.listener()
    async def on_guild_remove(self, guild: discord.Guild):
        delete_after = self.store.schedule_guild_deletion(guild.id, days=90)
        log(
            f"ADMIN // GUILD LEFT 『 {guild.id} 』 // DATA DELETE AFTER 『 {delete_after.isoformat()} 』",
            level="warning",
            logger_name="admin",
        )

    @commands.Cog.listener()
    async def on_ready(self):
        log(f"ADMIN // READY // ENSURING CONFIG FOR 『 {len(self.bot.guilds)} 』 GUILDS", level="debug", logger_name="admin")
        for guild in self.bot.guilds:
            self.store.cancel_guild_deletion(guild.id)
            self.store.ensure_guild_config(guild)
        if not self._purge_departed_guilds.is_running():
            self._purge_departed_guilds.start()

    @tasks.loop(hours=24)
    async def _purge_departed_guilds(self) -> None:
        active_guild_ids = {guild.id for guild in self.bot.guilds}
        for guild_id in self.store.get_due_guild_deletions():
            if guild_id in active_guild_ids:
                self.store.cancel_guild_deletion(guild_id)
                continue
            self.store.purge_guild_data(guild_id)
            log(
                f"ADMIN // PURGED DEPARTED GUILD DATA 『 {guild_id} 』",
                level="warning",
                logger_name="admin",
            )

    @_purge_departed_guilds.before_loop
    async def _before_purge_departed_guilds(self) -> None:
        await self.bot.wait_until_ready()

    async def _send_setup_prompt(self, guild: discord.Guild):
        module_list = ", ".join(
            f"`{extension_to_module_name(ext)}`"
            for ext in get_configured_cog_extensions()
            if not ext.endswith("_admin")
        ) or "None configured"

        embed = discord.Embed(
            title="Amadeus — Getting Started",
            description=(
                f"Thanks for adding Amadeus to **{guild.name}**!\n\n"
                "**Step 1 — Set an admin role:**\n"
                "`/amadeus set-admin-role` — The role that can manage Amadeus\n\n"
                "**Step 2 — Enable modules:**\n"
                f"`/amadeus module enable <module>`\n\n"
                f"Available modules: {module_list}\n\n"
                "Run `/amadeus config` to review your configuration at any time."
            ),
            color=discord.Color.orange(),
        )

        # Try to DM the guild owner first.
        try:
            owner = await guild.fetch_member(guild.owner_id)
            await owner.send(embed=embed)
            return
        except (discord.Forbidden, discord.HTTPException, discord.NotFound):
            log(f"ADMIN // DM FAILED // OWNER 『 {guild.owner_id} 』 GUILD 『 {guild.id} 』", level="debug", logger_name="admin")

        # Fall back to the system channel if the DM fails.
        system_channel = guild.system_channel
        if system_channel and system_channel.permissions_for(guild.me).send_messages:
            try:
                await system_channel.send(embed=embed)
            except (discord.Forbidden, discord.HTTPException) as e:
                log(f"ADMIN // SYSTEM CHANNEL SEND FAILED 『 GUILD {guild.id} 』 // {e}", level="debug", logger_name="admin")
        else:
            log(f"ADMIN // NO SYSTEM CHANNEL 『 GUILD {guild.id} 』", level="debug", logger_name="admin")

    # ========================================================
    # Module enable/disable  (/amadeus module ...)
    # ========================================================

    def _available_module_names(self) -> list[str]:
        """Short names of all configured non-static extensions that are currently loaded.

        Excludes *_admin paired extensions — those are implementation details.
        """

        return [
            extension_to_module_name(ext)
            for ext in get_configured_cog_extensions()
            if ext in self.bot.extensions and not ext.endswith("_admin")
        ]

    async def _module_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        modules = self._available_module_names()
        return [
            app_commands.Choice(name=m, value=m)
            for m in modules
            if current.lower() in m.lower()
        ]

    @amadeus_module.command(
        name="enable",
        description="Enable a module for this server.",
    )
    @app_commands.describe(module="The module to enable.")
    @app_commands.autocomplete(module=_module_autocomplete)
    async def module_enable(
        self,
        interaction: discord.Interaction,
        module: str,
    ):
        config = await require_amadeus_access(interaction, self.store)

        if config is None or interaction.guild is None:
            return

        available = self._available_module_names()

        if module not in available:
            available_str = ", ".join(f"`{m}`" for m in available) or "none"
            await interaction.response.send_message(
                f"`{module}` is not a known module. Available: {available_str}",
                ephemeral=True,
            )
            return

        if self.store.is_module_enabled(interaction.guild.id, module):
            log(f"ADMIN // MODULE ALREADY ENABLED 『 {module} 』 GUILD 『 {interaction.guild.id} 』", level="debug", logger_name="admin")
            await interaction.response.send_message(
                f"The **{module}** module is already enabled for this server.",
                ephemeral=True,
            )
            return

        if module == "boost":
            if config.alert_channel_id is None:
                await interaction.response.send_message(
                    "Set an admin alert channel before enabling boost with "
                    "`/amadeus set-admin-channel`.",
                    ephemeral=True,
                )
                return
            alert_channel = interaction.guild.get_channel(config.alert_channel_id)
            bot_member = interaction.guild.me
            if not isinstance(alert_channel, discord.TextChannel) or bot_member is None:
                await interaction.response.send_message(
                    "The configured admin alert channel is missing or unavailable. "
                    "Run `/amadeus set-admin-channel` again.",
                    ephemeral=True,
                )
                return
            missing = _missing_admin_channel_permissions(alert_channel, bot_member)
            if missing:
                await interaction.response.send_message(
                    f"Boost was not enabled. Grant me {', '.join(missing)} in "
                    f"{alert_channel.mention}, then try again.",
                    ephemeral=True,
                )
                return

        self.store.enable_module(interaction.guild.id, module)

        _setup_hints: dict[str, str] = {
            "bouncer": (
                "**Next steps:**\n"
                "1. `/amadeus bouncer set-role <role>` — role granted on verification\n"
                "2. `/amadeus bouncer set-channel <channel>` — channel members verify in\n"
                "3. `/amadeus bouncer post-panel` — post the verification button"
            ),
            "honeypot": (
                "**Next steps:**\n"
                "1. `/amadeus honeypot set-channel <channel>` — trap channel\n"
                "2. `/amadeus honeypot set-action <action>` — action taken on anyone who posts\n"
                "3. *(Optional)* `/amadeus honeypot message <message>` — customize the warning message\n"
                "4. `/amadeus honeypot post` — post or update the warning message in the channel"
            ),
            "boost": (
                "**Next steps:**\n"
                "1. `/amadeus set-admin-channel <channel>` — where approval requests are posted\n"
                "2. The flow starts automatically when a member boosts. Use `/amadeus boost start <member>` to trigger it manually."
            ),
            "activity": (
                "**Next steps:**\n"
                "1. `/amadeus activity tier-add <threshold> <role>` — add at least one milestone\n"
                "2. *(Optional)* `/amadeus activity channel-include` or `channel-exclude` — filter which channels count\n"
                "3. *(Optional)* `/amadeus activity cooldown` — adjust message cooldown (default 5s)"
            ),
            "play": (
                "**Next steps:**\n"
                "1. `/amadeus play set-forum <forum>` — optional default forum for newly added games\n"
                "2. `/amadeus play add-game <name> [forum]` — add a game and bind or create its forum tag\n"
                "3. `/amadeus play config` — verify permissions and setup"
            ),
        }

        hint = _setup_hints.get(module, "")
        suffix = f"\n\n{hint}" if hint else ""

        await interaction.response.send_message(
            f"The **{module}** module has been enabled for this server.{suffix}",
            ephemeral=True,
        )

    @amadeus_module.command(
        name="disable",
        description="Disable a module for this server.",
    )
    @app_commands.describe(module="The module to disable.")
    @app_commands.autocomplete(module=_module_autocomplete)
    async def module_disable(
        self,
        interaction: discord.Interaction,
        module: str,
    ):
        config = await require_amadeus_access(interaction, self.store)

        if config is None or interaction.guild is None:
            return

        available = self._available_module_names()

        if module not in available:
            available_str = ", ".join(f"`{m}`" for m in available) or "none"
            await interaction.response.send_message(
                f"`{module}` is not a known module. Available: {available_str}",
                ephemeral=True,
            )
            return

        if not self.store.is_module_enabled(interaction.guild.id, module):
            log(f"ADMIN // MODULE NOT ENABLED 『 {module} 』 GUILD 『 {interaction.guild.id} 』", level="debug", logger_name="admin")
            await interaction.response.send_message(
                f"The **{module}** module is not currently enabled for this server.",
                ephemeral=True,
            )
            return

        self.store.disable_module(interaction.guild.id, module)

        await interaction.response.send_message(
            f"The **{module}** module has been disabled for this server.",
            ephemeral=True,
        )

    # ========================================================
    # Module listing  (/amadeus list-cogs)
    # ========================================================

    @amadeus.command(
        name="list-cogs",
        description="Show modules and their enabled state for this server.",
    )
    async def amadeus_list_cogs(self, interaction: discord.Interaction):
        config = await require_amadeus_access(interaction, self.store)

        if config is None or interaction.guild is None:
            return

        configured = get_configured_cog_extensions()
        enabled_modules = self.store.get_enabled_modules(interaction.guild.id)

        embed = discord.Embed(
            title="Amadeus Modules",
            color=discord.Color.orange(),
        )

        embed.add_field(
            name="Static",
            value="\n".join(f"`{cog}`" for cog in sorted(STATIC_COG_EXTENSIONS)),
            inline=False,
        )

        if configured:
            lines = []
            for ext in configured:
                if ext.endswith("_admin"):
                    continue
                module = extension_to_module_name(ext)
                loaded = ext in self.bot.extensions
                enabled = module in enabled_modules
                status = "✅ enabled" if enabled else "❌ disabled"
                loaded_tag = "" if loaded else " *(not loaded)*"
                lines.append(f"`{module}`{loaded_tag} — {status}")

            embed.add_field(name="Modules", value="\n".join(lines), inline=False)
        else:
            embed.add_field(name="Modules", value="No dynamic modules configured.", inline=False)

        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ========================================================
    # Setup/config commands  (/amadeus config, set-admin-role)
    # ========================================================

    @amadeus.command(
        name="config",
        description="Show this server's Amadeus configuration.",
    )
    async def amadeus_config(self, interaction: discord.Interaction):
        config = await require_amadeus_access(interaction, self.store)

        if config is None or interaction.guild is None:
            return

        admin_role = get_admin_role(interaction.guild, config)

        embed = discord.Embed(
            title="Amadeus Configuration",
            color=discord.Color.orange(),
        )

        embed.add_field(name="Server ID", value=str(config.guild_id), inline=False)
        embed.add_field(
            name="Server owner",
            value=f"<@{config.owner_id}> (`{config.owner_id}`)",
            inline=False,
        )
        embed.add_field(
            name="Admin role",
            value=admin_role.mention if admin_role else "Not set",
            inline=False,
        )
        embed.add_field(
            name="Alert channel",
            value=f"<#{config.alert_channel_id}>" if config.alert_channel_id else "Not set",
            inline=False,
        )

        enabled_modules = self.store.get_enabled_modules(interaction.guild.id)
        modules_value = ", ".join(f"`{m}`" for m in sorted(enabled_modules)) or "None enabled"
        embed.add_field(name="Enabled modules", value=modules_value, inline=False)

        await interaction.response.send_message(embed=embed, ephemeral=True)

    @amadeus.command(
        name="health",
        description="Check Amadeus configuration and operational health for this server.",
    )
    async def amadeus_health(self, interaction: discord.Interaction):
        config = await require_amadeus_access(interaction, self.store)
        if config is None or interaction.guild is None:
            return

        guild = interaction.guild
        issues: list[str] = []
        db_result = self.store.db.execute("PRAGMA quick_check").fetchone()[0]
        if db_result != "ok":
            issues.append(f"Database integrity check returned: `{db_result}`")

        admin_role = get_admin_role(guild, config)
        if admin_role is None:
            issues.append("Configured Amadeus admin role is missing.")

        alert_channel = guild.get_channel(config.alert_channel_id) if config.alert_channel_id else None
        if not isinstance(alert_channel, discord.TextChannel):
            issues.append("Admin alert channel is not configured or no longer exists.")
        elif guild.me is None:
            issues.append("Bot member information is unavailable.")
        else:
            missing = _missing_admin_channel_permissions(alert_channel, guild.me)
            if missing:
                issues.append(f"Admin alert channel is missing: {', '.join(missing)}.")

        enabled = self.store.get_enabled_modules(guild.id)
        unloaded = sorted(
            module for module in enabled if f"cogs.{module}" not in self.bot.extensions
        )
        if unloaded:
            issues.append(f"Enabled but not loaded: {', '.join(unloaded)}.")

        boost_store = getattr(self.bot.get_cog("Boost"), "boost_store", None)
        pending_cleanup = boost_store.count_cleanup(guild.id) if boost_store else 0
        if pending_cleanup:
            issues.append(f"{pending_cleanup} boost resource cleanup job(s) are pending.")

        embed = discord.Embed(
            title="Amadeus Health",
            color=discord.Color.green() if not issues else discord.Color.orange(),
        )
        embed.add_field(name="Database", value="✅ OK" if db_result == "ok" else "❌ Error")
        embed.add_field(name="Gateway latency", value=f"{self.bot.latency * 1000:.0f} ms")
        embed.add_field(name="Enabled modules", value=", ".join(sorted(enabled)) or "None", inline=False)
        embed.add_field(
            name="Result",
            value="✅ No issues found." if not issues else "\n".join(f"• {issue}" for issue in issues),
            inline=False,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @amadeus.command(
        name="set-admin-channel",
        description="Set the channel where Amadeus posts internal alerts.",
    )
    @app_commands.describe(channel="The text channel to receive Amadeus alerts.")
    async def amadeus_set_admin_channel(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
    ):
        config = await require_amadeus_access(interaction, self.store)

        if config is None or interaction.guild is None:
            return

        everyone_can_view = channel.permissions_for(interaction.guild.default_role).view_channel
        warning = (
            "\n\n⚠ `@everyone` can view this channel. Approval buttons are restricted to the configured Amadeus admin role, "
            "but approval request details will be visible to all members who can view the channel."
            if everyone_can_view
            else ""
        )

        await interaction.response.send_message(
            f"Set {channel.mention} as the Amadeus alert channel?{warning}",
            view=_AdminChannelConfirmView(
                store=self.store,
                guild_id=interaction.guild.id,
                channel_id=channel.id,
                requester_id=interaction.user.id,
            ),
            ephemeral=True,
        )

    @amadeus.command(
        name="set-admin-role",
        description="Set the role allowed to use /amadeus commands.",
    )
    @app_commands.describe(role="The staff/admin role allowed to use /amadeus.")
    async def amadeus_set_admin_role(
        self,
        interaction: discord.Interaction,
        role: discord.Role,
    ):
        config = await require_amadeus_access(interaction, self.store)

        if config is None or interaction.guild is None:
            return

        if role == interaction.guild.default_role:
            await interaction.response.send_message(
                "You cannot use @everyone as the Amadeus admin role.",
                ephemeral=True,
            )
            return

        self.store.set_admin_role(interaction.guild, role.id)
        log(f"ADMIN // ADMIN ROLE SET 『 {role.id} 』 GUILD 『 {interaction.guild.id} 』", level="debug", logger_name="admin")

        await interaction.response.send_message(
            f"Amadeus admin role set to {role.mention}.",
            ephemeral=True,
        )

    # ========================================================
    # Debug commands  (/amadeus debug ...)
    # ========================================================

    @amadeus_debug.command(
        name="ping",
        description="Check that the bot is responding.",
    )
    async def debug_ping(self, interaction: discord.Interaction):
        await debug.cmd_ping(interaction)

    @amadeus_debug.command(
        name="reacts",
        description="List usernames that reacted to a message with an emoji.",
    )
    @app_commands.describe(
        post_id="Message ID or link to inspect in this channel.",
        emoji="Reaction emoji to inspect.",
    )
    async def debug_reacts(
        self,
        interaction: discord.Interaction,
        post_id: app_commands.Range[str, 1, 128],
        emoji: app_commands.Range[str, 1, 128],
    ):
        config = await require_amadeus_access(interaction, self.store)

        if config is None or interaction.guild is None:
            return

        await debug.cmd_reacts(interaction, post_id, emoji)


async def setup(bot: commands.Bot):
    await bot.add_cog(AmadeusAdmin(bot))
