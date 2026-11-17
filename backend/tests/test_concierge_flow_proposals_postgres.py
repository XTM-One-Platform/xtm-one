"""The flow proposal handlers through level 3's real filing and approval (#8260), on PostgreSQL.

Level 2's real sandbox copies, level 3's ``file_proposal`` / ``approve``:

* an approved flow repair writes the measured graph as a new flow version by
  the approver, audited, and its rollback restores the graph it replaced;
* a person who may not manage the flow cannot approve it;
* a flow edited after the measurement makes the approval stale (409);
* a graph naming an agent the approver may not use is refused (422): the
  approver is the writer, as on the canvas;
* an approved agent-to-flow replacement starts a switch-over shadow on a
  fresh copy of exactly the approved graph, never on the Flow Lab's working
  copy, and nothing reaches production yet.
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager

import pytest
from sqlalchemy import func, select

import app.models  # noqa: F401 - registers every mapper on Base.metadata
from app.models.agent import Agent
from app.models.assignment import Assignment
from app.models.audit_log import AuditLog
from app.models.flow import Flow, FlowVersion
from app.models.flow_shadow import SHADOW_PURPOSE_SWITCH_OVER, FlowShadow
from app.models.user import User
from app.services.concierge.sandbox.copies import SandboxOverrides, create_sandbox_copy
from app.services.flow_graph import auto_layout, normalize_graph
from app.services.improvement_proposals import ProposalError, ProposalSpec, approve, file_proposal, handler_for
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration


def _graph(cron: str, operator: str = "is_not_empty") -> dict:
    return {
        "nodes": [
            {"id": "t1", "kind": "trigger.schedule", "position": {"x": 0, "y": 0}, "config": {"cron": cron}},
            {"id": "c1", "kind": "condition.if", "position": {"x": 0, "y": 100}, "config": {"left": "x", "operator": operator}},
        ],
        "edges": [{"id": "e1", "source": "t1", "source_handle": "out", "target": "c1", "target_handle": "in", "kind": "data"}],
        "viewport": {"x": 0, "y": 0, "zoom": 1},
    }


BASE = _graph("0 8 * * *")
REPAIRED = _graph("0 8 * * *", operator="is_empty")


@asynccontextmanager
async def _database():
    async with scratch_database("flowproposals", tables=ALL_TABLES, extensions=("vector",)) as db:
        yield db.sessions


def _user(name: str) -> User:
    return User(id=uuid.uuid4(), email=f"{name}-{uuid.uuid4().hex[:6]}@example.com", full_name=name, hashed_password="x", role="user")


async def _seed(sessions) -> dict:
    async with sessions() as db:
        owner, stranger = _user("owner"), _user("stranger")
        db.add_all([owner, stranger])
        await db.flush()
        flow = Flow(id=uuid.uuid4(), user_id=owner.id, name="Triage flow", graph=BASE, version_number=1)
        experimenter = Agent(id=uuid.uuid4(), name="Experimenter", user_id=owner.id, is_company_managed=True)
        agent = Agent(id=uuid.uuid4(), name="Report agent", user_id=owner.id)
        foreign = Agent(id=uuid.uuid4(), name="Someone else's agent", user_id=stranger.id)
        db.add_all([flow, experimenter, agent, foreign])
        await db.flush()
        # Every saved flow has its first version snapshot.
        db.add(FlowVersion(flow_id=flow.id, version_number=1, graph=BASE, author_id=owner.id))
        assignment = Assignment(
            id=uuid.uuid4(), agent_id=agent.id, user_id=owner.id, name="Daily report", trigger_type="schedule",
            trigger_config={"cron": "0 8 * * *"}, prompt="Send the report.", enabled=True,
        )
        db.add(assignment)
        await db.commit()
        return {"owner": owner.id, "stranger": stranger.id, "foreign_agent": foreign.id, "flow": flow.id, "experimenter": experimenter.id, "assignment": assignment.id}


async def _file(sessions, seeded: dict, *, proposal_type: str, target_id, source_id, graph: dict, base_version=None) -> uuid.UUID:
    async with sessions() as db:
        copy = await create_sandbox_copy(
            db, source_type="flow", source_id=source_id, created_for="test", overrides=SandboxOverrides(graph=graph)
        )
        filed = await file_proposal(
            db,
            spec=ProposalSpec(
                proposal_type=proposal_type,
                target_id=target_id,
                change={"sandbox_flow_id": str(copy.copy_flow_id), "graph": graph},
                summary="Fix the condition.",
                finding={"fingerprint": uuid.uuid4().hex},
                experiment_verdict="progression",
                base_version_number=base_version,
            ),
            author_agent=await db.get(Agent, seeded["experimenter"]),
        )
        await db.commit()
        return filed.proposal.id, copy.copy_flow_id


async def _approve(sessions, user_id, proposal_id):
    async with sessions() as db:
        proposal = await approve(db, user=await db.get(User, user_id), proposal_id=proposal_id)
        await db.commit()
        return proposal.status


async def test_an_approved_repair_writes_a_version_and_its_rollback_restores_the_graph():
    async with _database() as sessions:
        seeded = await _seed(sessions)
        proposal_id, _ = await _file(
            sessions, seeded, proposal_type="flow_repair", target_id=seeded["flow"], source_id=seeded["flow"], graph=REPAIRED, base_version=1
        )

        assert await _approve(sessions, seeded["owner"], proposal_id) == "watching"

        async with sessions() as db:
            flow = await db.get(Flow, seeded["flow"])
            assert flow.version_number == 2
            assert flow.graph == normalize_graph(auto_layout(REPAIRED))
            version = await db.scalar(select(FlowVersion).where(FlowVersion.flow_id == flow.id, FlowVersion.version_number == 2))
            assert version.author_id == seeded["owner"]
            actions = list((await db.execute(select(AuditLog.action, AuditLog.entity_type).where(AuditLog.entity_id == flow.id))).all())
            assert actions == [("update", "flow")]

            from app.models.improvement_proposal import ImprovementProposal

            proposal = await db.get(ImprovementProposal, proposal_id)
            handler = handler_for("flow_repair")
            assert await handler.rollback(db, proposal, flow, "regressed") == 3
            await db.commit()
            assert flow.graph == BASE


async def test_a_person_who_may_not_manage_the_flow_cannot_approve():
    async with _database() as sessions:
        seeded = await _seed(sessions)
        proposal_id, _ = await _file(
            sessions, seeded, proposal_type="flow_repair", target_id=seeded["flow"], source_id=seeded["flow"], graph=REPAIRED, base_version=1
        )
        with pytest.raises(ProposalError) as refused:
            await _approve(sessions, seeded["stranger"], proposal_id)
        assert refused.value.status_code in (403, 404)
        async with sessions() as db:
            assert (await db.get(Flow, seeded["flow"])).version_number == 1


async def test_a_flow_edited_after_the_measurement_is_stale():
    async with _database() as sessions:
        seeded = await _seed(sessions)
        proposal_id, _ = await _file(
            sessions, seeded, proposal_type="flow_optimization", target_id=seeded["flow"], source_id=seeded["flow"], graph=REPAIRED, base_version=1
        )
        async with sessions() as db:
            (await db.get(Flow, seeded["flow"])).version_number = 2
            await db.commit()
        with pytest.raises(ProposalError) as refused:
            await _approve(sessions, seeded["owner"], proposal_id)
        assert refused.value.status_code == 409


async def test_an_approved_replacement_starts_a_shadow_on_a_fresh_copy():
    async with _database() as sessions:
        seeded = await _seed(sessions)
        prototype = _graph("0 8 * * *")
        proposal_id, working_copy = await _file(
            sessions, seeded, proposal_type="agent_to_flow", target_id=seeded["assignment"], source_id=None, graph=prototype
        )
        async with sessions() as db:
            flows_before = await db.scalar(select(func.count()).select_from(Flow))

        assert await _approve(sessions, seeded["owner"], proposal_id) == "shadow"

        async with sessions() as db:
            shadow = await db.scalar(select(FlowShadow).where(FlowShadow.proposal_id == proposal_id))
            assert shadow.purpose == SHADOW_PURPOSE_SWITCH_OVER
            assert shadow.started_by_user_id == seeded["owner"]
            assert shadow.flow_id != working_copy
            copy = await db.get(Flow, shadow.flow_id)
            assert copy.is_sandbox is True
            assert copy.graph == normalize_graph(auto_layout(prototype))
            assert shadow.production_flow_id is None
            # One new flow: the fresh sandbox copy; production has no new flow yet.
            assert await db.scalar(select(func.count()).select_from(Flow)) == flows_before + 1
            assignment = await db.get(Assignment, seeded["assignment"])
            assert assignment.enabled is True


async def test_a_graph_naming_an_agent_the_approver_may_not_use_is_refused():
    async with _database() as sessions:
        seeded = await _seed(sessions)
        graph = _graph("0 8 * * *")
        graph["nodes"].append(
            {"id": "a1", "kind": "agent", "position": {"x": 0, "y": 200}, "config": {"agent_id": str(seeded["foreign_agent"]), "prompt": "Go"}}
        )
        graph["edges"].append({"id": "e2", "source": "t1", "source_handle": "out", "target": "a1", "target_handle": "in", "kind": "data"})
        proposal_id, _ = await _file(
            sessions, seeded, proposal_type="flow_repair", target_id=seeded["flow"], source_id=seeded["flow"], graph=graph, base_version=1
        )
        with pytest.raises(ProposalError) as refused:
            await _approve(sessions, seeded["owner"], proposal_id)
        assert refused.value.status_code == 422
        async with sessions() as db:
            flow = await db.get(Flow, seeded["flow"])
            assert (flow.version_number, flow.graph) == (1, BASE)


async def test_the_switch_over_refuses_a_copy_changed_after_approval():
    from app.services.concierge.flow_switch_over import _is_the_approved_graph

    async with _database() as sessions:
        seeded = await _seed(sessions)
        proposal_id, _ = await _file(
            sessions, seeded, proposal_type="agent_to_flow", target_id=seeded["assignment"], source_id=None, graph=_graph("0 8 * * *")
        )
        await _approve(sessions, seeded["owner"], proposal_id)
        async with sessions() as db:
            shadow = await db.scalar(select(FlowShadow).where(FlowShadow.proposal_id == proposal_id))
            copy = await db.get(Flow, shadow.flow_id)
            assert await _is_the_approved_graph(db, shadow, copy) is True
            copy.graph = _graph("0 8 * * *", operator="is_empty")
            assert await _is_the_approved_graph(db, shadow, copy) is False
