"""create_github_pr warns about an open pull request of the same author that overlaps (issue #10353).

A fake ``safe_request`` answers the title-rule read (none declared), the
creation, the list of open pull requests and their files. The warning names a
peer that shares a file or an issue; no overlap, another author or an
unreadable list adds nothing, and the creation is never refused.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import pytest

from app.integrations.providers.github import build_tools
from app.integrations.providers.github.pr_overlap import issue_refs

_SAFE_REQUEST = "app.integrations.providers.github._common.safe_request"
_PULLS_URL = "https://api.github.com/repos/acme/app/pulls"


def _pr(number: int, login: str, title: str = "t", body: str = "") -> dict:
    return {"number": number, "user": {"login": login}, "title": title, "body": body}


class _GitHub:
    def __init__(self, open_prs: list[dict] | None, files: dict[int, list[str]]) -> None:
        self.open_prs = open_prs
        self.files = files
        self.calls: list[tuple[str, str]] = []

    async def __call__(self, client, method, url, provider, action, **kwargs):
        self.calls.append((method, url))
        if url.endswith("/contents/.xtm-one/checks.toml"):
            return None, "Error: GitHub API error (404): Not Found"
        if method == "get" and "/issues/" in url:  # the closing-keyword guard reads the kind: an issue
            return httpx.Response(200, json={"number": int(url.rsplit("/", 1)[1])}), None
        if method == "post" and url == _PULLS_URL:
            created = _pr(12, "SamuelHassine", kwargs["json"]["title"], kwargs["json"].get("body", ""))
            created |= {"html_url": "https://github.com/acme/app/pull/12", "head": {"ref": "f", "sha": "abc"}}
            return httpx.Response(201, json=created), None
        if method == "get" and url == _PULLS_URL:
            if self.open_prs is None:
                return None, "Error: GitHub API error (502): Bad Gateway"
            return httpx.Response(200, json=self.open_prs), None
        if method == "get" and url.endswith("/files"):
            number = int(url.split("/pulls/")[1].split("/")[0])
            return httpx.Response(200, json=[{"filename": f} for f in self.files.get(number, [])]), None
        raise AssertionError(f"unexpected {method} {url}")

    def writes(self) -> list[tuple[str, str]]:
        return [(m, u) for m, u in self.calls if m in ("post", "patch")]


def _tools():
    executor = MagicMock()
    executor.register = MagicMock()
    build_tools("fake-token", executor)
    return {c[0][0]: c[0][1] for c in executor.register.call_args_list}


async def _create(fake: _GitHub, title: str = "fix(work-items): defer a draft (#20)", body: str = "Closes #20") -> str:
    with patch(_SAFE_REQUEST, new=fake):
        return await _tools()["create_github_pr"](repo="acme/app", title=title, head="f", base="main", body=body)


def test_issue_refs_reads_keywords_and_the_title_suffix():
    assert issue_refs("fix(x): y (#20)", "Closes #21. Refs #22, see #23, part of #24") == {20, 21, 22, 24}
    assert issue_refs("fix(x): y", "") == set()


@pytest.mark.asyncio
async def test_a_peer_sharing_files_is_named_and_the_pr_is_created():
    peers = [_pr(12, "SamuelHassine"), _pr(9, "samuelhassine", "fix(work-items): draft path (#18)")]
    fake = _GitHub(peers, {12: ["a.py", "b.py", "c.py"], 9: ["b.py", "c.py", "z.py"]})
    result = await _create(fake)
    first = result.split("\n", 1)[0]
    warning = result[result.index("Warning:") :]
    assert first == "PR #12 created: https://github.com/acme/app/pull/12"
    assert warning.startswith("Warning: 1 other open PR(s) by samuelhassine in acme/app overlap this one")
    assert '#9 "fix(work-items): draft path (#18)": shares 2 file(s): b.py, c.py' in warning
    assert fake.writes() == [("post", _PULLS_URL)]


@pytest.mark.asyncio
async def test_a_peer_on_the_same_issue_is_named_without_shared_files():
    fake = _GitHub([_pr(9, "SamuelHassine", "feat(x): other", "Refs #20")], {12: ["a.py"], 9: ["z.py"]})
    result = await _create(fake)
    assert '#9 "feat(x): other": shares issue #20' in result


@pytest.mark.asyncio
async def test_no_overlap_and_another_author_add_nothing():
    peers = [_pr(9, "SamuelHassine", "feat(x): other (#5)"), _pr(8, "octocat", "fix(y): z (#20)")]
    fake = _GitHub(peers, {12: ["a.py"], 9: ["z.py"], 8: ["a.py"]})
    result = await _create(fake)
    assert result.split("\n")[0] == "PR #12 created: https://github.com/acme/app/pull/12"
    assert "Warning" not in result
    assert ("get", f"{_PULLS_URL}/8/files") not in fake.calls


@pytest.mark.asyncio
async def test_an_unreadable_list_never_fails_the_creation():
    result = await _create(_GitHub(None, {}))
    assert result.split("\n")[0] == "PR #12 created: https://github.com/acme/app/pull/12"
    assert "Warning" not in result
