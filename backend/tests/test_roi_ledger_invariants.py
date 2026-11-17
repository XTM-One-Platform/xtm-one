"""The ROI ledger invariants (issue #12077, slice L3; design ``dev-docs/roi-outcomes.md`` 12.7).

A seeded fixture: 7 people (one administrator), an own agent, a shared agent,
a company-managed agent, two fleets (one of 2 people, one of 6), agentless
chat, system traces, a deleted agent, facts of every class over 5 weeks; each
invariant runs on it and on 200 seeded random draws of it, priced and with
costs masked. Slice L3 turns a, b, c, e and h green; L4 turns d green (the
surfaces on ``figures``: the agent view, the list column of ``compute_subjects``
and the administrator's by-agent row); f and g come with L5 (anonymity).
"""

from __future__ import annotations

import random
import uuid
from dataclasses import replace
from datetime import date, timedelta

import pytest

from app.services import roi
from app.services.roi_figures import (
    CLASS_AUTOMATION,
    CLASS_CHAT,
    CLASS_PLATFORM,
    ROW_NO_PERSON,
    ROW_OTHER,
    SCOPE_AGENT,
    SCOPE_FLEET,
    SCOPE_MINE,
    SCOPE_ORGANIZATION,
    Figures,
    LedgerFact,
    Totals,
    Viewer,
    chat_fact,
    compute,
    compute_subjects,
    normalize_scope,
    run_fact,
)

FIRST = date(2026, 10, 5)
LAST = FIRST + timedelta(days=34)
PEOPLE = [uuid.UUID(int=i + 1) for i in range(7)]
ADMIN = PEOPLE[0]
OWN, SHARED, COMPANY, DELETED = (uuid.UUID(int=100 + i) for i in range(4))
AGENTS = (OWN, SHARED, COMPANY, DELETED)
SMALL_FLEET, BIG_FLEET = uuid.UUID(int=200), uuid.UUID(int=201)
FLEET_OF = {OWN: SMALL_FLEET, SHARED: BIG_FLEET, COMPANY: BIG_FLEET, DELETED: None}
BASELINE = roi.BASELINES_BY_UNIT["threat_report_triaged"]


def _fixture(seed: int) -> list[LedgerFact]:
    """Facts of every class over 5 weeks; seed 0 is the fixed fixture, any other a random draw."""
    rng = random.Random(seed)  # noqa: S311 - seeded test draws, not cryptography
    facts: list[LedgerFact] = []
    small_people = PEOPLE[1:3]
    for n in range(40 if seed == 0 else rng.randint(5, 80)):
        day = FIRST + timedelta(days=rng.randint(0, 34))
        agent = rng.choice(AGENTS)
        person = rng.choice(small_people if agent == OWN else PEOPLE)
        kind = n % 5 if seed == 0 else rng.randint(0, 4)
        if kind == 0:
            facts.append(
                run_fact(
                    day=day,
                    person_id=person,
                    attribution="user",
                    agent_id=agent,
                    fleet_id=FLEET_OF[agent],
                    unit=BASELINE.unit,
                    units=rng.randint(0, 4),
                    outcome=rng.choice([None, roi.OUTCOME_ACCEPTED, roi.OUTCOME_REJECTED]),
                    review_min=rng.choice([None, 2.0]),
                    baseline=BASELINE,
                    rate=38.85,
                )
            )
        elif kind == 1:
            # A record whose run and item are gone: no person; or a unit the catalog does not know.
            facts.append(
                run_fact(
                    day=day,
                    person_id=None,
                    attribution=rng.choice(["unknown", None]),
                    agent_id=agent,
                    fleet_id=FLEET_OF[agent],
                    unit=rng.choice([BASELINE.unit, "not_in_catalog"]),
                    units=1,
                    outcome=None,
                    review_min=None,
                    baseline=BASELINE,
                    rate=38.85,
                )
            )
        elif kind == 2:
            # Chat on an agent, or agentless general chat.
            on_agent = rng.random() < 0.6
            facts.append(
                chat_fact(
                    day=day,
                    person_id=person,
                    agent_id=agent if on_agent else None,
                    fleet_id=FLEET_OF[agent] if on_agent else None,
                    unit="presentation_built",
                    minutes=rng.uniform(0, 120),
                    rate=38.85,
                )
            )
        else:
            # Cost facts: automation and chat for a person, platform calls for nobody (system).
            cls = rng.choice([CLASS_AUTOMATION, CLASS_CHAT, CLASS_PLATFORM])
            nobody = cls == CLASS_PLATFORM and rng.random() < 0.5
            facts.append(
                LedgerFact(
                    day=day,
                    person_id=None if nobody else person,
                    attribution="system" if nobody else "user",
                    agent_id=None if cls == CLASS_PLATFORM else agent,
                    fleet_id=None if cls == CLASS_PLATFORM else FLEET_OF[agent],
                    cost_class=cls,
                    cost=round(rng.uniform(0, 3), 4),
                )
            )
    return facts


