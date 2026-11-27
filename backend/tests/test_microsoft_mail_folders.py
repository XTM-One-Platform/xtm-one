"""A custom Outlook folder is read by its name, path or id (#2585).

``list_microsoft_emails(folder="<custom folder>")`` put the display name in the
Graph URL segment, which Graph reads as an id: ``Id is malformed``. No tool
listed the folders either. ``list_microsoft_mail_folders`` lists the tree with
paths and ids, and ``list_microsoft_emails`` / ``search_microsoft_emails``
resolve a well-known name, a path, a display name or an id.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.integrations.providers import microsoft as ms
from app.integrations.providers.microsoft import build_tools
from app.services.integration_tools import _SafeIntegrationExecutor
from app.services.tool_classification import infer_tool_type
from app.tools.base import ToolCall, ToolResult

_BASE = "https://graph.microsoft.com/v1.0"
_INBOX_ID = "AAMkAGinbox" + "i" * 80
_CONTRACTS_ID = "AAMkAGcontracts" + "c" * 80
_PROJECTS_ID = "AAMkAGprojects" + "p" * 80
_ARCHIVE_ID = "AAMkAGarchive" + "a" * 80
_ARCHIVE_CONTRACTS_ID = "AAMkAGarchcontracts" + "d" * 80
_SENT_ID = "AAMkAGsent" + "s" * 80


def _folder(folder_id: str, name: str, children: int = 0, total: int = 3, unread: int = 1) -> dict[str, Any]:
    return {
        "id": folder_id,
        "displayName": name,
        "childFolderCount": children,
        "totalItemCount": total,
        "unreadItemCount": unread,
    }


_TREE: dict[str, list[dict[str, Any]]] = {
    f"{_BASE}/me/mailFolders": [
        _folder(_INBOX_ID, "Inbox", children=2),
        _folder(_SENT_ID, "Sent Items"),
        _folder(_ARCHIVE_ID, "Archive", children=1),
    ],
    f"{_BASE}/me/mailFolders/{_INBOX_ID}/childFolders": [
        _folder(_CONTRACTS_ID, "Contracts copies"),
        _folder(_PROJECTS_ID, "Projects"),
    ],
    f"{_BASE}/me/mailFolders/{_ARCHIVE_ID}/childFolders": [
        _folder(_ARCHIVE_CONTRACTS_ID, "Old"),
    ],
}


def _message(folder: str, index: int) -> dict[str, Any]:
    return {
        "id": f"AAMkmsg-{folder[:12]}-{index}",
        "subject": f"Subject {index}",
        "from": {"emailAddress": {"name": "Sender", "address": "sender@example.com"}},
        "receivedDateTime": "2026-11-27T08:00:00Z",
        "bodyPreview": "Hello",
        "isRead": True,
        "conversationId": f"conv{index}",
    }


class _Response:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload
        self.status_code = 200

    def json(self) -> dict[str, Any]:
        return self._payload


@pytest.fixture
def graph(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """A mailbox with a custom folder under Inbox; every request URL recorded."""
    urls: list[str] = []

    async def fake_safe_request(
        _client: Any, _method: str, url: str, _provider: str, _op: str, **kwargs: Any
    ) -> tuple[_Response | None, str | None]:
        urls.append(url)
        if url in _TREE:
            return _Response({"value": _TREE[url]}), None
        if url == f"{_BASE}/me/mailFolders/inbox":
            return _Response({"id": _INBOX_ID}), None
        if url.endswith("/messages"):
            segment = url.rsplit("/", 2)[-2]
            if segment == "me":
                return _Response({"value": [_message("all", 1), _message("all", 2)]}), None
            return _Response({"value": [_message(segment, 1)]}), None
        return None, "Error: Microsoft rejected the request to list emails — bad request. Id is malformed."

    monkeypatch.setattr(ms._common, "safe_request", fake_safe_request)
    return urls


async def _executor() -> _SafeIntegrationExecutor:
    executor = _SafeIntegrationExecutor()
    build_tools("token", executor)
    return executor


async def _call(executor: _SafeIntegrationExecutor, name: str, **arguments: Any) -> ToolResult:
    return await executor.execute_tool(ToolCall(id="c1", name=name, arguments=arguments))


def _messages_urls(urls: list[str]) -> list[str]:
    return [u for u in urls if u.endswith("/messages")]


@pytest.mark.asyncio
async def test_the_folders_are_listed_with_their_path_and_id(graph: list[str]) -> None:
    executor = await _executor()
    result = await _call(executor, "list_microsoft_mail_folders")

    assert result.is_error is False
    assert f"Inbox/Contracts copies | {_CONTRACTS_ID} | 0 | 3 | 1" in result.content
    assert f"Archive/Old | {_ARCHIVE_CONTRACTS_ID}" in result.content
    assert result.content.splitlines()[0].startswith("6 mail folders")


@pytest.mark.asyncio
async def test_the_folders_under_one_folder(graph: list[str]) -> None:
    executor = await _executor()
    result = await _call(executor, "list_microsoft_mail_folders", parent="inbox")

    assert result.is_error is False
    assert "under 'Inbox'" in result.content
    assert "Inbox/Contracts copies" in result.content
    assert "Inbox/Projects" in result.content
    assert "Archive/Old" not in result.content


@pytest.mark.parametrize(
    "folder",
    ["Inbox/Contracts copies", "inbox/contracts copies", "Contracts copies", "CONTRACTS COPIES", _CONTRACTS_ID],
)
@pytest.mark.asyncio
async def test_a_custom_folder_is_read_by_path_name_or_id(graph: list[str], folder: str) -> None:
    executor = await _executor()
    result = await _call(executor, "list_microsoft_emails", folder=folder, count=1)

    assert result.is_error is False, result.content
    assert _messages_urls(graph) == [f"{_BASE}/me/mailFolders/{_CONTRACTS_ID}/messages"]


@pytest.mark.asyncio
async def test_a_well_known_name_reads_no_folder_tree(graph: list[str]) -> None:
    executor = await _executor()
    result = await _call(executor, "list_microsoft_emails", folder="sent", count=1)

    assert result.is_error is False
    assert graph == [f"{_BASE}/me/mailFolders/sentitems/messages"]


@pytest.mark.asyncio
async def test_an_unknown_name_names_the_top_level_folders(graph: list[str]) -> None:
    executor = await _executor()
    result = await _call(executor, "list_microsoft_emails", folder="Nowhere")

    assert "no mail folder named 'Nowhere'" in result.content
    assert "'Inbox', 'Sent Items', 'Archive'" in result.content
    assert "malformed" not in result.content
    assert _messages_urls(graph) == []


@pytest.mark.asyncio
async def test_an_ambiguous_name_is_refused_with_the_candidate_paths(
    graph: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(
        _TREE,
        f"{_BASE}/me/mailFolders/{_ARCHIVE_ID}/childFolders",
        [_folder(_ARCHIVE_CONTRACTS_ID, "Contracts copies")],
    )
    executor = await _executor()
    result = await _call(executor, "list_microsoft_emails", folder="Contracts copies")

    assert "more than one mail folder" in result.content
    assert "'Inbox/Contracts copies'" in result.content
    assert "'Archive/Contracts copies'" in result.content
    assert _messages_urls(graph) == []

    by_path = await _call(executor, "list_microsoft_emails", folder="Archive/Contracts copies")
    assert by_path.is_error is False
    assert _messages_urls(graph) == [f"{_BASE}/me/mailFolders/{_ARCHIVE_CONTRACTS_ID}/messages"]


@pytest.mark.asyncio
async def test_the_tree_is_read_once_per_session(graph: list[str]) -> None:
    executor = await _executor()
    await _call(executor, "list_microsoft_emails", folder="Inbox/Contracts copies")
    await _call(executor, "list_microsoft_emails", folder="Projects")
    await _call(executor, "search_microsoft_emails", query="nda", folder="Contracts copies")

    assert graph.count(f"{_BASE}/me/mailFolders") == 1


@pytest.mark.asyncio
async def test_a_search_is_scoped_to_the_folder(graph: list[str]) -> None:
    executor = await _executor()
    scoped = await _call(executor, "search_microsoft_emails", query="nda", folder="Inbox/Contracts copies")
    everywhere = await _call(executor, "search_microsoft_emails", query="nda")

    assert scoped.is_error is False
    assert everywhere.is_error is False
    assert _messages_urls(graph) == [
        f"{_BASE}/me/mailFolders/{_CONTRACTS_ID}/messages",
        f"{_BASE}/me/messages",
    ]


@pytest.mark.asyncio
async def test_a_search_in_an_unknown_folder_is_refused(graph: list[str]) -> None:
    executor = await _executor()
    result = await _call(executor, "search_microsoft_emails", query="nda", folder="Nowhere")

    assert "no mail folder named 'Nowhere'" in result.content
    assert _messages_urls(graph) == []


def test_the_folder_listing_is_a_read() -> None:
    assert infer_tool_type("list_microsoft_mail_folders") == "read"
