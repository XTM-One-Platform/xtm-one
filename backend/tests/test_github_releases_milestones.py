"""Tests for list_github_releases / list_github_milestones.

Both tools were missing an executor entirely ("No executor registered"); these
tests pin that they are now registered, exposed as tool definitions, and format
their results.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.integrations.providers.github import build_tools


def _mock_response(payload) -> httpx.Response:
    return httpx.Response(200, json=payload)


def _get_registered_fn(executor_mock: MagicMock, name: str):
    for call in executor_mock.register.call_args_list:
        if call[0][0] == name:
            return call[0][1]
    raise KeyError(f"Tool {name!r} not registered")


def test_release_and_milestone_tools_registered_and_exposed():
    mock_executor = MagicMock()
    mock_executor.register = MagicMock()
    tool_defs = build_tools("fake-token", mock_executor)

    registered = {c[0][0] for c in mock_executor.register.call_args_list}
    assert "list_github_releases" in registered
    assert "list_github_milestones" in registered

    names = {t.name for t in tool_defs}
    assert "list_github_releases" in names
    assert "list_github_milestones" in names


@pytest.mark.asyncio
async def test_list_github_releases_formats_output():
    mock_executor = MagicMock()
    mock_executor.register = MagicMock()
    build_tools("fake-token", mock_executor)
    fn = _get_registered_fn(mock_executor, "list_github_releases")

    payload = [
        {
            "name": "v2.0.0",
            "tag_name": "v2.0.0",
            "draft": False,
            "prerelease": False,
            "published_at": "2026-05-01T10:00:00Z",
            "html_url": "https://github.com/o/r/releases/tag/v2.0.0",
        },
        {
            "name": "",
            "tag_name": "v2.1.0-rc1",
            "draft": False,
            "prerelease": True,
            "published_at": "2026-06-01T10:00:00Z",
            "html_url": "https://github.com/o/r/releases/tag/v2.1.0-rc1",
        },
    ]
    with patch(
        "app.integrations.providers.github._common.safe_request", new_callable=AsyncMock
    ) as mock_req:
        mock_req.return_value = (_mock_response(payload), None)
        result = await fn(repo="o/r", count=10)

    assert "Releases in o/r (2)" in result
    assert "v2.0.0" in result
    assert "published 2026-05-01" in result
    assert "PRERELEASE" in result
    # Falls back to the tag when the release has no name.
    assert "v2.1.0-rc1" in result
    assert mock_req.call_args.kwargs["params"]["per_page"] == 10


@pytest.mark.asyncio
async def test_list_github_releases_empty():
    mock_executor = MagicMock()
    mock_executor.register = MagicMock()
    build_tools("fake-token", mock_executor)
    fn = _get_registered_fn(mock_executor, "list_github_releases")

    with patch(
        "app.integrations.providers.github._common.safe_request", new_callable=AsyncMock
    ) as mock_req:
        mock_req.return_value = (_mock_response([]), None)
        result = await fn(repo="o/r")

    assert "No releases found in o/r" in result


@pytest.mark.asyncio
async def test_list_github_milestones_formats_output_and_params():
    mock_executor = MagicMock()
    mock_executor.register = MagicMock()
    build_tools("fake-token", mock_executor)
    fn = _get_registered_fn(mock_executor, "list_github_milestones")

    payload = [
        {
            "number": 7,
            "title": "Q3 Release",
            "state": "open",
            "open_issues": 3,
            "closed_issues": 9,
            "due_on": "2026-09-30T00:00:00Z",
            "description": "Third quarter deliverables",
            "html_url": "https://github.com/o/r/milestone/7",
        }
    ]
    with patch(
        "app.integrations.providers.github._common.safe_request", new_callable=AsyncMock
    ) as mock_req:
        mock_req.return_value = (_mock_response(payload), None)
        result = await fn(
            repo="o/r", state="open", sort="due_on", direction="asc", count=5
        )

    assert "Milestones in o/r (1)" in result
    assert "#7 Q3 Release [open]" in result
    assert "9/12 issues closed" in result
    assert "due 2026-09-30" in result
    assert "Third quarter deliverables" in result
    params = mock_req.call_args.kwargs["params"]
    assert params["state"] == "open"
    assert params["sort"] == "due_on"
    assert params["direction"] == "asc"
    assert params["per_page"] == 5


@pytest.mark.asyncio
async def test_list_github_milestones_empty():
    mock_executor = MagicMock()
    mock_executor.register = MagicMock()
    build_tools("fake-token", mock_executor)
    fn = _get_registered_fn(mock_executor, "list_github_milestones")

    with patch(
        "app.integrations.providers.github._common.safe_request", new_callable=AsyncMock
    ) as mock_req:
        mock_req.return_value = (_mock_response([]), None)
        result = await fn(repo="o/r", state="closed")

    assert "No closed milestones found in o/r" in result


@pytest.mark.asyncio
async def test_list_github_milestones_empty_all_state_message():
    """``state="all"`` must not produce the awkward "No all milestones found";
    the state qualifier is dropped so the empty message reads naturally."""
    mock_executor = MagicMock()
    mock_executor.register = MagicMock()
    build_tools("fake-token", mock_executor)
    fn = _get_registered_fn(mock_executor, "list_github_milestones")

    with patch(
        "app.integrations.providers.github._common.safe_request", new_callable=AsyncMock
    ) as mock_req:
        mock_req.return_value = (_mock_response([]), None)
        result = await fn(repo="o/r", state="all")

    assert result == "No milestones found in o/r."
    assert "No all milestones" not in result


@pytest.mark.asyncio
async def test_list_github_releases_clamps_nonpositive_count():
    """A non-positive count must be clamped to a valid per_page (>= 1) so the
    request can't forward an invalid per_page to GitHub (matches
    search_github_issues)."""
    mock_executor = MagicMock()
    mock_executor.register = MagicMock()
    build_tools("fake-token", mock_executor)
    fn = _get_registered_fn(mock_executor, "list_github_releases")

    with patch(
        "app.integrations.providers.github._common.safe_request", new_callable=AsyncMock
    ) as mock_req:
        mock_req.return_value = (_mock_response([]), None)
        await fn(repo="o/r", count=0)

    assert mock_req.call_args.kwargs["params"]["per_page"] == 1


@pytest.mark.asyncio
async def test_list_github_milestones_clamps_nonpositive_count():
    """A non-positive count must be clamped to a valid per_page (>= 1)."""
    mock_executor = MagicMock()
    mock_executor.register = MagicMock()
    build_tools("fake-token", mock_executor)
    fn = _get_registered_fn(mock_executor, "list_github_milestones")

    with patch(
        "app.integrations.providers.github._common.safe_request", new_callable=AsyncMock
    ) as mock_req:
        mock_req.return_value = (_mock_response([]), None)
        await fn(repo="o/r", count=-5)

    assert mock_req.call_args.kwargs["params"]["per_page"] == 1
