from dataclasses import dataclass


@dataclass
class BounceConfig:
    guild_id: int
    verified_role_id: int | None = None
    verification_channel_id: int | None = None
    min_account_age_days: int | None = None
    max_failed_attempts: int | None = None
    captcha_expiry_minutes: int | None = None
    panel_image_url: str | None = None
    verification_role_delay_seconds: float | None = None
