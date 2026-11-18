"""The milestone and the type labels a new pull request takes from the issue it closes.

A team that files its changelog by milestone needs every pull request in the
milestone of the issue it fixes, and a workflow that files a closed issue by
its ``bug`` label needs the label on the pull request too. A prose rule ("set
the milestone right after create_github_pr") was skipped again and again, so
``create_github_pr`` applies it in the creating call:

* *milestone* given (a number or an exact title): resolved on the repository
  before anything is written; one the repository does not define refuses the
  call. A milestone is never created.
* no *milestone*: the milestone of the first issue of the same repository the
  title or body closes (``Closes #N``), when that issue has one.
* the type labels (:data:`TYPE_LABELS`) of those closing issues are added to
  the labels written right after creation.

Every read here is best effort: an unreadable issue or milestone list leaves the
pull request as it would have been, never refused, except an explicit
*milestone* that does not resolve.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from . import _common
from ._common import _BASE_URL, _PROVIDER
from .closing_refs import closing_refs

#: Labels that say what kind of change an issue is, copied from a closing issue.
TYPE_LABELS: frozenset[str] = frozenset({"bug", "feature"})

#: Closing issues read per creation: a pull request closes one issue, rarely a few.
MAX_CLOSING_ISSUES = 3

#: Milestones read when one is named by title: one page, GitHub's maximum.
_MILESTONES_PAGE = 100


@dataclass
class Inherited:
    """What the closing issues give the new pull request."""

    milestone_number: int | None = None
    milestone_title: str = ""
    source_issue: int | None = None
    labels: list[str] = field(default_factory=list)
    closing_issues: list[int] = field(default_factory=list)


def _json_or_none(resp: Any) -> Any:
    """The JSON of *resp*, ``None`` when its body is not JSON."""
    try:
        return resp.json()
    except ValueError:
        return None


def _same_repo_issue_numbers(owner: str, name: str, title: str, body: str) -> list[int]:
    """The issue numbers of this repository that *title* or *body* closes, in order, at most :data:`MAX_CLOSING_ISSUES`."""
    repo = f"{owner}/{name}".lower()
    numbers: list[int] = []
    for ref_repo, number, kind in closing_refs(f"{title}\n{body}", f"{owner}/{name}"):
        if kind == "pull" or ref_repo.lower() != repo or number in numbers:
            continue
        numbers.append(number)
        if len(numbers) >= MAX_CLOSING_ISSUES:
            break
    return numbers


async def from_closing_issues(
    client: Any, owner: str, name: str, title: str, body: str, headers: dict[str, str]
) -> Inherited:
    """Read the issues *title* and *body* close and collect their milestone and type labels."""
    found = Inherited()
    for number in _same_repo_issue_numbers(owner, name, title, body):
        resp, err = await _common.safe_request(
            client,
            "get",
            f"{_BASE_URL}/repos/{owner}/{name}/issues/{number}",
            _PROVIDER,
            f"read closing issue #{number} in {owner}/{name}",
            headers=headers,
        )
        if err or resp is None:
            continue
        issue = _json_or_none(resp)
        if not isinstance(issue, dict) or issue.get("pull_request"):
            continue
        found.closing_issues.append(number)
        milestone = issue.get("milestone")
        if found.milestone_number is None and isinstance(milestone, dict) and milestone.get("number"):
            found.milestone_number = int(milestone["number"])
            found.milestone_title = str(milestone.get("title") or "")
            found.source_issue = number
        for label in issue.get("labels") or []:
            label_name = label.get("name") if isinstance(label, dict) else label
            if isinstance(label_name, str) and label_name.lower() in TYPE_LABELS and label_name not in found.labels:
                found.labels.append(label_name)
    return found


async def resolve_milestone(
    client: Any, owner: str, name: str, wanted: str | int, headers: dict[str, str]
) -> tuple[int | None, str, str | None]:
    """``(number, title, refusal)`` of the milestone *wanted* (a number or an exact title) names.

    The refusal is set when the repository does not define it; a milestone is
    never created. An unreadable list also refuses: the caller asked for a
    milestone and would otherwise get a pull request without it, unnoticed.
    """
    text = str(wanted).strip()
    if text.isdigit():
        resp, err = await _common.safe_request(
            client,
            "get",
            f"{_BASE_URL}/repos/{owner}/{name}/milestones/{int(text)}",
            _PROVIDER,
            f"read milestone #{text} in {owner}/{name}",
            headers=headers,
        )
        data = _json_or_none(resp) if not err and resp is not None else None
        if isinstance(data, dict) and data.get("number"):
            return int(data["number"]), str(data.get("title") or ""), None
        return (
            None,
            "",
            (
                f"Refused: milestone #{text} not found in {owner}/{name}"
                f"{f' ({err})' if err else ''}. Nothing was written; list them with list_github_milestones "
                "and call again (a milestone is never created)."
            ),
        )
    resp, err = await _common.safe_request(
        client,
        "get",
        f"{_BASE_URL}/repos/{owner}/{name}/milestones",
        _PROVIDER,
        f"list milestones of {owner}/{name}",
        headers=headers,
        params={"state": "open", "per_page": _MILESTONES_PAGE},
    )
    milestones = _json_or_none(resp) if not err and resp is not None else None
    if isinstance(milestones, list):
        rows = [m for m in milestones if isinstance(m, dict) and m.get("number")]
        exact = [m for m in rows if str(m.get("title") or "") == text]
        folded = exact or [m for m in rows if str(m.get("title") or "").casefold() == text.casefold()]
        if len(folded) == 1:
            return int(folded[0]["number"]), str(folded[0].get("title") or ""), None
        titles = ", ".join(f"'{m.get('title')}'" for m in rows[:20]) or "none"
        return (
            None,
            "",
            (
                f"Refused: milestone '{text}' not found in {owner}/{name} (open milestones: {titles}). "
                "Nothing was written; a milestone is never created. Call again with one of them or without milestone."
            ),
        )
    return (
        None,
        "",
        (
            f"Refused: the milestones of {owner}/{name} could not be read ({err}). Nothing was written; "
            "call again, or without milestone."
        ),
    )


async def set_milestone(
    client: Any,
    owner: str,
    name: str,
    number: int,
    milestone: int,
    title: str,
    headers: dict[str, str],
    retry_delay: float,
) -> str:
    """Put pull request *number* in *milestone*; one line naming the milestone read back.

    A 404 right after creation (the pull request's issue not yet readable) is
    retried once; a failure never undoes the pull request.
    """
    resp, err = None, None
    for attempt in range(2):
        resp, err = await _common.safe_request(
            client,
            "patch",
            f"{_BASE_URL}/repos/{owner}/{name}/issues/{number}",
            _PROVIDER,
            f"set the milestone of PR #{number} in {owner}/{name}",
            headers=_common._json_headers(headers),
            json={"milestone": milestone},
        )
        if not (err and "resource not found" in err and attempt == 0):
            break
        await asyncio.sleep(retry_delay)
    if err:
        return (
            f"Milestone not set ({title or milestone}): {err}. The pull request was created; "
            "set it with update_github_issue."
        )
    data = _json_or_none(resp) if resp is not None else None
    got = data.get("milestone") if isinstance(data, dict) else None
    got_title = got.get("title") if isinstance(got, dict) else None
    return f"Milestone: {got_title or title or milestone}"
