"""``merge_github_branch`` with ``resolutions``: a conflicting merge landed in signed commits.

A fake GitHub answers the Merges API (409 until the conflicting paths match),
the compare, commit, tree and blob reads, and ``createCommitOnBranch``; it
records every request so the tests check the three commits, their contents,
the refusals that write nothing and the undo of a merge still refused.
"""

from __future__ import annotations

import base64
import hashlib
from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest

from app.integrations.providers.github import build_tools, merge_resolution
from app.integrations.providers.github.merge_resolution import parse_resolutions, split_batches

_SAFE_REQUEST = "app.integrations.providers.github._common.safe_request"
_WORKSPACE = "app.integrations.providers.github.merge_resolution.current_workspace"

API = "https://api.github.com/repos/acme/app"
GRAPHQL = "https://api.github.com/graphql"
BASE = "b" * 40
OURS = "1" * 40
THEIRS = "2" * 40
MERGE = "5" * 40


def _sha(text: str) -> str:
    """The blob id git gives *text*: the tool compares resolved contents with tree entries by it."""
    data = text.encode()
    return hashlib.sha1(b"blob %d\0" % len(data) + data, usedforsecurity=False).hexdigest()


class FakeGitHub:
    """Two sides as ``{path: text}``; the merge conflicts while a path changed on both sides differs."""

    def __init__(
        self,
        ours: dict[str, str],
        theirs: dict[str, str],
        *,
        both: list[str],
        always_conflict: bool = False,
        modes: dict[str, str] | None = None,
        their_modes: dict[str, str] | None = None,
        lose_commit: int | None = None,
        move_branch_on_merge: bool = False,
        omit_sizes: bool = False,
        source_moves: bool = False,
        lose_merge: bool = False,
        foreign_tip_after_loss: bool = False,
        refuse_ref_update: bool = False,
        moved_before_commit: str | None = None,
        compare_status: str = "",
        merged_answer_after_commits: bool = False,
        stale_branch_reads: int = 0,
    ):
        # stale_branch_reads: after the resolution commits, that many reads of the branch by name still answer the
        # merge commit and its tree (GitHub's REST read lagging a GraphQL commit), while the commit id reads fresh.
        self.stale_branch_reads = stale_branch_reads
        self.merge_snapshot: dict[str, str] = {}
        # "behind": the branch already holds the ref. merged_answer_after_commits: the Merges API answers 204
        # (already contains) once a commit landed, the stale-conflict second pass seen on a PR update.
        self.compare_status = compare_status
        self.merged_answer_after_commits = merged_answer_after_commits
        # "merged": the branch moves to a commit holding THEIRS before the first commit; "foreign": to one without it.
        self.moved_before_commit = moved_before_commit
        self.ours = dict(ours)
        self.initial_ours = dict(ours)
        self.refuse_ref_update = refuse_ref_update
        self.ref_updates: list[dict[str, Any]] = []
        self.theirs = dict(theirs)
        self.both = both
        self.always_conflict = always_conflict
        self.modes = modes or {}
        # their_modes: the ref's tree modes where they differ from the branch's (a bit set on one side only).
        self.their_modes = their_modes
        self.lose_commit = lose_commit
        self.move_branch_on_merge = move_branch_on_merge
        self.omit_sizes = omit_sizes
        self.source_moves = source_moves
        self.lose_merge = lose_merge
        self.foreign_tip_after_loss = foreign_tip_after_loss
        self.tip_parent: str | None = None
        self.tip_headline = ""
        self.source_reads = 0
        self.merge_heads: list[str] = []
        self.tip = OURS
        self.commits: list[dict[str, Any]] = []
        self.merged = False
        self.calls: list[tuple[str, str, dict]] = []
        self.blobs = {_sha(text): text for text in [*ours.values(), *theirs.values()]}
        # The scratch branch a resolution is built on shares the branch's state; the target keeps its own tip
        # (target_tip) from the scratch's creation until the one fast-forward moves it.
        self.scratch: str | None = None
        self.target_tip: str | None = None
        self.target_moves: list[str] = []
        self.scratch_deleted = False
        self.refuse_target_move = False
        # Armed by the stop handlers below: the next matching call raises a worker stop.
        self.stop_armed = False

    def posts(self) -> list[str]:
        return [url for verb, url, _ in self.calls if verb == "post"]

    def _tree(self, files: dict[str, str], modes: dict[str, str] | None = None) -> list[dict[str, Any]]:
        modes = self.modes if modes is None else modes
        return [
            {
                "path": path,
                "type": "blob",
                "mode": modes.get(path, "100644"),
                "sha": _sha(text),
                **({} if self.omit_sizes else {"size": len(text.encode())}),
            }
            for path, text in files.items()
        ]

    def blob_reads(self) -> int:
        return sum(1 for verb, url, _ in self.calls if verb == "get" and "/git/blobs/" in url)

    async def __call__(self, client, method, url, provider, action, **kwargs):
        self.calls.append((method, url, kwargs))

        def ok(payload, status: int = 200):
            return httpx.Response(status, json=payload), None

        if method == "get" and url == f"{API}/git/matching-refs/heads/tmp/commit-batch":
            return ok([])
        if method == "post" and url == f"{API}/git/refs":
            self.scratch = kwargs["json"]["ref"].removeprefix("refs/heads/")
            assert self.scratch.startswith("tmp/commit-batch/") and kwargs["json"]["sha"] == self.tip
            self.target_tip = self.tip
            return ok({"ref": kwargs["json"]["ref"]}, 201)
        if method == "delete" and self.scratch and url == f"{API}/git/refs/heads/{self.scratch}":
            self.scratch_deleted = True
            return httpx.Response(204), None
        if method == "patch" and url == f"{API}/git/refs/heads/feat/x":
            assert kwargs["json"]["force"] is False
            if self.refuse_target_move:
                return httpx.Response(422, json={"message": "Update is not a fast forward"}), "Error: 422"
            self.target_moves.append(kwargs["json"]["sha"])
            self.target_tip = None
            return ok({"object": {"sha": kwargs["json"]["sha"]}})
        if self.scratch and self.scratch in url:
            url = url.replace(self.scratch, "feat/x")
        elif self.target_tip is not None and method == "get" and url == f"{API}/commits/feat/x":
            return ok({"sha": self.target_tip, "commit": {"tree": {"sha": "a" * 40}}})

        if method == "get" and url == API:
            return ok({"default_branch": "main"})
        if method == "post" and url == f"{API}/merges":
            self.merge_heads.append(kwargs["json"]["head"])
            if self.merged_answer_after_commits and self.commits:
                return httpx.Response(204), None
            conflict = self.always_conflict or any(self.ours.get(p) != self.theirs.get(p) for p in self.both)
            if conflict:
                return httpx.Response(409, json={"message": "Merge conflict"}), "Error: GitHub API error (409)"
            if self.move_branch_on_merge:
                self.tip = "9" * 40
            parent = self.tip
            self.merged = True
            self.tip = MERGE
            self.ours = {**self.ours, **{p: t for p, t in self.theirs.items() if p not in self.ours}}
            self.merge_snapshot = dict(self.ours)
            if self.lose_merge:
                return httpx.Response(502, text="bad gateway"), "Error: GitHub API error (502)"
            payload = {
                "sha": MERGE,
                "parents": [{"sha": parent}, {"sha": THEIRS}],
                "commit": {"verification": {"verified": True, "reason": "valid"}},
            }
            return ok(payload, 201)
        if method == "get" and url == f"{API}/compare/feat/x...main":
            payload = {"merge_base_commit": {"sha": BASE}, "files": [{"filename": p} for p in self.both]}
            if self.compare_status:
                payload |= {"status": self.compare_status, "base_commit": {"sha": self.tip}}
            return ok(payload)
        if method == "get" and url == f"{API}/compare/{BASE}...feat/x":
            return ok({"files": [{"filename": p} for p in self.both]})
        stale = self.stale_branch_reads and self.tip != MERGE and self.merged and self.commits
        if method == "get" and url == f"{API}/commits/feat/x" and stale:
            self.stale_branch_reads -= 1
            return ok({"sha": MERGE, "commit": {"tree": {"sha": "d" * 40}}})
        if method == "get" and url in (f"{API}/commits/feat/x", f"{API}/commits/{self.tip}"):
            payload = {"sha": self.tip, "commit": {"tree": {"sha": "a" * 40}}}
            if self.tip_parent and not self.foreign_tip_after_loss:
                payload["parents"] = [{"sha": self.tip_parent}]
                payload["commit"] = {
                    **payload["commit"],
                    "message": self.tip_headline,
                    "verification": {"verified": True, "reason": "valid"},
                }
            return ok(payload)
        if method == "get" and url == f"{API}/commits/{BASE}":
            # No merge base tree: the line merge does not run (covered by test_github_merge_hunks).
            return httpx.Response(404, json={"message": "Not Found"}), "Error: GitHub API error (404): Not Found"
        if method == "get" and url == f"{API}/commits/main":
            self.source_reads += 1
            moved = self.source_moves and self.source_reads > 1
            return ok({"sha": "8" * 40 if moved else THEIRS, "commit": {"tree": {"sha": "c" * 40}}})
        if method == "get" and url == f"{API}/git/trees/{'a' * 40}":
            return ok({"tree": self._tree(self.ours), "truncated": False})
        if method == "get" and url == f"{API}/git/trees/{'d' * 40}":
            return ok({"tree": self._tree(self.merge_snapshot), "truncated": False})
        if method == "get" and url == f"{API}/git/trees/{'c' * 40}":
            return ok({"tree": self._tree(self.theirs, self.their_modes), "truncated": False})
        if method == "get" and url.startswith(f"{API}/git/blobs/"):
            text = self.blobs[url.rsplit("/", 1)[1]]
            return ok({"encoding": "base64", "content": base64.b64encode(text.encode()).decode()})
        if method == "post" and url == GRAPHQL and "repository(owner" in kwargs["json"]["query"]:
            return ok({"data": {"repository": {"id": "R_kgDOapp"}}})
        if method == "post" and url == GRAPHQL and "updateRefs" in kwargs["json"]["query"]:
            update = kwargs["json"]["variables"]["input"]["refUpdates"][0]
            self.ref_updates.append(update)
            if self.refuse_ref_update or update["beforeOid"] != self.tip:
                return ok({"errors": [{"message": "Ref update refused"}]})
            assert update["afterOid"] == OURS and update["name"] in ("refs/heads/feat/x", f"refs/heads/{self.scratch}")
            self.tip, self.tip_parent, self.ours = OURS, None, dict(self.initial_ours)
            return ok({"data": {"updateRefs": {"clientMutationId": None}}})
        if method == "get" and url.startswith(f"{API}/compare/{THEIRS}..."):
            contains = self.moved_before_commit == "merged"
            return ok({"status": "ahead" if contains else "diverged", "behind_by": 0 if contains else 1})
        if method == "post" and url == GRAPHQL:
            commit_input = kwargs["json"]["variables"]["input"]
            if self.moved_before_commit and not self.commits and self.tip == OURS:
                self.tip = "7" * 40
            if commit_input["expectedHeadOid"] != self.tip:
                return ok({"errors": [{"message": "Expected branch to point to another commit"}]})
            changes = commit_input["fileChanges"]
            for item in changes.get("additions", []):
                self.ours[item["path"]] = base64.b64decode(item["contents"]).decode()
            for item in changes.get("deletions", []):
                self.ours.pop(item["path"], None)
            self.tip_parent, self.tip_headline = self.tip, commit_input["message"]["headline"]
            self.tip = format(len(self.commits) + 0xC0FFEE, "040x")
            self.commits.append({"headline": commit_input["message"]["headline"], **changes})
            if self.lose_commit == len(self.commits):
                return httpx.Response(502, text="bad gateway"), "Error: GitHub API error (502)"
            commit = {"oid": self.tip, "signature": {"isValid": True, "state": "VALID"}}
            return ok({"data": {"createCommitOnBranch": {"commit": commit}}})
        raise AssertionError(f"unexpected {method.upper()} {url}")


