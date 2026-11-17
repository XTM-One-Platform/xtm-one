"""The runtime AI toggle against real PostgreSQL (US2).

The toggle is the half of the feature that writes: it flips the tri-state
column, provisions the built-in content boot would have created, and stops the
scheduler dispatching. All three only mean anything against a real database, so
this is ``integration``-marked and skips when PostgreSQL is unreachable, like
its neighbours - except on CI, whose integration job provides it: there a skip
would be a green build that checked nothing.

The endpoint function is called directly rather than over HTTP. What is being
tested is the transaction — lock, write, commit, cache, provision — and an HTTP
client would add an app fixture without covering a single extra branch. The one
thing that reading loses, the administrator check, is pinned structurally in
``test_ai_toggle.py``.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

import app.models  # noqa: F401 - registers every table on Base.metadata
from app.api.v1.platform_settings_helpers import get_platform_settings
from app.api.v1.settings import update_platform_settings_endpoint
from app.models.agent import Agent
from app.models.assignment import Assignment
from app.models.audit_log import AuditLog
from app.models.platform_settings import PlatformSettings
from app.models.user import User
from app.services import ai_state
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration

_ON_CI = bool(os.environ.get("CI"))


@asynccontextmanager
async def _temp_database(prefix: str) -> AsyncIterator[async_sessionmaker]:
    """Isolated database with the full schema, dropped on exit.

    Turning AI on runs the whole provisioning path - the seeders, the knowledge
    base enqueue, template assets, model prices, the env-provider seeders in
    ``app.main`` - and none of it may reach another database: any
    connection elsewhere, whichever engine asks, is refused and fails the test.
    The app's session factory object is rebound rather than any module's
    ``async_session`` replaced: a module first imported mid-test would keep a
    replaced one, a factory for a database dropped on exit. The seeded
    documents are enqueued for embedding, the worker's business, not the
    toggle's, so the queue is stubbed.
    """
    async with scratch_database(
        prefix,
        tables=ALL_TABLES,
        extensions=("vector",),
        skip_without_extensions=not _ON_CI,
        skip_when_unreachable=not _ON_CI,
        stub_worker_queue=True,
        refuse_other_databases=True,
    ) as db:
        yield db.sessions


@asynccontextmanager
async def _no_lock(wait_timeout: float | None = None):
    """Boot's advisory lock, neutered.

    ``bootstrap_lock`` opens its own connection on the process-wide engine,
    which points at the developer's real database, not the temporary one. The
    lock is real concurrency machinery tested where it lives; here it would
    only couple these tests to another database.

    ``wait_timeout`` is accepted and ignored: the request path passes one so an
    admin PUT cannot outlive the ingress timeout, and a stub that omitted it
    raised ``TypeError`` inside the endpoint's own ``except Exception`` — which
    surfaced as "provisioning failed" and hid the real cause.
    """
    yield


@pytest.fixture
def toggle(monkeypatch):
    """Call the toggle the way a request would, against a temporary database.

    Yields ``(session_factory, put)`` where ``put(ai_enabled=...)`` performs one
    administrator save and returns the endpoint's response body.
    """

    @asynccontextmanager
    async def _setup(mode: str = "ai_disabled"):
        async with _temp_database("ai_toggle") as session:
            monkeypatch.setattr(ai_state.settings, "platform_mode", mode)
            monkeypatch.setattr(
                "app.core.migrations.bootstrap_lock", _no_lock, raising=False
            )

            async with session() as db:
                admin = User(
                    email=f"admin-{uuid.uuid4().hex[:8]}@example.com",
                    hashed_password="x",
                    full_name="Toggle Admin",
                    role="admin",
                    is_active=True,
                )
                db.add(admin)
                await db.commit()

            request = SimpleNamespace(headers={}, client=None)

            async def put(**body):
                async with session() as db:
                    user = await db.get(User, admin.id)
                    result = await update_platform_settings_endpoint(
                        body, request, user=user, db=db
                    )
                    await db.commit()
                    return result

            yield session, put

    return _setup


async def _counts(session) -> dict[str, int]:
    async with session() as db:
        return {
            "agents": await db.scalar(select(func.count()).select_from(Agent)),
            "assignments": await db.scalar(
                select(func.count()).select_from(Assignment)
            ),
        }


# --- FR-012: turning AI on creates what boot would have -------------------


@pytest.mark.asyncio
async def test_turning_ai_on_provisions_the_built_in_content(toggle):
    """SC-002. The response must not come back before this is true: an
    administrator told "saved" who then finds an empty platform reports it as a
    bug rather than waiting.
    """
    async with toggle() as (session, put):
        assert (await _counts(session))["agents"] == 0, "nothing seeded yet"

        body = await put(ai_enabled=True)

        assert body["ai_enabled"] is True
        after = await _counts(session)
        assert after["agents"] > 0, "the toggle reported success and seeded nothing"


@pytest.mark.asyncio
async def test_turning_ai_on_twice_does_not_duplicate(toggle):
    """SC-010. ``true`` on an already-enabled platform is a save of an unchanged
    form field, which the settings page sends on every submit."""
    async with toggle() as (session, put):
        await put(ai_enabled=True)
        first = await _counts(session)

        await put(ai_enabled=True)

        assert await _counts(session) == first


@pytest.mark.asyncio
async def test_a_full_cycle_leaves_no_duplicate_and_no_orphan(toggle):
    """off -> on -> off -> on. Counts must land exactly where the first enable
    left them: duplicated built-in agents or a second copy of a scheduled
    assignment would both survive a naive idempotence check on a single field.
    """
    async with toggle() as (session, put):
        await put(ai_enabled=True)
        baseline = await _counts(session)

        await put(ai_enabled=False)
        await put(ai_enabled=True)

        assert await _counts(session) == baseline


# --- FR-002 / SC-003: turning AI off stops the next tick ------------------


@pytest.mark.asyncio
async def test_turning_ai_off_stops_dispatch_on_the_next_tick(toggle):
    """The scheduler resolves the state once per tick through ``is_ai_enabled``.
    Asserting the resolved answer flipped is asserting dispatch stopped, and it
    must flip on the replica that served the change without waiting out the
    cache TTL.
    """
    async with toggle() as (session, put):
        await put(ai_enabled=True)
        async with session() as db:
            assert await ai_state.is_ai_enabled(db) is True

        await put(ai_enabled=False)

        async with session() as db:
            assert await ai_state.is_ai_enabled(db) is False


@pytest.mark.asyncio
async def test_turning_ai_off_does_not_touch_existing_content(toggle):
    """FR-013/FR-025: off stops dispatch, it does not tear anything down. Work
    in flight finishes because nothing on this path reaches into it."""
    async with toggle() as (session, put):
        await put(ai_enabled=True)
        seeded = await _counts(session)

        await put(ai_enabled=False)

        assert await _counts(session) == seeded


# --- The three distinct operations ----------------------------------------


@pytest.mark.asyncio
async def test_null_clears_the_override_rather_than_disabling(toggle):
    """``null`` is not ``false``. A client that confuses them pins the platform
    to disabled and hides every later change to the deployment default."""
    async with toggle(mode="xtm_one") as (session, put):
        await put(ai_enabled=False)
        async with session() as db:
            assert await db.scalar(select(PlatformSettings.ai_disabled)) is True

        body = await put(ai_enabled=None)

        assert body["ai_enabled"] is None, "the override must read back as unset"
        async with session() as db:
            assert await db.scalar(select(PlatformSettings.ai_disabled)) is None
            # Following the mode again, which here means enabled.
            assert await ai_state.is_ai_enabled(db) is True


@pytest.mark.asyncio
async def test_a_rejected_value_changes_nothing(toggle):
    """A truthy string would otherwise switch AI on for a client that meant the
    opposite, on the one setting where being wrong runs models where they are
    forbidden."""
    from fastapi import HTTPException

    async with toggle() as (session, put):
        with pytest.raises(HTTPException) as exc:
            await put(ai_enabled="true")

        assert exc.value.status_code == 400
        async with session() as db:
            assert await db.scalar(select(PlatformSettings.ai_disabled)) is None


# --- FR-023 / FR-026: the audit trail -------------------------------------


@pytest.mark.asyncio
async def test_every_change_is_audited_with_who_and_what(toggle):
    """SC-009. The value has to be in the entry: "platform settings changed"
    cannot answer whether AI was on at the time of an incident."""
    async with toggle() as (session, put):
        await put(ai_enabled=True)
        await put(ai_enabled=False)

        async with session() as db:
            entries = (
                await db.execute(
                    select(AuditLog)
                    .where(AuditLog.action == "toggle")
                    .order_by(AuditLog.created_at.asc())
                )
            ).scalars().all()

        assert len(entries) == 2
        assert all(e.user_email for e in entries), "who"
        assert all(e.created_at for e in entries), "when"
        assert [e.details["to"] for e in entries] == [True, False], "to what"
        assert entries[1].details["from"] is True, "and from what"


@pytest.mark.asyncio
async def test_concurrent_changes_converge_and_stay_attributable(toggle):
    """FR-026. Two administrators saving at once must leave one value, not a
    torn one, and the audit trail must show both attempts — the loser's change
    is exactly what someone reading the trail afterwards is looking for.
    """
    async with toggle() as (session, put):
        await asyncio.gather(put(ai_enabled=True), put(ai_enabled=False))

        async with session() as db:
            stored = (
                await db.execute(select(PlatformSettings.ai_disabled))
            ).scalars().all()
            audited = (
                await db.execute(
                    select(AuditLog.user_email).where(AuditLog.action == "toggle")
                )
            ).scalars().all()

        assert len(stored) == 1, "the singleton must not have been forked"
        assert stored[0] in (True, False), "one value survived, not a null tear"
        assert len(audited) == 2, "both attempts must be attributable"


@pytest.mark.asyncio
async def test_the_loser_of_a_race_reports_what_it_actually_changed(toggle):
    """The second writer must report the value it actually changed from, even
    though its session read the row earlier in the request and the winner has
    committed since. ``previous`` goes into the audit trail, and a value the
    platform had already moved off is exactly what someone reconstructing a
    race must not be told.
    """
    async with toggle() as (session, put):
        await put(ai_enabled=True)

        async with session() as slow:
            # The entity, as the endpoint loads it, and before the other change
            # lands: this is what puts a stale copy in the identity map.
            await get_platform_settings(slow)
            await put(ai_enabled=False)

            previous = await ai_state.set_ai_enabled(slow, True)

        assert previous is False, "the waiter must see the committed value"
