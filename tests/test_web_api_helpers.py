"""Boundaries for the local native-probe HTTP helpers."""

import asyncio

import pytest
from starlette.requests import Request

from civ_mcp.web_api import (
    _AI_MOD_STATUS_LUA,
    _ENDGAME_CAPABILITY_PROBE_LUA,
    _UI_CONTEXT_PROBE_LUA,
    _UI_PROBE_CONTROL_NAMES,
    _parse_active_mod_probe,
    _bounded_turn_count,
    _parse_active_mod_ids,
    _parse_ai_mod_status,
    _parse_endgame_capabilities,
    _parse_ui_probe,
    _ui_probe_surface_matches,
    _summarize_ui_probes,
    create_app,
)
from civ_mcp.game_lifecycle import _frontend_state_index, _lua_string


def _complete_ui_probe_lines(*extra: str) -> list[str]:
    lines = [
        "UI_PROBE|CONTEXT|true|CONTROLS|true|LUA_EVENTS|true|LAUNCHBAR|true",
        "UI_LAUNCHBAR_STATE|PRESENT|true|SIZE_X|112|SIZE_Y|49",
    ]
    lines.extend(f"UI_CONTROL|{name}|true" for name in _UI_PROBE_CONTROL_NAMES)
    lines.extend(
        f"UI_CONTROL_STATE|{name}|PRESENT|true|VISIBLE|true|SIZE_X|100|SIZE_Y|20"
        for name in _UI_PROBE_CONTROL_NAMES
    )
    lines.extend(extra)
    lines.append("UI_PROBE_META|read_only|true|mutation_calls|0")
    return lines


def test_turn_budget_defaults_and_is_bounded():
    assert _bounded_turn_count({}) == 1
    assert _bounded_turn_count({"turns": "20"}) == 20

    with pytest.raises(Exception):
        _bounded_turn_count({"turns": 0})
    with pytest.raises(Exception):
        _bounded_turn_count({"turns": 21})


def test_turn_budget_rejects_non_numeric_values():
    with pytest.raises(Exception):
        _bounded_turn_count({"turns": "many"})


def test_active_mod_probe_is_optional_and_fail_closed():
    assert _parse_active_mod_ids("ACTIVE_MODS|abc,def") == ["abc", "def"]
    assert _parse_active_mod_ids("ACTIVE_MODS|") == []
    assert _parse_active_mod_ids("MODIFIERS|0") == []


def test_ui_probe_parser_reports_context_controls_and_read_only_boundary():
    parsed = _parse_ui_probe(
        "\n".join(
            _complete_ui_probe_lines("UI_CONTROL|Panel|false")
        )
    )
    assert parsed["context_available"] is True
    assert parsed["controls_available"] is True
    assert parsed["launchbar_available"] is True
    assert parsed["controls"]["Button"] is True
    assert parsed["controls"]["Panel"] is False
    assert parsed["launchbar_state"] == {"present": True, "size_x": 112, "size_y": 49}
    assert parsed["control_states"]["Panel"] == {
        "present": True,
        "visible": True,
        "size_x": 100,
        "size_y": 20,
    }
    assert parsed["probe_complete"] is True
    assert parsed["read_only_probe"] is True
    assert parsed["mutation_calls"] == 0


def test_ui_probe_parser_marks_truncated_output_incomplete():
    parsed = _parse_ui_probe("UI_PROBE|CONTEXT|true|CONTROLS|true")
    assert parsed["probe_complete"] is False


def test_ui_probe_parser_marks_missing_control_state_incomplete():
    lines = _complete_ui_probe_lines()
    lines.pop(-2)  # remove the final control-state line before metadata
    parsed = _parse_ui_probe("\n".join(lines))
    assert parsed["probe_complete"] is False


def test_ui_probe_parser_rejects_non_read_only_metadata():
    lines = _complete_ui_probe_lines()
    lines[-1] = "UI_PROBE_META|read_only|false|mutation_calls|1"
    parsed = _parse_ui_probe("\n".join(lines))
    assert parsed["probe_complete"] is False


