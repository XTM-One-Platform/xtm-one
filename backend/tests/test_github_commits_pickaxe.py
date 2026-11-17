"""The pickaxe search of list_github_commits (contains=, git log -S).

A fake ``safe_request`` answers the commit list and each commit's diff. The
tests check that only the commits whose diff under the path adds or removes
a line holding the text are answered, the per-file counts, the files GitHub
left out, and the refusal without a path.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from app.integrations.providers.github import build_tools
from app.integrations.providers.github.commits import pickaxe_hits
from app.tools.base import ToolDefinition

_SAFE_REQUEST = "app.integrations.providers.github._common.safe_request"


def _tools() -> tuple[dict[str, Any], dict[str, ToolDefinition]]:
    executor = MagicMock()
    executor.register = MagicMock()
    definitions = build_tools("fake-token", executor)
    handlers = {call[0][0]: call[0][1] for call in executor.register.call_args_list}
    return handlers, {d.name: d for d in definitions}


def _commit(sha: str, subject: str) -> dict[str, Any]:
    return {"sha": sha, "commit": {"message": subject, "author": {"name": "Octo Cat", "date": "2026-11-15T10:00:00Z"}}}


def test_a_line_added_or_removed_with_the_text_counts_and_context_does_not() -> None:
    files = [
        {
            "filename": "src/a.py",
            "patch": "@@ -1,3 +1,3 @@\n keep auto_generate here\n-old auto_generate()\n+new call()",
        },
        {"filename": "src/b.py", "patch": "@@ -1 +1 @@\n-x\n+y = auto_generate"},
        {"filename": "src/c.py", "patch": "@@ -1 +1 @@\n auto_generate only in context"},
        {"filename": "other/d.py", "patch": "+auto_generate"},
        {"filename": "src/big.bin"},
    ]

    hits, unread = pickaxe_hits(files, "auto_generate", "src")

    assert hits == ["src/a.py (+0 -1)", "src/b.py (+1 -0)"]
    assert unread == 1


@pytest.mark.asyncio
async def test_only_the_commits_whose_diff_holds_the_text_are_answered() -> None:
    handlers, _ = _tools()
    diffs = {
        "aaaaaaaa11": [{"filename": "src/stage.ts", "patch": "@@ -4 +4 @@\n-autoGenerate(stage)\n+noop()"}],
        "bbbbbbbb22": [{"filename": "src/stage.ts", "patch": "@@ -9 +9 @@\n-a\n+b"}],
    }
    calls: list[tuple[str, dict[str, Any]]] = []

    async def fake_request(
        client: Any, method: str, url: str, provider: str, action: str, **kwargs: Any
    ) -> tuple[MagicMock, None]:
        calls.append((url, kwargs.get("params") or {}))
        response = MagicMock()
        if url.endswith("/commits"):
            response.json.return_value = [
                _commit("aaaaaaaa11", "refactor: drop auto"),
                _commit("bbbbbbbb22", "chore: tidy"),
            ]
        else:
            response.json.return_value = {"files": diffs[url.rsplit("/", 1)[1]]}
        return response, None

    with patch(_SAFE_REQUEST, side_effect=fake_request):
        result = await handlers["list_github_commits"](repo="acme/app", path="src/", contains="autoGenerate", count=500)

    assert result.splitlines() == [
        "1 of 2 commit(s) touching acme/app:src add or remove a line holding 'autoGenerate':",
        "aaaaaaaa refactor: drop auto — Octo Cat (2026-11-15): src/stage.ts (+0 -1)",
    ]
    # The listing is bounded to the pickaxe cap, then one diff read per commit.
    assert calls[0][1]["per_page"] == 50
    assert calls[0][1]["path"] == "src/"
    assert len(calls) == 3


@pytest.mark.asyncio
async def test_no_match_says_how_many_commits_were_searched() -> None:
    handlers, _ = _tools()

    async def fake_request(
        client: Any, method: str, url: str, provider: str, action: str, **kwargs: Any
    ) -> tuple[MagicMock, None]:
        response = MagicMock()
        if url.endswith("/commits"):
            response.json.return_value = [_commit("cccccccc33", "fix: x")]
        else:
            response.json.return_value = {"files": [{"filename": "src/a.py"}]}
        return response, None

    with patch(_SAFE_REQUEST, side_effect=fake_request):
        result = await handlers["list_github_commits"](repo="acme/app", path="src", contains="needle")

    assert result.splitlines() == [
        "No commit of the 1 touching acme/app:src adds or removes a line holding 'needle'.",
        "1 file diff(s) not shown by GitHub (binary or too large) were not searched.",
    ]


@pytest.mark.asyncio
async def test_contains_without_a_path_is_refused_before_any_request() -> None:
    handlers, definitions = _tools()

    async def fake_request(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("no request expected")

    with patch(_SAFE_REQUEST, side_effect=fake_request):
        result = await handlers["list_github_commits"](repo="acme/app", contains="needle")

    assert result.startswith("Error: contains needs a path")
    assert "contains" in definitions["list_github_commits"].input_schema["properties"]
