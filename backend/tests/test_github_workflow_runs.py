"""list_github_workflow_runs: workflow, date, event and SHA filters, paging, head SHA per row.

"Every failed E2E run since T, with its head SHA" used to be out of the tool's
reach (no workflow filter, at most 30 runs, no page, no SHA): an agent paged raw
GraphQL by hand instead. These tests pin the request each filter sends and the
rows and next-page line the answer carries.
"""

from unittest.mock import MagicMock, patch

import httpx
import pytest

from app.integrations.providers.github import build_tools
from app.integrations.providers.github.actions import _format_workflow_runs

_SAFE_REQUEST = "app.integrations.providers.github._common.safe_request"


def _tools():
    executor = MagicMock()
    executor.register = MagicMock()
    definitions = build_tools("fake-token", executor)
    fns = {c[0][0]: c[0][1] for c in executor.register.call_args_list}
    return fns, {d.name: d for d in definitions}


def _run(run_id: int, *, conclusion: str = "failure", sha: str = "0123456789abcdef") -> dict:
    return {
        "id": run_id,
        "run_number": run_id - 1000,
        "name": "E2E Tests",
        "status": "completed",
        "conclusion": conclusion,
        "event": "push",
        "head_branch": "main",
        "head_sha": sha,
        "created_at": "2026-11-04T21:07:33Z",
        "html_url": f"https://github.com/o/r/actions/runs/{run_id}",
    }


class _GitHub:
    def __init__(self, payload: dict | None = None, error: str | None = None):
        self.payload = payload or {"total_count": 0, "workflow_runs": []}
        self.error = error
        self.calls: list[tuple[str, dict]] = []

    async def __call__(self, client, method, url, provider, action, **kwargs):
        self.calls.append((url, dict(kwargs.get("params") or {})))
        if self.error:
            return None, self.error
        return httpx.Response(200, json=self.payload), None


async def _call(github: _GitHub, **kwargs) -> str:
    fns, _ = _tools()
    with patch(_SAFE_REQUEST, new=github):
        return await fns["list_github_workflow_runs"](repo="o/r", **kwargs)


@pytest.mark.asyncio
async def test_failed_runs_of_one_workflow_since_a_time_in_one_call():
    github = _GitHub({"total_count": 2, "workflow_runs": [_run(1001), _run(1002, sha="fedcba9876543210")]})
    result = await _call(github, workflow="e2e.yml", status="failure", created=">=2026-11-03T00:00:00Z", count=50)
    url, params = github.calls[0]
    assert url.endswith("/repos/o/r/actions/workflows/e2e.yml/runs")
    assert params == {"per_page": 50, "page": 1, "status": "failure", "created": ">=2026-11-03T00:00:00Z"}
    assert result.splitlines()[0] == "Workflow runs of workflow e2e.yml in o/r: 1-2 of 2, newest first:"
    assert "@01234567 (2026-11-04 21:07Z)" in result and "@fedcba98" in result
    assert "(run_id 1001) push on main" in result
    assert "More remain" not in result


@pytest.mark.asyncio
async def test_without_a_workflow_the_repository_endpoint_is_read_and_empty_filters_are_not_sent():
    github = _GitHub()
    result = await _call(github, branch="main", event=" ", head_sha="abc")
    url, params = github.calls[0]
    assert url.endswith("/repos/o/r/actions/runs")
    assert params == {"per_page": 15, "page": 1, "branch": "main", "head_sha": "abc"}
    assert result == "No workflow runs found for o/r."


@pytest.mark.asyncio
async def test_a_workflow_id_and_a_name_with_odd_characters_are_quoted_in_the_path():
    github = _GitHub()
    await _call(github, workflow="123456")
    await _call(github, workflow="ci/e2e.yml")
    assert github.calls[0][0].endswith("/actions/workflows/123456/runs")
    assert github.calls[1][0].endswith("/actions/workflows/ci%2Fe2e.yml/runs")


@pytest.mark.asyncio
async def test_count_is_capped_at_100_and_page_floored_at_1():
    github = _GitHub()
    await _call(github, count=500, page=0)
    assert github.calls[0][1]["per_page"] == 100 and github.calls[0][1]["page"] == 1


@pytest.mark.asyncio
async def test_the_answer_names_the_next_page_while_runs_remain():
    github = _GitHub({"total_count": 7, "workflow_runs": [_run(1003), _run(1004)]})
    result = await _call(github, workflow="e2e.yml", count=2, page=2)
    assert github.calls[0][1]["page"] == 2
    assert "3-4 of 7" in result.splitlines()[0]
    assert result.splitlines()[-1] == "More remain: call again with page=3."


def test_the_last_page_has_no_next_page_line_and_a_page_past_the_end_says_so():
    last = _format_workflow_runs({"total_count": 5, "workflow_runs": [_run(1005)]}, scope="o/r", page=3, per_page=2)
    assert "5-5 of 5" in last and "More remain" not in last
    past = _format_workflow_runs({"total_count": 5, "workflow_runs": []}, scope="o/r", page=9, per_page=2)
    assert past == "No workflow runs of o/r on page 9 (5 total)."


def test_a_run_missing_fields_still_renders_one_line():
    result = _format_workflow_runs(
        {"total_count": 1, "workflow_runs": [{"id": 1, "status": "queued"}]}, scope="o/r", page=1, per_page=15
    )
    assert "[queued] (run_id 1) ? on ? @? (?)" in result


@pytest.mark.asyncio
async def test_an_api_error_is_returned_unchanged():
    error = "Error: GitHub list workflow runs failed (404): Not Found"
    assert await _call(_GitHub(error=error), workflow="missing.yml") == error


def test_the_schema_offers_the_new_filters():
    _, definitions = _tools()
    properties = definitions["list_github_workflow_runs"].input_schema["properties"]
    assert {"workflow", "created", "event", "head_sha", "page"} <= set(properties)
    assert properties["count"]["description"] == "Runs per page (max 100)"
