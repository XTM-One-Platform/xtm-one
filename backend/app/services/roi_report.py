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

"""ROI report: the figures per agent, per fleet and for the organization, and their exports.

Slice 3 of issue #8990 (``dev-docs/ctem-fleets.md`` section 5.3), on top of
the pure core ``services/roi.py`` and the records of ``services/roi_records.py``:

* :func:`build_report` is pure: it groups recorded runs by agent and by fleet
  and computes each group's figures with :func:`roi.compute_roi`, the
  organization's baselines and rate applied.
* :func:`rates_for` resolves the hourly rate of each role (#9321, design
  section 3.4): the organization's rate of that role in the currency
  reported, else the organization profile's loaded hourly rate when its
  currency is the one reported (labelled ``organization override``), else the
  published default of the catalog, else the ``analyst`` rate, each labelled.
* :func:`load_report` reads the records of a window and the organization's
  baselines, rates and profile, then builds the report of a scope: the
  platform (every record), one agent or one fleet (#9321).
* :func:`report_csv` and :func:`report_markdown` render the report shape; they go
  with that shape at slice L6 of #12077. ``GET /roi/export`` renders the ledger's
  figures since slice L4c (``services/roi_export.py``). The title names the
  product only in XTM One mode (:func:`report_title`): Copilot mode reads
  "Value delivered by your agents".
"""

from __future__ import annotations

import csv
import io
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.services import fleet_value_marks, roi
from app.services.roi_spend import COST_SOURCE, COST_SOURCE_LABEL, unallocated

#: The currency used when neither the request nor the organization names one.
DEFAULT_CURRENCY = "USD"

REPORT_TITLE = "Value delivered by XTM One"
#: The title of the report in Copilot mode, which runs no product called XTM One (#9321).
REPORT_TITLE_NEUTRAL = "Value delivered by your agents"

#: The report scopes (#9321): every record, one agent's, one fleet's.
SCOPE_PLATFORM = "platform"
SCOPE_AGENT = "agent"
SCOPE_FLEET = "fleet"
#: The records of the agents and fleets the caller reads (#9836 R1): every user's own view.
SCOPE_MINE = "mine"
SCOPES = (SCOPE_PLATFORM, SCOPE_AGENT, SCOPE_FLEET, SCOPE_MINE)

#: The name of the row that sums the agents or fleets outside the reader's scope (#9836 R1).
OTHER_ROW_NAME = "Other agents and fleets"
#: The name of the row that sums the runs outside any fleet (#9836 R1, UX round 2 B1).
NO_FLEET_ROW_NAME = "No fleet"

#: The rate label when the organization set a rate for at least one role used.
LABEL_ROLE_RATES = "organization rates by role"


def report_title(*, xtm_one: bool) -> str:
    """The title of the one-page report: the product's name in XTM One mode only."""
    return REPORT_TITLE if xtm_one else REPORT_TITLE_NEUTRAL


@dataclass(frozen=True)
class RecordRow:
    """One recorded run, as the report reads it."""

    agent_id: uuid.UUID | None
    fleet_id: uuid.UUID | None
    measure: roi.RunMeasure
    #: When the record was written; the week of the trend (#9836 R5a). ``None`` leaves it out of the trend.
    at: datetime | None = None


@dataclass
class ReportGroup:
    """The figures of one agent, one fleet or the organization."""

    id: str | None
    name: str
    figures: roi.RoiFigures


@dataclass
class RoiReport:
    window: str
    since: datetime | None
    currency: str
    rate_label: str
    organization: roi.RoiFigures
    agents: list[ReportGroup] = field(default_factory=list)
    fleets: list[ReportGroup] = field(default_factory=list)
    estimated: bool = True
    scope: str = SCOPE_PLATFORM
    scope_id: str | None = None
    scope_name: str | None = None
    #: The runs outside any fleet, so the fleet rows plus this one sum to the totals (#9836 R1).
    no_fleet: roi.RoiFigures | None = None
    #: The agents, and the fleets, outside the reader's scope, summed (platform report of a
    #: non-administrator, #9836 R1); ``None`` when nothing was folded.
    other_agents: roi.RoiFigures | None = None
    other_fleets: roi.RoiFigures | None = None
    #: Costs hidden from this reader (``OBSERVABILITY_COST_DATA_ENABLED`` off, #9836 R1).
    cost_masked: bool = False
    #: One row per unit of work, summing to the totals (the Value page's "By unit of work", #9836 R5a).
    units: list[ReportGroup] = field(default_factory=list)
    #: The value and net value per week of the window, oldest first (the Value page's trend, #9836 R5a).
    trend: list[TrendPoint] = field(default_factory=list)
    #: Records in the window written by a retroactive pass (#10006) and the latest pass.
    backfilled_records: int = 0
    backfilled_at: datetime | None = None
    #: Where the AI cost comes from (#10441): ``roi_spend.COST_SOURCE`` when :func:`load_report`
    #: priced the records with the agents' LLM spend, ``recorded_runs`` for a report built from
    #: the records alone.
    cost_source: str = "recorded_runs"
    #: The spend of the scope's agents no recorded unit carries (the "Cost not tied to a unit" row).
    unallocated_cost: float = 0.0
    #: A fleet report: the fleet's factory-reset mark when it falls inside the window (#2580),
    #: the views' "Measured since <date> (reset)"; ``None`` otherwise.
    value_measured_since: datetime | None = None
    #: How many fleets are measured since a reset inside the window (the platform total leaves
    #: their earlier records and spend out, #2580).
    reset_fleets: int = 0


@dataclass
class TrendPoint:
    """The figures of the records of one week (Monday 00:00 UTC), for the trend line."""

    week: datetime
    figures: roi.RoiFigures


#: The calendar windows next to the rolling ones of :data:`roi.WINDOWS` (#9836 R5a, design 10.7):
#: ``quarter`` is the current calendar quarter, so far.
WINDOW_QUARTER = "quarter"
REPORT_WINDOWS: tuple[str, ...] = (*roi.WINDOWS, WINDOW_QUARTER)


