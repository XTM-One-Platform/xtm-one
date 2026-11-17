"""list_notion_users pages with a cursor and finds a user by name or email.

A workspace of more than one page of users used to stop at ``count`` with no
cursor and no lookup, so a member past the cap could not be resolved to an id.
"""

from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.integrations.providers.notion import build_tools
from app.services.integration_tools import _SafeIntegrationExecutor
from app.tools.base import ToolCall, ToolResult

_GET = "app.integrations.providers.notion._common.AutoRefreshClient.get"

_USERS = [
    {
        "object": "user",
        "id": f"user-{i}",
        "type": "person",
        "name": f"Person {i}",
        "person": {"email": f"person{i}@example.com"},
    }
    for i in range(250)
]


def _fake_users_api(calls: list[dict]):
    """GET /v1/users over _USERS: page_size and start_cursor honoured."""

    async def _get(*args, **kwargs):
        params = dict(kwargs.get("params") or {})
        calls.append(params)
        start = int(params.get("start_cursor") or 0)
        size = int(params.get("page_size") or 100)
        page = _USERS[start : start + size]
        end = start + len(page)
        more = end < len(_USERS)
        return httpx.Response(
            200,
            json={
                "object": "list",
                "results": page,
                "has_more": more,
                "next_cursor": str(end) if more else None,
            },
        )

    return _get


async def _run(**arguments) -> tuple[ToolResult, list[dict]]:
    executor = _SafeIntegrationExecutor()
    build_tools("dummy-token", executor)
    calls: list[dict] = []
    with patch(_GET, new_callable=AsyncMock) as mock_get:
        mock_get.side_effect = _fake_users_api(calls)
        result = await executor.execute_tool(ToolCall(id="call-users", name="list_notion_users", arguments=arguments))
    return result, calls


@pytest.mark.asyncio
async def test_the_first_page_keeps_its_lines_and_ends_with_an_exact_cursor():
    result, calls = await _run(count=200)

    assert not result.is_error, result.content
    assert result.content.startswith("Found 200 users:")
    assert "• Person 0 (person0@example.com) [person] — ID: user-0" in result.content
    assert "ID: user-199" in result.content
    assert "ID: user-200" not in result.content
    assert 'list_notion_users(after="200", count=200)' in result.content
    assert [c["page_size"] for c in calls] == ["100", "100"]


@pytest.mark.asyncio
async def test_the_cursor_reads_the_users_past_the_first_page():
    result, calls = await _run(count=200, after="200")

    assert not result.is_error, result.content
    assert result.content.startswith("Found 50 users:")
    assert "ID: user-200" in result.content
    assert "ID: user-249" in result.content
    assert "More users" not in result.content
    assert calls[0]["start_cursor"] == "200"


@pytest.mark.asyncio
async def test_a_page_never_overshoots_the_count_so_no_user_is_skipped():
    result, calls = await _run(count=30)

    assert result.content.startswith("Found 30 users:")
    assert calls[0]["page_size"] == "30"
    assert 'after="30"' in result.content


@pytest.mark.asyncio
async def test_a_query_finds_a_user_past_the_first_page_by_email():
    result, calls = await _run(query="PERSON231@example.com")

    assert not result.is_error, result.content
    assert result.content.startswith("Found 1 user:")
    assert "ID: user-231" in result.content
    assert len(calls) == 3
    assert "More users" not in result.content


@pytest.mark.asyncio
async def test_a_query_matches_the_name_and_says_when_nothing_matches():
    by_name, _ = await _run(query="person 7")
    assert "ID: user-7" in by_name.content
    assert "ID: user-70" in by_name.content

    none, _ = await _run(query="nobody@example.com")
    assert none.content == "No users match 'nobody@example.com'."


@pytest.mark.asyncio
async def test_a_query_scan_stops_at_its_page_cap_with_a_cursor():
    with patch("app.integrations.providers.notion.users._QUERY_MAX_PAGES", 1):
        result, calls = await _run(query="person249")

    assert len(calls) == 1
    assert result.content.startswith("No users match 'person249'.")
    assert 'list_notion_users(query="person249", after="100")' in result.content
