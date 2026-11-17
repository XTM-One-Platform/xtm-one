"""The Defender TI host tools send the host as one percent-encoded path segment.

An IPv6 address (colons) or a value with ``/``, ``?`` or ``#`` put raw in
``/security/threatIntelligence/hosts/{host}`` changes the request path; the
host tools encode it like the detection rule and machine action tools do.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.integrations.providers.defender import build_tools
from app.tools.base import CallableToolExecutor, ToolCall

_HOSTS = "https://graph.microsoft.com/v1.0/security/threatIntelligence/hosts/"


class _StubResponse:
    def __init__(self, payload: dict[str, Any]):
        self._payload = payload

    def json(self) -> dict[str, Any]:
        return self._payload


class _RequestRecorder:
    def __init__(self):
        self.urls: list[str] = []

    async def __call__(self, _client, method, url, provider, op, **kwargs):
        self.urls.append(url)
        return _StubResponse({"value": []}), None


async def _async_token() -> str:
    return "dummy-token"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool", "suffix"),
    [
        ("get_defender_ti_host", ""),
        ("get_defender_ti_host_reputation", "/reputation"),
        ("get_defender_ti_passive_dns", "/passiveDns"),
    ],
)
@pytest.mark.parametrize(
    ("host", "encoded"),
    [
        ("2001:db8::1", "2001%3Adb8%3A%3A1"),
        ("example.com/x?y#z", "example.com%2Fx%3Fy%23z"),
        ("example.com", "example.com"),
    ],
)
async def test_host_is_one_encoded_path_segment(monkeypatch, tool, suffix, host, encoded):
    recorder = _RequestRecorder()
    from app.integrations.providers import defender as df

    monkeypatch.setattr(df, "safe_request", recorder)
    monkeypatch.setattr(df, "_get_token", lambda *a, **k: _async_token())

    executor = CallableToolExecutor()
    build_tools(token="tenant||client||secret", executor=executor)

    result = await executor.execute_tool(ToolCall(id="call-1", name=tool, arguments={"host": host}))

    assert not result.is_error
    assert recorder.urls == [f"{_HOSTS}{encoded}{suffix}"]
