import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from civ_mcp.native_popups import skip_information_popups


def connection(states, replies):
    return SimpleNamespace(lua_states=states, execute_in_state_once=AsyncMock(side_effect=replies))


def test_only_exact_allowlisted_contexts_receive_native_esc_callbacks():
    conn = connection({1: "TechCivicCompletedPopup", 2: "NaturalDisasterPopup", 3: "DiplomacyActionView",
                       4: "EC_Consent", 5: "NaturalWonderPopup", 6: "ModdedNaturalDisasterPopup",
                       7: "ProjectBuiltPopup", 8: "ModdedProjectBuiltPopup", 9: "WonderBuiltPopup",
                       10: "ModdedWonderBuiltPopup", 11: "ModdedNaturalWonderPopup"},
                      [["[GC_INFO_POPUP] ESC|true"], ["[GC_INFO_POPUP] ESC|false"], ["[GC_INFO_POPUP] ESC|true"],
                       ["[GC_INFO_POPUP] ESC|true"], ["[GC_INFO_POPUP] ESC|true"]])
    receipts = asyncio.run(skip_information_popups(conn, "GC_NATIVE"))
    assert [r["hidden_after"] for r in receipts] == [True, False, True, True, True]
    calls = conn.execute_in_state_once.call_args_list
    assert [c.args[0] for c in calls] == [1, 2, 7, 9, 5]
    assert "TryClose()" in calls[0].args[1]
    assert "KeyHandler(Keys.VK_ESCAPE)" in calls[1].args[1]
    assert "KeyHandler(Keys.VK_ESCAPE)" in calls[2].args[1]
    for c in calls:
        assert "IsHidden()" in c.args[1] and "IsAnyMultiplayer()==false" in c.args[1]
        assert "IsHotseat()==false" in c.args[1] and '=="GC_NATIVE"' in c.args[1]
        assert "SetHide" not in c.args[1] and "DequeuePopup" not in c.args[1]


def test_ambiguous_popup_rejected_before_any_callback():
    conn = connection({1: "TechCivicCompletedPopup", 2: "TechCivicCompletedPopup"}, [])
    with pytest.raises(ConnectionError, match="Ambiguous"):
        asyncio.run(skip_information_popups(conn, "GC_NATIVE"))
    conn.execute_in_state_once.assert_not_awaited()


@pytest.mark.parametrize("reply", [[], ["[GC_INFO_POPUP] ESC|nil"], ["[GC_INFO_POPUP] HIDDEN", "[GC_INFO_POPUP] HIDDEN"]])
def test_missing_invalid_or_duplicate_receipt_never_reports_success(reply):
    conn = connection({2: "NaturalDisasterPopup"}, [reply])
    with pytest.raises(ConnectionError, match="receipt unavailable"):
        asyncio.run(skip_information_popups(conn, "GC_NATIVE"))
    assert conn.execute_in_state_once.await_count == 1


def test_transport_loss_does_not_replay_callback():
    conn = connection({2: "NaturalDisasterPopup"}, [ConnectionError("unknown")])
    with pytest.raises(ConnectionError, match="unknown"):
        asyncio.run(skip_information_popups(conn, "GC_NATIVE"))
    assert conn.execute_in_state_once.await_count == 1


def test_hidden_popup_and_other_contexts_produce_no_close_receipt():
    conn = connection({1: "TechCivicCompletedPopup", 2: "GovernmentScreen"}, [["[GC_INFO_POPUP] HIDDEN"]])
    assert asyncio.run(skip_information_popups(conn, "GC_NATIVE")) == []
    assert conn.execute_in_state_once.await_count == 1
