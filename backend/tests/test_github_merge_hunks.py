"""The line merge of a conflicting ``merge_github_branch`` / ``manage_github_pr update_branch``, on the server.

Unit tests on :func:`merge_path` (clean, overlap, binary, delete against a
change, added on both sides) and on the union merge of ``merge_text``, then
tool tests on a fixture repository: a fake GitHub holding the merge base, the
branch and the ref as ``{path: text}``, answering the Merges API (409 while a
path changed on both sides differs), the compare, commit, tree and blob reads
and ``createCommitOnBranch``.
"""

from __future__ import annotations

import base64
import hashlib
import json
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.integrations.providers.github import build_tools, merge_hunks
from app.integrations.providers.github.merge_hunks import describe_conflicts, line_merge, merge_path
from app.services.sandbox_workspace.rebase import merge_text

_SAFE_REQUEST = "app.integrations.providers.github._common.safe_request"

API = "https://api.github.com/repos/acme/app"
GRAPHQL = "https://api.github.com/graphql"
BASE = "b" * 40
OURS = "1" * 40
THEIRS = "2" * 40
MERGE = "5" * 40
TREES = {"base": "d" * 40, "ours": "a" * 40, "theirs": "c" * 40}

BASE_FILE = "".join(f"line {n}\n" for n in range(1, 11))


def _edit(text: str, number: int, new: str) -> str:
    return text.replace(f"line {number}\n", new)


def _sha(text: str) -> str:
    data = text.encode()
    return hashlib.sha1(b"blob %d\0" % len(data) + data, usedforsecurity=False).hexdigest()


# ---------------------------------------------------------------------------
# merge_path / merge_text
# ---------------------------------------------------------------------------


def test_changes_that_do_not_overlap_merge_cleanly():
    ours = _edit(BASE_FILE, 2, "branch 2\n")
    theirs = _edit(BASE_FILE, 8, "main 8\n")
    merge = merge_path("a.py", BASE_FILE.encode(), ours.encode(), theirs.encode())
    assert merge.clean and merge.merged == _edit(ours, 8, "main 8\n")


def test_an_overlap_names_its_hunk_on_both_sides():
    ours = _edit(BASE_FILE, 5, "branch 5\n")
    theirs = _edit(BASE_FILE, 5, "main 5\nmain 5b\n")
    merge = merge_path("a.py", BASE_FILE.encode(), ours.encode(), theirs.encode())
    assert not merge.clean and len(merge.regions) == 1
    region = merge.regions[0]
    assert merge.ours[region.ours_start : region.ours_end] == ("branch 5",)
    assert merge.theirs[region.theirs_start : region.theirs_end] == ("main 5", "main 5b")
    text = describe_conflicts([merge], "feat/x", "main")
    assert "a.py: 1 overlapping hunk" in text
    assert "'feat/x' line 5, 'main' lines 5-6" in text
    assert "| branch 5" in text and "| main 5b" in text
    assert merge.union == _edit(BASE_FILE, 5, "branch 5\nmain 5\nmain 5b\n")


def test_a_binary_changed_on_both_sides_is_resolved_whole():
    merge = merge_path("logo.png", b"\x00base", b"\x00ours", b"\x00theirs")
    assert not merge.clean and "binary" in merge.reason and merge.union is None
    assert "logo.png: a binary file changed on both sides" in describe_conflicts([merge], "feat/x", "main")


@pytest.mark.parametrize(("ours", "theirs", "gone"), [(None, b"changed\n", "the branch"), (b"changed\n", None, "the ref")])
def test_a_delete_against_a_change_is_resolved_whole(ours, theirs, gone):
    merge = merge_path("a.py", b"base\n", ours, theirs)
    assert not merge.clean and f"removed on {gone}" in merge.reason


def test_a_path_added_on_both_sides_with_different_text_conflicts():
    merge = merge_path("new.md", None, b"branch\n", b"main\n")
    assert not merge.clean and merge.reason == "added on both sides" and merge.union == "branch\nmain\n"


def test_the_union_merge_keeps_both_sides_without_markers():
    merged = merge_text("a\nb\nc\n", "a\nB\nc\n", "a\nb2\nc\n", both=True)
    assert merged.text == "a\nB\nb2\nc\n" and merged.conflicts == 1
    assert "<<<<<<<" not in merged.text


def test_the_regions_are_reported_without_changing_the_marked_text():
    merged = merge_text("a\nb\nc\n", "a\nB\nc\n", "a\nb2\nc\n")
    assert merged.text == "a\n<<<<<<< workspace\nB\n=======\nb2\n>>>>>>> ref\nc\n"
    assert [(r.ours_start, r.ours_end, r.theirs_start, r.theirs_end) for r in merged.regions] == [(1, 2, 1, 2)]


