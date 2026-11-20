"""get_github_job_logs on the id of another app's check run (not an Actions job).

A check run of CircleCI, Codecov or a security scanner has no Actions log: the
jobs endpoint answers 404 for its id and the tool used to hand back a bare
"Not Found". These tests pin the answer that carries the run's own output, its
annotations and its details link, and that an id nothing knows keeps the
original error.
"""

from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest

from app.integrations.http import format_http_error
from app.integrations.providers.github import build_tools
from app.integrations.providers.github.actions import _MAX_CHECK_OUTPUT_CHARS, _app_check_run_answer

_SAFE_REQUEST = "app.integrations.providers.github._common.safe_request"


def _not_found(action: str) -> str:
    """A 404 in the words the real client writes (``format_http_error``), never a made-up wording (#639)."""
    resp = httpx.Response(
        404, json={"message": "Not Found"}, request=httpx.Request("GET", "https://api.github.com/repos/o/r")
    )
    return format_http_error(resp, "GitHub", action)


_LOGS_404 = _not_found("get logs for job 7 in o/r")


def _job_logs_tool() -> Any:
    executor = MagicMock()
    executor.register = MagicMock()
    build_tools("fake-token", executor)
    fns = {c[0][0]: c[0][1] for c in executor.register.call_args_list}
    return fns["get_github_job_logs"]


class _GitHub:
    """No Actions job behind the id; the check-runs endpoint answers ``check``."""

    def __init__(self, *, check: dict[str, Any] | None, annotations: list[Any] | None = None) -> None:
        self.check = check
        self.annotations = annotations
        self.urls: list[str] = []

    async def __call__(
        self, client: Any, method: str, url: str, provider: str, action: str, **kwargs: Any
    ) -> tuple[httpx.Response | None, str | None]:
        self.urls.append(url)
        if url.endswith("/logs"):
            return None, _LOGS_404
        if "/actions/jobs/" in url:
            return None, _not_found("get job 7 in o/r")
        if url.endswith("/annotations"):
            if self.annotations is None:
                return None, "Error: GitHub get annotations failed (500)"
            return httpx.Response(200, json=self.annotations), None
        if self.check is None:
            return None, _not_found("get check run 7 in o/r")
        return httpx.Response(200, json=self.check), None


async def _call(github: _GitHub) -> str:
    tool = _job_logs_tool()
    with patch(_SAFE_REQUEST, new=github):
        result: str = await tool(repo="o/r", job_id=7)
    return result


_CIRCLECI_RUN = {
    "id": 7,
    "name": "ci/circleci: build",
    "status": "completed",
    "conclusion": "failure",
    "details_url": "https://circleci.com/gh/o/r/123",
    "app": {"slug": "circleci-checks", "name": "CircleCI Checks"},
    "output": {"title": "Your tests failed on CircleCI", "summary": "2 tests failed in test_api.py", "text": None},
}


@pytest.mark.asyncio
async def test_another_apps_check_run_answers_its_output_and_details_link() -> None:
    note = {"path": "test_api.py", "start_line": 40, "annotation_level": "failure", "title": "", "message": "boom"}
    github = _GitHub(check=_CIRCLECI_RUN, annotations=[note])
    result = await _call(github)
    assert result.startswith(
        "Check run 7 (ci/circleci: build) in o/r is reported by circleci-checks, not by a GitHub Actions job [failure]"
    )
    assert "Details: https://circleci.com/gh/o/r/123" in result
    assert "Title:\nYour tests failed on CircleCI" in result
    assert "Summary:\n2 tests failed in test_api.py" in result
    assert "Text:" not in result
    assert "  [failure] boom (test_api.py:40)" in result
    assert "Not Found" not in result
    assert github.urls[-2:] == [
        "https://api.github.com/repos/o/r/check-runs/7",
        "https://api.github.com/repos/o/r/check-runs/7/annotations",
    ]


@pytest.mark.asyncio
async def test_an_id_no_endpoint_knows_keeps_the_original_error() -> None:
    assert await _call(_GitHub(check=None)) == _LOGS_404


@pytest.mark.asyncio
async def test_the_workspace_ingest_writes_no_file_for_another_apps_check_run() -> None:
    with patch("app.integrations.providers.github.actions.workspace_ingest_requested", return_value=True):
        result = await _call(_GitHub(check=_CIRCLECI_RUN, annotations=[]))
    assert result.startswith("Error: Check run 7 (ci/circleci: build)")


def test_an_empty_output_says_so_and_unreadable_annotations_are_named() -> None:
    run = {"id": 7, "name": "codecov/patch", "status": "completed", "conclusion": "neutral", "output": {}}
    result = _app_check_run_answer(7, "o/r", run, None)
    assert "reported by another app" in result
    assert "Output: none" in result
    assert result.endswith("Annotations: unreadable (the check-run annotations endpoint answered an error).")


def test_a_long_summary_is_cut() -> None:
    run = {
        "id": 7,
        "name": "scan",
        "app": {"name": "Scanner"},
        "output": {"summary": "x" * (_MAX_CHECK_OUTPUT_CHARS + 50)},
    }
    result = _app_check_run_answer(7, "o/r", run, [])
    assert "reported by Scanner" in result
    assert f"[cut at {_MAX_CHECK_OUTPUT_CHARS:,} characters]" in result
    assert "x" * (_MAX_CHECK_OUTPUT_CHARS + 1) not in result
