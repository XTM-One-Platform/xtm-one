"""create_github_pr sets labels in the creating call (issue #11551).

A workflow that runs on ``opened`` and reads the labels at start (OpenCTI's
"Check Pull Request is linked to an issue", which exempts ``dependencies``)
failed when the label came in a later call. A fake ``safe_request`` answers the
title-rule read (none declared), the label reads, the pull request creation and
the Issues API label write: the tests check that the labels are written first
after creation and read back, that an unknown label refuses the call before any
write, that a 404 right after creation is retried once, and that a call without
labels writes nothing more than before.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import pytest

from app.integrations.providers.github import build_tools, pulls

_SAFE_REQUEST = "app.integrations.providers.github._common.safe_request"
_PULLS_URL = "https://api.github.com/repos/acme/app/pulls"
_LABELS_URL = "https://api.github.com/repos/acme/app/issues/12/labels"
_ASSIGN_URL = "https://api.github.com/repos/acme/app/issues/12/assignees"
_NOT_FOUND = "Error: GitHub resource not found while trying to do it."


class _GitHub:
    def __init__(self, defined: set[str], label_404s: int = 0) -> None:
        self.defined = defined
        self.label_404s = label_404s
        self.calls: list[tuple[str, str, dict]] = []

    async def __call__(self, client, method, url, provider, action, **kwargs):
        self.calls.append((method, url, kwargs))
        if url.endswith("/contents/.xtm-one/checks.toml"):
            return None, _NOT_FOUND
        if method == "get" and "/labels/" in url:
            label = url.rsplit("/", 1)[1].replace("%20", " ")
            if label in self.defined:
                return httpx.Response(200, json={"name": label}), None
            return None, _NOT_FOUND
        if method == "post" and url == _PULLS_URL:
            pr = {"number": 12, "html_url": "https://github.com/acme/app/pull/12", "head": {"ref": "f", "sha": "abc"}}
            return httpx.Response(201, json=pr), None
        if method == "post" and url == _LABELS_URL:
            if self.label_404s:
                self.label_404s -= 1
                return None, _NOT_FOUND
            return httpx.Response(200, json=[{"name": n} for n in kwargs["json"]["labels"]]), None
        if method == "post" and url == _ASSIGN_URL:
            return httpx.Response(201, json={"assignees": [{"login": "octocat"}]}), None
        if method == "get":
            return None, _NOT_FOUND
        raise AssertionError(f"unexpected {method} {url}")

    def writes(self) -> list[tuple[str, str]]:
        return [(m, u) for m, u, _ in self.calls if m in ("post", "patch")]


def _tools():
    executor = MagicMock()
    executor.register = MagicMock()
    build_tools("fake-token", executor)
    return {c[0][0]: c[0][1] for c in executor.register.call_args_list}


@pytest.mark.asyncio
async def test_labels_are_written_first_after_creation_and_read_back():
    fake = _GitHub(defined={"dependencies", "filigran team"})
    with patch(_SAFE_REQUEST, new=fake):
        result = await _tools()["create_github_pr"](
            repo="acme/app",
            title="t",
            head="f",
            base="main",
            labels=[" dependencies ", "filigran team", "dependencies", ""],
            assignees=["octocat"],
        )
    lines = result.split("\n")
    assert lines[0] == "PR #12 created: https://github.com/acme/app/pull/12"
    assert lines[1] == "Labels: dependencies, filigran team"
    assert fake.writes() == [("post", _PULLS_URL), ("post", _LABELS_URL), ("post", _ASSIGN_URL)]
    label_write = next(kw for m, u, kw in fake.calls if u == _LABELS_URL)
    assert label_write["json"] == {"labels": ["dependencies", "filigran team"]}


@pytest.mark.asyncio
async def test_an_unknown_label_refuses_before_any_write():
    fake = _GitHub(defined={"dependencies"})
    with patch(_SAFE_REQUEST, new=fake):
        result = await _tools()["create_github_pr"](
            repo="acme/app", title="t", head="f", base="main", labels=["dependencies", "deps", "bug fix"]
        )
    assert result.startswith("Refused: label(s) not defined in acme/app: deps, bug fix.")
    assert "list_github_labels" in result
    assert fake.writes() == []


@pytest.mark.asyncio
async def test_a_404_right_after_creation_is_retried_once(monkeypatch):
    monkeypatch.setattr(pulls, "_LABEL_RETRY_DELAY", 0)
    fake = _GitHub(defined={"dependencies"}, label_404s=1)
    with patch(_SAFE_REQUEST, new=fake):
        result = await _tools()["create_github_pr"](
            repo="acme/app", title="t", head="f", base="main", labels=["dependencies"]
        )
    assert result.endswith("Labels: dependencies")
    assert fake.writes() == [("post", _PULLS_URL), ("post", _LABELS_URL), ("post", _LABELS_URL)]


@pytest.mark.asyncio
async def test_a_failed_label_write_keeps_the_pull_request(monkeypatch):
    monkeypatch.setattr(pulls, "_LABEL_RETRY_DELAY", 0)
    fake = _GitHub(defined={"dependencies"}, label_404s=2)
    with patch(_SAFE_REQUEST, new=fake):
        result = await _tools()["create_github_pr"](
            repo="acme/app", title="t", head="f", base="main", labels=["dependencies"]
        )
    first, note = result.split("\n", 1)
    assert first == "PR #12 created: https://github.com/acme/app/pull/12"
    assert note.startswith("Not labelled (dependencies):")
    assert "add_github_labels" in note


@pytest.mark.asyncio
async def test_no_labels_writes_nothing_more():
    fake = _GitHub(defined=set())
    with patch(_SAFE_REQUEST, new=fake):
        result = await _tools()["create_github_pr"](repo="acme/app", title="t", head="f", base="main")
    assert result == "PR #12 created: https://github.com/acme/app/pull/12"
    assert fake.writes() == [("post", _PULLS_URL)]
    assert not [u for m, u, _ in fake.calls if "/labels" in u]
