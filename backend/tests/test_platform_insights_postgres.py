"""The platform insights tools against PostgreSQL and the dashboards they mirror.

Every fixture is synthetic and made here: four people, two groups (one person in
both), two agents, traces over 90 days (some days rolled up, one left live),
two recorded builds, presentations created before and under each build, AI
adoption rows and audit entries. Each tool is then read through its builder,
as an agent calls it, and compared with the REST endpoint the admin page reads
on the same filters: what an agent says and what the dashboard shows must not
drift apart.
"""

from __future__ import annotations

import json
import uuid
from collections import defaultdict
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import func, select, text

import app.models  # noqa: F401 - registers every mapper on Base.metadata
from app.core.database import Base
from app.models.agent import Agent
from app.models.ai_adoption_metric import AiAdoptionMetric
from app.models.audit_log import AuditLog
from app.models.group import Group, GroupMember
from app.models.platform_build import PlatformBuild
from app.models.presentation import Presentation
from app.models.user import User
from tests.scratch_database import scratch_database

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

NOW = datetime.now(UTC)


def _noon(days_ago: int) -> datetime:
    day = (NOW - timedelta(days=days_ago)).date()
    return datetime.combine(day, datetime.min.time(), tzinfo=UTC) + timedelta(hours=12)


#: (person, days ago, cost, agent, model): the traces of the window.
TRACES = [
    ("alice", 3, 0.10, "a", "model-one"),
    ("alice", 12, 0.20, "a", "model-two"),
    ("alice", 70, 0.30, "b", "model-one"),
    ("bob", 25, 0.40, "a", "model-one"),
    ("bob", 45, 0.50, "a", "model-two"),
    ("carol", 12, 0.60, "b", "model-one"),
    (None, 3, 0.70, None, "model-one"),
    ("admin", 45, 0.80, "a", "model-two"),
]
#: Days rolled up by the aggregator; day 3 stays live (no rollup), as the read path serves it.
ROLLED_UP_DAYS = (12, 25, 45, 70)
#: (owner, days ago) of the presentations.
DECKS = [("alice", 80), ("alice", 50), ("bob", 30), ("carol", 5), ("alice", 2)]
#: The two recorded builds, by days ago of their first boot.
BUILDS = (("1.0.0", "aaaaaaa1", 60), ("1.1.0", "bbbbbbb2", 20))

#: Every table but the knowledge-base embeddings, which nothing here reads and
#: whose half-precision index needs a newer pgvector than some local servers have.
_TABLES = [table for table in Base.metadata.sorted_tables if table.name != "embeddings"]


