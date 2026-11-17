"""Which delegations a late callback may still reach, against a real database.

``addressed_delegation`` is one WHERE clause, and the property under test is
exactly which rows it admits. A session double would only replay whatever
predicate the test itself wrote down, which is the thing that can be wrong;
the clause has to be run.

What it must get right is a three-way split, not a two-way one:

* an outstanding row — the ordinary case — is addressable;
* a row concluded by anything that DECIDED something is not. The partner
  failed, the credential was rejected, the attempt hit its ceiling and
  cancelled the task at the partner. Nothing is still running.
* a row concluded by abandonment is addressable again. Abandonment cancels
  nothing: it is this side giving up on silence, and the work may well still
  be running. The callback that finally arrives is the answer the caller was
  promised, and a partner that did everything the protocol asks must not have
  it discarded.

Same scratch-database recipe as ``test_a2a_hooks_postgres.py``.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

import app.models  # noqa: F401 — registers every mapper on Base.metadata
from app.a2a.client.callbacks import addressed_delegation
from app.core.config import settings
from app.models.a2a import (
    OUTCOME_COMPLETED,
    OUTCOME_PARTNER_FAILED,
    OUTCOME_TIMEOUT,
    A2ADelegation,
)
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration


@asynccontextmanager
async def _temp_database() -> AsyncIterator[async_sessionmaker]:
    async with scratch_database("a2aaband", tables=ALL_TABLES, extensions=("vector",)) as db:
        yield db.sessions


async def _seed_owner(session):
    """The three rows a delegation's foreign keys require."""
    from app.models.agent import Agent
    from app.models.integration import Integration
    from app.models.user import User

    user = User(
        id=uuid.uuid4(),
        email=f"a2a-{uuid.uuid4().hex[:8]}@test.local",
        full_name="A2A Abandonment Test",
        hashed_password="x",
    )
    session.add(user)
    await session.flush()
    agent = Agent(id=uuid.uuid4(), name="Caller", user_id=user.id)
    integration = Integration(
        id=uuid.uuid4(),
        provider="oauth_a2a",
        name=f"partner-{uuid.uuid4().hex[:6]}",
        user_id=user.id,
    )
    session.add_all([agent, integration])
    await session.flush()
    return user, agent, integration


async def _delegation(session, owner, **kw) -> A2ADelegation:
    user, agent, integration = owner
    fields = {
        "id": uuid.uuid4(),
        "integration_id": integration.id,
        "agent_id": agent.id,
        "user_id": user.id,
        "partner_task_id": f"task-{uuid.uuid4().hex[:8]}",
        "protocol_version": "0.3",
        "signature_state": "unsigned",
        "submitted_at": datetime.now(UTC),
    }
    fields.update(kw)
    row = A2ADelegation(**fields)
    session.add(row)
    await session.flush()
    return row


