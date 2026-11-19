"""The Experimenter's engine on PostgreSQL (#8259), lower levels stubbed at
``quality_deps`` and the change measured by a stand-in kind: the budget read
before anything launches, what stops a launch stated, the proposal filed from
the row with its verdict and simulated writes or ``unmeasured`` with its
reason, never over a stale target or for a regression."""

from __future__ import annotations

import dataclasses
import uuid
from decimal import Decimal

import pytest

from app.models.health_experiment import (
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_INCOMPLETE,
    STATUS_RUNNING,
    STATUS_SKIPPED,
    STATUS_STALE,
)
from app.models.health_finding import STATUS_EXPERIMENTING, STATUS_PROPOSED
from app.services.health import experiments, quality_deps
from tests.health_experiment_helpers import VARIANT, Deps, draft_variant, run_experiment, seed
from tests.test_provisioning_postgres import _temp_database

pytestmark = pytest.mark.integration

BASE = "The text the variant replaces."


@dataclasses.dataclass
class _StubKind:
    """A text change measured on two sandbox copies of the finding's agent."""

    stale: bool = False
    measurable: bool = True

    async def prepare_draft(self, db, *, finding, target_id, payload):
        return experiments.DraftSpec(
            target_type="agent",
            subject_agent_id=target_id,
            base_text=BASE,
            base_version=1,
            variant_text=payload["text"],
            variant={},
            summary=payload["summary"],
        )

    async def check_stale(self, db, experiment):
        return self.stale

    async def dataset_target(self, db, experiment):
        return ("agent", experiment.subject_agent_id) if self.measurable else None

    async def subjects(self, db, experiment, *, created_for, author_agent_id):
        arms = []
        for persona in (None, experiment.variant_text):
            copy_id, agent_id = await quality_deps.create_agent_copy(
                db,
                agent_id=experiment.subject_agent_id,
                created_for=created_for,
                created_by_agent_id=author_agent_id,
                persona=persona,
                source_trace_ids=[],
            )
            arms.append(quality_deps.Subject(kind="agent", id=agent_id, copy_id=copy_id))
        return arms[0], arms[1]

    def proposal_content(self, experiment):
        return {"base_content": experiment.base_text, "proposed_content": experiment.variant_text}


@pytest.fixture
def stub(monkeypatch):
    kind = _StubKind()
    monkeypatch.setitem(experiments._KINDS, "persona", kind)
    return kind


@pytest.mark.asyncio
async def test_a_measured_variant_files_its_verdict(monkeypatch, stub):
    deps = Deps(monkeypatch)
    async with _temp_database("ce_measured") as sessions, sessions() as db:
        agent, _skill, _assignment, experimenter, finding = await seed(db)
        experiment, created = await draft_variant(db, agent, finding)
        assert created is True
        assert finding.status == STATUS_EXPERIMENTING
        again, created_again = await draft_variant(db, agent, finding)
        assert (again.id, created_again) == (experiment.id, False)

        await run_experiment(db, agent, experiment)
        assert experiment.status == STATUS_RUNNING
        assert [c["persona"] for c in deps.calls["create_agent_copy"]] == [None, VARIANT]
        (launch,) = deps.calls["launch"]
        assert (launch["baseline"].kind, launch["variant"].kind) == ("agent", "agent")
        assert launch["dataset_id"] == deps.dataset_id
        # The evaluation reserves the sizing and holds it until it charges its spend.
        assert (launch["tokens"], launch["cost_usd"]) == (experiment.sizing["tokens"], Decimal(experiment.sizing["cost_usd"]))
        (built,) = deps.calls["build_dataset"]
        assert (built["target_type"], built["target_id"], built["max_entries"]) == ("agent", agent.id, 20)
        assert experiment.sizing["dataset_entries"] == 12

        await experiments.settle(db, experiment)
        assert experiment.status == STATUS_COMPLETED
        assert experiment.result["verdict"] == "progression"
        assert experiment.result["simulated_writes"]["total"] == 6

        proposal_id, created = await experiments.propose(
            db, experiment=experiment, finding=finding, author_agent=experimenter
        )
        assert (proposal_id, created) == (deps.proposal_id, True)
        (spec,) = deps.calls["file_proposal"]
        assert spec["author_agent"] is experimenter
        assert (spec["proposal_type"], spec["target_id"]) == ("persona", agent.id)
        assert (spec["base_content"], spec["proposed_content"]) == (BASE, VARIANT)
        assert spec["experiment_verdict"] == "progression"
        assert spec["experiment"]["simulated_writes"] == 6
        assert spec["experiment"]["unmeasured_reason"] is None
        assert spec["finding"]["fingerprint"] == finding.fingerprint
        assert [r["kind"] for r in spec["evidence_refs"]] == ["rating"]
        assert (spec["baseline_score"], spec["base_version_number"]) == (60.0, 1)
        assert finding.status == STATUS_PROPOSED
        assert experiment.proposal_id == deps.proposal_id
        assert await experiments.propose(
            db, experiment=experiment, finding=finding, author_agent=experimenter
        ) == (deps.proposal_id, False)
        assert len(deps.calls["file_proposal"]) == 1


