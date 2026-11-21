"""Tests for HubSpot deal properties parameter support."""

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.integrations.http import format_http_error
from app.integrations.providers.hubspot import (
    _DEAL_DEFAULT_KEYS,
    _DEAL_PROPS,
    _build_search_body,
    _normalize_filter,
    _redact_search_body,
    build_tools,
)

# ---------------------------------------------------------------------------
# Filter normalization (array value in a string slot → HTTP 400)
# ---------------------------------------------------------------------------


def test_normalize_filter_unwraps_single_element_array_for_scalar_operator():
    """``{"operator": "EQ", "value": ["closedwon"]}`` — the exact case —
    must become a scalar ``value`` so HubSpot doesn't 400 on a String slot."""
    out = _normalize_filter(
        {"propertyName": "dealstage", "operator": "EQ", "value": ["closedwon"]}
    )
    assert out == {
        "propertyName": "dealstage",
        "operator": "EQ",
        "value": "closedwon",
    }


def test_normalize_filter_upgrades_multi_value_scalar_operator_to_in():
    """A scalar operator given a multi-element array is invalid for HubSpot;
    the only schema-valid (and lossless) repair is ``IN`` + ``values`` — not
    silently dropping all but the first value."""
    out = _normalize_filter(
        {"propertyName": "dealstage", "operator": "EQ", "value": ["a", "b"]}
    )
    assert out == {
        "propertyName": "dealstage",
        "operator": "IN",
        "values": ["a", "b"],
    }


def test_normalize_filter_upgrades_multi_value_neq_to_not_in():
    """A multi-element array on a *negated* scalar operator (NEQ) must upgrade
    to NOT_IN, not IN — otherwise the filter polarity flips from "neither a nor
    b" to "either a or b"."""
    out = _normalize_filter(
        {"propertyName": "dealstage", "operator": "NEQ", "value": ["a", "b"]}
    )
    assert out == {
        "propertyName": "dealstage",
        "operator": "NOT_IN",
        "values": ["a", "b"],
    }


def test_normalize_filter_moves_value_to_values_for_in():
    """``IN`` with the list mistakenly under ``value`` must be remapped to
    ``values`` (and ``value`` dropped)."""
    out = _normalize_filter(
        {"propertyName": "pipeline", "operator": "IN", "value": ["x", "y"]}
    )
    assert out == {
        "propertyName": "pipeline",
        "operator": "IN",
        "values": ["x", "y"],
    }
    assert "value" not in out


def test_normalize_filter_in_with_scalar_wraps_in_values():
    out = _normalize_filter(
        {"propertyName": "pipeline", "operator": "IN", "value": "x"}
    )
    assert out == {"propertyName": "pipeline", "operator": "IN", "values": ["x"]}


def test_normalize_filter_stringifies_non_string_scalars():
    out = _normalize_filter(
        {"propertyName": "amount", "operator": "GTE", "value": 1000}
    )
    assert out == {"propertyName": "amount", "operator": "GTE", "value": "1000"}


def test_normalize_filter_between_keeps_low_and_high():
    out = _normalize_filter(
        {
            "propertyName": "amount",
            "operator": "BETWEEN",
            "value": [100],
            "highValue": [500],
        }
    )
    assert out == {
        "propertyName": "amount",
        "operator": "BETWEEN",
        "value": "100",
        "highValue": "500",
    }


def test_normalize_filter_sends_an_iso_date_range_bound_as_epoch_ms() -> None:
    """A custom date property refuses ``GTE 2026-11-18`` with a bare 400 and
    reads epoch milliseconds: every range bound written as an ISO date or
    time is sent as its epoch milliseconds (UTC when no offset is given)."""
    midnight = "1794960000000"  # 2026-11-18T00:00:00Z
    out = _normalize_filter({"propertyName": "services_end_date", "operator": "GTE", "value": "2026-11-18"})
    assert out == {"propertyName": "services_end_date", "operator": "GTE", "value": midnight}
    out = _normalize_filter({"propertyName": "createdate", "operator": "LT", "value": "2026-11-18T01:00:00+01:00"})
    assert out == {"propertyName": "createdate", "operator": "LT", "value": midnight}
    out = _normalize_filter(
        {
            "propertyName": "closedate",
            "operator": "BETWEEN",
            "value": "2026-11-18",
            "highValue": "2026-11-18T00:00:01Z",
        }
    )
    assert out == {
        "propertyName": "closedate",
        "operator": "BETWEEN",
        "value": midnight,
        "highValue": "1794960001000",
    }


def test_normalize_filter_keeps_non_date_and_equality_values() -> None:
    """Numbers, epoch values already given, and an equality on a date stay as written."""
    for flt, value in (
        ({"propertyName": "amount", "operator": "GT", "value": "2026"}, "2026"),
        ({"propertyName": "closedate", "operator": "GTE", "value": "1794960000000"}, "1794960000000"),
        ({"propertyName": "closedate", "operator": "EQ", "value": "2026-11-18"}, "2026-11-18"),
        ({"propertyName": "closedate", "operator": "GTE", "value": "2026-13-40"}, "2026-13-40"),
    ):
        out = _normalize_filter(flt)
        assert out is not None and out["value"] == value


def test_normalize_filter_has_property_drops_stray_value():
    out = _normalize_filter(
        {"propertyName": "email", "operator": "HAS_PROPERTY", "value": ["junk"]}
    )
    assert out == {"propertyName": "email", "operator": "HAS_PROPERTY"}


def test_normalize_filter_drops_filter_without_property_name():
    assert _normalize_filter({"operator": "EQ", "value": "x"}) is None
    assert _normalize_filter("not-a-dict") is None


def test_normalize_filter_drops_in_with_no_usable_values():
    """``IN``/``NOT_IN`` require at least one value — an empty/uncoercible list
    still 400s, so the filter is dropped rather than emitted as
    ``{"values": []}``."""
    assert (
        _normalize_filter({"propertyName": "p", "operator": "IN", "values": []})
        is None
    )
    assert (
        _normalize_filter({"propertyName": "p", "operator": "NOT_IN", "value": [{}]})
        is None
    )


def test_normalize_filter_drops_between_missing_endpoint():
    """``BETWEEN`` needs both endpoints; a half-open range still 400s, so drop
    it when either side can't be coerced."""
    assert (
        _normalize_filter({"propertyName": "amount", "operator": "BETWEEN", "value": 100})
        is None
    )
    assert (
        _normalize_filter(
            {
                "propertyName": "amount",
                "operator": "BETWEEN",
                "value": 100,
                "highValue": {},
            }
        )
        is None
    )


def test_normalize_filter_drops_scalar_with_uncoercible_value():
    """A scalar operator with no coercible value (empty list, object, or
    missing) is dropped — HubSpot requires a ``value`` for these operators."""
    assert (
        _normalize_filter({"propertyName": "p", "operator": "EQ", "value": []}) is None
    )
    assert (
        _normalize_filter({"propertyName": "p", "operator": "GT", "value": {}}) is None
    )
    assert _normalize_filter({"propertyName": "p", "operator": "EQ"}) is None


