"""GitHub issues tools - search/read/create/update issues, comments, labels, milestones,
sub-issues, locking, and coding-agent assignment."""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import quote as urlquote

import httpx

from app.core.egress import egress_client
from app.services.pii_redactor import redact_pii
from app.tools.base import CallableToolExecutor, FileContext, ToolDefinition

from . import _common, attachments, body_edits, pr_inherit, pr_title
from ._common import (
    _BASE_URL,
    _GRAPHQL_URL,
    _ITEM_PREVIEW_CHARS,
    _PROVIDER,
    _body_preview,
    _coerce_int,
    _json_headers,
    _label_str,
    _login,
    body_section,
    format_pagination_hint,
    one_line,
    parse_repo,
    window_args_error,
)
from .landed import comment_key, comment_probe, issue_key, issue_probe

#: Comments a default issue read lists, one line each.
_DEFAULT_COMMENTS = 10
#: The comment page size of an issue read; include_body reads the first page.
_COMMENT_PAGE = 100
#: Comment pages ``add_github_comment(dedupe_first_line=true)`` reads at most (1,000 comments).
_DEDUPE_MAX_PAGES = 10

logger = logging.getLogger(__name__)

_SOURCE = "integration:github"

#: Coding-agent bot logins that must be assigned through the GraphQL
#: agent-assignment flow, not the plain issue ``assignees`` array.
_AGENT_LOGINS = {"copilot", "copilot-swe-agent", "claude", "anthropic-code-agent", "codex"}


def comment_id_of(comment: Any) -> int | None:
    """The numeric id of an issue comment given as its id or its link (``...#issuecomment-N``)."""
    text = str(comment if comment is not None else "").strip()
    if "#issuecomment-" in text:
        text = text.rsplit("#issuecomment-", 1)[1]
    elif "/issues/comments/" in text:
        text = text.rsplit("/issues/comments/", 1)[1]
    return int(text) if text.isdigit() and int(text) > 0 else None


def _number_of_issue_url(issue_url: str) -> int | None:
    """The issue or pull request number at the end of a comment's ``issue_url``."""
    tail = issue_url.rstrip("/").rsplit("/", 1)[-1]
    return int(tail) if tail.isdigit() else None


def first_line(body: str | None) -> str:
    """The first non-empty line of a comment body, stripped (``""`` for an empty body)."""
    return next((line.strip() for line in (body or "").splitlines() if line.strip()), "")


def _normalised(body: str | None) -> str:
    """*body* as compared for a duplicate: line ends unified, trailing spaces and outer blank lines dropped."""
    return "\n".join(line.rstrip() for line in (body or "").strip().splitlines())


def already_posted(stored: str | None, body: str | None) -> bool:
    """Whether the *stored* comment is *body* posted before (attachment lines appended after it allowed).

    The first line is only a pre-filter: a review-loop header such as ``UX-LOOP round=3 verdict=APPROVE``
    repeats across heads and carried verdicts, so a comment that shares it alone is not a duplicate.
    """
    wanted = _normalised(body)
    if not wanted or first_line(stored) != first_line(body):
        return False
    have = _normalised(stored)
    return have == wanted or have.startswith(wanted + "\n")


async def _resolve_issue_id(
    client: httpx.AsyncClient, owner: str, name: str, number: int, headers: dict[str, str]
) -> tuple[int | None, str | None]:
    """Resolve an issue NUMBER to its internal database ID (sub-issue endpoints need the id)."""
    resp, err = await _common.safe_request(
        client,
        "get",
        f"{_BASE_URL}/repos/{owner}/{name}/issues/{number}",
        _PROVIDER,
        f"resolve issue #{number}",
        headers=headers,
    )
    if err:
        return None, err
    issue_id = (resp.json() or {}).get("id")
    if not isinstance(issue_id, int):
        return None, f"Error: Could not resolve internal ID for issue #{number}."
    return issue_id, None


async def _resolve_issue_type(
    client: httpx.AsyncClient, owner: str, name: str, issue_type: str, headers: dict[str, str]
) -> tuple[str | None, str | None]:
    """Resolve an issue type NAME (case-insensitive) to the exact name GitHub stores.

    Issue types are defined on the organisation that owns the repository and
    read through the repository (GraphQL ``repository.issueTypes``, which needs
    no organisation permission). The lookup runs before any write, so an unknown
    name is refused with the list of the available types and nothing is created
    or changed. The REST issue endpoints then take the name in ``type``.
    """
    repo = f"{owner}/{name}"
    wanted = issue_type.strip()
    query = (
        "query($o:String!,$n:String!){repository(owner:$o,name:$n){"
        "issueTypes(first:50){nodes{name isEnabled}}}}"
    )
    resp, err = await _common.safe_request(
        client,
        "post",
        _GRAPHQL_URL,
        _PROVIDER,
        f"list issue types of {repo}",
        headers=_json_headers(headers),
        json={"query": query, "variables": {"o": owner, "n": name}},
    )
    if err:
        return None, err
    payload = (resp.json() if resp is not None else None) or {}
    # Any GraphQL error refuses the write, partial data included: a type list
    # read through an error may be incomplete, so a name is never matched on it.
    if payload.get("errors"):
        msg = "; ".join(str(e.get("message", e)) for e in payload["errors"] if isinstance(e, dict)) or "unknown error"
        return None, f"Error: could not read the issue types of {repo}: {msg}. Nothing was written."
    repository = (payload.get("data") or {}).get("repository") or {}
    nodes = (repository.get("issueTypes") or {}).get("nodes") or []
    names = [
        str(t["name"]) for t in nodes if isinstance(t, dict) and t.get("name") and t.get("isEnabled", True) is not False
    ]
    if not names:
        return None, (
            f"Error: {repo} has no issue types (its owner {owner} has none enabled). "
            "Retry without issue_type. Nothing was written."
        )
    for n in names:
        if n.lower() == wanted.lower():
            return n, None
    available = ", ".join(names) or "none"
    return None, f"Error: unknown issue type {wanted!r} for {repo}. Available issue types: {available}. Nothing was written."


def _type_suffix(issue: dict[str, Any]) -> str:
    """' [type Feature]' when GitHub answered an issue type, else ''."""
    t = issue.get("type")
    name = t.get("name") if isinstance(t, dict) else None
    return f" [type {name}]" if name else ""


def _milestone_given(milestone: int | str | None) -> bool:
    """Whether the caller named a milestone (a number or a non-blank title)."""
    return milestone is not None and bool(str(milestone).strip())


def _milestone_suffix(title: str) -> str:
    """' in milestone '1. Now'' once a milestone was resolved and set, else ''."""
    return f" in milestone '{title}'" if title else ""


