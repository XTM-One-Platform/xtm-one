# SPDX-License-Identifier: Apache-2.0

"""WhatsApp integration - one business phone number on the WhatsApp Cloud API.

The integration operates a business phone number registered on the WhatsApp
Business Platform (Meta Cloud API); it never operates a consumer WhatsApp
account. It is the integration counterpart of the ``whatsapp_bot`` channel bot:
an agent bound to it sends text, templates, media and interactive messages to
a number or to a group the business created, reacts, marks messages read,
downloads media, and reads the number's profile and its active groups.

Known limits of the Cloud API (live Meta documentation, Nov 2026):

- Groups need an Official Business Account; a group is created by the business
  and members join by an invite link (a business number cannot be added to an
  existing group); at most 8 participants.
- Interactive messages (reply buttons, lists) are 1:1 only: refused for a group.
- Polls can be neither created nor read: a member's poll reaches the webhook as
  an ``unsupported`` message with no question, options or votes.
- Outside the 24-hour customer service window only approved templates go out.

Credential format: ``phone_number_id||access_token`` (``integration_tools._get_token``).
"""

from __future__ import annotations

import logging
import re
from typing import Any

from app.core.egress import egress_client
from app.integrations.base import BaseIntegration, OAuthDefaults
from app.integrations.file_delivery import deliver_file, read_pipe_file
from app.integrations.http import bearer_auth_headers, parse_compound_token, safe_request
from app.integrations.providers.whatsapp_bot import _is_meta_media_host, _media_kind
from app.integrations.providers.whatsapp_gateway import build_tools as gateway_build_tools
from app.integrations.providers.whatsapp_gateway import gateway_only_definitions, is_gateway_token
from app.services.outbound_links import markdown_links_as_plain_urls
from app.tools.base import CallableToolExecutor, FileContext, ToolDefinition, ToolResult

logger = logging.getLogger(__name__)

_PROVIDER = "WhatsApp"
_SOURCE = "integration:whatsapp"
GRAPH_API = "https://graph.facebook.com/v21.0"
# The Cloud API's largest media (a document).
_MAX_MEDIA_BYTES = 100 * 1024 * 1024
_MAX_TEXT = 4096
_MAX_BUTTONS = 3
_MAX_LIST_ROWS = 10
_PHONE_RE = re.compile(r"^\+?\d{6,20}$")
_MEDIA_ID_RE = re.compile(r"^\d+$")
# The secret half of the catalog's introspection credential (build_dummy_token).
_CATALOG_DUMMY_SECRET = "__dummy__"  # noqa: S105 - catalog introspection marker

_GROUP_INTERACTIVE_LIMIT = (
    "Error: WhatsApp does not support interactive messages (buttons, lists) in groups. "
    "Send a text message with numbered choices instead and read the replies."
)
_POLL_LIMIT = (
    "WhatsApp polls cannot be created or read through the Cloud API; a member's poll "
    "reaches the webhook without its question, options or votes."
)


def whatsapp_headers(access_token: str) -> dict[str, str]:
    """Bearer headers for the Graph API, shared by the tools and the connectivity test."""
    return bearer_auth_headers(access_token, extra={"Content-Type": "application/json"})


