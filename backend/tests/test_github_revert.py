"""``revert_github_commit``: one commit undoing another, by object id, conflicts named.

A fake Git Data API answers the reads of one revert and records every
request: the tests check what is read, the one tree built, the one commit
made and the one ref moved or created, and the refusals.
"""

from __future__ import annotations

import base64
from unittest.mock import MagicMock, patch

import httpx
import pytest

from app.integrations.providers.github import build_tools
from app.integrations.providers.github import revert as revert_module
from app.tools.base import ToolResult

_SAFE_REQUEST = "app.integrations.providers.github._common.safe_request"

API = "https://api.github.com/repos/acme/app"
GRAPHQL = "https://api.github.com/graphql"
REVERTED = "a" * 40
PARENT = "b" * 40
TIP = "c" * 40
REVERTED_TREE = "1" * 40
PARENT_TREE = "2" * 40
TIP_TREE = "3" * 40
NEW_TREE = "4" * 40
NEW_COMMIT = "5" * 40
BLOB = "6" * 40
UNSIGNED = {"verified": False, "reason": "unsigned", "signature": None, "payload": None, "verified_at": None}


def _entry(path: str, sha: str, mode: str = "100644", kind: str = "blob") -> dict:
    return {"path": path, "mode": mode, "type": kind, "sha": sha}


def _tree(*entries: dict) -> list[dict]:
    dirs = {e["path"].rsplit("/", 1)[0] for e in entries if "/" in e["path"]}
    return [*(_entry(d, "d" * 40, "040000", "tree") for d in sorted(dirs)), *entries]


#: The parent: four files. The reverted commit changed a.py, deleted old.py,
#: added new.py and made run.sh executable; kept.md is untouched.
PARENT_ENTRIES = _tree(
    _entry("src/a.py", "aa" * 20), _entry("src/old.py", "bb" * 20), _entry("kept.md", "cc" * 20),
    _entry("run.sh", "ee" * 20),
)
REVERTED_ENTRIES = _tree(
    _entry("src/a.py", "a1" * 20), _entry("src/new.py", "dd" * 20), _entry("kept.md", "cc" * 20),
    _entry("run.sh", "ee" * 20, "100755"),
)