class TestWhichDelegationsALateCallbackReaches:
    async def test_the_three_way_split(self):
        async with _temp_database() as sessionmaker, sessionmaker() as session:
            owner = await _seed_owner(session)

            outstanding = await _delegation(session, owner)
            decided = await _delegation(
                session,
                owner,
                outcome=OUTCOME_PARTNER_FAILED,
                terminal_at=datetime.now(UTC),
            )
            # The ceiling ran out and the task was cancelled at the
            # partner: a timeout, and one nothing may contradict.
            cancelled = await _delegation(
                session,
                owner,
                outcome=OUTCOME_TIMEOUT,
                terminal_at=datetime.now(UTC),
            )
            # One timestamp, not two calls to `now()`: `conclude_abandoned`
            # writes both from a single one, and the equality is the whole
            # discriminator.
            gave_up = datetime.now(UTC)
            abandoned = await _delegation(
                session,
                owner,
                outcome=OUTCOME_TIMEOUT,
                terminal_at=gave_up,
                abandoned_at=gave_up,
            )
            # Abandoned a whole further window ago. The secret is still
            # checked, so leaving this open is not a door — but it would make a
            # one-shot credential replayable for the row's whole retention, and
            # a partner silent this long is not coming back.
            _long_ago = datetime.now(UTC) - timedelta(
                seconds=settings.a2a_client_delegation_abandoned_seconds * 2
            )
            stale = await _delegation(
                session,
                owner,
                outcome=OUTCOME_TIMEOUT,
                terminal_at=_long_ago,
                abandoned_at=_long_ago,
            )
            # Abandoned, re-opened by a late callback, and the collect that
            # followed hit its ceiling — which cancels at the partner. Back to
            # `timeout`, with the first give-up's stamp still on it, and a
            # fresh `terminal_at` that says something else ended it.
            recancelled = await _delegation(
                session,
                owner,
                outcome=OUTCOME_TIMEOUT,
                terminal_at=datetime.now(UTC),
                abandoned_at=datetime.now(UTC) - timedelta(seconds=30),
                reopened_at=datetime.now(UTC) - timedelta(seconds=20),
            )
            # Abandoned, then collected for real by the very callback the
            # row above admits. The stamp stays — when this side gave up is
            # worth keeping — so the pair is what has to close the door.
            collected = await _delegation(
                session,
                owner,
                outcome=OUTCOME_COMPLETED,
                terminal_at=gave_up,
                abandoned_at=gave_up,
            )
            await session.commit()

            assert (
                await addressed_delegation(session, str(outstanding.id))
            ) is not None, "the ordinary case"
            assert (
                await addressed_delegation(session, str(decided.id))
            ) is None, "something decided this; nothing is still running"
            assert (
                await addressed_delegation(session, str(cancelled.id))
            ) is None, (
                "the ceiling's timeout cancelled at the partner, so a "
                "callback after it contradicts our own cancellation"
            )
            assert (
                await addressed_delegation(session, str(collected.id))
            ) is None, "already collected; the answer is not owed twice"
            assert (
                await addressed_delegation(session, str(stale.id))
            ) is None, "the replay window never closed"
            assert (
                await addressed_delegation(session, str(recancelled.id))
            ) is None, (
                "this side cancelled at the partner; a callback after that "
                "contradicts our own decision, stamp or no stamp"
            )
            got = await addressed_delegation(session, str(abandoned.id))
            assert got is not None and got.id == abandoned.id, (
                "abandonment cancelled nothing — the work may still be "
                "running, and this is the answer the caller was promised"
            )

    async def test_an_unknown_id_is_still_indistinguishable(self):
        """The clause widened, and the one property it must not lose is that a
        stranger cannot learn which delegations exist by asking."""
        async with _temp_database() as sessionmaker, sessionmaker() as session:
            assert await addressed_delegation(session, str(uuid.uuid4())) is None
            assert await addressed_delegation(session, "not-a-uuid") is None


class TestAnOperatorCanTellTheTwoTimeoutsApart:
    """``outcome`` says ``timeout`` for both, and they mean opposite things
    about the partner: the attempt's ceiling CANCELLED the task there, and
    abandonment cancelled nothing — so that work may still be running, and
    still costing. Which of the two happened is the question an operator asks
    about a timeout, so the history has to carry the answer.
    """

    async def test_the_history_carries_the_distinction(self):
        """Read off a rendered entry, not off the schema: a field that is
        declared and never populated passes a ``model_fields`` check and tells
        an operator nothing."""
        from app.api.v1.a2a_client import DelegationEntry

        async with _temp_database() as sessionmaker, sessionmaker() as session:
            owner = await _seed_owner(session)
            when = datetime.now(UTC)
            abandoned = await _delegation(
                session,
                owner,
                outcome=OUTCOME_TIMEOUT,
                terminal_at=when,
                abandoned_at=when,
            )
            cancelled = await _delegation(
                session, owner, outcome=OUTCOME_TIMEOUT, terminal_at=when
            )
            await session.commit()

            def _entry(row):
                return DelegationEntry(
                    id=str(row.id),
                    outcome=row.outcome,
                    protocol_version=row.protocol_version,
                    signature_state=row.signature_state,
                    abandoned_at=(
                        row.abandoned_at.isoformat() if row.abandoned_at else None
                    ),
                    submitted_at=row.submitted_at.isoformat(),
                )

            assert _entry(abandoned).abandoned_at is not None
            assert _entry(cancelled).abandoned_at is None
            assert _entry(abandoned).outcome == _entry(cancelled).outcome, (
                "the outcome is what cannot tell them apart"
            )


