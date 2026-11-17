from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import pytest

from app.services.tracing.read_store import ClickHouseTraceReadStore


class _FakeResult:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def all(self) -> list[Any]:
        return self._rows

    def scalar(self) -> Any:
        # Coverage probes (min rollup date / min trace timestamp) read a scalar.
        return self._rows[0] if self._rows else None

    def scalars(self) -> _FakeResult:
        # The hidden-activity lookup (#11624) reads agent ids as scalars.
        return self


class _FakeDb:
    def __init__(self, execute_results: list[_FakeResult] | None = None) -> None:
        self.execute_results = list(execute_results or [])
        self.execute_statements: list[Any] = []

    async def execute(self, statement):
        self.execute_statements.append(statement)
        if not self.execute_results:
            return _FakeResult([])
        return self.execute_results.pop(0)


class _FakeClickHouseClient:
    def __init__(self, query_results: list[list[dict[str, Any]]]) -> None:
        self.query_results = list(query_results)
        self.queries: list[str] = []

    async def query_rows(self, sql: str) -> list[dict[str, Any]]:
        self.queries.append(sql)
        if not self.query_results:
            return []
        return self.query_results.pop(0)


class _FailingPostgresStore:
    async def analytics_summary(self, *_args, **_kwargs):
        raise AssertionError("clickhouse analytics should not delegate to postgres")

    async def analytics_cost_by_model(self, *_args, **_kwargs):
        raise AssertionError("clickhouse analytics should not delegate to postgres")

    async def analytics_cost_over_time(self, *_args, **_kwargs):
        raise AssertionError("clickhouse analytics should not delegate to postgres")

    async def analytics_volume_over_time(self, *_args, **_kwargs):
        raise AssertionError("clickhouse analytics should not delegate to postgres")

    async def analytics_latency(self, *_args, **_kwargs):
        raise AssertionError("clickhouse analytics should not delegate to postgres")

    async def analytics_cost_by_agent(self, *_args, **_kwargs):
        raise AssertionError("clickhouse analytics should not delegate to postgres")

    async def analytics_usage_by_user(self, *_args, **_kwargs):
        raise AssertionError("clickhouse analytics should not delegate to postgres")

    async def analytics_errors(self, *_args, **_kwargs):
        raise AssertionError("clickhouse analytics should not delegate to postgres")


def _filters(**overrides):
    return SimpleNamespace(
        agent_ids=overrides.get("agent_ids"),
        models=overrides.get("models"),
        sources=overrides.get("sources"),
    )


def _user(*, is_admin: bool = True) -> SimpleNamespace:
    return SimpleNamespace(id=uuid.uuid4(), is_admin=is_admin)


@pytest.mark.asyncio
async def test_clickhouse_analytics_summary_uses_clickhouse_path():
    now = datetime.now(UTC)
    client = _FakeClickHouseClient(
        query_results=[
            [
                {
                    "trace_count": 5,
                    "total_cost": 1.5,
                    "total_tokens": 120,
                    "avg_duration_ms": 25.0,
                    "error_count": 1,
                    "avg_feedback_score": 0.8,
                }
            ],
            [
                {
                    "trace_count": 4,
                    "total_cost": 1.0,
                    "total_tokens": 100,
                    "avg_duration_ms": 20.0,
                    "error_count": 0,
                    "avg_feedback_score": 0.9,
                }
            ],
            [{"unknown_cost_trace_count": 3}],
        ]
    )
    db = _FakeDb(
        execute_results=[_FakeResult([SimpleNamespace(last_aggregation_at=now - timedelta(hours=72))])]
    )
    store = ClickHouseTraceReadStore(
        _postgres=_FailingPostgresStore(),
        _client_factory=lambda: client,
    )

    payload = await store.analytics_summary(
        db,
        filters=_filters(),
        user=_user(),
        user_group_ids=[],
        start=now - timedelta(days=7),
        end=now,
        prev_start=now - timedelta(days=14),
        prev_end=now - timedelta(days=7),
        use_daily=False,
    )

    assert payload["trace_count"] == 5
    assert payload["previous"]["trace_count"] == 4
    assert payload["unknown_cost_trace_count"] == 3
    assert payload["aggregation_stale"] is True
    assert "from llm_traces" in client.queries[0].lower()