@asynccontextmanager
async def _platform():
    """A seeded scratch platform; yields ``(sessions, people, groups)``."""
    from app.services.trace_storage import aggregate_daily_stats

    async with scratch_database("insights", tables=_TABLES, extensions=("vector",)) as db:
        async with db.engine.begin() as conn:
            await conn.execute(
                text("INSERT INTO platform_settings (id, created_at, updated_at) VALUES (:id, now(), now())"),
                {"id": str(uuid.uuid4())},
            )
        async with db.sessions() as session:
            people = {
                name: User(
                    email=f"{name}@example.test",
                    hashed_password="x",
                    full_name=name.title(),
                    is_active=True,
                    role="admin" if name == "admin" else "user",
                )
                for name in ("admin", "alice", "bob", "carol")
            }
            session.add_all(people.values())
            await session.flush()
            success, sales = Group(name="Customer Success"), Group(name="Sales")
            session.add_all([success, sales])
            await session.flush()
            session.add_all(
                [
                    GroupMember(group_id=success.id, user_id=people["alice"].id),
                    GroupMember(group_id=success.id, user_id=people["bob"].id),
                    GroupMember(group_id=sales.id, user_id=people["alice"].id),
                    GroupMember(group_id=sales.id, user_id=people["carol"].id),
                ]
            )
            agents = {key: Agent(name=f"Agent {key.upper()}", user_id=people[owner].id) for key, owner in (("a", "admin"), ("b", "alice"))}
            session.add_all(agents.values())
            await session.flush()
            agents = {key: agent.id for key, agent in agents.items()}
            for person, days_ago, cost, agent, model in TRACES:
                await session.execute(
                    text(
                        "INSERT INTO llm_traces (id, source, name, tags, started_at, status, level, model, user_id, "
                        "agent_id, input_tokens, output_tokens, total_tokens, total_cost, duration_ms) VALUES "
                        "(:id, 'agent-chat', 'insights', :tags, :started_at, 'ok', 'DEFAULT', :model, :user_id, "
                        ":agent_id, 10, 20, 30, :cost, 150)"
                    ),
                    {
                        "id": uuid.uuid4(),
                        "tags": ["source:agent-chat"],
                        "started_at": _noon(days_ago),
                        "model": model,
                        "user_id": people[person].id if person else None,
                        "agent_id": agents[agent] if agent else None,
                        "cost": cost,
                    },
                )
            for version, sha, days_ago in BUILDS:
                session.add(
                    PlatformBuild(
                        version=version,
                        git_sha=sha,
                        first_seen_at=NOW - timedelta(days=days_ago),
                        last_seen_at=NOW - timedelta(days=days_ago - 1),
                    )
                )
            for owner, days_ago in DECKS:
                session.add(
                    Presentation(
                        title=f"Deck {days_ago}",
                        user_id=people[owner].id,
                        created_at=NOW - timedelta(days=days_ago),
                        updated_at=NOW - timedelta(days=days_ago),
                    )
                )
            session.add_all(
                [
                    AiAdoptionMetric(
                        date=(NOW - timedelta(days=2)).date(),
                        provider="copilot_platform",
                        metric_type="usage",
                        user_id=people["alice"].id,
                        data={"conversations": 3, "messages": 10},
                    ),
                    AiAdoptionMetric(
                        date=(NOW - timedelta(days=4)).date(),
                        provider="copilot_platform",
                        metric_type="usage",
                        user_id=people["bob"].id,
                        data={"assignment_runs": 4},
                    ),
                    # A row a removed development provider left behind (#11380):
                    # never counted.
                    AiAdoptionMetric(
                        date=(NOW - timedelta(days=3)).date(),
                        provider="cursor",
                        metric_type="usage",
                        user_id=people["carol"].id,
                        data={"chat_requests": 40},
                    ),
                ]
            )
            for person, action in (("alice", "create"), ("bob", "delete"), ("admin", "update")):
                session.add(
                    AuditLog(
                        user_id=people[person].id,
                        user_email=people[person].email,
                        action=action,
                        entity_type="agent",
                        summary=f"{person} {action}d an agent",
                    )
                )
            await session.commit()
            for days_ago in ROLLED_UP_DAYS:
                assert await aggregate_daily_stats(session, target_date=_noon(days_ago).date()) > 0
            await session.commit()
            groups = {"success": success, "sales": sales}
            yield db.sessions, people, groups


async def _call(user: User, tool: str, **args) -> dict | str:
    from app.services.builtin_tools import build_platform_insights_tools

    _defs, executor = build_platform_insights_tools(str(user.id), chart_sink=[])
    raw = await executor.handler(tool)(**args)
    try:
        return json.loads(raw)
    except ValueError:
        return raw


def _expected_cost(*people: str | None) -> float:
    return sum(cost for person, _d, cost, _a, _m in TRACES if person in people)


