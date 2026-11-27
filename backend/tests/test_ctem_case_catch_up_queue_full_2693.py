"""A case whose batch run the delivery queue dropped is asked again; the catch-up never overflows the queue (#2693).

A queued ``ctem_case.opened`` batch run past ``ASSIGNMENT_QUEUE_MAX_DEPTH`` is finalised
``skipped`` with the ``QUEUE_DROP_ERROR_PREFIX`` error (``trim_platform_event_queues``, or the
``drop_oldest`` eviction): no worker ever ran its cases, so it must not answer a catch-up ask.
The catch-up's own asks go through the same queue: one pass asks no more cases of an owner
than its case automation's queue has room for.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

import pytest

from app.services import ctem_case_routing as routing
from app.services import event_batching
from app.services.assignment_engine import PLATFORM_EVENT_QUEUE_FULL_REASON, QUEUE_DROP_ERROR_PREFIX
from tests.test_ctem_case_catch_up_batch_2447 import _batch, _opened
from tests.test_ctem_case_routing import _NOW, _OLD, LOOP, _case, _Db, _fleets, _loop, _Result

_TRIMMED = f"{QUEUE_DROP_ERROR_PREFIX} {PLATFORM_EVENT_QUEUE_FULL_REASON} (21/20)."
_EVICTED = f"{QUEUE_DROP_ERROR_PREFIX} the queue was full (20/20) when a newer delivery arrived"


@pytest.mark.parametrize("error", [_TRIMMED, _EVICTED])
async def test_a_case_of_a_batch_run_dropped_by_the_queue_is_asked_again(
    monkeypatch: pytest.MonkeyPatch, error: str
) -> None:
    # Acceptance 1: asked at _OLD, carried by a batch run created after the ask then trimmed: asked again.
    _fleets(monkeypatch, [_loop()])
    a, b = _case("P1", 1), _case("P1", 2)
    payload = _batch(_opened(a, "ea"), _opened(b, "eb"))
    db = _Db(
        [a, b],
        asked=[(a.id, LOOP, _OLD), (b.id, LOOP, _OLD)],
        open_keys=[],
        runs=[(str(a.id), _OLD + timedelta(minutes=1), "skipped", payload, error)],
    )
    assert await routing.plan_catch_up_asks(db, now=_NOW) == [(a, LOOP, 1), (b, LOOP, 1)]  # type: ignore[arg-type]


async def test_a_first_ask_of_a_case_whose_only_run_was_trimmed_is_made(monkeypatch: pytest.MonkeyPatch) -> None:
    # The live event's batch was trimmed before any ask: the first catch-up ask is made.
    _fleets(monkeypatch, [_loop()])
    a = _case("P1", 1)
    db = _Db([a], asked=[], open_keys=[], runs=[(str(a.id), _NOW - timedelta(minutes=5), "skipped", None, _TRIMMED)])
    assert await routing.plan_catch_up_asks(db, now=_NOW) == [(a, LOOP, 0)]  # type: ignore[arg-type]


async def test_the_same_batch_run_still_queued_answers_the_ask(monkeypatch: pytest.MonkeyPatch) -> None:
    # Acceptance 2 (unchanged, #2319 / #2447): still queued, its cases are not asked again.
    _fleets(monkeypatch, [_loop()])
    a, b = _case("P1", 1), _case("P1", 2)
    payload = _batch(_opened(a, "ea"), _opened(b, "eb"))
    db = _Db(
        [a, b],
        asked=[(a.id, LOOP, _OLD), (b.id, LOOP, _OLD)],
        open_keys=[],
        runs=[(str(a.id), _OLD + timedelta(minutes=1), "queued", payload)],
    )
    assert await routing.plan_catch_up_asks(db, now=_NOW) == []  # type: ignore[arg-type]


@pytest.mark.parametrize(("status", "error"), [("completed", None), ("skipped", "Skipped: duplicate of run x")])
async def test_a_batch_run_that_ended_otherwise_after_the_ask_still_answers_it(
    monkeypatch: pytest.MonkeyPatch, status: str, error: str | None
) -> None:
    # Acceptance 3 (unchanged): a run that ran, or was skipped for another reason, still answers.
    _fleets(monkeypatch, [_loop()])
    a, b = _case("P1", 1), _case("P1", 2)
    payload = _batch(_opened(a, "ea"), _opened(b, "eb"))
    db = _Db(
        [a, b],
        asked=[(a.id, LOOP, _OLD), (b.id, LOOP, _OLD)],
        open_keys=[],
        runs=[(str(a.id), _OLD + timedelta(minutes=1), status, payload, error)],
    )
    assert await routing.plan_catch_up_asks(db, now=_NOW) == []  # type: ignore[arg-type]


async def test_the_runs_read_carries_the_error_and_still_leaves_expired_runs_out() -> None:
    # Acceptance 4 (unchanged, #2234): the read still drops expired-before-start runs, and reads the error.
    from sqlalchemy.dialects import postgresql

    db = _Db([], asked=[], open_keys=[])
    await routing._answered_case_ids(db, {str(uuid.uuid4()): _OLD})  # type: ignore[arg-type]
    sql = str(
        db.statements[-1].compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})  # type: ignore[no-untyped-call]
    )
    assert "expired_not_started" in sql and "assignment_runs.error" in sql


# -- The catch-up never overflows its owner's queue ----------------------------------


class _HeadroomDb:
    """Answers queue_headroom's two reads: the case automations, then the queued counts."""

    def __init__(self, automations: list[tuple[Any, ...]], queued: list[tuple[Any, int]]) -> None:
        self.automations, self.queued = automations, queued

    async def execute(self, stmt: Any) -> _Result:
        names = [c.name for c in stmt.selected_columns]
        if "trigger_config" in names:
            return _Result(self.automations)
        return _Result(self.queued)


