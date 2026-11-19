"""manage_openaev_dashboard writes widgets: create, update, move, delete.

OpenAEV's widget endpoints are ``POST /api/custom-dashboards/{id}/widgets``,
``PUT .../widgets/{widget_id}`` (replaces type, configuration and layout
whole), ``PUT .../widgets/{widget_id}/layout`` and ``DELETE``. The layout is
bound to primitive ints, so a field left out would be stored as 0: the tool
merges over the stored widget and refuses what OpenAEV would reject or store
wrong, before any write.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from app.integrations.providers.openaev import _common, build_tools
from app.tools.base import CallableToolExecutor, ToolCall, ToolResult

BASE = "https://oaev.example"
DASHBOARD = "d19b316e-46a3-4319-87d4-886313d30cff"
WIDGET = "6b0e1c2a-0000-4000-8000-000000000001"
WIDGETS_URL = f"{BASE}/api/custom-dashboards/{DASHBOARD}/widgets"

_LAYOUT = {"widget_layout_x": 0, "widget_layout_y": 2, "widget_layout_w": 6, "widget_layout_h": 4}
_CONFIG = {
    "widget_configuration_type": "flat",
    "title": "Open ports",
    "series": [
        {
            "name": "ports",
            "filter": {
                "mode": "and",
                "filters": [{"key": "base_entity", "operator": "eq", "values": ["finding"], "mode": "or"}],
            },
        }
    ],
}
_STORED = {"widget_id": WIDGET, "widget_type": "number", "widget_config": _CONFIG, "widget_layout": _LAYOUT}


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
    """OpenAEV double: a write updates the stored widget; ``echo`` overrides what a PUT answers."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, Any]] = []
        self.stored: dict[str, Any] = dict(_STORED)
        self.echo: dict[str, Any] | None = None
        self.read_fails = False
        self.ignores_writes = False

    async def __call__(
        self, client: Any, method: str, url: str, provider: str, action: str, **kwargs: Any
    ) -> tuple[_Resp, None]:
        if url == f"{BASE}/api/settings":
            return _Resp({}), None
        method = method.lower()
        body = kwargs.get("json")
        self.calls.append((method, url, body))
        if method == "get" and url == WIDGETS_URL:
            return _Resp([_STORED]), None
        if method == "get":
            if self.read_fails and self.writes():
                return _Resp(None), "OpenAEV read widget failed: 500"  # type: ignore[return-value]
            return _Resp(dict(self.stored)), None
        if method == "delete":
            return _Resp(None), None
        if method == "put":
            echoed = {**self.stored, **(self.echo or {})}
            if self.ignores_writes:
                return _Resp(echoed), None
            written: dict[str, Any] = body or {}
            if url.endswith("/layout"):
                self.stored = {**self.stored, "widget_layout": written}
            else:
                self.stored = {**self.stored, **written}
            return _Resp(echoed if self.echo is not None else dict(self.stored)), None
        return _Resp({"widget_id": WIDGET, **(body or {})}), None

    def writes(self) -> list[tuple[str, str, Any]]:
        return [c for c in self.calls if c[0] in ("post", "put", "delete")]


@pytest.fixture
def setup(monkeypatch: pytest.MonkeyPatch) -> tuple[_Transport, CallableToolExecutor]:
    transport = _Transport()
    monkeypatch.setattr(_common, "safe_request", transport)
    executor = CallableToolExecutor()
    build_tools(f"{BASE}||test-token", executor)
    return transport, executor


async def _run(executor: CallableToolExecutor, **arguments: Any) -> str:
    result: ToolResult = await executor.execute_tool(
        ToolCall(id="call-widget", name="manage_openaev_dashboard", arguments=arguments)
    )
    return str(result.content)


