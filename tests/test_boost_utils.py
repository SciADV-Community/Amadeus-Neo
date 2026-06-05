from types import SimpleNamespace

import pytest

from amadeus.boost_utils import check_emoji_slots, get_proposed_tier, is_active_booster


@pytest.mark.parametrize(
    ("before_count", "after_count", "expected"),
    [
        (0, 1, 1),
        (1, 3, 2),
        (3, 3, 1),
        (4, 3, 1),
        (2, 5, 2),
    ],
)
def test_get_proposed_tier_uses_subscription_delta(before_count, after_count, expected):
    assert get_proposed_tier(before_count, after_count) == expected


def test_is_active_booster_checks_premium_since():
    assert is_active_booster(SimpleNamespace(premium_since=object()))
    assert not is_active_booster(SimpleNamespace(premium_since=None))


def test_check_emoji_slots_ignores_managed_emojis():
    guild = SimpleNamespace(
        id=123,
        emoji_limit=5,
        emojis=[
            SimpleNamespace(managed=False),
            SimpleNamespace(managed=True),
            SimpleNamespace(managed=False),
        ],
    )

    assert check_emoji_slots(guild, 3) == (True, 3, 5)
    assert check_emoji_slots(guild, 4) == (False, 3, 5)
