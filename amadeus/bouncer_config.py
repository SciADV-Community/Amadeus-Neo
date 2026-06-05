"""
Bouncer module configuration storage.
"""
import sqlite3

from amadeus.database import BaseStore
from amadeus.models.bouncer import BounceConfig


class BounceConfigStore(BaseStore):
    """SQLite wrapper for bouncer-specific per-server config."""

    def _initialize(self):
        self.db.execute(
            """
            CREATE TABLE IF NOT EXISTS bouncer_config (
                guild_id                INTEGER PRIMARY KEY,
                verified_role_id        INTEGER,
                verification_channel_id INTEGER,
                updated_at              TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        _new_columns = [
            "min_account_age_days           INTEGER",
            "max_failed_attempts            INTEGER",
            "captcha_expiry_minutes         INTEGER",
            "panel_image_url                TEXT",
            "verification_role_delay_seconds REAL",
        ]
        for col_def in _new_columns:
            try:
                self.db.execute(f"ALTER TABLE bouncer_config ADD COLUMN {col_def}")
            except sqlite3.OperationalError:
                pass
        self.db.commit()

    def get_bouncer_config(self, guild_id: int) -> BounceConfig | None:
        """Returns the BounceConfig for a guild, or None if not yet configured."""
        row = self.db.execute(
            """
            SELECT guild_id, verified_role_id, verification_channel_id,
                   min_account_age_days, max_failed_attempts, captcha_expiry_minutes,
                   panel_image_url, verification_role_delay_seconds
            FROM bouncer_config WHERE guild_id = ?
            """,
            (guild_id,),
        ).fetchone()

        if row is None:
            return None

        return BounceConfig(
            guild_id=row["guild_id"],
            verified_role_id=row["verified_role_id"],
            verification_channel_id=row["verification_channel_id"],
            min_account_age_days=row["min_account_age_days"],
            max_failed_attempts=row["max_failed_attempts"],
            captcha_expiry_minutes=row["captcha_expiry_minutes"],
            panel_image_url=row["panel_image_url"],
            verification_role_delay_seconds=row["verification_role_delay_seconds"],
        )

    def ensure_bouncer_config(self, guild_id: int) -> BounceConfig:
        """Creates a bouncer config row for the guild if needed, then returns it."""
        self.db.execute(
            "INSERT OR IGNORE INTO bouncer_config (guild_id) VALUES (?)",
            (guild_id,),
        )
        self.db.commit()
        return self.get_bouncer_config(guild_id)

    def set_verified_role(self, guild_id: int, role_id: int) -> None:
        """Persists the role granted to members upon successful verification."""
        self.db.execute(
            """
            INSERT INTO bouncer_config (guild_id, verified_role_id) VALUES (?, ?)
            ON CONFLICT(guild_id) DO UPDATE SET
                verified_role_id = excluded.verified_role_id,
                updated_at = CURRENT_TIMESTAMP
            """,
            (guild_id, role_id),
        )
        self.db.commit()

    def set_verification_channel(self, guild_id: int, channel_id: int) -> None:
        """Persists the channel where members complete CAPTCHA verification."""
        self.db.execute(
            """
            INSERT INTO bouncer_config (guild_id, verification_channel_id) VALUES (?, ?)
            ON CONFLICT(guild_id) DO UPDATE SET
                verification_channel_id = excluded.verification_channel_id,
                updated_at = CURRENT_TIMESTAMP
            """,
            (guild_id, channel_id),
        )
        self.db.commit()

    def _upsert_setting(self, guild_id: int, column: str, value: object) -> None:
        """Upserts a hardcoded column in bouncer_config."""
        self.db.execute(
            f"""
            INSERT INTO bouncer_config (guild_id, {column}) VALUES (?, ?)
            ON CONFLICT(guild_id) DO UPDATE SET
                {column} = excluded.{column},
                updated_at = CURRENT_TIMESTAMP
            """,
            (guild_id, value),
        )
        self.db.commit()

    def set_min_account_age_days(self, guild_id: int, value: int) -> None:
        self._upsert_setting(guild_id, "min_account_age_days", value)

    def set_max_failed_attempts(self, guild_id: int, value: int) -> None:
        self._upsert_setting(guild_id, "max_failed_attempts", value)

    def set_captcha_expiry_minutes(self, guild_id: int, value: int) -> None:
        self._upsert_setting(guild_id, "captcha_expiry_minutes", value)

    def set_panel_image_url(self, guild_id: int, url: str | None) -> None:
        self._upsert_setting(guild_id, "panel_image_url", url)

    def set_verification_role_delay(self, guild_id: int, seconds: int) -> None:
        self._upsert_setting(guild_id, "verification_role_delay_seconds", float(seconds))
