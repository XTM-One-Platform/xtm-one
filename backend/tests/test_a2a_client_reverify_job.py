"""The sweep divides work across replicas rather than duplicating it.

Constitution Principle IV. This service runs as N replicas by default, and
code that assumes single-instance execution breaks the moment a deployment
scales past one — a lesson this project has already paid for twice.

``FOR UPDATE SKIP LOCKED`` rather than an advisory lock, because this is
row-claiming work: two replicas sweeping *different* registrations
concurrently is the desired outcome, not a race to avoid.
"""

from __future__ import annotations

import uuid

import pytest

from app.a2a.providers import create_settings, sync_targets
from app.models.a2a import DIRECTION_INBOUND, DIRECTION_OUTBOUND
from app.models.integration import Integration
from app.models.user import User
from tests.test_a2a_hooks_postgres import _temp_database


async def _seed(session, direction, count=1, cards_each=1):
    """*count* registrations, each owning *cards_each* partner agents.

    The sweep claims TARGETS, so a registration with no card seeds nothing for
    it to find — which is exactly the shape an inbound one has, and why these
    tests pass ``cards_each`` rather than assuming one row per registration.

    Targets are created through ``sync_targets``, the same door the save
    endpoint uses, so a test can never seed a shape the product cannot
    produce.
    """
    user = User(
        id=uuid.uuid4(),
        email=f"sweep-{uuid.uuid4().hex[:8]}@test.local",
        full_name="Sweep",
        hashed_password="x",
    )
    session.add(user)
    await session.flush()
    ids = []
    for _ in range(count):
        integration = Integration(
            id=uuid.uuid4(),
            user_id=user.id,
            provider="oauth_a2a",
            auth_type="none",
            name="Partner",
        )
        session.add(integration)
        await session.flush()
        await create_settings(session, integration.id, direction)
        # Only an outbound registration owns cards. An inbound one has no
        # partner agent to verify, which is what keeps it out of the sweep by
        # construction rather than by a check afterwards.
        if direction == DIRECTION_OUTBOUND:
            await sync_targets(
                session,
                integration.id,
                [
                    f"https://partner.example/{integration.id}/{n}/agent-card.json"
                    for n in range(cards_each)
                ],
            )
        ids.append(integration.id)
    return ids


@pytest.mark.integration
class TestDueSelection:
    @pytest.mark.asyncio
    async def test_a_never_verified_outbound_row_is_due(self):
        from app.a2a.client.sweep import due_targets

        async with _temp_database() as sm, sm() as session:
            (integration_id,) = await _seed(session, DIRECTION_OUTBOUND)
            due = await due_targets(session, limit=10)
            assert [d.integration_id for d in due] == [integration_id]

    @pytest.mark.asyncio
    async def test_a_partner_exposing_several_agents_yields_a_row_each(self):
        # The cardinality the target table exists for. Three agents behind one
        # credential have three verdicts that age independently, and sweeping
        # the registration could only ever refresh one of them.
        from app.a2a.client.sweep import due_targets

        async with _temp_database() as sm, sm() as session:
            (integration_id,) = await _seed(
                session, DIRECTION_OUTBOUND, cards_each=3
            )
            due = await due_targets(session, limit=10)
            assert [d.integration_id for d in due] == [integration_id] * 3
            assert len({d.card_url for d in due}) == 3

    @pytest.mark.asyncio
    async def test_inbound_rows_are_never_swept(self):
        # Excluded by the QUERY, not by a check afterwards: an inbound row has
        # no card to verify, and scanning them would grow with every partner
        # credential a deployment holds for the other direction.
        #
        # Seeded BESIDE a real outbound target rather than alone: on an empty
        # table this claim holds for free, and the assertion would keep passing
        # if the sweep started scanning registrations again.
        from app.a2a.client.sweep import due_targets

        async with _temp_database() as sm, sm() as session:
            await _seed(session, DIRECTION_INBOUND, count=3)
            (outbound_id,) = await _seed(session, DIRECTION_OUTBOUND)
            due = await due_targets(session, limit=10)
            assert [d.integration_id for d in due] == [outbound_id]

    @pytest.mark.asyncio
    async def test_the_batch_is_bounded(self):
        # A deployment with many partners spreads work across cycles rather
        # than holding one transaction open across all of them.
        from app.a2a.client.sweep import due_targets

        async with _temp_database() as sm, sm() as session:
            await _seed(session, DIRECTION_OUTBOUND, count=5)
            assert len(await due_targets(session, limit=2)) == 2


