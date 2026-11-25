"""The cases in motion for the command center and the stage pages (#1621)."""

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
from app.models.ctem_case import CASE_STATES, CtemCase, CtemCaseEvent, CtemCaseIssue
from app.services import ctem_case_audit as audit
from app.services import ctem_cases as rules
from app.services import ctem_cases_overview as overview
from app.services import ctem_cases_store as store
from app.services import ctem_cases_view as view_of

ORG = uuid.uuid4()
NOW = datetime(2026, 11, 24, 12, 0, tzinfo=UTC)


def _case(state: str, *, title: str = "Upgrade Apache Tomcat", severity: str | None = None, since=NOW) -> CtemCase:
    return CtemCase(
        id=uuid.uuid4(),
        organization_id=ORG,
        vertical="cyber",
        title=title,
        remediation_kind="upgrade",
        keys=[],
        pinned_issues=[],
        state=state,
        state_since=since,
        severity=severity,
        created_at=since,
        updated_at=since,
    )


def _issue(ext: str, *, jewels: tuple[str, ...] = ()) -> rules.SourceIssue:
    return rules.SourceIssue(source="opencti", external_id=ext, name=ext, assets=("web-1",), primary_assets=jewels)


def _member(case: CtemCase, ext: str, *, source: str = "opencti", open_: bool = True) -> CtemCaseIssue:
    return CtemCaseIssue(case_id=case.id, source=source, external_id=ext, open=open_)


def _view(*issues: rules.SourceIssue) -> view_of.ViewerView:
    return view_of.ViewerView(issues={i.member_id: i for i in issues}, open_ids={i.member_id for i in issues})


def _phase_event(case: CtemCase, phase_kind: str, at: datetime) -> CtemCaseEvent:
    return CtemCaseEvent(case_id=case.id, at=at, actor="system", kind=phase_kind)


def test_no_case_answers_every_figure_at_zero():
    out = overview.empty()
    assert out["counts"] == {s: 0 for s in CASE_STATES}
    assert out["open"] == 0
    assert set(out["phases"]) == set(audit.PHASES)
    assert all(p["open"] == 0 and p["waiting"] == 0 and p["top"] == [] for p in out["phases"].values())
    assert out["waiting"] == {"count": 0, "oldest": None, "oldest_hours": None, "cases": []}
    assert out["funnel"]["in_case"] == 0


def test_counts_follow_the_list_visibility_and_the_funnel_counts_distinct_vulnerabilities():
    seen_open = _case("remediating")
    seen_waiting = _case("waiting_decision", since=NOW - timedelta(hours=5))
    hidden = _case("new")
    resolved = _case("resolved", since=NOW - timedelta(days=3))
    old_resolved = _case("resolved", since=NOW - timedelta(days=40))
    cases = [
        (seen_open, [_member(seen_open, "v1"), _member(seen_open, "v2")]),
        # v2 is shared by two open cases: counted once in the funnel step.
        (seen_waiting, [_member(seen_waiting, "v2"), _member(seen_waiting, "sig", source="signal")]),
        (hidden, [_member(hidden, "secret")]),
        (resolved, [_member(resolved, "v3", open_=False)]),
        (old_resolved, [_member(old_resolved, "v4", open_=False)]),
    ]
    view = _view(_issue("v1"), _issue("v2", jewels=("Payments",)), _issue("v3"), _issue("v4"))
    out = overview.build(cases, view, {}, now=NOW)
    assert out["counts"]["remediating"] == 1
    assert out["counts"]["waiting_decision"] == 1
    assert out["counts"]["new"] == 0  # the hidden case is in no figure
    assert out["counts"]["resolved"] == 2
    assert out["open"] == 2
    assert out["funnel"] == {"in_case": 2, "resolved_window_days": 30, "resolved_recent": 1}
    assert out["waiting"]["count"] == 1
    assert out["waiting"]["oldest"]["case_id"] == str(seen_waiting.id)
    assert out["waiting"]["oldest_hours"] == 5.0
    # No event yet: the open cases have no phase, in the band and in no stage card.
    assert out["no_phase_open"] == 2
    assert all(p["open"] == 0 for p in out["phases"].values())


