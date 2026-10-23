import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import discord

from amadeus.constants import DB_PATH
from amadeus.logging_utils import log
from amadeus.models import GuildConfig


class BaseStore:
    """
    SQLite connection boilerplate shared by all per-module stores.

    Subclasses implement _initialize() to create and migrate their tables.
    """

    def __init__(self):
        _ensure_database_path_ready(DB_PATH)
        self.db = sqlite3.connect(DB_PATH, timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA busy_timeout=30000")
        self._initialize()

    def _initialize(self):
        pass

    def close(self):
        self.db.close()

    def table_columns(self, table: str) -> set[str]:
        exists = self.db.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
        ).fetchone()
        if not exists:
            return set()
        if not table.replace("_", "").isalnum():
            raise ValueError("Invalid SQLite table name")
        return {row["name"] for row in self.db.execute(f"PRAGMA table_info({table})")}

    def add_column_if_missing(self, table: str, column: str, definition: str) -> bool:
        """Add a known migration column without masking unrelated SQLite errors."""
        if column in self.table_columns(table):
            return False
        if not table.replace("_", "").isalnum() or not column.replace("_", "").isalnum():
            raise ValueError("Invalid SQLite identifier")
        self.db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
        return True


def _ensure_database_path_ready(db_path: Path) -> None:
    """
    Validate that SQLite can create its database, WAL, and journal files.

    Docker runs with a read-only root filesystem, so the database must live in
    the writable /app/data bind mount.
    """
    parent = db_path.parent
    try:
        parent.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise RuntimeError(_database_path_error(db_path)) from e

    if not parent.is_dir():
        raise RuntimeError(_database_path_error(db_path))

    probe = parent / ".amadeus-write-test"
    try:
        probe.write_text("", encoding="utf-8")
        probe.unlink(missing_ok=True)
    except OSError as e:
        raise RuntimeError(_database_path_error(db_path)) from e

    if db_path.exists():
        try:
            with db_path.open("ab"):
                pass
        except OSError as e:
            raise RuntimeError(_database_path_error(db_path)) from e


def _database_path_error(db_path: Path) -> str:
    return (
        f"SQLite database path is not writable: {db_path}\n\n"
        "For Docker deployments, /app/data must be backed by a writable host directory.\n"
        "Run this on the host before starting the container:\n"
        "  sudo install -d -m 0770 -o 10001 -g 10001 /srv/amadeus-neo/data"
    )


