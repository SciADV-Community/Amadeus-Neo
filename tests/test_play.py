import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest

import cogs.play_admin as play_admin_module
from cogs.play_admin import MAX_FORUM_TAG_NAME_LENGTH, PlayAdmin, tag_name_error
from cogs.play import (
    MAX_FORUM_THREAD_TAGS,
    PLAY_LOCK_SWEEP_INITIAL_LOOKBACK_DAYS,
    Play,
    SPOILER_CHANNEL_FLAG,
    additional_spoiler_tags,
    calculate_spoiler_flags,
    find_active_play_thread,
    load_play_lock_sweep_checkpoint,
    find_forum_tag,
    format_play_thread_name,
    game_name_error,
    mark_thread_spoiler,
    missing_member_play_forum_permissions,
    missing_play_lock_sweep_permissions,
    missing_play_forum_permissions,
    play_lock_sweep_archive_stop_at,
    play_lock_sweep_checkpoint_path,
    play_lock_sweep_due,
    resolve_additional_spoiler_tags,
    save_play_lock_sweep_checkpoint,
    should_lock_archived_play_thread,
    thread_has_tag,
    thread_name_matches_player,
    thread_name_matches_playthrough,
)


class FakeForum:
    def __init__(self, tags, permissions):
        self.available_tags = tags
        self._tags = {tag.id: tag for tag in tags}
        self._permissions = permissions

    def get_tag(self, tag_id):
        return self._tags.get(tag_id)

    def permissions_for(self, member):
        return self._permissions


class FakeHttp:
    def __init__(self):
        self.calls = []

    async def edit_channel(self, channel_id, **kwargs):
        self.calls.append((channel_id, kwargs))


class FakeInteraction:
    def __init__(self, guild, *, user=None):
        self.guild = guild
        self.guild_id = getattr(guild, "id", None)
        self.user = user or SimpleNamespace(id=1)
        self.response = SimpleNamespace(
            sent_messages=[],
            defers=[],
            send_message=AsyncMock(side_effect=self._record_send_message),
            defer=AsyncMock(side_effect=self._record_defer),
        )
        self.edits = []

    async def _record_send_message(self, *args, **kwargs):
        self.response.sent_messages.append((args, kwargs))

    async def _record_defer(self, *args, **kwargs):
        self.response.defers.append((args, kwargs))

    async def edit_original_response(self, **kwargs):
        self.edits.append(kwargs)


class FakeThread:
    def __init__(
        self,
        *,
        name,
        parent_id,
        tags,
        mention="#thread",
        thread_id=100,
        archived=True,
        locked=False,
        archive_timestamp=None,
        created_at=None,
        last_message_id=None,
    ):
        self.id = thread_id
        self.name = name
        self.parent_id = parent_id
        self.applied_tags = tags
        self._applied_tags = []
        self.mention = mention
        self.archived = archived
        self.locked = locked
        self.archive_timestamp = archive_timestamp
        self.created_at = created_at or datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.last_message_id = last_message_id
        self.edit_calls = []

    async def edit(self, **kwargs):
        self.edit_calls.append(kwargs)
        if "locked" in kwargs:
            self.locked = kwargs["locked"]
        if "archived" in kwargs:
            self.archived = kwargs["archived"]


def test_game_name_validation_rejects_empty_long_control_and_mentions():
    assert game_name_error("Steins;Gate") is None
    assert game_name_error(" ") == "Game name cannot be empty."
    assert game_name_error("x" * 81) == "Game name must be 80 characters or fewer."
    assert game_name_error("Steins\x00Gate") == (
        "Game name cannot contain control or bidirectional formatting characters."
    )
    assert game_name_error("@everyone") == "Game name cannot contain Discord mention syntax."


