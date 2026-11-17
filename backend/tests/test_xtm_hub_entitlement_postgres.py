"""Refusal attribution against a real PostgreSQL.

The unit suite proves *what* a refusal decides. This proves what it leaves
behind, which only a real database can answer, because the constraint doing the
work is ``audit_logs.user_id NOT NULL``.

That constraint is why refusals are attributed rather than stored unconditionally
(FR-009 / FR-009a): a refusal for an address with no account has nobody to hang a
row on, and the owner's instruction was not to widen a table shared by every
other feature so it could hold rows belonging to no one. A fake session would
accept such a row happily and this design decision would go unverified until
production rejected it.

Marked ``integration``: needs PostgreSQL, skipped when unavailable.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.api.auth_sso import _record_refusal
from app.models.audit_log import AuditLog
from app.models.user import User
from tests.scratch_database import scratch_database

pytestmark = pytest.mark.integration

_TABLES = [User.__table__, AuditLog.__table__]


@asynccontextmanager
async def _temp_database(prefix: str) -> AsyncIterator[async_sessionmaker]:
    """Isolated database holding only users and audit_logs, dropped on exit."""
    async with scratch_database(prefix, tables=_TABLES) as db:
        yield db.sessions


async def test_a_refusal_for_an_existing_account_is_durable_and_queryable():
    """FR-009: an operator can find it afterwards, which is the point of durable.

    The query is the one from ``quickstart.md`` — by action, not by a null
    ``user_id`` — so the scenario an operator is told to run is the scenario
    this test runs.
    """
    async with _temp_database("hub_refusal") as sessionmaker:
        async with sessionmaker() as session:
            session.add(
                User(
                    email="person@example.com",
                    full_name="A Person",
                    role="user",
                    sso_provider="oidc",
                    sso_subject="auth0|person",
                )
            )
            await session.commit()

        async with sessionmaker() as session:
            await _record_refusal(
                session,
                "person@example.com",
                "userinfo carries no 'xtmone-admin_groups' claim for this instance",
            )
            await session.commit()

        async with sessionmaker() as session:
            rows = (
                await session.execute(
                    select(AuditLog).where(AuditLog.action.like("%refus%"))
                )
            ).scalars().all()

    assert len(rows) == 1
    assert rows[0].user_email == "person@example.com"
    assert rows[0].user_id is not None
    assert "xtmone-admin_groups" in rows[0].summary


async def test_a_refusal_for_an_unknown_address_creates_neither_row_nor_account():
    """FR-009a, and the reason this feature has no migration.

    Nothing is written and — the part that matters for FR-002 — no account is
    conjured up to hang the record on. Recording a refusal must not become a
    back door that provisions the very account the gate refused.
    """
    async with _temp_database("hub_refusal_unknown") as sessionmaker:
        async with sessionmaker() as session:
            await _record_refusal(session, "stranger@example.com", "no claim")
            await session.commit()

        async with sessionmaker() as session:
            audit_count = await session.scalar(select(func.count()).select_from(AuditLog))
            user_count = await session.scalar(select(func.count()).select_from(User))

    assert (audit_count, user_count) == (0, 0)


async def test_the_constraint_that_shapes_this_design_is_real():
    """Pins *why* attribution is the mechanism, not a preference.

    If ``audit_logs.user_id`` ever becomes nullable, this test fails — and that
    is the signal to revisit FR-009a, because the narrowing it justifies would
    no longer be necessary.
    """
    async with (
        _temp_database("hub_refusal_constraint") as sessionmaker,
        sessionmaker() as session,
    ):
        session.add(
            AuditLog(
                user_id=None,
                user_email="stranger@example.com",
                action="login_refused",
                entity_type="user",
                summary="Refused OIDC sign-in",
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()
