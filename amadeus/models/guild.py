from dataclasses import dataclass


@dataclass
class GuildConfig:
    guild_id: int
    owner_id: int
    admin_role_id: int | None = None
    alert_channel_id: int | None = None
