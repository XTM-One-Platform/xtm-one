"""``get_microsoft_email(include_headers=True)`` answers a message's internet headers (#10889).

Why a message reads as automated (List-Unsubscribe, Precedence,
Auto-Submitted) is only in its headers; Graph returns them only when
``$select`` names ``internetMessageHeaders``. The read without the argument
keeps its select and its answer.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

import app.integrations.providers.microsoft as ms
from app.integrations.providers.microsoft import mail
from app.integrations.providers.microsoft._internet_headers import (
    MAX_HEADER_VALUE_CHARS,
    MAX_HEADERS,
    format_internet_headers,
)
from app.tools.base import CallableToolExecutor, ToolCall

MESSAGE_ID = "AAMkAGI2NGVhZTVlLTI1OGMtNDI4My1iZmVmLTI1ZmM5OWM5MTIwNgBGAAAAAADUuTJK1K9aTpCdqXop_4NaBwCd9nJ-tVys"


class _Ok:
    def __init__(self, payload: dict):
        self._payload = payload
        self.status_code = 200

    def json(self):
        return self._payload


@pytest.fixture
def graph(monkeypatch):
    state = SimpleNamespace(calls=[], message={})

    async def _fake(_client, method, url, _provider, _action, **kwargs):
        state.calls.append((method, url, kwargs))
        if f"/me/messages/{MESSAGE_ID}" in url and not url.endswith("/attachments"):
            return _Ok(state.message), None
        return _Ok({}), None

    async def _quiet(*_args, **_kwargs):
        return None

    monkeypatch.setattr(ms._common, "safe_request", _fake)
    monkeypatch.setattr(mail, "record_mail_draft", _quiet)
    return state


async def _read(**arguments: Any) -> str:
    executor = CallableToolExecutor()
    ms.build_tools(token="dummy", executor=executor)
    result = await executor.execute_tool(
        ToolCall(id="c1", name="get_microsoft_email", arguments={"message_id": MESSAGE_ID, **arguments})
    )
    return result.content


def _message_read_url(state) -> str:
    return next(url for method, url, _ in state.calls if method == "get" and f"/me/messages/{MESSAGE_ID}?" in url)


async def test_include_headers_selects_and_answers_the_headers(graph):
    graph.message = {
        "id": MESSAGE_ID,
        "subject": "Weekly digest",
        "body": {"content": "<p>Hi</p>"},
        "internetMessageHeaders": [
            {"name": "List-Unsubscribe", "value": "<mailto:unsubscribe@example.com>"},
            {"name": "Auto-Submitted", "value": "auto-generated"},
            {"name": "Precedence", "value": "bulk"},
        ],
    }
    answer = await _read(include_headers=True)
    assert "internetMessageHeaders" in _message_read_url(graph)
    assert "Internet headers (3 of 3):" in answer
    assert "List-Unsubscribe: <mailto:unsubscribe@example.com>" in answer
    assert "Auto-Submitted: auto-generated" in answer
    assert "Precedence: bulk" in answer


async def test_the_default_read_neither_selects_nor_answers_headers(graph):
    graph.message = {"id": MESSAGE_ID, "subject": "S", "body": {"content": "<p>Hi</p>"}}
    answer = await _read()
    assert "internetMessageHeaders" not in _message_read_url(graph)
    assert "Internet headers" not in answer


def test_the_headers_block_is_bounded_and_says_the_cut():
    headers = [{"name": f"Received-{i}", "value": "x" * (MAX_HEADER_VALUE_CHARS + 50)} for i in range(MAX_HEADERS + 5)]
    block = format_internet_headers(headers)
    assert f"({MAX_HEADERS} of {MAX_HEADERS + 5}, the first {MAX_HEADERS} shown)" in block
    assert f"Received-{MAX_HEADERS - 1}:" in block and f"Received-{MAX_HEADERS}:" not in block
    assert "[50 chars cut]" in block
    assert all(len(line) < MAX_HEADER_VALUE_CHARS + 60 for line in block.splitlines())


def test_a_message_without_headers_says_so():
    assert "none returned" in format_internet_headers(None)
    assert "none returned" in format_internet_headers([])


def test_the_tool_declares_include_headers():
    tools = ms.build_tools(token="dummy", executor=CallableToolExecutor())
    read = next(t for t in tools if t.name == "get_microsoft_email")
    prop = read.input_schema["properties"]["include_headers"]
    assert prop["type"] == "boolean" and prop["default"] is False
    assert len(prop["description"]) <= 200
