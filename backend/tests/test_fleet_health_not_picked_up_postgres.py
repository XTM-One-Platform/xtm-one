"""Fleet health ``not_picked_up``: an open item no inbox will take, on real rows.

A desk files items for itself or a peer; when the owner has no work-item inbox,
or every inbox it has filters the item out by its correlation prefix, nothing
ever runs it. ``get_fleet_health`` names such items with the dispatch's own
pickup rule, and leaves out an item an inbox takes (now or from its
``not_before``), a scheduled one-off and a blocked item (reported by
``blocked_without_blocker`` when it has no real blocker). Marked
``integration``: CI's integration job runs it.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

import app.models  # noqa: F401 - every table, for ALL_TABLES
from app.models.agent import Agent
from app.models.assignment import Assignment
from app.models.fleet import Fleet, FleetMember
from app.models.user import User
from app.models.work_item import WorkItem
from app.services.workspaces import create_workspace
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration


@asynccontextmanager
async def _database() -> AsyncIterator[Any]:
    async with scratch_database("notpicked", tables=ALL_TABLES, extensions=("vector",)) as db:
        yield db.sessions


async def _world(sessions: Any) -> tuple[User, Fleet, dict[str, Agent]]:
    async with sessions() as db:
        user = User(
            id=uuid.uuid4(),
            email=f"owner-{uuid.uuid4().hex[:8]}@test.local",
            full_name="Owner",
            hashed_password="x",
        )
        db.add(user)
        await db.flush()
        workspace = await create_workspace(db, user_id=user.id, name="desk home")
        agents = {
            name: Agent(id=uuid.uuid4(), name=name, user_id=user.id) for name in ("orchestrator", "desk", "silent")
        }
        db.add_all(agents.values())
        await db.flush()
        fleet = Fleet(
            user_id=user.id,
            slug="desks",
            name="desks",
            workspace_id=workspace.id,
            orchestrator_agent_id=agents["orchestrator"].id,
        )
        db.add(fleet)
        await db.flush()
        db.add_all(FleetMember(fleet_id=fleet.id, agent_id=a.id, role="member") for a in agents.values())
        # The orchestrator drains everything; the desk only its own prefix; "silent" has no inbox.
        for agent, config in ((agents["orchestrator"], {}), (agents["desk"], {"correlation_prefix": "desk:"})):
            db.add(
                Assignment(
                    id=uuid.uuid4(),
                    agent_id=agent.id,
                    user_id=user.id,
                    name=f"Inbox of {agent.name}",
                    trigger_type="work_item",
                    trigger_config=config,
                    prompt="Work the item you were given.",
                    enabled=True,
                )
            )
        await db.commit()
        return user, fleet, agents


async def test_an_open_item_no_inbox_takes_is_reported_and_a_taken_one_is_not() -> None:
    from app.services.fleet_health_signals import _work_item_findings

    async with _database() as sessions:
        user, fleet, agents = await _world(sessions)
        now = datetime.now(UTC)

        def _row(owner: Agent | None, key: str | None, **kw: Any) -> WorkItem:
            base = dict(
                id=uuid.uuid4(),
                user_id=user.id,
                workspace_id=fleet.workspace_id,
                owner_agent_id=owner.id if owner else None,
                correlation_key=key,
                title="desk hand-off",
                status="open",
                priority="medium",
                source="agent",
                last_activity_at=now,
                created_at=now,
                updated_at=now,
            )
            base.update(kw)
            return WorkItem(**base)

        rows = {
            # Stranded: no owner, an owner with no inbox, an inbox whose prefix leaves it out.
            "unassigned": _row(None, "desk:note:1"),
            "no_inbox": _row(agents["silent"], "silent:retest:1"),
            "filtered_out": _row(agents["desk"], "openaev:detection-desk:note:1"),
            # Taken: by the orchestrator, by the desk's own prefix, deferred to an inbox that takes it.
            "orchestrator": _row(agents["orchestrator"], "anything:1"),
            "desk_prefix": _row(agents["desk"], "desk:note:2"),
            "deferred": _row(agents["desk"], "desk:note:3", not_before=now + timedelta(hours=2)),
            # Not open: a blocked item is judged by blocked_without_blocker, never here.
            "blocked": _row(
                agents["silent"],
                "silent:retest:2",
                status="blocked",
                blocked_on_refs=[
                    {"repo": "XTM-One-Platform/xtm-one", "kind": "pull_request", "number": 1, "release_on": "merged"}
                ],
            ),
        }
        async with sessions() as db:
            db.add_all(rows.values())
            await db.commit()

        all_ids = [a.id for a in agents.values()]
        async with sessions() as db:
            findings = await _work_item_findings(db, fleet, all_ids, user_id=str(user.id), now=now)
        (finding,) = [f for f in findings if f.kind == "not_picked_up"]
        reported = {str(e) for e in finding.all_evidence}
        assert reported == {str(rows[k].id) for k in ("unassigned", "no_inbox", "filtered_out")}
        assert finding.fix == "owner" and finding.advice


async def test_a_fleet_whose_items_all_reach_an_inbox_has_no_such_finding() -> None:
    from app.services.fleet_health_signals import _work_item_findings

    async with _database() as sessions:
        user, fleet, agents = await _world(sessions)
        now = datetime.now(UTC)
        async with sessions() as db:
            db.add(
                WorkItem(
                    id=uuid.uuid4(),
                    user_id=user.id,
                    workspace_id=fleet.workspace_id,
                    owner_agent_id=agents["desk"].id,
                    correlation_key="desk:note:9",
                    title="desk hand-off",
                    status="open",
                    priority="medium",
                    source="agent",
                    last_activity_at=now,
                    created_at=now,
                    updated_at=now,
                )
            )
            await db.commit()
        async with sessions() as db:
            findings = await _work_item_findings(
                db, fleet, [a.id for a in agents.values()], user_id=str(user.id), now=now
            )
        assert not [f for f in findings if f.kind == "not_picked_up"]