def test_normalize_filter_drops_scalar_multi_value_upgrade_when_all_uncoercible():
    """A multi-element scalar-operator array that upgrades to ``IN`` is dropped
    if none of the items coerce (would otherwise emit ``{"values": []}``)."""
    assert (
        _normalize_filter(
            {"propertyName": "p", "operator": "EQ", "value": [{}, []]}
        )
        is None
    )


def test_build_search_body_normalizes_array_filter_value():
    """End-to-end through ``_build_search_body``: an array ``value`` in
    ``filters_json`` is repaired to a scalar in the emitted filterGroups."""
    import json as _json

    body = _build_search_body(
        properties=_DEAL_PROPS.split(","),
        filters_json=_json.dumps(
            [{"propertyName": "dealstage", "operator": "EQ", "value": ["closedwon"]}]
        ),
    )
    flt = body["filterGroups"][0]["filters"][0]
    assert flt["value"] == "closedwon"
    assert isinstance(flt["value"], str)


def test_build_search_body_accepts_already_parsed_filters():
    """Some runtimes hand ``filters_json`` through already parsed (a list, not
    a JSON string); it must not crash and still normalize."""
    body = _build_search_body(
        properties=_DEAL_PROPS.split(","),
        filters_json=[
            {"propertyName": "dealstage", "operator": "IN", "value": ["a", "b"]}
        ],
    )
    flt = body["filterGroups"][0]["filters"][0]
    assert flt["operator"] == "IN"
    assert flt["values"] == ["a", "b"]
    assert "value" not in flt

# ---------------------------------------------------------------------------
# _build_search_body tests
# ---------------------------------------------------------------------------

def test_build_search_body_uses_custom_properties():
    """_build_search_body should pass through custom properties."""
    body = _build_search_body(
        properties=["dealname", "partner_sourced", "partner"],
        query="",
        filters_json="",
        sort_by="",
        sort_direction="DESCENDING",
        count=10,
        after="",
    )
    assert body["properties"] == ["dealname", "partner_sourced", "partner"]


def test_build_search_body_uses_default_properties():
    """_build_search_body should use defaults when given the standard list."""
    body = _build_search_body(
        properties=_DEAL_PROPS.split(","),
        query="",
        filters_json="",
        sort_by="",
        sort_direction="DESCENDING",
        count=10,
        after="",
    )
    assert body["properties"] == _DEAL_PROPS.split(",")


# ---------------------------------------------------------------------------
# Tool schema tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_search_hubspot_deals_custom_properties():
    """search_hubspot_deals tool definition should include properties parameter."""
    mock_executor = MagicMock()
    mock_executor.register = MagicMock()
    tool_defs = build_tools("fake-token", mock_executor)
    search_tool = next(t for t in tool_defs if t.name == "search_hubspot_deals")
    assert "properties" in search_tool.input_schema["properties"]
    assert search_tool.input_schema["properties"]["properties"]["type"] == "string"


@pytest.mark.asyncio
async def test_get_hubspot_deal_custom_properties():
    """get_hubspot_deal tool definition should include properties parameter."""
    mock_executor = MagicMock()
    mock_executor.register = MagicMock()
    tool_defs = build_tools("fake-token", mock_executor)
    get_tool = next(t for t in tool_defs if t.name == "get_hubspot_deal")
    assert "properties" in get_tool.input_schema["properties"]
    assert get_tool.input_schema["properties"]["properties"]["type"] == "string"


# ---------------------------------------------------------------------------
# _DEAL_DEFAULT_KEYS tests
# ---------------------------------------------------------------------------

def test_deal_default_keys_includes_hs_object_id():
    """_DEAL_DEFAULT_KEYS should include hs_object_id (always returned by HubSpot API)."""
    assert "hs_object_id" in _DEAL_DEFAULT_KEYS
    for prop in _DEAL_PROPS.split(","):
        assert prop in _DEAL_DEFAULT_KEYS


def test_deal_default_keys_does_not_include_custom_props():
    """Custom properties like partner_sourced should not be in _DEAL_DEFAULT_KEYS."""
    assert "partner_sourced" not in _DEAL_DEFAULT_KEYS
    assert "partner" not in _DEAL_DEFAULT_KEYS


def test_build_search_body_whitespace_only_properties():
    """_build_search_body should handle whitespace in property names by stripping."""
    body = _build_search_body(
        properties=["  dealname  ", " partner "],
        query="",
        filters_json="",
        sort_by="",
        sort_direction="DESCENDING",
        count=10,
        after="",
    )
    # Properties are passed as-is to _build_search_body; stripping is the caller's job
    assert len(body["properties"]) == 2


# ---------------------------------------------------------------------------
# Helpers for mocking HTTP responses
# ---------------------------------------------------------------------------

def _mock_response(payload: dict) -> httpx.Response:
    """Build a fake httpx.Response with the given JSON payload."""
    return httpx.Response(200, json=payload)


def _get_registered_fn(executor_mock: MagicMock, name: str):
    """Extract a registered async function from the executor mock by tool name."""
    for call in executor_mock.register.call_args_list:
        if call[0][0] == name:
            return call[0][1]
    raise KeyError(f"Tool {name!r} not registered")


# ---------------------------------------------------------------------------
# Formatting tests — get_hubspot_deal
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_hubspot_deal_extra_props_in_output():
    """Extra properties should appear in formatted get_hubspot_deal output."""
    mock_executor = MagicMock()
    mock_executor.register = MagicMock()
    build_tools("fake-token", mock_executor)
    get_deal_fn = _get_registered_fn(mock_executor, "get_hubspot_deal")

    fake_resp = _mock_response({
        "id": "999",
        "properties": {
            "dealname": "Big Deal",
            "amount": "50000",
            "dealstage": "closedwon",
            "pipeline": "default",
            "closedate": "2026-03-15T00:00:00.000Z",
            "createdate": "2026-01-01T00:00:00.000Z",
            "hs_lastmodifieddate": "2026-03-15T12:00:00.000Z",
            "hubspot_owner_id": "42",
            "hs_object_id": "999",
            "partner_sourced": "yes",
            "partner": "Acme Corp",
        },
    })

    with patch("app.integrations.providers.hubspot.safe_request", new_callable=AsyncMock) as mock_req:
        mock_req.return_value = (fake_resp, None)
        result = await get_deal_fn(deal_id="999", properties="partner_sourced,partner")

    # Core fields are present
    assert "Big Deal" in result
    assert "$50,000" in result
    assert "closedwon" in result
    # Extra properties are appended
    assert "partner_sourced: yes" in result
    assert "partner: Acme Corp" in result
    # Default keys like hs_object_id should NOT appear in extras section
    assert "hs_object_id:" not in result


