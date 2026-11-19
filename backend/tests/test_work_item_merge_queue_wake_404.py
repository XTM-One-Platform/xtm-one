"""A green pull request waiting in the merge queue is a wake still to come (#404).

The inbox run's end put a ready, green item back in the queue while its pull
request sat in the base's merge queue (``merge_github_pr`` enqueued it): the
next run had nothing to do. The live read now says ``merge_queue`` and the
settlement parks the item on the merge, like a running full CI run (#12040).
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from typing import Any

import pytest

import app.services.work_item as wi_service
from app.integrations.providers.github import reference_state as rs
from tests.test_work_item_double_dispatch_12040 import _item, settle  # noqa: F401  (fixture)

REPO = "owner/name"
SHA = "64e5994ca07d830521e2e7e76279c0e2856d732b"


@pytest.mark.asyncio
async def test_a_green_head_in_the_merge_queue_is_parked_not_requeued(settle) -> None:  # noqa: F811
    settle.live({"head_sha": "abc", "conclusion": "success", "draft": False, "merge_queue": True})
    item = _item()
    out = await settle(item)
    assert out == "parked"
    assert item.status == "blocked"
    assert [(r["number"], r["release_on"]) for r in item.blocked_on_refs] == [(12, "merged_or_closed")]
    assert not any("back in the queue" in n["text"] for n in item.claim_notes)


@pytest.mark.asyncio
async def test_a_green_head_out_of_the_merge_queue_still_goes_back_to_the_queue(settle) -> None:  # noqa: F811
    settle.live({"head_sha": "abc", "conclusion": "success", "draft": False})
    item = _item()
    assert await settle(item) == "deferred"
    assert item.status == "open"


def test_merge_queued_reads_only_a_true_flag() -> None:
    assert wi_service._merge_queued({"merge_queue": True})
    for read in ({"merge_queue": False}, {}, None, "merge_queue"):
        assert not wi_service._merge_queued(read)


class _Resp:
    def __init__(self, data: Any) -> None:
        self._data = data

    def json(self) -> Any:
        return self._data


def _fake_github(monkeypatch: pytest.MonkeyPatch, *, draft: bool, queued: Any) -> list[str]:
    pull = {"state": "open", "draft": draft, "head": {"sha": SHA, "ref": "fix/x"}, "base": {"ref": "main"}}
    checks = {"total_count": 1, "check_runs": [{"name": "Backend", "status": "completed", "conclusion": "success"}]}
    asked: list[str] = []

    async def fake_request(_client: object, _method: str, url: str, *_a: object, **_k: object) -> tuple[Any, Any]:
        if url.endswith("/pulls/7"):
            return _Resp(pull), None
        if "/check-runs" in url:
            return _Resp(checks), None
        if url.endswith("/actions/runs"):
            return None, "unreadable"
        if "/rules/branches/" in url:
            return _Resp([]), None
        return None, "unexpected"

    async def fake_graphql(_client: object, _headers: object, query: str, variables: Any = None, **_k: object):
        asked.append(query)
        if isinstance(queued, str):
            return None, queued
        return {"repository": {"pullRequest": {"isInMergeQueue": queued}}}, None

    async def no_gate(*_a: object, **_k: object) -> None:
        return None

    @contextlib.asynccontextmanager
    async def fake_client(**_k: object) -> AsyncIterator[object]:
        yield object()

    monkeypatch.setattr("app.integrations.providers.github._common.safe_request", fake_request)
    monkeypatch.setattr("app.integrations.providers.github._common.gh_graphql", fake_graphql)
    monkeypatch.setattr(rs, "_full_ci_state", no_gate)
    monkeypatch.setattr(rs, "egress_client", fake_client)
    return asked


@pytest.mark.asyncio
async def test_the_live_read_says_merge_queue_for_a_queued_green_ready_head(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_github(monkeypatch, draft=False, queued=True)
    state = await rs.read_pull_request_ci(REPO, 7, "token")
    assert state is not None and state["conclusion"] == "success"
    assert state["merge_queue"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("queued", [False, None, "Error: GitHub GraphQL error: boom"])
async def test_no_merge_queue_flag_when_out_of_the_queue_or_unreadable(monkeypatch: pytest.MonkeyPatch, queued) -> None:
    _fake_github(monkeypatch, draft=False, queued=queued)
    state = await rs.read_pull_request_ci(REPO, 7, "token")
    assert state is not None and "merge_queue" not in state


@pytest.mark.asyncio
async def test_a_draft_is_never_asked_about_the_merge_queue(monkeypatch: pytest.MonkeyPatch) -> None:
    asked = _fake_github(monkeypatch, draft=True, queued=True)
    state = await rs.read_pull_request_ci(REPO, 7, "token")
    assert state is not None and "merge_queue" not in state
    assert asked == []
