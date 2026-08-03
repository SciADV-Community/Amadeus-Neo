import discord
from discord import app_commands
from discord.ext import commands

from amadeus.database import ConfigStore
from amadeus.discord_utils import escape_untrusted_text
from amadeus.logging_utils import log
from amadeus.permissions import require_amadeus_access
from amadeus.play_store import PlayStore
from cogs.amadeus_admin import attach_amadeus_subgroup, detach_amadeus_subgroup
from cogs.play import (
    MODULE_NAME,
    find_forum_tag,
    game_name_error,
    missing_play_forum_permissions,
)

AUTO_ARCHIVE_CHOICES = [
    app_commands.Choice(name="Forum default", value=0),
    app_commands.Choice(name="1 hour", value=60),
    app_commands.Choice(name="24 hours", value=1440),
    app_commands.Choice(name="3 days", value=4320),
    app_commands.Choice(name="7 days", value=10080),
]
VALID_AUTO_ARCHIVE_DURATIONS = {choice.value for choice in AUTO_ARCHIVE_CHOICES}


def tag_name_error(name: str) -> str | None:
    name = name.strip()
    if not name:
        return "Forum tag name cannot be empty."
    error = game_name_error(name)
    if error is None:
        return None
    return error.replace("Game name", "Forum tag name")


def archive_duration_label(minutes: int | None) -> str:
    labels = {
        None: "Forum default",
        60: "1 hour",
        1440: "24 hours",
        4320: "3 days",
        10080: "7 days",
    }
    return labels.get(minutes, f"{minutes} minutes")


