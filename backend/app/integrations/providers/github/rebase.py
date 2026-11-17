"""``rebase_github_branch``: a true rebase, server-side, commit by commit.

``merge_github_branch`` brings a ref into a branch with one merge commit, and a
squashed tree commit on top of a ref followed by a forced ref move turns N
commits into one: a stacked pull request then loses its merge base. This tool
replays each commit of a branch onto a ref, in order, the way ``git rebase``
does:

1. the commits to replay are those of ``branch`` since ``upstream`` (default:
   the merge base with ``onto``), oldest first; a merge commit is skipped, as
   ``git rebase`` skips it;
2. each commit's change (its tree against its first parent's) is applied to the
   tip of a work branch made at ``onto``: a path the work tip still holds as the
   commit's parent did takes the commit's version; any other path is merged line
   by line (the diff3 merge of ``sandbox_checkout_rebase``); a commit whose
   change is already there is dropped, as git drops it;
3. each replayed commit is built and signed by GitHub (``createCommitOnBranch``,
   chained with ``expectedHeadOid``), with the original message, the original
   author as a ``Co-authored-by`` trailer and the original date and commit in a
   ``Rebased-from`` trailer (a GitHub-built commit has the acting identity as
   author and committer);
4. once every commit is replayed, ``branch`` is moved to the work tip with one
   ``updateRefs`` whose ``beforeOid`` is ``expected_head``: force with lease,
   refused when the branch moved since the caller read it; the work branch is
   then deleted.

A conflicting commit stops the replay before anything of it is written: the
answer names the commit, each overlapping hunk with both sides' text, and the
call to make again with ``resolutions`` for that commit (``keep`` onto, commit
or both, or a merged ``content``). The work branch is named from the branch,
``expected_head`` and ``onto``'s commit, and every replayed commit carries the
commit it came from, so a call made again (after a conflict, a worker restart or
``max_commits``) goes on from the last replayed commit and never replays one
twice.

The rebase remembers its calls (``rebase_state``, #11802): the commit ``onto``
pointed at on the first call (a later call naming the same ref replays onto it,
so a moved ``main`` never restarts the replay), every resolution passed so far
(a call passes only the new commit's), and the commits that wrote nothing once
applied. A path whose resolved or merged bytes equal the work tip's is not
written, so no commit is replayed as an empty duplicate, and a ``keep: onto``
that discards a change of the commit is named in the answer.

``onto`` and ``upstream`` give ``git rebase --onto``: a branch stacked on a
branch that was itself rebased is replayed with ``upstream`` set to the parent's
OLD tip and ``onto`` to its new one, so only its own commits move.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any, NamedTuple

from app.core.egress import egress_client
from app.services.sandbox_workspace import structured
from app.tools.base import CallableToolExecutor, ToolDefinition

from . import _common, rebase_state
from ._common import _BASE_URL, _PROVIDER, _coerce_str, _enc, _json_headers, parse_repo
from .contents import _GIT_OBJECT_ID_RE, _MAX_COMMIT_CONTENT_BYTES, _MODE_FILE, _branch_name_error, _json_object
from .merge_hunks import PathMerge, describe_conflicts, merge_path
from .merge_resolution import _Entry, _Lost, _read_blob, _read_side, _restore_head, _Side, _signed_commit
from .rebase_state import RebaseState, merge_resolutions, state_key

_SOURCE = "integration:github"

#: Commits one compare lists: a branch with more ahead of its upstream is refused.
MAX_REPLAYED = 250
#: Commits one call replays by default; the next call goes on from there.
DEFAULT_MAX_COMMITS = 40
#: The trailer naming the commit a replayed commit came from.
TRAILER = "Rebased-from"
_TRAILER_RE = re.compile(rf"^{TRAILER}: ([0-9a-f]{{40}})\b", re.MULTILINE)
KEEP_CHOICES = ("onto", "commit", "both")


class Resolution(NamedTuple):
    """How one conflicting path of one commit is settled: ``keep`` or ``content``."""

    keep: str | None
    content: str | None


class Commit(NamedTuple):
    sha: str
    parents: tuple[str, ...]
    message: str
    author: str
    date: str


def work_branch_name(branch: str, expected_head: str, onto_commit: str) -> str:
    """The work branch of one rebase: the same call made again finds it."""
    return f"rebase/{branch}/{expected_head[:12]}-onto-{onto_commit[:12]}"


def replayed_from(message: str) -> str | None:
    """The commit a replayed commit's message names in its trailer, or ``None``."""
    found = _TRAILER_RE.findall(message or "")
    return found[-1] if found else None