async def test_analytics_totals_and_breakdowns_equal_the_dashboard_endpoints():
    from app.api.v1 import traces as traces_api
    from app.services.tracing.store import build_trace_read_store

    async with _platform() as (sessions, people, _groups), sessions() as session:
        admin = people["admin"]
        store = build_trace_read_store()
        filters = traces_api.AnalyticsFilters(period="90d")

        total = await _call(admin, "query_platform_analytics", metric="cost", period="90d")
        summary = await traces_api.analytics_summary(filters=filters, user=admin, db=session, trace_store=store)
        assert total["scope"] == "platform"
        assert total["totals"]["total_cost"] == pytest.approx(summary.total_cost)
        assert total["totals"]["trace_count"] == summary.trace_count == len(TRACES)
        assert total["totals"]["total_tokens"] == summary.total_tokens
        assert total["rows"] == [{"bucket": None, "dimension": None, "value": pytest.approx(summary.total_cost)}]
        assert total["aggregation_stale"] is summary.aggregation_stale
        assert total["stats_coverage_start"] == str(summary.stats_coverage_start)

        # The dashboard reads the traces' own users: the tool does too with attribution raw.
        by_user = await _call(
            admin, "query_platform_analytics", metric="cost", group_by="user", period="90d", attribution="raw"
        )
        users_view = await traces_api.analytics_cost_by_user(
            filters=filters, limit=50, user=admin, db=session, trace_store=store
        )
        assert {row["dimension"]: row["value"] for row in by_user["rows"]} == {
            item.user_email: pytest.approx(item.total_cost) for item in users_view.items
        }
        assert by_user["unattributed"]["total_cost"] == pytest.approx(users_view.unattributed.total_cost)
        assert by_user["unattributed"]["total_cost"] == pytest.approx(_expected_cost(None))

        by_group = await _call(admin, "query_platform_analytics", metric="cost", group_by="group", period="90d")
        groups_view = await traces_api.analytics_cost_by_group(filters=filters, user=admin, db=session, trace_store=store)
        assert {row["dimension"]: row["value"] for row in by_group["rows"]} == {
            item.group_name: pytest.approx(item.total_cost) for item in groups_view.items
        }
        assert by_group["ungrouped"]["total_cost"] == pytest.approx(groups_view.ungrouped.total_cost)
        assert by_group["overlapping_membership"] is groups_view.overlapping_membership is True
        assert any("NOW" in caveat for caveat in by_group["caveats"])

        by_agent = await _call(admin, "query_platform_analytics", metric="cost", group_by="agent", period="90d")
        agents_view = await traces_api.analytics_cost_by_agent(filters=filters, user=admin, db=session, trace_store=store)
        # The dashboard's agent table leaves the usage with no agent out; here it is a row, so the rows add up.
        assert {row["dimension_id"]: row["value"] for row in by_agent["rows"] if row["dimension_id"]} == {
            item.agent_id: pytest.approx(item.total_cost) for item in agents_view.items
        }
        assert sum(row["value"] for row in by_agent["rows"]) == pytest.approx(summary.total_cost)
        assert [row["dimension"] for row in by_agent["rows"] if row["dimension_id"] is None] == [
            "(no agent: General Assistant, proxy, platform calls)"
        ]

        by_model = await _call(admin, "query_platform_analytics", metric="traces", group_by="model", period="90d")
        models_view = await traces_api.analytics_cost_by_model(filters=filters, user=admin, db=session, trace_store=store)
        assert {row["dimension"]: row["value"] for row in by_model["rows"]} == {
            item.model: item.trace_count for item in models_view.items
        }

        by_day = await _call(admin, "query_platform_analytics", metric="cost", group_by="day", period="90d")
        over_time = await traces_api.analytics_cost_over_time(filters=filters, user=admin, db=session, trace_store=store)
        per_day: dict[str, float] = defaultdict(float)
        for point in over_time.series:
            per_day[point.date] += point.cost
        assert {row["bucket"]: row["value"] for row in by_day["rows"]} == {
            day: pytest.approx(cost) for day, cost in per_day.items()
        }

        by_week = await _call(admin, "query_platform_analytics", metric="cost", group_by="week", period="90d")
        assert sum(row["value"] for row in by_week["rows"]) == pytest.approx(summary.total_cost)
        assert all(date.fromisoformat(row["bucket"]).weekday() == 0 for row in by_week["rows"])


