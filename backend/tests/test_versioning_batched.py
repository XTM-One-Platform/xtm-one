"""Batched-query regression tests for the versioning helpers.

These tests pin the fix for an N+1 query:

- ``get_active_agent_persona_version_numbers`` and
  ``get_active_agent_config_version_numbers`` must issue a single
  ``GROUP BY``-style query regardless of how many agents are passed in.
- ``batch_compute_run_replayability`` must issue at most one existence
  query per snapshot type (persona, config, prompt) regardless of how
  many runs are passed in.

Together they keep ``GET /evaluations/{id}/runs`` (page size 100) and
the scheduled-evaluation creation path off the per-agent / per-run
fan-out the per-record helpers would otherwise produce.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

import pytest

from app.services import (
    agent_config_versioning as cfg_svc,
)
from app.services import (
    evaluation_engine as ee,
)
from app.services import (
    prompt_versioning as prompt_svc,
)
from app.services.prompt_versioning import DEFAULT_PERSONA_SNAPSHOT_MARKER


class _StubResult:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def all(self) -> list[Any]:
        return list(self._rows)


class _RecordingDb:
    """Records every ``execute`` call so tests can assert query count."""

    def __init__(self, queued_rows: list[list[Any]]) -> None:
        # FIFO queue of result rows per call to ``execute``.
        self._queued = list(queued_rows)
        self.executed: list[Any] = []

    async def execute(self, statement: Any) -> _StubResult:
        self.executed.append(statement)
        if not self._queued:
            return _StubResult([])
        return _StubResult(self._queued.pop(0))


@pytest.mark.asyncio
async def test_persona_versions_batched_single_query():
    """One ``execute`` call regardless of the agent count."""
    agent_a = uuid.uuid4()
    agent_b = uuid.uuid4()
    agent_c = uuid.uuid4()

    db = _RecordingDb(
        [
            [(agent_a, 3), (agent_b, 1), (agent_c, 7)],
        ]
    )

    result = await prompt_svc.get_active_agent_persona_version_numbers(
        db, agent_ids=[agent_a, agent_b, agent_c]
    )

    assert len(db.executed) == 1
    assert result == {str(agent_a): 3, str(agent_b): 1, str(agent_c): 7}


@pytest.mark.asyncio
async def test_persona_versions_batched_empty_agent_ids():
    db = _RecordingDb([])

    result = await prompt_svc.get_active_agent_persona_version_numbers(
        db, agent_ids=[]
    )

    assert result == {}
    assert db.executed == []


@pytest.mark.asyncio
async def test_persona_versions_batched_skips_null_rows():
    """``GROUP BY`` can yield ``(None, None)`` for agents with no version
    rows; the helper must filter those out."""
    agent_a = uuid.uuid4()
    db = _RecordingDb(
        [
            [(agent_a, 2), (None, None), (agent_a, None)],
        ]
    )

    result = await prompt_svc.get_active_agent_persona_version_numbers(
        db, agent_ids=[agent_a]
    )

    assert result == {str(agent_a): 2}


@pytest.mark.asyncio
async def test_config_versions_batched_single_query():
    """One ``execute`` call regardless of the agent and field counts."""
    agent_a = uuid.uuid4()
    agent_b = uuid.uuid4()

    db = _RecordingDb(
        [
            [
                (agent_a, "guardrails", 4),
                (agent_a, "temperature", 2),
                (agent_b, "max_iterations", 9),
            ]
        ]
    )

    result = await cfg_svc.get_active_agent_config_version_numbers(
        db, agent_ids=[agent_a, agent_b]
    )

    assert len(db.executed) == 1
    assert result == {
        str(agent_a): {"guardrails": 4, "temperature": 2},
        str(agent_b): {"max_iterations": 9},
    }


@pytest.mark.asyncio
async def test_config_versions_batched_filters_unknown_field_names():
    """Rows for field names outside ``TRACKED_AGENT_CONFIG_FIELDS`` must
    be discarded so callers can rely on the helper's shape contract."""
    agent_a = uuid.uuid4()

    db = _RecordingDb(
        [
            [
                (agent_a, "guardrails", 4),
                (agent_a, "totally-unknown-field", 99),
            ]
        ]
    )

    result = await cfg_svc.get_active_agent_config_version_numbers(
        db, agent_ids=[agent_a]
    )

    assert result == {str(agent_a): {"guardrails": 4}}


