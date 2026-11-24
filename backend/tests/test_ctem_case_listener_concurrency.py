"""Seeded CTEM case and reminder listeners run different cases side by side (#1923)."""

from __future__ import annotations

from typing import Any

from app.agents.builtin_agents import (
    BUILTIN_ASSIGNMENTS,
    CTEM_CASE_ASSIGNMENT_NAME,
    CTEM_CASE_LISTENER_CONCURRENCY,
    CTEM_CASE_ORDERING_KEY,
    CTEM_LOOP_REMINDER,
)
from app.services.assignment_dispatch import (
    ORDERING_KEY_CONTEXT,
    assignment_concurrency_cap,
    resolve_ordering_key,
    seeded_trigger_config,
    with_ordering_key,
)


def _seeded(name: str) -> list[dict[str, Any]]:
    found = [a for a in BUILTIN_ASSIGNMENTS if a["name"] == name]
    assert found, name
    return found


def test_the_seed_keeps_the_case_listener_cap_over_the_one_run_event_default() -> None:
    assert CTEM_CASE_LISTENER_CONCURRENCY > 1
    for name in (CTEM_CASE_ASSIGNMENT_NAME, CTEM_LOOP_REMINDER):
        for data in _seeded(name):
            written = seeded_trigger_config(data["trigger_type"], data["trigger_config"])
            assert assignment_concurrency_cap(written) == CTEM_CASE_LISTENER_CONCURRENCY, (name, data["agent_slug"])
            assert written.get("ordering_key"), (name, data["agent_slug"])


def test_case_events_are_serialized_per_case_and_parallel_across_cases() -> None:
    for data in _seeded(CTEM_CASE_ASSIGNMENT_NAME):
        config = seeded_trigger_config(data["trigger_type"], data["trigger_config"])
        assert config["ordering_key"] == CTEM_CASE_ORDERING_KEY
        one = {"case_id": "case-1", "object_type": "ctem_case", "object_id": "case-1", "state": "open"}
        again = {**one, "state": "in_progress"}
        other = {**one, "case_id": "case-2", "object_id": "case-2"}
        assert resolve_ordering_key(config, one) == resolve_ordering_key(config, again) == "case-1"
        assert resolve_ordering_key(config, other) != resolve_ordering_key(config, one)
        context = with_ordering_key(config, one, {"type": "platform_event"})
        assert context is not None and context[ORDERING_KEY_CONTEXT] == "case-1"


def test_reminders_are_ordered_by_their_case_timer_key() -> None:
    (data,) = _seeded(CTEM_LOOP_REMINDER)
    config = seeded_trigger_config(data["trigger_type"], data["trigger_config"])
    one = {"correlation_key": "ctem-loop-reminder:case-1", "payload": {}}
    other = {"correlation_key": "ctem-loop-reminder:case-2", "payload": {}}
    assert resolve_ordering_key(config, one) == "ctem-loop-reminder:case-1"
    assert resolve_ordering_key(config, other) != resolve_ordering_key(config, one)
