"""Bounded, read-only GrandCampaign evidence, shared by HTTP and MCP tools."""

import math
from urllib.parse import unquote


CONFIG_KEYS = (
    "EC_OBSERVATION_ONLY", "EC_METRIC_BRIDGE_TEST", "EC_SHADOW_VASSAL_TEST",
    "EC_SHADOW_VASSAL_LIMIT", "EC_PLAYER_CONSENT_TEST", "EC_PROJECT_EFFECTS_TEST",
    "EC_AWAKENING_ENABLED", "EC_AWAKENING_DRY_RUN", "EC_AWAKENING_ACTIONS_TEST",
    "EC_AWAKENING_MIN_TURN", "EC_AWAKENING_ERA", "EC_AWAKENING_DEAD_PERCENT",
    "EC_AWAKENING_PREP_TURNS", "EC_AWAKENING_CONQUEST_TURNS",
    "EC_WAR_OBSERVATION_TEST", "EC_ELIGIBILITY_OBSERVATION_TEST",
    "EC_AI_MILITARY_PRODUCTION_TEST", "EC_API_PROBE", "EC_DEBUG_TIMING", "GAME_SPEED_TYPE",
    "GMS_NATURAL_WONDER_SAFE_MODE", "GMS_PANTHEON_FALLBACK",
    "GMS_SATELLITE_SAFE_MODE", "GMS_SATELLITE_CHUNKED_MODE", "GMS_SATELLITE_CHUNK_SIZE",
    "GMS_CLIMATE_DIAGNOSTICS", "GMS_DEBUG_TIMING", "GMS_SNAPSHOT_OBJECT_BUDGET",
)
GROUP_FIELDS = {
    "identity": ("Alive", "Major", "Human", "Era"),
    "awakening": ("STATE", "SCHEMA_VERSION", "MODE", "PAYLOAD_READY", "CIV",
                  "TRIGGER_TURN", "PREP_END_TURN", "CONQUEST_END_TURN", "LAST_TRANSITION_TURN"),
    "archive": ("STATE", "SCHEMA_VERSION", "OVERLORD", "AUTONOMY", "PROJECT",
                "PROJECT_FOCUS", "PROJECT_START_TURN", "PROJECT_END_TURN",
                "PROJECT_PENDING_TOKEN", "PROJECT_APPLIED_TOKEN", "PROJECT_EFFECT_STATE",
                "PROJECT_EFFECT_APPLIED_TOKEN"),
    "consent": ("STATE", "SCHEMA_VERSION", "MODE", "READY", "TOKEN", "OVERLORD",
                "CREATED_TURN", "EXPIRES_TURN"),
    "metrics": ("SCHEMA", "ACTIVE_BANK", "READY", "PlayerID", "AuthorID", "Turn",
                "Source", "Human", "Alive", "Major", "Independent", "SpeedMultiplier",
                "Cities", "Military", "NetGold", "GoldBalance", "Science", "Culture",
                "TechsCompleted", "CivicsCompleted"),
    "advantage": ("SCHEMA", "ACTIVE_BANK", "READY", "State", "Reason", "Turn",
                  "OverlordID", "TargetID", "Tier", "Samples", "LastSampleTurn"),
}


def _lua_list(values: tuple[str, ...]) -> str:
    return "{" + ",".join('"' + value + '"' for value in values) + "}"


CAMPAIGN_STATE_LUA = r'''
local records, rosterKnown = 0, true
local function scalar(callback)
    local ok, value = pcall(callback)
    if not ok then return "unreadable", "" end
    if value == nil then return "nil", "" end
    local kind = type(value)
    if kind ~= "string" and kind ~= "number" and kind ~= "boolean" then
        return "unreadable", ""
    end
    if kind == "number" and (value ~= value or value == math.huge or value == -math.huge) then
        return "unreadable", ""
    end
    local text = tostring(value):gsub("%%", "%%25"):gsub("|", "%%7C")
        :gsub("\r", "%%0D"):gsub("\n", "%%0A")
    return kind, text
end
local function emit(scope, id, key, callback)
    local kind, value = scalar(callback)
    print("GC_FIELD|" .. scope .. "|" .. id .. "|" .. key .. "|" .. kind .. "|" .. value)
    records = records + 1
end
emit("game", "-", "Turn", function() return Game.GetCurrentGameTurn() end)
emit("game", "-", "Multiplayer", function() return GameConfiguration.IsAnyMultiplayer() end)
for _, key in ipairs(CONFIG_FIELDS) do
    emit("config", "-", key, function() return GameConfiguration.GetValue(key) end)
end
for id = 0, 63 do
    local ok, player = pcall(function() return Players[id] end)
    if not ok then rosterKnown = false end
    if ok and player ~= nil then
        local majorOK, major = pcall(function() return player:IsMajor() end)
        if not majorOK or type(major) ~= "boolean" then rosterKnown = false end
        if majorOK and major == true then
            local function prop(group, field, key)
                emit(group, id, field, function() return player:GetProperty(key) end)
            end
            emit("identity", id, "Alive", function() return player:IsAlive() end)
            emit("identity", id, "Major", function() return player:IsMajor() end)
            emit("identity", id, "Human", function() return player:IsHuman() end)
            emit("identity", id, "Era", function() return player:GetEra() end)
            for _, field in ipairs(AWAKENING_FIELDS) do
                prop("awakening", field, "EC_AWAKENING_" .. field)
            end
            for _, field in ipairs(ARCHIVE_FIELDS) do
                prop("archive", field, "EC_VASSAL_" .. id .. "_" .. field)
            end
            for _, field in ipairs(CONSENT_FIELDS) do
                prop("consent", field, "EC_VASSAL_" .. id .. "_CONFIRMATION_" .. field)
            end
            for _, group in ipairs({"metrics", "advantage"}) do
                local prefix = group == "metrics" and "EC_METRICS_" or "EC_ADVANTAGE_"
                local bankOK, bank = pcall(function() return player:GetProperty(prefix .. "ACTIVE_BANK") end)
                for _, field in ipairs(group == "metrics" and METRIC_FIELDS or ADVANTAGE_FIELDS) do
                    if field == "SCHEMA" or field == "ACTIVE_BANK" then
                        prop(group, field, prefix .. field)
                    elseif bankOK and (bank == 1 or bank == 2) then
                        prop(group, field, prefix .. "BANK" .. bank .. "_" .. field)
                    else
                        emit(group, id, field, function()
                            if not bankOK or bank ~= nil then error("invalid bank") end
                            return nil
                        end)
                    end
                end
            end
        end
    end
end
print("GC_END|1|" .. records .. "|" .. tostring(rosterKnown) .. "|true|0")
'''
for name, fields in (
    ("CONFIG_FIELDS", CONFIG_KEYS), ("AWAKENING_FIELDS", GROUP_FIELDS["awakening"]),
    ("ARCHIVE_FIELDS", GROUP_FIELDS["archive"]), ("CONSENT_FIELDS", GROUP_FIELDS["consent"]),
    ("METRIC_FIELDS", GROUP_FIELDS["metrics"]), ("ADVANTAGE_FIELDS", GROUP_FIELDS["advantage"]),
):
    CAMPAIGN_STATE_LUA = "local " + name + "=" + _lua_list(fields) + "\n" + CAMPAIGN_STATE_LUA