@pytest.mark.asyncio
async def test_get_hubspot_deal_additive_merge():
    """Custom properties should be additive — defaults always fetched."""
    mock_executor = MagicMock()
    mock_executor.register = MagicMock()
    build_tools("fake-token", mock_executor)
    get_deal_fn = _get_registered_fn(mock_executor, "get_hubspot_deal")

    with patch("app.integrations.providers.hubspot.safe_request", new_callable=AsyncMock) as mock_req:
        mock_req.return_value = (_mock_response({"id": "1", "properties": {}}), None)
        await get_deal_fn(deal_id="1", properties="partner_sourced")

    # Verify the requested properties include both defaults and custom
    call_kwargs = mock_req.call_args
    requested = call_kwargs.kwargs.get("params", {}).get("properties", "")
    requested_set = set(requested.split(","))
    # All defaults must be present
    for prop in _DEAL_PROPS.split(","):
        assert prop in requested_set
    # Custom prop must also be present
    assert "partner_sourced" in requested_set


@pytest.mark.asyncio
async def test_get_hubspot_deal_no_properties_uses_defaults():
    """When properties is empty, only default props are requested."""
    mock_executor = MagicMock()
    mock_executor.register = MagicMock()
    build_tools("fake-token", mock_executor)
    get_deal_fn = _get_registered_fn(mock_executor, "get_hubspot_deal")

    with patch("app.integrations.providers.hubspot.safe_request", new_callable=AsyncMock) as mock_req:
        mock_req.return_value = (_mock_response({"id": "1", "properties": {}}), None)
        await get_deal_fn(deal_id="1")

    call_kwargs = mock_req.call_args
    requested = call_kwargs.kwargs.get("params", {}).get("properties", "")
    assert requested == _DEAL_PROPS


@pytest.mark.asyncio
async def test_get_hubspot_deal_whitespace_and_duplicates():
    """Whitespace and duplicate properties should be handled gracefully."""
    mock_executor = MagicMock()
    mock_executor.register = MagicMock()
    build_tools("fake-token", mock_executor)
    get_deal_fn = _get_registered_fn(mock_executor, "get_hubspot_deal")

    with patch("app.integrations.providers.hubspot.safe_request", new_callable=AsyncMock) as mock_req:
        mock_req.return_value = (_mock_response({"id": "1", "properties": {}}), None)
        # Spaces and a dup of a default prop
        await get_deal_fn(deal_id="1", properties=" partner , dealname , partner ")

    call_kwargs = mock_req.call_args
    requested = call_kwargs.kwargs.get("params", {}).get("properties", "")
    requested_set = set(requested.split(","))
    # partner is included, dealname is not duplicated (it's a set)
    assert "partner" in requested_set
    assert "dealname" in requested_set
    # All defaults still present
    for prop in _DEAL_PROPS.split(","):
        assert prop in requested_set


# ---------------------------------------------------------------------------
# Formatting tests — search_hubspot_deals
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_search_hubspot_deals_extra_props_in_output():
    """Extra properties should appear in formatted search_hubspot_deals output."""
    mock_executor = MagicMock()
    mock_executor.register = MagicMock()
    build_tools("fake-token", mock_executor)
    search_fn = _get_registered_fn(mock_executor, "search_hubspot_deals")

    fake_resp = _mock_response({
        "total": 1,
        "results": [
            {
                "id": "123",
                "properties": {
                    "dealname": "Partner Deal",
                    "amount": "10000",
                    "dealstage": "presentationscheduled",
                    "pipeline": "default",
                    "closedate": "2026-06-01T00:00:00.000Z",
                    "createdate": "2026-05-01T00:00:00.000Z",
                    "hs_object_id": "123",
                    "partner_sourced": "true",
                    "partner_manager": "Jane Doe",
                },
            }
        ],
        "paging": {},
    })

    with patch("app.integrations.providers.hubspot.safe_request", new_callable=AsyncMock) as mock_req:
        mock_req.return_value = (fake_resp, None)
        result = (await search_fn(properties="partner_sourced,partner_manager")).content

    # Core formatted fields
    assert "Partner Deal" in result
    assert "$10,000" in result
    # Extra properties appended
    assert "partner_sourced: true" in result
    assert "partner_manager: Jane Doe" in result


@pytest.mark.asyncio
async def test_search_hubspot_deals_additive_merge():
    """search_hubspot_deals custom properties should be additive to defaults."""
    mock_executor = MagicMock()
    mock_executor.register = MagicMock()
    build_tools("fake-token", mock_executor)
    search_fn = _get_registered_fn(mock_executor, "search_hubspot_deals")

    with patch("app.integrations.providers.hubspot.safe_request", new_callable=AsyncMock) as mock_req:
        mock_req.return_value = (_mock_response({"total": 0, "results": [], "paging": {}}), None)
        await search_fn(properties="partner_sourced")

    call_kwargs = mock_req.call_args
    body = call_kwargs.kwargs.get("json", {})
    requested_set = set(body.get("properties", []))
    for prop in _DEAL_PROPS.split(","):
        assert prop in requested_set
    assert "partner_sourced" in requested_set


@pytest.mark.asyncio
async def test_search_hubspot_deals_falsy_values_shown():
    """Falsy values (0, False) in extra properties should still appear in output."""
    mock_executor = MagicMock()
    mock_executor.register = MagicMock()
    build_tools("fake-token", mock_executor)
    search_fn = _get_registered_fn(mock_executor, "search_hubspot_deals")

    fake_resp = _mock_response({
        "total": 1,
        "results": [
            {
                "id": "456",
                "properties": {
                    "dealname": "Falsy Test",
                    "amount": "0",
                    "dealstage": "qualifiedtobuy",
                    "pipeline": "default",
                    "closedate": "2026-06-01T00:00:00.000Z",
                    "createdate": "2026-05-01T00:00:00.000Z",
                    "hs_object_id": "456",
                    "custom_bool": "false",
                    "custom_zero": "0",
                },
            }
        ],
        "paging": {},
    })

    with patch("app.integrations.providers.hubspot.safe_request", new_callable=AsyncMock) as mock_req:
        mock_req.return_value = (fake_resp, None)
        result = (await search_fn(properties="custom_bool,custom_zero")).content

    # Falsy values should NOT be filtered out
    assert "custom_bool: false" in result
    assert "custom_zero: 0" in result


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tool", ["search_hubspot_contacts", "search_hubspot_companies", "search_hubspot_deals", "search_hubspot_tickets"]
)
async def test_an_empty_search_is_an_empty_structured_list_a_gate_check_reads_as_empty(tool):
    """A gate check tests the structured value: nothing found is ``[]``, never the non-empty text."""
    from app.services.assignment_activation_check import condition_met, structured_result

    mock_executor = MagicMock()
    mock_executor.register = MagicMock()
    build_tools("fake-token", mock_executor)
    search_fn = _get_registered_fn(mock_executor, tool)

    with patch("app.integrations.providers.hubspot.safe_request", new_callable=AsyncMock) as mock_req:
        mock_req.return_value = (_mock_response({"total": 0, "results": [], "paging": {}}), None)
        result = await search_fn()

    data, readable = structured_result(result)
    assert readable and data == []
    assert result.content  # the model still reads a sentence
    assert not condition_met(data, "non_empty")
    assert condition_met(data, "empty")


