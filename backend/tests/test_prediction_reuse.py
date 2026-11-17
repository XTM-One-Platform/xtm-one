"""Prediction reuse: the input fingerprint, when a stored result may be reused, and how reuse shows.
"""

from __future__ import annotations

import contextlib
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from app.api.v1 import evaluations as evaluations_api
from app.api.v1.schemas.evaluation import (
    CreateEvaluationRequest,
    EvaluationRunResponse,
)
from app.models.agent import Agent
from app.models.dataset import Dataset, DatasetEntry
from app.models.dataset_version import DatasetVersion, DatasetVersionEntry
from app.models.evaluation import Evaluation, EvaluationResult, EvaluationRun
from app.models.flow import Flow
from app.models.user import User
from app.services import evaluation_engine as ee
from app.services.evaluation.fingerprint import (
    compute_input_fingerprint,
    is_result_reusable,
)
from app.services.evaluation.run_mode import FALLBACK_MODE
from tests.evaluation_helpers import rescore_as_started, run_on_postgres
from tests.scratch_database import scratch_database

# ── Fingerprint and reuse eligibility ─────────────────────────────────────────────────


_FP_INPUT = dict(
    agent_id="11111111-1111-1111-1111-111111111111",
    model_id="gpt-4o",
    execution_mode="full_loop",
    entry_input="What is the capital of France?",
    entry_file_ids=[],
    persona_version=3,
    prompt_versions={"p1": 2},
    agent_config_versions={"tools": 5},
    kb_state=[["kb1", 4, "text-embedding-3-small"]],
)


def _fp(**overrides):
    return compute_input_fingerprint(**{**_FP_INPUT, **overrides})


_GUARD_FINGERPRINT_INPUT = dict(
    agent_id="00000000-0000-0000-0000-000000005202",
    model_id="gpt-test",
    entry_input="What is 2+2?",
    persona_version=3,
    kb_state=[["kb", 1, "emb"]],
)


# ── What the fingerprint reads ──────────────────────────────────────────


@pytest.mark.parametrize(
    "override",
    [
        pytest.param({"entry_input": "Different question"}, id="entry_input"),
        pytest.param({"model_id": "claude-3-5-sonnet"}, id="model"),
        pytest.param({"agent_id": "22222222-2222-2222-2222-222222222222"}, id="agent"),
        pytest.param({"persona_version": 4}, id="persona_version"),
        pytest.param({"prompt_versions": {"p1": 3}}, id="prompt_version"),
        pytest.param({"agent_config_versions": {"tools": 6}}, id="config_version"),
        pytest.param({"entry_file_ids": ["f9"]}, id="file_ids"),
        pytest.param({"kb_state": [["kb1", 5, "text-embedding-3-small"]]}, id="kb_revision"),
        pytest.param({"kb_state": [["kb1", 4, "text-embedding-3-large"]]}, id="kb_embedding_model"),
        # An answer stored under the old, lower cap is not one produced under the floor.
        pytest.param({"floored_max_iterations": 15}, id="cap_raised_by_the_floor"),
        # An answer produced under the guard or behind the gate is never reused without it.
        pytest.param({"apply_prompt_guards": True}, id="prompt_guard_on"),
        pytest.param({"require_tool_approval": True}, id="tool_approval_on"),
    ],
)
def test_a_full_loop_input_that_changes_the_answer_changes_the_fingerprint(override):
    assert _fp(**override) != _fp()


def test_an_answer_produced_behind_the_gate_is_never_reused_without_it():
    """The two approval settings never share a stored answer in Full Loop, alone or with the guard setting."""
    off = compute_input_fingerprint(execution_mode="full_loop", **_GUARD_FINGERPRINT_INPUT)
    on = compute_input_fingerprint(
        execution_mode="full_loop", require_tool_approval=True, **_GUARD_FINGERPRINT_INPUT
    )
    guarded = compute_input_fingerprint(
        execution_mode="full_loop", apply_prompt_guards=True, **_GUARD_FINGERPRINT_INPUT
    )
    both = compute_input_fingerprint(
        execution_mode="full_loop", apply_prompt_guards=True, require_tool_approval=True, **_GUARD_FINGERPRINT_INPUT
    )
    assert len({off, on, guarded, both}) == 4


@pytest.mark.parametrize(
    ("first", "second"),
    [
        pytest.param(
            {"prompt_versions": {"a": 1, "b": 2}, "agent_config_versions": {"x": 1, "y": 2}},
            {"prompt_versions": {"b": 2, "a": 1}, "agent_config_versions": {"y": 2, "x": 1}},
            id="dict_ordering",
        ),
        pytest.param(
            {"entry_file_ids": ["f1", "f2", "f3"]},
            {"entry_file_ids": ["f3", "f1", "f2"]},
            id="file_id_ordering",
        ),
    ],
)
def test_the_fingerprint_does_not_depend_on_input_order(first, second):
    assert _fp(**first) == _fp(**second)


