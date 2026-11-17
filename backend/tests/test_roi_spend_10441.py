"""The AI cost of the ROI reports is the agents' LLM spend, not the cost of the recorded runs (#10441)."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.services import roi, roi_report, roi_spend
from app.services.roi_report import RecordRow


def _row(agent_id, fleet_id=None, *, unit="cve_assessed", units=1, cost=0.0, work_item=False) -> RecordRow:
    return RecordRow(agent_id, fleet_id, roi.RunMeasure(unit=unit, units=units, llm_cost=cost, work_item=work_item))


def _make(agent_id, fleet_id, measure) -> RecordRow:
    return RecordRow(agent_id, fleet_id, measure)


def test_a_cost_only_measure_counts_in_the_cost_and_nothing_else():
    figures = roi.compute_roi(
        [
            roi.RunMeasure(unit="cve_assessed", units=2, llm_cost=1.0),
            roi.RunMeasure(unit=roi.COST_ONLY_UNIT, units=0, llm_cost=9.0, outcome=None, cost_only=True),
        ]
    )
    assert figures.llm_cost == pytest.approx(10.0)
    assert (figures.runs, figures.units, figures.unreviewed) == (1, 2, 0)
    assert roi.COST_ONLY_UNIT not in " ".join(figures.assumptions)
    assert figures.net_value == pytest.approx(figures.value - 10.0)


def test_allocate_spreads_each_agents_spend_over_its_records_and_adds_the_unrecorded_spend():
    recorded, per_item, silent = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    fleet = uuid.uuid4()
    rows = [
        _row(recorded, fleet, cost=0.01),
        _row(recorded, fleet, cost=0.02),
        # A per_work_item record carries no run, so no trace: it used to cost 0.
        _row(per_item, fleet, cost=0.0, work_item=True),
    ]
    spend = {recorded: 30.0, per_item: 12.0, silent: 5.0}
    priced = roi_spend.allocate(
        rows,
        spend,
        records_per_agent={recorded: 3, per_item: 1},  # one record of `recorded` is outside this read
        scope_agents={recorded, per_item, silent},
        home_fleets={silent: fleet},
        make_row=_make,
    )
    assert [r.measure.llm_cost for r in priced[:3]] == pytest.approx([10.0, 10.0, 12.0])
    extra = priced[3]
    assert (extra.agent_id, extra.fleet_id, extra.measure.cost_only, extra.measure.llm_cost) == (
        silent,
        fleet,
        True,
        5.0,
    )
    assert roi_spend.unallocated(priced) == pytest.approx(5.0)


def test_allocate_leaves_out_agents_without_spend_and_never_duplicates_a_recorded_agent():
    agent, idle = uuid.uuid4(), uuid.uuid4()
    priced = roi_spend.allocate(
        [_row(agent)],
        {agent: 4.0},
        records_per_agent={agent: 1},
        scope_agents={agent, idle},
        make_row=_make,
    )
    assert len(priced) == 1 and priced[0].measure.llm_cost == pytest.approx(4.0)


def test_the_report_rows_sum_to_the_spend_and_name_the_unallocated_row():
    agent, silent, fleet = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    priced = roi_spend.allocate(
        [_row(agent, fleet), _row(agent, fleet)],
        {agent: 8.0, silent: 2.0},
        records_per_agent={agent: 2},
        scope_agents={agent, silent},
        home_fleets={silent: fleet},
        make_row=_make,
    )
    report = roi_report.build_report(
        priced,
        window="30d",
        since=None,
        currency="USD",
        rates=roi.HOURLY_RATES,
        rate_label="catalog default",
    )
    assert report.organization.llm_cost == pytest.approx(10.0)
    assert report.organization.runs == 2
    assert sum(g.figures.llm_cost for g in report.agents) == pytest.approx(10.0)
    assert [g.figures.llm_cost for g in report.fleets] == pytest.approx([10.0])
    assert sum(g.figures.llm_cost for g in report.units) == pytest.approx(10.0)
    unallocated = [g for g in report.units if g.id == roi.COST_ONLY_UNIT]
    assert [(g.name, g.figures.llm_cost, g.figures.runs) for g in unallocated] == [(roi.COST_ONLY_NAME, 2.0, 0)]


def test_the_report_names_its_cost_source():
    report = roi_report.build_report(
        [], window="30d", since=None, currency="USD", rates=roi.HOURLY_RATES, rate_label="catalog default"
    )
    assert roi_report.report_dict(report)["cost_source"] == "recorded_runs"
    report.cost_source = roi_spend.COST_SOURCE
    report.unallocated_cost = 1.5
    shape = roi_report.report_dict(report)
    assert shape["cost_source_label"] == roi_spend.COST_SOURCE_LABEL and shape["unallocated_cost"] == 1.5
    report.cost_masked = True
    assert roi_report.report_dict(report)["unallocated_cost"] is None


# ── Against PostgreSQL: the report's cost is LLM Analytics' cost of the same agents ──────────


@asynccontextmanager
async def _session(prefix: str) -> AsyncIterator[AsyncSession]:
    from tests.scratch_database import ALL_TABLES, scratch_database

    async with (
        scratch_database(prefix, tables=ALL_TABLES, extensions=("vector",), skip_without_extensions=True) as db,
        db.sessions() as session,
    ):
        yield session


@pytest.mark.integration
async def test_the_report_cost_equals_the_agents_traced_spend():
    import app.models  # noqa: F401 - registers every table on Base.metadata
    from app.models.agent import Agent
    from app.models.fleet import Fleet, FleetMember
    from app.models.llm_trace import LLMTrace
    from app.models.roi import RoiRunRecord
    from app.models.user import User
    from app.services.workspaces import create_workspace

    async with _session("roispend") as session:
        user = User(
            id=uuid.uuid4(), email=f"spend-{uuid.uuid4().hex[:8]}@test.local", full_name="S", hashed_password="x"
        )
        session.add(user)
        await session.flush()
        per_run = Agent(id=uuid.uuid4(), name="Per run", user_id=user.id)
        per_item = Agent(id=uuid.uuid4(), name="Per work item", user_id=user.id)
        silent = Agent(id=uuid.uuid4(), name="No unit", user_id=user.id)
        session.add_all([per_run, per_item, silent])
        await session.flush()
        workspace = await create_workspace(session, user_id=user.id, name="Spend")
        fleet = Fleet(id=uuid.uuid4(), name="Spend fleet", slug="spend", user_id=user.id, workspace_id=workspace.id)
        session.add(fleet)
        await session.flush()
        session.add_all([FleetMember(fleet_id=fleet.id, agent_id=a.id, role="member") for a in (per_item, silent)])
        now = datetime.now(UTC)
        at = now - timedelta(hours=2)
        # Traces of runs with and without a record, and of an agentless chat.
        for agent, cost in ((per_run, 2.0), (per_run, 6.0), (per_item, 3.0), (silent, 1.5), (None, 50.0)):
            session.add(
                LLMTrace(
                    id=uuid.uuid4(),
                    name="run",
                    source="assignment" if agent else "general-chat",
                    agent_id=agent.id if agent else None,
                    user_id=user.id,
                    total_cost=cost,
                    started_at=at,
                )
            )
        session.add_all(
            [
                RoiRunRecord(agent_id=per_run.id, unit="cve_assessed", units=1, llm_cost=2.0, created_at=at),
                RoiRunRecord(
                    agent_id=per_item.id,
                    fleet_id=fleet.id,
                    unit="cve_assessed",
                    units=1,
                    llm_cost=0.0,
                    work_item_id=uuid.uuid4(),
                    created_at=at,
                ),
            ]
        )
        await session.commit()

        spend = await roi_spend.agent_spend(session, since=now - timedelta(days=1), now=now)
        assert spend == pytest.approx({per_run.id: 8.0, per_item.id: 3.0, silent.id: 1.5})

        org = await roi_report.load_report(session, window="7d", currency="USD", now=now)
        # Every agent's spend, the agentless chat left out.
        assert org.organization.llm_cost == pytest.approx(12.5)
        assert org.cost_source == roi_spend.COST_SOURCE and org.unallocated_cost == pytest.approx(1.5)
        by_agent = {g.id: g.figures.llm_cost for g in org.agents}
        assert by_agent == pytest.approx({str(per_run.id): 8.0, str(per_item.id): 3.0, str(silent.id): 1.5})

        for agent, expected in ((per_run, 8.0), (per_item, 3.0), (silent, 1.5)):
            scoped = await roi_report.load_report(
                session, window="7d", currency="USD", now=now, scope="agent", scope_id=agent.id
            )
            assert scoped.organization.llm_cost == pytest.approx(expected)

        fleet_report = await roi_report.load_report(
            session, window="7d", currency="USD", now=now, scope="fleet", scope_id=fleet.id
        )
        assert fleet_report.organization.llm_cost == pytest.approx(4.5)