@pytest.mark.asyncio
async def test_a_ticket_search_lists_its_records_as_the_structured_value():
    mock_executor = MagicMock()
    mock_executor.register = MagicMock()
    build_tools("fake-token", mock_executor)
    search_fn = _get_registered_fn(mock_executor, "search_hubspot_tickets")
    ticket = {"id": "77", "properties": {"subject": "Access request", "hs_pipeline_stage": "1"}}

    with patch("app.integrations.providers.hubspot.safe_request", new_callable=AsyncMock) as mock_req:
        mock_req.return_value = (_mock_response({"total": 1, "results": [ticket], "paging": {}}), None)
        result = await search_fn(raw_values=True)

    assert result.structured == [{"id": "77", "properties": {"subject": "Access request", "hs_pipeline_stage": "1"}}]
    assert "Access request" in result.content


# ---------------------------------------------------------------------------
# get_hubspot_owner
# ---------------------------------------------------------------------------


def test_get_hubspot_owner_registered_and_exposed():
    mock_executor = MagicMock()
    mock_executor.register = MagicMock()
    tool_defs = build_tools("fake-token", mock_executor)

    registered = {c[0][0] for c in mock_executor.register.call_args_list}
    assert "get_hubspot_owner" in registered
    assert "get_hubspot_owner" in {t.name for t in tool_defs}


@pytest.mark.asyncio
async def test_get_hubspot_owner_returns_detail():
    mock_executor = MagicMock()
    mock_executor.register = MagicMock()
    build_tools("fake-token", mock_executor)
    fn = _get_registered_fn(mock_executor, "get_hubspot_owner")

    payload = {
        "id": "99",
        "firstName": "Dana",
        "lastName": "Owner",
        "email": "dana@example.com",
        "userId": 4242,
        "teams": [{"id": "1", "name": "Sales EMEA"}],
        "archived": False,
        "createdAt": "2025-01-02T10:00:00Z",
        "updatedAt": "2026-05-01T12:00:00Z",
    }
    with patch(
        "app.integrations.providers.hubspot.safe_request", new_callable=AsyncMock
    ) as mock_req:
        mock_req.return_value = (_mock_response(payload), None)
        result = await fn(owner_id="99")

    assert "Owner ID: 99" in result
    assert "Dana Owner" in result
    assert "dana@example.com" in result
    assert "User ID: 4242" in result
    assert "Teams: Sales EMEA" in result
    assert "Created: 2025-01-02" in result
    # Targets the single-owner endpoint, not the list endpoint.
    assert mock_req.call_args.args[2].endswith("/crm/v3/owners/99")


@pytest.mark.asyncio
async def test_get_hubspot_owner_surfaces_error():
    mock_executor = MagicMock()
    mock_executor.register = MagicMock()
    build_tools("fake-token", mock_executor)
    fn = _get_registered_fn(mock_executor, "get_hubspot_owner")

    with patch(
        "app.integrations.providers.hubspot.safe_request", new_callable=AsyncMock
    ) as mock_req:
        mock_req.return_value = (None, "Error: HubSpot resource not found.")
        result = await fn(owner_id="99999999")

    assert result == "Error: HubSpot resource not found."


# ---------------------------------------------------------------------------
# search_hubspot_owners
# ---------------------------------------------------------------------------


def test_search_hubspot_owners_registered_and_exposed():
    mock_executor = MagicMock()
    mock_executor.register = MagicMock()
    tool_defs = build_tools("fake-token", mock_executor)

    registered = {c[0][0] for c in mock_executor.register.call_args_list}
    assert "search_hubspot_owners" in registered

    tool = next(t for t in tool_defs if t.name == "search_hubspot_owners")
    # Unlike the Copilot alias, this exposes a real query contract.
    assert "query" in tool.input_schema["properties"]
    # Page size is named ``count`` to match every other HubSpot tool (the
    # executor drops undeclared kwargs, so a model that learned ``count`` from
    # the siblings must not have it silently dropped here).
    assert "count" in tool.input_schema["properties"]
    assert "limit" not in tool.input_schema["properties"]
    count_schema = tool.input_schema["properties"]["count"]
    assert count_schema["default"] == 50
    assert count_schema["minimum"] == 1
    assert count_schema["maximum"] == 100
    assert tool.input_schema["properties"]["query"]["default"] == ""


@pytest.mark.asyncio
async def test_search_hubspot_owners_filters_by_name_client_side():
    mock_executor = MagicMock()
    mock_executor.register = MagicMock()
    build_tools("fake-token", mock_executor)
    fn = _get_registered_fn(mock_executor, "search_hubspot_owners")

    owners = {
        "results": [
            {"id": "1", "firstName": "Ada", "lastName": "Lovelace", "email": "ada@x.io"},
            {"id": "2", "firstName": "Alan", "lastName": "Turing", "email": "alan@x.io"},
            {"id": "3", "firstName": "Grace", "lastName": "Hopper", "email": "grace@x.io"},
        ]
    }
    with patch(
        "app.integrations.providers.hubspot.safe_request", new_callable=AsyncMock
    ) as mock_req:
        mock_req.return_value = (_mock_response(owners), None)
        result = await fn(query="grace")

    assert "Grace Hopper" in result
    assert "Ada Lovelace" not in result
    assert "Alan Turing" not in result
    # Name search uses the list endpoint (no /search endpoint exists for owners).
    assert mock_req.call_args.args[2].endswith("/crm/v3/owners")


@pytest.mark.asyncio
async def test_search_hubspot_owners_email_uses_exact_filter():
    mock_executor = MagicMock()
    mock_executor.register = MagicMock()
    build_tools("fake-token", mock_executor)
    fn = _get_registered_fn(mock_executor, "search_hubspot_owners")

    with patch(
        "app.integrations.providers.hubspot.safe_request", new_callable=AsyncMock
    ) as mock_req:
        mock_req.return_value = (
            _mock_response(
                {"results": [{"id": "7", "firstName": "Ada", "email": "ada@x.io"}]}
            ),
            None,
        )
        result = await fn(query="ada@x.io")

    assert "ID: 7" in result
    # A full email is pushed to the owners API's exact ``email`` filter.
    assert mock_req.call_args.kwargs["params"]["email"] == "ada@x.io"