class ConfigStore(BaseStore):
    """
    SQLite wrapper for core per-server Amadeus config.

    Stores:
    - guild/server ID
    - server owner ID
    - Amadeus admin role ID
    """

    def _initialize(self):
        self.db.execute(
            """
            CREATE TABLE IF NOT EXISTS guild_config (
                guild_id   INTEGER PRIMARY KEY,
                owner_id   INTEGER NOT NULL,
                admin_role_id INTEGER,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        self.db.execute(
            """
            CREATE TABLE IF NOT EXISTS guild_enabled_modules (
                guild_id    INTEGER NOT NULL,
                module_name TEXT    NOT NULL,
                enabled_at  TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (guild_id, module_name)
            )
            """
        )
        self.db.execute(
            """
            CREATE TABLE IF NOT EXISTS guild_data_deletion (
                guild_id INTEGER PRIMARY KEY,
                delete_after TEXT NOT NULL
            )
            """
        )
        self.add_column_if_missing("guild_config", "alert_channel_id", "INTEGER")
        self.db.commit()

    def schedule_guild_deletion(self, guild_id: int, *, days: int = 90) -> datetime:
        delete_after = datetime.now(timezone.utc) + timedelta(days=days)
        self.db.execute(
            """
            INSERT INTO guild_data_deletion (guild_id, delete_after) VALUES (?, ?)
            ON CONFLICT(guild_id) DO UPDATE SET delete_after = excluded.delete_after
            """,
            (guild_id, delete_after.isoformat()),
        )
        self.db.commit()
        return delete_after

    def cancel_guild_deletion(self, guild_id: int) -> bool:
        cursor = self.db.execute(
            "DELETE FROM guild_data_deletion WHERE guild_id = ?", (guild_id,)
        )
        self.db.commit()
        return cursor.rowcount > 0

    def get_due_guild_deletions(self, *, now: datetime | None = None) -> list[int]:
        current = (now or datetime.now(timezone.utc)).isoformat()
        rows = self.db.execute(
            "SELECT guild_id FROM guild_data_deletion WHERE delete_after <= ? ORDER BY delete_after",
            (current,),
        ).fetchall()
        return [row["guild_id"] for row in rows]

    def purge_guild_data(self, guild_id: int) -> None:
        """Delete rows from every current module table containing a guild_id column."""
        tables = self.db.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
        with self.db:
            for row in tables:
                table = row["name"]
                if table == "guild_data_deletion":
                    continue
                if "guild_id" in self.table_columns(table):
                    self.db.execute(f'DELETE FROM "{table}" WHERE guild_id = ?', (guild_id,))
            self.db.execute("DELETE FROM guild_data_deletion WHERE guild_id = ?", (guild_id,))

    def ensure_guild_config(self, guild: discord.Guild) -> GuildConfig:
        """
        Creates a config row for this guild if it does not exist.
        Also updates the stored owner ID in case server ownership changed.
        """

        self.db.execute(
            """
            INSERT INTO guild_config (guild_id, owner_id)
            VALUES (?, ?)
            ON CONFLICT(guild_id) DO UPDATE SET
                owner_id = excluded.owner_id,
                updated_at = CURRENT_TIMESTAMP
            """,
            (guild.id, guild.owner_id),
        )
        self.db.commit()

        log(f"DB // GUILD CONFIG ENSURED 『 GUILD {guild.id} 』 OWNER 『 {guild.owner_id} 』", level="debug", logger_name="db")

        return self.get_guild_config(guild.id)

    def get_guild_config(self, guild_id: int) -> GuildConfig:
        """Returns the GuildConfig for a guild. Raises RuntimeError if no row exists."""
        row = self.db.execute(
            """
            SELECT guild_id, owner_id, admin_role_id, alert_channel_id
            FROM guild_config
            WHERE guild_id = ?
            """,
            (guild_id,),
        ).fetchone()

        if row is None:
            log(f"DB // GUILD CONFIG NOT FOUND 『 GUILD {guild_id} 』", level="debug", logger_name="db")
            raise RuntimeError(f"No guild config found for guild_id={guild_id}")

        return GuildConfig(
            guild_id=row["guild_id"],
            owner_id=row["owner_id"],
            admin_role_id=row["admin_role_id"],
            alert_channel_id=row["alert_channel_id"],
        )

    def set_alert_channel(self, guild: discord.Guild, channel_id: int) -> None:
        """Persists the alert channel ID for the guild."""
        self.ensure_guild_config(guild)

        self.db.execute(
            """
            UPDATE guild_config
            SET alert_channel_id = ?, updated_at = CURRENT_TIMESTAMP
            WHERE guild_id = ?
            """,
            (channel_id, guild.id),
        )
        self.db.commit()

        log(f"DB // ALERT CHANNEL SET 『 CHANNEL {channel_id} 』 GUILD 『 {guild.id} 』", level="debug", logger_name="db")

    def set_admin_role(self, guild: discord.Guild, role_id: int) -> None:
        """Persists the Amadeus admin role ID for the guild."""
        self.ensure_guild_config(guild)

        self.db.execute(
            """
            UPDATE guild_config
            SET admin_role_id = ?, updated_at = CURRENT_TIMESTAMP
            WHERE guild_id = ?
            """,
            (role_id, guild.id),
        )
        self.db.commit()

        log(f"DB // ADMIN ROLE SET 『 ROLE {role_id} 』 GUILD 『 {guild.id} 』", level="debug", logger_name="db")

    # ----------------------------------------------------------------
    # Per-guild module enable/disable
    # ----------------------------------------------------------------

    def enable_module(self, guild_id: int, module_name: str) -> None:
        """Marks a module as enabled for the guild. No-op if already enabled."""
        self.db.execute(
            """
            INSERT OR IGNORE INTO guild_enabled_modules (guild_id, module_name)
            VALUES (?, ?)
            """,
            (guild_id, module_name),
        )
        self.db.commit()

        log(f"DB // MODULE ENABLED 『 {module_name} 』 GUILD 『 {guild_id} 』", level="debug", logger_name="db")

    def disable_module(self, guild_id: int, module_name: str) -> None:
        """Removes a module from the enabled set for the guild. No-op if not enabled."""
        self.db.execute(
            "DELETE FROM guild_enabled_modules WHERE guild_id = ? AND module_name = ?",
            (guild_id, module_name),
        )
        self.db.commit()

        log(f"DB // MODULE DISABLED 『 {module_name} 』 GUILD 『 {guild_id} 』", level="debug", logger_name="db")

    def is_module_enabled(self, guild_id: int, module_name: str) -> bool:
        """Returns True if the module is enabled for the guild."""
        row = self.db.execute(
            "SELECT 1 FROM guild_enabled_modules WHERE guild_id = ? AND module_name = ?",
            (guild_id, module_name),
        ).fetchone()
        return row is not None

    def get_enabled_modules(self, guild_id: int) -> set[str]:
        """Returns the set of module names currently enabled for the guild."""
        rows = self.db.execute(
            "SELECT module_name FROM guild_enabled_modules WHERE guild_id = ?",
            (guild_id,),
        ).fetchall()
        return {row["module_name"] for row in rows}
