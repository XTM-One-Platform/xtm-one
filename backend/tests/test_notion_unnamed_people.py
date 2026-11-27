"""People whose names Notion withholds from the connection.

Without the integration's "Read user information" capability, Notion writes a
user mention as ``@Anonymous`` and cuts every user object to its id. The tool
results say so, so an agent does not report workspace members as anonymous.
"""

from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.api.v1.integration_catalog import PROVIDER_CATALOG
from app.integrations.providers.notion import build_tools
from app.integrations.providers.notion._common import (
    _rich_text_to_plain,
    collect_unnamed_people,
    with_unnamed_people_note,
)
from app.integrations.providers.notion.formatting import _summarize_properties
from app.services.integration_tools import _SafeIntegrationExecutor
from app.tools.base import ToolCall, ToolResult

_GET = "app.integrations.providers.notion._common.AutoRefreshClient.get"
_SAFE_REQUEST = "app.integrations.providers.notion._common.safe_request"
_SAFE_REQUEST = "app.integrations.providers.notion._common.safe_request"


def _mention(user: dict, plain_text: str = "@Anonymous") -> dict:
    return {
        "type": "mention",
        "mention": {"type": "user", "user": user},
        "plain_text": plain_text,
    }


def _text(content: str) -> dict:
    return {"type": "text", "text": {"content": content}, "plain_text": content}


def _url_of(args: tuple, kwargs: dict) -> str:
    return next((a for a in args if isinstance(a, str)), kwargs.get("url", ""))


def _paragraph(rich_text: list[dict]) -> dict:
    return {
        "object": "block",
        "id": "block-1",
        "type": "paragraph",
        "has_children": False,
        "paragraph": {"rich_text": rich_text},
    }


async def _run(executor: _SafeIntegrationExecutor, name: str, **arguments) -> ToolResult:
    return await executor.execute_tool(ToolCall(id=f"call-{name}", name=name, arguments=arguments))


@pytest.mark.asyncio
async def test_a_withheld_mention_is_explained_once_per_person():
    executor = _SafeIntegrationExecutor()
    build_tools("dummy-token", executor)
    block = _paragraph(
        [
            _text("Owners: "),
            _mention({"object": "user", "id": "user-1"}),
            _text(" and "),
            _mention({"object": "user", "id": "user-2"}),
            _text(", reviewed by "),
            _mention({"object": "user", "id": "user-1"}),
        ]
    )

    with patch(_SAFE_REQUEST, new_callable=AsyncMock) as mock_request:
        mock_request.return_value = (httpx.Response(200, json=block), None)
        result = await _run(executor, "get_notion_block", block_id="block-1")

    assert not result.is_error, result.content
    assert (
        "Owners: @[Anonymous](user:user-1) and @[Anonymous](user:user-2), reviewed by @[Anonymous](user:user-1)"
    ) in result.content
    assert result.content.count("Notion withheld the name of") == 1
    assert "Notion withheld the name of 2 people" in result.content
    assert "do not report them as anonymous" in result.content
    assert "Read user information without email addresses" in result.content


@pytest.mark.asyncio
async def test_a_named_mention_adds_no_note():
    executor = _SafeIntegrationExecutor()
    build_tools("dummy-token", executor)
    block = _paragraph(
        [
            _text("Owner: "),
            _mention(
                {"object": "user", "id": "user-1", "type": "person", "name": "Ada"},
                plain_text="@Ada",
            ),
        ]
    )

    with patch(_SAFE_REQUEST, new_callable=AsyncMock) as mock_request:
        mock_request.return_value = (httpx.Response(200, json=block), None)
        result = await _run(executor, "get_notion_block", block_id="block-1")

    assert not result.is_error, result.content
    assert "Owner: @[Ada](user:user-1)" in result.content
    assert "withheld" not in result.content


@pytest.mark.asyncio
async def test_an_error_result_is_returned_as_is():
    async def handler() -> str:
        _rich_text_to_plain([_mention({"object": "user", "id": "user-1"})])
        return "Error: Notion API returned 500."

    async def tool_result_handler() -> ToolResult:
        _rich_text_to_plain([_mention({"object": "user", "id": "user-1"})])
        return ToolResult(tool_call_id="", content="Error: failed", is_error=True)

    assert await with_unnamed_people_note(handler)() == "Error: Notion API returned 500."
    result = await with_unnamed_people_note(tool_result_handler)()
    assert result.content == "Error: failed"


