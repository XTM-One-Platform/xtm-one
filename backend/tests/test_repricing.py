from __future__ import annotations

import contextlib
import re
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.services.tracing import repricing
from app.services.tracing.cost_calculator import ModelPrice

_HAIKU_PRICE = ModelPrice(
    provider="anthropic",
    model_id="claude-haiku-4-5-20251001",
    model_pattern="^claude-haiku-4-5(-.*)?$",
    input_price_per_1m=Decimal("1.0"),
    output_price_per_1m=Decimal("5.0"),
    cache_read_price_per_1m=Decimal("0.1"),
    cache_write_price_per_1m=Decimal("1.25"),
    effective_from=date(2025, 10, 1),
    is_custom=False,
)


class _FakeScalarResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return _FakeScalarResult(self._rows)

    def all(self):
        return self._rows


class _FakeSession:
    """Serves queued result batches to the SELECTs; records the rollup lock,
    the rollup writes, commits and rollbacks, in order."""

    def __init__(self, batches):
        self._batches = list(batches)
        self.committed = 0
        self.rolled_back = 0
        self.log: list[str] = []
        self.statements: list[str] = []
        self.marked: list[list[date]] = []
        self.no_autoflush = contextlib.nullcontext()

    async def execute(self, stmt, params=None):
        sql = str(stmt)
        if "pg_advisory_xact_lock" in sql:
            self.log.append("lock")
            return None
        if sql.lstrip().upper().startswith("DELETE FROM LLM_DAILY_STATS"):
            self.log.append("delete_rollups")
            return None
        if "INSERT INTO llm_daily_stats_stale_days" in sql:
            self.log.append("mark_stale")
            self.marked.append(list(params["days"]))
            return None
        self.statements.append(sql)
        return _FakeResult(self._batches.pop(0))

    async def commit(self):
        self.committed += 1
        self.log.append("commit")

    async def rollback(self):
        self.rolled_back += 1
        self.log.append("rollback")


@pytest.fixture(autouse=True)
def retention(monkeypatch):
    """The platform settings the reprice reads its retention window from."""
    settings = SimpleNamespace(trace_retention_days=90)

    async def _peek(_db):
        return settings

    monkeypatch.setattr("app.api.v1.platform_settings_helpers.peek_platform_settings", _peek)
    return settings


def _window(today: date, *, retention_days: int = 90) -> repricing._RollupWindow:
    return repricing._RollupWindow(
        expired_before=today - timedelta(days=retention_days),
        today=today,
    )


def _trace(
    *,
    model: str | None = "claude-haiku-4-5-20251001",
    input_tokens: int | None = 1000,
    output_tokens: int | None = 1000,
    started_at: datetime | None = None,
    metadata_json: dict | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid4(),
        model=model,
        metadata_json=metadata_json,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        started_at=started_at or datetime.now(UTC),
        input_cost=None,
        output_cost=None,
        total_cost=None,
    )


def _span(
    *,
    model: str | None = None,
    usage_details: dict | None = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid4(),
        model=model,
        usage_details=usage_details,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        started_at=datetime.now(UTC),
        input_cost=None,
        output_cost=None,
        total_cost=None,
    )


def _patch_prices(monkeypatch, price: ModelPrice | None):
    async def _any(_db, model_id):
        del model_id
        return price

    async def _scoped(_db, model_id, provider):
        del model_id, provider
        return price

    monkeypatch.setattr(repricing, "resolve_model_price_any", _any)
    monkeypatch.setattr(repricing, "resolve_model_price", _scoped)


def _patch_stats(monkeypatch):
    calls: list[set[date]] = []

    async def _refresh(affected_dates, _window):
        calls.append(set(affected_dates))
        return len(affected_dates)

    monkeypatch.setattr(repricing, "_refresh_daily_stats", _refresh)
    return calls


@pytest.mark.asyncio
async def test_reprices_null_cost_trace_at_current_tariff(monkeypatch):
    _patch_prices(monkeypatch, _HAIKU_PRICE)
    stats_calls = _patch_stats(monkeypatch)

    trace = _trace()
    db = _FakeSession([[trace], [], []])

    result = await repricing.reprice_null_costs(db)

    assert result.traces_updated == 1
    assert trace.input_cost == Decimal("0.001")
    assert trace.output_cost == Decimal("0.005")
    assert trace.total_cost == Decimal("0.006")
    # One commit per non-empty batch, then the one closing the empty read.
    assert db.committed == 2
    assert stats_calls and trace.started_at.date() in stats_calls[0]