@pytest.mark.asyncio
async def test_config_versions_batched_empty_agent_ids():
    db = _RecordingDb([])

    result = await cfg_svc.get_active_agent_config_version_numbers(
        db, agent_ids=[]
    )

    assert result == {}
    assert db.executed == []


@pytest.mark.asyncio
async def test_batch_compute_run_replayability_no_queries_for_empty_input():
    db = _RecordingDb([])

    result = await ee.batch_compute_run_replayability(db, [])

    assert result == {}
    assert db.executed == []


@pytest.mark.asyncio
async def test_batch_compute_run_replayability_issues_one_query_per_snapshot_type():
    """Three runs, mixed persona/config/prompt refs — total of exactly
    three ``execute`` calls (one per snapshot type)."""
    agent_a = uuid.uuid4()
    agent_b = uuid.uuid4()
    prompt_x = uuid.uuid4()
    prompt_y = uuid.uuid4()

    run1 = SimpleNamespace(
        id=uuid.uuid4(),
        agent_id=agent_a,
        persona_version_number=2,
        agent_config_versions={"guardrails": 1, "temperature": 3},
        prompt_versions={str(prompt_x): 4},
        skill_versions=None,
    )
    run2 = SimpleNamespace(
        id=uuid.uuid4(),
        agent_id=agent_b,
        persona_version_number=1,
        agent_config_versions={"max_iterations": 5},
        prompt_versions={str(prompt_y): 2, str(prompt_x): 4},
        skill_versions=None,
    )
    run3 = SimpleNamespace(
        id=uuid.uuid4(),
        agent_id=agent_a,
        persona_version_number=None,
        agent_config_versions=None,
        prompt_versions=None,
        skill_versions=None,
    )

    db = _RecordingDb(
        [
            # personas existing
            [(agent_a, 2), (agent_b, 1)],
            # configs existing
            [
                (agent_a, "guardrails", 1),
                (agent_a, "temperature", 3),
                (agent_b, "max_iterations", 5),
            ],
            # prompts existing
            [(prompt_x, 4), (prompt_y, 2)],
        ]
    )

    result = await ee.batch_compute_run_replayability(db, [run1, run2, run3])

    # The whole batch is satisfied with exactly 3 round-trips, not
    # O(runs * snapshot_refs).
    assert len(db.executed) == 3
    # Every run is replayable (no missing snapshots).
    assert result == {run1.id: [], run2.id: [], run3.id: []}


@pytest.mark.asyncio
async def test_batch_compute_run_replayability_flags_missing_snapshots():
    """When a persona/config/prompt snapshot has been deleted, the
    corresponding run's ``missing`` list must include it in the same
    human-readable format the per-run resolver produces."""
    agent_a = uuid.uuid4()
    prompt_x = uuid.uuid4()

    run = SimpleNamespace(
        id=uuid.uuid4(),
        agent_id=agent_a,
        persona_version_number=2,
        agent_config_versions={"guardrails": 7},
        prompt_versions={str(prompt_x): 1, "not-a-uuid": 3},
        skill_versions=None,
    )

    db = _RecordingDb(
        [
            # personas — only v1 exists for agent_a, v2 is missing.
            [(agent_a, 1)],
            # configs — agent_a has no config rows at all.
            [],
            # prompts — only v2 exists for prompt_x; v1 is missing.
            [(prompt_x, 2)],
        ]
    )

    result = await ee.batch_compute_run_replayability(db, [run])

    missing = result[run.id]
    assert "persona v2" in missing
    assert "guardrails v7" in missing
    assert f"prompt {prompt_x} v1" in missing
    # Verbatim-invalid prompt id is surfaced as-is at the end.
    assert "prompt not-a-uuid v3" in missing
    assert missing.index("prompt not-a-uuid v3") == len(missing) - 1
    # Three queries regardless of how many bad refs the runs contain.
    assert len(db.executed) == 3


