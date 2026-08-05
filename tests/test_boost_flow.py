import asyncio
import base64
import io
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
from PIL import Image

import cogs.boost as boost_module
from amadeus.models.boost import BoostGrant
from amadeus.models.dm_flow import DmFlow
from cogs.boost import FLOW_TYPE, Boost, S, _ConfirmButton, _PREVIEW_MIN_WIDTH


class FakeFlowStore:
    def __init__(self, flows=None):
        self.flows = flows or {}
        self.saved = []
        self.deleted = []

    def key(self, guild_id, user_id):
        return (guild_id, user_id, FLOW_TYPE)

    def save(self, flow):
        self.saved.append(flow)
        self.flows[self.key(flow.guild_id, flow.user_id)] = flow

    def get(self, guild_id, user_id, flow_type):
        return self.flows.get((guild_id, user_id, flow_type))

    def get_all_for_user(self, user_id):
        return [flow for (_, uid, _), flow in self.flows.items() if uid == user_id]

    def delete(self, guild_id, user_id, flow_type):
        self.deleted.append((guild_id, user_id, flow_type))
        self.flows.pop((guild_id, user_id, flow_type), None)


class FakeBoostStore:
    def __init__(self):
        self.grants = {}
        self.subscription_count = None
        self.saved_grants = []
        self.deleted_grants = []

    def get_grant(self, guild_id, user_id):
        return self.grants.get((guild_id, user_id))

    def save_grant(self, grant):
        self.saved_grants.append(grant)
        self.grants[(grant.guild_id, grant.user_id)] = grant

    def delete_grant(self, guild_id, user_id):
        self.deleted_grants.append((guild_id, user_id))
        self.grants.pop((guild_id, user_id), None)

    def get_subscription_count(self, guild_id):
        return self.subscription_count

    def set_subscription_count(self, guild_id, count):
        self.subscription_count = count


class FakeResponse:
    def __init__(self):
        self.send_message = AsyncMock()


class FakeFollowup:
    def __init__(self):
        self.send = AsyncMock()


class FakeUser:
    def __init__(self, user_id=2):
        self.id = user_id
        self.bot = False
        self.mention = f"<@{user_id}>"

    def __str__(self):
        return f"user-{self.id}"


def make_cog(*, flow_store=None, boost_store=None, guild=None):
    cog = Boost.__new__(Boost)
    cog.bot = SimpleNamespace(
        guilds=[],
        get_guild=lambda guild_id: guild,
        fetch_user=AsyncMock(return_value=FakeUser()),
    )
    cog.flow_store = flow_store or FakeFlowStore()
    cog.boost_store = boost_store or FakeBoostStore()
    cog.module_store = SimpleNamespace(is_module_enabled=lambda guild_id, module: True)
    return cog


def make_png_bytes(size=(12, 8)):
    output = io.BytesIO()
    Image.new("RGBA", size, (255, 0, 0, 255)).save(output, format="PNG")
    return output.getvalue()


def make_flow(state=S.ROLE_NAME, *, tier=1, data=None):
    merged = {"tier": tier}
    if data:
        merged.update(data)
    return DmFlow(1, 2, FLOW_TYPE, state, merged)


def make_message(content="", attachments=None, author=None):
    return SimpleNamespace(
        content=content,
        attachments=attachments or [],
        author=author or FakeUser(),
        channel=SimpleNamespace(),
    )


def test_confirm_button_routes_to_loaded_boost_cog_and_reports_missing_cog():
    button = asyncio.run(
        _ConfirmButton.from_custom_id(
            None,
            SimpleNamespace(custom_id="amadeus_boost:confirm:1:2"),
            None,
        )
    )
    assert button.guild_id == 1
    assert button.user_id == 2

    interaction = SimpleNamespace(
        client=SimpleNamespace(get_cog=lambda name: None),
        response=FakeResponse(),
    )
    asyncio.run(button.callback(interaction))
    interaction.response.send_message.assert_awaited_once_with(
        "The boost module is currently unavailable. Please try again later.",
        ephemeral=True,
    )

    cog = SimpleNamespace(handle_confirm=AsyncMock())
    interaction = SimpleNamespace(
        client=SimpleNamespace(get_cog=lambda name: cog),
        response=FakeResponse(),
    )
    asyncio.run(button.callback(interaction))
    cog.handle_confirm.assert_awaited_once_with(interaction, 1, 2)


