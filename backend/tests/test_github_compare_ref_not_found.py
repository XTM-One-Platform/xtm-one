"""A compare on a ref that no longer exists answers a result, not an error.

A fake ``safe_request`` answers the compare call with a 404, then the
repository read, the ref resolution and the pull request lookup. A branch
deleted after its pull request merged is ``ref_not_found`` (non-error, the
pull request named); a missing repository keeps the error.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from app.integrations.providers.github import build_tools
from app.tools.base import ToolResult

_SAFE_REQUEST = "app.integrations.providers.github._common.safe_request"
_API = "https://api.github.com/repos/acme/app"
_NOT_FOUND = "Error: GitHub resource not found while trying to compare."


def _compare_tool():
    executor = MagicMock()
    executor.register = MagicMock()
    build_tools("fake-token", executor)
    handlers = {call[0][0]: call[0][1] for call in executor.register.call_args_list}
    return handlers["compare_github_commits"]


def _resp(status: int, payload=None) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status
    resp.json.return_value = payload if payload is not None else {}
    return resp


class FakeGitHub:
    """Answers each URL from a table: ``(status, payload)``; 2xx is a success."""

    def __init__(self, routes: dict[str, tuple[int, object]]) -> None:
        self.routes = routes
        self.calls: list[tuple[str, dict]] = []

    async def __call__(self, client, method, url, provider, action, **kwargs):
        self.calls.append((url, kwargs))
        status, payload = self.routes.get(url, (404, {}))
        if status < 300:
            return _resp(status, payload), None
        resp = _resp(status, payload)
        err = _NOT_FOUND if status == 404 else f"Error: GitHub HTTP {status}."
        return (resp if "expected_statuses" in kwargs else None), err


@pytest.mark.asyncio
async def test_a_deleted_head_branch_answers_ref_not_found_with_its_pull_request():
    fake = FakeGitHub(
        {
            _API: (200, {"full_name": "acme/app"}),
            f"{_API}/commits/abc1234": (200, {"sha": "abc1234"}),
            f"{_API}/pulls": (200, [{"number": 42, "state": "closed", "merged_at": "2026-11-05T10:00:00Z"}]),
        }
    )
    with patch(_SAFE_REQUEST, new=fake):
        result = await _compare_tool()(repo="acme/app", base="abc1234", head="fix/gone")

    assert isinstance(result, ToolResult)
    assert not result.is_error
    assert result.content.startswith("ref_not_found: fix/gone (head) does not exist in acme/app.")
    assert "#42 (merged)" in result.content
    assert result.structured["status"] == "ref_not_found"
    assert result.structured["missing"] == ["fix/gone"]
    assert result.structured["pull_requests"] == {"fix/gone": {"number": 42, "state": "closed", "merged": True}}
    # sandbox_checkout_rebase reads no merge base from it.
    assert result.structured["merge_base"] == ""
    pulls_call = next(kwargs for url, kwargs in fake.calls if url == f"{_API}/pulls")
    assert pulls_call["params"]["head"] == "acme:fix/gone"


@pytest.mark.asyncio
async def test_a_missing_sha_is_named_without_a_pull_request_lookup():
    fake = FakeGitHub({_API: (200, {}), f"{_API}/commits/main": (200, {})})
    with patch(_SAFE_REQUEST, new=fake):
        result = await _compare_tool()(repo="acme/app", base="main", head="deadbeef00")

    assert not result.is_error
    assert result.structured["missing"] == ["deadbeef00"]
    assert result.structured["pull_requests"] == {}
    assert all(url != f"{_API}/pulls" for url, _ in fake.calls)


@pytest.mark.asyncio
async def test_a_missing_repository_still_answers_an_error():
    fake = FakeGitHub({})
    with patch(_SAFE_REQUEST, new=fake):
        result = await _compare_tool()(repo="acme/app", base="main", head="feat")

    assert isinstance(result, str)
    assert result.startswith("Error:")


@pytest.mark.asyncio
async def test_both_refs_present_keeps_the_error():
    fake = FakeGitHub({_API: (200, {}), f"{_API}/commits/main": (200, {}), f"{_API}/commits/feat": (200, {})})
    with patch(_SAFE_REQUEST, new=fake):
        result = await _compare_tool()(repo="acme/app", base="main", head="feat")

    assert result == _NOT_FOUND


@pytest.mark.asyncio
async def test_an_unreadable_ref_keeps_the_error():
    fake = FakeGitHub({_API: (200, {}), f"{_API}/commits/main": (500, {})})
    with patch(_SAFE_REQUEST, new=fake):
        result = await _compare_tool()(repo="acme/app", base="main", head="feat")

    assert result == _NOT_FOUND


@pytest.mark.asyncio
async def test_another_compare_error_is_answered_as_is():
    fake = FakeGitHub({f"{_API}/compare/main...feat": (500, {})})
    with patch(_SAFE_REQUEST, new=fake):
        result = await _compare_tool()(repo="acme/app", base="main", head="feat")

    assert result == "Error: GitHub HTTP 500."
    assert len(fake.calls) == 1
