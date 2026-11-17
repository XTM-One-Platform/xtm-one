"""Tests for the GitHub contents tools — get_github_file (directory listing
guard, default-branch resolution, argument coercion, 404 caching),
get_github_repository_tree (non-string ref/path coercion), and the write paths
(create_or_update_github_file / delete_github_file default-branch resolution +
conventional-commit message normalization)."""

import base64
import sys
from types import ModuleType
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.integrations.providers.github import build_tools
from app.integrations.providers.github.contents import (
    _OMITTED_BRANCH_SENTINEL,
    _branch_cache_token,
    _file_404_cache_key,
    _normalize_commit_message,
    _token_digest,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_response(payload) -> httpx.Response:
    """Build a fake httpx.Response with the given JSON payload."""
    return httpx.Response(200, json=payload)


def _get_registered_fn(executor_mock: MagicMock, name: str):
    """Extract a registered async function from the executor mock by tool name."""
    for call in executor_mock.register.call_args_list:
        if call[0][0] == name:
            return call[0][1]
    raise KeyError(f"Tool {name!r} not registered")


def _build_file_response(path: str, content: str) -> dict:
    encoded = base64.b64encode(content.encode()).decode()
    return {
        "type": "file",
        "encoding": "base64",
        "size": len(content),
        "name": path.split("/")[-1],
        "path": path,
        "content": encoded,
    }


# ---------------------------------------------------------------------------
# Redis injection helpers for tests that need to stub the Redis client.
#
# app.core.redis requires the 'redis' package which is not installed in the
# unit-test environment.  Instead of relying on the real package, we inject a
# lightweight fake module into sys.modules so that the
# ``from app.core.redis import redis_client`` calls inside get_github_file get
# our mock without ever touching the real package.
# ---------------------------------------------------------------------------


def _redis_module_unavailable() -> ModuleType:
    """Return a fake app.core.redis module whose redis_client raises (Redis down)."""
    mod = MagicMock(spec=ModuleType)
    mod.redis_client = AsyncMock(side_effect=Exception("redis unavailable"))
    return mod


def _redis_module_with(redis_instance: AsyncMock) -> ModuleType:
    """Return a fake app.core.redis module whose redis_client returns *redis_instance*."""
    mod = MagicMock(spec=ModuleType)
    mod.redis_client = AsyncMock(return_value=redis_instance)
    return mod


#: Digest matching the "fake-token" credential used by build_tools in these
#: tests — the 404 cache key is scoped per credential (a 404 can mean "no
#: access for this token", not just "file absent").
_FAKE_TOKEN_DIGEST = _token_digest({"Authorization": "Bearer fake-token"})


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_github_file_directory_returns_error():
    """When GitHub API returns a list (directory), return a clear error message instead of crashing."""
    mock_executor = MagicMock()
    mock_executor.register = MagicMock()
    build_tools("fake-token", mock_executor)
    get_file_fn = _get_registered_fn(mock_executor, "get_github_file")

    # GitHub returns a list of directory entries when the path is a directory
    directory_listing = [
        {"type": "file", "name": "README.md", "path": "stream/splunk/src/README.md"},
        {"type": "dir", "name": "lib", "path": "stream/splunk/src/lib"},
    ]

    with patch(
        "app.integrations.providers.github._common.safe_request", new_callable=AsyncMock
    ) as mock_req:
        mock_req.return_value = (_mock_response(directory_listing), None)
        result = await get_file_fn(
            repo="OpenCTI-Platform/connectors",
            path="stream/splunk/src",
            branch="main",
        )

    assert "directory" in result.lower()
    assert "stream/splunk/src" in result
    # The error should also surface a few directory entry names so the LLM
    # can pick the right file in a single follow-up call.
    assert "README.md" in result
    assert "lib" in result


@pytest.mark.asyncio
async def test_get_github_file_directory_truncates_listing():
    """Directory listings beyond 20 entries should be truncated with a count hint."""
    mock_executor = MagicMock()
    mock_executor.register = MagicMock()
    build_tools("fake-token", mock_executor)
    get_file_fn = _get_registered_fn(mock_executor, "get_github_file")

    entries = [
        {"type": "file", "name": f"file_{i:03d}.txt", "path": f"dir/file_{i:03d}.txt"}
        for i in range(35)
    ]
    with patch(
        "app.integrations.providers.github._common.safe_request", new_callable=AsyncMock
    ) as mock_req:
        mock_req.return_value = (_mock_response(entries), None)
        result = await get_file_fn(
            repo="owner/repo",
            path="dir",
            branch="main",
        )

    assert "and 15 more" in result
    assert "file_000.txt" in result
    assert "file_019.txt" in result
    assert "file_020.txt" not in result


@pytest.mark.asyncio
async def test_get_github_file_success():
    """When GitHub API returns a valid file, return its decoded content."""
    mock_executor = MagicMock()
    mock_executor.register = MagicMock()
    build_tools("fake-token", mock_executor)
    get_file_fn = _get_registered_fn(mock_executor, "get_github_file")

    file_payload = _build_file_response("README.md", "# Hello World\n")

    with patch(
        "app.integrations.providers.github._common.safe_request", new_callable=AsyncMock
    ) as mock_req:
        mock_req.return_value = (_mock_response(file_payload), None)
        result = await get_file_fn(
            repo="owner/repo",
            path="README.md",
            branch="main",
        )

    assert "Hello World" in result
    assert "README.md" in result


@pytest.mark.asyncio
async def test_get_github_file_non_file_type_returns_error():
    """When GitHub API returns a dict with type != 'file', return an appropriate error."""
    mock_executor = MagicMock()
    mock_executor.register = MagicMock()
    build_tools("fake-token", mock_executor)
    get_file_fn = _get_registered_fn(mock_executor, "get_github_file")

    submodule_payload = {"type": "submodule", "name": "submod", "path": "vendor/submod"}

    with patch(
        "app.integrations.providers.github._common.safe_request", new_callable=AsyncMock
    ) as mock_req:
        mock_req.return_value = (_mock_response(submodule_payload), None)
        result = await get_file_fn(
            repo="owner/repo",
            path="vendor/submod",
            branch="main",
        )

    assert "not a file" in result.lower()
    assert "submodule" in result


@pytest.mark.asyncio
async def test_get_github_file_resolves_default_branch_when_none_specified():
    """When no branch is given, the tool queries the repo API first and uses default_branch."""
    mock_executor = MagicMock()
    mock_executor.register = MagicMock()
    build_tools("fake-token", mock_executor)
    get_file_fn = _get_registered_fn(mock_executor, "get_github_file")

    repo_meta = {"default_branch": "master", "full_name": "OpenCTI-Platform/opencti"}
    file_payload = _build_file_response("opencti-platform/Dockerfile_ubi9", "FROM ubi9\n")

    with patch(
        "app.integrations.providers.github._common.safe_request", new_callable=AsyncMock
    ) as mock_req:
        # First call: repo metadata; second call: file contents
        mock_req.side_effect = [
            (_mock_response(repo_meta), None),
            (_mock_response(file_payload), None),
        ]
        result = await get_file_fn(
            repo="OpenCTI-Platform/opencti",
            path="opencti-platform/Dockerfile_ubi9",
        )

    assert "FROM ubi9" in result
    # The resolved branch should appear in the output header
    assert "master" in result
    # Exactly two HTTP calls: one for repo metadata, one for file contents
    assert mock_req.call_count == 2
    # First call must be the repo metadata lookup
    first_url = mock_req.call_args_list[0][0][2]
    assert first_url.endswith("/repos/OpenCTI-Platform/opencti")
    # Second call must be the file contents request with ?ref=master
    second_kwargs = mock_req.call_args_list[1][1]
    assert second_kwargs.get("params", {}).get("ref") == "master"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "repo_meta",
    [
        {},  # no default_branch key
        {"default_branch": None},  # null default_branch
        {"default_branch": "  "},  # blank default_branch
    ],
)
async def test_get_github_file_uses_main_when_default_branch_missing(repo_meta: dict):
    """When repo metadata lacks a usable default_branch (missing, null, blank), fall back to 'main'."""
    mock_executor = MagicMock()
    mock_executor.register = MagicMock()
    build_tools("fake-token", mock_executor)
    get_file_fn = _get_registered_fn(mock_executor, "get_github_file")

    file_payload = _build_file_response("README.md", "# Hi\n")

    with patch(
        "app.integrations.providers.github._common.safe_request", new_callable=AsyncMock
    ) as mock_req:
        mock_req.side_effect = [
            (_mock_response(repo_meta), None),
            (_mock_response(file_payload), None),
        ]
        result = await get_file_fn(repo="owner/repo", path="README.md")

    assert "# Hi" in result
    second_kwargs = mock_req.call_args_list[1][1]
    assert second_kwargs.get("params", {}).get("ref") == "main"