class PlayAdmin(commands.Cog):
    """
    Admin cog for the play module.

    Commands: /amadeus play set-forum, add-game, remove-game, list-games, archive-duration, config
    """

    play = app_commands.Group(
        name="play",
        description="Visual Novel playthrough admin commands.",
    )

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.play_store = PlayStore()
        self.module_store = ConfigStore()

    def cog_unload(self):
        detach_amadeus_subgroup(self.bot, self.play.name)
        self.play_store.close()
        self.module_store.close()

    async def game_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        if interaction.guild_id is None:
            return []

        return [
            app_commands.Choice(name=game.display_name, value=game.key)
            for game in self.play_store.search_games(interaction.guild_id, current)
        ]

    async def _get_configured_forum(self, guild: discord.Guild) -> discord.ForumChannel | None:
        config = self.play_store.get_config(guild.id)
        if config is None or config.forum_channel_id is None:
            return None

        channel = guild.get_channel(config.forum_channel_id)
        if channel is None:
            try:
                channel = await guild.fetch_channel(config.forum_channel_id)
            except (discord.Forbidden, discord.HTTPException, discord.NotFound):
                return None

        return channel if isinstance(channel, discord.ForumChannel) else None

    # ========================================================
    # /amadeus play set-forum
    # ========================================================

    @play.command(
        name="set-forum",
        description="Set the forum channel used for playthrough posts.",
    )
    @app_commands.describe(channel="Forum channel where playthrough posts should be created.")
    async def play_set_forum(
        self,
        interaction: discord.Interaction,
        channel: discord.ForumChannel,
    ) -> None:
        config = await require_amadeus_access(interaction, self.module_store)
        if config is None or interaction.guild is None:
            return

        self.play_store.set_forum_channel(interaction.guild.id, channel.id)
        log(
            f"PLAY // FORUM SET 『 CHANNEL {channel.id} 』 GUILD 『 {interaction.guild.id} 』",
            level="debug",
            logger_name="play",
        )

        warning = ""
        bot_member = interaction.guild.me
        if bot_member is None:
            warning = "\n\nCould not read my server member data for a permission check."
        else:
            missing_permissions = missing_play_forum_permissions(channel, bot_member)
            if missing_permissions:
                needed = ", ".join(f"**{permission}**" for permission in missing_permissions)
                warning = f"\n\nMissing permissions in {channel.mention}: {needed}."

        await interaction.response.send_message(
            f"Playthrough forum set to {channel.mention}.{warning}",
            ephemeral=True,
        )

    # ========================================================
    # /amadeus play add-game
    # ========================================================

    @play.command(
        name="add-game",
        description="Add or update a game available from /play.",
    )
    @app_commands.describe(
        name="Game name shown to users.",
        tag_name="Forum tag to use. Defaults to the game name.",
    )
    async def play_add_game(
        self,
        interaction: discord.Interaction,
        name: str,
        tag_name: str | None = None,
    ) -> None:
        config = await require_amadeus_access(interaction, self.module_store)
        if config is None or interaction.guild is None:
            return

        name = name.strip()
        error = game_name_error(name)
        if error is not None:
            await interaction.response.send_message(error, ephemeral=True)
            return

        forum = await self._get_configured_forum(interaction.guild)
        if forum is None:
            await interaction.response.send_message(
                "Set a playthrough forum first with `/amadeus play set-forum`.",
                ephemeral=True,
            )
            return

        desired_tag_name = (tag_name or name).strip()
        error = tag_name_error(desired_tag_name)
        if error is not None:
            await interaction.response.send_message(error, ephemeral=True)
            return

        tag = find_forum_tag(forum, name=desired_tag_name)
        tag_created = False

        if tag is None:
            try:
                tag = await forum.create_tag(
                    name=desired_tag_name,
                    reason=f"Playthrough game tag added by {interaction.user} ({interaction.user.id})",
                )
                tag_created = True
            except discord.Forbidden:
                await interaction.response.send_message(
                    f"I could not create the forum tag **{escape_untrusted_text(desired_tag_name)}**. "
                    "Check my Manage Channels permission.",
                    ephemeral=True,
                )
                return
            except discord.HTTPException as e:
                await interaction.response.send_message(
                    f"Discord rejected the forum tag **{escape_untrusted_text(desired_tag_name)}**: `{e}`",
                    ephemeral=True,
                )
                return

        game = self.play_store.save_game(interaction.guild.id, name, tag.id)
        log(
            f"PLAY // GAME SAVED 『 {game.key} 』 TAG 『 {tag.id} 』 GUILD 『 {interaction.guild.id} 』",
            level="debug",
            logger_name="play",
        )

        tag_note = "created and linked" if tag_created else "linked"
        await interaction.response.send_message(
            f"Added **{escape_untrusted_text(game.display_name)}** to `/play`; "
            f"forum tag **{escape_untrusted_text(tag.name)}** {tag_note}.",
            ephemeral=True,
        )

    # ========================================================
    # /amadeus play remove-game
    # ========================================================

    @play.command(
        name="remove-game",
        description="Remove a game from /play.",
    )
    @app_commands.describe(game="Configured game to remove.")
    @app_commands.autocomplete(game=game_autocomplete)
    async def play_remove_game(
        self,
        interaction: discord.Interaction,
        game: str,
    ) -> None:
        config = await require_amadeus_access(interaction, self.module_store)
        if config is None or interaction.guild is None:
            return

        existing = self.play_store.get_game(interaction.guild.id, game, enabled_only=False)
        if existing is None:
            await interaction.response.send_message(
                "That game is not configured for `/play`.",
                ephemeral=True,
            )
            return

        self.play_store.remove_game(interaction.guild.id, game)
        log(
            f"PLAY // GAME REMOVED 『 {existing.key} 』 GUILD 『 {interaction.guild.id} 』",
            level="debug",
            logger_name="play",
        )

        await interaction.response.send_message(
            f"Removed **{escape_untrusted_text(existing.display_name)}** from `/play`.",
            ephemeral=True,
        )

    # ========================================================
    # /amadeus play list-games
    # ========================================================

    @play.command(
        name="list-games",
        description="List games configured for /play.",
    )
    async def play_list_games(self, interaction: discord.Interaction) -> None:
        config = await require_amadeus_access(interaction, self.module_store)
        if config is None or interaction.guild is None:
            return

        games = self.play_store.list_games(interaction.guild.id)
        forum = await self._get_configured_forum(interaction.guild)

        if not games:
            await interaction.response.send_message(
                "No games configured. Add one with `/amadeus play add-game`.",
                ephemeral=True,
            )
            return

        lines = []
        for game in games:
            tag_text = "No tag"
            if forum is not None:
                tag = find_forum_tag(forum, tag_id=game.forum_tag_id)
                tag_text = f"Tag: **{escape_untrusted_text(tag.name)}**" if tag else f"Missing tag `{game.forum_tag_id}`"
            elif game.forum_tag_id is not None:
                tag_text = f"Tag ID: `{game.forum_tag_id}`"

            lines.append(f"**{escape_untrusted_text(game.display_name)}** — {tag_text}")

        embed = discord.Embed(
            title="Playthrough Games",
            description="\n".join(lines),
            color=discord.Color.blurple(),
        )

        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ========================================================
    # /amadeus play archive-duration
    # ========================================================

    @play.command(
        name="archive-duration",
        description="Set how long inactive playthrough posts remain visible.",
    )
    @app_commands.describe(minutes="Inactive duration before Discord auto-archives the post.")
    @app_commands.choices(minutes=AUTO_ARCHIVE_CHOICES)
    async def play_archive_duration(
        self,
        interaction: discord.Interaction,
        minutes: int,
    ) -> None:
        config = await require_amadeus_access(interaction, self.module_store)
        if config is None or interaction.guild is None:
            return

        if minutes not in VALID_AUTO_ARCHIVE_DURATIONS:
            await interaction.response.send_message(
                "Auto-archive duration must be Forum default, 1 hour, 24 hours, 3 days, or 7 days.",
                ephemeral=True,
            )
            return

        stored_minutes = None if minutes == 0 else minutes
        self.play_store.set_auto_archive_duration(interaction.guild.id, stored_minutes)
        log(
            f"PLAY // ARCHIVE DURATION SET 『 {stored_minutes} 』 GUILD 『 {interaction.guild.id} 』",
            level="debug",
            logger_name="play",
        )

        await interaction.response.send_message(
            f"Playthrough auto-archive duration set to **{archive_duration_label(stored_minutes)}**.",
            ephemeral=True,
        )

    # ========================================================
    # /amadeus play config
    # ========================================================

    @play.command(
        name="config",
        description="Show playthrough module configuration.",
    )
    async def play_config(self, interaction: discord.Interaction) -> None:
        config = await require_amadeus_access(interaction, self.module_store)
        if config is None or interaction.guild is None:
            return

        play_config = self.play_store.get_config(interaction.guild.id)
        forum = await self._get_configured_forum(interaction.guild)
        games = self.play_store.list_games(interaction.guild.id)
        enabled = self.module_store.is_module_enabled(interaction.guild.id, MODULE_NAME)

        embed = discord.Embed(
            title="Playthrough Configuration",
            color=discord.Color.blurple(),
        )
        embed.add_field(
            name="Module",
            value="Enabled" if enabled else "Disabled",
            inline=True,
        )
        embed.add_field(
            name="Forum",
            value=forum.mention if forum is not None else "Not configured",
            inline=True,
        )
        embed.add_field(
            name="Auto-archive",
            value=archive_duration_label(play_config.auto_archive_duration if play_config else None),
            inline=True,
        )
        embed.add_field(
            name="Games",
            value=str(len(games)),
            inline=True,
        )

        bot_member = interaction.guild.me
        if forum is not None and bot_member is not None:
            missing_permissions = missing_play_forum_permissions(forum, bot_member)
            permission_text = (
                "All required permissions present."
                if not missing_permissions
                else "\n".join(f"- {permission}" for permission in missing_permissions)
            )
            embed.add_field(name="Forum permissions", value=permission_text, inline=False)

        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot):
    cog = PlayAdmin(bot)
    attach_amadeus_subgroup(bot, cog, cog.play)

    try:
        await bot.add_cog(cog)
    except Exception:
        detach_amadeus_subgroup(bot, cog.play.name)
        raise
