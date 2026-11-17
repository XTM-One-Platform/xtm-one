"""Notion user tools: list + get."""

from __future__ import annotations

from typing import Any

from app.tools.base import ToolDefinition

from ._common import (
    _BASE_URL,
    _PROVIDER,
    NotionContext,
    format_entity_list,
    user_capability_error,
)

# Pages of 100 users a ``query`` scan reads at most (5,000 users).
_QUERY_MAX_PAGES = 50


def _user_matches(user: dict[str, Any], needle: str) -> bool:
    """Whether *needle* (lower-case) is in the user's name or email."""
    name = str(user.get("name") or "").lower()
    email = str((user.get("person") or {}).get("email") or "").lower()
    return needle in name or needle in email


def _fmt_user(u: dict[str, Any]) -> str:
    name = u.get("name", "Unknown")
    utype = u.get("type", "")
    email = ""
    if utype == "person":
        email = u.get("person", {}).get("email", "")
    uid = u.get("id", "")
    detail = f" ({email})" if email else ""
    return f"• {name}{detail} [{utype}] — ID: {uid}"


def register(ctx: NotionContext, executor) -> list[ToolDefinition]:
    _client = ctx.client
    safe_request = ctx.request
    request_meta = ctx.request_meta

    async def list_notion_users(count: int = 50, after: str = "", query: str = "") -> str:
        """List the users of the Notion workspace, page by page or filtered.

        Without *query*: up to *count* users from *after* (a cursor of an
        earlier answer), each page sized to what remains so the cursor the
        answer ends with resumes exactly after the last user shown. With
        *query*: every page is scanned (at most ``_QUERY_MAX_PAGES``) for a
        name or email containing it, case-insensitive.
        """
        count = max(1, count)
        needle = query.strip().lower()
        matched: list[dict[str, Any]] = []
        next_cursor = ""
        pages = 0
        async with _client() as client:
            params: dict[str, str] = {}
            if after.strip():
                params["start_cursor"] = after.strip()
            while len(matched) < count:
                remaining = count - len(matched)
                params["page_size"] = str(100 if needle else min(remaining, 100))
                resp, err, status_code, _code = await request_meta(
                    client,
                    "get",
                    f"{_BASE_URL}/users",
                    _PROVIDER,
                    "list users",
                    params=params,
                )
                if err:
                    if status_code == 403:
                        return user_capability_error(err, action="list the workspace users")
                    return err
                pages += 1
                data = resp.json()
                results = data.get("results", [])
                if needle:
                    results = [u for u in results if _user_matches(u, needle)]
                matched.extend(results)
                next_cursor = (data.get("next_cursor") or "") if data.get("has_more") else ""
                if not next_cursor or (needle and pages >= _QUERY_MAX_PAGES):
                    break
                params["start_cursor"] = next_cursor

        if needle:
            listing = format_entity_list(
                matched[:count],
                _fmt_user,
                noun="user",
                empty_msg=f"No users match '{query.strip()}'.",
            )
            if next_cursor and len(matched) < count:
                return (
                    f"{listing}\nScan stopped after {pages} pages; more users remain: "
                    f'call list_notion_users(query="{query.strip()}", after="{next_cursor}").'
                )
            return listing
        listing = format_entity_list(matched[:count], _fmt_user, noun="user")
        if next_cursor:
            return f'{listing}\nMore users: call list_notion_users(after="{next_cursor}", count={count}).'
        return listing

    async def get_notion_me() -> str:
        """Identify the integration/bot that owns the current token (GET /v1/users/me).

        Returns the bot user backing this connection - its name, ID, and (when
        available) the workspace name and owner. Use this to confirm WHICH
        Notion integration is connected before acting.
        """
        async with _client() as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{_BASE_URL}/users/me",
                _PROVIDER,
                "retrieve token owner",
            )
            if err:
                return err
            u = resp.json()
            lines = [
                f"Name: {u.get('name', 'Unknown')}",
                f"Type: {u.get('type', '')}",
                f"ID: {u.get('id', '')}",
            ]
            bot = u.get("bot")
            if isinstance(bot, dict):
                workspace = bot.get("workspace_name")
                if workspace:
                    lines.append(f"Workspace: {workspace}")
                owner = bot.get("owner")
                if isinstance(owner, dict) and owner.get("type"):
                    lines.append(f"Owner type: {owner['type']}")
            return "\n".join(lines)

    async def get_notion_user(user_id: str) -> str:
        """Retrieve details about a specific Notion user by ID."""
        async with _client() as client:
            resp, err, status_code, _code = await request_meta(
                client,
                "get",
                f"{_BASE_URL}/users/{user_id}",
                _PROVIDER,
                "retrieve user",
            )
            if err:
                if status_code == 403:
                    return user_capability_error(err, action="read this user")
                return err
            u = resp.json()
            name = u.get("name", "Unknown")
            utype = u.get("type", "")
            email = ""
            if utype == "person":
                email = u.get("person", {}).get("email", "")
            avatar = u.get("avatar_url", "")
            lines = [
                f"Name: {name}",
                f"Type: {utype}",
                f"ID: {u.get('id', '')}",
            ]
            if email:
                lines.append(f"Email: {email}")
            if avatar:
                lines.append(f"Avatar: {avatar}")
            return "\n".join(lines)

    executor.register("list_notion_users", list_notion_users)
    executor.register("get_notion_user", get_notion_user)
    executor.register("get_notion_me", get_notion_me)

    return [
        # ── Users ──────────────────────────────────────────────────────
        ToolDefinition(
            name="get_notion_me",
            description=(
                "Identify the Notion integration/bot that owns the connected "
                "token (the 'who am I' for this connection): its name, ID, "
                "workspace, and owner. Useful to confirm which integration is "
                "connected and what it can access."
            ),
            priority=2,
            input_schema={"type": "object", "properties": {}, "required": []},
            source="integration:notion",
        ),
        ToolDefinition(
            name="list_notion_users",
            description="List all users in the Notion workspace (people and bots).",
            priority=2,
            input_schema={
                "type": "object",
                "properties": {
                    "count": {
                        "type": "integer",
                        "description": "Max users to return (default 50)",
                        "default": 50,
                    },
                    "after": {
                        "type": "string",
                        "description": "Cursor from an earlier answer: continue after it",
                    },
                    "query": {
                        "type": "string",
                        "description": "Name or email to find: every page is scanned",
                    },
                },
                "required": [],
            },
            source="integration:notion",
        ),
        ToolDefinition(
            name="get_notion_user",
            description="Retrieve details about a specific Notion user by ID.",
            priority=2,
            input_schema={
                "type": "object",
                "properties": {
                    "user_id": {"type": "string", "description": "Notion user ID"},
                },
                "required": ["user_id"],
            },
            source="integration:notion",
        ),
    ]
