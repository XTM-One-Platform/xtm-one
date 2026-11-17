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

"""Google Chat Bot channel tools – Google Chat REST API.

These tools let agents send and manage messages **as the Google Chat app**.
Previously Google Chat was only available as a channel adapter for routing
inbound messages; ``CHANNEL_TO_BOT_PROVIDER`` maps ``google_chat`` →
``googlechat_bot`` but no tool builder existed, so agents bound to a Google
Chat bot received zero proactive tools.

The service-account JSON key is stored as ``api_key_enc`` on the
``googlechat_bot`` channel bot and is passed directly by
``integration_tools._get_token``. Token acquisition reuses the channel
adapter's cached JWT-grant exchange (shared process-wide app-token cache).
"""

from __future__ import annotations

import logging
import re
from typing import Any

from app.core.egress import egress_client
from app.integrations.http import is_publish_allowed, safe_request
from app.tools.base import CallableToolExecutor, ToolDefinition

logger = logging.getLogger(__name__)

_PROVIDER = "Google Chat Bot"
_SOURCE = "integration:googlechat_bot"
_CHAT_API = "https://chat.googleapis.com/v1"

# LLM-supplied identifiers are interpolated into Chat API URL paths — restrict
# them to the documented resource-name shapes so a prompt-injected agent can't
# aim the bot's token at arbitrary API paths (e.g. via ../ normalization).
_SPACE_RE = re.compile(r"^spaces/[A-Za-z0-9_-]+$")
_MESSAGE_RE = re.compile(r"^spaces/[A-Za-z0-9_-]+/messages/[A-Za-z0-9_.-]+$")


def _space_resource(space: str) -> str:
    """Normalize a space identifier to the ``spaces/XXXX`` resource name."""
    space = (space or "").strip()
    return space if space.startswith("spaces/") else f"spaces/{space}"


