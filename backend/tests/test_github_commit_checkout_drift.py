"""create_github_commit(from_checkout) on a branch that moved since the checkout.

The checkout record names the commit the tree was read at. When the branch
tip is another commit that changed a file the workspace also changed, the
file is merged three-way (recorded, workspace, tip) on the server instead of
being written whole over the tip; an overlap refuses the commit.
"""

import base64
import hashlib
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pytest

from app.integrations.providers.github import contents
from tests.test_github_commit import (
    _SAFE_REQUEST,
    API,
    BASE_TREE,
    GRAPHQL,
    NEW_COMMIT,
    TIP,
    _commit_tool,
    _workspace_file_context,
)

#: The commit the checkout was read at; the branch has moved on to TIP since.
RECORDED = "9" * 40
RECORDED_TREE = "8" * 40
SRC_TREE = "f" * 40
#: The merge base of RECORDED and TIP when the record sits on another branch than the tip.
FORK = "7" * 40
FORK_TREE = "6" * 40

BASE_TEXT = b"one\ntwo\nthree\nfour\nfive\n"
#: The branch's change since the checkout: its last line.
BRANCH_TEXT = b"one\ntwo\nthree\nfour\nFIVE (branch)\n"


def _sha(data: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(data) + data, usedforsecurity=False).hexdigest()


class _DriftSink:
    """One checkout of acme/app read at RECORDED, with the workspace as given."""

    def __init__(self, workspace: dict[str, bytes | None], files: dict[str, bytes] | None = None) -> None:
        from app.services.sandbox_workspace.checkout import build_record

        self.record = build_record(
            provider="github",
            repository="acme/app",
            ref="feat/x",
            commit=RECORDED,
            path="app",
            sub_paths=("src",),
            files=list((files or {"src/app.py": BASE_TEXT}).items()),
        )
        self.workspace = workspace
        self.advanced: list[dict] = []

    async def checkout_changes(self, directory, paths):
        from app.services.sandbox_workspace.checkout import changes_since

        entries = {
            self.record.workspace_path(p): SimpleNamespace(sha256=hashlib.sha256(d).hexdigest())
            for p, d in self.workspace.items()
            if d is not None
        }
        return self.record, changes_since(self.record, entries, paths=paths)

    async def read_file(self, path, *, tool):
        repo_path = str(path).removeprefix("app/")
        return path, repo_path.rsplit("/", 1)[-1], self.workspace[repo_path]

    async def advance_checkout(self, record, *, commit, ref, written, removed, rewrites=None):
        self.advanced.append({"commit": commit, "written": written, "removed": removed, "rewrites": rewrites or {}})
        return f"Checkout record moved to {commit[:8]}."

    async def checkout_records_for(self, workspace_paths):
        return {path: self.record for path in workspace_paths if path.startswith("app/")}


