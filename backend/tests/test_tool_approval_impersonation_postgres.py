"""Impersonation attribution on a standing approval, against real PostgreSQL.

FR-017b wants both identities on the trail when an administrator acts through
impersonation: the account that holds the preference, and the administrator who
made it. Two ways that was lost, neither visible to a fake-DB test because both
turn on what an UPSERT does to a column it was not given a value for:

* the chat grant passed no impersonator at all, so a run-recorded administrator
  was erased the next time the same user clicked "Yes, always" in chat;
* an invalidation names nobody, because the drain that writes it has no request
  context — it has to fall back to the administrator recorded on the row.

Exercised end to end because the behaviour lives in the interaction between the
upsert, the row's existing state and the audit write, not in any one of them.
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

from app.core.database import Base
from app.models.audit_log import AuditLog
from app.models.tool_approval import ToolApprovalPreference
from app.models.user import User
from app.services.tool_approval_service import (
    REASON_TOOL_CHANGED,
    grant_exception,
    invalidate_stale_approvals,
)
from tests.scratch_database import with_referenced_tables

pytestmark = pytest.mark.integration

_TABLES = [User.__table__, ToolApprovalPreference.__table__, AuditLog.__table__]

TOOL = "delete_entity"
SOURCE = "custom:00000000-0000-0000-0000-000000000001"
FP_A = "a" * 64
FP_B = "b" * 64


def _base_url() -> str:
    # CRLF-safe: a .env dragged through Windows leaves \r on the value, which
    # asyncpg faithfully turns into a database named "copilot\r".
    return os.environ.get(
        "DATABASE_URL",
        "postgresql+asyncpg://copilot:copilot@localhost:5432/copilot",
    ).strip()


@asynccontextmanager
async def _temp_database(prefix: str) -> AsyncIterator[async_sessionmaker]:
    """Isolated database with just the approval tables, dropped on exit."""
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


async def _seed_user(db, email: str) -> uuid.UUID:
    user = User(id=uuid.uuid4(), email=email, hashed_password="x", full_name=email)
    db.add(user)
    await db.commit()
    return user.id


@pytest.mark.asyncio
async def test_a_later_grant_does_not_erase_the_administrator():
    """The regression itself: a run grants while impersonating, then the same
    user clicks "Yes, always" in chat — which knows nothing about impersonation
    and passes None. Overwriting with it would drop the administrator from a row
    whose history is the entire reason FR-017b exists."""
    async with _temp_database("hitl_imp") as session, session() as db:
        user_id = await _seed_user(db, f"u-{uuid.uuid4().hex[:8]}@example.com")
        admin_id = await _seed_user(db, f"a-{uuid.uuid4().hex[:8]}@example.com")

        await grant_exception(
            db,
            user_id=user_id,
            tool_key=TOOL,
            tool_source=SOURCE,
            schema_fingerprint=FP_A,
            granted_by_admin_id=admin_id,
        )
        await db.commit()

        # The chat path: same user, same tool, no impersonator known.
        await grant_exception(
            db,
            user_id=user_id,
            tool_key=TOOL,
            tool_source=SOURCE,
            schema_fingerprint=FP_B,
            granted_by_admin_id=None,
        )
        await db.commit()

        row = await db.scalar(
            select(ToolApprovalPreference).where(
                ToolApprovalPreference.user_id == user_id,
                ToolApprovalPreference.revoked_at.is_(None),
            )
        )
        assert row.granted_by_admin_id == admin_id, (
            "the administrator behind the original grant must survive a "
            "re-grant that knows nothing about impersonation"
        )
        # And it really was the same row, re-consented to the new shape.
        assert row.schema_fingerprint == FP_B


@pytest.mark.asyncio
async def test_one_active_row_per_user_and_tool():
    """Two "Yes, always" clicks converge rather than racing to insert."""
    async with _temp_database("hitl_imp") as session, session() as db:
        user_id = await _seed_user(db, f"u-{uuid.uuid4().hex[:8]}@example.com")
        for fp in (FP_A, FP_B):
            await grant_exception(
                db,
                user_id=user_id,
                tool_key=TOOL,
                tool_source=SOURCE,
                schema_fingerprint=fp,
            )
            await db.commit()

        rows = (
            await db.scalars(
                select(ToolApprovalPreference).where(
                    ToolApprovalPreference.user_id == user_id,
                    ToolApprovalPreference.revoked_at.is_(None),
                )
            )
        ).all()
        assert len(rows) == 1


@pytest.mark.asyncio
async def test_an_invalidation_still_names_the_administrator():
    """The drain has no request context, so it falls back to the impersonator
    recorded on the row — otherwise a grant made on someone's behalf loses its
    second identity precisely when the trail is asked to explain itself."""
    async with _temp_database("hitl_imp") as session, session() as db:
        user_id = await _seed_user(db, f"u-{uuid.uuid4().hex[:8]}@example.com")
        admin_id = await _seed_user(db, f"a-{uuid.uuid4().hex[:8]}@example.com")

        await grant_exception(
            db,
            user_id=user_id,
            tool_key=TOOL,
            tool_source=SOURCE,
            schema_fingerprint=FP_A,
            granted_by_admin_id=admin_id,
        )
        await db.commit()

        await invalidate_stale_approvals(
            db, user_id=user_id, tool_keys={TOOL}, reason=REASON_TOOL_CHANGED
        )
        await db.commit()

        row = await db.scalar(
            select(ToolApprovalPreference).where(
                ToolApprovalPreference.user_id == user_id
            )
        )
        assert row.revoked_reason == REASON_TOOL_CHANGED

        audits = (
            await db.scalars(
                select(AuditLog).where(
                    AuditLog.entity_type == "tool_approval_preference"
                )
            )
        ).all()
        invalidation = [a for a in audits if "invalidated" in (a.summary or "")]
        assert invalidation, "FR-019b: an invalidation belongs in the trail"
        assert invalidation[0].details["impersonated_by"] == str(admin_id)