def replay_message(commit: Commit) -> tuple[str, str]:
    """``(headline, body)`` of the replay of *commit*: its message, its author and origin as trailers."""
    headline, _, rest = (commit.message or "").partition("\n")
    body = rest.strip("\n")
    trailers = [f"{TRAILER}: {commit.sha} ({commit.date})" if commit.date else f"{TRAILER}: {commit.sha}"]
    if commit.author:
        trailers.append(f"Co-authored-by: {commit.author}")
    return headline.strip() or f"Replay of {commit.sha[:12]}", (f"{body}\n\n" if body else "") + "\n".join(trailers)


def parse_resolutions(raw: Any) -> tuple[dict[tuple[str, str], Resolution], str | None]:
    """``{(commit prefix, path): resolution}`` from the argument, or an error."""
    if raw in (None, "", []):
        return {}, None
    if not isinstance(raw, list):
        return {}, "Error: 'resolutions' must be a list of {commit, path, keep | content}."
    parsed: dict[tuple[str, str], Resolution] = {}
    for item in raw:
        if not isinstance(item, dict):
            return {}, "Error: each resolution is an object with 'commit', 'path' and one of 'keep' or 'content'."
        commit = _coerce_str(item.get("commit")).strip().lower()
        path = _coerce_str(item.get("path")).strip()
        if not re.fullmatch(r"[0-9a-f]{7,40}", commit) or not path:
            return {}, "Error: each resolution names its 'commit' (7 to 40 hexadecimal characters) and its 'path'."
        keep = item.get("keep")
        content = item.get("content")
        if (keep is None) == (not isinstance(content, str)):
            return (
                {},
                f"Error: the resolution of '{path}' needs exactly one of 'keep' ({'/'.join(KEEP_CHOICES)}) or 'content'.",
            )
        if keep is not None and keep not in KEEP_CHOICES:
            return {}, f"Error: 'keep' for '{path}' must be one of {', '.join(KEEP_CHOICES)}."
        if (commit, path) in parsed:
            return {}, f"Error: '{path}' of {commit} is resolved more than once."
        parsed[(commit, path)] = Resolution(keep, content if isinstance(content, str) else None)
    return parsed, None


def _resolution_for(resolutions: dict[tuple[str, str], Resolution], sha: str, path: str) -> Resolution | None:
    for (prefix, rpath), resolution in resolutions.items():
        if rpath == path and sha.startswith(prefix):
            return resolution
    return None


def _commits(listing: list[Any]) -> list[Commit]:
    commits = []
    for item in listing:
        if not isinstance(item, dict):
            continue
        raw_inner = item.get("commit")
        inner: dict[str, Any] = raw_inner if isinstance(raw_inner, dict) else {}
        raw_author = inner.get("author")
        author: dict[str, Any] = raw_author if isinstance(raw_author, dict) else {}
        parents = tuple(_coerce_str(p.get("sha")) for p in item.get("parents") or [] if isinstance(p, dict))
        name, email = _coerce_str(author.get("name")).strip(), _coerce_str(author.get("email")).strip()
        commits.append(
            Commit(
                _coerce_str(item.get("sha")),
                parents,
                _coerce_str(inner.get("message")),
                f"{name} <{email}>" if name and email else "",
                _coerce_str(author.get("date")),
            )
        )
    return commits


class _Applied(NamedTuple):
    """One commit applied to the work tip: the writes, or the conflicts that stop it, and what a resolution dropped."""

    additions: dict[str, bytes]
    deletions: list[str]
    conflicts: list[PathMerge]
    warnings: tuple[str, ...] = ()


def _blob_sha(data: bytes) -> str:
    """The git object id of a blob holding *data*."""
    return hashlib.sha1(b"blob %d\0" % len(data) + data, usedforsecurity=False).hexdigest()