@pytest.mark.parametrize(
    "override",
    [
        # llm_only consults no knowledge base, so a KB change must not invalidate reuse.
        pytest.param({"kb_state": [["kb", 99, "other"]]}, id="kb_state"),
        # llm_only runs no loop, so the cap cannot change its answer.
        pytest.param({"floored_max_iterations": 15}, id="floored_cap"),
        # llm_only runs no guard check and no approval gate.
        pytest.param({"apply_prompt_guards": True}, id="prompt_guard"),
        pytest.param({"require_tool_approval": True}, id="tool_approval"),
    ],
)
def test_llm_only_ignores_the_full_loop_only_inputs(override):
    base = {"execution_mode": "llm_only", **_GUARD_FINGERPRINT_INPUT}
    assert (
        compute_input_fingerprint(**{**base, **override})
        == compute_input_fingerprint(**base)
        == "c987f41f0f1de2a2168cbd7c62a377359e9284bebabd3a6ecd02d6d92d249ed2"
    )


@pytest.mark.parametrize(
    ("base", "setting", "stored_digest"),
    [
        # Unaffected agents keep reusing their answers.
        pytest.param(
            _FP_INPUT,
            {"floored_max_iterations": None},
            "47f3195128d742f0d258f52ff358daccf4b02939dc407451115b6c98ecff8c3d",
            id="cap_the_floor_did_not_raise",
        ),
        # Answers stored before the setting ran without the answer check.
        pytest.param(
            {"execution_mode": "full_loop", **_GUARD_FINGERPRINT_INPUT},
            {"apply_prompt_guards": False},
            "c9478c6b6d042ce9a8efa684e198f482c210840c8fb860469d1423fc482407f1",
            id="prompt_guard_off",
        ),
        # Answers stored before the setting keep their fingerprint under off.
        pytest.param(
            {"execution_mode": "full_loop", **_GUARD_FINGERPRINT_INPUT},
            {"require_tool_approval": False},
            "c9478c6b6d042ce9a8efa684e198f482c210840c8fb860469d1423fc482407f1",
            id="tool_approval_off",
        ),
        # An agent that is not a JSON producer, whatever its schema.
        pytest.param(
            {"execution_mode": "llm_only", **_GUARD_FINGERPRINT_INPUT},
            {"output_format": "markdown", "output_schema": {"type": "object"}},
            "c987f41f0f1de2a2168cbd7c62a377359e9284bebabd3a6ecd02d6d92d249ed2",
            id="not_a_json_producer",
        ),
    ],
)
def test_a_setting_left_off_keeps_the_fingerprint_stored_before_it(base, setting, stored_digest):
    # Update a digest only when the fingerprint changes on purpose: a new value
    # here means every stored answer stops being reused.
    assert (
        compute_input_fingerprint(**{**base, **setting})
        == compute_input_fingerprint(**base)
        == stored_digest
    )


_JSON_SCHEMA = {"type": "object", "properties": {"verdict": {"type": "string"}}}


@pytest.mark.parametrize("execution_mode", ["full_loop", "llm_only"])
def test_a_json_producer_never_reuses_an_answer_delivered_under_another_contract(execution_mode):
    """An answer stored before the delivery step, or under another schema, is not reused."""
    base = {"execution_mode": execution_mode, **_GUARD_FINGERPRINT_INPUT}
    stored_before = compute_input_fingerprint(**base)
    no_schema = compute_input_fingerprint(**base, output_format="json")
    with_schema = compute_input_fingerprint(**base, output_format="json", output_schema=_JSON_SCHEMA)
    other_schema = compute_input_fingerprint(
        **base, output_format="json", output_schema={"type": "array"}
    )
    assert len({stored_before, no_schema, with_schema, other_schema}) == 4


def test_fingerprint_handles_none_optional_fields():
    fp = compute_input_fingerprint(
        agent_id=uuid.uuid4(),
        model_id=None,
        execution_mode=None,
        entry_input=None,
    )
    assert len(fp) == 64


# ── Reuse validity ──────────────────────────────────────────────────────


def _pipeline_step(phase, outcome):
    """A pipeline tool call of a Skip LLM agent, as its timeline records it."""
    return {"type": "tool", "tool_name": "extract_ttp", "phase": phase, "outcome": outcome}


def _details(**over):
    d = {"traces": [], "execution_mode": "full_loop", "ttft_ms": 120}
    d.update(over)
    return d


