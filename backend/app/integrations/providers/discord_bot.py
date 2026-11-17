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

"""Discord Bot channel tools – Discord REST API with bot token.

These tools let agents send messages, add reactions, pin messages, and
manage threads **as the Discord bot**.

The bot token is stored as ``api_key_enc`` on the ``discord_bot`` channel
bot and is passed directly by ``integration_tools._get_channel_bot_token``.
"""

from __future__ import annotations

import json
import urllib.parse
from typing import Any

from app.core.egress import egress_client
from app.integrations.file_delivery import deliver_file, read_pipe_file, uploaded_file_answer
from app.integrations.http import (
    format_entity_list,
    is_publish_allowed,
    safe_request,
)
from app.tools.base import CallableToolExecutor, FileContext, ToolDefinition, ToolResult

_PROVIDER = "Discord Bot"
_SOURCE = "integration:discord_bot"
_BASE_URL = "https://discord.com/api/v10"
# Discord's upload limit for a bot without server boosts.
_MAX_DISCORD_FILE_BYTES = 10 * 1024 * 1024
_DISCORD_CDN_HOSTS = frozenset({"cdn.discordapp.com", "media.discordapp.net"})


def _is_discord_cdn(url: str) -> bool:
    try:
        parsed = urllib.parse.urlparse(url)
    except ValueError:
        return False
    return parsed.scheme == "https" and (parsed.hostname or "").lower() in _DISCORD_CDN_HOSTS


def _attachments_suffix(message: dict) -> str:
    """The attachments of a Discord message, one per line; ``""`` when none."""
    lines = []
    for a in message.get("attachments") or []:
        if isinstance(a, dict) and a.get("id"):
            lines.append(
                f"\n    attachment: {a.get('filename', 'file')} (attachment_id: {a['id']}, "
                f"{a.get('content_type', '?')}, {a.get('size', 0)} bytes) - {a.get('url', '')}"
            )
    return "".join(lines)


