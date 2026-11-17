"""The Workspace entity: its invariants, and its visibility.

Three things are under test here, and only one of them is CRUD.

**The database is the authority on both invariants.** One default per owner
and one slug per owner are enforced by partial unique indexes, not by the
service, because either can be violated by two replicas racing on the same
request. So the tests assert against Postgres — a partial index is not
something SQLite can stand in for — and they assert the *loser* of a race
gets the winner's row rather than a 500.

**Non-admin visibility is exercised deliberately.** ``user_visible_filter``
short-circuits on ``is_admin=True`` before touching a single column, so an
entity missing a visibility column passes every admin test and 500s for every
ordinary user. That is not hypothetical: ``flows``, ``flow_event_types`` and
``nova_rules`` each shipped that way. Every list assertion below therefore
runs as a NON-admin.

**The refusals are behaviour, not decoration.** The default workspace cannot
be archived or deleted; both are refused with a reason rather than silently
promoting some other workspace, because which one should inherit "default" is
the owner's decision.
"""

from __future__ import annotations

import logging
import os
import urllib.parse
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError, IntegrityError, OperationalError, ProgrammingError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.models  # noqa: F401 — registers every mapper
from app.api import deps
from app.api.v1 import workspaces as workspaces_api
from app.core.database import Base, get_db
from app.models.user import User
from app.models.workspace import Workspace
from app.services.workspaces import (
    create_workspace,
    ensure_default_workspace,
    slugify,
    unique_slug,
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
    db_name = f"wspace_{uuid.uuid4().hex[:12]}"
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
    # `User.is_admin` is a read-only property over `role`, not a column.
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


@asynccontextmanager
async def _client(session, user: User) -> AsyncIterator[httpx.AsyncClient]:
    """The workspaces router over a REAL session, as the given user.

    An in-loop ASGI client rather than ``TestClient``: the latter drives the
    app on its own event loop in a worker thread, and the asyncpg connection
    behind ``session`` belongs to this one — the request would fail with
    "attached to a different loop" before reaching any assertion.

    ``get_db`` is overridden with a generator that commits on success, exactly
    like the production dependency. The lazily created default workspace
    relies on that commit, so a harness that skipped it would prove the wrong
    thing.
    """
    api = FastAPI()
    api.include_router(workspaces_api.router, prefix="/api/v1/workspaces")
    api.dependency_overrides[deps.get_current_user] = lambda: user
    api.dependency_overrides[deps.require_agentic_ai_licensed] = lambda: None

    async def _get_db():
        yield session
        await session.commit()

    api.dependency_overrides[get_db] = _get_db
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=api), base_url="http://test"
    ) as client:
        yield client


# ── Slugs (pure) ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Threat Research", "threat-research"),
        ("  Spaces  Everywhere  ", "spaces-everywhere"),
        ("CVE-2024-1234!!", "cve-2024-1234"),
        ("---", "workspace"),
        ("研究", "workspace"),
        ("", "workspace"),
    ],
)
def test_slugify_never_returns_empty(name: str, expected: str) -> None:
    """A name of characters the slug drops must not produce a blank handle.

    A blank slug would collide with every other such name and make the
    per-owner unique index reject the second one — a rename in one alphabet
    breaking creation in another.
    """
    assert slugify(name) == expected


# ── The two database invariants ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_second_default_is_refused_by_the_database() -> None:
    """The invariant is a constraint, not a convention.

    Asserted directly rather than through the service, because the service's
    race handling is only correct if this actually raises.
    """
    async with _temp_database() as sessions, sessions() as session:
        user = await _make_user(session, email="one@example.com")
        session.add(
            Workspace(
                id=uuid.uuid4(),
                user_id=user.id,
                name="A",
                slug="a",
                is_default=True,
            )
        )
        await session.flush()
        session.add(
            Workspace(
                id=uuid.uuid4(),
                user_id=user.id,
                name="B",
                slug="b",
                is_default=True,
            )
        )
        with pytest.raises(IntegrityError):
            await session.flush()


@pytest.mark.asyncio
async def test_two_owners_may_share_a_slug_but_one_owner_may_not() -> None:
    """Uniqueness is per owner: the slug is a handle, not a global identity."""
    async with _temp_database() as sessions, sessions() as session:
        alice = await _make_user(session, email="alice@example.com")
        bob = await _make_user(session, email="bob@example.com")
        session.add_all(
            [
                Workspace(
                    id=uuid.uuid4(), user_id=alice.id, name="R", slug="research"
                ),
                Workspace(
                    id=uuid.uuid4(), user_id=bob.id, name="R", slug="research"
                ),
            ]
        )
        await session.flush()  # different owners: fine

        session.add(
            Workspace(
                id=uuid.uuid4(), user_id=alice.id, name="R2", slug="research"
            )
        )
        with pytest.raises(IntegrityError):
            await session.flush()


