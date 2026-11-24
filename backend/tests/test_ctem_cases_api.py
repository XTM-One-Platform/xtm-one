"""CTEM Cases API round-trip (#11545): list, read, change, a refused transition, and per-viewer visibility (#11613)."""

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
from app.services import ctem_cases as rules
from app.services import ctem_cases_store as store
from app.services import ctem_cases_view as view_of
from app.services import ctem_ticketing as ticketing

ORG = uuid.uuid4()
NOW = datetime.now(UTC)


class _Db:
    def __init__(self):
        self.commits = 0
        self.added: list = []

    async def commit(self):
        self.commits += 1

    def add(self, row):
        self.added.append(row)


def _case(**kw) -> CtemCase:
    base = {
        "id": uuid.uuid4(),
        "organization_id": ORG,
        "vertical": "cyber",
        "title": "Upgrade Apache Tomcat to 9.0.98",
        "remediation_kind": "upgrade",
        "product_label": "Apache Tomcat",
        "fixed_version": "9.0.98",
        "keys": ["upgrade:apache/tomcat@9.0.98"],
        "pinned_issues": [],
        "state": "new",
        "state_since": NOW,
    }
    base.update(kw)
    return CtemCase(**base)


def _view(*issues: rules.SourceIssue) -> view_of.ViewerView:
    return view_of.ViewerView(issues={i.member_id: i for i in issues}, open_ids={i.member_id for i in issues})


VISIBLE = rules.SourceIssue(
    source="opencti", external_id="vuln--1", name="CVE-2026-1", cve="CVE-2026-1", cvss=9.8, kev=True, assets=("web-1",)
)
HIDDEN = rules.SourceIssue(
    source="opencti", external_id="vuln--2", name="CVE-2026-2", cve="CVE-2026-2", cvss=5.0, assets=("db-secret",)
)


@pytest.fixture
def api(monkeypatch):
    """One shared case with two issues: the caller sees vuln--1 only; ``admin`` sees both; ``other`` neither."""
    seen = SimpleNamespace(db=_Db(), user=SimpleNamespace(id=uuid.uuid4(), ctem_vertical=None))
    seen.admin = uuid.uuid4()
    seen.case = _case()
    rows = [
        CtemCaseIssue(case_id=seen.case.id, source="opencti", external_id="vuln--1", open=True),
        CtemCaseIssue(case_id=seen.case.id, source="opencti", external_id="vuln--2", open=True),
    ]
    seen.views = {str(seen.user.id): _view(VISIBLE), str(seen.admin): _view(VISIBLE, HIDDEN)}

    async def _primary(_db):
        return SimpleNamespace(id=ORG)

    async def _list(_db, *, organization_id, vertical):
        assert organization_id == ORG
        return [(seen.case, rows)]

    async def _get(_db, case_id, *, organization_id):
        if case_id != seen.case.id or organization_id != ORG:
            return None
        events = [
            CtemCaseEvent(case_id=seen.case.id, at=NOW, actor="system", kind="created", to_value="k"),
            CtemCaseEvent(case_id=seen.case.id, at=NOW, actor="system", kind="issue_added", issue="opencti:vuln--2"),
        ]
        events += [r for r in seen.db.added if isinstance(r, CtemCaseEvent)]
        return seen.case, rows, events

    async def _viewer(_db, user_id, vertical, *, fresh=False):
        return seen.views.get(str(user_id), view_of.ViewerView())

    async def _events_of(_db, case_ids):
        return {case_id: [] for case_id in case_ids}

    monkeypatch.setattr(api_module, "get_primary", _primary)
    monkeypatch.setattr(store, "list_cases", _list)
    monkeypatch.setattr(store, "get_case", _get)
    monkeypatch.setattr(store, "events_of", _events_of)
    monkeypatch.setattr(view_of, "viewer_view", _viewer)

    async def _ticketing_target(_db):
        # The profile read (#1032) is covered in test_ctem_ticketing; the fake session has no execute().
        return {"connected": False, "reason": ticketing.NOT_CONNECTED, "settings": ticketing.SETTINGS_PATH}

    monkeypatch.setattr(ticketing, "ticketing_target", _ticketing_target)

    async def _owner_view(_db, _case):
        # The routing read (#1390) is covered in test_ctem_case_routing; the fake session has no execute().
        return {"slug": "ctem-loop", "name": "CTEM loop", "active": True}

    from app.services import ctem_case_routing

    monkeypatch.setattr(ctem_case_routing, "owner_view", _owner_view)

    async def _gate_records(_db, _case_id, _history, *, viewer_id):
        # The gate records read (#1518) is covered in test_ctem_case_gate_records; it opens its own
        # PostgreSQL session through work_item.list_work_items, which the unit shard does not have.
        return []

    from app.services import ctem_case_gate_records_view

    monkeypatch.setattr(ctem_case_gate_records_view, "gate_records", _gate_records)
    # XTM One mode: the platform mode of the test settings already is xtm_one.
    monkeypatch.setattr(api_module, "_require_xtm_one", lambda: None)

    app = FastAPI()
    app.include_router(api_module.router, prefix="/api/v1/ctem-cases")
    app.dependency_overrides[deps.get_current_user] = lambda: seen.user
    app.dependency_overrides[get_db] = lambda: seen.db
    seen.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
    return seen


