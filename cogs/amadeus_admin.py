import discord
from discord import app_commands
from discord.ext import commands

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
)


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
        self.store.close()

    # ========================================================
    # Guild lifecycle events
    # ========================================================

    @commands.Cog.listener()
    async def on_guild_join(self, guild: discord.Guild):
        log(f"ADMIN // GUILD JOIN 『 {guild.name} | {guild.id} 』", level="debug", logger_name="admin")
        self.store.ensure_guild_config(guild)
        await self._send_setup_prompt(guild)

    @commands.Cog.listener()
    async def on_ready(self):
        log(f"ADMIN // READY // ENSURING CONFIG FOR 『 {len(self.bot.guilds)} 』 GUILDS", level="debug", logger_name="admin")
        for guild in self.bot.guilds:
            self.store.ensure_guild_config(guild)

    async def _send_setup_prompt(self, guild: discord.Guild):
        module_list = ", ".join(
            f"`{extension_to_module_name(ext)}`"
            for ext in get_configured_cog_extensions()
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

        self.store.enable_module(interaction.guild.id, module)

        _setup_hints: dict[str, str] = {
            "bouncer": (
                "**Next steps:**\n"
                "1. `/bouncer setup set-role <role>` — role granted on verification\n"
                "2. `/bouncer setup set-channel <channel>` — channel members verify in\n"
                "3. `/bouncer setup post-panel` — post the verification button"
            ),
            "honeypot": (
                "**Next steps:**\n"
                "1. `/honeypot set-channel <channel>` — trap channel\n"
                "2. `/honeypot set-action <action>` — action taken on anyone who posts\n"
                "3. `/honeypot post` — post the warning embed in the channel"
            ),
            "boost": (
                "**Next steps:**\n"
                "1. `/amadeus set-admin-channel <channel>` — where approval requests are posted\n"
                "2. The flow starts automatically when a member boosts. Use `/boost admin start <member>` to trigger it manually."
            ),
            "activity": (
                "**Next steps:**\n"
                "1. `/activity tier add <threshold> <role>` — add at least one milestone\n"
                "2. *(Optional)* `/activity channel include/exclude` — filter which channels count\n"
                "3. *(Optional)* `/activity settings cooldown` — adjust message cooldown (default 5s)"
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


async def setup(bot: commands.Bot):
    await bot.add_cog(AmadeusAdmin(bot))
