"""The Concierge's evaluations and their tools (#8257, PRD 6.8 and 8).

One access rule for the agent tools and ``/mcp/xtm``: the Concierge (built in
since #101 S1, no flag), or an administrator while the evaluation surface is on
(B9). A launch takes a Concierge-built dataset and subjects that are sandbox
copies or targets of the supervision scope, and marks its evaluation for the
engine gate. A comparison gives no verdict on an incomplete run.
"""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.core.config import settings
from app.services.health import evaluation_tools, identity
from app.services.health.evaluation_tools import REFUSED, evaluation_tool_refusal


class _Db:
    def __init__(self, user):
        self.user = user

    async def get(self, _model, _id):
        return self.user


ADMIN = SimpleNamespace(is_admin=True, is_active=True)
MEMBER = SimpleNamespace(is_admin=False, is_active=True)
FORMER_ADMIN = SimpleNamespace(is_admin=True, is_active=False)


@pytest.mark.parametrize(
    ("features", "mode", "concierge_run", "caller", "allowed"),
    [
        ("CONCIERGE", "xtm_one", True, None, True),
        ("", "xtm_one", True, None, True),
        ("CONCIERGE", "xtm_one", False, ADMIN, False),
        ("CONCIERGE,EVALUATION", "copilot", False, ADMIN, True),
        ("CONCIERGE,EVALUATION", "copilot", False, MEMBER, False),
        ("CONCIERGE,EVALUATION", "copilot", False, FORMER_ADMIN, False),
        ("CONCIERGE,EVALUATION", "copilot", False, None, False),
        ("EVALUATION", "copilot", False, ADMIN, True),
        ("", "copilot", False, ADMIN, False),
    ],
    ids=[
        "concierge-xtm-one",
        "concierge-no-flag",
        "admin-xtm-one",
        "admin-surface-on",
        "member",
        "deactivated-admin",
        "anonymous",
        "admin-evaluation-only",
        "admin-surface-off",
    ],
)
async def test_one_access_rule(monkeypatch, features, mode, concierge_run, caller, allowed):
    monkeypatch.setattr(settings, "enabled_features", features)
    monkeypatch.setattr(settings, "platform_mode", mode)
    monkeypatch.setattr(identity, "in_concierge_run", lambda: concierge_run)

    refusal = await evaluation_tool_refusal(_Db(caller), str(uuid.uuid4()) if caller else None)  # type: ignore[arg-type]

    assert (refusal is None) is allowed


async def test_every_tool_refuses_generically_without_reading(monkeypatch):
    monkeypatch.setattr(settings, "enabled_features", "")
    definitions, executor = evaluation_tools.build_concierge_evaluation_tools(None, None)  # type: ignore[arg-type]
    calls = {
        "build_dataset_from_traces": {"target_type": "agent", "target_id": str(uuid.uuid4())},
        "run_concierge_evaluation": {"dataset_id": str(uuid.uuid4())},
        "get_concierge_evaluation": {"evaluation_id": str(uuid.uuid4())},
        "compare_concierge_runs": {"baseline_run_id": str(uuid.uuid4()), "candidate_run_id": str(uuid.uuid4())},
    }

    assert {d.name for d in definitions} == set(calls)
    for name, arguments in calls.items():
        assert await executor._handlers[name](**arguments) == REFUSED


class _AdminDb(_Db):
    def __init__(self):
        super().__init__(ADMIN)
        self.commits = 0

    async def commit(self):
        self.commits += 1