@pytest.mark.asyncio
async def test_get_github_file_whitespace_branch_resolves_default():
    """A whitespace-only branch is treated as unspecified and resolves the default branch."""
    mock_executor = MagicMock()
    mock_executor.register = MagicMock()
    build_tools("fake-token", mock_executor)
    get_file_fn = _get_registered_fn(mock_executor, "get_github_file")

    repo_meta = {"default_branch": "master"}
    file_payload = _build_file_response("README.md", "# Hi\n")

    with patch(
        "app.integrations.providers.github._common.safe_request", new_callable=AsyncMock
    ) as mock_req:
        mock_req.side_effect = [
            (_mock_response(repo_meta), None),
            (_mock_response(file_payload), None),
        ]
        result = await get_file_fn(repo="owner/repo", path="README.md", branch="  ")

    assert "# Hi" in result
    assert mock_req.call_count == 2
    second_kwargs = mock_req.call_args_list[1][1]
    assert second_kwargs.get("params", {}).get("ref") == "master"


@pytest.mark.asyncio
async def test_get_github_file_explicit_branch_skips_repo_lookup():
    """When a branch is explicitly provided, the repo metadata call is skipped."""
    mock_executor = MagicMock()
    mock_executor.register = MagicMock()
    build_tools("fake-token", mock_executor)
    get_file_fn = _get_registered_fn(mock_executor, "get_github_file")

    file_payload = _build_file_response("README.md", "# Hello\n")

    with patch(
        "app.integrations.providers.github._common.safe_request", new_callable=AsyncMock
    ) as mock_req:
        mock_req.return_value = (_mock_response(file_payload), None)
        result = await get_file_fn(repo="owner/repo", path="README.md", branch="develop")

    assert "# Hello" in result
    # Only one HTTP call — no repo metadata lookup
    assert mock_req.call_count == 1
    only_kwargs = mock_req.call_args_list[0][1]
    assert only_kwargs.get("params", {}).get("ref") == "develop"


