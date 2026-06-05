import importlib

import discord
from discord import app_commands
from discord.ext import commands

from amadeus.cogs import (
    format_extension_error,
    get_configured_cog_extensions,
    is_static_extension,
    normalize_extension_name,
    sync_commands_to_guild,
)
from amadeus.constants import OWNER_ID
from amadeus.logging_utils import log


class AmadeusOwner(commands.Cog):
    """
    Bot owner administration cog.

    Commands are visible to anyone with manage_guild permission but gated
    at runtime by AMADEUS_OWNER_ID — non-owners receive a rejection message.
    """

    amadeus_core = app_commands.Group(
        name="amadeus-core",
        description="Bot owner administration commands.",
        guild_only=True,
        default_permissions=discord.Permissions(manage_guild=True),
    )

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    def _extension_allowed(self, extension: str) -> bool:
        configured = set(get_configured_cog_extensions(skip_static=False))
        return extension.startswith("cogs.") and extension in configured

    async def _check_bot_owner(self, interaction: discord.Interaction) -> bool:
        if OWNER_ID is None or interaction.user.id != OWNER_ID:
            await interaction.response.send_message(
                "This command is restricted to the bot owner.",
                ephemeral=True,
            )
            return False

        return True

    # ========================================================
    # Cog management
    # ========================================================

    @amadeus_core.command(
        name="load-cog",
        description="Load a cog extension globally.",
    )
    @app_commands.describe(extension="Example: bouncer or cogs.bouncer")
    async def load_cog(
        self,
        interaction: discord.Interaction,
        extension: str,
    ):
        if not await self._check_bot_owner(interaction):
            return

        if interaction.guild is None:
            return

        extension = normalize_extension_name(extension)
        if not self._extension_allowed(extension):
            await interaction.response.send_message(
                f"`{extension}` is not in the configured `AMADEUS_COGS` allowlist.",
                ephemeral=True,
            )
            return
        log(f"OWNER // LOAD COG 『 {extension} 』", level="debug", logger_name="owner")

        if is_static_extension(extension):
            await interaction.response.send_message(
                f"`{extension}` is static and is already managed by `main.py`.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        try:
            await self.bot.load_extension(extension)
        except commands.ExtensionAlreadyLoaded:
            log(f"OWNER // ALREADY LOADED 『 {extension} 』", level="debug", logger_name="owner")
            await interaction.followup.send(
                f"`{extension}` is already loaded.",
                ephemeral=True,
            )
            return
        except commands.ExtensionNotFound:
            log(f"OWNER // NOT FOUND 『 {extension} 』", level="debug", logger_name="owner")
            await interaction.followup.send(
                f"`{extension}` was not found.",
                ephemeral=True,
            )
            return
        except commands.NoEntryPointError:
            log(f"OWNER // NO ENTRY POINT 『 {extension} 』", level="debug", logger_name="owner")
            await interaction.followup.send(
                f"`{extension}` does not have an async `setup(bot)` function.",
                ephemeral=True,
            )
            return
        except commands.ExtensionError as error:
            await interaction.followup.send(
                f"Failed to load `{extension}`:\n```txt\n{format_extension_error(error)}\n```",
                ephemeral=True,
            )
            return

        synced_count = await sync_commands_to_guild(self.bot, interaction.guild)

        await interaction.followup.send(
            f"Loaded `{extension}`.\n\nSynced **{synced_count}** command(s) to this server.",
            ephemeral=True,
        )

    @amadeus_core.command(
        name="unload-cog",
        description="Unload a cog extension globally.",
    )
    @app_commands.describe(extension="Example: bouncer or cogs.bouncer")
    async def unload_cog(
        self,
        interaction: discord.Interaction,
        extension: str,
    ):
        if not await self._check_bot_owner(interaction):
            return

        if interaction.guild is None:
            return

        extension = normalize_extension_name(extension)
        if not self._extension_allowed(extension):
            await interaction.response.send_message(
                f"`{extension}` is not in the configured `AMADEUS_COGS` allowlist.",
                ephemeral=True,
            )
            return

        if is_static_extension(extension):
            await interaction.response.send_message(
                f"`{extension}` is static and cannot be unloaded.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        if extension not in self.bot.extensions:
            log(f"OWNER // NOT LOADED 『 {extension} 』", level="debug", logger_name="owner")
            await interaction.followup.send(
                f"`{extension}` is not currently loaded.",
                ephemeral=True,
            )
            return

        try:
            await self.bot.unload_extension(extension)
        except commands.ExtensionError as error:
            await interaction.followup.send(
                f"Failed to unload `{extension}`:\n```txt\n{format_extension_error(error)}\n```",
                ephemeral=True,
            )
            return

        synced_count = await sync_commands_to_guild(self.bot, interaction.guild)

        await interaction.followup.send(
            f"Unloaded `{extension}`.\n\nSynced **{synced_count}** command(s) to this server.",
            ephemeral=True,
        )

    @amadeus_core.command(
        name="sync",
        description="Resync slash commands to this server.",
    )
    async def sync_cmd(self, interaction: discord.Interaction):
        if not await self._check_bot_owner(interaction):
            return

        if interaction.guild is None:
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        synced_count = await sync_commands_to_guild(self.bot, interaction.guild)

        await interaction.followup.send(
            f"Synced **{synced_count}** command(s) to this server.",
            ephemeral=True,
        )

    @amadeus_core.command(
        name="hot-reload",
        description="Reload all dynamic cog extensions and amadeus submodules.",
    )
    async def hot_reload(self, interaction: discord.Interaction):
        if not await self._check_bot_owner(interaction):
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        # Reload amadeus submodules first so cog reloads pick up fresh code.
        import amadeus.ascii
        import amadeus.captcha_utils
        import amadeus.cogs as amadeus_cogs_module
        import amadeus.constants
        import amadeus.database
        import amadeus.logging_utils
        import amadeus.models
        import amadeus.models.boost
        import amadeus.models.bouncer
        import amadeus.models.dm_flow
        import amadeus.models.guild
        import amadeus.models.honeypot
        import amadeus.permissions

        for mod in [
            amadeus.constants,
            amadeus.models.boost,
            amadeus.models.bouncer,
            amadeus.models.dm_flow,
            amadeus.models.guild,
            amadeus.models.honeypot,
            amadeus.models,
            amadeus.database,
            amadeus.permissions,
            amadeus.captcha_utils,
            amadeus.logging_utils,
            amadeus.ascii,
            amadeus_cogs_module,
        ]:
            importlib.reload(mod)

        reloaded: list[str] = []
        failed: list[str] = []

        log(f"OWNER // HOT RELOAD // EXTENSIONS 『 {len(list(self.bot.extensions.keys()))} 』", level="debug", logger_name="owner")

        for ext in list(self.bot.extensions.keys()):
            if is_static_extension(ext):
                continue
            if not self._extension_allowed(ext):
                failed.append(f"`{ext}`: not in configured `AMADEUS_COGS` allowlist")
                continue

            try:
                await self.bot.reload_extension(ext)
                reloaded.append(ext)
            except Exception as error:
                failed.append(f"`{ext}`: {error}")

        lines: list[str] = []

        if reloaded:
            lines.append("**Reloaded:**\n" + "\n".join(f"  `{e}`" for e in reloaded))

        if failed:
            lines.append("**Failed:**\n" + "\n".join(f"  {e}" for e in failed))

        if not reloaded and not failed:
            lines.append("No dynamic extensions to reload.")

        await interaction.followup.send("\n\n".join(lines), ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(AmadeusOwner(bot))