@pytest.mark.asyncio
async def test_no_budget_launches_nothing_and_files_unmeasured(monkeypatch, stub):
    deps = Deps(monkeypatch)
    deps.budget = quality_deps.Budget(tokens=10_000, cost_usd=None, refusal=None)
    async with _temp_database("ce_budget") as sessions, sessions() as db:
        agent, _skill, _assignment, experimenter, finding = await seed(db)
        experiment, _ = await draft_variant(db, agent, finding)
        await run_experiment(db, agent, experiment)
        assert (experiment.status, experiment.status_reason) == (STATUS_SKIPPED, "budget")
        assert experiment.sizing["cases"] == 2
        assert "build_dataset" not in deps.calls
        assert "create_agent_copy" not in deps.calls
        assert "launch" not in deps.calls

        await experiments.propose(db, experiment=experiment, finding=finding, author_agent=experimenter)
        (spec,) = deps.calls["file_proposal"]
        assert spec["experiment_verdict"] == "unmeasured"
        assert spec["experiment"]["unmeasured_reason"] == "budget"
        assert spec["experiment"]["simulated_writes"] is None
        assert spec["evaluation_id"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("setup", "status", "reason"),
    [
        ("refused", STATUS_SKIPPED, "budget"),
        ("no_consent", STATUS_SKIPPED, "no_consent"),
        ("few_cases", STATUS_SKIPPED, "no_dataset"),
        ("not_measurable", STATUS_SKIPPED, "evaluation_unavailable"),
    ],
)
async def test_what_stops_a_launch_is_stated(monkeypatch, stub, setup, status, reason):
    deps = Deps(monkeypatch)
    if setup == "refused":
        deps.budget = quality_deps.Budget(tokens=None, cost_usd=None, refusal="month_budget_reached")
    if setup == "few_cases":
        deps.entries = 3
    stub.measurable = setup != "not_measurable"
    async with _temp_database(f"ce_stop_{setup}") as sessions, sessions() as db:
        agent, _skill, _assignment, _experimenter, finding = await seed(db)
        experiment, _ = await draft_variant(db, agent, finding)
        await run_experiment(db, agent, experiment, content_readable=setup != "no_consent")
        assert (experiment.status, experiment.status_reason) == (status, reason)
        assert "launch" not in deps.calls


@pytest.mark.asyncio
async def test_a_stale_target_is_never_launched_nor_filed(monkeypatch, stub):
    deps = Deps(monkeypatch)
    async with _temp_database("ce_stale") as sessions, sessions() as db:
        agent, _skill, _assignment, experimenter, finding = await seed(db)
        experiment, _ = await draft_variant(db, agent, finding)
        stub.stale = True
        await run_experiment(db, agent, experiment)
        assert experiment.status == STATUS_STALE
        assert "launch" not in deps.calls
        with pytest.raises(experiments.ExperimentError):
            await experiments.propose(db, experiment=experiment, finding=finding, author_agent=experimenter)
        assert "file_proposal" not in deps.calls


@pytest.mark.asyncio
async def test_a_regression_files_nothing_and_an_early_stop_is_no_verdict(monkeypatch, stub):
    deps = Deps(monkeypatch)
    async with _temp_database("ce_regression") as sessions, sessions() as db:
        agent, _skill, _assignment, experimenter, finding = await seed(db)
        experiment, _ = await draft_variant(db, agent, finding)
        await run_experiment(db, agent, experiment)
        deps.compared = dataclasses.replace(deps.compared, verdict="regression", delta=-8.0)
        await experiments.settle(db, experiment)
        assert experiment.result["verdict"] == "regression"
        with pytest.raises(experiments.ExperimentError):
            await experiments.propose(db, experiment=experiment, finding=finding, author_agent=experimenter)

        other, _ = await draft_variant(db, agent, finding, text=VARIANT + " Always.")
        await run_experiment(db, agent, other)
        deps.compared = dataclasses.replace(deps.compared, incomplete=True, verdict=None, reason="budget")
        await experiments.settle(db, other)
        assert (other.status, other.status_reason) == (STATUS_INCOMPLETE, "budget_reached_mid_run")
        assert other.result["verdict"] is None
        await experiments.propose(db, experiment=other, finding=finding, author_agent=experimenter)
        (spec,) = deps.calls["file_proposal"]
        assert spec["experiment_verdict"] == "unmeasured"
        assert spec["experiment"]["unmeasured_reason"] == "budget_reached_mid_run"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("reason", "status", "status_reason"),
    [
        ("budget", STATUS_INCOMPLETE, "budget_reached_mid_run"),
        ("consent", STATUS_INCOMPLETE, "no_consent"),
        ("failed", STATUS_FAILED, "evaluation_unavailable"),
        ("unmeasured", STATUS_FAILED, "evaluation_unavailable"),
    ],
)
async def test_an_arm_with_no_verdict_states_why(monkeypatch, stub, reason, status, status_reason):
    deps = Deps(monkeypatch)
    async with _temp_database(f"ce_noverdict_{reason}") as sessions, sessions() as db:
        agent, _skill, _assignment, _experimenter, finding = await seed(db)
        experiment, _ = await draft_variant(db, agent, finding)
        await run_experiment(db, agent, experiment)
        deps.compared = dataclasses.replace(deps.compared, incomplete=True, verdict=None, reason=reason)
        await experiments.settle(db, experiment)

    # A failed arm is never read as the budget reached.
    assert (experiment.status, experiment.status_reason) == (status, status_reason)


