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

"""Telegram Bot channel tools – Telegram Bot API.

These tools let agents send messages, photos, and manage chats **as the
Telegram bot**.  Previously Telegram was only available as a channel
adapter for routing inbound messages; these tools enable proactive
outbound messaging in assignments.

The bot token is stored as ``api_key_enc`` on the ``telegram_bot``
integration and is passed directly by ``integration_tools._get_token``.
"""

from __future__ import annotations

import logging
from typing import Any

from app.core.egress import egress_client
from app.integrations.file_delivery import read_pipe_file
from app.integrations.http import (
    is_publish_allowed,
    safe_request,
)
from app.tools.base import CallableToolExecutor, FileContext, ToolDefinition, ToolResult

logger = logging.getLogger(__name__)

_PROVIDER = "Telegram Bot"
_SOURCE = "integration:telegram_bot"
# Bot API upload limits: 10 MB for a photo, 50 MB for any other file.
_MAX_PHOTO_BYTES = 10 * 1024 * 1024
_MAX_DOCUMENT_BYTES = 50 * 1024 * 1024


def _split_message(text: str, max_length: int = 4096) -> list[str]:
    """Split a long message into chunks respecting Telegram's char limit."""
    if len(text) <= max_length:
        return [text]
    chunks: list[str] = []
    while text:
        if len(text) <= max_length:
            chunks.append(text)
            break
        split_idx = text.rfind("\n", 0, max_length)
        if split_idx == -1:
            split_idx = max_length
        chunks.append(text[:split_idx])
        text = text[split_idx:].lstrip("\n")
    return chunks


