import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord

import amadeus.approval as approval_module
from amadeus.approval import (
    ApprovalView,
    _ApproveButton,
    _CommentModal,
    _DenyButton,
    _require_approval_role,
    add_dynamic_items,
    post_approval_request,
    register_approval_callback,
    unregister_approval_callback,
)
from amadeus.models import GuildConfig


class FakeResponse:
    def __init__(self):
        self.send_message = AsyncMock()
        self.defer = AsyncMock()
        self.send_modal = AsyncMock()


class FakeFollowup:
    def __init__(self):
        self.send = AsyncMock()


class FakeMember:
    def __init__(self, member_id=2, roles=None):
        self.id = member_id
        self.roles = roles or []
        self.mention = f"<@{member_id}>"


class FakeTextChannel:
    def __init__(self):
        self.send = AsyncMock()


def make_interaction(*, guild=None, user=None, channel_id=10):
    return SimpleNamespace(
        guild=guild,
        user=user if user is not None else FakeMember(),
        channel_id=channel_id,
        client=SimpleNamespace(name="bot"),
        message=SimpleNamespace(id=123),
        response=FakeResponse(),
        followup=FakeFollowup(),
    )


def install_config_store(monkeypatch, *, config=None, raises=False):
    class FakeStore:
        def get_guild_config(self, guild_id):
            if raises:
                raise RuntimeError("missing")
            return config

        def close(self):
            self.closed = True

    monkeypatch.setattr(approval_module, "ConfigStore", FakeStore)


def test_register_unregister_callback_and_dynamic_items():
    callback = AsyncMock()
    register_approval_callback("boost", callback)
    assert approval_module._registry["boost"] is callback

    unregister_approval_callback("boost")
    assert "boost" not in approval_module._registry

    bot = SimpleNamespace(add_dynamic_items=Mock())
    add_dynamic_items(bot)
    bot.add_dynamic_items.assert_called_once_with(_ApproveButton, _DenyButton)


def test_approval_role_gate_rejects_invalid_contexts(monkeypatch):
    monkeypatch.setattr(approval_module.discord, "Member", FakeMember)

    interaction = make_interaction(guild=None)
    assert asyncio.run(_require_approval_role(interaction, 1)) is False
    interaction.response.send_message.assert_awaited_once_with(
        "This approval can only be processed inside its original server.",
        ephemeral=True,
    )

    guild = SimpleNamespace(id=1, get_role=lambda role_id: None)
    interaction = make_interaction(guild=guild, user=SimpleNamespace(id=2))
    assert asyncio.run(_require_approval_role(interaction, 1)) is False
    interaction.response.send_message.assert_awaited_once_with(
        "Could not read your server member data.",
        ephemeral=True,
    )

    install_config_store(monkeypatch, raises=True)
    interaction = make_interaction(guild=guild)
    assert asyncio.run(_require_approval_role(interaction, 1)) is False
    interaction.response.send_message.assert_awaited_once_with(
        "This server is missing Amadeus configuration.",
        ephemeral=True,
    )


def test_approval_role_gate_checks_channel_role_and_success(monkeypatch):
    monkeypatch.setattr(approval_module.discord, "Member", FakeMember)
    role = SimpleNamespace(id=99)
    guild = SimpleNamespace(id=1, get_role=lambda role_id: role if role_id == 99 else None)

    config = GuildConfig(guild_id=1, owner_id=2, admin_role_id=99, alert_channel_id=10)
    install_config_store(monkeypatch, config=config)

    wrong_channel = make_interaction(guild=guild, user=FakeMember(2, [role]), channel_id=11)
    assert asyncio.run(_require_approval_role(wrong_channel, 1)) is False
    wrong_channel.response.send_message.assert_awaited_once_with(
        "This approval can only be processed from the configured admin alert channel.",
        ephemeral=True,
    )

    missing_admin_config = GuildConfig(guild_id=1, owner_id=2, admin_role_id=None, alert_channel_id=10)
    install_config_store(monkeypatch, config=missing_admin_config)
    interaction = make_interaction(guild=guild)
    assert asyncio.run(_require_approval_role(interaction, 1)) is False
    interaction.response.send_message.assert_awaited_once_with(
        "Approvals require an Amadeus admin role. Run `/amadeus set-admin-role` first.",
        ephemeral=True,
    )

    install_config_store(monkeypatch, config=config)
    no_role = make_interaction(guild=guild, user=FakeMember(2, []), channel_id=10)
    assert asyncio.run(_require_approval_role(no_role, 1)) is False
    no_role.response.send_message.assert_awaited_once_with(
        "You need the configured Amadeus admin role to process approvals.",
        ephemeral=True,
    )

    allowed = make_interaction(guild=guild, user=FakeMember(2, [role]), channel_id=10)
    assert asyncio.run(_require_approval_role(allowed, 1)) is True
    allowed.response.send_message.assert_not_awaited()