def test_start_and_reset_flow_save_state_and_send_prompts(monkeypatch):
    safe_dm = AsyncMock()
    monkeypatch.setattr(boost_module, "safe_dm", safe_dm)
    guild = SimpleNamespace(id=1, name="Guild")
    member = FakeUser()
    cog = make_cog()
    cog._send_prompt = AsyncMock()

    asyncio.run(cog._start_flow(guild, member, tier=2, forced=True))

    flow = cog.flow_store.saved[0]
    assert flow.state == S.ROLE_NAME
    assert flow.data == {"tier": 2, "forced": True}
    safe_dm.assert_awaited_once()
    cog._send_prompt.assert_awaited_once_with(member, flow)

    reset = asyncio.run(cog._reset_flow(1, 2, 1))
    assert reset.state == S.ROLE_NAME
    assert reset.data == {"tier": 1}


def test_role_and_emoji_step_handlers_validate_and_advance(monkeypatch):
    safe_dm = AsyncMock()
    monkeypatch.setattr(boost_module, "safe_dm", safe_dm)
    cog = make_cog()

    flow = make_flow(S.ROLE_NAME)
    asyncio.run(cog._step_role_name(flow, make_message(" Favorite Role ")))
    assert flow.data["role_name"] == "Favorite Role"
    assert flow.state == S.ROLE_IMAGE

    empty = make_flow(S.ROLE_NAME)
    asyncio.run(cog._step_role_name(empty, make_message("  ")))
    assert safe_dm.await_args.kwargs["content"] == "Please send a name for your role."

    cog._advance_past_role = AsyncMock()
    flow = make_flow(S.ROLE_COLOR, tier=2)
    asyncio.run(cog._step_role_color(flow, make_message("a3c2ff")))
    assert flow.data["role_color_hex"] == "#A3C2FF"
    cog._advance_past_role.assert_awaited_once()

    flow = make_flow(S.EMOJI_1_NAME)
    asyncio.run(cog._step_emoji_name(flow, make_message("okabe_1")))
    assert flow.data["emoji_1_name"] == "okabe_1"
    assert flow.state == S.EMOJI_1_IMAGE

    bad = make_flow(S.EMOJI_1_NAME)
    asyncio.run(cog._step_emoji_name(bad, make_message("!")))
    assert "Emoji names must be" in safe_dm.await_args.kwargs["content"]


def test_image_step_handlers_store_downloaded_images_and_advance(monkeypatch):
    safe_dm = AsyncMock()
    monkeypatch.setattr(boost_module, "safe_dm", safe_dm)
    image = make_png_bytes()
    attachment = SimpleNamespace(
        content_type="image/png",
        url="https://cdn.discordapp.com/attachments/1/2/icon.png",
        size=len(image),
    )

    cog = make_cog()
    cog._download_attachment = AsyncMock(return_value=image)
    cog._advance_past_role = AsyncMock()

    flow = make_flow(S.ROLE_IMAGE, tier=1)
    asyncio.run(cog._step_role_image(flow, make_message(attachments=[attachment])))
    assert base64.b64decode(flow.data["role_image_b64"]) == image
    assert flow.data["role_image_ext"] == ".png"
    cog._advance_past_role.assert_awaited_once()

    cog._send_confirmation = AsyncMock()
    flow = make_flow(S.EMOJI_1_IMAGE, tier=1, data={"emoji_1_name": "okabe"})
    asyncio.run(cog._step_emoji_image(flow, make_message(attachments=[attachment])))
    assert base64.b64decode(flow.data["emoji_1_b64"]) == image
    cog._send_confirmation.assert_awaited_once()

    flow = make_flow(S.EMOJI_2_IMAGE, tier=2, data={"emoji_2_name": "kurisu"})
    asyncio.run(cog._step_emoji_2_image(flow, make_message(attachments=[])))
    assert safe_dm.await_args.kwargs["content"] == "Please upload an image file (PNG, JPEG, GIF, or WebP)."


