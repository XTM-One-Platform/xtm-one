"""Relative-date placeholders in an assignment's ``activation_check`` (#10319).

``{{date.today+N}}`` and ``{{datetime.day_start+N}}`` / ``{{datetime.day_end+N}}``
resolve at evaluation time in the trigger's zone, so a schedule gate can target
"an event on the local day N days ahead" instead of a window anchored on now.
"""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from app.services.assignment_activation_check import (
    parse_activation_check,
    resolved_arguments,
    run_activation_check,
    stored_activation_check,
    unknown_placeholders,
    uses_date_placeholders,
)
from app.tools.base import CallableToolExecutor, CompositeToolExecutor, ToolDefinition, ToolResult

PARIS = ZoneInfo("Europe/Paris")

ALL_HANDS_CHECK = {
    "tool": "list_microsoft_calendar_events",
    "arguments": {"start_datetime": "{{datetime.day_start+7}}", "end_datetime": "{{datetime.day_end+7}}"},
    "condition": {"path": "$.subject", "contains": "Quarterly All Hands"},
}


def test_date_today_and_offsets_in_the_zone():
    # 23:30 UTC on 9 Nov is already 10 Nov in Paris.
    now = datetime(2026, 11, 9, 23, 30, tzinfo=UTC)
    args = {"a": "{{date.today}}", "b": "{{date.today+7}}", "c": "{{ date.today - 1 }}"}
    assert resolved_arguments(args, {}, now=now, zone=PARIS) == {
        "a": "2026-11-10",
        "b": "2026-11-17",
        "c": "2026-11-09",
    }
    assert resolved_arguments(args, {}, now=now) == {"a": "2026-11-09", "b": "2026-11-16", "c": "2026-11-08"}


def test_day_window_is_exactly_the_local_day_ahead():
    now = datetime(2026, 11, 9, 7, 0, tzinfo=UTC)
    resolved = resolved_arguments(ALL_HANDS_CHECK["arguments"], {}, now=now, zone=PARIS)
    assert resolved == {
        "start_datetime": "2026-11-16T00:00:00+01:00",
        "end_datetime": "2026-11-16T23:59:59+01:00",
    }


@pytest.mark.parametrize(
    ("now", "start", "end"),
    [
        # Before the late-October change (CEST, +02:00), 7 days ahead is after it (CET, +01:00).
        (datetime(2026, 10, 21, 6, 0, tzinfo=UTC), "2026-10-28T00:00:00+01:00", "2026-10-28T23:59:59+01:00"),
        # Before the late-March change (CET), 7 days ahead is after it (CEST).
        (datetime(2027, 3, 22, 6, 0, tzinfo=UTC), "2027-03-29T00:00:00+02:00", "2027-03-29T23:59:59+02:00"),
        # The change day itself: midnight is before it, the end of the day after it.
        (datetime(2026, 10, 18, 6, 0, tzinfo=UTC), "2026-10-25T00:00:00+02:00", "2026-10-25T23:59:59+01:00"),
    ],
)
def test_offsets_follow_the_target_day_across_dst(now, start, end):
    args = {"s": "{{datetime.day_start+7}}", "e": "{{datetime.day_end+7}}"}
    assert resolved_arguments(args, {}, now=now, zone=PARIS) == {"s": start, "e": end}


def test_save_refuses_an_unknown_or_malformed_placeholder():
    for bad in ("{{date.tomorrow}}", "{{datetime.day_start*7}}", "{{date.today+999}}", "{{now}}"):
        value, error, provided = parse_activation_check({"tool": "list_work_items", "arguments": {"since": bad}})
        assert value is None and provided is True
        assert bad in error and "Valid placeholders" in error and "{{datetime.day_end+N}}" in error


def test_save_accepts_every_known_placeholder():
    args = {
        "a": "{{agent.id}}",
        "b": ["{{assignment.id}}", "{{payload.pull_request.number}}"],
        "c": {"d": "from {{date.today-3}} to {{date.today}}", "e": "{{datetime.day_end+366}}"},
    }
    assert unknown_placeholders(args) == []
    value, error, _ = parse_activation_check({"tool": "list_work_items", "arguments": args})
    assert error is None and value["arguments"] == args


def test_a_stored_check_with_an_unknown_placeholder_still_reads():
    # Saved before the rule: the engine keeps evaluating it as it did.
    stored = {"tool": "list_work_items", "arguments": {"q": "{{something.else}}"}}
    assert stored_activation_check(stored)["arguments"] == {"q": "{{something.else}}"}


def test_checks_without_date_placeholders_resolve_unchanged():
    args = {"owner_agent_id": "{{agent.id}}", "open_only": True, "n": 3}
    assert not uses_date_placeholders(args)
    assert uses_date_placeholders(ALL_HANDS_CHECK["arguments"])
    assert resolved_arguments(args, {"agent.id": "agent-7"}) == {"owner_agent_id": "agent-7", "open_only": True, "n": 3}


def _calendar(events_by_day: dict[str, list[dict]], seen: dict) -> CompositeToolExecutor:
    async def _list(**kwargs) -> ToolResult:
        seen["args"] = kwargs
        events = events_by_day.get(str(kwargs.get("start_datetime", ""))[:10], [])
        return ToolResult("", f"{len(events)} event(s)", structured=events)

    inner = CallableToolExecutor()
    inner.register("list_microsoft_calendar_events", _list)
    composite = CompositeToolExecutor()
    composite.add_tools([ToolDefinition(name="list_microsoft_calendar_events", description="list")], inner)
    return composite


@pytest.mark.asyncio
async def test_the_gate_passes_only_when_the_event_is_seven_paris_days_ahead():
    now = datetime(2026, 11, 9, 7, 0, tzinfo=UTC)
    seen: dict = {}
    ahead_7 = _calendar({"2026-11-16": [{"subject": "Quarterly All Hands"}]}, seen)
    outcome = await run_activation_check(ALL_HANDS_CHECK, ahead_7, zone=PARIS, now=now)
    assert outcome.passed and outcome.error is None
    assert seen["args"] == {"start_datetime": "2026-11-16T00:00:00+01:00", "end_datetime": "2026-11-16T23:59:59+01:00"}

    ahead_3 = _calendar({"2026-11-12": [{"subject": "Quarterly All Hands"}]}, {})
    outcome = await run_activation_check(ALL_HANDS_CHECK, ahead_3, zone=PARIS, now=now)
    assert not outcome.passed and outcome.summary.startswith("Skipped: gate check")
