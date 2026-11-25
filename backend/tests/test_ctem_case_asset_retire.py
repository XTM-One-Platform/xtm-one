"""CTEM cases of an archived or removed inventory asset close (ctem-autoresolve-archived-asset)."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

import app.models  # noqa: F401 - registers every mapper on Base.metadata
from app.models.ctem_case import CtemCase, CtemCaseEvent, CtemCaseIssue
from app.models.ctem_signal import CtemSignal
from app.models.organization import Organization
from app.models.organization_asset_record import OrganizationAssetChange, OrganizationAssetRecord
from app.models.work_item import WorkItem
from app.services import ctem_case_asset_retire as retire
from app.services import ctem_case_loop as loop

NOW = datetime(2026, 11, 25, 13, 0, tzinfo=UTC)


# ── the rule, pure ──────────────────────────────────────────────────────


def test_every_record_inactive_retires_the_asset_as_archived():
    records = [("filigranhq/scred-project", "123", "inactive")]
    assert retire.retired_of(["filigranhq/scred-project", "123"], records, []) == {
        "filigranhq/scred-project": retire.ARCHIVED,
        "123": retire.ARCHIVED,
    }


def test_one_live_record_keeps_the_asset():
    records = [("host-1", "a", "inactive"), ("host-1", "b", "active")]
    assert retire.retired_of(["host-1"], records, []) == {}


def test_a_status_the_source_does_not_state_keeps_the_asset():
    assert retire.retired_of(["host-1"], [("host-1", "a", "")], []) == {}


def test_a_deleted_record_retires_the_asset_as_removed_and_an_unknown_subject_stays():
    assert retire.retired_of(["gone", "never-held"], [], ["gone"]) == {"gone": retire.REMOVED}


def test_the_subject_key_folds_case_and_whitespace_as_the_records_do():
    assert retire.subject_key("  FiligranHQ/Scred-Project ") == "filigranhq/scred-project"


# ── the pass, on PostgreSQL ─────────────────────────────────────────────


@asynccontextmanager
async def _database() -> AsyncIterator[async_sessionmaker]:
    from tests.scratch_database import ALL_TABLES, scratch_database

    async with scratch_database("ctemassetretire", tables=ALL_TABLES, extensions=("vector",)) as db:
        yield db.sessions


def _record(org: uuid.UUID, name: str, status: str, external_id: str) -> OrganizationAssetRecord:
    return OrganizationAssetRecord(
        organization_id=org,
        source="github",
        external_id=external_id,
        name=name,
        name_key=retire.subject_key(name),
        category="source_code",
        fields={"name": name, "category": "source_code", "status": status},
    )


async def _case_on(session, org: uuid.UUID, subject: str) -> tuple[CtemCase, CtemSignal]:
    case = CtemCase(
        organization_id=org,
        vertical="cyber",
        title=f"Restore evidence of {subject}",
        remediation_kind="close_exposure",
        keys=[f"close_exposure:asset/{subject}"],
        pinned_issues=[],
        state="waiting_decision",
        trigger_path="control_decay",
        validations=[],
    )
    session.add(case)
    await session.flush()
    signal = CtemSignal(
        organization_id=org,
        trigger_path="control_decay",
        lens="inside",
        source_provider="opencrq",
        subject_kind="asset",
        subject_ref=subject,
        dedupe_key=f"d-{uuid.uuid4().hex}",
        state="case_opened",
        case_id=case.id,
        recorded_via="listener",
        recorded_by="system",
    )
    session.add(signal)
    await session.flush()
    session.add(CtemCaseIssue(case_id=case.id, source="signal", external_id=str(signal.id), open=True))
    return case, signal


@pytest.mark.integration
async def test_an_archived_repository_resolves_its_case_and_cancels_its_gate():
    async with _database() as sessions:
        async with sessions() as session:
            org = Organization(id=uuid.uuid4(), name="Org", role="primary")
            session.add(org)
            await session.flush()
            session.add(_record(org.id, "FiligranHQ/scred-project", "inactive", "123"))
            session.add(_record(org.id, "FiligranHQ/live", "active", "456"))
            archived, _ = await _case_on(session, org.id, "FiligranHQ/scred-project")
            live, _ = await _case_on(session, org.id, "FiligranHQ/live")
            gate = WorkItem(
                title="Approve the validation",
                correlation_key=f"ctem:validation:{archived.id}",
                status="open",
                needs_human=True,
            )
            session.add(gate)
            await session.commit()
            ids = (archived.id, live.id, gate.id)
        async with sessions() as session:
            events = loop.PendingEvents()
            assert await retire.close_retired_asset_cases(session, events=events, now=NOW) == 1
            await session.commit()
            assert [name for name, _, _ in events.items] == [loop.CASE_RESOLVED]
        async with sessions() as session:
            archived_case = await session.get(CtemCase, ids[0])
            assert archived_case.state == "resolved" and archived_case.resolution_kind == retire.RESOLUTION
            assert (await session.get(CtemCase, ids[1])).state == "waiting_decision"
            item = await session.get(WorkItem, ids[2])
            assert item.status == "cancelled" and item.needs_human is False and "asset archived" in item.result
            kinds = (
                await session.execute(
                    select(CtemCaseEvent.kind, CtemCaseEvent.evidence).where(CtemCaseEvent.case_id == ids[0])
                )
            ).all()
            assert ("issue_closed", retire.ARCHIVED) in kinds and ("state", retire.ARCHIVED) in kinds
            # A second pass finds nothing left to close.
            assert await retire.close_retired_asset_cases(session, now=NOW) == 0


@pytest.mark.integration
async def test_a_removed_asset_resolves_and_a_subject_never_held_stays_open():
    async with _database() as sessions:
        async with sessions() as session:
            org = Organization(id=uuid.uuid4(), name="Org", role="primary")
            session.add(org)
            await session.flush()
            session.add(
                OrganizationAssetChange(
                    organization_id=org.id,
                    entity="record",
                    entity_id=uuid.uuid4(),
                    op="delete",
                    before={"name": "old-host.example.com"},
                    after=None,
                )
            )
            removed, _ = await _case_on(session, org.id, "old-host.example.com")
            outside, _ = await _case_on(session, org.id, "shop.example.com")
            await session.commit()
            ids: tuple[Any, Any] = (removed.id, outside.id)
        async with sessions() as session:
            assert await retire.close_retired_asset_cases(session, now=NOW) == 1
            await session.commit()
            case = await session.get(CtemCase, ids[0])
            assert case.state == "resolved"
            assert (await session.get(CtemCase, ids[1])).state == "waiting_decision"