def _merge() -> Any:
    executor = MagicMock()
    executor.register = MagicMock()
    build_tools("fake-token", executor)
    return next(call[0][1] for call in executor.register.call_args_list if call[0][0] == "merge_github_branch")


def _manage() -> Any:
    executor = MagicMock()
    executor.register = MagicMock()
    build_tools("fake-token", executor)
    return next(call[0][1] for call in executor.register.call_args_list if call[0][0] == "manage_github_pr")


def _sides() -> tuple[dict[str, str], dict[str, str]]:
    ours = {"src/a.py": "branch a\n", "docs/b.md": "branch b\n", "same.txt": "x\n"}
    theirs = {"src/a.py": "main a\n", "docs/b.md": "main b\n", "same.txt": "x\n"}
    return ours, theirs


@pytest.mark.asyncio
async def test_resolutions_land_the_merge_in_three_signed_commits():
    ours, theirs = _sides()
    fake = FakeGitHub(ours, theirs, both=["docs/b.md", "src/a.py"])
    with patch(_SAFE_REQUEST, new=fake):
        result = await _merge()(
            repo="acme/app",
            branch="feat/x",
            resolutions=[{"path": "src/a.py", "content": "merged a\n"}, {"path": "docs/b.md", "keep": "branch"}],
        )

    assert result.startswith("Merged 'main' into 'feat/x' of acme/app with its conflicts resolved")
    assert fake.merged
    assert len(fake.commits) == 2 and fake.posts().count(f"{API}/merges") == 2
    first, last = fake.commits
    assert sorted(item["path"] for item in first["additions"]) == ["docs/b.md", "src/a.py"]
    assert {item["path"]: base64.b64decode(item["contents"]).decode() for item in last["additions"]} == {
        "docs/b.md": "branch b\n",
        "src/a.py": "merged a\n",
    }
    assert fake.ours["src/a.py"] == "merged a\n" and fake.ours["docs/b.md"] == "branch b\n"
    assert "verified" in result and MERGE[:12] in result