async def test_an_administrator_s_launch_is_audited_and_a_bare_date_is_utc(monkeypatch):
    from datetime import UTC, datetime

    from app.services import audit
    from app.services.health import datasets, evaluations

    monkeypatch.setattr(evaluation_tools, "evaluation_tool_refusal", _allowed)
    monkeypatch.setattr(identity, "in_concierge_run", lambda: False)
    audited, built = [], []

    async def _log_audit(_db, **kwargs):
        audited.append((kwargs["action"], kwargs["entity_type"], kwargs["entity_id"], kwargs["user"]))

    async def _launch(_db, **_kwargs):
        return evaluations.HealthLaunch(evaluation_id, [run_id])

    async def _build(_db, **kwargs):
        built.append(kwargs["since"])
        return datasets.DatasetBuildResult(dataset_id=dataset_id)

    monkeypatch.setattr(audit, "log_audit", _log_audit)
    monkeypatch.setattr(evaluations, "launch_concierge_evaluation", _launch)
    monkeypatch.setattr(datasets, "build_dataset_from_traces", _build)
    evaluation_id, run_id, dataset_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    db = _AdminDb()
    _defs, executor = evaluation_tools.build_concierge_evaluation_tools(db, str(uuid.uuid4()))  # type: ignore[arg-type]

    launched = await executor._handlers["run_concierge_evaluation"](dataset_id=str(dataset_id), agent_ids=[str(uuid.uuid4())])
    await executor._handlers["build_dataset_from_traces"](
        target_type="agent", target_id=str(uuid.uuid4()), since="2026-11-01"
    )

    assert json.loads(launched) == {"evaluation_id": str(evaluation_id), "run_ids": [str(run_id)]}
    assert audited == [
        ("concierge.evaluation_launch", "evaluation", evaluation_id, ADMIN),
        ("concierge.dataset_build", "dataset", dataset_id, ADMIN),
    ]
    assert db.commits == 2
    assert built == [datetime(2026, 11, 1, tzinfo=UTC)]


async def _allowed(_db, _user_id):
    return None


# --- PostgreSQL --------------------------------------------------------------


@pytest.fixture
def concierge_on(monkeypatch):
    from app.services.health import budget

    async def _licensed(_db):
        return None

    monkeypatch.setattr(settings, "enabled_features", "CONCIERGE")
    monkeypatch.setattr(settings, "platform_mode", "xtm_one")
    monkeypatch.setattr(budget, "_gate", _licensed)


async def _world(s, *, scope_agent_ids: list | None = None, budget: dict | None = None):
    from app.models.agent import Agent
    from app.models.dataset import Dataset, DatasetEntry
    from app.models.flow import Flow
    from app.models.platform_settings import PlatformSettings

    # Enrolled: the supervision scope only holds what its managers opted in.
    agent = Agent(name="Analyst", slug=f"a-{uuid.uuid4().hex[:6]}", skip_llm=True, health_enrolled=True)
    fleet = Agent(
        name="Experimenter", slug=f"e-{uuid.uuid4().hex[:6]}", health_role="experimenter", is_company_managed=True
    )
    flow = Flow(
        name="Triage",
        graph={"nodes": [{"id": "start", "kind": "trigger.manual", "config": {}}]},
        health_enrolled=True,
    )
    dataset = Dataset(name="Concierge - Analyst", family="PROSE", concierge_built=True)
    human = Dataset(name="People's", family="PROSE")
    s.add_all([agent, fleet, flow, dataset, human])
    await s.flush()
    scope = (
        {"scope_mode": "all"}
        if scope_agent_ids is None
        else {"scope_mode": "selected", "scope_agent_ids": [str(a) for a in scope_agent_ids], "scope_flow_ids": []}
    )
    s.add(PlatformSettings(health_settings={"enabled": True, **scope, **({"budget": budget} if budget else {})}))
    s.add_all(
        DatasetEntry(dataset_id=d.id, position=0, input_prompt="Question", expected_answer="a") for d in (dataset, human)
    )
    # The subjects are sandbox copies (level 2): an evaluation never runs a production one.
    copies = [await _copy(s, source) for source in (agent, fleet, flow)]
    await s.commit()
    return (*copies, dataset, human, agent)


async def _copy(s, source):
    from datetime import UTC, datetime, timedelta

    from app.models.agent import Agent
    from app.models.flow import Flow
    from app.models.sandbox_copy import SandboxCopy

    if isinstance(source, Agent):
        copied = Agent(name=f"{source.name} (copy)", slug=f"c-{uuid.uuid4().hex[:6]}", skip_llm=True, is_sandbox=True)
    else:
        copied = Flow(name=f"{source.name} (copy)", graph=source.graph, is_sandbox=True)
    s.add(copied)
    await s.flush()
    kind = "agent" if isinstance(source, Agent) else "flow"
    s.add(
        SandboxCopy(
            source_type=kind,
            source_id=source.id,
            created_for="test",
            expires_at=datetime.now(UTC) + timedelta(days=14),
            **{f"copy_{kind}_id": copied.id},
        )
    )
    return copied


