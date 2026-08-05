import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from discord.ext import commands

import cogs.amadeus_owner as owner_module
from cogs.amadeus_owner import AmadeusOwner


class FakeResponse:
    def __init__(self):
        self.send_message = AsyncMock()
        self.defer = AsyncMock()


class FakeFollowup:
    def __init__(self):
        self.send = AsyncMock()


def make_interaction(*, user_id=10, guild=True):
    return SimpleNamespace(
        user=SimpleNamespace(id=user_id, mention=f"<@{user_id}>"),
        guild=SimpleNamespace(id=1) if guild else None,
        response=FakeResponse(),
        followup=FakeFollowup(),
    )


def make_bot(*, extensions=None):
    return SimpleNamespace(
        extensions=extensions or {},
        load_extension=AsyncMock(),
        unload_extension=AsyncMock(),
        reload_extension=AsyncMock(),
    )


def test_owner_gate_rejects_when_owner_id_unset_or_mismatched(monkeypatch):
    cog = AmadeusOwner(make_bot())
    interaction = make_interaction(user_id=10)

    monkeypatch.setattr(owner_module, "OWNER_ID", None)
    assert asyncio.run(cog._check_bot_owner(interaction)) is False
    interaction.response.send_message.assert_awaited_once_with(
        "This command is restricted to the bot owner.",
        ephemeral=True,
    )

    interaction = make_interaction(user_id=10)
    monkeypatch.setattr(owner_module, "OWNER_ID", 99)
    assert asyncio.run(cog._check_bot_owner(interaction)) is False


def test_extension_allowed_requires_cogs_prefix_and_configured_extension(monkeypatch):
    monkeypatch.setattr(
        owner_module,
        "get_configured_cog_extensions",
        lambda skip_static=False: ["cogs.boost", "cogs.boost_admin"],
    )
    cog = AmadeusOwner(make_bot())

    assert cog._extension_allowed("cogs.boost")
    assert not cog._extension_allowed("boost")
    assert not cog._extension_allowed("cogs.unknown")


def test_load_cog_rejects_unconfigured_and_static_extensions(monkeypatch):
    monkeypatch.setattr(owner_module, "OWNER_ID", 10)
    monkeypatch.setattr(
        owner_module,
        "get_configured_cog_extensions",
        lambda skip_static=False: ["cogs.boost", "cogs.amadeus_admin"],
    )
    cog = AmadeusOwner(make_bot())

    interaction = make_interaction()
    asyncio.run(AmadeusOwner.load_cog.callback(cog, interaction, "unknown"))
    interaction.response.send_message.assert_awaited_once()
    assert "not in the configured" in interaction.response.send_message.await_args.args[0]

    interaction = make_interaction()
    asyncio.run(AmadeusOwner.load_cog.callback(cog, interaction, "amadeus_admin"))
    interaction.response.send_message.assert_awaited_once_with(
        "`cogs.amadeus_admin` is static and is already managed by `main.py`.",
        ephemeral=True,
    )


def test_load_cog_loads_extension_and_syncs_commands(monkeypatch):
    monkeypatch.setattr(owner_module, "OWNER_ID", 10)
    monkeypatch.setattr(owner_module, "is_static_extension", lambda extension: False)
    monkeypatch.setattr(
        owner_module,
        "get_configured_cog_extensions",
        lambda skip_static=False: ["cogs.boost"],
    )
    sync = AsyncMock(return_value=7)
    monkeypatch.setattr(owner_module, "sync_commands_to_guild", sync)
    bot = make_bot()
    cog = AmadeusOwner(bot)
    interaction = make_interaction()

    asyncio.run(AmadeusOwner.load_cog.callback(cog, interaction, "boost"))

    interaction.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
    bot.load_extension.assert_awaited_once_with("cogs.boost")
    sync.assert_awaited_once_with(bot, interaction.guild)
    assert "Loaded `cogs.boost`" in interaction.followup.send.await_args.args[0]


