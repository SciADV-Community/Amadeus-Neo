import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord

from cogs import debug


class FakeAsyncUsers:
    def __init__(self, users):
        self.users = users

    def __aiter__(self):
        return self._iter()

    async def _iter(self):
        for user in self.users:
            yield user


class FakeReaction:
    def __init__(self, emoji, *, normal_users=None, burst_users=None):
        self.emoji = emoji
        self.normal_users = normal_users or []
        self.burst_users = burst_users or []
        self.burst_count = len(self.burst_users)
        self.user_calls = []

    def users(self, *, limit=None, type=None):
        self.user_calls.append((limit, type))
        if type == discord.ReactionType.burst:
            return FakeAsyncUsers(self.burst_users)
        return FakeAsyncUsers(self.normal_users)


class FakeChannel:
    def __init__(self, message):
        self.message = message
        self.fetched_message_id = None

    async def fetch_message(self, message_id):
        self.fetched_message_id = message_id
        return self.message


class FakeResponse:
    def __init__(self):
        self.send_message = AsyncMock()
        self.defer = AsyncMock()


class FakeFollowup:
    def __init__(self):
        self.send = AsyncMock()


def make_interaction(channel):
    return SimpleNamespace(
        channel=channel,
        response=FakeResponse(),
        followup=FakeFollowup(),
    )


def test_parse_message_id_accepts_ids_and_message_links():
    assert debug.parse_message_id("12345678901234567") == 12345678901234567
    assert (
        debug.parse_message_id(
            "https://discord.com/channels/1/2/12345678901234567"
        )
        == 12345678901234567
    )


def test_reaction_matches_custom_emoji_by_mention_name_or_id():
    emoji = SimpleNamespace(
        id=123456789012345678,
        name="blob",
        __str__=lambda self: "<:blob:123456789012345678>",
    )

    assert debug.reaction_matches_emoji(emoji, "<:blob:123456789012345678>")
    assert debug.reaction_matches_emoji(emoji, ":blob:")
    assert debug.reaction_matches_emoji(emoji, "blob")


def test_fetch_reaction_users_includes_burst_and_deduplicates():
    normal = SimpleNamespace(id=1, name="normal")
    duplicate = SimpleNamespace(id=2, name="duplicate")
    burst_duplicate = SimpleNamespace(id=2, name="duplicate")
    burst = SimpleNamespace(id=3, name="burst")
    reaction = FakeReaction(
        "\U0001f525",
        normal_users=[normal, duplicate],
        burst_users=[burst_duplicate, burst],
    )

    users = asyncio.run(debug.fetch_reaction_users(reaction))

    assert users == [normal, duplicate, burst]
    assert reaction.user_calls == [
        (None, discord.ReactionType.normal),
        (None, discord.ReactionType.burst),
    ]


def test_cmd_reacts_sends_usernames_not_display_names():
    message_id = 12345678901234567
    users = [
        SimpleNamespace(id=2, name="z_username", display_name="Z Nick"),
        SimpleNamespace(id=1, name="a_username", display_name="A Nick"),
    ]
    reaction = FakeReaction("\U0001f44d", normal_users=users)
    message = SimpleNamespace(reactions=[reaction])
    channel = FakeChannel(message)
    interaction = make_interaction(channel)

    asyncio.run(debug.cmd_reacts(interaction, str(message_id), "\U0001f44d"))

    interaction.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
    assert channel.fetched_message_id == message_id
    sent = interaction.followup.send.await_args.args[0]
    assert "a_username" in sent
    assert "z_username" in sent
    assert "A Nick" not in sent
    assert "Z Nick" not in sent
    assert interaction.followup.send.await_args.kwargs["ephemeral"] is True


def test_cmd_reacts_sends_multiple_ephemeral_pages_when_needed(monkeypatch):
    monkeypatch.setattr(debug, "REACTOR_PAGE_BODY_LIMIT", 12)
    message_id = 12345678901234567
    users = [
        SimpleNamespace(id=1, name="user_01"),
        SimpleNamespace(id=2, name="user_02"),
        SimpleNamespace(id=3, name="user_03"),
    ]
    reaction = FakeReaction("\u2705", normal_users=users)
    interaction = make_interaction(FakeChannel(SimpleNamespace(reactions=[reaction])))

    asyncio.run(debug.cmd_reacts(interaction, str(message_id), "\u2705"))

    assert interaction.followup.send.await_count == 3
    for call in interaction.followup.send.await_args_list:
        assert call.kwargs["ephemeral"] is True
