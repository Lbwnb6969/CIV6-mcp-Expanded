"""Collect GrandCampaign evidence via resident HTTP or saved JSON; never launch or click."""

import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
from urllib.error import URLError
from urllib.request import urlopen


def flag(value):
    if value in (True, 1, "1", "true"):
        return True
    if value in (False, 0, "0", "false"):
        return False
    return None


def integer(value):
    return type(value) is int and value >= 0


def assess_campaign(state):
    """Explain evidence gaps without authorizing gameplay or inventing thresholds."""
    gaps = ["REVIEWED_NATIVE_ADAPTER_REQUIRED"]
    if (not isinstance(state, dict) or state.get("evidence_complete") is not True
            or state.get("read_only") is not True or state.get("mutation_calls") != 0
            or state.get("context_ready") is not True or not integer(state.get("game", {}).get("Turn"))):
        return {"evidence_ready": False, "playable_verified": False,
                "gaps": gaps + ["CAMPAIGN_EVIDENCE_INCOMPLETE"]}
    game, config, players = state.get("game", {}), state.get("config", {}), state.get("players", {})
    humans = [id for id, groups in players.items() if groups.get("identity", {}).get("Human") is True
              and groups.get("identity", {}).get("Alive") is True]
    if game.get("Multiplayer") is not False or len(humans) != 1:
        return {"evidence_ready": False, "playable_verified": False,
                "gaps": gaps + ["SINGLE_HUMAN_REQUIRED"]}
    owner = humans[0]
    groups = players[owner]
    turn = game.get("Turn")
    metrics = groups.get("metrics", {})
    data = "UNKNOWN"
    if flag(config.get("EC_METRIC_BRIDGE_TEST")) is False:
        data = "DISABLED"
    elif flag(config.get("EC_METRIC_BRIDGE_TEST")) is True:
        if metrics.get("ACTIVE_BANK") is None and (metrics.get("SCHEMA") is None
                or type(metrics.get("SCHEMA")) is int and metrics["SCHEMA"] == 1):
            data = "WAITING"
        elif (type(metrics.get("SCHEMA")) is int and metrics["SCHEMA"] == 1
              and type(metrics.get("ACTIVE_BANK")) is int and metrics["ACTIVE_BANK"] in (1, 2)
              and type(metrics.get("READY")) is int and metrics["READY"] == 1):
            numeric = ("Military", "GoldBalance", "Science", "Culture", "NetGold", "SpeedMultiplier")
            valid = all(type(metrics.get(key)) in (int, float) and math.isfinite(metrics[key]) for key in numeric)
            valid = valid and all(integer(metrics.get(key)) for key in ("Turn", "Cities", "TechsCompleted", "CivicsCompleted"))
            valid = valid and all(metrics[key] >= 0 for key in ("Military", "GoldBalance", "Science", "Culture"))
            valid = valid and metrics["SpeedMultiplier"] > 0
            valid = valid and metrics.get("PlayerID") == int(owner) and metrics.get("AuthorID") == int(owner)
            valid = valid and all(metrics.get(key) is True for key in ("Human", "Alive", "Major"))
            valid = valid and type(metrics.get("Independent")) is bool and metrics.get("Source") == "UI_BRIDGE_TEST"
            if valid and integer(turn) and metrics["Turn"] <= turn:
                data = "FRESH" if turn - metrics["Turn"] <= 5 else "STALE"
    if data != "FRESH":
        gaps.append("METRICS_" + data)
    awakening = groups.get("awakening", {})
    legacy = players.get("0", {}).get("awakening", {}) if owner != "0" else {}
    if legacy and any(legacy.get(key) is not None for key in ("STATE", "SCHEMA_VERSION", "MODE", "PAYLOAD_READY", "CIV")):
        gaps.append("LEGACY_AWAKENING_OWNER_CONFLICT")
    observation = flag(config.get("EC_OBSERVATION_ONLY"))
    if observation is True:
        gaps.append("OBSERVATION_ONLY")
    elif observation is None:
        gaps.append("OBSERVATION_MODE_UNKNOWN")
    stage = awakening.get("STATE")
    if stage is not None:
        expected = "LIVE_TEST" if flag(config.get("EC_AWAKENING_ENABLED")) is True and flag(
            config.get("EC_AWAKENING_ACTIONS_TEST")) is True and flag(config.get("EC_AWAKENING_DRY_RUN")) is False else "DRY_RUN"
        if awakening.get("SCHEMA_VERSION") != 1 or awakening.get("MODE") != expected:
            gaps.append("AWAKENING_RECORD_CONFLICT")
        if stage not in {"LOCKED", "TRIGGERED", "PREPARATION", "CONQUEST", "SHOWDOWN", "RESOLVED"}:
            gaps.append("AWAKENING_STAGE_UNKNOWN")
        elif stage != "LOCKED":
            fields = ("CIV", "TRIGGER_TURN", "PREP_END_TURN", "CONQUEST_END_TURN", "LAST_TRANSITION_TURN")
            valid = all(integer(awakening.get(key)) for key in fields)
            if valid:
                valid = (awakening["CIV"] < 64 and awakening["CIV"] != int(owner)
                         and awakening["TRIGGER_TURN"] < awakening["PREP_END_TURN"] < awakening["CONQUEST_END_TURN"]
                         and awakening["TRIGGER_TURN"] <= awakening["LAST_TRANSITION_TURN"] <= turn)
            if awakening.get("PAYLOAD_READY") != 1 or not valid:
                gaps.append("AWAKENING_PAYLOAD_INCOMPLETE")
    return {"evidence_ready": True, "playable_verified": False, "owner": int(owner),
            "metric_data": data, "awakening_stage": stage, "gaps": gaps}