@pytest.mark.integration
class TestConcurrentSweepers:
    @pytest.mark.asyncio
    async def test_two_sweepers_divide_the_rows_rather_than_duplicating(self):
        # The property Principle IV is about. Two sessions claiming
        # concurrently must see disjoint sets — the second skips what the
        # first has locked rather than blocking on it or re-verifying it.
        from app.a2a.client.sweep import due_targets

        async with _temp_database() as sm:
            async with sm() as seed_session:
                await _seed(seed_session, DIRECTION_OUTBOUND, count=4)
                await seed_session.commit()

            async with sm() as first, sm() as second:
                claimed_first = await due_targets(first, limit=2)
                claimed_second = await due_targets(second, limit=2)

                # Compared by TARGET row, which is what SKIP LOCKED claims
                # now. Two replicas may legitimately hold different agents of
                # one partner; what must never happen is both holding the same
                # card and verifying it twice.
                ids_first = {r.id for r in claimed_first}
                ids_second = {r.id for r in claimed_second}
                assert ids_first and ids_second
                assert not (ids_first & ids_second), (
                    "two replicas claimed the same partner agent — SKIP LOCKED "
                    "is not doing its job, and both would verify it"
                )

    @pytest.mark.asyncio
    async def test_an_interrupted_cycle_is_safe_to_rerun(self):
        # Idempotency is nearly free here because the sweep writes a VERDICT
        # rather than performing a transition — but that is a property worth
        # pinning, not assuming.
        from app.a2a.client.sweep import due_targets

        async with _temp_database() as sm:
            async with sm() as session:
                ids = await _seed(session, DIRECTION_OUTBOUND, count=2)
                await session.commit()
            # A cycle that claimed rows and then died without committing.
            async with sm() as abandoned:
                await due_targets(abandoned, limit=10)
                await abandoned.rollback()
            # The next cycle finds them again, unchanged.
            async with sm() as retry:
                again = await due_targets(retry, limit=10)
                assert {r.integration_id for r in again} == set(ids)


@pytest.mark.integration
class TestOneRowPerTransaction:
    """A slow partner holds its row, not the batch: the claim's lock lasts
    until the transaction ends, and each row is followed by network work."""

    @pytest.mark.asyncio
    async def test_the_claim_is_released_before_the_next_row_is_verified(self, monkeypatch):
        from datetime import UTC, datetime

        from sqlalchemy import select

        from app.a2a.client import registration, sweep
        from app.models.a2a import A2AClientTarget
        from tests.a2a_chat_gate import stub_chat_gate

        stub_chat_gate(monkeypatch)

        async def _no_discovery(_db, _integration):
            return (0, 0)

        monkeypatch.setattr(registration, "discover_partner_agents", _no_discovery)

        async with _temp_database() as sm:
            async with sm() as seed:
                await _seed(seed, DIRECTION_OUTBOUND, count=2)
                await seed.commit()
                all_ids = set(
                    (await seed.execute(select(A2AClientTarget.id))).scalars().all()
                )

            seen: list = []

            async def _verify(db, integration, row, *, trigger):
                seen.append(row.id)
                # From another session, mid-verification: ``NOWAIT`` raises if
                # the sweep still holds a lock, so each claim is an assertion.
                async with sm() as other:
                    others = all_ids - {row.id}
                    # Every other row is claimable: the batch was not locked up front.
                    claimed = set(
                        (
                            await other.execute(
                                select(A2AClientTarget.id)
                                .where(A2AClientTarget.id.in_(others))
                                .with_for_update(nowait=True)
                            )
                        )
                        .scalars()
                        .all()
                    )
                    assert claimed == others
                    # And the row verified before this one has been released.
                    for done in seen[:-1]:
                        await other.execute(
                            select(A2AClientTarget.id)
                            .where(A2AClientTarget.id == done)
                            .with_for_update(nowait=True)
                        )
                    await other.rollback()
                row.last_verified_at = datetime.now(UTC)

            monkeypatch.setattr(registration, "verify_registration", _verify)

            async with sm() as db:
                assert await sweep.sweep_once(db, limit=10) == 2
            assert set(seen) == all_ids

    @pytest.mark.asyncio
    async def test_a_row_whose_verification_raised_is_not_claimed_twice(self, monkeypatch):
        # It stamped nothing, so it is still due; the cycle must move on.
        from app.a2a.client import registration, sweep
        from tests.a2a_chat_gate import stub_chat_gate

        stub_chat_gate(monkeypatch)

        async def _no_discovery(_db, _integration):
            return (0, 0)

        attempts: list = []

        async def _broken(_db, _integration, row, *, trigger):
            attempts.append(row.id)
            raise RuntimeError("a bug in verification")

        monkeypatch.setattr(registration, "discover_partner_agents", _no_discovery)
        monkeypatch.setattr(registration, "verify_registration", _broken)

        async with _temp_database() as sm:
            async with sm() as seed:
                await _seed(seed, DIRECTION_OUTBOUND, count=2)
                await seed.commit()
            async with sm() as db:
                assert await sweep.sweep_once(db, limit=10) == 0
            assert len(attempts) == 2
            assert len(set(attempts)) == 2
