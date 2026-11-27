"""Coalescing of polled integration events into one queued run (#1417)."""

from __future__ import annotations

import inspect
from datetime import UTC, datetime, timedelta
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


def test_a_payload_without_a_merge_spec_keeps_the_newest_fields() -> None:
    waiting = {**_event("a"), "tags": ["x"]}
    incoming = {**_event("b"), "tags": ["y"]}
    assert event_coalescing.merged_payload(waiting, incoming)["tags"] == ["y"]


def test_a_merge_spec_unions_lists_merges_by_key_and_keeps_counts_whole() -> None:
    spec = {"union": ["ids"], "by_key": {"rows": "id"}, "count_at_least": {"n": "ids"}}
    waiting = {
        **_event("a"),
        "coalesce_merge": spec,
        "ids": ["s1", "s2"],
        "rows": [{"id": "s1", "v": 1}],
        "n": 7,
        "entity": {"id": "a", "ids": ["s1", "s2"], "n": 7},
    }
    incoming = {
        **_event("b"),
        "coalesce_merge": spec,
        "ids": ["s2", "s3"],
        "rows": [{"id": "s1", "v": 2}, {"id": "s3", "v": 3}],
        "n": 2,
        "entity": {"id": "b", "ids": ["s2", "s3"], "n": 2},
    }
    merged = event_coalescing.merged_payload(waiting, incoming)
    assert merged["ids"] == merged["entity"]["ids"] == ["s1", "s2", "s3"]
    assert merged["rows"] == [{"id": "s1", "v": 2}, {"id": "s3", "v": 3}]
    assert "rows" not in merged["entity"]
    # Never below a side's own count (OpenCRQ caps its ids), never below the ids.
    assert merged["n"] == merged["entity"]["n"] == 7
    assert merged["entity"]["id"] == "b"


def test_a_sum_field_adds_up_and_counts_a_run_covered_twice_once() -> None:
    """``sum`` adds both sides' counts, less what a run both events cover carries (#2169)."""
    spec = {"sum": ["changed"]}
    waiting = {**_event("a"), "coalesce_merge": spec, "changed": 4, "entity": {"id": "a", "changed": 4}}
    waiting["covers"] = [{"id": "a", "changed": 4}]
    incoming = {**_event("b"), "coalesce_merge": spec, "changed": 9, "entity": {"id": "b", "changed": 9}}
    incoming["covers"] = [{"id": "a", "changed": 4}, {"id": "b", "changed": 5}]
    merged = event_coalescing.merged_payload(waiting, incoming)
    assert merged["changed"] == merged["entity"]["changed"] == 9
    fresh = {**incoming, "covers": [{"id": "b", "changed": 5}], "changed": 5}
    assert event_coalescing.merged_payload(waiting, fresh)["changed"] == 9
    assert "changed" not in event_coalescing.merged_payload(_event("a"), {**_event("b"), "coalesce_merge": spec})


def test_a_run_that_has_not_started_is_foldable() -> None:
    assert set(event_coalescing.FOLDABLE_STATUSES) == {assignment_engine.QUEUED_RUN_STATUS, "pending"}


def test_the_executor_rereads_a_coalesced_payload_under_the_row_lock_before_it_starts() -> None:
    source = inspect.getsource(assignment_engine.execute_assignment_run)
    reread = source.index("select(AssignmentRun.trigger_payload)")
    assert "event_coalescing.context_key(" in source[reread - 400 : reread]
    assert "with_for_update()" in source[reread : reread + 200]
    assert reread < source.index('run.status = "running"')


_T0 = datetime(2026, 11, 26, 18, 0, tzinfo=UTC)


def _debounced(run_id: str, quiet: Any = 300, cap: Any = 900) -> dict[str, Any]:
    return {**_event(run_id), event_coalescing.QUIET_KEY: quiet, event_coalescing.MAX_KEY: cap}


def test_an_event_without_the_declaration_is_not_debounced() -> None:
    """Every provider that declares no quiet period keeps today's dispatch (#2544)."""
    assert event_coalescing.debounce(_event("a")) is None
    assert event_coalescing.debounce(_debounced("a", quiet=0)) is None
    assert event_coalescing.debounce(_debounced("a", quiet=True)) is None
    assert event_coalescing.debounce({"coalesce_quiet_seconds": 300}) is None, "no coalesce key, no hold"
    assert event_coalescing.held_context({"type": "integration_event"}, _event("a"), _T0) is None
    assert event_coalescing.hold_seconds({"type": "integration_event"}, _T0) == 0
    assert event_coalescing.hold_seconds(None, _T0) == 0


def test_the_cap_is_never_below_the_quiet_period_nor_past_the_bound() -> None:
    assert event_coalescing.debounce(_debounced("a", 300, 900)) == (300, 900)
    assert event_coalescing.debounce(_debounced("a", 300, 60)) == (300, 300)
    assert event_coalescing.debounce(_debounced("a", 10**6, 10**7)) == (
        event_coalescing.MAX_HOLD_SECONDS,
        event_coalescing.MAX_HOLD_SECONDS,
    )


def test_a_hold_starts_after_the_quiet_period_and_never_past_the_cap() -> None:
    first = event_coalescing.held_context({"type": "integration_event"}, _debounced("a"), _T0)
    assert first is not None
    assert event_coalescing.hold_seconds(first, _T0) == 300
    # Each later event keeps the burst's first instant and moves the last one.
    later: dict[str, Any] | None = first
    for minute in (1, 2, 3):
        moment = _T0 + timedelta(minutes=minute)
        later = event_coalescing.held_context(later, _debounced("b"), moment)
        assert later is not None
        assert later[event_coalescing.HOLD_CONTEXT]["first_at"] == _T0.isoformat()
        assert event_coalescing.hold_seconds(later, moment) == 300
    # Twelve minutes in, the cap (15 min from the first event) wins over the quiet period.
    moment = _T0 + timedelta(minutes=12)
    late = event_coalescing.held_context(later, _debounced("c"), moment)
    assert event_coalescing.hold_seconds(late, moment) == 180
    assert event_coalescing.hold_seconds(late, _T0 + timedelta(minutes=20)) == 0


def test_the_dispatcher_records_the_hold_before_it_folds_or_starts_a_run() -> None:
    source = inspect.getsource(assignment_engine.trigger_or_queue_assignment)
    held = source.index("event_coalescing.held_context(")
    assert held < source.index("_fold_into_queued_run(") < source.index("_slot_usage(db, assignment.id)")
    schedule = inspect.getsource(assignment_engine._schedule_run_execution)
    assert "event_coalescing.hold_seconds(trigger_context, event_coalescing.now())" in schedule


def test_the_fold_targets_a_waiting_run_of_the_same_assignment_and_key_under_a_row_lock() -> None:
    source = inspect.getsource(assignment_engine._fold_into_queued_run)
    assert "AssignmentRun.status.in_(event_coalescing.FOLDABLE_STATUSES)" in source
    assert "AssignmentRun.assignment_id == assignment.id" in source
    assert "event_coalescing.CONTEXT_KEY" in source
    assert "with_for_update()" in source
