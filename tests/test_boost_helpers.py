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


def test_boost_setup_dashboard_lists_perks_and_an_always_present_submit():
    cog = Boost.__new__(Boost)
    tier_one = DmFlow(1, 2, "boost", S.COLLECTING, {"tier": 1})
    tier_two = DmFlow(1, 2, "boost", S.COLLECTING, {"tier": 2})

    tier_one_labels = [item.item.label for item in cog._build_setup_view(tier_one).children]
    tier_two_labels = [item.item.label for item in cog._build_setup_view(tier_two).children]

    assert tier_one_labels == ["Custom Role", "Emoji 1", "Submit for Review", "Clear"]
    assert tier_two_labels == [
        "Custom Role",
        "Emoji 1",
        "Emoji 2",
        "Submit for Review",
        "Clear",
    ]


def test_clear_button_is_red():
    cog = Boost.__new__(Boost)
    flow = DmFlow(1, 2, "boost", S.COLLECTING, {"tier": 1})

    clear = next(
        item.item
        for item in cog._build_setup_view(flow).children
        if getattr(item.item, "label", None) == "Clear"
    )

    assert clear.style is discord.ButtonStyle.danger


def test_boost_setup_embed_ends_instructions_with_a_divider():
    cog = Boost.__new__(Boost)
    cog.bot = SimpleNamespace(get_guild=lambda guild_id: None)
    flow = DmFlow(1, 2, "boost", S.COLLECTING, {"tier": 1})

    embed = cog._build_setup_embed(flow)

    assert "Press **Submit for Review**" in embed.description
    assert embed.description.rstrip().endswith(boost_module._EMBED_DIVIDER)


def test_boost_setup_dashboard_marks_completed_items_and_offers_clear():
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

    assert labels == ["✓ Custom Role", "Emoji 1", "Submit for Review", "Clear"]
    assert Boost._has_selection(flow) is True
    assert Boost._selection_valid(flow) is True


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


def test_approval_embed_lists_only_requested_perks():
    cog = Boost.__new__(Boost)
    flow = DmFlow(
        1,
        2,
        "boost",
        S.PENDING,
        {
            "tier": 2,
            "role_name": "Future Gadget Lab",
            "role_image_b64": "cm9sZQ==",
            "role_color_hex": "#12ABEF",
            "emoji_1_name": "kurisu",
            "emoji_1_b64": "ZW1vamk=",
            # Emoji 2 intentionally skipped.
        },
    )

    embed = cog._build_approval_embed(flow, SimpleNamespace(name="Steins;Gate"))
    perks = next(field for field in embed.fields if field.name == "Requested Perks")

    assert "Custom Role" in perks.value
    assert "Emoji 1" in perks.value
    assert "Emoji 2" not in perks.value


def test_preview_payload_omits_skipped_perks():
    cog = Boost.__new__(Boost)
    flow = DmFlow(
        1,
        2,
        "boost",
        S.PENDING,
        {"tier": 2, "emoji_1_name": "kurisu", "emoji_1_b64": "ZW1vamk="},
    )

    embeds, files = cog._build_preview_payload(flow, discord.Embed(title="Request"))

    titles = [embed.title for embed in embeds]
    assert "Role Preview" not in titles
    assert "Emoji 1 Preview" in titles
    assert "Emoji 2 Preview" not in titles
    assert len(files) == 1


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


def _capacity_guild(**overrides):
    guild = SimpleNamespace(
        id=1,
        roles=[],
        emojis=[],
        emoji_limit=5,
        premium_tier=2,
    )
    for key, value in overrides.items():
        setattr(guild, key, value)
    return guild


def test_boost_request_capacity_checks_only_the_requested_items():
    cog = Boost.__new__(Boost)
    full_roles = _capacity_guild(roles=[object()] * 250)
    full_emojis = _capacity_guild(
        emojis=[SimpleNamespace(managed=False)], emoji_limit=1
    )
    below_level_two = _capacity_guild(premium_tier=1)

    role_only = {"tier": 2, "role_name": "Lab", "role_image_b64": "cm9sZQ==", "role_color_hex": "#FFFFFF"}
    emoji_only = {"tier": 1, "emoji_1_name": "kurisu", "emoji_1_b64": "ZW1vamk="}

    # A full role list does not block an emoji-only request.
    assert cog._request_capacity_error(full_roles, emoji_only) is None
    # A full emoji list does not block a role-only request.
    assert cog._request_capacity_error(full_emojis, role_only) is None
    # Below Boost Level 2 does not block an emoji-only request.
    assert cog._request_capacity_error(below_level_two, emoji_only) is None

    # The requested items are still validated.
    assert "role limit" in cog._request_capacity_error(full_roles, role_only)
    assert "free emoji" in cog._request_capacity_error(full_emojis, emoji_only)
    assert "Boost Level 2" in cog._request_capacity_error(below_level_two, role_only)


