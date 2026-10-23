import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord

import amadeus.approval as approval_module
from amadeus.approval import (
    ApprovalResult,
    _CommentModal,
    _decision_message_edit_options,
    register_approval_callback,
    unregister_approval_callback,
)
from amadeus.discord_utils import NO_MENTIONS


def test_decision_message_keeps_summary_and_removes_previews():
    summary = discord.Embed(title="Boost Perks Request")
    role_preview = discord.Embed(title="Role Preview")
    emoji_preview = discord.Embed(title="Emoji Preview")
    message = SimpleNamespace(
        embeds=[summary, role_preview, emoji_preview],
        attachments=[SimpleNamespace(id=1), SimpleNamespace(id=2)],
    )

    options = _decision_message_edit_options(
        message,
        approved=False,
        reviewer_mention="<@42>",
        comment="The role name needs work.",
    )

    assert options == {
        "content": "❌ Denied by <@42>\n**Reason:** The role name needs work.",
        "embeds": [summary],
        "attachments": [],
        "view": None,
        "allowed_mentions": NO_MENTIONS,
    }


def test_denied_decision_records_missing_reason():
    summary = discord.Embed(title="Boost Perks Request")
    message = SimpleNamespace(embeds=[summary], attachments=[])

    options = _decision_message_edit_options(
        message,
        approved=False,
        reviewer_mention="<@42>",
        comment=None,
    )

    assert options["content"] == "❌ Denied by <@42>\n**Reason:** No reason provided."


def test_approved_decision_does_not_add_reason():
    summary = discord.Embed(title="Boost Perks Request")
    message = SimpleNamespace(embeds=[summary], attachments=[])

    options = _decision_message_edit_options(
        message,
        approved=True,
        reviewer_mention="<@42>",
        comment="Looks good.",
    )

    assert options["content"] == "✅ Approved by <@42>"


def test_approval_message_is_finalized_only_after_callback_commits(monkeypatch):
    events = []

    async def callback(**kwargs):
        events.append("callback")
        return ApprovalResult(True, "done")

    async def edit(**kwargs):
        events.append("edit")

    register_approval_callback("test", callback)
    monkeypatch.setattr(
        approval_module, "_require_approval_role", AsyncMock(return_value=True)
    )
    message = SimpleNamespace(embeds=[discord.Embed(title="Request")], edit=edit)
    modal = _CommentModal(
        approved=True,
        flow_type="test",
        guild_id=1,
        user_id=2,
        request_id="request-123456789",
        bot=SimpleNamespace(),
        approval_message=message,
    )
    modal.comment._value = ""
    interaction = SimpleNamespace(
        user=SimpleNamespace(mention="<@3>"),
        response=SimpleNamespace(defer=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()),
    )
    try:
        asyncio.run(modal.on_submit(interaction))
    finally:
        unregister_approval_callback("test")

    assert events == ["callback", "edit"]


def test_failed_approval_keeps_buttons_and_previews(monkeypatch):
    async def callback(**kwargs):
        return ApprovalResult(False, "retry")

    register_approval_callback("test", callback)
    monkeypatch.setattr(
        approval_module, "_require_approval_role", AsyncMock(return_value=True)
    )
    message = SimpleNamespace(
        embeds=[discord.Embed(title="Request")], edit=AsyncMock()
    )
    modal = _CommentModal(
        approved=True,
        flow_type="test",
        guild_id=1,
        user_id=2,
        request_id="request-123456789",
        bot=SimpleNamespace(),
        approval_message=message,
    )
    modal.comment._value = ""
    interaction = SimpleNamespace(
        user=SimpleNamespace(mention="<@3>"),
        response=SimpleNamespace(defer=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()),
    )
    try:
        asyncio.run(modal.on_submit(interaction))
    finally:
        unregister_approval_callback("test")

    message.edit.assert_not_awaited()
    interaction.followup.send.assert_awaited_once_with("retry", ephemeral=True)
