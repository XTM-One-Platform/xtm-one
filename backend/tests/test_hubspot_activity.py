"""The HubSpot activity tools against a fake HubSpot API: the record
timeline, engagements, labels, batch reads, paging, backoff, missing scopes,
forms, the inbound engagement report and the object-type Scope(s).

Every answer is shaped like HubSpot's documented one; nothing reaches HubSpot.
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

from app.api.v1.integration_catalog import PROVIDER_CATALOG
from app.integrations.providers import hubspot
from app.integrations.providers.hubspot_activity import TOOL_SCOPES
from app.integrations.providers.hubspot_records import epoch_ms, iso_utc
from app.services.integration_tools import _SafeIntegrationExecutor
from app.tools.base import is_auth_failure_result
from tests.integration_file_fakes import RecordingPersist, call, fake_api

T0 = datetime(2026, 10, 28, 13, 44, 10, tzinfo=UTC)
FORM = "2c1d3b8c-6c63-4e2b-9d1f-3a0b7f1e2a11"
DEMO_FORM = "7a0e4c55-1b2d-4f6a-8c9e-0d1e2f3a4b5c"
OWNER = {
    "id": "77",
    "firstName": "Ana",
    "lastName": "Rep",
    "email": "ana@acme.example",
    "userId": 1077,
    "teams": [{"id": "11", "name": "EMEA Sales", "primary": True}],
}
DEFINITIONS = {
    "contacts": {
        "lifecyclestage": {
            "name": "lifecyclestage",
            "label": "Lifecycle Stage",
            "type": "enumeration",
            "options": [{"value": "lead", "label": "Lead"}, {"value": "1491411131", "label": "Marketing Qualified Lead"}],
        },
        "hs_lead_status": {
            "name": "hs_lead_status",
            "label": "Lead Status",
            "type": "enumeration",
            "options": [{"value": "OPEN", "label": "Open"}],
        },
        "hubspot_owner_id": {"name": "hubspot_owner_id", "label": "Contact owner", "type": "enumeration", "referencedObjectType": "OWNER"},
    },
    "deals": {"dealstage": {"name": "dealstage", "label": "Deal Stage", "type": "enumeration", "options": []}},
}
SCOPE_403 = {
    "status": "error",
    "message": "This app hasn't been granted all required scopes to make this call.",
    "category": "MISSING_SCOPES",
    "errors": [
        {"message": "One or more of the following scopes are required.", "context": {"requiredGranularScopes": ["crm.objects.emails.read"]}}
    ],
}


class _Unpaced:
    @contextlib.asynccontextmanager
    async def slot(self, *args, **kwargs):
        yield


@pytest.fixture(autouse=True)
def unpaced(monkeypatch):
    """HubSpot's shared account limiter paces requests at 4/s; not in a unit test."""
    monkeypatch.setattr(hubspot, "get_shared_rate_limiter", lambda *a, **k: _Unpaced())


def _tools(scope: Any = None, persist: Any = None) -> tuple[dict[str, Any], _SafeIntegrationExecutor]:
    executor = _SafeIntegrationExecutor()
    defs = hubspot.build_tools("pat-token", executor, object_type_scope=scope, persist_file_fn=persist)
    return {d.name: d for d in defs}, executor


def _ok(payload) -> httpx.Response:
    return httpx.Response(200, json=payload)