def test_people_properties_show_who_notion_withheld():
    props = {
        "Owner": {"type": "people", "people": [{"object": "user", "id": "user-1"}]},
        "Reviewers": {
            "type": "people",
            "people": [
                {"object": "user", "id": "user-2", "type": "person", "name": "Ada"},
                {"object": "user", "id": "user-3"},
            ],
        },
        "Created by": {"type": "created_by", "created_by": {"object": "user", "id": "user-1"}},
    }

    with collect_unnamed_people() as seen:
        summary = _summarize_properties(props)

    assert "Owner: @Anonymous" in summary
    assert "Reviewers: Ada, @Anonymous" in summary
    assert "Created by: @Anonymous" in summary
    assert seen == {"user-1", "user-3"}


def test_nothing_is_collected_outside_a_tool_call():
    # A knowledge base sync renders the same rich text and keeps Notion's words.
    assert _rich_text_to_plain([_mention({"object": "user", "id": "user-1"})]) == "@Anonymous"


def _comments_payload(*authors: dict) -> dict:
    return {
        "results": [
            {
                "id": f"comment-{index}",
                "created_time": "2026-10-01T08:00:00.000Z",
                "created_by": author,
                "rich_text": [{"plain_text": f"Comment {index}"}],
            }
            for index, author in enumerate(authors)
        ],
        "has_more": False,
    }


@pytest.mark.asyncio
async def test_comment_authors_are_looked_up_by_id():
    executor = _SafeIntegrationExecutor()
    build_tools("dummy-token", executor)
    names = {"user-1": "Alice Reviewer", "user-2": "Bob Approver"}
    user_lookups: list[str] = []

    async def _mock_get(*args, **kwargs):
        url = _url_of(args, kwargs)
        if url.endswith("/comments"):
            return httpx.Response(
                200,
                json=_comments_payload(
                    {"object": "user", "id": "user-1"},
                    {"object": "user", "id": "user-2"},
                    {"object": "user", "id": "user-1"},
                ),
            )
        user_id = url.rsplit("/", 1)[-1]
        user_lookups.append(user_id)
        return httpx.Response(
            200,
            json={"object": "user", "id": user_id, "type": "person", "name": names[user_id]},
        )

    with patch(_GET, new_callable=AsyncMock) as mock_get:
        mock_get.side_effect = _mock_get
        result = await _run(executor, "list_notion_comments", page_id="page-1")

    assert not result.is_error, result.content
    assert "Alice Reviewer: Comment 0" in result.content
    assert "Bob Approver: Comment 1" in result.content
    assert "Alice Reviewer: Comment 2" in result.content
    assert sorted(user_lookups) == ["user-1", "user-2"]
    assert "withheld" not in result.content


@pytest.mark.asyncio
async def test_comment_authors_notion_will_not_name_are_explained():
    executor = _SafeIntegrationExecutor()
    build_tools("dummy-token", executor)
    user_lookups: list[str] = []

    async def _mock_get(*args, **kwargs):
        url = _url_of(args, kwargs)
        if url.endswith("/comments"):
            return httpx.Response(
                200,
                json=_comments_payload(
                    {"object": "user", "id": "user-1"},
                    {"object": "user", "id": "user-2"},
                ),
            )
        user_lookups.append(url)
        return httpx.Response(
            403,
            json={
                "object": "error",
                "status": 403,
                "code": "restricted_resource",
                "message": "Insufficient permissions for this endpoint.",
            },
        )

    with patch(_GET, new_callable=AsyncMock) as mock_get:
        mock_get.side_effect = _mock_get
        result = await _run(executor, "list_notion_comments", page_id="page-1")

    assert not result.is_error, result.content
    assert "@Anonymous: Comment 0" in result.content
    assert "@Anonymous: Comment 1" in result.content
    assert "Notion withheld the name of 2 people" in result.content
    # The first refusal settles it: the other authors are not looked up.
    assert len(user_lookups) == 1


@pytest.mark.asyncio
async def test_a_refused_user_read_names_the_missing_capability():
    executor = _SafeIntegrationExecutor()
    build_tools("dummy-token", executor)

    with patch(_GET, new_callable=AsyncMock) as mock_get:
        mock_get.return_value = httpx.Response(
            403,
            json={
                "object": "error",
                "status": 403,
                "code": "restricted_resource",
                "message": "Insufficient permissions for this endpoint.",
            },
        )
        get_result = await _run(executor, "get_notion_user", user_id="user-1")
        list_result = await _run(executor, "list_notion_users")

    for result in (get_result, list_result):
        assert result.is_error
        assert "not allowed to read user information" in result.content
        assert "Read user information without email addresses" in result.content
        assert "Capabilities tab" in result.content


def test_setup_guide_asks_for_the_user_information_capability():
    guide = PROVIDER_CATALOG["notion"]["setup_guide"]
    capability_step = next(step for step in guide["steps"] if "Select capabilities" in step)
    assert "Read user information without email addresses" in capability_step
    assert "@Anonymous" in guide["notes"]
