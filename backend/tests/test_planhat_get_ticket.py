"""Regression tests for get_planhat_ticket.

The `[Emma] CS Assignments Manager` agent repeatedly called a non-existent
`get_planhat_ticket` tool, failing with "No executor registered". Planhat
stores tickets as conversations of type "ticket" and exposes no /tickets/{id}
read route, so the tool fetches the ticket via /conversations/{id} using the
same _id that list_planhat_tickets returns.
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


def test_get_planhat_ticket_is_registered():
    """The tool must be registered so the agent never hits 'No executor registered'."""
    executor = MagicMock()
    executor.register = MagicMock()
    definitions = build_tools("fake-token", executor)

    registered = {c[0][0] for c in executor.register.call_args_list}
    assert "get_planhat_ticket" in registered

    defined = {d.name for d in definitions}
    assert "get_planhat_ticket" in defined


def test_get_planhat_ticket_definition_shape():
    """The tool definition must require ticket_id and be a standard-priority lookup."""
    executor = MagicMock()
    executor.register = MagicMock()
    definitions = build_tools("fake-token", executor)

    ticket_def = next(d for d in definitions if d.name == "get_planhat_ticket")
    assert ticket_def.input_schema["required"] == ["ticket_id"]
    assert "ticket_id" in ticket_def.input_schema["properties"]
    # Standard priority so it is retained ahead of the extended list tool.
    assert ticket_def.priority == 1
    assert ticket_def.source == "integration:planhat"


@pytest.mark.asyncio
async def test_get_planhat_ticket_fetches_via_conversations_endpoint():
    """A single ticket is retrieved through the conversations endpoint."""
    executor = MagicMock()
    executor.register = MagicMock()
    build_tools("fake-token", executor)
    get_ticket_fn = _get_registered_fn(executor, "get_planhat_ticket")

    ticket_payload = {
        "_id": "58e57392c9437d10624593fd",
        "type": "ticket",
        "subject": "Login broken",
        "snippet": "User cannot log in to the portal.",
        "status": "open",
        "isOpen": True,
        "source": "zendesk",
        "severity": "high",
        "email": "user@acme.com",
        "companyId": "56e911619f2ff04215515515",
        "companyName": "Acme",
        "createDate": "2026-06-03T06:44:57.000Z",
        "tags": ["urgent"],
        "custom": {"Priority": "P1"},
    }

    with patch(
        "app.integrations.providers.planhat.safe_request", new_callable=AsyncMock
    ) as mock_req:
        mock_req.return_value = (httpx.Response(200, json=ticket_payload), None)
        result = await get_ticket_fn(ticket_id="58e57392c9437d10624593fd")

    # The request must target the conversations endpoint with the ticket id.
    called_url = mock_req.call_args[0][2]
    assert called_url.endswith("/conversations/58e57392c9437d10624593fd")

    assert "Login broken" in result
    assert "open" in result
    assert "zendesk" in result
    assert "high" in result
    assert "Acme" in result
    assert "User cannot log in to the portal." in result
    assert "58e57392c9437d10624593fd" in result


@pytest.mark.asyncio
async def test_get_planhat_ticket_propagates_error():
    """A not-found ticket surfaces the helper's error string verbatim."""
    executor = MagicMock()
    executor.register = MagicMock()
    build_tools("fake-token", executor)
    get_ticket_fn = _get_registered_fn(executor, "get_planhat_ticket")

    with patch(
        "app.integrations.providers.planhat.safe_request", new_callable=AsyncMock
    ) as mock_req:
        mock_req.return_value = (None, "Error: Planhat resource not found while trying to get ticket.")
        result = await get_ticket_fn(ticket_id="missing")

    assert result == "Error: Planhat resource not found while trying to get ticket."
