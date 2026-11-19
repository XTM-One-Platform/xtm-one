from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.api.v1.integration_catalog import PROVIDER_CATALOG
from app.integrations.providers.notion import build_tools
from app.services.integration_tools import _SafeIntegrationExecutor
from app.tools.base import ToolCall


@pytest.mark.asyncio
async def test_list_notion_comments_formats_author_timestamp_and_text_with_pagination():
    executor = _SafeIntegrationExecutor()
    build_tools("dummy-token", executor)
    seen_params: list[dict[str, str]] = []

    async def _mock_get(*args, **kwargs):
        seen_params.append(dict(kwargs["params"]))
        if len(seen_params) == 1:
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "id": "comment-1",
                            "created_time": "2026-05-14T08:00:00.000Z",
                            "created_by": {"name": "Alice Reviewer"},
                            "rich_text": [{"plain_text": "Please clarify the rollout plan."}],
                        }
                    ],
                    "has_more": True,
                    "next_cursor": "cursor-2",
                },
            )
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "id": "comment-2",
                        "created_time": "2026-05-14T09:00:00.000Z",
                        "created_by": {"person": {"email": "bob@example.com"}},
                        "rich_text": [{"plain_text": "Looks good to me."}],
                    }
                ],
                "has_more": False,
            },
        )

    with patch("app.integrations.providers.notion._common.AutoRefreshClient.get", new_callable=AsyncMock) as mock_get:
        mock_get.side_effect = _mock_get

        result = await executor.execute_tool(
            ToolCall(
                id="call-comments",
                name="list_notion_comments",
                arguments={"page_id": "page-123"},
            )
        )

    assert not result.is_error
    assert "Alice Reviewer: Please clarify the rollout plan." in result.content
    assert "bob@example.com: Looks good to me." in result.content
    assert "[2026-05-14T08:00:00.000Z]" in result.content
    assert "[2026-05-14T09:00:00.000Z]" in result.content
    assert mock_get.await_count == 2
    assert seen_params[0] == {
        "block_id": "page-123",
        "page_size": "100",
    }
    assert seen_params[1] == {
        "block_id": "page-123",
        "page_size": "100",
        "start_cursor": "cursor-2",
    }


@pytest.mark.asyncio
async def test_list_notion_comments_lists_attachments_with_their_signed_url():
    executor = _SafeIntegrationExecutor()
    build_tools("dummy-token", executor)
    signed = "https://prod-files-secure.s3.us-west-2.amazonaws.com/ws/abc/Rollout%20plan.png?X-Amz-Signature=x"

    async def _mock_get(*args, **kwargs):
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "id": "comment-1",
                        "created_time": "2026-05-14T08:00:00.000Z",
                        "created_by": {"name": "Alice Reviewer"},
                        "rich_text": [{"plain_text": "See the screenshot."}],
                        "attachments": [
                            {
                                "category": "image",
                                "file": {"url": signed, "expiry_time": "2026-05-14T09:00:00.000Z"},
                            },
                            {"category": "file", "file": {}},
                        ],
                    },
                    {
                        "id": "comment-2",
                        "created_time": "2026-05-14T09:00:00.000Z",
                        "created_by": {"name": "Bob"},
                        "rich_text": [{"plain_text": "No file here."}],
                    },
                ],
                "has_more": False,
            },
        )

    with patch("app.integrations.providers.notion._common.AutoRefreshClient.get", new_callable=AsyncMock) as mock_get:
        mock_get.side_effect = _mock_get
        result = await executor.execute_tool(
            ToolCall(id="call-att", name="list_notion_comments", arguments={"page_id": "page-123"})
        )

    assert not result.is_error
    assert "Attachment (image): Rollout plan.png, URL expires 2026-05-14T09:00:00.000Z" in result.content
    assert f"file_url: {signed}" in result.content
    assert result.content.count("Attachment (") == 1
    assert result.content.count("download_notion_file(file_url=...)") == 1