@pytest.mark.asyncio
async def test_skips_trace_without_price(monkeypatch):
    _patch_prices(monkeypatch, None)
    _patch_stats(monkeypatch)

    trace = _trace(model="unknown-model")
    db = _FakeSession([[trace], [], []])

    result = await repricing.reprice_null_costs(db)

    assert result.traces_updated == 0
    assert result.traces_skipped_no_price == 1
    assert trace.total_cost is None


@pytest.mark.asyncio
async def test_skips_trace_without_usage(monkeypatch):
    _patch_prices(monkeypatch, _HAIKU_PRICE)
    _patch_stats(monkeypatch)

    trace = _trace(input_tokens=None, output_tokens=0)
    db = _FakeSession([[trace], [], []])

    result = await repricing.reprice_null_costs(db)

    assert result.traces_updated == 0
    assert result.traces_skipped_no_usage == 1
    assert trace.total_cost is None


@pytest.mark.asyncio
async def test_enforce_effective_from_skips_older_traces(monkeypatch):
    _patch_prices(monkeypatch, _HAIKU_PRICE)
    _patch_stats(monkeypatch)

    older = _trace(started_at=datetime(2025, 9, 1, tzinfo=UTC))
    newer = _trace(started_at=datetime(2025, 11, 1, tzinfo=UTC))
    db = _FakeSession([[older, newer], [], []])

    result = await repricing.reprice_null_costs(db, enforce_effective_from=True)

    assert result.traces_updated == 1
    assert result.traces_skipped_effective_from == 1
    assert older.total_cost is None
    assert newer.total_cost == Decimal("0.006")


@pytest.mark.asyncio
async def test_dry_run_counts_without_writing(monkeypatch):
    _patch_prices(monkeypatch, _HAIKU_PRICE)
    stats_calls = _patch_stats(monkeypatch)

    trace = _trace()
    db = _FakeSession([[trace], [], []])

    result = await repricing.reprice_null_costs(db, dry_run=True)

    assert result.traces_updated == 1
    assert trace.total_cost is None
    assert db.committed == 0
    assert db.rolled_back == 1
    assert not stats_calls


@pytest.mark.asyncio
async def test_span_falls_back_to_trace_model_and_prefers_usage_details(monkeypatch):
    _patch_prices(monkeypatch, _HAIKU_PRICE)
    _patch_stats(monkeypatch)

    # Span has no model of its own; usage_details must win over token columns.
    span = _span(
        usage_details={"input_tokens": 2000, "output_tokens": 0},
        input_tokens=999_999,
        output_tokens=999_999,
    )
    span_row = (span, "claude-haiku-4-5-20251001", {"provider": "anthropic"})
    db = _FakeSession([[], [span_row], []])

    result = await repricing.reprice_null_costs(db)

    assert result.spans_updated == 1
    assert span.input_cost == Decimal("0.002")
    assert span.total_cost == Decimal("0.002")


@pytest.mark.asyncio
async def test_span_without_any_model_is_skipped(monkeypatch):
    _patch_prices(monkeypatch, _HAIKU_PRICE)
    _patch_stats(monkeypatch)

    span = _span(input_tokens=10, output_tokens=10)
    span_row = (span, None, None)
    db = _FakeSession([[], [span_row], []])

    result = await repricing.reprice_null_costs(db)

    assert result.spans_updated == 0
    assert result.spans_skipped_no_price == 1
    assert span.total_cost is None


class _RebuiltDays(list):
    """The days handed to the rebuild, in order; ``calls`` holds its options."""

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[dict] = []


def _patch_rebuild(monkeypatch, returns=1) -> _RebuiltDays:
    """Stand in for ``trace_storage.rebuild_daily_stats``; record its calls."""
    import app.services.trace_storage as trace_storage

    recorded = _RebuiltDays()

    async def _fake_rebuild(_db, target, *, now=None, expired_before=None, build_missing=False):
        recorded.append(target)
        recorded.calls.append({"expired_before": expired_before, "build_missing": build_missing})
        return returns(target) if callable(returns) else returns

    monkeypatch.setattr(trace_storage, "rebuild_daily_stats", _fake_rebuild)
    return recorded


@pytest.mark.asyncio
async def test_stats_refresh_only_covers_past_days(monkeypatch):
    recorded = _patch_rebuild(monkeypatch)
    today = datetime.now(UTC).date()

    rebuilt = await repricing._refresh_daily_stats(
        {today, today - timedelta(days=1), today - timedelta(days=2)}, _window(today)
    )

    assert rebuilt == 2
    assert today not in recorded
    assert sorted(recorded) == [today - timedelta(days=2), today - timedelta(days=1)]


# -- Convergence ------------------------------------------------------


