# CIV6-mcp Expanded

Civilization VI MCP server with native game diagnostics and controlled mod acceptance tools. Based on [lmwilki/civ6-mcp](https://github.com/lmwilki/civ6-mcp), with MIT attribution retained.

文明 VI MCP 扩展版：通过 FireTuner 查询游戏状态、执行游戏操作，并提供独立测试局的原生验收接口。普通游戏工具保留上游行为；原生验收服务独立运行，不操作屏幕、鼠标或系统键盘。

## What is included

- Gameplay queries and commands for units, cities, research, diplomacy, religion, trade, and victory progress.
- Local HTTP queries sharing one resident FireTuner connection.
- GrandCampaign state and mod diagnostics; the GrandCampaign mods themselves are not included or required for ordinary queries.
- An isolated native acceptance service: exclusive process lock, bounded turns, native GC_ test-save loading, fresh Lua-context discovery on the sole socket, request hashes, completion receipts, and no automatic replay of uncertain writes.
- Optional native informational popup closure for tech/civic completion, disasters, projects, and world/natural wonders. This uses the game's original ESC handlers and requires explicit opt-in per request.

The legacy gameplay launcher retains upstream OCR/menu automation. Use the **native acceptance service** below when screen interaction must be excluded. Arbitrary Lua is privileged developer access: `read_only` labels caller intent and does not sandbox code. Use only trusted local clients.

## Requirements

Python 3.12 or newer; a licensed Civilization VI installation with FireTuner enabled. The native acceptance additions were exercised on Windows with the Steam version and Gathering Storm. Other platforms, game editions, and multiplayer support are not established by that evidence.

Enable the in-game Tuner option or set `EnableTuner 1` in the game's AppOptions.txt while the game is closed. Keep a copy of your original options and saves. Disable Auto End Turn. Close the SDK FireTuner GUI and other clients: the game accepts one tuner connection. The default tuner port is 4318.

## Install

```powershell
git clone https://github.com/Lbwnb6969/CIV6-mcp-Expanded.git
cd CIV6-mcp-Expanded
py -3.12 -m venv .venv
.venv\Scripts\python.exe -m pip install -e . pytest
```

On Linux/macOS, use `python3` and `.venv/bin/python`. Native Windows acceptance is the tested configuration. Optional legacy OCR extras are described in the upstream README.

## MCP client

Use this generic MCP configuration with the absolute Python executable from your virtual environment:

```json
{
  "mcpServers": {
    "civ6": {
      "command": "PATH_TO_PROJECT/.venv/Scripts/python.exe",
      "args": ["-m", "civ_mcp"]
    }
  }
}
```

Load a game manually, then query `get_game_overview` to orient the agent. The normal MCP lifecycle can launch the game and run background helpers. Do not run it simultaneously with the isolated service.

## Native acceptance service

Start this **instead of** the normal MCP server. It binds only to `127.0.0.1:8000` and does not launch the game, move the camera, watch popups in the background, or use screen fallback.

```powershell
.venv\Scripts\civ6-native-service.exe --tuner-port 4318
```

Read available contexts:

```powershell
Invoke-RestMethod http://127.0.0.1:8000/api/test/lua-states
Invoke-RestMethod http://127.0.0.1:8000/api/overview
```

Use disposable single-player tests. A GC_ save name alone is insufficient for mutation: the loaded world's `GC_NATIVE_RUN` configuration must match `expected_run`, and native mode checks must pass. When a request loses its receipt, inspect state before continuing; never automatically resend it. Save or protect current progress before leaving a game. See [native workflows](docs/NATIVE_WORKFLOWS.md) and [validation limits](docs/VALIDATION.md).

## Development

```powershell
.venv\Scripts\python.exe -m pytest tests -q
.venv\Scripts\python.exe -m pip wheel --no-deps . -w dist
```

These tests use fixtures and temporary files; they do not establish native gameplay success. Live evidence must also verify the loaded world, actual state changes, save completion/reload, and absence of duplicate effects.

## License

MIT. See [LICENSE](LICENSE) and [NOTICE](NOTICE.md). No game files, saves, private logs, local client settings, or evaluation datasets are distributed.
