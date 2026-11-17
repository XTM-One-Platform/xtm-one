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

"""Slack Bot channel tools – Slack Web API with bot token.

These tools let agents send and read messages **as the Slack bot**.  They
are separate from the ``slack`` integration tools (which use a user OAuth
token and expose workspace-wide search, files, bookmarks, etc.).

The bot token is stored as ``api_key_enc`` on the ``slack_bot`` integration
and is passed directly by ``integration_tools._get_token``.
"""

from __future__ import annotations

import logging
from typing import Any

from app.core.egress import egress_client
from app.core.utils import markdown_to_slack
from app.integrations.file_delivery import deliver_file, read_pipe_file, uploaded_file_answer
from app.integrations.http import (
    format_entity_list,
    is_publish_allowed,
    safe_request,
)
from app.integrations.providers.slack import SLACK_API, _file_channels, _slack_call
from app.integrations.providers.slack_files import (
    MAX_SLACK_FILE_BYTES,
    download_file_bytes,
    file_links,
    get_file_info,
    message_files_suffix,
    upload_file_bytes,
)
from app.services import chat_reply_guard
from app.services.agent_posts import record_slack_file_shares, record_slack_post
from app.services.content_safety import single_line
from app.services.slack_conversations import claim_reply as claim_slack_reply
from app.tools.base import CallableToolExecutor, FileContext, ToolDefinition, ToolResult

logger = logging.getLogger(__name__)

_PROVIDER = "Slack Bot"
_SOURCE = "integration:slack_bot"


