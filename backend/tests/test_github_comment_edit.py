"""update_github_comment: a comment changed in place.

Find/replace ``edits`` on the body as GitHub stores it (all or nothing, diff
returned) or a whole ``body``; ``expect_updated_at`` refuses a write over a
newer edit; the comment is named by its id or its link.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest

from app.integrations.providers.github import build_tools
from app.integrations.providers.github.issues import comment_id_of

_SAFE_REQUEST = "app.integrations.providers.github._common.safe_request"
_URL = "https://api.github.com/repos/o/r/issues/comments/42"
_BODY = "**UX verdict, round 2**\n\nB1: the label wraps at 1024.\n" + "".join(
    f"detail line {n}\n" for n in range(1_500)
)


def _tool():
    executor = MagicMock()
    executor.register = MagicMock()
    defs = {d.name: d for d in build_tools("fake-token", executor)}
    handler = {c[0][0]: c[0][1] for c in executor.register.call_args_list}["update_github_comment"]
    return handler, defs


class _GitHub:
    """Serves comment 42 of o/r#7 and records every PATCH."""

    def __init__(self, updated_at: str = "2026-11-17T18:00:00Z"):
        self.updated_at = updated_at
        self.patches: list[tuple[str, dict[str, Any]]] = []

    async def __call__(self, client, method, url, provider, action, **kwargs):
        assert url == _URL, url
        if method == "patch":
            self.patches.append((url, kwargs["json"]))
            return httpx.Response(
                200,
                json={
                    "html_url": "https://github.com/o/r/pull/7#issuecomment-42",
                    "issue_url": "https://api.github.com/repos/o/r/issues/7",
                },
            ), None
        return httpx.Response(200, json={"body": _BODY, "updated_at": self.updated_at}), None


async def _no_self_action(*args, **kwargs):
    return None


@pytest.fixture(autouse=True)
def _quiet_self_action():
    with patch("app.integrations.providers.github.issues.mark_self_action", new=_no_self_action, create=True):
        yield


def test_the_comment_is_named_by_its_id_or_its_link():
    assert comment_id_of("42") == 42
    assert comment_id_of(42) == 42
    assert comment_id_of("https://github.com/o/r/pull/7#issuecomment-42") == 42
    assert comment_id_of(_URL) == 42
    for bad in ("", None, "abc", "0", "-3", "https://github.com/o/r/pull/7"):
        assert comment_id_of(bad) is None


def test_the_schema_requires_only_the_repository_and_the_comment():
    _, defs = _tool()
    schema = defs["update_github_comment"].input_schema
    assert schema["required"] == ["repo", "comment"]
    assert {"body", "edits", "expect_updated_at"} <= set(schema["properties"])


@pytest.mark.asyncio
async def test_one_edit_changes_the_line_and_answers_the_diff_not_the_body():
    tool, _ = _tool()
    gh = _GitHub()
    with patch(_SAFE_REQUEST, new=gh):
        out = await tool(
            repo="o/r",
            comment="https://github.com/o/r/pull/7#issuecomment-42",
            edits=[{"find": "wraps at 1024", "replace": "wraps at 1280"}],
        )
    assert len(gh.patches) == 1
    assert gh.patches[0][1]["body"] == _BODY.replace("wraps at 1024", "wraps at 1280")
    assert out.startswith("Comment updated: https://github.com/o/r/pull/7#issuecomment-42")
    assert "+B1: the label wraps at 1280." in out and len(out) < 2_000


@pytest.mark.asyncio
async def test_a_missing_find_or_a_stale_read_writes_nothing():
    tool, _ = _tool()
    gh = _GitHub(updated_at="2026-11-17T18:05:00Z")
    with patch(_SAFE_REQUEST, new=gh):
        missing = await tool(repo="o/r", comment="42", edits=[{"find": "no such text", "replace": "x"}])
        stale = await tool(
            repo="o/r",
            comment="42",
            expect_updated_at="2026-11-17T18:00:00Z",
            edits=[{"find": "wraps at 1024", "replace": "wraps at 1280"}],
        )
    assert gh.patches == []
    assert missing.startswith("Error") and "not found" in missing
    assert "changed since your read" in stale


@pytest.mark.asyncio
async def test_a_whole_body_replaces_the_comment():
    tool, _ = _tool()
    gh = _GitHub()
    with patch(_SAFE_REQUEST, new=gh):
        out = await tool(repo="o/r", comment="42", body="short")
    assert gh.patches == [(_URL, {"body": "short"})] and "Body diff" not in out


@pytest.mark.asyncio
async def test_bad_arguments_are_refused_before_any_request():
    tool, _ = _tool()
    gh = _GitHub()
    with patch(_SAFE_REQUEST, new=gh):
        both = await tool(repo="o/r", comment="42", body="x", edits=[{"find": "a", "replace": "b"}])
        neither = await tool(repo="o/r", comment="42")
        bad_id = await tool(repo="o/r", comment="not-a-comment", body="x")
        bad_repo = await tool(repo="not-a-repo", comment="42", body="x")
    assert "not both" in both and "Nothing was written" in neither and "issuecomment" in bad_id
    assert "owner/name" in bad_repo.lower() or "invalid" in bad_repo.lower()
    assert gh.patches == []
