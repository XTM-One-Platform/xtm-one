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
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker

import app.models  # noqa: F401 - registers every mapper on Base.metadata
from app.models.ctem_case import CtemCase, CtemCaseEvent, CtemCaseIssue
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
    tables = with_referenced_tables([CtemCase.__table__, CtemCaseIssue.__table__, CtemCaseEvent.__table__])
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


async def _members(session, case: CtemCase) -> dict[str, bool]:
    rows = (await session.execute(select(CtemCaseIssue).where(CtemCaseIssue.case_id == case.id))).scalars()
    return {f"{r.source}:{r.external_id}": r.open for r in rows}


async def test_recompute_keeps_state_owner_and_due_date_and_resolves_when_all_closed():
    async with _database() as sessions, sessions() as session:
        org_id, owner_id = await _seed(session)
        counts = await store.apply_recompute(
            session, organization_id=org_id, vertical="cyber", issues=[_issue("v1", "CVE-1")], now=NOW
        )
        assert counts == {"created": 1, "updated": 0, "resolved": 0, "reopened": 0, "no_known_fix": 0}
        case = (await session.execute(select(CtemCase))).scalar_one()
        due = NOW + timedelta(days=14)
        await store.update_case(
            session,
            case,
            actor=str(owner_id),
            actor_kind="user",
            state="in_progress",
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
        assert (case.state, case.owner_user_id, case.due_at) == ("in_progress", owner_id, due)
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


async def test_an_expired_acceptance_returns_to_needs_attention():
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
            "needs_attention",
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
        assert await _members(session, manual) == {"opencti:v2": True} and manual.state == "needs_attention"
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
    shrinks it; rendered for Bob, CVE-2 is a bare Restricted row and the
    aggregates cover CVE-1 only; Carol (sees neither) does not see the case or
    its count; Bob cannot split CVE-2 out; nothing of the source is stored.
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
        assert case.state == "needs_attention"
        assert {f"{r.source}:{r.external_id}": r.open for r in rows} == {"opencti:v1": True, "opencti:v2": True}
        # Nothing of the source in the database: no name, score, asset or count column.
        for table in (CtemCase.__table__, CtemCaseIssue.__table__):
            assert not {"name", "cvss", "epss", "kev", "assets", "issue_count", "priority"} & set(table.c.keys())

        case_id, rows, events = await store.get_case(session, case.id, organization_id=org_id)
        for_alice = view_of.hydrate(case_id, rows, events, alice)
        for_bob = view_of.hydrate(case_id, rows, events, bob)
        assert for_alice["restricted_count"] == 0 and for_alice["max_cvss"] == 10.0
        assert for_bob["restricted_count"] == 1 and for_bob["max_cvss"] == 9.8 and for_bob["kev_count"] == 0
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
            state="in_progress",
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
        assert (target.state, target.owner_user_id) == ("in_progress", owner_id)
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