@pytest.mark.asyncio
async def test_get_github_file_non_string_branch_resolves_default():
    """A non-string branch (tool-test endpoint bypasses schema coercion) must not crash."""
    mock_executor = MagicMock()
    mock_executor.register = MagicMock()
    build_tools("fake-token", mock_executor)
    get_file_fn = _get_registered_fn(mock_executor, "get_github_file")

    repo_meta = {"default_branch": "master"}
    file_payload = _build_file_response("README.md", "# Hi\n")

    with patch(
        "app.integrations.providers.github._common.safe_request", new_callable=AsyncMock
    ) as mock_req:
        mock_req.side_effect = [
            (_mock_response(repo_meta), None),
            (_mock_response(file_payload), None),
        ]
        # None mimics {"branch": null} sent to the tool-test endpoint
        result = await get_file_fn(repo="owner/repo", path="README.md", branch=None)

    assert "# Hi" in result
    assert mock_req.call_count == 2
    second_kwargs = mock_req.call_args_list[1][1]
    assert second_kwargs.get("params", {}).get("ref") == "master"


@pytest.mark.asyncio
async def test_get_github_file_non_string_or_blank_path_returns_error():
    """A non-string or blank path must fail fast with a friendly error, not hit /contents/."""
    mock_executor = MagicMock()
    mock_executor.register = MagicMock()
    build_tools("fake-token", mock_executor)
    get_file_fn = _get_registered_fn(mock_executor, "get_github_file")

    with patch(
        "app.integrations.providers.github._common.safe_request", new_callable=AsyncMock
    ) as mock_req:
        # None mimics {"path": null} sent to the tool-test endpoint
        result_none = await get_file_fn(repo="owner/repo", path=None)
        result_blank = await get_file_fn(repo="owner/repo", path="   ")

    assert "'path' is required" in result_none
    assert "'path' is required" in result_blank
    # No HTTP call may be made for an invalid path
    assert mock_req.call_count == 0


@pytest.mark.asyncio
async def test_get_github_repository_tree_non_string_ref_and_path_resolve_default():
    """Non-string ref/path/count (tool-test endpoint bypasses schema coercion) must not crash.

    ``ref=None`` degrades to the default-branch resolution path, ``path=None``
    degrades to an unfiltered listing instead of raising ``AttributeError``, and
    ``count=None`` degrades to the default limit instead of raising ``TypeError``.
    """
    mock_executor = MagicMock()
    mock_executor.register = MagicMock()
    build_tools("fake-token", mock_executor)
    get_tree_fn = _get_registered_fn(mock_executor, "get_github_repository_tree")

    repo_meta = {"default_branch": "master"}
    tree_payload = {
        "tree": [
            {"path": "README.md", "type": "blob"},
            {"path": "src", "type": "tree"},
        ],
        "truncated": False,
    }

    with patch(
        "app.integrations.providers.github._common.safe_request", new_callable=AsyncMock
    ) as mock_req:
        mock_req.side_effect = [
            (_mock_response(repo_meta), None),
            (_mock_response(tree_payload), None),
        ]
        # None mimics {"ref": null, "path": null, "count": null} sent to the
        # tool-test endpoint
        result = await get_tree_fn(repo="owner/repo", ref=None, path=None, count=None)

    assert "README.md" in result
    assert "src" in result
    # ref=None resolved the default branch via the repo metadata call
    assert mock_req.call_count == 2
    first_url = mock_req.call_args_list[0][0][2]
    assert first_url.endswith("/repos/owner/repo")
    second_url = mock_req.call_args_list[1][0][2]
    assert second_url.endswith("/git/trees/master")