def _unchanged(ours: _Entry | None, data: bytes) -> bool:
    """Whether writing *data* leaves the work tip's plain file *ours* as it is."""
    return ours is not None and ours.mode == _MODE_FILE and ours.sha == _blob_sha(data)


async def _apply(
    client: Any,
    headers: dict[str, str],
    repo: str,
    repo_url: str,
    commit: Commit,
    parent: _Side,
    own: _Side,
    tip: _Side,
    resolutions: dict[tuple[str, str], Resolution],
) -> _Applied | str:
    """The change of *commit* (``own`` against ``parent``) applied to the work *tip*."""
    additions: dict[str, bytes] = {}
    deletions: list[str] = []
    conflicts: list[PathMerge] = []
    warnings: list[str] = []
    paths = sorted(set(parent.entries) | set(own.entries))
    for path in paths:
        base, theirs, ours = parent.entries.get(path), own.entries.get(path), tip.entries.get(path)
        if base == theirs:
            continue  # not changed by the commit
        if _same(ours, theirs):
            continue  # already there
        if _same(ours, base):
            # The work tip holds what the commit's parent held: take the commit's version.
            if theirs is None:
                deletions.append(path)
                continue
            if theirs.mode != (ours.mode if ours else _MODE_FILE):
                conflicts.append(
                    PathMerge(path, reason=f"its mode becomes {theirs.mode}, which a signed commit cannot set")
                )
                continue
            data = await _read_blob(client, headers, repo, repo_url, theirs.sha, path)
            if isinstance(data, str):
                return data
            additions[path] = data
            continue
        modes = {entry.mode for entry in (base, ours, theirs) if entry is not None}
        if modes != {_MODE_FILE}:
            merged = PathMerge(path, reason="not a plain file: keep one side whole")
        elif base is None and (ours is None or theirs is None):
            merged = PathMerge(path, reason="added on one side, removed on the other")
        else:
            blobs: list[bytes | None] = []
            for entry in (base, ours, theirs):
                if entry is None:
                    blobs.append(None)
                    continue
                data = await _read_blob(client, headers, repo, repo_url, entry.sha, path)
                if isinstance(data, str):
                    return data
                blobs.append(data)
            merged = merge_path(path, blobs[0], blobs[1], blobs[2])
        resolution = _resolution_for(resolutions, commit.sha, path)
        if resolution is None and merged.clean:
            data = (merged.merged or "").encode("utf-8")
            if not _unchanged(ours, data):
                additions[path] = data
            continue
        if resolution is None:
            conflicts.append(merged)
            continue
        if resolution.keep == "onto":
            warnings.append(
                f"- {commit.sha[:12]} {path}: 'keep: onto' kept the onto side and dropped this commit's change to the "
                "path; give 'keep: both' or a merged 'content' to keep it."
            )
        written = await _resolved(client, headers, repo, repo_url, path, resolution, merged, ours, theirs)
        if isinstance(written, str):
            return written
        if written is None:
            if ours is not None:
                deletions.append(path)
        elif not _unchanged(ours, written):
            additions[path] = written
    return _Applied(additions, deletions, conflicts, tuple(warnings))


def _same(left: _Entry | None, right: _Entry | None) -> bool:
    if left is None or right is None:
        return left is right
    return (left.mode, left.sha) == (right.mode, right.sha)


