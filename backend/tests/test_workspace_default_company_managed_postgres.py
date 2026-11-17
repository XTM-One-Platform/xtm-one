"""The default workspace is never company-managed, under races too.

The service refuses both directions with a reason: an admin cannot make a
default workspace company-managed (``ensure_can_be_company_managed``) and
nobody can make a company-managed workspace their default
(``make_default_workspace``). Each check used to read the row without a lock,
so an admin's ``PUT {is_company_managed: true}`` and a
``POST /{id}/default`` running at the same moment each passed on what they
read and both writes landed. Pinned here, on real PostgreSQL:

* the database refuses the state itself
  (``ck_workspaces_default_not_company_managed``);
* the two routes read the row ``FOR UPDATE``: the second request waits for the
  first and is refused with the service's reason, never a 500;
* the revision repairs a legacy default that is company-managed, and its owner
  gets a fresh default on their next listing.
"""

from __future__ import annotations

import asyncio
import importlib.util
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from fastapi import FastAPI
from sqlalchemy import select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

import app.models  # noqa: F401 - registers every table on Base.metadata
from app.api import deps
from app.api.v1 import workspaces as workspaces_api
from app.core.database import get_db
from app.models.user import User
from app.models.workspace import Workspace
from app.services.builtin_tools import build_agent_builder_tool
from app.services.workspaces import (
    COMPANY_MANAGED_DEFAULT_REFUSED,
    DEFAULT_COMPANY_MANAGED_REFUSED,
    create_workspace,
    ensure_default_workspace,
)
from app.tools.base import ToolCall
from tests.scratch_database import ALL_TABLES, LOCK_WAITERS_IN_THIS_DATABASE, scratch_database

pytestmark = pytest.mark.integration

_CHECK = "ck_workspaces_default_not_company_managed"

_REVISION = (
    Path(__file__).resolve().parents[1]
    / "alembic"
    / "versions"
    / "k5041wsdfcm01_workspace_default_never_company_managed.py"
)


def _load_revision():
    spec = importlib.util.spec_from_file_location("k5041wsdfcm01", _REVISION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


revision = _load_revision()


def _scratch(prefix: str):
    return scratch_database(
        prefix, tables=ALL_TABLES, extensions=("vector",), skip_without_extensions=True
    )


async def _user(session, name: str, *, admin: bool = False) -> User:
    user = User(
        id=uuid.uuid4(),
        email=f"{name}-{uuid.uuid4().hex[:6]}@example.com",
        full_name=name,
        hashed_password="x",
        role="admin" if admin else "user",
    )
    session.add(user)
    await session.flush()
    return user


@asynccontextmanager
async def _client(sessions: async_sessionmaker, user: User) -> AsyncIterator[httpx.AsyncClient]:
    """The workspaces router as *user*, one real session per request.

    One session per request, like production: two requests in flight at once
    must be two transactions, or there is no race to test.
    """
    api = FastAPI()
    api.include_router(workspaces_api.router, prefix="/api/v1/workspaces")
    api.dependency_overrides[deps.get_current_user] = lambda: user
    api.dependency_overrides[deps.require_agentic_ai_licensed] = lambda: None

    async def _get_db():
        async with sessions() as session:
            yield session
            await session.commit()

    api.dependency_overrides[get_db] = _get_db
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=api), base_url="http://test"
    ) as client:
        yield client


async def _until_a_lock_is_waited_on(engine: AsyncEngine) -> int:
    async with engine.connect() as conn:
        for _ in range(100):
            waiting = (await conn.execute(LOCK_WAITERS_IN_THIS_DATABASE)).scalar()
            await conn.rollback()
            if waiting:
                return waiting
            await asyncio.sleep(0.05)
    return 0


def _pause_the_first_audit(monkeypatch) -> tuple[asyncio.Event, asyncio.Event]:
    """Hold the first request at its audit entry, after its writes and before
    its commit; returns (reached, release)."""
    reached = asyncio.Event()
    release = asyncio.Event()
    real = workspaces_api.log_audit

    async def _audit(*args, **kwargs):
        if not reached.is_set():
            reached.set()
            await release.wait()
        return await real(*args, **kwargs)

    monkeypatch.setattr(workspaces_api, "log_audit", _audit)
    return reached, release


async def _flags(sessions: async_sessionmaker, workspace_id: uuid.UUID) -> tuple[bool, bool]:
    async with sessions() as session:
        row = (
            await session.execute(
                select(Workspace.is_default, Workspace.is_company_managed).where(
                    Workspace.id == workspace_id
                )
            )
        ).one()
    return row[0], row[1]


