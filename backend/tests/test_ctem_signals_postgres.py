"""CTEM signals on PostgreSQL (#826): one row per recorder and dedupe key, filters, organization isolation.

The store against the real schema: the upsert keeps one row per
``(organization, recorder, dedupe_key)`` and moves ``last_seen_at`` forward
only; another recorder's key never reaches the row; the
state, decision and case written by the next steps survive a new sighting;
the same key in two organizations is two rows that never cross; the check
constraints refuse an unknown path and a ``case_opened`` signal with no case.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker

import app.models  # noqa: F401 - registers every mapper on Base.metadata
from app.models.ctem_signal import CtemSignal
from app.models.organization import Organization
from app.services import ctem_signals as signals
from tests.scratch_database import scratch_database, with_referenced_tables

pytestmark = pytest.mark.integration

T0 = datetime(2026, 11, 21, 6, 0, tzinfo=UTC)


@asynccontextmanager
async def _database() -> AsyncIterator[async_sessionmaker]:
    tables = with_referenced_tables([CtemSignal.__table__])
    async with scratch_database("ctemsignals", tables=tables) as db:
        yield db.sessions


async def _org(session, name: str) -> uuid.UUID:
    # The store keys on the organization id alone, not on its role.
    primary = name == "A"
    org = Organization(
        id=uuid.uuid4(),
        name=name,
        role="primary" if primary else "tracked",
        relationship=None if primary else "customer",
    )
    session.add(org)
    await session.flush()
    return org.id


def _leak(**kw) -> signals.SignalInput:
    base = {
        "trigger_path": "credential_leak",
        "source_provider": "manual",
        "subject_kind": "identity",
        "subject_ref": "svc-deploy@example.com",
    }
    base.update(kw)
    return signals.SignalInput(**base)


async def test_the_same_key_twice_is_one_row_with_last_seen_moved_forward_only():
    async with _database() as sessions, sessions() as session:
        org = await _org(session, "A")
        first, created = await signals.record_signal(
            session, organization_id=org, signal=_leak(observed_at=T0), recorded_via="api", recorded_by="u1"
        )
        await session.commit()
        later, again = await signals.record_signal(
            session,
            organization_id=org,
            signal=_leak(observed_at=T0 + timedelta(hours=2), evidence={"link": "https://x"}),
            recorded_via="tool",
            recorded_by="u1",
        )
        await session.commit()
        older, _ = await signals.record_signal(
            session,
            organization_id=org,
            signal=_leak(observed_at=T0 - timedelta(days=1)),
            recorded_via="api",
            recorded_by="u1",
        )
        await session.commit()
        rows = (await session.execute(select(CtemSignal))).scalars().all()
    assert (created, again) == (True, False)
    assert len(rows) == 1
    row = rows[0]
    assert row.id == first.id == later.id == older.id
    assert row.seen_count == 3
    assert row.last_seen_at == T0 + timedelta(hours=2)
    assert row.first_seen_at == T0
    assert row.recorded_by == "u1" and row.recorded_via == "api"


async def test_another_recorder_with_the_same_key_never_reaches_the_first_row():
    async with _database() as sessions, sessions() as session:
        org = await _org(session, "A")
        first, _ = await signals.record_signal(
            session,
            organization_id=org,
            signal=_leak(evidence={"note": "u1"}, subject_label="Deploy account"),
            recorded_via="api",
            recorded_by="u1",
            now=T0,
        )
        first.state = "watchlist"
        await session.commit()
        other, created = await signals.record_signal(
            session,
            organization_id=org,
            signal=_leak(evidence={"note": "u2"}, subject_label="forged"),
            recorded_via="api",
            recorded_by="u2",
            now=T0 + timedelta(hours=1),
        )
        await session.commit()
        stored = await signals.get_signal(session, first.id, organization_id=org, recorded_by="u1")
    assert created is True and other.id != first.id
    assert other.state == "new" and other.evidence == {"note": "u2"}
    assert stored is not None
    assert stored.evidence == {"note": "u1"} and stored.subject_label == "Deploy account"
    assert stored.seen_count == 1 and stored.last_seen_at == T0 and stored.state == "watchlist"


async def test_a_new_sighting_keeps_the_state_decision_and_case_of_the_next_steps():
    async with _database() as sessions, sessions() as session:
        org = await _org(session, "A")
        row, _ = await signals.record_signal(
            session, organization_id=org, signal=_leak(), recorded_via="api", recorded_by="u1", now=T0
        )
        row.state = "watchlist"
        row.decision = {"outcome": "watchlist", "reason": "below the scope threshold"}
        await session.commit()
        again, created = await signals.record_signal(
            session, organization_id=org, signal=_leak(), recorded_via="tool", recorded_by="u1", now=T0
        )
        await session.commit()
    assert created is False
    assert again.state == "watchlist"
    assert again.decision == {"outcome": "watchlist", "reason": "below the scope threshold"}


async def test_two_organizations_never_share_or_see_each_others_signals():
    async with _database() as sessions, sessions() as session:
        org_a = await _org(session, "A")
        org_b = await _org(session, "B")
        a, _ = await signals.record_signal(
            session, organization_id=org_a, signal=_leak(), recorded_via="api", recorded_by="u1"
        )
        b, created_b = await signals.record_signal(
            session, organization_id=org_b, signal=_leak(), recorded_via="api", recorded_by="u1"
        )
        await session.commit()
        rows_a, total_a, _ = await signals.list_signals(session, organization_id=org_a, recorded_by=None)
        cross = await signals.get_signal(session, b.id, organization_id=org_a, recorded_by=None)
    assert created_b is True and a.id != b.id
    assert total_a == 1 and [r.id for r in rows_a] == [a.id]
    assert cross is None


async def test_list_filters_by_state_path_lens_and_recorder():
    async with _database() as sessions, sessions() as session:
        org = await _org(session, "A")
        for path, kind, ref, who in (
            ("new_exposure", "asset", "admin.example.com", "u1"),
            ("stale_asset", "asset", "old.example.com", "u1"),
            ("blind_spot", "asset", "runner-7", "u2"),
            ("new_vulnerability", "vulnerability", "CVE-1", "system"),
        ):
            await signals.record_signal(
                session,
                organization_id=org,
                signal=signals.SignalInput(path, "manual", kind, ref),
                recorded_via="api",
                recorded_by=who,
            )
        row = (await session.execute(select(CtemSignal).where(CtemSignal.trigger_path == "blind_spot"))).scalar_one()
        row.state = "to_validate"
        await session.commit()
        outside, n_outside, counts = await signals.list_signals(
            session, organization_id=org, recorded_by=None, lens="outside"
        )
        validating, n_val, _ = await signals.list_signals(
            session, organization_id=org, recorded_by=None, state="to_validate"
        )
        vulns, _, _ = await signals.list_signals(
            session, organization_id=org, recorded_by=None, trigger_path="new_vulnerability"
        )
        _, n_u1, _ = await signals.list_signals(session, organization_id=org, recorded_by="u1")
    assert n_outside == 2 and {r.trigger_path for r in outside} == {"new_exposure", "stale_asset"}
    assert counts["new"] == 2 and counts["to_validate"] == 0
    assert n_val == 1 and validating[0].subject_ref == "runner-7"
    assert [r.subject_ref for r in vulns] == ["CVE-1"]
    assert n_u1 == 2


async def test_the_constraints_refuse_an_unknown_path_and_a_case_opened_signal_without_a_case():
    async with _database() as sessions:
        async with sessions() as session:
            org = await _org(session, "A")
            row, _ = await signals.record_signal(
                session, organization_id=org, signal=_leak(), recorded_via="api", recorded_by="u"
            )
            await session.commit()
            row.state = "case_opened"
            with pytest.raises(IntegrityError):
                await session.commit()
        async with sessions() as session:
            with pytest.raises(IntegrityError):
                await session.execute(
                    text(
                        "INSERT INTO ctem_signals (id, organization_id, trigger_path, lens, source_provider,"
                        " subject_kind, subject_ref, evidence, dedupe_key, recorded_via, recorded_by)"
                        " VALUES (:id, :org, 'new_cve', 'world', 'm', 'asset', 'a', '{}', 'k', 'api', 'u')"
                    ),
                    {"id": uuid.uuid4(), "org": org},
                )
