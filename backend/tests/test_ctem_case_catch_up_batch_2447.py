"""The case catch-up sees every case a waiting batch run carries, not only its head (#2447).

A batch run of "Case opened or reopened" names its first case in its context;
the later cases live only in ``trigger_payload.events``, and a split hands the
rest to a remainder run whose context names its own head. The catch-up must
not ask a case again while one of those runs carries it, and a second
``ctem_case.opened`` for a case a waiting batch carries is not appended to it.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest

from app.services import ctem_case_routing as routing
from app.services import event_batching
from tests.test_ctem_case_routing import _NOW, _OLD, LOOP, _case, _Db, _fleets, _loop


def _opened(case: Any, event_id: str) -> dict[str, Any]:
    cid = str(case.id)
    return {
        "event": "ctem_case.opened",
        "event_id": event_id,
        "object_type": "ctem_case",
        "object_id": cid,
        "case_id": cid,
        "trigger_type": "platform_event",
    }


def _batch(*events: dict[str, Any]) -> dict[str, Any]:
    payload = event_batching.batch_payload(events[0], 60)
    for event in events[1:]:
        payload = event_batching.merged_payload(payload, event)
    return payload


@pytest.mark.parametrize("asked", [False, True])
async def test_no_case_of_a_queued_batch_run_is_asked(monkeypatch: pytest.MonkeyPatch, asked: bool) -> None:
    # Acceptance 1: a queued batch run carrying A (head), B and C; neither B nor C is picked,
    # on a first ask or on a re-ask after the re-ask delay.
    _fleets(monkeypatch, [_loop()])
    a, b, c, d = _case("P1", 1), _case("P1", 2), _case("P2", 1), _case("P2", 2)
    payload = _batch(_opened(a, "ea"), _opened(b, "eb"), _opened(c, "ec"))
    asks = [(x.id, LOOP, _OLD) for x in (a, b, c, d)] if asked else []
    db = _Db([a, b, c, d], asked=asks, open_keys=[], runs=[(str(a.id), _NOW - timedelta(hours=1), "queued", payload)])
    planned = await routing.plan_catch_up_asks(db, now=_NOW)  # type: ignore[arg-type]
    assert planned == [(d, LOOP, 1 if asked else 0)]


async def test_no_case_of_a_queued_remainder_run_is_asked(monkeypatch: pytest.MonkeyPatch) -> None:
    # Acceptance 2: after the split, the remainder run carries B (head) and C: C is not picked.
    _fleets(monkeypatch, [_loop()])
    a, b, c = _case("P1", 1), _case("P1", 2), _case("P2", 1)
    first, remainder = event_batching.split_batch(_batch(_opened(a, "ea"), _opened(b, "eb"), _opened(c, "ec")))
    assert remainder is not None and first["object_id"] == str(a.id)
    context = event_batching.remainder_context({"type": "platform_event"}, remainder, "batch-run")
    assert context["object_id"] == str(b.id)
    db = _Db(
        [a, b, c],
        asked=[(x.id, LOOP, _OLD) for x in (a, b, c)],
        open_keys=[f"ctem:f1:case:{a.id}"],
        runs=[(context["object_id"], _NOW - timedelta(minutes=30), "queued", remainder)],
    )
    assert await routing.plan_catch_up_asks(db, now=_NOW) == []  # type: ignore[arg-type]


def test_a_second_opened_event_for_a_carried_case_is_not_appended() -> None:
    # Acceptance 3: a catch-up ask raises ctem_case.opened for B under a new event id.
    a, b = _case("P1", 1), _case("P1", 2)
    waiting = _batch(_opened(a, "ea"), _opened(b, "eb"))
    again = _opened(b, "eb-catch-up-1")
    assert event_batching.carries(waiting, again)
    merged = event_batching.merged_payload(waiting, again)
    assert merged["event_count"] == 2
    assert [e["event_id"] for e in merged["events"]] == ["ea", "eb"]


def test_other_events_about_one_object_are_still_listed_each() -> None:
    # Only the listed events dedupe by object: two run.failed about one run stay two events.
    first = {"event": "run.failed", "event_id": "e1", "object_type": "run", "object_id": "r1"}
    second = {**first, "event_id": "e2"}
    waiting = event_batching.batch_payload(first, 60)
    assert not event_batching.carries(waiting, second)
    assert event_batching.merged_payload(waiting, second)["event_count"] == 2


def test_carried_object_ids_reads_every_event() -> None:
    a, b = _case("P1", 1), _case("P1", 2)
    assert event_batching.carried_object_ids(_batch(_opened(a, "ea"), _opened(b, "eb"))) == {str(a.id), str(b.id)}
    assert event_batching.carried_object_ids(_opened(a, "ea")) == {str(a.id)}
    assert event_batching.carried_object_ids(None) == set()


async def test_a_batch_run_that_ended_before_the_ask_does_not_answer_it(monkeypatch: pytest.MonkeyPatch) -> None:
    # Acceptance 4 (#2101): an ended batch run created before the ask leaves its later cases askable again.
    _fleets(monkeypatch, [_loop()])
    a, b = _case("P1", 1), _case("P1", 2)
    payload = _batch(_opened(a, "ea"), _opened(b, "eb"))
    db = _Db(
        [a, b],
        asked=[(a.id, LOOP, _OLD), (b.id, LOOP, _OLD)],
        open_keys=[],
        runs=[(str(a.id), _OLD - timedelta(hours=1), "completed", payload)],
    )
    assert await routing.plan_catch_up_asks(db, now=_NOW) == [(a, LOOP, 1), (b, LOOP, 1)]  # type: ignore[arg-type]


async def test_the_runs_read_takes_batch_runs_and_still_leaves_failed_and_expired_out() -> None:
    # Acceptance 4 (#2234, #2319): the read still drops failed, cancelled and expired-before-start runs.
    import uuid

    from sqlalchemy.dialects import postgresql

    db = _Db([], asked=[], open_keys=[])
    await routing._answered_case_ids(db, {str(uuid.uuid4()): _OLD})  # type: ignore[arg-type]
    dialect = postgresql.dialect()  # type: ignore[no-untyped-call]
    sql = str(db.statements[-1].compile(dialect=dialect, compile_kwargs={"literal_binds": True}))
    assert "trigger_payload" in sql and "'batch'" in sql
    assert "expired_not_started" in sql and "'failed'" in sql and "'cancelled'" in sql
