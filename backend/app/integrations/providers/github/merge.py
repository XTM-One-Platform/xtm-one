"""``merge_github_branch``: bring one ref into a branch, server-side.

``manage_github_pr action=update_branch`` merges a pull request's base into
its branch, so it needs an open pull request, and on a conflict it answers a
bare refusal. This tool merges any ref into any branch with GitHub's Merges
API (``POST /repos/{owner}/{repo}/merges``): GitHub builds the merge commit
itself, the branch moves once, and nothing crosses the model's context.

On a conflict nothing is merged. The tool reads the merge base with the
compare API and names the paths changed on both sides since it: the
candidates for the conflict, with the three refs to read them at
(``get_github_file``). Called again with ``resolutions`` (one per path
changed on both sides: keep the branch's or the ref's version, a merged
text, or a file of the sandbox workspace), it lands the merge in commits
GitHub builds and signs (``merge_resolution``), split into as many
signed commits as the 5 MB cap of one commit needs.

A path both sides changed to the same blob is not a conflict: git merges it
as it is, so it is neither listed nor asked a resolution. The same
analysis answers ``manage_github_pr update_branch`` when GitHub refuses the
update of a pull request's branch on a conflict.

The default branch is refused: a change reaches it through a pull request.
"""

from __future__ import annotations

from typing import Any

from app.core.egress import egress_client
from app.tools.base import CallableToolExecutor, ToolDefinition, ToolResult

from . import _common
from ._common import _BASE_URL, _PROVIDER, _coerce_str, _enc, _json_headers, parse_repo
from .contents import _GIT_OBJECT_ID_RE, _branch_name_error, _json_object, _rest_signature_verdict
from .merge_hunks import LineMerge, describe_conflicts, hunk_ranges, line_merge, removed_lines
from .merge_resolution import (
    KEEP_CHOICES,
    Resolution,
    _branch_tip,
    _read_side,
    _settled_tip,
    _Side,
    drop_stopped_alignment,
    final_head_of,
    finish_stopped_resolution,
    parse_resolutions,
    resolve_and_merge,
)

_SOURCE = "integration:github"

#: Paths named per informational list of a conflict refusal; the rest are counted. The paths that need a
#: resolution are all named (a compare lists at most 300 files), so one answer is enough to resolve them.
_MAX_LISTED_PATHS = 40


_REMOVED = "removed"

#: Files GitHub's compare lists at most: a side listing this many may hold more, read from the trees.
_COMPARE_FILE_CAP = 300


def _tree_changes(base: _Side, side: _Side) -> dict[str, str]:
    """``{path: what the side holds}`` for every path *side*'s tree holds differently from *base*'s."""
    changed: dict[str, str] = {}
    for path in set(base.entries) | set(side.entries):
        before, after = base.entries.get(path), side.entries.get(path)
        if before is not None and after is not None and (before.mode, before.sha) == (after.mode, after.sha):
            continue
        changed[path] = _REMOVED if after is None else after.sha
    return changed


async def _changes_from_trees(
    client: Any, headers: dict[str, str], repo: str, repo_url: str, merge_base: str, branch: str, head: str
) -> tuple[dict[str, str], dict[str, str]] | str:
    """The paths each side changed since *merge_base*, from the three recursive trees: no 300-file cap."""
    sides: list[_Side] = []
    for ref in (merge_base, branch, head):
        side = await _read_side(client, headers, repo, repo_url, ref)
        if isinstance(side, str):
            return side
        sides.append(side)
    base, ours, theirs = sides
    return _tree_changes(base, theirs), _tree_changes(base, ours)


def _changed_paths(compared: dict[str, Any]) -> dict[str, str]:
    """``{path: what the side holds}`` for the paths a compare lists as changed.

    The value is the blob id the side holds, ``removed`` for a path it removed,
    or ``''`` when the compare does not say (the old name of a rename counts as
    changed, with an unknown content).
    """
    paths: dict[str, str] = {}
    for item in compared.get("files") or []:
        if not isinstance(item, dict):
            continue
        name = item.get("filename")
        if isinstance(name, str) and name:
            sha = item.get("sha")
            paths[name] = _REMOVED if item.get("status") == _REMOVED else (sha if isinstance(sha, str) else "")
        previous = item.get("previous_filename")
        if isinstance(previous, str) and previous:
            paths.setdefault(previous, "")
    return paths


