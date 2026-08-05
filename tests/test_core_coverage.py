import asyncio
import logging
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import amadeus.alerts as alerts_module
import amadeus.application_profile as profile_module
import amadeus.captcha_utils as captcha_module
import amadeus.logging_utils as logging_module
import amadeus.permissions as permissions_module
from amadeus.alerts import send_alert
from amadeus.application_profile import sync_application_profile
from amadeus.captcha_utils import CAPTCHA_LENGTH, CaptchaService, normalize_code
from amadeus.cogs import format_extension_error, sync_commands_to_guild
from amadeus.database import ConfigStore
from amadeus.logging_utils import AmadeusFormatter, fullwidth, log, normalize_level, setup_logging, str_to_bool
from amadeus.models import GuildConfig
from amadeus.moderation import action_display, action_kick, action_mute, execute_action
from amadeus.permissions import require_amadeus_access
from discord.ext import commands


def test_config_store_persists_guild_config_and_enabled_modules(temp_db_path):
    store = ConfigStore()
    guild = SimpleNamespace(id=1, owner_id=2)

    try:
        config = store.ensure_guild_config(guild)
        assert config == GuildConfig(guild_id=1, owner_id=2)

        guild.owner_id = 3
        config = store.ensure_guild_config(guild)
        assert config.owner_id == 3

        store.set_alert_channel(guild, 10)
        store.set_admin_role(guild, 20)
        config = store.get_guild_config(1)
        assert config.alert_channel_id == 10
        assert config.admin_role_id == 20

        store.enable_module(1, "boost")
        store.enable_module(1, "boost")
        assert store.is_module_enabled(1, "boost")
        assert store.get_enabled_modules(1) == {"boost"}

        store.disable_module(1, "boost")
        assert not store.is_module_enabled(1, "boost")
        assert store.get_enabled_modules(1) == set()
    finally:
        store.close()


