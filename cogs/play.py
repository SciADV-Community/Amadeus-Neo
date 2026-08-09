import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
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
_PLAY_MESSAGE_MANAGEMENT_REQUIRED_PERMS: tuple[tuple[str, str], ...] = (
    ("manage_messages", "Manage Messages"),
)
_PLAY_THREAD_MANAGEMENT_REQUIRED_PERMS: tuple[tuple[str, str], ...] = (
    ("manage_threads", "Manage Threads"),
)
_PLAY_THREAD_OWNER_SEPARATOR = " | @"
_PLAY_CONTEXT_MENU_NAMES: tuple[str, ...] = (
    "Delete Message",
    "Pin Message",
    "Unlock Channel",
)
_PLAY_DELETE_PROTECTED_AUTHOR_MESSAGE = (
    "You can not delete bot or moderator messages."
)
_PLAY_MODAL_OPTION_LIMIT = 25


@dataclass(frozen=True)
class PlayThreadContext:
    thread: discord.Thread
    configured_tag_ids: frozenset[int]
    matched_tag_ids: frozenset[int]


@dataclass(frozen=True)
class PlayCreateContext:
    play_game: PlayGame
    forum: discord.ForumChannel
    required_tag: discord.ForumTag
    auto_archive_duration: int | None


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
    return play_thread_owner_username(name) is not None


def play_thread_owner_username(name: str) -> str | None:
    if _PLAY_THREAD_OWNER_SEPARATOR not in name:
        return None

    owner = name.rsplit(_PLAY_THREAD_OWNER_SEPARATOR, 1)[1].strip()
    return owner or None


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


def _thread_needs_unlock(thread: object) -> bool:
    return bool(getattr(thread, "archived", False)) or bool(
        getattr(thread, "locked", False)
    )


def _looks_like_thread_channel(channel: object) -> bool:
    return isinstance(channel, discord.Thread) or (
        getattr(channel, "parent_id", None) is not None
        and hasattr(channel, "id")
        and hasattr(channel, "name")
    )


def configured_playthrough_thread_context(
    channel: object,
    configured_forums: Mapping[int, set[int]],
) -> tuple[PlayThreadContext | None, str | None]:
    if not _looks_like_thread_channel(channel):
        return None, "This must be used in a forum post/thread."

    parent_id = getattr(channel, "parent_id", None)
    configured_tag_ids = configured_forums.get(parent_id, set())
    if not configured_tag_ids:
        return None, "This thread is not in a configured playthrough forum."

    if not is_playthrough_thread_name(channel.name):
        return None, "This thread is not named like a playthrough post."

    matched_tag_ids = frozenset(
        tag_id
        for tag_id in configured_tag_ids
        if thread_has_tag(channel, tag_id)
    )
    return (
        PlayThreadContext(
            thread=channel,
            configured_tag_ids=frozenset(configured_tag_ids),
            matched_tag_ids=matched_tag_ids,
        ),
        None,
    )


async def find_active_owned_playthrough_threads(
    guild: discord.Guild,
    configured_forums: Mapping[int, set[int]],
    member: discord.Member,
) -> list[discord.Thread]:
    active_threads = await guild.active_threads()
    matches: list[discord.Thread] = []

    for thread in active_threads:
        if getattr(thread, "archived", False):
            continue

        context, _ = configured_playthrough_thread_context(
            thread,
            configured_forums,
        )
        if context is None:
            continue
        if thread_name_matches_player(context.thread, member):
            matches.append(context.thread)

    return matches


def _missing_permissions(
    target: object,
    member: discord.Member,
    required_permissions: tuple[tuple[str, str], ...],
) -> list[str]:
    permissions_for = getattr(target, "permissions_for", None)
    if not callable(permissions_for):
        return []

    permissions = permissions_for(member)
    return [
        label
        for attr, label in required_permissions
        if not getattr(permissions, attr)
    ]


def missing_play_message_management_permissions(
    thread: discord.Thread,
    member: discord.Member,
) -> list[str]:
    return _missing_permissions(
        thread,
        member,
        _PLAY_MESSAGE_MANAGEMENT_REQUIRED_PERMS,
    )


def missing_play_thread_management_permissions(
    thread: discord.Thread,
    member: discord.Member,
) -> list[str]:
    return _missing_permissions(
        thread,
        member,
        _PLAY_THREAD_MANAGEMENT_REQUIRED_PERMS,
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
    *,
    game_tag_applied: bool = True,
) -> tuple[list[discord.ForumTag], str | None]:
    seen_ids = {required_tag.id}
    normalized_tag_ids: list[int] = []

    for tag_id in selected_tag_ids:
        if tag_id in seen_ids:
            if tag_id == required_tag.id and game_tag_applied:
                return (
                    [],
                    "The selected game tag is applied automatically and cannot be selected again.",
                )
            continue

        normalized_tag_ids.append(tag_id)
        seen_ids.add(tag_id)

    max_additional_tags = MAX_FORUM_THREAD_TAGS - (1 if game_tag_applied else 0)
    if len(normalized_tag_ids) > max_additional_tags:
        return [], f"Select at most **{max_additional_tags}** additional spoiler tags."

    resolved: list[discord.ForumTag] = []
    for tag_id in normalized_tag_ids:
        tag = forum.get_tag(tag_id)
        if tag is None:
            return (
                [],
                "One of the selected spoiler tags is no longer available. Run `/play new` again.",
            )

        resolved.append(tag)

    return resolved, None


def thread_name_matches_player(thread: discord.Thread, member: discord.Member) -> bool:
    owner_username = play_thread_owner_username(thread.name)
    if owner_username is None:
        return False

    username = _clean_thread_name_part(_member_username(member)).casefold()
    return _clean_thread_name_part(owner_username).casefold() == username


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
        if thread_name_matches_playthrough(thread, game, member):
            return thread
        if thread_has_tag(thread, tag.id) and thread_name_matches_player(thread, member):
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
    def __init__(self, tags: list[discord.ForumTag], *, max_tags: int) -> None:
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
            max_values=min(max_tags, len(options)),
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
            max_tags = MAX_FORUM_THREAD_TAGS - (1 if replay else 0)
            self.add_item(_AdditionalSpoilerSelect(selectable_tags, max_tags=max_tags))

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