@pytest.mark.asyncio
async def test_clickhouse_summary_compares_the_window_start_with_coverage_on_its_utc_day():
    # 00:00 at UTC+14 on the first covered day is 10:00 UTC the day before: the
    # window starts before any data, whatever the caller's calendar date says.
    client = _FakeClickHouseClient(
        query_results=[
            [{"trace_count": 1}],
            [{"trace_count": 0}],
            [{"unknown_cost_trace_count": 0}],
            [{"earliest": None}],
        ]
    )
    db = _FakeDb(
        execute_results=[
            _FakeResult([SimpleNamespace(last_aggregation_at=None)]),
            _FakeResult([None]),
            _FakeResult([datetime(2026, 6, 10, 12, tzinfo=UTC)]),
        ]
    )
    store = ClickHouseTraceReadStore(_postgres=_FailingPostgresStore(), _client_factory=lambda: client)
    start = datetime(2026, 6, 10, tzinfo=timezone(timedelta(hours=14)))

    payload = await store.analytics_summary(
        db,
        filters=_filters(),
        user=_user(),
        user_group_ids=[],
        start=start,
        end=start + timedelta(days=5),
        prev_start=start - timedelta(days=5),
        prev_end=start,
        use_daily=False,
    )

    assert payload["stats_coverage_start"] == date(2026, 6, 10)
    assert payload["window_starts_before_coverage"] is True


@pytest.mark.asyncio
async def test_clickhouse_cost_queries_apply_non_admin_visibility():
    visible_agent_id = uuid.uuid4()
    client = _FakeClickHouseClient(
        query_results=[
            [
                {
                    "model": "gpt-5.3-codex",
                    "trace_count": 8,
                    "input_tokens": 100,
                    "output_tokens": 80,
                    "input_cost": 1.1,
                    "output_cost": 0.8,
                    "total_cost": 1.9,
                }
            ],
            [{"date": "2026-06-20", "model": "gpt-5.3-codex", "cost": 1.9}],
            [{"date": "2026-06-20", "source": "assignment", "count": 8}],
        ]
    )
    # Each query reads, in order: the visible agents, the deleted agents (none)
    # and the visible agents whose activity is hidden from non-admins (#11624,
    # none).
    db = _FakeDb(
        execute_results=[
            _FakeResult([SimpleNamespace(id=visible_agent_id)]),
            _FakeResult([]),
            _FakeResult([]),
        ]
        * 3
    )
    user = _user(is_admin=False)
    store = ClickHouseTraceReadStore(
        _postgres=_FailingPostgresStore(),
        _client_factory=lambda: client,
    )
    now = datetime.now(UTC)

    by_model = await store.analytics_cost_by_model(
        db,
        filters=_filters(models="gpt-5.3-codex", sources="assignment"),
        user=user,
        user_group_ids=[],
        start=now - timedelta(days=7),
        end=now,
        use_daily=False,
    )
    over_time = await store.analytics_cost_over_time(
        db,
        filters=_filters(),
        user=user,
        user_group_ids=[],
        start=now - timedelta(days=7),
        end=now,
        use_daily=False,
    )
    volume = await store.analytics_volume_over_time(
        db,
        filters=_filters(),
        user=user,
        user_group_ids=[],
        start=now - timedelta(days=7),
        end=now,
        use_daily=False,
    )

    assert by_model[0]["model"] == "gpt-5.3-codex"
    assert over_time[0]["model"] == "gpt-5.3-codex"
    assert volume[0]["source"] == "assignment"
    assert "source = 'general-chat'" in client.queries[0]
    assert f"'{visible_agent_id}'" in client.queries[0]


@pytest.mark.asyncio
async def test_clickhouse_latency_returns_expected_shape():
    client = _FakeClickHouseClient(
        query_results=[
            [{"p50": 1.0, "p75": 2.0, "p90": 3.0, "p95": 4.0, "p99": 5.0}],
            [{"model": "gpt-5.3-codex", "p50": 1.0, "p75": 2.0, "p90": 3.0, "p95": 4.0, "p99": 5.0, "trace_count": 7}],
            [{"date": "2026-06-20", "p50": 1.5}],
        ]
    )
    store = ClickHouseTraceReadStore(
        _postgres=_FailingPostgresStore(),
        _client_factory=lambda: client,
    )
    now = datetime.now(UTC)

    payload = await store.analytics_latency(
        _FakeDb(),
        filters=_filters(),
        user=_user(),
        user_group_ids=[],
        start=now - timedelta(days=7),
        end=now,
        use_daily=False,
    )

    assert payload["overall"]["p95"] == 4.0
    assert payload["by_model"][0]["trace_count"] == 7
    assert payload["daily_median"][0]["p50"] == 1.5


@pytest.mark.asyncio
async def test_clickhouse_cost_by_agent_resolves_agent_metadata():
    agent_id = uuid.uuid4()
    client = _FakeClickHouseClient(
        query_results=[
            [
                {
                    "agent_id": str(agent_id),
                    "trace_count": 5,
                    "total_cost": 2.0,
                    "avg_cost_per_trace": 0.4,
                    "avg_duration_ms": 30.0,
                    "avg_feedback_score": 0.9,
                }
            ]
        ]
    )
    db = _FakeDb(
        execute_results=[_FakeResult([SimpleNamespace(id=agent_id, name="Support Bot", icon="headset")])]
    )
    store = ClickHouseTraceReadStore(
        _postgres=_FailingPostgresStore(),
        _client_factory=lambda: client,
    )
    now = datetime.now(UTC)

    payload = await store.analytics_cost_by_agent(
        db,
        filters=_filters(),
        user=_user(),
        user_group_ids=[],
        start=now - timedelta(days=7),
        end=now,
        use_daily=False,
    )

    assert payload[0]["agent_name"] == "Support Bot"
    assert payload[0]["agent_icon"] == "headset"
    assert payload[0]["avg_cost_per_trace"] == 0.4


