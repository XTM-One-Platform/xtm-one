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


def test_the_join_targets_a_waiting_batch_run_of_the_same_assignment_under_a_row_lock() -> None:
    source = inspect.getsource(assignment_engine._join_waiting_batch)
    assert "AssignmentRun.assignment_id == assignment.id" in source
    assert '(QUEUED_RUN_STATUS, "delayed")' in source
    assert "event_batching.WINDOW_CONTEXT" in source
    assert "with_for_update()" in source
    assert "still_open(" in source
