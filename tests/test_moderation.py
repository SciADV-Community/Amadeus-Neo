import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, ANY

from amadeus.moderation import (
    DEFAULT_HONEYPOT_REASON,
    action_ban,
    action_kick,
    action_mute,
    action_remove_role,
    execute_action,
)


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


def test_action_remove_role_requires_configured_role():
    guild = SimpleNamespace(id=1)
    member = SimpleNamespace(id=2)

    result = asyncio.run(action_remove_role(guild, member, None))

    assert result == "Failed — no role configured for remove-role action."


def test_action_remove_role_handles_missing_role():
    guild = SimpleNamespace(id=1, get_role=lambda role_id: None)
    member = SimpleNamespace(id=2)

    result = asyncio.run(action_remove_role(guild, member, 10))

    assert result == "Failed — configured role no longer exists."


def test_action_remove_role_skips_member_without_role():
    role = SimpleNamespace(id=10, name="Errors")
    guild = SimpleNamespace(id=1, get_role=lambda role_id: role)
    member = SimpleNamespace(id=2, roles=[], remove_roles=AsyncMock())

    result = asyncio.run(action_remove_role(guild, member, 10))

    assert result == "Skipped — member does not have **Errors**."
    member.remove_roles.assert_not_awaited()


def test_action_remove_role_removes_role_with_default_reason():
    role = SimpleNamespace(id=10, name="Errors")
    guild = SimpleNamespace(id=1, get_role=lambda role_id: role)
    member = SimpleNamespace(id=2, roles=[role], remove_roles=AsyncMock())

    result = asyncio.run(action_remove_role(guild, member, 10))

    assert result == "Removed role **Errors**."
    member.remove_roles.assert_awaited_once_with(role, reason=DEFAULT_HONEYPOT_REASON)
