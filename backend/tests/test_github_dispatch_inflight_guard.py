"""dispatch_github_workflow never cancels a run in progress by accident (#11850).

A workflow whose concurrency group cancels in-progress runs lost run #25298 at
22/23 jobs green to a second dispatch on the same ref: the tool now answers the
run in progress unless ``cancel_in_progress`` is passed.
"""

from unittest.mock import MagicMock, patch

import httpx
import pytest

from app.integrations.providers.github import build_tools

_SAFE_REQUEST = "app.integrations.providers.github._common.safe_request"

_RUN = {
    "id": 25298,
    "run_number": 812,
    "event": "workflow_dispatch",
    "status": "in_progress",
    "head_sha": "b4e2b45a6872aaaabbbbcccc",
    "run_started_at": "2026-11-14T15:00:00Z",
    "html_url": "https://github.com/o/r/actions/runs/25298",
}
_JOBS = [
    {"status": "completed", "conclusion": "success"},
    {"status": "completed", "conclusion": "skipped"},
    {"status": "in_progress", "conclusion": None},
]


def _dispatch_tool():
    executor = MagicMock()
    executor.register = MagicMock()
    build_tools("fake-token", executor)
    fns = {c[0][0]: c[0][1] for c in executor.register.call_args_list}
    return fns["dispatch_github_workflow"]


class _GitHub:
    def __init__(self, runs=None, runs_error=None):
        self.runs = runs if runs is not None else []
        self.runs_error = runs_error
        self.calls: list[tuple[str, str, dict]] = []

    async def __call__(self, client, method, url, provider, action, **kwargs):
        self.calls.append((method, url, kwargs.get("params") or {}))
        if url.endswith("/dispatches"):
            return httpx.Response(204), None
        if url.endswith("/jobs"):
            return httpx.Response(200, json={"jobs": _JOBS}), None
        if self.runs_error:
            return None, self.runs_error
        return httpx.Response(200, json={"workflow_runs": self.runs}), None

    def posts(self) -> int:
        return sum(1 for method, url, _ in self.calls if method == "post" and url.endswith("/dispatches"))


@pytest.mark.asyncio
async def test_a_run_in_progress_is_answered_and_nothing_is_dispatched():
    github = _GitHub(runs=[_RUN])
    with patch(_SAFE_REQUEST, new=github):
        answer = await _dispatch_tool()(repo="o/r", workflow_id="ci.yml", ref="main")
    assert github.posts() == 0
    assert answer.startswith("Not dispatched")
    assert "run 25298" in answer and "b4e2b45a6872" in answer
    assert "2/3 jobs completed (2 passed)" in answer
    assert "cancel_in_progress=true" in answer
    runs_read = [params for method, url, params in github.calls if url.endswith("/ci.yml/runs")]
    assert runs_read == [{"branch": "main", "per_page": 20}]


@pytest.mark.asyncio
async def test_the_flag_dispatches_and_names_the_run_it_may_cancel():
    github = _GitHub(runs=[_RUN])
    with patch(_SAFE_REQUEST, new=github):
        answer = await _dispatch_tool()(repo="o/r", workflow_id="ci.yml", ref="main", cancel_in_progress=True)
    assert github.posts() == 1
    assert answer.startswith("Dispatched")
    assert "may cancel" in answer and "run 25298" in answer


@pytest.mark.asyncio
async def test_completed_runs_do_not_hold_the_dispatch():
    github = _GitHub(runs=[{**_RUN, "status": "completed", "conclusion": "success"}])
    with patch(_SAFE_REQUEST, new=github):
        answer = await _dispatch_tool()(repo="o/r", workflow_id="ci.yml", ref="main")
    assert github.posts() == 1
    assert "may cancel" not in answer


@pytest.mark.asyncio
async def test_an_unreadable_run_list_never_blocks_the_dispatch():
    github = _GitHub(runs_error="Error: GitHub API 500")
    with patch(_SAFE_REQUEST, new=github):
        answer = await _dispatch_tool()(repo="o/r", workflow_id="ci.yml", ref="main")
    assert github.posts() == 1
    assert "not checked" in answer