@pytest.mark.asyncio
async def test_unique_slug_suffixes_within_an_owner_only() -> None:
    async with _temp_database() as sessions, sessions() as session:
        alice = await _make_user(session, email="alice@example.com")
        bob = await _make_user(session, email="bob@example.com")
        await create_workspace(session, user_id=alice.id, name="Research")
        await session.commit()

        assert await unique_slug(session, alice.id, "Research") == "research-2"
        # Bob's namespace is untouched by Alice's collisions.
        assert await unique_slug(session, bob.id, "Research") == "research"


# ── The default workspace ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_ensure_default_is_idempotent_and_absorbs_a_lost_race() -> None:
    """The loser of the race gets the winner's row, not a 500.

    The race is simulated with two independent sessions so the second insert
    genuinely hits the index rather than a local identity map — which is the
    only version of this test that proves anything about two replicas.
    """
    async with _temp_database() as sessions:
        async with sessions() as session:
            user = await _make_user(session, email="race@example.com")
            await session.commit()
            user_id = user.id

        async with sessions() as first:
            created = await ensure_default_workspace(first, user_id)
            await first.commit()

        # A second session that read BEFORE the winner committed would insert
        # and lose. Calling again is the same code path with the row already
        # present, and must return it rather than create a second.
        async with sessions() as second:
            again = await ensure_default_workspace(second, user_id)
            await second.commit()
            assert again.id == created.id

        async with sessions() as check:
            rows = (
                (
                    await check.execute(
                        select(Workspace).where(Workspace.user_id == user_id)
                    )
                )
                .scalars()
                .all()
            )
            assert len(rows) == 1
            assert rows[0].is_default is True


@pytest.mark.asyncio
async def test_listing_creates_the_default_once() -> None:
    """First call creates it; the second must not create a second one."""
    async with _temp_database() as sessions, sessions() as session:
        user = await _make_user(session, email="lister@example.com")
        await session.commit()
        async with _client(session, user) as client:

            first = await client.get("/api/v1/workspaces")
            assert first.status_code == 200, first.text
            assert len(first.json()) == 1
            assert first.json()[0]["is_default"] is True

            second = await client.get("/api/v1/workspaces")
            assert second.status_code == 200
            assert len(second.json()) == 1
            assert second.json()[0]["id"] == first.json()[0]["id"]


# ── Visibility, as a NON-admin (AGENTS.md rule 19) ───────────────────────────


@pytest.mark.asyncio
async def test_a_non_admin_can_list_and_sees_only_their_own() -> None:
    """The regression that shipped three times.

    ``user_visible_filter`` returns early for admins without touching a
    column, so a missing visibility column is invisible to an admin-only test
    and a 500 for every ordinary user. This asserts the non-admin branch runs
    AND that it isolates.
    """
    async with _temp_database() as sessions, sessions() as session:
        alice = await _make_user(session, email="alice@example.com")
        bob = await _make_user(session, email="bob@example.com")
        assert alice.is_admin is False
        await create_workspace(session, user_id=bob.id, name="Bob's Private")
        await session.commit()

        async with _client(session, alice) as client:
            listed = await client.get("/api/v1/workspaces")
            assert listed.status_code == 200, listed.text
            names = {w["name"] for w in listed.json()}
            assert "Bob's Private" not in names


@pytest.mark.asyncio
async def test_a_non_admin_cannot_read_someone_elses_workspace() -> None:
    """Answered like an unknown id: a 403 would confirm it exists."""
    async with _temp_database() as sessions, sessions() as session:
        alice = await _make_user(session, email="alice@example.com")
        bob = await _make_user(session, email="bob@example.com")
        bobs = await create_workspace(session, user_id=bob.id, name="Bob's")
        await session.commit()

        async with _client(session, alice) as client:
            got = await client.get(f"/api/v1/workspaces/{bobs.id}")
            assert got.status_code == 404, got.text
            # And the body must not name it either.
            assert "Bob" not in got.text
            malformed = await client.get("/api/v1/workspaces/not-a-uuid")
            assert malformed.status_code == 404, malformed.text


@pytest.mark.asyncio
async def test_a_workspace_readable_but_not_manageable_opens_read_only() -> None:
    """What the list shows, the detail route opens, and `can_manage` says why.

    A read-only grantee and a non-admin on a company-managed workspace both
    see it listed; opening it must not 403 on the manage gate, and editing it
    still must.
    """
    async with _temp_database() as sessions, sessions() as session:
        alice = await _make_user(session, email="alice@example.com")
        bob = await _make_user(session, email="bob@example.com")
        shared = await create_workspace(
            session,
            user_id=bob.id,
            name="Shared With Alice",
            shared_user_ids=[str(alice.id)],
            shared_user_permissions={str(alice.id): "read_only"},
        )
        company = await create_workspace(
            session, user_id=bob.id, name="Company", is_company_managed=True
        )
        await session.commit()

        async with _client(session, alice) as client:
            listed = {w["id"]: w for w in (await client.get("/api/v1/workspaces")).json()}
            for ws in (shared, company):
                assert str(ws.id) in listed
                assert listed[str(ws.id)]["can_manage"] is False

                got = await client.get(f"/api/v1/workspaces/{ws.id}")
                assert got.status_code == 200, got.text
                assert got.json()["can_manage"] is False

                edit = await client.put(
                    f"/api/v1/workspaces/{ws.id}", json={"name": "Renamed"}
                )
                assert edit.status_code == 403, edit.text

            own_default = next(w for w in listed.values() if w["is_default"])
            assert own_default["can_manage"] is True


