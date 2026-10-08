# Native acceptance workflows

Run `civ6-native-service` alone. Ordinary MCP commands are blocked from taking its reserved FireTuner session. Existing HTTP reads reuse its connection; never start a second direct probe while it runs.

| Endpoint | Method | Purpose |
| --- | --- | --- |
| /api/overview | GET | Current game overview |
| /api/test/lua-states | GET | Cached native context inventory |
| /api/test/refresh-states | POST | Refresh contexts after a world/menu transition |
| /api/test/campaign-state | GET | GrandCampaign configuration and persistent state |
| /api/test/ui-probe | GET | Read-only context/control evidence; does not prove visible behavior |
| /api/test/native-lua | POST | Execute trusted Lua once in one exact context |
| /api/test/load-save | POST | Native front-end loading of a named GC_ single-player save |
| /api/test/advance | POST | Bounded advancement of a matching independent single-player world |

Example advance body:

```json
{"turns": 1, "expected_run": "GC_MY_DISPOSABLE_TEST", "skip_information_popups": false}
```

Setting `skip_information_popups` to true authorizes original ESC callbacks for TechCivicCompletedPopup, NaturalDisasterPopup, ProjectBuiltPopup, WonderBuiltPopup, and NaturalWonderPopup in that independent single-player run. It does not accept diplomatic deals or gameplay confirmations. A movie may queue a successor; one ESC receipt is not proof all notifications closed.

Example developer Lua read:

```json
{"state":"GameCore_Tuner", "code":"print(Game.GetCurrentGameTurn())", "read_only":true}
```

`read_only` is an intent label, not a security sandbox. Never submit code from an untrusted document. Mutating requests require `read_only:false` and a matching `expected_run`. Select a unique exact context name; ambiguous contexts are rejected. The API records code SHA-256 and a completion marker. An HTTP success alone does not establish that a requested effect occurred; inspect `lua_ok`, receipts, and actual game state.

From 0.1.1, the native Lua endpoint refreshes the context list on the same exclusive FireTuner socket before resolving the requested name. Moving from a game to a menu can reuse old numeric indexes even while the socket remains connected. A disappeared or ambiguous context returns HTTP 409 before any probe Lua is sent. Run/single-player guards remain necessary because discovery cannot freeze a human's subsequent world transition. This refresh does not replay uncertain writes or open a second connection.

Load body: `{"save_name":"GC_MY_DISPOSABLE_TEST_SAVE"}`. First leave the independent game through an authorized game lifecycle operation. The loader must find the native front end and verify mod requirements; it has no menu-click or OCR fallback. After load, verify the actual run and turn.

An uncertain turn remains pending. Inspect its native core/UI state and blocking notifications; do not issue another turn request to compensate. Across a service restart preserve the pending marker with `--pending-run GC_MY_DISPOSABLE_TEST --pending-turn N`. This marker does not replay the engine command.

The service is for a trusted local development environment. It is unauthenticated, executes privileged Lua, and must not be exposed through a public tunnel or reverse proxy. The normal upstream MCP server's embedded dashboard binds to all interfaces; this isolated service binds to loopback.
