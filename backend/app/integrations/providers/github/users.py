"""GitHub users & notifications tools - whoami, user profile, notifications."""

from __future__ import annotations

from typing import Any
from urllib.parse import quote as urlquote

from app.core.egress import egress_client
from app.tools.base import CallableToolExecutor, ToolDefinition

from . import _common
from ._common import _BASE_URL, _PROVIDER, _json_headers, login_error

_SOURCE = "integration:github"


def register(
    headers: dict[str, str],
    executor: CallableToolExecutor,
    mark_self_action: Any,
) -> list[ToolDefinition]:
    """Build the GitHub user / notification tools and register their callables."""

    async def get_github_authenticated_user() -> str:
        """Get the authenticated account (whoami): login, name, plan, and counts. Useful to confirm who the integration acts as."""
        async with egress_client(timeout=30) as client:
            resp, err = await _common.safe_request(
                client, "get", f"{_BASE_URL}/user", _PROVIDER, "get authenticated user", headers=headers
            )
            if err:
                return err
            u = resp.json()
            lines = [
                f"Authenticated as: {u.get('login')} ({u.get('name') or 'no display name'}) [{u.get('type', 'User')}]",
                f"URL: {u.get('html_url', '')}",
                f"Public repos: {u.get('public_repos', 0)} | Private repos: {u.get('total_private_repos', 'N/A')}",
                f"Followers: {u.get('followers', 0)} | Following: {u.get('following', 0)}",
            ]
            if u.get("company"):
                lines.append(f"Company: {u['company']}")
            if u.get("plan"):
                lines.append(f"Plan: {u['plan'].get('name')}")
            return "\n".join(lines)

    async def get_github_user(username: str) -> str:
        """Get a public GitHub user or organization profile by login."""
        if e := login_error(username, "username"):
            return e
        async with egress_client(timeout=30) as client:
            resp, err = await _common.safe_request(
                client, "get", f"{_BASE_URL}/users/{username}", _PROVIDER, f"get user {username}", headers=headers
            )
            if err:
                return err
            u = resp.json()
            lines = [
                f"{u.get('login')} ({u.get('name') or 'no display name'}) [{u.get('type', 'User')}]",
                f"URL: {u.get('html_url', '')}",
                f"Bio: {u.get('bio') or 'None'}",
                f"Public repos: {u.get('public_repos', 0)} | Followers: {u.get('followers', 0)} | Following: {u.get('following', 0)}",
            ]
            if u.get("company"):
                lines.append(f"Company: {u['company']}")
            if u.get("location"):
                lines.append(f"Location: {u['location']}")
            return "\n".join(lines)

    async def list_github_notifications(all_notifications: bool = False, participating: bool = False, count: int = 30) -> str:
        """List the authenticated user's notifications (unread by default). Set all_notifications to include read ones."""
        params: dict[str, Any] = {
            "all": "true" if all_notifications else "false",
            "participating": "true" if participating else "false",
            "per_page": max(1, min(count, 50)),
        }
        async with egress_client(timeout=30) as client:
            resp, err = await _common.safe_request(
                client, "get", f"{_BASE_URL}/notifications", _PROVIDER, "list notifications", headers=headers, params=params
            )
            if err:
                return err
            notifs = resp.json()
            if not notifs:
                return "No notifications." if all_notifications else "No unread notifications."
            lines = [f"Notifications ({len(notifs)}):"]
            for n in notifs:
                subject = n.get("subject") or {}
                repo_name = (n.get("repository") or {}).get("full_name", "")
                unread = "* " if n.get("unread") else "  "
                lines.append(
                    f"{unread}[{n.get('id')}] {subject.get('type', '?')}: {subject.get('title', '')} "
                    f"({repo_name}) — reason: {n.get('reason', '?')}"
                )
            lines.append("\nUse manage_github_notification to mark a thread (or all) as read.")
            return "\n".join(lines)

    async def manage_github_notification(action: str, thread_id: str = "") -> str:
        """Mark notifications as read (write).

        ``action``: ``mark_read`` (one thread, needs ``thread_id``) or
        ``mark_all_read`` (every notification).
        """
        action = (action or "").strip().lower()
        async with egress_client(timeout=30) as client:
            if action == "mark_read":
                if not thread_id.strip():
                    return "Error: 'thread_id' is required for mark_read (the id from list_github_notifications)."
                # thread_id is an opaque single path segment - encode it with
                # safe="" (not _enc, which preserves "/") so a crafted id
                # containing "/" cannot change the request path (path-injection).
                _resp, err = await _common.safe_request(
                    client, "patch", f"{_BASE_URL}/notifications/threads/{urlquote(thread_id, safe='')}", _PROVIDER, f"mark thread {thread_id} read", headers=headers
                )
                if err:
                    return err
                return f"Notification thread {thread_id} marked as read."
            if action == "mark_all_read":
                _resp, err = await _common.safe_request(
                    client, "put", f"{_BASE_URL}/notifications", _PROVIDER, "mark all notifications read", headers=_json_headers(headers), json={}
                )
                if err:
                    return err
                return "All notifications marked as read."
            return "Error: invalid action. Use mark_read or mark_all_read."

    executor.register("get_github_authenticated_user", get_github_authenticated_user)
    executor.register("get_github_user", get_github_user)
    executor.register("list_github_notifications", list_github_notifications)
    executor.register("manage_github_notification", manage_github_notification)

    return [
        ToolDefinition(
            name="get_github_authenticated_user",
            description="Get the authenticated account (whoami): login, name, type, plan, and repo/follower counts. Use to confirm which account the integration acts as.",
            input_schema={"type": "object", "properties": {}, "required": []},
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="get_github_user",
            description="Get a public GitHub user or organization profile by login (name, bio, company, location, repo/follower counts).",
            input_schema={
                "type": "object",
                "properties": {"username": {"type": "string", "description": "GitHub login"}},
                "required": ["username"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="list_github_notifications",
            description="List the authenticated user's notifications (unread by default). Shows the subject type/title, repository, and reason. Set all_notifications to include already-read ones.",
            input_schema={
                "type": "object",
                "properties": {
                    "all_notifications": {"type": "boolean", "description": "Include read notifications", "default": False},
                    "participating": {"type": "boolean", "description": "Only notifications where the user is a participant", "default": False},
                    "count": {"type": "integer", "description": "Max notifications (max 50)", "default": 30},
                },
                "required": [],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="manage_github_notification",
            description="Mark notifications as read. action=mark_read (one thread, needs thread_id from list_github_notifications) or mark_all_read (all notifications).",
            input_schema={
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["mark_read", "mark_all_read"]},
                    "thread_id": {"type": "string", "description": "Notification thread id (mark_read)", "default": ""},
                },
                "required": ["action"],
            },
            source=_SOURCE,
            priority=1,
        ),
    ]
