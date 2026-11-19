"""Flow shadows, switch-over, watch and rollback (#8260, B12) against a real PostgreSQL.

Level 2's sandbox (copies, intercepted runs, their write reports) is stood in
for by a recording fake at its published names, so what is pinned here is
level 7's own contract:

* a shadow pairs the assignment's live runs, and only through level 2's
  intercepted path (``start_sandbox_flow_run(purpose="shadow")``), never
  ``create_flow_run``;
* a shadow that did not hold creates no flow;
* starting a shadow keeps its sandbox copy past the shadow and its watch.

The switch-over, its watch and rollback: ``test_health_flow_switch_over_postgres.py``.
"""

from __future__ import annotations

import sys
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import ModuleType, SimpleNamespace

import pytest
from sqlalchemy import func, select

import app.models  # noqa: F401 - registers every mapper on Base.metadata
from app.models.agent import Agent
from app.models.assignment import Assignment, AssignmentRun
from app.models.audit_log import AuditLog
from app.models.flow import Flow
from app.models.flow_run import FlowRun
from app.models.flow_shadow import (
    PAIR_STATUS_COMPARED,
    SHADOW_PURPOSE_SWITCH_OVER,
    SHADOW_STATUS_NOT_HELD,
    SHADOW_STATUS_RUNNING,
    FlowShadow,
    FlowShadowPair,
)
from app.models.user import User
from app.services.health import flow_shadow
from tests.agentic_gate_stub import stub_agentic_gate
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration

CRON = "0 8 * * *"


def _graph(cron: str = CRON) -> dict:
    return {
        "nodes": [
            {"id": "t1", "kind": "trigger.schedule", "position": {"x": 0, "y": 0}, "config": {"cron": cron}},
            {"id": "c1", "kind": "condition.if", "position": {"x": 0, "y": 100}, "config": {"left": "x", "operator": "is_not_empty"}},
        ],
        "edges": [
            {"id": "e1", "source": "t1", "source_handle": "out", "target": "c1", "target_handle": "in", "kind": "data"}
        ],
        "viewport": {"x": 0, "y": 0, "zoom": 1},
    }


class FakeSandbox:
    """Level 2 at its published names: which flows are copies, and the runs started on them."""

    def __init__(self) -> None:
        self.copy_ids: set[uuid.UUID] = set()
        self.started: list[dict] = []
        self.retained: list[datetime] = []
        self.run_status = "completed"

    def install(self, monkeypatch) -> None:
        copies = ModuleType("app.services.health.sandbox.copies")
        copies.is_sandbox_copy = lambda row: row.id in self.copy_ids

        async def sandbox_copy_of(_db, *, flow_id=None, agent_id=None):
            return SimpleNamespace(id=flow_id, copy_flow_id=flow_id) if flow_id in self.copy_ids else None

        copies.sandbox_copy_of = sandbox_copy_of

        async def retain_sandbox_copy(_db, copy_id, *, until):
            self.retained.append(until)

        copies.retain_sandbox_copy = retain_sandbox_copy
        runs = ModuleType("app.services.health.sandbox.runs")

        async def start_sandbox_flow_run(db, copy, *, trigger_type, trigger_payload, case_ref, purpose, run_as_user_id):
            self.started.append(
                {"trigger_type": trigger_type, "payload": trigger_payload, "purpose": purpose, "run_as": run_as_user_id}
            )
            run = FlowRun(
                flow_id=copy.copy_flow_id,
                graph={},
                status=self.run_status,
                trigger_type=trigger_type,
                trigger_payload=trigger_payload,
                result_summary="Report sent.",
                started_at=datetime.now(UTC),
                completed_at=datetime.now(UTC) if self.run_status == "completed" else None,
            )
            db.add(run)
            await db.flush()
            return run

        async def sandbox_run_report(_db, *, flow_run_id=None, sandbox_run_id=None):
            return SimpleNamespace(intercepted_writes=1, simulated_writes=1)

        runs.start_sandbox_flow_run = start_sandbox_flow_run
        runs.sandbox_run_report = sandbox_run_report
        import app.services.health.sandbox as real

        package = ModuleType("app.services.health.sandbox")
        # Still a package: the submodules not faked here import as they are.
        package.__path__ = real.__path__
        package.copies, package.runs = copies, runs
        monkeypatch.setitem(sys.modules, "app.services.health.sandbox", package)
        monkeypatch.setitem(sys.modules, "app.services.health.sandbox.copies", copies)
        monkeypatch.setitem(sys.modules, "app.services.health.sandbox.runs", runs)


@pytest.fixture
def sandbox(monkeypatch) -> FakeSandbox:
    return install_fake_sandbox(monkeypatch)


