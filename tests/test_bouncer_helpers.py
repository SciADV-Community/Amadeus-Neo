import asyncio
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord

import cogs.bouncer as bouncer_module
from amadeus.models.bouncer import BounceConfig
from cogs.bouncer import Bouncer, CaptchaChallenge


def make_bouncer():
    cog = Bouncer.__new__(Bouncer)
    cog.active_challenges = {}
    return cog


def test_bouncer_challenge_key_and_clear_challenge():
    cog = make_bouncer()
    cog.active_challenges[(1, 2)] = CaptchaChallenge(answer="ABC123")

    assert cog.challenge_key(1, 2) == (1, 2)
    cog.clear_challenge(1, 2)

    assert cog.active_challenges == {}


def test_bouncer_challenge_expiry_uses_configured_minutes():
    cog = make_bouncer()
    expired = CaptchaChallenge(
        answer="ABC123",
        created_at=discord.utils.utcnow() - timedelta(minutes=6),
    )
    fresh = CaptchaChallenge(
        answer="ABC123",
        created_at=discord.utils.utcnow() - timedelta(minutes=1),
    )

    assert cog._challenge_expired(expired, expiry_minutes=5)
    assert not cog._challenge_expired(fresh, expiry_minutes=5)


def test_bouncer_account_age_helpers():
    cog = make_bouncer()
    member = SimpleNamespace(created_at=discord.utils.utcnow() - timedelta(days=10, minutes=1))

    assert cog.account_age_days(member) == 10
    assert cog.account_is_old_enough(member, 10)
    assert not cog.account_is_old_enough(member, 11)


def test_bouncer_role_and_channel_lookup(monkeypatch):
    cog = make_bouncer()
    role = SimpleNamespace(id=10)

    class FakeTextChannel:
        pass

    channel = FakeTextChannel()
    monkeypatch.setattr(bouncer_module.discord, "TextChannel", FakeTextChannel)
    guild = SimpleNamespace(
        get_role=lambda role_id: role if role_id == 10 else None,
        get_channel=lambda channel_id: channel if channel_id == 20 else None,
    )
    config = BounceConfig(guild_id=1, verified_role_id=10, verification_channel_id=20)

    assert cog.get_verified_role(guild, config) is role
    assert cog.get_verification_channel(guild, config) is channel
    assert cog.get_verified_role(guild, BounceConfig(guild_id=1)) is None
    assert cog.get_verification_channel(guild, BounceConfig(guild_id=1)) is None


class FakeMember:
    def __init__(self, member_id=2, roles=None):
        self.id = member_id
        self.roles = roles or []
        self.created_at = discord.utils.utcnow() - timedelta(days=30)

    def __str__(self):
        return f"member-{self.id}"


class FakeCaptcha:
    def __init__(self):
        self.generated = []

    def make_captcha_text(self):
        return "ABC123"

    async def make_captcha_file(self, captcha_text):
        self.generated.append(captcha_text)
        return SimpleNamespace(filename="captcha.png")


def make_start_verification_cog(config, role):
    cog = make_bouncer()
    cog.captcha = FakeCaptcha()
    cog.get_ready_config = AsyncMock(return_value=(config, role, SimpleNamespace(id=20)))
    cog.require_verify_channel = AsyncMock(return_value=True)
    return cog


def make_interaction(member):
    return SimpleNamespace(
        user=member,
        guild=SimpleNamespace(id=1),
        guild_id=1,
        response=SimpleNamespace(send_message=AsyncMock()),
    )


def test_start_verification_sends_new_captcha(monkeypatch):
    monkeypatch.setattr(bouncer_module.discord, "Member", FakeMember)
    role = SimpleNamespace(id=10)
    config = BounceConfig(
        guild_id=1,
        verified_role_id=10,
        verification_channel_id=20,
        max_failed_attempts=3,
        min_account_age_days=14,
    )
    cog = make_start_verification_cog(config, role)
    interaction = make_interaction(FakeMember())

    asyncio.run(cog.start_verification(interaction))

    assert cog.active_challenges[(1, 2)].answer == "ABC123"
    assert cog.captcha.generated == ["ABC123"]
    interaction.response.send_message.assert_awaited_once()
    kwargs = interaction.response.send_message.await_args.kwargs
    assert kwargs["file"].filename == "captcha.png"
    assert kwargs["ephemeral"] is True
    assert "You have **3 attempts**." in kwargs["embed"].description


def test_start_verification_resends_existing_captcha_image(monkeypatch):
    monkeypatch.setattr(bouncer_module.discord, "Member", FakeMember)
    role = SimpleNamespace(id=10)
    config = BounceConfig(
        guild_id=1,
        verified_role_id=10,
        verification_channel_id=20,
        max_failed_attempts=3,
        min_account_age_days=14,
    )
    cog = make_start_verification_cog(config, role)
    cog.active_challenges[(1, 2)] = CaptchaChallenge(answer="OLD999", attempts=1)
    interaction = make_interaction(FakeMember())

    asyncio.run(cog.start_verification(interaction))

    assert cog.active_challenges[(1, 2)].answer == "OLD999"
    assert cog.captcha.generated == ["OLD999"]
    interaction.response.send_message.assert_awaited_once()
    kwargs = interaction.response.send_message.await_args.kwargs
    assert kwargs["file"].filename == "captcha.png"
    assert kwargs["ephemeral"] is True
    assert "You already have an active CAPTCHA." in kwargs["embed"].description
    assert "Attempts used: **1/3**" in kwargs["embed"].description
    assert "Attempts left: **2**" in kwargs["embed"].description
