"""create_github_branch at a commit SHA: a branch first, then a commit, else one clear refusal."""

from unittest.mock import MagicMock, patch

import httpx
import pytest

from app.integrations.providers.github import build_tools

_SAFE_REQUEST = "app.integrations.providers.github._common.safe_request"

API = "https://api.github.com/repos/acme/app"
BASE_TIP = "7" * 40
OLD_COMMIT = "abcdef0123456789abcdef0123456789abcdef01"


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


def _tool(name: str):
    executor = MagicMock()
    executor.register = MagicMock()
    build_tools("fake-token", executor)
    return next(call[0][1] for call in executor.register.call_args_list if call[0][0] == name)


@pytest.mark.asyncio
async def test_a_branch_name_keeps_its_requests_and_answer():
    fake = RecordingGitHub(
        {
            ("get", API): (200, {"default_branch": "main"}),
            ("get", f"{API}/git/ref/heads/main"): (200, {"object": {"sha": BASE_TIP}}),
            ("post", f"{API}/git/refs"): (201, {"ref": "refs/heads/feat/x"}),
        }
    )
    with patch(_SAFE_REQUEST, new=fake):
        result = await _tool("create_github_branch")(repo="acme/app", branch="feat/x", from_branch="main")

    assert fake.sent() == [("get", API), ("get", f"{API}/git/ref/heads/main"), ("post", f"{API}/git/refs")]
    assert result == f"Branch 'feat/x' created in acme/app from main (SHA: {BASE_TIP[:8]}), the default branch."


@pytest.mark.asyncio
@pytest.mark.parametrize("given", [OLD_COMMIT, OLD_COMMIT[:7]])
async def test_a_commit_sha_creates_the_branch_at_that_commit(given):
    fake = RecordingGitHub(
        {
            ("get", API): (200, {"default_branch": "main"}),
            ("get", f"{API}/git/ref/heads/{given}"): (404, {"message": "Not Found"}),
            ("get", f"{API}/commits/{given}"): (200, {"sha": OLD_COMMIT}),
            ("post", f"{API}/git/refs"): (201, {"ref": "refs/heads/scratch/x"}),
        }
    )
    with patch(_SAFE_REQUEST, new=fake):
        result = await _tool("create_github_branch")(repo="acme/app", branch="scratch/x", from_branch=given)

    assert fake.sent() == [
        ("get", API),
        ("get", f"{API}/git/ref/heads/{given}"),
        ("get", f"{API}/commits/{given}"),
        ("post", f"{API}/git/refs"),
    ]
    assert fake.kwargs("post", f"{API}/git/refs")["json"] == {"ref": "refs/heads/scratch/x", "sha": OLD_COMMIT}
    assert result == f"Branch 'scratch/x' created in acme/app at commit {OLD_COMMIT} (ref refs/heads/scratch/x)"


@pytest.mark.asyncio
async def test_an_unknown_sha_is_refused_in_one_line_and_nothing_is_created():
    fake = RecordingGitHub(
        {
            ("get", API): (200, {"default_branch": "main"}),
            ("get", f"{API}/git/ref/heads/deadbeef"): (404, {"message": "Not Found"}),
            ("get", f"{API}/commits/deadbeef"): (422, {"message": "No commit found for SHA: deadbeef"}),
        }
    )
    with patch(_SAFE_REQUEST, new=fake):
        result = await _tool("create_github_branch")(repo="acme/app", branch="scratch/x", from_branch="deadbeef")

    assert result == "Branch or commit not found: deadbeef (in acme/app; nothing created)."
    assert ("post", f"{API}/git/refs") not in fake.sent()


@pytest.mark.asyncio
async def test_an_unknown_branch_name_still_names_the_default_branch():
    fake = RecordingGitHub(
        {
            ("get", API): (200, {"default_branch": "main"}),
            ("get", f"{API}/git/ref/heads/develop"): (404, {"message": "Not Found"}),
        }
    )
    with patch(_SAFE_REQUEST, new=fake):
        result = await _tool("create_github_branch")(repo="acme/app", branch="feat/x", from_branch="develop")

    assert result == "Branch 'develop' not found in acme/app. The default branch is 'main'. Retry with from_branch='main'."
    assert fake.sent() == [("get", API), ("get", f"{API}/git/ref/heads/develop")]
