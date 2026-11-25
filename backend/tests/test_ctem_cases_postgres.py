"""CTEM Cases on PostgreSQL (#11545): recompute keeps the user's work, expiry reopens.

The store against the real schema: a recompute adds members to a case a user
started and leaves its state, owner and due date alone; members all closed at
source resolve it with evidence; an expired accepted risk returns to needs
attention; the accepted-risk check constraint refuses an incomplete row.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest
from sqlalchemy import Table, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker

import app.models  # noqa: F401 - registers every mapper on Base.metadata
from app.models.ctem_case import CtemCase, CtemCaseEvent, CtemCaseIssue
from app.models.ctem_signal import CtemSignal
from app.models.organization import Organization
from app.models.user import User
from app.services import ctem_cases as rules
from app.services import ctem_cases_store as store
from tests.scratch_database import scratch_database, with_referenced_tables

pytestmark = pytest.mark.integration

NOW = datetime(2026, 11, 13, 12, 0, tzinfo=UTC)
KEY = "upgrade:apache/tomcat@9.0.98"


@asynccontextmanager
async def _database() -> AsyncIterator[async_sessionmaker]:
    models = (CtemCase, CtemCaseIssue, CtemCaseEvent, CtemSignal)
    tables = with_referenced_tables([cast(Table, m.__table__) for m in models])
    async with scratch_database("ctemcases", tables=tables) as db:
        yield db.sessions


def _issue(ext: str, cve: str) -> rules.SourceIssue:
    return rules.SourceIssue(
        source="opencti",
        external_id=ext,
        name=cve,
        cve=cve,
        product="Tomcat",
        vendor="Apache",
        fixed_version="9.0.98",
        cvss=9.8,
    )


async def _seed(session) -> tuple[uuid.UUID, uuid.UUID]:
    org = Organization(id=uuid.uuid4(), name="Org", role="primary")
    user = User(
        id=uuid.uuid4(), email=f"ctem-{uuid.uuid4().hex[:8]}@test.local", full_name="Owner", hashed_password="x"
    )
    session.add_all([org, user])
    await session.flush()
    return org.id, user.id


@pytest.fixture(autouse=True)
def _gate_opens_a_case(monkeypatch: pytest.MonkeyPatch) -> None:
    """The scope gate decides ``case`` on every source signal: these tests are about the case store (#1529)."""
    from app.services import ctem_scope_gate_store as gate_store

    async def evaluate(db: Any, row: CtemSignal, **_: Any) -> None:
        row.state = "confirmed"
        row.decision = {"outcome": "case", "state": "confirmed"}

    monkeypatch.setattr(gate_store, "evaluate", evaluate)


async def _members(session, case: CtemCase) -> dict[str, bool]:
    """The source issue members of *case*; the signals the issues recorded are members too, left out here."""
    rows = (await session.execute(select(CtemCaseIssue).where(CtemCaseIssue.case_id == case.id))).scalars()
    return {f"{r.source}:{r.external_id}": r.open for r in rows if r.source != "signal"}


async def test_recompute_keeps_state_owner_and_due_date_and_resolves_when_all_closed():
    async with _database() as sessions, sessions() as session:
        org_id, owner_id = await _seed(session)
        counts = await store.apply_recompute(
            session, organization_id=org_id, vertical="cyber", issues=[_issue("v1", "CVE-1")], now=NOW
        )
        assert counts == {
            "created": 1,
            "updated": 0,
            "resolved": 0,
            "reopened": 0,
            "signals": 1,
            "no_known_fix": 0,
            "not_affected": 0,
        }
        case = (await session.execute(select(CtemCase))).scalar_one()
        due = NOW + timedelta(days=14)
        await store.update_case(
            session,
            case,
            actor=str(owner_id),
            actor_kind="user",
            state="remediating",
            owner_user_id=owner_id,
            due_at=due,
            now=NOW,
        )
        await session.commit()

        counts = await store.apply_recompute(
            session,
            organization_id=org_id,
            vertical="cyber",
            issues=[_issue("v1", "CVE-1"), _issue("v2", "CVE-2")],
            now=NOW + timedelta(hours=1),
        )
        await session.commit()
        assert counts["updated"] == 1 and counts["created"] == 0
        await session.refresh(case)
        assert (case.state, case.owner_user_id, case.due_at) == ("remediating", owner_id, due)
        assert await _members(session, case) == {"opencti:v1": True, "opencti:v2": True}

        counts = await store.apply_recompute(
            session,
            organization_id=org_id,
            vertical="cyber",
            issues=[],
            closed=["opencti:v1", "opencti:v2"],
            now=NOW + timedelta(hours=2),
        )
        await session.commit()
        await session.refresh(case)
        assert counts["resolved"] == 1 and case.state == "resolved"
        assert case.owner_user_id == owner_id
        last = (
            (
                await session.execute(
                    select(CtemCaseEvent).where(CtemCaseEvent.kind == "state").order_by(CtemCaseEvent.at.desc())
                )
            )
            .scalars()
            .first()
        )
        assert last.evidence == rules.CLOSED_AT_SOURCE and last.actor == rules.SYSTEM_ACTOR
        assert await _members(session, case) == {"opencti:v1": False, "opencti:v2": False}


async def test_an_expired_acceptance_returns_to_new():
    async with _database() as sessions, sessions() as session:
        org_id, owner_id = await _seed(session)
        await store.apply_recompute(
            session, organization_id=org_id, vertical="cyber", issues=[_issue("v1", "CVE-1")], now=NOW
        )
        case = (await session.execute(select(CtemCase))).scalar_one()
        await store.update_case(
            session,
            case,
            actor=str(owner_id),
            actor_kind="user",
            state="accepted_risk",
            owner_user_id=owner_id,
            reason="compensating control",
            accepted_until=NOW + timedelta(days=7),
            now=NOW,
        )
        await session.commit()
        assert await store.expire_acceptances(session, now=NOW + timedelta(days=6)) == 0
        assert await store.expire_acceptances(session, now=NOW + timedelta(days=7)) == 1
        await session.commit()
        await session.refresh(case)
        assert (case.state, case.accepted_reason, case.owner_user_id) == (
            "new",
            "compensating control",
            owner_id,
        )


async def test_a_manual_split_pins_its_issues_through_recompute():
    async with _database() as sessions, sessions() as session:
        org_id, owner_id = await _seed(session)
        issues = [_issue("v1", "CVE-1"), _issue("v2", "CVE-2")]
        await store.apply_recompute(session, organization_id=org_id, vertical="cyber", issues=issues, now=NOW)
        source = (await session.execute(select(CtemCase))).scalar_one()
        manual = await store.create_manual_case(
            session,
            organization_id=org_id,
            vertical="cyber",
            title="Tomcat on the DMZ",
            member_ids=["opencti:v2"],
            visible={"opencti:v1", "opencti:v2"},
            actor=str(owner_id),
            now=NOW,
        )
        await session.commit()
        assert await _members(session, source) == {"opencti:v1": True}
        assert await _members(session, manual) == {"opencti:v2": True} and manual.pinned_issues == ["opencti:v2"]

        counts = await store.apply_recompute(
            session, organization_id=org_id, vertical="cyber", issues=issues, now=NOW + timedelta(hours=1)
        )
        await session.commit()
        assert counts["created"] == 0
        await session.refresh(manual)
        assert await _members(session, source) == {"opencti:v1": True}
        assert await _members(session, manual) == {"opencti:v2": True} and manual.state == "new"
        with pytest.raises(rules.CaseRuleError):
            await store.create_manual_case(
                session,
                organization_id=org_id,
                vertical="cyber",
                title="x",
                member_ids=["opencti:nope"],
                visible={"opencti:nope"},
                actor=str(owner_id),
            )


async def test_two_users_with_different_visibility_share_cases_and_see_only_their_issues():
    """#11613: Alice sees CVE-1 and CVE-2 in OpenCTI, Bob only CVE-1 (another group, other markings).

    One shared case. Bob's recompute (he cannot see CVE-2) neither closes nor
    shrinks it; rendered for Bob, CVE-2 is a bare Restricted row; Carol (sees neither) does not see the case or
    its count; Bob cannot split CVE-2 out; nothing of the source is stored. Since #1930
    Bob's detail aggregates are unknown (null with aggregates_restricted), never the
    CVE-1-only figures a reader takes for the case's.
    """
    from app.services import ctem_cases_view as view_of

    def _view(*issues: rules.SourceIssue) -> view_of.ViewerView:
        return view_of.ViewerView(issues={i.member_id: i for i in issues}, open_ids={i.member_id for i in issues})

    one = _issue("v1", "CVE-1")
    two = rules.SourceIssue(
        source="opencti",
        external_id="v2",
        name="CVE-2",
        cve="CVE-2",
        product="Tomcat",
        vendor="Apache",
        fixed_version="9.0.98",
        cvss=10.0,
        kev=True,
        assets=("secret-host",),
    )
    alice, bob, carol = _view(one, two), _view(one), _view()
    async with _database() as sessions, sessions() as session:
        org_id, _ = await _seed(session)
        await store.apply_recompute(session, organization_id=org_id, vertical="cyber", issues=[one, two], now=NOW)
        await session.commit()
        await store.apply_recompute(
            session, organization_id=org_id, vertical="cyber", issues=[one], now=NOW + timedelta(hours=1)
        )
        await session.commit()

        (stored,) = await store.list_cases(session, organization_id=org_id, vertical="cyber")
        case, rows = stored
        assert case.state == "new"
        issue_rows = {f"{r.source}:{r.external_id}": r.open for r in rows if r.source != "signal"}
        assert issue_rows == {"opencti:v1": True, "opencti:v2": True}
        # Nothing of the source in the database: no name, score, asset or count column.
        for table in (CtemCase.__table__, CtemCaseIssue.__table__):
            assert not {"name", "cvss", "epss", "kev", "assets", "issue_count", "priority"} & set(table.c.keys())

        # Each viewer sees the source signals of the issues they see (#1529), the rule of viewer_view.
        alice = replace(alice, signals=await view_of.source_signal_members(session, set(alice.issues)))
        bob = replace(bob, signals=await view_of.source_signal_members(session, set(bob.issues)))
        assert len(alice.signals) == 2 and len(bob.signals) == 1
        case_id, rows, events = await store.get_case(session, case.id, organization_id=org_id)
        for_alice = view_of.hydrate(case_id, rows, events, alice)
        for_bob = view_of.hydrate(case_id, rows, events, bob)
        assert for_alice is not None and for_bob is not None
        assert for_alice["restricted_count"] == 0 and for_alice["max_cvss"] == 10.0
        # CVE-2 and the signal it recorded are both restricted for Bob.
        assert for_bob["restricted_count"] == 2 and for_bob["members_without_details"] == 1
        assert for_bob["aggregates_restricted"] is True
        assert for_bob["max_cvss"] is None and for_bob["kev_count"] is None
        assert for_alice["aggregates_restricted"] is False and for_alice["members_without_details"] == 0
        assert {"restricted": True} in for_bob["issues"]
        assert "CVE-2" not in str(for_bob) and "secret-host" not in str(for_bob) and "opencti:v2" not in str(for_bob)
        assert view_of.hydrate(case_id, rows, events, carol) is None
        listed, counts = view_of.list_for_viewer([(case, rows)], carol)
        assert listed == [] and counts == {}
        assert view_of.owner_sees(rows, bob) and not view_of.owner_sees(rows, carol)
        with pytest.raises(rules.CaseRuleError):
            await store.create_manual_case(
                session,
                organization_id=org_id,
                vertical="cyber",
                title="steal",
                member_ids=["opencti:v2"],
                visible=set(bob.open_ids),
                actor="bob",
            )


def _other(ext: str, cve: str) -> rules.SourceIssue:
    return rules.SourceIssue(
        source="opencti",
        external_id=ext,
        name=cve,
        cve=cve,
        product="OpenSSL",
        vendor="OpenSSL",
        fixed_version="3.0.15",
    )


async def test_a_manual_merge_keeps_the_survivor_and_maps_every_key_through_recompute():
    """S5c: the survivor keeps state, owner and due date, takes the keys, members and history."""
    async with _database() as sessions, sessions() as session:
        org_id, owner_id = await _seed(session)
        issues = [_issue("v1", "CVE-1"), _other("o1", "CVE-9")]
        await store.apply_recompute(session, organization_id=org_id, vertical="cyber", issues=issues, now=NOW)
        cases = {c.title: c for c in (await session.execute(select(CtemCase))).scalars()}
        assert len(cases) == 2
        target, merged = sorted(cases.values(), key=lambda c: "Tomcat" not in c.title)
        merged_id, merged_keys = merged.id, list(merged.keys)
        await store.update_case(
            session,
            target,
            actor=str(owner_id),
            actor_kind="user",
            state="remediating",
            owner_user_id=owner_id,
            now=NOW,
        )
        await session.commit()

        await store.merge_cases(
            session,
            organization_id=org_id,
            vertical="cyber",
            target_id=target.id,
            source_ids=[merged_id],
            actor=str(owner_id),
            now=NOW,
        )
        await session.commit()
        await session.refresh(target)
        assert (target.state, target.owner_user_id) == ("remediating", owner_id)
        assert set(target.keys) == {KEY, *merged_keys}
        assert await _members(session, target) == {"opencti:v1": True, "opencti:o1": True}
        assert (await session.get(CtemCase, merged_id)) is None
        kinds = [
            e.kind
            for e in (await session.execute(select(CtemCaseEvent).where(CtemCaseEvent.case_id == target.id))).scalars()
        ]
        assert kinds.count("created") == 2 and "merge" in kinds

        counts = await store.apply_recompute(
            session, organization_id=org_id, vertical="cyber", issues=issues, now=NOW + timedelta(hours=1)
        )
        await session.commit()
        assert counts["created"] == 0
        assert len((await session.execute(select(CtemCase))).scalars().all()) == 1
        with pytest.raises(rules.CaseRuleError):
            await store.merge_cases(
                session,
                organization_id=org_id,
                vertical="cyber",
                target_id=target.id,
                source_ids=[target.id],
                actor=str(owner_id),
            )


async def test_a_merged_duplicate_never_comes_back_and_the_survivor_keeps_its_sla() -> None:
    """#2037: merge, then two recompute passes: no new case, no ctem_case.opened, the SLA of the survivor kept."""
    from app.services import ctem_case_loop as loop

    async with _database() as sessions, sessions() as session:
        org_id, _ = await _seed(session)
        issues = [_issue("v1", "CVE-1"), _other("o1", "CVE-9")]
        await store.apply_recompute(session, organization_id=org_id, vertical="cyber", issues=issues, now=NOW)
        await session.commit()
        target, merged = sorted(
            (await session.execute(select(CtemCase))).scalars(), key=lambda c: "Tomcat" not in c.title
        )
        sla, target_id = target.sla_due_at, target.id
        await store.merge_cases(
            session,
            organization_id=org_id,
            vertical="cyber",
            target_id=target_id,
            source_ids=[merged.id],
            actor="agent-1",
            now=NOW,
        )
        await session.commit()
        for hours in (1, 2):
            events = loop.PendingEvents()
            counts = await store.apply_recompute(
                session,
                organization_id=org_id,
                vertical="cyber",
                issues=issues,
                events=events,
                now=NOW + timedelta(hours=hours),
            )
            await session.commit()
            assert counts["created"] == 0 and loop.CASE_OPENED not in _names(events)
        (survivor,) = (await session.execute(select(CtemCase))).scalars().all()
        assert survivor.id == target_id and survivor.sla_due_at == sla
        merge = (
            await session.execute(
                select(CtemCaseEvent).where(CtemCaseEvent.case_id == target_id, CtemCaseEvent.kind == "merge")
            )
        ).scalar_one()
        assert merge.actor == "agent-1"


async def test_a_vendor_less_spelling_of_an_open_case_product_opens_no_second_case() -> None:
    """#2037: MinIO under minio/minio and -/minio, same CVEs and assets: one case."""

    def _minio(ext: str, cve: str, vendor: str | None, product: str) -> rules.SourceIssue:
        return rules.SourceIssue(
            source="opencti", external_id=ext, name=cve, cve=cve, product=product, vendor=vendor, cvss=9.8
        )

    async with _database() as sessions, sessions() as session:
        org_id, _ = await _seed(session)
        first = [_minio("m1", "CVE-2023-28432", None, "minio")]
        await store.apply_recompute(session, organization_id=org_id, vertical="cyber", issues=first, now=NOW)
        await session.commit()
        second = [*first, _minio("m2", "CVE-2023-28434", "MinIO", "MinIO")]
        counts = await store.apply_recompute(
            session, organization_id=org_id, vertical="cyber", issues=second, now=NOW + timedelta(hours=1)
        )
        await session.commit()
        assert counts["created"] == 0
        (case,) = (await session.execute(select(CtemCase))).scalars().all()
        assert set(case.keys) == {"upgrade:-/minio@latest", "upgrade:minio/minio@latest"}
        assert await _members(session, case) == {"opencti:m1": True, "opencti:m2": True}


def _names(events: Any) -> list[str]:
    return [name for name, _, _ in events.items]


async def test_a_reader_case_raises_its_events_once_and_takes_the_severity_of_its_priority() -> None:
    """#1206: opened on a new case, nothing on a repeat, resolved when all close, reopened after."""
    from app.services import ctem_case_loop as loop

    async with _database() as sessions, sessions() as session:
        org_id, _ = await _seed(session)
        events = loop.PendingEvents()
        await store.apply_recompute(
            session, organization_id=org_id, vertical="cyber", issues=[_issue("v1", "CVE-1")], events=events, now=NOW
        )
        await session.commit()
        case = (await session.execute(select(CtemCase))).scalar_one()
        assert _names(events) == [loop.CASE_OPENED]
        assert events.items[0][1]["case_id"] == str(case.id)
        # CVSS 9.8 is critical: the scope gate's P1 and its 24 hour SLA, no human ranks it.
        assert (case.severity, case.sla_due_at) == ("P1", NOW + timedelta(hours=24))

        again = loop.PendingEvents()
        await store.apply_recompute(
            session,
            organization_id=org_id,
            vertical="cyber",
            issues=[_issue("v1", "CVE-1")],
            events=again,
            now=NOW + timedelta(hours=1),
        )
        await session.commit()
        assert again.items == []
        assert await loop.announce_unannounced(session, events=again, now=NOW) == 0
        # Opened from its signal (#1529): the ``created`` event names the signal, no backfill announce.
        created = (
            await session.execute(
                select(CtemCaseEvent).where(CtemCaseEvent.case_id == case.id, CtemCaseEvent.kind == "created")
            )
        ).scalar_one()
        assert (created.reason or "").startswith("signal ")

        closing = loop.PendingEvents()
        await store.apply_recompute(
            session,
            organization_id=org_id,
            vertical="cyber",
            issues=[],
            closed=["opencti:v1"],
            events=closing,
            now=NOW + timedelta(hours=2),
        )
        await session.commit()
        assert _names(closing) == [loop.CASE_RESOLVED]

        reopening = loop.PendingEvents()
        await store.apply_recompute(
            session,
            organization_id=org_id,
            vertical="cyber",
            issues=[_issue("v1", "CVE-1")],
            events=reopening,
            now=NOW + timedelta(hours=3),
        )
        await session.commit()
        assert _names(reopening) == [loop.CASE_REOPENED]
        announced = (
            await session.execute(
                select(CtemCaseEvent).where(CtemCaseEvent.case_id == case.id, CtemCaseEvent.kind == store.ANNOUNCED)
            )
        ).scalars()
        assert list(announced) == []


async def test_a_lower_priority_read_never_relaxes_the_severity() -> None:
    async with _database() as sessions, sessions() as session:
        org_id, _ = await _seed(session)
        low = rules.SourceIssue(
            source="opencti", external_id="v1", name="CVE-1", product="Tomcat", vendor="Apache", fixed_version="9.0.98"
        )
        await store.apply_recompute(session, organization_id=org_id, vertical="cyber", issues=[low], now=NOW)
        await session.commit()
        case = (await session.execute(select(CtemCase))).scalar_one()
        assert (case.severity, case.sla_due_at) == ("P3", NOW + timedelta(hours=720))
        await store.apply_recompute(
            session, organization_id=org_id, vertical="cyber", issues=[low, _issue("v2", "CVE-2")], now=NOW
        )
        await session.commit()
        await session.refresh(case)
        assert (case.severity, case.sla_due_at) == ("P1", NOW + timedelta(hours=24))
        await store.apply_recompute(session, organization_id=org_id, vertical="cyber", issues=[low], now=NOW)
        await session.commit()
        await session.refresh(case)
        assert case.severity == "P1"


async def test_the_backfill_announces_every_open_case_once() -> None:
    """#1206: cases written before their own event raise ctem_case.opened once on the sweep, never twice."""
    from app.services import ctem_case_loop as loop

    async with _database() as sessions, sessions() as session:
        org_id, _ = await _seed(session)
        # Written by a source read without events: what every case of a build before #1206 looks like.
        for title, label, state in (
            ("Upgrade Tomcat", "Apache Tomcat", "new"),
            ("Upgrade nginx", "F5 nginx", "resolved"),
        ):
            session.add(
                CtemCase(
                    organization_id=org_id,
                    vertical="cyber",
                    title=title,
                    remediation_kind="upgrade",
                    product_label=label,
                    keys=[f"upgrade:-/{label.lower()}@latest"],
                    pinned_issues=[],
                    state=state,
                    state_since=NOW,
                )
            )
        await session.commit()
        cases = (await session.execute(select(CtemCase))).scalars().all()
        resolved = next(c for c in cases if c.state == "resolved")

        first = loop.PendingEvents()
        assert await loop.announce_unannounced(session, events=first, now=NOW) == 1
        await session.commit()
        assert _names(first) == [loop.CASE_OPENED]
        assert first.items[0][1]["case_id"] != str(resolved.id)

        second = loop.PendingEvents()
        assert await loop.announce_unannounced(session, events=second, now=NOW) == 0
        assert second.items == []
        assert await loop.announce_unannounced(session, events=None, now=NOW) == 0


async def test_a_source_read_records_one_signal_per_issue_and_opens_the_case_from_it() -> None:
    """#1529: the case of a source read opens from its signal; a second read adds no signal and no case."""
    async with _database() as sessions, sessions() as session:
        org_id, _ = await _seed(session)
        issues = [_issue("v1", "CVE-1"), _issue("v2", "CVE-2")]
        first = await store.apply_recompute(session, organization_id=org_id, vertical="cyber", issues=issues, now=NOW)
        await session.commit()
        again = await store.apply_recompute(
            session, organization_id=org_id, vertical="cyber", issues=issues, now=NOW + timedelta(hours=1)
        )
        await session.commit()
        assert (first["created"], first["signals"]) == (1, 2)
        assert (again["created"], again["signals"]) == (0, 0)
        case = (await session.execute(select(CtemCase))).scalar_one()
        assert case.trigger_path == "new_vulnerability" and case.keys == [KEY]
        signals = (await session.execute(select(CtemSignal))).scalars().all()
        assert {s.source_external_id for s in signals} == {"v1", "v2"}
        assert all(s.case_id == case.id and s.state == "case_opened" for s in signals)
        assert {s.seen_count for s in signals} == {2}
        rows = (await session.execute(select(CtemCaseIssue).where(CtemCaseIssue.case_id == case.id))).scalars().all()
        assert {f"{r.source}:{r.external_id}" for r in rows} == {
            "opencti:v1",
            "opencti:v2",
            *(f"signal:{s.id}" for s in signals),
        }
        events = (await session.execute(select(CtemCaseEvent))).scalars().all()
        created = [e for e in events if e.kind == "created"]
        assert len(created) == 1 and (created[0].reason or "").startswith("signal ")
        assert not [e for e in events if e.reason == "source read"]


async def test_a_source_signal_the_gate_keeps_out_opens_no_case(monkeypatch: pytest.MonkeyPatch) -> None:
    """#1529: a watchlisted source signal stays a signal; no case, no member."""
    from app.services import ctem_scope_gate_store as gate_store

    async def watch(db: Any, row: CtemSignal, **_: Any) -> None:
        row.state = "watchlist"
        row.decision = {"outcome": "watchlist", "state": "watchlist"}

    monkeypatch.setattr(gate_store, "evaluate", watch)
    async with _database() as sessions, sessions() as session:
        org_id, _ = await _seed(session)
        counts = await store.apply_recompute(
            session, organization_id=org_id, vertical="cyber", issues=[_issue("v1", "CVE-1")], now=NOW
        )
        await session.commit()
        assert (counts["created"], counts["signals"]) == (0, 1)
        assert (await session.execute(select(CtemCase))).scalars().all() == []
        (signal,) = (await session.execute(select(CtemSignal))).scalars().all()
        assert (signal.state, signal.case_id, signal.trigger_path) == ("watchlist", None, "new_vulnerability")


async def test_a_manual_split_announces_the_new_case() -> None:
    from app.services import ctem_case_loop as loop

    async with _database() as sessions, sessions() as session:
        org_id, owner_id = await _seed(session)
        issues = [_issue("v1", "CVE-1"), _issue("v2", "CVE-2")]
        await store.apply_recompute(session, organization_id=org_id, vertical="cyber", issues=issues, now=NOW)
        events = loop.PendingEvents()
        manual = await store.create_manual_case(
            session,
            organization_id=org_id,
            vertical="cyber",
            title="Tomcat on the DMZ",
            member_ids=["opencti:v2"],
            visible={"opencti:v1", "opencti:v2"},
            actor=str(owner_id),
            events=events,
            now=NOW,
        )
        await session.commit()
        assert _names(events) == [loop.CASE_OPENED]
        assert events.items[0][1]["case_id"] == str(manual.id)


async def test_the_openaev_identity_remap_keeps_the_case_merges_duplicates_and_retires_the_rest():
    """OpenAEV #9233 re-keyed every finding (xtm-one #1994): the case keeps its row, owner and history."""
    async with _database() as sessions, sessions() as session:
        org_id, owner_id = await _seed(session)
        case = CtemCase(
            organization_id=org_id,
            vertical="cyber",
            title="Tomcat",
            remediation_kind="upgrade",
            keys=[KEY],
            pinned_issues=["openaev:o1"],
            state="remediating",
            owner_user_id=owner_id,
        )
        session.add(case)
        await session.flush()
        session.add_all(
            [
                CtemCaseIssue(case_id=case.id, source="openaev", external_id="o1", open=True),
                CtemCaseIssue(case_id=case.id, source="openaev", external_id="o2", open=False),
                # The new id of o2 joined since the OpenAEV deploy: a duplicate.
                CtemCaseIssue(case_id=case.id, source="openaev", external_id="n2", open=True),
                CtemCaseIssue(case_id=case.id, source="openaev", external_id="o3", open=True),
                CtemCaseIssue(case_id=case.id, source="opencti", external_id="v1", open=True),
            ]
        )
        await session.flush()
        first_row = (await session.execute(select(CtemCaseIssue).where(CtemCaseIssue.external_id == "o1"))).scalar_one()

        counts = await store.remap_members(
            session,
            organization_id=org_id,
            vertical="cyber",
            remap={"openaev:o1": "openaev:n1", "openaev:o2": "openaev:n2", "openaev:o3": None},
            retired_line="Replaced by OpenAEV finding identity migration",
            now=NOW,
        )
        await session.commit()

        assert counts == {"remapped": 1, "merged": 1, "retired": 1, "resolved": 0}
        assert await _members(session, case) == {"openaev:n1": True, "openaev:n2": True, "opencti:v1": True}
        moved = (await session.execute(select(CtemCaseIssue).where(CtemCaseIssue.external_id == "n1"))).scalar_one()
        assert moved.id == first_row.id  # the same row: nothing of the case is lost
        await session.refresh(case)
        assert case.state == "remediating" and case.owner_user_id == owner_id
        assert case.pinned_issues == ["openaev:n1"]
        notes = (
            (
                await session.execute(
                    select(CtemCaseEvent).where(CtemCaseEvent.case_id == case.id, CtemCaseEvent.kind == "note")
                )
            )
            .scalars()
            .all()
        )
        retired = [e for e in notes if e.issue == "openaev:o3"]
        assert [e.reason for e in retired] == ["Replaced by OpenAEV finding identity migration"]
        assert {(e.from_value, e.to_value) for e in notes if e.from_value} == {
            ("openaev:o1", "openaev:n1"),
            ("openaev:o2", "openaev:n2"),
        }

        # A second pass with the same answer finds nothing left to move.
        again = await store.remap_members(
            session,
            organization_id=org_id,
            vertical="cyber",
            remap={"openaev:o1": "openaev:n1", "openaev:o3": None},
            retired_line="Replaced by OpenAEV finding identity migration",
            now=NOW,
        )
        assert again == {"remapped": 0, "merged": 0, "retired": 0, "resolved": 0}


async def test_a_case_whose_members_were_all_retired_resolves_instead_of_staying_open_empty() -> None:
    """No member left for a recompute to resolve: the retirement resolves the case (xtm-one #2011 review)."""
    async with _database() as sessions, sessions() as session:
        org_id, owner_id = await _seed(session)
        emptied = CtemCase(
            organization_id=org_id,
            vertical="cyber",
            title="Tomcat",
            remediation_kind="upgrade",
            keys=[KEY],
            state="remediating",
            owner_user_id=owner_id,
        )
        kept = CtemCase(
            organization_id=org_id,
            vertical="cyber",
            title="Nginx",
            remediation_kind="upgrade",
            keys=["other"],
            state="new",
            owner_user_id=owner_id,
        )
        session.add_all([emptied, kept])
        await session.flush()
        session.add_all(
            [
                CtemCaseIssue(case_id=emptied.id, source="openaev", external_id="o1", open=True),
                CtemCaseIssue(case_id=emptied.id, source="openaev", external_id="o2", open=False),
                CtemCaseIssue(case_id=kept.id, source="openaev", external_id="o3", open=True),
                CtemCaseIssue(case_id=kept.id, source="opencti", external_id="v1", open=True),
            ]
        )
        await session.flush()

        counts = await store.remap_members(
            session,
            organization_id=org_id,
            vertical="cyber",
            remap={"openaev:o1": None, "openaev:o2": None, "openaev:o3": None},
            retired_line="Replaced by OpenAEV finding identity migration",
            now=NOW,
        )
        await session.commit()

        assert counts == {"remapped": 0, "merged": 0, "retired": 3, "resolved": 1}
        await session.refresh(emptied)
        await session.refresh(kept)
        assert emptied.state == "resolved" and emptied.owner_user_id == owner_id
        assert kept.state == "new" and await _members(session, kept) == {"opencti:v1": True}
        states = (
            (
                await session.execute(
                    select(CtemCaseEvent).where(CtemCaseEvent.case_id == emptied.id, CtemCaseEvent.kind == "state")
                )
            )
            .scalars()
            .all()
        )
        assert [(e.from_value, e.to_value, e.evidence) for e in states] == [
            ("remediating", "resolved", "Replaced by OpenAEV finding identity migration")
        ]


async def test_the_schema_refuses_an_incomplete_accepted_risk():
    async with _database() as sessions, sessions() as session:
        org_id, _ = await _seed(session)
        session.add(
            CtemCase(
                organization_id=org_id,
                vertical="cyber",
                title="t",
                remediation_kind="upgrade",
                keys=[KEY],
                pinned_issues=[],
                state="accepted_risk",
            )
        )
        with pytest.raises(IntegrityError):
            await session.flush()