SEEDS = [0, *range(1, 201)]


def _viewer(person: uuid.UUID, *, by_person: bool = False, k: int | None = None) -> Viewer:
    if person == ADMIN:
        return Viewer(user_id=ADMIN, organization=True, by_person=by_person, min_group_size=k)
    # A non-administrator sees the shared and company-managed agents and the big fleet only.
    return Viewer(
        user_id=person, agent_ids=frozenset({SHARED, COMPANY}), fleet_ids=frozenset({BIG_FLEET}), min_group_size=k
    )


def _figures(facts: list[LedgerFact], scope: str, viewer: Viewer, **kw: object) -> Figures:
    return compute(facts, scope=scope, viewer=viewer, window_first=FIRST, window_last=LAST, **kw)  # type: ignore[arg-type]


def _close(a: Totals, b: Totals) -> bool:
    return (
        (a.runs, a.reviewed, a.units, a.deliverables) == (b.runs, b.reviewed, b.units, b.deliverables)
        and a.minutes_likely == pytest.approx(b.minutes_likely)
        and a.minutes_low == pytest.approx(b.minutes_low)
        and a.minutes_high == pytest.approx(b.minutes_high)
        and a.value_likely == pytest.approx(b.value_likely)
        and a.cost == pytest.approx(b.cost)
    )


def _sum(rows: list[Totals]) -> Totals:
    total = Totals()
    for t in rows:
        total.runs += t.runs
        total.reviewed += t.reviewed
        total.units += t.units
        total.deliverables += t.deliverables
        total.minutes_low += t.minutes_low
        total.minutes_likely += t.minutes_likely
        total.minutes_high += t.minutes_high
        total.value_low += t.value_low
        total.value_likely += t.value_likely
        total.value_high += t.value_high
        total.cost += t.cost
    return total


@pytest.mark.parametrize("seed", SEEDS)
def test_a_mine_of_every_person_plus_no_person_is_the_organization(seed: int) -> None:
    facts = _fixture(seed)
    org = _figures(facts, SCOPE_ORGANIZATION, _viewer(ADMIN, by_person=True))
    mines = [_figures(facts, SCOPE_MINE, _viewer(p)).totals for p in PEOPLE]
    no_person = [r.totals for r in org.by_person or [] if r.key == ROW_NO_PERSON]
    assert _close(_sum([*mines, *no_person]), org.totals)


@pytest.mark.parametrize("seed", SEEDS)
def test_b_the_organization_totals_are_the_same_for_every_viewer(seed: int) -> None:
    facts = _fixture(seed)
    admin = _figures(facts, SCOPE_ORGANIZATION, _viewer(ADMIN)).totals.as_dict(cost_masked=False, period_days=35)
    for person in PEOPLE[1:]:
        seen = _figures(facts, SCOPE_ORGANIZATION, _viewer(person)).totals.as_dict(cost_masked=False, period_days=35)
        assert seen == admin


def _scopes() -> list[tuple[str, uuid.UUID | None]]:
    return [
        (SCOPE_ORGANIZATION, None),
        (SCOPE_MINE, None),
        *((SCOPE_AGENT, a) for a in AGENTS),
        (SCOPE_FLEET, SMALL_FLEET),
        (SCOPE_FLEET, BIG_FLEET),
    ]


@pytest.mark.parametrize("seed", SEEDS)
def test_c_every_breakdown_of_every_scope_sums_to_its_hero(seed: int) -> None:
    facts = _fixture(seed)
    for person in PEOPLE:
        for scope, scope_id in _scopes():
            figures = _figures(facts, scope, _viewer(person, by_person=True), scope_id=scope_id)
            breakdowns = [figures.by_source, figures.by_unit, figures.by_agent, figures.by_fleet, figures.by_week]
            if figures.by_person is not None:
                breakdowns.append(figures.by_person)
            for rows in breakdowns:
                assert _close(_sum([r.totals for r in rows]), figures.totals), (scope, scope_id, person)


