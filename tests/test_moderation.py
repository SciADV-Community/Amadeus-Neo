import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, ANY

from amadeus.moderation import DEFAULT_HONEYPOT_REASON, action_ban, action_kick, action_mute, execute_action


def test_action_kick_uses_custom_audit_log_reason():
    member = SimpleNamespace(kick=AsyncMock(), guild=SimpleNamespace(id=1), id=2)

    result = asyncio.run(action_kick(member, reason="Custom kick reason"))

    assert result == "Kicked."
    member.kick.assert_awaited_once_with(reason="Custom kick reason")


def test_action_ban_uses_default_audit_log_reason_when_reason_is_missing():
    member = SimpleNamespace(ban=AsyncMock(), guild=SimpleNamespace(id=1), id=2)

    result = asyncio.run(action_ban(member))

    assert result == "Banned."
    member.ban.assert_awaited_once_with(
        reason=DEFAULT_HONEYPOT_REASON,
        delete_message_days=0,
    )


def test_action_mute_uses_custom_audit_log_reason():
    member = SimpleNamespace(timeout=AsyncMock(), guild=SimpleNamespace(id=1), id=2)

    result = asyncio.run(action_mute(member, reason="Custom mute reason"))

    assert result == "Timed out for 28 days."
    member.timeout.assert_awaited_once_with(ANY, reason="Custom mute reason")


def test_execute_action_passes_reason_to_moderation_action():
    member = SimpleNamespace(kick=AsyncMock(), guild=SimpleNamespace(id=1), id=2)

    result = asyncio.run(
        execute_action(
            guild=SimpleNamespace(id=1),
            member=member,
            action="kick",
            action_reason="Configured reason",
        )
    )

    assert result == "Kicked."
    member.kick.assert_awaited_once_with(reason="Configured reason")
