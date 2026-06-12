from datetime import timedelta
from types import SimpleNamespace

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
