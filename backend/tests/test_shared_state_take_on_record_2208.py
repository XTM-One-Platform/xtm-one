"""A take never turns an entry kept for another purpose into a lease (#2208).

``dev:green-claim:<repo>@<head>`` keys were written by one run with a value of
its own; another run's ``hold="take"`` merged its claim into that value and the
later cleanup purged the first run's notes. A take on an active entry that is
not a lease and has no holder is now refused, nothing written, unless the
caller passes ``lease`` explicitly.
"""

from typing import Any

import pytest

from tests.test_shared_state_hold_7519 import HOLDER, OTHER, _holders, _run, _state, _upsert


def _record() -> Any:
    state = _state()
    state.key = "dev:green-claim:o/r@abc"
    state.is_lease = False
    state.value = {"merged": "abc", "by": "run r"}
    return state


@pytest.mark.asyncio
async def test_a_take_on_an_active_record_is_refused_and_writes_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    state = _record()
    _holders(monkeypatch)
    with pytest.raises(ValueError, match="not a lease"):
        await _upsert(monkeypatch, state, value={"holder": "me"}, note="claim", hold="take", run_id=HOLDER)
    assert state.held_by_run_id is None
    assert state.value == {"merged": "abc", "by": "run r"}
    assert state.notes == []


@pytest.mark.asyncio
async def test_an_explicit_lease_takes_the_record_on_purpose(monkeypatch: pytest.MonkeyPatch) -> None:
    state = _record()
    _holders(monkeypatch)
    await _upsert(monkeypatch, state, value={"holder": "me"}, hold="take", run_id=HOLDER, lease=True)
    assert state.held_by_run_id == HOLDER
    assert state.is_lease is True


@pytest.mark.asyncio
async def test_a_take_on_a_new_key_creates_a_lease(monkeypatch: pytest.MonkeyPatch) -> None:
    state = _state()
    state.id = None
    _holders(monkeypatch)
    await _upsert(monkeypatch, state, value={"holder": "me"}, hold="take", run_id=HOLDER)
    assert state.is_lease is True
    assert state.held_by_run_id == HOLDER


@pytest.mark.asyncio
async def test_a_take_on_a_released_lease_is_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    state = _state()
    state.is_lease = True
    state.status = "resolved"
    _holders(monkeypatch)
    await _upsert(monkeypatch, state, value={"holder": "me"}, hold="take", run_id=OTHER)
    assert state.held_by_run_id == OTHER


@pytest.mark.asyncio
async def test_a_take_on_a_resolved_record_is_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    state = _record()
    state.status = "resolved"
    _holders(monkeypatch)
    await _upsert(monkeypatch, state, value={"holder": "me"}, hold="take", run_id=OTHER)
    assert state.held_by_run_id == OTHER


@pytest.mark.asyncio
async def test_a_take_over_of_an_ended_holder_is_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    state = _record()
    state.held_by_run_id = HOLDER
    _holders(monkeypatch, **{str(HOLDER): _run("failed")})
    await _upsert(monkeypatch, state, value={}, hold="take", run_id=OTHER)
    assert state.held_by_run_id == OTHER


@pytest.mark.asyncio
async def test_a_write_without_hold_on_a_record_is_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    state = _record()
    _holders(monkeypatch)
    await _upsert(monkeypatch, state, value={"more": 1}, run_id=OTHER)
    assert state.value["more"] == 1
    assert state.held_by_run_id is None
