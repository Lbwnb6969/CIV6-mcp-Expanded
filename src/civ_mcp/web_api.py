"""Lightweight HTTP API for the web dashboard.

Provides read-only JSON endpoints for game state. Runs embedded inside the
MCP server process, sharing the same GameConnection via create_app().
"""

import dataclasses
import hashlib
import logging
import time

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from civ_mcp.connection import LuaError
from civ_mcp.game_state import GameState
from civ_mcp import lua as lq
from civ_mcp.campaign_probe import CAMPAIGN_STATE_LUA, parse_campaign_state

log = logging.getLogger(__name__)


def _require_local_test_request(request: Request) -> None:
    """Keep write-capable test helpers local to this machine."""
    host = request.client.host if request.client else ""
    if host not in {"127.0.0.1", "::1", "localhost"}:
        raise HTTPException(status_code=403, detail="test helpers are local-only")


def _bounded_turn_count(payload: object) -> int:
    """Parse a deliberately small automated-turn budget."""
    if not isinstance(payload, dict):
        payload = {}
    try:
        count = int(payload.get("turns", 1))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="turns must be an integer")
    if count < 1 or count > 20:
        raise HTTPException(status_code=400, detail="turns must be between 1 and 20")
    return count


def _parse_active_mod_ids(lines: str) -> list[str]:
    """Parse the optional active-mod probe without making it authoritative."""
    for line in lines.splitlines():
        if not line.startswith("ACTIVE_MODS|"):
            continue
        payload = line.split("|", 1)[1].strip()
        if not payload:
            return []
        return [item for item in payload.split(",") if item]
    return []


_ACTIVE_MOD_PROBE_LUA = r'''
local available = Modding and Modding.GetActiveMods ~= nil
print("ACTIVE_AVAILABLE|" .. tostring(available))
if available then
    local ok, mods = pcall(Modding.GetActiveMods)
    print("ACTIVE_CALL|" .. tostring(ok))
    if ok and mods then
        for _, row in ipairs(mods) do
            local id = row.Id or row.ModId or row.ID
            if id and id ~= "" then print("ACTIVE_MOD|" .. tostring(id)) end
        end
    end
end
'''


def _parse_active_mod_probe(lines: str) -> dict[str, object]:
    """Parse the front-end active-mod probe without treating it as authoritative."""
    available = None
    called = None
    mods: list[str] = []
    for line in lines.splitlines():
        if line.startswith("ACTIVE_AVAILABLE|"):
            available = line.split("|", 1)[1].strip() == "true"
        elif line.startswith("ACTIVE_CALL|"):
            called = line.split("|", 1)[1].strip() == "true"
        elif line.startswith("ACTIVE_MOD|"):
            mod_id = line.split("|", 1)[1].strip()
            if mod_id:
                mods.append(mod_id)
    return {
        "available": available is True,
        "call_succeeded": called is True,
        "mods": mods,
    }


_AI_MOD_STATUS_LUA = r'''
local modifierCount = 0
if GameInfo and GameInfo.Modifiers then
    for row in GameInfo.Modifiers() do
        local id = row.ModifierId or row.ModifierType or ""
        if string.find(id, "EC_AI_", 1, true) then modifierCount = modifierCount + 1 end
    end
end
print("MODIFIERS|" .. modifierCount)
local activeMods = {}
if Modding and Modding.GetActiveMods then
    local ok, mods = pcall(Modding.GetActiveMods)
    if ok and mods then
        for _, row in ipairs(mods) do
            local id = row.Id or row.ModId or row.ID
            if id and id ~= "" then table.insert(activeMods, tostring(id)) end
        end
    end
end
print("ACTIVE_MODS|" .. table.concat(activeMods, ","))
local aiTest, speed = "nil", "nil"
if GameConfiguration then
    local okTest, valueTest = pcall(function() return GameConfiguration.GetValue("EC_AI_MILITARY_PRODUCTION_TEST") end)
    if okTest then aiTest = tostring(valueTest) end
    local okSpeed, valueSpeed = pcall(function() return GameConfiguration.GetValue("GAME_SPEED_TYPE") end)
    if okSpeed then speed = tostring(valueSpeed) end
end
print("CONFIG|" .. aiTest .. "|" .. speed)
local contextReady, playerProbeErrors = false, 0
if Players then
    for i = 0, 63 do
        local p = Players[i]
        local okPlayer, isTarget = pcall(function()
            return p and p:IsMajor() and p:IsAlive()
        end)
        if not okPlayer then playerProbeErrors = playerProbeErrors + 1 end
        if okPlayer and isTarget then
            contextReady = true
            local complete, status = "nil", "nil"
            local okComplete, valueComplete = pcall(function() return p:GetProperty("EC_AI_MILITARY_PRODUCTION_COMPLETE") end)
            if okComplete then complete = tostring(valueComplete) end
            local okStatus, valueStatus = pcall(function() return p:GetProperty("EC_AI_MILITARY_PRODUCTION_STATUS") end)
            if okStatus then status = tostring(valueStatus) end
            print("PLAYER|" .. i .. "|" .. complete .. "|" .. status)
        end
    end
end
print("CONTEXT|" .. tostring(contextReady) .. "|" .. playerProbeErrors)
'''


