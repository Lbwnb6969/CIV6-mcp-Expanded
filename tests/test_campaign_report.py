import importlib.util
import json
from pathlib import Path

import pytest


spec = importlib.util.spec_from_file_location(
    "campaign_report", Path(__file__).resolve().parents[1] / "scripts" / "codex_campaign_report.py")
report = importlib.util.module_from_spec(spec)
spec.loader.exec_module(report)


def snapshot():
    return {"evidence_complete": True, "read_only": True, "mutation_calls": 0, "context_ready": True,
            "game": {"Turn": 10, "Multiplayer": False},
            "config": {"EC_METRIC_BRIDGE_TEST": 1, "EC_OBSERVATION_ONLY": 0},
            "players": {"2": {"identity": {"Human": True, "Alive": True},
                              "awakening": {}, "metrics": {}}}}


def test_enabled_bridge_with_no_record_is_waiting_and_never_playable():
    result = report.assess_campaign(snapshot())
    assert result["metric_data"] == "WAITING"
    assert result["owner"] == 2
    assert "METRICS_WAITING" in result["gaps"]
    assert "REVIEWED_NATIVE_ADAPTER_REQUIRED" in result["gaps"]
    assert result["playable_verified"] is False


@pytest.mark.parametrize("turn,source,expected", [(10, "UI_BRIDGE_TEST", "FRESH"),
                                                (4, "UI_BRIDGE_TEST", "STALE"),
                                                (11, "UI_BRIDGE_TEST", "UNKNOWN"),
                                                (10, "OTHER", "UNKNOWN")])
def test_freshness_and_provenance(turn, source, expected):
    state = snapshot()
    state["players"]["2"]["metrics"] = {
        "SCHEMA": 1, "ACTIVE_BANK": 1, "READY": 1, "PlayerID": 2, "AuthorID": 2,
        "Turn": turn, "Source": source, "Military": 100, "GoldBalance": 200, "Science": 10,
        "Culture": 10, "NetGold": -5, "SpeedMultiplier": 100, "Cities": 1,
        "TechsCompleted": 20, "CivicsCompleted": 15,
        "Human": True, "Alive": True, "Major": True, "Independent": True}
    assert report.assess_campaign(state)["metric_data"] == expected


def test_legacy_conflict_and_unknown_config_are_explicit():
    state = snapshot()
    state["config"]["EC_OBSERVATION_ONLY"] = None
    state["players"]["0"] = {"awakening": {"STATE": "PREPARATION"}}
    gaps = report.assess_campaign(state)["gaps"]
    assert "LEGACY_AWAKENING_OWNER_CONFLICT" in gaps
    assert "OBSERVATION_MODE_UNKNOWN" in gaps
    assert "METRICS_WAITING" in gaps


def test_uncommitted_bank_is_waiting_but_boolean_schema_is_not_version_one():
    state = snapshot()
    state["players"]["2"]["metrics"] = {"SCHEMA": 1, "ACTIVE_BANK": None}
    assert report.assess_campaign(state)["metric_data"] == "WAITING"
    state["players"]["2"]["metrics"]["SCHEMA"] = True
    assert report.assess_campaign(state)["metric_data"] == "UNKNOWN"


def test_untrusted_or_missing_evidence_cannot_pass():
    for state in (None, {}, {"evidence_complete": True}, {**snapshot(), "mutation_calls": 1}):
        assert report.assess_campaign(state)["evidence_ready"] is False


def test_logs_only_never_contacts_game_and_preserves_existing_report(tmp_path, monkeypatch):
    (tmp_path / "Lua.log").write_text("[EC:UI] overview_opened\n[EC:UI] overview_closed\n", encoding="utf-8")
    target = tmp_path / "report.json"
    monkeypatch.setattr("sys.argv", ["report", "--logs-only", "--logs-dir", str(tmp_path), "--output", str(target)])
    monkeypatch.setattr(report, "urlopen", lambda *args, **kwargs: pytest.fail("must not contact game"))
    assert report.main() == 0
    saved = json.loads(target.read_text())
    assert saved["assessment"]["playable_verified"] is False
    assert saved["screen_operations"] == 0
    assert saved["logs"][0]["markers"]["overview_opened"] == 1
    original = target.read_bytes()
    with pytest.raises(FileExistsError):
        report.main()
    assert target.read_bytes() == original
