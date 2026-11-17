"""What the per-file GitHub tools send and answer (#12051).

create_or_update_github_file and delete_github_file write through the
create_github_commit path: a commit GitHub builds and signs
(createCommitOnBranch, chained on the tip read with expectedHeadOid), under a
per-branch write lock. Three writes on one branch in one round land as three
signed commits in call order, never one unsigned Contents API commit and two
409s. Only the first commit of an empty repository goes through the Contents
API, which the answer says is not signed. create_github_branch is unchanged.
"""

import asyncio
import base64
import sys
from types import ModuleType
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.integrations.providers.github import build_tools
from app.integrations.providers.github.contents import (
    _OMITTED_BRANCH_SENTINEL,
    _branch_cache_token,
    _file_404_cache_key,
    _token_digest,
    branch_write_lock,
)
from tests.test_github_commit import _SAFE_REQUEST, API, GRAPHQL, NEW_COMMIT, TIP, FakeGitHub

BASE_TIP = "7" * 40
_SIGNED = "GitHub built and signed this commit itself, and reports it as verified."


class RecordingGitHub:
    """Answers each request from a table keyed by (method, url), and records it."""

    def __init__(self, answers: dict[tuple[str, str], tuple[int, dict]]) -> None:
        self.answers = answers
        self.calls: list[tuple[str, str, dict]] = []

    def sent(self) -> list[tuple[str, str]]:
        return [(method, url) for method, url, _ in self.calls]

    def kwargs(self, method: str, url: str) -> dict:
        return next(kwargs for verb, called, kwargs in self.calls if (verb, called) == (method, url))

    async def __call__(self, client, method, url, provider, action, **kwargs):
        self.calls.append((method, url, kwargs))
        status, payload = self.answers[(method, url)]
        resp = httpx.Response(status, json=payload)
        if status < 400:
            return resp, None
        returned = resp if kwargs.get("expected_statuses") is not None else None
        return returned, f"Error: GitHub API error ({status}): {payload.get('message', '')}"


class ChainedGitHub(FakeGitHub):
    """A branch whose tip moves with every commit, and refuses a commit built on a stale tip.

    Each request yields to the event loop first, so calls made in one round
    interleave as they do against the real API.
    """

    def __init__(self) -> None:
        super().__init__()
        self.tip = TIP
        self.landed: list[tuple[str, str, str]] = []
        self.refused = 0

    async def __call__(  # type: ignore[no-untyped-def]
        self, client, method, url, provider, action, **kwargs
    ):
        await asyncio.sleep(0)
        if method == "get" and url.startswith(f"{API}/git/ref/heads/"):
            self.calls.append((method, url, kwargs))
            return httpx.Response(200, json={"object": {"sha": self.tip, "type": "commit"}}), None
        if method == "get" and url.startswith(f"{API}/git/commits/"):
            self.calls.append((method, url, kwargs))
            return httpx.Response(200, json={"sha": self.tip, "tree": {"sha": "b" * 40}}), None
        if method == "post" and url == GRAPHQL:
            self.calls.append((method, url, kwargs))
            given = kwargs["json"]["variables"]["input"]
            if given["expectedHeadOid"] != self.tip:
                self.refused += 1
                errors = [{"type": "STALE_DATA", "message": "Expected branch to point to another commit"}]
                return httpx.Response(200, json={"data": None, "errors": errors}), None
            new = f"{len(self.landed) + 1:x}" * 40
            self.landed.append((self.tip, new, given["message"]["headline"]))
            self.tip = new
            commit = {"oid": new, "signature": {"isValid": True, "state": "VALID"}}
            return httpx.Response(200, json={"data": {"createCommitOnBranch": {"commit": commit}}}), None
        return await super().__call__(client, method, url, provider, action, **kwargs)  # type: ignore[no-untyped-call]


def _tool(name: str):
    executor = MagicMock()
    executor.register = MagicMock()
    build_tools("fake-token", executor)
    return next(call[0][1] for call in executor.register.call_args_list if call[0][0] == name)


def _redis_module_with(redis_instance: AsyncMock) -> ModuleType:
    module = MagicMock(spec=ModuleType)
    module.redis_client = AsyncMock(return_value=redis_instance)
    return module


def _graphql_input(fake: FakeGitHub) -> dict[str, Any]:
    given: dict[str, Any] = fake.payload("post", GRAPHQL)["variables"]["input"]
    return given


# --- create_or_update_github_file: one signed commit per call ---


