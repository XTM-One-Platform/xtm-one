"""Tests for the scorer registry: the judge toggle and selection validation."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from app.services.evaluation.scorers.registry import invalid_keys, judge_enabled
from tests.evaluation_helpers import HeadlinesFake, selects_run_status

# ── Judge gating (single source of truth) ────────────────────────────────


def test_judge_enabled_semantics():
    # None (unset) → judge on for backward compatibility.
    assert judge_enabled(None) is True
    # Explicit selection: on only when it contains "judge".
    assert judge_enabled(["judge"]) is True
    # Judge-off stays representable via an explicit empty/judge-less selection.
    assert judge_enabled([]) is False
    assert judge_enabled(["something_else"]) is False


# ── Validation ──────────────────────────────────────────────────────────


def test_invalid_keys():
    assert invalid_keys(None) == []
    assert invalid_keys(["judge"]) == []
    assert invalid_keys(["judge", "bogus", "nope"]) == ["bogus", "nope"]
    assert invalid_keys(["token_f1", "structured_output"]) == [
        "token_f1",
        "structured_output",
    ]


# ── Rescore on stored predictions (deterministic-only path) ──────────────


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return list(self._rows)

    def scalar_one_or_none(self):
        # No extractor configured for this agent.
        return None


class _FakeDb:
    """Minimal async DB stub for the deterministic rescore path."""

    def __init__(self, objects, results):
        # objects: {(model_name, id): obj}
        self._objects = objects
        self._results = results
        self.committed = False
        # Rescore logs metric rows for every result it touches, judge or not.
        self.added = []

    def add(self, obj):
        self.added.append(obj)

    async def get(self, model, obj_id):
        return self._objects.get((model.__name__, obj_id))

    async def execute(self, _stmt):
        if selects_run_status(_stmt):
            # The run the rescore owns reads ``running``.
            return SimpleNamespace(scalar_one_or_none=lambda: "running")
        return _FakeResult(self._results)

    async def commit(self):
        self.committed = True

    async def rollback(self):
        self.rolled_back = True


async def test_rescore_run_deterministic_recomputes_without_agent_execution(monkeypatch):
    from app.models.dataset import DatasetEntry
    from app.models.evaluation import Evaluation, EvaluationRun
    from app.services import evaluation_engine

    # Never invoke the LLM judge on the token_f1-only path.
    async def _boom(*_a, **_k):  # pragma: no cover - must not be called
        raise AssertionError("score_result should not run when judge is deselected")

    monkeypatch.setattr(evaluation_engine, "score_result", _boom)

    headlines = HeadlinesFake()
    monkeypatch.setattr(evaluation_engine, "_compute_run_headlines", headlines)

    run_id = uuid.uuid4()
    eval_id = uuid.uuid4()
    entry_id = uuid.uuid4()

    # As the rescore endpoints leave it.
    run = SimpleNamespace(id=run_id, evaluation_id=eval_id, agent_id=uuid.uuid4(), status="running")
    evaluation = SimpleNamespace(
        scorer_selection=["token_f1"],
        evaluator_model=None,
        evaluator_prompt=None,
        evaluation_instructions=None,
        sub_score_weights=None,
        default_output_schema=None,
    )
    entry = SimpleNamespace(
        id=entry_id,
        dataset_id=None,
        expected_answer="the quick brown fox",
        input_prompt="q",
        evaluation_prompt=None,
        output_schema=None,
    )
    result_row = SimpleNamespace(
        id=uuid.uuid4(),
        run_id=run_id,
        entry_id=entry_id,
        version_entry_id=None,
        agent_output="the quick brown fox",
        score=10.0,
        f1_score=42.0,
        execution_anomaly_kind=None,
        execution_details={"execution_mode": "full_loop", "traces": []},
    )

    db = _FakeDb(
        objects={
            (EvaluationRun.__name__, run_id): run,
            (Evaluation.__name__, eval_id): evaluation,
            (DatasetEntry.__name__, entry_id): entry,
        },
        results=[result_row],
    )

    n = await evaluation_engine.rescore_run(db, str(run_id), previous_status="completed")
    aggregated = headlines.calls[-1]

    assert n == 1
    # Token-F1 is retired: a rescore neither recomputes nor clears the legacy
    # value. A run with no ``family`` produces no package quality.
    assert result_row.f1_score == 42.0
    # Judge deselected → prior judge score untouched (no LLM call).
    assert result_row.score == 10.0
    assert db.committed is True
    assert aggregated["batch_run_ids"] == [str(run_id)]
    # require_completed=False: the background rescore has not flipped the
    # run to "completed" yet at this point.
    assert aggregated["require_completed"] is False
    # Judge deselected, so there is no judge timing to log -- and Run Duration on
    # a rescore is the judge alone, so it is absent rather than zero.
    assert {m.key for m in db.added} & {"judge_seconds", "run_duration_seconds"} == set()


async def test_rescore_run_raises_when_no_predictions(monkeypatch):
    from app.models.evaluation import Evaluation, EvaluationRun
    from app.services import evaluation_engine

    run_id = uuid.uuid4()
    eval_id = uuid.uuid4()
    # As the rescore endpoints leave it.
    run = SimpleNamespace(id=run_id, evaluation_id=eval_id, agent_id=uuid.uuid4(), status="running")
    evaluation = SimpleNamespace(scorer_selection=None)

    db = _FakeDb(
        objects={
            (EvaluationRun.__name__, run_id): run,
            (Evaluation.__name__, eval_id): evaluation,
        },
        results=[],
    )

    with pytest.raises(ValueError, match="no_predictions"):
        await evaluation_engine.rescore_run(db, str(run_id), previous_status="completed")
