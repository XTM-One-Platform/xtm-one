"""Coverage for the shared company-managed owner resolver.

The always-set-owner invariant requires every company-managed row to keep a
real ``user_id`` owner (the admin) rather than nulling it. Code paths that
create company-managed rows without an authenticated user to attribute them to
— the startup seed pass and platform registration (``POST /platform/register``,
token-authenticated, no current user) — resolve the owner through the single
shared helper ``resolve_company_managed_owner_id``.

These tests pin its resolution order (configured ``ADMIN_EMAIL`` → oldest admin
→ oldest user → ``None``) and that the seed wrapper delegates to it, so a new
ownerless creation path can't silently drift from the invariant again.
"""

from __future__ import annotations

import uuid

import pytest


class _ScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _QueueDB:
    """Fake async session returning a queued ``scalar_one_or_none`` per call.

    Records the compiled SQL of each statement so the resolution *order* and
    the column predicates of each fallback stage can be asserted.
    """

    def __init__(self, results):
        self._results = list(results)
        self.sql: list[str] = []

    async def execute(self, statement):
        self.sql.append(
            str(statement.compile(compile_kwargs={"literal_binds": True}))
        )
        value = self._results.pop(0) if self._results else None
        return _ScalarResult(value)


@pytest.mark.asyncio
async def test_resolves_configured_admin_email_first():
    """A user matching ``settings.admin_email`` short-circuits the resolver."""
    from app.api.v1.helpers import resolve_company_managed_owner_id
    from app.core.config import settings

    admin_id = uuid.uuid4()
    db = _QueueDB([admin_id])

    assert await resolve_company_managed_owner_id(db) == admin_id
    # Only the first (admin-email) query runs — no fallback needed.
    assert len(db.sql) == 1
    assert settings.admin_email in db.sql[0]


@pytest.mark.asyncio
async def test_falls_back_to_oldest_admin():
    """No ADMIN_EMAIL match → oldest ``role == 'admin'`` user."""
    from app.api.v1.helpers import resolve_company_managed_owner_id

    admin_id = uuid.uuid4()
    db = _QueueDB([None, admin_id])

    assert await resolve_company_managed_owner_id(db) == admin_id
    assert len(db.sql) == 2
    assert "role" in db.sql[1].lower()
    assert "admin" in db.sql[1].lower()


@pytest.mark.asyncio
async def test_falls_back_to_oldest_user():
    """No admin at all → oldest user of any role."""
    from app.api.v1.helpers import resolve_company_managed_owner_id

    user_id = uuid.uuid4()
    db = _QueueDB([None, None, user_id])

    assert await resolve_company_managed_owner_id(db) == user_id
    assert len(db.sql) == 3


@pytest.mark.asyncio
async def test_returns_none_on_empty_db():
    """Brand-new DB with no users yet → ``None`` (backfilled on next startup)."""
    from app.api.v1.helpers import resolve_company_managed_owner_id

    db = _QueueDB([None, None, None])

    assert await resolve_company_managed_owner_id(db) is None
    assert len(db.sql) == 3


@pytest.mark.asyncio
async def test_seed_owner_resolver_delegates_to_shared_helper():
    """``seed._resolve_seed_owner_id`` must defer to the shared resolver so the
    seed pass and platform registration can never diverge on the invariant."""
    from app.agents.seed import _resolve_seed_owner_id

    admin_id = uuid.uuid4()
    db = _QueueDB([admin_id])

    assert await _resolve_seed_owner_id(db) == admin_id