@pytest.mark.asyncio
async def test_create_widget_posts_the_widget_input_and_places_it_under_the_others(setup) -> None:
    transport, executor = setup
    answer = await _run(
        executor,
        action="create_widget",
        dashboard_id=DASHBOARD,
        config=json.dumps({"widget_type": "number", "widget_config": _CONFIG}),
    )
    [(method, url, body)] = transport.writes()
    assert (method, url) == ("post", WIDGETS_URL)
    assert body["widget_type"] == "number"
    assert body["widget_config"] == _CONFIG
    # The stored widget ends at row 2 + 4: the new one goes on row 6, default size.
    assert body["widget_layout"] == {
        "widget_layout_x": 0,
        "widget_layout_y": 6,
        "widget_layout_w": 6,
        "widget_layout_h": 4,
    }
    assert f"Created number widget {WIDGET}" in answer
    assert "Placed under the existing widgets" in answer


@pytest.mark.asyncio
async def test_create_widget_takes_short_layout_names(setup) -> None:
    transport, executor = setup
    await _run(
        executor,
        action="create_widget",
        dashboard_id=DASHBOARD,
        config=json.dumps(
            {"widget_type": "donut", "widget_config": _CONFIG, "widget_layout": {"x": 6, "y": 0, "w": 6, "h": 5}}
        ),
    )
    [(_, _, body)] = transport.writes()
    assert body["widget_layout"] == {
        "widget_layout_x": 6,
        "widget_layout_y": 0,
        "widget_layout_w": 6,
        "widget_layout_h": 5,
    }
    # A layout with y given needs no read of the other widgets.
    assert not [c for c in transport.calls if c[0] == "get"]


@pytest.mark.parametrize(
    ("config", "expected"),
    [
        ({"widget_type": "pie", "widget_config": _CONFIG}, "widget_type 'pie' is not an OpenAEV widget type"),
        ({"widget_type": "number"}, "create_widget needs widget_type and widget_config"),
        ({"widget_type": "number", "widget_config": {"title": "x"}}, "widget_configuration_type"),
        (
            {
                "widget_type": "number",
                "widget_config": {
                    **_CONFIG,
                    "series": [
                        {
                            "filter": {
                                "mode": "and",
                                "filters": [
                                    {"key": "base_entity", "operator": "eq", "values": ["endpoint"], "mode": "or"}
                                ],
                            }
                        }
                    ],
                },
            },
            "base_entity 'endpoint' is not indexed",
        ),
        ({"widget_type": "number", "widget_config": _CONFIG, "widget_layout": {"x": -1}}, "x must be a whole number"),
        ({"widget_type": "number", "widget_config": _CONFIG, "widget_layout": {"z": 1}}, "'z' is not a layout field"),
        ({"widget_type": "number", "widget_config": _CONFIG, "widgets": []}, "widgets are not widget fields"),
    ],
)
@pytest.mark.asyncio
async def test_create_widget_refuses_before_any_write(setup, config: dict[str, Any], expected: str) -> None:
    transport, executor = setup
    answer = await _run(executor, action="create_widget", dashboard_id=DASHBOARD, config=json.dumps(config))
    assert answer.startswith("Nothing was written") or "Nothing was written" in answer
    assert expected in answer
    assert transport.writes() == []


@pytest.mark.asyncio
async def test_update_widget_keeps_what_the_call_does_not_name(setup) -> None:
    transport, executor = setup
    answer = await _run(
        executor,
        action="update_widget",
        dashboard_id=DASHBOARD,
        widget_id=WIDGET,
        config=json.dumps({"widget_type": "donut", "widget_layout": {"w": 12}}),
    )
    [(method, url, body)] = transport.writes()
    assert (method, url) == ("put", f"{WIDGETS_URL}/{WIDGET}")
    assert body["widget_type"] == "donut"
    assert body["widget_config"] == _CONFIG
    assert body["widget_layout"] == {**_LAYOUT, "widget_layout_w": 12}
    assert "Kept as they were: widget_config" in answer


