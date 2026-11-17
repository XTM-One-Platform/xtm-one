"""The `workspace` coordination scope.

What this locks down is not "the scope works" but **how it refuses**.

A scope whose entire purpose is isolation must never degrade into the shared
namespace when it cannot be resolved, because that is not a lesser service —
it is a leak. So every failure path is asserted explicitly: no workspace
named, a workspace that does not exist, one that is not visible to the caller,
one that is archived, and a scope value outside the vocabulary.

The last of those was a live fail-OPEN before this change. `_wired_coordination`
matched `run` and `flow` with no `else`, so an unrecognised scope left the
namespace unset — which means raw, un-namespaced keys, i.e. the global
blackboard. The graph validator rejects an out-of-range static select, so it
took hand-written JSON or an API caller to reach; it stops being acceptable
once one of the scopes exists specifically to isolate.

The delete path is asserted for the same reason: `work_items` is RESTRICT and
`worksets` must go through `purge_workset`, and the difference between those
two RESTRICTs is the whole design.
"""

from __future__ import annotations

import os
import urllib.parse
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError, IntegrityError, OperationalError, ProgrammingError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.models  # noqa: F401 — registers every mapper
from app.core.database import Base
from app.core.scopes import (
    COORDINATION_SCOPE_WORKSPACE,
    COORDINATION_SCOPES,
)
from app.models.knowledge_base import KnowledgeBase
from app.models.user import User
from app.models.work_item import WorkItem
from app.models.workset import Workset
from app.models.workspace import Workspace
from app.services.workspaces import (
    WorkspaceScopeError,
    count_workspace_work_items,
    create_workspace,
    purge_workspace_worksets,
    resolve_workspace_scope,
    workspace_namespace,
)

# The integration job is the one CI runs with PostgreSQL: unmarked, every test
# below skips in the unit shards and none of them ever runs.
pytestmark = pytest.mark.integration


def _base_url() -> str:
    return os.environ.get(
        "DATABASE_URL",
        "postgresql+asyncpg://copilot:copilot@localhost:5432/copilot",
    )


@asynccontextmanager
async def _temp_database() -> AsyncIterator[async_sessionmaker]:
    parsed = urllib.parse.urlparse(_base_url())
    admin_url = parsed._replace(path="/postgres").geturl()
    db_name = f"wstscope_{uuid.uuid4().hex[:10]}"
    test_url = parsed._replace(path=f"/{db_name}").geturl()

    admin_engine = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        async with admin_engine.connect() as conn:
            await conn.execute(text(f'CREATE DATABASE "{db_name}"'))
    except (DBAPIError, OperationalError, ProgrammingError) as exc:
        orig = getattr(exc, "orig", None)
        pgcode = getattr(orig, "pgcode", None) or getattr(orig, "sqlstate", None)
        if pgcode == "42501":
            pytest.skip("DATABASE_URL user requires CREATEDB privilege")
        raise
    except Exception as exc:  # pragma: no cover — no PG in this environment
        pytest.skip(f"Cannot connect to PostgreSQL: {exc}")
    finally:
        await admin_engine.dispose()

    engine = create_async_engine(test_url)
    try:
        async with engine.begin() as conn:
            await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            await conn.run_sync(Base.metadata.create_all)
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()
        cleanup = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")
        try:
            async with cleanup.connect() as conn:
                await conn.execute(
                    text(
                        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                        "WHERE datname = :db AND pid <> pg_backend_pid()"
                    ),
                    {"db": db_name},
                )
                await conn.execute(text(f'DROP DATABASE IF EXISTS "{db_name}"'))
        finally:
            await cleanup.dispose()


async def _make_user(session, *, email: str, is_admin: bool = False) -> User:
    user = User(
        id=uuid.uuid4(),
        email=email,
        full_name=email.split("@")[0],
        hashed_password="x",
        role="admin" if is_admin else "user",
    )
    session.add(user)
    await session.flush()
    return user


# ── The vocabulary ───────────────────────────────────────────────────────────


def test_workspace_is_in_the_coordination_vocabulary_only() -> None:
    """And the separate scope vocabularies of `app.core.scopes` keep it off the
    event nodes.

    Asserted here too rather than relying on their own suite alone: this is the
    change they were built for, so its own tests should say what it must not
    do.
    """
    from app.core.scopes import EVENT_SCOPES

    assert COORDINATION_SCOPE_WORKSPACE in COORDINATION_SCOPES
    assert COORDINATION_SCOPE_WORKSPACE not in EVENT_SCOPES


