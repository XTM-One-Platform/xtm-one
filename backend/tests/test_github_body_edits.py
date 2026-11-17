"""In-place edits of an issue or pull request body.

``update_github_issue`` and ``update_github_pr`` take ``edits`` (find/replace on
the body as GitHub stores it at write time, all or nothing, diff returned) and
``expect_updated_at`` (refused when the object changed since the caller's read).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest

from app.integrations.providers.github import build_tools

_SAFE_REQUEST = "app.integrations.providers.github._common.safe_request"

_BODY = "## Checklist\n- [x] F1 first\n- [ ] F2 second\n" + "filler line\n" * 2_000 + "- [ ] F2 second\n"


def _tool(name: str):
    executor = MagicMock()
    executor.register = MagicMock()
    build_tools("fake-token", executor)
    return {c[0][0]: c[0][1] for c in executor.register.call_args_list}[name]


def _defs() -> dict[str, Any]:
    executor = MagicMock()
    executor.register = MagicMock()
    return {d.name: d for d in build_tools("fake-token", executor)}


class _GitHub:
    """Serves one issue (or PR) and records every PATCH."""

    def __init__(self, body: str = _BODY, updated_at: str = "2026-11-11T18:00:00Z"):
        self.body = body
        self.updated_at = updated_at
        self.patches: list[dict[str, Any]] = []

    async def __call__(self, client, method, url, provider, action, **kwargs):
        if method == "patch":
            self.patches.append(kwargs["json"])
            return httpx.Response(200, json={"state": "open", "html_url": "h", "number": 5}), None
        if url.endswith("/contents/.xtm-one/checks.toml") or "/contents/" in url:
            return httpx.Response(404, json={}), "not found"
        return httpx.Response(
            200, json={"body": self.body, "updated_at": self.updated_at, "state": "open", "html_url": "h"}
        ), None


async def _no_self_action(*args, **kwargs):
    return None


@pytest.fixture(autouse=True)
def _quiet_self_action():
    with (
        patch("app.integrations.providers.github.issues.mark_self_action", new=_no_self_action, create=True),
        patch("app.integrations.providers.github.pulls.mark_self_action", new=_no_self_action, create=True),
    ):
        yield


@pytest.mark.asyncio
async def test_one_edit_adds_a_line_after_an_anchor_and_answers_the_diff_not_the_body():
    gh = _GitHub()
    with patch(_SAFE_REQUEST, new=gh):
        out = await _tool("update_github_issue")(
            repo="o/r", number=5, edits=[{"find": "- [x] F1 first\n", "replace": "- [x] F1 first\n- [ ] F1b new\n"}]
        )
    assert len(gh.patches) == 1
    written = gh.patches[0]["body"]
    assert written == _BODY.replace("- [x] F1 first\n", "- [x] F1 first\n- [ ] F1b new\n")
    assert "Body diff:" in out and "+- [ ] F1b new" in out
    assert "filler line\n" * 20 not in out and len(out) < 2_000


@pytest.mark.asyncio
async def test_a_missing_or_ambiguous_find_writes_nothing():
    gh = _GitHub()
    with patch(_SAFE_REQUEST, new=gh):
        tool = _tool("update_github_issue")
        missing = await tool(repo="o/r", number=5, edits=[{"find": "no such line", "replace": "x"}])
        twice = await tool(repo="o/r", number=5, edits=[{"find": "- [ ] F2 second\n", "replace": "- [x] F2 second\n"}])
        partial = await tool(
            repo="o/r",
            number=5,
            edits=[{"find": "- [x] F1 first", "replace": "- [x] F1 done"}, {"find": "absent", "replace": "y"}],
        )
    assert gh.patches == []
    assert "not found" in missing and "occurs 2 times" in twice and "edit 2" in partial
    for out in (missing, twice, partial):
        assert out.startswith("Error")


@pytest.mark.asyncio
async def test_replace_all_changes_every_occurrence():
    gh = _GitHub()
    with patch(_SAFE_REQUEST, new=gh):
        await _tool("update_github_issue")(
            repo="o/r",
            number=5,
            edits=[{"find": "- [ ] F2 second\n", "replace": "- [x] F2 second\n", "replace_all": True}],
        )
    assert gh.patches[0]["body"].count("- [x] F2 second\n") == 2


@pytest.mark.asyncio
async def test_a_concurrent_edit_since_the_read_is_not_overwritten():
    gh = _GitHub(updated_at="2026-11-11T18:05:00Z")
    with patch(_SAFE_REQUEST, new=gh):
        tool = _tool("update_github_issue")
        stale = await tool(
            repo="o/r",
            number=5,
            expect_updated_at="2026-11-11T18:00:00Z",
            edits=[{"find": "- [x] F1 first", "replace": "- [x] F1 done"}],
        )
        stale_whole = await tool(repo="o/r", number=5, body="new", expect_updated_at="2026-11-11T18:00:00Z")
        fresh = await tool(
            repo="o/r",
            number=5,
            expect_updated_at="2026-11-11T18:05:00Z",
            edits=[{"find": "- [x] F1 first", "replace": "- [x] F1 done"}],
        )
    assert "changed since your read" in stale and "changed since your read" in stale_whole
    assert len(gh.patches) == 1 and "- [x] F1 done" in gh.patches[0]["body"] and "Body diff:" in fresh


@pytest.mark.asyncio
async def test_body_and_edits_together_are_refused():
    gh = _GitHub()
    with patch(_SAFE_REQUEST, new=gh):
        issue = await _tool("update_github_issue")(
            repo="o/r", number=5, body="x", edits=[{"find": "a", "replace": "b"}]
        )
        pr = await _tool("update_github_pr")(repo="o/r", number=5, body="x", edits=[{"find": "a", "replace": "b"}])
    assert "not both" in issue and "not both" in pr and gh.patches == []


@pytest.mark.asyncio
async def test_a_pull_request_body_is_edited_in_place_too():
    gh = _GitHub()
    with patch(_SAFE_REQUEST, new=gh):
        out = await _tool("update_github_pr")(
            repo="o/r", number=5, edits=[{"find": "- [x] F1 first", "replace": "- [x] F1 shipped"}]
        )
    assert len(gh.patches) == 1 and gh.patches[0]["body"].startswith("## Checklist\n- [x] F1 shipped\n")
    assert "Body diff:" in out and "+- [x] F1 shipped" in out


def test_the_new_arguments_are_optional():
    defs = _defs()
    for name in ("update_github_issue", "update_github_pr"):
        schema = defs[name].input_schema
        assert schema["required"] == ["repo", "number"]
        assert {"edits", "expect_updated_at"} <= set(schema["properties"])
    issue = defs["get_github_issue"].input_schema
    assert "include_comments" in issue["properties"] and issue["required"] == ["repo", "number"]
