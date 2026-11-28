# SPDX-License-Identifier: Apache-2.0

"""#2581 and #2726 on PostgreSQL: the retention of gate-discarded, watchlist and unmatched signals.

A discarded signal past its window goes, one inside it stays; a signal a person
dismissed and a signal in a case stay whatever their age; a watchlist signal past
its window goes; an unmatched (``to_validate``) signal past its window goes, one
inside it, one dismissed and one in a case stay (#2726); a 0 window turns its rule
off; the deletes run in batches and the pass writes one audit row with the counts
per state. A profile change decides again only the unmatched signals seen within
the window, and a deleted unmatched signal seen again is a first sighting.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

import app.models  # noqa: F401 - registers every mapper on Base.metadata
from app.models.audit_log import AuditLog
from app.models.ctem_case import CtemCase, CtemCaseIssue
from app.models.ctem_signal import CtemSignal
from app.models.organization import Organization
from app.models.user import User
from app.services import ctem_signal_retention as retention
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration

NOW = datetime(2026, 11, 27, 9, 0, tzinfo=UTC)
WINDOWS = {"ctem_signal_discarded_days": 30, "ctem_signal_watchlist_days": 90, "ctem_signal_unmatched_days": 30}
UNMATCHED = {"reason_code": "subject_unresolved", "outcome": "to_validate"}


@asynccontextmanager
async def _database() -> AsyncIterator[async_sessionmaker]:
    async with scratch_database("ctemsignalretention", tables=ALL_TABLES, extensions=("vector",)) as db:
        yield db.sessions


def _signal(org: uuid.UUID, name: str, state: str, seen_days_ago: int, **kw: Any) -> CtemSignal:
    seen = NOW - timedelta(days=seen_days_ago)
    return CtemSignal(
        organization_id=org,
        trigger_path="new_vulnerability",
        lens="world",
        source_provider="opencti",
        subject_kind="software",
        subject_ref=f"cpe:/a:vendor:{name}",
        dedupe_key=name,
        state=state,
        recorded_via="listener",
        recorded_by="system",
        first_seen_at=seen,
        last_seen_at=seen,
        observed_at=seen,
        **kw,
    )


async def _world(session: Any) -> dict[str, uuid.UUID]:
    org = Organization(id=uuid.uuid4(), name="Org", role="primary")
    admin = User(id=uuid.uuid4(), email="admin@test.local", full_name="Admin", hashed_password="x", role="admin")
    session.add_all([org, admin])
    await session.flush()
    case = CtemCase(organization_id=org.id, title="Upgrade", remediation_kind="upgrade", keys=["k:a"], state="new")
    session.add(case)
    await session.flush()
    member = _signal(org.id, "member", "discarded", 400)
    unmatched_member = _signal(org.id, "unmatched-member", "to_validate", 400, decision=UNMATCHED)
    rows = [
        _signal(org.id, "discarded-31", "discarded", 31, decision={"reason_code": "out_of_scope"}),
        _signal(org.id, "discarded-29", "discarded", 29, decision={"reason_code": "out_of_scope"}),
        _signal(org.id, "discarded-no-decision", "discarded", 31),
        _signal(org.id, "dismissed-365", "discarded", 365, decision={"reason_code": "dismissed_by_user"}),
        _signal(org.id, "watchlist-91", "watchlist", 91),
        _signal(org.id, "watchlist-89", "watchlist", 89),
        _signal(org.id, "named-case", "watchlist", 400, case_id=case.id),
        _signal(org.id, "confirmed-400", "confirmed", 400),
        _signal(org.id, "unmatched-31", "to_validate", 31, decision=UNMATCHED),
        _signal(org.id, "unmatched-29", "to_validate", 29, decision=UNMATCHED),
        _signal(org.id, "unmatched-dismissed", "to_validate", 400, decision={"reason_code": "dismissed_by_user"}),
        _signal(org.id, "unmatched-named-case", "to_validate", 400, case_id=case.id, decision=UNMATCHED),
        member,
        unmatched_member,
    ]
    session.add_all(rows)
    await session.flush()
    session.add(CtemCaseIssue(case_id=case.id, source="signal", external_id=str(member.id)))
    session.add(CtemCaseIssue(case_id=case.id, source="signal", external_id=str(unmatched_member.id)))
    await session.commit()
    return {"org": org.id, "admin": admin.id}


async def _left(sessions: Any) -> set[str]:
    async with sessions() as session:
        return set((await session.execute(select(CtemSignal.dedupe_key))).scalars())


async def test_each_rule_deletes_what_it_names_and_keeps_the_rest() -> None:
    async with _database() as sessions:
        async with sessions() as session:
            await _world(session)
        deleted, more = await retention.purge_expired_signals(WINDOWS, now=NOW, sessions=sessions)
        assert deleted == {"discarded": 2, "watchlist": 1, "to_validate": 1} and more is False
        assert await _left(sessions) == {
            "discarded-29",
            "dismissed-365",
            "watchlist-89",
            "named-case",
            "confirmed-400",
            "member",
            "unmatched-29",
            "unmatched-dismissed",
            "unmatched-named-case",
            "unmatched-member",
        }


async def test_a_0_window_turns_its_rule_off() -> None:
    async with _database() as sessions:
        async with sessions() as session:
            await _world(session)
        deleted, _more = await retention.purge_expired_signals(
            {"ctem_signal_discarded_days": 0, "ctem_signal_watchlist_days": 90, "ctem_signal_unmatched_days": 0},
            now=NOW,
            sessions=sessions,
        )
        assert deleted == {"discarded": 0, "watchlist": 1, "to_validate": 0}
        assert {"discarded-31", "discarded-no-decision", "unmatched-31"} <= await _left(sessions)


async def test_the_deletes_run_in_batches_and_report_more_at_the_cap() -> None:
    async with _database() as sessions:
        async with sessions() as session:
            await _world(session)
        deleted, more = await retention.purge_expired_signals(
            WINDOWS, now=NOW, sessions=sessions, batch=1, max_batches=1
        )
        assert deleted == {"discarded": 1, "watchlist": 1, "to_validate": 1} and more is True
        deleted, more = await retention.purge_expired_signals(WINDOWS, now=NOW, sessions=sessions, batch=1)
        assert deleted == {"discarded": 1, "watchlist": 0, "to_validate": 0} and more is False


async def test_one_audit_row_per_pass_with_the_counts_per_state(monkeypatch: pytest.MonkeyPatch) -> None:
    async with _database() as sessions:
        async with sessions() as session:
            await _world(session)
        monkeypatch.setattr("app.core.database.async_session", sessions)
        deleted, _ = await retention.purge_expired_signals(WINDOWS, now=NOW, sessions=sessions)
        await retention.audit_signal_retention_pass(deleted, WINDOWS)
        async with sessions() as session:
            rows = list(
                (await session.execute(select(AuditLog).where(AuditLog.entity_type == "ctem_signal"))).scalars()
            )
        assert len(rows) == 1
        assert rows[0].details["deleted"] == {"discarded": 2, "watchlist": 1, "to_validate": 1}
        assert rows[0].details["retention_days"] == {"discarded": 30, "watchlist": 90, "to_validate": 30}


async def test_a_profile_change_decides_again_only_the_unmatched_signals_seen_within_the_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.models.organization_context import OrganizationContextVersion
    from app.services import ctem_scope_gate_store as store

    async with _database() as sessions:
        async with sessions() as session:
            ids = await _world(session)
            stale = NOW - timedelta(days=60)
            for row in (await session.execute(select(CtemSignal))).scalars():
                row.updated_at = stale
            session.add(
                OrganizationContextVersion(organization_id=ids["org"], version=2, created_at=NOW - timedelta(hours=1))
            )
            await session.commit()
        picked: list[str] = []

        async def _decide_again(_db: Any, rows: list[CtemSignal], **_kw: Any) -> dict[str, int]:
            picked.extend(row.dedupe_key for row in rows)
            return {}

        monkeypatch.setattr(store, "_decide_again", _decide_again)
        async with sessions() as session:
            await store.recheck_parked(session, now=NOW, seen_within_days=30)
        assert set(picked) == {"unmatched-29"}
        picked.clear()
        async with sessions() as session:
            await store.recheck_parked(session, now=NOW, seen_within_days=0)
        assert set(picked) == {
            "unmatched-29",
            "unmatched-31",
            "unmatched-dismissed",
            "unmatched-named-case",
            "unmatched-member",
        }


async def test_a_deleted_unmatched_signal_seen_again_is_a_first_sighting() -> None:
    from app.services import ctem_signals as signals

    async with _database() as sessions:
        async with sessions() as session:
            ids = await _world(session)
        await retention.purge_expired_signals(WINDOWS, now=NOW, sessions=sessions)
        assert "unmatched-31" not in await _left(sessions)
        async with sessions() as session:
            again, created = await signals.record_signal(
                session,
                organization_id=ids["org"],
                signal=signals.SignalInput(
                    trigger_path="new_vulnerability",
                    source_provider="opencti",
                    subject_kind="software",
                    subject_ref="cpe:/a:vendor:unmatched-31",
                    dedupe_key="unmatched-31",
                    observed_at=NOW,
                ),
                recorded_via="listener",
                recorded_by="system",
                now=NOW,
            )
            await session.commit()
        assert created is True
        assert again.state == "new" and again.seen_count == 1 and again.first_seen_at == NOW
