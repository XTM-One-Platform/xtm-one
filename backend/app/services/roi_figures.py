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

"""The figures of the ROI ledger, computed from facts alone (issue #12077, slice L3).

Design: ``dev-docs/roi-outcomes.md`` sections 12.3 to 12.7. Every fact has one
shape (:class:`LedgerFact`): a value fact (a run or work item record, a chat
deliverable) carries its minutes and value, already priced; a cost fact (a row
of ``roi_cost_facts``, or of the live read of an open day) carries its cost.
:func:`compute` selects the facts of a scope for a viewer and sums them: the
hero, then one breakdown per dimension, every fact in exactly one row of each,
so each breakdown sums to the hero.

Nothing here reads the database: ``roi_ledger.figures`` loads the facts and
calls :func:`compute`, so the invariant suite (``tests/test_roi_ledger_invariants.py``)
runs on seeded facts with no PostgreSQL.

Scopes (12.4): ``mine`` is the viewer's own facts, every agent included;
``agent`` and ``fleet`` every person's facts of that object; ``organization``
every fact, the same totals for every viewer. A non-administrator's
organization and fleet views name only the agents and fleets he sees; the rest
are one "Other agents and fleets" row. "No person" is a row of By person only
(administrators, setting on).

k-anonymity (12.5, slice L5): for a non-administrator, on the organization,
agent and fleet scopes, every row of a breakdown backed by fewer than
``Viewer.min_group_size`` distinct people is folded into its breakdown's
"Other" row, with a complementary fold so no row is recoverable by
subtraction; an under-k week is merged with the next; an agent or fleet backed
by fewer than k people answers the viewer's own share only. Mine and every
administrator's answer are never suppressed.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Hashable, Iterable
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

SCOPE_MINE = "mine"
SCOPE_AGENT = "agent"
SCOPE_FLEET = "fleet"
SCOPE_ORGANIZATION = "organization"
#: ``platform`` stays an alias of ``organization`` for one release (12.4).
SCOPE_PLATFORM_ALIAS = "platform"
SCOPES: tuple[str, ...] = (SCOPE_MINE, SCOPE_AGENT, SCOPE_FLEET, SCOPE_ORGANIZATION)

CLASS_AUTOMATION = "automation"
CLASS_CHAT = "chat"
CLASS_PLATFORM = "platform"
#: The rows of By source, in their order (12.6).
CLASSES: tuple[str, ...] = (CLASS_AUTOMATION, CLASS_CHAT, CLASS_PLATFORM)

#: Residual and folded row keys (12.4). A residual row holds the facts that lack the
#: breakdown's own dimension; the folded row the objects the viewer does not see.
ROW_NO_AGENT = "no_agent"
ROW_NO_FLEET = "no_fleet"
ROW_NO_UNIT = "no_unit"
ROW_NO_PERSON = "no_person"
ROW_OTHER = "other"
#: The suppressed rows of By source and By unit (12.4, last column); By agent and By fleet
#: fold their under-k rows into ``ROW_OTHER`` ("Other agents and fleets").
ROW_OTHER_SOURCES = "other_sources"
ROW_OTHER_UNITS = "other_units"

#: What a hero counts (#2639), answered as ``basis`` so every surface states its scope.
BASIS_FLEET_MEMBERS = "fleet_members"
BASIS_AGENT = "agent"
BASIS_MINE = "mine"
BASIS_ORGANIZATION = "organization"

#: The bounds and default of ``PlatformSettings.roi_min_group_size`` (12.5).
MIN_GROUP_SIZE_DEFAULT = 5
MIN_GROUP_SIZE_MIN = 2
MIN_GROUP_SIZE_MAX = 50


@dataclass(frozen=True)
class LedgerFact:
    """One fact of the ledger: a value fact (minutes and value) or a cost fact (cost), never both.

    ``person_id`` is ``None`` for a fact that names nobody (``system``,
    ``anonymous_web``, ``unknown``, or not attributed yet: ``attribution`` ``None``).
    """

    day: date
    person_id: uuid.UUID | None
    attribution: str | None
    agent_id: uuid.UUID | None
    fleet_id: uuid.UUID | None
    cost_class: str
    unit: str | None = None
    #: A run or work item record (1), else 0; ``reviewed`` the records a person reviewed.
    runs: int = 0
    reviewed: int = 0
    #: The units of work a run counts, as ``roi.compute_roi`` counts them (none rejected, known unit).
    units: int = 0
    #: A chat deliverable that counts (1), else 0.
    deliverables: int = 0
    minutes_low: float = 0.0
    minutes_likely: float = 0.0
    minutes_high: float = 0.0
    value_low: float = 0.0
    value_likely: float = 0.0
    value_high: float = 0.0
    cost: float = 0.0


@dataclass(frozen=True)
class Viewer:
    """Who reads the figures: a person, the agents and fleets they see, and whether they read the organization."""

    user_id: uuid.UUID | None
    organization: bool = False
    agent_ids: frozenset[uuid.UUID] = frozenset()
    fleet_ids: frozenset[uuid.UUID] = frozenset()
    #: Administrators with ``roi_by_person_visible`` on (D12-1): the By person breakdown.
    by_person: bool = False
    #: k of the k-anonymity rule (12.5) for a non-administrator; ``None``: the rule is off.
    #: Never read for an organization reader (administrators see every row).
    min_group_size: int | None = None

    @property
    def k(self) -> int | None:
        """The k the figures apply: ``None`` for an administrator or when the rule is off."""
        return None if self.organization else self.min_group_size

    def sees_agent(self, agent_id: uuid.UUID | None) -> bool:
        return self.organization or (agent_id is not None and agent_id in self.agent_ids)

    def sees_fleet(self, fleet_id: uuid.UUID | None) -> bool:
        return self.organization or (fleet_id is not None and fleet_id in self.fleet_ids)


@dataclass
class Totals:
    """The sums of a set of facts. Kept unrounded: :meth:`as_dict` rounds once, at the answer."""

    runs: int = 0
    reviewed: int = 0
    units: int = 0
    deliverables: int = 0
    minutes_low: float = 0.0
    minutes_likely: float = 0.0
    minutes_high: float = 0.0
    value_low: float = 0.0
    value_likely: float = 0.0
    value_high: float = 0.0
    cost: float = 0.0
    #: Distinct people behind the facts (the k rule of slice L5 reads it); never answered.
    people: set[uuid.UUID] = field(default_factory=set)

    def add(self, fact: LedgerFact) -> None:
        self.runs += fact.runs
        self.reviewed += fact.reviewed
        self.units += fact.units
        self.deliverables += fact.deliverables
        self.minutes_low += fact.minutes_low
        self.minutes_likely += fact.minutes_likely
        self.minutes_high += fact.minutes_high
        self.value_low += fact.value_low
        self.value_likely += fact.value_likely
        self.value_high += fact.value_high
        self.cost += fact.cost
        if fact.person_id is not None:
            self.people.add(fact.person_id)

    def as_dict(self, *, cost_masked: bool, period_days: int | None) -> dict[str, Any]:
        """The answer of these totals; with *cost_masked* every cost-derived figure is ``None``, never 0."""
        cost = None if cost_masked else round(self.cost, 4)

        def net(value: float) -> float | None:
            return None if cost_masked else round(value - self.cost, 2)

        def ratio(value: float) -> float | None:
            if cost_masked or self.cost <= 0:
                return None
            return round((value - self.cost) / self.cost, 2)

        return {
            "runs": self.runs,
            "reviewed": self.reviewed,
            "units": self.units,
            "deliverables": self.deliverables,
            "hours": round(self.minutes_likely / 60.0, 2),
            "hours_low": round(self.minutes_low / 60.0, 2),
            "hours_high": round(self.minutes_high / 60.0, 2),
            "value": round(self.value_likely, 2),
            "value_low": round(self.value_low, 2),
            "value_high": round(self.value_high, 2),
            "ai_cost": cost,
            "net_value": net(self.value_likely),
            "net_value_low": net(self.value_low),
            "net_value_high": net(self.value_high),
            "roi": ratio(self.value_likely),
            "payback_months": None if cost_masked else _payback(self.value_likely - self.cost, period_days),
        }


def _payback(net_value: float, period_days: int | None) -> float | None:
    """No setup cost is recorded: ``0`` when the net is positive, else ``None`` (``roi._payback``)."""
    del period_days  # kept for the setup cost of a later slice; the rule of roi.compute_roi without one
    return 0.0 if net_value > 0 else None


@dataclass
class Row:
    """One row of a breakdown: its key (an id, or a residual or folded key) and its totals."""

    key: str
    totals: Totals
    #: By week: the last week a merged point holds (12.5), ``None`` for a single week.
    until: str | None = None


@dataclass
class Suppressed:
    """What the k rule did to one answer (12.5); answered as ``suppressed`` for a non-administrator."""

    k: int
    #: The rows folded into an "Other" row, over every breakdown (complementary folds included).
    groups: int = 0
    #: An agent or fleet backed by fewer than k people: the figures are the viewer's own share.
    own_share_only: bool = False
    #: False when By source is not split (one named row would be left next to "Other sources").
    sources_split: bool = True
    #: Weeks merged with a neighbour.
    weeks_merged: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "k": self.k,
            "groups": self.groups,
            "own_share_only": self.own_share_only,
            "sources_split": self.sources_split,
            "weeks_merged": self.weeks_merged,
        }


@dataclass
class Figures:
    """The figures of one scope for one viewer: the hero and its breakdowns, each summing to it."""

    scope: str
    scope_id: uuid.UUID | None
    window_first: date | None
    window_last: date
    totals: Totals
    by_source: list[Row]
    by_unit: list[Row]
    by_agent: list[Row]
    by_fleet: list[Row]
    by_week: list[Row]
    #: Administrators with the setting on only; ``None`` otherwise (never an empty list).
    by_person: list[Row] | None
    cost_masked: bool
    #: The first day from which every fact names its person, when the window starts earlier (12.11).
    since: date | None
    period_days: int | None = None
    currency: str = ""
    #: The k rule's trace (12.5); ``None`` when the rule did not apply (Mine, administrators).
    suppressed: Suppressed | None = None
    #: Fleet scope only (#2639): the facts of the agents OUTSIDE the fleet that supervise it, never in
    #: the hero nor in any breakdown (the organization counts them); ``None`` when there are none.
    supervision: Totals | None = None
    supervision_agents: int = 0

    @property
    def basis(self) -> str:
        """What the hero counts (#2639): ``fleet_members``, ``agent``, ``mine`` or ``organization``."""
        return {SCOPE_FLEET: BASIS_FLEET_MEMBERS, SCOPE_AGENT: BASIS_AGENT, SCOPE_MINE: BASIS_MINE}.get(
            self.scope, BASIS_ORGANIZATION
        )

    def as_dict(self) -> dict[str, Any]:
        def rows(items: list[Row] | None) -> list[dict[str, Any]] | None:
            if items is None:
                return None
            out = []
            for r in items:
                row = {"key": r.key, **r.totals.as_dict(cost_masked=self.cost_masked, period_days=self.period_days)}
                if r.until is not None:
                    row["until"] = r.until
                out.append(row)
            return out

        return {
            "scope": self.scope,
            "currency": self.currency,
            "id": str(self.scope_id) if self.scope_id else None,
            "window_first": self.window_first.isoformat() if self.window_first else None,
            "window_last": self.window_last.isoformat(),
            "totals": self.totals.as_dict(cost_masked=self.cost_masked, period_days=self.period_days),
            "by_source": rows(self.by_source),
            "by_unit": rows(self.by_unit),
            "by_agent": rows(self.by_agent),
            "by_fleet": rows(self.by_fleet),
            "by_week": rows(self.by_week),
            "by_person": rows(self.by_person),
            "cost_masked": self.cost_masked,
            "since": self.since.isoformat() if self.since else None,
            "suppressed": self.suppressed.as_dict() if self.suppressed else None,
            "basis": self.basis,
            "supervision": (
                {
                    "agents": self.supervision_agents,
                    **self.supervision.as_dict(cost_masked=self.cost_masked, period_days=self.period_days),
                }
                if self.supervision is not None
                else None
            ),
        }


def normalize_scope(scope: str) -> str:
    """*scope* with ``platform`` read as ``organization``; ``ValueError`` on an unknown one."""
    scope = SCOPE_ORGANIZATION if scope == SCOPE_PLATFORM_ALIAS else scope
    if scope not in SCOPES:
        raise ValueError(f"scope {scope!r} is not one of {', '.join(SCOPES)}")
    return scope


def select(facts: Iterable[LedgerFact], scope: str, viewer: Viewer, scope_id: uuid.UUID | None) -> list[LedgerFact]:
    """The facts of *scope* (12.4). The organization selects every fact, whoever reads it."""
    scope = normalize_scope(scope)
    if scope == SCOPE_MINE:
        if viewer.user_id is None:
            return []
        return [f for f in facts if f.person_id == viewer.user_id]
    if scope == SCOPE_AGENT:
        if scope_id is None:
            raise ValueError("an agent scope needs its id")
        return [f for f in facts if f.agent_id == scope_id]
    if scope == SCOPE_FLEET:
        if scope_id is None:
            raise ValueError("a fleet scope needs its id")
        return [f for f in facts if f.fleet_id == scope_id]
    return list(facts)


def week_of(day: date) -> date:
    """The Monday of *day*'s week."""
    return day - timedelta(days=day.weekday())


def _breakdown(facts: list[LedgerFact], key_of: Callable[[LedgerFact], Hashable], order: list[str]) -> list[Row]:
    groups: dict[str, Totals] = {}
    for fact in facts:
        groups.setdefault(str(key_of(fact)), Totals()).add(fact)
    residual = [k for k in order if k in groups]
    named = sorted((k for k in groups if k not in order), key=lambda k: (-groups[k].value_likely, k))
    return [Row(k, groups[k]) for k in (*named, *residual)]


def _since(facts: list[LedgerFact], window_first: date | None) -> date | None:
    """The day after the last fact not attributed yet (``attribution`` ``None``), when it falls in the window."""
    unattributed = [f.day for f in facts if f.attribution is None]
    if not unattributed:
        return None
    since = max(unattributed) + timedelta(days=1)
    return since if window_first is None or since > window_first else None


def compute(
    facts: Iterable[LedgerFact],
    *,
    scope: str,
    viewer: Viewer,
    scope_id: uuid.UUID | None = None,
    window_first: date | None,
    window_last: date,
    cost_masked: bool = False,
    period_days: int | None = None,
    supervisor_ids: frozenset[uuid.UUID] = frozenset(),
) -> Figures:
    """The figures of *scope* as *viewer* reads them, over the facts of the window (already filtered by day).

    *supervisor_ids* (fleet scope, #2639): agents outside the fleet that supervise it. Their facts
    never carry the fleet (a fact's fleet is its agent's HOME fleet), so the hero excludes them by
    construction; they are summed apart in ``supervision`` (the organization total counts them).

    The hero sums every selected fact. Each breakdown puts every selected fact in
    exactly one row: its own key, its residual row when it lacks the dimension,
    or "other" when a non-administrator does not see the object (organization
    and fleet scopes; Mine names every agent the viewer used).
    """
    scope = normalize_scope(scope)
    facts = list(facts)
    chosen = select(facts, scope, viewer, scope_id)
    totals = Totals()
    for fact in chosen:
        totals.add(fact)
    k = viewer.k if scope != SCOPE_MINE else None
    suppressed = Suppressed(k=k) if k is not None else None
    if k is not None and suppressed is not None and scope in (SCOPE_AGENT, SCOPE_FLEET) and len(totals.people) < k:
        # An object backed by fewer than k people: the viewer's own share only (12.5).
        chosen = [f for f in chosen if viewer.user_id is not None and f.person_id == viewer.user_id]
        totals = Totals()
        for fact in chosen:
            totals.add(fact)
        suppressed.own_share_only = True
        k = None  # his own facts: nothing left to suppress
    fold = not viewer.organization and scope in (SCOPE_ORGANIZATION, SCOPE_FLEET)

    def agent_key(f: LedgerFact) -> str:
        if f.agent_id is None:
            return ROW_NO_AGENT
        if fold and not viewer.sees_agent(f.agent_id):
            return ROW_OTHER
        return str(f.agent_id)

    def fleet_key(f: LedgerFact) -> str:
        if f.fleet_id is None:
            return ROW_NO_FLEET
        if fold and not viewer.sees_fleet(f.fleet_id):
            return ROW_OTHER
        return str(f.fleet_id)

    by_source = [Row(c, t) for c, t in _per_class(chosen)]
    by_week = sorted(_breakdown(chosen, lambda f: week_of(f.day).isoformat(), []), key=lambda r: r.key)
    by_unit = _breakdown(chosen, lambda f: f.unit or ROW_NO_UNIT, [ROW_NO_UNIT])
    by_agent = _breakdown(chosen, agent_key, [ROW_OTHER, ROW_NO_AGENT])
    by_fleet = _breakdown(chosen, fleet_key, [ROW_OTHER, ROW_NO_FLEET])
    if suppressed is not None and k is not None:
        by_source, folded = suppress_rows(by_source, k, ROW_OTHER_SOURCES)
        suppressed.groups += folded
        named = [r for r in by_source if r.key != ROW_OTHER_SOURCES]
        if folded and len(named) <= 1:
            # One named row next to "Other sources" tells nothing the hero does not (12.5).
            by_source = [Row(ROW_OTHER_SOURCES, _merged([r.totals for r in by_source]))]
            suppressed.sources_split = False
        by_unit, folded = suppress_rows(by_unit, k, ROW_OTHER_UNITS)
        suppressed.groups += folded
        by_agent, folded = suppress_rows(by_agent, k, ROW_OTHER)
        suppressed.groups += folded
        by_fleet, folded = suppress_rows(by_fleet, k, ROW_OTHER)
        suppressed.groups += folded
        by_week, suppressed.weeks_merged = merge_weeks(by_week, k)
    by_person = None
    if viewer.organization and viewer.by_person:
        by_person = _breakdown(chosen, lambda f: f.person_id or ROW_NO_PERSON, [ROW_NO_PERSON])
    supervision, supervision_agents = _supervision(facts, scope, scope_id, supervisor_ids, viewer, suppressed)
    return Figures(
        scope=scope,
        scope_id=scope_id if scope in (SCOPE_AGENT, SCOPE_FLEET) else None,
        window_first=window_first,
        window_last=window_last,
        totals=totals,
        by_source=by_source,
        by_unit=by_unit,
        by_agent=by_agent,
        by_fleet=by_fleet,
        by_week=by_week,
        by_person=by_person,
        cost_masked=cost_masked,
        since=_since(chosen, window_first),
        period_days=period_days,
        suppressed=suppressed,
        supervision=supervision,
        supervision_agents=supervision_agents,
    )


def _supervision(
    facts: Iterable[LedgerFact],
    scope: str,
    scope_id: uuid.UUID | None,
    supervisor_ids: frozenset[uuid.UUID],
    viewer: Viewer,
    suppressed: Suppressed | None,
) -> tuple[Totals | None, int]:
    """The supervision line of a fleet answer (#2639): the facts of its outside supervisors, apart.

    ``None`` outside the fleet scope, with no supervisor or no fact, when the viewer reads his own
    share only, or when a non-administrator would read a line backed by fewer than k people (12.5).
    A supervisor that is a home member of the fleet (its facts carry the fleet) is never summed here.
    """
    if scope != SCOPE_FLEET or not supervisor_ids or (suppressed is not None and suppressed.own_share_only):
        return None, 0
    totals = Totals()
    agents: set[uuid.UUID] = set()
    for fact in facts:
        if fact.agent_id in supervisor_ids and fact.fleet_id != scope_id:
            totals.add(fact)
            agents.add(fact.agent_id)
    if not agents:
        return None, 0
    k = viewer.k
    if k is not None and len(totals.people) < k:
        return None, 0
    return totals, len(agents)


def _merged(parts: Iterable[Totals]) -> Totals:
    """One :class:`Totals` holding every part (people included)."""
    out = Totals()
    for t in parts:
        out.runs += t.runs
        out.reviewed += t.reviewed
        out.units += t.units
        out.deliverables += t.deliverables
        out.minutes_low += t.minutes_low
        out.minutes_likely += t.minutes_likely
        out.minutes_high += t.minutes_high
        out.value_low += t.value_low
        out.value_likely += t.value_likely
        out.value_high += t.value_high
        out.cost += t.cost
        out.people |= t.people
    return out


def suppress_rows(rows: list[Row], k: int, other_key: str) -> tuple[list[Row], int]:
    """*rows* with every row backed by fewer than *k* people folded into *other_key* (12.5).

    Residual rows count as groups too. When exactly one row is folded, the next
    smallest row is folded with it (complementary suppression), so no row is
    recovered by subtracting the visible rows from the hero. Answers
    the rows (named first, in their order, then "Other") and the number folded.
    """
    other = next((r for r in rows if r.key == other_key), None)
    candidates = [r for r in rows if r.key != other_key]
    under = [r for r in candidates if len(r.totals.people) < k]
    if not under:
        return rows, 0
    kept = [r for r in candidates if len(r.totals.people) >= k]
    if len(under) == 1 and kept:
        smallest = min(kept, key=lambda r: (len(r.totals.people), r.totals.value_likely, r.key))
        kept.remove(smallest)
        under.append(smallest)
    parts = [r.totals for r in under]
    if other is not None:
        parts.append(other.totals)
    return [*kept, Row(other_key, _merged(parts))], len(under)


def merge_weeks(rows: list[Row], k: int) -> tuple[list[Row], int]:
    """By week with every week backed by fewer than *k* people merged with the next (12.5).

    The last week merges with the previous one; repeated until every point is backed
    by *k* people or one point is left. A merged point keeps its first week as key and
    names its last week in ``until``. Answers the points (in order) and the merges made.
    """
    points = [Row(r.key, r.totals, r.until) for r in sorted(rows, key=lambda r: r.key)]
    merges = 0
    while len(points) > 1:
        index = next((i for i, r in enumerate(points) if len(r.totals.people) < k), None)
        if index is None:
            break
        first, second = (index - 1, index) if index == len(points) - 1 else (index, index + 1)
        a, b = points[first], points[second]
        points[first : second + 1] = [Row(a.key, _merged([a.totals, b.totals]), b.until or b.key)]
        merges += 1
    return points, merges


def compute_subjects(
    facts: Iterable[LedgerFact],
    *,
    subject_type: str,
    ids: Iterable[uuid.UUID],
    viewer: Viewer,
    window_first: date | None,
    window_last: date,
    cost_masked: bool = False,
    period_days: int | None = None,
    supervisors: dict[uuid.UUID, frozenset[uuid.UUID]] | None = None,
) -> dict[uuid.UUID, Figures]:
    """The figures of each agent or fleet of *ids* over one set of facts (the list column, slice L4).

    Each entry is :func:`compute` of that object's scope, so agent X's figure here is the
    agent view's figure, by construction (invariant d).
    """
    if subject_type not in (SCOPE_AGENT, SCOPE_FLEET):
        raise ValueError(f"subject_type {subject_type!r} is not agent or fleet")
    chosen = list(facts)
    return {
        sid: compute(
            chosen,
            scope=subject_type,
            viewer=viewer,
            scope_id=sid,
            window_first=window_first,
            window_last=window_last,
            cost_masked=cost_masked,
            period_days=period_days,
            supervisor_ids=(supervisors or {}).get(sid, frozenset()),
        )
        for sid in dict.fromkeys(ids)
    }


def run_fact(
    *,
    day: date,
    person_id: uuid.UUID | None,
    attribution: str | None,
    agent_id: uuid.UUID | None,
    fleet_id: uuid.UUID | None,
    unit: str,
    units: int,
    outcome: str | None,
    review_min: float | None,
    baseline: Any,
    rate: float | None,
) -> LedgerFact:
    """The value fact of one run or work item record, priced as ``roi.compute_roi`` prices a run.

    *baseline* is the unit's ``roi.Baseline`` (``None``: a unit the catalog does not know, counted
    as a run with no value), *rate* the hourly rate of its role in the report's currency. A rejected
    run saves nothing. The record's own ``llm_cost`` is not read: the cost is the cost facts' (12.3).
    """
    from app.services import roi

    reviewed = 0 if outcome is None else 1
    counted = baseline is not None and outcome != roi.OUTCOME_REJECTED and units > 0
    common: dict[str, Any] = {
        "day": day,
        "person_id": person_id,
        "attribution": attribution,
        "agent_id": agent_id,
        "fleet_id": fleet_id,
        "cost_class": CLASS_AUTOMATION,
        "unit": unit,
        "runs": 1,
        "reviewed": reviewed,
        "units": units if counted else 0,
    }
    if not counted or rate is None:
        return LedgerFact(**common)
    review = max(review_min, 0.0) if review_min is not None else baseline.review_min * units
    low = max(baseline.manual_min_low * units - review, 0.0)
    likely = max(baseline.manual_min_likely * units - review, 0.0)
    high = max(baseline.manual_min_high * units - review, 0.0)
    return LedgerFact(
        **common,
        minutes_low=low,
        minutes_likely=likely,
        minutes_high=high,
        value_low=low / 60.0 * rate,
        value_likely=likely / 60.0 * rate,
        value_high=high / 60.0 * rate,
    )


def chat_fact(
    *,
    day: date,
    person_id: uuid.UUID | None,
    agent_id: uuid.UUID | None,
    fleet_id: uuid.UUID | None,
    unit: str,
    minutes: float,
    rate: float | None,
) -> LedgerFact:
    """The value fact of one counted chat deliverable: its counted minutes, low = likely = high (an estimate)."""
    value = minutes / 60.0 * rate if rate is not None else 0.0
    return LedgerFact(
        day=day,
        person_id=person_id,
        attribution="user" if person_id is not None else "unknown",
        agent_id=agent_id,
        fleet_id=fleet_id,
        cost_class=CLASS_CHAT,
        unit=unit,
        deliverables=1,
        minutes_low=minutes,
        minutes_likely=minutes,
        minutes_high=minutes,
        value_low=value,
        value_likely=value,
        value_high=value,
    )


def _per_class(facts: list[LedgerFact]) -> list[tuple[str, Totals]]:
    """By source: the three classes in their order, each present once it holds a fact."""
    groups: dict[str, Totals] = {}
    for fact in facts:
        if fact.cost_class not in CLASSES:
            raise ValueError(f"unknown cost class {fact.cost_class!r}")
        groups.setdefault(fact.cost_class, Totals()).add(fact)
    return [(c, groups[c]) for c in CLASSES if c in groups]
