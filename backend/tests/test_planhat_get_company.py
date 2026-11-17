"""Regression tests for Planhat lookup input hardening.

Covers the shared fail-fast ID guard and whitespace normalization across
get_planhat_company, get_planhat_enduser, and get_planhat_ticket.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.integrations.providers.planhat import build_tools


def _get_registered_fn(executor_mock: MagicMock, name: str):
    """Extract a registered async function from the executor mock by tool name."""
    for call in executor_mock.register.call_args_list:
        if call[0][0] == name:
            return call[0][1]
    raise KeyError(f"Tool {name!r} not registered")


@pytest.mark.parametrize(
    "company_id",
    ["", "   ", "N/A", "none", "unknown", "null", None, 42, {"_id": "x"}, ["x"]],
)
@pytest.mark.asyncio
async def test_get_planhat_company_rejects_missing_or_placeholder_id(company_id):
    """Missing/placeholder/non-string IDs fail fast without spending HTTP calls.

    Non-string cases matter because tool-call arguments come from provider
    JSON without type coercion (see CallableToolExecutor.execute_tool) — a
    numeric or object ID must yield the actionable guard message, not an
    AttributeError from ``.strip()``.
    """
    executor = MagicMock()
    executor.register = MagicMock()
    build_tools("fake-token", executor)
    get_company_fn = _get_registered_fn(executor, "get_planhat_company")

    with patch(
        "app.integrations.providers.planhat.safe_request", new_callable=AsyncMock
    ) as mock_req:
        result = await get_company_fn(company_id=company_id)

    mock_req.assert_not_called()
    assert "company ID is missing or invalid" in result
    assert "search_planhat_companies" in result


@pytest.mark.asyncio
async def test_get_planhat_company_strips_id_before_request():
    """Leading/trailing whitespace in IDs is trimmed before calling Planhat."""
    executor = MagicMock()
    executor.register = MagicMock()
    build_tools("fake-token", executor)
    get_company_fn = _get_registered_fn(executor, "get_planhat_company")

    payload = {"_id": "56e911619f2ff04215515515", "name": "Acme", "status": "customer"}
    with patch(
        "app.integrations.providers.planhat.safe_request", new_callable=AsyncMock
    ) as mock_req:
        mock_req.return_value = (httpx.Response(200, json=payload), None)
        result = await get_company_fn(company_id="  56e911619f2ff04215515515  ")

    called_url = mock_req.call_args[0][2]
    assert called_url.endswith("/companies/56e911619f2ff04215515515")
    assert "Name: Acme" in result


@pytest.mark.asyncio
async def test_get_planhat_company_propagates_provider_not_found():
    """404-like provider errors should be returned unchanged to the agent."""
    executor = MagicMock()
    executor.register = MagicMock()
    build_tools("fake-token", executor)
    get_company_fn = _get_registered_fn(executor, "get_planhat_company")

    err = (
        "Error: Planhat resource not found while trying to get company. "
        "Detail: No such company profile"
    )
    with patch(
        "app.integrations.providers.planhat.safe_request", new_callable=AsyncMock
    ) as mock_req:
        mock_req.return_value = (None, err)
        result = await get_company_fn(company_id="missing-company")

    assert result == err


@pytest.mark.parametrize(
    ("tool_name", "kwarg", "entity", "lookup_hint"),
    [
        ("get_planhat_enduser", "enduser_id", "enduser", "list_planhat_endusers"),
        ("get_planhat_ticket", "ticket_id", "ticket", "list_planhat_tickets"),
    ],
)
@pytest.mark.parametrize("bad_id", ["", "   ", "N/A", "none", None, 42])
@pytest.mark.asyncio
async def test_planhat_sibling_lookups_reject_invalid_id(
    tool_name: str, kwarg: str, entity: str, lookup_hint: str, bad_id
):
    """The enduser/ticket lookups share the same fail-fast ID guard."""
    executor = MagicMock()
    executor.register = MagicMock()
    build_tools("fake-token", executor)
    lookup_fn = _get_registered_fn(executor, tool_name)

    with patch(
        "app.integrations.providers.planhat.safe_request", new_callable=AsyncMock
    ) as mock_req:
        result = await lookup_fn(**{kwarg: bad_id})

    mock_req.assert_not_called()
    assert f"{entity} ID is missing or invalid" in result
    assert lookup_hint in result


@pytest.mark.parametrize(
    ("tool_name", "kwarg", "path_prefix"),
    [
        ("get_planhat_enduser", "enduser_id", "/endusers/"),
        ("get_planhat_ticket", "ticket_id", "/conversations/"),
    ],
)
@pytest.mark.asyncio
async def test_planhat_sibling_lookups_strip_id_before_request(
    tool_name: str, kwarg: str, path_prefix: str
):
    """The enduser/ticket lookups also trim whitespace before building the URL."""
    executor = MagicMock()
    executor.register = MagicMock()
    build_tools("fake-token", executor)
    lookup_fn = _get_registered_fn(executor, tool_name)

    entity_id = "58e57392c9437d10624593fd"
    with patch(
        "app.integrations.providers.planhat.safe_request", new_callable=AsyncMock
    ) as mock_req:
        mock_req.return_value = (httpx.Response(200, json={"_id": entity_id}), None)
        result = await lookup_fn(**{kwarg: f"  {entity_id}  "})

    called_url = mock_req.call_args[0][2]
    assert called_url.endswith(f"{path_prefix}{entity_id}")
    assert entity_id in result
