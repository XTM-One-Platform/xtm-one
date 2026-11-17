"""Model-level checks for Story 1.2a observability persistence tables."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import text

import app.models  # noqa: F401 - side-effect import to register model metadata
from app.core.database import Base
from app.models.llm_model_price import LLMModelPrice
from app.models.llm_trace import LLMDailyStats, LLMScore, LLMSpan, LLMTrace
from tests.scratch_database import scratch_database


def test_llm_observability_models_registered_in_metadata():
    table_names = set(Base.metadata.tables.keys())

    assert "llm_traces" in table_names
    assert "llm_spans" in table_names
    assert "llm_scores" in table_names
    assert "llm_model_prices" in table_names
    assert "llm_daily_stats" in table_names


@pytest.mark.integration
@pytest.mark.asyncio
async def test_llm_observability_models_roundtrip_insert():
    """Create isolated DB, create only observability tables, insert and read back."""
    async with scratch_database(
        "copilot_obs",
        tables=[
            LLMTrace.__table__,
            LLMSpan.__table__,
            LLMScore.__table__,
            LLMModelPrice.__table__,
            LLMDailyStats.__table__,
        ],
    ) as db:
        engine = db.engine

        trace_id = uuid.uuid4()
        span_id = uuid.uuid4()
        now = datetime.now(UTC)
        day = date.today()

        async with engine.begin() as conn:
            await conn.execute(
                text(
                    """
                    INSERT INTO llm_traces (
                        id, source, name, tags, started_at, status, level
                    ) VALUES (
                        :id, :source, :name, :tags, :started_at, :status, :level
                    )
                    """
                ),
                {
                    "id": str(trace_id),
                    "source": "agent-chat",
                    "name": "trace-roundtrip",
                    "tags": ["source:agent-chat"],
                    "started_at": now,
                    "status": "ok",
                    "level": "DEFAULT",
                },
            )
            await conn.execute(
                text(
                    """
                    INSERT INTO llm_spans (
                        id, trace_id, type, name, started_at, position, level
                    ) VALUES (
                        :id, :trace_id, :type, :name, :started_at, :position, :level
                    )
                    """
                ),
                {
                    "id": str(span_id),
                    "trace_id": str(trace_id),
                    "type": "GENERATION",
                    "name": "gen",
                    "started_at": now,
                    "position": 0,
                    "level": "DEFAULT",
                },
            )
            await conn.execute(
                text(
                    """
                    INSERT INTO llm_scores (
                        id, trace_id, span_id, name, source, data_type, value, created_at
                    ) VALUES (
                        :id, :trace_id, :span_id, :name, :source, :data_type, :value, :created_at
                    )
                    """
                ),
                {
                    "id": str(uuid.uuid4()),
                    "trace_id": str(trace_id),
                    "span_id": str(span_id),
                    "name": "feedback",
                    "source": "USER",
                    "data_type": "BOOLEAN",
                    "value": 1.0,
                    "created_at": now,
                },
            )
            await conn.execute(
                text(
                    """
                    INSERT INTO llm_model_prices (
                        id, provider, model_id, model_pattern, input_price_per_1m, output_price_per_1m,
                        cache_read_price_per_1m, cache_write_price_per_1m, effective_from, is_custom, created_at
                    ) VALUES (
                        :id, :provider, :model_id, :model_pattern, :input_price_per_1m, :output_price_per_1m,
                        :cache_read_price_per_1m, :cache_write_price_per_1m, :effective_from, :is_custom, :created_at
                    )
                    """
                ),
                {
                    "id": str(uuid.uuid4()),
                    "provider": "anthropic",
                    "model_id": "claude-sonnet-4-20250514",
                    "model_pattern": "claude-sonnet-4-*",
                    "input_price_per_1m": 3.0,
                    "output_price_per_1m": 15.0,
                    "cache_read_price_per_1m": 0.3,
                    "cache_write_price_per_1m": 3.75,
                    "effective_from": day,
                    "is_custom": False,
                    "created_at": now,
                },
            )
            await conn.execute(
                text(
                    """
                    INSERT INTO llm_daily_stats (
                        id, date, agent_id, model, source, trace_count, error_count,
                        total_input_tokens, total_output_tokens, total_cost, avg_duration_ms,
                        p50_duration_ms, p95_duration_ms, avg_feedback_score, created_at
                    ) VALUES (
                        :id, :date, :agent_id, :model, :source, :trace_count, :error_count,
                        :total_input_tokens, :total_output_tokens, :total_cost, :avg_duration_ms,
                        :p50_duration_ms, :p95_duration_ms, :avg_feedback_score, :created_at
                    )
                    """
                ),
                {
                    "id": str(uuid.uuid4()),
                    "date": day,
                    "agent_id": None,
                    "model": "claude-sonnet-4-20250514",
                    "source": "agent-chat",
                    "trace_count": 1,
                    "error_count": 0,
                    "total_input_tokens": 10,
                    "total_output_tokens": 20,
                    "total_cost": 0.01,
                    "avg_duration_ms": 100.0,
                    "p50_duration_ms": 100.0,
                    "p95_duration_ms": 100.0,
                    "avg_feedback_score": 1.0,
                    "created_at": now,
                },
            )

        async with engine.connect() as conn:
            trace_row = await conn.execute(
                text("SELECT source, status FROM llm_traces WHERE id = :id"),
                {"id": str(trace_id)},
            )
            assert trace_row.one() == ("agent-chat", "ok")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_analytics_combined_daily_stats_executes_on_postgres():
    """Regression for the LLM Analytics dashboard (30d/90d ranges).

    The combined "prior-day rollups UNION ALL today's live aggregation" query
    must actually EXECUTE on PostgreSQL. The today-live arm groups by the raw
    ``model`` column; an earlier revision grouped by ``func.coalesce(model, "")``
    which SQLAlchemy renders as a SECOND, distinct bind parameter for the ``""``
    default, so Postgres could not match it to the SELECT's coalesce and rejected
    the whole statement with ``column "anon_3.model" must appear in the GROUP BY
    clause`` - 500-ing every analytics endpoint for any range older than 7 days.
    The other analytics tests only render SQL against a fake DB (where literal
    binds make the two coalesces look identical), so this real-Postgres execution
    is what guards the regression.
    """
    from types import SimpleNamespace

    from sqlalchemy import func, select
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.api.v1 import traces as traces_api
    from app.models.agent import Agent
    from app.models.agent_tombstone import AgentTombstone
    from app.models.user import User
    from app.services.tracing.read_store import combined_daily_stats_subquery

    # Minimal self-contained schema: agents -> users (FK); llm_traces /
    # llm_daily_stats carry agent/user ids as plain UUIDs (no FK by design),
    # and the combined query left-joins agents, their tombstones and
    # llm_scores. llm_scores FKs to llm_spans, so that table is created too
    # even though the query never reads it.
    async with scratch_database(
        "copilot_analytics",
        tables=[
            User.__table__,
            Agent.__table__,
            AgentTombstone.__table__,
            LLMTrace.__table__,
            LLMSpan.__table__,
            LLMScore.__table__,
            LLMDailyStats.__table__,
        ],
    ) as db:
        engine = db.engine

        now = datetime.now(UTC)
        yesterday = (now - timedelta(days=1)).date()
        async with engine.begin() as conn:
            # Prior-day rollup (date < today) -> the daily-stats arm of the union.
            await conn.execute(
                text(
                    """
                    INSERT INTO llm_daily_stats (
                        id, date, agent_id, model, source, trace_count, error_count,
                        total_input_tokens, total_output_tokens, total_cost,
                        avg_duration_ms, p50_duration_ms, p95_duration_ms,
                        avg_feedback_score, created_at
                    ) VALUES (
                        :id, :date, NULL, :model, :source, 1, 0,
                        10, 20, 0.01, 100.0, 100.0, 100.0, 1.0, :created_at
                    )
                    """
                ),
                {
                    "id": str(uuid.uuid4()),
                    "date": yesterday,
                    "model": "claude-opus-4-8",
                    "source": "agent-chat",
                    "created_at": now,
                },
            )
            # Today's traces -> the live arm. One carries a model, one is NULL to
            # exercise the coalesce(model, "") path that previously broke grouping.
            for model_value in ("claude-opus-4-8", None):
                await conn.execute(
                    text(
                        """
                        INSERT INTO llm_traces (
                            id, source, name, tags, started_at, status, level,
                            model, input_tokens, output_tokens, total_cost, duration_ms
                        ) VALUES (
                            :id, :source, :name, :tags, :started_at, :status, :level,
                            :model, 5, 7, 0.002, 50.0
                        )
                        """
                    ),
                    {
                        "id": str(uuid.uuid4()),
                        "source": "agent-chat",
                        "name": "analytics-regression",
                        "tags": ["source:agent-chat"],
                        "started_at": now,
                        "status": "ok",
                        "level": "DEFAULT",
                        "model": model_value,
                    },
                )

        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        admin_user = SimpleNamespace(id=uuid.uuid4(), is_admin=True)
        filters = traces_api.AnalyticsFilters(period="30d")
        async with session_factory() as session:
            base = combined_daily_stats_subquery(
                filters,
                user=admin_user,
                user_group_ids=[],
                start=now - timedelta(days=30),
                # End a minute ahead of the traces' started_at (== now): the
                # today-live arm filters started_at < window_end, so an exact
                # `now` end would drop today's traces and the union would be
                # daily-only - not exercising the previously-broken arm.
                end=now + timedelta(minutes=1),
            )
            # Aggregate over the combined base by model, exactly as the real
            # cost-by-model / volume endpoints do. Pre-fix this raised
            # asyncpg.exceptions.GroupingError on the today-live arm.
            rows = (
                await session.execute(
                    select(
                        base.c.model,
                        func.coalesce(func.sum(base.c.trace_count), 0),
                    ).group_by(base.c.model)
                )
            ).all()

        models_to_traces = {row[0]: int(row[1]) for row in rows}
        # The NULL-model today trace collapses to "" (consistent with the stored
        # daily-stats convention) and survives the grouping.
        assert "" in models_to_traces
        # claude-opus-4-8 spans the prior-day rollup (1) + today's live trace (1).
        assert models_to_traces.get("claude-opus-4-8") == 2


@pytest.mark.integration
@pytest.mark.asyncio
async def test_daily_stats_nulls_not_distinct_migration_dedupes_and_enforces():
    """Regression for migration dlystatsnnd01.

    Recreates the OLD ``llm_daily_stats`` shape (plain UNIQUE, NULLs distinct),
    seeds duplicate NULL-agent rows the old constraint allowed, runs the
    migration's upgrade SQL, and asserts it (a) dedupes to one row and (b) the
    swapped ``UNIQUE ... NULLS NOT DISTINCT`` constraint now rejects a fresh
    NULL-agent duplicate. Guards the PG15+ syntax + dedupe against the real DB.
    """
    from sqlalchemy.exc import IntegrityError

    constraint = "uq_llm_daily_stats_date_agent_model_source"
    # Empty: the test builds the OLD table shape itself, and no app code runs.
    async with scratch_database("copilot_nnd", tables=(), bind_app_sessions=False) as db:
        engine = db.engine
        # Old shape: plain UNIQUE treats the two NULL-agent rows as distinct.
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    f"""
                    CREATE TABLE llm_daily_stats (
                        id uuid PRIMARY KEY,
                        date date NOT NULL,
                        agent_id uuid,
                        model varchar(255) NOT NULL,
                        source varchar(50) NOT NULL,
                        created_at timestamptz NOT NULL DEFAULT now(),
                        CONSTRAINT {constraint} UNIQUE (date, agent_id, model, source)
                    )
                    """
                )
            )
            for _ in range(2):
                await conn.execute(
                    text(
                        "INSERT INTO llm_daily_stats (id, date, agent_id, model, source) "
                        "VALUES (:id, '2026-06-29', NULL, 'm', 'general-chat')"
                    ),
                    {"id": str(uuid.uuid4())},
                )

        # The migration's upgrade SQL (kept in sync with dlystatsnnd01).
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    """
                    DELETE FROM llm_daily_stats a
                    USING llm_daily_stats b
                    WHERE a.ctid < b.ctid
                      AND a.date = b.date
                      AND a.model = b.model
                      AND a.source = b.source
                      AND a.agent_id IS NOT DISTINCT FROM b.agent_id
                    """
                )
            )
            await conn.execute(text(f"ALTER TABLE llm_daily_stats DROP CONSTRAINT IF EXISTS {constraint}"))
            await conn.execute(
                text(
                    f"ALTER TABLE llm_daily_stats ADD CONSTRAINT {constraint} "
                    "UNIQUE NULLS NOT DISTINCT (date, agent_id, model, source)"
                )
            )

        async with engine.connect() as conn:
            remaining = await conn.scalar(text("SELECT count(*) FROM llm_daily_stats"))
        assert remaining == 1  # the duplicate NULL-agent row was collapsed

        # A fresh NULL-agent duplicate is now rejected by NULLS NOT DISTINCT.
        with pytest.raises(IntegrityError):
            async with engine.begin() as conn:
                await conn.execute(
                    text(
                        "INSERT INTO llm_daily_stats (id, date, agent_id, model, source) "
                        "VALUES (:id, '2026-06-29', NULL, 'm', 'general-chat')"
                    ),
                    {"id": str(uuid.uuid4())},
                )