@pytest.mark.asyncio
async def test_each_batch_records_its_stale_rollups_with_its_costs(monkeypatch):
    """The rollups a batch makes stale are RECORDED in the same transaction as
    its cost updates, under the aggregation lock - before, the refresh ran
    after the commit from an in-memory day set that a crash or a skipped day
    lost for good. Nothing is deleted: a rollup-served read keeps the day's
    rows until the rebuild replaces them."""
    _patch_prices(monkeypatch, _HAIKU_PRICE)
    _patch_rebuild(monkeypatch)
    yesterday = datetime.now(UTC) - timedelta(days=1)
    db = _FakeSession([[_trace(started_at=yesterday)], [], []])

    await repricing.reprice_null_costs(db)

    assert db.log[:3] == ["lock", "mark_stale", "commit"]
    assert db.marked == [[yesterday.date()]]
    assert "delete_rollups" not in db.log


@pytest.mark.asyncio
async def test_a_batch_of_todays_traces_records_nothing(monkeypatch):
    """Today has no rollup yet: nothing to record, and no rollup lock to take."""
    _patch_prices(monkeypatch, _HAIKU_PRICE)
    recorded = _patch_rebuild(monkeypatch)
    db = _FakeSession([[_trace(started_at=datetime.now(UTC))], [], []])

    result = await repricing.reprice_null_costs(db)

    assert result.traces_updated == 1
    assert db.log == ["commit", "commit"]
    assert recorded == []


@pytest.mark.asyncio
async def test_a_crash_after_a_batch_keeps_that_batch_consistent(monkeypatch):
    """Batch 1 is committed with the record of its stale rollup; batch 2 fails
    before its commit. Nothing half-done is left: the next run finds batch 2
    still NULL, and the backfill finds batch 1's day recorded stale."""
    _patch_prices(monkeypatch, _HAIKU_PRICE)
    _patch_rebuild(monkeypatch)
    monkeypatch.setattr(repricing, "_BATCH_SIZE", 1)
    yesterday = datetime.now(UTC) - timedelta(days=1)
    first = _trace(started_at=yesterday)
    broken = _trace(started_at=yesterday)
    broken.metadata_json = {"provider": "anthropic"}

    calls = {"n": 0}
    real_resolve = repricing._PriceMemo.resolve

    async def _resolve(self, model, provider):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("database went away")
        return await real_resolve(self, model, provider)

    monkeypatch.setattr(repricing._PriceMemo, "resolve", _resolve)
    db = _FakeSession([[first], [broken], []])

    with pytest.raises(repricing.RepriceInterruptedError):
        await repricing.reprice_null_costs(db)

    assert first.total_cost == Decimal("0.006")
    assert db.log == ["lock", "mark_stale", "commit"]


@pytest.mark.asyncio
async def test_a_failed_rebuild_is_not_counted(monkeypatch):
    """``rebuild_daily_stats`` swallows its errors and returns 0: the day was
    not rebuilt, so it is not counted. Its stale rows and their record stay
    for the backfill."""
    _patch_rebuild(monkeypatch, returns=0)
    today = datetime.now(UTC).date()

    rebuilt = await repricing._refresh_daily_stats({today - timedelta(days=1)}, _window(today))

    assert rebuilt == 0


@pytest.mark.asyncio
async def test_the_inline_refresh_keeps_mains_bound_and_builds_missing_days(monkeypatch):
    """The newest ``_MAX_INLINE_REBUILD_DAYS`` days are refreshed inline - the
    92 the refresh always covered, so a day a reprice reached before still
    reads its new cost when the request returns. The older ones are left to
    the backfill with their stale rows, instead of keeping them forever. A
    repriced day with no rollup yet is built, as before."""
    recorded = _patch_rebuild(monkeypatch)
    today = datetime.now(UTC).date()
    days = {today - timedelta(days=n) for n in range(1, 101)}

    rebuilt = await repricing._refresh_daily_stats(days, _window(today))

    assert repricing._MAX_INLINE_REBUILD_DAYS == 92
    assert rebuilt == 92
    assert recorded == sorted(days, reverse=True)[:92]
    assert all(call["build_missing"] for call in recorded.calls)


# -- Expired days, late traces, partial failures, batch reads -------------------


