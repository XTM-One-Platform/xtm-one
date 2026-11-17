"""``manage_github_ref action=update_branch``: a guarded branch move.

A fake GitHub answers the reads of one move and records every request: the
tests check that a fast-forward is one PATCH without force, that a move which
drops commits needs ``force``, and that a move leaving an open pull request
with nothing ahead of its base (GitHub then closes it) is refused even forced.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import pytest

from app.integrations.providers.github import build_tools

_SAFE_REQUEST = "app.integrations.providers.github._common.safe_request"

API = "https://api.github.com/repos/acme/app"
TIP = "a" * 40
TARGET = "b" * 40
MAIN_TIP = "c" * 40


class FakeGitHub:
    def __init__(
        self,
        *,
        pulls: list[dict] | None = None,
        tip_to_target: str = "ahead",
        behind_by: int = 0,
        base_to_target_ahead: int = 2,
    ) -> None:
        self.pulls = pulls or []
        self.tip_to_target = tip_to_target
        self.behind_by = behind_by
        self.base_to_target_ahead = base_to_target_ahead
        self.calls: list[tuple[str, str, dict]] = []

    def urls(self, method: str | None = None) -> list[str]:
        return [url for verb, url, _ in self.calls if method is None or verb == method]

    async def __call__(self, client, method, url, provider, action, **kwargs):
        self.calls.append((method, url, kwargs))

        def ok(payload):
            return httpx.Response(200, json=payload), None

        if method == "get" and url == f"{API}/git/ref/heads/feat/x":
            return ok({"ref": "refs/heads/feat/x", "object": {"sha": TIP, "type": "commit"}})
        if method == "get" and url == f"{API}/pulls":
            assert kwargs["params"]["head"] == "acme:feat/x"
            assert kwargs["params"]["state"] == "open"
            return ok(self.pulls)
        if method == "get" and url == f"{API}/compare/{TIP}...{TARGET}":
            return ok({"status": self.tip_to_target, "ahead_by": 1, "behind_by": self.behind_by})
        if method == "get" and url == f"{API}/compare/main...{TARGET}":
            status = "ahead" if self.base_to_target_ahead else "behind"
            return ok({"status": status, "ahead_by": self.base_to_target_ahead, "behind_by": 0})
        if method == "patch" and url == f"{API}/git/refs/heads/feat/x":
            return ok({"ref": "refs/heads/feat/x", "object": {"sha": kwargs["json"]["sha"]}})
        raise AssertionError(f"unexpected {method.upper()} {url}")


def _ref_tool():
    executor = MagicMock()
    executor.register = MagicMock()
    definitions = build_tools("fake-token", executor)
    handler = next(call[0][1] for call in executor.register.call_args_list if call[0][0] == "manage_github_ref")
    definition = next(d for d in definitions if d.name == "manage_github_ref")
    return handler, definition


_OPEN_PR = {"number": 42, "title": "feat(chat): websockets", "base": {"ref": "main"}, "head": {"ref": "feat/x"}}


@pytest.mark.asyncio
async def test_a_fast_forward_is_one_patch_without_force():
    move, _ = _ref_tool()
    fake = FakeGitHub(pulls=[_OPEN_PR])
    with patch(_SAFE_REQUEST, new=fake):
        result = await move(repo="acme/app", action="update_branch", branch="feat/x", sha=TARGET)

    assert result == f"Branch 'feat/x' of acme/app moved from {TIP[:8]} to {TARGET[:8]} (fast-forward)."
    assert fake.urls("patch") == [f"{API}/git/refs/heads/feat/x"]
    assert fake.calls[-1][2]["json"] == {"sha": TARGET, "force": False}


@pytest.mark.asyncio
async def test_a_move_that_drops_commits_is_refused_without_force():
    move, _ = _ref_tool()
    fake = FakeGitHub(tip_to_target="diverged", behind_by=3)
    with patch(_SAFE_REQUEST, new=fake):
        result = await move(repo="acme/app", action="update_branch", branch="feat/x", sha=TARGET)

    assert result.startswith("Error: ")
    assert "not a fast-forward" in result and "3 commit(s)" in result and "force=true" in result
    assert fake.urls("patch") == []


@pytest.mark.asyncio
async def test_a_forced_move_patches_with_force_and_skips_the_fast_forward_check():
    move, _ = _ref_tool()
    fake = FakeGitHub(tip_to_target="diverged", behind_by=3)
    with patch(_SAFE_REQUEST, new=fake):
        result = await move(repo="acme/app", action="update_branch", branch="feat/x", sha=TARGET, force=True)

    assert result.endswith("(forced).")
    assert f"{API}/compare/{TIP}...{TARGET}" not in fake.urls("get")
    assert fake.calls[-1][2]["json"] == {"sha": TARGET, "force": True}


@pytest.mark.asyncio
@pytest.mark.parametrize("force", [False, True])
async def test_a_move_onto_the_base_of_an_open_pull_request_is_refused_even_forced(force):
    move, _ = _ref_tool()
    fake = FakeGitHub(pulls=[_OPEN_PR], base_to_target_ahead=0)
    with patch(_SAFE_REQUEST, new=fake):
        result = await move(repo="acme/app", action="update_branch", branch="feat/x", sha=TARGET, force=force)

    assert result.startswith("Error: ")
    assert "pull request #42" in result and "nothing ahead of its base 'main'" in result
    assert "manage_github_pr action=update_branch" in result
    assert fake.urls("patch") == []


@pytest.mark.asyncio
async def test_a_branch_already_at_the_target_moves_nothing():
    move, _ = _ref_tool()
    fake = FakeGitHub()
    with patch(_SAFE_REQUEST, new=fake):
        result = await move(repo="acme/app", action="update_branch", branch="feat/x", sha=TIP)

    assert "already at" in result and "nothing moved" in result
    assert fake.urls() == [f"{API}/git/ref/heads/feat/x"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"branch": "", "sha": TARGET}, "'branch' is required for update_branch"),
        ({"branch": "feat/x", "sha": ""}, "'sha' is required for update_branch"),
    ],
)
async def test_update_branch_needs_a_branch_and_a_sha(kwargs, message):
    move, _ = _ref_tool()
    fake = FakeGitHub()
    with patch(_SAFE_REQUEST, new=fake):
        result = await move(repo="acme/app", action="update_branch", **kwargs)

    assert result.startswith("Error: ") and message in result
    assert fake.urls() == []


def test_the_schema_and_the_graphql_description_name_the_guarded_move():
    _, definition = _ref_tool()
    props = definition.input_schema["properties"]
    assert "update_branch" in props["action"]["enum"]
    assert props["force"] == {
        "type": "boolean",
        "description": "update_branch only: allow a move that is not a fast-forward (drops commits)",
        "default": False,
    }
    executor = MagicMock()
    graphql = next(d for d in build_tools("fake-token", executor) if d.name == "github_graphql_query")
    assert "manage_github_ref action=update_branch" in graphql.description
