import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import cogs.boost_admin as boost_admin_module
from amadeus.models.boost import BoostGrant
from amadeus.models.dm_flow import DmFlow
from cogs.boost import FLOW_TYPE, S
from cogs.boost_admin import BoostAdmin


class FakeResponse:
    def __init__(self):
        self.send_message = AsyncMock()


class FakeMember:
    def __init__(self, member_id=2, *, boosting=True, roles=None):
        self.id = member_id
        self.mention = f"<@{member_id}>"
        self.roles = roles or []
        self.premium_since = object() if boosting else None

    def __str__(self):
        return f"member-{self.id}"


class FakeFlowStore:
    def __init__(self, flow=None):
        self.flow = flow
        self.deleted = []

    def get(self, guild_id, user_id, flow_type):
        return self.flow

    def delete(self, guild_id, user_id, flow_type):
        self.deleted.append((guild_id, user_id, flow_type))
        self.flow = None


class FakeBoostStore:
    def __init__(self, grant=None, subscription_count=1):
        self.grant = grant
        self.subscription_count = subscription_count
        self.deleted = []

    def get_grant(self, guild_id, user_id):
        return self.grant

    def delete_grant(self, guild_id, user_id):
        self.deleted.append((guild_id, user_id))
        self.grant = None

    def get_subscription_count(self, guild_id):
        return self.subscription_count


def make_guild(*, boost_cog=None):
    role = SimpleNamespace(id=10, mention="<@&10>")
    emoji_1 = SimpleNamespace(id=20, __str__=lambda self: ":okabe:")
    emoji_2 = SimpleNamespace(id=30, __str__=lambda self: ":kurisu:")
    return SimpleNamespace(
        id=1,
        emojis=[emoji_1, emoji_2],
        premium_subscription_count=2,
        get_role=lambda role_id: role if role_id == 10 else None,
        get_cog=lambda name: boost_cog,
    )


def make_admin(*, flow=None, grant=None, boost_cog=None, module_enabled=True):
    bot = SimpleNamespace(get_cog=lambda name: boost_cog)
    cog = BoostAdmin.__new__(BoostAdmin)
    cog.bot = bot
    cog.flow_store = FakeFlowStore(flow)
    cog.boost_store = FakeBoostStore(grant)
    cog.module_store = SimpleNamespace(
        is_module_enabled=lambda guild_id, module: module_enabled
    )
    return cog


def make_interaction(*, guild=None, user=None):
    return SimpleNamespace(
        guild=guild,
        user=user if user is not None else FakeMember(),
        response=FakeResponse(),
    )


def test_boost_status_rejects_invalid_context_and_disabled_module(monkeypatch):
    monkeypatch.setattr(boost_admin_module.discord, "Member", FakeMember)
    cog = make_admin()

    interaction = make_interaction(guild=None)
    asyncio.run(BoostAdmin.boost_status.callback(cog, interaction))
    interaction.response.send_message.assert_awaited_once_with(
        "This can only be used inside a server.",
        ephemeral=True,
    )

    interaction = make_interaction(guild=make_guild())
    cog = make_admin(module_enabled=False)
    asyncio.run(BoostAdmin.boost_status.callback(cog, interaction))
    interaction.response.send_message.assert_awaited_once_with(
        "The **boost** module is not enabled on this server.",
        ephemeral=True,
    )


def test_boost_status_reports_grant_flow_and_restarts_denied_flow(monkeypatch):
    monkeypatch.setattr(boost_admin_module.discord, "Member", FakeMember)
    grant = BoostGrant(1, 2, 2, role_id=10, emoji_1_id=20, emoji_2_id=30)
    guild = make_guild()
    cog = make_admin(grant=grant)
    interaction = make_interaction(guild=guild, user=FakeMember())

    asyncio.run(BoostAdmin.boost_status.callback(cog, interaction))

    embed = interaction.response.send_message.await_args.kwargs["embed"]
    assert embed.description == "✅ Your perks are active."
    assert {field.name for field in embed.fields} >= {"Tier", "Role"}

    flow = DmFlow(1, 2, FLOW_TYPE, S.DENIED, {"tier": 1})
    boost_cog = SimpleNamespace(_reset_flow=AsyncMock(return_value=flow), _send_prompt=AsyncMock())
    guild = make_guild(boost_cog=boost_cog)
    cog = make_admin(flow=flow, boost_cog=boost_cog)
    interaction = make_interaction(guild=guild, user=FakeMember())

    asyncio.run(BoostAdmin.boost_status.callback(cog, interaction))

    boost_cog._reset_flow.assert_awaited_once_with(1, 2, 1, forced=False)
    boost_cog._send_prompt.assert_awaited_once_with(interaction.user, flow)