@pytest.mark.parametrize(
    ("output", "details", "status", "kwargs", "reusable"),
    [
        pytest.param("A valid answer", _details(), "completed", {}, True, id="completed_answer"),
        pytest.param(
            "A valid answer", _details(), "completed", {"is_execution_anomaly": False}, True, id="explicit_no_anomaly"
        ),
        pytest.param("", _details(), "completed", {}, False, id="empty_output"),
        pytest.param("   ", _details(), "completed", {}, False, id="blank_output"),
        pytest.param(None, _details(), "completed", {}, False, id="no_output"),
        pytest.param("Error: boom", _details(), "completed", {}, False, id="error_prefix"),
        pytest.param("Context overflow: too big", _details(), "completed", {}, False, id="context_overflow_prefix"),
        # The engine's failure path leaves execution_details NULL.
        pytest.param("Some output", None, "completed", {}, False, id="no_execution_details"),
        pytest.param("Some output", {}, "completed", {}, False, id="empty_execution_details"),
        # An execution anomaly is not a deliverable, even with a non-empty
        # output: a fresh run may still deliver. Reuse reads only the boolean,
        # so every anomaly subtype is refused alike.
        pytest.param(
            "Which option would you like, A or B?",
            _details(),
            "completed",
            {"is_execution_anomaly": True},
            False,
            id="execution_anomaly",
        ),
        pytest.param("Some output", _details(), "failed", {}, False, id="failed_run"),
        pytest.param("Some output", _details(), "timed_out", {}, False, id="timed_out_run"),
        pytest.param("Some output", _details(), "running", {}, False, id="running_run"),
        # A full_loop entry answered in LLM Only after its run failed:
        # its fingerprint says full_loop, so reusing it would score the
        # persona's answer as the agent's.
        pytest.param(
            "An answer", _details(execution_mode=FALLBACK_MODE), "completed", {}, False, id="answered_by_a_fallback"
        ),
        pytest.param("An answer", _details(execution_mode="llm_only"), "completed", {}, True, id="llm_only_answer"),
        # A Skip LLM answer stored before a pipeline tool's refusal was left out: its
        # text is the tool's error, which a fixed configuration would no longer give.
        pytest.param(
            '{"error": "No API base URL configured for extract_ttp."}',
            _details(execution_mode="skip_llm", traces=[_pipeline_step("prepare", "error")]),
            "completed",
            {},
            False,
            id="a_pipeline_tool_refused",
        ),
        pytest.param(
            "The tool's error",
            _details(
                execution_mode="skip_llm",
                traces=[_pipeline_step("prepare", "success"), _pipeline_step("finalize", "error")],
            ),
            "completed",
            {},
            False,
            id="a_finalize_tool_failed",
        ),
        pytest.param(
            '{"objects": []}',
            _details(
                execution_mode="skip_llm",
                traces=[_pipeline_step("prepare", "success"), _pipeline_step("finalize", "success")],
            ),
            "completed",
            {},
            True,
            id="a_pipeline_answer",
        ),
    ],
)
def test_when_a_stored_result_is_reusable(output, details, status, kwargs, reusable):
    assert is_result_reusable(output, details, status, **kwargs) is reusable


class _AllResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _FakeReuseSession:
    """Async session that ignores the WHERE clause and returns canned
    ``(result, status)`` rows, so the in-loop check runs directly.
    """

    def __init__(self, rows):
        self._rows = rows

    async def execute(self, _query):
        return _AllResult(self._rows)


def _candidate(*, anomaly, output):
    return SimpleNamespace(
        id=uuid.uuid4(),
        agent_output=output,
        execution_details={"traces": [], "execution_mode": "full_loop"},
        execution_anomaly_kind="confirmation_seeking" if anomaly else None,
    )


@pytest.mark.parametrize(
    ("anomaly", "output", "clean_behind_it"),
    [
        # A flagged row is skipped whatever its subtype: the subtype never
        # changes reuse eligibility.
        pytest.param(True, "Which option would you like, A or B?", True, id="flagged_row"),
        pytest.param(True, "Done. Here is the report.", True, id="flagged_row_with_a_deliverable_output"),
        # Rows scored before the detector existed carry no anomaly kind and are
        # not backfilled; the in-loop ``detect_confirmation_seeking`` check
        # still rejects their confirmation-seeking output.
        pytest.param(False, "Which option would you like, A or B?", True, id="legacy_unflagged_row"),
        pytest.param(True, "Should I proceed with the extraction?", False, id="no_clean_row_behind_it"),
    ],
)
@pytest.mark.asyncio
async def test_find_reusable_prediction_skips_an_anomaly_source(anomaly, output, clean_behind_it):
    newest = _candidate(anomaly=anomaly, output=output)
    clean = _candidate(anomaly=False, output="Done. The STIX bundle is ready.")
    rows = [(newest, "completed")]
    if clean_behind_it:
        rows.append((clean, "completed"))
    picked = await ee._find_reusable_prediction(
        _FakeReuseSession(rows), uuid.uuid4(), uuid.uuid4(), "fingerprint"
    )
    assert picked is (clean if clean_behind_it else None)


