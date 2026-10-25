import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord
from PIL import Image

from amadeus.models.dm_flow import DmFlow
import cogs.boost as boost_module
from cogs.boost import Boost, S, _EmojiModal, _RoleModal


def test_boost_flow_accent_color_uses_tier_two_color():
    flow = DmFlow(
        guild_id=1,
        user_id=2,
        flow_type="boost",
        state="DONE",
        data={"tier": 2, "role_color_hex": "#12abef"},
    )
    fallback = discord.Color.green()

    assert Boost._flow_accent_color(flow, fallback).value == 0x12ABEF


def test_boost_flow_accent_color_falls_back_for_invalid_or_non_tier_two_color():
    fallback = discord.Color.green()
    invalid = DmFlow(1, 2, "boost", "DONE", {"tier": 2, "role_color_hex": "nope"})
    tier_one = DmFlow(1, 2, "boost", "DONE", {"tier": 1, "role_color_hex": "#12abef"})

    assert Boost._flow_accent_color(invalid, fallback) is fallback
    assert Boost._flow_accent_color(tier_one, fallback) is fallback


def test_boost_tier_perks_text_varies_by_tier():
    tier_one = Boost._tier_perks_text(1)
    tier_two = Boost._tier_perks_text(2)

    assert "1 sciadv-related emote" in tier_one
    assert "2 sciadv-related emotes" in tier_two
    assert "Color cannot clash" in tier_two


def test_boost_setup_dashboard_has_one_button_per_unlocked_item():
    cog = Boost.__new__(Boost)
    tier_one = DmFlow(1, 2, "boost", S.COLLECTING, {"tier": 1})
    tier_two = DmFlow(1, 2, "boost", S.COLLECTING, {"tier": 2})

    tier_one_labels = [item.item.label for item in cog._build_setup_view(tier_one).children]
    tier_two_labels = [item.item.label for item in cog._build_setup_view(tier_two).children]

    assert tier_one_labels == ["Custom Role", "Emoji 1"]
    assert tier_two_labels == ["Custom Role", "Emoji 1", "Emoji 2"]


def test_boost_setup_dashboard_marks_completed_items():
    cog = Boost.__new__(Boost)
    flow = DmFlow(
        1,
        2,
        "boost",
        S.COLLECTING,
        {
            "tier": 1,
            "role_name": "Future Gadget Lab",
            "role_image_b64": "aW1hZ2U=",
        },
    )

    labels = [item.item.label for item in cog._build_setup_view(flow).children]

    assert labels == ["✓ Custom Role", "Emoji 1"]
    assert Boost._collection_complete(flow) is False


def test_role_modals_match_tier_fields_and_use_file_uploads():
    cog = Boost.__new__(Boost)
    tier_one = _RoleModal(cog, DmFlow(1, 2, "boost", S.COLLECTING, {"tier": 1}))
    tier_two = _RoleModal(cog, DmFlow(1, 2, "boost", S.COLLECTING, {"tier": 2}))

    tier_one_components = tier_one.to_dict()["components"]
    tier_two_components = tier_two.to_dict()["components"]

    assert [component["type"] for component in tier_one_components] == [10, 18, 18]
    assert tier_one_components[-1]["component"]["type"] == 19
    assert [component["label"] for component in tier_two_components if component["type"] == 18] == [
        "Custom Role",
        "Custom Role Color",
        "Role Icon",
    ]


def test_emoji_modal_collects_name_and_image_together():
    cog = Boost.__new__(Boost)
    modal = _EmojiModal(cog, DmFlow(1, 2, "boost", S.COLLECTING, {"tier": 2}), 2)
    components = modal.to_dict()["components"]

    assert [component["type"] for component in components] == [10, 18, 18]
    assert components[1]["component"]["type"] == 4
    assert components[2]["component"]["type"] == 19


