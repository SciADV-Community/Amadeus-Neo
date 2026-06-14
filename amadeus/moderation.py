"""
Shared moderation action helpers.

All action functions return a human-readable result string.
"""
from datetime import timedelta

import discord

from amadeus.logging_utils import log


DEFAULT_HONEYPOT_REASON = "Honeypot triggered"


async def execute_action(
    guild: discord.Guild,
    member: discord.Member,
    action: str | None,
    action_role_id: int | None = None,
    action_reason: str | None = None,
    delete_history_seconds: int | None = None,
) -> str:
    """Takes the given action against a member. Returns a human-readable result."""
    if action is None:
        return "No action configured."
    if action == "remove_role":
        return await action_remove_role(guild, member, action_role_id)
    if action == "mute":
        return await action_mute(member, reason=action_reason)
    if action == "kick":
        return await action_kick(member, reason=action_reason)
    if action == "ban":
        return await action_ban(
            member,
            reason=action_reason,
            delete_message_seconds=delete_history_seconds,
        )
    return f"Unknown action: {action}"


async def action_remove_role(
    guild: discord.Guild,
    member: discord.Member,
    role_id: int | None,
) -> str:
    if role_id is None:
        return "Failed — no role configured for remove-role action."

    role = guild.get_role(role_id)

    if role is None:
        return "Failed — configured role no longer exists."

    if role not in member.roles:
        return f"Skipped — member does not have **{role.name}**."

    try:
        await member.remove_roles(role, reason=DEFAULT_HONEYPOT_REASON)
        log(
            f"MODERATION // REMOVE ROLE 『 {member.id} 』 ROLE 『 {role_id} 』 GUILD 『 {guild.id} 』",
            level="debug",
            logger_name="moderation",
        )
        return f"Removed role **{role.name}**."
    except discord.Forbidden:
        return "Failed — missing permission or role hierarchy issue."
    except discord.HTTPException as e:
        return f"Failed — {e}"


async def action_mute(member: discord.Member, *, reason: str | None = None) -> str:
    until = discord.utils.utcnow() + timedelta(days=28)

    try:
        await member.timeout(until, reason=reason or DEFAULT_HONEYPOT_REASON)
        log(
            f"MODERATION // TIMEOUT 『 {member.id} 』 GUILD 『 {member.guild.id} 』",
            level="debug",
            logger_name="moderation",
        )
        return "Timed out for 28 days."
    except discord.Forbidden:
        return "Failed — missing Moderate Members permission or member outranks the bot."
    except discord.HTTPException as e:
        return f"Failed — {e}"


async def action_kick(member: discord.Member, *, reason: str | None = None) -> str:
    try:
        await member.kick(reason=reason or DEFAULT_HONEYPOT_REASON)
        log(
            f"MODERATION // KICK 『 {member.id} 』 GUILD 『 {member.guild.id} 』",
            level="debug",
            logger_name="moderation",
        )
        return "Kicked."
    except discord.Forbidden:
        return "Failed — missing Kick Members permission or member outranks the bot."
    except discord.HTTPException as e:
        return f"Failed — {e}"


async def action_ban(
    member: discord.Member,
    *,
    reason: str | None = None,
    delete_message_seconds: int | None = None,
) -> str:
    try:
        await member.ban(
            reason=reason or DEFAULT_HONEYPOT_REASON,
            delete_message_seconds=delete_message_seconds or 0,
        )
        log(
            f"MODERATION // BAN 『 {member.id} 』 GUILD 『 {member.guild.id} 』",
            level="debug",
            logger_name="moderation",
        )
        return "Banned."
    except discord.Forbidden:
        return "Failed — missing Ban Members permission or member outranks the bot."
    except discord.HTTPException as e:
        return f"Failed — {e}"


def action_display(action: str | None, action_role_id: int | None, guild: discord.Guild) -> str:
    """Returns a human-readable description of the configured action."""
    if action is None:
        return "None"

    if action == "remove_role":
        role = guild.get_role(action_role_id) if action_role_id else None
        role_name = role.name if role else f"ID {action_role_id}"
        return f"Remove role ({role_name})"

    return {"mute": "Mute (28-day timeout)", "kick": "Kick", "ban": "Ban"}.get(action, action)