def test_ui_probe_summary_classifies_visible_controls_without_overclaiming():
    parsed = _parse_ui_probe("\n".join(_complete_ui_probe_lines()))
    summary = _summarize_ui_probes([parsed])
    assert summary["highest_evidence"] == "VISIBLE_CONTROL"
    assert summary["complete_states"] == 1
    assert summary["visible_control_states"] == 1
    assert summary["launchbar_present_states"] == 1
    assert summary["states"] == [{
        "index": None,
        "name": None,
        "evidence_level": "VISIBLE_CONTROL",
    }]


def test_ui_probe_summary_keeps_incomplete_state_incomplete():
    summary = _summarize_ui_probes([_parse_ui_probe("UI_PROBE|CONTEXT|true")])
    assert summary["highest_evidence"] == "INCOMPLETE"
    assert summary["complete_states"] == 0


def test_ui_probe_maps_named_surfaces_without_claiming_activation():
    launch = _parse_ui_probe("\n".join(_complete_ui_probe_lines()))
    launch["name"] = "EC_OverviewLaunchUI"
    panel = _parse_ui_probe("\n".join(_complete_ui_probe_lines()))
    panel["name"] = "EC_OverviewPanelUI"
    assert _ui_probe_surface_matches(launch) == ["overview_launch"]
    assert _ui_probe_surface_matches(panel) == ["overview_panel"]
    summary = _summarize_ui_probes([launch, panel])
    assert summary["surface_states"]["overview_launch"][0]["evidence_level"] == "VISIBLE_CONTROL"
    assert summary["surface_states"]["overview_panel"][0]["name"] == "EC_OverviewPanelUI"
    assert summary["surface_status"]["overview_launch"] == {
        "candidate_count": 1,
        "highest_evidence": "VISIBLE_CONTROL",
        "observed": True,
    }
    assert summary["surface_status"]["consent_popup"] == {
        "candidate_count": 0,
        "highest_evidence": "NO_CANDIDATE",
        "observed": False,
    }


def test_ui_surface_mapping_does_not_mistake_generic_buttons_for_our_entry():
    generic = {
        "name": "OtherPopup",
        "controls": {"Button": True, "Icon": True, "Panel": True, "Summary": True},
        "launchbar_state": {"present": True},
    }
    assert _ui_probe_surface_matches(generic) == []
    assert _ui_probe_surface_matches({"name": "OtherConsentDialog"}) == []


def test_ui_surface_mapping_locates_incomplete_named_contexts_without_upgrading_evidence():
    probes = [
        {"index": index, "name": name, "probe_complete": False}
        for index, name in enumerate((
            "EC_OverviewLaunchUI", "EC_OverviewPanelUI", "EC_ConsentPopup",
            "EC_MetricBridge", "PantheonChooser", "NaturalWonderPopup",
        ))
    ]
    summary = _summarize_ui_probes(probes)
    assert summary["highest_evidence"] == "INCOMPLETE"
    assert summary["complete_states"] == 0
    for surface, states in summary["surface_states"].items():
        assert len(states) == 1, surface
        assert states[0]["evidence_level"] == "INCOMPLETE"
        assert summary["surface_status"][surface]["highest_evidence"] == "INCOMPLETE"


def test_ui_surface_mapping_identifies_hidden_panel_by_distinctive_controls():
    probe = {
        "name": "UnnamedContext",
        "control_states": {
            name: {"present": True, "visible": False}
            for name in ("Panel", "Summary", "ArchiveScroll", "OperationStatus", "ReadOnly")
        },
    }
    assert _ui_probe_surface_matches(probe) == ["overview_panel"]


def test_ui_probe_lua_contains_no_mutation_or_input_apis():
    assert "RequestPlayerOperation" not in _UI_CONTEXT_PROBE_LUA
    assert "SetProperty" not in _UI_CONTEXT_PROBE_LUA
    assert "RegisterCallback" not in _UI_CONTEXT_PROBE_LUA


