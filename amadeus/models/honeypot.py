from dataclasses import dataclass


@dataclass
class HoneypotConfig:
    guild_id: int
    channel_id: int | None = None
    action: str | None = None          # 'remove_role' | 'mute' | 'kick' | 'ban'
    action_role_id: int | None = None  # only used with remove_role
    alerts_enabled: bool = True
