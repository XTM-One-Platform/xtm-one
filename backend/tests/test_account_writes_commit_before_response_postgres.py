"""A created account is visible to the next request as its 201 goes out.

The unit tests pin the ORDER of a route's session calls against a stub. This one
runs the real thing: ``POST /api/v1/users`` through FastAPI, the real ``get_db``
and real PostgreSQL, with the new row looked up from a SECOND connection at the
exact moment the response body is handed to the server - the moment a client
holds its 201 and can send the next request, which runs on another connection
just like this lookup. A commit in the ``get_db`` teardown would come after
that moment, and the lookup would find nobody: the A2A e2e sign-in got a 401
that way.

Skips rather than fails when PostgreSQL is unreachable, matching the other
``integration``-marked tests here.
"""

from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

import app.models  # noqa: F401 - registers every table on Base.metadata
from app.api import deps
from app.api.v1 import users as users_module
from app.models.user import User
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration


async def _post_and_look_up_on_send(
    app: FastAPI, sessions: async_sessionmaker, path: str, payload: dict, email: str
) -> dict:
    """POST *payload*, looking *email* up from another session as the body goes out."""
    inbound = [{"type": "http.request", "body": json.dumps(payload).encode(), "more_body": False}]
    outcome: dict = {}

    async def receive():
        return inbound.pop(0) if inbound else {"type": "http.disconnect"}

    async def send(message):
        if message["type"] == "http.response.start":
            outcome["status"] = message["status"]
        elif message["type"] == "http.response.body" and not message.get("more_body"):
            async with sessions() as probe:
                outcome["found"] = await probe.scalar(
                    select(User.id).where(User.email == email)
                )

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "root_path": "",
        "query_string": b"",
        "headers": [(b"host", b"testserver"), (b"content-type", b"application/json")],
        "client": ("127.0.0.1", 50000),
        "server": ("testserver", 80),
    }
    await app(scope, receive, send)
    return outcome


async def test_a_created_user_is_committed_before_the_201_goes_out():
    # The real ``get_db`` opens its sessions on the scratch database: the
    # process-wide session factory is bound to it for the duration.
    async with scratch_database(
        "account_commit",
        tables=ALL_TABLES,
        extensions=("vector",),
        skip_without_extensions=True,
        refuse_other_databases=True,
    ) as scratch:
        async with scratch.sessions() as db:
            admin = User(email="admin@example.com", full_name="Admin", role="admin")
            db.add(admin)
            await db.commit()

        app = FastAPI()
        app.include_router(users_module.router, prefix="/api/v1/users")
        app.dependency_overrides[deps.require_admin] = lambda: admin

        email = "e2e-member@example.com"
        outcome = await _post_and_look_up_on_send(
            app,
            scratch.sessions,
            "/api/v1/users",
            {
                "email": email,
                "full_name": "E2E Member",
                "password": "Sup3r-Secret-Passw0rd!",
                "role": "user",
            },
            email,
        )

    assert outcome.get("status") == 201, outcome
    assert outcome.get("found") is not None, (
        "the 201 went out while the new user was still uncommitted: a sign-in "
        "on the client's next request would be refused as 'Invalid credentials'"
    )
