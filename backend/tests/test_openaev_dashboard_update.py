"""manage_openaev_dashboard update merges over the stored dashboard.

OpenAEV's ``PUT /api/custom-dashboards/{id}`` replaces the entity whole: a
description it does not carry is cleared and a missing name is refused, and
any field outside ``CustomDashboardInput`` (widgets) is dropped without an
error. The tool reads the dashboard first, carries over the fields the call
does not name, and refuses the fields the endpoint would drop instead of
reporting a success that did not happen.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from app.integrations.providers.openaev import _common, build_tools
from app.tools.base import CallableToolExecutor, ToolCall, ToolResult

BASE = "https://oaev.example"
DASHBOARD = "d19b316e-46a3-4319-87d4-886313d30cff"

Call = tuple[str, str, Any]


class _Resp:
    status_code = 200
    content = b"{}"
    text = "{}"
    headers: dict[str, str] = {}

    def __init__(self, data: Any) -> None:
        self._data = data

    def json(self) -> Any:
        return self._data


class _Transport:
    """Stands in for ``safe_request``: one stored dashboard, every call recorded."""

    def __init__(self, stored: dict[str, Any], *, echo: dict[str, Any] | None = None) -> None:
        self.stored = stored
        self.echo = echo
        self.calls: list[Call] = []

    async def __call__(
        self, client: Any, method: str, url: str, provider: str, action: str, **kwargs: Any
    ) -> tuple[_Resp, None]:
        if url == f"{BASE}/api/settings":
            return _Resp({}), None
        self.calls.append((method.lower(), url, kwargs.get("json")))
        if method.lower() == "get":
            return _Resp(dict(self.stored)), None
        body = kwargs.get("json") or {}
        answer = dict(self.echo) if self.echo is not None else {**self.stored, **body}
        answer.setdefault("custom_dashboard_id", DASHBOARD)
        return _Resp(answer), None

    def writes(self) -> list[Call]:
        return [c for c in self.calls if c[0] in ("put", "post")]


def _install(
    monkeypatch: pytest.MonkeyPatch, stored: dict[str, Any], *, echo: dict[str, Any] | None = None
) -> tuple[_Transport, CallableToolExecutor]:
    transport = _Transport(stored, echo=echo)
    monkeypatch.setattr(_common, "safe_request", transport)
    executor = CallableToolExecutor()
    build_tools(f"{BASE}||test-token", executor)
    return transport, executor


async def _run(executor: CallableToolExecutor, arguments: dict[str, Any]) -> ToolResult:
    return await executor.execute_tool(ToolCall(id="call-dash", name="manage_openaev_dashboard", arguments=arguments))


_STORED: dict[str, Any] = {
    "custom_dashboard_id": DASHBOARD,
    "custom_dashboard_name": "[CTEM] Port scan reporting",
    "custom_dashboard_description": "Open ports per asset group",
    "custom_dashboard_parameters": [{"custom_dashboards_parameter_id": "p1"}],
}


def _only_write(transport: _Transport) -> dict[str, Any]:
    writes = transport.writes()
    assert len(writes) == 1, writes
    body = writes[0][2]
    assert isinstance(body, dict)
    return body


@pytest.mark.asyncio
async def test_update_with_a_name_keeps_the_stored_description(monkeypatch: pytest.MonkeyPatch) -> None:
    transport, executor = _install(monkeypatch, _STORED)

    result = await _run(executor, {"action": "update", "dashboard_id": DASHBOARD, "name": "Ports"})

    assert not result.is_error, result.content
    assert transport.writes()[0][:2] == ("put", f"{BASE}/api/custom-dashboards/{DASHBOARD}")
    assert _only_write(transport) == {
        "custom_dashboard_name": "Ports",
        "custom_dashboard_description": "Open ports per asset group",
    }
    assert "Kept as they were: custom_dashboard_description" in result.content


@pytest.mark.asyncio
async def test_update_without_a_name_keeps_the_stored_name(monkeypatch: pytest.MonkeyPatch) -> None:
    transport, executor = _install(monkeypatch, _STORED)

    result = await _run(executor, {"action": "update", "dashboard_id": DASHBOARD, "description": "New text"})

    assert not result.is_error, result.content
    assert transport.calls[0][0] == "get"
    assert _only_write(transport) == {
        "custom_dashboard_name": "[CTEM] Port scan reporting",
        "custom_dashboard_description": "New text",
    }


@pytest.mark.asyncio
async def test_config_can_clear_the_description_explicitly(monkeypatch: pytest.MonkeyPatch) -> None:
    transport, executor = _install(monkeypatch, _STORED)

    await _run(
        executor,
        {"action": "update", "dashboard_id": DASHBOARD, "config": json.dumps({"custom_dashboard_description": ""})},
    )

    body = _only_write(transport)
    assert body["custom_dashboard_description"] == ""
    assert body["custom_dashboard_name"] == "[CTEM] Port scan reporting"


@pytest.mark.parametrize("action", ["update", "create"])
@pytest.mark.asyncio
async def test_widgets_in_config_are_refused_and_nothing_is_written(
    monkeypatch: pytest.MonkeyPatch, action: str
) -> None:
    transport, executor = _install(monkeypatch, _STORED)

    result = await _run(
        executor,
        {
            "action": action,
            "dashboard_id": DASHBOARD,
            "name": "Ports",
            "config": json.dumps({"custom_dashboard_widgets": [{"widget_type": "list"}]}),
        },
    )

    assert "Nothing was written" in result.content
    assert "custom_dashboard_widgets" in result.content
    assert transport.calls == []


@pytest.mark.asyncio
async def test_an_answer_that_differs_from_the_write_is_flagged(monkeypatch: pytest.MonkeyPatch) -> None:
    echo = {**_STORED, "custom_dashboard_description": None, "custom_dashboard_name": "Ports"}
    _, executor = _install(monkeypatch, _STORED, echo=echo)

    result = await _run(executor, {"action": "update", "dashboard_id": DASHBOARD, "name": "Ports"})

    assert "WARNING" in result.content
    assert "custom_dashboard_description" in result.content.split("WARNING", 1)[1]


@pytest.mark.asyncio
async def test_create_posts_only_the_fields_given(monkeypatch: pytest.MonkeyPatch) -> None:
    transport, executor = _install(monkeypatch, _STORED)

    result = await _run(executor, {"action": "create", "name": "Fresh", "description": "D"})

    assert not result.is_error, result.content
    assert transport.calls == [
        (
            "post",
            f"{BASE}/api/custom-dashboards",
            {"custom_dashboard_name": "Fresh", "custom_dashboard_description": "D"},
        )
    ]
    assert "Created custom dashboard 'Fresh'" in result.content
