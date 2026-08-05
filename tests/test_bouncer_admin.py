import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import cogs.bouncer_admin as bouncer_admin_module
from amadeus.models.bouncer import BounceConfig
from cogs.bouncer_admin import BounceAdmin, build_bouncer_panel_embed


def test_build_bouncer_panel_embed_includes_terms_privacy_and_assistance_text():
    embed = build_bouncer_panel_embed(
        min_account_age_days=14,
        panel_image_url=None,
        terms_of_service_url="https://example.com/terms",
        privacy_policy_url="https://example.com/privacy",
    )

    assert "- Your Discord account must be at least **14 days old**." in embed.description
    assert "- You must complete a private CAPTCHA." in embed.description
    assert "- You must agree to our [Terms of Service](https://example.com/terms)." in embed.description
    assert "**Privacy Policy:**" in embed.description
    assert "[Click here to see our Privacy Policy](https://example.com/privacy)" in embed.description
    assert embed.footer.text == (
        "Verification is private. Your CAPTCHA is only visible to you.\n"
        "If you require special assistance please contact a moderator."
    )


def test_build_bouncer_panel_embed_omits_unconfigured_terms_and_privacy_links():
    embed = build_bouncer_panel_embed(
        min_account_age_days=1,
        panel_image_url=None,
        terms_of_service_url="",
        privacy_policy_url="",
    )

    assert "Terms of Service" not in embed.description
    assert "Privacy Policy" not in embed.description


def test_build_bouncer_panel_embed_sets_optional_panel_image():
    embed = build_bouncer_panel_embed(
        min_account_age_days=14,
        panel_image_url="https://cdn.discordapp.com/attachments/1/2/panel.png",
        terms_of_service_url="",
        privacy_policy_url="",
    )

    assert embed.image.url == "https://cdn.discordapp.com/attachments/1/2/panel.png"


class FakeResponse:
    def __init__(self):
        self.send_message = AsyncMock()
        self.defer = AsyncMock()


class FakeFollowup:
    def __init__(self):
        self.send = AsyncMock()


class FakeStore:
    def __init__(self):
        self.config = BounceConfig(guild_id=1, verified_role_id=10, verification_channel_id=20)
        self.calls = []

    def get_bouncer_config(self, guild_id):
        return self.config

    def set_min_account_age_days(self, guild_id, days):
        self.calls.append(("min_age", guild_id, days))

    def set_max_failed_attempts(self, guild_id, attempts):
        self.calls.append(("attempts", guild_id, attempts))

    def set_captcha_expiry_minutes(self, guild_id, minutes):
        self.calls.append(("expiry", guild_id, minutes))

    def set_panel_image_url(self, guild_id, image_url):
        self.calls.append(("panel", guild_id, image_url))

    def set_verification_role_delay(self, guild_id, seconds):
        self.calls.append(("delay", guild_id, seconds))


def make_cog(*, store=None, bouncer_cog=None):
    cog = BounceAdmin.__new__(BounceAdmin)
    cog.bot = SimpleNamespace(get_cog=lambda name: bouncer_cog)
    cog.bounce_store = store or FakeStore()
    cog.module_store = SimpleNamespace(is_module_enabled=lambda guild_id, module: True)
    cog.backfill_tasks = {}
    cog.backfill_cancel_requested = set()
    cog.backfill_progress = {}
    return cog


def make_interaction(guild):
    return SimpleNamespace(
        guild=guild,
        user=SimpleNamespace(id=99),
        response=FakeResponse(),
        followup=FakeFollowup(),
    )


def install_access(monkeypatch):
    monkeypatch.setattr(
        bouncer_admin_module,
        "require_amadeus_access",
        AsyncMock(return_value=SimpleNamespace()),
    )


def test_bouncer_helpers_resolve_role_channel_and_clear_challenge(monkeypatch):
    class FakeTextChannel:
        pass

    channel = FakeTextChannel()
    role = SimpleNamespace(id=10)
    guild = SimpleNamespace(
        get_role=lambda role_id: role if role_id == 10 else None,
        get_channel=lambda channel_id: channel if channel_id == 20 else None,
    )
    monkeypatch.setattr(bouncer_admin_module.discord, "TextChannel", FakeTextChannel)
    bouncer_cog = SimpleNamespace(clear_challenge=Mock())
    cog = make_cog(bouncer_cog=bouncer_cog)

    assert cog.get_verified_role(guild, BounceConfig(guild_id=1, verified_role_id=10)) is role
    assert cog.get_verification_channel(guild, BounceConfig(guild_id=1, verification_channel_id=20)) is channel
    assert cog.get_backfill_progress(1) is cog.get_backfill_progress(1)

    cog._clear_user_challenge(1, 2)
    bouncer_cog.clear_challenge.assert_called_once_with(1, 2)


