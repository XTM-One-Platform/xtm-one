"""The Flow Lab's diagnostics against a real PostgreSQL (#8260).

The readers of the modes on real rows: a flow's diagnosis (content withheld
per run, its failures still counted), an assignment's path and recurring tool
sequences. The seed (owner, grantees, a group, a company-managed flow, sandbox
copies) is shared with the access rule's suite.
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest

import app.models  # noqa: F401 - registers every mapper on Base.metadata
from app.models.agent import Agent
from app.models.assignment import Assignment, AssignmentRun
from app.models.flow import Flow
from app.models.flow_run import FlowRun, FlowRunStep
from app.models.group import Group, GroupMember
from app.models.llm_trace import LLMSpan, LLMTrace
from app.models.user import User
from app.services.health.flow_lab import assignment_path, diagnose_flow, repeated_sequences
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration


@asynccontextmanager
async def _database():
    async with scratch_database("flowlab", tables=ALL_TABLES, extensions=("vector",)) as db:
        yield db.sessions


def _user(name: str, role: str = "user") -> User:
    return User(
        id=uuid.uuid4(), email=f"{name}-{uuid.uuid4().hex[:6]}@example.com", full_name=name, hashed_password="x", role=role
    )


async def _seed(sessions) -> dict:
    async with sessions() as db:
        users = {name: _user(name) for name in ("owner", "stranger", "reader", "editor", "member", "revoked")}
        users["admin"] = _user("admin", role="admin")
        db.add_all(users.values())
        await db.flush()
        group = Group(id=uuid.uuid4(), name=f"ops-{uuid.uuid4().hex[:6]}")
        db.add(group)
        await db.flush()
        db.add(GroupMember(group_id=group.id, user_id=users["member"].id))
        flow = Flow(
            id=uuid.uuid4(),
            user_id=users["owner"].id,
            name="Triage flow",
            graph={},
            shared_user_ids=[str(users["reader"].id), str(users["editor"].id)],
            shared_user_permissions={str(users["reader"].id): "read_only", str(users["editor"].id): "config"},
            shared_group_ids=[str(group.id)],
            shared_group_permissions={str(group.id): "config"},
        )
        company = Flow(id=uuid.uuid4(), user_id=users["owner"].id, name="Company flow", graph={}, is_company_managed=True)
        sandbox = Flow(id=uuid.uuid4(), user_id=None, name="Sandbox of triage", graph={}, is_sandbox=True)
        agent = Agent(
            id=uuid.uuid4(),
            name="Triage agent",
            user_id=users["owner"].id,
            shared_user_ids=[str(users["reader"].id), str(users["editor"].id)],
            shared_user_permissions={str(users["reader"].id): "read_only", str(users["editor"].id): "config"},
        )
        db.add_all([flow, company, sandbox, agent])
        await db.flush()
        assignment = Assignment(
            id=uuid.uuid4(),
            agent_id=agent.id,
            user_id=users["owner"].id,
            name="Nightly triage",
            trigger_type="schedule",
            trigger_config={"cron": "0 2 * * *"},
            prompt="Triage the new alerts.",
        )
        db.add(assignment)
        await db.flush()
        from app.models.sandbox_copy import SandboxCopy

        # The Flow Lab's own copy (``create_sandbox_flow``), and one of another purpose.
        proposal_copy = Flow(id=uuid.uuid4(), user_id=None, name="Approved copy", graph={}, is_sandbox=True)
        db.add(proposal_copy)
        await db.flush()
        expires = datetime.now(UTC) + timedelta(days=14)
        db.add_all(
            [
                SandboxCopy(source_type="flow", source_id=flow.id, copy_flow_id=sandbox.id, created_for="flow_lab", expires_at=expires),
                SandboxCopy(source_type="flow", copy_flow_id=proposal_copy.id, created_for="proposal:x", expires_at=expires),
            ]
        )
        await db.commit()
        return {
            "proposal_copy": proposal_copy.id,
            "users": {name: u.id for name, u in users.items()},
            "flow": flow.id,
            "company": company.id,
            "sandbox": sandbox.id,
            "agent": agent.id,
            "assignment": assignment.id,
        }


async def _runs(sessions, seeded: dict) -> dict:
    now = datetime.now(UTC)
    owner = seeded["users"]["owner"]
    stranger = seeded["users"]["stranger"]
    async with sessions() as db:
        failed = FlowRun(
            flow_id=seeded["flow"], graph={}, user_id=owner, status="failed", trigger_type="manual",
            trigger_payload={"payload": {"alert": "A-1"}}, created_at=now - timedelta(hours=2),
            started_at=now - timedelta(hours=2), completed_at=now - timedelta(hours=2) + timedelta(seconds=3),
        )
        other = FlowRun(
            flow_id=seeded["flow"], graph={}, user_id=stranger, status="completed", trigger_type="manual",
            trigger_payload={"payload": {"alert": "A-2"}}, result_summary="Closed.", created_at=now - timedelta(hours=1),
            started_at=now - timedelta(hours=1), completed_at=now - timedelta(hours=1) + timedelta(seconds=1),
        )
        db.add_all([failed, other])
        await db.flush()
        db.add_all(
            [
                FlowRunStep(
                    flow_run_id=failed.id, seq=0, node_id="http", node_kind="action.http_request", status="failed",
                    error="HTTP 500 from https://intranet.example.com/secret", started_at=failed.started_at,
                    completed_at=failed.started_at + timedelta(milliseconds=800),
                ),
                FlowRunStep(
                    flow_run_id=other.id, seq=0, node_id="http", node_kind="action.http_request", status="failed",
                    error="Token expired for alice@example.com", started_at=other.started_at,
                    completed_at=other.started_at + timedelta(milliseconds=200),
                ),
                LLMTrace(
                    id=uuid.uuid4(), source="flow", name="flow-run:Triage flow", started_at=other.started_at,
                    metadata_json={"flow_run_id": str(other.id)}, input_tokens=100, output_tokens=40, total_cost=0.25,
                ),
            ]
        )
        await db.commit()
        return {"failed": failed.id, "other": other.id}


async def test_a_diagnosis_withholds_the_content_of_runs_consent_does_not_admit():
    async with _database() as sessions:
        seeded = await _seed(sessions)
        runs = await _runs(sessions, seeded)
        async with sessions() as db:
            flow = await db.get(Flow, seeded["flow"])

            async def only_the_owners_runs(run):
                return run.user_id == seeded["users"]["owner"]

            diagnosis = await diagnose_flow(db, flow, content_allowed=only_the_owners_runs)
        assert (diagnosis.runs, diagnosis.failed, diagnosis.completed, diagnosis.withheld) == (2, 1, 1, 1)
        assert diagnosis.failure_rate == 0.5
        assert [(c.run_id, c.status) for c in diagnosis.cases] == [(runs["failed"], "failed"), (runs["other"], "completed")]
        assert [c.trigger_payload for c in diagnosis.cases] == [{"payload": {"alert": "A-1"}}, None]
        (node,) = diagnosis.nodes
        assert (node.node_id, node.executions, node.failures) == ("http", 2, 2)
        # The withheld run's failure is counted; its error text is not read.
        assert node.errors == ("HTTP 500 from https://intranet.example.com/secret",)


async def test_an_assignment_path_reads_its_completed_top_level_runs():
    async with _database() as sessions:
        seeded = await _seed(sessions)
        now = datetime.now(UTC)
        async with sessions() as db:
            for i in range(10):
                db.add(
                    AssignmentRun(
                        assignment_id=seeded["assignment"], agent_id=seeded["agent"], status="completed",
                        actions_taken=[{"tool": "list_alerts", "arguments": {"q": "secret"}}, {"tool": "close_alert"}],
                        created_at=now - timedelta(hours=i),
                    )
                )
            db.add(
                AssignmentRun(
                    assignment_id=seeded["assignment"], agent_id=seeded["agent"], status="failed",
                    actions_taken=[{"tool": "delete_everything"}], created_at=now,
                )
            )
            await db.commit()
        async with sessions() as db:
            path = await assignment_path(db, await db.get(Assignment, seeded["assignment"]))
        assert (path.runs, path.path, path.share, path.stable, path.switchable) == (
            10,
            ("list_alerts", "close_alert"),
            1.0,
            True,
            True,
        )


async def test_recurring_sequences_need_several_users():
    async with _database() as sessions:
        seeded = await _seed(sessions)
        now = datetime.now(UTC)
        async with sessions() as db:
            for i in range(6):
                user = seeded["users"]["owner"] if i % 2 else seeded["users"]["editor"]
                trace = LLMTrace(
                    id=uuid.uuid4(), source="agent-chat", name="chat", agent_id=seeded["agent"], user_id=user,
                    conversation_id=uuid.uuid4(), started_at=now - timedelta(hours=i),
                )
                db.add(trace)
                await db.flush()
                for position, name in enumerate(("search_cve", "get_asset", "create_ticket")):
                    db.add(
                        LLMSpan(
                            trace_id=trace.id, type="TOOL", name=name, position=position,
                            started_at=now - timedelta(hours=i) + timedelta(seconds=position),
                        )
                    )
            await db.commit()
        async with sessions() as db:
            candidates = await repeated_sequences(db, [seeded["agent"]], min_occurrences=5, min_users=2)
        assert [(c.sequence, c.conversations, c.users) for c in candidates] == [
            (("search_cve", "get_asset", "create_ticket"), 6, 2)
        ]