class FakeGitHub:
    def __init__(
        self,
        *,
        tip_entries: list[dict] | None = None,
        tip: str = TIP,
        target_exists: bool = False,
        truncated: bool = False,
        parents: list[str] | None = None,
        create_status: int = 200,
        moved_tip: str | None = None,
        open_prs: list[dict] | None = None,
        signed_refusal: str | None = None,
    ) -> None:
        self.open_prs = open_prs or []
        self.signed_refusal = signed_refusal
        self.signed_inputs: list[dict] = []
        # The branch as it is now: the reverted commit's tree unless changed since.
        self.tip_entries = tip_entries if tip_entries is not None else REVERTED_ENTRIES
        self.tip = tip
        self.target_exists = target_exists
        self.truncated = truncated
        self.parents = parents if parents is not None else [PARENT]
        self.create_status = create_status
        self.moved_tip = moved_tip
        self.ref_reads = 0
        self.calls: list[tuple[str, str, dict]] = []

    def urls(self, method: str | None = None) -> list[str]:
        return [url for verb, url, _ in self.calls if method is None or verb == method]

    def payload(self, method: str, url: str) -> dict:
        for verb, called, kwargs in self.calls:
            if verb == method and called == url:
                return kwargs["json"]
        raise AssertionError(f"no {method.upper()} {url}")

    async def __call__(self, client, method, url, provider, action, **kwargs):
        self.calls.append((method, url, kwargs))

        def ok(payload: dict):
            return httpx.Response(200, json=payload), None

        def failed(status: int, message: str):
            resp = httpx.Response(status, json={"message": message})
            returned = resp if kwargs.get("expected_statuses") is not None else None
            return returned, f"Error: GitHub API error ({status}): {message}"

        if method == "get" and url == API:
            return ok({"default_branch": "main"})
        if method == "get" and url == f"{API}/git/commits/{REVERTED}":
            return ok({
                "sha": REVERTED, "tree": {"sha": REVERTED_TREE}, "message": "feat(chat): move to websockets\n\nbody",
                "parents": [{"sha": p} for p in self.parents],
            })
        if method == "get" and url == f"{API}/git/commits/{PARENT}":
            return ok({"sha": PARENT, "tree": {"sha": PARENT_TREE}, "parents": [{"sha": "0" * 40}]})
        if method == "get" and url == f"{API}/git/commits/{self.tip}":
            return ok({"sha": self.tip, "tree": {"sha": TIP_TREE}, "parents": [{"sha": "0" * 40}]})
        if method == "get" and url == f"{API}/git/ref/heads/main":
            self.ref_reads += 1
            tip = self.moved_tip if self.ref_reads > 1 and self.moved_tip else self.tip
            return ok({"ref": "refs/heads/main", "object": {"sha": tip, "type": "commit"}})
        if method == "get" and url.startswith(f"{API}/git/ref/heads/"):
            if self.target_exists:
                return ok({"ref": url.rsplit("/", 1)[1], "object": {"sha": "9" * 40, "type": "commit"}})
            return failed(404, "Not Found")
        if method == "get" and url.startswith(f"{API}/git/trees/"):
            sha = url.rsplit("/", 1)[1]
            assert kwargs.get("params") == {"recursive": "1"}
            entries = {REVERTED_TREE: REVERTED_ENTRIES, PARENT_TREE: PARENT_ENTRIES, TIP_TREE: self.tip_entries}[sha]
            return ok({"sha": sha, "tree": entries, "truncated": self.truncated})
        if method == "post" and url == f"{API}/git/blobs":
            return ok({"sha": BLOB})
        if method == "get" and url.startswith(f"{API}/git/blobs/"):
            oid = url.rsplit("/", 1)[1]
            return ok({"encoding": "base64", "content": base64.b64encode(f"blob {oid[:4]}\n".encode()).decode()})
        if method == "get" and url == f"{API}/pulls":
            assert kwargs["params"]["state"] == "open"
            return httpx.Response(200, json=self.open_prs), None
        if method == "post" and url == GRAPHQL:
            commit_input = kwargs["json"]["variables"]["input"]
            self.signed_inputs.append(commit_input)
            if self.signed_refusal:
                return ok({"errors": [{"message": self.signed_refusal}]})
            commit = {"oid": NEW_COMMIT, "signature": {"isValid": True, "state": "VALID"}}
            return ok({"data": {"createCommitOnBranch": {"commit": commit}}})
        if method == "post" and url == f"{API}/git/trees":
            return ok({"sha": NEW_TREE})
        if method == "post" and url == f"{API}/git/commits":
            made = {"sha": NEW_COMMIT, "tree": {"sha": NEW_TREE}, "parents": [{"sha": self.tip}]}
            return ok({**made, "verification": UNSIGNED})
        if method == "post" and url == f"{API}/git/refs":
            if self.create_status != 200:
                return failed(self.create_status, "Reference already exists")
            return ok({"ref": kwargs["json"]["ref"], "object": {"sha": NEW_COMMIT}})
        if method == "patch" and url.startswith(f"{API}/git/refs/heads/"):
            return ok({"ref": url.rsplit("/", 1)[1], "object": {"sha": NEW_COMMIT}})
        raise AssertionError(f"unexpected {method.upper()} {url}")


def _revert_tool():
    executor = MagicMock()
    executor.register = MagicMock()
    definitions = build_tools("fake-token", executor)
    handler = next(call[0][1] for call in executor.register.call_args_list if call[0][0] == "revert_github_commit")
    definition = next(d for d in definitions if d.name == "revert_github_commit")
    return handler, definition