def test_boost_confirmation_is_one_names_only_embed(monkeypatch):
    cog = Boost.__new__(Boost)
    cog.bot = SimpleNamespace(get_guild=lambda guild_id: SimpleNamespace(name="Steins;Gate"))
    cog.flow_store = SimpleNamespace(save=lambda flow: None)
    flow = DmFlow(
        1,
        2,
        "boost",
        S.COLLECTING,
        {
            "tier": 2,
            "role_name": "Future Gadget Lab",
            "role_image_b64": "cm9sZQ==",
            "emoji_1_name": "kurisu",
            "emoji_1_b64": "ZW1vamkx",
            "emoji_2_name": "mayuri",
            "emoji_2_b64": "ZW1vamky",
        },
    )
    send_dm = AsyncMock(return_value=True)
    monkeypatch.setattr(boost_module, "safe_dm", send_dm)

    asyncio.run(cog._send_confirmation(SimpleNamespace(id=2), flow))

    kwargs = send_dm.await_args.kwargs
    assert "embeds" not in kwargs
    assert "files" not in kwargs
    embed = kwargs["embed"]
    assert len(embed.fields) == 0
    assert embed.image.url is None
    assert "Future Gadget Lab" in embed.description
    assert "kurisu" in embed.description
    assert "mayuri" in embed.description
    assert "cm9sZQ==" not in embed.description
    assert [item.item.label for item in kwargs["view"].children] == [
        "Submit for Review",
        "Start Over",
    ]


def test_modal_success_only_updates_dashboard():
    cog = Boost.__new__(Boost)
    cog.bot = SimpleNamespace(get_guild=lambda guild_id: SimpleNamespace(name="Steins;Gate"))
    cog.flow_store = SimpleNamespace(save=lambda flow: None)
    flow = DmFlow(
        1,
        2,
        "boost",
        S.COLLECTING,
        {
            "tier": 1,
            "role_name": "Future Gadget Lab",
            "role_image_b64": "cm9sZQ==",
        },
    )
    interaction = SimpleNamespace(
        edit_original_response=AsyncMock(),
        followup=SimpleNamespace(send=AsyncMock()),
    )

    asyncio.run(cog._finish_modal_submission(interaction, flow))

    interaction.edit_original_response.assert_awaited_once()
    interaction.followup.send.assert_not_awaited()


def test_preview_frames_are_padded_to_full_embed_width():
    frame = Image.new("RGBA", (32, 32), (255, 0, 0, 255))

    padded = Boost._pad_preview_frame(frame, Image)

    assert padded.size == (520, 32)
    assert padded.getpixel((260, 16)) == (255, 0, 0, 255)
    assert padded.getpixel((0, 0)) == (0, 0, 0, 0)


def test_boost_capacity_requires_role_emoji_and_role_icon_space():
    full_roles = SimpleNamespace(id=1, roles=[object()] * 250)
    assert "role limit" in Boost._boost_capacity_error(full_roles, 1)

    full_emojis = SimpleNamespace(
        id=1,
        roles=[],
        emojis=[SimpleNamespace(managed=False)],
        emoji_limit=1,
        premium_tier=2,
    )
    assert "free emoji" in Boost._boost_capacity_error(full_emojis, 1)

    no_role_icons = SimpleNamespace(
        id=1,
        roles=[],
        emojis=[],
        emoji_limit=5,
        premium_tier=1,
    )
    assert "Boost Level 2" in Boost._boost_capacity_error(no_role_icons, 1)


def test_start_flow_capacity_failure_alerts_admin_without_saving(monkeypatch):
    cog = Boost.__new__(Boost)
    cog.bot = SimpleNamespace()
    cog.module_store = SimpleNamespace()
    cog.flow_store = SimpleNamespace(save=Mock())
    guild = SimpleNamespace(id=1, roles=[object()] * 250)
    member = SimpleNamespace(id=2)
    alert = AsyncMock(return_value=True)
    monkeypatch.setattr(boost_module, "send_alert", alert)

    started = asyncio.run(cog._start_flow(guild, member, 1))

    assert started is False
    cog.flow_store.save.assert_not_called()
    assert "<@2>" in alert.await_args.args[3]


