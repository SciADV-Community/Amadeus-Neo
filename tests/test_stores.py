from datetime import timedelta

import discord

from amadeus.activity_store import ActivityStore
from amadeus.boost_store import BoostStore
from amadeus.bouncer_config import BounceConfigStore
from amadeus.dm_flow import DmFlowStore
from amadeus.models.boost import BoostGrant
from amadeus.models.dm_flow import DmFlow
from amadeus.play_store import PlayStore, normalize_game_key


def test_bouncer_config_defaults_and_setters_persist(temp_db_path):
    store = BounceConfigStore()
    try:
        assert store.get_bouncer_config(1) is None

        config = store.ensure_bouncer_config(1)
        assert config.verified_role_id is None
        assert config.verification_channel_id is None

        store.set_verified_role(1, 10)
        store.set_verification_channel(1, 20)
        store.set_min_account_age_days(1, 7)
        store.set_max_failed_attempts(1, 4)
        store.set_captcha_expiry_minutes(1, 15)
        store.set_panel_image_url(1, "https://cdn.discordapp.com/attachments/1/2/panel.png")
        store.set_verification_role_delay(1, 30)

        config = store.get_bouncer_config(1)
    finally:
        store.close()

    assert config.verified_role_id == 10
    assert config.verification_channel_id == 20
    assert config.min_account_age_days == 7
    assert config.max_failed_attempts == 4
    assert config.captcha_expiry_minutes == 15
    assert config.panel_image_url == "https://cdn.discordapp.com/attachments/1/2/panel.png"
    assert config.verification_role_delay_seconds == 30.0


def test_dm_flow_save_update_delete_and_get_all_for_user(temp_db_path):
    store = DmFlowStore()
    try:
        flow = DmFlow(
            guild_id=1,
            user_id=2,
            flow_type="boost",
            state="ROLE_NAME",
            data={"role_name": "Lab"},
        )
        store.save(flow)

        saved = store.get(1, 2, "boost")
        assert saved.state == "ROLE_NAME"
        assert saved.data == {"role_name": "Lab"}

        flow.state = "ROLE_IMAGE"
        flow.data = {"role_name": "Lab", "image": "ok"}
        store.save(flow)

        updated = store.get(1, 2, "boost")
        assert updated.state == "ROLE_IMAGE"
        assert updated.data["image"] == "ok"
        assert [f.flow_type for f in store.get_all_for_user(2)] == ["boost"]

        store.delete(1, 2, "boost")
        assert store.get(1, 2, "boost") is None
    finally:
        store.close()


def test_dm_flow_get_expired_respects_flow_type_age_and_excluded_state(temp_db_path):
    store = DmFlowStore()
    try:
        old_time = (discord.utils.utcnow() - timedelta(hours=2)).replace(microsecond=0).isoformat()
        fresh_time = discord.utils.utcnow().replace(microsecond=0).isoformat()
        store.db.execute(
            """
            INSERT INTO dm_flow_state (guild_id, user_id, flow_type, state, data, started_at, updated_at)
            VALUES (?, ?, ?, ?, '{}', ?, ?)
            """,
            (1, 1, "boost", "ROLE_NAME", old_time, old_time),
        )
        store.db.execute(
            """
            INSERT INTO dm_flow_state (guild_id, user_id, flow_type, state, data, started_at, updated_at)
            VALUES (?, ?, ?, ?, '{}', ?, ?)
            """,
            (1, 2, "boost", "PENDING_APPROVAL", old_time, old_time),
        )
        store.db.execute(
            """
            INSERT INTO dm_flow_state (guild_id, user_id, flow_type, state, data, started_at, updated_at)
            VALUES (?, ?, ?, ?, '{}', ?, ?)
            """,
            (1, 3, "boost", "ROLE_NAME", fresh_time, fresh_time),
        )
        store.db.commit()

        expired = store.get_expired("boost", max_age_seconds=3600, exclude_state="PENDING_APPROVAL")
    finally:
        store.close()

    assert [(flow.user_id, flow.state) for flow in expired] == [(1, "ROLE_NAME")]


def test_activity_store_tracks_cooldown_channels_tiers_and_counts(temp_db_path):
    store = ActivityStore()
    try:
        assert store.get_cooldown(1) == 5
        assert store.get_role_swap_enabled(1) is False
        store.set_cooldown(1, 12)
        store.set_role_swap_enabled(1, True)
        assert store.get_cooldown(1) == 12
        assert store.get_role_swap_enabled(1) is True

        store.add_channel(1, 100, "include")
        store.add_channel(1, 200, "exclude")
        assert store.get_channels(1) == ({100}, {200})
        store.add_channel(1, 100, "exclude")
        assert store.get_channels(1) == (set(), {100, 200})
        assert store.remove_channel(1, 200) is True
        assert store.remove_channel(1, 999) is False

        store.add_tier(1, 100, 10)
        store.add_tier(1, 50, 5)
        store.add_tier(1, 100, 11)
        assert store.get_tiers(1) == [(50, 5), (100, 11)]
        assert store.remove_tier(1, 50) is True
        assert store.remove_tier(1, 999) is False

        assert store.get_count(1, 2) == 0
        assert store.increment_count(1, 2) == 1
        assert store.increment_count(1, 2) == 2
        assert store.increment_count(1, 3) == 1
        assert store.get_count(1, 2) == 2
        assert store.get_leaderboard(1, limit=2) == [(2, 2), (3, 1)]
    finally:
        store.close()


def test_boost_store_tracks_subscription_count_and_grant_lifecycle(temp_db_path):
    store = BoostStore()
    try:
        assert store.get_subscription_count(1) is None
        store.set_subscription_count(1, 3)
        assert store.get_subscription_count(1) == 3
        store.set_subscription_count(1, 4)
        assert store.get_subscription_count(1) == 4

        grant = BoostGrant(
            guild_id=1,
            user_id=2,
            tier=2,
            role_id=10,
            emoji_1_id=20,
            emoji_2_id=30,
        )
        store.save_grant(grant)
        assert store.get_grant(1, 2) == grant

        updated = BoostGrant(guild_id=1, user_id=2, tier=3, role_id=11)
        store.save_grant(updated)
        assert store.get_grant(1, 2) == updated

        store.delete_grant(1, 2)
        assert store.get_grant(1, 2) is None
    finally:
        store.close()


def test_play_store_tracks_config_and_games(temp_db_path):
    store = PlayStore()
    try:
        assert store.get_config(1) is None
        assert store.ensure_config(1).forum_channel_id is None

        store.set_forum_channel(1, 100)
        store.set_auto_archive_duration(1, 1440)
        config = store.get_config(1)
        assert config.forum_channel_id == 100
        assert config.auto_archive_duration == 1440

        gate = store.save_game(1, "Steins;Gate", 200)
        zero = store.save_game(1, "Steins;Gate 0", 201)
        assert gate.key == "steins;gate"
        assert zero.key == "steins;gate 0"

        updated = store.save_game(1, "steins;gate", 202)
        assert updated.display_name == "steins;gate"
        assert updated.forum_tag_id == 202
        assert store.get_game(1, "STEINS;GATE").forum_tag_id == 202

        assert [game.key for game in store.search_games(1, "gate")] == [
            "steins;gate",
            "steins;gate 0",
        ]
        assert store.remove_game(1, "steins;gate 0") is True
        assert store.remove_game(1, "missing") is False
        assert [game.key for game in store.list_games(1)] == ["steins;gate"]
    finally:
        store.close()


def test_normalize_game_key_collapses_case_and_whitespace():
    assert normalize_game_key("  Steins;Gate   Re:Boot  ") == "steins;gate re:boot"