def test_active_mod_probe_parses_available_and_unavailable_states():
    parsed = _parse_active_mod_probe(
        "ACTIVE_AVAILABLE|true\nACTIVE_CALL|true\n"
        "ACTIVE_MOD|2af0c438-bab0-4d0c-987f-0beaf0f7c7f2\n"
        "ACTIVE_MOD|7b9b91d9-6ce7-4fc7-93c2-1c170ccbf401\n"
    )
    assert parsed == {
        "available": True,
        "call_succeeded": True,
        "mods": [
            "2af0c438-bab0-4d0c-987f-0beaf0f7c7f2",
            "7b9b91d9-6ce7-4fc7-93c2-1c170ccbf401",
        ],
    }
    assert _parse_active_mod_probe("ACTIVE_AVAILABLE|false") == {
        "available": False,
        "call_succeeded": False,
        "mods": [],
    }


def test_ai_mod_probe_parses_partial_menu_context_without_players():
    parsed = _parse_ai_mod_status(
        "MODIFIERS|20\nACTIVE_MODS|mod-a,mod-b\nCONFIG|nil|327976177\n"
        "CONTEXT|false|3\n"
    )

    assert parsed["modifier_count"] == 20
    assert parsed["active_mods"] == ["mod-a", "mod-b"]
    assert parsed["context_ready"] is False
    assert parsed["player_probe_errors"] == 3
    assert parsed["players"] == []


def test_ai_mod_probe_lua_guards_partial_player_objects():
    assert "if Players then" in _AI_MOD_STATUS_LUA
    assert "local okPlayer, isTarget = pcall" in _AI_MOD_STATUS_LUA
    assert "if okPlayer and isTarget then" in _AI_MOD_STATUS_LUA


def test_endgame_capability_probe_is_read_only_and_fails_closed():
    parsed = _parse_endgame_capabilities(
        "CAP|CityManager|TransferCity|false\n"
        "CAP|UnitManager|Kill|true\n"
        "CAP|Player|GetCities|true\n"
        "CAP|Player|GetUnits|true\n"
        "CAP|Cities|Destroy|true\n"
        "CAP|Units|Destroy|false\n"
        "CAP_META|read_only|true|mutation_calls|0\n"
    )
    assert parsed["read_only_probe"] is True
    assert parsed["mutation_calls"] == 0
    assert parsed["native_prerequisites_ready"] is False
    assert parsed["adapter_ready"] is False
    assert parsed["adapter_reason"] == "REVIEWED_NATIVE_ADAPTER_REQUIRED"
    assert "CityCommandTypes.TRANSFER_CITY" in parsed["missing_standard_transfer_symbols"]
    assert "CityManager.TransferCity" in parsed["missing_required_capabilities"]
    assert "Units.Destroy" in parsed["missing_required_capabilities"]


def test_endgame_capability_probe_requires_all_mutation_prerequisites():
    lines = "\n".join(
        [
            "CAP|CityManager|TransferCity|true",
            "CAP|UnitManager|Kill|true",
            "CAP|Player|GetCities|true",
            "CAP|Player|GetUnits|true",
            "CAP|Cities|Destroy|true",
            "CAP|Units|Destroy|true",
            "CAP_META|read_only|true|mutation_calls|0",
        ]
    )
    parsed = _parse_endgame_capabilities(lines)
    assert parsed["native_prerequisites_ready"] is True
    assert parsed["adapter_ready"] is False


def test_endgame_capability_probe_lua_contains_no_mutation_calls():
    assert "TransferCity(" not in _ENDGAME_CAPABILITY_PROBE_LUA
    assert "Destroy(" not in _ENDGAME_CAPABILITY_PROBE_LUA
    assert "Kill(" not in _ENDGAME_CAPABILITY_PROBE_LUA
    assert "CAP_META|read_only|true|mutation_calls|0" in _ENDGAME_CAPABILITY_PROBE_LUA


def test_web_api_normalizes_firetuner_transport_loss():
    app = create_app(object())
    assert EOFError in app.exception_handlers
    assert OSError in app.exception_handlers