@pytest.mark.parametrize("seed", SEEDS)
def test_d_an_agent_s_figures_are_the_same_on_its_view_the_list_column_and_the_by_agent_row(seed: int) -> None:
    """Agent X: the agent view, the list column (``compute_subjects``, what ``GET /roi/summary`` answers
    as ``totals``) and the administrator's organization by-agent row carry the same totals, for every
    viewer; the same holds for a fleet and its by-fleet row."""
    facts = _fixture(seed)
    org = _figures(facts, SCOPE_ORGANIZATION, _viewer(ADMIN))
    by_agent = {r.key: r.totals for r in org.by_agent}
    by_fleet = {r.key: r.totals for r in org.by_fleet}
    for person in PEOPLE:
        viewer = _viewer(person)
        column = compute_subjects(
            facts, subject_type=SCOPE_AGENT, ids=AGENTS, viewer=viewer, window_first=FIRST, window_last=LAST
        )
        fleets = compute_subjects(
            facts,
            subject_type=SCOPE_FLEET,
            ids=(SMALL_FLEET, BIG_FLEET),
            viewer=viewer,
            window_first=FIRST,
            window_last=LAST,
        )
        for agent in AGENTS:
            view = _figures(facts, SCOPE_AGENT, viewer, scope_id=agent).totals
            assert column[agent].totals.as_dict(cost_masked=False, period_days=35) == view.as_dict(
                cost_masked=False, period_days=35
            )
            assert _close(view, by_agent.get(str(agent), Totals())), (agent, person)
        for fleet in (SMALL_FLEET, BIG_FLEET):
            view = _figures(facts, SCOPE_FLEET, viewer, scope_id=fleet).totals
            assert _close(fleets[fleet].totals, view)
            assert _close(view, by_fleet.get(str(fleet), Totals())), (fleet, person)


def test_compute_subjects_refuses_a_wide_scope_and_answers_each_id_once() -> None:
    facts = _fixture(0)
    with pytest.raises(ValueError):
        compute_subjects(
            facts, subject_type=SCOPE_MINE, ids=[], viewer=_viewer(ADMIN), window_first=FIRST, window_last=LAST
        )
    once = compute_subjects(
        facts,
        subject_type=SCOPE_AGENT,
        ids=[OWN, OWN, SHARED],
        viewer=_viewer(ADMIN),
        window_first=FIRST,
        window_last=LAST,
    )
    assert list(once) == [OWN, SHARED]


@pytest.mark.parametrize("seed", SEEDS)
def test_e_a_fact_on_a_shared_agent_moves_only_its_persons_mine(seed: int) -> None:
    facts = _fixture(seed)
    rng = random.Random(seed)  # noqa: S311 - seeded test draws, not cryptography
    person = rng.choice(PEOPLE)
    agent = rng.choice([SHARED, COMPANY])
    extra = [
        run_fact(
            day=LAST,
            person_id=person,
            attribution="user",
            agent_id=agent,
            fleet_id=FLEET_OF[agent],
            unit=BASELINE.unit,
            units=2,
            outcome=roi.OUTCOME_ACCEPTED,
            review_min=None,
            baseline=BASELINE,
            rate=38.85,
        ),
        chat_fact(
            day=LAST,
            person_id=person,
            agent_id=agent,
            fleet_id=FLEET_OF[agent],
            unit="report_drafted",
            minutes=30,
            rate=38.85,
        ),
        LedgerFact(
            day=LAST,
            person_id=person,
            attribution="user",
            agent_id=agent,
            fleet_id=FLEET_OF[agent],
            cost_class=CLASS_AUTOMATION,
            cost=1.25,
        ),
    ]
    for other in PEOPLE:
        before = _figures(facts, SCOPE_MINE, _viewer(other)).totals
        after = _figures([*facts, *extra], SCOPE_MINE, _viewer(other)).totals
        if other == person:
            assert after.value_likely > before.value_likely
            assert after.cost == pytest.approx(before.cost + 1.25)
        else:
            assert _close(after, before)


@pytest.mark.parametrize("seed", SEEDS)
def test_h_every_fact_lands_in_exactly_one_row_and_no_person_is_by_person_only(seed: int) -> None:
    facts = _fixture(seed)
    for person in PEOPLE:
        for scope, scope_id in _scopes():
            figures = _figures(facts, scope, _viewer(person, by_person=True), scope_id=scope_id)
            for rows in (figures.by_source, figures.by_unit, figures.by_agent, figures.by_fleet, figures.by_week):
                keys = [r.key for r in rows]
                assert len(keys) == len(set(keys)), "one row per key"
                assert ROW_NO_PERSON not in keys
                # Exactly one row each: the row counts of facts add up to the facts selected.
                facts_in_rows = sum(r.totals.runs + r.totals.deliverables for r in rows)
                assert facts_in_rows == figures.totals.runs + figures.totals.deliverables


