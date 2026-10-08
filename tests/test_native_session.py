"""Use actual OS locks to verify service isolation and release."""
import asyncio
import subprocess
import sys
from unittest.mock import AsyncMock

import pytest
from civ_mcp import connection, tuner_client
from civ_mcp.connection import GameConnection
from civ_mcp.native_session import native_session_active, reserve_native_session


def test_reservation_excludes_another_process_and_releases(tmp_path):
    path = tmp_path / "native.lock"
    assert not native_session_active(path)
    with reserve_native_session(path):
        assert native_session_active(path)
        with pytest.raises(ConnectionError):
            with reserve_native_session(path):
                pass
        result = subprocess.run([sys.executable, "-c",
            "import sys; from civ_mcp.native_session import native_session_active; "
            "assert native_session_active(sys.argv[1])", str(path)], capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
    assert not native_session_active(path)


def test_other_connections_are_rejected_before_handshake(monkeypatch):
    monkeypatch.setattr(connection, "native_session_active", lambda: True)
    send = AsyncMock()
    monkeypatch.setattr(tuner_client, "connect", send)
    conn = GameConnection()
    with pytest.raises(ConnectionError, match="reserved"):
        asyncio.run(conn.connect())
    with pytest.raises(ConnectionError, match="reserved"):
        asyncio.run(conn.ensure_connected())
    send.assert_not_awaited()
    owner = GameConnection(native_acceptance_owner=True)
    owner._check_native_reservation()
