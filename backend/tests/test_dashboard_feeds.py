"""Feeds and event refresh: a source read on a schedule or an event, an assignment's trend, a feed created in one call.

A source refreshed on a schedule takes a cron firing at most every 5 minutes;
one refreshed on an event names one event; an event marks the sources read on
it; an assignment source with ``history`` draws the figure of each of its last
runs as a series; ``create_feed`` creates the assignment through the agent
builder's own tools, runs it once and adds the widget reading it.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.services import dashboard_refresh
from app.services.dashboard_definition import validate_definition

KB = "11111111-1111-4111-8111-111111111111"


def _with(options: dict) -> dict:
    source = {"type": "knowledge", "options": {"knowledge_base_id": KB, "document_id": KB, **options}}
    return validate_definition({
        "version": 2, "title": "T", "dataSources": {"s": source}, "visualizations": {},
        "layout": {"tabs": {"items": [{"layoutId": "m", "label": "M"}]}, "layoutDefinitions": {"m": {"structure": []}}},
    })["dataSources"]["s"]  # fmt: skip


def test_a_schedule_takes_a_cron_firing_at_most_every_5_minutes():
    assert _with({"refreshMode": "schedule", "cron": " 0 7 * * 1-5 "})["options"]["cron"] == "0 7 * * 1-5"
    for bad, reason in [({"refreshMode": "schedule", "cron": "* * * * *"}, "at most every 5 minutes"),
                        ({"refreshMode": "schedule"}, "a schedule needs one"),
                        ({"cron": "0 7 * * *"}, "goes with refreshMode 'schedule'")]:  # fmt: skip
        with pytest.raises(Exception, match=reason):
            _with(bad)


def test_an_event_refresh_names_one_event():
    on = _with({"refreshMode": "event", "on": {"integrationEvent": "issues.opened"}})["options"]["on"]
    assert on == {"integrationEvent": "issues.opened"}
    for bad, reason in [({"refreshMode": "event"}, "needs one"),
                        ({"refreshMode": "event", "on": {"platformEvent": "run.completed", "integrationEvent": "x"}}, "names one event"),
                        ({"refreshMode": "event", "on": {"platformEvent": "run.completed", "integrationId": KB}}, "goes with integrationEvent")]:  # fmt: skip
        with pytest.raises(Exception, match=reason):
            _with(bad)


def test_each_mode_says_when_the_server_reads_it_again():
    now = datetime(2026, 11, 24, 6, 0, tzinfo=UTC)
    server = {"options": {"refreshMode": "server", "refresh": "1h"}}
    schedule = {"options": {"refreshMode": "schedule", "cron": "0 7 * * *"}}
    event = {"options": {"refreshMode": "event", "on": {"integrationEvent": "issues.opened", "integrationId": KB}}}
    assert dashboard_refresh._next_due(server, now) == datetime(2026, 11, 24, 7, 0, tzinfo=UTC)
    assert dashboard_refresh._next_due(schedule, now) == datetime(2026, 11, 24, 7, 0, tzinfo=UTC)
    assert dashboard_refresh._next_due(event, now) == dashboard_refresh.NEVER
    assert dashboard_refresh.trigger_of(event) == f"integration:issues.opened:{KB}"
    assert dashboard_refresh.trigger_of({"options": {"refreshMode": "event", "on": {"platformEvent": "run.completed"}}}) == "platform:run.completed"
    assert dashboard_refresh.trigger_of(server) is None


async def test_an_integration_event_marks_the_sources_read_on_it(monkeypatch, dashboard_studio_enabled):
    marked: list[list[str]] = []

    async def _mark(_factory, triggers):
        marked.append(triggers)
        return 1

    monkeypatch.setattr(dashboard_refresh, "mark_event", _mark)
    await dashboard_refresh.hear_integration_event({"type": "integration_event", "event": "issues.opened", "integration_id": KB})
    await dashboard_refresh.hear_integration_event({"type": "webhook", "event_key": "push"})
    await dashboard_refresh.hear_integration_event({"type": "webhook"})
    assert marked == [["integration:issues.opened:*", f"integration:issues.opened:{KB}"], ["integration:push:*"]]


def test_an_assignment_s_trend_is_the_figure_of_each_run():
    from app.services.dashboard_sources import _trend

    def run(minute: int, answer: str):
        at = datetime(2026, 11, 24, 6, minute, tzinfo=UTC)
        return SimpleNamespace(result_summary=answer, completed_at=at, created_at=at)

    runs = [run(0, '```json\n{"value": 3}\n```'), run(5, "no figure today"), run(10, 'Done.\n{"value": 7}')]
    read = _trend(SimpleNamespace(name="Open findings"), runs, {})
    assert read["status"] == "ok" and read["shape"] == "series"
    assert [(p["x"], p["y"]) for p in read["data"]["series"][0]["points"]] == [
        ("2026-11-24T06:00:00+00:00", 3), ("2026-11-24T06:10:00+00:00", 7),
    ]  # fmt: skip
    assert _trend(SimpleNamespace(name="x"), [run(0, "nothing")], {}) == {"status": "error", "code": "no_result"}
    with pytest.raises(Exception, match="history draws each run's figure"):
        validate_definition({
            "version": 2, "title": "T", "dataSources": {"s": {"type": "assignment", "options": {"assignment_id": KB, "history": 10}}},
            "visualizations": {}, "layout": {"tabs": {"items": [{"layoutId": "m", "label": "M"}]}, "layoutDefinitions": {"m": {"structure": []}}},
        })  # fmt: skip


async def test_a_feed_is_an_assignment_made_by_the_agent_builder_and_a_widget_reading_it(monkeypatch, dashboard_studio_enabled):
    from contextlib import asynccontextmanager

    import app.core.database as database
    from app.services.builtin_tools import agent_builder, custom_dashboard_studio
    from app.tools.base import ToolDefinition, ToolResult

    assignment_id = str(uuid.uuid4())
    calls: list[tuple[str, dict]] = []

    class _Builder:
        async def execute_tool(self, call):
            calls.append((call.name, call.arguments))
            if call.name == "create_assignment":
                return ToolResult(tool_call_id="x", content=f"Assignment created.\n  • ID: `{assignment_id}`\n")
            return ToolResult(tool_call_id="x", content="Run started.")

    @asynccontextmanager
    async def _session():
        yield None

    monkeypatch.setattr(agent_builder, "build_agent_builder_tool", lambda *_a, **_k: ([], _Builder()))
    refusal: list[str | None] = [None]

    async def _writable(*_a, **_k):
        return SimpleNamespace(), refusal[0]

    monkeypatch.setattr(agent_builder, "_load_writable_agent_for_builder", _writable)
    monkeypatch.setattr(database, "async_session", _session)
    added: list[dict] = []

    async def _edit(**kwargs):
        return ToolResult(tool_call_id="x", content="Updated dashboard.")

    definitions, handlers = custom_dashboard_studio.build_dashboard_studio_tools(
        user_id=str(uuid.uuid4()), conversation_id=str(uuid.uuid4()), assignment_run_id=None, edit=_edit, source="test"
    )
    assert "create_feed" in {d.name for d in definitions if isinstance(d, ToolDefinition)}

    async def _working(_dashboard_id, *, need="read"):
        return SimpleNamespace(revision=1), {"dataSources": {}, "visualizations": {}, "inputs": {}, "title": "T",
                                             "layout": {"tabs": {"items": [{"layoutId": "m", "label": "M"}]}, "layoutDefinitions": {"m": {"structure": []}}}}  # fmt: skip

    # The feed reads the dashboard and adds its widget through the studio's own helpers: observed here.
    async def _add(**kwargs):
        added.append(kwargs)
        return ToolResult(tool_call_id="x", content="Widget added.")

    feed = handlers["create_feed"]
    closure = dict(zip(feed.__code__.co_freevars, feed.__closure__ or (), strict=True))
    closure["_working"].cell_contents = _working
    closure["_add"].cell_contents = _add
    answer = await feed(
        dashboard_id=str(uuid.uuid4()), agent_id=str(uuid.uuid4()), name="Open findings", prompt="Count the open findings.",
        trigger={"type": "schedule", "config": {"cron": "0 7 * * *"}}, shape="number", history=30,
        knowledge={"document_id": KB},
    )  # fmt: skip
    assert answer.is_error is False, answer.content
    created = dict(calls)["create_assignment"]
    assert (created["trigger_type"], created["trigger_config"]) == ("schedule", '{"cron": "0 7 * * *"}')
    assert '{"value": <number>}' in created["prompt"] and KB in created["prompt"]
    assert calls[1] == ("trigger_assignment", {"assignment_id": assignment_id})
    assert added[0]["source"]["options"] == {"assignment_id": assignment_id, "shape": "series", "history": 30}
    assert added[0]["visualization"]["type"] == "xtm.line"

    # An agent the user may not give an assignment (a built-in one): refused before anything is created, with what to do.
    calls.clear()
    refusal[0] = "Cannot add an assignment to built-in agents."
    refused = await feed(dashboard_id=str(uuid.uuid4()), agent_id=str(uuid.uuid4()), name="N", prompt="P")
    assert refused.is_error and calls == []
    assert "built-in agents. A feed runs on an agent the user may give an assignment" in refused.content
    assert "create_agent" in refused.content