async def _resolved(
    client: Any,
    headers: dict[str, str],
    repo: str,
    repo_url: str,
    path: str,
    resolution: Resolution,
    merged: PathMerge,
    ours: _Entry | None,
    theirs: _Entry | None,
) -> bytes | None | str:
    """The bytes a resolution writes, ``None`` to delete the path, or an error.

    A written text (``content`` or ``keep: both``) of a path with a parser (Python, JSON, YAML, TOML) that both
    sides parse is parsed before it is committed: one that no longer parses is refused with its line (#12179).
    """
    if resolution.content is not None:
        text = resolution.content
    elif resolution.keep == "both":
        if merged.union is None:
            return f"Error: 'keep: both' for '{path}' needs a text merged line by line; give 'content' instead."
        text = merged.union
    else:
        text = None
    if text is not None:
        if structured.resolution_kind(path):
            sides: list[str] = []
            for side, lines in ((ours, merged.ours), (theirs, merged.theirs)):
                if lines:
                    sides.append("\n".join(lines) + "\n")
                elif side is not None:
                    data = await _read_blob(client, headers, repo, repo_url, side.sha, path)
                    if isinstance(data, str):
                        return data
                    sides.append(data.decode("utf-8", "replace"))
            why = structured.resolution_defect(path, text, *sides)
            if why:
                how = "'keep: both'" if resolution.content is None else "the given 'content'"
                return (
                    f"Error: {how} for '{path}' would break it: both sides parse but the resolved text is {why}. "
                    "Give a merged 'content' that parses, or keep 'onto' or 'commit'."
                )
        return text.encode("utf-8")
    entry = ours if resolution.keep == "onto" else theirs
    if entry is None:
        return None
    data = await _read_blob(client, headers, repo, repo_url, entry.sha, path)
    return data


async def _compare(
    client: Any, headers: dict[str, str], repo: str, repo_url: str, base: str, head: str
) -> dict[str, Any] | str:
    resp, err = await _common.safe_request(
        client,
        "get",
        f"{repo_url}/compare/{_enc(base)}...{_enc(head)}",
        _PROVIDER,
        f"compare {base} and {head} in {repo}",
        headers=headers,
        params={"per_page": "100"},
    )
    if err:
        return err
    return _json_object(resp)


async def _tip(client: Any, headers: dict[str, str], repo: str, repo_url: str, ref: str) -> str:
    resp, err = await _common.safe_request(
        client,
        "get",
        f"{repo_url}/commits/{_enc(ref)}",
        _PROVIDER,
        f"read {ref} of {repo}",
        headers=headers,
        expected_statuses={404, 422},
    )
    if err or resp is None or resp.status_code != 200:
        return ""
    sha = _coerce_str(_json_object(resp).get("sha"))
    return sha if _GIT_OBJECT_ID_RE.fullmatch(sha) else ""


async def _message_origin(client: Any, headers: dict[str, str], repo: str, repo_url: str, sha: str) -> str | None:
    """The commit the message of *sha* names in its ``Rebased-from`` trailer: a call replayed after it landed."""
    resp, err = await _common.safe_request(
        client, "get", f"{repo_url}/commits/{sha}", _PROVIDER, f"read commit {sha[:12]} of {repo}", headers=headers
    )
    if err:
        return None
    inner = _json_object(resp).get("commit")
    return replayed_from(_coerce_str(inner.get("message"))) if isinstance(inner, dict) else None


async def _create_branch(
    client: Any, headers: dict[str, str], repo: str, repo_url: str, name: str, sha: str
) -> str | None:
    _resp, err = await _common.safe_request(
        client,
        "post",
        f"{repo_url}/git/refs",
        _PROVIDER,
        f"create work branch {name} in {repo}",
        headers=_json_headers(headers),
        json={"ref": f"refs/heads/{name}", "sha": sha},
    )
    return err


async def _delete_branch(client: Any, headers: dict[str, str], repo: str, repo_url: str, name: str) -> str | None:
    _resp, err = await _common.safe_request(
        client,
        "delete",
        f"{repo_url}/git/refs/heads/{_enc(name)}",
        _PROVIDER,
        f"delete work branch {name} in {repo}",
        headers=headers,
    )
    return err


def _resume_hint(repo: str, branch: str, expected_head: str, onto: str, upstream: str) -> str:
    extra = f", upstream '{upstream}'" if upstream else ""
    return (
        f"Call rebase_github_branch again (repo '{repo}', branch '{branch}', expected_head '{expected_head}', "
        f"onto '{onto}'{extra}): it goes on from the last replayed commit."
    )


