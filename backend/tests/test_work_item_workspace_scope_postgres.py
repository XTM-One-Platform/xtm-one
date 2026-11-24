"""Work items shared per workspace.

`work_items` was the primitive whose absence of scoping invalidated the
spec in the first place, and it stayed the odd one out through 4a and 4b: it
gained a `workspace_id` column that nothing read. So the acceptance criterion
*"two agents pointed at one workspace read each other's shared state, worksets
AND work items"* was false while the other two were true.

**One thing makes this table different from the other two, and it raises the
stakes on every assertion below.** `shared_states` and `worksets` namespace
their key (`workspace:<id>:<key>`), so a query that forgets to filter on the
column still misses. A work item has NO key — it is addressed by id, owner and
correlation key — so the column and the read filter are the ONLY things
isolating it. There is no second mechanism behind them, which is why the tests
here cover every entry path rather than a representative one.
"""

from __future__ import annotations

import os
import urllib.parse
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError, OperationalError, ProgrammingError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.models  # noqa: F401 — registers every mapper
from app.core.database import Base
from app.models.user import User
from app.models.work_item import WorkItem
from app.services.company_managed_writes import agent_writer, person_writer
from app.services.workspaces import create_workspace

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
    db_name = f"wiscope_{uuid.uuid4().hex[:10]}"
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
                # A session opened after the terminate (a task the code under
                # test spawned) would make a plain DROP fail: FORCE closes it.
                await conn.execute(text(f'DROP DATABASE IF EXISTS "{db_name}" WITH (FORCE)'))
        finally:
            await cleanup.dispose()


async def _make_agent(session, *, name: str = "Dispatcher"):
    """A real agent row: `work_items.owner_agent_id` is a foreign key."""
    from app.models.agent import Agent

    agent = Agent(id=uuid.uuid4(), name=name)
    session.add(agent)
    await session.flush()
    return agent


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


@asynccontextmanager
async def _pointed_at_db(sessions):
    """Point `async_session` at the scratch database.

    The work-item service opens its OWN session per operation (deliberately:
    the agentic loop holds one across a whole turn), so a test that only had a
    session would exercise nothing.
    """
    from app.core import database as db_module

    original = db_module.async_session
    db_module.async_session = sessions
    try:
        yield
    finally:
        db_module.async_session = original


# ── Every read path honours the tier ────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_workspace_item_is_visible_to_everyone_who_can_see_it() -> None:
    """The criterion that was false before this step."""
    from app.services.work_item import get_work_item, list_work_items

    async with _temp_database() as sessions:
        async with sessions() as session:
            alice = await _make_user(session, email="alice@example.com")
            bob = await _make_user(session, email="bob@example.com")
            ws = await create_workspace(session, user_id=alice.id, name="Shared")
            _share(ws, bob, "read_only")
            await session.flush()
            item = WorkItem(
                id=uuid.uuid4(),
                user_id=alice.id,
                title="Cross-check the OCTI reports",
                workspace_id=ws.id,
            )
            session.add(item)
            await session.commit()
            item_id, bob_id = str(item.id), str(bob.id)

        async with _pointed_at_db(sessions):
            # Bob did not write it and does not own the workspace, but he can
            # SEE the workspace — so the item is his to read.
            assert await get_work_item(item_id, user_id=bob_id) is not None
            titles = [
                i["title"] for i in await list_work_items(user_id=bob_id)
            ]
            assert "Cross-check the OCTI reports" in titles


@pytest.mark.asyncio
async def test_an_unfiled_probe_ignores_a_readable_workspaces_item() -> None:
    """A flow's create action dedupes where it creates (unfiled): an open item
    with the same key in a workspace its owner can read is not its own."""
    from app.services.work_item import list_work_items

    async with _temp_database() as sessions:
        async with sessions() as session:
            alice = await _make_user(session, email="alice2@example.com")
            ws = await create_workspace(session, user_id=alice.id, name="Other")
            await session.flush()
            session.add(
                WorkItem(
                    id=uuid.uuid4(),
                    user_id=alice.id,
                    title="Filed elsewhere",
                    correlation_key="order-42",
                    workspace_id=ws.id,
                )
            )
            await session.commit()
            alice_id = str(alice.id)

        async with _pointed_at_db(sessions):
            probe = dict(user_id=alice_id, correlation_key="order-42", open_only=True)
            assert len(await list_work_items(**probe)) == 1
            assert await list_work_items(**probe, unfiled_only=True) == []