@pytest.mark.asyncio
async def test_list_notion_comments_returns_actionable_missing_scope_error():
    executor = _SafeIntegrationExecutor()
    build_tools("dummy-token", executor)

    with patch("app.integrations.providers.notion._common.AutoRefreshClient.get", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = httpx.Response(
            403,
            json={
                "code": "unauthorized",
                "message": "Access denied for this operation.",
            },
        )

        result = await executor.execute_tool(
            ToolCall(
                id="call-comments-read-scope",
                name="list_notion_comments",
                arguments={"page_id": "page-123"},
            )
        )

    # A missing capability is returned as an error string: a failure.
    assert result.is_error
    assert "comments:read" in result.content
    assert "comments:write" in result.content
    assert "reconnect or re-authorize" in result.content
    assert "Capabilities tab" in result.content
    assert "shared with the integration" in result.content
    assert mock_get.await_count == 1


@pytest.mark.asyncio
async def test_add_notion_comment_returns_actionable_missing_scope_error():
    executor = _SafeIntegrationExecutor()
    build_tools("dummy-token", executor)

    with patch("app.integrations.providers.notion._common.AutoRefreshClient.post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = httpx.Response(
            403,
            json={
                "code": "unauthorized",
                "message": "Access denied for this operation.",
            },
        )

        result = await executor.execute_tool(
            ToolCall(
                id="call-comments-write-scope",
                name="add_notion_comment",
                arguments={"page_id": "page-123", "text": "Follow-up summary"},
            )
        )

    # A missing capability is returned as an error string: a failure.
    assert result.is_error
    assert "comments:write" in result.content
    assert "comments:read" in result.content
    assert "reconnect or re-authorize" in result.content
    assert "Capabilities tab" in result.content
    assert "shared with the integration" in result.content
    assert mock_post.await_count == 1


@pytest.mark.asyncio
async def test_list_notion_comments_missing_scope_error_with_unknown_error_code():
    """403 with an unrecognised Notion error code must still trigger the capability message.

    Notion has been observed returning codes like "validation_error" for the same
    missing-capability root cause that usually produces "unauthorized".  The old
    generic_403_signal only checked {"unauthorized", "restricted_resource"} so
    this case fell through to the raw HTTP error without actionable guidance.
    """
    executor = _SafeIntegrationExecutor()
    build_tools("dummy-token", executor)

    with patch("app.integrations.providers.notion._common.AutoRefreshClient.get", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = httpx.Response(
            403,
            json={
                "code": "validation_error",
                "message": "Insufficient permissions for this endpoint.",
            },
        )

        result = await executor.execute_tool(
            ToolCall(
                id="call-comments-unknown-code",
                name="list_notion_comments",
                arguments={"page_id": "page-999"},
            )
        )

    # A missing capability is returned as an error string: a failure.
    assert result.is_error
    assert "comments:read" in result.content
    assert "may be missing" in result.content
    assert "Capabilities tab" in result.content
    assert "reconnect or re-authorize" in result.content


@pytest.mark.asyncio
async def test_add_notion_comment_missing_scope_error_with_no_error_code():
    """403 with no JSON code field must still trigger the capability message.

    Notion sometimes returns a 403 without a structured 'code' field in the
    response body (or with a plain-text body).  The broadened generic_403_signal
    (status_code == 403) ensures these cases are caught rather than surfacing a
    raw 'access denied' message to the agent.

    The mock message deliberately contains NONE of the text markers checked by
    _format_comment_scope_error, so this test covers the pure status_code == 403
    branch (the unknown-error-code test above covers the production-observed
    "Insufficient permissions for this endpoint." wording, which also matches a
    text marker).
    """
    executor = _SafeIntegrationExecutor()
    build_tools("dummy-token", executor)

    with patch("app.integrations.providers.notion._common.AutoRefreshClient.post", new_callable=AsyncMock) as mock_post:
        # Response with no 'code' field and no capability-marker text.
        mock_post.return_value = httpx.Response(
            403,
            json={"message": "Access denied."},
        )

        result = await executor.execute_tool(
            ToolCall(
                id="call-comments-no-code",
                name="add_notion_comment",
                arguments={"page_id": "page-999", "text": "Needs update"},
            )
        )

    # A missing capability is returned as an error string: a failure.
    assert result.is_error
    assert "comments:write" in result.content
    assert "may be missing" in result.content
    assert "Capabilities tab" in result.content
    assert "reconnect or re-authorize" in result.content


def test_notion_catalog_documents_comment_capabilities_without_oauth_scope_param():
    notion = PROVIDER_CATALOG["notion"]

    assert notion["scopes"] == ""
    assert any("comments:read" in step for step in notion["setup_guide"]["steps"])
    assert any("comments:write" in step for step in notion["setup_guide"]["steps"])
    permissions = notion["setup_guide"]["permissions"]
    assert any("comments:read" in item["scope"] for item in permissions)
    assert any("comments:write" in item["scope"] for item in permissions)
    assert "Notion capabilities are configured" in notion["setup_guide"]["notes"]
