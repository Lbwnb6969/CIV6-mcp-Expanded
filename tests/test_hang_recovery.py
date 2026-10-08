"""Tests for HANG recovery and post-load civ verification.

Tests the autosave cleanup logic and GameState hang guard field.
"""

import os

from civ_mcp.game_lifecycle import cleanup_old_autosaves


# ---------------------------------------------------------------------------
# cleanup_old_autosaves
# ---------------------------------------------------------------------------


class TestCleanupAutosaves:
    def test_keeps_n_most_recent(self, tmp_path, monkeypatch):
        """With 12 saves and keep=8, the 4 oldest are archived."""
        monkeypatch.setattr("civ_mcp.game_launcher.SINGLE_SAVE_DIR", str(tmp_path))
        # Create 12 fake saves with staggered mtimes
        for i in range(12):
            p = tmp_path / f"0_MCP_{i:04d}.Civ6Save"
            p.write_text("x")
            os.utime(p, (1000 + i, 1000 + i))

        cleanup_old_autosaves(keep=8)

        remaining = sorted(p.name for p in tmp_path.glob("0_MCP_*.Civ6Save"))
        assert len(remaining) == 8
        # The 8 newest (highest mtime) should survive
        expected = sorted(f"0_MCP_{i:04d}.Civ6Save" for i in range(4, 12))
        assert remaining == expected
        archive = tmp_path / "_abandoned_mcp"
        archived = sorted(p.name for p in archive.glob("0_MCP_*.Civ6Save"))
        assert archived == sorted(f"0_MCP_{i:04d}.Civ6Save" for i in range(4))
        assert (archive / "0_MCP_0000.Civ6Save").read_text() == "x"

    def test_no_delete_when_under_limit(self, tmp_path, monkeypatch):
        monkeypatch.setattr("civ_mcp.game_launcher.SINGLE_SAVE_DIR", str(tmp_path))
        for i in range(5):
            (tmp_path / f"0_MCP_{i:04d}.Civ6Save").write_text("x")

        cleanup_old_autosaves(keep=8)
        assert len(list(tmp_path.glob("0_MCP_*.Civ6Save"))) == 5

    def test_exactly_at_limit(self, tmp_path, monkeypatch):
        monkeypatch.setattr("civ_mcp.game_launcher.SINGLE_SAVE_DIR", str(tmp_path))
        for i in range(8):
            (tmp_path / f"0_MCP_{i:04d}.Civ6Save").write_text("x")

        cleanup_old_autosaves(keep=8)
        assert len(list(tmp_path.glob("0_MCP_*.Civ6Save"))) == 8

    def test_archive_name_collision_keeps_both_saves(self, tmp_path, monkeypatch):
        monkeypatch.setattr("civ_mcp.game_launcher.SINGLE_SAVE_DIR", str(tmp_path))
        for i in range(3):
            p = tmp_path / f"0_MCP_{i:04d}.Civ6Save"
            p.write_text(f"save-{i}")
            os.utime(p, (1000 + i, 1000 + i))
        archive = tmp_path / "_abandoned_mcp"
        archive.mkdir()
        existing = archive / "0_MCP_0000.Civ6Save"
        existing.write_text("existing")

        cleanup_old_autosaves(keep=2)

        archived = sorted(p.name for p in archive.glob("0_MCP_0000*.Civ6Save"))
        assert archived == ["0_MCP_0000.Civ6Save", "0_MCP_0000_1000.Civ6Save"]
        assert (archive / "0_MCP_0000.Civ6Save").read_text() == "existing"
        assert (archive / "0_MCP_0000_1000.Civ6Save").read_text() == "save-0"


# ---------------------------------------------------------------------------
# GameState._hang_retry_active guard
# ---------------------------------------------------------------------------


class TestHangRetryGuard:
    def test_initial_state(self):
        """GameState starts with _hang_retry_active = False."""
        from unittest.mock import MagicMock

        from civ_mcp.game_state import GameState

        conn = MagicMock()
        gs = GameState(conn)
        assert gs._hang_retry_active is False
