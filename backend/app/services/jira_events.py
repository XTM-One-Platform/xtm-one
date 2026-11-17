# Copyright (c) 2025-2026 Filigran SAS
#
# This file is part of the XTM One Enterprise Edition ("EE") and is
# licensed under the XTM One Enterprise Edition License (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://github.com/XTM-One-Platform/xtm-one/blob/main/LICENSE
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.

"""Jira events for ``integration_event`` assignments (#11036).

An assignment with ``{"provider": "jira", "event": <event>}`` is woken by the
changes of the Jira site its integration is connected to:

* ``issue_assigned``: the assignee of an issue changed (a changelog item on
  ``assignee``);
* ``issue_created``: an issue was created;
* ``issue_updated``: any change of an issue (one event per changelog entry);
* ``issue_transitioned``: the status of an issue changed;
* ``comment_created``: a comment was added to an issue.

The events are read by the poller (``event_poller._poll_jira_issues``, every
``POLL_INTERVAL_SECONDS``): one enhanced search (``POST /rest/api/3/search/jql``)
of the issues updated since the cursor, with their changelog and comments,
narrowed in JQL by the trigger filter (``poll_jql``). Each change becomes one
payload whose ``message_id`` names it (the changelog entry, the comment, the
creation), so the assignment's seen set runs it once.

Filter keys (``filter_reason``, also applied in the JQL so the read stays
small): ``assignee_account_id`` (an account id, a list of them, or ``self``:
the account the integration is connected as), ``project_key``, ``issue_type``,
``status`` (names, case-insensitive) and ``jql`` (free JQL, ANDed into the
search, never compared with the payload). A change made by the connected
account itself starts no run when the assignment sets
``ignore_self_triggered_events``.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

logger = logging.getLogger(__name__)

PROVIDER = "jira"

ISSUE_ASSIGNED = "issue_assigned"
ISSUE_CREATED = "issue_created"
ISSUE_UPDATED = "issue_updated"
ISSUE_TRANSITIONED = "issue_transitioned"
COMMENT_CREATED = "comment_created"

EVENTS = frozenset({ISSUE_ASSIGNED, ISSUE_CREATED, ISSUE_UPDATED, ISSUE_TRANSITIONED, COMMENT_CREATED})

#: A reassignment is acted on within the minute.
POLL_INTERVAL_SECONDS = 60

#: The keys the filter reads (``filter_reason``); ``jql`` only narrows the search.
FILTER_KEYS = ("assignee_account_id", "project_key", "issue_type", "status", "jql")

#: The value of ``assignee_account_id`` naming the connected account.
SELF = "self"

#: Issues one search reads.
SEARCH_PAGE = 50

#: Minutes read before the cursor: JQL ``updated`` is to the minute, so a
#: margin keeps a change of the cursor's own minute; the seen set skips what ran.
_MARGIN_MINUTES = 2
#: How far before the cursor the changes of the issues read are taken.
WINDOW_MARGIN = timedelta(minutes=_MARGIN_MINUTES)

_SEARCH_FIELDS = ["summary", "status", "assignee", "reporter", "project", "issuetype", "created", "updated", "comment"]

_MAX_COMMENT_CHARS = 4_000


def _strings(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, (list, tuple, set)):
        return [str(v) for v in value if isinstance(v, (str, int)) and str(v).strip()]
    return []


def _jql_quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _jql_in(field: str, values: list[str]) -> str:
    return f"{field} in ({', '.join(_jql_quote(v) for v in values)})"


def parse_instant(value: Any) -> datetime:
    """A Jira timestamp (``2026-11-11T08:01:15.123+0000``) as an aware datetime; the oldest one when unreadable."""
    text = str(value or "").strip()
    if len(text) >= 5 and text[-5] in "+-" and text[-4:].isdigit():
        text = f"{text[:-2]}:{text[-2:]}"
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return datetime.min.replace(tzinfo=UTC)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def poll_jql(
    event_type: str, filter_config: Mapping[str, Any] | None, since: datetime, *, now: datetime | None = None
) -> str:
    """The JQL of one poll: the issues touched since *since*, narrowed by the filter, oldest first.

    The window is relative (``-Nm``), so the search does not depend on the
    time zone Jira reads an absolute date in.
    """
    now = now or datetime.now(UTC)
    minutes = max(1, int((now - since).total_seconds() // 60) + _MARGIN_MINUTES)
    field = "created" if event_type == ISSUE_CREATED else "updated"
    clauses = [f'{field} >= "-{minutes}m"']
    config = filter_config if isinstance(filter_config, Mapping) else {}
    assignees = _strings(config.get("assignee_account_id"))
    if assignees:
        named = [a for a in assignees if a.casefold() != SELF]
        parts = [_jql_in("assignee", named)] if named else []
        if len(named) < len(assignees):
            parts.append("assignee = currentUser()")
        clauses.append(parts[0] if len(parts) == 1 else f"({' OR '.join(parts)})")
    projects = _strings(config.get("project_key"))
    if projects:
        clauses.append(_jql_in("project", projects))
    types = _strings(config.get("issue_type"))
    if types:
        clauses.append(_jql_in("issuetype", types))
    statuses = _strings(config.get("status"))
    if statuses:
        clauses.append(_jql_in("status", statuses))
    extra = str(config.get("jql") or "").strip()
    if extra:
        clauses.append(f"({extra})")
    return " AND ".join(clauses) + f" ORDER BY {field} ASC"


def search_body(jql: str, *, limit: int = SEARCH_PAGE) -> dict[str, Any]:
    """The ``/search/jql`` body of one poll: the issues with their changelog and comments."""
    return {"jql": jql, "fields": list(_SEARCH_FIELDS), "expand": "changelog", "maxResults": limit}


def _person(value: Any) -> dict[str, str] | None:
    if not isinstance(value, Mapping):
        return None
    account = str(value.get("accountId") or "")
    if not account and not value.get("displayName"):
        return None
    return {"account_id": account, "display_name": str(value.get("displayName") or "")}


def _name(value: Any) -> str:
    return str(value.get("name") or "") if isinstance(value, Mapping) else ""


def _comment_text(body: Any) -> str:
    from app.integrations.providers.jira._common import adf_to_text

    text = adf_to_text(body).strip()
    return text if len(text) <= _MAX_COMMENT_CHARS else text[:_MAX_COMMENT_CHARS] + "..."


def _base_payload(issue: Mapping[str, Any], event_type: str, message_id: str, at: str) -> dict[str, Any]:
    fields = issue.get("fields") if isinstance(issue.get("fields"), Mapping) else {}
    project = fields.get("project") if isinstance(fields.get("project"), Mapping) else {}
    return {
        "message_id": message_id,
        "provider": PROVIDER,
        "event": event_type,
        "occurred_at": at,
        "issue_key": str(issue.get("key") or ""),
        "issue_id": str(issue.get("id") or ""),
        "summary": str(fields.get("summary") or ""),
        "status": _name(fields.get("status")),
        "issue_type": _name(fields.get("issuetype")),
        "project": {"key": str(project.get("key") or ""), "name": str(project.get("name") or "")},
        "assignee": _person(fields.get("assignee")),
        "reporter": _person(fields.get("reporter")),
        "api_url": str(issue.get("self") or ""),
    }


def issue_events(issue: Mapping[str, Any], event_type: str, *, since: datetime) -> list[dict[str, Any]]:
    """The events of *event_type* *issue* carries after *since*, oldest first."""
    key = str(issue.get("key") or "")
    if not key:
        return []
    fields = issue.get("fields") if isinstance(issue.get("fields"), Mapping) else {}
    events: list[dict[str, Any]] = []
    if event_type == ISSUE_CREATED:
        created = str(fields.get("created") or "")
        if parse_instant(created) > since:
            payload = _base_payload(issue, event_type, f"jira:{key}:created", created)
            payload["actor"] = _person(fields.get("reporter"))
            events.append(payload)
        return events
    if event_type == COMMENT_CREATED:
        block = fields.get("comment") if isinstance(fields.get("comment"), Mapping) else {}
        for comment in block.get("comments") or []:
            if not isinstance(comment, Mapping) or not comment.get("id"):
                continue
            created = str(comment.get("created") or "")
            if parse_instant(created) <= since:
                continue
            payload = _base_payload(issue, event_type, f"jira:{key}:comment:{comment['id']}", created)
            payload["actor"] = _person(comment.get("author"))
            payload["comment"] = {"id": str(comment["id"]), "body": _comment_text(comment.get("body"))}
            events.append(payload)
        return sorted(events, key=lambda e: parse_instant(e["occurred_at"]))
    changelog = issue.get("changelog") if isinstance(issue.get("changelog"), Mapping) else {}
    for history in changelog.get("histories") or []:
        if not isinstance(history, Mapping) or not history.get("id"):
            continue
        created = str(history.get("created") or "")
        if parse_instant(created) <= since:
            continue
        items = [i for i in history.get("items") or [] if isinstance(i, Mapping)]
        wanted = {ISSUE_ASSIGNED: "assignee", ISSUE_TRANSITIONED: "status"}.get(event_type)
        if wanted is not None:
            items = [i for i in items if str(i.get("field") or "").casefold() == wanted]
            if not items:
                continue
        payload = _base_payload(issue, event_type, f"jira:{key}:history:{history['id']}", created)
        payload["actor"] = _person(history.get("author"))
        payload["changelog"] = [
            {
                "field": str(i.get("field") or ""),
                "from": i.get("from"),
                "from_string": i.get("fromString"),
                "to": i.get("to"),
                "to_string": i.get("toString"),
            }
            for i in items
        ]
        if event_type == ISSUE_ASSIGNED:
            item = items[-1]
            payload["previous_assignee"] = (
                {"account_id": str(item.get("from") or ""), "display_name": str(item.get("fromString") or "")}
                if item.get("from") or item.get("fromString")
                else None
            )
            # The assignee this change set, not the issue's current one.
            payload["assignee"] = (
                {"account_id": str(item.get("to") or ""), "display_name": str(item.get("toString") or "")}
                if item.get("to") or item.get("toString")
                else None
            )
        if event_type == ISSUE_TRANSITIONED:
            item = items[-1]
            payload["previous_status"] = str(item.get("fromString") or "")
            payload["status"] = str(item.get("toString") or payload["status"])
        events.append(payload)
    return sorted(events, key=lambda e: parse_instant(e["occurred_at"]))


def names_self(filter_config: Mapping[str, Any]) -> bool:
    """The filter's ``assignee_account_id`` names the connected account (``self``)."""
    return any(a.casefold() == SELF for a in _strings(filter_config.get("assignee_account_id")))


