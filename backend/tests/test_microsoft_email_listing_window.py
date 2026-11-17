"""The folder email listing reads a received window across pages.

``list_microsoft_emails`` read one Graph page of ``count`` messages and had no
date filter and no cursor, so a 24-hour read of a busy inbox stopped at its
count. ``received_from`` / ``received_to`` keep a window, the answer counts it
and names the next ``page_token`` until the window is read.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import parse_qsl, urlencode

import pytest

from app.integrations.providers import microsoft as ms
from app.integrations.providers.microsoft import build_tools
from app.integrations.providers.microsoft._mail_window import (
    next_page_token,
    page_params,
    pages_line,
    received_filter,
    received_instant,
)
from app.services.integration_tools import _SafeIntegrationExecutor
from app.tools.base import ToolCall

_TOTAL = 320


def _message(index: int) -> dict[str, Any]:
    return {
        "id": f"AAMkAGI2win{index:04d}" + "x" * 133,
        "subject": f"Subject {index}",
        "from": {"emailAddress": {"name": "Sender", "address": "sender@example.com"}},
        "receivedDateTime": f"2026-11-09T{23 - index // 14:02d}:{59 - (index % 14) * 4:02d}:00Z",
        "bodyPreview": "Hello",
        "isRead": True,
        "conversationId": f"AAQkconv{index:04d}",
    }


class _Response:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload
        self.status_code = 200

    def json(self) -> dict[str, Any]:
        return self._payload


@pytest.fixture
def graph(monkeypatch):
    """A folder of 320 messages served by $top / $skip with Graph's nextLink and count; every request recorded."""
    messages = [_message(i) for i in range(_TOTAL)]
    sent: list[dict[str, str]] = []

    async def fake_safe_request(_client, _method, url, _provider, _op, **kwargs):
        params = dict(kwargs.get("params") or {})
        sent.append(params)
        top, skip = int(params["$top"]), int(params.get("$skip", "0"))
        payload: dict[str, Any] = {"value": messages[skip : skip + top]}
        if params.get("$count") == "true":
            payload["@odata.count"] = _TOTAL
        if skip + top < _TOTAL:
            query = {k: v for k, v in params.items() if k != "$skip"}
            query["$skip"] = str(skip + top)
            payload["@odata.nextLink"] = f"{url}?{urlencode(query)}"
        return _Response(payload), None

    monkeypatch.setattr(ms._common, "safe_request", fake_safe_request)
    return sent


async def _call(**arguments: Any):
    executor = _SafeIntegrationExecutor()
    build_tools("token", executor)
    return await executor.execute_tool(ToolCall(id="c1", name="list_microsoft_emails", arguments=arguments))


def _token(content: str) -> str:
    marker = 'page_token="'
    if marker not in content:
        return ""
    return content.split(marker, 1)[1].split('"', 1)[0]


@pytest.mark.asyncio
async def test_a_window_is_read_whole_across_pages(graph):
    seen: list[str] = []
    footers: list[str] = []
    token = ""
    for _ in range(10):
        arguments: dict[str, Any] = {"count": 150, "include_preview": False, "received_from": "2026-11-09T00:00:00Z"}
        if token:
            arguments["page_token"] = token
        result = await _call(**arguments)
        assert result.is_error is False
        lines = result.content.split("\n")
        body = [line for line in lines[1:] if line.startswith("AAMk")]
        seen += [line.split(" | ", 1)[0] for line in body]
        footers.append(lines[-2] if _token(result.content) else lines[-1])
        token = _token(result.content)
        if not token:
            assert lines[-1] == "No more messages."
            break
    assert seen == [_message(i)["id"] for i in range(_TOTAL)]
    assert len(graph) == 3
    assert all(p["$filter"] == "receivedDateTime ge 2026-11-09T00:00:00Z" for p in graph)
    assert all(p["$orderby"] == "receivedDateTime desc" and p["$top"] == "150" for p in graph)
    assert footers[0] == "Messages 1-150 of 320; 2 more pages remain at this count."
    assert footers[1] == "Messages 151-300 of 320; 1 more page remains at this count."
    assert footers[2] == "No more messages."


@pytest.mark.asyncio
async def test_the_full_listing_pages_too_and_keeps_its_previews(graph):
    first = await _call(count=100, received_from="2026-11-09", received_to="2026-11-10T00:00:00+01:00")
    assert first.content.count("Preview: Hello") == 100
    assert first.content.endswith(f'More messages: the same call with page_token="{_token(first.content)}".')
    assert graph[0]["$filter"] == (
        "receivedDateTime ge 2026-11-09T00:00:00Z and receivedDateTime lt 2026-11-09T23:00:00Z"
    )
    second = await _call(count=100, received_from="2026-11-09", page_token=_token(first.content))
    assert graph[1]["$skip"] == "100"
    assert f"ID: {_message(100)['id']}" in second.content


@pytest.mark.asyncio
async def test_a_call_without_the_new_parameters_reads_as_before(graph):
    result = await _call(count=5, include_preview=False)
    assert "$filter" not in graph[0] and "$count" not in graph[0] and "$skip" not in graph[0]
    lines = result.content.split("\n")
    # One header and one line per message; a next page is still named.
    assert len(lines) == 1 + 5 + 1
    assert lines[-1].startswith('More messages: the same call with page_token="')


@pytest.mark.asyncio
async def test_a_bad_date_or_a_foreign_token_is_refused_before_any_request(graph):
    bad_date = await _call(received_from="yesterday")
    assert "received_from must be an ISO-8601 date" in bad_date.content
    foreign = await _call(page_token="https://evil.example/steal")
    assert "page_token is not a page token of this listing" in foreign.content
    assert graph == []


def test_the_token_keeps_only_the_paging_parameters():
    link = (
        "https://graph.microsoft.com/v1.0/me/mailFolders/inbox/messages?"
        "%24top=150&%24select=id&%24expand=x&%24count=true&%24filter=receivedDateTime+ge+2026-11-09T00%3A00%3A00Z"
        "&%24orderby=receivedDateTime+desc&%24skip=150"
    )
    token = next_page_token(link)
    assert dict(parse_qsl(token)) == {
        "$filter": "receivedDateTime ge 2026-11-09T00:00:00Z",
        "$orderby": "receivedDateTime desc",
        "$skip": "150",
    }
    assert page_params(token) == dict(parse_qsl(token))
    assert next_page_token(None) == ""
    assert page_params("$filter=x") is None


def test_dates_and_counts():
    assert received_instant("2026-11-09T07:39:00+02:00") == "2026-11-09T05:39:00Z"
    assert received_instant("2026-11-09") == "2026-11-09T00:00:00Z"
    assert received_instant("not a date") is None
    assert received_filter() == ("", None)
    assert pages_line(None, 0, 10, 10) == ""
    assert pages_line(0, 0, 0, 10) == "Messages 0-0 of 0; 0 more pages remain at this count."
    assert pages_line(25, 20, 5, 10) == "Messages 21-25 of 25; 0 more pages remain at this count."