@pytest.mark.asyncio
async def test_only_a_list_that_asks_pays_for_the_counts() -> None:
    """The pickers list workspaces too; only the management view counts."""
    async with _temp_database() as sessions, sessions() as session:
        user = await _make_user(session, email="counter@example.com")
        await session.commit()
        async with _client(session, user) as client:
            plain = (await client.get("/api/v1/workspaces")).json()
            assert plain and all(w["contents"] is None for w in plain)
            counted = (
                await client.get("/api/v1/workspaces", params={"with_contents": True})
            ).json()
            assert counted and all(w["contents"] is not None for w in counted)
            assert counted[0]["contents"]["work_items"] == 0


@pytest.mark.asyncio
async def test_an_admin_lists_the_library_set_unless_asking_for_all() -> None:
    """Every user owns a default, so an admin bypass on the list floods it."""
    async with _temp_database() as sessions, sessions() as session:
        admin = await _make_user(session, email="admin@example.com", is_admin=True)
        bob = await _make_user(session, email="bob@example.com")
        bobs = await create_workspace(session, user_id=bob.id, name="Bob's Private")
        await session.commit()

        async with _client(session, admin) as client:
            visible = await client.get("/api/v1/workspaces")
            assert visible.status_code == 200, visible.text
            assert str(bobs.id) not in {w["id"] for w in visible.json()}

            everything = await client.get("/api/v1/workspaces", params={"scope": "all"})
            assert everything.status_code == 200, everything.text
            assert str(bobs.id) in {w["id"] for w in everything.json()}

            # The detail route keeps the admin bypass.
            got = await client.get(f"/api/v1/workspaces/{bobs.id}")
            assert got.status_code == 200, got.text
            assert got.json()["can_manage"] is True


@pytest.mark.asyncio
async def test_transferring_a_default_leaves_every_owner_one_default() -> None:
    """The default and the slug are per owner, and a transfer changes the owner.

    Bob's default moves to Alice, who already has a default AND a workspace
    with the same slug: carried over as they were, both per-owner uniques
    would reject the write and the transfer would 500.
    """
    async with _temp_database() as sessions, sessions() as session:
        admin = await _make_user(session, email="admin@example.com", is_admin=True)
        alice = await _make_user(session, email="alice@example.com")
        bob = await _make_user(session, email="bob@example.com")
        await ensure_default_workspace(session, alice.id)
        bobs_default = await ensure_default_workspace(session, bob.id)
        await session.commit()
        assert bobs_default.slug == "general"

        async with _client(session, admin) as client:
            moved = await client.put(
                f"/api/v1/workspaces/{bobs_default.id}",
                json={"user_id": str(alice.id)},
            )
            assert moved.status_code == 200, moved.text
            assert moved.json()["user_id"] == str(alice.id)
            assert moved.json()["is_default"] is False
            assert moved.json()["slug"] != "general"

        async with _client(session, alice) as client:
            defaults = [
                w
                for w in (await client.get("/api/v1/workspaces")).json()
                if w["is_default"] and w["user_id"] == str(alice.id)
            ]
            assert len(defaults) == 1

        async with _client(session, bob) as client:
            defaults = [
                w for w in (await client.get("/api/v1/workspaces")).json() if w["is_default"]
            ]
            assert len(defaults) == 1
            assert defaults[0]["id"] != str(bobs_default.id)


# ── CRUD and its refusals ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_derives_a_slug_and_suffixes_a_collision() -> None:
    async with _temp_database() as sessions, sessions() as session:
        user = await _make_user(session, email="creator@example.com")
        await session.commit()
        async with _client(session, user) as client:

            first = await client.post("/api/v1/workspaces", json={"name": "Threat Research"})
            assert first.status_code == 201, first.text
            assert first.json()["slug"] == "threat-research"
            assert first.json()["is_default"] is False

            second = await client.post("/api/v1/workspaces", json={"name": "Threat Research"})
            assert second.status_code == 201, second.text
            assert second.json()["slug"] == "threat-research-2"


@pytest.mark.asyncio
async def test_renaming_does_not_re_slug() -> None:
    """A slug that followed the name would break every link pointing at it."""
    async with _temp_database() as sessions, sessions() as session:
        user = await _make_user(session, email="renamer@example.com")
        await session.commit()
        async with _client(session, user) as client:

            created = await client.post("/api/v1/workspaces", json={"name": "Old Name"})
            ws_id = created.json()["id"]
            renamed = await client.put(
                f"/api/v1/workspaces/{ws_id}", json={"name": "Totally New"}
            )
            assert renamed.status_code == 200, renamed.text
            assert renamed.json()["name"] == "Totally New"
            assert renamed.json()["slug"] == "old-name"