_REUSE_TABLES = [
    User.__table__,
    Agent.__table__,
    Evaluation.__table__,
    Flow.__table__,
    EvaluationRun.__table__,
    # Referenced by ``evaluation_results``' foreign keys.
    Dataset.__table__,
    DatasetEntry.__table__,
    DatasetVersion.__table__,
    DatasetVersionEntry.__table__,
    EvaluationResult.__table__,
]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_the_lookup_skips_fallbacks_in_its_query_and_returns_the_valid_source_behind_them():
    """Fallbacks are excluded in SQL, not only in Python: five newer ones would fill the five-row window."""
    async with scratch_database("reusefallback", tables=_REUSE_TABLES) as db, db.sessions() as s:
        user = User(email=f"u-{uuid.uuid4().hex[:8]}@x.io", full_name="A", hashed_password="x", role="admin")
        s.add(user)
        await s.flush()
        agent = Agent(user_id=user.id, name="A", slug=f"a-{uuid.uuid4().hex[:6]}")
        evaluation = Evaluation(name="Eval")
        s.add_all([agent, evaluation])
        await s.flush()
        runs = [
            EvaluationRun(
                evaluation_id=evaluation.id, agent_id=agent.id, model_id="m", status="completed", trigger_reason="manual"
            )
            for _ in range(3)
        ]
        s.add_all(runs)
        await s.flush()
        start = datetime.now(UTC) - timedelta(hours=1)

        def _result(run, minutes, mode):
            return EvaluationResult(
                run_id=run.id,
                agent_output="An answer",
                score=90.0,
                execution_details={"traces": [], "execution_mode": mode},
                input_fingerprint="fp",
                created_at=start + timedelta(minutes=minutes),
            )

        valid = _result(runs[0], 0, "full_loop")
        fallbacks = [_result(runs[1], 1 + i, FALLBACK_MODE) for i in range(5)]
        unreadable = _result(runs[1], 10, FALLBACK_MODE)
        s.add_all([valid, *fallbacks, unreadable])
        await s.commit()
        # Written around the ORM, whose serializer would repair the lone surrogate:
        # PostgreSQL cannot read this row's mode, so Python decides it.
        await s.execute(
            text("UPDATE evaluation_results SET execution_details = CAST(:v AS json) WHERE id = :id"),
            {"v": '{"execution_mode": "llm_only_fallback", "x": "\\ud800"}', "id": unreadable.id},
        )
        await s.commit()

        picked = await ee._find_reusable_prediction(s, evaluation.id, runs[2].id, "fp")

        assert picked is not None and picked.id == valid.id


# ── Reuse visibility ──────────────────────────────────────────────────────────────────
# ``EvaluationRunResponse.reused_entries`` defaults to ``0``; ``_get_avg_sub_scores``
# counts reused and judge-failed results from a single query and keeps judge
# failures out of the averages; ``CreateEvaluationRequest.reuse_default`` defaults
# to ``"on"``.


@pytest.mark.integration
async def test_an_answer_stored_before_the_agent_became_a_json_producer_is_not_reused(monkeypatch):
    """An unchanged agent reuses its answer; the same agent turned JSON producer does not."""
    from sqlalchemy import select

    from app.models.dataset import EvaluationDataset
    from tests.evaluation_helpers import seed_provider
    from tests.scratch_database import ALL_TABLES

    calls: list[str] = []

    async def _agent(_db, _agent, _model_id, input_prompt, *_args, **_kwargs):
        calls.append(input_prompt)
        return "an answer", [], None, []

    async def _judge(*_args, **_kwargs):
        return {
            "score": 80.0,
            "sub_scores": {k: 80 for k in ee.SUB_SCORE_WEIGHTS},
            "evaluator_reasoning": "ok",
            "is_judge_failure": False,
        }

    async def _no_price_rows(_db, _model_id):
        return []

    monkeypatch.setattr(ee, "_execute_agent_llm_only", _agent)
    monkeypatch.setattr(ee, "score_result", _judge)
    monkeypatch.setattr(ee, "_build_run_price_rows", _no_price_rows)

    async with scratch_database("reusejson", tables=ALL_TABLES, extensions=("vector",)) as db:
        sm = db.sessions
        async with sm() as s:
            agent = Agent(name="Agent", slug=f"a-{uuid.uuid4().hex[:6]}")
            evaluation = Evaluation(name="Eval", execution_mode="llm_only", reuse_default="on")
            dataset = Dataset(name="ds")
            s.add_all([agent, evaluation, dataset])
            await s.flush()
            await seed_provider(s, ["gpt-4o"])
            s.add(EvaluationDataset(evaluation_id=evaluation.id, dataset_id=dataset.id))
            s.add(DatasetEntry(dataset_id=dataset.id, position=0, input_prompt="Question", expected_answer="a"))
            await s.commit()
            agent_id, evaluation_id = agent.id, evaluation.id

        async def _run() -> EvaluationResult:
            async with sm() as s:
                run = EvaluationRun(
                    evaluation_id=evaluation_id,
                    agent_id=agent_id,
                    model_id="gpt-4o",
                    trigger_reason="manual",
                    status="pending",
                )
                s.add(run)
                await s.commit()
                run_id = run.id
            async with sm() as s:
                await ee.execute_evaluation_run(s, str(run_id))
            async with sm() as s:
                return (
                    await s.execute(select(EvaluationResult).where(EvaluationResult.run_id == run_id))
                ).scalar_one()

        await _run()
        unchanged = await _run()
        async with sm() as s:
            stored_agent = await s.get(Agent, agent_id)
            stored_agent.output_format = "json"
            await s.commit()
        json_producer = await _run()

    assert unchanged.is_reused
    assert not json_producer.is_reused
    assert calls == ["Question", "Question"]


