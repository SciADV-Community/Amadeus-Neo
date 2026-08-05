import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import cogs.activity as activity_module
from cogs.activity import Activity


class FakeRole:
    def __init__(self, role_id):
        self.id = role_id
        self.mention = f"<@&{role_id}>"


def test_apply_role_swap_adds_highest_role_and_removes_old_tier_roles():
    old_role = FakeRole(10)
    new_role = FakeRole(20)
    unrelated_role = FakeRole(30)
    guild = SimpleNamespace(
        id=1,
        get_role=lambda role_id: {10: old_role, 20: new_role}.get(role_id),
    )
    member = SimpleNamespace(
        id=2,
        roles=[old_role, unrelated_role],
        add_roles=AsyncMock(),
        remove_roles=AsyncMock(),
    )
    cog = Activity.__new__(Activity)
    cog.bot = SimpleNamespace()
    cog.module_store = SimpleNamespace()
    cog._role_alert_sent_at = {}

    asyncio.run(cog._apply_role_swap(guild, member, [(50, 10), (100, 20)], count=100))

    member.add_roles.assert_awaited_once_with(
        new_role,
        reason="Activity milestone: 100 messages",
    )
    member.remove_roles.assert_awaited_once_with(
        old_role,
        reason="Activity role swap: 100 messages",
    )


def test_apply_role_swap_does_nothing_before_first_tier():
    role = FakeRole(10)
    guild = SimpleNamespace(id=1, get_role=lambda role_id: role)
    member = SimpleNamespace(
        id=2,
        roles=[],
        add_roles=AsyncMock(),
        remove_roles=AsyncMock(),
    )
    cog = Activity.__new__(Activity)
    cog._role_alert_sent_at = {}

    asyncio.run(cog._apply_role_swap(guild, member, [(50, 10)], count=49))

    member.add_roles.assert_not_awaited()
    member.remove_roles.assert_not_awaited()


class FakeActivityStore:
    def __init__(self):
        self.count = 0
        self.cooldown = 5
        self.role_swap_enabled = False
        self.includes = set()
        self.excludes = set()
        self.tiers = []

    def get_cooldown(self, guild_id):
        return self.cooldown

    def get_role_swap_enabled(self, guild_id):
        return self.role_swap_enabled

    def get_channels(self, guild_id):
        return self.includes, self.excludes

    def get_tiers(self, guild_id):
        return self.tiers

    def increment_count(self, guild_id, user_id):
        self.count += 1
        return self.count


class FakeMember:
    def __init__(self, member_id=2, *, bot=False, roles=None):
        self.id = member_id
        self.bot = bot
        self.roles = roles or []
        self.add_roles = AsyncMock()
        self.remove_roles = AsyncMock()


def make_activity_cog(store=None, *, enabled=True):
    cog = Activity.__new__(Activity)
    cog.bot = SimpleNamespace()
    cog.activity_store = store or FakeActivityStore()
    cog.module_store = SimpleNamespace(
        is_module_enabled=lambda guild_id, module_name: enabled
    )
    cog._last_counted = {}
    cog._cache = {}
    cog._role_alert_sent_at = {}
    return cog


def make_message(*, guild=None, author=None, channel_id=10):
    return SimpleNamespace(
        guild=guild,
        author=author or FakeMember(),
        channel=SimpleNamespace(id=channel_id),
    )


def test_activity_cache_channel_filters_and_invalidation():
    store = FakeActivityStore()
    store.includes = {10}
    store.excludes = {20}
    store.tiers = [(5, 100)]
    cog = make_activity_cog(store)

    assert cog._channel_allowed(1, 10)
    assert not cog._channel_allowed(1, 20)

    store.includes = set()
    cog.invalidate_cache(1)
    assert not cog._channel_allowed(1, 20)
    assert cog._channel_allowed(1, 30)


def test_activity_listener_ignores_disabled_filtered_and_cooldown_messages(monkeypatch):
    monkeypatch.setattr(activity_module.discord, "Member", FakeMember)
    guild = SimpleNamespace(id=1, get_role=lambda role_id: None)
    store = FakeActivityStore()
    store.excludes = {99}

    disabled = make_activity_cog(store, enabled=False)
    asyncio.run(disabled.on_message(make_message(guild=guild)))
    assert store.count == 0

    cog = make_activity_cog(store)
    asyncio.run(cog.on_message(make_message(guild=guild, channel_id=99)))
    assert store.count == 0

    asyncio.run(cog.on_message(make_message(guild=guild, channel_id=10)))
    asyncio.run(cog.on_message(make_message(guild=guild, channel_id=10)))
    assert store.count == 1


def test_activity_listener_assigns_milestone_role(monkeypatch):
    monkeypatch.setattr(activity_module.discord, "Member", FakeMember)
    role = FakeRole(10)
    guild = SimpleNamespace(id=1, get_role=lambda role_id: role if role_id == 10 else None)
    member = FakeMember()
    store = FakeActivityStore()
    store.cooldown = 0
    store.tiers = [(1, 10)]
    cog = make_activity_cog(store)

    asyncio.run(cog.on_message(make_message(guild=guild, author=member)))

    member.add_roles.assert_awaited_once_with(
        role,
        reason="Activity milestone: 1 messages",
    )
