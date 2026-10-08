"""Tests for the shared-MCP read-only smoke check."""

import importlib.util
from pathlib import Path


_SCRIPT = Path(__file__).parents[1] / "scripts" / "codex_readonly_check.py"
_SPEC = importlib.util.spec_from_file_location("codex_readonly_check", _SCRIPT)
assert _SPEC and _SPEC.loader
check = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(check)


def test_embedded_api_summary_uses_shared_endpoints(monkeypatch):
    responses = {
        "/api/overview": {"turn": 7, "civ_name": "Rome", "leader_name": "Trajan", "difficulty": "Prince", "game_speed": "Standard"},
        "/api/cities": [[{"name": "Rome"}], []],
        "/api/diplomacy": [{"player_id": 1}],
    }
    monkeypatch.setattr(check, "_get_json", responses.__getitem__)

    assert check._run_embedded_api() == {
        "source": "embedded-api",
        "gamecore_index": None,
        "ingame_index": None,
        "turn": 7,
        "civ": "Rome",
        "leader": "Trajan",
        "cities": 1,
        "city_errors": [],
        "diplomacy_entries": 1,
        "difficulty": "Prince",
        "game_speed": "Standard",
    }


def test_embedded_api_failure_allows_direct_fallback(monkeypatch):
    def unavailable(_path):
        raise OSError("server unavailable")

    monkeypatch.setattr(check, "_get_json", unavailable)
    assert check._run_embedded_api() is None
