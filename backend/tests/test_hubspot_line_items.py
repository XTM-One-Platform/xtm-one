"""HubSpot deal line items and labelled associations.

A deal's ACV and TCV are calculated from its line items, so they are corrected
through the line-item tools; a labelled association (a deal's Primary company)
is added and removed by label name or by category and type id, the records'
other association types kept.
"""

from __future__ import annotations

import contextlib
import json

import httpx
import pytest

from app.integrations.providers import hubspot
from app.integrations.providers.hubspot_activity import TOOL_SCOPES
from app.integrations.providers.hubspot_line_items import format_line_item, line_items_sum
from app.integrations.providers.hubspot_records import association_label, association_type_text
from app.integrations.providers.hubspot_scope import TOOL_OBJECT_TYPE
from app.services.integration_tools import _SafeIntegrationExecutor
from app.services.tool_classification import infer_tool_type
from tests.integration_file_fakes import call, fake_api

LINE_ITEM_TOOLS = (
    "list_hubspot_line_items",
    "create_hubspot_line_item",
    "update_hubspot_line_item",
    "delete_hubspot_line_item",
)
LABELS = {
    "results": [
        {"category": "HUBSPOT_DEFINED", "typeId": 341, "label": None},
        {"category": "HUBSPOT_DEFINED", "typeId": 5, "label": "Primary"},
        {"category": "USER_DEFINED", "typeId": 17, "label": "Reseller"},
    ]
}


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
    """``routes`` maps ``(method, path)`` to a JSON answer or a responder."""

    def __init__(self, routes: dict) -> None:
        self.routes = routes

    def __call__(self, request: httpx.Request) -> httpx.Response:
        route = self.routes.get((request.method, request.url.path))
        if route is None:
            return httpx.Response(404, json={"message": f"no route for {request.method} {request.url.path}"})
        return route(request) if callable(route) else httpx.Response(200, json=route)


def _deal_with_items() -> FakeHubSpot:
    items = {
        "11": {
            "name": "Platform",
            "hs_product_id": "900",
            "price": "1000",
            "quantity": "2",
            "amount": "2000",
            "hs_recurring_billing_period": "P12M",
            "hs_acv": "2000",
            "hs_tcv": "2000",
        },
        "12": {"name": "Services", "price": "500", "quantity": "1", "amount": "500", "hs_acv": "500", "hs_tcv": "500"},
    }

    def batch(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert "hs_acv" in body["properties"] and "price" in body["properties"]
        ids = [i["id"] for i in body["inputs"]]
        return httpx.Response(200, json={"results": [{"id": i, "properties": items[i]} for i in ids if i in items]})

    return FakeHubSpot(
        {
            ("GET", "/crm/v3/objects/deals/7"): {
                "id": "7",
                "properties": {"amount": "3000", "hs_acv": "2500", "hs_tcv": "2500", "deal_currency_code": "EUR"},
            },
            ("GET", "/crm/v4/objects/deals/7/associations/line_items"): {
                "results": [{"toObjectId": 11}, {"toObjectId": 12}]
            },
            ("POST", "/crm/v3/objects/line_items/batch/read"): batch,
        }
    )


# ---------------------------------------------------------------------------
# Line items
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_deal_lists_its_line_items_with_their_sum_next_to_its_amount():
    _defs, executor = _tools()
    with fake_api(_deal_with_items()):
        result = await call(executor, "list_hubspot_line_items", deal_id="7")
    text = result.content
    assert "Deal 7: amount 3000, ACV 2500, TCV 2500, currency EUR" in text
    assert "11 Platform | product 900 | price 1000 | quantity 2 | amount 2000 | term P12M | ACV 2000 | TCV 2000" in text
    assert "12 Services" in text
    assert "Sum of the line items: amount 2500, ACV 2500, TCV 2500" in text


@pytest.mark.asyncio
async def test_a_deal_without_line_items_says_so():
    api = _deal_with_items()
    api.routes[("GET", "/crm/v4/objects/deals/7/associations/line_items")] = {"results": []}
    _defs, executor = _tools()
    with fake_api(api) as sent:
        result = await call(executor, "list_hubspot_line_items", deal_id="7")
    assert "No line items." in result.content
    assert not any(r.url.path.endswith("/batch/read") for r in sent)


@pytest.mark.asyncio
async def test_a_line_item_is_updated_with_its_price_and_term():
    seen: list[dict] = []

    def patch(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "id": "11",
                "properties": {
                    "name": "Platform",
                    "price": "1500",
                    "hs_recurring_billing_period": "P12M",
                    "hs_acv": "3000",
                },
            },
        )

    _defs, executor = _tools()
    with fake_api(FakeHubSpot({("PATCH", "/crm/v3/objects/line_items/11"): patch})):
        result = await call(
            executor,
            "update_hubspot_line_item",
            line_item_id="11",
            properties_json='{"price": "1500", "hs_recurring_billing_period": "P12M"}',
        )
    assert seen == [{"properties": {"price": "1500", "hs_recurring_billing_period": "P12M"}}]
    assert "Line item 11 updated" in result.content and "ACV 3000" in result.content