def test_tag_name_validation_enforces_discord_forum_tag_limit():
    assert tag_name_error("x" * MAX_FORUM_TAG_NAME_LENGTH) is None
    assert tag_name_error("x" * (MAX_FORUM_TAG_NAME_LENGTH + 1)) == (
        "Forum tag name must be 20 characters or fewer."
    )
    assert tag_name_error(" ") == "Forum tag name cannot be empty."
    assert tag_name_error("@everyone") == "Forum tag name cannot contain Discord mention syntax."


def test_format_play_thread_name_uses_username_not_nickname():
    member = SimpleNamespace(name="zips", display_name="Server Nickname")
    assert (
        format_play_thread_name("Steins;Gate Re:Boot", member)
        == "Steins;Gate Re:Boot | @zips"
    )

    long_name = "A" * 140
    assert len(format_play_thread_name(long_name, member)) == 100
    assert format_play_thread_name(long_name, member).endswith(" | @zips")


def test_forum_tag_helpers_match_by_id_or_case_insensitive_name():
    gate = SimpleNamespace(id=10, name="Steins;Gate")
    zero = SimpleNamespace(id=20, name="Steins;Gate 0")
    forum = FakeForum([gate, zero], SimpleNamespace())

    assert find_forum_tag(forum, tag_id=10) is gate
    assert find_forum_tag(forum, name="steins;gate 0") is zero
    assert find_forum_tag(forum, tag_id=999, name="missing") is None

    thread = SimpleNamespace(applied_tags=[gate])
    assert thread_has_tag(thread, 10) is True
    assert thread_has_tag(thread, 20) is False
    assert (
        thread_has_tag(SimpleNamespace(applied_tags=[], _applied_tags=[20]), 20)
        is True
    )


def test_additional_spoiler_tags_excludes_required_game_tag():
    gate = SimpleNamespace(id=10, name="Steins;Gate")
    zero = SimpleNamespace(id=20, name="Steins;Gate 0")
    chaos = SimpleNamespace(id=30, name="Chaos;Head")
    forum = FakeForum([gate, zero, chaos], SimpleNamespace())

    assert additional_spoiler_tags(forum, gate) == [zero, chaos]


def test_resolve_additional_spoiler_tags_validates_ids_and_limit():
    gate = SimpleNamespace(id=10, name="Steins;Gate")
    tags = [gate] + [
        SimpleNamespace(id=20 + index, name=f"Tag {index}")
        for index in range(MAX_FORUM_THREAD_TAGS)
    ]
    forum = FakeForum(tags, SimpleNamespace())

    resolved, error = resolve_additional_spoiler_tags(forum, gate, [20, 21])
    assert error is None
    assert [tag.id for tag in resolved] == [20, 21]

    resolved, error = resolve_additional_spoiler_tags(forum, gate, [10])
    assert resolved == []
    assert error == "The selected game tag is applied automatically and cannot be selected again."

    resolved, error = resolve_additional_spoiler_tags(forum, gate, [999])
    assert resolved == []
    assert error == "One of the selected spoiler tags is no longer available. Run `/play` again."

    resolved, error = resolve_additional_spoiler_tags(
        forum,
        gate,
        [20 + index for index in range(MAX_FORUM_THREAD_TAGS)],
    )
    assert resolved == []
    assert error == "Select at most **4** additional spoiler tags."


def test_thread_name_player_match_is_case_insensitive():
    member = SimpleNamespace(name="zips", display_name="Server Nickname")
    thread = SimpleNamespace(name="Steins;Gate | @zips")
    assert thread_name_matches_player(thread, member) is True
    assert (
        thread_name_matches_player(
            SimpleNamespace(name="Steins;Gate | @Server Nickname"),
            member,
        )
        is False
    )


def test_thread_name_playthrough_match_requires_the_selected_game():
    member = SimpleNamespace(name="zips", display_name="Server Nickname")
    gate = SimpleNamespace(display_name="Steins;Gate")
    zero = SimpleNamespace(display_name="Steins;Gate 0")
    thread = SimpleNamespace(name="Steins;Gate | @zips")

    assert thread_name_matches_playthrough(thread, gate, member) is True
    assert thread_name_matches_playthrough(thread, zero, member) is False


