"""Platform Health analysis on PostgreSQL (#101 S9, dev-docs/platform-health.md 6):
test (e) a forced over-spend is refused at the ceiling, a completed analysis books its real
cost, an unchanged finding set is not analysed again, and no quota counter moves."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from app.models.agentic_execution import AgenticExecutionDaily
from app.models.health_analysis import HealthAnalysisSpend, HealthModuleAnalysis
from app.services.health import analysis
from app.services.health.engine import CheckResult, record_results
from app.services.health.settings import HealthAnalysisSettings
from tests.test_provisioning_postgres import _temp_database

pytestmark = pytest.mark.integration

T0 = datetime(2026, 11, 20, 3, 0, tzinfo=UTC)
PRICE = SimpleNamespace(name="price")


def _settings(**kw) -> HealthAnalysisSettings:
    values = {
        "enabled": True,
        "day_tokens": 100_000,
        "day_cost_usd": Decimal("10"),
        "month_tokens": 1_000_000,
        "month_cost_usd": Decimal("100"),
    }
    values.update(kw)
    return HealthAnalysisSettings(**values)


class _LLM:
    def __init__(self) -> None:
        self.calls = 0

    async def chat(self, messages, model=None, temperature=0.7, max_tokens=4096, **kwargs):
        self.calls += 1
        return SimpleNamespace(
            content="Two agents fail on the same tool.", usage={"input_tokens": 300, "output_tokens": 50}
        )


@pytest.fixture
def wired(monkeypatch):
    """No tracing backend, a fixed price: 1 dollar per 1,000 tokens."""
    import app.services.langfuse_tracing as tracing
    import app.services.quota as quota
    import app.services.tracing.cost_calculator as cost

    traces: list[dict] = []

    async def trace_llm_call(db, **kwargs):
        traces.append(kwargs)
        return None

    monkeypatch.setattr(tracing, "trace_llm_call", trace_llm_call)
    monkeypatch.setattr(tracing, "end_trace", lambda *a, **k: None)

    async def price_any(db, model):
        return PRICE

    def price_usage(usage, price):
        tokens = sum(int(v) for k, v in (usage or {}).items() if k in ("input_tokens", "output_tokens"))
        return SimpleNamespace(total_cost=Decimal(tokens) / Decimal(1000))

    monkeypatch.setattr(cost, "resolve_model_price_any", price_any)
    monkeypatch.setattr(cost, "price_usage", price_usage)

    touched: list[str] = []
    for name in ("increment_agentic_usage", "check_agentic_quota", "increment_llm_proxy_usage"):

        async def spy(*args, _name=name, **kwargs):
            touched.append(_name)

        monkeypatch.setattr(quota, name, spy)
    return SimpleNamespace(traces=traces, touched=touched)


async def _findings(db, n: int = 2) -> None:
    await record_results(
        db,
        [
            CheckResult(
                check_id="agents.tool_errors",
                module="agents",
                category="performance",
                severity="warning",
                failing=True,
                subject={"agent_id": f"a{i}"},
                evidence={"values": {"rate": 0.3}},
            )
            for i in range(n)
        ],
        now=T0,
    )
    await db.commit()


@pytest.mark.asyncio
async def test_a_forced_over_spend_is_refused_at_the_ceiling(wired):
    """Test (e): a day ceiling under one analysis's worst case refuses it; nothing is spent."""
    llm = _LLM()

    async def factory(db):
        return llm, "routing-model"

    async with _temp_database("ha_ceiling") as sessions, sessions() as db:
        await _findings(db)
        outcome = await analysis.analyse_module(
            db, "agents", _settings(day_cost_usd=Decimal("0.0001")), now=T0, llm_factory=factory
        )
        assert (outcome.status, outcome.reason) == ("refused", "ceiling_reached")
        assert llm.calls == 0
        spent = await db.scalar(select(func.coalesce(func.sum(HealthAnalysisSpend.spent_cost_usd), 0)))
        reserved = await db.scalar(select(func.coalesce(func.sum(HealthAnalysisSpend.reserved_tokens), 0)))
        assert (Decimal(spent), int(reserved)) == (Decimal(0), 0)
        row = await db.get(HealthModuleAnalysis, "agents")
        assert (row.status, row.reason) == ("refused", "ceiling_reached")


@pytest.mark.asyncio
async def test_unset_ceilings_refuse_without_a_model_call(wired):
    llm = _LLM()

    async def factory(db):
        return llm, "routing-model"

    async with _temp_database("ha_unset") as sessions, sessions() as db:
        await _findings(db)
        outcome = await analysis.analyse_module(
            db, "agents", HealthAnalysisSettings(enabled=True, day_tokens=None), now=T0, llm_factory=factory
        )
        assert (outcome.status, outcome.reason, llm.calls) == ("refused", "ceiling_not_set", 0)


