"""What changed since a source's previous read, and the alerts on its figure.

A read stands for one figure (a number's value, a series' last point, a
breakdown's total, a table's row count); what changed is computed from the two
reads, never by a model; an alert tells its people when the figure starts to
cross its threshold - once, until it goes back - and a failed read changes
nothing; the bell shows it and opens the dashboard.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.services.dashboard_definition import validate_definition
from app.services.dashboard_refresh import evaluate_alerts, figure_of, with_changes

KB = "11111111-1111-4111-8111-111111111111"


def _ok(shape: str, data: dict) -> dict:
    return {"status": "ok", "shape": shape, "data": data, "fetched_at": "2026-11-24T06:00:00+00:00"}


def test_a_read_stands_for_one_figure():
    assert figure_of(_ok("number", {"value": 4, "change": None})) == 4
    assert figure_of(_ok("series", {"series": [{"name": "a", "points": [{"x": "1", "y": 2}, {"x": "2", "y": 9}]}]})) == 9
    assert figure_of(_ok("distribution", {"items": [{"label": "a", "value": 2}, {"label": "b", "value": 3}]})) == 5
    assert figure_of(_ok("rows", {"columns": [], "rows": [{}, {}, {}], "total": None})) == 3
    assert figure_of(_ok("text", {"text": "hi"})) is None
    assert figure_of({"status": "error", "code": "timeout"}) is None
    assert figure_of(None) is None


def test_what_changed_is_computed_from_the_two_reads():
    since = datetime(2026, 11, 24, 6, 0, tzinfo=UTC)
    number = with_changes(_ok("number", {"value": 4, "change": None}), since, _ok("number", {"value": 7, "change": None}))
    assert number["changes"]["figure"] == {"before": 4, "after": 7} and number["data"]["change"] == 3
    # A number that says its own change keeps it.
    assert with_changes(_ok("number", {"value": 4, "change": None}), since, _ok("number", {"value": 7, "change": 1}))["data"]["change"] == 1
    before = _ok("distribution", {"items": [{"label": "high", "value": 2}, {"label": "low", "value": 9}]})
    after = _ok("distribution", {"items": [{"label": "high", "value": 6}, {"label": "low", "value": 8}, {"label": "new", "value": 1}]})
    changes = with_changes(before, since, after)["changes"]
    assert changes["since"] == since.isoformat() and changes["figure"] == {"before": 11, "after": 15}
    assert changes["items"] == [
        {"label": "high", "before": 2, "after": 6}, {"label": "low", "before": 9, "after": 8}, {"label": "new", "before": 0, "after": 1},
    ]  # fmt: skip
    # Nothing to compare with: the read as it is.
    first = _ok("number", {"value": 7, "change": None})
    assert with_changes(None, None, first) is first
    assert with_changes({"status": "error", "code": "timeout"}, since, first) is first


def test_an_alert_tells_once_when_it_starts_to_hold_and_a_failed_read_changes_nothing():
    source = {"options": {"alerts": [{"id": "many", "op": "gt", "value": 5}, {"id": "none", "op": "eq", "value": 0}]}}
    holding, started = evaluate_alerts(source, _ok("number", {"value": 8, "change": None}), [])
    assert holding == ["many"] and [a["id"] for a in started] == ["many"]
    holding, started = evaluate_alerts(source, _ok("number", {"value": 9, "change": None}), holding)
    assert holding == ["many"] and started == []
    assert evaluate_alerts(source, {"status": "error", "code": "timeout"}, holding) == (["many"], [])
    holding, started = evaluate_alerts(source, _ok("number", {"value": 0, "change": None}), holding)
    assert holding == ["none"] and [a["id"] for a in started] == ["none"]


def _source(options: dict) -> dict:
    return {"version": 2, "title": "T", "visualizations": {},
            "dataSources": {"s": {"type": "knowledge", "options": {"knowledge_base_id": KB, "document_id": KB, **options}}},
            "layout": {"tabs": {"items": [{"layoutId": "m", "label": "M"}]}, "layoutDefinitions": {"m": {"structure": []}}}}  # fmt: skip


def test_alerts_are_checked_on_sources_read_on_the_server():
    alert = {"id": "a", "op": "gt", "value": 5, "severity": "critical"}
    stored = validate_definition(_source({"refreshMode": "manual", "alerts": [alert]}))["dataSources"]["s"]["options"]["alerts"]
    assert stored == [{"id": "a", "op": "gt", "value": 5.0, "severity": "critical"}]
    with pytest.raises(Exception, match="checked when the server reads the source"):
        validate_definition(_source({"alerts": [alert]}))
    with pytest.raises(Exception, match="an id of its own"):
        validate_definition(_source({"refreshMode": "manual", "alerts": [alert, alert]}))
    # A source without alerts stores none.
    assert "alerts" not in validate_definition(_source({"refreshMode": "manual"}))["dataSources"]["s"]["options"]


def test_the_bell_shows_an_alert_and_opens_its_dashboard():
    from app.services.notification_feed import NOTIFICATION_KINDS, present_activity

    activity = SimpleNamespace(
        id="act-1", activity_type="notification", title="SOC: Critical findings", summary="8 > 5", agent_name=None,
        created_at="2026-11-24T06:00:00+00:00",
        extra_data={"notification_source": "dashboard_alert", "dashboard_id": KB, "severity": "critical"},
    )  # fmt: skip
    item = present_activity(activity, flows_enabled=False)
    assert (item.kind, item.route, item.critical) == ("dashboard_alert", f"/dashboards/{KB}", True)
    assert "dashboard_alert" in NOTIFICATION_KINDS
