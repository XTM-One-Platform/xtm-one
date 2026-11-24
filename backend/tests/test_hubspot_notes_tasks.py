"""HubSpot notes and tasks on a record, custom ticket properties, structured
engagement search and the object type of generic (lead) webhook events."""

from __future__ import annotations

import contextlib
import json

import httpx
import pytest

from app.api.v1.webhooks import _hubspot_object_type
from app.integrations.providers import hubspot
from app.integrations.providers.hubspot_activity import TOOL_SCOPES
from app.services.integration_tools import _SafeIntegrationExecutor
from tests.integration_file_fakes import call, fake_api


class _Unpaced:
    @contextlib.asynccontextmanager
    async def slot(self, *args, **kwargs):
        yield


@pytest.fixture(autouse=True)
def unpaced(monkeypatch):
    monkeypatch.setattr(hubspot, "get_shared_rate_limiter", lambda *a, **k: _Unpaced())


def _tools(scope=None):
    executor = _SafeIntegrationExecutor()
    defs = hubspot.build_tools("pat-token", executor, object_type_scope=scope)
    return {d.name: d for d in defs}, executor


class FakeHubSpot:
    def __init__(self, routes: dict) -> None:
        self.routes = routes

    def __call__(self, request: httpx.Request) -> httpx.Response:
        route = self.routes.get((request.method, request.url.path))
        if route is None:
            return httpx.Response(404, json={"message": f"no route for {request.method} {request.url.path}"})
        return route(request) if callable(route) else httpx.Response(200, json=route)


def test_note_and_task_tools_are_listed_with_their_write_scopes():
    defs, _ = _tools()
    assert {"create_hubspot_note", "create_hubspot_task"} <= set(defs)
    assert "crm.objects.notes.write" in TOOL_SCOPES["create_hubspot_note"]
    assert "crm.objects.tasks.write" in TOOL_SCOPES["create_hubspot_task"]
    assert "crm.objects.notes.write" in defs["create_hubspot_note"].description


@pytest.mark.asyncio
async def test_a_note_is_created_and_associated_with_its_record():
    created: list[dict] = []

    def post(request: httpx.Request) -> httpx.Response:
        created.append(json.loads(request.content))
        return httpx.Response(201, json={"id": "501"})

    api = FakeHubSpot(
        {
            ("POST", "/crm/v3/objects/notes"): post,
            ("PUT", "/crm/v4/objects/notes/501/associations/default/deals/9"): {},
        }
    )
    _defs, executor = _tools()
    with fake_api(api) as sent:
        result = await call(executor, "create_hubspot_note", object_type="deal", record_id="9", body="Call recap")
    assert not result.is_error, result.content
    assert "Created note 501 on deals 9" in result.content
    assert created[0]["properties"]["hs_note_body"] == "Call recap"
    assert created[0]["properties"]["hs_timestamp"]
    assert [r.method for r in sent] == ["POST", "PUT"]


@pytest.mark.asyncio
async def test_a_task_carries_owner_due_date_and_priority():
    created: list[dict] = []

    def post(request: httpx.Request) -> httpx.Response:
        created.append(json.loads(request.content))
        return httpx.Response(201, json={"id": "601"})

    api = FakeHubSpot(
        {
            ("POST", "/crm/v3/objects/tasks"): post,
            ("PUT", "/crm/v4/objects/tasks/601/associations/default/contacts/3"): {},
        }
    )
    _defs, executor = _tools()
    with fake_api(api):
        result = await call(
            executor,
            "create_hubspot_task",
            object_type="contacts",
            record_id="3",
            subject="Send the proposal",
            due_date="2026-12-01",
            owner_id="77",
            priority="high",
        )
    assert "Created task 601 'Send the proposal' on contacts 3" in result.content
    props = created[0]["properties"]
    assert props["hs_task_subject"] == "Send the proposal"
    assert props["hs_task_priority"] == "HIGH"
    assert props["hubspot_owner_id"] == "77"
    assert props["hs_timestamp"].startswith("2026-12-01")