def quarter_start(now: datetime) -> datetime:
    """00:00 UTC on the first day of ``now``'s calendar quarter."""
    return datetime(now.year, 3 * ((now.month - 1) // 3) + 1, 1, tzinfo=UTC)


def window_days(window: str, *, now: datetime | None = None) -> int | None:
    """The days ``window`` spans, for the payback; ``None`` since the start."""
    if window == WINDOW_QUARTER:
        now = now or datetime.now(UTC)
        return max(1, (now - quarter_start(now)).days + 1)
    return roi.WINDOWS.get(window)


def week_start(at: datetime) -> datetime:
    """00:00 UTC on the Monday of ``at``'s week (a naive time is read as UTC)."""
    at = at.replace(tzinfo=UTC) if at.tzinfo is None else at.astimezone(UTC)
    day = datetime(at.year, at.month, at.day, tzinfo=UTC)
    return day - timedelta(days=day.weekday())


def window_since(window: str, *, now: datetime | None = None) -> datetime | None:
    """The start of ``window`` (one of :data:`REPORT_WINDOWS`), ``None`` for since the start."""
    if window == WINDOW_QUARTER:
        return quarter_start(now or datetime.now(UTC))
    if window not in roi.WINDOWS:
        raise ValueError(f"window {window!r} is not one of {', '.join(REPORT_WINDOWS)}")
    days = roi.WINDOWS[window]
    if days is None:
        return None
    return (now or datetime.now(UTC)) - timedelta(days=days)


@dataclass(frozen=True)
class RoleRate:
    """The organization's rate of one role in one currency (a ``roi_rates`` row)."""

    role: str
    currency: str
    amount: float
    source: str = ""


def rates_for(
    currency: str | None,
    *,
    loaded_hourly_rate: float | None = None,
    org_currency: str | None = None,
    role_rates: Iterable[RoleRate] = (),
    roles: Iterable[str] = (),
    fallback_role: str = roi.CTEM_FALLBACK_ROLE,
) -> tuple[str, dict[str, dict[str, roi.HourlyRate]], str]:
    """The currency, the rate table and the rate label the report uses.

    The currency reported is the one asked, else the organization's, else USD;
    it falls back to USD when neither the organization profile nor the
    catalog has a rate in it. Each role (the built-in ones, the roles in
    ``role_rates`` and ``roles``) then resolves to: the organization's rate of
    that role in that currency, else the profile's loaded hourly rate when its
    currency matches, else the catalog's published rate, else the
    ``fallback_role`` rate so resolved (``analyst`` in XTM One mode,
    ``knowledge_worker`` in Copilot mode, #9550), labelled as such.
    """
    org_currency = (org_currency or "").upper() or None
    wanted = (currency or org_currency or DEFAULT_CURRENCY).upper()
    org_rate = loaded_hourly_rate is not None and loaded_hourly_rate > 0 and org_currency == wanted
    if not org_rate and not all(wanted in by_currency for by_currency in roi.HOURLY_RATES.values()):
        wanted = DEFAULT_CURRENCY
    custom = {r.role: r for r in role_rates if r.currency.upper() == wanted and r.amount > 0}

    def resolve(role: str) -> roi.HourlyRate | None:
        if role in custom:
            r = custom[role]
            return roi.HourlyRate(role, wanted, float(r.amount), roi.LABEL_OVERRIDE, r.source or LABEL_ROLE_RATES)
        if org_rate:
            return roi.HourlyRate(
                role,
                wanted,
                float(loaded_hourly_rate or 0),
                roi.LABEL_OVERRIDE,
                "organization profile, loaded hourly rate",
            )
        return roi.HOURLY_RATES.get(role, {}).get(wanted)

    analyst = resolve(fallback_role)
    if analyst is None:  # the currency kept above always has a rate for every built-in role
        raise RuntimeError(f"no {fallback_role} rate in {wanted}")
    table: dict[str, dict[str, roi.HourlyRate]] = {}
    for role in set(roi.HOURLY_RATES) | set(custom) | set(roles):
        rate = resolve(role)
        if rate is None:
            rate = roi.HourlyRate(
                role,
                wanted,
                analyst.amount,
                analyst.label,
                f"no rate for role {role}, {fallback_role} rate: {analyst.source}",
            )
        table[role] = {wanted: rate}
    label = roi.LABEL_OVERRIDE if org_rate else "catalog default"
    if custom:
        label = f"{label}; {LABEL_ROLE_RATES}"
    return wanted, table, label


def build_report(
    rows: Iterable[RecordRow],
    *,
    window: str,
    since: datetime | None,
    currency: str,
    rates: Mapping[str, Mapping[str, roi.HourlyRate]],
    rate_label: str,
    baselines: Mapping[str, roi.Baseline] | None = None,
    overridden_units: Iterable[str] = (),
    agent_names: Mapping[uuid.UUID, str] | None = None,
    fleet_names: Mapping[uuid.UUID, str] | None = None,
    setup_cost: float = 0.0,
    period_days: int | None = None,
) -> RoiReport:
    """The report of ``rows``: the organization, then each agent, each fleet and each unit, highest net value first.

    ``period_days`` is the span of a calendar window (:func:`window_days`); a rolling window reads its own.
    """
    rows = list(rows)
    overridden = set(overridden_units)
    if period_days is None:
        period_days = roi.WINDOWS.get(window)

    def figures(measures: list[roi.RunMeasure], cost: float = 0.0) -> roi.RoiFigures:
        return roi.compute_roi(
            measures,
            currency=currency,
            baselines=baselines,
            rates=rates,
            period_days=period_days,
            setup_cost=cost,
            overridden_units=overridden,
        )

    by_agent: dict[uuid.UUID | None, list[roi.RunMeasure]] = {}
    by_fleet: dict[uuid.UUID, list[roi.RunMeasure]] = {}
    no_fleet: list[roi.RunMeasure] = []
    for row in rows:
        by_agent.setdefault(row.agent_id, []).append(row.measure)
        if row.fleet_id is not None:
            by_fleet.setdefault(row.fleet_id, []).append(row.measure)
        else:
            no_fleet.append(row.measure)
    agent_names = agent_names or {}
    fleet_names = fleet_names or {}
    agents = [
        ReportGroup(
            str(agent_id) if agent_id else None,
            agent_names.get(agent_id, "") if agent_id else "deleted agent",
            figures(measures),
        )
        for agent_id, measures in by_agent.items()
    ]
    fleets = [
        ReportGroup(str(fleet_id), fleet_names.get(fleet_id, "deleted fleet"), figures(measures))
        for fleet_id, measures in by_fleet.items()
    ]
    by_unit: dict[str, list[roi.RunMeasure]] = {}
    by_week: dict[datetime, list[roi.RunMeasure]] = {}
    for row in rows:
        by_unit.setdefault(row.measure.unit, []).append(row.measure)
        if row.at is not None:
            by_week.setdefault(week_start(row.at), []).append(row.measure)
    catalog = baselines if baselines is not None else roi.BASELINES_BY_UNIT
    units = [ReportGroup(unit, _unit_name(unit, catalog), figures(measures)) for unit, measures in by_unit.items()]
    agents.sort(key=lambda g: (-g.figures.net_value, g.name))
    fleets.sort(key=lambda g: (-g.figures.net_value, g.name))
    units.sort(key=lambda g: (-g.figures.net_value, g.name))
    trend = [TrendPoint(week, figures(measures)) for week, measures in sorted(by_week.items())]
    organization = figures([row.measure for row in rows], setup_cost)
    used = set(organization.units_by_kind)
    return RoiReport(
        window=window,
        since=since,
        currency=currency,
        rate_label=rate_label,
        organization=organization,
        agents=agents,
        fleets=fleets,
        estimated=not (used and used <= overridden),
        no_fleet=figures(no_fleet) if no_fleet else None,
        units=units,
        trend=trend,
    )


def _unit_name(unit: str, catalog: Mapping[str, roi.Baseline]) -> str:
    if unit == roi.COST_ONLY_UNIT:
        return roi.COST_ONLY_NAME
    return catalog[unit].name if unit in catalog else unit


async def price_rows(
    db: AsyncSession,
    rows: list[RecordRow],
    *,
    since: datetime | None,
    now: datetime | None = None,
    scope_agents: set[uuid.UUID] | None,
) -> list[RecordRow]:
    """*rows* priced with their agents' LLM spend in the window (#10441, :mod:`roi_spend`).

    *scope_agents* are the agents whose spend the report counts even without a record (a
    cost-only row each); ``None`` counts every agent that spent (the organization).
    """
    from sqlalchemy import func

    from app.models.roi import RoiRunRecord
    from app.services import roi_spend
    from app.services.fleets import home_fleets_of

    record_agents = {r.agent_id for r in rows if r.agent_id is not None}
    wanted = None if scope_agents is None else record_agents | scope_agents
    spend = await roi_spend.agent_spend(db, since=since, now=now, agent_ids=wanted)
    spend = await clip_spend_to_marks(db, spend, since=since, now=now, agent_ids=wanted)
    in_scope = set(spend) if scope_agents is None else scope_agents
    counts: dict[uuid.UUID, int] = {}
    if spend:
        query = select(RoiRunRecord.agent_id, func.count()).where(RoiRunRecord.agent_id.in_(list(spend)))
        if since is not None:
            query = query.where(RoiRunRecord.created_at >= since)
        query = query.where(fleet_value_marks.counted_clause(RoiRunRecord.created_at, RoiRunRecord.fleet_id))
        counts = {aid: int(n) for aid, n in (await db.execute(query.group_by(RoiRunRecord.agent_id))).all()}
    orphans = [a for a in in_scope if spend.get(a, 0.0) > 0 and not counts.get(a)]
    homes = {aid: home.fleet_id for aid, home in (await home_fleets_of(db, orphans)).items()}
    return roi_spend.allocate(
        rows,
        spend,
        records_per_agent=counts,
        scope_agents=in_scope,
        home_fleets=homes,
        make_row=lambda agent_id, fleet_id, measure: RecordRow(agent_id, fleet_id, measure),
    )


async def clip_spend_to_marks(
    db: AsyncSession,
    spend: dict[uuid.UUID, float],
    *,
    since: datetime | None,
    now: datetime | None,
    agent_ids: set[uuid.UUID] | None,
) -> dict[uuid.UUID, float]:
    """*spend* with the agents of a reset fleet read again from their fleet's mark (#2580).

    An agent's spend is its home fleet's (:mod:`roi_spend`); when that fleet carries a
    ``value_measured_since`` later than *since*, only the spend from the mark counts, so no
    pre-reset cost comes back as a cost-only row. One read per distinct mark.
    """
    from app.services import roi_spend

    marks = await fleet_value_marks.load_marks(db)
    if not marks:
        return spend
    candidates = set(spend) | (agent_ids or set())
    by_agent = await fleet_value_marks.agent_marks(db, candidates, marks)
    later = {aid: m for aid, m in by_agent.items() if since is None or m > since}
    if not later:
        return spend
    clipped = {aid: cost for aid, cost in spend.items() if aid not in later}
    for mark in sorted(set(later.values())):
        agents = [aid for aid, m in later.items() if m == mark]
        clipped.update(await roi_spend.agent_spend(db, since=mark, now=now, agent_ids=agents))
    return clipped


async def fleet_member_agents(db: AsyncSession, fleet_ids: Iterable[uuid.UUID]) -> set[uuid.UUID]:
    """The agents whose home fleet is one of *fleet_ids* (their spend is the fleet's, #10441)."""
    from app.models.fleet import FLEET_ROLE_MEMBER, FleetMember

    ids = list(set(fleet_ids))
    if not ids:
        return set()
    query = select(FleetMember.agent_id).where(
        FleetMember.fleet_id.in_(ids), FleetMember.role == FLEET_ROLE_MEMBER, FleetMember.agent_id.is_not(None)
    )
    return {aid for aid in (await db.execute(query)).scalars() if aid is not None}


def fold_outside(report: RoiReport, rows: Iterable[RecordRow], reader: Any, figures_of: Any) -> RoiReport:
    """Fold the agent and fleet rows outside *reader*'s scope into one "other" row each (#9836 R1).

    *reader* is a ``roi_access.RoiScope``; *figures_of* computes the figures of a list of
    measures the way the report did. The totals and the by-unit figures are untouched: a
    unit names no object. Rows of a reader of the organization are never folded.

    The "Agents outside a fleet" row (``no_fleet``) is folded too (Sam, 15 Nov 2026): it
    keeps only the fleetless records of agents in scope; a fleetless record of an agent
    outside it goes to "Other agents and fleets", so the fleet rows, ``no_fleet`` and
    ``other_fleets`` still sum to the totals.
    """
    if getattr(reader, "organization", False):
        return report
    rows = list(rows)
    kept_agents = [g for g in report.agents if g.id is not None and reader.holds_agent(uuid.UUID(g.id))]
    kept_fleets = [g for g in report.fleets if g.id is not None and reader.holds_fleet(uuid.UUID(g.id))]
    other_agent_measures = [r.measure for r in rows if not reader.holds_agent(r.agent_id)]
    no_fleet_measures = [r.measure for r in rows if r.fleet_id is None and reader.holds_agent(r.agent_id)]
    other_fleet_measures = [
        r.measure
        for r in rows
        if (r.fleet_id is not None and not reader.holds_fleet(r.fleet_id))
        or (r.fleet_id is None and not reader.holds_agent(r.agent_id))
    ]
    report.agents = kept_agents
    report.fleets = kept_fleets
    report.no_fleet = figures_of(no_fleet_measures) if no_fleet_measures else None
    report.other_agents = figures_of(other_agent_measures) if other_agent_measures else None
    report.other_fleets = figures_of(other_fleet_measures) if other_fleet_measures else None
    return report


async def load_report(
    db: AsyncSession,
    *,
    window: str = "30d",
    currency: str | None = None,
    now: datetime | None = None,
    scope: str = SCOPE_PLATFORM,
    scope_id: uuid.UUID | None = None,
    reader: Any = None,
) -> RoiReport:
    """The report over ``window`` from the stored records: every record, or one agent's or one fleet's.

    A fleet's records are those whose home fleet at the run's end was that
    fleet (``roi_run_records.fleet_id``). ``ValueError`` on an unknown scope
    or a scoped report without its id.

    *reader* (a ``roi_access.RoiScope``, #9836 R1) is what the caller reads:
    ``scope=mine`` reports the records whose agent or fleet it holds, each
    record once; ``scope=platform`` for a reader that does not hold the
    organization keeps the totals and folds the rows outside it
    (:func:`fold_outside`). ``scope=mine`` needs it.
    """
    from app.models.agent import Agent
    from app.models.fleet import Fleet
    from app.models.roi import RoiRunRecord

    if scope not in SCOPES:
        raise ValueError(f"scope {scope!r} is not one of {', '.join(SCOPES)}")
    if scope not in (SCOPE_PLATFORM, SCOPE_MINE) and scope_id is None:
        raise ValueError(f"a {scope} report needs its id")
    if scope == SCOPE_MINE and reader is None:
        raise ValueError("a mine report needs its reader")
    since = window_since(window, now=now)
    query = select(
        RoiRunRecord.agent_id,
        RoiRunRecord.fleet_id,
        RoiRunRecord.unit,
        RoiRunRecord.units,
        RoiRunRecord.llm_cost,
        RoiRunRecord.duration_s,
        RoiRunRecord.outcome,
        RoiRunRecord.review_min,
        RoiRunRecord.work_item_id,
        RoiRunRecord.created_at,
        RoiRunRecord.backfilled_at,
    )
    if since is not None:
        query = query.where(RoiRunRecord.created_at >= since)
    # A fleet's value is measured from its last factory reset (#2580): its earlier records are left out.
    query = query.where(fleet_value_marks.counted_clause(RoiRunRecord.created_at, RoiRunRecord.fleet_id))
    if scope == SCOPE_AGENT:
        query = query.where(RoiRunRecord.agent_id == scope_id)
    elif scope == SCOPE_FLEET:
        query = query.where(RoiRunRecord.fleet_id == scope_id)
    elif scope == SCOPE_MINE and not reader.organization:
        from sqlalchemy import false, or_

        held = [
            clause
            for clause in (
                RoiRunRecord.agent_id.in_(reader.agent_ids) if reader.agent_ids else None,
                RoiRunRecord.fleet_id.in_(reader.fleet_ids) if reader.fleet_ids else None,
            )
            if clause is not None
        ]
        # One record, one row: a record whose agent and fleet are both in scope is selected once.
        query = query.where(or_(*held) if held else false())
    fetched = (await db.execute(query)).all()
    backfilled = [row[-1] for row in fetched if row[-1] is not None]
    rows = [
        RecordRow(
            agent_id,
            fleet_id,
            roi.RunMeasure(
                unit=unit,
                units=int(units or 0),
                llm_cost=float(llm_cost or 0),
                duration_s=float(duration_s or 0),
                outcome=outcome,
                review_min=review_min,
                work_item=work_item_id is not None,
            ),
            created_at,
        )
        for agent_id, fleet_id, unit, units, llm_cost, duration_s, outcome, review_min, work_item_id, created_at, _ in (
            fetched
        )
    ]
    # The AI cost is the agents' LLM spend in the window, not the cost of the recorded runs (#10441).
    scope_agents: set[uuid.UUID] | None = None
    if scope == SCOPE_AGENT and scope_id is not None:
        scope_agents = {scope_id}
    elif scope == SCOPE_FLEET and scope_id is not None:
        scope_agents = await fleet_member_agents(db, [scope_id])
    elif scope == SCOPE_MINE and not reader.organization:
        scope_agents = set(reader.agent_ids) | await fleet_member_agents(db, reader.fleet_ids)
    rows = await price_rows(db, rows, since=since, now=now, scope_agents=scope_agents)
    period_days = window_days(window, now=now)
    agent_ids = {r.agent_id for r in rows if r.agent_id}
    fleet_ids = {r.fleet_id for r in rows if r.fleet_id}
    if scope == SCOPE_AGENT and scope_id is not None:
        agent_ids.add(scope_id)
    elif scope == SCOPE_FLEET and scope_id is not None:
        fleet_ids.add(scope_id)
    agent_names: dict[uuid.UUID, str] = {}
    fleet_names: dict[uuid.UUID, str] = {}
    if agent_ids:
        agent_rows = (await db.execute(select(Agent.id, Agent.name).where(Agent.id.in_(agent_ids)))).all()
        agent_names = {aid: name for aid, name in agent_rows}
    if fleet_ids:
        fleet_rows = (await db.execute(select(Fleet.id, Fleet.name).where(Fleet.id.in_(fleet_ids)))).all()
        fleet_names = {fid: name for fid, name in fleet_rows}
    pricing = await load_pricing(db, currency)
    baselines, overridden = pricing.baselines, pricing.overridden
    used_currency, rates, rate_label = pricing.currency, pricing.rates, pricing.rate_label
    report = build_report(
        rows,
        window=window,
        since=since,
        currency=used_currency,
        rates=rates,
        rate_label=rate_label,
        baselines=baselines,
        overridden_units=overridden,
        agent_names=agent_names,
        fleet_names=fleet_names,
        period_days=period_days,
    )
    if scope == SCOPE_PLATFORM and reader is not None:

        def figures_of(measures: list[roi.RunMeasure]) -> roi.RoiFigures:
            return roi.compute_roi(
                measures,
                currency=used_currency,
                baselines=baselines,
                rates=rates,
                period_days=period_days,
                overridden_units=overridden,
            )

        fold_outside(report, rows, reader, figures_of)
    # Cost masking applies identically to every scope and every reader (#12077, invariant 6): the
    # administrator's platform report, an agent's and a fleet's mask as `mine` does, so `cost_masked`
    # says what the ledger figures say and no surface draws a net value the setting hides.
    report.cost_masked = not settings.observability_cost_data_enabled
    report.scope = scope
    report.cost_source = COST_SOURCE
    report.unallocated_cost = unallocated(rows)
    report.backfilled_records = len(backfilled)
    report.backfilled_at = max(backfilled) if backfilled else None
    marks = await fleet_value_marks.load_marks(db)
    first_day = since.date() if since is not None else None
    if scope == SCOPE_FLEET and scope_id is not None:
        mark = marks.get(scope_id)
        report.value_measured_since = mark if fleet_value_marks.in_window(mark, first_day) else None
    else:
        report.reset_fleets = sum(1 for m in marks.values() if fleet_value_marks.in_window(m, first_day))
    if scope_id is not None and scope not in (SCOPE_PLATFORM, SCOPE_MINE):
        report.scope_id = str(scope_id)
        names = agent_names if scope == SCOPE_AGENT else fleet_names
        report.scope_name = names.get(scope_id, f"deleted {scope}")
    return report


@dataclass(frozen=True)
class Pricing:
    """What a report prices its records with: the catalog as the organization edited it, and the rates."""

    baselines: dict[str, roi.Baseline]
    overridden: set[str]
    currency: str
    rates: dict[str, dict[str, roi.HourlyRate]]
    rate_label: str


async def load_pricing(db: AsyncSession, currency: str | None) -> Pricing:
    """The organization's baselines, overrides and rates in *currency* (:func:`rates_for`), read once.

    The one pricing of :func:`load_report` and of the ledger (``roi_ledger``), so a figure never
    differs between the scoped report and the list column of the same subject and window.
    """
    from app.services.organization_context import current_version, parse_profile
    from app.services.roi_records import effective_baselines, list_rates

    baselines, overridden = await effective_baselines(db)
    version = await current_version(db)
    profile = parse_profile(version.profile if version else {})
    role_rates = [RoleRate(r.role, r.currency, float(r.amount), r.source) for r in await list_rates(db)]
    used_currency, rates, rate_label = rates_for(
        currency,
        loaded_hourly_rate=profile.loaded_hourly_rate,
        org_currency=profile.currency,
        role_rates=role_rates,
        roles={b.rate_role for b in baselines.values()},
        fallback_role=roi.fallback_rate_role(xtm_one=settings.is_xtm_one),
    )
    return Pricing(baselines, set(overridden), used_currency, rates, rate_label)


#: The most ids one ``GET /roi/summary`` answers: one page of a list (the list column, design 10.8).
SUMMARY_MAX_IDS = 200


async def declared_subjects(db: AsyncSession, subject_type: str, ids: list[uuid.UUID]) -> set[uuid.UUID]:
    """The ids of *ids* that declare a unit: a declaration row, a built-in agent's default, a legacy
    ``x-xtm-roi``; a fleet also when one of its agent members declares one (its runs count it).

    The list column (``GET /roi/summary``) shows a declared object's zero figures, and "-" for one
    that declares nothing and counts nothing.
    """
    from app.agents.builtin_roi_units import BUILTIN_ROI_UNITS
    from app.models.agent import Agent
    from app.models.fleet import FleetMember
    from app.models.roi import RoiDeclaration

    if not ids:
        return set()

    async def agents_declaring(agent_ids: set[uuid.UUID]) -> set[uuid.UUID]:
        if not agent_ids:
            return set()
        rows = (
            await db.execute(
                select(RoiDeclaration.subject_id).where(
                    RoiDeclaration.subject_type == SCOPE_AGENT, RoiDeclaration.subject_id.in_(agent_ids)
                )
            )
        ).scalars()
        found = set(rows)
        agents = (
            await db.execute(
                select(Agent.id, Agent.is_builtin, Agent.slug, Agent.output_schema).where(Agent.id.in_(agent_ids))
            )
        ).all()
        for aid, builtin, slug, schema in agents:
            if (builtin and (slug or "") in BUILTIN_ROI_UNITS) or roi.unit_declaration(schema) is not None:
                found.add(aid)
        return found

    if subject_type == SCOPE_AGENT:
        return await agents_declaring(set(ids))
    own = set(
        (
            await db.execute(
                select(RoiDeclaration.subject_id).where(
                    RoiDeclaration.subject_type == SCOPE_FLEET, RoiDeclaration.subject_id.in_(ids)
                )
            )
        ).scalars()
    )
    members = (
        await db.execute(
            select(FleetMember.fleet_id, FleetMember.agent_id).where(
                FleetMember.fleet_id.in_(ids), FleetMember.agent_id.is_not(None)
            )
        )
    ).all()
    declaring = await agents_declaring({aid for _fid, aid in members})
    return own | {fid for fid, aid in members if aid in declaring}


async def record_details(
    db: AsyncSession,
    subject_type: str,
    ids: list[uuid.UUID],
    *,
    window: str = "30d",
    currency: str | None = None,
    now: datetime | None = None,
) -> dict[uuid.UUID, dict[str, Any]]:
    """What the hero of each listed agent or fleet keeps from its records beside the ledger (#12077 L7a UX r1 B1).

    Per id: the ``rejected`` records, the ``work_items`` among them (and those nobody reviewed),
    and the ``assumption_facts`` (unit minutes and hourly rates) of the units its counted records
    used. They are the counts and inputs the agent page reads from ``GET /roi?scope=agent|fleet``
    (:func:`load_report`'s ``organization``: the same records of the window, an agent's by
    ``agent_id``, a fleet's by ``fleet_id``), so the list column's "How this is computed" says what
    the agent page says. One read for the page; an id with no record answers zeros.
    """
    from app.models.roi import RoiRunRecord
    from app.services import roi_export

    if subject_type not in (SCOPE_AGENT, SCOPE_FLEET):
        raise ValueError(f"subject_type {subject_type!r} is not agent or fleet")
    if not ids:
        return {}
    key = RoiRunRecord.agent_id if subject_type == SCOPE_AGENT else RoiRunRecord.fleet_id
    query = select(key, RoiRunRecord.unit, RoiRunRecord.units, RoiRunRecord.outcome, RoiRunRecord.work_item_id).where(
        key.in_(ids)
    )
    since = window_since(window, now=now)
    if since is not None:
        query = query.where(RoiRunRecord.created_at >= since)
    query = query.where(fleet_value_marks.counted_clause(RoiRunRecord.created_at, RoiRunRecord.fleet_id))
    pricing = await load_pricing(db, currency)
    counts = {sid: {"rejected": 0, "work_items": 0, "unreviewed_work_items": 0} for sid in ids}
    used: dict[uuid.UUID, set[str]] = {sid: set() for sid in ids}
    for sid, unit, units, outcome, work_item_id in (await db.execute(query)).all():
        if sid not in counts:
            continue
        line = counts[sid]
        if work_item_id is not None:
            line["work_items"] += 1
            if outcome is None:
                line["unreviewed_work_items"] += 1
        if outcome == roi.OUTCOME_REJECTED:
            line["rejected"] += 1
        elif int(units or 0) > 0 and unit in pricing.baselines:
            used[sid].add(unit)
    return {
        sid: {**counts[sid], "assumption_facts": roi_export.assumption_facts(used[sid], pricing=pricing)} for sid in ids
    }


def figures_dict(f: roi.RoiFigures) -> dict[str, Any]:
    """The JSON shape of one group's figures."""
    return {
        "currency": f.currency,
        "runs": f.runs,
        "units": f.units,
        "units_by_kind": dict(f.units_by_kind),
        "minutes_saved": f.minutes_saved,
        "hours_saved": f.hours_saved,
        "llm_cost": f.llm_cost,
        "value": f.value,
        "net_value": f.net_value,
        "roi": f.roi,
        "payback_months": f.payback_months,
        # The low / high range of every estimated figure (#9836 R4a, design 10.5).
        "range": {
            "minutes_saved": [f.minutes_saved_low, f.minutes_saved_high],
            "hours_saved": [f.hours_saved_low, f.hours_saved_high],
            "value": [f.value_low, f.value_high],
            "net_value": [f.net_value_low, f.net_value_high],
            "roi": [f.roi_low, f.roi_high],
            "payback_months": [f.payback_months_low, f.payback_months_high],
        },
        "reviewed": f.reviewed,
        "unreviewed": f.unreviewed,
        "rejected": f.rejected,
        "work_items": f.work_items,
        "unreviewed_work_items": f.unreviewed_work_items,
        "review_rate": f.review_rate,
        "assumptions": list(f.assumptions),
        "assumption_facts": [dict(fact) for fact in f.assumption_facts],
        "computation": [dict(fact) for fact in f.computation],
    }


#: The figures a reader whose costs are masked never sees (#9836 R1): ``null``, never 0.
COST_FIELDS = ("llm_cost", "net_value", "roi", "payback_months")


def _masked(figures: dict[str, Any]) -> dict[str, Any]:
    """*figures* with every cost figure ``null``, the computation's total included."""
    out = {**figures, **dict.fromkeys(COST_FIELDS)}
    if "range" in figures:
        out["range"] = {**figures["range"], **{key: [None, None] for key in COST_FIELDS if key in figures["range"]}}
    out["computation"] = [
        {**step, "llm_cost": None, "net_value": None} if step.get("kind") == "total" else step
        for step in figures.get("computation", [])
    ]
    return out


def report_dict(report: RoiReport) -> dict[str, Any]:
    """The JSON shape of the report the API answers; the cost figures ``null`` when ``cost_masked``."""
    shape = _report_shape(report)
    if not report.cost_masked:
        return shape
    for key in ("organization", "no_fleet", "other_agents", "other_fleets"):
        if shape[key] is not None:
            shape[key] = _masked(shape[key])
    shape["agents"] = [_masked(g) for g in shape["agents"]]
    shape["fleets"] = [_masked(g) for g in shape["fleets"]]
    shape["units"] = [_masked(g) for g in shape["units"]]
    shape["trend"] = [{**p, "net_value": None, "net_value_range": [None, None]} for p in shape["trend"]]
    return shape


#: The figures of one row of the agent tool's compact report (#10627).
COMPACT_ROW_FIELDS = ("runs", "units", "hours_saved", "value", "llm_cost", "net_value", "roi", "unreviewed")
#: The figures every group repeats: the compact report writes them once, the organization's.
SHARED_FIGURES = ("assumptions", "assumption_facts", "computation")
#: The shared figures the compact report keeps (#10664): the prose ``assumptions`` repeat
#: ``assumption_facts`` line for line, so only the facts travel; the prose is the agent or fleet report.
COMPACT_SHARED_FIGURES = ("assumption_facts", "computation")
#: The keys a compact ``saving`` step leaves out: the unit's name is its ``unit`` fact, the currency the report's.
COMPACT_STEP_DROPPED = ("name", "currency")
#: Rows per list of the compact report unless the caller asks for more.
COMPACT_DEFAULT_LIMIT = 20


def _compact_row(group: dict[str, Any]) -> dict[str, Any]:
    row = {key: group[key] for key in ("id", "name") if key in group}
    return {**row, **{key: group.get(key) for key in COMPACT_ROW_FIELDS}}


def _compact_facts(facts: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[str]]:
    """*facts* with each ``source`` string written once in a list and referenced by its index (#10664)."""
    sources: list[str] = []
    out: list[dict[str, Any]] = []
    for fact in facts:
        source = fact.get("source")
        if not isinstance(source, str):
            out.append(dict(fact))
            continue
        if source not in sources:
            sources.append(source)
        out.append({**{k: v for k, v in fact.items() if k != "source"}, "source_ref": sources.index(source)})
    return out, sources


def _compact_steps(steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The computation with the per-unit ``saving`` steps stripped of what the report already says."""
    return [
        {k: v for k, v in step.items() if k not in COMPACT_STEP_DROPPED} if step.get("kind") == "saving" else step
        for step in steps
    ]


def _idle(group: dict[str, Any]) -> bool:
    return not group.get("runs") and not group.get("units")


def _compact_page(groups: list[dict[str, Any]], *, include_idle: bool, limit: int, offset: int) -> dict[str, Any]:
    """One page of compact rows, highest value first; idle groups counted, listed only on request."""
    kept = groups if include_idle else [g for g in groups if not _idle(g)]
    kept = sorted(kept, key=lambda g: (-(g.get("value") or 0.0), g.get("name") or ""))
    rows = [_compact_row(g) for g in kept[offset : offset + limit]]
    remaining = max(0, len(kept) - offset - len(rows))
    return {
        "rows": rows,
        "total": len(kept),
        "idle_not_listed": 0 if include_idle else len(groups) - len(kept),
        "remaining": remaining,
        "next_offset": offset + len(rows) if remaining else None,
    }


def compact_report_dict(
    report: RoiReport, *, include_idle: bool = False, limit: int = COMPACT_DEFAULT_LIMIT, offset: int = 0
) -> dict[str, Any]:
    """The report an agent tool answers on a wide scope (#10627): bounded whatever the number of agents.

    :func:`report_dict` repeats ``assumptions``, ``assumption_facts`` and
    ``computation`` on every agent, fleet and unit and lists idle agents, so a
    platform with a hundred agents answers past the tool cap. This projection
    keeps the totals (the organization's figures, unchanged), the trend and
    the shared figures once, then pages compact rows of the agents, the
    fleets and the units by value; a group with no run and no unit is counted
    in ``idle_not_listed`` and listed with ``include_idle``. A group's own
    computation is the agent or fleet report.

    Under 20,000 characters for 15 units, 6 rates and 20 agent rows (#10664):
    the structured ``assumption_facts`` travel without their prose twin
    ``assumptions``, each distinct ``source`` once in ``sources`` (a fact
    carries ``source_ref``, its index), and a ``saving`` step of the
    computation without the unit name and currency the report already gives.
    """
    shape = report_dict(report)
    organization = shape["organization"]
    out = {key: value for key, value in shape.items() if key not in ("organization", "agents", "fleets", "units")}
    out["totals"] = {key: value for key, value in organization.items() if key not in SHARED_FIGURES}
    out["assumption_facts"], out["sources"] = _compact_facts(organization.get("assumption_facts") or [])
    out["computation"] = _compact_steps(organization.get("computation") or [])
    for key in ("no_fleet", "other_agents", "other_fleets"):
        if shape[key] is not None:
            out[key] = _compact_row(shape[key])
    for key in ("agents", "fleets", "units"):
        out[key] = _compact_page(shape[key], include_idle=include_idle, limit=limit, offset=offset)
    return out


def _report_shape(report: RoiReport) -> dict[str, Any]:
    return {
        "scope": report.scope,
        "scope_id": report.scope_id,
        "scope_name": report.scope_name,
        "window": report.window,
        "since": report.since.isoformat() if report.since else None,
        "currency": report.currency,
        "rate_label": report.rate_label,
        "estimated": report.estimated,
        "organization": figures_dict(report.organization),
        "agents": [{"id": g.id, "name": g.name, **figures_dict(g.figures)} for g in report.agents],
        "fleets": [{"id": g.id, "name": g.name, **figures_dict(g.figures)} for g in report.fleets],
        "no_fleet": figures_dict(report.no_fleet) if report.no_fleet else None,
        "other_agents": figures_dict(report.other_agents) if report.other_agents else None,
        "other_fleets": figures_dict(report.other_fleets) if report.other_fleets else None,
        "cost_masked": report.cost_masked,
        # Where the AI cost comes from and what no unit carries (#10441): the Value page's footnote.
        "cost_source": report.cost_source,
        "cost_source_label": COST_SOURCE_LABEL if report.cost_source == COST_SOURCE else None,
        "unallocated_cost": None if report.cost_masked else report.unallocated_cost,
        # A fleet's value measured since its factory reset (#2580): "Measured since <date> (reset)".
        "value_measured_since": report.value_measured_since.isoformat() if report.value_measured_since else None,
        "reset_fleets": report.reset_fleets,
        # The Value page (#9836 R5a, design 10.7): the rows of "By unit of work" and the weekly trend.
        "units": [{"id": g.id, "name": g.name, **figures_dict(g.figures)} for g in report.units],
        "trend": [
            {
                "week": p.week.date().isoformat(),
                "value": p.figures.value,
                "value_range": [p.figures.value_low, p.figures.value_high],
                "net_value": p.figures.net_value,
                "net_value_range": [p.figures.net_value_low, p.figures.net_value_high],
                "runs": p.figures.runs,
            }
            for p in report.trend
        ],
        # "N records backfilled on <date>" in How this is computed (#10006).
        "backfill": {
            "records": report.backfilled_records,
            "last_at": report.backfilled_at.isoformat() if report.backfilled_at else None,
        },
    }


_CSV_COLUMNS = (
    "scope",
    "id",
    "name",
    "runs",
    "units",
    "hours_saved",
    "llm_cost",
    "value",
    "net_value",
    "roi",
    "payback_months",
    "currency",
)


def _cell(value: Any) -> Any:
    """A CSV cell a spreadsheet never reads as a formula."""
    if isinstance(value, str) and value[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + value
    return "" if value is None else value


#: What a masked cost reads in an export (#9836 R7a): never 0, never a blank that reads as nothing spent.
HIDDEN_COST = "Hidden by your administrator"

#: The model in one line, carried by every export (#9836 R7a, design 10.6); the long form is
#: ``dev-docs/roi-methodology.md`` and the product's methodology page.
METHODOLOGY = (
    "Value = units x (manual minutes - review minutes) x hourly rate; "
    "net value = value - AI cost; ROI = net value / AI cost; "
    "payback = setup cost / net value per month. "
    "Each input is labelled assumption, published estimate, measured or customized, with its source. "
    "Rejected outcomes count no unit; unreviewed runs are counted and shown apart. "
    "AI cost is the LLM cost of the scope's agents in the window, as LLM Analytics reports it, "
    "spread over their recorded units; the spend no unit carries is its own row. "
    "Figures are computed from recorded runs and stored traces; no model is called."
)

#: The columns of the assumptions section of the CSV.
_CSV_ASSUMPTION_COLUMNS = ("assumption", "kind", "name", "value", "label", "source")


def assumption_rows(figures: roi.RoiFigures) -> list[tuple[str, str, str, str, str]]:
    """The inputs a figure rests on, one ``(kind, name, value, label, source)`` row per assumption fact."""
    return assumption_rows_of(figures.assumption_facts)


def assumption_rows_of(facts: Iterable[Mapping[str, Any]]) -> list[tuple[str, str, str, str, str]]:
    """One ``(kind, name, value, label, source)`` row per assumption fact (``roi._assumption_facts`` shape).

    Shared by the report's exports and the ledger's (``roi_export``, #12077 slice L4c).
    """
    rows: list[tuple[str, str, str, str, str]] = []
    for fact in facts:
        kind = fact.get("kind")
        if kind == "unit":
            value = f"{fact['manual_min']:g} manual min"
            low, high = fact.get("manual_min_low"), fact.get("manual_min_high")
            if low is not None and high is not None and (low, high) != (fact["manual_min"], fact["manual_min"]):
                value += f" ({low:g} to {high:g})"
            value += f", {fact['review_min']:g} review min per unit"
            rows.append(("unit", str(fact["name"]), value, str(fact.get("label") or ""), str(fact.get("source") or "")))
        elif kind == "rate":
            rows.append(
                (
                    "rate",
                    str(fact["role"]),
                    f"{fact['amount']:g} {fact['currency']}/h",
                    str(fact.get("label") or ""),
                    str(fact.get("source") or ""),
                )
            )
        elif kind == "measured_review":
            rows.append(
                ("review", "review minutes", "measured where recorded, baseline otherwise", roi.LABEL_MEASURED, "")
            )
        elif kind == "unknown_units":
            rows.append(
                ("unknown_units", ", ".join(fact.get("units", [])), "no baseline: cost counted, no time saved", "", "")
            )
    return rows


def report_csv(report: RoiReport) -> str:
    """The report as CSV: one line for the organization, then one per fleet and per agent.

    After the figures, a blank line, the ``methodology`` line and the ``assumptions`` section
    (#9836 R7a): every input the figures rest on with its label and source. A masked cost
    (``cost_masked``) reads :data:`HIDDEN_COST`, never a number.
    """
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(_CSV_COLUMNS)
    masked = report.cost_masked

    def line(scope: str, gid: str | None, name: str, f: roi.RoiFigures) -> None:
        writer.writerow(
            [
                _cell(v)
                for v in (
                    scope,
                    gid,
                    name,
                    f.runs,
                    f.units,
                    f.hours_saved,
                    HIDDEN_COST if masked else f.llm_cost,
                    f.value,
                    HIDDEN_COST if masked else f.net_value,
                    HIDDEN_COST if masked else f.roi,
                    HIDDEN_COST if masked else f.payback_months,
                    f.currency,
                )
            ]
        )

    if report.scope == SCOPE_PLATFORM:
        line("organization", None, "organization", report.organization)
    else:  # the total of a scoped report is its agent or fleet
        line(f"{report.scope}_total", report.scope_id, report.scope_name or "", report.organization)
    for g in report.fleets:
        line("fleet", g.id, g.name, g.figures)
    for g in report.agents:
        line("agent", g.id, g.name, g.figures)
    writer.writerow([])
    writer.writerow(["methodology", METHODOLOGY])
    writer.writerow([])
    writer.writerow(_CSV_ASSUMPTION_COLUMNS)
    for row in assumption_rows(report.organization):
        writer.writerow([_cell(v) for v in ("assumption", *row)])
    return out.getvalue()


def _md(text: str) -> str:
    """Text safe inside a Markdown table cell."""
    return text.replace("|", "\\|").replace("\n", " ").replace("<", "&lt;").replace(">", "&gt;")


def _money(amount: float, currency: str) -> str:
    return f"{amount:,.2f} {currency}"


def _cost(amount: float | None, currency: str, *, masked: bool) -> str:
    """A cost cell: :data:`HIDDEN_COST` when costs are masked, never 0."""
    if masked:
        return HIDDEN_COST
    return "n/a" if amount is None else _money(amount, currency)


def _payback_words(months: float | None) -> str:
    if months is None:
        return "not reached in this window"
    if months < 1:
        return "under a month"
    return f"{months:g} months"


def methodology_markdown(report: RoiReport) -> list[str]:
    """The last page of the value report (#9836 R7a, design 10.6): the formula with the report's
    own numbers, the inputs with their label and source, and what is counted apart."""
    o = report.organization
    masked = report.cost_masked
    total = next((s for s in o.computation if s.get("kind") == "total"), None)
    lines = [
        '<div style="page-break-before: always"></div>',
        "",
        "## How these figures are computed",
        "",
        "- Value = units x (manual minutes - review minutes) x hourly rate",
    ]
    for step in o.computation:
        if step.get("kind") == "saving":
            lines.append(
                # manual_min and review_min of a saving step are the unit's totals (units x per unit).
                f"    - {_md(str(step.get('name') or step.get('unit')))}: {step['units']} units, "
                f"({step['manual_min']:g} - {step['review_min']:g}) min = {step['minutes_saved']:g} min "
                f"at {step['rate']:g} {o.currency}/h = {_money(step['value'], o.currency)}"
            )
    if total is not None:
        lines.append(f"    - Total: {o.hours_saved:g} h saved = {_money(o.value, o.currency)}")
    if masked:
        lines += [
            f"- Net value = value - AI cost: {HIDDEN_COST}",
            f"- ROI = net value / AI cost: {HIDDEN_COST}",
            f"- Payback = setup cost / net value per month: {HIDDEN_COST}",
        ]
    else:
        lines += [
            f"- Net value = value - AI cost = {_money(o.value, o.currency)} - {_money(o.llm_cost, o.currency)} "
            f"= {_money(o.net_value, o.currency)}",
            f"- ROI = net value / AI cost = {'n/a' if o.roi is None else f'{o.roi:g}x'}",
            f"- Payback = setup cost / net value per month = {_payback_words(o.payback_months)}",
        ]
    lines += ["", f"Counted: {o.units} units from {o.runs} runs."]
    apart = []
    if o.rejected:
        apart.append(f"{o.rejected} rejected (no unit counted)")
    if o.unreviewed:
        apart.append(f"{o.unreviewed} unreviewed, counted and shown apart")
    if apart:
        lines.append("Not counted as reviewed: " + "; ".join(apart) + ".")
    lines.append("")
    rows = assumption_rows(o)
    if rows:
        lines += ["## Assumptions", "", "| Input | Value | Label | Source |", "|---|---|---|---|"]
        lines += [
            f"| {_md(name)} | {_md(value)} | {_md(label)} | {_md(source) or '-'} |"
            for _kind, name, value, label, source in rows
        ]
        lines.append("")
    lines += [f"_{METHODOLOGY}_", ""]
    return lines


def report_markdown(
    report: RoiReport, *, generated_at: datetime | None = None, top: int = 8, title: str = REPORT_TITLE
) -> str:
    """The one-page value report as Markdown (the PDF source), under ``title`` (:func:`report_title`).

    A scoped report names its agent or fleet under the title.
    """
    o = report.organization
    if report.since is None:
        period = "since the start"
    elif report.window == WINDOW_QUARTER:
        period = f"this quarter (since {report.since.date().isoformat()})"
    else:
        period = f"the last {roi.WINDOWS[report.window]} days"
    when = (generated_at or datetime.now(UTC)).strftime("%Y-%m-%d")
    label = "estimated" if report.estimated else "organization baselines"
    masked = report.cost_masked
    heading = [f"# {title}", ""]
    if report.scope != SCOPE_PLATFORM and report.scope_name:
        heading += [f"**{report.scope.capitalize()}: {_md(report.scope_name)}**", ""]
    lines = [
        *heading,
        f"_{period.capitalize()}, generated {when}. Figures {label}; rate: {report.rate_label}._",
        "",
        "| Hours saved | Value | Cost of the agentic work | Net value | ROI |",
        "|---|---|---|---|---|",
        f"| {o.hours_saved:g} h | {_money(o.value, o.currency)} | {_cost(o.llm_cost, o.currency, masked=masked)} "
        f"| {_cost(o.net_value, o.currency, masked=masked)} "
        f"| {HIDDEN_COST if masked else ('n/a' if o.roi is None else f'{o.roi:g}x')} |",
        "",
        f"{o.runs} runs produced {o.units} units of work.",
        "",
    ]
    if report.fleets:
        lines += ["## By fleet", "", "| Fleet | Units | Hours saved | Net value |", "|---|---|---|---|"]
        lines += [
            f"| {_md(g.name)} | {g.figures.units} | {g.figures.hours_saved:g} "
            f"| {_cost(g.figures.net_value, o.currency, masked=masked)} |"
            for g in report.fleets[:top]
        ]
        lines.append("")
    if report.agents:
        lines += ["## By agent", "", "| Agent | Units | Hours saved | Net value |", "|---|---|---|---|"]
        lines += [
            f"| {_md(g.name)} | {g.figures.units} | {g.figures.hours_saved:g} "
            f"| {_cost(g.figures.net_value, o.currency, masked=masked)} |"
            for g in report.agents[:top]
        ]
        lines.append("")
    lines += methodology_markdown(report)
    return "\n".join(lines).rstrip() + "\n"
