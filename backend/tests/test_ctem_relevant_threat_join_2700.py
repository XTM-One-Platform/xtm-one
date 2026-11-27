"""A relevant_threat signal joins the open case that already holds its exposure (#2700).

A manual signal on a CVE keyed ``manual:vulnerability/<subject>`` and only joined a case of the
same key: an open upgrade case of the product, or the patch case of the CVE, never matched, so one
fix was tracked in two or three cases. ``open_from_signal`` now looks for the open patch case of the
CVE, then an open upgrade case of the signal's product running on the signal's assets, and joins it;
a manual exposure keys on (CVE, products, assets) so two subject spellings land on one case.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

import pytest

from app.models.ctem_case import CtemCase, CtemCaseEvent
from app.services import ctem_case_loop as loop
from app.services import ctem_case_opening as case_opening
from app.services import ctem_case_versions as case_versions

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.models.ctem_signal import CtemSignal

NOW = datetime(2026, 11, 27, 21, 0, tzinfo=UTC)
ORG = uuid.uuid4()
CVE = "CVE-2026-61001"


def _signal(subject: str = CVE, **kw: Any) -> CtemSignal:
    base: dict[str, Any] = dict(
        id=uuid.uuid4(),
        organization_id=ORG,
        trigger_path="relevant_threat",
        lens="world",
        subject_kind="vulnerability",
        subject_ref=subject,
        subject_label=subject,
        evidence={"assets": ["LAPTOP-042"], "products": ["Chrome"], "cve": CVE},
        state="confirmed",
        case_id=None,
        updated_at=None,
        decision={"outcome": "case", "state": "confirmed", "severity": "P1", "sla_hours": 24, "scope": {}},
    )
    base.update(kw)
    return cast("CtemSignal", SimpleNamespace(**base))


def _case(keys: list[str], kind: str, **kw: Any) -> CtemCase:
    base: dict[str, Any] = dict(
        id=uuid.uuid4(),
        organization_id=ORG,
        vertical="cyber",
        title="Upgrade Google Chrome to a fixed version",
        remediation_kind=kind,
        keys=keys,
        pinned_issues=[],
        state="waiting_decision",
        state_since=NOW - timedelta(days=1),
        trigger_path="new_vulnerability",
        risk_scenario_ids=[],
        severity="P2",
        sla_due_at=NOW + timedelta(hours=48),
        sla_breached_at=None,
        tickets=[],
        validations=[],
        validation_plan=False,
        resolution_kind=None,
        product_label="Google Chrome",
        created_at=NOW - timedelta(days=1),
    )
    base.update(kw)
    return CtemCase(**base)


class _Session:
    """Answers the open cases for every lookup and records what is added."""

    def __init__(self, cases: list[CtemCase]) -> None:
        self.cases = cases
        self.added: list[object] = []

    def add(self, row: object) -> None:
        self.added.append(row)

    async def flush(self) -> None:
        return None

    async def execute(self, _statement: object, *_a: object, **_k: object) -> SimpleNamespace:
        cases = self.cases
        return SimpleNamespace(scalars=lambda: iter(cases))


def _db(session: _Session) -> AsyncSession:
    return cast("AsyncSession", session)


@pytest.fixture(autouse=True)
def _opening_on(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    tokens = {"taken": 0}

    async def is_on(_db: object, _vertical: str) -> bool:
        return True

    async def take(_vertical: str) -> bool:
        tokens["taken"] += 1
        return True

    async def no_lock(*_a: object, **_k: object) -> None:
        return None

    async def running(_db: object, case: Any) -> list[str]:
        return ["laptop-042", "LAPTOP-077"] if case.remediation_kind == "upgrade" else []

    async def route(*_a: object, **_k: object) -> SimpleNamespace:
        return SimpleNamespace(owner=None, team_id=None)

    from app.services import ctem_case_owner

    monkeypatch.setattr(case_opening, "is_on", is_on)
    monkeypatch.setattr(case_opening, "take_opening_token", take)
    monkeypatch.setattr(loop, "hold_key_lock", no_lock)
    monkeypatch.setattr(case_versions, "running_asset_names", running)
    monkeypatch.setattr(ctem_case_owner, "route_for_signal", route)
    return tokens


def _signal_events(session: _Session, case: CtemCase) -> list[CtemCaseEvent]:
    return [r for r in session.added if isinstance(r, CtemCaseEvent) and r.case_id == case.id and r.kind == "signal"]


@pytest.mark.asyncio
async def test_a_manual_signal_joins_the_open_upgrade_case_of_its_product(_opening_on: dict[str, int]) -> None:
    upgrade = _case(["upgrade:google/chrome@latest"], "upgrade")
    session = _Session([upgrade])
    signal = _signal()
    case = await loop.open_from_signal(_db(session), signal, events=loop.PendingEvents(), now=NOW)
    assert case is upgrade
    assert signal.case_id == upgrade.id
    assert not any(isinstance(r, CtemCase) for r in session.added)
    assert len(_signal_events(session, upgrade)) == 1
    # The existing join rule: the most urgent severity and the earliest SLA win.
    assert upgrade.severity == "P1"
    assert upgrade.sla_due_at == NOW + timedelta(hours=24)
    assert loop.fix_of(signal).key in upgrade.keys
    assert _opening_on["taken"] == 0


@pytest.mark.asyncio
async def test_a_manual_signal_joins_the_open_patch_case_of_its_cve() -> None:
    patch = _case([f"patch:vulnerability/{CVE.lower()}"], "patch", product_label=None)
    session = _Session([patch])
    signal = _signal(evidence={"assets": ["LAPTOP-042"], "cve": CVE})
    assert await loop.open_from_signal(_db(session), signal, now=NOW) is patch
    assert signal.case_id == patch.id


@pytest.mark.asyncio
async def test_an_asset_the_upgrade_case_does_not_run_on_opens_its_own_case() -> None:
    upgrade = _case(["upgrade:-/chrome@latest"], "upgrade")
    session = _Session([upgrade])
    signal = _signal(evidence={"assets": ["SERVER-9"], "products": ["Chrome"], "cve": CVE})
    case = await loop.open_from_signal(_db(session), signal, now=NOW)
    assert case is not None and case is not upgrade
    assert case.remediation_kind == "manual"
    assert upgrade.keys == ["upgrade:-/chrome@latest"]


@pytest.mark.asyncio
async def test_a_signal_on_another_product_opens_its_own_case() -> None:
    upgrade = _case(["upgrade:mozilla/firefox@latest"], "upgrade", product_label="Mozilla Firefox")
    session = _Session([upgrade])
    case = await loop.open_from_signal(_db(session), _signal(), now=NOW)
    assert case is not None and case is not upgrade and case.remediation_kind == "manual"


@pytest.mark.asyncio
async def test_a_closed_upgrade_case_is_never_joined() -> None:
    # The lookup reads open cases only: an empty answer stands for a store whose upgrade case is closed.
    session = _Session([])
    case = await loop.open_from_signal(_db(session), _signal(), now=NOW)
    assert case is not None and case.remediation_kind == "manual"


def test_two_subject_spellings_of_one_exposure_key_one_case() -> None:
    by_cve = loop.fix_of(_signal(CVE))
    by_name = loop.fix_of(
        _signal(
            f"{CVE} in Google Chrome on laptop-042",
            evidence={"assets": ["laptop-042 "], "products": ["CHROME"], "cve": CVE.lower()},
        )
    )
    assert by_cve.key == by_name.key == f"manual:vulnerability/{CVE.lower()}:chrome:laptop-042"
    assert by_cve.kind == "manual"
    assert by_cve.title == f"Respond to {CVE}"
    # The former subject key stays an alias, so an open case keyed on it is still joined.
    assert by_cve.aliases == (f"manual:vulnerability/{CVE.lower()}",)


def test_a_manual_signal_without_a_cve_keeps_its_subject_key() -> None:
    fix = loop.fix_of(_signal("Some campaign", subject_label="Some campaign", evidence={"assets": ["A"]}))
    assert fix.key == "manual:vulnerability/some campaign"
    assert fix.aliases == ()


@pytest.mark.asyncio
async def test_an_open_case_on_the_former_manual_key_is_still_joined() -> None:
    former = _case([f"manual:vulnerability/{CVE.lower()}"], "manual", product_label=None)
    session = _Session([former])
    signal = _signal()
    assert await loop.open_from_signal(_db(session), signal, now=NOW) is former
