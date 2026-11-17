"""Which provider key a web-search step may use.

Against real Postgres because the defect this pins was a `WHERE` clause: the
lookup selected on `LLMProviderConfig.user_id`, reading it as ownership. That
model says in its own first paragraph that providers are "configured once by
admins and shared across all users" and that `user_id` is "an optional audit
trail (created by)" — so a platform key typed in by one admin carries a
non-null `user_id` that is not the caller, and every other user was excluded
from the install's only search key while being told none was configured.

A single test asserting that a config created by user A is reachable by user B
would have caught it, and there was no test at all.
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

import app.models  # noqa: F401 — registers every mapper
from app.core.database import Base
from app.models.llm_config import LLMProviderConfig
from app.models.user import User
from app.services.investigation_queriers import resolve_search_credentials

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
    db_name = f"searchcred_{uuid.uuid4().hex[:10]}"
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
        # Broad on purpose, and it has to be: asyncpg raises a bare `OSError`
        # when nothing is listening, which is not a SQLAlchemy error and so
        # slipped past the tuple above — turning "no database here" into three
        # failures in a CI job that has no Postgres at all.
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


async def _seed_user(session, email: str) -> User:
    user = User(
        id=uuid.uuid4(),
        email=email,
        hashed_password="x",
        full_name="Analyst",
        role="user",
    )
    session.add(user)
    await session.flush()
    return user


def _config(provider: str, creator, *, is_default: bool = False) -> LLMProviderConfig:
    from app.core.security import encrypt_value

    return LLMProviderConfig(
        id=uuid.uuid4(),
        provider=provider,
        api_key_enc=encrypt_value(f"sk-{provider}"),
        user_id=creator.id if creator else None,
        is_default=is_default,
    )


@pytest.mark.asyncio
async def test_a_key_one_admin_configured_is_reachable_by_everyone_else():
    """The regression this file exists for. `user_id` records who typed the
    key in, not who may use it, so a platform key stamped with its creator was
    invisible to every other user — and `web_search`, the source kind that
    makes an open question answerable, was silently dead for them while the
    error told the operator to add a key they had already added."""
    async with _temp_database() as sessionmaker, sessionmaker() as session:
        admin = await _seed_user(session, "admin@example.test")
        await _seed_user(session, "analyst@example.test")
        session.add(_config("anthropic", admin))
        await session.commit()

        found = await resolve_search_credentials(session)

        assert found is not None, "the install's only search key was unreachable"
        assert found[0] == "anthropic"


@pytest.mark.asyncio
async def test_no_configured_provider_is_reported_as_none():
    async with _temp_database() as sessionmaker, sessionmaker() as session:
        await _seed_user(session, "analyst@example.test")
        await session.commit()
        assert await resolve_search_credentials(session) is None


@pytest.mark.asyncio
async def test_the_provider_order_decides_then_the_admin_marked_default():
    from app.services.web_search import SEARCH_PROVIDERS

    async with _temp_database() as sessionmaker, sessionmaker() as session:
        admin = await _seed_user(session, "admin@example.test")
        # Seeded in the reverse of SEARCH_PROVIDERS so insertion order cannot
        # be what makes this pass.
        for provider in reversed(SEARCH_PROVIDERS):
            session.add(_config(provider, admin))
        await session.commit()

        found = await resolve_search_credentials(session)
        assert found is not None
        assert found[0] == SEARCH_PROVIDERS[0]