class _MovedBranch:
    """GitHub with branch feat/x at TIP, whose src/app.py is BRANCH_TEXT; the checkout read RECORDED."""

    def __init__(self, recorded: dict[str, bytes], tip: dict[str, bytes], fork: dict[str, bytes] | None = None) -> None:
        self.sides = {RECORDED: (RECORDED_TREE, recorded), TIP: (BASE_TREE, tip)}
        #: The merge base of RECORDED and TIP GitHub names: RECORDED, or FORK when the record is on another line.
        self.merge_base = RECORDED
        if fork is not None:
            self.sides[FORK] = (FORK_TREE, fork)
            self.merge_base = FORK
        self.blobs = {_sha(data): data for side in self.sides.values() for data in side[1].values()}
        self.commits: list[dict] = []
        self.calls: list[tuple[str, str]] = []

    async def __call__(self, client, method, url, provider, action, **kwargs):
        self.calls.append((method, url))

        def ok(payload: dict):
            return httpx.Response(200, json=payload), None

        if method == "get" and url.startswith(f"{API}/git/ref/heads/"):
            return ok({"object": {"sha": TIP, "type": "commit"}})
        if method == "get" and url.startswith(f"{API}/compare/"):
            return ok({"merge_base_commit": {"sha": self.merge_base}, "status": "diverged"})
        if method == "get" and url.startswith(f"{API}/commits/"):
            ref = url.rsplit("/", 1)[1]
            return ok({"sha": ref, "commit": {"tree": {"sha": self.sides[ref][0]}}})
        if method == "get" and url.startswith(f"{API}/git/trees/") and kwargs.get("params", {}).get("recursive"):
            tree = url.rsplit("/", 1)[1]
            files = next(side for sha, side in self.sides.values() if sha == tree)
            listing = [
                {"path": p, "mode": "100644", "type": "blob", "sha": _sha(d), "size": len(d)} for p, d in files.items()
            ]
            return ok({"sha": tree, "tree": listing, "truncated": False})
        if method == "get" and url.startswith(f"{API}/git/blobs/"):
            sha = url.rsplit("/", 1)[1]
            return ok({"sha": sha, "encoding": "base64", "content": base64.b64encode(self.blobs[sha]).decode()})
        if method == "get" and url == f"{API}/git/commits/{TIP}":
            return ok({"sha": TIP, "tree": {"sha": BASE_TREE}})
        if method == "get" and url.startswith(f"{API}/git/trees/"):
            tree = url.rsplit("/", 1)[1]
            if tree == BASE_TREE:
                return ok({"sha": tree, "tree": [{"path": "src", "mode": "040000", "type": "tree", "sha": SRC_TREE}]})
            names = self.sides[TIP][1]
            listing = [
                {"path": p.split("/", 1)[1], "mode": "100644", "type": "blob", "sha": _sha(d)} for p, d in names.items()
            ]
            return ok({"sha": tree, "tree": listing, "truncated": False})
        if method == "post" and url == f"{API}/git/blobs":
            return ok({"sha": _sha(base64.b64decode(kwargs["json"]["content"]))})
        if method == "post" and url == f"{API}/git/trees":
            # GitHub answers the tip's own tree when every entry written is what the tip holds.
            tip_files = self.sides[TIP][1]
            posted = kwargs.get("json", {}).get("tree", [])
            same = bool(posted) and all(
                item.get("path") in tip_files
                and (
                    item.get("sha") == _sha(tip_files[item["path"]])
                    or (isinstance(item.get("content"), str) and item["content"].encode() == tip_files[item["path"]])
                )
                for item in posted
            )
            return ok({"sha": BASE_TREE if same else "c" * 40})
        if method == "post" and url == GRAPHQL:
            self.commits.append(kwargs["json"]["variables"]["input"])
            made = {"oid": NEW_COMMIT, "signature": {"isValid": True, "state": "VALID"}}
            return ok({"data": {"createCommitOnBranch": {"commit": made}}})
        raise AssertionError(f"unexpected {method.upper()} {url}")


def _committed(fake: _MovedBranch) -> dict[str, bytes]:
    [commit_input] = fake.commits
    return {
        item["path"]: base64.b64decode(item["contents"]) for item in commit_input["fileChanges"].get("additions", [])
    }


@pytest.mark.asyncio
async def test_a_branch_moved_by_another_commit_on_the_same_file_keeps_both_changes():
    commit, _ = _commit_tool()
    # The workspace changed the first line; the branch changed the last one since the checkout.
    sink = _DriftSink({"src/app.py": b"ONE (workspace)\ntwo\nthree\nfour\nfive\n"})
    fake = _MovedBranch({"src/app.py": BASE_TEXT}, {"src/app.py": BRANCH_TEXT})
    with patch(_SAFE_REQUEST, new=fake), patch.object(contents, "current_workspace", return_value=sink):
        result = await commit(repo="acme/app", message="feat(app): x", from_checkout="app", branch="feat/x")

    both = b"ONE (workspace)\ntwo\nthree\nfour\nFIVE (branch)\n"
    assert result.startswith("Committed 1 file change(s)"), result
    assert _committed(fake) == {"src/app.py": both}
    assert "Merged with the branch" in result and "src/app.py" in result
    [advanced] = sink.advanced
    assert advanced["written"] == {"src/app.py": both}
    # The workspace is brought to the merged text with the record.
    assert advanced["rewrites"] == {"src/app.py": both}


