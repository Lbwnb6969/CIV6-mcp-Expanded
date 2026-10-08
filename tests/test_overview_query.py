"""Contract checks for optional overview APIs across Civ6 contexts."""

from civ_mcp.lua.overview import build_overview_query


def test_overview_optional_player_metrics_fail_closed():
    query = build_overview_query()

    assert "pcall(function() myScore = p:GetScore() or 0 end)" in query
    assert "pcall(function() favor = p:GetFavor() or 0 end)" in query
    assert "pcall(function() pDiplo = p:GetDiplomacy() end)" in query


def test_overview_diplomacy_dependent_rows_require_a_valid_object():
    query = build_overview_query()

    assert "if pDiplo and i ~= id" in query
    assert "if i == id or (pDiplo and pDiplo:HasMet(i)) then" in query