@pytest.mark.integration
async def test_an_answer_is_judged_against_the_same_tools_fresh_reused_and_rescored(monkeypatch):
    """Reuse and rescore give the judge the tools the agent had, as the fresh pass did."""
    from sqlalchemy import select

    from app.models.dataset import EvaluationDataset
    from tests.evaluation_helpers import seed_provider
    from tests.scratch_database import ALL_TABLES

    agent_calls: list[str] = []
    judged_with: list[list[str] | None] = []

    async def _agent(_db, _agent, _model_id, input_prompt, *_args, **_kwargs):
        agent_calls.append(input_prompt)
        # Had two tools, called neither.
        return "an answer", [], None, ["search", "fetch"], []

    async def _judge(*_args, **kwargs):
        judged_with.append(kwargs.get("available_tools"))
        return {
            "score": 80.0,
            "sub_scores": {k: 80 for k in ee.SUB_SCORE_WEIGHTS},
            "evaluator_reasoning": "ok",
            "is_judge_failure": False,
        }

    async def _no_price_rows(_db, _model_id):
        return []

    async def _no_tools(*_args, **_kwargs):
        return None

    monkeypatch.setattr(ee, "_execute_agent_full_loop", _agent)
    monkeypatch.setattr(ee, "_build_evaluation_integration_tools", _no_tools)
    monkeypatch.setattr(ee, "score_result", _judge)
    monkeypatch.setattr(ee, "_build_run_price_rows", _no_price_rows)

    async with scratch_database("reusetools", tables=ALL_TABLES, extensions=("vector",)) as db:
        sm = db.sessions
        async with sm() as s:
            agent = Agent(name="Agent", slug=f"a-{uuid.uuid4().hex[:6]}")
            evaluation = Evaluation(name="Eval", execution_mode="full_loop", reuse_default="on")
            dataset = Dataset(name="ds")
            s.add_all([agent, evaluation, dataset])
            await s.flush()
            await seed_provider(s, ["gpt-4o"])
            s.add(EvaluationDataset(evaluation_id=evaluation.id, dataset_id=dataset.id))
            s.add(DatasetEntry(dataset_id=dataset.id, position=0, input_prompt="Question", expected_answer="a"))
            await s.commit()
            agent_id, evaluation_id = agent.id, evaluation.id

        async def _run() -> EvaluationResult:
            async with sm() as s:
                run = EvaluationRun(
                    evaluation_id=evaluation_id,
                    agent_id=agent_id,
                    model_id="gpt-4o",
                    trigger_reason="manual",
                    status="pending",
                )
                s.add(run)
                await s.commit()
                run_id = run.id
            async with sm() as s:
                await ee.execute_evaluation_run(s, str(run_id))
            async with sm() as s:
                return (
                    await s.execute(select(EvaluationResult).where(EvaluationResult.run_id == run_id))
                ).scalar_one()

        fresh = await _run()
        reused = await _run()
        async with sm() as s:
            await rescore_as_started(s, reused.run_id)

    assert agent_calls == ["Question"]
    assert reused.is_reused
    assert fresh.execution_details["available_tools"] == ["search", "fetch"]
    assert reused.execution_details["available_tools"] == ["search", "fetch"]
    # Fresh, reused, rescored: one list, never the ``[]`` that grades tool selection 100.
    assert judged_with == [["search", "fetch"]] * 3


