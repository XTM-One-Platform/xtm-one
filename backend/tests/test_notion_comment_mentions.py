"""add_notion_comment writes real user mentions (#2611).

``mentions`` (ids or emails) become ``mention`` runs at their ``@{...}``
placeholders (else at the start); an unknown person refuses the call before
any POST; ``rich_text_json`` posts a raw rich_text array instead of ``text``.
"""

from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.integrations.providers.notion import build_tools
from app.services.integration_tools import _SafeIntegrationExecutor
from app.tools.base import ToolCall

_ADA = {
    "object": "user",
    "id": "user-ada",
    "type": "person",
    "name": "Ada Lovelace",
    "person": {"email": "ada@example.com"},
}
_BOB = {
    "object": "user",
    "id": "user-bob",
    "type": "person",
    "name": "Bob Martin",
    "person": {"email": "bob@example.com"},
}


async def _users_get(*args: Any, **kwargs: Any) -> httpx.Response:
    url = str(args[0]) if args else str(kwargs.get("url"))
    if url.endswith("/users/user-bob"):
        return httpx.Response(200, json=_BOB)
    if url.endswith("/users"):
        return httpx.Response(200, json={"results": [_ADA, _BOB], "has_more": False, "next_cursor": None})
    return httpx.Response(404, json={"object": "error", "code": "object_not_found", "message": "Could not find user"})


async def _call(arguments: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    executor = _SafeIntegrationExecutor()
    build_tools("dummy-token", executor)
    posted: list[dict[str, Any]] = []

    async def _post(*args: Any, **kwargs: Any) -> httpx.Response:
        posted.append(kwargs.get("json") or {})
        return httpx.Response(200, json={"id": "comment-1"})

    with (
        patch(
            "app.integrations.providers.notion._common.AutoRefreshClient.get",
            new_callable=AsyncMock,
        ) as mock_get,
        patch(
            "app.integrations.providers.notion._common.AutoRefreshClient.post",
            new_callable=AsyncMock,
        ) as mock_post,
    ):
        mock_get.side_effect = _users_get
        mock_post.side_effect = _post
        result = await executor.execute_tool(ToolCall(id="m1", name="add_notion_comment", arguments=arguments))
    return result.content, posted


@pytest.mark.asyncio
async def test_an_email_placeholder_becomes_a_user_mention_run() -> None:
    content, posted = await _call(
        {
            "page_id": "page-1",
            "text": "Please review @{ada@example.com} today",
            "mentions": ["ada@example.com"],
        }
    )
    assert len(posted) == 1, content
    assert posted[0]["parent"] == {"page_id": "page-1"}
    assert posted[0]["rich_text"] == [
        {"type": "text", "text": {"content": "Please review "}},
        {"type": "mention", "mention": {"type": "user", "user": {"id": "user-ada"}}},
        {"type": "text", "text": {"content": " today"}},
    ]
    assert "mentioning Ada Lovelace" in content


@pytest.mark.asyncio
async def test_a_mention_without_placeholder_goes_first_and_an_id_is_read() -> None:
    content, posted = await _call({"page_id": "page-1", "text": "card is yours", "mentions": ["user-bob"]})
    assert posted[0]["rich_text"] == [
        {"type": "mention", "mention": {"type": "user", "user": {"id": "user-bob"}}},
        {"type": "text", "text": {"content": " "}},
        {"type": "text", "text": {"content": "card is yours"}},
    ]
    assert "mentioning Bob Martin" in content


@pytest.mark.asyncio
async def test_an_unknown_person_refuses_the_call_with_no_post() -> None:
    content, posted = await _call(
        {
            "page_id": "page-1",
            "text": "hi @{ada@example.com}",
            "mentions": ["ada@example.com", "ghost@example.com", "user-nobody"],
        }
    )
    assert posted == []
    assert content.startswith("Error:")
    assert "ghost@example.com" in content and "user-nobody" in content
    assert "nothing was posted" in content


@pytest.mark.asyncio
async def test_a_discussion_reply_carries_the_mention() -> None:
    content, posted = await _call(
        {
            "discussion_id": "disc-9",
            "text": "@{bob@example.com} over to you",
            "mentions": ["bob@example.com"],
        }
    )
    assert posted[0]["discussion_id"] == "disc-9"
    assert "parent" not in posted[0]
    assert posted[0]["rich_text"][0] == {
        "type": "mention",
        "mention": {"type": "user", "user": {"id": "user-bob"}},
    }
    assert "discussion disc-9" in content


@pytest.mark.asyncio
async def test_rich_text_json_is_posted_as_given() -> None:
    runs = [
        {"type": "mention", "mention": {"type": "user", "user": {"id": "user-ada"}}},
        {"type": "text", "text": {"content": " see above"}, "annotations": {"bold": True}},
    ]
    content, posted = await _call({"page_id": "page-1", "rich_text_json": runs})
    assert posted[0]["rich_text"] == runs, content


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        ({"page_id": "p", "text": "x", "rich_text_json": "[]"}, "rich_text_json"),
        (
            {"page_id": "p", "text": "x", "rich_text_json": '[{"type": "text", "text": {"content": "y"}}]'},
            "not both",
        ),
        ({"page_id": "p", "rich_text_json": '[{"type": "image"}]'}, "rich_text_json[0]"),
        ({"page_id": "p", "rich_text_json": "not json"}, "rich_text_json"),
    ],
)
async def test_bad_rich_text_json_is_refused_with_no_post(arguments: dict[str, Any], expected: str) -> None:
    content, posted = await _call(arguments)
    assert posted == []
    assert content.startswith("Error"), content
    assert expected in content


@pytest.mark.asyncio
async def test_a_long_text_around_a_mention_is_cut_per_run() -> None:
    content, posted = await _call(
        {
            "page_id": "page-1",
            "text": "x" * 2500 + " @{ada@example.com}",
            "mentions": ["ada@example.com"],
        }
    )
    first = posted[0]["rich_text"][0]["text"]["content"]
    assert len(first) == 2000 and first.endswith("…"), content
    assert posted[0]["rich_text"][1]["type"] == "mention"


@pytest.mark.asyncio
async def test_an_email_listed_without_emails_names_the_capability() -> None:
    no_email = [{**person, "person": {}} for person in (_ADA, _BOB)]

    async def _users_without_email(*args: Any, **kwargs: Any) -> httpx.Response:
        return httpx.Response(200, json={"results": no_email, "has_more": False, "next_cursor": None})

    with patch(f"{__name__}._users_get", side_effect=_users_without_email):
        content, posted = await _call(
            {"page_id": "page-1", "text": "hi @{ada@example.com}", "mentions": ["ada@example.com"]}
        )
    assert posted == []
    assert "ada@example.com" in content
    assert "Read user information including email addresses" in content
    assert "Nothing was posted" in content
    assert "no Notion workspace user matches" not in content