@pytest.mark.asyncio
async def test_an_existing_file_is_updated_in_a_signed_commit() -> None:
    fake = FakeGitHub()
    with patch(_SAFE_REQUEST, new=fake):
        result = await _tool("create_or_update_github_file")(
            repo="acme/app", path="src/app.py", content="print('hi')\n", message="fix(app): say hi", branch="feat/x"
        )

    commit = _graphql_input(fake)
    assert commit["expectedHeadOid"] == TIP
    assert commit["branch"]["branchName"] == "feat/x"
    assert commit["message"] == {"headline": "fix(app): say hi"}
    assert commit["fileChanges"]["additions"] == [
        {"path": "src/app.py", "contents": base64.b64encode(b"print('hi')\n").decode("ascii")}
    ]
    # No Contents API write, which a user credential leaves unsigned.
    assert not [url for verb, url, _ in fake.calls if "/contents/" in url and verb in ("put", "delete")]
    assert result.splitlines()[0] == f"Updated src/app.py on branch feat/x (commit: {NEW_COMMIT[:8]})"
    assert "Web URL: https://github.com/acme/app/blob/feat/x/src/app.py" in result
    assert "Download URL: https://raw.githubusercontent.com/acme/app/feat/x/src/app.py" in result
    assert _SIGNED in result


@pytest.mark.asyncio
async def test_a_missing_file_is_created_and_a_blank_message_is_conventional() -> None:
    fake = FakeGitHub()
    with patch(_SAFE_REQUEST, new=fake):
        result = await _tool("create_or_update_github_file")(
            repo="acme/app", path="docs/new.md", content="# New\n", message="", branch="main"
        )

    assert _graphql_input(fake)["message"] == {"headline": "chore(github): write docs/new.md"}
    assert result.startswith(f"Created docs/new.md on branch main (commit: {NEW_COMMIT[:8]})")


@pytest.mark.asyncio
async def test_the_default_branch_is_looked_up_when_none_is_given() -> None:
    fake = FakeGitHub(default_branch="trunk")
    with patch(_SAFE_REQUEST, new=fake):
        result = await _tool("create_or_update_github_file")(repo="acme/app", path="a.txt", content="x", message="add a")

    assert fake.urls("get")[0] == API
    assert _graphql_input(fake)["branch"]["branchName"] == "trunk"
    # A non-conventional message is wrapped, as before.
    assert _graphql_input(fake)["message"] == {"headline": "chore(github): add a"}
    assert result.startswith(f"Created a.txt on branch trunk (commit: {NEW_COMMIT[:8]})")


@pytest.mark.asyncio
async def test_a_missing_branch_commits_nothing() -> None:
    fake = FakeGitHub(ref_status=404)
    with patch(_SAFE_REQUEST, new=fake):
        result = await _tool("create_or_update_github_file")(
            repo="acme/app", path="a.txt", content="x", message="chore: x", branch="main"
        )

    assert result.startswith("Error: branch 'main' does not exist in acme/app.")
    assert fake.urls("post") == []


@pytest.mark.asyncio
async def test_a_refused_signed_commit_is_said_and_never_written_unsigned() -> None:
    fake = FakeGitHub(graphql_errors=[{"type": "FORBIDDEN", "message": "Resource not accessible by integration"}])
    with patch(_SAFE_REQUEST, new=fake):
        result = await _tool("create_or_update_github_file")(
            repo="acme/app", path="src/app.py", content="x", message="fix: x", branch="main"
        )

    text = result.content if hasattr(result, "content") else result
    assert "Resource not accessible by integration" in text
    assert not [url for verb, url, _ in fake.calls if "/contents/" in url and verb in ("put", "delete")]


@pytest.mark.asyncio
async def test_writing_a_convention_file_forgets_its_cached_404_in_one_delete() -> None:
    redis = AsyncMock()
    module = _redis_module_with(redis)
    with patch(_SAFE_REQUEST, new=FakeGitHub()), patch.dict(sys.modules, {"app.core.redis": module}):
        await _tool("create_or_update_github_file")(
            repo="acme/app", path="AGENTS.md", content="# Conventions\n", message="docs: x", branch="main"
        )

    digest = _token_digest({"Authorization": "Bearer fake-token"})
    redis.delete.assert_awaited_once_with(
        _file_404_cache_key(digest, "acme", "app", _branch_cache_token("main"), "AGENTS.md"),
        _file_404_cache_key(digest, "acme", "app", _OMITTED_BRANCH_SENTINEL, "AGENTS.md"),
    )


# --- three writes on one branch in one round (#12051) ---


@pytest.mark.asyncio
async def test_parallel_writes_on_one_branch_land_as_signed_commits_in_call_order() -> None:
    fake = ChainedGitHub()
    write = _tool("create_or_update_github_file")
    commit = _tool("create_github_commit")
    delete = _tool("delete_github_file")
    with patch(_SAFE_REQUEST, new=fake):
        results = await asyncio.gather(
            write(repo="acme/app", path="docs/a.md", content="a", message="docs: a", branch="feat/x"),
            commit(repo="acme/app", message="docs: b", files=[{"path": "docs/b.md", "content": "b"}], branch="feat/x"),
            delete(repo="acme/app", path="README.md", message="docs: c", branch="feat/x"),
        )

    texts = [r.content if hasattr(r, "content") else r for r in results]
    assert fake.refused == 0, texts
    assert [headline for _, _, headline in fake.landed] == ["docs: a", "docs: b", "docs: c"]
    # Each commit is built on the tip the previous one left.
    assert [base for base, _, _ in fake.landed] == [TIP] + [new for _, new, _ in fake.landed[:-1]]
    assert texts[0].startswith("Created docs/a.md on branch feat/x")
    assert texts[2].startswith("Deleted README.md from branch feat/x")
    assert all(_SIGNED in text for text in texts)