async def test_a_group_filter_reads_its_current_members_and_says_so():
    async with _platform() as (_sessions, people, _groups):
        result = await _call(
            people["admin"],
            "query_platform_analytics",
            metric="cost",
            group_by="week",
            period="90d",
            group_ids=["Customer Success"],
        )
        assert result["filters"]["groups"] == ["Customer Success"]
        assert sum(row["value"] for row in result["rows"]) == pytest.approx(_expected_cost("alice", "bob"))
        assert result["totals"]["total_cost"] == pytest.approx(_expected_cost("alice", "bob"))
        assert any("NOW" in caveat for caveat in result["caveats"])

        refused = await _call(people["admin"], "query_platform_analytics", group_ids=["No Such Team"])
        assert isinstance(refused, str) and "No Such Team" in refused


async def test_a_non_admin_reads_their_own_slice_and_is_told_so():
    async with _platform() as (_sessions, people, _groups):
        alice = people["alice"]
        mine = await _call(alice, "query_platform_analytics", metric="cost", group_by="user", period="90d")
        assert mine["scope"] == "self" and "own slice" in mine["scope_note"]
        assert [row["dimension"] for row in mine["rows"]] == ["alice@example.test"]
        assert mine["totals"]["total_cost"] == pytest.approx(_expected_cost("alice"))

        assert "only name yourself" in await _call(alice, "query_platform_analytics", user_ids=["bob@example.test"])
        # Bob is not in Sales: outside the administrator scope only one's own groups can be named.
        assert "your own groups" in await _call(people["bob"], "query_platform_analytics", group_ids=["Sales"])
        own_group = await _call(alice, "query_platform_analytics", group_ids=["Customer Success"], period="90d")
        assert own_group["totals"]["total_cost"] == pytest.approx(_expected_cost("alice"))

        decks = await _call(alice, "query_platform_objects", object_types=["presentation"])
        assert decks["scope"] == "self"
        assert decks["total"] == sum(1 for owner, _days in DECKS if owner == "alice")
        assert {item["owner_email"] for item in decks["items"]} == {"alice@example.test"}

        audit = await _call(alice, "query_audit_log")
        assert {entry["actor_email"] for entry in audit["entries"]} <= {"alice@example.test"}
        assert all("ip_address" not in entry for entry in audit["entries"])

        overview = await _call(alice, "get_platform_overview")
        assert overview["scope"] == "self" and "users" not in overview
        # The caller's own sandbox workspaces, without the administrator's top owners.
        assert overview["sandbox_workspaces"]["count"] == 0 and "top_owners" not in overview["sandbox_workspaces"]
        assert overview["objects_by_type"].get("presentation") == decks["total"]
        # Any caller can name its own instance: the Instance ID of Settings > Parameters (#690).
        from app.api.v1.platform_settings_helpers import get_platform_settings
        from app.core.config import settings
        from app.services.branding import get_cached_branding

        async with _sessions() as session:
            platform_settings = await get_platform_settings(session)
        assert isinstance(overview, dict)
        instance = overview["instance"]
        assert instance == {
            "instance_id": str(platform_settings.id),
            "platform_name": get_cached_branding().name,
            "url": settings.frontend_url,
        }

        adoption = await _call(alice, "query_ai_adoption", view="gaps")
        assert adoption["scope"] == "self" and "administrator view" in adoption["note"]
        assert {row["dimension"] for row in adoption["rows"]} == {"activity"}


async def test_hidden_costs_come_back_masked_never_as_zero(monkeypatch):
    from app.core.config import settings

    async with _platform() as (_sessions, people, _groups):
        monkeypatch.setattr(settings, "observability_cost_data_enabled", False)
        result = await _call(people["admin"], "query_platform_analytics", metric="cost", group_by="user", period="90d")
        assert result["cost_masked"] is True
        assert result["totals"]["total_cost"] is None
        assert result["rows"] and all(row["value"] is None for row in result["rows"])
        assert result["unattributed"]["total_cost"] is None
        assert any("hidden" in caveat for caveat in result["caveats"])
        # A masked cost is never drawn as a zero bar.
        refused = await _call(people["admin"], "render_chart", rows=result["rows"], kind="bar")
        assert isinstance(refused, str) and "masked" in refused

        traces = await _call(people["admin"], "query_platform_analytics", metric="traces", period="90d")
        assert traces["rows"][0]["value"] == len(TRACES)
        assert traces["totals"]["total_cost"] is None


