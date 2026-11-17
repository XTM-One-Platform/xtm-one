"""``rebase_github_branch``: commits replayed onto a ref one by one, the branch moved with a lease (#11479).

A fake GitHub holds commits as ``{sha: (parents, message, files)}`` and answers the reads, the compare,
``createCommitOnBranch`` on any branch, the ref create/delete and ``updateRefs`` with its ``beforeOid``.
"""

from __future__ import annotations

import base64
import hashlib
from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest

from app.integrations.providers.github import build_tools
from app.integrations.providers.github.rebase import Commit, parse_resolutions, replay_message, replayed_from

_SAFE_REQUEST = "app.integrations.providers.github._common.safe_request"
API = "https://api.github.com/repos/acme/app"
GRAPHQL = "https://api.github.com/graphql"


def _blob(text: str) -> str:
    data = text.encode()
    return hashlib.sha1(b"blob %d\0" % len(data) + data, usedforsecurity=False).hexdigest()


class FakeGitHub:
    def __init__(self) -> None:
        self.commits: dict[str, tuple[tuple[str, ...], str, dict[str, str]]] = {}
        self.refs: dict[str, str] = {}
        self.blobs: dict[str, str] = {}
        self.counter = 0
        self.signed: list[dict[str, Any]] = []
        self.ref_updates: list[dict[str, Any]] = []
        self.deleted: list[str] = []

    def commit(self, parent: str | None, message: str, files: dict[str, str]) -> str:
        self.counter += 1
        sha = format(self.counter, "040x")
        self.commits[sha] = ((parent,) if parent else (), message, dict(files))
        for text in files.values():
            self.blobs[_blob(text)] = text
        return sha

    def chain(self, start: str, steps: list[tuple[str, dict[str, str]]]) -> list[str]:
        made, tip = [], start
        for message, changes in steps:
            files = {**self.commits[tip][2], **changes}
            files = {p: t for p, t in files.items() if t is not None}
            tip = self.commit(tip, message, files)
            made.append(tip)
        return made

    def _resolve(self, ref: str) -> str | None:
        return ref if ref in self.commits else self.refs.get(ref)

    def _ancestors(self, sha: str) -> list[str]:
        out, cur = [], sha
        while cur:
            out.append(cur)
            parents = self.commits[cur][0]
            cur = parents[0] if parents else ""
        return out

    def _payload(self, sha: str) -> dict:
        parents, message, _ = self.commits[sha]
        return {
            "sha": sha,
            "parents": [{"sha": p} for p in parents],
            "commit": {
                "message": message,
                "tree": {"sha": "e" + sha[1:]},
                "author": {"name": "Ada", "email": "ada@example.com", "date": "2026-11-01T10:00:00Z"},
            },
        }

    async def __call__(self, client, method, url, provider, action, **kwargs):
        def ok(payload, status=200):
            return httpx.Response(status, json=payload), None

        if method == "get" and url == API:
            return ok({"default_branch": "main"})
        if method == "get" and url.startswith(f"{API}/commits/"):
            sha = self._resolve(url.removeprefix(f"{API}/commits/"))
            if sha is None:
                return httpx.Response(404, json={}), "Error: GitHub API error (404)"
            return ok(self._payload(sha))
        if method == "get" and url.startswith(f"{API}/git/trees/e"):
            sha = "0" + url.rsplit("/", 1)[1][1:]
            files = self.commits[sha][2]
            tree = [
                {"path": p, "type": "blob", "mode": "100644", "sha": _blob(t), "size": len(t)} for p, t in files.items()
            ]
            return ok({"tree": tree, "truncated": False})
        if method == "get" and url.startswith(f"{API}/git/blobs/"):
            text = self.blobs[url.rsplit("/", 1)[1]]
            return ok({"encoding": "base64", "content": base64.b64encode(text.encode()).decode()})
        if method == "get" and url.startswith(f"{API}/compare/"):
            base, head = url.removeprefix(f"{API}/compare/").split("...")
            base_sha, head_sha = self._resolve(base), self._resolve(head)
            base_line = set(self._ancestors(base_sha))
            own = [s for s in self._ancestors(head_sha) if s not in base_line]
            return ok({"total_commits": len(own), "commits": [self._payload(s) for s in reversed(own)]})
        if method == "post" and url == f"{API}/git/refs":
            body = kwargs["json"]
            self.refs[body["ref"].removeprefix("refs/heads/")] = body["sha"]
            return ok({}, 201)
        if method == "delete" and url.startswith(f"{API}/git/refs/heads/"):
            name = url.removeprefix(f"{API}/git/refs/heads/")
            self.refs.pop(name, None)
            self.deleted.append(name)
            return httpx.Response(204), None
        if method == "post" and url == GRAPHQL and "repository(owner" in kwargs["json"]["query"]:
            return ok({"data": {"repository": {"id": "R_1"}}})
        if method == "post" and url == GRAPHQL and "updateRefs" in kwargs["json"]["query"]:
            update = kwargs["json"]["variables"]["input"]["refUpdates"][0]
            self.ref_updates.append(update)
            name = update["name"].removeprefix("refs/heads/")
            if self.refs.get(name) != update["beforeOid"]:
                return ok({"errors": [{"message": "Ref moved"}]})
            self.refs[name] = update["afterOid"]
            return ok({"data": {"updateRefs": {"clientMutationId": None}}})
        if method == "post" and url == GRAPHQL:
            data = kwargs["json"]["variables"]["input"]
            name = data["branch"]["branchName"]
            if self.refs.get(name) != data["expectedHeadOid"]:
                return ok({"errors": [{"message": "Expected branch to point to another commit"}]})
            files = dict(self.commits[data["expectedHeadOid"]][2])
            for item in data["fileChanges"].get("additions", []):
                files[item["path"]] = base64.b64decode(item["contents"]).decode()
            for item in data["fileChanges"].get("deletions", []):
                files.pop(item["path"], None)
            message = data["message"]["headline"] + (
                "\n\n" + data["message"]["body"] if data["message"].get("body") else ""
            )
            sha = self.commit(data["expectedHeadOid"], message, files)
            self.refs[name] = sha
            self.signed.append(data)
            return ok(
                {
                    "data": {
                        "createCommitOnBranch": {
                            "commit": {"oid": sha, "signature": {"isValid": True, "state": "VALID"}}
                        }
                    }
                }
            )
        raise AssertionError(f"unexpected {method.upper()} {url}")


