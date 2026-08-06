import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands, tasks

from amadeus.constants import CACHE_DIR, PLAY_ARCHIVED_LOCK_GRACE_DAYS
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
PLAY_LOCK_SWEEP_INTERVAL_DAYS = 30
PLAY_LOCK_SWEEP_INITIAL_LOOKBACK_DAYS = (
    PLAY_LOCK_SWEEP_INTERVAL_DAYS + PLAY_ARCHIVED_LOCK_GRACE_DAYS
)
PLAY_LOCK_SWEEP_CHECKPOINT_VERSION = 1
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
    ("send_messages", "Send Messages / Create Posts"),
    ("send_messages_in_threads", "Send Messages in Threads"),
    ("manage_threads", "Manage Threads"),
    ("manage_channels", "Manage Channels"),
)
_PLAY_MEMBER_REQUIRED_PERMS: tuple[tuple[str, str], ...] = (
    ("view_channel", "View Channels"),
    ("send_messages_in_threads", "Send Messages in Threads"),
)
_PLAY_LOCK_SWEEP_REQUIRED_PERMS: tuple[tuple[str, str], ...] = (
    ("view_channel", "View Channels"),
    ("read_message_history", "Read Message History"),
    ("manage_threads", "Manage Threads"),
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _coerce_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _parse_utc_datetime(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None

    normalized = value.replace("Z", "+00:00")
    try:
        return _coerce_utc(datetime.fromisoformat(normalized))
    except ValueError:
        return None


def _format_utc_datetime(value: datetime) -> str:
    return _coerce_utc(value).isoformat()


def play_lock_sweep_checkpoint_path(
    cache_dir: Path,
    guild_id: int,
    forum_channel_id: int,
) -> Path:
    return (
        cache_dir
        / str(guild_id)
        / "play_lock_sweeps"
        / f"{forum_channel_id}.json"
    )


def load_play_lock_sweep_checkpoint(
    cache_dir: Path,
    guild_id: int,
    forum_channel_id: int,
) -> dict[str, object] | None:
    path = play_lock_sweep_checkpoint_path(cache_dir, guild_id, forum_channel_id)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError):
        return None

    return data if isinstance(data, dict) else None


def save_play_lock_sweep_checkpoint(
    cache_dir: Path,
    *,
    guild_id: int,
    forum_channel_id: int,
    started_at: datetime,
    completed_at: datetime,
    archive_stop_at: datetime,
    scanned_count: int,
    locked_count: int,
) -> None:
    path = play_lock_sweep_checkpoint_path(cache_dir, guild_id, forum_channel_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": PLAY_LOCK_SWEEP_CHECKPOINT_VERSION,
        "guild_id": str(guild_id),
        "forum_channel_id": str(forum_channel_id),
        "last_sweep_started_at": _format_utc_datetime(started_at),
        "last_sweep_completed_at": _format_utc_datetime(completed_at),
        "last_archive_stop_at": _format_utc_datetime(archive_stop_at),
        "last_scanned_count": scanned_count,
        "last_locked_count": locked_count,
    }
    temp_path = path.with_suffix(".tmp")
    temp_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temp_path.replace(path)


def play_lock_sweep_due(
    checkpoint: dict[str, object] | None,
    now: datetime,
) -> bool:
    if checkpoint is None:
        return True

    last_completed_at = _parse_utc_datetime(checkpoint.get("last_sweep_completed_at"))
    if last_completed_at is None:
        return True

    return _coerce_utc(now) - last_completed_at >= timedelta(
        days=PLAY_LOCK_SWEEP_INTERVAL_DAYS
    )


def play_lock_sweep_archive_stop_at(
    checkpoint: dict[str, object] | None,
    now: datetime,
    grace_days: int,
) -> datetime:
    safe_grace_days = max(0, grace_days)
    grace_delta = timedelta(days=safe_grace_days)
    initial_lookback = timedelta(days=PLAY_LOCK_SWEEP_INTERVAL_DAYS + safe_grace_days)

    if checkpoint is None:
        return _coerce_utc(now) - initial_lookback

    last_started_at = _parse_utc_datetime(checkpoint.get("last_sweep_started_at"))
    if last_started_at is None:
        return _coerce_utc(now) - initial_lookback

    return last_started_at - grace_delta


def thread_archive_timestamp(thread: discord.Thread) -> datetime:
    archive_timestamp = getattr(thread, "archive_timestamp", None)
    if isinstance(archive_timestamp, datetime):
        return _coerce_utc(archive_timestamp)
    return _coerce_utc(thread.created_at)


def thread_last_activity_at(thread: discord.Thread) -> datetime:
    last_message_id = getattr(thread, "last_message_id", None)
    if last_message_id is not None:
        try:
            return _coerce_utc(discord.utils.snowflake_time(int(last_message_id)))
        except (TypeError, ValueError):
            pass

    archive_timestamp = getattr(thread, "archive_timestamp", None)
    if isinstance(archive_timestamp, datetime):
        return _coerce_utc(archive_timestamp)
    return _coerce_utc(thread.created_at)


def is_playthrough_thread_name(name: str) -> bool:
    return " | @" in name


def should_lock_archived_play_thread(
    thread: discord.Thread,
    *,
    configured_tag_ids: set[int],
    now: datetime,
    grace_days: int,
) -> bool:
    if getattr(thread, "locked", False):
        return False
    if getattr(thread, "archived", True) is False:
        return False
    if not is_playthrough_thread_name(thread.name):
        return False
    if not any(thread_has_tag(thread, tag_id) for tag_id in configured_tag_ids):
        return False

    lock_before = _coerce_utc(now) - timedelta(days=max(0, grace_days))
    return thread_last_activity_at(thread) <= lock_before


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


def _member_username(member: discord.Member) -> str:
    return getattr(member, "name", None) or member.display_name


def format_play_thread_name(game_name: str, member: discord.Member) -> str:
    game_part = _clean_thread_name_part(game_name)
    user_part = f"@{_clean_thread_name_part(_member_username(member))}"
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
    if any(tag.id == tag_id for tag in getattr(thread, "applied_tags", ())):
        return True

    return any(
        int(raw_tag_id) == tag_id
        for raw_tag_id in getattr(thread, "_applied_tags", ())
    )


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
                return (
                    [],
                    "The selected game tag is applied automatically and cannot be selected again.",
                )
            continue

        tag = forum.get_tag(tag_id)
        if tag is None:
            return (
                [],
                "One of the selected spoiler tags is no longer available. Run `/play` again.",
            )

        resolved.append(tag)
        seen_ids.add(tag.id)

    return resolved, None


def thread_name_matches_player(thread: discord.Thread, member: discord.Member) -> bool:
    username = _clean_thread_name_part(_member_username(member)).casefold()
    return f"@{username}" in thread.name.casefold()


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
        if thread_name_matches_player(thread, member):
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


def missing_member_play_forum_permissions(
    forum: discord.ForumChannel,
    member: discord.Member,
) -> list[str]:
    permissions = forum.permissions_for(member)
    return [
        label
        for attr, label in _PLAY_MEMBER_REQUIRED_PERMS
        if not getattr(permissions, attr)
    ]


def missing_play_lock_sweep_permissions(
    forum: discord.ForumChannel,
    member: discord.Member,
) -> list[str]:
    permissions = forum.permissions_for(member)
    return [
        label
        for attr, label in _PLAY_LOCK_SWEEP_REQUIRED_PERMS
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
        self.message: discord.InteractionMessage | None = None

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

    async def on_timeout(self) -> None:
        if self.message is None:
            return

        try:
            await self.message.edit(
                content="Playthrough setup timed out.",
                view=None,
            )
        except discord.HTTPException:
            pass

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


class _DuplicatePlayThreadView(discord.ui.View):
    def __init__(
        self,
        *,
        cog: "Play",
        requester_id: int,
        guild_id: int,
        existing_thread: discord.Thread,
        forum: discord.ForumChannel,
        play_game: PlayGame,
        required_tag: discord.ForumTag,
        selected_tag_ids: list[int] | None,
        replay: bool,
        auto_archive_duration: int | None,
    ) -> None:
        super().__init__(timeout=180)
        self.cog = cog
        self.requester_id = requester_id
        self.guild_id = guild_id
        self.existing_thread = existing_thread
        self.forum = forum
        self.play_game = play_game
        self.required_tag = required_tag
        self.selected_tag_ids = selected_tag_ids
        self.replay = replay
        self.auto_archive_duration = auto_archive_duration
        self.message: discord.InteractionMessage | None = None

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

    async def on_timeout(self) -> None:
        if self.message is None:
            return

        try:
            await self.message.edit(
                content="Playthrough setup timed out.",
                view=None,
            )
        except discord.HTTPException:
            pass

    @discord.ui.button(label="No", style=discord.ButtonStyle.secondary, row=0)
    async def no(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        await interaction.response.edit_message(
            content="Playthrough creation cancelled.",
            view=None,
        )
        self.stop()

    @discord.ui.button(label="Yes", style=discord.ButtonStyle.danger, row=0)
    async def yes(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        await interaction.response.edit_message(
            content="Archiving existing playthrough post...",
            view=None,
        )
        self.stop()
        await self.cog.archive_duplicate_and_continue(
            interaction,
            existing_thread=self.existing_thread,
            forum=self.forum,
            play_game=self.play_game,
            required_tag=self.required_tag,
            selected_tag_ids=self.selected_tag_ids,
            replay=self.replay,
            auto_archive_duration=self.auto_archive_duration,
        )


class Play(commands.Cog):
    """
    Visual Novel playthrough forum posts.

    Commands: /play
    """

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.play_store = PlayStore()
        self.module_store = ConfigStore()
        self._inflight_creates: set[tuple[int, int, str]] = set()
        self._cache_dir = CACHE_DIR
        self._lock_grace_days = max(0, PLAY_ARCHIVED_LOCK_GRACE_DAYS)

    def cog_unload(self):
        self._lock_archived_playthroughs.cancel()
        self.play_store.close()
        self.module_store.close()

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        if not self._lock_archived_playthroughs.is_running():
            self._lock_archived_playthroughs.start()

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

    def _configured_play_forums(self, guild_id: int) -> dict[int, set[int]]:
        config = self.play_store.get_config(guild_id)
        default_forum_id = config.forum_channel_id if config else None
        forums: dict[int, set[int]] = {}

        for game in self.play_store.list_games(guild_id):
            forum_id = game.forum_channel_id or default_forum_id
            if forum_id is None or game.forum_tag_id is None:
                continue
            forums.setdefault(forum_id, set()).add(game.forum_tag_id)

        return forums

    def configured_play_tag_ids_for_forum(
        self,
        guild_id: int,
        forum_channel_id: int,
    ) -> set[int]:
        return self._configured_play_forums(guild_id).get(forum_channel_id, set())

    def archived_lock_grace_days(self, grace_days: int | None = None) -> int:
        return self._lock_grace_days if grace_days is None else max(0, grace_days)

    async def _sweep_archived_playthroughs_for_forum(
        self,
        guild: discord.Guild,
        forum: discord.ForumChannel,
        configured_tag_ids: set[int],
        checkpoint: dict[str, object] | None,
        started_at: datetime,
        grace_days: int | None = None,
    ) -> tuple[int, int]:
        effective_grace_days = self.archived_lock_grace_days(grace_days)
        archive_stop_at = play_lock_sweep_archive_stop_at(
            checkpoint,
            started_at,
            effective_grace_days,
        )
        scanned_count = 0
        locked_count = 0

        async for thread in forum.archived_threads(limit=None):
            if thread_archive_timestamp(thread) < archive_stop_at:
                break

            scanned_count += 1
            if not should_lock_archived_play_thread(
                thread,
                configured_tag_ids=configured_tag_ids,
                now=started_at,
                grace_days=effective_grace_days,
            ):
                continue

            try:
                await thread.edit(
                    archived=True,
                    locked=True,
                    reason="Lock inactive archived playthrough post",
                )
            except discord.Forbidden:
                log(
                    f"PLAY // ARCHIVED LOCK FORBIDDEN 『 THREAD {thread.id} 』 "
                    f"GUILD 『 {guild.id} 』 FORUM 『 {forum.id} 』",
                    level="warning",
                    logger_name="play",
                )
                continue
            except discord.HTTPException as e:
                log(
                    f"PLAY // ARCHIVED LOCK FAILED 『 THREAD {thread.id} 』 "
                    f"GUILD 『 {guild.id} 』 FORUM 『 {forum.id} 』 // {e}",
                    level="warning",
                    logger_name="play",
                )
                continue

            locked_count += 1

        completed_at = _utc_now()
        save_play_lock_sweep_checkpoint(
            self._cache_dir,
            guild_id=guild.id,
            forum_channel_id=forum.id,
            started_at=started_at,
            completed_at=completed_at,
            archive_stop_at=archive_stop_at,
            scanned_count=scanned_count,
            locked_count=locked_count,
        )
        return scanned_count, locked_count

    async def run_archived_playthrough_lock_sweep(
        self,
        guild: discord.Guild,
        forum: discord.ForumChannel,
        *,
        grace_days: int | None = None,
        started_at: datetime | None = None,
    ) -> tuple[int, int] | None:
        configured_tag_ids = self.configured_play_tag_ids_for_forum(
            guild.id,
            forum.id,
        )
        if not configured_tag_ids:
            return None

        checkpoint = load_play_lock_sweep_checkpoint(
            self._cache_dir,
            guild.id,
            forum.id,
        )
        return await self._sweep_archived_playthroughs_for_forum(
            guild,
            forum,
            configured_tag_ids,
            checkpoint,
            started_at or _utc_now(),
            grace_days=grace_days,
        )

    async def _sweep_archived_playthroughs_for_guild(
        self,
        guild: discord.Guild,
        started_at: datetime,
    ) -> None:
        if not self.module_store.is_module_enabled(guild.id, MODULE_NAME):
            return

        bot_member = guild.me
        if bot_member is None:
            return

        for forum_id, configured_tag_ids in self._configured_play_forums(
            guild.id
        ).items():
            forum = await self._get_forum_channel(guild, forum_id)
            if forum is None:
                continue

            missing_permissions = missing_play_lock_sweep_permissions(
                forum,
                bot_member,
            )
            if missing_permissions:
                needed = ", ".join(missing_permissions)
                log(
                    f"PLAY // ARCHIVED LOCK SWEEP SKIPPED 『 GUILD {guild.id} 』 "
                    f"FORUM 『 {forum.id} 』 MISSING 『 {needed} 』",
                    level="warning",
                    logger_name="play",
                )
                continue

            checkpoint = load_play_lock_sweep_checkpoint(
                self._cache_dir,
                guild.id,
                forum.id,
            )
            if not play_lock_sweep_due(checkpoint, started_at):
                continue

            try:
                scanned_count, locked_count = (
                    await self._sweep_archived_playthroughs_for_forum(
                        guild,
                        forum,
                        configured_tag_ids,
                        checkpoint,
                        started_at,
                    )
                )
            except discord.Forbidden:
                log(
                    f"PLAY // ARCHIVED LOCK SWEEP FORBIDDEN 『 GUILD {guild.id} 』 "
                    f"FORUM 『 {forum.id} 』",
                    level="warning",
                    logger_name="play",
                )
                continue
            except discord.HTTPException as e:
                log(
                    f"PLAY // ARCHIVED LOCK SWEEP FAILED 『 GUILD {guild.id} 』 "
                    f"FORUM 『 {forum.id} 』 // {e}",
                    level="warning",
                    logger_name="play",
                )
                continue
            except OSError as e:
                log(
                    f"PLAY // ARCHIVED LOCK CHECKPOINT FAILED 『 GUILD {guild.id} 』 "
                    f"FORUM 『 {forum.id} 』 // {e}",
                    level="warning",
                    logger_name="play",
                )
                continue

            log(
                f"PLAY // ARCHIVED LOCK SWEEP COMPLETE 『 GUILD {guild.id} 』 "
                f"FORUM 『 {forum.id} 』 SCANNED {scanned_count} LOCKED {locked_count}",
                level="debug",
                logger_name="play",
            )

    @tasks.loop(hours=24)
    async def _lock_archived_playthroughs(self) -> None:
        started_at = _utc_now()
        for guild in list(self.bot.guilds):
            await self._sweep_archived_playthroughs_for_guild(guild, started_at)

    @_lock_archived_playthroughs.before_loop
    async def _before_lock_archived_playthroughs(self) -> None:
        await self.bot.wait_until_ready()

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

    async def _find_active_play_thread_or_respond(
        self,
        interaction: discord.Interaction,
        *,
        forum: discord.ForumChannel,
        play_game: PlayGame,
        required_tag: discord.ForumTag,
        member: discord.Member,
    ) -> tuple[discord.Thread | None, bool]:
        try:
            return (
                await find_active_play_thread(
                    interaction.guild,
                    forum,
                    play_game,
                    required_tag,
                    member,
                ),
                True,
            )
        except discord.HTTPException as e:
            guild_id = interaction.guild.id if interaction.guild else "—"
            log(
                f"PLAY // ACTIVE THREAD LOOKUP FAILED 『 GUILD {guild_id} 』 // {e}",
                level="debug",
                logger_name="play",
            )
            await interaction.edit_original_response(
                content=(
                    "I couldn't check existing active playthrough posts right now. "
                    "Please try again in a moment."
                ),
                view=None,
            )
            return None, False

    async def _send_spoiler_tag_prompt(
        self,
        interaction: discord.Interaction,
        *,
        forum: discord.ForumChannel,
        play_game: PlayGame,
        required_tag: discord.ForumTag,
        replay: bool,
        auto_archive_duration: int | None,
    ) -> None:
        safe_game_name = escape_untrusted_text(play_game.display_name)
        view = _PlaySpoilerTagView(
            cog=self,
            requester_id=interaction.user.id,
            guild_id=interaction.guild.id,
            forum=forum,
            play_game=play_game,
            required_tag=required_tag,
            replay=replay,
            auto_archive_duration=auto_archive_duration,
        )
        tag_note = (
            f"\n\nThe **{escape_untrusted_text(required_tag.name)}** tag will be "
            "applied automatically."
            if required_tag.name
            else ""
        )
        view.message = await interaction.edit_original_response(
            content=(
                f"Would you like to include any additional spoilers in this channel "
                f"for **{safe_game_name}**?{tag_note}"
            ),
            view=view,
        )

    async def _send_duplicate_confirmation(
        self,
        interaction: discord.Interaction,
        *,
        existing_thread: discord.Thread,
        forum: discord.ForumChannel,
        play_game: PlayGame,
        required_tag: discord.ForumTag,
        selected_tag_ids: list[int] | None,
        replay: bool,
        auto_archive_duration: int | None,
    ) -> None:
        safe_game_name = escape_untrusted_text(play_game.display_name)
        view = _DuplicatePlayThreadView(
            cog=self,
            requester_id=interaction.user.id,
            guild_id=interaction.guild.id,
            existing_thread=existing_thread,
            forum=forum,
            play_game=play_game,
            required_tag=required_tag,
            selected_tag_ids=selected_tag_ids,
            replay=replay,
            auto_archive_duration=auto_archive_duration,
        )
        view.message = await interaction.edit_original_response(
            content=(
                f"An active {safe_game_name} playthrough by you was found: "
                f"{existing_thread.mention}.\n"
                "Would you like to archive it and create a new channel?"
            ),
            view=view,
        )

    async def archive_duplicate_and_continue(
        self,
        interaction: discord.Interaction,
        *,
        existing_thread: discord.Thread,
        forum: discord.ForumChannel,
        play_game: PlayGame,
        required_tag: discord.ForumTag,
        selected_tag_ids: list[int] | None,
        replay: bool,
        auto_archive_duration: int | None,
    ) -> None:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.edit_original_response(
                content="This can only be used inside a server.",
                view=None,
            )
            return

        try:
            await existing_thread.edit(
                archived=True,
                locked=True,
                reason=(
                    f"Replace active playthrough post for "
                    f"{interaction.user} ({interaction.user.id})"
                ),
            )
        except discord.Forbidden:
            await interaction.edit_original_response(
                content=(
                    f"I could not archive {existing_thread.mention}. "
                    "Check my Manage Threads permission."
                ),
                view=None,
            )
            return
        except discord.HTTPException as e:
            await interaction.edit_original_response(
                content=f"Discord rejected the archive request: `{e}`",
                view=None,
            )
            return

        log(
            f"PLAY // DUPLICATE ARCHIVED 『 THREAD {existing_thread.id} 』 "
            f"GAME 『 {play_game.key} 』 USER 『 {interaction.user.id} 』 "
            f"GUILD 『 {interaction.guild.id} 』",
            level="debug",
            logger_name="play",
        )

        if selected_tag_ids is None:
            await self._send_spoiler_tag_prompt(
                interaction,
                forum=forum,
                play_game=play_game,
                required_tag=required_tag,
                replay=replay,
                auto_archive_duration=auto_archive_duration,
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

        await self._create_playthrough_thread_unlocked(
            interaction,
            forum=forum,
            play_game=play_game,
            required_tag=required_tag,
            extra_tags=extra_tags,
            replay=replay,
            auto_archive_duration=auto_archive_duration,
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

        lock_key = (interaction.guild.id, interaction.user.id, play_game.key)
        if lock_key in self._inflight_creates:
            await interaction.edit_original_response(
                content=(
                    "A playthrough post for this game is already being created. "
                    "Please wait a moment."
                ),
                view=None,
            )
            return

        missing_member_permissions = missing_member_play_forum_permissions(
            forum,
            interaction.user,
        )
        if missing_member_permissions:
            needed = ", ".join(
                f"**{permission}**" for permission in missing_member_permissions
            )
            await interaction.edit_original_response(
                content=(
                    f"You do not have the required permissions in {forum.mention}.\n\n"
                    f"Required: {needed}."
                ),
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

        self._inflight_creates.add(lock_key)
        try:
            existing_thread, lookup_ok = await self._find_active_play_thread_or_respond(
                interaction,
                forum=forum,
                play_game=play_game,
                required_tag=required_tag,
                member=interaction.user,
            )
            if not lookup_ok:
                return
            if existing_thread is not None:
                await self._send_duplicate_confirmation(
                    interaction,
                    existing_thread=existing_thread,
                    forum=forum,
                    play_game=play_game,
                    required_tag=required_tag,
                    selected_tag_ids=selected_tag_ids,
                    replay=replay,
                    auto_archive_duration=auto_archive_duration,
                )
                return

            await self._create_playthrough_thread_unlocked(
                interaction,
                forum=forum,
                play_game=play_game,
                required_tag=required_tag,
                extra_tags=extra_tags,
                replay=replay,
                auto_archive_duration=auto_archive_duration,
            )
        finally:
            self._inflight_creates.discard(lock_key)

    async def _create_playthrough_thread_unlocked(
        self,
        interaction: discord.Interaction,
        *,
        forum: discord.ForumChannel,
        play_game: PlayGame,
        required_tag: discord.ForumTag,
        extra_tags: list[discord.ForumTag],
        replay: bool,
        auto_archive_duration: int | None,
    ) -> None:
        safe_game_name = escape_untrusted_text(play_game.display_name)
        replay_note = " (replay)" if replay else ""
        thread_name = format_play_thread_name(play_game.display_name, interaction.user)
        spoiler_names = [safe_game_name] + [
            escape_untrusted_text(tag.name)
            for tag in extra_tags
        ]
        starter_content = (
            f"{interaction.user.mention} | Spoilers for "
            f"{', '.join(spoiler_names)}{replay_note}"
        )

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
                reason=(
                    f"Mark playthrough post as spoiler for "
                    f"{interaction.user} ({interaction.user.id})"
                ),
            )
        except discord.Forbidden:
            await self._archive_failed_thread(thread)
            await interaction.edit_original_response(
                content=(
                    "I created the playthrough post, but Discord rejected the "
                    "Spoiler Channel update, so I archived it. Check my Manage "
                    "Channels and Manage Threads permissions."
                ),
                view=None,
            )
            return
        except discord.HTTPException as e:
            await self._archive_failed_thread(thread)
            await interaction.edit_original_response(
                content=(
                    "I created the playthrough post, but Discord rejected the "
                    "Spoiler Channel update, "
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
                f"PLAY // INTRO MESSAGE FAILED 『 THREAD {thread.id} 』 "
                f"GUILD 『 {interaction.guild.id} 』 // {e}",
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

        forum_channel_id = (
            play_game.forum_channel_id
            or (config.forum_channel_id if config else None)
        )
        forum = await self._get_forum_channel(interaction.guild, forum_channel_id)

        if forum is None:
            safe_game_name = escape_untrusted_text(play_game.display_name)
            await interaction.response.send_message(
                f"**{safe_game_name}** does not have a valid playthrough forum. "
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

        missing_member_permissions = missing_member_play_forum_permissions(
            forum,
            interaction.user,
        )
        if missing_member_permissions:
            needed = ", ".join(
                f"**{permission}**" for permission in missing_member_permissions
            )
            await interaction.response.send_message(
                f"You do not have the required permissions in {forum.mention}.\n\n"
                f"Required: {needed}.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        existing_thread, lookup_ok = await self._find_active_play_thread_or_respond(
            interaction,
            forum=forum,
            play_game=play_game,
            required_tag=tag,
            member=interaction.user,
        )
        if not lookup_ok:
            return
        if existing_thread is not None:
            await self._send_duplicate_confirmation(
                interaction,
                existing_thread=existing_thread,
                forum=forum,
                play_game=play_game,
                required_tag=tag,
                selected_tag_ids=None,
                replay=replay,
                auto_archive_duration=(
                    config.auto_archive_duration
                    if config and config.auto_archive_duration
                    else None
                ),
            )
            return

        await self._send_spoiler_tag_prompt(
            interaction,
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


async def setup(bot: commands.Bot):
    await bot.add_cog(Play(bot))