async def test_bucket_release_counts_what_predates_the_history_apart():
    async with _platform() as (_sessions, people, _groups):
        admin = people["admin"]
        history = await _call(admin, "get_platform_release_history")
        assert [release["version"] for release in history["releases"]] == ["1.0.0", "1.1.0"]
        assert history["history_starts_at"] is not None

        decks = await _call(admin, "query_platform_objects", object_types=["presentation"], bucket="release")
        counts = {row["bucket"]: row["value"] for row in decks["rows"]}
        assert counts == {"before_release_history": 1, "1.0.0 (aaaaaaa)": 2, "1.1.0 (bbbbbbb)": 2}
        assert decks["rows"][0]["bucket"] == "before_release_history"
        assert any("history starts" in caveat for caveat in decks["caveats"])

        cost = await _call(admin, "query_platform_analytics", metric="cost", group_by="release", period="90d")
        by_release = {row["bucket"]: row["value"] for row in cost["rows"]}
        # Day 70 is before the first recorded boot (60 days ago): alice's 0.30 only.
        assert by_release["before_release_history"] == pytest.approx(0.30)
        assert by_release["1.1.0 (bbbbbbb)"] == pytest.approx(0.10 + 0.20 + 0.60 + 0.70)
        assert sum(by_release.values()) == pytest.approx(cost["totals"]["total_cost"])
        assert any("starts before it" in caveat for caveat in cost["caveats"])


async def test_objects_are_counted_per_week_and_filtered_by_group():
    async with _platform() as (_sessions, people, _groups):
        weekly = await _call(people["admin"], "query_platform_objects", object_types=["presentation"], bucket="week")
        assert sum(row["value"] for row in weekly["rows"]) == len(DECKS)
        assert all(row["dimension"] == "presentation" for row in weekly["rows"])

        success = await _call(
            people["admin"], "query_platform_objects", object_types=["presentation"], group_id=["Customer Success"],
            count_only=True,
        )
        assert success["counts_by_type"] == {"presentation": sum(1 for o, _ in DECKS if o in {"alice", "bob"})}

        owned = await _call(people["admin"], "query_platform_objects", owner=["carol@example.test"], count_only=True)
        assert owned["counts_by_type"]["presentation"] == 1

        overview = await _call(people["admin"], "get_platform_overview")
        everything = await _call(people["admin"], "query_platform_objects", count_only=True)
        assert {k: v for k, v in overview["objects_by_type"].items() if v} == {
            k: v for k, v in everything["counts_by_type"].items() if v
        }
        assert overview["users"] == {"total": 4, "active": 4}
        # The sandbox workspaces' footprint sits next to the files'.
        assert overview["sandbox_workspaces"]["bytes"] == 0 and overview["sandbox_workspaces"]["top_owners"] == []
        # The sandbox pods per image key: an administrator view, None when the server was not read.
        assert "sandbox_pods_by_image_key" in overview
        assert overview["groups"] == 2


