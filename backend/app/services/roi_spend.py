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

"""The LLM spend the ROI reports count as AI cost (#10441).

A report used to sum ``roi_run_records.llm_cost``: the traces of the runs that
recorded a unit. Every run that recorded none (most of an agent's activity) and
every agent whose unit is counted per work item (its record carries no run, so
no trace) were left out, and the ROI read two orders of magnitude too high.

The cost of a scope is now what its agents spent in the window, read through
the aggregate LLM Analytics reads (:func:`app.services.tracing.breakdown.breakdown`
over ``analytics_stats_subquery``, grouped by agent, as an administrator sees
it), then spread over the scope's records:

* :func:`agent_spend` reads the spend of each agent in the window.
* :func:`allocate` gives each record of an agent an equal share of that agent's
  spend (pro rata to its records in the window, whatever the scope reads), and
  adds one cost-only row (:data:`app.services.roi.COST_ONLY_UNIT`) for each
  agent of the scope that spent without recording anything, in its current home
  fleet. Its rows therefore sum to the agents' spend.

Usage no agent carries (general chat, the LLM proxy, other agentless traces) is
not an agent's spend and is not counted: :data:`COST_SOURCE_LABEL` says so.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.services import roi

#: Where the AI cost of a report comes from, as the Value page's footnote and the exports name it.
COST_SOURCE = "llm_analytics_by_agent"
COST_SOURCE_LABEL = (
    "AI cost is the LLM cost of the scope's agents in the window, as LLM Analytics reports it; "
    "usage no agent carries (general chat, LLM proxy) is not counted."
)

#: How far back a report "since the start" reads the spend: the longest trace retention (365 days).
SPEND_HORIZON = timedelta(days=365)


@dataclass
class _SpendFilters:
    """The trace store's analytics filters: every model and source, the agents asked or all of them."""

    period: str = "custom"
    date_from: datetime | None = None
    date_to: datetime | None = None
    agent_ids: str | None = None
    user_ids: str | None = None
    models: str | None = None
    sources: str | None = None


async def agent_spend(
    db: AsyncSession,
    *,
    since: datetime | None,
    now: datetime | None = None,
    agent_ids: Iterable[uuid.UUID] | None = None,
) -> dict[uuid.UUID, float]:
    """The LLM cost of each agent from *since* to *now*, the figure LLM Analytics shows for it.

    *agent_ids* narrows the read (an empty iterable reads nothing); ``None`` reads every agent.
    Agentless usage is left out. Read as an administrator: the ROI scope is enforced by the
    caller on the records and on the agents it asks for, never by the trace visibility.
    """
    from app.services.tracing.breakdown import breakdown
    from app.services.tracing.read_store import use_daily_stats

    wanted = None if agent_ids is None else sorted({a for a in agent_ids if a is not None}, key=str)
    if wanted is not None and not wanted:
        return {}
    end = now or datetime.now(UTC)
    start = since if since is not None else end - SPEND_HORIZON
    filters = _SpendFilters(
        date_from=start,
        date_to=end,
        agent_ids=",".join(str(a) for a in wanted) if wanted else None,
    )
    rows = await breakdown(
        db,
        filters,
        viewer=SimpleNamespace(id=None, is_admin=True),
        user_group_ids=[],
        start=start,
        end=end,
        use_daily=use_daily_stats(start, end),
        keys=["agent"],
    )
    spend: dict[uuid.UUID, float] = {}
    for row in rows:
        raw = row.keys.get("agent")
        if raw is None or not row.sums.cost:
            continue
        agent_id = uuid.UUID(str(raw))
        spend[agent_id] = spend.get(agent_id, 0.0) + float(row.sums.cost)
    return spend


def allocate(
    rows: Iterable[Any],
    spend: Mapping[uuid.UUID, float],
    *,
    records_per_agent: Mapping[uuid.UUID, int],
    scope_agents: Iterable[uuid.UUID] = (),
    home_fleets: Mapping[uuid.UUID, uuid.UUID] | None = None,
    make_row: Any,
) -> list[Any]:
    """*rows* (``RecordRow``) with their cost replaced by their agent's share of *spend*, then the cost-only rows.

    A record's share is its agent's spend divided by *records_per_agent* (the agent's records in
    the window, every scope included, so a record costs the same in every report). A record of a
    deleted agent (no ``agent_id``) costs nothing here: its spend, under the agent's former id, is
    a cost-only row. Each agent of *scope_agents* with spend and no record in the window gets one
    cost-only row, built by ``make_row(agent_id, fleet_id, measure)`` in its home fleet.
    """
    homes = home_fleets or {}
    out: list[Any] = []
    for row in rows:
        agent_id = row.agent_id
        count = records_per_agent.get(agent_id, 0) if agent_id is not None else 0
        share = spend.get(agent_id, 0.0) / count if agent_id is not None and count else 0.0
        out.append(replace(row, measure=replace(row.measure, llm_cost=share)))
    for agent_id in sorted(set(scope_agents), key=str):
        cost = spend.get(agent_id, 0.0)
        if cost <= 0 or records_per_agent.get(agent_id, 0):
            continue
        measure = roi.RunMeasure(unit=roi.COST_ONLY_UNIT, units=0, llm_cost=cost, outcome=None, cost_only=True)
        out.append(make_row(agent_id, homes.get(agent_id), measure))
    return out


def unallocated(rows: Iterable[Any]) -> float:
    """The cost of the cost-only rows of *rows*: the spend no recorded unit carries."""
    total: float = sum(float(r.measure.llm_cost) for r in rows if r.measure.cost_only)
    return round(total, 4)