def test_a_change_of_line_endings_alone_is_not_a_conflict():
    merged = merge_text("a\nb\nc\nd\n", "a\r\nB\r\nc\r\nd\r\n", "a\nb\nc\nD\n")
    assert merged.conflicts == 0 and merged.text == "a\r\nB\r\nc\r\nD\r\n"
    lf_ours = merge_text("a\r\nb\r\nc\r\nd\r\n", "a\nB\nc\nd\n", "a\r\nb\r\nc\r\nD\r\n")
    assert lf_ours.conflicts == 0 and lf_ours.text == "a\nB\nc\nD\n"


def test_a_real_overlap_across_line_endings_is_still_one_conflict():
    merged = merge_text("a\nb\nc\n", "a\r\nB\r\nc\r\n", "a\nb2\nc\n")
    assert merged.conflicts == 1
    assert merged.text == "a\r\n<<<<<<< workspace\r\nB\r\n=======\r\nb2\r\n>>>>>>> ref\r\nc\r\n"


def test_conflict_markers_are_found_only_as_a_whole_hunk():
    from app.services.sandbox_workspace.rebase import has_conflict_markers

    assert has_conflict_markers(b"a\n<<<<<<< workspace\nX\n=======\nE\n>>>>>>> ref\n")
    assert has_conflict_markers(b"<<<<<<< workspace\r\nX\r\n=======\r\nE\r\n>>>>>>> ref\r\n")
    assert not has_conflict_markers(b"s = '<<<<<<< '\n")
    assert not has_conflict_markers(b">>>>>>> ref\n<<<<<<< workspace\n")


# ---------------------------------------------------------------------------
# The tools on a fixture repository
# ---------------------------------------------------------------------------


class FakeGitHub:
    """The merge base, the branch and the ref as ``{path: text}``; a merge conflicts while a shared path differs."""

    def __init__(
        self, base: dict[str, str], ours: dict[str, str], theirs: dict[str, str], modes: dict[str, str] | None = None
    ):
        self.sides = {"base": dict(base), "ours": dict(ours), "theirs": dict(theirs)}
        # modes: the tree mode of a path on every side (100755 for a script), 100644 otherwise.
        self.modes = modes or {}
        self.tip = OURS
        self.commits: list[dict[str, Any]] = []
        self.merged = False
        self.calls: list[tuple[str, str, dict]] = []
        self.blobs = {_sha(t): t for side in self.sides.values() for t in side.values()}
        # A resolved merge is built on a tmp/commit-batch/ scratch branch sharing the branch's state; the target
        # keeps its own tip (target_tip) until the one fast-forward moves it.
        self.scratch: str | None = None
        self.target_tip: str | None = None
        self.target_moves: list[str] = []

    def _changed(self, side: str) -> list[dict]:
        base, mine = self.sides["base"], self.sides[side]
        files = []
        for path in sorted(set(base) | set(mine)):
            if base.get(path) != mine.get(path):
                removed = path not in mine
                files.append({"filename": path, "status": "removed" if removed else "modified", "sha": None if removed else _sha(mine[path])})
        return files

    def _conflict(self) -> bool:
        ours, theirs = self._changed("ours"), self._changed("theirs")
        shared = {f["filename"] for f in ours} & {f["filename"] for f in theirs}
        return any(self.sides["ours"].get(p) != self.sides["theirs"].get(p) for p in shared)

    def posts(self) -> list[str]:
        return [url for verb, url, _ in self.calls if verb == "post"]

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
            return httpx.Response(204), None
        if method == "patch" and url == f"{API}/git/refs/heads/feat/x":
            assert kwargs["json"]["force"] is False and kwargs["json"]["sha"] == self.tip
            self.target_moves.append(kwargs["json"]["sha"])
            self.target_tip = None
            return ok({"object": {"sha": kwargs["json"]["sha"]}})
        if self.scratch and self.scratch in url:
            url = url.replace(self.scratch, "feat/x")
        elif self.target_tip is not None and method == "get" and url == f"{API}/commits/feat/x":
            return ok({"sha": self.target_tip, "commit": {"tree": {"sha": TREES["ours"]}}})

        if method == "get" and url == API:
            return ok({"default_branch": "main"})
        if method == "put" and url == f"{API}/pulls/7/update-branch":
            return httpx.Response(422, json={"message": "merge conflict between base and head"}), "Error: 422"
        if method == "get" and url == f"{API}/pulls/7":
            return ok({"head": {"ref": "feat/x", "repo": {"full_name": "acme/app"}}, "base": {"ref": "main"}})
        if method == "post" and url == f"{API}/merges":
            if self._conflict():
                return httpx.Response(409, json={"message": "Merge conflict"}), "Error: GitHub API error (409)"
            parent, self.tip, self.merged = self.tip, MERGE, True
            for path, text in self.sides["theirs"].items():
                if self.sides["base"].get(path) != text and self.sides["ours"].get(path) == self.sides["base"].get(path):
                    self.sides["ours"][path] = text
            return ok({"sha": MERGE, "parents": [{"sha": parent}, {"sha": THEIRS}], "commit": {"verification": {"verified": True}}}, 201)
        if method == "get" and url == f"{API}/compare/feat/x...main":
            return ok({"merge_base_commit": {"sha": BASE}, "files": self._changed("theirs")})
        if method == "get" and url == f"{API}/compare/{BASE}...feat/x":
            return ok({"files": self._changed("ours")})
        for name, ref, commit in (("base", BASE, BASE), ("ours", "feat/x", None), ("theirs", "main", THEIRS)):
            # The branch tip is also read by its commit id (the check after the resolution commits).
            if method == "get" and url in (
                f"{API}/commits/{ref}",
                *((f"{API}/commits/{self.tip}",) if name == "ours" else ()),
            ):
                return ok({"sha": commit or self.tip, "commit": {"tree": {"sha": TREES[name]}}})
            if method == "get" and url == f"{API}/git/trees/{TREES[name]}":
                files = self.sides[name]
                tree = [
                    {
                        "path": p,
                        "type": "blob",
                        "mode": self.modes.get(p, "100644"),
                        "sha": _sha(t),
                        "size": len(t.encode()),
                    }
                    for p, t in files.items()
                ]
                return ok({"tree": tree, "truncated": False})
        if method == "get" and url.startswith(f"{API}/git/blobs/"):
            text = self.blobs[url.rsplit("/", 1)[1]]
            return ok({"encoding": "base64", "content": base64.b64encode(text.encode()).decode()})
        if method == "post" and url == GRAPHQL:
            commit_input = kwargs["json"]["variables"]["input"]
            assert commit_input["expectedHeadOid"] == self.tip
            changes = commit_input["fileChanges"]
            for item in changes.get("additions", []):
                text = base64.b64decode(item["contents"]).decode()
                self.sides["ours"][item["path"]] = text
                self.blobs[_sha(text)] = text
            self.tip = format(len(self.commits) + 0xC0FFEE, "040x")
            self.commits.append(changes)
            return ok({"data": {"createCommitOnBranch": {"commit": {"oid": self.tip, "signature": {"isValid": True, "state": "VALID"}}}}})
        raise AssertionError(f"unexpected {method.upper()} {url}")