def test_find_active_play_thread_matches_username_in_active_post_name():
    tag = SimpleNamespace(id=10, name="Steins;Gate")
    forum = SimpleNamespace(id=20)
    member = SimpleNamespace(name="zips", display_name="Server Nickname")
    game = SimpleNamespace(display_name="Steins;Gate")
    nickname_thread = FakeThread(
        name="Steins;Gate | @Server Nickname",
        parent_id=20,
        tags=[tag],
    )
    username_thread = FakeThread(
        name="Steins;Gate replay | @zips",
        parent_id=20,
        tags=[tag],
    )

    class Guild:
        async def active_threads(self):
            return [nickname_thread, username_thread]

    assert (
        asyncio.run(find_active_play_thread(Guild(), forum, game, tag, member))
        is username_thread
    )


def test_missing_play_forum_permissions_lists_only_missing_permissions():
    permissions = SimpleNamespace(
        view_channel=True,
        send_messages=True,
        create_public_threads=False,
        send_messages_in_threads=True,
        manage_threads=False,
        manage_channels=True,
    )
    forum = FakeForum([], permissions)

    assert missing_play_forum_permissions(forum, SimpleNamespace()) == [
        "Manage Threads",
    ]


def test_missing_member_play_forum_permissions_checks_member_access_only():
    permissions = SimpleNamespace(
        view_channel=False,
        send_messages=False,
        create_public_threads=False,
        send_messages_in_threads=True,
        manage_threads=False,
        manage_channels=False,
    )
    forum = FakeForum([], permissions)

    assert missing_member_play_forum_permissions(forum, SimpleNamespace()) == [
        "View Channels",
    ]


def test_missing_play_lock_sweep_permissions_requires_history_and_manage_threads():
    permissions = SimpleNamespace(
        view_channel=True,
        read_message_history=False,
        manage_threads=False,
    )
    forum = FakeForum([], permissions)

    assert missing_play_lock_sweep_permissions(forum, SimpleNamespace()) == [
        "Read Message History",
        "Manage Threads",
    ]


def test_play_lock_sweep_checkpoint_round_trips(tmp_path):
    started_at = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)
    completed_at = started_at + timedelta(seconds=5)
    archive_stop_at = started_at - timedelta(days=14)

    save_play_lock_sweep_checkpoint(
        tmp_path,
        guild_id=1,
        forum_channel_id=2,
        started_at=started_at,
        completed_at=completed_at,
        archive_stop_at=archive_stop_at,
        scanned_count=9,
        locked_count=3,
    )

    path = play_lock_sweep_checkpoint_path(tmp_path, 1, 2)
    checkpoint = load_play_lock_sweep_checkpoint(tmp_path, 1, 2)

    assert path == tmp_path / "1" / "play_lock_sweeps" / "2.json"
    assert path.exists()
    assert checkpoint["guild_id"] == "1"
    assert checkpoint["forum_channel_id"] == "2"
    assert checkpoint["last_scanned_count"] == 9
    assert checkpoint["last_locked_count"] == 3


def test_play_lock_sweep_due_uses_completed_checkpoint_time():
    now = datetime(2026, 8, 31, tzinfo=timezone.utc)
    checkpoint = {
        "last_sweep_completed_at": "2026-08-01T00:00:00+00:00",
    }

    assert play_lock_sweep_due(checkpoint, now) is True
    assert play_lock_sweep_due(checkpoint, now - timedelta(days=1)) is False
    assert play_lock_sweep_due(None, now) is True


