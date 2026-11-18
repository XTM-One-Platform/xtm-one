"""create_github_pr sets the milestone and the type label in the creating call.

A team files its changelog by milestone and a close workflow files issues by
their ``bug`` label; a rule written as prose ("set the milestone right after
creating the PR") was skipped again and again. A fake ``safe_request`` answers
the title-rule read (none declared), the closing-issue read, the milestone
reads, the creation and the Issues API writes: the tests check that the closing
issue's milestone and type label are inherited, that an explicit milestone is
resolved by number or title and wins, that an unknown one refuses before any
write (a milestone is never created), that a closing issue without milestone is
named, and that a PR closing nothing writes nothing more than before.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest

from app.integrations.providers.github import build_tools, pulls

_SAFE_REQUEST = "app.integrations.providers.github._common.safe_request"
_REPO = "https://api.github.com/repos/acme/app"
_PULLS_URL = f"{_REPO}/pulls"
_PR_ISSUE_URL = f"{_REPO}/issues/12"
_LABELS_URL = f"{_PR_ISSUE_URL}/labels"
_NOT_FOUND = "Error: GitHub resource not found while trying to do it."
_MILESTONES = [{"number": 4, "title": "Backlog"}, {"number": 9, "title": "Next release"}]


class _GitHub:
    def __init__(self, issues: dict[int, dict[str, Any]] | None = None, patch_404s: int = 0) -> None:
        self.issues = issues or {}
        self.patch_404s = patch_404s
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    async def __call__(
        self, client: Any, method: str, url: str, provider: str, action: str, **kwargs: Any
    ) -> tuple[Any, str | None]:
        self.calls.append((method, url, kwargs))
        if url.endswith("/contents/.xtm-one/checks.toml"):
            return None, _NOT_FOUND
        if method == "get" and url == f"{_REPO}/milestones":
            return httpx.Response(200, json=_MILESTONES), None
        if method == "get" and url.startswith(f"{_REPO}/milestones/"):
            number = int(url.rsplit("/", 1)[1])
            found = [m for m in _MILESTONES if m["number"] == number]
            return (httpx.Response(200, json=found[0]), None) if found else (None, _NOT_FOUND)
        if method == "get" and "/labels/" in url:
            return httpx.Response(200, json={"name": url.rsplit("/", 1)[1]}), None
        if method == "get" and url.startswith(f"{_REPO}/issues/"):
            number = int(url.rsplit("/", 1)[1])
            if number in self.issues:
                return httpx.Response(200, json={"number": number, **self.issues[number]}), None
            return None, _NOT_FOUND
        if method == "post" and url == _PULLS_URL:
            pr = {"number": 12, "html_url": "https://github.com/acme/app/pull/12", "head": {"ref": "f", "sha": "abc"}}
            return httpx.Response(201, json=pr), None
        if method == "post" and url == _LABELS_URL:
            return httpx.Response(200, json=[{"name": n} for n in kwargs["json"]["labels"]]), None
        if method == "patch" and url == _PR_ISSUE_URL:
            if self.patch_404s:
                self.patch_404s -= 1
                return None, _NOT_FOUND
            number = kwargs["json"]["milestone"]
            title = next(m["title"] for m in _MILESTONES if m["number"] == number)
            return httpx.Response(200, json={"milestone": {"number": number, "title": title}}), None
        if method == "get":
            return None, _NOT_FOUND
        raise AssertionError(f"unexpected {method} {url}")

    def writes(self) -> list[tuple[str, str]]:
        return [(m, u) for m, u, _ in self.calls if m in ("post", "patch")]

    def written(self, method: str, url: str) -> Any:
        return next(kw["json"] for m, u, kw in self.calls if m == method and u == url)


def _tools() -> dict[str, Any]:
    executor = MagicMock()
    executor.register = MagicMock()
    build_tools("fake-token", executor)
    return {c[0][0]: c[0][1] for c in executor.register.call_args_list}


async def _create(fake: _GitHub, body: str = "Closes #7", **kwargs: Any) -> str:
    with patch(_SAFE_REQUEST, new=fake):
        return str(
            await _tools()["create_github_pr"](
                repo="acme/app", title="fix(x): a thing", head="f", base="main", body=body, **kwargs
            )
        )


@pytest.mark.asyncio
async def test_the_closing_issue_gives_its_milestone_and_bug_label() -> None:
    fake = _GitHub({7: {"milestone": {"number": 4, "title": "Backlog"}, "labels": [{"name": "bug"}, {"name": "ui"}]}})
    result = await _create(fake)
    lines = result.split("\n")
    assert lines[0] == "PR #12 created: https://github.com/acme/app/pull/12"
    assert "Labels: bug" in lines
    assert "Milestone: Backlog (from closing issue #7)" in lines
    assert fake.writes() == [("post", _PULLS_URL), ("post", _LABELS_URL), ("patch", _PR_ISSUE_URL)]
    assert fake.written("post", _LABELS_URL) == {"labels": ["bug"]}
    assert fake.written("patch", _PR_ISSUE_URL) == {"milestone": 4}


@pytest.mark.asyncio
async def test_an_explicit_milestone_by_title_wins_over_the_closing_issue() -> None:
    fake = _GitHub({7: {"milestone": {"number": 4, "title": "Backlog"}, "labels": [{"name": "feature"}]}})
    result = await _create(fake, milestone="next release", labels=["docs"])
    assert "Milestone: Next release" in result.split("\n")
    assert "from closing issue" not in result
    assert fake.written("patch", _PR_ISSUE_URL) == {"milestone": 9}
    assert fake.written("post", _LABELS_URL) == {"labels": ["docs", "feature"]}


@pytest.mark.asyncio
async def test_an_explicit_milestone_by_number_is_read_on_the_repository() -> None:
    fake = _GitHub()
    result = await _create(fake, body="", milestone="9")
    assert "Milestone: Next release" in result.split("\n")
    assert ("get", f"{_REPO}/milestones/9") in [(m, u) for m, u, _ in fake.calls]


@pytest.mark.asyncio
async def test_an_unknown_milestone_refuses_before_any_write() -> None:
    fake = _GitHub()
    result = await _create(fake, milestone="Someday")
    assert result.startswith("Refused: milestone 'Someday' not found in acme/app")
    assert "never created" in result
    assert fake.writes() == []
    unknown_number = await _create(_GitHub(), milestone=42)
    assert unknown_number.startswith("Refused: milestone #42 not found in acme/app")


@pytest.mark.asyncio
async def test_a_closing_issue_without_milestone_is_named_and_nothing_is_set() -> None:
    fake = _GitHub({7: {"milestone": None, "labels": []}})
    result = await _create(fake)
    assert result.split("\n")[-1] == "No milestone: closing issue #7 has none"
    assert fake.writes() == [("post", _PULLS_URL)]


@pytest.mark.asyncio
async def test_an_issue_of_another_repository_gives_nothing() -> None:
    fake = _GitHub({3: {"milestone": {"number": 4, "title": "Backlog"}, "labels": [{"name": "bug"}]}})
    result = await _create(fake, body="Closes acme/other#3")
    assert "ilestone" not in result
    assert ("get", f"{_REPO}/issues/3") not in [(m, u) for m, u, _ in fake.calls]
    assert fake.writes() == [("post", _PULLS_URL)]


@pytest.mark.asyncio
async def test_a_404_right_after_creation_is_retried_once(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pulls, "_LABEL_RETRY_DELAY", 0)
    fake = _GitHub({7: {"milestone": {"number": 4, "title": "Backlog"}}}, patch_404s=1)
    result = await _create(fake)
    assert result.endswith("Milestone: Backlog (from closing issue #7)")
    assert fake.writes() == [("post", _PULLS_URL), ("patch", _PR_ISSUE_URL), ("patch", _PR_ISSUE_URL)]


@pytest.mark.asyncio
async def test_a_failed_milestone_write_keeps_the_pull_request(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pulls, "_LABEL_RETRY_DELAY", 0)
    fake = _GitHub({7: {"milestone": {"number": 4, "title": "Backlog"}}}, patch_404s=2)
    result = await _create(fake)
    assert result.split("\n")[0] == "PR #12 created: https://github.com/acme/app/pull/12"
    assert "Milestone not set (Backlog)" in result
    assert "update_github_issue" in result


@pytest.mark.asyncio
async def test_a_pull_request_closing_nothing_writes_nothing_more() -> None:
    fake = _GitHub()
    result = await _create(fake, body="Refs #7")
    assert result == "PR #12 created: https://github.com/acme/app/pull/12"
    assert fake.writes() == [("post", _PULLS_URL)]
    assert not [u for m, u, _ in fake.calls if "/milestones" in u or u.endswith("/issues/7")]