@pytest.mark.asyncio
async def test_get_github_file_repo_lookup_error_returns_error():
    """When the repo metadata lookup fails, surface the error immediately."""
    mock_executor = MagicMock()
    mock_executor.register = MagicMock()
    build_tools("fake-token", mock_executor)
    get_file_fn = _get_registered_fn(mock_executor, "get_github_file")

    with patch(
        "app.integrations.providers.github._common.safe_request", new_callable=AsyncMock
    ) as mock_req:
        mock_req.return_value = (None, "GitHub resource not found: owner/missing-repo")
        result = await get_file_fn(repo="owner/missing-repo", path="README.md")

    assert "not found" in result.lower()
    # Must not make a second HTTP call
    assert mock_req.call_count == 1


@pytest.mark.asyncio
async def test_resolve_branch_404_appends_actionable_repo_not_found_hint():
    """When GET /repos/{owner}/{name} returns 404 (e.g. repo renamed/deleted or
    integration lost access), _resolve_branch must append a clear, actionable
    hint so the LLM and the user can diagnose and fix the stale config reference
    ('Filigran-SAS/xtm-hub not found')."""
    mock_executor = MagicMock()
    mock_executor.register = MagicMock()
    build_tools("fake-token", mock_executor)
    get_tree_fn = _get_registered_fn(mock_executor, "get_github_repository_tree")

    not_found_err = (
        "Error: GitHub resource not found while trying to get default branch "
        "for Filigran-SAS/xtm-hub. Detail: Not Found"
    )

    with patch(
        "app.integrations.providers.github._common.safe_request", new_callable=AsyncMock
    ) as mock_req:
        # Only the repo-metadata call fires; no tree call because _resolve_branch
        # returns early with the error. The metadata call opts into receiving
        # the failed response (expected_statuses=set()), so the mock returns
        # the real 404 response alongside the error string.
        mock_req.return_value = (httpx.Response(404, json={"message": "Not Found"}), not_found_err)
        result = await get_tree_fn(repo="Filigran-SAS/xtm-hub")

    # The original error text is preserved
    assert "not found" in result.lower()
    assert "Filigran-SAS/xtm-hub" in result
    # The actionable hint is appended
    assert "renamed" in result
    assert "re-authorized" in result
    # Exactly one HTTP call — no retry or tree fetch after a 404
    assert mock_req.call_count == 1
    # The metadata call must opt into the failed-response contract so the
    # hint keys off resp.status_code, not error-message text.
    assert mock_req.call_args_list[0][1].get("expected_statuses") == set()


@pytest.mark.asyncio
async def test_resolve_branch_non_404_error_not_extended():
    """Non-404 errors (e.g. 429 rate limit) from the repo-metadata call must
    pass through _resolve_branch unchanged — only real 404s get the
    stale-config hint."""
    mock_executor = MagicMock()
    mock_executor.register = MagicMock()
    build_tools("fake-token", mock_executor)
    get_tree_fn = _get_registered_fn(mock_executor, "get_github_repository_tree")

    rate_limit_err = "Error: GitHub rate limit exceeded while trying to get default branch for owner/repo. Wait a moment and try again."

    with patch(
        "app.integrations.providers.github._common.safe_request", new_callable=AsyncMock
    ) as mock_req:
        mock_req.return_value = (httpx.Response(429, json={"message": "rate limited"}), rate_limit_err)
        result = await get_tree_fn(repo="owner/repo")

    # Original error text is returned verbatim — no stale-config hint appended
    assert result == rate_limit_err
    assert "renamed" not in result
    assert "re-authorized" not in result


@pytest.mark.asyncio
async def test_resolve_branch_non_404_error_mentioning_not_found_not_extended():
    """The 404 detection keys off the HTTP status code, not the error text: a
    non-404 failure whose detail happens to contain the words 'not found'
    (e.g. a 502 from a proxy) must NOT get the stale-config hint."""
    mock_executor = MagicMock()
    mock_executor.register = MagicMock()
    build_tools("fake-token", mock_executor)
    get_tree_fn = _get_registered_fn(mock_executor, "get_github_repository_tree")

    proxy_err = (
        "Error: GitHub server error (502) while trying to get default branch "
        "for owner/repo. Detail: upstream not found"
    )

    with patch(
        "app.integrations.providers.github._common.safe_request", new_callable=AsyncMock
    ) as mock_req:
        mock_req.return_value = (httpx.Response(502, json={"message": "upstream not found"}), proxy_err)
        result = await get_tree_fn(repo="owner/repo")

    assert result == proxy_err
    assert "renamed" not in result
    assert "re-authorized" not in result


