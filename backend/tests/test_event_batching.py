"""Batch window of event triggers: one run per batch of events (#1504)."""

from __future__ import annotations

import inspect
from typing import Any

import pytest

from app.services import assignment_engine, event_batching


def _event(event_id: str) -> dict[str, Any]:
    return {"trigger_type": "platform_event", "event_id": event_id, "event": "run.failed", "run_id": event_id}


PLATFORM = {"type": "platform_event"}


def test_the_window_applies_to_event_runs_only_and_is_bounded() -> None:
    assert event_batching.window_seconds({"batch_window_seconds": 60}, PLATFORM) == 60
    assert event_batching.window_seconds({"batch_window_seconds": 60}, {"type": "integration_event"}) == 60
    assert event_batching.window_seconds({"batch_window_seconds": 60}, {"type": "webhook"}) == 0
    assert event_batching.window_seconds({"batch_window_seconds": 60}, {"type": "schedule"}) == 0
    assert event_batching.window_seconds({}, PLATFORM) == 0
    assert event_batching.window_seconds(None, PLATFORM) == 0
    assert event_batching.window_seconds({"batch_window_seconds": True}, PLATFORM) == 0
    assert (
        event_batching.window_seconds({"batch_window_seconds": 10_000}, PLATFORM) == event_batching.MAX_WINDOW_SECONDS
    )


@pytest.mark.parametrize(
    ("trigger_type", "config", "refused"),
    [
        ("platform_event", {"event": "run.failed", "batch_window_seconds": 60}, False),
        ("integration_event", {"batch_window_seconds": 0}, False),
        ("platform_event", {"event": "run.failed"}, False),
        ("schedule", {"cron": "0 * * * *", "batch_window_seconds": 60}, True),
        ("platform_event", {"batch_window_seconds": 601}, True),
        ("platform_event", {"batch_window_seconds": -1}, True),
        ("platform_event", {"batch_window_seconds": "60"}, True),
        ("platform_event", {"batch_window_seconds": True}, True),
    ],
)
def test_the_window_is_judged_at_save(trigger_type: str, config: dict[str, Any], refused: bool) -> None:
    assert (event_batching.window_error(trigger_type, config) is not None) is refused


def test_thirty_events_in_the_window_make_one_payload_of_thirty() -> None:
    payload = event_batching.batch_payload(_event("e00"), 60)
    for i in range(1, 30):
        assert event_batching.can_join(payload)
        payload = event_batching.merged_payload(payload, _event(f"e{i:02d}"))
    assert payload["batch"] is True
    assert payload["event_count"] == 30
    assert [e["event_id"] for e in payload["events"]] == [f"e{i:02d}" for i in range(30)]
    assert payload["trigger_type"] == "platform_event"


def test_an_event_delivered_twice_is_listed_once() -> None:
    payload = event_batching.batch_payload(_event("a"), 30)
    assert event_batching.carries(payload, _event("a"))
    merged = event_batching.merged_payload(payload, _event("a"))
    assert merged["event_count"] == 1


def test_a_full_batch_takes_no_more_events() -> None:
    payload = event_batching.batch_payload(_event("e0"), 30)
    for i in range(1, event_batching.MAX_BATCH_EVENTS):
        payload = event_batching.merged_payload(payload, _event(f"e{i}"))
    assert payload["event_count"] == event_batching.MAX_BATCH_EVENTS
    assert not event_batching.can_join(payload)


def test_the_dispatcher_batches_before_any_slot_decision_and_delays_the_start() -> None:
    source = inspect.getsource(assignment_engine.trigger_or_queue_assignment)
    join = source.index("_join_waiting_batch(")
    assert join < source.index("_slot_usage(db, assignment.id)")
    assert 'status="coalesced"' in source[join:]
    assert "extra_delay_seconds=batch_window" in source