def test_load_cog_reports_known_extension_errors(monkeypatch):
    monkeypatch.setattr(owner_module, "OWNER_ID", 10)
    monkeypatch.setattr(owner_module, "is_static_extension", lambda extension: False)
    monkeypatch.setattr(
        owner_module,
        "get_configured_cog_extensions",
        lambda skip_static=False: ["cogs.boost"],
    )
    bot = make_bot()
    bot.load_extension = AsyncMock(side_effect=commands.ExtensionAlreadyLoaded("cogs.boost"))
    cog = AmadeusOwner(bot)
    interaction = make_interaction()

    asyncio.run(AmadeusOwner.load_cog.callback(cog, interaction, "boost"))

    interaction.followup.send.assert_awaited_once_with(
        "`cogs.boost` is already loaded.",
        ephemeral=True,
    )


def test_unload_cog_handles_not_loaded_and_success(monkeypatch):
    monkeypatch.setattr(owner_module, "OWNER_ID", 10)
    monkeypatch.setattr(owner_module, "is_static_extension", lambda extension: False)
    monkeypatch.setattr(
        owner_module,
        "get_configured_cog_extensions",
        lambda skip_static=False: ["cogs.boost"],
    )
    sync = AsyncMock(return_value=3)
    monkeypatch.setattr(owner_module, "sync_commands_to_guild", sync)

    bot = make_bot(extensions={})
    cog = AmadeusOwner(bot)
    interaction = make_interaction()
    asyncio.run(AmadeusOwner.unload_cog.callback(cog, interaction, "boost"))
    interaction.followup.send.assert_awaited_once_with(
        "`cogs.boost` is not currently loaded.",
        ephemeral=True,
    )

    bot = make_bot(extensions={"cogs.boost": object()})
    cog = AmadeusOwner(bot)
    interaction = make_interaction()
    asyncio.run(AmadeusOwner.unload_cog.callback(cog, interaction, "boost"))
    bot.unload_extension.assert_awaited_once_with("cogs.boost")
    sync.assert_awaited_once_with(bot, interaction.guild)
    assert "Unloaded `cogs.boost`" in interaction.followup.send.await_args.args[0]


def test_sync_command_uses_current_guild(monkeypatch):
    monkeypatch.setattr(owner_module, "OWNER_ID", 10)
    sync = AsyncMock(return_value=4)
    monkeypatch.setattr(owner_module, "sync_commands_to_guild", sync)
    bot = make_bot()
    cog = AmadeusOwner(bot)
    interaction = make_interaction()

    asyncio.run(AmadeusOwner.sync_cmd.callback(cog, interaction))

    interaction.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
    sync.assert_awaited_once_with(bot, interaction.guild)
    interaction.followup.send.assert_awaited_once_with(
        "Synced **4** command(s) to this server.",
        ephemeral=True,
    )


def test_hot_reload_reports_reloaded_failed_and_empty(monkeypatch):
    monkeypatch.setattr(owner_module, "OWNER_ID", 10)
    monkeypatch.setattr(owner_module.importlib, "reload", lambda module: module)
    monkeypatch.setattr(owner_module, "is_static_extension", lambda extension: extension == "cogs.amadeus_admin")
    monkeypatch.setattr(owner_module.AmadeusOwner, "_extension_allowed", lambda self, ext: ext != "cogs.unconfigured")

    bot = make_bot(extensions={"cogs.boost": object(), "cogs.unconfigured": object(), "cogs.amadeus_admin": object()})
    cog = AmadeusOwner(bot)
    interaction = make_interaction()

    asyncio.run(AmadeusOwner.hot_reload.callback(cog, interaction))

    bot.reload_extension.assert_awaited_once_with("cogs.boost")
    content = interaction.followup.send.await_args.args[0]
    assert "**Reloaded:**" in content
    assert "`cogs.boost`" in content
    assert "not in configured" in content

    bot = make_bot(extensions={"cogs.amadeus_admin": object()})
    cog = AmadeusOwner(bot)
    interaction = make_interaction()
    asyncio.run(AmadeusOwner.hot_reload.callback(cog, interaction))
    assert interaction.followup.send.await_args.args[0] == "No dynamic extensions to reload."
