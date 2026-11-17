"""The changed files and commits of compare_github_commits agree with the header.

A fake ``safe_request`` answers the compare call with more files than one
answer lists: the answer says "N of M shown", names the ``file_offset`` that
reads the rest, and that offset lists the remaining files.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from app.integrations.providers.github import build_tools

_SAFE_REQUEST = "app.integrations.providers.github._common.safe_request"


def _handlers():
    executor = MagicMock()
    executor.register = MagicMock()
    definitions = build_tools("fake-token", executor)
    return {call[0][0]: call[0][1] for call in executor.register.call_args_list}, {d.name: d for d in definitions}


def _files(count: int) -> list[dict]:
    return [
        {"filename": f"src/file_{i:03d}.py", "status": "modified", "additions": 1, "deletions": 0} for i in range(count)
    ]


def _commits(count: int) -> list[dict]:
    return [
        {
            "sha": f"{i:040x}",
            "commit": {"message": f"feat: change {i}", "author": {"name": "a", "date": "2026-11-07T00:00:00Z"}},
        }
        for i in range(count)
    ]


async def _compare(files: list[dict], commits: list[dict] | None = None, **kwargs):
    handlers, _ = _handlers()
    response = MagicMock()
    response.json.return_value = {
        "status": "ahead",
        "ahead_by": len(commits or []),
        "behind_by": 0,
        "html_url": "https://github.com/acme/app/compare/main...feat",
        "commits": commits or [],
        "files": files,
    }

    async def fake_request(client, method, url, provider, action, **kw):
        return response, None

    with patch(_SAFE_REQUEST, side_effect=fake_request):
        return await handlers["compare_github_commits"](repo="acme/app", base="main", head="feat", **kwargs)


def _listed(content: str) -> list[str]:
    return [
        line.split()[1]
        for line in content.split("Changed files:", 1)[1].splitlines()[1:]
        if line.startswith("  ") and not line.lstrip().startswith(("...", "GitHub"))
    ]


@pytest.mark.asyncio
async def test_a_list_longer_than_one_answer_says_n_of_m_and_the_offset():
    result = await _compare(_files(51))

    assert "51 files changed" in result.content
    assert "Changed files: (50 of 51 shown)" in result.content
    assert "file_offset=50" in result.content
    assert len(_listed(result.content)) == 50


@pytest.mark.asyncio
async def test_the_offset_lists_the_files_not_shown():
    result = await _compare(_files(51), file_offset=50)

    assert "Changed files: (1 of 51 shown, from 51)" in result.content
    assert _listed(result.content) == ["src/file_050.py"]
    assert "file_offset=" not in result.content


@pytest.mark.asyncio
async def test_a_whole_list_carries_its_count_and_no_continuation():
    result = await _compare(_files(3))

    assert "Changed files: (3)" in result.content
    assert len(_listed(result.content)) == 3
    assert "file_offset=" not in result.content


@pytest.mark.asyncio
async def test_github_s_300_file_cap_is_named():
    result = await _compare(_files(300))

    assert "at most 300 files" in result.content


@pytest.mark.asyncio
async def test_commits_beyond_the_window_are_counted():
    result = await _compare(_files(1), _commits(31))

    assert "Commits: (30 of 31 shown)" in result.content
    assert "lists the 1 not shown" in result.content


@pytest.mark.asyncio
async def test_a_non_integer_offset_is_refused():
    result = await _compare(_files(2), file_offset="x")

    assert result == "Error: 'file_offset' must be an integer."


def test_file_offset_is_declared_optional():
    _, definitions = _handlers()
    schema = definitions["compare_github_commits"].input_schema

    assert "file_offset" in schema["properties"]
    assert schema["required"] == ["repo", "base", "head"]
