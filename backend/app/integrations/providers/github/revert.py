"""``revert_github_commit``: one commit that undoes another, whatever its size.

``create_github_commit`` carries a change set the caller spells out, held to
a file count and a content size because every file crosses the model's
context. A revert needs neither: the parent commit already holds every
version to restore. This tool reads the three trees involved - the parent
(what to restore), the reverted commit (what it changed) and the branch tip
(what is there now) - as recursive listings, and builds ONE tree on top of
the tip that points at the parent's objects by id. No content is downloaded
or uploaded, so a revert of 400 files costs six requests, and the branch
moves once.

A path the branch changed again since the reverted commit is a CONFLICT, as
``git revert`` reports one: the tool refuses the whole revert and names every
conflicting path with its three versions (the parent's, the reverted
commit's and the branch's object ids), which ``get_github_file`` reads at
those refs. The caller then passes ``resolutions``: keep the branch's
version, restore the parent's, delete the path, or write a merged content
(plain text or a file of the conversation). Nothing is created until every
conflict has a resolution.

The commit is SIGNED whenever it can be: when every path the revert writes is
a plain file (no executable bit, symbolic link or submodule on either side),
the change set is within the bounds of one ``createCommitOnBranch`` (100
paths, 5 MB of content, sized from the tree listings before any blob is
read), the restored blobs are read and GitHub builds and signs the commit,
chained on the tip with ``expectedHeadOid``. A new branch is created at the
base tip first, so the signed commit lands on it. Only a revert outside those
bounds goes through the Git Data API, which GitHub leaves unsigned for a
user credential: the result says why and gives GitHub's verdict, and on an
existing branch with an open pull request it is refused before anything is
written unless ``allow_unsigned`` is true, because one unsigned commit keeps
a signed-commits check red on every later head of that pull request.
"""

from __future__ import annotations

import base64
import json
from typing import Any, NamedTuple

from app.core.egress import egress_client
from app.integrations.file_delivery import read_pipe_file
from app.tools.base import CallableToolExecutor, FileContext, ToolDefinition, ToolResult

from . import _common
from ._common import _BASE_URL, _PROVIDER, _coerce_str, _enc, _json_headers, parse_repo
from .contents import (
    _GIT_OBJECT_ID_RE,
    _MAX_COMMIT_CHANGES,
    _MAX_COMMIT_CONTENT_BYTES,
    _MODE_FILE,
    _branch_name_error,
    _commit_path,
    _git_data_signing_note,
    _json_object,
    _moved_tip_refusal,
    _resolve_branch,
    _rest_signature_verdict,
)
from .merge_resolution import _Lost, _read_blob, _signed_commit

_SOURCE = "integration:github"

#: Conflicts named in a refusal; the rest are counted.
_MAX_LISTED_CONFLICTS = 40
#: Paths named per section of the result.
_MAX_LISTED_PATHS = 20
#: Bytes one resolution may write (a merged file is text a person reviews).
_MAX_RESOLUTION_BYTES = 5 * 1024 * 1024
#: Changes one revert may carry: the tree API takes the whole list in one
#: request, so the ceiling only guards against a reverted commit that
#: rewrote a repository.
_MAX_REVERT_CHANGES = 5000

_MODE_SUBMODULE = "160000"
_KEEP_CHOICES = ("branch", "parent", "delete")


class _Entry(NamedTuple):
    """One path of a tree: its mode and object id."""

    mode: str
    sha: str


def _default_branch_name(sha: str) -> str:
    return f"revert-{sha[:8]}"


_KINDS = {_MODE_SUBMODULE: "submodule", "100755": "executable", "120000": "symlink"}


def _short(entry: _Entry | None) -> str:
    """One version of a path in a conflict line: its object id and what it is."""
    if entry is None:
        return "absent"
    return f"{entry.sha[:8]} ({_KINDS.get(entry.mode, 'file')})"


def _listed(label: str, paths: list[str]) -> str:
    shown = ", ".join(paths[:_MAX_LISTED_PATHS])
    more = len(paths) - _MAX_LISTED_PATHS
    return f"{label}: {shown}" + (f" (and {more} more)" if more > 0 else "")


