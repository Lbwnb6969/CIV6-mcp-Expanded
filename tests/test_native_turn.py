import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from civ_mcp.native_turn import advance_native_turn


class Connection:
    lua_states = {5: "GameCore_Tuner", 126: "InGame"}

    def __init__(self, replies):
        self.execute_in_state_once = AsyncMock(side_effect=replies)
        self.ensure_connected = AsyncMock()

    @asynccontextmanager
    async def exclusive(self):
        yield


def state(replies):
    return SimpleNamespace(conn=Connection(replies), dismiss_popup=AsyncMock(), end_turn=AsyncMock())


def test_persisted_pending_turn_is_not_submitted_again_or_given_popup_fallback():
    gs = state([["[GC_TURN] 2|2"]])
    result = asyncio.run(advance_native_turn(gs, "GC_NATIVE", timeout=0))
    assert "BLOCKED" in result
    assert gs.conn.execute_in_state_once.await_count == 1
    assert gs.conn.execute_in_state_once.call_args.args[0] == 5
    gs.dismiss_popup.assert_not_awaited()
    gs.end_turn.assert_not_awaited()


def test_completed_pending_turn_clears_receipt_without_another_end_turn():
    gs = state([["[GC_TURN] 3|2"], ["[GC_UI_TURN] 3|true"], []])
    result = asyncio.run(advance_native_turn(gs, "GC_NATIVE", timeout=0))
    assert "from 2 to 3" in result
    assert gs._native_turn_receipt == (2, 3)
    assert all("ACTION_ENDTURN" not in call.args[1] for call in gs.conn.execute_in_state_once.call_args_list)


def test_lost_dispatch_response_leaves_intent_and_a_retry_sends_no_second_action():
    gs = state([["[GC_TURN] 2|-1"], ["[GC_READY] true|true|3|50|0"], [], ConnectionError("lost after send")])
    with pytest.raises(ConnectionError, match="lost after send"):
        asyncio.run(advance_native_turn(gs, "GC_NATIVE", timeout=0))
    assert gs._native_pending_turn == ("GC_NATIVE", 2)
    first = gs.conn.execute_in_state_once.call_args_list
    assert "Game:SetProperty" in first[2].args[1]
    assert "ACTION_ENDTURN" in first[3].args[1]
    gs.conn.execute_in_state_once.side_effect = [["[GC_TURN] 2|2"], []]
    assert "BLOCKED" in asyncio.run(advance_native_turn(gs, "GC_NATIVE", timeout=0))
    assert gs._native_turn_receipt is None
    assert sum("ACTION_ENDTURN" in call.args[1] for call in gs.conn.execute_in_state_once.call_args_list) == 1


def test_native_blocker_stops_before_intent_or_dispatch():
    gs = state([["[GC_TURN] 2|-1"], ["[GC_READY] false|true|3|50|0"]])
    assert "BLOCKED" in asyncio.run(advance_native_turn(gs, "GC_NATIVE", timeout=0))
    assert gs.conn.execute_in_state_once.await_count == 2
    assert all("Game:SetProperty" not in call.args[1] and "ACTION_ENDTURN" not in call.args[1]
               for call in gs.conn.execute_in_state_once.call_args_list)


@pytest.mark.parametrize("receipt", ["true|true|0|50|0", "true|true|3|0|0", "true|true|nil|50|0", "true|true"])
def test_invalid_autosave_settings_cannot_persist_intent_or_end_turn(receipt):
    gs = state([["[GC_TURN] 2|-1"], ["[GC_READY] " + receipt]])
    assert "positive autosave" in asyncio.run(advance_native_turn(gs, "GC_NATIVE", timeout=0))
    assert gs.conn.execute_in_state_once.await_count == 2
    assert all("Game:SetProperty" not in call.args[1] and "ACTION_ENDTURN" not in call.args[1]
               for call in gs.conn.execute_in_state_once.call_args_list)


def test_native_meeting_blocks_before_any_intent_or_end_turn_dispatch():
    gs = state([["[GC_TURN] 3|-1"], ["[GC_READY] true|true|3|50|1"]])
    assert "closed diplomacy" in asyncio.run(advance_native_turn(gs, "GC_NATIVE", timeout=0))
    assert gs.conn.execute_in_state_once.await_count == 2
    assert all("Game:SetProperty" not in call.args[1] and "ACTION_ENDTURN" not in call.args[1]
               for call in gs.conn.execute_in_state_once.call_args_list)


def test_explicit_save_load_clears_old_world_memory_without_touching_durable_intent():
    from civ_mcp.game_state import GameState
    gs = GameState(Connection([]))
    gs._native_pending_turn = ("GC_NATIVE", 3)
    gs._native_turn_receipt = (2, 3)
    gs._record_save_load("GC_BEFORE_DISPATCH")
    assert gs._native_pending_turn is None
    assert gs._native_turn_receipt is None
    gs.conn.execute_in_state_once.assert_not_awaited()


def test_core_advance_with_paused_ui_preserves_intent_and_does_not_count_completion():
    gs = state([["[GC_TURN] 3|2"], ["[GC_UI_TURN] 2|false"]])
    assert "BLOCKED" in asyncio.run(advance_native_turn(gs, "GC_NATIVE", timeout=0))
    assert gs._native_turn_receipt is None
    assert all("ACTION_ENDTURN" not in c.args[1] and "SetProperty" not in c.args[1]
               for c in gs.conn.execute_in_state_once.call_args_list)


def test_authorized_information_popup_releases_pending_turn_without_second_end_turn():
    from unittest.mock import patch
    gs = state([["[GC_TURN] 3|2"], ["[GC_UI_TURN] 3|true"], []])
    skip = AsyncMock(return_value=[{"context": "NaturalDisasterPopup", "callback": "ESC", "hidden_after": True}])
    with patch("civ_mcp.native_turn.skip_native_information", new=skip):
        result = asyncio.run(advance_native_turn(gs, "GC_NATIVE", timeout=0, skip_information_popups=True))
    assert "from 2 to 3" in result and gs._native_turn_receipt == (2, 3)
    skip.assert_awaited_once_with(gs.conn, "GC_NATIVE")
    assert gs._native_popup_receipts[0]["context"] == "NaturalDisasterPopup"
    assert all("ACTION_ENDTURN" not in c.args[1] for c in gs.conn.execute_in_state_once.call_args_list)
