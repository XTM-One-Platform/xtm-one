"""Resolutions of a conflicting ``merge_github_branch``.

A merge whose two sides changed the same paths is refused by GitHub's Merges
API, and the only commit GitHub builds and signs for a user credential,
``createCommitOnBranch``, has one parent: a merge commit the agent built
itself would be unsigned, which a branch requiring signed commits refuses.
So a conflict is resolved in three steps GitHub builds and signs:

1. commits on the branch giving each resolved path the ref's version, so the
   merge has nothing left to conflict on;
2. the merge itself, through the Merges API;
3. commits writing the resolved content of every path not resolved to the
   ref's version (the branch's own version, a merged text, or a file of the
   sandbox workspace).

Steps 1 and 3 (and the undo) are split into as many signed commits as the
5 MB cap of one ``createCommitOnBranch`` needs, each chained on the
last with ``expectedHeadOid``: four 1.3 MB i18n catalogues conflicting at
once land in two commits per step instead of being refused. A single file
over the cap, or a resolution set over the read budget, is refused before
the first write with each path, its size and the split named.

Everything a resolution needs is read and checked before the first write: a
path changed on both sides without a resolution, a resolution naming another
path, a file mode ``createCommitOnBranch`` cannot keep, a workspace file that
cannot be read, or any of the three commits (the alignment, the resolution, the
undo) over the commit cap writes nothing. The caps are checked on the sizes the
tree listings give before a blob is read, and only the blobs of the paths the
two sides hold differently are read. The merge is pinned to the commit the ref
pointed at when it was read, so a ref or a branch moving meanwhile is reported,
never taken for a resolved merge. When the merge is still refused after step 1,
the branch goes back to the head it had before the call: one atomic ref
update (``updateRefs`` with ``beforeOid`` set to the tip this call left), so
no take-and-restore commit pair stays in the branch history and nothing
another writer pushed meanwhile is dropped. When GitHub refuses that update,
a signed commit puts the branch's own versions back instead; it carries
``expectedHeadOid``, so it never lands over a merge that did. A commit whose
answer is lost is never called unchanged: the branch tip is read again. When it
is the commit asked for (its one parent is the tip it was chained on and its
headline is the one sent), the commit counts as landed and the sequence goes
on, so an alignment commit GitHub landed before answering 5xx is undone with
the others when the merge stops; otherwise the answer says whether the commit
may have landed. A branch that moved before this call wrote anything (its first
commit refused on ``expectedHeadOid``, or the Merges API answering 204) is read
again: when it now contains the ref commit, the same merge landed meanwhile (a
call replayed after a worker restart), and the answer is a no-change success
with the head, never "Nothing was merged"; a foreign push still refuses.

A split resolution (step 3) is all or an error: a chunk GitHub refuses while
the branch did not move is sent once more; a resolution still unfinished
answers ``Error:`` naming every path not written; after the last chunk the
tree of that last commit, read by its id (never through the branch name,
which can still answer the merge commit for a moment), is compared by blob
id with every resolved path, and a mismatch is an error naming the expected
and found blob id of each path. A branch still pointing elsewhere after a
short re-read is named as a warning. A call stopped between two chunks leaves the branch on
``Resolve the merge of <ref>: N files (k/n)``; its replay meets a merge
already there (the Merges API answers 204), so the resolutions it was given
are checked against the tip and, when the tip is that stopped commit, the
paths still missing are written (:func:`finish_stopped_resolution`), never
answered as "no change".
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import re
import time
from dataclasses import dataclass
from typing import Any, NamedTuple

from app.tools.base import current_workspace

from . import _common
from ._common import _GRAPHQL_URL, _PROVIDER, _coerce_str, _enc, _json_headers
from .contents import (
    _CREATE_COMMIT_MUTATION,
    _GIT_OBJECT_ID_RE,
    _MAX_CHECKOUT_SPLIT_CHANGES,
    _MAX_COMMIT_CHANGES,
    _MAX_COMMIT_CONTENT_BYTES,
    _MODE_EXECUTABLE,
    _MODE_FILE,
    _commit_path,
    _graphql_commit_refusal,
    _graphql_error_text,
    _graphql_signature_verdict,
    _json_object,
    _rest_signature_verdict,
    _scratch_branch_name,
    _sweep_stale_scratch_branches,
)

#: What ``keep`` takes: the branch's own version, the merged ref's, or both sides' lines in each overlapping hunk
#: (settled into a merged text by ``merge_github_branch`` before :func:`resolve_and_merge` runs).
KEEP_CHOICES = ("branch", "ref", "both")
#: Paths named per line of an answer; the rest are counted.
_MAX_LISTED = 20
#: The blob bytes one resolved merge may read: the commits are split under the per-commit cap, this bounds the whole.
_MAX_READ_BYTES = 8 * _MAX_COMMIT_CONTENT_BYTES
_KINDS = {"100755": "an executable file", "120000": "a symbolic link", "160000": "a submodule", "040000": "a directory"}


def _mode_refusal(path: str, sides: tuple[tuple[str, _Entry | None], ...]) -> str | None:
    """Why the conflict on *path* cannot be resolved in signed commits, or ``None`` when it can.

    ``createCommitOnBranch`` writes plain files and keeps the mode of a path it overwrites (an executable
    script stays 100755, #10444), but it cannot create a file executable nor set or clear the bit. An
    executable file held 100755 on both sides is therefore resolved like a plain file; a symbolic link, a
    submodule, a directory, an executable on one side only or a mode change between the sides is not.
    """
    present = [(side, entry) for side, entry in sides if entry is not None]
    for side, entry in present:
        if entry.mode not in (_MODE_FILE, _MODE_EXECUTABLE):
            kind = _KINDS.get(entry.mode, f"a path of mode {entry.mode}")
            return (
                f"Error: '{path}' is {kind} on '{side}': createCommitOnBranch, the commit GitHub signs, writes "
                "plain files only, so this conflict is resolved by hand. Nothing was written."
            )
    executable = [side for side, entry in present if entry.mode == _MODE_EXECUTABLE]
    if executable and len(executable) != len(sides):
        return (
            f"Error: '{path}' is an executable file on '{executable[0]}' only: createCommitOnBranch, the commit "
            "GitHub signs, cannot create a file executable nor set or clear the bit, so this conflict is resolved "
            "by hand. Nothing was written."
        )
    return None


class Resolution(NamedTuple):
    """How one path changed on both sides is settled: exactly one field is set."""

    keep: str | None
    content: str | None
    workspace_path: str | None


class _Entry(NamedTuple):
    """One path of a tree: its mode, object id and size in bytes (0 when the listing gives none)."""

    mode: str
    sha: str
    size: int = 0


class _Side(NamedTuple):
    """One side of the merge: its commit and its tree, path by path."""

    commit: str
    entries: dict[str, _Entry]


@dataclass(frozen=True)
class _Lost:
    """A commit GitHub may have made: its answer was lost and the branch tip moved or could not be read."""

    text: str


_TO_REF = "the ref's versions of the conflicting paths"
_BACK = "the branch's versions an undo would put back"
_FINAL = "the resolved contents"


def _over_read_budget(size: int, to_ref_sizes: dict[str, int]) -> str:
    return (
        f"Error: the two versions of the conflicting files add up to {size} bytes, over the {_MAX_READ_BYTES} one "
        "resolved merge may read; nothing was written. Bring the largest files onto the ref's version first with "
        "create_github_commit (a commit per batch below), then call this tool again for the rest.\n"
        + _describe_split(_TO_REF, to_ref_sizes)
    )


def split_batches(sizes: dict[str, int]) -> list[list[str]]:
    """The paths of ``{path: bytes}`` in batches each one signed commit may carry (first fit, largest first).

    A path over the cap on its own gets a batch of its own: the caller refuses it before any write.
    """
    batches: list[tuple[int, list[str]]] = []
    for path in sorted(sizes, key=lambda item: (-sizes[item], item)):
        size = sizes[path]
        for index, (used, members) in enumerate(batches):
            if used + size <= _MAX_COMMIT_CONTENT_BYTES and len(members) < _MAX_COMMIT_CHANGES:
                batches[index] = (used + size, [*members, path])
                break
        else:
            batches.append((size, [path]))
    return [sorted(members) for _, members in batches]


def _describe_split(what: str, sizes: dict[str, int]) -> str:
    """The split *sizes* would take, one line per commit, for a refusal."""
    batches = split_batches(sizes)
    lines = [f"The split of {what} would be {len(batches)} signed commit{'s' if len(batches) != 1 else ''}:"]
    for number, paths in enumerate(batches, 1):
        total = sum(sizes[path] for path in paths)
        lines.append(f"  {number}. {total} bytes: {_listed(paths)}")
    return "\n".join(lines)


def _single_over_cap(what: str, sizes: dict[str, int]) -> str | None:
    """The refusal naming every file of *what* over the cap on its own, or None."""
    big = sorted(path for path, size in sizes.items() if size > _MAX_COMMIT_CONTENT_BYTES)
    if not big:
        return None
    named = ", ".join(f"{path} ({sizes[path]} bytes)" for path in big[:_MAX_LISTED])
    return (
        f"Error: {what} hold {len(big)} file{'s' if len(big) != 1 else ''} over the {_MAX_COMMIT_CONTENT_BYTES} "
        f"bytes one signed commit may carry, which no split can bring under it: {named}. Resolve "
        f"{'them' if len(big) != 1 else 'it'} by hand; nothing was written."
    )


@dataclass
class _Chain:
    """Signed commits landed one after another: the tip, each commit's (oid, word, paths), and the failure if any."""

    tip: str
    commits: list[tuple[str, str, list[str]]]
    failure: _Lost | str | None = None

    @property
    def paths(self) -> list[str]:
        return sorted(path for _, _, paths in self.commits for path in paths)


def _git_blob_id(data: bytes) -> str:
    """The object id git gives *data* as a blob, to compare a resolved content with a tree entry unread."""
    return hashlib.sha1(b"blob %d\0" % len(data) + data, usedforsecurity=False).hexdigest()


def _listed(paths: list[str]) -> str:
    more = len(paths) - _MAX_LISTED
    return ", ".join(paths[:_MAX_LISTED]) + (f" (and {more} more)" if more > 0 else "")


def parse_resolutions(raw: Any) -> tuple[dict[str, Resolution], str | None]:
    """``{path: resolution}`` from the ``resolutions`` argument, or an error; nothing is requested."""
    if raw is None or raw == "" or raw == []:
        return {}, None
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return {}, "Error: 'resolutions' must be a list."
    if not isinstance(raw, list):
        return {}, "Error: 'resolutions' must be a list."
    # The commits are split under the per-commit cap (split_batches), so more paths than one commit carries work.
    if len(raw) > _MAX_CHECKOUT_SPLIT_CHANGES:
        return {}, (
            f"Error: 'resolutions' names {len(raw)} paths, over the {_MAX_CHECKOUT_SPLIT_CHANGES} one call splits into "
            "signed commits: resolve the largest share with create_github_commit first."
        )
    parsed: dict[str, Resolution] = {}
    for item in raw:
        if not isinstance(item, dict):
            return {}, (
                "Error: each resolution is an object with a 'path' and one of 'keep', 'content' or 'workspace_path'."
            )
        path, err = _commit_path(item.get("path"), "resolutions")
        if err:
            return {}, err
        keep = item.get("keep")
        content = item.get("content")
        workspace_path = _coerce_str(item.get("workspace_path")).strip()
        ways = sum((keep is not None, isinstance(content, str), bool(workspace_path)))
        if ways != 1:
            return {}, (
                f"Error: the resolution of '{path}' needs exactly one of 'keep' ({' / '.join(KEEP_CHOICES)}), "
                "'content' (the merged text) or 'workspace_path' (a file of the sandbox workspace)."
            )
        if keep is not None and keep not in KEEP_CHOICES:
            return {}, f"Error: 'keep' for '{path}' must be one of {', '.join(KEEP_CHOICES)}."
        if path in parsed:
            return {}, f"Error: '{path}' is resolved more than once."
        parsed[path] = Resolution(keep, content if isinstance(content, str) else None, workspace_path or None)
    return parsed, None


async def _read_side(client: Any, headers: dict[str, str], repo: str, repo_url: str, ref: str) -> _Side | str:
    """The commit *ref* points at and its recursive tree listing, or an error."""
    resp, err = await _common.safe_request(
        client, "get", f"{repo_url}/commits/{_enc(ref)}", _PROVIDER, f"read commit {ref} of {repo}", headers=headers
    )
    if err:
        return err
    data = _json_object(resp)
    commit = _coerce_str(data.get("sha"))
    inner = data.get("commit")
    tree_ref = inner.get("tree") if isinstance(inner, dict) else None
    tree = _coerce_str(tree_ref.get("sha")) if isinstance(tree_ref, dict) else ""
    if not _GIT_OBJECT_ID_RE.fullmatch(commit) or not _GIT_OBJECT_ID_RE.fullmatch(tree):
        return f"Error: GitHub did not return the commit and tree of '{ref}' in {repo}."
    resp, err = await _common.safe_request(
        client, "get", f"{repo_url}/git/trees/{tree}", _PROVIDER, f"read the tree of {ref} in {repo}",
        headers=headers, params={"recursive": "1"},
    )
    if err:
        return err
    listing = _json_object(resp)
    if listing.get("truncated") or not isinstance(listing.get("tree"), list):
        return f"Error: GitHub did not list the tree of '{ref}' in {repo} in full, so the resolutions cannot be checked."
    entries: dict[str, _Entry] = {}
    for item in listing["tree"]:
        if isinstance(item, dict) and isinstance(item.get("path"), str) and item.get("type") in ("blob", "commit"):
            size = item.get("size")
            entries[item["path"]] = _Entry(
                _coerce_str(item.get("mode")),
                _coerce_str(item.get("sha")),
                size if isinstance(size, int) and not isinstance(size, bool) and size > 0 else 0,
            )
    return _Side(commit, entries)


async def _read_blob(client: Any, headers: dict[str, str], repo: str, repo_url: str, sha: str, path: str) -> bytes | str:
    """The bytes of one blob, or an error."""
    resp, err = await _common.safe_request(
        client, "get", f"{repo_url}/git/blobs/{sha}", _PROVIDER, f"read '{path}' in {repo}", headers=headers
    )
    if err:
        return err
    data = _json_object(resp)
    if data.get("encoding") != "base64" or not isinstance(data.get("content"), str):
        return f"Error: GitHub did not return the content of '{path}' in {repo}."
    try:
        return base64.b64decode(data["content"])
    except ValueError:
        return f"Error: GitHub returned an unreadable content for '{path}' in {repo}."


def _signature_word(commit: dict[str, Any]) -> str:
    verdict = _graphql_signature_verdict(commit)
    if verdict is None:
        return "signed by GitHub"
    if verdict.verified:
        return "verified"
    return f"signed, not verified ({verdict.reason})" if verdict.signed else "unsigned"


async def _branch_tip(client: Any, headers: dict[str, str], repo: str, repo_url: str, branch: str) -> str:
    """The commit *branch* points at now, or '' when it cannot be read."""
    resp, err = await _common.safe_request(
        client, "get", f"{repo_url}/commits/{_enc(branch)}", _PROVIDER, f"read the tip of {branch} in {repo}",
        headers=headers,
    )
    if err:
        return ""
    commit = _coerce_str(_json_object(resp).get("sha"))
    return commit if _GIT_OBJECT_ID_RE.fullmatch(commit) else ""


async def _already_merged(
    client: Any, headers: dict[str, str], repo: str, repo_url: str, branch: str, ref_commit: str
) -> str:
    """The tip of *branch* when it now contains *ref_commit* (behind it by 0), else ''.

    A first commit refused because the branch moved is not always a foreign push: the same merge may have
    landed meanwhile (a call replayed after a worker restart, or an identical call running at once). Then
    the work asked for is on the branch and the answer is a success, never "Nothing was merged".
    """
    now = await _branch_tip(client, headers, repo, repo_url, branch)
    if not now:
        return ""
    resp, err = await _common.safe_request(
        client, "get", f"{repo_url}/compare/{ref_commit}...{now}", _PROVIDER,
        f"check {branch} of {repo} contains {ref_commit[:12]}", headers=headers,
    )
    if err:
        return ""
    compared = _json_object(resp)
    if compared.get("status") in ("ahead", "identical") and compared.get("behind_by") == 0:
        return now
    return ""


async def _landed_commit(
    client: Any, headers: dict[str, str], repo: str, repo_url: str, branch: str, tip: str, headline: str
) -> tuple[str, str] | None:
    """``(commit, signature word)`` when *branch* now points at the commit this call asked for, else ``None``.

    The commit is recognised by its single parent, the tip it was chained on, and its headline: a 5xx that
    GitHub answered after landing the commit then counts as landed, so the sequence (and its undo) goes on.
    """
    resp, err = await _common.safe_request(
        client, "get", f"{repo_url}/commits/{_enc(branch)}", _PROVIDER, f"read the tip of {branch} in {repo}",
        headers=headers,
    )
    if err:
        return None
    answer = _json_object(resp)
    oid = _coerce_str(answer.get("sha"))
    parents = answer.get("parents")
    commit = answer.get("commit")
    if not _GIT_OBJECT_ID_RE.fullmatch(oid) or oid == tip or not isinstance(parents, list) or len(parents) != 1:
        return None
    parent = parents[0].get("sha") if isinstance(parents[0], dict) else None
    message = _coerce_str(commit.get("message")) if isinstance(commit, dict) else ""
    if parent != tip or message.split("\n", 1)[0].strip() != headline.strip():
        return None
    verdict = _rest_signature_verdict(commit) if isinstance(commit, dict) else None
    if verdict is None:
        word = "signed by GitHub"
    elif verdict.verified:
        word = "verified"
    else:
        word = f"signed, not verified ({verdict.reason})" if verdict.signed else "unsigned"
    return oid, f"{word}, read back after GitHub's answer was lost"


async def _lost_answer(
    client: Any,
    headers: dict[str, str],
    repo: str,
    repo_url: str,
    branch: str,
    tip: str,
    why: str,
    headline: str = "",
) -> tuple[str, str] | _Lost | str:
    """After a commit whose answer was lost: the commit when it is read back on the branch, a refusal when the tip
    did not move, else an unknown outcome."""
    if headline:
        landed = await _landed_commit(client, headers, repo, repo_url, branch, tip, headline)
        if landed is not None:
            return landed
    now = await _branch_tip(client, headers, repo, repo_url, branch)
    if now == tip:
        return f"{why} Branch '{branch}' of {repo} still points at {tip[:12]}, so the commit did not land."
    where = f"now points at {now[:12]}" if now else "could not be read again"
    return _Lost(
        f"{why} Whether the commit landed is not known: branch '{branch}' of {repo} {where} "
        f"(it pointed at {tip[:12]}). Read it with list_github_commits before calling this tool again."
    )


async def _signed_commit(
    client: Any,
    headers: dict[str, str],
    repo: str,
    repo_url: str,
    branch: str,
    tip: str,
    headline: str,
    additions: dict[str, bytes],
    deletions: list[str],
    body: str = "",
) -> tuple[str, str] | _Lost | str:
    """``(commit, signature word)`` of a commit GitHub builds on *branch* at *tip*, the refusal, or a lost answer."""
    owner, _, name = repo.partition("/")
    file_changes: dict[str, Any] = {}
    if additions:
        file_changes["additions"] = [
            {"path": path, "contents": base64.b64encode(data).decode("ascii")} for path, data in additions.items()
        ]
    if deletions:
        file_changes["deletions"] = [{"path": path} for path in deletions]
    commit_input = {
        "branch": {"repositoryNameWithOwner": f"{owner}/{name}", "branchName": branch},
        "expectedHeadOid": tip,
        "message": {"headline": headline, **({"body": body} if body else {})},
        "fileChanges": file_changes,
    }
    resp, err = await _common.safe_request(
        client, "post", _GRAPHQL_URL, _PROVIDER, f"commit to branch {branch} of {repo}",
        headers=_json_headers(headers),
        json={"query": _CREATE_COMMIT_MUTATION, "variables": {"input": commit_input}},
        expected_statuses=set(),
    )
    if err and (resp is None or resp.status_code >= 500):
        return await _lost_answer(client, headers, repo, repo_url, branch, tip, err, headline)
    if err:
        return err
    answer = _json_object(resp)
    data = answer.get("data")
    created = data.get("createCommitOnBranch") if isinstance(data, dict) else None
    commit = created.get("commit") if isinstance(created, dict) else None
    oid = _coerce_str(commit.get("oid")) if isinstance(commit, dict) else ""
    if isinstance(commit, dict) and _GIT_OBJECT_ID_RE.fullmatch(oid):
        return oid, _signature_word(commit)
    errors = answer.get("errors")
    if isinstance(errors, list) and errors:
        message, kinds = _graphql_error_text(errors)
        return _graphql_commit_refusal(message, kinds, branch, repo, tip)
    return await _lost_answer(
        client, headers, repo, repo_url, branch, tip,
        f"Error: GitHub did not return the new commit on branch '{branch}' of {repo}.", headline,
    )


async def _signed_commits(
    client: Any,
    headers: dict[str, str],
    repo: str,
    repo_url: str,
    branch: str,
    tip: str,
    headline: str,
    payload: dict[str, bytes | None],
) -> _Chain:
    """Commit *payload* (``None`` deletes the path) in signed commits each under the cap, chained from *tip*."""
    batches = split_batches({path: 0 if data is None else len(data) for path, data in payload.items()})
    chain = _Chain(tip, [])
    for number, paths in enumerate(batches, 1):
        part = f" ({number}/{len(batches)})" if len(batches) > 1 else ""
        additions = {path: data for path in paths if (data := payload[path]) is not None}
        deletions = [path for path in paths if payload[path] is None]
        made = await _signed_commit(
            client, headers, repo, repo_url, branch, chain.tip, headline + part, additions, deletions
        )
        if isinstance(made, str) and await _branch_tip(client, headers, repo, repo_url, branch) == chain.tip:
            # Refused while the branch did not move (a transient GraphQL refusal): the same chunk once more, so a
            # split is not left at (k/n) by one bad answer. A branch that moved is never written over.
            made = await _signed_commit(
                client, headers, repo, repo_url, branch, chain.tip, headline + part, additions, deletions
            )
        if isinstance(made, _Lost | str):
            chain.failure = made
            return chain
        chain.tip = made[0]
        chain.commits.append((made[0], made[1], paths))
    return chain


#: Moves one branch only while it still points at ``beforeOid``, atomically: the expected-head check a plain
#: force-push lacks. ``force`` is needed because going back to an ancestor is not a fast-forward.
_UPDATE_REFS_MUTATION = (
    "mutation($input: UpdateRefsInput!) { updateRefs(input: $input) { clientMutationId } }"
)
_REPOSITORY_ID_QUERY = "query($owner: String!, $name: String!) { repository(owner: $owner, name: $name) { id } }"


async def _restore_head(
    client: Any, headers: dict[str, str], repo: str, repo_url: str, branch: str, before_call: str, left: str
) -> str | None:
    """Point *branch* back at *before_call* while it still points at *left*; None once read back, else why not.

    The commits this call landed then leave the branch history, so a branch whose repository checks every
    commit's signature keeps no take-and-restore pair, and nothing pushed by another writer since is dropped:
    GitHub refuses the update when the branch no longer points at *left*.
    """
    owner, _, name = repo.partition("/")
    resp, err = await _common.safe_request(
        client, "post", _GRAPHQL_URL, _PROVIDER, f"read the id of {repo}", headers=_json_headers(headers),
        json={"query": _REPOSITORY_ID_QUERY, "variables": {"owner": owner, "name": name}},
    )
    if err:
        return err
    data = _json_object(resp).get("data")
    repository = data.get("repository") if isinstance(data, dict) else None
    repository_id = _coerce_str(repository.get("id")) if isinstance(repository, dict) else ""
    if not repository_id:
        return f"GitHub did not return the id of {repo}."
    update = {"name": f"refs/heads/{branch}", "beforeOid": left, "afterOid": before_call, "force": True}
    resp, err = await _common.safe_request(
        client, "post", _GRAPHQL_URL, _PROVIDER, f"move branch {branch} of {repo} back to {before_call[:12]}",
        headers=_json_headers(headers),
        json={"query": _UPDATE_REFS_MUTATION, "variables": {"input": {"repositoryId": repository_id, "refUpdates": [update]}}},
        expected_statuses=set(),
    )
    errors = _json_object(resp).get("errors") if resp is not None and not err else None
    # The answer alone is not trusted: the branch is read back, so a lost answer that landed still counts.
    now = await _branch_tip(client, headers, repo, repo_url, branch)
    if now == before_call:
        return None
    if err:
        return err
    if isinstance(errors, list) and errors:
        return _graphql_error_text(errors)[0] or "GitHub refused the ref update."
    return f"branch '{branch}' points at {now[:12] or 'an unreadable commit'} after the ref update."


#: The headline of an alignment commit (step 1) this tool writes, the batch suffix of a split included.
_ALIGNMENT_HEADLINE_RE = re.compile(r"^Take .+'s version of \d+ conflicting files? before the merge(?: \(\d+/\d+\))?$")
#: Alignment commits walked back from a branch tip at most: the read budget splits into fewer batches than this.
_MAX_STOPPED_ALIGNMENT = 16


async def _commit_link(
    client: Any, headers: dict[str, str], repo: str, repo_url: str, ref: str
) -> tuple[str, str, str] | None:
    """``(commit, its one parent, headline)`` of *ref*, or ``None`` when unreadable or not a single-parent commit."""
    resp, err = await _common.safe_request(
        client, "get", f"{repo_url}/commits/{_enc(ref)}", _PROVIDER, f"read commit {ref} in {repo}", headers=headers,
    )
    if err:
        return None
    answer = _json_object(resp)
    oid = _coerce_str(answer.get("sha"))
    parents = answer.get("parents")
    commit = answer.get("commit")
    if not _GIT_OBJECT_ID_RE.fullmatch(oid) or not isinstance(parents, list) or len(parents) != 1:
        return None
    parent = _coerce_str(parents[0].get("sha")) if isinstance(parents[0], dict) else ""
    if not _GIT_OBJECT_ID_RE.fullmatch(parent):
        return None
    message = _coerce_str(commit.get("message")) if isinstance(commit, dict) else ""
    return oid, parent, message.split("\n", 1)[0].strip()


async def drop_stopped_alignment(
    client: Any, headers: dict[str, str], repo: str, repo_url: str, branch: str
) -> tuple[str, str | None]:
    """Move *branch* off the alignment commits a stopped call of this tool left at its tip: ``(note, refusal)``.

    A call stopped between step 1 and the merge (a worker stop, a deploy) leaves the branch on commits giving
    the conflicting paths the ref's version. A replay merging from there sees those paths equal to the ref, so
    they no longer conflict and the branch's own changes to them are lost without a word. The tip is read
    first: while it is an alignment commit (its headline, one parent), the walk goes back to the commit before
    them, and the branch is moved there with the same atomic ref update as the undo (``beforeOid`` = the tip
    read, so nothing pushed meanwhile is dropped). Then every conflicting path is resolved from both sides
    again. A tip that is not an alignment commit, or cannot be read, changes nothing; a refused move is a
    refusal: merging over it would lose those paths.
    """
    link = await _commit_link(client, headers, repo, repo_url, branch)
    if link is None or not _ALIGNMENT_HEADLINE_RE.fullmatch(link[2]):
        return "", None
    tip, parent = link[0], link[1]
    dropped = [tip]
    while len(dropped) < _MAX_STOPPED_ALIGNMENT:
        before = await _commit_link(client, headers, repo, repo_url, parent)
        if before is None or not _ALIGNMENT_HEADLINE_RE.fullmatch(before[2]):
            break
        dropped.append(before[0])
        parent = before[1]
    commits = ", ".join(oid[:12] for oid in reversed(dropped))
    why = await _restore_head(client, headers, repo, repo_url, branch, parent, tip)
    if why is not None:
        return "", (
            f"Error: '{branch}' of {repo} ends with {len(dropped)} alignment commit(s) a stopped call of this tool "
            f"left ({commits}: '{link[2]}'), and moving it back to {parent[:12]} was refused ({why}). Nothing was "
            "merged: merging over them would keep the ref's version of those files. Read the branch with "
            "list_github_commits and put the branch's versions back before merging again."
        )
    return (
        f"'{branch}' ended with {len(dropped)} alignment commit(s) a stopped call of this tool left ({commits}); "
        f"it was moved back to {parent[:12]} first, so every conflicting path was resolved from both sides again."
    ), None


async def _alignment_failed(
    client: Any,
    headers: dict[str, str],
    repo: str,
    repo_url: str,
    branch: str,
    chain: _Chain,
    back: dict[str, bytes | None],
    before_call: str = "",
    shown: str = "",
) -> str:
    """The answer when an alignment commit fails: the commits already landed are undone, nothing is merged."""
    failure = chain.failure or "Error: a commit of the alignment did not land."
    text = _failure_text(failure)
    if not chain.commits:
        return f"{text} Nothing was merged." if isinstance(failure, _Lost) else f"{text}\nNothing was merged."
    landed = chain.paths
    if isinstance(failure, _Lost):
        # The tip is unknown: an undo built on the last known tip would be refused or land wrong.
        return (
            f"{text} Nothing was merged. Already on '{branch}': the ref's versions of {_listed(landed)} "
            f"(tip {chain.tip[:12]}); read the branch before restoring them."
        )
    if before_call:
        why = await _restore_head(client, headers, repo, repo_url, branch, before_call, chain.tip)
        if why is None:
            return (
                f"{text}\nNothing was merged. '{branch}' was moved back to {before_call[:12]}, its head before this "
                f"call, so the commits already landed ({_listed(landed)}) left its history."
            )
        text = f"{text}\nThe ref update back to {before_call[:12]} was refused ({why}), so signed commits undo them."
    undone = await _signed_commits(
        client,
        headers,
        repo,
        repo_url,
        branch,
        chain.tip,
        f"Restore {shown or branch}'s version of the files a stopped merge needed",
        {path: back[path] for path in landed},
    )
    if undone.failure is not None:
        return (
            f"{text}\nNothing was merged. The ref's versions of {_listed(landed)} are on '{branch}' (tip "
            f"{chain.tip[:12]}) and their undo stopped: {_failure_text(undone.failure)}"
        )
    by = ", ".join(f"{oid[:12]} ({word})" for oid, word, _ in undone.commits)
    return f"{text}\nNothing was merged. The commits already landed were undone by {by}."


def _all_listed(paths: list[str]) -> str:
    """Every path, none counted away: an answer naming what was not written must name all of it."""
    return ", ".join(paths)


class _Mismatch(NamedTuple):
    """One path a tree does not hold as written: the blob id expected and the one found ("absent" for none)."""

    path: str
    expected: str
    actual: str


def _mismatch_text(wrong: list[_Mismatch]) -> str:
    """Every mismatching path with both blob ids, so a reader can tell a real difference from a stale read."""
    return "; ".join(f"{item.path} (expected blob {item.expected[:12]}, found {item.actual[:12]})" for item in wrong)


#: Pauses before each new read of a commit just written: GitHub's REST reads can lag a GraphQL commit by a moment.
_READ_BACK_DELAYS = (0.5, 1.5)


async def _tip_mismatch(
    client: Any, headers: dict[str, str], repo: str, repo_url: str, ref: str, payload: dict[str, bytes | None]
) -> list[_Mismatch] | str:
    """The paths of *payload* the tree of *ref* does not hold as written (``None``: absent), or why it is unread.

    Compared by blob id on the tree listing: no blob is read. *ref* is a branch, or the 40-character id of a
    commit just written: a commit id is immutable, so its tree never answers for an older tip.
    """
    side = await _read_side(client, headers, repo, repo_url, ref)
    if isinstance(side, str) and _GIT_OBJECT_ID_RE.fullmatch(ref):
        # A commit written a moment ago may not be readable yet: read it again before calling it unreadable.
        for delay in _READ_BACK_DELAYS:
            await asyncio.sleep(delay)
            side = await _read_side(client, headers, repo, repo_url, ref)
            if not isinstance(side, str):
                break
    if isinstance(side, str):
        return side
    wrong: list[_Mismatch] = []
    for path, data in sorted(payload.items()):
        entry = side.entries.get(path)
        expected = "absent" if data is None else _git_blob_id(data)
        actual = "absent" if entry is None else entry.sha
        if expected != actual:
            wrong.append(_Mismatch(path, expected, actual))
    return wrong


async def _settled_tip(
    client: Any, headers: dict[str, str], repo: str, repo_url: str, branch: str, written: str
) -> str:
    """The tip of *branch*, read again a few times while it still shows an older commit than *written*."""
    now = await _branch_tip(client, headers, repo, repo_url, branch)
    for delay in _READ_BACK_DELAYS:
        if now == written:
            break
        await asyncio.sleep(delay)
        now = await _branch_tip(client, headers, repo, repo_url, branch)
    return now


#: The headline of a resolution commit (step 3) this tool writes, its batch number and count captured.
_RESOLUTION_HEADLINE_RE = re.compile(r"^Resolve the merge of (?P<head>.+): \d+ files? \((?P<part>\d+)/(?P<of>\d+)\)$")


async def finish_stopped_resolution(
    client: Any,
    headers: dict[str, str],
    repo: str,
    repo_url: str,
    branch: str,
    head: str,
    resolutions: dict[str, Resolution],
) -> str | None:
    """The answer to a call whose merge is already on *branch* while *resolutions* were given, or None.

    A call stopped between two resolution commits of a split (a worker stop, a deploy) leaves the branch on
    ``Resolve the merge of <ref>: N files (k/n)`` with k < n. Its replay finds the merge already there (the
    Merges API answers 204) and would answer "no change" over a branch missing the last chunks. So the
    resolutions whose written content is known (``content``, ``workspace_path``) are compared with the tip by
    blob id; a ``keep`` is settled per overlapping hunk from the merge base, so it is named as not checked.
    When the tip is such a stopped resolution commit, the paths still missing are written in signed commits
    and named; otherwise nothing is written and the paths that differ are named, so
    a resolution left out is never answered as a plain no change. When no resolution can be checked and the tip
    is such a stopped commit, the answer is an error naming it and the unchecked paths. None: no resolution can
    be checked and the tip is not a stopped resolution.
    """
    expected: dict[str, bytes | None] = {}
    unchecked: list[str] = []
    workspace = None
    for path, resolution in sorted(resolutions.items()):
        if resolution.content is not None:
            expected[path] = resolution.content.encode("utf-8")
        elif resolution.workspace_path:
            workspace = workspace or current_workspace()
            if workspace is None:
                unchecked.append(path)
                continue
            try:
                _rel, _name, data = await workspace.read_file(resolution.workspace_path, tool="merge_github_branch")
            except ValueError:
                unchecked.append(path)
                continue
            expected[path] = data
        else:
            unchecked.append(path)
    lead = f"'{branch}' of {repo} already contains '{head}'"
    if not expected:
        # Nothing can be checked by blob id. A tip that is a resolution this tool left stopped still lacks its
        # last chunks: "no change" would hide them, so the stopped commit and the unchecked paths are named.
        link = await _commit_link(client, headers, repo, repo_url, branch)
        stopped = _RESOLUTION_HEADLINE_RE.fullmatch(link[2]) if link is not None else None
        if link is None or stopped is None or int(stopped["part"]) >= int(stopped["of"]):
            return None
        return (
            f"Error: {lead}, and its tip '{link[2]}' ({link[0][:12]}) is a resolution a stopped call of this tool "
            f"left at part {stopped['part']} of {stopped['of']}: the later parts were not written. These "
            f"resolutions cannot be checked or finished from the tip (a keep resolution, settled per hunk, or "
            f"unreadable): {_all_listed(unchecked)}. Nothing was written. Read those files on the branch against "
            "the merge base and write the missing content with create_github_commit, or call again with a merged "
            "'content' or 'workspace_path' for each."
        )
    wrong = await _tip_mismatch(client, headers, repo, repo_url, branch, expected)
    tail = (
        f"\nNot checked (a keep resolution, settled per hunk, or unreadable): {_listed(unchecked)}."
        if unchecked
        else ""
    )
    if isinstance(wrong, str):
        return f"Error: {lead}, and its tip could not be read to check the resolutions: {wrong}{tail}"
    if not wrong:
        return (
            f"No change: {lead}, and its tip holds the resolved content of {_all_listed(sorted(expected))}. "
            f"Nothing was written.{tail}"
        )
    wrong_paths = [item.path for item in wrong]
    link = await _commit_link(client, headers, repo, repo_url, branch)
    stopped = _RESOLUTION_HEADLINE_RE.fullmatch(link[2]) if link is not None else None
    if link is None or stopped is None or int(stopped["part"]) >= int(stopped["of"]):
        return (
            f"Error: {lead}, but its tip does not hold the resolved content of {_mismatch_text(wrong)}, and it is not "
            "a resolution this tool left stopped, so nothing was written. Read those files on the branch and "
            f"write them with create_github_commit if the resolution still applies.{tail}"
        )
    chain = await _signed_commits(
        client,
        headers,
        repo,
        repo_url,
        branch,
        link[0],
        f"Resolve the merge of {head}: {len(wrong)} file{'s' if len(wrong) != 1 else ''} left by a stopped call",
        {path: expected[path] for path in wrong_paths},
    )
    lines = _chain_lines("3", chain, "the resolved content of")
    if chain.failure is not None:
        left = sorted(set(wrong_paths) - set(chain.paths))
        return (
            f"Error: {lead}; '{link[2]}' left {len(wrong)} resolved file(s) unwritten, and finishing them stopped "
            f"with {_all_listed(left)} still not written: {_failure_text(chain.failure)}"
            + ("\n" + "\n".join(lines) if lines else "")
        )
    return (
        f"Finished a stopped resolution: {lead}, and '{link[2]}' ({link[0][:12]}) had left "
        f"{_all_listed(wrong_paths)} unwritten, now written:\n" + "\n".join(lines) + tail
    )


#: Words of the refusal when the ref moved between the snapshot and the merge (see :func:`ref_moved_and_undone`).
_REF_MOVED = "the resolutions were checked against"


def ref_moved_and_undone(answer: str) -> bool:
    """Whether *answer* refused a merge only because the ref moved, the branch left as it was.

    The refusal names the ref's new commit and either wrote nothing or moved the branch back to its head
    before the call: a fresh line merge against the new ref commit is then safe to run once more.
    """
    return (
        answer.startswith("Error")
        and _REF_MOVED in answer
        and ("Nothing was written." in answer or "its head before this call, so the alignment commits" in answer)
    )


#: The last line of an answer that wrote commits: the 40-character id of the last commit this call wrote.
_FINAL_HEAD_RE = re.compile(r"^Final head: (?P<sha>[0-9a-f]{40}) ", re.MULTILINE)


def final_head_line(sha: str) -> str:
    """The line naming the last commit a call wrote, so no reader takes an intermediate commit for the head."""
    return f"Final head: {sha} (the last commit this call wrote; CI is read on it)."


def final_head_of(answer: str) -> str:
    """The commit an answer's ``Final head:`` line names, or ``""``."""
    found = _FINAL_HEAD_RE.findall(answer)
    return found[-1] if found else ""


def _chain_lines(step: str, chain: _Chain, what: str) -> list[str]:
    return [f"{step}. {oid[:12]} ({word}): {what} {_listed(paths)}." for oid, word, paths in chain.commits]


def _failure_text(failure: _Lost | str) -> str:
    return failure.text if isinstance(failure, _Lost) else failure


async def _undo_alignment(
    client: Any,
    headers: dict[str, str],
    repo: str,
    repo_url: str,
    branch: str,
    before_call: str,
    tip: str,
    back: dict[str, bytes | None],
    shown: str = "",
) -> str:
    """Take the alignment commits of this call off *branch* (ref update, else signed restore commits); the sentence."""
    refused = await _restore_head(client, headers, repo, repo_url, branch, before_call, tip)
    if refused is None:
        return (
            f"'{branch}' was moved back to {before_call[:12]}, its head before this call, so the alignment commits "
            "left its history and its tree is as before the call."
        )
    undone = await _signed_commits(
        client,
        headers,
        repo,
        repo_url,
        branch,
        tip,
        f"Restore {shown or branch}'s version of the files a refused merge needed",
        back,
    )
    if undone.failure is not None:
        restored = f" ({_listed(undone.paths)} restored)" if undone.commits else ""
        return (
            f"The ref update back to {before_call[:12]} was refused ({refused}), and the undo{restored} could not be "
            f"finished: the ref's versions of {_listed(sorted(back))} may still be on '{branch}': "
            f"{_failure_text(undone.failure)}"
        )
    by = ", ".join(f"{oid[:12]} ({word})" for oid, word, _ in undone.commits)
    return (
        f"The ref update back to {before_call[:12]} was refused ({refused}); commit {tip[:12]} was undone by {by}: "
        f"'{branch}' holds its own versions again."
    )


def _broken_resolutions(
    written: dict[str, bytes],
    ref_versions: dict[str, bytes | None],
    branch_versions: dict[str, bytes | None] | None = None,
) -> str | None:
    """The refusal naming every given content that would break its path, before any write; None when all are sound.

    A content still holding a conflict hunk, a structured content (``.json``, ``.yaml``, ``.toml``) that no
    longer parses or writes a key twice while the ref's version is sound (an i18n catalog merged by hand,
    #11986), a JSON content that drops a key both sides hold (a stale copy of a catalog, #1311), or a Python file
    (a ``keep: both`` union, a hand merge) that no longer parses while both sides do (#12179), is refused with
    the path, the line and the reason named.
    """
    from app.services.sandbox_workspace import structured
    from app.services.sandbox_workspace.jsonmerge import dropped_keys
    from app.services.sandbox_workspace.rebase import has_conflict_markers

    broken: list[str] = []
    for path, data in sorted(written.items()):
        if has_conflict_markers(data):
            broken.append(f"{path} (it still holds a conflict hunk between <<<<<<< and >>>>>>> markers)")
            continue
        ref = ref_versions.get(path)
        kind = structured.structured_kind(path)
        if kind and ref is not None:
            ref_text, text = ref.decode("utf-8", "replace"), data.decode("utf-8", "replace")
            why = "" if structured.parse_defect(kind, ref_text) else structured.parse_defect(kind, text)
            if why:
                broken.append(f"{path} (the ref's version is valid {kind}, the resolved content does not parse: {why})")
                continue
            branch = (branch_versions or {}).get(path)
            if kind == structured.JSON and branch is not None:
                # A merge keeps every key both sides hold; a resolution that drops one was written from a stale
                # copy of a catalog: refused, the keys named.
                lost = dropped_keys(text, ref_text, branch.decode("utf-8", "replace"))
                if lost:
                    shown = ", ".join(lost[:10]) + (f" and {len(lost) - 10} more" if len(lost) > 10 else "")
                    broken.append(
                        f"{path} (the resolved content drops {len(lost)} key(s) both sides hold: {shown}; "
                        "keep them, or remove them in a commit of their own after the merge)"
                    )
            continue
        source = structured.resolution_kind(path)
        if source and not kind and (ref is not None or (branch_versions or {}).get(path) is not None):
            sides = [ref, (branch_versions or {}).get(path)]
            texts = [side.decode("utf-8", "replace") for side in sides if side is not None]
            why = structured.resolution_defect(path, data.decode("utf-8", "replace"), *texts)
            if why:
                broken.append(f"{path} (both sides are valid {source}, the resolved content is {why})")
    if not broken:
        return None
    return (
        f"Error: {len(broken)} resolution{'s' if len(broken) != 1 else ''} would break {'their' if len(broken) != 1 else 'its'} "
        f"file, so nothing was written: {_listed(broken)}. Fix the content and call again."
    )


async def _move_target(
    client: Any, headers: dict[str, str], repo: str, repo_url: str, branch: str, before: str, last: str
) -> str | None:
    """Fast-forward *branch* from *before* to *last* once; None when done, else why it was not moved."""
    now = await _branch_tip(client, headers, repo, repo_url, branch)
    if now != before:
        return f"'{branch}' now points at {now[:12] or 'an unreadable commit'}, not at {before[:12]}"
    _, err = await _common.safe_request(
        client,
        "patch",
        f"{repo_url}/git/refs/heads/{_enc(branch)}",
        _PROVIDER,
        f"move branch {branch} of {repo} to the resolved merge",
        headers=_json_headers(headers),
        json={"sha": last, "force": False},
    )
    return err


def _unsettled(lead: str, both: list[str], resolutions: dict[str, Resolution]) -> str | None:
    """Why *resolutions* do not settle the paths changed on both sides, or ``None`` when they do."""
    missing = [path for path in both if path not in resolutions]
    unknown = sorted(set(resolutions) - set(both))
    if not missing and not unknown:
        return None
    lines = [f"Error: {lead}, and the resolutions do not settle it, so nothing was written."]
    if missing:
        lines.append(f"Changed on both sides with no resolution ({len(missing)}): {_listed(missing)}.")
    if unknown:
        lines.append(f"Resolutions of paths not changed on both sides ({len(unknown)}): {_listed(unknown)}.")
    lines.append("Give exactly one resolution per path changed on both sides.")
    return "\n".join(lines)


async def resolve_and_merge(
    client: Any,
    headers: dict[str, str],
    repo: str,
    repo_url: str,
    branch: str,
    head: str,
    message: str,
    both: list[str],
    resolutions: dict[str, Resolution],
) -> str:
    """Land a conflicting merge on a scratch branch, then move *branch* once to its last commit.

    The alignment, the merge and the resolution chunks are written on ``tmp/commit-batch/...`` made at the
    branch's tip, so *branch* never points at an intermediate commit: no push event, CI run or stacked pull
    request sees the ref's side of a conflicting file or a split resolution at ``(k/n)``. The branch is then
    fast-forwarded once, only while it still points at the tip read; any failure leaves it unchanged.
    """
    # Resolutions that cannot settle the conflict are refused before any write, the scratch branch included.
    unsettled = _unsettled(f"Merging '{head}' into '{branch}' of {repo} conflicts", both, resolutions)
    if unsettled:
        return unsettled
    before = await _branch_tip(client, headers, repo, repo_url, branch)
    if not before:
        return f"Error: the tip of '{branch}' of {repo} could not be read, so nothing was written."
    now = time.time()
    await _sweep_stale_scratch_branches(client, headers, repo_url, repo, now)
    scratch = _scratch_branch_name(before, now)
    _, err = await _common.safe_request(
        client,
        "post",
        f"{repo_url}/git/refs",
        _PROVIDER,
        f"create scratch branch {scratch} in {repo}",
        headers=_json_headers(headers),
        json={"ref": f"refs/heads/{scratch}", "sha": before},
    )
    if err:
        return f"{err}\nThe scratch branch the merge is built on could not be made, so nothing was written."
    keep_scratch = False
    try:
        answer = await _resolve_and_merge_on(
            client, headers, repo, repo_url, scratch, head, message, both, resolutions, target=branch
        )
        # The steps name the branch they wrote on: the scratch one, never seen by the caller.
        answer = answer.replace(scratch, branch)
        last = final_head_of(answer)
        if not answer.startswith("Merged") or not last:
            # The helpers name the branch they wrote on: the scratch stands for the target in the answer.
            return (
                f"{answer.replace(scratch, branch)}\nAll of the above happened on a scratch branch made at "
                f"{before[:12]}, now deleted: '{branch}' itself was never moved and is unchanged."
            )
        why = await _move_target(client, headers, repo, repo_url, branch, before, last)
        if why is not None:
            keep_scratch = True
            return (
                f"Error: the merge was built on scratch branch '{scratch}' (last commit {last}), but '{branch}' "
                f"was not moved to it and is unchanged: {why}. A pull request can take the scratch branch; the "
                f"next split commit on {repo} deletes it once it is a day old."
            )
        moved = await _settled_tip(client, headers, repo, repo_url, branch, last)
        body, sep, tail = answer.rpartition("\nFinal head: ")
        note = (
            f"\nBuilt on a scratch branch, then '{branch}' moved once from {before[:12]} to {last[:12]}: it never "
            "pointed at an intermediate commit."
        )
        if moved != last:
            note += f" Read back: '{branch}' points at {moved[:12] or 'an unreadable commit'}."
        return f"{body}{note}{sep}{tail}" if sep else answer + note
    finally:
        if not keep_scratch:
            await _common.safe_request(
                client,
                "delete",
                f"{repo_url}/git/refs/heads/{_enc(scratch)}",
                _PROVIDER,
                f"delete scratch branch {scratch} of {repo}",
                headers=headers,
            )


async def _resolve_and_merge_on(
    client: Any,
    headers: dict[str, str],
    repo: str,
    repo_url: str,
    branch: str,
    head: str,
    message: str,
    both: list[str],
    resolutions: dict[str, Resolution],
    *,
    target: str = "",
) -> str:
    """Land a conflicting merge with its *resolutions* in signed commits split under the cap on *branch*.

    *branch* is the scratch branch :func:`resolve_and_merge` made; *target* names the branch it stands for in
    the answer.
    """
    shown = target or branch
    lead = f"Merging '{head}' into '{shown}' of {repo} conflicts"
    unsettled = _unsettled(lead, both, resolutions)
    if unsettled:
        return unsettled
    ours = await _read_side(client, headers, repo, repo_url, branch)
    if isinstance(ours, str):
        return f"{ours}\nNothing was written."
    theirs = await _read_side(client, headers, repo, repo_url, head)
    if isinstance(theirs, str):
        return f"{theirs}\nNothing was written."
    for path in sorted(resolutions):
        refusal = _mode_refusal(path, ((shown, ours.entries.get(path)), (head, theirs.entries.get(path))))
        if refusal:
            return refusal
    written: dict[str, bytes] = {}
    workspace = None
    for path, resolution in resolutions.items():
        if resolution.content is not None:
            written[path] = resolution.content.encode("utf-8")
        elif resolution.workspace_path:
            workspace = workspace or current_workspace()
            if workspace is None:
                return "Error: a 'workspace_path' resolution needs the sandbox workspace of this run, and it has none."
            try:
                _rel, _name, data = await workspace.read_file(resolution.workspace_path, tool="merge_github_branch")
            except ValueError as exc:
                return f"Error: the resolution of '{path}' could not read {resolution.workspace_path}: {exc}"
            written[path] = data
    # Step 1 aligns every resolved path the two sides hold differently on the ref.
    aligned = sorted(path for path in resolutions if ours.entries.get(path) != theirs.entries.get(path))
    # Step 3 writes every path not resolved to the ref's version that the merge leaves different.
    final_paths: list[str] = []
    final_sizes: dict[str, int] = {}
    for path, resolution in sorted(resolutions.items()):
        current = theirs.entries.get(path)
        if resolution.keep == "ref":
            continue
        if resolution.keep == "branch":
            entry = ours.entries.get(path)
            if entry == current:
                continue
            final_sizes[path] = entry.size if entry is not None else 0
        else:
            data = written[path]
            if current is not None and _git_blob_id(data) == current.sha:
                continue
            final_sizes[path] = len(data)
        final_paths.append(path)
    # The commits are sized from the tree listings before any blob is read: a file over the cap on its own is
    # refused, everything else is split into as many signed commits as it needs.
    to_ref_sizes = {path: entry.size if (entry := theirs.entries.get(path)) is not None else 0 for path in aligned}
    back_sizes = {path: entry.size if (entry := ours.entries.get(path)) is not None else 0 for path in aligned}
    for what, sizes in ((_TO_REF, to_ref_sizes), (_BACK, back_sizes), (_FINAL, final_sizes)):
        refusal = _single_over_cap(what, sizes)
        if refusal:
            return refusal
    listed_read = sum(to_ref_sizes.values()) + sum(back_sizes.values())
    if listed_read > _MAX_READ_BYTES:
        return _over_read_budget(listed_read, to_ref_sizes)
    # Only the two versions of the paths the sides hold differently are read, within the read budget.
    budget = _MAX_READ_BYTES
    blobs: dict[str, bytes] = {}
    for path in aligned:
        for entry in (ours.entries.get(path), theirs.entries.get(path)):
            if entry is None or entry.sha in blobs:
                continue
            data = await _read_blob(client, headers, repo, repo_url, entry.sha, path)
            if isinstance(data, str):
                return f"{data}\nNothing was written."
            budget -= len(data)
            if budget < 0:
                return _over_read_budget(_MAX_READ_BYTES - budget, to_ref_sizes)
            blobs[entry.sha] = data

    def version(side: _Side, path: str) -> bytes | None:
        entry = side.entries.get(path)
        return None if entry is None else blobs[entry.sha]

    to_ref: dict[str, bytes | None] = {path: version(theirs, path) for path in aligned}
    back: dict[str, bytes | None] = {path: version(ours, path) for path in aligned}
    final: dict[str, bytes | None] = {
        path: version(ours, path) if resolutions[path].keep == "branch" else written[path] for path in final_paths
    }
    # The listing may omit a size: the payloads themselves are checked again before the first write.
    for what, payload in ((_TO_REF, to_ref), (_BACK, back), (_FINAL, final)):
        refusal = _single_over_cap(what, {path: len(data) for path, data in payload.items() if data is not None})
        if refusal:
            return refusal
    broken = _broken_resolutions(
        written,
        {path: version(theirs, path) for path in aligned},
        {path: version(ours, path) for path in aligned},
    )
    if broken:
        return broken
    lines: list[str] = []
    tip = ours.commit
    if aligned:
        chain = await _signed_commits(
            client, headers, repo, repo_url, branch, tip,
            f"Take {head}'s version of {len(aligned)} conflicting file{'s' if len(aligned) != 1 else ''} before the merge",
            to_ref,
        )
        if chain.failure is not None:
            if not chain.commits and not isinstance(chain.failure, _Lost):
                now = await _already_merged(client, headers, repo, repo_url, branch, theirs.commit)
                if now:
                    return (
                        f"No change: '{branch}' of {repo} already contains '{head}' at {theirs.commit[:12]}; its head "
                        f"is {now}. The merge landed while this call ran (an earlier or replayed call), so this "
                        "call wrote nothing more. Read the resolved files on the branch if they matter."
                    )
            return await _alignment_failed(client, headers, repo, repo_url, branch, chain, back, ours.commit, shown)
        tip = chain.tip
        lines += _chain_lines("1", chain, f"'{head}' versions of")
    # The ref is read again: a ref that moved since the snapshot is not merged from the snapshot.
    source_now = await _branch_tip(client, headers, repo, repo_url, head)
    if source_now != theirs.commit:
        moved = (
            f"'{head}' now points at {source_now[:12]}, not at {theirs.commit[:12]} {_REF_MOVED}"
            if source_now
            else f"'{head}' could not be read again to check it still points at {theirs.commit[:12]}"
        )
        status: int | None = None
        err: str | None = moved
        resp: Any = None
    else:
        # The merge is pinned to the commit read above: a ref moved meanwhile cannot pass for a resolved merge.
        resp, err = await _common.safe_request(
            client, "post", f"{repo_url}/merges", _PROVIDER, f"merge {head} into {branch} in {repo}",
            headers=_json_headers(headers), json={"base": branch, "head": theirs.commit, "commit_message": message},
            expected_statuses={409},
        )
        status = resp.status_code if resp is not None else None
        if status is None or status >= 500:
            # A lost merge answer: the merge may have landed, so the branch tip decides.
            now = await _branch_tip(client, headers, repo, repo_url, branch)
            if now != tip:
                where = f"now points at {now[:12]}" if now else "could not be read again"
                return (
                    f"Error: {lead}: {err or f'GitHub answered {status}'}. Whether the merge of {theirs.commit[:12]} "
                    f"landed is not known: '{branch}' {where} (it pointed at {tip[:12]}), so the resolved content "
                    "was not written."
                    + (f"\nAlready on '{branch}':\n" + "\n".join(lines) if lines else "")
                    + "\nRead the branch with list_github_commits and compare_github_commits before calling this "
                    "tool again."
                )
    done = "\n".join(lines)
    if status == 204 and not done:
        # Nothing was written by this call and the branch holds the ref commit: the merge asked for is there.
        now = await _branch_tip(client, headers, repo, repo_url, branch)
        return (
            f"No change: '{branch}' of {repo} already contains '{head}' at {theirs.commit[:12]}"
            + (f"; its head is {now}." if now else ".")
            + " The merge landed while this call ran (an earlier or replayed call), so this call wrote nothing more."
        )
    if status == 204:
        # The branch already held the ref when the take commits landed on it: no merge follows them, so they would
        # only replace the branch's own files with the ref's versions. They are undone before the answer.
        undo = await _undo_alignment(client, headers, repo, repo_url, branch, ours.commit, tip, back, shown)
        return (
            f"Error: {lead}: GitHub answered that '{branch}' already contains {theirs.commit[:12]} ('{head}' when it "
            f"was read), so no merge was needed and the resolved content was not written. {undo}"
            + "\nRead the branch with list_github_commits and compare_github_commits before calling this tool again."
        )
    if status not in (201, 204):
        why = "GitHub still reports a conflict" if status == 409 else (err or f"GitHub answered {status}")
        if not aligned:
            return f"Error: {lead}: {why}. Nothing was written."
        refused = await _restore_head(client, headers, repo, repo_url, branch, ours.commit, tip)
        if refused is None:
            hint = (
                " Read compare_github_commits to find the paths the compare did not list, then call this tool again "
                "with their resolutions."
                if status == 409
                else " Call this tool again."
            )
            return (
                f"Error: {lead}: {why}. '{branch}' was moved back to {ours.commit[:12]}, its head before this call, "
                f"so the alignment commits left its history.{hint}"
            )
        why = f"{why}; the ref update back to {ours.commit[:12]} was refused ({refused})"
        undone = await _signed_commits(
            client, headers, repo, repo_url, branch, tip,
            f"Restore {shown}'s version of the files a refused merge needed", back,
        )
        if undone.failure is not None:
            how = "answered nothing" if isinstance(undone.failure, _Lost) else "could not be finished"
            restored = f" ({_listed(undone.paths)} restored)" if undone.commits else ""
            return (
                f"Error: {lead}: {why}. The ref's versions of {_listed(aligned)} are on '{branch}' (tip {tip[:12]}), "
                f"and their undo{restored} {how}: {_failure_text(undone.failure)}"
            )
        by = ", ".join(f"{oid[:12]} ({word})" for oid, word, _ in undone.commits)
        return (
            f"Error: {lead}: {why}, on paths the compare did not list. Commit {tip[:12]} was undone by "
            f"{by}: '{branch}' holds its own versions again. Read compare_github_commits "
            f"to find the other paths, then call this tool again with their resolutions."
            if status == 409
            else f"Error: {lead}: {why}. Commit {tip[:12]} was undone by {by}: '{branch}' "
            "holds its own versions again. Call this tool again."
        )
    created = _json_object(resp)
    merged = _coerce_str(created.get("sha"))
    parents = created.get("parents")
    first = parents[0] if isinstance(parents, list) and parents and isinstance(parents[0], dict) else {}
    on_top_of = _coerce_str(first.get("sha"))
    if not _GIT_OBJECT_ID_RE.fullmatch(merged) or on_top_of != tip:
        return (
            f"Error: {lead}: the merge landed as {merged[:12] or 'a commit GitHub did not name'} on top of "
            f"{on_top_of[:12] or 'a commit GitHub did not name'}, not on {tip[:12]}: '{branch}' moved meanwhile, "
            "so the resolved content was not written."
            + (f"\nAlready on '{branch}':\n{done}" if done else "")
            + "\nRead the branch with list_github_commits and compare_github_commits before calling this tool again."
        )
    tip = merged
    merge_commit = created.get("commit")
    verification = merge_commit.get("verification") if isinstance(merge_commit, dict) else None
    verified = isinstance(verification, dict) and verification.get("verified") is True
    lines.append(
        f"2. {tip[:12]} ({'verified' if verified else 'built by GitHub'}): the merge of '{head}' at {theirs.commit[:12]}."
    )
    if final:
        changed = sorted(final)
        chain = await _signed_commits(
            client, headers, repo, repo_url, branch, tip,
            f"Resolve the merge of {head}: {len(changed)} file{'s' if len(changed) != 1 else ''}", final,
        )
        lines += _chain_lines("3", chain, "the resolved content of")
        if chain.failure is not None:
            left = sorted(set(changed) - set(chain.paths))
            return (
                f"Error: {lead}: the merge landed, but the resolution stopped with {len(left)} of {len(changed)} "
                f"resolved file{'s' if len(changed) != 1 else ''} not written, so '{branch}' holds the ref's version "
                f"of {_all_listed(left)}. {_failure_text(chain.failure)}\nCall this tool again with the same "
                "resolutions: it finds the merge on the branch and writes the paths still missing.\nAlready on "
                f"'{branch}':\n" + "\n".join(lines) + f"\n{final_head_line(chain.tip)}"
            )
        # The last resolution commit is read by its id, not through the branch name: right after a GraphQL commit
        # the branch can still read as the merge commit for a moment, which held the ref's versions and made a
        # correct resolution look missing.
        checked = await _tip_mismatch(client, headers, repo, repo_url, chain.tip, final)
        if isinstance(checked, list) and checked:
            return (
                f"Error: {lead}: every resolution commit landed, but {chain.tip[:12]}, the last of them, does not "
                f"hold the resolved content of {_mismatch_text(checked)} (a commit GitHub dropped). Read those files "
                f"and write them with create_github_commit.\nAlready on '{branch}':\n" + "\n".join(lines)
                + f"\n{final_head_line(chain.tip)}"
            )
        lines.append(
            f"Checked on {chain.tip[:12]}: the {len(changed)} resolved file{'s' if len(changed) != 1 else ''} "
            "hold the resolved content."
            if not isinstance(checked, str)
            else f"{chain.tip[:12]} could not be read to check the resolved files: {checked}"
        )
        now = await _settled_tip(client, headers, repo, repo_url, branch, chain.tip)
        if now != chain.tip:
            where = f"now points at {now[:12]}" if now else "could not be read again"
            lines.append(
                f"'{branch}' {where}, not at {chain.tip[:12]}: another writer pushed after the resolution, or "
                "GitHub still serves an older tip. Read the branch before writing on it."
            )
        tip = chain.tip
    kept = sorted(path for path, resolution in resolutions.items() if resolution.keep == "ref")
    if kept:
        lines.append(f"Kept as on '{head}': {_listed(kept)}.")
    lines.append(final_head_line(tip))
    return (
        f"Merged '{head}' into '{shown}' of {repo} with its conflicts resolved, in commits GitHub built:\n"
        + "\n".join(lines)
    )