def test_a_phase_counts_its_open_and_waiting_cases_and_lists_the_waiting_first(monkeypatch):
    first = _case("remediating", severity="P3", since=NOW - timedelta(days=2))
    second = _case("waiting_decision", severity="P3", since=NOW - timedelta(hours=1))
    third = _case("analyzing", severity="P1", since=NOW - timedelta(days=1))
    cases = [(c, [_member(c, f"v-{i}")]) for i, c in enumerate((first, second, third))]
    view = _view(*(_issue(f"v-{i}", jewels=("Payments",) if i == 2 else ()) for i in range(3)))
    phase_of = {first.id: "remediation", second.id: "remediation", third.id: "prioritization"}

    def _position(events, _view):
        case_id = events[0].case_id
        return {
            "phase": phase_of[case_id],
            "waiting_on": {"gate": "approve_fix", "by": None, "since": None} if case_id == second.id else None,
        }

    monkeypatch.setattr(view_of, "case_position", _position)
    history = {c.id: [_phase_event(c, "state", NOW)] for c, _ in cases}
    out = overview.build(cases, view, history, now=NOW)
    remediation = out["phases"]["remediation"]
    assert remediation["open"] == 2 and remediation["waiting"] == 1
    assert [row["id"] for row in remediation["top"]] == [str(second.id), str(first.id)]
    assert remediation["counts"]["waiting_decision"] == 1 and remediation["counts"]["remediating"] == 1
    assert out["phases"]["prioritization"]["top"][0]["primary_asset"] == "Payments"
    assert out["phases"]["prioritization"]["top"][0]["priority"] == "critical"
    # The gate the oldest waiting case waits on, falling back to its status time.
    assert out["waiting"]["oldest"]["gate"] == "approve_fix"
    assert out["waiting"]["oldest"]["phase"] == "remediation"
    assert out["waiting"]["oldest_hours"] == 1.0


def test_a_phase_lists_five_cases_at_most(monkeypatch):
    cases = [_case("new", since=NOW - timedelta(hours=i)) for i in range(7)]
    pairs = [(c, [_member(c, f"v-{i}")]) for i, c in enumerate(cases)]
    view = _view(*(_issue(f"v-{i}") for i in range(7)))
    monkeypatch.setattr(view_of, "case_position", lambda _e, _v: {"phase": "discovery", "waiting_on": None})
    out = overview.build(pairs, view, {c.id: [] for c in cases}, now=NOW)
    discovery = out["phases"]["discovery"]
    assert discovery["open"] == 7
    assert len(discovery["top"]) == overview.TOP_PER_PHASE
    # Oldest status first among equals.
    assert discovery["top"][0]["id"] == str(cases[6].id)


@pytest.fixture
def client(monkeypatch):
    user = SimpleNamespace(id=uuid.uuid4(), ctem_vertical=None)
    case = _case("new")
    rows = [_member(case, "v1")]
    reads = {"events": 0}

    async def _primary(_db):
        return SimpleNamespace(id=ORG)

    async def _list(_db, *, organization_id, vertical):
        return [(case, rows)]

    async def _viewer(_db, user_id, vertical, *, fresh=False, members=None, stale_ok=False):
        return _view(_issue("v1"))

    async def _events_of(_db, case_ids):
        reads["events"] += 1
        return {cid: [] for cid in case_ids}

    monkeypatch.setattr(api_module, "get_primary", _primary)
    monkeypatch.setattr(store, "list_cases", _list)
    monkeypatch.setattr(store, "events_of", _events_of)
    monkeypatch.setattr(view_of, "viewer_view", _viewer)
    monkeypatch.setattr(api_module, "_require_xtm_one", lambda: None)
    app = FastAPI()
    app.include_router(api_module.router, prefix="/api/v1/ctem-cases")
    app.dependency_overrides[deps.get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: object()
    return SimpleNamespace(
        http=httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test"), reads=reads
    )


async def test_the_overview_route_reads_the_history_once(client):
    async with client.http as http:
        response = await http.get("/api/v1/ctem-cases/overview")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["vertical"] == "cyber"
    assert body["counts"]["new"] == 1 and body["open"] == 1
    assert client.reads["events"] == 1


async def test_the_overview_without_an_organization_is_empty(client, monkeypatch):
    async def _none(_db):
        return None

    monkeypatch.setattr(api_module, "get_primary", _none)
    async with client.http as http:
        response = await http.get("/api/v1/ctem-cases/overview")
    assert response.status_code == 200
    assert response.json()["open"] == 0