async def test_fleets_are_objects_counted_listed_and_bucketed():
    """``fleet`` is a type of the union: counted, listed with its archived
    state, bucketed per month, scoped to the caller's own outside the
    administrator view, and counted by the overview."""
    from app.models.fleet import Fleet
    from app.models.workspace import Workspace

    async with _platform() as (sessions, people, _groups):
        async with sessions() as session:
            for owner, name, archived in (("alice", "Ops", False), ("bob", "Old", True)):
                home = Workspace(name=f"{name} home", slug=f"{name.lower()}-home", user_id=people[owner].id)
                session.add(home)
                await session.flush()
                session.add(
                    Fleet(
                        name=name,
                        slug=name.lower(),
                        workspace_id=home.id,
                        user_id=people[owner].id,
                        is_archived=archived,
                    )
                )
            await session.commit()

        admin = people["admin"]
        counted = await _call(admin, "query_platform_objects", object_types=["fleet"], count_only=True)
        assert counted["counts_by_type"] == {"fleet": 2}

        listed = await _call(admin, "query_platform_objects", object_types=["fleet"])
        # A NULL detail column is left out of the item, so an active fleet carries no status key.
        assert {(item["name"], item.get("status")) for item in listed["items"]} == {("Ops", None), ("Old", "archived")}

        monthly = await _call(admin, "query_platform_objects", object_types=["fleet"], bucket="month")
        assert sum(row["value"] for row in monthly["rows"]) == 2
        assert all(row["dimension"] == "fleet" for row in monthly["rows"])

        mine = await _call(people["alice"], "query_platform_objects", object_types=["fleet"])
        assert [item["name"] for item in mine["items"]] == ["Ops"]

        overview = await _call(admin, "get_platform_overview")
        assert overview["objects_by_type"]["fleet"] == 2


async def test_ai_adoption_rows_match_the_dashboard_agentic_only():
    from app.api.v1 import ai_adoption

    async with _platform() as (sessions, people, _groups), sessions() as session:
        admin = people["admin"]
        result = await _call(admin, "query_ai_adoption", view="overview", days=30)
        payload = await ai_adoption.get_ai_adoption_overview(days=30, group_id=None, user=admin, db=session)
        # alice and bob on the platform; carol's leftover Cursor row is not counted.
        assert result["totals"]["active_user_count"] == payload["active_user_count"] == 2
        assert "sync_status" not in result and "active_users_by_provider" not in payload
        assert {user["email"] for user in payload["top_users"]} == {people["alice"].email, people["bob"].email}

        gaps = await _call(admin, "query_ai_adoption", view="gaps", group_id=["Customer Success"])
        assert gaps["groups"] == ["Customer Success"]
        assert gaps["totals"]["total_users"] == 2


async def test_the_gaps_view_has_one_agentic_section():
    """#11380: no development split, no development groups."""
    from app.api.v1 import ai_adoption

    async with _platform() as (sessions, people, _groups), sessions() as session:
        payload = await ai_adoption.get_adoption_gaps(days=30, group_id=None, user=people["admin"], db=session)
        assert payload["total_users"] == 4
        assert set(payload["summary"]) == {"inactive_count", "low_activity_count", "active_count", "avg_active_days"}
        assert "dev_group_ids" not in payload and "total_dev_users" not in payload
        # bob scores 4, under the threshold; carol's leftover Cursor row is not counted.
        assert {entry["email"] for entry in payload["low_activity"]} == {people["bob"].email}
        assert people["carol"].email in {entry["email"] for entry in payload["inactive"]}

        result = await _call(people["admin"], "query_ai_adoption", view="gaps")
        assert result["totals"]["total_users"] == 4


async def test_the_running_build_is_recorded_once_per_version_and_commit(monkeypatch):
    from app.core.config import settings
    from app.services.platform_builds import record_running_build

    async with _platform() as (sessions, _people, _groups), sessions() as session:
        monkeypatch.setattr(settings, "app_version", "2.0.0")
        monkeypatch.setattr(settings, "git_sha", "ccccccc3")
        await record_running_build(session)
        first = (await session.execute(select(PlatformBuild).where(PlatformBuild.version == "2.0.0"))).scalar_one()
        seen_at = first.last_seen_at
        await record_running_build(session)
        session.expire_all()
        rows = (await session.execute(select(PlatformBuild).where(PlatformBuild.version == "2.0.0"))).scalars().all()
        assert len(rows) == 1 and rows[0].last_seen_at >= seen_at
        assert rows[0].first_seen_at == first.first_seen_at

        monkeypatch.setattr(settings, "git_sha", "")
        await record_running_build(session)
        assert await session.scalar(select(func.count(PlatformBuild.id)).where(PlatformBuild.version == "2.0.0")) == 2


