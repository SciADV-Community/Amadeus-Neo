import time
from dataclasses import dataclass, field

import discord
from discord.ext import commands

from amadeus.alerts import send_alert
from amadeus.activity_store import ActivityStore
from amadeus.database import ConfigStore
from amadeus.logging_utils import log

_ROLE_ALERT_COOLDOWN_SECONDS = 600  # 10 minutes per guild


# ============================================================
# Per-guild config cache
# ============================================================

@dataclass
class _GuildCache:
    cooldown_seconds: int
    includes: set[int]
    excludes: set[int]
    tiers: list[tuple[int, int]]  # sorted ascending by threshold


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

    def cog_unload(self):
        self.activity_store.close()
        self.module_store.close()

    def invalidate_cache(self, guild_id: int) -> None:
        self._cache.pop(guild_id, None)

    def _get_cache(self, guild_id: int) -> _GuildCache:
        if guild_id not in self._cache:
            cooldown = self.activity_store.get_cooldown(guild_id)
            includes, excludes = self.activity_store.get_channels(guild_id)
            tiers = self.activity_store.get_tiers(guild_id)
            self._cache[guild_id] = _GuildCache(
                cooldown_seconds=cooldown,
                includes=includes,
                excludes=excludes,
                tiers=tiers,
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

        new_count = self.activity_store.increment_count(guild_id, user_id)

        if not cache.tiers or not isinstance(message.author, discord.Member):
            return

        member = message.author

        for threshold, role_id in cache.tiers:
            if new_count < threshold:
                break  # tiers are sorted; none above will match

            role = message.guild.get_role(role_id)

            if role is None or role in member.roles:
                continue

            try:
                await member.add_roles(role, reason=f"Activity milestone: {threshold} messages")
                log(
                    f"ACTIVITY // ROLE ASSIGNED 『 USER {user_id} 』 ROLE 『 {role_id} 』 "
                    f"THRESHOLD {threshold} GUILD 『 {guild_id} 』",
                    level="debug",
                    logger_name="activity",
                )
            except discord.Forbidden:
                log(
                    f"ACTIVITY // FORBIDDEN ASSIGN ROLE 『 {role_id} 』 GUILD 『 {guild_id} 』",
                    level="debug",
                    logger_name="activity",
                )
                now = time.monotonic()
                if now - self._role_alert_sent_at.get(guild_id, 0.0) >= _ROLE_ALERT_COOLDOWN_SECONDS:
                    self._role_alert_sent_at[guild_id] = now
                    await send_alert(
                        self.bot,
                        self.module_store,
                        guild_id,
                        f"⚠ **Activity:** Missing permission to assign <@&{role_id}> "
                        f"(milestone: {threshold} messages). "
                        "Check the bot's role hierarchy and Manage Roles permission.",
                    )
            except discord.HTTPException as e:
                log(
                    f"ACTIVITY // HTTP ERROR 『 {e} 』 GUILD 『 {guild_id} 』",
                    level="debug",
                    logger_name="activity",
                )


async def setup(bot: commands.Bot):
    await bot.add_cog(Activity(bot))