@pytest.mark.asyncio
async def test_search_hubspot_owners_no_match_message():
    mock_executor = MagicMock()
    mock_executor.register = MagicMock()
    build_tools("fake-token", mock_executor)
    fn = _get_registered_fn(mock_executor, "search_hubspot_owners")

    with patch(
        "app.integrations.providers.hubspot.safe_request", new_callable=AsyncMock
    ) as mock_req:
        mock_req.return_value = (
            _mock_response(
                {"results": [{"id": "1", "firstName": "Ada", "email": "ada@x.io"}]}
            ),
            None,
        )
        result = await fn(query="zzz-nobody")

    assert "No HubSpot owners found matching 'zzz-nobody'." in result


@pytest.mark.asyncio
async def test_search_hubspot_owners_empty_query_lists():
    mock_executor = MagicMock()
    mock_executor.register = MagicMock()
    build_tools("fake-token", mock_executor)
    fn = _get_registered_fn(mock_executor, "search_hubspot_owners")

    with patch(
        "app.integrations.providers.hubspot.safe_request", new_callable=AsyncMock
    ) as mock_req:
        mock_req.return_value = (
            _mock_response(
                {
                    "results": [
                        {"id": "1", "firstName": "Ada", "email": "ada@x.io"},
                        {"id": "2", "firstName": "Alan", "email": "alan@x.io"},
                    ]
                }
            ),
            None,
        )
        result = await fn(query="")

    assert "Ada" in result
    assert "Alan" in result


@pytest.mark.asyncio
async def test_search_hubspot_owners_count_caps_results():
    """``count`` (the sibling-consistent page-size arg) must actually bound the
    returned owners — a regression to a dropped/ignored arg would return all."""
    mock_executor = MagicMock()
    mock_executor.register = MagicMock()
    build_tools("fake-token", mock_executor)
    fn = _get_registered_fn(mock_executor, "search_hubspot_owners")

    with patch(
        "app.integrations.providers.hubspot.safe_request", new_callable=AsyncMock
    ) as mock_req:
        mock_req.return_value = (
            _mock_response(
                {
                    "results": [
                        {"id": "1", "firstName": "Ada", "email": "ada@x.io"},
                        {"id": "2", "firstName": "Alan", "email": "alan@x.io"},
                        {"id": "3", "firstName": "Grace", "email": "grace@x.io"},
                    ]
                }
            ),
            None,
        )
        result = await fn(query="", count=1)

    assert "Ada" in result
    assert "Alan" not in result
    assert "Grace" not in result


# ---------------------------------------------------------------------------
# Ticket write tools — create_hubspot_ticket / update_hubspot_ticket
# ---------------------------------------------------------------------------


def test_ticket_write_tools_registered_and_exposed():
    """Tickets must have full create/update coverage like every other CRM
    object — read-only tickets were the gap."""
    mock_executor = MagicMock()
    mock_executor.register = MagicMock()
    tool_defs = build_tools("fake-token", mock_executor)

    registered = {c[0][0] for c in mock_executor.register.call_args_list}
    exposed = {t.name for t in tool_defs}
    for name in ("create_hubspot_ticket", "update_hubspot_ticket"):
        assert name in registered, f"{name} not registered"
        assert name in exposed, f"{name} not exposed as a tool"


def test_update_hubspot_ticket_schema_requires_id_and_properties():
    mock_executor = MagicMock()
    mock_executor.register = MagicMock()
    tool_defs = build_tools("fake-token", mock_executor)
    tool = next(t for t in tool_defs if t.name == "update_hubspot_ticket")
    props = tool.input_schema["properties"]
    assert "ticket_id" in props
    assert "properties_json" in props
    assert set(tool.input_schema["required"]) == {"ticket_id", "properties_json"}


def test_create_hubspot_ticket_schema_requires_subject_only():
    mock_executor = MagicMock()
    mock_executor.register = MagicMock()
    tool_defs = build_tools("fake-token", mock_executor)
    tool = next(t for t in tool_defs if t.name == "create_hubspot_ticket")
    assert tool.input_schema["required"] == ["subject"]


@pytest.mark.asyncio
async def test_update_hubspot_ticket_patches_properties():
    """update_hubspot_ticket must PATCH the ticket object with a properties
    envelope so a stage change / internal reason field is actually written."""
    mock_executor = MagicMock()
    mock_executor.register = MagicMock()
    build_tools("fake-token", mock_executor)
    fn = _get_registered_fn(mock_executor, "update_hubspot_ticket")

    with patch(
        "app.integrations.providers.hubspot.safe_request", new_callable=AsyncMock
    ) as mock_req:
        mock_req.return_value = (_mock_response({"id": "555"}), None)
        result = await fn(
            ticket_id="555",
            properties_json='{"hs_pipeline_stage": "4", "reason": "approved"}',
        )

    assert "555" in result
    # Targets the single-ticket PATCH endpoint with a properties envelope.
    assert mock_req.call_args.args[1] == "patch"
    assert mock_req.call_args.args[2].endswith("/crm/v3/objects/tickets/555")
    assert mock_req.call_args.kwargs["json"] == {
        "properties": {"hs_pipeline_stage": "4", "reason": "approved"}
    }


@pytest.mark.asyncio
async def test_update_hubspot_ticket_accepts_already_parsed_properties():
    """Some runtimes hand properties_json through already parsed (a dict, not a
    JSON string); it must not crash and still PATCH the same envelope."""
    mock_executor = MagicMock()
    mock_executor.register = MagicMock()
    build_tools("fake-token", mock_executor)
    fn = _get_registered_fn(mock_executor, "update_hubspot_ticket")

    with patch(
        "app.integrations.providers.hubspot.safe_request", new_callable=AsyncMock
    ) as mock_req:
        mock_req.return_value = (_mock_response({"id": "9"}), None)
        result = await fn(ticket_id="9", properties_json={"hs_ticket_priority": "HIGH"})

    assert "9" in result
    assert mock_req.call_args.kwargs["json"] == {
        "properties": {"hs_ticket_priority": "HIGH"}
    }


@pytest.mark.asyncio
async def test_update_hubspot_ticket_rejects_invalid_json():
    mock_executor = MagicMock()
    mock_executor.register = MagicMock()
    build_tools("fake-token", mock_executor)
    fn = _get_registered_fn(mock_executor, "update_hubspot_ticket")

    with patch(
        "app.integrations.providers.hubspot.safe_request", new_callable=AsyncMock
    ) as mock_req:
        result = await fn(ticket_id="1", properties_json="not-json")

    assert "Error" in result
    # Must not have attempted the HTTP call on invalid input.
    mock_req.assert_not_called()


@pytest.mark.asyncio
async def test_update_hubspot_ticket_surfaces_error():
    mock_executor = MagicMock()
    mock_executor.register = MagicMock()
    build_tools("fake-token", mock_executor)
    fn = _get_registered_fn(mock_executor, "update_hubspot_ticket")

    with patch(
        "app.integrations.providers.hubspot.safe_request", new_callable=AsyncMock
    ) as mock_req:
        mock_req.return_value = (None, "Error: HubSpot resource not found.")
        result = await fn(ticket_id="nope", properties_json='{"x": "y"}')

    assert result == "Error: HubSpot resource not found."


