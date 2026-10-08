"""Capture a read-only Civ 6 runtime snapshot with active-mod evidence.

The FireTuner connection is opened once and closed before returning.  The
probe deliberately records both the loaded game state and the front-end
``Modding.GetActiveMods`` result.  The active-mod list is a diagnostic hint;
local profile activation and save metadata are not guaranteed to be exposed
by that API.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import sys
from datetime import datetime, timezone

from civ_mcp.connection import GameConnection
from civ_mcp.game_state import GameState
from civ_mcp.web_api import (
    _ACTIVE_MOD_PROBE_LUA,
    _AI_MOD_STATUS_LUA,
    _ENDGAME_CAPABILITY_PROBE_LUA,
    _UI_CONTEXT_PROBE_LUA,
    _parse_active_mod_probe,
    _parse_ai_mod_status,
    _parse_endgame_capabilities,
    _parse_ui_probe,
    _summarize_ui_probes,
)


DEFAULT_EXPECTED_MODS = {
    "Endgame": "2af0c438-bab0-4d0c-987f-0beaf0f7c7f2",
    "Stability": "7b9b91d9-6ce7-4fc7-93c2-1c170ccbf401",
    "TestFlow": "b7f8e82d-2ef8-4db3-a1a7-91ac8c5d7f13",
}


async def _capture(port: int, expected_mods: dict[str, str]) -> dict[str, object]:
    conn = GameConnection(port=port)
    await conn.connect()
    try:
        state = GameState(conn)
        overview = await state.get_game_overview()
        ai_lines = await conn.execute_read(_AI_MOD_STATUS_LUA)
        capability_lines = await conn.execute_write(_ENDGAME_CAPABILITY_PROBE_LUA)
        ui_probe_states = [
            (index, name)
            for index, name in sorted(conn.lua_states.items())
            if any(
                needle.lower() in name.lower()
                for needle in (
                    "InGame", "Overview", "LaunchBar", "Pantheon", "NaturalWonder",
                    "EC_", "GMS_", "Popup", "Chooser",
                )
            )
        ]
        ui_probes = []
        for index, name in ui_probe_states[:64]:
            try:
                probe_lines = await conn.execute_in_state(
                    index,
                    _UI_CONTEXT_PROBE_LUA + 'print("---END---")',
                )
                probe = _parse_ui_probe("\n".join(probe_lines))
                probe.update({"index": index, "name": name})
                ui_probes.append(probe)
            except Exception as exc:
                ui_probes.append({"index": index, "name": name, "error": type(exc).__name__})
        main_state = next(
            (index for index, name in conn.lua_states.items() if name == "Main State"),
            None,
        )
        active_lines: list[str] = []
        if main_state is not None:
            active_lines = await conn.execute_in_state(
                main_state,
                _ACTIVE_MOD_PROBE_LUA + 'print("---END---")',
            )
        active = _parse_active_mod_probe("\n".join(active_lines))
        diary = await state.get_diary_snapshot()
        return {
            "timestamp_utc": datetime.now(timezone.utc).isoformat(),
            "firetuner_states": {
                "gamecore": conn.gamecore_index,
                "ingame": conn.ingame_index,
                "main_state": main_state,
            },
            "overview": dataclasses.asdict(overview),
            "ai_mod_status": _parse_ai_mod_status("\n".join(ai_lines)),
            "endgame_capabilities": _parse_endgame_capabilities(
                "\n".join(capability_lines)
            ),
            "ui_probe": {
                "candidate_states": len(ui_probe_states),
                "probes": ui_probes,
                "summary": _summarize_ui_probes(ui_probes),
                "read_only": True,
                "mutation_calls": 0,
            },
            "active_mod_probe": active,
            "expected_mods": expected_mods,
            "expected_mods_active": {
                name: mod_id in active["mods"]
                for name, mod_id in expected_mods.items()
            },
            "diary": dataclasses.asdict(diary),
        }
    finally:
        await conn.disconnect()


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=4318)
    parser.add_argument(
        "--output",
        type=str,
        help="write JSON to this path instead of stdout",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    try:
        result = asyncio.run(_capture(args.port, DEFAULT_EXPECTED_MODS))
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 1
    payload = json.dumps(result, ensure_ascii=False, default=str, indent=2)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as handle:
            handle.write(payload + "\n")
    else:
        print(payload)
    return 0


if __name__ == "__main__":
    sys.exit(main())