def test_advance_past_role_skips_emojis_when_slots_are_insufficient(monkeypatch):
    safe_dm = AsyncMock()
    monkeypatch.setattr(boost_module, "safe_dm", safe_dm)
    monkeypatch.setattr(boost_module, "check_emoji_slots", lambda guild, needed: (False, 0, 1))
    guild = SimpleNamespace(id=1)
    cog = make_cog(guild=guild)
    cog._send_confirmation = AsyncMock()
    flow = make_flow(S.ROLE_COLOR, tier=2)

    asyncio.run(cog._advance_past_role(flow, FakeUser()))

    assert flow.data["emoji_skipped"] is True
    cog._send_confirmation.assert_awaited_once()


def test_dm_message_routes_commands_and_active_flow(monkeypatch):
    class FakeDMChannel:
        pass

    monkeypatch.setattr(boost_module.discord, "DMChannel", FakeDMChannel)
    safe_dm = AsyncMock()
    monkeypatch.setattr(boost_module, "safe_dm", safe_dm)

    flow = make_flow(S.ROLE_NAME, data={"forced": True})
    store = FakeFlowStore({(1, 2, FLOW_TYPE): flow})
    cog = make_cog(flow_store=store)
    cog._step_role_name = AsyncMock()

    message = make_message("My Role", author=FakeUser())
    message.channel = FakeDMChannel()
    asyncio.run(cog.on_message(message))
    cog._step_role_name.assert_awaited_once_with(flow, message)

    message = make_message("cancel", author=FakeUser())
    message.channel = FakeDMChannel()
    asyncio.run(cog.on_message(message))
    assert store.deleted[-1] == (1, 2, FLOW_TYPE)
    assert "cancelled" in safe_dm.await_args.kwargs["content"]


def test_start_self_service_flow_from_dm_starts_for_active_booster(monkeypatch):
    member = FakeUser()
    member.guild = SimpleNamespace(id=1)
    guild = SimpleNamespace(
        id=1,
        name="Guild",
        premium_subscription_count=2,
        get_member=lambda user_id: member,
    )
    boost_store = FakeBoostStore()
    boost_store.subscription_count = 1
    cog = make_cog(boost_store=boost_store)
    cog.bot.guilds = [guild]
    cog._start_flow = AsyncMock()
    monkeypatch.setattr(boost_module, "is_active_booster", lambda member: True)
    monkeypatch.setattr(boost_module, "get_proposed_tier", lambda previous, current: 2)

    assert asyncio.run(cog._start_self_service_flow_from_dm(FakeUser())) is True
    cog._start_flow.assert_awaited_once_with(guild, member, 2)


def test_confirmation_and_confirm_handler_submit_approval(monkeypatch):
    safe_dm = AsyncMock()
    monkeypatch.setattr(boost_module, "safe_dm", safe_dm)
    monkeypatch.setattr(boost_module.secrets, "token_urlsafe", lambda length: "request123456789")
    post = AsyncMock(return_value=True)
    monkeypatch.setattr(boost_module, "post_approval_request", post)
    guild = SimpleNamespace(id=1, name="Guild", premium_tier=2)
    flow = make_flow(
        S.CONFIRMATION,
        tier=1,
        data={
            "role_name": "Role",
            "forced": True,
            "role_image_b64": base64.b64encode(make_png_bytes()).decode(),
            "role_image_ext": ".png",
        },
    )
    store = FakeFlowStore({(1, 2, FLOW_TYPE): flow})
    cog = make_cog(flow_store=store, guild=guild)

    asyncio.run(cog._send_confirmation(FakeUser(), flow))
    assert flow.state == S.CONFIRMATION
    assert safe_dm.await_args.kwargs["view"] is not None
    assert safe_dm.await_args.kwargs["embeds"][0].title == "Review Your Request"

    interaction = SimpleNamespace(
        user=FakeUser(),
        response=FakeResponse(),
    )
    asyncio.run(cog.handle_confirm(interaction, 1, 2))

    assert flow.state == S.PENDING
    assert flow.data["approval_request_id"] == "request123456789"
    post.assert_awaited_once()
    interaction.response.send_message.assert_awaited_once()


