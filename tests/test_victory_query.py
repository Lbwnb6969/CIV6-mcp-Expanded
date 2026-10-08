"""Regression checks for anonymized all-major victory snapshots."""

from civ_mcp.lua.victory import build_victory_progress_query


def test_default_victory_query_keeps_unmet_players_hidden():
    query = build_victory_progress_query()
    assert "local includeUnmet = false" in query
    assert '"Unmet Player "' in query


def test_native_probe_query_includes_unmet_major_players():
    query = build_victory_progress_query(include_unmet=True)
    assert "local includeUnmet = true" in query
    assert "if met or includeUnmet then" in query
    assert "if i ~= me and met then" in query
