"""The Module Analyst ceiling ledger on PostgreSQL (#101 S14b, dev-docs/platform-health.md 12.5):
test (i) a forced over-spend is refused at the day and month ceiling, a booking replaces the
reservation, and the ledger is apart from the analysis ledger."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.models.health_analysis import HealthAnalysisSpend, HealthModuleAnalystSpend
from app.services.health import module_analyst_budget as mab
from app.services.health.settings import HealthModuleAnalystSettings
from tests.test_provisioning_postgres import _temp_database

pytestmark = pytest.mark.integration

T0 = datetime(2026, 11, 21, 3, 0, tzinfo=UTC)


def _settings(**kw) -> HealthModuleAnalystSettings:
    values = {
        "cycle_tokens": 100_000,
        "day_tokens": 10_000,
        "day_cost_usd": Decimal("1"),
        "month_tokens": 100_000,
        "month_cost_usd": Decimal("10"),
    }
    values.update(kw)
    return HealthModuleAnalystSettings(**values)


async def test_a_forced_over_spend_is_refused_at_the_day_ceiling():
    async with _temp_database("ma_day") as sessions, sessions() as db:
        assert await mab.reserve(db, _settings(), now=T0, tokens=8_000, cost=Decimal("0.5")) is None
        await db.commit()
        refusal = await mab.reserve(db, _settings(), now=T0, tokens=3_000, cost=Decimal("0.1"))
        assert refusal == mab.REFUSED_CEILING_REACHED
        day = await db.get(HealthModuleAnalystSpend, mab.day_period(T0))
        assert day.reserved_tokens == 8_000


async def test_a_forced_over_spend_is_refused_at_the_month_ceiling():
    async with _temp_database("ma_month") as sessions, sessions() as db:
        settings = _settings(day_tokens=1_000_000, day_cost_usd=Decimal("100"), month_cost_usd=Decimal("1"))
        assert await mab.reserve(db, settings, now=T0, tokens=100, cost=Decimal("0.9")) is None
        assert await mab.reserve(db, settings, now=T0, tokens=100, cost=Decimal("0.2")) == mab.REFUSED_CEILING_REACHED


async def test_a_booking_replaces_the_reservation_with_the_real_usage():
    async with _temp_database("ma_book") as sessions, sessions() as db:
        assert await mab.reserve(db, _settings(), now=T0, tokens=5_000, cost=Decimal("0.5")) is None
        await mab.book(
            db,
            now=T0,
            reserved_tokens=5_000,
            reserved_cost=Decimal("0.5"),
            spent_tokens=1_200,
            spent_cost=Decimal("0.1"),
        )
        await db.commit()
        for period in (mab.day_period(T0), mab.month_period(T0)):
            row = await db.get(HealthModuleAnalystSpend, period)
            assert (row.reserved_tokens, row.spent_tokens) == (0, 1_200)
            assert Decimal(row.spent_cost_usd) == Decimal("0.1")
        assert (await mab.spend(db, T0))["day"]["tokens"] == 1_200
        # One ceiling, one ledger: the analysis ledger did not move.
        assert await db.scalar(select(func.count()).select_from(HealthAnalysisSpend)) == 0
