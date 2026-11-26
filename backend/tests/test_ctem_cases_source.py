"""CTEM cases recompute source (#11545, slice S3): issues read from OpenCTI and OpenAEV.

The readers build ``SourceIssue`` rows from raw product nodes; the recompute
writes nothing when a product cannot answer, and the API throttles and gates it.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from app.api import deps
from app.api.v1 import ctem_cases as api_module
from app.core.database import get_db
from app.services import ctem_cases as rules
from app.services import ctem_cases_source as source


def _write_db() -> SimpleNamespace:
    """A session double for the write phase: counts commits, records each statement executed."""
    executed: list[tuple[str, Any]] = []
    db = SimpleNamespace(commits=0, executed=executed)

    async def commit() -> None:
        db.commits += 1

    async def execute(statement: Any, params: Any = None) -> None:
        db.executed.append((str(statement), params))

    db.commit, db.execute = commit, execute
    return db


@pytest.fixture(autouse=True)
def _no_gate_reads(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """The gate's reads prepared before the write phase (#1953), recorded instead of read."""
    from app.services import ctem_scope_gate_store as gate_store

    prepared: list[Any] = []

    async def prepare(db: Any, organization_id: Any) -> None:
        prepared.append(organization_id)

    monkeypatch.setattr(gate_store, "prepare_system_reads", prepare)
    return prepared


STACK = [{"product": "Tomcat", "vendor": "Apache", "cpe": "", "label": "Apache Tomcat"}]
SOFTWARE = [
    {"id": "sw-1", "name": "tomcat", "vendor": "apache", "cpe": ""},
    {"id": "sw-2", "name": "nginx", "vendor": "f5", "cpe": ""},
]


def _rel(sw: str, vid: str, name: str, **kw) -> dict:
    return {"from": {"id": sw}, "to": {"id": vid, "name": name, **kw}}


def test_opencti_issues_keep_only_the_stack_and_take_the_course_of_action():
    relations = [
        _rel("sw-1", "v-1", "CVE-2026-1", x_opencti_cvss_base_score=9.8, x_opencti_cisa_kev=True),
        _rel("sw-1", "v-2", "CVE-2026-2", x_opencti_epss_score="0.4", created_at="2026-10-01T00:00:00Z"),
        _rel("sw-2", "v-3", "CVE-2026-3"),  # nginx is not in the stack
        _rel("sw-1", "v-1", "CVE-2026-1"),  # duplicate edge
    ]
    remediations = [
        {"from": {"id": "coa-1", "standard_id": "course-of-action--1", "name": "Disable AJP"}, "to": {"id": "v-2"}}
    ]
    issues = source.opencti_issues(SOFTWARE, STACK, relations, remediations)
    assert [i.external_id for i in issues] == ["v-1", "v-2"]
    first, second = issues
    assert (first.product, first.vendor, first.cve, first.kev, first.cvss) == (
        "Tomcat",
        "Apache",
        "CVE-2026-1",
        True,
        9.8,
    )
    assert first.course_of_action_id is None
    assert second.course_of_action_id == "course-of-action--1"
    assert second.epss == 0.4 and second.first_seen_at is not None
    grouping = rules.group_issues(issues)
    assert set(grouping.groups) == {"upgrade:apache/tomcat@latest", "coa:course-of-action--1"}


def test_openaev_findings_join_the_opencti_case_by_cve():
    findings = [
        {
            "finding_id": "f-1",
            "finding_value": "CVE-2026-1",
            "finding_assets": [{"asset_name": "web-1"}, {"asset_id": "a-2"}],
        },
        {"finding_id": "f-1", "finding_value": "CVE-2026-1"},  # duplicate
        {"finding_id": "f-2", "finding_value": "CVE-2099-9"},
        {"finding_value": "no id"},
    ]
    aev = source.openaev_issues(findings)
    assert [(i.external_id, i.assets) for i in aev] == [("f-1", ("web-1", "a-2")), ("f-2", ())]
    octi = source.opencti_issues(SOFTWARE, STACK, [_rel("sw-1", "v-1", "CVE-2026-1")], [])
    grouping = rules.group_issues([*octi, *aev])
    members = {m.member_id for m in grouping.groups["upgrade:apache/tomcat@latest"]}
    assert members == {"opencti:v-1", "openaev:f-1"}
    assert [i.member_id for i in grouping.no_known_fix] == ["openaev:f-2"]


