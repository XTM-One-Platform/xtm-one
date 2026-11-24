"""``merge_github_branch``: a ref merged into a branch server-side.

A fake GitHub answers the Merges API and the compare reads and records every
request: the tests check the one merge request, the no-op, the refusals, and
the conflict answer naming the paths changed on both sides.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import pytest

from app.integrations.providers.github import build_tools
from app.tools.base import ToolResult

_SAFE_REQUEST = "app.integrations.providers.github._common.safe_request"

API = "https://api.github.com/repos/acme/app"
MERGE = "5" * 40
BASE = "b" * 40


class FakeGitHub:
    def __init__(self, *, merge_status: int = 201, ours: list[dict] | None = None, theirs: list[dict] | None = None):
        self.merge_status = merge_status
        self.ours = ours or []
        self.theirs = theirs or []
        self.calls: list[tuple[str, str, dict]] = []

    def urls(self, method: str | None = None) -> list[str]:
        return [url for verb, url, _ in self.calls if method is None or verb == method]

    async def __call__(self, client, method, url, provider, action, **kwargs):
        self.calls.append((method, url, kwargs))

        def ok(payload, status: int = 200):
            return httpx.Response(status, json=payload), None

        if method == "get" and url == API:
            return ok({"default_branch": "main"})
        if method == "post" and url == f"{API}/merges":
            if self.merge_status == 201:
                commit = {"verification": {"verified": True, "reason": "valid", "signature": "sig"}}
                return ok({"sha": MERGE, "commit": commit}, 201)
            if self.merge_status == 204:
                return httpx.Response(204), None
            message = {404: "Not Found", 409: "Merge conflict"}[self.merge_status]
            resp = httpx.Response(self.merge_status, json={"message": message})
            return resp, f"Error: GitHub API error ({self.merge_status}): {message}"
        if method == "get" and url == f"{API}/compare/feat/x...main":
            return ok({"merge_base_commit": {"sha": BASE}, "files": self.theirs})
        if method == "get" and url == f"{API}/compare/{BASE}...feat/x":
            return ok({"files": self.ours})
        if method == "get" and url.startswith(f"{API}/commits/"):
            # No tree to read: the line merge does not run and the answer lists the paths (test_github_merge_hunks).
            return httpx.Response(404, json={"message": "Not Found"}), "Error: GitHub API error (404): Not Found"
        raise AssertionError(f"unexpected {method.upper()} {url}")


def _merge_tool():
    executor = MagicMock()
    executor.register = MagicMock()
    definitions = build_tools("fake-token", executor)
    handler = next(call[0][1] for call in executor.register.call_args_list if call[0][0] == "merge_github_branch")
    definition = next(d for d in definitions if d.name == "merge_github_branch")
    return handler, definition


@pytest.mark.asyncio
async def test_a_clean_merge_is_one_merges_request_from_the_default_branch():
    merge, _ = _merge_tool()
    fake = FakeGitHub()
    with patch(_SAFE_REQUEST, new=fake):
        result = await merge(repo="acme/app", branch="feat/x")

    assert result == f"Merged 'main' into branch 'feat/x' of acme/app: merge commit {MERGE}. GitHub reports it as verified."
    assert fake.urls("post") == [f"{API}/merges"]
    assert fake.calls[-1][2]["json"] == {"base": "feat/x", "head": "main", "commit_message": "Merge main into feat/x"}
    assert fake.calls[-1][2]["expected_statuses"] == {404, 409}


@pytest.mark.asyncio
async def test_from_ref_and_message_are_passed_through():
    merge, _ = _merge_tool()
    fake = FakeGitHub()
    with patch(_SAFE_REQUEST, new=fake):
        await merge(repo="acme/app", branch="feat/x", from_ref="release/1.2", message="chore: sync release")

    assert fake.calls[-1][2]["json"] == {"base": "feat/x", "head": "release/1.2", "commit_message": "chore: sync release"}


@pytest.mark.asyncio
async def test_an_up_to_date_branch_is_a_no_op():
    merge, _ = _merge_tool()
    fake = FakeGitHub(merge_status=204)
    with patch(_SAFE_REQUEST, new=fake):
        result = await merge(repo="acme/app", branch="feat/x")

    assert isinstance(result, ToolResult) and result.no_op
    assert "already contains 'main'" in result.content


@pytest.mark.asyncio
async def test_a_conflict_merges_nothing_and_names_the_paths_changed_on_both_sides():
    merge, _ = _merge_tool()
    fake = FakeGitHub(
        merge_status=409,
        theirs=[{"filename": "src/a.py"}, {"filename": "src/only_main.py"}, {"filename": "docs/new.md", "previous_filename": "docs/old.md"}],
        ours=[{"filename": "src/a.py"}, {"filename": "docs/old.md"}, {"filename": "src/only_branch.py"}],
    )
    with patch(_SAFE_REQUEST, new=fake):
        result = await merge(repo="acme/app", branch="feat/x")

    assert result.startswith("Error: merging 'main' into 'feat/x' of acme/app conflicts, so nothing was merged.")
    assert f"Merge base {BASE[:8]}" in result
    assert "Changed on both sides since the merge base (2):\n- docs/old.md\n- src/a.py" in result
    assert "only_main" not in result and "only_branch" not in result
    assert fake.urls("post") == [f"{API}/merges"]


@pytest.mark.asyncio
async def test_a_conflict_names_every_path_to_resolve_past_forty():
    merge, _ = _merge_tool()
    files = [{"filename": f"src/m{n:02d}.py"} for n in range(61)]
    fake = FakeGitHub(merge_status=409, theirs=files, ours=files)
    with patch(_SAFE_REQUEST, new=fake):
        result = await merge(repo="acme/app", branch="feat/x")

    assert "Changed on both sides since the merge base (61):" in result
    assert all(f"- src/m{n:02d}.py" in result for n in range(61))
    assert "more\n" not in result and not result.endswith("more")


@pytest.mark.asyncio
async def test_a_path_changed_the_same_way_on_both_sides_is_not_a_conflict():
    merge, _ = _merge_tool()
    fake = FakeGitHub(
        merge_status=409,
        theirs=[{"filename": "i18n/en.json", "sha": "e" * 40}, {"filename": "src/a.py", "sha": "1" * 40}],
        ours=[{"filename": "i18n/en.json", "sha": "e" * 40}, {"filename": "src/a.py", "sha": "2" * 40}],
    )
    with patch(_SAFE_REQUEST, new=fake):
        result = await merge(repo="acme/app", branch="feat/x")

    assert "Changed on both sides since the merge base (1):\n- src/a.py" in result
    assert "Changed the same way on both sides, no resolution needed (1):\n- i18n/en.json" in result


@pytest.mark.asyncio
async def test_update_branch_on_a_conflict_answers_the_paths_to_resolve():
    executor = MagicMock()
    executor.register = MagicMock()
    build_tools("fake-token", executor)
    manage = next(call[0][1] for call in executor.register.call_args_list if call[0][0] == "manage_github_pr")
    fake = FakeGitHub(merge_status=409, theirs=[{"filename": "src/a.py"}], ours=[{"filename": "src/a.py"}])
    real = fake.__call__

    async def github(client, method, url, provider, action, **kwargs):
        if method == "put" and url == f"{API}/pulls/7/update-branch":
            assert kwargs["expected_statuses"] == {422}
            return httpx.Response(422, json={"message": "merge conflict between base and head"}), "Error: 422"
        if method == "get" and url == f"{API}/pulls/7":
            head = {"ref": "feat/x", "repo": {"full_name": "acme/app"}}
            return httpx.Response(200, json={"head": head, "base": {"ref": "main"}}), None
        return await real(client, method, url, provider, action, **kwargs)

    with patch(_SAFE_REQUEST, new=github):
        result = await manage(repo="acme/app", number=7, action="update_branch")

    assert result.startswith("Error: the branch of PR #7 in acme/app cannot take 'main'")
    assert "merge_github_branch (branch 'feat/x', from_ref 'main')" in result and "- src/a.py" in result
    assert fake.urls("post") == []


@pytest.mark.asyncio
async def test_update_branch_refused_for_another_reason_is_passed_through():
    executor = MagicMock()
    executor.register = MagicMock()
    build_tools("fake-token", executor)
    manage = next(call[0][1] for call in executor.register.call_args_list if call[0][0] == "manage_github_pr")

    async def github(client, method, url, provider, action, **kwargs):
        return httpx.Response(422, json={"message": "expected head sha didn't match"}), "Error: GitHub API error (422)"

    with patch(_SAFE_REQUEST, new=github):
        result = await manage(repo="acme/app", number=7, action="update_branch")

    assert result == "Error: GitHub API error (422)"


@pytest.mark.asyncio
async def test_update_branch_on_an_up_to_date_branch_is_a_no_op_naming_the_head():
    executor = MagicMock()
    executor.register = MagicMock()
    build_tools("fake-token", executor)
    manage = next(call[0][1] for call in executor.register.call_args_list if call[0][0] == "manage_github_pr")
    head_sha = "c" * 40

    async def github(client, method, url, provider, action, **kwargs):
        if method == "put" and url == f"{API}/pulls/7/update-branch":
            body = {"message": "There are no new commits on the base branch."}
            return httpx.Response(422, json=body), "Error: GitHub API error (422)"
        if method == "get" and url == f"{API}/pulls/7":
            payload = {"head": {"ref": "feat/x", "sha": head_sha}, "base": {"ref": "main", "sha": BASE}}
            return httpx.Response(200, json=payload), None
        raise AssertionError(f"unexpected {method.upper()} {url}")

    with patch(_SAFE_REQUEST, new=github):
        result = await manage(repo="acme/app", number=7, action="update_branch")

    assert isinstance(result, ToolResult) and result.no_op and not result.is_error
    assert not result.content.startswith("Error")
    assert f"already up to date with 'main' at {BASE[:8]}; head {head_sha}" in result.content


_PULLS = "app.integrations.providers.github.pulls"


def _manage_tool():
    executor = MagicMock()
    executor.register = MagicMock()
    build_tools("fake-token", executor)
    return next(call[0][1] for call in executor.register.call_args_list if call[0][0] == "manage_github_pr")


async def _conflict_pr(client, method, url, provider, action, **kwargs):
    if method == "put" and url == f"{API}/pulls/7/update-branch":
        return httpx.Response(422, json={"message": "merge conflict between base and head"}), "Error: 422"
    if method == "get" and url == f"{API}/pulls/7":
        head = {"ref": "feat/x", "repo": {"full_name": "acme/app"}}
        return httpx.Response(200, json={"head": head, "base": {"ref": "main"}}), None
    if method == "get" and url.startswith(f"{API}/commits/"):
        # The branch tips: the update first reads the branch for a stopped alignment, then the base.
        tip = "d" * 40 if url.endswith("/main") else BASE
        return httpx.Response(200, json={"sha": tip, "parents": [{"sha": "0" * 40}], "commit": {"message": "x"}}), None
    raise AssertionError(f"unexpected {method.upper()} {url}")


@pytest.mark.asyncio
@pytest.mark.parametrize("holds_base", [True, False])
async def test_update_branch_whose_line_merge_landed_after_an_error_leads_with_merged(holds_base):
    # A line merge answered an error while its commits landed (a lost answer, a replayed call): when the
    # branch now holds the base, the answer leads with "Merged (landed)", never "Error" over written work.
    manage = _manage_tool()
    error = "Error: the merge commit could not be read back."
    landed = f"Outcome: landed true: 'feat/x' head moved from {BASE[:12]} to {MERGE}."
    main_tip = "d" * 40

    with (
        patch(_SAFE_REQUEST, new=_conflict_pr),
        patch(f"{_PULLS}.conflict_answer", return_value=error),
        patch(f"{_PULLS}.ref_moved_and_undone", return_value=False),
        patch(f"{_PULLS}.outcome_line", return_value=landed),
        patch(f"{_PULLS}._already_merged", return_value=MERGE if holds_base else "") as merged,
    ):
        result = await manage(repo="acme/app", number=7, action="update_branch")

    assert merged.await_args.args[-1] == main_tip
    if holds_base:
        assert result.startswith("Merged (landed): the branch of PR #7 in acme/app now holds 'main'"), result
        assert landed in result and error in result
    else:
        assert result.startswith("Error: the branch of PR #7 in acme/app cannot take 'main'"), result
        assert result.endswith(landed)


@pytest.mark.asyncio
async def test_a_missing_ref_is_named():
    merge, _ = _merge_tool()
    fake = FakeGitHub(merge_status=404)
    with patch(_SAFE_REQUEST, new=fake):
        result = await merge(repo="acme/app", branch="feat/x", from_ref="nope")

    assert result.startswith("Error: branch 'feat/x' or ref 'nope' does not exist in acme/app")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"branch": "main"}, "is the default branch"),
        ({"branch": "feat/x", "from_ref": "feat/x"}, "name the same branch"),
        ({"branch": ""}, "'branch' is required"),
    ],
)
async def test_refusals_write_nothing(kwargs, message):
    merge, _ = _merge_tool()
    fake = FakeGitHub()
    with patch(_SAFE_REQUEST, new=fake):
        result = await merge(repo="acme/app", **kwargs)

    assert result.startswith("Error: ") and message in result
    assert fake.urls("post") == []


def test_the_schema_requires_repo_and_branch():
    _, definition = _merge_tool()
    assert definition.input_schema["required"] == ["repo", "branch"]
    assert set(definition.input_schema["properties"]) == {"repo", "branch", "from_ref", "message", "resolutions"}