@pytest.mark.asyncio
async def test_a_completed_analysis_books_its_real_cost_and_is_not_repeated(wired):
    llm = _LLM()

    async def factory(db):
        return llm, "routing-model"

    async with _temp_database("ha_book") as sessions, sessions() as db:
        before = await db.scalar(select(func.count()).select_from(AgenticExecutionDaily))
        await _findings(db)
        outcome = await analysis.analyse_module(db, "agents", _settings(), now=T0, llm_factory=factory)
        assert outcome.status == "completed" and outcome.tokens == 350
        assert outcome.cost_usd == Decimal("0.35")
        for period in (analysis.day_period(T0), analysis.month_period(T0)):
            row = await db.get(HealthAnalysisSpend, period)
            await db.refresh(row)
            assert (row.spent_tokens, Decimal(row.spent_cost_usd), row.reserved_tokens) == (
                350,
                Decimal("0.350000"),
                0,
            )
        assert wired.traces[0]["source"] == "platform_health"
        assert wired.traces[0]["user_id"] == "system"

        # Same findings: skipped. A new finding the same day: still at most once a day.
        again = await analysis.analyse_module(
            db, "agents", _settings(), now=T0 + timedelta(hours=1), llm_factory=factory
        )
        assert (again.status, again.reason) == ("skipped", "unchanged")
        await _findings(db, 3)
        same_day = await analysis.analyse_module(
            db, "agents", _settings(), now=T0 + timedelta(hours=2), llm_factory=factory
        )
        assert (same_day.status, same_day.reason) == ("skipped", "already_today")
        next_day = await analysis.analyse_module(
            db, "agents", _settings(), now=T0 + timedelta(days=1), llm_factory=factory
        )
        assert next_day.status == "completed"
        assert llm.calls == 2

        after = await db.scalar(select(func.count()).select_from(AgenticExecutionDaily))
    assert wired.touched == []
    assert before == after == 0


class _FailingLLM:
    def __init__(self) -> None:
        self.calls = 0

    async def chat(self, messages, model=None, temperature=0.7, max_tokens=4096, **kwargs):
        self.calls += 1
        raise TimeoutError("provider timeout")


@pytest.mark.asyncio
async def test_a_failed_analysis_books_its_worst_case_and_waits_for_the_next_day(wired):
    """A model error is today's attempt: not retried on the next pass, and its reserved worst
    case is booked as spent (a provider may bill a timed-out call), never zero."""
    llm = _FailingLLM()

    async def factory(db):
        return llm, "routing-model"

    async with _temp_database("ha_failed") as sessions, sessions() as db:
        await _findings(db)
        outcome = await analysis.analyse_module(db, "agents", _settings(), now=T0, llm_factory=factory)
        assert (outcome.status, outcome.reason) == ("failed", "model_error")
        day = await db.get(HealthAnalysisSpend, analysis.day_period(T0))
        await db.refresh(day)
        assert day.reserved_tokens == 0
        assert day.spent_tokens > analysis.MAX_OUTPUT_TOKENS
        assert Decimal(day.spent_cost_usd) > 0
        row = await db.get(HealthModuleAnalysis, "agents")
        assert (row.status, row.tokens) == ("failed", day.spent_tokens)

        again = await analysis.analyse_module(
            db, "agents", _settings(), now=T0 + timedelta(minutes=15), llm_factory=factory
        )
        assert (again.status, again.reason) == ("skipped", "already_today")
        assert llm.calls == 1
        next_day = await analysis.analyse_module(
            db, "agents", _settings(), now=T0 + timedelta(days=1), llm_factory=factory
        )
        assert next_day.status == "failed"
        assert llm.calls == 2


@pytest.mark.asyncio
async def test_a_refused_analysis_is_not_retried_on_the_same_day(wired):
    llm = _LLM()

    async def factory(db):
        return llm, "routing-model"

    async with _temp_database("ha_refused") as sessions, sessions() as db:
        await _findings(db)
        first = await analysis.analyse_module(
            db, "agents", _settings(day_cost_usd=Decimal("0.0001")), now=T0, llm_factory=factory
        )
        assert (first.status, first.reason) == ("refused", "ceiling_reached")
        again = await analysis.analyse_module(
            db, "agents", _settings(day_cost_usd=Decimal("0.0001")), now=T0 + timedelta(minutes=15), llm_factory=factory
        )
        assert (again.status, again.reason) == ("skipped", "already_today")
        on_demand = await analysis.analyse_module(
            db, "agents", _settings(), now=T0 + timedelta(minutes=20), trigger="on_demand", llm_factory=factory
        )
        assert on_demand.status == "completed"
        assert llm.calls == 1


@pytest.mark.asyncio
async def test_the_tick_does_nothing_with_ai_off():
    assert await analysis.analysis_tick(ai_on=False) == []