_ENDGAME_CAPABILITY_PROBE_LUA = r'''
-- Read-only capability inventory for the GrandCampaign vassal adapter.
-- Checking member presence is deliberately the only operation performed.
local function has(object, name)
    local ok, value = pcall(function()
        return object ~= nil and object[name] ~= nil
    end)
    return ok and value == true
end
local function cap(scope, name, object)
    print("CAP|" .. scope .. "|" .. name .. "|" .. tostring(has(object, name)))
end

cap("CityManager", "TransferCity", CityManager)
cap("CityManager", "TransferCityToFreeCities", CityManager)
cap("CityManager", "RequestCommand", CityManager)
cap("CityManager", "CanStartCommand", CityManager)
cap("CityManager", "SetAsCapital", CityManager)
cap("CityManager", "SetAsOriginalCapital", CityManager)
cap("UnitManager", "Kill", UnitManager)
cap("PlayerManager", "SetLocalPlayerAndObserver", PlayerManager)
cap("CityCommandTypes", "TRANSFER_CITY", CityCommandTypes)
cap("CityCommandTypes", "CHANGE_OWNER", CityCommandTypes)
cap("PlayerOperations", "VASSALIZE", PlayerOperations)
cap("PlayerOperations", "RESTORE_CIVILIZATION", PlayerOperations)

local me = nil
pcall(function() me = Game.GetLocalPlayer() end)
local player = nil
pcall(function() player = me ~= nil and Players[me] or nil end)
cap("Player", "GetCities", player)
cap("Player", "GetUnits", player)
cap("Player", "GetProperty", player)
cap("Player", "SetProperty", player)

local cities, units = nil, nil
pcall(function() if player and player.GetCities then cities = player:GetCities() end end)
pcall(function() if player and player.GetUnits then units = player:GetUnits() end end)
cap("Cities", "Destroy", cities)
cap("Units", "Destroy", units)
print("CAP_META|read_only|true|mutation_calls|0")
'''


_UI_CONTEXT_PROBE_LUA = r'''
-- Read-only UI context inventory.  This does not open, close, click, or
-- reparent anything; it only checks ContextPtr/Controls members in the
-- already-discovered FireTuner state.
local function has_control(name)
    local ok, value = pcall(function()
        return Controls ~= nil and Controls[name] ~= nil
    end)
    return ok and value == true
end
local launchbar = false
local function value_text(value)
    if value == nil then return "unknown" end
    return tostring(value)
end
local function control_state(name)
    local control = nil
    pcall(function()
        if Controls ~= nil then control = Controls[name] end
    end)
    if control == nil then return false, nil, nil, nil end
    local visible, size_x, size_y = nil, nil, nil
    pcall(function() visible = control:IsVisible() end)
    if visible == nil then pcall(function() visible = not control:IsHidden() end) end
    pcall(function() size_x = control:GetSizeX() end)
    pcall(function() size_y = control:GetSizeY() end)
    return true, visible, size_x, size_y
end
local function emit_control_state(name)
    local present, visible, size_x, size_y = control_state(name)
    print("UI_CONTROL_STATE|" .. name .. "|PRESENT|" .. tostring(present)
        .. "|VISIBLE|" .. value_text(visible)
        .. "|SIZE_X|" .. value_text(size_x)
        .. "|SIZE_Y|" .. value_text(size_y))
end
local launchbar_size_x, launchbar_size_y = nil, nil
pcall(function()
    local stack = ContextPtr ~= nil
        and ContextPtr:LookUpControl("/InGame/LaunchBar/ButtonStack")
    launchbar = stack ~= nil
    if stack ~= nil then
        pcall(function() launchbar_size_x = stack:GetSizeX() end)
        pcall(function() launchbar_size_y = stack:GetSizeY() end)
    end
end)
print("UI_PROBE|CONTEXT|" .. tostring(ContextPtr ~= nil)
    .. "|CONTROLS|" .. tostring(Controls ~= nil)
    .. "|LUA_EVENTS|" .. tostring(LuaEvents ~= nil)
    .. "|LAUNCHBAR|" .. tostring(launchbar))
print("UI_LAUNCHBAR_STATE|PRESENT|" .. tostring(launchbar)
    .. "|SIZE_X|" .. value_text(launchbar_size_x)
    .. "|SIZE_Y|" .. value_text(launchbar_size_y))
for _, name in ipairs({
    "Button", "Icon", "Panel", "CloseButton", "Summary", "Notice",
    "ArchiveScroll", "OperationStatus", "ReadOnly", "PopupDialog",
    "ConfirmPantheonButton", "CancelButton", "PantheonChooserSlideAnim",
}) do
    print("UI_CONTROL|" .. name .. "|" .. tostring(has_control(name)))
    emit_control_state(name)
end
print("UI_PROBE_META|read_only|true|mutation_calls|0")
'''


_UI_PROBE_CONTROL_NAMES = {
    "Button", "Icon", "Panel", "CloseButton", "Summary", "Notice",
    "ArchiveScroll", "OperationStatus", "ReadOnly", "PopupDialog",
    "ConfirmPantheonButton", "CancelButton", "PantheonChooserSlideAnim",
}


