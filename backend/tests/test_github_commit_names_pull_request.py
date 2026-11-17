"""A commit pushed to an open pull request names it on the run's work item (#9819).

An inbox run that pushed to a pull request opened under another item, then
ended on its pending CI, left an item naming no pull request: the run end
released it, and at the dispatch cap it was blocked "No release condition"
(work_item.release_exhausted). The GitHub commit tools now name the open pull
request of the branch on the dispatched item, so the run end parks the item on
it (``settle_inbox_work_item`` keeps an item naming an open pull request).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

import app.services.work_item as wi_service
from app.integrations.providers.github import build_tools
from app.services.integration_tools import _make_record_push_fn
from app.services.work_item_pull_requests import bind_pull_request


def _fn(executor: MagicMock, name: str):
    for call in executor.register.call_args_list:
        if call[0][0] == name:
            return call[0][1]
    raise KeyError(name)


def _ok(payload) -> httpx.Response:
    return httpx.Response(200, json=payload)


def _file_tool(record_push):
    executor = MagicMock()
    executor.register = MagicMock()
    build_tools("fake-token", executor, record_push=record_push)
    return _fn(executor, "create_or_update_github_file")


_TIP = "a" * 40
_SHA = "9e2f9b46d4fdf32d4dc268ac5c1dedb690f7249f"


def _write_responses(pulls: object) -> list[tuple[httpx.Response, None]]:
    """The requests of one signed per-file commit (#12051), then the open pull request of the branch."""
    commit = {"oid": _SHA, "signature": {"isValid": True, "state": "VALID"}}
    return [
        (_ok({"object": {"sha": _TIP, "type": "commit"}}), None),
        (_ok({"sha": _TIP, "tree": {"sha": "b" * 40}}), None),
        # The root listing: a.txt is a new file.
        (_ok({"sha": "b" * 40, "tree": [], "truncated": False}), None),
        (_ok({"sha": "c" * 40}), None),
        (_ok({"data": {"createCommitOnBranch": {"commit": commit}}}), None),
        (_ok(pulls), None),
    ]


@pytest.mark.asyncio
async def test_a_push_to_a_branch_with_an_open_pull_request_names_it():
    record_push = AsyncMock(return_value="Named on work item W: it is dispatched to its owner again")
    write = _file_tool(record_push)
    pulls = [{"number": 9777, "html_url": "https://github.com/o/r/pull/9777"}]
    with patch("app.integrations.providers.github._common.safe_request", new_callable=AsyncMock) as request:
        request.side_effect = _write_responses(pulls)
        result = await write(repo="o/r", path="a.txt", content="x", message="fix: a", branch="feat/x")
    record_push.assert_awaited_once_with("o/r", 9777, "feat/x", _SHA, "https://github.com/o/r/pull/9777", None)
    assert request.call_args_list[-1][1]["params"] == {"head": "o:feat/x", "state": "open", "per_page": "1"}
    assert "Named on work item W" in result


@pytest.mark.asyncio
async def test_a_push_to_a_branch_with_no_open_pull_request_names_nothing():
    record_push = AsyncMock()
    write = _file_tool(record_push)
    with patch("app.integrations.providers.github._common.safe_request", new_callable=AsyncMock) as request:
        request.side_effect = _write_responses([])
        result = await write(repo="o/r", path="a.txt", content="x", message="fix: a", branch="feat/x")
    record_push.assert_not_awaited()
    assert result.startswith("Created a.txt on branch feat/x")


@pytest.mark.asyncio
async def test_without_a_dispatched_item_nothing_is_named():
    # #12067: the open pull request is still read, to start its gated full CI on the
    # new head, but with no dispatched item nothing is named.
    write = _file_tool(None)
    with patch("app.integrations.providers.github._common.safe_request", new_callable=AsyncMock) as request:
        request.side_effect = _write_responses([{"number": 1}])
        result = await write(repo="o/r", path="a.txt", content="x", message="fix: a", branch="feat/x")
    assert request.call_count == 6
    assert request.call_args_list[-1][1]["params"] == {"head": "o:feat/x", "state": "open", "per_page": "1"}
    assert "Named" not in result


@pytest.mark.asyncio
async def test_a_failed_naming_never_fails_the_commit():
    record_push = AsyncMock(side_effect=RuntimeError("database down"))
    write = _file_tool(record_push)
    with patch("app.integrations.providers.github._common.safe_request", new_callable=AsyncMock) as request:
        request.side_effect = _write_responses([{"number": 5}])
        result = await write(repo="o/r", path="a.txt", content="x", message="fix: a", branch="feat/x")
    assert result.startswith("Created a.txt on branch feat/x")


@pytest.mark.asyncio
async def test_record_push_names_the_dispatched_item_only():
    record = AsyncMock(return_value="ok")
    assert _make_record_push_fn({}, record) is None
    assert _make_record_push_fn({"work_item_id": "W"}, None) is None
    push = _make_record_push_fn({"work_item_id": "W"}, record)
    assert await push("o/r", 9777, "feat/x", "abc", "u", True) == "ok"
    record.assert_awaited_once_with("W", "o/r", 9777, "feat/x", "abc", "u", True)


@pytest.mark.asyncio
async def test_a_push_to_a_draft_pull_request_names_it_draft():
    # #10324: the run end never parks an item on the merge of a draft.
    record_push = AsyncMock(return_value="Named")
    write = _file_tool(record_push)
    pulls = [{"number": 9777, "html_url": "u", "draft": True}]
    with patch("app.integrations.providers.github._common.safe_request", new_callable=AsyncMock) as request:
        request.side_effect = _write_responses(pulls)
        await write(repo="o/r", path="a.txt", content="x", message="fix: a", branch="feat/x")
    assert record_push.await_args.args[-1] is True


def test_an_item_naming_the_pushed_pull_request_is_waiting_on_it():
    """The binding the push writes is what the run end parks the item on."""
    payload = bind_pull_request({}, repo="O/R", number=9777, head_branch="feat/x", head_sha="abc")
    item = SimpleNamespace(payload=payload)
    assert [(b["repo"], b["number"]) for b in wi_service.open_pull_requests(item)] == [("o/r", 9777)]
