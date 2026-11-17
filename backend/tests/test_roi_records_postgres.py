"""ROI run records and baseline overrides against real PostgreSQL (issue #8990, slice 2).

``integration``-marked and skips when PostgreSQL is unreachable.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import app.models  # noqa: F401 - registers every table on Base.metadata
from app.models.agent import Agent
from app.models.assignment import AssignmentRun
from app.models.llm_trace import LLMTrace
from app.models.roi import RoiRunRecord
from app.models.user import User
from app.services import roi, roi_records
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration


@asynccontextmanager
async def _session(prefix: str) -> AsyncIterator[AsyncSession]:
    async with (
        scratch_database(prefix, tables=ALL_TABLES, extensions=("vector",), skip_without_extensions=True) as db,
        db.sessions() as session,
    ):
        yield session


_SCHEMA = {"type": "object", "x-xtm-roi": {"unit": "cve_assessed", "count": "cves"}}


async def _seed(session):
    user = User(id=uuid.uuid4(), email=f"roi-{uuid.uuid4().hex[:8]}@test.local", full_name="R", hashed_password="x")
    session.add(user)
    await session.flush()
    agent = Agent(id=uuid.uuid4(), name="CVE analyst", user_id=user.id)
    session.add(agent)
    await session.flush()
    start = datetime(2026, 11, 5, 20, 0, tzinfo=UTC)
    run = AssignmentRun(
        id=uuid.uuid4(),
        assignment_id=None,
        agent_id=agent.id,
        user_id=user.id,
        status="completed",
        started_at=start,
        completed_at=start + timedelta(seconds=120),
        result_summary=json.dumps({"cves": [{"id": "CVE-1"}, {"id": "CVE-2"}, {"id": "CVE-3"}]}),
    )
    session.add(run)
    await session.flush()
    return user, agent, run


async def test_a_run_records_its_units_cost_and_duration_once():
    async with _session("roirec") as session:
        _, agent, run = await _seed(session)
        for cost in (0.25, 0.5):
            session.add(
                LLMTrace(
                    id=uuid.uuid4(),
                    name="assignment",
                    source="assignment",
                    assignment_run_id=run.id,
                    agent_id=agent.id,
                    total_cost=cost,
                    started_at=run.started_at,
                )
            )
        await session.flush()
        fleet_id = uuid.uuid4()

        row = await roi_records.record_run_units(session, run=run, output_schema=_SCHEMA, fleet_id=fleet_id)
        assert row is not None
        # Written unreviewed (#9310): a person marks the outcome.
        assert (row.unit, row.units, row.duration_s, row.outcome) == ("cve_assessed", 3, 120.0, None)
        assert row.llm_cost == pytest.approx(0.75)

        # One record per run: a second call writes nothing.
        assert await roi_records.record_run_units(session, run=run, output_schema=_SCHEMA) is None
        await session.commit()
        stored = (await session.execute(select(RoiRunRecord))).scalars().all()
        assert len(stored) == 1 and stored[0].fleet_id == fleet_id

        # The review queue lists it until reviewed.
        queue = await roi_records.list_records(session, outcome=roi.OUTCOME_UNREVIEWED)
        assert [r.run_id for r in queue] == [run.id]

        # The review is recorded on the same row, with the reviewer and the note.
        reviewer = run.user_id
        assert await roi_records.set_run_outcome(
            session, run.id, outcome="modified", review_min=4, note=" fixed two CVSS ", user_id=reviewer
        )
        await session.commit()
        stored = (await session.execute(select(RoiRunRecord))).scalar_one()
        assert (stored.outcome, stored.review_min, stored.review_note) == ("modified", 4, "fixed two CVSS")
        assert stored.reviewed_by_user_id == reviewer and stored.reviewed_at is not None
        assert await roi_records.list_records(session, outcome=roi.OUTCOME_UNREVIEWED) == []
        assert [r.id for r in await roi_records.list_records(session, outcome="modified")] == [stored.id]
        assert roi_records.record_dict(stored)["outcome"] == "modified"

        # Back to unreviewed clears the reviewer, the time and the note.
        assert await roi_records.set_run_outcome(session, run.id, outcome=None)
        await session.commit()
        stored = (await session.execute(select(RoiRunRecord))).scalar_one()
        assert (stored.outcome, stored.reviewed_by_user_id, stored.reviewed_at, stored.review_note) == (
            None,
            None,
            None,
            None,
        )

        assert not await roi_records.set_run_outcome(session, uuid.uuid4(), outcome="accepted")
        with pytest.raises(ValueError, match="outcome"):
            await roi_records.set_run_outcome(session, run.id, outcome="great")
        with pytest.raises(ValueError, match="note"):
            await roi_records.set_run_outcome(session, run.id, outcome="accepted", note="x" * 501)
        with pytest.raises(ValueError, match="outcome"):
            await roi_records.list_records(session, outcome="great")


async def test_the_review_block_reads_the_record_of_a_run():
    async with _session("roiblock") as session:
        user, _, run = await _seed(session)
        # No record yet: no block.
        assert (
            await roi_records.review_block(
                session, run_id=run.id, user_id=user.id, is_admin=False, subject_owner_id=user.id
            )
            is None
        )
        await roi_records.record_run_units(session, run=run, output_schema=_SCHEMA)
        await session.commit()

        block = await roi_records.review_block(
            session, run_id=run.id, user_id=user.id, is_admin=False, subject_owner_id=user.id
        )
        assert block is not None
        assert (block["unit"], block["units"], block["outcome"], block["can_review"]) == ("cve_assessed", 3, None, True)
        assert block["default_review_min"] == roi.BASELINES_BY_UNIT["cve_assessed"].review_min

        # Another person reads it without the right to mark it.
        other = await roi_records.review_block(
            session, run_id=run.id, user_id=uuid.uuid4(), is_admin=False, subject_owner_id=user.id
        )
        assert other is not None and other["can_review"] is False

        # A work item with no record has no block.
        assert (
            await roi_records.review_block(
                session, work_item_id=uuid.uuid4(), user_id=user.id, is_admin=True, subject_owner_id=None
            )
            is None
        )
        with pytest.raises(ValueError):
            await roi_records.review_block(session, user_id=user.id, is_admin=True, subject_owner_id=None)


async def test_an_agent_without_a_unit_records_nothing():
    async with _session("roinone") as session:
        _, _, run = await _seed(session)
        assert await roi_records.record_run_units(session, run=run, output_schema={"type": "object"}) is None
        assert await roi_records.record_run_units(session, run=run, output_schema=None) is None
        await session.commit()
        assert (await session.execute(select(RoiRunRecord))).first() is None


async def test_the_overrides_shape_the_effective_baselines():
    async with _session("roiovr") as session:
        user, _, _ = await _seed(session)
        await roi_records.upsert_override(
            session, unit="cve_assessed", low=4, likely=10, high=25, review=2, source="study", user_id=user.id
        )
        await session.commit()
        merged, overridden = await roi_records.effective_baselines(session)
        assert overridden == {"cve_assessed"}
        assert merged["cve_assessed"].manual_min_likely == 10
        assert merged["cve_assessed"].minutes_label == roi.LABEL_OVERRIDE

        # A second write updates the same row.
        await roi_records.upsert_override(session, unit="cve_assessed", low=4, likely=12, high=25, review=2)
        await session.commit()
        merged, _ = await roi_records.effective_baselines(session)
        assert merged["cve_assessed"].manual_min_likely == 12

        assert await roi_records.delete_override(session, "cve_assessed")
        await session.commit()
        merged, overridden = await roi_records.effective_baselines(session)
        assert overridden == set()
        assert merged["cve_assessed"].manual_min_likely == roi.BASELINES_BY_UNIT["cve_assessed"].manual_min_likely
        assert not await roi_records.delete_override(session, "cve_assessed")


async def test_a_custom_unit_is_versioned_declarable_archived_and_counted():
    from app.models.roi import RoiUnit
    from app.services import roi_report

    async with _session("roiunit") as session:
        user, _, run = await _seed(session)
        known_before = await roi_records.known_units(session)
        unit = await roi_records.create_unit(
            session,
            key="contract_reviewed",
            name="Contract reviewed",
            role="Legal counsel",
            low=30,
            likely=60,
            high=120,
            review=10,
            source="time study",
            user_id=user.id,
        )
        await session.commit()
        assert unit.version == 1
        assert "contract_reviewed" not in known_before
        assert "contract_reviewed" in await roi_records.known_units(session)

        # Taken and built-in keys are refused.
        for key in ("contract_reviewed", "cve_assessed"):
            with pytest.raises(ValueError):
                await roi_records.create_unit(session, key=key, name="x", low=1, likely=2, high=3, review=0)

        updated = await roi_records.update_unit(
            session, "contract_reviewed", name="Contract reviewed", low=30, likely=90, high=120, review=10
        )
        await session.commit()
        assert updated is not None and updated.version == 2
        assert await roi_records.update_unit(session, "missing", name="x", low=1, likely=2, high=3, review=0) is None

        # A run of a custom unit counts with the unit's own minutes.
        schema = {"type": "object", "x-xtm-roi": {"unit": "contract_reviewed", "count": "cves"}}
        await roi_records.record_run_units(session, run=run, output_schema=schema)
        await session.commit()
        report = await roi_report.load_report(session, window="all", currency="USD")
        assert report.organization.units == 3
        assert report.organization.minutes_saved == pytest.approx(3 * (90 - 10))

        archived = await roi_records.archive_unit(session, "contract_reviewed", user_id=user.id)
        await session.commit()
        assert archived is not None and archived.archived_at is not None and archived.version == 3
        assert "contract_reviewed" not in await roi_records.known_units(session)
        # Archived: no longer declarable, still counted in history.
        merged, _ = await roi_records.effective_baselines(session)
        assert merged["contract_reviewed"].manual_min_likely == 90
        assert [v.version for v in await roi_records.unit_versions(session, "contract_reviewed")] == [3, 2, 1]
        assert (await session.get(RoiUnit, "contract_reviewed")) is not None

        # A custom unit is never overridden through the built-in baselines.
        with pytest.raises(ValueError):
            await roi_records.upsert_override(session, unit="contract_reviewed", low=1, likely=2, high=3, review=0)


async def test_a_text_agent_declared_per_run_records_one_unit_and_resolution_order():
    """#9285: a declaration without JSON; assignment over agent over fleet."""
    async with _session("roidecl") as session:
        user, agent, run = await _seed(session)
        run.result_summary = "A plain prose answer, no structured output."
        assignment_id, fleet_id = uuid.uuid4(), uuid.uuid4()
        assert await roi_records.resolve_declaration(session, agent_id=agent.id) is None

        await roi_records.put_declaration(
            session, subject_type="fleet", subject_id=fleet_id, unit="cve_assessed", count_mode="per_run"
        )
        got = await roi_records.resolve_declaration(session, agent_id=agent.id, fleet_id=fleet_id)
        assert got is not None and got.source == "fleet"

        await roi_records.put_declaration(
            session,
            subject_type="agent",
            subject_id=agent.id,
            unit="cve_assessed",
            count_mode="per_run",
            units_per_count=2,
            user_id=user.id,
        )
        await roi_records.put_declaration(
            session,
            subject_type="assignment",
            subject_id=assignment_id,
            unit="cve_assessed",
            count_mode="per_work_item",
        )
        got = await roi_records.resolve_declaration(
            session, agent_id=agent.id, assignment_id=assignment_id, fleet_id=fleet_id
        )
        assert got is not None and (got.source, got.count_mode) == ("assignment", "per_work_item")

        declaration = await roi_records.resolve_declaration(
            session, agent_id=agent.id, output_schema=_SCHEMA, fleet_id=fleet_id
        )
        assert declaration is not None and (declaration.source, declaration.units_per_count) == ("agent", 2)
        row = await roi_records.record_run_units(session, run=run, declaration=declaration, fleet_id=fleet_id)
        assert row is not None and (row.unit, row.units) == ("cve_assessed", 2)

        # An unknown unit is refused; a second put replaces the row.
        with pytest.raises(ValueError, match="not a known unit"):
            await roi_records.put_declaration(
                session, subject_type="agent", subject_id=agent.id, unit="nope", count_mode="per_run"
            )
        await roi_records.put_declaration(
            session, subject_type="agent", subject_id=agent.id, unit="cve_assessed", count_mode="per_run"
        )
        assert (await roi_records.get_declaration(session, "agent", agent.id)).units_per_count == 1
        assert await roi_records.delete_declaration(session, "agent", agent.id)
        assert not await roi_records.delete_declaration(session, "agent", agent.id)
        await session.commit()


