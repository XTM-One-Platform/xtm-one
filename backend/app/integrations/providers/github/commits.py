"""GitHub commits tools - list/get commits, compare, blame, commit comments."""

from __future__ import annotations

import re
from typing import Any

from app.core.egress import egress_client
from app.tools.base import CallableToolExecutor, ToolDefinition, ToolResult

from . import _common
from ._common import _BASE_URL, _PROVIDER, _enc, _json_headers, _login, parse_repo

_SOURCE = "integration:github"

#: Lines of a file one blame call covers; the answer names the next window.
_MAX_BLAME_LINES = 400
#: Commits and changed files one compare answer lists; a longer list says "N of M shown".
_MAX_COMPARE_COMMITS = 30
_MAX_COMPARE_FILES = 50
#: GitHub's compare endpoint answers at most this many files, whatever changed.
_GITHUB_COMPARE_FILES_CAP = 300
#: Characters of a commit message get_github_commit shows unless full_message is set.
_COMMIT_MESSAGE_CHARS = 1_500
#: Characters of a commit subject shown on a blame line.
_BLAME_SUBJECT_CHARS = 72
#: A full or abbreviated commit SHA: no pull request is looked up for it.
_SHA_RE = re.compile(r"[0-9a-f]{7,40}")

_BLAME_QUERY = """
query($owner: String!, $name: String!, $expression: String!, $path: String!) {
  repository(owner: $owner, name: $name) {
    object(expression: $expression) {
      ... on Commit {
        oid
        blame(path: $path) {
          ranges {
            startingLine
            endingLine
            commit {
              oid
              committedDate
              messageHeadline
              author { name user { login } }
            }
          }
        }
      }
    }
  }
}
"""


def bound_commit_message(message: object, *, full: bool = False) -> str:
    """A commit message stripped, cut at ``_COMMIT_MESSAGE_CHARS`` with the rest counted unless *full*."""
    text = str(message or "").strip()
    if full or len(text) <= _COMMIT_MESSAGE_CHARS:
        return text
    rest = len(text) - _COMMIT_MESSAGE_CHARS
    return f"{text[:_COMMIT_MESSAGE_CHARS]}\n... [message cut: {rest:,} more characters; full_message=true reads it]"


def _blame_author(commit: dict) -> str:
    author = commit.get("author") if isinstance(commit.get("author"), dict) else {}
    user = author.get("user") if isinstance(author.get("user"), dict) else {}
    return str(user.get("login") or author.get("name") or "unknown")


def format_blame(ranges: list[dict], start_line: int, end_line: int) -> tuple[list[str], int]:
    """One line per blame range intersecting ``start_line..end_line``, clipped to it.

    Returns the lines and the last line of the file the blame covers (0 when
    no range was returned), so the caller can tell a window past the end.
    """
    lines: list[str] = []
    last = 0
    for item in ranges:
        if not isinstance(item, dict):
            continue
        first, final = int(item.get("startingLine") or 0), int(item.get("endingLine") or 0)
        last = max(last, final)
        lo, hi = max(first, start_line), min(final, end_line)
        if lo > hi:
            continue
        commit = item.get("commit") if isinstance(item.get("commit"), dict) else {}
        subject = str(commit.get("messageHeadline") or "").strip()
        if len(subject) > _BLAME_SUBJECT_CHARS:
            subject = subject[: _BLAME_SUBJECT_CHARS - 1] + "…"
        span = f"L{lo}" if lo == hi else f"L{lo}-{hi}"
        date = str(commit.get("committedDate") or "")[:10]
        lines.append(f"{span} {str(commit.get('oid') or '')[:8]} {_blame_author(commit)} {date} {subject}".rstrip())
    return lines, last


def _window_label(start: int, shown: int, total: int) -> str:
    """' (N of M shown, from K)' when a window holds less than the whole list, else ' (M)'."""
    if shown >= total and start == 0:
        return f" ({total})"
    if start:
        return f" ({shown} of {total} shown, from {start + 1})"
    return f" ({shown} of {total} shown)"


def _fmt_commit_line(c: dict) -> str:
    commit = c.get("commit") or {}
    msg = (commit.get("message") or "").splitlines()[0] if commit.get("message") else "(no message)"
    sha = (c.get("sha") or "")[:8]
    author = (commit.get("author") or {}).get("name") or _login(c.get("author"))
    date = ((commit.get("author") or {}).get("date") or "")[:10]
    return f"{sha} {msg} — {author} ({date})"


