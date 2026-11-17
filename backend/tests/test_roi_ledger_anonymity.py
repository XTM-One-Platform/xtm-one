"""The k-anonymity rule of the ROI ledger (issue #12077, slice L5; design ``dev-docs/roi-outcomes.md`` 12.5).

Invariant (g): a group backed by fewer than k people is never shown to a
non-administrator. Seeded on the fixture of ``test_roi_ledger_invariants``,
with k in 2..6 so both the folded and the clean paths run, plus the cases the
design names one by one (complementary fold, By source not split, weeks
merged, object views under k, administrators and Mine never suppressed).
"""

from __future__ import annotations

import json
import uuid
from datetime import timedelta

import pytest

from app.services.roi_figures import (
    CLASS_AUTOMATION,
    CLASS_CHAT,
    CLASS_PLATFORM,
    ROW_OTHER,
    ROW_OTHER_SOURCES,
    ROW_OTHER_UNITS,
    SCOPE_AGENT,
    SCOPE_FLEET,
    SCOPE_MINE,
    SCOPE_ORGANIZATION,
    Figures,
    LedgerFact,
    Row,
    Totals,
    Viewer,
    compute_subjects,
    merge_weeks,
    suppress_rows,
)
from tests.test_roi_ledger_invariants import (
    ADMIN,
    AGENTS,
    BIG_FLEET,
    FIRST,
    LAST,
    PEOPLE,
    SEEDS,
    SMALL_FLEET,
    _close,
    _figures,
    _fixture,
    _scopes,
    _sum,
    _viewer,
)

KS = (2, 3, 4, 5, 6)


def _breakdowns(figures: Figures) -> dict[str, list[Row]]:
    return {
        "by_source": figures.by_source,
        "by_unit": figures.by_unit,
        "by_agent": figures.by_agent,
        "by_fleet": figures.by_fleet,
        "by_week": figures.by_week,
    }


_OTHER_KEYS = {ROW_OTHER, ROW_OTHER_SOURCES, ROW_OTHER_UNITS}


@pytest.mark.parametrize("seed", SEEDS)
@pytest.mark.parametrize("k", KS)
def test_g_no_group_under_k_is_shown_to_a_non_administrator(seed: int, k: int) -> None:
    facts = _fixture(seed)
    for person in PEOPLE[1:]:
        for scope, scope_id in _scopes():
            if scope == SCOPE_MINE:
                continue
            figures = _figures(facts, scope, _viewer(person, k=k), scope_id=scope_id)
            if figures.suppressed is not None and figures.suppressed.own_share_only:
                # His own share: every fact is his.
                assert figures.totals.people <= {person}
                continue
            for name, rows in _breakdowns(figures).items():
                for row in rows:
                    if name == "by_week" and len(rows) == 1:
                        continue  # every week merged into one point: the trend keeps its total
                    if row.key in _OTHER_KEYS:
                        continue
                    assert len(row.totals.people) >= k, (scope, scope_id, person, name, row.key)


@pytest.mark.parametrize("seed", SEEDS)
@pytest.mark.parametrize("k", KS)
def test_c_under_k_every_breakdown_still_sums_to_its_hero(seed: int, k: int) -> None:
    facts = _fixture(seed)
    for person in PEOPLE:
        for scope, scope_id in _scopes():
            figures = _figures(facts, scope, _viewer(person, k=k), scope_id=scope_id)
            for name, rows in _breakdowns(figures).items():
                assert _close(_sum([r.totals for r in rows]), figures.totals), (scope, scope_id, person, name)


@pytest.mark.parametrize("seed", SEEDS)
@pytest.mark.parametrize("k", KS)
def test_b_the_organization_total_is_never_suppressed(seed: int, k: int) -> None:
    facts = _fixture(seed)
    admin = _figures(facts, SCOPE_ORGANIZATION, _viewer(ADMIN)).totals.as_dict(cost_masked=False, period_days=35)
    for person in PEOPLE[1:]:
        seen = _figures(facts, SCOPE_ORGANIZATION, _viewer(person, k=k)).totals
        assert seen.as_dict(cost_masked=False, period_days=35) == admin


@pytest.mark.parametrize("seed", SEEDS)
@pytest.mark.parametrize("k", KS)
def test_d_under_k_the_list_column_equals_the_object_view(seed: int, k: int) -> None:
    facts = _fixture(seed)
    for person in PEOPLE[1:]:
        viewer = _viewer(person, k=k)
        for subject_type, ids in ((SCOPE_AGENT, AGENTS), (SCOPE_FLEET, (SMALL_FLEET, BIG_FLEET))):
            column = compute_subjects(
                facts, subject_type=subject_type, ids=ids, viewer=viewer, window_first=FIRST, window_last=LAST
            )
            for sid in ids:
                view = _figures(facts, subject_type, viewer, scope_id=sid)
                assert column[sid].as_dict() == view.as_dict()