def _thread_reference(thread: discord.Thread) -> str:
    mention = getattr(thread, "mention", None)
    if mention:
        return mention
    return f"**{escape_untrusted_text(getattr(thread, 'name', 'thread'))}**"


def _message_preview(message: discord.Message, *, max_length: int = 180) -> str:
    content = re.sub(r"\s+", " ", getattr(message, "content", "") or "").strip()
    if content:
        return escape_untrusted_text(content, max_length=max_length)

    attachments = getattr(message, "attachments", ())
    if attachments:
        count = len(attachments)
        return f"[{count} attachment{'s' if count != 1 else ''}]"

    embeds = getattr(message, "embeds", ())
    if embeds:
        count = len(embeds)
        return f"[{count} embed{'s' if count != 1 else ''}]"

    return "[no text content]"


def _thread_last_post_description(thread: discord.Thread) -> str:
    return f"Last post: {thread_last_activity_at(thread).date().isoformat()}"


def _thread_select_option(thread: discord.Thread) -> discord.SelectOption:
    return discord.SelectOption(
        label=(thread.name or f"Thread {thread.id}")[:100],
        description=_thread_last_post_description(thread),
        value=str(thread.id),
    )


def _game_select_option(game: PlayGame) -> discord.SelectOption:
    return discord.SelectOption(
        label=game.display_name[:100],
        value=game.key[:100],
    )


def _spoiler_tag_value(forum_id: int, tag_id: int) -> str:
    return f"{forum_id}:{tag_id}"


class _EndPlayThreadModal(discord.ui.Modal):
    def __init__(
        self,
        *,
        cog: "Play",
        requester_id: int,
        guild_id: int,
        threads: list[discord.Thread],
    ) -> None:
        super().__init__(title="End Channel", timeout=180)
        self.cog = cog
        self.requester_id = requester_id
        self.guild_id = guild_id
        self.threads = {
            thread.id: thread
            for thread in threads[:_PLAY_MODAL_OPTION_LIMIT]
        }

        self.thread_select = discord.ui.Select(
            placeholder="Make a selection",
            min_values=1,
            max_values=1,
            options=[
                _thread_select_option(thread)
                for thread in threads[:_PLAY_MODAL_OPTION_LIMIT]
            ],
            required=True,
        )
        self.add_item(
            discord.ui.Label(
                text="Channel Select",
                component=self.thread_select,
                description="Select a channel to end",
            )
        )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message(
                "Only the user who started this confirmation can submit it.",
                ephemeral=True,
            )
            return
        if interaction.guild_id != self.guild_id:
            await interaction.response.send_message(
                "This confirmation is no longer valid.",
                ephemeral=True,
            )
            return

        try:
            selected_thread_id = int(self.thread_select.values[0])
        except (IndexError, ValueError):
            await interaction.response.send_message(
                "Choose a playthrough post to end.",
                ephemeral=True,
            )
            return

        thread = self.threads.get(selected_thread_id)
        if thread is None:
            await interaction.response.send_message(
                "That playthrough post is no longer available.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        await self.cog.end_playthrough_thread_from_confirmation(
            interaction,
            thread=thread,
        )


class _PlayMessageActionConfirmModal(discord.ui.Modal):
    def __init__(
        self,
        *,
        cog: "Play",
        requester_id: int,
        guild_id: int,
        message: discord.Message,
        action: str,
    ) -> None:
        title = {
            "delete": "Delete Message",
            "pin": "Pin Message",
        }[action]
        super().__init__(title=title, timeout=180)
        self.cog = cog
        self.requester_id = requester_id
        self.guild_id = guild_id
        self.message = message
        self.action = action

        prompt = {
            "delete": "Do you want to delete this message?",
            "pin": "Do you want to pin this message?",
        }[action]
        self.quote = discord.ui.TextDisplay(
            f"> {_message_preview(message, max_length=900)}"
        )
        self.question = discord.ui.TextDisplay(prompt)
        self.add_item(self.quote)
        self.add_item(self.question)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message(
                "Only the user who started this confirmation can submit it.",
                ephemeral=True,
            )
            return
        if interaction.guild_id != self.guild_id:
            await interaction.response.send_message(
                "This confirmation is no longer valid.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        await self.cog.confirm_play_message_action(
            interaction,
            message=self.message,
            action=self.action,
        )


class _NewPlaythroughModal(discord.ui.Modal):
    def __init__(
        self,
        *,
        cog: "Play",
        requester_id: int,
        guild_id: int,
        game_options: list[discord.SelectOption],
        spoiler_options: list[discord.SelectOption],
    ) -> None:
        super().__init__(title="New Playthrough", timeout=180)
        self.cog = cog
        self.requester_id = requester_id
        self.guild_id = guild_id

        self.game_select = discord.ui.Select(
            placeholder="Make a selection",
            min_values=1,
            max_values=1,
            options=game_options[:_PLAY_MODAL_OPTION_LIMIT],
            required=True,
        )
        self.add_item(
            discord.ui.Label(
                text="Visual Novel",
                component=self.game_select,
                description="Create a new playthrough",
            )
        )

        self.replay_select = discord.ui.Select(
            placeholder="First playthrough",
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(
                    label="First playthrough",
                    value="false",
                    default=True,
                ),
                discord.SelectOption(label="Replay", value="true"),
            ],
            required=True,
        )
        self.add_item(
            discord.ui.Label(
                text="Replay",
                component=self.replay_select,
                description="Mark this playthrough as a replay",
            )
        )

        self.spoiler_select: discord.ui.Select | None = None
        if spoiler_options:
            self.spoiler_select = discord.ui.Select(
                placeholder="No additional spoilers",
                min_values=0,
                max_values=min(
                    MAX_FORUM_THREAD_TAGS,
                    len(spoiler_options),
                ),
                options=spoiler_options[:_PLAY_MODAL_OPTION_LIMIT],
                required=False,
            )
            self.add_item(
                discord.ui.Label(
                    text="Spoiler Tags",
                    component=self.spoiler_select,
                    description="Please select up to 5 spoiler tags (4 for replays)",
                )
            )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message(
                "Only the user who started this playthrough can submit it.",
                ephemeral=True,
            )
            return
        if interaction.guild_id != self.guild_id:
            await interaction.response.send_message(
                "This playthrough setup is no longer valid.",
                ephemeral=True,
            )
            return

        try:
            game_key = self.game_select.values[0]
        except IndexError:
            await interaction.response.send_message(
                "Choose a Visual Novel for this playthrough.",
                ephemeral=True,
            )
            return

        selected_tag_values = (
            list(self.spoiler_select.values)
            if self.spoiler_select is not None
            else []
        )
        replay = self.replay_select.values[:1] == ["true"]

        await self.cog.start_playthrough_from_modal(
            interaction,
            game_key=game_key,
            selected_tag_values=selected_tag_values,
            replay=replay,
        )