def test_the_namespace_is_built_on_the_id() -> None:
    """Never the slug: a rename must not orphan existing state."""
    wid = uuid.uuid4()
    assert workspace_namespace(wid) == f"workspace:{wid}"


# ── Resolution fails closed, five ways ───────────────────────────────────────


@pytest.mark.asyncio
async def test_resolution_refuses_when_no_workspace_is_named() -> None:
    async with _temp_database() as sessions, sessions() as session:
        user = await _make_user(session, email="a@example.com")
        await session.commit()
        with pytest.raises(WorkspaceScopeError) as exc:
            await resolve_workspace_scope(session, user, None)
        assert exc.value.reason == "missing"


@pytest.mark.asyncio
async def test_resolution_refuses_an_unknown_workspace() -> None:
    async with _temp_database() as sessions, sessions() as session:
        user = await _make_user(session, email="a@example.com")
        await session.commit()
        with pytest.raises(WorkspaceScopeError) as exc:
            await resolve_workspace_scope(session, user, str(uuid.uuid4()))
        assert exc.value.reason == "not_found"


@pytest.mark.asyncio
async def test_resolution_refuses_someone_elses_workspace_as_not_found() -> None:
    """One reason for "deleted" and "not visible", deliberately.

    Distinguishing them would tell a caller that a workspace they cannot see
    EXISTS — the existence oracle the visibility filter is there to prevent.
    """
    async with _temp_database() as sessions, sessions() as session:
        alice = await _make_user(session, email="alice@example.com")
        bob = await _make_user(session, email="bob@example.com")
        bobs = await create_workspace(session, user_id=bob.id, name="Bob's")
        await session.commit()

        with pytest.raises(WorkspaceScopeError) as exc:
            await resolve_workspace_scope(session, alice, str(bobs.id))
        assert exc.value.reason == "not_found"


@pytest.mark.asyncio
async def test_resolution_refuses_an_archived_workspace() -> None:
    async with _temp_database() as sessions, sessions() as session:
        user = await _make_user(session, email="a@example.com")
        ws = await create_workspace(session, user_id=user.id, name="Retired")
        ws.is_archived = True
        await session.commit()

        with pytest.raises(WorkspaceScopeError) as exc:
            await resolve_workspace_scope(session, user, str(ws.id))
        assert exc.value.reason == "archived"


@pytest.mark.asyncio
async def test_resolution_succeeds_on_a_visible_live_workspace() -> None:
    async with _temp_database() as sessions, sessions() as session:
        user = await _make_user(session, email="a@example.com")
        ws = await create_workspace(session, user_id=user.id, name="Live")
        await session.commit()

        resolved = await resolve_workspace_scope(session, user, str(ws.id))
        assert resolved.id == ws.id


@pytest.mark.asyncio
async def test_an_admin_resolves_a_workspace_they_do_not_own() -> None:
    """Admins bypass the predicate — asserted so the bypass is intentional."""
    async with _temp_database() as sessions, sessions() as session:
        admin = await _make_user(session, email="admin@example.com", is_admin=True)
        bob = await _make_user(session, email="bob@example.com")
        bobs = await create_workspace(session, user_id=bob.id, name="Bob's")
        await session.commit()

        assert (
            await resolve_workspace_scope(session, admin, str(bobs.id))
        ).id == bobs.id


# ── The CHECK keeps scope and column in step ─────────────────────────────────


@pytest.mark.asyncio
async def test_a_workspace_scoped_workset_without_a_workspace_is_refused() -> None:
    """The row would be accepted at WRITE time and fail at READ time.

    `_scope_key` would raise, or namespace against nothing, on data already
    stored — which is why this is a database constraint rather than a
    write-path check.
    """
    async with _temp_database() as sessions, sessions() as session:
        user = await _make_user(session, email="a@example.com")
        await session.flush()
        session.add(
            Workset(
                id=uuid.uuid4(),
                user_id=user.id,
                key="k",
                scope=COORDINATION_SCOPE_WORKSPACE,
                workspace_id=None,
            )
        )
        with pytest.raises(IntegrityError):
            await session.flush()


@pytest.mark.asyncio
async def test_a_run_scoped_workset_carrying_a_workspace_is_refused() -> None:
    """The other direction of the same CHECK."""
    async with _temp_database() as sessions, sessions() as session:
        user = await _make_user(session, email="a@example.com")
        ws = await create_workspace(session, user_id=user.id, name="W")
        await session.flush()
        session.add(
            Workset(
                id=uuid.uuid4(),
                user_id=user.id,
                key="k",
                scope="run",
                scope_ref="r",
                workspace_id=ws.id,
            )
        )
        with pytest.raises(IntegrityError):
            await session.flush()