@pytest.mark.integration
@pytest.mark.usefixtures("concierge_on")
async def test_a_launch_marks_its_evaluation_and_runs_agents_and_flows(monkeypatch):
    import app.models  # noqa: F401
    from app.models.evaluation import Evaluation, EvaluationRun
    from app.models.orchestration import OrchestrationExecution
    from app.services import orchestration_engine
    from app.services.evaluation.constants import MODEL_INDEPENDENT
    from app.services.health.evaluations import launch_concierge_evaluation
    from tests.scratch_database import ALL_TABLES, scratch_database

    async def _no_wake() -> None:
        return None

    monkeypatch.setattr(orchestration_engine, "notify_orchestration_wake", _no_wake)
    async with (
        scratch_database("conciergeevlaunch", tables=ALL_TABLES, extensions=("vector",)) as db,
        db.sessions() as s,
    ):
        agent, _fleet, flow, dataset, _human, _source = await _world(s)
        launch = await launch_concierge_evaluation(
            s, dataset_id=dataset.id, subject_agent_ids=[agent.id], subject_flow_ids=[flow.id]
        )
        evaluation = await s.get(Evaluation, launch.evaluation_id)
        runs = (await s.execute(select(EvaluationRun))).scalars().all()
        executions = (await s.execute(select(OrchestrationExecution))).scalars().all()

    assert (
        evaluation.concierge_built,
        evaluation.user_id,
        evaluation.is_company_managed,
        evaluation.enabled,
        evaluation.reuse_default,
    ) == (True, None, False, False, "off")
    assert {(r.agent_id, r.flow_id, r.model_id, r.status) for r in runs} == {
        (agent.id, None, MODEL_INDEPENDENT, "pending"),
        (None, flow.id, MODEL_INDEPENDENT, "pending"),
    }
    assert sorted(launch.run_ids) == sorted(r.id for r in runs)
    assert [(e.adapter, e.payload["evaluation_id"]) for e in executions] == [("evaluation", str(evaluation.id))]
    assert {r.owner_execution_id for r in runs} == {executions[0].id}


@pytest.mark.integration
@pytest.mark.usefixtures("concierge_on")
@pytest.mark.parametrize("case", ["human-dataset", "production-agent", "fleet-agent", "out-of-scope", "no-subject"])
async def test_a_launch_is_refused_before_anything_is_written(case):
    import app.models  # noqa: F401
    from app.models.evaluation import Evaluation
    from app.services.health.evaluations import HealthEvaluationError, launch_concierge_evaluation
    from tests.scratch_database import ALL_TABLES, scratch_database

    async with (
        scratch_database("conciergeevrefuse", tables=ALL_TABLES, extensions=("vector",)) as db,
        db.sessions() as s,
    ):
        scope = [uuid.uuid4()] if case == "out-of-scope" else None
        agent, fleet, _flow, dataset, human, source = await _world(s, scope_agent_ids=scope)
        subjects = {"fleet-agent": [fleet.id], "no-subject": [], "production-agent": [source.id]}.get(case, [agent.id])
        with pytest.raises(HealthEvaluationError):
            await launch_concierge_evaluation(
                s, dataset_id=(human if case == "human-dataset" else dataset).id, subject_agent_ids=subjects
            )
        await s.rollback()
        evaluations = (await s.execute(select(Evaluation))).scalars().all()

    assert evaluations == []


@pytest.mark.integration
@pytest.mark.usefixtures("concierge_on")
@pytest.mark.parametrize(
    ("candidate", "verdict", "reason"),
    [
        ({"score": 90.0}, "progression", None),
        ({"score": 79.0}, "neutral", None),
        ({"score": 60.0, "status": "failed"}, None, "the candidate run is failed"),
        ({"score": 90.0, "headline_details": {"withdrawn_entries": 1}}, None, "the candidate run lost entries to a consent revocation"),
        ({"score": None}, None, "the quality metric is not measured on both runs"),
    ],
    ids=["progression", "neutral", "failed", "withdrawn", "unmeasured"],
)
async def test_a_comparison_states_its_verdict_or_why_there_is_none(candidate, verdict, reason):
    import app.models  # noqa: F401
    from app.models.evaluation import Evaluation, EvaluationResult, EvaluationRun
    from app.services.health.evaluations import compare_concierge_runs
    from tests.scratch_database import ALL_TABLES, scratch_database

    async with (
        scratch_database("conciergeevcompare", tables=ALL_TABLES, extensions=("vector",)) as db,
        db.sessions() as s,
    ):
        agent, _fleet, flow, _dataset, _human, _source = await _world(s)
        evaluation = Evaluation(name="E", concierge_built=True)
        s.add(evaluation)
        await s.flush()
        base = EvaluationRun(
            evaluation_id=evaluation.id, agent_id=agent.id, model_id="m", trigger_reason="concierge", status="completed", score=80.0
        )
        cand = EvaluationRun(
            evaluation_id=evaluation.id,
            flow_id=flow.id,
            model_id="m",
            trigger_reason="concierge",
            **{"status": "completed", **candidate},
        )
        s.add_all([base, cand])
        await s.flush()
        s.add(
            EvaluationResult(
                run_id=cand.id, agent_output="x", score=1.0, execution_details={"sandbox": {"simulated_writes": 2}}
            )
        )
        await s.commit()
        comparison = await compare_concierge_runs(s, baseline_run_id=base.id, candidate_run_id=cand.id)

    data = json.loads(json.dumps(comparison.to_dict()))
    assert (data["verdict"], data["reason"], data["quality_metric"]) == (verdict, reason, "score")
    assert data["simulated_writes"] == {"baseline": 0, "candidate": 2}
    assert len(data["warnings"]) == 1