def _parse_endgame_capabilities(lines: str) -> dict[str, object]:
    """Parse the read-only native capability inventory."""
    capabilities: dict[str, dict[str, bool]] = {}
    read_only = False
    mutation_calls = None
    for line in lines.splitlines():
        if line.startswith("CAP|"):
            parts = line.split("|", 3)
            if len(parts) != 4:
                continue
            scope, name, value = parts[1], parts[2], parts[3]
            capabilities.setdefault(scope, {})[name] = value == "true"
        elif line.startswith("CAP_META|"):
            parts = line.split("|")
            if len(parts) == 5:
                read_only = parts[2] == "true"
                try:
                    mutation_calls = int(parts[4])
                except ValueError:
                    mutation_calls = None
    required = {
        "CityManager.TransferCity",
        "UnitManager.Kill",
        "Player.GetCities",
        "Player.GetUnits",
        "Cities.Destroy",
        "Units.Destroy",
    }
    standard_transfer_symbols = {
        "CityCommandTypes.TRANSFER_CITY",
        "CityCommandTypes.CHANGE_OWNER",
        "PlayerOperations.VASSALIZE",
        "PlayerOperations.RESTORE_CIVILIZATION",
    }
    present = {
        f"{scope}.{name}"
        for scope, entries in capabilities.items()
        for name, available in entries.items()
        if available
    }
    return {
        "capabilities": capabilities,
        "required_capabilities": sorted(required),
        "missing_required_capabilities": sorted(required - present),
        "native_prerequisites_ready": required <= present,
        "standard_transfer_symbols": sorted(standard_transfer_symbols),
        "missing_standard_transfer_symbols": sorted(standard_transfer_symbols - present),
        # Member presence is never an execution authorization. A reviewed
        # adapter must be wired explicitly before this can become true.
        "adapter_ready": False,
        "adapter_reason": "REVIEWED_NATIVE_ADAPTER_REQUIRED",
        "read_only_probe": read_only,
        "mutation_calls": mutation_calls,
    }


def _parse_ui_probe(lines: str) -> dict[str, object]:
    """Parse a read-only UI context/control inventory."""
    result: dict[str, object] = {
        "context_available": False,
        "controls_available": False,
        "lua_events_available": False,
        "launchbar_available": False,
        "controls": {},
        "control_states": {},
        "launchbar_state": {},
        "probe_complete": False,
        "read_only_probe": False,
        "mutation_calls": None,
    }
    controls: dict[str, bool] = {}
    control_states: dict[str, dict[str, object]] = {}
    launchbar_state: dict[str, object] = {}
    probe_keys = {"CONTEXT", "CONTROLS", "LUA_EVENTS", "LAUNCHBAR"}
    seen_probe_keys: set[str] = set()
    seen_control_states: set[str] = set()
    launchbar_state_seen = False
    meta_seen = False

    def parse_bool(value: str) -> bool | None:
        if value == "true":
            return True
        if value == "false":
            return False
        return None

    def parse_number(value: str) -> int | float | None:
        if value == "unknown":
            return None
        try:
            number = float(value)
        except ValueError:
            return None
        return int(number) if number.is_integer() else number

    for line in lines.splitlines():
        if line.startswith("UI_PROBE|"):
            parts = line.split("|")
            values = {}
            # The emitted format is KEY|VALUE pairs.  Do not mark a partial
            # line complete; a truncated FireTuner response is evidence loss.
            if len(parts) == 9:
                keys = parts[1::2]
                vals = parts[2::2]
                values = {key: value == "true" for key, value in zip(keys, vals)}
                seen_probe_keys = set(values)
            result["context_available"] = values.get("CONTEXT", False)
            result["controls_available"] = values.get("CONTROLS", False)
            result["lua_events_available"] = values.get("LUA_EVENTS", False)
            result["launchbar_available"] = values.get("LAUNCHBAR", False)
        elif line.startswith("UI_CONTROL|"):
            parts = line.split("|", 2)
            if len(parts) == 3:
                controls[parts[1]] = parts[2] == "true"
        elif line.startswith("UI_CONTROL_STATE|"):
            parts = line.split("|")
            if len(parts) == 10:
                name = parts[1]
                values = dict(zip(parts[2::2], parts[3::2]))
                control_states[name] = {
                    "present": parse_bool(values.get("PRESENT", "unknown")),
                    "visible": parse_bool(values.get("VISIBLE", "unknown")),
                    "size_x": parse_number(values.get("SIZE_X", "unknown")),
                    "size_y": parse_number(values.get("SIZE_Y", "unknown")),
                }
                seen_control_states.add(name)
        elif line.startswith("UI_LAUNCHBAR_STATE|"):
            parts = line.split("|")
            if len(parts) == 7:
                values = dict(zip(parts[1::2], parts[2::2]))
                launchbar_state = {
                    "present": values.get("PRESENT") == "true",
                    "size_x": None,
                    "size_y": None,
                }
                for key in ("SIZE_X", "SIZE_Y"):
                    value = values.get(key, "unknown")
                    if value != "unknown":
                        try:
                            number = float(value)
                            launchbar_state[key.lower()] = int(number) if number.is_integer() else number
                        except ValueError:
                            pass
                launchbar_state_seen = True
        elif line.startswith("UI_PROBE_META|"):
            parts = line.split("|")
            if len(parts) == 5:
                meta_seen = True
                result["read_only_probe"] = parts[2] == "true"
                try:
                    result["mutation_calls"] = int(parts[4])
                except ValueError:
                    result["mutation_calls"] = None
    result["controls"] = controls
    result["control_states"] = control_states
    result["launchbar_state"] = launchbar_state
    result["probe_complete"] = (
        seen_probe_keys == probe_keys
        and meta_seen
        and result["read_only_probe"] is True
        and result["mutation_calls"] == 0
        and launchbar_state_seen
        and seen_control_states >= set(_UI_PROBE_CONTROL_NAMES)
    )
    return result


