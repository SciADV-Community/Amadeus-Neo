"""
Shared interaction guards for optional Amadeus modules.
"""
import discord

from amadeus.database import ConfigStore


async def require_module_enabled_for_interaction(
    interaction: discord.Interaction,
    store: ConfigStore,
    module_name: str,
    *,
    admin_hint: bool = True,
) -> bool:
    """Returns False after responding if a module is disabled for the guild."""
    if interaction.guild_id is None:
        return True

    if store.is_module_enabled(interaction.guild_id, module_name):
        return True

    hint = (
        f"\nEnable it first with `/amadeus module enable {module_name}`."
        if admin_hint
        else ""
    )
    await interaction.response.send_message(
        f"The **{module_name}** module is not enabled on this server.{hint}",
        ephemeral=True,
    )
    return False