def _tool(name: str, recorder: Any = None):
    executor = MagicMock()
    executor.register = MagicMock()
    build_tools("fake-token", executor, **({"self_action_recorder": recorder} if recorder else {}))
    return next(call[0][1] for call in executor.register.call_args_list if call[0][0] == name)


def _fixture(theirs_line: int, theirs_text: str, modes: dict[str, str] | None = None) -> FakeGitHub:
    base = {"src/frictions.py": BASE_FILE, "README.md": "readme\n"}
    ours = {"src/frictions.py": _edit(BASE_FILE, 3, "branch 3\n"), "README.md": "readme\n"}
    theirs = {"src/frictions.py": _edit(BASE_FILE, theirs_line, theirs_text), "README.md": "readme\nmain\n"}
    return FakeGitHub(base, ours, theirs, modes)


@pytest.mark.asyncio
async def test_a_clean_line_merge_lands_in_one_call():
    fake = _fixture(9, "main 9\n")
    with patch(_SAFE_REQUEST, new=fake):
        result = await _tool("merge_github_branch")(repo="acme/app", branch="feat/x")

    assert result.startswith("Merged 'main' into 'feat/x' of acme/app with its conflicts resolved"), result
    assert fake.merged and MERGE[:12] in result
    assert fake.sides["ours"]["src/frictions.py"] == _edit(_edit(BASE_FILE, 3, "branch 3\n"), 9, "main 9\n")


def _catalog(keys: dict[str, str]) -> str:
    return json.dumps(keys, indent=2) + "\n"


CATALOG = "frontend/src/i18n/lang/en.json"
CATALOG_BASE = {f"k{n}": f"v{n}" for n in range(12)}