@pytest.mark.asyncio
async def test_a_stranger_sees_nothing() -> None:
    from app.services.work_item import get_work_item, list_work_items

    async with _temp_database() as sessions:
        async with sessions() as session:
            alice = await _make_user(session, email="alice@example.com")
            carol = await _make_user(session, email="carol@example.com")
            ws = await create_workspace(session, user_id=alice.id, name="Private")
            await session.flush()
            item = WorkItem(
                id=uuid.uuid4(), user_id=alice.id, title="Secret", workspace_id=ws.id
            )
            session.add(item)
            await session.commit()
            item_id, carol_id = str(item.id), str(carol.id)

        async with _pointed_at_db(sessions):
            assert await get_work_item(item_id, user_id=carol_id) is None
            assert await list_work_items(user_id=carol_id) == []


@pytest.mark.asyncio
async def test_the_author_loses_a_filed_item_when_they_lose_the_workspace() -> None:
    """Authorship is provenance, not a standing grant.

    Alice wrote it, but it is filed into a workspace she can no longer see —
    so she cannot read it either. Without this the own-row tier would be a
    back door around every workspace permission.
    """
    from app.services.work_item import list_work_items

    async with _temp_database() as sessions:
        async with sessions() as session:
            alice = await _make_user(session, email="alice@example.com")
            bob = await _make_user(session, email="bob@example.com")
            # Bob owns it; Alice merely wrote the row and has no grant.
            ws = await create_workspace(session, user_id=bob.id, name="Bob's")
            await session.flush()
            session.add(
                WorkItem(
                    id=uuid.uuid4(),
                    user_id=alice.id,
                    title="Written by Alice",
                    workspace_id=ws.id,
                )
            )
            await session.commit()
            alice_id = str(alice.id)

        async with _pointed_at_db(sessions):
            assert await list_work_items(user_id=alice_id) == []


@pytest.mark.asyncio
async def test_unfiled_items_keep_their_per_author_visibility() -> None:
    """The old behaviour must survive untouched for every existing row."""
    from app.services.work_item import list_work_items

    async with _temp_database() as sessions:
        async with sessions() as session:
            alice = await _make_user(session, email="alice@example.com")
            bob = await _make_user(session, email="bob@example.com")
            session.add(
                WorkItem(id=uuid.uuid4(), user_id=alice.id, title="Alice's own")
            )
            await session.commit()
            alice_id, bob_id = str(alice.id), str(bob.id)

        async with _pointed_at_db(sessions):
            assert [i["title"] for i in await list_work_items(user_id=alice_id)] == [
                "Alice's own"
            ]
            assert await list_work_items(user_id=bob_id) == []


@pytest.mark.asyncio
async def test_the_autonomous_dispatch_path_honours_the_tier_too() -> None:
    """The scheduler reads through the same predicate.

    Easy to miss because it is the one read path with no human behind it — and
    the one that would silently hand another workspace's backlog to an agent.
    """
    from app.services.work_item import list_dispatchable_work_items

    async with _temp_database() as sessions:
        async with sessions() as session:
            alice = await _make_user(session, email="alice@example.com")
            carol = await _make_user(session, email="carol@example.com")
            ws = await create_workspace(session, user_id=alice.id, name="Private")
            await session.flush()
            agent_id = (await _make_agent(session)).id
            session.add(
                WorkItem(
                    id=uuid.uuid4(),
                    user_id=alice.id,
                    title="Queued",
                    status="open",
                    owner_agent_id=agent_id,
                    workspace_id=ws.id,
                )
            )
            await session.commit()
            alice_id, carol_id = str(alice.id), str(carol.id)

        async with _pointed_at_db(sessions):
            mine = await list_dispatchable_work_items(
                owner_agent_id=str(agent_id), user_id=alice_id, writer=_PERSON
            )
            assert [i["title"] for i in mine] == ["Queued"]
            theirs = await list_dispatchable_work_items(
                owner_agent_id=str(agent_id), user_id=carol_id, writer=_PERSON
            )
            assert theirs == [], "the dispatcher must not cross a workspace"


