import asyncio
from types import SimpleNamespace

import pytest

from cogs.honeypot_admin import (
    HONEYPOT_POST_MESSAGE_MAX_LENGTH,
    delete_history_label,
    find_existing_honeypot_post,
    honeypot_action_label,
    honeypot_alerts_hint,
    honeypot_post_message_error,
)


@pytest.mark.parametrize(
    ("action", "label"),
    [
        ("mute", "Mute (28-day timeout)"),
        ("kick", "Kick"),
        ("ban", "Ban"),
    ],
)
def test_honeypot_action_label_does_not_require_role_for_moderation_actions(action, label):
    assert honeypot_action_label(action) == label


def test_honeypot_action_label_includes_role_for_remove_role():
    role = SimpleNamespace(mention="<@&123>")

    assert honeypot_action_label("remove_role", role) == "Remove role (<@&123>)"


def test_honeypot_action_label_rejects_remove_role_without_role():
    with pytest.raises(ValueError, match="remove_role requires a role"):
        honeypot_action_label("remove_role")


def test_honeypot_alerts_hint_warns_when_enabling_without_admin_channel():
    assert (
        honeypot_alerts_hint(True, None)
        == "\n\nMake sure `/amadeus set-admin-channel` is configured so alerts have somewhere to go."
    )


def test_honeypot_alerts_hint_is_empty_when_admin_channel_is_configured():
    assert honeypot_alerts_hint(True, 123) == ""


def test_honeypot_alerts_hint_is_empty_when_disabling_alerts():
    assert honeypot_alerts_hint(False, None) == ""


@pytest.mark.parametrize(
    ("seconds", "label"),
    [
        (3600, "1 hour"),
        (21600, "6 hours"),
        (43200, "12 hours"),
        (86400, "24 hours"),
    ],
)
def test_delete_history_label_formats_supported_windows(seconds, label):
    assert delete_history_label(seconds) == label


def test_honeypot_post_message_error_accepts_plain_message():
    assert honeypot_post_message_error("Read this before posting.") is None


@pytest.mark.parametrize("message", ["", "   "])
def test_honeypot_post_message_error_rejects_empty_message(message):
    assert honeypot_post_message_error(message) == "Honeypot message cannot be empty."


def test_honeypot_post_message_error_rejects_too_long_message():
    message = "x" * (HONEYPOT_POST_MESSAGE_MAX_LENGTH + 1)

    assert (
        honeypot_post_message_error(message)
        == f"Honeypot message must be {HONEYPOT_POST_MESSAGE_MAX_LENGTH} characters or fewer."
    )


@pytest.mark.parametrize(
    "message",
    [
        "Visit https://example.com",
        "Visit http://example.com",
        "Visit www.example.com",
        "Visit example.com",
        "Join discord.gg/example",
    ],
)
def test_honeypot_post_message_error_rejects_urls(message):
    assert honeypot_post_message_error(message) == "Honeypot message cannot contain URLs."


class FakeHistory:
    def __init__(self, messages):
        self.messages = messages

    def __aiter__(self):
        return self._iter()

    async def _iter(self):
        for message in self.messages:
            yield message


class FakeChannel:
    def __init__(self, *, fetch_message_result=None, history_messages=None):
        self.fetch_message_result = fetch_message_result
        self.history_messages = history_messages or []
        self.history_limit = None
        self.history_oldest_first = None

    async def fetch_message(self, message_id):
        return self.fetch_message_result

    def history(self, *, limit, oldest_first):
        self.history_limit = limit
        self.history_oldest_first = oldest_first
        return FakeHistory(self.history_messages)


def test_find_existing_honeypot_post_uses_stored_bot_message_id():
    bot_message = SimpleNamespace(author=SimpleNamespace(id=10))
    channel = FakeChannel(fetch_message_result=bot_message)

    found = asyncio.run(find_existing_honeypot_post(channel, 10, 123))

    assert found is bot_message
    assert channel.history_limit is None


def test_find_existing_honeypot_post_falls_back_to_recent_bot_message():
    user_message = SimpleNamespace(author=SimpleNamespace(id=99))
    bot_message = SimpleNamespace(author=SimpleNamespace(id=10))
    channel = FakeChannel(
        fetch_message_result=SimpleNamespace(author=SimpleNamespace(id=77)),
        history_messages=[user_message, bot_message],
    )

    found = asyncio.run(find_existing_honeypot_post(channel, 10, 123))

    assert found is bot_message
    assert channel.history_limit == 50
    assert channel.history_oldest_first is True