@pytest.mark.asyncio
async def test_an_expired_day_is_recorded_and_rebuilt_under_the_purge_guard(monkeypatch, retention):
    """A day past the retention window is recorded stale like any other - the
    purge keeps its traces until it is rebuilt - and its rebuild is told where
    the window starts, so it never replaces the rollup with what a purge
    already running left of the day. The trace itself is priced."""
    _patch_prices(monkeypatch, _HAIKU_PRICE)
    recorded = _patch_rebuild(monkeypatch)
    retention.trace_retention_days = 7
    expired = _trace(started_at=datetime.now(UTC) - timedelta(days=30))
    db = _FakeSession([[expired], [], []])

    await repricing.reprice_null_costs(db)

    assert expired.total_cost == Decimal("0.006")
    assert db.marked == [[expired.started_at.date()]]
    assert "delete_rollups" not in db.log
    assert recorded == [expired.started_at.date()]
    assert recorded.calls[0]["expired_before"] == datetime.now(UTC).date() - timedelta(days=7)


class _HalfPastMidnight(datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 6, 16, 0, 30, tzinfo=UTC)


@pytest.mark.asyncio
async def test_yesterday_is_recorded_before_1am_and_its_rebuild_decides(monkeypatch):
    """Before 01:00 UTC yesterday's traces may still be landing. It is recorded
    like any past day and handed to the rebuild, which only rebuilds it when it
    already has a rollup - and then keeps it recorded, for the backfill to
    rebuild it once more when it is complete."""
    _patch_prices(monkeypatch, _HAIKU_PRICE)
    recorded = _patch_rebuild(monkeypatch)
    monkeypatch.setattr(repricing, "datetime", _HalfPastMidnight)
    late = _trace(started_at=datetime(2026, 6, 15, 23, 50, tzinfo=UTC))
    older = _trace(started_at=datetime(2026, 6, 14, 12, 0, tzinfo=UTC))
    db = _FakeSession([[late, older], [], []])

    result = await repricing.reprice_null_costs(db)

    assert db.marked == [[date(2026, 6, 14), date(2026, 6, 15)]]
    assert recorded == [date(2026, 6, 15), date(2026, 6, 14)]
    assert result.days_reaggregated == 2
    assert "delete_rollups" not in db.log


def _failing_price_for(monkeypatch, failing_provider: str):
    """Pricing a trace of *failing_provider* raises, like a database that went away."""
    real_resolve = repricing._PriceMemo.resolve

    async def _resolve(self, model, provider):
        if provider == failing_provider:
            raise RuntimeError("database went away")
        return await real_resolve(self, model, provider)

    monkeypatch.setattr(repricing._PriceMemo, "resolve", _resolve)


@pytest.mark.asyncio
async def test_a_failed_run_reports_only_the_committed_batches(monkeypatch):
    """Batches commit one by one, so a failure part-way leaves the earlier ones
    applied. The error carries what they applied - and only that - so the caller
    can audit it. The failed batch is the caller's to roll back: a rollback
    expires every object of the session, the caller's admin included."""
    _patch_prices(monkeypatch, _HAIKU_PRICE)
    _patch_rebuild(monkeypatch)
    monkeypatch.setattr(repricing, "_BATCH_SIZE", 2)
    yesterday = datetime.now(UTC) - timedelta(days=1)
    committed = [_trace(started_at=yesterday), _trace(started_at=yesterday)]
    priced_then_lost = _trace(started_at=yesterday)
    broken = _trace(started_at=yesterday, metadata_json={"provider": "anthropic"})
    _failing_price_for(monkeypatch, "anthropic")
    db = _FakeSession([committed, [priced_then_lost, broken], []])

    with pytest.raises(Exception, match="database went away") as excinfo:
        await repricing.reprice_null_costs(db)

    applied = excinfo.value.result
    assert applied.traces_updated == 2
    assert applied.affected_dates == {yesterday.date()}
    assert db.log == ["lock", "mark_stale", "commit"]


@pytest.mark.asyncio
async def test_a_batch_loads_only_the_columns_it_prices(monkeypatch):
    """A batch read every column, the input / output payloads included - up to
    1 MiB each inline, 500 rows at a time. Pricing reads none of them."""
    _patch_prices(monkeypatch, _HAIKU_PRICE)
    db = _FakeSession([[], [], []])

    await repricing.reprice_null_costs(db)

    trace_select, span_select = db.statements[:2]
    assert "llm_traces.input_tokens" in trace_select
    assert not re.search(r"llm_traces\.(input|output)\b", trace_select)
    assert "llm_spans.usage_details" in span_select
    assert not re.search(
        r"llm_spans\.(input|output|metadata|model_parameters|tool_arguments)\b", span_select
    )


@pytest.fixture
def audits(monkeypatch):
    calls: list[dict] = []

    async def _log_audit(db, **kwargs):
        calls.append(kwargs)
        db.log.append("audit")

    monkeypatch.setattr("app.api.v1.settings.log_audit", _log_audit)
    return calls


