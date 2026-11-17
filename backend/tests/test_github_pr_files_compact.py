"""``get_github_pr_files`` answers the file list, not every patch.

* The default answer is one line per file (status, path, +/-) and no patch.
* ``path`` narrows the files: a glob, a directory or a file.
* ``pattern`` answers only the hunks whose lines match, per file.
* ``include_patch`` reads the patches by window, the next offset named.
* Every page of the files endpoint is read; a large PR stays bounded.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest

from app.integrations.providers.github import build_tools, pr_files

_SAFE_REQUEST = "app.integrations.providers.github._common.safe_request"


def _tool() -> Callable[..., Awaitable[str]]:
    executor = MagicMock()
    executor.register = MagicMock()
    build_tools("fake-token", executor)
    tool: Callable[..., Awaitable[str]] = {c[0][0]: c[0][1] for c in executor.register.call_args_list}[
        "get_github_pr_files"
    ]
    return tool


_TWO_HUNKS = (
    "@@ -1,3 +1,3 @@\n context\n-old_value = 1\n+new_value = 1\n@@ -40,3 +40,4 @@\n other\n+NEEDLE = True\n tail"
)


def _file(name: str, patch_text: str = "@@ -1 +1 @@\n-a\n+b", status: str = "modified", **extra: Any) -> dict[str, Any]:
    return {"filename": name, "status": status, "additions": 1, "deletions": 1, "patch": patch_text, **extra}


class _Files:
    def __init__(self, files: list[dict[str, Any]]):
        self.files = files
        self.pages: list[int] = []

    async def __call__(
        self, client: Any, method: str, url: str, provider: str, action: str, **kwargs: Any
    ) -> tuple[httpx.Response, None]:
        assert url.endswith("/pulls/7/files")
        page = kwargs["params"]["page"]
        size = kwargs["params"]["per_page"]
        self.pages.append(page)
        return httpx.Response(200, json=self.files[(page - 1) * size : page * size]), None


async def _call(files: list[dict[str, Any]], **kwargs: Any) -> tuple[str, _Files]:
    github = _Files(files)
    with patch(_SAFE_REQUEST, new=github):
        out = await _tool()(repo="o/r", number=7, **kwargs)
    return out, github


@pytest.mark.asyncio
async def test_the_default_answer_lists_the_files_without_patches() -> None:
    files = [
        _file("backend/app/a.py", _TWO_HUNKS),
        _file("frontend/src/b.tsx"),
        _file("docs/new.md", status="renamed", previous_filename="docs/old.md"),
    ]
    out, _ = await _call(files)
    assert "PR #7: 3 files changed (+3 −3)" in out
    assert "MODIFIED backend/app/a.py (+1 −1)" in out
    assert "RENAMED docs/old.md → docs/new.md (+1 −1)" in out
    assert "@@" not in out and "NEEDLE" not in out
    assert "pattern=REGEX answers the matching hunks" in out


@pytest.mark.asyncio
async def test_path_narrows_the_files_by_directory_glob_or_file() -> None:
    files = [_file("backend/app/a.py"), _file("backend/tests/test_a.py"), _file("frontend/src/b.tsx")]
    by_dir, _ = await _call(files, path="backend/app")
    assert "backend/app/a.py" in by_dir and "test_a.py" not in by_dir and "b.tsx" not in by_dir
    assert "1 of them match path 'backend/app'" in by_dir
    by_glob, _ = await _call(files, path="*.py")
    assert "a.py" in by_glob and "test_a.py" in by_glob and "b.tsx" not in by_glob
    by_deep_glob, _ = await _call(files, path="backend/tests/*")
    assert "test_a.py" in by_deep_glob and "backend/app/a.py" not in by_deep_glob
    none, _ = await _call(files, path="relay/")
    assert "0 of them match path 'relay/'" in none and "No changed file matches the path." in none
    renamed, _ = await _call([_file("new/x.py", status="renamed", previous_filename="old/x.py")], path="old")
    assert "old/x.py → new/x.py" in renamed


@pytest.mark.asyncio
async def test_pattern_answers_only_the_matching_hunks() -> None:
    files = [_file("backend/app/a.py", _TWO_HUNKS), _file("backend/app/c.py", "@@ -1 +1 @@\n-x\n+y")]
    out, _ = await _call(files, pattern=r"NEEDLE\s*=")
    assert "Pattern 'NEEDLE\\\\s*=': 1 matching hunks in 1 of 2 files" in out
    assert "a.py (+1 −1): 1 of 2 hunks match" in out
    assert "+NEEDLE = True" in out
    assert "old_value" not in out and "c.py" not in out.split("\n", 2)[-1]
    nothing, _ = await _call(files, pattern="ABSENT")
    assert "0 matching hunks in 0 of 2 files" in nothing and "@@" not in nothing


@pytest.mark.asyncio
async def test_an_invalid_pattern_is_refused_before_any_read() -> None:
    out, github = await _call([_file("a.py")], pattern="(")
    assert out.startswith("Error: 'pattern' is not a valid regular expression")
    assert github.pages == []


@pytest.mark.asyncio
async def test_include_patch_reads_the_patches_by_window() -> None:
    files = [_file(f"src/f{i}.py", "@@ -1 +1 @@\n" + "+" + "x" * 900 + "\n") for i in range(10)]
    first, _ = await _call(files, include_patch=True, max_chars=2_000)
    assert "[characters 0 to 2,000 of" in first and "continue with offset=2000" in first
    assert "src/f0.py" in first and "src/f9.py" not in first
    last, _ = await _call(files, include_patch=True, offset=8_000)
    assert "; the end]" in last and "src/f9.py" in last
    past, _ = await _call(files, include_patch=True, offset=500_000)
    assert "is past the end of the answer" in past


@pytest.mark.asyncio
async def test_every_page_is_read_and_a_large_pr_stays_bounded() -> None:
    files = [_file(f"pkg/m{i:04d}.py", "@@ -1 +1 @@\n+" + "y" * 2_000) for i in range(450)]
    out, github = await _call(files)
    assert github.pages == [1, 2, 3, 4, 5]
    assert "PR #7: 450 files changed" in out
    assert f"[… {450 - pr_files.MAX_LIST_FILES:,} more files; narrow with path]" in out
    assert len(out) < 20_000
    patched, _ = await _call(files, include_patch=True)
    assert len(patched) < 21_000


def test_hunks_split_on_the_hunk_headers() -> None:
    assert pr_files.hunks(_TWO_HUNKS)[1].startswith("@@ -40,3 +40,4 @@")
    assert len(pr_files.hunks(_TWO_HUNKS)) == 2