@pytest.mark.asyncio
async def test_batch_compute_run_replayability_handles_invalid_int_values():
    """Non-integer version values must be surfaced as verbatim missing
    entries rather than crashing the batch."""
    agent_a = uuid.uuid4()

    run = SimpleNamespace(
        id=uuid.uuid4(),
        agent_id=agent_a,
        persona_version_number=None,
        agent_config_versions={"guardrails": "not-an-int"},
        prompt_versions=None,
        skill_versions=None,
    )

    # The run's only versioned reference (``guardrails: "not-an-int"``)
    # fails the int-coercion step inside ``batch_compute_run_replayability``,
    # so no ``(agent_id, field_name, version_number)`` tuple ends up in
    # ``config_lookups``. With nothing to look up, no DB query is issued
    # at all — the recording-DB queue is therefore empty, NOT a queued
    # empty-result for a query that never runs (the earlier wording
    # contradicted the ``len(db.executed) == 0`` assertion below).
    db = _RecordingDb([])

    result = await ee.batch_compute_run_replayability(db, [run])
    assert result[run.id] == ["guardrails vnot-an-int"]
    # Zero queries since no parseable references exist anywhere.
    assert len(db.executed) == 0


@pytest.mark.asyncio
async def test_batch_compute_run_replayability_accepts_null_config_markers():
    """A ``null`` config snapshot marker means "explicitly unset" and
    must not be reported as missing."""
    agent_a = uuid.uuid4()
    run = SimpleNamespace(
        id=uuid.uuid4(),
        agent_id=agent_a,
        persona_version_number=None,
        agent_config_versions={"max_iterations": None},
        prompt_versions=None,
        skill_versions=None,
    )

    db = _RecordingDb([])
    result = await ee.batch_compute_run_replayability(db, [run])

    assert result[run.id] == []
    assert db.executed == []


@pytest.mark.asyncio
async def test_resolve_agent_version_snapshots_uses_constant_round_trips():
    """``resolve_agent_version_snapshots`` must issue a constant number of
    DB round-trips regardless of the number of agents:

    1. ``Agent.prompt_ids`` + ``Agent.skill_ids`` (composite ``IN``)
    2. ``get_active_agent_persona_version_numbers`` (single ``GROUP BY``)
    3. ``get_active_prompt_version_numbers`` (single ``GROUP BY``)
    4. ``get_active_agent_config_version_numbers`` (single ``GROUP BY``)
    5. ``get_active_skill_version_numbers`` (single ``GROUP BY``) — skipped
       entirely when no agent has a skill attached, as here.

    The previous singular helper issued
    ``1 + 1 + 1 + len(TRACKED_AGENT_CONFIG_FIELDS)`` round-trips PER
    agent, which became prohibitive when triggering evaluations for
    many agents at once.  This test pins the new constant-call-count
    contract so a future regression is impossible to miss.
    """
    from app.services import agent_version_snapshots

    agent_a = uuid.uuid4()
    agent_b = uuid.uuid4()
    agent_c = uuid.uuid4()
    prompt_x = uuid.uuid4()
    prompt_y = uuid.uuid4()

    db = _RecordingDb(
        [
            # 1. Agent.prompt_ids batch query
            [
                (agent_a, [str(prompt_x), str(prompt_y)], None, "Persona A"),
                (agent_b, [str(prompt_x)], None, "Persona B"),
                (agent_c, None, None, None),  # no prompts / skills / persona
            ],
            # 2. persona max-versions (GROUP BY agent_id)
            [(str(agent_a), 3), (str(agent_b), 1)],
            # 3. prompt max-versions (GROUP BY prompt_id)
            [(str(prompt_x), 5), (str(prompt_y), 2)],
            # 4. config max-versions (GROUP BY agent_id, field_name)
            [
                (str(agent_a), "temperature", 4),
                (str(agent_b), "guardrails", 7),
            ],
        ]
    )

    result = await agent_version_snapshots.resolve_agent_version_snapshots(
        db, [agent_a, agent_b, agent_c]
    )

    # Constant round-trip count regardless of agent fan-out.
    assert len(db.executed) == 4, (
        f"expected exactly 4 DB calls (one per snapshot type + the "
        f"agent prompt_ids lookup), got {len(db.executed)}"
    )

    # Per-agent assembly correctness.
    assert result[agent_a] == (
        3,
        {str(prompt_x): 5, str(prompt_y): 2},
        {"temperature": 4},
        None,
    )
    assert result[agent_b] == (1, {str(prompt_x): 5}, {"guardrails": 7}, None)
    # agent_c had no persona configured, so the snapshot persists the
    # explicit default-persona marker instead of falling back to live
    # persona on replay.
    assert result[agent_c] == (DEFAULT_PERSONA_SNAPSHOT_MARKER, None, None, None)