def _parse_resolutions(raw: Any) -> tuple[dict[str, dict[str, Any]], str | None]:
    """``{path: resolution}`` from the ``resolutions`` argument, or an error."""
    if raw is None or raw == "":
        return {}, None
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return {}, "Error: 'resolutions' must be a list."
    if not isinstance(raw, list):
        return {}, "Error: 'resolutions' must be a list."
    parsed: dict[str, dict[str, Any]] = {}
    for item in raw:
        if not isinstance(item, dict):
            return {}, "Error: each resolution is an object with a 'path' and one of 'keep', 'content' or 'file_id'."
        path, err = _commit_path(item.get("path"), "resolutions")
        if err:
            return {}, err
        keep = item.get("keep")
        content = item.get("content")
        file_id = _coerce_str(item.get("file_id")).strip()
        ways = sum((keep is not None, isinstance(content, str), bool(file_id)))
        if ways != 1:
            return {}, (
                f"Error: the resolution of '{path}' needs exactly one of 'keep' ({' / '.join(_KEEP_CHOICES)}), "
                "'content' (the merged text) or 'file_id' (a file of this conversation)."
            )
        if keep is not None and keep not in _KEEP_CHOICES:
            return {}, f"Error: 'keep' for '{path}' must be one of {', '.join(_KEEP_CHOICES)}."
        if path in parsed:
            return {}, f"Error: '{path}' is resolved more than once."
        parsed[path] = {"keep": keep, "content": content, "file_id": file_id or None}
    return parsed, None


