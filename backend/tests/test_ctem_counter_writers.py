"""One seeded writer per counter of ctem:f1:state and ctem:f2:state (#2250).

The monthly posture report reads these counters; a counter written by any run
that chooses to (the case listener runs up to five cases at once) skips or
double counts. Each counter is named with exactly one writer in the seeded
prompts and in the fleet persona, and every other listener is told never to
write the state.
"""

from __future__ import annotations

import pathlib
from typing import Any

import pytest

from app.agents.builtin_agents import (
    CTEM_CASE_ASSIGNMENT_NAME,
    CTEM_CASE_NEVER_WRITES_COUNTERS,
    CTEM_F1_ASSIGNMENTS,
    CTEM_F1_COUNTER_WRITERS,
    CTEM_F1_STATE_KEY,
    CTEM_F2_ASSIGNMENTS,
    CTEM_F2_COUNTER_WRITERS,
    CTEM_F2_STATE_KEY,
)

_PERSONAS = pathlib.Path(__file__).resolve().parents[1] / "app" / "agents" / "seed_data" / "agent_persona"
_FLEETS = [
    (CTEM_F1_ASSIGNMENTS, CTEM_F1_COUNTER_WRITERS, CTEM_F1_STATE_KEY, "ctem-threat-response-orchestrator.md"),
    (CTEM_F2_ASSIGNMENTS, CTEM_F2_COUNTER_WRITERS, CTEM_F2_STATE_KEY, "ctem-vuln-prioritization-orchestrator.md"),
]


def _by_name(assignments: list[dict[str, Any]], name: str) -> dict[str, Any]:
    return next(a for a in assignments if a["name"] == name)


@pytest.mark.parametrize(("assignments", "writers", "key", "persona"), _FLEETS)
def test_each_counter_is_named_with_its_state_key_in_its_one_writer(
    assignments: list[dict[str, Any]], writers: dict[str, str], key: str, persona: str
) -> None:
    for counter, writer in writers.items():
        prompt = _by_name(assignments, writer)["prompt"]
        assert key in prompt, writer
        assert counter in prompt, (writer, counter)
        assert "only writer" in prompt, writer


@pytest.mark.parametrize(("assignments", "writers", "key", "persona"), _FLEETS)
def test_no_other_assignment_writes_a_counter(
    assignments: list[dict[str, Any]], writers: dict[str, str], key: str, persona: str
) -> None:
    owners = set(writers.values())
    for a in assignments:
        if a["name"] in owners:
            continue
        prompt = a["prompt"]
        for counter in writers:
            assert counter not in prompt, (a["name"], counter)
        assert "bump the counters" not in prompt, a["name"]


@pytest.mark.parametrize(("assignments", "writers", "key", "persona"), _FLEETS)
def test_case_listener_never_writes_the_state(
    assignments: list[dict[str, Any]], writers: dict[str, str], key: str, persona: str
) -> None:
    prompt = _by_name(assignments, CTEM_CASE_ASSIGNMENT_NAME)["prompt"]
    assert CTEM_CASE_NEVER_WRITES_COUNTERS in prompt
    assert key in CTEM_CASE_NEVER_WRITES_COUNTERS


def test_per_case_writers_dedupe_on_the_case_id() -> None:
    for assignments in (CTEM_F1_ASSIGNMENTS, CTEM_F2_ASSIGNMENTS):
        prompt = _by_name(assignments, "Re-test due")["prompt"]
        assert "note names the case id" in prompt
        assert "add nothing" in prompt


def test_f2_triage_listeners_never_write_the_state() -> None:
    owners = set(CTEM_F2_COUNTER_WRITERS.values()) | {CTEM_CASE_ASSIGNMENT_NAME}
    triage = [a for a in CTEM_F2_ASSIGNMENTS if a["name"] not in owners and "record_ctem_signal" in a["prompt"]]
    assert triage
    for a in triage:
        assert CTEM_CASE_NEVER_WRITES_COUNTERS in a["prompt"], a["name"]


@pytest.mark.parametrize(("assignments", "writers", "key", "persona"), _FLEETS)
def test_persona_names_each_counter_with_its_writer(
    assignments: list[dict[str, Any]], writers: dict[str, str], key: str, persona: str
) -> None:
    text = (_PERSONAS / persona).read_text(encoding="utf-8")
    line = next(line for line in text.splitlines() if f"`{key}`" in line)
    for counter, writer in writers.items():
        assert f"`{counter}`" in line, counter
        assert writer in line, writer
    assert CTEM_CASE_ASSIGNMENT_NAME in line
