"""Whether a GitHub write a resumed turn's interrupted attempt had started landed.

A worker stop between a write and its recorded answer leaves the resumed turn
with "may have taken effect" (``turn_checkpoint.in_flight_calls``). For the two
writes whose double is costly and whose effect a read can tell, the tool
declares a ``landed_probe`` (``ToolDefinition.landed_probe``) built here, run
by the loop's in-flight repeat guard with the tool's own credential:

- ``create_github_issue``: an issue of the repository with the same title,
  opened by the authenticated account in the last :data:`LOOKBACK`;
- ``create_github_commit``: a commit on the branch (the default branch when
  none is named) with the same subject line, authored in the last
  :data:`LOOKBACK`;
- ``create_github_pr``: a pull request of the repository whose head is the
  call's branch, opened by the authenticated account in the last
  :data:`LOOKBACK`;
- ``create_github_branch``: the branch exists;
- ``add_github_comment``: a comment of the authenticated account on the issue
  or pull request with the same body, posted in the last :data:`LOOKBACK`.

Each probe answers a line naming what landed, ``False`` when the read shows
nothing, ``None`` when it cannot tell (a read error, an argument missing or
cut for the replay).

The writes but the commit also declare their natural key
(``ToolDefinition.landed_key``, the ``*_key`` functions here): the object a
call creates, whatever its wording (the repository of an issue, the head
branch of a pull request). A resumed turn that found the interrupted write
landed answers a reworded repeat on the same key with what landed, once
(``agent_loop``'s in-flight guard). A commit has none: a second commit on a
branch with another subject is another commit.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import quote

from app.core.egress import egress_client

from . import _common
from ._common import _BASE_URL, _PROVIDER, parse_repo

#: How far back a landed write is looked for: a resume follows its cut by minutes.
LOOKBACK = timedelta(hours=2)
#: Recent objects read per probe.
_PAGE = 30
#: What ``turn_checkpoint._cut_arguments`` appends to a string it cut.
_CUT_MARK = "[cut for the replay:"

Probe = Callable[[Mapping[str, Any]], Awaitable[str | bool | None]]


def _obj(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _recent(stamp: object, now: datetime) -> bool:
    if not isinstance(stamp, str) or not stamp:
        return False
    try:
        when = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return False
    return now - when <= LOOKBACK


def subject_of(message: object) -> str:
    """The first line of a commit *message*, stripped."""
    return str(message or "").strip().partition("\n")[0].strip()


def issue_match(issues: object, title: str, login: str, now: datetime) -> str | bool | None:
    """The landed answer for *title* among *issues* (a GitHub issue list), ``False`` when none matches."""
    if not isinstance(issues, list):
        return None
    for issue in issues:
        if not isinstance(issue, dict) or issue.get("pull_request"):
            continue
        author = _obj(issue.get("user"))
        if (
            str(issue.get("title") or "").strip() == title
            and (not login or str(author.get("login") or "").lower() == login.lower())
            and _recent(issue.get("created_at"), now)
        ):
            return f"issue #{issue.get('number')} ({issue.get('html_url')}), opened {issue.get('created_at')}"
    return False


def commit_match(commits: object, subject: str, now: datetime) -> str | bool | None:
    """The landed answer for *subject* among *commits* (a GitHub commit list), ``False`` when none matches."""
    if not isinstance(commits, list):
        return None
    for item in commits:
        if not isinstance(item, dict):
            continue
        commit = _obj(item.get("commit"))
        author = _obj(commit.get("author"))
        candidate = subject_of(commit.get("message"))
        if (candidate == subject or candidate == f"chore(github): {subject}") and _recent(author.get("date"), now):
            return f"commit {str(item.get('sha') or '')[:12]} ({item.get('html_url')}), authored {author.get('date')}"
    return False


def pull_match(pulls: object, head: str, login: str, now: datetime) -> str | bool | None:
    """The landed answer for the head branch *head* among *pulls* (a GitHub pull list), ``False`` when none matches."""
    if not isinstance(pulls, list):
        return None
    for pull in pulls:
        if not isinstance(pull, dict):
            continue
        ref = str(_obj(pull.get("head")).get("ref") or "")
        author = _obj(pull.get("user"))
        if (
            ref == head
            and (not login or str(author.get("login") or "").lower() == login.lower())
            and _recent(pull.get("created_at"), now)
        ):
            return (
                f"pull request #{pull.get('number')} ({pull.get('html_url')}), head {head}, "
                f"{pull.get('state')}, opened {pull.get('created_at')}"
            )
    return False


def comment_match(comments: object, body: str, login: str, now: datetime) -> str | bool | None:
    """The landed answer for *body* among *comments* (a GitHub comment list), ``False`` when none matches."""
    if not isinstance(comments, list):
        return None
    for comment in reversed(comments):
        if not isinstance(comment, dict):
            continue
        author = _obj(comment.get("user"))
        if (
            str(comment.get("body") or "").strip() == body
            and (not login or str(author.get("login") or "").lower() == login.lower())
            and _recent(comment.get("created_at"), now)
        ):
            return f"comment {comment.get('html_url')}, posted {comment.get('created_at')}"
    return False


def _repo_key(arguments: Mapping[str, Any]) -> str | None:
    owner, name, err = parse_repo(arguments.get("repo"))
    return None if err else f"{owner}/{name}".lower()


def issue_key(arguments: Mapping[str, Any]) -> str | None:
    """The natural key of a ``create_github_issue`` call: its repository."""
    return _repo_key(arguments)


def pull_key(arguments: Mapping[str, Any]) -> str | None:
    """The natural key of a ``create_github_pr`` call: its repository and head branch."""
    repo, head = _repo_key(arguments), str(arguments.get("head") or "").strip()
    return f"{repo}:{head}" if repo and head else None


def branch_key(arguments: Mapping[str, Any]) -> str | None:
    """The natural key of a ``create_github_branch`` call: its repository and branch."""
    repo, branch = _repo_key(arguments), str(arguments.get("branch") or "").strip()
    return f"{repo}:{branch}" if repo and branch else None


def comment_key(arguments: Mapping[str, Any]) -> str | None:
    """The natural key of an ``add_github_comment`` call: its repository and issue or pull request."""
    repo, number = _repo_key(arguments), arguments.get("issue_number")
    return f"{repo}#{number}" if repo and number not in (None, "") else None


async def _account(client: Any, headers: dict[str, str]) -> str:
    resp, err = await _common.safe_request(
        client, "get", f"{_BASE_URL}/user", _PROVIDER, "read the account", headers=headers
    )
    return str(resp.json().get("login") or "") if not err else ""


def issue_probe(headers: dict[str, str]) -> Probe:
    """The ``landed_probe`` of ``create_github_issue`` for the credential *headers*."""

    async def probe(arguments: Mapping[str, Any]) -> str | bool | None:
        owner, name, err = parse_repo(arguments.get("repo"))
        title = str(arguments.get("title") or "").strip()
        if err or not title or _CUT_MARK in title:
            return None
        async with egress_client(timeout=10) as client:
            login = await _account(client, headers)
            resp, err = await _common.safe_request(
                client,
                "get",
                f"{_BASE_URL}/repos/{owner}/{name}/issues",
                _PROVIDER,
                f"list recent issues of {owner}/{name}",
                headers=headers,
                params={"state": "all", "sort": "created", "direction": "desc", "per_page": _PAGE},
            )
            if err:
                return None
            return issue_match(resp.json(), title, login, datetime.now(UTC))

    return probe


def commit_probe(headers: dict[str, str]) -> Probe:
    """The ``landed_probe`` of ``create_github_commit`` for the credential *headers*."""

    async def probe(arguments: Mapping[str, Any]) -> str | bool | None:
        owner, name, err = parse_repo(arguments.get("repo"))
        subject = subject_of(arguments.get("message"))
        if err or not subject:
            return None
        params: dict[str, Any] = {"per_page": _PAGE}
        branch = str(arguments.get("branch") or "").strip()
        if branch:
            params["sha"] = branch
        async with egress_client(timeout=10) as client:
            resp, err = await _common.safe_request(
                client,
                "get",
                f"{_BASE_URL}/repos/{owner}/{name}/commits",
                _PROVIDER,
                f"list recent commits of {owner}/{name}" + (f" on {quote(branch)}" if branch else ""),
                headers=headers,
                params=params,
            )
            if err:
                return None
            return commit_match(resp.json(), subject, datetime.now(UTC))

    return probe


def pull_probe(headers: dict[str, str]) -> Probe:
    """The ``landed_probe`` of ``create_github_pr`` for the credential *headers*."""

    async def probe(arguments: Mapping[str, Any]) -> str | bool | None:
        owner, name, err = parse_repo(arguments.get("repo"))
        head = str(arguments.get("head") or "").strip()
        if err or not head or _CUT_MARK in head:
            return None
        # A fork head is written "user:branch"; the list filters on it as given.
        qualified = head if ":" in head else f"{owner}:{head}"
        async with egress_client(timeout=10) as client:
            login = await _account(client, headers)
            resp, err = await _common.safe_request(
                client,
                "get",
                f"{_BASE_URL}/repos/{owner}/{name}/pulls",
                _PROVIDER,
                f"list pull requests of {owner}/{name} from {head}",
                headers=headers,
                params={"state": "all", "head": qualified, "per_page": _PAGE},
            )
            if err:
                return None
            return pull_match(resp.json(), head.partition(":")[2] or head, login, datetime.now(UTC))

    return probe


def branch_probe(headers: dict[str, str]) -> Probe:
    """The ``landed_probe`` of ``create_github_branch`` for the credential *headers*."""

    async def probe(arguments: Mapping[str, Any]) -> str | bool | None:
        owner, name, err = parse_repo(arguments.get("repo"))
        branch = str(arguments.get("branch") or "").strip()
        if err or not branch or _CUT_MARK in branch:
            return None
        async with egress_client(timeout=10) as client:
            resp, err = await _common.safe_request(
                client,
                "get",
                f"{_BASE_URL}/repos/{owner}/{name}/git/ref/heads/{quote(branch, safe='/')}",
                _PROVIDER,
                f"read branch {branch} of {owner}/{name}",
                headers=headers,
                expected_statuses={404},
            )
            if resp is not None and resp.status_code == 404:
                return False
            if err or resp is None:
                return None
            sha = str(_obj(resp.json().get("object")).get("sha") or "")
            return f"branch {branch} of {owner}/{name}, at {sha[:12]}" if sha else None

    return probe


def comment_probe(headers: dict[str, str]) -> Probe:
    """The ``landed_probe`` of ``add_github_comment`` for the credential *headers*."""

    async def probe(arguments: Mapping[str, Any]) -> str | bool | None:
        owner, name, err = parse_repo(arguments.get("repo"))
        body = str(arguments.get("body") or "").strip()
        number = arguments.get("issue_number")
        if err or not body or _CUT_MARK in body or number in (None, ""):
            return None
        since = (datetime.now(UTC) - LOOKBACK).isoformat().replace("+00:00", "Z")
        async with egress_client(timeout=10) as client:
            login = await _account(client, headers)
            resp, err = await _common.safe_request(
                client,
                "get",
                f"{_BASE_URL}/repos/{owner}/{name}/issues/{quote(str(number))}/comments",
                _PROVIDER,
                f"list recent comments of {owner}/{name}#{number}",
                headers=headers,
                params={"since": since, "per_page": 100},
            )
            if err:
                return None
            return comment_match(resp.json(), body, login, datetime.now(UTC))

    return probe