def test_a_vendor_foreign_node_of_the_same_name_never_joins_the_entry_case():
    """#1693: a node whose CPE names another vendor does not carry its CVEs into the entry's case."""
    stack = [{"product": "Chrome", "vendor": "Google", "cpe": "", "label": "Google Chrome"}]
    software = [
        {"id": "sw-g", "name": "chrome", "vendor": "", "cpe": "cpe:2.3:a:google:chrome:142.0:*:*:*:*:*:*:*"},
        {"id": "sw-m", "name": "chrome", "vendor": "", "cpe": "cpe:2.3:a:microsoft:chrome:1.0:*:*:*:*:*:*:*"},
        {"id": "sw-x", "name": "Chrome", "vendor": "Microsoft Corporation", "cpe": ""},
    ]
    relations = [
        _rel("sw-g", "v-g", "CVE-2026-10"),
        _rel("sw-m", "v-e", "CVE-2026-11"),
        _rel("sw-x", "v-x", "CVE-2026-12"),
    ]
    issues = source.opencti_issues(software, stack, relations, [])
    assert [i.external_id for i in issues] == ["v-g"]


def test_a_cve_shared_by_two_stack_products_is_one_issue_and_one_case_per_product():
    """#1693: a Chromium CVE OpenCTI links to Chrome and Edge fans out to both products' cases."""
    stack = [
        {"product": "Chrome", "vendor": "Google", "cpe": "", "label": "Google Chrome"},
        {"product": "Edge", "vendor": "Microsoft", "cpe": "", "label": "Microsoft Edge"},
    ]
    software = [
        {"id": "sw-c", "name": "chrome", "vendor": "google", "cpe": ""},
        {"id": "sw-e", "name": "edge", "vendor": "", "cpe": "cpe:2.3:a:microsoft:edge:*:*:*:*:*:*:*:*"},
    ]
    relations = [_rel("sw-c", "v-1", "CVE-2026-20"), _rel("sw-e", "v-1", "CVE-2026-20")]
    issues = source.opencti_issues(software, stack, relations, [])
    assert sorted(i.product for i in issues) == ["Chrome", "Edge"]
    grouping = rules.group_issues(issues)
    assert set(grouping.groups) == {"upgrade:google/chrome@latest", "upgrade:microsoft/edge@latest"}


def test_openaev_finding_lifecycle_is_read_from_the_finding_and_absent_from_a_search_row():
    findings = [
        {
            "finding_id": "f-1",
            "finding_value": "CVE-2026-1",
            "finding_status": "NOT_REPRODUCED",
            "finding_last_tested": "2026-11-21T10:00:00Z",
        },
        {"finding_id": "f-2", "finding_value": "CVE-2026-2", "finding_status": "OPEN", "finding_retest_inject_id": "inj-9"},
        {"finding_id": "f-3", "finding_value": "CVE-2026-3"},  # a search row: no status (OpenAEV #9233)
    ]
    one, two, three = source.openaev_issues(findings)
    assert one.retest_status == "not_reproduced" and one.last_tested_at.startswith("2026-11-21T10:00")
    assert not one.retest_pending
    assert two.retest_status == "open" and two.retest_pending and two.last_tested_at is None
    assert (three.retest_status, three.last_tested_at, three.retest_pending) == (None, None, False)


@pytest.mark.asyncio
async def test_recompute_writes_nothing_when_a_product_cannot_answer(monkeypatch):
    async def read(db, user_id, vertical, *, now=None, members=None, remap=False):
        return source.SourceRead(issues=[], missing=["openaev"])

    async def apply(*a, **kw):
        raise AssertionError("an outage must never resolve a case")

    async def members(db, *, organization_id, vertical):
        return []

    monkeypatch.setattr(source, "read_open_issues", read)
    monkeypatch.setattr(source.store, "member_ids", members)
    monkeypatch.setattr(source.store, "apply_recompute", apply)
    answer = await source.recompute_for_user(None, organization_id=uuid.uuid4(), user_id="u", vertical="cyber")
    assert answer["applied"] is False and answer["missing"] == ["openaev"]