@pytest.mark.integration
@pytest.mark.usefixtures("concierge_on")
async def test_a_person_s_evaluation_is_never_read_or_compared():
    import app.models  # noqa: F401
    from app.models.evaluation import Evaluation, EvaluationRun
    from app.services.health.evaluations import (
        HealthEvaluationError,
        compare_concierge_runs,
        concierge_evaluation_runs,
    )
    from tests.scratch_database import ALL_TABLES, scratch_database

    async with (
        scratch_database("conciergeevhuman", tables=ALL_TABLES, extensions=("vector",)) as db,
        db.sessions() as s,
    ):
        agent, *_ = await _world(s)
        evaluation = Evaluation(name="People's")
        s.add(evaluation)
        await s.flush()
        runs = [
            EvaluationRun(evaluation_id=evaluation.id, agent_id=agent.id, model_id=m, trigger_reason="manual", status="completed")
            for m in ("a", "b")
        ]
        s.add_all(runs)
        await s.commit()
        with pytest.raises(HealthEvaluationError):
            await concierge_evaluation_runs(s, evaluation.id)
        with pytest.raises(HealthEvaluationError):
            await compare_concierge_runs(s, baseline_run_id=runs[0].id, candidate_run_id=runs[1].id)


# --- Budget ------------------------------------------------------------------


async def _no_wake() -> None:
    return None


async def _measured(s, run, *, tokens: float, cost: float) -> None:
    """One scored entry of *run* with its performance metrics."""
    from app.models.evaluation import EvaluationResult
    from app.models.evaluation_metric import EvaluationMetric

    result = EvaluationResult(run_id=run.id, agent_output="x", score=1.0)
    s.add(result)
    await s.flush()
    s.add_all(
        EvaluationMetric(result_id=result.id, run_id=run.id, key=key, value=value)
        for key, value in (("performance.tokens.total", tokens), ("performance.cost.llm", cost))
    )


@pytest.mark.integration
@pytest.mark.usefixtures("concierge_on")
async def test_a_launch_holds_its_reservation_past_its_cycle_and_is_charged_when_it_ends(monkeypatch):
    from decimal import Decimal

    import app.models  # noqa: F401
    from app.models.evaluation import EvaluationRun
    from app.models.health import HealthBudgetMonth, HealthCycle
    from app.services import orchestration_engine
    from app.services.health import budget
    from app.services.health.evaluations import (
        ENTRY_TOKENS_ESTIMATE,
        launch_concierge_evaluation,
        settle_concierge_evaluations,
    )
    from tests.scratch_database import ALL_TABLES, scratch_database

    monkeypatch.setattr(orchestration_engine, "notify_orchestration_wake", _no_wake)
    async with (
        scratch_database("conciergeevhold", tables=ALL_TABLES, extensions=("vector",)) as db,
        db.sessions() as s,
    ):
        agent, _fleet, flow, dataset, _human, _source = await _world(s)
        cycle_id = await budget.open_cycle(s, specialist="experimenter", trigger="signal")
        await s.commit()
        await launch_concierge_evaluation(
            s, dataset_id=dataset.id, subject_agent_ids=[agent.id], subject_flow_ids=[flow.id], cycle_id=cycle_id
        )
        reserved = (await s.get(HealthCycle, cycle_id)).reserved_tokens
        # The run that launched it ends first: the cycle closes, the evaluation keeps its share.
        await budget.close_cycle(s, cycle_id, status="completed")
        await s.commit()
        month = (await s.execute(select(HealthBudgetMonth))).scalar_one()
        held = ((await s.get(HealthCycle, cycle_id)).reserved_tokens, month.reserved_tokens)
        assert await settle_concierge_evaluations(s) == 0
        for run in (await s.execute(select(EvaluationRun))).scalars().all():
            await _measured(s, run, tokens=3_000, cost=0.01)
            run.status = "completed"
        await s.commit()
        charged, again = await settle_concierge_evaluations(s), await settle_concierge_evaluations(s)
        cycle = await s.get(HealthCycle, cycle_id)
        await s.refresh(cycle)
        await s.refresh(month)

    assert reserved == 2 * ENTRY_TOKENS_ESTIMATE
    assert held == (reserved, reserved)
    assert (charged, again) == (1, 0)
    assert (cycle.reserved_tokens, cycle.spent_tokens, cycle.spent_cost_usd) == (0, 6_000, Decimal("0.02"))
    assert (month.reserved_tokens, month.spent_tokens) == (0, 6_000)


