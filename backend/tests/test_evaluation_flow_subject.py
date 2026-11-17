"""A flow as evaluation subject (#8257).

A link or a run names an agent or a flow, never both. A launch gives each flow
one run on ``MODEL_INDEPENDENT``; the engine runs the flow on every entry and
scores its final output like an agent's answer, in the ``flow`` mode. The agent
path is unchanged: its reuse fingerprint is byte-identical to what it was.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
import sqlalchemy as sa
from sqlalchemy import select

from app.models.evaluation import SUBJECT_AGENT, SUBJECT_FLOW, EvaluationAgent, EvaluationRun
from app.services.evaluation.constants import MODEL_INDEPENDENT
from app.services.evaluation.fingerprint import compute_input_fingerprint
from app.services.evaluation.flow_subject import FLOW_EXECUTION_MODE, FlowEntryOutcome
from app.services.evaluation.launch import check_runs_served

AGENT_ARGS = {
    "agent_id": "11111111-1111-4111-8111-111111111111",
    "model_id": "gpt-x",
    "execution_mode": "full_loop",
    "entry_input": "Hello",
    "entry_file_ids": ["f2", "f1"],
    "persona_version": 3,
    "prompt_versions": {"p": 2},
    "agent_config_versions": {"temperature": 1},
    "kb_state": [["kb", 4, "emb"]],
    "floored_max_iterations": 8,
    "apply_prompt_guards": True,
}


def test_an_agents_fingerprint_is_unchanged():
    # Computed with the fingerprint as it was before flows (#8257).
    assert compute_input_fingerprint(**AGENT_ARGS) == "9ae37e544950e1f2562905320af3d3968351914ce16134c4cf561e616ffcb9ef"


def test_a_flows_fingerprint_names_the_flow_and_its_version():
    same_id = AGENT_ARGS["agent_id"]

    def flow(flow_id=same_id, version=1):
        return compute_input_fingerprint(
            agent_id=None,
            flow_id=flow_id,
            flow_version=version,
            model_id=MODEL_INDEPENDENT,
            execution_mode=FLOW_EXECUTION_MODE,
            entry_input="Hello",
        )

    fingerprints = {
        flow(),
        flow(version=2),
        flow(flow_id="22222222-2222-4222-8222-222222222222"),
        compute_input_fingerprint(
            agent_id=same_id, model_id=MODEL_INDEPENDENT, execution_mode=FLOW_EXECUTION_MODE, entry_input="Hello"
        ),
    }
    assert len(fingerprints) == 4
    assert flow() == flow()


@pytest.mark.parametrize(
    ("agent_id", "flow_id", "kind"),
    [(uuid.UUID(int=1), None, SUBJECT_AGENT), (None, uuid.UUID(int=2), SUBJECT_FLOW)],
    ids=["agent", "flow"],
)
def test_a_subject_is_read_off_its_link_and_its_run(agent_id, flow_id, kind):
    for row in (EvaluationAgent(agent_id=agent_id, flow_id=flow_id), EvaluationRun(agent_id=agent_id, flow_id=flow_id)):
        assert (row.subject_kind, row.subject_id) == (kind, agent_id or flow_id)


async def test_a_flow_run_is_never_checked_as_a_model():
    # No database read: nothing is left to check.
    await check_runs_served(None, SimpleNamespace(), [(None, MODEL_INDEPENDENT, False)])  # type: ignore[arg-type]


# --- PostgreSQL --------------------------------------------------------------


async def _seed(s, *, with_agent: bool = True):
    from app.models.agent import Agent
    from app.models.dataset import Dataset, DatasetEntry, EvaluationDataset
    from app.models.evaluation import Evaluation
    from app.models.flow import Flow

    evaluation = Evaluation(name="Eval", family="PROSE", scorer_selection=None, execution_mode="llm_only")
    dataset = Dataset(name="ds", family="PROSE")
    flow = Flow(name="Flow", graph={"nodes": [{"id": "start", "kind": "trigger.manual", "config": {}}]}, version_number=4)
    s.add_all([evaluation, dataset, flow])
    agent = None
    if with_agent:
        agent = Agent(name="Agent", slug=f"a-{uuid.uuid4().hex[:6]}", skip_llm=True)
        s.add(agent)
    await s.flush()
    s.add(EvaluationDataset(evaluation_id=evaluation.id, dataset_id=dataset.id))
    s.add_all(
        DatasetEntry(dataset_id=dataset.id, position=i, input_prompt=f"Question {i}", expected_answer="a")
        for i in range(2)
    )
    s.add(EvaluationAgent(evaluation_id=evaluation.id, flow_id=flow.id))
    if agent is not None:
        s.add(EvaluationAgent(evaluation_id=evaluation.id, agent_id=agent.id))
    await s.commit()
    return evaluation, flow, agent


@pytest.mark.integration
async def test_a_launch_gives_a_flow_one_model_independent_run():
    import app.models  # noqa: F401 - register every mapper on Base.metadata
    from app.services.evaluation.launch import add_launch_runs, plan_launch
    from tests.scratch_database import ALL_TABLES, scratch_database

    async with (
        scratch_database("evalflowlaunch", tables=ALL_TABLES, extensions=("vector",)) as db,
        db.sessions() as s,
    ):
        evaluation, flow, agent = await _seed(s)
        plan = await plan_launch(s, evaluation, model_ids=["gpt-x", "gpt-y"], use_default=False)
        runs = await add_launch_runs(
            s, evaluation, plan, trigger_reason="manual", owner_execution_id=uuid.uuid4()
        )
        await s.commit()
        rows = {
            (run.agent_id, run.flow_id, run.model_id, run.flow_version_number)
            for run in (await s.execute(select(EvaluationRun))).scalars()
        }

    assert (plan.agent_ids, plan.flow_ids) == ([agent.id], [flow.id])
    assert len(runs) == 2
    # The agent is model-independent too (skip_llm): one run each.
    assert rows == {(agent.id, None, MODEL_INDEPENDENT, None), (None, flow.id, MODEL_INDEPENDENT, 4)}


@pytest.mark.integration
async def test_a_placebo_launch_with_a_flow_is_refused():
    import app.models  # noqa: F401
    from app.services.evaluation.launch import LaunchRefusedError, add_launch_runs, plan_launch
    from tests.scratch_database import ALL_TABLES, scratch_database

    async with (
        scratch_database("evalflowplacebo", tables=ALL_TABLES, extensions=("vector",)) as db,
        db.sessions() as s,
    ):
        evaluation, _flow, _agent = await _seed(s)
        plan = await plan_launch(s, evaluation, model_ids=["gpt-x"], use_default=False)
        with pytest.raises(LaunchRefusedError) as refused:
            await add_launch_runs(
                s,
                evaluation,
                plan,
                trigger_reason="manual",
                owner_execution_id=uuid.uuid4(),
                comparison=(SimpleNamespace(id=uuid.uuid4()), []),  # type: ignore[arg-type]
            )

    assert refused.value.detail == "A placebo comparison applies to agents only."


@pytest.mark.integration
@pytest.mark.parametrize(
    "subject",
    [{}, {"agent": True, "flow": True}],
    ids=["neither", "both"],
)
async def test_a_link_and_a_run_name_exactly_one_subject(subject):
    import app.models  # noqa: F401
    from tests.scratch_database import ALL_TABLES, scratch_database

    async with (
        scratch_database("evalflowcheck", tables=ALL_TABLES, extensions=("vector",)) as db,
        db.sessions() as s,
    ):
        evaluation, flow, agent = await _seed(s)
        ids = {
            "agent_id": agent.id if subject.get("agent") else None,
            "flow_id": flow.id if subject.get("flow") else None,
        }
        for row in (
            EvaluationAgent(evaluation_id=evaluation.id, **ids),
            EvaluationRun(evaluation_id=evaluation.id, model_id="m", trigger_reason="manual", **ids),
        ):
            async with s.begin_nested():
                s.add(row)
                with pytest.raises(sa.exc.IntegrityError, match="one_subject"):
                    await s.flush()


@pytest.mark.integration
async def test_the_engine_runs_the_flow_on_every_entry_and_scores_its_output(monkeypatch):
    import app.models  # noqa: F401
    from app.models.evaluation import EvaluationResult
    from app.services import evaluation_engine as ee
    from tests.scratch_database import ALL_TABLES, scratch_database

    asked: list[tuple[str | None, uuid.UUID | None]] = []

    async def _run_flow(_db, flow, *, input_prompt, entry_id, owner_id, **_kwargs):
        asked.append((input_prompt, flow.id))
        return FlowEntryOutcome(
            output=f"answer to {input_prompt}",
            flow_run_id=uuid.uuid4(),
            status="completed",
            wall_clock_ms=1234,
            ledger=None,
            timeline=[{"type": "flow_step", "duration_ms": 1000, "description": "x"}],
        )

    async def _judge(*_args, **_kwargs):
        return {"score": 80.0, "sub_scores": {k: 80 for k in ee.SUB_SCORE_WEIGHTS}, "evaluator_reasoning": "ok"}

    async def _no_price_rows(_db, _model_id):
        return []

    async def _agent_never(*_args, **_kwargs):
        raise AssertionError("a flow subject never runs an agent")

    monkeypatch.setattr(ee, "run_flow_for_entry", _run_flow)
    monkeypatch.setattr(ee, "score_result", _judge)
    monkeypatch.setattr(ee, "_build_run_price_rows", _no_price_rows)
    monkeypatch.setattr(ee, "_execute_agent_full_loop", _agent_never)
    monkeypatch.setattr(ee, "_execute_agent_llm_only", _agent_never)

    async with scratch_database("evalflowengine", tables=ALL_TABLES, extensions=("vector",)) as db:
        async with db.sessions() as s:
            evaluation, flow, _agent = await _seed(s, with_agent=False)
            run = EvaluationRun(
                evaluation_id=evaluation.id,
                flow_id=flow.id,
                model_id=MODEL_INDEPENDENT,
                trigger_reason="manual",
                status="pending",
            )
            s.add(run)
            await s.commit()
            run_id, flow_id = run.id, flow.id

        async with db.sessions() as s:
            await ee.execute_evaluation_run(s, str(run_id))

        async with db.sessions() as s:
            stored = await s.get(EvaluationRun, run_id)
            results = (
                (await s.execute(select(EvaluationResult).where(EvaluationResult.run_id == run_id))).scalars().all()
            )

    assert (stored.status, stored.error, stored.execution_mode, stored.flow_version_number) == (
        "completed",
        None,
        FLOW_EXECUTION_MODE,
        4,
    )
    assert sorted(asked) == [("Question 0", flow_id), ("Question 1", flow_id)]
    assert sorted(r.agent_output for r in results) == ["answer to Question 0", "answer to Question 1"]
    assert {r.score for r in results} == {80.0}
    assert {r.execution_details["agent_wall_clock_ms"] for r in results} == {1234}
    # The traces had not landed: the chain says so instead of a measured zero.
    assert {r.execution_details["chain"]["incomplete_reason"] for r in results} == {"ledger_missing"}
    assert len({r.input_fingerprint for r in results}) == 2


@pytest.mark.integration
async def test_a_rescore_with_two_flow_subjects_reads_no_extractor():
    # ``agent_id == None`` would be ``IS NULL``: every flow link, two rows, a raise.
    import app.models  # noqa: F401
    from app.models.flow import Flow
    from app.services.evaluation_engine import stored_scoring_for_run
    from tests.scratch_database import ALL_TABLES, scratch_database

    async with (
        scratch_database("evalflowrescore", tables=ALL_TABLES, extensions=("vector",)) as db,
        db.sessions() as s,
    ):
        evaluation, flow, _agent = await _seed(s, with_agent=False)
        other = Flow(name="Other", graph=flow.graph, version_number=1)
        s.add(other)
        await s.flush()
        s.add(EvaluationAgent(evaluation_id=evaluation.id, flow_id=other.id, output_extractor={"kind": "regex"}))
        run = EvaluationRun(
            evaluation_id=evaluation.id, flow_id=flow.id, model_id=MODEL_INDEPENDENT, trigger_reason="manual"
        )
        s.add(run)
        await s.commit()
        scoring = await stored_scoring_for_run(s, run, judge_selected=False)

    assert scoring.extractor is None
