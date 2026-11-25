"""Issues a completing pull request leaves open: refused at write, named at merge.

GitHub closes an issue only through a closing keyword (``Closes #N``) in a pull
request merged into the default branch. The last slice of a multi-slice issue
that writes ``Refs #N (slice 4 of 4)`` merges the work and leaves the issue
open; so does any PR merged into another branch.

Two guards, generic to every repository:

* :func:`last_slice_refusal`, called by ``create_github_pr`` and
  ``update_github_pr`` on the body they are about to write: a body that marks
  the PR as completing (``last slice``, ``final part``, ``slice 4 of 4``,
  ``S4 of 4``, ``completes #N``) and names an issue with a non-closing word
  (``Refs``, ``Related``, ``See``, ``Part of``, a bare ``#N``) is refused with
  nothing written, the issue named. A reference to a pull request is never
  refused here (the closing-keyword guard of :mod:`.closing_refs` covers it),
  and a reference whose kind cannot be read never blocks.
* :func:`still_open_line`, called by ``merge_github_pr`` once the merge is read
  back: every issue the PR title or body references without a closing keyword
  (and, on a base that is not the default branch, with one too) that is still
  open is named with its title and the count of open pull requests that still
  reference it. Read-only: one GraphQL read for at most :data:`MAX_ISSUES`
  references; a failed read adds nothing.
"""

from __future__ import annotations

import re
from typing import Any

from . import _common
from ._common import _BASE_URL, _PROVIDER
from .closing_refs import closing_refs

#: References resolved per write or merge: a body is prose, not a list.
MAX_ISSUES = 10

#: An issue reference: ``#N``, ``owner/repo#N`` or a github.com issue URL.
_REF = re.compile(
    r"(?:https://github\.com/(?P<url_repo>[\w.-]+/[\w.-]+)/issues/(?P<url_num>\d+)"
    r"|(?<![\w/&-])(?P<repo>[\w.-]+/[\w.-]+)?#(?P<num>\d+))\b"
)

#: The slice words right before a reference: they tie it to a completion marker on another line.
_SLICE_WORD = re.compile(r"(?:\brefs?|\bpart\s+of)\s*:?\s*$", re.IGNORECASE)

#: Words that say the pull request completes the work.
_LAST = re.compile(r"\b(?:last|final)\s+(?:slice|part|step|pr|pull\s+request)\b", re.IGNORECASE)
_K_OF_N = re.compile(r"\b(?:slices?\s*|parts?\s*|S)(\d+)\s*(?:of|/)\s*(\d+)\b", re.IGNORECASE)
_COMPLETES = re.compile(r"\bcomplete[sd]?\s*:?\s*$", re.IGNORECASE)


def _refs(text: str, default_repo: str) -> list[tuple[str, int, int]]:
    """Every issue-like reference of *text* as ``(owner/repo, number, start)``."""
    out: list[tuple[str, int, int]] = []
    for m in _REF.finditer(text or ""):
        if m.group("url_num"):
            out.append((m.group("url_repo"), int(m.group("url_num")), m.start()))
        else:
            out.append((m.group("repo") or default_repo, int(m.group("num")), m.start()))
    return out


def _completes_line(line: str) -> bool:
    """Whether *line* marks the PR as the one completing the work."""
    if _LAST.search(line):
        return True
    return any(int(k) == int(n) and int(n) > 0 for k, n in _K_OF_N.findall(line))


def completing_refs(body: str, default_repo: str) -> list[tuple[str, int]]:
    """The issues *body* marks as completed by this PR without a closing keyword.

    A reference counts when ``completes`` stands right before it; when its own
    line marks the PR as the last one (``slice 4 of 4``, ``last slice``) and the
    reference is bare or follows a non-closing word; or when another line of the
    body marks it and the reference follows ``Refs`` or ``Part of`` (the slice
    words). A reference the body also closes (``Closes #N``) never counts.
    The cross-line rule applies only to a body that closes nothing: when the body
    closes an issue, a marker on another line is read as completing that issue,
    and a ``Part of #N`` epic beside it is left alone.
    """
    text = body or ""
    closed = {(r.lower(), n) for r, n, _k in closing_refs(text, default_repo)}
    body_marked = not closed and _completes_line(text)
    found: list[tuple[str, int]] = []
    seen: set[tuple[str, int]] = set()
    for line in text.splitlines():
        line_marked = _completes_line(line)
        for repo, number, start in _refs(line, default_repo):
            key = (repo.lower(), number)
            if key in closed or key in seen:
                continue
            before = line[:start]
            hit = bool(_COMPLETES.search(before) or line_marked or (body_marked and _SLICE_WORD.search(before)))
            if hit:
                seen.add(key)
                found.append((repo, number))
    return found


async def _kind(client: Any, repo: str, number: int, headers: dict[str, str]) -> str | None:
    """``"pull"``, ``"issue"`` or ``None`` when the reference cannot be read."""
    resp, err = await _common.safe_request(
        client,
        "get",
        f"{_BASE_URL}/repos/{repo}/issues/{number}",
        _PROVIDER,
        f"read the kind of {repo}#{number}",
        headers=headers,
    )
    if err or resp is None:
        return None
    try:
        data = resp.json()
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    return "pull" if data.get("pull_request") else "issue"