@pytest.mark.integration
@pytest.mark.usefixtures("concierge_on")
async def test_an_evaluation_past_a_cap_is_stopped_and_reads_budget_reached(monkeypatch):
    import app.models  # noqa: F401
    from app.models.evaluation import EvaluationRun
    from app.models.health import HealthCycle
    from app.services import orchestration_engine
    from app.services.health import budget
    from app.services.health.evaluations import (
        BUDGET_STOP_ERROR,
        compare_concierge_runs,
        launch_concierge_evaluation,
        settle_concierge_evaluations,
    )
    from tests.scratch_database import ALL_TABLES, scratch_database

    monkeypatch.setattr(orchestration_engine, "notify_orchestration_wake", _no_wake)
    async with (
        scratch_database("conciergeevcap", tables=ALL_TABLES, extensions=("vector",)) as db,
        db.sessions() as s,
    ):
        agent, _fleet, flow, dataset, _human, _source = await _world(s, budget={"cycle_tokens": 12_000})
        cycle_id = await budget.open_cycle(s, specialist="experimenter", trigger="signal")
        await s.commit()
        await launch_concierge_evaluation(
            s,
            dataset_id=dataset.id,
            subject_agent_ids=[agent.id],
            subject_flow_ids=[flow.id],
            cycle_id=cycle_id,
            reserve_tokens=10_000,
        )
        runs = (await s.execute(select(EvaluationRun))).scalars().all()
        base, cand = sorted(runs, key=lambda r: r.flow_id is not None)
        base.status, cand.status = "completed", "running"
        await _measured(s, base, tokens=4_000, cost=0.0)
        await _measured(s, cand, tokens=9_000, cost=0.0)
        await s.commit()
        # 13 000 measured: past the 10 000 reserved and the 2 000 left under the cycle's cap.
        assert await settle_concierge_evaluations(s) == 0
        await s.refresh(cand)
        stopped = (cand.status, cand.error)
        assert await settle_concierge_evaluations(s) == 1
        comparison = await compare_concierge_runs(s, baseline_run_id=base.id, candidate_run_id=cand.id)
        cycle = await s.get(HealthCycle, cycle_id)
        await s.refresh(cycle)

    assert stopped == ("cancelled", BUDGET_STOP_ERROR)
    assert (comparison.verdict, comparison.reason_code) == (None, "budget")
    assert (cycle.reserved_tokens, cycle.spent_tokens) == (0, 13_000)


@pytest.mark.integration
@pytest.mark.usefixtures("concierge_on")
async def test_a_launch_outside_a_cycle_is_refused_at_the_cap():
    import app.models  # noqa: F401
    from app.models.evaluation import Evaluation
    from app.services.health import budget
    from app.services.health.evaluations import launch_concierge_evaluation
    from tests.scratch_database import ALL_TABLES, scratch_database

    async with (
        scratch_database("conciergeevgate", tables=ALL_TABLES, extensions=("vector",)) as db,
        db.sessions() as s,
    ):
        agent, _fleet, _flow, dataset, _human, _source = await _world(s, budget={"month_tokens": 0})
        with pytest.raises(budget.BudgetRefusedError) as refused:
            await launch_concierge_evaluation(s, dataset_id=dataset.id, subject_agent_ids=[agent.id])
        await s.rollback()
        evaluations = (await s.execute(select(Evaluation))).scalars().all()

    assert (refused.value.code, evaluations) == ("month_budget_reached", [])
