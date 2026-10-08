"""Native mutations must never be replayed after a lost FireTuner response."""
import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from civ_mcp.connection import GameConnection
from civ_mcp import tuner_client
from civ_mcp.web_api import create_app
from unittest.mock import patch


def test_single_send_does_not_reconnect_or_replay_on_transport_loss():
    async def run():
        conn = GameConnection()
        conn.ensure_connected = AsyncMock()
        conn.reconnect = AsyncMock()
        conn._locked_execute = AsyncMock(side_effect=ConnectionError("lost after send"))
        with pytest.raises(ConnectionError):
            await conn.execute_in_state_once(7, "mutate()")
        assert conn._locked_execute.await_count == 1
        conn.reconnect.assert_not_awaited()
    asyncio.run(run())


def test_strict_connection_applies_once_policy_to_existing_tools():
    async def run():
        conn = GameConnection(replay_on_disconnect=False)
        conn.execute_in_state_once = AsyncMock(side_effect=ConnectionError("unknown"))
        conn.reconnect = AsyncMock()
        with pytest.raises(ConnectionError):
            await conn._execute_and_collect(7, "native_write()", 5.0)
        conn.execute_in_state_once.assert_awaited_once_with(7, "native_write()", 5.0)
        conn.reconnect.assert_not_awaited()
    asyncio.run(run())


def test_single_send_rejects_missing_sentinel(monkeypatch):
    async def run():
        conn = GameConnection()
        conn._reader = object()
        conn._writer = object()
        conn.ensure_connected = AsyncMock()
        send = AsyncMock()
        monkeypatch.setattr(tuner_client, "send_message", send)
        monkeypatch.setattr(tuner_client, "drain_messages", AsyncMock())
        monkeypatch.setattr(tuner_client, "recv_message_timeout", AsyncMock(side_effect=[
            SimpleNamespace(payload="O\x00GameCore_Tuner: mutation applied"), None]))
        with pytest.raises(ConnectionError, match="OUTCOME_UNKNOWN"):
            await conn.execute_in_state_once(7, "mutate()")
        assert send.await_count == 1
    asyncio.run(run())


def test_empty_handshake_is_unavailable_and_closes_socket(monkeypatch):
    # This test exercises the mocked handshake, independently of a running
    # resident service. Real reservation behavior has its own OS-lock tests.
    monkeypatch.setattr("civ_mcp.connection.native_session_active", lambda: False)
    async def run():
        conn = GameConnection()
        writer = SimpleNamespace(is_closing=lambda: False, close=lambda: None,
                                 wait_closed=AsyncMock())
        monkeypatch.setattr(tuner_client, "connect", AsyncMock(return_value=(object(), writer)))
        monkeypatch.setattr(tuner_client, "handshake", AsyncMock(return_value=("<no response>", [])))
        with pytest.raises(ConnectionError, match="no Lua states"):
            await conn.connect()
        assert not conn.is_connected
        writer.wait_closed.assert_awaited_once()
    asyncio.run(run())


class ProbeConnection:
    lua_states = {7: "GameCore_Tuner", 8: "FrontEnd", 9: "InGame"}

    def __init__(self):
        self.sent = []

    @asynccontextmanager
    async def exclusive(self):
        yield

    async def ensure_connected(self):
        pass

    async def execute_in_state_once(self, index, code, timeout):
        self.sent.append((index, code))
        return ["[GC_PREFLIGHT] READY"] if index == 9 else ["[GC_HTTP] OK"]


def client_for(conn, host="127.0.0.1"):
    return TestClient(create_app(SimpleNamespace(conn=conn)), client=(host, 1234))


def test_endpoint_checks_origin_run_and_unique_state_before_send():
    conn = ProbeConnection()
    body = {"state": "GameCore_Tuner", "code": "mutate()", "read_only": False}
    with client_for(conn) as client:
        assert client.post("/api/test/native-lua", json=body).status_code == 400
        body["expected_run"] = "GC_ACCEPTANCE"
        body["state"] = "unknown"
        assert client.post("/api/test/native-lua", json=body).status_code == 409
    with client_for(conn, host="192.0.2.1") as client:
        assert client.post("/api/test/native-lua", json=body).status_code == 403
    assert conn.sent == []


def test_turn_advance_rejects_unidentified_run_before_any_native_call():
    conn = ProbeConnection()
    with client_for(conn) as client:
        assert client.post("/api/test/advance", json={"turns": 1}).status_code == 400
        assert client.post("/api/test/advance", json={"expected_run": "normal-save", "turns": 1}).status_code == 400
    assert conn.sent == []


def test_endpoint_wraps_active_game_mutation_with_run_and_single_player_guards():
    conn = ProbeConnection()
    with client_for(conn) as client:
        response = client.post("/api/test/native-lua", json={"state": "GameCore_Tuner",
            "code": "mutate()", "read_only": False, "expected_run": "GC_ACCEPTANCE"})
    assert response.status_code == 200
    assert response.json()["replayed"] is False
    assert [index for index, _ in conn.sent] == [9, 7]
    assert '== "GC_ACCEPTANCE"' in conn.sent[1][1]
    assert "IsAnyMultiplayer()" in conn.sent[1][1]
    assert "IsHotseat()" in conn.sent[0][1]


