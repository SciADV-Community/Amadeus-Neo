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
                sort_order       INTEGER NOT NULL DEFAULT 0,
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
        try:
            self.db.execute(
                "ALTER TABLE play_game ADD COLUMN sort_order INTEGER NOT NULL DEFAULT 0"
            )
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
        self._reflow_all_game_orders()
        self.db.commit()

    def _row_to_game(self, row: sqlite3.Row) -> PlayGame:
        return PlayGame(
            guild_id=row["guild_id"],
            key=row["key"],
            display_name=row["display_name"],
            sort_order=row["sort_order"],
            forum_channel_id=row["forum_channel_id"],
            forum_tag_id=row["forum_tag_id"],
            enabled=bool(row["enabled"]),
        )

    def _ordered_game_keys(self, guild_id: int) -> list[str]:
        rows = self.db.execute(
            """
            SELECT key
            FROM play_game
            WHERE guild_id = ?
            ORDER BY
                CASE WHEN sort_order > 0 THEN 0 ELSE 1 END,
                sort_order,
                display_name COLLATE NOCASE,
                key
            """,
            (guild_id,),
        ).fetchall()
        return [row["key"] for row in rows]

    def _reflow_game_orders(self, guild_id: int) -> None:
        for index, key in enumerate(self._ordered_game_keys(guild_id), start=1):
            self.db.execute(
                """
                UPDATE play_game
                SET sort_order = ?, updated_at = CURRENT_TIMESTAMP
                WHERE guild_id = ? AND key = ?
                """,
                (index, guild_id, key),
            )

    def _reflow_all_game_orders(self) -> None:
        rows = self.db.execute(
            """
            SELECT guild_id
            FROM play_game
            GROUP BY guild_id
            HAVING
                MIN(sort_order) < 1
                OR COUNT(DISTINCT sort_order) != COUNT(*)
                OR MAX(sort_order) != COUNT(*)
            """
        ).fetchall()
        for row in rows:
            self._reflow_game_orders(row["guild_id"])

    def _next_game_order(self, guild_id: int) -> int:
        row = self.db.execute(
            """
            SELECT COALESCE(MAX(sort_order), 0) + 1 AS next_order
            FROM play_game
            WHERE guild_id = ?
            """,
            (guild_id,),
        ).fetchone()
        return int(row["next_order"])

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
        sort_order: int | None = None,
    ) -> PlayGame:
        key = normalize_game_key(display_name)
        existing = self.get_game(guild_id, key, enabled_only=False)
        if sort_order is None:
            resolved_order = (
                existing.sort_order
                if existing is not None and existing.sort_order > 0
                else self._next_game_order(guild_id)
            )
        else:
            resolved_order = max(1, sort_order)

        self.db.execute(
            """
            INSERT INTO play_game (
                guild_id,
                key,
                display_name,
                sort_order,
                forum_channel_id,
                forum_tag_id,
                enabled
            )
            VALUES (?, ?, ?, ?, ?, ?, 1)
            ON CONFLICT(guild_id, key) DO UPDATE SET
                display_name = excluded.display_name,
                sort_order = excluded.sort_order,
                forum_channel_id = excluded.forum_channel_id,
                forum_tag_id = excluded.forum_tag_id,
                enabled = 1,
                updated_at = CURRENT_TIMESTAMP
            """,
            (
                guild_id,
                key,
                display_name.strip(),
                resolved_order,
                forum_channel_id,
                forum_tag_id,
            ),
        )
        if sort_order is None:
            self._reflow_game_orders(guild_id)
        else:
            self._set_game_order_by_key(guild_id, key, sort_order)
        self.db.commit()
        game = self.get_game(guild_id, key, enabled_only=False)
        if game is None:
            raise RuntimeError(f"Could not save play game for guild_id={guild_id}")
        return game

    def _set_game_order_by_key(
        self,
        guild_id: int,
        key: str,
        sort_order: int,
    ) -> None:
        keys = [
            ordered_key
            for ordered_key in self._ordered_game_keys(guild_id)
            if ordered_key != key
        ]
        insert_at = min(max(1, sort_order), len(keys) + 1) - 1
        keys.insert(insert_at, key)
        for index, ordered_key in enumerate(keys, start=1):
            self.db.execute(
                """
                UPDATE play_game
                SET sort_order = ?, updated_at = CURRENT_TIMESTAMP
                WHERE guild_id = ? AND key = ?
                """,
                (index, guild_id, ordered_key),
            )

    def set_game_order(
        self,
        guild_id: int,
        game: str,
        sort_order: int,
    ) -> PlayGame | None:
        key = normalize_game_key(game)
        if self.get_game(guild_id, key, enabled_only=False) is None:
            return None

        self._set_game_order_by_key(guild_id, key, sort_order)
        self.db.commit()
        return self.get_game(guild_id, key, enabled_only=False)

    def remove_game(self, guild_id: int, game: str) -> bool:
        cursor = self.db.execute(
            "DELETE FROM play_game WHERE guild_id = ? AND key = ?",
            (guild_id, normalize_game_key(game)),
        )
        if cursor.rowcount > 0:
            self._reflow_game_orders(guild_id)
        self.db.commit()
        return cursor.rowcount > 0

    def configured_forum_ids(self, guild_id: int) -> set[int]:
        config = self.get_config(guild_id)
        default_forum_id = config.forum_channel_id if config else None
        forum_ids = set()
        if default_forum_id is not None:
            forum_ids.add(default_forum_id)
        forum_ids.update(
            game.forum_channel_id or default_forum_id
            for game in self.list_games(guild_id)
            if game.forum_channel_id is not None or default_forum_id is not None
        )
        return {forum_id for forum_id in forum_ids if forum_id is not None}

    def games_for_forum(
        self,
        guild_id: int,
        forum_channel_id: int,
        *,
        enabled_only: bool = True,
    ) -> list[PlayGame]:
        config = self.get_config(guild_id)
        default_forum_id = config.forum_channel_id if config else None
        return [
            game
            for game in self.list_games(guild_id, enabled_only=enabled_only)
            if (game.forum_channel_id or default_forum_id) == forum_channel_id
        ]

    def remove_forum(
        self,
        guild_id: int,
        forum_channel_id: int,
    ) -> tuple[list[PlayGame], bool]:
        games = self.games_for_forum(
            guild_id,
            forum_channel_id,
            enabled_only=False,
        )
        config = self.get_config(guild_id)
        removes_default_forum = (
            config is not None and config.forum_channel_id == forum_channel_id
        )

        if removes_default_forum:
            self.db.execute(
                """
                UPDATE play_config
                SET forum_channel_id = NULL, updated_at = CURRENT_TIMESTAMP
                WHERE guild_id = ?
                """,
                (guild_id,),
            )
            self.db.execute(
                """
                DELETE FROM play_game
                WHERE guild_id = ?
                    AND (forum_channel_id = ? OR forum_channel_id IS NULL)
                """,
                (guild_id, forum_channel_id),
            )
        else:
            self.db.execute(
                """
                DELETE FROM play_game
                WHERE guild_id = ? AND forum_channel_id = ?
                """,
                (guild_id, forum_channel_id),
            )

        if games:
            self._reflow_game_orders(guild_id)
        self.db.commit()
        return games, removes_default_forum

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
            SELECT
                guild_id,
                key,
                display_name,
                sort_order,
                forum_channel_id,
                forum_tag_id,
                enabled
            FROM play_game
            WHERE guild_id = ? AND key = ?
            {enabled_clause}
            """,
            (guild_id, normalize_game_key(game)),
        ).fetchone()

        if row is None:
            return None

        return self._row_to_game(row)

    def list_games(self, guild_id: int, *, enabled_only: bool = True) -> list[PlayGame]:
        enabled_clause = "AND enabled = 1" if enabled_only else ""
        rows = self.db.execute(
            f"""
            SELECT
                guild_id,
                key,
                display_name,
                sort_order,
                forum_channel_id,
                forum_tag_id,
                enabled
            FROM play_game
            WHERE guild_id = ?
            {enabled_clause}
            ORDER BY
                CASE WHEN sort_order > 0 THEN 0 ELSE 1 END,
                sort_order,
                display_name COLLATE NOCASE,
                key
            """,
            (guild_id,),
        ).fetchall()
        return [self._row_to_game(row) for row in rows]

    def configured_play_forums(self, guild_id: int) -> dict[int, set[int]]:
        """Return configured playthrough forum IDs mapped to valid game tag IDs."""
        config = self.get_config(guild_id)
        default_forum_id = config.forum_channel_id if config else None
        forums: dict[int, set[int]] = {}

        for game in self.list_games(guild_id):
            forum_id = game.forum_channel_id or default_forum_id
            if forum_id is None or game.forum_tag_id is None:
                continue
            forums.setdefault(forum_id, set()).add(game.forum_tag_id)

        return forums

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