def _ui_probe_evidence_level(probe: dict[str, object]) -> str:
    """Return the strongest safe evidence level for one UI state."""
    if "error" in probe:
        return "ERROR"
    if probe.get("probe_complete") is not True:
        return "INCOMPLETE"
    if probe.get("context_available") is not True or probe.get("controls_available") is not True:
        return "CONTEXT_PARTIAL"
    states = probe.get("control_states")
    if isinstance(states, dict):
        for state in states.values():
            if not isinstance(state, dict):
                continue
            size_x, size_y = state.get("size_x"), state.get("size_y")
            if (
                state.get("present") is True
                and state.get("visible") is True
                and type(size_x) in (int, float)
                and type(size_y) in (int, float)
                and size_x > 0
                and size_y > 0
            ):
                return "VISIBLE_CONTROL"
    return "CONTEXT_CONTROLS"


def _ui_probe_surface_matches(probe: dict[str, object]) -> list[str]:
    """Map a discovered state to named UI surfaces using read-only clues.

    Names are intentionally descriptive evidence labels, not activation
    claims.  A state can be incomplete and still be useful to locate when its
    native context name is known; the attached evidence level preserves that
    distinction for callers.
    """
    name = str(probe.get("name") or "").lower()
    states = probe.get("control_states")
    controls = probe.get("controls")
    present: set[str] = set()
    if isinstance(states, dict):
        present.update(
            key for key, value in states.items()
            if isinstance(value, dict) and value.get("present") is True
        )
    if isinstance(controls, dict):
        present.update(key for key, value in controls.items() if value is True)

    named_matches: list[str] = []
    if "overviewlaunch" in name:
        named_matches.append("overview_launch")
    if "overviewpanel" in name:
        named_matches.append("overview_panel")
    if "ec_consent" in name:
        named_matches.append("consent_popup")
    if "metricbridge" in name:
        named_matches.append("metric_bridge")
    if "pantheon" in name:
        named_matches.append("pantheon_replacement")
    if "naturalwonder" in name:
        named_matches.append("natural_wonder_replacement")
    if named_matches:
        return named_matches

    matches: list[str] = []
    # Launch instances live in a Lua-local table, not Controls.  Generic
    # Button/Icon members in another context cannot identify our entry.
    if {"Panel", "Summary", "ArchiveScroll", "OperationStatus", "ReadOnly"}.issubset(present):
        matches.append("overview_panel")
    if {"ConfirmPantheonButton", "PantheonChooserSlideAnim"}.issubset(present):
        matches.append("pantheon_replacement")
    return matches


def _summarize_ui_probes(probes: list[dict[str, object]]) -> dict[str, object]:
    """Classify UI probe evidence without claiming gameplay activation."""
    rank = {
        "NO_CANDIDATE": 0,
        "ERROR": 1,
        "INCOMPLETE": 2,
        "CONTEXT_PARTIAL": 3,
        "CONTEXT_CONTROLS": 4,
        "VISIBLE_CONTROL": 5,
    }
    counts = {name: 0 for name in rank}
    complete_states = 0
    visible_control_states = 0
    launchbar_present_states = 0
    highest = "NO_CANDIDATE" if not probes else "ERROR"
    states: list[dict[str, object]] = []
    surface_states: dict[str, list[dict[str, object]]] = {
        "overview_launch": [],
        "overview_panel": [],
        "consent_popup": [],
        "metric_bridge": [],
        "pantheon_replacement": [],
        "natural_wonder_replacement": [],
    }

    for probe in probes:
        level = _ui_probe_evidence_level(probe)
        if level in {"CONTEXT_CONTROLS", "VISIBLE_CONTROL"}:
            complete_states += 1
            launchbar = probe.get("launchbar_state")
            if isinstance(launchbar, dict) and launchbar.get("present") is True:
                launchbar_present_states += 1
            if level == "VISIBLE_CONTROL":
                visible_control_states += 1
        counts[level] += 1
        if rank[level] > rank[highest]:
            highest = level
        state = {
            "index": probe.get("index"),
            "name": probe.get("name"),
            "evidence_level": level,
        }
        states.append(state)
        for surface in _ui_probe_surface_matches(probe):
            surface_states[surface].append(dict(state))

    surface_status = {}
    for surface, candidates in surface_states.items():
        evidence = max(
            (candidate["evidence_level"] for candidate in candidates),
            key=rank.get,
            default="NO_CANDIDATE",
        )
        surface_status[surface] = {
            "candidate_count": len(candidates),
            "highest_evidence": evidence,
            "observed": bool(candidates),
        }

    return {
        "candidate_states": len(probes),
        "complete_states": complete_states,
        "visible_control_states": visible_control_states,
        "launchbar_present_states": launchbar_present_states,
        "highest_evidence": highest,
        "counts": counts,
        "states": states,
        "surface_states": surface_states,
        "surface_status": surface_status,
    }