# ── resolve_workset honours the pairing ──────────────────────────────────────


@pytest.mark.asyncio
async def test_resolve_workset_namespaces_on_the_workspace_id() -> None:
    """The column AND the namespace, not one instead of the other.

    The FK gives integrity; the namespace means a query that forgets to filter
    on the column MISSES rather than reading another workspace's corpus.
    """
    from app.services.workset import resolve_workset

    async with _temp_database() as sessions, sessions() as session:
        user = await _make_user(session, email="a@example.com")
        one = await create_workspace(session, user_id=user.id, name="One")
        two = await create_workspace(session, user_id=user.id, name="Two")
        await session.commit()

        first = await resolve_workset(
            session,
            user,
            key="reports",
            scope=COORDINATION_SCOPE_WORKSPACE,
            workspace_id=one.id,
        )
        second = await resolve_workset(
            session,
            user,
            key="reports",
            scope=COORDINATION_SCOPE_WORKSPACE,
            workspace_id=two.id,
        )
        await session.commit()

        # Same caller, same key, two workspaces — two corpora, not one.
        assert first.id != second.id
        assert first.key == f"workspace:{one.id}:reports"
        assert second.key == f"workspace:{two.id}:reports"
        assert first.workspace_id == one.id
        assert second.workspace_id == two.id
        # `scope_ref` stays NULL: the workspace referent lives in the column.
        assert first.scope_ref is None


@pytest.mark.asyncio
async def test_resolve_workset_refuses_the_scope_without_a_workspace() -> None:
    from app.services.workset import WorksetSourceError, resolve_workset

    async with _temp_database() as sessions, sessions() as session:
        user = await _make_user(session, email="a@example.com")
        await session.commit()
        with pytest.raises(WorksetSourceError):
            await resolve_workset(
                session, user, key="k", scope=COORDINATION_SCOPE_WORKSPACE
            )


@pytest.mark.asyncio
async def test_resolve_workset_refuses_a_workspace_on_another_scope() -> None:
    from app.services.workset import WorksetSourceError, resolve_workset

    async with _temp_database() as sessions, sessions() as session:
        user = await _make_user(session, email="a@example.com")
        ws = await create_workspace(session, user_id=user.id, name="W")
        await session.commit()
        with pytest.raises(WorksetSourceError):
            await resolve_workset(
                session, user, key="k", scope="global", workspace_id=ws.id
            )


# ── Deleting a workspace: two RESTRICTs, two different reasons ───────────────


@pytest.mark.asyncio
async def test_work_items_block_the_delete_and_are_counted() -> None:
    async with _temp_database() as sessions, sessions() as session:
        user = await _make_user(session, email="a@example.com")
        ws = await create_workspace(session, user_id=user.id, name="Busy")
        await session.flush()
        session.add_all(
            [
                WorkItem(
                    id=uuid.uuid4(),
                    user_id=user.id,
                    title=f"item {n}",
                    workspace_id=ws.id,
                )
                for n in range(3)
            ]
        )
        await session.commit()

        # ALL work items block the delete, not only open ones: the FK is
        # RESTRICT, so a closed item holds the workspace just as firmly.
        assert await count_workspace_work_items(session, ws) == 3

        # And the database itself refuses, not only the endpoint's pre-check —
        # so a caller that bypassed the check still cannot delete tracked work.
        with pytest.raises(IntegrityError):
            await session.delete(ws)
            await session.flush()