def _listed_paths(paths: list[str], limit: int | None = _MAX_LISTED_PATHS) -> list[str]:
    shown = paths if limit is None else paths[:limit]
    lines = [f"- {path}" for path in shown]
    if len(paths) > len(shown):
        lines.append(f"- and {len(paths) - len(shown)} more")
    return lines


def _per_hunk_keep(path: str, merge: Any, keep: str, branch: str, head: str) -> tuple[str | None, str]:
    """The merged text of a ``keep: branch|ref`` that settles the overlapping hunks only, and the note naming them.

    ``None`` keeps the side's whole file (a binary, a delete against a change, a path the line merge read whole,
    or a per-hunk text that would break the path), the note saying why.
    """
    side = branch if keep == "branch" else head
    text = merge.keep_ours if keep == "branch" else merge.keep_theirs
    if merge.clean:
        return merge.merged, f"{path}: no overlapping hunk; merged line by line, both sides' changes kept."
    if text is None:
        return None, f"{path}: '{side}' version kept whole ({merge.reason or 'no line merge'})."
    defect = merge.defect(text)
    if defect:
        return None, f"{path}: '{side}' version kept whole (the per-hunk merge would break it: {defect})."
    ranges = hunk_ranges(merge, branch, head)
    return text, (
        f"{path}: {len(ranges)} overlapping hunk{'s' if len(ranges) != 1 else ''} taken from '{side}' "
        f"({'; '.join(ranges)}); every non-overlapping change of both sides kept."
    )


def _settled(
    resolved: dict[str, Resolution], merged: LineMerge | None, branch: str = "", head: str = ""
) -> tuple[dict[str, Resolution], list[str], str | None, list[str]]:
    """The resolutions with the clean merges filled in, the conflicting paths still unresolved, a refusal, the notes.

    A ``keep: branch|ref`` on a path the line merge read becomes the merged text taking that side in the
    overlapping hunks only, so the other side's non-overlapping changes in the file are never dropped.
    """
    settled = dict(resolved)
    notes: list[str] = []
    if merged is None:
        whole = sorted(path for path, resolution in resolved.items() if resolution.keep in ("branch", "ref"))
        if whole:
            notes.append(f"Kept whole (the line merge did not run): {', '.join(whole)}.")
        stray = sorted(path for path, resolution in resolved.items() if resolution.keep == "both")
        if stray:
            return (
                {},
                [],
                (
                    f"Error: keep 'both' needs the line merge of the path, which could not run: {', '.join(stray)}. "
                    "Give 'branch', 'ref', a merged 'content' or a 'workspace_path' instead. Nothing was written."
                ),
                [],
            )
        return settled, [], None, notes
    for path, merge in merged.merges.items():
        given = resolved.get(path)
        if given is None:
            if merge.clean:
                settled[path] = Resolution(None, merge.merged, None)
            continue
        if given.keep in ("branch", "ref"):
            text, note = _per_hunk_keep(path, merge, given.keep, branch, head)
            notes.append(note)
            if text is not None:
                settled[path] = Resolution(None, text, None)
            continue
        if given.keep != "both":
            continue
        both = merge.merged if merge.clean else merge.union
        if both is None:
            return (
                {},
                [],
                (
                    f"Error: keep 'both' cannot settle '{path}' ({merge.reason}): give 'branch', 'ref', a merged "
                    "'content' or a 'workspace_path'. Nothing was written."
                ),
                [],
            )
        # keep 'both' writes each side's whole hunk (#12179): a line both sides hold is written in each by design,
        # so only the parse is judged here; a resolved Python file is parsed again before any write.
        defect = merge.defect(both) if merge.clean else merge.parse_defect(both)
        if defect:
            return (
                {},
                [],
                (
                    f"Error: keep 'both' on '{path}' would break it: {defect}. Give a merged 'content' or a "
                    "'workspace_path' (sandbox_diff_files with base writes the three-way merge to settle), or keep "
                    "'branch' or 'ref'. Nothing was written."
                ),
                [],
            )
        settled[path] = Resolution(None, both, None)
        notes.append(f"{path}: both sides' lines kept in each overlapping hunk.")
    open_paths = [path for path in merged.conflicting if path not in resolved]
    return settled, open_paths, None, notes


