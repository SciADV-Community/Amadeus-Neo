from dataclasses import dataclass


@dataclass(slots=True)
class BoostGrant:
    guild_id: int
    user_id: int
    tier: int
    role_id: int | None = None
    emoji_1_id: int | None = None
    emoji_2_id: int | None = None