async def last_slice_refusal(client: Any, repo: str, body: str, headers: dict[str, str], action: str) -> str | None:
    """The refusal when *body* completes an issue it does not close; ``None`` otherwise."""
    issues: list[tuple[str, int]] = []
    for ref_repo, number in completing_refs(body, repo)[:MAX_ISSUES]:
        if await _kind(client, ref_repo, number, headers) == "issue":
            issues.append((ref_repo, number))
    if not issues:
        return None
    lines = [
        f"Error: {action} refused, nothing was written: the body marks this pull request as completing "
        "an issue it does not close, and GitHub would leave that issue open after the merge."
    ]
    for ref_repo, number in issues:
        ref = f"#{number}" if ref_repo.lower() == repo.lower() else f"{ref_repo}#{number}"
        lines.append(f"  body: {ref_repo}#{number} is an issue: write 'Closes {ref}'")
    lines.append(
        "Write 'Closes #N' for the issue this PR completes (keep 'Refs #N (S2 of 4)' for an earlier "
        "slice) and call again."
    )
    return "\n".join(lines)


_ISSUE_FIELDS = (
    "__typename ... on Issue { state title "
    "timelineItems(itemTypes: [CROSS_REFERENCED_EVENT], last: 100) { nodes { ... on CrossReferencedEvent { "
    "source { ... on PullRequest { number state repository { nameWithOwner } } } } } } }"
)


def _query(refs: list[tuple[str, int]]) -> str:
    """One GraphQL read of *refs*, a repository block per owner/name and an alias per number."""
    by_repo: dict[str, list[int]] = {}
    for repo, number in refs:
        by_repo.setdefault(repo, []).append(number)
    blocks = []
    for r, (repo, numbers) in enumerate(by_repo.items()):
        owner, name = repo.split("/", 1)
        items = " ".join(f"i{number}: issueOrPullRequest(number: {number}) {{ {_ISSUE_FIELDS} }}" for number in numbers)
        blocks.append(f'r{r}: repository(owner: "{owner}", name: "{name}") {{ nameWithOwner {items} }}')
    return "query { " + " ".join(blocks) + " }"


def _referenced(title: str, body: str, repo: str, closing_too: bool) -> list[tuple[str, int]]:
    """The references of *title* and *body* to report: non-closing ones, closing ones too when asked."""
    text = f"{title}\n{body}"
    closed = {(r.lower(), n) for r, n, _k in closing_refs(text, repo)}
    out: list[tuple[str, int]] = []
    seen: set[tuple[str, int]] = set()
    for ref_repo, number, _start in _refs(text, repo):
        key = (ref_repo.lower(), number)
        if key in seen or (key in closed and not closing_too):
            continue
        seen.add(key)
        out.append((ref_repo, number))
    return out[:MAX_ISSUES]


async def still_open_line(
    client: Any,
    repo: str,
    number: int,
    pr_data: dict[str, Any] | None,
    headers: dict[str, str],
) -> str:
    """' Still open after this merge: ...' for the issues the merged PR leaves open; '' when none.

    *pr_data* is the PR as read before the merge (title, body, base). On a base
    that is not the repository's default branch GitHub closes nothing, so the
    issues the PR closes by keyword are reported too. Never writes; a failed
    read answers ''.
    """
    if not isinstance(pr_data, dict):
        return ""
    base = pr_data.get("base") or {}
    base_ref = str(base.get("ref") or "")
    default = str((base.get("repo") or {}).get("default_branch") or "")
    off_default = bool(base_ref and default and base_ref != default)
    refs = [
        (r, n)
        for r, n in _referenced(str(pr_data.get("title") or ""), str(pr_data.get("body") or ""), repo, off_default)
        if not (r.lower() == repo.lower() and n == number)
    ]
    if not refs:
        return ""
    # keep_partial: a reference that is not an issue of the repository (a colour,
    # a deleted issue) fails alone; the other aliases still answer.
    data, _err = await _common.gh_graphql(
        client,
        headers,
        _query(refs),
        action=f"read the issues PR #{number} references after its merge",
        keep_partial=True,
    )
    if not isinstance(data, dict):
        return ""
    lines: list[str] = []
    for block in data.values():
        if not isinstance(block, dict):
            continue
        owner_name = str(block.get("nameWithOwner") or "")
        for key, node in block.items():
            if not key.startswith("i") or not isinstance(node, dict) or node.get("__typename") != "Issue":
                continue
            if node.get("state") != "OPEN":
                continue
            open_prs = set()
            for event in (node.get("timelineItems") or {}).get("nodes") or []:
                source = (event or {}).get("source") or {}
                if source.get("state") == "OPEN" and source.get("number") is not None:
                    src_repo = str((source.get("repository") or {}).get("nameWithOwner") or "")
                    if not (src_repo.lower() == repo.lower() and source.get("number") == number):
                        open_prs.add((src_repo, source.get("number")))
            ref = f"#{key[1:]}" if owner_name.lower() == repo.lower() else f"{owner_name}#{key[1:]}"
            lines.append(
                f"{ref} {_common.one_line(str(node.get('title') or ''), 80)} ({len(open_prs)} open PRs reference it)"
            )
    if not lines:
        return ""
    where = (
        f" GitHub closes nothing on a merge into '{base_ref}', which is not the default branch '{default}'."
        if off_default
        else ""
    )
    return (
        "\nStill open after this merge: "
        + "; ".join(lines)
        + "."
        + where
        + " If this PR completed it, close it now with update_github_issue (state_reason completed) and a "
        "comment naming the PRs."
    )