def test_endgame_capability_route_is_local_only_and_uses_ingame_context():
    class FakeGameState:
        async def execute_lua(self, code, context="gamecore"):
            assert code == _ENDGAME_CAPABILITY_PROBE_LUA
            assert context == "ingame"
            return "CAP_META|read_only|true|mutation_calls|0"

    app = create_app(FakeGameState())
    route = next(
        route for route in app.routes if route.path == "/api/test/endgame-capabilities"
    )
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/test/endgame-capabilities",
            "headers": [],
            "client": ("127.0.0.1", 0),
            "query_string": b"",
            "scheme": "http",
            "server": ("127.0.0.1", 8000),
            "app": app,
        }
    )
    assert asyncio.run(route.endpoint(request)) == {
        "capabilities": {},
        "required_capabilities": [
            "Cities.Destroy",
            "CityManager.TransferCity",
            "Player.GetCities",
            "Player.GetUnits",
            "UnitManager.Kill",
            "Units.Destroy",
        ],
        "missing_required_capabilities": [
            "Cities.Destroy",
            "CityManager.TransferCity",
            "Player.GetCities",
            "Player.GetUnits",
            "UnitManager.Kill",
            "Units.Destroy",
        ],
        "native_prerequisites_ready": False,
        "standard_transfer_symbols": [
            "CityCommandTypes.CHANGE_OWNER",
            "CityCommandTypes.TRANSFER_CITY",
            "PlayerOperations.RESTORE_CIVILIZATION",
            "PlayerOperations.VASSALIZE",
        ],
        "missing_standard_transfer_symbols": [
            "CityCommandTypes.CHANGE_OWNER",
            "CityCommandTypes.TRANSFER_CITY",
            "PlayerOperations.RESTORE_CIVILIZATION",
            "PlayerOperations.VASSALIZE",
        ],
        "adapter_ready": False,
        "adapter_reason": "REVIEWED_NATIVE_ADAPTER_REQUIRED",
        "read_only_probe": True,
        "mutation_calls": 0,
    }


def test_ui_probe_route_scans_discovered_contexts_without_mutation():
    class FakeConnection:
        lua_states = {3: "GameCore", 10: "EC_OverviewPanelUI"}
        ingame_index = 10

        async def ensure_connected(self):
            return None

        async def execute_in_state(self, index, code):
            assert index == 10
            assert code.startswith(_UI_CONTEXT_PROBE_LUA)
            return [
                *_complete_ui_probe_lines("UI_CONTROL|Panel|true"),
            ]

    class FakeGameState:
        conn = FakeConnection()

    app = create_app(FakeGameState())
    route = next(route for route in app.routes if route.path == "/api/test/ui-probe")
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/test/ui-probe",
            "headers": [],
            "client": ("127.0.0.1", 0),
            "query_string": b"",
            "scheme": "http",
            "server": ("127.0.0.1", 8000),
            "app": app,
        }
    )
    result = asyncio.run(route.endpoint(request))
    assert result["candidate_states"] == 1
    assert result["read_only"] is True
    assert result["mutation_calls"] == 0
    assert result["probes"][0]["name"] == "EC_OverviewPanelUI"
    assert result["probes"][0]["controls"]["Panel"] is True
    assert result["probes"][0]["probe_complete"] is True
    assert result["probes"][0]["control_states"]["Panel"]["visible"] is True
    assert result["summary"]["highest_evidence"] == "VISIBLE_CONTROL"


def test_load_save_route_delegates_to_resident_game_state():
    class FakeGameState:
        async def load_game_save(self, save_name):
            return f"Loading save: {save_name}"

    app = create_app(FakeGameState())
    route = next(route for route in app.routes if route.path == "/api/test/load-save")
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/test/load-save",
            "headers": [(b"content-type", b"application/json")],
            "client": ("127.0.0.1", 0),
            "query_string": b"",
            "scheme": "http",
            "server": ("127.0.0.1", 8000),
            "app": app,
        },
        receive=lambda: None,
    )

    async def receive():
        return {
            "type": "http.request",
            "body": b'{"save_name":"GC_RUNTIME_T30_ENDGAME_AUDIT"}',
            "more_body": False,
        }

    request = Request(request.scope, receive=receive)
    response = asyncio.run(route.endpoint(request))
    assert response["save_name"] == "GC_RUNTIME_T30_ENDGAME_AUDIT"
    assert response["result"].startswith("Loading save:")


def test_frontend_state_selection_and_lua_quoting_are_bounded():
    class FakeConnection:
        lua_states = {30: "FrontEnd", 5: "LoadGameMenu", 24: "MainMenu"}

    assert _frontend_state_index(FakeConnection()) == 30
    assert _lua_string('save"one') == '"save\\"one"'
