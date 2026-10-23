"""Activity module storage."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from amadeus.database import BaseStore


ALL_TIME_PERIOD = "all_time"
ALL_TIME_SCOPE = "all_time"
MONTHLY_SCOPE = "monthly"


def utc_month_key(now: datetime | None = None) -> str:
    """Return the canonical storage key for a UTC calendar month."""
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc).strftime("%Y-%m")


def previous_utc_month_key(now: datetime | None = None) -> str:
    """Return the UTC month immediately before ``now``."""
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    current = current.astimezone(timezone.utc)
    return (current.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")


class ActivityStore(BaseStore):
    """SQLite wrapper for all-time and monthly activity tracking."""

    def _initialize(self):
        self.db.execute(
            """
            CREATE TABLE IF NOT EXISTS activity_config (
                guild_id         INTEGER PRIMARY KEY,
                cooldown_seconds INTEGER NOT NULL DEFAULT 5,
                role_swap_enabled INTEGER NOT NULL DEFAULT 0,
                monthly_roles_period TEXT,
                updated_at       TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        for column, definition in (
            ("role_swap_enabled", "INTEGER NOT NULL DEFAULT 0"),
            ("monthly_roles_period", "TEXT"),
        ):
            self.add_column_if_missing("activity_config", column, definition)

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

        # TEMPORARY MIGRATION — remove after all deployed databases have upgraded.
        # Legacy tables had no scope/period column, so their rows are all-time data.
        self._migrate_legacy_activity_tables()

        self.db.execute(
            """
            CREATE TABLE IF NOT EXISTS activity_tiers (
                guild_id  INTEGER NOT NULL,
                scope     TEXT NOT NULL CHECK(scope IN ('all_time', 'monthly')),
                threshold INTEGER NOT NULL,
                role_id   INTEGER NOT NULL,
                PRIMARY KEY (guild_id, scope, threshold)
            )
            """
        )
        self.db.execute(
            """
            CREATE TABLE IF NOT EXISTS activity_counts (
                guild_id INTEGER NOT NULL,
                user_id  INTEGER NOT NULL,
                period   TEXT NOT NULL,
                count    INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (guild_id, user_id, period)
            )
            """
        )
        self.db.execute(
            "CREATE INDEX IF NOT EXISTS activity_counts_leaderboard "
            "ON activity_counts (guild_id, period, count DESC, user_id ASC)"
        )
        # TEMPORARY MIGRATION — legacy releases allowed one role to be assigned
        # to multiple thresholds/scopes. Keep the first configuration so a
        # database-level uniqueness guarantee can be installed safely.
        self.db.execute(
            """
            DELETE FROM activity_tiers
            WHERE rowid NOT IN (
                SELECT MIN(rowid) FROM activity_tiers GROUP BY guild_id, role_id
            )
            """
        )
        self.db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS activity_tiers_unique_role "
            "ON activity_tiers (guild_id, role_id)"
        )
        self.db.commit()

    def _migrate_legacy_activity_tables(self) -> None:
        tier_columns = self.table_columns("activity_tiers")
        if tier_columns and "scope" not in tier_columns:
            with self.db:
                self.db.execute("ALTER TABLE activity_tiers RENAME TO activity_tiers_legacy")
                self.db.execute(
                    """
                    CREATE TABLE activity_tiers (
                        guild_id  INTEGER NOT NULL,
                        scope     TEXT NOT NULL CHECK(scope IN ('all_time', 'monthly')),
                        threshold INTEGER NOT NULL,
                        role_id   INTEGER NOT NULL,
                        PRIMARY KEY (guild_id, scope, threshold)
                    )
                    """
                )
                self.db.execute(
                    """
                    INSERT INTO activity_tiers (guild_id, scope, threshold, role_id)
                    SELECT guild_id, 'all_time', threshold, role_id
                    FROM activity_tiers_legacy
                    """
                )
                self.db.execute("DROP TABLE activity_tiers_legacy")

        count_columns = self.table_columns("activity_counts")
        if count_columns and "period" not in count_columns:
            with self.db:
                self.db.execute("ALTER TABLE activity_counts RENAME TO activity_counts_legacy")
                self.db.execute(
                    """
                    CREATE TABLE activity_counts (
                        guild_id INTEGER NOT NULL,
                        user_id  INTEGER NOT NULL,
                        period   TEXT NOT NULL,
                        count    INTEGER NOT NULL DEFAULT 0,
                        PRIMARY KEY (guild_id, user_id, period)
                    )
                    """
                )
                self.db.execute(
                    """
                    INSERT INTO activity_counts (guild_id, user_id, period, count)
                    SELECT guild_id, user_id, 'all_time', count
                    FROM activity_counts_legacy
                    """
                )
                self.db.execute("DROP TABLE activity_counts_legacy")

    @staticmethod
    def _scope(monthly: bool) -> str:
        return MONTHLY_SCOPE if monthly else ALL_TIME_SCOPE

    @staticmethod
    def _period(monthly: bool, month: str | None = None) -> str:
        return (month or utc_month_key()) if monthly else ALL_TIME_PERIOD

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

    def get_monthly_roles_period(self, guild_id: int) -> str | None:
        row = self.db.execute(
            "SELECT monthly_roles_period FROM activity_config WHERE guild_id = ?",
            (guild_id,),
        ).fetchone()
        return row["monthly_roles_period"] if row else None

    def set_monthly_roles_period(self, guild_id: int, period: str) -> None:
        self.db.execute(
            """
            INSERT INTO activity_config (guild_id, monthly_roles_period) VALUES (?, ?)
            ON CONFLICT(guild_id) DO UPDATE SET
                monthly_roles_period = excluded.monthly_roles_period,
                updated_at = CURRENT_TIMESTAMP
            """,
            (guild_id, period),
        )
        self.db.commit()

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

    def add_tier(self, guild_id: int, threshold: int, role_id: int, *, monthly: bool = False) -> None:
        self.db.execute(
            """
            INSERT INTO activity_tiers (guild_id, scope, threshold, role_id) VALUES (?, ?, ?, ?)
            ON CONFLICT(guild_id, scope, threshold) DO UPDATE SET role_id = excluded.role_id
            """,
            (guild_id, self._scope(monthly), threshold, role_id),
        )
        self.db.commit()

    def remove_tier(self, guild_id: int, threshold: int, *, monthly: bool = False) -> bool:
        cursor = self.db.execute(
            "DELETE FROM activity_tiers WHERE guild_id = ? AND scope = ? AND threshold = ?",
            (guild_id, self._scope(monthly), threshold),
        )
        self.db.commit()
        return cursor.rowcount > 0

    def get_tiers(self, guild_id: int, *, monthly: bool = False) -> list[tuple[int, int]]:
        rows = self.db.execute(
            """
            SELECT threshold, role_id FROM activity_tiers
            WHERE guild_id = ? AND scope = ? ORDER BY threshold
            """,
            (guild_id, self._scope(monthly)),
        ).fetchall()
        return [(r["threshold"], r["role_id"]) for r in rows]

    def get_tier(
        self,
        guild_id: int,
        threshold: int,
        *,
        monthly: bool = False,
    ) -> tuple[int, int] | None:
        row = self.db.execute(
            """
            SELECT threshold, role_id FROM activity_tiers
            WHERE guild_id = ? AND scope = ? AND threshold = ?
            """,
            (guild_id, self._scope(monthly), threshold),
        ).fetchone()
        if row is None:
            return None
        return row["threshold"], row["role_id"]

    def get_tier_uses_for_role(self, guild_id: int, role_id: int) -> list[tuple[str, int]]:
        """Return every scope/threshold using a role in this guild."""
        rows = self.db.execute(
            """
            SELECT scope, threshold FROM activity_tiers
            WHERE guild_id = ? AND role_id = ?
            ORDER BY scope, threshold
            """,
            (guild_id, role_id),
        ).fetchall()
        return [(row["scope"], row["threshold"]) for row in rows]

    def increment_counts(self, guild_id: int, user_id: int, *, month: str | None = None) -> tuple[int, int]:
        """Atomically increment and return the all-time and UTC-month counts."""
        monthly_period = month or utc_month_key()
        with self.db:
            for period in (ALL_TIME_PERIOD, monthly_period):
                self.db.execute(
                    """
                    INSERT INTO activity_counts (guild_id, user_id, period, count) VALUES (?, ?, ?, 1)
                    ON CONFLICT(guild_id, user_id, period) DO UPDATE SET count = count + 1
                    """,
                    (guild_id, user_id, period),
                )
            rows = self.db.execute(
                """
                SELECT period, count FROM activity_counts
                WHERE guild_id = ? AND user_id = ? AND period IN (?, ?)
                """,
                (guild_id, user_id, ALL_TIME_PERIOD, monthly_period),
            ).fetchall()
        counts = {row["period"]: row["count"] for row in rows}
        return counts[ALL_TIME_PERIOD], counts[monthly_period]

    def increment_count(self, guild_id: int, user_id: int) -> int:
        """Backward-compatible all-time-only increment used by older callers."""
        self.db.execute(
            """
            INSERT INTO activity_counts (guild_id, user_id, period, count) VALUES (?, ?, ?, 1)
            ON CONFLICT(guild_id, user_id, period) DO UPDATE SET count = count + 1
            """,
            (guild_id, user_id, ALL_TIME_PERIOD),
        )
        self.db.commit()
        return self.get_count(guild_id, user_id)

    def get_count(
        self,
        guild_id: int,
        user_id: int,
        *,
        monthly: bool = False,
        month: str | None = None,
    ) -> int:
        row = self.db.execute(
            "SELECT count FROM activity_counts WHERE guild_id = ? AND user_id = ? AND period = ?",
            (guild_id, user_id, self._period(monthly, month)),
        ).fetchone()
        return row["count"] if row else 0

    def get_counts(
        self,
        guild_id: int,
        *,
        monthly: bool = False,
        month: str | None = None,
    ) -> dict[int, int]:
        rows = self.db.execute(
            "SELECT user_id, count FROM activity_counts WHERE guild_id = ? AND period = ?",
            (guild_id, self._period(monthly, month)),
        ).fetchall()
        return {row["user_id"]: row["count"] for row in rows}

    def get_leaderboard(
        self,
        guild_id: int,
        limit: int = 30,
        *,
        monthly: bool = False,
        month: str | None = None,
    ) -> list[tuple[int, int]]:
        rows = self.db.execute(
            """
            SELECT user_id, count
            FROM activity_counts
            WHERE guild_id = ? AND period = ?
            ORDER BY count DESC, user_id ASC
            LIMIT ?
            """,
            (guild_id, self._period(monthly, month), limit),
        ).fetchall()
        return [(row["user_id"], row["count"]) for row in rows]
