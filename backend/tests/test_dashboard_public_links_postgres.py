"""A dashboard's published link, through the real routes, against PostgreSQL (and Redis for its lock).

Managers publish, regenerate and turn off a link; a ``read_only`` grantee or a
stranger cannot. A visitor with no account receives the title, the definition
reduced to what draws (plain sources: no question, agent or query; no inputs,
no click actions) and the snapshot read with the publisher's access - a
question's answer never in it, directly or through a derived source. A
change of the dashboard reads it again on the next visit. The link answers
nothing once regenerated, turned off, its publisher's share revoked, or the
admin switch turned off.

``integration``-marked; the temporary database is ``tests/scratch_database.py``.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from types import SimpleNamespace

import httpx
import pytest
from fastapi import Depends, FastAPI
from sqlalchemy.ext.asyncio import AsyncSession

import app.models  # noqa: F401 - registers every table on Base.metadata
from app.api import deps
from app.api.v1 import custom_dashboards, dashboard_public
from app.core.database import get_db
from app.models.agent import Agent
from app.models.custom_dashboard import CustomDashboard, CustomDashboardPublicLink
from app.models.user import User
from app.services import dashboard_public_links
from app.services.dashboard_definition import validate_definition
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("dashboard_studio_enabled")]


@pytest.fixture(autouse=True)
def links_switch(monkeypatch) -> dict:
    """The admin switch, on unless a test turns it off."""
    from app.services import dashboard_public_links

    state = {"on": True}
    original = dashboard_public_links.security_option
    monkeypatch.setattr(
        dashboard_public_links,
        "security_option",
        lambda key: state["on"] if key == "dashboard_public_links_enabled" else original(key),
    )
    return state


class _Locks:
    """The refresh lock's Redis: ``SET NX`` and ``DELETE``, per test (a real client is bound to one event loop)."""

    def __init__(self) -> None:
        self.held: set[str] = set()
        self.taken: list[str] = []

    async def set(self, key: str, value: str, *, nx: bool = False, ex: int | None = None) -> bool:
        if nx and key in self.held:
            return False
        self.held.add(key)
        self.taken.append(key)
        return True

    async def delete(self, key: str) -> int:
        self.held.discard(key)
        return 1


@pytest.fixture(autouse=True)
def locks(monkeypatch) -> _Locks:
    import app.core.redis as redis_module

    store = _Locks()

    async def _redis():
        return store

    monkeypatch.setattr(redis_module, "redis_client", _redis)
    return store


@pytest.fixture(autouse=True)
def limits(monkeypatch) -> list[dict]:
    """The rate limits asked for, recorded (their store is Redis; the limiter has its own tests)."""
    asked: list[dict] = []

    async def _record(request, name, **kwargs):
        asked.append({"name": name, **kwargs})

    monkeypatch.setattr(dashboard_public, "enforce_rate_limit", _record)
    monkeypatch.setattr(custom_dashboards, "enforce_rate_limit", _record)
    return asked


def _definition(agent_id: uuid.UUID) -> dict:
    return {
        "version": 2,
        "title": "Ops",
        "dataSources": {
            "open": {"type": "static", "name": "Open findings", "options": {"shape": "number", "data": {"value": 37}}},
            "q": {"type": "ask", "options": {"agent_id": str(agent_id), "question": "Which hosts are at risk?", "shape": "text"}},
            "q_top": {"type": "derived", "options": {"extends": "q", "steps": [{"op": "head", "count": 3}]}},
        },
        "visualizations": {
            "kpi": {
                "type": "xtm.singlevalue",
                "title": "Open",
                "dataSources": {"primary": "open"},
                "eventHandlers": [{"type": "drilldown.setToken", "options": {"tokens": [{"token": "picked", "key": "value"}]}}],
            },
            "answer": {"type": "xtm.answer", "title": "Risk", "dataSources": {"primary": "q"}},
        },
        "inputs": {"sev": {"type": "input.dropdown", "title": "Severity", "token": "sev", "defaultValue": "High", "options": {"items": [{"label": "High", "value": "High"}]}}},
        "layout": {
            "tabs": {"items": [{"layoutId": "main", "label": "Main"}]},
            "layoutDefinitions": {
                "main": {
                    "structure": [
                        {"item": "kpi", "position": {"x": 0, "y": 0, "w": 6, "h": 6}},
                        {"item": "answer", "position": {"x": 6, "y": 0, "w": 6, "h": 6}},
                    ]
                }
            },
            "globalInputs": ["sev"],
        },
    }