@pytest.mark.asyncio
async def test_resolve_agent_version_snapshots_empty_input_short_circuits():
    """No agents → no queries, no result rows."""
    from app.services import agent_version_snapshots

    db = _RecordingDb([])

    result = await agent_version_snapshots.resolve_agent_version_snapshots(db, [])

    assert result == {}
    assert db.executed == []


@pytest.mark.asyncio
async def test_resolve_agent_version_snapshots_deduplicates_repeated_agents():
    """A list with the same agent_id repeated must NOT inflate the
    Agent.prompt_ids query payload — de-duplication happens up front.

    The helper short-circuits the prompt-versions query when no agent
    has any prompt_ids (saves a no-op DB round-trip), so we give the
    duplicated agent at least one prompt below to keep this test
    focused on the de-duplication contract rather than that
    optimization."""
    from app.services import agent_version_snapshots

    agent_a = uuid.uuid4()
    prompt_x = uuid.uuid4()

    db = _RecordingDb(
        [
            # prompt_ids + skill_ids — issued ONCE
            [(agent_a, [str(prompt_x)], None, "Persona A")],
            [],  # personas
            [],  # prompts
            [],  # configs
        ]
    )

    result = await agent_version_snapshots.resolve_agent_version_snapshots(
        db, [agent_a, agent_a, agent_a]
    )

    # Even though the agent appears 3x in the input, exactly 4 queries
    # (the constant set) run and the result has a single entry.
    assert len(db.executed) == 4
    assert list(result.keys()) == [agent_a]
    assert result[agent_a] == (None, None, None, None)


@pytest.mark.asyncio
async def test_resolve_agent_version_snapshots_includes_null_markers_for_unversioned_fields():
    """When tracked config fields are unset and have no version row, the
    snapshot must keep an explicit ``null`` marker per field so replay
    does not fall back to future agent values."""
    from app.services import agent_version_snapshots

    agent_a = uuid.uuid4()

    db = _RecordingDb(
        [
            # Agent row: id, prompt_ids, skill_ids, persona, guardrails,
            # temperature, reasoning_effort, max_iterations.
            [(agent_a, None, None, None, None, None, None, None)],
            [],  # personas
            [],  # prompts
            [],  # configs
        ]
    )

    result = await agent_version_snapshots.resolve_agent_version_snapshots(db, [agent_a])

    # Prompt-version lookup is skipped when no prompt IDs are attached.
    assert len(db.executed) == 3
    assert result[agent_a] == (
        DEFAULT_PERSONA_SNAPSHOT_MARKER,
        None,
        {
            "guardrails": None,
            "temperature": None,
            "reasoning_effort": None,
            "max_iterations": None,
        },
        None,
    )


@pytest.mark.asyncio
async def test_batch_compute_run_replayability_accepts_default_persona_marker():
    run = SimpleNamespace(
        id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        persona_version_number=DEFAULT_PERSONA_SNAPSHOT_MARKER,
        agent_config_versions=None,
        prompt_versions=None,
        skill_versions=None,
    )

    db = _RecordingDb([])
    result = await ee.batch_compute_run_replayability(db, [run])

    assert result[run.id] == []
    assert db.executed == []
