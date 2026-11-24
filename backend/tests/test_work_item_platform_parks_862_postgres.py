"""Platform parks and references met at the write, against a real PostgreSQL (#862).

* The dispatch cap (``release_work_item(failed=True)`` at
  ``work_item_max_dispatch_attempts``) leaves the item waiting for a person
  on the card the platform writes; fleet health reports nothing, and the
  card's answer puts it back in the queue.
* A pull request merged before the write that blocks on it releases the
  item at the write, not at the next polling pass.

Marked ``integration``: CI's integration job runs it.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

import pytest

import app.models  # noqa: F401 - every table, for ALL_TABLES
from app.integrations import reference_state
from app.integrations.pull_request_events import PullRequestEvent
from app.models.agent import Agent
from app.models.fleet import Fleet, FleetMember
from app.models.user import User
from app.services import work_item as wi_service
from app.services import work_item_blocked_refs as refs_mod
from app.services.company_managed_writes import person_writer
from app.services.workspaces import create_workspace
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration

WRITER = person_writer(is_admin=False)
REPO = "xtm-one-platform/xtm-one"


@asynccontextmanager
async def _database() -> AsyncIterator[Any]:
    async with scratch_database("parks862", tables=ALL_TABLES, extensions=("vector",)) as db:
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


async def test_the_dispatch_cap_parks_for_a_person_and_the_answer_puts_it_back(monkeypatch) -> None:
    from app.services.fleet_health_signals import _work_item_findings

    monkeypatch.setattr(wi_service.settings, "work_item_max_dispatch_attempts", 1)
    async with _database() as sessions:
        user, agent, fleet = await _world(sessions)
        item = await wi_service.create_work_item(
            user_id=str(user.id), title="Rebuild the index", owner_agent_id=str(agent.id), writer=WRITER
        )
        await wi_service.release_work_item_claim(
            work_item_id=item["id"], user_id=str(user.id), failed=True, error="boom", writer=WRITER
        )
        parked = await wi_service.get_work_item(item["id"], user_id=str(user.id))
        assert parked is not None and parked["status"] == "blocked"
        assert parked["needs_human"] and parked["ask"]["recommendation"] == "requeue"
        assert "boom" in parked["blocked_reason"]
        async with sessions() as db:
            findings = await _work_item_findings(db, fleet, [agent.id], user_id=str(user.id), now=datetime.now(UTC))
        assert not [f for f in findings if f.kind in ("blocked_without_blocker", "decision_card_not_readable")]

        answered = await wi_service.answer_work_item(item["id"], user_id=str(user.id), option="requeue", writer=WRITER)
        assert answered is not None and answered["status"] == "open" and not answered.get("needs_human")


async def test_a_pull_request_merged_before_the_write_releases_the_item_at_the_write(monkeypatch) -> None:
    reads: list = []
    # The read waits for the write's read-back: the spawned check may otherwise
    # release the item before create_work_item returns (merge group of #1586).
    gate = asyncio.Event()

    async def reader(repo, kind, number, token):
        reads.append((repo, kind, number))
        await gate.wait()
        return PullRequestEvent(
            kind="closed", repo=repo, pr_numbers=(number,), merged=True, closed_at="2026-11-21T07:18:48Z"
        )

    async def token(db, provider, user_id):
        return "tok"

    monkeypatch.setattr(reference_state, "_READERS", {"github": reader})
    monkeypatch.setattr(refs_mod, "reference_token", token)
    async with _database() as sessions:
        user, agent, _fleet = await _world(sessions)
        item = await wi_service.create_work_item(
            user_id=str(user.id),
            title="Retest after the merge",
            owner_agent_id=str(agent.id),
            blocked_on_refs=[{"repo": REPO, "kind": "pull_request", "number": 767, "release_on": "merged"}],
            writer=WRITER,
        )
        assert item["status"] == "blocked"
        gate.set()
        # The write spawned its one read: wait for it, no polling pass involved.
        await asyncio.gather(*list(refs_mod._checks))
        assert reads == [(REPO, "pull_request", 767)]
        now = await wi_service.get_work_item(item["id"], user_id=str(user.id))
        assert now is not None and now["status"] == "open"
        assert now["description"].startswith(f"released: {REPO}#767 merged at 2026-11-21T07:18:48Z")
