"""An assistant copies a deck, then deletes the copy, against PostgreSQL (``delete_presentation``).

The unit tests in ``test_presentation_search_and_delete.py`` pin who the tool
lets delete; this one proves what a delete MEANS, through the real visibility
predicate and the real foreign keys: a reader copies a deck shared with them
read-only, gives the copy a comment, a public link and a grant, and then has
their assistant delete it. The copy is gone from the library and from its own
URL, its history, comments and public link went with it, the original and its
own comment are untouched - and neither the reader nor an editor of the
original can delete the original.

``integration``-marked and skips when PostgreSQL is unreachable, like its
neighbours; the temporary database is the shared ``tests/scratch_database.py``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

import app.models  # noqa: F401 - registers every table on Base.metadata
from app.api import deps
from app.api.v1 import presentations
from app.core.database import get_db
from app.models.audit_log import AuditLog
from app.models.presentation import (
    Presentation,
    PresentationComment,
    PresentationPublicLink,
    PresentationVersion,
)
from app.models.user import User
from app.services.builtin_tools import build_presentation_tools
from app.services.presentation_public_links import enable_public_link
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


async def _tool(user: User, name: str, **arguments):
    _defs, executor = build_presentation_tools(db=None, user_id=str(user.id), conversation_id=None)  # type: ignore[arg-type]
    return await executor.execute_tool(ToolCall(id=name, name=name, arguments=arguments))


async def _rows(db, model, deck_id) -> int:
    return (await db.execute(select(func.count()).select_from(model).where(model.presentation_id == deck_id))).scalar_one()


async def test_the_assistant_deletes_the_copy_it_made_and_nothing_else() -> None:
    async with scratch_database(
        "presentation_delete",
        tables=ALL_TABLES,
        extensions=("vector",),
        skip_without_extensions=True,
    ) as scratch:
        async with scratch.sessions() as db:
            owner = User(email="owner@example.com", hashed_password="x", full_name="Deck Owner", role="user")
            reader = User(email="reader@example.com", hashed_password="x", full_name="Deck Reader", role="user")
            editor = User(email="editor@example.com", hashed_password="x", full_name="Deck Editor", role="user")
            db.add_all([owner, reader, editor])
            await db.flush()
            spec = validate_spec(
                {
                    "title": _TITLE,
                    "slides": [
                        {"layout": "title", "title": "Q3 2026"},
                        {"layout": "content", "title": "Numbers", "blocks": [{"type": "text", "content": "ARR grew."}]},
                    ],
                }
            )
            original = Presentation(
                user_id=owner.id,
                title=_TITLE,
                theme="dark",
                slide_count=2,
                spec=spec,
                shared_user_ids=[str(reader.id), str(editor.id)],
                shared_user_permissions={str(reader.id): "read_only", str(editor.id): "config"},
            )
            db.add(original)
            await db.flush()
            db.add(PresentationComment(presentation_id=original.id, author_id=owner.id, body="Check the ARR figure."))
            await db.commit()
            original_id = original.id

        # The original is shared read-only: the reader's assistant copies it.
        copied = await _tool(reader, "duplicate_presentation", presentation_id=str(original_id), title="My Q3 copy")
        assert not copied.is_error, copied.content
        async with scratch.sessions() as db:
            copy = (await db.execute(select(Presentation).where(Presentation.title == "My Q3 copy"))).scalar_one()
            copy_id = copy.id
            assert copy.user_id == reader.id
            # Everything a delete must take along: a comment, a public link, a grant.
            db.add(PresentationComment(presentation_id=copy_id, author_id=reader.id, body="Draft only."))
            copy.shared_user_ids = [str(owner.id)]
            copy.shared_user_permissions = {str(owner.id): "read_only"}
            await enable_public_link(db, copy_id, actor_id=reader.id)
            await db.commit()
            assert await _rows(db, PresentationVersion, copy_id) == 1
            assert await _rows(db, PresentationComment, copy_id) == 1
            assert await _rows(db, PresentationPublicLink, copy_id) == 1

        # Neither the reader nor an editor of the original may delete it.
        for someone, grant in ((reader, "read-only"), (editor, "for editing")):
            refused = await _tool(someone, "delete_presentation", presentation_id=str(original_id))
            assert refused.is_error and refused.refused, refused.content
            assert grant in refused.content

        # The reader deletes the copy.
        deleted = await _tool(reader, "delete_presentation", presentation_id=str(copy_id))
        assert not deleted.is_error, deleted.content
        assert "**My Q3 copy**" in deleted.content
        assert "no trash" in deleted.content

        async with scratch.sessions() as db:
            assert await db.get(Presentation, copy_id) is None
            assert await _rows(db, PresentationVersion, copy_id) == 0
            assert await _rows(db, PresentationComment, copy_id) == 0
            assert await _rows(db, PresentationPublicLink, copy_id) == 0
            audit = (
                await db.execute(select(AuditLog).where(AuditLog.entity_id == copy_id, AuditLog.action == "delete"))
            ).scalar_one()
            assert audit.user_id == reader.id
            assert audit.details["via"] == "agent"
            # The original and its own comment are untouched.
            kept = await db.get(Presentation, original_id)
            assert kept is not None and kept.title == _TITLE
            assert await _rows(db, PresentationComment, original_id) == 1

        # No longer listed, no longer openable - for its owner or the person it was shared with.
        for someone in (reader, owner):
            async with _client(scratch.sessions, someone) as client:
                assert (await client.get(f"/api/v1/presentations/{copy_id}")).status_code == 404
                listed = (await client.get("/api/v1/presentations")).json()
                assert str(copy_id) not in [deck["id"] for deck in listed["items"]]
                assert str(original_id) in [deck["id"] for deck in listed["items"]]

        # A deck already deleted is refused like one that never existed.
        again = await _tool(reader, "delete_presentation", presentation_id=str(copy_id))
        assert again.is_error and again.refused
        assert "you can access was found" in again.content