@pytest.mark.asyncio
@pytest.mark.parametrize("properties_json", ["not json", "[1]", "{}"])
async def test_an_update_without_a_property_object_sends_nothing(properties_json):
    _defs, executor = _tools()
    with fake_api(FakeHubSpot({})) as sent:
        result = await call(executor, "update_hubspot_line_item", line_item_id="11", properties_json=properties_json)
    assert "Error" in result.content
    assert sent == []


@pytest.mark.asyncio
async def test_a_line_item_is_created_on_its_deal_from_a_name_and_a_price():
    seen: list[dict] = []

    def create(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(
            201, json={"id": "21", "properties": {"name": "Support", "price": "800", "quantity": "1"}}
        )

    _defs, executor = _tools()
    with fake_api(FakeHubSpot({("POST", "/crm/v3/objects/line_items"): create})):
        result = await call(
            executor,
            "create_hubspot_line_item",
            deal_id="7",
            name="Support",
            price="800",
            properties_json='{"hs_recurring_billing_period": "P12M"}',
        )
    assert seen == [
        {
            "properties": {"hs_recurring_billing_period": "P12M", "name": "Support", "price": "800", "quantity": "1"},
            "associations": [
                {"to": {"id": "7"}, "types": [{"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": 20}]}
            ],
        }
    ]
    assert "Created line item 21 on deal 7" in result.content


@pytest.mark.asyncio
async def test_a_line_item_needs_a_product_or_a_name_and_a_price():
    _defs, executor = _tools()
    with fake_api(FakeHubSpot({})) as sent:
        result = await call(executor, "create_hubspot_line_item", deal_id="7", name="Support")
    assert "pass product_id, or name and price" in result.content
    assert sent == []


@pytest.mark.asyncio
async def test_a_line_item_is_deleted():
    _defs, executor = _tools()
    with fake_api(FakeHubSpot({("DELETE", "/crm/v3/objects/line_items/11"): lambda r: httpx.Response(204)})) as sent:
        result = await call(executor, "delete_hubspot_line_item", line_item_id="11")
    assert [r.method for r in sent] == ["DELETE"]
    assert "Line item 11 deleted" in result.content


def test_the_sum_ignores_unset_values():
    items = [{"properties": {"amount": "10.5", "hs_acv": ""}}, {"properties": {"amount": "2"}}]
    assert line_items_sum(items) == "Sum of the line items: amount 12.5"
    assert format_line_item({"id": "1", "properties": {}}) == "  - 1 (no name)"


# ---------------------------------------------------------------------------
# Labelled associations
# ---------------------------------------------------------------------------


def test_a_label_is_found_by_name_whatever_its_case():
    assert association_label(LABELS["results"], " primary ") == {
        "associationCategory": "HUBSPOT_DEFINED",
        "associationTypeId": 5,
        "label": "Primary",
    }
    assert association_label(LABELS["results"], "reseller")["associationCategory"] == "USER_DEFINED"
    assert association_label(LABELS["results"], "Partner") is None
    assert (
        association_type_text({"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": 5})
        == "HUBSPOT_DEFINED type 5"
    )


@pytest.mark.asyncio
async def test_without_a_label_the_default_association_is_written_as_before():
    _defs, executor = _tools()
    with fake_api(FakeHubSpot({("PUT", "/crm/v4/objects/deals/7/associations/default/companies/42"): {}})) as sent:
        result = await call(
            executor, "create_hubspot_association", from_type="deals", from_id="7", to_type="companies", to_id="42"
        )
    assert [r.url.path for r in sent] == ["/crm/v4/objects/deals/7/associations/default/companies/42"]
    assert result.content == "Successfully associated deals 7 with companies 42."


@pytest.mark.asyncio
async def test_the_primary_label_moves_from_one_company_to_another():
    writes: list[tuple[str, str, object]] = []

    def record(request: httpx.Request) -> httpx.Response:
        writes.append((request.method, request.url.path, json.loads(request.content)))
        return httpx.Response(200, json={"results": []})

    api = FakeHubSpot(
        {
            ("GET", "/crm/v4/associations/deals/companies/labels"): LABELS,
            ("PUT", "/crm/v4/objects/deals/7/associations/companies/43"): record,
            ("POST", "/crm/v4/associations/deals/companies/batch/labels/archive"): record,
        }
    )
    _defs, executor = _tools()
    with fake_api(api):
        added = await call(
            executor,
            "create_hubspot_association",
            from_type="deals",
            from_id="7",
            to_type="companies",
            to_id="43",
            label="Primary",
        )
        removed = await call(
            executor,
            "delete_hubspot_association",
            from_type="deals",
            from_id="7",
            to_type="companies",
            to_id="42",
            label="Primary",
        )
    primary = {"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": 5}
    assert writes == [
        ("PUT", "/crm/v4/objects/deals/7/associations/companies/43", [primary]),
        (
            "POST",
            "/crm/v4/associations/deals/companies/batch/labels/archive",
            {"inputs": [{"from": {"id": "7"}, "to": {"id": "42"}, "types": [primary]}]},
        ),
    ]
    assert "the 'Primary' label (HUBSPOT_DEFINED type 5)" in added.content
    assert "stay associated by their other types" in removed.content


@pytest.mark.asyncio
async def test_a_type_id_needs_no_label_lookup():
    seen: list[object] = []

    def put(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={})

    _defs, executor = _tools()
    with fake_api(FakeHubSpot({("PUT", "/crm/v4/objects/deals/7/associations/companies/43"): put})) as sent:
        await call(
            executor,
            "create_hubspot_association",
            from_type="deals",
            from_id="7",
            to_type="companies",
            to_id="43",
            association_category="user_defined",
            association_type_id="17",
        )
    assert len(sent) == 1
    assert seen == [[{"associationCategory": "USER_DEFINED", "associationTypeId": 17}]]


@pytest.mark.asyncio
async def test_an_unknown_label_names_the_labels_defined_and_writes_nothing():
    _defs, executor = _tools()
    with fake_api(FakeHubSpot({("GET", "/crm/v4/associations/deals/companies/labels"): LABELS})) as sent:
        result = await call(
            executor,
            "create_hubspot_association",
            from_type="deals",
            from_id="7",
            to_type="companies",
            to_id="43",
            label="Partner",
        )
    assert "no association label 'Partner'" in result.content and "Primary, Reseller" in result.content
    assert [r.method for r in sent] == ["GET"]


@pytest.mark.asyncio
async def test_without_a_label_the_whole_association_is_removed():
    _defs, executor = _tools()
    with fake_api(
        FakeHubSpot({("DELETE", "/crm/v4/objects/deals/7/associations/companies/42"): lambda r: httpx.Response(204)})
    ) as sent:
        result = await call(
            executor, "delete_hubspot_association", from_type="deals", from_id="7", to_type="companies", to_id="42"
        )
    assert [r.method for r in sent] == ["DELETE"]
    assert "Removed every association" in result.content


# ---------------------------------------------------------------------------
# Gating: writes, scopes, Scope(s)
# ---------------------------------------------------------------------------


def test_the_new_writes_are_writes_and_the_list_is_a_read():
    assert infer_tool_type("list_hubspot_line_items") == "read"
    for name in (
        "create_hubspot_line_item",
        "update_hubspot_line_item",
        "delete_hubspot_line_item",
        "delete_hubspot_association",
    ):
        assert infer_tool_type(name) == "write", name


def test_every_new_tool_states_its_line_item_scopes():
    defs, _ = _tools()
    for name in (*LINE_ITEM_TOOLS, "delete_hubspot_association"):
        assert name in defs and name in TOOL_SCOPES, name
        assert "HubSpot private app scopes it uses" in defs[name].description, name
    assert "crm.objects.line_items.write" in defs["update_hubspot_line_item"].description
    assert "crm.objects.line_items.read" in defs["list_hubspot_line_items"].description


def test_line_items_go_with_the_deal_type_of_a_scope():
    assert all(TOOL_OBJECT_TYPE[name] == "deal" for name in LINE_ITEM_TOOLS)
    without_deals, _ = _tools(frozenset({"contact"}))
    assert not set(LINE_ITEM_TOOLS) & set(without_deals)
    with_deals, _ = _tools(frozenset({"deal"}))
    assert set(LINE_ITEM_TOOLS) <= set(with_deals)


@pytest.mark.asyncio
async def test_a_label_removal_outside_the_scope_is_refused_before_any_request():
    _defs, executor = _tools(frozenset({"deal"}))
    with fake_api(FakeHubSpot({})) as sent:
        result = await call(
            executor,
            "delete_hubspot_association",
            from_type="deals",
            from_id="7",
            to_type="companies",
            to_id="42",
            label="Primary",
        )
    assert result.is_error is True and "'companies' is outside" in result.content
    assert sent == []