@asynccontextmanager
async def _client(session_factory, user_id: uuid.UUID | None) -> AsyncIterator[httpx.AsyncClient]:
    app = FastAPI()
    app.include_router(custom_dashboards.router, prefix="/api/v1/custom-dashboards")
    app.include_router(dashboard_public.router, prefix="/api/v1/public")

    async def _get_db():
        async with session_factory() as db:
            yield db
            await db.commit()

    async def _signed_in(db: AsyncSession = Depends(get_db)) -> User:
        if user_id is None:
            from fastapi import HTTPException

            raise HTTPException(status_code=401, detail="Not authenticated")
        return await db.get(User, user_id)

    for dependency in (deps.get_current_user, deps.get_current_user_ungated):
        app.dependency_overrides[dependency] = _signed_in
    app.dependency_overrides[get_db] = _get_db
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        yield client


@asynccontextmanager
async def _world():
    async with scratch_database(
        "dashboard_public_links", tables=ALL_TABLES, extensions=("vector",), skip_without_extensions=True
    ) as scratch:
        async with scratch.sessions() as db:
            people = {
                name: User(email=f"{name}@example.com", hashed_password="x", full_name=name.title(), role="user")
                for name in ("owner", "editor", "viewer", "stranger")
            }
            db.add_all(people.values())
            await db.flush()
            agent = Agent(id=uuid.uuid4(), name="Analyst", user_id=people["owner"].id)
            db.add(agent)
            await db.flush()
            share = {
                "shared_user_ids": [str(people["editor"].id), str(people["viewer"].id)],
                "shared_user_permissions": {str(people["editor"].id): "config", str(people["viewer"].id): "read_only"},
            }
            dashboard = CustomDashboard(
                user_id=people["owner"].id, title="Ops", widget_count=2, revision=1, spec=validate_definition(_definition(agent.id)), **share
            )  # fmt: skip
            db.add(dashboard)
            await db.commit()
            ids = SimpleNamespace(**{name: person.id for name, person in people.items()}, dashboard=dashboard.id)
        yield scratch, ids


def _manage(ids) -> str:
    return f"/api/v1/custom-dashboards/{ids.dashboard}/public-link"


def _token(url: str) -> str:
    return url.rsplit("/", 1)[1]


async def test_a_manager_publishes_and_a_visitor_sees_the_snapshot_and_nothing_of_its_sources() -> None:
    async with _world() as (scratch, ids):
        async with _client(scratch.sessions, ids.viewer) as viewer:
            refused = await viewer.post(_manage(ids), json={})
        async with _client(scratch.sessions, ids.editor) as editor:
            published = await editor.post(_manage(ids), json={"refresh_minutes": 240})
        async with _client(scratch.sessions, None) as visitor:
            page = await visitor.get(f"/api/v1/public/dashboards/{_token(published.json()['url'])}")
    assert refused.status_code == 403
    assert published.status_code == 200, published.text
    link = published.json()
    assert (link["enabled_on_platform"], link["publisher_name"], link["refresh_minutes"]) == (True, "Editor", 240)
    assert link["url"].startswith("http") and link["url"].endswith(link["path"])
    assert link["path"] == f"/public/dashboards/{_token(link['url'])}"
    assert link["snapshot_at"] is not None
    assert page.status_code == 200, page.text
    assert page.headers["cache-control"] == "no-store"
    assert page.headers["referrer-policy"] == "no-referrer"
    body = page.json()
    assert body["title"] == "Ops"
    definition = body["definition"]
    assert definition["dataSources"] == {
        "open": {"type": "static", "name": "Open findings", "options": {"shape": "number", "data": {}}},
        "q": {"type": "static", "options": {"shape": "number", "data": {}}},
        "q_top": {"type": "static", "options": {"shape": "number", "data": {}}},
    }
    assert "eventHandlers" not in definition["visualizations"]["kpi"]
    assert (definition["inputs"], definition["layout"]["globalInputs"]) == ({}, [])
    snapshot = body["snapshot"]
    assert snapshot["widgets"]["kpi"]["data"] == {"value": 37.0, "change": None}
    assert snapshot["widgets"]["kpi"]["open_url"] is None
    # A question, and what derives from it, are never read for a snapshot.
    no_result = {"status": "error", "code": "no_result", "open_url": None}
    assert (snapshot["sources"]["q"], snapshot["sources"]["q_top"], snapshot["widgets"]["answer"]) == (no_result, no_result, no_result)
    assert "Which hosts" not in page.text