@pytest.mark.asyncio
async def test_create_or_update_github_file_resolves_default_branch_when_unspecified():
    """Write operations should resolve the repository default branch when branch is omitted."""
    from tests.test_github_commit import _SAFE_REQUEST, API, GRAPHQL, FakeGitHub

    mock_executor = MagicMock()
    mock_executor.register = MagicMock()
    build_tools("fake-token", mock_executor)
    write_fn = _get_registered_fn(mock_executor, "create_or_update_github_file")

    fake = FakeGitHub(default_branch="master")
    with patch(_SAFE_REQUEST, new=fake):
        result = await write_fn(
            repo="acme/app", path=".github/dummy_never_use.txt", content="noop", message="dummy probe"
        )

    assert "Created .github/dummy_never_use.txt on branch master" in result
    # GET /repos/... to resolve the default branch, then the commit on it.
    assert fake.urls("get")[0] == API
    assert fake.payload("post", GRAPHQL)["variables"]["input"]["branch"]["branchName"] == "master"


@pytest.mark.asyncio
async def test_create_or_update_github_file_surfaces_a_failed_branch_read() -> None:
    """A failure reading the branch (403, 5xx) must abort before any commit."""
    mock_executor = MagicMock()
    mock_executor.register = MagicMock()
    build_tools("fake-token", mock_executor)
    write_fn = _get_registered_fn(mock_executor, "create_or_update_github_file")

    with patch(
        "app.integrations.providers.github._common.safe_request", new_callable=AsyncMock
    ) as mock_req:
        mock_req.return_value = (
            httpx.Response(403, json={"message": "Forbidden"}),
            "GitHub API error (403): Forbidden",
        )
        result = await write_fn(
            repo="owner/repo",
            path="docs/a.md",
            content="noop",
            message="chore: x",
            branch="main",
        )

    assert "403" in result
    # The failure must short-circuit: nothing is written afterwards.
    assert mock_req.call_count == 1


@pytest.mark.asyncio
async def test_create_or_update_github_file_normalizes_non_conventional_message():
    """Non-conventional commit messages should be normalized to a conventional commit."""
    from tests.test_github_commit import _SAFE_REQUEST, GRAPHQL, FakeGitHub

    mock_executor = MagicMock()
    mock_executor.register = MagicMock()
    build_tools("fake-token", mock_executor)
    write_fn = _get_registered_fn(mock_executor, "create_or_update_github_file")

    fake = FakeGitHub()
    with patch(_SAFE_REQUEST, new=fake):
        result = await write_fn(
            repo="acme/app", path="README.md", content="noop", message="dummy probe", branch="master"
        )

    assert "Updated README.md on branch master" in result
    headline = fake.payload("post", GRAPHQL)["variables"]["input"]["message"]["headline"]
    assert headline == "chore(github): dummy probe"


@pytest.mark.asyncio
async def test_delete_github_file_resolves_default_branch_and_normalizes_message():
    """Deletes resolve the default branch when omitted and normalize the commit message."""
    from tests.test_github_commit import _SAFE_REQUEST, API, GRAPHQL, FakeGitHub

    mock_executor = MagicMock()
    mock_executor.register = MagicMock()
    build_tools("fake-token", mock_executor)
    delete_fn = _get_registered_fn(mock_executor, "delete_github_file")

    fake = FakeGitHub(default_branch="master")
    with patch(_SAFE_REQUEST, new=fake):
        result = await delete_fn(repo="acme/app", path="src/old.py", message="remove obsolete doc")

    assert "Deleted src/old.py from branch master" in result
    assert fake.urls("get")[0] == API
    commit = fake.payload("post", GRAPHQL)["variables"]["input"]
    assert commit["message"] == {"headline": "chore(github): remove obsolete doc"}
    assert commit["branch"]["branchName"] == "master"
    assert commit["fileChanges"] == {"deletions": [{"path": "src/old.py"}]}


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        # Already-compliant subjects are preserved untouched.
        ("fix(deps): bump httpx to 0.28 (#123)", "fix(deps): bump httpx to 0.28 (#123)"),
        ("chore: dummy probe", "chore: dummy probe"),
        ("feat(api)!: breaking change", "feat(api)!: breaking change"),
        # Non-compliant subjects are wrapped.
        ("dummy probe", "chore(github): dummy probe"),
        ("Update README", "chore(github): Update README"),
        # A scope outside the ruleset charset ([A-Za-z0-9_.-]) is wrapped too,
        # since the target repo's commit-message ruleset would reject it as-is.
        ("fix(my scope): x", "chore(github): fix(my scope): x"),
        # Blank / non-string messages fall back to an action-based subject.
        ("", "chore(github): create docs/a.md"),
        ("   ", "chore(github): create docs/a.md"),
        (None, "chore(github): create docs/a.md"),
        # Multi-line: only the subject is validated; the body is preserved.
        ("fix: subject\n\nbody line", "fix: subject\n\nbody line"),
        ("subject only\n\nbody line", "chore(github): subject only\n\nbody line"),
    ],
)
def test_normalize_commit_message_shapes(message, expected):
    assert _normalize_commit_message(message, "docs/a.md", "create") == expected


