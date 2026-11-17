"""The presentation library a page at a time, against PostgreSQL and the REST route.

Agents keep adding decks; every one must stay reachable, and the search and
the filters must cover the whole library, not the page the browser holds.
Every actor is an ordinary user (rule 19):

* the decks come a page at a time with their total, the last changed first,
  past the 100 the list used to stop at;
* narrowed to mine, shared with me or the company's, to decks or pages, and
  searched by every word of the title or the owner's name;
* a stranger's private deck is never listed nor counted.

``integration``-marked and skips when PostgreSQL is unreachable, like its
neighbours; the temporary database is the shared ``tests/scratch_database.py``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import async_sessionmaker

import app.models  # noqa: F401 - registers every table on Base.metadata
from app.api import deps
from app.api.v1 import presentations
from app.core.database import get_db
from app.models.presentation import Presentation
from app.models.user import User
from app.services.presentation_spec import validate_spec
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration

_BASE = "/api/v1/presentations"
#: More than the 100 decks the list answered before it was paginated.
_OWN = 110


@asynccontextmanager
async def _client(sessions: async_sessionmaker, user: User) -> AsyncIterator[httpx.AsyncClient]:
    api = FastAPI()
    api.include_router(presentations.router, prefix=_BASE)

    async def _get_db():
        async with sessions() as db:
            yield db
            await db.commit()

    api.dependency_overrides[deps.get_current_user] = lambda: user
    api.dependency_overrides[get_db] = _get_db
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://test") as client:
        yield client


def _user(email: str, name: str) -> User:
    return User(email=email, hashed_password="x", full_name=name, role="user")


def _deck(title: str, changed: datetime, *, aspect_ratio: str = "16:9", **columns) -> Presentation:
    spec = validate_spec(
        {"title": title, "aspect_ratio": aspect_ratio, "slides": [{"layout": "title", "title": title}]}
    )
    return Presentation(title=title, theme="dark", slide_count=1, spec=spec, updated_at=changed, **columns)


async def _get(client: httpx.AsyncClient, **params) -> dict:
    answer = await client.get(_BASE, params=params)
    assert answer.status_code == 200, answer.text
    return answer.json()


async def test_the_library_comes_a_page_at_a_time_searched_and_filtered_on_the_server() -> None:
    async with scratch_database(
        "presentation_library", tables=ALL_TABLES, extensions=("vector",), skip_without_extensions=True
    ) as scratch:
        start = datetime(2026, 10, 1, tzinfo=UTC)
        async with scratch.sessions() as db:
            owner = _user("samuel@filigran.io", "Samuel Hassine")
            colleague = _user("ada@filigran.io", "Ada Lovelace")
            stranger = _user("eve@filigran.io", "Eve Stranger")
            db.add_all([owner, colleague, stranger])
            await db.flush()
            # 110 of the owner's own, a minute apart, two of them portrait pages and one a board update.
            own = [_deck(f"Deck {n:03d}", start + timedelta(minutes=n), user_id=owner.id) for n in range(_OWN)]
            for n in (5, 6):
                own[n] = _deck(f"Brief {n}", start + timedelta(minutes=n), aspect_ratio="A4", user_id=owner.id)
            own[40].title = "Board update, October"
            shared = _deck(
                "Customer roadmap",
                start + timedelta(minutes=500),
                user_id=colleague.id,
                shared_user_ids=[str(owner.id)],
                shared_user_permissions={str(owner.id): "read_only"},
            )
            published = _deck(
                "Company all hands", start + timedelta(minutes=600), user_id=None, is_company_managed=True
            )
            private = _deck("Eve's private plan", start + timedelta(minutes=700), user_id=stranger.id)
            db.add_all([*own, shared, published, private])
            await db.commit()

        async with _client(scratch.sessions, owner) as client:
            # The last changed first; the stranger's deck neither listed nor counted.
            first = await _get(client)
            assert first["total"] == _OWN + 2
            assert len(first["items"]) == presentations.LIST_PAGE_SIZE
            assert [deck["title"] for deck in first["items"][:3]] == [
                "Company all hands",
                "Customer roadmap",
                "Deck 109",
            ]
            # Every deck is reachable: the last page holds the oldest, past the 100 the list stopped at.
            last = await _get(client, limit=24, offset=96)
            assert len(last["items"]) == _OWN + 2 - 96
            assert last["items"][-1]["title"] == "Deck 000"
            seen = [
                deck["id"] for offset in range(0, _OWN + 2, 24) for deck in (await _get(client, offset=offset))["items"]
            ]
            assert len(seen) == len(set(seen)) == _OWN + 2

            # The pills, on the whole library.
            assert (await _get(client, visibility="mine"))["total"] == _OWN
            mine_shared = await _get(client, visibility="shared")
            assert [deck["title"] for deck in mine_shared["items"]] == ["Customer roadmap"]
            company = await _get(client, visibility="company")
            assert [deck["title"] for deck in company["items"]] == ["Company all hands"]

            # Decks or pages, combined with a pill.
            pages = await _get(client, kind="page")
            assert pages["total"] == 2
            assert {deck["kind"] for deck in pages["items"]} == {"page"}
            assert (await _get(client, kind="deck", visibility="mine"))["total"] == _OWN - 2

            # Every word, any case, in the title or the owner's name.
            board = await _get(client, search="october BOARD")
            assert [deck["title"] for deck in board["items"]] == ["Board update, October"]
            by_owner = await _get(client, search="lovelace")
            assert [deck["title"] for deck in by_owner["items"]] == ["Customer roadmap"]
            assert (await _get(client, search="eve"))["total"] == 0
            assert (await _get(client, search="board march"))["total"] == 0
