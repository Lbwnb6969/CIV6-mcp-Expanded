# CIV6-mcp Expanded

## Goal

Publish a clean independent MIT source project to Lbwnb6969/CIV6-mcp-Expanded, preserving upstream attribution and useful native acceptance improvements.

## Requirements and decisions

Do not redistribute game files, personal saves/logs, local client settings, raw native evidence, or private Git history. Use a separate public checkout and independent Git history; retain the original MIT license. Mirror scoped development fixes without restarting a user's running game or resident process as part of publication. Normal upstream MCP and isolated native acceptance are distinct modes. No claim of universal native completion.

## Progress

Allowlisted source export created from 9e3c31f; README, notice, source manifest, native workflow documentation and installed service entry point prepared. GitHub account Lbwnb6969 was verified through existing Git credentials without displaying secrets.

Fresh Windows Python 3.14 environment: 206 offline tests passed. Initial unconstrained install exposed the MCP 2.x FastMCP removal; dependency bounds now require MCP 1.x, FastAPI 0.x, and Anthropic 0.x. Verified installed versions: mcp 1.30.0, fastapi 0.143.0, anthropic 0.125.0. Wheel/sdist build and pip check passed. Installing the actual wheel and importing from outside the checkout resolved site-packages, version 0.1.0 and 77 registered tools; native service help worked without opening another game connection. Source/archive audit found no configured token/private-key categories, private local paths, game saves, logs or virtual environment files. GitHub Actions is configured for Python 3.12 on Windows/Linux but has not yet run.

## Next steps and unresolved issues

0.1.1 prepared: the native Lua endpoint refreshes context names on the same socket before dispatch, preventing cached game indexes from selecting menus. Existing run/single-player guards remain. Three new regression cases and 209 offline tests passed. Default pytest collection now excludes legacy live scripts; an accidental root collection was stopped by the existing connection reservation before any connection. Wheel/sdist and configured audit passed; the actual installed wheel imports version 0.1.1, new discovery and 77 tools outside the checkout; CLI help and pip check passed. Updated discovery is not yet live-validated; the active resident process remains on its previously loaded source. Publication/CI follow, preserving 0.1.0 and its recovery bundle.

Published public repository and v0.1.0 Beta release, including wheel, sdist and SHA256SUMS. Release source commit: 449fb19bbe4ae12611a2681669f4a51a632cb524. GitHub Actions run 37780832334 completed successfully on Python 3.12 Windows/Linux. The release is tagged and the local complete Git bundle was verified. Publication is complete; future source changes require new scoped verification and a new version.

Live mod feature completion continues in the four GrandCampaign projects. Platform/general gameplay coverage and natural-wonder movie acceptance remain limited as documented. Source/content scanning is a bounded check, not proof against every possible secret format.
