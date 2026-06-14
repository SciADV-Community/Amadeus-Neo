import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

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
