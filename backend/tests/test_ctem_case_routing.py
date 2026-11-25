"""Which fleet runs a CTEM case: declared on the fleet, exactly one owner (#1390, #1966).

dev-docs/ctem-fleets.md sections 4 and 14: the routing precedence, the owner
stamped on the case events at publish, the catch-up of the open cases nobody
ran (on enable, pause, reset and on the case sweep), idempotent and bounded,
and the listener filter a fork's case automation gets.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from app.agents.builtin_agents import CTEM_F1_CASE_ASSIGNMENT, CTEM_F2_CASE_ASSIGNMENT
from app.models.ctem_signal import TRIGGER_PATHS
from app.services import ctem_case_routing as routing
from app.services.integration_event_filters import payload_filter_reason

LOOP = "ctem-loop"
VULN = "ctem-vuln-prioritization"
_T0 = datetime(2026, 1, 1, tzinfo=UTC)


def _fleet(
    token: str,
    paths: tuple[str, ...] | None,
    *,
    builtin: bool,
    active: bool = True,
    key: str | None = None,
    age_days: int = 0,
) -> routing.CaseFleet:
    return routing.CaseFleet(
        token=token,
        fleet_id=str(uuid.uuid4()),
        slug=token.removeprefix(routing.CUSTOM_OWNER_PREFIX),
        name=token,
        paths=paths,
        key=key or f"ctem:{token}",
        is_builtin=builtin,
        created_at=_T0 + timedelta(days=age_days),
        active=active,
    )


def _loop(active: bool = True) -> routing.CaseFleet:
    return _fleet(LOOP, None, builtin=True, active=active, key="ctem:f1")


def _vuln(active: bool = True) -> routing.CaseFleet:
    return _fleet(VULN, ("new_vulnerability",), builtin=True, active=active, key="ctem:f2", age_days=1)


FORK = "fleet:fork-of-loop"


def _fork(active: bool = True, paths: tuple[str, ...] | None = None, age_days: int = 10) -> routing.CaseFleet:
    return _fleet(FORK, paths, builtin=False, active=active, age_days=age_days)


@pytest.mark.parametrize("path", TRIGGER_PATHS)
def test_the_loop_owns_every_path_when_no_other_fleet_is_active(path: str) -> None:
    assert routing.owner_of(path, []) == LOOP
    assert routing.owner_of(path, [_loop()]) == LOOP
    assert routing.owner_of(path, [_loop(), _vuln(active=False)]) == LOOP


def test_the_vulnerability_fleet_owns_new_vulnerability_only_while_active() -> None:
    assert routing.owner_of("new_vulnerability", [_loop(active=False), _vuln()]) == VULN
    assert routing.owner_of("new_vulnerability", [_loop(), _vuln()]) == VULN
    assert routing.owner_of("new_vulnerability", [_loop(), _vuln(active=False)]) == LOOP
    for path in TRIGGER_PATHS:
        if path != "new_vulnerability":
            assert routing.owner_of(path, [_loop(), _vuln()]) == LOOP, path


def test_an_unknown_or_missing_path_goes_to_the_loop() -> None:
    assert routing.owner_of(None, [_vuln()]) == LOOP
    assert routing.owner_of("something_new", [_vuln()]) == LOOP


@pytest.mark.parametrize("loop_on", [True, False])
@pytest.mark.parametrize("path", TRIGGER_PATHS)
def test_an_enabled_fork_of_the_loop_takes_every_path_from_it(path: str, loop_on: bool) -> None:
    fleets = [_loop(active=loop_on), _fork()]
    assert routing.owner_of(path, fleets) == FORK


def test_a_disabled_fork_hands_its_paths_back() -> None:
    assert routing.owner_of("relevant_threat", [_loop(), _fork(active=False)]) == LOOP


def test_a_specific_path_beats_every_path_even_against_a_custom_fleet() -> None:
    # The fork takes every path; the built-in vulnerability fleet declares path 1.
    fleets = [_loop(), _vuln(), _fork()]
    assert routing.owner_of("new_vulnerability", fleets) == VULN
    assert routing.owner_of("relevant_threat", fleets) == FORK


def test_a_fork_owning_only_new_vulnerability_beside_the_loop() -> None:
    fleets = [_loop(), _vuln(), _fork(paths=("new_vulnerability",))]
    assert routing.owner_of("new_vulnerability", fleets) == FORK
    assert routing.owner_of("relevant_threat", fleets) == LOOP
    fleets = [_loop(), _vuln(active=False), _fork(paths=("new_vulnerability",))]
    assert routing.owner_of("new_vulnerability", fleets) == FORK


def test_among_equal_custom_fleets_the_oldest_wins() -> None:
    old = _fleet("fleet:old", None, builtin=False, age_days=5)
    new = _fleet("fleet:new", None, builtin=False, age_days=9)
    assert routing.owner_of("relevant_threat", [_loop(), new, old]) == "fleet:old"


def test_token_of_uses_the_slug_of_a_builtin_fleet_and_the_id_of_any_other() -> None:
    fid = uuid.uuid4()
    assert routing.token_of(SimpleNamespace(is_builtin=True, slug=LOOP, id=fid)) == LOOP
    assert routing.token_of(SimpleNamespace(is_builtin=False, slug=LOOP, id=fid)) == f"fleet:{fid}"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, None),
        ({}, None),
        ({"paths": None, "key": "ctem:f1"}, (None, "ctem:f1")),
        ({"paths": ["new_vulnerability"], "key": "k"}, (("new_vulnerability",), "k")),
        ({"paths": [], "key": "k"}, None),
        ({"paths": None}, None),
        ({"paths": "new_vulnerability", "key": "k"}, None),
    ],
)
def test_parse_declaration(value: Any, expected: Any) -> None:
    assert routing.parse_declaration(value) == expected


@pytest.mark.parametrize("path", TRIGGER_PATHS)
@pytest.mark.parametrize(
    "fleets",
    [
        [],
        [_loop()],
        [_loop(), _vuln()],
        [_loop(), _fork()],
        [_loop(active=False), _fork()],
        [_loop(), _vuln(), _fork()],
        [_loop(), _vuln(), _fork(paths=("new_vulnerability",))],
        [_loop(), _fork(active=False)],
    ],
)
def test_every_case_event_is_heard_by_exactly_one_listener(path: str, fleets: list[routing.CaseFleet]) -> None:
    payload = {"case_id": "c", "trigger_path": path, routing.OWNER_KEY: routing.owner_of(path, fleets)}
    filters = {
        LOOP: CTEM_F1_CASE_ASSIGNMENT["trigger_config"]["filter"],
        VULN: CTEM_F2_CASE_ASSIGNMENT["trigger_config"]["filter"],
        FORK: routing.listener_filter(FORK),
    }
    heard = [t for t, f in filters.items() if payload_filter_reason(payload, f) is None]
    assert len(heard) == 1, (path, heard)


def test_an_unstamped_case_event_goes_to_the_loop_only() -> None:
    payload = {"case_id": "c", "trigger_path": "new_vulnerability"}
    assert payload_filter_reason(payload, CTEM_F1_CASE_ASSIGNMENT["trigger_config"]["filter"]) is None
    assert payload_filter_reason(payload, CTEM_F2_CASE_ASSIGNMENT["trigger_config"]["filter"]) is not None
    assert payload_filter_reason(payload, routing.listener_filter(FORK)) is not None


async def test_stamp_owners_writes_the_owner_on_case_events_only() -> None:
    datas: list[dict[str, Any]] = [
        {"case_id": "a", "trigger_path": "new_vulnerability"},
        {"case_id": "b", "trigger_path": "relevant_threat"},
        {"case_id": "c", "trigger_path": "new_vulnerability", "owner_fleet": LOOP},
        {"not_a_case": True},
    ]
    await routing.stamp_owners(datas, fleets=[_loop(), _vuln(), _fork()])
    assert datas[0]["owner_fleet"] == VULN
    assert datas[1]["owner_fleet"] == FORK
    assert datas[2]["owner_fleet"] == LOOP  # a catch-up owner is kept
    assert "owner_fleet" not in datas[3]


async def test_stamp_owners_leaves_the_event_unstamped_when_the_routing_is_unreadable(monkeypatch) -> None:
    async def boom() -> list[routing.CaseFleet]:
        raise RuntimeError("db down")

    monkeypatch.setattr(routing, "_fleets_now", boom)
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
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def scalars(self) -> _Result:
        return self

    def all(self) -> list[Any]:
        return list(self._rows)


class _Db:
    """Answers the reads plan_catch_up makes: open cases, owner_requested events, case runs, open items.

    An ask given as ``(case id, owner)`` was made just now; ``(case id, owner, at)`` at *at*.
    *runs* are ``(case id, created_at)`` (an ended run) or ``(case id, created_at, status)`` of the case automation's runs.
    """

    def __init__(
        self,
        cases: list[Any],
        asked: list[tuple[Any, ...]],
        open_keys: list[str],
        runs: list[tuple[Any, ...]] | None = None,
    ) -> None:
        now = datetime.now(UTC)
        self.cases, self.open_keys = cases, open_keys
        self.runs = [r if len(r) == 3 else (*r, "completed") for r in runs or []]
        self.asked = [a if len(a) == 3 else (*a, now) for a in asked]
        self.calls = 0
        self.statements: list[Any] = []

    async def execute(self, stmt: Any) -> _Result:
        self.calls += 1
        self.statements.append(stmt)
        table = list(stmt.selected_columns)[-1].table.name
        if table == "ctem_cases":
            return _Result(self.cases)
        if table == "ctem_case_events":
            return _Result(self.asked)
        if table == "assignment_runs":
            return _Result(self.runs)
        return _Result([(k,) for k in self.open_keys])


def _fleets(monkeypatch, fleets: list[routing.CaseFleet]) -> None:
    async def read(_db: Any) -> list[routing.CaseFleet]:
        return list(fleets)

    monkeypatch.setattr(routing, "case_fleets", read)


async def test_catch_up_hands_every_open_case_once_most_urgent_first(monkeypatch) -> None:
    _fleets(monkeypatch, [_loop()])
    p3, p1_late, p1_soon, p2 = _case("P3", 1), _case("P1", 10), _case("P1", 2), _case("P2", 1)
    db = _Db([p3, p1_late, p1_soon, p2], asked=[], open_keys=[])
    planned = await routing.plan_catch_up(db)  # type: ignore[arg-type]
    assert [c for c, _ in planned] == [p1_soon, p1_late, p2, p3]
    assert {o for _, o in planned} == {LOOP}


async def test_a_second_pass_hands_nothing(monkeypatch) -> None:
    _fleets(monkeypatch, [_loop()])
    a, b = _case("P1", 1), _case("P2", 1)
    db = _Db([a, b], asked=[(a.id, LOOP), (b.id, LOOP)], open_keys=[])
    assert await routing.plan_catch_up(db) == []  # type: ignore[arg-type]


async def test_a_case_with_an_open_item_of_any_declared_fleet_is_left_alone(monkeypatch) -> None:
    # The fork took over; the loop still runs case a: no second run on the fork.
    _fleets(monkeypatch, [_loop(), _fork()])
    a, b = _case("P1", 1), _case("P1", 2)
    db = _Db([a, b], asked=[], open_keys=[f"ctem:f1:case:{a.id}"])
    planned = await routing.plan_catch_up(db)  # type: ignore[arg-type]
    assert planned == [(b, FORK)]


async def test_every_case_with_an_open_item_is_left_alone_not_only_the_first(monkeypatch: pytest.MonkeyPatch) -> None:
    # The vulnerability fleet enabled while the loop runs three of its cases: no second run on any of them.
    _fleets(monkeypatch, [_loop(), _vuln()])
    cases = [_case("P1", 1), _case("P1", 2), _case("P2", 1), _case("P3", 1)]
    db = _Db(cases, asked=[], open_keys=[f"ctem:f1:case:{c.id}" for c in cases[:3]])
    assert await routing.plan_catch_up(db) == [(cases[3], VULN)]  # type: ignore[arg-type]


async def test_cases_opened_or_breached_while_every_owner_was_paused_go_to_the_first_one_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    breached, soon, later, unscored = _case("P1", -3), _case("P1", 4), _case("P2", 5), _case(None, None)
    threat = _case("P3", -1, path="relevant_threat")
    cases = [later, breached, threat, soon, unscored]
    _fleets(monkeypatch, [_loop(active=False), _vuln(active=False)])
    assert await routing.plan_catch_up(_Db(cases, asked=[], open_keys=[])) == []  # type: ignore[arg-type]
    _fleets(monkeypatch, [_loop(), _vuln(active=False)])
    planned = await routing.plan_catch_up(_Db(cases, asked=[], open_keys=[]))  # type: ignore[arg-type]
    assert planned == [(c, LOOP) for c in (breached, soon, later, threat, unscored)]


async def test_enabling_a_fork_hands_it_the_cases_the_loop_never_ran(monkeypatch) -> None:
    _fleets(monkeypatch, [_loop(), _fork()])
    a = _case("P1", 1, path="relevant_threat")
    db = _Db([a], asked=[], open_keys=[])
    assert await routing.plan_catch_up(db) == [(a, FORK)]  # type: ignore[arg-type]


async def test_disabling_the_fork_hands_its_cases_back_to_the_loop(monkeypatch) -> None:
    _fleets(monkeypatch, [_loop(), _fork(active=False)])
    a = _case("P1", 1)
    db = _Db([a], asked=[(a.id, FORK)], open_keys=[])
    assert await routing.plan_catch_up(db) == [(a, LOOP)]  # type: ignore[arg-type]


async def test_no_active_fleet_hands_nothing(monkeypatch) -> None:
    _fleets(monkeypatch, [_loop(active=False)])
    db = _Db([_case("P1", 1)], asked=[], open_keys=[])
    assert await routing.plan_catch_up(db) == []  # type: ignore[arg-type]
    assert db.calls == 0


async def test_only_narrows_to_the_named_owners(monkeypatch) -> None:
    _fleets(monkeypatch, [_loop(), _vuln()])
    vuln, threat = _case("P1", 1), _case("P1", 2, path="relevant_threat")
    db = _Db([vuln, threat], asked=[], open_keys=[])
    planned = await routing.plan_catch_up(db, only={VULN})  # type: ignore[arg-type]
    assert planned == [(vuln, VULN)]


async def test_the_pass_is_bounded(monkeypatch) -> None:
    _fleets(monkeypatch, [_loop()])
    cases = [_case("P2", i) for i in range(5)]
    db = _Db(cases, asked=[], open_keys=[])
    planned = await routing.plan_catch_up(db, limit=2)  # type: ignore[arg-type]
    assert len(planned) == 2


async def test_catch_up_after_change_ignores_a_fleet_running_no_case(monkeypatch) -> None:
    called: list[Any] = []

    async def fake(**kwargs: Any) -> int:
        called.append(kwargs)
        return 3

    monkeypatch.setattr(routing, "catch_up", fake)
    assert await routing.catch_up_after_change(SimpleNamespace(case_ownership=None)) == 0
    assert called == []
    fleet = SimpleNamespace(case_ownership={"paths": None, "key": "ctem:f1"})
    assert await routing.catch_up_after_change(fleet) == 3
    assert called == [{}]


async def test_point_case_listeners_rewrites_the_filter_to_the_fleet_token() -> None:
    fid = uuid.uuid4()
    row = SimpleNamespace(trigger_config={"event": ["ctem_case.opened"], "filter": {"owner_fleet": {"eq": LOOP}}})

    class _Rows:
        async def execute(self, _stmt: Any) -> _Result:
            return _Result([row])

    fleet = SimpleNamespace(is_builtin=False, slug="my-loop", id=fid)
    assert await routing.point_case_listeners(_Rows(), fleet, [uuid.uuid4()]) == 1  # type: ignore[arg-type]
    assert row.trigger_config == {"event": ["ctem_case.opened"], "filter": {"owner_fleet": {"eq": f"fleet:{fid}"}}}
    assert await routing.point_case_listeners(_Rows(), fleet, []) == 0  # type: ignore[arg-type]


def test_the_catch_up_event_id_is_stable_per_case_and_owner() -> None:
    cid = uuid.uuid4()
    assert routing._catch_up_event_id(cid, LOOP) == routing._catch_up_event_id(cid, LOOP)
    assert routing._catch_up_event_id(cid, LOOP) != routing._catch_up_event_id(cid, FORK)


def test_an_ask_made_again_gets_a_new_event_id() -> None:
    # The run dedupe of the first ask's event must never swallow the second (#2101).
    cid = uuid.uuid4()
    first = routing._catch_up_event_id(cid, LOOP)
    assert routing._catch_up_event_id(cid, LOOP, 0) == first
    assert len({first, routing._catch_up_event_id(cid, LOOP, 1), routing._catch_up_event_id(cid, LOOP, 2)}) == 3


# -- An ask nobody answered is made again (#2101) -------------------------------

_NOW = datetime(2026, 11, 25, 12, 0, tzinfo=UTC)
_OLD = _NOW - timedelta(seconds=routing.REASK_AFTER_SECONDS + 60)


async def test_an_unanswered_ask_is_made_again_on_a_later_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    # Case x was asked, its event never became a run of the owner's case automation.
    _fleets(monkeypatch, [_loop()])
    x = _case("P1", 1)
    db = _Db([x], asked=[(x.id, LOOP, _OLD)], open_keys=[])
    assert await routing.plan_catch_up_asks(db, now=_NOW) == [(x, LOOP, 1)]  # type: ignore[arg-type]


async def test_an_answered_ask_is_not_made_again(monkeypatch: pytest.MonkeyPatch) -> None:
    # Case y got its run after the ask (its item since closed or not yet open): not raised again.
    _fleets(monkeypatch, [_loop()])
    x, y = _case("P1", 1), _case("P1", 2)
    db = _Db(
        [x, y],
        asked=[(x.id, LOOP, _OLD), (y.id, LOOP, _OLD)],
        open_keys=[],
        runs=[(str(y.id), _OLD + timedelta(seconds=5))],
    )
    assert await routing.plan_catch_up_asks(db, now=_NOW) == [(x, LOOP, 1)]  # type: ignore[arg-type]


async def test_a_run_expired_before_start_does_not_answer_an_ask() -> None:
    # #2234: a case-open run that expired in the queue never ran its case: the next pass asks again.
    from sqlalchemy.dialects import postgresql

    db = _Db([], asked=[], open_keys=[])
    await routing._answered_case_ids(db, {str(uuid.uuid4()): _OLD})  # type: ignore[arg-type]
    statement: Any = db.statements[-1]
    dialect = postgresql.dialect()  # type: ignore[no-untyped-call]
    sql = str(statement.compile(dialect=dialect, compile_kwargs={"literal_binds": True}))
    assert "stop_reason" in sql and "expired_not_started" in sql


async def test_an_ended_run_before_the_ask_does_not_answer_it(monkeypatch: pytest.MonkeyPatch) -> None:
    _fleets(monkeypatch, [_loop()])
    x = _case("P1", 1)
    db = _Db([x], asked=[(x.id, LOOP, _OLD)], open_keys=[], runs=[(str(x.id), _OLD - timedelta(hours=1))])
    assert await routing.plan_catch_up_asks(db, now=_NOW) == [(x, LOOP, 1)]  # type: ignore[arg-type]


# -- No ask while the case's own run waits or works (#2319) ----------------------

_WAITING = ("queued", "pending", "delayed", "running", "awaiting_human")


@pytest.mark.parametrize("status", _WAITING)
async def test_a_first_ask_is_not_made_while_the_case_run_waits_or_works(
    monkeypatch: pytest.MonkeyPatch, status: str
) -> None:
    # Case x was opened, its ctem_case.opened run is queued or running and has no case item yet.
    _fleets(monkeypatch, [_loop()])
    x, y = _case("P1", 1), _case("P1", 2)
    db = _Db([x, y], asked=[], open_keys=[], runs=[(str(x.id), _NOW - timedelta(minutes=5), status)])
    assert await routing.plan_catch_up_asks(db, now=_NOW) == [(y, LOOP, 0)]  # type: ignore[arg-type]


@pytest.mark.parametrize("status", _WAITING)
async def test_a_run_before_the_ask_still_waiting_or_working_answers_it(
    monkeypatch: pytest.MonkeyPatch, status: str
) -> None:
    _fleets(monkeypatch, [_loop()])
    x = _case("P1", 1)
    db = _Db([x], asked=[(x.id, LOOP, _OLD)], open_keys=[], runs=[(str(x.id), _OLD - timedelta(hours=1), status)])
    assert await routing.plan_catch_up_asks(db, now=_NOW) == []  # type: ignore[arg-type]


async def test_the_runs_read_takes_every_waiting_run_whatever_its_age() -> None:
    from sqlalchemy.dialects import postgresql

    db = _Db([], asked=[], open_keys=[])
    await routing._answered_case_ids(db, {str(uuid.uuid4()): None})  # type: ignore[arg-type]
    dialect = postgresql.dialect()  # type: ignore[no-untyped-call]
    sql = str(db.statements[-1].compile(dialect=dialect, compile_kwargs={"literal_binds": True}))
    assert "'queued'" in sql and "'running'" in sql and "created_at >=" not in sql


async def test_a_case_whose_only_run_ended_without_answer_is_still_asked(monkeypatch: pytest.MonkeyPatch) -> None:
    # #2101 / #2234: failed, cancelled and expired-before-start runs are left out by the read;
    # the fake hands none back, so first asks and re-asks are both made.
    _fleets(monkeypatch, [_loop()])
    x, y = _case("P1", 1), _case("P1", 2)
    db = _Db([x, y], asked=[(y.id, LOOP, _OLD)], open_keys=[])
    assert await routing.plan_catch_up_asks(db, now=_NOW) == [(x, LOOP, 0), (y, LOOP, 1)]  # type: ignore[arg-type]


async def test_a_recent_ask_is_left_to_its_delivery(monkeypatch: pytest.MonkeyPatch) -> None:
    _fleets(monkeypatch, [_loop()])
    x = _case("P1", 1)
    db = _Db([x], asked=[(x.id, LOOP, _NOW - timedelta(seconds=60))], open_keys=[])
    assert await routing.plan_catch_up_asks(db, now=_NOW) == []  # type: ignore[arg-type]


async def test_an_open_item_answers_an_ask_made_again(monkeypatch: pytest.MonkeyPatch) -> None:
    _fleets(monkeypatch, [_loop()])
    x = _case("P1", 1)
    db = _Db([x], asked=[(x.id, LOOP, _OLD)], open_keys=[f"ctem:f1:case:{x.id}"])
    assert await routing.plan_catch_up_asks(db, now=_NOW) == []  # type: ignore[arg-type]


async def test_asks_are_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    _fleets(monkeypatch, [_loop()])
    x = _case("P1", 1)
    asks = [(x.id, LOOP, _OLD - timedelta(hours=i)) for i in range(routing.MAX_ASKS)]
    assert await routing.plan_catch_up_asks(_Db([x], asked=asks, open_keys=[]), now=_NOW) == []  # type: ignore[arg-type]
    two = asks[: routing.MAX_ASKS - 1]
    planned = await routing.plan_catch_up_asks(_Db([x], asked=two, open_keys=[]), now=_NOW)  # type: ignore[arg-type]
    assert planned == [(x, LOOP, routing.MAX_ASKS - 1)]


async def test_catch_up_publishes_the_ask_again_under_a_new_event_id(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.core.database as database
    from app.services import platform_events

    x = _case("P1", 1)
    added: list[Any] = []

    class _Session:
        async def __aenter__(self) -> _Session:
            return self

        async def __aexit__(self, *exc: Any) -> bool:
            return False

        def add(self, row: Any) -> None:
            added.append(row)

        async def commit(self) -> None:
            return None

    async def plan(_db: Any, **_kw: Any) -> list[tuple[Any, str, int]]:
        return [(x, LOOP, 1)]

    published: list[str | None] = []

    async def publish(_name: str, _data: Any, *, event_id: str | None = None) -> str | None:
        published.append(event_id)
        return event_id

    monkeypatch.setattr(database, "async_session", _Session)
    monkeypatch.setattr(routing, "plan_catch_up_asks", plan)
    monkeypatch.setattr(platform_events, "publish", publish)
    assert await routing.catch_up(now=_NOW) == 1
    assert published == [routing._catch_up_event_id(x.id, LOOP, 1)]
    assert added[0].kind == routing.OWNER_REQUESTED and "asked again" in added[0].reason
