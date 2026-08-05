import re

import discord
from discord import app_commands
from discord.ext import commands

from amadeus.database import ConfigStore
from amadeus.discord_utils import NO_MENTIONS, escape_untrusted_text
from amadeus.logging_utils import log
from amadeus.models.play import PlayGame
from amadeus.module_guard import require_module_enabled_for_interaction
from amadeus.play_store import PlayStore

MODULE_NAME = "play"
SPOILER_CHANNEL_FLAG = 1 << 21
MAX_PLAY_THREAD_NAME_LENGTH = 100
MAX_GAME_NAME_LENGTH = 80
MAX_FORUM_THREAD_TAGS = 5
INTRO_MESSAGE_TEMPLATE = (
    "This channel is your personal {kind} channel for {game}. "
    "Upon completion the channel will automatically archive itself.\n"
    "Please only use this channel for playthrough purposes."
)

_CONTROL_OR_BIDI_RE = re.compile(r"[\x00-\x1f\x7f\u202a-\u202e\u2066-\u2069]")
_MENTIONISH_RE = re.compile(
    r"@(?:everyone|here)|<@!?\d+>|<@&\d+>|<#\d+>",
    re.IGNORECASE,
)

_PLAY_FORUM_REQUIRED_PERMS: tuple[tuple[str, str], ...] = (
    ("view_channel", "View Channels"),
    ("send_messages", "Send Messages"),
    ("create_public_threads", "Create Public Threads"),
    ("send_messages_in_threads", "Send Messages in Threads"),
    ("manage_threads", "Manage Threads"),
    ("manage_channels", "Manage Channels"),
)


def game_name_error(name: str) -> str | None:
    name = name.strip()
    if not name:
        return "Game name cannot be empty."
    if len(name) > MAX_GAME_NAME_LENGTH:
        return f"Game name must be {MAX_GAME_NAME_LENGTH} characters or fewer."
    if _CONTROL_OR_BIDI_RE.search(name):
        return "Game name cannot contain control or bidirectional formatting characters."
    if _MENTIONISH_RE.search(name):
        return "Game name cannot contain Discord mention syntax."
    return None


def _clean_thread_name_part(value: str) -> str:
    value = _CONTROL_OR_BIDI_RE.sub("", value)
    return re.sub(r"\s+", " ", value).strip() or "unknown"


def format_play_thread_name(game_name: str, member: discord.Member) -> str:
    game_part = _clean_thread_name_part(game_name)
    user_part = f"@{_clean_thread_name_part(member.display_name)}"
    separator = " | "

    max_game_length = MAX_PLAY_THREAD_NAME_LENGTH - len(separator) - len(user_part)
    if max_game_length >= 1:
        game_part = game_part[:max_game_length].rstrip() or game_part[:1]
    else:
        max_user_length = MAX_PLAY_THREAD_NAME_LENGTH - len(separator) - 1
        user_part = user_part[:max(1, max_user_length)].rstrip() or "@user"
        max_game_length = MAX_PLAY_THREAD_NAME_LENGTH - len(separator) - len(user_part)
        game_part = game_part[:max(1, max_game_length)].rstrip() or game_part[:1]

    return f"{game_part}{separator}{user_part}"


def calculate_spoiler_flags(existing_flags: int | None) -> int:
    return (existing_flags or 0) | SPOILER_CHANNEL_FLAG


async def mark_thread_spoiler(
    thread: discord.Thread,
    *,
    reason: str | None = None,
) -> None:
    flags = calculate_spoiler_flags(thread.flags.value)
    # discord.py does not expose IS_SPOILER_CHANNEL yet, but its HTTP client
    # supports channel flag updates and preserves auth/rate-limit handling.
    await thread._state.http.edit_channel(thread.id, flags=flags, reason=reason)


def find_forum_tag(
    forum: discord.ForumChannel,
    *,
    tag_id: int | None = None,
    name: str | None = None,
) -> discord.ForumTag | None:
    if tag_id is not None:
        tag = forum.get_tag(tag_id)
        if tag is not None:
            return tag

    if name is None:
        return None

    normalized_name = name.strip().casefold()
    return discord.utils.find(
        lambda tag: tag.name.strip().casefold() == normalized_name,
        forum.available_tags,
    )