def test_merge_path_names_the_json_keys_the_branch_removed() -> None:
    """#1095: a stale copy of a catalog removes main's keys without a conflict; the merge names them."""
    ours = {k: v for k, v in CATALOG_BASE.items() if k not in ("k3", "k4")}
    merge = merge_path(
        CATALOG,
        _catalog(CATALOG_BASE).encode(),
        _catalog(ours).encode(),
        _catalog({**CATALOG_BASE, "k9": "main"}).encode(),
    )
    assert merge.clean and merge.removed_keys == ("k3", "k4")
    assert "k3" not in json.loads(merge.merged or "{}")


def test_a_key_both_sides_removed_or_a_plain_file_names_nothing() -> None:
    both_gone = {k: v for k, v in CATALOG_BASE.items() if k != "k3"}
    merge = merge_path(
        CATALOG,
        _catalog(CATALOG_BASE).encode(),
        _catalog(both_gone).encode(),
        _catalog({**both_gone, "k9": "main"}).encode(),
    )
    assert merge.clean and merge.removed_keys == ()
    plain = merge_path(
        "notes.txt", BASE_FILE.encode(), _edit(BASE_FILE, 3, "").encode(), _edit(BASE_FILE, 9, "x\n").encode()
    )
    assert plain.removed_keys == ()


@pytest.mark.asyncio
async def test_a_landed_merge_names_the_catalog_keys_the_branch_removed() -> None:
    ours = {k: v for k, v in CATALOG_BASE.items() if k != "k5"}
    fake = FakeGitHub(
        {CATALOG: _catalog(CATALOG_BASE)},
        {CATALOG: _catalog(ours)},
        {CATALOG: _catalog({**CATALOG_BASE, "k11": "main"})},
    )
    with patch(_SAFE_REQUEST, new=fake):
        result = await _tool("merge_github_branch")(repo="acme/app", branch="feat/x")

    assert result.startswith("Merged 'main' into 'feat/x'"), result
    assert "Keys the branch removed that the merge base and the ref still hold (1 path(s))" in result
    assert f"- {CATALOG}: k5" in result
    assert result.rstrip().splitlines()[-1].startswith("Final head: ")


def test_merge_path_ref_first_keeps_the_refs_key_order() -> None:
    """#1116: main merged into a PR branch keeps main's appended keys first, the branch's own keys after."""
    base, ours, theirs = (
        _catalog(CATALOG_BASE),
        _catalog({**CATALOG_BASE, "pr1": "p", "pr2": "p"}),
        _catalog({**CATALOG_BASE, "main1": "m", "main2": "m"}),
    )
    default = merge_path(CATALOG, base.encode(), ours.encode(), theirs.encode())
    assert list(json.loads(default.merged or "{}"))[-4:] == ["pr1", "pr2", "main1", "main2"]
    merge = merge_path(CATALOG, base.encode(), ours.encode(), theirs.encode(), ref_first=True)
    assert merge.clean and list(json.loads(merge.merged or "{}"))[-4:] == ["main1", "main2", "pr1", "pr2"]


@pytest.mark.asyncio
async def test_a_landed_merge_of_main_keeps_mains_catalog_order() -> None:
    """#1116: the branch's diff against main after the merge holds the branch's keys only, appended last."""
    ours = {**CATALOG_BASE, "pr1": "p"}
    theirs = {**CATALOG_BASE, "main1": "m", "main2": "m"}
    fake = FakeGitHub({CATALOG: _catalog(CATALOG_BASE)}, {CATALOG: _catalog(ours)}, {CATALOG: _catalog(theirs)})
    with patch(_SAFE_REQUEST, new=fake):
        result = await _tool("merge_github_branch")(repo="acme/app", branch="feat/x")

    assert result.startswith("Merged 'main' into 'feat/x'"), result
    assert fake.sides["ours"][CATALOG] == _catalog({**theirs, "pr1": "p"})


@pytest.mark.asyncio
async def test_an_executable_script_on_every_side_merges_line_by_line() -> None:
    # createCommitOnBranch keeps the 100755 mode of the path it overwrites: no hand resolution is needed.
    fake = _fixture(9, "main 9\n", modes={"src/frictions.py": "100755"})
    with patch(_SAFE_REQUEST, new=fake):
        result = await _tool("merge_github_branch")(repo="acme/app", branch="feat/x")

    assert result.startswith("Merged 'main' into 'feat/x' of acme/app with its conflicts resolved"), result
    assert "not a plain file" not in result
    assert fake.sides["ours"]["src/frictions.py"] == _edit(_edit(BASE_FILE, 3, "branch 3\n"), 9, "main 9\n")


