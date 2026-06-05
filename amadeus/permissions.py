import discord

from amadeus.database import ConfigStore
from amadeus.logging_utils import log
from amadeus.models import GuildConfig


def get_admin_role(
    guild: discord.Guild,
    config: GuildConfig,
) -> discord.Role | None:
    if config.admin_role_id is None:
        return None

    return guild.get_role(config.admin_role_id)


def user_has_amadeus_access(
    member: discord.Member,
    guild: discord.Guild,
    config: GuildConfig,
) -> bool:
    """
    Access rules:
    - Server owner can always use /amadeus.
    - Members with the configured admin role can use /amadeus.
    """

    log(
        f"PERMS // ACCESS CHECK 『 USER {member.id} 』 GUILD 『 {guild.id} 』 OWNER {member.id == config.owner_id}",
        level="debug",
        logger_name="perms",
    )

    if member.id == config.owner_id:
        return True

    admin_role = get_admin_role(guild, config)

    if admin_role is not None and admin_role in member.roles:
        return True

    return False


async def require_amadeus_access(
    interaction: discord.Interaction,
    store: ConfigStore,
) -> GuildConfig | None:
    """
    Validates access to /amadeus commands.

    Returns:
    - GuildConfig if allowed
    - None if blocked
    """

    if interaction.guild is None:
        await interaction.response.send_message(
            "This can only be used inside a server.",
            ephemeral=True,
        )
        return None

    if not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message(
            "Could not read your server member data.",
            ephemeral=True,
        )
        return None

    config = store.ensure_guild_config(interaction.guild)

    if user_has_amadeus_access(interaction.user, interaction.guild, config):
        return config

    log(
        f"PERMS // ACCESS DENIED 『 USER {interaction.user.id} 』 GUILD 『 {interaction.guild.id} 』",
        level="debug",
        logger_name="perms",
    )

    if config.admin_role_id is None:
        await interaction.response.send_message(
            "Only the server owner can use `/amadeus` until an admin role is configured.",
            ephemeral=True,
        )
    else:
        await interaction.response.send_message(
            "You need the configured Amadeus admin role to use this command.",
            ephemeral=True,
        )

    return None
