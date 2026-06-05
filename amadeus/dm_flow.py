"""
Generic DB-backed DM state machine.

Each in-progress conversation is keyed on (guild_id, user_id, flow_type)
so multiple cogs can run independent DM flows without interfering.
"""
import json
import sqlite3
from datetime import datetime

from amadeus.database import BaseStore
from amadeus.logging_utils import log
from amadeus.models.dm_flow import DmFlow


class DmFlowStore(BaseStore):
    """
    SQLite wrapper for DM flow state.

    Keyed on (guild_id, user_id, flow_type) so different cogs can run
    independent conversations for the same user without interfering.
    """

    def _initialize(self):
        self.db.execute(
            """
            CREATE TABLE IF NOT EXISTS dm_flow_state (
                guild_id    INTEGER NOT NULL,
                user_id     INTEGER NOT NULL,
                flow_type   TEXT    NOT NULL,
                state       TEXT    NOT NULL,
                data        TEXT    NOT NULL DEFAULT '{}',
                started_at  TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at  TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (guild_id, user_id, flow_type)
            )
            """
        )
        self.db.commit()

    def _row_to_flow(self, row: sqlite3.Row) -> DmFlow:
        """Converts a raw SQLite row into a DmFlow dataclass."""
        return DmFlow(
            guild_id=row["guild_id"],
            user_id=row["user_id"],
            flow_type=row["flow_type"],
            state=row["state"],
            data=json.loads(row["data"]),
            started_at=datetime.fromisoformat(row["started_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    def get(self, guild_id: int, user_id: int, flow_type: str) -> DmFlow | None:
        """Returns the active flow for this (guild, user, type) triple, or None."""
        row = self.db.execute(
            """
            SELECT guild_id, user_id, flow_type, state, data, started_at, updated_at
            FROM dm_flow_state
            WHERE guild_id = ? AND user_id = ? AND flow_type = ?
            """,
            (guild_id, user_id, flow_type),
        ).fetchone()
        return self._row_to_flow(row) if row else None

    def get_all_for_user(self, user_id: int) -> list[DmFlow]:
        """Returns all flows for a user across all guilds, newest first."""
        rows = self.db.execute(
            """
            SELECT guild_id, user_id, flow_type, state, data, started_at, updated_at
            FROM dm_flow_state
            WHERE user_id = ?
            ORDER BY updated_at DESC
            """,
            (user_id,),
        ).fetchall()
        return [self._row_to_flow(r) for r in rows]

    def save(self, flow: DmFlow) -> None:
        """Creates or updates a flow record."""
        self.db.execute(
            """
            INSERT INTO dm_flow_state (guild_id, user_id, flow_type, state, data, updated_at)
            VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(guild_id, user_id, flow_type) DO UPDATE SET
                state      = excluded.state,
                data       = excluded.data,
                updated_at = CURRENT_TIMESTAMP
            """,
            (flow.guild_id, flow.user_id, flow.flow_type, flow.state, json.dumps(flow.data)),
        )
        self.db.commit()
        log(
            f"DM_FLOW // SAVED 『 USER {flow.user_id} 』 GUILD 『 {flow.guild_id} 』 "
            f"TYPE 『 {flow.flow_type} 』 STATE 『 {flow.state} 』",
            level="debug",
            logger_name="dm_flow",
        )

    def delete(self, guild_id: int, user_id: int, flow_type: str) -> None:
        """Deletes the flow record for this (guild, user, type) triple."""
        self.db.execute(
            "DELETE FROM dm_flow_state WHERE guild_id = ? AND user_id = ? AND flow_type = ?",
            (guild_id, user_id, flow_type),
        )
        self.db.commit()

    def get_expired(
        self,
        flow_type: str,
        max_age_seconds: int,
        exclude_state: str | None = None,
    ) -> list[DmFlow]:
        """
        Returns flows of a given type that have not been updated within max_age_seconds.

        Use exclude_state to skip flows awaiting admin action (e.g. PENDING_APPROVAL)
        so they aren't cleared by the timeout task before a decision is made.
        """
        query = """
            SELECT guild_id, user_id, flow_type, state, data, started_at, updated_at
            FROM dm_flow_state
            WHERE flow_type = ?
              AND (CAST(strftime('%s', 'now') AS INTEGER)
                   - CAST(strftime('%s', updated_at) AS INTEGER)) > ?
        """
        params: list = [flow_type, max_age_seconds]

        if exclude_state is not None:
            query += " AND state != ?"
            params.append(exclude_state)

        rows = self.db.execute(query, params).fetchall()
        return [self._row_to_flow(r) for r in rows]