class TestCollectingALateCallbackEndsTheRowItReopened:
    """The defect the re-open exists to avoid, asserted rather than argued.

    ``_record`` concludes the row it finds still OPEN, keyed on the partner's
    task id within the integration. Collecting on a row that is still
    concluded finds nothing and inserts a second one — two rows for one
    exchange, with the first left outstanding forever. The re-open is what
    makes the existing row findable, and the stamp has to survive it: how this
    delegation ended is not the same question as whether we had given up on it
    before it did.
    """

    async def test_one_row_ends_and_it_is_the_one_that_was_abandoned(
        self, monkeypatch
    ):
        from sqlalchemy import func, select

        from app.a2a.client import callbacks as callback_svc

        async with _temp_database() as sessionmaker, sessionmaker() as session:
            owner = await _seed_owner(session)
            _user, _agent, integration = owner
            when = datetime.now(UTC)
            row = await _delegation(
                session,
                owner,
                outcome=OUTCOME_TIMEOUT,
                terminal_at=when,
                abandoned_at=when,
            )
            await session.commit()
            row_id, task_id = row.id, row.partner_task_id

            async def _claim(_db, _row):
                return True

            monkeypatch.setattr(callback_svc, "claim", _claim)

            target = type(
                "T", (), {"id": uuid.uuid4(), "declared_name": "P"}
            )()

            async def _target_of(_db, _id):
                return target

            monkeypatch.setattr(callback_svc, "_target_of", _target_of)

            async def _resume(*_a, **_k):
                return None

            monkeypatch.setattr(callback_svc, "_resume_caller", _resume)

            from app.a2a.client import delegation as delegation_mod
            from app.a2a.client.delegation import DelegationResult

            def _completed():
                return DelegationResult(
                    outcome=OUTCOME_COMPLETED,
                    result_text="done",
                    partner_task_id=task_id,
                    protocol_version="0.3",
                    signature_state="unsigned",
                )

            async def _delegate(db, integration_, target_, **kw):
                # What a real collect does on a terminal partner task: end the
                # row it finds still open, through the ordinary write path.
                await delegation_mod._record(
                    db,
                    integration_,
                    target_,
                    kw.get("calling"),
                    _completed(),
                    submitted_at=datetime.now(UTC),
                )
                return _completed()

            monkeypatch.setattr(delegation_mod, "delegate", _delegate)
            await callback_svc.on_partner_finished(
                session, row, mechanism="called_back"
            )

            total = (
                await session.execute(
                    select(func.count())
                    .select_from(A2ADelegation)
                    .where(A2ADelegation.integration_id == integration.id)
                )
            ).scalar_one()
            assert total == 1, "the collect wrote a second row for one exchange"
            ended = (
                await session.execute(
                    select(A2ADelegation).where(A2ADelegation.id == row_id)
                )
            ).scalar_one()
            assert ended.outcome == OUTCOME_COMPLETED, "the row never ended"
            assert ended.abandoned_at is not None, (
                "that we had given up is not erased by the answer arriving"
            )


class TestTheFloorDoesNotAbandonWhatIsBeingCollected:
    """Two actors reach one row, and only one of them goes through ``claim``.

    The floor's SELECT tests `unclaimed_or_stale()` once, for a batch of up to
    25 rows that are then polled one at a time. A callback arriving after that
    SELECT wins the claim and starts a collect bounded by the full ceiling —
    so by the time the floor reaches this row, "unclaimed" is minutes stale
    while the collect is very much alive. Abandoning there resumes the caller
    with "The partner never reported a terminal state" while the real answer
    is in flight, and the collect then writes a second row for the exchange
    because `_outstanding_row` no longer finds an open one.

    Which rows the predicate spares is a WHERE clause, so it is run here.
    """

    async def test_a_claimed_row_survives_the_floor(self, monkeypatch):
        from app.a2a.client import callbacks as callback_svc

        async with _temp_database() as sessionmaker, sessionmaker() as session:
            owner = await _seed_owner(session)
            held = await _delegation(
                session, owner, callback_claimed_at=datetime.now(UTC)
            )
            free = await _delegation(session, owner)
            stale_claim = await _delegation(
                session,
                owner,
                callback_claimed_at=datetime.now(UTC)
                - timedelta(
                    seconds=settings.a2a_client_delegation_ceiling_seconds * 3
                ),
            )
            await session.commit()

            async def _resume(*_a, **_k):
                return None

            monkeypatch.setattr(callback_svc, "_resume_caller", _resume)
            for row in (held, free, stale_claim):
                await callback_svc.conclude_abandoned(
                    session, row, mechanism="asked"
                )
                await session.refresh(row)

            assert held.outcome is None, (
                "abandoned a row a live collect was already ending"
            )
            assert free.outcome == OUTCOME_TIMEOUT, "nobody was on this one"
            assert stale_claim.outcome == OUTCOME_TIMEOUT, (
                "a claim whose holder died must not hide the row forever — "
                "the one case the floor exists to catch"
            )