def test_confirmation_dm_failure_keeps_resumable_state_and_alerts(monkeypatch):
    cog = Boost.__new__(Boost)
    cog.bot = SimpleNamespace(get_guild=lambda guild_id: SimpleNamespace(name="Steins;Gate"))
    cog.module_store = SimpleNamespace()
    cog.flow_store = SimpleNamespace(save=lambda flow: None)
    flow = DmFlow(
        1,
        2,
        "boost",
        S.COLLECTING,
        {
            "tier": 1,
            "role_name": "Lab",
            "role_image_b64": "cm9sZQ==",
            "emoji_1_name": "kurisu",
            "emoji_1_b64": "ZW1vamk=",
        },
    )
    monkeypatch.setattr(boost_module, "safe_dm", AsyncMock(return_value=False))
    alert = AsyncMock(return_value=True)
    monkeypatch.setattr(boost_module, "send_alert", alert)

    asyncio.run(cog._send_confirmation(SimpleNamespace(id=2), flow))

    assert flow.state == S.COLLECTING
    alert.assert_awaited_once()


def test_role_assignment_failure_rolls_back_new_grant():
    response = SimpleNamespace(status=403, reason="Forbidden")
    role = SimpleNamespace(id=10, edit=AsyncMock())
    guild = SimpleNamespace(
        id=1,
        premium_tier=2,
        create_role=AsyncMock(return_value=role),
    )
    member = SimpleNamespace(
        id=2,
        add_roles=AsyncMock(side_effect=discord.Forbidden(response, "no permission")),
    )
    flow = DmFlow(
        1,
        2,
        "boost",
        S.PROCESSING,
        {
            "tier": 1,
            "role_name": "Lab",
            "role_image_b64": "cm9sZQ==",
            "emoji_1_name": "kurisu",
            "emoji_1_b64": "ZW1vamk=",
        },
    )
    cog = Boost.__new__(Boost)
    cog._rollback_uncommitted_grant = AsyncMock()

    grant = asyncio.run(cog._apply_grant(guild, member, flow))

    assert grant is None
    role.edit.assert_awaited_once_with(
        display_icon=b"role",
        reason="Boost perk role icon",
    )
    cog._rollback_uncommitted_grant.assert_awaited_once()


def test_failed_teardown_retains_grant_and_alerts(monkeypatch):
    cog = Boost.__new__(Boost)
    cog.bot = SimpleNamespace()
    cog.module_store = SimpleNamespace()
    cog._delete_grant_resources = AsyncMock(return_value=False)
    cog.boost_store = SimpleNamespace(delete_grant=Mock())
    alert = AsyncMock(return_value=True)
    monkeypatch.setattr(boost_module, "send_alert", alert)
    grant = SimpleNamespace(guild_id=1, user_id=2, tier=1)

    removed = asyncio.run(cog._teardown_grant(SimpleNamespace(id=1), grant))

    assert removed is False
    cog.boost_store.delete_grant.assert_not_called()
    alert.assert_awaited_once()


def test_denial_dm_includes_admin_reason(monkeypatch):
    flow = DmFlow(
        1,
        2,
        "boost",
        S.PENDING,
        {"tier": 1, "approval_request_id": "request-123456789"},
    )
    flow_store = SimpleNamespace(
        get=lambda guild_id, user_id, flow_type: flow,
        save=lambda updated: None,
    )
    user = SimpleNamespace(id=2)
    cog = Boost.__new__(Boost)
    cog.flow_store = flow_store
    cog.bot = SimpleNamespace(
        get_guild=lambda guild_id: SimpleNamespace(name="Steins;Gate"),
        fetch_user=AsyncMock(return_value=user),
    )
    send_dm = AsyncMock(return_value=True)
    monkeypatch.setattr(boost_module, "safe_dm", send_dm)

    result = asyncio.run(
        cog._handle_approval(
            interaction=SimpleNamespace(),
            bot=cog.bot,
            guild_id=1,
            user_id=2,
            request_id="request-123456789",
            approved=False,
            comment="Please rename it.",
        )
    )

    assert result.committed is True
    assert "Please rename it." in send_dm.await_args.kwargs["content"]