@pytest.mark.asyncio
@pytest.mark.parametrize("complete", [True, False], ids=["complete", "capped"])
async def test_recompute_merges_and_resolves_only_what_the_reader_confirms_closed(monkeypatch, complete):
    """#11613: the caller's closed members are passed on; a capped read resolves nothing."""
    issue = rules.SourceIssue(source="opencti", external_id="v-1", name="CVE-2026-1", product="Tomcat")
    gone = rules.SourceIssue(source="opencti", external_id="v-9", name="CVE-2026-9")
    asked: list = []

    async def read(db, user_id, vertical, *, now=None, members=None, remap=False):
        assert remap is True  # the recompute matches the members OpenAEV #9233 re-keyed
        asked.append(members)
        return source.SourceRead(issues=[issue], closed=[gone], complete=complete)

    applied: list = []

    async def apply(db: Any, **kw: Any) -> dict[str, int]:
        applied.append(list(kw["closed"]))
        return {"created": 1, "updated": 0, "resolved": 0, "reopened": 0, "no_known_fix": 0}

    async def members(db, *, organization_id, vertical):
        return ["opencti:v-1", "opencti:v-9"]

    forgotten: list = []

    async def forget(user_id, vertical):
        forgotten.append(user_id)

    db = _write_db()
    monkeypatch.setattr(source, "read_open_issues", read)
    monkeypatch.setattr(source.store, "apply_recompute", apply)
    monkeypatch.setattr(source.store, "member_ids", members)
    monkeypatch.setattr("app.services.ctem_cases_view.forget_view", forget)
    answer = await source.recompute_for_user(db, organization_id=uuid.uuid4(), user_id="u-1", vertical="cyber")
    assert answer["applied"] is True and answer["created"] == 1 and answer["issues"] == 1
    assert db.commits == 1 and forgotten == ["u-1"]
    # #1953: the write phase fails fast on a held row instead of waiting until the statement timeout.
    assert [p for s, p in db.executed if "lock_timeout" in s] == [{"timeout": source.WRITE_LOCK_TIMEOUT}]
    assert asked == [["opencti:v-1", "opencti:v-9"]]
    assert applied == [["opencti:v-9"] if complete else []]


#: The ``@Queryable(filterable = true)`` fields of OpenAEV's ``Finding`` entity
#: (openaev-model ``io.openaev.database.model.Finding``): any other key sent to
#: ``/api/findings/search`` is a 400 "This path is not handled by Queryable
#: annotation" (#12134: ``base_created_at``).
AEV_FINDING_FILTERABLE = frozenset(
    {
        "finding_type",
        "finding_value",
        "finding_name",
        "finding_tags",
        "finding_created_at",
        "finding_updated_at",
        "finding_status",
        "finding_assets",
        "finding_teams",
        "finding_users",
        "finding_inject_id",
        "finding_field",
        "finding_simulation",
        "finding_scenario",
        "finding_asset_groups",
    }
)


def _aev_findings_api(sent: list[dict]):
    """An OpenAEV ``/api/findings/search`` double that refuses a key Finding cannot filter on."""

    async def api(base_url, headers, method, path, action, *, json=None, **kw):
        sent.append(json)
        keys = {f["key"] for f in ((json or {}).get("filterGroup") or {}).get("filters") or []}
        refused = keys - AEV_FINDING_FILTERABLE
        if refused:
            return None, f"400 bad request. This path is not handled by Queryable annotation: {sorted(refused)[0]}"
        content = [{"finding_id": "f-1", "finding_value": "CVE-2026-1", "finding_created_at": "2026-11-01T00:00:00Z"}]
        return httpx.Response(200, json={"content": content, "totalPages": 1}), None

    return api


@pytest.mark.asyncio
async def test_openaev_findings_search_filters_only_on_keys_finding_accepts(monkeypatch):
    """#12134: every filter key sent to /api/findings/search is a queryable field of Finding."""
    sent: list[dict] = []
    monkeypatch.setattr(source, "_api", _aev_findings_api(sent))
    async with httpx.AsyncClient() as client:
        issues = await source._read_openaev(client, "https://aev", {}, source.datetime.now(source.UTC))
    assert issues is not None and [i.external_id for i in issues] == ["f-1"]
    keys = [f["key"] for f in sent[0]["filterGroup"]["filters"]]
    assert keys == ["finding_type", "finding_status", "finding_updated_at"]
    assert set(keys) <= AEV_FINDING_FILTERABLE


