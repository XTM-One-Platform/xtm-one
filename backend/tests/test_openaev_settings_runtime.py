"""``manage_openaev_settings`` actions ``get_runtime`` and ``update_runtime``.

OpenAEV lists the settings an administrator may change at runtime (the inject
execution threshold, agent jobs and queue, agent auto-update) under
``runtime_settings`` of ``GET /api/settings`` and stores them with
``PUT /api/settings/runtime`` (OpenAEV #9416). ``get`` answered the whole
settings object cut at 12,000 characters, so the threshold was not readable,
and nothing could set it. ``get_runtime`` reads the list alone, whole;
``update_runtime`` writes it; an OpenAEV before that release is named as such.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from app.integrations.providers.openaev import _common, build_tools
from app.integrations.providers.openaev.platform import _runtime_settings_answer
from app.tools.base import CallableToolExecutor, ToolCall

BASE = "https://demo.openaev.io"

_THRESHOLD = {
    "key": "inject_execution_threshold_minutes",
    "value": "10",
    "source": "default",
    "env_var": "OPENAEV_INJECT_EXECUTION_THRESHOLD",
    "locked": False,
    "type": "integer",
    "min": 1,
    "max": 1440,
    "enterprise_only": False,
}
_AUTO_UPDATE = {
    "key": "agent_auto_update_enabled",
    "value": "true",
    "source": "env",
    "env_var": "OPENAEV_AGENT_AUTO_UPDATE",
    "locked": True,
    "type": "boolean",
    "min": None,
    "max": None,
    "enterprise_only": True,
}


class _Resp:
    status_code = 200
    headers: dict[str, str] = {}

    def __init__(self, body: Any) -> None:
        self.text = json.dumps(body)
        self.content = self.text.encode()
        self._body = body

    def json(self) -> Any:
        return self._body


class _Transport:
    def __init__(self, settings: dict[str, Any]) -> None:
        self.settings = settings
        self.calls: list[tuple[str, str, Any]] = []

    async def __call__(
        self, client: Any, method: str, url: str, provider: str, action: str, **kwargs: Any
    ) -> tuple[_Resp, None]:
        self.calls.append((method.lower(), url, kwargs.get("json")))
        if method.lower() == "put" and url == f"{BASE}/api/settings/runtime":
            updated = dict(_THRESHOLD, value="60", source="ui")
            return _Resp({"runtime_settings": [updated, _AUTO_UPDATE]}), None
        return _Resp(self.settings), None


async def _call(
    monkeypatch: pytest.MonkeyPatch, settings: dict[str, Any], arguments: dict[str, Any]
) -> tuple[str, _Transport]:
    transport = _Transport(settings)
    monkeypatch.setattr(_common, "safe_request", transport)
    executor = CallableToolExecutor()
    build_tools(f"{BASE}||test-token", executor)
    result = await executor.execute_tool(ToolCall(id="call-r", name="manage_openaev_settings", arguments=arguments))
    return str(result.content), transport


async def test_get_runtime_reads_the_list_whole_past_a_long_settings_object(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = {"platform_theme": {"x": "y" * 20000}, "runtime_settings": [_THRESHOLD, _AUTO_UPDATE]}
    content, transport = await _call(monkeypatch, settings, {"action": "get_runtime"})
    assert transport.calls[0][:2] == ("get", f"{BASE}/api/settings")
    assert "inject_execution_threshold_minutes = 10 (source default" in content
    assert "range 1..1440" in content
    assert "env var OPENAEV_INJECT_EXECUTION_THRESHOLD unset: settable" in content
    assert "LOCKED by env var OPENAEV_AGENT_AUTO_UPDATE" in content
    assert "Enterprise Edition only" in content
    assert "truncated" not in content


async def test_get_runtime_names_an_openaev_without_runtime_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    content, _ = await _call(monkeypatch, {"platform_name": "x"}, {"action": "get_runtime"})
    assert "does not expose runtime settings" in content


async def test_update_runtime_puts_the_settings_and_reads_them_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    content, transport = await _call(
        monkeypatch,
        {},
        {
            "action": "update_runtime",
            "config": json.dumps({"inject_execution_threshold_minutes": 60}),
        },
    )
    assert transport.calls == [
        (
            "put",
            f"{BASE}/api/settings/runtime",
            {"settings": {"inject_execution_threshold_minutes": 60}},
        )
    ]
    assert content.startswith("Updated runtime settings.")
    assert "inject_execution_threshold_minutes = 60 (source ui" in content


async def test_update_runtime_accepts_the_api_body_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    _, transport = await _call(
        monkeypatch,
        {},
        {
            "action": "update_runtime",
            "config": json.dumps({"settings": {"agent_queue_threshold": 5}}),
        },
    )
    assert transport.calls[0][2] == {"settings": {"agent_queue_threshold": 5}}


async def test_update_runtime_without_config_sends_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    content, transport = await _call(monkeypatch, {}, {"action": "update_runtime"})
    assert transport.calls == []
    assert "config is required for 'update_runtime'" in content


def test_runtime_settings_answer_empty_list() -> None:
    assert _runtime_settings_answer({"runtime_settings": []}) == "OpenAEV lists no runtime setting."