@contextlib.asynccontextmanager
async def _full_loop_evaluation_on_a_knowledge_base(monkeypatch):
    """A full-loop evaluation of one entry, on an agent bound to a knowledge base of one document.

    Yields ``run(reuse_default)``, which executes one run with the evaluation's reuse
    setting at ``reuse_default`` and returns its result, ``edit_document()``, which edits
    the document through the REST route, and ``agent_calls``, the prompts the agent was
    actually run on.
    """
    from sqlalchemy import select

    from app.api.v1 import knowledge_bases as kb_api
    from app.api.v1.schemas.knowledge_base import UpdateKnowledgeDocumentRequest
    from app.models.dataset import EvaluationDataset
    from app.models.knowledge_base import KnowledgeBase, KnowledgeDocument
    from tests.evaluation_helpers import seed_provider
    from tests.scratch_database import ALL_TABLES

    agent_calls: list[str] = []

    async def _agent(_db, _agent, _model_id, input_prompt, *_args, **_kwargs):
        agent_calls.append(input_prompt)
        return "every 90 days", [], None, [], []

    async def _judge(*_args, **_kwargs):
        return {
            "score": 80.0,
            "sub_scores": {k: 80 for k in ee.SUB_SCORE_WEIGHTS},
            "evaluator_reasoning": "ok",
            "is_judge_failure": False,
        }

    async def _no_price_rows(_db, _model_id):
        return []

    async def _no_tools(*_args, **_kwargs):
        return None

    monkeypatch.setattr(ee, "_execute_agent_full_loop", _agent)
    monkeypatch.setattr(ee, "_build_evaluation_integration_tools", _no_tools)
    monkeypatch.setattr(ee, "score_result", _judge)
    monkeypatch.setattr(ee, "_build_run_price_rows", _no_price_rows)

    async with scratch_database(
        "reusekb", tables=ALL_TABLES, extensions=("vector",), stub_worker_queue=True
    ) as db:
        sm = db.sessions
        async with sm() as s:
            user = User(email=f"kb-{uuid.uuid4().hex[:8]}@example.com", full_name="Owner", hashed_password="x", role="admin")
            s.add(user)
            await s.flush()
            kb = KnowledgeBase(name="Policies", user_id=user.id)
            s.add(kb)
            await s.flush()
            document = KnowledgeDocument(
                knowledge_base_id=kb.id,
                filename="keys.md",
                source_type="text",
                content="Rotate the signing keys every 90 days.",
                status="ready",
            )
            agent = Agent(name="Agent", slug=f"a-{uuid.uuid4().hex[:6]}", knowledge_base_ids=[str(kb.id)])
            evaluation = Evaluation(name="Eval", execution_mode="full_loop", reuse_default="on")
            dataset = Dataset(name="ds")
            s.add_all([document, agent, evaluation, dataset])
            await s.flush()
            await seed_provider(s, ["gpt-4o"])
            s.add(EvaluationDataset(evaluation_id=evaluation.id, dataset_id=dataset.id))
            s.add(DatasetEntry(dataset_id=dataset.id, position=0, input_prompt="Rotation?", expected_answer="90 days"))
            await s.commit()
            agent_id, evaluation_id = agent.id, evaluation.id

        async def _run(reuse_default: str) -> EvaluationResult:
            async with sm() as s:
                (await s.get(Evaluation, evaluation_id)).reuse_default = reuse_default
                run = EvaluationRun(
                    evaluation_id=evaluation_id,
                    agent_id=agent_id,
                    model_id="gpt-4o",
                    trigger_reason="manual",
                    status="pending",
                )
                s.add(run)
                await s.commit()
                run_id = run.id
            async with sm() as s:
                await ee.execute_evaluation_run(s, str(run_id))
            async with sm() as s:
                return (
                    await s.execute(select(EvaluationResult).where(EvaluationResult.run_id == run_id))
                ).scalar_one()

        async def _edit_document() -> None:
            async with sm() as s:
                await kb_api.update_document(
                    kb_id=str(kb.id),
                    doc_id=str(document.id),
                    body=UpdateKnowledgeDocumentRequest(content="Rotate the signing keys every 30 days."),
                    user=await s.get(User, user.id),
                    db=s,
                    _ai_gate=None,
                )

        yield SimpleNamespace(run=_run, edit_document=_edit_document, agent_calls=agent_calls)


@pytest.mark.integration
async def test_an_answer_is_not_reused_once_a_document_of_the_agents_knowledge_base_is_edited(monkeypatch):
    """A full-loop answer made against a document's old content is not reused after the edit."""
    async with _full_loop_evaluation_on_a_knowledge_base(monkeypatch) as evaluation:
        await evaluation.run("on")
        unchanged = await evaluation.run("on")
        await evaluation.edit_document()
        edited = await evaluation.run("on")

    assert unchanged.is_reused
    assert not edited.is_reused
    assert evaluation.agent_calls == ["Rotation?", "Rotation?"]