@pytest.mark.asyncio
async def test_an_overlap_answers_the_hunks_without_writing_then_a_resolution_lands():
    fake = _fixture(3, "main 3\n")
    merge = _tool("merge_github_branch")
    with patch(_SAFE_REQUEST, new=fake):
        refused = await merge(repo="acme/app", branch="feat/x")

    assert refused.startswith("Error: merging 'main' into 'feat/x' of acme/app conflicts, so nothing was merged.")
    assert "0 merge without an overlap" in refused and "1 hold overlapping hunks" in refused
    assert "src/frictions.py: 1 overlapping hunk" in refused and "'feat/x' line 3, 'main' line 3" in refused
    assert "| branch 3" in refused and "| main 3" in refused
    assert fake.commits == [] and fake.posts() == [f"{API}/merges"]

    with patch(_SAFE_REQUEST, new=fake):
        landed = await merge(repo="acme/app", branch="feat/x", resolutions=[{"path": "src/frictions.py", "keep": "both"}])

    assert landed.startswith("Merged 'main' into 'feat/x'"), landed
    assert fake.merged and fake.sides["ours"]["src/frictions.py"] == _edit(BASE_FILE, 3, "branch 3\nmain 3\n")


@pytest.mark.asyncio
async def test_a_second_call_resolves_only_the_conflicting_paths():
    base = {"a.py": BASE_FILE, "b.py": BASE_FILE}
    ours = {"a.py": _edit(BASE_FILE, 3, "branch 3\n"), "b.py": _edit(BASE_FILE, 1, "branch 1\n")}
    theirs = {"a.py": _edit(BASE_FILE, 3, "main 3\n"), "b.py": _edit(BASE_FILE, 10, "main 10\n")}
    fake = FakeGitHub(base, ours, theirs)
    with patch(_SAFE_REQUEST, new=fake):
        refused = await _tool("merge_github_branch")(repo="acme/app", branch="feat/x")
        landed = await _tool("merge_github_branch")(
            repo="acme/app", branch="feat/x", resolutions=[{"path": "a.py", "content": "settled\n"}]
        )

    assert "Merged without an overlap (1):\n- b.py" in refused and "a.py: 1 overlapping hunk" in refused
    assert landed.startswith("Merged"), landed
    assert fake.sides["ours"]["a.py"] == "settled\n"
    assert fake.sides["ours"]["b.py"] == _edit(_edit(BASE_FILE, 1, "branch 1\n"), 10, "main 10\n")


@pytest.mark.asyncio
async def test_the_conflict_answer_pins_a_symbolic_ref_to_its_commit():
    fake = _fixture(3, "main 3\n")
    with patch(_SAFE_REQUEST, new=fake):
        refused = await _tool("merge_github_branch")(repo="acme/app", branch="feat/x")

    assert f"'main' is at {THEIRS} for this answer: pass from_ref '{THEIRS}' with the resolutions" in refused


@pytest.mark.asyncio
async def test_a_compare_at_its_cap_reads_the_paths_from_the_trees():
    base = {f"f{i:03}.txt": f"{i}\n" for i in range(301)}
    ours = {**base, **{f"f{i:03}.txt": f"branch {i}\n" for i in range(301)}}
    theirs = {**base, "f300.txt": "main 300\n"}
    fake = FakeGitHub(base, ours, theirs)
    full = fake._changed
    conflicts = fake._conflict()
    fake._changed = lambda side: full(side)[:300]  # GitHub's compare stops at 300 files
    fake._conflict = lambda: conflicts
    with patch(_SAFE_REQUEST, new=fake):
        refused = await _tool("merge_github_branch")(repo="acme/app", branch="feat/x")

    assert "f300.txt: 1 overlapping hunk" in refused, refused
    assert f"{API}/git/trees/{TREES['base']}" in [url for _, url, _ in fake.calls]


@pytest.mark.asyncio
async def test_update_branch_refused_on_a_conflict_merges_line_by_line():
    fake = _fixture(9, "main 9\n")
    recorder = AsyncMock()
    with patch(_SAFE_REQUEST, new=fake):
        result = await _tool("manage_github_pr", recorder)(repo="acme/app", number=7, action="update_branch")

    assert result.startswith("GitHub refused to update the branch of PR #7 on a conflict; merged line by line"), result
    assert fake.merged
    recorder.assert_awaited_once_with("acme/app", 7)


@pytest.mark.asyncio
async def test_update_branch_with_an_overlap_names_the_hunks_and_the_next_call():
    fake = _fixture(3, "main 3\n")
    with patch(_SAFE_REQUEST, new=fake):
        result = await _tool("manage_github_pr")(repo="acme/app", number=7, action="update_branch")

    assert result.startswith("Error: the branch of PR #7 in acme/app cannot take 'main'")
    assert "merge_github_branch (branch 'feat/x', from_ref 'main') with 'resolutions'" in result
    assert "src/frictions.py: 1 overlapping hunk" in result
    assert not fake.merged and fake.commits == []