@pytest.mark.asyncio
async def test_the_branch_moves_once_after_every_commit_landed_on_a_scratch_branch() -> None:
    ours, theirs = _sides()
    fake = FakeGitHub(ours, theirs, both=["docs/b.md", "src/a.py"])
    with patch(_SAFE_REQUEST, new=fake):
        result = await _merge()(
            repo="acme/app",
            branch="feat/x",
            resolutions=[{"path": "src/a.py", "content": "merged a\n"}, {"path": "docs/b.md", "keep": "branch"}],
        )

    commit_branches = [
        kwargs["json"]["variables"]["input"]["branch"]["branchName"]
        for verb, url, kwargs in fake.calls
        if verb == "post" and url == GRAPHQL and "createCommitOnBranch" in kwargs["json"]["query"]
    ]
    merges = [kwargs["json"]["base"] for verb, url, kwargs in fake.calls if verb == "post" and url == f"{API}/merges"]
    assert commit_branches and set(commit_branches) == {fake.scratch}
    # The first merge attempt (refused on the conflict) names the target; the resolved merge, the scratch.
    assert merges == ["feat/x", fake.scratch]
    # One fast-forward of the target, to the last commit, never to an intermediate one.
    assert fake.target_moves == [fake.tip] and fake.scratch_deleted
    assert "moved once from" in result and result.rstrip().splitlines()[-1].startswith("Outcome") is False


@pytest.mark.asyncio
async def test_a_refused_move_leaves_the_branch_unchanged_and_keeps_the_scratch() -> None:
    ours, theirs = _sides()
    fake = FakeGitHub(ours, theirs, both=["src/a.py"])
    fake.refuse_target_move = True
    with patch(_SAFE_REQUEST, new=fake):
        result = await _merge()(
            repo="acme/app", branch="feat/x", resolutions=[{"path": "src/a.py", "content": "merged a\n"}]
        )

    assert result.startswith("Error: the merge was built on scratch branch") and "is unchanged" in result
    assert not fake.target_moves and not fake.scratch_deleted


@pytest.mark.asyncio
async def test_keep_ref_needs_no_third_commit():
    ours, theirs = _sides()
    fake = FakeGitHub(ours, theirs, both=["docs/b.md", "src/a.py"])
    with patch(_SAFE_REQUEST, new=fake):
        result = await _merge()(
            repo="acme/app",
            branch="feat/x",
            resolutions=[{"path": "src/a.py", "keep": "ref"}, {"path": "docs/b.md", "keep": "ref"}],
        )

    assert fake.merged and len(fake.commits) == 1
    assert "Kept as on 'main': docs/b.md, src/a.py." in result


@pytest.mark.asyncio
async def test_the_merge_is_pinned_to_the_commit_read():
    ours, theirs = _sides()
    fake = FakeGitHub(ours, theirs, both=["src/a.py"])
    with patch(_SAFE_REQUEST, new=fake):
        await _merge()(repo="acme/app", branch="feat/x", resolutions=[{"path": "src/a.py", "keep": "ref"}])

    assert fake.merge_heads == ["main", THEIRS]


@pytest.mark.asyncio
async def test_a_branch_moved_under_the_merge_writes_no_resolution():
    ours, theirs = _sides()
    fake = FakeGitHub(ours, theirs, both=["src/a.py"], move_branch_on_merge=True)
    with patch(_SAFE_REQUEST, new=fake):
        result = await _merge()(
            repo="acme/app", branch="feat/x", resolutions=[{"path": "src/a.py", "content": "merged a\n"}]
        )

    assert result.startswith("Error: ") and "moved meanwhile" in result and "not written" in result
    assert len(fake.commits) == 1


@pytest.mark.asyncio
async def test_a_ref_moved_since_the_snapshot_is_not_merged_and_the_alignment_is_undone():
    ours, theirs = _sides()
    fake = FakeGitHub(ours, theirs, both=["src/a.py"], source_moves=True)
    with patch(_SAFE_REQUEST, new=fake):
        result = await _merge()(repo="acme/app", branch="feat/x", resolutions=[{"path": "src/a.py", "keep": "ref"}])

    assert result.startswith("Error: ") and "now points at 888888888888" in result and "was moved back to" in result
    assert fake.merge_heads == ["main"] and not fake.merged
    assert fake.ours["src/a.py"] == "branch a\n" and fake.tip == OURS


@pytest.mark.asyncio
async def test_a_lost_merge_answer_is_not_called_unchanged():
    ours, theirs = _sides()
    fake = FakeGitHub(ours, theirs, both=["src/a.py"], lose_merge=True)
    with patch(_SAFE_REQUEST, new=fake):
        result = await _merge()(
            repo="acme/app", branch="feat/x", resolutions=[{"path": "src/a.py", "content": "merged a\n"}]
        )

    assert result.startswith("Error: ") and "Whether the merge of" in result and "is not known" in result
    assert "Nothing was written" not in result and len(fake.commits) == 1


@pytest.mark.asyncio
async def test_a_lost_alignment_answer_is_not_called_unchanged():
    ours, theirs = _sides()
    fake = FakeGitHub(ours, theirs, both=["src/a.py"], lose_commit=1, foreign_tip_after_loss=True)
    with patch(_SAFE_REQUEST, new=fake):
        result = await _merge()(repo="acme/app", branch="feat/x", resolutions=[{"path": "src/a.py", "keep": "branch"}])

    assert "Whether the commit landed is not known" in result and "Nothing was merged." in result
    assert "Nothing was written" not in result and fake.merge_heads == ["main"]


@pytest.mark.asyncio
async def test_an_alignment_landed_before_a_5xx_is_read_back_and_the_merge_goes_on():
    ours, theirs = _sides()
    fake = FakeGitHub(ours, theirs, both=["src/a.py"], lose_commit=1)
    with patch(_SAFE_REQUEST, new=fake):
        result = await _merge()(repo="acme/app", branch="feat/x", resolutions=[{"path": "src/a.py", "keep": "branch"}])

    assert result.startswith("Merged 'main' into 'feat/x' of acme/app with its conflicts resolved"), result
    assert "read back after GitHub's answer was lost" in result
    assert fake.merged and fake.ours["src/a.py"] == "branch a\n"


@pytest.mark.asyncio
async def test_an_alignment_landed_before_a_5xx_is_undone_when_the_merge_stops():
    ours, theirs = _sides()
    fake = FakeGitHub(ours, theirs, both=["src/a.py"], lose_commit=1, always_conflict=True)
    with patch(_SAFE_REQUEST, new=fake):
        result = await _merge()(repo="acme/app", branch="feat/x", resolutions=[{"path": "src/a.py", "keep": "branch"}])

    assert result.startswith("Error: ") and "was moved back to" in result, result
    assert len(fake.commits) == 1 and not fake.merged
    assert fake.tip == OURS and fake.ours == ours


@pytest.mark.asyncio
async def test_a_branch_moved_by_the_same_merge_answers_success_with_its_head():
    ours, theirs = _sides()
    fake = FakeGitHub(ours, theirs, both=["src/a.py"], moved_before_commit="merged")
    with patch(_SAFE_REQUEST, new=fake):
        result = await _merge()(repo="acme/app", branch="feat/x", resolutions=[{"path": "src/a.py", "keep": "branch"}])

    assert result.startswith("No change: ") and "7" * 40 in result and "already contains 'main'" in result, result
    assert "Nothing was merged" not in result and not fake.commits and fake.merge_heads == ["main"]
    assert not fake.ref_updates


