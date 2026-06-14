"""
Activity module storage.
"""
import sqlite3

from amadeus.database import BaseStore


class ActivityStore(BaseStore):
    """SQLite wrapper for activity tracking."""

    def _initialize(self):
        self.db.execute(
            """
            CREATE TABLE IF NOT EXISTS activity_config (
                guild_id         INTEGER PRIMARY KEY,
                cooldown_seconds INTEGER NOT NULL DEFAULT 5,
                role_swap_enabled INTEGER NOT NULL DEFAULT 0,
                updated_at       TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        try:
            self.db.execute("ALTER TABLE activity_config ADD COLUMN role_swap_enabled INTEGER NOT NULL DEFAULT 0")
        except sqlite3.OperationalError:
            pass
        self.db.execute(
            """
            CREATE TABLE IF NOT EXISTS activity_channels (
                guild_id   INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                mode       TEXT NOT NULL,
                PRIMARY KEY (guild_id, channel_id)
            )
            """
        )
        self.db.execute(
            """
            CREATE TABLE IF NOT EXISTS activity_tiers (
                guild_id  INTEGER NOT NULL,
                threshold INTEGER NOT NULL,
                role_id   INTEGER NOT NULL,
                PRIMARY KEY (guild_id, threshold)
            )
            """
        )
        self.db.execute(
            """
            CREATE TABLE IF NOT EXISTS activity_counts (
                guild_id INTEGER NOT NULL,
                user_id  INTEGER NOT NULL,
                count    INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (guild_id, user_id)
            )
            """
        )
        self.db.commit()

    def get_cooldown(self, guild_id: int) -> int:
        row = self.db.execute(
            "SELECT cooldown_seconds FROM activity_config WHERE guild_id = ?",
            (guild_id,),
        ).fetchone()
        return row["cooldown_seconds"] if row else 5

    def get_role_swap_enabled(self, guild_id: int) -> bool:
        row = self.db.execute(
            "SELECT role_swap_enabled FROM activity_config WHERE guild_id = ?",
            (guild_id,),
        ).fetchone()
        return bool(row["role_swap_enabled"]) if row else False

    def set_cooldown(self, guild_id: int, seconds: int) -> None:
        self.db.execute(
            """
            INSERT INTO activity_config (guild_id, cooldown_seconds) VALUES (?, ?)
            ON CONFLICT(guild_id) DO UPDATE SET
                cooldown_seconds = excluded.cooldown_seconds,
                updated_at = CURRENT_TIMESTAMP
            """,
            (guild_id, seconds),
        )
        self.db.commit()

    def set_role_swap_enabled(self, guild_id: int, enabled: bool) -> None:
        self.db.execute(
            """
            INSERT INTO activity_config (guild_id, role_swap_enabled) VALUES (?, ?)
            ON CONFLICT(guild_id) DO UPDATE SET
                role_swap_enabled = excluded.role_swap_enabled,
                updated_at = CURRENT_TIMESTAMP
            """,
            (guild_id, int(enabled)),
        )
        self.db.commit()

    def add_channel(self, guild_id: int, channel_id: int, mode: str) -> None:
        self.db.execute(
            """
            INSERT INTO activity_channels (guild_id, channel_id, mode) VALUES (?, ?, ?)
            ON CONFLICT(guild_id, channel_id) DO UPDATE SET mode = excluded.mode
            """,
            (guild_id, channel_id, mode),
        )
        self.db.commit()

    def remove_channel(self, guild_id: int, channel_id: int) -> bool:
        cursor = self.db.execute(
            "DELETE FROM activity_channels WHERE guild_id = ? AND channel_id = ?",
            (guild_id, channel_id),
        )
        self.db.commit()
        return cursor.rowcount > 0

    def get_channels(self, guild_id: int) -> tuple[set[int], set[int]]:
        rows = self.db.execute(
            "SELECT channel_id, mode FROM activity_channels WHERE guild_id = ?",
            (guild_id,),
        ).fetchall()
        includes = {r["channel_id"] for r in rows if r["mode"] == "include"}
        excludes = {r["channel_id"] for r in rows if r["mode"] == "exclude"}
        return includes, excludes

    def get_channels_raw(self, guild_id: int) -> list:
        return self.db.execute(
            "SELECT channel_id, mode FROM activity_channels WHERE guild_id = ? ORDER BY mode, channel_id",
            (guild_id,),
        ).fetchall()

    def add_tier(self, guild_id: int, threshold: int, role_id: int) -> None:
        self.db.execute(
            """
            INSERT INTO activity_tiers (guild_id, threshold, role_id) VALUES (?, ?, ?)
            ON CONFLICT(guild_id, threshold) DO UPDATE SET role_id = excluded.role_id
            """,
            (guild_id, threshold, role_id),
        )
        self.db.commit()

    def remove_tier(self, guild_id: int, threshold: int) -> bool:
        cursor = self.db.execute(
            "DELETE FROM activity_tiers WHERE guild_id = ? AND threshold = ?",
            (guild_id, threshold),
        )
        self.db.commit()
        return cursor.rowcount > 0

    def get_tiers(self, guild_id: int) -> list[tuple[int, int]]:
        rows = self.db.execute(
            "SELECT threshold, role_id FROM activity_tiers WHERE guild_id = ? ORDER BY threshold",
            (guild_id,),
        ).fetchall()
        return [(r["threshold"], r["role_id"]) for r in rows]

    def increment_count(self, guild_id: int, user_id: int) -> int:
        self.db.execute(
            """
            INSERT INTO activity_counts (guild_id, user_id, count) VALUES (?, ?, 1)
            ON CONFLICT(guild_id, user_id) DO UPDATE SET count = count + 1
            """,
            (guild_id, user_id),
        )
        self.db.commit()
        row = self.db.execute(
            "SELECT count FROM activity_counts WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
        ).fetchone()
        return row["count"]

    def get_count(self, guild_id: int, user_id: int) -> int:
        row = self.db.execute(
            "SELECT count FROM activity_counts WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
        ).fetchone()
        return row["count"] if row else 0

    def get_leaderboard(self, guild_id: int, limit: int = 10) -> list[tuple[int, int]]:
        rows = self.db.execute(
            """
            SELECT user_id, count
            FROM activity_counts
            WHERE guild_id = ?
            ORDER BY count DESC, user_id ASC
            LIMIT ?
            """,
            (guild_id, limit),
        ).fetchall()
        return [(row["user_id"], row["count"]) for row in rows]
