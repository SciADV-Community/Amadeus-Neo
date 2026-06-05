"""
Boost-specific utility functions shared between the boost cog and admin cog.
"""
import discord

from amadeus.logging_utils import log


def get_proposed_tier(before_count: int, after_count: int) -> int:
    """
    Infers the boost tier a member contributed based on the guild-level
    subscription count delta at the moment of the on_member_update event.

    delta >= 2  →  tier 2 (member added 2 boosts)
    anything else  →  tier 1

    This is best-effort: if two users boost simultaneously the counts can
    overlap. Admins confirm the actual tier at approval time.
    """
    delta = after_count - before_count
    return 2 if delta >= 2 else 1


def is_active_booster(member: discord.Member) -> bool:
    """Returns True if the member is currently boosting the guild."""
    return member.premium_since is not None


def check_emoji_slots(guild: discord.Guild, needed: int) -> tuple[bool, int, int]:
    """
    Checks whether the guild has enough free emoji slots.

    Returns (sufficient, available, capacity).

    Managed emojis (e.g. from integrations) are excluded because they occupy
    a separate pool and don't count against the user-uploadable limit.
    """
    capacity = guild.emoji_limit
    used = sum(1 for e in guild.emojis if not e.managed)
    available = capacity - used
    sufficient = available >= needed

    log(
        f"BOOST_UTILS // EMOJI SLOTS 『 GUILD {guild.id} 』 "
        f"USED {used}/{capacity} NEED {needed} SUFFICIENT {sufficient}",
        level="debug",
        logger_name="boost_utils",
    )
    return sufficient, available, capacity
