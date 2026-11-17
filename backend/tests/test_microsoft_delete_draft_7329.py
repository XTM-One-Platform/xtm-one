"""delete_microsoft_draft deletes a draft, and only a draft."""

from __future__ import annotations

from typing import Any

import pytest

import app.integrations.providers.microsoft as ms
from app.tools.base import CallableToolExecutor, ToolCall

DRAFT = "AAMkAGI2NGVhZTVlLTI1OGMtNDI4My1iZmVmLTI1ZmM5OWM5MTIwNgBGAAAAAADUuTJK1K9aTpCdqXop_4NaBwCdDRAFT"
RECEIVED = "AAMkAGI2NGVhZTVlLTI1OGMtNDI4My1iZmVmLTI1ZmM5OWM5MTIwNgBGAAAAAADUuTJK1K9aTpCdqXop_4NaBwCdINBOX"


class _Ok:
    def __init__(self, payload: dict | None = None):
        self._payload = payload or {}
        self.status_code = 200

    def json(self):
        return self._payload


@pytest.fixture
def graph(monkeypatch):
    calls: list[tuple[str, str, dict]] = []
    messages = {
        DRAFT: {"id": DRAFT, "isDraft": True, "subject": "Memo", "createdDateTime": "2026-11-02T10:00:00Z"},
        RECEIVED: {"id": RECEIVED, "isDraft": False, "subject": "Hello", "createdDateTime": "2026-11-01T09:00:00Z"},
    }

    async def _fake(_client, method, url, _provider, _action, **kwargs):
        calls.append((method, url, kwargs))
        message_id = url.rsplit("/", 1)[-1]
        if method == "get":
            return _Ok(messages[message_id]), None
        return _Ok({}), None

    monkeypatch.setattr(ms._common, "safe_request", _fake)
    return calls


async def _run(**arguments: Any) -> str:
    executor = CallableToolExecutor()
    ms.build_tools(token="dummy", executor=executor)
    result = await executor.execute_tool(ToolCall(id="c1", name="delete_microsoft_draft", arguments=arguments))
    return result.content


async def test_a_draft_is_deleted_and_the_answer_names_it(graph):
    answer = await _run(draft_id=DRAFT)
    assert [(m, u.rsplit("/", 1)[-1]) for m, u, _k in graph] == [("get", DRAFT), ("delete", DRAFT)]
    assert graph[0][2]["params"]["$select"] == "id,isDraft,subject,createdDateTime"
    assert "deleted" in answer and "'Memo'" in answer and "2026-11-02T10:00:00Z" in answer


async def test_a_message_that_is_not_a_draft_is_refused_and_left_untouched(graph):
    answer = await _run(draft_id=RECEIVED)
    assert answer.startswith("Error:") and "not a draft" in answer and "Nothing was deleted" in answer
    assert [m for m, _u, _k in graph] == ["get"]


async def test_an_address_is_refused_before_any_request(graph):
    answer = await _run(draft_id="sam@example.com")
    assert "Error" in answer
    assert graph == []


def test_the_tool_is_declared_and_classified_as_a_write():
    from app.services.tool_classification import classify_tool_name

    executor = CallableToolExecutor()
    names = {d.name for d in ms.build_tools(token="dummy", executor=executor)}
    assert "delete_microsoft_draft" in names
    assert classify_tool_name("delete_microsoft_draft")[0] == "write"
