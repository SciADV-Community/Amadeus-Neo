import discord
from discord import app_commands
from discord.ext import commands

from amadeus.boost_store import BoostStore
from amadeus.boost_utils import get_proposed_tier, is_active_booster
from amadeus.database import ConfigStore
from amadeus.dm_flow import DmFlowStore
from amadeus.logging_utils import log
from amadeus.module_guard import require_module_enabled_for_interaction
from amadeus.permissions import require_amadeus_access
from cogs.boost import FLOW_TYPE, S, Boost


class BoostAdmin(commands.Cog):
    """
    Admin cog for the boost module.

    Commands:
      /boost status            — member checks their own perk status
      /boost admin start       — manually start the flow for an existing booster
      /boost admin remove      — tear down a member's perks and clear their flow
      /boost admin status      — inspect any member's flow and grant state
    """

    boost = app_commands.Group(
        name="boost",
        description="Boost perks commands.",
        guild_only=True,
    )

    boost_admin = app_commands.Group(
        name="admin",
        description="Admin commands for the boost module.",
        parent=boost,
    )

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.flow_store = DmFlowStore()
        self.boost_store = BoostStore()
        self.module_store = ConfigStore()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await require_module_enabled_for_interaction(
            interaction, self.module_store, "boost"
        )

    def cog_unload(self):
        self.flow_store.close()
        self.boost_store.close()
        self.module_store.close()

    def _boost_cog(self) -> Boost | None:
        return self.bot.get_cog("Boost")

    # ========================================================
    # /boost status — open to all guild members
    # ========================================================

    @boost.command(
        name="status",
        description="Check your boost perk status or resume an in-progress request.",
    )
    async def boost_status(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message(
                "This can only be used inside a server.", ephemeral=True
            )
            return

        if not self.module_store.is_module_enabled(interaction.guild.id, "boost"):
            await interaction.response.send_message(
                "The **boost** module is not enabled on this server.", ephemeral=True
            )
            return

        member = interaction.user
        guild = interaction.guild
        grant = self.boost_store.get_grant(guild.id, member.id)
        flow = self.flow_store.get(guild.id, member.id, FLOW_TYPE)

        embed = discord.Embed(title="Your Boost Perks", color=discord.Color.from_rgb(255, 115, 250))

        if grant is not None:
            role = guild.get_role(grant.role_id) if grant.role_id else None
            emoji_1 = discord.utils.get(guild.emojis, id=grant.emoji_1_id) if grant.emoji_1_id else None
            emoji_2 = discord.utils.get(guild.emojis, id=grant.emoji_2_id) if grant.emoji_2_id else None

            embed.description = "✅ Your perks are active."
            embed.add_field(name="Tier", value=str(grant.tier), inline=True)
            embed.add_field(name="Role", value=role.mention if role else "*(removed)*", inline=True)
            if emoji_1:
                embed.add_field(name="Emoji 1", value=str(emoji_1), inline=True)
            if emoji_2:
                embed.add_field(name="Emoji 2", value=str(emoji_2), inline=True)

        elif flow is not None:
            state_labels = {
                S.ROLE_NAME:     "Waiting for role name",
                S.ROLE_IMAGE:    "Waiting for role icon image",
                S.ROLE_COLOR:    "Waiting for role color",
                S.EMOJI_1_NAME:  "Waiting for emoji 1 name",
                S.EMOJI_1_IMAGE: "Waiting for emoji 1 image",
                S.EMOJI_2_NAME:  "Waiting for emoji 2 name",
                S.EMOJI_2_IMAGE: "Waiting for emoji 2 image",
                S.CONFIRMATION:  "Ready to submit — check your DMs",
                S.PENDING:       "Pending admin review",
                S.PROCESSING:    "Approval is being processed",
                S.DENIED:        "Previous request was not approved",
            }
            if flow.state == S.DENIED:
                if flow.data.get("forced"):
                    embed.description = "Your previous request was not approved."
                else:
                    embed.description = (
                        "Your previous request was not approved.\n\n"
                        "I'll restart the setup in your DMs."
                    )
            else:
                embed.description = (
                    "📝 You have an active request in progress.\n\n"
                    f"**Current step:** {state_labels.get(flow.state, flow.state)}"
                )
            if flow.state not in (S.PENDING, S.PROCESSING, S.CONFIRMATION, S.DENIED):
                embed.set_footer(text="Check your DMs to continue, or the bot will re-send the prompt.")

        elif is_active_booster(member):
            embed.description = (
                "You're boosting but have no active request.\n\n"
                "An admin can start one for you with `/boost admin start`."
            )
        else:
            embed.description = "You are not currently boosting this server."

        await interaction.response.send_message(embed=embed, ephemeral=True)

        # If the user has an active mid-flow, re-send the current step prompt in DMs.
        if flow is not None and flow.state == S.DENIED and not flow.data.get("forced"):
            cog = self._boost_cog()
            if cog is not None:
                restarted = await cog._reset_flow(
                    guild.id,
                    member.id,
                    flow.data.get("tier", 1),
                    forced=bool(flow.data.get("forced")),
                )
                await cog._send_prompt(interaction.user, restarted)
        elif flow is not None and flow.state not in (S.PENDING, S.PROCESSING, S.CONFIRMATION):
            cog = self._boost_cog()
            if cog is not None:
                await cog._send_prompt(interaction.user, flow)

    # ========================================================
    # /boost admin start
    # ========================================================

    @boost_admin.command(
        name="start",
        description="Manually start the boost perks flow for a member.",
    )
    @app_commands.describe(
        member="The member to start the flow for.",
        force="Skip the active-booster check (for gifting perks or testing).",
        count="Boost count to use for perks: 1 or 2. Omit to infer automatically.",
    )
    async def boost_admin_start(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        force: bool = False,
        count: int | None = None,
    ) -> None:
        config = await require_amadeus_access(interaction, self.module_store)
        if config is None or interaction.guild is None:
            return

        if count is not None and count not in (1, 2):
            await interaction.response.send_message(
                "`count` must be either **1** or **2**.",
                ephemeral=True,
            )
            return

        if not force and not is_active_booster(member):
            await interaction.response.send_message(
                f"{member.mention} is not currently boosting this server. "
                "Use `force: True` to start the flow anyway.",
                ephemeral=True,
            )
            return

        cog = self._boost_cog()
        if cog is None:
            await interaction.response.send_message("Boost cog is not loaded.", ephemeral=True)
            return

        # Discard any stale flow so the new one starts cleanly.
        self.flow_store.delete(interaction.guild.id, member.id, FLOW_TYPE)

        current = interaction.guild.premium_subscription_count or 0
        cached = self.boost_store.get_subscription_count(interaction.guild.id)
        tier = count if count is not None else get_proposed_tier(
            cached if cached is not None else max(0, current - 1),
            current,
        )

        await cog._start_flow(interaction.guild, member, tier, forced=force)
        log(
            f"BOOST // ADMIN START 『 USER {member.id} 』 GUILD 『 {interaction.guild.id} 』 "
            f"TIER {tier}{' [FORCED]' if force else ''}{' [COUNT OVERRIDE]' if count is not None else ''}",
            level="debug",
            logger_name="boost",
        )

        forced_note = " *(booster check bypassed)*" if force else ""
        tier_source = "selected" if count is not None else "inferred"
        await interaction.response.send_message(
            f"Started the boost perks flow for {member.mention} ({tier_source} tier: **{tier}**){forced_note}. "
            "They'll receive a DM shortly.",
            ephemeral=True,
        )

    # ========================================================
    # /boost admin remove
    # ========================================================

    @boost_admin.command(
        name="remove",
        description="Remove a member's boost perks (role + emojis) and clear any active flow.",
    )
    @app_commands.describe(member="The member whose perks should be removed.")
    async def boost_admin_remove(
        self, interaction: discord.Interaction, member: discord.Member
    ) -> None:
        config = await require_amadeus_access(interaction, self.module_store)
        if config is None or interaction.guild is None:
            return

        self.flow_store.delete(interaction.guild.id, member.id, FLOW_TYPE)
        grant = self.boost_store.get_grant(interaction.guild.id, member.id)

        if grant is None:
            await interaction.response.send_message(
                f"{member.mention} has no active boost grant.", ephemeral=True
            )
            return

        cog = self._boost_cog()
        if cog is not None:
            await cog._teardown_grant(interaction.guild, grant)
        else:
            # Cog not loaded; just delete the DB record — manual cleanup required.
            self.boost_store.delete_grant(interaction.guild.id, member.id)

        log(
            f"BOOST // ADMIN REMOVE 『 USER {member.id} 』 GUILD 『 {interaction.guild.id} 』",
            level="debug",
            logger_name="boost",
        )

        await interaction.response.send_message(
            f"Boost perks removed for {member.mention}.",
            ephemeral=True,
        )

    # ========================================================
    # /boost admin status
    # ========================================================

    @boost_admin.command(
        name="status",
        description="Check the boost flow and grant details for any member.",
    )
    @app_commands.describe(member="The member to inspect.")
    async def boost_admin_status(
        self, interaction: discord.Interaction, member: discord.Member
    ) -> None:
        config = await require_amadeus_access(interaction, self.module_store)
        if config is None or interaction.guild is None:
            return

        flow = self.flow_store.get(interaction.guild.id, member.id, FLOW_TYPE)
        grant = self.boost_store.get_grant(interaction.guild.id, member.id)

        embed = discord.Embed(
            title=f"Boost Status — {member}",
            color=discord.Color.from_rgb(255, 115, 250),
        )
        embed.add_field(name="Currently Boosting", value=str(is_active_booster(member)), inline=True)

        if grant is not None:
            role = interaction.guild.get_role(grant.role_id) if grant.role_id else None
            e1 = discord.utils.get(interaction.guild.emojis, id=grant.emoji_1_id) if grant.emoji_1_id else None
            e2 = discord.utils.get(interaction.guild.emojis, id=grant.emoji_2_id) if grant.emoji_2_id else None

            embed.add_field(name="Grant Tier", value=str(grant.tier), inline=True)
            embed.add_field(name="Role", value=role.mention if role else f"ID {grant.role_id} *(missing)*", inline=True)
            embed.add_field(name="Emoji 1", value=str(e1) if e1 else "None", inline=True)
            embed.add_field(name="Emoji 2", value=str(e2) if e2 else "None", inline=True)
        else:
            embed.add_field(name="Grant", value="None", inline=True)

        if flow is not None:
            embed.add_field(name="Flow State", value=flow.state, inline=False)
            embed.add_field(name="Flow Tier", value=str(flow.data.get("tier", "?")), inline=True)
            if flow.updated_at:
                embed.add_field(
                    name="Last Updated",
                    value=discord.utils.format_dt(flow.updated_at, "R"),
                    inline=True,
                )
        else:
            embed.add_field(name="Active Flow", value="None", inline=False)

        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(BoostAdmin(bot))
