"""The persona self-improvement loop (#1044) against a real PostgreSQL: the
``persona`` type of the improvement proposals (#8256).

Propose -> approve -> the candidate run ends -> settle, with the model call
replaced by a fixed rewrite. Pinned here: a regression beyond the margin rolls
the persona back through a new persona version and an audited system entry; a
tie inside the margin keeps the rewrite; an evaluation still set to the retired
F1 is judged on the judge's score like any other; a human edit made between the rewrite
and the verdict is kept; an approver who lost the right to manage the agent is
refused; an approval commits together with the execution that runs its
candidate; no baseline, no rewrite; an agent deleted mid-loop takes its cycle
with it and the scheduler step carries on.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import delete, func, select

import app.core.database as database
from app.models.agent import Agent
from app.models.audit_log import AuditLog
from app.models.dataset import Dataset, DatasetEntry, EvaluationDataset
from app.models.evaluation import Evaluation, EvaluationAgent, EvaluationRun
from app.models.improvement_proposal import (
    STATUS_KEPT,
    STATUS_PENDING_APPROVAL,
    STATUS_REGRESSION_KEPT,
    STATUS_ROLLED_BACK,
    STATUS_WATCHING,
    ImprovementProposal,
)
from app.models.orchestration import OrchestrationExecution
from app.models.prompt_version import PromptVersion
from app.models.user import User
from app.services import improvement_proposals as proposals
from app.services import persona_improvement as loop
from app.services.prompt_versioning import create_agent_persona_version, get_active_agent_persona_version_number
from tests.evaluation_helpers import seed_provider
from tests.test_provisioning_postgres import _temp_database

pytestmark = pytest.mark.integration

BASE_PERSONA = "You answer questions about invoices."
REWRITE = "You answer questions about invoices. Always quote the invoice number."


async def _rewrite(_db, _user_id, _agent, evidence):
    assert "weakest_cases" in evidence
    return REWRITE, "Quote the invoice number."


def _user(role: str = "user") -> User:
    return User(
        id=uuid.uuid4(),
        email=f"loop-{uuid.uuid4().hex[:8]}@test.local",
        full_name=f"Loop {role}",
        hashed_password="x",
        role=role,
    )


async def _seed(
    sessionmaker, *, baseline: bool = True, grantee: bool = False, quality_source: str = "judge", f1: float | None = None
) -> dict:
    async with sessionmaker() as db:
        admin, owner, other = _user("admin"), _user(), _user()
        db.add_all([admin, owner, other])
        await db.flush()
        # Shared with the admin, who evaluates it as its owner lent it.
        agent = Agent(
            id=uuid.uuid4(), name="Invoice agent", user_id=owner.id, persona=BASE_PERSONA, shared_user_ids=[str(admin.id)]
        )
        if grantee:
            agent.shared_user_ids = [str(admin.id), str(other.id)]
            agent.shared_user_permissions = {str(other.id): "config"}
        db.add(agent)
        await db.flush()
        version = await create_agent_persona_version(
            db, agent_id=agent.id, content=BASE_PERSONA, author_id=owner.id, commit_message="Initial"
        )
        # Runnable as every launch requires: a family on both, and a
        # provider offering the baseline's model.
        evaluation = Evaluation(name="Invoices", user_id=admin.id, family="PROSE", quality_source=quality_source)
        dataset = Dataset(name="Invoices", user_id=admin.id, family="PROSE")
        db.add_all([evaluation, dataset])
        await db.flush()
        await seed_provider(db, ["model-a"])
        db.add_all(
            [
                EvaluationAgent(evaluation_id=evaluation.id, agent_id=agent.id),
                EvaluationDataset(evaluation_id=evaluation.id, dataset_id=dataset.id),
                DatasetEntry(dataset_id=dataset.id, input_prompt="Total of INV-7?", expected_answer="INV-7: 120 EUR"),
            ]
        )
        if baseline:
            db.add(
                EvaluationRun(
                    evaluation_id=evaluation.id,
                    agent_id=agent.id,
                    model_id="model-a",
                    status="completed",
                    trigger_reason="manual",
                    score=70.0,
                    overall_score=60.0,
                    f1_score=f1,
                    completed_entries=1,
                    total_entries=1,
                    persona_version_number=version.version_number,
                    completed_at=datetime.now(UTC),
                )
            )
        await db.commit()
        return {"admin": admin, "owner": owner, "other": other, "agent": agent, "evaluation": evaluation}


async def _propose(sessionmaker, seeded) -> uuid.UUID:
    async with sessionmaker() as db:
        admin = await db.get(User, seeded["admin"].id)
        agent = await db.get(Agent, seeded["agent"].id)
        evaluation = await db.get(Evaluation, seeded["evaluation"].id)
        improvement = await loop.propose(db, user=admin, agent=agent, evaluation=evaluation, generate=_rewrite)
        await db.commit()
        assert improvement.status == STATUS_PENDING_APPROVAL
        return improvement.id


async def _approve(sessionmaker, improvement_id, approver) -> ImprovementProposal:
    async with sessionmaker() as db:
        user = await db.get(User, approver.id)
        improvement = await proposals.approve(db, user=user, proposal_id=improvement_id)
        await db.commit()
        return improvement


async def _finish_candidate(sessionmaker, improvement_id, score: float) -> None:
    async with sessionmaker() as db:
        improvement = await db.get(ImprovementProposal, improvement_id)
        run = await db.get(EvaluationRun, improvement.candidate_run_id)
        run.status, run.score, run.completed_at = "completed", score, datetime.now(UTC)
        await db.commit()


async def _settle(sessionmaker, monkeypatch) -> int:
    monkeypatch.setattr(database, "async_session", sessionmaker)
    return await proposals.settle_open_proposals()


async def _state(sessionmaker, seeded, improvement_id):
    async with sessionmaker() as db:
        agent = await db.get(Agent, seeded["agent"].id)
        improvement = await db.get(ImprovementProposal, improvement_id) if improvement_id else None
        versions = int(
            await db.scalar(select(func.count()).select_from(PromptVersion).where(PromptVersion.agent_id == agent.id))
        )
        active = await get_active_agent_persona_version_number(db, agent_id=agent.id)
        return agent, improvement, versions, active


@pytest.mark.asyncio
async def test_a_regression_rolls_the_persona_back_through_its_version_history(monkeypatch):
    async with _temp_database("loop_rollback") as sessionmaker:
        seeded = await _seed(sessionmaker)
        improvement_id = await _propose(sessionmaker, seeded)
        applied = await _approve(sessionmaker, improvement_id, seeded["owner"])

        agent, improvement, versions, active = await _state(sessionmaker, seeded, improvement_id)
        assert agent.persona == REWRITE
        assert (improvement.status, versions, active) == (STATUS_WATCHING, 2, applied.applied_version_number)
        async with sessionmaker() as db:
            candidate = await db.get(EvaluationRun, improvement.candidate_run_id)
        # The candidate reruns the baseline's model, pinned to the applied version.
        assert (candidate.status, candidate.model_id, candidate.persona_version_number) == ("pending", "model-a", active)
        # Still running: nothing to settle.
        assert await _settle(sessionmaker, monkeypatch) == 0

        await _finish_candidate(sessionmaker, improvement_id, 67.5)
        assert await _settle(sessionmaker, monkeypatch) == 1

        agent, improvement, versions, active = await _state(sessionmaker, seeded, improvement_id)
        assert improvement.status == STATUS_ROLLED_BACK
        assert (improvement.delta, improvement.verdict) == (-2.5, "regression")
        assert agent.persona == BASE_PERSONA
        # The rollback is a new version, so it is itself reversible.
        assert (versions, active, improvement.rollback_version_number) == (3, 3, 3)
        async with sessionmaker() as db:
            actions = set(
                (await db.execute(select(AuditLog.action).where(AuditLog.entity_id == agent.id))).scalars()
            )
        assert {"update", "rollback"} <= actions
        # Settled once: a second tick changes nothing.
        assert await _settle(sessionmaker, monkeypatch) == 0


@pytest.mark.asyncio
async def test_a_tie_inside_the_margin_keeps_the_rewrite(monkeypatch):
    async with _temp_database("loop_tie") as sessionmaker:
        seeded = await _seed(sessionmaker)
        improvement_id = await _propose(sessionmaker, seeded)
        await _approve(sessionmaker, improvement_id, seeded["owner"])
        await _finish_candidate(sessionmaker, improvement_id, 68.0)  # exactly -2.0

        assert await _settle(sessionmaker, monkeypatch) == 1

        agent, improvement, versions, _active = await _state(sessionmaker, seeded, improvement_id)
        assert (improvement.status, improvement.verdict) == (STATUS_KEPT, "neutral")
        assert (agent.persona, versions) == (REWRITE, 2)


@pytest.mark.asyncio
async def test_an_evaluation_still_set_to_the_retired_f1_is_judged_on_the_judge_score(monkeypatch):
    """No new run records an F1, so a cycle measured on it would never get a
    verdict: an evaluation set to it (an older row, an import or a copy) is
    judged on the judge's score, even when its baseline holds an F1."""
    async with _temp_database("loop_retired_f1") as sessionmaker:
        seeded = await _seed(sessionmaker, quality_source="f1", f1=40.0)
        improvement_id = await _propose(sessionmaker, seeded)
        await _approve(sessionmaker, improvement_id, seeded["owner"])
        await _finish_candidate(sessionmaker, improvement_id, 75.0)

        assert await _settle(sessionmaker, monkeypatch) == 1

        agent, improvement, _versions, _active = await _state(sessionmaker, seeded, improvement_id)
        assert (improvement.quality_metric, improvement.baseline_score, improvement.candidate_score) == (
            "score",
            70.0,
            75.0,
        )
        assert (improvement.status, improvement.verdict, improvement.delta) == (STATUS_KEPT, "progression", 5.0)
        assert agent.persona == REWRITE