@pytest.mark.asyncio
async def test_the_database_refuses_a_default_that_is_company_managed() -> None:
    async with _scratch("wsdefcm") as scratch:
        async with scratch.sessions() as session:
            owner = await _user(session, "owner")
            session.add(
                Workspace(
                    user_id=owner.id,
                    name="Both",
                    slug="both",
                    is_default=True,
                    is_company_managed=True,
                )
            )
            with pytest.raises(IntegrityError) as raised:
                await session.flush()
            assert _CHECK in str(raised.value)
            await session.rollback()

        async with scratch.sessions() as session:
            owner = await _user(session, "owner")
            default = await ensure_default_workspace(session, owner.id)
            await session.commit()
            with pytest.raises(IntegrityError) as raised:
                await session.execute(
                    update(Workspace)
                    .where(Workspace.id == default.id)
                    .values(is_company_managed=True)
                )
            assert _CHECK in str(raised.value)


@pytest.mark.asyncio
async def test_making_it_company_managed_while_its_owner_makes_it_the_default(
    monkeypatch,
) -> None:
    """The owner's ``POST /default`` goes first; the admin's ``PUT`` waits for
    it, then reads a default and is refused with the reason."""
    async with _scratch("wsdefcm") as scratch:
        async with scratch.sessions() as session:
            admin = await _user(session, "admin", admin=True)
            alice = await _user(session, "alice")
            await ensure_default_workspace(session, alice.id)
            workspace = await create_workspace(session, user_id=alice.id, name="Team")
            await session.commit()
        reached, release = _pause_the_first_audit(monkeypatch)
        url = f"/api/v1/workspaces/{workspace.id}"

        async with _client(scratch.sessions, alice) as owner, _client(
            scratch.sessions, admin
        ) as admins:
            made_default = asyncio.create_task(owner.post(f"{url}/default"))
            await asyncio.wait_for(reached.wait(), timeout=10)
            published = asyncio.create_task(admins.put(url, json={"is_company_managed": True}))
            try:
                waiting = await _until_a_lock_is_waited_on(scratch.engine)
            finally:
                release.set()
            made_default, published = await asyncio.wait_for(
                asyncio.gather(made_default, published), timeout=30
            )

        assert waiting == 1, "the admin's update never waited on the owner's request"
        assert made_default.status_code == 200, made_default.text
        assert published.status_code == 400, published.text
        assert published.json()["detail"] == DEFAULT_COMPANY_MANAGED_REFUSED
        assert await _flags(scratch.sessions, workspace.id) == (True, False)


@pytest.mark.parametrize(
    ("owner_is_admin", "status", "detail"),
    [
        # An admin owner may manage a company-managed workspace: the default
        # rule is what refuses it.
        (True, 400, COMPANY_MANAGED_DEFAULT_REFUSED),
        # A non-admin owner may no longer manage it at all, read afresh.
        (False, 403, "Only admins can edit company-managed workspaces"),
    ],
)
@pytest.mark.asyncio
async def test_making_it_the_default_while_an_admin_makes_it_company_managed(
    monkeypatch, owner_is_admin: bool, status: int, detail: str
) -> None:
    """The admin's ``PUT`` goes first; the owner's ``POST /default`` waits for
    it, then reads a company-managed workspace and is refused."""
    async with _scratch("wsdefcm") as scratch:
        async with scratch.sessions() as session:
            admin = await _user(session, "admin", admin=True)
            owner_user = admin if owner_is_admin else await _user(session, "alice")
            previous = await ensure_default_workspace(session, owner_user.id)
            workspace = await create_workspace(session, user_id=owner_user.id, name="Team")
            await session.commit()
        reached, release = _pause_the_first_audit(monkeypatch)
        url = f"/api/v1/workspaces/{workspace.id}"

        async with _client(scratch.sessions, admin) as admins, _client(
            scratch.sessions, owner_user
        ) as owner:
            published = asyncio.create_task(admins.put(url, json={"is_company_managed": True}))
            await asyncio.wait_for(reached.wait(), timeout=10)
            made_default = asyncio.create_task(owner.post(f"{url}/default"))
            try:
                waiting = await _until_a_lock_is_waited_on(scratch.engine)
            finally:
                release.set()
            published, made_default = await asyncio.wait_for(
                asyncio.gather(published, made_default), timeout=30
            )

        assert waiting == 1, "the owner's request never waited on the admin's update"
        assert published.status_code == 200, published.text
        assert made_default.status_code == status, made_default.text
        assert made_default.json()["detail"] == detail
        assert await _flags(scratch.sessions, workspace.id) == (False, True)
        # The refused request changed nothing: the owner keeps their default.
        assert await _flags(scratch.sessions, previous.id) == (True, False)