def build_tools(
    token: str,
    executor: CallableToolExecutor,
    **_kwargs: Any,
) -> list[ToolDefinition]:
    """Build Google Chat bot tools using a service-account JSON key."""
    from app.channels.google_chat import GoogleChatChannel

    channel = GoogleChatChannel(token)

    # Publish channel whitelist — when set, restricts sends to these spaces.
    _publish_channels: list[dict] | None = _kwargs.get("publish_channels")
    _allowed_ids: set[str] | None = (
        {ch["id"] for ch in _publish_channels if ch.get("id")}
        if _publish_channels
        else None
    )

    async def _auth_headers() -> dict[str, str] | None:
        access_token = await channel._get_access_token()
        if not access_token:
            return None
        return {"Authorization": f"Bearer {access_token}"}

    # ── Write tools ───────────────────────────────────────────────────────

    async def send_googlechat_bot_message(
        space: str, text: str, thread_name: str = ""
    ) -> str:
        """Send a text message to a Google Chat space as the bot."""
        resource = _space_resource(space)
        if not _SPACE_RE.match(resource):
            return f"Invalid space identifier: {space!r} (expected spaces/XXXX)."
        if not is_publish_allowed(resource, _allowed_ids) and not is_publish_allowed(
            resource.removeprefix("spaces/"), _allowed_ids
        ):
            return (
                f"Access denied: space {space} is not in the publish channels "
                "list for this channel bot."
            )
        headers = await _auth_headers()
        if headers is None:
            return "Failed to acquire a Google Chat access token — check the service account key."

        payload: dict[str, Any] = {"text": text}
        params: dict[str, str] = {}
        if thread_name:
            payload["thread"] = {"name": thread_name}
            params["messageReplyOption"] = "REPLY_MESSAGE_FALLBACK_TO_NEW_THREAD"

        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "post",
                f"{_CHAT_API}/{resource}/messages",
                _PROVIDER,
                "send message",
                headers=headers,
                params=params or None,
                json=payload,
            )
            if err:
                return err
            data = resp.json()
            return f"Message sent to {resource} (name: {data.get('name', '?')})."

    async def list_googlechat_bot_spaces(page_size: int = 50) -> str:
        """List the spaces the Google Chat app is a member of."""
        headers = await _auth_headers()
        if headers is None:
            return "Failed to acquire a Google Chat access token — check the service account key."
        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{_CHAT_API}/spaces",
                _PROVIDER,
                "list spaces",
                headers=headers,
                params={"pageSize": str(min(max(page_size, 1), 100))},
            )
            if err:
                return err
            spaces = resp.json().get("spaces", [])
            if not spaces:
                return "The bot is not a member of any spaces."
            lines = []
            for sp in spaces:
                lines.append(
                    f"- {sp.get('displayName') or '(direct message)'} "
                    f"[{sp.get('spaceType', sp.get('type', '?'))}] — {sp.get('name', '?')}"
                )
            return f"Spaces ({len(spaces)}):\n" + "\n".join(lines)

    async def get_googlechat_bot_space(space: str) -> str:
        """Get details about a Google Chat space."""
        resource = _space_resource(space)
        if not _SPACE_RE.match(resource):
            return f"Invalid space identifier: {space!r} (expected spaces/XXXX)."
        headers = await _auth_headers()
        if headers is None:
            return "Failed to acquire a Google Chat access token — check the service account key."
        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{_CHAT_API}/{resource}",
                _PROVIDER,
                "get space",
                headers=headers,
            )
            if err:
                return err
            sp = resp.json()
            return (
                f"Name: {sp.get('displayName') or '(direct message)'}\n"
                f"Resource: {sp.get('name', '?')}\n"
                f"Type: {sp.get('spaceType', sp.get('type', '?'))}\n"
                f"Threaded: {sp.get('spaceThreadingState', '?')}"
            )

    async def delete_googlechat_bot_message(message_name: str) -> str:
        """Delete a message previously sent by the bot."""
        message_name = (message_name or "").strip()
        if not _MESSAGE_RE.match(message_name):
            return (
                f"Invalid message name: {message_name!r} "
                "(expected spaces/XXX/messages/YYY)."
            )
        # The space component of the message name is subject to the same
        # publish whitelist as sends (deleting is a write to the space).
        space_resource = "/".join(message_name.split("/")[:2])
        if not is_publish_allowed(
            space_resource, _allowed_ids
        ) and not is_publish_allowed(
            space_resource.removeprefix("spaces/"), _allowed_ids
        ):
            return (
                f"Access denied: space {space_resource} is not in the publish "
                "channels list for this channel bot."
            )
        headers = await _auth_headers()
        if headers is None:
            return "Failed to acquire a Google Chat access token — check the service account key."
        async with egress_client(timeout=30) as client:
            _, err = await safe_request(
                client,
                "delete",
                f"{_CHAT_API}/{message_name}",
                _PROVIDER,
                "delete message",
                headers=headers,
            )
            if err:
                return err
            return f"Message {message_name} deleted."

    # ── Register handlers ─────────────────────────────────────────────────

    executor.register("send_googlechat_bot_message", send_googlechat_bot_message)
    executor.register("list_googlechat_bot_spaces", list_googlechat_bot_spaces)
    executor.register("get_googlechat_bot_space", get_googlechat_bot_space)
    executor.register("delete_googlechat_bot_message", delete_googlechat_bot_message)

    return [
        ToolDefinition(
            name="send_googlechat_bot_message",
            description=(
                "Send a text message to a Google Chat space as the bot. "
                "Use list_googlechat_bot_spaces to discover space IDs. "
                "Optionally reply within an existing thread. Text only: a Chat app "
                "cannot attach a file (Google's media.upload requires user authentication)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "space": {
                        "type": "string",
                        "description": "Space ID or full resource name (spaces/XXXX)",
                    },
                    "text": {
                        "type": "string",
                        "description": "Message text (Google Chat formatting: *bold*, _italic_)",
                    },
                    "thread_name": {
                        "type": "string",
                        "default": "",
                        "description": "Optional thread resource name to reply in (spaces/XXX/threads/YYY)",
                    },
                },
                "required": ["space", "text"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="list_googlechat_bot_spaces",
            description="List the Google Chat spaces and direct-message conversations the bot is a member of.",
            input_schema={
                "type": "object",
                "properties": {
                    "page_size": {
                        "type": "integer",
                        "default": 50,
                        "description": "Maximum number of spaces to return (1-100)",
                    },
                },
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="get_googlechat_bot_space",
            description="Get details about a Google Chat space (name, type, threading state).",
            input_schema={
                "type": "object",
                "properties": {
                    "space": {
                        "type": "string",
                        "description": "Space ID or full resource name (spaces/XXXX)",
                    },
                },
                "required": ["space"],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="delete_googlechat_bot_message",
            description="Delete a message previously sent by the bot (full message resource name required).",
            input_schema={
                "type": "object",
                "properties": {
                    "message_name": {
                        "type": "string",
                        "description": "Full message resource name (spaces/XXX/messages/YYY)",
                    },
                },
                "required": ["message_name"],
            },
            source=_SOURCE,
            priority=2,
        ),
    ]
