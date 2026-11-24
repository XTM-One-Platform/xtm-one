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

"""WhatsApp Bot channel tools – Meta Cloud API.

These tools let agents send WhatsApp messages **as the bot**.  Previously
WhatsApp was only available as a channel adapter for routing inbound
messages; these tools enable proactive outbound messaging in assignments.

Credential format:  ``phone_number_id||access_token``  (passed by
``integration_tools._get_token``).
"""

from __future__ import annotations

import logging
import mimetypes
from typing import Any
from urllib.parse import urlparse

from app.core.egress import egress_client
from app.integrations.file_delivery import deliver_file, read_pipe_file, uploaded_file_answer
from app.integrations.http import (
    is_publish_allowed,
    parse_compound_token,
    safe_request,
)
from app.services.outbound_links import markdown_links_as_plain_urls
from app.tools.base import CallableToolExecutor, FileContext, ToolDefinition, ToolResult

logger = logging.getLogger(__name__)

_PROVIDER = "WhatsApp Bot"
_SOURCE = "integration:whatsapp_bot"
_BASE_URL = "https://graph.facebook.com/v19.0"
# The Cloud API's largest media (a document).
_MAX_MEDIA_BYTES = 100 * 1024 * 1024
_META_MEDIA_HOST_SUFFIXES = ("fbsbx.com", "facebook.com", "whatsapp.net", "fbcdn.net")


