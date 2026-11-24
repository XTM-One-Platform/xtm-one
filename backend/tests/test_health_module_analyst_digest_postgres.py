# Copyright (c) 2025-2026 Filigran SAS
#
# This file is part of the XTM One Enterprise Edition ("EE") and is
# licensed under the XTM One Enterprise Edition License (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://github.com/XTM-One-Platform/xtm-one/blob/main/LICENSE
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.

"""The Module Analyst in the cycle digest and in Spend limits (#101 S14d, dev-docs 12.7).

A module cycle's digest counts the recommendations it attached and the agentic findings of
its registration opened and resolved in its window, and reads its spend against its own
per-cycle cap; ``GET /health/analysis`` carries the Module Analyst row of Spend limits.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import app.models  # noqa: F401 - registers every table on Base.metadata
from app.api import deps
from app.api.v1 import platform_health_analysis
from app.core.database import get_db
from app.models.health import HealthCycle
from app.models.health_module_analyst import HealthFindingRecommendation
from app.services.health import module_analyst_budget
from app.services.health.digest import build_digest
from app.services.health.engine import CheckResult, finding_key, record_results
from tests.health_support import health_database, make_user

pytestmark = pytest.mark.integration

NOW = datetime.now(UTC)


def _failing(check_id: str, subject: dict[str, str]) -> CheckResult:
    return CheckResult(
        check_id=check_id,
        module="opencti",
        category="availability",
        severity="warning",
        failing=True,
        subject=subject,
        evidence={"values": {"x": 1}},
        recommendation="Do it.",
    )


async def _module_cycle(db: AsyncSession, registration: uuid.UUID) -> HealthCycle:
    cycle = HealthCycle(
        specialist="module_analyst",
        trigger="finding_change",
        target_type="module",
        target_id=registration,
        month=NOW.strftime("%Y-%m"),
        status="completed",
        opened_at=NOW - timedelta(minutes=10),
        closed_at=NOW + timedelta(minutes=1),
    )
    db.add(cycle)
    await db.flush()
    return cycle


@pytest.mark.asyncio
async def test_a_module_cycle_digest_counts_its_recommendations_and_agentic_findings() -> None:
    registration, other = uuid.uuid4(), uuid.uuid4()
    subject, other_subject = {"registration_id": str(registration)}, {"registration_id": str(other)}
    async with health_database() as sessions, sessions() as db:
        await record_results(
            db,
            [
                _failing("opencti.heartbeat", subject),
                _failing("opencti.agentic.connector_backlog", subject),
                _failing("opencti.agentic.queue_growth", other_subject),
            ],
            now=NOW - timedelta(minutes=5),
        )
        cycle = await _module_cycle(db, registration)
        heartbeat = finding_key("opencti.heartbeat", subject)
        backlog = finding_key("opencti.agentic.connector_backlog", subject)
        for finding_id, registration_id, confidence, age in (
            (heartbeat, registration, "high", 2),
            (backlog, registration, "medium", 3),
            # Another registration, and one older than the window: neither is this cycle's.
            (finding_key("opencti.agentic.queue_growth", other_subject), other, "low", 2),
            (heartbeat, registration, "low", 60),
        ):
            db.add(
                HealthFindingRecommendation(
                    finding_id=finding_id,
                    registration_id=registration_id,
                    cycle_id=uuid.uuid4(),
                    module="opencti",
                    text="Restart the worker.",
                    evidence=[],
                    confidence=confidence,
                    created_at=NOW - timedelta(minutes=age),
                )
            )
        await db.flush()

        digest = await build_digest(db, cycle)

    assert digest.recommendations == {"attached": 2, "high": 1, "medium": 1, "low": 0}
    assert digest.agentic_findings == {"opened": 1, "resolved": 0}
    assert "recommendations" not in digest.unavailable and "agentic_findings" not in digest.unavailable
    # Its spend is on its own ledger: the digest reads it against its own per-cycle cap.
    assert digest.budget["ledger"] == "module_analyst"
    assert (digest.budget["spent_tokens"], digest.budget["cap_tokens"]) == (0, 80_000)


@pytest.mark.asyncio
async def test_another_specialists_digest_keeps_the_fleet_budget_and_no_module_sections() -> None:
    async with health_database() as sessions, sessions() as db:
        cycle = HealthCycle(
            specialist="quality_analyst",
            trigger="schedule",
            target_type="agent",
            target_id=uuid.uuid4(),
            month=NOW.strftime("%Y-%m"),
            status="completed",
            opened_at=NOW - timedelta(minutes=10),
            closed_at=NOW,
        )
        db.add(cycle)
        await db.flush()

        digest = await build_digest(db, cycle)

    assert digest.recommendations is None and digest.agentic_findings is None
    assert "ledger" not in digest.budget


@asynccontextmanager
async def _client(sessions: async_sessionmaker[AsyncSession], user: Any) -> AsyncIterator[httpx.AsyncClient]:
    api = FastAPI()
    api.include_router(platform_health_analysis.router, prefix="/api/v1/health")

    async def _get_db() -> AsyncIterator[AsyncSession]:
        async with sessions() as db:
            yield db
            await db.commit()

    api.dependency_overrides[deps.get_current_user] = lambda: user
    api.dependency_overrides[get_db] = _get_db
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://test") as client:
        yield client


@pytest.mark.asyncio
async def test_spend_limits_carry_the_module_analyst_row_with_its_spend() -> None:
    admin = make_user("admin", admin=True)
    async with health_database() as sessions:
        async with sessions() as db:
            db.add(admin)
            await module_analyst_budget.book(
                db,
                now=NOW,
                reserved_tokens=0,
                reserved_cost=Decimal(0),
                spent_tokens=22_600,
                spent_cost=Decimal("0.108"),
            )
            await db.commit()
        async with _client(sessions, admin) as client:
            body = (await client.get("/api/v1/health/analysis")).json()

    row = body["module_analyst"]
    assert (row["enabled"], row["ceilings_set"]) == (True, True)
    assert row["ceilings"]["cycle_tokens"] == 80_000
    assert (row["ceilings"]["day_tokens"], row["ceilings"]["month_tokens"]) == (500_000, 10_000_000)
    assert Decimal(row["ceilings"]["day_cost_usd"]) == Decimal(4)
    assert (row["day"]["tokens"], Decimal(row["day"]["cost_usd"])) == (22_600, Decimal("0.108"))
    assert row["month"]["tokens"] == 22_600