def build_tools(
    token: str,
    executor: CallableToolExecutor,
    **_kwargs: Any,
) -> list[ToolDefinition]:
    """Build Slack Bot channel tools using a bot token."""
    headers = {"Authorization": f"Bearer {token}"}

    # Publish channel whitelist — when set, restricts writes to these channel
    # IDs.  Read tools use ``read_channels`` when provided (the
    # blocked-publish case passes the agent's own selection there while
    # ``publish_channels`` holds a never-matching sentinel — see
    # ``build_channel_tools``); otherwise reads fall back to the publish
    # whitelist, preserving the historical single-whitelist behavior.
    _publish_channels: list[dict] | None = _kwargs.get("publish_channels")
    _read_channels: list[dict] | None = (
        _kwargs.get("read_channels") or _publish_channels
    )
    _allowed_ids: set[str] | None = (
        {ch["id"] for ch in _publish_channels if ch.get("id")}
        if _publish_channels
        else None
    )
    _read_allowed_ids: set[str] | None = (
        {ch["id"] for ch in _read_channels if ch.get("id")}
        if _read_channels
        else None
    )
    persist_file_fn = _kwargs.get("persist_file_fn")

    # ── Read tools ────────────────────────────────────────────────────────

    async def list_slack_bot_channels(count: int = 200) -> str:
        """List channels the bot is a member of (public + private)."""
        all_channels: list[dict] = []
        cursor: str | None = None
        channel_types = "public_channel,private_channel"

        async with egress_client(timeout=30) as client:
            while len(all_channels) < count:
                params: dict[str, str] = {
                    "limit": str(min(count - len(all_channels), 200)),
                    "types": channel_types,
                    "exclude_archived": "true",
                }
                if cursor:
                    params["cursor"] = cursor
                resp, err = await safe_request(
                    client,
                    "get",
                    f"{SLACK_API}/users.conversations",
                    _PROVIDER,
                    "list channels",
                    headers=headers,
                    params=params,
                )
                if err:
                    if all_channels:
                        break
                    return err
                data = resp.json()
                if (
                    not data.get("ok")
                    and data.get("error") == "missing_scope"
                    and "private_channel" in channel_types
                ):
                    logger.info(
                        "Slack bot missing groups:read scope, falling back to public channels only"
                    )
                    channel_types = "public_channel"
                    cursor = None
                    all_channels.clear()
                    continue
                if err := _slack_call(data):
                    if all_channels:
                        break
                    return err
                all_channels.extend(data.get("channels", []))
                next_cursor = data.get("response_metadata", {}).get("next_cursor", "")
                if not next_cursor:
                    break
                cursor = next_cursor

        channels = all_channels
        if _read_allowed_ids is not None:
            channels = [c for c in channels if c.get("id") in _read_allowed_ids]
        return format_entity_list(
            channels,
            lambda c: (
                f"#{c['name']} (ID: {c['id']}"
                f"{', private' if c.get('is_private') else ''}"
                f") — {(c.get('purpose', {}).get('value') or '')[:80]}"
            ),
            noun="channel",
            empty_msg="No channels found. The bot may need to be invited to channels.",
        )

    async def read_slack_bot_messages(channel: str, count: int = 15) -> str:
        """Read recent messages from a Slack channel."""
        if not is_publish_allowed(channel, _read_allowed_ids):
            return f"Access denied: channel {channel} is not in the allowed channels list for this channel bot."
        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{SLACK_API}/conversations.history",
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

            def _fmt(m: dict) -> str:
                user = m.get("user", "bot")
                ts = m.get("ts", "")
                text = (m.get("text") or "")[:300]
                thread = (
                    f" [thread: {m['thread_ts']}]"
                    if m.get("thread_ts") and m.get("thread_ts") != ts
                    else ""
                )
                return f"[{ts}] <{user}>{thread}: {text}{message_files_suffix(m)}"

            return format_entity_list(
                msgs,
                _fmt,
                noun="message",
                empty_msg="No messages found in this channel.",
            )

    async def read_slack_bot_thread(
        channel: str, thread_ts: str, count: int = 20
    ) -> str:
        """Read all replies in a Slack message thread."""
        if not is_publish_allowed(channel, _read_allowed_ids):
            return f"Access denied: channel {channel} is not in the allowed channels list for this channel bot."
        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{SLACK_API}/conversations.replies",
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
                text_content = (m.get("text") or "")[:300]
                lines.append(
                    f"[{m.get('ts', '')}] <{user}>: {text_content}{message_files_suffix(m)}"
                )
            return "\n".join(lines)

    # ── File tools ────────────────────────────────────────────────────────

    async def download_slack_bot_file(file_id: str) -> str | ToolResult:
        """Download a file shared in a channel the bot may read (stored file + links + file_id)."""
        file_id = (file_id or "").strip()
        if not file_id:
            return ToolResult(tool_call_id="", content="Error: file_id is required.", is_error=True)
        async with egress_client(timeout=30) as client:
            f, err = await get_file_info(client, headers, _PROVIDER, file_id)
        if err:
            return err
        if _read_allowed_ids is not None and not (_file_channels(f) & _read_allowed_ids):
            return ToolResult(
                tool_call_id="",
                content=(
                    f"Access denied: file {file_id} is not shared in a channel on the "
                    "allowed channels list for this channel bot."
                ),
                is_error=True,
            )
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

    async def upload_slack_bot_file(
        channel: str,
        file_id: str = "",
        content: str = "",
        filename: str = "",
        title: str = "",
        initial_comment: str = "",
        thread_ts: str = "",
        file_context: FileContext | None = None,
    ) -> str | ToolResult:
        """Share a file in a Slack channel as the bot: a file_id of the run or text content."""
        channel = (channel or "").strip()
        if not is_publish_allowed(channel, _allowed_ids):
            return f"Access denied: channel {channel} is not in the publish channels list for this channel bot."
        if bool(content) == bool((file_id or "").strip()):
            return ToolResult(
                tool_call_id="",
                content=(
                    "Error: provide exactly one of file_id (a file attached, generated or "
                    "downloaded in this conversation) or content (text to upload as a file)."
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
            channels=[channel],
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
            f"Uploaded {what} to Slack (ID: {uploaded.get('id', '')}), shared in {channel}.",
            target=_PROVIDER,
            filename=filename,
            details=[f"Slack name: {single_line(str(uploaded['name']))}"] if uploaded.get("name") else [],
            links=[("Permalink", uploaded.get("permalink"))],
        )

    async def get_slack_bot_user(user_id: str) -> str:
        """Get info about a Slack user."""
        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{SLACK_API}/users.info",
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

    async def list_slack_bot_users(count: int = 50) -> str:
        """List users in the Slack workspace."""
        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{SLACK_API}/users.list",
                _PROVIDER,
                "list users",
                headers=headers,
                params={"limit": str(count)},
            )
            if err:
                return err
            data = resp.json()
            if err := _slack_call(data):
                return err
            members = data.get("members", [])
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

    # ── Write tools ───────────────────────────────────────────────────────

    async def send_slack_bot_message(channel: str, text: str) -> str:
        """Send a message to a Slack channel as the bot."""
        if not is_publish_allowed(channel, _allowed_ids):
            return f"Access denied: channel {channel} is not in the publish channels list for this channel bot."
        # One reply per inbound message: refused when another run answered it.
        guard = await claim_slack_reply(channel)
        if guard.refused:
            return guard.refusal()
        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "post",
                f"{SLACK_API}/chat.postMessage",
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

    async def reply_slack_bot_thread(channel: str, thread_ts: str, text: str) -> str:
        """Reply in a thread as the bot."""
        if not is_publish_allowed(channel, _allowed_ids):
            return f"Access denied: channel {channel} is not in the publish channels list for this channel bot."
        guard = await claim_slack_reply(channel, thread_ts)
        if guard.refused:
            return guard.refusal()
        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "post",
                f"{SLACK_API}/chat.postMessage",
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

    async def add_slack_bot_reaction(channel: str, timestamp: str, emoji: str) -> str:
        """Add an emoji reaction to a message."""
        if not is_publish_allowed(channel, _allowed_ids):
            return f"Access denied: channel {channel} is not in the publish channels list for this channel bot."
        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "post",
                f"{SLACK_API}/reactions.add",
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
            return f"Reaction :{emoji.strip(':')}: added."

    async def set_slack_bot_channel_topic(channel: str, topic: str) -> str:
        """Set the topic of a Slack channel."""
        if not is_publish_allowed(channel, _allowed_ids):
            return f"Access denied: channel {channel} is not in the publish channels list for this channel bot."
        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "post",
                f"{SLACK_API}/conversations.setTopic",
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

    async def delete_slack_bot_message(channel: str, timestamp: str) -> str:
        """Delete a message from a Slack channel.

        The bot can only delete its own messages, or messages in channels
        where the bot has the ``chat:write`` scope and appropriate permissions.
        """
        if not is_publish_allowed(channel, _allowed_ids):
            return f"Access denied: channel {channel} is not in the publish channels list for this channel bot."
        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "post",
                f"{SLACK_API}/chat.delete",
                _PROVIDER,
                "delete message",
                headers={**headers, "Content-Type": "application/json"},
                json={"channel": channel, "ts": timestamp},
            )
            if err:
                return err
            data = resp.json()
            if err := _slack_call(data):
                return err
            return f"Message {timestamp} deleted from {channel}."

    async def remove_slack_bot_reaction(
        channel: str, timestamp: str, emoji: str
    ) -> str:
        """Remove an emoji reaction from a Slack message."""
        if not is_publish_allowed(channel, _allowed_ids):
            return f"Access denied: channel {channel} is not in the publish channels list for this channel bot."
        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "post",
                f"{SLACK_API}/reactions.remove",
                _PROVIDER,
                "remove reaction",
                headers={**headers, "Content-Type": "application/json"},
                json={"channel": channel, "timestamp": timestamp, "name": emoji},
            )
            if err:
                return err
            data = resp.json()
            if err := _slack_call(data):
                return err
            return f"Reaction :{emoji}: removed from message {timestamp}."

    async def pin_slack_bot_message(channel: str, timestamp: str) -> str:
        """Pin a message in a Slack channel."""
        if not is_publish_allowed(channel, _allowed_ids):
            return f"Access denied: channel {channel} is not in the publish channels list for this channel bot."
        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "post",
                f"{SLACK_API}/pins.add",
                _PROVIDER,
                "pin message",
                headers={**headers, "Content-Type": "application/json"},
                json={"channel": channel, "timestamp": timestamp},
            )
            if err:
                return err
            data = resp.json()
            if err := _slack_call(data):
                return err
            return f"Message {timestamp} pinned in {channel}."

    async def unpin_slack_bot_message(channel: str, timestamp: str) -> str:
        """Unpin a message in a Slack channel."""
        if not is_publish_allowed(channel, _allowed_ids):
            return f"Access denied: channel {channel} is not in the publish channels list for this channel bot."
        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "post",
                f"{SLACK_API}/pins.remove",
                _PROVIDER,
                "unpin message",
                headers={**headers, "Content-Type": "application/json"},
                json={"channel": channel, "timestamp": timestamp},
            )
            if err:
                return err
            data = resp.json()
            if err := _slack_call(data):
                return err
            return f"Message {timestamp} unpinned in {channel}."

    # ── Register handlers ─────────────────────────────────────────────────

    executor.register("list_slack_bot_channels", list_slack_bot_channels)
    executor.register("read_slack_bot_messages", read_slack_bot_messages)
    executor.register("read_slack_bot_thread", read_slack_bot_thread)
    executor.register("download_slack_bot_file", download_slack_bot_file)
    executor.register("upload_slack_bot_file", upload_slack_bot_file)
    executor.register("get_slack_bot_user", get_slack_bot_user)
    executor.register("list_slack_bot_users", list_slack_bot_users)
    executor.register("send_slack_bot_message", send_slack_bot_message)
    executor.register("reply_slack_bot_thread", reply_slack_bot_thread)
    executor.register("add_slack_bot_reaction", add_slack_bot_reaction)
    executor.register("remove_slack_bot_reaction", remove_slack_bot_reaction)
    executor.register("pin_slack_bot_message", pin_slack_bot_message)
    executor.register("unpin_slack_bot_message", unpin_slack_bot_message)
    executor.register("set_slack_bot_channel_topic", set_slack_bot_channel_topic)
    executor.register("delete_slack_bot_message", delete_slack_bot_message)

    return [
        # Read
        ToolDefinition(
            name="list_slack_bot_channels",
            description="List Slack channels the bot is a member of (both public and private). Returns channel name, ID, private/public indicator, and purpose. The bot must be invited to private channels to see them. Uses cursor-based pagination.",
            input_schema={
                "type": "object",
                "properties": {
                    "count": {
                        "type": "integer",
                        "default": 200,
                        "description": "Max channels to return",
                    },
                },
                "required": [],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="read_slack_bot_messages",
            description="Read recent messages from a Slack channel. Returns timestamp, sender, thread info, and message text.",
            input_schema={
                "type": "object",
                "properties": {
                    "channel": {"type": "string", "description": "Slack channel ID"},
                    "count": {
                        "type": "integer",
                        "default": 15,
                        "description": "Max messages to return",
                    },
                },
                "required": ["channel"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="read_slack_bot_thread",
            description="Read all replies in a Slack message thread. Use read_slack_bot_messages first to find threads, then use the thread_ts to read replies.",
            input_schema={
                "type": "object",
                "properties": {
                    "channel": {"type": "string", "description": "Slack channel ID"},
                    "thread_ts": {
                        "type": "string",
                        "description": "Thread timestamp (ts of the parent message)",
                    },
                    "count": {
                        "type": "integer",
                        "default": 20,
                        "description": "Max replies to return",
                    },
                },
                "required": ["channel", "thread_ts"],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="download_slack_bot_file",
            workspace_ingest=True,
            description=(
                "Download a file shared in a Slack channel (the file IDs are listed "
                "under the messages) into the conversation: a download card, its links "
                "and a file_id to forward to any tool that takes a file. Its readable "
                "content comes with the result."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "file_id": {"type": "string", "description": "Slack file ID"},
                },
                "required": ["file_id"],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="upload_slack_bot_file",
            description=(
                "Share a file in a Slack channel as the bot: file_id (any file attached, "
                "generated or downloaded in this conversation, sent byte-for-byte) or "
                "text content."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "channel": {"type": "string", "description": "Slack channel ID"},
                    "file_id": {"type": "string", "description": "A file of this conversation to share as-is"},
                    "content": {"type": "string", "description": "Text to upload as a file (instead of file_id)"},
                    "filename": {"type": "string", "description": "Filename with extension (default: the file's own name)"},
                    "title": {"type": "string", "description": "Optional display title"},
                    "initial_comment": {"type": "string", "description": "Optional message posted with the file"},
                    "thread_ts": {"type": "string", "description": "Optional thread timestamp to share the file in a thread"},
                },
                "required": ["channel"],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="get_slack_bot_user",
            description="Get information about a Slack user (name, email, title, status). Use this to look up details about message senders.",
            input_schema={
                "type": "object",
                "properties": {
                    "user_id": {
                        "type": "string",
                        "description": "Slack user ID (e.g. U01234)",
                    },
                },
                "required": ["user_id"],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="list_slack_bot_users",
            description="List users in the Slack workspace with name, email, title, and status.",
            input_schema={
                "type": "object",
                "properties": {
                    "count": {
                        "type": "integer",
                        "default": 50,
                        "description": "Max users to return",
                    },
                },
                "required": [],
            },
            source=_SOURCE,
            priority=2,
        ),
        # Write
        ToolDefinition(
            name="send_slack_bot_message",
            description="Send a message to a Slack channel as the bot. Use list_slack_bot_channels to discover available channels first.",
            input_schema={
                "type": "object",
                "properties": {
                    "channel": {"type": "string", "description": "Slack channel ID"},
                    "text": {
                        "type": "string",
                        "description": "Message text (supports Slack markdown)",
                    },
                },
                "required": ["channel", "text"],
            },
            source=_SOURCE,
            priority=0,
            # Artifact markers in the text are sent as links (outbound_markers).
            outbound_text=("text",),
        ),
        ToolDefinition(
            name="reply_slack_bot_thread",
            description="Reply to a message thread in a Slack channel as the bot. The reply appears in the thread, not as a new channel message.",
            input_schema={
                "type": "object",
                "properties": {
                    "channel": {"type": "string", "description": "Slack channel ID"},
                    "thread_ts": {
                        "type": "string",
                        "description": "Thread timestamp (ts of the parent message)",
                    },
                    "text": {"type": "string", "description": "Reply text"},
                },
                "required": ["channel", "thread_ts", "text"],
            },
            source=_SOURCE,
            priority=0,
            # Artifact markers in the text are sent as links (outbound_markers).
            outbound_text=("text",),
        ),
        ToolDefinition(
            name="add_slack_bot_reaction",
            description="Add an emoji reaction to a Slack message as the bot.",
            input_schema={
                "type": "object",
                "properties": {
                    "channel": {"type": "string", "description": "Slack channel ID"},
                    "timestamp": {
                        "type": "string",
                        "description": "Message timestamp to react to",
                    },
                    "emoji": {
                        "type": "string",
                        "description": "Emoji name without colons (e.g. 'thumbsup')",
                    },
                },
                "required": ["channel", "timestamp", "emoji"],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="set_slack_bot_channel_topic",
            description="Set the topic of a Slack channel as the bot.",
            input_schema={
                "type": "object",
                "properties": {
                    "channel": {"type": "string", "description": "Slack channel ID"},
                    "topic": {
                        "type": "string",
                        "description": "New channel topic text",
                    },
                },
                "required": ["channel", "topic"],
            },
            source=_SOURCE,
            priority=2,
        ),
        ToolDefinition(
            name="remove_slack_bot_reaction",
            description="Remove an emoji reaction from a Slack message as the bot.",
            input_schema={
                "type": "object",
                "properties": {
                    "channel": {"type": "string", "description": "Slack channel ID"},
                    "timestamp": {
                        "type": "string",
                        "description": "Message timestamp to remove the reaction from",
                    },
                    "emoji": {
                        "type": "string",
                        "description": "Emoji name without colons (e.g. 'thumbsup')",
                    },
                },
                "required": ["channel", "timestamp", "emoji"],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="pin_slack_bot_message",
            description="Pin a message in a Slack channel. Pinned messages are highlighted for easy access by all channel members.",
            input_schema={
                "type": "object",
                "properties": {
                    "channel": {"type": "string", "description": "Slack channel ID"},
                    "timestamp": {
                        "type": "string",
                        "description": "Timestamp (ts) of the message to pin",
                    },
                },
                "required": ["channel", "timestamp"],
            },
            source=_SOURCE,
            priority=2,
        ),
        ToolDefinition(
            name="unpin_slack_bot_message",
            description="Unpin a previously pinned message in a Slack channel.",
            input_schema={
                "type": "object",
                "properties": {
                    "channel": {"type": "string", "description": "Slack channel ID"},
                    "timestamp": {
                        "type": "string",
                        "description": "Timestamp (ts) of the message to unpin",
                    },
                },
                "required": ["channel", "timestamp"],
            },
            source=_SOURCE,
            priority=2,
        ),
        ToolDefinition(
            name="delete_slack_bot_message",
            description="Delete a message from a Slack channel. The bot can only delete its own messages or messages in channels where it has appropriate permissions. Use read_slack_bot_messages to get the timestamp of the message to delete.",
            input_schema={
                "type": "object",
                "properties": {
                    "channel": {"type": "string", "description": "Slack channel ID"},
                    "timestamp": {
                        "type": "string",
                        "description": "Timestamp (ts) of the message to delete",
                    },
                },
                "required": ["channel", "timestamp"],
            },
            source=_SOURCE,
            priority=1,
        ),
    ]
