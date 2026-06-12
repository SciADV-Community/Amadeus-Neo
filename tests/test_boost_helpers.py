import discord

from amadeus.models.dm_flow import DmFlow
from cogs.boost import Boost


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