def _tool():
    executor = MagicMock()
    executor.register = MagicMock()
    definitions = build_tools("fake-token", executor)
    assert any(d.name == "rebase_github_branch" for d in definitions)
    return next(c[0][1] for c in executor.register.call_args_list if c[0][0] == "rebase_github_branch")


def _scene() -> tuple[FakeGitHub, list[str]]:
    gh = FakeGitHub()
    root = gh.commit(None, "init", {"a.txt": "a1\na2\na3\n", "b.txt": "b\n"})
    main = gh.chain(root, [("main 1", {"m1.txt": "m1\n"}), ("main 2", {"m2.txt": "m2\n"})])
    feat = gh.chain(
        root, [("feat 1", {"f1.txt": "f1\n"}), ("feat 2", {"a.txt": "a1\nA2\na3\n"}), ("feat 3", {"f3.txt": "f3\n"})]
    )
    gh.refs.update({"main": main[-1], "feat/x": feat[-1]})
    return gh, feat


def _own_messages(gh: FakeGitHub, tip: str, stop: str) -> list[str]:
    out = []
    for sha in gh._ancestors(tip):
        if sha == stop:
            break
        out.append(gh.commits[sha][1].partition("\n")[0])
    return list(reversed(out))


@pytest.mark.asyncio
async def test_three_commits_replayed_onto_the_moved_ref_and_moved_with_a_lease():
    gh, feat = _scene()
    with patch(_SAFE_REQUEST, new=gh):
        result = await _tool()(repo="acme/app", branch="feat/x", expected_head=feat[-1])
    assert result.startswith("Rebased 'feat/x'"), result
    tip = gh.refs["feat/x"]
    assert _own_messages(gh, tip, gh.refs["main"]) == ["feat 1", "feat 2", "feat 3"]
    assert gh.commits[tip][2]["a.txt"] == "a1\nA2\na3\n" and "m2.txt" in gh.commits[tip][2]
    assert replayed_from(gh.commits[tip][1]) == feat[-1]
    assert "Co-authored-by: Ada <ada@example.com>" in gh.commits[tip][1]
    assert gh.ref_updates[-1]["beforeOid"] == feat[-1]
    assert not any(name.startswith("rebase/") for name in gh.refs)


@pytest.mark.asyncio
async def test_a_stale_expected_head_is_refused_before_any_write():
    gh, feat = _scene()
    with patch(_SAFE_REQUEST, new=gh):
        result = await _tool()(repo="acme/app", branch="feat/x", expected_head=feat[0])
    assert result.startswith("Error:") and "lease" in result
    assert gh.signed == [] and gh.ref_updates == []