async def test_a_done_work_item_records_one_unit_once():
    """#9285 ``per_work_item``: one record per item, nothing without the declaration."""
    async with _session("roiwi") as session:
        _, agent, _ = await _seed(session)
        item_id = uuid.uuid4()
        assert await roi_records.record_work_item_units(session, work_item_id=item_id, agent_id=agent.id) is None

        await roi_records.put_declaration(
            session, subject_type="agent", subject_id=agent.id, unit="cve_assessed", count_mode="per_work_item"
        )
        row = await roi_records.record_work_item_units(session, work_item_id=item_id, agent_id=agent.id)
        assert row is not None and (row.units, row.run_id, row.work_item_id) == (1, None, item_id)
        assert await roi_records.record_work_item_units(session, work_item_id=item_id, agent_id=agent.id) is None
        await session.commit()
        stored = (await session.execute(select(RoiRunRecord))).scalars().all()
        assert [(r.work_item_id, r.units) for r in stored] == [(item_id, 1)]


async def test_the_report_reads_the_records_with_names_and_overrides():
    from app.services import roi_report

    async with _session("roirep") as session:
        _, _, run = await _seed(session)
        await roi_records.record_run_units(session, run=run, output_schema=_SCHEMA)
        await roi_records.upsert_override(session, unit="cve_assessed", low=4, likely=10, high=25, review=2)
        await session.commit()

        report = await roi_report.load_report(session, window="all", currency="USD")
        assert report.organization.runs == 1 and report.organization.units == 3
        assert report.organization.minutes_saved == pytest.approx(3 * (10 - 2))
        assert [(g.name, g.figures.units) for g in report.agents] == [("CVE analyst", 3)]
        assert report.fleets == []
        assert report.estimated is False
        assert report.rate_label == "catalog default"


