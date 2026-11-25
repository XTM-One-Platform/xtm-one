"""A finished simulation is delivered once per occurrence (c87c8335).

OpenAEV's ``simulation_finished`` trigger watches ``SIMULATION`` ``UPDATE``
filtered on ``FINISHED``, so every later update of a finished simulation is
notified again. The receiver delivers it once per assignment, simulation and
end date: a repeat starts no run; a relaunched simulation (new end date) does.
The claim is kept only once a run started: a delivery that started none gives it
back, so the next delivery of the same occurrence still runs (#2247 review).
"""

from __future__ import annotations

from typing import Any

import pytest

from app.services import ctem_openaev_events as aev


class _Redis:
    """The ``SET NX`` the occurrence claim uses."""

    def __init__(self) -> None:
        self.keys: set[str] = set()

    async def set(self, key: str, value: str, *, nx: bool = False, ex: int | None = None) -> bool | None:
        if nx and key in self.keys:
            return None
        self.keys.add(key)
        return True

    async def delete(self, key: str) -> int:
        if key in self.keys:
            self.keys.discard(key)
            return 1
        return 0


def _delivery(resource_id: str = "sim-1") -> dict[str, Any]:
    return {
        "_openaev_event": {
            "event_type": "simulation_finished",
            "resource_type": "SIMULATION",
            "resource_id": resource_id,
        }
    }


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    state: dict[str, Any] = {"end": "2026-11-23T15:16:31Z", "reads": [], "redis": _Redis(), "fail": None}

    async def read(integration_id: str, path: str) -> Any:
        state["reads"].append(path)
        if state["fail"] == "read":
            raise RuntimeError("down")
        return {"exercise_id": path.rsplit("/", 1)[-1], "exercise_end_date": state["end"]}

    async def client() -> Any:
        if state["fail"] == "redis":
            raise RuntimeError("redis down")
        return state["redis"]

    monkeypatch.setattr(aev, "_read_object", read)
    monkeypatch.setattr("app.core.redis.redis_client", client)
    return state


async def _claim(
    event: str = "simulation_finished", *, assignment: str = "a1", verified: bool = True, sim: str = "sim-1"
) -> tuple[bool, str | None]:
    return await aev.claim_occurrence(
        event, _delivery(sim), integration_id="i1", assignment_id=assignment, verified=verified
    )


async def _repeat(
    event: str = "simulation_finished", *, assignment: str = "a1", verified: bool = True, sim: str = "sim-1"
) -> bool:
    repeat, _claim_key = await _claim(event, assignment=assignment, verified=verified, sim=sim)
    return repeat


@pytest.mark.asyncio
async def test_a_later_update_of_a_finished_simulation_is_a_repeat(world: dict[str, Any]) -> None:
    assert await _repeat() is False
    assert await _repeat() is True
    assert world["reads"] == ["/api/exercises/sim-1", "/api/exercises/sim-1"]


@pytest.mark.asyncio
async def test_a_relaunched_simulation_is_delivered_again(world: dict[str, Any]) -> None:
    assert await _repeat() is False
    world["end"] = "2026-11-25T10:00:00Z"
    assert await _repeat() is False


@pytest.mark.asyncio
async def test_each_assignment_and_simulation_hears_its_own_occurrence(world: dict[str, Any]) -> None:
    assert await _repeat(assignment="a1") is False
    assert await _repeat(assignment="a2") is False
    assert await _repeat(sim="sim-2") is False


@pytest.mark.asyncio
async def test_other_events_and_forged_deliveries_are_never_held(world: dict[str, Any]) -> None:
    assert await _repeat("finding_updated") is False
    assert await _repeat("finding_updated") is False
    assert await _repeat(verified=False) is False
    assert await _repeat(verified=False) is False
    assert world["reads"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("fail", ["read", "redis"])
async def test_it_fails_open(world: dict[str, Any], fail: str) -> None:
    world["fail"] = fail
    assert await _repeat() is False
    assert await _repeat() is False


@pytest.mark.asyncio
async def test_a_simulation_without_an_end_date_is_delivered(world: dict[str, Any]) -> None:
    world["end"] = None
    assert await _repeat() is False
    assert await _repeat() is False


@pytest.mark.asyncio
async def test_a_delivery_that_started_no_run_gives_the_occurrence_back(world: dict[str, Any]) -> None:
    repeat, claim = await _claim()
    assert repeat is False and claim is not None
    # Dropped, filtered, busy or failed: the receiver releases the claim.
    await aev.release_occurrence(claim)
    repeat, claim_again = await _claim()
    assert repeat is False and claim_again == claim
    # Run started: the claim is kept and the next update is a repeat.
    assert await _claim() == (True, None)


@pytest.mark.asyncio
@pytest.mark.parametrize("fail", ["read", "redis"])
async def test_an_unclaimed_delivery_has_nothing_to_release(world: dict[str, Any], fail: str) -> None:
    world["fail"] = fail
    assert await _claim() == (False, None)
    await aev.release_occurrence(None)


@pytest.mark.asyncio
async def test_release_never_raises(world: dict[str, Any]) -> None:
    _repeat_flag, claim = await _claim()
    world["fail"] = "redis"
    await aev.release_occurrence(claim)
