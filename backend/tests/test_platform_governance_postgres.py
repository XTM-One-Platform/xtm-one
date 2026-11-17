"""An unregistered platform must stop governing its integrations.

Unregistering keeps the row and only sets the status, so a platform that is gone
went on speaking for its provider forever: its tools kept taking a policy nobody
could reach, and the per-integration approval toggle stayed replaced by a 409
pointing at a platform no longer connected. The operator was left with a control
they could not use and a pointer to a screen that could not help them.

Real PostgreSQL because the behaviour is a WHERE clause over enum-ish status
values interacting with the identifier-to-provider fan-out — a fake session
would assert the query we wrote, not the rows it returns.
"""

from __future__ import annotations

import os
import urllib.parse
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, OperationalError, ProgrammingError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.database import Base
from app.models.integration import Integration
from app.models.platform_registration import PlatformRegistration
from app.models.relay import RelayGroup
from app.models.user import User
from app.services.hitl_whitelist import platform_approval_by_provider
from tests.scratch_database import with_referenced_tables

pytestmark = pytest.mark.integration

# PlatformRegistration points at an integration, which points at a user, so the
# FK chain has to exist even though these tests never populate it.
_TABLES = [User.__table__, RelayGroup.__table__, Integration.__table__, PlatformRegistration.__table__]


def _base_url() -> str:
    return os.environ.get(
        "DATABASE_URL",
        "postgresql+asyncpg://copilot:copilot@localhost:5432/copilot",
    ).strip()


@asynccontextmanager
async def _temp_database(prefix: str) -> AsyncIterator[async_sessionmaker]:
    parsed = urllib.parse.urlparse(_base_url())
    admin_url = parsed._replace(path="/postgres").geturl()
    db_name = f"{prefix}_{uuid.uuid4().hex[:12]}"
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
    except Exception as exc:
        pytest.skip(f"Cannot connect to PostgreSQL: {exc}")
    finally:
        await admin_engine.dispose()

    engine = create_async_engine(test_url)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(
                lambda sync_conn: Base.metadata.create_all(sync_conn, tables=with_referenced_tables(_TABLES))
            )
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()
        cleanup = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")
        try:
            async with cleanup.connect() as conn:
                await conn.execute(text(f'DROP DATABASE IF EXISTS "{db_name}"'))
        finally:
            await cleanup.dispose()


def _registration(identifier: str, status: str) -> PlatformRegistration:
    return PlatformRegistration(
        id=uuid.uuid4(),
        platform_identifier=identifier,
        platform_url="https://example.invalid",
        platform_title="Test platform",
        platform_version="1.0.0",
        status=status,
        supports_approval_prompts=False,
        approval_policy="require",
        approval_policy_set_by="default",
    )


@pytest.mark.asyncio
async def test_a_live_platform_governs_its_provider():
    async with _temp_database("hitl_gov") as session, session() as db:
        db.add(_registration("openaev", "active"))
        await db.commit()

        governed, _exempt = await platform_approval_by_provider(db)

        assert governed, "a live registration speaks for its provider"


@pytest.mark.asyncio
async def test_an_unregistered_platform_governs_nothing():
    """The fix: the toggle must become the operator's control again, rather than
    answering 409 with a pointer to a platform that is no longer connected."""
    async with _temp_database("hitl_gov") as session, session() as db:
        reg = _registration("openaev", "active")
        db.add(reg)
        await db.commit()
        governed_before, _ = await platform_approval_by_provider(db)

        reg.status = "unregistered"
        await db.commit()
        governed_after, _ = await platform_approval_by_provider(db)

        assert governed_before
        assert governed_after == frozenset(), (
            "an unregistered platform must release its provider, or the "
            "per-integration toggle stays 409 forever"
        )


@pytest.mark.asyncio
async def test_an_unknown_status_keeps_governing():
    """Deliberate asymmetry: only the one terminal status releases the provider,
    so an unrecognised status errs toward the gate rather than toward the older
    per-integration flag."""
    async with _temp_database("hitl_gov") as session, session() as db:
        db.add(_registration("openaev", "something_new"))
        await db.commit()

        governed, _ = await platform_approval_by_provider(db)

        assert governed, "an unknown status must not silently un-gate"
