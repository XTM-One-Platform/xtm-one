"""The Concierge companion (#8261) against PostgreSQL: the preference and the activity.

* The preference: the signed-in user's own, off by default; an unknown pet
  or a malformed body is a 422; a removed pet falls back to the default.
* The activity: a non-admin's counts come only from the cycles they asked
  for, their own runs and the proposals they may decide (owner, ``config``
  share; not ``read_only``, not a stranger, not an administrator on a private
  agent); an administrator's cycles are every cycle.

``integration``-marked; skips when PostgreSQL is unreachable.
"""

from __future__ import annotations

import io
import json
import uuid
import zipfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi import FastAPI
from PIL import Image

import app.models  # noqa: F401 - registers every table on Base.metadata
from app.api import deps
from app.api.v1 import concierge_companion as routes
from app.core.config import settings
from app.core.database import get_db
from app.models.assignment import Assignment, AssignmentRun
from app.models.concierge import ConciergeCycle
from app.models.concierge_cycle import ConciergeCycleRun
from app.models.improvement_proposal import ImprovementProposal
from app.models.user import User
from tests.concierge_support import World, concierge_database

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _flag_on(monkeypatch):
    monkeypatch.setattr(settings, "enabled_features", "CONCIERGE")


@asynccontextmanager
async def _client(sessions, user: User) -> AsyncIterator[httpx.AsyncClient]:
    app = FastAPI()
    app.include_router(routes.router, prefix="/api/v1/concierge")

    async def _get_db():
        async with sessions() as db:
            yield db
            await db.commit()

    async def _current():
        async with sessions() as db:
            return await db.get(User, user.id)

    app.dependency_overrides[deps.get_current_user] = _current
    app.dependency_overrides[get_db] = _get_db
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        yield client


def _package(slug: str = "owl", **manifest) -> bytes:
    sheet = io.BytesIO()
    Image.new("RGBA", (1536, 1872), (200, 100, 50, 255)).save(sheet, format="PNG")
    data = {"id": slug, "displayName": "Owl", "spritesheetPath": "spritesheet.png", **manifest}
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        archive.writestr("pet.json", json.dumps(data))
        archive.writestr("spritesheet.png", sheet.getvalue())
    return out.getvalue()


async def _upload(client: httpx.AsyncClient, package: bytes) -> httpx.Response:
    return await client.post("/api/v1/concierge/pets", files=[("files", ("owl.zip", package, "application/zip"))])


# ── The preference ─────────────────────────────────────────────────────────


async def test_a_removed_pet_falls_back_to_alfred_for_who_picked_it() -> None:
    async with concierge_database() as sessions:
        world = await World().build(sessions)
        async with _client(sessions, world.admin) as client:
            await _upload(client, _package())
        async with _client(sessions, world.reader) as client:
            chosen = await client.put("/api/v1/concierge/me/companion", json={"enabled": True, "pet": "owl"})
        async with _client(sessions, world.admin) as client:
            removed = await client.delete("/api/v1/concierge/pets/owl")
        async with _client(sessions, world.reader) as client:
            after = await client.get("/api/v1/concierge/me/companion")
    assert chosen.json() == {"enabled": True, "pet": "owl", "default_pet": "alfred"}
    assert removed.status_code == 204
    assert after.json() == {"enabled": True, "pet": "alfred", "default_pet": "alfred"}


async def test_the_companion_is_off_by_default_and_each_users_own() -> None:
    async with concierge_database() as sessions:
        world = await World().build(sessions)
        async with _client(sessions, world.member) as client:
            default = await client.get("/api/v1/concierge/me/companion")
            saved = await client.put("/api/v1/concierge/me/companion", json={"enabled": True, "pet": "alfred"})
            unknown = await client.put("/api/v1/concierge/me/companion", json={"enabled": True, "pet": "owl"})
            malformed = await client.put("/api/v1/concierge/me/companion", json={"enabled": True, "pet": "../x"})
            extra = await client.put(
                "/api/v1/concierge/me/companion", json={"enabled": True, "user_id": str(world.owner.id)}
            )
            not_bool = await client.put("/api/v1/concierge/me/companion", json={"enabled": "maybe"})
        async with _client(sessions, world.owner) as client:
            others = await client.get("/api/v1/concierge/me/companion")
        async with sessions() as db:
            stored = (await db.get(User, world.member.id)).concierge_companion
    assert default.json() == {"enabled": False, "pet": "alfred", "default_pet": "alfred"}
    assert saved.json() == {"enabled": True, "pet": "alfred", "default_pet": "alfred"}
    assert (unknown.status_code, malformed.status_code, extra.status_code, not_bool.status_code) == (422, 422, 422, 422)
    assert stored == {"enabled": True, "pet": "alfred"}
    assert others.json()["enabled"] is False


async def test_every_companion_route_is_dark_while_the_flag_is_off(monkeypatch) -> None:
    async with concierge_database() as sessions:
        world = await World().build(sessions)
        monkeypatch.setattr(settings, "enabled_features", "")
        async with _client(sessions, world.admin) as client:
            for method, url in [
                ("GET", "/api/v1/concierge/me/companion"),
                ("PUT", "/api/v1/concierge/me/companion"),
                ("GET", "/api/v1/concierge/me/activity"),
            ]:
                assert (await client.request(method, url, json={"enabled": True})).status_code == 403, url


# ── The activity ───────────────────────────────────────────────────────────


