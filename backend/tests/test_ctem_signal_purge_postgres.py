# SPDX-License-Identifier: Apache-2.0

"""#2528 on PostgreSQL: the purge deletes every signal and its cases with no orphan, in batches,
leaves the other cases, sources and baseline hosts, and the next attack surface pass raises again."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

import app.models  # noqa: F401 - registers every mapper on Base.metadata
from app.models.audit_log import AuditLog
from app.models.ctem_case import CtemCase, CtemCaseEvent, CtemCaseIssue
from app.models.ctem_signal import CtemSignal
from app.models.ctem_surface_observation import CtemSurfaceObservation
from app.models.organization import Organization
from app.models.user import User
from app.models.work_item import WorkItem
from app.services import ctem_attack_surface as surface
from app.services import ctem_signal_purge as purge
from app.services import organization_context as oc
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration

NOW = datetime(2026, 11, 26, 16, 0, tzinfo=UTC)


@asynccontextmanager
async def _database() -> AsyncIterator[async_sessionmaker]:
    async with scratch_database("ctemsignalpurge", tables=ALL_TABLES, extensions=("vector",)) as db:
        yield db.sessions


@pytest.fixture(autouse=True)
def _quiet(monkeypatch: pytest.MonkeyPatch) -> None:
    """No Redis, no remote read, no gate inside the purge: it only deletes."""

    async def _down() -> Any:
        raise ConnectionError("no redis in this test")

    async def _no_gate(db: Any, row: Any, *, created: bool) -> None:
        return None

    monkeypatch.setattr(purge, "_redis", _down)
    monkeypatch.setitem(purge._LOCAL, "paused", False)
    monkeypatch.setattr("app.services.ctem_scope_gate_store.evaluate_after_record", _no_gate)


def _signal(org: uuid.UUID, n: int, **kw: Any) -> CtemSignal:
    return CtemSignal(
        organization_id=org,
        trigger_path=kw.pop("trigger_path", "new_vulnerability"),
        lens="world",
        source_provider="opencti",
        subject_kind="software",
        subject_ref=f"cpe:/a:vendor:product{n}",
        dedupe_key=f"d-{n}",
        state=kw.pop("state", "confirmed"),
        recorded_via="listener",
        recorded_by="system",
        **kw,
    )


async def _world(session: Any, *, signals: int = 3) -> dict[str, Any]:
    org = Organization(id=uuid.uuid4(), name="Org", role="primary")
    admin = User(id=uuid.uuid4(), email="admin@test.local", full_name="Admin", hashed_password="x", role="admin")
    session.add_all([org, admin])
    await session.flush()
    opened = CtemCase(organization_id=org.id, title="Upgrade A", remediation_kind="upgrade", keys=["k:a"], state="new")
    joined = CtemCase(organization_id=org.id, title="Upgrade B", remediation_kind="upgrade", keys=["k:b"], state="new")
    legacy = CtemCase(organization_id=org.id, title="Legacy", remediation_kind="upgrade", keys=["k:c"], state="new")
    session.add_all([opened, joined, legacy])
    await session.flush()
    session.add_all(
        [
            CtemCaseEvent(case_id=opened.id, actor="system", kind="created", to_value="k:a", reason="signal s0"),
            CtemCaseIssue(case_id=joined.id, source="signal", external_id="s1"),
            CtemCaseEvent(case_id=joined.id, actor="system", kind="signal", issue="signal:s1"),
            CtemCaseEvent(case_id=legacy.id, actor="system", kind="created", to_value="k:c"),
            CtemCaseIssue(case_id=legacy.id, source="opencti", external_id="vuln--1"),
            WorkItem(user_id=admin.id, title="gate", correlation_key=f"ctem:review:{opened.id}", status="blocked"),
            WorkItem(user_id=admin.id, title="kept", correlation_key=f"ctem:review:{legacy.id}", status="open"),
        ]
    )
    rows = [_signal(org.id, 0, state="case_opened", case_id=opened.id)]
    rows += [_signal(org.id, n) for n in range(1, signals)]
    session.add_all(rows)
    session.add(
        CtemSurfaceObservation(
            organization_id=org.id,
            host="dev.corp.example",
            ip="203.0.113.20",
            ports=[{"port": 22, "transport": "tcp", "product": ""}],
            signalled=["surface_drift:attack_surface:dev.corp.example:port:22/tcp"],
            first_seen_at=NOW - timedelta(days=3),
            last_seen_at=NOW - timedelta(days=1),
        )
    )
    await session.commit()
    return {"org": org.id, "admin": admin.id, "opened": opened.id, "joined": joined.id, "legacy": legacy.id}


async def _progress(_n: int) -> None:
    return None


async def test_counts_name_every_signal_and_every_case_a_signal_opened_or_joined() -> None:
    async with _database() as sessions, sessions() as session:
        await _world(session)
        assert await purge.counts(session) == {"signals": 3, "cases": 2}


async def test_the_job_deletes_signals_and_their_cases_with_no_orphan() -> None:
    async with _database() as sessions, sessions() as session:
        world = await _world(session)
        admin = await session.get(User, world["admin"])
        # The job id is a UUID in production (start_job): the audit row stores it as entity_id.
        status = purge.new_status(str(uuid.uuid4()), admin, resync=False)
        done = await purge.run_job(status, world["admin"], sessions=sessions)
        assert done["state"] == "done", done["error"]
        assert done["steps"]["purge"]["counts"] == {
            "cases": 2,
            "signals": 3,
            "surface_hosts": 1,
            "surface_hosts_rearmed": 0,
        }
        session.expire_all()
        assert await session.scalar(select(func.count()).select_from(CtemSignal)) == 0
        assert {c.id for c in (await session.execute(select(CtemCase))).scalars()} == {world["legacy"]}
        # No member, history or open work item of a purged case is left.
        left_issues = {i.case_id for i in (await session.execute(select(CtemCaseIssue))).scalars()}
        left_events = {e.case_id for e in (await session.execute(select(CtemCaseEvent))).scalars()}
        assert left_issues == {world["legacy"]} and left_events == {world["legacy"]}
        items = {i.correlation_key: i.status for i in (await session.execute(select(WorkItem))).scalars()}
        assert items[f"ctem:review:{world['opened']}"] == "cancelled"
        assert items[f"ctem:review:{world['legacy']}"] == "open"
        # The baseline host stays; its raised key goes with the port it raised, so the next pass sees it again.
        host = (await session.execute(select(CtemSurfaceObservation))).scalar_one()
        assert host.signalled == [] and host.ports == [] and host.ip == "203.0.113.20"
        audits = (await session.execute(select(AuditLog))).scalars().all()
        job = [a for a in audits if a.entity_type == "ctem_signal_purge"]
        assert len(job) == 1 and job[0].details["resync"] is False
        assert job[0].details["steps"]["purge"]["counts"]["signals"] == 3
        assert sum(1 for a in audits if a.entity_type == "ctem_case") == 2  # one tombstone per case


async def test_a_large_set_is_purged_in_batches(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(purge, "SIGNAL_BATCH", 500)
    seen: list[int] = []

    async def _track(n: int) -> None:
        seen.append(n)

    async with _database() as sessions, sessions() as session:
        await _world(session, signals=4400)
        assert await purge.purge_signals(sessions, progress=_track) == 4400
        assert seen == [500 * k for k in range(1, 9)] + [4400]
        assert await session.scalar(select(func.count()).select_from(CtemSignal)) == 0


async def test_after_a_purge_the_same_pass_raises_the_same_outside_signals_again() -> None:
    profile = oc.parse_profile({"owned_domains": ["corp.example"], "address_ranges": ["203.0.113.0/24"]})

    class _Reader:
        provider = "shodan"

        async def observe(self, domains: list[str], ranges: list[str]) -> list[surface.Observation]:
            ports = [surface.Port(22), surface.Port(8080)]
            return [surface.Observation(host="dev.corp.example", ip="203.0.113.20", ports=ports)]

    async with _database() as sessions, sessions() as session:
        world = await _world(session, signals=1)
        first = await surface.run_pass(
            session, organization_id=world["org"], profile=profile, reader=_Reader(), now=NOW
        )
        await session.commit()
        assert first["counts"].get("surface_drift") == 1  # 8080 is new against the baseline
        again = await surface.run_pass(
            session, organization_id=world["org"], profile=profile, reader=_Reader(), now=NOW
        )
        await session.commit()
        assert again["counts"]["recorded"] == 0  # two identical passes record nothing

        admin = await session.get(User, world["admin"])
        done = await purge.run_job(
            purge.new_status(str(uuid.uuid4()), admin, resync=False), world["admin"], sessions=sessions
        )
        assert done["state"] == "done", done["error"]
        session.expire_all()

        after = await surface.run_pass(
            session, organization_id=world["org"], profile=profile, reader=_Reader(), now=NOW
        )
        await session.commit()
        keys = set((await session.execute(select(CtemSignal.dedupe_key))).scalars())
        # Both ports raised before the purge come back, once each.
        assert after["counts"]["recorded"] == 2
        assert keys == {
            "surface_drift:attack_surface:dev.corp.example:port:22/tcp",
            "surface_drift:attack_surface:dev.corp.example:port:8080/tcp",
        }
        twice = await surface.run_pass(
            session, organization_id=world["org"], profile=profile, reader=_Reader(), now=NOW
        )
        assert twice["counts"]["recorded"] == 0  # and the pass dedupes again from there
