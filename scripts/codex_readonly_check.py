"""Run the smallest useful read-only check against a live Steam Civ6 game.

This is the repeatable validation entry point used by the GrandCampaign
project.  It opens one FireTuner connection, reads overview/cities/diplomacy,
prints a compact result, and always closes the connection so a later MCP
server is not blocked by a stale client.
"""

from __future__ import annotations

import asyncio
import json
import sys
from urllib.error import URLError
from urllib.request import Request, urlopen

from civ_mcp.connection import GameConnection
from civ_mcp.game_state import GameState


_EMBEDDED_API = "http://127.0.0.1:8000"


def _get_json(path: str, timeout: float = 5.0) -> object:
    """Read one endpoint from the MCP server's shared read-only API."""
    request = Request(f"{_EMBEDDED_API}{path}", method="GET")
    with urlopen(request, timeout=timeout) as response:
        return json.load(response)


def _run_embedded_api() -> dict[str, object] | None:
    """Use the already-connected MCP server when it is running.

    FireTuner accepts one client at a time.  The embedded API shares that
    client's GameConnection, so this path is safe to run while Codex's MCP
    server is active.  A connection/refused response means the standalone
    fallback may still be useful.
    """
    try:
        overview = _get_json("/api/overview")
        cities = _get_json("/api/cities")
        diplomacy = _get_json("/api/diplomacy")
    except (OSError, URLError, ValueError):
        return None
    if not isinstance(overview, dict):
        return None
    city_rows = cities[0] if isinstance(cities, list) and cities else []
    diplomacy_rows = diplomacy if isinstance(diplomacy, list) else []
    return {
        "source": "embedded-api",
        "gamecore_index": None,
        "ingame_index": None,
        "turn": overview.get("turn"),
        "civ": overview.get("civ_name"),
        "leader": overview.get("leader_name"),
        "cities": len(city_rows) if isinstance(city_rows, list) else 0,
        "city_errors": cities[1] if isinstance(cities, list) and len(cities) > 1 else [],
        "diplomacy_entries": len(diplomacy_rows),
        "difficulty": overview.get("difficulty"),
        "game_speed": overview.get("game_speed"),
    }


async def _run() -> dict[str, object]:
    conn = GameConnection()
    await conn.connect()
    try:
        state = GameState(conn)
        overview = await state.get_game_overview()
        cities, city_errors = await state.get_cities()
        diplomacy = await state.get_diplomacy()
        return {
            "source": "firetuner-direct",
            "gamecore_index": conn.gamecore_index,
            "ingame_index": conn.ingame_index,
            "turn": overview.turn,
            "civ": overview.civ_name,
            "leader": overview.leader_name,
            "cities": len(cities),
            "city_errors": city_errors,
            "diplomacy_entries": len(diplomacy),
            "difficulty": overview.difficulty,
            "game_speed": overview.game_speed,
        }
    finally:
        await conn.disconnect()


def main() -> int:
    # Keep JSON readable under the Chinese Windows console used by Steam/Civ6.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    embedded = _run_embedded_api()
    if embedded is not None:
        print(json.dumps({"ok": True, **embedded}, ensure_ascii=False))
        return 0
    try:
        result = asyncio.run(_run())
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps({"ok": True, **result}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