def test_confirm_handler_rejects_wrong_user_expired_and_post_failure(monkeypatch):
    post = AsyncMock(return_value=False)
    monkeypatch.setattr(boost_module, "post_approval_request", post)
    flow = make_flow(S.CONFIRMATION)
    store = FakeFlowStore({(1, 2, FLOW_TYPE): flow})
    cog = make_cog(flow_store=store)

    wrong_user = SimpleNamespace(user=FakeUser(99), response=FakeResponse())
    asyncio.run(cog.handle_confirm(wrong_user, 1, 2))
    wrong_user.response.send_message.assert_awaited_once_with("This button isn't for you.", ephemeral=True)

    expired = SimpleNamespace(user=FakeUser(), response=FakeResponse())
    asyncio.run(make_cog(flow_store=FakeFlowStore()).handle_confirm(expired, 1, 2))
    assert "expired" in expired.response.send_message.await_args.args[0]

    interaction = SimpleNamespace(user=FakeUser(), response=FakeResponse())
    asyncio.run(cog.handle_confirm(interaction, 1, 2))
    assert flow.state == S.CONFIRMATION
    assert "Could not reach" in interaction.response.send_message.await_args.args[0]


def test_handle_restart_resets_flow_and_sends_prompt():
    flow = make_flow(S.DENIED, tier=2, data={"forced": True})
    store = FakeFlowStore({(1, 2, FLOW_TYPE): flow})
    cog = make_cog(flow_store=store)
    cog._send_prompt = AsyncMock()
    interaction = SimpleNamespace(user=FakeUser(), response=FakeResponse())

    asyncio.run(cog.handle_restart(interaction, 1, 2))

    interaction.response.send_message.assert_awaited_once_with("Starting over!", ephemeral=True)
    restarted = cog._send_prompt.await_args.args[1]
    assert restarted.data == {"tier": 2, "forced": True}


def test_handle_approval_denies_and_approves(monkeypatch):
    safe_dm = AsyncMock()
    monkeypatch.setattr(boost_module, "safe_dm", safe_dm)

    flow = make_flow(S.PENDING, data={"approval_request_id": "request123456789"})
    store = FakeFlowStore({(1, 2, FLOW_TYPE): flow})
    cog = make_cog(flow_store=store)
    interaction = SimpleNamespace(followup=FakeFollowup())

    asyncio.run(
        cog._handle_approval(
            interaction=interaction,
            bot=cog.bot,
            guild_id=1,
            user_id=2,
            request_id="request123456789",
            approved=False,
            comment=None,
        )
    )
    assert flow.state == S.DENIED
    interaction.followup.send.assert_awaited_once()

    guild = SimpleNamespace(
        id=1,
        name="Guild",
        fetch_member=AsyncMock(return_value=FakeUser()),
    )
    flow = make_flow(S.PENDING, data={"approval_request_id": "request123456789"})
    store = FakeFlowStore({(1, 2, FLOW_TYPE): flow})
    boost_store = FakeBoostStore()
    cog = make_cog(flow_store=store, boost_store=boost_store, guild=guild)
    cog._apply_grant = AsyncMock(return_value=BoostGrant(1, 2, 1, role_id=10))
    monkeypatch.setattr(boost_module, "is_active_booster", lambda member: True)
    interaction = SimpleNamespace(followup=FakeFollowup())

    asyncio.run(
        cog._handle_approval(
            interaction=interaction,
            bot=cog.bot,
            guild_id=1,
            user_id=2,
            request_id="request123456789",
            approved=True,
            comment="note",
        )
    )
    assert boost_store.saved_grants[0].role_id == 10
    assert store.deleted == [(1, 2, FLOW_TYPE)]
    assert "Grant applied" in interaction.followup.send.await_args.args[0]


