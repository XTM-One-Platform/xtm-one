"""create_github_branch and create_tag start from the repository's default branch, never a hard-coded main.

A repository whose default branch is master may keep a stale main: a branch
made from main there starts on old code. Without from_branch (or from_ref)
the base is read from the repository, and the answer names the base branch,
its SHA, and flags a named base that is not the default branch.
"""

from unittest.mock import MagicMock, patch

import httpx
import pytest

from app.integrations.providers.github import build_tools

_SAFE_REQUEST = "app.integrations.providers.github._common.safe_request"

API = "https://api.github.com/repos/acme/legacy"
MASTER_TIP = "a" * 40
STALE_MAIN = "20db47a8" + "0" * 32


class RecordingGitHub:
    """Answers each request from a table keyed by (method, url), and records it."""

    def __init__(self, answers: dict[tuple[str, str], tuple[int, dict]]) -> None:
        self.answers = answers
        self.calls: list[tuple[str, str, dict]] = []

    def sent(self) -> list[tuple[str, str]]:
        return [(method, url) for method, url, _ in self.calls]

    def posted(self) -> dict:
        return next(kwargs["json"] for verb, _, kwargs in self.calls if verb == "post")

    async def __call__(self, client, method, url, provider, action, **kwargs):
        self.calls.append((method, url, kwargs))
        status, payload = self.answers[(method, url)]
        resp = httpx.Response(status, json=payload)
        if status < 400:
            return resp, None
        returned = resp if kwargs.get("expected_statuses") is not None else None
        return returned, f"Error: GitHub API error ({status}): {payload.get('message', '')}"


def _tool(name: str):
    executor = MagicMock()
    executor.register = MagicMock()
    build_tools("fake-token", executor)
    return next(call[0][1] for call in executor.register.call_args_list if call[0][0] == name)


def _master_with_stale_main(extra: dict | None = None) -> RecordingGitHub:
    answers = {
        ("get", API): (200, {"default_branch": "master"}),
        ("get", f"{API}/git/ref/heads/master"): (200, {"object": {"sha": MASTER_TIP}}),
        ("get", f"{API}/git/ref/heads/main"): (200, {"object": {"sha": STALE_MAIN}}),
        ("post", f"{API}/git/refs"): (201, {}),
    }
    answers.update(extra or {})
    return RecordingGitHub(answers)


@pytest.mark.asyncio
async def test_without_from_branch_the_branch_starts_on_the_default_branch_not_a_stale_main():
    fake = _master_with_stale_main()
    with patch(_SAFE_REQUEST, new=fake):
        result = await _tool("create_github_branch")(repo="acme/legacy", branch="feat/x")

    assert fake.sent() == [("get", API), ("get", f"{API}/git/ref/heads/master"), ("post", f"{API}/git/refs")]
    assert fake.posted() == {"ref": "refs/heads/feat/x", "sha": MASTER_TIP}
    assert result == f"Branch 'feat/x' created in acme/legacy from master (SHA: {MASTER_TIP[:8]}), the default branch."


@pytest.mark.asyncio
async def test_a_named_base_that_is_not_the_default_branch_is_flagged():
    fake = _master_with_stale_main()
    with patch(_SAFE_REQUEST, new=fake):
        result = await _tool("create_github_branch")(repo="acme/legacy", branch="feat/x", from_branch="main")

    assert fake.posted() == {"ref": "refs/heads/feat/x", "sha": STALE_MAIN}
    assert result == (
        f"Branch 'feat/x' created in acme/legacy from main (SHA: {STALE_MAIN[:8]}). "
        "Warning: 'main' is not the default branch of acme/legacy ('master'); "
        "omit from_branch to start from the default branch."
    )


@pytest.mark.asyncio
async def test_an_unreadable_default_branch_creates_nothing_without_from_branch():
    fake = RecordingGitHub({("get", API): (404, {"message": "Not Found"})})
    with patch(_SAFE_REQUEST, new=fake):
        result = await _tool("create_github_branch")(repo="acme/legacy", branch="feat/x")

    assert result.endswith("Nothing created: pass from_branch to name the base branch.")
    assert fake.sent() == [("get", API)]


@pytest.mark.asyncio
async def test_a_named_base_still_works_when_the_default_branch_cannot_be_read():
    fake = _master_with_stale_main({("get", API): (500, {"message": "boom"})})
    with patch(_SAFE_REQUEST, new=fake):
        result = await _tool("create_github_branch")(repo="acme/legacy", branch="feat/x", from_branch="master")

    assert fake.posted() == {"ref": "refs/heads/feat/x", "sha": MASTER_TIP}
    assert result == (
        f"Branch 'feat/x' created in acme/legacy from master (SHA: {MASTER_TIP[:8]}). "
        "The default branch of acme/legacy could not be read."
    )


@pytest.mark.asyncio
async def test_create_tag_without_sha_or_from_ref_tags_the_default_branch():
    fake = _master_with_stale_main()
    with patch(_SAFE_REQUEST, new=fake):
        result = await _tool("manage_github_ref")(repo="acme/legacy", action="create_tag", tag="v1.0.0")

    assert fake.sent() == [("get", API), ("get", f"{API}/git/ref/heads/master"), ("post", f"{API}/git/refs")]
    assert fake.posted() == {"ref": "refs/tags/v1.0.0", "sha": MASTER_TIP}
    assert result == f"Tag 'v1.0.0' created in acme/legacy at {MASTER_TIP[:8]}."