def test_start_flow_no_longer_blocks_on_capacity(monkeypatch):
    cog = Boost.__new__(Boost)
    cog.bot = SimpleNamespace(get_guild=lambda guild_id: None)
    cog.module_store = SimpleNamespace()
    cog.flow_store = SimpleNamespace(save=Mock())
    prompt = AsyncMock(return_value=True)
    monkeypatch.setattr(cog, "_send_prompt", prompt)
    # A guild at the role limit would previously have refused to start.
    guild = SimpleNamespace(id=1, roles=[object()] * 250)
    member = SimpleNamespace(id=2)

    started = asyncio.run(cog._start_flow(guild, member, 1))

    assert started is True
    cog.flow_store.save.assert_called_once()
    prompt.assert_awaited_once()


def test_partial_perk_is_not_submittable():
    role_without_icon = DmFlow(1, 2, "boost", S.COLLECTING, {"tier": 1, "role_name": "Lab"})
    emoji_without_image = DmFlow(
        1, 2, "boost", S.COLLECTING, {"tier": 1, "emoji_1_name": "kurisu"}
    )

    assert Boost._selection_valid(role_without_icon) is False
    assert Boost._selection_valid(emoji_without_image) is False
    assert Boost._has_selection(role_without_icon) is False


def test_handle_confirm_requires_at_least_one_perk():
    flow = DmFlow(1, 2, "boost", S.COLLECTING, {"tier": 1})
    cog = Boost.__new__(Boost)
    cog.flow_store = SimpleNamespace(get=lambda *args: flow, save=Mock())
    interaction = SimpleNamespace(
        user=SimpleNamespace(id=2),
        response=SimpleNamespace(send_message=AsyncMock()),
    )

    asyncio.run(cog.handle_confirm(interaction, 1, 2))

    interaction.response.send_message.assert_awaited_once()
    assert "at least one perk" in interaction.response.send_message.await_args.args[0].lower()
    assert flow.state == S.COLLECTING


def test_handle_confirm_submits_for_review(monkeypatch):
    flow = DmFlow(
        1,
        2,
        "boost",
        S.COLLECTING,
        {"tier": 1, "role_name": "Lab", "role_image_b64": "cm9sZQ=="},
    )
    cog = Boost.__new__(Boost)
    cog.bot = SimpleNamespace(get_guild=lambda guild_id: None)
    cog.module_store = SimpleNamespace()
    cog.flow_store = SimpleNamespace(get=lambda *args: flow, save=lambda updated: None)
    posted = AsyncMock(return_value=True)
    monkeypatch.setattr(boost_module, "post_approval_request", posted)
    message = SimpleNamespace(edit=AsyncMock())
    interaction = SimpleNamespace(
        user=SimpleNamespace(id=2),
        message=message,
        response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()),
    )

    asyncio.run(cog.handle_confirm(interaction, 1, 2))

    assert flow.state == S.PENDING
    posted.assert_awaited_once()
    interaction.response.defer.assert_awaited_once()
    interaction.followup.send.assert_awaited_once()
    assert "submitted" in interaction.followup.send.await_args.args[0].lower()
    # The submitted dashboard must stop showing usable buttons.
    message.edit.assert_awaited_once_with(view=None)


def test_handle_confirm_rejects_a_second_submission(monkeypatch):
    # A second click lands while the first is still posting (state PROCESSING).
    flow = DmFlow(
        1,
        2,
        "boost",
        S.PROCESSING,
        {"tier": 1, "role_name": "Lab", "role_image_b64": "cm9sZQ=="},
    )
    cog = Boost.__new__(Boost)
    cog.flow_store = SimpleNamespace(get=lambda *args: flow, save=Mock())
    posted = AsyncMock(return_value=True)
    monkeypatch.setattr(boost_module, "post_approval_request", posted)
    message = SimpleNamespace(edit=AsyncMock())
    interaction = SimpleNamespace(
        user=SimpleNamespace(id=2),
        message=message,
        response=SimpleNamespace(send_message=AsyncMock()),
    )

    asyncio.run(cog.handle_confirm(interaction, 1, 2))

    interaction.response.send_message.assert_awaited_once()
    posted.assert_not_awaited()
    message.edit.assert_awaited_once_with(view=None)