# ── The write gate ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_read_only_grantee_cannot_create_work_in_the_workspace() -> None:
    """Queueing work is steering other people's agents.

    A `read_only` grantee who could create items would be putting tasks into a
    backlog that everyone else's agents pick up and act on — no permission
    gained on paper.
    """
    from app.services.shared_state import WorkspaceWriteRefusedError
    from app.services.work_item import create_work_item

    async with _temp_database() as sessions:
        async with sessions() as session:
            alice = await _make_user(session, email="alice@example.com")
            bob = await _make_user(session, email="bob@example.com")
            ws = await create_workspace(session, user_id=alice.id, name="Shared")
            _share(ws, bob, "read_only")
            await session.commit()
            ws_id, bob_id, alice_id = ws.id, str(bob.id), str(alice.id)

        async with _pointed_at_db(sessions):
            with pytest.raises(WorkspaceWriteRefusedError):
                await create_work_item(
                    user_id=bob_id, title="Injected", workspace_id=ws_id,
                    writer=_PERSON,
                )
            # The owner can, and the row carries the workspace.
            created = await create_work_item(
                user_id=alice_id, title="Legitimate", workspace_id=ws_id,
                writer=_PERSON,
            )
            assert created["title"] == "Legitimate"

        async with sessions() as session:
            rows = (
                (await session.execute(select(WorkItem))).scalars().all()
            )
            assert [r.title for r in rows] == ["Legitimate"]
            assert rows[0].workspace_id == ws_id


@pytest.mark.asyncio
async def test_a_config_grantee_can_create() -> None:
    from app.services.work_item import create_work_item

    async with _temp_database() as sessions:
        async with sessions() as session:
            alice = await _make_user(session, email="alice@example.com")
            bob = await _make_user(session, email="bob@example.com")
            ws = await create_workspace(session, user_id=alice.id, name="Shared")
            _share(ws, bob, "config")
            await session.commit()
            ws_id, bob_id = ws.id, str(bob.id)

        async with _pointed_at_db(sessions):
            created = await create_work_item(
                user_id=bob_id, title="From a collaborator", workspace_id=ws_id,
                writer=_PERSON,
            )
            assert created["title"] == "From a collaborator"


@pytest.mark.asyncio
async def test_deleting_needs_the_config_tier_not_mere_visibility() -> None:
    """Removing tracked work is the same escalation as creating it."""
    from app.services.work_item import delete_work_item

    async with _temp_database() as sessions:
        async with sessions() as session:
            alice = await _make_user(session, email="alice@example.com")
            bob = await _make_user(session, email="bob@example.com")
            ws = await create_workspace(session, user_id=alice.id, name="Shared")
            _share(ws, bob, "read_only")
            await session.flush()
            item = WorkItem(
                id=uuid.uuid4(), user_id=alice.id, title="Tracked", workspace_id=ws.id
            )
            session.add(item)
            await session.commit()
            item_id, bob_id = str(item.id), str(bob.id)

        async with _pointed_at_db(sessions):
            # Bob can SEE it (read tier) but must not be able to delete it.
            assert await delete_work_item(item_id, user_id=bob_id, writer=_PERSON) is False

        async with sessions() as session:
            assert await session.get(WorkItem, uuid.UUID(item_id)) is not None


# ── The fleet-wide queue: visible, dispatchable, and mutable ────────────────


@pytest.mark.asyncio
async def test_a_company_managed_item_can_be_claimed_completed_and_released() -> None:
    """The live-lock this gate introduced.

    A company-managed item is the normal product of a company-managed agent:
    `user_id = NULL`, `workspace_id = NULL`. It stays VISIBLE (tier 0 of the
    read clause) and DISPATCHABLE, but the write gate had no company tier, so
    it matched nothing and could be claimed by nobody — not even an admin,
    since the gate hardcodes `is_admin=False`.

    That is a live-lock, not a permission error: the dispatcher hands the item
    out, the claim misses, the row never leaves `open`, and the next sweep
    dispatches it again — until `reap_work_items` auto-cancels it days later
    and fleet work silently disappears.
    """
    from app.services.work_item import (
        WORK_ITEM_TERMINAL_STATUSES,
        claim_work_item,
        complete_work_item,
        create_work_item,
        list_dispatchable_work_items,
        release_work_item_claim,
    )

    async with _temp_database() as sessions:
        async with sessions() as session:
            carol = await _make_user(session, email="carol@example.com")
            # `owner_agent_id` is a real foreign key, and the dispatcher
            # resolves it through `safe_uuid` — a non-UUID short-circuits the
            # query to [] and would make this test pass for the wrong reason.
            agent = await _make_agent(session)
            await session.commit()
            carol_id, agent_id = str(carol.id), str(agent.id)

        async with _pointed_at_db(sessions):
            fleet = agent_writer(is_company_managed=True, agent_id=agent_id)
            created = await create_work_item(
                title="Fleet backlog",
                user_id=None,
                owner_agent_id=agent_id,
                is_company_managed=True,
                writer=fleet,
            )
            item_id = created["id"]

            dispatchable = await list_dispatchable_work_items(
                owner_agent_id=agent_id, user_id=carol_id, writer=fleet
            )
            assert item_id in [i["id"] for i in dispatchable], (
                "the dispatcher must still hand it out"
            )

            claimed = await claim_work_item(
                item_id, user_id=carol_id, agent_id=agent_id, writer=fleet
            )
            assert claimed.get("claimed") is True, (
                f"the dispatcher offered it and the claim missed: {claimed}"
            )

            released = await release_work_item_claim(
                work_item_id=item_id, user_id=carol_id, writer=fleet
            )
            assert released is not None, "a claimed fleet item must be releasable"
            assert released["claimed_by_agent_id"] is None
            assert released["status"] == "open", "and must return to the queue"

            await claim_work_item(
                item_id, user_id=carol_id, agent_id=agent_id, writer=fleet
            )
            done = await complete_work_item(item_id, user_id=carol_id, writer=fleet)
            assert done is not None, "a fleet item must be completable"
            assert done["status"] in WORK_ITEM_TERMINAL_STATUSES


