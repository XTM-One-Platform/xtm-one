# SPDX-License-Identifier: Apache-2.0

"""#2581 on PostgreSQL: the retention of gate-discarded and watchlist signals.

A discarded signal past its window goes, one inside it stays; a signal a person
dismissed and a signal in a case stay whatever their age; a watchlist signal past
its window goes; a 0 window turns its rule off; the deletes run in batches and the
pass writes one audit row with the counts per state.
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
WINDOWS = {"ctem_signal_discarded_days": 30, "ctem_signal_watchlist_days": 90}


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
    rows = [
        _signal(org.id, "discarded-31", "discarded", 31, decision={"reason_code": "out_of_scope"}),
        _signal(org.id, "discarded-29", "discarded", 29, decision={"reason_code": "out_of_scope"}),
        _signal(org.id, "discarded-no-decision", "discarded", 31),
        _signal(org.id, "dismissed-365", "discarded", 365, decision={"reason_code": "dismissed_by_user"}),
        _signal(org.id, "watchlist-91", "watchlist", 91),
        _signal(org.id, "watchlist-89", "watchlist", 89),
        _signal(org.id, "named-case", "watchlist", 400, case_id=case.id),
        _signal(org.id, "confirmed-400", "confirmed", 400),
        member,
    ]
    session.add_all(rows)
    await session.flush()
    session.add(CtemCaseIssue(case_id=case.id, source="signal", external_id=str(member.id)))
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
        assert deleted == {"discarded": 2, "watchlist": 1} and more is False
        assert await _left(sessions) == {
            "discarded-29",
            "dismissed-365",
            "watchlist-89",
            "named-case",
            "confirmed-400",
            "member",
        }


async def test_a_0_window_turns_its_rule_off() -> None:
    async with _database() as sessions:
        async with sessions() as session:
            await _world(session)
        deleted, _more = await retention.purge_expired_signals(
            {"ctem_signal_discarded_days": 0, "ctem_signal_watchlist_days": 90}, now=NOW, sessions=sessions
        )
        assert deleted == {"discarded": 0, "watchlist": 1}
        assert {"discarded-31", "discarded-no-decision"} <= await _left(sessions)


async def test_the_deletes_run_in_batches_and_report_more_at_the_cap() -> None:
    async with _database() as sessions:
        async with sessions() as session:
            await _world(session)
        deleted, more = await retention.purge_expired_signals(
            WINDOWS, now=NOW, sessions=sessions, batch=1, max_batches=1
        )
        assert deleted == {"discarded": 1, "watchlist": 1} and more is True
        deleted, more = await retention.purge_expired_signals(WINDOWS, now=NOW, sessions=sessions, batch=1)
        assert deleted == {"discarded": 1, "watchlist": 0} and more is False


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
        assert rows[0].details["deleted"] == {"discarded": 2, "watchlist": 1}
        assert rows[0].details["retention_days"] == {"discarded": 30, "watchlist": 90}
