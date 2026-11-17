# SPDX-License-Identifier: Apache-2.0

"""JetBrains YouTrack integration – Issues, Work Items, Custom Fields, Tags, Attachments.

Provides 17 tools across 7 categories:
  0. Current User: get authenticated user profile
  1. Projects: list projects
  2. Issues: search, get, create, update, delete, add comment
  3. Work Items (Time Tracking): list per-issue, add, search globally by user/date
  4. Custom Fields: list project custom fields, get issue custom fields
  5. Tags: list available issue tags
  6. Attachments: list, download (a file of the run), upload a file of the run

Uses YouTrack REST API via:
  {base_url}/api/...

Authentication: permanent token via ``Authorization: Bearer {token}`` header.

Token format: "base_url||api_key" (compound, split by ||).
"""

from __future__ import annotations

from datetime import UTC
from typing import Any
from urllib.parse import quote, urljoin

import httpx

from app.core.egress import egress_client
from app.integrations.base import BaseIntegration, OAuthDefaults
from app.integrations.file_delivery import (
    deliver_file,
    fetch_file,
    file_content_type,
    format_kb,
    format_mb,
    read_pipe_file,
    uploaded_file_answer,
)
from app.integrations.http import (
    format_entity_list,
    format_pagination_hint,
    parse_compound_token,
    safe_request,
)
from app.tools.base import CallableToolExecutor, FileContext, ToolDefinition, ToolResult

_PROVIDER = "YouTrack"

# Fields to request from the YouTrack API for issue list/summary views
_ISSUE_LIST_FIELDS = (
    "id,idReadable,summary,description,created,updated,"
    "project(id,name,shortName),"
    "reporter(login,fullName),"
    "assignee(login,fullName),"
    "state(name),"
    "priority(name),"
    "type(name),"
    "tags(name)"
)

# Fields to request for issue detail view (includes customFields)
_ISSUE_DETAIL_FIELDS = (
    "id,idReadable,summary,description,created,updated,"
    "project(id,name,shortName),"
    "reporter(login,fullName),"
    "assignee(login,fullName),"
    "state(name),"
    "priority(name),"
    "type(name),"
    "tags(name),"
    "customFields(name,value(name,login,fullName,text,minutes)),"
    "attachments(id,name,size,mimeType,url,created,removed,author(login,fullName),comment(id))"
)

# Fields to request for an issue attachment
_ATTACHMENT_FIELDS = "id,name,size,mimeType,url,created,removed,author(login,fullName),comment(id)"

#: Largest issue attachment downloaded or uploaded.
_MAX_ATTACHMENT_BYTES = 50 * 1024 * 1024

# Fields to request for global work item search results
_WORK_ITEM_SEARCH_FIELDS = (
    "id,text,date,duration(minutes),"
    "author(login,fullName),"
    "issue(id,idReadable,summary,project(shortName)),"
    "type(name)"
)