@pytest.mark.asyncio
async def test_the_tick_settles_a_running_experiment_nobody_reads(monkeypatch, stub):
    deps = Deps(monkeypatch)
    async with _temp_database("ce_tick") as sessions:
        async with sessions() as db:
            agent, _skill, _assignment, _experimenter, finding = await seed(db)
            experiment, _ = await draft_variant(db, agent, finding)
            await run_experiment(db, agent, experiment)
            await db.commit()
            deps.compared = dataclasses.replace(deps.compared, finished=False)
            assert await experiments.settle_running_experiments(db) == 0
            deps.compared = dataclasses.replace(deps.compared, finished=True)
            assert await experiments.settle_running_experiments(db) == 1
        async with sessions() as db:
            settled = await db.get(type(experiment), experiment.id)

    assert (settled.status, settled.result["verdict"]) == (STATUS_COMPLETED, "progression")


@dataclasses.dataclass
class _FlowKind:
    """A stand-in for a level 7 flow kind: structured variant, flow subjects."""

    stale: bool = False

    async def prepare_draft(self, db, *, finding, target_id, payload):
        return experiments.DraftSpec(
            target_type="flow",
            subject_agent_id=None,
            base_text="",
            base_version=3,
            variant_text="",
            variant={"sandbox_flow_id": payload["sandbox_flow_id"], "graph": {"nodes": ["big"]}},
            summary=payload["summary"],
        )

    async def check_stale(self, db, experiment):
        return self.stale

    async def dataset_target(self, db, experiment):
        return ("flow", experiment.target_id)

    async def subjects(self, db, experiment, *, created_for, author_agent_id):
        baseline_copy, variant_copy = uuid.uuid4(), uuid.uuid4()
        return (
            quality_deps.Subject(kind="flow", id=uuid.uuid4(), copy_id=baseline_copy),
            quality_deps.Subject(kind="flow", id=uuid.UUID(experiment.variant["sandbox_flow_id"]), copy_id=variant_copy),
        )

    def proposal_content(self, experiment):
        return {"change": {"sandbox_flow_id": experiment.variant["sandbox_flow_id"]}}


