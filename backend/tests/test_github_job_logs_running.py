"""get_github_job_logs on a job that has not completed.

GitHub publishes a job's log only once the job completes: before that, the
logs endpoint redirects to a blob that does not exist and the tool used to hand
the storage error (``BlobNotFound``) to the agent, which read it as a failure.
These tests pin the "still running" answer for queued and in-progress jobs,
the steps and check-run annotations of a completed job whose log was never
published (a lost runner), and that any other error keeps its original text.
"""

from unittest.mock import MagicMock, patch

import httpx
import pytest

from app.integrations.providers.github import build_tools
from app.integrations.providers.github.actions import _log_is_missing, _running_job_answer

_SAFE_REQUEST = "app.integrations.providers.github._common.safe_request"
_BLOB_ERROR = "Error: GitHub get logs for job 9 in o/r failed (404): <Code>BlobNotFound</Code>"


def _job_logs_tool():
    executor = MagicMock()
    executor.register = MagicMock()
    build_tools("fake-token", executor)
    fns = {c[0][0]: c[0][1] for c in executor.register.call_args_list}
    return fns["get_github_job_logs"]


class _GitHub:
    """The logs endpoint answers ``logs`` (text or an error); the job endpoint answers ``job``."""

    def __init__(
        self,
        *,
        logs: str | None = None,
        logs_error: str | None = None,
        job: dict | None = None,
        annotations: list | None = None,
    ):
        self.logs = logs
        self.logs_error = logs_error
        self.job = job
        self.annotations = annotations
        self.urls: list[str] = []

    async def __call__(self, client, method, url, provider, action, **kwargs):
        self.urls.append(url)
        if url.endswith("/logs"):
            if self.logs_error:
                return None, self.logs_error
            return httpx.Response(200, text=self.logs or ""), None
        if url.endswith("/annotations"):
            if self.annotations is None:
                return None, "Error: GitHub get annotations failed (500)"
            return httpx.Response(200, json=self.annotations), None
        if self.job is None:
            return None, "Error: GitHub get job failed (404)"
        return httpx.Response(200, json=self.job), None


async def _call(github: _GitHub, **kwargs) -> str:
    tool = _job_logs_tool()
    with patch(_SAFE_REQUEST, new=github):
        return await tool(repo="o/r", job_id=9, **kwargs)


@pytest.mark.asyncio
@pytest.mark.parametrize("failures_only", [False, True])
async def test_an_in_progress_job_says_it_is_running_instead_of_the_blob_error(failures_only):
    job = {"status": "in_progress", "name": "Backend (Python) 1/4", "started_at": "2026-11-01T02:00:00Z"}
    result = await _call(_GitHub(logs_error=_BLOB_ERROR, job=job), failures_only=failures_only)
    assert result.startswith("Error: job 9 (Backend (Python) 1/4) in o/r is in_progress, started 2026-11-01T02:00:00Z")
    assert "once it completes" in result and "get_github_ci_status" in result
    assert "BlobNotFound" not in result


@pytest.mark.asyncio
async def test_a_queued_job_with_an_empty_log_says_it_is_queued():
    result = await _call(_GitHub(logs="", job={"status": "queued", "name": "E2E core 1/6"}))
    assert result.startswith("Error: job 9 (E2E core 1/6) in o/r is queued:")
    assert "started" not in result


_LOST_RUNNER_JOB = {
    "status": "completed",
    "conclusion": "failure",
    "name": "Analyze (python)",
    "steps": [
        {"number": 1, "name": "Set up job", "status": "completed", "conclusion": "success"},
        {"number": 2, "name": "Perform CodeQL Analysis", "status": "in_progress", "conclusion": None},
    ],
}
_LOST_RUNNER_NOTE = {
    "path": ".github",
    "start_line": 1,
    "annotation_level": "failure",
    "title": "",
    "message": "The hosted runner lost communication with the server.",
}


