"""Audit log retention purge against a real PostgreSQL."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.models.audit_log import AuditLog
from app.models.user import User
from app.services.audit_retention import purge_expired_audit_logs
from tests.test_provisioning_postgres import _temp_database

pytestmark = pytest.mark.integration

NOW = datetime(2026, 11, 7, 12, 0, tzinfo=UTC)


def _user(name: str) -> User:
    return User(id=uuid.uuid4(), email=f"{name}@example.com", full_name=name, hashed_password="x")


def _entry(
    user: User, days_ago: int, action: str = "workspace_edit", entity_type: str = "sandbox_workspace"
) -> AuditLog:
    return AuditLog(
        id=uuid.uuid4(),
        user_id=user.id,
        user_email=user.email,
        action=action,
        entity_type=entity_type,
        summary=f"{action} {days_ago} day(s) ago",
        created_at=NOW - timedelta(days=days_ago),
    )


@pytest.mark.asyncio
async def test_entries_past_the_window_are_deleted_and_the_rest_kept():
    async with _temp_database("audit_retention") as sessionmaker:
        alice, bob = _user("alice"), _user("bob")
        old_sign_in = _entry(alice, 200, "login", "user")
        older_sign_in = _entry(alice, 300, "login", "user")
        bob_recent_sign_in = _entry(bob, 5, "login", "user")
        bob_old_sign_in = _entry(bob, 150, "login", "user")
        recent = _entry(alice, 10)
        edge_kept = _entry(alice, 89)
        expired = [_entry(alice, 91), _entry(bob, 120), _entry(alice, 400)]
        async with sessionmaker() as db:
            db.add_all([alice, bob])
            await db.flush()
            db.add_all([old_sign_in, older_sign_in, bob_recent_sign_in, bob_old_sign_in, recent, edge_kept, *expired])
            await db.commit()

        async with sessionmaker() as db:
            deleted = await purge_expired_audit_logs(db, 90, now=NOW)

        async with sessionmaker() as db:
            left = set((await db.execute(select(AuditLog.id))).scalars().all())

        # Each user's latest sign-in survives the window (last login, dormant accounts);
        # an older sign-in of the same user does not.
        assert left == {old_sign_in.id, bob_recent_sign_in.id, recent.id, edge_kept.id}
        assert deleted == 5

        async with sessionmaker() as db:
            assert await purge_expired_audit_logs(db, 90, now=NOW) == 0


@pytest.mark.asyncio
async def test_the_setting_moves_the_cut():
    async with _temp_database("audit_retention_window") as sessionmaker:
        alice = _user("alice")
        kept, gone = _entry(alice, 100), _entry(alice, 400)
        async with sessionmaker() as db:
            db.add(alice)
            await db.flush()
            db.add_all([kept, gone])
            await db.commit()

        async with sessionmaker() as db:
            assert await purge_expired_audit_logs(db, 365, now=NOW) == 1

        async with sessionmaker() as db:
            assert set((await db.execute(select(AuditLog.id))).scalars().all()) == {kept.id}