@pytest.mark.asyncio
async def test_a_human_edit_before_the_verdict_is_kept(monkeypatch):
    async with _temp_database("loop_edit") as sessionmaker:
        seeded = await _seed(sessionmaker)
        improvement_id = await _propose(sessionmaker, seeded)
        await _approve(sessionmaker, improvement_id, seeded["owner"])
        async with sessionmaker() as db:
            agent = await db.get(Agent, seeded["agent"].id)
            agent.persona = "Edited by hand."
            await create_agent_persona_version(
                db, agent_id=agent.id, content=agent.persona, author_id=seeded["owner"].id
            )
            await db.commit()
        await _finish_candidate(sessionmaker, improvement_id, 40.0)

        assert await _settle(sessionmaker, monkeypatch) == 1

        agent, improvement, versions, _active = await _state(sessionmaker, seeded, improvement_id)
        assert (improvement.status, improvement.verdict) == (STATUS_REGRESSION_KEPT, "regression")
        assert (agent.persona, versions, improvement.rollback_version_number) == ("Edited by hand.", 3, None)


@pytest.mark.asyncio
async def test_a_rollback_outlives_the_account_of_its_approver(monkeypatch):
    """The approver's id carries no key, the rollback version's author does: a
    deleted approver leaves the version without an author instead of failing the
    settlement and keeping the regressed persona."""
    async with _temp_database("loop_gone_approver") as sessionmaker:
        seeded = await _seed(sessionmaker, grantee=True)
        improvement_id = await _propose(sessionmaker, seeded)
        await _approve(sessionmaker, improvement_id, seeded["other"])
        async with sessionmaker() as db:
            await db.execute(delete(User).where(User.id == seeded["other"].id))
            await db.commit()
        await _finish_candidate(sessionmaker, improvement_id, 60.0)

        assert await _settle(sessionmaker, monkeypatch) == 1

        agent, improvement, _versions, active = await _state(sessionmaker, seeded, improvement_id)
        assert (improvement.status, agent.persona) == (STATUS_ROLLED_BACK, BASE_PERSONA)
        assert active == improvement.rollback_version_number
        async with sessionmaker() as db:
            author_id = await db.scalar(
                select(PromptVersion.author_id).where(
                    PromptVersion.agent_id == agent.id,
                    PromptVersion.version_number == improvement.rollback_version_number,
                )
            )
        assert author_id is None