@pytest.mark.asyncio
async def test_purging_a_workspace_takes_the_workset_corpus_with_it() -> None:
    """The KB, its DOCUMENTS and its EMBEDDINGS go — through `purge_workset`.

    All three are counted, not just the workset row and not just the
    KnowledgeBase: the acceptance criterion names `KnowledgeBase`,
    `KnowledgeDocument` AND `Embedding` precisely because counting the light
    rows is what would let the heavy orphans pass review. A cascade would
    delete the workset without calling `purge_workset` and leave every one of
    them behind, invisibly, because the row that knew what they belonged to is
    the one that got cascaded away.

    A BORROWED document is created alongside and asserted to SURVIVE. That is
    the other half of the criterion, and the reason `purge_workset` filters on
    `document_is_foreign`: deleting one *"would let a workspace take content
    out of somebody's knowledge base on its way out"*.
    """
    from app.services.workset import resolve_workset

    async with _temp_database() as sessions, sessions() as session:
        user = await _make_user(session, email="a@example.com")
        ws = await create_workspace(session, user_id=user.id, name="Doomed")
        await session.commit()

        workset = await resolve_workset(
            session,
            user,
            key="reports",
            scope=COORDINATION_SCOPE_WORKSPACE,
            workspace_id=ws.id,
        )
        # Give it a corpus KB, which is what a cascade would have orphaned.
        kb = KnowledgeBase(
            id=uuid.uuid4(), user_id=user.id, name="corpus", is_system_managed=True
        )
        session.add(kb)
        await session.flush()
        workset.knowledge_base_id = kb.id
        await session.commit()

        # An OWNED document with an embedding, and a BORROWED one from
        # somebody else's knowledge base. The purge must take the first and
        # leave the second.
        from app.models.embedding import Embedding
        from app.models.knowledge_base import KnowledgeDocument
        from app.models.workset import WorksetItem

        foreign_kb = KnowledgeBase(
            id=uuid.uuid4(), user_id=user.id, name="Somebody else's corpus"
        )
        session.add(foreign_kb)
        await session.flush()
        owned_doc = KnowledgeDocument(
            id=uuid.uuid4(),
            knowledge_base_id=kb.id,
            filename="owned.txt",
            source_type="manual",
        )
        borrowed_doc = KnowledgeDocument(
            id=uuid.uuid4(),
            knowledge_base_id=foreign_kb.id,
            filename="borrowed.txt",
            source_type="manual",
        )
        session.add_all([owned_doc, borrowed_doc])
        await session.flush()
        session.add(
            Embedding(
                id=uuid.uuid4(),
                knowledge_base_id=kb.id,
                source_type="document",
                source_id=str(owned_doc.id),
                content="a chunk of the owned document",
                # `purge_workset` deletes by `knowledge_base_id`, so the vector
                # itself is what matters here, not its dimensionality.
                embedding=[0.0] * 768,
            )
        )
        session.add_all(
            [
                WorksetItem(
                    id=uuid.uuid4(),
                    workset_id=workset.id,
                    title="owned",
                    knowledge_document_id=owned_doc.id,
                    document_is_foreign=False,
                ),
                WorksetItem(
                    id=uuid.uuid4(),
                    workset_id=workset.id,
                    title="borrowed",
                    knowledge_document_id=borrowed_doc.id,
                    document_is_foreign=True,
                ),
            ]
        )
        await session.commit()

        purged = await purge_workspace_worksets(session, ws)
        await session.commit()
        assert purged == 1

        assert await session.get(Workset, workset.id) is None
        assert await session.get(KnowledgeBase, kb.id) is None, (
            "the workset's own corpus must go with it, not be orphaned"
        )
        assert await session.get(KnowledgeDocument, owned_doc.id) is None, (
            "an owned document must not outlive the workset"
        )
        assert (
            (
                await session.execute(
                    select(Embedding).where(Embedding.knowledge_base_id == kb.id)
                )
            )
            .scalars()
            .all()
            == []
        ), "the vectors must go too — this is the leak the criterion names"

        # The other half: a borrowed document keeps its own knowledge base.
        assert await session.get(KnowledgeDocument, borrowed_doc.id) is not None, (
            "a borrowed document must survive — a workspace must not take "
            "content out of somebody else's knowledge base on its way out"
        )
        assert await session.get(KnowledgeBase, foreign_kb.id) is not None

        # And with the worksets gone the workspace deletes cleanly.
        await session.delete(ws)
        await session.commit()
        assert await session.get(Workspace, ws.id) is None


@pytest.mark.asyncio
async def test_shared_state_rows_cascade_with_the_workspace() -> None:
    """The one table where CASCADE is right: no owned heavy resource."""
    from app.models.shared_state import SharedState

    async with _temp_database() as sessions, sessions() as session:
        user = await _make_user(session, email="a@example.com")
        ws = await create_workspace(session, user_id=user.id, name="W")
        await session.flush()
        session.add(
            SharedState(
                id=uuid.uuid4(),
                user_id=user.id,
                key=f"workspace:{ws.id}:note",
                workspace_id=ws.id,
            )
        )
        await session.commit()

        await session.delete(ws)
        await session.commit()

        remaining = (
            (
                await session.execute(
                    select(SharedState).where(SharedState.workspace_id == ws.id)
                )
            )
            .scalars()
            .all()
        )
        assert remaining == []