def test_endpoint_does_not_report_success_when_lua_guard_rejects_operation():
    conn = ProbeConnection()
    conn.execute_in_state_once = AsyncMock(side_effect=[
        ["[GC_PREFLIGHT] READY"], ["[GC_HTTP] ERROR Independent test run mismatch"]])
    with client_for(conn) as client:
        response = client.post("/api/test/native-lua", json={"state": "GameCore_Tuner",
            "code": "mutate()", "read_only": False, "expected_run": "GC_ACCEPTANCE"})
    assert response.json()["lua_ok"] is False
    assert response.json()["complete"] is True


def test_turn_blocker_is_not_counted_as_progress_and_is_not_retried():
    conn = ProbeConnection()
    gs = SimpleNamespace(conn=conn, get_game_overview=AsyncMock(return_value=SimpleNamespace(turn=2)),
                         end_turn=AsyncMock(return_value="Cannot end turn: diplomacy encounter pending"))
    with patch("civ_mcp.web_api._prepare_test_turn", new=AsyncMock(return_value=[])):
        with TestClient(create_app(gs), client=("127.0.0.1", 1234)) as client:
            response = client.post("/api/test/advance", json={"expected_run":"GC_ACCEPTANCE", "turns":3})
    assert response.status_code == 200
    body=response.json()
    assert body["completed_steps"] == 0 and body["attempted_steps"] == 1
    assert body["results"][0]["advanced"] is False
    gs.end_turn.assert_awaited_once()


def test_resident_native_advance_bypasses_general_turn_and_popup_recovery():
    conn = ProbeConnection()
    conn.native_acceptance_owner = True
    gs = SimpleNamespace(conn=conn, get_game_overview=AsyncMock(return_value=SimpleNamespace(turn=2)),
                         end_turn=AsyncMock(side_effect=AssertionError("general turn fallback forbidden")))
    native = AsyncMock(return_value="BLOCKED: native turn still 2; request retained")
    with patch("civ_mcp.web_api._prepare_test_turn", new=AsyncMock(return_value=[])), \
         patch("civ_mcp.native_turn.advance_native_turn", new=native):
        with TestClient(create_app(gs), client=("127.0.0.1", 1234)) as client:
            response = client.post("/api/test/advance", json={"expected_run":"GC_ACCEPTANCE", "turns":3})
    assert response.json()["completed_steps"] == 0
    assert response.json()["attempted_steps"] == 1
    native.assert_awaited_once_with(gs, "GC_ACCEPTANCE")
    gs.end_turn.assert_not_awaited()


def test_native_core_receipt_counts_progress_when_ui_overview_lags():
    conn = ProbeConnection()
    conn.native_acceptance_owner = True
    gs = SimpleNamespace(conn=conn, get_game_overview=AsyncMock(return_value=SimpleNamespace(turn=11)),
                         end_turn=AsyncMock(side_effect=AssertionError("general turn fallback forbidden")))
    async def native_turn(state, run):
        state._native_turn_receipt = (11, 12)
        return "Native turn advanced from 11 to 12"
    with patch("civ_mcp.web_api._prepare_test_turn", new=AsyncMock(return_value=[])), \
         patch("civ_mcp.native_turn.advance_native_turn", new=native_turn):
        with TestClient(create_app(gs), client=("127.0.0.1", 1234)) as client:
            response = client.post("/api/test/advance", json={"expected_run":"GC_ACCEPTANCE", "turns":1})
    body = response.json()
    assert body["completed_steps"] == 1 and body["final_turn"] == 12
    assert body["results"][0]["before_turn"] == 11
    gs.end_turn.assert_not_awaited()


def test_ui_overview_advance_without_confirmed_native_receipt_is_not_completion():
    conn = ProbeConnection()
    conn.native_acceptance_owner = True
    gs = SimpleNamespace(conn=conn, get_game_overview=AsyncMock(side_effect=[SimpleNamespace(turn=2), SimpleNamespace(turn=3)]))
    with patch("civ_mcp.web_api._prepare_test_turn", new=AsyncMock(return_value=[])), \
         patch("civ_mcp.native_turn.advance_native_turn", new=AsyncMock(return_value="BLOCKED: UI playback pending")):
        with TestClient(create_app(gs), client=("127.0.0.1", 1234)) as client:
            body = client.post("/api/test/advance", json={"expected_run":"GC_ACCEPTANCE", "turns":3}).json()
    assert body["completed_steps"] == 0 and body["attempted_steps"] == 1


@pytest.mark.parametrize("value", ["true", 1, None])
def test_informational_esc_flag_requires_explicit_boolean_before_any_native_call(value):
    conn = ProbeConnection()
    with client_for(conn) as client:
        response = client.post("/api/test/advance", json={"expected_run":"GC_ACCEPTANCE", "skip_information_popups":value})
    assert response.status_code == 400 and conn.sent == []