@pytest.mark.asyncio
async def test_an_overlap_with_the_branch_refuses_the_commit_and_names_the_hunk():
    commit, _ = _commit_tool()
    sink = _DriftSink({"src/app.py": b"one\ntwo\nthree\nfour\nfive (workspace)\n"})
    fake = _MovedBranch({"src/app.py": BASE_TEXT}, {"src/app.py": BRANCH_TEXT})
    with patch(_SAFE_REQUEST, new=fake), patch.object(contents, "current_workspace", return_value=sink):
        result = await commit(repo="acme/app", message="feat(app): x", from_checkout="app", branch="feat/x")

    assert result.startswith("Error: branch 'feat/x' of acme/app is at aaaaaaaa, not at the checkout's commit 99999999")
    assert "nothing was committed" in result
    assert 'sandbox_checkout_rebase(checkout="app", ref="feat/x")' in result
    assert "src/app.py: 1 overlapping hunk" in result
    assert "FIVE (branch)" in result and "five (workspace)" in result
    assert fake.commits == []
    assert sink.advanced == []


@pytest.mark.asyncio
async def test_a_file_the_branch_did_not_change_is_written_as_the_workspace_has_it():
    commit, _ = _commit_tool()
    other = b"untouched\n"
    sink = _DriftSink(
        {"src/app.py": BASE_TEXT, "src/other.py": b"edited\n"},
        files={"src/app.py": BASE_TEXT, "src/other.py": other},
    )
    fake = _MovedBranch(
        {"src/app.py": BASE_TEXT, "src/other.py": other}, {"src/app.py": BRANCH_TEXT, "src/other.py": other}
    )
    with patch(_SAFE_REQUEST, new=fake), patch.object(contents, "current_workspace", return_value=sink):
        result = await commit(repo="acme/app", message="feat(app): x", from_checkout="app", branch="feat/x")

    # src/app.py is not a change of the workspace, so the branch's version of it is never touched.
    assert result.startswith("Committed 1 file change(s)"), result
    assert _committed(fake) == {"src/other.py": b"edited\n"}
    assert "Merged with the branch" not in result
    [advanced] = sink.advanced
    assert advanced["rewrites"] == {}
    # The branch changed src/app.py, which the workspace holds at RECORDED: the record keeps its
    # commit, so the next commit from this checkout merges src/app.py instead of writing over it.
    assert advanced["commit"] == ""
    assert "Checkout behind the branch" in result and "(src/app.py)" in result
    assert 'sandbox_checkout_rebase(checkout="app", ref="feat/x")' in result


@pytest.mark.asyncio
async def test_a_second_commit_after_a_stale_checkout_keeps_the_branch_lines_of_a_file_the_first_did_not_carry():
    """#10061: checkout at A, branch cut at B (B changed F), commit 1 carries G, commit 2 carries F."""
    from app.services.sandbox_workspace.checkout import advance

    commit, _ = _commit_tool()
    other = b"untouched\n"
    sink = _DriftSink(
        {"src/app.py": BASE_TEXT, "src/other.py": b"edited\n"},
        files={"src/app.py": BASE_TEXT, "src/other.py": other},
    )
    fake = _MovedBranch(
        {"src/app.py": BASE_TEXT, "src/other.py": other}, {"src/app.py": BRANCH_TEXT, "src/other.py": other}
    )
    with patch(_SAFE_REQUEST, new=fake), patch.object(contents, "current_workspace", return_value=sink):
        first = await commit(repo="acme/app", message="feat(app): x", from_checkout="app", branch="feat/x")
    assert first.startswith("Committed 1 file change(s)"), first
    [step] = sink.advanced
    # What the workspace sink does with the record: only the written hash moves, the commit stays.
    sink.record = advance(sink.record, commit=step["commit"], ref="feat/x", written=step["written"], removed=[])
    assert sink.record.commit == RECORDED

    # The second commit edits the first line of src/app.py, whose last line the branch changed.
    sink.workspace = {"src/app.py": b"ONE (workspace)\ntwo\nthree\nfour\nfive\n", "src/other.py": b"edited\n"}
    fake.sides[TIP] = (BASE_TREE, {"src/app.py": BRANCH_TEXT, "src/other.py": b"edited\n"})
    fake.blobs[_sha(b"edited\n")] = b"edited\n"
    fake.commits.clear()
    with patch(_SAFE_REQUEST, new=fake), patch.object(contents, "current_workspace", return_value=sink):
        second = await commit(repo="acme/app", message="feat(app): y", from_checkout="app", branch="feat/x")

    assert second.startswith("Committed 1 file change(s)"), second
    # The branch's last line is kept: no silent revert of what the branch gained before the checkout caught up.
    assert _committed(fake) == {"src/app.py": b"ONE (workspace)\ntwo\nthree\nfour\nFIVE (branch)\n"}
    assert "Merged with the branch" in second


