from scripts.codex_ui_acceptance import SURFACES, evaluate_ui_probe


def _report(*, safe=True, observed=True, evidence="CONTEXT_CONTROLS"):
    status = {
        surface: {
            "candidate_count": 1 if observed else 0,
            "highest_evidence": evidence if observed else "NO_CANDIDATE",
            "observed": observed,
        }
        for surface in SURFACES
    }
    return {
        "read_only": safe,
        "mutation_calls": 0 if safe else 1,
        "summary": {"surface_status": status},
    }


def test_ui_acceptance_returns_review_ready_for_safe_probe():
    result = evaluate_ui_probe(_report())
    assert result["safe"] is True
    assert result["review_ready"] is True
    assert result["reason"] == "OK"
    assert result["gaps"] == []


def test_ui_acceptance_keeps_missing_surfaces_as_gaps():
    result = evaluate_ui_probe(_report(observed=False))
    assert result["safe"] is True
    assert result["review_ready"] is True
    assert result["gaps"] == list(SURFACES)
    assert result["surface_status"]["overview_panel"]["highest_evidence"] == "NO_CANDIDATE"


def test_ui_acceptance_rejects_visible_boundary_failure():
    result = evaluate_ui_probe(_report(safe=False))
    assert result["safe"] is False
    assert result["review_ready"] is False
    assert result["reason"] == "READ_ONLY_BOUNDARY_FAILED"


def test_ui_acceptance_rejects_malformed_report():
    result = evaluate_ui_probe(None)
    assert result["safe"] is False
    assert result["reason"] == "INVALID_RESPONSE"
    assert result["gaps"] == list(SURFACES)