def register(headers: dict[str, str], executor: CallableToolExecutor, _mark_self_action: Any) -> list[ToolDefinition]:
    """Build the rebase tool and register its callable."""

    async def rebase_github_branch(
        repo: str,
        branch: str,
        expected_head: str,
        onto: str = "",
        upstream: str = "",
        resolutions: list[dict[str, Any]] | None = None,
        max_commits: int = DEFAULT_MAX_COMMITS,
    ) -> str:
        """Replay the commits of ``branch`` onto ``onto`` and move it there with a lease on ``expected_head``."""
        owner, name, rerr = parse_repo(repo)
        if rerr:
            return rerr
        branch = _coerce_str(branch).strip()
        expected_head = _coerce_str(expected_head).strip().lower()
        if not branch:
            return "Error: 'branch' is required: the branch whose commits are replayed."
        if err := _branch_name_error(branch):
            return err
        if not _GIT_OBJECT_ID_RE.fullmatch(expected_head):
            return (
                "Error: 'expected_head' is required: the full 40-character SHA the branch points at now, read just "
                "before (the lease: the branch is moved only while it still points there)."
            )
        resolved, perr = parse_resolutions(resolutions)
        if perr:
            return perr
        try:
            budget = max(1, min(int(max_commits), MAX_REPLAYED))
        except (TypeError, ValueError):
            return "Error: 'max_commits' must be a number."
        repo_url = f"{_BASE_URL}/repos/{owner}/{name}"
        async with egress_client(timeout=60) as client:
            resp, err = await _common.safe_request(
                client, "get", repo_url, _PROVIDER, f"read the default branch of {repo}", headers=headers
            )
            if err:
                return err
            default = _coerce_str(_json_object(resp).get("default_branch"))
            if branch == default:
                return f"Error: '{branch}' is the default branch of {repo}: it is never rebased. Nothing was written."
            onto_ref = _coerce_str(onto).strip() or default
            now = await _tip(client, headers, repo, repo_url, branch)
            if not now:
                return f"Error: branch '{branch}' of {repo} could not be read. Nothing was written."
            key = state_key(repo, branch, expected_head)
            stored = await rebase_state.load(key)
            if stored is not None and stored.onto_ref != onto_ref:
                stored = None  # another onto: another rebase
            pinned_note = ""
            onto_commit = await _tip(client, headers, repo, repo_url, onto_ref)
            if stored is not None and _GIT_OBJECT_ID_RE.fullmatch(stored.onto_commit):
                if onto_commit and onto_commit != stored.onto_commit:
                    pinned_note = (
                        f"'{onto_ref}' moved to {onto_commit[:12]} since the first call of this rebase: the replay goes "
                        f"on onto {stored.onto_commit[:12]}, the commit it pointed at then."
                    )
                onto_commit = stored.onto_commit
            if not onto_commit:
                return f"Error: '{onto_ref}' could not be read in {repo}. Nothing was written."
            all_resolutions = merge_resolutions(stored.resolutions if stored else [], resolved)
            resolved = {
                (str(commit), str(path)): Resolution(keep, content) for commit, path, keep, content in all_resolutions
            }
            settled = list(stored.settled) if stored else []
            work = work_branch_name(branch, expected_head, onto_commit)
            if now != expected_head:
                if await _message_origin(client, headers, repo, repo_url, now) == expected_head:
                    return (
                        f"No change: '{branch}' of {repo} already holds the rebase of {expected_head[:12]} (its tip "
                        f"{now[:12]} was replayed from it); nothing was written."
                    )
                return (
                    f"Error: branch '{branch}' of {repo} points at {now}, not at expected_head {expected_head}: it "
                    "moved since it was read (the lease). Nothing was written; read the branch again and decide."
                )
            upstream_ref = _coerce_str(upstream).strip() or onto_commit
            compared = await _compare(client, headers, repo, repo_url, upstream_ref, expected_head)
            if isinstance(compared, str):
                return compared
            total = compared.get("total_commits")
            raw_listed = compared.get("commits")
            listed: list[Any] = raw_listed if isinstance(raw_listed, list) else []
            if isinstance(total, int) and total > len(listed):
                return (
                    f"Error: '{branch}' has {total} commits since '{upstream_ref}', over the {len(listed)} one compare "
                    f"lists (at most {MAX_REPLAYED}). Nothing was written."
                )
            todo = _commits(listed)
            skipped_merges = [c for c in todo if len(c.parents) > 1]
            todo = [c for c in todo if len(c.parents) == 1]
            if not todo:
                return f"No change: '{branch}' has no commit of its own since '{upstream_ref}'. Nothing was written."

            await rebase_state.save(key, RebaseState(onto_ref, onto_commit, all_resolutions, settled))
            work_tip = await _tip(client, headers, repo, repo_url, work)
            done: set[str] = set(settled)
            if not work_tip:
                err = await _create_branch(client, headers, repo, repo_url, work, onto_commit)
                if err:
                    return f"{err}\nThe work branch '{work}' could not be created. Nothing was written."
                work_tip = onto_commit
            elif work_tip != onto_commit:
                replayed = await _compare(client, headers, repo, repo_url, onto_commit, work_tip)
                if isinstance(replayed, str):
                    return replayed
                for item in _commits(replayed.get("commits") or []):
                    if origin := replayed_from(item.message):
                        done.add(origin)

            lines: list[str] = [pinned_note] if pinned_note else []
            sides: dict[str, _Side] = {}

            async def side(sha: str) -> _Side | str:
                if sha not in sides:
                    read = await _read_side(client, headers, repo, repo_url, sha)
                    if isinstance(read, str):
                        return read
                    sides[sha] = read
                return sides[sha]

            replayed_now = 0
            dropped: list[str] = []
            for commit in todo:
                if commit.sha in done:
                    continue
                if replayed_now >= budget:
                    left = len([c for c in todo if c.sha not in done])
                    return "\n".join(
                        [
                            f"Replayed {replayed_now} commit(s) onto '{onto_ref}' ({onto_commit[:12]}) on work branch "
                            f"'{work}' (tip {work_tip[:12]}); {left} remain(s), and '{branch}' was not moved yet.",
                            *lines,
                            _resume_hint(repo, branch, expected_head, onto_ref, _coerce_str(upstream).strip()),
                        ]
                    )
                parent = await side(commit.parents[0])
                own = await side(commit.sha)
                tip = await side(work_tip)
                for read in (parent, own, tip):
                    if isinstance(read, str):
                        return f"{read}\nStopped before {commit.sha[:12]}; work branch '{work}' is at {work_tip[:12]}."
                applied = await _apply(client, headers, repo, repo_url, commit, parent, own, tip, resolved)  # type: ignore[arg-type]
                if isinstance(applied, str):
                    return f"{applied}\nStopped before {commit.sha[:12]}; work branch '{work}' is at {work_tip[:12]}."
                if applied.conflicts:
                    headline = commit.message.partition("\n")[0]
                    return "\n".join(
                        [
                            f"Conflict: commit {commit.sha[:12]} ('{headline}') does not apply onto work branch '{work}' "
                            f"(tip {work_tip[:12]}); nothing of it was written and '{branch}' was not moved.",
                            *lines,
                            f"Call again with 'resolutions' for this commit: {{commit: '{commit.sha[:12]}', path, keep: "
                            "onto|commit|both}} (both: the onto side's lines then the commit's, in each hunk) or "
                            "{commit, path, content}. Paths:",
                            describe_conflicts(applied.conflicts, "onto", f"commit {commit.sha[:12]}"),
                            _resume_hint(repo, branch, expected_head, onto_ref, _coerce_str(upstream).strip()),
                        ]
                    )
                lines.extend(applied.warnings)
                if not applied.additions and not applied.deletions:
                    # Nothing to write: never an empty commit, and never applied again by a later call.
                    dropped.append(commit.sha[:12])
                    done.add(commit.sha)
                    settled.append(commit.sha)
                    await rebase_state.save(key, RebaseState(onto_ref, onto_commit, all_resolutions, settled))
                    continue
                size = sum(len(data) for data in applied.additions.values())
                if size > _MAX_COMMIT_CONTENT_BYTES:
                    return (
                        f"Error: commit {commit.sha[:12]} writes {size} bytes, over the {_MAX_COMMIT_CONTENT_BYTES} one "
                        f"signed commit carries; work branch '{work}' is at {work_tip[:12]} and '{branch}' was not moved."
                    )
                headline, body = replay_message(commit)
                made = await _signed_commit(
                    client,
                    headers,
                    repo,
                    repo_url,
                    work,
                    work_tip,
                    headline,
                    applied.additions,
                    applied.deletions,
                    body,
                )
                if isinstance(made, _Lost):
                    return f"{made.text}\n{_resume_hint(repo, branch, expected_head, onto_ref, _coerce_str(upstream).strip())}"
                if isinstance(made, str):
                    return f"{made}\nStopped at {commit.sha[:12]}; work branch '{work}' is at {work_tip[:12]}."
                work_tip = made[0]
                done.add(commit.sha)
                replayed_now += 1
                lines.append(f"- {commit.sha[:12]} -> {work_tip[:12]} ({made[1]}): {headline}")

            moved = await _restore_head(client, headers, repo, repo_url, branch, work_tip, expected_head)
            if moved is not None:
                return "\n".join(
                    [
                        f"Error: every commit is replayed on work branch '{work}' (tip {work_tip}), but '{branch}' was not "
                        f"moved: {moved} The lease on {expected_head[:12]} refuses a branch that moved meanwhile.",
                        *lines,
                    ]
                )
            await rebase_state.drop(key)
            cleanup = await _delete_branch(client, headers, repo, repo_url, work)
            summary = [
                f"Rebased '{branch}' of {repo} onto '{onto_ref}' ({onto_commit[:12]}): {expected_head[:12]} -> "
                f"{work_tip}, moved with a lease on {expected_head[:12]} and read back.",
                *lines,
            ]
            if dropped:
                summary.append(f"Dropped (their change is already on '{onto_ref}'): {', '.join(dropped)}.")
            if skipped_merges:
                summary.append(
                    f"Merge commits skipped, as git rebase does: {', '.join(c.sha[:12] for c in skipped_merges)}."
                )
            if cleanup:
                summary.append(f"The work branch '{work}' could not be deleted: {cleanup}")
            return "\n".join(summary)

    executor.register("rebase_github_branch", rebase_github_branch)
    return [
        ToolDefinition(
            name="rebase_github_branch",
            description=(
                "True rebase server-side: replay each commit of 'branch' (since 'upstream', default its merge base "
                "with 'onto') onto 'onto' (default: the default branch), in order, each as a commit GitHub builds and "
                "signs with the original message, the original author as Co-authored-by and the original commit in a "
                "Rebased-from trailer; then move 'branch' with force-with-lease: 'expected_head' (its current full "
                "SHA) is required and a branch that moved is refused. A conflicting commit stops the replay with its "
                "hunks: call again with 'resolutions' for that commit ({commit, path, keep: onto|commit|both} or "
                "{commit, path, content}), only the new ones: the rebase keeps every resolution passed, the onto "
                "commit of its first call and the commits settled, and goes on from the last replayed commit, never "
                "replaying one twice (also after a worker restart or 'max_commits'). Stacked branch: after its parent is rebased, pass "
                "upstream = the parent's OLD tip and onto = its new tip (git rebase --onto)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "repo": {"type": "string", "description": "Repository in format owner/name"},
                    "branch": {"type": "string", "description": "The branch to rebase (not the default branch)"},
                    "expected_head": {
                        "type": "string",
                        "description": "The full SHA the branch points at now: the lease of the final move",
                    },
                    "onto": {
                        "type": "string",
                        "description": "Ref to replay onto (default: the default branch)",
                        "default": "",
                    },
                    "upstream": {
                        "type": "string",
                        "description": "Commits after this ref are replayed (default: the merge base with onto)",
                        "default": "",
                    },
                    "resolutions": {
                        "type": "array",
                        "description": "After a conflict answer: {commit, path, keep: onto|commit|both} or {commit, path, content}",
                        "items": {
                            "type": "object",
                            "properties": {
                                "commit": {"type": "string"},
                                "path": {"type": "string"},
                                "keep": {"type": "string", "enum": list(KEEP_CHOICES)},
                                "content": {"type": "string", "description": "The merged file, as plain text"},
                            },
                            "required": ["commit", "path"],
                        },
                    },
                    "max_commits": {
                        "type": "integer",
                        "description": f"Commits replayed in this call (default {DEFAULT_MAX_COMMITS}); call again to go on",
                        "default": DEFAULT_MAX_COMMITS,
                    },
                },
                "required": ["repo", "branch", "expected_head"],
            },
            source=_SOURCE,
            priority=1,
        )
    ]