@pytest.mark.asyncio
async def test_a_deletion_the_branch_already_made_is_dropped():
    commit, _ = _commit_tool()
    sink = _DriftSink(
        {"src/app.py": b"ONE\ntwo\nthree\nfour\nfive\n", "src/old.py": None},
        files={"src/app.py": BASE_TEXT, "src/old.py": b"gone\n"},
    )
    fake = _MovedBranch({"src/app.py": BASE_TEXT, "src/old.py": b"gone\n"}, {"src/app.py": BASE_TEXT})
    with patch(_SAFE_REQUEST, new=fake), patch.object(contents, "current_workspace", return_value=sink):
        result = await commit(repo="acme/app", message="feat(app): x", from_checkout="app", branch="feat/x")

    assert result.startswith("Committed 1 file change(s)"), result
    assert "deletions" not in fake.commits[0]["fileChanges"]
    assert "Not deleted, the branch already removed them: src/old.py." in result


@pytest.mark.asyncio
async def test_a_branch_at_the_recorded_commit_reads_nothing_more():
    commit, _ = _commit_tool()

    class _AtRecord(_MovedBranch):
        async def __call__(self, client, method, url, provider, action, **kwargs):
            assert not url.startswith(f"{API}/commits/"), "no comparison when the tip is the recorded commit"
            return await super().__call__(client, method, url, provider, action, **kwargs)

    sink = _DriftSink({"src/app.py": b"ONE\ntwo\nthree\nfour\nfive\n"})
    sink.record = sink.record.__class__(**{**sink.record.__dict__, "commit": TIP})
    fake = _AtRecord({"src/app.py": BASE_TEXT}, {"src/app.py": BASE_TEXT})
    with patch(_SAFE_REQUEST, new=fake), patch.object(contents, "current_workspace", return_value=sink):
        result = await commit(repo="acme/app", message="feat(app): x", from_checkout="app", branch="feat/x")

    assert result.startswith("Committed 1 file change(s)"), result
    assert _committed(fake) == {"src/app.py": b"ONE\ntwo\nthree\nfour\nfive\n"}


# --- files[].workspace_path of a checkout file on a branch that moved since the checkout (#11852) ---


async def _files_commit(sink: _DriftSink, fake: _MovedBranch, data: bytes):
    commit, _ = _commit_tool()
    with patch(_SAFE_REQUEST, new=fake), patch.object(contents, "current_workspace", return_value=sink):
        return await commit(
            repo="acme/app",
            message="fix(app): x",
            files=[{"path": "src/app.py", "file_id": "w1"}],
            branch="feat/x",
            file_context=_workspace_file_context("w1", "app/src/app.py", data),
        )


@pytest.mark.asyncio
async def test_a_workspace_path_write_of_a_stale_checkout_file_is_merged_never_reverting_the_branch():
    edited = b"ONE (workspace)\ntwo\nthree\nfour\nfive\n"
    sink = _DriftSink({"src/app.py": edited})
    fake = _MovedBranch({"src/app.py": BASE_TEXT}, {"src/app.py": BRANCH_TEXT})

    result = await _files_commit(sink, fake, edited)

    assert result.startswith("Committed 1 file change(s)"), result
    # The branch's last line is kept: the checkout's stale text is never written over it.
    assert _committed(fake) == {"src/app.py": b"ONE (workspace)\ntwo\nthree\nfour\nFIVE (branch)\n"}
    assert "Merged with the branch" in result and "src/app.py" in result
    assert 'sandbox_checkout_rebase(checkout="app", ref="feat/x")' in result
    # A files[] commit never rewrites the workspace nor moves the from_checkout record.
    assert sink.advanced == []


@pytest.mark.asyncio
async def test_a_workspace_path_write_overlapping_the_branch_is_refused_with_the_file_named():
    edited = b"one\ntwo\nthree\nfour\nfive (workspace)\n"
    sink = _DriftSink({"src/app.py": edited})
    fake = _MovedBranch({"src/app.py": BASE_TEXT}, {"src/app.py": BRANCH_TEXT})

    result = await _files_commit(sink, fake, edited)

    assert result.startswith("Error: branch 'feat/x' of acme/app is at aaaaaaaa, not at the checkout's commit 99999999")
    assert "nothing was committed" in result
    assert "src/app.py: 1 overlapping hunk" in result
    assert fake.commits == []