def test_apply_grant_creates_role_icon_and_emojis():
    role = SimpleNamespace(id=10, edit=AsyncMock())
    guild = SimpleNamespace(
        id=1,
        premium_tier=2,
        create_role=AsyncMock(return_value=role),
        create_custom_emoji=AsyncMock(
            side_effect=[
                SimpleNamespace(id=20),
                SimpleNamespace(id=30),
            ]
        ),
    )
    member = SimpleNamespace(id=2, add_roles=AsyncMock())
    member.__str__ = lambda self=member: "member-2"
    image_b64 = base64.b64encode(b"image").decode()
    flow = make_flow(
        S.PROCESSING,
        tier=2,
        data={
            "role_name": "Favorite Character Role That Is Too Long",
            "role_color_hex": "#12ABEF",
            "role_image_b64": image_b64,
            "emoji_1_name": "okabe",
            "emoji_1_b64": image_b64,
            "emoji_2_name": "kurisu",
            "emoji_2_b64": image_b64,
        },
    )

    grant = asyncio.run(make_cog()._apply_grant(guild, member, flow))

    assert grant == BoostGrant(1, 2, 2, role_id=10, emoji_1_id=20, emoji_2_id=30)
    assert guild.create_role.await_args.kwargs["name"] == "Favorite Character Role That Is "
    role.edit.assert_awaited_once_with(icon=b"image", reason="Boost perk role icon")
    member.add_roles.assert_awaited_once_with(role, reason="Boost perk role assignment")


def test_teardown_grant_deletes_role_emojis_and_store_record():
    role = SimpleNamespace(id=10, delete=AsyncMock())
    emoji_1 = SimpleNamespace(id=20, delete=AsyncMock())
    emoji_2 = SimpleNamespace(id=30, delete=AsyncMock())
    guild = SimpleNamespace(
        id=1,
        get_role=lambda role_id: role if role_id == 10 else None,
        emojis=[emoji_1, emoji_2],
    )
    boost_store = FakeBoostStore()
    cog = make_cog(boost_store=boost_store)
    grant = BoostGrant(1, 2, 2, role_id=10, emoji_1_id=20, emoji_2_id=30)

    asyncio.run(cog._teardown_grant(guild, grant))

    role.delete.assert_awaited_once()
    emoji_1.delete.assert_awaited_once()
    emoji_2.delete.assert_awaited_once()
    assert boost_store.deleted_grants == [(1, 2)]


def test_preview_payload_and_padding_for_static_images():
    image = make_png_bytes(size=(8, 6))
    padded, ext = Boost._pad_preview_image(image, ".png")

    assert ext == ".png"
    with Image.open(io.BytesIO(padded)) as result:
        assert result.size == (_PREVIEW_MIN_WIDTH, 6)

    data = {
        "role_name": "Role",
        "tier": 2,
        "role_color_hex": "#12ABEF",
        "role_image_b64": base64.b64encode(image).decode(),
        "role_image_ext": ".png",
        "emoji_1_name": "okabe",
        "emoji_1_b64": base64.b64encode(image).decode(),
        "emoji_1_ext": ".png",
    }
    embeds, files = make_cog()._build_preview_payload(
        DmFlow(1, 2, FLOW_TYPE, S.CONFIRMATION, data),
        discord.Embed(title="Base"),
    )

    assert [embed.title for embed in embeds] == ["Base", "Role Preview", "Emoji 1 Preview"]
    assert [file.filename for file in files] == ["role_icon.png", "emoji_1.png"]
    assert Boost._image_file_from_flow({"role_image_b64": "not base64"}, "role_image", "role") is None


def test_build_prompt_and_approval_embeds_and_attachment_helpers():
    cog = make_cog()
    assert cog._build_prompt_embed(make_flow(S.ROLE_NAME)).title == "Step 1/4 — Role Name"
    assert cog._build_prompt_embed(make_flow("unknown")).title == "Unknown step"

    flow = make_flow(S.CONFIRMATION, data={"role_image_b64": base64.b64encode(b"x").decode()})
    guild = SimpleNamespace(name="Guild", premium_tier=1)
    embed = cog._build_approval_embed(flow, guild)
    fields = {field.name: field.value for field in embed.fields}
    assert "Role icons require" in fields["⚠ Role Icon"]

    image_attachment = SimpleNamespace(content_type="image/png")
    text_attachment = SimpleNamespace(content_type="text/plain")
    assert Boost._first_image_attachment(SimpleNamespace(attachments=[text_attachment, image_attachment])) is image_attachment
    assert Boost._first_image_attachment(SimpleNamespace(attachments=[text_attachment])) is None