@pytest.mark.asyncio
async def test_a_clean_revert_is_one_tree_one_commit_one_new_branch():
    revert, _ = _revert_tool()
    fake = FakeGitHub()
    with patch(_SAFE_REQUEST, new=fake):
        result = await revert(repo="acme/app", sha=REVERTED)

    assert isinstance(result, str), result
    assert result.startswith(
        f"Reverted commit {REVERTED[:8]} of acme/app on new branch 'revert-{REVERTED[:8]}' (from 'main') in one "
        f"commit ({NEW_COMMIT}): 3 restored, 1 deleted."
    )
    assert "Restored: run.sh, src/a.py, src/old.py" in result
    assert "Deleted: src/new.py" in result
    assert "Not signed:" in result and "Git Data API" in result and "GitHub reports it as unsigned" in result
    assert f"Open a pull request from 'revert-{REVERTED[:8]}' into 'main'" in result
    # No blob was downloaded or uploaded: the three trees are read recursively, one tree is built.
    assert fake.urls("post") == [f"{API}/git/trees", f"{API}/git/commits", f"{API}/git/refs"]
    assert f"{API}/git/blobs" not in fake.urls()
    tree = fake.payload("post", f"{API}/git/trees")
    assert tree["base_tree"] == TIP_TREE
    assert tree["tree"] == [
        {"path": "run.sh", "mode": "100644", "type": "blob", "sha": "ee" * 20},
        {"path": "src/a.py", "mode": "100644", "type": "blob", "sha": "aa" * 20},
        {"path": "src/new.py", "mode": "100644", "type": "blob", "sha": None},
        {"path": "src/old.py", "mode": "100644", "type": "blob", "sha": "bb" * 20},
    ]
    commit = fake.payload("post", f"{API}/git/commits")
    assert commit == {
        "message": f"revert: feat(chat): move to websockets\n\nThis reverts commit {REVERTED}.",
        "tree": NEW_TREE,
        "parents": [TIP],
    }
    assert fake.payload("post", f"{API}/git/refs") == {"ref": f"refs/heads/revert-{REVERTED[:8]}", "sha": NEW_COMMIT}


@pytest.mark.asyncio
async def test_a_revert_on_the_base_branch_moves_it_without_force():
    revert, _ = _revert_tool()
    fake = FakeGitHub()
    with patch(_SAFE_REQUEST, new=fake):
        result = await revert(
            repo="acme/app", sha=REVERTED, branch="main", base_branch="main", message="revert(chat): back to SSE"
        )

    assert "on branch 'main' in one commit" in result
    assert "Open a pull request" not in result
    assert fake.payload("post", f"{API}/git/commits")["message"] == "revert(chat): back to SSE"
    assert fake.payload("patch", f"{API}/git/refs/heads/main") == {"sha": NEW_COMMIT, "force": False}
    assert f"{API}/git/refs" not in fake.urls("post")


@pytest.mark.asyncio
async def test_a_path_changed_since_is_a_conflict_named_with_its_three_versions():
    revert, _ = _revert_tool()
    # src/a.py moved on after the reverted commit; src/old.py came back already.
    tip_entries = _tree(
        _entry("src/a.py", "a2" * 20), _entry("src/new.py", "dd" * 20), _entry("src/old.py", "bb" * 20),
        _entry("kept.md", "cc" * 20), _entry("run.sh", "ee" * 20, "100755"),
    )
    fake = FakeGitHub(tip_entries=tip_entries)
    with patch(_SAFE_REQUEST, new=fake):
        result = await revert(repo="acme/app", sha=REVERTED)

    assert result.startswith(
        "Error: 1 path(s) changed on branch 'main' of acme/app since commit aaaaaaaa, so the revert conflicts and "
        "nothing was created."
    )
    assert f"get_github_file at refs {PARENT[:8]} (parent), {REVERTED[:8]} (reverted) and main (branch)" in result
    assert "- src/a.py: parent aaaaaaaa (file), reverted a1a1a1a1 (file), branch a2a2a2a2 (file)" in result
    assert fake.urls("post") == []


@pytest.mark.asyncio
async def test_resolutions_settle_the_conflicts_and_say_what_was_kept_or_merged():
    revert, _ = _revert_tool()
    tip_entries = _tree(
        _entry("src/a.py", "a2" * 20), _entry("src/new.py", "d2" * 20), _entry("src/old.py", "bb" * 20),
        _entry("kept.md", "cc" * 20), _entry("run.sh", "ee" * 20, "100755"),
    )
    fake = FakeGitHub(tip_entries=tip_entries)
    with patch(_SAFE_REQUEST, new=fake):
        result = await revert(
            repo="acme/app",
            sha=REVERTED,
            resolutions=[{"path": "src/a.py", "content": "merged\n"}, {"path": "src/new.py", "keep": "branch"}],
        )

    assert "1 restored, 1 merged, 1 kept from the branch." in result
    assert "Restored: run.sh" in result and "Merged: src/a.py" in result and "Kept: src/new.py" in result
    assert "Already reverted on the branch: src/old.py" in result
    assert fake.urls("post")[0] == f"{API}/git/blobs"
    tree = fake.payload("post", f"{API}/git/trees")["tree"]
    assert {"path": "src/a.py", "mode": "100644", "type": "blob", "sha": BLOB} in tree
    assert not any(e["path"] in ("src/new.py", "src/old.py") for e in tree)


