"""Regression coverage for a NULL ``oauth_share_acknowledged``.

``GET /api/v1/integrations`` returned HTTP 500 whenever a row had
``oauth_share_acknowledged = NULL`` in the database, because
``IntegrationResponse.oauth_share_acknowledged`` is a non-optional ``bool``
and Pydantic rejects ``None`` during serialization. The column only had a
Python-side ``default=False`` (applied by the ORM at flush time), so any
row written outside a normal ``session.add()`` + flush — a raw ``INSERT``,
a bulk load, a column added by the ``_ensure_model_columns`` safety net in
``app/core/migrations.py`` — could end up ``NULL``. The fix makes the
column ``NOT NULL DEFAULT FALSE`` at the DB level (``app/models/integration.py``
+ migration ``f6a2oash0001``), so the database itself now guarantees a
value regardless of the insert path, and an explicit ``NULL`` is rejected
outright.

Runs against a dedicated, disposable Postgres database (same isolation
pattern as ``test_platform_settings_singleton.py``) because the behaviour
under test is the column's DB-level ``NOT NULL``/``DEFAULT`` — a plain ORM
insert already exercised the Python-side default even before the fix, so
it wouldn't catch a regression here; a raw SQL insert that bypasses the
ORM default is what actually distinguishes the two.
"""

from __future__ import annotations

import contextlib
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.models as _models  # noqa: F401 — side-effect: register all models
from app.api.v1.integrations import _to_response
from app.models.integration import Integration
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration


@contextlib.asynccontextmanager
async def _temporary_database():
    """Create a dedicated, empty Postgres database with our schema, drop on exit.

    An unreachable server fails the test rather than skipping it.
    """
    async with scratch_database(
        "copilot_oauth_share_default",
        tables=ALL_TABLES,
        extensions=("vector",),
        skip_when_unreachable=False,
    ) as db:
        yield db.url


@pytest.fixture
async def empty_db_url():
    async with _temporary_database() as url:
        yield url


_RAW_INSERT_COLUMNS = (
    "id, user_id, is_company_managed, is_builtin, provider, auth_type, name, status, call_count, created_at, updated_at"
)

# Sentinel distinguishing "omit the column from the INSERT entirely" (let the
# DB DEFAULT apply) from an explicit `oauth_share_acknowledged=None` call,
# which must mean "insert the literal SQL NULL".
_OMIT = object()


async def _raw_insert_integration(session, *, oauth_share_acknowledged=_OMIT) -> uuid.UUID:
    """Insert a row via raw SQL, bypassing the ORM's Python-side default.

    Mirrors a legacy row, bulk load, or the ``_ensure_model_columns`` safety
    net — none of which go through ``session.add(Integration(...))``, so
    they never benefit from the column's Python-side ``default=False``.
    """
    integration_id = uuid.uuid4()
    now = datetime.now(UTC)
    params = {
        "id": integration_id,
        "user_id": None,
        "is_company_managed": False,
        "is_builtin": False,
        "provider": "slack",
        "auth_type": "api_key",
        "name": "Raw insert",
        "status": "active",
        "call_count": 0,
        "created_at": now,
        "updated_at": now,
    }
    if oauth_share_acknowledged is _OMIT:
        columns, values = _RAW_INSERT_COLUMNS, ":" + ", :".join(params)
    else:
        columns = f"{_RAW_INSERT_COLUMNS}, oauth_share_acknowledged"
        values = ":" + ", :".join(params) + ", :oauth_share_acknowledged"
        params["oauth_share_acknowledged"] = oauth_share_acknowledged
    await session.execute(
        text(f"INSERT INTO integrations ({columns}) VALUES ({values})"),  # noqa: S608
        params,
    )
    return integration_id


async def test_raw_insert_omitting_column_defaults_to_false(empty_db_url):
    """A raw INSERT that never mentions the column must still land `false`."""
    engine = create_async_engine(empty_db_url)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    async with session_factory() as session:
        integration_id = await _raw_insert_integration(session)
        await session.commit()

    async with session_factory() as session:
        row = await session.get(Integration, integration_id)
        assert row.oauth_share_acknowledged is False

        # The actual regression: _to_response (GET /api/v1/integrations)
        # must not raise, and must serialize the default as `false`.
        response = _to_response(row, can_manage=True, can_authorize=True)
        assert response.oauth_share_acknowledged is False
        assert response.model_dump()["oauth_share_acknowledged"] is False

    await engine.dispose()


async def test_explicit_null_is_rejected_by_the_database(empty_db_url):
    """The DB must refuse NULL outright — the invariant that keeps a NULL from reaching the response."""
    engine = create_async_engine(empty_db_url)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    async with session_factory() as session:
        with pytest.raises(IntegrityError):
            await _raw_insert_integration(session, oauth_share_acknowledged=None)

    await engine.dispose()