def _parse_ai_mod_status(lines: str) -> dict[str, object]:
    """Parse the AI/mod probe, including partial menu-context responses."""
    result: dict[str, object] = {
        "modifier_count": 0,
        "active_mods": [],
        "config_test": "nil",
        "game_speed": "nil",
        "players": [],
        "context_ready": False,
        "player_probe_errors": 0,
    }
    players: list[dict[str, object]] = []
    for line in lines.splitlines():
        if line.startswith("MODIFIERS|"):
            try:
                result["modifier_count"] = int(line.split("|", 1)[1])
            except ValueError:
                pass
        elif line.startswith("ACTIVE_MODS|"):
            result["active_mods"] = _parse_active_mod_ids(line)
        elif line.startswith("CONFIG|"):
            parts = line.split("|", 2)
            if len(parts) == 3:
                result["config_test"], result["game_speed"] = parts[1], parts[2]
        elif line.startswith("PLAYER|"):
            parts = line.split("|", 3)
            if len(parts) == 4:
                try:
                    players.append(
                        {
                            "player_id": int(parts[1]),
                            "complete": parts[2],
                            "status": parts[3],
                        }
                    )
                except ValueError:
                    pass
        elif line.startswith("CONTEXT|"):
            parts = line.split("|", 2)
            if len(parts) == 3:
                result["context_ready"] = parts[1] == "true"
                try:
                    result["player_probe_errors"] = int(parts[2])
                except ValueError:
                    pass
    result["players"] = players
    return result


async def _prepare_test_turn(gs: GameState) -> list[str]:
    """Resolve ordinary human-side blockers before a bounded test turn.

    This helper deliberately chooses only existing legal options.  It does
    not create units, change AI state, or use arbitrary Lua, so the resulting
    AI observations still come from the normal game turn processor.
    """
    actions: list[str] = []
    try:
        cities, _errors = await gs.get_cities()
        for city in cities:
            if city.currently_building not in {"", "nothing", "NONE", "None"}:
                continue
            options = await gs.list_city_production(city.city_id)
            units = [o for o in options if o.category == "UNIT" and not o.is_repair]
            if not units:
                continue
            preference = {
                "UNIT_WARRIOR": 0,
                "UNIT_SPEARMAN": 1,
                "UNIT_SLINGER": 2,
                "UNIT_SCOUT": 3,
            }
            choice = min(
                units,
                key=lambda o: (preference.get(o.item_name, 10), o.cost, o.item_name),
            )
            result = await gs.set_city_production(city.city_id, choice.category, choice.item_name)
            actions.append(f"production:{city.city_id}:{choice.item_name}:{result}")
    except Exception as exc:
        actions.append(f"production-error:{type(exc).__name__}")

    try:
        tech_civics = await gs.get_tech_civics()
        if (
            tech_civics.current_research in {"", "None", "NONE"}
            and tech_civics.available_techs
        ):
            choice = min(
                tech_civics.available_techs,
                key=lambda item: (not item.boosted, item.turns, item.tech_type),
            )
            actions.append(f"research:{await gs.set_research(choice.tech_type)}")
        if (
            tech_civics.current_civic in {"", "None", "NONE"}
            and tech_civics.available_civics
        ):
            choice = min(
                tech_civics.available_civics,
                key=lambda item: (not item.boosted, item.turns, item.civic_type),
            )
            actions.append(f"civic:{await gs.set_civic(choice.civic_type)}")
    except Exception as exc:
        actions.append(f"research-civic-error:{type(exc).__name__}")

    try:
        actions.append(f"units:{await gs.skip_remaining_units()}")
    except Exception as exc:
        actions.append(f"units-error:{type(exc).__name__}")

    try:
        policies = await gs.get_policies()
        assignments: dict[int, str] = {}
        for slot in policies.slots:
            if slot.current_policy is not None:
                continue
            compatible = [
                policy
                for policy in policies.available_policies
                if policy.slot_type == slot.slot_type
            ]
            if compatible:
                choice = min(
                    compatible,
                    key=lambda policy: (
                        policy.policy_type != "POLICY_AGOGE",
                        policy.policy_type,
                    ),
                )
                assignments[slot.slot_index] = choice.policy_type
        if assignments:
            actions.append(f"policies:{await gs.set_policies(assignments)}")
    except Exception as exc:
        actions.append(f"policies-error:{type(exc).__name__}")
    return actions


