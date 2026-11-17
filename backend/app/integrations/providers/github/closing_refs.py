"""Closing keywords that point at a pull request, refused before a PR is written or merged.

GitHub closes the target of a closing keyword (``close``, ``closes``,
``closed``, ``fix``, ``fixes``, ``fixed``, ``resolve``, ``resolves``,
``resolved``, an optional colon, then ``#N``, ``owner/repo#N`` or an issue or
pull request URL) when the pull request carrying it merges into the default
branch. Aimed at an issue, that is the point of ``Closes #N``. Aimed at a pull
request, in the same repository or another, it silently closes that other PR:
a body saying "Pool fix owner/repo#N awaiting deploy" closed a PR under team
review two seconds after an unrelated merge.

``create_github_pr``, ``update_github_pr`` and ``merge_github_pr`` call
:func:`pr_closing_refusal` on the text they are about to write (title, body,
squash commit title and message) and refuse it with nothing written when a
closing keyword resolves to a pull request. A reference whose kind cannot be
read (no access, an API error) never blocks: the write goes on as before.

``merge_github_pr`` also checks the messages of the PR's own commits
(:func:`pr_commit_messages`) whenever they reach the base branch: GitHub builds
the default squash message from them when no commit message is given, and a
merge commit or a rebase keeps them as they are.
"""

from __future__ import annotations

import re
from typing import Any

from . import _common
from ._common import _BASE_URL, _PROVIDER

#: GitHub's closing keywords, an optional colon, then a reference: ``#N``,
#: ``owner/repo#N`` or a github.com issue / pull URL.
_CLOSING_REF = re.compile(
    r"(?<![\w/-])(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?):?\s+"
    r"(?:https://github\.com/(?P<url_repo>[\w.-]+/[\w.-]+)/(?P<url_kind>issues|pull)/(?P<url_num>\d+)"
    r"|(?P<repo>[\w.-]+/[\w.-]+)?#(?P<num>\d+))\b",
    re.IGNORECASE,
)

#: At most this many references are resolved per write: a body is prose, not a list.
MAX_REFS = 10

#: The PR commits read before a merge: one page, GitHub's maximum page size.
MAX_COMMITS = 100


def closing_refs(text: str, default_repo: str) -> list[tuple[str, int, str | None]]:
    """The closing references of *text* as ``(owner/repo, number, kind)``.

    *kind* is ``"pull"`` or ``"issues"`` when the reference is a URL that says
    so, ``None`` when it must be read. A bare ``#N`` belongs to *default_repo*.
    Duplicates are listed once, in order of appearance.
    """
    seen: set[tuple[str, int]] = set()
    refs: list[tuple[str, int, str | None]] = []
    for m in _CLOSING_REF.finditer(text or ""):
        if m.group("url_num"):
            repo, number, kind = m.group("url_repo"), int(m.group("url_num")), m.group("url_kind")
        else:
            repo, number, kind = m.group("repo") or default_repo, int(m.group("num")), None
        key = (repo.lower(), number)
        if key in seen:
            continue
        seen.add(key)
        refs.append((repo, number, kind))
    return refs


async def _is_pull_request(client: Any, repo: str, number: int, headers: dict[str, str]) -> bool:
    """Whether *repo*#*number* is a pull request; ``False`` when it cannot be read."""
    resp, err = await _common.safe_request(
        client,
        "get",
        f"{_BASE_URL}/repos/{repo}/issues/{number}",
        _PROVIDER,
        f"read the kind of {repo}#{number}",
        headers=headers,
    )
    if err or resp is None:
        return False
    try:
        data = resp.json()
    except Exception:
        return False
    return isinstance(data, dict) and bool(data.get("pull_request"))


async def pr_commit_messages(client: Any, repo: str, number: int, headers: dict[str, str]) -> dict[str, str]:
    """The messages of PR *number*'s commits as ``{"commit <sha12>": message}``, oldest first.

    Only the commits whose message carries a closing keyword are returned, so a
    long PR adds nothing to check. Empty when the commits cannot be read: an
    unreadable list never blocks a merge.
    """
    resp, err = await _common.safe_request(
        client,
        "get",
        f"{_BASE_URL}/repos/{repo}/pulls/{number}/commits",
        _PROVIDER,
        f"read the commits of PR #{number} in {repo} before the merge",
        headers=headers,
        params={"per_page": MAX_COMMITS},
    )
    if err or resp is None:
        return {}
    try:
        data = resp.json()
    except Exception:
        return {}
    if not isinstance(data, list):
        return {}
    messages: dict[str, str] = {}
    for item in data:
        if not isinstance(item, dict):
            continue
        commit = item.get("commit")
        message = (commit.get("message") or "") if isinstance(commit, dict) else ""
        if message and _CLOSING_REF.search(message):
            messages[f"commit {str(item.get('sha') or '')[:12]}"] = message
    return messages


async def pr_closing_refusal(
    client: Any, repo: str, texts: dict[str, str], headers: dict[str, str], action: str
) -> str | None:
    """The refusal when a closing keyword in *texts* (field name to text) points at a
    pull request; ``None`` when every closing reference is an issue or unreadable."""
    found: list[tuple[str, str, int]] = []
    checked = 0
    for field, text in texts.items():
        for ref_repo, number, kind in closing_refs(text, repo):
            if checked >= MAX_REFS:
                break
            if kind == "issues":
                continue
            if kind is None:
                checked += 1
                if not await _is_pull_request(client, ref_repo, number, headers):
                    continue
            found.append((field, ref_repo, number))
    if not found:
        return None
    lines = [
        f"Error: {action} refused, nothing was written: a closing keyword points at a pull request, "
        "and GitHub would close that pull request when this one merges."
    ]
    lines += [f"  {field}: {ref_repo}#{number} is a pull request" for field, ref_repo, number in found]
    lines.append(
        "Reword the reference without close/fix/resolve before it (for example 'see', 'needs', "
        "'awaiting'), keep 'Closes #N' for issues only, and call again."
    )
    return "\n".join(lines)