@pytest.mark.asyncio
async def test_create_hubspot_ticket_builds_properties_and_merges_custom():
    """create_hubspot_ticket must POST a properties envelope; named fields are
    included only when set, and properties_json is merged on top for custom
    properties."""
    mock_executor = MagicMock()
    mock_executor.register = MagicMock()
    build_tools("fake-token", mock_executor)
    fn = _get_registered_fn(mock_executor, "create_hubspot_ticket")

    with patch(
        "app.integrations.providers.hubspot.safe_request", new_callable=AsyncMock
    ) as mock_req:
        mock_req.return_value = (_mock_response({"id": "777"}), None)
        result = await fn(
            subject="Cannot log in",
            hs_pipeline_stage="1",
            hs_ticket_priority="HIGH",
            properties_json='{"hs_ticket_category": "billing"}',
        )

    assert "777" in result
    assert mock_req.call_args.args[1] == "post"
    assert mock_req.call_args.args[2].endswith("/crm/v3/objects/tickets")
    sent = mock_req.call_args.kwargs["json"]["properties"]
    assert sent["subject"] == "Cannot log in"
    assert sent["hs_pipeline_stage"] == "1"
    assert sent["hs_ticket_priority"] == "HIGH"
    assert sent["hs_ticket_category"] == "billing"
    # Unset optional fields are omitted, not sent as empty strings.
    assert "content" not in sent
    assert "hs_pipeline" not in sent


@pytest.mark.asyncio
async def test_create_hubspot_ticket_rejects_invalid_properties_json():
    mock_executor = MagicMock()
    mock_executor.register = MagicMock()
    build_tools("fake-token", mock_executor)
    fn = _get_registered_fn(mock_executor, "create_hubspot_ticket")

    with patch(
        "app.integrations.providers.hubspot.safe_request", new_callable=AsyncMock
    ) as mock_req:
        result = await fn(subject="x", properties_json="[1, 2, 3]")

    assert "Error" in result
    mock_req.assert_not_called()


# ---------------------------------------------------------------------------
# get_hubspot_owner — non-numeric ID guard
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_hubspot_owner_rejects_non_numeric_id():
    """A MongoDB-style ObjectId (hex string) must be rejected before the HTTP
    call is made — HubSpot returns HTTP 400 for non-numeric ownerId path
    parameters."""
    mock_executor = MagicMock()
    mock_executor.register = MagicMock()
    build_tools("fake-token", mock_executor)
    fn = _get_registered_fn(mock_executor, "get_hubspot_owner")

    with patch(
        "app.integrations.providers.hubspot.safe_request", new_callable=AsyncMock
    ) as mock_req:
        result = await fn(owner_id="6968ac502e2dbdb5d627edb1")

    assert "Error" in result
    assert "not a valid HubSpot owner ID" in result
    assert "numeric" in result.lower()
    mock_req.assert_not_called()


@pytest.mark.asyncio
async def test_get_hubspot_owner_rejects_alpha_string():
    """An alphabetic string (e.g. 'john.doe') must also be rejected early."""
    mock_executor = MagicMock()
    mock_executor.register = MagicMock()
    build_tools("fake-token", mock_executor)
    fn = _get_registered_fn(mock_executor, "get_hubspot_owner")

    with patch(
        "app.integrations.providers.hubspot.safe_request", new_callable=AsyncMock
    ) as mock_req:
        result = await fn(owner_id="john.doe")

    assert "Error" in result
    assert "not a valid HubSpot owner ID" in result
    mock_req.assert_not_called()


@pytest.mark.asyncio
async def test_get_hubspot_owner_accepts_numeric_id():
    """A valid numeric owner ID must pass through to the HTTP call."""
    mock_executor = MagicMock()
    mock_executor.register = MagicMock()
    build_tools("fake-token", mock_executor)
    fn = _get_registered_fn(mock_executor, "get_hubspot_owner")

    fake_resp = _mock_response(
        {
            "id": "12345678",
            "firstName": "Jane",
            "lastName": "Doe",
            "email": "jane@example.com",
        }
    )

    with patch(
        "app.integrations.providers.hubspot.safe_request",
        new_callable=AsyncMock,
    ) as mock_req:
        mock_req.return_value = (fake_resp, None)
        result = await fn(owner_id="12345678")

    mock_req.assert_called_once()
    assert "Jane Doe" in result
    assert "jane@example.com" in result


@pytest.mark.asyncio
async def test_get_hubspot_owner_strips_whitespace_before_call():
    """A whitespace-padded numeric ID must pass the guard AND hit the API with
    the stripped value — otherwise the URL contains an encoded space and
    HubSpot 400s, the exact failure mode the guard exists to prevent."""
    mock_executor = MagicMock()
    mock_executor.register = MagicMock()
    build_tools("fake-token", mock_executor)
    fn = _get_registered_fn(mock_executor, "get_hubspot_owner")

    fake_resp = _mock_response({"id": "12345678", "firstName": "Jane"})

    with patch(
        "app.integrations.providers.hubspot.safe_request", new_callable=AsyncMock
    ) as mock_req:
        mock_req.return_value = (fake_resp, None)
        await fn(owner_id=" 12345678 ")

    mock_req.assert_called_once()
    assert mock_req.call_args.args[2].endswith("/crm/v3/owners/12345678")


@pytest.mark.asyncio
async def test_get_hubspot_owner_accepts_integer_input():
    """Some runtimes hand numeric-looking args through as integers; the guard
    must coerce instead of crashing on ``int.strip``."""
    mock_executor = MagicMock()
    mock_executor.register = MagicMock()
    build_tools("fake-token", mock_executor)
    fn = _get_registered_fn(mock_executor, "get_hubspot_owner")

    fake_resp = _mock_response({"id": "12345678", "firstName": "Jane"})

    with patch(
        "app.integrations.providers.hubspot.safe_request", new_callable=AsyncMock
    ) as mock_req:
        mock_req.return_value = (fake_resp, None)
        await fn(owner_id=12345678)

    mock_req.assert_called_once()
    assert mock_req.call_args.args[2].endswith("/crm/v3/owners/12345678")


@pytest.mark.asyncio
async def test_get_hubspot_owner_rejects_non_ascii_digits():
    """Non-ASCII digits pass ``str.isdigit()`` but HubSpot still 400s on them,
    so the guard must reject them too."""
    mock_executor = MagicMock()
    mock_executor.register = MagicMock()
    build_tools("fake-token", mock_executor)
    fn = _get_registered_fn(mock_executor, "get_hubspot_owner")

    with patch(
        "app.integrations.providers.hubspot.safe_request", new_callable=AsyncMock
    ) as mock_req:
        result = await fn(owner_id="\u0661\u0662\u0663\u0664")  # Arabic-Indic digits

    assert "not a valid HubSpot owner ID" in result
    mock_req.assert_not_called()


