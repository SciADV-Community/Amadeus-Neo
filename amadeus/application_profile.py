"""
Discord application profile helpers.
"""
import discord
from discord.ext import commands

from amadeus.constants import PRIVACY_POLICY_URL, TERMS_OF_SERVICE_URL
from amadeus.logging_utils import log

PRIVACY_POLICY_LABEL = "Privacy Policy:"
TERMS_OF_SERVICE_LABEL = "Terms of Service:"


def build_application_description(
    current_description: str | None,
    *,
    privacy_policy_url: str = PRIVACY_POLICY_URL,
    terms_of_service_url: str = TERMS_OF_SERVICE_URL,
) -> str | None:
    privacy_policy_url = privacy_policy_url.strip()
    terms_of_service_url = terms_of_service_url.strip()

    if not privacy_policy_url and not terms_of_service_url:
        return current_description

    lines = [
        line.rstrip()
        for line in (current_description or "").splitlines()
        if not line.strip().startswith((PRIVACY_POLICY_LABEL, TERMS_OF_SERVICE_LABEL))
    ]

    while lines and lines[-1] == "":
        lines.pop()

    if lines:
        lines.append("")
    if privacy_policy_url:
        lines.append(f"{PRIVACY_POLICY_LABEL} {privacy_policy_url}")
    if terms_of_service_url:
        lines.append(f"{TERMS_OF_SERVICE_LABEL} {terms_of_service_url}")

    return "\n".join(lines)


async def sync_application_profile(bot: commands.Bot) -> None:
    if not PRIVACY_POLICY_URL and not TERMS_OF_SERVICE_URL:
        return

    try:
        app_info = await bot.application_info()
        description = build_application_description(app_info.description)

        if description == app_info.description:
            log("APPLICATION PROFILE // BIO ALREADY CURRENT", level="debug", logger_name="core")
            return

        await app_info.edit(
            description=description,
            reason="Amadeus Neo application profile sync",
        )
        log("APPLICATION PROFILE // BIO UPDATED", logger_name="core")
    except discord.HTTPException as e:
        log(f"APPLICATION PROFILE // BIO UPDATE FAILED 『 {e} 』", level="warning", logger_name="core")