def build_tools(
    token: str,
    executor: CallableToolExecutor,
    **_kwargs: Any,
) -> list[ToolDefinition]:
    """Build Telegram Bot channel tools using a bot token."""
    base_url = f"https://api.telegram.org/bot{token}"

    # Publish channel whitelist — when set, restricts writes to these chat
    # IDs.  Read tools use ``read_channels`` when provided (blocked-publish
    # case — see ``build_channel_tools``), falling back to the publish
    # whitelist.
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

    # ── Write tools ───────────────────────────────────────────────────────

    async def send_telegram_bot_message(
        chat_id: str, text: str, parse_mode: str = "Markdown"
    ) -> str:
        """Send a text message to a Telegram chat.

        *chat_id* can be a numeric user/group chat ID, or a ``@channel``
        username for public channels.
        """
        if not is_publish_allowed(chat_id, _allowed_ids):
            return f"Access denied: chat {chat_id} is not in the publish channels list for this channel bot."
        chunks = _split_message(text)
        last_msg_id = None

        async with egress_client(timeout=30) as client:
            for chunk in chunks:
                resp, err = await safe_request(
                    client,
                    "post",
                    f"{base_url}/sendMessage",
                    _PROVIDER,
                    "send message",
                    json={
                        "chat_id": chat_id,
                        "text": chunk,
                        "parse_mode": parse_mode,
                    },
                )
                if err:
                    # Retry without parse_mode in case of formatting errors
                    resp, err = await safe_request(
                        client,
                        "post",
                        f"{base_url}/sendMessage",
                        _PROVIDER,
                        "send message",
                        json={"chat_id": chat_id, "text": chunk},
                    )
                    if err:
                        return err
                data = resp.json()
                last_msg_id = str(data.get("result", {}).get("message_id", ""))

        return f"Message sent to chat {chat_id} (message_id: {last_msg_id})."

    async def _send_media(
        method: str,
        field: str,
        chat_id: str,
        *,
        url: str,
        file_id: str,
        caption: str,
        filename: str,
        max_bytes: int,
        file_context: FileContext | None,
    ) -> str | ToolResult:
        """``sendPhoto`` / ``sendDocument``: a URL Telegram fetches, or a file of the run uploaded."""
        if not is_publish_allowed(chat_id, _allowed_ids):
            return f"Access denied: chat {chat_id} is not in the publish channels list for this channel bot."
        if bool((url or "").strip()) == bool((file_id or "").strip()):
            return ToolResult(
                tool_call_id="",
                content=(
                    f"Error: provide exactly one of {field}_url (a public URL) or file_id "
                    "(a file attached, generated or downloaded in this conversation)."
                ),
                is_error=True,
            )
        fields: dict[str, Any] = {"chat_id": chat_id}
        if caption:
            fields["caption"] = caption[:1024]  # Telegram caption limit
            fields["parse_mode"] = "Markdown"
        async with egress_client(timeout=60) as client:
            if url.strip():
                resp, err = await safe_request(
                    client, "post", f"{base_url}/{method}", _PROVIDER, f"send {field}",
                    json={**fields, field: url.strip()},
                )
            else:
                piped = await read_pipe_file(
                    file_context, file_id, max_bytes=max_bytes, target="Telegram"
                )
                if isinstance(piped, str):
                    return ToolResult(tool_call_id="", content=piped, is_error=True)
                data, piped_name, mime = piped
                resp, err = await safe_request(
                    client, "post", f"{base_url}/{method}", _PROVIDER, f"send {field}",
                    data={k: str(v) for k, v in fields.items()},
                    files={field: ((filename or "").strip() or piped_name, data, mime)},
                )
        if err:
            return err
        msg_id = str((resp.json().get("result") or {}).get("message_id", ""))
        return f"{field.title()} sent to chat {chat_id} (message_id: {msg_id})."

    async def send_telegram_bot_photo(
        chat_id: str,
        photo_url: str = "",
        caption: str = "",
        file_id: str = "",
        file_context: FileContext | None = None,
    ) -> str | ToolResult:
        """Send a photo to a Telegram chat: by URL, or a file of the run by file_id."""
        return await _send_media(
            "sendPhoto", "photo", chat_id, url=photo_url, file_id=file_id, caption=caption,
            filename="", max_bytes=_MAX_PHOTO_BYTES, file_context=file_context,
        )

    async def send_telegram_bot_document(
        chat_id: str,
        file_id: str = "",
        document_url: str = "",
        caption: str = "",
        filename: str = "",
        file_context: FileContext | None = None,
    ) -> str | ToolResult:
        """Send any file (PDF, spreadsheet, archive...) to a Telegram chat."""
        return await _send_media(
            "sendDocument", "document", chat_id, url=document_url, file_id=file_id,
            caption=caption, filename=filename, max_bytes=_MAX_DOCUMENT_BYTES,
            file_context=file_context,
        )

    async def edit_telegram_bot_message(
        chat_id: str, message_id: str, text: str
    ) -> str:
        """Edit a previously sent message."""
        if not is_publish_allowed(chat_id, _allowed_ids):
            return f"Access denied: chat {chat_id} is not in the publish channels list for this channel bot."
        async with egress_client(timeout=30) as client:
            _, err = await safe_request(
                client,
                "post",
                f"{base_url}/editMessageText",
                _PROVIDER,
                "edit message",
                json={
                    "chat_id": chat_id,
                    "message_id": int(message_id),
                    "text": text[:4096],
                    "parse_mode": "Markdown",
                },
            )
            if not err:
                return f"Message {message_id} edited in chat {chat_id}."
            # Retry without parse_mode
            _, err = await safe_request(
                client,
                "post",
                f"{base_url}/editMessageText",
                _PROVIDER,
                "edit message",
                json={
                    "chat_id": chat_id,
                    "message_id": int(message_id),
                    "text": text[:4096],
                },
            )
            if err:
                return err
            return f"Message {message_id} edited in chat {chat_id}."

    async def get_telegram_bot_chat_info(chat_id: str) -> str:
        """Get information about a Telegram chat or group."""
        if not is_publish_allowed(chat_id, _read_allowed_ids):
            return f"Access denied: chat {chat_id} is not in the allowed channels list for this channel bot."
        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "post",
                f"{base_url}/getChat",
                _PROVIDER,
                "get chat info",
                json={"chat_id": chat_id},
            )
            if err:
                return err
            data = resp.json()
            chat = data.get("result", {})
            chat_type = chat.get("type", "?")
            title = chat.get("title") or chat.get("first_name", "?")
            username = chat.get("username", "")
            description = (chat.get("description") or "")[:200]
            member_count = chat.get("member_count")

            lines = [
                f"Type: {chat_type}",
                f"Title: {title}",
            ]
            if username:
                lines.append(f"Username: @{username}")
            if description:
                lines.append(f"Description: {description}")
            if member_count is not None:
                lines.append(f"Members: {member_count}")
            lines.append(f"Chat ID: {chat.get('id', chat_id)}")
            return "\n".join(lines)

    async def pin_telegram_bot_message(chat_id: str, message_id: str) -> str:
        """Pin a message in a Telegram chat."""
        if not is_publish_allowed(chat_id, _allowed_ids):
            return f"Access denied: chat {chat_id} is not in the publish channels list for this channel bot."
        async with egress_client(timeout=30) as client:
            _, err = await safe_request(
                client,
                "post",
                f"{base_url}/pinChatMessage",
                _PROVIDER,
                "pin message",
                json={"chat_id": chat_id, "message_id": int(message_id)},
            )
            if err:
                return err
            return f"Message {message_id} pinned in chat {chat_id}."

    async def unpin_telegram_bot_message(chat_id: str, message_id: str) -> str:
        """Unpin a message in a Telegram chat."""
        if not is_publish_allowed(chat_id, _allowed_ids):
            return f"Access denied: chat {chat_id} is not in the publish channels list for this channel bot."
        async with egress_client(timeout=30) as client:
            _, err = await safe_request(
                client,
                "post",
                f"{base_url}/unpinChatMessage",
                _PROVIDER,
                "unpin message",
                json={"chat_id": chat_id, "message_id": int(message_id)},
            )
            if err:
                return err
            return f"Message {message_id} unpinned in chat {chat_id}."

    async def forward_telegram_bot_message(
        from_chat_id: str,
        to_chat_id: str,
        message_id: str,
    ) -> str:
        """Forward a message from one Telegram chat to another."""
        if not is_publish_allowed(to_chat_id, _allowed_ids):
            return f"Access denied: chat {to_chat_id} is not in the publish channels list for this channel bot."
        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "post",
                f"{base_url}/forwardMessage",
                _PROVIDER,
                "forward message",
                json={
                    "chat_id": to_chat_id,
                    "from_chat_id": from_chat_id,
                    "message_id": int(message_id),
                },
            )
            if err:
                return err
            data = resp.json()
            new_id = str(data.get("result", {}).get("message_id", ""))
            return f"Message forwarded to {to_chat_id} (new message_id: {new_id})."

    async def delete_telegram_bot_message(chat_id: str, message_id: str) -> str:
        """Delete a message from a Telegram chat."""
        if not is_publish_allowed(chat_id, _allowed_ids):
            return f"Access denied: chat {chat_id} is not in the publish channels list for this channel bot."
        async with egress_client(timeout=30) as client:
            _, err = await safe_request(
                client,
                "post",
                f"{base_url}/deleteMessage",
                _PROVIDER,
                "delete message",
                json={"chat_id": chat_id, "message_id": int(message_id)},
            )
            if err:
                return err
            return f"Message {message_id} deleted from chat {chat_id}."

    async def create_telegram_bot_poll(
        chat_id: str,
        question: str,
        options: str,
    ) -> str:
        """Create a poll in a Telegram chat.

        *options* is a comma-separated list of answer options (2–10 items).
        """
        if not is_publish_allowed(chat_id, _allowed_ids):
            return f"Access denied: chat {chat_id} is not in the publish channels list for this channel bot."
        option_list = [o.strip() for o in options.split(",") if o.strip()]
        if len(option_list) < 2:
            return "Polls require at least 2 options (comma-separated)."
        if len(option_list) > 10:
            return "Telegram polls support a maximum of 10 options."
        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "post",
                f"{base_url}/sendPoll",
                _PROVIDER,
                "create poll",
                json={
                    "chat_id": chat_id,
                    "question": question[:300],
                    "options": option_list,
                    "is_anonymous": True,
                },
            )
            if err:
                return err
            data = resp.json()
            msg_id = str(data.get("result", {}).get("message_id", ""))
            return f"Poll created in chat {chat_id} (message_id: {msg_id})."

    # ── Register handlers ─────────────────────────────────────────────────

    executor.register("send_telegram_bot_message", send_telegram_bot_message)
    executor.register("send_telegram_bot_photo", send_telegram_bot_photo)
    executor.register("send_telegram_bot_document", send_telegram_bot_document)
    executor.register("edit_telegram_bot_message", edit_telegram_bot_message)
    executor.register("get_telegram_bot_chat_info", get_telegram_bot_chat_info)
    executor.register("pin_telegram_bot_message", pin_telegram_bot_message)
    executor.register("unpin_telegram_bot_message", unpin_telegram_bot_message)
    executor.register("forward_telegram_bot_message", forward_telegram_bot_message)
    executor.register("delete_telegram_bot_message", delete_telegram_bot_message)
    executor.register("create_telegram_bot_poll", create_telegram_bot_poll)

    return [
        ToolDefinition(
            name="send_telegram_bot_message",
            description="Send a text message to a Telegram chat as the bot. The chat_id can be a numeric user/group chat ID or a @channel username for public channels. Long messages are automatically split.",
            input_schema={
                "type": "object",
                "properties": {
                    "chat_id": {
                        "type": "string",
                        "description": "Telegram chat ID (numeric) or @channel username",
                    },
                    "text": {
                        "type": "string",
                        "description": "Message text (supports Markdown formatting)",
                    },
                    "parse_mode": {
                        "type": "string",
                        "enum": ["Markdown", "HTML", "MarkdownV2"],
                        "default": "Markdown",
                        "description": "Text formatting mode",
                    },
                },
                "required": ["chat_id", "text"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="send_telegram_bot_photo",
            description=(
                "Send a photo to a Telegram chat by URL, or an image of this conversation "
                "by file_id (uploaded byte-for-byte, up to 10 MB), with an optional caption."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "chat_id": {
                        "type": "string",
                        "description": "Telegram chat ID or @channel username",
                    },
                    "photo_url": {
                        "type": "string",
                        "description": "URL of the photo to send (instead of file_id)",
                    },
                    "file_id": {
                        "type": "string",
                        "description": "An image of this conversation (attached, generated or downloaded) to upload",
                    },
                    "caption": {
                        "type": "string",
                        "default": "",
                        "description": "Optional photo caption (max 1024 characters, supports Markdown)",
                    },
                },
                "required": ["chat_id"],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="send_telegram_bot_document",
            description=(
                "Send a file to a Telegram chat as the bot: any file of this conversation "
                "by file_id (a PDF, a spreadsheet, an archive... uploaded byte-for-byte, up "
                "to 50 MB), or a public document_url, with an optional caption."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "chat_id": {"type": "string", "description": "Telegram chat ID or @channel username"},
                    "file_id": {"type": "string", "description": "A file of this conversation to upload"},
                    "document_url": {"type": "string", "description": "Public URL of the file (instead of file_id)"},
                    "caption": {"type": "string", "default": "", "description": "Optional caption (max 1024 characters)"},
                    "filename": {"type": "string", "default": "", "description": "Optional file name shown in Telegram"},
                },
                "required": ["chat_id"],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="edit_telegram_bot_message",
            description="Edit a previously sent message in a Telegram chat. Only messages sent by the bot can be edited.",
            input_schema={
                "type": "object",
                "properties": {
                    "chat_id": {
                        "type": "string",
                        "description": "Telegram chat ID",
                    },
                    "message_id": {
                        "type": "string",
                        "description": "ID of the message to edit",
                    },
                    "text": {
                        "type": "string",
                        "description": "New message text",
                    },
                },
                "required": ["chat_id", "message_id", "text"],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="get_telegram_bot_chat_info",
            description="Get information about a Telegram chat, group, or channel. Returns type, title, username, description, and member count.",
            input_schema={
                "type": "object",
                "properties": {
                    "chat_id": {
                        "type": "string",
                        "description": "Telegram chat ID or @channel username",
                    },
                },
                "required": ["chat_id"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="pin_telegram_bot_message",
            description="Pin a message in a Telegram chat. The bot must be an admin in the chat to pin messages.",
            input_schema={
                "type": "object",
                "properties": {
                    "chat_id": {"type": "string", "description": "Telegram chat ID"},
                    "message_id": {
                        "type": "string",
                        "description": "ID of the message to pin",
                    },
                },
                "required": ["chat_id", "message_id"],
            },
            source=_SOURCE,
            priority=2,
        ),
        ToolDefinition(
            name="unpin_telegram_bot_message",
            description="Unpin a previously pinned message in a Telegram chat.",
            input_schema={
                "type": "object",
                "properties": {
                    "chat_id": {"type": "string", "description": "Telegram chat ID"},
                    "message_id": {
                        "type": "string",
                        "description": "ID of the message to unpin",
                    },
                },
                "required": ["chat_id", "message_id"],
            },
            source=_SOURCE,
            priority=2,
        ),
        ToolDefinition(
            name="forward_telegram_bot_message",
            description="Forward a message from one Telegram chat to another.",
            input_schema={
                "type": "object",
                "properties": {
                    "from_chat_id": {"type": "string", "description": "Source chat ID"},
                    "to_chat_id": {
                        "type": "string",
                        "description": "Destination chat ID",
                    },
                    "message_id": {
                        "type": "string",
                        "description": "ID of the message to forward",
                    },
                },
                "required": ["from_chat_id", "to_chat_id", "message_id"],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="delete_telegram_bot_message",
            description="Delete a message from a Telegram chat. The bot can only delete its own messages, or messages in groups/channels where it has admin rights.",
            input_schema={
                "type": "object",
                "properties": {
                    "chat_id": {"type": "string", "description": "Telegram chat ID"},
                    "message_id": {
                        "type": "string",
                        "description": "ID of the message to delete",
                    },
                },
                "required": ["chat_id", "message_id"],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="create_telegram_bot_poll",
            description="Create a poll in a Telegram chat. Provide 2-10 answer options as a comma-separated string.",
            input_schema={
                "type": "object",
                "properties": {
                    "chat_id": {"type": "string", "description": "Telegram chat ID"},
                    "question": {
                        "type": "string",
                        "description": "Poll question (max 300 characters)",
                    },
                    "options": {
                        "type": "string",
                        "description": "Comma-separated answer options (2-10 items)",
                    },
                },
                "required": ["chat_id", "question", "options"],
            },
            source=_SOURCE,
            priority=2,
        ),
    ]
