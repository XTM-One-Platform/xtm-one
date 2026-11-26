"""A batch of a ``batch_split`` trigger is a dispatch bound only: one context per event (#2379)."""

from __future__ import annotations

import inspect
from typing import Any

import pytest

from app.services import assignment_engine, event_batching


def _case_event(case_id: str) -> dict[str, Any]:
    return {
        "trigger_type": "platform_event",
        "event": "ctem_case.opened",
        "event_id": f"evt-{case_id}",
        "object_type": "ctem_case",
        "object_id": case_id,
        "case_id": case_id,
    }


def _batch_of(n: int) -> dict[str, Any]:
    payload = event_batching.batch_payload(_case_event("c0"), 60)
    for i in range(1, n):
        payload = event_batching.merged_payload(payload, _case_event(f"c{i}"))
    return payload


def _contexts(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """The payload each run of a split chain works, in order (the drain runs them one after the other)."""
    worked: list[dict[str, Any]] = []
    rest: dict[str, Any] | None = payload
    while rest is not None:
        first, rest = event_batching.split_batch(rest)
        worked.append(first)
    return worked


def test_a_batch_of_five_opened_cases_yields_five_contexts_of_one_case_each() -> None:
    worked = _contexts(_batch_of(5))
    assert [p["case_id"] for p in worked] == ["c0", "c1", "c2", "c3", "c4"]
    for payload in worked:
        # Each context starts from its own event only: no list, no other case.
        assert "events" not in payload
        assert payload.get("batch") is not True
        assert payload["event"] == "ctem_case.opened"


def test_the_rest_keeps_the_batch_form_in_arrival_order() -> None:
    first, rest = event_batching.split_batch(_batch_of(3))
    assert first["case_id"] == "c0"
    assert rest is not None
    assert rest["batch"] is True
    assert rest["event_count"] == 2
    assert [e["case_id"] for e in rest["events"]] == ["c1", "c2"]


@pytest.mark.parametrize("payload", [_case_event("solo"), {}, None])
def test_an_unbatched_payload_is_worked_as_it_is(payload: Any) -> None:
    first, rest = event_batching.split_batch(payload)
    assert rest is None
    assert first == dict(payload or {})


def test_a_batch_of_one_is_worked_as_its_event() -> None:
    first, rest = event_batching.split_batch(_batch_of(1))
    assert rest is None
    assert first == _case_event("c0")


def test_the_remainder_run_keeps_the_ordering_key_and_drops_the_window_and_dedup() -> None:
    _, rest = event_batching.split_batch(_batch_of(3))
    assert rest is not None
    batch_context = {
        "type": "platform_event",
        "event": "ctem_case.opened",
        "event_id": "evt-c0",
        "object_id": "c0",
        "ordering_key": "ctem:f1",
        event_batching.WINDOW_CONTEXT: 60,
        "dedup_key": "platform_event:evt-c0",
        "resumes": [{"attempt": 2}],
    }
    context = event_batching.remainder_context(batch_context, rest, "run-1")
    assert context["ordering_key"] == "ctem:f1"
    assert context["type"] == "platform_event"
    assert event_batching.WINDOW_CONTEXT not in context
    assert "dedup_key" not in context
    assert "resumes" not in context
    assert context["event_id"] == "evt-c1"
    assert context["object_id"] == "c1"
    assert context[event_batching.SPLIT_FROM_CONTEXT] == "run-1"


def test_the_split_is_opt_in_and_judged_at_save() -> None:
    assert not event_batching.splits({})
    assert not event_batching.splits(None)
    assert not event_batching.splits({"batch_split": "yes"})
    assert event_batching.splits({"batch_split": True})
    base = {"batch_window_seconds": 60, "batch_events": ["ctem_case.opened"]}
    assert event_batching.window_error("platform_event", {**base, "batch_split": True}) is None
    assert event_batching.window_error("platform_event", {**base, "batch_split": "true"}) is not None


def test_the_seeded_case_listeners_split_their_batches() -> None:
    from app.agents import builtin_agents

    for assignment in (builtin_agents.CTEM_F1_CASE_ASSIGNMENT, builtin_agents.CTEM_F2_CASE_ASSIGNMENT):
        config = assignment["trigger_config"]
        assert event_batching.splits(config)
        assert event_batching.window_error("platform_event", config) is None
        # Only opened cases batch: reopened, SLA breach, validation, retest and stall run alone at once.
        for name in (
            "ctem_case.reopened",
            "ctem_case.sla_breached",
            "ctem_case.validation_finished",
            "ctem_case.retest_requested",
            "ctem_case.stalled",
        ):
            assert event_batching.window_seconds(config, {"type": "platform_event", "event": name}) == 0
        prompt = assignment["prompt"]
        assert "one case" in prompt
        assert "own work item" in prompt
        assert "events list" not in prompt


def test_the_run_splits_before_it_is_marked_running_and_never_on_a_resume() -> None:
    source = inspect.getsource(assignment_engine.execute_assignment_run)
    split = source.index("await _split_batch_run(db, assignment, run)")
    running = source.index('run.status = "running"')
    assert split < running
    guard = source.rfind("if not _resuming", 0, split)
    assert guard != -1 and split - guard < 120


def test_the_split_queues_the_rest_under_the_row_lock_with_the_batch_run_identity() -> None:
    source = inspect.getsource(assignment_engine._split_batch_run)
    assert "event_batching.splits(assignment.trigger_config)" in source
    assert "with_for_update()" in source
    assert "status=QUEUED_RUN_STATUS" in source
    assert "user_id=run.user_id" in source
    assert "event_batching.remainder_context(" in source
    assert "run.trigger_payload = first" in source
    # The rest never runs beside the batch run: both carry one ordering key.
    assert "event_batching.split_ordering_key(str(run.id))" in source
    assert source.index("split_ordering_key") < source.index("remainder_context(")


def test_a_chain_of_rests_keeps_the_first_batch_run_ordering_key() -> None:
    key = event_batching.split_ordering_key("run-1")
    _, rest = event_batching.split_batch(_batch_of(3))
    assert rest is not None
    ctx1 = event_batching.remainder_context({"type": "platform_event", "ordering_key": key}, rest, "run-1")
    _, rest2 = event_batching.split_batch(rest)
    assert rest2 is not None
    ctx2 = event_batching.remainder_context(ctx1, rest2, "run-2")
    assert ctx1["ordering_key"] == ctx2["ordering_key"] == key


def test_five_split_contexts_cost_the_sum_of_five_cases_alone() -> None:
    """The token model of #2379: a context re-sends what it holds with each call.

    One conversation working five cases sends case k with the results of cases
    1..k-1 (the measured 18.1M against ~1.2M per case). Split, each run holds
    one case: the total is the sum of the five run alone, within 1.3x.
    """
    per_case = 1_200_000

    def shared_context_total(n: int) -> int:
        return sum(per_case * (k + 1) for k in range(n))

    worked = _contexts(_batch_of(5))
    split_total = sum(per_case * len(event_batching.events_of(p)) for p in worked)
    alone_total = 5 * per_case
    assert split_total <= 1.3 * alone_total
    assert shared_context_total(5) > 1.3 * alone_total