@pytest.mark.asyncio
async def test_a_task_with_an_unknown_priority_sends_nothing():
    _defs, executor = _tools()
    with fake_api(FakeHubSpot({})) as sent:
        result = await call(
            executor, "create_hubspot_task", object_type="deals", record_id="9", subject="x", priority="urgent"
        )
    assert "priority must be one of" in result.content
    assert sent == []


@pytest.mark.asyncio
async def test_a_failed_association_names_the_created_note():
    api = FakeHubSpot({("POST", "/crm/v3/objects/notes"): {"id": "502"}})
    _defs, executor = _tools()
    with fake_api(api):
        result = await call(executor, "create_hubspot_note", object_type="tickets", record_id="4", body="hi")
    assert "Note 502 was created but is not associated with tickets 4" in result.content


@pytest.mark.asyncio
async def test_the_object_type_scope_refuses_a_note_on_another_type():
    _defs, executor = _tools(scope=frozenset({"deal"}))
    with fake_api(FakeHubSpot({})) as sent:
        result = await call(executor, "create_hubspot_note", object_type="contacts", record_id="3", body="hi")
    assert result.is_error and "outside the Scope(s)" in result.content
    assert sent == []


@pytest.mark.asyncio
async def test_a_ticket_reads_its_custom_properties():
    def get(request: httpx.Request) -> httpx.Response:
        assert "customer_tier" in request.url.params["properties"].split(",")
        return httpx.Response(
            200,
            json={"id": "40", "properties": {"subject": "Access", "customer_tier": "Gold"}},
        )

    api = FakeHubSpot({("GET", "/crm/v3/objects/tickets/40"): get})
    _defs, executor = _tools()
    with fake_api(api):
        result = await call(executor, "get_hubspot_ticket", ticket_id="40", properties="customer_tier", raw_values=True)
    assert "customer_tier: Gold" in result.content


@pytest.mark.asyncio
async def test_a_ticket_search_requests_and_shows_custom_properties():
    seen: list[dict] = []

    def search(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(
            200, json={"total": 1, "results": [{"id": "41", "properties": {"subject": "VPN", "customer_tier": "Gold"}}]}
        )

    api = FakeHubSpot({("POST", "/crm/v3/objects/tickets/search"): search})
    _defs, executor = _tools()
    with fake_api(api):
        result = await call(executor, "search_hubspot_tickets", properties="customer_tier", raw_values=True)
    assert "customer_tier" in seen[0]["properties"]
    assert "customer_tier: Gold" in result.content
    assert result.structured[0]["properties"]["customer_tier"] == "Gold"


@pytest.mark.asyncio
async def test_an_engagement_search_lists_its_records_as_the_structured_value():
    api = FakeHubSpot(
        {
            ("POST", "/crm/v3/objects/notes/search"): {
                "total": 1,
                "results": [
                    {"id": "88", "properties": {"hs_note_body": "Recap", "hs_timestamp": "2026-11-20T10:00:00Z"}}
                ],
            }
        }
    )
    _defs, executor = _tools()
    with fake_api(api):
        result = await call(executor, "search_hubspot_engagements", engagement_type="notes")
    assert result.structured and result.structured[0]["id"] == "88"


@pytest.mark.asyncio
async def test_an_empty_engagement_search_is_an_empty_structured_list():
    api = FakeHubSpot({("POST", "/crm/v3/objects/tasks/search"): {"total": 0, "results": []}})
    _defs, executor = _tools()
    with fake_api(api):
        result = await call(executor, "search_hubspot_engagements", engagement_type="tasks")
    assert result.structured == []


@pytest.mark.parametrize(
    ("subscription", "type_id", "expected"),
    [
        ("object.creation", "0-136", "lead"),
        ("object.propertyChange", "0-3", "deal"),
        ("object.deletion", "2-1234", "2-1234"),
        ("contact.creation", None, "contact"),
        ("object.creation", None, "object"),
        ("nodot", "0-136", "unknown"),
    ],
)
def test_a_generic_event_takes_its_object_type_from_its_type_id(subscription, type_id, expected):
    assert _hubspot_object_type(subscription, type_id) == expected