def test_play_lock_sweep_archive_stop_overlaps_by_grace_window():
    now = datetime(2026, 8, 31, tzinfo=timezone.utc)
    checkpoint = {
        "last_sweep_started_at": "2026-08-01T00:00:00+00:00",
    }

    assert play_lock_sweep_archive_stop_at(checkpoint, now, 14) == datetime(
        2026,
        7,
        18,
        tzinfo=timezone.utc,
    )
    assert play_lock_sweep_archive_stop_at(None, now, 14) == now - timedelta(
        days=PLAY_LOCK_SWEEP_INITIAL_LOOKBACK_DAYS
    )


def test_should_lock_archived_play_thread_filters_to_old_configured_play_posts():
    tag = SimpleNamespace(id=10, name="Steins;Gate")
    now = datetime(2026, 8, 31, tzinfo=timezone.utc)
    old_message_id = discord.utils.time_snowflake(now - timedelta(days=15))
    recent_message_id = discord.utils.time_snowflake(now - timedelta(days=10))
    old_thread = FakeThread(
        name="Steins;Gate | @zips",
        parent_id=20,
        tags=[tag],
        archive_timestamp=now - timedelta(days=8),
        last_message_id=old_message_id,
    )

    assert should_lock_archived_play_thread(
        old_thread,
        configured_tag_ids={10},
        now=now,
        grace_days=14,
    ) is True
    assert should_lock_archived_play_thread(
        FakeThread(
            name="Steins;Gate | @zips",
            parent_id=20,
            tags=[tag],
            locked=True,
            last_message_id=old_message_id,
        ),
        configured_tag_ids={10},
        now=now,
        grace_days=14,
    ) is False
    assert should_lock_archived_play_thread(
        FakeThread(
            name="Steins;Gate | @zips",
            parent_id=20,
            tags=[tag],
            last_message_id=recent_message_id,
        ),
        configured_tag_ids={10},
        now=now,
        grace_days=14,
    ) is False
    assert should_lock_archived_play_thread(
        FakeThread(
            name="Steins;Gate | @zips",
            parent_id=20,
            tags=[SimpleNamespace(id=99)],
            last_message_id=old_message_id,
        ),
        configured_tag_ids={10},
        now=now,
        grace_days=14,
    ) is False
    assert should_lock_archived_play_thread(
        FakeThread(
            name="General spoilers",
            parent_id=20,
            tags=[tag],
            last_message_id=old_message_id,
        ),
        configured_tag_ids={10},
        now=now,
        grace_days=14,
    ) is False


def test_archived_playthrough_sweep_locks_old_threads_and_writes_checkpoint(
    temp_db_path,
    tmp_path,
):
    tag = SimpleNamespace(id=10, name="Steins;Gate")
    now = datetime(2026, 8, 31, tzinfo=timezone.utc)
    eligible_thread = FakeThread(
        name="Steins;Gate | @zips",
        parent_id=20,
        tags=[tag],
        archive_timestamp=now - timedelta(days=8),
        last_message_id=discord.utils.time_snowflake(now - timedelta(days=15)),
    )
    recent_thread = FakeThread(
        name="Steins;Gate 0 | @zips",
        parent_id=20,
        tags=[tag],
        archive_timestamp=now - timedelta(days=3),
        last_message_id=discord.utils.time_snowflake(now - timedelta(days=10)),
    )
    out_of_window_thread = FakeThread(
        name="Steins;Gate | @kurisu",
        parent_id=20,
        tags=[tag],
        archive_timestamp=now - timedelta(days=45),
        last_message_id=discord.utils.time_snowflake(now - timedelta(days=60)),
    )

    class Forum:
        id = 20

        def archived_threads(self, *, limit=None):
            async def iterator():
                yield eligible_thread
                yield recent_thread
                yield out_of_window_thread

            return iterator()

    cog = Play(SimpleNamespace())
    cog._cache_dir = tmp_path

    try:
        scanned_count, locked_count = asyncio.run(
            cog._sweep_archived_playthroughs_for_forum(
                SimpleNamespace(id=1),
                Forum(),
                {10},
                None,
                now,
            )
        )
    finally:
        cog.cog_unload()

    assert scanned_count == 2
    assert locked_count == 1
    assert eligible_thread.edit_calls == [
        {
            "archived": True,
            "locked": True,
            "reason": "Lock inactive archived playthrough post",
        }
    ]
    assert recent_thread.edit_calls == []
    assert load_play_lock_sweep_checkpoint(tmp_path, 1, 20)["last_locked_count"] == 1


