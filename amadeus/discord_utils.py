"""
Shared Discord helpers used across multiple cogs.
"""
import re
from pathlib import PurePosixPath
from urllib.parse import urlparse

import discord

from amadeus.logging_utils import log

# Slowmode applied to verification channels (bouncer). Message deletion is
# gated on slow-mode being active to avoid Discord rate limits.
SLOWMODE_VERIFICATION = 3600  # 1 hour

# Slowmode applied to honeypot channels to limit incoming message rate.
SLOWMODE_HONEYPOT = 60  # 1 minute

DEFAULT_ALLOWED_MENTIONS = discord.AllowedMentions(
    everyone=False,
    users=True,
    roles=True,
    replied_user=False,
)
NO_MENTIONS = discord.AllowedMentions.none()

DISCORD_ATTACHMENT_HOSTS = {
    "cdn.discordapp.com",
    "media.discordapp.net",
}
_DISCORD_IMAGE_PROXY_RE = re.compile(r"^images-ext-\d+\.discordapp\.net$")

ROLE_ICON_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
EMOJI_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
PANEL_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}

_CONTROL_OR_BIDI_RE = re.compile(r"[\x00-\x1f\x7f\u202a-\u202e\u2066-\u2069]")
_MENTIONISH_RE = re.compile(
    r"@(?:everyone|here)|<@!?\d+>|<@&\d+>|<#\d+>",
    re.IGNORECASE,
)


async def safe_dm(
    user: discord.User | discord.Member,
    *,
    content: str | None = None,
    embed: discord.Embed | None = None,
    embeds: list[discord.Embed] | None = None,
    files: list[discord.File] | None = None,
    view: discord.ui.View | None = None,
) -> bool:
    """
    Sends a DM, logging and swallowing failures if the user has DMs disabled.

    Returns True if the message was sent successfully.
    """
    try:
        await user.send(
            content=content,
            embed=None if embeds is not None else embed,
            embeds=embeds,
            files=files or None,
            view=view,
            allowed_mentions=NO_MENTIONS,
        )
        return True
    except (discord.Forbidden, discord.HTTPException) as e:
        log(f"DM // FAILED 『 USER {user.id} 』 // {e}", level="debug", logger_name="discord_utils")
        return False


def check_role_hierarchy(bot_member: discord.Member, role: discord.Role) -> str | None:
    """
    Returns an error string if the role is at or above the bot's top role, else None.

    Call before any role assignment to avoid Discord Forbidden errors from
    hierarchy violations.
    """
    if role >= bot_member.top_role:
        return (
            "I cannot assign that role because it is above or equal to my highest role.\n\n"
            "Move my bot role above it, then try again."
        )
    return None


def escape_untrusted_text(value: str, *, max_length: int | None = None) -> str:
    """Escapes Discord markdown/mentions for user-supplied text shown by the bot."""
    if max_length is not None:
        value = value[:max_length]
    return discord.utils.escape_mentions(discord.utils.escape_markdown(value))


def validate_safe_role_name(name: str) -> str | None:
    """Returns an error if a user-provided role name can spoof mentions or UI."""
    if _CONTROL_OR_BIDI_RE.search(name):
        return "Role names cannot contain control or bidirectional formatting characters."
    if _MENTIONISH_RE.search(name):
        return "Role names cannot contain @everyone, @here, or Discord mention syntax."
    return None


def is_discord_media_url(url: str) -> bool:
    parsed = urlparse(url)
    if parsed.scheme != "https":
        return False
    host = parsed.hostname or ""
    if host in DISCORD_ATTACHMENT_HOSTS:
        return True
    return bool(_DISCORD_IMAGE_PROXY_RE.match(host))


def image_extension_from_url(url: str) -> str:
    parsed = urlparse(url)
    return PurePosixPath(parsed.path).suffix.lower()


def validate_discord_image_url(url: str, allowed_extensions: set[str]) -> str | None:
    if not is_discord_media_url(url):
        return "Image must be uploaded to Discord, not linked from an external site."
    ext = image_extension_from_url(url)
    if ext not in allowed_extensions:
        allowed = ", ".join(sorted(allowed_extensions))
        return f"Image must use one of these file types: {allowed}."
    return None


def validate_discord_attachment_image(
    attachment: discord.Attachment,
    allowed_extensions: set[str],
) -> str | None:
    if not attachment.content_type or not attachment.content_type.startswith("image/"):
        return "Please upload an image file."
    return validate_discord_image_url(attachment.url, allowed_extensions)
