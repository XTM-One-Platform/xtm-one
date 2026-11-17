"""Department-grade LLM analytics against PostgreSQL.

One synthetic tenant, nothing from a real one: 40 people, 3 departments (two
from the directory department, one from a group rule), 12 groups of mixed
kinds with the administrator in every one, 5 agents of which 2 are deleted
after their usage, an unpriced model, and 6 months of traces where about a
third have no user (assignment runs, sub-agents, the platform's own calls).

The traces are written as an older build wrote them (no attribution), rolled
up, and the oldest weeks purged as the retention purge does: what the
attribution backfill then stamps is the history an upgraded platform has.
Every figure is read through the tool, as an agent calls it.
"""

from __future__ import annotations

import json
import uuid
from collections import defaultdict
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import delete, func, select, text

import app.models  # noqa: F401 - registers every mapper on Base.metadata
from app.core.database import Base
from app.models.agent import Agent
from app.models.assignment import Assignment, AssignmentRun
from app.models.department import Department, DepartmentGroupRule
from app.models.group import Group, GroupMember
from app.models.llm_model_price import LLMModelPrice
from app.models.llm_trace import LLMDailyStats, LLMTrace
from app.models.user import User
from tests.frozen_clock import FROZEN_NOW, freeze_datetime
from tests.scratch_database import scratch_database

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

#: The instant the fixture writes around and the analytics read at: a
#: run crossing midnight UTC on the last day of a month no longer puts the
#: seeded "current month" and the one the service reads in two months.
NOW = FROZEN_NOW
#: Traced days: every sixth day over the last 180, today's included.
TRACED_DAYS = tuple(range(0, 181, 6))
#: The days older than this are purged after their rollup: only the rollup is left of them.
PURGED_BEFORE_DAYS = 150

_TABLES = [table for table in Base.metadata.sorted_tables if table.name != "embeddings"]

DIRECTORY = {
    **{f"u{index:02d}": "Engineering" for index in range(0, 13)},
    **{f"u{index:02d}": "R&D" for index in (5, 6)},
    **{f"u{index:02d}": "sales" for index in range(13, 25)},
}
FINANCE_TEAM = [f"u{index:02d}" for index in range(25, 33)]
UNASSIGNED = [f"u{index:02d}" for index in range(33, 40)]
#: (name, kind, members besides the administrator).
GROUPS = [
    ("Finance Team", "org_unit", FINANCE_TEAM),
    ("Platform Engineering", "org_unit", [f"u{index:02d}" for index in range(1, 7)] + ["u25"]),
    ("VPN", "access", [f"u{index:02d}" for index in range(1, 40)]),
    ("Office Paris", "access", [f"u{index:02d}" for index in range(1, 21)]),
    ("IT Admins", "access", ["u01"]),
    ("Desk A", "access", ["u33", "u34"]),
    ("All Hands", "distribution", [f"u{index:02d}" for index in range(1, 40)]),
    ("Newsletter", "distribution", ["u10", "u20", "u30"]),
    ("Leadership", "distribution", ["u01", "u13"]),
    ("saml-grp-1", "auto", ["u35"]),
    ("saml-grp-2", "auto", ["u36", "u37"]),
    ("saml-grp-3", "auto", ["u38"]),
]
#: key -> (name, owner, deleted after the usage).
AGENTS = {
    "helpdesk": ("Helpdesk", "u01", False),
    "sales": ("Sales Copilot", "u13", False),
    "finance": ("Finance Bot", "u25", False),
    "researcher": ("Old Researcher", "u02", True),
    "writer": ("Legacy Writer", "u14", True),
}
UNPRICED_MODEL = "model-unpriced"


@pytest.fixture(autouse=True)
def _frozen_insights_clock(monkeypatch):
    from app.services.platform_insights import analytics, scope
    from app.services.tracing import read_store

    freeze_datetime(monkeypatch, analytics, scope, read_store)


def _noon(days_ago: int) -> datetime:
    day = (NOW - timedelta(days=days_ago)).date()
    midnight = datetime.combine(day, datetime.min.time(), tzinfo=UTC)
    # Today's traces are in the past (the window ends now) and stay on today: a
    # suite started minutes after 00:00 UTC on the 1st still seeds the current month.
    return min(midnight + timedelta(hours=12), max(NOW - timedelta(minutes=5), midnight))


