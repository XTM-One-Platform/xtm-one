"""A deck opened by its public link, against PostgreSQL, the REST routes and an anonymous client.

The unit tests in ``test_presentation_public_links.py`` pin what each piece
writes; this one proves what a link MEANS for someone with no account: the
owner turns it on from the Share dialog's routes, an anonymous client opens
the deck - its slides and its picture, never a speaker note, never a file the
deck does not show - a read-only grantee is told the deck is public but cannot
touch the link, regenerating it kills the old URL at once, the admin switch
hides every link and refuses new ones, and the owner's assistant turns it off.
The second test does the same for the PDF a link may offer, the
headless browser stubbed.

``integration``-marked and skips when PostgreSQL is unreachable, like its
neighbours; the temporary database is the shared ``tests/scratch_database.py``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

import app.models  # noqa: F401 - registers every table on Base.metadata
from app.api import deps
from app.api.v1 import presentation_public, presentations
from app.core.config import settings
from app.core.database import get_db
from app.models.audit_log import AuditLog
from app.models.file_attachment import FileAttachment
from app.models.presentation import Presentation, PresentationPublicLink
from app.models.user import User
from app.services.builtin_tools import build_presentation_tools
from app.services.presentation_spec import validate_spec
from app.tools.base import ToolCall
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration

_TITLE = "Quarterly Review, Q3 2026"
_NOTE = "Pause here: the pipeline number is the story."
_PNG = b"\x89PNG\r\n\x1a\nq3-chart"


@asynccontextmanager
async def _client(session_factory: async_sessionmaker, user: User | None) -> AsyncIterator[httpx.AsyncClient]:
    """Both routers on their own session per request; *user* signed in, or nobody."""
    api = FastAPI()
    api.include_router(presentations.router, prefix="/api/v1/presentations")
    api.include_router(presentation_public.router, prefix="/api/v1/public")

    async def _get_db():
        async with session_factory() as db:
            yield db
            await db.commit()

    if user is not None:
        api.dependency_overrides[deps.get_current_user] = lambda: user
    api.dependency_overrides[get_db] = _get_db
    api.dependency_overrides[deps.require_ai_enabled] = lambda: None
    transport = httpx.ASGITransport(app=api)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


async def _set_link(user: User, **arguments):
    _defs, executor = build_presentation_tools(db=None, user_id=str(user.id), conversation_id=None)  # type: ignore[arg-type]
    return await executor.execute_tool(ToolCall(id="link", name="set_presentation_public_link", arguments=arguments))


@pytest.fixture
def stubs(monkeypatch):
    """The public routes without their Redis budget, and the object store answering one picture."""
    from app.services import file_storage as storage

    async def _no_budget(*_a, **_kw):
        return None

    async def _download(object_name):
        assert object_name == "generated/q3-chart.png"
        return _PNG

    monkeypatch.setattr(presentation_public, "enforce_rate_limit", _no_budget)
    monkeypatch.setattr(storage.file_storage, "download", _download)


async def test_anyone_with_the_link_views_the_deck_until_it_is_turned_off(stubs, monkeypatch) -> None:
    async with scratch_database(
        "presentation_public_links",
        tables=ALL_TABLES,
        extensions=("vector",),
        skip_without_extensions=True,
    ) as scratch:
        async with scratch.sessions() as db:
            owner = User(email="samuel@filigran.io", hashed_password="x", full_name="Samuel Hassine", role="user")
            reader = User(email="ada@filigran.io", hashed_password="x", full_name="Ada Lovelace", role="user")
            db.add_all([owner, reader])
            await db.flush()
            picture = FileAttachment(
                user_id=owner.id,
                filename="q3-chart.png",
                content_type="image/png",
                size=len(_PNG),
                s3_object_name="generated/q3-chart.png",
            )
            elsewhere = FileAttachment(
                user_id=owner.id,
                filename="board-minutes.pdf",
                content_type="application/pdf",
                size=3,
                s3_object_name="uploads/board-minutes.pdf",
            )
            db.add_all([picture, elsewhere])
            await db.flush()
            spec = validate_spec(
                {
                    "title": _TITLE,
                    "slides": [
                        {"layout": "title", "title": "Q3 2026", "notes": _NOTE},
                        {
                            "layout": "content",
                            "title": "Pipeline",
                            "blocks": [{"type": "image", "file_id": str(picture.id)}],
                        },
                    ],
                }
            )
            deck = Presentation(
                user_id=owner.id,
                title=_TITLE,
                theme="dark",
                slide_count=2,
                spec=spec,
                shared_user_ids=[str(reader.id)],
                shared_user_permissions={str(reader.id): "read_only"},
            )
            db.add(deck)
            await db.commit()
            deck_id, picture_id, elsewhere_id = deck.id, picture.id, elsewhere.id
        link_url = f"/api/v1/presentations/{deck_id}/public-link"

        # The owner turns it on from the Share dialog.
        async with _client(scratch.sessions, owner) as client:
            before = (await client.get(link_url)).json()
            assert before == {
                "available": True,
                "enabled": False,
                "path": None,
                "url": None,
                "created_at": None,
                "created_by_name": None,
                "allow_pdf_download": False,
            }
            created = await client.post(link_url, json={})
            assert created.status_code == 200, created.text
            link = created.json()
            assert link["enabled"] is True
            assert link["created_by_name"] == "Samuel Hassine"
            assert link["path"].startswith("/public/presentations/")
            assert link["url"].endswith(link["path"])
            # Enabling again hands back the same link.
            again = (await client.post(link_url, json={})).json()
            assert again["path"] == link["path"]
            assert (await client.get(f"/api/v1/presentations/{deck_id}")).json()["has_public_link"] is True
        token = link["path"].rsplit("/", 1)[1]

        async with scratch.sessions() as db:
            stored = (await db.execute(select(PresentationPublicLink))).scalars().all()
            assert len(stored) == 1
            assert stored[0].presentation_id == deck_id
            assert token not in (stored[0].token_hash, stored[0].token_enc)

        # Anyone with the link: the slides and their picture, nothing else.
        async with _client(scratch.sessions, None) as anonymous:
            opened = await anonymous.get(f"/api/v1/public/presentations/{token}")
            assert opened.status_code == 200, opened.text
            assert opened.headers["cache-control"] == "no-store"
            assert opened.headers["referrer-policy"] == "no-referrer"
            body = opened.json()
            assert body["title"] == _TITLE
            assert [slide["title"] for slide in body["spec"]["slides"]] == ["Q3 2026", "Pipeline"]
            assert _NOTE not in opened.text
            for private in ("user_id", "owner_name", "shared_user_ids", "permission", "can_manage"):
                assert private not in body
            assert "samuel@filigran.io" not in opened.text and "Samuel Hassine" not in opened.text

            shown = await anonymous.get(f"/api/v1/public/presentations/{token}/assets/{picture_id}")
            assert shown.status_code == 200, shown.text
            assert shown.content == _PNG
            assert "no-store" in shown.headers["cache-control"]
            # A file the deck does not show, a malformed id, a wrong token: the same 404.
            for path in (
                f"/api/v1/public/presentations/{token}/assets/{elsewhere_id}",
                f"/api/v1/public/presentations/{token}/assets/not-a-uuid",
                f"/api/v1/public/presentations/{'A' * 43}",
                f"/api/v1/public/presentations/{'A' * 43}/assets/{picture_id}",
            ):
                assert (await anonymous.get(path)).status_code == 404, path
            # The authenticated deck routes still want an account.
            assert (await anonymous.get(f"/api/v1/presentations/{deck_id}")).status_code == 401

        # A read-only grantee is told the deck is public, and cannot touch the link.
        async with _client(scratch.sessions, reader) as client:
            assert (await client.get(f"/api/v1/presentations/{deck_id}")).json()["has_public_link"] is True
            assert (await client.get(link_url)).status_code == 403
            assert (await client.post(link_url, json={"regenerate": True})).status_code == 403
            assert (await client.delete(link_url)).status_code == 403
        refused = await _set_link(reader, presentation_id=str(deck_id), action="disable")
        assert "but not share it" in refused.content

        # Regenerating kills the old URL at once.
        async with _client(scratch.sessions, owner) as client:
            rotated = (await client.post(link_url, json={"regenerate": True})).json()
        new_token = rotated["path"].rsplit("/", 1)[1]
        assert new_token != token
        async with _client(scratch.sessions, None) as anonymous:
            assert (await anonymous.get(f"/api/v1/public/presentations/{token}")).status_code == 404
            assert (await anonymous.get(f"/api/v1/public/presentations/{new_token}")).status_code == 200

        # The admin switch off: every link is a 404 and none can be created.
        monkeypatch.setattr(settings, "presentation_public_links_enabled", False)
        async with _client(scratch.sessions, None) as anonymous:
            assert (await anonymous.get(f"/api/v1/public/presentations/{new_token}")).status_code == 404
        async with _client(scratch.sessions, owner) as client:
            state = (await client.get(link_url)).json()
            assert (state["available"], state["enabled"]) == (False, True)
            assert (await client.get(f"/api/v1/presentations/{deck_id}")).json()["has_public_link"] is False
            off = await client.post(link_url, json={"regenerate": True})
            assert off.status_code == 403
            assert "turned off on this platform" in off.json()["detail"]
        # ...and back on, the link that was never turned off answers again.
        monkeypatch.setattr(settings, "presentation_public_links_enabled", True)
        async with _client(scratch.sessions, None) as anonymous:
            assert (await anonymous.get(f"/api/v1/public/presentations/{new_token}")).status_code == 200

        # The owner's assistant turns it off; then on again, with a URL that works.
        disabled = await _set_link(owner, presentation_id=str(deck_id), action="disable")
        assert not disabled.is_error, disabled.content
        async with _client(scratch.sessions, None) as anonymous:
            assert (await anonymous.get(f"/api/v1/public/presentations/{new_token}")).status_code == 404
        enabled = await _set_link(owner, presentation_id=str(deck_id))
        assert not enabled.is_error, enabled.content
        agent_token = enabled.content.split("/public/presentations/", 1)[1].split()[0]
        async with _client(scratch.sessions, None) as anonymous:
            assert (await anonymous.get(f"/api/v1/public/presentations/{agent_token}")).status_code == 200

        async with scratch.sessions() as db:
            rows = (await db.execute(select(AuditLog).where(AuditLog.entity_id == deck_id))).scalars().all()
            changes = [(row.details["public_link"], row.details.get("via")) for row in rows]
            assert sorted(changes, key=str) == sorted(
                [("enabled", None), ("regenerated", None), ("disabled", "agent"), ("enabled", "agent")], key=str
            )
            assert all(row.user_id == owner.id for row in rows)

        # Deleting the deck takes its link with it.
        async with _client(scratch.sessions, owner) as client:
            assert (await client.delete(f"/api/v1/presentations/{deck_id}")).status_code == 204
        async with scratch.sessions() as db:
            assert (await db.execute(select(PresentationPublicLink))).scalars().all() == []
        async with _client(scratch.sessions, None) as anonymous:
            assert (await anonymous.get(f"/api/v1/public/presentations/{agent_token}")).status_code == 404


@pytest.fixture
def pdf_stubs(stubs, monkeypatch):
    """The PDF route without its Redis budgets, its headless browser counted and stubbed."""
    import app.core.rate_limit as rate_limit
    from app.services import presentation_export, presentation_public_links

    renders: list[tuple[str, str]] = []

    async def _no_budget(*_a, **_kw):
        return None

    async def _render(token: str, *, aspect_ratio: str) -> bytes:
        renders.append((token, aspect_ratio))
        return b"%PDF-1.7 public deck"

    monkeypatch.setattr(rate_limit, "enforce_rate_limit", _no_budget)
    monkeypatch.setattr(presentation_export, "render_public_deck_pdf", _render)
    presentation_public_links.clear_pdf_cache()
    yield renders
    presentation_public_links.clear_pdf_cache()


async def test_a_link_offers_its_pdf_only_when_its_managers_allow_it(pdf_stubs, monkeypatch) -> None:
    renders = pdf_stubs
    async with scratch_database(
        "presentation_public_pdf",
        tables=ALL_TABLES,
        extensions=("vector",),
        skip_without_extensions=True,
    ) as scratch:
        async with scratch.sessions() as db:
            owner = User(email="samuel@filigran.io", hashed_password="x", full_name="Samuel Hassine", role="user")
            reader = User(email="ada@filigran.io", hashed_password="x", full_name="Ada Lovelace", role="user")
            db.add_all([owner, reader])
            await db.flush()
            spec = validate_spec(
                {"title": _TITLE, "aspect_ratio": "4:3", "slides": [{"layout": "title", "title": "Q3", "notes": _NOTE}]}
            )
            deck = Presentation(
                user_id=owner.id,
                title=_TITLE,
                theme="dark",
                slide_count=1,
                spec=spec,
                shared_user_ids=[str(reader.id)],
                shared_user_permissions={str(reader.id): "read_only"},
            )
            db.add(deck)
            await db.commit()
            deck_id = deck.id
        link_url = f"/api/v1/presentations/{deck_id}/public-link"

        # No link, no option.
        async with _client(scratch.sessions, owner) as client:
            assert (await client.patch(link_url, json={"allow_pdf_download": True})).status_code == 404
            created = (await client.post(link_url, json={})).json()
        assert created["allow_pdf_download"] is False
        token = created["path"].rsplit("/", 1)[1]
        pdf_url = f"/api/v1/public/presentations/{token}/pdf"

        # Off on a new link: no button, and the route refuses.
        async with _client(scratch.sessions, None) as anonymous:
            assert (await anonymous.get(f"/api/v1/public/presentations/{token}")).json()["pdf_download"] is False
            assert (await anonymous.get(pdf_url)).status_code == 404
        assert renders == []

        # A read-only grantee cannot offer it; the owner can.
        async with _client(scratch.sessions, reader) as client:
            assert (await client.patch(link_url, json={"allow_pdf_download": True})).status_code == 403
        async with _client(scratch.sessions, owner) as client:
            allowed = await client.patch(link_url, json={"allow_pdf_download": True})
            assert allowed.status_code == 200, allowed.text
            assert allowed.json()["allow_pdf_download"] is True
            assert allowed.json()["path"] == created["path"]

        # Anyone with the link downloads it; an unchanged deck renders once.
        async with _client(scratch.sessions, None) as anonymous:
            assert (await anonymous.get(f"/api/v1/public/presentations/{token}")).json()["pdf_download"] is True
            for _ in range(3):
                downloaded = await anonymous.get(pdf_url)
                assert downloaded.status_code == 200, downloaded.text
                assert downloaded.content == b"%PDF-1.7 public deck"
                assert downloaded.headers["content-type"] == "application/pdf"
                assert downloaded.headers["cache-control"] == "no-store"
                assert 'filename="quarterly-review-q3-2026.pdf"' in downloaded.headers["content-disposition"]
        # Rendered from the public page with the link's own token, on the deck's paper.
        assert renders == [(token, "4:3")]

        # Replacing the link keeps the option; the old address offers nothing.
        async with _client(scratch.sessions, owner) as client:
            rotated = (await client.post(link_url, json={"regenerate": True})).json()
        assert rotated["allow_pdf_download"] is True
        new_token = rotated["path"].rsplit("/", 1)[1]
        async with _client(scratch.sessions, None) as anonymous:
            assert (await anonymous.get(pdf_url)).status_code == 404
            assert (await anonymous.get(f"/api/v1/public/presentations/{new_token}/pdf")).status_code == 200

        # The switch off: nothing is served, and the option cannot be turned on.
        monkeypatch.setattr(settings, "presentation_public_links_enabled", False)
        async with _client(scratch.sessions, None) as anonymous:
            assert (await anonymous.get(f"/api/v1/public/presentations/{new_token}/pdf")).status_code == 404
        async with _client(scratch.sessions, owner) as client:
            assert (await client.patch(link_url, json={"allow_pdf_download": True})).status_code == 403
        monkeypatch.setattr(settings, "presentation_public_links_enabled", True)

        # The assistant turns it off, then the link off and on: it starts without it.
        stopped = await _set_link(owner, presentation_id=str(deck_id), pdf_download=False)
        assert not stopped.is_error, stopped.content
        assert "Visitors can no longer download it" in stopped.content
        async with _client(scratch.sessions, None) as anonymous:
            assert (await anonymous.get(f"/api/v1/public/presentations/{new_token}/pdf")).status_code == 404
        await _set_link(owner, presentation_id=str(deck_id), pdf_download=True)
        assert (await _set_link(owner, presentation_id=str(deck_id), action="disable")).is_error is False
        again = await _set_link(owner, presentation_id=str(deck_id))
        assert "Visitors cannot download it" in again.content
        async with _client(scratch.sessions, owner) as client:
            assert (await client.get(link_url)).json()["allow_pdf_download"] is False

        async with scratch.sessions() as db:
            rows = (await db.execute(select(AuditLog).where(AuditLog.entity_id == deck_id))).scalars().all()
            pdf_changes = [
                (row.details["public_link"], row.details.get("via"))
                for row in rows
                if row.details["public_link"].startswith("pdf_download")
            ]
            assert sorted(pdf_changes, key=str) == sorted(
                [("pdf_download_enabled", None), ("pdf_download_disabled", "agent"), ("pdf_download_enabled", "agent")],
                key=str,
            )