@pytest.mark.asyncio
async def test_a_resolution_of_an_unchanged_path_and_a_malformed_one_are_refused():
    revert, _ = _revert_tool()
    fake = FakeGitHub()
    with patch(_SAFE_REQUEST, new=fake):
        unchanged = await revert(repo="acme/app", sha=REVERTED, resolutions=[{"path": "kept.md", "keep": "branch"}])
        malformed = await revert(
            repo="acme/app", sha=REVERTED, resolutions=[{"path": "src/a.py", "keep": "branch", "content": "x"}]
        )
        unknown = await revert(repo="acme/app", sha=REVERTED, resolutions=[{"path": "src/a.py", "keep": "theirs"}])

    assert unchanged.startswith("Error: the resolutions name paths commit aaaaaaaa did not change: kept.md.")
    assert "exactly one of 'keep'" in malformed
    assert "'keep' for 'src/a.py' must be one of branch, parent, delete" in unknown
    assert fake.urls("post") == []


@pytest.mark.asyncio
async def test_an_already_reverted_branch_is_a_no_op():
    revert, _ = _revert_tool()
    fake = FakeGitHub(tip_entries=PARENT_ENTRIES)
    with patch(_SAFE_REQUEST, new=fake):
        result = await revert(repo="acme/app", sha=REVERTED)

    assert isinstance(result, ToolResult) and result.no_op
    assert "already has every file of commit aaaaaaaa as its parent had it" in result.content
    assert fake.urls("post") == []


@pytest.mark.asyncio
async def test_refusals_create_nothing():
    revert, _ = _revert_tool()
    with patch(_SAFE_REQUEST, new=FakeGitHub()) as fake:
        short = await revert(repo="acme/app", sha="abc123")
        exists = await revert(repo="acme/app", sha=REVERTED, branch="main")
    assert short.startswith("Error: 'sha' must be the full object id")
    # 'branch' alone naming the default branch never moves it: the base must be named too.
    assert exists.startswith("Error: branch 'main' already exists in acme/app.")
    assert fake.urls("post") == []

    with patch(_SAFE_REQUEST, new=FakeGitHub(truncated=True)) as fake:
        truncated = await revert(repo="acme/app", sha=REVERTED)
    assert "did not list the parent tree of acme/app in full" in truncated
    assert fake.urls("post") == []

    with patch(_SAFE_REQUEST, new=FakeGitHub(parents=[])) as fake:
        root = await revert(repo="acme/app", sha=REVERTED)
    assert "has no parent (it is the root commit)" in root

    with patch(_SAFE_REQUEST, new=FakeGitHub(parents=[PARENT, "7" * 40])) as fake:
        bad_parent = await revert(repo="acme/app", sha=REVERTED, parent=3)
    assert "has 2 parent(s); 'parent' must be between 1 and 2" in bad_parent


@pytest.mark.asyncio
async def test_a_branch_that_moved_meanwhile_keeps_the_commit_off_every_branch():
    revert, _ = _revert_tool()
    fake = FakeGitHub(moved_tip="8" * 40)
    with patch(_SAFE_REQUEST, new=fake):
        result = await revert(repo="acme/app", sha=REVERTED)

    assert result.startswith("Error: branch 'main' of acme/app changed while the commit was being built")
    assert f"{API}/git/refs" not in fake.urls("post")
    assert f"{API}/git/commits" in fake.urls("post")


#: A commit that changed plain files only: a.py edited, old.py deleted, new.py added.
PLAIN_REVERTED = _tree(_entry("src/a.py", "a1" * 20), _entry("src/new.py", "dd" * 20), _entry("kept.md", "cc" * 20),
                       _entry("run.sh", "ee" * 20))


class PlainFake(FakeGitHub):
    """The same history, the reverted commit touching plain files only, so the revert can be signed."""

    async def __call__(self, client, method, url, provider, action, **kwargs):
        if method == "get" and url == f"{API}/git/trees/{REVERTED_TREE}":
            self.calls.append((method, url, kwargs))
            return httpx.Response(200, json={"sha": REVERTED_TREE, "tree": PLAIN_REVERTED, "truncated": False}), None
        return await super().__call__(client, method, url, provider, action, **kwargs)