async def test_scoped_reports_and_role_rates():
    """#9321: an agent's and a fleet's report, and a custom role's rate on a custom unit."""
    from app.services import roi_report

    async with _session("roiscope") as session:
        _, agent, _ = await _seed(session)
        fleet_id, other_agent = uuid.uuid4(), uuid.uuid4()
        await roi_records.put_rate(session, role="legal", currency="usd", amount=120.0, source="payroll")
        await roi_records.create_unit(
            session,
            key="contract_reviewed",
            name="Contract reviewed",
            low=30,
            likely=60,
            high=90,
            review=10,
            rate_role="legal",
        )
        session.add_all(
            [
                RoiRunRecord(agent_id=agent.id, fleet_id=fleet_id, unit="contract_reviewed", units=2),
                RoiRunRecord(agent_id=None, fleet_id=None, unit="contract_reviewed", units=5),
            ]
        )
        await session.commit()

        fleet = await roi_report.load_report(session, window="all", currency="USD", scope="fleet", scope_id=fleet_id)
        assert (fleet.organization.runs, fleet.organization.units) == (1, 2)
        # 2 x (60 - 10) = 100 min at the legal rate 120/h.
        assert fleet.organization.value == pytest.approx(200.0)
        assert fleet.scope_name == "deleted fleet" and fleet.rate_label.endswith(roi_report.LABEL_ROLE_RATES)
        mine = await roi_report.load_report(session, window="all", currency="USD", scope="agent", scope_id=agent.id)
        assert mine.organization.units == 2 and mine.scope_name == "CVE analyst"
        nobody = await roi_report.load_report(
            session, window="all", currency="USD", scope="agent", scope_id=other_agent
        )
        assert nobody.organization.runs == 0
        everything = await roi_report.load_report(session, window="all", currency="USD")
        assert everything.organization.units == 7
        with pytest.raises(ValueError, match="needs its id"):
            await roi_report.load_report(session, scope="fleet")

        assert await roi_records.delete_rate(session, role="legal", currency="USD") is True
        assert await roi_records.delete_rate(session, role="legal", currency="USD") is False
        await session.commit()
        # The role keeps resolving, at the analyst rate, once its rate is gone.
        fleet = await roi_report.load_report(session, window="all", currency="USD", scope="fleet", scope_id=fleet_id)
        analyst = roi.HOURLY_RATES["analyst"]["USD"].amount
        assert fleet.organization.value == pytest.approx(round(100 / 60 * analyst, 2))