@pytest.mark.asyncio
async def test_filing_a_company_item_into_a_workspace_still_overrides_the_fleet_tier() -> None:
    """The company tier is for UNFILED rows only.

    Filing hands the row to the workspace's own rule, which is what the read
    clause already does — otherwise `is_company_managed` would be a permanent
    bypass of every workspace grant.
    """
    from app.services.work_item import claim_work_item

    async with _temp_database() as sessions:
        async with sessions() as session:
            alice = await _make_user(session, email="alice@example.com")
            bob = await _make_user(session, email="bob@example.com")
            ws = await create_workspace(session, user_id=alice.id, name="Shared")
            _share(ws, bob, "read_only")
            await session.flush()
            agent = await _make_agent(session)
            item = WorkItem(
                id=uuid.uuid4(),
                user_id=None,
                is_company_managed=True,
                owner_agent_id=agent.id,
                title="Filed fleet work",
                workspace_id=ws.id,
            )
            session.add(item)
            await session.commit()
            item_id, bob_id, agent_id = str(item.id), str(bob.id), str(agent.id)

        async with _pointed_at_db(sessions):
            refused = await claim_work_item(
                item_id,
                user_id=bob_id,
                agent_id=agent_id,
                writer=agent_writer(is_company_managed=True, agent_id=agent_id),
            )
            assert refused.get("claimed") is not True, (
                "a read_only grantee must not steer a filed backlog"
            )


@pytest.mark.asyncio
async def test_a_filed_company_item_is_the_workspaces_to_read_and_dispatch() -> None:
    """Filing hands a company-managed item to the workspace for reads too.

    Readable by the whole fleet, it leaked to users with no grant on the
    workspace; dispatched off the READ tier, it was offered on every tick to
    a caller who could never claim it, crowding the rest of the queue out.
    """
    from app.services.work_item import get_work_item, list_dispatchable_work_items

    async with _temp_database() as sessions:
        async with sessions() as session:
            alice = await _make_user(session, email="alice@example.com")
            bob = await _make_user(session, email="bob@example.com")
            eve = await _make_user(session, email="eve@example.com")
            ws = await create_workspace(session, user_id=alice.id, name="Shared")
            _share(ws, bob, "read_only")
            await session.flush()
            agent = await _make_agent(session)
            item = WorkItem(
                id=uuid.uuid4(),
                user_id=None,
                is_company_managed=True,
                owner_agent_id=agent.id,
                title="Filed fleet work",
                status="open",
                workspace_id=ws.id,
            )
            session.add(item)
            await session.commit()
            item_id, agent_id = str(item.id), str(agent.id)
            alice_id, bob_id, eve_id = str(alice.id), str(bob.id), str(eve.id)

        fleet = agent_writer(is_company_managed=True, agent_id=agent_id)
        async with _pointed_at_db(sessions):
            assert await get_work_item(item_id, user_id=eve_id) is None
            assert await get_work_item(item_id, user_id=bob_id) is not None

            offered = await list_dispatchable_work_items(
                owner_agent_id=agent_id, user_id=bob_id, writer=fleet
            )
            assert offered == []
            owners = await list_dispatchable_work_items(
                owner_agent_id=agent_id, user_id=alice_id, writer=fleet
            )
            assert [i["id"] for i in owners] == [item_id]


# ── Idempotency belongs to the destination, not to the reader ──────────────


