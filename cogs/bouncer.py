import asyncio
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands

from amadeus.alerts import send_alert
from amadeus.bouncer_config import BounceConfigStore
from amadeus.captcha_utils import CaptchaService, normalize_code
from amadeus.constants import (
    CAPTCHA_EXPIRY_MINUTES,
    MAX_FAILED_ATTEMPTS,
    MIN_ACCOUNT_AGE_DAYS,
    VERIFICATION_ROLE_DELAY_SECONDS,
)
from amadeus.database import ConfigStore
from amadeus.logging_utils import log
from amadeus.models.bouncer import BounceConfig
from amadeus.module_guard import require_module_enabled_for_interaction

_SLOWMODE_ALERT_COOLDOWN_SECONDS = 600  # 10 minutes per guild


# ============================================================
# Panel view
# ============================================================

class BouncerPanelView(discord.ui.View):
    """
    Persistent view for the public bouncer verification panel.

    Registered by bouncer_admin on setup so the button survives restarts.
    If the Bouncer cog is unloaded, button clicks respond with an unavailable message.
    """

    def __init__(self, bot: commands.Bot):
        super().__init__(timeout=None)
        self.bot = bot

    @discord.ui.button(
        label="Start Verification",
        style=discord.ButtonStyle.success,
        custom_id="amadeus_verification:start",
    )
    async def start_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        cog = self.bot.get_cog("Bouncer")

        if cog is None:
            await interaction.response.send_message(
                "Verification is currently unavailable. Please try again later.",
                ephemeral=True,
            )
            return

        await cog.start_verification(interaction)


# ============================================================
# CAPTCHA challenge state
# ============================================================

@dataclass
class CaptchaChallenge:
    """
    Stores one user's active CAPTCHA challenge.

    In-memory only — clears on bot restart.
    """

    answer: str
    attempts: int = 0
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


# ============================================================
# Bouncer cog
# ============================================================