@pytest.mark.asyncio
async def test_a_plain_file_revert_is_signed_by_github_on_a_new_branch():
    revert, _ = _revert_tool()
    fake = PlainFake(tip_entries=PLAIN_REVERTED)
    with patch(_SAFE_REQUEST, new=fake):
        result = await revert(repo="acme/app", sha=REVERTED)

    assert result.startswith(f"Reverted commit {REVERTED[:8]} of acme/app on new branch 'revert-{REVERTED[:8]}'"), result
    assert "Signed: GitHub built the commit with createCommitOnBranch (verified)" in result
    # The branch is created at the base tip, then GitHub builds the commit on it: no Git Data API commit.
    assert fake.payload("post", f"{API}/git/refs") == {"ref": f"refs/heads/revert-{REVERTED[:8]}", "sha": TIP}
    assert f"{API}/git/commits" not in fake.urls("post") and f"{API}/git/trees" not in fake.urls("post")
    (commit_input,) = fake.signed_inputs
    assert commit_input["expectedHeadOid"] == TIP
    assert commit_input["branch"]["branchName"] == f"revert-{REVERTED[:8]}"
    assert commit_input["message"] == {
        "headline": "revert: feat(chat): move to websockets", "body": f"This reverts commit {REVERTED}."
    }
    changes = commit_input["fileChanges"]
    assert sorted(item["path"] for item in changes["additions"]) == ["src/a.py", "src/old.py"]
    assert changes["deletions"] == [{"path": "src/new.py"}]


@pytest.mark.asyncio
async def test_a_signed_revert_on_an_existing_branch_chains_on_its_tip():
    revert, _ = _revert_tool()
    fake = PlainFake(tip_entries=PLAIN_REVERTED, open_prs=[{"number": 42}])
    with patch(_SAFE_REQUEST, new=fake):
        result = await revert(repo="acme/app", sha=REVERTED, branch="main", base_branch="main")

    assert "on branch 'main' in one commit" in result and "Signed:" in result
    assert fake.urls("post") == [GRAPHQL] and fake.signed_inputs[0]["expectedHeadOid"] == TIP


@pytest.mark.asyncio
async def test_an_unsigned_revert_on_a_branch_with_an_open_pull_request_is_refused():
    revert, _ = _revert_tool()
    # run.sh changes mode, so the revert cannot be signed.
    fake = FakeGitHub(open_prs=[{"number": 42}])
    with patch(_SAFE_REQUEST, new=fake):
        result = await revert(repo="acme/app", sha=REVERTED, branch="main", base_branch="main")

    assert result.startswith("Error: this revert cannot be signed ('run.sh' is not a plain file"), result
    assert "open pull request (#42)" in result and "allow_unsigned" in result
    assert fake.urls("post") == [] and fake.urls("patch") == []

    fake = FakeGitHub(open_prs=[{"number": 42}])
    with patch(_SAFE_REQUEST, new=fake):
        forced = await revert(repo="acme/app", sha=REVERTED, branch="main", base_branch="main", allow_unsigned=True)
    assert "on branch 'main' in one commit" in forced and "Not signed:" in forced
    assert f"{API}/pulls" not in fake.urls("get")


@pytest.mark.asyncio
async def test_a_refused_signed_commit_on_a_new_branch_names_the_empty_branch():
    revert, _ = _revert_tool()
    fake = PlainFake(tip_entries=PLAIN_REVERTED, signed_refusal="Something went wrong")
    with patch(_SAFE_REQUEST, new=fake):
        result = await revert(repo="acme/app", sha=REVERTED)

    assert "holds no revert" in result and f"{API}/git/commits" not in fake.urls("post")


def test_the_tool_is_registered_as_a_write_and_described():
    _, definition = _revert_tool()
    from app.services.tool_classification import classify_tool_name

    assert classify_tool_name("revert_github_commit") == ("write", "write_token")
    assert definition.input_schema["required"] == ["repo", "sha"]
    assert "whatever its size" in definition.description
    assert "named after the commit" in definition.description
    assert revert_module._KEEP_CHOICES == ("branch", "parent", "delete")
