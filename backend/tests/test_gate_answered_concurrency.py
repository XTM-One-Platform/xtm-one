"""Seeded "Gate answered" listeners run answers of different decisions side by side (#1649)."""

from __future__ import annotations

from app.agents.builtin_agents import BUILTIN_ASSIGNMENTS
from app.agents.gate_answered import (
    GATE_ANSWERED,
    GATE_ANSWERED_CONCURRENCY,
    GATE_ANSWERED_ORDERING_KEY,
)
from app.services.assignment_dispatch import (
    ORDERING_KEY_CONTEXT,
    assignment_concurrency_cap,
    resolve_ordering_key,
    seeded_trigger_config,
    with_ordering_key,
)


def _gate_assignments() -> list[dict]:
    found = [a for a in BUILTIN_ASSIGNMENTS if a["name"] == GATE_ANSWERED]
    assert found
    return found


def test_the_seed_keeps_the_listener_cap_over_the_one_run_event_default() -> None:
    assert GATE_ANSWERED_CONCURRENCY > 1
    for data in _gate_assignments():
        written = seeded_trigger_config(data["trigger_type"], data["trigger_config"])
        assert assignment_concurrency_cap(written) == GATE_ANSWERED_CONCURRENCY, data["agent_slug"]
        assert written["ordering_key"] == GATE_ANSWERED_ORDERING_KEY, data["agent_slug"]


def test_answers_are_serialized_per_decision_key_and_parallel_across_keys() -> None:
    config = seeded_trigger_config("platform_event", _gate_assignments()[0]["trigger_config"])
    one = {"correlation_key": "ctem:validation:case-1", "object_type": "work_item", "object_id": "a"}
    same = {**one, "object_id": "b"}
    other = {**one, "correlation_key": "ctem:validation:case-2"}
    assert resolve_ordering_key(config, one) == resolve_ordering_key(config, same) == "ctem:validation:case-1"
    assert resolve_ordering_key(config, other) != resolve_ordering_key(config, one)
    # The configured key wins over the object key the platform event context carries.
    context = with_ordering_key(config, one, {"type": "platform_event", ORDERING_KEY_CONTEXT: "work_item:a"})
    assert context is not None and context[ORDERING_KEY_CONTEXT] == "ctem:validation:case-1"


def test_an_existing_row_is_backfilled_by_the_seed_rewrite() -> None:
    # The seed rewrites trigger_config of an existing row from the seed data at every
    # boot (seed.seed_builtin_assignments); a row written with the one-run default
    # therefore takes the listener's cap at the next boot.
    import inspect

    from app.agents import seed

    source = inspect.getsource(seed.seed_builtin_assignments)
    assert 'existing.trigger_config = {\n                **seeded_trigger_config(data["trigger_type"]' in source
