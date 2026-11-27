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

"""#2639: an outside agent supervising a fleet stays out of the fleet's figures, shown apart.

Pure: ``roi_figures.compute`` on seeded facts, no PostgreSQL.
"""

from __future__ import annotations

import uuid
from datetime import date

from app.services.roi_figures import (
    BASIS_FLEET_MEMBERS,
    BASIS_ORGANIZATION,
    CLASS_AUTOMATION,
    SCOPE_AGENT,
    SCOPE_FLEET,
    SCOPE_ORGANIZATION,
    LedgerFact,
    Viewer,
    compute,
    compute_subjects,
)

DAY = date(2026, 11, 27)
FLEET = uuid.UUID(int=100)
OTHER_FLEET = uuid.UUID(int=101)
MEMBER = uuid.UUID(int=200)
SUPERVISOR = uuid.UUID(int=300)
PEOPLE = [uuid.UUID(int=i + 1) for i in range(6)]
ADMIN = Viewer(user_id=PEOPLE[0], organization=True)


def _fact(
    agent: uuid.UUID, fleet: uuid.UUID | None, person: uuid.UUID, *, cost: float = 0.0, value: float = 0.0
) -> LedgerFact:
    return LedgerFact(
        day=DAY,
        person_id=person,
        attribution="user",
        agent_id=agent,
        fleet_id=fleet,
        cost_class=CLASS_AUTOMATION,
        runs=1 if value else 0,
        value_low=value,
        value_likely=value,
        value_high=value,
        cost=cost,
    )


def _facts(supervisor_fleet: uuid.UUID | None = None) -> list[LedgerFact]:
    fleet_runs = [_fact(MEMBER, FLEET, p, cost=1.0, value=10.0) for p in PEOPLE]
    # A supervisor checking every 15 minutes: many cheap runs, no value, its own home fleet (or none).
    watching = [_fact(SUPERVISOR, supervisor_fleet, p, cost=0.5) for p in PEOPLE for _ in range(4)]
    return fleet_runs + watching


def _fleet(
    facts: list[LedgerFact], viewer: Viewer = ADMIN, supervisors: frozenset[uuid.UUID] = frozenset({SUPERVISOR})
):
    return compute(
        facts,
        scope=SCOPE_FLEET,
        viewer=viewer,
        scope_id=FLEET,
        window_first=DAY,
        window_last=DAY,
        supervisor_ids=supervisors,
    )


def test_a_supervisor_never_changes_the_fleet_roi_or_cost_per_run() -> None:
    alone = _fleet([f for f in _facts() if f.agent_id == MEMBER], supervisors=frozenset())
    watched = _fleet(_facts())
    assert watched.totals.cost == alone.totals.cost == 6.0
    assert watched.totals.runs == alone.totals.runs == 6
    assert watched.as_dict()["totals"]["roi"] == alone.as_dict()["totals"]["roi"]
    assert all(row.key != str(SUPERVISOR) for row in watched.by_agent)


def test_the_supervision_shows_apart_with_its_own_cost() -> None:
    answer = _fleet(_facts()).as_dict()
    assert answer["basis"] == BASIS_FLEET_MEMBERS
    assert answer["supervision"]["agents"] == 1
    assert answer["supervision"]["ai_cost"] == 12.0


def test_a_supervisor_with_its_own_home_fleet_stays_out_too() -> None:
    answer = _fleet(_facts(supervisor_fleet=OTHER_FLEET)).as_dict()
    assert answer["totals"]["ai_cost"] == 6.0
    assert answer["supervision"]["ai_cost"] == 12.0


def test_the_organization_total_still_includes_the_supervision() -> None:
    org = compute(_facts(), scope=SCOPE_ORGANIZATION, viewer=ADMIN, window_first=DAY, window_last=DAY)
    assert org.totals.cost == 18.0
    assert org.as_dict()["basis"] == BASIS_ORGANIZATION
    assert org.as_dict()["supervision"] is None
    assert any(row.key == str(SUPERVISOR) for row in org.by_agent)


def test_a_home_member_named_as_supervisor_stays_in_the_fleet() -> None:
    answer = _fleet(_facts(), supervisors=frozenset({MEMBER})).as_dict()
    assert answer["totals"]["ai_cost"] == 6.0
    assert answer["supervision"] is None


def test_no_supervisor_answers_no_line() -> None:
    assert _fleet(_facts(), supervisors=frozenset()).as_dict()["supervision"] is None
    agent = compute(_facts(), scope=SCOPE_AGENT, viewer=ADMIN, scope_id=MEMBER, window_first=DAY, window_last=DAY)
    assert agent.as_dict()["supervision"] is None


def test_an_under_k_supervision_line_is_hidden_from_a_non_administrator() -> None:
    reader = Viewer(user_id=PEOPLE[1], fleet_ids=frozenset({FLEET}), agent_ids=frozenset({MEMBER}), min_group_size=5)
    few = [f for f in _facts() if f.agent_id == MEMBER] + [_fact(SUPERVISOR, None, PEOPLE[1], cost=0.5)]
    assert _fleet(few, viewer=reader).supervision is None
    assert _fleet(_facts(), viewer=reader).supervision is not None


def test_the_list_column_carries_the_same_supervision_line() -> None:
    by_id = compute_subjects(
        _facts(),
        subject_type=SCOPE_FLEET,
        ids=[FLEET],
        viewer=ADMIN,
        window_first=DAY,
        window_last=DAY,
        supervisors={FLEET: frozenset({SUPERVISOR})},
    )
    assert by_id[FLEET].as_dict()["supervision"] == _fleet(_facts()).as_dict()["supervision"]
