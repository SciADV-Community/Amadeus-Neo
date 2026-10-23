import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import cogs.activity_admin as activity_admin_module
from cogs.activity_admin import ActivityAdmin


class FakeRole:
    def __init__(self, role_id, mention):
        self.id = role_id
        self.mention = mention


class FakeGuild:
    def __init__(self):
        self.roles = {
            10: FakeRole(10, "<@&10>"),
            20: FakeRole(20, "<@&20>"),
        }
        self.members = {
            1: SimpleNamespace(id=1, mention="<@1>"),
            2: SimpleNamespace(id=2, mention="<@2>"),
        }

    def get_role(self, role_id):
        return self.roles.get(role_id)

    def get_member(self, user_id):
        return self.members.get(user_id)


def make_activity_admin():
    return ActivityAdmin.__new__(ActivityAdmin)


def test_build_status_embed_shows_current_and_next_role():
    cog = make_activity_admin()
    guild = FakeGuild()
    member = SimpleNamespace(id=1, display_name="Kurisu")

    embed = cog._build_status_embed(
        guild,
        member,
        count=75,
        tiers=[(50, 10), (100, 20)],
    )

    assert embed.title == "Activity Status — Kurisu"
    fields = {field.name: field.value for field in embed.fields}
    assert fields["Messages counted"] == "**75**"
    assert "<@&10>" in fields["Current role"]
    assert "**25** more messages for <@&20>." == fields["Next role"]


def test_build_status_embed_handles_no_tiers():
    cog = make_activity_admin()
    guild = FakeGuild()
    member = SimpleNamespace(id=1, display_name="Okabe")

    embed = cog._build_status_embed(guild, member, count=3, tiers=[])

    fields = {field.name: field.value for field in embed.fields}
    assert fields["Current role"] == "No activity role yet."
    assert fields["Activity tiers"] == "No tiers configured."


def test_build_leaderboard_embed_formats_top_members():
    cog = make_activity_admin()
    guild = FakeGuild()

    embed = cog._build_leaderboard_embed(guild, [(1, 42), (2, 30), (999, 10)])

    assert embed.title == "Activity Leaderboard"
    assert "🥇 <@1> — **42** messages" in embed.description
    assert "🥈 <@2> — **30** messages" in embed.description
    assert "🥉 <@999> — **10** messages" in embed.description


def test_build_leaderboard_embed_handles_empty_leaderboard():
    cog = make_activity_admin()

    embed = cog._build_leaderboard_embed(FakeGuild(), [])

    assert embed.description == "No activity has been counted yet."


def test_build_leaderboard_embed_paginates_30_members_with_global_ranks():
    cog = make_activity_admin()
    leaderboard = [(user_id, 100 - user_id) for user_id in range(1, 31)]

    embed = cog._build_leaderboard_embed(
        FakeGuild(),
        leaderboard,
        page=1,
        monthly=True,
    )

    assert embed.title == "Monthly Activity Leaderboard"
    assert "`#11` <@11> — **89** messages" in embed.description
    assert "<@10>" not in embed.description
    assert embed.footer.text == "Page 2/3 • Top 30"


def test_send_leaderboard_is_ephemeral():
    cog = make_activity_admin()
    cog.activity_store = SimpleNamespace(get_leaderboard=lambda *args, **kwargs: [])
    interaction = SimpleNamespace(
        guild=FakeGuild(),
        response=SimpleNamespace(send_message=AsyncMock()),
    )
    interaction.guild.id = 1

    asyncio.run(cog._send_leaderboard(interaction, monthly=False))

    assert interaction.response.send_message.await_args.kwargs["ephemeral"] is True


def test_tier_add_rejects_role_already_used_by_any_scope(monkeypatch):
    cog = make_activity_admin()
    cog.module_store = SimpleNamespace()
    cog.activity_store = SimpleNamespace(
        get_tier_uses_for_role=lambda guild_id, role_id: [("monthly", 25)],
    )
    cog.bot = SimpleNamespace(get_cog=lambda _name: None)
    role = SimpleNamespace(id=10, mention="<@&10>")
    guild = SimpleNamespace(id=1, default_role=SimpleNamespace(id=0), me=SimpleNamespace())
    interaction = SimpleNamespace(
        guild=guild,
        response=SimpleNamespace(send_message=AsyncMock()),
    )

    async def allow_access(_interaction, _store):
        return SimpleNamespace()

    monkeypatch.setattr(activity_admin_module, "require_amadeus_access", allow_access)
    monkeypatch.setattr(activity_admin_module, "check_role_hierarchy", lambda _bot, _role: None)

    asyncio.run(ActivityAdmin.tier_add.callback(cog, interaction, 100, role, False))

    interaction.response.send_message.assert_awaited_once()
    assert "already used" in interaction.response.send_message.await_args.args[0]


def test_tier_remove_cleans_role_before_deleting_database_entry(monkeypatch):
    events = []
    member = SimpleNamespace(
        id=7,
        remove_roles=AsyncMock(side_effect=lambda *args, **kwargs: events.append("role removed")),
    )
    role = SimpleNamespace(id=10, mention="<@&10>", members=[member])
    guild = SimpleNamespace(id=1, get_role=lambda role_id: role if role_id == 10 else None)

    class Store:
        def get_tier(self, guild_id, threshold, *, monthly=False):
            return threshold, 10

        def get_tier_uses_for_role(self, guild_id, role_id):
            return [("monthly", 25)]

        def remove_tier(self, guild_id, threshold, *, monthly=False):
            events.append("database removed")
            return True

    cog = make_activity_admin()
    cog.activity_store = Store()
    cog.module_store = SimpleNamespace()
    cog.bot = SimpleNamespace(get_cog=lambda _name: None)
    interaction = SimpleNamespace(
        guild=guild,
        response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()),
        edit_original_response=AsyncMock(),
    )

    async def allow_access(_interaction, _store):
        return SimpleNamespace()

    monkeypatch.setattr(activity_admin_module, "require_amadeus_access", allow_access)

    asyncio.run(ActivityAdmin.tier_remove.callback(cog, interaction, 25, True))

    assert events == ["role removed", "database removed"]
    interaction.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