@pytest.mark.asyncio
async def test_the_default_cannot_be_archived_or_deleted() -> None:
    async with _temp_database() as sessions, sessions() as session:
        user = await _make_user(session, email="keeper@example.com")
        await session.commit()
        async with _client(session, user) as client:

            default_id = (await client.get("/api/v1/workspaces")).json()[0]["id"]

            archived = await client.put(
                f"/api/v1/workspaces/{default_id}", json={"is_archived": True}
            )
            assert archived.status_code == 400
            assert "default workspace" in archived.json()["detail"]

            deleted = await client.delete(f"/api/v1/workspaces/{default_id}")
            assert deleted.status_code == 400
            assert "default workspace" in deleted.json()["detail"]

            # Still exactly one workspace, still the default.
            remaining = (await client.get("/api/v1/workspaces")).json()
            assert len(remaining) == 1
            assert remaining[0]["is_default"] is True


@pytest.mark.asyncio
async def test_setting_a_new_default_clears_the_old_one() -> None:
    """Two statements in one transaction, and the order matters.

    Setting the new default before clearing the old one would transiently hold
    two and trip the partial unique — the constraint working correctly on a
    legitimate request.
    """
    async with _temp_database() as sessions, sessions() as session:
        user = await _make_user(session, email="switcher@example.com")
        await session.commit()
        async with _client(session, user) as client:

            old_id = (await client.get("/api/v1/workspaces")).json()[0]["id"]
            new_id = (await client.post("/api/v1/workspaces", json={"name": "Second"})).json()["id"]

            promoted = await client.post(f"/api/v1/workspaces/{new_id}/default")
            assert promoted.status_code == 200, promoted.text
            assert promoted.json()["is_default"] is True

            listed = {w["id"]: w["is_default"] for w in (await client.get(
                "/api/v1/workspaces"
            )).json()}
            assert listed[new_id] is True
            assert listed[old_id] is False


async def _two_defaults(session, user: User) -> tuple[uuid.UUID, uuid.UUID]:
    """Break the one-default invariant the way a data fix or raw SQL could.

    The partial unique is dropped first: it is what makes the state impossible
    under normal operation, and the readers must survive it anyway.
    Returns the older and the newer default's ids.
    """
    await session.execute(text("DROP INDEX uq_workspaces_default_per_user"))
    older = Workspace(
        id=uuid.uuid4(),
        user_id=user.id,
        name="Older",
        slug="older",
        is_default=True,
        created_at=datetime(2024, 1, 1, tzinfo=UTC),
    )
    newer = Workspace(
        id=uuid.uuid4(),
        user_id=user.id,
        name="Newer",
        slug="newer",
        is_default=True,
        created_at=datetime(2025, 1, 1, tzinfo=UTC),
    )
    session.add_all([older, newer])
    await session.commit()
    return older.id, newer.id


@pytest.mark.asyncio
async def test_two_defaults_still_list_and_resolve_to_the_oldest() -> None:
    """A broken invariant must not turn every listing into a 500.

    ``ensure_default_workspace`` runs on every listing; a reader insisting on
    exactly one row raised ``MultipleResultsFound`` there, and the user could
    not even reach the UI that would let them fix it.
    """
    async with _temp_database() as sessions, sessions() as session:
        user = await _make_user(session, email="twice@example.com")
        older_id, newer_id = await _two_defaults(session, user)

        assert (await ensure_default_workspace(session, user.id)).id == older_id

        async with _client(session, user) as client:
            listed = await client.get("/api/v1/workspaces")
            assert listed.status_code == 200, listed.text
            # Nothing created on top of the two: a default was found.
            assert {w["id"] for w in listed.json()} == {str(older_id), str(newer_id)}


