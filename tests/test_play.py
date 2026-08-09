import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest

import cogs.play_admin as play_admin_module
import cogs.play as play_module
from cogs.play_admin import MAX_FORUM_TAG_NAME_LENGTH, PlayAdmin, tag_name_error
from cogs.play import (
    MAX_FORUM_THREAD_TAGS,
    PLAY_LOCK_SWEEP_INITIAL_LOOKBACK_DAYS,
    Play,
    SPOILER_CHANNEL_FLAG,
    additional_spoiler_tags,
    calculate_spoiler_flags,
    configured_playthrough_thread_context,
    find_active_play_thread,
    find_active_owned_playthrough_threads,
    load_play_lock_sweep_checkpoint,
    find_forum_tag,
    format_play_thread_name,
    game_name_error,
    mark_thread_spoiler,
    missing_play_message_management_permissions,
    missing_member_play_forum_permissions,
    missing_play_lock_sweep_permissions,
    missing_play_thread_management_permissions,
    missing_play_forum_permissions,
    play_thread_owner_username,
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
    def __init__(
        self,
        tags,
        permissions,
        *,
        forum_id=20,
        name="play",
        mention="#play",
    ):
        self.id = forum_id
        self.name = name
        self.mention = mention
        self.available_tags = tags
        self._tags = {tag.id: tag for tag in tags}
        self._permissions = permissions
        self.delete = AsyncMock()

    def get_tag(self, tag_id):
        return self._tags.get(tag_id)

    def permissions_for(self, member):
        return self._permissions


class FakeMember(SimpleNamespace):
    def __init__(self, **kwargs):
        kwargs.setdefault("roles", [])
        super().__init__(**kwargs)


class FakeHttp:
    def __init__(self):
        self.calls = []

    async def edit_channel(self, channel_id, **kwargs):
        self.calls.append((channel_id, kwargs))


class FakeInteraction:
    def __init__(self, guild, *, user=None, channel=None):
        self.guild = guild
        self.guild_id = getattr(guild, "id", None)
        self.user = user or SimpleNamespace(id=1)
        self.channel = channel
        self.response = SimpleNamespace(
            sent_messages=[],
            defers=[],
            modals=[],
            message_edits=[],
            send_message=AsyncMock(side_effect=self._record_send_message),
            defer=AsyncMock(side_effect=self._record_defer),
            send_modal=AsyncMock(side_effect=self._record_send_modal),
            edit_message=AsyncMock(side_effect=self._record_response_edit_message),
        )
        self.edits = []

    async def _record_send_message(self, *args, **kwargs):
        self.response.sent_messages.append((args, kwargs))

    async def _record_defer(self, *args, **kwargs):
        self.response.defers.append((args, kwargs))

    async def _record_send_modal(self, modal):
        self.response.modals.append(modal)

    async def _record_response_edit_message(self, **kwargs):
        self.response.message_edits.append(kwargs)

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
        permissions=None,
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
        self._permissions = permissions or SimpleNamespace(
            manage_messages=True,
            manage_threads=True,
        )
        self.send = AsyncMock()
        self.delete = AsyncMock()

    async def edit(self, **kwargs):
        self.edit_calls.append(kwargs)
        if "locked" in kwargs:
            self.locked = kwargs["locked"]
        if "archived" in kwargs:
            self.archived = kwargs["archived"]

    def permissions_for(self, member):
        return self._permissions


class FakeMessage:
    def __init__(self, *, channel, content="message", pinned=False, author=None):
        self.channel = channel
        self.content = content
        self.pinned = pinned
        self.author = author or FakeMember(id=456, name="author", display_name="Author")
        self.attachments = []
        self.embeds = []
        self.delete = AsyncMock()
        self.pin = AsyncMock(side_effect=self._pin)

    async def _pin(self, **kwargs):
        self.pinned = True


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
    assert error == "One of the selected spoiler tags is no longer available. Run `/play new` again."

    resolved, error = resolve_additional_spoiler_tags(
        forum,
        gate,
        [20 + index for index in range(MAX_FORUM_THREAD_TAGS)],
    )
    assert resolved == []
    assert error == "Select at most **4** additional spoiler tags."

    resolved, error = resolve_additional_spoiler_tags(
        forum,
        gate,
        [10] + [20 + index for index in range(MAX_FORUM_THREAD_TAGS)],
        game_tag_applied=False,
    )
    assert error is None
    assert [tag.id for tag in resolved] == [
        20 + index for index in range(MAX_FORUM_THREAD_TAGS)
    ]


def test_thread_name_player_match_is_case_insensitive():
    member = SimpleNamespace(name="zips", display_name="Server Nickname")
    thread = SimpleNamespace(name="Steins;Gate | @zips")
    assert play_thread_owner_username(thread.name) == "zips"
    assert thread_name_matches_player(thread, member) is True
    assert (
        thread_name_matches_player(
            SimpleNamespace(name="Steins;Gate | @Server Nickname"),
            member,
        )
        is False
    )
    assert (
        thread_name_matches_player(
            SimpleNamespace(name="Steins;Gate | @anna"),
            SimpleNamespace(name="ann", display_name="Ann"),
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


def test_configured_playthrough_thread_context_filters_parent_tag_and_name():
    tag = SimpleNamespace(id=10, name="Steins;Gate")
    thread = FakeThread(
        name="Steins;Gate | @zips",
        parent_id=20,
        tags=[tag],
    )

    context, error = configured_playthrough_thread_context(thread, {20: {10}})
    assert error is None
    assert context.thread is thread
    assert context.configured_tag_ids == frozenset({10})
    assert context.matched_tag_ids == frozenset({10})

    assert configured_playthrough_thread_context(SimpleNamespace(), {20: {10}}) == (
        None,
        "This must be used in a forum post/thread.",
    )
    assert configured_playthrough_thread_context(thread, {30: {10}}) == (
        None,
        "This thread is not in a configured playthrough forum.",
    )
    untagged_context, error = configured_playthrough_thread_context(
        thread,
        {20: {99}},
    )
    assert error is None
    assert untagged_context.thread is thread
    assert untagged_context.matched_tag_ids == frozenset()
    assert configured_playthrough_thread_context(
        FakeThread(name="General", parent_id=20, tags=[tag]),
        {20: {10}},
    ) == (None, "This thread is not named like a playthrough post.")


def test_configured_play_forums_uses_default_forum_and_requires_tags(temp_db_path):
    cog = Play(SimpleNamespace())

    try:
        cog.play_store.set_forum_channel(1, 20)
        cog.play_store.save_game(1, "Steins;Gate", None, 10)
        cog.play_store.save_game(1, "Chaos;Head", 30, 40)
        cog.play_store.save_game(1, "Missing Tag", 30, None)

        assert cog.configured_play_tag_ids_for_forum(1, 20) == {10}
        assert cog.configured_play_tag_ids_for_forum(1, 30) == {40}
    finally:
        cog.cog_unload()


def test_find_active_owned_playthrough_threads_filters_to_unarchived_owned_posts():
    tag = SimpleNamespace(id=10, name="Steins;Gate")
    owned = FakeThread(
        name="Steins;Gate | @zips",
        parent_id=20,
        tags=[tag],
        thread_id=1,
        archived=False,
    )
    untagged_owned = FakeThread(
        name="Steins;Gate 0 | @zips",
        parent_id=20,
        tags=[],
        thread_id=4,
        archived=False,
    )
    other_user = FakeThread(
        name="Steins;Gate | @anna",
        parent_id=20,
        tags=[tag],
        thread_id=2,
        archived=False,
    )
    archived = FakeThread(
        name="Steins;Gate 0 | @zips",
        parent_id=20,
        tags=[tag],
        thread_id=3,
        archived=True,
    )

    class Guild:
        async def active_threads(self):
            return [owned, untagged_owned, other_user, archived]

    member = SimpleNamespace(name="zips", display_name="Server Nickname")
    assert asyncio.run(
        find_active_owned_playthrough_threads(Guild(), {20: {10}}, member)
    ) == [owned, untagged_owned]


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
    exact_untagged_thread = FakeThread(
        name="Steins;Gate | @zips",
        parent_id=20,
        tags=[],
    )
    username_thread = FakeThread(
        name="Steins;Gate replay | @zips",
        parent_id=20,
        tags=[tag],
    )

    class Guild:
        async def active_threads(self):
            return [nickname_thread, exact_untagged_thread, username_thread]

    assert (
        asyncio.run(find_active_play_thread(Guild(), forum, game, tag, member))
        is exact_untagged_thread
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


def test_missing_play_context_permissions_check_thread_permissions():
    thread = FakeThread(
        name="Steins;Gate | @zips",
        parent_id=20,
        tags=[],
        permissions=SimpleNamespace(
            manage_messages=False,
            manage_threads=False,
        ),
    )

    assert missing_play_message_management_permissions(thread, SimpleNamespace()) == [
        "Manage Messages"
    ]
    assert missing_play_thread_management_permissions(thread, SimpleNamespace()) == [
        "Manage Threads"
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
            tags=[],
            last_message_id=old_message_id,
        ),
        configured_tag_ids={10},
        now=now,
        grace_days=14,
    ) is True
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
        "`20` is not configured for any playthrough games.",
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


def test_play_admin_add_game_and_set_order_control_list_order(
    temp_db_path,
    monkeypatch,
):
    async def allow_access(interaction, store):
        return SimpleNamespace()

    monkeypatch.setattr(play_admin_module, "require_amadeus_access", allow_access)

    tags = [
        SimpleNamespace(id=10, name="Steins;Gate"),
        SimpleNamespace(id=20, name="Steins;Gate 0"),
        SimpleNamespace(id=30, name="Chaos;Head NoAH"),
    ]
    forum = FakeForum(
        tags,
        SimpleNamespace(),
        forum_id=20,
        mention="#playthroughs",
    )
    guild = SimpleNamespace(id=1, get_channel=lambda channel_id: forum)
    admin_cog = PlayAdmin(SimpleNamespace(get_cog=lambda name: None))
    admin_cog._get_forum_channel = AsyncMock(return_value=forum)

    try:
        asyncio.run(
            PlayAdmin.play_add_game.callback(
                admin_cog,
                FakeInteraction(guild),
                "Steins;Gate",
                forum,
                None,
                None,
            )
        )
        asyncio.run(
            PlayAdmin.play_add_game.callback(
                admin_cog,
                FakeInteraction(guild),
                "Steins;Gate 0",
                forum,
                None,
                None,
            )
        )
        add_interaction = FakeInteraction(guild)
        asyncio.run(
            PlayAdmin.play_add_game.callback(
                admin_cog,
                add_interaction,
                "Chaos;Head NoAH",
                forum,
                None,
                1,
            )
        )

        assert [(game.key, game.sort_order) for game in admin_cog.play_store.list_games(1)] == [
            ("chaos;head noah", 1),
            ("steins;gate", 2),
            ("steins;gate 0", 3),
        ]
        add_interaction.response.send_message.assert_awaited_once_with(
            "Added **Chaos;Head NoAH** to `/play new`; order **1**; "
            "forum #playthroughs; tag **Chaos;Head NoAH** linked.",
            ephemeral=True,
        )

        order_interaction = FakeInteraction(guild)
        asyncio.run(
            PlayAdmin.play_set_order.callback(
                admin_cog,
                order_interaction,
                "Steins;Gate 0",
                1,
            )
        )

        assert [(game.key, game.sort_order) for game in admin_cog.play_store.list_games(1)] == [
            ("steins;gate 0", 1),
            ("chaos;head noah", 2),
            ("steins;gate", 3),
        ]
        order_interaction.response.send_message.assert_awaited_once_with(
            "Set **Steins;Gate 0** to order **1** in `/play new`.",
            ephemeral=True,
        )

        list_interaction = FakeInteraction(guild)
        asyncio.run(PlayAdmin.play_list_games.callback(admin_cog, list_interaction))
    finally:
        admin_cog.cog_unload()

    _, kwargs = list_interaction.response.sent_messages[0]
    assert kwargs["ephemeral"] is True
    assert kwargs["embed"].description.splitlines() == [
        "**1. Steins;Gate 0** — Forum: #playthroughs — Tag: **Steins;Gate 0**",
        "**2. Chaos;Head NoAH** — Forum: #playthroughs — Tag: **Chaos;Head NoAH**",
        "**3. Steins;Gate** — Forum: #playthroughs — Tag: **Steins;Gate**",
    ]


def test_play_admin_remove_forum_confirms_with_configured_games(
    temp_db_path,
    monkeypatch,
):
    async def allow_access(interaction, store):
        return SimpleNamespace()

    monkeypatch.setattr(play_admin_module, "require_amadeus_access", allow_access)

    forum_a = FakeForum(
        [
            SimpleNamespace(id=10, name="Steins;Gate"),
            SimpleNamespace(id=20, name="Chaos;Head NoAH"),
        ],
        SimpleNamespace(),
        forum_id=20,
        name="science-adventure",
        mention="#science-adventure",
    )
    forum_b = FakeForum(
        [SimpleNamespace(id=30, name="Robotics;Notes")],
        SimpleNamespace(),
        forum_id=30,
        name="robotics-notes",
        mention="#robotics-notes",
    )
    forum_without_games = FakeForum(
        [],
        SimpleNamespace(),
        forum_id=40,
        name="empty-default",
        mention="#empty-default",
    )
    forums = {20: forum_a, 30: forum_b, 40: forum_without_games}
    guild = SimpleNamespace(id=1, get_channel=lambda channel_id: forums.get(channel_id))
    interaction = FakeInteraction(guild)
    admin_cog = PlayAdmin(SimpleNamespace(get_cog=lambda name: None))
    admin_cog._get_forum_channel = AsyncMock(
        side_effect=lambda guild, channel_id: forums.get(channel_id)
    )
    admin_cog.play_store.set_forum_channel(1, 40)
    admin_cog.play_store.save_game(1, "Steins;Gate", 20, 10)
    admin_cog.play_store.save_game(1, "Chaos;Head NoAH", 20, 20)
    admin_cog.play_store.save_game(1, "Robotics;Notes", 30, 30)

    try:
        asyncio.run(PlayAdmin.play_remove_forum.callback(admin_cog, interaction))
    finally:
        admin_cog.cog_unload()

    args, kwargs = interaction.response.sent_messages[0]
    assert kwargs["ephemeral"] is True
    assert args[0] == (
        "Select a configured playthrough forum to review the games that will be "
        "removed."
    )
    buttons = {
        item.label: item
        for item in kwargs["view"].children
        if isinstance(item, discord.ui.Button)
    }
    assert sorted(buttons) == ["Cancel", "Yes"]
    assert buttons["Yes"].disabled is True
    forum_select = next(
        item
        for item in kwargs["view"].children
        if getattr(item, "placeholder", None) == "Configured forum"
    )
    assert [option.value for option in forum_select.options] == ["20", "30"]
    assert not any(option.default for option in forum_select.options)
    assert not any(
        getattr(item, "placeholder", None) == "Configured forum"
        and any(option.value == "40" for option in item.options)
        for item in kwargs["view"].children
    )

    forum_select._values = ["20"]
    select_interaction = FakeInteraction(guild)
    asyncio.run(forum_select.callback(select_interaction))

    assert select_interaction.response.message_edits == [
        {
            "content": (
                "Are you sure you want to delete this Forum channel?\n\n"
                "Forum: #science-adventure\n"
                "This removes these games from Amadeus play configuration first.\n\n"
                "Configured games for this channel:\n"
                "- 1. Steins;Gate\n"
                "- 2. Chaos;Head NoAH"
            ),
            "view": kwargs["view"],
        }
    ]
    assert buttons["Yes"].disabled is False


def test_play_admin_remove_forum_confirmation_removes_games_and_reflows(
    temp_db_path,
    monkeypatch,
):
    async def allow_access(interaction, store):
        return SimpleNamespace()

    monkeypatch.setattr(play_admin_module, "require_amadeus_access", allow_access)

    forum_a = FakeForum(
        [],
        SimpleNamespace(),
        forum_id=20,
        name="science-adventure",
        mention="#science-adventure",
    )
    forum_b = FakeForum(
        [],
        SimpleNamespace(),
        forum_id=30,
        name="robotics-notes",
        mention="#robotics-notes",
    )
    forums = {20: forum_a, 30: forum_b}
    guild = SimpleNamespace(id=1, get_channel=lambda channel_id: forums.get(channel_id))
    interaction = FakeInteraction(guild)
    admin_cog = PlayAdmin(SimpleNamespace(get_cog=lambda name: None))
    admin_cog._get_forum_channel = AsyncMock(
        side_effect=lambda guild, channel_id: forums.get(channel_id)
    )
    admin_cog.play_store.set_forum_channel(1, 20)
    admin_cog.play_store.save_game(1, "Steins;Gate", 20, 10)
    admin_cog.play_store.save_game(1, "Chaos;Head NoAH", 20, 20)
    admin_cog.play_store.save_game(1, "Robotics;Notes", 30, 30)

    try:
        asyncio.run(
            admin_cog.remove_play_forum_from_confirmation(
                interaction,
                forum_id=20,
            )
        )
        remaining_games = admin_cog.play_store.list_games(1)
        config = admin_cog.play_store.get_config(1)
    finally:
        admin_cog.cog_unload()

    assert [(game.key, game.sort_order) for game in remaining_games] == [
        ("robotics;notes", 1),
    ]
    assert config.forum_channel_id is None
    forum_a.delete.assert_not_awaited()
    assert len(interaction.edits) == 1
    assert interaction.edits[0]["content"] == (
        "ARE YOU SURE?\n"
        "Removal will attempt to delete the channel and all contained threads."
    )
    assert sorted(
        item.label
        for item in interaction.edits[0]["view"].children
    ) == ["Cancel", "Delete Forum"]


def test_play_admin_second_remove_forum_confirmation_deletes_channel(
    temp_db_path,
):
    forum = FakeForum(
        [],
        SimpleNamespace(),
        forum_id=20,
        name="science-adventure",
        mention="#science-adventure",
    )
    guild = SimpleNamespace(id=1, get_channel=lambda channel_id: forum)
    interaction = FakeInteraction(guild)
    admin_cog = PlayAdmin(SimpleNamespace(get_cog=lambda name: None))
    admin_cog._get_forum_channel = AsyncMock(return_value=forum)

    try:
        asyncio.run(
            admin_cog.delete_play_forum_channel_from_confirmation(
                interaction,
                forum_id=20,
                forum_reference="#science-adventure",
            )
        )
    finally:
        admin_cog.cog_unload()

    forum.delete.assert_awaited_once_with(
        reason=f"Playthrough forum deleted by {interaction.user} ({interaction.user.id})"
    )
    assert interaction.edits == [
        {
            "content": "Deleted Discord forum channel #science-adventure.",
            "view": None,
        }
    ]


@pytest.mark.filterwarnings("ignore:'count' is passed as positional argument:DeprecationWarning")
def test_duplicate_confirmation_prompt_uses_ephemeral_buttons(temp_db_path):
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
        "An active Steins;Gate playthrough by you was found: #steins-gate\n"
        "Would you like to archive it?"
    )
    assert [item.label for item in interaction.edits[0]["view"].children] == [
        "No",
        "Yes",
    ]


def test_play_new_opens_modal_with_game_and_spoiler_selects(
    temp_db_path,
    monkeypatch,
):
    monkeypatch.setattr(play_module.discord, "Member", FakeMember)

    game_tag = SimpleNamespace(id=10, name="Steins;Gate")
    spoiler_tag = SimpleNamespace(id=50, name="General spoilers")
    forum = FakeForum(
        [game_tag, spoiler_tag],
        SimpleNamespace(),
        forum_id=20,
        name="playthroughs",
    )
    guild = SimpleNamespace(id=1, me=SimpleNamespace(id=999))
    user = FakeMember(id=123, name="zips", display_name="Server Nickname")
    interaction = FakeInteraction(guild, user=user)
    cog = Play(SimpleNamespace())
    cog.module_store.enable_module(1, "play")
    cog.play_store.save_game(1, "Steins;Gate", 20, 10)
    cog._get_forum_channel = AsyncMock(return_value=forum)

    try:
        asyncio.run(Play.play_new.callback(cog, interaction))
    finally:
        cog.cog_unload()

    modal = interaction.response.modals[0]
    assert modal.title == "New Playthrough"
    assert [(option.label, option.value) for option in modal.game_select.options] == [
        ("Steins;Gate", "steins;gate")
    ]
    assert modal.replay_select.options[0].label == "First playthrough"
    assert modal.spoiler_select is not None
    assert [
        (option.label, option.description, option.value)
        for option in modal.spoiler_select.options
    ] == [
        ("Steins;Gate", None, "20:10"),
        ("General spoilers", None, "20:50")
    ]
    assert modal.spoiler_select.max_values == 2
    spoiler_label = next(
        item
        for item in modal.children
        if getattr(item, "text", None) == "Spoiler Tags"
    )
    assert spoiler_label.description == (
        "Please select up to 5 spoiler tags (4 for replays)"
    )


def test_new_playthrough_modal_allows_five_spoiler_tags():
    game_options = [discord.SelectOption(label="Steins;Gate", value="steins;gate")]
    spoiler_options = [
        discord.SelectOption(label=f"Tag {index}", value=f"20:{index}")
        for index in range(MAX_FORUM_THREAD_TAGS + 1)
    ]

    modal = play_module._NewPlaythroughModal(
        cog=SimpleNamespace(),
        requester_id=123,
        guild_id=1,
        game_options=game_options,
        spoiler_options=spoiler_options,
    )

    assert modal.spoiler_select is not None
    assert modal.spoiler_select.max_values == MAX_FORUM_THREAD_TAGS


def test_play_new_modal_replay_rejects_five_extra_spoiler_tags(
    temp_db_path,
    monkeypatch,
):
    monkeypatch.setattr(play_module.discord, "Member", FakeMember)

    game_tag = SimpleNamespace(id=10, name="Steins;Gate")
    extra_tags = [
        SimpleNamespace(id=20 + index, name=f"Tag {index}")
        for index in range(MAX_FORUM_THREAD_TAGS)
    ]
    forum = FakeForum(
        [game_tag, *extra_tags],
        SimpleNamespace(
            view_channel=True,
            send_messages=True,
            create_public_threads=True,
            send_messages_in_threads=True,
            read_message_history=True,
            manage_channels=True,
            manage_threads=True,
        ),
        forum_id=20,
        name="playthroughs",
    )
    user = FakeMember(id=123, name="zips", display_name="Server Nickname")
    guild = SimpleNamespace(
        id=1,
        me=FakeMember(id=999),
        active_threads=AsyncMock(return_value=[]),
    )
    interaction = FakeInteraction(guild, user=user)
    cog = Play(SimpleNamespace())
    cog.module_store.enable_module(1, "play")
    cog.play_store.save_game(1, "Steins;Gate", 20, 10)
    cog._get_forum_channel = AsyncMock(return_value=forum)

    try:
        asyncio.run(Play.play_new.callback(cog, interaction))
        modal = interaction.response.modals[0]
        modal.game_select._values = ["steins;gate"]
        modal.replay_select._values = ["true"]
        modal.spoiler_select._values = [
            f"20:{20 + index}" for index in range(MAX_FORUM_THREAD_TAGS)
        ]

        submit_interaction = FakeInteraction(guild, user=user)
        asyncio.run(modal.on_submit(submit_interaction))
    finally:
        cog.cog_unload()

    guild.active_threads.assert_not_awaited()
    submit_interaction.response.send_message.assert_awaited_once_with(
        "Select at most **4** additional spoiler tags.",
        ephemeral=True,
    )


def test_play_new_modal_submit_shows_duplicate_buttons_before_creation(
    temp_db_path,
    monkeypatch,
):
    monkeypatch.setattr(play_module.discord, "Member", FakeMember)

    game_tag = SimpleNamespace(id=10, name="Steins;Gate")
    spoiler_tag = SimpleNamespace(id=50, name="General spoilers")
    forum = FakeForum(
        [game_tag, spoiler_tag],
        SimpleNamespace(
            view_channel=True,
            send_messages=True,
            create_public_threads=True,
            send_messages_in_threads=True,
            read_message_history=True,
            manage_channels=True,
            manage_threads=True,
        ),
        forum_id=20,
        name="playthroughs",
    )
    user = FakeMember(id=123, name="zips", display_name="Server Nickname")
    existing_thread = FakeThread(
        name="Steins;Gate | @zips",
        parent_id=20,
        tags=[game_tag],
        thread_id=30,
        archived=False,
        mention="#steins-gate",
    )
    guild = SimpleNamespace(
        id=1,
        me=FakeMember(id=999),
        active_threads=AsyncMock(return_value=[existing_thread]),
    )
    interaction = FakeInteraction(guild, user=user)
    cog = Play(SimpleNamespace())
    cog.module_store.enable_module(1, "play")
    cog.play_store.save_game(1, "Steins;Gate", 20, 10)
    cog._get_forum_channel = AsyncMock(return_value=forum)

    try:
        asyncio.run(Play.play_new.callback(cog, interaction))
        modal = interaction.response.modals[0]
        modal.game_select._values = ["steins;gate"]
        modal.replay_select._values = ["false"]
        modal.spoiler_select._values = ["20:10", "20:50"]

        submit_interaction = FakeInteraction(guild, user=user)
        asyncio.run(modal.on_submit(submit_interaction))

        duplicate_view = submit_interaction.edits[0]["view"]
    finally:
        cog.cog_unload()

    guild.active_threads.assert_awaited_once()
    assert existing_thread.edit_calls == []
    assert submit_interaction.response.defers == [((), {"ephemeral": True, "thinking": True})]
    assert submit_interaction.edits[0]["content"] == (
        "An active Steins;Gate playthrough by you was found: #steins-gate\n"
        "Would you like to archive it?"
    )
    assert [item.label for item in duplicate_view.children] == ["No", "Yes"]
    assert duplicate_view.selected_tag_ids == [50]


@pytest.mark.parametrize(
    ("replay", "expected_tag_ids"),
    [
        (False, [50]),
        (True, [10, 50]),
    ],
)
def test_create_playthrough_applies_game_tag_only_for_replays(
    temp_db_path,
    monkeypatch,
    replay,
    expected_tag_ids,
):
    monkeypatch.setattr(play_module, "mark_thread_spoiler", AsyncMock())

    game_tag = SimpleNamespace(id=10, name="Steins;Gate")
    spoiler_tag = SimpleNamespace(id=50, name="General spoilers")
    user = FakeMember(
        id=123,
        name="zips",
        display_name="Server Nickname",
        mention="<@123>",
    )
    guild = SimpleNamespace(id=1)
    interaction = FakeInteraction(guild, user=user)

    class RecordingForum(FakeForum):
        async def create_thread(self, **kwargs):
            self.create_kwargs = kwargs
            thread = FakeThread(
                name=kwargs["name"],
                parent_id=self.id,
                tags=kwargs["applied_tags"],
                archived=False,
            )
            return SimpleNamespace(thread=thread)

    forum = RecordingForum(
        [game_tag, spoiler_tag],
        SimpleNamespace(),
        forum_id=20,
    )
    cog = Play(SimpleNamespace())

    try:
        asyncio.run(
            cog._create_playthrough_thread_unlocked(
                interaction,
                forum=forum,
                play_game=SimpleNamespace(
                    display_name="Steins;Gate",
                    key="steins;gate",
                ),
                required_tag=game_tag,
                extra_tags=[spoiler_tag],
                replay=replay,
                auto_archive_duration=None,
            )
        )
    finally:
        cog.cog_unload()

    assert [
        tag.id for tag in forum.create_kwargs["applied_tags"]
    ] == expected_tag_ids


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


def test_play_end_current_thread_fast_path_skips_active_lookup(
    temp_db_path,
    monkeypatch,
):
    monkeypatch.setattr(play_module.discord, "Member", FakeMember)

    tag = SimpleNamespace(id=10, name="Steins;Gate")
    user = FakeMember(id=123, name="zips", display_name="Server Nickname")
    thread = FakeThread(
        name="Steins;Gate | @zips",
        parent_id=20,
        tags=[tag],
        thread_id=30,
        archived=False,
        archive_timestamp=datetime(2026, 6, 1, tzinfo=timezone.utc),
    )
    guild = SimpleNamespace(
        id=1,
        me=SimpleNamespace(id=999),
        active_threads=AsyncMock(return_value=[]),
    )
    interaction = FakeInteraction(guild, user=user, channel=thread)
    cog = Play(SimpleNamespace())
    cog.module_store.enable_module(1, "play")
    cog.play_store.save_game(1, "Steins;Gate", 20, 10)

    try:
        asyncio.run(Play.play_end.callback(cog, interaction))
    finally:
        cog.cog_unload()

    guild.active_threads.assert_not_awaited()
    modal = interaction.response.modals[0]
    assert modal.title == "End Channel"
    assert [
        (option.label, option.description, option.value)
        for option in modal.thread_select.options
    ] == [
        ("Steins;Gate | @zips", "Last post: 2026-06-01", "30")
    ]


def test_play_end_active_thread_fallback_lists_owned_threads(
    temp_db_path,
    monkeypatch,
):
    monkeypatch.setattr(play_module.discord, "Member", FakeMember)

    tag = SimpleNamespace(id=10, name="Steins;Gate")
    user = FakeMember(id=123, name="zips", display_name="Server Nickname")
    owned = FakeThread(
        name="Steins;Gate | @zips",
        parent_id=20,
        tags=[tag],
        thread_id=30,
        archived=False,
        archive_timestamp=datetime(2026, 7, 2, tzinfo=timezone.utc),
    )
    guild = SimpleNamespace(
        id=1,
        me=SimpleNamespace(id=999),
        active_threads=AsyncMock(return_value=[owned]),
    )
    interaction = FakeInteraction(guild, user=user, channel=SimpleNamespace())
    cog = Play(SimpleNamespace())
    cog.module_store.enable_module(1, "play")
    cog.play_store.save_game(1, "Steins;Gate", 20, 10)

    try:
        asyncio.run(Play.play_end.callback(cog, interaction))
    finally:
        cog.cog_unload()

    guild.active_threads.assert_awaited_once()
    modal = interaction.response.modals[0]
    assert modal.title == "End Channel"
    assert [
        (option.label, option.description, option.value)
        for option in modal.thread_select.options
    ] == [
        ("Steins;Gate | @zips", "Last post: 2026-07-02", "30")
    ]


def test_play_end_modal_revalidates_and_archives_selection(
    temp_db_path,
    monkeypatch,
):
    monkeypatch.setattr(play_module.discord, "Member", FakeMember)

    tag = SimpleNamespace(id=10, name="Steins;Gate")
    user = FakeMember(id=123, name="zips", display_name="Server Nickname")
    thread = FakeThread(
        name="Steins;Gate | @zips",
        parent_id=20,
        tags=[tag],
        thread_id=30,
        archived=False,
    )
    guild = SimpleNamespace(id=1, me=SimpleNamespace(id=999))
    cog = Play(SimpleNamespace())
    cog.play_store.save_game(1, "Steins;Gate", 20, 10)
    modal = play_module._EndPlayThreadModal(
        cog=cog,
        requester_id=user.id,
        guild_id=guild.id,
        threads=[thread],
    )
    modal.thread_select._values = ["30"]
    interaction = FakeInteraction(guild, user=user)

    try:
        asyncio.run(modal.on_submit(interaction))
    finally:
        cog.cog_unload()

    assert interaction.response.defers == [((), {"ephemeral": True, "thinking": True})]
    assert thread.edit_calls == [
        {
            "archived": True,
            "locked": True,
            "reason": f"Playthrough ended by {user} ({user.id})",
        }
    ]
    assert interaction.edits == [
        {
            "content": "Ended playthrough post: #thread",
            "view": None,
        }
    ]


def test_end_confirmation_revalidates_and_archives_thread(
    temp_db_path,
    monkeypatch,
):
    monkeypatch.setattr(play_module.discord, "Member", FakeMember)

    tag = SimpleNamespace(id=10, name="Steins;Gate")
    user = FakeMember(id=123, name="zips", display_name="Server Nickname")
    thread = FakeThread(
        name="Steins;Gate | @zips",
        parent_id=20,
        tags=[tag],
        thread_id=30,
        archived=False,
    )
    guild = SimpleNamespace(id=1, me=SimpleNamespace(id=999))
    interaction = FakeInteraction(guild, user=user)
    cog = Play(SimpleNamespace())
    cog.play_store.save_game(1, "Steins;Gate", 20, 10)

    try:
        asyncio.run(
            cog.end_playthrough_thread_from_confirmation(
                interaction,
                thread=thread,
            )
        )
    finally:
        cog.cog_unload()

    assert thread.edit_calls == [
        {
            "archived": True,
            "locked": True,
            "reason": f"Playthrough ended by {user} ({user.id})",
        }
    ]
    assert interaction.edits == [
        {
            "content": "Ended playthrough post: #thread",
            "view": None,
        }
    ]


def test_play_delete_modal_lists_owned_threads_and_removes_selection(
    temp_db_path,
    monkeypatch,
):
    monkeypatch.setattr(play_module.discord, "Member", FakeMember)

    tag = SimpleNamespace(id=10, name="Steins;Gate")
    user = FakeMember(id=123, name="zips", display_name="Server Nickname")
    owned = FakeThread(
        name="Steins;Gate | @zips",
        parent_id=20,
        tags=[tag],
        thread_id=30,
        archived=False,
        archive_timestamp=datetime(2026, 6, 1, tzinfo=timezone.utc),
    )
    other_user = FakeThread(
        name="Steins;Gate | @anna",
        parent_id=20,
        tags=[tag],
        thread_id=31,
        archived=False,
    )
    guild = SimpleNamespace(
        id=1,
        me=SimpleNamespace(id=999),
        active_threads=AsyncMock(return_value=[owned, other_user]),
    )
    interaction = FakeInteraction(guild, user=user)
    cog = Play(SimpleNamespace())
    cog.module_store.enable_module(1, "play")
    cog.play_store.save_game(1, "Steins;Gate", 20, 10)

    try:
        asyncio.run(Play.play_delete.callback(cog, interaction))
        modal = interaction.response.modals[0]
        assert modal.title == "Delete Channel"
        assert modal.warning.content == "THIS WILL DELETE YOUR CHANNEL PERMANENTLY."
        delete_label = next(
            item
            for item in modal.children
            if getattr(item, "text", None) == "Channel Select"
        )
        assert delete_label.description == "Select a channel to be removed:"
        assert [
            (option.label, option.description, option.value)
            for option in modal.thread_select.options
        ] == [
            ("Steins;Gate | @zips", "Last post: 2026-06-01", "30")
        ]
        modal.thread_select._values = ["30"]

        confirm_interaction = FakeInteraction(guild, user=user)
        asyncio.run(modal.on_submit(confirm_interaction))
    finally:
        cog.cog_unload()

    owned.delete.assert_awaited_once_with(
        reason=f"Playthrough deleted by {user} ({user.id})",
    )
    assert owned.edit_calls == []
    assert confirm_interaction.edits == [
        {
            "content": "Deleted Playthrough Post.",
            "view": None,
        }
    ]


def test_play_unlock_modal_lists_owned_locked_and_archived_threads(
    temp_db_path,
    monkeypatch,
):
    monkeypatch.setattr(play_module.discord, "Member", FakeMember)

    tag = SimpleNamespace(id=10, name="Steins;Gate")
    user = FakeMember(id=123, name="zips", display_name="Server Nickname")
    active_locked = FakeThread(
        name="Steins;Gate | @zips",
        parent_id=20,
        tags=[tag],
        thread_id=30,
        archived=False,
        locked=True,
        last_message_id=discord.utils.time_snowflake(
            datetime(2026, 8, 3, tzinfo=timezone.utc)
        ),
    )
    active_unlocked = FakeThread(
        name="Chaos;Head | @zips",
        parent_id=20,
        tags=[tag],
        thread_id=31,
        archived=False,
        locked=False,
    )
    archived_locked = FakeThread(
        name="Steins;Gate 0 | @zips",
        parent_id=20,
        tags=[tag],
        thread_id=32,
        mention="#archived-thread",
        archived=True,
        locked=True,
        archive_timestamp=datetime(2026, 7, 2, tzinfo=timezone.utc),
    )
    other_user_thread = FakeThread(
        name="Steins;Gate | @anna",
        parent_id=20,
        tags=[tag],
        thread_id=33,
        archived=True,
        locked=True,
    )

    class Forum:
        def archived_threads(self, *, limit=None):
            async def iterator():
                yield archived_locked
                yield other_user_thread

            return iterator()

    guild = SimpleNamespace(
        id=1,
        me=SimpleNamespace(id=999),
        active_threads=AsyncMock(return_value=[active_locked, active_unlocked]),
    )
    interaction = FakeInteraction(guild, user=user)
    cog = Play(SimpleNamespace())
    cog.module_store.enable_module(1, "play")
    cog.play_store.save_game(1, "Steins;Gate", 20, 10)
    cog._get_forum_channel = AsyncMock(return_value=Forum())

    try:
        asyncio.run(Play.play_unlock.callback(cog, interaction))
    finally:
        cog.cog_unload()

    guild.active_threads.assert_awaited_once()
    modal = interaction.response.modals[0]
    assert modal.title == "Unlock Channel"
    assert [
        (option.label, option.description, option.value)
        for option in modal.thread_select.options
    ] == [
        ("Steins;Gate | @zips", "Last post: 2026-08-03", "30"),
        ("Steins;Gate 0 | @zips", "Last post: 2026-07-02", "32"),
    ]


def test_context_menu_rejects_wrong_channel(temp_db_path, monkeypatch):
    monkeypatch.setattr(play_module.discord, "Member", FakeMember)

    user = FakeMember(id=123, name="zips", display_name="Server Nickname")
    guild = SimpleNamespace(id=1, me=SimpleNamespace(id=999))
    interaction = FakeInteraction(guild, user=user)
    message = FakeMessage(channel=SimpleNamespace())
    cog = Play(SimpleNamespace())
    cog.module_store.enable_module(1, "play")

    try:
        asyncio.run(cog.delete_message_context_menu(interaction, message))
    finally:
        cog.cog_unload()

    interaction.response.send_message.assert_awaited_once_with(
        "This must be used in a forum post/thread.",
        ephemeral=True,
    )
    message.delete.assert_not_awaited()


def test_context_menu_rejects_missing_manage_messages(
    temp_db_path,
    monkeypatch,
):
    monkeypatch.setattr(play_module.discord, "Member", FakeMember)

    tag = SimpleNamespace(id=10, name="Steins;Gate")
    user = FakeMember(id=123, name="zips", display_name="Server Nickname")
    thread = FakeThread(
        name="Steins;Gate | @zips",
        parent_id=20,
        tags=[tag],
        archived=False,
        permissions=SimpleNamespace(manage_messages=False, manage_threads=True),
    )
    guild = SimpleNamespace(id=1, me=SimpleNamespace(id=999))
    interaction = FakeInteraction(guild, user=user)
    message = FakeMessage(channel=thread)
    cog = Play(SimpleNamespace())
    cog.module_store.enable_module(1, "play")
    cog.play_store.save_game(1, "Steins;Gate", 20, 10)

    try:
        asyncio.run(cog.delete_message_context_menu(interaction, message))
    finally:
        cog.cog_unload()

    args, kwargs = interaction.response.sent_messages[0]
    assert "Grant me: **Manage Messages**." in args[0]
    assert kwargs["ephemeral"] is True
    message.delete.assert_not_awaited()


def test_delete_message_rejects_bot_authored_message(temp_db_path, monkeypatch):
    monkeypatch.setattr(play_module.discord, "Member", FakeMember)

    tag = SimpleNamespace(id=10, name="Steins;Gate")
    user = FakeMember(id=123, name="zips", display_name="Server Nickname")
    thread = FakeThread(
        name="Steins;Gate | @zips",
        parent_id=20,
        tags=[tag],
        archived=False,
    )
    guild = SimpleNamespace(id=1, me=SimpleNamespace(id=999))
    interaction = FakeInteraction(guild, user=user)
    message = FakeMessage(
        channel=thread,
        author=FakeMember(id=999, name="amadeus", display_name="Amadeus"),
    )
    cog = Play(SimpleNamespace(user=SimpleNamespace(id=999)))
    cog.module_store.enable_module(1, "play")
    cog.play_store.save_game(1, "Steins;Gate", 20, 10)

    try:
        asyncio.run(cog.delete_message_context_menu(interaction, message))
    finally:
        cog.cog_unload()

    interaction.response.send_message.assert_awaited_once_with(
        "You can not delete bot or moderator messages.",
        ephemeral=True,
        allowed_mentions=play_module.NO_MENTIONS,
    )
    assert interaction.response.modals == []
    message.delete.assert_not_awaited()


def test_delete_message_rejects_admin_role_author(temp_db_path, monkeypatch):
    monkeypatch.setattr(play_module.discord, "Member", FakeMember)

    admin_role = SimpleNamespace(id=50)
    tag = SimpleNamespace(id=10, name="Steins;Gate")
    user = FakeMember(id=123, name="zips", display_name="Server Nickname")
    author = FakeMember(
        id=456,
        name="staff",
        display_name="Staff",
        roles=[admin_role],
    )
    thread = FakeThread(
        name="Steins;Gate | @zips",
        parent_id=20,
        tags=[tag],
        archived=False,
    )
    guild = SimpleNamespace(
        id=1,
        owner_id=999,
        me=SimpleNamespace(id=998),
        get_role=lambda role_id: admin_role if role_id == admin_role.id else None,
    )
    interaction = FakeInteraction(guild, user=user)
    message = FakeMessage(channel=thread, author=author)
    cog = Play(SimpleNamespace(user=SimpleNamespace(id=998)))
    cog.module_store.enable_module(1, "play")
    cog.module_store.set_admin_role(guild, admin_role.id)
    cog.play_store.save_game(1, "Steins;Gate", 20, 10)

    try:
        asyncio.run(cog.delete_message_context_menu(interaction, message))
    finally:
        cog.cog_unload()

    interaction.response.send_message.assert_awaited_once_with(
        "You can not delete bot or moderator messages.",
        ephemeral=True,
        allowed_mentions=play_module.NO_MENTIONS,
    )
    assert interaction.response.modals == []
    message.delete.assert_not_awaited()


def test_delete_confirmation_revalidates_admin_role_author(
    temp_db_path,
    monkeypatch,
):
    monkeypatch.setattr(play_module.discord, "Member", FakeMember)

    admin_role = SimpleNamespace(id=50)
    tag = SimpleNamespace(id=10, name="Steins;Gate")
    user = FakeMember(id=123, name="zips", display_name="Server Nickname")
    author = FakeMember(id=456, name="staff", display_name="Staff", roles=[])
    thread = FakeThread(
        name="Steins;Gate | @zips",
        parent_id=20,
        tags=[tag],
        archived=False,
    )
    guild = SimpleNamespace(
        id=1,
        owner_id=999,
        me=SimpleNamespace(id=998),
        get_role=lambda role_id: admin_role if role_id == admin_role.id else None,
    )
    interaction = FakeInteraction(guild, user=user)
    message = FakeMessage(channel=thread, author=author)
    cog = Play(SimpleNamespace(user=SimpleNamespace(id=998)))
    cog.module_store.enable_module(1, "play")
    cog.module_store.set_admin_role(guild, admin_role.id)
    cog.play_store.save_game(1, "Steins;Gate", 20, 10)

    try:
        asyncio.run(cog.delete_message_context_menu(interaction, message))
        modal = interaction.response.modals[0]
        author.roles = [admin_role]

        confirm_interaction = FakeInteraction(guild, user=user)
        asyncio.run(modal.on_submit(confirm_interaction))
    finally:
        cog.cog_unload()

    message.delete.assert_not_awaited()
    assert confirm_interaction.edits == [
        {
            "content": "You can not delete bot or moderator messages.",
            "view": None,
            "allowed_mentions": play_module.NO_MENTIONS,
        }
    ]


def test_delete_message_confirmation_revalidates_and_deletes(
    temp_db_path,
    monkeypatch,
):
    monkeypatch.setattr(play_module.discord, "Member", FakeMember)

    tag = SimpleNamespace(id=10, name="Steins;Gate")
    user = FakeMember(id=123, name="zips", display_name="Server Nickname")
    thread = FakeThread(
        name="Steins;Gate | @zips",
        parent_id=20,
        tags=[tag],
        archived=False,
    )
    guild = SimpleNamespace(id=1, me=SimpleNamespace(id=999))
    interaction = FakeInteraction(guild, user=user)
    message = FakeMessage(channel=thread, content="Delete me")
    cog = Play(SimpleNamespace())
    cog.module_store.enable_module(1, "play")
    cog.play_store.save_game(1, "Steins;Gate", 20, 10)

    try:
        asyncio.run(cog.delete_message_context_menu(interaction, message))
        modal = interaction.response.modals[0]
        assert modal.title == "Delete Message"
        assert modal.quote.content == "> Delete me"
        assert modal.question.content == "Do you want to delete this message?"

        confirm_interaction = FakeInteraction(guild, user=user)
        asyncio.run(
            modal.on_submit(confirm_interaction)
        )
    finally:
        cog.cog_unload()

    message.delete.assert_awaited_once()
    assert confirm_interaction.edits == [
        {
            "content": "Deleted the message in #thread.",
            "view": None,
        }
    ]


def test_pin_message_confirmation_revalidates_and_pins(
    temp_db_path,
    monkeypatch,
):
    monkeypatch.setattr(play_module.discord, "Member", FakeMember)

    tag = SimpleNamespace(id=10, name="Steins;Gate")
    user = FakeMember(id=123, name="zips", display_name="Server Nickname")
    thread = FakeThread(
        name="Steins;Gate | @zips",
        parent_id=20,
        tags=[tag],
        archived=False,
    )
    guild = SimpleNamespace(id=1, me=SimpleNamespace(id=999))
    interaction = FakeInteraction(guild, user=user)
    message = FakeMessage(channel=thread, content="Pin me")
    cog = Play(SimpleNamespace())
    cog.module_store.enable_module(1, "play")
    cog.play_store.save_game(1, "Steins;Gate", 20, 10)

    try:
        asyncio.run(cog.pin_message_context_menu(interaction, message))
        modal = interaction.response.modals[0]
        assert modal.title == "Pin Message"
        assert modal.quote.content == "> Pin me"
        assert modal.question.content == "Do you want to pin this message?"

        confirm_interaction = FakeInteraction(guild, user=user)
        asyncio.run(
            modal.on_submit(confirm_interaction)
        )
    finally:
        cog.cog_unload()

    message.pin.assert_awaited_once_with(
        reason=f"Playthrough message pinned by {user} ({user.id})",
    )
    assert message.pinned is True
    assert confirm_interaction.edits == [
        {
            "content": "Pinned the message in #thread.",
            "view": None,
        }
    ]


def test_unlock_channel_rejects_already_unlocked(temp_db_path, monkeypatch):
    monkeypatch.setattr(play_module.discord, "Member", FakeMember)

    tag = SimpleNamespace(id=10, name="Steins;Gate")
    user = FakeMember(id=123, name="zips", display_name="Server Nickname")
    thread = FakeThread(
        name="Steins;Gate | @zips",
        parent_id=20,
        tags=[tag],
        archived=False,
        locked=False,
    )
    guild = SimpleNamespace(id=1, me=SimpleNamespace(id=999))
    interaction = FakeInteraction(guild, user=user)
    message = FakeMessage(channel=thread)
    cog = Play(SimpleNamespace())
    cog.module_store.enable_module(1, "play")
    cog.play_store.save_game(1, "Steins;Gate", 20, 10)

    try:
        asyncio.run(cog.unlock_channel_context_menu(interaction, message))
    finally:
        cog.cog_unload()

    interaction.response.send_message.assert_awaited_once_with(
        "That playthrough post is already unlocked.",
        ephemeral=True,
    )


def test_unlock_channel_lists_owned_archived_threads_and_reopens_selection(
    temp_db_path,
    monkeypatch,
):
    monkeypatch.setattr(play_module.discord, "Member", FakeMember)

    tag = SimpleNamespace(id=10, name="Steins;Gate")
    user = FakeMember(id=123, name="zips", display_name="Server Nickname")
    selected_thread = FakeThread(
        name="Steins;Gate | @zips",
        parent_id=20,
        tags=[tag],
        thread_id=30,
        archived=True,
        locked=True,
        archive_timestamp=datetime(2026, 6, 1, tzinfo=timezone.utc),
    )
    other_thread = FakeThread(
        name="Steins;Gate 0 | @zips",
        parent_id=20,
        tags=[tag],
        thread_id=31,
        mention="#other-thread",
        archived=True,
        locked=True,
        archive_timestamp=datetime(2026, 7, 2, tzinfo=timezone.utc),
    )
    other_user_thread = FakeThread(
        name="Steins;Gate | @anna",
        parent_id=20,
        tags=[tag],
        thread_id=32,
        archived=True,
        locked=True,
    )

    class Forum:
        def archived_threads(self, *, limit=None):
            async def iterator():
                yield other_thread
                yield other_user_thread

            return iterator()

    guild = SimpleNamespace(id=1, me=SimpleNamespace(id=999))
    interaction = FakeInteraction(guild, user=user)
    message = FakeMessage(channel=selected_thread, content="Open this")
    cog = Play(SimpleNamespace())
    cog.module_store.enable_module(1, "play")
    cog.play_store.save_game(1, "Steins;Gate", 20, 10)
    cog._get_forum_channel = AsyncMock(return_value=Forum())

    try:
        asyncio.run(cog.unlock_channel_context_menu(interaction, message))
        modal = interaction.response.modals[0]
        assert modal.title == "Unlock Channel"
        select = modal.thread_select
        assert {option.value for option in select.options} == {"30", "31"}
        assert {
            option.value: option.description for option in select.options
        } == {
            "30": "Last post: 2026-06-01",
            "31": "Last post: 2026-07-02",
        }
        select._values = ["31"]

        confirm_interaction = FakeInteraction(guild, user=user)
        asyncio.run(
            modal.on_submit(confirm_interaction)
        )
    finally:
        cog.cog_unload()

    assert selected_thread.edit_calls == []
    assert other_thread.edit_calls == [
        {
            "archived": False,
            "reason": f"Playthrough reopened by {user} ({user.id})",
        },
        {
            "locked": False,
            "reason": f"Playthrough reopened by {user} ({user.id})",
        }
    ]
    assert confirm_interaction.edits == [
        {
            "content": "Unlocked playthrough post: #other-thread",
            "view": None,
        }
    ]


def test_unlock_channel_finishes_active_locked_thread(temp_db_path, monkeypatch):
    monkeypatch.setattr(play_module.discord, "Member", FakeMember)

    tag = SimpleNamespace(id=10, name="Steins;Gate")
    user = FakeMember(id=123, name="zips", display_name="Server Nickname")
    thread = FakeThread(
        name="Steins;Gate | @zips",
        parent_id=20,
        tags=[tag],
        thread_id=30,
        archived=False,
        locked=True,
        last_message_id=discord.utils.time_snowflake(
            datetime(2026, 8, 3, tzinfo=timezone.utc)
        ),
    )

    class Forum:
        def archived_threads(self, *, limit=None):
            async def iterator():
                if False:
                    yield None

            return iterator()

    guild = SimpleNamespace(id=1, me=SimpleNamespace(id=999))
    interaction = FakeInteraction(guild, user=user)
    message = FakeMessage(channel=thread, content="Open this")
    cog = Play(SimpleNamespace())
    cog.module_store.enable_module(1, "play")
    cog.play_store.save_game(1, "Steins;Gate", 20, 10)
    cog._get_forum_channel = AsyncMock(return_value=Forum())

    try:
        asyncio.run(cog.unlock_channel_context_menu(interaction, message))
        modal = interaction.response.modals[0]
        assert modal.title == "Unlock Channel"
        assert [option.value for option in modal.thread_select.options] == ["30"]
        assert modal.thread_select.options[0].description == "Last post: 2026-08-03"
        modal.thread_select._values = ["30"]

        confirm_interaction = FakeInteraction(guild, user=user)
        asyncio.run(
            modal.on_submit(confirm_interaction)
        )
    finally:
        cog.cog_unload()

    assert thread.edit_calls == [
        {
            "locked": False,
            "reason": f"Playthrough reopened by {user} ({user.id})",
        }
    ]
    assert thread.locked is False
    assert thread.archived is False
    assert confirm_interaction.edits == [
        {
            "content": "Unlocked playthrough post: #thread",
            "view": None,
        }
    ]


def test_unlock_submit_treats_already_unlocked_thread_as_success(
    temp_db_path,
    monkeypatch,
):
    monkeypatch.setattr(play_module.discord, "Member", FakeMember)

    tag = SimpleNamespace(id=10, name="Steins;Gate")
    user = FakeMember(id=123, name="zips", display_name="Server Nickname")
    thread = FakeThread(
        name="Steins;Gate | @zips",
        parent_id=20,
        tags=[tag],
        archived=True,
        locked=True,
    )
    guild = SimpleNamespace(id=1, me=SimpleNamespace(id=999))
    interaction = FakeInteraction(guild, user=user)
    cog = Play(SimpleNamespace())
    cog.play_store.save_game(1, "Steins;Gate", 20, 10)

    try:
        thread.archived = False
        thread.locked = False
        asyncio.run(
            cog.unlock_playthrough_thread_from_confirmation(
                interaction,
                thread=thread,
            )
        )
    finally:
        cog.cog_unload()

    assert thread.edit_calls == []
    assert interaction.edits == [
        {
            "content": "Unlocked playthrough post: #thread",
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
