"""Concurrent ``append`` + ``keep_last`` writes on one shared-state list (#2096).

Several runs of one assignment append to the same list of one key at once,
each with ``keep_last``. Every append must land, no call may trim an item it
appended or a list under its cap, and each answer must describe the list as
stored. Runs against PostgreSQL: the row lock is what serialises the writes.
"""

from __future__ import annotations

import asyncio
import json
import os
import urllib.parse
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError, OperationalError, ProgrammingError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.models  # noqa: F401 - registers every mapper
from app.core.database import Base
from app.models.shared_state import SharedState
from app.models.user import User
from app.services.company_managed_writes import agent_writer

pytestmark = pytest.mark.integration

_AGENT = agent_writer(is_company_managed=False)


def _base_url() -> str:
    return os.environ.get(
        "DATABASE_URL",
        "postgresql+asyncpg://copilot:copilot@localhost:5432/copilot",
    )


@asynccontextmanager
async def _temp_database() -> AsyncIterator[async_sessionmaker]:
    parsed = urllib.parse.urlparse(_base_url())
    admin_url = parsed._replace(path="/postgres").geturl()
    db_name = f"sskeep_{uuid.uuid4().hex[:10]}"
    test_url = parsed._replace(path=f"/{db_name}").geturl()
    admin_engine = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        async with admin_engine.connect() as conn:
            await conn.execute(text(f'CREATE DATABASE "{db_name}"'))
    except (DBAPIError, OperationalError, ProgrammingError) as exc:
        orig = getattr(exc, "orig", None)
        if (getattr(orig, "pgcode", None) or getattr(orig, "sqlstate", None)) == "42501":
            pytest.skip("DATABASE_URL user requires CREATEDB privilege")
        raise
    except Exception as exc:  # pragma: no cover - no PG in this environment
        pytest.skip(f"Cannot connect to PostgreSQL: {exc}")
    finally:
        await admin_engine.dispose()
    engine = create_async_engine(test_url, pool_size=20, max_overflow=10)
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


@asynccontextmanager
async def _pointed_at(sessions) -> AsyncIterator[None]:
    """Point the service's own sessions at the scratch database."""
    from app.core import database as db_module

    original = db_module.async_session
    db_module.async_session = sessions
    try:
        yield
    finally:
        db_module.async_session = original


async def _user(sessions) -> str:
    async with sessions() as session:
        user = User(id=uuid.uuid4(), email="ledger@example.com", full_name="ledger", hashed_password="x", role="user")
        session.add(user)
        await session.commit()
        return str(user.id)


async def _stored(sessions, key: str) -> list:
    async with sessions() as session:
        row = (await session.execute(select(SharedState).where(SharedState.key == key))).scalars().one()
        return list((row.value or {}).get("runs") or [])


@pytest.mark.asyncio
@pytest.mark.parametrize("existing", [True, False], ids=["existing-entry", "first-touch"])
async def test_ten_concurrent_appends_with_keep_last_all_land(existing: bool) -> None:
    from app.services.shared_state import upsert_state

    async with _temp_database() as sessions, _pointed_at(sessions):
        uid = await _user(sessions)
        key = "ledger"
        if existing:
            await upsert_state(user_id=uid, key=key, value={"runs": []}, writer=_AGENT)

        async def _one(n: int) -> dict:
            return await upsert_state(
                user_id=uid,
                key=key,
                append={"runs": [{"n": n}]},
                keep_last={"runs": 50},
                guard_trim=True,
                deep=True,
                writer=_AGENT,
            )

        answers = await asyncio.gather(*(_one(n) for n in range(10)))

        stored = await _stored(sessions, key)
        assert sorted(item["n"] for item in stored) == list(range(10))
        for n, answer in enumerate(answers):
            counts = answer["list_changes"]["runs"]
            assert counts["appended"] == 1, answer
            assert counts["trimmed"] == 0, answer
            held = answer["value"]["runs"]
            assert len(held) >= 1
            assert {"n": n} in held, answer


@pytest.mark.asyncio
async def test_ten_concurrent_tool_calls_with_keep_last_all_land() -> None:
    """The same race through set_shared_state, its archive and answer paths included."""
    from types import SimpleNamespace

    from app.services.builtin_tools import build_shared_state_tools
    from app.tools.base import ToolCall

    async with _temp_database() as sessions, _pointed_at(sessions):
        uid = await _user(sessions)
        _, executor = build_shared_state_tools(SimpleNamespace(), uid, "")

        async def _one(n: int):
            return await executor.execute_tool(
                ToolCall(
                    id=str(uuid.uuid4()),
                    name="set_shared_state",
                    arguments={
                        "key": "ledger",
                        "append_json": json.dumps({"runs": [{"n": n}]}),
                        "keep_last_json": '{"runs": 50}',
                    },
                )
            )

        results = await asyncio.gather(*(_one(n) for n in range(10)))
        for result in results:
            assert not result.is_error, result.content
            assert "1 appended, 0 removed, 0 trimmed" in result.content, result.content
            assert ", 0 item(s) now" not in result.content, result.content
        stored = await _stored(sessions, "ledger")
        assert sorted(item["n"] for item in stored) == list(range(10))


@pytest.mark.asyncio
async def test_keep_last_three_on_five_plus_one_keeps_the_newest_with_the_append() -> None:
    from app.services.shared_state import upsert_state

    async with _temp_database() as sessions, _pointed_at(sessions):
        uid = await _user(sessions)
        await upsert_state(user_id=uid, key="ledger", value={"runs": [{"n": i} for i in range(5)]}, writer=_AGENT)
        answer = await upsert_state(
            user_id=uid,
            key="ledger",
            append={"runs": [{"n": 5}]},
            keep_last={"runs": 3},
            writer=_AGENT,
        )
        assert answer["list_changes"]["runs"] == {"appended": 1, "removed": 0, "trimmed": 3}
        assert answer["value"]["runs"] == [{"n": 3}, {"n": 4}, {"n": 5}]
        assert await _stored(sessions, "ledger") == [{"n": 3}, {"n": 4}, {"n": 5}]