@pytest.mark.asyncio
async def test_a_branch_moved_by_a_third_party_still_refuses():
    ours, theirs = _sides()
    fake = FakeGitHub(ours, theirs, both=["src/a.py"], moved_before_commit="foreign")
    with patch(_SAFE_REQUEST, new=fake):
        result = await _merge()(repo="acme/app", branch="feat/x", resolutions=[{"path": "src/a.py", "keep": "branch"}])

    assert result.startswith("Error: ") and "changed while the commit was being built" in result, result
    assert "Nothing was merged" in result and not fake.commits and fake.merge_heads == ["main"]


@pytest.mark.asyncio
async def test_a_refused_ref_update_falls_back_to_signed_undo_commits():
    ours, theirs = _sides()
    fake = FakeGitHub(ours, theirs, both=["src/a.py"], lose_commit=1, always_conflict=True, refuse_ref_update=True)
    with patch(_SAFE_REQUEST, new=fake):
        result = await _merge()(repo="acme/app", branch="feat/x", resolutions=[{"path": "src/a.py", "keep": "branch"}])

    assert result.startswith("Error: ") and "was undone by" in result and "Ref update refused" in result, result
    restore = format(1 + 0xC0FFEE, "040x")
    assert restore[:12] in result and len(fake.commits) == 2 and not fake.merged
    assert fake.commits[1]["headline"].startswith("Restore feat/x's version")
    assert fake.ours == ours


@pytest.mark.asyncio
async def test_the_ref_update_carries_the_expected_head_and_never_drops_a_foreign_push():
    ours, theirs = _sides()
    fake = FakeGitHub(ours, theirs, both=["src/a.py"], always_conflict=True)
    real = fake.__call__

    async def foreign_push_before_update(client, method, url, provider, action, **kwargs):
        if method == "post" and url == GRAPHQL and "updateRefs" in kwargs["json"]["query"]:
            fake.tip = "7" * 40
        return await real(client, method, url, provider, action, **kwargs)

    with patch(_SAFE_REQUEST, new=foreign_push_before_update):
        result = await _merge()(repo="acme/app", branch="feat/x", resolutions=[{"path": "src/a.py", "keep": "branch"}])

    update = fake.ref_updates[0]
    assert update["beforeOid"] == format(0xC0FFEE, "040x") and update["afterOid"] == OURS and update["force"]
    # The branch moved under the update: it is refused, and the expected-head check keeps the foreign push.
    assert fake.tip == "7" * 40 and "was moved back to" not in result and "refused" in result


@pytest.mark.asyncio
async def test_a_conflict_answer_on_a_branch_already_holding_the_ref_writes_nothing():
    ours, theirs = _sides()
    fake = FakeGitHub(ours, theirs, both=["src/a.py"], compare_status="behind")
    with patch(_SAFE_REQUEST, new=fake):
        result = await _merge()(repo="acme/app", branch="feat/x", resolutions=[{"path": "src/a.py", "keep": "branch"}])

    assert result.startswith("No change: ") and "already contains 'main'" in result and OURS in result, result
    assert not fake.commits and not fake.ref_updates and fake.merge_heads == ["main"] and fake.ours == ours


@pytest.mark.asyncio
async def test_a_merge_answered_already_contained_after_the_alignment_leaves_the_branch_as_before():
    ours, theirs = _sides()
    fake = FakeGitHub(ours, theirs, both=["src/a.py", "docs/b.md"], merged_answer_after_commits=True)
    with patch(_SAFE_REQUEST, new=fake):
        result = await _merge()(
            repo="acme/app",
            branch="feat/x",
            resolutions=[{"path": "src/a.py", "keep": "branch"}, {"path": "docs/b.md", "content": "merged b\n"}],
        )

    assert result.startswith("Error: ") and "already contains" in result and "was moved back to" in result, result
    assert len(fake.commits) == 1 and fake.commits[0]["headline"].startswith("Take main's version")
    assert fake.tip == OURS and fake.ours == ours and fake.ref_updates[0]["afterOid"] == OURS


@pytest.mark.asyncio
async def test_a_refused_ref_update_after_an_already_contained_answer_restores_the_files_by_commit():
    ours, theirs = _sides()
    fake = FakeGitHub(
        ours, theirs, both=["src/a.py"], merged_answer_after_commits=True, refuse_ref_update=True
    )
    with patch(_SAFE_REQUEST, new=fake):
        result = await _merge()(repo="acme/app", branch="feat/x", resolutions=[{"path": "src/a.py", "keep": "branch"}])

    assert result.startswith("Error: ") and "was undone by" in result, result
    assert len(fake.commits) == 2 and fake.commits[1]["headline"].startswith("Restore feat/x's version")
    assert fake.ours == ours


@pytest.mark.asyncio
async def test_the_outcome_line_waits_for_the_final_head_the_answer_names(monkeypatch: pytest.MonkeyPatch) -> None:
    # #11867: the branch read once still showed the merge commit (the ref's side of the conflicting files)
    # while the resolution commit had landed; the outcome now settles on the final head the answer names.
    from app.integrations.providers.github import merge

    final = "f" * 40
    reads = iter([MERGE, MERGE, final])

    async def tip(*_args: object) -> str:
        return next(reads)

    monkeypatch.setattr(merge_resolution, "_branch_tip", tip)
    monkeypatch.setattr(merge_resolution, "_READ_BACK_DELAYS", (0, 0))
    answer = "Error: the resolution stopped.\n" + merge_resolution.final_head_line(final)
    line = await merge.outcome_line(None, {}, "acme/app", API, "feat/x", OURS, answer)

    assert line == f"Outcome: landed true: 'feat/x' head moved from {OURS[:12]} to {final}."
    assert merge_resolution.final_head_of(answer) == final and merge_resolution.final_head_of("Error: x") == ""


class _WorkerStop(BaseException):
    """A worker stop (SIGTERM, deploy) cutting the call: not an error the tool catches."""


def _stop_after_alignment(fake: FakeGitHub):
    """The fake, cut at the merge once an alignment commit landed; the commit before it read as a plain commit."""
    real = fake.__call__

    async def handler(client, method, url, provider, action, **kwargs):
        if method == "post" and url == f"{API}/merges" and fake.commits and not fake.merged and fake.stop_armed:
            raise _WorkerStop
        if method == "get" and url == f"{API}/commits/{OURS}":
            fake.calls.append((method, url, kwargs))
            commit = {"message": "feat: the branch's own work", "tree": {"sha": "a" * 40}}
            return httpx.Response(200, json={"sha": OURS, "parents": [{"sha": BASE}], "commit": commit}), None
        return await real(client, method, url, provider, action, **kwargs)

    return handler


def _as_left_by_an_older_build(fake: FakeGitHub) -> None:
    """The stopped state as a build writing every step on the branch itself left it: the scratch's state is the branch's."""
    fake.target_tip, fake.scratch = None, None