@pytest.fixture
def own_sessions(monkeypatch):
    """The sessions opened with ``async_session()`` rather than the request's."""
    opened: list[_FakeSession] = []

    @contextlib.asynccontextmanager
    async def _session():
        session = _FakeSession([])
        opened.append(session)
        yield session

    monkeypatch.setattr("app.core.database.async_session", _session)
    return opened


async def _call_reprice_route(db, **body):
    from app.api.v1.settings import ModelPricingRepriceRequest, reprice_null_cost_traces

    return await reprice_null_cost_traces(
        body=ModelPricingRepriceRequest(**body),
        request=SimpleNamespace(headers={}, client=None),
        user=SimpleNamespace(id=uuid4(), email="admin@example.com"),
        db=db,
        _ai_gate=None,
    )


@pytest.mark.asyncio
async def test_the_route_commits_its_audit_before_answering(monkeypatch, audits):
    """``get_db`` only commits after the response is sent."""
    _patch_prices(monkeypatch, _HAIKU_PRICE)
    _patch_rebuild(monkeypatch)
    db = _FakeSession([[_trace(started_at=datetime.now(UTC) - timedelta(days=2))], [], []])

    response = await _call_reprice_route(db)

    assert response.traces_updated == 1
    assert len(audits) == 1
    assert db.log[-2:] == ["audit", "commit"]


@pytest.mark.asyncio
async def test_a_failed_reprice_audits_what_it_applied_before_the_500(
    monkeypatch, audits, own_sessions
):
    """With per-batch commits a failed request is not
    atomic any more. What the committed batches applied is audited - and
    committed, on a session of its own - before the error is answered."""
    _patch_prices(monkeypatch, _HAIKU_PRICE)
    _patch_rebuild(monkeypatch)
    monkeypatch.setattr(repricing, "_BATCH_SIZE", 1)
    yesterday = datetime.now(UTC) - timedelta(days=1)
    broken = _trace(started_at=yesterday, metadata_json={"provider": "anthropic"})
    _failing_price_for(monkeypatch, "anthropic")
    db = _FakeSession([[_trace(started_at=yesterday)], [broken], []])

    with pytest.raises(HTTPException) as excinfo:
        await _call_reprice_route(db, model="claude-haiku-4-5-20251001")

    assert excinfo.value.status_code == 500
    assert "1 trace(s)" in excinfo.value.detail
    assert len(audits) == 1
    summary = audits[0]["summary"]
    assert "1 traces" in summary and "failed" in summary
    assert "model=claude-haiku-4-5-20251001" in summary
    assert [session.log for session in own_sessions] == [["audit", "commit"]]
    # The failed batch is left to the request's teardown rollback.
    assert db.log == ["lock", "mark_stale", "commit"]


@pytest.mark.asyncio
async def test_a_failed_inline_rebuild_still_answers_and_audits_the_run(
    monkeypatch, audits, own_sessions
):
    """The inline rebuild runs once every batch is
    committed. Its failure - a lost connection the rebuild's own rollback
    cannot recover from - must not turn a run that applied its costs into an
    unaudited 500: every day it would rebuild keeps its stale rows and its
    record, which the backfill rebuilds."""
    import app.services.trace_storage as trace_storage

    _patch_prices(monkeypatch, _HAIKU_PRICE)

    async def _broken(_db, target, **_kwargs):
        raise ConnectionError("connection lost during the rollback")

    monkeypatch.setattr(trace_storage, "rebuild_daily_stats", _broken)
    db = _FakeSession([[_trace(started_at=datetime.now(UTC) - timedelta(days=2))], [], []])

    response = await _call_reprice_route(db)

    assert response.traces_updated == 1
    assert response.days_reaggregated == 0
    assert len(audits) == 1
    assert db.log[-2:] == ["audit", "commit"]


@pytest.mark.asyncio
async def test_a_reprice_that_failed_before_any_commit_writes_no_audit(
    monkeypatch, audits, own_sessions
):
    _patch_prices(monkeypatch, _HAIKU_PRICE)
    _patch_rebuild(monkeypatch)

    async def _resolve(self, model, provider):
        raise RuntimeError("database went away")

    monkeypatch.setattr(repricing._PriceMemo, "resolve", _resolve)
    db = _FakeSession([[_trace(started_at=datetime.now(UTC) - timedelta(days=1))], []])

    with pytest.raises(HTTPException) as excinfo:
        await _call_reprice_route(db)

    assert excinfo.value.status_code == 500
    assert excinfo.value.detail == "Failed to reprice traces; see server logs."
    assert audits == []
    assert own_sessions == []
    assert "commit" not in db.log
