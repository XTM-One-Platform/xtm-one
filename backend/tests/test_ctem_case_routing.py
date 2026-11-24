"""The CTEM loop owns every case; a use-case fleet takes its paths only while active (#1390).

dev-docs/ctem-fleets.md sections 4 and 14: the routing table, the owner stamped on
the case events at publish, the catch-up of the open cases nobody ran (on enable
and on the case sweep), idempotent and bounded.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from app.models.ctem_signal import TRIGGER_PATHS
from app.services import ctem_case_routing as routing

LOOP = "ctem-loop"
VULN = "ctem-vuln-prioritization"


@pytest.mark.parametrize("path", TRIGGER_PATHS)
def test_the_loop_owns_every_path_when_no_use_case_fleet_is_active(path: str) -> None:
    assert routing.owner_of(path, set()) == LOOP
    assert routing.owner_of(path, {LOOP}) == LOOP


def test_the_vulnerability_fleet_owns_new_vulnerability_only_while_active() -> None:
    assert routing.owner_of("new_vulnerability", {VULN}) == VULN
    assert routing.owner_of("new_vulnerability", {LOOP, VULN}) == VULN
    assert routing.owner_of("new_vulnerability", {LOOP}) == LOOP
    for path in TRIGGER_PATHS:
        if path != "new_vulnerability":
            assert routing.owner_of(path, {LOOP, VULN}) == LOOP, path


def test_an_unknown_or_missing_path_goes_to_the_loop() -> None:
    assert routing.owner_of(None, {VULN}) == LOOP
    assert routing.owner_of("something_new", {VULN}) == LOOP


async def test_stamp_owners_writes_the_owner_on_case_events_only() -> None:
    datas: list[dict[str, Any]] = [
        {"case_id": "a", "trigger_path": "new_vulnerability"},
        {"case_id": "b", "trigger_path": "relevant_threat"},
        {"case_id": "c", "trigger_path": "new_vulnerability", "owner_fleet": LOOP},
        {"not_a_case": True},
    ]
    await routing.stamp_owners(datas, active={VULN})
    assert datas[0]["owner_fleet"] == VULN
    assert datas[1]["owner_fleet"] == LOOP
    assert datas[2]["owner_fleet"] == LOOP  # a catch-up owner is kept
    assert "owner_fleet" not in datas[3]


async def test_stamp_owners_leaves_the_event_unstamped_when_activation_is_unreadable(monkeypatch) -> None:
    async def boom() -> set[str]:
        raise RuntimeError("db down")

    monkeypatch.setattr(routing, "_active_now", boom)
    data = {"case_id": "a", "trigger_path": "new_vulnerability"}
    await routing.stamp_owners([data])
    assert "owner_fleet" not in data


def _case(severity: str | None, due_hours: float | None, path: str = "new_vulnerability") -> Any:
    now = datetime(2026, 11, 23, 8, 0, tzinfo=UTC)
    return SimpleNamespace(
        id=uuid.uuid4(),
        severity=severity,
        sla_due_at=None if due_hours is None else now + timedelta(hours=due_hours),
        trigger_path=path,
        state="new",
        vertical="cyber",
    )


class _Result:
    def __init__(self, rows: list[Any], *, scalars: bool) -> None:
        self._rows = rows
        self._scalars = scalars

    def scalars(self) -> _Result:
        return self

    def all(self) -> list[Any]:
        return list(self._rows)


class _Db:
    """Answers the three reads plan_catch_up makes: open cases, owner_requested events, open items."""

    def __init__(self, cases: list[Any], asked: list[tuple[Any, str]], open_keys: list[str]) -> None:
        self.cases, self.asked, self.open_keys = cases, asked, open_keys
        self.calls = 0

    async def execute(self, _stmt: Any) -> _Result:
        self.calls += 1
        if self.calls == 1:
            return _Result(self.cases, scalars=True)
        if self.calls == 2:
            return _Result(self.asked, scalars=False)
        return _Result([(k,) for k in self.open_keys], scalars=False)


def _active(monkeypatch, fleets: set[str]) -> None:
    async def active(_db: Any) -> set[str]:
        return set(fleets)

    monkeypatch.setattr(routing, "active_case_fleets", active)


async def test_catch_up_hands_every_open_case_once_most_urgent_first(monkeypatch) -> None:
    _active(monkeypatch, {LOOP})
    p3, p1_late, p1_soon, p2 = _case("P3", 1), _case("P1", 10), _case("P1", 2), _case("P2", 1)
    db = _Db([p3, p1_late, p1_soon, p2], asked=[], open_keys=[])
    planned = await routing.plan_catch_up(db)  # type: ignore[arg-type]
    assert [c for c, _ in planned] == [p1_soon, p1_late, p2, p3]
    assert {o for _, o in planned} == {LOOP}


async def test_a_second_pass_hands_nothing(monkeypatch) -> None:
    _active(monkeypatch, {LOOP})
    a, b = _case("P1", 1), _case("P2", 1)
    asked = [(a.id, LOOP), (b.id, LOOP)]
    db = _Db([a, b], asked=asked, open_keys=[])
    assert await routing.plan_catch_up(db) == []  # type: ignore[arg-type]


async def test_a_case_with_an_open_item_of_any_fleet_is_left_alone(monkeypatch) -> None:
    _active(monkeypatch, {LOOP})
    a, b = _case("P1", 1), _case("P1", 2)
    db = _Db([a, b], asked=[], open_keys=[f"ctem:f2:case:{a.id}"])
    planned = await routing.plan_catch_up(db)  # type: ignore[arg-type]
    assert [c for c, _ in planned] == [b]


async def test_a_case_asked_of_another_fleet_is_handed_to_its_new_owner(monkeypatch) -> None:
    # The vulnerability fleet was switched off: its case moves to the loop once.
    _active(monkeypatch, {LOOP})
    a = _case("P1", 1)
    db = _Db([a], asked=[(a.id, VULN)], open_keys=[])
    planned = await routing.plan_catch_up(db)  # type: ignore[arg-type]
    assert planned == [(a, LOOP)]


async def test_no_active_fleet_hands_nothing(monkeypatch) -> None:
    _active(monkeypatch, set())
    db = _Db([_case("P1", 1)], asked=[], open_keys=[])
    assert await routing.plan_catch_up(db) == []  # type: ignore[arg-type]
    assert db.calls == 0


async def test_the_enabled_fleet_only_takes_its_own_cases(monkeypatch) -> None:
    _active(monkeypatch, {LOOP, VULN})
    vuln, threat = _case("P1", 1), _case("P1", 2, path="relevant_threat")
    db = _Db([vuln, threat], asked=[], open_keys=[])
    planned = await routing.plan_catch_up(db, only={VULN})  # type: ignore[arg-type]
    assert planned == [(vuln, VULN)]


async def test_the_pass_is_bounded(monkeypatch) -> None:
    _active(monkeypatch, {LOOP})
    cases = [_case("P2", i) for i in range(5)]
    db = _Db(cases, asked=[], open_keys=[])
    planned = await routing.plan_catch_up(db, limit=2)  # type: ignore[arg-type]
    assert len(planned) == 2


async def test_catch_up_after_enable_ignores_other_fleets(monkeypatch) -> None:
    called: list[Any] = []

    async def fake(**kwargs: Any) -> int:
        called.append(kwargs)
        return 3

    monkeypatch.setattr(routing, "catch_up", fake)
    assert await routing.catch_up_after_enable("some-other-fleet") == 0
    assert await routing.catch_up_after_enable(None) == 0
    assert called == []
    assert await routing.catch_up_after_enable(LOOP) == 3
    assert called == [{"only": {LOOP}}]


def test_the_catch_up_event_id_is_stable_per_case_and_owner() -> None:
    cid = uuid.uuid4()
    assert routing._catch_up_event_id(cid, LOOP) == routing._catch_up_event_id(cid, LOOP)
    assert routing._catch_up_event_id(cid, LOOP) != routing._catch_up_event_id(cid, VULN)
