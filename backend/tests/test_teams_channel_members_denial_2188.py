"""#2188: a denied channel roster read names the delegated permission it needs.

Synthetic tenant and IDs; Microsoft Graph is a fake answering 403 to the read.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from app.integrations.http import format_http_error
from app.integrations.providers import microsoft as ms
from app.integrations.providers.microsoft import build_tools
from app.tools.base import CallableToolExecutor, ToolCall, ToolResult

GRAPH = "https://graph.microsoft.com/v1.0"
TEAM = "5b3c2a1d-6e7f-4a8b-9c0d-1e2f3a4b5c6d"
CHANNEL = "19:0a1b2c3d4e5f60718293a4b5c6d7e8f9@thread.tacv2"
DENIED = {"error": {"code": "Forbidden", "message": "InsufficientPrivileges"}}


async def _call(name: str, **arguments: Any) -> ToolResult:
    executor = CallableToolExecutor()
    build_tools(token="dummy", executor=executor)
    return await executor.execute_tool(ToolCall(id="call-1", name=name, arguments=arguments))


@pytest.mark.asyncio
async def test_a_denied_channel_roster_read_names_channel_member_read_all(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[tuple[str, str]] = []

    async def denied(_client: Any, method: str, url: str, _provider: str, op: str, **_kwargs: Any) -> tuple[None, str]:
        sent.append((method, url))
        return None, format_http_error(httpx.Response(403, json=DENIED), "Microsoft", op)

    monkeypatch.setattr(ms._common, "safe_request", denied)

    result = await _call("list_microsoft_channel_members", team_id=TEAM, channel_id=CHANNEL)

    assert ("get", f"{GRAPH}/teams/{TEAM}/channels/{CHANNEL}/members") in sent
    assert "ChannelMember.Read.All" in result.content
    assert "re-authorized" in result.content