async def test_an_edit_is_read_again_on_the_next_visit() -> None:
    async with _world() as (scratch, ids):
        async with _client(scratch.sessions, ids.owner) as owner:
            token = _token((await owner.post(_manage(ids), json={})).json()["url"])
            source = {"type": "static", "name": "Open findings", "options": {"shape": "number", "data": {"value": 41}}}
            edited = await owner.patch(
                f"/api/v1/custom-dashboards/{ids.dashboard}",
                json={"revision": 1, "operations": [{"op": "set", "path": "dataSources.open", "value": source}]},
            )
        async with _client(scratch.sessions, None) as visitor:
            page = await visitor.get(f"/api/v1/public/dashboards/{token}")
        async with scratch.sessions() as db:
            from sqlalchemy import select

            link = (await db.execute(select(CustomDashboardPublicLink))).scalar_one()
    assert edited.status_code == 200, edited.text
    assert page.status_code == 200, page.text
    assert page.json()["snapshot"]["widgets"]["kpi"]["data"]["value"] == 41.0
    assert link.snapshot_revision == 2


async def test_a_regenerated_or_turned_off_link_answers_nothing() -> None:
    async with _world() as (scratch, ids), _client(scratch.sessions, ids.owner) as owner:
        first = _token((await owner.post(_manage(ids), json={})).json()["url"])
        second = _token((await owner.post(_manage(ids), json={"regenerate": True})).json()["url"])
        async with _client(scratch.sessions, None) as visitor:
            old = await visitor.get(f"/api/v1/public/dashboards/{first}")
            new = await visitor.get(f"/api/v1/public/dashboards/{second}")
        off = await owner.delete(_manage(ids))
        after = await owner.get(_manage(ids))
        async with _client(scratch.sessions, None) as visitor:
            gone = await visitor.get(f"/api/v1/public/dashboards/{second}")
            malformed = await visitor.get("/api/v1/public/dashboards/not-a-token")
    assert first != second
    assert (old.status_code, new.status_code, off.status_code, gone.status_code, malformed.status_code) == (404, 200, 204, 404, 404)
    assert old.json() == gone.json() == malformed.json() == {"detail": "This dashboard is not available"}
    assert after.json() == {"enabled_on_platform": True, "url": None, "path": None, "created_at": None, "publisher_name": None, "refresh_minutes": None, "snapshot_at": None, "marking_ceiling": []}  # fmt: skip


async def test_a_link_stops_with_its_publishers_access() -> None:
    async with _world() as (scratch, ids):
        async with _client(scratch.sessions, ids.editor) as editor:
            token = _token((await editor.post(_manage(ids), json={})).json()["url"])
        async with _client(scratch.sessions, ids.owner) as owner:
            revoked = await owner.put(
                f"/api/v1/custom-dashboards/{ids.dashboard}/sharing",
                json={"shared_user_ids": [str(ids.viewer)], "shared_user_permissions": {str(ids.viewer): "read_only"}},
            )
            refresh = await owner.post(f"{_manage(ids)}/refresh")
        async with _client(scratch.sessions, None) as visitor:
            page = await visitor.get(f"/api/v1/public/dashboards/{token}")
    assert revoked.status_code == 200, revoked.text
    assert page.status_code == 404
    # Read again with someone else's access only by publishing again.
    assert refresh.status_code == 409


async def test_the_admin_switch_stops_every_link_and_refuses_new_ones(links_switch) -> None:
    async with _world() as (scratch, ids), _client(scratch.sessions, ids.owner) as owner:
        token = _token((await owner.post(_manage(ids), json={})).json()["url"])
        links_switch["on"] = False
        refused = await owner.post(_manage(ids), json={"regenerate": True})
        state = await owner.get(_manage(ids))
        async with _client(scratch.sessions, None) as visitor:
            page = await visitor.get(f"/api/v1/public/dashboards/{token}")
        links_switch["on"] = True
        async with _client(scratch.sessions, None) as visitor:
            back = await visitor.get(f"/api/v1/public/dashboards/{token}")
    assert refused.status_code == 403
    assert state.json()["enabled_on_platform"] is False
    assert (page.status_code, back.status_code) == (404, 200)