class Bouncer(commands.Cog):
    """
    User-facing bouncer verification system.

    Commands: /verify, /code
    Service methods used by bouncer_admin and the panel view.
    """

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.bounce_store = BounceConfigStore()
        self.module_store = ConfigStore()
        self.captcha = CaptchaService()

        # Key: (guild_id, user_id)
        self.active_challenges: dict[tuple[int, int], CaptchaChallenge] = {}

        # Tracks the last time a slowmode-missing alert was sent per guild.
        self._slowmode_alert_sent_at: dict[int, float] = {}

    def cog_unload(self):
        self.bounce_store.close()
        self.module_store.close()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await require_module_enabled_for_interaction(
            interaction, self.module_store, "bouncer", admin_hint=False
        )

    # ========================================================
    # Helpers
    # ========================================================

    def challenge_key(self, guild_id: int, user_id: int) -> tuple[int, int]:
        return (guild_id, user_id)

    def clear_challenge(self, guild_id: int, user_id: int):
        self.active_challenges.pop(self.challenge_key(guild_id, user_id), None)

    def _challenge_expired(self, challenge: CaptchaChallenge, expiry_minutes: int) -> bool:
        age = discord.utils.utcnow() - challenge.created_at
        return age.total_seconds() > expiry_minutes * 60

    def account_age_days(self, member: discord.Member) -> int:
        return (discord.utils.utcnow() - member.created_at).days

    def account_is_old_enough(self, member: discord.Member, min_days: int) -> bool:
        return self.account_age_days(member) >= min_days

    async def send_captcha_challenge(
        self,
        interaction: discord.Interaction,
        captcha_text: str,
        max_attempts: int,
        *,
        attempts_used: int = 0,
        existing: bool = False,
    ) -> None:
        captcha_file = await self.captcha.make_captcha_file(captcha_text)
        description = (
            "You already have an active CAPTCHA.\n\n"
            if existing
            else ""
        )
        attempts_line = (
            f"Attempts used: **{attempts_used}/{max_attempts}**\n"
            f"Attempts left: **{max_attempts - attempts_used}**"
            if existing
            else f"You have **{max_attempts} attempts**."
        )

        embed = discord.Embed(
            title="Verification CAPTCHA",
            description=(
                f"{description}"
                "Type the code shown in the image using:\n\n"
                "`/code <code>`\n\n"
                "The code is not case-sensitive.\n"
                f"{attempts_line}"
            ),
            color=discord.Color.blurple(),
        )
        embed.set_footer(text="Only you can see this message.")
        embed.set_image(url="attachment://captcha.png")

        await interaction.response.send_message(
            embed=embed,
            file=captcha_file,
            ephemeral=True,
        )

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

    async def require_verify_channel(
        self,
        interaction: discord.Interaction,
        verification_channel_id: int | None,
    ) -> bool:
        if (
            verification_channel_id is not None
            and interaction.channel_id == verification_channel_id
        ):
            return True

        await interaction.response.send_message(
            f"Please use this command in <#{verification_channel_id}>.",
            ephemeral=True,
        )
        return False

    async def get_ready_config(self, interaction: discord.Interaction):
        """
        Loads and validates bouncer config for the interaction's guild.

        Returns (config, role, channel) if ready, None if setup is incomplete.
        """

        if interaction.guild is None:
            await interaction.response.send_message(
                "This can only be used inside a server.",
                ephemeral=True,
            )
            return None

        config = self.bounce_store.get_bouncer_config(interaction.guild.id)

        if config is None:
            config = BounceConfig(guild_id=interaction.guild.id)

        role = self.get_verified_role(interaction.guild, config)
        channel = self.get_verification_channel(interaction.guild, config)

        missing: list[str] = []
        if role is None:
            missing.append("Verified role")
        if channel is None:
            missing.append("Verification channel")

        if missing:
            await interaction.response.send_message(
                (
                    "This server's bouncer is not fully configured yet.\n\n"
                    "Missing: " + ", ".join(missing) + "\n\n"
                    "An admin can configure this with:\n"
                    "`/bouncer setup set-role`\n"
                    "`/bouncer setup set-channel`"
                ),
                ephemeral=True,
            )
            return None

        return config, role, channel

    # ========================================================
    # Verification flow
    # ========================================================

    async def start_verification(self, interaction: discord.Interaction):
        """
        Starts the verification flow.

        Used by /verify and the persistent panel button.
        Does not create a new challenge if the user already has an active one.
        """

        ready_config = await self.get_ready_config(interaction)

        if ready_config is None:
            return

        config, role, _channel = ready_config

        eff_min_age = config.min_account_age_days if config.min_account_age_days is not None else MIN_ACCOUNT_AGE_DAYS
        eff_max_attempts = config.max_failed_attempts if config.max_failed_attempts is not None else MAX_FAILED_ATTEMPTS
        eff_expiry = config.captcha_expiry_minutes if config.captcha_expiry_minutes is not None else CAPTCHA_EXPIRY_MINUTES

        if not isinstance(interaction.user, discord.Member):
            log(f"BOUNCER // NON-MEMBER USER 『 {interaction.user.id} 』 GUILD 『 {interaction.guild_id} 』", level="debug", logger_name="bouncer")
            await interaction.response.send_message(
                "Could not read your server member data.",
                ephemeral=True,
            )
            return

        if not await self.require_verify_channel(
            interaction, config.verification_channel_id
        ):
            return

        member = interaction.user

        if role in member.roles:
            log(f"BOUNCER // ALREADY VERIFIED 『 {member} | {member.id} 』", level="debug", logger_name="bouncer")
            await interaction.response.send_message(
                "You are already verified.",
                ephemeral=True,
            )
            return

        if not self.account_is_old_enough(member, eff_min_age):
            age = self.account_age_days(member)
            log(f"BOUNCER // ACCOUNT TOO YOUNG 『 {member.id} 』 AGE {age} / {eff_min_age}", level="debug", logger_name="bouncer")
            await interaction.response.send_message(
                (
                    "Your account is not old enough to verify yet.\n\n"
                    f"Required account age: **{eff_min_age} days**\n"
                    f"Your account age: **{age} days**\n\n"
                    "You were **not kicked**. Please come back once your account "
                    "meets the requirement."
                ),
                ephemeral=True,
            )
            return

        key = self.challenge_key(interaction.guild.id, member.id)
        existing = self.active_challenges.get(key)

        if existing is not None:
            if self._challenge_expired(existing, eff_expiry):
                self.clear_challenge(interaction.guild.id, member.id)
            else:
                log(f"BOUNCER // ACTIVE CHALLENGE 『 {member.id} 』 ATTEMPTS {existing.attempts}/{eff_max_attempts}", level="debug", logger_name="bouncer")
                await self.send_captcha_challenge(
                    interaction,
                    existing.answer,
                    eff_max_attempts,
                    attempts_used=existing.attempts,
                    existing=True,
                )
                return

        captcha_text = self.captcha.make_captcha_text()

        self.active_challenges[key] = CaptchaChallenge(answer=captcha_text)
        log(f"BOUNCER // CAPTCHA ISSUED 『 {member.id} 』 GUILD {interaction.guild.id}", level="debug", logger_name="bouncer")

        await self.send_captcha_challenge(
            interaction,
            captcha_text,
            eff_max_attempts,
        )

    async def complete_verification(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        role: discord.Role,
        delay_seconds: float,
    ):
        if interaction.guild is None:
            await interaction.response.send_message(
                "This can only be used inside a server.",
                ephemeral=True,
            )
            return

        self.clear_challenge(interaction.guild.id, member.id)

        if delay_seconds > 0:
            timing_note = f"\n\nYou will be moved into the server in **{delay_seconds:.0f} seconds**."
        else:
            timing_note = ""

        success_embed = discord.Embed(
            title="Verification Complete",
            description=f"You passed verification.{timing_note}",
            color=discord.Color.green(),
        )

        await interaction.response.send_message(
            embed=success_embed,
            ephemeral=True,
        )

        await asyncio.sleep(delay_seconds)

        log(f"BOUNCER // ADDING VERIFIED ROLE 『 {member.id} 』 ROLE 『 {role.id} 』 GUILD 『 {interaction.guild.id} 』", level="debug", logger_name="bouncer")

        try:
            await member.add_roles(role, reason="Passed CAPTCHA verification")
        except discord.Forbidden:
            log(f"BOUNCER // FORBIDDEN ADD ROLE 『 {member.id} 』 GUILD 『 {interaction.guild.id} 』", level="debug", logger_name="bouncer")
            await interaction.followup.send(
                (
                    "I could not give you the Verified role.\n\n"
                    "Make sure my bot role is above the Verified role and that "
                    "I have Manage Roles permission."
                ),
                ephemeral=True,
            )
        except discord.HTTPException:
            await interaction.followup.send(
                "Something went wrong while giving you the Verified role.",
                ephemeral=True,
            )

    async def fail_code_attempt(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        challenge: CaptchaChallenge,
        max_attempts: int,
    ):
        if interaction.guild is None:
            await interaction.response.send_message(
                "This can only be used inside a server.",
                ephemeral=True,
            )
            return

        log(f"BOUNCER // FAILED ATTEMPT 『 {member.id} 』 {challenge.attempts + 1}/{max_attempts}", level="debug", logger_name="bouncer")
        challenge.attempts += 1
        attempts_left = max_attempts - challenge.attempts

        if challenge.attempts < max_attempts:
            await interaction.response.send_message(
                (
                    "Incorrect code.\n\n"
                    f"Attempts used: **{challenge.attempts}/{max_attempts}**\n"
                    f"Attempts left: **{attempts_left}**\n\n"
                    "Use `/code <code>` again with the same CAPTCHA image."
                ),
                ephemeral=True,
            )
            return

        self.clear_challenge(interaction.guild.id, member.id)

        await interaction.response.send_message(
            f"Incorrect code. You failed verification {max_attempts} times and will be kicked.",
            ephemeral=True,
        )

        log(f"BOUNCER // KICKING MEMBER 『 {member.id} 』 GUILD 『 {interaction.guild.id} 』", level="debug", logger_name="bouncer")

        try:
            await member.kick(
                reason=f"Failed CAPTCHA verification {max_attempts} times"
            )
        except discord.Forbidden:
            await interaction.followup.send(
                (
                    "I tried to kick you, but I do not have permission.\n\n"
                    "Make sure I have Kick Members permission and that my role "
                    "is high enough."
                ),
                ephemeral=True,
            )
        except discord.HTTPException:
            await interaction.followup.send(
                "Something went wrong while trying to kick you.",
                ephemeral=True,
            )

    # ========================================================
    # Message guard
    # ========================================================

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.guild is None or message.author.bot:
            return

        guild_id = message.guild.id

        if not self.module_store.is_module_enabled(guild_id, "bouncer"):
            log(f"BOUNCER // MESSAGE GUARD // MODULE DISABLED 『 GUILD {guild_id} 』", level="debug", logger_name="bouncer")
            return

        config = self.bounce_store.get_bouncer_config(guild_id)

        if config is None or config.verification_channel_id is None:
            log(f"BOUNCER // MESSAGE GUARD // NO CHANNEL CONFIGURED 『 GUILD {guild_id} 』", level="debug", logger_name="bouncer")
            return

        if message.channel.id != config.verification_channel_id:
            return

        if not isinstance(message.channel, discord.TextChannel) or message.channel.slowmode_delay == 0:
            now = time.monotonic()
            last_sent = self._slowmode_alert_sent_at.get(guild_id, 0.0)
            if now - last_sent >= _SLOWMODE_ALERT_COOLDOWN_SECONDS:
                self._slowmode_alert_sent_at[guild_id] = now
                await send_alert(
                    self.bot,
                    self.module_store,
                    guild_id,
                    f"⚠ **Bouncer:** Slow-mode is not set on <#{message.channel.id}>. "
                    "Message deletion is disabled until it is configured. "
                    "Run `/bouncer setup set-channel` to restore it.",
                )
            return

        log(f"BOUNCER // MESSAGE GUARD // DELETING 『 MSG {message.id} 』 CHANNEL 『 {message.channel.id} 』", level="debug", logger_name="bouncer")

        try:
            await message.delete()
        except discord.Forbidden:
            log(f"BOUNCER // MESSAGE GUARD // MISSING MANAGE MESSAGES 『 CHANNEL {message.channel.id} 』", level="debug", logger_name="bouncer")

            now = time.monotonic()
            last_sent = self._slowmode_alert_sent_at.get(guild_id, 0.0)
            if now - last_sent >= _SLOWMODE_ALERT_COOLDOWN_SECONDS:
                self._slowmode_alert_sent_at[guild_id] = now
                await send_alert(
                    self.bot,
                    self.module_store,
                    guild_id,
                    f"⚠ **Bouncer:** Missing **Manage Messages** permission in <#{message.channel.id}>. "
                    "Cannot delete messages from unverified members. "
                    "Grant the permission or re-run `/bouncer setup set-channel`.",
                )

            try:
                await message.channel.send(
                    f"{message.author.mention} Please verify before posting in this channel.",
                    delete_after=8,
                )
            except (discord.Forbidden, discord.HTTPException) as e:
                log(f"BOUNCER // MESSAGE GUARD // FALLBACK SEND FAILED 『 CHANNEL {message.channel.id} 』 // {e}", level="debug", logger_name="bouncer")

        except discord.HTTPException as e:
            log(f"BOUNCER // MESSAGE GUARD // DELETE FAILED 『 {e} 』", level="debug", logger_name="bouncer")

    # ========================================================
    # Slash commands
    # ========================================================

    @app_commands.command(name="verify", description="Start server verification.")
    @app_commands.guild_only()
    async def verify(self, interaction: discord.Interaction):
        await self.start_verification(interaction)

    @app_commands.command(
        name="code",
        description="Submit your verification CAPTCHA code.",
    )
    @app_commands.describe(code="The code shown in your private CAPTCHA image.")
    @app_commands.guild_only()
    async def code(self, interaction: discord.Interaction, code: str):
        ready_config = await self.get_ready_config(interaction)

        if ready_config is None:
            return

        config, role, _channel = ready_config

        eff_max_attempts = config.max_failed_attempts if config.max_failed_attempts is not None else MAX_FAILED_ATTEMPTS
        eff_expiry = config.captcha_expiry_minutes if config.captcha_expiry_minutes is not None else CAPTCHA_EXPIRY_MINUTES
        eff_delay = config.verification_role_delay_seconds if config.verification_role_delay_seconds is not None else VERIFICATION_ROLE_DELAY_SECONDS

        if not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message(
                "Could not read your server member data.",
                ephemeral=True,
            )
            return

        if not await self.require_verify_channel(
            interaction, config.verification_channel_id
        ):
            return

        member = interaction.user

        if role in member.roles:
            await interaction.response.send_message(
                "You are already verified.",
                ephemeral=True,
            )
            return

        key = self.challenge_key(interaction.guild.id, member.id)
        challenge = self.active_challenges.get(key)

        if challenge is None or self._challenge_expired(challenge, eff_expiry):
            if challenge is not None:
                self.clear_challenge(interaction.guild.id, member.id)
            log(f"BOUNCER // NO ACTIVE CHALLENGE 『 {member.id} 』 GUILD 『 {interaction.guild.id} 』", level="debug", logger_name="bouncer")
            await interaction.response.send_message(
                "You do not have an active CAPTCHA. Run `/verify` first.",
                ephemeral=True,
            )
            return

        submitted_code = normalize_code(code)

        if submitted_code == challenge.answer:
            await self.complete_verification(interaction, member, role, eff_delay)
        else:
            await self.fail_code_attempt(interaction, member, challenge, eff_max_attempts)


async def setup(bot: commands.Bot):
    await bot.add_cog(Bouncer(bot))