def test_a_non_administrator_sees_the_objects_he_cannot_open_as_one_folded_row() -> None:
    facts = _fixture(0)
    figures = _figures(facts, SCOPE_ORGANIZATION, _viewer(PEOPLE[3]))
    keys = {r.key for r in figures.by_agent}
    assert str(OWN) not in keys and str(DELETED) not in keys
    assert ROW_OTHER in keys
    assert str(SMALL_FLEET) not in {r.key for r in figures.by_fleet}
    assert figures.by_person is None


def test_mine_names_every_agent_the_viewer_used() -> None:
    person = PEOPLE[1]
    facts = [
        LedgerFact(
            day=FIRST,
            person_id=person,
            attribution="user",
            agent_id=OWN,
            fleet_id=SMALL_FLEET,
            cost_class=CLASS_AUTOMATION,
            cost=1.0,
        ),
    ]
    figures = _figures(facts, SCOPE_MINE, Viewer(user_id=person))
    assert [r.key for r in figures.by_agent] == [str(OWN)]


def test_costs_masked_answer_none_never_zero() -> None:
    figures = _figures(_fixture(0), SCOPE_ORGANIZATION, _viewer(ADMIN), cost_masked=True)
    answer = figures.as_dict()
    for key in ("ai_cost", "net_value", "roi", "payback_months"):
        assert answer["totals"][key] is None
    assert all(row["ai_cost"] is None for row in answer["by_agent"])
    assert answer["totals"]["value"] == round(figures.totals.value_likely, 2)


def test_a_rejected_run_saves_nothing_and_an_unknown_unit_counts_as_a_run() -> None:
    common = {
        "day": FIRST,
        "person_id": ADMIN,
        "attribution": "user",
        "agent_id": OWN,
        "fleet_id": None,
        "units": 3,
        "review_min": None,
        "baseline": BASELINE,
        "rate": 38.85,
    }
    rejected = run_fact(unit=BASELINE.unit, outcome=roi.OUTCOME_REJECTED, **common)  # type: ignore[arg-type]
    unknown = run_fact(unit="nope", outcome=None, **{**common, "baseline": None})  # type: ignore[arg-type]
    assert (rejected.runs, rejected.reviewed, rejected.units, rejected.value_likely) == (1, 1, 0, 0.0)
    assert (unknown.runs, unknown.reviewed, unknown.units, unknown.minutes_likely) == (1, 0, 0, 0.0)


def test_a_run_is_priced_as_compute_roi_prices_it() -> None:
    fact = run_fact(
        day=FIRST,
        person_id=ADMIN,
        attribution="user",
        agent_id=OWN,
        fleet_id=None,
        unit=BASELINE.unit,
        units=3,
        outcome=None,
        review_min=None,
        baseline=BASELINE,
        rate=38.85,
    )
    rates = {BASELINE.rate_role: {"EUR": roi.HourlyRate(BASELINE.rate_role, "EUR", 38.85, "", "")}}
    expected = roi.compute_roi([roi.RunMeasure(unit=BASELINE.unit, units=3, outcome=None)], currency="EUR", rates=rates)
    assert round(fact.minutes_likely, 1) == expected.minutes_saved
    assert round(fact.value_likely, 2) == expected.value
    assert fact.units == expected.units == 3


def test_since_is_the_day_after_the_last_unattributed_fact() -> None:
    facts = [
        replace(
            LedgerFact(
                day=FIRST + timedelta(days=3),
                person_id=None,
                attribution=None,
                agent_id=None,
                fleet_id=None,
                cost_class=CLASS_PLATFORM,
                cost=1.0,
            )
        ),
    ]
    assert _figures(facts, SCOPE_ORGANIZATION, _viewer(ADMIN)).since == FIRST + timedelta(days=4)
    assert _figures([], SCOPE_ORGANIZATION, _viewer(ADMIN)).since is None


def test_platform_is_an_alias_of_organization_and_an_unknown_scope_is_refused() -> None:
    assert normalize_scope("platform") == SCOPE_ORGANIZATION
    with pytest.raises(ValueError, match="scope"):
        normalize_scope("team")
    with pytest.raises(ValueError, match="agent scope needs its id"):
        _figures([], SCOPE_AGENT, _viewer(ADMIN))