@pytest.mark.asyncio
async def test_update_layout_merges_over_the_stored_layout(setup) -> None:
    transport, executor = setup
    answer = await _run(
        executor, action="update_layout", dashboard_id=DASHBOARD, widget_id=WIDGET, config=json.dumps({"y": 0})
    )
    [(method, url, body)] = transport.writes()
    assert (method, url) == ("put", f"{WIDGETS_URL}/{WIDGET}/layout")
    assert body == {**_LAYOUT, "widget_layout_y": 0}
    assert answer == f"Moved widget {WIDGET}: x 0, y 0, 6 wide, 4 high."


@pytest.mark.parametrize("action", ["update_widget", "update_layout"])
@pytest.mark.asyncio
async def test_widget_write_answer_reports_the_layout_read_back_not_the_write_echo(
    setup: tuple[_Transport, CallableToolExecutor], action: str
) -> None:
    transport, executor = setup
    # The PUT answers the layout as it was before the write: the answer must not echo it.
    transport.echo = {"widget_layout": dict(_LAYOUT)}
    config: dict[str, Any] = {"widget_layout": {"y": 9}} if action == "update_widget" else {"y": 9}
    answer = await _run(executor, action=action, dashboard_id=DASHBOARD, widget_id=WIDGET, config=json.dumps(config))
    assert "x 0, y 9, 6 wide, 4 high" in answer
    assert "y 2" not in answer
    assert "stored a layout other" not in answer
    assert [c for c in transport.calls if c[0] == "get"][-1][1] == f"{WIDGETS_URL}/{WIDGET}"


@pytest.mark.asyncio
async def test_widget_write_answer_says_when_the_stored_layout_differs(
    setup: tuple[_Transport, CallableToolExecutor],
) -> None:
    transport, executor = setup
    # OpenAEV answers the write but the row keeps its old layout (y 2).
    transport.ignores_writes = True
    answer = await _run(
        executor, action="update_layout", dashboard_id=DASHBOARD, widget_id=WIDGET, config=json.dumps({"y": 7})
    )
    assert "x 0, y 2, 6 wide, 4 high" in answer
    assert "OpenAEV stored a layout other than the one sent." in answer


@pytest.mark.asyncio
async def test_widget_write_answer_names_a_failed_read_back(setup: tuple[_Transport, CallableToolExecutor]) -> None:
    transport, executor = setup
    transport.read_fails = True
    answer = await _run(
        executor, action="update_layout", dashboard_id=DASHBOARD, widget_id=WIDGET, config=json.dumps({"y": 5})
    )
    assert "x 0, y 5, 6 wide, 4 high" in answer
    assert "could not be read back: this is the layout sent" in answer


@pytest.mark.asyncio
async def test_delete_widget(setup) -> None:
    transport, executor = setup
    answer = await _run(executor, action="delete_widget", dashboard_id=DASHBOARD, widget_id=WIDGET)
    assert transport.writes() == [("delete", f"{WIDGETS_URL}/{WIDGET}", None)]
    assert answer == f"Deleted widget {WIDGET} from dashboard {DASHBOARD}."


@pytest.mark.parametrize("action", ["update_widget", "update_layout", "delete_widget"])
@pytest.mark.asyncio
async def test_widget_writes_need_a_widget_id(setup, action: str) -> None:
    transport, executor = setup
    answer = await _run(executor, action=action, dashboard_id=DASHBOARD, config="{}")
    assert answer == f"widget_id is required for action '{action}'."
    assert transport.calls == []


@pytest.mark.asyncio
async def test_dashboard_update_points_at_the_widget_actions(setup) -> None:
    transport, executor = setup
    answer = await _run(
        executor, action="update", dashboard_id=DASHBOARD, config=json.dumps({"custom_dashboard_widgets": []})
    )
    assert "create_widget, update_widget, update_layout and delete_widget" in answer
    assert transport.writes() == []
