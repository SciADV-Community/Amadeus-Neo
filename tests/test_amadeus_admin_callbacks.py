import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import cogs.amadeus_admin as admin_module
from amadeus.models import GuildConfig
from cogs.amadeus_admin import AmadeusAdmin, _AdminChannelConfirmView, _missing_admin_channel_permissions


class FakeResponse:
    def __init__(self):
        self.send_message = AsyncMock()
        self.edit_message = AsyncMock()


class FakeStore:
    def __init__(self):
        self.enabled = set()
        self.admin_role_id = None
        self.alert_channel_id = None

    def is_module_enabled(self, guild_id, module):
        return module in self.enabled

    def enable_module(self, guild_id, module):
        self.enabled.add(module)

    def disable_module(self, guild_id, module):
        self.enabled.discard(module)

    def get_enabled_modules(self, guild_id):
        return set(self.enabled)

    def set_admin_role(self, guild, role_id):
        self.admin_role_id = role_id

    def set_alert_channel(self, guild, channel_id):
        self.alert_channel_id = channel_id


def make_cog(store=None):
    cog = AmadeusAdmin.__new__(AmadeusAdmin)
    cog.bot = SimpleNamespace(extensions={"cogs.boost": object(), "cogs.activity": object()})
    cog.store = store or FakeStore()
    return cog


def make_interaction(guild):
    return SimpleNamespace(
        guild=guild,
        user=SimpleNamespace(id=2),
        response=FakeResponse(),
    )


def make_guild():
    admin_role = SimpleNamespace(id=10, mention="<@&10>")
    return SimpleNamespace(
        id=1,
        owner_id=2,
        default_role=SimpleNamespace(id=0),
        get_role=lambda role_id: admin_role if role_id == 10 else None,
        get_channel=lambda channel_id: None,
    )


def install_access(monkeypatch):
    monkeypatch.setattr(
        admin_module,
        "require_amadeus_access",
        AsyncMock(return_value=GuildConfig(guild_id=1, owner_id=2)),
    )


def test_missing_admin_channel_permissions_lists_missing_labels():
    channel = SimpleNamespace(
        permissions_for=lambda member: SimpleNamespace(
            view_channel=True,
            send_messages=False,
            embed_links=False,
        )
    )

    assert _missing_admin_channel_permissions(channel, SimpleNamespace()) == [
        "Send Messages",
        "Embed Links",
    ]


def test_module_enable_validates_available_already_enabled_and_success(monkeypatch):
    install_access(monkeypatch)
    monkeypatch.setattr(
        admin_module,
        "get_configured_cog_extensions",
        lambda: ["cogs.boost", "cogs.boost_admin", "cogs.activity"],
    )
    store = FakeStore()
    cog = make_cog(store)
    guild = make_guild()

    interaction = make_interaction(guild)
    asyncio.run(AmadeusAdmin.module_enable.callback(cog, interaction, "missing"))
    assert "not a known module" in interaction.response.send_message.await_args.args[0]

    store.enabled.add("boost")
    interaction = make_interaction(guild)
    asyncio.run(AmadeusAdmin.module_enable.callback(cog, interaction, "boost"))
    assert "already enabled" in interaction.response.send_message.await_args.args[0]

    interaction = make_interaction(guild)
    asyncio.run(AmadeusAdmin.module_enable.callback(cog, interaction, "activity"))
    assert "activity" in store.enabled
    assert "has been enabled" in interaction.response.send_message.await_args.args[0]


def test_module_disable_validates_state_and_disables(monkeypatch):
    install_access(monkeypatch)
    monkeypatch.setattr(
        admin_module,
        "get_configured_cog_extensions",
        lambda: ["cogs.boost"],
    )
    store = FakeStore()
    cog = make_cog(store)
    guild = make_guild()

    interaction = make_interaction(guild)
    asyncio.run(AmadeusAdmin.module_disable.callback(cog, interaction, "boost"))
    assert "not currently enabled" in interaction.response.send_message.await_args.args[0]

    store.enabled.add("boost")
    interaction = make_interaction(guild)
    asyncio.run(AmadeusAdmin.module_disable.callback(cog, interaction, "boost"))
    assert "boost" not in store.enabled
    assert "has been disabled" in interaction.response.send_message.await_args.args[0]


def test_list_cogs_and_config_build_expected_embeds(monkeypatch):
    monkeypatch.setattr(
        admin_module,
        "require_amadeus_access",
        AsyncMock(
            return_value=GuildConfig(
                guild_id=1,
                owner_id=2,
                admin_role_id=10,
                alert_channel_id=20,
            )
        ),
    )
    monkeypatch.setattr(
        admin_module,
        "get_configured_cog_extensions",
        lambda: ["cogs.boost", "cogs.boost_admin", "cogs.activity"],
    )
    store = FakeStore()
    store.enabled = {"boost"}
    store.admin_role_id = 10
    store.alert_channel_id = 20
    store.get_guild_config = lambda guild_id: GuildConfig(
        guild_id=guild_id,
        owner_id=2,
        admin_role_id=10,
        alert_channel_id=20,
    )
    cog = make_cog(store)
    guild = make_guild()

    interaction = make_interaction(guild)
    asyncio.run(AmadeusAdmin.amadeus_list_cogs.callback(cog, interaction))
    embed = interaction.response.send_message.await_args.kwargs["embed"]
    assert embed.title == "Amadeus Modules"
    assert "boost" in embed.fields[1].value

    interaction = make_interaction(guild)
    asyncio.run(AmadeusAdmin.amadeus_config.callback(cog, interaction))
    embed = interaction.response.send_message.await_args.kwargs["embed"]
    fields = {field.name: field.value for field in embed.fields}
    assert fields["Admin role"] == "<@&10>"
    assert fields["Alert channel"] == "<#20>"


def test_set_admin_role_and_admin_channel_confirmation(monkeypatch):
    install_access(monkeypatch)
    store = FakeStore()
    cog = make_cog(store)
    guild = make_guild()
    role = SimpleNamespace(id=10, mention="<@&10>")

    interaction = make_interaction(guild)
    asyncio.run(AmadeusAdmin.amadeus_set_admin_role.callback(cog, interaction, role))
    assert store.admin_role_id == 10
    assert "admin role set" in interaction.response.send_message.await_args.args[0]

    channel = SimpleNamespace(
        id=20,
        mention="#alerts",
        permissions_for=lambda target: SimpleNamespace(view_channel=True),
    )
    interaction = make_interaction(guild)
    asyncio.run(AmadeusAdmin.amadeus_set_admin_channel.callback(cog, interaction, channel))
    assert isinstance(interaction.response.send_message.await_args.kwargs["view"], _AdminChannelConfirmView)
    assert "@everyone" in interaction.response.send_message.await_args.args[0]
