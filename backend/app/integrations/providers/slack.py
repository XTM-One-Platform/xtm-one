# SPDX-License-Identifier: Apache-2.0

"""Slack integration – messaging, channels."""

from typing import Any

import httpx

from app.core.egress import egress_client
from app.core.utils import markdown_to_slack
from app.integrations.base import BaseIntegration, OAuthDefaults
from app.integrations.file_delivery import deliver_file, read_pipe_file, uploaded_file_answer
from app.integrations.http import format_entity_list, safe_request
from app.integrations.providers.slack_files import (
    MAX_SLACK_FILE_BYTES,
    download_file_bytes,
    download_url,
    file_links,
    get_file_info,
    message_files_suffix,
    split_channels,
    upload_file_bytes,
)
from app.integrations.providers.slack_scope import (
    active_channel_scope,
    allowed_text,
    apply_channel_scope,
    in_channel_scope,
)
from app.services import chat_reply_guard
from app.services.agent_posts import record_slack_file_shares, record_slack_post
from app.services.content_safety import single_line
from app.services.slack_conversations import claim_reply as claim_slack_reply
from app.tools.base import CallableToolExecutor, FileContext, ToolDefinition, ToolResult

_PROVIDER = "Slack"
_BASE_URL = "https://slack.com/api"
SLACK_API = _BASE_URL


class SlackIntegration(BaseIntegration):
    provider_name = "slack"
    display_name = "Slack"
    icon = "Hash"
    description = (
        "Connect Slack for reading and responding to messages across channels."
    )
    auth_types = ["oauth", "api_key"]

    def get_oauth_defaults(self) -> OAuthDefaults:
        return OAuthDefaults(
            authorize_url="https://slack.com/oauth/v2/authorize",
            token_url="https://slack.com/api/oauth.v2.access",  # noqa: S106
            # OAuth token exchange endpoint URL, not a secret.
            scopes=[
                "bookmarks:read",
                "bookmarks:write",
                "channels:history",
                "channels:read",
                "channels:write",
                "chat:write",
                "files:read",
                "files:write",
                "groups:history",
                "groups:read",
                "groups:write",
                "im:history",
                "im:read",
                "im:write",
                "reactions:read",
                "reactions:write",
                "search:read",
                "search:read.im",
                "search:read.private",
                "search:read.public",
                "search:read.users",
                "users:read",
                "users.profile:write",
            ],
        )

    async def validate_connection(self, token: str) -> bool:
        async with egress_client(timeout=30) as client:
            resp = await client.post(
                f"{_BASE_URL}/auth.test",
                headers={"Authorization": f"Bearer {token}"},
            )
            data = resp.json()
            return data.get("ok", False)

    async def get_user_info(self, token: str) -> dict:
        async with egress_client(timeout=30) as client:
            resp = await client.post(
                f"{_BASE_URL}/auth.test",
                headers={"Authorization": f"Bearer {token}"},
            )
            data = resp.json()
            return {
                "team": data.get("team"),
                "user": data.get("user"),
                "url": data.get("url"),
            }


# ---------------------------------------------------------------------------
# Slack tools
# ---------------------------------------------------------------------------


def _slack_call(data: dict) -> str | None:
    """Return error string if Slack returned ok=false, else None."""
    if not data.get("ok"):
        return f"Slack error: {data.get('error', 'unknown')}"
    return None


# Cap on cursor-followed pages so a huge workspace can't spin forever.
_SLACK_MAX_PAGES = 10


async def _slack_collect(
    client: httpx.AsyncClient,
    headers: dict,
    url: str,
    action: str,
    params: dict,
    result_key: str,
    want: int,
) -> tuple[list[dict], str | None]:
    """Fetch a Slack list endpoint, following ``response_metadata.next_cursor``.

    Slack's ``conversations.list`` / ``users.list`` apply server-side filtering
    *after* pagination, so a single page can return fewer than ``limit`` items
    while more pages still exist. Following the cursor (up to ``want`` items /
    ``_SLACK_MAX_PAGES`` pages) prevents the tools from silently returning an
    incomplete set — which previously made the agent conclude a channel/user
    did not exist.
    """
    items: list[dict] = []
    cursor: str | None = None
    for _ in range(_SLACK_MAX_PAGES):
        page_params = dict(params)
        # Request only what's still needed (Slack caps per-page at ~1000).
        page_params["limit"] = str(min(max(want - len(items), 1), 200))
        if cursor:
            page_params["cursor"] = cursor
        resp, err = await safe_request(
            client, "get", url, _PROVIDER, action, headers=headers, params=page_params
        )
        if err:
            return items, err
        data = resp.json()
        if err := _slack_call(data):
            # Surface the error only if we have nothing yet; otherwise return
            # what we collected so a mid-pagination hiccup isn't fatal.
            return (items, err) if not items else (items, None)
        items.extend(data.get(result_key, []))
        cursor = (data.get("response_metadata") or {}).get("next_cursor") or None
        if not cursor or len(items) >= want:
            break
    return items, None


def _match_channel_id(match: dict) -> Any:
    """The conversation ID of a ``search.messages`` match."""
    channel = match.get("channel")
    return channel.get("id") if isinstance(channel, dict) else channel


def _file_channels(file: dict) -> set[str]:
    """Every conversation a Slack file object says it is shared in."""
    ids: set[str] = set()
    for key in ("channels", "groups", "ims"):
        ids.update(c for c in file.get(key) or [] if isinstance(c, str))
    for kind in ("public", "private"):
        ids.update((file.get("shares") or {}).get(kind) or {})
    return ids


def _file_outside_scope(file: dict, file_id: str, *, deleting: bool = False) -> str | None:
    """Under a channel Scope(s), why *file* may not be used.

    Reading a file shared in one selected channel is reading that channel.
    ``files.delete`` is workspace-wide, though - it removes the file from
    EVERY conversation it is shared in - so a delete needs all of them in
    the Scope(s)."""
    scope = active_channel_scope()
    if scope is None:
        return None
    channels = _file_channels(file)
    if deleting:
        outside = sorted(c for c in channels if not in_channel_scope(c))
        if channels and not outside:
            return None
        where = (
            f"it is also shared in {', '.join(outside)}, outside the Scope(s)"
            if outside
            else "Slack reports no channel it is shared in"
        )
        return (
            f"Refused: file {file_id} cannot be deleted - deletion removes it from every "
            f"conversation, and {where} set for this agent or assignment "
            f"(allowed channels: {allowed_text(scope)})."
        )
    if any(in_channel_scope(c) for c in channels):
        return None
    return (
        f"Refused: file {file_id} is not shared in a channel of the Scope(s) set for this "
        f"agent or assignment (allowed channels: {allowed_text(scope)})."
    )