@pytest.mark.asyncio
async def test_keep_both_on_a_binary_is_refused_before_any_write():
    base = {"logo.png": "\x00base"}
    fake = FakeGitHub(base, {"logo.png": "\x00ours"}, {"logo.png": "\x00theirs"})
    with patch(_SAFE_REQUEST, new=fake):
        refused = await _tool("merge_github_branch")(repo="acme/app", branch="feat/x")
        both = await _tool("merge_github_branch")(
            repo="acme/app", branch="feat/x", resolutions=[{"path": "logo.png", "keep": "both"}]
        )

    assert "logo.png: a binary file changed on both sides" in refused
    assert both.startswith("Error: keep 'both' cannot settle 'logo.png'") and fake.commits == []


_MOVED = (
    "Error: Merging 'main' into 'feat/x' of acme/app conflicts: 'main' now points at 333333333333, not at "
    "222222222222 the resolutions were checked against. Nothing was written."
)


def test_a_ref_that_moved_with_nothing_left_on_the_branch_is_retryable() -> None:
    from app.integrations.providers.github.merge_resolution import ref_moved_and_undone

    back = (
        "'feat/x' was moved back to 111111111111, its head before this call, so the alignment commits left its history."
    )
    undone = _MOVED.replace("Nothing was written.", back)
    assert ref_moved_and_undone(_MOVED) and ref_moved_and_undone(undone)
    assert not ref_moved_and_undone(_MOVED.replace("Nothing was written.", "The undo failed."))
    assert not ref_moved_and_undone("Error: merging 'main' into 'feat/x' of acme/app conflicts, so nothing was merged.")


@pytest.mark.asyncio
async def test_update_branch_runs_the_line_merge_again_when_the_base_moved_meanwhile() -> None:
    # xtm-one #405: main moved between the conflict check and the merge; one more pass lands it.
    fake = _fixture(9, "main 9\n")
    answers = AsyncMock(side_effect=[_MOVED, "Merged 'main' into 'feat/x' of acme/app with its conflicts resolved"])
    with patch(_SAFE_REQUEST, new=fake), patch("app.integrations.providers.github.pulls.conflict_answer", new=answers):
        result = await _tool("manage_github_pr")(repo="acme/app", number=7, action="update_branch")

    assert answers.await_count == 2
    assert result.startswith("GitHub refused to update the branch of PR #7 on a conflict; merged line by line"), result


@pytest.mark.asyncio
async def test_update_branch_retries_a_moved_base_once_only() -> None:
    fake = _fixture(9, "main 9\n")
    answers = AsyncMock(side_effect=[_MOVED, _MOVED, _MOVED])
    with patch(_SAFE_REQUEST, new=fake), patch("app.integrations.providers.github.pulls.conflict_answer", new=answers):
        result = await _tool("manage_github_pr")(repo="acme/app", number=7, action="update_branch")

    assert answers.await_count == 2 and "now points at 333333333333" in result


# ---------------------------------------------------------------------------
# The read budget is per path, not one total (#9040)
# ---------------------------------------------------------------------------


def _large(lines: int, tag: str) -> str:
    return "".join(f"{tag} key {n:06}: {'x' * 90}\n" for n in range(lines))


@pytest.mark.asyncio
async def test_paths_over_the_old_total_budget_merge_one_by_one():
    # Five ~1.1 MB files changed on both sides: their three versions add up to ~16.5 MB, over the 15 MB the
    # line merge once read in all (the four i18n catalogs and types/index.ts on xtm-one), each well under a path's budget.
    paths = [f"frontend/src/i18n/{lang}.json" for lang in ("en", "fr", "de", "es", "ja")]
    big = _large(11_000, "i18n")
    base = dict.fromkeys(paths, big)
    ours = {path: big.replace("i18n key 000100:", "branch key 000100:") for path in paths}
    theirs = {path: big.replace("i18n key 010900:", "main key 010900:") for path in paths}
    fake = FakeGitHub(base, ours, theirs)
    total = sum(len(side[path].encode()) for side in (base, ours, theirs) for path in paths)
    assert total > merge_hunks.MAX_MERGE_READ_BYTES

    with patch(_SAFE_REQUEST, new=fake):
        merged = await line_merge(None, {}, "acme/app", API, BASE, "feat/x", "main", paths)

    assert not isinstance(merged, str), merged
    assert merged.clean == sorted(paths) and merged.conflicting == []
    expected = ours[paths[0]].replace("i18n key 010900:", "main key 010900:")
    assert all(merged.merges[path].merged == expected for path in paths)


