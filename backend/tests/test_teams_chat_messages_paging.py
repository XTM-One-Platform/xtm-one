"""``list_microsoft_chat_messages`` never sends Graph a ``$top`` above its 50 ceiling.

Graph refuses a chat messages ``$top`` over 50 ("limit of 50 for Top
exceeded"). The tool clamps *count* to its maximum, asks for at most 50 a page
and follows ``@odata.nextLink`` until it has *count* messages. Synthetic
tenant: Alice, Bob, fake ids.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from app.integrations.providers import microsoft as ms
from app.integrations.providers.microsoft import build_tools
from app.integrations.providers.microsoft.teams_chats import _CHAT_MESSAGES_MAX, _CHAT_MESSAGES_PAGE
from app.tools.base import CallableToolExecutor, ToolCall

GRAPH = "https://graph.microsoft.com/v1.0"
CHAT = "19:aaaabbbbccccddddeeeeffff00001111@thread.v2"
MESSAGES = f"/me/chats/{CHAT}/messages"


def _message(index: int) -> dict[str, Any]:
    created = (datetime.now(UTC) - timedelta(minutes=index)).strftime("%Y-%m-%dT%H:%M:%SZ")
    author = "Alice" if index % 2 else "Bob"
    return {
        "id": f"17000000{index:05d}",
        "messageType": "message",
        "createdDateTime": created,
        "from": {"user": {"id": f"user-{author.lower()}", "displayName": author}},
        "body": {"contentType": "text", "content": f"note {index}"},
    }


class _Response:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload
        self.status_code = 200

    def json(self) -> dict[str, Any]:
        return self._payload


@pytest.fixture
def graph(monkeypatch):
    """A fake Graph serving *total* chat messages, newest first, ``$top`` at most 50 a page."""
    sent: list[tuple[str, dict | None]] = []
    state = {"total": 0}

    async def fake_safe_request(_client, _method, url, _provider, _op, **kwargs):
        params = kwargs.get("params")
        sent.append((url, params))
        path = url.removeprefix(GRAPH)
        if path.startswith(MESSAGES + "?skip="):
            skip, top = (int(v) for v in path.removeprefix(MESSAGES + "?skip=").split("&top="))
        elif path == MESSAGES:
            top = int(params["$top"])
            if top > _CHAT_MESSAGES_PAGE:
                return None, "Microsoft: limit of 50 for Top exceeded"
            skip = 0
        else:
            return None, f"Microsoft: nothing at {path}"
        page = [_message(i) for i in range(skip, min(skip + top, state["total"]))]
        answer: dict[str, Any] = {"value": page}
        if skip + top < state["total"]:
            answer["@odata.nextLink"] = f"{GRAPH}{MESSAGES}?skip={skip + top}&top={top}"
        return _Response(answer), None

    monkeypatch.setattr(ms._common, "safe_request", fake_safe_request)
    return state, sent


async def _list(count: Any) -> Any:
    executor = CallableToolExecutor()
    build_tools(token="dummy", executor=executor)
    return await executor.execute_tool(
        ToolCall(id="c1", name="list_microsoft_chat_messages", arguments={"chat_id": CHAT, "count": count})
    )


@pytest.mark.asyncio
async def test_a_count_above_the_graph_ceiling_is_read_page_by_page(graph):
    state, sent = graph
    state["total"] = 500

    result = await _list(120)

    assert not result.is_error, result.content
    assert result.content.startswith("Found 120 message(s)")
    assert [int(p["$top"]) for _, p in sent if p] == [_CHAT_MESSAGES_PAGE]
    assert len(sent) == 3, "50 + 50 + 50, cut to 120"


@pytest.mark.asyncio
async def test_a_count_past_the_maximum_is_clamped(graph):
    state, sent = graph
    state["total"] = 1000

    result = await _list(5000)

    assert not result.is_error, result.content
    assert result.content.startswith(f"Found {_CHAT_MESSAGES_MAX} message(s)")
    assert len(sent) == _CHAT_MESSAGES_MAX // _CHAT_MESSAGES_PAGE


@pytest.mark.asyncio
async def test_a_small_count_is_one_request_and_a_string_count_is_read(graph):
    state, sent = graph
    state["total"] = 30

    result = await _list("10")

    assert not result.is_error, result.content
    assert result.content.startswith("Found 10 message(s)")
    assert sent == [(f"{GRAPH}{MESSAGES}", {"$top": "10", "$orderby": "createdDateTime desc"})]


@pytest.mark.asyncio
async def test_a_short_chat_stops_at_its_last_page(graph):
    state, sent = graph
    state["total"] = 70

    result = await _list(200)

    assert not result.is_error, result.content
    assert result.content.startswith("Found 70 message(s)")
    assert len(sent) == 2
