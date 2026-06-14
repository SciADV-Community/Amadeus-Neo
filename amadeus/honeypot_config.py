"""
Honeypot module configuration storage.
"""
import sqlite3

from amadeus.database import BaseStore
from amadeus.models.honeypot import HoneypotConfig


class HoneypotConfigStore(BaseStore):
    """SQLite wrapper for honeypot per-server config."""

    def _initialize(self):
        self.db.execute(
            """
            CREATE TABLE IF NOT EXISTS honeypot_config (
                guild_id        INTEGER PRIMARY KEY,
                channel_id      INTEGER,
                action          TEXT,
                action_role_id  INTEGER,
                action_reason   TEXT,
                delete_history_seconds INTEGER,
                alerts_enabled  INTEGER NOT NULL DEFAULT 1,
                updated_at      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        try:
            self.db.execute("ALTER TABLE honeypot_config ADD COLUMN action_reason TEXT")
        except sqlite3.OperationalError:
            pass  # column already exists
        try:
            self.db.execute("ALTER TABLE honeypot_config ADD COLUMN delete_history_seconds INTEGER")
        except sqlite3.OperationalError:
            pass  # column already exists
        self.db.commit()

    def get_config(self, guild_id: int) -> HoneypotConfig | None:
        """Returns the HoneypotConfig for a guild, or None if not yet configured."""
        row = self.db.execute(
            """
            SELECT guild_id, channel_id, action, action_role_id, action_reason,
                   delete_history_seconds, alerts_enabled
            FROM honeypot_config WHERE guild_id = ?
            """,
            (guild_id,),
        ).fetchone()

        if row is None:
            return None

        return HoneypotConfig(
            guild_id=row["guild_id"],
            channel_id=row["channel_id"],
            action=row["action"],
            action_role_id=row["action_role_id"],
            action_reason=row["action_reason"],
            delete_history_seconds=row["delete_history_seconds"],
            alerts_enabled=bool(row["alerts_enabled"]),
        )

    def _upsert(self, guild_id: int, column: str, value: object) -> None:
        """Upserts a hardcoded column in honeypot_config."""
        self.db.execute(
            f"""
            INSERT INTO honeypot_config (guild_id, {column}) VALUES (?, ?)
            ON CONFLICT(guild_id) DO UPDATE SET
                {column} = excluded.{column},
                updated_at = CURRENT_TIMESTAMP
            """,
            (guild_id, value),
        )
        self.db.commit()

    def set_channel(self, guild_id: int, channel_id: int) -> None:
        """Persists the trap channel ID for the guild."""
        self._upsert(guild_id, "channel_id", channel_id)

    def set_action(
        self,
        guild_id: int,
        action: str,
        role_id: int | None = None,
        reason: str | None = None,
        delete_history_seconds: int | None = None,
    ) -> None:
        """Persists the moderation action and optional target role."""
        self.db.execute(
            """
            INSERT INTO honeypot_config (
                guild_id, action, action_role_id, action_reason, delete_history_seconds
            )
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(guild_id) DO UPDATE SET
                action = excluded.action,
                action_role_id = excluded.action_role_id,
                action_reason = excluded.action_reason,
                delete_history_seconds = excluded.delete_history_seconds,
                updated_at = CURRENT_TIMESTAMP
            """,
            (guild_id, action, role_id, reason, delete_history_seconds),
        )
        self.db.commit()

    def set_alerts_enabled(self, guild_id: int, enabled: bool) -> None:
        """Enables or disables admin channel alerts when the honeypot is triggered."""
        self._upsert(guild_id, "alerts_enabled", int(enabled))
