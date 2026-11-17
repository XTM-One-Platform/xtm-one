"""``rebase_github_branch`` remembers its calls (#11802).

Each call passes only the resolution of the commit it stopped on; ``onto`` moving between calls does not
restart the replay; a commit whose resolution writes nothing is never replayed as an empty commit; a
``keep: onto`` that drops a commit's change is named.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from app.integrations.providers.github.rebase import Resolution
from app.integrations.providers.github.rebase_state import RebaseState, merge_resolutions, state_key
from tests.test_github_rebase_branch import _SAFE_REQUEST, FakeGitHub, _own_messages, _tool


class _Redis:
    def __init__(self) -> None:
        self.data: dict[str, Any] = {}

    async def get(self, key: str) -> Any:
        return self.data.get(key)

    async def set(self, key: str, value: Any, ex: int | None = None) -> None:
        self.data[key] = value

    async def delete(self, key: str) -> None:
        self.data.pop(key, None)


@pytest.fixture
def redis():
    store = _Redis()

    async def client():
        return store

    with patch("app.core.redis.redis_client", new=client):
        yield store


def _three_conflicts() -> tuple[FakeGitHub, list[str], str]:
    gh = FakeGitHub()
    root = gh.commit(None, "init", {"a.txt": "a\n", "b.txt": "b\n", "c.txt": "c\n"})
    main = gh.chain(root, [("main", {"a.txt": "A-main\n", "b.txt": "B-main\n", "c.txt": "C-main\n"})])
    feat = gh.chain(
        root,
        [("feat a", {"a.txt": "A-feat\n"}), ("feat b", {"b.txt": "B-feat\n"}), ("feat c", {"c.txt": "C-feat\n"})],
    )
    gh.refs.update({"main": main[-1], "feat/x": feat[-1]})
    return gh, feat, main[-1]


@pytest.mark.asyncio
async def test_three_conflicts_resolved_over_three_calls_with_only_the_new_resolution(redis):
    gh, feat, main = _three_conflicts()
    tool = _tool()
    with patch(_SAFE_REQUEST, new=gh):
        first = await tool(repo="acme/app", branch="feat/x", expected_head=feat[-1])
        assert first.startswith("Conflict: commit " + feat[0][:12]), first
        second = await tool(
            repo="acme/app",
            branch="feat/x",
            expected_head=feat[-1],
            resolutions=[{"commit": feat[0][:12], "path": "a.txt", "keep": "commit"}],
        )
        assert second.startswith("Conflict: commit " + feat[1][:12]), second
        third = await tool(
            repo="acme/app",
            branch="feat/x",
            expected_head=feat[-1],
            resolutions=[{"commit": feat[1][:12], "path": "b.txt", "keep": "onto"}],
        )
        assert third.startswith("Conflict: commit " + feat[2][:12]), third
        done = await tool(
            repo="acme/app",
            branch="feat/x",
            expected_head=feat[-1],
            resolutions=[{"commit": feat[2][:12], "path": "c.txt", "content": "C-merged\n"}],
        )
    assert done.startswith("Rebased"), done
    tip = gh.refs["feat/x"]
    # feat b kept onto's side: nothing to write, so no empty commit; the others once each.
    assert _own_messages(gh, tip, main) == ["feat a", "feat c"]
    files = gh.commits[tip][2]
    assert files == {"a.txt": "A-feat\n", "b.txt": "B-main\n", "c.txt": "C-merged\n"}
    for sha in gh._ancestors(tip):
        if sha == main:
            break
        parent = gh.commits[sha][0][0]
        assert gh.commits[sha][2] != gh.commits[parent][2], "an empty commit was replayed"
    assert f"- {feat[1][:12]} b.txt: 'keep: onto'" in third, third
    assert redis.data == {}  # dropped once the branch moved


@pytest.mark.asyncio
async def test_onto_moving_between_calls_goes_on_onto_the_first_commit(redis):
    gh, feat, main = _three_conflicts()
    tool = _tool()
    with patch(_SAFE_REQUEST, new=gh):
        await tool(repo="acme/app", branch="feat/x", expected_head=feat[-1])
        moved = gh.chain(main, [("main later", {"z.txt": "z\n"})])
        gh.refs["main"] = moved[-1]
        again = await tool(
            repo="acme/app",
            branch="feat/x",
            expected_head=feat[-1],
            resolutions=[{"commit": feat[0][:12], "path": "a.txt", "keep": "commit"}],
        )
    assert "moved to " + moved[-1][:12] in again and "onto " + main[:12] in again, again
    assert again.startswith("Conflict: commit " + feat[1][:12]), again


@pytest.mark.asyncio
async def test_without_redis_a_call_uses_its_own_arguments():
    gh, feat, _main = _three_conflicts()
    with patch(_SAFE_REQUEST, new=gh):
        first = await _tool()(repo="acme/app", branch="feat/x", expected_head=feat[-1])
    assert first.startswith("Conflict: commit " + feat[0][:12])


def test_merge_resolutions_replaces_the_same_commit_and_path():
    stored = [["abcdef1", "a.txt", "onto", None], ["1234567", "b.txt", "both", None]]
    merged = merge_resolutions(stored, {("abcdef1234", "a.txt"): Resolution("commit", None)})
    assert ["1234567", "b.txt", "both", None] in merged
    assert ["abcdef1234", "a.txt", "commit", None] in merged and len(merged) == 2


def test_state_key_is_per_repository_branch_and_head():
    assert state_key("Acme/App", "feat/x", "f" * 40) == f"github:rebase:acme/app:feat/x:{'f' * 40}"
    assert RebaseState("main", "a" * 40, [], [])._asdict()["onto_ref"] == "main"