def _is_meta_media_host(url: str) -> bool:
    """True for an HTTPS URL on a Meta host, the only place the access token is sent."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    host = (parsed.hostname or "").lower()
    return parsed.scheme == "https" and any(
        host == suffix or host.endswith("." + suffix) for suffix in _META_MEDIA_HOST_SUFFIXES
    )


def _media_kind(mime: str) -> str:
    """The WhatsApp message type a file is sent as."""
    mime = (mime or "").lower()
    if mime in ("image/jpeg", "image/png"):
        return "image"
    if mime in ("video/mp4", "video/3gpp"):
        return "video"
    if mime.startswith("audio/"):
        return "audio"
    return "document"


def build_tools(
    token: str,
    executor: CallableToolExecutor,
    **_kwargs: Any,
) -> list[ToolDefinition]:
    """Build WhatsApp Bot channel tools.

    *token* has the form ``phone_number_id||access_token``.
    """
    phone_number_id, access_token = parse_compound_token(token, 2)
    if not phone_number_id or not access_token:
        logger.warning(
            "whatsapp_bot token format invalid — expected phone_number_id||access_token"
        )
        return []
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }

    # Publish channel whitelist — when set, restricts operations to these phone numbers.
    _publish_channels: list[dict] | None = _kwargs.get("publish_channels")
    _allowed_ids: set[str] | None = (
        {ch["id"] for ch in _publish_channels if ch.get("id")}
        if _publish_channels
        else None
    )
    # Set when the agent/bot publish intersection is empty — writes that are
    # not keyed by a recipient (mark-read, react) must fail closed too.
    _publish_blocked: bool = bool(_kwargs.get("publish_blocked"))
    persist_file_fn = _kwargs.get("persist_file_fn")

    # ── Write tools ───────────────────────────────────────────────────────

    async def send_whatsapp_bot_message(to: str, text: str = "", decision: str = "") -> str:
        """Send a text message to a WhatsApp number, or a decision as reply buttons.

        *to* should be the recipient phone number in international format
        (e.g. ``14155552671``). *decision* names a human gate (``run:<uuid>``,
        ``work_item:<uuid>`` ...): it is sent as the decision's buttons or
        list, answered in place (#842, S12), and *text* is not used.
        """
        if not is_publish_allowed(to, _allowed_ids):
            return f"Access denied: recipient {to} is not in the publish contacts list for this channel bot."
        fields: dict[str, Any]
        if decision and decision.strip():
            from app.services.decision_whatsapp import decision_message_for_tool

            rendered = await decision_message_for_tool(decision.strip())
            if isinstance(rendered, str):
                return rendered
            fields = rendered
        elif text:
            # WhatsApp draws no Markdown: a link is sent as its full URL (#10115).
            fields = {"type": "text", "text": {"body": markdown_links_as_plain_urls(text)[:4096]}}
        else:
            return "Error: pass the message text, or a decision to send as reply buttons."
        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "post",
                f"{_BASE_URL}/{phone_number_id}/messages",
                _PROVIDER,
                "send message",
                headers=headers,
                json={"messaging_product": "whatsapp", "to": to, **fields},
            )
            if err:
                return err
            data = resp.json()
            messages = data.get("messages", [])
            msg_id = messages[0]["id"] if messages else "unknown"
            return f"WhatsApp message sent to {to} (message ID: {msg_id})."

    async def send_whatsapp_bot_template(
        to: str,
        template_name: str,
        language_code: str = "en_US",
        body_parameters: str = "",
    ) -> str:
        """Send a template message to a WhatsApp number.

        Template messages are required to initiate conversations outside
        the 24-hour customer service window.  The template must be
        pre-approved in the Meta Business Manager.

        *body_parameters* is a comma-separated list of parameter values
        that fill ``{{1}}``, ``{{2}}``, … placeholders in the template body.
        """
        if not is_publish_allowed(to, _allowed_ids):
            return f"Access denied: recipient {to} is not in the publish contacts list for this channel bot."
        template_obj: dict[str, Any] = {
            "name": template_name,
            "language": {"code": language_code},
        }
        # Add body component parameters if provided
        if body_parameters:
            params = [
                {"type": "text", "text": p.strip()}
                for p in body_parameters.split(",")
                if p.strip()
            ]
            if params:
                template_obj["components"] = [
                    {"type": "body", "parameters": params},
                ]

        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "post",
                f"{_BASE_URL}/{phone_number_id}/messages",
                _PROVIDER,
                "send template",
                headers=headers,
                json={
                    "messaging_product": "whatsapp",
                    "to": to,
                    "type": "template",
                    "template": template_obj,
                },
            )
            if err:
                return err
            data = resp.json()
            messages = data.get("messages", [])
            msg_id = messages[0]["id"] if messages else "unknown"
            return f"Template '{template_name}' sent to {to} (message ID: {msg_id})."

    async def send_whatsapp_bot_media(
        to: str,
        file_id: str,
        caption: str = "",
        filename: str = "",
        file_context: FileContext | None = None,
    ) -> str | ToolResult:
        """Send a file of the run (image, video, audio or document) to a WhatsApp number."""
        if not is_publish_allowed(to, _allowed_ids):
            return f"Access denied: recipient {to} is not in the publish contacts list for this channel bot."
        piped = await read_pipe_file(
            file_context, file_id, max_bytes=_MAX_MEDIA_BYTES, target="WhatsApp"
        )
        if isinstance(piped, str):
            return ToolResult(tool_call_id="", content=piped, is_error=True)
        data, piped_name, mime = piped
        name = (filename or "").strip() or piped_name
        kind = _media_kind(mime)
        auth_only = {"Authorization": headers["Authorization"]}
        async with egress_client(timeout=120) as client:
            resp, err = await safe_request(
                client,
                "post",
                f"{_BASE_URL}/{phone_number_id}/media",
                _PROVIDER,
                "upload media",
                headers=auth_only,
                data={"messaging_product": "whatsapp", "type": mime},
                files={"file": (name, data, mime)},
            )
            if err:
                return err
            media_id = resp.json().get("id")
            if not media_id:
                return "Error: WhatsApp did not return a media id for the upload."
            media: dict[str, Any] = {"id": media_id}
            if caption and kind != "audio":
                media["caption"] = caption[:1024]
            if kind == "document":
                media["filename"] = name
            resp, err = await safe_request(
                client,
                "post",
                f"{_BASE_URL}/{phone_number_id}/messages",
                _PROVIDER,
                "send media",
                headers=headers,
                json={"messaging_product": "whatsapp", "to": to, "type": kind, kind: media},
            )
            if err:
                return err
        messages = resp.json().get("messages", [])
        msg_id = messages[0]["id"] if messages else "unknown"
        return uploaded_file_answer(
            f"WhatsApp {kind} of file {file_id.strip()} sent to {to} (message ID: {msg_id}, media ID: {media_id}).",
            target=_PROVIDER,
            filename=name,
        )

    async def download_whatsapp_bot_media(media_id: str, filename: str = "") -> str | ToolResult:
        """Download a WhatsApp media (by its media id) into the conversation."""
        media_id = (media_id or "").strip()
        if not media_id or not media_id.isdigit():
            return ToolResult(
                tool_call_id="", content="Error: media_id must be a WhatsApp media id (digits).", is_error=True
            )
        async with egress_client(timeout=120) as client:
            resp, err = await safe_request(
                client, "get", f"{_BASE_URL}/{media_id}", _PROVIDER, "get media URL", headers=headers
            )
            if err:
                return err
            meta = resp.json()
            url = str(meta.get("url") or "")
            if not _is_meta_media_host(url):
                return ToolResult(
                    tool_call_id="", content="Error: WhatsApp gave no Meta media URL for this media.", is_error=True
                )
            if int(meta.get("file_size") or 0) > _MAX_MEDIA_BYTES:
                return f"Media {media_id} is over the {_MAX_MEDIA_BYTES // 1024 // 1024} MB download limit."
            # The media URL needs the access token; it is only sent to a Meta host.
            file_resp, err = await safe_request(
                client, "get", url, _PROVIDER, "download media", headers={"Authorization": headers["Authorization"]}
            )
            if err:
                return err
        mime = str(meta.get("mime_type") or "application/octet-stream").split(";", 1)[0].strip()
        ext = mimetypes.guess_extension(mime) or ""
        return await deliver_file(
            data=file_resp.content,
            filename=(filename or "").strip() or f"whatsapp-{media_id}{ext}",
            content_type=mime,
            source="WhatsApp",
            persist_file_fn=persist_file_fn,
            links=[("Download URL (needs the WhatsApp access token, expires after 5 minutes)", url)],
        )

    async def mark_whatsapp_bot_read(message_id: str) -> str:
        """Mark a WhatsApp message as read (shows blue check marks)."""
        if _publish_blocked:
            return "Access denied: publishing is disabled for this channel bot (the agent's publish channels do not overlap the bot whitelist)."
        async with egress_client(timeout=30) as client:
            _, err = await safe_request(
                client,
                "post",
                f"{_BASE_URL}/{phone_number_id}/messages",
                _PROVIDER,
                "mark as read",
                headers=headers,
                json={
                    "messaging_product": "whatsapp",
                    "status": "read",
                    "message_id": message_id,
                },
            )
            if err:
                return err
            return f"Message {message_id} marked as read."

    async def react_whatsapp_bot_message(message_id: str, emoji: str) -> str:
        """React to a WhatsApp message with an emoji."""
        if _publish_blocked:
            return "Access denied: publishing is disabled for this channel bot (the agent's publish channels do not overlap the bot whitelist)."
        async with egress_client(timeout=30) as client:
            _, err = await safe_request(
                client,
                "post",
                f"{_BASE_URL}/{phone_number_id}/messages",
                _PROVIDER,
                "react to message",
                headers=headers,
                json={
                    "messaging_product": "whatsapp",
                    "recipient_type": "individual",
                    "type": "reaction",
                    "reaction": {
                        "message_id": message_id,
                        "emoji": emoji,
                    },
                },
            )
            if err:
                return err
            return f"Reacted to message {message_id} with {emoji}."

    # ── Register handlers ─────────────────────────────────────────────────

    executor.register("send_whatsapp_bot_message", send_whatsapp_bot_message)
    executor.register("send_whatsapp_bot_template", send_whatsapp_bot_template)
    executor.register("send_whatsapp_bot_media", send_whatsapp_bot_media)
    # A media id says nothing about the conversation it came from, so a bot
    # restricted to some recipients cannot bound the download: withheld.
    media_download_allowed = (
        _allowed_ids is None and not _kwargs.get("read_channels") and not _publish_blocked
    )
    if media_download_allowed:
        executor.register("download_whatsapp_bot_media", download_whatsapp_bot_media)
    executor.register("mark_whatsapp_bot_read", mark_whatsapp_bot_read)
    executor.register("react_whatsapp_bot_message", react_whatsapp_bot_message)

    definitions = [
        ToolDefinition(
            name="send_whatsapp_bot_message",
            description="Send a text message to a WhatsApp number as the bot. The recipient phone number must be in international format without '+' (e.g. '14155552671'). Note: if more than 24 hours have passed since the user's last message, you must use send_whatsapp_bot_template instead.",
            input_schema={
                "type": "object",
                "properties": {
                    "to": {
                        "type": "string",
                        "description": "Recipient phone number in international format (e.g. '14155552671')",
                    },
                    "text": {
                        "type": "string",
                        "description": "Message text (max 4096 characters)",
                    },
                    "decision": {
                        "type": "string",
                        "description": "A decision id (run: or work_item: then its UUID): sent as reply buttons the person answers in place, 1:1 only; text is then ignored",
                    },
                },
                "required": ["to"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="send_whatsapp_bot_template",
            description="Send a pre-approved template message to a WhatsApp number. Required to initiate conversations outside the 24-hour customer service window. Templates must be configured in Meta Business Manager.",
            input_schema={
                "type": "object",
                "properties": {
                    "to": {
                        "type": "string",
                        "description": "Recipient phone number in international format",
                    },
                    "template_name": {
                        "type": "string",
                        "description": "Name of the approved WhatsApp message template",
                    },
                    "language_code": {
                        "type": "string",
                        "default": "en_US",
                        "description": "Template language code (e.g. 'en_US', 'fr', 'es')",
                    },
                    "body_parameters": {
                        "type": "string",
                        "default": "",
                        "description": "Comma-separated values for template body placeholders ({{1}}, {{2}}, ...)",
                    },
                },
                "required": ["to", "template_name"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="send_whatsapp_bot_media",
            description=(
                "Send a file of this conversation (attached, generated or downloaded) to a "
                "WhatsApp number as the bot: a JPEG / PNG as an image, an MP4 as a video, "
                "audio as audio, anything else (PDF, spreadsheet...) as a document, with an "
                "optional caption. The 24-hour customer service window applies."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "to": {"type": "string", "description": "Recipient phone number in international format"},
                    "file_id": {"type": "string", "description": "file_id of a file of this conversation"},
                    "caption": {"type": "string", "description": "Optional caption"},
                    "filename": {"type": "string", "description": "Optional file name shown for a document"},
                },
                "required": ["to", "file_id"],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="download_whatsapp_bot_media",
            workspace_ingest=True,
            description=(
                "Download a WhatsApp media by its media id into the conversation: a download "
                "card and a file_id to forward to any tool that takes a file. Its readable "
                "content comes with the result."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "media_id": {"type": "string", "description": "WhatsApp media id (digits)"},
                    "filename": {"type": "string", "description": "Optional name for the stored file"},
                },
                "required": ["media_id"],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="mark_whatsapp_bot_read",
            description="Mark a WhatsApp message as read (shows blue check marks to the sender).",
            input_schema={
                "type": "object",
                "properties": {
                    "message_id": {
                        "type": "string",
                        "description": "WhatsApp message ID to mark as read",
                    },
                },
                "required": ["message_id"],
            },
            source=_SOURCE,
            priority=2,
        ),
        ToolDefinition(
            name="react_whatsapp_bot_message",
            description="React to a WhatsApp message with an emoji. Use a single Unicode emoji character (e.g. '\U0001f44d', '\u2764\ufe0f', '\U0001f602').",
            input_schema={
                "type": "object",
                "properties": {
                    "message_id": {
                        "type": "string",
                        "description": "WhatsApp message ID to react to",
                    },
                    "emoji": {
                        "type": "string",
                        "description": "Single Unicode emoji character (e.g. '\U0001f44d', '\u2764\ufe0f')",
                    },
                },
                "required": ["message_id", "emoji"],
            },
            source=_SOURCE,
            priority=1,
        ),
    ]
    if not media_download_allowed:
        definitions = [d for d in definitions if d.name != "download_whatsapp_bot_media"]
    return definitions
