"""#2132: the handover_not_allowed finding names each source with its home fleet.

The finding carried only ``source->target`` ids and advised "expose the target", so an
ask built on it read the sources as loop members and offered an exposure with no word
on its side effect. The summary now names the agents and where each source lives; the
evidence ids (what the finding id and a dismissal are computed on) are unchanged.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from app.services import fleet_health_signals as signals
from app.services import fleets as fleets_mod
from app.services.fleets import BoundaryViolation, HomeFleet

OUTSIDER = uuid.uuid4()
LONER = uuid.uuid4()
HIDDEN = uuid.uuid4()
NAMES = {OUTSIDER: "Product Assistant", LONER: "Lone Agent", HIDDEN: "Case Triage"}


class _Result:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self._rows = rows

    def all(self) -> list[tuple[Any, ...]]:
        return self._rows


class _Session:
    async def execute(self, _stmt: Any) -> _Result:
        return _Result(list(NAMES.items()))


@pytest.fixture
def violations(monkeypatch: pytest.MonkeyPatch) -> list[BoundaryViolation]:
    found = [
        BoundaryViolation(source_agent_id=OUTSIDER, target_agent_id=HIDDEN),
        BoundaryViolation(source_agent_id=LONER, target_agent_id=HIDDEN),
    ]

    async def _violations(_db: Any, _fleet: Any) -> list[BoundaryViolation]:
        return found

    async def _homes(_db: Any, ids: Any) -> dict[uuid.UUID, HomeFleet]:
        home = HomeFleet(
            fleet_id=uuid.uuid4(),
            fleet_name="Assistants",
            member_id=uuid.uuid4(),
            is_orchestrator=False,
            is_exposed=False,
        )
        return {OUTSIDER: home} if OUTSIDER in set(ids) else {}

    monkeypatch.setattr(fleets_mod, "fleet_boundary_violations", _violations)
    monkeypatch.setattr(fleets_mod, "home_fleets_of", _homes)
    return found


@pytest.mark.asyncio
async def test_the_summary_names_each_source_with_its_home_fleet(violations: list[BoundaryViolation]) -> None:
    [finding] = await signals._handover_findings(_Session(), object())  # type: ignore[arg-type]
    assert "Product Assistant (fleet Assistants) -> Case Triage" in finding.summary
    assert "Lone Agent (no fleet) -> Case Triage" in finding.summary
    assert finding.evidence == [f"{OUTSIDER}->{HIDDEN}", f"{LONER}->{HIDDEN}"]


def test_the_advice_states_the_exposure_side_effect() -> None:
    advice = signals.FINDING_ADVICE["handover_not_allowed"]
    assert "outside this fleet" in advice
    assert "every other fleet" in advice
