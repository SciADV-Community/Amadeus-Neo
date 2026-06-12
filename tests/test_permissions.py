import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import amadeus.permissions as permissions_module
from amadeus.models import GuildConfig
from amadeus.module_guard import require_module_enabled_for_interaction
from amadeus.permissions import get_admin_role, require_amadeus_access, user_has_amadeus_access


class FakeRole:
    def __init__(self, role_id):
        self.id = role_id


class FakeMember:
    def __init__(self, member_id, roles):
        self.id = member_id
        self.roles = roles


def test_user_has_amadeus_access_allows_owner():
    config = GuildConfig(guild_id=1, owner_id=2, admin_role_id=None)
    member = SimpleNamespace(id=2, roles=[])
    guild = SimpleNamespace(id=1, get_role=lambda role_id: None)

    assert user_has_amadeus_access(member, guild, config)


def test_user_has_amadeus_access_allows_configured_admin_role():
    admin_role = FakeRole(10)
    config = GuildConfig(guild_id=1, owner_id=2, admin_role_id=10)
    member = SimpleNamespace(id=3, roles=[admin_role])
    guild = SimpleNamespace(id=1, get_role=lambda role_id: admin_role)

    assert get_admin_role(guild, config) is admin_role
    assert user_has_amadeus_access(member, guild, config)


def test_require_amadeus_access_denies_when_user_lacks_admin_role(monkeypatch):
    admin_role = FakeRole(10)
    config = GuildConfig(guild_id=1, owner_id=2, admin_role_id=10)
    guild = SimpleNamespace(id=1, owner_id=2, get_role=lambda role_id: admin_role)
    user = FakeMember(3, [])
    monkeypatch.setattr(permissions_module.discord, "Member", FakeMember)
    interaction = SimpleNamespace(
        guild=guild,
        user=user,
        response=SimpleNamespace(send_message=AsyncMock()),
    )
    store = SimpleNamespace(ensure_guild_config=lambda guild: config)

    result = asyncio.run(require_amadeus_access(interaction, store))

    assert result is None
    interaction.response.send_message.assert_awaited_once_with(
        "You need the configured Amadeus admin role to use this command.",
        ephemeral=True,
    )


def test_require_module_enabled_for_interaction_responds_when_disabled():
    interaction = SimpleNamespace(
        guild_id=1,
        response=SimpleNamespace(send_message=AsyncMock()),
    )
    store = SimpleNamespace(is_module_enabled=lambda guild_id, module_name: False)

    result = asyncio.run(require_module_enabled_for_interaction(interaction, store, "honeypot"))

    assert result is False
    interaction.response.send_message.assert_awaited_once_with(
        "The **honeypot** module is not enabled on this server.\n"
        "Enable it first with `/amadeus module enable honeypot`.",
        ephemeral=True,
    )


def test_require_module_enabled_for_interaction_allows_guildless_interaction():
    interaction = SimpleNamespace(guild_id=None)
    store = SimpleNamespace(is_module_enabled=lambda guild_id, module_name: False)

    assert asyncio.run(require_module_enabled_for_interaction(interaction, store, "honeypot"))