def build_tools(
    token: str,
    executor: CallableToolExecutor,
    **_kwargs: Any,
) -> list[ToolDefinition]:
    """Build Discord Bot channel tools using a bot token."""
    headers = {
        "Authorization": f"Bot {token}",
        "Content-Type": "application/json",
    }

    # Publish channel whitelist — restricts writes.  Read tools use
    # ``read_channels`` when provided (blocked-publish case — see
    # ``build_channel_tools``), falling back to the publish whitelist.
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

    # ── Read tools ─────────────────────────────────────────────────────────

    async def list_discord_bot_channels(guild_id: str) -> str:
        """List channels in a Discord guild (server)."""
        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{_BASE_URL}/guilds/{guild_id}/channels",
                _PROVIDER,
                "list channels",
                headers=headers,
            )
            if err:
                return err
            channels = resp.json()
            # Filter to text channels (type 0) and announcement (type 5)
            text_channels = [c for c in channels if c.get("type") in (0, 5)][:50]
            return format_entity_list(
                text_channels,
                lambda c: (
                    f"#{c.get('name', '?')} (ID: {c.get('id', '?')})"
                    + (f" — {(c.get('topic') or '')[:60]}" if c.get("topic") else "")
                ),
                noun="text channel",
                empty_msg="No text channels found.",
            )

    async def read_discord_bot_messages(channel_id: str, count: int = 15) -> str:
        """Read recent messages from a Discord channel."""
        if not is_publish_allowed(channel_id, _read_allowed_ids):
            return f"Access denied: channel {channel_id} is not in the allowed channels list."
        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{_BASE_URL}/channels/{channel_id}/messages",
                _PROVIDER,
                "read messages",
                headers=headers,
                params={"limit": min(count, 50)},
            )
            if err:
                return err
            messages = resp.json()
            return format_entity_list(
                list(reversed(messages)),
                lambda m: (
                    f"[{m.get('id', '?')}] {m.get('author', {}).get('username', '?')}: {m.get('content', '')[:200]}"
                    + _attachments_suffix(m)
                ),
                noun="message",
                empty_msg="No messages found.",
            )

    # ── File tools ─────────────────────────────────────────────────────────

    async def download_discord_bot_attachment(
        channel_id: str, message_id: str, attachment_id: str
    ) -> str | ToolResult:
        """Download a message attachment into the conversation (stored file + links + file_id)."""
        if not is_publish_allowed(channel_id, _read_allowed_ids):
            return f"Access denied: channel {channel_id} is not in the allowed channels list."
        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{_BASE_URL}/channels/{channel_id}/messages/{message_id}",
                _PROVIDER,
                "read message",
                headers=headers,
            )
        if err:
            return err
        attachment = next(
            (
                a
                for a in resp.json().get("attachments") or []
                if isinstance(a, dict) and str(a.get("id")) == str(attachment_id).strip()
            ),
            None,
        )
        if attachment is None:
            return ToolResult(
                tool_call_id="",
                content=f"Error: message {message_id} has no attachment {attachment_id}.",
                is_error=True,
            )
        url = str(attachment.get("url") or "")
        if not _is_discord_cdn(url):
            return ToolResult(
                tool_call_id="",
                content="Error: Discord gave no CDN URL for this attachment.",
                is_error=True,
            )
        if int(attachment.get("size") or 0) > _MAX_DISCORD_FILE_BYTES:
            return (
                f"Attachment {attachment.get('filename')} is over the "
                f"{_MAX_DISCORD_FILE_BYTES // 1024 // 1024} MB download limit. Download URL: {url}"
            )
        # The CDN URL is signed; the bot token is never sent there.
        async with egress_client(timeout=120, follow_redirects=True) as client:
            file_resp, err = await safe_request(client, "get", url, _PROVIDER, "download attachment")
        if err:
            return ToolResult(tool_call_id="", content=err, is_error=True)
        return await deliver_file(
            data=file_resp.content,
            filename=attachment.get("filename") or f"discord-{attachment_id}",
            content_type=attachment.get("content_type") or "application/octet-stream",
            source="Discord",
            persist_file_fn=persist_file_fn,
            links=[("Download URL", url), ("Proxy URL", attachment.get("proxy_url"))],
        )

    async def send_discord_bot_file(
        channel_id: str,
        file_id: str,
        text: str = "",
        filename: str = "",
        file_context: FileContext | None = None,
    ) -> str | ToolResult:
        """Post a file of the run to a Discord channel, with an optional message."""
        if not is_publish_allowed(channel_id, _allowed_ids):
            return f"Access denied: channel {channel_id} is not in the publish channels list."
        piped = await read_pipe_file(
            file_context, file_id, max_bytes=_MAX_DISCORD_FILE_BYTES, target="Discord"
        )
        if isinstance(piped, str):
            return ToolResult(tool_call_id="", content=piped, is_error=True)
        data, piped_name, mime = piped
        name = (filename or "").strip() or piped_name
        payload = {"content": text[:2000], "attachments": [{"id": 0, "filename": name}]}
        async with egress_client(timeout=60) as client:
            resp, err = await safe_request(
                client,
                "post",
                f"{_BASE_URL}/channels/{channel_id}/messages",
                _PROVIDER,
                "send file",
                headers={"Authorization": headers["Authorization"]},
                data={"payload_json": json.dumps(payload)},
                files={"files[0]": (name, data, mime)},
            )
        if err:
            return err
        sent = resp.json()
        urls = [a.get("url") for a in sent.get("attachments") or [] if isinstance(a, dict) and a.get("url")]
        # The name and the CDN URL that carries it go in the block.
        return uploaded_file_answer(
            f"File {file_id.strip()} sent (message ID: {sent.get('id', '?')}).",
            target=_PROVIDER,
            filename=name,
            links=[("Download URL", urls[0] if urls else None)],
        )

    # ── Write tools ────────────────────────────────────────────────────────

    async def send_discord_bot_message(channel_id: str, text: str) -> str:
        """Send a message to a Discord channel."""
        if not is_publish_allowed(channel_id, _allowed_ids):
            return f"Access denied: channel {channel_id} is not in the publish channels list."
        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "post",
                f"{_BASE_URL}/channels/{channel_id}/messages",
                _PROVIDER,
                "send message",
                headers=headers,
                json={"content": text[:2000]},
            )
            if err:
                return err
            data = resp.json()
            return f"Message sent (ID: {data.get('id', '?')})."

    async def add_discord_bot_reaction(
        channel_id: str, message_id: str, emoji: str
    ) -> str:
        """Add a reaction to a Discord message.

        Use URL-encoded emoji for custom ones, or plain Unicode for standard.
        """
        if not is_publish_allowed(channel_id, _allowed_ids):
            return f"Access denied: channel {channel_id} is not in the publish channels list."
        encoded_emoji = urllib.parse.quote(emoji)
        async with egress_client(timeout=30) as client:
            _, err = await safe_request(
                client,
                "put",
                f"{_BASE_URL}/channels/{channel_id}/messages/{message_id}/reactions/{encoded_emoji}/@me",
                _PROVIDER,
                "add reaction",
                headers=headers,
            )
            if err:
                return err
            return f"Reaction {emoji} added to message {message_id}."

    async def pin_discord_bot_message(channel_id: str, message_id: str) -> str:
        """Pin a message in a Discord channel."""
        if not is_publish_allowed(channel_id, _allowed_ids):
            return f"Access denied: channel {channel_id} is not in the publish channels list."
        async with egress_client(timeout=30) as client:
            _, err = await safe_request(
                client,
                "put",
                f"{_BASE_URL}/channels/{channel_id}/pins/{message_id}",
                _PROVIDER,
                "pin message",
                headers=headers,
            )
            if err:
                return err
            return f"Message {message_id} pinned in channel {channel_id}."

    async def unpin_discord_bot_message(channel_id: str, message_id: str) -> str:
        """Unpin a message in a Discord channel."""
        if not is_publish_allowed(channel_id, _allowed_ids):
            return f"Access denied: channel {channel_id} is not in the publish channels list."
        async with egress_client(timeout=30) as client:
            _, err = await safe_request(
                client,
                "delete",
                f"{_BASE_URL}/channels/{channel_id}/pins/{message_id}",
                _PROVIDER,
                "unpin message",
                headers=headers,
            )
            if err:
                return err
            return f"Message {message_id} unpinned from channel {channel_id}."

    async def create_discord_bot_thread(
        channel_id: str,
        name: str,
        message_id: str = "",
    ) -> str:
        """Create a thread in a Discord channel.

        If *message_id* is provided, creates a thread from that message.
        Otherwise creates a standalone thread.
        """
        if not is_publish_allowed(channel_id, _allowed_ids):
            return f"Access denied: channel {channel_id} is not in the publish channels list."
        if message_id:
            url = f"{_BASE_URL}/channels/{channel_id}/messages/{message_id}/threads"
            payload: dict[str, Any] = {"name": name[:100]}
        else:
            url = f"{_BASE_URL}/channels/{channel_id}/threads"
            payload = {
                "name": name[:100],
                "type": 11,  # PUBLIC_THREAD
            }
        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "post",
                url,
                _PROVIDER,
                "create thread",
                headers=headers,
                json=payload,
            )
            if err:
                return err
            data = resp.json()
            return f"Thread '{name}' created (ID: {data.get('id', '?')})."

    async def delete_discord_bot_message(channel_id: str, message_id: str) -> str:
        """Delete a message from a Discord channel."""
        if not is_publish_allowed(channel_id, _allowed_ids):
            return f"Access denied: channel {channel_id} is not in the publish channels list."
        async with egress_client(timeout=30) as client:
            _, err = await safe_request(
                client,
                "delete",
                f"{_BASE_URL}/channels/{channel_id}/messages/{message_id}",
                _PROVIDER,
                "delete message",
                headers=headers,
            )
            if err:
                return err
            return f"Message {message_id} deleted from channel {channel_id}."

    # ── Register handlers ─────────────────────────────────────────────────

    executor.register("list_discord_bot_channels", list_discord_bot_channels)
    executor.register("read_discord_bot_messages", read_discord_bot_messages)
    executor.register("download_discord_bot_attachment", download_discord_bot_attachment)
    executor.register("send_discord_bot_message", send_discord_bot_message)
    executor.register("send_discord_bot_file", send_discord_bot_file)
    executor.register("add_discord_bot_reaction", add_discord_bot_reaction)
    executor.register("pin_discord_bot_message", pin_discord_bot_message)
    executor.register("unpin_discord_bot_message", unpin_discord_bot_message)
    executor.register("create_discord_bot_thread", create_discord_bot_thread)
    executor.register("delete_discord_bot_message", delete_discord_bot_message)

    return [
        # Read
        ToolDefinition(
            name="list_discord_bot_channels",
            description="List text channels in a Discord server. Returns channel names, IDs, and topics.",
            input_schema={
                "type": "object",
                "properties": {
                    "guild_id": {
                        "type": "string",
                        "description": "Discord guild (server) ID",
                    },
                },
                "required": ["guild_id"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="download_discord_bot_attachment",
            workspace_ingest=True,
            description=(
                "Download an attachment of a Discord message (the ids are listed under the "
                "messages) into the conversation: a download card, its URL and a file_id to "
                "forward to any tool that takes a file. Its readable content comes with the "
                "result."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "channel_id": {"type": "string", "description": "Discord channel ID"},
                    "message_id": {"type": "string", "description": "Message ID"},
                    "attachment_id": {"type": "string", "description": "Attachment ID"},
                },
                "required": ["channel_id", "message_id", "attachment_id"],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="send_discord_bot_file",
            description=(
                "Post a file of this conversation (attached, generated or downloaded; up to "
                "10 MB) to a Discord channel as the bot, with an optional message."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "channel_id": {"type": "string", "description": "Discord channel ID"},
                    "file_id": {"type": "string", "description": "file_id of a file of this conversation"},
                    "text": {"type": "string", "description": "Optional message text (max 2000 characters)"},
                    "filename": {"type": "string", "description": "Optional file name (default: the file's own name)"},
                },
                "required": ["channel_id", "file_id"],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="read_discord_bot_messages",
            description="Read recent messages from a Discord channel. Returns message ID, author, content and attachments.",
            input_schema={
                "type": "object",
                "properties": {
                    "channel_id": {
                        "type": "string",
                        "description": "Discord channel ID",
                    },
                    "count": {
                        "type": "integer",
                        "default": 15,
                        "description": "Max messages to return",
                    },
                },
                "required": ["channel_id"],
            },
            source=_SOURCE,
            priority=0,
        ),
        # Write
        ToolDefinition(
            name="send_discord_bot_message",
            description="Send a message to a Discord channel as the bot (max 2000 characters).",
            input_schema={
                "type": "object",
                "properties": {
                    "channel_id": {
                        "type": "string",
                        "description": "Discord channel ID",
                    },
                    "text": {
                        "type": "string",
                        "description": "Message text (supports Markdown)",
                    },
                },
                "required": ["channel_id", "text"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="add_discord_bot_reaction",
            description="Add an emoji reaction to a Discord message. Use Unicode emoji (e.g. '\U0001f44d') or custom emoji format (name:id).",
            input_schema={
                "type": "object",
                "properties": {
                    "channel_id": {
                        "type": "string",
                        "description": "Discord channel ID",
                    },
                    "message_id": {
                        "type": "string",
                        "description": "Message ID to react to",
                    },
                    "emoji": {
                        "type": "string",
                        "description": "Emoji — Unicode (\U0001f44d) or custom (name:id)",
                    },
                },
                "required": ["channel_id", "message_id", "emoji"],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="pin_discord_bot_message",
            description="Pin a message in a Discord channel. Channels can have up to 50 pinned messages.",
            input_schema={
                "type": "object",
                "properties": {
                    "channel_id": {
                        "type": "string",
                        "description": "Discord channel ID",
                    },
                    "message_id": {
                        "type": "string",
                        "description": "Message ID to pin",
                    },
                },
                "required": ["channel_id", "message_id"],
            },
            source=_SOURCE,
            priority=2,
        ),
        ToolDefinition(
            name="unpin_discord_bot_message",
            description="Unpin a previously pinned message in a Discord channel.",
            input_schema={
                "type": "object",
                "properties": {
                    "channel_id": {
                        "type": "string",
                        "description": "Discord channel ID",
                    },
                    "message_id": {
                        "type": "string",
                        "description": "Message ID to unpin",
                    },
                },
                "required": ["channel_id", "message_id"],
            },
            source=_SOURCE,
            priority=2,
        ),
        ToolDefinition(
            name="create_discord_bot_thread",
            description="Create a thread in a Discord channel. Optionally attach to an existing message.",
            input_schema={
                "type": "object",
                "properties": {
                    "channel_id": {
                        "type": "string",
                        "description": "Discord channel ID",
                    },
                    "name": {
                        "type": "string",
                        "description": "Thread name (max 100 characters)",
                    },
                    "message_id": {
                        "type": "string",
                        "default": "",
                        "description": "Optional: message ID to create thread from",
                    },
                },
                "required": ["channel_id", "name"],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="delete_discord_bot_message",
            description="Delete a message from a Discord channel. The bot can delete its own messages, or messages in channels where it has Manage Messages permission.",
            input_schema={
                "type": "object",
                "properties": {
                    "channel_id": {
                        "type": "string",
                        "description": "Discord channel ID",
                    },
                    "message_id": {
                        "type": "string",
                        "description": "Message ID to delete",
                    },
                },
                "required": ["channel_id", "message_id"],
            },
            source=_SOURCE,
            priority=1,
        ),
    ]
