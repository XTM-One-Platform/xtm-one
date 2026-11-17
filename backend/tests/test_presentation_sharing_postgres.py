"""An assistant shares a deck it built, against PostgreSQL and the REST routes.

The unit tests in ``test_presentations.py`` pin what ``share_presentation``
writes; this one proves what the write MEANS for the people it names, through
the real visibility predicate: the owner's assistant shares the Q3 deck
read-only with ten people by email in one call, each of them opens and exports
it and cannot edit it, a read-only grantee's own assistant is refused, and a
``remove`` takes the deck away at once - that person's ``GET`` is a 404.

``integration``-marked and skips when PostgreSQL is unreachable, like its
neighbours; the temporary database is the shared ``tests/scratch_database.py``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

import app.models  # noqa: F401 - registers every table on Base.metadata
from app.api import deps
from app.api.v1 import presentations
from app.core.database import get_db
from app.models.audit_log import AuditLog
from app.models.presentation import Presentation
from app.models.user import User
from app.services.builtin_tools import build_presentation_tools
from app.services.presentation_spec import validate_spec
from app.tools.base import ToolCall
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration

_TITLE = "Quarterly Review, Q3 2026"


@asynccontextmanager
async def _client(session_factory: async_sessionmaker, user: User) -> AsyncIterator[httpx.AsyncClient]:
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


async def _share(user: User, **arguments):
    _defs, executor = build_presentation_tools(db=None, user_id=str(user.id), conversation_id=None)  # type: ignore[arg-type]
    return await executor.execute_tool(ToolCall(id="share", name="share_presentation", arguments=arguments))


@pytest.fixture
def export_stub(monkeypatch):
    """The export route without its headless browser and its Redis budget."""

    async def _render(**_kw):
        return SimpleNamespace(data=b"%PDF-1.7", content_type="application/pdf", filename="q3.pdf", notes=())

    async def _no_budget(_user_id):
        return None

    monkeypatch.setattr(presentations, "export_presentation", _render)
    monkeypatch.setattr(presentations, "enforce_export_rate_limit", _no_budget)


async def test_the_assistant_shares_the_q3_deck_with_the_emt_and_takes_it_back(export_stub) -> None:
    async with scratch_database(
        "presentation_sharing",
        tables=ALL_TABLES,
        extensions=("vector",),
        skip_without_extensions=True,
    ) as scratch:
        async with scratch.sessions() as db:
            owner = User(email="samuel@filigran.io", hashed_password="x", full_name="Samuel Hassine", role="user")
            emt = [
                User(email=f"emt{i}@filigran.io", hashed_password="x", full_name=f"EMT Member {i}", role="user")
                for i in range(10)
            ]
            outsider = User(email="outsider@filigran.io", hashed_password="x", full_name="Outsider", role="user")
            db.add_all([owner, *emt, outsider])
            await db.flush()
            spec = validate_spec({"title": _TITLE, "slides": [{"layout": "title", "title": "Q3 2026"}]})
            deck = Presentation(user_id=owner.id, title=_TITLE, theme="dark", slide_count=1, spec=spec)
            db.add(deck)
            await db.commit()
            deck_id = deck.id
        url = f"/api/v1/presentations/{deck_id}"

        # One call, ten addresses written as a person would paste them.
        shared = await _share(
            owner,
            presentation_id=str(deck_id),
            users=[member.email.upper() for member in emt],
            permission="read_only",
        )
        assert not shared.is_error, shared.content
        assert "Given access (can view) to 10 people" in shared.content

        async with scratch.sessions() as db:
            stored = await db.get(Presentation, deck_id)
            assert sorted(stored.shared_user_ids) == sorted(str(member.id) for member in emt)
            assert set(stored.shared_user_permissions.values()) == {"read_only"}
            audits = (await db.execute(select(AuditLog).where(AuditLog.entity_id == deck_id))).scalars().all()
            assert len(audits) == 1
            assert audits[0].user_id == owner.id
            assert audits[0].details["via"] == "agent"
            assert audits[0].details["permission"] == "read_only"
            assert sorted(audits[0].details["added_users"]) == sorted(str(member.id) for member in emt)

        # Each of them opens it, presents and exports it, and cannot edit it.
        for member in emt:
            async with _client(scratch.sessions, member) as client:
                opened = await client.get(url)
                assert opened.status_code == 200, opened.text
                assert opened.json()["permission"] == "read_only"
                assert opened.json()["can_manage"] is False
                exported = await client.post(f"{url}/export", json={"format": "pdf"})
                assert exported.status_code == 200, exported.text
                edited = await client.patch(url, json={"title": "Changed by a reader"})
                assert edited.status_code == 403, edited.text
        async with _client(scratch.sessions, outsider) as client:
            assert (await client.get(url)).status_code == 404

        # A read-only grantee's own assistant cannot pass the deck on.
        refused = await _share(emt[0], presentation_id=str(deck_id), users=[outsider.email])
        assert refused.is_error
        assert "but not share it" in refused.content

        # An address no account answers to changes nothing.
        unknown = await _share(owner, presentation_id=str(deck_id), users=["partner@outside.example"])
        assert unknown.is_error
        assert '"partner@outside.example"' in unknown.content

        # remove takes it away at once: their GET is a 404, the others keep it.
        removed = await _share(owner, presentation_id=str(deck_id), users=[emt[3].email], action="remove")
        assert not removed.is_error, removed.content
        async with _client(scratch.sessions, emt[3]) as client:
            assert (await client.get(url)).status_code == 404
        async with _client(scratch.sessions, emt[4]) as client:
            assert (await client.get(url)).status_code == 200

        # The Share dialog's PATCH goes through the same write: the owner puts
        # them back by hand, next to the grants the assistant made.
        async with _client(scratch.sessions, owner) as client:
            current = (await client.get(url)).json()
            restored = await client.patch(
                url,
                json={
                    "shared_user_ids": [*current["shared_user_ids"], str(emt[3].id)],
                    "shared_user_permissions": {**current["shared_user_permissions"], str(emt[3].id): "read_only"},
                },
            )
            assert restored.status_code == 200, restored.text
            assert len(restored.json()["shared_user_ids"]) == 10
        async with _client(scratch.sessions, emt[3]) as client:
            assert (await client.get(url)).status_code == 200

        async with scratch.sessions() as db:
            rows = (await db.execute(select(AuditLog).where(AuditLog.entity_id == deck_id))).scalars().all()
            audits = [audit for audit in rows if (audit.details or {}).get("via") == "agent"]
            # One row per agent call that changed something: the share and the removal.
            assert sorted(audit.details["action"] for audit in audits) == ["add", "remove"]
            removal = next(audit for audit in audits if audit.details["action"] == "remove")
            assert removal.details["removed_users"] == [str(emt[3].id)]