class YouTrackIntegration(BaseIntegration):
    provider_name = "youtrack"
    display_name = "YouTrack"
    icon = "ClipboardList"
    description = (
        "Connect JetBrains YouTrack for issue tracking, project management, "
        "time tracking, and agile boards."
    )
    auth_types = ["api_key"]

    def get_oauth_defaults(self) -> OAuthDefaults | None:
        return None

    async def validate_connection(self, token: str) -> bool:
        _base_raw, api_key = parse_compound_token(token, 2)
        base_url = _base_raw.rstrip("/")
        if not base_url:
            return False
        async with egress_client(timeout=15) as client:
            resp, _err = await safe_request(
                client,
                "get",
                f"{base_url}/api/users/me",
                _PROVIDER,
                "validate connection",
                headers=_hdrs(api_key),
                params={"fields": "id,login,fullName"},
            )
            return resp is not None

    async def get_user_info(self, token: str) -> dict:
        _base_raw, api_key = parse_compound_token(token, 2)
        base_url = _base_raw.rstrip("/")
        async with egress_client(timeout=15) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{base_url}/api/users/me",
                _PROVIDER,
                "get user info",
                headers=_hdrs(api_key),
                params={"fields": "id,login,fullName,email"},
            )
            if resp is None:
                return {"error": err}
            data = resp.json()
            return {
                "login": data.get("login"),
                "name": data.get("fullName"),
                "email": data.get("email"),
            }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _hdrs(api_key: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


def _format_issue(issue: dict, *, detail: bool = False) -> str:
    """Format a YouTrack issue dict into a readable string."""
    readable_id = issue.get("idReadable", issue.get("id", "?"))
    summary = issue.get("summary", "No summary")
    project = (issue.get("project") or {}).get("shortName", "?")
    state = (issue.get("state") or {}).get("name", "?")
    priority = (issue.get("priority") or {}).get("name", "?")
    issue_type = (issue.get("type") or {}).get("name", "?")
    assignee = (issue.get("assignee") or {}).get("fullName") or (
        issue.get("assignee") or {}
    ).get("login", "Unassigned")
    reporter = (issue.get("reporter") or {}).get("fullName") or (
        issue.get("reporter") or {}
    ).get("login", "?")
    created = issue.get("created")
    updated = issue.get("updated")

    lines = [f"{readable_id} [{project}]: {summary}"]
    lines.append(f"  State: {state} | Priority: {priority} | Type: {issue_type}")
    lines.append(f"  Assignee: {assignee} | Reporter: {reporter}")

    tags = issue.get("tags", [])
    if tags:
        tag_names = [t.get("name", "?") for t in tags if isinstance(t, dict)]
        if tag_names:
            lines.append(f"  Tags: {', '.join(tag_names)}")

    if detail:
        if created:
            lines.append(f"  Created: {_ms_to_str(created)}")
        if updated:
            lines.append(f"  Updated: {_ms_to_str(updated)}")
        desc = issue.get("description", "")
        if desc:
            preview = desc[:1000] + ("…" if len(desc) > 1000 else "")
            lines.append(f"\n  Description:\n  {preview}")

        custom_fields = issue.get("customFields", [])
        if custom_fields:
            lines.append("\n  Custom Fields:")
            for cf in custom_fields:
                name = cf.get("name", "?")
                val = cf.get("value")
                if val is None:
                    val_str = "—"
                elif isinstance(val, dict):
                    val_str = (
                        val.get("name")
                        or val.get("fullName")
                        or val.get("login")
                        or val.get("text")
                        or str(val)
                    )
                elif isinstance(val, list):
                    val_str = ", ".join(
                        (
                            v.get("name") or v.get("fullName") or str(v)
                            if isinstance(v, dict)
                            else str(v)
                        )
                        for v in val
                    )
                else:
                    val_str = str(val)
                lines.append(f"    {name}: {val_str}")

    return "\n".join(lines)


def _attachment_url(base_url: str, attachment: dict) -> str:
    """The attachment's signed ``url``, absolute (YouTrack answers it host-relative)."""
    url = str(attachment.get("url") or "").strip()
    return urljoin(f"{base_url}/", url) if url else ""


def _live_attachments(attachments: Any) -> list[dict]:
    return [
        a for a in attachments or [] if isinstance(a, dict) and a.get("id") and not a.get("removed")
    ]


def _format_attachment(base_url: str, attachment: dict) -> str:
    parts = [f"{attachment.get('name') or 'attachment'} (id: {attachment['id']})"]
    if isinstance(attachment.get("size"), int):
        parts.append(format_kb(attachment["size"]))
    if attachment.get("mimeType"):
        parts.append(str(attachment["mimeType"]))
    author = attachment.get("author") or {}
    if author.get("fullName") or author.get("login"):
        parts.append(f"by {author.get('fullName') or author.get('login')}")
    if attachment.get("created"):
        parts.append(_ms_to_str(attachment["created"]))
    if isinstance(attachment.get("comment"), dict):
        parts.append(f"on comment {attachment['comment'].get('id', '?')}")
    line = " | ".join(parts)
    url = _attachment_url(base_url, attachment)
    if url:
        line += f"\n      Download URL: {url}"
    return line


def _ms_to_str(ms: Any) -> str:
    """Convert millisecond timestamp to a readable date string (best-effort)."""
    try:
        from datetime import datetime

        return datetime.fromtimestamp(int(ms) / 1000, tz=UTC).strftime(
            "%Y-%m-%d %H:%M UTC"
        )
    except Exception:
        return str(ms)


def _format_work_item(wi: dict) -> str:
    """Format a YouTrack work item into a readable string."""
    wi_id = wi.get("id", "?")
    author = (wi.get("author") or {}).get("fullName") or (
        wi.get("author") or {}
    ).get("login", "?")
    minutes = (wi.get("duration") or {}).get("minutes", 0)
    hours = minutes // 60
    mins = minutes % 60
    duration_str = f"{hours}h {mins}m" if hours else f"{mins}m"
    date = wi.get("date")
    date_str = _ms_to_str(date) if date else "?"
    text = wi.get("text", "")

    lines = [f"Work Item {wi_id}: {duration_str} by {author} on {date_str}"]
    if text:
        lines.append(f"  Note: {text[:200]}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Tool builder
# ---------------------------------------------------------------------------


def build_tools(
    token: str, executor: CallableToolExecutor, **_kwargs: Any
) -> list[ToolDefinition]:
    _base_raw, api_key = parse_compound_token(token, 2)
    base_url = _base_raw.rstrip("/")

    if not base_url:
        return []  # No base_url — can't make API calls

    api = f"{base_url}/api"
    headers = _hdrs(api_key)
    persist_file_fn = _kwargs.get("persist_file_fn")

    # ══════════════════════════════════════════════════════════════════════
    # 0. CURRENT USER
    # ══════════════════════════════════════════════════════════════════════

    async def get_youtrack_myself() -> str:
        """Get the authenticated YouTrack user's profile (login, name, email).

        Useful for understanding which account the integration is connected as
        and for building issue queries referencing the current user.
        """
        async with egress_client(timeout=15) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{api}/users/me",
                _PROVIDER,
                "get current user profile",
                headers=headers,
                params={"fields": "id,login,fullName,email,guest"},
            )
            if err:
                return err
            me = resp.json()
            lines = [
                f"Authenticated as: {me.get('fullName', me.get('login', '?'))}",
                f"  Login: {me.get('login', '?')}",
                f"  Email: {me.get('email', 'N/A')}",
                f"  ID: {me.get('id', '?')}",
            ]
            if me.get("guest"):
                lines.append("  (Guest account)")
            lines.append(
                "\nUse the login value for issue queries like 'assignee: LOGIN'."
            )
            return "\n".join(lines)

    # ══════════════════════════════════════════════════════════════════════
    # 1. PROJECTS
    # ══════════════════════════════════════════════════════════════════════

    async def list_youtrack_projects(
        query: str = "", limit: int = 50, skip: int = 0
    ) -> str:
        """List YouTrack projects accessible to the current user.

        Optionally filter by name using the query parameter.
        Use skip for pagination (offset-based).
        """
        async with egress_client(timeout=20) as client:
            params: dict[str, Any] = {
                "fields": "id,name,shortName,description",
                "$top": min(limit, 100),
                "$skip": skip,
            }
            if query:
                params["query"] = query
            resp, err = await safe_request(
                client,
                "get",
                f"{api}/admin/projects",
                _PROVIDER,
                "list projects",
                headers=headers,
                params=params,
            )
            if err:
                return err
            projects = resp.json()
            if not isinstance(projects, list) or not projects:
                return "No projects found."
            lines = [f"Projects ({len(projects)}):"]
            for p in projects:
                short = p.get("shortName", "?")
                name = p.get("name", "?")
                desc = p.get("description", "")
                desc_str = f" — {desc[:80]}" if desc else ""
                lines.append(f"  {short}: {name}{desc_str}")
            result = "\n".join(lines)
            hint = format_pagination_hint(
                skip + len(projects) if len(projects) == min(limit, 100) else None,
                param_name="skip",
            )
            return result + hint

    # ══════════════════════════════════════════════════════════════════════
    # 2. ISSUES
    # ══════════════════════════════════════════════════════════════════════

    async def search_youtrack_issues(
        query: str,
        limit: int = 25,
        skip: int = 0,
    ) -> str:
        """Search YouTrack issues using the YouTrack query language.

        Examples:
        - 'project: PROJ state: Open'
        - 'assignee: me priority: Critical'
        - 'tag: bug created: today'
        - '#PROJ-123'

        Use skip for pagination (offset-based).
        """
        async with egress_client(timeout=30) as client:
            params: dict[str, Any] = {
                "query": query,
                "fields": _ISSUE_LIST_FIELDS,
                "$top": min(limit, 100),
                "$skip": skip,
            }
            resp, err = await safe_request(
                client,
                "get",
                f"{api}/issues",
                _PROVIDER,
                f"search issues: {query}",
                headers=headers,
                params=params,
            )
            if err:
                return err
            issues = resp.json()
            if not isinstance(issues, list) or not issues:
                return f"No issues found for query: {query}"
            result = format_entity_list(issues, _format_issue, noun="issue")
            hint = format_pagination_hint(
                skip + len(issues) if len(issues) == min(limit, 100) else None,
                param_name="skip",
            )
            return result + hint

    async def get_youtrack_issue(issue_id: str) -> str:
        """Get full details of a YouTrack issue including description and custom fields.

        issue_id can be the readable ID (e.g. 'PROJ-123') or internal ID.
        """
        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{api}/issues/{issue_id}",
                _PROVIDER,
                f"get issue {issue_id}",
                headers=headers,
                params={"fields": _ISSUE_DETAIL_FIELDS},
            )
            if err:
                return err
            issue = resp.json()
            result = _format_issue(issue, detail=True)
            attachments = _live_attachments(issue.get("attachments"))
            if attachments:
                result += f"\n\n  Attachments ({len(attachments)}):"
                for attachment in attachments:
                    result += f"\n    {_format_attachment(base_url, attachment)}"

            # Fetch recent comments (non-critical)
            comments_resp, comments_err = await safe_request(
                client,
                "get",
                f"{api}/issues/{issue_id}/comments",
                _PROVIDER,
                f"get comments for {issue_id}",
                headers=headers,
                params={
                    "fields": "id,text,created,author(login,fullName)",
                    "$top": 20,
                },
            )
            if not comments_err:
                comments = comments_resp.json()
                if isinstance(comments, list) and comments:
                    result += f"\n\nComments ({len(comments)}):"
                    for c in comments:
                        author = (c.get("author") or {}).get("fullName") or (
                            c.get("author") or {}
                        ).get("login", "?")
                        created = _ms_to_str(c.get("created")) if c.get("created") else "?"
                        text = c.get("text", "")
                        preview = text[:400] + ("…" if len(text) > 400 else "")
                        result += f"\n\n--- {author} ({created}) ---\n{preview}"
                else:
                    result += "\n\nNo comments."
            return result

    async def create_youtrack_issue(
        project_id: str,
        summary: str,
        description: str = "",
        assignee_login: str = "",
        priority: str = "",
        issue_type: str = "",
    ) -> str:
        """Create a new YouTrack issue.

        project_id: the project short name (e.g. 'PROJ') or internal ID.
        priority examples: Critical, Major, Normal, Minor, Show-stopper.
        issue_type examples: Bug, Feature, Task, Usability Problem, Exception.
        """
        payload: dict[str, Any] = {
            "project": {"id": project_id},
            "summary": summary,
        }
        if description:
            payload["description"] = description

        custom_fields: list[dict[str, Any]] = []
        if assignee_login:
            custom_fields.append(
                {
                    "$type": "SingleUserIssueCustomField",
                    "name": "Assignee",
                    "value": {"login": assignee_login},
                }
            )
        if priority:
            custom_fields.append(
                {
                    "$type": "SingleEnumIssueCustomField",
                    "name": "Priority",
                    "value": {"name": priority},
                }
            )
        if issue_type:
            custom_fields.append(
                {
                    "$type": "SingleEnumIssueCustomField",
                    "name": "Type",
                    "value": {"name": issue_type},
                }
            )
        if custom_fields:
            payload["customFields"] = custom_fields

        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "post",
                f"{api}/issues",
                _PROVIDER,
                f"create issue in {project_id}",
                headers=headers,
                params={"fields": "id,idReadable,summary"},
                json=payload,
            )
            if err:
                return err
            created = resp.json()
            readable_id = created.get("idReadable", created.get("id", "?"))
            return f"Issue created: {readable_id} — {created.get('summary', summary)}"

    async def update_youtrack_issue(
        issue_id: str,
        summary: str = "",
        description: str = "",
        state: str = "",
        assignee_login: str = "",
        priority: str = "",
    ) -> str:
        """Update fields on an existing YouTrack issue. Only provided fields are changed.

        issue_id: the readable ID (e.g. 'PROJ-123') or internal ID.
        state examples: Open, In Progress, Fixed, Won't fix, Duplicate.
        """
        payload: dict[str, Any] = {}
        if summary:
            payload["summary"] = summary
        if description:
            payload["description"] = description

        custom_fields: list[dict[str, Any]] = []
        if state:
            custom_fields.append(
                {
                    "$type": "StateIssueCustomField",
                    "name": "State",
                    "value": {"name": state},
                }
            )
        if assignee_login:
            custom_fields.append(
                {
                    "$type": "SingleUserIssueCustomField",
                    "name": "Assignee",
                    "value": {"login": assignee_login},
                }
            )
        if priority:
            custom_fields.append(
                {
                    "$type": "SingleEnumIssueCustomField",
                    "name": "Priority",
                    "value": {"name": priority},
                }
            )
        if custom_fields:
            payload["customFields"] = custom_fields

        if not payload:
            return "No fields to update."

        async with egress_client(timeout=30) as client:
            _, err = await safe_request(
                client,
                "post",
                f"{api}/issues/{issue_id}",
                _PROVIDER,
                f"update issue {issue_id}",
                headers=headers,
                params={"fields": "id,idReadable"},
                json=payload,
            )
            if err:
                return err
            return f"Issue {issue_id} updated successfully."

    async def delete_youtrack_issue(issue_id: str) -> str:
        """Delete a YouTrack issue permanently. This action cannot be undone."""
        async with egress_client(timeout=20) as client:
            _, err = await safe_request(
                client,
                "delete",
                f"{api}/issues/{issue_id}",
                _PROVIDER,
                f"delete issue {issue_id}",
                headers=headers,
            )
            if err:
                return err
            return f"Issue {issue_id} deleted successfully."

    async def add_youtrack_comment(issue_id: str, text: str) -> str:
        """Add a comment to a YouTrack issue.

        issue_id: the readable ID (e.g. 'PROJ-123') or internal ID.
        text: the comment text (Markdown supported).
        """
        async with egress_client(timeout=20) as client:
            resp, err = await safe_request(
                client,
                "post",
                f"{api}/issues/{issue_id}/comments",
                _PROVIDER,
                f"add comment to {issue_id}",
                headers=headers,
                params={"fields": "id,text"},
                json={"text": text},
            )
            if err:
                return err
            comment = resp.json()
            return f"Comment added to {issue_id} (id: {comment.get('id', '?')})"

    # ══════════════════════════════════════════════════════════════════════
    # 3. WORK ITEMS (TIME TRACKING)
    # ══════════════════════════════════════════════════════════════════════

    async def list_youtrack_work_items(
        issue_id: str, limit: int = 25, skip: int = 0
    ) -> str:
        """List time tracking work items for a YouTrack issue.

        Returns work items with duration, author, date, and notes.
        Use skip for pagination (offset-based).
        """
        async with egress_client(timeout=20) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{api}/issues/{issue_id}/timeTracking/workItems",
                _PROVIDER,
                f"list work items for {issue_id}",
                headers=headers,
                params={
                    "fields": "id,text,date,duration(minutes),author(login,fullName)",
                    "$top": min(limit, 100),
                    "$skip": skip,
                },
            )
            if err:
                return err
            items = resp.json()
            if not isinstance(items, list) or not items:
                return f"No work items found for issue {issue_id}."
            result = format_entity_list(items, _format_work_item, noun="work item")
            hint = format_pagination_hint(
                skip + len(items) if len(items) == min(limit, 100) else None,
                param_name="skip",
            )
            return result + hint

    async def add_youtrack_work_item(
        issue_id: str,
        duration_minutes: int,
        date: str = "",
        text: str = "",
    ) -> str:
        """Add a time tracking work item to a YouTrack issue.

        issue_id: the readable ID (e.g. 'PROJ-123') or internal ID.
        duration_minutes: time spent in minutes (e.g. 90 = 1h 30m).
        date: ISO 8601 date (e.g. '2025-01-15'). Defaults to today if omitted.
        text: optional work description or note.
        """
        if duration_minutes <= 0:
            return "Error: duration_minutes must be a positive integer."

        # Build date timestamp (milliseconds since epoch)
        date_ms: int | None = None
        if date:
            try:
                from datetime import datetime

                dt = datetime.strptime(date, "%Y-%m-%d").replace(tzinfo=UTC)
                date_ms = int(dt.timestamp() * 1000)
            except ValueError:
                return f"Error: date must be in YYYY-MM-DD format, got '{date}'."

        payload: dict[str, Any] = {
            "duration": {"minutes": duration_minutes},
        }
        if date_ms is not None:
            payload["date"] = date_ms
        if text:
            payload["text"] = text

        async with egress_client(timeout=20) as client:
            resp, err = await safe_request(
                client,
                "post",
                f"{api}/issues/{issue_id}/timeTracking/workItems",
                _PROVIDER,
                f"add work item to {issue_id}",
                headers=headers,
                params={"fields": "id,duration(minutes),date"},
                json=payload,
            )
            if err:
                return err
            wi = resp.json()
            mins = (wi.get("duration") or {}).get("minutes", duration_minutes)
            h, m = divmod(mins, 60)
            dur_str = f"{h}h {m}m" if h else f"{m}m"
            return f"Work item added to {issue_id}: {dur_str} (id: {wi.get('id', '?')})"

    async def search_youtrack_work_items(
        author: str = "",
        start_date: str = "",
        end_date: str = "",
        project: str = "",
        query: str = "",
        limit: int = 25,
        skip: int = 0,
    ) -> str:
        """Search work items (time tracking entries) across all YouTrack issues.

        Prefer the dedicated parameters (author, start_date, end_date, project) over
        the raw query string — they are sent as structured filters to the YouTrack
        work-item search API. Use query only for advanced filters not covered by the
        other params; it is passed through as-is as an additional raw query clause.

        author:     YouTrack login (e.g. 'john.doe') or 'me' for the current user.
        start_date: Inclusive lower bound in YYYY-MM-DD format (e.g. '2025-01-01').
        end_date:   Inclusive upper bound in YYYY-MM-DD format (e.g. '2025-01-31').
        project:    Project short name (e.g. 'PROJ') to narrow results to one project.
        query:      Any additional raw YouTrack query clauses (sent as-is).

        Use skip for pagination.
        """

        async with egress_client(timeout=30) as client:
            params: dict[str, Any] = {
                "query": query,
                "fields": _WORK_ITEM_SEARCH_FIELDS,
                "author": author,
                "startDate": start_date,
                "endDate": end_date,
                "project": project,
                "$top": min(limit, 100),
                "$skip": skip,
            }
            resp, err = await safe_request(
                client,
                "get",
                f"{api}/workItems",
                _PROVIDER,
                f"search work items: {query}",
                headers=headers,
                params=params,
            )
            if err:
                return err
            items = resp.json()
            if not isinstance(items, list) or not items:
                return f"No work items found for query: {query}"

            def _fmt_search_wi(wi: dict) -> str:
                wi_id = wi.get("id", "?")
                author = (wi.get("author") or {}).get("fullName") or (
                    wi.get("author") or {}
                ).get("login", "?")
                minutes = (wi.get("duration") or {}).get("minutes", 0)
                hours, mins = divmod(minutes, 60)
                duration_str = f"{hours}h {mins}m" if hours else f"{mins}m"
                date_str = _ms_to_str(wi["date"]) if wi.get("date") else "?"
                issue = wi.get("issue") or {}
                issue_id = issue.get("idReadable", issue.get("id", "?"))
                issue_summary = issue.get("summary", "")
                project_short = (issue.get("project") or {}).get("shortName", "")
                work_type = (wi.get("type") or {}).get("name", "")
                text = wi.get("text", "")

                prefix = f"[{project_short}] " if project_short else ""
                issue_ref = f"{prefix}{issue_id}"
                if issue_summary:
                    issue_ref += f" — {issue_summary[:80]}"

                lines = [f"Work Item {wi_id}: {duration_str} by {author} on {date_str}"]
                lines.append(f"  Issue: {issue_ref}")
                if work_type:
                    lines.append(f"  Type: {work_type}")
                if text:
                    lines.append(f"  Note: {text[:200]}")
                return "\n".join(lines)

            result = format_entity_list(items, _fmt_search_wi, noun="work item")
            hint = format_pagination_hint(
                skip + len(items) if len(items) == min(limit, 100) else None,
                param_name="skip",
            )
            return result + hint

    # ══════════════════════════════════════════════════════════════════════
    # 4. CUSTOM FIELDS
    # ══════════════════════════════════════════════════════════════════════

    async def list_youtrack_custom_fields(
        project_id: str, limit: int = 50, skip: int = 0
    ) -> str:
        """List custom field definitions for a YouTrack project.

        project_id: the project short name (e.g. 'PROJ') or internal ID.
        Returns field names, types, and allowed values where applicable.
        Use skip for pagination (offset-based).
        """
        async with egress_client(timeout=20) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{api}/admin/projects/{project_id}/customFields",
                _PROVIDER,
                f"list custom fields for project {project_id}",
                headers=headers,
                params={
                    "fields": "id,field(id,name,fieldType(id)),bundle(values(name))",
                    "$top": min(limit, 200),
                    "$skip": skip,
                },
            )
            if err:
                return err
            fields = resp.json()
            if not isinstance(fields, list) or not fields:
                return f"No custom fields found for project {project_id}."
            lines = [f"Custom fields for project {project_id} ({len(fields)}):"]
            for cf in fields:
                field_meta = cf.get("field") or {}
                name = field_meta.get("name", "?")
                field_type = (field_meta.get("fieldType") or {}).get("id", "?")
                bundle = cf.get("bundle") or {}
                values = bundle.get("values", [])
                val_str = ""
                if values and isinstance(values, list):
                    val_names = [
                        v.get("name", "?") for v in values[:10] if isinstance(v, dict)
                    ]
                    if val_names:
                        val_str = f" [values: {', '.join(val_names)}"
                        if len(values) > 10:
                            val_str += f" +{len(values) - 10} more"
                        val_str += "]"
                lines.append(f"  {name} ({field_type}){val_str}")
            result = "\n".join(lines)
            hint = format_pagination_hint(
                skip + len(fields) if len(fields) == min(limit, 200) else None,
                param_name="skip",
            )
            return result + hint

    async def get_youtrack_issue_custom_fields(
        issue_id: str, limit: int = 50, skip: int = 0
    ) -> str:
        """Get the custom field values currently set on a YouTrack issue.

        issue_id: the readable ID (e.g. 'PROJ-123') or internal ID.
        Returns each field name with its current value.
        Use skip for pagination (offset-based).
        """
        async with egress_client(timeout=20) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{api}/issues/{issue_id}/customFields",
                _PROVIDER,
                f"get custom fields for issue {issue_id}",
                headers=headers,
                params={
                    "fields": "id,name,value(name,login,fullName,text,minutes,presentation)",
                    "$top": min(limit, 200),
                    "$skip": skip,
                },
            )
            if err:
                return err
            fields = resp.json()
            if not isinstance(fields, list) or not fields:
                return f"No custom fields found for issue {issue_id}."
            lines = [f"Custom fields for {issue_id} ({len(fields)}):"]
            for cf in fields:
                name = cf.get("name", "?")
                val = cf.get("value")
                if val is None:
                    val_str = "—"
                elif isinstance(val, dict):
                    val_str = (
                        val.get("presentation")
                        or val.get("name")
                        or val.get("fullName")
                        or val.get("login")
                        or val.get("text")
                        or str(val)
                    )
                elif isinstance(val, list):
                    val_str = ", ".join(
                        (
                            v.get("name") or v.get("fullName") or str(v)
                            if isinstance(v, dict)
                            else str(v)
                        )
                        for v in val
                    )
                else:
                    val_str = str(val)
                lines.append(f"  {name}: {val_str}")
            result = "\n".join(lines)
            hint = format_pagination_hint(
                skip + len(fields) if len(fields) == min(limit, 200) else None,
                param_name="skip",
            )
            return result + hint

    # ══════════════════════════════════════════════════════════════════════
    # 5. TAGS
    # ══════════════════════════════════════════════════════════════════════

    async def list_youtrack_tags(
        query: str = "", limit: int = 50, skip: int = 0
    ) -> str:
        """List issue tags available in the YouTrack instance.

        Optionally filter by tag name using the query parameter.
        Returns tag names that can be used in issue search queries with 'tag: NAME'.
        Use skip for pagination (offset-based).

        Use this to discover available tags before building issue search queries.
        """
        async with egress_client(timeout=20) as client:
            params: dict[str, Any] = {
                "fields": "id,name,owner(login,fullName),issues(id)",
                "$top": min(limit, 200),
                "$skip": skip,
            }
            if query:
                params["query"] = query
            resp, err = await safe_request(
                client,
                "get",
                f"{api}/issueTags",
                _PROVIDER,
                "list issue tags",
                headers=headers,
                params=params,
            )
            if err:
                return err
            tags = resp.json()
            if not isinstance(tags, list) or not tags:
                return "No tags found."
            lines = [f"Issue tags ({len(tags)}):"]
            for t in tags:
                name = t.get("name", "?")
                owner = (t.get("owner") or {}).get("fullName") or (
                    t.get("owner") or {}
                ).get("login", "")
                owner_str = f" (owner: {owner})" if owner else ""
                lines.append(f"  tag: {name}{owner_str}")
            lines.append(
                "\nUse these in issue search queries, e.g. 'tag: bug project: PROJ'."
            )
            result = "\n".join(lines)
            hint = format_pagination_hint(
                skip + len(tags) if len(tags) == min(limit, 200) else None,
                param_name="skip",
            )
            return result + hint

    # ══════════════════════════════════════════════════════════════════════
    # 6. ATTACHMENTS
    # ══════════════════════════════════════════════════════════════════════

    async def _issue_attachments(issue_id: str) -> tuple[list[dict], str | None]:
        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{api}/issues/{quote(issue_id, safe='')}/attachments",
                _PROVIDER,
                f"list attachments of {issue_id}",
                headers=headers,
                params={"fields": _ATTACHMENT_FIELDS, "$top": 200},
            )
        if err:
            return [], err
        return _live_attachments(resp.json()), None

    async def list_youtrack_attachments(issue_id: str) -> str:
        """List an issue's attachments (issue and comments) with their links."""
        attachments, err = await _issue_attachments(issue_id)
        if err:
            return err
        if not attachments:
            return f"No attachments on {issue_id}."
        lines = [f"Attachments on {issue_id} ({len(attachments)}):"]
        lines.extend(f"  {_format_attachment(base_url, a)}" for a in attachments)
        lines.append("\nTip: download_youtrack_attachment(issue_id, attachment_id) stores one as a file.")
        return "\n".join(lines)

    async def download_youtrack_attachment(
        issue_id: str, attachment_id: str
    ) -> str | ToolResult:
        """Download an issue attachment and store it as a file of the run."""
        attachments, err = await _issue_attachments(issue_id)
        if err:
            return err
        attachment = next(
            (a for a in attachments if str(a.get("id")) == str(attachment_id).strip()), None
        )
        if attachment is None:
            listed = ", ".join(f"{a.get('name')} (id: {a.get('id')})" for a in attachments)
            return f"Error: no attachment '{attachment_id}' on {issue_id}." + (
                f" Attachments: {listed}." if listed else ""
            )
        name = str(attachment.get("name") or f"youtrack-attachment-{attachment_id}")
        url = _attachment_url(base_url, attachment)
        if not url:
            return f"Error: YouTrack gave no download URL for '{name}'."
        size = attachment.get("size")
        if isinstance(size, int) and size > _MAX_ATTACHMENT_BYTES:
            return (
                f"Error: '{name}' is {format_mb(size)}, over the "
                f"{_MAX_ATTACHMENT_BYTES // 1024 // 1024} MB download limit.\nDownload URL: {url}"
            )
        # The URL is signed; the token goes to the instance's own host only.
        same_host = httpx.URL(url).host == httpx.URL(base_url).host
        async with egress_client(timeout=60) as client:
            fetched = await fetch_file(
                client,
                "get",
                url,
                provider=_PROVIDER,
                action=f"download attachment {attachment_id}",
                max_bytes=_MAX_ATTACHMENT_BYTES,
                headers={"Authorization": headers["Authorization"]} if same_host else {},
                follow_redirects=True,
            )
        if isinstance(fetched, str):
            return f"{fetched}\nDownload URL: {url}"
        return await deliver_file(
            data=fetched.data,
            filename=name,
            content_type=file_content_type(
                attachment.get("mimeType") or fetched.content_type, name
            ),
            source="YouTrack",
            persist_file_fn=persist_file_fn,
            links=[("Download URL", url)],
            details=[f"Attachment of {issue_id}."],
        )

    async def upload_youtrack_attachment(
        issue_id: str,
        file_id: str,
        filename: str = "",
        file_context: FileContext | None = None,
    ) -> str | ToolResult:
        """Attach a file of the run to an issue, byte-for-byte."""
        piped = await read_pipe_file(
            file_context, file_id, max_bytes=_MAX_ATTACHMENT_BYTES, target="YouTrack"
        )
        if isinstance(piped, str):
            return ToolResult(tool_call_id="", content=piped, is_error=True)
        data, piped_name, content_type = piped
        file_id = file_id.strip()
        name = (filename or "").strip() or piped_name
        async with egress_client(timeout=120) as client:
            resp, err = await safe_request(
                client,
                "post",
                f"{api}/issues/{quote(issue_id, safe='')}/attachments",
                _PROVIDER,
                f"attach file {file_id} to {issue_id}",
                headers={k: v for k, v in headers.items() if k != "Content-Type"},
                params={"fields": _ATTACHMENT_FIELDS},
                files={"file": (name, data, content_type)},
            )
        if err:
            return err
        created = resp.json()
        created = created[0] if isinstance(created, list) and created else created
        # YouTrack's attachment line repeats the name: it goes in the block.
        details = (
            [f"Attachment: {_format_attachment(base_url, created)}"]
            if isinstance(created, dict) and created.get("id")
            else []
        )
        return uploaded_file_answer(
            f"Attached file {file_id} to {issue_id}.",
            target=_PROVIDER,
            filename=name,
            details=details,
        )

    # ══════════════════════════════════════════════════════════════════════
    # REGISTER ALL TOOLS
    # ══════════════════════════════════════════════════════════════════════

    # 0. Current User
    executor.register("get_youtrack_myself", get_youtrack_myself)
    # 1. Projects
    executor.register("list_youtrack_projects", list_youtrack_projects)
    # 2. Issues
    executor.register("search_youtrack_issues", search_youtrack_issues)
    executor.register("get_youtrack_issue", get_youtrack_issue)
    executor.register("create_youtrack_issue", create_youtrack_issue)
    executor.register("update_youtrack_issue", update_youtrack_issue)
    executor.register("delete_youtrack_issue", delete_youtrack_issue)
    executor.register("add_youtrack_comment", add_youtrack_comment)
    # 3. Work Items
    executor.register("list_youtrack_work_items", list_youtrack_work_items)
    executor.register("add_youtrack_work_item", add_youtrack_work_item)
    executor.register("search_youtrack_work_items", search_youtrack_work_items)
    # 4. Custom Fields
    executor.register("list_youtrack_custom_fields", list_youtrack_custom_fields)
    executor.register(
        "get_youtrack_issue_custom_fields", get_youtrack_issue_custom_fields
    )
    # 5. Tags
    executor.register("list_youtrack_tags", list_youtrack_tags)
    # 6. Attachments
    executor.register("list_youtrack_attachments", list_youtrack_attachments)
    executor.register("download_youtrack_attachment", download_youtrack_attachment)
    executor.register("upload_youtrack_attachment", upload_youtrack_attachment)

    return [
        # ── 6. Attachments ────────────────────────────────────────────────
        ToolDefinition(
            name="list_youtrack_attachments",
            description=(
                "List the attachments of a YouTrack issue and its comments with name, size, "
                "type, author, attachment id and download URL."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "issue_id": {"type": "string", "description": "Issue ID (e.g. 'PROJ-123')"},
                },
                "required": ["issue_id"],
            },
            source="integration:youtrack",
            priority=1,
        ),
        ToolDefinition(
            name="download_youtrack_attachment",
            workspace_ingest=True,
            description=(
                "Download an issue attachment by id into the conversation: a download card, its "
                "download URL and a file_id to forward to any tool that takes a file; its "
                "readable content comes with the result. Use list_youtrack_attachments to find "
                "ids. Include the returned [[FILE:...]] marker verbatim so the user gets a "
                "download card."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "issue_id": {"type": "string", "description": "Issue ID (e.g. 'PROJ-123')"},
                    "attachment_id": {"type": "string", "description": "Attachment id from list_youtrack_attachments"},
                },
                "required": ["issue_id", "attachment_id"],
            },
            source="integration:youtrack",
            priority=1,
        ),
        ToolDefinition(
            name="upload_youtrack_attachment",
            description=(
                "Attach a file of this conversation to a YouTrack issue byte-for-byte: any file "
                "attached by the user, generated, or downloaded by another tool (a log, a "
                "screenshot, a report...), by its file_id."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "issue_id": {"type": "string", "description": "Issue ID (e.g. 'PROJ-123')"},
                    "file_id": {"type": "string", "description": "file_id of a file of this conversation"},
                    "filename": {
                        "type": "string",
                        "description": "Optional attachment name (default: the file's own name)",
                        "default": "",
                    },
                },
                "required": ["issue_id", "file_id"],
            },
            source="integration:youtrack",
            priority=1,
        ),
        # ── 0. Current User ───────────────────────────────────────────────
        ToolDefinition(
            name="get_youtrack_myself",
            description=(
                "Get the authenticated YouTrack user's profile — login, display name, and email. "
                "Use this to discover your own login for issue queries (e.g. 'assignee: LOGIN') "
                "or to understand which account the integration is connected as."
            ),
            input_schema={
                "type": "object",
                "properties": {},
                "required": [],
            },
            source="integration:youtrack",
            priority=1,
        ),
        # ── 1. Projects ───────────────────────────────────────────────────
        ToolDefinition(
            name="list_youtrack_projects",
            description=(
                "List YouTrack projects accessible to the current user. "
                "Optionally filter by name. Returns project short names (used as project IDs in queries). "
                "Use skip for pagination."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Filter by project name (optional)",
                        "default": "",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max projects to return (max 100)",
                        "default": 50,
                    },
                    "skip": {
                        "type": "integer",
                        "description": "Offset for pagination",
                        "default": 0,
                    },
                },
                "required": [],
            },
            source="integration:youtrack",
            priority=2,
        ),
        # ── 2. Issues ─────────────────────────────────────────────────────
        ToolDefinition(
            name="search_youtrack_issues",
            description=(
                "Search YouTrack issues using the YouTrack query language. "
                "Examples: 'project: PROJ state: Open', 'assignee: me priority: Critical', "
                "'tag: bug created: today'. Use skip for pagination."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "YouTrack query string (e.g. 'project: PROJ state: Open')",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max results (max 100)",
                        "default": 25,
                    },
                    "skip": {
                        "type": "integer",
                        "description": "Offset for pagination",
                        "default": 0,
                    },
                },
                "required": ["query"],
            },
            source="integration:youtrack",
            priority=0,
        ),
        ToolDefinition(
            name="get_youtrack_issue",
            description=(
                "Get full details of a YouTrack issue including description, custom fields, "
                "attachments (id, size, download URL) and comments. "
                "Use the readable ID (e.g. 'PROJ-123'). Call search_youtrack_issues first to find issue IDs."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "issue_id": {
                        "type": "string",
                        "description": "Issue ID (e.g. 'PROJ-123')",
                    },
                },
                "required": ["issue_id"],
            },
            source="integration:youtrack",
            priority=0,
        ),
        ToolDefinition(
            name="create_youtrack_issue",
            description=(
                "Create a new YouTrack issue. Provide the project short name, summary, "
                "and optionally description, assignee login, priority, and type."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "project_id": {
                        "type": "string",
                        "description": "Project short name (e.g. 'PROJ') or internal ID",
                    },
                    "summary": {
                        "type": "string",
                        "description": "Issue title/summary",
                    },
                    "description": {
                        "type": "string",
                        "description": "Detailed description (Markdown supported)",
                        "default": "",
                    },
                    "assignee_login": {
                        "type": "string",
                        "description": "YouTrack login of the assignee (optional)",
                        "default": "",
                    },
                    "priority": {
                        "type": "string",
                        "description": "Priority name (e.g. 'Critical', 'Major', 'Normal', 'Minor')",
                        "default": "",
                    },
                    "issue_type": {
                        "type": "string",
                        "description": "Issue type name (e.g. 'Bug', 'Feature', 'Task')",
                        "default": "",
                    },
                },
                "required": ["project_id", "summary"],
            },
            source="integration:youtrack",
            priority=0,
        ),
        ToolDefinition(
            name="update_youtrack_issue",
            description=(
                "Update fields on an existing YouTrack issue. Only provided fields are changed. "
                "Use state to transition the issue (e.g. 'In Progress', 'Fixed', 'Won't fix')."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "issue_id": {
                        "type": "string",
                        "description": "Issue ID (e.g. 'PROJ-123')",
                    },
                    "summary": {
                        "type": "string",
                        "description": "New issue summary (optional)",
                        "default": "",
                    },
                    "description": {
                        "type": "string",
                        "description": "New description (optional)",
                        "default": "",
                    },
                    "state": {
                        "type": "string",
                        "description": "New state name (e.g. 'Open', 'In Progress', 'Fixed')",
                        "default": "",
                    },
                    "assignee_login": {
                        "type": "string",
                        "description": "New assignee login (optional)",
                        "default": "",
                    },
                    "priority": {
                        "type": "string",
                        "description": "New priority name (optional)",
                        "default": "",
                    },
                },
                "required": ["issue_id"],
            },
            source="integration:youtrack",
            priority=1,
        ),
        ToolDefinition(
            name="delete_youtrack_issue",
            description="Delete a YouTrack issue permanently. This action cannot be undone.",
            input_schema={
                "type": "object",
                "properties": {
                    "issue_id": {
                        "type": "string",
                        "description": "Issue ID to delete (e.g. 'PROJ-123')",
                    },
                },
                "required": ["issue_id"],
            },
            source="integration:youtrack",
            priority=2,
        ),
        ToolDefinition(
            name="add_youtrack_comment",
            description=(
                "Add a comment to a YouTrack issue. Markdown formatting is supported in the text."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "issue_id": {
                        "type": "string",
                        "description": "Issue ID (e.g. 'PROJ-123')",
                    },
                    "text": {
                        "type": "string",
                        "description": "Comment text (Markdown supported)",
                    },
                },
                "required": ["issue_id", "text"],
            },
            source="integration:youtrack",
            priority=0,
        ),
        # ── 3. Work Items ─────────────────────────────────────────────────
        ToolDefinition(
            name="list_youtrack_work_items",
            description=(
                "List time tracking work items for a YouTrack issue. "
                "Returns work items with duration, author, date, and notes. "
                "Use skip for pagination."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "issue_id": {
                        "type": "string",
                        "description": "Issue ID (e.g. 'PROJ-123')",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max work items to return (max 100)",
                        "default": 25,
                    },
                    "skip": {
                        "type": "integer",
                        "description": "Offset for pagination",
                        "default": 0,
                    },
                },
                "required": ["issue_id"],
            },
            source="integration:youtrack",
            priority=1,
        ),
        ToolDefinition(
            name="add_youtrack_work_item",
            description=(
                "Add a time tracking work item to a YouTrack issue. "
                "Specify the duration in minutes (e.g. 90 = 1h 30m), an optional date (YYYY-MM-DD), "
                "and an optional description note."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "issue_id": {
                        "type": "string",
                        "description": "Issue ID (e.g. 'PROJ-123')",
                    },
                    "duration_minutes": {
                        "type": "integer",
                        "description": "Time spent in minutes (e.g. 90 = 1h 30m)",
                    },
                    "date": {
                        "type": "string",
                        "description": "Date in YYYY-MM-DD format (defaults to today)",
                        "default": "",
                    },
                    "text": {
                        "type": "string",
                        "description": "Optional work description or note",
                        "default": "",
                    },
                },
                "required": ["issue_id", "duration_minutes"],
            },
            source="integration:youtrack",
            priority=1,
        ),
        ToolDefinition(
            name="search_youtrack_work_items",
            description=(
                "Search work items (time tracking entries) across all YouTrack issues. "
                "Prefer the dedicated parameters (author, start_date, end_date, project) over "
                "the raw query string — they are composed automatically into a correct query. "
                "Use author to find all time logged by a specific user. "
                "Use start_date and end_date (YYYY-MM-DD) to filter by time period. "
                "Use project to narrow to a single project. "
                "Use query only for advanced filters not covered by the other params. "
                "Use skip for pagination."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "author": {
                        "type": "string",
                        "description": "YouTrack login of the work item author (e.g. 'john.doe' or 'me' for the current user)",
                        "default": "",
                    },
                    "start_date": {
                        "type": "string",
                        "description": "Inclusive start date in YYYY-MM-DD format (e.g. '2025-01-01')",
                        "default": "",
                    },
                    "end_date": {
                        "type": "string",
                        "description": "Inclusive end date in YYYY-MM-DD format (e.g. '2025-01-31')",
                        "default": "",
                    },
                    "project": {
                        "type": "string",
                        "description": "Project short name to narrow results (e.g. 'PROJ')",
                        "default": "",
                    },
                    "query": {
                        "type": "string",
                        "description": "Additional raw YouTrack query clauses appended after structured params (optional)",
                        "default": "",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max results to return (max 100)",
                        "default": 25,
                    },
                    "skip": {
                        "type": "integer",
                        "description": "Offset for pagination",
                        "default": 0,
                    },
                },
                "required": [],
            },
            source="integration:youtrack",
            priority=1,
        ),
        # ── 4. Custom Fields ──────────────────────────────────────────────
        ToolDefinition(
            name="list_youtrack_custom_fields",
            description=(
                "List custom field definitions for a YouTrack project. "
                "Returns field names, types, and allowed values. "
                "Use this to discover what fields and values are available before creating or updating issues. "
                "Use skip for pagination."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "project_id": {
                        "type": "string",
                        "description": "Project short name (e.g. 'PROJ') or internal ID",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max custom fields to return (max 200)",
                        "default": 50,
                    },
                    "skip": {
                        "type": "integer",
                        "description": "Offset for pagination",
                        "default": 0,
                    },
                },
                "required": ["project_id"],
            },
            source="integration:youtrack",
            priority=2,
        ),
        ToolDefinition(
            name="get_youtrack_issue_custom_fields",
            description=(
                "Get the current custom field values set on a YouTrack issue. "
                "Returns each field name with its current value (state, priority, assignee, etc.). "
                "Use skip for pagination."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "issue_id": {
                        "type": "string",
                        "description": "Issue ID (e.g. 'PROJ-123')",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max custom fields to return (max 200)",
                        "default": 50,
                    },
                    "skip": {
                        "type": "integer",
                        "description": "Offset for pagination",
                        "default": 0,
                    },
                },
                "required": ["issue_id"],
            },
            source="integration:youtrack",
            priority=1,
        ),
        # ── 5. Tags ───────────────────────────────────────────────────────
        ToolDefinition(
            name="list_youtrack_tags",
            description=(
                "List issue tags available in the YouTrack instance. "
                "Optionally filter by tag name. "
                "Returns tag names that can be used in issue search queries with 'tag: NAME'. "
                "Use this to discover available tags before building search queries like "
                "'tag: bug project: PROJ' or 'tag: {needs review} state: Open'. "
                "Use skip for pagination."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Filter by tag name (optional)",
                        "default": "",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max tags to return (max 200)",
                        "default": 50,
                    },
                    "skip": {
                        "type": "integer",
                        "description": "Offset for pagination",
                        "default": 0,
                    },
                },
                "required": [],
            },
            source="integration:youtrack",
            priority=2,
        ),
    ]