async def conflict_answer(
    client: Any,
    headers: dict[str, str],
    repo: str,
    repo_url: str,
    branch: str,
    head: str,
    message: str,
    resolved: dict[str, Resolution],
) -> str:
    """The refusal of a conflicting merge of *head* into *branch*, or the merge landed.

    The paths changed on both sides since the merge base are merged line by
    line on the server (:mod:`.merge_hunks`). When every path merges cleanly,
    or the *resolved* paths settle the rest, the merge lands in signed commits.
    Resolutions naming every such path (none asking ``keep: both``) need no
    line merge, so none is read. Otherwise nothing is written and the answer
    names the overlapping hunks of each conflicting path with both sides'
    text: a second call resolves those paths only.
    """
    lead = f"Error: merging '{head}' into '{branch}' of {repo} conflicts, so nothing was merged."
    resp, err = await _common.safe_request(
        client, "get", f"{repo_url}/compare/{_enc(branch)}...{_enc(head)}", _PROVIDER,
        f"compare {branch}...{head} in {repo}", headers=headers,
    )
    if err:
        return f"{lead} The merge base could not be read: {err}"
    theirs_compare = _json_object(resp)
    if theirs_compare.get("status") in ("behind", "identical"):
        # The branch already holds the ref (GitHub's conflict answer is stale, or a retried call follows one that
        # landed): any write here would only put the ref's versions over the branch's own files.
        if resolved:
            checked = await finish_stopped_resolution(client, headers, repo, repo_url, branch, head, resolved)
            if checked is not None:
                return checked
        tip = theirs_compare.get("base_commit")
        now = _coerce_str(tip.get("sha")) if isinstance(tip, dict) else ""
        return (
            f"No change: '{branch}' of {repo} already contains '{head}'" + (f"; its head is {now}." if now else ".")
            + " Nothing was written."
        )
    base_commit = theirs_compare.get("merge_base_commit")
    merge_base = _coerce_str(base_commit.get("sha")) if isinstance(base_commit, dict) else ""
    if not merge_base:
        return f"{lead} GitHub did not return the merge base of the two refs."
    resp, err = await _common.safe_request(
        client, "get", f"{repo_url}/compare/{merge_base}...{_enc(branch)}", _PROVIDER,
        f"compare {merge_base[:8]}...{branch} in {repo}", headers=headers,
    )
    if err:
        return f"{lead} The branch's side could not be read: {err}"
    ours_compare = _json_object(resp)
    theirs = _changed_paths(theirs_compare)
    ours = _changed_paths(ours_compare)
    capped = any(len(c.get("files") or []) >= _COMPARE_FILE_CAP for c in (theirs_compare, ours_compare))
    tree_note = ""
    if capped:
        # A compare stops at 300 files: the paths it did not list are read from the trees, so none is hidden.
        from_trees = await _changes_from_trees(client, headers, repo, repo_url, merge_base, branch, head)
        if isinstance(from_trees, str):
            tree_note = f"A compare listed {_COMPARE_FILE_CAP} files, its cap, and the trees could not be read: {from_trees}"
        else:
            theirs, ours = from_trees
    changed = sorted(set(theirs) & set(ours))
    # Both sides holding the same blob (or both removing the path) merge as they are: not a conflict.
    same = [path for path in changed if theirs[path] and theirs[path] == ours[path]]
    both = [path for path in changed if path not in same]
    merged: LineMerge | None = None
    why_not = ""
    # A keep resolution is settled per hunk too: the line merge runs for every path a keep names.
    wants_merge = any(path not in resolved for path in both) or any(
        resolved[path].keep is not None for path in both if path in resolved
    )
    if both and wants_merge:
        attempt = await line_merge(client, headers, repo, repo_url, merge_base, branch, head, both)
        if isinstance(attempt, str):
            why_not = attempt
        else:
            merged = attempt
    settled, open_paths, refusal, notes = _settled(
        {path: resolution for path, resolution in resolved.items() if path not in same}, merged, branch, head
    )
    if refusal:
        return refusal
    # Given resolutions always go to the resolver, which names a path left unresolved; else a clean merge lands.
    if both and (resolved or (merged is not None and not open_paths)):
        landed = await resolve_and_merge(client, headers, repo, repo_url, branch, head, message, both, settled)
        removed = removed_lines(merged)
        if (notes or removed) and landed.startswith("Merged"):
            # Before the Final head line, which stays the last line of the answer.
            body, sep, last = landed.rpartition("\nFinal head: ")
            how = ("\nHow each resolution was applied:\n" + "\n".join(f"- {note}" for note in notes)) if notes else ""
            how += ("\n" + "\n".join(removed)) if removed else ""
            landed = f"{body}{how}{sep}{last}" if sep else landed + how
        return landed
    lines = [lead, f"Merge base {merge_base[:8]}."]
    if tree_note:
        lines.append(tree_note)
    if both and not _GIT_OBJECT_ID_RE.fullmatch(head):
        pinned = await _branch_tip(client, headers, repo, repo_url, head)
        if pinned:
            lines.append(
                f"'{head}' is at {pinned} for this answer: pass from_ref '{pinned}' with the resolutions, so a "
                f"whole-file 'content' written against it is never merged over commits '{head}' gained since."
            )
    if merged is not None and not open_paths:
        lines.append(
            f"Every path changed on both sides merges line by line without an overlap ({len(merged.clean)}); "
            f"call merge_github_branch (branch '{branch}', from_ref '{head}') to land it, no resolution needed:"
        )
        lines += _listed_paths(merged.clean)
        if merged.validated:
            lines.append(f"Parsed after the merge, valid with no duplicate key ({len(merged.validated)}):")
            lines += _listed_paths(merged.validated)
        lines += removed_lines(merged)
        return "\n".join(lines)
    if merged is not None:
        lines.append(
            f"The paths changed on both sides were merged line by line on the server: {len(merged.clean)} merge "
            f"without an overlap and need no resolution, {len(open_paths)} hold overlapping hunks. Call "
            f"merge_github_branch (branch '{branch}', from_ref '{head}') with 'resolutions' for those "
            f"{len(open_paths)} only: {{path, keep: branch|ref|both}} (on the overlapping hunks only, every other "
            "change kept; both: the branch's lines then the ref's), {path, content} or {path, workspace_path} (a checkout file settled with "
            "sandbox_checkout_rebase); the clean merges are filled in, and the merge lands in commits GitHub "
            "builds and signs."
        )
        lines.append(f"Overlapping hunks ({len(open_paths)} path{'s' if len(open_paths) != 1 else ''}):")
        lines.append(describe_conflicts([merged.merges[path] for path in open_paths], branch, head))
        if merged.clean:
            lines.append(f"Merged without an overlap ({len(merged.clean)}):")
            lines += _listed_paths(merged.clean)
        if merged.validated:
            lines.append(f"Parsed after the merge, valid with no duplicate key ({len(merged.validated)}):")
            lines += _listed_paths(merged.validated)
        lines += removed_lines(merged)
    else:
        lines.append(
            f"Read each path with get_github_file at refs {merge_base[:8]} (base), '{branch}' (ours) and '{head}' "
            f"(theirs), or bring a checkout onto '{head}' with sandbox_checkout_rebase and settle its conflict "
            f"markers; then call merge_github_branch (branch '{branch}', from_ref '{head}') with 'resolutions', "
            "one per path below: {path, keep: branch|ref}, {path, content} or {path, workspace_path}. The merge "
            "then lands in commits GitHub builds and signs, as many as the 5 MB cap of one signed commit needs."
        )
        if why_not:
            lines.append(f"The line merge on the server did not run: {why_not}.")
        if not both:
            lines.append(
                "No path is listed as changed differently on both sides (a compare lists at most 300 files): read "
                f"compare_github_commits {merge_base[:8]}...{head} to find them."
            )
        else:
            lines.append(f"Changed on both sides since the merge base ({len(both)}):")
            # Every path that needs a resolution is named: a cut list costs a second call to learn the rest.
            lines += _listed_paths(both, limit=None)
    if same:
        lines.append(f"Changed the same way on both sides, no resolution needed ({len(same)}):")
        lines += _listed_paths(same)
    return "\n".join(lines)