# ---------------------------------------------------------------------------
# 400 error surfacing (HubSpot error payload not logged)
# ---------------------------------------------------------------------------

def _error_response(payload: dict, status: int = 400) -> httpx.Response:
    """Build a fake httpx.Response with the given JSON payload and status code."""
    return httpx.Response(status, json=payload)


def test_format_http_error_400_hubspot_style_no_message_in_errors():
    """HubSpot 400 errors sometimes have ``errors`` items with ``"error"``
    (code) + ``"name"`` (property) but NO ``"message"`` key.  The error
    string must include the code and property name rather than falling back
    to the generic top-level message."""
    resp = _error_response({
        "status": "error",
        "message": "There was a problem with the request.",
        "category": "VALIDATION_ERROR",
        "errors": [
            {"error": "UNKNOWN_PROPERTY", "name": "nfr_license_type"}
        ],
    })
    result = format_http_error(resp, "HubSpot", "search tickets")
    assert "UNKNOWN_PROPERTY" in result
    assert "nfr_license_type" in result
    # The generic fallback message must NOT be the only detail surfaced.
    assert result != "Error: HubSpot returned HTTP 400 while trying to search tickets. There was a problem with the request."


def test_format_http_error_400_includes_category():
    """A 400 response must surface the ``"category"`` field when present
    so log lines are self-explanatory (e.g. "[VALIDATION_ERROR]")."""
    resp = _error_response({
        "status": "error",
        "message": "There was a problem with the request.",
        "category": "VALIDATION_ERROR",
    })
    result = format_http_error(resp, "HubSpot", "search tickets")
    assert "VALIDATION_ERROR" in result


def test_format_http_error_400_message_in_errors():
    """When HubSpot ``errors[0]`` has a ``"message"`` key (the GitHub /
    standard format), that message is still surfaced correctly."""
    resp = _error_response({
        "status": "error",
        "message": "There was a problem with the request.",
        "category": "VALIDATION_ERROR",
        "errors": [
            {"message": "Invalid filterGroups", "error": "INVALID_FILTER_GROUPS"}
        ],
    })
    result = format_http_error(resp, "HubSpot", "search tickets")
    assert "Invalid filterGroups" in result


def test_format_http_error_400_multiple_errors_all_surfaced():
    """All errors in the array (up to 5) are joined into the result so
    multi-field validation failures are fully visible in a single log line."""
    resp = _error_response({
        "status": "error",
        "message": "There was a problem with the request.",
        "errors": [
            {"error": "UNKNOWN_PROPERTY", "name": "bad_prop_1"},
            {"error": "UNKNOWN_PROPERTY", "name": "bad_prop_2"},
        ],
    })
    result = format_http_error(resp, "HubSpot", "search tickets")
    assert "bad_prop_1" in result
    assert "bad_prop_2" in result


def test_format_http_error_400_empty_detail_has_no_trailing_space():
    """A 400 with an unparseable/empty body must produce a clean message
    ending at "bad request." — no dangling detail separator."""
    resp = httpx.Response(400, text="")
    result = format_http_error(resp, "HubSpot", "search tickets")
    assert result == "Error: HubSpot rejected the request to search tickets — bad request."


def test_redact_search_body_strips_values_keeps_structure():
    """The logged search body must keep property names / operators / sorts
    (needed to root-cause UNKNOWN_PROPERTY 400s) but redact the free-text
    query and filter values, which can carry customer data / PII."""
    body = _build_search_body(
        properties=["subject"],
        query="customer secret text",
        filters_json=(
            '[{"propertyName": "nfr_license_type", "operator": "EQ", "value": "standard"},'
            ' {"propertyName": "amount", "operator": "BETWEEN", "value": "100", "highValue": "500"}]'
        ),
        sort_by="createdate",
    )
    redacted = _redact_search_body(body)
    assert redacted["query"] == "<redacted>"
    filters = redacted["filterGroups"][0]["filters"]
    assert filters[0] == {
        "propertyName": "nfr_license_type",
        "operator": "EQ",
        "value": "<redacted>",
    }
    assert filters[1] == {
        "propertyName": "amount",
        "operator": "BETWEEN",
        "value": "<redacted>",
        "highValue": "<redacted>",
    }
    assert redacted["sorts"] == body["sorts"]
    # The original body is never mutated (it is still sent to HubSpot).
    assert body["query"] == "customer secret text"
    assert body["filterGroups"][0]["filters"][0]["value"] == "standard"


def test_format_http_error_400_no_errors_array_falls_back_to_message():
    """When HubSpot returns a 400 with only the top-level ``"message"``
    and no ``"errors"`` array, the message is still surfaced."""
    resp = _error_response({
        "status": "error",
        "message": "There was a problem with the request.",
    })
    result = format_http_error(resp, "HubSpot", "search tickets")
    assert "There was a problem with the request." in result
    # The response is now labelled "bad request" rather than the raw HTTP status.
    assert "bad request" in result


@pytest.mark.asyncio
async def test_search_hubspot_tickets_surfaces_400_error_detail(caplog):
    """``search_hubspot_tickets`` must surface a rich 400 error message
    (property name, error code, category) when HubSpot rejects the request
    — not just the generic "There was a problem with the request.".

    Exercises the REAL ``safe_request`` → ``format_http_error`` →
    ``_extract_error_detail`` chain via a mocked HTTP transport (no
    pre-baked error string), and asserts the rejected request body is
    logged at WARNING for production root-causing."""
    mock_executor = MagicMock()
    mock_executor.register = MagicMock()
    build_tools("fake-token", mock_executor)
    fn = _get_registered_fn(mock_executor, "search_hubspot_tickets")

    # Simulate a HubSpot 400 caused by a stale custom property in the filter.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={
                "status": "error",
                "message": "There was a problem with the request.",
                "category": "VALIDATION_ERROR",
                "errors": [
                    {"error": "UNKNOWN_PROPERTY", "name": "nfr_license_type"}
                ],
            },
        )

    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient

    def client_factory(**kwargs):
        kwargs["transport"] = transport
        return real_async_client(**kwargs)

    with (
        patch(
            "app.integrations.providers.hubspot.httpx.AsyncClient",
            side_effect=client_factory,
        ),
        caplog.at_level("WARNING", logger="app.integrations.providers.hubspot"),
    ):
        result = await fn(
            filters_json='[{"propertyName": "nfr_license_type", "operator": "EQ", "value": "standard"}]'
        )

    assert result == (
        "Error: HubSpot rejected the request to search tickets — bad request. "
        "UNKNOWN_PROPERTY property=nfr_license_type [VALIDATION_ERROR]"
    )
    # The rejected search payload must land in the WARNING stream so the
    # offending filter is visible in production logs without DEBUG logging —
    # with the filter VALUE redacted (it can carry customer data / PII).
    assert "search_hubspot_tickets failed" in caplog.text
    assert "nfr_license_type" in caplog.text
    assert "standard" not in caplog.text
    assert "<redacted>" in caplog.text