@pytest.mark.asyncio
async def test_a_path_over_its_budget_is_a_conflict_while_the_others_merge(monkeypatch):
    monkeypatch.setattr(merge_hunks, "MAX_MERGE_READ_BYTES", 3_000)
    big = _large(20, "big")
    base = {"big.txt": big, "a.py": BASE_FILE}
    ours = {"big.txt": big.replace("big key 000001:", "branch key 000001:"), "a.py": _edit(BASE_FILE, 2, "branch 2\n")}
    theirs = {"big.txt": big.replace("big key 000019:", "main key 000019:"), "a.py": _edit(BASE_FILE, 9, "main 9\n")}
    fake = FakeGitHub(base, ours, theirs)
    with patch(_SAFE_REQUEST, new=fake):
        refused = await _tool("merge_github_branch")(repo="acme/app", branch="feat/x")

    assert "1 hold overlapping hunks" in refused, refused
    assert "- big.txt: its three versions add up to" in refused and "over the 3000 the line merge reads for one path" in refused
    assert "Merged without an overlap (1):\n- a.py" in refused
    assert not fake.merged and fake.commits == []
    blob_reads = [url for verb, url, _ in fake.calls if verb == "get" and "/git/blobs/" in url]
    assert _sha(big) not in " ".join(blob_reads)  # a path over its budget is never read


@pytest.mark.asyncio
async def test_reads_past_the_total_guard_fall_back_to_the_list_of_paths(monkeypatch):
    monkeypatch.setattr(merge_hunks, "MAX_LINE_MERGE_TOTAL_BYTES", 200)
    base = {"a.py": BASE_FILE, "b.py": BASE_FILE}
    ours = {"a.py": _edit(BASE_FILE, 2, "branch 2\n"), "b.py": _edit(BASE_FILE, 2, "branch 2\n")}
    theirs = {"a.py": _edit(BASE_FILE, 9, "main 9\n"), "b.py": _edit(BASE_FILE, 9, "main 9\n")}
    fake = FakeGitHub(base, ours, theirs)
    with patch(_SAFE_REQUEST, new=fake):
        merged = await line_merge(None, {}, "acme/app", API, BASE, "feat/x", "main", ["a.py", "b.py"])

    assert merged == "the versions of the paths are over the 200 bytes one line merge reads"


@pytest.mark.asyncio
async def test_a_path_past_the_time_budget_is_a_conflict_and_the_paths_before_it_merge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # #51: a slow merge_path (difflib on a large file) must never hold the call past its budget.
    monkeypatch.setattr(merge_hunks, "LINE_MERGE_SECONDS", 0.2)
    real = merge_hunks.merge_path

    def slow_on_b(
        path: str, base: bytes | None, ours: bytes | None, theirs: bytes | None, *, ref_first: bool = False
    ) -> merge_hunks.PathMerge:
        if path == "b.py":
            time.sleep(1.0)
        return real(path, base, ours, theirs, ref_first=ref_first)

    monkeypatch.setattr(merge_hunks, "merge_path", slow_on_b)
    paths = ["a.py", "b.py", "c.py"]
    base = dict.fromkeys(paths, BASE_FILE)
    ours = dict.fromkeys(paths, _edit(BASE_FILE, 2, "branch 2\n"))
    theirs = dict.fromkeys(paths, _edit(BASE_FILE, 9, "main 9\n"))
    fake = FakeGitHub(base, ours, theirs)
    started = time.monotonic()
    with patch(_SAFE_REQUEST, new=fake):
        merged = await line_merge(None, {}, "acme/app", API, BASE, "feat/x", "main", paths)

    assert time.monotonic() - started < 0.9
    assert not isinstance(merged, str), merged
    assert merged.clean == ["a.py"]
    assert merged.conflicting == ["b.py", "c.py"]
    assert all("time budget" in merged.merges[path].reason for path in ("b.py", "c.py"))


@pytest.mark.asyncio
async def test_a_spent_time_budget_reads_no_more_blobs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(merge_hunks, "LINE_MERGE_SECONDS", 0.0)
    base = {"a.py": BASE_FILE}
    ours = {"a.py": _edit(BASE_FILE, 2, "branch 2\n")}
    theirs = {"a.py": _edit(BASE_FILE, 9, "main 9\n")}
    fake = FakeGitHub(base, ours, theirs)
    with patch(_SAFE_REQUEST, new=fake):
        merged = await line_merge(None, {}, "acme/app", API, BASE, "feat/x", "main", ["a.py"])

    assert not isinstance(merged, str), merged
    assert merged.conflicting == ["a.py"] and "time budget" in merged.merges["a.py"].reason
    assert not [url for verb, url, _ in fake.calls if verb == "get" and "/git/blobs/" in url]


