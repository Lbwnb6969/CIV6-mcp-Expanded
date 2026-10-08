from pathlib import Path

from civ_mcp import game_launcher
from civ_mcp.server import _archive_stale_mcp_saves


def test_stale_mcp_saves_are_moved_to_abandoned_folder(tmp_path, monkeypatch):
    monkeypatch.setattr(game_launcher, "SINGLE_SAVE_DIR", str(tmp_path))
    first = tmp_path / "0_MCP_0042.Civ6Save"
    second = tmp_path / "0_MCP_0043.Civ6Save"
    first.write_bytes(b"first")
    second.write_bytes(b"second")

    moved = _archive_stale_mcp_saves()

    archive = tmp_path / "_abandoned_mcp"
    assert sorted(Path(path).parent.name for path in moved) == [
        "_abandoned_mcp",
        "_abandoned_mcp",
    ]
    assert (archive / first.name).read_bytes() == b"first"
    assert (archive / second.name).read_bytes() == b"second"
    assert not first.exists()
    assert not second.exists()


def test_stale_mcp_archive_is_noop_when_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(game_launcher, "SINGLE_SAVE_DIR", str(tmp_path))
    assert _archive_stale_mcp_saves() == []