async def test_only_managers_read_or_change_the_link() -> None:
    async with _world() as (scratch, ids):
        async with _client(scratch.sessions, ids.viewer) as viewer:
            read = await viewer.get(_manage(ids))
            patch = await viewer.patch(_manage(ids), json={"refresh_minutes": 15})
            delete = await viewer.delete(_manage(ids))
        async with _client(scratch.sessions, ids.stranger) as stranger:
            hidden = await stranger.get(_manage(ids))
        async with _client(scratch.sessions, ids.owner) as owner:
            no_link = await owner.patch(_manage(ids), json={"refresh_minutes": 15})
            odd = await owner.post(_manage(ids), json={"refresh_minutes": 7})
    assert (read.status_code, patch.status_code, delete.status_code, hidden.status_code) == (403, 403, 403, 404)
    assert (no_link.status_code, odd.status_code) == (404, 422)


async def test_a_marking_ceiling_is_kept_and_read_again_with_the_publisher_s_access(monkeypatch) -> None:
    reads: list[tuple] = []
    original = dashboard_public_links.read_snapshot

    async def recording(db, definition, dashboard_id, user_id, marking_ceiling=()):
        reads.append((user_id, marking_ceiling))
        return await original(db, definition, dashboard_id, user_id, marking_ceiling)

    monkeypatch.setattr(dashboard_public_links, "read_snapshot", recording)
    async with _world() as (scratch, ids):
        async with _client(scratch.sessions, ids.editor) as editor:
            published = await editor.post(_manage(ids), json={"marking_ceiling": ["TLP:AMBER"]})
        async with _client(scratch.sessions, ids.owner) as owner:
            changed = await owner.patch(_manage(ids), json={"marking_ceiling": ["TLP:GREEN", "PAP:GREEN"]})
            nothing = await owner.patch(_manage(ids), json={})
            odd = await owner.patch(_manage(ids), json={"marking_ceiling": ["TLP:<b>"]})
            too_many = await owner.patch(_manage(ids), json={"marking_ceiling": [f"M{n}" for n in range(11)]})
    assert published.status_code == 200, published.text
    assert published.json()["marking_ceiling"] == ["TLP:AMBER"]
    assert changed.status_code == 200, changed.text
    assert changed.json()["marking_ceiling"] == ["TLP:GREEN", "PAP:GREEN"]
    # The first read is the editor's (who published); the owner changed the ceiling, so the owner reads it now
    # and publishes it: a ceiling never widens what the link shows with someone else's access.
    assert reads == [(ids.editor, ("TLP:AMBER",)), (ids.owner, ("TLP:GREEN", "PAP:GREEN"))]
    assert changed.json()["publisher_name"] == "Owner"
    assert (nothing.status_code, odd.status_code, too_many.status_code) == (422, 422, 422)


async def test_under_a_ceiling_an_agent_s_answer_is_not_kept(monkeypatch) -> None:
    kept: list[str] = []

    async def read_source(db, user_id, definition, source_id, range_key=None, **kwargs):
        kept.append(source_id)
        return {"status": "ok", "shape": "text", "data": {"text": "x"}}

    monkeypatch.setattr("app.services.dashboard_sources.read_source", read_source)
    definition = {"dataSources": {
        "cti": {"type": "platform", "options": {}},
        "answer": {"type": "assignment", "options": {}},
        "top": {"type": "derived", "options": {"extends": "answer"}},
    }, "visualizations": {}}  # fmt: skip
    under = await dashboard_public_links.read_snapshot(None, definition, uuid.uuid4(), uuid.uuid4(), ("TLP:GREEN",))
    assert kept == ["cti"]
    assert (under["sources"]["answer"]["code"], under["sources"]["top"]["code"]) == ("no_result", "no_result")
    kept.clear()
    await dashboard_public_links.read_snapshot(None, definition, uuid.uuid4(), uuid.uuid4())
    assert kept == ["cti", "answer", "top"]


