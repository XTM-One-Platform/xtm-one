"""get_github_job_logs(patch=true): the unified diff a CI job printed, ready for sandbox_apply_patch (#1477).

A formatter or codegen check prints the diff it wants, every line behind the runner's
timestamp. The extractor gives it back as git apply takes it: no timestamp, no colour,
no clipped line, and nothing after the last complete hunk.
"""

from unittest.mock import MagicMock, patch

import httpx
import pytest

from app.integrations import ci_logs
from app.integrations.providers.github import build_tools

_TS = "2026-11-23T10:41:02.1234567Z "

_DIFF = [
    "diff --git a/backend/app/x.py b/backend/app/x.py",
    "index 1111111..2222222 100644",
    "--- a/backend/app/x.py",
    "+++ b/backend/app/x.py",
    "@@ -1,4 +1,4 @@",
    " import os",
    "",
    "-x = {'a':1}",
    "+x = {'a': 1}",
    " y = 2",
]


def _log(*blocks: list[str]) -> str:
    lines = ["##[group]Run ruff format --diff .", "##[endgroup]"]
    for block in blocks:
        lines += block
    lines += ["1 file would be reformatted", "##[error]Process completed with exit code 1."]
    return "\n".join(_TS + line for line in lines) + "\n"


def test_a_timestamped_git_diff_comes_back_byte_exact() -> None:
    out = ci_logs.extract_patch(_log(_DIFF))
    expected = [line if line else " " for line in _DIFF]
    assert out == "\n".join(expected) + "\n"
    assert "##[error]" not in out and "reformatted" not in out
    assert "2026-11-23T" not in out


def test_two_files_are_both_kept_and_counted() -> None:
    second = [
        "--- a/backend/app/y.py",
        "+++ b/backend/app/y.py",
        "@@ -3 +3 @@",
        "-z=1",
        "+z = 1",
    ]
    out = ci_logs.extract_patch(_log(_DIFF, ["would reformat y.py"], second))
    assert out is not None
    assert ci_logs.patch_file_count(out) == 2
    assert "would reformat" not in out


def test_colours_are_removed_and_long_lines_are_never_clipped() -> None:
    long_line = "+" + "a" * (ci_logs.MAX_LINE_CHARS * 3)
    block = ["--- a/f.txt", "+++ b/f.txt", "@@ -1 +1 @@", "\x1b[31m-old\x1b[0m", long_line]
    out = ci_logs.extract_patch(_log(block))
    assert out is not None
    assert "\x1b" not in out
    assert long_line in out.splitlines()


def test_a_hunk_cut_short_is_not_a_patch() -> None:
    cut = ["--- a/f.txt", "+++ b/f.txt", "@@ -1,3 +1,3 @@", " a", "-b"]
    assert ci_logs.extract_patch(_log(cut)) is None


def test_a_log_with_no_diff_has_no_patch() -> None:
    assert ci_logs.extract_patch(_log(["E   AssertionError: boom"])) is None


def _job_logs_tool():
    executor = MagicMock()
    executor.register = MagicMock()
    build_tools("fake-token", executor)
    return {c[0][0]: c[0][1] for c in executor.register.call_args_list}["get_github_job_logs"]


def _github(text: str):
    async def _call(client, method, url, provider, action, **kwargs):
        return httpx.Response(200, text=text), None

    return _call


async def _no_cache(*_args, **_kwargs):
    return None


@pytest.mark.asyncio
async def test_the_tool_answers_the_patch_and_writes_it_whole_for_the_workspace() -> None:
    tool = _job_logs_tool()
    base = "app.integrations.providers.github"
    with (
        patch(f"{base}._common.safe_request", new=_github(_log(_DIFF))),
        patch(f"{base}.actions._job_log_cache_get", new=_no_cache),
        patch(f"{base}.actions._job_log_cache_set", new=_no_cache),
        patch(f"{base}.actions.workspace_ingest_requested", return_value=False),
    ):
        answer = await tool(repo="o/r", job_id=9, patch=True)
    assert "1 file(s)" in answer
    assert "+x = {'a': 1}" in answer
    with (
        patch(f"{base}._common.safe_request", new=_github(_log(_DIFF))),
        patch(f"{base}.actions.workspace_ingest_requested", return_value=True),
    ):
        ingested = await tool(repo="o/r", job_id=9, patch=True)
    assert ingested.startswith("diff --git a/backend/app/x.py")


@pytest.mark.asyncio
async def test_no_diff_answers_an_error_so_nothing_is_written() -> None:
    tool = _job_logs_tool()
    base = "app.integrations.providers.github"
    with (
        patch(f"{base}._common.safe_request", new=_github(_log(["E   boom"]))),
        patch(f"{base}.actions.workspace_ingest_requested", return_value=True),
    ):
        out = await tool(repo="o/r", job_id=9, patch=True)
    assert out.startswith("Error:") and "no unified diff" in out
