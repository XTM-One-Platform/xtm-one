"""The card facts of the Fleets list (#9977 S2): ``fleet_views.fleet_card_facts``.

Three reads whatever the list's size (home members, their assignments, their
newest visible run), so the list never asks one health per card and starts no
run. The session is a stub answering the three queries in order.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.services.fleet_views import AutomationCounts, FleetCardFacts, fleet_card_facts


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return list(self._rows)


class _Session:
    def __init__(self, *answers):
        self._answers = list(answers)
        self.calls = 0

    async def execute(self, _query):
        self.calls += 1
        return _Result(self._answers.pop(0))


def _fleet(**kw):
    return SimpleNamespace(id=uuid.uuid4(), is_builtin=False, **kw)


@pytest.mark.asyncio
async def test_an_empty_list_reads_nothing():
    db = _Session()
    assert await fleet_card_facts(db, [], run_scope=True) == {}
    assert db.calls == 0


@pytest.mark.asyncio
async def test_a_fleet_with_no_home_member_is_not_set_up_and_never_run():
    fleet = _fleet()
    db = _Session([])
    facts = await fleet_card_facts(db, [fleet], run_scope=True)
    assert facts == {fleet.id: FleetCardFacts()}
    assert db.calls == 1


@pytest.mark.asyncio
async def test_each_fleet_counts_its_own_members_and_dates_its_newest_run():
    a, b = _fleet(), _fleet()
    x, y, z = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    early = datetime(2026, 11, 1, tzinfo=UTC)
    late = datetime(2026, 11, 7, tzinfo=UTC)
    db = _Session(
        [(a.id, x), (a.id, y), (b.id, z)],
        [
            (x, None, False, "Daily triage", True, None),
            (y, None, False, "Weekly report", False, "fleet_pause"),
            (z, None, False, "Draft", False, "created_disabled"),
        ],
        [(x, early), (y, late)],
    )
    facts = await fleet_card_facts(db, [a, b], run_scope=True)
    assert db.calls == 3
    assert facts[a.id].automations.total == 2
    assert facts[a.id].automations.enabled == 1
    assert facts[a.id].last_run_at == late
    assert facts[b.id].automations == AutomationCounts(total=1, enabled=0, paused=0)
    assert facts[b.id].last_run_at is None