# ---------------------------------------------------------------------------
# 403 scope-error detail extraction
# ---------------------------------------------------------------------------


def test_format_http_error_403_object_style_required_scopes():
    """HubSpot 403 scope errors with object-style ``requiredScopes`` must
    include the scope names in the error detail so the user knows exactly
    which scopes to grant on the private app."""
    resp = httpx.Response(
        403,
        json={
            "status": "error",
            "message": "One or more of the following scopes are required.",
            "correlationId": "abc-123",
            "requiredScopes": [
                {
                    "scopeTypes": ["content"],
                    "requiredAllScopes": ["content"],
                    "requiredAnyScopes": [],
                }
            ],
        },
    )
    result = format_http_error(resp, "HubSpot", "list campaigns")
    assert "content" in result
    # Required scopes must appear in the hint, not just be mentioned.
    assert "Required scopes" in result
    # The base 403 phrasing must still be present.
    assert "access denied" in result
    assert "insufficient permissions or scopes" in result


def test_format_http_error_403_flat_string_required_scopes():
    """HubSpot 403 scope errors with a flat ``requiredScopes`` string list
    (newer API format) must also include the scope names."""
    resp = httpx.Response(
        403,
        json={
            "status": "error",
            "message": "This app hasn't been granted all required scopes.",
            "requiredScopes": ["content", "crm.objects.contacts.read"],
        },
    )
    result = format_http_error(resp, "HubSpot", "list campaigns")
    assert "content" in result
    assert "crm.objects.contacts.read" in result
    assert "Required scopes" in result


def test_format_http_error_403_context_nested_required_scopes():
    """``context.requiredScopes`` (alternative HubSpot layout) must also be
    extracted and included in the error detail."""
    resp = httpx.Response(
        403,
        json={
            "status": "error",
            "message": "Missing scopes.",
            "context": {
                "requiredScopes": ["marketing-email"],
            },
        },
    )
    result = format_http_error(resp, "HubSpot", "list campaigns")
    assert "marketing-email" in result
    assert "Required scopes" in result


def test_format_http_error_403_no_required_scopes_unchanged():
    """A plain 403 without ``requiredScopes`` must still produce the standard
    access-denied message — no regression for other providers."""
    resp = httpx.Response(
        403,
        json={
            "status": "error",
            "message": "Forbidden.",
        },
    )
    result = format_http_error(resp, "HubSpot", "list campaigns")
    assert "access denied" in result
    assert "Required scopes" not in result


def test_format_http_error_403_deduplicates_scope_names():
    """Scope names that appear in multiple sub-lists (e.g. ``scopeTypes`` and
    ``requiredAllScopes``) must be deduplicated in the output."""
    resp = httpx.Response(
        403,
        json={
            "status": "error",
            "message": "One or more of the following scopes are required.",
            "requiredScopes": [
                {
                    "scopeTypes": ["content"],
                    "requiredAllScopes": ["content"],
                    "requiredAnyScopes": [],
                }
            ],
        },
    )
    result = format_http_error(resp, "HubSpot", "list campaigns")
    # "content" should appear exactly once (not "content, content").
    assert result.count("content") == 1


def test_format_http_error_403_errors_nested_granular_scopes():
    """The documented MISSING_SCOPES shape nests scope names under
    ``errors[*].context.requiredGranularScopes``; the populated ``errors``
    array early-return must still carry the scope hint."""
    resp = httpx.Response(
        403,
        json={
            "status": "error",
            "message": "This app hasn't been granted all required scopes to make this call.",
            "errors": [
                {
                    "message": "All of the following scopes are required.",
                    "context": {"requiredGranularScopes": ["files"]},
                }
            ],
            "category": "MISSING_SCOPES",
        },
    )
    result = format_http_error(resp, "HubSpot", "upload file")
    assert "Required scopes: files" in result
    # The per-error message must still be present (early-return path).
    assert "All of the following scopes are required" in result


def test_required_scopes_reads_every_documented_key():
    """HubSpot's error schema also names them ``context.missingScopes``."""
    from app.integrations.http import required_scopes

    resp = httpx.Response(
        403,
        json={
            "message": "x",
            "context": {"missingScopes": ["automation.sequences.read"]},
            "errors": [{"message": "y", "context": {"requiredGranularScopes": ["forms"]}}],
        },
    )
    assert required_scopes(resp) == ["automation.sequences.read", "forms"]
    assert "Required scopes: automation.sequences.read, forms" in format_http_error(resp, "HubSpot", "list sequences")
    assert required_scopes(httpx.Response(403, text="<html>Forbidden</html>")) == []


def test_format_http_error_403_non_dict_context_still_parses_message():
    """A non-dict ``context`` value (some providers send a string) must not
    break detail extraction — the top-level message must still be surfaced
    instead of collapsing to the raw response text."""
    resp = httpx.Response(
        403,
        json={
            "status": "error",
            "message": "Forbidden.",
            "context": "not-a-dict",
        },
    )
    result = format_http_error(resp, "HubSpot", "list campaigns")
    assert "Forbidden." in result
    assert "Required scopes" not in result


def test_format_http_error_403_scope_hint_survives_long_detail():
    """When the base detail is near the 300-char cap, the scope suffix must
    survive truncation — the base detail is trimmed first so the actionable
    scope names are never cut off."""
    resp = httpx.Response(
        403,
        json={
            "status": "error",
            "message": "x" * 400,
            "requiredScopes": ["content"],
        },
    )
    result = format_http_error(resp, "HubSpot", "list campaigns")
    assert "Required scopes: content." in result


def test_format_http_error_403_ellipsis_detail_preserved():
    """A detail ending with an ellipsis ("...") must keep all its dots — only
    a single sentence-ending period is stripped before the scope suffix."""
    resp = httpx.Response(
        403,
        json={
            "status": "error",
            "message": "Truncated upstream message...",
            "requiredScopes": ["content"],
        },
    )
    result = format_http_error(resp, "HubSpot", "list campaigns")
    assert "Truncated upstream message..." in result
    assert "Required scopes: content." in result


def test_format_http_error_403_huge_scope_list_no_leading_space():
    """When the scope suffix alone fills the 300-char budget, the detail must
    start directly with "Required scopes:" — no leading space, no empty base."""
    from app.integrations.http import _append_scope_hint

    scopes = [f"crm.objects.very.long.scope.name.number.{i:02d}" for i in range(10)]
    result = _append_scope_hint("Some base detail.", {"requiredScopes": scopes})
    assert result.startswith("Required scopes: ")
    assert len(result) <= 300
