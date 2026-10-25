import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import cogs.boost_admin as boost_admin_module
from amadeus.models.dm_flow import DmFlow
from cogs.boost import S
from cogs.boost_admin import BoostAdmin


class _FakeMember:
    def __init__(self, user_id: int):
        self.id = user_id


def _status_cog(flow: DmFlow, boost_cog: SimpleNamespace) -> BoostAdmin:
    cog = BoostAdmin.__new__(BoostAdmin)
    cog.module_store = SimpleNamespace(is_module_enabled=lambda guild_id, module: True)
    cog.boost_store = SimpleNamespace(
        get_grant=lambda guild_id, user_id: SimpleNamespace(
            tier=2,
            role_id=None,
            emoji_1_id=None,
            emoji_2_id=None,
        )
    )
    cog.flow_store = SimpleNamespace(
        get=lambda guild_id, user_id, flow_type: flow,
        delete=Mock(),
    )
    cog.bot = SimpleNamespace(get_cog=lambda name: boost_cog)
    return cog


def test_status_does_not_send_stale_dashboard_for_denied_forced_flow(monkeypatch):
    monkeypatch.setattr(boost_admin_module.discord, "Member", _FakeMember)
    flow = DmFlow(1, 2, "boost", S.DENIED, {"tier": 2, "forced": True})
    boost_cog = SimpleNamespace(
        _reset_flow=AsyncMock(),
        _send_prompt=AsyncMock(),
    )
    cog = _status_cog(flow, boost_cog)
    interaction = SimpleNamespace(
        guild=SimpleNamespace(id=1, get_role=lambda role_id: None, emojis=[]),
        user=_FakeMember(2),
        response=SimpleNamespace(send_message=AsyncMock()),
    )

    asyncio.run(BoostAdmin.boost_status.callback(cog, interaction))

    interaction.response.send_message.assert_awaited_once()
    cog.flow_store.delete.assert_called_once_with(1, 2, "boost")
    boost_cog._reset_flow.assert_not_awaited()
    boost_cog._send_prompt.assert_not_awaited()


def test_status_restarts_denied_self_service_flow(monkeypatch):
    monkeypatch.setattr(boost_admin_module.discord, "Member", _FakeMember)
    flow = DmFlow(1, 2, "boost", S.DENIED, {"tier": 1})
    restarted = DmFlow(1, 2, "boost", S.COLLECTING, {"tier": 1})
    boost_cog = SimpleNamespace(
        _reset_flow=AsyncMock(return_value=restarted),
        _send_prompt=AsyncMock(),
    )
    cog = _status_cog(flow, boost_cog)
    interaction = SimpleNamespace(
        guild=SimpleNamespace(id=1, get_role=lambda role_id: None, emojis=[]),
        user=_FakeMember(2),
        response=SimpleNamespace(send_message=AsyncMock()),
    )

    asyncio.run(BoostAdmin.boost_status.callback(cog, interaction))

    boost_cog._reset_flow.assert_awaited_once_with(1, 2, 1, forced=False)
    boost_cog._send_prompt.assert_awaited_once_with(interaction.user, restarted)


def _status_interaction():
    return SimpleNamespace(
        guild=SimpleNamespace(id=1, get_role=lambda role_id: None, emojis=[]),
        user=_FakeMember(2),
        response=SimpleNamespace(send_message=AsyncMock()),
    )


def test_status_resends_a_collecting_dashboard_unchanged(monkeypatch):
    monkeypatch.setattr(boost_admin_module.discord, "Member", _FakeMember)
    flow = DmFlow(1, 2, "boost", S.COLLECTING, {"tier": 1})
    boost_cog = SimpleNamespace(_reset_flow=AsyncMock(), _send_prompt=AsyncMock())
    cog = _status_cog(flow, boost_cog)
    interaction = _status_interaction()

    asyncio.run(BoostAdmin.boost_status.callback(cog, interaction))

    boost_cog._reset_flow.assert_not_awaited()
    boost_cog._send_prompt.assert_awaited_once_with(interaction.user, flow)


def test_status_refreshes_a_stale_flow_state(monkeypatch):
    # A flow left in a state from an older version of the cog must not re-send a
    # dashboard whose buttons all report "no longer active".
    monkeypatch.setattr(boost_admin_module.discord, "Member", _FakeMember)
    flow = DmFlow(1, 2, "boost", "awaiting_confirmation", {"tier": 1})
    restarted = DmFlow(1, 2, "boost", S.COLLECTING, {"tier": 1})
    boost_cog = SimpleNamespace(
        _reset_flow=AsyncMock(return_value=restarted),
        _send_prompt=AsyncMock(),
    )
    cog = _status_cog(flow, boost_cog)
    interaction = _status_interaction()

    asyncio.run(BoostAdmin.boost_status.callback(cog, interaction))

    boost_cog._reset_flow.assert_awaited_once_with(1, 2, 1, forced=False)
    boost_cog._send_prompt.assert_awaited_once_with(interaction.user, restarted)


def test_status_does_not_resend_a_pending_dashboard(monkeypatch):
    monkeypatch.setattr(boost_admin_module.discord, "Member", _FakeMember)
    flow = DmFlow(1, 2, "boost", S.PENDING, {"tier": 1})
    boost_cog = SimpleNamespace(_reset_flow=AsyncMock(), _send_prompt=AsyncMock())
    cog = _status_cog(flow, boost_cog)
    interaction = _status_interaction()

    asyncio.run(BoostAdmin.boost_status.callback(cog, interaction))

    boost_cog._reset_flow.assert_not_awaited()
    boost_cog._send_prompt.assert_not_awaited()
    cog.flow_store.delete.assert_not_called()


def test_status_clears_a_forced_setup_instead_of_resuming_it(monkeypatch):
    monkeypatch.setattr(boost_admin_module.discord, "Member", _FakeMember)
    flow = DmFlow(1, 2, "boost", S.COLLECTING, {"tier": 1, "forced": True})
    boost_cog = SimpleNamespace(_reset_flow=AsyncMock(), _send_prompt=AsyncMock())
    cog = _status_cog(flow, boost_cog)
    interaction = _status_interaction()

    asyncio.run(BoostAdmin.boost_status.callback(cog, interaction))

    # A forced setup is admin-managed: clear it and require a fresh force.
    cog.flow_store.delete.assert_called_once_with(1, 2, "boost")
    boost_cog._reset_flow.assert_not_awaited()
    boost_cog._send_prompt.assert_not_awaited()
