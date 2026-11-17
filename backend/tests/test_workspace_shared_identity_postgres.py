"""A workspace shared between two PEOPLE, not two agents.

Step 4a gave the tool tables a `workspace_id` but `(user_id, key)` still
decided identity, so two users pointed at one workspace each built their own
state and neither saw the other's. That was enough for Deep
Investigation — its agent acts for the requesting user, so both agents share a
`user_id` — and it was NOT a workspace.

What is under test here is the pair of decisions that closes it:

* **Identity** is per-user for an unfiled row and per-workspace for a filed
  one — two PARTIAL unique indexes, because one constraint cannot say
  "unique per user OR unique per workspace". The tempting shortcut (NULL the
  `user_id` on workspace rows) would have disabled uniqueness entirely, since
  NULLs are distinct in Postgres.
* **Read follows visibility, write requires the `config` tier**. A `read_only` grantee who could write would be
  injecting state other agents in that workspace then act on: no permission
  gained on paper, everyone else's agents steered. So the read and the write tiers are asserted
  SEPARATELY — a test that only checked reads would pass on the escalation.
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
from app.core.scopes import COORDINATION_SCOPE_WORKSPACE
from app.models.shared_state import SharedState
from app.models.user import User
from app.models.workset import Workset
from app.services.company_managed_writes import person_writer
from app.services.workspaces import (
    create_workspace,
    workspace_namespace,
    workspace_scope_ids,
)

# The integration job is the one CI runs with PostgreSQL: unmarked, every test
# below skips in the unit shards and none of them ever runs.
pytestmark = pytest.mark.integration

_PERSON = person_writer(is_admin=False)


def _base_url() -> str:
    return os.environ.get(
        "DATABASE_URL",
        "postgresql+asyncpg://copilot:copilot@localhost:5432/copilot",
    )


@asynccontextmanager
async def _temp_database() -> AsyncIterator[async_sessionmaker]:
    parsed = urllib.parse.urlparse(_base_url())
    admin_url = parsed._replace(path="/postgres").geturl()
    db_name = f"wsident_{uuid.uuid4().hex[:10]}"
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


def _share(workspace, user: User, permission: str) -> None:
    workspace.shared_user_ids = [str(user.id)]
    workspace.shared_user_permissions = {str(user.id): permission}


# ── Identity: per-user when unfiled, per-workspace when filed ────────────────


@pytest.mark.asyncio
async def test_two_users_in_one_workspace_collide_on_one_row() -> None:
    """The point of the whole increment.

    Before 4b this inserted two rows, one per user, and each saw only their
    own — a container that contained nothing shared.
    """
    async with _temp_database() as sessions, sessions() as session:
        alice = await _make_user(session, email="alice@example.com")
        bob = await _make_user(session, email="bob@example.com")
        ws = await create_workspace(session, user_id=alice.id, name="Shared")
        await session.flush()
        key = f"{workspace_namespace(ws.id)}:incident"

        session.add(
            SharedState(id=uuid.uuid4(), user_id=alice.id, key=key, workspace_id=ws.id)
        )
        await session.flush()
        session.add(
            SharedState(id=uuid.uuid4(), user_id=bob.id, key=key, workspace_id=ws.id)
        )
        with pytest.raises(IntegrityError):
            await session.flush()


@pytest.mark.asyncio
async def test_two_users_unfiled_keep_their_own_rows() -> None:
    """Per-user identity survives for everything outside a workspace.

    The partial predicate is what keeps this true: widening the workspace index
    to cover unfiled rows would have made one user's key block another's.
    """
    async with _temp_database() as sessions, sessions() as session:
        alice = await _make_user(session, email="alice@example.com")
        bob = await _make_user(session, email="bob@example.com")
        session.add_all(
            [
                SharedState(id=uuid.uuid4(), user_id=alice.id, key="incident"),
                SharedState(id=uuid.uuid4(), user_id=bob.id, key="incident"),
            ]
        )
        await session.flush()  # two rows, no collision
        rows = (
            (await session.execute(select(SharedState).where(SharedState.key == "incident")))
            .scalars()
            .all()
        )
        assert len(rows) == 2
        assert all(r.workspace_id is None for r in rows)


@pytest.mark.asyncio
async def test_the_same_key_in_two_workspaces_is_two_rows() -> None:
    async with _temp_database() as sessions, sessions() as session:
        alice = await _make_user(session, email="alice@example.com")
        one = await create_workspace(session, user_id=alice.id, name="One")
        two = await create_workspace(session, user_id=alice.id, name="Two")
        await session.flush()
        session.add_all(
            [
                SharedState(
                    id=uuid.uuid4(),
                    user_id=alice.id,
                    key=f"{workspace_namespace(one.id)}:k",
                    workspace_id=one.id,
                ),
                SharedState(
                    id=uuid.uuid4(),
                    user_id=alice.id,
                    key=f"{workspace_namespace(two.id)}:k",
                    workspace_id=two.id,
                ),
            ]
        )
        await session.flush()
        assert (
            len(
                (await session.execute(select(SharedState))).scalars().all()
            )
            == 2
        )


@pytest.mark.asyncio
async def test_one_workspace_corpus_is_shared_between_two_users() -> None:
    """`resolve_workset` must hand both people the SAME corpus.

    This is the workset half of the same identity change, and it goes through
    the real resolver rather than a hand-built row — because the resolver's
    ON CONFLICT had to be re-targeted at the partial index, and an
    `ON CONFLICT ON CONSTRAINT` naming the dropped constraint would raise here
    instead of resolving.
    """
    from app.services.workset import resolve_workset

    async with _temp_database() as sessions, sessions() as session:
        alice = await _make_user(session, email="alice@example.com")
        bob = await _make_user(session, email="bob@example.com")
        ws = await create_workspace(session, user_id=alice.id, name="Shared")
        # Bob is granted `config`, and that grant is the point of the test
        # rather than scenery. The first version shared nothing at all and
        # still asserted Bob got the corpus — which passed only because
        # `resolve_workset` had no authorisation check. The test was
        # documenting the bug.
        _share(ws, bob, "config")
        await session.commit()

        first = await resolve_workset(
            session, alice, key="reports",
            scope=COORDINATION_SCOPE_WORKSPACE, workspace_id=ws.id,
        )
        await session.commit()
        second = await resolve_workset(
            session, bob, key="reports",
            scope=COORDINATION_SCOPE_WORKSPACE, workspace_id=ws.id,
        )
        await session.commit()

        assert first.id == second.id, "two people, one workspace, one corpus"
        assert (
            len((await session.execute(select(Workset))).scalars().all()) == 1
        )


@pytest.mark.asyncio
async def test_a_read_only_grantee_cannot_delete_or_resolve_workspace_state(
    monkeypatch,
) -> None:
    """Removing state is the same escalation as writing it, in reverse.

    Four single-row paths computed the write tier and then discarded it
    (`readable, _writable = ...`), selected the row through the READ tier and
    mutated it. Only `bulk_delete_states` used the write tier — so the rule
    this feature states twice was enforced in one place out of five, and a
    `read_only` grantee could delete or resolve state other agents were
    acting on.

    All four are asserted, not one: they are four copies of the same mistake,
    and a fix applied only to the path that was reported would have left
    three.

    These services open their OWN session from `app.core.database`, so the
    factory is pointed at this test's scratch database — otherwise they would
    quietly operate on the developer's real one and the assertions would mean
    nothing.
    """
    from app.core import database
    from app.services.shared_state import (
        delete_state,
        delete_state_by_id,
        get_state,
        resolve_state,
        resolve_state_by_id,
        upsert_state,
    )

    async with _temp_database() as sessions:
        monkeypatch.setattr(database, "async_session", sessions)
        async with sessions() as session:
            alice = await _make_user(session, email="stateowner@example.com")
            bob = await _make_user(session, email="statereader@example.com")
            ws = await create_workspace(session, user_id=alice.id, name="Guarded")
            _share(ws, bob, "read_only")
            await session.commit()
            ws_id, bob_id, alice_id = ws.id, bob.id, alice.id

        namespaced = f"workspace:{ws_id}:findings"
        await upsert_state(
            user_id=str(alice_id),
            key=namespaced,
            value={"note": "alice's"},
            workspace_id=ws_id,
            writer=_PERSON,
        )

        async with sessions() as session:
            row = (await session.execute(select(SharedState))).scalars().one()
            row_id = row.id
            assert row.workspace_id == ws_id

        # Bob may READ it — that is what `read_only` means.
        assert await get_state(user_id=str(bob_id), key=namespaced) is not None

        # ...and may not remove or close it, by key or by id.
        assert (
            await delete_state(user_id=str(bob_id), key=namespaced, writer=_PERSON)
            is False
        )
        assert (
            await delete_state_by_id(
                user_id=str(bob_id), state_id=str(row_id), writer=_PERSON
            )
            is False
        )
        assert (
            await resolve_state(user_id=str(bob_id), key=namespaced, writer=_PERSON)
            is None
        )
        assert (
            await resolve_state_by_id(
                user_id=str(bob_id), state_id=str(row_id), writer=_PERSON
            )
            is None
        )

        async with sessions() as session:
            survivor = (await session.execute(select(SharedState))).scalars().one()
            assert survivor.status != "resolved", "resolve slipped through"

        # The owner still can — the gate must not lock everyone out.
        assert (
            await resolve_state(user_id=str(alice_id), key=namespaced, writer=_PERSON)
            is not None
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("name_the_workspace", [True, False])
async def test_a_read_only_grantee_cannot_overwrite_workspace_state(
    monkeypatch, name_the_workspace: bool
) -> None:
    """The write gate reads the row, not the argument.

    Leaving `workspace_id` out while sending the namespaced key used to skip
    the gate entirely: the row was found through the READ tier and merged.
    """
    from app.core import database
    from app.services.shared_state import WorkspaceWriteRefusedError, upsert_state

    async with _temp_database() as sessions:
        monkeypatch.setattr(database, "async_session", sessions)
        async with sessions() as session:
            alice = await _make_user(session, email="owner2@example.com")
            bob = await _make_user(session, email="reader2@example.com")
            ws = await create_workspace(session, user_id=alice.id, name="Guarded")
            _share(ws, bob, "read_only")
            await session.commit()
            ws_id, bob_id, alice_id = ws.id, bob.id, alice.id

        namespaced = f"workspace:{ws_id}:plan"
        await upsert_state(
            user_id=str(alice_id), key=namespaced, value={"v": 1}, writer=_PERSON
        )
        with pytest.raises(WorkspaceWriteRefusedError):
            await upsert_state(
                user_id=str(bob_id),
                key=namespaced,
                value={"v": 2},
                workspace_id=ws_id if name_the_workspace else None,
                writer=_PERSON,
            )

        async with sessions() as session:
            row = (await session.execute(select(SharedState))).scalars().one()
            # Filed from the key alone, and untouched by the refused write.
            assert row.workspace_id == ws_id
            assert row.value == {"v": 1}


@pytest.mark.asyncio
async def test_a_read_only_grantee_cannot_open_a_workspace_corpus() -> None:
    """The sharpest escalation in this feature, and it was unguarded.

    A workset owns a knowledge base, its documents and its vectors. A caller
    who can only READ the workspace but resolves a workset in it can ingest
    documents into a corpus every agent in that workspace then retrieves from
    and acts on — the injection half of the escalation the shared-state paths
    guard against, applied to RAG content, and worse because the poison is
    retrieved rather than merely read.
    """
    from app.services.workset import WorksetSourceError, resolve_workset

    async with _temp_database() as sessions, sessions() as session:
        alice = await _make_user(session, email="owner@example.com")
        bob = await _make_user(session, email="reader@example.com")
        ws = await create_workspace(session, user_id=alice.id, name="Sensitive")
        _share(ws, bob, "read_only")
        await session.commit()

        with pytest.raises(WorksetSourceError, match="permission"):
            await resolve_workset(
                session, bob, key="reports",
                scope=COORDINATION_SCOPE_WORKSPACE, workspace_id=ws.id,
            )

        # And nothing was created on the way to the refusal.
        assert (await session.execute(select(Workset))).scalars().all() == []


@pytest.mark.asyncio
async def test_a_stranger_cannot_open_a_workspace_corpus() -> None:
    """No grant at all is refused the same way a read-only one is.

    Separate from the `read_only` case because they fail for different
    reasons — one is in the readable tier and not the writable one, the other
    is in neither — and a fix that only handled the first would leave the
    wider hole open.
    """
    from app.services.workset import WorksetSourceError, resolve_workset

    async with _temp_database() as sessions, sessions() as session:
        alice = await _make_user(session, email="owner2@example.com")
        mallory = await _make_user(session, email="stranger@example.com")
        ws = await create_workspace(session, user_id=alice.id, name="Private")
        await session.commit()

        with pytest.raises(WorksetSourceError, match="permission"):
            await resolve_workset(
                session, mallory, key="reports",
                scope=COORDINATION_SCOPE_WORKSPACE, workspace_id=ws.id,
            )
        with pytest.raises(WorksetSourceError, match="permission"):
            await resolve_workset(
                session, mallory, key="reports",
                scope=COORDINATION_SCOPE_WORKSPACE, workspace_id=ws.id,
                write=False,
            )


@pytest.mark.asyncio
async def test_a_namespace_listing_is_filtered_before_the_page_is_cut(
    monkeypatch,
) -> None:
    """Newer entries of other namespaces must not push a namespace's own
    entries off the page the tool asks for."""
    from app.core import database
    from app.services.shared_state import list_states, upsert_state

    async with _temp_database() as sessions:
        monkeypatch.setattr(database, "async_session", sessions)
        async with sessions() as session:
            alice = await _make_user(session, email="lister@example.com")
            ws = await create_workspace(session, user_id=alice.id, name="Busy")
            await session.commit()
            ws_id, alice_id = ws.id, alice.id

        mine = f"workspace:{ws_id}:plan"
        await upsert_state(user_id=str(alice_id), key=mine, value={"v": 1}, writer=_PERSON)
        for n in range(3):
            await upsert_state(
                user_id=str(alice_id), key=f"other-{n}", value={"v": n}, writer=_PERSON
            )

        page = await list_states(
            user_id=str(alice_id), limit=1, key_prefix=f"workspace:{ws_id}:"
        )
        assert [s["key"] for s in page] == [mine]


@pytest.mark.asyncio
async def test_a_read_only_grantee_can_read_a_workspace_corpus() -> None:
    """Reads follow the workspace's visibility: a member who may read the
    workspace reads its worksets, and a read never creates one."""
    from app.services.workset import WorksetSourceError, resolve_workset

    async with _temp_database() as sessions, sessions() as session:
        alice = await _make_user(session, email="owner3@example.com")
        bob = await _make_user(session, email="reader3@example.com")
        ws = await create_workspace(session, user_id=alice.id, name="Readable")
        _share(ws, bob, "read_only")
        await session.commit()

        with pytest.raises(WorksetSourceError, match="no such workset"):
            await resolve_workset(
                session, bob, key="reports",
                scope=COORDINATION_SCOPE_WORKSPACE, workspace_id=ws.id,
                write=False,
            )
        assert (await session.execute(select(Workset))).scalars().all() == []

        created = await resolve_workset(
            session, alice, key="reports",
            scope=COORDINATION_SCOPE_WORKSPACE, workspace_id=ws.id,
        )
        await session.commit()
        read = await resolve_workset(
            session, bob, key="reports",
            scope=COORDINATION_SCOPE_WORKSPACE, workspace_id=ws.id,
            write=False,
        )
        assert read.id == created.id


# ── The two tiers, asserted separately ──────────────────────────────────────


@pytest.mark.asyncio
async def test_a_read_only_grantee_can_read_but_not_write() -> None:
    """The escalation this decision exists to prevent.

    A test that only checked the read tier would pass while `read_only` could
    write, so both are asserted here in one place.
    """
    from app.api.v1.helpers import SHARE_PERMISSION_READ_ONLY

    async with _temp_database() as sessions, sessions() as session:
        alice = await _make_user(session, email="alice@example.com")
        bob = await _make_user(session, email="bob@example.com")
        ws = await create_workspace(session, user_id=alice.id, name="Shared")
        _share(ws, bob, SHARE_PERMISSION_READ_ONLY)
        await session.commit()

        assert ws.id in await workspace_scope_ids(session, bob, writable=False)
        assert ws.id not in await workspace_scope_ids(session, bob, writable=True)


@pytest.mark.asyncio
async def test_a_config_grantee_can_write() -> None:
    from app.api.v1.helpers import SHARE_PERMISSION_CONFIG

    async with _temp_database() as sessions, sessions() as session:
        alice = await _make_user(session, email="alice@example.com")
        bob = await _make_user(session, email="bob@example.com")
        ws = await create_workspace(session, user_id=alice.id, name="Shared")
        _share(ws, bob, SHARE_PERMISSION_CONFIG)
        await session.commit()

        assert ws.id in await workspace_scope_ids(session, bob, writable=False)
        assert ws.id in await workspace_scope_ids(session, bob, writable=True)


@pytest.mark.asyncio
async def test_the_owner_can_write_their_own_workspace() -> None:
    async with _temp_database() as sessions, sessions() as session:
        alice = await _make_user(session, email="alice@example.com")
        ws = await create_workspace(session, user_id=alice.id, name="Mine")
        await session.commit()
        assert ws.id in await workspace_scope_ids(session, alice, writable=True)


@pytest.mark.asyncio
async def test_a_stranger_gets_neither_tier() -> None:
    async with _temp_database() as sessions, sessions() as session:
        alice = await _make_user(session, email="alice@example.com")
        bob = await _make_user(session, email="bob@example.com")
        ws = await create_workspace(session, user_id=alice.id, name="Private")
        await session.commit()

        assert ws.id not in await workspace_scope_ids(session, bob, writable=False)
        assert ws.id not in await workspace_scope_ids(session, bob, writable=True)


@pytest.mark.asyncio
async def test_an_archived_workspace_is_in_neither_tier() -> None:
    """Excluded from READ as well as write.

    Leaving it readable would mean a picker that hides a workspace while its
    state still surfaced in a list — a half-cut that is harder to reason about
    than a clean one, and `resolve_workspace_scope` already refuses to write
    there.
    """
    async with _temp_database() as sessions, sessions() as session:
        alice = await _make_user(session, email="alice@example.com")
        ws = await create_workspace(session, user_id=alice.id, name="Retired")
        ws.is_archived = True
        await session.commit()

        assert ws.id not in await workspace_scope_ids(session, alice, writable=False)
        assert ws.id not in await workspace_scope_ids(session, alice, writable=True)


# ── The read clause actually uses the tier ──────────────────────────────────


@pytest.mark.asyncio
async def test_the_visible_clause_admits_a_readable_workspace_row() -> None:
    """And admits it for someone who is NOT the author.

    The author's own-row tier is deliberately restricted to UNFILED rows, so
    this row is reachable only through the workspace tier — which is what makes
    the assertion meaningful rather than incidentally true.
    """
    from app.services.shared_state import _visible_clause

    async with _temp_database() as sessions, sessions() as session:
        alice = await _make_user(session, email="alice@example.com")
        bob = await _make_user(session, email="bob@example.com")
        ws = await create_workspace(session, user_id=alice.id, name="Shared")
        await session.flush()
        session.add(
            SharedState(
                id=uuid.uuid4(),
                user_id=alice.id,
                key=f"{workspace_namespace(ws.id)}:note",
                workspace_id=ws.id,
            )
        )
        await session.commit()

        # Bob with the tier: visible.
        rows = (
            (
                await session.execute(
                    select(SharedState).where(_visible_clause(bob.id, [ws.id]))
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) == 1

        # Bob without it: nothing. An empty IN must render false, not true.
        assert (
            (
                await session.execute(
                    select(SharedState).where(_visible_clause(bob.id, []))
                )
            )
            .scalars()
            .all()
            == []
        )


@pytest.mark.asyncio
async def test_the_author_loses_a_filed_row_when_they_lose_the_workspace() -> None:
    """Authorship is provenance here, not a standing grant.

    Alice wrote the row, but it is filed into a workspace; if she is not in the
    read tier for it, she does not see it. Without this the own-row tier would
    be a back door around every workspace permission.
    """
    from app.services.shared_state import _visible_clause

    async with _temp_database() as sessions, sessions() as session:
        alice = await _make_user(session, email="alice@example.com")
        ws = await create_workspace(session, user_id=alice.id, name="Shared")
        await session.flush()
        session.add(
            SharedState(
                id=uuid.uuid4(),
                user_id=alice.id,
                key=f"{workspace_namespace(ws.id)}:note",
                workspace_id=ws.id,
            )
        )
        await session.commit()

        assert (
            (
                await session.execute(
                    select(SharedState).where(_visible_clause(alice.id, []))
                )
            )
            .scalars()
            .all()
            == []
        )


# ── The write tier on every MUTATING path, not just create ──────────────────
#
# Found the hard way: the first cut of 4b threaded the READ tier into every
# path, including the mutating ones — so a `read_only` grantee could update,
# resolve or delete a workspace-scoped entry even though creating one was
# refused. A test that only exercised `create` passed while the escalation was
# wide open, which is why each verb is asserted separately below.


@asynccontextmanager
async def _pointed_at_db(sessions):
    """Point `async_session` at the scratch database.

    The shared-state service opens its OWN session per operation, so a test
    holding only a session would exercise nothing.
    """
    from app.core import database as db_module

    original = db_module.async_session
    db_module.async_session = sessions
    try:
        yield
    finally:
        db_module.async_session = original


async def _workspace_entry(sessions, *, permission: str):
    """A workspace entry Alice wrote, shared with Bob at *permission*."""
    async with sessions() as session:
        alice = await _make_user(session, email="alice@example.com")
        bob = await _make_user(session, email="bob@example.com")
        ws = await create_workspace(session, user_id=alice.id, name="Shared")
        ws.shared_user_ids = [str(bob.id)]
        ws.shared_user_permissions = {str(bob.id): permission}
        await session.flush()
        key = f"{workspace_namespace(ws.id)}:incident"
        session.add(
            SharedState(
                id=uuid.uuid4(),
                user_id=alice.id,
                key=key,
                workspace_id=ws.id,
                value={"seen": 1},
            )
        )
        await session.commit()
        return key, str(bob.id), str(ws.id)


@pytest.mark.asyncio
async def test_read_only_can_read_the_entry_but_not_change_it() -> None:
    from app.services.shared_state import (
        WorkspaceWriteRefusedError,
        delete_state,
        get_state,
        upsert_state,
    )

    async with _temp_database() as sessions:
        key, bob_id, _ = await _workspace_entry(sessions, permission="read_only")
        async with _pointed_at_db(sessions):
            # Reads: allowed, and that is the point of sharing.
            assert await get_state(user_id=bob_id, key=key) is not None

            # Writes: refused, on the UPDATE path — the create gate never
            # fires here because Bob names no workspace, he just writes a key
            # that already exists in one.
            with pytest.raises(WorkspaceWriteRefusedError):
                await upsert_state(
                    user_id=bob_id, key=key, value={"seen": 2}, writer=_PERSON
                )

            # Delete: answered as "nothing removed", indistinguishable from a
            # missing key so the refusal leaks no existence.
            assert await delete_state(user_id=bob_id, key=key, writer=_PERSON) is False

        async with sessions() as session:
            row = (
                (await session.execute(select(SharedState).where(SharedState.key == key)))
                .scalars()
                .one()
            )
            assert row.value == {"seen": 1}, "the read-only grantee changed it"


@pytest.mark.asyncio
async def test_a_config_grantee_can_change_the_entry() -> None:
    from app.services.shared_state import upsert_state

    async with _temp_database() as sessions:
        key, bob_id, _ = await _workspace_entry(sessions, permission="config")
        async with _pointed_at_db(sessions):
            out = await upsert_state(
                user_id=bob_id, key=key, value={"seen": 2}, writer=_PERSON
            )
            assert out["value"]["seen"] == 2
