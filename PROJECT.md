# CIV6-mcp Expanded

## Goal

Publish a clean independent MIT source project to Lbwnb6969/CIV6-mcp-Expanded, preserving upstream attribution and useful native acceptance improvements.

## Requirements and decisions

Do not redistribute game files, personal saves/logs, local client settings, raw native evidence, or private Git history. Keep the active resident service and source checkout unchanged. Use a new source export and independent Git history; retain the original MIT license. Normal upstream MCP and isolated native acceptance are distinct modes. No claim of universal native completion.

## Progress

Allowlisted source export created from 9e3c31f; README, notice, source manifest, native workflow documentation and installed service entry point prepared. GitHub account Lbwnb6969 was verified through existing Git credentials without displaying secrets.

Fresh Windows Python 3.14 environment: 206 offline tests passed. Initial unconstrained install exposed the MCP 2.x FastMCP removal; dependency bounds now require MCP 1.x, FastAPI 0.x, and Anthropic 0.x. Verified installed versions: mcp 1.30.0, fastapi 0.143.0, anthropic 0.125.0. Wheel/sdist build and pip check passed. Installing the actual wheel and importing from outside the checkout resolved site-packages, version 0.1.0 and 77 registered tools; native service help worked without opening another game connection. Source/archive audit found no configured token/private-key categories, private local paths, game saves, logs or virtual environment files. GitHub Actions is configured for Python 3.12 on Windows/Linux but has not yet run.

## Next steps and unresolved issues

Create/push the public repository and preserve a recoverable local bundle/archive. Live mod feature completion continues in the four GrandCampaign projects. Platform/general gameplay coverage and natural-wonder movie acceptance remain limited as documented. Source/content scanning is a bounded check, not proof against every possible secret format.
