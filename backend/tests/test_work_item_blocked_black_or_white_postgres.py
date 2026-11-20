"""Blocked is black or white (#762), against a real PostgreSQL.

A blocked work item has an owner and either a blocker the platform releases by
itself (``blocked_on``, ``blocked_on_refs``) or a person's decision
(``needs_human`` with an ask card). Through the service and the API: a blocked
status with only a free-text reason, with no owner, or ``needs_human`` without
an ask card is refused with nothing written; ``not_before`` alone is a deferral
of an open item, never a blocker. Legacy rows that break the rule are left as
they are and reported by the fleet health finding ``blocked_without_blocker``,
item ids included. Marked ``integration``: CI's integration job runs it.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import func, select

import app.models  # noqa: F401 - every table, for ALL_TABLES
from app.models.agent import Agent
from app.models.fleet import Fleet, FleetMember
from app.models.user import User
from app.models.work_item import WorkItem
from app.services import work_item as wi_service
from app.services.company_managed_writes import person_writer
from app.services.workspaces import create_workspace
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration

WRITER = person_writer(is_admin=False)
ASK = {
    "question": "Ship the migration tonight?",
    "options": [{"key": "A", "label": "tonight"}, {"key": "B", "label": "Monday"}],
    "recommendation": "A",
}
PR = [{"repo": "XTM-One-Platform/xtm-one", "kind": "pull_request", "number": 762, "release_on": "merged"}]


@asynccontextmanager
async def _database() -> AsyncIterator[Any]:
    async with scratch_database("blackwhite", tables=ALL_TABLES, extensions=("vector",)) as db:
        yield db.sessions


async def _world(sessions: Any) -> tuple[User, Agent, Fleet]:
    async with sessions() as db:
        user = User(
            id=uuid.uuid4(),
            email=f"member-{uuid.uuid4().hex[:8]}@test.local",
            full_name="Member",
            hashed_password="x",
        )
        db.add(user)
        await db.flush()
        workspace = await create_workspace(db, user_id=user.id, name="ops home")
        agent = Agent(id=uuid.uuid4(), name="ops lead", user_id=user.id)
        db.add(agent)
        await db.flush()
        fleet = Fleet(
            user_id=user.id, slug="ops", name="ops", workspace_id=workspace.id, orchestrator_agent_id=agent.id
        )
        db.add(fleet)
        await db.flush()
        db.add(FleetMember(fleet_id=fleet.id, agent_id=agent.id, role="member"))
        await db.commit()
        return user, agent, fleet


async def _create(user: User, **kwargs: Any) -> dict[str, Any]:
    return await wi_service.create_work_item(user_id=str(user.id), title="Backfill the rows", writer=WRITER, **kwargs)


async def test_a_blocked_create_needs_an_owner_and_a_real_blocker_or_a_decision() -> None:
    async with _database() as sessions:
        user, agent, _fleet = await _world(sessions)
        owner = str(agent.id)

        # Free text alone: refused, naming the three options.
        with pytest.raises(ValueError) as refused:
            await _create(user, owner_agent_id=owner, status="blocked", blocked_reason="waits for the partner retest")
        for name in ("blocked_on", "blocked_on_refs", "needs_human", "ask"):
            assert name in str(refused.value)
        assert "nothing was written" in str(refused.value)
        # A real blocker but no owner: refused.
        with pytest.raises(ValueError, match="no owner_agent_id"):
            await _create(user, status="blocked", blocked_on_refs=PR)
        # A person's decision without its card: refused, whatever the status.
        with pytest.raises(ValueError, match="needs_human needs an ask card"):
            await _create(user, owner_agent_id=owner, needs_human=True)
        with pytest.raises(ValueError, match="needs_human needs an ask card"):
            await _create(user, owner_agent_id=owner, needs_human=True, ask={"question": "Ship it?"})
        # Nothing was written by the refusals.
        async with sessions() as db:
            assert (await db.execute(select(func.count()).select_from(WorkItem))).scalar_one() == 0

        # Black or white: each goes through.
        by_ref = await _create(user, owner_agent_id=owner, status="blocked", blocked_on_refs=PR)
        assert by_ref["status"] == "blocked"
        by_item = await _create(user, owner_agent_id=owner, blocked_on=[by_ref["id"]])
        assert by_item["status"] == "blocked"
        decision = await _create(user, owner_agent_id=owner, needs_human=True, ask=ASK)
        assert decision["status"] == "blocked" and decision["ask"]["recommendation"] == "A"


async def test_not_before_alone_is_a_deferral_never_a_blocker() -> None:
    async with _database() as sessions:
        user, agent, _fleet = await _world(sessions)
        later = datetime.now(UTC) + timedelta(hours=2)
        with pytest.raises(ValueError, match="a deferral is an open item with not_before"):
            await _create(user, owner_agent_id=str(agent.id), status="blocked", not_before=later)
        deferred = await _create(user, owner_agent_id=str(agent.id), not_before=later)
        assert deferred["status"] == "open"


async def test_an_update_to_blocked_is_checked_on_the_item_as_it_would_land() -> None:
    async with _database() as sessions:
        user, agent, _fleet = await _world(sessions)
        item = await _create(user, owner_agent_id=str(agent.id))

        with pytest.raises(ValueError, match="no blocker the platform releases"):
            await wi_service.update_work_item(
                item["id"], user_id=str(user.id), status="blocked", blocked_reason="waits", writer=WRITER
            )
        with pytest.raises(ValueError, match="needs_human needs an ask card"):
            await wi_service.update_work_item(item["id"], user_id=str(user.id), needs_human=True, writer=WRITER)
        still = await wi_service.get_work_item(item["id"], user_id=str(user.id))
        assert still is not None and still["status"] == "open"

        parked = await wi_service.update_work_item(
            item["id"], user_id=str(user.id), needs_human=True, ask=ASK, writer=WRITER
        )
        assert parked is not None and parked["status"] == "blocked"
        # Clearing the owner of a blocked item is refused: nobody would resume it.
        with pytest.raises(ValueError, match="no owner_agent_id"):
            await wi_service.update_work_item(item["id"], user_id=str(user.id), owner_agent_id="", writer=WRITER)


async def test_a_legacy_item_is_left_as_it_is_and_reported_by_fleet_health() -> None:
    from app.services.fleet_health_signals import _work_item_findings

    async with _database() as sessions:
        user, agent, fleet = await _world(sessions)
        now = datetime.now(UTC)

        def _row(**kw: Any) -> WorkItem:
            base = dict(
                id=uuid.uuid4(),
                user_id=user.id,
                workspace_id=fleet.workspace_id,
                owner_agent_id=agent.id,
                title="legacy",
                status="blocked",
                priority="medium",
                source="agent",
                last_activity_at=now,
                created_at=now,
                updated_at=now,
            )
            base.update(kw)
            return WorkItem(**base)

        cancelled = _row(status="cancelled", completed_at=now)
        rows = {
            "free_text": _row(blocked_reason="waits for the partner deploy"),
            "ownerless": _row(owner_agent_id=None, blocked_on_refs=PR),
            "no_ask": _row(needs_human=True),
            "on_cancelled": _row(blocked_on=[str(cancelled.id)]),
            "compliant_ref": _row(blocked_on_refs=PR),
            "compliant_ask": _row(needs_human=True, ask=ASK),
        }
        async with sessions() as db:
            db.add(cancelled)
            db.add_all(rows.values())
            await db.commit()

        async with sessions() as db:
            findings = await _work_item_findings(db, fleet, [agent.id], user_id=str(user.id), now=now)
        (finding,) = [f for f in findings if f.kind == "blocked_without_blocker"]
        reported = {str(e) for e in finding.all_evidence}
        assert reported == {str(rows[k].id) for k in ("free_text", "ownerless", "no_ask", "on_cancelled")}

        # Left as they are: no migration changed a status.
        for row in rows.values():
            stored = await wi_service.get_work_item(str(row.id), user_id=str(user.id))
            assert stored is not None and stored["status"] == "blocked"
        # A note on a legacy item still lands: only a write to its blocking fields is checked.
        noted = await wi_service.update_work_item(
            str(rows["free_text"].id), user_id=str(user.id), note="looked at", writer=WRITER
        )
        assert noted is not None and noted["status"] == "blocked"
