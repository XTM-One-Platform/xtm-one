"""Global search against PostgreSQL and the REST route (#11201).

One query over every entity type, never wider than each type's own list:

* an agent, a presentation, a knowledge document and a work item are each
  found by their name, counted on their own type;
* a colleague who cannot see them finds nothing and counts nothing;
* the page is ranked (exact name first) and paginated, of one type or merged;
* people are searched by an administrator only.

``integration``-marked and skips when PostgreSQL is unreachable, like its
neighbours; the temporary database is the shared ``tests/scratch_database.py``.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import async_sessionmaker

import app.models  # noqa: F401 - registers every table on Base.metadata
from app.api import deps
from app.api.v1 import search
from app.core.database import get_db
from app.models.agent import Agent
from app.models.knowledge_base import KnowledgeBase, KnowledgeDocument
from app.models.presentation import Presentation
from app.models.user import User
from app.models.work_item import WorkItem
from app.services.global_search import SEARCH_TYPES
from app.services.presentation_spec import validate_spec
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration

_BASE = "/api/v1/search"


@asynccontextmanager
async def _client(sessions: async_sessionmaker, user: User) -> AsyncIterator[httpx.AsyncClient]:
    api = FastAPI()
    api.include_router(search.router, prefix=_BASE)

    async def _get_db():
        async with sessions() as db:
            yield db
            await db.commit()

    api.dependency_overrides[deps.get_current_user] = lambda: user
    api.dependency_overrides[get_db] = _get_db
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://test") as client:
        yield client


def _user(email: str, name: str, role: str = "user") -> User:
    return User(email=email, hashed_password="x", full_name=name, role=role)


def _deck(title: str, **columns) -> Presentation:
    spec = validate_spec({"title": title, "slides": [{"layout": "title", "title": title}]})
    return Presentation(title=title, theme="dark", slide_count=1, spec=spec, **columns)


async def _json(client: httpx.AsyncClient, path: str, **params) -> dict:
    answer = await client.get(f"{_BASE}{path}", params=params)
    assert answer.status_code == 200, answer.text
    return answer.json()


async def test_search_finds_every_type_for_its_reader_only() -> None:
    async with scratch_database(
        "global_search", tables=ALL_TABLES, extensions=("vector",), skip_without_extensions=True
    ) as scratch:
        async with scratch.sessions() as db:
            owner = _user("samuel@filigran.io", "Samuel Hassine")
            colleague = _user("ada@filigran.io", "Ada Lovelace")
            admin = _user("root@filigran.io", "Root Admin", role="admin")
            db.add_all([owner, colleague, admin])
            await db.flush()
            kb = KnowledgeBase(id=uuid.uuid4(), name="Policies", user_id=owner.id)
            db.add(kb)
            await db.flush()
            db.add_all(
                [
                    Agent(name="Zephyr triage", slug=f"z-{uuid.uuid4().hex[:8]}", user_id=owner.id),
                    Agent(name="Zephyr", slug=f"z-{uuid.uuid4().hex[:8]}", user_id=owner.id),
                    Agent(
                        name="Helper",
                        description="Routes zephyr alerts",
                        slug=f"h-{uuid.uuid4().hex[:8]}",
                        user_id=owner.id,
                    ),
                    _deck("Quarterly zephyr review", user_id=owner.id),
                    KnowledgeDocument(knowledge_base_id=kb.id, filename="zephyr-runbook.md", source_type="text"),
                    WorkItem(user_id=owner.id, title="Migrate zephyr tenants"),
                    # The colleague's own private agent: never in the owner's results.
                    Agent(name="Zephyr private", slug=f"p-{uuid.uuid4().hex[:8]}", user_id=colleague.id),
                ]
            )
            await db.commit()

        async with _client(scratch.sessions, owner) as client:
            counts = await _json(client, "/counts", q="zephyr")
            assert set(counts["counts"]) == set(SEARCH_TYPES)
            assert counts["counts"]["agent"] == 3
            assert counts["counts"]["presentation"] == 1
            assert counts["counts"]["knowledge_document"] == 1
            assert counts["counts"]["work_item"] == 1
            assert counts["counts"]["user"] == 0
            assert counts["total"] == 6

            # Ranked: the exact name, then the prefix, then a description match.
            agents = await _json(client, "", q="Zephyr", type="agent")
            assert [hit["name"] for hit in agents["items"]] == ["Zephyr", "Zephyr triage", "Helper"]
            assert agents["items"][2]["excerpt"] == "Routes zephyr alerts"
            assert agents["total"] == 3

            # Paginated.
            second = await _json(client, "", q="zephyr", type="agent", limit=1, offset=1)
            assert [hit["name"] for hit in second["items"]] == ["Zephyr triage"]

            # Every word must match, in any case.
            both = await _json(client, "", q="ZEPHYR quarterly")
            assert [(hit["type"], hit["name"]) for hit in both["items"]] == [
                ("presentation", "Quarterly zephyr review")
            ]

            # The merged view holds each type, the document under its knowledge base.
            merged = await _json(client, "", q="zephyr", limit=50)
            types = {hit["type"] for hit in merged["items"]}
            assert types == {"agent", "presentation", "knowledge_document", "work_item"}
            document = next(hit for hit in merged["items"] if hit["type"] == "knowledge_document")
            assert document["parent_id"] == str(kb.id)

            # LIKE wildcards are literal.
            assert (await _json(client, "/counts", q="%"))["total"] == 0

        async with _client(scratch.sessions, colleague) as client:
            counts = await _json(client, "/counts", q="zephyr")
            assert counts["counts"]["agent"] == 1
            assert counts["total"] == 1
            only = await _json(client, "", q="zephyr")
            assert [hit["name"] for hit in only["items"]] == ["Zephyr private"]
            assert (await _json(client, "/counts", q="lovelace"))["counts"]["user"] == 0

        async with _client(scratch.sessions, admin) as client:
            people = await _json(client, "", q="lovelace", type="user")
            assert [hit["name"] for hit in people["items"]] == ["Ada Lovelace"]
            # Library scope: an admin does not find a colleague's private agent either.
            assert (await _json(client, "/counts", q="zephyr"))["counts"]["agent"] == 0

        async with _client(scratch.sessions, owner) as client:
            blank = await client.get(_BASE, params={"q": "   "})
            assert blank.status_code == 422
            unknown = await client.get(_BASE, params={"q": "x", "type": "nope"})
            assert unknown.status_code == 422
