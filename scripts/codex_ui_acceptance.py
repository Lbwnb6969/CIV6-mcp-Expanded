"""Run one conservative HTTP-only UI acceptance check against the MCP server.

The script consumes ``/api/test/ui-probe`` from the resident MCP connection.
It never starts Civ VI, opens a popup, clicks a control, publishes a LuaEvent,
or writes a property.  A positive result means the probe response is safe and
usable for review; surface gaps remain runtime evidence to investigate.
"""

from __future__ import annotations

import argparse
import json
import sys
from urllib.error import URLError
from urllib.request import Request, urlopen


SURFACES = (
    "overview_launch",
    "overview_panel",
    "consent_popup",
    "metric_bridge",
    "pantheon_replacement",
    "natural_wonder_replacement",
)
EVIDENCE_RANK = {
    "NO_CANDIDATE": 0,
    "ERROR": 1,
    "INCOMPLETE": 2,
    "CONTEXT_PARTIAL": 3,
    "CONTEXT_CONTROLS": 4,
    "VISIBLE_CONTROL": 5,
}


def evaluate_ui_probe(report: object) -> dict[str, object]:
    """Convert a raw UI probe response into a review-oriented result."""
    if not isinstance(report, dict):
        return {
            "safe": False,
            "review_ready": False,
            "reason": "INVALID_RESPONSE",
            "surface_status": {},
            "gaps": list(SURFACES),
        }
    summary = report.get("summary")
    statuses = summary.get("surface_status") if isinstance(summary, dict) else None
    if not isinstance(statuses, dict):
        statuses = {}
    safe = report.get("read_only") is True and report.get("mutation_calls") == 0
    surface_status: dict[str, dict[str, object]] = {}
    gaps: list[str] = []
    for surface in SURFACES:
        value = statuses.get(surface)
        if not isinstance(value, dict):
            value = {
                "candidate_count": 0,
                "highest_evidence": "NO_CANDIDATE",
                "observed": False,
            }
        evidence = value.get("highest_evidence", "NO_CANDIDATE")
        if evidence not in EVIDENCE_RANK:
            evidence = "ERROR"
        candidate_count = value.get("candidate_count", 0)
        observed = value.get("observed") is True and candidate_count > 0
        surface_status[surface] = {
            "candidate_count": candidate_count,
            "highest_evidence": evidence,
            "observed": observed,
        }
        if not observed or EVIDENCE_RANK[evidence] < EVIDENCE_RANK["CONTEXT_CONTROLS"]:
            gaps.append(surface)
    return {
        "safe": safe,
        "review_ready": safe and isinstance(summary, dict),
        "reason": "OK" if safe else "READ_ONLY_BOUNDARY_FAILED",
        "surface_status": surface_status,
        "gaps": gaps,
    }


def fetch_ui_probe(base_url: str, timeout: float = 5.0) -> object:
    url = base_url.rstrip("/") + "/api/test/ui-probe"
    request = Request(url, method="GET")
    with urlopen(request, timeout=timeout) as response:
        return json.load(response)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--timeout", type=float, default=5.0)
    parser.add_argument("--output", help="write JSON to this path instead of stdout")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    try:
        report = fetch_ui_probe(args.base_url, args.timeout)
        result = evaluate_ui_probe(report)
    except (OSError, URLError, ValueError) as exc:
        result = {
            "safe": False,
            "review_ready": False,
            "reason": type(exc).__name__,
            "error": str(exc),
            "surface_status": {},
            "gaps": list(SURFACES),
        }
    payload = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as handle:
            handle.write(payload + "\n")
    else:
        print(payload)
    return 0 if result["safe"] else 1


if __name__ == "__main__":
    sys.exit(main())
