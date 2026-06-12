import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import cogs.honeypot as honeypot_module
from cogs.honeypot import Honeypot


class FakeMember:
    def __init__(self, member_id=2):
        self.id = member_id
        self.mention = f"<@{member_id}>"

    def __str__(self):
        return f"member-{self.id}"


class FakeModuleStore:
    def __init__(self, enabled=True):
        self.enabled = enabled

    def is_module_enabled(self, guild_id, module_name):
        return self.enabled


class FakeHoneypotStore:
    def __init__(self, config):
        self.config = config

    def get_config(self, guild_id):
        return self.config


def make_cog(config=None, *, enabled=True):
    cog = Honeypot.__new__(Honeypot)
    cog.bot = SimpleNamespace(user=SimpleNamespace(id=999))
    cog.module_store = FakeModuleStore(enabled=enabled)
    cog.honeypot_store = FakeHoneypotStore(config)
    return cog


def make_message(*, guild_id=1, channel_id=10, author=None, content="trap"):
    guild = SimpleNamespace(id=guild_id)
    return SimpleNamespace(
        guild=guild,
        channel=SimpleNamespace(id=channel_id),
        author=author or FakeMember(2),
        content=content,
        delete=AsyncMock(),
    )


def test_honeypot_ignores_dm_messages():
    cog = make_cog()
    message = make_message()
    message.guild = None

    asyncio.run(cog.on_message(message))

    message.delete.assert_not_awaited()


def test_honeypot_ignores_own_bot_messages():
    config = SimpleNamespace(channel_id=10)
    cog = make_cog(config)
    message = make_message(author=SimpleNamespace(id=999))

    asyncio.run(cog.on_message(message))

    message.delete.assert_not_awaited()


def test_honeypot_ignores_disabled_module(monkeypatch):
    execute_action = AsyncMock()
    monkeypatch.setattr(honeypot_module, "execute_action", execute_action)
    cog = make_cog(enabled=False)
    message = make_message()

    asyncio.run(cog.on_message(message))

    message.delete.assert_not_awaited()
    execute_action.assert_not_awaited()


def test_honeypot_ignores_non_honeypot_channel(monkeypatch):
    execute_action = AsyncMock()
    monkeypatch.setattr(honeypot_module, "execute_action", execute_action)
    config = SimpleNamespace(channel_id=10)
    cog = make_cog(config)
    message = make_message(channel_id=11)

    asyncio.run(cog.on_message(message))

    message.delete.assert_not_awaited()
    execute_action.assert_not_awaited()


def test_honeypot_deletes_message_before_action_and_passes_reason(monkeypatch):
    monkeypatch.setattr(honeypot_module.discord, "Member", FakeMember)
    events = []

    async def delete():
        events.append("delete")

    async def execute_action(guild, member, action, action_role_id, action_reason):
        events.append("action")
        assert guild.id == 1
        assert member.id == 2
        assert action == "ban"
        assert action_role_id is None
        assert action_reason == "Configured audit reason"
        return "Banned."

    monkeypatch.setattr(honeypot_module, "execute_action", execute_action)
    monkeypatch.setattr(honeypot_module, "send_alert", AsyncMock())
    config = SimpleNamespace(
        channel_id=10,
        action="ban",
        action_role_id=None,
        action_reason="Configured audit reason",
        alerts_enabled=False,
    )
    cog = make_cog(config)
    message = make_message()
    message.delete = delete

    asyncio.run(cog.on_message(message))

    assert events == ["delete", "action"]


def test_honeypot_sends_alert_when_enabled(monkeypatch):
    monkeypatch.setattr(honeypot_module.discord, "Member", FakeMember)
    send_alert = AsyncMock()
    monkeypatch.setattr(honeypot_module, "send_alert", send_alert)
    monkeypatch.setattr(honeypot_module, "execute_action", AsyncMock(return_value="Kicked."))
    config = SimpleNamespace(
        channel_id=10,
        action="kick",
        action_role_id=None,
        action_reason="Reason",
        alerts_enabled=True,
    )
    cog = make_cog(config)
    message = make_message(content="@everyone **bad**")

    asyncio.run(cog.on_message(message))

    send_alert.assert_awaited_once()
    alert_text = send_alert.await_args.args[3]
    assert "**Action:** Kick" in alert_text
    assert "**Result:** Kicked." in alert_text
    assert "@everyone" not in alert_text


def test_honeypot_alerts_when_delete_forbidden(monkeypatch):
    class FakeForbidden(Exception):
        pass

    monkeypatch.setattr(honeypot_module.discord, "Forbidden", FakeForbidden)
    monkeypatch.setattr(honeypot_module.discord, "Member", FakeMember)
    send_alert = AsyncMock()
    monkeypatch.setattr(honeypot_module, "send_alert", send_alert)
    monkeypatch.setattr(honeypot_module, "execute_action", AsyncMock(return_value="Timed out for 28 days."))
    config = SimpleNamespace(
        channel_id=10,
        action="mute",
        action_role_id=None,
        action_reason=None,
        alerts_enabled=False,
    )
    cog = make_cog(config)
    message = make_message()
    message.delete = AsyncMock(side_effect=FakeForbidden())

    asyncio.run(cog.on_message(message))

    send_alert.assert_awaited_once()
    alert_text = send_alert.await_args.args[3]
    assert "Missing **Manage Messages** permission" in alert_text