def is_self_change(payload: Mapping[str, Any], self_account_id: str) -> bool:
    """The change was made by the connected account itself."""
    actor = payload.get("actor") if isinstance(payload.get("actor"), Mapping) else {}
    return bool(self_account_id) and str(actor.get("account_id") or "") == self_account_id


def filter_reason(
    payload: Mapping[str, Any], filter_config: Mapping[str, Any], *, self_account_id: str = ""
) -> str | None:
    """Why the trigger's filter drops a Jira event, ``None`` when it runs.

    ``self`` in ``assignee_account_id`` names *self_account_id*; unknown, it
    names nobody rather than everybody.
    """
    assignees = {a.casefold() for a in _strings(filter_config.get("assignee_account_id"))}
    if assignees:
        if SELF in assignees:
            assignees.discard(SELF)
            if self_account_id:
                assignees.add(self_account_id.casefold())
        assignee = payload.get("assignee") if isinstance(payload.get("assignee"), Mapping) else {}
        if str(assignee.get("account_id") or "").casefold() not in assignees:
            return "assignee is not in assignee_account_id"
    projects = {p.casefold() for p in _strings(filter_config.get("project_key"))}
    project = payload.get("project") if isinstance(payload.get("project"), Mapping) else {}
    if projects and str(project.get("key") or "").casefold() not in projects:
        return "project is not in project_key"
    types = {t.casefold() for t in _strings(filter_config.get("issue_type"))}
    if types and str(payload.get("issue_type") or "").casefold() not in types:
        return "issue type is not in issue_type"
    statuses = {s.casefold() for s in _strings(filter_config.get("status"))}
    if statuses and str(payload.get("status") or "").casefold() not in statuses:
        return "status is not in status"
    return None


async def self_account_id(base: str, headers: dict[str, str]) -> str:
    """The account id the integration is connected as (``GET /myself``), ``""`` when unreadable."""
    from app.integrations.providers.jira._common import api3, jira_request

    resp, err = await jira_request("get", f"{api3(base)}/myself", "read the connected account", headers=headers)
    if err or resp is None:
        return ""
    data = resp.json() if resp.content else {}
    return str(data.get("accountId") or "") if isinstance(data, dict) else ""


async def search_issues(base: str, headers: dict[str, str], jql: str) -> tuple[list[dict], bool, str | None]:
    """``(issues, more, error)``: one page of the poll's search."""
    from app.integrations.providers.jira._common import api3, jira_request

    resp, err = await jira_request(
        "post", f"{api3(base)}/search/jql", "search the changed issues", headers=headers, json=search_body(jql)
    )
    if err or resp is None:
        return [], False, err or "no response"
    data = resp.json() if resp.content else {}
    if not isinstance(data, dict):
        return [], False, "unreadable search answer"
    issues = [i for i in data.get("issues") or [] if isinstance(i, dict)]
    return issues, bool(data.get("nextPageToken")), None