def _scalar(kind: str, value: str) -> tuple[object, bool]:
    if kind in {"nil", "unreadable"} and value == "":
        return None, kind == "nil"
    if kind == "boolean" and value in {"true", "false"}:
        return value == "true", True
    if kind == "number":
        number = float(value)
        if math.isfinite(number):
            return int(number) if number.is_integer() else number, True
    if kind == "string":
        return unquote(value, encoding="utf-8", errors="strict"), True
    raise ValueError("invalid campaign scalar")


def parse_campaign_state(lines: str | list[str]) -> dict:
    """Preserve unavailable reads; reject truncated, duplicate or malformed evidence."""
    result = {"game": {}, "config": {}, "players": {}, "unreadable": [],
              "errors": [], "read_only": False, "mutation_calls": None,
              "adapter_ready": False, "native_gameplay_verified": False}
    seen = set()
    end = None
    for line in lines.splitlines() if isinstance(lines, str) else lines:
        if line.startswith("GC_END|"):
            if end is not None:
                result["errors"].append("DUPLICATE_END")
            end = line.split("|")
        elif line.startswith("GC_FIELD|"):
            try:
                _, scope, player, key, kind, value = line.split("|", 5)
                identity = (scope, player, key)
                if identity in seen or end is not None:
                    raise ValueError("duplicate/late field")
                allowed = {"game": ("Turn", "Multiplayer"), "config": CONFIG_KEYS}.get(scope)
                if allowed is not None:
                    if player != "-" or key not in allowed:
                        raise ValueError("invalid global field")
                    target = result[scope]
                else:
                    if scope not in GROUP_FIELDS or key not in GROUP_FIELDS[scope]:
                        raise ValueError("invalid player field")
                    if not player.isdigit() or not 0 <= int(player) < 64 or player != str(int(player)):
                        raise ValueError("invalid player slot")
                    target = result["players"].setdefault(player, {}).setdefault(scope, {})
                parsed, readable = _scalar(kind, value)
                target[key] = parsed
                seen.add(identity)
                if not readable:
                    result["unreadable"].append("/".join(identity))
            except (ValueError, UnicodeError):
                result["errors"].append("MALFORMED_FIELD")
    try:
        if end is None or len(end) != 6 or end[1] != "1" or int(end[2]) != len(seen):
            raise ValueError("truncated probe")
        result["roster_known"] = end[3] == "true"
        result["read_only"] = end[4] == "true"
        result["mutation_calls"] = int(end[5])
    except (ValueError, TypeError):
        result["errors"].append("INCOMPLETE_METADATA")
    if set(result["game"]) != {"Turn", "Multiplayer"} or set(result["config"]) != set(CONFIG_KEYS):
        result["errors"].append("INCOMPLETE_GLOBALS")
    for groups in result["players"].values():
        if set(groups) != set(GROUP_FIELDS) or any(
            set(groups.get(group, {})) != set(fields) for group, fields in GROUP_FIELDS.items()
        ):
            result["errors"].append("INCOMPLETE_PLAYER")
    turn = result["game"].get("Turn")
    result["context_ready"] = type(turn) is int and turn >= 0 and bool(result["players"])
    result["evidence_complete"] = (
        not result["errors"] and not result["unreadable"] and result.get("roster_known") is True
        and result["read_only"] is True and result["mutation_calls"] == 0 and result["context_ready"]
    )
    return result
