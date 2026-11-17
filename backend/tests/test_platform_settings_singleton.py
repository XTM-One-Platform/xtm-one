"""Regression tests for the ``platform_settings`` singleton race.

``platform_settings`` must hold exactly one row — every reader does
``SELECT ... LIMIT 1`` — but that invariant used to be enforced only in
application code via a check-then-insert in ``get_platform_settings()``:
``SELECT`` returns ``None`` -> ``INSERT``. Two requests racing that check
(observed in the wild during a ``--reload`` restart) could both see no row
and both insert one. After that, every ``SELECT ... LIMIT 1`` call site
non-deterministically returned either row, so a write made through one
request could appear to "disappear" whenever a later read landed on the
other row — this is what made clicking a model tile in Settings to set it
as the platform default look like it silently did nothing, or reverted to
a previously-clicked model.

These tests run against a dedicated, disposable Postgres database (same
isolation pattern as ``test_fresh_install.py``) because the bug is a
genuine DB-level race: it cannot be observed against a mocked session.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.models as _models  # noqa: F401 — side-effect: register all models
from app.api.v1.platform_settings_helpers import get_platform_settings
from app.models.platform_settings import PlatformSettings
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration


@contextlib.asynccontextmanager
async def _temporary_database():
    """Create a dedicated, empty Postgres database with our schema, drop on exit.

    An unreachable server fails the test rather than skipping it.
    """
    async with scratch_database(
        "copilot_ps_singleton",
        tables=ALL_TABLES,
        extensions=("vector",),
        skip_when_unreachable=False,
    ) as db:
        yield db.url


@pytest.fixture
async def empty_db_url():
    async with _temporary_database() as url:
        yield url


async def test_second_platform_settings_row_is_rejected(empty_db_url):
    """The DB-level singleton index must reject a second row outright.

    This is the constraint that makes the race in ``get_platform_settings``
    safe to recover from — without it, two concurrent creators would both
    succeed and silently corrupt the "singleton".
    """
    engine = create_async_engine(empty_db_url)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    async with session_factory() as session:
        session.add(PlatformSettings())
        await session.commit()

    async with session_factory() as session:
        session.add(PlatformSettings())
        with pytest.raises(IntegrityError):
            await session.flush()

    await engine.dispose()


async def test_get_platform_settings_survives_concurrent_create_race(empty_db_url):
    """Simulate N requests hitting an empty table at the same time.

    Before the fix, every one of these would see no row and insert its own,
    leaving the table with N rows and every future read non-deterministic
    about which one it gets. After the fix, exactly one row must survive and
    every caller — including the ones that lost the race — must end up
    pointing at that same row instead of raising or silently duplicating.
    """
    engine = create_async_engine(empty_db_url)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    async def _one_request() -> uuid.UUID:
        async with session_factory() as session:
            ps = await get_platform_settings(session)
            await session.commit()
            return ps.id

    concurrency = 16
    results = await asyncio.gather(*(_one_request() for _ in range(concurrency)))

    # Every concurrent caller must agree on the same row.
    assert len(set(results)) == 1, (
        f"Expected all {concurrency} concurrent get_platform_settings() calls to "
        f"resolve to the same row; got distinct ids: {set(results)}"
    )

    async with session_factory() as session:
        count = (await session.execute(text("SELECT count(*) FROM platform_settings"))).scalar_one()
    assert count == 1, f"Expected exactly one platform_settings row, found {count}"

    await engine.dispose()