def audit_logs(directory):
    evidence = []
    patterns = {
        "overview_opened": r"\[EC:UI\] overview_opened",
        "overview_closed": r"\[EC:UI\] overview_closed",
        "metric_snapshot": r"\[EC\] metric_snapshot player=",
        "metric_unavailable": r"\[EC:UI\] metric_(?:snapshot|bridge)_unavailable",
        "shadow_created": r"\[EC\] shadow_vassal_created",
        "awakening_legacy_conflict": r"\[EC\] awakening_legacy_owner_conflict",
        "reserve_icon_missing": r"IconManager[^\r\n]*ICON_BELIEF_GMS_",
        "unload_resize_failed": r"overview_launch_resize_failed",
        "pantheon_diagnostic_unknown": r"pantheon_(?:total|used|available)=UNKNOWN",
    }
    for name in ("Lua.log", "UserInterface.log", "Modding.log", "Database.log", "GameCore.log", "Blocker.log"):
        path = directory / name
        if not path.is_file():
            continue
        raw = path.read_bytes()
        text = raw.decode("utf-8-sig", errors="replace")
        evidence.append({"name": name, "path": str(path.resolve()), "sha256": hashlib.sha256(raw).hexdigest(),
                         "modified_utc": datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(),
                         "markers": {key: len(re.findall(pattern, text)) for key, pattern in patterns.items()}})
    return evidence


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--snapshot-file", type=Path)
    source.add_argument("--logs-only", action="store_true")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--logs-dir", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    state, error = None, None
    try:
        if args.snapshot_file:
            state = json.loads(args.snapshot_file.read_text(encoding="utf-8-sig"))
        elif not args.logs_only:
            with urlopen(args.base_url.rstrip("/") + "/api/test/campaign-state", timeout=20) as response:
                state = json.load(response)
    except (OSError, URLError, ValueError) as exc:
        error = type(exc).__name__ + ": " + str(exc)
    report = {"collected_utc": datetime.now(timezone.utc).isoformat(),
              "source": "logs_only" if args.logs_only else "saved_snapshot" if args.snapshot_file else "resident_http",
              "state": state, "assessment": assess_campaign(state), "error": error,
              "logs": audit_logs(args.logs_dir) if args.logs_dir else [],
              "screen_operations": 0, "launch_load_advance_calls": 0}
    # Preserve earlier reports rather than overwriting evidence.
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
    print(args.output.resolve())
    return 1 if error else 0


if __name__ == "__main__":
    raise SystemExit(main())
