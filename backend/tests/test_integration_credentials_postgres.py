"""The OAuth token rule's SQL form, run by PostgreSQL itself.

``test_integration_credentials`` pins the SQL form against the Python form with
an in-memory evaluator; this runs the same truth table through a real database,
so the two forms are proven to agree under PostgreSQL's own NULL / boolean
semantics. A session-scoped TEMP table named ``integrations`` shadows the real
one: nothing persistent is created or read.
"""

from __future__ import annotations

import itertools
import os
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import create_async_engine

from app.models.integration import Integration
from app.services.integration_credentials import (
    oauth_credentials_usable,
    oauth_credentials_usable_clause,
)

pytestmark = pytest.mark.integration

_OWNER = uuid.uuid4()
_COLLEAGUE = uuid.uuid4()


def _backfill_module():
    import importlib.util
    from pathlib import Path

    path = (
        Path(__file__).resolve().parents[1]
        / "alembic"
        / "versions"
        / "b8ack2642001_backfill_shared_oauth_consent.py"
    )
    spec = importlib.util.spec_from_file_location("backfill_2642", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _backfill_where() -> str:
    return _backfill_module().SHARED_OAUTH_ROWS


async def test_the_consent_backfill_runs_in_committed_batches_and_resumes():
    """Every matching row is acknowledged across several batches, each committed
    on its own; the session's lock_timeout is put back; a re-run is a no-op."""
    module = _backfill_module()
    engine = create_async_engine(
        os.environ.get(
            "DATABASE_URL", "postgresql+asyncpg://copilot:copilot@localhost:5432/copilot"
        )
    ).execution_options(isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as conn:
            await conn.execute(
                text(
                    "CREATE TEMP TABLE integrations ("
                    " id uuid PRIMARY KEY, auth_type varchar(20) NOT NULL,"
                    " access_token_enc text, is_company_managed boolean NOT NULL,"
                    " shared_group_ids json, shared_user_ids json,"
                    " oauth_share_acknowledged boolean NOT NULL DEFAULT FALSE)"
                )
            )
            for index in range(9):
                await conn.execute(
                    text(
                        "INSERT INTO integrations (id, auth_type, access_token_enc,"
                        " is_company_managed) VALUES (:i, :a, 'tok', TRUE)"
                    ),
                    {"i": uuid.uuid4(), "a": "oauth" if index < 7 else "api_key"},
                )
            before = (await conn.execute(text("SHOW lock_timeout"))).scalar()

            first = await conn.run_sync(module.backfill_in_batches, 2)
            second = await conn.run_sync(module.backfill_in_batches, 2)

            after = (await conn.execute(text("SHOW lock_timeout"))).scalar()
            acknowledged = (
                await conn.execute(
                    text("SELECT count(*) FROM integrations WHERE oauth_share_acknowledged")
                )
            ).scalar()
            await conn.execute(text("DROP TABLE integrations"))
        assert first == 7
        assert second == 0
        assert acknowledged == 7
        assert after == before
    finally:
        await engine.dispose()


async def test_the_consent_backfill_acknowledges_exactly_the_shared_oauth_rows_holding_a_token():
    """Worked on main -> still works: every shared OAuth integration holding a
    token is acknowledged once; nothing else is touched, and a re-run is a no-op."""
    cases = {
        "company_managed": ("oauth", "tok", True, None, None, True),
        "group_shared": ("oauth", "tok", False, '["g"]', None, True),
        "user_shared": ("oauth", "tok", False, None, '["u"]', True),
        "empty_lists": ("oauth", "tok", False, "[]", "[]", False),
        "unshared": ("oauth", "tok", False, None, None, False),
        "stripped_token": ("oauth", None, True, None, None, False),
        "api_key": ("api_key", "tok", True, None, None, False),
    }
    engine = create_async_engine(
        os.environ.get(
            "DATABASE_URL", "postgresql+asyncpg://copilot:copilot@localhost:5432/copilot"
        )
    )
    try:
        async with engine.connect() as conn:
            await conn.execute(
                text(
                    "CREATE TEMP TABLE integrations ("
                    " name text PRIMARY KEY, auth_type varchar(20) NOT NULL,"
                    " access_token_enc text, is_company_managed boolean NOT NULL,"
                    " shared_group_ids json, shared_user_ids json,"
                    " oauth_share_acknowledged boolean NOT NULL DEFAULT FALSE)"
                )
            )
            for name, (auth, token, company, groups, users, _) in cases.items():
                await conn.execute(
                    text(
                        "INSERT INTO integrations (name, auth_type, access_token_enc,"
                        " is_company_managed, shared_group_ids, shared_user_ids)"
                        " VALUES (:n, :a, :t, :c, CAST(:g AS json), CAST(:u AS json))"
                    ),
                    {"n": name, "a": auth, "t": token, "c": company, "g": groups, "u": users},
                )
            where = _backfill_where()
            first = await conn.execute(
                text(f"UPDATE integrations SET oauth_share_acknowledged = TRUE WHERE {where}")  # noqa: S608
            )
            second = await conn.execute(
                text(f"UPDATE integrations SET oauth_share_acknowledged = TRUE WHERE {where}")  # noqa: S608
            )
            rows = dict(
                (await conn.execute(text("SELECT name, oauth_share_acknowledged FROM integrations"))).all()
            )
        assert rows == {name: expected for name, (*_, expected) in cases.items()}
        assert first.rowcount == 3
        assert second.rowcount == 0
    finally:
        await engine.dispose()


async def test_postgres_and_python_agree_on_who_may_use_a_stored_token():
    rows = [
        {"id": uuid.uuid4(), "user_id": owner, "auth_type": auth_type, "ack": ack, "mode": mode}
        for auth_type, owner, ack, mode in itertools.product(
            ["oauth", "api_key", "platform_managed"],
            [None, _OWNER, _COLLEAGUE],
            [True, False],
            ["shared", "per_user"],
        )
    ]
    engine = create_async_engine(
        os.environ.get(
            "DATABASE_URL", "postgresql+asyncpg://copilot:copilot@localhost:5432/copilot"
        )
    )
    try:
        async with engine.connect() as conn:
            await conn.execute(
                text(
                    "CREATE TEMP TABLE integrations ("
                    " id uuid PRIMARY KEY, user_id uuid,"
                    " auth_type varchar(20) NOT NULL,"
                    " oauth_share_acknowledged boolean NOT NULL,"
                    " credential_mode varchar(16) NOT NULL DEFAULT 'shared')"
                )
            )
            await conn.execute(
                text(
                    "INSERT INTO integrations"
                    " (id, user_id, auth_type, oauth_share_acknowledged, credential_mode)"
                    " VALUES (:id, :user_id, :auth_type, :ack, :mode)"
                ),
                rows,
            )
            for acting in (None, _OWNER, _COLLEAGUE, "anonymous"):
                result = await conn.execute(
                    select(Integration.id).where(oauth_credentials_usable_clause(acting))
                )
                expected = {
                    row["id"]
                    for row in rows
                    if oauth_credentials_usable(
                        SimpleNamespace(
                            auth_type=row["auth_type"],
                            user_id=row["user_id"],
                            oauth_share_acknowledged=row["ack"],
                            credential_mode=row["mode"],
                        ),
                        acting,
                    )
                }
                assert set(result.scalars()) == expected, f"acting={acting!r}"
    finally:
        await engine.dispose()