def test_the_window_applies_only_to_the_events_it_names() -> None:
    config = {"batch_window_seconds": 60, "batch_events": ["ctem_case.opened"]}
    assert event_batching.window_seconds(config, {"type": "platform_event", "event": "ctem_case.opened"}) == 60
    assert event_batching.window_seconds(config, {"type": "platform_event", "event": "ctem_case.reopened"}) == 0
    assert event_batching.window_seconds(config, {"type": "platform_event"}) == 0


def test_a_batch_holds_at_most_the_trigger_cap() -> None:
    config = {"batch_window_seconds": 60, "batch_max_events": 5}
    limit = event_batching.max_events(config)
    assert limit == 5
    payload = event_batching.batch_payload(_event("e0"), 60)
    for i in range(1, 5):
        assert event_batching.can_join(payload, limit)
        payload = event_batching.merged_payload(payload, _event(f"e{i}"))
    assert not event_batching.can_join(payload, limit)
    assert event_batching.max_events({}) == event_batching.MAX_BATCH_EVENTS
    assert event_batching.max_events({"batch_max_events": 10_000}) == event_batching.MAX_BATCH_EVENTS


def test_a_burst_of_500_opened_cases_starts_at_most_100_batches() -> None:
    limit = event_batching.max_events({"batch_max_events": 5})
    batches: list[dict[str, Any]] = []
    for i in range(500):
        event = _event(f"case-{i}")
        if batches and event_batching.can_join(batches[-1], limit):
            batches[-1] = event_batching.merged_payload(batches[-1], event)
        else:
            batches.append(event_batching.batch_payload(event, 60))
    assert len(batches) == 100
    listed = [e["event_id"] for b in batches for e in b["events"]]
    assert listed == [f"case-{i}" for i in range(500)]


@pytest.mark.parametrize(
    ("config", "refused"),
    [
        ({"batch_window_seconds": 60, "batch_events": ["ctem_case.opened"], "batch_max_events": 5}, False),
        ({"batch_window_seconds": 60, "batch_events": []}, True),
        ({"batch_window_seconds": 60, "batch_events": "ctem_case.opened"}, True),
        ({"batch_window_seconds": 60, "batch_events": [""]}, True),
        ({"batch_window_seconds": 60, "batch_max_events": 0}, True),
        ({"batch_window_seconds": 60, "batch_max_events": 101}, True),
        ({"batch_window_seconds": 60, "batch_max_events": True}, True),
    ],
)
def test_the_batch_scope_is_judged_at_save(config: dict[str, Any], refused: bool) -> None:
    assert (event_batching.window_error("platform_event", config) is not None) is refused


def test_the_seeded_case_listeners_batch_opened_cases_only() -> None:
    from app.agents import builtin_agents

    for assignment in (builtin_agents.CTEM_F1_CASE_ASSIGNMENT, builtin_agents.CTEM_F2_CASE_ASSIGNMENT):
        config = assignment["trigger_config"]
        assert event_batching.window_error("platform_event", config) is None
        assert config["batch_events"] == ["ctem_case.opened"]
        assert event_batching.max_events(config) == builtin_agents.CTEM_CASE_BATCH_MAX_EVENTS
        opened = {"type": "platform_event", "event": "ctem_case.opened"}
        assert event_batching.window_seconds(config, opened) > 0
        for name in ("ctem_case.reopened", "ctem_case.sla_breached", "ctem_case.stalled"):
            assert event_batching.window_seconds(config, {"type": "platform_event", "event": name}) == 0
        assert "own work item" in assignment["prompt"]


def test_the_join_reads_the_trigger_cap() -> None:
    source = inspect.getsource(assignment_engine._join_waiting_batch)
    assert "event_batching.max_events(assignment.trigger_config)" in source


def test_the_join_targets_a_waiting_batch_run_of_the_same_assignment_under_a_row_lock() -> None:
    source = inspect.getsource(assignment_engine._join_waiting_batch)
    assert "AssignmentRun.assignment_id == assignment.id" in source
    assert '(QUEUED_RUN_STATUS, "delayed")' in source
    assert "event_batching.WINDOW_CONTEXT" in source
    assert "with_for_update()" in source
    assert "still_open(" in source