async def outcome_line(
    client: Any, headers: dict[str, str], repo: str, repo_url: str, branch: str, before: str, answer: str = ""
) -> str:
    """The outcome read from GitHub after a merge call: whether *branch* moved, and its head.

    The answer above it can be an error while commits landed (a lost answer, an undo that failed): this
    line is read from the branch itself, so the agent never has to guess whether anything was written.
    When *answer* names the last commit the call wrote (its ``Final head:`` line), the branch is read again
    while it still shows an older commit: GitHub's REST read can lag a GraphQL commit, and naming the merge
    commit in between (which holds the ref's side of the conflicting files) made a landed resolution look lost.
    """
    written = final_head_of(answer)
    if written:
        after = await _settled_tip(client, headers, repo, repo_url, branch, written)
    else:
        after = await _branch_tip(client, headers, repo, repo_url, branch)
    if not before or not after:
        return (
            f"Outcome: not read ('{branch}' head before {before[:12] or 'unread'}, after {after[:12] or 'unread'}): "
            "read compare_github_commits before another write."
        )
    if after == before:
        return f"Outcome: landed false: '{branch}' head is still {after}."
    return f"Outcome: landed true: '{branch}' head moved from {before[:12]} to {after}."


def register(
    headers: dict[str, str],
    executor: CallableToolExecutor,
    _mark_self_action: Any,
) -> list[ToolDefinition]:
    """Build the merge tool and register its callable.

    The self-action recorder every module takes is for issue and pull request
    numbers; a merge into a branch has none, so it is unused here.
    """

    async def merge_github_branch(
        repo: str,
        branch: str,
        from_ref: str = "",
        message: str = "",
        resolutions: list[dict[str, Any]] | None = None,
    ) -> str | ToolResult:
        """Merge ``from_ref`` (default: the default branch) into ``branch`` with GitHub's Merges API."""
        owner, name, rerr = parse_repo(repo)
        if rerr:
            return rerr
        resolved, perr = parse_resolutions(resolutions)
        if perr:
            return perr
        branch = _coerce_str(branch).strip()
        if not branch:
            return "Error: 'branch' is required: the branch the merge lands on."
        branch_err = _branch_name_error(branch)
        if branch_err:
            return branch_err
        repo_url = f"{_BASE_URL}/repos/{owner}/{name}"
        async with egress_client(timeout=60) as client:
            resp, err = await _common.safe_request(
                client, "get", repo_url, _PROVIDER, f"read the default branch of {repo}", headers=headers
            )
            if err:
                return err
            default = _coerce_str(_json_object(resp).get("default_branch"))
            if branch == default:
                return (
                    f"Error: '{branch}' is the default branch of {repo}: a change reaches it through a pull request "
                    "(create_github_pr, then merge_github_pr), never by a direct merge. Nothing was merged."
                )
            head = _coerce_str(from_ref).strip() or default
            if not head:
                return "Error: 'from_ref' is required: GitHub did not return the default branch of the repository."
            if head == branch:
                return "Error: 'from_ref' and 'branch' name the same branch: nothing to merge."
            text = _coerce_str(message).strip() or f"Merge {head} into {branch}"
            # A stopped earlier call may have left the branch on its take-the-ref alignment commits: merging from
            # there would keep the ref's version of those files. The branch goes back before them first.
            dropped, refusal = await drop_stopped_alignment(client, headers, repo, repo_url, branch)
            if refusal:
                return refusal
            before = await _branch_tip(client, headers, repo, repo_url, branch)
            answer = await _merge(client, repo, repo_url, branch, head, text, resolved)
            trailer = [dropped] if dropped else []
            if isinstance(answer, str) and answer.startswith("Error:"):
                # An error can follow commits that landed: the outcome is read from the branch itself.
                trailer.append(await outcome_line(client, headers, repo, repo_url, branch, before, answer))
            if isinstance(answer, ToolResult):
                answer.content = "\n".join([answer.content, *trailer])
                return answer
            return "\n".join([answer, *trailer])

    async def _merge(
        client: Any, repo: str, repo_url: str, branch: str, head: str, text: str, resolved: dict[str, Resolution]
    ) -> str | ToolResult:
        """The Merges API call of :func:`merge_github_branch`, and the conflict path on a 409."""
        resp, err = await _common.safe_request(
            client,
            "post",
            f"{repo_url}/merges",
            _PROVIDER,
            f"merge {head} into {branch} in {repo}",
            headers=_json_headers(headers),
            json={"base": branch, "head": head, "commit_message": text},
            expected_statuses={404, 409},
        )
        status = resp.status_code if resp is not None else None
        if status == 404:
            return (
                f"Error: branch '{branch}' or ref '{head}' does not exist in {repo} (GitHub answered 404). "
                "Nothing was merged."
            )
        if status == 409:
            return await conflict_answer(client, headers, repo, repo_url, branch, head, text, resolved)
        if err:
            return err
        if status == 204 and resolved:
            # The ref is already merged, yet resolutions were given: a replay of a call stopped between two
            # resolution commits must not answer "no change" over a branch missing the last chunks.
            checked = await finish_stopped_resolution(client, headers, repo, repo_url, branch, head, resolved)
            if checked is not None:
                return checked
        if status == 204:
            return ToolResult(
                tool_call_id="",
                content=f"No change: branch '{branch}' of {repo} already contains '{head}'. Nothing was merged.",
                no_op=True,
            )
        created = _json_object(resp)
        merge_sha = _coerce_str(created.get("sha"))
        commit = created.get("commit")
        verdict = _rest_signature_verdict(commit if isinstance(commit, dict) else {})
        signed = ""
        if verdict is not None:
            signed = " GitHub reports it as verified." if verdict.verified else (
                f" GitHub reports it as {verdict.reason or 'not verified'}."
            )
        return f"Merged '{head}' into branch '{branch}' of {repo}: merge commit {merge_sha}.{signed}"

    executor.register("merge_github_branch", merge_github_branch)

    return [
        ToolDefinition(
            name="merge_github_branch",
            description=(
                "Merge a ref into a branch server-side with GitHub's Merges API: 'from_ref' (default: the "
                "repository's default branch) into 'branch', one merge commit built by GitHub, the branch moved "
                "once. Works without a pull request (a stacked branch, a release branch); for a pull request's own "
                "base, manage_github_pr update_branch does the same. When GitHub refuses on a conflict, the paths "
                "changed on both sides are merged line by line on the server: if none overlaps, the merge lands in "
                "the same call; else nothing is written and the answer names each overlapping hunk (line ranges and "
                "both sides' text). Call again with 'resolutions' for the conflicting paths only (keep 'branch', "
                "'ref' or 'both', a merged 'content', or a 'workspace_path' such as a checkout file settled after "
                "sandbox_checkout_rebase): the clean merges are filled in and the merge lands in commits GitHub "
                "builds and signs, split under the 5 MB cap of one signed commit. A path both sides changed "
                "the same way needs no resolution. The default branch is refused: changes reach it through a pull request."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "repo": {"type": "string", "description": "Repository in format owner/name"},
                    "branch": {"type": "string", "description": "Branch the merge lands on (not the default branch)"},
                    "from_ref": {
                        "type": "string",
                        "description": "Branch, tag or SHA merged into 'branch' (default: the default branch)",
                        "default": "",
                    },
                    "message": {
                        "type": "string",
                        "description": "Merge commit message (default: 'Merge' then the source ref, 'into' and the branch)",
                        "default": "",
                    },
                    "resolutions": {
                        "type": "array",
                        "description": (
                            "After a conflict answer: one entry per conflicting path, {path, keep: branch|ref|both} "
                            "(on the overlapping hunks only; both: the branch's lines then the ref's), {path, content} or "
                            "{path, workspace_path}; paths that merged line by line are filled in"
                        ),
                        "items": {
                            "type": "object",
                            "properties": {
                                "path": {"type": "string"},
                                "keep": {"type": "string", "enum": list(KEEP_CHOICES)},
                                "content": {"type": "string", "description": "The merged file, as plain text"},
                                "workspace_path": {
                                    "type": "string",
                                    "description": "A file of the sandbox workspace holding the merged file",
                                },
                            },
                            "required": ["path"],
                        },
                    },
                },
                "required": ["repo", "branch"],
            },
            source=_SOURCE,
            priority=1,
            # A repeat cannot double the merge: it drops alignment commits a stopped call left, answers a merge
            # already landed, and writes the resolution chunks a stopped call left unwritten
            # (finish_stopped_resolution). A resumed turn must run it, not refuse it as an in-flight write.
            replay_safe=True,
        ),
    ]
