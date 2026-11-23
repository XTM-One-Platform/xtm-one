"""Coalescing of polled integration events into one queued run (#1417)."""

from __future__ import annotations

import inspect
from typing import Any

from app.services import assignment_engine, event_coalescing


def _event(run_id: str, count: int | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "message_id": f"opencrq:t:{run_id}",
        "entity": {"id": run_id},
        "covers": [{"id": run_id, "message_id": f"opencrq:t:{run_id}"}],
        "coalesce_key": "opencrq:t:risk_scenario_recomputed",
    }
    if count is not None:
        payload["run_count"] = count
    return payload


def test_the_key_is_read_from_the_payload_and_from_an_integration_event_context_only() -> None:
    assert event_coalescing.payload_key(_event("a")) == "opencrq:t:risk_scenario_recomputed"
    assert event_coalescing.payload_key({"message_id": "x"}) is None
    assert event_coalescing.payload_key(None) is None
    ctx = {"type": "integration_event", "coalesce_key": "k"}
    assert event_coalescing.context_key(ctx) == "k"
    assert event_coalescing.context_key({"type": "webhook", "coalesce_key": "k"}) is None
    assert event_coalescing.context_key({"type": "integration_event", "coalesce_key": ""}) is None


def test_a_folded_event_wins_the_entity_and_adds_its_runs() -> None:
    merged = event_coalescing.merged_payload(_event("a", 3), _event("b", 2))
    assert merged["message_id"] == "opencrq:t:b"
    assert merged["entity"] == {"id": "b"}
    assert [c["id"] for c in merged["covers"]] == ["a", "b"]
    assert merged["run_count"] == 5


def test_an_event_folded_twice_is_counted_once() -> None:
    merged = event_coalescing.merged_payload(_event("a"), _event("a"))
    assert [c["id"] for c in merged["covers"]] == ["a"]
    assert merged["run_count"] == 1


def test_thirty_folds_keep_one_payload_bounded() -> None:
    payload = _event("r00")
    for i in range(1, 80):
        payload = event_coalescing.merged_payload(payload, _event(f"r{i:02d}"))
    assert payload["run_count"] == 80
    assert len(payload["covers"]) == event_coalescing.MAX_COVERED
    assert payload["covers"][-1]["id"] == "r79"


def test_the_dispatcher_folds_before_any_slot_or_queue_decision() -> None:
    source = inspect.getsource(assignment_engine.trigger_or_queue_assignment)
    fold = source.index("_fold_into_queued_run(")
    assert fold < source.index("_slot_usage(db, assignment.id)")
    assert 'status="coalesced"' in source[fold:]


def test_the_fold_targets_a_queued_run_of_the_same_assignment_and_key_under_a_row_lock() -> None:
    source = inspect.getsource(assignment_engine._fold_into_queued_run)
    assert "AssignmentRun.status == QUEUED_RUN_STATUS" in source
    assert "AssignmentRun.assignment_id == assignment.id" in source
    assert "event_coalescing.CONTEXT_KEY" in source
    assert "with_for_update()" in source
