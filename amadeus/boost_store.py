"""
Boost module storage.
"""
from amadeus.database import BaseStore
from amadeus.models.boost import BoostGrant


class BoostStore(BaseStore):
    """SQLite wrapper for boost grants and subscription count cache."""

    def _initialize(self):
        self.db.execute(
            """
            CREATE TABLE IF NOT EXISTS boost_grant (
                guild_id    INTEGER NOT NULL,
                user_id     INTEGER NOT NULL,
                tier        INTEGER NOT NULL,
                role_id     INTEGER,
                emoji_1_id  INTEGER,
                emoji_2_id  INTEGER,
                granted_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (guild_id, user_id)
            )
            """
        )
        self.db.execute(
            """
            CREATE TABLE IF NOT EXISTS boost_meta (
                guild_id            INTEGER PRIMARY KEY,
                subscription_count  INTEGER NOT NULL DEFAULT 0,
                updated_at          TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        self.db.commit()

    def get_subscription_count(self, guild_id: int) -> int | None:
        row = self.db.execute(
            "SELECT subscription_count FROM boost_meta WHERE guild_id = ?",
            (guild_id,),
        ).fetchone()
        return row["subscription_count"] if row else None

    def set_subscription_count(self, guild_id: int, count: int) -> None:
        self.db.execute(
            """
            INSERT INTO boost_meta (guild_id, subscription_count)
            VALUES (?, ?)
            ON CONFLICT(guild_id) DO UPDATE SET
                subscription_count = excluded.subscription_count,
                updated_at         = CURRENT_TIMESTAMP
            """,
            (guild_id, count),
        )
        self.db.commit()

    def get_grant(self, guild_id: int, user_id: int) -> BoostGrant | None:
        row = self.db.execute(
            "SELECT * FROM boost_grant WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
        ).fetchone()
        if row is None:
            return None
        return BoostGrant(
            guild_id=row["guild_id"],
            user_id=row["user_id"],
            tier=row["tier"],
            role_id=row["role_id"],
            emoji_1_id=row["emoji_1_id"],
            emoji_2_id=row["emoji_2_id"],
        )

    def save_grant(self, grant: BoostGrant) -> None:
        self.db.execute(
            """
            INSERT INTO boost_grant (guild_id, user_id, tier, role_id, emoji_1_id, emoji_2_id)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(guild_id, user_id) DO UPDATE SET
                tier       = excluded.tier,
                role_id    = excluded.role_id,
                emoji_1_id = excluded.emoji_1_id,
                emoji_2_id = excluded.emoji_2_id,
                granted_at = CURRENT_TIMESTAMP
            """,
            (grant.guild_id, grant.user_id, grant.tier,
             grant.role_id, grant.emoji_1_id, grant.emoji_2_id),
        )
        self.db.commit()

    def delete_grant(self, guild_id: int, user_id: int) -> None:
        self.db.execute(
            "DELETE FROM boost_grant WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
        )
        self.db.commit()
