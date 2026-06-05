import asyncio
from dataclasses import dataclass, field

import discord
from discord import app_commands
from discord.ext import commands

from amadeus.bouncer_config import BounceConfigStore
from amadeus.constants import (
    BACKFILL_DELAY_SECONDS,
    BACKFILL_INCLUDE_BOTS_BY_DEFAULT,
    MIN_ACCOUNT_AGE_DAYS,
)
from amadeus.database import ConfigStore
from amadeus.discord_utils import (
    PANEL_IMAGE_EXTENSIONS,
    SLOWMODE_VERIFICATION,
    check_role_hierarchy,
    validate_discord_attachment_image,
)
from amadeus.logging_utils import log
from amadeus.models.bouncer import BounceConfig
from amadeus.module_guard import require_module_enabled_for_interaction
from amadeus.permissions import require_amadeus_access
from cogs.bouncer import BouncerPanelView


# ============================================================
# Backfill progress
# ============================================================

@dataclass
class BackfillProgress:
    running: bool = False
    total_seen: int = 0
    added: int = 0
    skipped_already_verified: int = 0
    skipped_bots: int = 0
    failed: int = 0
    cancelled: bool = False
    last_member: str = "—"
    last_error: str = "—"


# ============================================================
# BounceAdmin cog
# ============================================================

