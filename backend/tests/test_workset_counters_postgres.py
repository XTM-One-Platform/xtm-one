"""Workset counters under concurrent writers, against a real PostgreSQL.

``item_count``, ``evicted_count`` and ``content_chars`` are what Operations >
Worksets shows and what the eviction plan checks ``max_items`` against. Two
writers to one workset must not lose each other's updates, and the
reconciliation migration must bring drifted counters back to what the items
say.
"""

from __future__ import annotations

import asyncio
import importlib.util
import pathlib
import uuid
from types import ModuleType

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import app.models  # noqa: F401 — registers every mapper on Base.metadata
from app.models.user import User
from app.models.workset import Workset, WorksetItem
from app.services.workset import AddOutcome, add_item, resolve_workset
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration

_MIGRATION = next((pathlib.Path(__file__).resolve().parents[1] / "alembic/versions").glob("w12165wscnt1_*.py"))


async def _seed(
    session_factory: async_sessionmaker[AsyncSession], *, max_items: int = 500
) -> tuple[uuid.UUID, uuid.UUID]:
    async with session_factory() as session:
        user = User(
            id=uuid.uuid4(),
            email=f"ws-{uuid.uuid4().hex[:8]}@test.local",
            full_name="Workset Counters",
            hashed_password="x",
        )
        session.add(user)
        await session.flush()
        ws = await resolve_workset(session, user, key="reports", scope="global", max_items=max_items)
        await session.commit()
        return user.id, ws.id


async def _loaded(session: AsyncSession, user_id: uuid.UUID, workset_id: uuid.UUID) -> tuple[User, Workset]:
    user = await session.get(User, user_id)
    ws = await session.get(Workset, workset_id)
    assert user is not None and ws is not None
    return user, ws


async def _concurrent_adds(
    session_factory: async_sessionmaker[AsyncSession],
    user_id: uuid.UUID,
    workset_id: uuid.UUID,
    count: int,
) -> list[AddOutcome]:
    """*count* adds, each in its own session and transaction, all at once."""
    ready = asyncio.Event()

    async def _one(index: int) -> AddOutcome:
        async with session_factory() as session:
            # Every writer holds the row as loaded BEFORE the others commit:
            # the stale read the lock exists for.
            user, ws = await _loaded(session, user_id, workset_id)
            await ready.wait()
            outcome = await add_item(
                session,
                ws,
                user,
                title=f"item-{index}",
                content="C" * (10 + index),
                external_ref=f"item-{index}",
            )
            await session.commit()
            return outcome

    tasks = [asyncio.create_task(_one(index)) for index in range(count)]
    await asyncio.sleep(0.2)
    ready.set()
    return list(await asyncio.gather(*tasks))


async def _truth(session: AsyncSession, workset_id: uuid.UUID) -> tuple[int, int, int]:
    live = (WorksetItem.workset_id == workset_id, WorksetItem.status != "evicted")
    items = await session.scalar(select(func.count()).where(*live))
    chars = await session.scalar(select(func.coalesce(func.sum(WorksetItem.content_chars), 0)).where(*live))
    evicted = await session.scalar(
        select(func.count()).where(WorksetItem.workset_id == workset_id, WorksetItem.status == "evicted")
    )
    return int(items or 0), int(evicted or 0), int(chars or 0)


async def test_concurrent_adds_keep_every_counter_update() -> None:
    async with scratch_database("workset", tables=ALL_TABLES, extensions=("vector",)) as db:
        user_id, workset_id = await _seed(db.sessions)

        outcomes = await _concurrent_adds(db.sessions, user_id, workset_id, 8)
        assert all(outcome.ok for outcome in outcomes), [o.error for o in outcomes]

        async with db.sessions() as check:
            ws = await check.get(Workset, workset_id)
            items, evicted, chars = await _truth(check, workset_id)
            assert items == 8
            assert (ws.item_count, ws.evicted_count, ws.content_chars) == (
                items,
                evicted,
                chars,
            )
            assert chars == sum(10 + index for index in range(8))