@pytest.mark.asyncio
async def test_a_registered_kind_runs_and_files_through_the_same_steps(monkeypatch):
    deps = Deps(monkeypatch)
    monkeypatch.setitem(experiments._KINDS, "flow_repair", _FlowKind())
    async with _temp_database("ce_kind") as sessions, sessions() as db:
        agent, _skill, _assignment, experimenter, finding = await seed(db)
        flow_id, sandbox_flow = uuid.uuid4(), uuid.uuid4()
        experiment, created = await experiments.draft(
            db,
            finding=finding,
            change_type="flow_repair",
            target_id=flow_id,
            payload={"sandbox_flow_id": str(sandbox_flow), "summary": "Retry the failing node."},
            author_agent_id=None,
        )
        assert created is True
        assert (experiment.target_type, experiment.subject_agent_id, experiment.base_version) == ("flow", None, 3)
        assert experiments.experiment_payload(experiment)["variant"] == {"sandbox_flow_id": str(sandbox_flow)}

        await run_experiment(db, agent, experiment, signals={})
        assert experiment.status == STATUS_RUNNING
        (built,) = deps.calls["build_dataset"]
        assert (built["target_type"], built["target_id"]) == ("flow", flow_id)
        (launch,) = deps.calls["launch"]
        assert (launch["baseline"].kind, launch["variant"].kind, launch["variant"].id) == ("flow", "flow", sandbox_flow)

        await experiments.settle(db, experiment)
        await experiments.propose(db, experiment=experiment, finding=finding, author_agent=experimenter)
        (spec,) = deps.calls["file_proposal"]
        assert (spec["proposal_type"], spec["change"]) == ("flow_repair", {"sandbox_flow_id": str(sandbox_flow)})
        assert "base_content" not in spec
        assert spec["experiment_verdict"] == "progression"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (quality_deps.EvaluationUnavailableError("refused"), "evaluation_unavailable"),
        (quality_deps.BudgetUnavailableError("cycle_budget_reached"), "budget"),
    ],
    ids=["evaluation", "budget"],
)
async def test_a_refused_launch_is_stated(monkeypatch, stub, error, reason):
    deps = Deps(monkeypatch)

    async def _refused(_db, **kwargs):
        deps._seen("launch", **kwargs)
        raise error

    monkeypatch.setattr(quality_deps, "launch", _refused)
    async with _temp_database(f"ce_refused_{reason}") as sessions, sessions() as db:
        agent, _skill, _assignment, experimenter, finding = await seed(db)
        experiment, _ = await draft_variant(db, agent, finding)
        await run_experiment(db, agent, experiment)
        assert (experiment.status, experiment.status_reason) == (STATUS_SKIPPED, reason)
        await experiments.propose(db, experiment=experiment, finding=finding, author_agent=experimenter)
        (spec,) = deps.calls["file_proposal"]
        assert (spec["experiment_verdict"], spec["experiment"]["unmeasured_reason"]) == ("unmeasured", reason)


@pytest.mark.asyncio
async def test_the_run_s_cycle_is_read_and_reserved_on(monkeypatch, stub):
    deps = Deps(monkeypatch)
    cycle = uuid.uuid4()
    async with _temp_database("ce_cycle") as sessions, sessions() as db:
        agent, _skill, _assignment, _experimenter, finding = await seed(db)
        experiment, _ = await draft_variant(db, agent, finding)
        await run_experiment(db, agent, experiment, cycle_id=cycle)
        assert experiment.status == STATUS_RUNNING
        assert deps.calls["budget_left"] == [{"cycle_id": cycle}]
        assert [call["cycle_id"] for call in deps.calls["launch"]] == [cycle]


@pytest.mark.asyncio
async def test_no_experiment_runs_outside_a_cycle(monkeypatch, stub):
    deps = Deps(monkeypatch)
    async with _temp_database("ce_nocycle") as sessions, sessions() as db:
        agent, _skill, _assignment, _experimenter, finding = await seed(db)
        experiment, _ = await draft_variant(db, agent, finding)
        with pytest.raises(experiments.ExperimentError):
            await run_experiment(db, agent, experiment, cycle_id=None)

    # Nothing could book its spend: nothing is read, built or launched.
    assert experiment.status == "draft"
    assert deps.calls == {}


@pytest.mark.asyncio
async def test_a_failed_launch_is_stated(monkeypatch, stub):
    deps = Deps(monkeypatch)

    async def _broken(_db, **_kwargs):
        raise RuntimeError("sandbox copy refused")

    monkeypatch.setattr(quality_deps, "create_agent_copy", _broken)
    async with _temp_database("ce_broken") as sessions, sessions() as db:
        agent, _skill, _assignment, _experimenter, finding = await seed(db)
        experiment, _ = await draft_variant(db, agent, finding)
        await run_experiment(db, agent, experiment)
        assert (experiment.status, experiment.status_reason) == (STATUS_SKIPPED, "evaluation_unavailable")
        assert "launch" not in deps.calls


@pytest.mark.asyncio
async def test_a_content_finding_files_only_while_readable_and_with_today_s_refs(monkeypatch, stub):
    deps = Deps(monkeypatch)
    async with _temp_database("ce_consent") as sessions, sessions() as db:
        agent, _skill, _assignment, experimenter, finding = await seed(db)
        experiment, _ = await draft_variant(db, agent, finding)
        await run_experiment(db, agent, experiment)
        await experiments.settle(db, experiment)
        with pytest.raises(experiments.ExperimentError, match="may no longer read"):
            await experiments.propose(
                db, experiment=experiment, finding=finding, author_agent=experimenter, content_readable=False
            )
        assert "file_proposal" not in deps.calls
        kept = str(uuid.uuid4())
        await experiments.propose(
            db,
            experiment=experiment,
            finding=finding,
            author_agent=experimenter,
            evidence={"trace_ids": [], "rating_ids": [kept], "run_ids": [], "counts": {"negative": 9}},
        )
        (spec,) = deps.calls["file_proposal"]
        assert spec["evidence_refs"] == [{"kind": "rating", "id": kept}]