@pytest.mark.asyncio
async def test_an_approver_who_lost_the_right_to_manage_is_refused():
    async with _temp_database("loop_rights") as sessionmaker:
        seeded = await _seed(sessionmaker, grantee=True)
        improvement_id = await _propose(sessionmaker, seeded)
        async with sessionmaker() as db:
            agent = await db.get(Agent, seeded["agent"].id)
            agent.shared_user_ids, agent.shared_user_permissions = [], {}
            await db.commit()

        with pytest.raises(proposals.ProposalError) as caught:
            await _approve(sessionmaker, improvement_id, seeded["other"])
        assert caught.value.status_code == 403
        # Nor does an administrator's bypass approve a private agent.
        with pytest.raises(proposals.ProposalError):
            await _approve(sessionmaker, improvement_id, seeded["admin"])

        agent, improvement, versions, _active = await _state(sessionmaker, seeded, improvement_id)
        assert (improvement.status, agent.persona, versions) == (STATUS_PENDING_APPROVAL, BASE_PERSONA, 1)


@pytest.mark.asyncio
async def test_an_approval_commits_with_the_execution_that_runs_its_candidate():
    """An approval that commits has the candidate's orchestration execution;
    one that is rolled back leaves neither, so no cycle waits on a run nothing
    will start."""
    async with _temp_database("loop_execution") as sessionmaker:
        seeded = await _seed(sessionmaker)
        improvement_id = await _propose(sessionmaker, seeded)
        async with sessionmaker() as db:
            owner = await db.get(User, seeded["owner"].id)
            await proposals.approve(db, user=owner, proposal_id=improvement_id)
            await db.rollback()
        async with sessionmaker() as db:
            assert int(await db.scalar(select(func.count()).select_from(OrchestrationExecution))) == 0
            assert (await db.get(ImprovementProposal, improvement_id)).status == STATUS_PENDING_APPROVAL

        improvement = await _approve(sessionmaker, improvement_id, seeded["owner"])

        async with sessionmaker() as db:
            executions = list((await db.execute(select(OrchestrationExecution))).scalars())
            # A person's request, its baseline on the agent: the candidate runs the agent itself.
            candidate = await db.get(EvaluationRun, improvement.candidate_run_id)
            assert candidate.agent_id == seeded["agent"].id
        payload = {"evaluation_id": str(seeded["evaluation"].id), "trigger_reason": loop.CANDIDATE_TRIGGER_REASON}
        assert [(row.adapter, row.state, row.payload) for row in executions] == [("evaluation", "pending", payload)]
        assert executions[0].deadline_at is not None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("change", "status_code", "reason"),
    [
        pytest.param({"family": None}, 400, "no output family", id="no_family"),
        pytest.param(
            {"trigger_config": {"await_callback": True}}, 400, "needs a resume secret", id="callback_without_secret"
        ),
        pytest.param({"provider": False}, 422, "model-a", id="model_no_provider_offers"),
    ],
)
async def test_an_approval_whose_candidate_the_run_button_would_refuse_is_not_taken(change, status_code, reason):
    """The candidate run passes the checks of every launch, before the persona changes."""
    from app.models.llm_config import LLMProviderConfig

    async with _temp_database("loop_launchable") as sessionmaker:
        seeded = await _seed(sessionmaker)
        improvement_id = await _propose(sessionmaker, seeded)
        async with sessionmaker() as db:
            evaluation = await db.get(Evaluation, seeded["evaluation"].id)
            for field in ("family", "trigger_config"):
                if field in change:
                    setattr(evaluation, field, change[field])
            if change.get("provider") is False:
                await db.execute(delete(LLMProviderConfig))
            await db.commit()

        with pytest.raises(proposals.ProposalError) as caught:
            await _approve(sessionmaker, improvement_id, seeded["owner"])

        assert caught.value.status_code == status_code
        assert reason in caught.value.detail
        agent, improvement, versions, _active = await _state(sessionmaker, seeded, improvement_id)
        assert (agent.persona, improvement.status, versions) == (BASE_PERSONA, STATUS_PENDING_APPROVAL, 1)