@pytest.mark.asyncio
async def test_a_call_replayed_after_the_move_answers_no_change():
    gh, feat = _scene()
    tool = _tool()
    with patch(_SAFE_REQUEST, new=gh):
        await tool(repo="acme/app", branch="feat/x", expected_head=feat[-1])
        written = len(gh.signed)
        again = await tool(repo="acme/app", branch="feat/x", expected_head=feat[-1])
    assert again.startswith("No change") and len(gh.signed) == written


@pytest.mark.asyncio
async def test_a_conflicting_middle_commit_stops_then_resumes_with_its_resolution():
    gh = FakeGitHub()
    root = gh.commit(None, "init", {"a.txt": "a1\na2\na3\n"})
    main = gh.chain(root, [("main edits a2", {"a.txt": "a1\nMAIN\na3\n"})])
    feat = gh.chain(
        root, [("feat 1", {"f1.txt": "f1\n"}), ("feat 2", {"a.txt": "a1\nFEAT\na3\n"}), ("feat 3", {"f3.txt": "f3\n"})]
    )
    gh.refs.update({"main": main[-1], "feat/x": feat[-1]})
    tool = _tool()
    with patch(_SAFE_REQUEST, new=gh):
        stopped = await tool(repo="acme/app", branch="feat/x", expected_head=feat[-1])
        assert stopped.startswith("Conflict: commit " + feat[1][:12]), stopped
        assert "a.txt" in stopped and gh.refs["feat/x"] == feat[-1]
        assert len(gh.signed) == 1  # feat 1 only
        done = await tool(
            repo="acme/app",
            branch="feat/x",
            expected_head=feat[-1],
            resolutions=[{"commit": feat[1][:12], "path": "a.txt", "keep": "both"}],
        )
    assert done.startswith("Rebased"), done
    tip = gh.refs["feat/x"]
    assert _own_messages(gh, tip, main[-1]) == ["feat 1", "feat 2", "feat 3"]  # feat 1 not replayed twice
    text = gh.commits[tip][2]["a.txt"]
    assert "<<<<<<<" not in text and "MAIN" in text and "FEAT" in text


@pytest.mark.asyncio
async def test_a_stacked_branch_rebased_onto_its_rebased_parent_keeps_its_own_diff():
    gh, feat = _scene()
    stacked = gh.chain(feat[-1], [("stack 1", {"s.txt": "s\n"})])
    gh.refs["stack"] = stacked[-1]
    old_parent = feat[-1]
    tool = _tool()
    with patch(_SAFE_REQUEST, new=gh):
        await tool(repo="acme/app", branch="feat/x", expected_head=old_parent)
        new_parent = gh.refs["feat/x"]
        result = await tool(
            repo="acme/app", branch="stack", expected_head=stacked[-1], onto="feat/x", upstream=old_parent
        )
    assert result.startswith("Rebased"), result
    tip = gh.refs["stack"]
    assert gh.commits[tip][0] == (new_parent,)
    parent_files, own_files = gh.commits[new_parent][2], gh.commits[tip][2]
    assert {p: t for p, t in own_files.items() if parent_files.get(p) != t} == {"s.txt": "s\n"}


@pytest.mark.asyncio
async def test_max_commits_stops_without_moving_the_branch_and_the_next_call_goes_on():
    gh, feat = _scene()
    tool = _tool()
    with patch(_SAFE_REQUEST, new=gh):
        first = await tool(repo="acme/app", branch="feat/x", expected_head=feat[-1], max_commits=2)
        assert "1 remain(s)" in first and gh.refs["feat/x"] == feat[-1]
        second = await tool(repo="acme/app", branch="feat/x", expected_head=feat[-1], max_commits=2)
    assert second.startswith("Rebased"), second
    assert _own_messages(gh, gh.refs["feat/x"], gh.refs["main"]) == ["feat 1", "feat 2", "feat 3"]


def test_parse_resolutions_and_message():
    parsed, err = parse_resolutions([{"commit": "ABCDEF1", "path": "a", "keep": "onto"}])
    assert err is None and ("abcdef1", "a") in parsed
    assert parse_resolutions([{"commit": "abcdef1", "path": "a"}])[1]
    assert parse_resolutions([{"commit": "zz", "path": "a", "keep": "onto"}])[1]
    headline, body = replay_message(Commit("f" * 40, ("e" * 40,), "fix: x\n\nwhy", "Ada <a@x>", "2026-11-01"))
    assert headline == "fix: x" and body.startswith("why\n\nRebased-from: " + "f" * 40)


def test_the_tool_is_a_write():
    from app.services.tool_classification import infer_tool_type

    assert infer_tool_type("rebase_github_branch") == "write"
