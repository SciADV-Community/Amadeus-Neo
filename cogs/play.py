import re

import discord
from discord import app_commands
from discord.ext import commands

from amadeus.database import ConfigStore
from amadeus.discord_utils import NO_MENTIONS, escape_untrusted_text
from amadeus.logging_utils import log
from amadeus.models.play import PlayConfig, PlayGame
from amadeus.module_guard import require_module_enabled_for_interaction
from amadeus.play_store import PlayStore

MODULE_NAME = "play"
SPOILER_CHANNEL_FLAG = 1 << 21
MAX_PLAY_THREAD_NAME_LENGTH = 100
MAX_GAME_NAME_LENGTH = 80
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


def thread_name_matches_player(thread: discord.Thread, member: discord.Member) -> bool:
    suffix = f" | @{_clean_thread_name_part(member.display_name)}".casefold()
    return thread.name.casefold().endswith(suffix)


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
        if thread_name_matches_player(thread, member):
            return thread

        starter_message = thread.starter_message
        if starter_message is not None and (
            member.mention in starter_message.content
            or f"<@!{member.id}>" in starter_message.content
        ):
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

    async def _get_configured_forum(
        self,
        guild: discord.Guild,
        config: PlayConfig | None,
    ) -> discord.ForumChannel | None:
        if config is None or config.forum_channel_id is None:
            return None

        channel = guild.get_channel(config.forum_channel_id)
        if channel is None:
            try:
                channel = await guild.fetch_channel(config.forum_channel_id)
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
        forum = await self._get_configured_forum(interaction.guild, config)

        if forum is None:
            await interaction.response.send_message(
                "Playthroughs are not configured yet. Ask an admin to run `/amadeus play set-forum`.",
                ephemeral=True,
            )
            return

        play_game = self.play_store.get_game(interaction.guild.id, game)
        if play_game is None:
            await interaction.response.send_message(
                "That game is not configured for playthroughs on this server.",
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
        replay_note = " (replay)" if replay else ""
        thread_name = format_play_thread_name(play_game.display_name, interaction.user)
        starter_content = f"{interaction.user.mention} | Spoilers for {safe_game_name}{replay_note}"

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
                applied_tags=[tag],
                auto_archive_duration=config.auto_archive_duration or discord.utils.MISSING,
                reason=f"Playthrough post for {interaction.user} ({interaction.user.id})",
            )
        except discord.Forbidden:
            await interaction.edit_original_response(
                content=(
                    f"I could not create a playthrough post in {forum.mention}. "
                    "Check my forum channel permissions."
                )
            )
            return
        except discord.HTTPException as e:
            await interaction.edit_original_response(
                content=f"Discord rejected the playthrough post request: `{e}`"
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
                )
            )
            return
        except discord.HTTPException as e:
            await self._archive_failed_thread(thread)
            await interaction.edit_original_response(
                content=(
                    "I created the playthrough post, but Discord rejected the Spoiler Channel update, "
                    f"so I archived it.\n\nError: `{e}`"
                )
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
            f"USER 『 {interaction.user.id} 』 GUILD 『 {interaction.guild.id} 』 REPLAY {replay}",
            level="debug",
            logger_name="play",
        )

        await interaction.edit_original_response(
            content=f"Created your **{safe_game_name}** playthrough post: {thread.mention}"
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(Play(bot))
