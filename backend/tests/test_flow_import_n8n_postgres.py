"""n8n import through the real route, against a real PostgreSQL.

`tests/test_flow_import_n8n.py` covers the conversion itself. Here the import
is driven through ``POST /api/v1/flows/import`` as an ordinary user, and the
row it writes is read back: the report the route answers, the review notes
kept on the stored graph, no credential or literal secret anywhere in it, and
a graph that saves again (``PUT``) both with its review notes and once they
are cleared. Same scratch-database recipe as
``test_name_length_guards_postgres.py``.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

import app.models  # noqa: F401 - registers every table on Base.metadata
from app.api import deps
from app.api.v1 import flow_runs as flow_runs_api
from app.api.v1 import flows as flows_api
from app.core.database import get_db
from app.models.flow import Flow
from app.models.user import User
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration

BASE = "n8n-nodes-base."


@asynccontextmanager
async def _temp_database() -> AsyncIterator[async_sessionmaker]:
    async with scratch_database(
        "n8nimport",
        tables=ALL_TABLES,
        extensions=("vector",),
        skip_without_extensions=True,
        refuse_other_databases=True,
    ) as db:
        yield db.sessions


async def _user(sessions: async_sessionmaker) -> User:
    async with sessions() as db:
        user = User(
            email=f"user-{uuid.uuid4().hex[:8]}@example.com",
            hashed_password="x",
            full_name="Importer",
            role="user",
        )
        db.add(user)
        await db.commit()
    assert not user.is_admin
    return user


@asynccontextmanager
async def _client(sessions: async_sessionmaker, user: User) -> AsyncIterator[httpx.AsyncClient]:
    app = FastAPI()
    app.include_router(flows_api.router, prefix="/api/v1/flows")
    app.include_router(flow_runs_api.router, prefix="/api/v1/flows")

    async def _get_db():
        async with sessions() as db:
            yield db
            await db.commit()

    app.dependency_overrides[deps.get_current_user] = lambda: user
    app.dependency_overrides[get_db] = _get_db
    for gate in (deps.require_agentic_ai, deps.require_ai_enabled, deps.require_agentic_ai_licensed):
        app.dependency_overrides[gate] = lambda: None
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


_WORKFLOW = {
    "name": "Enrich an alert",
    "nodes": [
        {
            "name": "Webhook",
            "type": BASE + "webhook",
            "typeVersion": 2,
            "position": [0, 0],
            "parameters": {"path": "alert", "httpMethod": "POST"},
        },
        {
            "name": "Lookup",
            "type": BASE + "httpRequest",
            "typeVersion": 4.2,
            "position": [220, 0],
            "parameters": {
                "url": "=https://intel.example/ioc/{{ $json.body.ioc }}",
                "sendHeaders": True,
                "headerParameters": {"parameters": [{"name": "X-Api-Key", "value": "literal-key-123"}]},
            },
            "credentials": {"httpHeaderAuth": {"id": "cred-9", "name": "Intel key"}},
        },
    ],
    "connections": {"Webhook": {"main": [[{"node": "Lookup", "type": "main", "index": 0}]]}},
    "pinData": {"Webhook": [{"json": {"body": {"ioc": "1.2.3.4"}}}]},
}


@pytest.mark.asyncio
async def test_an_n8n_import_writes_a_reviewable_draft_with_no_secret():
    async with _temp_database() as sessions:
        user = await _user(sessions)
        async with _client(sessions, user) as client:
            response = await client.post("/api/v1/flows/import", json={"document": _WORKFLOW})
            assert response.status_code == 201, response.text
            body = response.json()

        assert body["report"] == {
            "format": "n8n",
            "nodes_imported": 2,
            "nodes_unresolved": 0,
            "nodes_to_review": 1,
            "edges_imported": 1,
            "edges_dropped": 0,
            "warnings": [
                "1 nodes used n8n credentials (httpHeaderAuth). Credentials are "
                "never imported: configure the matching XTM One integration, "
                "secret variable or channel.",
                "1 literal secret values (API keys, passwords, authorization "
                "headers) were removed from the imported parameters. Store them "
                "as secret variables.",
                "Pinned test data was not imported.",
                "n8n runs each node once per item; XTM One passes one value from "
                "step to step. Where the workflow handled lists, iterate them "
                "with a Loop node.",
            ],
        }
        review_issues = [i for i in body["flow"]["issues"] if i["code"] == "node.import_review"]
        assert [(i["node_id"], i["severity"]) for i in review_issues] == [("lookup", "warning")]

        async with sessions() as db:
            flow = (await db.execute(select(Flow).where(Flow.id == uuid.UUID(body["flow"]["id"])))).scalar_one()
        assert flow.user_id == user.id
        assert flow.status == "draft"
        assert flow.name == "Enrich an alert"
        lookup = next(n for n in flow.graph["nodes"] if n["id"] == "lookup")
        assert lookup["config"] == {
            "method": "GET",
            "url": "https://intel.example/ioc/{{webhook.payload.ioc}}",
            "headers": {"X-Api-Key": ""},
        }
        assert lookup["source_payload"]["review"] == [
            "The literal value of the `X-Api-Key` header was not imported: store "
            "it as a secret variable and reference it.",
        ]
        stored = json.dumps(flow.graph)
        for leaked in ("literal-key-123", "cred-9", "Intel key", "1.2.3.4"):
            assert leaked not in stored


@pytest.mark.asyncio
async def test_an_imported_graph_saves_with_its_review_notes_and_without():
    async with _temp_database() as sessions:
        user = await _user(sessions)
        async with _client(sessions, user) as client:
            created = (await client.post("/api/v1/flows/import", json={"document": _WORKFLOW})).json()
            flow_id = created["flow"]["id"]
            graph = created["flow"]["graph"]

            kept = await client.put(f"/api/v1/flows/{flow_id}", json={"graph": graph})
            assert kept.status_code == 200, kept.text
            assert [i["node_id"] for i in kept.json()["issues"] if i["code"] == "node.import_review"] == ["lookup"]

            # "Mark as reviewed" removes the notes from the node's payload.
            for node in graph["nodes"]:
                node.get("source_payload", {}).pop("review", None)
            cleared = await client.put(f"/api/v1/flows/{flow_id}", json={"graph": graph})
            assert cleared.status_code == 200, cleared.text
            assert [i for i in cleared.json()["issues"] if i["code"] == "node.import_review"] == []


@pytest.mark.asyncio
async def test_a_document_that_is_no_known_format_is_a_400():
    async with _temp_database() as sessions:
        user = await _user(sessions)
        async with _client(sessions, user) as client:
            response = await client.post(
                "/api/v1/flows/import",
                json={"document": {"nodes": "not a list", "connections": {}}},
            )
        assert response.status_code == 400
        assert response.json()["detail"] == (
            "Unrecognised flow format — expected an XTM One, LangFlow or n8n flow export."
        )
        async with sessions() as db:
            assert (await db.execute(select(Flow))).scalars().all() == []


@pytest.mark.asyncio
async def test_a_flow_awaiting_review_cannot_be_activated_until_cleared():
    async with _temp_database() as sessions:
        user = await _user(sessions)
        async with _client(sessions, user) as client:
            created = (await client.post("/api/v1/flows/import", json={"document": _WORKFLOW})).json()
            flow_id = created["flow"]["id"]

            refused = await client.post(f"/api/v1/flows/{flow_id}/activate")
            assert refused.status_code == 422, refused.text
            detail = refused.json()["detail"]
            assert detail["message"] == "Review the imported nodes before activating."
            assert [(i["code"], i["node_id"]) for i in detail["issues"]] == [("node.import_review", "lookup")]

            graph = created["flow"]["graph"]
            for node in graph["nodes"]:
                node.get("source_payload", {}).pop("review", None)
            assert (await client.put(f"/api/v1/flows/{flow_id}", json={"graph": graph})).status_code == 200
            activated = await client.post(f"/api/v1/flows/{flow_id}/activate")
            assert activated.status_code == 200, activated.text