def thread_has_tag(thread: discord.Thread, tag_id: int) -> bool:
    return any(tag.id == tag_id for tag in thread.applied_tags)


def additional_spoiler_tags(
    forum: discord.ForumChannel,
    required_tag: discord.ForumTag,
) -> list[discord.ForumTag]:
    return [
        tag
        for tag in forum.available_tags
        if tag.id != required_tag.id
    ]


def resolve_additional_spoiler_tags(
    forum: discord.ForumChannel,
    required_tag: discord.ForumTag,
    selected_tag_ids: list[int],
) -> tuple[list[discord.ForumTag], str | None]:
    max_additional_tags = MAX_FORUM_THREAD_TAGS - 1
    if len(selected_tag_ids) > max_additional_tags:
        return [], f"Select at most **{max_additional_tags}** additional spoiler tags."

    resolved: list[discord.ForumTag] = []
    seen_ids = {required_tag.id}

    for tag_id in selected_tag_ids:
        if tag_id in seen_ids:
            if tag_id == required_tag.id:
                return [], "The selected game tag is applied automatically and cannot be selected again."
            continue

        tag = forum.get_tag(tag_id)
        if tag is None:
            return [], "One of the selected spoiler tags is no longer available. Run `/play` again."

        resolved.append(tag)
        seen_ids.add(tag.id)

    return resolved, None


def thread_name_matches_player(thread: discord.Thread, member: discord.Member) -> bool:
    suffix = f" | @{_clean_thread_name_part(member.display_name)}".casefold()
    return thread.name.casefold().endswith(suffix)


def thread_name_matches_playthrough(
    thread: discord.Thread,
    game: PlayGame,
    member: discord.Member,
) -> bool:
    expected_name = format_play_thread_name(game.display_name, member).casefold()
    return thread.name.casefold() == expected_name


async def find_active_play_thread(
    guild: discord.Guild,
    forum: discord.ForumChannel,
    game: PlayGame,
    tag: discord.ForumTag,
    member: discord.Member,
) -> discord.Thread | None:
    active_threads = await guild.active_threads()

    for thread in active_threads:
        if thread.parent_id != forum.id:
            continue
        if not thread_has_tag(thread, tag.id):
            continue
        if thread_name_matches_playthrough(thread, game, member):
            return thread

    return None


def missing_play_forum_permissions(
    forum: discord.ForumChannel,
    member: discord.Member,
) -> list[str]:
    permissions = forum.permissions_for(member)
    return [
        label
        for attr, label in _PLAY_FORUM_REQUIRED_PERMS
        if not getattr(permissions, attr)
    ]