class BounceAdmin(commands.Cog):
    """
    Admin cog for the bouncer module.

    Commands:
      /bouncer setup    set-role, set-channel, post-panel
      /bouncer settings min-account-age-days, max-failed-attempts,
                        captcha-expiration-minutes, panel-image,
                        verification-role-delay-seconds
      /bouncer admin    verify, unverify, verify-all, backfill-status, cancel-backfill

    Auto-paired with cogs.bouncer by the extension loader.
    Registers BouncerPanelView so the panel button survives restarts.
    """

    bouncer = app_commands.Group(
        name="bouncer",
        description="Bouncer commands.",
        guild_only=True,
        default_permissions=discord.Permissions(manage_roles=True),
    )

    setup = app_commands.Group(
        name="setup",
        description="Configure the verification channel, role, and panel.",
        parent=bouncer,
    )

    settings = app_commands.Group(
        name="settings",
        description="Tune per-server bouncer behaviour.",
        parent=bouncer,
    )

    admin = app_commands.Group(
        name="admin",
        description="Manual verification and bulk operations.",
        parent=bouncer,
    )

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.bounce_store = BounceConfigStore()
        self.module_store = ConfigStore()

        # Backfill state, tracked per server.
        self.backfill_tasks: dict[int, asyncio.Task] = {}
        self.backfill_cancel_requested: set[int] = set()
        self.backfill_progress: dict[int, BackfillProgress] = {}

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await require_module_enabled_for_interaction(
            interaction, self.module_store, "bouncer"
        )

    def cog_unload(self):
        for task in self.backfill_tasks.values():
            task.cancel()

        self.bounce_store.close()
        self.module_store.close()

    # ========================================================
    # Helpers
    # ========================================================

    def get_verified_role(
        self, guild: discord.Guild, config: BounceConfig
    ) -> discord.Role | None:
        if config.verified_role_id is None:
            return None
        return guild.get_role(config.verified_role_id)

    def get_verification_channel(
        self, guild: discord.Guild, config: BounceConfig
    ) -> discord.TextChannel | None:
        if config.verification_channel_id is None:
            return None
        channel = guild.get_channel(config.verification_channel_id)
        return channel if isinstance(channel, discord.TextChannel) else None

    def get_backfill_progress(self, guild_id: int) -> BackfillProgress:
        if guild_id not in self.backfill_progress:
            self.backfill_progress[guild_id] = BackfillProgress()
        return self.backfill_progress[guild_id]

    def _clear_user_challenge(self, guild_id: int, user_id: int):
        bouncer_cog = self.bot.get_cog("Bouncer")

        if bouncer_cog is None:
            return

        clear_challenge = getattr(bouncer_cog, "clear_challenge", None)

        if callable(clear_challenge):
            clear_challenge(guild_id, user_id)

    def _get_config(self, guild: discord.Guild) -> BounceConfig:
        config = self.bounce_store.get_bouncer_config(guild.id)
        return config if config is not None else BounceConfig(guild_id=guild.id)

    # ========================================================
    # /bouncer setup
    # ========================================================

    @setup.command(
        name="set-role",
        description="Set the role users receive after passing verification.",
    )
    @app_commands.describe(role="The role users receive after they pass verification.")
    async def bouncer_set_role(
        self,
        interaction: discord.Interaction,
        role: discord.Role,
    ):
        config = await require_amadeus_access(interaction, self.module_store)

        if config is None or interaction.guild is None:
            return

        if role == interaction.guild.default_role:
            await interaction.response.send_message(
                "You cannot use @everyone as the Verified role.",
                ephemeral=True,
            )
            return

        bot_member = interaction.guild.me

        if bot_member is None:
            log(f"BOUNCER // SET ROLE // BOT MEMBER NONE 『 GUILD {interaction.guild.id} 』", level="debug", logger_name="bouncer")
            await interaction.response.send_message(
                "Could not read my server member data.",
                ephemeral=True,
            )
            return

        hierarchy_error = check_role_hierarchy(bot_member, role)

        if hierarchy_error is not None:
            log(f"BOUNCER // SET ROLE // HIERARCHY BLOCK 『 ROLE {role.id} 』 GUILD 『 {interaction.guild.id} 』", level="debug", logger_name="bouncer")
            await interaction.response.send_message(hierarchy_error, ephemeral=True)
            return

        self.bounce_store.set_verified_role(interaction.guild.id, role.id)
        log(f"BOUNCER // VERIFIED ROLE SET 『 {role.id} 』 GUILD 『 {interaction.guild.id} 』", level="debug", logger_name="bouncer")

        await interaction.response.send_message(
            f"Verified role set to {role.mention}.",
            ephemeral=True,
        )

    @setup.command(
        name="set-channel",
        description="Set the channel where users verify.",
    )
    @app_commands.describe(channel="The channel users can see before verification.")
    async def bouncer_set_channel(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
    ):
        config = await require_amadeus_access(interaction, self.module_store)

        if config is None or interaction.guild is None:
            return

        self.bounce_store.set_verification_channel(interaction.guild.id, channel.id)
        log(f"BOUNCER // VERIFICATION CHANNEL SET 『 {channel.id} 』 GUILD 『 {interaction.guild.id} 』", level="debug", logger_name="bouncer")

        slowmode_note = ""
        try:
            await channel.edit(slowmode_delay=SLOWMODE_VERIFICATION, reason="Bouncer: verification channel slow-mode")
            log(f"BOUNCER // SLOWMODE SET 『 CHANNEL {channel.id} 』 GUILD 『 {interaction.guild.id} 』", level="debug", logger_name="bouncer")
        except discord.Forbidden:
            log(f"BOUNCER // SLOWMODE FORBIDDEN 『 CHANNEL {channel.id} 』 GUILD 『 {interaction.guild.id} 』", level="debug", logger_name="bouncer")
            slowmode_note = "\n\n⚠ Could not set slow-mode — I need the **Manage Channel** permission on that channel."
        except discord.HTTPException as e:
            log(f"BOUNCER // SLOWMODE FAILED 『 {e} 』", level="debug", logger_name="bouncer")
            slowmode_note = "\n\n⚠ Could not set slow-mode due to an unexpected error."

        await interaction.response.send_message(
            f"Verification channel set to {channel.mention}.{slowmode_note}",
            ephemeral=True,
        )

    @setup.command(
        name="post-panel",
        description="Post the public verification panel in the configured verification channel.",
    )
    async def bouncer_post_panel(self, interaction: discord.Interaction):
        config = await require_amadeus_access(interaction, self.module_store)

        if config is None or interaction.guild is None:
            return

        bounce_config = self._get_config(interaction.guild)
        channel = self.get_verification_channel(interaction.guild, bounce_config)

        if channel is None:
            await interaction.response.send_message(
                "Set a verification channel first with `/bouncer setup set-channel`.",
                ephemeral=True,
            )
            return

        if self.get_verified_role(interaction.guild, bounce_config) is None:
            await interaction.response.send_message(
                "Set a Verified role first with `/bouncer setup set-role`.",
                ephemeral=True,
            )
            return

        if not self.module_store.is_module_enabled(interaction.guild.id, "bouncer"):
            await interaction.response.send_message(
                (
                    "The **bouncer** module is not enabled for this server.\n"
                    "Enable it first with `/amadeus module enable bouncer`."
                ),
                ephemeral=True,
            )
            return

        if channel.slowmode_delay == 0:
            await interaction.response.send_message(
                (
                    f"{channel.mention} does not have slow-mode enabled.\n\n"
                    "Run `/bouncer setup set-channel` again to apply it, or set it "
                    "manually in the channel settings before posting the panel."
                ),
                ephemeral=True,
            )
            return

        eff_min_age = (
            bounce_config.min_account_age_days
            if bounce_config.min_account_age_days is not None
            else MIN_ACCOUNT_AGE_DAYS
        )
        eff_panel_image = bounce_config.panel_image_url

        embed = discord.Embed(
            title="Welcome — Verification Required",
            description=(
                "Before you can enter the server, please verify your account.\n\n"
                "**Requirements:**\n"
                f"- Your Discord account must be at least **{eff_min_age} days old**.\n"
                "- You must complete a private CAPTCHA.\n\n"
                "**How to verify:**\n"
                "Click **Start Verification** below or run `/verify`.\n"
                "Then submit your CAPTCHA with `/code <code>`."
            ),
            color=discord.Color.orange(),
        )

        if eff_panel_image:
            embed.set_image(url=eff_panel_image)

        embed.set_footer(
            text="Verification is private. Your CAPTCHA is only visible to you."
        )

        try:
            await channel.send(embed=embed, view=BouncerPanelView(self.bot))
        except (discord.Forbidden, discord.HTTPException) as e:
            log(f"BOUNCER // POST PANEL // SEND FAILED 『 CHANNEL {channel.id} 』 GUILD 『 {interaction.guild.id} 』 // {e}", level="debug", logger_name="bouncer")
            await interaction.response.send_message(
                f"Failed to post the panel in {channel.mention} — {e}",
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            f"Verification panel posted in {channel.mention}.",
            ephemeral=True,
        )

    # ========================================================
    # /bouncer settings
    # ========================================================

    @settings.command(
        name="min-account-age-days",
        description="Minimum age a Discord account must be to start verification.",
    )
    @app_commands.describe(days="0–30 days. Default: 14.")
    async def bouncer_settings_min_age(
        self,
        interaction: discord.Interaction,
        days: int,
    ):
        config = await require_amadeus_access(interaction, self.module_store)

        if config is None or interaction.guild is None:
            return

        if not 0 <= days <= 30:
            await interaction.response.send_message(
                "Value must be between **0** and **30** days.",
                ephemeral=True,
            )
            return

        self.bounce_store.set_min_account_age_days(interaction.guild.id, days)
        log(f"BOUNCER // SETTINGS MIN AGE DAYS {days} 『 GUILD {interaction.guild.id} 』", level="debug", logger_name="bouncer")

        await interaction.response.send_message(
            f"Minimum account age set to **{days} days**.",
            ephemeral=True,
        )

    @settings.command(
        name="max-failed-attempts",
        description="Number of failed CAPTCHA attempts before a user is kicked.",
    )
    @app_commands.describe(attempts="1–5 attempts. Default: 3.")
    async def bouncer_settings_max_attempts(
        self,
        interaction: discord.Interaction,
        attempts: int,
    ):
        config = await require_amadeus_access(interaction, self.module_store)

        if config is None or interaction.guild is None:
            return

        if not 1 <= attempts <= 5:
            await interaction.response.send_message(
                "Value must be between **1** and **5** attempts.",
                ephemeral=True,
            )
            return

        self.bounce_store.set_max_failed_attempts(interaction.guild.id, attempts)
        log(f"BOUNCER // SETTINGS MAX ATTEMPTS {attempts} 『 GUILD {interaction.guild.id} 』", level="debug", logger_name="bouncer")

        await interaction.response.send_message(
            f"Maximum failed attempts set to **{attempts}**.",
            ephemeral=True,
        )

    @settings.command(
        name="captcha-expiration-minutes",
        description="Minutes before an unsolved CAPTCHA challenge expires.",
    )
    @app_commands.describe(minutes="1–30 minutes. Default: 10.")
    async def bouncer_settings_captcha_expiry(
        self,
        interaction: discord.Interaction,
        minutes: int,
    ):
        config = await require_amadeus_access(interaction, self.module_store)

        if config is None or interaction.guild is None:
            return

        if not 1 <= minutes <= 30:
            await interaction.response.send_message(
                "Value must be between **1** and **30** minutes.",
                ephemeral=True,
            )
            return

        self.bounce_store.set_captcha_expiry_minutes(interaction.guild.id, minutes)
        log(f"BOUNCER // SETTINGS CAPTCHA EXPIRY {minutes} 『 GUILD {interaction.guild.id} 』", level="debug", logger_name="bouncer")

        await interaction.response.send_message(
            f"CAPTCHA expiration set to **{minutes} minutes**.",
            ephemeral=True,
        )

    @settings.command(
        name="panel-image",
        description="Upload the image displayed on the verification panel. Omit to clear.",
    )
    @app_commands.describe(image="Discord-uploaded image. Leave blank to remove the panel image.")
    async def bouncer_settings_panel_image(
        self,
        interaction: discord.Interaction,
        image: discord.Attachment | None = None,
    ):
        config = await require_amadeus_access(interaction, self.module_store)

        if config is None or interaction.guild is None:
            return

        if image is not None:
            validation_error = validate_discord_attachment_image(image, PANEL_IMAGE_EXTENSIONS)
            if validation_error is not None:
                await interaction.response.send_message(validation_error, ephemeral=True)
                return

        image_url = image.url if image is not None else None
        self.bounce_store.set_panel_image_url(interaction.guild.id, image_url)
        log(f"BOUNCER // SETTINGS PANEL IMAGE {'SET' if image_url else 'CLEARED'} 『 GUILD {interaction.guild.id} 』", level="debug", logger_name="bouncer")

        if image_url:
            await interaction.response.send_message(
                "Panel image set. Run `/bouncer setup post-panel` to repost the panel.",
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(
                "Panel image cleared. Run `/bouncer setup post-panel` to repost the panel.",
                ephemeral=True,
            )

    @settings.command(
        name="verification-role-delay-seconds",
        description="Seconds to wait before granting the Verified role after a successful CAPTCHA.",
    )
    @app_commands.describe(seconds="0–10 seconds. Default: 5.")
    async def bouncer_settings_role_delay(
        self,
        interaction: discord.Interaction,
        seconds: int,
    ):
        config = await require_amadeus_access(interaction, self.module_store)

        if config is None or interaction.guild is None:
            return

        if not 0 <= seconds <= 10:
            await interaction.response.send_message(
                "Value must be between **0** and **10** seconds.",
                ephemeral=True,
            )
            return

        self.bounce_store.set_verification_role_delay(interaction.guild.id, seconds)
        log(f"BOUNCER // SETTINGS ROLE DELAY {seconds} 『 GUILD {interaction.guild.id} 』", level="debug", logger_name="bouncer")

        await interaction.response.send_message(
            f"Verification role delay set to **{seconds} seconds**.",
            ephemeral=True,
        )

    # ========================================================
    # /bouncer admin
    # ========================================================

    @admin.command(
        name="verify",
        description="Manually verify a user.",
    )
    @app_commands.describe(member="The user to verify.")
    async def bouncer_verify(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
    ):
        config = await require_amadeus_access(interaction, self.module_store)

        if config is None or interaction.guild is None:
            return

        bounce_config = self._get_config(interaction.guild)
        role = self.get_verified_role(interaction.guild, bounce_config)

        if role is None:
            await interaction.response.send_message(
                "Set a Verified role first with `/bouncer setup set-role`.",
                ephemeral=True,
            )
            return

        if role in member.roles:
            await interaction.response.send_message(
                f"{member.mention} is already verified.",
                ephemeral=True,
            )
            return

        log(f"BOUNCER // MANUAL VERIFY 『 {member.id} 』 GUILD 『 {interaction.guild.id} 』", level="debug", logger_name="bouncer")

        try:
            await member.add_roles(
                role,
                reason=f"Manually verified by {interaction.user}",
            )
        except discord.Forbidden:
            await interaction.response.send_message(
                (
                    "I do not have permission to add the Verified role.\n\n"
                    "Make sure my bot role is above the Verified role."
                ),
                ephemeral=True,
            )
            return
        except discord.HTTPException:
            await interaction.response.send_message(
                "Something went wrong while verifying that user.",
                ephemeral=True,
            )
            return

        self._clear_user_challenge(interaction.guild.id, member.id)

        await interaction.response.send_message(
            f"{member.mention} has been manually verified.",
            ephemeral=True,
        )

    @admin.command(
        name="unverify",
        description="Manually remove verification from a user.",
    )
    @app_commands.describe(member="The user to unverify.")
    async def bouncer_unverify(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
    ):
        config = await require_amadeus_access(interaction, self.module_store)

        if config is None or interaction.guild is None:
            return

        bounce_config = self._get_config(interaction.guild)
        role = self.get_verified_role(interaction.guild, bounce_config)

        if role is None:
            await interaction.response.send_message(
                "Set a Verified role first with `/bouncer setup set-role`.",
                ephemeral=True,
            )
            return

        if role not in member.roles:
            await interaction.response.send_message(
                f"{member.mention} is not currently verified.",
                ephemeral=True,
            )
            return

        log(f"BOUNCER // MANUAL UNVERIFY 『 {member.id} 』 GUILD 『 {interaction.guild.id} 』", level="debug", logger_name="bouncer")

        try:
            await member.remove_roles(
                role,
                reason=f"Manually unverified by {interaction.user}",
            )
        except discord.Forbidden:
            await interaction.response.send_message(
                (
                    "I do not have permission to remove the Verified role.\n\n"
                    "Make sure my bot role is above the Verified role."
                ),
                ephemeral=True,
            )
            return
        except discord.HTTPException:
            await interaction.response.send_message(
                "Something went wrong while unverifying that user.",
                ephemeral=True,
            )
            return

        self._clear_user_challenge(interaction.guild.id, member.id)

        await interaction.response.send_message(
            f"{member.mention} has been manually unverified.",
            ephemeral=True,
        )

    @admin.command(
        name="verify-all",
        description="Slowly give the Verified role to all existing members.",
    )
    @app_commands.describe(
        include_bots="Whether bots should also receive the Verified role.",
    )
    async def bouncer_verify_all(
        self,
        interaction: discord.Interaction,
        include_bots: bool = BACKFILL_INCLUDE_BOTS_BY_DEFAULT,
    ):
        config = await require_amadeus_access(interaction, self.module_store)

        if config is None or interaction.guild is None:
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        guild_id = interaction.guild.id
        existing_task = self.backfill_tasks.get(guild_id)

        if existing_task is not None and not existing_task.done():
            log(f"BOUNCER // BACKFILL ALREADY RUNNING 『 GUILD {guild_id} 』", level="debug", logger_name="bouncer")
            await interaction.followup.send(
                (
                    "A Verified role backfill is already running for this server.\n\n"
                    "Use `/bouncer admin backfill-status` to check progress."
                ),
                ephemeral=True,
            )
            return

        bounce_config = self._get_config(interaction.guild)
        role = self.get_verified_role(interaction.guild, bounce_config)

        if role is None:
            await interaction.followup.send(
                "Set a Verified role first with `/bouncer setup set-role`.",
                ephemeral=True,
            )
            return

        bot_member = interaction.guild.me

        if bot_member is None:
            log(f"BOUNCER // VERIFY ALL // BOT MEMBER NONE 『 GUILD {interaction.guild.id} 』", level="debug", logger_name="bouncer")
            await interaction.followup.send(
                "Could not read my server member data.",
                ephemeral=True,
            )
            return

        if not bot_member.guild_permissions.manage_roles:
            log(f"BOUNCER // VERIFY ALL // MISSING MANAGE ROLES 『 GUILD {interaction.guild.id} 』", level="debug", logger_name="bouncer")
            await interaction.followup.send(
                "I need the Manage Roles permission before I can do this.",
                ephemeral=True,
            )
            return

        hierarchy_error = check_role_hierarchy(bot_member, role)

        if hierarchy_error is not None:
            log(f"BOUNCER // VERIFY ALL // HIERARCHY BLOCK 『 ROLE {role.id} 』 GUILD 『 {interaction.guild.id} 』", level="debug", logger_name="bouncer")
            await interaction.followup.send(hierarchy_error, ephemeral=True)
            return

        self.backfill_cancel_requested.discard(guild_id)
        self.backfill_tasks[guild_id] = asyncio.create_task(
            self._run_backfill(
                guild=interaction.guild,
                role=role,
                include_bots=include_bots,
            )
        )
        log(f"BOUNCER // BACKFILL STARTED 『 GUILD {guild_id} 』 BOTS {include_bots}", level="debug", logger_name="bouncer")

        await interaction.followup.send(
            (
                "Started the Verified role backfill for this server.\n\n"
                f"Delay per role add: **{BACKFILL_DELAY_SECONDS} seconds**\n"
                f"Include bots: **{include_bots}**\n\n"
                "Use `/bouncer admin backfill-status` to check progress.\n"
                "Use `/bouncer admin cancel-backfill` to stop it."
            ),
            ephemeral=True,
        )

    @admin.command(
        name="backfill-status",
        description="Check the Verified role backfill progress.",
    )
    async def bouncer_backfill_status(self, interaction: discord.Interaction):
        config = await require_amadeus_access(interaction, self.module_store)

        if config is None or interaction.guild is None:
            return

        progress = self.get_backfill_progress(interaction.guild.id)
        status = "Running" if progress.running else "Not running"

        embed = discord.Embed(
            title="Verified Role Backfill Status",
            color=discord.Color.orange(),
        )

        embed.add_field(name="Status", value=status, inline=False)
        embed.add_field(name="Members seen", value=str(progress.total_seen), inline=True)
        embed.add_field(name="Roles added", value=str(progress.added), inline=True)
        embed.add_field(
            name="Already verified",
            value=str(progress.skipped_already_verified),
            inline=True,
        )
        embed.add_field(name="Bots skipped", value=str(progress.skipped_bots), inline=True)
        embed.add_field(name="Failures", value=str(progress.failed), inline=True)
        embed.add_field(name="Cancelled", value=str(progress.cancelled), inline=True)
        embed.add_field(name="Last member", value=progress.last_member, inline=False)
        embed.add_field(name="Last error", value=progress.last_error, inline=False)

        await interaction.response.send_message(embed=embed, ephemeral=True)

    @admin.command(
        name="cancel-backfill",
        description="Cancel the running Verified role backfill.",
    )
    async def bouncer_cancel_backfill(self, interaction: discord.Interaction):
        config = await require_amadeus_access(interaction, self.module_store)

        if config is None or interaction.guild is None:
            return

        guild_id = interaction.guild.id
        task = self.backfill_tasks.get(guild_id)

        if task is None or task.done():
            await interaction.response.send_message(
                "No Verified role backfill is currently running for this server.",
                ephemeral=True,
            )
            return

        self.backfill_cancel_requested.add(guild_id)

        await interaction.response.send_message(
            "Backfill cancellation requested. It will stop after the current member finishes.",
            ephemeral=True,
        )

    async def _run_backfill(
        self,
        guild: discord.Guild,
        role: discord.Role,
        include_bots: bool,
    ):
        guild_id = guild.id
        progress = BackfillProgress(running=True)
        self.backfill_progress[guild_id] = progress

        try:
            async for member in guild.fetch_members(limit=None):
                if guild_id in self.backfill_cancel_requested:
                    log(f"BOUNCER // BACKFILL CANCELLED 『 GUILD {guild_id} 』", level="debug", logger_name="bouncer")
                    progress.cancelled = True
                    break

                progress.total_seen += 1
                progress.last_member = f"{member} ({member.id})"

                if member.bot and not include_bots:
                    progress.skipped_bots += 1
                    continue

                if role in member.roles:
                    progress.skipped_already_verified += 1
                    continue

                try:
                    await member.add_roles(
                        role,
                        reason="Initial Verified role backfill",
                    )

                    progress.added += 1

                    await asyncio.sleep(BACKFILL_DELAY_SECONDS)

                except discord.Forbidden:
                    log(f"BOUNCER // BACKFILL FORBIDDEN 『 {member.id} 』 GUILD 『 {guild_id} 』 // STOPPING", level="debug", logger_name="bouncer")
                    progress.failed += 1
                    progress.last_error = (
                        "Missing permission or role hierarchy problem. "
                        "Stopping backfill to avoid repeated failed requests."
                    )
                    break

                except discord.HTTPException as error:
                    log(f"BOUNCER // BACKFILL HTTP ERROR 『 {member.id} 』 GUILD 『 {guild_id} 』 // {error}", level="debug", logger_name="bouncer")
                    progress.failed += 1
                    progress.last_error = repr(error)

                    await asyncio.sleep(BACKFILL_DELAY_SECONDS * 5)

        finally:
            progress.running = False
            self.backfill_tasks.pop(guild_id, None)
            self.backfill_cancel_requested.discard(guild_id)


async def setup(bot: commands.Bot):
    await bot.add_cog(BounceAdmin(bot))

    # Register the persistent panel view so button clicks work even if the
    # Bouncer user cog is reloaded or temporarily unloaded.
    bot.add_view(BouncerPanelView(bot))