def test_config_store_migrates_alert_channel_column(temp_db_path):
    db = sqlite3.connect(temp_db_path)
    db.execute(
        """
        CREATE TABLE guild_config (
            guild_id INTEGER PRIMARY KEY,
            owner_id INTEGER NOT NULL,
            admin_role_id INTEGER,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    db.commit()
    db.close()

    store = ConfigStore()
    try:
        columns = {
            row["name"]
            for row in store.db.execute("PRAGMA table_info(guild_config)").fetchall()
        }
    finally:
        store.close()

    assert "alert_channel_id" in columns


def test_send_alert_handles_missing_config_and_unconfigured_channel():
    class MissingConfigStore:
        def get_guild_config(self, guild_id):
            raise RuntimeError("missing")

    assert asyncio.run(send_alert(SimpleNamespace(), MissingConfigStore(), 1, "content")) is False

    store = SimpleNamespace(
        get_guild_config=lambda guild_id: GuildConfig(
            guild_id=guild_id,
            owner_id=2,
            alert_channel_id=None,
        )
    )
    assert asyncio.run(send_alert(SimpleNamespace(), store, 1, "content")) is False


def test_send_alert_rejects_wrong_channel_type_and_sends_to_text_channel(monkeypatch):
    class FakeTextChannel:
        def __init__(self):
            self.send = AsyncMock()

    store = SimpleNamespace(
        get_guild_config=lambda guild_id: GuildConfig(
            guild_id=guild_id,
            owner_id=2,
            alert_channel_id=10,
        )
    )
    monkeypatch.setattr(alerts_module.discord, "TextChannel", FakeTextChannel)

    wrong_bot = SimpleNamespace(get_channel=lambda channel_id: SimpleNamespace())
    assert asyncio.run(send_alert(wrong_bot, store, 1, "content")) is False

    channel = FakeTextChannel()
    bot = SimpleNamespace(get_channel=lambda channel_id: channel)
    assert asyncio.run(send_alert(bot, store, 1, "content")) is True
    channel.send.assert_awaited_once()
    assert channel.send.await_args.args == ("content",)


def test_sync_application_profile_updates_changed_description(monkeypatch):
    monkeypatch.setattr(profile_module, "PRIVACY_POLICY_URL", "https://example.com/privacy")
    monkeypatch.setattr(profile_module, "TERMS_OF_SERVICE_URL", "https://example.com/terms")
    monkeypatch.setattr(
        profile_module,
        "build_application_description",
        lambda current: f"{current}\nPrivacy Policy: https://example.com/privacy",
    )

    app_info = SimpleNamespace(description="Amadeus", edit=AsyncMock())
    bot = SimpleNamespace(application_info=AsyncMock(return_value=app_info))

    asyncio.run(sync_application_profile(bot))

    app_info.edit.assert_awaited_once()
    assert "Privacy Policy: https://example.com/privacy" in app_info.edit.await_args.kwargs["description"]


def test_sync_application_profile_skips_when_no_links_or_already_current(monkeypatch):
    monkeypatch.setattr(profile_module, "PRIVACY_POLICY_URL", "")
    monkeypatch.setattr(profile_module, "TERMS_OF_SERVICE_URL", "")
    bot = SimpleNamespace(application_info=AsyncMock())

    asyncio.run(sync_application_profile(bot))

    bot.application_info.assert_not_awaited()

    monkeypatch.setattr(profile_module, "PRIVACY_POLICY_URL", "https://example.com/privacy")
    monkeypatch.setattr(profile_module, "TERMS_OF_SERVICE_URL", "")
    description = "Privacy Policy: https://example.com/privacy"
    app_info = SimpleNamespace(description=description, edit=AsyncMock())
    bot = SimpleNamespace(application_info=AsyncMock(return_value=app_info))

    asyncio.run(sync_application_profile(bot))

    app_info.edit.assert_not_awaited()


def test_logging_helpers_and_formatter(monkeypatch):
    assert str_to_bool(" YES ")
    assert not str_to_bool("no")
    assert fullwidth("A b!") == "Ａ　ｂ！"
    assert normalize_level("warn") == logging.WARNING
    assert normalize_level("fatal") == logging.CRITICAL
    assert normalize_level("unknown") == logging.INFO

    formatter = AmadeusFormatter(fullwidth_messages=True)
    record = logging.LogRecord(
        "amadeus.test",
        logging.INFO,
        __file__,
        1,
        "Hi %s",
        ("A",),
        None,
    )
    assert "Ｈｉ" in formatter.format(record)
    assert record.msg == "Hi %s"
    assert record.args == ("A",)

    monkeypatch.setenv("AMADEUS_LOG_LEVEL", "DEBUG")
    monkeypatch.setenv("AMADEUS_DISCORD_LOG_LEVEL", "ERROR")
    assert logging_module.get_log_level() == logging.DEBUG
    assert logging_module.get_discord_log_level() == logging.ERROR


def test_setup_logging_replaces_handlers_and_log_helper(monkeypatch):
    logger = logging.getLogger(logging_module.LOGGER_NAME)
    discord_logger = logging.getLogger("discord")
    old_handlers = list(logger.handlers)
    old_level = logger.level
    old_propagate = logger.propagate
    old_discord_level = discord_logger.level

    try:
        logger.addHandler(logging.NullHandler())
        monkeypatch.setenv("AMADEUS_LOG_LEVEL", "WARNING")
        monkeypatch.setenv("AMADEUS_FULLWIDTH_LOGS", "true")
        monkeypatch.setenv("AMADEUS_DISCORD_LOG_LEVEL", "ERROR")

        setup_logging()

        assert logger.level == logging.WARNING
        assert logger.propagate is False
        assert len(logger.handlers) == 1
        assert discord_logger.level == logging.ERROR
        log("handled", level="exception", logger_name="tests")
    finally:
        logger.handlers[:] = old_handlers
        logger.setLevel(old_level)
        logger.propagate = old_propagate
        discord_logger.setLevel(old_discord_level)


def test_captcha_service_generates_text_and_file(monkeypatch):
    monkeypatch.setattr(captcha_module.secrets, "choice", lambda alphabet: alphabet[-1])

    service = CaptchaService.__new__(CaptchaService)
    assert service.make_captcha_text() == "9" * CAPTCHA_LENGTH
    assert normalize_code(" ab12 ") == "AB12"


def test_moderation_exception_paths_and_display_helpers():
    class ForbiddenMember:
        id = 2
        guild = SimpleNamespace(id=1)

        async def timeout(self, *args, **kwargs):
            raise permissions_module.discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "no")

    result = asyncio.run(action_mute(ForbiddenMember()))
    assert result == "Failed — missing Moderate Members permission or member outranks the bot."

    class HttpMember:
        id = 2
        guild = SimpleNamespace(id=1)

        async def kick(self, **kwargs):
            raise permissions_module.discord.HTTPException(SimpleNamespace(status=500, reason="Error"), "boom")

    assert asyncio.run(action_kick(HttpMember())).startswith("Failed — ")
    assert asyncio.run(execute_action(SimpleNamespace(), SimpleNamespace(), None)) == "No action configured."
    assert asyncio.run(execute_action(SimpleNamespace(), SimpleNamespace(), "missing")) == "Unknown action: missing"

    role = SimpleNamespace(id=10, name="Verified")
    guild = SimpleNamespace(get_role=lambda role_id: role if role_id == 10 else None)
    assert action_display(None, None, guild) == "None"
    assert action_display("remove_role", 10, guild) == "Remove role (Verified)"
    assert action_display("remove_role", 99, guild) == "Remove role (ID 99)"
    assert action_display("mute", None, guild) == "Mute (28-day timeout)"


def test_require_amadeus_access_rejects_guildless_non_member_and_owner_without_admin(monkeypatch):
    guildless = SimpleNamespace(
        guild=None,
        response=SimpleNamespace(send_message=AsyncMock()),
    )
    assert asyncio.run(require_amadeus_access(guildless, SimpleNamespace())) is None
    guildless.response.send_message.assert_awaited_once_with(
        "This can only be used inside a server.",
        ephemeral=True,
    )

    class FakeMember:
        def __init__(self, member_id, roles=None):
            self.id = member_id
            self.roles = roles or []

    monkeypatch.setattr(permissions_module.discord, "Member", FakeMember)
    guild = SimpleNamespace(id=1, owner_id=2, get_role=lambda role_id: None)

    non_member = SimpleNamespace(
        guild=guild,
        user=SimpleNamespace(id=3),
        response=SimpleNamespace(send_message=AsyncMock()),
    )
    assert asyncio.run(require_amadeus_access(non_member, SimpleNamespace())) is None
    non_member.response.send_message.assert_awaited_once_with(
        "Could not read your server member data.",
        ephemeral=True,
    )

    config = GuildConfig(guild_id=1, owner_id=2, admin_role_id=None)
    store = SimpleNamespace(ensure_guild_config=lambda guild: config)
    denied = SimpleNamespace(
        guild=guild,
        user=FakeMember(3),
        response=SimpleNamespace(send_message=AsyncMock()),
    )
    assert asyncio.run(require_amadeus_access(denied, store)) is None
    denied.response.send_message.assert_awaited_once_with(
        "Only the server owner can use `/amadeus` until an admin role is configured.",
        ephemeral=True,
    )


def test_format_extension_error_and_sync_commands_to_guild():
    failed = commands.ExtensionFailed("cogs.broken", RuntimeError("boom"))
    assert format_extension_error(failed) == "RuntimeError: boom"
    assert format_extension_error(commands.ExtensionAlreadyLoaded("cogs.boost")).startswith(
        "ExtensionAlreadyLoaded:"
    )

    class FakeTree:
        def __init__(self):
            self.cleared = []
            self.copied = []

        def clear_commands(self, *, guild):
            self.cleared.append(guild.id)

        def copy_global_to(self, *, guild):
            self.copied.append(guild.id)

        async def sync(self, *, guild):
            return [object(), object()]

    tree = FakeTree()
    count = asyncio.run(sync_commands_to_guild(SimpleNamespace(tree=tree), SimpleNamespace(id=123)))

    assert count == 2
    assert tree.cleared == [123]
    assert tree.copied == [123]
