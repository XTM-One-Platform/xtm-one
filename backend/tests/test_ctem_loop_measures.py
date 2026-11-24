"""CTEM loop measures (CTEM-30, #937): durations from the case history, closed on proof, per-viewer scope."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from app.api import deps
from app.api.v1 import ctem_cases as api_module
from app.core.database import get_db
from app.models.ctem_case import CtemCase, CtemCaseEvent, CtemCaseIssue
from app.services import ctem_cases_view as view_of
from app.services import ctem_loop_measures as measures

T0 = datetime(2026, 11, 20, 8, 0, tzinfo=UTC)
ORG = uuid.uuid4()


def _case(state: str = "new", validations: list | None = None) -> CtemCase:
    return CtemCase(
        id=uuid.uuid4(),
        organization_id=ORG,
        vertical="cyber",
        title="Close the exposure of portal-edge-01",
        remediation_kind="close_exposure",
        keys=["close_exposure:portal-edge-01"],
        pinned_issues=[],
        state=state,
        state_since=T0,
        validations=validations or [],
        created_at=T0,
    )


def _ev(case: CtemCase, kind: str, hours: float, to_value: str | None = None) -> CtemCaseEvent:
    return CtemCaseEvent(case_id=case.id, actor="system", kind=kind, at=T0 + timedelta(hours=hours), to_value=to_value)


def test_durations_follow_the_case_history() -> None:
    case = _case()
    events = [
        _ev(case, "created", 0),
        _ev(case, "validation", 2, "failed"),
        _ev(case, "state", 3, "in_progress"),
        _ev(case, "validation", 10, "passed"),
    ]
    got = measures.case_durations(case, events, [T0 - timedelta(minutes=30)])
    assert got == {
        "signal_to_case": 0.5,
        "case_to_validation": 2.0,
        "case_to_decision": 3.0,
        "case_to_passing_retest": 10.0,
    }


def test_a_step_not_reached_is_none_never_zero() -> None:
    case = _case()
    got = measures.case_durations(case, [_ev(case, "created", 0)], [])
    assert set(got.values()) == {None}
    summary = measures.summarize([case], {case.id: [_ev(case, "created", 0)]}, {})
    assert summary["case_to_validation"] == {"median_hours": None, "count": 0}
    assert summary["closed_on_proof"] == {"share": None, "proven": 0, "resolved": 0}


def test_closed_on_proof_counts_resolved_cases_whose_newest_validation_passes() -> None:
    proven = _case("resolved", [{"result": "failed"}, {"result": "passed"}])
    on_word = _case("resolved", [])
    still_open = _case("remediating", [{"result": "passed"}])
    summary = measures.summarize([proven, on_word, still_open], {}, {})
    assert summary["closed_on_proof"] == {"share": 0.5, "proven": 1, "resolved": 2}
    assert summary["case_count"] == 3


def test_median_over_cases() -> None:
    cases = [_case() for _ in range(3)]
    events = {
        c.id: [_ev(c, "created", 0), _ev(c, "validation", h, "pending")] for c, h in zip(cases, (1, 4, 9), strict=True)
    }
    assert measures.summarize(cases, events, {})["case_to_validation"] == {"median_hours": 4.0, "count": 3}


@pytest.fixture
def api(monkeypatch):
    """Two shared cases: the caller sees only the one opened by their own signal."""
    seen = SimpleNamespace(user=SimpleNamespace(id=uuid.uuid4(), ctem_vertical=None, is_admin=False))
    mine, other = _case(), _case()
    my_signal, other_signal = uuid.uuid4(), uuid.uuid4()
    cases = [
        (mine, [CtemCaseIssue(case_id=mine.id, source="signal", external_id=str(my_signal), open=True)]),
        (other, [CtemCaseIssue(case_id=other.id, source="signal", external_id=str(other_signal), open=True)]),
    ]
    seen.mine, seen.asked = mine, {}

    async def _primary(_db):
        return SimpleNamespace(id=ORG)

    async def _list(_db, *, organization_id, vertical):
        return cases

    async def _view(_db, user_id, vertical, **_kw):
        return view_of.ViewerView(signals={f"signal:{my_signal}"})

    async def _events(_db, ids):
        seen.asked["events"] = list(ids)
        return {i: [CtemCaseEvent(case_id=i, actor="system", kind="created", at=T0)] for i in ids}

    async def _observed(_db, ids, *, organization_id, recorded_by):
        seen.asked["observed"] = (list(ids), recorded_by)
        return {i: [T0 - timedelta(hours=1)] for i in ids}

    monkeypatch.setattr(api_module, "_require_xtm_one", lambda: None)
    monkeypatch.setattr(api_module, "get_primary", _primary)
    monkeypatch.setattr(api_module.store, "list_cases", _list)
    monkeypatch.setattr(api_module.store, "events_of", _events)
    monkeypatch.setattr(api_module.view_of, "viewer_view", _view)
    monkeypatch.setattr(api_module.signal_store, "observed_by_case", _observed)

    app = FastAPI()
    app.include_router(api_module.router, prefix="/ctem-cases")

    async def _db():
        yield SimpleNamespace()

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[deps.get_current_user] = lambda: seen.user
    seen.app = app
    return seen


async def test_measures_cover_only_the_cases_the_caller_sees(api) -> None:
    transport = httpx.ASGITransport(app=api.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        res = await client.get("/ctem-cases/measures")
    assert res.status_code == 200
    body = res.json()["measures"]
    assert body["case_count"] == 1
    assert body["signal_to_case"] == {"median_hours": 1.0, "count": 1}
    assert api.asked["events"] == [api.mine.id]
    # A restricted user reads the observed time of their own signals only.
    assert api.asked["observed"] == ([api.mine.id], str(api.user.id))