def test_bouncer_setting_commands_validate_and_persist(monkeypatch):
    install_access(monkeypatch)
    store = FakeStore()
    cog = make_cog(store=store)
    guild = SimpleNamespace(id=1)

    interaction = make_interaction(guild)
    asyncio.run(BounceAdmin.bouncer_settings_min_age.callback(cog, interaction, 31))
    assert "between **0** and **30**" in interaction.response.send_message.await_args.args[0]

    interaction = make_interaction(guild)
    asyncio.run(BounceAdmin.bouncer_settings_min_age.callback(cog, interaction, 7))
    assert store.calls[-1] == ("min_age", 1, 7)

    interaction = make_interaction(guild)
    asyncio.run(BounceAdmin.bouncer_settings_max_attempts.callback(cog, interaction, 4))
    assert store.calls[-1] == ("attempts", 1, 4)

    interaction = make_interaction(guild)
    asyncio.run(BounceAdmin.bouncer_settings_captcha_expiry.callback(cog, interaction, 12))
    assert store.calls[-1] == ("expiry", 1, 12)

    image = SimpleNamespace(
        content_type="image/png",
        url="https://cdn.discordapp.com/attachments/1/2/panel.png",
    )
    interaction = make_interaction(guild)
    asyncio.run(BounceAdmin.bouncer_settings_panel_image.callback(cog, interaction, image))
    assert store.calls[-1] == ("panel", 1, image.url)

    interaction = make_interaction(guild)
    asyncio.run(BounceAdmin.bouncer_settings_role_delay.callback(cog, interaction, 3))
    assert store.calls[-1] == ("delay", 1, 3)


def test_manual_verify_and_unverify_paths(monkeypatch):
    install_access(monkeypatch)
    role = SimpleNamespace(id=10, mention="<@&10>")
    guild = SimpleNamespace(id=1, get_role=lambda role_id: role if role_id == 10 else None)
    member = SimpleNamespace(id=2, mention="<@2>", roles=[], add_roles=AsyncMock(), remove_roles=AsyncMock())
    cog = make_cog()

    interaction = make_interaction(guild)
    asyncio.run(BounceAdmin.bouncer_verify.callback(cog, interaction, member))
    member.add_roles.assert_awaited_once_with(role, reason=f"Manually verified by {interaction.user}")
    assert "manually verified" in interaction.response.send_message.await_args.args[0]

    member.roles = [role]
    interaction = make_interaction(guild)
    asyncio.run(BounceAdmin.bouncer_verify.callback(cog, interaction, member))
    assert "already verified" in interaction.response.send_message.await_args.args[0]

    interaction = make_interaction(guild)
    asyncio.run(BounceAdmin.bouncer_unverify.callback(cog, interaction, member))
    member.remove_roles.assert_awaited_once_with(role, reason=f"Manually unverified by {interaction.user}")

    member.roles = []
    interaction = make_interaction(guild)
    asyncio.run(BounceAdmin.bouncer_unverify.callback(cog, interaction, member))
    assert "not currently verified" in interaction.response.send_message.await_args.args[0]


def test_verify_all_launches_backfill_and_reports_running_task(monkeypatch):
    install_access(monkeypatch)
    monkeypatch.setattr(bouncer_admin_module, "check_role_hierarchy", lambda bot_member, role: None)
    created = []

    def fake_create_task(coro):
        created.append(coro)
        coro.close()
        return SimpleNamespace(done=lambda: False)

    monkeypatch.setattr(bouncer_admin_module.asyncio, "create_task", fake_create_task)
    role = SimpleNamespace(id=10)
    guild = SimpleNamespace(
        id=1,
        get_role=lambda role_id: role,
        me=SimpleNamespace(guild_permissions=SimpleNamespace(manage_roles=True)),
    )
    cog = make_cog()
    interaction = make_interaction(guild)

    asyncio.run(BounceAdmin.bouncer_verify_all.callback(cog, interaction, False))

    assert created
    assert 1 in cog.backfill_tasks
    assert "Started the Verified role backfill" in interaction.followup.send.await_args.args[0]

    interaction = make_interaction(guild)
    asyncio.run(BounceAdmin.bouncer_verify_all.callback(cog, interaction, False))
    assert "already running" in interaction.followup.send.await_args.args[0]


def test_backfill_status_cancel_and_run_loop(monkeypatch):
    install_access(monkeypatch)
    monkeypatch.setattr(bouncer_admin_module.asyncio, "sleep", AsyncMock())
    role = SimpleNamespace(id=10)
    members = [
        SimpleNamespace(id=1, bot=True, roles=[], add_roles=AsyncMock(), __str__=lambda self: "bot"),
        SimpleNamespace(id=2, bot=False, roles=[role], add_roles=AsyncMock(), __str__=lambda self: "verified"),
        SimpleNamespace(id=3, bot=False, roles=[], add_roles=AsyncMock(), __str__=lambda self: "new"),
    ]

    class FakeMemberStream:
        def __aiter__(self):
            return self._iter()

        async def _iter(self):
            for member in members:
                yield member

    guild = SimpleNamespace(id=1, fetch_members=lambda limit: FakeMemberStream())
    cog = make_cog()

    asyncio.run(cog._run_backfill(guild, role, include_bots=False))

    progress = cog.backfill_progress[1]
    assert progress.total_seen == 3
    assert progress.skipped_bots == 1
    assert progress.skipped_already_verified == 1
    assert progress.added == 1
    members[2].add_roles.assert_awaited_once_with(role, reason="Initial Verified role backfill")

    cog.backfill_tasks[1] = SimpleNamespace(done=lambda: False)
    interaction = make_interaction(SimpleNamespace(id=1))
    asyncio.run(BounceAdmin.bouncer_backfill_status.callback(cog, interaction))
    assert interaction.response.send_message.await_args.kwargs["embed"].title == "Verified Role Backfill Status"

    interaction = make_interaction(SimpleNamespace(id=1))
    asyncio.run(BounceAdmin.bouncer_cancel_backfill.callback(cog, interaction))
    assert 1 in cog.backfill_cancel_requested
