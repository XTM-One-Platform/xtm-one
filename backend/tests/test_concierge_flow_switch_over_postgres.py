"""Switch-over, live watch and rollback (#8260, B12) against a real PostgreSQL.

On the shadow suite's fixtures (``test_concierge_flow_shadow_postgres``):

* a held switch-over shadow creates the production flow from the sandbox
  copy, activates it and pauses the assignment, both audited;
* a switch-over whose live runs regress is rolled back: assignment resumed,
  flow deactivated, two audit entries as ``system:concierge``;
* a person's change during the watch is kept, nothing rolled back;
* a healthy watch rolls nothing back.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.models.assignment import Assignment
from app.models.flow import FLOW_STATUS_ACTIVE, FLOW_STATUS_DRAFT, Flow
from app.models.flow_run import FlowRun
from app.models.flow_shadow import (
    SHADOW_PURPOSE_SWITCH_OVER,
    SHADOW_STATUS_REGRESSION_KEPT,
    SHADOW_STATUS_ROLLED_BACK,
    SHADOW_STATUS_WATCHING,
    FlowShadow,
)
from app.services.assignment_pause import pause_assignment
from tests.test_concierge_flow_shadow_postgres import (
    CRON,
    FakeSandbox,
    _advance,
    _assignment_run,
    _audit_actions,
    _database,
    _graph,
    _seed,
    _shadow,
    install_fake_sandbox,
)

pytestmark = pytest.mark.integration


@pytest.fixture
def sandbox(monkeypatch) -> FakeSandbox:
    return install_fake_sandbox(monkeypatch)


async def test_a_held_switch_over_creates_the_flow_and_pauses_the_assignment(sandbox):
    async with _database() as sessions:
        seeded = await _seed(sessions, sandbox)
        now = datetime.now(UTC)
        shadow_id = await _shadow(sessions, seeded, ends_at=now - timedelta(minutes=1), cursor_at=now - timedelta(days=3))
        async with sessions() as db:
            db.add_all([_assignment_run(seeded, created_at=now - timedelta(days=2, hours=-i)) for i in range(5)])
            await db.commit()

        assert await _advance(sessions, shadow_id) == SHADOW_STATUS_WATCHING

        async with sessions() as db:
            shadow = await db.get(FlowShadow, shadow_id)
            flow = await db.get(Flow, shadow.production_flow_id)
            assignment = await db.get(Assignment, seeded["assignment_id"])
            assert flow.status == FLOW_STATUS_ACTIVE
            assert flow.graph["nodes"][0]["config"] == {"cron": CRON}
            assert flow.user_id == seeded["owner_id"]
            assert flow.id != seeded["copy_id"]
            assert assignment.enabled is False
            assert assignment.paused_reason == "concierge_switch_over"
            assert assignment.paused_by_user_id is None
            assert shadow.verdict["status"] == "held"
            assert shadow.verdict["compared"] == 5
            assert shadow.watch_until == shadow.switched_at + timedelta(days=7)
        assert await _audit_actions(sessions) == [
            ("pause", "assignment", "system:concierge"),
            ("flow.activate", "flow", "system:concierge"),
        ]


async def _watching(sessions, seeded: dict, *, failed: int, completed: int, resumed_by_person: bool = False) -> uuid.UUID:
    now = datetime.now(UTC)
    async with sessions() as db:
        flow = Flow(
            id=uuid.uuid4(), user_id=seeded["owner_id"], name="Daily report (flow)", graph=_graph(),
            version_number=1, status=FLOW_STATUS_ACTIVE, last_triggered_at=now - timedelta(hours=1),
        )
        db.add(flow)
        assignment = await db.get(Assignment, seeded["assignment_id"])
        pause_assignment(assignment, reason="concierge_switch_over", details="Replaced.", actor_label="Concierge switch-over")
        if resumed_by_person:
            assignment.enabled = True
        await db.flush()
        for i in range(failed + completed):
            db.add(
                FlowRun(
                    flow_id=flow.id, graph={}, trigger_type="schedule", status="failed" if i < failed else "completed",
                    created_at=now - timedelta(hours=i + 1),
                )
            )
        shadow = FlowShadow(
            assignment_id=seeded["assignment_id"],
            flow_id=seeded["copy_id"],
            production_flow_id=flow.id,
            purpose=SHADOW_PURPOSE_SWITCH_OVER,
            status=SHADOW_STATUS_WATCHING,
            started_at=now - timedelta(days=5),
            ends_at=now - timedelta(days=2),
            cursor_at=now - timedelta(days=2),
            switched_at=now - timedelta(days=2),
            watch_until=now + timedelta(days=5),
            baseline={"runs": 20, "failure_rate": 0.0, "cost_per_run": None},
            verdict={"status": "held", "applied_version": 1},
            started_by_user_id=seeded["owner_id"],
        )
        db.add(shadow)
        await db.commit()
        return shadow.id


async def test_a_regressing_switch_over_is_rolled_back_and_audited(sandbox):
    async with _database() as sessions:
        seeded = await _seed(sessions, sandbox)
        shadow_id = await _watching(sessions, seeded, failed=3, completed=7)

        assert await _advance(sessions, shadow_id) == SHADOW_STATUS_ROLLED_BACK

        async with sessions() as db:
            shadow = await db.get(FlowShadow, shadow_id)
            flow = await db.get(Flow, shadow.production_flow_id)
            assignment = await db.get(Assignment, seeded["assignment_id"])
            assert assignment.enabled is True
            assert assignment.paused_reason is None
            # It carries on from the flow's last firing, not at once.
            assert assignment.last_triggered_at == flow.last_triggered_at
            assert flow.status == FLOW_STATUS_DRAFT
            assert shadow.verdict["watch"]["failure_rate"] == 0.3
            assert shadow.note.startswith("The flow failed 30 % of its last 10 runs")
        assert await _audit_actions(sessions) == [
            ("flow.deactivate", "flow", "system:concierge"),
            ("resume", "assignment", "system:concierge"),
        ]


async def test_a_regression_after_a_person_resumed_the_assignment_is_kept(sandbox):
    async with _database() as sessions:
        seeded = await _seed(sessions, sandbox)
        shadow_id = await _watching(sessions, seeded, failed=3, completed=7, resumed_by_person=True)

        assert await _advance(sessions, shadow_id) == SHADOW_STATUS_REGRESSION_KEPT

        async with sessions() as db:
            shadow = await db.get(FlowShadow, shadow_id)
            flow = await db.get(Flow, shadow.production_flow_id)
            assert flow.status == FLOW_STATUS_ACTIVE
            assert "the assignment was resumed meanwhile" in shadow.note
        assert await _audit_actions(sessions) == []


async def test_a_healthy_watch_rolls_nothing_back(sandbox):
    async with _database() as sessions:
        seeded = await _seed(sessions, sandbox)
        shadow_id = await _watching(sessions, seeded, failed=0, completed=10)

        assert await _advance(sessions, shadow_id) == SHADOW_STATUS_WATCHING

        async with sessions() as db:
            assignment = await db.get(Assignment, seeded["assignment_id"])
            assert assignment.enabled is False
            shadow = await db.get(FlowShadow, shadow_id)
            assert shadow.verdict["watch"] == {
                "runs": 10, "failure_rate": 0.0, "cost_per_run": None,
                "note": "The flow failed 0 % of its last 10 runs (ceiling 5 %); no regression.", "measured": True,
            }
        assert await _audit_actions(sessions) == []


async def test_a_deleted_assignment_closes_its_watch_and_leaves_the_flow(sandbox):
    async with _database() as sessions:
        seeded = await _seed(sessions, sandbox)
        shadow_id = await _watching(sessions, seeded, failed=3, completed=7)
        async with sessions() as db:
            await db.delete(await db.get(Assignment, seeded["assignment_id"]))
            await db.commit()

        # The switch-over went through: the flow doing the work is a success, not a failure.
        assert await _advance(sessions, shadow_id) == "kept"

        async with sessions() as db:
            shadow = await db.get(FlowShadow, shadow_id)
            assert shadow.assignment_id is None
            assert shadow.note == "The assignment was deleted after the switch-over; the flow stays."
            assert (await db.get(Flow, shadow.production_flow_id)).status == FLOW_STATUS_ACTIVE