class _DeletePlayThreadModal(discord.ui.Modal):
    def __init__(
        self,
        *,
        cog: "Play",
        requester_id: int,
        guild_id: int,
        threads: list[discord.Thread],
    ) -> None:
        super().__init__(title="Delete Channel", timeout=180)
        self.cog = cog
        self.requester_id = requester_id
        self.guild_id = guild_id
        self.threads = {thread.id: thread for thread in threads[:_PLAY_MODAL_OPTION_LIMIT]}

        self.warning = discord.ui.TextDisplay(
            "THIS WILL DELETE YOUR CHANNEL PERMANENTLY."
        )
        self.thread_select = discord.ui.Select(
            placeholder="Make a selection",
            min_values=1,
            max_values=1,
            options=[
                _thread_select_option(thread)
                for thread in threads[:_PLAY_MODAL_OPTION_LIMIT]
            ],
            required=True,
        )
        self.add_item(self.warning)
        self.add_item(
            discord.ui.Label(
                text="Channel Select",
                component=self.thread_select,
                description="Select a channel to be removed:",
            )
        )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message(
                "Only the user who started this deletion can submit it.",
                ephemeral=True,
            )
            return
        if interaction.guild_id != self.guild_id:
            await interaction.response.send_message(
                "This deletion is no longer valid.",
                ephemeral=True,
            )
            return

        try:
            selected_thread_id = int(self.thread_select.values[0])
        except (IndexError, ValueError):
            await interaction.response.send_message(
                "Choose a playthrough post to remove.",
                ephemeral=True,
            )
            return

        thread = self.threads.get(selected_thread_id)
        if thread is None:
            await interaction.response.send_message(
                "That playthrough post is no longer available.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        await self.cog.delete_playthrough_thread_from_confirmation(
            interaction,
            thread=thread,
        )


class _UnlockPlayThreadModal(discord.ui.Modal):
    def __init__(
        self,
        *,
        cog: "Play",
        requester_id: int,
        guild_id: int,
        threads: list[discord.Thread],
    ) -> None:
        super().__init__(title="Unlock Channel", timeout=180)
        self.cog = cog
        self.requester_id = requester_id
        self.guild_id = guild_id
        self.threads = {thread.id: thread for thread in threads[:25]}

        self.thread_select = discord.ui.Select(
            placeholder="Locked or archived playthrough post",
            min_values=1,
            max_values=1,
            options=[
                _thread_select_option(thread)
                for thread in threads[:25]
            ],
            required=True,
        )
        self.add_item(
            discord.ui.Label(
                text="Which channel should be unlocked?",
                component=self.thread_select,
            )
        )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message(
                "Only the user who started this confirmation can submit it.",
                ephemeral=True,
            )
            return
        if interaction.guild_id != self.guild_id:
            await interaction.response.send_message(
                "This confirmation is no longer valid.",
                ephemeral=True,
            )
            return

        try:
            selected_thread_id = int(self.thread_select.values[0])
        except (IndexError, ValueError):
            await interaction.response.send_message(
                "Choose a playthrough post to unlock.",
                ephemeral=True,
            )
            return

        thread = self.threads.get(selected_thread_id)
        if thread is None:
            await interaction.response.send_message(
                "That playthrough post is no longer available.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        await self.cog.unlock_playthrough_thread_from_confirmation(
            interaction,
            thread=thread,
        )


