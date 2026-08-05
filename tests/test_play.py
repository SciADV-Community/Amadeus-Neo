import asyncio
from types import SimpleNamespace

from cogs.play_admin import MAX_FORUM_TAG_NAME_LENGTH, tag_name_error
from cogs.play import (
    SPOILER_CHANNEL_FLAG,
    calculate_spoiler_flags,
    find_forum_tag,
    format_play_thread_name,
    game_name_error,
    mark_thread_spoiler,
    missing_play_forum_permissions,
    thread_has_tag,
    thread_name_matches_player,
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


def test_game_name_validation_rejects_empty_long_control_and_mentions():
    assert game_name_error("Steins;Gate") is None
    assert game_name_error(" ") == "Game name cannot be empty."
    assert game_name_error("x" * 81) == "Game name must be 80 characters or fewer."
    assert game_name_error("Steins\x00Gate") == "Game name cannot contain control or bidirectional formatting characters."
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


def test_thread_name_player_match_is_case_insensitive():
    member = SimpleNamespace(display_name="Zips")
    thread = SimpleNamespace(name="Steins;Gate | @zips")
    assert thread_name_matches_player(thread, member) is True


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
        "Create Public Threads",
        "Manage Threads",
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