@pytest.mark.asyncio
async def test_setting_a_default_over_two_clears_both(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Promoting a workspace repairs the invariant instead of 500ing.

    Clearing only one of the two would leave the other standing beside the
    new default, so every current default is cleared, and the violation is
    logged once.
    """
    async with _temp_database() as sessions, sessions() as session:
        user = await _make_user(session, email="repair@example.com")
        await _two_defaults(session, user)

        async with _client(session, user) as client:
            new_id = (
                await client.post("/api/v1/workspaces", json={"name": "Chosen"})
            ).json()["id"]
            with caplog.at_level(logging.ERROR, logger="app.services.workspaces"):
                promoted = await client.post(f"/api/v1/workspaces/{new_id}/default")
            assert promoted.status_code == 200, promoted.text
            assert promoted.json()["is_default"] is True

        defaults = (
            await session.execute(
                select(Workspace.id).where(
                    Workspace.user_id == user.id, Workspace.is_default.is_(True)
                )
            )
        ).scalars().all()
        assert defaults == [uuid.UUID(new_id)]
        violations = [
            r for r in caplog.records
            if r.name == "app.services.workspaces" and r.levelno == logging.ERROR
        ]
        assert len(violations) == 1
        assert violations[0].args == (user.id, 2)


@pytest.mark.asyncio
async def test_an_archived_workspace_cannot_become_the_default() -> None:
    """The default is where a user lands; somewhere hidden is a dead end."""
    async with _temp_database() as sessions, sessions() as session:
        user = await _make_user(session, email="archiver@example.com")
        await session.commit()
        async with _client(session, user) as client:

            ws_id = (await client.post("/api/v1/workspaces", json={"name": "Later"})).json()["id"]
            assert (
                (await client.put(
                    f"/api/v1/workspaces/{ws_id}", json={"is_archived": True}
                )).status_code
                == 200
            )
            refused = await client.post(f"/api/v1/workspaces/{ws_id}/default")
            assert refused.status_code == 400
            assert "archived" in refused.json()["detail"].lower()


@pytest.mark.asyncio
async def test_archived_workspaces_are_hidden_unless_asked_for() -> None:
    async with _temp_database() as sessions, sessions() as session:
        user = await _make_user(session, email="hider@example.com")
        await session.commit()
        async with _client(session, user) as client:

            ws_id = (await client.post("/api/v1/workspaces", json={"name": "Old Work"})).json()["id"]
            await client.put(f"/api/v1/workspaces/{ws_id}", json={"is_archived": True})

            visible = {w["id"] for w in (await client.get("/api/v1/workspaces")).json()}
            assert ws_id not in visible

            with_archived = {
                w["id"]
                for w in (await client.get(
                    "/api/v1/workspaces", params={"include_archived": True}
                )).json()
            }
            assert ws_id in with_archived


@pytest.mark.asyncio
async def test_a_non_default_workspace_deletes() -> None:
    async with _temp_database() as sessions, sessions() as session:
        user = await _make_user(session, email="deleter@example.com")
        await session.commit()
        async with _client(session, user) as client:

            ws_id = (await client.post("/api/v1/workspaces", json={"name": "Scratch"})).json()["id"]
            assert (await client.delete(f"/api/v1/workspaces/{ws_id}")).status_code == 204
            assert ws_id not in {
                w["id"] for w in (await client.get("/api/v1/workspaces")).json()
            }


# ── Migration / model parity, including indexes ──────────────────────────────


#: Tables whose migrated and `create_all` shapes must match exactly.
#: Extend this as later increments add columns — the tool tables in
#: particular, whose partial uniques carry the same weight as these.
PARITY_TABLES = (
    "workspaces",
    "workspace_knowledge_bases",
    "conversations",
    # The tool tables. These matter MOST: their `workspace_id`
    # arrives by ALTER on hot tables, with CONCURRENTLY index builds and a
    # CHECK added NOT VALID then validated — the shape most likely to end up
    # subtly different from what `create_all` produces, and the one whose
    # drift would mean fresh installs and upgrades enforcing different rules.
    "shared_states",
    "worksets",
    "work_items",
    # Workspace knowledge: the owning FK and the one-per-role partial
    # unique arrive by ALTER on an existing table, the linked skills by a new
    # table.
    "knowledge_bases",
    "workspace_skills",
    "workspace_conversation_indexes",
)


@pytest.mark.asyncio
async def test_listing_works_when_the_default_slug_is_already_taken() -> None:
    """Creating "General" before ever listing must not brick the list.

    The path is ordinary and the failure was total. ``POST`` does not create a
    default, so a client that creates before it lists — a script, a bootstrap
    step, anything not driven by the UI — can own the slug ``general`` while
    still having no default. The first version treated the resulting
    ``uq_workspaces_slug_per_user`` violation as the default-per-user RACE,
    found no winner to re-read, and raised: ``GET /workspaces`` 500ed for that
    user on every call, for ever. The Workspaces surface lists on mount, so
    they could not even reach the UI that would let them rename it.

    Asserted over HTTP rather than on the service, because "the list endpoint
    answers" is the actual claim — and the 500 was on the endpoint.
    """
    async with _temp_database() as sessions, sessions() as session:
        user = await _make_user(session, email="collider@example.com")
        await session.commit()
        async with _client(session, user) as client:
            created = await client.post(
                "/api/v1/workspaces", json={"name": "General"}
            )
            assert created.status_code == 201, created.text
            assert created.json()["slug"] == "general"
            assert created.json()["is_default"] is False

            listed = await client.get("/api/v1/workspaces")
            assert listed.status_code == 200, listed.text

            rows = listed.json()
            defaults = [w for w in rows if w["is_default"]]
            assert len(defaults) == 1, rows
            # The default took a free slug rather than failing. Its slug is a
            # handle, not an identity — nothing persisted references it.
            assert defaults[0]["slug"] != "general"
            assert defaults[0]["name"] == "General"

            # Idempotent: listing again neither 500s nor makes a second one.
            again = await client.get("/api/v1/workspaces")
            assert again.status_code == 200
            assert len([w for w in again.json() if w["is_default"]]) == 1


#: Columns whose `server_default` drifts between the migration and
#: `create_all` and that **this stack did not create**, as
#: ``(table, column)``. FROZEN — never add to it to make a change pass.
#:
#: `worksets` predates this stack and twelve of its columns
#: carry a DDL default in their migration and none in the model. That is a real
#: bug with the real consequence: a fresh install gets NOT NULL and no default,
#: so any INSERT skipping the ORM fails there and succeeds on an upgraded
#: database. It is listed rather than fixed because fixing twelve columns of
#: another feature, mid-stack and unreviewed, is a wider change than this PR
#: should carry — and listed rather than ignored because the widened check
#: found it and silence would be the wrong record.
#:
#: The columns THIS stack added to `worksets` (`workspace_id`) are deliberately
#: absent: they are aligned, and they must stay pinned.
_PREEXISTING_DEFAULT_DRIFT: frozenset[tuple[str, str]] = frozenset({
    ("worksets", "auto_calls_used"),
    ("worksets", "content_chars"),
    ("worksets", "evicted_count"),
    ("worksets", "is_company_managed"),
    ("worksets", "item_count"),
    ("worksets", "max_auto_calls"),
    ("worksets", "max_content_chars"),
    ("worksets", "max_items"),
    ("worksets", "processing"),
    ("worksets", "scope"),
    ("worksets", "status"),
    ("worksets", "workers"),
})


@pytest.mark.asyncio
async def test_open_work_items_counts_every_live_status() -> None:
    """"Open" is four statuses, not one — and the amber hint depends on it.

    `WORK_ITEM_OPEN_STATUSES` is `("open", "claimed", "in_progress",
    "blocked")`. A bare `status == "open"` counted the first only, so a
    workspace holding nothing but `in_progress` items reported "0 still open"
    — and that hint is the only thing telling somebody to look before
    archiving a workspace out from under live work. The one case it exists
    for was the one case it stayed silent on.

    Asserted against the model's own vocabulary rather than a literal list, so
    a fifth non-terminal status starts counting without anyone editing this
    test. That is the whole reason the tally reads from that tuple.
    """
    from app.models.work_item import (
        WORK_ITEM_OPEN_STATUSES,
        WORK_ITEM_TERMINAL_STATUSES,
        WorkItem,
    )
    from app.services.workspaces import load_workspace_contents

    async with _temp_database() as sessions, sessions() as session:
        user = await _make_user(session, email="tallier@example.com")
        ws = await create_workspace(session, user_id=user.id, name="Busy")
        await session.flush()
        for status in WORK_ITEM_OPEN_STATUSES + WORK_ITEM_TERMINAL_STATUSES:
            session.add(
                WorkItem(
                    id=uuid.uuid4(),
                    user_id=user.id,
                    workspace_id=ws.id,
                    title=f"item {status}",
                    status=status,
                )
            )
        await session.commit()

        counts = (await load_workspace_contents(session, [ws]))[str(ws.id)]
        assert counts["work_items"] == len(WORK_ITEM_OPEN_STATUSES) + len(
            WORK_ITEM_TERMINAL_STATUSES
        )
        assert counts["open_work_items"] == len(WORK_ITEM_OPEN_STATUSES)


@pytest.mark.asyncio
async def test_a_workspace_of_claimed_work_is_not_reported_as_idle() -> None:
    """The exact shape the bug produced, kept as its own case.

    A workspace whose items have all been picked up is the most live one there
    is, and it was the one the hint called empty. Separate from the tally
    above because this is the consequence, and a refactor breaking only this
    should say so in the failure name.
    """
    from app.models.work_item import WorkItem
    from app.services.workspaces import load_workspace_contents

    async with _temp_database() as sessions, sessions() as session:
        user = await _make_user(session, email="claimer@example.com")
        ws = await create_workspace(session, user_id=user.id, name="All claimed")
        await session.flush()
        for n in range(10):
            session.add(
                WorkItem(
                    id=uuid.uuid4(),
                    user_id=user.id,
                    workspace_id=ws.id,
                    title=f"picked up {n}",
                    status="in_progress",
                )
            )
        await session.commit()

        counts = (await load_workspace_contents(session, [ws]))[str(ws.id)]
        assert counts["open_work_items"] == 10, counts


#: Columns whose `server_default` drifts between the migration and
#: `create_all` and that **this stack did not create**, as
#: ``(table, column)``. FROZEN — never add to it to make a change pass.
#:
#: `worksets` predates this stack and twelve of its columns
#: carry a DDL default in their migration and none in the model. That is a real
#: bug with the real consequence: a fresh install gets NOT NULL and no default,
#: so any INSERT skipping the ORM fails there and succeeds on an upgraded
#: database. It is listed rather than fixed because fixing twelve columns of
#: another feature, mid-stack and unreviewed, is a wider change than this PR
#: should carry — and listed rather than ignored because the widened check
#: found it and silence would be the wrong record.
#:
#: The columns THIS stack added to `worksets` (`workspace_id`) are deliberately
#: absent: they are aligned, and they must stay pinned.
_PREEXISTING_DEFAULT_DRIFT: frozenset[tuple[str, str]] = frozenset({
    ("worksets", "auto_calls_used"),
    ("worksets", "content_chars"),
    ("worksets", "evicted_count"),
    ("worksets", "is_company_managed"),
    ("worksets", "item_count"),
    ("worksets", "max_auto_calls"),
    ("worksets", "max_content_chars"),
    ("worksets", "max_items"),
    ("worksets", "processing"),
    ("worksets", "scope"),
    ("worksets", "status"),
    ("worksets", "workers"),
})


@pytest.mark.asyncio
async def test_migrated_schema_matches_create_all_including_indexes() -> None:
    """The migration and the model must agree on more than column names.

    ``test_fresh_install`` compares tables and columns but states outright
    that it "does not verify column types, defaults, indexes, FK constraints,
    or check constraints". For these tables that is the whole point: the
    PARTIAL unique indexes ARE the invariants — one default per owner, one
    slug per owner, one binding per (workspace, knowledge base) — and a
    predicate that drifted between the migration and the model would leave the
    `create_all` path (fresh installs) and the migrated path (upgrades)
    enforcing different rules, with CI green on both.

    ``conversations`` is included for a different reason: its `workspace_id`
    arrives by ALTER on a hot table with a CONCURRENTLY index build, which is
    the shape most likely to end up subtly different from what `create_all`
    produces.

    So this compares the two paths directly: run the real Alembic history on
    one scratch database, ``create_all`` on another, and diff the columns
    (name, type, nullability AND default) plus every index definition.

    The default was added after six columns drifted on it — three reported in
    review, three more found only by running both paths and diffing the
    result. Reading the two files side by side had missed them.
    """
    import subprocess
    import sys

    parsed = urllib.parse.urlparse(_base_url())
    admin_url = parsed._replace(path="/postgres").geturl()
    migrated_name = f"wsparity_m_{uuid.uuid4().hex[:8]}"
    created_name = f"wsparity_c_{uuid.uuid4().hex[:8]}"

    admin_engine = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        async with admin_engine.connect() as conn:
            for name in (migrated_name, created_name):
                await conn.execute(text(f'CREATE DATABASE "{name}"'))
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

    def _url(name: str) -> str:
        return parsed._replace(path=f"/{name}").geturl()

    try:
        # The migrated side, through the real runner. A subprocess because
        # `alembic/env.py` reads the URL from settings at import time.
        for name in (migrated_name, created_name):
            engine = create_async_engine(_url(name))
            async with engine.begin() as conn:
                await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            await engine.dispose()

        proc = subprocess.run(
            [sys.executable, "-m", "alembic", "-c", "alembic/alembic.ini", "upgrade", "head"],
            env={
                **os.environ,
                "DATABASE_URL": _url(migrated_name),
                "SECRET_KEY": os.environ.get("SECRET_KEY", "test-secret"),
            },
            capture_output=True,
            text=True,
        )
        assert proc.returncode == 0, proc.stderr[-4000:]

        engine = create_async_engine(_url(created_name))
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        await engine.dispose()

        async def _schema(name: str) -> tuple[dict, dict, dict]:
            engine = create_async_engine(_url(name))
            async with engine.connect() as conn:
                # `column_default` is in here deliberately. SQLAlchemy never
                # emits a Python-side `default=` as DDL, so a column carrying
                # only one gets NOT NULL and NO default on the `create_all`
                # path while the migrated path has whatever the migration
                # wrote. Any INSERT that skips the ORM then succeeds on an
                # upgraded database and raises a NOT NULL violation on a fresh
                # install — environment-dependent, the hardest kind to chase.
                #
                # Six columns had drifted exactly that way and the comparison
                # above could not see it, because it stopped at name, type and
                # nullability.
                cols = {
                    (r[0], r[1]): (r[2], r[3], r[4])
                    for r in (
                        await conn.execute(
                            text(
                                "SELECT table_name, column_name, data_type, "
                                "is_nullable, column_default "
                                "FROM information_schema.columns "
                                "WHERE table_name = ANY(:tables)"
                            ),
                            {"tables": list(PARITY_TABLES)},
                        )
                    ).all()
                }
                idx = {
                    r[0]: r[1].split(" ON ", 1)[1]
                    for r in (
                        await conn.execute(
                            text(
                                "SELECT indexname, indexdef FROM pg_indexes "
                                "WHERE tablename = ANY(:tables)"
                            ),
                            {"tables": list(PARITY_TABLES)},
                        )
                    ).all()
                }
                # The foreign keys of the workspace tables and the ones that
                # reference `workspaces` (name, target, ON DELETE): the
                # migration adds `conversations.workspace_id`'s in two steps
                # under the name `create_all` gives it.
                fks = {
                    r[0]: r[1]
                    for r in (
                        await conn.execute(
                            text(
                                "SELECT conname, pg_get_constraintdef(oid) "
                                "FROM pg_constraint WHERE contype = 'f' AND ("
                                "conrelid::regclass::text IN "
                                "('workspaces', 'workspace_knowledge_bases') "
                                "OR confrelid::regclass::text = 'workspaces')"
                            )
                        )
                    ).all()
                }
            await engine.dispose()
            return cols, idx, fks

        migrated_cols, migrated_idx, migrated_fks = await _schema(migrated_name)
        created_cols, created_idx, created_fks = await _schema(created_name)
        assert migrated_fks == created_fks
        assert "conversations_workspace_id_fkey" in migrated_fks

        # Named per (table, column) rather than one opaque dict comparison:
        # a bare `==` across every parity table reports "not equal" and
        # leaves the reader diffing two dumps by eye.
        drift = {
            name: (migrated_cols.get(name), created_cols.get(name))
            for name in set(migrated_cols) | set(created_cols)
            if migrated_cols.get(name) != created_cols.get(name)
            and name not in _PREEXISTING_DEFAULT_DRIFT
        }
        assert not drift, f"migration and create_all disagree per column: {drift}"

        # The frozen set must not rot either: an entry that stops drifting has
        # been fixed, and leaving it listed would hide the next real one.
        stale = {
            name
            for name in _PREEXISTING_DEFAULT_DRIFT
            if name in migrated_cols
            and migrated_cols[name] == created_cols.get(name)
        }
        assert not stale, (
            f"these no longer drift — remove them from "
            f"_PREEXISTING_DEFAULT_DRIFT: {sorted(stale)}"
        )
        assert migrated_idx == created_idx
        # Every table in the list was actually reached — a typo'd name would
        # otherwise make this pass by comparing two empty dicts.
        assert {t for t, _ in migrated_cols} == set(PARITY_TABLES)
        # And the invariants really are partial indexes, not plain ones —
        # a plain unique on user_id would forbid a second workspace entirely.
        assert "WHERE" in migrated_idx["uq_workspaces_default_per_user"]
        assert "WHERE" in migrated_idx["uq_workspaces_slug_per_user"]
        # Step 4b's identity split: per-user when unfiled, per-workspace when
        # filed. Both predicates must survive on BOTH paths, and the old
        # per-user CONSTRAINT must be gone from both — a database still
        # carrying it would reject the very sharing the feature promises.
        for table in ("shared_states", "worksets"):
            assert "workspace_id IS NULL" in migrated_idx[
                f"uq_{table}_user_key_unfiled"
            ]
            assert "workspace_id IS NOT NULL" in migrated_idx[
                f"uq_{table}_workspace_key"
            ]
            assert f"uq_{table}_user_key" not in migrated_idx
            assert f"uq_{table}_user_key" not in created_idx

        # CHECK constraints are not in `pg_indexes`, and the workset one IS an
        # invariant: it stops a row being accepted at write time that
        # `_scope_key` would reject at read time. So they are compared across
        # both paths too.
        #
        # DEFINITIONS are compared, with a trailing " NOT VALID" normalised
        # away, and validity is asserted only where the model's promise depends
        # on it. `NOT VALID` is a legitimate, deliberate pattern in this repo,
        # not drift: `e3a9shr0439` adds `ck_conversations_link_access_valid`
        # that way on purpose to skip a full-table scan, documenting that
        # "every existing row already holds the server default... the
        # historical rows this skips validating cannot violate the
        # constraint". Forbidding it here would fail the build on a considered
        # decision. A constraint whose DEFINITION differs between the two
        # paths is still real drift, and that is what this catches.
        def _normalise(definition: str) -> str:
            return definition.removesuffix(" NOT VALID")

        async def _checks(name: str) -> dict[str, tuple[str, bool]]:
            engine = create_async_engine(_url(name))
            async with engine.connect() as conn:
                rows = (
                    await conn.execute(
                        text(
                            "SELECT conname, pg_get_constraintdef(oid), convalidated "
                            "FROM pg_constraint WHERE contype = 'c' "
                            "AND conrelid::regclass::text = ANY(:tables)"
                        ),
                        {"tables": list(PARITY_TABLES)},
                    )
                ).all()
            await engine.dispose()
            return {r[0]: (r[1], r[2]) for r in rows}

        migrated_checks = await _checks(migrated_name)
        created_checks = await _checks(created_name)
        assert {k: _normalise(v[0]) for k, v in migrated_checks.items()} == {
            k: _normalise(v[0]) for k, v in created_checks.items()
        }
        # This one must be VALID on BOTH paths: it guards existing rows too,
        # and the migration validates it in a second step for exactly that
        # reason.
        assert migrated_checks["ck_worksets_workspace_scope"][1] is True
        assert created_checks["ck_worksets_workspace_scope"][1] is True
        # Same for the default that is never company-managed: the
        # migration repairs the legacy rows, then validates it.
        assert migrated_checks["ck_workspaces_default_not_company_managed"][1] is True
        assert created_checks["ck_workspaces_default_not_company_managed"][1] is True
    finally:
        cleanup = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")
        try:
            async with cleanup.connect() as conn:
                for name in (migrated_name, created_name):
                    await conn.execute(
                        text(
                            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                            "WHERE datname = :db AND pid <> pg_backend_pid()"
                        ),
                        {"db": name},
                    )
                    await conn.execute(text(f'DROP DATABASE IF EXISTS "{name}"'))
        finally:
            await cleanup.dispose()