async def test_the_list_answers_the_cases_and_every_state_count(api):
    async with api.client as client:
        response = await client.get("/api/v1/ctem-cases", params={"state": "new"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["counts"] == {state: int(state == "new") for state in CASE_STATES}
    assert [c["title"] for c in body["cases"]] == ["Upgrade Apache Tomcat to 9.0.98"]
    # No history names a signal or a split (#1723): the Cases list's "Opened by" filter reads legacy.
    assert body["cases"][0]["opened_by"] == "legacy"


async def test_a_case_carries_its_loop_fields(api):
    """trigger path, severity, SLA, scenarios, tickets, validations and resolution are read back (#857)."""
    api.case.trigger_path = "new_exposure"
    api.case.severity = "P2"
    api.case.sla_due_at = NOW
    api.case.risk_scenario_ids = ["rs-1"]
    api.case.validations = [{"provider": "openaev", "kind": "validation", "ref": "sim-1", "result": "passed"}]
    api.case.validation_plan = True
    api.case.resolution_kind = "not_exposed"
    async with api.client as client:
        response = await client.get("/api/v1/ctem-cases", params={"state": "new"})
    assert response.status_code == 200, response.text
    row = response.json()["cases"][0]
    assert row["trigger_path"] == "new_exposure"
    assert row["severity"] == "P2"
    assert row["sla_due_at"] == NOW.isoformat()
    assert row["sla_breached_at"] is None
    assert row["risk_scenario_ids"] == ["rs-1"]
    assert row["tickets"] == []
    assert row["validations"][0]["ref"] == "sim-1"
    assert row["validation_plan"] is True
    assert row["resolution_kind"] == "not_exposed"


async def test_the_list_without_an_organization_is_empty_never_404(api, monkeypatch):
    """A deployment with no organization context yet answers an empty list (the stage pages showed a failed read)."""

    async def _no_primary(_db):
        return None

    monkeypatch.setattr(api_module, "get_primary", _no_primary)
    async with api.client as client:
        response = await client.get("/api/v1/ctem-cases", params={"limit": 200})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["cases"] == []
    assert body["counts"] == dict.fromkeys(CASE_STATES, 0)
    assert body["details_unavailable"] == []
    assert body["details_unavailable_reasons"] == {}


async def test_the_list_says_why_a_product_did_not_answer(api: SimpleNamespace) -> None:
    """#1852: each product of details_unavailable carries the reason of this viewer's read."""
    view = api.views[str(api.user.id)]
    view.missing = ["opencti", "openaev"]
    view.reasons = {"opencti": "too_large"}
    async with api.client as client:
        response = await client.get("/api/v1/ctem-cases")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["details_unavailable"] == ["opencti", "openaev"]
    assert body["details_unavailable_reasons"] == {"opencti": "too_large", "openaev": "unavailable"}


async def test_the_list_filters_by_state(api):
    async with api.client as client:
        response = await client.get("/api/v1/ctem-cases", params={"state": "resolved"})
    assert response.status_code == 200, response.text
    assert response.json()["cases"] == []


async def test_a_viewer_sees_the_shared_case_with_only_their_issues_and_restricted_rows(api):
    """#11613: the hidden issue is a bare Restricted row; scores and assets cover the visible one only."""
    async with api.client as client:
        listed = (await client.get("/api/v1/ctem-cases")).json()["cases"][0]
        detail = (await client.get(f"/api/v1/ctem-cases/{api.case.id}")).json()
    assert listed["issue_count"] == 2 and listed["restricted_count"] == 1
    assert listed["max_cvss"] == 9.8 and listed["asset_count"] == 1 and listed["priority"] == "critical"
    assert detail["issues"][0]["name"] == "CVE-2026-1"
    assert detail["issues"][1] == {"restricted": True}
    text = str(detail)
    assert "vuln--2" not in text and "CVE-2026-2" not in text and "db-secret" not in text
    assert [e["issue"] for e in detail["history"] if e["kind"] == "issue_added"] == [view_of.RESTRICTED]


async def test_a_wider_viewer_sees_every_issue_of_the_same_case(api):
    api.user = SimpleNamespace(id=api.admin, ctem_vertical=None)
    async with api.client as client:
        detail = (await client.get(f"/api/v1/ctem-cases/{api.case.id}")).json()
    assert [i["name"] for i in detail["issues"]] == ["CVE-2026-1", "CVE-2026-2"] and detail["restricted_count"] == 0


async def test_a_case_with_no_visible_issue_is_absent_and_404(api):
    """#11613: absent from the list and the counts (a count is data), 404 on read and change."""
    api.user = SimpleNamespace(id=uuid.uuid4(), ctem_vertical=None)
    async with api.client as client:
        listed = (await client.get("/api/v1/ctem-cases")).json()
        read = await client.get(f"/api/v1/ctem-cases/{api.case.id}")
        write = await client.patch(f"/api/v1/ctem-cases/{api.case.id}", json={"state": "remediating"})
    assert listed["cases"] == [] and set(listed["counts"].values()) == {0}
    assert read.status_code == 404 and write.status_code == 404
    assert api.case.state == "new" and api.db.commits == 0 and api.db.added == []


async def test_a_case_shows_where_its_issues_came_from(api):
    async with api.client as client:
        response = await client.get(f"/api/v1/ctem-cases/{api.case.id}")
    assert response.status_code == 200, response.text
    issue = response.json()["issues"][0]
    assert (issue["source"], issue["external_id"], issue["name"]) == ("opencti", "vuln--1", "CVE-2026-1")
    assert issue["cvss"] == 9.8 and issue["assets"] == ["web-1"]


async def test_an_unknown_case_is_404(api):
    async with api.client as client:
        response = await client.get(f"/api/v1/ctem-cases/{uuid.uuid4()}")
    assert response.status_code == 404


async def test_starting_a_case_sets_owner_and_state_with_history(api):
    owner = api.user.id
    async with api.client as client:
        response = await client.patch(
            f"/api/v1/ctem-cases/{api.case.id}", json={"state": "remediating", "owner_user_id": str(owner)}
        )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["state"] == "remediating" and body["owner_user_id"] == str(owner)
    kinds = [e["kind"] for e in body["history"]]
    assert "owner" in kinds and "state" in kinds
    assert api.db.commits == 1


@pytest.mark.parametrize("right", ["decide", "view"])
async def test_decide_sets_owner_and_due_date_and_view_is_refused(
    api: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, right: str
) -> None:
    """#1748: a CTEM decide right writes the case like an administrator; view is read only (403)."""
    from app.services import ctem_access

    async def _right(_db: object, user_id: object) -> str | None:
        return right if str(user_id) == str(api.user.id) else None

    monkeypatch.setattr(ctem_access, "group_right", _right)
    api.user.is_admin = False
    due = (NOW + timedelta(days=7)).isoformat()
    async with api.client as client:
        read = await client.get(f"/api/v1/ctem-cases/{api.case.id}")
        response = await client.patch(
            f"/api/v1/ctem-cases/{api.case.id}", json={"owner_user_id": str(api.user.id), "due_at": due}
        )
    assert read.status_code == 200
    if right == "view":
        assert response.status_code == 403
        assert api.db.commits == 0 and api.db.added == []
        return
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["owner_user_id"] == str(api.user.id) and body["due_at"]
    owner_events = [e for e in body["history"] if e["kind"] == "owner"]
    assert owner_events and owner_events[0]["actor"] == str(api.user.id)


async def test_view_is_refused_on_bulk_split_and_merge(api: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services import ctem_access

    async def _right(_db: object, _user_id: object) -> str:
        return "view"

    monkeypatch.setattr(ctem_access, "group_right", _right)
    api.user.is_admin = False
    async with api.client as client:
        bulk = await client.post("/api/v1/ctem-cases/bulk", json={"case_ids": [str(api.case.id)], "note": "x"})
        split = await client.post("/api/v1/ctem-cases/split", json={"title": "t", "issue_ids": ["opencti:vuln--1"]})
        merge = await client.post(
            "/api/v1/ctem-cases/merge", json={"target_id": str(api.case.id), "case_ids": [str(api.case.id)]}
        )
    assert bulk.status_code == 200 and bulk.json()["updated"] == []
    assert bulk.json()["failed"][0]["reason"] == ctem_access.VIEW_ONLY_MESSAGE
    assert split.status_code == 403 and merge.status_code == 403
    assert api.db.added == []


async def test_an_owner_who_sees_none_of_the_issues_is_refused(api):
    """#11613: the assignee is checked with their own source read."""
    async with api.client as client:
        response = await client.patch(
            f"/api/v1/ctem-cases/{api.case.id}", json={"state": "remediating", "owner_user_id": str(uuid.uuid4())}
        )
    assert response.status_code == 422 and "see this case's issues" in response.json()["detail"]
    assert api.case.state == "new" and api.db.commits == 0 and api.db.added == []


async def test_an_owner_who_sees_one_issue_is_accepted(api):
    async with api.client as client:
        response = await client.patch(
            f"/api/v1/ctem-cases/{api.case.id}", json={"state": "remediating", "owner_user_id": str(api.admin)}
        )
    assert response.status_code == 200, response.text
    assert response.json()["owner_user_id"] == str(api.admin)


async def test_accepting_risk_without_a_reason_is_refused_and_nothing_is_written(api):
    until = (NOW + timedelta(days=30)).isoformat()
    async with api.client as client:
        response = await client.patch(
            f"/api/v1/ctem-cases/{api.case.id}",
            json={"state": "accepted_risk", "owner_user_id": str(api.user.id), "accepted_until": until},
        )
    assert response.status_code == 422
    assert "reason" in response.json()["detail"]
    assert api.case.state == "new" and api.db.commits == 0 and api.db.added == []


async def test_accepting_risk_records_owner_reason_and_expiry(api):
    until = NOW + timedelta(days=30)
    async with api.client as client:
        response = await client.patch(
            f"/api/v1/ctem-cases/{api.case.id}",
            json={
                "state": "accepted_risk",
                "owner_user_id": str(api.user.id),
                "reason": "WAF rule in place",
                "accepted_until": until.isoformat(),
            },
        )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["state"] == "accepted_risk" and body["accepted_reason"] == "WAF rule in place"
    assert body["accepted_until"] == until.isoformat()


@pytest.mark.parametrize("state", ["accepted_risk"])
async def test_removing_the_owner_of_a_case_that_needs_one_is_refused(api, state):
    owner = uuid.uuid4()
    api.case.state = state
    api.case.owner_user_id = owner
    if state == "accepted_risk":
        api.case.accepted_reason = "WAF rule in place"
        api.case.accepted_until = NOW + timedelta(days=30)
    async with api.client as client:
        response = await client.patch(f"/api/v1/ctem-cases/{api.case.id}", json={"clear_owner": True})
    assert response.status_code == 422
    assert "owner" in response.json()["detail"]
    assert api.case.owner_user_id == owner and api.db.commits == 0 and api.db.added == []


async def test_removing_the_owner_of_a_case_that_new_is_allowed(api):
    api.case.owner_user_id = uuid.uuid4()
    async with api.client as client:
        response = await client.patch(f"/api/v1/ctem-cases/{api.case.id}", json={"clear_owner": True})
    assert response.status_code == 200, response.text
    assert response.json()["owner_user_id"] is None and api.db.commits == 1


async def test_a_bulk_change_applies_each_case_and_names_the_ones_refused(api):
    """S5c: one bulk Start; an unknown case fails alone, the visible one is written in one commit."""
    unknown = uuid.uuid4()
    async with api.client as client:
        response = await client.post(
            "/api/v1/ctem-cases/bulk",
            json={
                "case_ids": [str(api.case.id), str(unknown)],
                "state": "remediating",
                "owner_user_id": str(api.user.id),
            },
        )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["updated"] == [str(api.case.id)]
    assert body["failed"] == [{"id": str(unknown), "reason": "Case not found"}]
    assert api.case.state == "remediating" and api.db.commits == 1


async def test_a_bulk_accept_risk_without_a_reason_writes_nothing(api):
    """S5c: the state machine holds in bulk: accepted risk needs owner, reason and expiry."""
    until = (NOW + timedelta(days=30)).isoformat()
    async with api.client as client:
        response = await client.post(
            "/api/v1/ctem-cases/bulk",
            json={
                "case_ids": [str(api.case.id)],
                "state": "accepted_risk",
                "owner_user_id": str(api.user.id),
                "accepted_until": until,
            },
        )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["updated"] == [] and "reason" in body["failed"][0]["reason"]
    assert api.case.state == "new" and api.db.added == []


async def test_a_merge_needs_every_case_visible_to_the_caller(api, monkeypatch):
    """S5c: a case the caller sees nothing of is 404, as an unknown one; nothing is merged."""
    called: list = []

    async def _merge(*_a, **kw):
        called.append(kw)

    monkeypatch.setattr(store, "merge_cases", _merge)
    async with api.client as client:
        response = await client.post(
            "/api/v1/ctem-cases/merge", json={"target_id": str(api.case.id), "case_ids": [str(uuid.uuid4())]}
        )
    assert response.status_code == 404 and called == [] and api.db.commits == 0


async def test_a_merge_into_a_visible_case_answers_the_survivor(api, monkeypatch):
    other = uuid.uuid4()
    seen: list = []

    async def _merge(_db, **kw):
        seen.append(kw)
        return api.case

    real_get = store.get_case

    async def _get(db, case_id, *, organization_id):
        return await real_get(db, api.case.id if case_id == other else case_id, organization_id=organization_id)

    monkeypatch.setattr(store, "merge_cases", _merge)
    monkeypatch.setattr(store, "get_case", _get)
    async with api.client as client:
        response = await client.post(
            "/api/v1/ctem-cases/merge", json={"target_id": str(api.case.id), "case_ids": [str(other)]}
        )
    assert response.status_code == 200, response.text
    assert response.json()["id"] == str(api.case.id)
    assert seen[0]["target_id"] == api.case.id and seen[0]["source_ids"] == [other]
    assert seen[0]["actor"] == str(api.user.id) and api.db.commits == 1


async def test_a_split_of_an_issue_the_caller_cannot_see_is_refused(api, monkeypatch):
    """S5c: the split takes the caller's own open ids, so a hidden issue is unknown (422)."""
    seen: list = []

    async def _create(_db, **kw):
        seen.append(kw)
        raise rules.CaseRuleError("not an open issue of a case: opencti:vuln--2")

    monkeypatch.setattr(store, "create_manual_case", _create)
    async with api.client as client:
        response = await client.post(
            "/api/v1/ctem-cases/split", json={"title": "DMZ hosts", "issue_ids": ["opencti:vuln--2"]}
        )
    assert response.status_code == 422 and "not an open issue" in response.json()["detail"]
    assert seen[0]["visible"] == {"opencti:vuln--1"} and seen[0]["actor"] == str(api.user.id)
    assert api.db.commits == 0


async def test_a_case_delete_is_refused_to_a_non_administrator(api):
    """#1723: DELETE and bulk-delete need an administrator (require_admin answers 403)."""
    api.user.role = "user"
    api.user.is_admin = False
    async with api.client as client:
        one = await client.delete(f"/api/v1/ctem-cases/{api.case.id}")
        many = await client.post("/api/v1/ctem-cases/bulk-delete", json={"opened_by": "legacy"})
    assert one.status_code == 403 and many.status_code == 403
    assert api.db.commits == 0


async def test_a_bulk_delete_as_administrator_answers_the_dry_run(api, monkeypatch):
    """#1723: the route selects with the filter and answers the service's dry run with the unknown ids."""
    from app.services import ctem_case_delete as case_delete

    api.user.is_admin = True
    calls: dict = {}

    async def _select(_db, **kw):
        calls["select"] = kw
        return [api.case], []

    async def _delete(_db, cases, **kw):
        calls["delete"] = kw
        return {"dry_run": kw["dry_run"], "count": len(cases), "cases": [{"id": str(cases[0].id)}]}

    monkeypatch.setattr(case_delete, "select_cases", _select)
    monkeypatch.setattr(case_delete, "delete_cases", _delete)
    async with api.client as client:
        refused = await client.post("/api/v1/ctem-cases/bulk-delete", json={})
        response = await client.post("/api/v1/ctem-cases/bulk-delete", json={"opened_by": "legacy"})
    assert refused.status_code == 422
    assert response.status_code == 200, response.text
    body = response.json()
    assert body == {"dry_run": True, "count": 1, "cases": [{"id": str(api.case.id)}], "unknown": []}
    assert calls["select"]["opened_by"] == "legacy" and calls["delete"]["actor"] is api.user


async def test_an_expiry_without_an_offset_is_read_as_utc(api):
    until = (NOW + timedelta(days=30)).replace(tzinfo=None, microsecond=0)
    due = (NOW + timedelta(days=7)).replace(tzinfo=None, microsecond=0)
    async with api.client as client:
        response = await client.patch(
            f"/api/v1/ctem-cases/{api.case.id}",
            json={
                "state": "accepted_risk",
                "owner_user_id": str(api.user.id),
                "reason": "WAF rule in place",
                "accepted_until": until.isoformat(),
                "due_at": due.isoformat(),
            },
        )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["accepted_until"] == until.replace(tzinfo=UTC).isoformat()
    assert body["due_at"] == due.replace(tzinfo=UTC).isoformat()
