import asyncio
import time
from dataclasses import dataclass
from datetime import time as datetime_time, timezone

import discord
from discord.ext import commands, tasks

from amadeus.alerts import send_alert
from amadeus.activity_store import ActivityStore, previous_utc_month_key, utc_month_key
from amadeus.database import ConfigStore
from amadeus.logging_utils import log

_ROLE_ALERT_COOLDOWN_SECONDS = 600  # 10 minutes per guild


# ============================================================
# Per-guild config cache
# ============================================================

@dataclass
class _GuildCache:
    cooldown_seconds: int
    role_swap_enabled: bool
    includes: set[int]
    excludes: set[int]
    tiers: list[tuple[int, int]]  # sorted ascending by threshold
    monthly_tiers: list[tuple[int, int]]  # sorted ascending by threshold


# ============================================================
# Activity cog
# ============================================================

class Activity(commands.Cog):
    """
    Message activity tracking.

    Counts messages per user (subject to a per-user cooldown) and assigns
    configured roles when thresholds are crossed.

    Channel filtering:
        If any channel is on the include list, only those channels count.
        Otherwise, all channels count except those on the exclude list.
    """

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.activity_store = ActivityStore()
        self.module_store = ConfigStore()

        # (guild_id, user_id) → monotonic timestamp of last counted message.
        # In-memory only; resets on restart (acceptable — avoids a DB read per message).
        self._last_counted: dict[tuple[int, int], float] = {}

        # Per-guild cache for tiers, channel filters, and cooldown.
        # Invalidated by the admin cog whenever config changes.
        self._cache: dict[int, _GuildCache] = {}

        # Tracks the last time a role-assign failure alert was sent per guild.
        self._role_alert_sent_at: dict[int, float] = {}

        # Prevent on_ready and the scheduled midnight pass from overlapping.
        self._monthly_rollover_lock = asyncio.Lock()

    async def cog_load(self) -> None:
        if not self._monthly_role_rollover.is_running():
            self._monthly_role_rollover.start()

    def cog_unload(self):
        self._monthly_role_rollover.cancel()
        self.activity_store.close()
        self.module_store.close()

    def invalidate_cache(self, guild_id: int) -> None:
        self._cache.pop(guild_id, None)

    def _get_cache(self, guild_id: int) -> _GuildCache:
        if guild_id not in self._cache:
            cooldown = self.activity_store.get_cooldown(guild_id)
            role_swap_enabled = self.activity_store.get_role_swap_enabled(guild_id)
            includes, excludes = self.activity_store.get_channels(guild_id)
            tiers = self.activity_store.get_tiers(guild_id)
            monthly_tiers = self.activity_store.get_tiers(guild_id, monthly=True)
            self._cache[guild_id] = _GuildCache(
                cooldown_seconds=cooldown,
                role_swap_enabled=role_swap_enabled,
                includes=includes,
                excludes=excludes,
                tiers=tiers,
                monthly_tiers=monthly_tiers,
            )
        return self._cache[guild_id]

    def _channel_allowed(self, guild_id: int, channel_id: int) -> bool:
        cache = self._get_cache(guild_id)
        if cache.includes:
            return channel_id in cache.includes
        return channel_id not in cache.excludes

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.guild is None or message.author.bot:
            return

        guild_id = message.guild.id

        if not self.module_store.is_module_enabled(guild_id, "activity"):
            return

        if not self._channel_allowed(guild_id, message.channel.id):
            return

        user_id = message.author.id
        key = (guild_id, user_id)
        now = time.monotonic()
        cache = self._get_cache(guild_id)

        if now - self._last_counted.get(key, 0.0) < cache.cooldown_seconds:
            return

        self._last_counted[key] = now

        all_time_count, _monthly_count = self.activity_store.increment_counts(guild_id, user_id)

        if not isinstance(message.author, discord.Member):
            return

        member = message.author
        await self._apply_tiers(
            message.guild,
            member,
            cache.tiers,
            all_time_count,
            role_swap_enabled=cache.role_swap_enabled,
            monthly=False,
        )

    async def _apply_tiers(
        self,
        guild: discord.Guild,
        member: discord.Member,
        tiers: list[tuple[int, int]],
        count: int,
        *,
        role_swap_enabled: bool,
        monthly: bool,
    ) -> None:
        if not tiers:
            return
        if role_swap_enabled:
            await self._apply_role_swap(guild, member, tiers, count, monthly=monthly)
            return

        scope_label = "Monthly activity" if monthly else "Activity"
        for threshold, role_id in tiers:
            if count < threshold:
                break  # tiers are sorted; none above will match

            role = guild.get_role(role_id)

            if role is None or role in member.roles:
                continue

            try:
                await member.add_roles(role, reason=f"{scope_label} milestone: {threshold} messages")
                log(
                    f"ACTIVITY // ROLE ASSIGNED 『 USER {member.id} 』 ROLE 『 {role_id} 』 "
                    f"THRESHOLD {threshold} GUILD 『 {guild.id} 』 SCOPE 『 {scope_label} 』",
                    level="debug",
                    logger_name="activity",
                )
            except discord.Forbidden:
                log(
                    f"ACTIVITY // FORBIDDEN ASSIGN ROLE 『 {role_id} 』 GUILD 『 {guild.id} 』",
                    level="debug",
                    logger_name="activity",
                )
                now = time.monotonic()
                if now - self._role_alert_sent_at.get(guild.id, 0.0) >= _ROLE_ALERT_COOLDOWN_SECONDS:
                    self._role_alert_sent_at[guild.id] = now
                    await send_alert(
                        self.bot,
                        self.module_store,
                        guild.id,
                        f"⚠ **{scope_label}:** Missing permission to assign <@&{role_id}> "
                        f"(milestone: {threshold} messages). "
                        "Check the bot's role hierarchy and Manage Roles permission.",
                    )
            except discord.HTTPException as e:
                log(
                    f"ACTIVITY // HTTP ERROR 『 {e} 』 GUILD 『 {guild.id} 』",
                    level="debug",
                    logger_name="activity",
                )

    async def _apply_role_swap(
        self,
        guild: discord.Guild,
        member: discord.Member,
        tiers: list[tuple[int, int]],
        count: int,
        *,
        monthly: bool = False,
    ) -> None:
        eligible = [(threshold, role_id) for threshold, role_id in tiers if count >= threshold]
        if not eligible:
            return

        threshold, current_role_id = eligible[-1]
        scope_label = "Monthly activity" if monthly else "Activity"
        current_role = guild.get_role(current_role_id)
        if current_role is None:
            return

        previous_roles = []
        configured_role_ids = {role_id for _, role_id in tiers}
        for role in member.roles:
            if role.id in configured_role_ids and role.id != current_role_id:
                previous_roles.append(role)

        try:
            if current_role not in member.roles:
                await member.add_roles(current_role, reason=f"{scope_label} milestone: {threshold} messages")
                log(
                    f"ACTIVITY // ROLE ASSIGNED 『 USER {member.id} 』 ROLE 『 {current_role_id} 』 "
                    f"THRESHOLD {threshold} GUILD 『 {guild.id} 』",
                    level="debug",
                    logger_name="activity",
                )
            if previous_roles:
                await member.remove_roles(
                    *previous_roles,
                    reason=f"{scope_label} role swap: {threshold} messages",
                )
                log(
                    f"ACTIVITY // ROLE SWAP REMOVED 『 USER {member.id} 』 ROLES 『 {[role.id for role in previous_roles]} 』 "
                    f"GUILD 『 {guild.id} 』",
                    level="debug",
                    logger_name="activity",
                )
        except discord.Forbidden:
            log(
                f"ACTIVITY // FORBIDDEN ROLE SWAP 『 ROLE {current_role_id} 』 GUILD 『 {guild.id} 』",
                level="debug",
                logger_name="activity",
            )
            now = time.monotonic()
            if now - self._role_alert_sent_at.get(guild.id, 0.0) >= _ROLE_ALERT_COOLDOWN_SECONDS:
                self._role_alert_sent_at[guild.id] = now
                await send_alert(
                    self.bot,
                    self.module_store,
                    guild.id,
                    f"⚠ **Activity:** Missing permission to apply role swap for <@&{current_role_id}> "
                    f"(milestone: {threshold} messages). "
                    "Check the bot's role hierarchy and Manage Roles permission.",
                )
        except discord.HTTPException as e:
            log(
                f"ACTIVITY // ROLE SWAP HTTP ERROR 『 {e} 』 GUILD 『 {guild.id} 』",
                level="debug",
                logger_name="activity",
            )

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        """Catch up a rollover that occurred while the bot was offline."""
        await self._ensure_monthly_roles_current()

    @tasks.loop(time=datetime_time(hour=0, minute=0, tzinfo=timezone.utc))
    async def _monthly_role_rollover(self) -> None:
        # The period marker makes this a no-op after a successful first-of-month
        # run. Running daily lets a failed or disabled rollover safely catch up.
        await self._ensure_monthly_roles_current()

    @_monthly_role_rollover.before_loop
    async def _before_monthly_role_rollover(self) -> None:
        await self.bot.wait_until_ready()

    async def _ensure_monthly_roles_current(self) -> None:
        """Reconcile monthly tier roles once for the current UTC month."""
        now = discord.utils.utcnow()
        period = utc_month_key(now)
        completed_period = previous_utc_month_key(now)
        async with self._monthly_rollover_lock:
            for guild in list(self.bot.guilds):
                if not self.module_store.is_module_enabled(guild.id, "activity"):
                    continue
                if self.activity_store.get_monthly_roles_period(guild.id) == period:
                    continue
                succeeded = await self._reconcile_monthly_roles(guild, completed_period)
                if succeeded:
                    self.activity_store.set_monthly_roles_period(guild.id, period)

    async def _reconcile_monthly_roles(self, guild: discord.Guild, period: str) -> bool:
        """Make monthly role membership match counts in ``period``."""
        cache = self._get_cache(guild.id)
        if not cache.monthly_tiers:
            return True

        counts = self.activity_store.get_counts(guild.id, monthly=True, month=period)
        desired_by_role: dict[int, set[int]] = {
            role_id: set() for _, role_id in cache.monthly_tiers
        }
        for user_id, count in counts.items():
            eligible = [tier for tier in cache.monthly_tiers if count >= tier[0]]
            if cache.role_swap_enabled and eligible:
                eligible = [eligible[-1]]
            for _, role_id in eligible:
                desired_by_role[role_id].add(user_id)

        succeeded = True
        for role_id, desired_user_ids in desired_by_role.items():
            role = guild.get_role(role_id)
            if role is None:
                succeeded = False
                await send_alert(
                    self.bot,
                    self.module_store,
                    guild.id,
                    f"⚠ **Monthly activity:** Configured role `{role_id}` no longer exists. "
                    "The rollover was not marked complete; replace or remove that tier.",
                )
                continue
            current_members = {member.id: member for member in role.members}
            for user_id, member in current_members.items():
                if user_id in desired_user_ids:
                    continue
                succeeded = (
                    await self._change_monthly_role(member, role, add=False, period=period)
                    and succeeded
                )
            for user_id in desired_user_ids - current_members.keys():
                member = guild.get_member(user_id)
                if member is None:
                    try:
                        member = await guild.fetch_member(user_id)
                    except discord.NotFound:
                        # A member who left cannot receive the role and does not
                        # make the guild's reconciliation retry forever.
                        continue
                    except discord.HTTPException as error:
                        succeeded = False
                        log(
                            f"ACTIVITY // MONTHLY MEMBER FETCH FAILED 『 USER {user_id} 』 "
                            f"GUILD 『 {guild.id} 』 // {error}",
                            level="warning",
                            logger_name="activity",
                        )
                        continue
                if member is not None and not member.bot:
                    succeeded = (
                        await self._change_monthly_role(member, role, add=True, period=period)
                        and succeeded
                    )
        return succeeded

    async def _change_monthly_role(
        self,
        member: discord.Member,
        role: discord.Role,
        *,
        add: bool,
        period: str,
    ) -> bool:
        action = member.add_roles if add else member.remove_roles
        verb = "ASSIGNED" if add else "REMOVED"
        try:
            await action(role, reason=f"Monthly activity rollover: {period}")
            log(
                f"ACTIVITY // MONTHLY ROLE {verb} 『 USER {member.id} 』 "
                f"ROLE 『 {role.id} 』 GUILD 『 {member.guild.id} 』 PERIOD 『 {period} 』",
                level="debug",
                logger_name="activity",
            )
            return True
        except (discord.Forbidden, discord.HTTPException) as error:
            log(
                f"ACTIVITY // MONTHLY ROLE {verb} FAILED 『 USER {member.id} 』 "
                f"ROLE 『 {role.id} 』 GUILD 『 {member.guild.id} 』 // {error}",
                level="warning",
                logger_name="activity",
            )
            now = time.monotonic()
            guild_id = member.guild.id
            if now - self._role_alert_sent_at.get(guild_id, 0.0) >= _ROLE_ALERT_COOLDOWN_SECONDS:
                self._role_alert_sent_at[guild_id] = now
                action_text = "assign" if add else "remove"
                await send_alert(
                    self.bot,
                    self.module_store,
                    guild_id,
                    f"⚠ **Monthly activity:** Failed to {action_text} <@&{role.id}> "
                    f"for <@{member.id}> during the **{period}** rollover. "
                    "Check the bot's role hierarchy and Manage Roles permission.",
                )
            return False


async def setup(bot: commands.Bot):
    await bot.add_cog(Activity(bot))