@pytest.mark.asyncio
async def test_writes_on_other_branches_are_not_held_by_the_lock() -> None:
    entered: list[str] = []
    release = asyncio.Event()

    async def hold(branch: str) -> None:
        async with branch_write_lock("acme/app", branch):
            entered.append(branch)
            await release.wait()

    first = asyncio.create_task(hold("feat/x"))
    second = asyncio.create_task(hold("feat/y"))
    third = asyncio.create_task(hold("feat/x"))
    for _ in range(5):
        await asyncio.sleep(0)
    assert entered == ["feat/x", "feat/y"]
    release.set()
    await asyncio.gather(first, second, third)
    assert entered == ["feat/x", "feat/y", "feat/x"]


# --- the first commit of an empty repository: the Contents API, said unsigned ---


@pytest.mark.asyncio
async def test_the_first_file_of_an_empty_repository_goes_through_the_contents_api() -> None:
    unsigned = {"verified": False, "reason": "unsigned", "signature": None, "payload": None}
    fake = RecordingGitHub(
        {
            ("get", f"{API}/git/ref/heads/main"): (409, {"message": "Git Repository is empty."}),
            ("get", f"{API}/contents/README.md"): (404, {"message": "Not Found"}),
            ("put", f"{API}/contents/README.md"): (201, {"commit": {"sha": "6" * 40, "verification": unsigned}}),
        }
    )
    with patch(_SAFE_REQUEST, new=fake):
        result = await _tool("create_or_update_github_file")(
            repo="acme/app", path="README.md", content="# App\n", message="docs: start", branch="main"
        )

    # The write is the last write; the branch's open pull request is looked up after it (#12067).
    assert [call for call in fake.sent() if call[0] != "get"][-1] == ("put", f"{API}/contents/README.md")
    assert result.startswith(f"Created README.md on branch main (commit: {'6' * 8})\nWarning: ")
    assert "GitHub reports this commit as not signed (unsigned)." in result


# --- delete_github_file: one signed commit per call ---


@pytest.mark.asyncio
async def test_a_file_is_deleted_in_a_signed_commit() -> None:
    fake = FakeGitHub()
    with patch(_SAFE_REQUEST, new=fake):
        result = await _tool("delete_github_file")(
            repo="acme/app", path="src/old.py", message="remove the old module", branch="main"
        )

    commit = _graphql_input(fake)
    assert commit["fileChanges"] == {"deletions": [{"path": "src/old.py"}]}
    assert commit["message"] == {"headline": "chore(github): remove the old module"}
    assert result.startswith(f"Deleted src/old.py from branch main (commit: {NEW_COMMIT[:8]})")
    assert _SIGNED in result


@pytest.mark.asyncio
async def test_a_missing_file_is_not_deleted() -> None:
    fake = FakeGitHub()
    with patch(_SAFE_REQUEST, new=fake):
        result = await _tool("delete_github_file")(repo="acme/app", path="docs/old.md", message="", branch="main")

    assert result.startswith("Error: 'docs/old.md' does not exist on branch 'main' of acme/app")
    assert fake.urls("post") == []


# --- create_github_branch: one ref created from the default branch's tip ---


@pytest.mark.asyncio
async def test_a_branch_is_created_from_the_tip_of_its_base() -> None:
    fake = RecordingGitHub(
        {
            ("get", API): (200, {"default_branch": "main"}),
            ("get", f"{API}/git/ref/heads/main"): (200, {"object": {"sha": BASE_TIP}}),
            ("post", f"{API}/git/refs"): (201, {"ref": "refs/heads/feat/x"}),
        }
    )
    with patch(_SAFE_REQUEST, new=fake):
        result = await _tool("create_github_branch")(repo="acme/app", branch="feat/x")

    assert fake.sent() == [("get", API), ("get", f"{API}/git/ref/heads/main"), ("post", f"{API}/git/refs")]
    assert list(fake.kwargs("post", f"{API}/git/refs")["json"].items()) == [
        ("ref", "refs/heads/feat/x"),
        ("sha", BASE_TIP),
    ]
    assert result == f"Branch 'feat/x' created in acme/app from main (SHA: {BASE_TIP[:8]}), the default branch."


@pytest.mark.asyncio
async def test_a_missing_base_branch_names_the_default_one() -> None:
    fake = RecordingGitHub(
        {
            ("get", API): (200, {"default_branch": "trunk"}),
            ("get", f"{API}/git/ref/heads/main"): (404, {"message": "Not Found"}),
        }
    )
    with patch(_SAFE_REQUEST, new=fake):
        result = await _tool("create_github_branch")(repo="acme/app", branch="feat/x", from_branch="main")

    assert result == "Branch 'main' not found in acme/app. The default branch is 'trunk'. Retry with from_branch='trunk'."
    assert fake.sent() == [("get", API), ("get", f"{API}/git/ref/heads/main")]
