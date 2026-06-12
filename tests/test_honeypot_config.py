import sqlite3

from amadeus.honeypot_config import HoneypotConfigStore


def test_set_action_persists_moderation_reason(temp_db_path):
    store = HoneypotConfigStore()
    try:
        store.set_action(1, "ban", reason="Custom audit reason")

        config = store.get_config(1)
    finally:
        store.close()

    assert config.action == "ban"
    assert config.action_role_id is None
    assert config.action_reason == "Custom audit reason"


def test_set_action_clears_reason_when_switching_to_remove_role(temp_db_path):
    store = HoneypotConfigStore()
    try:
        store.set_action(1, "kick", reason="Old reason")
        store.set_action(1, "remove_role", role_id=123)

        config = store.get_config(1)
    finally:
        store.close()

    assert config.action == "remove_role"
    assert config.action_role_id == 123
    assert config.action_reason is None


def test_honeypot_config_migration_adds_action_reason_to_existing_table(temp_db_path):
    db = sqlite3.connect(temp_db_path)
    try:
        db.execute(
            """
            CREATE TABLE honeypot_config (
                guild_id        INTEGER PRIMARY KEY,
                channel_id      INTEGER,
                action          TEXT,
                action_role_id  INTEGER,
                alerts_enabled  INTEGER NOT NULL DEFAULT 1,
                updated_at      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        db.execute(
            "INSERT INTO honeypot_config (guild_id, action) VALUES (?, ?)",
            (1, "mute"),
        )
        db.commit()
    finally:
        db.close()

    store = HoneypotConfigStore()
    try:
        store.set_action(1, "mute", reason="Migrated reason")
        config = store.get_config(1)
    finally:
        store.close()

    assert config.action == "mute"
    assert config.action_reason == "Migrated reason"


def test_honeypot_config_setters_preserve_existing_fields(temp_db_path):
    store = HoneypotConfigStore()
    try:
        store.set_action(1, "kick", reason="Keep this")
        store.set_channel(1, 456)
        store.set_alerts_enabled(1, False)

        config = store.get_config(1)
    finally:
        store.close()

    assert config.channel_id == 456
    assert config.action == "kick"
    assert config.action_reason == "Keep this"
    assert config.alerts_enabled is False
