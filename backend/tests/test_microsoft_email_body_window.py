"""``get_microsoft_email`` answers a long body whole or by window, never cut in silence.

A long internal email (about 33,000 characters of HTML) came back cut at 3,000
characters of text, mid-sentence, with no sign of the cut: a reader could
summarise or flag an email it never read whole. A body up to
``BODY_WINDOW_CHARS`` is answered whole; a longer one by window, each window
naming the characters left and the ``body_offset`` of the next one.
"""

from __future__ import annotations

import re
from types import SimpleNamespace
from typing import Any

import pytest

import app.integrations.providers.microsoft as ms
from app.integrations.providers.microsoft import mail
from app.integrations.providers.microsoft._body_window import BODY_WINDOW_CHARS, format_body_window
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
    state = SimpleNamespace(message={})

    async def _fake(_client, _method, url, _provider, _action, **_kwargs):
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


def _long_html(paragraphs: int) -> str:
    return "".join(f"<p>Paragraph {i:04d} says something worth reading to its end.</p>" for i in range(paragraphs))


async def test_a_short_body_is_answered_whole_without_a_marker(graph):
    graph.message = {"id": MESSAGE_ID, "subject": "S", "body": {"content": "<p>Hi there</p>"}}
    answer = await _read()
    assert "\nBody:\n" in answer and "Hi there" in answer
    assert "truncated" not in answer.lower()


async def test_a_body_past_the_old_3000_cut_is_answered_whole(graph):
    graph.message = {"id": MESSAGE_ID, "subject": "S", "body": {"content": _long_html(150)}}
    answer = await _read()
    assert "Paragraph 0149 says something worth reading to its end." in answer
    assert "truncated" not in answer.lower()


async def test_a_long_html_body_is_windowed_with_the_marker_and_read_to_its_end(graph):
    graph.message = {"id": MESSAGE_ID, "subject": "S", "body": {"content": _long_html(1200)}}
    first = await _read()
    marker = re.search(r"\[Body truncated: (\d+) characters left; .* body_offset=(\d+) ", first)
    assert marker, first[-400:]
    total = int(re.search(r"Body \(characters 0 to \d+ of (\d+)\)", first).group(1))
    left, offset = int(marker.group(1)), int(marker.group(2))
    assert offset == BODY_WINDOW_CHARS and left == total - offset

    seen = [first]
    while "[End of body]" not in seen[-1]:
        assert len(seen) < 10
        nxt = re.search(r"body_offset=(\d+) ", seen[-1])
        seen.append(await _read(body_offset=int(nxt.group(1))))
    text = "".join(seen)
    for i in (0, 599, 1199):
        assert f"Paragraph {i:04d}" in text


def test_the_window_states_the_cut_and_the_end():
    text = "x" * (BODY_WINDOW_CHARS + 10)
    first = format_body_window(text)
    assert f"of {BODY_WINDOW_CHARS + 10})" in first and "10 characters left" in first
    last = format_body_window(text, BODY_WINDOW_CHARS)
    assert "[End of body]" in last and "truncated" not in last
    past = format_body_window(text, 10**9)
    assert "Nothing at body_offset" in past


def test_the_tool_declares_body_offset():
    tools = ms.build_tools(token="dummy", executor=CallableToolExecutor())
    read = next(t for t in tools if t.name == "get_microsoft_email")
    prop = read.input_schema["properties"]["body_offset"]
    assert prop["type"] == "integer" and prop["default"] == 0
    assert len(prop["description"]) <= 200