async def download_media_bytes(access_token: str, media_id: str, max_bytes: int) -> tuple[bytes, str | None]:
    """The bytes of a received Cloud API media (a voice note), at most *max_bytes*; ``(b"", reason)`` on failure.

    ``GET /{media-id}`` names a short-lived URL; the access token is only ever
    sent to a Meta host. Held in memory by the caller, never stored here.
    """
    media_id = (media_id or "").strip()
    if not _MEDIA_ID_RE.match(media_id):
        return b"", "not a media id"
    if not access_token:
        return b"", "no access token on the integration"
    headers = whatsapp_headers(access_token)
    async with egress_client(timeout=60) as client:
        resp, err = await safe_request(
            client, "get", f"{GRAPH_API}/{media_id}", _PROVIDER, "get media URL", headers=headers
        )
        if err:
            return b"", err
        meta = resp.json() if resp is not None else {}
        url = str((meta or {}).get("url") or "") if isinstance(meta, dict) else ""
        if not _is_meta_media_host(url):
            return b"", "no Meta media URL for this media"
        try:
            declared = int((meta or {}).get("file_size") or 0)
        except (TypeError, ValueError):
            declared = 0
        if declared > max_bytes:
            return b"", f"larger than {max_bytes // (1024 * 1024)} MB"
        file_resp, err = await safe_request(
            client, "get", url, _PROVIDER, "download media", headers={"Authorization": headers["Authorization"]}
        )
        if err:
            return b"", err
    data = file_resp.content or b""
    if len(data) > max_bytes:
        return b"", f"larger than {max_bytes // (1024 * 1024)} MB"
    return data, None


def recipient_fields(to: str) -> dict[str, str] | None:
    """The ``to`` / ``recipient_type`` pair of a send: a phone number or a group id.

    A phone number is digits in international format (a leading ``+`` is
    dropped); anything else is a group id as the Groups API returns it.
    ``None`` for an empty recipient.
    """
    value = (to or "").strip()
    if not value:
        return None
    if _PHONE_RE.match(value):
        return {"recipient_type": "individual", "to": value.lstrip("+")}
    return {"recipient_type": "group", "to": value}


class WhatsAppIntegration(BaseIntegration):
    """WhatsApp Cloud API, one business phone number (system user access token)."""

    provider_name = "whatsapp"
    display_name = "WhatsApp"
    icon = "Phone"
    description = (
        "Send and receive WhatsApp messages as a business phone number: text, "
        "templates, media and interactive messages, reactions, read receipts, "
        "and the groups the number created."
    )
    auth_types = ["api_key"]

    def get_oauth_defaults(self) -> OAuthDefaults | None:
        """The Cloud API takes a system user access token, not OAuth."""
        return None

    async def validate_connection(self, token: str) -> bool:
        """Read the phone number's profile with the token."""
        phone_number_id, access_token = (p.strip() for p in parse_compound_token(token, 2, greedy_last=True))
        if not phone_number_id or not access_token:
            return False
        async with egress_client(timeout=15) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{GRAPH_API}/{phone_number_id}",
                _PROVIDER,
                "read phone number",
                headers=whatsapp_headers(access_token),
                params={"fields": "display_phone_number,verified_name"},
            )
        return err is None and resp is not None and resp.is_success

    async def get_user_info(self, token: str) -> dict:
        """The phone number id the integration operates."""
        phone_number_id, _ = parse_compound_token(token, 2, greedy_last=True)
        return {"name": "WhatsApp", "phone_number_id": phone_number_id.strip()}


def _message_id(data: Any) -> str:
    messages = data.get("messages") if isinstance(data, dict) else None
    if isinstance(messages, list) and messages and isinstance(messages[0], dict):
        return str(messages[0].get("id") or "unknown")
    return "unknown"


def _error(text: str) -> ToolResult:
    return ToolResult(tool_call_id="", content=text, is_error=True)