@pytest.mark.asyncio
@pytest.mark.parametrize("failures_only", [False, True])
async def test_a_completed_job_without_a_published_log_answers_its_steps_and_annotations(failures_only):
    github = _GitHub(logs_error=_BLOB_ERROR, job=_LOST_RUNNER_JOB, annotations=[_LOST_RUNNER_NOTE])
    result = await _call(github, failures_only=failures_only)
    assert not result.startswith("Error")
    assert result.startswith("Job 9 (Analyze (python)) in o/r completed [failure] but GitHub never published its log")
    assert "  2. Perform CodeQL Analysis [in_progress]" in result
    assert "  [failure] The hosted runner lost communication with the server." in result
    assert "BlobNotFound" not in result
    assert github.urls[-1] == "https://api.github.com/repos/o/r/check-runs/9/annotations"


@pytest.mark.asyncio
async def test_an_annotation_with_a_file_names_its_location():
    note = {"path": "src/a.py", "start_line": 12, "annotation_level": "warning", "title": "Lint", "message": "x"}
    result = await _call(_GitHub(logs_error=_BLOB_ERROR, job=_LOST_RUNNER_JOB, annotations=[note]))
    assert "  [warning] Lint: x (src/a.py:12)" in result


@pytest.mark.asyncio
async def test_unreadable_annotations_still_answer_the_steps():
    result = await _call(_GitHub(logs_error=_BLOB_ERROR, job=_LOST_RUNNER_JOB, annotations=None))
    assert "never published its log" in result and "Perform CodeQL Analysis" in result
    assert "Annotations: unreadable" in result


@pytest.mark.asyncio
async def test_a_completed_job_with_an_empty_log_answers_no_annotations():
    result = await _call(_GitHub(logs="", job={"status": "completed", "conclusion": "cancelled"}, annotations=[]))
    assert result.startswith("Job 9 (unnamed) in o/r completed [cancelled]")
    assert result.endswith("Annotations: none.")


@pytest.mark.asyncio
async def test_the_workspace_ingest_writes_no_file_for_an_unpublished_log():
    github = _GitHub(logs_error=_BLOB_ERROR, job=_LOST_RUNNER_JOB, annotations=[_LOST_RUNNER_NOTE])
    with patch("app.integrations.providers.github.actions.workspace_ingest_requested", return_value=True):
        result = await _call(github)
    assert result.startswith("Error: Job 9 (Analyze (python))")


@pytest.mark.asyncio
async def test_a_completed_job_keeps_any_other_error():
    denied = "Error: GitHub get logs for job 9 in o/r failed (403): Resource not accessible by integration"
    github = _GitHub(logs_error=denied, job={"status": "completed", "conclusion": "failure", "name": "Lint"})
    assert await _call(github) == denied
    assert len(github.urls) == 2


def test_a_missing_log_is_a_404_or_the_storage_blob_error():
    assert _log_is_missing(_BLOB_ERROR)
    assert _log_is_missing("Error: GitHub get logs failed (404): Not Found")
    assert not _log_is_missing("Error: GitHub get logs failed (500): boom")
    assert not _log_is_missing(None)


@pytest.mark.asyncio
async def test_an_unreadable_job_keeps_the_original_error():
    assert await _call(_GitHub(logs_error=_BLOB_ERROR, job=None)) == _BLOB_ERROR


@pytest.mark.asyncio
async def test_a_log_that_exists_is_read_without_reading_the_job():
    github = _GitHub(logs="##[group]Run ruff\nAll checks passed\n", job={"status": "in_progress"})
    result = await _call(github)
    assert result.startswith("Logs for job 9 in o/r:") and "All checks passed" in result
    assert github.urls == ["https://api.github.com/repos/o/r/actions/jobs/9/logs"]


def test_the_answer_is_none_for_a_completed_or_unknown_job():
    assert _running_job_answer(9, "o/r", {"status": "completed"}) is None
    assert _running_job_answer(9, "o/r", {}) is None
    assert _running_job_answer(9, "o/r", {"status": "waiting"}).startswith("Error: job 9 (unnamed) in o/r is waiting:")