@pytest.mark.asyncio
async def test_no_baseline_no_rewrite():
    async with _temp_database("loop_baseline") as sessionmaker:
        seeded = await _seed(sessionmaker, baseline=False)
        with pytest.raises(proposals.ProposalError) as caught:
            await _propose(sessionmaker, seeded)
        assert caught.value.status_code == 422
        async with sessionmaker() as db:
            assert int(await db.scalar(select(func.count()).select_from(ImprovementProposal))) == 0


@pytest.mark.asyncio
async def test_one_open_cycle_per_agent():
    async with _temp_database("loop_open") as sessionmaker:
        seeded = await _seed(sessionmaker)
        await _propose(sessionmaker, seeded)
        with pytest.raises(proposals.ProposalError) as caught:
            await _propose(sessionmaker, seeded)
        assert caught.value.status_code == 409


@pytest.mark.asyncio
async def test_an_agent_deleted_mid_loop_takes_its_cycle_with_it(monkeypatch):
    async with _temp_database("loop_deleted") as sessionmaker:
        seeded = await _seed(sessionmaker)
        improvement_id = await _propose(sessionmaker, seeded)
        await _approve(sessionmaker, improvement_id, seeded["owner"])
        async with sessionmaker() as db:
            await db.execute(delete(Agent).where(Agent.id == seeded["agent"].id))
            await db.commit()

        assert await _settle(sessionmaker, monkeypatch) == 0
        async with sessionmaker() as db:
            assert await db.get(ImprovementProposal, improvement_id) is None