@pytest.mark.parametrize("seed", SEEDS[:40])
def test_mine_and_administrators_are_never_suppressed(seed: int) -> None:
    facts = _fixture(seed)
    for scope, scope_id in _scopes():
        plain = _figures(facts, scope, _viewer(ADMIN), scope_id=scope_id)
        with_k = _figures(facts, scope, _viewer(ADMIN, k=50), scope_id=scope_id)
        assert with_k.suppressed is None
        assert with_k.as_dict() == plain.as_dict()
    for person in PEOPLE[1:]:
        plain = _figures(facts, SCOPE_MINE, _viewer(person))
        with_k = _figures(facts, SCOPE_MINE, _viewer(person, k=50))
        assert with_k.suppressed is None
        assert with_k.as_dict() == plain.as_dict()


def _row(key: str, people: int, value: float = 1.0) -> Row:
    totals = Totals(value_likely=value, people={uuid.UUID(int=1000 + i) for i in range(people)})
    return Row(key, totals)


def test_exactly_one_group_under_k_takes_the_next_smallest_with_it() -> None:
    rows = [_row("a", 9), _row("b", 6), _row("c", 7), _row("d", 2)]
    out, folded = suppress_rows(rows, 5, ROW_OTHER)
    assert folded == 2
    assert [r.key for r in out] == ["a", "c", ROW_OTHER]
    assert out[-1].totals.value_likely == pytest.approx(2.0)


def test_the_complement_applies_when_the_other_row_already_holds_folded_objects() -> None:
    rows = [_row("a", 9), _row("c", 6), _row("b", 2), _row(ROW_OTHER, 3)]
    out, folded = suppress_rows(rows, 5, ROW_OTHER)
    assert folded == 2
    assert [r.key for r in out] == ["a", ROW_OTHER]
    assert out[-1].totals.value_likely == pytest.approx(3.0)


def test_nothing_under_k_leaves_the_rows_as_they_are() -> None:
    rows = [_row("a", 9), _row("b", 5)]
    assert suppress_rows(rows, 5, ROW_OTHER) == (rows, 0)


def _cost(person: uuid.UUID, cls: str, day_offset: int = 0) -> LedgerFact:
    return LedgerFact(
        day=FIRST + timedelta(days=day_offset),
        person_id=person,
        attribution="user",
        agent_id=None if cls == CLASS_PLATFORM else AGENTS[1],
        fleet_id=None,
        cost_class=cls,
        cost=1.0,
    )


def test_by_source_is_not_split_when_one_named_row_would_be_left() -> None:
    many = [uuid.UUID(int=500 + i) for i in range(6)]
    facts = [_cost(p, CLASS_AUTOMATION) for p in many] + [_cost(many[0], CLASS_CHAT), _cost(many[1], CLASS_PLATFORM)]
    figures = _figures(facts, SCOPE_ORGANIZATION, Viewer(user_id=PEOPLE[3], min_group_size=5))
    assert [r.key for r in figures.by_source] == [ROW_OTHER_SOURCES]
    assert figures.suppressed is not None and figures.suppressed.sources_split is False
    assert _close(figures.by_source[0].totals, figures.totals)


def test_an_under_k_week_merges_with_the_next_and_the_last_with_the_previous() -> None:
    weeks = [_row("2026-10-05", 6), _row("2026-10-12", 1), _row("2026-10-19", 6), _row("2026-10-26", 1)]
    out, merges = merge_weeks(weeks, 5)
    assert merges == 2
    assert [(r.key, r.until) for r in out] == [("2026-10-05", None), ("2026-10-12", "2026-10-26")]


def test_every_week_merged_leaves_one_point_never_none() -> None:
    weeks = [_row("2026-10-05", 1), _row("2026-10-12", 1)]
    out, merges = merge_weeks(weeks, 5)
    assert merges == 1 and len(out) == 1
    assert out[0].until == "2026-10-12"


def test_an_agent_under_k_answers_the_viewers_own_share_only() -> None:
    me, other = PEOPLE[2], PEOPLE[3]
    facts = [_cost(me, CLASS_AUTOMATION), _cost(other, CLASS_AUTOMATION), _cost(other, CLASS_CHAT)]
    viewer = Viewer(user_id=me, agent_ids=frozenset({AGENTS[1]}), min_group_size=5)
    figures = _figures(facts, SCOPE_AGENT, viewer, scope_id=AGENTS[1])
    assert figures.suppressed is not None and figures.suppressed.own_share_only is True
    assert figures.totals.cost == pytest.approx(1.0)
    assert figures.totals.people == {me}


def test_f_the_serialized_non_administrator_answer_names_nobody_else() -> None:
    facts = _fixture(0)
    for person in PEOPLE[1:]:
        for scope, scope_id in _scopes():
            answer = json.dumps(_figures(facts, scope, _viewer(person, k=5), scope_id=scope_id).as_dict())
            for other in PEOPLE:
                if other != person:
                    assert str(other) not in answer
            assert "people" not in answer


def test_the_suppressed_trace_is_answered_for_a_non_administrator_only() -> None:
    facts = _fixture(0)
    seen = _figures(facts, SCOPE_ORGANIZATION, _viewer(PEOPLE[3], k=5)).as_dict()
    assert seen["suppressed"]["k"] == 5
    assert set(seen["suppressed"]) == {"k", "groups", "own_share_only", "sources_split", "weeks_merged"}
    assert _figures(facts, SCOPE_ORGANIZATION, _viewer(ADMIN, k=5)).as_dict()["suppressed"] is None