class Play(commands.Cog):
    """
    Visual Novel playthrough forum posts.

    Commands: /play new, /play delete, /play end, /play unlock, message context menus
    """

    play = app_commands.Group(
        name="play",
        description="Visual Novel playthrough commands.",
        guild_only=True,
    )

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.play_store = PlayStore()
        self.module_store = ConfigStore()
        self._inflight_creates: set[tuple[int, int, str]] = set()
        self._cache_dir = CACHE_DIR
        self._lock_grace_days = max(0, PLAY_ARCHIVED_LOCK_GRACE_DAYS)
        self._context_menu_commands = tuple(
            app_commands.guild_only(
                app_commands.ContextMenu(name=name, callback=callback)
            )
            for name, callback in (
                ("Delete Message", self.delete_message_context_menu),
                ("Pin Message", self.pin_message_context_menu),
                ("Unlock Channel", self.unlock_channel_context_menu),
            )
        )

    @property
    def context_menu_commands(self) -> tuple[app_commands.ContextMenu, ...]:
        return self._context_menu_commands

    def cog_unload(self):
        tree = getattr(self.bot, "tree", None)
        if tree is not None:
            for name in _PLAY_CONTEXT_MENU_NAMES:
                tree.remove_command(
                    name,
                    type=discord.AppCommandType.message,
                )
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
        return self.play_store.configured_play_forums(guild_id)

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

    async def _resolve_play_create_context(
        self,
        interaction: discord.Interaction,
        *,
        game_key: str,
    ) -> PlayCreateContext | None:
        config = self.play_store.get_config(interaction.guild.id)
        play_game = self.play_store.get_game(interaction.guild.id, game_key)
        if play_game is None:
            await interaction.response.send_message(
                "That game is not configured for playthroughs on this server.",
                ephemeral=True,
            )
            return None

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
            return None

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
            return None

        bot_member = interaction.guild.me
        if bot_member is None:
            await interaction.response.send_message(
                "Could not read my server member data.",
                ephemeral=True,
            )
            return None

        missing_permissions = missing_play_forum_permissions(forum, bot_member)
        if missing_permissions:
            needed = ", ".join(f"**{permission}**" for permission in missing_permissions)
            await interaction.response.send_message(
                f"I do not have the required permissions in {forum.mention}.\n\n"
                f"Grant me: {needed}.",
                ephemeral=True,
            )
            return None

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
            return None

        return PlayCreateContext(
            play_game=play_game,
            forum=forum,
            required_tag=tag,
            auto_archive_duration=(
                config.auto_archive_duration
                if config and config.auto_archive_duration
                else None
            ),
        )

    def _decode_selected_spoiler_tag_ids(
        self,
        selected_tag_values: list[str],
        *,
        forum_id: int,
    ) -> tuple[list[int], str | None]:
        selected_tag_ids: list[int] = []
        for raw_value in selected_tag_values:
            try:
                raw_forum_id, raw_tag_id = raw_value.split(":", 1)
                selected_forum_id = int(raw_forum_id)
                selected_tag_id = int(raw_tag_id)
            except (ValueError, TypeError):
                return (
                    [],
                    "One of the selected spoiler tags is no longer available. Run `/play new` again.",
                )

            if selected_forum_id != forum_id:
                return (
                    [],
                    "One of the selected spoiler tags is not available for this game's forum.",
                )

            selected_tag_ids.append(selected_tag_id)

        return selected_tag_ids, None

    async def _play_spoiler_tag_options(
        self,
        guild: discord.Guild,
        games: list[PlayGame],
    ) -> list[discord.SelectOption]:
        config = self.play_store.get_config(guild.id)
        options: list[discord.SelectOption] = []
        seen_tag_keys: set[tuple[int, int]] = set()
        for game in games:
            forum_id = game.forum_channel_id or (
                config.forum_channel_id if config else None
            )
            if forum_id is None:
                continue

            forum = await self._get_forum_channel(guild, forum_id)
            if forum is None:
                continue

            for tag in forum.available_tags:
                tag_key = (forum.id, tag.id)
                if tag_key in seen_tag_keys:
                    continue

                options.append(
                    discord.SelectOption(
                        label=(tag.name or f"Tag {tag.id}")[:100],
                        value=_spoiler_tag_value(forum.id, tag.id),
                    )
                )
                seen_tag_keys.add(tag_key)
                if len(options) >= _PLAY_MODAL_OPTION_LIMIT:
                    return options

        return options

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
            if replay and required_tag.name
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
            selected_tag_ids=selected_tag_ids or [],
            replay=replay,
            auto_archive_duration=auto_archive_duration,
        )
        view.message = await interaction.edit_original_response(
            content=(
                f"An active {safe_game_name} playthrough by you was found: "
                f"{_thread_reference(existing_thread)}\n"
                "Would you like to archive it?"
            ),
            view=view,
            allowed_mentions=NO_MENTIONS,
        )

    async def start_playthrough_from_modal(
        self,
        interaction: discord.Interaction,
        *,
        game_key: str,
        selected_tag_values: list[str],
        replay: bool,
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

        context = await self._resolve_play_create_context(
            interaction,
            game_key=game_key,
        )
        if context is None:
            return

        selected_tag_ids, tag_error = self._decode_selected_spoiler_tag_ids(
            selected_tag_values,
            forum_id=context.forum.id,
        )
        if tag_error is not None:
            await interaction.response.send_message(tag_error, ephemeral=True)
            return

        selected_tag_ids = [
            tag_id
            for tag_id in selected_tag_ids
            if tag_id != context.required_tag.id
        ]
        _, tag_error = resolve_additional_spoiler_tags(
            context.forum,
            context.required_tag,
            selected_tag_ids,
            game_tag_applied=replay,
        )
        if tag_error is not None:
            await interaction.response.send_message(tag_error, ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        try:
            existing_thread = await find_active_play_thread(
                interaction.guild,
                context.forum,
                context.play_game,
                context.required_tag,
                interaction.user,
            )
        except discord.HTTPException as e:
            guild_id = interaction.guild.id
            log(
                f"PLAY // ACTIVE THREAD LOOKUP FAILED 『 GUILD {guild_id} 』 // {e}",
                level="debug",
                logger_name="play",
            )
            await interaction.edit_original_response(
                content=(
                    "I couldn't check existing active playthrough posts right now. "
                    "Please try again in a moment."
                )
            )
            return

        if existing_thread is not None:
            await self._send_duplicate_confirmation(
                interaction,
                existing_thread=existing_thread,
                forum=context.forum,
                play_game=context.play_game,
                required_tag=context.required_tag,
                selected_tag_ids=selected_tag_ids,
                replay=replay,
                auto_archive_duration=context.auto_archive_duration,
            )
            return

        await self.create_playthrough_thread(
            interaction,
            forum=context.forum,
            play_game=context.play_game,
            required_tag=context.required_tag,
            selected_tag_ids=selected_tag_ids,
            replay=replay,
            auto_archive_duration=context.auto_archive_duration,
            check_duplicate=False,
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
            game_tag_applied=replay,
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
        check_duplicate: bool = True,
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
            game_tag_applied=replay,
        )
        if error is not None:
            await interaction.edit_original_response(content=error, view=None)
            return

        self._inflight_creates.add(lock_key)
        try:
            if check_duplicate:
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
        auto_tags = [required_tag] if replay else []

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
                applied_tags=[*auto_tags, *extra_tags],
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

    def _configured_thread_context(
        self,
        guild_id: int,
        channel: object,
    ) -> tuple[PlayThreadContext | None, str | None]:
        return configured_playthrough_thread_context(
            channel,
            self._configured_play_forums(guild_id),
        )

    def _owned_thread_context(
        self,
        guild_id: int,
        channel: object,
        member: discord.Member,
    ) -> tuple[PlayThreadContext | None, str | None]:
        context, error = self._configured_thread_context(guild_id, channel)
        if error is not None:
            return None, error
        if context is None:
            return None, "This thread is not a configured playthrough post."
        if not thread_name_matches_player(context.thread, member):
            return (
                None,
                "You can only manage playthrough posts named for your Discord username.",
            )
        return context, None

    async def _send_end_thread_modal(
        self,
        interaction: discord.Interaction,
        threads: list[discord.Thread],
    ) -> None:
        await interaction.response.send_modal(
            _EndPlayThreadModal(
                cog=self,
                requester_id=interaction.user.id,
                guild_id=interaction.guild.id,
                threads=threads,
            )
        )

    async def end_playthrough_thread_from_confirmation(
        self,
        interaction: discord.Interaction,
        *,
        thread: discord.Thread,
        success_label: str = "Ended",
        audit_action: str = "ended",
    ) -> None:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.edit_original_response(
                content="This can only be used inside a server.",
                view=None,
            )
            return

        context, error = self._owned_thread_context(
            interaction.guild.id,
            thread,
            interaction.user,
        )
        if error is not None or context is None:
            await interaction.edit_original_response(
                content=f"{error or 'That playthrough post is no longer valid'} No changes were made.",
                view=None,
            )
            return

        if getattr(context.thread, "archived", False):
            await interaction.edit_original_response(
                content=(
                    f"{_thread_reference(context.thread)} is already archived. "
                    "No changes were made."
                ),
                view=None,
            )
            return

        bot_member = interaction.guild.me
        if bot_member is None:
            await interaction.edit_original_response(
                content="Could not read my server member data. No changes were made.",
                view=None,
            )
            return

        missing_permissions = missing_play_thread_management_permissions(
            context.thread,
            bot_member,
        )
        if missing_permissions:
            needed = ", ".join(f"**{permission}**" for permission in missing_permissions)
            await interaction.edit_original_response(
                content=(
                    f"I cannot archive {_thread_reference(context.thread)}.\n\n"
                    f"Grant me: {needed}."
                ),
                view=None,
            )
            return

        try:
            await context.thread.edit(
                archived=True,
                locked=True,
                reason=(
                    f"Playthrough {audit_action} by "
                    f"{interaction.user} ({interaction.user.id})"
                ),
            )
        except discord.Forbidden:
            await interaction.edit_original_response(
                content=(
                    f"I could not archive {_thread_reference(context.thread)}. "
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
            f"PLAY // THREAD {audit_action.upper()} 『 THREAD {context.thread.id} 』 "
            f"USER 『 {interaction.user.id} 』 GUILD 『 {interaction.guild.id} 』",
            level="debug",
            logger_name="play",
        )

        await interaction.edit_original_response(
            content=f"{success_label} playthrough post: {_thread_reference(context.thread)}",
            view=None,
        )

    async def delete_playthrough_thread_from_confirmation(
        self,
        interaction: discord.Interaction,
        *,
        thread: discord.Thread,
    ) -> None:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.edit_original_response(
                content="This can only be used inside a server.",
                view=None,
            )
            return

        context, error = self._owned_thread_context(
            interaction.guild.id,
            thread,
            interaction.user,
        )
        if error is not None or context is None:
            await interaction.edit_original_response(
                content=f"{error or 'That playthrough post is no longer valid'} No changes were made.",
                view=None,
            )
            return

        permission_error = self._missing_bot_thread_permissions(
            interaction.guild,
            context.thread,
        )
        if permission_error is not None:
            await interaction.edit_original_response(
                content=f"{permission_error} No changes were made.",
                view=None,
                allowed_mentions=NO_MENTIONS,
            )
            return

        try:
            await context.thread.delete(
                reason=(
                    f"Playthrough deleted by "
                    f"{interaction.user} ({interaction.user.id})"
                ),
            )
        except discord.Forbidden:
            await interaction.edit_original_response(
                content=(
                    f"I could not delete {_thread_reference(context.thread)}. "
                    "Check my Manage Threads permission."
                ),
                view=None,
            )
            return
        except discord.HTTPException as e:
            await interaction.edit_original_response(
                content=f"Discord rejected the delete request: `{e}`",
                view=None,
            )
            return

        log(
            f"PLAY // THREAD DELETED 『 THREAD {context.thread.id} 』 "
            f"USER 『 {interaction.user.id} 』 GUILD 『 {interaction.guild.id} 』",
            level="debug",
            logger_name="play",
        )

        await interaction.edit_original_response(
            content="Deleted Playthrough Post.",
            view=None,
        )

    def _is_unlockable_playthrough_thread(
        self,
        guild_id: int,
        thread: object,
        member: discord.Member,
    ) -> bool:
        context, error = self._owned_thread_context(guild_id, thread, member)
        if error is not None or context is None:
            return False
        return _thread_needs_unlock(context.thread)

    async def _find_unlockable_playthrough_threads(
        self,
        guild: discord.Guild,
        member: discord.Member,
        *,
        selected_thread: discord.Thread | None = None,
    ) -> list[discord.Thread]:
        configured_forums = self._configured_play_forums(guild.id)
        threads: list[discord.Thread] = []
        seen_thread_ids: set[int] = set()

        def add_if_unlockable(thread: discord.Thread) -> None:
            if thread.id in seen_thread_ids:
                return
            if not self._is_unlockable_playthrough_thread(guild.id, thread, member):
                return

            threads.append(thread)
            seen_thread_ids.add(thread.id)

        if selected_thread is not None:
            add_if_unlockable(selected_thread)

        active_threads = getattr(guild, "active_threads", None)
        if callable(active_threads):
            try:
                for thread in await active_threads():
                    if getattr(thread, "parent_id", None) in configured_forums:
                        add_if_unlockable(thread)
            except discord.HTTPException as e:
                log(
                    f"PLAY // UNLOCK ACTIVE THREAD SCAN FAILED "
                    f"『 GUILD {guild.id} 』 // {e}",
                    level="debug",
                    logger_name="play",
                )

        for forum_id in sorted(configured_forums):
            forum = await self._get_forum_channel(guild, forum_id)
            if forum is None:
                continue

            try:
                async for thread in forum.archived_threads(limit=None):
                    add_if_unlockable(thread)
            except discord.Forbidden:
                log(
                    f"PLAY // UNLOCK ARCHIVED THREAD SCAN FORBIDDEN "
                    f"『 GUILD {guild.id} 』 FORUM 『 {forum_id} 』",
                    level="debug",
                    logger_name="play",
                )
                continue
            except discord.HTTPException as e:
                log(
                    f"PLAY // UNLOCK ARCHIVED THREAD SCAN FAILED "
                    f"『 GUILD {guild.id} 』 FORUM 『 {forum_id} 』 // {e}",
                    level="debug",
                    logger_name="play",
                )
                continue

        return threads

    async def _send_unlock_thread_prompt(
        self,
        interaction: discord.Interaction,
        threads: list[discord.Thread],
    ) -> None:
        modal = _UnlockPlayThreadModal(
            cog=self,
            requester_id=interaction.user.id,
            guild_id=interaction.guild.id,
            threads=threads,
        )
        await interaction.response.send_modal(modal)

    async def unlock_playthrough_thread_from_confirmation(
        self,
        interaction: discord.Interaction,
        *,
        thread: discord.Thread,
    ) -> None:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.edit_original_response(
                content="This can only be used inside a server.",
                view=None,
            )
            return

        context, error = self._owned_thread_context(
            interaction.guild.id,
            thread,
            interaction.user,
        )
        if error is not None or context is None:
            await interaction.edit_original_response(
                content=f"{error or 'That playthrough post is no longer valid'} No changes were made.",
                view=None,
            )
            return

        unlock_locked = bool(getattr(context.thread, "locked", False))
        unlock_archived = bool(getattr(context.thread, "archived", False))
        if not unlock_locked and not unlock_archived:
            await interaction.edit_original_response(
                content=f"Unlocked playthrough post: {_thread_reference(context.thread)}",
                view=None,
            )
            return

        permission_error = self._missing_bot_thread_permissions(
            interaction.guild,
            context.thread,
        )
        if permission_error is not None:
            await interaction.edit_original_response(
                content=f"{permission_error} No changes were made.",
                view=None,
                allowed_mentions=NO_MENTIONS,
            )
            return

        try:
            reason = (
                f"Playthrough reopened by "
                f"{interaction.user} ({interaction.user.id})"
            )
            if unlock_archived:
                await context.thread.edit(
                    archived=False,
                    reason=reason,
                )
            if unlock_locked:
                await context.thread.edit(
                    locked=False,
                    reason=reason,
                )
        except discord.Forbidden:
            await interaction.edit_original_response(
                content=(
                    "I could not unlock that playthrough post. "
                    "Check my Manage Threads permission."
                ),
                view=None,
            )
            return
        except discord.HTTPException as e:
            await interaction.edit_original_response(
                content=f"Discord rejected the unlock request: `{e}`",
                view=None,
            )
            return

        await interaction.edit_original_response(
            content=f"Unlocked playthrough post: {_thread_reference(context.thread)}",
            view=None,
        )

    async def _validate_play_message_target(
        self,
        interaction: discord.Interaction,
        message: discord.Message,
    ) -> tuple[discord.Thread | None, str | None]:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            return None, "This can only be used inside a server."

        context, error = self._owned_thread_context(
            interaction.guild.id,
            getattr(message, "channel", None),
            interaction.user,
        )
        if error is not None or context is None:
            return None, error or "That message is not in a configured playthrough post."

        return context.thread, None

    def _missing_bot_message_permissions(
        self,
        guild: discord.Guild,
        thread: discord.Thread,
    ) -> str | None:
        bot_member = guild.me
        if bot_member is None:
            return "Could not read my server member data."

        missing_permissions = missing_play_message_management_permissions(
            thread,
            bot_member,
        )
        if not missing_permissions:
            return None

        needed = ", ".join(f"**{permission}**" for permission in missing_permissions)
        return f"I cannot manage messages in {_thread_reference(thread)}.\n\nGrant me: {needed}."

    def _missing_bot_thread_permissions(
        self,
        guild: discord.Guild,
        thread: discord.Thread,
    ) -> str | None:
        bot_member = guild.me
        if bot_member is None:
            return "Could not read my server member data."

        missing_permissions = missing_play_thread_management_permissions(
            thread,
            bot_member,
        )
        if not missing_permissions:
            return None

        needed = ", ".join(f"**{permission}**" for permission in missing_permissions)
        return f"I cannot manage {_thread_reference(thread)}.\n\nGrant me: {needed}."

    async def _delete_protection_error(
        self,
        guild: discord.Guild,
        message: discord.Message,
    ) -> str | None:
        author = getattr(message, "author", None)
        author_id = getattr(author, "id", None)
        if author_id is None:
            return "I could not verify who wrote that message, so I will not delete it."

        bot_user = getattr(self.bot, "user", None)
        bot_user_id = getattr(bot_user, "id", None)
        bot_member_id = getattr(getattr(guild, "me", None), "id", None)
        if author_id in {bot_user_id, bot_member_id}:
            return _PLAY_DELETE_PROTECTED_AUTHOR_MESSAGE

        try:
            guild_config = self.module_store.get_guild_config(guild.id)
        except RuntimeError:
            return None

        admin_role_id = getattr(guild_config, "admin_role_id", None)
        if admin_role_id is None:
            return None

        member = author if hasattr(author, "roles") else None
        if member is None:
            get_member = getattr(guild, "get_member", None)
            if callable(get_member):
                member = get_member(author_id)

        if member is None:
            fetch_member = getattr(guild, "fetch_member", None)
            if callable(fetch_member):
                try:
                    member = await fetch_member(author_id)
                except (discord.Forbidden, discord.HTTPException, discord.NotFound):
                    return (
                        "I could not verify whether that message author has the "
                        "configured Amadeus admin role, so I will not delete it."
                    )

        if member is None or not hasattr(member, "roles"):
            return (
                "I could not verify whether that message author has the configured "
                "Amadeus admin role, so I will not delete it."
            )

        if any(role.id == admin_role_id for role in getattr(member, "roles", ())):
            return _PLAY_DELETE_PROTECTED_AUTHOR_MESSAGE

        return None

    async def _send_message_action_prompt(
        self,
        interaction: discord.Interaction,
        *,
        message: discord.Message,
        thread: discord.Thread,
        action: str,
    ) -> None:
        modal = _PlayMessageActionConfirmModal(
            cog=self,
            requester_id=interaction.user.id,
            guild_id=interaction.guild.id,
            message=message,
            action=action,
        )
        await interaction.response.send_modal(modal)

    async def _handle_play_message_context(
        self,
        interaction: discord.Interaction,
        message: discord.Message,
        *,
        action: str,
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

        thread, error = await self._validate_play_message_target(
            interaction,
            message,
        )
        if error is not None or thread is None:
            await interaction.response.send_message(
                error or "That message is not in a configured playthrough post.",
                ephemeral=True,
            )
            return

        if action in {"delete", "pin"} and (
            getattr(thread, "archived", False) or getattr(thread, "locked", False)
        ):
            await interaction.response.send_message(
                "Please unlock the channel before trying to manage messages",
                ephemeral=True,
            )
            return

        if action in {"delete", "pin"}:
            permission_error = self._missing_bot_message_permissions(
                interaction.guild,
                thread,
            )
            if permission_error is not None:
                await interaction.response.send_message(
                    permission_error,
                    ephemeral=True,
                    allowed_mentions=NO_MENTIONS,
                )
                return

        if action == "pin" and getattr(message, "pinned", False):
            await interaction.response.send_message(
                "That message is already pinned.",
                ephemeral=True,
            )
            return

        if action == "delete":
            protection_error = await self._delete_protection_error(
                interaction.guild,
                message,
            )
            if protection_error is not None:
                await interaction.response.send_message(
                    protection_error,
                    ephemeral=True,
                    allowed_mentions=NO_MENTIONS,
                )
                return

        if action == "unlock":
            if not _thread_needs_unlock(thread):
                await interaction.response.send_message(
                    "That playthrough post is already unlocked.",
                    ephemeral=True,
                )
                return

            permission_error = self._missing_bot_thread_permissions(
                interaction.guild,
                thread,
            )
            if permission_error is not None:
                await interaction.response.send_message(
                    permission_error,
                    ephemeral=True,
                    allowed_mentions=NO_MENTIONS,
                )
                return

            threads = await self._find_unlockable_playthrough_threads(
                interaction.guild,
                interaction.user,
                selected_thread=thread,
            )
            if not threads:
                await interaction.response.send_message(
                    (
                        "I could not find a locked or archived playthrough post "
                        "owned by your Discord username."
                    ),
                    ephemeral=True,
                )
                return

            await self._send_unlock_thread_prompt(interaction, threads)
            return

        await self._send_message_action_prompt(
            interaction,
            message=message,
            thread=thread,
            action=action,
        )

    async def confirm_play_message_action(
        self,
        interaction: discord.Interaction,
        *,
        message: discord.Message,
        action: str,
    ) -> None:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.edit_original_response(
                content="This can only be used inside a server.",
                view=None,
            )
            return

        thread, error = await self._validate_play_message_target(
            interaction,
            message,
        )
        if error is not None or thread is None:
            await interaction.edit_original_response(
                content=f"{error or 'That message is no longer valid'} No changes were made.",
                view=None,
            )
            return

        if action in {"delete", "pin"} and (
            getattr(thread, "archived", False) or getattr(thread, "locked", False)
        ):
            await interaction.edit_original_response(
                content="Please unlock the channel before trying to manage messages",
                view=None,
            )
            return

        if action in {"delete", "pin"}:
            permission_error = self._missing_bot_message_permissions(
                interaction.guild,
                thread,
            )
            if permission_error is not None:
                await interaction.edit_original_response(
                    content=f"{permission_error} No changes were made.",
                    view=None,
                    allowed_mentions=NO_MENTIONS,
                )
                return

        if action == "delete":
            protection_error = await self._delete_protection_error(
                interaction.guild,
                message,
            )
            if protection_error is not None:
                content = protection_error
                if protection_error != _PLAY_DELETE_PROTECTED_AUTHOR_MESSAGE:
                    content = f"{protection_error} No changes were made."
                await interaction.edit_original_response(
                    content=content,
                    view=None,
                    allowed_mentions=NO_MENTIONS,
                )
                return

            try:
                await message.delete()
            except discord.Forbidden:
                await interaction.edit_original_response(
                    content=(
                        "I could not delete that message. "
                        "Check my Manage Messages permission."
                    ),
                    view=None,
                )
                return
            except discord.HTTPException as e:
                await interaction.edit_original_response(
                    content=f"Discord rejected the delete request: `{e}`",
                    view=None,
                )
                return

            await interaction.edit_original_response(
                content=f"Deleted the message in {_thread_reference(thread)}.",
                view=None,
            )
            return

        if action == "pin":
            if getattr(message, "pinned", False):
                await interaction.edit_original_response(
                    content="That message is already pinned. No changes were made.",
                    view=None,
                )
                return

            try:
                await message.pin(
                    reason=(
                        f"Playthrough message pinned by "
                        f"{interaction.user} ({interaction.user.id})"
                    ),
                )
            except discord.Forbidden:
                await interaction.edit_original_response(
                    content=(
                        "I could not pin that message. "
                        "Check my Manage Messages permission."
                    ),
                    view=None,
                )
                return
            except discord.HTTPException as e:
                await interaction.edit_original_response(
                    content=f"Discord rejected the pin request: `{e}`",
                    view=None,
                )
                return

            await interaction.edit_original_response(
                content=f"Pinned the message in {_thread_reference(thread)}.",
                view=None,
            )
            return

    async def delete_message_context_menu(
        self,
        interaction: discord.Interaction,
        message: discord.Message,
    ) -> None:
        await self._handle_play_message_context(
            interaction,
            message,
            action="delete",
        )

    async def pin_message_context_menu(
        self,
        interaction: discord.Interaction,
        message: discord.Message,
    ) -> None:
        await self._handle_play_message_context(
            interaction,
            message,
            action="pin",
        )

    async def unlock_channel_context_menu(
        self,
        interaction: discord.Interaction,
        message: discord.Message,
    ) -> None:
        await self._handle_play_message_context(
            interaction,
            message,
            action="unlock",
        )

    @play.command(
        name="new",
        description="Create a personal Visual Novel playthrough post.",
    )
    async def play_new(
        self,
        interaction: discord.Interaction,
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

        games = self.play_store.list_games(interaction.guild.id)
        if not games:
            await interaction.response.send_message(
                (
                    "No games are configured for playthroughs on this server. "
                    "Ask an admin to run `/amadeus play add-game` first."
                ),
                ephemeral=True,
            )
            return

        game_options = [
            _game_select_option(game)
            for game in games[:_PLAY_MODAL_OPTION_LIMIT]
        ]
        spoiler_options = await self._play_spoiler_tag_options(
            interaction.guild,
            games[:_PLAY_MODAL_OPTION_LIMIT],
        )

        await interaction.response.send_modal(
            _NewPlaythroughModal(
                cog=self,
                requester_id=interaction.user.id,
                guild_id=interaction.guild.id,
                game_options=game_options,
                spoiler_options=spoiler_options,
            )
        )

    @play.command(
        name="delete",
        description="Permanently delete one of your active Visual Novel playthrough posts.",
    )
    async def play_delete(
        self,
        interaction: discord.Interaction,
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

        try:
            threads = await find_active_owned_playthrough_threads(
                interaction.guild,
                self._configured_play_forums(interaction.guild.id),
                interaction.user,
            )
        except discord.HTTPException as e:
            guild_id = interaction.guild.id
            log(
                f"PLAY // ACTIVE THREAD LOOKUP FAILED 『 GUILD {guild_id} 』 // {e}",
                level="debug",
                logger_name="play",
            )
            await interaction.response.send_message(
                (
                    "I couldn't check your active playthrough posts right now. "
                    "Please try again in a moment."
                ),
                ephemeral=True,
            )
            return

        if not threads:
            await interaction.response.send_message(
                "I couldn't find an active playthrough post owned by your Discord username.",
                ephemeral=True,
            )
            return

        await interaction.response.send_modal(
            _DeletePlayThreadModal(
                cog=self,
                requester_id=interaction.user.id,
                guild_id=interaction.guild.id,
                threads=threads,
            )
        )

    @play.command(
        name="end",
        description="End one of your active Visual Novel playthrough posts.",
    )
    async def play_end(
        self,
        interaction: discord.Interaction,
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

        current_context, _ = self._configured_thread_context(
            interaction.guild.id,
            interaction.channel,
        )
        if current_context is not None:
            if not thread_name_matches_player(current_context.thread, interaction.user):
                await interaction.response.send_message(
                    (
                        "This playthrough post is named for another Discord username, "
                        "so you cannot end it."
                    ),
                    ephemeral=True,
                )
                return

            if getattr(current_context.thread, "archived", False):
                await interaction.response.send_message(
                    f"{_thread_reference(current_context.thread)} is already archived.",
                    ephemeral=True,
                    allowed_mentions=NO_MENTIONS,
                )
                return

            await self._send_end_thread_modal(
                interaction,
                [current_context.thread],
            )
            return

        try:
            threads = await find_active_owned_playthrough_threads(
                interaction.guild,
                self._configured_play_forums(interaction.guild.id),
                interaction.user,
            )
        except discord.HTTPException as e:
            guild_id = interaction.guild.id
            log(
                f"PLAY // ACTIVE THREAD LOOKUP FAILED 『 GUILD {guild_id} 』 // {e}",
                level="debug",
                logger_name="play",
            )
            await interaction.response.send_message(
                (
                    "I couldn't check your active playthrough posts right now. "
                    "Please try again in a moment."
                ),
                ephemeral=True,
            )
            return

        if not threads:
            await interaction.response.send_message(
                "I couldn't find an active playthrough post owned by your Discord username.",
                ephemeral=True,
            )
            return

        await self._send_end_thread_modal(interaction, threads)

    @play.command(
        name="unlock",
        description="Unlock one of your locked or archived Visual Novel playthrough posts.",
    )
    async def play_unlock(
        self,
        interaction: discord.Interaction,
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

        threads = await self._find_unlockable_playthrough_threads(
            interaction.guild,
            interaction.user,
        )
        if not threads:
            await interaction.response.send_message(
                (
                    "I could not find a locked or archived playthrough post "
                    "owned by your Discord username."
                ),
                ephemeral=True,
            )
            return

        await self._send_unlock_thread_prompt(interaction, threads)


async def setup(bot: commands.Bot):
    cog = Play(bot)
    tree = getattr(bot, "tree", None)
    added_context_menus: list[app_commands.ContextMenu] = []
    try:
        if tree is not None:
            for command in cog.context_menu_commands:
                tree.add_command(command)
                added_context_menus.append(command)
        await bot.add_cog(cog)
    except Exception:
        if tree is not None:
            for command in added_context_menus:
                tree.remove_command(
                    command.name,
                    type=discord.AppCommandType.message,
                )
        cog._lock_archived_playthroughs.cancel()
        cog.play_store.close()
        cog.module_store.close()
        raise