async def test_a_build_is_released_once_its_rollout_completes_never_at_first_boot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#2214: the release event waits for the build to settle as the newest one; a build superseded before it
    settled never raises it, and the next release names the last released build as the one before it."""
    from datetime import UTC, datetime, timedelta

    from app.core.config import settings
    from app.services.platform_builds import DRAIN_GRACE, _upsert_running_build, release_event_id

    async with _platform() as (sessions, _people, _groups), sessions() as session:
        start = datetime(2026, 11, 25, 8, 0, tzinfo=UTC)
        tick = timedelta(seconds=45)

        async def boot(sha: str, at: datetime) -> dict[str, object] | None:
            monkeypatch.setattr(settings, "app_version", "3.0.0")
            monkeypatch.setattr(settings, "git_sha", sha)
            return await _upsert_running_build(session, now=at)

        # A: first boot, nothing yet; once settled past the grace, released once.
        assert await boot("aaaaaaa1", start) is None
        released_a = await boot("aaaaaaa1", start + DRAIN_GRACE + tick)
        assert released_a is not None and released_a["git_sha"] == "aaaaaaa1"
        assert await boot("aaaaaaa1", start + DRAIN_GRACE + 2 * tick) is None  # another pod, same build

        # B boots, C boots three minutes later: B never settles, no event for it.
        t_b = start + timedelta(hours=1)
        assert await boot("bbbbbbb2", t_b) is None
        t_c = t_b + timedelta(minutes=3)
        assert await boot("bbbbbbb2", t_c - tick) is None
        assert await boot("ccccccc3", t_c) is None
        # A pod of B still serves a minute after C booted: B is not the newest, nothing for it.
        b_last = t_c + timedelta(minutes=1)
        assert await boot("bbbbbbb2", b_last) is None
        # While B's pods are still seen within the grace, C is not released.
        assert await boot("ccccccc3", t_c + DRAIN_GRACE) is None
        # B's pods gone past the grace: C is released, the build before it is A (the last released).
        settled = b_last + DRAIN_GRACE
        released_c = await boot("ccccccc3", settled)
        assert released_c is not None and released_c["git_sha"] == "ccccccc3"
        assert released_c["previous_git_sha"] == "aaaaaaa1"
        # B, booting again behind C (a stale pod), is never released: it is not the newest.
        assert await boot("bbbbbbb2", settled + 2 * DRAIN_GRACE) is None
        assert release_event_id("3.0.0", "ccccccc3") != release_event_id("3.0.0", "bbbbbbb2")


async def test_an_audit_log_read_is_audited_once_per_window(monkeypatch):
    class _FakeRedis:
        def __init__(self) -> None:
            self.keys: dict[str, int] = {}

        async def exists(self, key):
            return key in self.keys

        async def set(self, key, value, ex=None):
            self.keys[key] = ex

    fake = _FakeRedis()

    async def _redis_client():
        return fake

    monkeypatch.setattr("app.core.redis.redis_client", _redis_client)
    async with _platform() as (sessions, people, _groups):
        admin = people["admin"]
        first = await _call(admin, "query_audit_log", action=["delete"])
        assert [entry["actor_email"] for entry in first["entries"]] == ["bob@example.test"]
        await _call(admin, "query_audit_log", action=["delete"])
        await _call(admin, "query_audit_log", action=["create"])
        async with sessions() as session:
            reads = await session.scalar(
                select(func.count(AuditLog.id)).where(AuditLog.action == "inspect_audit_log")
            )
        assert reads == 2
        too_many = await _call(admin, "query_audit_log", limit=1000)
        assert len(too_many["entries"]) <= 200
