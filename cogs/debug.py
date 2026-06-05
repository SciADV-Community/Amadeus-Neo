"""
Debug command handlers for /amadeus debug.

This is a helper module, not a cog. Functions here are wired to the
amadeus_debug subgroup in amadeus_admin.py.
"""

import discord


async def cmd_ping(interaction: discord.Interaction):
    await interaction.response.send_message("Pong!", ephemeral=True)
