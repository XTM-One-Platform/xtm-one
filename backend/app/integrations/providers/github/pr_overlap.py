"""Open pull requests of the same author that overlap a pull request just created.

Two agent runs working on sibling tasks can open two pull requests for one fix
minutes apart: same base, same files, two issues, two CI runs, and a person
left to close one of them. ``create_github_pr`` calls :func:`overlap_warning`
right after GitHub created the pull request and appends its answer to the
result: every other OPEN pull request of the same author in the repository that
changes one of the same files, or closes or refers to one of the same issues,
is named with what it shares. It is a warning, never a refusal: the pull request
exists, and the caller decides whether one of the two is a duplicate.

Every read is bounded (one page of open pull requests, the files of at most
:data:`MAX_PEERS` of them) and best effort: a read that fails adds nothing and
never fails the creation.
"""

from __future__ import annotations

import re
from typing import Any

from . import _common
from ._common import _BASE_URL, _PROVIDER

#: The open pull requests read: one page, GitHub's maximum page size.
MAX_OPEN = 100

#: The same-author pull requests whose files are read, most recently updated first.
MAX_PEERS = 10

#: The files read per pull request: one page, GitHub's maximum page size.
MAX_FILES = 100

#: The shared files named per overlapping pull request.
MAX_NAMED_FILES = 5

#: An issue reference: a closing keyword or ``refs``, an optional colon, then ``#N``
#: of the same repository; or a trailing ``(#N)`` of a title.
_ISSUE_REF = re.compile(
    r"(?<![\w/-])(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?|refs?|part of):?\s+#(\d+)\b",
    re.IGNORECASE,
)
_TITLE_REF = re.compile(r"\(#(\d+)\)\s*$")


def issue_refs(title: str, body: str) -> set[int]:
    """The issue numbers *title* and *body* close or refer to in the same repository."""
    refs = {int(n) for n in _ISSUE_REF.findall(f"{title or ''}\n{body or ''}")}
    if m := _TITLE_REF.search(title or ""):
        refs.add(int(m.group(1)))
    return refs


async def _get_json(client: Any, url: str, action: str, headers: dict[str, str], params: dict) -> Any:
    resp, err = await _common.safe_request(client, "get", url, _PROVIDER, action, headers=headers, params=params)
    if err or resp is None:
        return None
    try:
        return resp.json()
    except Exception:
        return None


async def _files(client: Any, repo: str, number: int, headers: dict[str, str]) -> set[str] | None:
    data = await _get_json(
        client,
        f"{_BASE_URL}/repos/{repo}/pulls/{number}/files",
        f"read the files of PR #{number} in {repo}",
        headers,
        {"per_page": MAX_FILES},
    )
    if not isinstance(data, list):
        return None
    return {f["filename"] for f in data if isinstance(f, dict) and isinstance(f.get("filename"), str)}


async def overlap_warning(client: Any, repo: str, pr: dict, headers: dict[str, str]) -> str | None:
    """The warning naming the open pull requests of *pr*'s author that share a file or an
    issue with *pr* (the creation answer of GitHub); ``None`` when none or unreadable."""
    number = pr.get("number")
    user = pr.get("user")
    author = (user.get("login") or "").lower() if isinstance(user, dict) else ""
    if not isinstance(number, int) or not author:
        return None
    listed = await _get_json(
        client,
        f"{_BASE_URL}/repos/{repo}/pulls",
        f"list the open PRs of {repo}",
        headers,
        {"state": "open", "sort": "updated", "direction": "desc", "per_page": MAX_OPEN},
    )
    if not isinstance(listed, list):
        return None
    peers = [
        p
        for p in listed
        if isinstance(p, dict)
        and isinstance(p.get("number"), int)
        and p["number"] != number
        and isinstance(p.get("user"), dict)
        and (p["user"].get("login") or "").lower() == author
    ][:MAX_PEERS]
    if not peers:
        return None
    own_issues = issue_refs(pr.get("title") or "", pr.get("body") or "")
    own_files = await _files(client, repo, number, headers)
    lines: list[str] = []
    for peer in peers:
        shared_issues = sorted(own_issues & issue_refs(peer.get("title") or "", peer.get("body") or ""))
        shared_files: list[str] = []
        if own_files:
            peer_files = await _files(client, repo, peer["number"], headers)
            shared_files = sorted(own_files & (peer_files or set()))
        if not shared_issues and not shared_files:
            continue
        parts = []
        if shared_issues:
            parts.append("issue " + ", ".join(f"#{n}" for n in shared_issues))
        if shared_files:
            named = ", ".join(shared_files[:MAX_NAMED_FILES])
            more = len(shared_files) - MAX_NAMED_FILES
            parts.append(f"{len(shared_files)} file(s): {named}" + (f" and {more} more" if more > 0 else ""))
        lines.append(f"  #{peer['number']} {_one_title(peer)}: shares {'; '.join(parts)}")
    if not lines:
        return None
    return "\n".join(
        [
            f"Warning: {len(lines)} other open PR(s) by {author} in {repo} overlap this one "
            "(a possible duplicate; nothing was refused):",
            *lines,
            "Read them before going on; close the duplicate (update_github_pr state=closed) "
            "or say in the body why both are needed.",
        ]
    )


def _one_title(pr: dict) -> str:
    """The title of *pr* on one line, quoted."""
    return '"' + " ".join(str(pr.get("title") or "").split())[:120] + '"'