def install_fake_sandbox(monkeypatch) -> FakeSandbox:
    """Level 2 faked, Platform Health's gates open, and any production flow run refused."""
    from app.services import flow_runner
    from app.services.health import availability, budget, consent

    stub_agentic_gate(monkeypatch)
    monkeypatch.setattr(availability, "health_enabled", lambda: True)

    async def no_refusal(_db, cycle_id=None):
        return None

    async def active(_db):
        return True

    async def readable(_db, **_kwargs):
        return True

    monkeypatch.setattr(budget, "budget_refusal", no_refusal)
    monkeypatch.setattr(availability, "health_active", active)
    monkeypatch.setattr(budget, "health_active", active)
    monkeypatch.setattr(consent, "content_readable", readable)

    async def refuse(*_args, **_kwargs):
        raise AssertionError("a shadow must never start a production flow run")

    monkeypatch.setattr(flow_runner, "create_flow_run", refuse)
    fake = FakeSandbox()
    fake.install(monkeypatch)
    return fake


@asynccontextmanager
async def _database():
    async with scratch_database("flowshadow", tables=ALL_TABLES, extensions=("vector",)) as db:
        yield db.sessions


async def _seed(sessions, sandbox: FakeSandbox) -> dict:
    async with sessions() as db:
        owner = User(
            id=uuid.uuid4(), email=f"owner-{uuid.uuid4().hex[:8]}@example.com", full_name="Owner", hashed_password="x", role="user"
        )
        db.add(owner)
        await db.flush()
        agent = Agent(id=uuid.uuid4(), name="Daily report agent", user_id=owner.id)
        db.add(agent)
        await db.flush()
        assignment = Assignment(
            id=uuid.uuid4(),
            agent_id=agent.id,
            user_id=owner.id,
            name="Daily report",
            trigger_type="schedule",
            trigger_config={"cron": CRON},
            prompt="Send the daily report.",
            enabled=True,
        )
        copy = Flow(id=uuid.uuid4(), user_id=None, name="Sandbox: daily report", graph=_graph(), version_number=1)
        db.add_all([assignment, copy])
        await db.commit()
        sandbox.copy_ids.add(copy.id)
        return {"owner_id": owner.id, "agent_id": agent.id, "assignment_id": assignment.id, "copy_id": copy.id}


def _assignment_run(seeded: dict, *, created_at: datetime, context_type: str = "schedule", status: str = "completed") -> AssignmentRun:
    return AssignmentRun(
        assignment_id=seeded["assignment_id"],
        agent_id=seeded["agent_id"],
        user_id=seeded["owner_id"],
        trigger_payload={"trigger_type": context_type, "scheduled_at": created_at.isoformat()},
        trigger_context={"type": context_type},
        status=status,
        result_summary="Report sent.",
        created_at=created_at,
        started_at=created_at,
        completed_at=created_at + timedelta(seconds=5),
    )


async def _shadow(sessions, seeded: dict, **fields) -> uuid.UUID:
    now = datetime.now(UTC)
    async with sessions() as db:
        shadow = FlowShadow(
            assignment_id=seeded["assignment_id"],
            flow_id=seeded["copy_id"],
            purpose=SHADOW_PURPOSE_SWITCH_OVER,
            status=fields.pop("status", SHADOW_STATUS_RUNNING),
            started_at=fields.pop("started_at", now - timedelta(days=3)),
            ends_at=fields.pop("ends_at", now + timedelta(days=1)),
            cursor_at=fields.pop("cursor_at", now - timedelta(days=3)),
            started_by_user_id=seeded["owner_id"],
            **fields,
        )
        db.add(shadow)
        await db.commit()
        return shadow.id


async def _advance(sessions, shadow_id: uuid.UUID) -> str:
    async with sessions() as db:
        shadow = await db.get(FlowShadow, shadow_id)
        status = await flow_shadow.advance_shadow(db, shadow)
        await db.commit()
        return status


async def _audit_actions(sessions) -> list[tuple[str, str, str]]:
    async with sessions() as db:
        rows = (await db.execute(select(AuditLog).order_by(AuditLog.created_at))).scalars()
        return [(row.action, row.entity_type, row.user_email) for row in rows]


