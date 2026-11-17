"""An agent bundle's remote MCP servers are held to the MCP server URL policy.

Both agent import routes (``POST /agents/import`` and ``/agents/import-file``)
recreate the bundle's MCP servers, which this platform then connects to: a URL
the policy refuses is a 422 and nothing of the bundle is written. Against real
PostgreSQL (``tests/scratch_database.py``).
"""

from __future__ import annotations

import io
import json
import uuid

import pytest
from fastapi import HTTPException, Request, UploadFile
from sqlalchemy import func, select

import app.models  # noqa: F401 - registers every table on Base.metadata
from app.api.v1 import agents as agents_api
from app.api.v1.schemas.agent import ImportAgentRequest
from app.core import security_options
from app.models.agent import Agent
from app.models.mcp_server import MCPServer
from app.models.user import User
from app.tools.mcp_destinations import REFUSED_DETAIL
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration


def _bundle(server: dict) -> dict:
    return {
        "version": 3,
        "export_type": "agent",
        "agent": {"name": "Imported agent", "persona": "You help."},
        "mcp_servers": [server],
    }


def _request() -> Request:
    return Request({"type": "http", "method": "POST", "path": "/x", "headers": []})


async def _import_json(db, user: User, bundle: dict) -> None:
    await agents_api.import_agent(ImportAgentRequest(**bundle), _request(), user, db)


async def _import_file(db, user: User, bundle: dict) -> None:
    upload = UploadFile(file=io.BytesIO(json.dumps(bundle).encode()), filename="bundle.json")
    await agents_api.import_agent_file(_request(), upload, user, db)


_ROUTES = {"import": _import_json, "import-file": _import_file}


@pytest.mark.parametrize("route", sorted(_ROUTES))
@pytest.mark.parametrize(
    ("policy", "server", "detail"),
    [
        ("permissive", {"name": "M", "transport": "sse", "url": "http://169.254.169.254/latest/"}, REFUSED_DETAIL),
        ("private_networks", {"name": "M", "transport": "streamable_http", "url": "http://[::1]:8080/mcp"}, REFUSED_DETAIL),
        ("public_only", {"name": "M", "transport": "sse", "url": "http://10.0.0.5/sse"}, REFUSED_DETAIL),
        ("permissive", {"name": "M", "transport": "sse", "url": "file:///etc/passwd"}, "The MCP server URL must be an http or https URL."),
    ],
    ids=["metadata", "loopback-v6", "private-public-only", "file-scheme"],
)
async def test_a_refused_mcp_server_url_refuses_the_whole_import(route, policy, server, detail):
    async with scratch_database(
        "agent_import_mcp_url", tables=ALL_TABLES, extensions=("vector",), skip_without_extensions=True
    ) as scratch:
        security_options.publish_stored({"mcp_server_url_policy": policy, "mcp_server_trusted_origins": ()})
        async with scratch.sessions() as db:
            user = User(id=uuid.uuid4(), email="importer@example.com", full_name="Importer", hashed_password="x")
            db.add(user)
            await db.commit()
        async with scratch.sessions() as db:
            with pytest.raises(HTTPException) as refused:
                await _ROUTES[route](db, await db.get(User, user.id), _bundle(server))
            await db.rollback()
        assert (refused.value.status_code, refused.value.detail) == (422, detail)
        async with scratch.sessions() as db:
            for model in (Agent, MCPServer):
                assert (await db.execute(select(func.count()).select_from(model))).scalar_one() == 0


@pytest.mark.parametrize("route", sorted(_ROUTES))
async def test_an_admitted_or_stdio_mcp_server_imports(route):
    async with scratch_database(
        "agent_import_mcp_url_ok", tables=ALL_TABLES, extensions=("vector",), skip_without_extensions=True
    ) as scratch:
        security_options.publish_stored({"mcp_server_url_policy": "private_networks", "mcp_server_trusted_origins": ()})
        async with scratch.sessions() as db:
            user = User(id=uuid.uuid4(), email="importer@example.com", full_name="Importer", hashed_password="x")
            db.add(user)
            await db.commit()
        bundle = _bundle({"name": "Private", "transport": "sse", "url": "http://10.0.0.5:8080/sse"})
        bundle["mcp_servers"].append({"name": "Local", "transport": "stdio", "command": "npx", "url": "http://127.0.0.1/"})
        async with scratch.sessions() as db:
            await _ROUTES[route](db, await db.get(User, user.id), bundle)
        async with scratch.sessions() as db:
            urls = sorted((await db.execute(select(MCPServer.name, MCPServer.url))).all())
        assert urls == [("Local", "http://127.0.0.1/"), ("Private", "http://10.0.0.5:8080/sse")]