def build_tools(token: str, executor: CallableToolExecutor, **kwargs: Any) -> list[ToolDefinition]:
    """Build the WhatsApp integration tools.

    *token* is ``phone_number_id||access_token`` on the Meta Cloud API backend,
    or a linked-device gateway credential (``linked_device_gateway||...``),
    whose tools :mod:`whatsapp_gateway` builds.
    """
    if is_gateway_token(token):
        return gateway_build_tools(token, executor, **kwargs)
    phone_number_id, access_token = (p.strip() for p in parse_compound_token(token, 2, greedy_last=True))
    if not phone_number_id or not access_token:
        logger.warning("whatsapp token format invalid - expected phone_number_id||access_token")
        return []
    headers = whatsapp_headers(access_token)
    persist_file_fn = kwargs.get("persist_file_fn")
    integration_id = str(kwargs.get("integration_id") or "")
    send_origin = dict(kwargs.get("send_origin") or {})
    messages_url = f"{GRAPH_API}/{phone_number_id}/messages"

    async def _log_send(tool: str, to: str, text: str, message_id: str | None, error: str | None) -> None:
        """The send in the activity log of the WhatsApp agent account on this integration."""
        if not integration_id or tool == "react_whatsapp_message":
            return
        from app.services.agent_accounts.sent_log import record_proactive_send

        recipient = recipient_fields(to) or {}
        await record_proactive_send(
            integration_id,
            provider="whatsapp",
            tool=tool,
            conversation_id=recipient.get("to") or to,
            conversation_type="group" if recipient.get("recipient_type") == "group" else "direct",
            text=text,
            message_id=message_id,
            error=error,
            origin=send_origin,
        )

    async def _send(
        to: str, action: str, message: dict[str, Any], *, tool: str = "", logged: str = ""
    ) -> tuple[str | None, str | None]:
        """POST one message; ``(message_id, None)`` or ``(None, error)``. *tool* logs it as *logged*."""
        recipient = recipient_fields(to)
        if recipient is None:
            return None, "Error: 'to' must be a phone number in international format or a group id."
        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "post",
                messages_url,
                _PROVIDER,
                action,
                headers=headers,
                json={"messaging_product": "whatsapp", **recipient, **message},
            )
        msg_id = None if err else _message_id(resp.json())
        if tool:
            await _log_send(tool, to, logged, msg_id, err)
        return msg_id, err

    # ── Reads ─────────────────────────────────────────────────────────────

    async def get_whatsapp_business_number() -> str:
        """The business phone number the integration operates."""
        async with egress_client(timeout=15) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{GRAPH_API}/{phone_number_id}",
                _PROVIDER,
                "read phone number",
                headers=headers,
                params={"fields": "display_phone_number,verified_name,quality_rating,name_status"},
            )
        if err:
            return err
        data = resp.json()
        return (
            f"WhatsApp business number {data.get('display_phone_number', '?')} "
            f"({data.get('verified_name', 'no verified name')}), phone number id {phone_number_id}, "
            f"quality rating {data.get('quality_rating', 'unknown')}, display name status "
            f"{data.get('name_status', 'unknown')}."
        )

    async def list_whatsapp_groups(limit: int = 25, after: str = "") -> str:
        """The active groups of the business number."""
        params: dict[str, Any] = {"limit": max(1, min(int(limit or 25), 1024))}
        if after:
            params["after"] = after
        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{GRAPH_API}/{phone_number_id}/groups",
                _PROVIDER,
                "list groups",
                headers=headers,
                params=params,
            )
        if err:
            return err
        body = resp.json()
        data = body.get("data") or {}
        groups = data.get("groups") if isinstance(data, dict) else data
        if not groups:
            return (
                "This business number has no active WhatsApp group. Groups are created by the "
                "business (Official Business Account) and members join by an invite link."
            )
        lines = [
            f"- {g.get('subject') or '(no subject)'}: group id {g.get('id')}, created {g.get('created_at', '?')}"
            for g in groups
            if isinstance(g, dict)
        ]
        cursor = ((body.get("paging") or {}).get("cursors") or {}).get("after")
        more = (
            f"\nMore groups: call again with after='{cursor}'." if cursor and body.get("paging", {}).get("next") else ""
        )
        return f"{len(lines)} WhatsApp group(s):\n" + "\n".join(lines) + more

    async def get_whatsapp_group(group_id: str) -> str:
        """Metadata and participants of one group."""
        group_id = (group_id or "").strip()
        if not group_id:
            return "Error: group_id is required."
        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{GRAPH_API}/{group_id}",
                _PROVIDER,
                "read group",
                headers=headers,
                params={
                    "fields": "subject,description,participants,total_participant_count,join_approval_mode,suspended"
                },
            )
        if err:
            return err
        g = resp.json()
        participants = ", ".join(p.get("wa_id", "?") for p in g.get("participants") or [] if isinstance(p, dict))
        return (
            f"Group {g.get('subject') or '(no subject)'} ({group_id}): "
            f"{g.get('total_participant_count', '?')} participant(s) besides the business"
            f"{': ' + participants if participants else ''}; join approval "
            f"{g.get('join_approval_mode', 'unknown')}; suspended {g.get('suspended', False)}."
            + (f"\nDescription: {g['description']}" if g.get("description") else "")
        )

    async def download_whatsapp_media(media_id: str, filename: str = "") -> str | ToolResult:
        """Download a received WhatsApp media into the conversation."""
        media_id = (media_id or "").strip()
        if not _MEDIA_ID_RE.match(media_id):
            return _error("Error: media_id must be a WhatsApp media id (digits).")
        async with egress_client(timeout=120) as client:
            resp, err = await safe_request(
                client, "get", f"{GRAPH_API}/{media_id}", _PROVIDER, "get media URL", headers=headers
            )
            if err:
                return err
            meta = resp.json()
            url = str(meta.get("url") or "")
            if not _is_meta_media_host(url):
                return _error("Error: WhatsApp gave no Meta media URL for this media.")
            if int(meta.get("file_size") or 0) > _MAX_MEDIA_BYTES:
                return f"Media {media_id} is over the {_MAX_MEDIA_BYTES // 1024 // 1024} MB download limit."
            # The media URL needs the access token; it is only sent to a Meta host.
            file_resp, err = await safe_request(
                client,
                "get",
                url,
                _PROVIDER,
                "download media",
                headers={"Authorization": headers["Authorization"]},
            )
            if err:
                return err
        mime = str(meta.get("mime_type") or "application/octet-stream").split(";", 1)[0].strip()
        return await deliver_file(
            data=file_resp.content,
            filename=(filename or "").strip() or f"whatsapp-{media_id}",
            content_type=mime,
            source="WhatsApp",
            persist_file_fn=persist_file_fn,
        )

    async def list_whatsapp_conversations(limit: int = 25) -> str:
        """The conversations the business number received messages in, newest first."""
        from app.services import whatsapp_inbound

        if not integration_id:
            return _error("Error: received messages are only available to an agent bound to the integration.")
        return await whatsapp_inbound.conversations_text(
            integration_id,
            limit,
            "No WhatsApp message received yet. Inbound messages need the Meta app's webhook "
            "pointed at the platform's /api/channels/whatsapp/webhook and subscribed to 'messages'.",
        )

    async def read_whatsapp_messages(conversation_id: str, limit: int = 50) -> str:
        """The latest received messages of one conversation, oldest first."""
        from app.services import whatsapp_inbound

        if not integration_id:
            return _error("Error: received messages are only available to an agent bound to the integration.")
        conversation_id = (conversation_id or "").strip()
        if not conversation_id:
            return _error("Error: conversation_id is required (a phone number or a group id).")
        return await whatsapp_inbound.messages_text(integration_id, conversation_id, limit)

    # ── Writes ────────────────────────────────────────────────────────────

    async def send_whatsapp_message(to: str, text: str) -> str:
        """Send a text to a number or a group."""
        if not (text or "").strip():
            return "Error: text is empty."
        # WhatsApp draws no Markdown: a link is sent as its full URL (#10115).
        text = markdown_links_as_plain_urls(text)
        msg_id, err = await _send(
            to,
            "send message",
            {"type": "text", "text": {"body": text[:_MAX_TEXT]}},
            tool="send_whatsapp_message",
            logged=text[:_MAX_TEXT],
        )
        if err:
            return err
        cut = f" (cut to {_MAX_TEXT} characters)" if len(text) > _MAX_TEXT else ""
        return f"WhatsApp message sent to {to}{cut} (message ID: {msg_id})."

    async def send_whatsapp_template(
        to: str, template_name: str, language_code: str = "en_US", body_parameters: list[str] | None = None
    ) -> str:
        """Send an approved template to a number or a group."""
        template: dict[str, Any] = {"name": template_name, "language": {"code": language_code or "en_US"}}
        params = [{"type": "text", "text": str(p)} for p in (body_parameters or []) if str(p).strip()]
        if params:
            template["components"] = [{"type": "body", "parameters": params}]
        msg_id, err = await _send(
            to,
            "send template",
            {"type": "template", "template": template},
            tool="send_whatsapp_template",
            logged=f"[template {template_name}] {' | '.join(p['text'] for p in params)}".strip(),
        )
        if err:
            return err
        return f"Template '{template_name}' sent to {to} (message ID: {msg_id})."

    async def send_whatsapp_media(
        to: str,
        file_id: str,
        caption: str = "",
        filename: str = "",
        file_context: FileContext | None = None,
    ) -> str | ToolResult:
        """Send a file of the run to a number or a group."""
        if recipient_fields(to) is None:
            return _error("Error: 'to' must be a phone number in international format or a group id.")
        piped = await read_pipe_file(file_context, file_id, max_bytes=_MAX_MEDIA_BYTES, target="WhatsApp")
        if isinstance(piped, str):
            return _error(piped)
        data, piped_name, mime = piped
        name = (filename or "").strip() or piped_name
        kind = _media_kind(mime)
        async with egress_client(timeout=120) as client:
            resp, err = await safe_request(
                client,
                "post",
                f"{GRAPH_API}/{phone_number_id}/media",
                _PROVIDER,
                "upload media",
                headers={"Authorization": headers["Authorization"]},
                data={"messaging_product": "whatsapp", "type": mime},
                files={"file": (name, data, mime)},
            )
        if err:
            return err
        media_id = resp.json().get("id")
        if not media_id:
            return _error("Error: WhatsApp did not return a media id for the upload.")
        media: dict[str, Any] = {"id": media_id}
        if caption and kind != "audio":
            media["caption"] = caption[:1024]
        if kind == "document":
            media["filename"] = name
        msg_id, err = await _send(
            to,
            "send media",
            {"type": kind, kind: media},
            tool="send_whatsapp_media",
            logged=f"[{kind} {name}] {caption or ''}".strip(),
        )
        if err:
            return err
        return f"WhatsApp {kind} {name} sent to {to} (message ID: {msg_id}, media ID: {media_id})."

    async def send_whatsapp_interactive(
        to: str,
        body: str,
        buttons: list[str] | None = None,
        list_rows: list[str] | None = None,
        list_button_label: str = "Choose",
    ) -> str:
        """Send reply buttons or a list to a number (1:1 only)."""
        recipient = recipient_fields(to)
        if recipient is None:
            return "Error: 'to' must be a phone number in international format."
        if recipient["recipient_type"] == "group":
            return _GROUP_INTERACTIVE_LIMIT
        if not (body or "").strip():
            return "Error: body is empty."
        buttons = [str(b).strip() for b in (buttons or []) if str(b).strip()]
        list_rows = [str(r).strip() for r in (list_rows or []) if str(r).strip()]
        if bool(buttons) == bool(list_rows):
            return "Error: give either buttons (1 to 3) or list_rows (1 to 10), not both and not neither."
        if len(buttons) > _MAX_BUTTONS:
            return f"Error: WhatsApp allows at most {_MAX_BUTTONS} reply buttons; use list_rows for more choices."
        if len(list_rows) > _MAX_LIST_ROWS:
            return f"Error: a WhatsApp list holds at most {_MAX_LIST_ROWS} rows."
        if buttons:
            interactive: dict[str, Any] = {
                "type": "button",
                "body": {"text": body[:1024]},
                "action": {
                    "buttons": [
                        {"type": "reply", "reply": {"id": f"choice_{i + 1}", "title": b[:20]}}
                        for i, b in enumerate(buttons)
                    ]
                },
            }
        else:
            interactive = {
                "type": "list",
                "body": {"text": body[:4096]},
                "action": {
                    "button": (list_button_label or "Choose")[:20],
                    "sections": [
                        {"rows": [{"id": f"choice_{i + 1}", "title": r[:24]} for i, r in enumerate(list_rows)]}
                    ],
                },
            }
        msg_id, err = await _send(
            to,
            "send interactive message",
            {"type": "interactive", "interactive": interactive},
            tool="send_whatsapp_interactive",
            logged=str(body or ""),
        )
        if err:
            return err
        return f"Interactive message sent to {to} (message ID: {msg_id}); a reply arrives as the choice's title."

    async def react_whatsapp_message(to: str, message_id: str, emoji: str) -> str:
        """React to a message of a number or a group with an emoji (empty removes it)."""
        _, err = await _send(
            to,
            "react to message",
            {"type": "reaction", "reaction": {"message_id": message_id, "emoji": emoji or ""}},
        )
        if err:
            return err
        return f"Reacted to message {message_id} with {emoji}." if emoji else f"Reaction removed from {message_id}."

    async def mark_whatsapp_message_read(message_id: str) -> str:
        """Mark a received message as read (blue check marks)."""
        async with egress_client(timeout=30) as client:
            _, err = await safe_request(
                client,
                "post",
                messages_url,
                _PROVIDER,
                "mark as read",
                headers=headers,
                json={"messaging_product": "whatsapp", "status": "read", "message_id": message_id},
            )
        if err:
            return err
        return f"Message {message_id} marked as read."

    handlers = {
        "get_whatsapp_business_number": get_whatsapp_business_number,
        "list_whatsapp_groups": list_whatsapp_groups,
        "get_whatsapp_group": get_whatsapp_group,
        "download_whatsapp_media": download_whatsapp_media,
        "list_whatsapp_conversations": list_whatsapp_conversations,
        "read_whatsapp_messages": read_whatsapp_messages,
        "send_whatsapp_message": send_whatsapp_message,
        "send_whatsapp_template": send_whatsapp_template,
        "send_whatsapp_media": send_whatsapp_media,
        "send_whatsapp_interactive": send_whatsapp_interactive,
        "react_whatsapp_message": react_whatsapp_message,
        "mark_whatsapp_message_read": mark_whatsapp_message_read,
    }
    for name, handler in handlers.items():
        executor.register(name, handler)

    to_prop = {
        "type": "string",
        "description": "A phone number in international format (e.g. '14155552671') or a group id from list_whatsapp_groups",
    }
    # Catalog introspection (the dummy credential, ``build_dummy_token``) lists
    # the provider's whole tool surface: the tools only the linked-device
    # gateway backend has (polls, pairing) are added there, never to a real
    # Meta integration, so the catalog and the HITL snapshot classify them.
    introspection = gateway_only_definitions() if access_token == _CATALOG_DUMMY_SECRET else []
    return [
        *introspection,
        ToolDefinition(
            name="get_whatsapp_business_number",
            description="Read the WhatsApp business phone number this integration operates: display number, verified name, quality rating.",
            input_schema={"type": "object", "properties": {}},
            source=_SOURCE,
            priority=2,
        ),
        ToolDefinition(
            name="list_whatsapp_groups",
            description=(
                "List the active WhatsApp groups of the business number (id, subject, creation time). "
                "Groups are created by the business; members join by an invite link."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "default": 25, "description": "Groups per page (1 to 1024)"},
                    "after": {"type": "string", "description": "Cursor from a previous page"},
                },
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="get_whatsapp_group",
            description="Read one WhatsApp group: subject, description, participants, join approval mode.",
            input_schema={
                "type": "object",
                "properties": {"group_id": {"type": "string", "description": "Group id from list_whatsapp_groups"}},
                "required": ["group_id"],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="download_whatsapp_media",
            workspace_ingest=True,
            description=(
                "Download a received WhatsApp media by its media id into the conversation: a download "
                "card and a file_id to forward to any tool that takes a file."
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
            name="list_whatsapp_conversations",
            description=(
                "List the WhatsApp conversations (a phone number or a group id) the business number received "
                "messages in, newest first, with the last message. Messages are kept for the integration's "
                "retention (30 days by default). " + _POLL_LIMIT
            ),
            input_schema={
                "type": "object",
                "properties": {"limit": {"type": "integer", "default": 25, "description": "Conversations (1 to 100)"}},
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="read_whatsapp_messages",
            description=(
                "Read the latest received messages of one WhatsApp conversation, oldest first: sender, time, "
                "message id (for react / mark read) and text; a media shows its media id for "
                "download_whatsapp_media; a poll is recorded in words because WhatsApp does not deliver it."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "conversation_id": {
                        "type": "string",
                        "description": "Phone number or group id from list_whatsapp_conversations",
                    },
                    "limit": {"type": "integer", "default": 50, "description": "Messages (1 to 200)"},
                },
                "required": ["conversation_id"],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="send_whatsapp_message",
            description=(
                "Send a text (up to 4096 characters) to a WhatsApp number or group as the business number. "
                "Free-form text needs an open 24-hour window (the recipient or a group member wrote in the "
                "last 24 hours); otherwise use send_whatsapp_template. " + _POLL_LIMIT
            ),
            input_schema={
                "type": "object",
                "properties": {"to": to_prop, "text": {"type": "string", "description": "Message text"}},
                "required": ["to", "text"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="send_whatsapp_template",
            description=(
                "Send a pre-approved WhatsApp template to a number or group: the only message that opens a "
                "conversation outside the 24-hour window. Templates are managed in WhatsApp Manager."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "to": to_prop,
                    "template_name": {"type": "string", "description": "Approved template name"},
                    "language_code": {
                        "type": "string",
                        "default": "en_US",
                        "description": "Template language (e.g. 'en_US', 'fr')",
                    },
                    "body_parameters": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Values for the body placeholders {{1}}, {{2}}, ... in order",
                    },
                },
                "required": ["to", "template_name"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="send_whatsapp_media",
            description=(
                "Send a file of this conversation to a WhatsApp number or group: JPEG / PNG as an image, MP4 "
                "as a video, audio as audio, anything else as a document, with an optional caption. The "
                "24-hour window applies."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "to": to_prop,
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
            name="send_whatsapp_interactive",
            description=(
                "Send a question with reply buttons (up to 3) or a list (up to 10 rows) to a WhatsApp number, "
                "1:1 only: WhatsApp refuses interactive messages in groups. The closest substitute for a "
                "poll. " + _POLL_LIMIT
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "to": {"type": "string", "description": "Phone number in international format"},
                    "body": {"type": "string", "description": "The question"},
                    "buttons": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "1 to 3 button titles (20 characters each)",
                    },
                    "list_rows": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "1 to 10 row titles (24 characters each)",
                    },
                    "list_button_label": {
                        "type": "string",
                        "default": "Choose",
                        "description": "Label of the button that opens the list",
                    },
                },
                "required": ["to", "body"],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="react_whatsapp_message",
            description="React to a WhatsApp message with one emoji (an empty emoji removes the reaction).",
            input_schema={
                "type": "object",
                "properties": {
                    "to": to_prop,
                    "message_id": {"type": "string", "description": "WhatsApp message id (wamid...)"},
                    "emoji": {"type": "string", "description": "One Unicode emoji"},
                },
                "required": ["to", "message_id", "emoji"],
            },
            source=_SOURCE,
            priority=2,
        ),
        ToolDefinition(
            name="mark_whatsapp_message_read",
            description="Mark a received WhatsApp message as read (blue check marks for the sender).",
            input_schema={
                "type": "object",
                "properties": {"message_id": {"type": "string", "description": "WhatsApp message id (wamid...)"}},
                "required": ["message_id"],
            },
            source=_SOURCE,
            priority=2,
        ),
    ]
