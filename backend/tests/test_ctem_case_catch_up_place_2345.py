"""#2345: a case the catch-up has not asked yet names its place in the backlog.

The catch-up asks at most ``CATCH_UP_BATCH`` cases per pass, most urgent first.
``catch_up_places`` answers, for each waiting case, its position, the backlog
size, the batch size, the full passes ahead and the sweep cadence;
``get_ctem_case`` and ``list_ctem_cases`` carry it as ``catch_up``.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from app.services import ctem_case_routing as routing
from tests.test_ctem_case_routing import LOOP, _case, _Db, _fleets, _loop
from tests.test_ctem_case_tools import env  # noqa: F401 (fixture)


async def test_a_case_past_the_batch_names_its_place(monkeypatch: pytest.MonkeyPatch) -> None:
    _fleets(monkeypatch, [_loop()])
    monkeypatch.setattr(routing, "CATCH_UP_BATCH", 2)
    cases = [_case("P2", i) for i in range(5)]
    db = _Db(cases, asked=[], open_keys=[])
    places = await routing.catch_up_places(db, [c.id for c in cases])  # type: ignore[arg-type]
    last = places[str(cases[4].id)]
    assert last["position"] == 5
    assert last["backlog"] == 5
    assert last["batch_size"] == 2
    assert last["passes_ahead"] == 2
    assert last["asked_before"] == 0
    assert last["sweep_seconds"] >= 60
    assert places[str(cases[0].id)]["passes_ahead"] == 0


async def test_the_backlog_is_read_past_the_batch(monkeypatch: pytest.MonkeyPatch) -> None:
    _fleets(monkeypatch, [_loop()])
    cases = [_case("P3", i) for i in range(routing.CATCH_UP_BATCH + 5)]
    db = _Db(cases, asked=[], open_keys=[])
    places = await routing.catch_up_places(db, [cases[-1].id])  # type: ignore[arg-type]
    assert places[str(cases[-1].id)]["position"] == routing.CATCH_UP_BATCH + 5
    assert places[str(cases[-1].id)]["passes_ahead"] == 1


async def test_a_case_not_waiting_has_no_place(monkeypatch: pytest.MonkeyPatch) -> None:
    _fleets(monkeypatch, [_loop()])
    run, asked, free = _case("P1", 1), _case("P1", 2), _case("P1", 3)
    db = _Db([run, asked, free], asked=[(asked.id, LOOP)], open_keys=[f"ctem:f1:case:{run.id}"])
    places = await routing.catch_up_places(db, [run.id, asked.id, free.id])  # type: ignore[arg-type]
    assert set(places) == {str(free.id)}
    assert places[str(free.id)]["position"] == 1


async def test_no_active_owner_and_no_ids_answer_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    _fleets(monkeypatch, [_loop(active=False)])
    a = _case("P1", 1)
    assert await routing.catch_up_places(_Db([a], asked=[], open_keys=[]), [a.id]) == {}  # type: ignore[arg-type]
    assert await routing.catch_up_places(_Db([a], asked=[], open_keys=[]), []) == {}  # type: ignore[arg-type]


async def test_an_unreadable_backlog_answers_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    async def broken(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError("down")

    monkeypatch.setattr(routing, "plan_catch_up_asks", broken)
    assert await routing.catch_up_places(object(), ["x"]) == {}  # type: ignore[arg-type]


_PLACE = {
    "position": 140,
    "backlog": 150,
    "batch_size": 100,
    "passes_ahead": 1,
    "sweep_seconds": 3600,
    "asked_before": 0,
}


def _places(monkeypatch: pytest.MonkeyPatch, answer: dict[str, dict[str, Any]]) -> None:
    async def read(_db: Any, case_ids: Any, **_k: Any) -> dict[str, dict[str, Any]]:
        wanted = {str(c) for c in case_ids}
        return {k: v for k, v in answer.items() if k in wanted}

    monkeypatch.setattr(routing, "catch_up_places", read)


async def test_get_ctem_case_names_the_place_of_a_waiting_case(env: Any, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    _places(monkeypatch, {str(env.case.id): _PLACE})
    body = json.loads(await env.call("get_ctem_case", case_id=str(env.case.id)))
    assert body["catch_up"] == _PLACE


async def test_list_ctem_cases_names_the_place_of_a_waiting_case(env: Any, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    _places(monkeypatch, {str(env.case.id): _PLACE})
    body = json.loads(await env.call("list_ctem_cases"))
    assert body["cases"][0]["catch_up"] == _PLACE


async def test_a_case_not_waiting_carries_no_place(env: Any, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    _places(monkeypatch, {})
    assert "catch_up" not in json.loads(await env.call("get_ctem_case", case_id=str(env.case.id)))
    assert "catch_up" not in json.loads(await env.call("list_ctem_cases"))["cases"][0]