async def test_administrators_review_every_published_link_and_no_one_else() -> None:
    async with _world() as (scratch, ids):
        async with scratch.sessions() as db:
            admin = User(email="admin@example.com", hashed_password="x", full_name="Admin", role="admin")
            db.add(admin)
            await db.commit()
            admin_id = admin.id
        async with _client(scratch.sessions, ids.editor) as editor:
            await editor.post(_manage(ids), json={"refresh_minutes": 240})
            refused = await editor.get("/api/v1/custom-dashboards/public-links")
        async with _client(scratch.sessions, admin_id) as reviewer:
            listed = await reviewer.get("/api/v1/custom-dashboards/public-links")
            # An administrator reads another person's dashboard only: the link is turned off from the review list.
            as_manager = await reviewer.delete(_manage(ids))
            turned_off = await reviewer.delete(f"/api/v1/custom-dashboards/public-links/{ids.dashboard}")
            after = await reviewer.get("/api/v1/custom-dashboards/public-links")
        async with _client(scratch.sessions, ids.owner) as owner:
            not_admin = await owner.delete(f"/api/v1/custom-dashboards/public-links/{ids.dashboard}")
    assert refused.status_code == 403
    assert listed.status_code == 200, listed.text
    [item] = listed.json()
    assert (item["dashboard_id"], item["dashboard_title"], item["publisher_name"], item["refresh_minutes"]) == (str(ids.dashboard), "Ops", "Editor", 240)
    # The link itself is not shown: a manager copies it from its dashboard.
    assert "public/dashboards" not in listed.text
    assert (as_manager.status_code, turned_off.status_code, not_admin.status_code) == (403, 204, 403)
    assert after.json() == []


async def test_a_visit_during_a_refresh_is_served_the_snapshot_kept(locks) -> None:
    async with _world() as (scratch, ids):
        async with _client(scratch.sessions, ids.owner) as owner:
            token = _token((await owner.post(_manage(ids), json={})).json()["url"])
            source = {"type": "static", "name": "Open findings", "options": {"shape": "number", "data": {"value": 41}}}
            await owner.patch(
                f"/api/v1/custom-dashboards/{ids.dashboard}",
                json={"revision": 1, "operations": [{"op": "set", "path": "dataSources.open", "value": source}]},
            )
        # Another request is reading it again: this one does not read a second time.
        locks.held.update(key for key in locks.taken)
        async with _client(scratch.sessions, None) as visitor:
            page = await visitor.get(f"/api/v1/public/dashboards/{token}")
    assert page.json()["snapshot"]["widgets"]["kpi"]["data"]["value"] == 37.0
    assert len(locks.taken) == 1


async def test_every_public_read_is_budgeted_per_address_and_refused_when_unsure(limits) -> None:
    async with _world() as (scratch, ids):
        async with _client(scratch.sessions, ids.owner) as owner:
            token = _token((await owner.post(_manage(ids), json={})).json()["url"])
        async with _client(scratch.sessions, None) as visitor:
            await visitor.get(f"/api/v1/public/dashboards/{token}")
            await visitor.get(f"/api/v1/public/dashboards/{token}/assets/{uuid.uuid4()}")
    assert [entry for entry in limits if entry["name"].startswith("dashboard_public")] == [
        {"name": "dashboard_public", "limit": 300, "window_seconds": 60, "fail_closed": True},
        {"name": "dashboard_public_asset", "limit": 3000, "window_seconds": 60, "fail_closed": True},
    ]