class FakeHubSpot:
    """A HubSpot account: ``routes`` maps ``(method, path)`` to an answer."""

    def __init__(self) -> None:
        self.routes: dict[tuple[str, str], Callable[[httpx.Request], httpx.Response] | dict] = {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        route = self.routes.get((request.method, path))
        if route is not None:
            return route(request) if callable(route) else _ok(route)
        if path == "/crm/v3/owners":
            archived = request.url.params.get("archived") == "true"
            return _ok({"results": [] if archived else [OWNER]})
        if path.startswith("/crm/v3/properties/") and path.endswith("/batch/read"):
            object_type = path.split("/")[4]
            names = [i["name"] for i in json.loads(request.content)["inputs"]]
            known = DEFINITIONS.get(object_type, {})
            return _ok({"results": [known[n] for n in names if n in known]})
        if path.startswith("/crm/v4/objects/") and "/associations/" in path:
            return _ok({"results": []})
        if path.startswith("/crm/v4/associations/") and path.endswith("/batch/read"):
            return _ok({"results": []})
        if path.startswith("/automation/v4/sequences/enrollments/contact/"):
            return httpx.Response(404, json={"message": "not enrolled"})
        return httpx.Response(404, json={"message": f"no route for {request.method} {path}"})


def _batch(objects: dict[str, dict]) -> Callable[[httpx.Request], httpx.Response]:
    def answer(request: httpx.Request) -> httpx.Response:
        ids = [i["id"] for i in json.loads(request.content)["inputs"]]
        return _ok({"results": [{"id": i, "properties": objects[i]} for i in ids if i in objects]})

    return answer


def _f5_account() -> FakeHubSpot:
    """Contact 501 submitted Website - Contact at T0; Ana became its owner an hour
    later and emailed it two hours after the submission, the note logged at the
    same second as the email."""
    api = FakeHubSpot()
    api.routes[("GET", "/contacts/v1/contact/vid/501/profile")] = {
        "vid": 501,
        "properties": {"email": {"value": "f5@example.com"}},
        "form-submissions": [
            {
                "conversion-id": "c-1",
                "timestamp": epoch_ms(T0),
                "form-id": FORM,
                "title": "Website - Contact",
                "page-url": "https://filigran.io/contact",
                "page-title": "Contact us",
            }
        ],
    }
    api.routes[("GET", f"/form-integrations/v1/submissions/forms/{FORM}")] = {
        "results": [
            {
                "conversionId": "c-1",
                "submittedAt": epoch_ms(T0),
                "pageUrl": "https://filigran.io/contact",
                "values": [
                    {"name": "email", "value": "f5@example.com"},
                    {"name": "country", "value": "United States"},
                    {"name": "message", "value": "Please call me about OpenCTI"},
                ],
            }
        ]
    }
    api.routes[("GET", "/crm/v4/objects/contacts/501/associations/emails")] = {"results": [{"toObjectId": 9001}]}
    api.routes[("GET", "/crm/v4/objects/contacts/501/associations/notes")] = {"results": [{"toObjectId": 9101}]}
    sent = T0 + timedelta(hours=2)
    api.routes[("POST", "/crm/v3/objects/emails/batch/read")] = _batch(
        {
            "9001": {
                "hs_timestamp": iso_utc(sent),
                "hs_email_direction": "EMAIL",
                "hs_email_status": "SENT",
                "hs_email_subject": "Re: your request",
                "hs_email_text": "Hello, happy to set up a call.",
                "hs_email_from_firstname": "Ana",
                "hs_email_from_lastname": "Rep",
                "hs_email_from_email": "ana@acme.example",
                "hs_email_to_email": "f5@example.com",
                "hubspot_owner_id": "77",
            }
        }
    )
    api.routes[("POST", "/crm/v3/objects/notes/batch/read")] = _batch(
        {"9101": {"hs_timestamp": iso_utc(sent), "hs_note_body": "<p>Left a <b>voicemail</b> too</p>", "hubspot_owner_id": "77"}}
    )
    api.routes[("GET", "/crm/v3/objects/contacts/501")] = {
        "id": "501",
        "properties": {},
        "propertiesWithHistory": {
            "lifecyclestage": [{"value": "lead", "timestamp": iso_utc(T0), "sourceType": "FORM"}],
            "hubspot_owner_id": [
                {"value": "77", "timestamp": iso_utc(T0 + timedelta(hours=1)), "sourceType": "CRM_UI", "updatedByUserId": 1077}
            ],
        },
    }
    return api


# ---------------------------------------------------------------------------
# Timeline
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_timeline_merges_submissions_changes_and_engagements_in_order():
    _defs, executor = _tools()
    with fake_api(_f5_account()):
        result = await call(executor, "get_hubspot_record_timeline", object_type="contact", record_id="501")
    assert not result.is_error, result.content
    text = result.content
    order = [
        "FORM SUBMISSION 'Website - Contact'",
        "PROPERTY CHANGE Lifecycle Stage: Lead (lead) | source FORM",
        "PROPERTY CHANGE Contact owner: Ana Rep <ana@acme.example> (owner 77) | source CRM_UI | by Ana Rep <ana@acme.example>",
        "EMAIL outgoing",
        "NOTE",
    ]
    positions = [text.index(marker) for marker in order]
    assert positions == sorted(positions), text
    assert "message: Please call me about OpenCTI" in text
    assert "page https://filigran.io/contact" in text
    assert "2026-10-28T15:44:10Z | EMAIL outgoing | owner Ana Rep <ana@acme.example> | from Ana Rep <ana@acme.example>" in text
    assert "subject 'Re: your request'" in text
    assert "Left a voicemail too" in text
    assert text.startswith("Timeline of contacts 501: 5 event(s)")


@pytest.mark.asyncio
async def test_an_untouched_contact_shows_its_submission_and_no_engagement():
    api = _f5_account()
    api.routes.pop(("GET", "/crm/v4/objects/contacts/501/associations/emails"))
    api.routes.pop(("GET", "/crm/v4/objects/contacts/501/associations/notes"))
    api.routes[("GET", "/crm/v3/objects/contacts/501")] = {"id": "501", "properties": {}, "propertiesWithHistory": {}}
    _defs, executor = _tools()
    with fake_api(api):
        result = await call(executor, "get_hubspot_record_timeline", object_type="contacts", record_id="501")
    assert "1 event(s) (form submissions: 1)" in result.content
    assert "EMAIL" not in result.content and "owner" not in result.content.split("FORM SUBMISSION")[1]


@pytest.mark.asyncio
async def test_the_timeline_range_and_types_narrow_it():
    _defs, executor = _tools()
    with fake_api(_f5_account()) as sent:
        result = await call(
            executor,
            "get_hubspot_record_timeline",
            object_type="contacts",
            record_id="501",
            from_date="2026-10-28T15:00:00Z",
            types="emails,notes",
        )
    assert "2 event(s)" in result.content
    assert not any(r.url.path.endswith("/profile") for r in sent)
    bad = await call(executor, "get_hubspot_record_timeline", object_type="contacts", record_id="501", types="sms")
    assert bad.is_error and "unknown timeline type 'sms'" in bad.content


@pytest.mark.asyncio
async def test_a_type_the_app_cannot_read_names_the_missing_scope_and_the_rest_is_returned():
    api = _f5_account()
    api.routes[("GET", "/crm/v4/objects/contacts/501/associations/emails")] = lambda r: httpx.Response(403, json=SCOPE_403)
    _defs, executor = _tools()
    with fake_api(api):
        result = await call(executor, "get_hubspot_record_timeline", object_type="contacts", record_id="501")
    assert not result.is_error
    assert "NOTE" in result.content and "EMAIL outgoing" not in result.content
    assert (
        "emails not read: Error: HubSpot access denied while trying to list the emails of contacts 501 - the private "
        "app token is missing the scope(s) crm.objects.emails.read, which get_hubspot_record_timeline needs"
    ) in result.content


# ---------------------------------------------------------------------------
# Engagements are never bare IDs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_associations_to_an_engagement_type_are_read_not_listed_as_ids():
    _defs, executor = _tools()
    with fake_api(_f5_account()):
        result = await call(
            executor, "get_hubspot_associations", object_type="contacts", object_id="501", to_object_type="emails"
        )
    assert "ID: 9001" not in result.content and "(None)" not in result.content
    assert "EMAIL outgoing" in result.content and "subject 'Re: your request'" in result.content


@pytest.mark.asyncio
async def test_an_unlabelled_association_has_no_none_label():
    api = FakeHubSpot()
    api.routes[("GET", "/crm/v4/objects/contacts/501/associations/companies")] = {
        "results": [{"toObjectId": 42, "associationTypes": [{"category": "HUBSPOT_DEFINED", "typeId": 1, "label": None}]}]
    }
    _defs, executor = _tools()
    with fake_api(api):
        result = await call(executor, "get_hubspot_associations", object_type="contacts", object_id="501", to_object_type="companies")
    assert "  ID: 42" in result.content and "None" not in result.content


@pytest.mark.asyncio
async def test_one_engagement_is_read_in_full_with_its_associations():
    api = FakeHubSpot()
    api.routes[("GET", "/crm/v3/objects/emails/9001")] = {
        "id": "9001",
        "properties": {"hs_timestamp": iso_utc(T0), "hs_email_direction": "INCOMING_EMAIL", "hs_email_text": "x" * 30_000, "hs_attachment_ids": "7001"},
        "associations": {"contacts": {"results": [{"id": "501", "type": "email_to_contact"}]}},
    }
    _defs, executor = _tools()
    with fake_api(api) as sent:
        result = await call(executor, "get_hubspot_engagement", engagement_type="email", engagement_id="9001")
    assert "EMAIL incoming" in result.content and "associated contacts 501" in result.content
    assert "[body cut: 10,000 more characters" in result.content
    assert "download_hubspot_file(file_id, engagement_type='emails', engagement_id='9001')" in result.content
    assert sent[0].url.params["associations"] == "contacts,companies,deals,tickets"


@pytest.mark.asyncio
async def test_a_missing_scope_names_the_scope_and_the_tool():
    api = FakeHubSpot()
    api.routes[("GET", "/crm/v3/objects/emails/9001")] = lambda r: httpx.Response(403, json=SCOPE_403)
    _defs, executor = _tools()
    with fake_api(api):
        result = await call(executor, "get_hubspot_engagement", engagement_type="emails", engagement_id="9001")
    assert result.content.startswith("Error: HubSpot access denied while trying to read emails 9001")
    assert "missing the scope(s) crm.objects.emails.read, which get_hubspot_engagement needs" in result.content
    assert is_auth_failure_result(result.content)


@pytest.mark.asyncio
async def test_a_403_naming_no_scope_gets_the_tools_declared_scopes():
    api = FakeHubSpot()
    api.routes[("GET", "/crm/v3/objects/contacts/1")] = lambda r: httpx.Response(403, json={"message": "Forbidden"})
    _defs, executor = _tools()
    with fake_api(api):
        result = await call(executor, "get_hubspot_contact", contact_id="1")
    assert "access denied" in result.content
    assert "(get_hubspot_contact uses the private app scopes crm.objects.contacts.read" in result.content


@pytest.mark.asyncio
async def test_engagement_search_filters_on_the_record_and_the_range():
    api = FakeHubSpot()
    bodies = []

    def search(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return _ok({"total": 1, "results": [{"id": "9001", "properties": {"hs_timestamp": iso_utc(T0), "hs_email_direction": "EMAIL"}}]})

    api.routes[("POST", "/crm/v3/objects/emails/search")] = search
    _defs, executor = _tools()
    with fake_api(api):
        result = await call(
            executor,
            "search_hubspot_engagements",
            engagement_type="emails",
            associated_object_type="contact",
            associated_object_id="501",
            from_date="2026-10-01",
            to_date="2026-10-28",
            filters_json='[{"propertyName": "hs_email_direction", "operator": "EQ", "value": "EMAIL"}]',
        )
    assert "Found 1 emails from 2026-10-01T00:00:00Z to 2026-10-28T23:59:59Z" in result.content
    (filters,) = [g["filters"] for g in bodies[0]["filterGroups"]]
    assert {"propertyName": "associations.contact", "operator": "EQ", "value": "501"} in filters
    window = next(f for f in filters if f["propertyName"] == "hs_timestamp")
    assert window["operator"] == "BETWEEN" and window["value"] == str(epoch_ms(datetime(2026, 10, 1, tzinfo=UTC)))
    assert {"propertyName": "hs_email_direction", "operator": "EQ", "value": "EMAIL"} in filters
    assert bodies[0]["sorts"] == [{"propertyName": "hs_timestamp", "direction": "DESCENDING"}]


# ---------------------------------------------------------------------------
# Labels
# ---------------------------------------------------------------------------


def _contact_api(lifecycle: str = "1491411131") -> FakeHubSpot:
    api = FakeHubSpot()
    api.routes[("GET", "/crm/v3/objects/contacts/501")] = {
        "id": "501",
        "properties": {
            "firstname": "Jo",
            "lastname": "F5",
            "email": "f5@example.com",
            "lifecyclestage": lifecycle,
            "hs_lead_status": "OPEN",
            "hubspot_owner_id": "77",
            "hubspot_owner_assigneddate": "2026-10-28T14:44:10Z",
            "country": "United States",
            "recent_conversion_event_name": "Website - Contact: Contact us",
            "recent_conversion_date": "2026-10-28T13:44:10Z",
            "custom_region": "NA",
        },
    }
    return api


@pytest.mark.asyncio
async def test_a_contact_shows_labels_owner_team_and_conversion():
    _defs, executor = _tools()
    with fake_api(_contact_api()):
        result = await call(executor, "get_hubspot_contact", contact_id="501", properties="custom_region")
    text = result.content
    assert "Lifecycle Stage: Marketing Qualified Lead (1491411131)" in text
    assert "Lead Status: Open (OPEN)" in text
    assert "Owner: Ana Rep <ana@acme.example> (owner 77) | team(s) EMEA Sales (primary) | assigned 2026-10-28T14:44:10Z" in text
    assert "Location: United States" in text
    assert "Recent conversion: Website - Contact: Contact us at 2026-10-28T13:44:10Z" in text
    assert "custom_region: NA" in text


@pytest.mark.asyncio
async def test_raw_values_skip_every_lookup():
    _defs, executor = _tools()
    with fake_api(_contact_api()) as sent:
        result = await call(executor, "get_hubspot_contact", contact_id="501", raw_values=True)
    assert "Lifecycle Stage: 1491411131" in result.content and "Owner: 77" in result.content
    assert len(sent) == 1


@pytest.mark.asyncio
async def test_a_stage_the_options_do_not_name_is_read_from_the_pipelines():
    api = FakeHubSpot()
    api.routes[("GET", "/crm/v3/objects/deals/9")] = {"id": "9", "properties": {"dealname": "Big", "dealstage": "1234", "pipeline": "default"}}
    api.routes[("GET", "/crm/v3/pipelines/deals")] = {
        "results": [{"id": "default", "label": "Sales Pipeline", "stages": [{"id": "1234", "label": "Demo scheduled"}]}]
    }
    _defs, executor = _tools()
    with fake_api(api):
        result = await call(executor, "get_hubspot_deal", deal_id="9")
    assert "Stage: Demo scheduled (1234)" in result.content
    assert "Pipeline: Sales Pipeline (default)" in result.content


@pytest.mark.asyncio
async def test_unreadable_definitions_leave_raw_values_and_say_why():
    api = _contact_api()
    forbidden = lambda r: httpx.Response(403, json={"message": "no", "errors": [{"message": "x", "context": {"missingScopes": ["crm.schemas.contacts.read"]}}]})  # noqa: E731
    api.routes[("POST", "/crm/v3/properties/contacts/batch/read")] = forbidden
    api.routes[("GET", "/crm/v3/properties/contacts")] = forbidden
    _defs, executor = _tools()
    with fake_api(api):
        result = await call(executor, "get_hubspot_contact", contact_id="501")
    assert "Lifecycle Stage: 1491411131" in result.content
    assert "Owner: Ana Rep <ana@acme.example> (owner 77)" in result.content
    assert "enumeration labels not resolved" in result.content and "crm.schemas.contacts.read" in result.content


# ---------------------------------------------------------------------------
# Batch reads, paging, backoff
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_batch_records_are_read_100_per_request_and_the_missing_named():
    api = FakeHubSpot()
    sizes = []

    def read(request: httpx.Request) -> httpx.Response:
        ids = [i["id"] for i in json.loads(request.content)["inputs"]]
        sizes.append(len(ids))
        return _ok({"results": [{"id": i, "properties": {"email": f"{i}@x.example"}} for i in ids if i != "7"]})

    api.routes[("POST", "/crm/v3/objects/contacts/batch/read")] = read
    _defs, executor = _tools()
    with fake_api(api):
        result = await call(executor, "batch_get_hubspot_records", object_type="contacts", ids=[str(i) for i in range(1, 251)])
    assert sizes == [100, 100, 50]
    assert result.content.startswith("249 of 250 contacts:") and "Not found: 7" in result.content


@pytest.mark.asyncio
async def test_batch_associations_read_engagements_for_each_record():
    api = _f5_account()
    sizes = []

    def assoc(request: httpx.Request) -> httpx.Response:
        ids = [i["id"] for i in json.loads(request.content)["inputs"]]
        sizes.append(len(ids))
        return _ok({"results": [{"from": {"id": "501"}, "to": [{"toObjectId": 9001, "associationTypes": []}]}] if "501" in ids else []})

    api.routes[("POST", "/crm/v4/associations/contacts/emails/batch/read")] = assoc
    _defs, executor = _tools()
    with fake_api(api):
        result = await call(
            executor, "batch_get_hubspot_associations", object_type="contacts", ids=",".join(str(i) for i in range(400, 550)), to_object_type="email"
        )
    assert sizes == [100, 50]
    assert "contacts 501: 1 emails" in result.content
    assert "email outgoing | Ana Rep <ana@acme.example> | Re: your request | id 9001" in result.content


@pytest.mark.asyncio
async def test_a_search_pages_200_at_a_time_up_to_the_count():
    api = FakeHubSpot()
    limits, cursors = [], []

    def search(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        limits.append(body["limit"])
        cursors.append(body.get("after"))
        start = int(body.get("after") or 0)
        page = [{"id": str(start + i), "properties": {"firstname": "C"}} for i in range(body["limit"])]
        return _ok({"total": 1000, "results": page, "paging": {"next": {"after": str(start + len(page))}}})

    api.routes[("POST", "/crm/v3/objects/contacts/search")] = search
    _defs, executor = _tools()
    with fake_api(api):
        result = await call(executor, "search_hubspot_contacts", count=450)
    assert limits == [200, 200, 50] and cursors == [None, "200", "400"]
    assert result.content.startswith("Found 1000 contacts:")
    assert result.content.count("| ID: ") == 450 and 'after="450"' in result.content


@pytest.mark.asyncio
async def test_a_search_page_without_a_cursor_goes_on_from_the_offset_while_the_total_says_more() -> None:
    api = FakeHubSpot()
    limits, cursors = [], []

    def search(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        limits.append(body["limit"])
        cursors.append(body.get("after"))
        start = int(body.get("after") or 0)
        size = min(100, body["limit"], 230 - start)
        page = [{"id": str(start + i), "properties": {"dealname": "D"}} for i in range(size)]
        return _ok({"total": 230, "results": page})

    api.routes[("POST", "/crm/v3/objects/deals/search")] = search
    _defs, executor = _tools()
    with fake_api(api):
        result = await call(executor, "search_hubspot_deals", count=200)
    assert cursors == [None, "100"] and limits == [200, 100]
    assert result.content.startswith("Found 230 deals:")
    assert result.content.count("| ID: ") == 200
    assert "Showing 200 of 230" in result.content and 'after="200"' in result.content


@pytest.mark.asyncio
async def test_a_search_without_a_total_counts_what_it_lists_and_adds_no_note() -> None:
    api = FakeHubSpot()
    api.routes[("POST", "/crm/v3/objects/deals/search")] = lambda r: _ok(
        {"results": [{"id": str(i), "properties": {"dealname": "D"}} for i in range(3)]}
    )
    _defs, executor = _tools()
    with fake_api(api):
        result = await call(executor, "search_hubspot_deals", count=10)
    assert result.content.startswith("Found 3 deals:")
    assert "Showing" not in result.content and "after=" not in result.content


@pytest.mark.asyncio
async def test_a_search_whose_cursor_is_not_an_offset_stops_and_tells_how_many_are_left() -> None:
    api = FakeHubSpot()
    calls = []

    def search(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content).get("after"))
        return _ok({"total": 50, "results": [{"id": str(i), "properties": {"dealname": "D"}} for i in range(5)]})

    api.routes[("POST", "/crm/v3/objects/deals/search")] = search
    _defs, executor = _tools()
    with fake_api(api):
        result = await call(executor, "search_hubspot_deals", count=20, after="opaque-token")
    assert calls == ["opaque-token"]
    assert "Showing 5 of 50: HubSpot gave no next page; narrow the filters" in result.content


@pytest.mark.asyncio
async def test_reads_retry_a_rate_limit_or_a_gateway_error_and_writes_do_not(monkeypatch):
    from app.integrations import http as integration_http

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(integration_http.asyncio, "sleep", no_sleep)
    answers = iter([httpx.Response(429, headers={"Retry-After": "1"}), httpx.Response(503), _ok({"id": "5", "properties": {"hs_note_body": "ok"}})])
    api = FakeHubSpot()
    api.routes[("GET", "/crm/v3/objects/notes/5")] = lambda r: next(answers)
    api.routes[("POST", "/crm/v3/objects/contacts")] = lambda r: httpx.Response(503)
    _defs, executor = _tools()
    with fake_api(api) as sent:
        read = await call(executor, "get_hubspot_engagement", engagement_type="notes", engagement_id="5")
        write = await call(executor, "create_hubspot_contact", email="a@b.example")
    assert "NOTE" in read.content
    assert [r.method for r in sent].count("GET") == 3
    assert [r.method for r in sent].count("POST") == 1
    assert "server error (503)" in write.content


# ---------------------------------------------------------------------------
# Forms
# ---------------------------------------------------------------------------


def _forms_account() -> FakeHubSpot:
    api = _f5_account()
    api.routes[("GET", "/marketing/v3/forms")] = {
        "results": [{"id": FORM, "name": "Website - Contact"}, {"id": DEMO_FORM, "name": "Book a demo"}]
    }
    api.routes[("GET", f"/form-integrations/v1/submissions/forms/{FORM}")] = {
        "results": [
            {"conversionId": "c-1", "submittedAt": epoch_ms(T0), "pageUrl": "https://filigran.io/contact", "values": [{"name": "email", "value": "F5@example.com"}]},
            {"conversionId": "c-2", "submittedAt": epoch_ms(T0 - timedelta(days=1)), "pageUrl": "https://filigran.io/contact", "values": [{"name": "email", "value": "buyer@corp.example"}]},
            {"conversionId": "c-3", "submittedAt": epoch_ms(T0 - timedelta(days=2)), "pageUrl": "https://filigran.io/contact", "values": [{"name": "email", "value": "ghost@nowhere.example"}]},
            {"conversionId": "c-0", "submittedAt": epoch_ms(datetime(2026, 9, 15, tzinfo=UTC)), "values": [{"name": "email", "value": "old@x.example"}]},
        ],
        "paging": {"next": {"after": "never-read"}},
    }
    contacts = {
        "f5@example.com": {"id": "501", "properties": {"email": "f5@example.com", "firstname": "Jo", "country": "United States"}},
        "buyer@corp.example": {"id": "502", "properties": {"email": "buyer@corp.example", "firstname": "Bo", "country": "France", "hubspot_owner_id": "77"}},
    }

    def by_email(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["idProperty"] == "email"
        return _ok({"results": [contacts[i["id"]] for i in body["inputs"] if i["id"] in contacts]})

    api.routes[("POST", "/crm/v3/objects/contacts/batch/read")] = by_email
    return api


@pytest.mark.asyncio
async def test_form_submissions_come_with_values_page_and_contact():
    _defs, executor = _tools()
    with fake_api(_forms_account()):
        result = await call(executor, "list_hubspot_form_submissions", form_id="Website - Contact", from_date="2026-10-01")
    text = result.content
    assert text.startswith(f"3 submission(s) of form 'Website - Contact' ({FORM}) since 2026-10-01T00:00:00Z")
    assert "contact 501 (f5@example.com)" in text and "contact 502 (buyer@corp.example)" in text
    assert "email ghost@nowhere.example (no contact found)" in text
    assert "old@x.example" not in text and "never-read" not in text


@pytest.mark.asyncio
async def test_an_ambiguous_form_name_is_refused():
    api = _forms_account()
    api.routes[("GET", "/marketing/v3/forms")] = {"results": [{"id": FORM, "name": "Partner inquiry EMEA"}, {"id": DEMO_FORM, "name": "Partner inquiry US"}]}
    _defs, executor = _tools()
    with fake_api(api):
        result = await call(executor, "list_hubspot_form_submissions", form_id="Partner inquiry")
    assert result.is_error and "names several forms" in result.content


@pytest.mark.asyncio
async def test_a_contacts_whole_form_history_comes_with_its_values():
    _defs, executor = _tools()
    with fake_api(_f5_account()):
        result = await call(executor, "get_hubspot_contact_form_submissions", contact_id="501")
    assert "1 form submission(s) of contact 501" in result.content
    assert "message: Please call me about OpenCTI" in result.content and "page title 'Contact us'" in result.content


@pytest.mark.asyncio
async def test_without_the_legacy_profile_the_conversion_history_is_read():
    api = _f5_account()
    api.routes[("GET", "/contacts/v1/contact/vid/501/profile")] = lambda r: httpx.Response(410, json={"message": "gone"})
    api.routes[("GET", "/crm/v3/objects/contacts/501")] = {
        "id": "501",
        "properties": {"email": "f5@example.com"},
        "propertiesWithHistory": {"recent_conversion_event_name": [{"value": "Website - Contact: Contact us", "timestamp": iso_utc(T0)}]},
    }
    api.routes[("GET", "/marketing/v3/forms")] = {"results": [{"id": FORM, "name": "Website - Contact"}]}
    _defs, executor = _tools()
    with fake_api(api):
        result = await call(executor, "get_hubspot_contact_form_submissions", contact_id="501")
    assert "FORM SUBMISSION 'Website - Contact: Contact us'" in result.content
    assert "message: Please call me about OpenCTI" in result.content
    assert "read from the contact's conversion history" in result.content


# ---------------------------------------------------------------------------
# Inbound engagement report
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_inbound_report_counts_touches_replies_and_lists_the_untouched():
    api = _forms_account()
    api.routes[("POST", "/crm/v4/associations/contacts/emails/batch/read")] = {
        "results": [{"from": {"id": "502"}, "to": [{"toObjectId": 9201, "associationTypes": []}, {"toObjectId": 9202}]}]
    }
    first = T0 - timedelta(days=1) + timedelta(hours=3)
    api.routes[("POST", "/crm/v3/objects/emails/batch/read")] = _batch(
        {
            "9201": {"hs_timestamp": iso_utc(first), "hs_email_direction": "EMAIL", "hs_email_status": "SENT", "hubspot_owner_id": "77"},
            "9202": {"hs_timestamp": iso_utc(first + timedelta(hours=2)), "hs_email_direction": "INCOMING_EMAIL"},
        }
    )
    _defs, executor = _tools()
    with fake_api(api) as sent:
        result = await call(
            executor,
            "get_hubspot_inbound_engagement_report",
            from_date="2026-10-01",
            to_date="2026-10-28",
            forms=["Website - Contact"],
            group_by="region",
        )
    text = result.content
    assert "Submissions in range: 3; contacts: 2" in text and "submissions whose email matches no contact: 1" in text
    assert (
        "All contacts | submitted 2 | assigned 1 (50%) | touched 1 (50%) | touched within 24 h 1 (50%) | "
        "first touch median 3.0 h, mean 3.0 h | replied 1 (50%)"
    ) in text
    assert "  EMEA Sales | submitted 1 |" in text and "  (no owner) | submitted 1 |" in text
    assert "first touch by manual email 1" in text
    untouched = text.split("Untouched contacts (1), oldest submission first:")[1]
    assert "contact 501 Jo <f5@example.com> | United States | Website - Contact at 2026-10-28T13:44:10Z | no owner" in untouched
    assert "How this is read:" in text
    assert len(sent) < 50


@pytest.mark.asyncio
async def test_the_report_refuses_a_bad_range_or_grouping():
    _defs, executor = _tools()
    missing = await call(executor, "get_hubspot_inbound_engagement_report", from_date="")
    bad = await call(executor, "get_hubspot_inbound_engagement_report", from_date="2026-10-01", group_by="planet")
    reversed_range = await call(executor, "get_hubspot_inbound_engagement_report", from_date="2026-10-28", to_date="2026-10-01")
    assert missing.is_error and bad.is_error and reversed_range.is_error
    assert "from_date is after to_date" in reversed_range.content


# ---------------------------------------------------------------------------
# Sequences and teams
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sequence_enrollments_list_history_steps_and_reply():
    api = FakeHubSpot()
    enrolled = T0 + timedelta(hours=1)
    api.routes[("GET", "/automation/v4/sequences/enrollments/contact/501")] = {
        "id": "e1",
        "sequenceId": "555",
        "sequenceName": "Inbound follow-up",
        "enrolledAt": iso_utc(enrolled),
        "enrolledByEmail": "ana@acme.example",
    }
    api.routes[("GET", "/crm/v3/objects/contacts/501")] = {
        "id": "501",
        "properties": {"email": "f5@example.com", "hs_sequences_enrolled_count": "1"},
        "propertiesWithHistory": {"hs_latest_sequence_enrolled": [{"value": "555", "timestamp": iso_utc(enrolled)}]},
    }
    api.routes[("POST", "/crm/v3/objects/emails/search")] = {
        "total": 2,
        "results": [
            {"id": "1", "properties": {"hs_timestamp": iso_utc(enrolled + timedelta(minutes=5)), "hs_email_direction": "EMAIL", "hs_sequence_id": "555", "hs_email_subject": "Step 1", "hs_email_open_count": "3"}},
            {"id": "2", "properties": {"hs_timestamp": iso_utc(enrolled + timedelta(days=1)), "hs_email_direction": "INCOMING_EMAIL", "hs_email_subject": "Re: Step 1"}},
        ],
    }
    _defs, executor = _tools()
    with fake_api(api):
        result = await call(executor, "get_hubspot_sequence_enrollments", contact_id="501")
    text = result.content
    assert "Now enrolled in 'Inbound follow-up' (sequence 555) since 2026-10-28T14:44:10Z by ana@acme.example." in text
    assert "sequence 555 'Inbound follow-up' | enrolled 2026-10-28T14:44:10Z | still enrolled | 1 email(s) sent | replied 2026-10-29T14:44:10Z" in text
    assert "email 1: 2026-10-28T14:49:10Z | 'Step 1' | opens 3" in text


@pytest.mark.asyncio
async def test_teams_fall_back_to_the_owners_when_the_teams_scope_is_missing():
    api = FakeHubSpot()
    api.routes[("GET", "/settings/v3/users/teams")] = lambda r: httpx.Response(
        403, json={"errors": [{"message": "x", "context": {"requiredGranularScopes": ["settings.users.teams.read"]}}]}
    )
    _defs, executor = _tools()
    with fake_api(api):
        result = await call(executor, "list_hubspot_teams")
    assert "EMEA Sales (team id 11): 1 member(s): Ana Rep <ana@acme.example> (owner 77)" in result.content
    assert "settings.users.teams.read" in result.content


@pytest.mark.asyncio
async def test_teams_list_their_members():
    api = FakeHubSpot()
    api.routes[("GET", "/settings/v3/users/teams")] = {"results": [{"id": "11", "name": "EMEA Sales", "userIds": ["1077"], "secondaryUserIds": []}]}
    _defs, executor = _tools()
    with fake_api(api):
        result = await call(executor, "list_hubspot_teams")
    assert result.content == "1 team(s):\nEMEA Sales (team id 11): 1 member(s): Ana Rep <ana@acme.example> (owner 77)"


# ---------------------------------------------------------------------------
# Attachments
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_file_downloaded_through_its_engagement_comes_with_the_engagement_text():
    api = FakeHubSpot()
    api.routes[("GET", "/crm/v3/objects/notes/301")] = {
        "id": "301",
        "properties": {"hs_timestamp": iso_utc(T0), "hs_attachment_ids": "7001", "hs_note_body": "<p>Spiky.ai meeting report: <b>next steps</b></p>"},
    }
    api.routes[("GET", "/files/v3/files/7001")] = {"id": "7001", "name": "report", "extension": "pdf", "size": 9, "access": "HIDDEN_PRIVATE"}
    api.routes[("GET", "/files/v3/files/7001/signed-url")] = {"url": "https://cdn.hubspotusercontent.example/r.pdf", "expiresAt": "2026-10-29T00:00:00Z"}

    def responder(request: httpx.Request) -> httpx.Response:
        if request.url.host == "cdn.hubspotusercontent.example":
            return httpx.Response(200, content=b"%PDF-1.4 x", headers={"content-type": "application/pdf"})
        return api(request)

    persist = RecordingPersist()
    _defs, executor = _tools(persist=persist)
    with fake_api(responder):
        result = await call(executor, "download_hubspot_file", file_id="7001", engagement_type="notes", engagement_id="301")
        other = await call(executor, "download_hubspot_file", file_id="9999", engagement_type="notes", engagement_id="301")
    assert not result.is_error, result.content
    assert "Spiky.ai meeting report: next steps" in result.content
    assert other.is_error and "file 9999 is not attached to notes 301" in other.content


# ---------------------------------------------------------------------------
# Object-type Scope(s)
# ---------------------------------------------------------------------------


def test_contact_data_tools_go_with_the_contact_type():
    contact_tools, _ = _tools(frozenset({"contact"}))
    deal_tools, _ = _tools(frozenset({"deal"}))
    for name in ("list_hubspot_form_submissions", "get_hubspot_sequence_enrollments", "get_hubspot_inbound_engagement_report"):
        assert name in contact_tools and name not in deal_tools
    for name in ("list_hubspot_teams", "list_hubspot_properties", "get_hubspot_record_timeline", "get_hubspot_engagement"):
        assert name in deal_tools


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool", "arguments", "message"),
    [
        ("get_hubspot_record_timeline", {"object_type": "deals", "record_id": "9"}, "'deals' is outside the Scope(s)"),
        ("search_hubspot_engagements", {"engagement_type": "emails"}, "pass associated_object_type"),
        ("batch_get_hubspot_records", {"object_type": "emails", "ids": ["1"]}, "'emails' is outside the Scope(s)"),
        ("get_hubspot_associations", {"object_type": "deals", "object_id": "9", "to_object_type": "emails"}, "'deals' is outside"),
    ],
)
async def test_activity_outside_the_scope_is_refused_before_any_request(tool, arguments, message):
    _defs, executor = _tools(frozenset({"contact"}))
    with fake_api(_f5_account()) as sent:
        result = await call(executor, tool, **arguments)
    assert result.is_error and message in result.content
    assert sent == []


@pytest.mark.asyncio
async def test_the_activity_of_an_allowed_record_is_reachable_under_a_scope():
    _defs, executor = _tools(frozenset({"contact"}))
    with fake_api(_f5_account()):
        associations = await call(executor, "get_hubspot_associations", object_type="contacts", object_id="501", to_object_type="emails")
        timeline = await call(executor, "get_hubspot_record_timeline", object_type="contact", record_id="501", types="emails")
    assert "EMAIL outgoing" in associations.content and "EMAIL outgoing" in timeline.content


@pytest.mark.asyncio
async def test_an_engagement_of_no_allowed_record_is_refused():
    api = FakeHubSpot()
    api.routes[("GET", "/crm/v3/objects/notes/5")] = {
        "id": "5",
        "properties": {"hs_note_body": "deal only"},
        "associations": {"deals": {"results": [{"id": "9"}]}},
    }
    _defs, executor = _tools(frozenset({"contact"}))
    with fake_api(api):
        result = await call(executor, "get_hubspot_engagement", engagement_type="notes", engagement_id="5")
    assert result.is_error and "not associated with a record of an object type" in result.content
    assert "deal only" not in result.content


# ---------------------------------------------------------------------------
# Scopes: descriptions and the catalog say the same
# ---------------------------------------------------------------------------


def test_every_tool_states_its_scopes_and_the_catalog_lists_them_all():
    defs, _ = _tools()
    assert set(defs) <= set(TOOL_SCOPES)
    for name, definition in defs.items():
        assert "HubSpot private app scopes it uses" in definition.description, name
    listed = {p["scope"] for p in PROVIDER_CATALOG["hubspot"]["setup_guide"]["permissions"]}
    needed = {scope for scopes in TOOL_SCOPES.values() for scope in scopes}
    assert needed <= listed, sorted(needed - listed)