@pytest.mark.asyncio
async def test_the_agent_builder_tool_waits_for_the_admin_too(monkeypatch) -> None:
    """``set_default_workspace`` from chat takes the same lock as the route:
    started while an admin makes the workspace company-managed, it waits and
    answers the service's refusal."""
    async with _scratch("wsdefcm") as scratch:
        async with scratch.sessions() as session:
            admin = await _user(session, "admin", admin=True)
            previous = await ensure_default_workspace(session, admin.id)
            workspace = await create_workspace(session, user_id=admin.id, name="Team")
            await session.commit()
        reached, release = _pause_the_first_audit(monkeypatch)
        _, executor = build_agent_builder_tool(None, str(admin.id), conversation_id=None)

        async with _client(scratch.sessions, admin) as admins:
            published = asyncio.create_task(
                admins.put(f"/api/v1/workspaces/{workspace.id}", json={"is_company_managed": True})
            )
            await asyncio.wait_for(reached.wait(), timeout=10)
            answered = asyncio.create_task(
                executor.execute_tool(
                    ToolCall(
                        id=str(uuid.uuid4()),
                        name="set_default_workspace",
                        arguments={"workspace_id": str(workspace.id)},
                    )
                )
            )
            try:
                waiting = await _until_a_lock_is_waited_on(scratch.engine)
            finally:
                release.set()
            published, answered = await asyncio.wait_for(
                asyncio.gather(published, answered), timeout=30
            )

        assert waiting == 1, "the tool never waited on the admin's update"
        assert published.status_code == 200, published.text
        assert answered.content == COMPANY_MANAGED_DEFAULT_REFUSED
        assert await _flags(scratch.sessions, workspace.id) == (False, True)
        assert await _flags(scratch.sessions, previous.id) == (True, False)


async def _run_revision(engine: AsyncEngine, step) -> None:
    """Run *step* as ``env.py`` runs a revision: the migration context owns the
    transaction."""

    def _call(sync_conn) -> None:
        context = MigrationContext.configure(sync_conn)
        with Operations.context(context), context.begin_transaction():
            step()

    async with engine.connect() as conn:
        await conn.run_sync(_call)


async def _check_state(engine: AsyncEngine) -> bool | None:
    async with engine.connect() as conn:
        return (
            await conn.execute(
                text(
                    "SELECT convalidated FROM pg_constraint "
                    "WHERE conname = :name AND conrelid = 'workspaces'::regclass"
                ),
                {"name": _CHECK},
            )
        ).scalar()


@pytest.mark.asyncio
async def test_the_revision_repairs_a_legacy_company_managed_default() -> None:
    async with _scratch("wsdefcmrev") as scratch:
        # The database as it stood before the revision: no check, and a
        # default an earlier version let become company-managed.
        async with scratch.engine.begin() as conn:
            await conn.execute(text(f"ALTER TABLE workspaces DROP CONSTRAINT {_CHECK}"))
        async with scratch.sessions() as session:
            alice = await _user(session, "alice")
            bob = await _user(session, "bob")
            legacy = Workspace(
                user_id=alice.id,
                name="General",
                slug="general",
                is_default=True,
                is_company_managed=True,
            )
            session.add(legacy)
            untouched = await ensure_default_workspace(session, bob.id)
            await session.commit()
        assert await _check_state(scratch.engine) is None

        await _run_revision(scratch.engine, revision.upgrade)

        assert await _check_state(scratch.engine) is True
        # It stops being the default; its company-managed audience keeps it.
        assert await _flags(scratch.sessions, legacy.id) == (False, True)
        assert await _flags(scratch.sessions, untouched.id) == (True, False)
        # Its owner lands somewhere again on their next listing, under a slug
        # that does not collide with the old default's.
        async with scratch.sessions() as session:
            fresh = await ensure_default_workspace(session, alice.id)
            await session.commit()
        assert fresh.id != legacy.id
        assert fresh.slug == "general-2"
        assert await _flags(scratch.sessions, fresh.id) == (True, False)

        # Idempotent, and reversible.
        await _run_revision(scratch.engine, revision.upgrade)
        assert await _check_state(scratch.engine) is True
        await _run_revision(scratch.engine, revision.downgrade)
        assert await _check_state(scratch.engine) is None
        await _run_revision(scratch.engine, revision.downgrade)
        assert await _check_state(scratch.engine) is None
        await _run_revision(scratch.engine, revision.upgrade)
        assert await _check_state(scratch.engine) is True