def test_play_auto_archive_command_runs_manual_sweep(temp_db_path, monkeypatch):
    async def allow_access(interaction, store):
        return SimpleNamespace()

    monkeypatch.setattr(play_admin_module, "require_amadeus_access", allow_access)

    permissions = SimpleNamespace(
        view_channel=True,
        read_message_history=True,
        manage_threads=True,
    )
    channel = SimpleNamespace(
        id=20,
        name="steins-gate",
        mention="#playthroughs",
        permissions_for=lambda member: permissions,
    )
    guild = SimpleNamespace(id=1, me=SimpleNamespace())
    interaction = FakeInteraction(guild)
    play_cog = Play(SimpleNamespace())
    play_cog.play_store.save_game(1, "Steins;Gate", 20, 10)
    play_cog.run_archived_playthrough_lock_sweep = AsyncMock(return_value=(6, 4))
    bot = SimpleNamespace(get_cog=lambda name: play_cog if name == "Play" else None)
    admin_cog = PlayAdmin(bot)
    admin_cog._get_forum_channel = AsyncMock(return_value=channel)

    try:
        asyncio.run(
            PlayAdmin.play_auto_archive.callback(
                admin_cog,
                interaction,
                str(channel.id),
                21,
            )
        )
    finally:
        admin_cog.cog_unload()
        play_cog.cog_unload()

    interaction.response.defer.assert_awaited_once_with(
        ephemeral=True,
        thinking=True,
    )
    play_cog.run_archived_playthrough_lock_sweep.assert_awaited_once_with(
        guild,
        channel,
        grace_days=21,
    )
    assert interaction.edits == [
        {
            "content": (
                "Archived playthrough sweep complete for #playthroughs.\n"
                "Scanned **6** archived posts and locked **4** eligible posts.\n"
                "Grace period: **21 days**."
            ),
        }
    ]


def test_play_auto_archive_command_rejects_unconfigured_forum(
    temp_db_path,
    monkeypatch,
):
    async def allow_access(interaction, store):
        return SimpleNamespace()

    monkeypatch.setattr(play_admin_module, "require_amadeus_access", allow_access)

    guild = SimpleNamespace(id=1, me=SimpleNamespace())
    interaction = FakeInteraction(guild)
    play_cog = Play(SimpleNamespace())
    bot = SimpleNamespace(get_cog=lambda name: play_cog if name == "Play" else None)
    admin_cog = PlayAdmin(bot)
    admin_cog._get_forum_channel = AsyncMock()

    try:
        asyncio.run(
            PlayAdmin.play_auto_archive.callback(
                admin_cog,
                interaction,
                "20",
                None,
            )
        )
    finally:
        admin_cog.cog_unload()
        play_cog.cog_unload()

    interaction.response.send_message.assert_awaited_once_with(
        "`20` is not configured for any `/play` games.",
        ephemeral=True,
    )
    admin_cog._get_forum_channel.assert_not_awaited()


def test_play_auto_archive_autocomplete_lists_only_configured_forums(temp_db_path):
    configured_channel = SimpleNamespace(id=20, name="steins-gate")
    unconfigured_channel = SimpleNamespace(id=30, name="general")

    class Guild:
        id = 1

        def get_channel(self, channel_id):
            return {
                20: configured_channel,
                30: unconfigured_channel,
            }.get(channel_id)

    interaction = SimpleNamespace(guild=Guild())
    admin_cog = PlayAdmin(SimpleNamespace(get_cog=lambda name: None))
    admin_cog.play_store.save_game(1, "Steins;Gate", 20, 10)

    try:
        choices = asyncio.run(
            PlayAdmin.configured_forum_autocomplete(
                admin_cog,
                interaction,
                "",
            )
        )
    finally:
        admin_cog.cog_unload()

    assert [(choice.name, choice.value) for choice in choices] == [
        ("#steins-gate (1 games)", "20")
    ]