async def test_a_shadow_pairs_live_runs_through_the_intercepted_path_only(sandbox):
    async with _database() as sessions:
        seeded = await _seed(sessions, sandbox)
        shadow_id = await _shadow(sessions, seeded)
        start = datetime.now(UTC) - timedelta(days=2)
        async with sessions() as db:
            db.add_all(
                [
                    _assignment_run(seeded, created_at=start),
                    _assignment_run(seeded, created_at=start + timedelta(hours=1), context_type="manual"),
                    _assignment_run(seeded, created_at=start + timedelta(hours=2)),
                ]
            )
            await db.commit()

        assert await _advance(sessions, shadow_id) == SHADOW_STATUS_RUNNING

        assert [s["purpose"] for s in sandbox.started] == ["shadow", "shadow"]
        assert [s["trigger_type"] for s in sandbox.started] == ["schedule", "schedule"]
        assert sandbox.started[0]["payload"] == {"fired_at": start.isoformat()}
        assert {s["run_as"] for s in sandbox.started} == {seeded["owner_id"]}
        async with sessions() as db:
            pairs = list((await db.execute(select(FlowShadowPair))).scalars())
            # The manual run is recorded as passed over, never paired.
            assert sorted(p.status for p in pairs) == ["compared", "compared", "skipped"]
            pairs = [p for p in pairs if p.status == PAIR_STATUS_COMPARED]
            assert {p.comparison["verdict"] for p in pairs} == {"agree"}
            assert {p.simulated_writes for p in pairs} == {1}
            # The manual run was passed over, and the cursor moved past all three.
            shadow = await db.get(FlowShadow, shadow_id)
            assert shadow.cursor_at == start + timedelta(hours=2)


async def test_starting_a_shadow_keeps_its_copy_past_the_watch(sandbox):
    async with _database() as sessions:
        seeded = await _seed(sessions, sandbox)
        async with sessions() as db:
            shadow = await flow_shadow.start_shadow(
                db,
                assignment=await db.get(Assignment, seeded["assignment_id"]),
                flow=await db.get(Flow, seeded["copy_id"]),
                purpose=SHADOW_PURPOSE_SWITCH_OVER,
            )
            started_at, ends_at = shadow.started_at, shadow.ends_at
            await db.commit()
        assert ends_at - started_at == timedelta(days=3)
        # Until the latest it may run (30 days to gather its pairs), its watch and a day.
        assert sandbox.retained == [started_at + timedelta(days=30 + 7 + 1)]


async def test_a_shadow_that_did_not_hold_creates_no_flow(sandbox):
    async with _database() as sessions:
        seeded = await _seed(sessions, sandbox)
        now = datetime.now(UTC)
        shadow_id = await _shadow(sessions, seeded, ends_at=now - timedelta(minutes=1))
        async with sessions() as db:
            db.add_all(
                [
                    _assignment_run(seeded, created_at=now - timedelta(days=2, hours=-i))
                    for i in range(5)
                ]
            )
            await db.commit()
        # The assignment's answers differ from the flow's every time.
        async with sessions() as db:
            for run in (await db.execute(select(AssignmentRun))).scalars():
                run.result_summary = f"Escalated incident {run.id}"
            await db.commit()

        assert await _advance(sessions, shadow_id) == SHADOW_STATUS_NOT_HELD

        async with sessions() as db:
            assert (await db.scalar(select(func.count()).select_from(Flow))) == 1
            assignment = await db.get(Assignment, seeded["assignment_id"])
            assert assignment.enabled is True
            shadow = await db.get(FlowShadow, shadow_id)
            assert shadow.production_flow_id is None
            assert shadow.verdict["agreement_share"] == 0.0
        assert await _audit_actions(sessions) == []


async def test_a_shadow_runs_content_follows_the_assignment_run_it_copied(sandbox, monkeypatch):
    from app.services.health import consent, flow_lab_access, identity
    from app.services.health.identity import HealthPrincipal, acting_as_health_principal

    calls: list[dict] = []
    withdrawn = False

    async def content_readable(_db, *, agent_id=None, flow_id=None, user_id=None, user_ids=()):
        calls.append({"agent_id": agent_id, "flow_id": flow_id, "user_id": user_id})
        return not withdrawn

    monkeypatch.setattr(identity, "health_enabled", lambda: True)
    monkeypatch.setattr(consent, "content_readable", content_readable)
    async with _database() as sessions:
        seeded = await _seed(sessions, sandbox)
        shadow_id = await _shadow(sessions, seeded)
        async with sessions() as db:
            db.add(_assignment_run(seeded, created_at=datetime.now(UTC) - timedelta(hours=1)))
            await db.commit()
        await _advance(sessions, shadow_id)
        withdrawn = True
        calls.clear()
        principal = HealthPrincipal(agent_id=uuid.uuid4(), role="flow_lab", on_behalf_of_admin_id=None)
        with acting_as_health_principal(principal):
            async with sessions() as db:
                caller = await flow_lab_access.resolve_caller(db, None, agent_id=str(principal.agent_id))
                pair = await db.scalar(select(FlowShadowPair))
                run = await db.get(FlowRun, pair.flow_run_id)
                copy = await db.get(Flow, seeded["copy_id"])
                allowed = await flow_lab_access.flow_run_content_allowed(db, caller, copy, run)
        assert allowed is False
        assert calls == [{"agent_id": seeded["agent_id"], "flow_id": None, "user_id": seeded["owner_id"]}]
