"""Planhat ticket list filter, severity mapping and the conversation read (#8661).

- ``/tickets`` ignores a ``status`` query parameter: the filter runs on the
  page, ``count`` applies after it.
- The severity of a help-desk ticket lives in ``custom.Severity``.
- ``get_planhat_conversation`` reads one conversation in full with its send time.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.integrations.providers.planhat import build_tools


def _tool(name: str):
    executor = MagicMock()
    executor.register = MagicMock()
    definitions = build_tools("fake-token", executor)
    for call in executor.register.call_args_list:
        if call[0][0] == name:
            return call[0][1], definitions
    raise KeyError(name)


_TICKETS = [
    {
        "_id": "t1",
        "subject": "Queued one",
        "status": "Queued",
        "source": "zendesk",
        "custom": {"Severity": "3 (Low impact)"},
        "companyName": "Acme",
    },
    {
        "_id": "t2",
        "subject": "Working on it",
        "status": "In Progress",
        "source": "zendesk",
        "custom": {"Severity": "2 (High impact)"},
        "companyName": "Acme",
    },
    {
        "_id": "t3",
        "subject": "Done",
        "status": "Closed",
        "source": "zendesk",
        "severity": "high",
        "companyName": "Acme",
    },
    {
        "_id": "t4",
        "subject": "Second queued",
        "status": "queued",
        "source": "email",
        "companyName": "Beta",
    },
]


async def _list(**kwargs):
    fn, _ = _tool("list_planhat_tickets")
    with patch(
        "app.integrations.providers.planhat.safe_request", new_callable=AsyncMock
    ) as mock_req:
        mock_req.return_value = (httpx.Response(200, json=_TICKETS), None)
        result = await fn(**kwargs)
    return result, mock_req.call_args.kwargs["params"]


@pytest.mark.asyncio
async def test_status_filter_keeps_only_matching_tickets_case_insensitive():
    result, params = await _list(status="Queued", count=5)
    assert "status" not in params  # the API ignores it; never sent
    assert "t1" in result and "t4" in result
    assert "t2" not in result and "t3" not in result


@pytest.mark.asyncio
async def test_status_open_means_not_closed_and_count_applies_after_filter():
    result, params = await _list(status="open", count=2)
    assert int(params["limit"]) >= 200
    assert "t1" in result and "t2" in result
    assert "t3" not in result and "t4" not in result  # count 2 after the filter


@pytest.mark.asyncio
async def test_status_closed_and_no_filter_keeps_the_page():
    closed, _ = await _list(status="closed")
    assert "t3" in closed and "t1" not in closed
    unfiltered, params = await _list(count=20)
    assert params["limit"] == "20"
    assert all(tid in unfiltered for tid in ("t1", "t2", "t3", "t4"))


@pytest.mark.asyncio
async def test_severity_read_from_custom_field_then_raw_then_question_mark():
    result, _ = await _list()
    lines = {line.split("ID: ")[-1].strip(): line for line in result.splitlines() if "ID: " in line}
    assert "Severity: 3 (Low impact)" in lines["t1"]
    assert "Severity: high" in lines["t3"]
    assert "Severity: ?" in lines["t4"]


@pytest.mark.asyncio
async def test_get_planhat_ticket_shows_custom_severity_and_updated_at():
    fn, _ = _tool("get_planhat_ticket")
    payload = {
        "_id": "t1",
        "status": "Queued",
        "custom": {"Severity": "3 (Low impact)"},
        "createDate": "2026-11-03T01:00:00.000Z",
        "updatedAt": "2026-11-04T22:15:00.000Z",
    }
    with patch(
        "app.integrations.providers.planhat.safe_request", new_callable=AsyncMock
    ) as mock_req:
        mock_req.return_value = (httpx.Response(200, json=payload), None)
        result = await fn(ticket_id="t1")
    assert "Severity: 3 (Low impact)" in result
    assert "Updated: 2026-11-04T22:15:00.000Z" in result


@pytest.mark.asyncio
async def test_get_planhat_conversation_reads_full_body_and_send_time():
    fn, definitions = _tool("get_planhat_conversation")
    body = "Answer to the AI governance question. " * 30  # well past the list preview
    payload = {
        "_id": "c1",
        "type": "email",
        "subject": "AI governance answers",
        "description": body,
        "snippet": body[:100],
        "date": "2026-11-04T06:44:57.000Z",
        "companyId": "co1",
        "companyName": "Acme",
        "users": [{"id": "u1", "name": "Jane Doe"}],
    }
    with patch(
        "app.integrations.providers.planhat.safe_request", new_callable=AsyncMock
    ) as mock_req:
        mock_req.return_value = (httpx.Response(200, json=payload), None)
        result = await fn(conversation_id="c1")

    assert mock_req.call_args[0][2].endswith("/conversations/c1")
    assert body.strip() in result
    assert "Date: 2026-11-04 06:44 UTC" in result
    assert "Acme (co1)" in result
    assert "Jane Doe" in result

    definition = next(d for d in definitions if d.name == "get_planhat_conversation")
    assert definition.input_schema["required"] == ["conversation_id"]
    assert definition.source == "integration:planhat"


@pytest.mark.asyncio
async def test_get_planhat_conversation_rejects_placeholder_id_without_a_call():
    fn, _ = _tool("get_planhat_conversation")
    with patch(
        "app.integrations.providers.planhat.safe_request", new_callable=AsyncMock
    ) as mock_req:
        result = await fn(conversation_id="unknown")
    assert result.startswith("Error:")
    assert "list_planhat_conversations" in result
    mock_req.assert_not_called()


@pytest.mark.asyncio
async def test_list_planhat_conversations_shows_the_send_time():
    fn, _ = _tool("list_planhat_conversations")
    payload = [
        {"_id": "c1", "type": "email", "subject": "Hi", "date": "2026-11-04T06:44:57.000Z"},
        {"_id": "c2", "type": "note", "subject": "Day only", "date": "2026-11-03"},
    ]
    with patch(
        "app.integrations.providers.planhat.safe_request", new_callable=AsyncMock
    ) as mock_req:
        mock_req.return_value = (httpx.Response(200, json=payload), None)
        result = await fn()
    assert "2026-11-04 06:44 UTC" in result
    assert "2026-11-03" in result
    assert "ID: c1" in result and "ID: c2" in result