@pytest.mark.asyncio
async def test_a_workspace_path_write_on_a_branch_that_did_not_change_the_file_is_written_as_is():
    edited = b"ONE (workspace)\ntwo\nthree\nfour\nfive\n"
    sink = _DriftSink({"src/app.py": edited})
    other = b"other\n"
    fake = _MovedBranch({"src/app.py": BASE_TEXT}, {"src/app.py": BASE_TEXT, "src/new.py": other})

    result = await _files_commit(sink, fake, edited)

    assert result.startswith("Committed 1 file change(s)"), result
    assert _committed(fake) == {"src/app.py": edited}
    assert "Merged with the branch" not in result


# --- the merge ancestor is the merge base, and a record already on the tip's version writes as is ---


@pytest.mark.asyncio
async def test_a_new_branch_cut_from_main_after_a_commit_on_another_branch_commits_the_changed_files():
    """The record moved to RECORDED by a commit on another branch; feat/x is cut from main at TIP.

    RECORDED is not an ancestor of TIP: their merge base FORK is. With RECORDED as the ancestor the
    workspace text looked like the record's and the merge lost or refused the workspace's changes
    ("No change" over real changes). With FORK it carries the workspace's edit and the branch's.
    """
    commit, _ = _commit_tool()
    # The other branch's commit changed the first line: the record holds that.
    on_record = b"ONE (other branch)\ntwo\nthree\nfour\nfive\n"
    sink = _DriftSink(
        {"src/app.py": b"ONE (other branch)\ntwo (workspace)\nthree\nfour\nfive\n"}, files={"src/app.py": on_record}
    )
    # main changed the last line since the fork.
    fake = _MovedBranch({"src/app.py": on_record}, {"src/app.py": BRANCH_TEXT}, fork={"src/app.py": BASE_TEXT})
    with patch(_SAFE_REQUEST, new=fake), patch.object(contents, "current_workspace", return_value=sink):
        result = await commit(repo="acme/app", message="feat(app): x", from_checkout="app", branch="feat/x")

    assert result.startswith("Committed 1 file change(s)"), result
    assert "No change" not in result
    assert _committed(fake) == {"src/app.py": b"ONE (other branch)\ntwo (workspace)\nthree\nfour\nFIVE (branch)\n"}


@pytest.mark.asyncio
async def test_identical_files_on_the_target_branch_still_answer_no_change():
    commit, _ = _commit_tool()
    on_record = b"ONE (other branch)\ntwo\nthree\nfour\nfive\n"
    # The workspace holds exactly the tip's bytes: nothing to commit, and no blob read to find it out.
    sink = _DriftSink({"src/app.py": BRANCH_TEXT}, files={"src/app.py": on_record})
    fake = _MovedBranch({"src/app.py": on_record}, {"src/app.py": BRANCH_TEXT}, fork={"src/app.py": BASE_TEXT})
    with patch(_SAFE_REQUEST, new=fake), patch.object(contents, "current_workspace", return_value=sink):
        result = await commit(repo="acme/app", message="feat(app): x", from_checkout="app", branch="feat/x")

    assert "No change" in str(getattr(result, "content", result)), result
    assert fake.commits == []
    assert not any("/git/blobs/" in url for _method, url in fake.calls)


@pytest.mark.asyncio
async def test_two_consecutive_from_checkout_commits_on_one_branch_both_land():
    """Commit 1 writes the file on feat/x and the record holds its hash; commit 2 edits the same line again."""
    from app.services.sandbox_workspace.checkout import advance

    commit, _ = _commit_tool()
    first_text = b"one\ntwo\nthree\nfour\nfive (first)\n"
    sink = _DriftSink({"src/app.py": first_text})
    sink.record = sink.record.__class__(**{**sink.record.__dict__, "commit": TIP})
    fake = _MovedBranch({"src/app.py": BASE_TEXT}, {"src/app.py": BASE_TEXT})
    with patch(_SAFE_REQUEST, new=fake), patch.object(contents, "current_workspace", return_value=sink):
        first = await commit(repo="acme/app", message="feat(app): x", from_checkout="app", branch="feat/x")
    assert first.startswith("Committed 1 file change(s)"), first
    [step] = sink.advanced
    # The record is left on a commit the branch has moved past (a stale record: a kept commit, a move that
    # failed) while it holds the written file's hash, as advance() leaves it.
    sink.record = advance(sink.record, commit=RECORDED, ref="feat/x", written=step["written"], removed=[])

    # The branch holds the first commit's text; the workspace changes the same last line again.
    second_text = b"one\ntwo\nthree\nfour\nfive (second)\n"
    sink.workspace = {"src/app.py": second_text}
    fake.sides[TIP] = (BASE_TREE, {"src/app.py": first_text})
    fake.blobs[_sha(first_text)] = first_text
    fake.commits.clear()
    with patch(_SAFE_REQUEST, new=fake), patch.object(contents, "current_workspace", return_value=sink):
        second = await commit(repo="acme/app", message="feat(app): y", from_checkout="app", branch="feat/x")

    # The record holds the tip's version, so the branch's own first commit is no overlap.
    assert second.startswith("Committed 1 file change(s)"), second
    assert _committed(fake) == {"src/app.py": second_text}
    assert "Merged with the branch" not in second