@pytest.mark.asyncio
async def test_a_stop_between_alignment_and_merge_leaves_the_branch_unchanged_and_a_replay_merges() -> None:
    ours, theirs = _sides()
    fake = FakeGitHub(ours, theirs, both=["docs/b.md", "src/a.py"])
    fake.stop_armed = True
    resolutions = [{"path": "src/a.py", "content": "merged a\n"}, {"path": "docs/b.md", "keep": "branch"}]
    with patch(_SAFE_REQUEST, new=_stop_after_alignment(fake)):
        with pytest.raises(_WorkerStop):
            await _merge()(repo="acme/app", branch="feat/x", resolutions=resolutions)
        # The alignment landed on the scratch branch only: 'feat/x' still points at its own head.
        assert fake.target_tip == OURS and not fake.target_moves and fake.scratch_deleted
        # The replay reads the branch as GitHub serves it: its head and its own files.
        fake.tip, fake.tip_parent, fake.ours = OURS, None, dict(ours)
        fake.target_tip, fake.scratch, fake.stop_armed = None, None, False
        result = await _merge()(repo="acme/app", branch="feat/x", resolutions=resolutions)

    assert result.startswith("Merged 'main' into 'feat/x' of acme/app with its conflicts resolved"), result
    assert not fake.ref_updates and len(fake.target_moves) == 1 and "moved back to" not in result
    assert fake.merged and fake.ours["src/a.py"] == "merged a\n" and fake.ours["docs/b.md"] == "branch b\n"


@pytest.mark.asyncio
async def test_a_replay_after_a_stop_between_alignment_and_merge_resolves_every_path_from_both_sides() -> None:
    # A branch an older build left on its alignment commit is still moved off it before the merge.
    ours, theirs = _sides()
    fake = FakeGitHub(ours, theirs, both=["docs/b.md", "src/a.py"])
    fake.stop_armed = True
    resolutions = [{"path": "src/a.py", "content": "merged a\n"}, {"path": "docs/b.md", "keep": "branch"}]
    with patch(_SAFE_REQUEST, new=_stop_after_alignment(fake)):
        with pytest.raises(_WorkerStop):
            await _merge()(repo="acme/app", branch="feat/x", resolutions=resolutions)
        _as_left_by_an_older_build(fake)
        # The stopped call left the branch on its alignment commit: both paths hold main's version.
        assert fake.tip == format(0xC0FFEE, "040x") and fake.tip_parent == OURS
        assert fake.ours["src/a.py"] == "main a\n" and fake.ours["docs/b.md"] == "main b\n"
        fake.stop_armed = False
        result = await _merge()(repo="acme/app", branch="feat/x", resolutions=resolutions)

    assert result.startswith("Merged 'main' into 'feat/x' of acme/app with its conflicts resolved"), result
    assert "moved back to " + OURS[:12] in result and format(0xC0FFEE, "040x")[:12] in result
    # One atomic move off the stopped alignment, expected head = the tip read; no path left at main's version.
    assert fake.ref_updates == [
        {"name": "refs/heads/feat/x", "beforeOid": format(0xC0FFEE, "040x"), "afterOid": OURS, "force": True}
    ]
    assert fake.merged and fake.ours["src/a.py"] == "merged a\n" and fake.ours["docs/b.md"] == "branch b\n"


@pytest.mark.asyncio
async def test_update_branch_after_a_stopped_alignment_never_merges_over_it() -> None:
    # xtm-one #1254: a call stopped after its 'Take main's version' commits left the i18n catalogs at main's
    # version; GitHub's update-branch then merged cleanly over them and the PR's own keys were lost. The
    # update now moves the branch off those commits first, so the conflict is seen and both sides kept.
    ours, theirs = _sides()
    fake = FakeGitHub(ours, theirs, both=["docs/b.md", "src/a.py"])
    fake.stop_armed = True
    stopped = _stop_after_alignment(fake)
    updates: list[str] = []

    async def github(client: Any, method: str, url: str, provider: str, action: str, **kwargs: Any) -> Any:
        if method == "get" and url == f"{API}/pulls/7":
            pr = {"head": {"ref": "feat/x", "repo": {"full_name": "acme/app"}}, "base": {"ref": "main"}}
            return httpx.Response(200, json=pr), None
        if method == "put" and url == f"{API}/pulls/7/update-branch":
            updates.append(fake.tip)
            if any(fake.ours.get(p) != fake.theirs.get(p) for p in fake.both):
                return httpx.Response(422, json={"message": "merge conflict between base and head"}), "Error: 422"
            fake.merged = True
            return httpx.Response(202, json={"message": "Updating pull request branch."}), None
        return await stopped(client, method, url, provider, action, **kwargs)

    resolutions = [{"path": "src/a.py", "keep": "branch"}, {"path": "docs/b.md", "keep": "branch"}]
    with patch(_SAFE_REQUEST, new=github):
        with pytest.raises(_WorkerStop):
            await _merge()(repo="acme/app", branch="feat/x", resolutions=resolutions)
        _as_left_by_an_older_build(fake)
        assert fake.ours["src/a.py"] == "main a\n"
        fake.stop_armed = False
        result = await _manage()(repo="acme/app", number=7, action="update_branch")

    assert updates == [OURS], updates
    assert fake.ref_updates == [
        {"name": "refs/heads/feat/x", "beforeOid": format(0xC0FFEE, "040x"), "afterOid": OURS, "force": True}
    ]
    assert not fake.merged and fake.ours == ours
    assert result.startswith("Error: the branch of PR #7 in acme/app cannot take 'main'"), result
    assert "moved back to " + OURS[:12] in result


@pytest.mark.asyncio
async def test_a_stopped_alignment_that_cannot_be_dropped_merges_nothing():
    ours, theirs = _sides()
    fake = FakeGitHub(ours, theirs, both=["src/a.py"])
    fake.stop_armed = True
    with patch(_SAFE_REQUEST, new=_stop_after_alignment(fake)):
        with pytest.raises(_WorkerStop):
            await _merge()(repo="acme/app", branch="feat/x", resolutions=[{"path": "src/a.py", "keep": "branch"}])
        _as_left_by_an_older_build(fake)
        fake.stop_armed, fake.refuse_ref_update = False, True
        merges_before = fake.posts().count(f"{API}/merges")
        result = await _merge()(repo="acme/app", branch="feat/x")

    assert result.startswith("Error: ") and "stopped call of this tool" in result and "Ref update refused" in result
    assert "Nothing was merged" in result and fake.posts().count(f"{API}/merges") == merges_before and not fake.merged


@pytest.mark.asyncio
async def test_a_branch_tip_that_is_not_an_alignment_commit_is_left_alone():
    ours, theirs = _sides()
    fake = FakeGitHub(ours, theirs, both=["src/a.py"])
    fake.tip_parent, fake.tip_headline = BASE, "Take care of the main page"
    with patch(_SAFE_REQUEST, new=fake):
        result = await _merge()(repo="acme/app", branch="feat/x", resolutions=[{"path": "src/a.py", "keep": "branch"}])

    assert result.startswith("Merged 'main' into 'feat/x'"), result
    assert not fake.ref_updates and "moved back to" not in result


@pytest.mark.asyncio
async def test_a_branch_side_over_the_cap_writes_nothing():
    big = "x" * (5 * 1024 * 1024 + 1)
    fake = FakeGitHub({"a.bin": big}, {"a.bin": "small\n"}, both=["a.bin"])
    with patch(_SAFE_REQUEST, new=fake):
        result = await _merge()(repo="acme/app", branch="feat/x", resolutions=[{"path": "a.bin", "keep": "branch"}])

    assert result.startswith("Error: ") and "nothing was written" in result
    assert fake.commits == [] and fake.blob_reads() == 0


