"""A workflow run that concluded without any job is classified ``no_jobs``, never red (#11490).

GitHub can conclude a run ``failure`` without ever assigning it a runner (an
Actions incident: 9 Nov 2026 18:09-18:28Z, five workflows of xtm-one and two of
OpenCTI at once, ``checkSuite.checkRuns.totalCount`` 0). The run has no job, no
log and nothing in the code to fix: the tools say so, a re-run of it is whole
and happens once, and the event enrichment lets a red-check filter leave it out.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import pytest

from app.integrations.providers.github import build_tools
from app.integrations.providers.github import event_enrichment as gh_enrichment
from app.integrations.providers.github.actions import no_jobs_run

REPO = "octo/repo"
SHA = "d" * 40


def _fn(name: str):
    executor = MagicMock()
    build_tools("fake-token", executor)
    for call in executor.register.call_args_list:
        if call[0][0] == name:
            return call[0][1]
    raise KeyError(name)


def _ok(payload) -> httpx.Response:
    return httpx.Response(200, json=payload)


def _run(conclusion: str = "failure", attempt: int = 1) -> dict:
    return {"id": 42, "status": "completed", "conclusion": conclusion, "run_attempt": attempt}


def _github(run: dict, jobs: list[dict], calls: list[tuple[str, str]]):
    async def fake_request(client, method, url, provider, action, **kwargs):
        calls.append((method, url))
        if method == "post":
            return _ok({}), None
        if url.endswith("/runs/42/jobs"):
            return _ok({"total_count": len(jobs), "jobs": jobs}), None
        if url.endswith("/runs/42"):
            return _ok(run), None
        return None, f"Error: unexpected {url}"

    return fake_request


@pytest.mark.parametrize(
    ("run", "jobs_total", "expected"),
    [
        (_run(), 0, True),
        (_run("cancelled"), 0, True),
        (_run("startup_failure"), 0, True),
        (_run(), 3, False),
        (_run("success"), 0, False),
        ({"id": 42, "status": "queued", "conclusion": None}, 0, False),
    ],
)
def test_no_jobs_classification(run, jobs_total, expected):
    assert no_jobs_run(run, jobs_total) is expected


@pytest.mark.asyncio
async def test_list_jobs_names_a_run_without_jobs_as_no_jobs_and_its_one_rerun():
    calls: list[tuple[str, str]] = []
    with patch("app.integrations.providers.github._common.safe_request", side_effect=_github(_run(), [], calls)):
        result = await _fn("list_github_workflow_jobs")(repo=REPO, run_id=42)

    assert "classified no_jobs" in result
    assert "not a test or workflow failure" in result
    assert "action='rerun'" in result
    assert "No jobs found" not in result


@pytest.mark.asyncio
async def test_list_jobs_of_a_rerun_without_jobs_says_not_to_rerun_again():
    calls: list[tuple[str, str]] = []
    with patch(
        "app.integrations.providers.github._common.safe_request", side_effect=_github(_run(attempt=2), [], calls)
    ):
        result = await _fn("list_github_workflow_jobs")(repo=REPO, run_id=42)

    assert "classified no_jobs" in result
    assert "do not re-run it again" in result


@pytest.mark.asyncio
async def test_list_jobs_of_a_queued_run_without_jobs_keeps_the_plain_answer():
    calls: list[tuple[str, str]] = []
    queued = {"id": 42, "status": "queued", "conclusion": None, "run_attempt": 1}
    with patch("app.integrations.providers.github._common.safe_request", side_effect=_github(queued, [], calls)):
        result = await _fn("list_github_workflow_jobs")(repo=REPO, run_id=42)

    assert result == f"No jobs found for run 42 in {REPO}."


@pytest.mark.asyncio
async def test_list_jobs_of_a_fresh_rerun_without_jobs_names_the_previous_attempt_failures():
    """Attempt 2 has not queued its jobs: the failed jobs of attempt 1 are named (#355)."""
    calls: list[tuple[str, str]] = []
    running = {"id": 42, "status": "queued", "conclusion": None, "run_attempt": 2}
    failed = [{"id": 7, "name": "Backend unit", "status": "completed", "conclusion": "failure"}]
    base = _github(running, [], calls)

    async def fake_request(client, method, url, provider, action, **kwargs):
        if url.endswith("/runs/42/attempts/1/jobs"):
            calls.append((method, url))
            return _ok({"total_count": 1, "jobs": failed}), None
        return await base(client, method, url, provider, action, **kwargs)

    with patch("app.integrations.providers.github._common.safe_request", side_effect=fake_request):
        result = await _fn("list_github_workflow_jobs")(repo=REPO, run_id=42)

    assert "No jobs yet for run 42 attempt 2 (latest)" in result
    assert "Attempt 1 (the latest is still running) ended with: Backend unit [failure] (job_id 7)" in result
    assert "No jobs found" not in result


@pytest.mark.asyncio
async def test_list_jobs_of_a_fresh_rerun_without_a_previous_failure_points_to_it():
    calls: list[tuple[str, str]] = []
    running = {"id": 42, "status": "queued", "conclusion": None, "run_attempt": 2}
    base = _github(running, [], calls)

    async def fake_request(client, method, url, provider, action, **kwargs):
        if url.endswith("/runs/42/attempts/1/jobs"):
            return _ok({"total_count": 0, "jobs": []}), None
        return await base(client, method, url, provider, action, **kwargs)

    with patch("app.integrations.providers.github._common.safe_request", side_effect=fake_request):
        result = await _fn("list_github_workflow_jobs")(repo=REPO, run_id=42)

    assert "No jobs yet for run 42 attempt 2 (latest)" in result
    assert "list_github_workflow_jobs(run_id=42, attempt=1)" in result


@pytest.mark.asyncio
async def test_rerun_failed_of_a_no_jobs_run_is_a_whole_rerun():
    calls: list[tuple[str, str]] = []
    with patch("app.integrations.providers.github._common.safe_request", side_effect=_github(_run(), [], calls)):
        result = await _fn("manage_github_workflow_run")(repo=REPO, run_id=42, action="rerun_failed")

    posts = [url for method, url in calls if method == "post"]
    assert posts == [f"https://api.github.com/repos/{REPO}/actions/runs/42/rerun"]
    assert "classified no_jobs" in result


@pytest.mark.asyncio
async def test_a_no_jobs_run_is_rerun_once_only():
    calls: list[tuple[str, str]] = []
    with patch(
        "app.integrations.providers.github._common.safe_request", side_effect=_github(_run(attempt=2), [], calls)
    ):
        result = await _fn("manage_github_workflow_run")(repo=REPO, run_id=42, action="rerun")

    assert result.startswith("Error: ")
    assert "do not re-run it again" in result
    assert not [url for method, url in calls if method == "post"]


@pytest.mark.asyncio
async def test_rerun_failed_of_a_run_with_jobs_is_unchanged():
    calls: list[tuple[str, str]] = []
    jobs = [{"id": 1, "name": "Lint", "status": "completed", "conclusion": "failure"}]
    with patch("app.integrations.providers.github._common.safe_request", side_effect=_github(_run(), jobs, calls)):
        result = await _fn("manage_github_workflow_run")(repo=REPO, run_id=42, action="rerun_failed")

    posts = [url for method, url in calls if method == "post"]
    assert posts == [f"https://api.github.com/repos/{REPO}/actions/runs/42/rerun-failed-jobs"]
    assert "no_jobs" not in result


@pytest.mark.asyncio
async def test_cancel_reads_nothing_first():
    calls: list[tuple[str, str]] = []
    with patch("app.integrations.providers.github._common.safe_request", side_effect=_github(_run(), [], calls)):
        await _fn("manage_github_workflow_run")(repo=REPO, run_id=42, action="cancel")

    assert [method for method, _url in calls] == ["post"]


# --- event enrichment: enriched.run -------------------------------------------------------------


def _delivery(conclusion: str = "failure", attempt: int = 1) -> dict:
    return {
        "action": "completed",
        "workflow_run": {
            "id": 42,
            "run_attempt": attempt,
            "status": "completed",
            "conclusion": conclusion,
            "head_sha": SHA,
            "pull_requests": [],
        },
        "repository": {"full_name": REPO},
    }


def test_the_run_facet_follows_the_filter_keys():
    assert gh_enrichment.facets("workflow_run.completed", _delivery(), ("enriched.run.no_jobs",)) == frozenset({"run"})
    assert gh_enrichment.facets("workflow_run.completed", _delivery(), ("enriched.pull_request.number",)) == frozenset()
    check_run = {"check_run": {"head_sha": SHA}, "repository": {"full_name": REPO}}
    assert gh_enrichment.facets("check_run.completed", check_run, ("enriched.run.no_jobs",)) == frozenset()


class _Resp:
    def __init__(self, body):
        self._body = body

    def json(self):
        return self._body


def _enrichment_github(monkeypatch, jobs_total: int) -> list[str]:
    calls: list[str] = []

    async def _request(_client, _method, url, *_a, **_k):
        calls.append(url)
        if url.endswith("/pulls"):
            return _Resp([]), None
        if url.endswith("/runs/42/jobs"):
            return _Resp({"total_count": jobs_total, "jobs": []}), None
        return None, f"unexpected {url}"

    monkeypatch.setattr(gh_enrichment._common, "safe_request", _request)
    return calls


@pytest.mark.asyncio
async def test_a_failed_run_without_jobs_reads_no_jobs(monkeypatch):
    _enrichment_github(monkeypatch, jobs_total=0)
    facts = await gh_enrichment.fetch("workflow_run.completed", _delivery(), "tok", facets=frozenset({"run"}))
    assert facts["run"] == {"id": 42, "attempt": 1, "jobs": 0, "no_jobs": True}


@pytest.mark.asyncio
async def test_a_failed_run_with_jobs_is_a_real_red(monkeypatch):
    _enrichment_github(monkeypatch, jobs_total=4)
    facts = await gh_enrichment.fetch("workflow_run.completed", _delivery(), "tok", facets=frozenset({"run"}))
    assert facts["run"]["no_jobs"] is False
    assert facts["run"]["jobs"] == 4


@pytest.mark.asyncio
async def test_a_green_run_reads_no_jobs_false_without_a_jobs_read(monkeypatch):
    calls = _enrichment_github(monkeypatch, jobs_total=0)
    facts = await gh_enrichment.fetch("workflow_run.completed", _delivery("success"), "tok", facets=frozenset({"run"}))
    assert facts["run"] == {"id": 42, "attempt": 1, "jobs": None, "no_jobs": False}
    assert not any(url.endswith("/jobs") for url in calls)


@pytest.mark.asyncio
async def test_a_filter_without_the_run_facet_reads_no_jobs(monkeypatch):
    calls = _enrichment_github(monkeypatch, jobs_total=0)
    facts = await gh_enrichment.fetch("workflow_run.completed", _delivery(), "tok", facets=frozenset())
    assert "run" not in facts
    assert not any(url.endswith("/jobs") for url in calls)
