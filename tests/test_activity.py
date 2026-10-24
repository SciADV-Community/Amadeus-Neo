import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord

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


def test_monthly_rollover_removes_old_holders_and_adds_current_qualifiers():
    old_member = SimpleNamespace(
        id=1,
        bot=False,
        remove_roles=AsyncMock(),
        add_roles=AsyncMock(),
    )
    new_member = SimpleNamespace(
        id=2,
        bot=False,
        remove_roles=AsyncMock(),
        add_roles=AsyncMock(),
    )
    role = FakeRole(10)
    role.members = [old_member]
    guild = SimpleNamespace(
        id=3,
        get_role=lambda role_id: role if role_id == 10 else None,
        get_member=lambda user_id: {1: old_member, 2: new_member}.get(user_id),
    )
    old_member.guild = guild
    new_member.guild = guild

    cog = Activity.__new__(Activity)
    cog._cache = {
        3: SimpleNamespace(
            monthly_tiers=[(5, 10)],
            role_swap_enabled=False,
        )
    }
    cog.activity_store = SimpleNamespace(
        get_counts=lambda *args, **kwargs: {2: 7},
    )

    asyncio.run(cog._reconcile_monthly_roles(guild, "2026-10"))

    old_member.remove_roles.assert_awaited_once_with(
        role,
        reason="Monthly activity rollover: 2026-10",
    )
    new_member.add_roles.assert_awaited_once_with(
        role,
        reason="Monthly activity rollover: 2026-10",
    )


def test_monthly_rollover_missing_configured_role_fails_and_alerts(monkeypatch):
    guild = SimpleNamespace(id=3, get_role=lambda role_id: None)
    cog = Activity.__new__(Activity)
    cog.bot = SimpleNamespace()
    cog.module_store = SimpleNamespace()
    cog._cache = {
        3: SimpleNamespace(monthly_tiers=[(5, 10)], role_swap_enabled=False)
    }
    cog.activity_store = SimpleNamespace(get_counts=lambda *args, **kwargs: {2: 7})
    alert = AsyncMock(return_value=True)
    monkeypatch.setattr(activity_module, "send_alert", alert)

    succeeded = asyncio.run(cog._reconcile_monthly_roles(guild, "2026-10"))

    assert succeeded is False
    alert.assert_awaited_once()
    assert "no longer exists" in alert.await_args.args[3]


def test_monthly_period_keys_share_one_clock_read(monkeypatch):
    timestamp = object()
    seen = []
    monkeypatch.setattr(activity_module.discord.utils, "utcnow", lambda: timestamp)
    monkeypatch.setattr(
        activity_module,
        "utc_month_key",
        lambda now: seen.append(now) or "2026-10",
    )
    monkeypatch.setattr(
        activity_module,
        "previous_utc_month_key",
        lambda now: seen.append(now) or "2026-09",
    )

    cog = Activity.__new__(Activity)
    cog._monthly_rollover_lock = asyncio.Lock()
    cog.bot = SimpleNamespace(guilds=[])

    asyncio.run(cog._ensure_monthly_roles_current())

    assert seen == [timestamp, timestamp]


def test_failed_monthly_role_change_sends_alert(monkeypatch):
    response = SimpleNamespace(status=403, reason="Forbidden")
    member = SimpleNamespace(
        id=2,
        guild=SimpleNamespace(id=1),
        add_roles=AsyncMock(side_effect=discord.Forbidden(response, "no permission")),
        remove_roles=AsyncMock(),
    )
    role = FakeRole(10)
    send_alert = AsyncMock(return_value=True)
    monkeypatch.setattr(activity_module, "send_alert", send_alert)

    cog = Activity.__new__(Activity)
    cog.bot = SimpleNamespace()
    cog.module_store = SimpleNamespace()
    cog._role_alert_sent_at = {}

    succeeded = asyncio.run(
        cog._change_monthly_role(member, role, add=True, period="2026-09")
    )

    assert succeeded is False
    send_alert.assert_awaited_once()
    assert "Failed to assign" in send_alert.await_args.args[3]


def test_failed_monthly_role_change_alerts_on_a_fresh_monotonic_clock(monkeypatch):
    """Regression: on a freshly booted host time.monotonic() starts below the
    cooldown, so the first alert must not be treated as already sent."""
    response = SimpleNamespace(status=403, reason="Forbidden")
    member = SimpleNamespace(
        id=2,
        guild=SimpleNamespace(id=1),
        add_roles=AsyncMock(side_effect=discord.Forbidden(response, "no permission")),
        remove_roles=AsyncMock(),
    )
    role = FakeRole(10)
    send_alert = AsyncMock(return_value=True)
    monkeypatch.setattr(activity_module, "send_alert", send_alert)
    # Replace only this module's reference; the event loop keeps the real clock.
    monkeypatch.setattr(
        activity_module, "time", SimpleNamespace(monotonic=lambda: 5.0)
    )

    cog = Activity.__new__(Activity)
    cog.bot = SimpleNamespace()
    cog.module_store = SimpleNamespace()
    cog._role_alert_sent_at = {}

    succeeded = asyncio.run(
        cog._change_monthly_role(member, role, add=True, period="2026-09")
    )

    assert succeeded is False
    send_alert.assert_awaited_once()


def test_failed_monthly_role_change_respects_cooldown(monkeypatch):
    response = SimpleNamespace(status=403, reason="Forbidden")
    member = SimpleNamespace(
        id=2,
        guild=SimpleNamespace(id=1),
        add_roles=AsyncMock(side_effect=discord.Forbidden(response, "no permission")),
        remove_roles=AsyncMock(),
    )
    role = FakeRole(10)
    send_alert = AsyncMock(return_value=True)
    monkeypatch.setattr(activity_module, "send_alert", send_alert)
    monkeypatch.setattr(
        activity_module, "time", SimpleNamespace(monotonic=lambda: 5.0)
    )

    cog = Activity.__new__(Activity)
    cog.bot = SimpleNamespace()
    cog.module_store = SimpleNamespace()
    # An alert was sent moments ago; it should be suppressed.
    cog._role_alert_sent_at = {1: 4.9}

    succeeded = asyncio.run(
        cog._change_monthly_role(member, role, add=True, period="2026-09")
    )

    assert succeeded is False
    send_alert.assert_not_awaited()
