"""The agent review loop's verdict, read before a pull request is readied or merged.

An agent review loop (a design review desk, for example) posts its verdict on
the pull request as a comment whose first line is machine readable::

    UX-LOOP round=2 verdict=CHANGES_REQUESTED new=1 repeat=0 ...

``merge_github_pr`` and ``manage_github_pr`` (``ready_for_review`` and
``enable_automerge``) call :func:`review_loop_refusal` before any write and
refuse when the NEWEST such comment does not read ``verdict=APPROVE``: a broad
"merge everything" instruction must not land a pull request whose reviewer
asked for changes. The refusal names the round, the verdict, the comment link
and its time; a newer ``verdict=APPROVE`` comment releases it.

A pull request with no marker, or whose comments cannot be read, is never
blocked: the guard only enforces a loop the pull request is part of.
"""

from __future__ import annotations

import re
from typing import Any

from . import _common

#: The machine-readable first line of a review-loop verdict comment. It may be posted as code: wrapped in
#: backticks (`` `UX-LOOP ...` ``) or opened by a fence (```` ``` ```` then the marker on the next line).
#: Prose that cites a marker mid-sentence is not one.
_MARKER = re.compile(r"\A\s*(?:`{3,}[^\n]*\n\s*)?`*\s*UX-LOOP\s+round=(?P<round>\d+)\s+verdict=(?P<verdict>[A-Za-z_]+)")

#: The newest comments read: a verdict is among the latest, never page one of a long thread.
MAX_COMMENTS = 50

_QUERY = (
    "query($owner:String!,$name:String!,$number:Int!){repository(owner:$owner,name:$name){"
    f"pullRequest(number:$number){{comments(last:{MAX_COMMENTS}){{nodes{{body url createdAt}}}}}}}}}}"
)


def newest_verdict(comments: list[dict[str, Any]]) -> dict[str, str] | None:
    """The newest review-loop marker of *comments* (oldest first), or ``None``.

    Answered as ``{"round", "verdict", "url", "created_at"}``, the verdict upper-cased.
    """
    for comment in reversed(comments):
        if not isinstance(comment, dict):
            continue
        m = _MARKER.match(str(comment.get("body") or ""))
        if m:
            return {
                "round": m.group("round"),
                "verdict": m.group("verdict").upper(),
                "url": str(comment.get("url") or ""),
                "created_at": str(comment.get("createdAt") or ""),
            }
    return None


async def review_loop_refusal(
    client: Any, owner: str, name: str, number: int, headers: dict[str, str], action: str
) -> str | None:
    """The refusal when the newest review-loop verdict on PR *number* is not APPROVE, else ``None``."""
    data, err = await _common.gh_graphql(
        client,
        headers,
        _QUERY,
        {"owner": owner, "name": name, "number": number},
        action=f"read the review-loop verdict of PR #{number} before it is {action}",
    )
    if err or not isinstance(data, dict):
        return None
    pull = ((data.get("repository") or {}).get("pullRequest")) or {}
    nodes = ((pull.get("comments") or {}).get("nodes")) or []
    verdict = newest_verdict(nodes if isinstance(nodes, list) else [])
    if verdict is None or verdict["verdict"] == "APPROVE":
        return None
    return (
        f"Error: PR #{number} not {action}, nothing was written: the newest review-loop verdict on it is "
        f"round {verdict['round']} verdict={verdict['verdict']} ({verdict['url'] or 'comment'}, "
        f"{verdict['created_at'] or 'time unknown'}). Fix what it asks and get a new round with "
        "verdict=APPROVE on the head first; a broad instruction to merge does not lift a reviewer's verdict."
    )
