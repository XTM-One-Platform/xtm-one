# SPDX-License-Identifier: Apache-2.0

"""Mattermost integration - the self-hosted chat SOC teams are notified in.

Mattermost through the REST API v4:

  - Messages: post in a channel or a thread, post an ephemeral message one
    user alone sees, read a channel's recent messages, delete a post.
  - Reactions: list, add, remove.
  - Channels: list teams, search channels, read one with its statistics and
    members, create, archive, restore, add a member.
  - Users: list (a team's or a channel's), read by id or email, deactivate,
    invite to a team by email.

Authentication is a bot account or personal access token sent as
``Authorization: Bearer <token>``. The instance is the integration's
``base_url``, required (Mattermost has no shared cloud host) and checked by
:func:`~app.integrations.http.operator_base_url`. Every Mattermost id (26
lower-case letters and digits) is matched before it goes in a path.

Token format: ``base_url||token``.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote

import httpx

from app.core.egress import egress_client
from app.integrations.http import (
    TRANSIENT_RETRY_STATUSES,
    bearer_auth_headers,
    clip_text,
    operator_base_url,
    parse_compound_token,
    safe_request,
)
from app.tools.base import CallableToolExecutor, ToolDefinition

_PROVIDER = "Mattermost"
_SOURCE = "integration:mattermost"

_TIMEOUT = 30
_DEFAULT_LIMIT = 30
_MAX_LIMIT = 200
#: Mattermost's own cap on a post's text.
_MAX_MESSAGE_CHARS = 16_383

_ID_RE = re.compile(r"[a-z0-9]{26}")
_CHANNEL_NAME_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
_EMAIL_RE = re.compile(r"[^@\s/]{1,64}@[^@\s/]{1,255}\.[^@\s/]{2,63}")
_EMOJI_RE = re.compile(r"[a-z0-9_+-]{1,64}")


def mattermost_api(base_url: str) -> str | None:
    """``<instance>/api/v4``, ``None`` when *base_url* is blank or not usable."""
    instance = operator_base_url(base_url, strip_suffix="/api/v4") if (base_url or "").strip() else None
    return f"{instance}/api/v4" if instance else None


def build_mattermost_headers(token: str) -> dict[str, str]:
    """The one header builder, shared with the connectivity test."""
    return bearer_auth_headers(token, extra={"Accept": "application/json"})


def _clean(value: Any, limit: int = 200) -> str:
    text = " ".join(str(value if value is not None else "").split())
    return clip_text(text, limit) if text else "-"


def _json(resp: httpx.Response) -> Any:
    try:
        return resp.json()
    except ValueError:
        return None


def _limit(value: Any, default: int = _DEFAULT_LIMIT) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return max(1, min(n, _MAX_LIMIT))


def _page(value: Any) -> int:
    try:
        return max(0, min(int(value or 0), 10_000))
    except (TypeError, ValueError):
        return 0


def _is_id(value: Any) -> bool:
    return bool(_ID_RE.fullmatch(str(value or "").strip()))


def _user_line(u: dict[str, Any]) -> str:
    name = " ".join(x for x in (u.get("first_name"), u.get("last_name")) if x)
    return (
        f"- @{_clean(u.get('username'), 64)} (id: {_clean(u.get('id'), 26)})"
        + (f" {_clean(name, 120)}" if name else "")
        + (f", {_clean(u.get('email'), 120)}" if u.get("email") else "")
        + (", deactivated" if u.get("delete_at") else "")
        + (", bot" if u.get("is_bot") else "")
    )


def _channel_line(c: dict[str, Any]) -> str:
    kind = {"O": "public", "P": "private", "D": "direct", "G": "group"}.get(c.get("type"), "?")
    return (
        f"- ~{_clean(c.get('name'), 64)} \"{_clean(c.get('display_name'), 120)}\" (id: {_clean(c.get('id'), 26)}, {kind})"
        + (", archived" if c.get("delete_at") else "")
    )


def build_tools(
    token: str, executor: CallableToolExecutor, **_kwargs: Any
) -> list[ToolDefinition]:
    base_url, access_token = parse_compound_token(token or "", 2, greedy_last=True)
    api = mattermost_api(base_url)
    if api is None or not access_token.strip():
        return []
    headers = build_mattermost_headers(access_token)

    async def _get(path: str, action: str, params: Any = None):
        async with egress_client(timeout=_TIMEOUT) as client:
            return await safe_request(
                client, "get", f"{api}{path}", _PROVIDER, action,
                headers=headers, params=params, retry_statuses=TRANSIENT_RETRY_STATUSES,
            )

    async def _write(method: str, path: str, action: str, body: Any = None):
        async with egress_client(timeout=_TIMEOUT) as client:
            return await safe_request(client, method, f"{api}{path}", _PROVIDER, action, headers=headers, json=body)

    def _bad_id(name: str) -> str:
        return f"Error: {name} must be a Mattermost id (26 lower-case letters and digits)."

    def _message(text: Any) -> tuple[str, str]:
        body = str(text or "").strip()
        if len(body) > _MAX_MESSAGE_CHARS:
            return body[:_MAX_MESSAGE_CHARS], f" (cut to {_MAX_MESSAGE_CHARS} characters)"
        return body, ""

    # -- Messages ---------------------------------------------------------

    async def post_mattermost_message(channel_id: str, message: str, root_id: str = "") -> str:
        if not _is_id(channel_id):
            return _bad_id("channel_id")
        text, cut = _message(message)
        if not text:
            return "Error: message is required."
        post: dict[str, Any] = {"channel_id": channel_id.strip(), "message": text}
        if root_id:
            if not _is_id(root_id):
                return _bad_id("root_id")
            post["root_id"] = root_id.strip()
        resp, err = await _write("post", "/posts", "post a message", post)
        if err:
            return err
        return f"Posted message {_clean((_json(resp) or {}).get('id'), 26)}{cut}."

    async def post_mattermost_ephemeral_message(channel_id: str, user_id: str, message: str) -> str:
        if not _is_id(channel_id):
            return _bad_id("channel_id")
        if not _is_id(user_id):
            return _bad_id("user_id")
        text, cut = _message(message)
        if not text:
            return "Error: message is required."
        body = {"user_id": user_id.strip(), "post": {"channel_id": channel_id.strip(), "message": text}}
        _resp, err = await _write("post", "/posts/ephemeral", "post an ephemeral message", body)
        return err or f"Posted an ephemeral message only that user sees{cut}."

    async def list_mattermost_channel_posts(channel_id: str, limit: int = _DEFAULT_LIMIT, page: int = 0) -> str:
        if not _is_id(channel_id):
            return _bad_id("channel_id")
        n, p = _limit(limit), _page(page)
        resp, err = await _get(f"/channels/{channel_id.strip()}/posts", "list channel messages", {"per_page": n, "page": p})
        if err:
            return err
        data = _json(resp) or {}
        posts = data.get("posts") or {}
        order = [pid for pid in data.get("order") or [] if isinstance(posts.get(pid), dict)]
        if not order:
            return "No messages in that channel."
        lines = []
        for pid in order:
            post = posts[pid]
            thread = f" (reply to {_clean(post.get('root_id'), 26)})" if post.get("root_id") else ""
            lines.append(
                f"- {post.get('create_at', '?')} user {_clean(post.get('user_id'), 26)} post {_clean(pid, 26)}{thread}: "
                f"{_clean(post.get('message'), 2000)}"
            )
        hint = f"\n[Older messages: pass page={p + 1}]" if len(order) == n else ""
        return f"Messages, newest first ({len(order)}; create_at is in ms since epoch):\n" + "\n".join(lines) + hint

    async def delete_mattermost_post(post_id: str) -> str:
        if not _is_id(post_id):
            return _bad_id("post_id")
        _resp, err = await _write("delete", f"/posts/{post_id.strip()}", "delete a post")
        return err or f"Deleted post {post_id.strip()}."

    # -- Reactions --------------------------------------------------------

    async def list_mattermost_reactions(post_id: str) -> str:
        if not _is_id(post_id):
            return _bad_id("post_id")
        resp, err = await _get(f"/posts/{post_id.strip()}/reactions", "list reactions")
        if err:
            return err
        reactions = [r for r in _json(resp) or [] if isinstance(r, dict)]
        if not reactions:
            return "No reactions on that post."
        lines = [f"- :{_clean(r.get('emoji_name'), 64)}: by user {_clean(r.get('user_id'), 26)}" for r in reactions]
        return f"Reactions ({len(reactions)}):\n" + "\n".join(lines)

    async def _me() -> tuple[str | None, str | None]:
        resp, err = await _get("/users/me", "read the token's user")
        if err:
            return None, err
        uid = (_json(resp) or {}).get("id")
        return (uid, None) if _is_id(uid) else (None, "Error: Mattermost did not name the token's user.")

    async def add_mattermost_reaction(post_id: str, emoji_name: str) -> str:
        if not _is_id(post_id):
            return _bad_id("post_id")
        emoji = str(emoji_name or "").strip().strip(":").lower()
        if not _EMOJI_RE.fullmatch(emoji):
            return "Error: emoji_name must be an emoji name such as white_check_mark."
        uid, err = await _me()
        if err:
            return err
        body = {"user_id": uid, "post_id": post_id.strip(), "emoji_name": emoji}
        _resp, err = await _write("post", "/reactions", "add a reaction", body)
        return err or f"Reacted :{emoji}: to post {post_id.strip()}."

    async def remove_mattermost_reaction(post_id: str, emoji_name: str) -> str:
        if not _is_id(post_id):
            return _bad_id("post_id")
        emoji = str(emoji_name or "").strip().strip(":").lower()
        if not _EMOJI_RE.fullmatch(emoji):
            return "Error: emoji_name must be an emoji name such as white_check_mark."
        uid, err = await _me()
        if err:
            return err
        _resp, err = await _write("delete", f"/users/{uid}/posts/{post_id.strip()}/reactions/{emoji}", "remove a reaction")
        return err or f"Removed :{emoji}: from post {post_id.strip()}."

    # -- Teams and channels -------------------------------------------------

    async def list_mattermost_teams() -> str:
        resp, err = await _get("/users/me/teams", "list teams")
        if err:
            return err
        teams = [t for t in _json(resp) or [] if isinstance(t, dict)]
        if not teams:
            return "The token's user belongs to no team."
        lines = [
            f"- {_clean(t.get('display_name'), 120)} ({_clean(t.get('name'), 64)}, id: {_clean(t.get('id'), 26)})"
            for t in teams
        ]
        return f"Teams ({len(teams)}):\n" + "\n".join(lines)

    async def search_mattermost_channels(team_id: str, term: str = "") -> str:
        if not _is_id(team_id):
            return _bad_id("team_id")
        resp, err = await _write(
            "post", f"/teams/{team_id.strip()}/channels/search", "search channels", {"term": str(term or "")[:64]}
        ) if term else await _get(f"/teams/{team_id.strip()}/channels", "list channels", {"per_page": _MAX_LIMIT})
        if err:
            return err
        channels = [c for c in _json(resp) or [] if isinstance(c, dict)]
        if not channels:
            return "No channels found."
        return f"Channels ({len(channels)}):\n" + "\n".join(_channel_line(c) for c in channels)

    async def get_mattermost_channel(channel_id: str) -> str:
        if not _is_id(channel_id):
            return _bad_id("channel_id")
        cid = channel_id.strip()
        resp, err = await _get(f"/channels/{cid}", "get a channel")
        if err:
            return err
        c = _json(resp) or {}
        out = _channel_line(c)[2:]
        if c.get("purpose"):
            out += f"\n  Purpose: {_clean(c.get('purpose'), 500)}"
        stats, err = await _get(f"/channels/{cid}/stats", "read channel statistics")
        if not err:
            s = _json(stats) or {}
            out += f"\n  Members: {s.get('member_count', '?')} | Pinned posts: {s.get('pinnedpost_count', '?')}"
        return out

    async def list_mattermost_channel_members(channel_id: str, limit: int = 60, page: int = 0) -> str:
        if not _is_id(channel_id):
            return _bad_id("channel_id")
        params = {"in_channel": channel_id.strip(), "per_page": _limit(limit, 60), "page": _page(page)}
        resp, err = await _get("/users", "list channel members", params)
        if err:
            return err
        users = [u for u in _json(resp) or [] if isinstance(u, dict)]
        if not users:
            return "No members found."
        return f"Members ({len(users)}):\n" + "\n".join(_user_line(u) for u in users)

    async def create_mattermost_channel(
        team_id: str, name: str, display_name: str, private: bool = False, purpose: str = ""
    ) -> str:
        if not _is_id(team_id):
            return _bad_id("team_id")
        handle = str(name or "").strip().lower()
        if not _CHANNEL_NAME_RE.fullmatch(handle):
            return "Error: name must be the channel handle: lower-case letters, digits, '-' and '_' (e.g. vuln-alerts)."
        title = " ".join(str(display_name or "").split())
        if not title:
            return "Error: display_name is required."
        body = {
            "team_id": team_id.strip(),
            "name": handle,
            "display_name": title[:64],
            "type": "P" if private else "O",
        }
        if purpose:
            body["purpose"] = str(purpose)[:250]
        resp, err = await _write("post", "/channels", f"create channel {handle}", body)
        if err:
            return err
        return "Created channel " + _channel_line(_json(resp) or {})[2:]

    async def archive_mattermost_channel(channel_id: str) -> str:
        if not _is_id(channel_id):
            return _bad_id("channel_id")
        _resp, err = await _write("delete", f"/channels/{channel_id.strip()}", "archive a channel")
        return err or f"Archived channel {channel_id.strip()}."

    async def restore_mattermost_channel(channel_id: str) -> str:
        if not _is_id(channel_id):
            return _bad_id("channel_id")
        _resp, err = await _write("post", f"/channels/{channel_id.strip()}/restore", "restore a channel")
        return err or f"Restored channel {channel_id.strip()}."

    async def add_mattermost_channel_member(channel_id: str, user_id: str) -> str:
        if not _is_id(channel_id):
            return _bad_id("channel_id")
        if not _is_id(user_id):
            return _bad_id("user_id")
        _resp, err = await _write(
            "post", f"/channels/{channel_id.strip()}/members", "add a channel member", {"user_id": user_id.strip()}
        )
        return err or f"Added user {user_id.strip()} to channel {channel_id.strip()}."

    # -- Users ------------------------------------------------------------

    async def list_mattermost_users(team_id: str = "", limit: int = 60, page: int = 0) -> str:
        params: dict[str, Any] = {"per_page": _limit(limit, 60), "page": _page(page)}
        if team_id:
            if not _is_id(team_id):
                return _bad_id("team_id")
            params["in_team"] = team_id.strip()
        resp, err = await _get("/users", "list users", params)
        if err:
            return err
        users = [u for u in _json(resp) or [] if isinstance(u, dict)]
        if not users:
            return "No users found."
        return f"Users ({len(users)}):\n" + "\n".join(_user_line(u) for u in users)

    async def get_mattermost_user(user_id: str = "", email: str = "") -> str:
        if user_id:
            if not _is_id(user_id):
                return _bad_id("user_id")
            path = f"/users/{user_id.strip()}"
        elif email:
            address = str(email).strip()
            if not _EMAIL_RE.fullmatch(address):
                return "Error: email must be an email address."
            path = f"/users/email/{quote(address, safe='@')}"
        else:
            return "Error: give user_id or email."
        resp, err = await _get(path, "get a user")
        if err:
            return err
        return _user_line(_json(resp) or {})[2:]

    async def deactivate_mattermost_user(user_id: str) -> str:
        if not _is_id(user_id):
            return _bad_id("user_id")
        _resp, err = await _write("delete", f"/users/{user_id.strip()}", "deactivate a user")
        return err or f"Deactivated user {user_id.strip()}."

    async def invite_mattermost_users(team_id: str, emails: list[str]) -> str:
        if not _is_id(team_id):
            return _bad_id("team_id")
        addresses = [str(e).strip() for e in emails or [] if str(e).strip()]
        if not addresses or not all(_EMAIL_RE.fullmatch(a) for a in addresses):
            return "Error: emails must be a list of email addresses."
        _resp, err = await _write(
            "post", f"/teams/{team_id.strip()}/invite/email", "invite users", addresses[:20]
        )
        return err or f"Invited {len(addresses[:20])} address(es) to the team."

    handlers = {
        "post_mattermost_message": post_mattermost_message,
        "post_mattermost_ephemeral_message": post_mattermost_ephemeral_message,
        "list_mattermost_channel_posts": list_mattermost_channel_posts,
        "delete_mattermost_post": delete_mattermost_post,
        "list_mattermost_reactions": list_mattermost_reactions,
        "add_mattermost_reaction": add_mattermost_reaction,
        "remove_mattermost_reaction": remove_mattermost_reaction,
        "list_mattermost_teams": list_mattermost_teams,
        "search_mattermost_channels": search_mattermost_channels,
        "get_mattermost_channel": get_mattermost_channel,
        "list_mattermost_channel_members": list_mattermost_channel_members,
        "create_mattermost_channel": create_mattermost_channel,
        "archive_mattermost_channel": archive_mattermost_channel,
        "restore_mattermost_channel": restore_mattermost_channel,
        "add_mattermost_channel_member": add_mattermost_channel_member,
        "list_mattermost_users": list_mattermost_users,
        "get_mattermost_user": get_mattermost_user,
        "deactivate_mattermost_user": deactivate_mattermost_user,
        "invite_mattermost_users": invite_mattermost_users,
    }
    for tool_name, fn in handlers.items():
        executor.register(tool_name, fn)

    def _s(description: str) -> dict:
        return {"type": "string", "description": description}

    def _i(description: str) -> dict:
        return {"type": "integer", "description": description}

    channel_p = _s("Channel id (26 characters), from search_mattermost_channels")
    team_p = _s("Team id, from list_mattermost_teams")
    user_p = _s("User id (26 characters)")
    post_p = _s("Post id (26 characters)")
    emoji_p = _s("Emoji name without colons (e.g. white_check_mark, eyes)")
    page_p = _i("Page, from the previous answer's hint (first page: 0)")

    def _tool(name: str, description: str, properties: dict, required: list[str]) -> ToolDefinition:
        return ToolDefinition(
            name=name,
            description=description,
            input_schema={"type": "object", "properties": properties, "required": required},
            source=_SOURCE,
        )

    return [
        _tool(
            "post_mattermost_message",
            "Post a message (Markdown) in a Mattermost channel, or in a thread with root_id: e.g. "
            "notify a SOC channel of a new critical exposure.",
            {"channel_id": channel_p, "message": _s("Message text (Markdown)"), "root_id": _s("Post id of the thread to reply in")},
            ["channel_id", "message"],
        ),
        _tool(
            "post_mattermost_ephemeral_message",
            "Post a message in a Mattermost channel that only one user sees, until they reload.",
            {"channel_id": channel_p, "user_id": user_p, "message": _s("Message text (Markdown)")},
            ["channel_id", "user_id", "message"],
        ),
        _tool(
            "list_mattermost_channel_posts",
            "Read a Mattermost channel's recent messages, newest first, with their post ids and threads.",
            {"channel_id": channel_p, "limit": _i(f"Messages per page (default {_DEFAULT_LIMIT}, max {_MAX_LIMIT})"), "page": page_p},
            ["channel_id"],
        ),
        _tool("delete_mattermost_post", "Delete a Mattermost post.", {"post_id": post_p}, ["post_id"]),
        _tool("list_mattermost_reactions", "List the emoji reactions on a Mattermost post.", {"post_id": post_p}, ["post_id"]),
        _tool("add_mattermost_reaction", "React to a Mattermost post with an emoji, as the token's user.",
              {"post_id": post_p, "emoji_name": emoji_p}, ["post_id", "emoji_name"]),
        _tool("remove_mattermost_reaction", "Remove the token's user's emoji reaction from a Mattermost post.",
              {"post_id": post_p, "emoji_name": emoji_p}, ["post_id", "emoji_name"]),
        _tool("list_mattermost_teams", "List the Mattermost teams the token's user belongs to.", {}, []),
        _tool(
            "search_mattermost_channels",
            "Find a team's public Mattermost channels by name, or list them.",
            {"team_id": team_p, "term": _s("Part of the channel name")},
            ["team_id"],
        ),
        _tool("get_mattermost_channel", "Read a Mattermost channel: name, type, purpose, member and pinned post counts.",
              {"channel_id": channel_p}, ["channel_id"]),
        _tool("list_mattermost_channel_members", "List the members of a Mattermost channel.",
              {"channel_id": channel_p, "limit": _i("Members per page (default 60)"), "page": page_p}, ["channel_id"]),
        _tool(
            "create_mattermost_channel",
            "Create a Mattermost channel in a team, public or private.",
            {
                "team_id": team_p,
                "name": _s("Handle: lower-case letters, digits, '-', '_' (e.g. vuln-alerts)"),
                "display_name": _s("Display name"),
                "private": {"type": "boolean", "description": "Private channel (default public)"},
                "purpose": _s("Purpose"),
            },
            ["team_id", "name", "display_name"],
        ),
        _tool("archive_mattermost_channel", "Archive a Mattermost channel (restorable).", {"channel_id": channel_p}, ["channel_id"]),
        _tool("restore_mattermost_channel", "Restore an archived Mattermost channel.", {"channel_id": channel_p}, ["channel_id"]),
        _tool("add_mattermost_channel_member", "Add a user to a Mattermost channel.",
              {"channel_id": channel_p, "user_id": user_p}, ["channel_id", "user_id"]),
        _tool("list_mattermost_users", "List Mattermost users, optionally of one team.",
              {"team_id": team_p, "limit": _i("Users per page (default 60)"), "page": page_p}, []),
        _tool("get_mattermost_user", "Read a Mattermost user by id or by email.",
              {"user_id": user_p, "email": _s("Email address")}, []),
        _tool("deactivate_mattermost_user", "Deactivate a Mattermost user account.", {"user_id": user_p}, ["user_id"]),
        _tool("invite_mattermost_users", "Invite people to a Mattermost team by email (at most 20 per call).",
              {"team_id": team_p, "emails": {"type": "array", "items": {"type": "string"}, "description": "Email addresses"}},
              ["team_id", "emails"]),
    ]