def test_normalize_commit_message_delete_fallback():
    assert _normalize_commit_message(None, "docs/a.md", "delete") == "chore(github): delete docs/a.md"


# ---------------------------------------------------------------------------
# get_github_file — 404 caching and log-suppression
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_github_file_404_returns_file_not_found_message():
    """A 404 response returns 'File not found:' (not 'Error:') to keep WARNING
    logs silent in the agent loop, which only WARNs when content starts with
    'Error'."""
    mock_executor = MagicMock()
    mock_executor.register = MagicMock()
    build_tools("fake-token", mock_executor)
    get_file_fn = _get_registered_fn(mock_executor, "get_github_file")

    with patch(
        "app.integrations.providers.github._common.safe_request", new_callable=AsyncMock
    ) as mock_req, patch.dict(sys.modules, {"app.core.redis": _redis_module_unavailable()}):
        mock_req.side_effect = [
            (_mock_response({"default_branch": "master"}), None),
            (
                httpx.Response(404, json={"message": "Not Found"}),
                "Error: GitHub resource not found while trying to read file AGENTS.md "
                "in FiligranHQ/platform (branch: master). Detail: Not Found",
            ),
        ]
        result = await get_file_fn(
            repo="FiligranHQ/platform",
            path="AGENTS.md",
        )

    assert result.startswith("File not found:"), f"Expected 'File not found:' prefix, got: {result!r}"
    assert "AGENTS.md" in result
    assert "FiligranHQ/platform" in result
    assert "master" in result
    # Must not start with "Error:" (would trigger agent_loop WARN)
    assert not result.startswith("Error:")


@pytest.mark.asyncio
async def test_get_github_file_404_caches_negative_result_in_redis():
    """After a real 404, the file path is stored in Redis so the next call is
    served from cache without hitting GitHub again."""
    mock_executor = MagicMock()
    mock_executor.register = MagicMock()
    build_tools("fake-token", mock_executor)
    get_file_fn = _get_registered_fn(mock_executor, "get_github_file")

    mock_redis = AsyncMock()
    mock_redis.get.return_value = None  # cache miss on first call

    with patch(
        "app.integrations.providers.github._common.safe_request", new_callable=AsyncMock
    ) as mock_req, patch.dict(sys.modules, {"app.core.redis": _redis_module_with(mock_redis)}):
        mock_req.side_effect = [
            # Explicit branch="master" means _resolve_branch makes no HTTP call,
            # so only the file-contents request fires — and it returns 404.
            (
                httpx.Response(404, json={"message": "Not Found"}),
                "Error: GitHub resource not found",
            ),
        ]
        result = await get_file_fn(
            repo="FiligranHQ/platform",
            path="AGENTS.md",
            branch="master",
        )

    assert "File not found" in result
    # Redis.set must be called with the canonical cache key and a TTL
    mock_redis.set.assert_called_once()
    set_key = mock_redis.set.call_args[0][0]
    assert set_key == _file_404_cache_key(
        _FAKE_TOKEN_DIGEST, "FiligranHQ", "platform", _branch_cache_token("master"), "AGENTS.md"
    )
    set_kwargs = mock_redis.set.call_args[1]
    assert "ex" in set_kwargs and set_kwargs["ex"] > 0


@pytest.mark.asyncio
async def test_get_github_file_serves_cached_404_without_http_call():
    """When the Redis negative-lookup cache has a hit, get_github_file returns
    'File not found' immediately — no HTTP call to GitHub is made (zero HTTP
    round-trips after the first run that cached the 404)."""
    mock_executor = MagicMock()
    mock_executor.register = MagicMock()
    build_tools("fake-token", mock_executor)
    get_file_fn = _get_registered_fn(mock_executor, "get_github_file")

    mock_redis = AsyncMock()
    mock_redis.get.return_value = b"1"  # cache hit

    with patch(
        "app.integrations.providers.github._common.safe_request", new_callable=AsyncMock
    ) as mock_req, patch.dict(sys.modules, {"app.core.redis": _redis_module_with(mock_redis)}):
        # Explicit branch so that _resolve_branch skips the repo-metadata call
        result = await get_file_fn(
            repo="FiligranHQ/platform",
            path="AGENTS.md",
            branch="master",
        )

    assert "File not found" in result
    assert "AGENTS.md" in result
    # The cached path must make ZERO HTTP calls (no branch resolution, no file
    # fetch — everything is served from the in-memory Redis hit).
    assert mock_req.call_count == 0, "HTTP call should be skipped when 404 is cached"