def create_app(gs: GameState) -> FastAPI:
    """Create a FastAPI app wired to the given GameState."""
    app = FastAPI(
        title="civ6-mcp API",
        description="Read-only game state API for the Civ 6 web dashboard",
    )
    app.state.gs = gs

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:3001"],
        allow_methods=["GET"],
        allow_headers=["*"],
    )

    @app.exception_handler(ConnectionError)
    async def connection_error_handler(request, exc):
        return JSONResponse(
            status_code=503,
            content={"error": "Game not connected", "detail": str(exc)},
        )

    @app.exception_handler(LuaError)
    async def lua_error_handler(request, exc):
        return JSONResponse(
            status_code=502,
            content={"error": "Lua error", "detail": str(exc)},
        )

    async def tuner_unavailable_handler(request, exc):
        """Normalize FireTuner EOF/transport loss to a retryable 503."""
        return JSONResponse(
            status_code=503,
            content={"error": "Game not connected", "detail": str(exc)},
        )

    app.add_exception_handler(EOFError, tuner_unavailable_handler)
    app.add_exception_handler(OSError, tuner_unavailable_handler)

    @app.get("/api/overview")
    async def overview(request: Request):
        ov = await request.app.state.gs.get_game_overview()
        return _to_dict(ov)

    @app.get("/api/units")
    async def units(request: Request):
        data = await request.app.state.gs.get_units()
        return _to_dict(data)

    @app.get("/api/cities")
    async def cities(request: Request):
        data = await request.app.state.gs.get_cities()
        return _to_dict(data)

    @app.get("/api/map")
    async def map_area(
        request: Request,
        x: int = Query(..., description="Center X coordinate"),
        y: int = Query(..., description="Center Y coordinate"),
        radius: int = Query(3, ge=1, le=5, description="Radius (1-5)"),
    ):
        tiles = await request.app.state.gs.get_map_area(x, y, radius)
        return _to_dict(tiles)

    @app.get("/api/resources")
    async def resources(request: Request):
        (
            stockpiles,
            owned,
            nearby,
            luxury_count,
        ) = await request.app.state.gs.get_empire_resources()
        return {
            "stockpiles": _to_dict(stockpiles),
            "owned": _to_dict(owned),
            "nearby": _to_dict(nearby),
            "luxury_count": luxury_count,
        }

    @app.get("/api/tech")
    async def tech(request: Request):
        data = await request.app.state.gs.get_tech_civics()
        return _to_dict(data)

    @app.get("/api/diplomacy")
    async def diplomacy(request: Request):
        data = await request.app.state.gs.get_diplomacy()
        return _to_dict(data)

    @app.get("/api/victory")
    async def victory(request: Request, include_unmet: bool = Query(False)):
        """Return the full native victory/demographics snapshot.

        Unlike diplomacy, this includes anonymized military and city
        summaries for major civilizations that have not been met yet.
        """
        data = await request.app.state.gs.get_victory_progress(
            include_unmet=include_unmet
        )
        return _to_dict(data)

    @app.get("/api/test/ai-mod-status")
    async def ai_mod_status(request: Request):
        """Inspect GrandCampaign AI rows without changing game state."""
        _require_local_test_request(request)
        gs = request.app.state.gs
        lines = await gs.execute_lua(
            _AI_MOD_STATUS_LUA, context="gamecore"
        )
        result = _parse_ai_mod_status(lines)

        # GameCore often does not expose Modding.GetActiveMods.  Probe the
        # stable ``Main State`` context separately when available.  The
        # result is diagnostic only: local-profile activation and save
        # metadata are not guaranteed to be represented by this API.
        conn = gs.conn
        probe_state = next(
            (index for index, name in conn.lua_states.items() if name == "Main State"),
            None,
        )
        if probe_state is not None:
            try:
                probe_lines = await conn.execute_in_state(
                    probe_state,
                    _ACTIVE_MOD_PROBE_LUA + 'print("---END---")',
                )
                probe = _parse_active_mod_probe("\n".join(probe_lines))
                result["active_mods"] = probe["mods"]
                result["active_mods_available"] = probe["available"]
                result["active_mod_probe_state"] = probe_state
            except Exception as exc:
                log.debug("Active-mod probe failed: %s", exc)
                result["active_mods_available"] = False
                result["active_mod_probe_error"] = type(exc).__name__
        else:
            result["active_mods_available"] = False
            result["active_mod_probe_error"] = "Main State unavailable"
        return result

    @app.get("/api/test/ai-observation")
    async def ai_observation(request: Request):
        """Return a full omniscient turn snapshot for local AI A/B probes.

        The snapshot uses the existing diary query, which records per-major
        player cities, production, units, yields and strategic resources.  It
        is intentionally local-only and read-only so it cannot affect a game.
        """
        _require_local_test_request(request)
        started = time.perf_counter()
        snapshot = await request.app.state.gs.get_diary_snapshot()
        elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
        return {"elapsed_ms": elapsed_ms, "snapshot": _to_dict(snapshot)}

    @app.get("/api/test/campaign-state")
    async def campaign_state(request: Request):
        """Read campaign configuration and persisted records over the shared connection."""
        _require_local_test_request(request)
        lines = await request.app.state.gs.execute_lua(CAMPAIGN_STATE_LUA, context="gamecore")
        return parse_campaign_state(lines)

    @app.get("/api/test/endgame-capabilities")
    async def endgame_capabilities(request: Request):
        """Inventory native method presence for the Endgame adapter.

        This endpoint is intentionally local-only and read-only.  It checks
        member presence in the InGame Lua context without invoking any city,
        unit, player, or property mutation.  A positive inventory is still a
        prerequisite, not proof that a transaction is safe to execute.
        """
        _require_local_test_request(request)
        gs = request.app.state.gs
        lines = await gs.execute_lua(
            _ENDGAME_CAPABILITY_PROBE_LUA, context="ingame"
        )
        return _parse_endgame_capabilities(lines)

    @app.get("/api/test/lua-states")
    async def lua_states(request: Request):
        """Expose FireTuner state discovery for local menu/game diagnostics."""
        _require_local_test_request(request)
        conn = request.app.state.gs.conn
        await conn.ensure_connected()
        return {
            "states": [
                {"index": index, "name": name}
                for index, name in sorted(conn.lua_states.items())
            ],
            "gamecore_index": conn.gamecore_index,
            "ingame_index": conn.ingame_index,
        }

    @app.post("/api/test/refresh-states")
    async def refresh_states(request: Request):
        """Rediscover menu/game contexts using the single resident connection."""
        _require_local_test_request(request)
        conn = request.app.state.gs.conn
        async with conn.exclusive():
            await conn.reconnect()
        return {"states": [{"index": i, "name": n} for i, n in sorted(conn.lua_states.items())]}

    @app.post("/api/test/native-lua")
    async def native_lua(request: Request):
        """Execute an explicitly supplied native probe once, without GUI fallback.

        Mutations are restricted to the front end or a named independent
        single-player GC_ run. An incomplete response is never replayed.
        """
        _require_local_test_request(request)
        payload = await request.json()
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="JSON object required")
        code, state = payload.get("code"), payload.get("state")
        read_only = payload.get("read_only", True)
        expected_run = payload.get("expected_run", "")
        if not isinstance(code, str) or not 1 <= len(code) <= 65536:
            raise HTTPException(status_code=400, detail="code must contain 1..65536 characters")
        if not isinstance(state, str) or not isinstance(read_only, bool):
            raise HTTPException(status_code=400, detail="exact state name and boolean read_only required")
        if not read_only and (
            not isinstance(expected_run, str) or not expected_run.startswith("GC_")
            or not expected_run.replace("_", "").isalnum()
        ):
            raise HTTPException(status_code=400, detail="mutation requires a plain GC_ independent run name")
        conn = request.app.state.gs.conn
        started = time.perf_counter()
        async with conn.exclusive():
            await conn.ensure_connected()
            matches = [i for i, n in conn.lua_states.items() if n == state]
            if len(matches) != 1:
                raise HTTPException(status_code=409, detail="state missing or ambiguous; refresh contexts")
            guard = ""
            if not read_only:
                if state in {"FrontEnd", "MainMenu", "AdvancedSetup", "LoadGameMenu"}:
                    guard = 'assert(UI.IsInFrontEnd(), "Front-end mutation requires menu");\n'
                else:
                    guard = (
                        f'assert(GameConfiguration.GetValue("GC_NATIVE_RUN") == "{expected_run}", '
                        '"Independent test run mismatch");\n'
                        'assert(not GameConfiguration.IsAnyMultiplayer(), '
                        '"Single-player test required");\n'
                    )
                    # IsHotseat is an InGame-only API in this Steam build.
                    # Check it there before sending a GameCore mutation.
                    ui_states = [i for i, n in conn.lua_states.items() if n == "InGame"]
                    if len(ui_states) != 1:
                        raise HTTPException(status_code=409, detail="InGame single-player preflight unavailable")
                    preflight = guard + (
                        'assert(GameConfiguration.IsHotseat() == false, "Hotseat test not allowed"); '
                        'print("[GC_PREFLIGHT] READY"); '
                        f'print("{lq.SENTINEL}")'
                    )
                    checked = await conn.execute_in_state_once(ui_states[0], preflight, timeout=10.0)
                    if "[GC_PREFLIGHT] READY" not in checked:
                        raise HTTPException(status_code=409, detail="Independent single-player preflight failed")
            wrapped = (
                "local gc_ok,gc_error=xpcall(function()\n" + guard + code
                + '\nend,function(e) return tostring(e) end); '
                + 'if gc_ok then print("[GC_HTTP] OK") else print("[GC_HTTP] ERROR "..gc_error) end; '
                + f'print("{lq.SENTINEL}")'
            )
            log.info("Native acceptance probe state=%s read_only=%s run=%s sha256=%s",
                     state, read_only, expected_run, hashlib.sha256(code.encode()).hexdigest())
            lines = await conn.execute_in_state_once(matches[0], wrapped, timeout=20.0)
        return {"state": state, "read_only": read_only, "expected_run": expected_run,
                "sha256": hashlib.sha256(code.encode()).hexdigest(), "complete": True,
                "lua_ok": "[GC_HTTP] OK" in lines, "replayed": False, "lines": lines,
                "elapsed_ms": round((time.perf_counter() - started) * 1000, 2)}

    @app.get("/api/test/ui-probe")
    async def ui_probe(request: Request):
        """Inventory discovered UI contexts and key controls without mutation.

        This endpoint is deliberately observational.  It does not open a
        popup, publish a LuaEvent, click a control, or change a property.  A
        missing custom context is reported as an empty candidate list rather
        than being inferred from the mod manifest.
        """
        _require_local_test_request(request)
        conn = request.app.state.gs.conn
        await conn.ensure_connected()
        needles = (
            "InGame",
            "Overview",
            "LaunchBar",
            "Pantheon",
            "NaturalWonder",
            "EC_",
            "GMS_",
            "Popup",
            "Chooser",
        )
        candidates = [
            (index, name)
            for index, name in sorted(conn.lua_states.items())
            if any(needle.lower() in name.lower() for needle in needles)
        ]
        if conn.ingame_index is not None and not any(
            index == conn.ingame_index for index, _name in candidates
        ):
            candidates.append((conn.ingame_index, conn.lua_states.get(conn.ingame_index, "InGame")))
        probes = []
        for index, name in candidates[:64]:
            try:
                lines = await conn.execute_in_state(
                    index,
                    _UI_CONTEXT_PROBE_LUA + f'print("{lq.SENTINEL}")',
                )
                parsed = _parse_ui_probe("\n".join(lines))
                parsed.update({"index": index, "name": name})
                probes.append(parsed)
            except Exception as exc:
                probes.append({"index": index, "name": name, "error": type(exc).__name__})
        return {
            "candidate_states": len(candidates),
            "probes": probes,
            "summary": _summarize_ui_probes(probes),
            "read_only": True,
            "mutation_calls": 0,
        }

    @app.get("/api/test/frontend-probe")
    async def frontend_probe(request: Request):
        """Report which front-end states expose the native save-list API."""
        _require_local_test_request(request)
        conn = request.app.state.gs.conn
        await conn.ensure_connected()
        probes = []
        for index, name in sorted(conn.lua_states.items()):
            if name not in {"FrontEnd", "LoadGameMenu", "MainMenu"}:
                continue
            try:
                lines = await conn.execute_in_state(
                    index,
                    'print("PROBE|UI|" .. tostring(UI ~= nil) .. "|QUERY|" .. '
                    'tostring(UI ~= nil and UI.QuerySaveGameList ~= nil) .. '
                    '"|EVENT|" .. tostring(LuaEvents ~= nil and '
                    'LuaEvents.FileListQueryResults ~= nil)); '
                    f'print("{lq.SENTINEL}")',
                )
                probes.append({"index": index, "name": name, "lines": lines})
            except Exception as exc:
                probes.append(
                    {"index": index, "name": name, "error": type(exc).__name__}
                )
        return {"probes": probes}

    @app.post("/api/test/load-save")
    async def test_load_save(request: Request):
        """Load a named save through the resident MCP FireTuner connection."""
        _require_local_test_request(request)
        try:
            payload = await request.json()
        except ValueError:
            payload = {}
        save_name = payload.get("save_name") if isinstance(payload, dict) else None
        if not isinstance(save_name, str):
            raise HTTPException(status_code=400, detail="save_name must be a string")
        save_name = save_name.strip()
        if (
            not save_name
            or save_name in {".", ".."}
            or any(separator in save_name for separator in ("\\", "/"))
        ):
            raise HTTPException(status_code=400, detail="save_name must be a plain save name")
        started = time.perf_counter()
        result = await request.app.state.gs.load_game_save(save_name)
        return {
            "save_name": save_name,
            "result": result,
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
        }

    @app.post("/api/test/advance")
    async def test_advance(request: Request):
        """Advance at most 20 normal turns for local native A/B probes.

        The endpoint is intentionally separate from the read-only API and
        rejects non-loopback callers. It resolves routine human gameplay
        choices. The reserved acceptance service uses a bounded, durable,
        single-dispatch turn path with explicitly enabled informational ESC;
        only tech/civic and disaster callbacks are allowed. The normal service
        keeps GameState.end_turn. The game engine processes AI turns.
        """
        _require_local_test_request(request)
        try:
            payload = await request.json()
        except ValueError:
            payload = {}
        count = _bounded_turn_count(payload)
        expected_run = payload.get("expected_run") if isinstance(payload, dict) else None
        if not isinstance(expected_run, str) or not expected_run.startswith("GC_") or not expected_run.replace("_", "").isalnum():
            raise HTTPException(status_code=400, detail="advance requires a plain GC_ independent run name")
        skip_info = payload.get("skip_information_popups", False)
        if type(skip_info) is not bool:
            raise HTTPException(status_code=400, detail="skip_information_popups must be a boolean")
        gs = request.app.state.gs
        native_owner = getattr(gs.conn, "native_acceptance_owner", False)
        if skip_info and not native_owner:
            raise HTTPException(status_code=409, detail="informational ESC requires the reserved native acceptance service")
        results: list[dict[str, object]] = []
        total_started = time.perf_counter()
        for _ in range(count):
            step_started = time.perf_counter()
            await gs.conn.ensure_connected()
            ui_states = [i for i, n in gs.conn.lua_states.items() if n == "InGame"]
            if len(ui_states) != 1:
                raise HTTPException(status_code=409, detail="Independent game context missing or ambiguous")
            checked = await gs.conn.execute_in_state_once(ui_states[0],
                f'assert(GameConfiguration.GetValue("GC_NATIVE_RUN") == "{expected_run}"); '
                'assert(not GameConfiguration.IsAnyMultiplayer() and not GameConfiguration.IsHotseat()); '
                'print("[GC_PREFLIGHT] READY"); ' + f'print("{lq.SENTINEL}")', timeout=10.0)
            if "[GC_PREFLIGHT] READY" not in checked:
                raise HTTPException(status_code=409, detail="Independent single-player run mismatch")
            before = await gs.get_game_overview()
            actions = await _prepare_test_turn(gs)
            if native_owner:
                from civ_mcp.native_turn import advance_native_turn
                if skip_info:
                    result = await advance_native_turn(gs, expected_run, skip_information_popups=True)
                else:
                    result = await advance_native_turn(gs, expected_run)
            else:
                result = await gs.end_turn()
            overview = await gs.get_game_overview()
            before_turn, after_turn = before.turn, overview.turn
            advanced = after_turn > before_turn
            if native_owner:
                receipt = getattr(gs, "_native_turn_receipt", None)
                advanced = receipt is not None and receipt[1] > receipt[0]
                if receipt is not None:
                    before_turn, after_turn = receipt
            results.append(
                {
                    "turn": after_turn,
                    "before_turn": before_turn,
                    "advanced": advanced,
                    "information_popups": getattr(gs, "_native_popup_receipts", []) if native_owner else [],
                    "actions": actions,
                    "result": result,
                    "elapsed_ms": round(
                        (time.perf_counter() - step_started) * 1000, 2
                    ),
                }
            )
            upper = result.upper()
            if not advanced or any(marker in upper for marker in ("BLOCKED", "HANG", "GAME OVER", "ERROR:")):
                break
        return {
            "requested_turns": count,
            "completed_steps": sum(step["advanced"] is True for step in results),
            "attempted_steps": len(results),
            "final_turn": results[-1]["turn"] if results else None,
            "total_elapsed_ms": round(
                (time.perf_counter() - total_started) * 1000, 2
            ),
            "results": results,
        }

    return app


def _to_dict(obj):
    """Serialize dataclass instances (including nested) to plain dicts."""
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return dataclasses.asdict(obj)
    if isinstance(obj, (list, tuple)):
        return [_to_dict(item) for item in obj]
    if isinstance(obj, dict):
        return {k: _to_dict(v) for k, v in obj.items()}
    return obj
