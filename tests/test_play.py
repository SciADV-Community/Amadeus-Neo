import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest

import cogs.play as play_module
from cogs.play_admin import MAX_FORUM_TAG_NAME_LENGTH, PlayAdmin, tag_name_error
from cogs.play import (
    MAX_FORUM_THREAD_TAGS,
    Play,
    SPOILER_CHANNEL_FLAG,
    additional_spoiler_tags,
    calculate_spoiler_flags,
    find_forum_tag,
    format_play_thread_name,
    game_name_error,
    mark_thread_spoiler,
    missing_member_play_forum_permissions,
    missing_play_forum_permissions,
    resolve_additional_spoiler_tags,
    starter_message_matches_playthrough,
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
    def __init__(self, guild):
        self.guild = guild
        self.edits = []

    async def edit_original_response(self, **kwargs):
        self.edits.append(kwargs)


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


def test_format_play_thread_name_preserves_shape_and_length():
    member = SimpleNamespace(display_name="Zips")
    assert format_play_thread_name("Steins;Gate Re:Boot", member) == "Steins;Gate Re:Boot | @Zips"

    long_name = "A" * 140
    assert len(format_play_thread_name(long_name, member)) == 100
    assert format_play_thread_name(long_name, member).endswith(" | @Zips")


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
    member = SimpleNamespace(display_name="Zips")
    thread = SimpleNamespace(name="Steins;Gate | @zips")
    assert thread_name_matches_player(thread, member) is True


def test_thread_name_playthrough_match_requires_the_selected_game():
    member = SimpleNamespace(display_name="Zips")
    gate = SimpleNamespace(display_name="Steins;Gate")
    zero = SimpleNamespace(display_name="Steins;Gate 0")
    thread = SimpleNamespace(name="Steins;Gate | @Zips")

    assert thread_name_matches_playthrough(thread, gate, member) is True
    assert thread_name_matches_playthrough(thread, zero, member) is False


@pytest.mark.filterwarnings("ignore:'count' is passed as positional argument:DeprecationWarning")
def test_starter_message_match_survives_nickname_changes_without_prefix_false_positive():
    member = SimpleNamespace(id=123, mention="<@123>", display_name="Renamed")
    gate = SimpleNamespace(display_name="Steins;Gate")
    zero = SimpleNamespace(display_name="Steins;Gate 0")

    assert starter_message_matches_playthrough(
        "<@123> | Spoilers for Steins;Gate",
        gate,
        member,
    ) is True
    assert starter_message_matches_playthrough(
        "<@!123> | Spoilers for Steins;Gate 0, Chaos;Head (replay)",
        zero,
        member,
    ) is True
    assert starter_message_matches_playthrough(
        "<@123> | Spoilers for Steins;Gate 0",
        gate,
        member,
    ) is False


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


class FakeMember:
    def __init__(self, member_id=2, display_name="Zips"):
        self.id = member_id
        self.display_name = display_name
        self.mention = f"<@{member_id}>"
        self.roles = []

    def __str__(self):
        return self.display_name


class FakeResponse:
    def __init__(self):
        self.send_message = AsyncMock()
        self.defer = AsyncMock()


class FakeCommandInteraction:
    def __init__(self, *, guild, user):
        self.guild = guild
        self.guild_id = guild.id if guild else None
        self.user = user
        self.response = FakeResponse()
        self.edits = []

    async def edit_original_response(self, **kwargs):
        self.edits.append(kwargs)
        return SimpleNamespace(id=999)


def all_play_permissions(**overrides):
    values = {
        "view_channel": True,
        "send_messages": True,
        "create_public_threads": True,
        "send_messages_in_threads": True,
        "manage_threads": True,
        "manage_channels": True,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def make_play_cog():
    cog = Play.__new__(Play)
    cog._inflight_creates = set()
    cog.module_store = SimpleNamespace()
    cog.play_store = SimpleNamespace()
    return cog


def test_create_playthrough_thread_handles_guard_paths(monkeypatch):
    monkeypatch.setattr(play_module.discord, "Member", FakeMember)
    member = FakeMember()
    guild = SimpleNamespace(id=1)
    gate = SimpleNamespace(id=10, name="Steins;Gate")
    forum = FakeForum([gate], all_play_permissions(view_channel=False))
    forum.mention = "#play"
    play_game = SimpleNamespace(display_name="Steins;Gate", key="steins-gate")
    cog = make_play_cog()

    interaction = FakeCommandInteraction(guild=None, user=member)
    asyncio.run(
        cog.create_playthrough_thread(
            interaction,
            forum=forum,
            play_game=play_game,
            required_tag=gate,
            selected_tag_ids=[],
            replay=False,
            auto_archive_duration=None,
        )
    )
    assert interaction.edits[-1] == {
        "content": "This can only be used inside a server.",
        "view": None,
    }

    interaction = FakeCommandInteraction(guild=guild, user=member)
    asyncio.run(
        cog.create_playthrough_thread(
            interaction,
            forum=forum,
            play_game=play_game,
            required_tag=gate,
            selected_tag_ids=[],
            replay=False,
            auto_archive_duration=None,
        )
    )
    assert "required permissions" in interaction.edits[-1]["content"]

    forum = FakeForum([gate], all_play_permissions())
    forum.mention = "#play"
    interaction = FakeCommandInteraction(guild=guild, user=member)
    asyncio.run(
        cog.create_playthrough_thread(
            interaction,
            forum=forum,
            play_game=play_game,
            required_tag=gate,
            selected_tag_ids=[gate.id],
            replay=False,
            auto_archive_duration=None,
        )
    )
    assert "applied automatically" in interaction.edits[-1]["content"]


def test_create_playthrough_thread_serializes_duplicate_and_existing_thread(monkeypatch):
    monkeypatch.setattr(play_module.discord, "Member", FakeMember)
    member = FakeMember()
    guild = SimpleNamespace(id=1)
    gate = SimpleNamespace(id=10, name="Steins;Gate")
    forum = FakeForum([gate], all_play_permissions())
    forum.mention = "#play"
    play_game = SimpleNamespace(display_name="Steins;Gate", key="steins-gate")
    cog = make_play_cog()
    cog._find_active_play_thread_or_respond = AsyncMock(
        return_value=(SimpleNamespace(mention="#existing"), True)
    )
    cog._create_playthrough_thread_unlocked = AsyncMock()

    cog._inflight_creates.add((1, 2, "steins-gate"))
    interaction = FakeCommandInteraction(guild=guild, user=member)
    asyncio.run(
        cog.create_playthrough_thread(
            interaction,
            forum=forum,
            play_game=play_game,
            required_tag=gate,
            selected_tag_ids=[],
            replay=False,
            auto_archive_duration=None,
        )
    )
    assert "already being created" in interaction.edits[-1]["content"]

    cog._inflight_creates.clear()
    interaction = FakeCommandInteraction(guild=guild, user=member)
    asyncio.run(
        cog.create_playthrough_thread(
            interaction,
            forum=forum,
            play_game=play_game,
            required_tag=gate,
            selected_tag_ids=[],
            replay=False,
            auto_archive_duration=None,
        )
    )
    assert "#existing" in interaction.edits[-1]["content"]
    assert cog._inflight_creates == set()
    cog._create_playthrough_thread_unlocked.assert_not_awaited()


def test_create_playthrough_thread_unlocked_creates_spoiler_thread(monkeypatch):
    monkeypatch.setattr(play_module, "mark_thread_spoiler", AsyncMock())
    member = FakeMember()
    guild = SimpleNamespace(id=1)
    interaction = FakeCommandInteraction(guild=guild, user=member)
    thread = SimpleNamespace(id=123, mention="#thread", send=AsyncMock())
    forum = SimpleNamespace(
        mention="#play",
        create_thread=AsyncMock(return_value=SimpleNamespace(thread=thread)),
    )
    required_tag = SimpleNamespace(id=10, name="Steins;Gate")
    extra_tag = SimpleNamespace(id=20, name="Chaos;Head")
    play_game = SimpleNamespace(display_name="Steins;Gate", key="steins-gate")
    cog = make_play_cog()

    asyncio.run(
        cog._create_playthrough_thread_unlocked(
            interaction,
            forum=forum,
            play_game=play_game,
            required_tag=required_tag,
            extra_tags=[extra_tag],
            replay=True,
            auto_archive_duration=1440,
        )
    )

    kwargs = forum.create_thread.await_args.kwargs
    assert kwargs["name"] == "Steins;Gate | @Zips"
    assert "Spoilers for Steins;Gate, Chaos;Head (replay)" in kwargs["content"]
    assert kwargs["applied_tags"] == [required_tag, extra_tag]
    play_module.mark_thread_spoiler.assert_awaited_once()
    thread.send.assert_awaited_once()
    assert interaction.edits[-1]["content"] == "Created your **Steins;Gate** playthrough post: #thread"


def test_play_command_validates_config_and_reaches_tag_picker(monkeypatch):
    monkeypatch.setattr(play_module.discord, "Member", FakeMember)
    module_enabled = AsyncMock(return_value=True)
    monkeypatch.setattr(play_module, "require_module_enabled_for_interaction", module_enabled)

    member = FakeMember()
    bot_member = SimpleNamespace(id=99)
    guild = SimpleNamespace(id=1, me=bot_member)
    play_game = SimpleNamespace(
        display_name="Steins;Gate",
        key="steins-gate",
        forum_channel_id=None,
        forum_tag_id=10,
    )
    config = SimpleNamespace(forum_channel_id=50, auto_archive_duration=1440)
    tag = SimpleNamespace(id=10, name="Steins;Gate")
    forum = FakeForum([tag], all_play_permissions())
    forum.mention = "#play"
    cog = make_play_cog()
    cog.play_store = SimpleNamespace(
        get_config=lambda guild_id: config,
        get_game=lambda guild_id, game: play_game if game == "steins-gate" else None,
    )
    cog._get_forum_channel = AsyncMock(return_value=forum)
    cog._find_active_play_thread_or_respond = AsyncMock(return_value=(None, True))

    missing_game = FakeCommandInteraction(guild=guild, user=member)
    asyncio.run(Play.play.callback(cog, missing_game, "missing", False))
    missing_game.response.send_message.assert_awaited_once_with(
        "That game is not configured for playthroughs on this server.",
        ephemeral=True,
    )

    forum_missing = FakeCommandInteraction(guild=guild, user=member)
    cog._get_forum_channel = AsyncMock(return_value=None)
    asyncio.run(Play.play.callback(cog, forum_missing, "steins-gate", False))
    assert "valid playthrough forum" in forum_missing.response.send_message.await_args.args[0]

    tag_missing = FakeCommandInteraction(guild=guild, user=member)
    empty_forum = FakeForum([], all_play_permissions())
    empty_forum.mention = "#play"
    cog._get_forum_channel = AsyncMock(return_value=empty_forum)
    asyncio.run(Play.play.callback(cog, tag_missing, "steins-gate", False))
    assert "missing its forum tag" in tag_missing.response.send_message.await_args.args[0]

    success = FakeCommandInteraction(guild=guild, user=member)
    cog._get_forum_channel = AsyncMock(return_value=forum)
    asyncio.run(Play.play.callback(cog, success, "steins-gate", True))
    success.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
    assert "additional spoilers" in success.edits[-1]["content"]
    assert success.edits[-1]["view"] is not None
