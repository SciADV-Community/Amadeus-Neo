from types import SimpleNamespace

import pytest

from cogs.honeypot_admin import honeypot_action_label


@pytest.mark.parametrize(
    ("action", "label"),
    [
        ("mute", "Mute (28-day timeout)"),
        ("kick", "Kick"),
        ("ban", "Ban"),
    ],
)
def test_honeypot_action_label_does_not_require_role_for_moderation_actions(action, label):
    assert honeypot_action_label(action) == label


def test_honeypot_action_label_includes_role_for_remove_role():
    role = SimpleNamespace(mention="<@&123>")

    assert honeypot_action_label("remove_role", role) == "Remove role (<@&123>)"


def test_honeypot_action_label_rejects_remove_role_without_role():
    with pytest.raises(ValueError, match="remove_role requires a role"):
        honeypot_action_label("remove_role")