@pytest.mark.integration
async def test_an_answer_made_with_reuse_off_is_reused_by_a_later_run_while_the_knowledge_base_is_unchanged(
    monkeypatch,
):
    """A run with reuse off still fingerprints the knowledge base it ran against.

    Fingerprinted against no knowledge base instead, its answer would never match a
    later run's, and the agent would run again for nothing.
    """
    async with _full_loop_evaluation_on_a_knowledge_base(monkeypatch) as evaluation:
        made_with_reuse_off = await evaluation.run("off")
        unchanged = await evaluation.run("on")
        await evaluation.edit_document()
        edited = await evaluation.run("on")

    assert not made_with_reuse_off.is_reused
    assert unchanged.is_reused
    assert unchanged.reused_from_result_id == made_with_reuse_off.id
    assert not edited.is_reused
    assert evaluation.agent_calls == ["Rotation?", "Rotation?"]


@pytest.mark.integration
async def test_an_answer_a_callback_carried_is_never_reused_by_a_run_that_waits_for_none(monkeypatch):
    """A callback run stores the callback's answer, not the agent's, and with no
    fingerprint: it is never a reuse source, whatever the mode and the knowledge bases.

    Once the evaluation stops waiting for callbacks, a run with reuse on still asks
    its agent again rather than scoring that answer as the agent's own.
    """
    from sqlalchemy import select

    later: list[EvaluationResult] = []

    async def _run_without_callback(session, callback_run_id):
        callback_run = await session.get(EvaluationRun, callback_run_id)
        evaluation = await session.get(Evaluation, callback_run.evaluation_id)
        evaluation.trigger_config = None
        evaluation.reuse_default = "on"
        run = EvaluationRun(
            evaluation_id=evaluation.id,
            agent_id=callback_run.agent_id,
            model_id=callback_run.model_id,
            trigger_reason="manual",
            status="pending",
        )
        session.add(run)
        await session.commit()
        await ee.execute_evaluation_run(session, str(run.id))
        later.extend(
            (await session.execute(select(EvaluationResult).where(EvaluationResult.run_id == run.id))).scalars()
        )

    callback_run, carried = await run_on_postgres(
        monkeypatch, scorer_selection=[], answers=["a"], await_callback=True, then=_run_without_callback
    )

    assert callback_run.status == "completed"
    assert [r.agent_output for r in carried] == ["a"]
    assert len(later) == 1
    assert not later[0].is_reused


@pytest.mark.integration
async def test_a_run_whose_reused_answer_counts_is_completed_beside_an_entry_left_out(monkeypatch):
    """A reused answer is scored, so it counts in the headlines like a fresh one: a run
    holding one beside an entry our side failed is a measurement, ``completed``."""
    from sqlalchemy import select

    from app.models.dataset import EvaluationDataset
    from tests.evaluation_helpers import seed_provider
    from tests.scratch_database import ALL_TABLES

    agent_calls: list[str] = []

    async def _agent(_db, _agent, _model_id, input_prompt, *_args, **_kwargs):
        agent_calls.append(input_prompt)
        if input_prompt == "Question 1":
            raise RuntimeError("the vector store is gone")  # ours: left out
        return "an answer", [], None, [], []

    async def _judge(*_args, **_kwargs):
        return {
            "score": 80.0,
            "sub_scores": {k: 80 for k in ee.SUB_SCORE_WEIGHTS},
            "evaluator_reasoning": "ok",
            "is_judge_failure": False,
        }

    async def _no_price_rows(_db, _model_id):
        return []

    async def _no_tools(*_args, **_kwargs):
        return None

    monkeypatch.setattr(ee, "_execute_agent_full_loop", _agent)
    monkeypatch.setattr(ee, "_build_evaluation_integration_tools", _no_tools)
    monkeypatch.setattr(ee, "score_result", _judge)
    monkeypatch.setattr(ee, "_build_run_price_rows", _no_price_rows)

    async with scratch_database("reusecounted", tables=ALL_TABLES, extensions=("vector",)) as db:
        sm = db.sessions
        async with sm() as s:
            agent = Agent(name="Agent", slug=f"a-{uuid.uuid4().hex[:6]}")
            evaluation = Evaluation(name="Eval", execution_mode="full_loop", reuse_default="on")
            dataset = Dataset(name="ds")
            s.add_all([agent, evaluation, dataset])
            await s.flush()
            await seed_provider(s, ["gpt-4o"])
            s.add(EvaluationDataset(evaluation_id=evaluation.id, dataset_id=dataset.id))
            s.add_all(
                DatasetEntry(dataset_id=dataset.id, position=i, input_prompt=f"Question {i}", expected_answer="a")
                for i in range(2)
            )
            await s.commit()
            agent_id, evaluation_id = agent.id, evaluation.id

        async def _run() -> EvaluationRun:
            async with sm() as s:
                run = EvaluationRun(
                    evaluation_id=evaluation_id,
                    agent_id=agent_id,
                    model_id="gpt-4o",
                    trigger_reason="manual",
                    status="pending",
                )
                s.add(run)
                await s.commit()
                run_id = run.id
            async with sm() as s:
                await ee.execute_evaluation_run(s, str(run_id))
            async with sm() as s:
                return await s.get(EvaluationRun, run_id)

        await _run()
        second = await _run()
        async with sm() as s:
            reused = (
                await s.execute(
                    select(EvaluationResult.is_reused).where(
                        EvaluationResult.run_id == second.id, EvaluationResult.is_reused.is_(True)
                    )
                )
            ).scalars().all()

    assert agent_calls.count("Question 0") == 1, "the second run did not reuse the answer"
    assert reused == [True]
    assert second.status == "completed"