@pytest.mark.asyncio
async def test_get_github_file_cached_404_skips_default_branch_resolution():
    """A cached miss with the branch omitted must short-circuit BEFORE the
    default-branch repo-metadata lookup — truly zero HTTP calls. The
    omitted-branch case is cached under the _OMITTED_BRANCH_SENTINEL token."""
    mock_executor = MagicMock()
    mock_executor.register = MagicMock()
    build_tools("fake-token", mock_executor)
    get_file_fn = _get_registered_fn(mock_executor, "get_github_file")

    mock_redis = AsyncMock()
    mock_redis.get.return_value = b"1"  # cache hit

    with patch(
        "app.integrations.providers.github._common.safe_request", new_callable=AsyncMock
    ) as mock_req, patch.dict(sys.modules, {"app.core.redis": _redis_module_with(mock_redis)}):
        result = await get_file_fn(repo="FiligranHQ/platform", path="AGENTS.md")

    assert "File not found" in result
    assert mock_req.call_count == 0, "repo-metadata branch resolution must be skipped on a cache hit"
    mock_redis.get.assert_called_once_with(
        _file_404_cache_key(_FAKE_TOKEN_DIGEST, "FiligranHQ", "platform", _OMITTED_BRANCH_SENTINEL, "AGENTS.md")
    )


def test_branch_cache_token_cannot_collide_with_omitted_branch():
    """The omitted-branch token and explicit-branch tokens live in disjoint
    namespaces: the omitted token contains '@{' (forbidden in git ref names by
    git check-ref-format) and explicit branches are prefixed with 'ref:', so
    even a pathological explicit request for the omitted token's literal value
    cannot share the omitted-branch cache key."""
    assert _branch_cache_token("") == _OMITTED_BRANCH_SENTINEL
    assert "@{" in _OMITTED_BRANCH_SENTINEL
    assert _branch_cache_token("main") == "ref:main"
    # An explicit branch equal to the omitted token's literal value still maps
    # to the explicit namespace.
    assert _branch_cache_token(_OMITTED_BRANCH_SENTINEL) != _OMITTED_BRANCH_SENTINEL
    # No explicit branch can ever produce the omitted token (prefix differs).
    assert not _OMITTED_BRANCH_SENTINEL.startswith("ref:")


@pytest.mark.asyncio
async def test_get_github_file_404_not_cached_for_arbitrary_paths():
    """404s for paths outside the convention-file allow-list must NOT be
    cached — arbitrary reads keep their exact semantics (a freshly-pushed file
    is immediately visible) and agents cannot mint unbounded Redis keys."""
    mock_executor = MagicMock()
    mock_executor.register = MagicMock()
    build_tools("fake-token", mock_executor)
    get_file_fn = _get_registered_fn(mock_executor, "get_github_file")

    mock_redis = AsyncMock()
    mock_redis.get.return_value = None

    with patch(
        "app.integrations.providers.github._common.safe_request", new_callable=AsyncMock
    ) as mock_req, patch.dict(sys.modules, {"app.core.redis": _redis_module_with(mock_redis)}):
        mock_req.side_effect = [
            (
                httpx.Response(404, json={"message": "Not Found"}),
                "Error: GitHub resource not found",
            ),
        ]
        result = await get_file_fn(repo="owner/repo", path="src/some/module.py", branch="main")

    # The friendly non-"Error:" message still applies to any 404 ...
    assert result.startswith("File not found:")
    # ... but nothing is read from or written to the cache.
    mock_redis.get.assert_not_called()
    mock_redis.set.assert_not_called()


@pytest.mark.asyncio
async def test_get_github_file_404_not_cached_for_non_canonical_casing():
    """Allow-list matching is case-sensitive against the canonical spellings:
    git paths are case-sensitive, so 'agents.md' (which may exist while
    'AGENTS.md' 404s, or vice versa) must not share cache entries — and
    casing variants must not mint extra Redis keys."""
    mock_executor = MagicMock()
    mock_executor.register = MagicMock()
    build_tools("fake-token", mock_executor)
    get_file_fn = _get_registered_fn(mock_executor, "get_github_file")

    mock_redis = AsyncMock()
    mock_redis.get.return_value = None

    with patch(
        "app.integrations.providers.github._common.safe_request", new_callable=AsyncMock
    ) as mock_req, patch.dict(sys.modules, {"app.core.redis": _redis_module_with(mock_redis)}):
        mock_req.side_effect = [
            (
                httpx.Response(404, json={"message": "Not Found"}),
                "Error: GitHub resource not found",
            ),
        ]
        result = await get_file_fn(repo="owner/repo", path="AgEnTs.Md", branch="main")

    assert result.startswith("File not found:")
    mock_redis.get.assert_not_called()
    mock_redis.set.assert_not_called()