# ---------------------------------------------------------------------------
# A keep resolution settles the overlapping hunks only (#11987)
# ---------------------------------------------------------------------------


def test_prefer_takes_one_side_in_the_overlap_and_keeps_every_other_hunk() -> None:
    base = BASE_FILE
    ours = _edit(_edit(base, 2, "branch 2\n"), 5, "branch 5\n")
    theirs = _edit(_edit(base, 5, "main 5\n"), 9, "main 9\n")
    kept_theirs = merge_text(base, ours, theirs, prefer="theirs")
    kept_ours = merge_text(base, ours, theirs, prefer="ours")
    assert kept_theirs.conflicts == 1 and "<<<<<<<" not in kept_theirs.text
    assert kept_theirs.text == _edit(_edit(_edit(base, 2, "branch 2\n"), 5, "main 5\n"), 9, "main 9\n")
    assert kept_ours.text == _edit(_edit(_edit(base, 2, "branch 2\n"), 5, "branch 5\n"), 9, "main 9\n")


def test_merge_path_carries_both_per_hunk_keeps() -> None:
    ours = _edit(_edit(BASE_FILE, 2, "branch 2\n"), 5, "branch 5\n")
    theirs = _edit(BASE_FILE, 5, "main 5\n")
    merge = merge_path("a.py", BASE_FILE.encode(), ours.encode(), theirs.encode())
    assert not merge.clean
    assert merge.keep_theirs == _edit(_edit(BASE_FILE, 2, "branch 2\n"), 5, "main 5\n")
    assert merge.keep_ours == ours


@pytest.mark.asyncio
@pytest.mark.parametrize(("keep", "line_5"), [("ref", "main 5\n"), ("branch", "branch 5\n")])
async def test_a_keep_resolution_keeps_the_other_sides_non_overlapping_hunk(keep: str, line_5: str) -> None:
    # #11907: keep 'ref' on a file with one overlapping hunk wrote main's whole file and dropped the branch's
    # non-overlapping change at line 2; keep 'branch' likewise dropped main's change at line 9.
    base = {"budget.py": BASE_FILE}
    ours = {"budget.py": _edit(_edit(BASE_FILE, 2, "branch 2\n"), 5, "branch 5\n")}
    theirs = {"budget.py": _edit(_edit(BASE_FILE, 5, "main 5\n"), 9, "main 9\n")}
    fake = FakeGitHub(base, ours, theirs)
    with patch(_SAFE_REQUEST, new=fake):
        landed = await _tool("merge_github_branch")(
            repo="acme/app", branch="feat/x", resolutions=[{"path": "budget.py", "keep": keep}]
        )

    assert landed.startswith("Merged 'main' into 'feat/x'"), landed
    assert fake.sides["ours"]["budget.py"] == _edit(_edit(_edit(BASE_FILE, 2, "branch 2\n"), 5, line_5), 9, "main 9\n")
    side = "main" if keep == "ref" else "feat/x"
    assert f"budget.py: 1 overlapping hunk taken from '{side}' ('feat/x' line 5 / 'main' line 5)" in landed
    assert "every non-overlapping change of both sides kept" in landed


@pytest.mark.asyncio
async def test_a_keep_resolution_answers_the_head_the_branch_ends_on() -> None:
    base = {"budget.py": BASE_FILE}
    ours = {"budget.py": _edit(BASE_FILE, 5, "branch 5\n")}
    theirs = {"budget.py": _edit(BASE_FILE, 5, "main 5\n")}
    fake = FakeGitHub(base, ours, theirs)
    with patch(_SAFE_REQUEST, new=fake):
        landed = await _tool("merge_github_branch")(
            repo="acme/app", branch="feat/x", resolutions=[{"path": "budget.py", "keep": "branch"}]
        )

    assert landed.splitlines()[-1].startswith(f"Final head: {fake.tip} "), landed


@pytest.mark.asyncio
async def test_a_keep_on_a_binary_still_takes_the_whole_side_and_says_so() -> None:
    base = {"logo.png": "\x00base"}
    fake = FakeGitHub(base, {"logo.png": "\x00ours"}, {"logo.png": "\x00theirs"})
    with patch(_SAFE_REQUEST, new=fake):
        landed = await _tool("merge_github_branch")(
            repo="acme/app", branch="feat/x", resolutions=[{"path": "logo.png", "keep": "branch"}]
        )

    assert landed.startswith("Merged"), landed
    assert fake.sides["ours"]["logo.png"] == "\x00ours"
    assert "- logo.png: 'feat/x' version kept whole (a binary file changed on both sides" in landed, landed