def build_tools(
    token: str, executor: CallableToolExecutor, **kwargs: Any
) -> list[ToolDefinition]:
    headers = {"Authorization": f"Bearer {token}"}
    persist_file_fn = kwargs.get("persist_file_fn")

    # ── Read tools ────────────────────────────────────────────────────────

    async def list_slack_channels(count: int = 20) -> str:
        """List Slack channels the user has access to."""
        async with egress_client(timeout=30) as client:
            channels, err = await _slack_collect(
                client,
                headers,
                f"{_BASE_URL}/conversations.list",
                "list channels",
                {"types": "public_channel,private_channel"},
                "channels",
                count,
            )
            # Fallback: if groups:read scope is missing, retry public-only.
            if err and "missing_scope" in err:
                channels, err = await _slack_collect(
                    client,
                    headers,
                    f"{_BASE_URL}/conversations.list",
                    "list channels",
                    {"types": "public_channel"},
                    "channels",
                    count,
                )
            if err:
                return err

            def _fmt(c: dict) -> str:
                return (
                    f"#{c['name']} (ID: {c['id']}, "
                    f"{c.get('num_members', '?')} members) — "
                    f"{c.get('purpose', {}).get('value', '')[:80]}"
                )

            scope = active_channel_scope()
            if scope is not None:
                # Under a channel Scope(s): the selected channels
                # only, and the ones this listing did not return still named.
                kept = [c for c in channels if isinstance(c, dict) and in_channel_scope(c.get("id"))]
                listed = {str(c.get("id", "")).upper() for c in kept}
                lines = [
                    f"Channels in this agent's or assignment's Scope(s) ({len(scope)}); "
                    "no other channel may be used:"
                ]
                lines += [_fmt(c) for c in kept]
                lines += [
                    f"#{name or channel_id} (ID: {channel_id}, in scope; not in this listing)"
                    for channel_id, name in sorted(scope.items())
                    if channel_id not in listed
                ]
                return "\n".join(lines)
            return format_entity_list(channels[:count], _fmt, noun="channel")

    async def read_slack_messages(channel: str, count: int = 15) -> str:
        """Read recent messages from a Slack channel or DM."""
        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{_BASE_URL}/conversations.history",
                _PROVIDER,
                "read messages",
                headers=headers,
                params={"channel": channel, "limit": str(count)},
            )
            if err:
                return err
            data = resp.json()
            if err := _slack_call(data):
                return err
            msgs = data.get("messages", [])
            if not msgs:
                return "No messages found."
            lines = []
            for m in msgs:
                user = m.get("user", "bot")
                ts = m.get("ts", "")
                text = m.get("text", "")[:300]
                thread = (
                    f" [thread: {m['thread_ts']}]"
                    if m.get("thread_ts") and m.get("thread_ts") != ts
                    else ""
                )
                lines.append(f"[{ts}] <{user}>{thread}: {text}{message_files_suffix(m)}")
            return "\n".join(lines)

    async def read_slack_thread(channel: str, thread_ts: str, count: int = 20) -> str:
        """Read replies in a Slack thread."""
        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{_BASE_URL}/conversations.replies",
                _PROVIDER,
                "read thread",
                headers=headers,
                params={"channel": channel, "ts": thread_ts, "limit": str(count)},
            )
            if err:
                return err
            data = resp.json()
            if err := _slack_call(data):
                return err
            msgs = data.get("messages", [])
            if not msgs:
                return "No thread replies found."
            lines = []
            for m in msgs:
                user = m.get("user", "bot")
                text = m.get("text", "")[:300]
                lines.append(f"[{m.get('ts', '')}] <{user}>: {text}{message_files_suffix(m)}")
            return "\n".join(lines)

    async def list_slack_dms(count: int = 20) -> str:
        """List direct message conversations."""
        async with egress_client(timeout=30) as client:
            channels, err = await _slack_collect(
                client,
                headers,
                f"{_BASE_URL}/conversations.list",
                "list DMs",
                {"types": "im,mpim"},
                "channels",
                count,
            )
            if err:
                return err
            return format_entity_list(
                channels[:count],
                lambda c: f"DM with {c.get('user', 'group')} (ID: {c['id']})",
                noun="DM conversation",
            )

    async def get_slack_user(user_id: str) -> str:
        """Get info about a Slack user."""
        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{_BASE_URL}/users.info",
                _PROVIDER,
                "get user info",
                headers=headers,
                params={"user": user_id},
            )
            if err:
                return err
            data = resp.json()
            if err := _slack_call(data):
                return err
            u = data.get("user", {})
            profile = u.get("profile", {})
            return (
                f"Name: {profile.get('real_name', u.get('name', '?'))}\n"
                f"Display: {profile.get('display_name', '')}\n"
                f"Email: {profile.get('email', 'N/A')}\n"
                f"Title: {profile.get('title', '')}\n"
                f"Status: {profile.get('status_emoji', '')} {profile.get('status_text', '')}\n"
                f"Is bot: {u.get('is_bot', False)}"
            )

    async def list_slack_users(count: int = 50) -> str:
        """List users in the Slack workspace."""
        async with egress_client(timeout=30) as client:
            members, err = await _slack_collect(
                client,
                headers,
                f"{_BASE_URL}/users.list",
                "list users",
                {},
                "members",
                count,
            )
            if err:
                return err
            if not members:
                return "No users found."
            active = [
                u
                for u in members
                if not u.get("deleted") and u.get("id") != "USLACKBOT"
            ]

            def _fmt_user(u: dict) -> str:
                profile = u.get("profile", {})
                name = profile.get("real_name", u.get("name", "?"))
                parts = [f"{name} (ID: {u['id']})"]
                if display := profile.get("display_name", ""):
                    parts.append(f"display: {display}")
                if email := profile.get("email", ""):
                    parts.append(f"email: {email}")
                if ttl := profile.get("title", ""):
                    parts.append(f"title: {ttl}")
                if u.get("is_bot", False):
                    parts.append("(bot)")
                return " — ".join(parts)

            return format_entity_list(
                active,
                _fmt_user,
                noun="user",
                empty_msg="No active users found.",
            )

    # ── Search tools ────────────────────────────────────────────────────

    async def search_slack_messages(
        query: str, count: int = 20, sort: str = "timestamp"
    ) -> str:
        """Search messages across the entire Slack workspace."""
        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{_BASE_URL}/search.messages",
                _PROVIDER,
                "search messages",
                headers=headers,
                params={
                    "query": query,
                    "count": str(count),
                    "sort": sort,
                    "sort_dir": "desc",
                },
            )
            if err:
                return err
            data = resp.json()
            if err := _slack_call(data):
                return err
            matches = data.get("messages", {}).get("matches", [])
            total = data.get("messages", {}).get("total", len(matches))
            if active_channel_scope() is not None:
                # The search spans the workspace: under a channel Scope(s)
                # a match from another conversation is dropped, and so
                # is Slack's total over the whole workspace.
                matches = [
                    m for m in matches
                    if isinstance(m, dict) and in_channel_scope(_match_channel_id(m))
                ]
                total = len(matches)
            if not matches:
                return f"No messages found for query: {query}"
            lines = [f"Found {total} results (showing {len(matches)}):"]
            for m in matches:
                ch = m.get("channel", {})
                ch_name = (
                    ch.get("name", ch.get("id", "?"))
                    if isinstance(ch, dict)
                    else str(ch)
                )
                user = m.get("user", m.get("username", "?"))
                ts = m.get("ts", "")
                text = m.get("text", "")[:300]
                permalink = m.get("permalink", "")
                lines.append(f"[{ts}] #{ch_name} <{user}>: {text}{message_files_suffix(m)}")
                if permalink:
                    lines.append(f"  Link: {permalink}")
            return "\n".join(lines)

    async def search_slack_files(query: str, count: int = 20) -> str:
        """Search files across the entire Slack workspace."""
        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{_BASE_URL}/search.files",
                _PROVIDER,
                "search files",
                headers=headers,
                params={
                    "query": query,
                    "count": str(count),
                    "sort": "timestamp",
                    "sort_dir": "desc",
                },
            )
            if err:
                return err
            data = resp.json()
            if err := _slack_call(data):
                return err
            matches = data.get("files", {}).get("matches", [])
            total = data.get("files", {}).get("total", len(matches))
            if active_channel_scope() is not None:
                # Only the files shared in a selected channel.
                matches = [
                    f for f in matches
                    if isinstance(f, dict) and any(in_channel_scope(c) for c in _file_channels(f))
                ]
                total = len(matches)
            if not matches:
                return f"No files found for query: {query}"
            lines = [f"Found {total} files (showing {len(matches)}):"]
            for f in matches:
                name = f.get("name", "untitled")
                ftype = f.get("filetype", "?")
                user = f.get("user", "?")
                size = f.get("size", 0)
                permalink = f.get("permalink", "")
                line = f"  {name} (ID: {f.get('id', '?')}, {ftype}, {size} bytes, by {user}) — {permalink}"
                if url := download_url(f):
                    line += f" | download: {url}"
                lines.append(line)
            lines.append("\nTip: download_slack_file(file_id) brings a file into the conversation.")
            return "\n".join(lines)

    # ── File tools ────────────────────────────────────────────────────────

    async def list_slack_files(
        channel: str = "", count: int = 20, types: str = "all"
    ) -> str:
        """List files shared in a channel, DM, or across the workspace."""
        async with egress_client(timeout=30) as client:
            params: dict[str, str] = {"count": str(count), "types": types}
            if channel:
                params["channel"] = channel
            resp, err = await safe_request(
                client,
                "get",
                f"{_BASE_URL}/files.list",
                _PROVIDER,
                "list files",
                headers=headers,
                params=params,
            )
            if err:
                return err
            data = resp.json()
            if err := _slack_call(data):
                return err
            def _fmt_file(f: dict) -> str:
                line = (
                    f"{f.get('name', 'untitled')} (ID: {f.get('id', '')}, "
                    f"{f.get('filetype', '?')}, {f.get('size', 0)} bytes, "
                    f"by {f.get('user', '?')}) — {f.get('permalink', '')}"
                )
                if url := download_url(f):
                    line += f" | download: {url}"
                return line

            return format_entity_list(data.get("files", []), _fmt_file, noun="file")

    async def get_slack_file_info(file_id: str) -> str | ToolResult:
        """Get detailed information about a Slack file."""
        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{_BASE_URL}/files.info",
                _PROVIDER,
                "get file info",
                headers=headers,
                params={"file": file_id},
            )
            if err:
                return err
            data = resp.json()
            if err := _slack_call(data):
                return err
            f = data.get("file", {})
            if refusal := _file_outside_scope(f, file_id):
                return ToolResult(tool_call_id="", content=refusal, is_error=True)
            shares = f.get("shares", {})
            shared_in = []
            for ch_type in ("public", "private"):
                for ch_id in shares.get(ch_type, {}):
                    shared_in.append(ch_id)
            return (
                f"Name: {f.get('name', '?')}\n"
                f"Type: {f.get('filetype', '?')} ({f.get('mimetype', '')})\n"
                f"Size: {f.get('size', 0)} bytes\n"
                f"User: {f.get('user', '?')}\n"
                f"Created: {f.get('created', '')}\n"
                f"URL: {f.get('url_private', f.get('permalink', ''))}\n"
                f"Web URL: {f.get('permalink', '') or 'N/A'}\n"
                f"Download URL: {download_url(f) or 'N/A'}\n"
                f"Shared in: {', '.join(shared_in) if shared_in else 'N/A'}\n"
                f"Preview: {f.get('preview', '')[:500]}\n"
                f"Tip: download_slack_file(file_id='{file_id}') brings the whole file "
                "into the conversation."
            )

    async def download_slack_file(file_id: str) -> str | ToolResult:
        """Download a Slack file into the conversation (stored file + links + file_id)."""
        file_id = (file_id or "").strip()
        if not file_id:
            return ToolResult(tool_call_id="", content="Error: file_id is required.", is_error=True)
        async with egress_client(timeout=30) as client:
            f, err = await get_file_info(client, headers, _PROVIDER, file_id)
        if err:
            return err
        if refusal := _file_outside_scope(f, file_id):
            return ToolResult(tool_call_id="", content=refusal, is_error=True)
        data, err = await download_file_bytes(headers, _PROVIDER, f)
        if err:
            return ToolResult(tool_call_id="", content=err, is_error=True)
        return await deliver_file(
            data=data,
            filename=f.get("name") or f.get("title") or f"slack-{file_id}",
            content_type=f.get("mimetype") or "application/octet-stream",
            source="Slack",
            persist_file_fn=persist_file_fn,
            links=file_links(f),
        )

    async def upload_slack_file(
        channels: str,
        content: str = "",
        filename: str = "",
        title: str = "",
        file_id: str = "",
        initial_comment: str = "",
        thread_ts: str = "",
        file_context: FileContext | None = None,
    ) -> str | ToolResult:
        """Upload a file to Slack channel(s): text ``content`` or a ``file_id`` of the run."""
        targets = split_channels(channels)
        if not targets:
            return ToolResult(
                tool_call_id="", content="Error: channels is required.", is_error=True
            )
        if bool(content) == bool((file_id or "").strip()):
            return ToolResult(
                tool_call_id="",
                content=(
                    "Error: provide exactly one of content (text to upload as a file) "
                    "or file_id (a file attached, generated or downloaded in this "
                    "conversation)."
                ),
                is_error=True,
            )
        if file_id.strip():
            piped = await read_pipe_file(
                file_context, file_id, max_bytes=MAX_SLACK_FILE_BYTES, target="Slack"
            )
            if isinstance(piped, str):
                return ToolResult(tool_call_id="", content=piped, is_error=True)
            data, piped_name, _ = piped
            filename = (filename or "").strip() or piped_name
        else:
            data = content.encode("utf-8")
            filename = (filename or "").strip() or "file.txt"
        uploaded, err = await upload_file_bytes(
            headers,
            _PROVIDER,
            data=data,
            filename=filename,
            channels=targets,
            title=title,
            initial_comment=initial_comment,
            thread_ts=(thread_ts or "").strip(),
        )
        if err:
            return ToolResult(tool_call_id="", content=err, is_error=True)
        await record_slack_file_shares(uploaded)
        # The stored name, Slack's name for it and its permalink (which
        # carries the name) go in the block.
        what = f"file {file_id.strip()}" if file_id.strip() else "the content as a file"
        return uploaded_file_answer(
            f"Uploaded {what} to Slack (ID: {uploaded.get('id', '')}), shared in {', '.join(targets)}.",
            target=_PROVIDER,
            filename=filename,
            details=[f"Slack name: {single_line(str(uploaded['name']))}"] if uploaded.get("name") else [],
            links=[("Permalink", uploaded.get("permalink"))],
        )

    # ── DM tools ──────────────────────────────────────────────────────────

    async def open_slack_dm(user_id: str) -> str:
        """Open or resume a direct message conversation with a user."""
        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "post",
                f"{_BASE_URL}/conversations.open",
                _PROVIDER,
                "open DM",
                headers={**headers, "Content-Type": "application/json"},
                json={"users": user_id},
            )
            if err:
                return err
            data = resp.json()
            if err := _slack_call(data):
                return err
            ch = data.get("channel", {})
            return f"DM opened with {user_id} — channel ID: {ch.get('id', '?')}"

    # ── Write tools ───────────────────────────────────────────────────────

    async def send_slack_message(channel: str, text: str) -> str:
        """Send a message to a Slack channel or DM."""
        # One reply per inbound message: refused when another run answered it.
        guard = await claim_slack_reply(channel)
        if guard.refused:
            return guard.refusal()
        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "post",
                f"{_BASE_URL}/chat.postMessage",
                _PROVIDER,
                "send message",
                headers={**headers, "Content-Type": "application/json"},
                json={"channel": channel, "text": markdown_to_slack(text)},
            )
            if err:
                await guard.release()
                return err
            data = resp.json()
            if err := _slack_call(data):
                await guard.release()
                return err
            await guard.confirm()
            await record_slack_post(data)
            return f"Message sent to {channel} (ts: {data.get('ts', '')})" + chat_reply_guard.answered_note(guard)

    async def reply_slack_thread(channel: str, thread_ts: str, text: str) -> str:
        """Reply to a specific message thread in Slack."""
        guard = await claim_slack_reply(channel, thread_ts)
        if guard.refused:
            return guard.refusal()
        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "post",
                f"{_BASE_URL}/chat.postMessage",
                _PROVIDER,
                "reply in thread",
                headers={**headers, "Content-Type": "application/json"},
                json={
                    "channel": channel,
                    "text": markdown_to_slack(text),
                    "thread_ts": thread_ts,
                },
            )
            if err:
                await guard.release()
                return err
            data = resp.json()
            if err := _slack_call(data):
                await guard.release()
                return err
            await guard.confirm()
            await record_slack_post(data)
            return f"Reply sent in thread {thread_ts} (ts: {data.get('ts', '')})" + chat_reply_guard.answered_note(guard)

    async def add_slack_reaction(channel: str, timestamp: str, emoji: str) -> str:
        """Add an emoji reaction to a message."""
        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "post",
                f"{_BASE_URL}/reactions.add",
                _PROVIDER,
                "add reaction",
                headers={**headers, "Content-Type": "application/json"},
                json={
                    "channel": channel,
                    "timestamp": timestamp,
                    "name": emoji.strip(":"),
                },
            )
            if err:
                return err
            data = resp.json()
            if err := _slack_call(data):
                return err
            return f"Reaction :{emoji.strip(':')}:  added."

    async def get_slack_reactions(channel: str, timestamp: str) -> str:
        """Get emoji reactions on a message."""
        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{_BASE_URL}/reactions.get",
                _PROVIDER,
                "get reactions",
                headers=headers,
                params={"channel": channel, "timestamp": timestamp, "full": "true"},
            )
            if err:
                return err
            data = resp.json()
            if err := _slack_call(data):
                return err
            msg = data.get("message", {})
            reactions = msg.get("reactions", [])
            if not reactions:
                return "No reactions on this message."
            lines = []
            for r in reactions:
                emoji = r.get("name", "?")
                count = r.get("count", 0)
                users = ", ".join(r.get("users", []))
                lines.append(f":{emoji}: × {count} (by {users})")
            return "\n".join(lines)

    # ── User profile tools ────────────────────────────────────────────────

    async def set_slack_user_status(
        status_text: str, status_emoji: str = "", expiration: int = 0
    ) -> str:
        """Set the authenticated user's Slack status."""
        async with egress_client(timeout=30) as client:
            profile: dict[str, Any] = {"status_text": status_text}
            if status_emoji:
                profile["status_emoji"] = (
                    status_emoji
                    if status_emoji.startswith(":")
                    else f":{status_emoji}:"
                )
            if expiration:
                profile["status_expiration"] = expiration
            resp, err = await safe_request(
                client,
                "post",
                f"{_BASE_URL}/users.profile.set",
                _PROVIDER,
                "set user status",
                headers={**headers, "Content-Type": "application/json"},
                json={"profile": profile},
            )
            if err:
                return err
            data = resp.json()
            if err := _slack_call(data):
                return err
            return f"Status updated to {status_emoji} {status_text}"

    # ── Channel management tools ──────────────────────────────────────────

    async def set_slack_channel_topic(channel: str, topic: str) -> str:
        """Set the topic of a Slack channel."""
        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "post",
                f"{_BASE_URL}/conversations.setTopic",
                _PROVIDER,
                "set channel topic",
                headers={**headers, "Content-Type": "application/json"},
                json={"channel": channel, "topic": topic},
            )
            if err:
                return err
            data = resp.json()
            if err := _slack_call(data):
                return err
            return "Channel topic updated."

    async def set_slack_channel_purpose(channel: str, purpose: str) -> str:
        """Set the purpose/description of a Slack channel."""
        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "post",
                f"{_BASE_URL}/conversations.setPurpose",
                _PROVIDER,
                "set channel purpose",
                headers={**headers, "Content-Type": "application/json"},
                json={"channel": channel, "purpose": purpose},
            )
            if err:
                return err
            data = resp.json()
            if err := _slack_call(data):
                return err
            return "Channel purpose updated."

    async def create_slack_channel(name: str, is_private: bool = False) -> str:
        """Create a new Slack channel (public or private)."""
        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "post",
                f"{_BASE_URL}/conversations.create",
                _PROVIDER,
                "create channel",
                headers={**headers, "Content-Type": "application/json"},
                json={"name": name, "is_private": is_private},
            )
            if err:
                return err
            data = resp.json()
            if err := _slack_call(data):
                return err
            ch = data.get("channel", {})
            kind = "private" if is_private else "public"
            return f"Created {kind} channel #{ch.get('name', name)} (ID: {ch.get('id', '?')})"

    async def archive_slack_channel(channel: str) -> str:
        """Archive a Slack channel."""
        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "post",
                f"{_BASE_URL}/conversations.archive",
                _PROVIDER,
                "archive channel",
                headers={**headers, "Content-Type": "application/json"},
                json={"channel": channel},
            )
            if err:
                return err
            data = resp.json()
            if err := _slack_call(data):
                return err
            return f"Channel {channel} archived."

    async def rename_slack_channel(channel: str, name: str) -> str:
        """Rename a Slack channel."""
        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "post",
                f"{_BASE_URL}/conversations.rename",
                _PROVIDER,
                "rename channel",
                headers={**headers, "Content-Type": "application/json"},
                json={"channel": channel, "name": name},
            )
            if err:
                return err
            data = resp.json()
            if err := _slack_call(data):
                return err
            ch = data.get("channel", {})
            return f"Channel renamed to #{ch.get('name', name)}"

    async def invite_to_slack_channel(channel: str, users: str) -> str:
        """Invite one or more users to a Slack channel."""
        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "post",
                f"{_BASE_URL}/conversations.invite",
                _PROVIDER,
                "invite to channel",
                headers={**headers, "Content-Type": "application/json"},
                json={"channel": channel, "users": users},
            )
            if err:
                return err
            data = resp.json()
            if err := _slack_call(data):
                return err
            return f"User(s) {users} invited to channel {channel}."

    async def kick_from_slack_channel(channel: str, user: str) -> str:
        """Remove a user from a Slack channel."""
        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "post",
                f"{_BASE_URL}/conversations.kick",
                _PROVIDER,
                "kick from channel",
                headers={**headers, "Content-Type": "application/json"},
                json={"channel": channel, "user": user},
            )
            if err:
                return err
            data = resp.json()
            if err := _slack_call(data):
                return err
            return f"User {user} removed from channel {channel}."

    # ── File management tools ─────────────────────────────────────────────

    async def delete_slack_file(file_id: str) -> str | ToolResult:
        """Delete a file from Slack."""
        async with egress_client(timeout=30) as client:
            if active_channel_scope() is not None:
                # A file ID says nothing about where the file lives: resolve
                # it before deleting anything under a channel Scope(s).
                info, err = await safe_request(
                    client,
                    "get",
                    f"{_BASE_URL}/files.info",
                    _PROVIDER,
                    "get file info",
                    headers=headers,
                    params={"file": file_id},
                )
                if err:
                    return err
                info_data = info.json()
                if err := _slack_call(info_data):
                    return err
                if refusal := _file_outside_scope(
                    info_data.get("file") or {}, file_id, deleting=True
                ):
                    return ToolResult(tool_call_id="", content=refusal, is_error=True)
            resp, err = await safe_request(
                client,
                "post",
                f"{_BASE_URL}/files.delete",
                _PROVIDER,
                "delete file",
                headers={**headers, "Content-Type": "application/json"},
                json={"file": file_id},
            )
            if err:
                return err
            data = resp.json()
            if err := _slack_call(data):
                return err
            return f"File {file_id} deleted."

    # ── User search tools ─────────────────────────────────────────────────

    async def search_slack_users(query: str, count: int = 20) -> str:
        """Search users in the Slack workspace by name or email."""
        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{_BASE_URL}/users.search",
                _PROVIDER,
                "search users",
                headers=headers,
                params={"query": query, "count": str(count)},
            )
            if err:
                return err
            data = resp.json()
            if err := _slack_call(data):
                return err
            members = data.get("members", [])
            if not members:
                return f"No users found for query: {query}"

            def _fmt_user(u: dict) -> str:
                profile = u.get("profile", {})
                name = profile.get("real_name", u.get("name", "?"))
                parts = [f"{name} (ID: {u['id']})"]
                if display := profile.get("display_name", ""):
                    parts.append(f"display: {display}")
                if email := profile.get("email", ""):
                    parts.append(f"email: {email}")
                if ttl := profile.get("title", ""):
                    parts.append(f"title: {ttl}")
                if u.get("is_bot", False):
                    parts.append("(bot)")
                return " — ".join(parts)

            return format_entity_list(
                members,
                _fmt_user,
                noun="user",
                empty_msg="No users found.",
            )

    # ── Bookmark tools ────────────────────────────────────────────────────

    async def list_slack_bookmarks(channel: str) -> str:
        """List bookmarks saved in a Slack channel."""
        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "post",
                f"{_BASE_URL}/bookmarks.list",
                _PROVIDER,
                "list bookmarks",
                headers={**headers, "Content-Type": "application/json"},
                json={"channel_id": channel},
            )
            if err:
                return err
            data = resp.json()
            if err := _slack_call(data):
                return err
            return format_entity_list(
                data.get("bookmarks", []),
                lambda b: (
                    f"{b.get('emoji', '')} {b.get('title', 'Untitled')} — {b.get('link', '')}"
                    if b.get("link")
                    else f"{b.get('emoji', '')} {b.get('title', 'Untitled')}"
                ),
                noun="bookmark",
                empty_msg="No bookmarks in this channel.",
            )

    async def add_slack_bookmark(
        channel: str, title: str, link: str, emoji: str = ""
    ) -> str:
        """Add a bookmark to a Slack channel."""
        async with egress_client(timeout=30) as client:
            payload: dict[str, str] = {
                "channel_id": channel,
                "title": title,
                "type": "link",
                "link": link,
            }
            if emoji:
                payload["emoji"] = emoji
            resp, err = await safe_request(
                client,
                "post",
                f"{_BASE_URL}/bookmarks.add",
                _PROVIDER,
                "add bookmark",
                headers={**headers, "Content-Type": "application/json"},
                json=payload,
            )
            if err:
                return err
            data = resp.json()
            if err := _slack_call(data):
                return err
            return f"Bookmark '{title}' added to channel."

    # ── Register ──────────────────────────────────────────────────────────

    executor.register("list_slack_channels", list_slack_channels)
    executor.register("read_slack_messages", read_slack_messages)
    executor.register("read_slack_thread", read_slack_thread)
    executor.register("list_slack_dms", list_slack_dms)
    executor.register("get_slack_user", get_slack_user)
    executor.register("list_slack_users", list_slack_users)
    executor.register("search_slack_messages", search_slack_messages)
    executor.register("search_slack_files", search_slack_files)
    executor.register("search_slack_users", search_slack_users)
    executor.register("list_slack_files", list_slack_files)
    executor.register("get_slack_file_info", get_slack_file_info)
    executor.register("download_slack_file", download_slack_file)
    executor.register("upload_slack_file", upload_slack_file)
    executor.register("delete_slack_file", delete_slack_file)
    executor.register("open_slack_dm", open_slack_dm)
    executor.register("send_slack_message", send_slack_message)
    executor.register("reply_slack_thread", reply_slack_thread)
    executor.register("add_slack_reaction", add_slack_reaction)
    executor.register("get_slack_reactions", get_slack_reactions)
    executor.register("set_slack_user_status", set_slack_user_status)
    executor.register("set_slack_channel_topic", set_slack_channel_topic)
    executor.register("set_slack_channel_purpose", set_slack_channel_purpose)
    executor.register("create_slack_channel", create_slack_channel)
    executor.register("archive_slack_channel", archive_slack_channel)
    executor.register("rename_slack_channel", rename_slack_channel)
    executor.register("invite_to_slack_channel", invite_to_slack_channel)
    executor.register("kick_from_slack_channel", kick_from_slack_channel)
    executor.register("list_slack_bookmarks", list_slack_bookmarks)
    executor.register("add_slack_bookmark", add_slack_bookmark)

    definitions = [
        # Search
        ToolDefinition(
            name="search_slack_messages",
            description="Search messages across the entire Slack workspace. Searches all public channels, private channels, and DMs the user has access to.",
            input_schema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search query (supports Slack search modifiers like 'from:@user', 'in:#channel', 'has:link', 'before:2024-01-01')",
                    },
                    "count": {
                        "type": "integer",
                        "default": 20,
                        "description": "Max results to return",
                    },
                    "sort": {
                        "type": "string",
                        "enum": ["timestamp", "score"],
                        "default": "timestamp",
                        "description": "Sort by relevance (score) or recency (timestamp)",
                    },
                },
                "required": ["query"],
            },
            source="integration:slack",
            priority=0,
        ),
        ToolDefinition(
            name="search_slack_files",
            description="Search files shared across the Slack workspace.",
            input_schema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search query for files",
                    },
                    "count": {"type": "integer", "default": 20},
                },
                "required": ["query"],
            },
            source="integration:slack",
            priority=1,
        ),
        # Read
        ToolDefinition(
            name="list_slack_channels",
            description="List Slack channels the user has access to. Returns channel name, ID, member count, and purpose.",
            input_schema={
                "type": "object",
                "properties": {"count": {"type": "integer", "default": 20}},
                "required": [],
            },
            source="integration:slack",
            priority=1,
        ),
        ToolDefinition(
            name="read_slack_messages",
            description="Read recent messages from a Slack channel or DM. Returns timestamp, user, and text.",
            input_schema={
                "type": "object",
                "properties": {
                    "channel": {
                        "type": "string",
                        "description": "Slack channel or DM ID",
                    },
                    "count": {"type": "integer", "default": 15},
                },
                "required": ["channel"],
            },
            source="integration:slack",
            priority=0,
        ),
        ToolDefinition(
            name="read_slack_thread",
            description="Read all replies in a Slack message thread.",
            input_schema={
                "type": "object",
                "properties": {
                    "channel": {"type": "string", "description": "Channel ID"},
                    "thread_ts": {
                        "type": "string",
                        "description": "Thread timestamp (ts of the parent message)",
                    },
                    "count": {"type": "integer", "default": 20},
                },
                "required": ["channel", "thread_ts"],
            },
            source="integration:slack",
            priority=1,
        ),
        ToolDefinition(
            name="list_slack_dms",
            description="List direct message conversations (1:1 and group DMs).",
            input_schema={
                "type": "object",
                "properties": {"count": {"type": "integer", "default": 20}},
                "required": [],
            },
            source="integration:slack",
            priority=1,
        ),
        ToolDefinition(
            name="get_slack_user",
            description="Get information about a Slack user (name, email, title, status).",
            input_schema={
                "type": "object",
                "properties": {
                    "user_id": {
                        "type": "string",
                        "description": "Slack user ID (e.g. U01234)",
                    }
                },
                "required": ["user_id"],
            },
            source="integration:slack",
            priority=1,
        ),
        ToolDefinition(
            name="list_slack_users",
            description="List users in the Slack workspace with name, email, title, and status.",
            input_schema={
                "type": "object",
                "properties": {
                    "count": {
                        "type": "integer",
                        "default": 50,
                        "description": "Max users to return",
                    }
                },
                "required": [],
            },
            source="integration:slack",
            priority=2,
        ),
        ToolDefinition(
            name="search_slack_users",
            description="Search users in the Slack workspace by name or email.",
            input_schema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search query (name, display name, or email)",
                    },
                    "count": {
                        "type": "integer",
                        "default": 20,
                        "description": "Max users to return",
                    },
                },
                "required": ["query"],
            },
            source="integration:slack",
            priority=1,
        ),
        # Files
        ToolDefinition(
            name="list_slack_files",
            description="List files shared in a channel, DM, or across the workspace.",
            input_schema={
                "type": "object",
                "properties": {
                    "channel": {
                        "type": "string",
                        "default": "",
                        "description": "Channel/DM ID to filter by (empty for all)",
                    },
                    "count": {"type": "integer", "default": 20},
                    "types": {
                        "type": "string",
                        "default": "all",
                        "description": "File type filter: all, spaces, snippets, images, gdocs, zips, pdfs",
                    },
                },
                "required": [],
            },
            source="integration:slack",
            priority=2,
        ),
        ToolDefinition(
            name="get_slack_file_info",
            description="Get detailed information about a Slack file (name, type, size, links, preview, shared channels).",
            input_schema={
                "type": "object",
                "properties": {
                    "file_id": {"type": "string", "description": "Slack file ID"}
                },
                "required": ["file_id"],
            },
            source="integration:slack",
            priority=2,
        ),
        ToolDefinition(
            name="download_slack_file",
            workspace_ingest=True,
            description=(
                "Download a Slack file by ID into the conversation: a download card, "
                "its links and a file_id to forward to any tool that takes a file. "
                "Its readable content comes with the result."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "file_id": {"type": "string", "description": "Slack file ID (from a message, a listing or a search)"}
                },
                "required": ["file_id"],
            },
            source="integration:slack",
            priority=1,
        ),
        ToolDefinition(
            name="upload_slack_file",
            description=(
                "Upload a file to one or more Slack channels: either text content, or "
                "file_id - any file attached, generated or downloaded in this "
                "conversation, forwarded byte-for-byte."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "channels": {
                        "type": "string",
                        "description": "Comma-separated channel IDs to share the file in",
                    },
                    "content": {
                        "type": "string",
                        "description": "Text content to upload (instead of file_id)",
                    },
                    "file_id": {
                        "type": "string",
                        "description": "A file of this conversation to upload as-is (instead of content)",
                    },
                    "filename": {
                        "type": "string",
                        "default": "",
                        "description": "Filename with extension (default: the file's own name, or file.txt for content)",
                    },
                    "title": {
                        "type": "string",
                        "default": "",
                        "description": "Optional display title",
                    },
                    "initial_comment": {
                        "type": "string",
                        "default": "",
                        "description": "Optional message posted with the file",
                    },
                    "thread_ts": {
                        "type": "string",
                        "default": "",
                        "description": "Optional thread timestamp to share the file in a thread",
                    },
                },
                "required": ["channels"],
            },
            source="integration:slack",
            priority=1,
        ),
        # DMs
        ToolDefinition(
            name="open_slack_dm",
            description="Open or resume a direct message conversation with a Slack user. Returns the DM channel ID which can be used to send messages.",
            input_schema={
                "type": "object",
                "properties": {
                    "user_id": {
                        "type": "string",
                        "description": "Slack user ID to open DM with",
                    }
                },
                "required": ["user_id"],
            },
            source="integration:slack",
            priority=1,
        ),
        # Write
        ToolDefinition(
            name="send_slack_message",
            description="Send a message to a Slack channel or DM.",
            input_schema={
                "type": "object",
                "properties": {
                    "channel": {"type": "string", "description": "Channel or DM ID"},
                    "text": {
                        "type": "string",
                        "description": "Message text (supports Slack markdown)",
                    },
                },
                "required": ["channel", "text"],
            },
            source="integration:slack",
            priority=0,
            # Artifact markers in the text are sent as links (outbound_markers).
            outbound_text=("text",),
        ),
        ToolDefinition(
            name="reply_slack_thread",
            description="Reply to a specific message thread in Slack. The reply appears in the thread, not as a new channel message.",
            input_schema={
                "type": "object",
                "properties": {
                    "channel": {"type": "string", "description": "Channel ID"},
                    "thread_ts": {
                        "type": "string",
                        "description": "Thread timestamp (ts of the parent message)",
                    },
                    "text": {"type": "string", "description": "Reply text"},
                },
                "required": ["channel", "thread_ts", "text"],
            },
            source="integration:slack",
            priority=0,
            # Artifact markers in the text are sent as links (outbound_markers).
            outbound_text=("text",),
        ),
        # Reactions
        ToolDefinition(
            name="add_slack_reaction",
            description="Add an emoji reaction to a Slack message.",
            input_schema={
                "type": "object",
                "properties": {
                    "channel": {"type": "string", "description": "Channel ID"},
                    "timestamp": {"type": "string", "description": "Message timestamp"},
                    "emoji": {
                        "type": "string",
                        "description": "Emoji name without colons (e.g. 'thumbsup')",
                    },
                },
                "required": ["channel", "timestamp", "emoji"],
            },
            source="integration:slack",
            priority=1,
        ),
        ToolDefinition(
            name="get_slack_reactions",
            description="Get emoji reactions on a Slack message. Returns each reaction emoji, count, and which users reacted.",
            input_schema={
                "type": "object",
                "properties": {
                    "channel": {"type": "string", "description": "Channel ID"},
                    "timestamp": {"type": "string", "description": "Message timestamp"},
                },
                "required": ["channel", "timestamp"],
            },
            source="integration:slack",
            priority=2,
        ),
        # User profile
        ToolDefinition(
            name="set_slack_user_status",
            description="Set the authenticated user's Slack status (status text, emoji, and optional expiration).",
            input_schema={
                "type": "object",
                "properties": {
                    "status_text": {
                        "type": "string",
                        "description": "Status text (e.g. 'In a meeting')",
                    },
                    "status_emoji": {
                        "type": "string",
                        "default": "",
                        "description": "Status emoji (e.g. ':calendar:' or 'calendar')",
                    },
                    "expiration": {
                        "type": "integer",
                        "default": 0,
                        "description": "Unix timestamp when the status should expire (0 for no expiration)",
                    },
                },
                "required": ["status_text"],
            },
            source="integration:slack",
            priority=1,
        ),
        # Channel management
        ToolDefinition(
            name="set_slack_channel_topic",
            description="Set the topic of a Slack channel.",
            input_schema={
                "type": "object",
                "properties": {
                    "channel": {"type": "string", "description": "Channel ID"},
                    "topic": {
                        "type": "string",
                        "description": "New channel topic text",
                    },
                },
                "required": ["channel", "topic"],
            },
            source="integration:slack",
            priority=1,
        ),
        ToolDefinition(
            name="set_slack_channel_purpose",
            description="Set the purpose/description of a Slack channel.",
            input_schema={
                "type": "object",
                "properties": {
                    "channel": {"type": "string", "description": "Channel ID"},
                    "purpose": {
                        "type": "string",
                        "description": "New channel purpose text",
                    },
                },
                "required": ["channel", "purpose"],
            },
            source="integration:slack",
            priority=1,
        ),
        ToolDefinition(
            name="create_slack_channel",
            description="Create a new public or private Slack channel.",
            input_schema={
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Channel name (lowercase, no spaces, max 80 chars)",
                    },
                    "is_private": {
                        "type": "boolean",
                        "default": False,
                        "description": "If true, creates a private channel",
                    },
                },
                "required": ["name"],
            },
            source="integration:slack",
            priority=1,
        ),
        ToolDefinition(
            name="archive_slack_channel",
            description="Archive a Slack channel. Archived channels are hidden but can be unarchived by an admin.",
            input_schema={
                "type": "object",
                "properties": {
                    "channel": {
                        "type": "string",
                        "description": "Channel ID to archive",
                    },
                },
                "required": ["channel"],
            },
            source="integration:slack",
            priority=2,
        ),
        ToolDefinition(
            name="rename_slack_channel",
            description="Rename a Slack channel.",
            input_schema={
                "type": "object",
                "properties": {
                    "channel": {
                        "type": "string",
                        "description": "Channel ID to rename",
                    },
                    "name": {
                        "type": "string",
                        "description": "New channel name (lowercase, no spaces, max 80 chars)",
                    },
                },
                "required": ["channel", "name"],
            },
            source="integration:slack",
            priority=2,
        ),
        ToolDefinition(
            name="invite_to_slack_channel",
            description="Invite one or more users to a Slack channel (public or private).",
            input_schema={
                "type": "object",
                "properties": {
                    "channel": {"type": "string", "description": "Channel ID"},
                    "users": {
                        "type": "string",
                        "description": "Comma-separated user IDs to invite (e.g. 'U01234,U05678')",
                    },
                },
                "required": ["channel", "users"],
            },
            source="integration:slack",
            priority=1,
        ),
        ToolDefinition(
            name="kick_from_slack_channel",
            description="Remove a user from a Slack channel.",
            input_schema={
                "type": "object",
                "properties": {
                    "channel": {"type": "string", "description": "Channel ID"},
                    "user": {
                        "type": "string",
                        "description": "User ID to remove",
                    },
                },
                "required": ["channel", "user"],
            },
            source="integration:slack",
            priority=2,
        ),
        ToolDefinition(
            name="delete_slack_file",
            description="Delete a file from Slack.",
            input_schema={
                "type": "object",
                "properties": {
                    "file_id": {"type": "string", "description": "Slack file ID"},
                },
                "required": ["file_id"],
            },
            source="integration:slack",
            priority=2,
        ),
        # Bookmarks
        ToolDefinition(
            name="list_slack_bookmarks",
            description="List bookmarks saved in a Slack channel.",
            input_schema={
                "type": "object",
                "properties": {
                    "channel": {"type": "string", "description": "Channel ID"}
                },
                "required": ["channel"],
            },
            source="integration:slack",
            priority=2,
        ),
        ToolDefinition(
            name="add_slack_bookmark",
            description="Add a link bookmark to a Slack channel.",
            input_schema={
                "type": "object",
                "properties": {
                    "channel": {"type": "string", "description": "Channel ID"},
                    "title": {"type": "string", "description": "Bookmark title"},
                    "link": {"type": "string", "description": "URL to bookmark"},
                    "emoji": {
                        "type": "string",
                        "default": "",
                        "description": "Optional emoji for the bookmark (e.g. ':link:')",
                    },
                },
                "required": ["channel", "title", "link"],
            },
            source="integration:slack",
            priority=2,
        ),
    ]
    # The Slack channel Scope(s) of the run is a hard boundary.
    channel_scope = kwargs.get("channel_scope")
    if channel_scope is not None:
        return apply_channel_scope(definitions, executor, channel_scope)
    return definitions