# --- #1040: a record moved by a commit to a tip whose other files the workspace never held ---


def _moved_record_sink(workspace: dict[str, bytes | None], new_file: bytes) -> _DriftSink:
    """Checkout read at FORK; a first commit from it wrote src/new.py on feat/x and moved the record to TIP."""
    import dataclasses

    from app.services.sandbox_workspace.checkout import RecordedFile

    sink = _DriftSink(workspace)
    files = dict(sink.record.files)
    files["src/new.py"] = RecordedFile(sha256=hashlib.sha256(new_file).hexdigest(), size=len(new_file))
    sink.record = dataclasses.replace(
        sink.record, commit=TIP, base_commit=FORK, branch={"src/new.py": "A"}, files=files
    )
    return sink


@pytest.mark.asyncio
async def test_a_file_the_moved_record_never_wrote_is_merged_with_the_tip_not_written_over_it():
    """#1040: checkout at FORK, branch cut at TIP (TIP changed src/app.py), commit 1 wrote src/new.py only.

    The record moved to TIP, the tip itself: the workspace still holds src/app.py at FORK plus the edit,
    so writing it whole reverted what TIP added. It is merged with FORK as the ancestor.
    """
    commit, _ = _commit_tool()
    new_file = b"new\n"
    sink = _moved_record_sink(
        {"src/app.py": b"ONE (workspace)\ntwo\nthree\nfour\nfive\n", "src/new.py": new_file}, new_file
    )
    fake = _MovedBranch(
        {"src/app.py": BASE_TEXT}, {"src/app.py": BRANCH_TEXT, "src/new.py": new_file}, fork={"src/app.py": BASE_TEXT}
    )
    with patch(_SAFE_REQUEST, new=fake), patch.object(contents, "current_workspace", return_value=sink):
        result = await commit(repo="acme/app", message="feat(app): y", from_checkout="app", branch="feat/x")

    both = b"ONE (workspace)\ntwo\nthree\nfour\nFIVE (branch)\n"
    assert result.startswith("Committed 1 file change(s)"), result
    # The tip's last line is kept: no silent revert of what the branch held before the first commit.
    assert _committed(fake) == {"src/app.py": both}
    assert "Merged with the branch" in result and "src/app.py" in result
    [advanced] = sink.advanced
    assert advanced["rewrites"] == {"src/app.py": both}


@pytest.mark.asyncio
async def test_a_moved_record_writes_a_file_its_own_commit_wrote_as_the_workspace_has_it():
    commit, _ = _commit_tool()
    new_file = b"new\n"
    edited = b"new (second commit)\n"
    sink = _moved_record_sink({"src/app.py": BASE_TEXT, "src/new.py": edited}, new_file)
    fake = _MovedBranch(
        {"src/app.py": BASE_TEXT}, {"src/app.py": BASE_TEXT, "src/new.py": new_file}, fork={"src/app.py": BASE_TEXT}
    )
    with patch(_SAFE_REQUEST, new=fake), patch.object(contents, "current_workspace", return_value=sink):
        result = await commit(repo="acme/app", message="feat(app): z", from_checkout="app", branch="feat/x")

    assert result.startswith("Committed 1 file change(s)"), result
    assert _committed(fake) == {"src/new.py": edited}
    assert "Merged with the branch" not in result
    assert not any("/git/blobs/" in url for _method, url in fake.calls)