def register(
    headers: dict[str, str],
    executor: CallableToolExecutor,
    _mark_self_action: Any,
) -> list[ToolDefinition]:
    """Build the revert tool and register its callable.

    The self-action recorder every module takes is for issue and pull request
    numbers; a commit has none, so it is unused here.
    """

    async def revert_github_commit(
        repo: str,
        sha: str,
        branch: str = "",
        base_branch: str = "",
        message: str = "",
        resolutions: list[dict[str, Any]] | None = None,
        parent: int = 1,
        allow_unsigned: bool = False,
        file_context: FileContext | None = None,
    ) -> str | ToolResult:
        """Undo one commit on a new branch (or on an existing one) as a single commit."""
        owner, name, rerr = parse_repo(repo)
        if rerr:
            return rerr
        sha = _coerce_str(sha).strip().lower()
        if not _GIT_OBJECT_ID_RE.fullmatch(sha):
            return "Error: 'sha' must be the full object id of the commit to revert (40 hexadecimal characters)."
        if not isinstance(parent, int) or isinstance(parent, bool) or parent < 1:
            return "Error: 'parent' is the 1-based number of the parent to revert to (1 unless the commit is a merge)."
        resolved, err = _parse_resolutions(resolutions)
        if err:
            return err
        repo_url = f"{_BASE_URL}/repos/{owner}/{name}"
        explicit_base = bool(_coerce_str(base_branch).strip())
        async with egress_client(timeout=120) as client:
            base_branch, branch_err = await _resolve_branch(client, headers, repo, owner, name, base_branch)
            if branch_err:
                return branch_err
            target = _coerce_str(branch).strip() or _default_branch_name(sha)
            for candidate in (base_branch, target):
                branch_err = _branch_name_error(candidate)
                if branch_err:
                    return branch_err
            # Committing on an existing branch takes both names, said: 'branch'
            # alone naming the default branch never moves it.
            same_branch = explicit_base and target == base_branch

            # -- The commit to revert and its parent ----------------------------
            resp, err = await _common.safe_request(
                client, "get", f"{repo_url}/git/commits/{sha}", _PROVIDER, f"read commit {sha[:8]} of {repo}",
                headers=headers, expected_statuses={404, 422},
            )
            if err:
                if resp is not None and resp.status_code in (404, 422):
                    return f"Error: commit {sha[:8]} does not exist in {repo}."
                return err
            reverted = _json_object(resp)
            parents = [p.get("sha") for p in reverted.get("parents") or [] if isinstance(p, dict)]
            parents = [p for p in parents if isinstance(p, str) and _GIT_OBJECT_ID_RE.fullmatch(p)]
            if not parents:
                return (
                    f"Error: commit {sha[:8]} of {repo} has no parent (it is the root commit), so it cannot be "
                    "reverted."
                )
            if parent > len(parents):
                return (
                    f"Error: commit {sha[:8]} of {repo} has {len(parents)} parent(s); 'parent' must be between 1 and "
                    f"{len(parents)}."
                )
            parent_sha = parents[parent - 1]
            reverted_tree = _coerce_str(reverted["tree"].get("sha")) if isinstance(reverted.get("tree"), dict) else ""
            message_lines = _coerce_str(reverted.get("message")).splitlines()
            subject = message_lines[0] if message_lines else sha[:8]
            resp, err = await _common.safe_request(
                client, "get", f"{repo_url}/git/commits/{parent_sha}", _PROVIDER,
                f"read commit {parent_sha[:8]} of {repo}", headers=headers,
            )
            if err:
                return err
            parent_obj = _json_object(resp)
            parent_tree = _coerce_str(parent_obj["tree"].get("sha")) if isinstance(parent_obj.get("tree"), dict) else ""
            if not (_GIT_OBJECT_ID_RE.fullmatch(reverted_tree) and _GIT_OBJECT_ID_RE.fullmatch(parent_tree)):
                return f"Error: GitHub did not return the trees of commit {sha[:8]} and its parent in {repo}."

            # -- The branch tip ---------------------------------------------------
            resp, err = await _common.safe_request(
                client, "get", f"{repo_url}/git/ref/heads/{_enc(base_branch)}", _PROVIDER,
                f"read branch {base_branch} of {repo}", headers=headers, expected_statuses={404},
            )
            if err:
                if resp is not None and resp.status_code == 404:
                    return f"Error: branch '{base_branch}' does not exist in {repo}."
                return err
            ref_object = _json_object(resp).get("object")
            tip = _coerce_str(ref_object.get("sha")) if isinstance(ref_object, dict) else ""
            if not _GIT_OBJECT_ID_RE.fullmatch(tip):
                return f"Error: GitHub did not return the tip commit of branch '{base_branch}' in {repo}."
            resp, err = await _common.safe_request(
                client, "get", f"{repo_url}/git/commits/{tip}", _PROVIDER, f"read commit {tip[:8]} of {repo}",
                headers=headers,
            )
            if err:
                return err
            tip_obj = _json_object(resp)
            tip_tree = _coerce_str(tip_obj["tree"].get("sha")) if isinstance(tip_obj.get("tree"), dict) else ""
            if not _GIT_OBJECT_ID_RE.fullmatch(tip_tree):
                return f"Error: GitHub did not return the tree of commit {tip[:8]} in {repo}."
            if not same_branch:
                resp, err = await _common.safe_request(
                    client, "get", f"{repo_url}/git/ref/heads/{_enc(target)}", _PROVIDER,
                    f"check branch {target} of {repo}", headers=headers, expected_statuses={404},
                )
                if not err:
                    return (
                        f"Error: branch '{target}' already exists in {repo}. Give another 'branch' name, or name it "
                        "as 'base_branch' too to commit the revert on top of it."
                    )
                if resp is None or resp.status_code != 404:
                    return err

            # -- The three trees --------------------------------------------------
            async def tree_of(tree_sha: str, what: str) -> tuple[dict[str, _Entry], str | None]:
                resp, err = await _common.safe_request(
                    client, "get", f"{repo_url}/git/trees/{tree_sha}", _PROVIDER, f"read the {what} tree of {repo}",
                    headers=headers, params={"recursive": "1"},
                )
                if err:
                    return {}, err
                data = _json_object(resp)
                if data.get("truncated") or not isinstance(data.get("tree"), list):
                    return {}, (
                        f"Error: GitHub did not list the {what} tree of {repo} in full (it is over the listing "
                        "limit), so the revert cannot be computed here; nothing was created."
                    )
                entries: dict[str, _Entry] = {}
                for item in data["tree"]:
                    if not isinstance(item, dict) or item.get("type") == "tree":
                        continue
                    path, mode, oid = item.get("path"), _coerce_str(item.get("mode")), _coerce_str(item.get("sha"))
                    if isinstance(path, str) and mode and _GIT_OBJECT_ID_RE.fullmatch(oid):
                        entries[path] = _Entry(mode, oid)
                return entries, None

            parent_entries, err = await tree_of(parent_tree, "parent")
            if err:
                return err
            reverted_entries, err = await tree_of(reverted_tree, "reverted commit's")
            if err:
                return err
            tip_entries, err = await tree_of(tip_tree, "branch")
            if err:
                return err

            changed = sorted(
                path for path in set(parent_entries) | set(reverted_entries)
                if parent_entries.get(path) != reverted_entries.get(path)
            )
            if not changed:
                return ToolResult(
                    tool_call_id="",
                    content=(
                        f"No change: commit {sha[:8]} of {repo} changed no file against its parent, so there is "
                        "nothing to revert."
                    ),
                    no_op=True,
                )
            if len(changed) > _MAX_REVERT_CHANGES:
                return (
                    f"Error: commit {sha[:8]} of {repo} changed {len(changed)} paths, over the {_MAX_REVERT_CHANGES} "
                    "one revert may carry; nothing was created."
                )

            tree_entries: list[dict[str, Any]] = []
            restored: list[str] = []
            deleted: list[str] = []
            already: list[str] = []
            conflicts: list[tuple[str, _Entry | None, _Entry | None, _Entry | None]] = []
            kept: list[str] = []
            merged: list[str] = []
            merged_raw: dict[str, bytes] = {}
            unused = set(resolved)

            def restore(path: str, want: _Entry | None) -> None:
                if want is None:
                    tree_entries.append({"path": path, "mode": _MODE_FILE, "type": "blob", "sha": None})
                    deleted.append(path)
                else:
                    kind = "commit" if want.mode == _MODE_SUBMODULE else "blob"
                    tree_entries.append({"path": path, "mode": want.mode, "type": kind, "sha": want.sha})
                    restored.append(path)

            for path in changed:
                before, after, now = parent_entries.get(path), reverted_entries.get(path), tip_entries.get(path)
                resolution = resolved.get(path)
                if resolution is not None:
                    unused.discard(path)
                    keep = resolution["keep"]
                    if keep == "branch":
                        kept.append(path)
                    elif keep == "parent":
                        restore(path, before)
                    elif keep == "delete":
                        if now is not None:
                            tree_entries.append({"path": path, "mode": _MODE_FILE, "type": "blob", "sha": None})
                        deleted.append(path)
                    else:
                        content = resolution["content"]
                        if resolution["file_id"]:
                            piped = await read_pipe_file(
                                file_context,
                                resolution["file_id"],
                                max_bytes=_MAX_RESOLUTION_BYTES,
                                target="a GitHub commit",
                            )
                            if isinstance(piped, str):
                                return piped
                            content = piped[0]
                        raw = content if isinstance(content, bytes) else content.encode("utf-8")
                        if len(raw) > _MAX_RESOLUTION_BYTES:
                            return (
                                f"Error: the resolution of '{path}' is over {_MAX_RESOLUTION_BYTES} bytes; "
                                "nothing was created."
                            )
                        resp, err = await _common.safe_request(
                            client, "post", f"{repo_url}/git/blobs", _PROVIDER, f"upload '{path}' to {repo}",
                            headers=_json_headers(headers),
                            json={"content": base64.b64encode(raw).decode("ascii"), "encoding": "base64"},
                        )
                        if err:
                            return f"{err}\nNothing was created in {repo}."
                        blob = _coerce_str(_json_object(resp).get("sha"))
                        if not _GIT_OBJECT_ID_RE.fullmatch(blob):
                            return f"Error: GitHub did not return the uploaded '{path}' in {repo}; nothing was created."
                        mode = now.mode if now is not None and now.mode in (_MODE_FILE, "100755") else _MODE_FILE
                        tree_entries.append({"path": path, "mode": mode, "type": "blob", "sha": blob})
                        merged.append(path)
                        merged_raw[path] = raw
                    continue
                if now == after:
                    restore(path, before)
                elif now == before:
                    already.append(path)
                else:
                    conflicts.append((path, before, after, now))
            # A directory the parent held where the branch now has a file (or
            # the reverse) cannot be expressed in one tree: say so as a conflict.
            tip_dirs = {p.rsplit("/", 1)[0] for p in tip_entries if "/" in p}
            for entry in list(tree_entries):
                path = entry["path"]
                if entry.get("sha") is not None and path in tip_dirs and path not in resolved:
                    conflicts.append((path, parent_entries.get(path), reverted_entries.get(path), None))
                    tree_entries.remove(entry)
            if unused:
                names = ", ".join(sorted(unused)[:_MAX_LISTED_PATHS])
                return (
                    f"Error: the resolutions name paths commit {sha[:8]} did not change: {names}. Only a path the "
                    "commit changed can be resolved; nothing was created."
                )
            if conflicts:
                lines = [
                    f"Error: {len(conflicts)} path(s) changed on branch '{base_branch}' of {repo} since commit "
                    f"{sha[:8]}, so the revert conflicts and nothing was created. For each one, read the versions "
                    f"with get_github_file at refs {parent_sha[:8]} (parent), {sha[:8]} (reverted) and "
                    f"{base_branch} (branch), then call the tool again with a resolution per path: keep "
                    "'branch', keep 'parent', 'delete', or the merged 'content' / 'file_id'."
                ]
                for path, before, after, now in conflicts[:_MAX_LISTED_CONFLICTS]:
                    lines.append(f"- {path}: parent {_short(before)}, reverted {_short(after)}, branch {_short(now)}")
                if len(conflicts) > _MAX_LISTED_CONFLICTS:
                    lines.append(f"- and {len(conflicts) - _MAX_LISTED_CONFLICTS} more")
                return "\n".join(lines)
            if not tree_entries:
                return ToolResult(
                    tool_call_id="",
                    content=(
                        f"No change: branch '{base_branch}' of {repo} already has every file of commit {sha[:8]} "
                        "as its parent had it, so there is nothing to revert."
                    ),
                    no_op=True,
                )

            text = _coerce_str(message).strip() or (
                f"revert: {subject}\n\nThis reverts commit {sha}."
            )

            # -- A commit GitHub signs, whenever the revert fits one --------------
            unsigned_why = ""
            additions: dict[str, bytes] = {}
            deletions: list[str] = []
            if len(tree_entries) > _MAX_COMMIT_CHANGES:
                unsigned_why = (
                    f"the revert writes {len(tree_entries)} paths, over the {_MAX_COMMIT_CHANGES} one signed commit "
                    "may carry"
                )
            for entry in [] if unsigned_why else tree_entries:
                path = entry["path"]
                now = tip_entries.get(path)
                if entry["sha"] is None:
                    if now is not None:
                        deletions.append(path)
                    continue
                if entry["mode"] != _MODE_FILE or (now is not None and now.mode != _MODE_FILE):
                    unsigned_why = f"'{path}' is not a plain file, and a signed commit writes plain files only"
                    break
                data = merged_raw.get(path)
                if data is None:
                    read = await _read_blob(client, headers, repo, repo_url, entry["sha"], path)
                    if isinstance(read, str):
                        return f"{read}\nNothing was created in {repo}."
                    data = read
                additions[path] = data
                if sum(len(item) for item in additions.values()) > _MAX_COMMIT_CONTENT_BYTES:
                    unsigned_why = (
                        f"its content is over the {_MAX_COMMIT_CONTENT_BYTES} bytes one signed commit may carry"
                    )
                    break
            if not unsigned_why:
                return await _signed_revert(
                    client, repo, repo_url, sha, base_branch, target, same_branch, tip, text, additions, deletions,
                    (restored, deleted, merged, kept, already),
                )
            if same_branch and not allow_unsigned:
                resp, err = await _common.safe_request(
                    client, "get", f"{repo_url}/pulls", _PROVIDER, f"list the open pull requests of {target}",
                    headers=headers, params={"head": f"{owner}:{target}", "state": "open", "per_page": "1"},
                )
                if err:
                    return f"{err}\nNothing was created in {repo}."
                open_prs = resp.json() if resp is not None else []
                if isinstance(open_prs, list) and open_prs:
                    number = open_prs[0].get("number") if isinstance(open_prs[0], dict) else None
                    return (
                        f"Error: this revert cannot be signed ({unsigned_why}), and branch '{target}' of {repo} "
                        f"has an open pull request (#{number}): one unsigned commit keeps a signed-commits check "
                        "red on every later head of it. Nothing was created. Revert onto a new branch (the "
                        "default), or pass allow_unsigned: true to write it unsigned anyway."
                    )

            # -- One tree, one commit, one ref (Git Data API, unsigned) -----------
            resp, err = await _common.safe_request(
                client, "post", f"{repo_url}/git/trees", _PROVIDER, f"build the reverted tree of {repo}",
                headers=_json_headers(headers), json={"base_tree": tip_tree, "tree": tree_entries},
            )
            if err:
                return f"{err}\nNothing was created in {repo}."
            new_tree = _coerce_str(_json_object(resp).get("sha"))
            if not _GIT_OBJECT_ID_RE.fullmatch(new_tree):
                return f"Error: GitHub did not return the reverted tree for {repo}; nothing was created."
            resp, err = await _common.safe_request(
                client, "post", f"{repo_url}/git/commits", _PROVIDER, f"create the revert commit in {repo}",
                headers=_json_headers(headers), json={"message": text, "tree": new_tree, "parents": [tip]},
            )
            if err:
                return f"{err}\nNothing was created in {repo}."
            created = _json_object(resp)
            new_commit = _coerce_str(created.get("sha"))
            if not _GIT_OBJECT_ID_RE.fullmatch(new_commit):
                return f"Error: GitHub did not return the revert commit for {repo}; no branch was changed."
            reason = unsigned_why
            signing_note = _git_data_signing_note(reason, _rest_signature_verdict(created))
            resp, err = await _common.safe_request(
                client, "get", f"{repo_url}/git/ref/heads/{_enc(base_branch)}", _PROVIDER,
                f"re-read branch {base_branch} of {repo}", headers=headers,
            )
            if err:
                return f"{err}\nCommit {new_commit[:8]} was created but no branch points at it.\n{signing_note}"
            current_object = _json_object(resp).get("object")
            current = _coerce_str(current_object.get("sha")) if isinstance(current_object, dict) else ""
            if current != tip:
                return f"{_moved_tip_refusal(base_branch, repo, current)}\n{signing_note}"
            if same_branch:
                resp, err = await _common.safe_request(
                    client, "patch", f"{repo_url}/git/refs/heads/{_enc(target)}", _PROVIDER,
                    f"move branch {target} of {repo} to the revert commit", headers=_json_headers(headers),
                    json={"sha": new_commit, "force": False}, expected_statuses=set(),
                )
            else:
                resp, err = await _common.safe_request(
                    client, "post", f"{repo_url}/git/refs", _PROVIDER, f"create branch {target} in {repo}",
                    headers=_json_headers(headers), json={"ref": f"refs/heads/{target}", "sha": new_commit},
                    expected_statuses=set(),
                )
            if err:
                if resp is None or resp.status_code >= 500:
                    return (
                        f"{err}\nCommit {new_commit[:8]} was created, but whether branch '{target}' now points at it "
                        f"is not known; read the branch before calling the tool again.\n{signing_note}"
                    )
                return (
                    f"{err}\nCommit {new_commit[:8]} was created but branch '{target}' was not moved to it, so no "
                    f"branch changed. Either the branch moved meanwhile, or it is protected: revert onto a new branch "
                    f"(the default) and open a pull request.\n{signing_note}"
                )
        counts = ", ".join(
            f"{len(paths)} {label}"
            for label, paths in (
                ("restored", restored),
                ("deleted", deleted),
                ("merged", merged),
                ("kept from the branch", kept),
            )
            if paths
        )
        where = f"on new branch '{target}' (from '{base_branch}')" if not same_branch else f"on branch '{target}'"
        lines = [f"Reverted commit {sha[:8]} of {repo} {where} in one commit ({new_commit}): {counts}."]
        lines += [
            _listed(label, paths)
            for label, paths in (("Restored", restored), ("Deleted", deleted), ("Merged", merged), ("Kept", kept))
            if paths
        ]
        if already:
            lines.append(_listed("Already reverted on the branch", already))
        lines.append(signing_note)
        if not same_branch:
            lines.append(f"Open a pull request from '{target}' into '{base_branch}' with create_github_pr.")
        return "\n".join(lines)

    async def _signed_revert(
        client: Any,
        repo: str,
        repo_url: str,
        sha: str,
        base_branch: str,
        target: str,
        same_branch: bool,
        tip: str,
        text: str,
        additions: dict[str, bytes],
        deletions: list[str],
        listed: tuple[list[str], list[str], list[str], list[str], list[str]],
    ) -> str:
        """Land the revert as one commit GitHub builds and signs, chained on *tip* with ``expectedHeadOid``."""
        if not same_branch:
            # The new branch starts at the base tip, so the signed commit lands on it.
            _, err = await _common.safe_request(
                client, "post", f"{repo_url}/git/refs", _PROVIDER, f"create branch {target} in {repo}",
                headers=_json_headers(headers), json={"ref": f"refs/heads/{target}", "sha": tip},
            )
            if err:
                return f"{err}\nNothing was created in {repo}."
        headline, _, body = text.partition("\n")
        made = await _signed_commit(
            client, headers, repo, repo_url, target, tip, headline.strip(), additions, deletions, body.strip()
        )
        if isinstance(made, _Lost):
            return made.text
        if isinstance(made, str):
            if not same_branch:
                return (
                    f"{made}\nBranch '{target}' was created at {tip[:8]} ('{base_branch}') and holds no revert: call "
                    f"the tool again with branch and base_branch both '{target}'."
                )
            return made
        new_commit, word = made
        restored, deleted, merged, kept, already = listed
        counts = ", ".join(
            f"{len(paths)} {label}"
            for label, paths in (
                ("restored", restored), ("deleted", deleted), ("merged", merged), ("kept from the branch", kept)
            )
            if paths
        )
        where = f"on new branch '{target}' (from '{base_branch}')" if not same_branch else f"on branch '{target}'"
        lines = [f"Reverted commit {sha[:8]} of {repo} {where} in one commit ({new_commit}): {counts}."]
        lines += [
            _listed(label, paths)
            for label, paths in (("Restored", restored), ("Deleted", deleted), ("Merged", merged), ("Kept", kept))
            if paths
        ]
        if already:
            lines.append(_listed("Already reverted on the branch", already))
        lines.append(f"Signed: GitHub built the commit with createCommitOnBranch ({word}).")
        if not same_branch:
            lines.append(f"Open a pull request from '{target}' into '{base_branch}' with create_github_pr.")
        return "\n".join(lines)

    executor.register("revert_github_commit", revert_github_commit)

    return [
        ToolDefinition(
            name="revert_github_commit",
            description=(
                "Undo one commit as a single new commit, whatever its size (hundreds of files, megabytes): the "
                "parent's versions are restored by object id, nothing is downloaded or uploaded. By default the "
                "revert lands on a new branch named after the commit (revert- and its first 8 characters) made "
                "from the base branch (the default branch unless "
                "base_branch says otherwise), ready for create_github_pr; name an existing branch as both 'branch' "
                "and 'base_branch' to commit on top of it. A path the branch changed again since the commit is a "
                "conflict: the call is refused with every conflicting path and its three versions (parent, "
                "reverted commit, branch), and nothing is created until 'resolutions' settles each one (keep "
                "'branch' or 'parent', 'delete', or a merged 'content' / 'file_id'). A merge commit is reverted to "
                "its first parent unless 'parent' says otherwise. The commit is signed by GitHub "
                "(createCommitOnBranch) whenever it fits one: plain files, at most 100 paths and 5 MB. A larger "
                "revert goes through the Git Data API, unsigned with a user credential (the result says why and "
                "reports GitHub's verdict); on an existing branch with an open pull request that unsigned path "
                "is refused unless allow_unsigned is true."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "repo": {"type": "string", "description": "Repository in format owner/name"},
                    "sha": {"type": "string", "description": "Full SHA of the commit to revert"},
                    "branch": {
                        "type": "string",
                        "description": (
                            "Branch the revert commit lands on (default: a new branch named revert- and the "
                            "commit's first 8 characters)"
                        ),
                        "default": "",
                    },
                    "base_branch": {
                        "type": "string",
                        "description": (
                            "Branch the revert is computed against and the new branch starts from "
                            "(default: the default branch)"
                        ),
                        "default": "",
                    },
                    "message": {
                        "type": "string",
                        "description": (
                            "Commit message (default: 'revert: ' and the reverted commit's subject, its full SHA "
                            "in the body)"
                        ),
                        "default": "",
                    },
                    "resolutions": {
                        "type": "array",
                        "description": (
                            "One entry per conflicting path: {path, keep: branch|parent|delete}, {path, content} "
                            "or {path, file_id}"
                        ),
                        "items": {
                            "type": "object",
                            "properties": {
                                "path": {"type": "string"},
                                "keep": {"type": "string", "enum": list(_KEEP_CHOICES)},
                                "content": {"type": "string", "description": "The merged file, as plain text"},
                                "file_id": {
                                    "type": "string",
                                    "description": "A file of this conversation, committed byte-for-byte",
                                },
                            },
                            "required": ["path"],
                        },
                    },
                    "parent": {
                        "type": "integer",
                        "description": "For a merge commit: the 1-based parent to revert to (default 1)",
                        "default": 1,
                    },
                    "allow_unsigned": {
                        "type": "boolean",
                        "description": (
                            "Write an unsigned revert on an existing branch with an open pull request when it "
                            "cannot be signed (default false: refused)"
                        ),
                        "default": False,
                    },
                },
                "required": ["repo", "sha"],
            },
            source=_SOURCE,
            priority=1,
        ),
    ]