async def test_masked_costs_mask_every_scope_for_every_reader(monkeypatch: pytest.MonkeyPatch) -> None:
    """#12077 invariant 6: with costs hidden, the administrator's platform report, an agent's and a
    fleet's answer ``cost_masked`` and null costs, as ``mine`` does (prod retest of #12152)."""
    from app.core.config import settings
    from app.services import roi_access, roi_report

    monkeypatch.setattr(settings, "observability_cost_data_enabled", False)
    async with _session("roimask") as session:
        _, agent, run = await _seed(session)  # type: ignore[no-untyped-call]
        await roi_records.record_run_units(session, run=run, output_schema=_SCHEMA)
        fleet_id = uuid.uuid4()
        session.add(RoiRunRecord(agent_id=agent.id, fleet_id=fleet_id, unit="cve_assessed", units=2, llm_cost=0.5))
        await session.commit()

        admin = roi_access.RoiScope(organization=True)
        reports = {
            "platform": await roi_report.load_report(session, window="all", currency="USD"),
            "platform-admin-reader": await roi_report.load_report(
                session, window="all", currency="USD", scope="platform", reader=admin
            ),
            "agent": await roi_report.load_report(
                session, window="all", currency="USD", scope="agent", scope_id=agent.id
            ),
            "fleet": await roi_report.load_report(
                session, window="all", currency="USD", scope="fleet", scope_id=fleet_id
            ),
            "mine": await roi_report.load_report(session, window="all", currency="USD", scope="mine", reader=admin),
        }
        for name, report in reports.items():
            assert report.cost_masked is True, name
            data = roi_report.report_dict(report)
            assert data["cost_masked"] is True, name
            assert data["organization"]["value"] is not None, name
            for key in roi_report.COST_FIELDS:
                assert data["organization"][key] is None, (name, key)

    monkeypatch.setattr(settings, "observability_cost_data_enabled", True)
    async with _session("roiunmask") as session:
        await _seed(session)  # type: ignore[no-untyped-call]
        await session.commit()
        assert (await roi_report.load_report(session, window="all", currency="USD")).cost_masked is False
