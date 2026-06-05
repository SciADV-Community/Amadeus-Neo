import sqlite3
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
        self._initialize()

    def _initialize(self):
        pass

    def close(self):
        self.db.close()


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
        # Migration: add alert_channel_id if it doesn't exist yet.
        try:
            self.db.execute("ALTER TABLE guild_config ADD COLUMN alert_channel_id INTEGER")
        except sqlite3.OperationalError:
            pass  # column already exists
        self.db.commit()

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