@pytest.mark.asyncio
async def test_a_correlation_key_is_found_by_a_caller_who_cannot_see_its_workspace() -> None:
    """The duplicate the caller-scoped probe created.

    Alice files `octi-report-42` into workspace W. A scheduled sweep runs as
    Bob, who has no grant on W. With the probe filtered by Bob's readable
    tier the answer came back empty and the materialiser minted a SECOND row
    for the same key — two agents on the same report, and `claim_work_item`
    cannot prevent it because they are distinct rows with distinct ids.

    Asked within the destination instead, the key is found. Bob learns only
    about the workspace he is writing into, so nothing cross-workspace is
    disclosed either.
    """
    from app.services.work_item import existing_correlation_keys

    async with _temp_database() as sessions:
        async with sessions() as session:
            alice = await _make_user(session, email="alice@example.com")
            bob = await _make_user(session, email="bob@example.com")
            ws = await create_workspace(session, user_id=alice.id, name="Alice only")
            await session.flush()
            session.add(
                WorkItem(
                    id=uuid.uuid4(),
                    user_id=alice.id,
                    title="Report 42",
                    workspace_id=ws.id,
                    correlation_key="octi-report-42",
                )
            )
            await session.commit()
            bob_id, ws_id = str(bob.id), ws.id

        async with _pointed_at_db(sessions):
            found = await existing_correlation_keys(
                user_id=bob_id, keys=["octi-report-42"], workspace_id=ws_id
            )
            assert found == {"octi-report-42"}, (
                "a caller without a grant on the destination still has to see "
                "that the key is taken, or it files a duplicate"
            )

            # A DIFFERENT destination is a different question, and the answer
            # there is genuinely "not taken" — the key is free to be used.
            assert await existing_correlation_keys(
                user_id=bob_id, keys=["octi-report-42"], workspace_id=None
            ) == set()


@pytest.mark.asyncio
async def test_the_unfiled_probe_does_not_answer_for_filed_rows() -> None:
    """The default destination is the unfiled space, and it is a real scope —
    not "everything". The Action Register materialises unfiled, so a key taken
    inside some workspace must not block it."""
    from app.services.work_item import existing_correlation_keys

    async with _temp_database() as sessions:
        async with sessions() as session:
            alice = await _make_user(session, email="alice@example.com")
            ws = await create_workspace(session, user_id=alice.id, name="Filed")
            await session.flush()
            session.add(
                WorkItem(
                    id=uuid.uuid4(), user_id=alice.id, title="Filed",
                    workspace_id=ws.id, correlation_key="shared-key",
                )
            )
            session.add(
                WorkItem(
                    id=uuid.uuid4(), user_id=alice.id, title="Unfiled",
                    workspace_id=None, correlation_key="unfiled-key",
                )
            )
            await session.commit()
            alice_id = str(alice.id)

        async with _pointed_at_db(sessions):
            found = await existing_correlation_keys(
                user_id=alice_id, keys=["shared-key", "unfiled-key"]
            )
            assert found == {"unfiled-key"}


@pytest.mark.asyncio
async def test_two_materialisation_passes_as_two_users_produce_one_item() -> None:
    """The end-to-end property the destination-scoped probe delivers.

    The Action Register materialises UNFILED, and an unfiled item belonging to
    Alice is not visible to Bob either — so the caller-scoped probe created a
    duplicate between two users on the register's own path, not only across
    workspaces. Two passes over the same register must converge on one item
    however many different people run them.
    """
    from app.services.action_register import materialize_action_register

    register = (
        "## Action Register\n"
        "| Action | Owner | Status | Due |\n"
        "|--------|-------|--------|-----|\n"
        "| Patch CVE-2026-1234 | Dispatcher | open | |\n"
    )

    async with _temp_database() as sessions:
        async with sessions() as session:
            alice = await _make_user(session, email="alice@example.com")
            bob = await _make_user(session, email="bob@example.com")
            # Company-managed so BOTH users resolve the owner by name.
            agent = await _make_agent(session, name="Dispatcher")
            agent.is_company_managed = True
            agent.user_id = None
            await session.commit()
            alice_id, bob_id = str(alice.id), str(bob.id)

        async with _pointed_at_db(sessions):
            first = await materialize_action_register(
                text=register,
                user_id=alice_id,
                is_company_managed=False,
                correlation_prefix="run-42",
                writer=_PERSON,
            )
            second = await materialize_action_register(
                text=register,
                user_id=bob_id,
                is_company_managed=False,
                correlation_prefix="run-42",
                writer=_PERSON,
            )

        assert first == 1, "the first pass materialises the row"
        assert second == 0, (
            "the second pass, run by someone who cannot SEE the first item, "
            "must still recognise the key as taken"
        )

        async with sessions() as session:
            rows = (
                await session.execute(
                    select(WorkItem).where(WorkItem.correlation_key.isnot(None))
                )
            ).scalars().all()
            assert len(rows) == 1, f"one register row, one item — got {len(rows)}"