async def test_queue_headroom_counts_free_slots_times_the_cases_a_batch_carries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.core.config import settings

    monkeypatch.setattr(settings, "assignment_queue_max_depth", 20)
    loop_id, full_id, plain_id, fork_fleet = uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    batched = {event_batching.WINDOW_CONFIG: 60, event_batching.MAX_EVENTS_CONFIG: 5}
    db = _HeadroomDb(
        automations=[
            (uuid.uuid4(), LOOP, True, loop_id, batched),
            (fork_fleet, "my-fork", False, plain_id, {}),
            (fork_fleet, "my-fork", False, full_id, batched),
        ],
        queued=[(loop_id, 17), (full_id, 20), (plain_id, 2)],
    )
    room = await routing.queue_headroom(db)  # type: ignore[arg-type]
    # The loop: 3 free slots of 5 cases; the fork: its full automation bounds it to 0.
    assert room == {LOOP: 15, f"fleet:{fork_fleet}": 0}


async def test_catch_up_under_a_full_queue_asks_only_what_fits(monkeypatch: pytest.MonkeyPatch) -> None:
    # Acceptance: a pass under a nearly full queue asks no more cases than fit; the rest
    # get no owner_requested event (no ask spent) and wait for the next pass.
    import app.core.database as database
    from app.services import platform_events

    cases = [_case("P1", i) for i in range(1, 8)]
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
        return [(c, LOOP, 0) for c in cases]

    async def room(_db: Any) -> dict[str, int]:
        return {LOOP: 3}

    published: list[str | None] = []

    async def publish(_name: str, _data: Any, *, event_id: str | None = None) -> str | None:
        published.append(event_id)
        return event_id

    monkeypatch.setattr(database, "async_session", _Session)
    monkeypatch.setattr(routing, "plan_catch_up_asks", plan)
    monkeypatch.setattr(routing, "queue_headroom", room)
    monkeypatch.setattr(platform_events, "publish", publish)
    assert await routing.catch_up(now=_NOW) == 3
    assert [r.case_id for r in added] == [c.id for c in cases[:3]]
    assert published == [routing._catch_up_event_id(c.id, LOOP, 0) for c in cases[:3]]


async def test_catch_up_with_a_full_queue_asks_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.core.database as database

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
        return [(x, LOOP, 0)]

    async def room(_db: Any) -> dict[str, int]:
        return {LOOP: 0}

    monkeypatch.setattr(database, "async_session", _Session)
    monkeypatch.setattr(routing, "plan_catch_up_asks", plan)
    monkeypatch.setattr(routing, "queue_headroom", room)
    assert await routing.catch_up(now=_NOW) == 0
    assert added == []
