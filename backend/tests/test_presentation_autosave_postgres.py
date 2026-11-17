"""The editor's autosave against PostgreSQL and the REST routes.

Two editors autosave the same deck seconds apart, so the PATCH must serialise:
two saves carrying the same ``expected_revision`` meet one 200 and one 409,
never two writes of the same next revision. A run of saves by one person is
one history entry and one audit line; a save the server finds unchanged
writes neither. The presence beat, sent every few seconds while someone
edits, answers the deck's revision without reading its spec.

``integration``-marked and skips when PostgreSQL is unreachable, like its
neighbours; the temporary database is the shared ``tests/scratch_database.py``.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import event, select

import app.models  # noqa: F401 - registers every table on Base.metadata
from app.api import deps
from app.api.v1 import presentations
from app.core.database import get_db
from app.models.audit_log import AuditLog
from app.models.presentation import Presentation, PresentationVersion
from app.models.user import User
from app.services.presentation_spec import validate_spec
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration


@asynccontextmanager
async def _client(session_factory, user: User) -> AsyncIterator[httpx.AsyncClient]:
    """The presentations router, as *user*, on its own session per request."""
    app = FastAPI()
    app.include_router(presentations.router, prefix="/api/v1/presentations")

    async def _get_db():
        async with session_factory() as db:
            yield db
            await db.commit()

    app.dependency_overrides[deps.get_current_user] = lambda: user
    app.dependency_overrides[get_db] = _get_db
    app.dependency_overrides[deps.require_ai_enabled] = lambda: None
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


@pytest.fixture
def presence_offline(monkeypatch):
    """The beat without Redis: no budget to spend, an empty room."""

    async def _no_budget(*_args, **_kwargs):
        return None

    async def _empty_room(*_args, **_kwargs):
        return []

    monkeypatch.setattr(presentations, "enforce_rate_limit", _no_budget)
    monkeypatch.setattr(presentations, "heartbeat_presence", _empty_room)


async def test_two_autosaves_at_one_revision_meet_one_write_and_one_conflict(presence_offline) -> None:
    async with scratch_database(
        "presentation_autosave",
        tables=ALL_TABLES,
        extensions=("vector",),
        skip_without_extensions=True,
    ) as scratch:
        async with scratch.sessions() as db:
            owner = User(email="samuel@filigran.io", hashed_password="x", full_name="Samuel Hassine", role="user")
            db.add(owner)
            await db.flush()
            spec = validate_spec({"title": "Q3 review", "slides": [{"layout": "title", "title": "Q3 2026"}]})
            deck = Presentation(user_id=owner.id, title="Q3 review", theme="dark", slide_count=1, spec=spec)
            db.add(deck)
            await db.commit()
            deck_id, revision = deck.id, deck.revision
        url = f"/api/v1/presentations/{deck_id}"

        async with _client(scratch.sessions, owner) as client:
            # Two saves of the same revision, in flight together - two tabs, or
            # two editors' autosaves: the row lock lets one through, the other
            # reads the revision it wrote and is refused.
            first, second = await asyncio.gather(
                client.patch(url, json={"title": "Q3 review (draft A)", "expected_revision": revision}),
                client.patch(url, json={"title": "Q3 review (draft B)", "expected_revision": revision}),
            )
            statuses = sorted([first.status_code, second.status_code])
            assert statuses == [200, 409], (first.text, second.text)
            refused = first if first.status_code == 409 else second
            assert refused.json()["detail"]["current_revision"] == revision + 1

            # The next autosave continues the same edit session: one history
            # entry moved forward, still one audit line.
            later = await client.patch(url, json={"title": "Q3 review (final)", "expected_revision": revision + 1})
            assert later.status_code == 200, later.text

            # A save the server finds unchanged writes nothing at all.
            same = await client.patch(url, json={"title": "Q3 review (final)", "expected_revision": revision + 2})
            assert same.status_code == 200, same.text

            # The beat answers the revision.
            statements: list[str] = []

            def _record(_conn, _cursor, statement, *_args):
                statements.append(statement)

            event.listen(scratch.engine.sync_engine, "before_cursor_execute", _record)
            try:
                beat = await client.post(f"{url}/presence", json={"editing": True, "slide_id": "s1", "block": "title"})
            finally:
                event.remove(scratch.engine.sync_engine, "before_cursor_execute", _record)
            assert beat.status_code == 200, beat.text
            assert beat.json()["revision"] == revision + 2

        async with scratch.sessions() as db:
            stored = await db.get(Presentation, deck_id)
            assert stored.revision == revision + 2
            assert stored.title == "Q3 review (final)"
            versions = (
                (await db.execute(select(PresentationVersion).where(PresentationVersion.presentation_id == deck_id)))
                .scalars()
                .all()
            )
            assert [(v.revision, v.updated_at is not None) for v in versions] == [(revision + 2, True)]
            audits = (await db.execute(select(AuditLog).where(AuditLog.entity_id == deck_id))).scalars().all()
            assert len(audits) == 1

        deck_reads = [s for s in statements if "FROM presentations" in s and "presentation_versions" not in s]
        assert deck_reads, statements
        assert all("presentations.spec" not in s for s in deck_reads), deck_reads