async def _cycle(db, *, status: str, requested_by: uuid.UUID | None, closed_ago: timedelta | None = None) -> None:
    cycle = ConciergeCycle(
        id=uuid.uuid4(),
        specialist="orchestrator",
        trigger="on_demand",
        month=datetime.now(UTC).strftime("%Y-%m"),
        status=status,
        closed_at=datetime.now(UTC) - closed_ago if closed_ago is not None else None,
    )
    db.add(cycle)
    await db.flush()
    db.add(ConciergeCycleRun(cycle_id=cycle.id, requested_by_user_id=requested_by))


async def _run(db, world: World, user: User, status: str) -> None:
    assignment = Assignment(
        id=uuid.uuid4(),
        agent_id=world.private.id,
        user_id=user.id,
        name=f"a-{uuid.uuid4().hex[:6]}",
        prompt="p",
        trigger_type="manual",
    )
    db.add(assignment)
    await db.flush()
    db.add(AssignmentRun(assignment_id=assignment.id, agent_id=world.private.id, user_id=user.id, status=status))


async def _activity(sessions, user: User) -> dict:
    async with _client(sessions, user) as client:
        response = await client.get("/api/v1/concierge/me/activity")
    assert response.status_code == 200
    body = response.json()
    return {key: body[key] for key in ("state", "running", "waiting", "review")}


async def test_the_activity_reads_only_what_the_user_may_see() -> None:
    async with concierge_database() as sessions:
        world = await World().build(sessions)
        idle = await _activity(sessions, world.stranger)
        async with sessions() as db:
            await _cycle(db, status="open", requested_by=world.owner.id)
            await _cycle(db, status="open", requested_by=world.reader.id)
            await db.commit()
        owner_running = await _activity(sessions, world.owner)
        stranger_running = await _activity(sessions, world.stranger)
        admin_running = await _activity(sessions, world.admin)
        async with sessions() as db:
            await _run(db, world, world.owner, "awaiting_human")
            await _run(db, world, world.owner, "running")
            await db.commit()
        owner_waiting = await _activity(sessions, world.owner)
        reader_after_runs = await _activity(sessions, world.reader)
    assert idle == {"state": "idle", "running": 0, "waiting": 0, "review": 0}
    assert owner_running == {"state": "running", "running": 1, "waiting": 0, "review": 0}
    assert stranger_running == {"state": "idle", "running": 0, "waiting": 0, "review": 0}
    assert admin_running == {"state": "running", "running": 2, "waiting": 0, "review": 0}
    assert owner_waiting == {"state": "waiting", "running": 2, "waiting": 1, "review": 0}
    assert reader_after_runs == {"state": "running", "running": 1, "waiting": 0, "review": 0}


async def test_a_proposal_is_review_for_who_may_decide_it_only() -> None:
    async with concierge_database() as sessions:
        world = await World().build(sessions)
        async with sessions() as db:
            db.add(
                ImprovementProposal(
                    id=uuid.uuid4(),
                    proposal_type="persona",
                    target_type="agent",
                    target_id=world.private.id,
                    agent_id=world.private.id,
                    status="pending_approval",
                    dedup_key=uuid.uuid4().hex,
                    base_content="p",
                    proposed_content="p, better",
                    summary="Better persona",
                    author_agent_id=world.fleet.id,
                )
            )
            await db.commit()
        reviews = {
            name: (await _activity(sessions, getattr(world, name)))["review"]
            for name in ("owner", "configurer", "member", "reader", "stranger", "admin")
        }
        owner_state = (await _activity(sessions, world.owner))["state"]
    # The owner, a ``config`` share and a ``config`` group grant decide; a
    # read-only share, a stranger and an administrator on a private agent do not.
    assert reviews == {"owner": 1, "configurer": 1, "member": 1, "reader": 0, "stranger": 0, "admin": 0}
    assert owner_state == "review"


async def test_a_recent_failure_shows_and_an_old_one_does_not() -> None:
    async with concierge_database() as sessions:
        world = await World().build(sessions)
        async with sessions() as db:
            await _cycle(db, status="failed", requested_by=world.owner.id, closed_ago=timedelta(hours=2))
            await db.commit()
        old = await _activity(sessions, world.owner)
        async with sessions() as db:
            await _cycle(db, status="failed", requested_by=world.owner.id, closed_ago=timedelta(minutes=5))
            await db.commit()
        recent = await _activity(sessions, world.owner)
        others = await _activity(sessions, world.reader)
    assert old["state"] == "idle"
    assert recent["state"] == "failed"
    assert others["state"] == "idle"


async def test_a_failed_run_without_its_end_time_still_shows() -> None:
    async with concierge_database() as sessions:
        world = await World().build(sessions)
        async with sessions() as db:
            await _run(db, world, world.owner, "failed")
            await db.commit()
        owner = await _activity(sessions, world.owner)
        reader = await _activity(sessions, world.reader)
    assert (owner["state"], reader["state"]) == ("failed", "idle")


async def test_the_review_count_is_reused_for_a_minute(monkeypatch) -> None:
    from app.services.concierge import companion

    calls: list[str] = []

    async def _count(_db, user):
        calls.append(str(user.id))
        return 3

    monkeypatch.setattr(companion, "_count_reviews", _count)
    monkeypatch.setattr(companion, "_review_cache", {})
    async with concierge_database() as sessions:
        world = await World().build(sessions)
        first = await _activity(sessions, world.owner)
        second = await _activity(sessions, world.owner)
        other = await _activity(sessions, world.reader)
    assert (first["review"], second["review"], other["review"]) == (3, 3, 3)
    assert calls == [str(world.owner.id), str(world.reader.id)]