def register(
    headers: dict[str, str],
    executor: CallableToolExecutor,
    mark_self_action: Any,
) -> list[ToolDefinition]:
    """Build the GitHub issue tools and register their callables."""

    async def search_github_issues(
        repo: str,
        query: str = "",
        state: str = "open",
        labels: str = "",
        assignee: str = "",
        count: int = 20,
        page: int = 1,
    ) -> str:
        """Search issues in a GitHub repository with filters.

        Returns issue number, title, labels, assignee, and a body preview
        so you can judge relevance without reading the full issue.
        """
        if e := _common.repo_error(repo):
            return e
        async with egress_client(timeout=30) as client:
            q = f"repo:{repo} is:issue state:{state}"
            if query:
                q += f" {query}"
            if labels:
                for label in labels.split(","):
                    q += f' label:"{label.strip()}"'
            if assignee:
                q += f" assignee:{assignee}"
            per_page = max(1, min(count, 100))
            page = max(1, page)
            resp, err = await _common.safe_request(
                client,
                "get",
                f"{_BASE_URL}/search/issues",
                _PROVIDER,
                f"search issues in {repo}",
                headers=headers,
                params={"q": q, "per_page": per_page, "page": page},
            )
            if err:
                return err
            data = resp.json()
            items = data.get("items", [])
            total = data.get("total_count", 0)
            capped_total = min(total, 1000)
            start = (page - 1) * per_page + 1
            # The page's extent drives pagination; under a repository Scope(s)
            # the results of another repository (a query widened on GitHub's
            # side) are dropped, and so is GitHub's total over that search.
            end = start + len(items) - 1
            scoped = _common.active_repo_scope() is not None
            if scoped:
                items = [
                    i for i in items
                    if isinstance(i, dict)
                    and _common.in_repo_scope(_common.repo_from_api_url(i.get("repository_url")))
                ]
            if not items:
                if page > 1:
                    if total > 1000 and start > 1000:
                        return (
                            f"No issues found on page {page}. "
                            "Note: GitHub Search API caps accessible results to the first 1000 matches."
                        )
                    return f"No issues found on page {page}."
                return "No issues found."
            lines = [
                f"Found {len(items)} issues in this Scope(s) on page {page}:"
                if scoped
                else f"Found {total} issues (showing {start}-{end} on page {page}):"
            ]
            for i in items:
                assignees = ", ".join(a["login"] for a in i.get("assignees", []))
                assignee_str = f" → {assignees}" if assignees else ""
                entry = f"#{i['number']} {i['title']} [{i['state']}]{_label_str(i.get('labels', []))}{assignee_str} by {i['user']['login']}"
                entry += _body_preview(i.get("body"))
                lines.append(entry)
            if total > 1000:
                lines.append("\nNote: GitHub Search API caps accessible results to the first 1000 matches.")
            if end < capped_total:
                lines.append(format_pagination_hint(str(page + 1), "page"))
            return "\n\n".join(lines)

    async def get_github_issue(
        repo: str,
        number: int,
        include_body: bool = False,
        offset: int = 0,
        max_chars: int = 0,
        include_comments: bool = False,
    ) -> str:
        """Read an issue: its fields, the body's head and one line per recent comment.

        ``include_body`` reads a body window; ``include_comments`` the comments
        whole (first page). The two are apart so a body window never carries
        every comment of a long-lived issue.
        """
        owner, name, rerr = parse_repo(repo)
        if rerr:
            return rerr
        start, size, werr = window_args_error(offset, max_chars)
        if werr:
            return werr
        include = include_body is True or str(include_body).lower() == "true"
        whole_comments = include_comments is True or str(include_comments).lower() == "true"
        async with egress_client(timeout=30) as client:
            resp, err = await _common.safe_request(
                client,
                "get",
                f"{_BASE_URL}/repos/{owner}/{name}/issues/{number}",
                _PROVIDER,
                f"read issue #{number} in {repo}",
                headers=headers,
                expected_statuses={404},
            )
            if err:
                if resp is not None and resp.status_code == 404:
                    # Return a "not found" message (no leading "Error" so the agent
                    # loop does not log it at WARNING level) with explicit guidance to
                    # use search_github_issues instead of guessing/incrementing IDs.
                    return (
                        f"Issue #{number} not found in {repo}. "
                        "Do not guess or increment issue numbers — use search_github_issues "
                        "to discover real, existing issue numbers for this repository."
                    )
                return err
            issue = resp.json()
            comment_total = _coerce_int(issue.get("comments")) or 0

            async def comment_page(page: int) -> list[dict]:
                got, _ = await _common.safe_request(
                    client,
                    "get",
                    f"{_BASE_URL}/repos/{owner}/{name}/issues/{number}/comments",
                    _PROVIDER,
                    f"fetch comments for issue #{number}",
                    headers=headers,
                    params={"per_page": _COMMENT_PAGE, "page": page},
                )
                data = got.json() if got is not None else []
                return data if isinstance(data, list) else []

            # GitHub lists comments oldest first: the default view
            # reads the LAST page (and the one before when it holds fewer than
            # the latest _DEFAULT_COMMENTS), include_comments the first page.
            last_page = max(1, -(-comment_total // _COMMENT_PAGE))
            if whole_comments or last_page == 1:
                comments = await comment_page(1)
            else:
                comments = await comment_page(last_page)
                if len(comments) < _DEFAULT_COMMENTS:
                    comments = (await comment_page(last_page - 1)) + comments
            comment_total = max(comment_total, len(comments))
            lines = [
                f"Issue #{number}: {issue['title']}",
                f"State: {issue['state']}",
                f"Author: {issue['user']['login']}",
                f"Created: {issue['created_at']}",
                f"Updated: {issue['updated_at']}",
                f"URL: {issue['html_url']}",
            ]
            if issue.get("closed_at"):
                lines.append(f"Closed: {issue['closed_at']}")
            if issue.get("assignees"):
                lines.append(f"Assignees: {', '.join(a['login'] for a in issue['assignees'])}")
            if issue.get("labels"):
                lines.append(f"Labels: {', '.join(lb['name'] for lb in issue['labels'])}")
            if issue.get("milestone"):
                lines.append(f"Milestone: {issue['milestone']['title']}")
            sub = issue.get("sub_issues_summary") or {}
            if sub.get("total"):
                lines.append(f"Sub-issues: {sub.get('completed', 0)}/{sub.get('total')} completed")
            lines.append(body_section(issue.get("body"), include=include, offset=start, size=size, what="issue"))
            if comments and whole_comments:
                window = f", the first {len(comments)}" if comment_total > len(comments) else ""
                lines.append(f"\nComments ({comment_total}{window}):")
                for c in comments:
                    lines.append(f"\n--- {_login(c.get('user'))} ({c.get('created_at', '?')}) ---")
                    lines.append(c.get("body") or "")
                if window:
                    lines.append(
                        f"\n({comment_total - len(comments)} later comment(s) not shown; the default read lists "
                        "the latest ones)"
                    )
            elif comments:
                shown = comments[-_DEFAULT_COMMENTS:]
                older = comment_total - len(shown)
                head = f"\nComments ({comment_total}" + (f", the latest {len(shown)}" if older else "") + "):"
                lines.append(head)
                for c in shown:
                    preview = one_line(c.get("body"))
                    lines.append(f"  - {_login(c.get('user'))} ({c.get('created_at', '?')}): {preview}")
                if older or any(len(c.get("body") or "") > _ITEM_PREVIEW_CHARS for c in shown):
                    lines.append("  (comments cut; include_comments=true shows every comment whole)")
            else:
                lines.append("\nNo comments.")
            return "\n".join(lines)

    async def create_github_issue(
        repo: str,
        title: str,
        body: str = "",
        labels: list[str] | None = None,
        assignees: list[str] | None = None,
        milestone: int | str | None = None,
        issue_type: str = "",
    ) -> str:
        """Create a new issue in a repository, optionally with its issue type.

        The title is checked first against the ``[issue]`` rule the repository
        declares on its default branch (:mod:`.pr_title`): a title that breaks it
        is refused with the rule named and nothing is written. ``milestone`` is a
        number or an exact title, resolved on the repository
        (:func:`.pr_inherit.resolve_milestone`): an unknown one refuses, nothing
        written, and a milestone is never created.
        """
        owner, name, rerr = parse_repo(repo)
        if rerr:
            return rerr
        # PII redaction: scan title and body for PII before writing to GitHub.
        # Detected entities are replaced with typed placeholders (e.g. [EMAIL]).
        title_result = redact_pii(title)
        body_result = redact_pii(body) if body else None
        if title_result.was_redacted or (body_result is not None and body_result.was_redacted):
            redaction_summary: dict[str, int] = {}
            for entity_type, count in title_result.summary.items():
                redaction_summary[f"title_{entity_type}"] = count
            if body_result is not None:
                for entity_type, count in body_result.summary.items():
                    redaction_summary[f"body_{entity_type}"] = redaction_summary.get(f"body_{entity_type}", 0) + count
            logger.warning("PII redacted before creating GitHub issue in %s: %s", repo, redaction_summary)
        title = title_result.redacted_text
        if body_result is not None:
            body = body_result.redacted_text
        async with egress_client(timeout=30) as client:
            refusal, title_note = await pr_title.checked_title(client, owner, name, "", title, headers, pr_title.ISSUE)
            if refusal:
                return refusal
            payload: dict[str, Any] = {"title": title}
            if body:
                payload["body"] = body
            if labels:
                payload["labels"] = labels
            if assignees:
                payload["assignees"] = assignees
            milestone_title = ""
            if _milestone_given(milestone):
                milestone_number, milestone_title, refusal = await pr_inherit.resolve_milestone(
                    client, owner, name, str(milestone), headers
                )
                if refusal:
                    return refusal
                payload["milestone"] = milestone_number
            if issue_type and issue_type.strip():
                type_name, terr = await _resolve_issue_type(client, owner, name, issue_type, headers)
                if terr:
                    return terr
                payload["type"] = type_name
            resp, err = await _common.safe_request(
                client,
                "post",
                f"{_BASE_URL}/repos/{repo}/issues",
                _PROVIDER,
                f"create issue in {repo}",
                headers=_json_headers(headers),
                json=payload,
            )
            if err:
                return err
            issue = resp.json()
            await mark_self_action(repo, issue.get("number"))
            created = (
                f"Issue #{issue['number']} created{_type_suffix(issue)}{_milestone_suffix(milestone_title)}: "
                f"{issue['html_url']}"
            )
            return f"{created}\n{title_note}" if title_note else created

    async def update_github_issue(
        repo: str,
        number: int,
        state: str = "",
        title: str = "",
        body: str = "",
        labels: list[str] | None = None,
        assignees: list[str] | None = None,
        milestone: int | str | None = None,
        state_reason: str = "",
        issue_type: str = "",
        edits: list[dict[str, Any]] | None = None,
        expect_updated_at: str = "",
    ) -> str:
        """Update an existing issue. Only provided fields are changed.

        ``edits`` change the body in place (:mod:`.body_edits`) instead of a
        whole ``body``; ``expect_updated_at`` refuses a write over a newer edit.

        Use state='closed' to close, state='open' to reopen.
        state_reason can be 'completed' or 'not_planned' when closing.
        issue_type sets the issue type by name (e.g. Feature, Bug, Task).
        milestone is a number or an exact title, resolved as create_github_issue does.
        A new title is checked against the repository's ``[issue]`` title rule
        on the default branch, as create_github_issue does.
        """
        owner, name, rerr = parse_repo(repo)
        if rerr:
            return rerr
        if assignees is not None:
            bots = [a for a in assignees if a.lower() in _AGENT_LOGINS]
            if bots:
                return (
                    f"Cannot assign coding agents ({', '.join(bots)}) via update_github_issue. "
                    f'Use the assign_coding_agent_to_issue tool instead with agent="{bots[0]}".'
                )
        if edits and body:
            return "Error: give 'body' or 'edits', not both. Nothing was written."
        payload: dict[str, Any] = {}
        if state:
            payload["state"] = state
        if title:
            payload["title"] = title
        if body:
            payload["body"] = body
        if labels is not None:
            payload["labels"] = labels
        if assignees is not None:
            payload["assignees"] = assignees
        if state_reason:
            payload["state_reason"] = state_reason
        want_type = bool(issue_type and issue_type.strip())
        want_milestone = _milestone_given(milestone)
        if not payload and not want_type and not want_milestone and not edits:
            return "No fields to update."
        async with egress_client(timeout=30) as client:
            title_note = None
            if title:
                refusal, title_note = await pr_title.checked_title(
                    client, owner, name, "", title, headers, pr_title.ISSUE
                )
                if refusal:
                    return refusal
            diff = ""
            if edits or expect_updated_at:
                new_body, diff, eerr = await body_edits.edited_body(
                    client,
                    f"{_BASE_URL}/repos/{owner}/{name}/issues/{number}",
                    headers,
                    edits,
                    expect_updated_at,
                    f"issue #{number} in {repo}",
                )
                if eerr:
                    return eerr
                if edits:
                    payload["body"] = new_body
            if want_type:
                type_name, terr = await _resolve_issue_type(client, owner, name, issue_type, headers)
                if terr:
                    return terr
                payload["type"] = type_name
            milestone_title = ""
            if want_milestone:
                milestone_number, milestone_title, refusal = await pr_inherit.resolve_milestone(
                    client, owner, name, str(milestone), headers
                )
                if refusal:
                    return refusal
                payload["milestone"] = milestone_number
            resp, err = await _common.safe_request(
                client,
                "patch",
                f"{_BASE_URL}/repos/{repo}/issues/{number}",
                _PROVIDER,
                f"update issue #{number} in {repo}",
                headers=_json_headers(headers),
                json=payload,
            )
            if err:
                return err
            issue = resp.json()
            await mark_self_action(repo, number)
            updated = (
                f"Issue #{number} updated [{issue['state']}]{_type_suffix(issue)}"
                f"{_milestone_suffix(milestone_title)}: {issue['html_url']}"
            )
            if title_note:
                updated = f"{updated}\n{title_note}"
            return f"{updated}\nBody diff:\n{diff}" if diff else updated

    async def add_github_comment(
        repo: str,
        issue_number: int,
        body: str,
        attachment_file_ids: list[str] | None = None,
        dedupe_first_line: bool = False,
        file_context: FileContext | None = None,
    ) -> str:
        """Add a comment to an issue or pull request, with files of the run attached (:mod:`.attachments`).

        ``dedupe_first_line``: the comments already on it are read server side and,
        when one opens with the same first non-empty line as ``body`` and holds the
        same body (:func:`already_posted`), nothing is committed or posted and that
        comment is named instead (a retried run never posts a verdict twice, and no
        prior comment body enters the context). A comment that shares only the first
        line, a verdict header of an older round for example, does not stop the post.
        """
        if e := _common.repo_error(repo):
            return e
        async with egress_client(timeout=60 if attachment_file_ids else 30) as client:
            if dedupe_first_line:
                existing, err = await _comment_with_first_line(client, repo, issue_number, body)
                if err:
                    return err
                if existing:
                    return existing
            lines, err = await attachments.attach_files(
                client, headers, repo, issue_number, attachment_file_ids, file_context
            )
            if err:
                return err
            body = attachments.compose_body(body, lines)
            resp, err = await _common.safe_request(
                client,
                "post",
                f"{_BASE_URL}/repos/{repo}/issues/{issue_number}/comments",
                _PROVIDER,
                f"add comment to #{issue_number} in {repo}",
                headers=_json_headers(headers),
                json={"body": body},
            )
            if err:
                return err
            comment = resp.json()
            await mark_self_action(repo, issue_number)
            stored = f" with {len(lines)} attachment(s) stored on branch {attachments.ASSETS_BRANCH}" if lines else ""
            return f"Comment added{stored}: {comment['html_url']}"

    async def update_github_comment(
        repo: str,
        comment: str,
        body: str = "",
        edits: list[dict[str, Any]] | None = None,
        expect_updated_at: str = "",
    ) -> str:
        """Change a comment of an issue or pull request in place.

        ``comment`` is the comment's id or its link (``...#issuecomment-N``).
        ``edits`` are find/replace edits on the body as stored (:mod:`.body_edits`),
        so a correction never resends the whole comment; ``body`` replaces it whole.
        ``expect_updated_at`` refuses a write over a newer edit.
        """
        if e := _common.repo_error(repo):
            return e
        comment_id = comment_id_of(comment)
        if comment_id is None:
            return (
                "Error: 'comment' is the comment's id or its link ending in #issuecomment-N "
                f"(got {str(comment)[:80]!r}). Nothing was written."
            )
        if edits and body:
            return "Error: give 'body' or 'edits', not both. Nothing was written."
        if not edits and not body:
            return "Error: give 'body' or 'edits'. Nothing was written."
        url = f"{_BASE_URL}/repos/{repo}/issues/comments/{comment_id}"
        what = f"comment {comment_id} in {repo}"
        async with egress_client(timeout=30) as client:
            diff = ""
            if edits or expect_updated_at:
                new_body, diff, eerr = await body_edits.edited_body(
                    client, url, headers, edits, expect_updated_at, what
                )
                if eerr:
                    return eerr
                if edits:
                    body = new_body
            resp, err = await _common.safe_request(
                client,
                "patch",
                url,
                _PROVIDER,
                f"update {what}",
                headers=_json_headers(headers),
                json={"body": body},
            )
            if err:
                return err
            stored = resp.json() or {}
            number = _number_of_issue_url(str(stored.get("issue_url") or ""))
            if number is not None:
                await mark_self_action(repo, number)
            updated = f"Comment updated: {stored.get('html_url', '')}"
            return f"{updated}\nBody diff:\n{diff}" if diff else updated

    async def _comment_with_first_line(client: Any, repo: str, issue_number: int, body: str) -> tuple[str, str | None]:
        """The comment of ``issue_number`` that already holds ``body`` (:func:`already_posted`), as an answer line, or ``""``."""
        wanted = first_line(body)
        if not wanted:
            return "", None
        for page in range(1, _DEDUPE_MAX_PAGES + 1):
            resp, err = await _common.safe_request(
                client,
                "get",
                f"{_BASE_URL}/repos/{repo}/issues/{issue_number}/comments?per_page={_COMMENT_PAGE}&page={page}",
                _PROVIDER,
                f"read the comments of #{issue_number} in {repo}",
                headers=headers,
            )
            if err:
                return "", err
            comments = resp.json() or []
            for comment in comments:
                if already_posted(comment.get("body"), body):
                    author = (comment.get("user") or {}).get("login", "?")
                    return (
                        f"Not posted: comment by {author} on {comment.get('created_at', '?')} already holds "
                        f"this body: {comment.get('html_url', '')}"
                    ), None
            if len(comments) < _COMMENT_PAGE:
                return "", None
        return "", None

    async def assign_coding_agent_to_issue(
        repo: str,
        number: int,
        agent: str = "",
        base_ref: str = "",
        custom_agent: str = "",
        custom_instructions: str = "",
    ) -> str:
        """Assign a GitHub coding agent to an issue.

        ``agent`` selects the provider bot:
        - ``""`` / ``"copilot"`` -> ``copilot-swe-agent``
        - ``"claude"``           -> ``claude`` (Anthropic partner agent)
        - ``"codex"``            -> ``codex`` (OpenAI partner agent)

        ``custom_agent`` is for repo-level custom agents defined in
        ``.github/agents/`` (passed in ``agentAssignment.customAgent``).
        """
        owner, name, rerr = parse_repo(repo)
        if rerr:
            return rerr
        agent_bots: dict[str, str] = {
            "": "copilot-swe-agent",
            "copilot": "copilot-swe-agent",
            "claude": "claude",
            "codex": "codex",
        }
        bot_login = agent_bots.get(agent.lower().strip(), agent.strip())
        issue_url = f"https://github.com/{repo}/issues/{number}"
        hdr = {**_json_headers(headers), "GraphQL-Features": "issues_copilot_assignment_api_support"}
        async with egress_client(timeout=30) as client:
            agent_id = None
            available_bots: list[str] = []
            cursor = "null"
            for _ in range(5):
                after = f', after: "{cursor}"' if cursor != "null" else ""
                q = (
                    "query($o:String!,$n:String!){"
                    f"repository(owner:$o,name:$n){{"
                    f"suggestedActors(first:100{after},"
                    "capabilities:CAN_BE_ASSIGNED){"
                    "nodes{__typename ...on Bot{id login}}"
                    "pageInfo{hasNextPage endCursor}}}}"
                )
                sa_resp, sa_err = await _common.safe_request(
                    client,
                    "post",
                    _GRAPHQL_URL,
                    _PROVIDER,
                    f"find {bot_login} bot in {repo} suggestedActors",
                    headers=hdr,
                    json={"query": q, "variables": {"o": owner, "n": name}},
                )
                if sa_err:
                    return sa_err
                sa_data = ((sa_resp.json() if sa_resp else {}) or {}).get("data", {})
                sa = (sa_data.get("repository") or {}).get("suggestedActors", {})
                for node in sa.get("nodes", []):
                    login = node.get("login") or ""
                    if login:
                        available_bots.append(login)
                    if login == bot_login:
                        agent_id = node.get("id")
                        break
                if agent_id:
                    break
                pi = sa.get("pageInfo", {})
                if not pi.get("hasNextPage"):
                    break
                cursor = pi.get("endCursor", "null")
            if not agent_id:
                bot_list = ", ".join(available_bots) or "none found"
                return (
                    f"'{bot_login}' is not available as an assignee for {repo}. "
                    f"Available agent bots: {bot_list}. "
                    f"Ensure the agent is enabled in the org/repo Copilot settings."
                )
            issue_q = (
                "query($o:String!,$n:String!,$num:Int!){"
                "repository(owner:$o,name:$n){id "
                "issue(number:$num){id assignees(first:100){nodes{id}}}}}"
            )
            iq_resp, iq_err = await _common.safe_request(
                client,
                "post",
                _GRAPHQL_URL,
                _PROVIDER,
                f"get issue #{number} details in {repo}",
                headers=hdr,
                json={"query": issue_q, "variables": {"o": owner, "n": name, "num": number}},
            )
            if iq_err:
                return iq_err
            iq_data = ((iq_resp.json() if iq_resp else {}) or {}).get("data", {}).get("repository") or {}
            repo_id = iq_data.get("id")
            issue_obj = iq_data.get("issue") or {}
            issue_id = issue_obj.get("id")
            if not repo_id or not issue_id:
                return f"Could not resolve IDs for issue #{number} in {repo}."
            existing_ids = [a["id"] for a in (issue_obj.get("assignees") or {}).get("nodes", [])]
            assignee_ids = [*existing_ids, agent_id]
            mut = (
                "mutation($input:UpdateIssueInput!){"
                "updateIssue(input:$input){issue{id number url assignees(first:10){nodes{login}}}}}"
            )
            agent_assignment: dict[str, Any] = {
                "targetRepositoryId": repo_id,
                "customAgent": custom_agent or "",
                "customInstructions": custom_instructions or "",
            }
            if base_ref:
                agent_assignment["baseRef"] = base_ref
            mut_input: dict[str, Any] = {
                "id": issue_id,
                "assigneeIds": assignee_ids,
                "agentAssignment": agent_assignment,
            }
            mr, me = await _common.safe_request(
                client,
                "post",
                _GRAPHQL_URL,
                _PROVIDER,
                f"assign {bot_login} to issue #{number} via updateIssue",
                headers=hdr,
                json={"query": mut, "variables": {"input": mut_input}},
            )
            if me:
                return me
            mr_data = (mr.json() if mr else {}) or {}
            errs = mr_data.get("errors") or []
            if errs:
                detail = errs[0].get("message", "unknown error")
                return f"Failed to assign {bot_login} to issue #{number}: {detail}"
            label = agent.strip().capitalize() or "Copilot"
            updated = ((mr_data.get("data") or {}).get("updateIssue") or {}).get("issue") or {}
            assignees = [a["login"] for a in (updated.get("assignees") or {}).get("nodes", [])]
            await mark_self_action(repo, number)
            return (
                f"Success: {label} coding agent is now assigned to issue #{number} ({issue_url}). "
                f"Assignees: {', '.join(assignees) or bot_login}. "
                f"The agent will autonomously create a branch, write code, and open a pull request. "
                f"No further action needed."
            )

    async def add_github_labels(repo: str, issue_number: int, labels: list[str]) -> str:
        """Add labels to an issue or pull request."""
        if e := _common.repo_error(repo):
            return e
        async with egress_client(timeout=30) as client:
            resp, err = await _common.safe_request(
                client,
                "post",
                f"{_BASE_URL}/repos/{repo}/issues/{issue_number}/labels",
                _PROVIDER,
                f"add labels to #{issue_number} in {repo}",
                headers=_json_headers(headers),
                json={"labels": labels},
            )
            if err:
                return err
            result = resp.json()
            await mark_self_action(repo, issue_number)
            label_names = [lb["name"] for lb in result]
            return f"Labels on #{issue_number}: {', '.join(label_names)}"

    async def remove_github_labels(repo: str, issue_number: int, labels: list[str]) -> str:
        """Remove labels from an issue or pull request."""
        if e := _common.repo_error(repo):
            return e
        removed = []
        async with egress_client(timeout=30) as client:
            for label in labels:
                encoded_label = urlquote(label, safe="")
                resp, _ = await _common.safe_request(
                    client,
                    "delete",
                    f"{_BASE_URL}/repos/{repo}/issues/{issue_number}/labels/{encoded_label}",
                    _PROVIDER,
                    f"remove label '{label}' from #{issue_number}",
                    headers=headers,
                )
                if resp is not None:
                    removed.append(label)
        if removed:
            await mark_self_action(repo, issue_number)
            return f"Removed labels from #{issue_number}: {', '.join(removed)}"
        return f"No labels were removed from #{issue_number} (labels may not exist)."

    async def list_github_labels(repo: str, count: int = 50) -> str:
        """List all labels defined in a repository with their color and description."""
        owner, name, rerr = parse_repo(repo)
        if rerr:
            return rerr
        async with egress_client(timeout=30) as client:
            resp, err = await _common.safe_request(
                client,
                "get",
                f"{_BASE_URL}/repos/{owner}/{name}/labels",
                _PROVIDER,
                f"list labels in {repo}",
                headers=headers,
                params={"per_page": max(1, min(count, 100))},
            )
            if err:
                return err
            labels = resp.json()
            return _common.format_entity_list(
                labels,
                lambda lb: f"{lb.get('name')} (#{lb.get('color', '')})"
                + (f" — {lb.get('description')}" if lb.get("description") else ""),
                noun="label",
                empty_msg=f"No labels found in {repo}.",
            )

    async def manage_github_label(
        repo: str,
        action: str,
        name: str,
        new_name: str = "",
        color: str = "",
        description: str = "",
    ) -> str:
        """Create, update, or delete a repository label (write).

        ``action``: ``create`` | ``update`` | ``delete``. ``color`` is a 6-char
        hex string without ``#`` (e.g. ``"d73a4a"``). Use ``new_name`` to rename.
        """
        owner, repo_name, rerr = parse_repo(repo)
        if rerr:
            return rerr
        action = (action or "").strip().lower()
        if not name.strip():
            return "Error: 'name' (the label name) is required."
        base = f"{_BASE_URL}/repos/{owner}/{repo_name}/labels"
        async with egress_client(timeout=30) as client:
            if action == "create":
                payload: dict[str, Any] = {"name": name}
                if color:
                    payload["color"] = color.lstrip("#")
                if description:
                    payload["description"] = description
                _resp, err = await _common.safe_request(
                    client, "post", base, _PROVIDER, f"create label {name}", headers=_json_headers(headers), json=payload
                )
                if err:
                    return err
                return f"Label '{name}' created in {owner}/{repo_name}."
            if action == "update":
                payload = {}
                if new_name:
                    payload["new_name"] = new_name
                if color:
                    payload["color"] = color.lstrip("#")
                if description:
                    payload["description"] = description
                if not payload:
                    return "No fields to update. Provide new_name, color, or description."
                _resp, err = await _common.safe_request(
                    client,
                    "patch",
                    f"{base}/{urlquote(name, safe='')}",
                    _PROVIDER,
                    f"update label {name}",
                    headers=_json_headers(headers),
                    json=payload,
                )
                if err:
                    return err
                return f"Label '{name}' updated in {owner}/{repo_name}."
            if action == "delete":
                _resp, err = await _common.safe_request(
                    client,
                    "delete",
                    f"{base}/{urlquote(name, safe='')}",
                    _PROVIDER,
                    f"delete label {name}",
                    headers=headers,
                )
                if err:
                    return err
                return f"Label '{name}' deleted from {owner}/{repo_name}."
            return "Error: invalid action. Use create, update, or delete."

    async def list_github_milestones(
        repo: str, state: str = "open", sort: str = "due_on", direction: str = "asc", count: int = 20
    ) -> str:
        """List a repository's milestones with issue progress and due dates."""
        owner, name, rerr = parse_repo(repo)
        if rerr:
            return rerr
        params: dict[str, Any] = {
            "state": state,
            "sort": sort,
            "direction": direction,
            "per_page": max(1, min(count, 100)),
        }
        async with egress_client(timeout=30) as client:
            resp, err = await _common.safe_request(
                client,
                "get",
                f"{_BASE_URL}/repos/{owner}/{name}/milestones",
                _PROVIDER,
                f"list milestones in {repo}",
                headers=headers,
                params=params,
            )
            if err:
                return err
            milestones = resp.json()
            if not milestones:
                scope = "" if state == "all" else f"{state} "
                return f"No {scope}milestones found in {repo}."
            lines = []
            for m in milestones:
                open_issues = m.get("open_issues", 0)
                closed_issues = m.get("closed_issues", 0)
                total = open_issues + closed_issues
                progress = f"{closed_issues}/{total} issues closed" if total else "no issues"
                due = m.get("due_on")
                due_str = f", due {due[:10]}" if due else ""
                desc = (m.get("description") or "").strip().replace("\n", " ")
                desc_str = f"\n   {desc[:120]}" if desc else ""
                url = m.get("html_url", "")
                lines.append(
                    f"#{m['number']} {m['title']} [{m['state']}] ({progress}{due_str}) — {url}{desc_str}"
                )
            return f"Milestones in {repo} ({len(milestones)}):\n" + "\n".join(lines)

    async def manage_github_milestone(
        repo: str,
        action: str,
        number: int = 0,
        title: str = "",
        state: str = "",
        description: str = "",
        due_on: str = "",
    ) -> str:
        """Create, update, or delete a milestone (write).

        ``action``: ``create`` (needs ``title``) | ``update`` (needs ``number``) |
        ``delete`` (needs ``number``). ``due_on`` is an ISO 8601 timestamp.
        """
        owner, name, rerr = parse_repo(repo)
        if rerr:
            return rerr
        action = (action or "").strip().lower()
        base = f"{_BASE_URL}/repos/{owner}/{name}/milestones"
        async with egress_client(timeout=30) as client:
            if action == "create":
                if not title.strip():
                    return "Error: 'title' is required to create a milestone."
                payload: dict[str, Any] = {"title": title}
                if state in ("open", "closed"):
                    payload["state"] = state
                if description:
                    payload["description"] = description
                if due_on:
                    payload["due_on"] = due_on
                resp, err = await _common.safe_request(
                    client, "post", base, _PROVIDER, f"create milestone in {repo}", headers=_json_headers(headers), json=payload
                )
                if err:
                    return err
                m = resp.json()
                return f"Milestone #{m.get('number')} '{m.get('title')}' created."
            num = _coerce_int(number)
            if not num:
                return "Error: 'number' (the milestone number) is required for update/delete."
            if action == "update":
                payload = {}
                if title:
                    payload["title"] = title
                if state in ("open", "closed"):
                    payload["state"] = state
                if description:
                    payload["description"] = description
                if due_on:
                    payload["due_on"] = due_on
                if not payload:
                    return "No fields to update. Provide title, state, description, or due_on."
                resp, err = await _common.safe_request(
                    client, "patch", f"{base}/{num}", _PROVIDER, f"update milestone #{num}", headers=_json_headers(headers), json=payload
                )
                if err:
                    return err
                return f"Milestone #{num} updated in {owner}/{name}."
            if action == "delete":
                resp, err = await _common.safe_request(
                    client, "delete", f"{base}/{num}", _PROVIDER, f"delete milestone #{num}", headers=headers
                )
                if err:
                    return err
                return f"Milestone #{num} deleted from {owner}/{name}."
            return "Error: invalid action. Use create, update, or delete."

    async def list_github_sub_issues(repo: str, issue_number: int, count: int = 50) -> str:
        """List the sub-issues of a parent issue (the issue hierarchy / breakdown)."""
        owner, name, rerr = parse_repo(repo)
        if rerr:
            return rerr
        async with egress_client(timeout=30) as client:
            resp, err = await _common.safe_request(
                client,
                "get",
                f"{_BASE_URL}/repos/{owner}/{name}/issues/{issue_number}/sub_issues",
                _PROVIDER,
                f"list sub-issues of #{issue_number} in {repo}",
                headers=headers,
                params={"per_page": max(1, min(count, 100))},
            )
            if err:
                return err
            subs = resp.json()
            if _common.active_repo_scope() is not None and isinstance(subs, list):
                # A sub-issue may live in another repository.
                subs = [
                    s for s in subs
                    if isinstance(s, dict)
                    and _common.in_repo_scope(_common.repo_from_api_url(s.get("repository_url")))
                ]
            return _common.format_entity_list(
                subs,
                lambda s: f"#{s.get('number')} {s.get('title')} [{s.get('state')}]"
                + _label_str(s.get("labels", [])),
                noun="sub-issue",
                empty_msg=f"Issue #{issue_number} in {repo} has no sub-issues.",
            )

    async def manage_github_sub_issue(
        repo: str,
        parent_number: int,
        action: str,
        sub_issue_number: int,
        after_number: int = 0,
        before_number: int = 0,
        replace_parent: bool = False,
    ) -> str:
        """Manage an issue's sub-issues (write).

        ``action``: ``add`` (attach ``sub_issue_number`` under ``parent_number``;
        set ``replace_parent`` to move it from another parent), ``remove``
        (detach it), or ``reprioritize`` (reorder it relative to
        ``after_number`` or ``before_number``). Issue NUMBERS are accepted and
        resolved to internal IDs automatically.
        """
        owner, name, rerr = parse_repo(repo)
        if rerr:
            return rerr
        action = (action or "").strip().lower()
        base = f"{_BASE_URL}/repos/{owner}/{name}/issues/{parent_number}"
        async with egress_client(timeout=30) as client:
            sub_id, e = await _resolve_issue_id(client, owner, name, sub_issue_number, headers)
            if e:
                return e
            if action == "add":
                payload: dict[str, Any] = {"sub_issue_id": sub_id}
                if replace_parent:
                    payload["replace_parent"] = True
                _resp, err = await _common.safe_request(
                    client, "post", f"{base}/sub_issues", _PROVIDER,
                    f"add sub-issue #{sub_issue_number} to #{parent_number}",
                    headers=_json_headers(headers), json=payload,
                )
                if err:
                    return err
                await mark_self_action(repo, parent_number)
                return f"Added #{sub_issue_number} as a sub-issue of #{parent_number} in {owner}/{name}."
            if action == "remove":
                _resp, err = await _common.safe_request(
                    client, "delete", f"{base}/sub_issue", _PROVIDER,
                    f"remove sub-issue #{sub_issue_number} from #{parent_number}",
                    headers=_json_headers(headers), json={"sub_issue_id": sub_id},
                )
                if err:
                    return err
                await mark_self_action(repo, parent_number)
                return f"Removed #{sub_issue_number} as a sub-issue of #{parent_number}."
            if action == "reprioritize":
                payload = {"sub_issue_id": sub_id}
                if after_number:
                    aid, ae = await _resolve_issue_id(client, owner, name, after_number, headers)
                    if ae:
                        return ae
                    payload["after_id"] = aid
                elif before_number:
                    bid, be = await _resolve_issue_id(client, owner, name, before_number, headers)
                    if be:
                        return be
                    payload["before_id"] = bid
                else:
                    return "Error: reprioritize needs after_number or before_number."
                _resp, err = await _common.safe_request(
                    client, "patch", f"{base}/sub_issues/priority", _PROVIDER,
                    f"reprioritize sub-issue #{sub_issue_number}",
                    headers=_json_headers(headers), json=payload,
                )
                if err:
                    return err
                await mark_self_action(repo, parent_number)
                return f"Reprioritized sub-issue #{sub_issue_number} under #{parent_number}."
            return "Error: invalid action. Use add, remove, or reprioritize."

    async def manage_github_issue(repo: str, number: int, action: str, lock_reason: str = "") -> str:
        """Lock or unlock an issue/PR conversation (write).

        ``action``: ``lock`` (optionally with ``lock_reason`` = off-topic |
        too heated | resolved | spam) or ``unlock``.
        """
        if e := _common.repo_error(repo):
            return e
        action = (action or "").strip().lower()
        url = f"{_BASE_URL}/repos/{repo}/issues/{number}/lock"
        async with egress_client(timeout=30) as client:
            if action == "lock":
                payload = {}
                if lock_reason in ("off-topic", "too heated", "resolved", "spam"):
                    payload["lock_reason"] = lock_reason
                _resp, err = await _common.safe_request(
                    client, "put", url, _PROVIDER, f"lock #{number} in {repo}", headers=_json_headers(headers), json=payload
                )
                if err:
                    return err
                await mark_self_action(repo, number)
                return f"Locked conversation on #{number}" + (f" (reason: {lock_reason})" if payload else "") + "."
            if action == "unlock":
                _resp, err = await _common.safe_request(
                    client, "delete", url, _PROVIDER, f"unlock #{number} in {repo}", headers=headers
                )
                if err:
                    return err
                await mark_self_action(repo, number)
                return f"Unlocked conversation on #{number}."
            return "Error: invalid action. Use lock or unlock."

    executor.register("search_github_issues", search_github_issues)
    executor.register("get_github_issue", get_github_issue)
    executor.register("create_github_issue", create_github_issue)
    executor.register("update_github_issue", update_github_issue)
    executor.register("add_github_comment", add_github_comment)
    executor.register("update_github_comment", update_github_comment)
    executor.register("assign_coding_agent_to_issue", assign_coding_agent_to_issue)
    executor.register("add_github_labels", add_github_labels)
    executor.register("remove_github_labels", remove_github_labels)
    executor.register("list_github_labels", list_github_labels)
    executor.register("manage_github_label", manage_github_label)
    executor.register("list_github_milestones", list_github_milestones)
    executor.register("manage_github_milestone", manage_github_milestone)
    executor.register("list_github_sub_issues", list_github_sub_issues)
    executor.register("manage_github_sub_issue", manage_github_sub_issue)
    executor.register("manage_github_issue", manage_github_issue)

    _repo = {"type": "string", "description": "Repository in format owner/name"}
    _issue_type = {
        "type": "string",
        "description": (
            "Issue type by name, case-insensitive (e.g. Feature, Bug, Task; optional). "
            "Checked against the repository's issue types before any write; an unknown name is refused "
            "with the available ones."
        ),
    }
    _milestone_param = {
        "type": "string",
        "description": (
            "Existing milestone, number or exact title (optional), e.g. '1. Now'. Resolved on the repository "
            "before any write; an unknown one is refused with the open milestones named, never created."
        ),
    }

    return [
        ToolDefinition(
            name="search_github_issues",
            # The rest of the toolset enumerates with ``list_`` (labels,
            # milestones, teams): a model calls this one by that guess.
            aliases=("list_github_issues",),
            description=(
                "Search issues in a GitHub repository with filters. Returns issue number, title, labels, "
                "assignee, and a body preview so you can judge relevance. Supports pagination via page. Only use "
                "get_github_issue for the full body or comments."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "repo": _repo,
                    "query": {"type": "string", "description": "Search keywords (optional)", "default": ""},
                    "state": {"type": "string", "enum": ["open", "closed", "all"], "default": "open"},
                    "labels": {
                        "type": "string",
                        "description": "Comma-separated label names to filter by (optional)",
                        "default": "",
                    },
                    "assignee": {
                        "type": "string",
                        "description": "Filter by assignee username (optional)",
                        "default": "",
                    },
                    "count": {"type": "integer", "description": "Number of results per page (max 100)", "default": 20},
                    "page": {"type": "integer", "description": "Page number to fetch (starts at 1)", "default": 1},
                },
                "required": ["repo"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="get_github_issue",
            description=(
                "Read an issue: state, labels, assignees, milestone, sub-issue progress, the first 1,500 characters "
                "of the body and one line per comment (the latest 10). include_body=true reads the body by window "
                "(offset, max_chars); include_comments=true reads every comment whole. Use only when search preview "
                "is insufficient."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "repo": _repo,
                    "number": {"type": "integer", "description": "Issue number"},
                    "include_body": {
                        "type": "boolean",
                        "description": "Read the body by window (default false: the body's head)",
                        "default": False,
                    },
                    "include_comments": {
                        "type": "boolean",
                        "description": "Every comment whole, the first 100 (default false: one line each, the latest 10)",
                        "default": False,
                    },
                    "offset": {
                        "type": "integer",
                        "description": "With include_body: body character to start at (default 0)",
                        "default": 0,
                    },
                    "max_chars": {
                        "type": "integer",
                        "description": "With include_body: body characters to return (default 20,000, at most 60,000)",
                        "default": 0,
                    },
                },
                "required": ["repo", "number"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="create_github_issue",
            description=(
                "Create a new issue in a repository with title, body, labels, assignees, optional milestone "
                "and optional issue type (the Type field: e.g. Feature, Bug, Task), all in one call."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "repo": _repo,
                    "title": {"type": "string", "description": "Issue title"},
                    "body": {
                        "type": "string",
                        "description": "Issue body/description in Markdown (optional)",
                        "default": "",
                    },
                    "labels": {"type": "array", "items": {"type": "string"}, "description": "Labels to add (optional)"},
                    "assignees": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "GitHub usernames to assign (optional)",
                    },
                    "milestone": _milestone_param,
                    "issue_type": _issue_type,
                },
                "required": ["repo", "title"],
            },
            source=_SOURCE,
            priority=1,
            # A resumed turn's repeat of an issue that landed is answered with it, never a duplicate.
            landed_probe=issue_probe(headers),
            landed_key=issue_key,
        ),
        ToolDefinition(
            name="update_github_issue",
            description=(
                "Update an existing issue. Change state (open/closed), title, body, labels, assignees, milestone, or issue type. "
                "Only provided fields are changed. Use state='closed' to close an issue, state='open' to reopen. "
                "NOTE: to assign a GitHub coding agent (Copilot, Claude, Codex), use assign_coding_agent_to_issue instead."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "repo": _repo,
                    "number": {"type": "integer", "description": "Issue number"},
                    "state": {"type": "string", "enum": ["open", "closed"], "description": "New state (optional)"},
                    "title": {"type": "string", "description": "New title (optional)"},
                    "body": {"type": "string", "description": "New body in Markdown (optional)"},
                    "labels": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Replace all labels (optional)",
                    },
                    "assignees": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Replace all assignees (optional)",
                    },
                    "milestone": _milestone_param,
                    "state_reason": {
                        "type": "string",
                        "enum": ["completed", "not_planned", "reopened"],
                        "description": "Reason when closing (optional)",
                    },
                    "issue_type": _issue_type,
                    "edits": body_edits.EDITS_SCHEMA,
                    "expect_updated_at": body_edits.EXPECT_UPDATED_AT_SCHEMA,
                },
                "required": ["repo", "number"],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="add_github_comment",
            description=(
                "Add a comment to a GitHub issue or pull request. attachment_file_ids: files of this conversation "
                "committed to branch xtm-assets, images shown inline, others linked."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "repo": _repo,
                    "issue_number": {"type": "integer", "description": "Issue or PR number"},
                    "body": {"type": "string", "description": "Comment text in Markdown"},
                    "attachment_file_ids": attachments.SCHEMA,
                    "dedupe_first_line": {
                        "type": "boolean",
                        "description": "Post nothing when a comment already holds this body (same first line, same text); names it",
                    },
                },
                "required": ["repo", "issue_number", "body"],
            },
            source=_SOURCE,
            priority=1,
            # A resumed turn's repeat of a comment that landed is answered with it, never posted twice.
            landed_probe=comment_probe(headers),
            landed_key=comment_key,
        ),
        ToolDefinition(
            name="update_github_comment",
            description=(
                "Change an issue or pull request comment in place: find/replace 'edits' on the stored body "
                "(diff returned), or a whole 'body'."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "repo": _repo,
                    "comment": {"type": "string", "description": "Comment id, or its link ending in #issuecomment-N"},
                    "body": {"type": "string", "description": "New whole body (instead of edits)"},
                    "edits": body_edits.EDITS_SCHEMA,
                    "expect_updated_at": body_edits.EXPECT_UPDATED_AT_SCHEMA,
                },
                "required": ["repo", "comment"],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="assign_coding_agent_to_issue",
            description=(
                "Assign a GitHub coding agent to an issue so it automatically creates a branch, writes code, and "
                "opens a pull request. Supports Copilot (default), Claude, and Codex as provider agents, plus "
                "repo-level custom agents."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "repo": _repo,
                    "number": {"type": "integer", "description": "Issue number"},
                    "agent": {"type": "string", "description": '"copilot" (default), "claude", or "codex".'},
                    "base_ref": {
                        "type": "string",
                        "description": "Git branch the agent starts from (defaults to the repo default branch)",
                    },
                    "custom_agent": {
                        "type": "string",
                        "description": "Name of a repo-level custom agent in .github/agents/*.agent.md (optional)",
                    },
                    "custom_instructions": {
                        "type": "string",
                        "description": "Extra instructions beyond the issue body (optional)",
                    },
                },
                "required": ["repo", "number"],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="add_github_labels",
            description="Add labels to a GitHub issue or pull request. Creates labels if they don't exist in the repo.",
            input_schema={
                "type": "object",
                "properties": {
                    "repo": _repo,
                    "issue_number": {"type": "integer", "description": "Issue or PR number"},
                    "labels": {"type": "array", "items": {"type": "string"}, "description": "Label names to add"},
                },
                "required": ["repo", "issue_number", "labels"],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="remove_github_labels",
            description="Remove specific labels from a GitHub issue or pull request.",
            input_schema={
                "type": "object",
                "properties": {
                    "repo": _repo,
                    "issue_number": {"type": "integer", "description": "Issue or PR number"},
                    "labels": {"type": "array", "items": {"type": "string"}, "description": "Label names to remove"},
                },
                "required": ["repo", "issue_number", "labels"],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="list_github_labels",
            description="List all labels defined in a repository with their color and description. Use to discover valid label names before applying them.",
            input_schema={
                "type": "object",
                "properties": {
                    "repo": _repo,
                    "count": {"type": "integer", "description": "Max labels (max 100)", "default": 50},
                },
                "required": ["repo"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="manage_github_label",
            description="Create, update, or delete a repository label. action=create|update|delete. color is a 6-char hex (no '#'); use new_name to rename. Delete is permanent.",
            input_schema={
                "type": "object",
                "properties": {
                    "repo": _repo,
                    "action": {"type": "string", "enum": ["create", "update", "delete"]},
                    "name": {"type": "string", "description": "Label name (target)"},
                    "new_name": {"type": "string", "description": "New name when renaming (update)", "default": ""},
                    "color": {
                        "type": "string",
                        "description": "6-char hex color without '#' (e.g. d73a4a)",
                        "default": "",
                    },
                    "description": {"type": "string", "default": ""},
                },
                "required": ["repo", "action", "name"],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="list_github_milestones",
            description="List a repository's milestones with issue progress (closed/total), state, and due date. Use for sprint/release tracking.",
            input_schema={
                "type": "object",
                "properties": {
                    "repo": _repo,
                    "state": {"type": "string", "enum": ["open", "closed", "all"], "default": "open"},
                    "sort": {"type": "string", "enum": ["due_on", "completeness"], "default": "due_on"},
                    "direction": {"type": "string", "enum": ["asc", "desc"], "default": "asc"},
                    "count": {
                        "type": "integer",
                        "description": "Number of milestones to return (max 100)",
                        "default": 20,
                    },
                },
                "required": ["repo"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="manage_github_milestone",
            description="Create, update, or delete a milestone. action=create (needs title), update (needs number), or delete (needs number). due_on is ISO 8601. Delete is permanent.",
            input_schema={
                "type": "object",
                "properties": {
                    "repo": _repo,
                    "action": {"type": "string", "enum": ["create", "update", "delete"]},
                    "number": {"type": "integer", "description": "Milestone number (update/delete)", "default": 0},
                    "title": {"type": "string", "default": ""},
                    "state": {
                        "type": "string",
                        "enum": ["open", "closed"],
                        "description": "Milestone state (optional)",
                    },
                    "description": {"type": "string", "default": ""},
                    "due_on": {"type": "string", "description": "Due date (ISO 8601, optional)", "default": ""},
                },
                "required": ["repo", "action"],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="list_github_sub_issues",
            description=(
                "List the sub-issues of a parent issue - the issue breakdown/hierarchy used to plan and track work "
                "(the product backlog tree). Returns each sub-issue's number, title, and state."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "repo": _repo,
                    "issue_number": {"type": "integer", "description": "Parent issue number"},
                    "count": {"type": "integer", "description": "Max sub-issues (max 100)", "default": 50},
                },
                "required": ["repo", "issue_number"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="manage_github_sub_issue",
            description=(
                "Manage an issue's sub-issues (issue hierarchy). action=add attaches sub_issue_number under "
                "parent_number (set replace_parent=true to move it from another parent); action=remove detaches it; "
                "action=reprioritize reorders it relative to after_number or before_number. Pass issue NUMBERS - they "
                "are resolved to internal IDs automatically."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "repo": _repo,
                    "parent_number": {"type": "integer", "description": "Parent issue number"},
                    "action": {"type": "string", "enum": ["add", "remove", "reprioritize"]},
                    "sub_issue_number": {"type": "integer", "description": "The sub-issue's number"},
                    "after_number": {
                        "type": "integer",
                        "description": "Place after this sub-issue's number (reprioritize)",
                        "default": 0,
                    },
                    "before_number": {
                        "type": "integer",
                        "description": "Place before this sub-issue's number (reprioritize)",
                        "default": 0,
                    },
                    "replace_parent": {
                        "type": "boolean",
                        "description": "Move the sub-issue from its current parent (add)",
                        "default": False,
                    },
                },
                "required": ["repo", "parent_number", "action", "sub_issue_number"],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="manage_github_issue",
            description="Lock or unlock an issue/PR conversation. action=lock (optional lock_reason: off-topic|too heated|resolved|spam) or unlock.",
            input_schema={
                "type": "object",
                "properties": {
                    "repo": _repo,
                    "number": {"type": "integer", "description": "Issue or PR number"},
                    "action": {"type": "string", "enum": ["lock", "unlock"]},
                    "lock_reason": {
                        "type": "string",
                        "enum": ["off-topic", "too heated", "resolved", "spam"],
                        "description": "Reason (lock only, optional)",
                        "default": "",
                    },
                },
                "required": ["repo", "number", "action"],
            },
            source=_SOURCE,
            priority=1,
        ),
    ]
