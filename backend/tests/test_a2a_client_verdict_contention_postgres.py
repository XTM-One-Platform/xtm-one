"""A contended verdict write never cancels a delegation's send.

A delegation re-verifying a stale target writes the verdict in a short
transaction of its own, and the sweep holds no row lock across its card
fetch. Before, the delegation's own session issued the UPDATE and waited on
any other holder of the row until the statement timeout, which cancelled the
statement and rolled the session back: the send never left.

Same scratch-database recipe as ``test_a2a_client_reverify_job.py``.
"""

from __future__ import annotations

import asyncio
import time
import uuid

import pytest
from sqlalchemy import select

from app.a2a.client.verification import Verdict
from app.a2a.providers import create_settings, sync_targets
from app.models.a2a import (
    DIRECTION_OUTBOUND,
    SIGNATURE_UNSIGNED,
    USABILITY_USABLE,
    A2AClientTarget,
)
from app.models.integration import Integration
from app.models.user import User
from tests.test_a2a_hooks_postgres import _temp_database


def _usable() -> Verdict:
    return Verdict(
        usability=USABILITY_USABLE,
        signature_state=SIGNATURE_UNSIGNED,
        agreed_version="1.0",
        pinned_origin="https://partner.example",
        fingerprint="fp-1",
        invocation_url="https://partner.example/rpc",
    )


async def _seed(sm) -> tuple[Integration, uuid.UUID]:
    async with sm() as session:
        user = User(
            id=uuid.uuid4(),
            email=f"contention-{uuid.uuid4().hex[:8]}@test.local",
            full_name="Contention",
            hashed_password="x",
        )
        session.add(user)
        await session.flush()
        integration = Integration(
            id=uuid.uuid4(),
            user_id=user.id,
            provider="oauth_a2a",
            auth_type="none",
            name="Partner",
        )
        session.add(integration)
        await session.flush()
        await create_settings(session, integration.id, DIRECTION_OUTBOUND)
        await sync_targets(session, integration.id, ["https://partner.example/agent-card.json"])
        await session.commit()
        target_id = (
            await session.execute(select(A2AClientTarget.id).where(A2AClientTarget.integration_id == integration.id))
        ).scalar_one()
        return integration, target_id


def _patch(monkeypatch, sm, fetches: list):
    from app.a2a.client import registration

    async def _fetch(_integration, target, *, trigger):
        fetches.append(target.id)
        return _usable(), None

    monkeypatch.setattr(registration, "_fetch_verdict", _fetch)
    monkeypatch.setattr("app.core.database.async_session", sm)
    monkeypatch.setattr(registration, "_CONTENDED_WAIT_SECONDS", 0.5)
    monkeypatch.setattr(registration, "_CONTENDED_POLL_SECONDS", 0.05)


@pytest.mark.integration
class TestDelegationReverification:
    @pytest.mark.asyncio
    async def test_a_held_row_refuses_fast_and_leaves_the_caller_session_usable(self, monkeypatch):
        from app.a2a.client import registration

        async with _temp_database() as sm:
            integration, target_id = await _seed(sm)
            _patch(monkeypatch, sm, [])
            async with sm() as holder, sm() as caller:
                # The sweep, mid-verification, as it used to hold the row.
                await holder.execute(select(A2AClientTarget).where(A2AClientTarget.id == target_id).with_for_update())
                target = (
                    await caller.execute(select(A2AClientTarget).where(A2AClientTarget.id == target_id))
                ).scalar_one()
                started = time.monotonic()
                with pytest.raises(registration.VerdictWriteContendedError):
                    await registration.reverify_before_delegating(integration, target)
                assert time.monotonic() - started < 5
                # Not rolled back, not dirtied: the send's session goes on.
                assert not caller.dirty
                await caller.execute(select(A2AClientTarget.id))
                await caller.commit()
                await holder.rollback()

    @pytest.mark.asyncio
    async def test_the_verdict_is_committed_without_the_caller_session(self, monkeypatch):
        from app.a2a.client import registration

        async with _temp_database() as sm:
            integration, target_id = await _seed(sm)
            _patch(monkeypatch, sm, [])
            async with sm() as caller:
                target = (
                    await caller.execute(select(A2AClientTarget).where(A2AClientTarget.id == target_id))
                ).scalar_one()
                await registration.reverify_before_delegating(integration, target)
                assert target.usability == USABILITY_USABLE
                assert target.last_verified_at is not None
                assert not caller.dirty
                await caller.rollback()
            # Committed by its own session: the caller's rollback undid nothing.
            async with sm() as reader:
                row = (
                    await reader.execute(select(A2AClientTarget).where(A2AClientTarget.id == target_id))
                ).scalar_one()
                assert row.usability == USABILITY_USABLE
                assert row.last_verified_at is not None

    @pytest.mark.asyncio
    async def test_two_concurrent_delegations_both_proceed(self, monkeypatch):
        from app.a2a.client import registration

        async with _temp_database() as sm:
            integration, target_id = await _seed(sm)
            fetches: list = []
            _patch(monkeypatch, sm, fetches)
            async with sm() as first, sm() as second:
                targets = [
                    (await s.execute(select(A2AClientTarget).where(A2AClientTarget.id == target_id))).scalar_one()
                    for s in (first, second)
                ]
                await asyncio.gather(*(registration.reverify_before_delegating(integration, t) for t in targets))
                assert all(t.usability == USABILITY_USABLE for t in targets)
                assert targets[0].last_verified_at is not None
                assert targets[1].last_verified_at is not None


@pytest.mark.integration
class TestSweepHoldsNoLockAcrossTheFetch:
    @pytest.mark.asyncio
    async def test_the_row_is_free_while_the_card_is_fetched(self, monkeypatch):
        from app.a2a.client import registration, sweep
        from tests.a2a_chat_gate import stub_chat_gate

        stub_chat_gate(monkeypatch)

        async def _no_discovery(_db, _integration):
            return (0, 0)

        monkeypatch.setattr(registration, "discover_partner_agents", _no_discovery)

        async with _temp_database() as sm:
            _integration, target_id = await _seed(sm)
            free_during_fetch: list = []

            async def _fetch(_integration, target, *, trigger):
                # From another session, mid-fetch: NOWAIT raises if the sweep
                # still holds the row.
                async with sm() as other:
                    await other.execute(
                        select(A2AClientTarget.id).where(A2AClientTarget.id == target_id).with_for_update(nowait=True)
                    )
                    free_during_fetch.append(True)
                    await other.rollback()
                return _usable(), None

            monkeypatch.setattr(registration, "_fetch_verdict", _fetch)
            async with sm() as db:
                assert await sweep.sweep_once(db, limit=10) == 1
            assert free_during_fetch == [True]
            async with sm() as reader:
                row = (
                    await reader.execute(select(A2AClientTarget).where(A2AClientTarget.id == target_id))
                ).scalar_one()
                assert row.last_verified_at is not None