def test_run_response_reused_entries_defaults_to_zero():
    resp = EvaluationRunResponse(
        id=str(uuid.uuid4()),
        evaluation_id=str(uuid.uuid4()),
        agent_id=str(uuid.uuid4()),
        model_id="gpt-4o",
        status="completed",
        trigger_reason="manual",
        created_at="2026-07-24T00:00:00+00:00",
    )
    assert resp.reused_entries == 0


def _row(sub_scores, *, reused=False, judge_failure=False, anomaly_kind=None):
    return {
        "sub_scores": sub_scores,
        "is_reused": reused,
        "is_judge_failure": judge_failure,
        "execution_anomaly_kind": anomaly_kind,
    }


@pytest.mark.integration
@pytest.mark.parametrize(
    ("rows", "expected"),
    [
        (
            [
                _row({"quality": 8}, reused=True),
                _row({"quality": 6}, reused=True),
                _row({"quality": 4}),
            ],
            ({"quality": 6.0}, 2, 0, 0),
        ),
        ([_row(None, reused=True), _row(None, reused=True)], ({}, 2, 0, 0)),
        ([_row({"quality": 5})], ({"quality": 5.0}, 0, 0, 0)),
        # A judge failure is counted, and its fabricated verdict stays out of the
        # averages.
        (
            [_row({"quality": 8}), _row({"quality": 0}, reused=True, judge_failure=True)],
            ({"quality": 8.0}, 1, 1, 0),
        ),
        # An entry the platform could not run is counted; neither it nor a
        # transient one is averaged, as Judge Quality leaves both out.
        (
            [
                _row({"quality": 8}),
                _row({"quality": 0}, anomaly_kind="platform_error"),
                _row({"quality": 0}, anomaly_kind="transient_error"),
            ],
            ({"quality": 8.0}, 0, 0, 1),
        ),
    ],
    ids=["some-reused", "all-reused-unscored", "none-reused", "judge-failure-excluded", "platform-error"],
)
@pytest.mark.parametrize(
    "scorer_selection",
    [None, ["judge"], []],
    ids=["judge-on-by-default", "judge-selected", "judge-off"],
)
async def test_get_avg_sub_scores_counts_reused_and_judge_failed_entries(rows, expected, scorer_selection):
    """The run's own entries, counted; their sub-scores averaged only when its snapshot had the judge on."""
    from tests.scratch_database import ALL_TABLES

    async with scratch_database("reuseavg", tables=ALL_TABLES, extensions=("vector",)) as db, db.sessions() as s:
        agent = Agent(name="Agent", slug=f"a-{uuid.uuid4().hex[:6]}")
        evaluation = Evaluation(name="Eval")
        s.add_all([agent, evaluation])
        await s.flush()
        run, other_run = (
            EvaluationRun(
                evaluation_id=evaluation.id,
                agent_id=agent.id,
                model_id="gpt-4o",
                trigger_reason="manual",
                status="completed",
                scorer_selection=scorer_selection,
            )
            for _ in range(2)
        )
        s.add_all([run, other_run])
        await s.flush()
        s.add_all(
            EvaluationResult(run_id=run.id, agent_output="a", score=0, evaluator_reasoning="r", duration_seconds=1, **row)
            for row in rows
        )
        # Another run's entry, never counted in this one's.
        s.add(
            EvaluationResult(
                run_id=other_run.id,
                agent_output="a",
                score=0,
                evaluator_reasoning="r",
                duration_seconds=1,
                **_row({"quality": 100}, reused=True, judge_failure=True),
            )
        )
        await s.commit()

        averages, *counts = expected
        if scorer_selection == []:
            averages = {}
        assert await evaluations_api._get_avg_sub_scores(s, run) == (averages, *counts)


def test_evaluation_create_defaults_reuse_on():
    assert CreateEvaluationRequest(name="demo").reuse_default == "on"
