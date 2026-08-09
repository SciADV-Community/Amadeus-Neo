from dataclasses import dataclass

import discord
from discord import app_commands
from discord.ext import commands

from amadeus.database import ConfigStore
from amadeus.discord_utils import escape_untrusted_text
from amadeus.logging_utils import log
from amadeus.models.play import PlayGame
from amadeus.module_guard import require_module_enabled_for_interaction
from amadeus.permissions import require_amadeus_access
from amadeus.play_store import PlayStore
from cogs.amadeus_admin import attach_amadeus_subgroup, detach_amadeus_subgroup
from cogs.play import (
    MODULE_NAME,
    Play as PlayCog,
    find_forum_tag,
    game_name_error,
    missing_play_lock_sweep_permissions,
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
MAX_FORUM_TAG_NAME_LENGTH = 20
MAX_REMOVE_FORUM_OPTIONS = 25


@dataclass(frozen=True)
class ConfiguredPlayForum:
    forum_id: int
    label: str
    reference: str
    games: tuple[PlayGame, ...]
    is_default: bool = False


def tag_name_error(name: str) -> str | None:
    name = name.strip()
    if not name:
        return "Forum tag name cannot be empty."
    if len(name) > MAX_FORUM_TAG_NAME_LENGTH:
        return f"Forum tag name must be {MAX_FORUM_TAG_NAME_LENGTH} characters or fewer."
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


def parse_channel_id(value: str) -> int | None:
    value = value.strip()
    if value.startswith("<#") and value.endswith(">"):
        value = value[2:-1]
    if not value.isdigit():
        return None
    return int(value)


def _remove_forum_confirmation_content(target: ConfiguredPlayForum) -> str:
    game_lines = [
        f"- {game.sort_order}. {escape_untrusted_text(game.display_name)}"
        for game in target.games
    ] or ["- No games configured for this forum."]
    games_text = "\n".join(game_lines)
    default_note = "\nDefault forum: yes" if target.is_default else ""
    return (
        "Are you sure you want to delete this Forum channel?\n\n"
        f"Forum: {target.reference}{default_note}\n"
        "This removes these games from Amadeus play configuration first.\n\n"
        "Configured games for this channel:\n"
        f"{games_text}"
    )


def _delete_forum_channel_confirmation_content(
    *,
    forum_reference: str,
    removed_games: list[PlayGame],
) -> str:
    return (
        "ARE YOU SURE?\n"
        "Removal will attempt to delete the channel and all contained threads."
    )


def _remove_forum_placeholder_content() -> str:
    return (
        "Select a configured playthrough forum to review the games that will be "
        "removed."
    )


class _RemoveForumSelect(discord.ui.Select):
    def __init__(self, forums: list[ConfiguredPlayForum]) -> None:
        super().__init__(
            placeholder="Configured forum",
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(
                    label=forum.label[:100],
                    value=str(forum.forum_id),
                    description=f"{len(forum.games)} configured game(s)",
                )
                for forum in forums[:MAX_REMOVE_FORUM_OPTIONS]
            ],
            row=0,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if not isinstance(view, _RemoveForumConfirmationView):
            await interaction.response.defer()
            return

        try:
            view.selected_forum_id = int(self.values[0])
        except (IndexError, ValueError):
            await interaction.response.defer()
            return

        for option in self.options:
            option.default = option.value == str(view.selected_forum_id)

        target = view.selected_target
        view.set_confirm_enabled(target is not None)
        await interaction.response.edit_message(
            content=(
                _remove_forum_confirmation_content(target)
                if target is not None
                else "That playthrough forum is no longer available."
            ),
            view=view,
        )


class _RemoveForumConfirmationView(discord.ui.View):
    def __init__(
        self,
        *,
        cog: "PlayAdmin",
        requester_id: int,
        guild_id: int,
        forums: list[ConfiguredPlayForum],
    ) -> None:
        super().__init__(timeout=180)
        self.cog = cog
        self.requester_id = requester_id
        self.guild_id = guild_id
        self.forums = {
            forum.forum_id: forum
            for forum in forums[:MAX_REMOVE_FORUM_OPTIONS]
        }
        self.selected_forum_id: int | None = None

        if forums:
            self.add_item(_RemoveForumSelect(forums))
        self.set_confirm_enabled(False)

    @property
    def selected_target(self) -> ConfiguredPlayForum | None:
        if self.selected_forum_id is None:
            return None
        return self.forums.get(self.selected_forum_id)

    def set_confirm_enabled(self, enabled: bool) -> None:
        for child in self.children:
            if isinstance(child, discord.ui.Button) and child.label == "Yes":
                child.disabled = not enabled

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message(
                "Only the user who started this confirmation can use these controls.",
                ephemeral=True,
            )
            return False
        if interaction.guild_id != self.guild_id:
            await interaction.response.send_message(
                "This confirmation is no longer valid.",
                ephemeral=True,
            )
            return False
        return True

    @discord.ui.button(label="Yes", style=discord.ButtonStyle.danger, row=1)
    async def yes(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        target = self.selected_target
        if target is None:
            await interaction.response.edit_message(
                content=_remove_forum_placeholder_content(),
                view=self,
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        self.stop()
        await self.cog.remove_play_forum_from_confirmation(
            interaction,
            forum_id=target.forum_id,
        )

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary, row=1)
    async def cancel(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        await interaction.response.edit_message(
            content="Playthrough forum removal cancelled.",
            view=None,
        )
        self.stop()


class _DeleteForumChannelConfirmationView(discord.ui.View):
    def __init__(
        self,
        *,
        cog: "PlayAdmin",
        requester_id: int,
        guild_id: int,
        forum_id: int,
        forum_reference: str,
    ) -> None:
        super().__init__(timeout=180)
        self.cog = cog
        self.requester_id = requester_id
        self.guild_id = guild_id
        self.forum_id = forum_id
        self.forum_reference = forum_reference

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message(
                "Only the user who started this confirmation can use these controls.",
                ephemeral=True,
            )
            return False
        if interaction.guild_id != self.guild_id:
            await interaction.response.send_message(
                "This confirmation is no longer valid.",
                ephemeral=True,
            )
            return False
        return True

    @discord.ui.button(label="Delete Forum", style=discord.ButtonStyle.danger, row=0)
    async def delete_forum(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        self.stop()
        await self.cog.delete_play_forum_channel_from_confirmation(
            interaction,
            forum_id=self.forum_id,
            forum_reference=self.forum_reference,
        )

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary, row=0)
    async def cancel(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        await interaction.response.edit_message(
            content=(
                "Discord forum deletion cancelled. Amadeus play configuration "
                "changes were already applied."
            ),
            view=None,
        )
        self.stop()


class PlayAdmin(commands.Cog):
    """
    Admin cog for the play module.

    Commands: /amadeus play set-forum, remove-forum, add-game, set-order,
    remove-game, list-games, archive-duration, auto-archive, config
    """

    play = app_commands.Group(
        name="play",
        description="Visual Novel playthrough admin commands.",
    )

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.play_store = PlayStore()
        self.module_store = ConfigStore()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await require_module_enabled_for_interaction(
            interaction, self.module_store, MODULE_NAME
        )

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

    async def configured_forum_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        if interaction.guild is None:
            return []

        config = self.play_store.get_config(interaction.guild.id)
        default_forum_id = config.forum_channel_id if config else None
        forum_game_counts: dict[int, int] = {}

        for game in self.play_store.list_games(interaction.guild.id):
            forum_id = game.forum_channel_id or default_forum_id
            if forum_id is None:
                continue
            forum_game_counts[forum_id] = forum_game_counts.get(forum_id, 0) + 1

        normalized_current = current.strip().casefold()
        choices: list[app_commands.Choice[str]] = []

        for forum_id, game_count in sorted(forum_game_counts.items()):
            channel = interaction.guild.get_channel(forum_id)
            channel_name = getattr(channel, "name", None)
            label = (
                f"#{channel_name} ({game_count} games)"
                if channel_name
                else f"Forum {forum_id} ({game_count} games)"
            )
            searchable = f"{label} {forum_id}".casefold()
            if normalized_current and normalized_current not in searchable:
                continue

            choices.append(
                app_commands.Choice(
                    name=label[:100],
                    value=str(forum_id),
                )
            )
            if len(choices) >= 25:
                break

        return choices

    async def _get_forum_channel(
        self,
        guild: discord.Guild,
        channel_id: int | None,
    ) -> discord.ForumChannel | None:
        if channel_id is None:
            return None

        channel = guild.get_channel(channel_id)
        if channel is None:
            try:
                channel = await guild.fetch_channel(channel_id)
            except (discord.Forbidden, discord.HTTPException, discord.NotFound):
                return None

        return channel if isinstance(channel, discord.ForumChannel) else None

    async def _get_default_forum(self, guild: discord.Guild) -> discord.ForumChannel | None:
        config = self.play_store.get_config(guild.id)
        return await self._get_forum_channel(
            guild,
            config.forum_channel_id if config else None,
        )

    async def _configured_play_forum_targets(
        self,
        guild: discord.Guild,
    ) -> list[ConfiguredPlayForum]:
        config = self.play_store.get_config(guild.id)
        default_forum_id = config.forum_channel_id if config else None
        targets: list[ConfiguredPlayForum] = []
        games_by_forum_id: dict[int, list[PlayGame]] = {}

        for game in self.play_store.list_games(guild.id):
            forum_id = game.forum_channel_id or default_forum_id
            if forum_id is None:
                continue
            games_by_forum_id.setdefault(forum_id, []).append(game)

        for forum_id in sorted(games_by_forum_id):
            forum = await self._get_forum_channel(guild, forum_id)
            if forum is None:
                label = f"Forum {forum_id}"
                reference = f"`{forum_id}`"
            else:
                label = f"#{forum.name}"
                reference = forum.mention

            targets.append(
                ConfiguredPlayForum(
                    forum_id=forum_id,
                    label=label,
                    reference=reference,
                    games=tuple(games_by_forum_id[forum_id]),
                    is_default=forum_id == default_forum_id,
                )
            )

        return targets

    # ========================================================
    # /amadeus play set-forum
    # ========================================================

    @play.command(
        name="set-forum",
        description="Set the default forum channel used when adding games.",
    )
    @app_commands.describe(channel="Default forum channel for newly added games.")
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
            f"Default playthrough forum set to {channel.mention}.{warning}",
            ephemeral=True,
        )

    # ========================================================
    # /amadeus play remove-forum
    # ========================================================

    @play.command(
        name="remove-forum",
        description="Remove a configured playthrough forum.",
    )
    async def play_remove_forum(
        self,
        interaction: discord.Interaction,
    ) -> None:
        config = await require_amadeus_access(interaction, self.module_store)
        if config is None or interaction.guild is None:
            return

        forums = await self._configured_play_forum_targets(interaction.guild)
        if not forums:
            await interaction.response.send_message(
                "No playthrough forums are configured.",
                ephemeral=True,
            )
            return

        view = _RemoveForumConfirmationView(
            cog=self,
            requester_id=interaction.user.id,
            guild_id=interaction.guild.id,
            forums=forums,
        )
        await interaction.response.send_message(
            _remove_forum_placeholder_content(),
            view=view,
            ephemeral=True,
        )

    async def remove_play_forum_from_confirmation(
        self,
        interaction: discord.Interaction,
        *,
        forum_id: int,
    ) -> None:
        if interaction.guild is None:
            await interaction.edit_original_response(
                content="This can only be used inside a server.",
                view=None,
            )
            return

        current_forums = await self._configured_play_forum_targets(interaction.guild)
        target = next(
            (forum for forum in current_forums if forum.forum_id == forum_id),
            None,
        )
        if target is None:
            await interaction.edit_original_response(
                content="That playthrough forum is no longer configured.",
                view=None,
            )
            return

        removed_games, removed_default = self.play_store.remove_forum(
            interaction.guild.id,
            forum_id,
        )
        delete_view = _DeleteForumChannelConfirmationView(
            cog=self,
            requester_id=interaction.user.id,
            guild_id=interaction.guild.id,
            forum_id=forum_id,
            forum_reference=target.reference,
        )

        log(
            f"PLAY // FORUM REMOVED 『 CHANNEL {forum_id} 』 "
            f"DEFAULT {removed_default} GAMES {[game.key for game in removed_games]} "
            f"GUILD 『 {interaction.guild.id} 』",
            level="debug",
            logger_name="play",
        )

        await interaction.edit_original_response(
            content=_delete_forum_channel_confirmation_content(
                forum_reference=target.reference,
                removed_games=removed_games,
            ),
            view=delete_view,
        )

    async def delete_play_forum_channel_from_confirmation(
        self,
        interaction: discord.Interaction,
        *,
        forum_id: int,
        forum_reference: str,
    ) -> None:
        if interaction.guild is None:
            await interaction.edit_original_response(
                content="This can only be used inside a server.",
                view=None,
            )
            return

        forum = await self._get_forum_channel(interaction.guild, forum_id)
        if forum is None:
            await interaction.edit_original_response(
                content=(
                    f"{forum_reference} was removed from Amadeus play configuration, "
                    "but the Discord forum channel could not be found."
                ),
                view=None,
            )
            return

        try:
            await forum.delete(
                reason=(
                    f"Playthrough forum deleted by "
                    f"{interaction.user} ({interaction.user.id})"
                )
            )
        except discord.Forbidden:
            await interaction.edit_original_response(
                content=(
                    f"{forum_reference} was removed from Amadeus play configuration, "
                    "but I could not delete the Discord forum channel. "
                    "Check my Manage Channels permission."
                ),
                view=None,
            )
            return
        except discord.HTTPException as e:
            await interaction.edit_original_response(
                content=(
                    f"{forum_reference} was removed from Amadeus play configuration, "
                    f"but Discord rejected the forum channel deletion: `{e}`"
                ),
                view=None,
            )
            return

        log(
            f"PLAY // FORUM CHANNEL DELETED 『 CHANNEL {forum_id} 』 "
            f"GUILD 『 {interaction.guild.id} 』",
            level="debug",
            logger_name="play",
        )

        await interaction.edit_original_response(
            content=f"Deleted Discord forum channel {forum_reference}.",
            view=None,
        )

    # ========================================================
    # /amadeus play add-game
    # ========================================================

    @play.command(
        name="add-game",
        description="Add or update a game available from /play new.",
    )
    @app_commands.describe(
        name="Game name shown to users.",
        forum="Forum channel for this game. Defaults to `/amadeus play set-forum`.",
        tag_name="Forum tag to use. Defaults to the game name.",
        order="Position in `/play new`. Defaults to the end.",
    )
    async def play_add_game(
        self,
        interaction: discord.Interaction,
        name: str,
        forum: discord.ForumChannel | None = None,
        tag_name: str | None = None,
        order: int | None = None,
    ) -> None:
        config = await require_amadeus_access(interaction, self.module_store)
        if config is None or interaction.guild is None:
            return

        if order is not None and order < 1:
            await interaction.response.send_message(
                "Game order must be **1** or higher.",
                ephemeral=True,
            )
            return

        name = name.strip()
        error = game_name_error(name)
        if error is not None:
            await interaction.response.send_message(error, ephemeral=True)
            return

        forum = forum or await self._get_default_forum(interaction.guild)
        if forum is None:
            await interaction.response.send_message(
                "Choose a **forum** for this game or set a default first with "
                "`/amadeus play set-forum`.",
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
                    reason=(
                        f"Playthrough game tag added by "
                        f"{interaction.user} ({interaction.user.id})"
                    ),
                )
                tag_created = True
            except discord.Forbidden:
                await interaction.response.send_message(
                    f"I could not create the forum tag "
                    f"**{escape_untrusted_text(desired_tag_name)}**. "
                    "Check my Manage Channels permission.",
                    ephemeral=True,
                )
                return
            except discord.HTTPException as e:
                await interaction.response.send_message(
                    f"Discord rejected the forum tag "
                    f"**{escape_untrusted_text(desired_tag_name)}**: `{e}`",
                    ephemeral=True,
                )
                return

        game = self.play_store.save_game(
            interaction.guild.id,
            name,
            forum.id,
            tag.id,
            sort_order=order,
        )
        log(
            f"PLAY // GAME SAVED 『 {game.key} 』 FORUM 『 {forum.id} 』 TAG 『 {tag.id} 』 "
            f"ORDER {game.sort_order} GUILD 『 {interaction.guild.id} 』",
            level="debug",
            logger_name="play",
        )

        tag_note = "created and linked" if tag_created else "linked"
        await interaction.response.send_message(
            f"Added **{escape_untrusted_text(game.display_name)}** to `/play new`; "
            f"order **{game.sort_order}**; forum {forum.mention}; "
            f"tag **{escape_untrusted_text(tag.name)}** {tag_note}.",
            ephemeral=True,
        )

    # ========================================================
    # /amadeus play set-order
    # ========================================================

    @play.command(
        name="set-order",
        description="Set a game's position in /play new.",
    )
    @app_commands.describe(
        game="Configured game to move.",
        order="Position in `/play new`.",
    )
    @app_commands.autocomplete(game=game_autocomplete)
    async def play_set_order(
        self,
        interaction: discord.Interaction,
        game: str,
        order: int,
    ) -> None:
        config = await require_amadeus_access(interaction, self.module_store)
        if config is None or interaction.guild is None:
            return

        if order < 1:
            await interaction.response.send_message(
                "Game order must be **1** or higher.",
                ephemeral=True,
            )
            return

        updated = self.play_store.set_game_order(
            interaction.guild.id,
            game,
            order,
        )
        if updated is None:
            await interaction.response.send_message(
                "That game is not configured for `/play new`.",
                ephemeral=True,
            )
            return

        log(
            f"PLAY // GAME ORDER SET 『 {updated.key} 』 ORDER {updated.sort_order} "
            f"GUILD 『 {interaction.guild.id} 』",
            level="debug",
            logger_name="play",
        )

        await interaction.response.send_message(
            f"Set **{escape_untrusted_text(updated.display_name)}** to order "
            f"**{updated.sort_order}** in `/play new`.",
            ephemeral=True,
        )

    # ========================================================
    # /amadeus play remove-game
    # ========================================================

    @play.command(
        name="remove-game",
        description="Remove a game from /play new.",
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
                "That game is not configured for `/play new`.",
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
            f"Removed **{escape_untrusted_text(existing.display_name)}** from `/play new`.",
            ephemeral=True,
        )

    # ========================================================
    # /amadeus play list-games
    # ========================================================

    @play.command(
        name="list-games",
        description="List games configured for /play new.",
    )
    async def play_list_games(self, interaction: discord.Interaction) -> None:
        config = await require_amadeus_access(interaction, self.module_store)
        if config is None or interaction.guild is None:
            return

        games = self.play_store.list_games(interaction.guild.id)
        play_config = self.play_store.get_config(interaction.guild.id)
        default_forum_id = play_config.forum_channel_id if play_config else None

        if not games:
            await interaction.response.send_message(
                "No games configured. Add one with `/amadeus play add-game`.",
                ephemeral=True,
            )
            return

        lines = []
        for game in games:
            forum_id = game.forum_channel_id or default_forum_id
            forum = await self._get_forum_channel(interaction.guild, forum_id)
            if forum is None:
                forum_text = f"Missing forum `{forum_id}`" if forum_id else "No forum"
            else:
                forum_text = f"Forum: {forum.mention}"

            tag_text = "No tag"
            if forum is not None:
                tag = find_forum_tag(forum, tag_id=game.forum_tag_id)
                tag_text = (
                    f"Tag: **{escape_untrusted_text(tag.name)}**"
                    if tag
                    else f"Missing tag `{game.forum_tag_id}`"
                )
            elif game.forum_tag_id is not None:
                tag_text = f"Tag ID: `{game.forum_tag_id}`"

            lines.append(
                f"**{game.sort_order}. {escape_untrusted_text(game.display_name)}** "
                f"— {forum_text} — {tag_text}"
            )

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
            f"Playthrough auto-archive duration set to "
            f"**{archive_duration_label(stored_minutes)}**.",
            ephemeral=True,
        )

    # ========================================================
    # /amadeus play auto-archive
    # ========================================================

    @play.command(
        name="auto-archive",
        description="Lock eligible archived playthrough posts in a configured forum.",
    )
    @app_commands.describe(
        configured_channel="Configured playthrough forum channel to sweep.",
        grace_days=(
            "Optional inactive grace period before locking. "
            "Default: environment setting."
        ),
    )
    @app_commands.autocomplete(configured_channel=configured_forum_autocomplete)
    async def play_auto_archive(
        self,
        interaction: discord.Interaction,
        configured_channel: str,
        grace_days: int | None = None,
    ) -> None:
        config = await require_amadeus_access(interaction, self.module_store)
        if config is None or interaction.guild is None:
            return

        if grace_days is not None and not 0 <= grace_days <= 365:
            await interaction.response.send_message(
                "Grace days must be between 0 and 365.",
                ephemeral=True,
            )
            return

        configured_channel_id = parse_channel_id(configured_channel)
        if configured_channel_id is None:
            await interaction.response.send_message(
                "Choose a configured playthrough forum from autocomplete.",
                ephemeral=True,
            )
            return

        play_cog = self.bot.get_cog("Play")
        if not isinstance(play_cog, PlayCog):
            await interaction.response.send_message(
                "The member-facing play cog is not loaded, so I cannot run the "
                "archived lock sweep.",
                ephemeral=True,
            )
            return

        configured_tag_ids = play_cog.configured_play_tag_ids_for_forum(
            interaction.guild.id,
            configured_channel_id,
        )
        if not configured_tag_ids:
            await interaction.response.send_message(
                f"`{configured_channel_id}` is not configured for any playthrough games.",
                ephemeral=True,
            )
            return

        resolved_channel = await self._get_forum_channel(
            interaction.guild,
            configured_channel_id,
        )
        if resolved_channel is None:
            await interaction.response.send_message(
                "That configured playthrough forum is missing or is no longer a forum.",
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

        missing_permissions = missing_play_lock_sweep_permissions(
            resolved_channel,
            bot_member,
        )
        if missing_permissions:
            needed = ", ".join(f"**{permission}**" for permission in missing_permissions)
            await interaction.response.send_message(
                f"I cannot scan and lock archived posts in {resolved_channel.mention}.\n\n"
                f"Grant me: {needed}.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        try:
            result = await play_cog.run_archived_playthrough_lock_sweep(
                interaction.guild,
                resolved_channel,
                grace_days=grace_days,
            )
        except discord.Forbidden:
            await interaction.edit_original_response(
                content=(
                    f"Discord rejected the archived post scan for "
                    f"{resolved_channel.mention}. Check my forum permissions."
                ),
            )
            return
        except discord.HTTPException as e:
            await interaction.edit_original_response(
                content=f"Discord rejected the archived lock sweep: `{e}`",
            )
            return
        except OSError as e:
            await interaction.edit_original_response(
                content=f"I could not write the archived lock sweep checkpoint: `{e}`",
            )
            return

        if result is None:
            await interaction.edit_original_response(
                content=(
                    f"{resolved_channel.mention} is not configured for any "
                    "playthrough games."
                ),
            )
            return

        scanned_count, locked_count = result
        effective_grace_days = play_cog.archived_lock_grace_days(grace_days)
        log(
            f"PLAY // MANUAL ARCHIVED LOCK SWEEP 『 GUILD {interaction.guild.id} 』 "
            f"FORUM 『 {resolved_channel.id} 』 SCANNED {scanned_count} "
            f"LOCKED {locked_count} GRACE_DAYS {effective_grace_days}",
            level="debug",
            logger_name="play",
        )

        await interaction.edit_original_response(
            content=(
                f"Archived playthrough sweep complete for {resolved_channel.mention}.\n"
                f"Scanned **{scanned_count}** archived posts and locked "
                f"**{locked_count}** eligible posts.\n"
                f"Grace period: **{effective_grace_days} day"
                f"{'s' if effective_grace_days != 1 else ''}**."
            ),
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
        default_forum = await self._get_default_forum(interaction.guild)
        games = self.play_store.list_games(interaction.guild.id)
        enabled = self.module_store.is_module_enabled(interaction.guild.id, MODULE_NAME)
        configured_forum_ids = {
            game.forum_channel_id
            for game in games
            if game.forum_channel_id is not None
        }
        if play_config and play_config.forum_channel_id is not None:
            configured_forum_ids.add(play_config.forum_channel_id)

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
            name="Default forum",
            value=(
                default_forum.mention
                if default_forum is not None
                else "Not configured"
            ),
            inline=True,
        )
        embed.add_field(
            name="Auto-archive",
            value=archive_duration_label(
                play_config.auto_archive_duration if play_config else None
            ),
            inline=True,
        )
        embed.add_field(
            name="Games",
            value=str(len(games)),
            inline=True,
        )
        embed.add_field(
            name="Forums",
            value=str(len(configured_forum_ids)),
            inline=True,
        )

        bot_member = interaction.guild.me
        if configured_forum_ids and bot_member is not None:
            permission_lines = []
            for forum_id in sorted(configured_forum_ids):
                forum = await self._get_forum_channel(interaction.guild, forum_id)
                if forum is None:
                    permission_lines.append(f"`{forum_id}`: missing or not a forum")
                    continue

                missing_permissions = missing_play_forum_permissions(forum, bot_member)
                permission_lines.append(
                    f"{forum.mention}: OK"
                    if not missing_permissions
                    else f"{forum.mention}: missing {', '.join(missing_permissions)}"
                )

            permission_text = "\n".join(permission_lines[:10])
            if len(permission_lines) > 10:
                permission_text += f"\n...and {len(permission_lines) - 10} more."
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