@pytest.mark.asyncio
async def test_without_listed_sizes_the_payload_is_still_capped():
    big = "x" * (5 * 1024 * 1024 + 1)
    fake = FakeGitHub({"a.bin": big}, {"a.bin": "small\n"}, both=["a.bin"], omit_sizes=True)
    with patch(_SAFE_REQUEST, new=fake):
        result = await _merge()(repo="acme/app", branch="feat/x", resolutions=[{"path": "a.bin", "keep": "branch"}])

    assert result.startswith("Error: ") and "nothing was written" in result and fake.commits == []


@pytest.mark.asyncio
async def test_paths_equal_on_both_sides_read_no_blob():
    fake = FakeGitHub({"same.txt": "x\n"}, {"same.txt": "x\n"}, both=["same.txt"])
    with patch(_SAFE_REQUEST, new=fake):
        await _merge()(repo="acme/app", branch="feat/x", resolutions=[{"path": "same.txt", "keep": "branch"}])

    assert fake.blob_reads() == 0


@pytest.mark.asyncio
async def test_a_workspace_file_is_a_resolution():
    ours, theirs = _sides()
    fake = FakeGitHub(ours, theirs, both=["src/a.py"])
    workspace = MagicMock()

    async def read_file(path, *, tool):
        assert (path, tool) == ("xo/src/a.py", "merge_github_branch")
        return path, "a.py", b"from the checkout\n"

    workspace.read_file = read_file
    with patch(_SAFE_REQUEST, new=fake), patch(_WORKSPACE, return_value=workspace):
        result = await _merge()(
            repo="acme/app", branch="feat/x", resolutions=[{"path": "src/a.py", "workspace_path": "xo/src/a.py"}]
        )

    assert result.startswith("Merged")
    assert fake.ours["src/a.py"] == "from the checkout\n"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("resolutions", "message"),
    [
        ([{"path": "src/a.py", "keep": "ref"}], "with no resolution (1): docs/b.md"),
        (
            [{"path": "src/a.py", "keep": "ref"}, {"path": "docs/b.md", "keep": "ref"}, {"path": "x.md", "keep": "ref"}],
            "not changed on both sides (1): x.md",
        ),
    ],
)
async def test_resolutions_that_do_not_settle_the_conflict_write_nothing(resolutions, message):
    ours, theirs = _sides()
    fake = FakeGitHub(ours, theirs, both=["docs/b.md", "src/a.py"])
    with patch(_SAFE_REQUEST, new=fake):
        result = await _merge()(repo="acme/app", branch="feat/x", resolutions=resolutions)

    assert result.startswith("Error: ") and message in result and "nothing was written" in result
    assert fake.posts() == [f"{API}/merges"] and fake.commits == []


@pytest.mark.asyncio
async def test_an_executable_file_on_both_sides_is_resolved_like_a_plain_file() -> None:
    # createCommitOnBranch keeps the mode of a path it overwrites, so a script 100755 on both sides merges.
    ours, theirs = _sides()
    fake = FakeGitHub(ours, theirs, both=["src/a.py"], modes={"src/a.py": "100755"})
    with patch(_SAFE_REQUEST, new=fake):
        result = await _merge()(
            repo="acme/app", branch="feat/x", resolutions=[{"path": "src/a.py", "content": "merged a\n"}]
        )

    assert result.startswith("Merged 'main' into 'feat/x' of acme/app with its conflicts resolved"), result
    assert fake.merged and fake.ours["src/a.py"] == "merged a\n"


@pytest.mark.asyncio
async def test_an_executable_bit_on_one_side_only_is_resolved_by_hand() -> None:
    ours, theirs = _sides()
    fake = FakeGitHub(ours, theirs, both=["src/a.py"], modes={"src/a.py": "100755"}, their_modes={})
    with patch(_SAFE_REQUEST, new=fake):
        result = await _merge()(repo="acme/app", branch="feat/x", resolutions=[{"path": "src/a.py", "keep": "ref"}])

    assert "is an executable file on 'feat/x' only" in result and "Nothing was written" in result
    assert fake.commits == []


@pytest.mark.asyncio
async def test_a_symbolic_link_is_resolved_by_hand() -> None:
    ours, theirs = _sides()
    fake = FakeGitHub(ours, theirs, both=["src/a.py"], modes={"src/a.py": "120000"})
    with patch(_SAFE_REQUEST, new=fake):
        result = await _merge()(repo="acme/app", branch="feat/x", resolutions=[{"path": "src/a.py", "keep": "ref"}])

    assert "is a symbolic link on 'feat/x'" in result and "Nothing was written" in result
    assert fake.commits == []


@pytest.mark.asyncio
async def test_a_merge_still_refused_is_undone_with_a_signed_commit():
    ours, theirs = _sides()
    fake = FakeGitHub(ours, theirs, both=["src/a.py"], always_conflict=True)
    with patch(_SAFE_REQUEST, new=fake):
        result = await _merge()(repo="acme/app", branch="feat/x", resolutions=[{"path": "src/a.py", "keep": "branch"}])

    assert result.startswith("Error: ") and "was moved back to 111111111111" in result
    assert len(fake.commits) == 1 and not fake.merged and fake.tip == OURS
    assert fake.ours["src/a.py"] == "branch a\n"


@pytest.mark.asyncio
async def test_without_resolutions_the_conflict_answer_names_the_argument():
    ours, theirs = _sides()
    fake = FakeGitHub(ours, theirs, both=["src/a.py"])
    with patch(_SAFE_REQUEST, new=fake):
        result = await _merge()(repo="acme/app", branch="feat/x")

    assert result.startswith("Error: merging 'main' into 'feat/x' of acme/app conflicts")
    assert "'resolutions'" in result and "sandbox_checkout_rebase" in result
    assert fake.commits == []


_CAP = 5 * 1024 * 1024
_MB = 1024 * 1024