def test_clear_on_a_retired_dashboard_removes_its_buttons():
    flow = DmFlow(
        1,
        2,
        "boost",
        S.PENDING,
        {"tier": 1, "role_name": "Lab", "role_image_b64": "cm9sZQ=="},
    )
    cog = Boost.__new__(Boost)
    cog.bot = SimpleNamespace(get_guild=lambda guild_id: None)
    cog.flow_store = SimpleNamespace(get=lambda *args: flow, save=lambda updated: None)
    message = SimpleNamespace(edit=AsyncMock())
    interaction = SimpleNamespace(
        user=SimpleNamespace(id=2),
        message=message,
        response=SimpleNamespace(send_message=AsyncMock()),
    )

    asyncio.run(cog.handle_clear(interaction, 1, 2))

    interaction.response.send_message.assert_awaited_once()
    assert "no longer active" in interaction.response.send_message.await_args.args[0].lower()
    message.edit.assert_awaited_once_with(view=None)


def test_clear_button_wipes_all_perks():
    flow = DmFlow(
        1,
        2,
        "boost",
        S.COLLECTING,
        {
            "tier": 2,
            "role_name": "Lab",
            "role_image_b64": "cm9sZQ==",
            "role_color_hex": "#12ABEF",
            "emoji_1_name": "kurisu",
            "emoji_1_b64": "ZW1vamk=",
            "emoji_2_name": "mayuri",
            "emoji_2_b64": "ZW1vamky",
        },
    )
    cog = Boost.__new__(Boost)
    cog.bot = SimpleNamespace(get_guild=lambda guild_id: SimpleNamespace(name="Steins;Gate"))
    cog.flow_store = SimpleNamespace(get=lambda *args: flow, save=lambda updated: None)
    interaction = SimpleNamespace(
        user=SimpleNamespace(id=2),
        response=SimpleNamespace(edit_message=AsyncMock(), send_message=AsyncMock()),
    )

    asyncio.run(cog.handle_clear(interaction, 1, 2))

    assert Boost._has_selection(flow) is False
    for key in ("role_name", "role_image_b64", "role_color_hex", "emoji_1_name", "emoji_2_name"):
        assert key not in flow.data
    interaction.response.edit_message.assert_awaited_once()


def test_apply_grant_with_only_an_emoji_skips_the_role():
    cog = Boost.__new__(Boost)
    cog._rollback_uncommitted_grant = AsyncMock()
    guild = SimpleNamespace(
        id=1,
        roles=[],
        emojis=[],
        emoji_limit=5,
        premium_tier=2,
        create_role=AsyncMock(),
        create_custom_emoji=AsyncMock(return_value=SimpleNamespace(id=99)),
    )
    member = SimpleNamespace(id=2, add_roles=AsyncMock())
    flow = DmFlow(
        1, 2, "boost", S.PENDING,
        {"tier": 1, "emoji_1_name": "kurisu", "emoji_1_b64": "ZW1vamk="},
    )

    grant = asyncio.run(cog._apply_grant(guild, member, flow))

    assert grant is not None
    assert grant.role_id is None
    assert grant.emoji_1_id == 99
    guild.create_role.assert_not_awaited()
    member.add_roles.assert_not_awaited()


def test_apply_grant_with_only_a_role_skips_emojis():
    cog = Boost.__new__(Boost)
    cog._rollback_uncommitted_grant = AsyncMock()
    role = SimpleNamespace(id=10, edit=AsyncMock())
    guild = SimpleNamespace(
        id=1,
        roles=[],
        emojis=[],
        emoji_limit=5,
        premium_tier=2,
        create_role=AsyncMock(return_value=role),
        create_custom_emoji=AsyncMock(),
    )
    member = SimpleNamespace(id=2, add_roles=AsyncMock())
    flow = DmFlow(
        1, 2, "boost", S.PENDING,
        {"tier": 1, "role_name": "Lab", "role_image_b64": "cm9sZQ=="},
    )

    grant = asyncio.run(cog._apply_grant(guild, member, flow))

    assert grant is not None
    assert grant.role_id == 10
    assert grant.emoji_1_id is None
    guild.create_custom_emoji.assert_not_awaited()
    role.edit.assert_awaited_once()
    member.add_roles.assert_awaited_once_with(role, reason="Boost perk role assignment")


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
