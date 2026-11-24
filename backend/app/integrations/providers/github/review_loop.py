"""The agent review loop's verdict, read before a pull request is readied or merged.

An agent review loop (a design review desk, for example) posts its verdict on
the pull request as a comment whose first line is machine readable::

    UX-LOOP round=2 verdict=CHANGES_REQUESTED new=1 repeat=0 ...

The loop gates a merge only on a repository that declares it, in its
``.xtm-one/checks.toml`` on the default branch::

    [merge]
    review_loop_gate = true

There, ``merge_github_pr`` and ``manage_github_pr`` (``ready_for_review`` and
``enable_automerge``) refuse when the NEWEST such comment does not read
``verdict=APPROVE``: a broad "merge everything" instruction must not land a
pull request whose reviewer asked for changes. The refusal names the round, the
verdict, the comment link and its time; a newer ``verdict=APPROVE`` comment
releases it.

A repository that does not declare the gate (its pull requests are checked by
CI alone, the review runs after the merge) is never refused: the newest verdict
is reported in the merge answer as a fact, and the caller decides.

A pull request with no marker, or whose comments or rules cannot be read, is
never blocked.
"""

from __future__ import annotations

import re
import tomllib
from typing import Any

from . import _common
from ._common import _BASE_URL, _PROVIDER

#: The machine-readable first line of a review-loop verdict comment. It may be posted as code: wrapped in
#: backticks (`` `UX-LOOP ...` ``) or opened by a fence (```` ``` ```` then the marker on the next line).
#: Prose that cites a marker mid-sentence is not one.
_MARKER = re.compile(r"\A\s*(?:`{3,}[^\n]*\n\s*)?`*\s*UX-LOOP\s+round=(?P<round>\d+)\s+verdict=(?P<verdict>[A-Za-z_]+)")

#: The newest comments read: a verdict is among the latest, never page one of a long thread.
MAX_COMMENTS = 50

#: Where a repository declares that the loop gates its merges, and the key.
RULES_FILE = ".xtm-one/checks.toml"
GATE_KEY = "review_loop_gate"

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


def gate_declared(text: str) -> bool:
    """Whether a ``checks.toml`` *text* declares ``[merge] review_loop_gate = true``. Pure."""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return False
    table = data.get("merge")
    return isinstance(table, dict) and table.get(GATE_KEY) is True


async def _read_gate(client: Any, owner: str, name: str, headers: dict[str, str]) -> bool:
    """Whether *owner/name* declares the gate on its default branch; ``False`` when unreadable."""
    resp, err = await _common.safe_request(
        client,
        "get",
        f"{_BASE_URL}/repos/{owner}/{name}/contents/{RULES_FILE}",
        _PROVIDER,
        f"read the review-loop gate of {owner}/{name}",
        headers={**headers, "Accept": "application/vnd.github.raw+json"},
    )
    if err or resp is None:
        return False
    return gate_declared(resp.text or "")


async def review_loop_check(
    client: Any, owner: str, name: str, number: int, headers: dict[str, str], action: str
) -> tuple[str | None, str]:
    """``(refusal, note)`` for PR *number*: a refusal only where the repository declares the gate.

    On a repository that does not declare it, a non-APPROVE newest verdict is the *note*
    (a sentence for the answer), never a refusal.
    """
    data, err = await _common.gh_graphql(
        client,
        headers,
        _QUERY,
        {"owner": owner, "name": name, "number": number},
        action=f"read the review-loop verdict of PR #{number} before it is {action}",
    )
    if err or not isinstance(data, dict):
        return None, ""
    pull = ((data.get("repository") or {}).get("pullRequest")) or {}
    nodes = ((pull.get("comments") or {}).get("nodes")) or []
    verdict = newest_verdict(nodes if isinstance(nodes, list) else [])
    if verdict is None or verdict["verdict"] == "APPROVE":
        return None, ""
    where = (
        f"round {verdict['round']} verdict={verdict['verdict']} ({verdict['url'] or 'comment'}, "
        f"{verdict['created_at'] or 'time unknown'})"
    )
    if not await _read_gate(client, owner, name, headers):
        return None, (
            f" Newest review-loop verdict on it: {where}; {owner}/{name} does not declare "
            f"[merge] {GATE_KEY} in {RULES_FILE}, so it did not gate this."
        )
    return (
        f"Error: PR #{number} not {action}, nothing was written: the newest review-loop verdict on it is "
        f"{where}. Fix what it asks and get a new round with verdict=APPROVE on the head first; a broad "
        f"instruction to merge does not lift a reviewer's verdict ({owner}/{name} declares [merge] {GATE_KEY})."
    ), ""


async def review_loop_refusal(
    client: Any, owner: str, name: str, number: int, headers: dict[str, str], action: str
) -> str | None:
    """The refusal when the gate is declared and the newest verdict on PR *number* is not APPROVE, else ``None``."""
    refusal, _note = await review_loop_check(client, owner, name, number, headers, action)
    return refusal
