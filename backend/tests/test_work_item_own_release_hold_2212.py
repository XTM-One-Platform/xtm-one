"""An item its own holder run releases to open is not claimed again at once by that inbox (#2212).

Partner report (ctem.one, 21 Nov 2026): inbox run 49f5b1bf released item
0227dae3 to open because a person drove it from chat; run 61b65ca7 of the same
inbox assignment claimed it again within seconds. A release by the holder run
now records the run and holds the item back for ``OWN_RELEASE_HOLD``; a release
by any other writer still dispatches at once.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import app.services.work_item as wi_service

NOW = datetime(2026, 11, 25, 14, 30, tzinfo=UTC)
RUN = uuid.uuid4()


def _item(**over: object) -> SimpleNamespace:
    base = {"claimed_by_run_id": RUN, "payload": {}, "claim_notes": [], "not_before": None}
    return SimpleNamespace(**{**base, **over})


def test_the_holder_run_is_recognised_among_the_writer_runs() -> None:
    assert wi_service._released_by_its_holder(_item(), [RUN])
    assert wi_service._released_by_its_holder(_item(), [str(RUN)])


def test_another_writer_or_no_holder_is_not_the_holder() -> None:
    assert not wi_service._released_by_its_holder(_item(), [uuid.uuid4()])
    assert not wi_service._released_by_its_holder(_item(), None)
    assert not wi_service._released_by_its_holder(_item(), [])
    assert not wi_service._released_by_its_holder(_item(claimed_by_run_id=None), [RUN])


def test_an_own_release_is_held_back_and_names_the_run() -> None:
    item = _item(payload={"_released_by_runs": ["earlier"]})
    wi_service._hold_own_release(item, NOW)
    assert item.not_before == NOW + wi_service.OWN_RELEASE_HOLD
    assert item.payload["_released_by_runs"] == ["earlier", str(RUN)]
    assert str(RUN) in item.claim_notes[-1]["text"]
    assert "not dispatched again before" in item.claim_notes[-1]["text"]


def test_the_released_runs_list_stays_bounded_and_unique() -> None:
    item = _item(payload={"_released_by_runs": [str(RUN), *[f"r{i}" for i in range(20)]]})
    wi_service._hold_own_release(item, NOW)
    runs = item.payload["_released_by_runs"]
    assert len(runs) <= wi_service._RELEASED_BY_RUNS_KEPT
    assert runs.count(str(RUN)) <= 1