def register(
    headers: dict[str, str],
    executor: CallableToolExecutor,
    mark_self_action: Any,
) -> list[ToolDefinition]:
    """Build the GitHub commit tools and register their callables."""

    async def list_github_commits(
        repo: str,
        branch: str = "",
        path: str = "",
        author: str = "",
        since: str = "",
        until: str = "",
        count: int = 30,
    ) -> str:
        """List commits on a repository, optionally filtered by branch, file path, author, or date range."""
        owner, name, rerr = parse_repo(repo)
        if rerr:
            return rerr
        params: dict[str, Any] = {"per_page": max(1, min(count, 100))}
        if branch:
            params["sha"] = branch
        if path:
            params["path"] = path
        if author:
            params["author"] = author
        if since:
            params["since"] = since
        if until:
            params["until"] = until
        async with egress_client(timeout=30) as client:
            resp, err = await _common.safe_request(
                client,
                "get",
                f"{_BASE_URL}/repos/{owner}/{name}/commits",
                _PROVIDER,
                f"list commits in {repo}",
                headers=headers,
                params=params,
            )
            if err:
                return err
            commits = resp.json()
            return _common.format_entity_list(
                commits, _fmt_commit_line, noun="commit", empty_msg=f"No commits found in {repo}."
            )

    async def get_github_commit(repo: str, ref: str, full_message: bool = False) -> str:
        """Get a single commit's details: message (bounded unless *full_message*), author, stats, and changed files."""
        owner, name, rerr = parse_repo(repo)
        if rerr:
            return rerr
        async with egress_client(timeout=30) as client:
            resp, err = await _common.safe_request(
                client,
                "get",
                f"{_BASE_URL}/repos/{owner}/{name}/commits/{_enc(ref)}",
                _PROVIDER,
                f"get commit {ref} in {repo}",
                headers=headers,
            )
            if err:
                return err
            c = resp.json()
            commit = c.get("commit") or {}
            stats = c.get("stats") or {}
            files = c.get("files") or []
            author = commit.get("author") or {}
            lines = [
                f"Commit {(c.get('sha') or '')[:12]}: {bound_commit_message(commit.get('message'), full=full_message)}",
                f"Author: {author.get('name')} <{author.get('email')}> ({(author.get('date') or '')[:19]})",
                f"URL: {c.get('html_url', '')}",
                f"Changes: +{stats.get('additions', 0)} -{stats.get('deletions', 0)} across {len(files)} files",
            ]
            if files:
                lines.append("\nFiles:")
                for f in files[:50]:
                    icon = {"added": "+", "removed": "-", "modified": "~", "renamed": "→"}.get(f.get("status", ""), "?")
                    lines.append(f"  {icon} {f.get('filename')} (+{f.get('additions', 0)} -{f.get('deletions', 0)})")
            return "\n".join(lines)

    async def _ref_exists(client: Any, owner: str, name: str, ref: str) -> bool | None:
        """Whether *ref* resolves to a commit: True, False on a 404 or 422, None when unknown."""
        resp, err = await _common.safe_request(
            client,
            "get",
            f"{_BASE_URL}/repos/{owner}/{name}/commits/{_enc(ref)}",
            _PROVIDER,
            f"resolve {ref} in {owner}/{name}",
            headers=headers,
            expected_statuses={404, 422},
        )
        if not err:
            return True
        if resp is not None and resp.status_code in (404, 422):
            return False
        return None

    async def _last_pr_with_head(client: Any, owner: str, name: str, branch: str) -> dict | None:
        """The most recent pull request whose head branch is *branch*, or None."""
        if _SHA_RE.fullmatch(branch):
            return None
        resp, err = await _common.safe_request(
            client,
            "get",
            f"{_BASE_URL}/repos/{owner}/{name}/pulls",
            _PROVIDER,
            f"find the pull request of {branch} in {owner}/{name}",
            headers=headers,
            params={"head": f"{owner}:{branch}", "state": "all", "per_page": 1},
        )
        if err or resp is None:
            return None
        pulls = resp.json()
        if not isinstance(pulls, list) or not pulls or not isinstance(pulls[0], dict):
            return None
        pr = pulls[0]
        return {"number": pr.get("number"), "state": pr.get("state") or "", "merged": bool(pr.get("merged_at"))}

    async def _compare_not_found(client: Any, owner: str, name: str, base: str, head: str, err: str) -> str | ToolResult:
        """Explain a 404 on compare: a missing ref answers a result, anything else the error.

        A branch deleted after its pull request merged is an expected outcome
        for a caller that read the pull request open a moment earlier, so it
        is answered as ``ref_not_found`` (not an error) naming each missing ref
        and the last pull request that used it as head. A missing repository,
        or a ref whose existence cannot be read, keeps the error.
        """
        repo_resp, repo_err = await _common.safe_request(
            client,
            "get",
            f"{_BASE_URL}/repos/{owner}/{name}",
            _PROVIDER,
            f"read {owner}/{name}",
            headers=headers,
        )
        if repo_err or repo_resp is None:
            return err
        missing: list[str] = []
        for ref in dict.fromkeys((base, head)):
            exists = await _ref_exists(client, owner, name, ref)
            if exists is None:
                return err
            if not exists:
                missing.append(ref)
        if not missing:
            return err
        pulls: dict[str, dict] = {}
        for ref in missing:
            pr = await _last_pr_with_head(client, owner, name, ref)
            if pr:
                pulls[ref] = pr
        lines = []
        for ref in missing:
            role = " and ".join(r for r, v in (("base", base), ("head", head)) if v == ref)
            line = f"ref_not_found: {ref} ({role}) does not exist in {owner}/{name}."
            pr = pulls.get(ref)
            if pr:
                outcome = "merged" if pr["merged"] else pr["state"]
                line += f" Last pull request with it as head: #{pr['number']} ({outcome})."
            lines.append(line)
        lines.append("Nothing to compare: a branch deleted after its pull request merged or closed is the usual cause.")
        return ToolResult(
            tool_call_id="",
            content="\n".join(lines),
            structured={
                "status": "ref_not_found",
                "missing": missing,
                "pull_requests": pulls,
                "merge_base": "",
            },
        )

    async def compare_github_commits(repo: str, base: str, head: str, file_offset: int = 0) -> str | ToolResult:
        """Compare two commits/branches/tags (base...head): ahead/behind counts, commits, and changed files.

        The commit and file lists are windows of what GitHub answered: when a
        window holds less than the whole list the answer says "N of M shown"
        and how to read the rest (``file_offset`` for the files), so the
        header count and the listed entries never disagree silently.

        The result's ``structured`` carries ``merge_base``, ``status``,
        ``ahead_by``, ``behind_by``, the changed ``files`` (``filename``,
        ``status``, ``previous_filename``, every file GitHub listed) and
        ``files_capped`` (GitHub's 300-file cap reached) for a caller reading
        it without the text (``sandbox_checkout_rebase``: the files a branch
        changes outside a narrow checkout). A ref that does not exist (a
        branch deleted after its pull request merged) answers a non-error
        ``ref_not_found`` result; a missing repository stays an error.
        """
        owner, name, rerr = parse_repo(repo)
        if rerr:
            return rerr
        async with egress_client(timeout=30) as client:
            resp, err = await _common.safe_request(
                client,
                "get",
                f"{_BASE_URL}/repos/{owner}/{name}/compare/{_enc(base)}...{_enc(head)}",
                _PROVIDER,
                f"compare {base}...{head} in {repo}",
                headers=headers,
                expected_statuses={404},
            )
            if err:
                if resp is not None and resp.status_code == 404:
                    return await _compare_not_found(client, owner, name, base, head, err)
                return err
            try:
                start = max(0, int(file_offset or 0))
            except (TypeError, ValueError):
                return "Error: 'file_offset' must be an integer."
            data = resp.json()
            commits = data.get("commits") or []
            files = data.get("files") or []
            lines = [
                f"Comparison {base}...{head} in {repo}: {data.get('status', '?')}",
                f"Ahead by {data.get('ahead_by', 0)}, behind by {data.get('behind_by', 0)} "
                f"({len(commits)} commits, {len(files)} files changed)",
                f"URL: {data.get('html_url', '')}",
            ]
            merge_base = (data.get("merge_base_commit") or {}).get("sha") if isinstance(data.get("merge_base_commit"), dict) else ""
            if merge_base:
                lines.insert(2, f"Merge base: {merge_base[:12]}")
            if commits:
                lines.append("\nCommits:" + _window_label(0, min(len(commits), _MAX_COMPARE_COMMITS), len(commits)))
                lines.extend(f"  {_fmt_commit_line(c)}" for c in commits[:_MAX_COMPARE_COMMITS])
                if len(commits) > _MAX_COMPARE_COMMITS:
                    rest = len(commits) - _MAX_COMPARE_COMMITS
                    lines.append(f"  ... list_github_commits(repo, branch='{head}') lists the {rest} not shown.")
            if files:
                window = files[start : start + _MAX_COMPARE_FILES]
                lines.append("\nChanged files:" + _window_label(start, len(window), len(files)))
                for f in window:
                    lines.append(f"  {f.get('status', '?')[:1].upper()} {f.get('filename')} (+{f.get('additions', 0)} -{f.get('deletions', 0)})")
                if start + len(window) < len(files):
                    lines.append(f"  ... call again with file_offset={start + len(window)} for the next files.")
                if len(files) >= _GITHUB_COMPARE_FILES_CAP:
                    lines.append(
                        f"  GitHub lists at most {_GITHUB_COMPARE_FILES_CAP} files in a comparison: more may have changed; "
                        "materialize_repository at each ref (or get_github_pr_files on its pull request) reads them all."
                    )
            return ToolResult(
                tool_call_id="",
                content="\n".join(lines),
                structured={
                    "merge_base": merge_base or "",
                    "status": data.get("status") or "",
                    "ahead_by": data.get("ahead_by", 0),
                    "behind_by": data.get("behind_by", 0),
                    "files": [
                        {
                            "filename": str(f.get("filename") or ""),
                            "status": str(f.get("status") or ""),
                            "previous_filename": str(f.get("previous_filename") or ""),
                        }
                        for f in files
                        if isinstance(f, dict) and f.get("filename")
                    ],
                    "files_capped": len(files) >= _GITHUB_COMPARE_FILES_CAP,
                },
            )

    async def get_github_blame(repo: str, path: str, ref: str = "", start_line: int = 1, end_line: int = 0) -> str:
        """Who last changed each line of a file range, in which commit, and why."""
        owner, name, rerr = parse_repo(repo)
        if rerr:
            return rerr
        file_path = str(path or "").strip().lstrip("/")
        if not file_path:
            return "Error: 'path' is required (a file path from the repository root)."
        try:
            start = max(1, int(start_line or 1))
            end = int(end_line or 0)
        except (TypeError, ValueError):
            return "Error: 'start_line' and 'end_line' must be integers."
        if end and end < start:
            return f"Error: 'end_line' ({end}) is before 'start_line' ({start})."
        window_end = start + _MAX_BLAME_LINES - 1
        stop = min(end, window_end) if end else window_end
        expression = str(ref or "").strip() or "HEAD"
        async with egress_client(timeout=60) as client:
            data, err = await _common.gh_graphql(
                client, headers, _BLAME_QUERY,
                {"owner": owner, "name": name, "expression": expression, "path": file_path},
                action=f"blame {file_path} at {expression} in {repo}",
            )
        if err:
            return err
        obj = ((data or {}).get("repository") or {}).get("object")
        if not isinstance(obj, dict) or not obj.get("oid"):
            return f"Error: ref '{expression}' is not a commit of {repo}."
        blame = obj.get("blame") if isinstance(obj.get("blame"), dict) else {}
        ranges = [r for r in blame.get("ranges") or [] if isinstance(r, dict)]
        if not ranges:
            return f"Error: no file '{file_path}' at {str(obj['oid'])[:8]} in {repo} (or it is empty or binary)."
        lines, last = format_blame(ranges, start, stop)
        if not lines:
            return f"Error: {file_path} has {last} lines at {str(obj['oid'])[:8]}; line {start} is past its end."
        shown_end = min(stop, last)
        head = f"Blame of {repo}:{file_path} at {str(obj['oid'])[:8]}, lines {start}-{shown_end} of {last}:"
        tail: list[str] = []
        wanted_end = min(end, last) if end else last
        if shown_end < wanted_end:
            tail.append(f"Lines {shown_end + 1}-{wanted_end} not shown: call again with start_line={shown_end + 1}.")
        return "\n".join([head, *lines, *tail])

    async def create_github_commit_comment(repo: str, commit_sha: str, body: str, path: str = "", line: int = 0) -> str:
        """Add a comment to a commit, optionally anchored to a file path and line."""
        owner, name, rerr = parse_repo(repo)
        if rerr:
            return rerr
        payload: dict[str, Any] = {"body": body}
        if path:
            payload["path"] = path
        if line:
            payload["line"] = line
        async with egress_client(timeout=30) as client:
            resp, err = await _common.safe_request(
                client,
                "post",
                f"{_BASE_URL}/repos/{owner}/{name}/commits/{_enc(commit_sha)}/comments",
                _PROVIDER,
                f"comment on commit {commit_sha} in {repo}",
                headers=_json_headers(headers),
                json=payload,
            )
            if err:
                return err
            return f"Comment added to commit {commit_sha[:8]}: {(resp.json() or {}).get('html_url', '')}"

    executor.register("list_github_commits", list_github_commits)
    executor.register("get_github_commit", get_github_commit)
    executor.register("compare_github_commits", compare_github_commits)
    executor.register("get_github_blame", get_github_blame)
    executor.register("create_github_commit_comment", create_github_commit_comment)

    return [
        ToolDefinition(
            name="list_github_commits",
            description=(
                "List commits on a repository. Filter by branch (sha), file path (commits touching that file), "
                "author (login), and date range (since/until, ISO 8601). Returns short SHA, summary, author, and date."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "repo": {"type": "string", "description": "Repository in format owner/name"},
                    "branch": {"type": "string", "description": "Branch, tag, or SHA to list commits from (default branch if omitted)", "default": ""},
                    "path": {"type": "string", "description": "Only commits that touch this file path (optional)", "default": ""},
                    "author": {"type": "string", "description": "Filter by author login or email (optional)", "default": ""},
                    "since": {"type": "string", "description": "ISO 8601 timestamp lower bound (optional)", "default": ""},
                    "until": {"type": "string", "description": "ISO 8601 timestamp upper bound (optional)", "default": ""},
                    "count": {"type": "integer", "description": "Max commits (max 100)", "default": 30},
                },
                "required": ["repo"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="get_github_commit",
            description="Get a single commit's details: message, author, addition/deletion stats, and the list of changed files. ref is a SHA, branch, or tag.",
            input_schema={
                "type": "object",
                "properties": {
                    "repo": {"type": "string", "description": "Repository in format owner/name"},
                    "ref": {"type": "string", "description": "Commit SHA, branch, or tag"},
                    "full_message": {"type": "boolean", "description": "The whole message (default: 1,500 chars)"},
                },
                "required": ["repo", "ref"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="compare_github_commits",
            description=(
                "Compare two refs (base...head) — branches, tags, or SHAs. Returns ahead/behind counts, the list of "
                "commits between them, and the changed files (50 at a time: 'N of M shown' and file_offset for the "
                "rest). Useful to see what a branch adds over main, or what a release includes."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "repo": {"type": "string", "description": "Repository in format owner/name"},
                    "base": {"type": "string", "description": "Base ref (e.g. 'main')"},
                    "head": {"type": "string", "description": "Head ref (e.g. 'feature/x' or a tag)"},
                    "file_offset": {
                        "type": "integer",
                        "description": "First changed file to list (default 0); the answer names the next one when more remain.",
                    },
                },
                "required": ["repo", "base", "head"],
            },
            source=_SOURCE,
            priority=0,
            repository_compare=True,
            # "compare" is no verb the name classifier reads: declared, so a resumed turn may repeat it.
            read_only=True,
        ),
        ToolDefinition(
            name="get_github_blame",
            description=(
                "Blame a file: for each range of lines, the commit that last changed it (short SHA, author, date, "
                "subject), read server-side, so a checkout without history still answers who changed a line and "
                f"why. At most {_MAX_BLAME_LINES} lines per call from start_line (end_line 0: to the end); the "
                "answer names the next start_line when more remains. ref: branch, tag or SHA (default branch when empty)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "repo": {"type": "string", "description": "Repository in format owner/name"},
                    "path": {"type": "string", "description": "File path from the repository root"},
                    "ref": {"type": "string", "description": "Branch, tag or SHA (default: the default branch)", "default": ""},
                    "start_line": {"type": "integer", "description": "First line (1-based, default 1)", "default": 1},
                    "end_line": {"type": "integer", "description": "Last line (0: to the end of the file)", "default": 0},
                },
                "required": ["repo", "path"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="create_github_commit_comment",
            description="Add a comment to a commit. Optionally anchor it to a file 'path' and 'line' for inline review feedback on a commit.",
            input_schema={
                "type": "object",
                "properties": {
                    "repo": {"type": "string", "description": "Repository in format owner/name"},
                    "commit_sha": {"type": "string", "description": "The commit SHA to comment on"},
                    "body": {"type": "string", "description": "Comment text (Markdown)"},
                    "path": {"type": "string", "description": "File path to anchor the comment to (optional)", "default": ""},
                    "line": {"type": "integer", "description": "Line number in the file (optional)", "default": 0},
                },
                "required": ["repo", "commit_sha", "body"],
            },
            source=_SOURCE,
            priority=1,
        ),
    ]
