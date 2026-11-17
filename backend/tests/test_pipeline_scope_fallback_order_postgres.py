"""The pipeline scope fallback picks the same integration on a real database.

Same scratch-database recipe as ``test_store_message_sequence_postgres.py``.
A real database because what is under test is the ``ORDER BY``: rows are
written newest first, so a query without it would hand them back in write
order and pick the newest; with it the oldest wins, and ``id`` decides
between rows created at the same instant.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

import app.models  # noqa: F401 - registers every mapper on Base.metadata
from app.models.integration import Integration
from app.models.user import User
from app.services import agent_chat
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration

REF = {"source": "integration", "provider": "opencti", "tool_name": "search"}
T0 = datetime(2026, 1, 1, tzinfo=UTC)


@asynccontextmanager
async def _temp_database() -> AsyncIterator[async_sessionmaker]:
    async with scratch_database(
        "pipeorder",
        tables=ALL_TABLES,
        extensions=("vector",),
        bind_app_sessions=False,
    ) as db:
        yield db.sessions


def _user(label: str) -> User:
    return User(
        id=uuid.uuid4(),
        email=f"{label}-{uuid.uuid4().hex[:8]}@test.local",
        full_name=label,
        hashed_password="x",
    )


def _integration(
    owner: User, *, created_at: datetime, company: bool, integ_id: uuid.UUID | None = None
) -> Integration:
    return Integration(
        id=integ_id or uuid.uuid4(),
        user_id=owner.id,
        provider="opencti",
        name="OpenCTI",
        auth_type="api_key",
        is_company_managed=company,
        status="connected",
        created_at=created_at,
    )


async def _seed(sessionmaker, *rows) -> None:
    """Write *rows* one commit at a time, in the order given."""
    for row in rows:
        async with sessionmaker() as session:
            session.add(row)
            await session.commit()


async def _pick(sessionmaker, acting: User) -> str:
    build = AsyncMock(return_value=([SimpleNamespace(name="search")], object()))
    agent = SimpleNamespace(name="Pipeline", id=uuid.uuid4())
    with patch("app.services.integration_tools.build_integration_tools", build):
        async with sessionmaker() as session:
            loaded = await agent_chat._load_pipeline_tool_standalone(
                session, REF, str(acting.id), agent, None
            )
    assert loaded is not None
    return build.await_args.args[1]["opencti"][0]


async def test_oldest_company_wide_row_wins_whatever_the_write_order():
    async with _temp_database() as sessionmaker:
        admin, acting = _user("admin"), _user("acting")
        await _seed(sessionmaker, admin, acting)
        newest = _integration(admin, created_at=T0 + timedelta(days=2), company=True)
        middle = _integration(admin, created_at=T0 + timedelta(days=1), company=True)
        oldest = _integration(admin, created_at=T0, company=True)
        await _seed(sessionmaker, newest, middle, oldest)

        picks = {await _pick(sessionmaker, acting) for _ in range(3)}
        assert picks == {str(oldest.id)}


async def test_id_breaks_a_timestamp_tie():
    async with _temp_database() as sessionmaker:
        admin, acting = _user("admin"), _user("acting")
        await _seed(sessionmaker, admin, acting)
        low, high = sorted([uuid.uuid4(), uuid.uuid4()])
        await _seed(
            sessionmaker,
            _integration(admin, created_at=T0, company=True, integ_id=high),
            _integration(admin, created_at=T0, company=True, integ_id=low),
        )

        assert await _pick(sessionmaker, acting) == str(low)


async def test_the_callers_own_row_still_wins_over_older_company_rows():
    async with _temp_database() as sessionmaker:
        admin, acting = _user("admin"), _user("acting")
        await _seed(sessionmaker, admin, acting)
        own = _integration(acting, created_at=T0 + timedelta(days=5), company=False)
        await _seed(
            sessionmaker,
            _integration(admin, created_at=T0, company=True),
            own,
        )

        assert await _pick(sessionmaker, acting) == str(own.id)