def test_post_approval_request_handles_missing_config_channel_and_success(monkeypatch):
    embed = discord.Embed(title="Review")

    store = SimpleNamespace(get_guild_config=lambda guild_id: (_ for _ in ()).throw(RuntimeError("missing")))
    assert asyncio.run(post_approval_request(SimpleNamespace(), store, 1, embed, "boost", 2, "abcDEF1234567890")) is False

    store = SimpleNamespace(
        get_guild_config=lambda guild_id: GuildConfig(
            guild_id=guild_id,
            owner_id=2,
            alert_channel_id=None,
        )
    )
    assert asyncio.run(post_approval_request(SimpleNamespace(), store, 1, embed, "boost", 2, "abcDEF1234567890")) is False

    store = SimpleNamespace(
        get_guild_config=lambda guild_id: GuildConfig(
            guild_id=guild_id,
            owner_id=2,
            alert_channel_id=10,
        )
    )
    monkeypatch.setattr(approval_module.discord, "TextChannel", FakeTextChannel)
    assert asyncio.run(
        post_approval_request(
            SimpleNamespace(get_channel=lambda channel_id: SimpleNamespace()),
            store,
            1,
            embed,
            "boost",
            2,
            "abcDEF1234567890",
        )
    ) is False

    channel = FakeTextChannel()
    assert asyncio.run(
        post_approval_request(
            SimpleNamespace(get_channel=lambda channel_id: channel),
            store,
            1,
            embed,
            "boost",
            2,
            "abcDEF1234567890",
            extra_embeds=[discord.Embed(title="Preview")],
            files=[],
        )
    ) is True
    channel.send.assert_awaited_once()
    assert len(channel.send.await_args.kwargs["embeds"]) == 2
    assert isinstance(channel.send.await_args.kwargs["view"], ApprovalView)


def test_dynamic_buttons_parse_custom_ids_and_open_modal(monkeypatch):
    monkeypatch.setattr(approval_module, "_require_approval_role", AsyncMock(return_value=True))
    interaction = make_interaction(guild=SimpleNamespace(id=1))

    approve = asyncio.run(
        _ApproveButton.from_custom_id(
            interaction,
            SimpleNamespace(custom_id="amadeus_approval:approve:boost:1:2:abcDEF1234567890"),
            None,
        )
    )
    assert approve.flow_type == "boost"
    assert approve.guild_id == 1
    assert approve.user_id == 2
    assert approve.request_id == "abcDEF1234567890"

    asyncio.run(approve.callback(interaction))

    interaction.response.send_modal.assert_awaited_once()
    modal = interaction.response.send_modal.await_args.args[0]
    assert isinstance(modal, _CommentModal)
    assert modal.approved is True

    deny = asyncio.run(
        _DenyButton.from_custom_id(
            interaction,
            SimpleNamespace(custom_id="amadeus_approval:deny:boost:1:2:abcDEF1234567890"),
            None,
        )
    )
    assert deny.request_id == "abcDEF1234567890"


def test_comment_modal_submits_decision_and_handles_errors(monkeypatch):
    monkeypatch.setattr(approval_module, "_require_approval_role", AsyncMock(return_value=True))

    callback = AsyncMock()
    register_approval_callback("boost", callback)
    try:
        approval_message = SimpleNamespace(edit=AsyncMock())
        modal = _CommentModal(
            approved=True,
            flow_type="boost",
            guild_id=1,
            user_id=2,
            request_id="abcDEF1234567890",
            bot=SimpleNamespace(name="bot"),
            approval_message=approval_message,
        )
        modal.comment._value = " approved "
        interaction = make_interaction(guild=SimpleNamespace(id=1), user=FakeMember(5))

        asyncio.run(modal.on_submit(interaction))

        interaction.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
        approval_message.edit.assert_awaited_once()
        callback.assert_awaited_once()
        assert callback.await_args.kwargs["approved"] is True
        assert callback.await_args.kwargs["comment"] == "approved"
    finally:
        unregister_approval_callback("boost")

    modal = _CommentModal(
        approved=False,
        flow_type="missing",
        guild_id=1,
        user_id=2,
        request_id="abcDEF1234567890",
        bot=SimpleNamespace(),
        approval_message=SimpleNamespace(edit=AsyncMock()),
    )
    modal.comment._value = ""
    interaction = make_interaction(guild=SimpleNamespace(id=1))
    asyncio.run(modal.on_submit(interaction))
    interaction.response.send_message.assert_awaited_once_with(
        "This approval type has no registered handler. The module may be unloaded.",
        ephemeral=True,
    )
