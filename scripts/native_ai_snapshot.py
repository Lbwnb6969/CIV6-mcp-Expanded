"""Capture the read-only native AI summary exposed by the running MCP host."""

from __future__ import annotations

import json
import sys
from urllib.request import Request, urlopen


API_ROOT = "http://127.0.0.1:8000"


def _get(path: str) -> object:
    with urlopen(Request(f"{API_ROOT}{path}", method="GET"), timeout=20) as response:
        return json.load(response)


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    overview = _get("/api/overview")
    victory = _get("/api/victory?include_unmet=true")
    players = victory.get("players", []) if isinstance(victory, dict) else []
    summary = []
    for player in players:
        if not isinstance(player, dict):
            continue
        summary.append(
            {
                "player_id": player.get("player_id"),
                "name": player.get("name"),
                "score": player.get("score"),
                "military_strength": player.get("military_strength"),
                "num_cities": player.get("num_cities"),
                "techs_researched": player.get("techs_researched"),
                "civics_completed": player.get("civics_completed"),
                "science_yield": player.get("science_yield"),
                "gold_yield": player.get("gold_yield"),
            }
        )
    print(
        json.dumps(
            {
                "ok": True,
                "turn": overview.get("turn") if isinstance(overview, dict) else None,
                "players": summary,
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
