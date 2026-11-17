"""A repeated ``create_github_pr`` / ``create_github_branch`` answers what already exists (#12049).

A resumed turn, or a retry, may send again a creation that landed. GitHub then
answers 422 ("A pull request already exists", "Reference already exists"); the
tools read the existing object and answer it as a success, flagged, with no
second write. The existing object is read only after the 422.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.integrations.providers.github import build_tools

_SAFE_REQUEST = "app.integrations.providers.github._common.safe_request"
API = "https://api.github.com/repos/acme/app"
SHA = "a" * 40
OTHER = "b" * 40
_PR_EXISTS = "Error: GitHub API error (422): A pull request already exists for acme:feat/x."
_REF_EXISTS = "Error: GitHub API error (422): Reference already exists"

_OPEN_PR = {
    "number": 41,
    "html_url": "https://github.com/acme/app/pull/41",
    "draft": False,
    "head": {"ref": "feat/x", "sha": SHA},
}


def _tool(name: str, **build_kwargs):
    executor = MagicMock()
    executor.register = MagicMock()
    build_tools("fake-token", executor, **build_kwargs)
    return next(c[0][1] for c in executor.register.call_args_list if c[0][0] == name)


class FakeGitHub:
    def __init__(self, routes: dict[tuple[str, str], tuple[object, str | None]]) -> None:
        self.routes = routes
        self.calls: list[tuple[str, str, dict]] = []

    def count(self, method: str, url: str) -> int:
        return sum(1 for verb, u, _ in self.calls if verb == method and u == url)

    async def __call__(self, client, method, url, provider, action, **kwargs):
        self.calls.append((method, url, kwargs))
        if url.endswith("/contents/.xtm-one/checks.toml"):
            return None, "Error: GitHub API error (404): Not Found"
        for (verb, prefix), (payload, err) in self.routes.items():
            if verb == method and url == prefix:
                if err:
                    return None, err
                return httpx.Response(200, json=payload), None
        raise AssertionError(f"unexpected {method.upper()} {url}")


def _pr_routes(post_err: str | None, open_pulls: list[dict]) -> dict:
    return {
        ("post", f"{API}/pulls"): (_OPEN_PR, post_err),
        ("get", f"{API}/pulls"): (open_pulls, None),
    }


@pytest.mark.asyncio
async def test_a_repeated_pull_request_answers_the_open_one():
    fake = FakeGitHub(_pr_routes(_PR_EXISTS, [_OPEN_PR]))
    with (
        patch(_SAFE_REQUEST, new=fake),
        patch("app.integrations.providers.github.pr_overlap.overlap_warning", new=AsyncMock(return_value=None)),
    ):
        result = await _tool("create_github_pr")(
            repo="acme/app", title="fix(x): y (#1)", head="feat/x", base="main", labels=None, assignees=["sam"]
        )

    assert result.startswith("PR #41 already open, nothing created: https://github.com/acme/app/pull/41")
    assert f"head feat/x at {SHA[:12]}, base main" in result
    assert "Error" not in result
    assert fake.count("post", f"{API}/pulls") == 1
    read = next(kw for verb, url, kw in fake.calls if verb == "get" and url == f"{API}/pulls")
    assert read["params"]["head"] == "acme:feat/x"
    assert read["params"]["base"] == "main"
    assert read["params"]["state"] == "open"
    # Nothing is assigned or labelled a second time.
    assert not any("/issues/41/" in url for _, url, _ in fake.calls)


@pytest.mark.asyncio
async def test_a_repeated_pull_request_binds_the_work_item_to_the_open_one():
    record = AsyncMock(return_value="Work item names PR #41.")
    fake = FakeGitHub(_pr_routes(_PR_EXISTS, [_OPEN_PR]))
    with patch(_SAFE_REQUEST, new=fake):
        tool = _tool("create_github_pr", record_pull_request=record)
        result = await tool(repo="acme/app", title="fix(x): y (#1)", head="feat/x", base="main", work_item_id="w1")

    assert result.endswith("Work item names PR #41.")
    record.assert_awaited_once_with("w1", "acme/app", 41, "feat/x", SHA, _OPEN_PR["html_url"], False)


@pytest.mark.asyncio
async def test_the_refusal_stands_when_no_open_pull_request_is_found():
    fake = FakeGitHub(_pr_routes(_PR_EXISTS, []))
    with patch(_SAFE_REQUEST, new=fake):
        result = await _tool("create_github_pr")(repo="acme/app", title="fix(x): y (#1)", head="feat/x", base="main")

    assert result == _PR_EXISTS


@pytest.mark.asyncio
async def test_another_refusal_is_answered_without_a_read():
    err = "Error: GitHub API error (422): No commits between main and feat/x"
    fake = FakeGitHub(_pr_routes(err, [_OPEN_PR]))
    with patch(_SAFE_REQUEST, new=fake):
        result = await _tool("create_github_pr")(repo="acme/app", title="fix(x): y (#1)", head="feat/x", base="main")

    assert result == err
    assert fake.count("get", f"{API}/pulls") == 0


def _branch_routes(existing_sha: str | None) -> dict:
    routes: dict = {
        ("get", f"{API}"): ({"default_branch": "main"}, None),
        ("get", f"{API}/git/ref/heads/main"): ({"object": {"sha": SHA}}, None),
        ("post", f"{API}/git/refs"): ({}, _REF_EXISTS),
    }
    if existing_sha is not None:
        routes[("get", f"{API}/git/ref/heads/feat/x")] = ({"object": {"sha": existing_sha}}, None)
    else:
        routes[("get", f"{API}/git/ref/heads/feat/x")] = (None, "Error: GitHub API error (404): Not Found")
    return routes


@pytest.mark.asyncio
async def test_a_repeated_branch_at_the_same_sha_is_a_success():
    fake = FakeGitHub(_branch_routes(SHA))
    with patch(_SAFE_REQUEST, new=fake):
        result = await _tool("create_github_branch")(repo="acme/app", branch="feat/x")

    assert result == f"Branch 'feat/x' already exists in acme/app at {SHA[:8]}, nothing created (ref refs/heads/feat/x)"
    assert fake.count("post", f"{API}/git/refs") == 1


@pytest.mark.asyncio
async def test_a_branch_elsewhere_stays_refused_with_both_shas():
    fake = FakeGitHub(_branch_routes(OTHER))
    with patch(_SAFE_REQUEST, new=fake):
        result = await _tool("create_github_branch")(repo="acme/app", branch="feat/x")

    assert result.startswith("Error: branch 'feat/x' already exists in acme/app at bbbbbbbb, not at aaaaaaaa.")


@pytest.mark.asyncio
async def test_an_unreadable_existing_branch_answers_the_refusal():
    fake = FakeGitHub(_branch_routes(None))
    with patch(_SAFE_REQUEST, new=fake):
        result = await _tool("create_github_branch")(repo="acme/app", branch="feat/x")

    assert result == _REF_EXISTS
