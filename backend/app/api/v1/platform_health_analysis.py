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

"""Platform Health analysis API (#101 S9, ``dev-docs/platform-health.md`` section 6).

Mounted under ``/api/v1/health`` beside the other Platform Health routers,
administrators only:

* ``GET /analysis``: the latest analysis per module present, the ceilings and
  today's and this month's spend (readable with AI off: it reads stored rows),
  and the Module Analyst's own ceilings and spend (``module_analyst``, #101 S14d);
* ``POST /analysis/{module}/run``: an on-demand analysis of one module, gated by
  ``require_ai_enabled`` and refused over the ceiling like a scheduled one.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_admin, require_ai_enabled
from app.api.v1.platform_health_findings import present_modules
from app.core.database import get_db
from app.models.health_analysis import HealthModuleAnalysis
from app.models.user import User
from app.services.health import analysis
from app.services.health.settings import load_health_settings

router = APIRouter()


class ModuleAnalysis(BaseModel):
    module: str
    status: str | None
    reason: str | None
    summary: str
    model: str | None
    tokens: int
    cost_usd: Decimal
    trigger: str | None
    analysed_at: datetime | None


class AnalysisSpend(BaseModel):
    tokens: int
    cost_usd: Decimal
    reserved_tokens: int


class AnalysisCeilings(BaseModel):
    day_tokens: int | None
    day_cost_usd: Decimal | None
    month_tokens: int | None
    month_cost_usd: Decimal | None


class ModuleAnalystCeilings(AnalysisCeilings):
    cycle_tokens: int | None


class ModuleAnalystSpendRow(BaseModel):
    """The Module Analyst row of Spend limits (#101 S14d, dev-docs 12.5 and 12.7): its own ledger."""

    enabled: bool
    #: ``False`` while a ceiling is unset: every model call of the analyst is then refused.
    ceilings_set: bool
    ceilings: ModuleAnalystCeilings
    day: AnalysisSpend
    month: AnalysisSpend


class AnalysisOverview(BaseModel):
    enabled: bool
    #: ``False`` while a ceiling is unset: every analysis is then refused.
    ceilings_set: bool
    ceilings: AnalysisCeilings
    day: AnalysisSpend
    month: AnalysisSpend
    modules: list[ModuleAnalysis]
    module_analyst: ModuleAnalystSpendRow


def _module(module: str, row: HealthModuleAnalysis | None) -> ModuleAnalysis:
    if row is None:
        return ModuleAnalysis(
            module=module,
            status=None,
            reason=None,
            summary="",
            model=None,
            tokens=0,
            cost_usd=Decimal(0),
            trigger=None,
            analysed_at=None,
        )
    return ModuleAnalysis(
        module=module,
        status=row.status,
        reason=row.reason,
        summary=row.summary,
        model=row.model,
        tokens=row.tokens,
        cost_usd=Decimal(row.cost_usd),
        trigger=row.trigger,
        analysed_at=row.analysed_at,
    )


async def _module_analyst_row(db: AsyncSession, now: datetime) -> ModuleAnalystSpendRow:
    from app.services.health import module_analyst_budget

    settings = (await load_health_settings(db)).module_analyst
    spent = await module_analyst_budget.spend(db, now)
    return ModuleAnalystSpendRow(
        enabled=settings.enabled,
        ceilings_set=settings.ceilings_set(),
        ceilings=ModuleAnalystCeilings(
            cycle_tokens=settings.cycle_tokens,
            day_tokens=settings.day_tokens,
            day_cost_usd=settings.day_cost_usd,
            month_tokens=settings.month_tokens,
            month_cost_usd=settings.month_cost_usd,
        ),
        day=AnalysisSpend(**spent["day"]),
        month=AnalysisSpend(**spent["month"]),
    )


async def _overview(db: AsyncSession) -> AnalysisOverview:
    settings = (await load_health_settings(db)).analysis
    modules = present_modules()
    rows = {
        row.module: row
        for row in (
            await db.execute(select(HealthModuleAnalysis).where(HealthModuleAnalysis.module.in_(modules)))
        ).scalars()
    }
    spent = await analysis.spend(db, datetime.now(UTC))
    return AnalysisOverview(
        enabled=settings.enabled,
        ceilings_set=settings.ceilings_set(),
        ceilings=AnalysisCeilings(
            day_tokens=settings.day_tokens,
            day_cost_usd=settings.day_cost_usd,
            month_tokens=settings.month_tokens,
            month_cost_usd=settings.month_cost_usd,
        ),
        day=AnalysisSpend(**spent["day"]),
        month=AnalysisSpend(**spent["month"]),
        modules=[_module(module, rows.get(module)) for module in modules],
        module_analyst=await _module_analyst_row(db, datetime.now(UTC)),
    )


@router.get("/analysis", response_model=AnalysisOverview)
async def get_health_analysis(
    _admin: User = Depends(require_admin), db: AsyncSession = Depends(get_db)
) -> AnalysisOverview:
    """The latest analysis per module, the ceilings and the spend (stored rows only, no model)."""
    return await _overview(db)


@router.post("/analysis/{module}/run", response_model=ModuleAnalysis)
async def run_health_analysis(
    module: str,
    _admin: User = Depends(require_admin),
    _ai: None = Depends(require_ai_enabled),
    db: AsyncSession = Depends(get_db),
) -> ModuleAnalysis:
    """Analyse one module now, within the ceiling; a refusal is answered with its reason, not an error."""
    if module not in present_modules():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown health module")
    settings = (await load_health_settings(db)).analysis
    await analysis.analyse_module(db, module, settings, now=datetime.now(UTC), trigger="on_demand")
    return _module(module, await db.get(HealthModuleAnalysis, module))
