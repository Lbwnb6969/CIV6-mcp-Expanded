import asyncio
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch

from civ_mcp.game_lifecycle import load_game_save
from civ_mcp.native_load import load_native_test_save


class Connection:
    native_acceptance_owner = True
    lua_states = {24: "MainMenu"}
    ingame_index = 126

    def __init__(self):
        self.ensure_connected = AsyncMock()
        self.execute_in_state_once = AsyncMock(return_value=[])
        self.execute_write = AsyncMock(side_effect=AssertionError("Generic load forbidden"))

    @asynccontextmanager
    async def exclusive(self):
        yield


def test_native_owner_never_enters_generic_load_or_screen_fallback_after_failure():
    conn = Connection()
    with patch("civ_mcp.native_load.load_native_test_save", new=AsyncMock(return_value="Error: Native unconfirmed")) as native:
        result = asyncio.run(load_game_save(conn, "GC_NATIVE"))
    assert result == "Error: Native unconfirmed"
    native.assert_awaited_once_with(conn, "GC_NATIVE")
    conn.execute_write.assert_not_awaited()


def test_native_load_dispatches_once_and_failure_has_no_menu_control_or_retry():
    conn = Connection()
    with patch("civ_mcp.game_lifecycle._wait_for_loaded_game", new=AsyncMock(return_value=False)):
        result = asyncio.run(load_native_test_save(conn, "GC_NATIVE"))
    assert "no replay or screen fallback" in result
    conn.execute_in_state_once.assert_awaited_once()
    code = conn.execute_in_state_once.call_args.args[1]
    assert code.count("Network.LoadGame(") == 1
    assert 'save.Name=="GC_NATIVE.Civ6Save"' in code
    assert "Modding.CheckRequirements" in code
    assert "GameConfiguration.SetToPreGame()" in code
    assert "Network.LeaveGame()" not in code
    assert all(name not in code for name in ("ContextPtr", "Controls", "UIManager", "OnLoadYes", "debug.setupvalue"))


def test_native_load_cannot_accept_normal_save_or_replace_active_game():
    conn = Connection()
    assert "GC_" in asyncio.run(load_native_test_save(conn, "normal-save"))
    conn.ensure_connected.assert_not_awaited()
    conn.lua_states = {5: "GameCore_Tuner", 24: "MainMenu"}
    assert "leaving" in asyncio.run(load_native_test_save(conn, "GC_NATIVE"))
    conn.execute_in_state_once.assert_not_awaited()


def test_only_verified_native_world_completes_previous_load_receipt():
    conn=Connection()
    conn.execute_in_state_once.side_effect=[[],["[GC_LOAD] VERIFIED|GC_NATIVE|3"]]
    with patch("civ_mcp.game_lifecycle._wait_for_loaded_game",new=AsyncMock(return_value=True)):
        result=asyncio.run(load_native_test_save(conn,"GC_NATIVE_T3"))
    assert result.startswith("Loading save:")
    assert conn._native_completed_save_load=="GC_NATIVE_T3"
    assert conn.execute_in_state_once.await_count==2
    completion=conn.execute_in_state_once.call_args.args[1]
    assert "Unexpected native loaded world" in completion and 'receipt.State="COMPLETE"' in completion