def test_play_admin_interaction_check_requires_enabled_module():
    interaction = SimpleNamespace(
        guild_id=1,
        response=SimpleNamespace(send_message=AsyncMock()),
    )
    cog = SimpleNamespace(
        module_store=SimpleNamespace(
            is_module_enabled=lambda guild_id, module_name: False
        )
    )

    assert asyncio.run(PlayAdmin.interaction_check(cog, interaction)) is False
    interaction.response.send_message.assert_awaited_once_with(
        "The **play** module is not enabled on this server.\n"
        "Enable it first with `/amadeus module enable play`.",
        ephemeral=True,
    )


@pytest.mark.filterwarnings("ignore:'count' is passed as positional argument:DeprecationWarning")
def test_duplicate_confirmation_prompt_offers_no_then_yes(temp_db_path):
    interaction = FakeInteraction(
        SimpleNamespace(id=1),
        user=SimpleNamespace(id=123),
    )
    cog = Play(SimpleNamespace())

    try:
        asyncio.run(
            cog._send_duplicate_confirmation(
                interaction,
                existing_thread=SimpleNamespace(mention="#steins-gate"),
                forum=SimpleNamespace(id=10),
                play_game=SimpleNamespace(display_name="Steins;Gate", key="steins;gate"),
                required_tag=SimpleNamespace(id=20),
                selected_tag_ids=None,
                replay=False,
                auto_archive_duration=None,
            )
        )
    finally:
        cog.cog_unload()

    assert interaction.edits[0]["content"] == (
        "An active Steins;Gate playthrough by you was found: #steins-gate.\n"
        "Would you like to archive it and create a new channel?"
    )
    assert [item.label for item in interaction.edits[0]["view"].children] == [
        "No",
        "Yes",
    ]


def test_active_thread_lookup_failure_edits_original_response(temp_db_path):
    class Guild:
        id = 1

        async def active_threads(self):
            response = SimpleNamespace(status=500, reason="Internal Server Error")
            raise discord.HTTPException(response, "boom")

    interaction = FakeInteraction(Guild())
    cog = Play(SimpleNamespace())

    try:
        thread, ok = asyncio.run(
            cog._find_active_play_thread_or_respond(
                interaction,
                forum=SimpleNamespace(id=10),
                play_game=SimpleNamespace(display_name="Steins;Gate", key="steins;gate"),
                required_tag=SimpleNamespace(id=20),
                member=SimpleNamespace(id=30),
            )
        )
    finally:
        cog.cog_unload()

    assert thread is None
    assert ok is False
    assert interaction.edits == [
        {
            "content": (
                "I couldn't check existing active playthrough posts right now. "
                "Please try again in a moment."
            ),
            "view": None,
        }
    ]


def test_spoiler_flag_helper_preserves_existing_flags():
    assert calculate_spoiler_flags(0) == SPOILER_CHANNEL_FLAG
    assert calculate_spoiler_flags(2) == 2 | SPOILER_CHANNEL_FLAG


def test_mark_thread_spoiler_patches_channel_flags():
    http = FakeHttp()
    thread = SimpleNamespace(
        id=123,
        flags=SimpleNamespace(value=2),
        _state=SimpleNamespace(http=http),
    )

    asyncio.run(mark_thread_spoiler(thread, reason="test"))

    assert http.calls == [
        (123, {"flags": 2 | SPOILER_CHANNEL_FLAG, "reason": "test"})
    ]
