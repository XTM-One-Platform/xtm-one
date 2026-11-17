"""Editing a Teams message keeps its file cards.

Graph replaces a chatMessage whole on PATCH: a file posted as a ``reference``
attachment shows only while the body carries its ``<attachment id>`` tag and
the attachment is sent again. The edit tools read the message first and send
its file attachments back with the new body; a card or a quoted message is
not carried over, and a message without a file is edited exactly as before.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.integrations.providers import microsoft as ms
from app.integrations.providers.microsoft import build_tools
from app.integrations.providers.microsoft._kept_files import file_attachments, keep_file_attachments
from app.tools.base import CallableToolExecutor, ToolCall

GRAPH = "https://graph.microsoft.com/v1.0"
TEAM = "6c4d3b2a-1f0e-4d9c-8b7a-6f5e4d3c2b1a"
CHANNEL = "19:1b2c3d4e5f60718293a4b5c6d7e8f9a0@thread.tacv2"
CHAT = "19:1f2e3d4c5b6a79880716253443526170@thread.v2"
MESSAGE_ID = "1789541727000"
FILE_ID = "5e2a4d0c-1b3f-4a6e-9c8d-7f1e2d3c4b5a"

FILE = {
    "id": FILE_ID,
    "contentType": "reference",
    "contentUrl": "https://contoso.sharepoint.com/personal/agent/Documents/report.pdf",
    "name": "report.pdf",
    "content": None,
    "thumbnailUrl": None,
}
CARD = {"id": "c1", "contentType": "application/vnd.microsoft.card.adaptive", "content": "{}"}


class _Graph:
    def __init__(self, attachments: list[dict[str, Any]], read_error: str | None = None) -> None:
        self.attachments = attachments
        self.read_error = read_error
        self.calls: list[tuple[str, str, dict | None]] = []

    async def __call__(self, _client, method, url, _provider, _action, **kwargs):
        self.calls.append((method, url, kwargs.get("json")))
        if method == "get":
            if self.read_error:
                return None, self.read_error
            return _Resp({"id": MESSAGE_ID, "attachments": self.attachments}), None
        return _Resp(None, 204), None

    def patch(self) -> tuple[str, dict]:
        patches = [(url, payload) for method, url, payload in self.calls if method == "patch"]
        assert len(patches) == 1, self.calls
        return patches[0]


class _Resp:
    def __init__(self, payload: dict | None, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def json(self) -> dict:
        return self._payload or {}


async def _call(tool: str, **arguments):
    executor = CallableToolExecutor()
    build_tools(token="dummy", executor=executor)
    return await executor.execute_tool(ToolCall(id="c1", name=tool, arguments=arguments))


_EDITS = [
    ("update_microsoft_chat_message", {"chat_id": CHAT}, f"{GRAPH}/chats/{CHAT}/messages/{MESSAGE_ID}"),
    (
        "update_microsoft_channel_message",
        {"team_id": TEAM, "channel_id": CHANNEL},
        f"{GRAPH}/teams/{TEAM}/channels/{CHANNEL}/messages/{MESSAGE_ID}",
    ),
]


@pytest.mark.parametrize(("tool", "where", "url"), _EDITS)
async def test_an_edit_sends_the_file_card_back_with_the_new_text(monkeypatch, tool, where, url):
    graph = _Graph([FILE, CARD])
    monkeypatch.setattr(ms._common, "safe_request", graph)

    result = await _call(tool, message_id=MESSAGE_ID, message="Updated report", **where)

    assert not result.is_error, result.content
    assert graph.calls[0][:2] == ("get", url)
    patch_url, payload = graph.patch()
    assert patch_url == url
    assert payload["body"] == {
        "contentType": "html",
        "content": f'<p>Updated report</p><attachment id="{FILE_ID}"></attachment>',
    }
    # Only the file is carried over: the old card is not.
    assert payload["attachments"] == [
        {"id": FILE_ID, "contentType": "reference", "contentUrl": FILE["contentUrl"], "name": "report.pdf"}
    ]
    assert result.content.endswith(" Files kept: report.pdf.")


@pytest.mark.parametrize(("tool", "where", "url"), _EDITS)
async def test_an_edit_of_a_message_without_a_file_is_sent_as_before(monkeypatch, tool, where, url):
    graph = _Graph([CARD])
    monkeypatch.setattr(ms._common, "safe_request", graph)

    result = await _call(tool, message_id=MESSAGE_ID, message="Fixed", **where)

    assert not result.is_error, result.content
    _, payload = graph.patch()
    assert payload == {"body": {"contentType": "html", "content": "<p>Fixed</p>"}}
    assert result.content.endswith("Last edited: .")


@pytest.mark.parametrize(("tool", "where", "url"), _EDITS)
async def test_an_unreadable_message_is_still_edited_with_a_warning(monkeypatch, tool, where, url):
    graph = _Graph([], read_error="Error: Microsoft Graph returned 403 (Forbidden).")
    monkeypatch.setattr(ms._common, "safe_request", graph)

    result = await _call(tool, message_id=MESSAGE_ID, message="Fixed", **where)

    assert not result.is_error, result.content
    _, payload = graph.patch()
    assert "attachments" not in payload
    assert "could not be read before the edit" in result.content
    assert "403 (Forbidden)" in result.content


def test_a_file_without_a_content_url_is_not_carried_over():
    assert file_attachments({"attachments": [{**FILE, "contentUrl": ""}, "junk", None]}) == []
    assert file_attachments(None) == []


def test_a_tag_already_in_the_body_is_not_written_twice():
    files = file_attachments({"attachments": [FILE]})
    payload = {"body": {"contentType": "html", "content": f'<p>x</p><attachment id="{FILE_ID}"></attachment>'}}

    keep_file_attachments(payload, files)

    assert payload["body"]["content"].count(f'<attachment id="{FILE_ID}">') == 1
    assert payload["attachments"] == files
