"""Offline evidence boundaries, including the real generated Lua probe."""

import asyncio

import pytest
from starlette.requests import Request

from civ_mcp.campaign_probe import CAMPAIGN_STATE_LUA, CONFIG_KEYS, GROUP_FIELDS, parse_campaign_state
from civ_mcp.web_api import create_app


def evidence():
    lines = ["GC_FIELD|game|-|Turn|number|10", "GC_FIELD|game|-|Multiplayer|boolean|false"]
    lines += [f"GC_FIELD|config|-|{key}|nil|" for key in CONFIG_KEYS]
    for group, fields in GROUP_FIELDS.items():
        for field in fields:
            kind, value = ("boolean", "true") if group == "identity" and field != "Era" else ("nil", "")
            lines.append(f"GC_FIELD|{group}|2|{field}|{kind}|{value}")
    lines.append(f"GC_END|1|{len(lines)}|true|true|0")
    return lines


def test_complete_evidence_does_not_claim_native_adapter_or_gameplay():
    result = parse_campaign_state("\n".join(evidence()))
    assert result["context_ready"] is True
    assert result["evidence_complete"] is True
    assert result["players"]["2"]["identity"]["Human"] is True
    assert result["adapter_ready"] is False
    assert result["native_gameplay_verified"] is False


@pytest.mark.parametrize("change", ["truncated", "count", "duplicate", "bad_slot", "alias_slot", "late", "nan", "unreadable"])
def test_incomplete_or_unreadable_evidence_is_never_complete(change):
    lines = evidence()
    if change == "truncated":
        lines.pop()
    elif change == "count":
        lines[-1] = "GC_END|1|0|true|true|0"
    elif change == "duplicate":
        lines.insert(1, lines[0])
    elif change == "bad_slot":
        lines[2 + len(CONFIG_KEYS)] = "GC_FIELD|identity|64|Alive|boolean|true"
    elif change == "alias_slot":
        lines[2 + len(CONFIG_KEYS)] = "GC_FIELD|identity|02|Alive|boolean|true"
    elif change == "late":
        lines.append(lines[0])
    elif change == "nan":
        lines[0] = "GC_FIELD|game|-|Turn|number|nan"
    else:
        lines[0] = "GC_FIELD|game|-|Turn|unreadable|"
    assert parse_campaign_state(lines)["evidence_complete"] is False


def test_escaped_token_cannot_inject_another_field():
    lines = evidence()
    index = next(i for i, line in enumerate(lines) if "|consent|2|TOKEN|" in line)
    lines[index] = "GC_FIELD|consent|2|TOKEN|string|a%7Cb%0Atext%25"
    result = parse_campaign_state(lines)
    assert result["players"]["2"]["consent"]["TOKEN"] == "a|b\ntext%"
    assert result["evidence_complete"] is True


def test_campaign_route_uses_existing_gamecore_connection_and_rejects_remote():
    calls = []
    class FakeGameState:
        async def execute_lua(self, code, context="gamecore"):
            calls.append((code, context))
            return "\n".join(evidence())
    app = create_app(FakeGameState())
    route = next(route for route in app.routes if route.path == "/api/test/campaign-state")
    def request(host):
        return Request({"type": "http", "method": "GET", "path": route.path,
                        "client": (host, 1234), "headers": [], "app": app})
    result = asyncio.run(route.endpoint(request("127.0.0.1")))
    assert result["evidence_complete"] is True
    assert calls == [(CAMPAIGN_STATE_LUA, "gamecore")]
    with pytest.raises(Exception) as error:
        asyncio.run(route.endpoint(request("192.0.2.1")))
    assert error.value.status_code == 403
    assert len(calls) == 1