@pytest.mark.asyncio
async def test_recompute_applies_when_opencti_and_openaev_answer(monkeypatch):
    """#12134: with both products answering, the OpenAEV source is not missing and the recompute writes."""
    octi_issue = rules.SourceIssue(source="opencti", external_id="v-1", name="CVE-2026-1", product="Tomcat")

    async def stack(db):
        return STACK

    async def creds(db, user_id, provider):
        return (f"https://{provider}", {})

    async def read_opencti(client, base_url, headers, stack):
        return source.OpenCtiRead([octi_issue])

    applied: list = []

    async def apply(db: Any, **kw: Any) -> dict[str, int]:
        applied.append(sorted(i.member_id for i in kw["issues"]))
        return {"created": 1, "updated": 0, "resolved": 0, "reopened": 0, "no_known_fix": 0}

    async def members(db, *, organization_id, vertical):
        return []

    async def forget(user_id, vertical):
        return None

    db = _write_db()
    monkeypatch.setattr(source, "_organization_software", stack)
    monkeypatch.setattr(source, "_user_creds", creds)
    monkeypatch.setattr(source, "_read_opencti", read_opencti)
    monkeypatch.setattr(source, "attach_probe", lambda headers, base_url: headers)
    monkeypatch.setattr(source, "_api", _aev_findings_api([]))
    monkeypatch.setattr(source.store, "member_ids", members)
    monkeypatch.setattr(source.store, "apply_recompute", apply)
    monkeypatch.setattr("app.services.ctem_cases_view.forget_view", forget)
    answer = await source.recompute_for_user(db, organization_id=uuid.uuid4(), user_id="u-1", vertical="cyber")
    assert answer["missing"] == [] and answer["applied"] is True
    assert applied == [["openaev:f-1", "opencti:v-1"]]


@pytest.mark.asyncio
async def test_other_verticals_read_nothing():
    read = await source.read_open_issues(None, "u", "fraud")
    assert read.issues == [] and read.missing == []


def _client(monkeypatch, user):
    app = FastAPI()
    app.include_router(api_module.router, prefix="/api/v1/ctem-cases")
    app.dependency_overrides[deps.get_current_user] = lambda: user

    async def _db():
        yield SimpleNamespace()

    app.dependency_overrides[get_db] = _db

    async def _org(db):
        return uuid.uuid4()

    monkeypatch.setattr(api_module, "_organization_id", _org)

    async def _decide(_db, _user):
        return "decide"  # no CTEM without a right (#2441): the caller holds one

    monkeypatch.setattr(api_module.ctem_access, "level", _decide)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


@pytest.mark.asyncio
async def test_recompute_route_builds_the_callers_own_cases_and_is_throttled(monkeypatch):
    """#11613: any user recomputes, with their own credentials, into their own cases."""
    calls: list[tuple[str, str]] = []

    async def recompute(db, *, organization_id, user_id, vertical):
        calls.append((user_id, vertical))
        return {"applied": True, "vertical": vertical}

    slots = iter([True, False])

    async def slot(user_id, vertical):
        return next(slots)

    monkeypatch.setattr(source, "recompute_for_user", recompute)
    monkeypatch.setattr(api_module, "recompute_slot", slot)
    member = SimpleNamespace(id=uuid.uuid4(), ctem_vertical=None, is_admin=False)
    async with _client(monkeypatch, member) as client:
        first = await client.post("/api/v1/ctem-cases/recompute")
        second = await client.post("/api/v1/ctem-cases/recompute")
    assert first.status_code == 200 and first.json()["applied"] is True
    assert second.status_code == 429
    assert calls == [(str(member.id), "cyber")]


@pytest.mark.asyncio
async def test_recompute_route_answers_409_while_another_recompute_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    """#1953: one recompute per organization at a time; the route never queues a second one."""
    from app.services import ctem_cases_refresh as refresh

    async def busy(db: Any, *, organization_id: Any, user_id: str, vertical: str) -> dict[str, Any]:
        return refresh.busy_answer(vertical)

    async def slot(user_id: str, vertical: str) -> bool:
        return True

    monkeypatch.setattr(refresh, "recompute_as", busy)
    monkeypatch.setattr(api_module, "recompute_slot", slot)
    member = SimpleNamespace(id=uuid.uuid4(), ctem_vertical=None, is_admin=False)
    async with _client(monkeypatch, member) as client:  # type: ignore[no-untyped-call]
        answer = await client.post("/api/v1/ctem-cases/recompute")
    assert answer.status_code == 409 and "already running" in answer.json()["detail"]
