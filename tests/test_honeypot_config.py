import sqlite3

from amadeus.honeypot_config import HoneypotConfigStore


def test_set_action_persists_moderation_reason_and_delete_history_window(temp_db_path):
    store = HoneypotConfigStore()
    try:
        store.set_action(
            1,
            "ban",
            reason="Custom audit reason",
            delete_history_seconds=3600,
        )

        config = store.get_config(1)
    finally:
        store.close()

    assert config.action == "ban"
    assert config.action_role_id is None
    assert config.action_reason == "Custom audit reason"
    assert config.delete_history_seconds == 3600


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
    assert config.delete_history_seconds is None


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
        store.set_action(1, "mute", reason="Migrated reason", delete_history_seconds=21600)
        config = store.get_config(1)
    finally:
        store.close()

    assert config.action == "mute"
    assert config.action_reason == "Migrated reason"
    assert config.delete_history_seconds == 21600
    assert config.post_message is None
    assert config.post_message_id is None


def test_honeypot_config_setters_preserve_existing_fields(temp_db_path):
    store = HoneypotConfigStore()
    try:
        store.set_action(1, "kick", reason="Keep this", delete_history_seconds=43200)
        store.set_channel(1, 456)
        store.set_post_message(1, "Custom warning")
        store.set_post_message_id(1, 789)
        store.set_alerts_enabled(1, False)

        config = store.get_config(1)
    finally:
        store.close()

    assert config.channel_id == 456
    assert config.action == "kick"
    assert config.action_reason == "Keep this"
    assert config.delete_history_seconds == 43200
    assert config.post_message == "Custom warning"
    assert config.post_message_id == 789
    assert config.alerts_enabled is False


def test_set_post_message_overwrites_previous_message(temp_db_path):
    store = HoneypotConfigStore()
    try:
        store.set_post_message(1, "Old warning")
        store.set_post_message(1, "New warning")

        config = store.get_config(1)
    finally:
        store.close()

    assert config.post_message == "New warning"


def test_set_post_message_id_overwrites_previous_message_id(temp_db_path):
    store = HoneypotConfigStore()
    try:
        store.set_post_message_id(1, 111)
        store.set_post_message_id(1, 222)

        config = store.get_config(1)
    finally:
        store.close()

    assert config.post_message_id == 222