async def test_concurrent_adds_past_the_cap_never_exceed_it() -> None:
    async with scratch_database("workset", tables=ALL_TABLES, extensions=("vector",)) as db:
        user_id, workset_id = await _seed(db.sessions, max_items=3)

        outcomes = await _concurrent_adds(db.sessions, user_id, workset_id, 8)
        assert all(outcome.ok for outcome in outcomes), [o.error for o in outcomes]

        async with db.sessions() as check:
            ws = await check.get(Workset, workset_id)
            items, evicted, chars = await _truth(check, workset_id)
            assert items == 3
            assert evicted == 5
            assert (ws.item_count, ws.evicted_count, ws.content_chars) == (
                items,
                evicted,
                chars,
            )
            assert sum(len(outcome.evicted) for outcome in outcomes) == evicted


async def test_a_correction_of_an_evicted_item_counts_it_live_again() -> None:
    async with scratch_database("workset", tables=ALL_TABLES, extensions=("vector",)) as db:
        user_id, workset_id = await _seed(db.sessions, max_items=1)
        async with db.sessions() as session:
            user, ws = await _loaded(session, user_id, workset_id)
            await add_item(session, ws, user, title="a", content="A" * 10, external_ref="a")
            await add_item(session, ws, user, title="b", content="B" * 20, external_ref="b")
            await session.commit()
            assert (ws.item_count, ws.evicted_count) == (1, 1)

            # "a" was evicted; new text for it makes room by evicting "b".
            outcome = await add_item(session, ws, user, title="a", content="A2" * 15, external_ref="a")
            await session.commit()
            assert outcome.ok, outcome.error
            assert outcome.action == "revised"

            items, evicted, chars = await _truth(session, workset_id)
            await session.refresh(ws)
            assert (items, evicted, chars) == (1, 1, 30)
            assert (ws.item_count, ws.evicted_count, ws.content_chars) == (1, 1, 30)


def _load_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location(_MIGRATION.stem, _MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def test_the_reconciliation_recomputes_drifted_counters() -> None:
    migration = _load_migration()
    async with scratch_database("workset", tables=ALL_TABLES, extensions=("vector",)) as db:
        user_id, drifted_id = await _seed(db.sessions, max_items=2)
        async with db.sessions() as session:
            user, ws = await _loaded(session, user_id, drifted_id)
            for index in range(3):
                await add_item(
                    session,
                    ws,
                    user,
                    title=f"i{index}",
                    content="D" * 7,
                    external_ref=f"i{index}",
                )
            empty = await resolve_workset(session, user, key="empty", scope="global")
            empty_id = empty.id
            await session.commit()
            # What lost updates leave behind.
            await session.execute(
                update(Workset)
                .where(Workset.id.in_([drifted_id, empty_id]))
                .values(item_count=500, evicted_count=364, content_chars=9)
            )
            await session.commit()

        engine = db.sessions.kw["bind"]
        async with engine.connect() as conn:
            corrected = await conn.run_sync(lambda sync: migration.reconcile_in_batches(sync, batch_size=1))
            await conn.commit()
        assert corrected == 2

        async with db.sessions() as check:
            _user, drifted = await _loaded(check, user_id, drifted_id)
            assert (drifted.item_count, drifted.evicted_count, drifted.content_chars) == (
                2,
                1,
                14,
            )
            assert (drifted.item_count, drifted.evicted_count, drifted.content_chars) == (
                await _truth(check, drifted_id)
            )
            _user, blank = await _loaded(check, user_id, empty_id)
            assert (blank.item_count, blank.evicted_count, blank.content_chars) == (0, 0, 0)

        # Idempotent: counters that agree are not written again.
        async with engine.connect() as conn:
            again = await conn.run_sync(lambda sync: migration.reconcile_in_batches(sync, batch_size=1))
            await conn.commit()
        assert again == 0