def _catalogues(prefix: str) -> dict[str, str]:
    """Four 1.5 MB files: a 6 MB conflict set, over the 5 MB one signed commit may carry."""
    return {f"i18n/{lang}.json": prefix + lang + "x" * (3 * _MB // 2) for lang in ("de", "en", "fr", "ja")}


@pytest.mark.asyncio
async def test_a_6_mb_conflict_set_lands_in_commits_each_under_the_cap():
    ours, theirs = _catalogues("branch "), _catalogues("main ")
    fake = FakeGitHub(ours, theirs, both=sorted(ours))
    with patch(_SAFE_REQUEST, new=fake):
        result = await _merge()(repo="acme/app", branch="feat/x", resolutions=[{"path": p, "keep": "branch"} for p in ours])

    assert result.startswith("Merged 'main' into 'feat/x' of acme/app with its conflicts resolved"), result
    assert fake.merged
    sizes = [sum(len(base64.b64decode(item["contents"])) for item in commit.get("additions", [])) for commit in fake.commits]
    assert len(fake.commits) == 4 and all(0 < size <= _CAP for size in sizes)
    assert [commit["headline"].endswith(f"({n}/2)") for n, commit in zip((1, 2, 1, 2), fake.commits, strict=True)] == [
        True
    ] * 4
    # The two alignment commits come before the merge, the two resolution commits after it.
    graphql_then_merge = [url for url in fake.posts() if url in (GRAPHQL, f"{API}/merges")]
    assert graphql_then_merge == [f"{API}/merges", GRAPHQL, GRAPHQL, f"{API}/merges", GRAPHQL, GRAPHQL]
    assert fake.ours == ours
    # Every resolved path is named with the commit that carried it, and the tip is checked for all of them.
    assert all(path in result for path in ours)
    assert f"Checked on {fake.tip[:12]}: the 4 resolved files hold the resolved content." in result


def _refuse_resolution_chunk(fake: FakeGitHub, times: int):
    """The fake, refusing the (2/2) resolution commit *times* times while the branch stays where it was."""
    real = fake.__call__
    refused = {"count": 0}

    async def handler(client, method, url, provider, action, **kwargs):
        body = str(kwargs.get("json"))
        chunk = method == "post" and url == GRAPHQL and "Resolve the merge" in body and "(2/2)" in body
        if chunk and refused["count"] < times:
            refused["count"] += 1
            fake.calls.append((method, url, kwargs))
            return httpx.Response(200, json={"errors": [{"message": "Something went wrong"}]}), None
        return await real(client, method, url, provider, action, **kwargs)

    return handler, refused


@pytest.mark.asyncio
async def test_a_refused_resolution_chunk_is_retried_once_and_lands():
    ours, theirs = _catalogues("branch "), _catalogues("main ")
    fake = FakeGitHub(ours, theirs, both=sorted(ours))
    handler, refused = _refuse_resolution_chunk(fake, times=1)
    with patch(_SAFE_REQUEST, new=handler):
        result = await _merge()(
            repo="acme/app", branch="feat/x", resolutions=[{"path": p, "keep": "branch"} for p in ours]
        )

    assert refused["count"] == 1
    assert result.startswith("Merged 'main' into 'feat/x' of acme/app with its conflicts resolved"), result
    assert fake.ours == ours and [c["headline"][-5:] for c in fake.commits] == ["(1/2)", "(2/2)", "(1/2)", "(2/2)"]


@pytest.mark.asyncio
async def test_a_resolution_chunk_that_never_lands_is_an_error_naming_every_missing_path():
    ours, theirs = _catalogues("branch "), _catalogues("main ")
    fake = FakeGitHub(ours, theirs, both=sorted(ours))
    handler, refused = _refuse_resolution_chunk(fake, times=2)
    with patch(_SAFE_REQUEST, new=handler):
        result = await _merge()(
            repo="acme/app", branch="feat/x", resolutions=[{"path": p, "keep": "branch"} for p in ours]
        )

    assert refused["count"] == 2 and fake.merged
    assert result.startswith("Error: "), result
    assert "1 of 4 resolved files not written" in result and "holds the ref's version of i18n/ja.json." in result
    assert "Call this tool again with the same resolutions" in result
    # The chunk that landed is listed with its commit.
    assert "i18n/de.json, i18n/en.json, i18n/fr.json." in result


@pytest.mark.asyncio
async def test_a_tip_missing_a_resolved_file_after_every_commit_is_an_error():
    ours, theirs = _sides()
    fake = FakeGitHub(ours, theirs, both=["docs/b.md", "src/a.py"])
    real = fake.__call__

    async def drop_after_resolution(client, method, url, provider, action, **kwargs):
        answer = await real(client, method, url, provider, action, **kwargs)
        if method == "post" and url == GRAPHQL and "Resolve the merge" in str(kwargs.get("json")):
            fake.ours["src/a.py"] = "main a\n"  # a commit GitHub answered for but the tip does not hold
        return answer

    with patch(_SAFE_REQUEST, new=drop_after_resolution):
        result = await _merge()(
            repo="acme/app",
            branch="feat/x",
            resolutions=[{"path": "src/a.py", "content": "merged a\n"}, {"path": "docs/b.md", "keep": "branch"}],
        )

    assert result.startswith("Error: ") and "does not hold the resolved content of src/a.py" in result, result
    # Both blob ids are named, so a real difference is told from a stale read.
    assert f"expected blob {_sha('merged a\n')[:12]}, found {_sha('main a\n')[:12]}" in result, result


@pytest.mark.asyncio
async def test_a_branch_read_lagging_the_resolution_commit_is_not_a_mismatch(monkeypatch):
    # The case of 11 Nov 2026: the branch read by name still answered the merge commit right after the resolution
    # commit, so a correct resolution was reported missing. The check reads the resolution commit by its id.
    monkeypatch.setattr(merge_resolution, "_READ_BACK_DELAYS", (0, 0))
    ours, theirs = _sides()
    fake = FakeGitHub(ours, theirs, both=["docs/b.md", "src/a.py"], stale_branch_reads=1)
    with patch(_SAFE_REQUEST, new=fake):
        result = await _merge()(
            repo="acme/app",
            branch="feat/x",
            resolutions=[{"path": "src/a.py", "content": "merged a\n"}, {"path": "docs/b.md", "keep": "branch"}],
        )

    assert result.startswith("Merged 'main' into 'feat/x' of acme/app with its conflicts resolved"), result
    assert f"Checked on {fake.tip[:12]}: the 2 resolved files hold the resolved content." in result, result
    assert "another writer" not in result, result
    assert f"{API}/commits/{fake.tip}" in [url for verb, url, _ in fake.calls if verb == "get"]


@pytest.mark.asyncio
async def test_a_branch_that_stays_on_another_commit_after_the_resolution_is_named(monkeypatch):
    monkeypatch.setattr(merge_resolution, "_READ_BACK_DELAYS", (0, 0))
    ours, theirs = _sides()
    fake = FakeGitHub(ours, theirs, both=["docs/b.md", "src/a.py"], stale_branch_reads=10)
    with patch(_SAFE_REQUEST, new=fake):
        result = await _merge()(
            repo="acme/app",
            branch="feat/x",
            resolutions=[{"path": "src/a.py", "content": "merged a\n"}, {"path": "docs/b.md", "keep": "branch"}],
        )

    # The resolution itself is verified on its commit; the branch pointing elsewhere is a warning, not an error.
    assert result.startswith("Merged 'main' into 'feat/x'"), result
    assert f"'feat/x' now points at {MERGE[:12]}, not at {fake.tip[:12]}" in result, result


def _stop_at_second_resolution_chunk(fake: FakeGitHub):
    """The fake, cut at the (2/2) resolution commit while armed; the Merges API answers 204 once merged."""
    real = fake.__call__

    async def handler(client, method, url, provider, action, **kwargs):
        body = str(kwargs.get("json"))
        if method == "post" and url == GRAPHQL and fake.stop_armed and "Resolve the merge" in body and "(2/2)" in body:
            raise _WorkerStop
        if method == "post" and url == f"{API}/merges" and fake.merged:
            fake.calls.append((method, url, kwargs))
            return httpx.Response(204), None
        return await real(client, method, url, provider, action, **kwargs)

    return handler


@pytest.mark.asyncio
async def test_a_replay_after_a_stop_between_resolution_chunks_writes_the_missing_paths():
    ours, theirs = _catalogues("branch "), _catalogues("main ")
    settled = _catalogues("merged ")
    resolutions = [{"path": path, "content": text} for path, text in settled.items()]
    fake = FakeGitHub(ours, theirs, both=sorted(ours))
    fake.stop_armed = True
    with patch(_SAFE_REQUEST, new=_stop_at_second_resolution_chunk(fake)):
        with pytest.raises(_WorkerStop):
            await _merge()(repo="acme/app", branch="feat/x", resolutions=resolutions)
        _as_left_by_an_older_build(fake)
        # The stopped call left '(1/2)' alone: the last file still holds main's version.
        assert fake.tip_headline.endswith("(1/2)") and fake.ours["i18n/ja.json"] == theirs["i18n/ja.json"]
        fake.stop_armed = False
        result = await _merge()(repo="acme/app", branch="feat/x", resolutions=resolutions)

    assert result.startswith("Finished a stopped resolution"), result
    assert "i18n/ja.json" in result and fake.ours == settled
    assert fake.commits[-1]["headline"] == "Resolve the merge of main: 1 file left by a stopped call"
    # A second replay finds every resolution on the tip: no change, nothing written.
    with patch(_SAFE_REQUEST, new=_stop_at_second_resolution_chunk(fake)):
        again = await _merge()(repo="acme/app", branch="feat/x", resolutions=resolutions)
    # Two alignment commits, the stopped '(1/2)' and the finishing commit: the replay adds none.
    assert again.startswith("No change: ") and len(fake.commits) == 4


@pytest.mark.asyncio
async def test_a_keep_only_replay_over_a_stopped_resolution_names_it_instead_of_no_change():
    ours, theirs = _catalogues("branch "), _catalogues("main ")
    resolutions = [{"path": path, "keep": "branch"} for path in ours]
    fake = FakeGitHub(ours, theirs, both=sorted(ours))
    fake.stop_armed = True
    with patch(_SAFE_REQUEST, new=_stop_at_second_resolution_chunk(fake)):
        with pytest.raises(_WorkerStop):
            await _merge()(repo="acme/app", branch="feat/x", resolutions=resolutions)
        _as_left_by_an_older_build(fake)
        assert fake.tip_headline.endswith("(1/2)")
        written = len(fake.commits)
        fake.stop_armed = False
        result = await _merge()(repo="acme/app", branch="feat/x", resolutions=resolutions)

    # A keep is settled per hunk and cannot be checked by blob id: the stopped (1/2) tip is named, never hidden.
    assert result.startswith("Error: ") and "left at part 1 of 2" in result, result
    assert all(path in result for path in ours) and "No change" not in result
    assert len(fake.commits) == written


def test_merge_github_branch_is_replay_safe_so_a_resumed_turn_finishes_a_stopped_resolution():
    executor = MagicMock()
    executor.register = MagicMock()
    definitions = build_tools("fake-token", executor)
    merge = next(tool for tool in definitions if tool.name == "merge_github_branch")
    assert merge.replay_safe is True


@pytest.mark.asyncio
async def test_an_already_merged_branch_missing_a_resolution_it_did_not_leave_writes_nothing():
    ours, theirs = _sides()
    fake = FakeGitHub(ours, theirs, both=["src/a.py"])
    fake.merged, fake.tip, fake.ours = True, MERGE, dict(theirs)
    with patch(_SAFE_REQUEST, new=_stop_at_second_resolution_chunk(fake)):
        fake.stop_armed = False
        result = await _merge()(repo="acme/app", branch="feat/x", resolutions=[{"path": "src/a.py", "content": "x\n"}])

    assert result.startswith("Error: ") and "does not hold the resolved content of src/a.py" in result, result
    assert fake.commits == []


@pytest.mark.asyncio
async def test_a_failed_second_alignment_commit_undoes_the_first():
    ours, theirs = _catalogues("branch "), _catalogues("main ")
    fake = FakeGitHub(ours, theirs, both=sorted(ours))
    real = fake.__call__

    async def refuse_second_commit(client, method, url, provider, action, **kwargs):
        if method == "post" and url == GRAPHQL and len(fake.commits) == 1 and "(2/2)" in str(kwargs["json"]):
            fake.calls.append((method, url, kwargs))
            return httpx.Response(200, json={"errors": [{"message": "refused"}]}), None
        return await real(client, method, url, provider, action, **kwargs)

    with patch(_SAFE_REQUEST, new=refuse_second_commit):
        result = await _merge()(repo="acme/app", branch="feat/x", resolutions=[{"path": p, "keep": "ref"} for p in ours])

    assert "Nothing was merged" in result and "was moved back to" in result
    assert not fake.merged and fake.ours == ours and fake.tip == OURS


@pytest.mark.asyncio
async def test_a_single_file_over_the_cap_is_named_with_its_size():
    big = "x" * (_CAP + 1)
    fake = FakeGitHub({"a.bin": big, "b.txt": "b\n"}, {"a.bin": "small\n", "b.txt": "main b\n"}, both=["a.bin", "b.txt"])
    with patch(_SAFE_REQUEST, new=fake):
        result = await _merge()(
            repo="acme/app", branch="feat/x",
            resolutions=[{"path": "a.bin", "keep": "branch"}, {"path": "b.txt", "keep": "ref"}],
        )

    assert f"a.bin ({_CAP + 1} bytes)" in result and "no split" in result and "nothing was written" in result
    assert "b.txt (" not in result and fake.commits == [] and fake.blob_reads() == 0


@pytest.mark.asyncio
async def test_a_set_over_the_read_budget_names_the_split_and_writes_nothing(monkeypatch):
    monkeypatch.setattr(merge_resolution, "_MAX_READ_BYTES", 4 * _MB)
    ours, theirs = _catalogues("branch "), _catalogues("main ")
    fake = FakeGitHub(ours, theirs, both=sorted(ours))
    with patch(_SAFE_REQUEST, new=fake):
        result = await _merge()(repo="acme/app", branch="feat/x", resolutions=[{"path": p, "keep": "branch"} for p in ours])

    assert result.startswith("Error: ") and "nothing was written" in result
    assert "would be 2 signed commits" in result and "i18n/de.json" in result
    assert fake.commits == [] and fake.blob_reads() == 0


def test_split_batches_keeps_each_batch_under_the_cap():
    sizes = {f"f{i}": 2 * _MB for i in range(5)} | {"tiny": 10}
    batches = split_batches(sizes)
    assert sorted(p for batch in batches for p in batch) == sorted(sizes)
    assert all(sum(sizes[p] for p in batch) <= _CAP for batch in batches)
    assert len(batches) == 3
    assert split_batches({"huge": _CAP + 1, "a": 1}) == [["huge"], ["a"]]
    assert len(split_batches({f"p{i}": 0 for i in range(250)})) == 3


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ("nope", "must be a list"),
        ([{"path": "a", "keep": "ref", "content": "x"}], "exactly one of"),
        ([{"path": "a", "keep": "theirs"}], "must be one of branch, ref"),
        ([{"path": "a", "keep": "ref"}, {"path": "a", "keep": "branch"}], "resolved more than once"),
        ([{"path": "../a", "keep": "ref"}], "segment"),
    ],
)
def test_parse_resolutions_refusals(raw, message):
    parsed, err = parse_resolutions(raw)
    assert parsed == {} and err is not None and message in err


def test_parse_resolutions_accepts_each_way():
    parsed, err = parse_resolutions(
        [{"path": "a", "keep": "ref"}, {"path": "b", "content": ""}, {"path": "c", "workspace_path": "xo/c"}]
    )
    assert err is None
    assert parsed["a"].keep == "ref" and parsed["b"].content == "" and parsed["c"].workspace_path == "xo/c"