async def test_publishing_again_applies_the_settings_asked_for_to_the_link_kept(monkeypatch) -> None:
    """A POST on a dashboard that has a link keeps its address but never answers with other settings than the ones
    asked for: the refresh and the ceiling apply, as PATCH does, and a ceiling is read with the caller's access."""
    reads: list[tuple] = []
    original = dashboard_public_links.read_snapshot

    async def recording(db, definition, dashboard_id, user_id, marking_ceiling=()):
        reads.append((user_id, marking_ceiling))
        return await original(db, definition, dashboard_id, user_id, marking_ceiling)

    monkeypatch.setattr(dashboard_public_links, "read_snapshot", recording)
    async with _world() as (scratch, ids):
        async with _client(scratch.sessions, ids.owner) as owner:
            first = await owner.post(_manage(ids), json={"refresh_minutes": 240})
            same = await owner.post(_manage(ids), json={})
        async with _client(scratch.sessions, ids.editor) as editor:
            again = await editor.post(_manage(ids), json={"refresh_minutes": 15, "marking_ceiling": ["TLP:GREEN"]})
            refresh_only = await editor.post(_manage(ids), json={"refresh_minutes": 60})
    assert [r.status_code for r in (first, same, again, refresh_only)] == [200, 200, 200, 200]
    # One address throughout: publishing again regenerates nothing.
    assert {r.json()["url"] for r in (first, same, again, refresh_only)} == {first.json()["url"]}
    # Nothing asked, nothing changed.
    assert (same.json()["refresh_minutes"], same.json()["marking_ceiling"], same.json()["publisher_name"]) == (240, [], "Owner")
    assert (again.json()["refresh_minutes"], again.json()["marking_ceiling"], again.json()["publisher_name"]) == (15, ["TLP:GREEN"], "Editor")
    assert (refresh_only.json()["refresh_minutes"], refresh_only.json()["marking_ceiling"]) == (60, ["TLP:GREEN"])
    # The first snapshot is the owner's; the ceiling is read at once with the editor's access, the refresh reads nothing.
    assert reads == [(ids.owner, ()), (ids.editor, ("TLP:GREEN",))]


async def test_whoever_changes_what_the_link_reads_becomes_its_publisher(monkeypatch) -> None:
    reads: list[uuid.UUID] = []
    original = dashboard_public_links.read_snapshot

    async def recording(db, definition, dashboard_id, user_id, marking_ceiling=()):
        reads.append(user_id)
        return await original(db, definition, dashboard_id, user_id, marking_ceiling)

    monkeypatch.setattr(dashboard_public_links, "read_snapshot", recording)
    url = lambda ids: f"/api/v1/custom-dashboards/{ids.dashboard}"  # noqa: E731
    async with _world() as (scratch, ids):
        async with _client(scratch.sessions, ids.owner) as owner:
            token = _token((await owner.post(_manage(ids), json={})).json()["url"])
        async with _client(scratch.sessions, ids.editor) as editor:
            # Moving a widget reads nothing new: the owner still publishes.
            moved = await editor.patch(url(ids), json={"revision": 1, "operations": [{"op": "layout", "layoutId": "main", "items": [{"id": "kpi", "x": 0, "y": 6, "w": 6, "h": 6}]}]})
            async with _client(scratch.sessions, None) as visitor:
                after_move = await visitor.get(f"/api/v1/public/dashboards/{token}")
            source = {"type": "static", "name": "Open findings", "options": {"shape": "number", "data": {"value": 41}}}
            edited = await editor.patch(url(ids), json={"revision": 2, "operations": [{"op": "set", "path": "dataSources.open", "value": source}]})
            state = await editor.get(_manage(ids))
        async with _client(scratch.sessions, None) as visitor:
            after_edit = await visitor.get(f"/api/v1/public/dashboards/{token}")
    assert (moved.status_code, edited.status_code) == (200, 200), (moved.text, edited.text)
    assert (after_move.status_code, after_edit.status_code) == (200, 200)
    assert reads == [ids.owner, ids.owner, ids.editor]
    assert state.json()["publisher_name"] == "Editor"
    assert after_edit.json()["snapshot"]["widgets"]["kpi"]["data"]["value"] == 41.0


async def test_a_publisher_who_can_no_longer_edit_serves_nothing() -> None:
    async with _world() as (scratch, ids):
        async with _client(scratch.sessions, ids.editor) as editor:
            token = _token((await editor.post(_manage(ids), json={})).json()["url"])
        async with _client(scratch.sessions, ids.owner) as owner:
            # The editor keeps reading the dashboard, but no longer edits it.
            downgraded = await owner.put(
                f"/api/v1/custom-dashboards/{ids.dashboard}/sharing",
                json={"shared_user_ids": [str(ids.editor)], "shared_user_permissions": {str(ids.editor): "read_only"}},
            )
        async with _client(scratch.sessions, None) as visitor:
            page = await visitor.get(f"/api/v1/public/dashboards/{token}")
    assert downgraded.status_code == 200, downgraded.text
    assert page.status_code == 404

