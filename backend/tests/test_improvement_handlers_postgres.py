"""The ``skill`` and ``assignment_prompt`` improvement proposals against a real PostgreSQL (#8256).

Pinned here: who decides (the target's owner, a ``config`` grantee directly or
through a group; not a ``read_only`` grantee, a revoked one, a stranger, nor an
administrator on a private target; administrators only on a company-managed
one), what applying writes (a new skill version, the assignment's prompt,
each audited), that a live regression puts the text back, that a human edit
meanwhile is kept, that a change of a URL-sourced skill is refused at filing,
that a target edited after filing is refused at approval, and that a skill
change naming an evaluation queues its re-evaluation on the agent its baseline
ran - a fresh sandbox copy of its source when the baseline ran a copy, never
the production agent; the persona type alike - whose regression rolls the
skill back, and that a change with no like-for-like candidate (source gone,
skill dropped, copy out of the supervision scope) goes unmeasured, saying why.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import delete, func, select

from app.api.v1.helpers import get_user_group_ids
from app.models.agent import Agent
from app.models.agent_skill import AgentSkill
from app.models.assignment import Assignment
from app.models.audit_log import AuditLog
from app.models.dataset import Dataset, DatasetEntry, EvaluationDataset
from app.models.evaluation import Evaluation, EvaluationRun
from app.models.group import Group, GroupMember
from app.models.improvement_proposal import (
    STATUS_FAILED,
    STATUS_REGRESSION_KEPT,
    STATUS_ROLLED_BACK,
    STATUS_WATCHING,
    ImprovementProposal,
)
from app.models.sandbox_copy import SandboxCopy
from app.models.skill_version import SkillVersion
from app.models.user import User
from app.services import improvement_proposals as proposals
from app.services.improvement_proposals import candidate as candidate_module
from app.services.improvement_proposals import registry
from app.services.skill_versioning import create_skill_version
from tests.evaluation_helpers import seed_provider
from tests.test_provisioning_postgres import _temp_database

pytestmark = pytest.mark.integration

BASE = "Answer with the invoice number first."
CHANGE = "Answer with the invoice number first, then the total."


def _user(role: str = "user") -> User:
    return User(
        id=uuid.uuid4(),
        email=f"handler-{uuid.uuid4().hex[:8]}@test.local",
        full_name=f"Handler {role}",
        hashed_password="x",
        role=role,
    )


async def _seed(sessionmaker) -> dict:
    async with sessionmaker() as db:
        users = {name: _user("admin" if name == "admin" else "user") for name in (
            "owner", "admin", "stranger", "config", "read_only", "member"
        )}
        db.add_all(users.values())
        await db.flush()
        group = Group(id=uuid.uuid4(), name=f"Billing {uuid.uuid4().hex[:6]}")
        db.add(group)
        await db.flush()
        db.add(GroupMember(group_id=group.id, user_id=users["member"].id))
        shares = {
            "shared_user_ids": [str(users["config"].id), str(users["read_only"].id)],
            "shared_user_permissions": {str(users["config"].id): "config", str(users["read_only"].id): "read_only"},
            "shared_group_ids": [str(group.id)],
            "shared_group_permissions": {str(group.id): "config"},
        }
        skill = AgentSkill(id=uuid.uuid4(), name="Invoices", user_id=users["owner"].id, content=BASE, **shares)
        agent = Agent(id=uuid.uuid4(), name="Invoice agent", user_id=users["owner"].id, persona="P", **shares)
        db.add_all([skill, agent])
        await db.flush()
        await create_skill_version(db, skill, author_id=users["owner"].id, commit_message="Initial")
        assignment = Assignment(
            id=uuid.uuid4(), agent_id=agent.id, user_id=users["owner"].id, name="Nightly", trigger_type="manual", prompt=BASE
        )
        db.add(assignment)
        await db.commit()
        return {**users, "skill": skill, "agent": agent, "assignment": assignment}


def _target_id(seeded, proposal_type):
    return seeded["skill"].id if proposal_type == "skill" else seeded["assignment"].id


async def _file(sessionmaker, seeded, proposal_type, *, base=BASE) -> uuid.UUID:
    async with sessionmaker() as db:
        filed = await proposals.file_proposal(
            db,
            spec=proposals.ProposalSpec(
                proposal_type=proposal_type,
                target_id=_target_id(seeded, proposal_type),
                base_content=base,
                proposed_content=CHANGE,
                summary="Lead with the total too.",
                evidence_refs=[{"kind": "trace", "id": "t-1"}],
                experiment_verdict="progression",
            ),
            author_agent=SimpleNamespace(id=uuid.uuid4(), user_id=seeded["admin"].id),
        )
        await db.commit()
        return filed.proposal.id


async def _approve(sessionmaker, proposal_id, who):
    async with sessionmaker() as db:
        proposal = await proposals.approve(db, user=await db.get(User, who.id), proposal_id=proposal_id)
        await db.commit()
        return proposal


async def _text(sessionmaker, seeded, proposal_type) -> str:
    async with sessionmaker() as db:
        if proposal_type == "skill":
            return (await db.get(AgentSkill, seeded["skill"].id)).content
        return (await db.get(Assignment, seeded["assignment"].id)).prompt


@pytest.mark.asyncio
@pytest.mark.parametrize("proposal_type", ["skill", "assignment_prompt"])
async def test_who_decides_a_change(proposal_type):
    async with _temp_database(f"handler_who_{proposal_type[:5]}") as sessionmaker:
        seeded = await _seed(sessionmaker)
        handler = proposals.handler_for(proposal_type)

        async def _decides(name) -> bool:
            async with sessionmaker() as db:
                target = await handler.load_target(db, _target_id(seeded, proposal_type), lock=False)
                user = await db.get(User, seeded[name].id)
                return proposals.approval_error(handler, target, user, await get_user_group_ids(db, user.id)) is None

        decides = {name: await _decides(name) for name in ("owner", "config", "member", "read_only", "stranger", "admin")}
        # No administrator bypass on a private target (B11).
        assert decides == {
            "owner": True, "config": True, "member": True, "read_only": False, "stranger": False, "admin": False
        }

        async with sessionmaker() as db:
            for model, row_id in ((AgentSkill, seeded["skill"].id), (Agent, seeded["agent"].id)):
                row = await db.get(model, row_id)
                row.shared_user_ids, row.shared_user_permissions = [], {}
            await db.commit()
        # A revoked share decides nothing, at once.
        assert await _decides("config") is False

        async with sessionmaker() as db:
            for model, row_id in ((AgentSkill, seeded["skill"].id), (Agent, seeded["agent"].id), (Assignment, seeded["assignment"].id)):
                (await db.get(model, row_id)).is_company_managed = True
            await db.commit()
        # Company-managed: administrators, and nobody else.
        assert (await _decides("admin"), await _decides("owner"), await _decides("member")) == (True, False, False)


@pytest.mark.asyncio
@pytest.mark.parametrize("proposal_type", ["skill", "assignment_prompt"])
async def test_an_approved_change_is_applied_audited_and_rolled_back_on_a_live_regression(monkeypatch, proposal_type):
    async with _temp_database(f"handler_flow_{proposal_type[:5]}") as sessionmaker:
        seeded = await _seed(sessionmaker)
        proposal_id = await _file(sessionmaker, seeded, proposal_type)
        applied = await _approve(sessionmaker, proposal_id, seeded["owner"])

        assert applied.status == STATUS_WATCHING
        assert await _text(sessionmaker, seeded, proposal_type) == CHANGE
        async with sessionmaker() as db:
            target_id = _target_id(seeded, proposal_type)
            audited = set((await db.execute(select(AuditLog.action).where(AuditLog.entity_id == target_id))).scalars())
            versions = int(await db.scalar(select(func.count()).select_from(SkillVersion).where(SkillVersion.skill_id == target_id)))
        assert "update" in audited
        assert versions == (2 if proposal_type == "skill" else 0)

        async def _regressed(_db, _proposal):
            return proposals.LiveVerdict(regressed=True, note="Its failure rate rose")

        monkeypatch.setitem(registry._WATCHERS, proposal_type, _regressed)
        async with sessionmaker() as db:
            proposal = await db.get(ImprovementProposal, proposal_id)
            result = await proposals.settle(db, proposal, now=proposal.applied_at + timedelta(hours=1))
            await db.commit()

        assert result.status == STATUS_ROLLED_BACK
        assert await _text(sessionmaker, seeded, proposal_type) == BASE
        async with sessionmaker() as db:
            actions = set((await db.execute(select(AuditLog.action).where(AuditLog.entity_id == target_id))).scalars())
        assert "rollback" in actions


@pytest.mark.asyncio
@pytest.mark.parametrize("proposal_type", ["skill", "assignment_prompt"])
async def test_a_human_edit_after_the_change_is_kept(monkeypatch, proposal_type):
    async with _temp_database(f"handler_edit_{proposal_type[:5]}") as sessionmaker:
        seeded = await _seed(sessionmaker)
        proposal_id = await _file(sessionmaker, seeded, proposal_type)
        await _approve(sessionmaker, proposal_id, seeded["owner"])
        async with sessionmaker() as db:
            if proposal_type == "skill":
                skill = await db.get(AgentSkill, seeded["skill"].id)
                skill.content = "Edited by hand."
                await create_skill_version(db, skill, author_id=seeded["owner"].id)
            else:
                (await db.get(Assignment, seeded["assignment"].id)).prompt = "Edited by hand."
            await db.commit()

        async def _regressed(_db, _proposal):
            return proposals.LiveVerdict(regressed=True, note="Its failure rate rose")

        monkeypatch.setitem(registry._WATCHERS, proposal_type, _regressed)
        async with sessionmaker() as db:
            proposal = await db.get(ImprovementProposal, proposal_id)
            result = await proposals.settle(db, proposal, now=proposal.applied_at + timedelta(hours=1))
            await db.commit()

        assert result.status == STATUS_REGRESSION_KEPT
        assert await _text(sessionmaker, seeded, proposal_type) == "Edited by hand."


@pytest.mark.asyncio
@pytest.mark.parametrize("proposal_type", ["skill", "assignment_prompt"])
async def test_a_target_edited_after_filing_is_refused_at_approval(proposal_type):
    async with _temp_database(f"handler_stale_{proposal_type[:5]}") as sessionmaker:
        seeded = await _seed(sessionmaker)
        proposal_id = await _file(sessionmaker, seeded, proposal_type)
        async with sessionmaker() as db:
            if proposal_type == "skill":
                (await db.get(AgentSkill, seeded["skill"].id)).content = "Edited."
            else:
                (await db.get(Assignment, seeded["assignment"].id)).prompt = "Edited."
            await db.commit()

        with pytest.raises(proposals.ProposalError) as caught:
            await _approve(sessionmaker, proposal_id, seeded["owner"])
        assert caught.value.status_code == 409
        assert await _text(sessionmaker, seeded, proposal_type) == "Edited."


@pytest.mark.asyncio
async def test_a_skill_whose_content_comes_from_its_source_is_never_changed():
    async with _temp_database("handler_url_skill") as sessionmaker:
        seeded = await _seed(sessionmaker)
        async with sessionmaker() as db:
            (await db.get(AgentSkill, seeded["skill"].id)).url = "https://skills.test.local/invoices.md"
            await db.commit()

        with pytest.raises(proposals.ProposalError) as caught:
            await _file(sessionmaker, seeded, "skill")
        assert caught.value.status_code == 422


@pytest.mark.asyncio
async def test_an_assignment_deleted_while_watched_ends_the_watch_as_failed():
    """Its agent remains, so no cascade takes the proposal: it must not stay watched forever."""
    async with _temp_database("handler_gone") as sessionmaker:
        seeded = await _seed(sessionmaker)
        proposal_id = await _file(sessionmaker, seeded, "assignment_prompt")
        await _approve(sessionmaker, proposal_id, seeded["owner"])
        async with sessionmaker() as db:
            await db.delete(await db.get(Assignment, seeded["assignment"].id))
            await db.commit()
        async with sessionmaker() as db:
            proposal = await db.get(ImprovementProposal, proposal_id)
            result = await proposals.settle(db, proposal)
            await db.commit()

        assert (result.changed, result.status) == (True, "failed")


async def _measured_world(sessionmaker, seeded, *, baseline_on: str, proposal_type: str = "skill") -> uuid.UUID:
    """The skill on the agent, an evaluation whose baseline ran *baseline_on*
    (the agent or a sandbox copy of it), and the change filed against it."""
    async with sessionmaker() as db:
        agent = await db.get(Agent, seeded["agent"].id)
        agent.skill_ids = [str(seeded["skill"].id)]
        measured = agent
        if baseline_on == "sandbox_copy":
            measured = Agent(
                id=uuid.uuid4(),
                name="Invoice agent (copy)",
                user_id=None,
                is_sandbox=True,
                persona=agent.persona,
                skill_ids=[str(seeded["skill"].id)],
            )
            db.add(measured)
            await db.flush()
            db.add(
                SandboxCopy(
                    source_type="agent",
                    source_id=agent.id,
                    copy_agent_id=measured.id,
                    expires_at=datetime.now(UTC) + timedelta(days=1),
                )
            )
        evaluation = Evaluation(name="Invoices", user_id=seeded["admin"].id, family="PROSE")
        dataset = Dataset(name="Invoices", user_id=seeded["admin"].id, family="PROSE")
        db.add_all([evaluation, dataset])
        await db.flush()
        await seed_provider(db, ["model-a"])
        db.add_all(
            [
                EvaluationDataset(evaluation_id=evaluation.id, dataset_id=dataset.id),
                DatasetEntry(dataset_id=dataset.id, input_prompt="Total of INV-7?", expected_answer="120 EUR"),
            ]
        )
        baseline = EvaluationRun(
            evaluation_id=evaluation.id,
            agent_id=measured.id,
            model_id="model-a",
            status="completed",
            trigger_reason="manual",
            score=70.0,
            completed_entries=1,
            total_entries=1,
            completed_at=datetime.now(UTC),
        )
        db.add(baseline)
        await db.flush()
        persona = proposal_type == "persona"
        filed = await proposals.file_proposal(
            db,
            spec=proposals.ProposalSpec(
                proposal_type=proposal_type,
                target_id=agent.id if persona else seeded["skill"].id,
                base_content=agent.persona if persona else BASE,
                proposed_content=f"{agent.persona} Quote the invoice number." if persona else CHANGE,
                evidence_refs=[{"kind": "trace", "id": "t-1"}],
                experiment_verdict="progression",
                evaluation_id=evaluation.id,
                baseline_run_id=baseline.id,
                baseline_score=70.0,
            ),
            author_agent=SimpleNamespace(id=uuid.uuid4(), user_id=seeded["admin"].id),
        )
        await db.commit()
        return filed.proposal.id


def _in_scope(monkeypatch) -> None:
    """Platform Health supervises the agent: its sandbox copy may be evaluated."""
    from app.services.health import evaluations

    async def in_supervision_scope(_db, _target_type, _target_id):
        return True

    monkeypatch.setattr(evaluations, "in_supervision_scope", in_supervision_scope)


async def _candidate_subject(sessionmaker, seeded, candidate_run_id) -> tuple[Agent, SandboxCopy | None]:
    async with sessionmaker() as db:
        candidate = await db.get(EvaluationRun, candidate_run_id)
        assert candidate.status == "pending"
        subject = await db.get(Agent, candidate.agent_id)
        copy = await db.scalar(select(SandboxCopy).where(SandboxCopy.copy_agent_id == subject.id))
        return subject, copy


@pytest.mark.asyncio
@pytest.mark.parametrize("baseline_on", ["agent", "sandbox_copy"])
async def test_a_measured_skill_change_queues_its_re_evaluation(monkeypatch, baseline_on):
    """A baseline on the agent re-runs the agent; one on a sandbox copy re-runs
    a fresh copy of its source, never the production agent, which would write
    again for real; the copy is kept until the verdict is read."""
    _in_scope(monkeypatch)
    async with _temp_database(f"handler_measured_{baseline_on[:5]}") as sessionmaker:
        seeded = await _seed(sessionmaker)
        proposal_id = await _measured_world(sessionmaker, seeded, baseline_on=baseline_on)

        applied = await _approve(sessionmaker, proposal_id, seeded["owner"])

        assert applied.candidate_run_id is not None
        subject, copy = await _candidate_subject(sessionmaker, seeded, applied.candidate_run_id)
        if baseline_on == "agent":
            assert (subject.id, copy) == (seeded["agent"].id, None)
        else:
            assert subject.id != seeded["agent"].id
            assert subject.is_sandbox and (copy.source_type, copy.source_id) == ("agent", seeded["agent"].id)
            assert subject.skill_ids == [str(seeded["skill"].id)]
            async with sessionmaker() as db:
                assert await candidate_module.copies_awaiting_a_verdict(db, [copy.id]) == {copy.id}
        async with sessionmaker() as db:
            candidate = await db.get(EvaluationRun, applied.candidate_run_id)
            candidate.status, candidate.score = "completed", 60.0
            await db.commit()
        async with sessionmaker() as db:
            result = await proposals.settle(db, await db.get(ImprovementProposal, proposal_id))
            await db.commit()

        assert result.status == STATUS_ROLLED_BACK
        assert await _text(sessionmaker, seeded, "skill") == BASE
        async with sessionmaker() as db:
            kinds = set(
                (await db.execute(select(AuditLog.entity_type).where(AuditLog.entity_id == seeded["skill"].id))).scalars()
            )
            if copy is not None:
                # Read: the purge may take the copy at its expiry.
                assert await candidate_module.copies_awaiting_a_verdict(db, [copy.id]) == set()
        # The Agent skill filter of the audit log finds the change and its rollback.
        assert kinds == {"agent_skill"}


@pytest.mark.asyncio
async def test_an_agent_filed_persona_rewrite_is_re_evaluated_on_a_fresh_copy(monkeypatch):
    """The persona type shares the rule: its baseline ran a copy, its candidate
    runs a fresh copy carrying the approved rewrite."""
    _in_scope(monkeypatch)
    async with _temp_database("handler_measured_persona") as sessionmaker:
        seeded = await _seed(sessionmaker)
        proposal_id = await _measured_world(sessionmaker, seeded, baseline_on="sandbox_copy", proposal_type="persona")

        applied = await _approve(sessionmaker, proposal_id, seeded["owner"])

        assert applied.candidate_run_id is not None
        subject, copy = await _candidate_subject(sessionmaker, seeded, applied.candidate_run_id)
        assert subject.id != seeded["agent"].id and subject.is_sandbox
        assert copy.source_id == seeded["agent"].id
        assert subject.persona == "P Quote the invoice number."


@pytest.mark.asyncio
@pytest.mark.parametrize("gone", ["source_deleted", "skill_dropped", "out_of_scope"])
async def test_a_change_with_no_like_for_like_candidate_goes_unmeasured(monkeypatch, gone):
    """No fallback on the production agent: the change applies, nothing runs,
    and the watch ends with no verdict, saying why."""
    if gone != "out_of_scope":
        _in_scope(monkeypatch)
    async with _temp_database(f"handler_unlike_{gone[:6]}") as sessionmaker:
        seeded = await _seed(sessionmaker)
        proposal_id = await _measured_world(sessionmaker, seeded, baseline_on="sandbox_copy")
        async with sessionmaker() as db:
            if gone == "source_deleted":
                await db.execute(delete(Agent).where(Agent.id == seeded["agent"].id))
            elif gone == "skill_dropped":
                (await db.get(Agent, seeded["agent"].id)).skill_ids = []
            await db.commit()

        applied = await _approve(sessionmaker, proposal_id, seeded["owner"])

        assert (applied.status, applied.candidate_run_id) == (STATUS_WATCHING, None)
        assert applied.outcome_note.startswith("Not re-evaluated: ")
        assert await _text(sessionmaker, seeded, "skill") == CHANGE
        async with sessionmaker() as db:
            runs = int(await db.scalar(select(func.count()).select_from(EvaluationRun)))
            copies = int(await db.scalar(select(func.count()).select_from(SandboxCopy)))
            proposal = await db.get(ImprovementProposal, proposal_id)
            result = await proposals.settle(db, proposal, now=applied.watch_until + timedelta(seconds=1))
            await db.commit()
        # The baseline alone, and the baseline's copy alone: nothing left behind.
        assert (runs, copies) == (1, 1)
        assert result.status == STATUS_FAILED
        assert "Nothing measured" in proposal.outcome_note and "Not re-evaluated: " in proposal.outcome_note
        assert await _text(sessionmaker, seeded, "skill") == CHANGE


@pytest.mark.asyncio
@pytest.mark.parametrize("proposal_type", ["skill", "assignment_prompt"])
async def test_an_unmeasured_change_ends_its_watch_with_no_verdict(proposal_type):
    async with _temp_database(f"handler_unread_{proposal_type[:5]}") as sessionmaker:
        seeded = await _seed(sessionmaker)
        proposal_id = await _file(sessionmaker, seeded, proposal_type)
        applied = await _approve(sessionmaker, proposal_id, seeded["owner"])
        async with sessionmaker() as db:
            proposal = await db.get(ImprovementProposal, proposal_id)
            result = await proposals.settle(db, proposal, now=applied.watch_until + timedelta(seconds=1))
            await db.commit()

        assert (result.changed, result.status) == (True, "failed")
        assert await _text(sessionmaker, seeded, proposal_type) == CHANGE