def test_boost_admin_start_validates_count_booster_and_loaded_cog(monkeypatch):
    monkeypatch.setattr(
        boost_admin_module,
        "require_amadeus_access",
        AsyncMock(return_value=SimpleNamespace()),
    )
    cog = make_admin()
    interaction = make_interaction(guild=make_guild())

    asyncio.run(BoostAdmin.boost_admin_start.callback(cog, interaction, FakeMember(), False, 3))
    assert "`count` must be" in interaction.response.send_message.await_args.args[0]

    interaction = make_interaction(guild=make_guild())
    asyncio.run(BoostAdmin.boost_admin_start.callback(cog, interaction, FakeMember(boosting=False), False, None))
    assert "not currently boosting" in interaction.response.send_message.await_args.args[0]

    interaction = make_interaction(guild=make_guild())
    asyncio.run(BoostAdmin.boost_admin_start.callback(cog, interaction, FakeMember(), False, None))
    assert interaction.response.send_message.await_args.args[0] == "Boost cog is not loaded."


def test_boost_admin_start_deletes_stale_flow_and_starts_loaded_cog(monkeypatch):
    monkeypatch.setattr(
        boost_admin_module,
        "require_amadeus_access",
        AsyncMock(return_value=SimpleNamespace()),
    )
    monkeypatch.setattr(boost_admin_module, "get_proposed_tier", lambda cached, current: 2)
    boost_cog = SimpleNamespace(_start_flow=AsyncMock())
    cog = make_admin(boost_cog=boost_cog)
    interaction = make_interaction(guild=make_guild())
    member = FakeMember()

    asyncio.run(BoostAdmin.boost_admin_start.callback(cog, interaction, member, False, None))

    assert cog.flow_store.deleted == [(1, 2, FLOW_TYPE)]
    boost_cog._start_flow.assert_awaited_once_with(interaction.guild, member, 2, forced=False)
    assert "Started the boost perks flow" in interaction.response.send_message.await_args.args[0]


def test_boost_admin_remove_handles_missing_grant_teardown_and_missing_boost_cog(monkeypatch):
    monkeypatch.setattr(
        boost_admin_module,
        "require_amadeus_access",
        AsyncMock(return_value=SimpleNamespace()),
    )
    guild = make_guild()
    member = FakeMember()

    cog = make_admin()
    interaction = make_interaction(guild=guild)
    asyncio.run(BoostAdmin.boost_admin_remove.callback(cog, interaction, member))
    assert "no active boost grant" in interaction.response.send_message.await_args.args[0]

    grant = BoostGrant(1, 2, 1, role_id=10)
    boost_cog = SimpleNamespace(_teardown_grant=AsyncMock())
    cog = make_admin(grant=grant, boost_cog=boost_cog)
    interaction = make_interaction(guild=guild)
    asyncio.run(BoostAdmin.boost_admin_remove.callback(cog, interaction, member))
    boost_cog._teardown_grant.assert_awaited_once_with(guild, grant)

    grant = BoostGrant(1, 2, 1, role_id=10)
    cog = make_admin(grant=grant, boost_cog=None)
    interaction = make_interaction(guild=guild)
    asyncio.run(BoostAdmin.boost_admin_remove.callback(cog, interaction, member))
    assert cog.boost_store.deleted == [(1, 2)]


def test_boost_admin_status_summarizes_flow_and_grant(monkeypatch):
    monkeypatch.setattr(
        boost_admin_module,
        "require_amadeus_access",
        AsyncMock(return_value=SimpleNamespace()),
    )
    flow = DmFlow(1, 2, FLOW_TYPE, S.PENDING, {"tier": 2})
    grant = BoostGrant(1, 2, 2, role_id=10, emoji_1_id=20)
    cog = make_admin(flow=flow, grant=grant)
    interaction = make_interaction(guild=make_guild())

    asyncio.run(BoostAdmin.boost_admin_status.callback(cog, interaction, FakeMember()))

    embed = interaction.response.send_message.await_args.kwargs["embed"]
    fields = {field.name: field.value for field in embed.fields}
    assert fields["Grant Tier"] == "2"
    assert fields["Flow State"] == S.PENDING