class _AdditionalSpoilerSelect(discord.ui.Select):
    def __init__(self, tags: list[discord.ForumTag]) -> None:
        options = [
            discord.SelectOption(
                label=tag.name or f"Tag {tag.id}",
                value=str(tag.id),
            )
            for tag in tags
        ]
        super().__init__(
            placeholder="Additional spoiler tags",
            min_values=0,
            max_values=min(MAX_FORUM_THREAD_TAGS - 1, len(options)),
            options=options,
            row=0,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if isinstance(view, _PlaySpoilerTagView):
            view.selected_tag_ids = [int(value) for value in self.values]
        await interaction.response.defer()


class _PlaySpoilerTagView(discord.ui.View):
    def __init__(
        self,
        *,
        cog: "Play",
        requester_id: int,
        guild_id: int,
        forum: discord.ForumChannel,
        play_game: PlayGame,
        required_tag: discord.ForumTag,
        replay: bool,
        auto_archive_duration: int | None,
    ) -> None:
        super().__init__(timeout=180)
        self.cog = cog
        self.requester_id = requester_id
        self.guild_id = guild_id
        self.forum = forum
        self.play_game = play_game
        self.required_tag = required_tag
        self.replay = replay
        self.auto_archive_duration = auto_archive_duration
        self.selected_tag_ids: list[int] = []

        selectable_tags = additional_spoiler_tags(forum, required_tag)
        if selectable_tags:
            self.add_item(_AdditionalSpoilerSelect(selectable_tags))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message(
                "Only the user who started this playthrough can use these controls.",
                ephemeral=True,
            )
            return False
        if interaction.guild_id != self.guild_id:
            await interaction.response.send_message(
                "This playthrough setup is no longer valid.",
                ephemeral=True,
            )
            return False
        return True

    @discord.ui.button(label="Create", style=discord.ButtonStyle.success, row=1)
    async def create(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        await interaction.response.edit_message(
            content="Creating your playthrough post...",
            view=None,
        )
        self.stop()
        await self.cog.create_playthrough_thread(
            interaction,
            forum=self.forum,
            play_game=self.play_game,
            required_tag=self.required_tag,
            selected_tag_ids=self.selected_tag_ids,
            replay=self.replay,
            auto_archive_duration=self.auto_archive_duration,
        )

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary, row=1)
    async def cancel(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        await interaction.response.edit_message(
            content="Playthrough creation cancelled.",
            view=None,
        )
        self.stop()


class Play(commands.Cog):
    """
    Visual Novel playthrough forum posts.

    Commands: /play
    """

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.play_store = PlayStore()
        self.module_store = ConfigStore()

    def cog_unload(self):
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

    async def _archive_failed_thread(self, thread: discord.Thread) -> None:
        try:
            await thread.edit(
                archived=True,
                locked=True,
                reason="Playthrough thread setup failed after creation",
            )
        except (discord.Forbidden, discord.HTTPException):
            log(
                f"PLAY // FAILED TO ARCHIVE UNSPOILERED THREAD 『 {thread.id} 』",
                level="warning",
                logger_name="play",
            )

    async def create_playthrough_thread(
        self,
        interaction: discord.Interaction,
        *,
        forum: discord.ForumChannel,
        play_game: PlayGame,
        required_tag: discord.ForumTag,
        selected_tag_ids: list[int],
        replay: bool,
        auto_archive_duration: int | None,
    ) -> None:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.edit_original_response(
                content="This can only be used inside a server.",
                view=None,
            )
            return

        extra_tags, error = resolve_additional_spoiler_tags(
            forum,
            required_tag,
            selected_tag_ids,
        )
        if error is not None:
            await interaction.edit_original_response(content=error, view=None)
            return

        existing_thread = await find_active_play_thread(
            interaction.guild,
            forum,
            play_game,
            required_tag,
            interaction.user,
        )
        if existing_thread is not None:
            await interaction.edit_original_response(
                content=(
                    f"You already have an active **{escape_untrusted_text(play_game.display_name)}** "
                    f"playthrough post: {existing_thread.mention}"
                ),
                view=None,
            )
            return

        safe_game_name = escape_untrusted_text(play_game.display_name)
        replay_note = " (replay)" if replay else ""
        thread_name = format_play_thread_name(play_game.display_name, interaction.user)
        spoiler_names = [safe_game_name] + [
            escape_untrusted_text(tag.name)
            for tag in extra_tags
        ]
        starter_content = f"{interaction.user.mention} | Spoilers for {', '.join(spoiler_names)}{replay_note}"

        try:
            result = await forum.create_thread(
                name=thread_name,
                content=starter_content,
                allowed_mentions=discord.AllowedMentions(
                    everyone=False,
                    users=[interaction.user],
                    roles=False,
                    replied_user=False,
                ),
                applied_tags=[required_tag, *extra_tags],
                auto_archive_duration=auto_archive_duration or discord.utils.MISSING,
                reason=f"Playthrough post for {interaction.user} ({interaction.user.id})",
            )
        except discord.Forbidden:
            await interaction.edit_original_response(
                content=(
                    f"I could not create a playthrough post in {forum.mention}. "
                    "Check my forum channel permissions."
                ),
                view=None,
            )
            return
        except discord.HTTPException as e:
            await interaction.edit_original_response(
                content=f"Discord rejected the playthrough post request: `{e}`",
                view=None,
            )
            return

        thread = result.thread

        try:
            await mark_thread_spoiler(
                thread,
                reason=f"Mark playthrough post as spoiler for {interaction.user} ({interaction.user.id})",
            )
        except discord.Forbidden:
            await self._archive_failed_thread(thread)
            await interaction.edit_original_response(
                content=(
                    "I created the playthrough post, but Discord rejected the Spoiler Channel update, "
                    "so I archived it. Check my Manage Channels and Manage Threads permissions."
                ),
                view=None,
            )
            return
        except discord.HTTPException as e:
            await self._archive_failed_thread(thread)
            await interaction.edit_original_response(
                content=(
                    "I created the playthrough post, but Discord rejected the Spoiler Channel update, "
                    f"so I archived it.\n\nError: `{e}`"
                ),
                view=None,
            )
            return

        kind = "replay playthrough" if replay else "playthrough"
        try:
            await thread.send(
                INTRO_MESSAGE_TEMPLATE.format(kind=kind, game=safe_game_name),
                allowed_mentions=NO_MENTIONS,
            )
        except discord.HTTPException as e:
            log(
                f"PLAY // INTRO MESSAGE FAILED 『 THREAD {thread.id} 』 GUILD 『 {interaction.guild.id} 』 // {e}",
                level="debug",
                logger_name="play",
            )

        log(
            f"PLAY // THREAD CREATED 『 THREAD {thread.id} 』 GAME 『 {play_game.key} 』 "
            f"USER 『 {interaction.user.id} 』 GUILD 『 {interaction.guild.id} 』 "
            f"REPLAY {replay} EXTRA_TAGS {[tag.id for tag in extra_tags]}",
            level="debug",
            logger_name="play",
        )

        await interaction.edit_original_response(
            content=f"Created your **{safe_game_name}** playthrough post: {thread.mention}",
            view=None,
        )

    @app_commands.command(
        name="play",
        description="Create a personal Visual Novel playthrough post.",
    )
    @app_commands.guild_only()
    @app_commands.describe(
        game="The Visual Novel you are playing.",
        replay="Whether this playthrough is a replay.",
    )
    @app_commands.autocomplete(game=game_autocomplete)
    async def play(
        self,
        interaction: discord.Interaction,
        game: str,
        replay: bool = False,
    ) -> None:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message(
                "This can only be used inside a server.",
                ephemeral=True,
            )
            return

        if not await require_module_enabled_for_interaction(
            interaction, self.module_store, MODULE_NAME
        ):
            return

        config = self.play_store.get_config(interaction.guild.id)
        play_game = self.play_store.get_game(interaction.guild.id, game)
        if play_game is None:
            await interaction.response.send_message(
                "That game is not configured for playthroughs on this server.",
                ephemeral=True,
            )
            return

        forum_channel_id = play_game.forum_channel_id or (config.forum_channel_id if config else None)
        forum = await self._get_forum_channel(interaction.guild, forum_channel_id)

        if forum is None:
            await interaction.response.send_message(
                f"**{escape_untrusted_text(play_game.display_name)}** does not have a valid playthrough forum. "
                "Ask an admin to run `/amadeus play add-game` with a forum channel.",
                ephemeral=True,
            )
            return

        tag = find_forum_tag(
            forum,
            tag_id=play_game.forum_tag_id,
            name=play_game.display_name,
        )
        if tag is None:
            await interaction.response.send_message(
                f"**{escape_untrusted_text(play_game.display_name)}** is missing its forum tag. "
                "Ask an admin to re-run `/amadeus play add-game` for this game.",
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

        missing_permissions = missing_play_forum_permissions(forum, bot_member)
        if missing_permissions:
            needed = ", ".join(f"**{permission}**" for permission in missing_permissions)
            await interaction.response.send_message(
                f"I do not have the required permissions in {forum.mention}.\n\n"
                f"Grant me: {needed}.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        existing_thread = await find_active_play_thread(
            interaction.guild,
            forum,
            play_game,
            tag,
            interaction.user,
        )
        if existing_thread is not None:
            await interaction.edit_original_response(
                content=(
                    f"You already have an active **{escape_untrusted_text(play_game.display_name)}** "
                    f"playthrough post: {existing_thread.mention}"
                )
            )
            return

        safe_game_name = escape_untrusted_text(play_game.display_name)
        view = _PlaySpoilerTagView(
            cog=self,
            requester_id=interaction.user.id,
            guild_id=interaction.guild.id,
            forum=forum,
            play_game=play_game,
            required_tag=tag,
            replay=replay,
            auto_archive_duration=(
                config.auto_archive_duration
                if config and config.auto_archive_duration
                else None
            ),
        )
        tag_note = (
            f"\n\nThe **{escape_untrusted_text(tag.name)}** tag will be applied automatically."
            if tag.name
            else ""
        )
        await interaction.edit_original_response(
            content=(
                f"Would you like to include any additional spoilers in this channel "
                f"for **{safe_game_name}**?{tag_note}"
            ),
            view=view,
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(Play(bot))
