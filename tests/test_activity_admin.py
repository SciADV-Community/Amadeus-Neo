from types import SimpleNamespace

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