@pytest.mark.asyncio
async def test_get_github_file_redis_failure_falls_through_to_http():
    """When Redis is unavailable, get_github_file falls through to the normal
    HTTP call — Redis failures must never block a legitimate file read."""
    mock_executor = MagicMock()
    mock_executor.register = MagicMock()
    build_tools("fake-token", mock_executor)
    get_file_fn = _get_registered_fn(mock_executor, "get_github_file")

    # Use an allow-listed convention file so the cache path is actually
    # consulted (arbitrary paths skip the cache entirely).
    file_payload = _build_file_response("AGENTS.md", "# Hello from fallback\n")

    with patch(
        "app.integrations.providers.github._common.safe_request", new_callable=AsyncMock
    ) as mock_req, patch.dict(sys.modules, {"app.core.redis": _redis_module_unavailable()}):
        mock_req.side_effect = [
            (_mock_response({"default_branch": "main"}), None),
            (_mock_response(file_payload), None),
        ]
        result = await get_file_fn(repo="owner/repo", path="AGENTS.md")

    assert "Hello from fallback" in result
    # Both HTTP calls (branch resolution + file read) must still happen
    assert mock_req.call_count == 2


@pytest.mark.asyncio
async def test_get_github_file_404_expected_statuses_suppresses_warning():
    """safe_request is called with expected_statuses={404} for get_github_file
    so that the HTTP WARNING log is suppressed for missing convention files."""
    mock_executor = MagicMock()
    mock_executor.register = MagicMock()
    build_tools("fake-token", mock_executor)
    get_file_fn = _get_registered_fn(mock_executor, "get_github_file")

    with patch(
        "app.integrations.providers.github._common.safe_request", new_callable=AsyncMock
    ) as mock_req, patch.dict(sys.modules, {"app.core.redis": _redis_module_unavailable()}):
        mock_req.side_effect = [
            (_mock_response({"default_branch": "main"}), None),
            (
                httpx.Response(404, json={"message": "Not Found"}),
                "Error: not found",
            ),
        ]
        await get_file_fn(repo="owner/repo", path="AGENTS.md")

    # The file-contents GET call (second call) must use expected_statuses={404}
    contents_call = mock_req.call_args_list[1]
    assert 404 in (contents_call[1].get("expected_statuses") or set()), (
        "expected_statuses={404} must be passed to suppress WARNING log for missing files"
    )


def test_file_404_cache_key_is_case_insensitive_for_repo():
    """Cache keys must be identical regardless of the capitalisation used in
    the repository owner or name (GitHub repos are case-insensitive)."""
    key_lower = _file_404_cache_key(_FAKE_TOKEN_DIGEST, "filigranhq", "platform", "master", "AGENTS.md")
    key_upper = _file_404_cache_key(_FAKE_TOKEN_DIGEST, "FiligranHQ", "Platform", "master", "AGENTS.md")
    assert key_lower == key_upper


def test_file_404_cache_key_is_scoped_per_credential():
    """Different credentials must never share a cached 404 — GitHub answers 404
    both for a missing file and for a private repo the token cannot access, so
    a miss observed by one integration must not shadow another integration's
    legitimate read. The digest must also never embed the raw token."""
    digest_a = _token_digest({"Authorization": "Bearer token-a"})
    digest_b = _token_digest({"Authorization": "Bearer token-b"})
    assert digest_a != digest_b
    assert "token-a" not in digest_a
    key_a = _file_404_cache_key(digest_a, "owner", "repo", "main", "AGENTS.md")
    key_b = _file_404_cache_key(digest_b, "owner", "repo", "main", "AGENTS.md")
    assert key_a != key_b


@pytest.mark.asyncio
async def test_create_or_update_github_file_invalidates_cached_404():
    """A successful write must delete the writer's cached 404 for that file so
    an immediate get_github_file re-read is not served a stale 'File not
    found' for the rest of the cache TTL."""
    mock_executor = MagicMock()
    mock_executor.register = MagicMock()
    build_tools("fake-token", mock_executor)
    write_fn = _get_registered_fn(mock_executor, "create_or_update_github_file")

    from tests.test_github_commit import _SAFE_REQUEST, FakeGitHub

    mock_redis = AsyncMock()

    with (
        patch(_SAFE_REQUEST, new=FakeGitHub()),
        patch.dict(sys.modules, {"app.core.redis": _redis_module_with(mock_redis)}),
    ):
        result = await write_fn(
            repo="acme/app",
            path="AGENTS.md",
            content="# Conventions\n",
            message="docs: add AGENTS.md",
            branch="master",
        )

    assert "Updated AGENTS.md on branch master" in result
    # Both the explicit-branch key and the omitted-branch token key are
    # dropped — the miss may have been cached under either form.
    mock_redis.delete.assert_called_once_with(
        _file_404_cache_key(_FAKE_TOKEN_DIGEST, "acme", "app", _branch_cache_token("master"), "AGENTS.md"),
        _file_404_cache_key(_FAKE_TOKEN_DIGEST, "acme", "app", _OMITTED_BRANCH_SENTINEL, "AGENTS.md"),
    )
