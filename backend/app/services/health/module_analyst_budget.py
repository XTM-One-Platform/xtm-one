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

"""The Module Analyst outside the agentic quota, under its own ceiling (#101 S14b, dev-docs 12.5).

This is the one exception to "an extra cap, never a bypass" of ``budget.py``, for
this member only. Two pieces live here, so the rule is in code and never in a prompt:

* :func:`is_module_analyst_run`, the server-only predicate the assignment engine
  reads before it charges the agentic quota. It holds only for the seeded,
  built-in, company-managed agent whose ``health_role`` is ``module_analyst``
  (a column only the seed writes, ``identity.set_health_role``) running the seeded
  module investigation assignment. No trigger payload, setting or run field a
  person or a tool can write turns it on; every other fleet member keeps counting
  in the tiers.
* The ceiling ledger ``health_module_analyst_spend`` (one row per UTC day and
  month, locked month then day, the ``analysis.py`` pattern): :func:`reserve`
  sets aside the worst case of a model call and refuses over any ceiling,
  :func:`book` replaces it with the real usage. A cleared ceiling refuses
  (``ceiling_not_set``).

S14b-1 ships the predicate, the exemption and the ledger; S14b-2 reserves on it
before every model call of the loop (``module_analyst_ceiling.py``) and takes the
member out of the fleet caps of ``budget.py`` (``open_cycle``, ``cycles._settle_cycle``).
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.health_analysis import HealthModuleAnalystSpend
from app.services.health.settings import HealthModuleAnalystSettings

#: The role the seed gave the S14 Module Analyst (``identity.HealthRole``), kept as an
#: alias of the module checkers' roles on rows written before S15 (section 13.1).
MODULE_ANALYST_ROLE: Final = "module_analyst"

REFUSED_CEILING_NOT_SET: Final = "ceiling_not_set"
REFUSED_CEILING_REACHED: Final = "ceiling_reached"
REFUSED_CYCLE_CEILING_REACHED: Final = "cycle_ceiling_reached"


def is_module_checker_run(agent: Any, assignment: Any) -> bool:
    """Whether a run of *assignment* by *agent* is a module checker's, exempt from the agentic quota.

    Read from the rows the engine loaded at run start; every condition is a column
    only the Platform Health seed writes. *agent* holds one of the three checker
    roles (section 13.1) and *assignment* is that agent's own seeded checks
    assignment (by its seeded names, never one an admin added to the agent).
    """
    from app.services.health.identity import MODULE_CHECKER_ROLES

    if agent is None or assignment is None:
        return False
    role = getattr(agent, "health_role", None)
    if role not in MODULE_CHECKER_ROLES:
        return False
    if getattr(agent, "is_builtin", False) is not True or getattr(agent, "is_company_managed", False) is not True:
        return False
    if getattr(assignment, "agent_id", None) != getattr(agent, "id", None):
        return False
    from app.services.health.fleet_seed import checks_assignment_names

    return getattr(assignment, "name", None) in checks_assignment_names(role)


def day_period(now: datetime) -> str:
    return "D:" + now.astimezone(UTC).strftime("%Y-%m-%d")


def month_period(now: datetime) -> str:
    return "M:" + now.astimezone(UTC).strftime("%Y-%m")


async def _locked_periods(db: AsyncSession, now: datetime) -> tuple[HealthModuleAnalystSpend, HealthModuleAnalystSpend]:
    """The day and month rows, created when missing and locked in a fixed order (month, then day)."""
    periods = (month_period(now), day_period(now))
    await db.execute(
        pg_insert(HealthModuleAnalystSpend).values([{"period": p} for p in periods]).on_conflict_do_nothing()
    )
    rows = {
        row.period: row
        for row in (
            await db.execute(
                select(HealthModuleAnalystSpend)
                .where(HealthModuleAnalystSpend.period.in_(periods))
                .order_by(HealthModuleAnalystSpend.period.desc())
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalars()
    }
    return rows[periods[1]], rows[periods[0]]


def _over(row: HealthModuleAnalystSpend, tokens: int, cost: Decimal, cap_tokens: int, cap_cost: Decimal) -> bool:
    used_tokens = row.reserved_tokens + row.spent_tokens + tokens
    used_cost = Decimal(row.reserved_cost_usd) + Decimal(row.spent_cost_usd) + cost
    return used_tokens > cap_tokens or used_cost > cap_cost


async def reserve(
    db: AsyncSession,
    settings: HealthModuleAnalystSettings,
    *,
    now: datetime,
    tokens: int,
    cost: Decimal,
    cycle_tokens_used: int = 0,
) -> str | None:
    """Reserve *tokens* / *cost* on today and this month; the refusal, or ``None`` once reserved. Flushed.

    *cycle_tokens_used* is what the cycle already spent and holds: the call is
    refused when it would take the cycle past ``cycle_tokens``.
    """
    if not settings.ceilings_set():
        return REFUSED_CEILING_NOT_SET
    if cycle_tokens_used + tokens > (settings.cycle_tokens or 0):
        return REFUSED_CYCLE_CEILING_REACHED
    day, month = await _locked_periods(db, now)
    if _over(day, tokens, cost, settings.day_tokens or 0, settings.day_cost_usd or Decimal(0)) or _over(
        month, tokens, cost, settings.month_tokens or 0, settings.month_cost_usd or Decimal(0)
    ):
        return REFUSED_CEILING_REACHED
    for row in (day, month):
        row.reserved_tokens += tokens
        row.reserved_cost_usd = Decimal(row.reserved_cost_usd) + cost
    await db.flush()
    return None


async def book(
    db: AsyncSession,
    *,
    now: datetime,
    reserved_tokens: int,
    reserved_cost: Decimal,
    spent_tokens: int,
    spent_cost: Decimal,
) -> None:
    """Release a reservation made at *now* and book what was really spent. Flushed.

    A failed call books its reserved worst case as spent (the caller passes it):
    a provider may bill a timed-out call, and an unknown cost is never zero.
    """
    day, month = await _locked_periods(db, now)
    for row in (day, month):
        row.reserved_tokens = max(0, row.reserved_tokens - reserved_tokens)
        row.reserved_cost_usd = max(Decimal(0), Decimal(row.reserved_cost_usd) - reserved_cost)
        row.spent_tokens += spent_tokens
        row.spent_cost_usd = Decimal(row.spent_cost_usd) + spent_cost
    await db.flush()


async def spend(db: AsyncSession, now: datetime) -> dict[str, dict[str, Any]]:
    """Today's and this month's spend, for the Spend limits row: ``{"day": {...}, "month": {...}}``."""
    periods = (("day", day_period(now)), ("month", month_period(now)))
    rows = {
        row.period: row
        for row in (
            await db.execute(
                select(HealthModuleAnalystSpend).where(HealthModuleAnalystSpend.period.in_([p for _, p in periods]))
            )
        ).scalars()
    }
    out: dict[str, dict[str, Any]] = {}
    for name, period in periods:
        row = rows.get(period)
        out[name] = {
            "tokens": int(row.spent_tokens) if row else 0,
            "cost_usd": str(Decimal(row.spent_cost_usd)) if row else "0",
            "reserved_tokens": int(row.reserved_tokens) if row else 0,
        }
    return out