@dataclass
class Tenant:
    sessions: object
    people: dict[str, User]
    groups: dict[str, Group]
    agents: dict[str, uuid.UUID]
    departments: dict[str, uuid.UUID]
    runs: dict[str, uuid.UUID] = field(default_factory=dict)
    #: kind -> trace count, as the fixture wrote them.
    counts: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    total_cost: float = 0.0


def _traces(people: dict[str, User], agents: dict[str, uuid.UUID], runs: dict[str, uuid.UUID]):
    """Every trace of the tenant: ``(kind, row)``."""
    for days_ago in TRACED_DAYS:
        started = _noon(days_ago)
        parent_id = None
        for index in range(40):
            name = f"u{index:02d}"
            if (index + days_ago // 6) % 4:
                continue
            agent = "sales" if 13 <= index < 25 else "finance" if 25 <= index < 33 else "helpdesk"
            trace_id = uuid.uuid4()
            parent_id = parent_id or trace_id
            yield "chat", {
                "id": trace_id, "user_id": people[name].id, "agent_id": agents[agent], "source": "agent-chat",
                "model": "model-a", "cost": 0.01 * (1 + index % 5), "started_at": started,
            }
        for _ in range(3):
            yield "assignment", {
                "id": uuid.uuid4(), "user_id": None, "agent_id": agents["finance"], "source": "assignment",
                "assignment_run_id": runs["finance"], "model": "model-b", "cost": 0.05, "started_at": started,
            }
        yield "assignment", {
            "id": uuid.uuid4(), "user_id": None, "agent_id": agents["helpdesk"], "source": "assignment",
            "assignment_run_id": runs["helpdesk"], "model": "model-b", "cost": 0.04, "started_at": started,
        }
        if parent_id is not None:
            yield "sub_agent", {
                "id": uuid.uuid4(), "user_id": None, "agent_id": agents["helpdesk"], "source": "agent-chat",
                "parent_trace_id": parent_id, "model": "model-a", "cost": 0.02, "started_at": started,
            }
        if days_ago % 12 == 0:
            yield "system", {
                "id": uuid.uuid4(), "user_id": None, "agent_id": None, "source": "memory",
                "model": "model-b", "cost": 0.01, "started_at": started,
            }
        if days_ago % 18 == 0:
            yield "unpriced", {
                "id": uuid.uuid4(), "user_id": people["u33"].id, "agent_id": agents["helpdesk"],
                "source": "agent-chat", "model": UNPRICED_MODEL, "cost": None, "started_at": started,
            }
        if days_ago % 30 == 0:
            for person, agent in (("u02", "researcher"), ("u14", "writer")):
                yield "deleted_agent", {
                    "id": uuid.uuid4(), "user_id": people[person].id, "agent_id": agents[agent],
                    "source": "agent-chat", "model": "model-a", "cost": 0.03, "started_at": started,
                }


_INSERT_TRACE = text(
    "INSERT INTO llm_traces (id, source, name, started_at, status, level, model, user_id, agent_id, "
    "assignment_run_id, parent_trace_id, input_tokens, output_tokens, total_tokens, total_cost, duration_ms) "
    "VALUES (:id, :source, 'fixture', :started_at, 'ok', 'DEFAULT', :model, :user_id, :agent_id, "
    ":assignment_run_id, :parent_trace_id, 100, 50, 150, :cost, 200)"
)


@asynccontextmanager
async def _tenant():
    from app.services.departments import invalidate_department_map
    from app.services.trace_storage import aggregate_daily_stats, rebuild_daily_stats
    from app.services.tracing.attribution_backfill import run_attribution_backfill

    async with scratch_database("insights_departments", tables=_TABLES, extensions=("vector",)) as db:
        async with db.engine.begin() as conn:
            await conn.execute(
                text("INSERT INTO platform_settings (id, created_at, updated_at) VALUES (:id, now(), now())"),
                {"id": str(uuid.uuid4())},
            )
        invalidate_department_map()
        async with db.sessions() as session:
            people = {
                f"u{index:02d}": User(
                    email=f"u{index:02d}@example.test",
                    hashed_password="x",
                    full_name=f"Person {index:02d}",
                    is_active=True,
                    role="admin" if index == 0 else "user",
                    directory_department=DIRECTORY.get(f"u{index:02d}"),
                )
                for index in range(40)
            }
            session.add_all(people.values())
            await session.flush()
            groups: dict[str, Group] = {}
            for name, kind, members in GROUPS:
                group = Group(name=name, kind=kind)
                session.add(group)
                await session.flush()
                groups[name] = group
                for member in {"u00", *members}:
                    session.add(GroupMember(group_id=group.id, user_id=people[member].id))
            agents: dict[str, uuid.UUID] = {}
            for key, (name, owner, _deleted) in AGENTS.items():
                agent = Agent(name=name, user_id=people[owner].id, is_company_managed=key == "finance")
                session.add(agent)
                await session.flush()
                agents[key] = agent.id
            runs: dict[str, uuid.UUID] = {}
            for key, owner, acting in (("finance", "u25", "u26"), ("helpdesk", "u01", None)):
                assignment = Assignment(
                    agent_id=agents[key], user_id=people[owner].id, name=f"{key} digest", trigger_type="schedule", prompt="x"
                )
                session.add(assignment)
                await session.flush()
                run = AssignmentRun(
                    assignment_id=assignment.id, agent_id=agents[key], user_id=people[acting].id if acting else None
                )
                session.add(run)
                await session.flush()
                runs[key] = run.id
            await session.commit()

            tenant = Tenant(db.sessions, people, groups, agents, {}, runs)
            for kind, row in _traces(people, agents, runs):
                await session.execute(
                    _INSERT_TRACE, {"assignment_run_id": None, "parent_trace_id": None, **row}
                )
                tenant.counts[kind] += 1
                tenant.total_cost += row["cost"] or 0.0
            await session.commit()

            # Rolled up as the older build did, then the oldest weeks purged.
            for days_ago in TRACED_DAYS:
                if days_ago >= 2:
                    assert await aggregate_daily_stats(session, target_date=_noon(days_ago).date()) > 0
            await session.execute(
                delete(LLMTrace).where(LLMTrace.started_at < NOW - timedelta(days=PURGED_BEFORE_DAYS))
            )
            await session.commit()

            # The first mapping: two departments from the directory, one from a group rule.
            for name, values in (("Engineering", ["R&D"]), ("Sales", []), ("Finance", [])):
                department = Department(name=name, directory_values=values or None)
                session.add(department)
                await session.flush()
                tenant.departments[name] = department.id
            session.add_all(
                [
                    DepartmentGroupRule(
                        department_id=tenant.departments["Finance"], group_id=groups["Finance Team"].id, priority=0
                    ),
                    DepartmentGroupRule(
                        department_id=tenant.departments["Engineering"],
                        group_id=groups["Platform Engineering"].id,
                        priority=1,
                    ),
                ]
            )
            await session.commit()
            invalidate_department_map()

            for _ in range(10):
                result = await run_attribution_backfill(session, budget_seconds=600)
                if result.completed:
                    break
            assert result.completed
            stale = (await session.execute(text("SELECT date FROM llm_daily_stats_stale_days"))).scalars().all()
            for day in stale:
                await rebuild_daily_stats(session, day)

            for key, (_name, _owner, deleted) in AGENTS.items():
                if deleted:
                    await session.execute(delete(Agent).where(Agent.id == agents[key]))
            await session.commit()
        yield tenant
        invalidate_department_map()


async def _call(user: User, **args) -> dict | str:
    from app.services.builtin_tools import build_platform_insights_tools

    _defs, executor = build_platform_insights_tools(str(user.id), chart_sink=[])
    raw = await executor.handler("query_platform_analytics")(**args)
    try:
        return json.loads(raw)
    except ValueError:
        return raw


def _resolved(person: str) -> str:
    """The department the fixture means *person* to resolve to."""
    if person in DIRECTORY:
        return "Engineering" if DIRECTORY[person] in {"Engineering", "R&D"} else "Sales"
    if person in FINANCE_TEAM:
        return "Finance"
    return "Unassigned"


async def test_department_rows_partition_the_total_and_the_admin_counts_once():
    async with _tenant() as tenant:
        admin = tenant.people["u00"]
        result = await _call(admin, metric="cost", group_by="department", period="1y")
        assert isinstance(result, dict), result
        assert result["totals"]["total_cost"] == pytest.approx(tenant.total_cost, abs=0.01)
        assert sum(row["value"] for row in result["rows"]) == pytest.approx(result["totals"]["total_cost"], abs=0.01)
        assert {row["dimension"] for row in result["rows"]} <= {
            "Engineering", "Sales", "Finance", "Unassigned", "Unattributed (no person)"
        }

        people = await _call(admin, metric="traces", group_by="user", split_by="department", period="1y", limit=200)
        departments_of: dict[str, set[str]] = defaultdict(set)
        for row in people["rows"]:
            departments_of[row["bucket"]].add(row["dimension"])
        assert departments_of["u00@example.test"] == {"Engineering"}
        for email, seen in departments_of.items():
            assert seen == {_resolved(email.split("@")[0])}, email

        # Groups overlap - the admin is in all 12 - and say so.
        groups = await _call(admin, metric="cost", group_by="group", period="1y")
        assert groups["overlapping_membership"] is True
        assert sum(row["value"] for row in groups["rows"]) > result["totals"]["total_cost"]
        org_units = await _call(admin, metric="cost", group_by="group", group_kinds=["org_unit"], period="1y")
        assert {row["dimension"] for row in org_units["rows"]} == {"Finance Team", "Platform Engineering"}


async def test_the_unattributed_block_holds_the_system_traces_only():
    async with _tenant() as tenant:
        result = await _call(tenant.people["u00"], metric="traces", group_by="user", period="1y")
        assert result["unattributed"]["trace_count"] == tenant.counts["system"]
        assert set(result["unattributed"]["classes"]) == {"system"}

        raw = await _call(tenant.people["u00"], metric="traces", group_by="user", period="1y", attribution="raw")
        no_user = tenant.counts["assignment"] + tenant.counts["sub_agent"] + tenant.counts["system"]
        assert raw["unattributed"]["trace_count"] == no_user
        assert raw["attribution"] == "raw"


async def test_complement_and_exclusions_are_group_arithmetic():
    async with _tenant() as tenant:
        admin = tenant.people["u00"]
        named = ["Engineering", "Sales", "Finance"]
        others = await _call(
            admin, metric="traces", group_by="user", department_ids=named, complement=True, period="1y", limit=200
        )
        assert {row["dimension"].split("@")[0] for row in others["rows"]} == {
            person for person in UNASSIGNED if (await _has_usage(tenant, person))
        }

        everything = await _call(admin, metric="cost", period="1y")
        excluded = await _call(admin, metric="cost", period="1y", exclude_group_ids=["Finance Team"])
        finance_team = await _call(admin, metric="cost", period="1y", group_ids=["Finance Team"])
        assert everything["totals"]["total_cost"] - excluded["totals"]["total_cost"] == pytest.approx(
            finance_team["totals"]["total_cost"], abs=1e-6
        )
        # Nothing but the excluded people's usage left: no member of the group in the rest.
        by_user = await _call(admin, metric="cost", group_by="user", period="1y", exclude_group_ids=["Finance Team"], limit=200)
        members = {f"{person}@example.test" for person in [*FINANCE_TEAM, "u00"]}
        assert not members & {row["dimension"] for row in by_user["rows"]}

        refused = await _call(admin, metric="cost", complement=True)
        assert isinstance(refused, str) and "name some" in refused


async def _has_usage(tenant: Tenant, person: str) -> bool:
    async with tenant.sessions() as session:
        user_id = tenant.people[person].id
        count = (
            await session.execute(
                select(func.coalesce(func.sum(LLMDailyStats.trace_count), 0)).where(
                    func.coalesce(LLMDailyStats.attributed_user_id, LLMDailyStats.user_id) == user_id
                )
            )
        ).scalar()
        live = (
            await session.execute(select(func.count(LLMTrace.id)).where(LLMTrace.attributed_user_id == user_id))
        ).scalar()
        return bool(count or live)


async def test_a_deleted_agent_keeps_its_name_and_says_it_is_deleted():
    async with _tenant() as tenant:
        result = await _call(tenant.people["u00"], metric="cost", group_by="agent", period="1y")
        rows = {row["dimension_id"]: row for row in result["rows"] if row["dimension_id"]}
        for key, (name, _owner, deleted) in AGENTS.items():
            row = rows[str(tenant.agents[key])]
            assert row["dimension"] == name
            assert row["status"] == ("deleted" if deleted else "active")
        assert all(row["dimension"] != row["dimension_id"] for row in result["rows"])
        assert rows[str(tenant.agents["finance"])]["company_managed"] is True
        assert rows[str(tenant.agents["helpdesk"])]["owner_email"] == "u01@example.test"


async def test_the_budget_question_in_two_calls():
    async with _tenant() as tenant:
        admin = tenant.people["u00"]
        series = await _call(
            admin,
            metric="cost",
            group_by="month",
            split_by="department",
            period="1y",
            projection={"method": "linear", "horizon_months": 14},
        )
        assert isinstance(series, dict), series
        months = {row["bucket"] for row in series["rows"]}
        assert len(months) >= 6
        # One series per department, never overlapping: each month adds up to its total.
        per_month: dict[str, float] = defaultdict(float)
        for row in series["rows"]:
            per_month[row["bucket"]] += row["value"]
        monthly_total = await _call(admin, metric="cost", group_by="month", period="1y")
        for row in monthly_total["rows"]:
            assert per_month[row["bucket"]] == pytest.approx(row["value"], abs=1e-6)
        current = date(NOW.year, NOW.month, 1).isoformat()
        assert any(row.get("partial") for row in series["rows"] if row["bucket"] == current)
        projection = series["projection"]
        fitted = [entry for entry in projection["series"] if "projected" in entry]
        assert {entry["dimension"] for entry in fitted} >= {"Engineering", "Sales", "Finance"}
        for entry in fitted:
            assert len(entry["projected"]) == 14
            assert entry["current_month"]["partial"] is True
            assert "r2" in entry and "slope_per_month" in entry["params"]
        assert current not in projection["basis_months"]
        assert len(projection["projected_total"]) == 14

        top = await _call(
            admin, metric="cost", group_by="agent", department_ids=["Engineering"], period="1y", limit=5
        )
        assert 0 < len(top["rows"]) <= 5
        assert all(row["dimension"] != row["dimension_id"] for row in top["rows"])
        assert top["attribution"] == "attributed"


async def test_the_unpriced_model_is_listed_and_a_dated_price_fills_its_cost_from_that_date():
    from app.services.tracing.repricing import reprice_null_costs

    async with _tenant() as tenant:
        result = await _call(tenant.people["u00"], metric="cost", period="90d")
        assert [item["model"] for item in result["unpriced_models"]] == [UNPRICED_MODEL]
        assert result["unknown_cost_trace_count"] == result["unpriced_models"][0]["trace_count"] > 0
        assert any(UNPRICED_MODEL in caveat for caveat in result["caveats"])

        effective = (NOW - timedelta(days=40)).date()
        async with tenant.sessions() as session:
            session.add(
                LLMModelPrice(
                    provider="custom", model_id=UNPRICED_MODEL, input_price_per_1m=1, output_price_per_1m=2,
                    effective_from=effective, is_custom=True,
                )
            )
            await session.commit()
            await reprice_null_costs(session, model=UNPRICED_MODEL, enforce_effective_from=True)
            rows = (
                await session.execute(select(LLMTrace.started_at, LLMTrace.total_cost).where(LLMTrace.model == UNPRICED_MODEL))
            ).all()
        assert rows
        for started_at, cost in rows:
            if started_at.date() >= effective:
                assert cost is not None and cost > 0
            else:
                assert cost is None


async def test_the_backfill_is_idempotent():
    from app.services.tracing.attribution_backfill import restart_walk, run_attribution_backfill

    async with _tenant() as tenant, tenant.sessions() as session:
        snapshot = select(
            LLMTrace.id, LLMTrace.attributed_user_id, LLMTrace.attribution, LLMTrace.department_id
        ).order_by(LLMTrace.id)
        before = (await session.execute(snapshot)).all()
        rollup = select(
            LLMDailyStats.id, LLMDailyStats.attributed_user_id, LLMDailyStats.attribution, LLMDailyStats.department_id
        ).order_by(LLMDailyStats.id)
        rollup_before = (await session.execute(rollup)).all()
        assert all(row.attribution is not None for row in before)
        assert all(row.attribution is not None for row in rollup_before)

        await restart_walk(session)
        await session.commit()
        for _ in range(10):
            again = await run_attribution_backfill(session, budget_seconds=600)
            assert again.traces == 0 and again.rollup_rows == 0
            if again.completed:
                break
        assert again.completed
        assert (await session.execute(snapshot)).all() == before
        assert (await session.execute(rollup)).all() == rollup_before


async def test_the_export_holds_the_query_totals():
    from app.services.platform_insights.analytics import export_rows
    from app.services.platform_insights.scope import load_scope

    async with _tenant() as tenant, tenant.sessions() as session:
        scope = await load_scope(session, str(tenant.people["u00"].id))
        for args in (
            {"group_by": "department", "period": "1y"},
            {"group_by": "month", "split_by": "department", "period": "1y"},
            {"group_by": "user", "period": "1y"},
            {"group_by": "agent", "split_by": "model", "period": "90d"},
        ):
            header, rows, totals = await export_rows(session, scope, args)
            query = await _call(tenant.people["u00"], metric="cost", **args)
            cost = header.index("cost")
            traces = header.index("traces")
            assert sum(row[cost] or 0 for row in rows) == pytest.approx(query["totals"]["total_cost"], abs=1e-6), args
            assert sum(row[traces] for row in rows) == query["totals"]["trace_count"] == totals["trace_count"], args


async def test_every_cell_of_the_matrix_answers_or_names_an_alternative():
    from app.services.platform_insights.analytics import DIMENSIONS, GROUP_BYS, METRICS

    async with _tenant() as tenant:
        admin = tenant.people["u00"]
        for metric in METRICS:
            for group_by in (None, *GROUP_BYS):
                for split_by in (None, *DIMENSIONS):
                    args = {"metric": metric, "period": "30d"}
                    if group_by:
                        args["group_by"] = group_by
                    if split_by:
                        args["split_by"] = split_by
                    result = await _call(admin, **args)
                    if isinstance(result, dict):
                        assert isinstance(result["rows"], list), args
                        assert result["capabilities"]["metrics"] == list(METRICS)
                    else:
                        assert result.startswith("Error:"), (args, result)
                        assert "use " in result.casefold(), (args, result)


async def test_the_sink_stamps_a_trace_when_it_is_written():
    from app.services.departments import invalidate_department_map
    from app.services.tracing.base import TraceData
    from app.services.tracing.local_sink import LocalPGSink

    async with _tenant() as tenant:
        invalidate_department_map()
        sink = LocalPGSink(session_factory=tenant.sessions)
        run_trace = TraceData(
            id=uuid.uuid4(),
            source="assignment",
            name="digest",
            started_at=NOW - timedelta(minutes=1),
            agent_id=tenant.agents["finance"],
            assignment_run_id=tenant.runs["finance"],
            model="model-b",
            input_tokens=1000,
            output_tokens=100,
            usage_details={"input_tokens": 1000, "output_tokens": 100, "cache_read_input_tokens": 400},
        )
        sub_agent = TraceData(
            id=uuid.uuid4(),
            source="agent-chat",
            name="consulted",
            started_at=NOW - timedelta(seconds=30),
            agent_id=tenant.agents["helpdesk"],
            parent_trace_id=run_trace.id,
            model="model-a",
        )
        platform_call = TraceData(id=uuid.uuid4(), source="memory", name="compaction", started_at=NOW, model="model-b")
        for trace in (run_trace, sub_agent, platform_call):
            await sink.flush_trace(trace, [])
        assert sink.tracing_errors == 0

        async with tenant.sessions() as session:
            rows = {
                row.id: row
                for row in (
                    await session.execute(
                        select(LLMTrace).where(LLMTrace.id.in_([run_trace.id, sub_agent.id, platform_call.id]))
                    )
                ).scalars()
            }
        stamped = rows[run_trace.id]
        # The run's acting identity, not the assignment's owner (u25), and their department.
        assert (stamped.attribution, stamped.attributed_user_id) == ("assignment", tenant.people["u26"].id)
        assert stamped.user_id is None
        assert stamped.department_id == tenant.departments["Finance"]
        assert stamped.department_resolved_at is not None and stamped.department_backfilled is False
        assert stamped.agent_name_snapshot == "Finance Bot"
        assert stamped.cached_input_tokens == 400
        child = rows[sub_agent.id]
        assert (child.attribution, child.attributed_user_id) == ("parent", tenant.people["u26"].id)
        system = rows[platform_call.id]
        assert (system.attribution, system.attributed_user_id, system.department_id) == ("system", None, None)


@asynccontextmanager
async def _client(tenant: Tenant, user: User):
    import httpx
    from fastapi import FastAPI

    from app.api import deps
    from app.api.v1 import groups as groups_api
    from app.api.v1 import insights as insights_api
    from app.core.database import get_db

    app = FastAPI()
    app.include_router(insights_api.router, prefix="/api/v1/insights")
    app.include_router(groups_api.router, prefix="/api/v1/groups")

    async def _get_db():
        async with tenant.sessions() as db:
            yield db
            await db.commit()

    app.dependency_overrides[deps.get_current_user] = lambda: user
    app.dependency_overrides[get_db] = _get_db
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        yield client


async def test_the_mapping_is_an_administrators_and_the_export_reads_the_callers_scope():
    import csv
    import io

    async with _tenant() as tenant:
        async with _client(tenant, tenant.people["u26"]) as client:
            assert (await client.get("/api/v1/insights/departments")).status_code == 403
            assert (await client.put("/api/v1/insights/departments", json={"departments": []})).status_code == 403
            assert (await client.post("/api/v1/insights/departments/history", json={})).status_code == 403
            vpn = tenant.groups["VPN"].id
            assert (await client.put(f"/api/v1/groups/{vpn}", json={"kind": "access"})).status_code == 403
            export = await client.get("/api/v1/insights/analytics/export", params={"group_by": "user", "period": "1y"})
            assert export.status_code == 200 and export.headers["content-type"].startswith("text/csv")
            rows = list(csv.reader(io.StringIO(export.text)))
            assert rows[0][:2] == ["user", "user_id"]
            assert {row[0] for row in rows[1:]} == {"u26@example.test"}

        async with _client(tenant, tenant.people["u00"]) as client:
            mapping = (await client.get("/api/v1/insights/departments")).json()
            assert {item["name"] for item in mapping["departments"]} == {"Engineering", "Sales", "Finance"}
            assert [rule["group_name"] for rule in mapping["rules"]] == ["Finance Team", "Platform Engineering"]
            assert mapping["backfill"]["completed_at"] is not None
            preview = (await client.get("/api/v1/insights/departments/preview")).json()
            assert preview["unassigned_count"] == len(UNASSIGNED)

            # Platform Engineering first: u25, in both groups and in no directory, moves to Engineering.
            body = {
                "departments": [*mapping["departments"], {"name": "Marketing", "directory_values": ["Mktg"]}],
                "rules": [
                    {"group_id": rule["group_id"], "department": rule["department"]} for rule in reversed(mapping["rules"])
                ],
                "settings": mapping["settings"],
            }
            saved = await client.put("/api/v1/insights/departments", json=body)
            assert saved.status_code == 200, saved.text
            assert [rule["group_name"] for rule in saved.json()["rules"]] == ["Platform Engineering", "Finance Team"]
            preview = (await client.get("/api/v1/insights/departments/preview")).json()
            engineering = next(item for item in preview["departments"] if item["name"] == "Engineering")
            assert "u25@example.test" in {person["email"] for person in engineering["users"]}

            imported = await client.post(
                "/api/v1/insights/departments/directory",
                json={"entries": [{"email": "U33@example.test", "department": "mktg"}, {"email": "ghost@example.test", "department": "X"}]},
            )
            assert imported.json()["updated"] == 1 and imported.json()["unknown_emails"] == ["ghost@example.test"]
            preview = (await client.get("/api/v1/insights/departments/preview")).json()
            marketing = next(item for item in preview["departments"] if item["name"] == "Marketing")
            assert [person["email"] for person in marketing["users"]] == ["u33@example.test"]

            vpn = tenant.groups["VPN"].id
            kind = await client.put(f"/api/v1/groups/{vpn}", json={"kind": "distribution"})
            assert kind.status_code == 200 and kind.json()["kind"] == "distribution"
            assert (await client.put(f"/api/v1/groups/{vpn}", json={"kind": "team"})).status_code == 422

            duplicate = {**body, "departments": [*body["departments"], {"name": "marketing", "directory_values": []}]}
            assert (await client.put("/api/v1/insights/departments", json=duplicate)).status_code == 422

            state = (await client.post("/api/v1/insights/departments/history", json={"rewrite": True})).json()
            assert state["rewrite_requested_at"] is not None and state["completed_at"] is None


async def test_a_non_admin_reads_the_usage_attributed_to_them():
    async with _tenant() as tenant:
        # u26 runs the finance assignment: its traces have no user, and count for them.
        mine = await _call(tenant.people["u26"], metric="traces", group_by="source", period="1y")
        assert mine["scope"] == "self"
        sources = {row["dimension"]: row["value"] for row in mine["rows"]}
        assert sources.get("assignment", 0) > 0
        raw = await _call(tenant.people["u26"], metric="traces", group_by="source", period="1y", attribution="raw")
        assert "assignment" not in {row["dimension"] for row in raw["rows"]}
