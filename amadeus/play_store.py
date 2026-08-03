"""
Play module storage.
"""
import re
import sqlite3

from amadeus.database import BaseStore
from amadeus.models.play import PlayConfig, PlayGame

_WHITESPACE_RE = re.compile(r"\s+")


def normalize_game_key(value: str) -> str:
    """Returns the case-insensitive key used for configured playthrough games."""
    return _WHITESPACE_RE.sub(" ", value.strip()).casefold()


class PlayStore(BaseStore):
    """SQLite wrapper for Visual Novel playthrough configuration."""

    def _initialize(self):
        self.db.execute(
            """
            CREATE TABLE IF NOT EXISTS play_config (
                guild_id              INTEGER PRIMARY KEY,
                forum_channel_id      INTEGER,
                auto_archive_duration INTEGER,
                created_at            TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at            TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        self.db.execute(
            """
            CREATE TABLE IF NOT EXISTS play_game (
                guild_id         INTEGER NOT NULL,
                key              TEXT    NOT NULL,
                display_name     TEXT    NOT NULL,
                forum_channel_id INTEGER,
                forum_tag_id     INTEGER,
                enabled          INTEGER NOT NULL DEFAULT 1,
                created_at       TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at       TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (guild_id, key)
            )
            """
        )
        try:
            self.db.execute("ALTER TABLE play_game ADD COLUMN forum_channel_id INTEGER")
        except sqlite3.OperationalError:
            pass
        self.db.execute(
            """
            UPDATE play_game
            SET forum_channel_id = (
                SELECT forum_channel_id
                FROM play_config
                WHERE play_config.guild_id = play_game.guild_id
            )
            WHERE forum_channel_id IS NULL
            """
        )
        self.db.commit()

    def get_config(self, guild_id: int) -> PlayConfig | None:
        row = self.db.execute(
            """
            SELECT guild_id, forum_channel_id, auto_archive_duration
            FROM play_config
            WHERE guild_id = ?
            """,
            (guild_id,),
        ).fetchone()

        if row is None:
            return None

        return PlayConfig(
            guild_id=row["guild_id"],
            forum_channel_id=row["forum_channel_id"],
            auto_archive_duration=row["auto_archive_duration"],
        )

    def ensure_config(self, guild_id: int) -> PlayConfig:
        self.db.execute(
            "INSERT OR IGNORE INTO play_config (guild_id) VALUES (?)",
            (guild_id,),
        )
        self.db.commit()
        config = self.get_config(guild_id)
        if config is None:
            raise RuntimeError(f"Could not create play config for guild_id={guild_id}")
        return config

    def set_forum_channel(self, guild_id: int, channel_id: int) -> None:
        self.db.execute(
            """
            INSERT INTO play_config (guild_id, forum_channel_id)
            VALUES (?, ?)
            ON CONFLICT(guild_id) DO UPDATE SET
                forum_channel_id = excluded.forum_channel_id,
                updated_at = CURRENT_TIMESTAMP
            """,
            (guild_id, channel_id),
        )
        self.db.commit()

    def set_auto_archive_duration(self, guild_id: int, minutes: int | None) -> None:
        self.db.execute(
            """
            INSERT INTO play_config (guild_id, auto_archive_duration)
            VALUES (?, ?)
            ON CONFLICT(guild_id) DO UPDATE SET
                auto_archive_duration = excluded.auto_archive_duration,
                updated_at = CURRENT_TIMESTAMP
            """,
            (guild_id, minutes),
        )
        self.db.commit()

    def save_game(
        self,
        guild_id: int,
        display_name: str,
        forum_channel_id: int | None,
        forum_tag_id: int | None,
    ) -> PlayGame:
        key = normalize_game_key(display_name)
        self.db.execute(
            """
            INSERT INTO play_game (guild_id, key, display_name, forum_channel_id, forum_tag_id, enabled)
            VALUES (?, ?, ?, ?, ?, 1)
            ON CONFLICT(guild_id, key) DO UPDATE SET
                display_name = excluded.display_name,
                forum_channel_id = excluded.forum_channel_id,
                forum_tag_id = excluded.forum_tag_id,
                enabled = 1,
                updated_at = CURRENT_TIMESTAMP
            """,
            (guild_id, key, display_name.strip(), forum_channel_id, forum_tag_id),
        )
        self.db.commit()
        game = self.get_game(guild_id, key, enabled_only=False)
        if game is None:
            raise RuntimeError(f"Could not save play game for guild_id={guild_id}")
        return game

    def remove_game(self, guild_id: int, game: str) -> bool:
        cursor = self.db.execute(
            "DELETE FROM play_game WHERE guild_id = ? AND key = ?",
            (guild_id, normalize_game_key(game)),
        )
        self.db.commit()
        return cursor.rowcount > 0

    def get_game(
        self,
        guild_id: int,
        game: str,
        *,
        enabled_only: bool = True,
    ) -> PlayGame | None:
        enabled_clause = "AND enabled = 1" if enabled_only else ""
        row = self.db.execute(
            f"""
            SELECT guild_id, key, display_name, forum_channel_id, forum_tag_id, enabled
            FROM play_game
            WHERE guild_id = ? AND key = ?
            {enabled_clause}
            """,
            (guild_id, normalize_game_key(game)),
        ).fetchone()

        if row is None:
            return None

        return PlayGame(
            guild_id=row["guild_id"],
            key=row["key"],
            display_name=row["display_name"],
            forum_channel_id=row["forum_channel_id"],
            forum_tag_id=row["forum_tag_id"],
            enabled=bool(row["enabled"]),
        )

    def list_games(self, guild_id: int, *, enabled_only: bool = True) -> list[PlayGame]:
        enabled_clause = "AND enabled = 1" if enabled_only else ""
        rows = self.db.execute(
            f"""
            SELECT guild_id, key, display_name, forum_channel_id, forum_tag_id, enabled
            FROM play_game
            WHERE guild_id = ?
            {enabled_clause}
            ORDER BY display_name COLLATE NOCASE
            """,
            (guild_id,),
        ).fetchall()
        return [
            PlayGame(
                guild_id=row["guild_id"],
                key=row["key"],
                display_name=row["display_name"],
                forum_channel_id=row["forum_channel_id"],
                forum_tag_id=row["forum_tag_id"],
                enabled=bool(row["enabled"]),
            )
            for row in rows
        ]

    def search_games(self, guild_id: int, query: str, *, limit: int = 25) -> list[PlayGame]:
        games = self.list_games(guild_id)
        normalized_query = normalize_game_key(query)

        if normalized_query:
            games = [
                game
                for game in games
                if normalized_query in normalize_game_key(game.display_name)
            ]

        return games[:limit]