@pytest.mark.asyncio
async def test_clickhouse_usage_by_user_keeps_traces_without_a_user():
    user_id = uuid.uuid4()
    client = _FakeClickHouseClient(
        query_results=[
            [
                {"user_key": str(user_id), "trace_count": 4, "total_tokens": 900, "total_cost": 1.2, "agent_count": 2},
                {"user_key": None, "trace_count": 3, "total_tokens": 300, "total_cost": 0.3, "agent_count": 1},
            ]
        ]
    )
    store = ClickHouseTraceReadStore(_postgres=_FailingPostgresStore(), _client_factory=lambda: client)
    now = datetime.now(UTC)

    payload = await store.analytics_usage_by_user(
        _FakeDb(),
        filters=_filters(),
        user=_user(),
        user_group_ids=[],
        start=now - timedelta(days=7),
        end=now,
        use_daily=False,
    )

    assert payload == [
        {"user_id": str(user_id), "trace_count": 4, "total_tokens": 900, "total_cost": 1.2, "agent_count": 2},
        {"user_id": None, "trace_count": 3, "total_tokens": 300, "total_cost": 0.3, "agent_count": 1},
    ]
    # Grouped under an alias: ClickHouse substitutes a SELECT alias into WHERE,
    # and the visibility clause reads the ``user_id`` column.
    assert "GROUP BY user_key" in client.queries[0]
    assert " AS user_id" not in client.queries[0]


@pytest.mark.asyncio
async def test_clickhouse_errors_resolves_agent_name_and_iso_timestamp():
    agent_id = uuid.uuid4()
    now = datetime.now(UTC)
    client = _FakeClickHouseClient(
        query_results=[
            [
                {
                    "error_message": "Rate limit exceeded",
                    "count": 2,
                    "last_seen": now,
                    "agent_id": str(agent_id),
                    "model": "gpt-5.3-codex",
                }
            ]
        ]
    )
    db = _FakeDb(execute_results=[_FakeResult([SimpleNamespace(id=agent_id, name="Support Bot", icon="headset")])])
    store = ClickHouseTraceReadStore(
        _postgres=_FailingPostgresStore(),
        _client_factory=lambda: client,
    )

    payload = await store.analytics_errors(
        db,
        filters=_filters(),
        user=_user(),
        user_group_ids=[],
        start=now - timedelta(days=7),
        end=now,
    )

    assert payload[0]["error_message"] == "Rate limit exceeded"
    assert payload[0]["agent_name"] == "Support Bot"
    assert payload[0]["last_seen"] == now.isoformat()


class _RecordingPostgresStore:
    """Records which analytics calls were delegated to the PostgreSQL store."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def _record(self, name: str):
        async def _call(_db, **kwargs):
            self.calls.append(name)
            return {"delegated": name, "use_daily": kwargs.get("use_daily")}

        return _call

    def __getattr__(self, name: str):
        if name.startswith("analytics_"):
            return self._record(name)
        raise AttributeError(name)


@pytest.mark.asyncio
async def test_clickhouse_serves_long_windows_from_the_postgres_rollup():
    # ClickHouse mode used to ignore use_daily and always query raw llm_traces in
    # ClickHouse, so 30d/90d dashboards went blank once those traces aged out -
    # even though the PostgreSQL rollup that outlives them had the answer.
    now = datetime.now(UTC)
    client = _FakeClickHouseClient(query_results=[])
    postgres = _RecordingPostgresStore()
    store = ClickHouseTraceReadStore(_postgres=postgres, _client_factory=lambda: client)
    window = {
        "filters": _filters(),
        "user": _user(),
        "user_group_ids": [],
        "start": now - timedelta(days=90),
        "end": now,
        "use_daily": True,
    }

    await store.analytics_summary(_FakeDb(), prev_start=now - timedelta(days=180), prev_end=now - timedelta(days=90), **window)
    await store.analytics_cost_by_model(_FakeDb(), **window)
    await store.analytics_cost_over_time(_FakeDb(), **window)
    await store.analytics_volume_over_time(_FakeDb(), **window)
    await store.analytics_latency(_FakeDb(), **window)
    await store.analytics_cost_by_agent(_FakeDb(), **window)
    await store.analytics_usage_by_user(_FakeDb(), **window)

    assert postgres.calls == [
        "analytics_summary",
        "analytics_cost_by_model",
        "analytics_cost_over_time",
        "analytics_volume_over_time",
        "analytics_latency",
        "analytics_cost_by_agent",
        "analytics_usage_by_user",
    ]
    # Nothing was asked of ClickHouse: the rollup is the source for these windows.
    assert client.queries == []
