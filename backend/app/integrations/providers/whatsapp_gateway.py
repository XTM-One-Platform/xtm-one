# SPDX-License-Identifier: Apache-2.0

"""WhatsApp linked-device gateway backend of the ``whatsapp`` integration.

The ``whatsapp`` integration has two backends, chosen per integration by its
``backend`` extra field:

- ``meta_cloud_api`` (default): a business phone number on the official
  WhatsApp Cloud API (:mod:`app.integrations.providers.whatsapp`).
- ``linked_device_gateway``: a WhatsApp number paired as a linked device to a
  hosted gateway that exposes it over REST. It can do what the Cloud API cannot:
  join an existing group, create a poll and read its votes.

A gateway vendor is one :class:`GatewayClient` subclass registered in
``GATEWAY_CLIENTS``; Whapi.Cloud (https://gate.whapi.cloud, bearer channel
token) is the first. The tools only speak to the client interface.

Terms of service: a linked-device gateway is not the official WhatsApp
Business Platform; the paired number can be banned by WhatsApp. Use a
dedicated number (docs: "WhatsApp: two backends").

Credential format: ``linked_device_gateway||<vendor>||<base_url>||<channel_token>``
(``integration_tools._get_token``). Message bodies are never logged above debug.

Outbound pacing: every send (text, media, poll) is counted against a
per-integration and a per-chat budget per minute, then waits a random jitter;
a text also shows the typing indicator for a time proportional to its length.
A burst of instant messages from a paired number reads as automation to
WhatsApp, which is what gets a number banned.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import re
import secrets
import time
from datetime import UTC, datetime
from typing import Any

from fastapi import HTTPException

from app.core.egress import egress_client
from app.core.rate_limit import enforce_rate_limit, refund_rate_limit
from app.integrations.file_delivery import deliver_file, read_pipe_file
from app.integrations.http import bearer_auth_headers, parse_compound_token, safe_request
from app.services.outbound_links import markdown_links_as_plain_urls
from app.tools.base import CallableToolExecutor, FileContext, ToolDefinition, ToolResult

logger = logging.getLogger(__name__)

BACKEND_META = "meta_cloud_api"
BACKEND_GATEWAY = "linked_device_gateway"
BACKENDS = (BACKEND_META, BACKEND_GATEWAY)
DEFAULT_VENDOR = "whapi"
WHAPI_BASE_URL = "https://gate.whapi.cloud"

_PROVIDER = "WhatsApp gateway"
_SOURCE = "integration:whatsapp"
_MAX_TEXT = 4096
_MAX_MEDIA_BYTES = 100 * 1024 * 1024
_MIN_POLL_OPTIONS = 2
_MAX_POLL_OPTIONS = 12
_GROUP_SUFFIX = "@g.us"
_USER_SUFFIX = "@s.whatsapp.net"
_PHONE_RE = re.compile(r"^\+?\d{6,20}$")
_GROUP_RE = re.compile(r"^[\d-]{6,40}@g\.us$")

#: The pairing states every vendor maps to.
PAIRING_STATES = ("connected", "qr", "banned", "sync_error", "starting", "unknown")

#: Outbound pacing: sends per minute for one integration, and for one chat of it.
SENDS_PER_MINUTE = 20
SENDS_PER_CHAT_PER_MINUTE = 6
#: Random pause before every send, in seconds (low, high).
SEND_JITTER_SECONDS = (0.4, 1.6)
#: Typing indicator before a text: about a fast typist's pace, at least 1 s, at most this.
TYPING_CHARS_PER_SECOND = 12
TYPING_MAX_SECONDS = 8
#: WhatsApp lets the sender edit a text message for this long after sending it.
EDIT_WINDOW_SECONDS = 15 * 60
#: Sent messages read per conversation by ``read_whatsapp_messages``, at most.
_MAX_SENT_READ = 200

_RANDOM = secrets.SystemRandom()


def typing_seconds(text: str) -> int:
    """Seconds the typing indicator shows before *text* is sent."""
    return max(1, min(TYPING_MAX_SECONDS, round(len(text or "") / TYPING_CHARS_PER_SECOND)))


async def pace_send(integration_id: str, chat: str) -> str | None:
    """Count one send against the integration's and the chat's budget, then wait a jitter.

    ``None`` lets the send go; otherwise the error the tool answers, naming the
    seconds to wait. A send refused on the chat budget gives its unit back to
    the integration budget, so one busy chat does not starve the others. Redis
    unavailable fails open: pacing protects the number, it is not a security
    boundary.
    """
    owner = integration_id or "unbound"
    buckets = (
        ("whatsapp-gateway-send", owner, SENDS_PER_MINUTE),
        ("whatsapp-gateway-chat", f"{owner}:{chat}", SENDS_PER_CHAT_PER_MINUTE),
    )
    for index, (name, bucket, limit) in enumerate(buckets):
        try:
            await enforce_rate_limit(
                None, name, limit=limit, window_seconds=60, bucket=bucket, fail_closed=False, quiet=True
            )
        except HTTPException as exc:
            if exc.status_code != 429:
                raise
            for prev_name, prev_bucket, _ in buckets[:index]:
                await refund_rate_limit(None, prev_name, bucket=prev_bucket)
            wait = (exc.headers or {}).get("Retry-After", "60")
            scope = "this WhatsApp number" if index == 0 else f"chat {chat}"
            return (
                f"Error: send limit reached for {scope} ({limit} messages per minute, to keep the paired number "
                f"from being flagged as automated). Retry in {wait} seconds; nothing was sent."
            )
    low, high = SEND_JITTER_SECONDS
    if high > 0:
        await _pause(_RANDOM.uniform(low, high))
    return None


async def _pause(seconds: float) -> None:
    await asyncio.sleep(seconds)


def is_gateway_token(token: str) -> bool:
    """Whether *token* is a linked-device gateway credential."""
    return (token or "").startswith(f"{BACKEND_GATEWAY}||")


def gateway_token(vendor: str, base_url: str, channel_token: str) -> str:
    """The compound credential the gateway tools parse."""
    return f"{BACKEND_GATEWAY}||{vendor or DEFAULT_VENDOR}||{(base_url or '').rstrip('/')}||{channel_token}"


def chat_id(to: str) -> str | None:
    """Normalise a recipient to a gateway chat id; ``None`` when it is neither.

    A phone number in international format (a leading ``+`` dropped) or a user
    jid is a 1:1 chat; ``...@g.us`` is a group.
    """
    value = (to or "").strip()
    if not value:
        return None
    if _PHONE_RE.match(value):
        return value.lstrip("+")
    if value.endswith(_USER_SUFFIX) and _PHONE_RE.match(value[: -len(_USER_SUFFIX)]):
        return value
    if _GROUP_RE.match(value):
        return value
    return None


def is_group(chat: str) -> bool:
    """Whether a chat id names a group."""
    return chat.endswith(_GROUP_SUFFIX)


def poll_payload(to: str, title: str, options: list[str], multiple: bool = False) -> dict[str, Any] | str:
    """The body of a poll send, or an error string.

    ``count`` is the number of options a voter may select: 1 for a single
    choice, 0 for any number.
    """
    chat = chat_id(to)
    if chat is None:
        return "Error: 'to' must be a phone number in international format or a group id ending in @g.us."
    question = (title or "").strip()
    if not question:
        return "Error: the poll title is empty."
    cleaned = [str(o).strip() for o in (options or []) if str(o).strip()]
    if len(set(cleaned)) != len(cleaned):
        return "Error: poll options must be distinct."
    if not _MIN_POLL_OPTIONS <= len(cleaned) <= _MAX_POLL_OPTIONS:
        return f"Error: a WhatsApp poll takes {_MIN_POLL_OPTIONS} to {_MAX_POLL_OPTIONS} options."
    return {"to": chat, "title": question, "options": cleaned, "count": 0 if multiple else 1}


def poll_tally(message: dict[str, Any]) -> dict[str, Any] | None:
    """The live tally of a poll message: title, total and per option the count and voters.

    ``None`` when the message carries no poll.
    """
    poll = message.get("poll") if isinstance(message, dict) else None
    if not isinstance(poll, dict):
        return None
    options: list[dict[str, Any]] = []
    for result in poll.get("results") or []:
        if not isinstance(result, dict):
            continue
        voters = [
            str(v.get("id") if isinstance(v, dict) else v)
            for v in (result.get("voters") or [])
            if (v.get("id") if isinstance(v, dict) else v)
        ]
        options.append(
            {
                "id": str(result.get("id") or ""),
                "name": str(result.get("name") or ""),
                "count": int(result.get("count") or len(voters)),
                "voters": voters,
            }
        )
    if not options:
        options = [{"id": "", "name": str(o), "count": 0, "voters": []} for o in poll.get("options") or []]
    return {
        "title": str(poll.get("title") or ""),
        "total": int(poll.get("total") or sum(o["count"] for o in options)),
        "options": options,
    }


def edit_refusal(message: Any, message_id: str, now: float) -> str | None:
    """Why the paired number cannot edit *message* (the gateway's read of it), or ``None``.

    Only a text the number sent, younger than :data:`EDIT_WINDOW_SECONDS`, can
    be edited. A field the gateway did not answer is not held against the edit:
    WhatsApp itself refuses what it must.
    """
    if not isinstance(message, dict) or not message:
        return None
    if message.get("from_me") is False:
        return f"Error: message {message_id} was not sent by this number; only your own messages can be edited."
    kind = str(message.get("type") or "")
    if kind and kind != "text":
        return f"Error: message {message_id} is a {kind} message; only a text message can be edited."
    try:
        sent_at = float(str(message.get("timestamp")))
    except ValueError:
        return None
    age = now - sent_at
    if age > EDIT_WINDOW_SECONDS:
        return (
            f"Error: message {message_id} was sent {int(age // 60)} minutes ago; WhatsApp allows an edit only "
            f"within {EDIT_WINDOW_SECONDS // 60} minutes of sending. Nothing was changed: send a correction or "
            "delete it with delete_whatsapp_message."
        )
    return None


def list_chat_id(chat: str) -> str:
    """The chat id the gateway's message list takes: a bare phone number becomes its user jid."""
    return chat if "@" in chat else f"{chat}{_USER_SUFFIX}"


def sent_lines(messages: Any) -> list[str]:
    """One line per message the paired number sent, oldest first: time, id and text."""
    rows = [m for m in messages if isinstance(m, dict) and m.get("id")] if isinstance(messages, list) else []
    rows.sort(key=lambda m: int(str(m.get("timestamp"))) if str(m.get("timestamp") or "").isdigit() else 0)
    lines = []
    for m in rows:
        kind = str(m.get("type") or "unknown")
        raw = m.get(kind)
        body: dict[str, Any] = raw if isinstance(raw, dict) else {}
        if kind == "text":
            text = str(body.get("body") or "")
        elif kind == "poll":
            text = f"[poll] {body.get('title') or ''}"
        else:
            text = f"[{kind}] {body.get('caption') or body.get('file_name') or ''}".strip()
        try:
            when = datetime.fromtimestamp(int(str(m.get("timestamp"))), UTC).strftime("%Y-%m-%d %H:%M")
        except (TypeError, ValueError, OverflowError, OSError):
            when = "unknown time"
        lines.append(f"[{when} UTC] sent by this number (outgoing), message id {m['id']}: {text}")
    return lines


def format_tally(message_id: str, tally: dict[str, Any]) -> str:
    """The tally as text an agent can answer "who voted what" from."""
    lines = [f'Poll {message_id}: "{tally["title"]}", {tally["total"]} vote(s).']
    for option in tally["options"]:
        voters = ", ".join(option["voters"]) if option["voters"] else "nobody"
        lines.append(f"- {option['name']} (option {option['id'] or '?'}): {option['count']} vote(s), by {voters}")
    return "\n".join(lines)


class GatewayClient:
    """One gateway vendor's REST surface. Subclasses map their API to these calls."""

    vendor = ""

    def __init__(self, base_url: str, channel_token: str) -> None:
        self.base_url = (base_url or "").rstrip("/")
        self.headers = bearer_auth_headers(channel_token, extra={"Content-Type": "application/json"})

    async def _call(self, method: str, path: str, action: str, **kwargs: Any) -> tuple[Any, str | None]:
        async with egress_client(timeout=60) as client:
            resp, err = await safe_request(
                client, method, f"{self.base_url}{path}", _PROVIDER, action, headers=self.headers, **kwargs
            )
        if err:
            return None, err
        try:
            return resp.json(), None
        except ValueError:
            return {}, None

    def pairing_state(self, health: dict[str, Any]) -> str:  # pragma: no cover - interface
        raise NotImplementedError

    async def health(self) -> tuple[Any, str | None]:  # pragma: no cover - interface
        raise NotImplementedError

    async def login_qr(self) -> tuple[Any, str | None]:  # pragma: no cover - interface
        raise NotImplementedError

    async def send_text(self, chat: str, text: str, typing: int = 0) -> tuple[Any, str | None]:  # pragma: no cover
        """Send *text*; *typing* > 0 shows the typing indicator that many seconds first."""
        raise NotImplementedError

    async def send_media(
        self, chat: str, kind: str, data: bytes, mime: str, filename: str, caption: str
    ) -> tuple[Any, str | None]:  # pragma: no cover - interface
        raise NotImplementedError

    async def react(self, message_id: str, emoji: str) -> tuple[Any, str | None]:  # pragma: no cover
        raise NotImplementedError

    async def send_poll(self, payload: dict[str, Any]) -> tuple[Any, str | None]:  # pragma: no cover
        raise NotImplementedError

    async def get_message(self, message_id: str) -> tuple[Any, str | None]:  # pragma: no cover
        raise NotImplementedError

    async def edit_text(self, chat: str, message_id: str, text: str) -> tuple[Any, str | None]:  # pragma: no cover
        """Replace the text of a message the number sent."""
        raise NotImplementedError

    async def delete_message(self, message_id: str) -> tuple[Any, str | None]:  # pragma: no cover
        """Delete a message for everyone in its chat."""
        raise NotImplementedError

    async def list_sent(self, chat: str, count: int) -> tuple[Any, str | None]:  # pragma: no cover
        """The latest *count* messages the number sent in *chat*."""
        raise NotImplementedError

    async def list_groups(self, count: int, offset: int) -> tuple[Any, str | None]:  # pragma: no cover
        raise NotImplementedError

    async def download_media(self, media_id: str, max_bytes: int) -> tuple[bytes, str | None]:  # pragma: no cover
        """The bytes of a received media (a voice note), at most *max_bytes*; ``(b"", reason)`` on failure."""
        raise NotImplementedError

    async def get_group(self, group_id: str) -> tuple[Any, str | None]:  # pragma: no cover
        raise NotImplementedError


class WhapiClient(GatewayClient):
    """Whapi.Cloud (https://gate.whapi.cloud), bearer channel token."""

    vendor = "whapi"

    #: Whapi's channel status texts (GET /health ``status.text``) by pairing state.
    _STATES = {
        "AUTH": "connected",
        "QR": "qr",
        "BANNED": "banned",
        "SYNC_ERROR": "sync_error",
        "ERROR": "sync_error",
        "INIT": "starting",
        "LAUNCH": "starting",
        "LAUNCHED": "starting",
    }

    def pairing_state(self, health: dict[str, Any]) -> str:
        status = health.get("status") if isinstance(health, dict) else None
        text = str((status or {}).get("text") if isinstance(status, dict) else status or "").strip().upper()
        return self._STATES.get(text, "unknown")

    async def health(self) -> tuple[Any, str | None]:
        return await self._call("get", "/health", "read channel health")

    async def login_qr(self) -> tuple[Any, str | None]:
        return await self._call("get", "/users/login", "read pairing QR code")

    async def send_text(self, chat: str, text: str, typing: int = 0) -> tuple[Any, str | None]:
        body: dict[str, Any] = {"to": chat, "body": text}
        if typing > 0:
            body["typing_time"] = typing
        return await self._call("post", "/messages/text", "send message", json=body)

    async def send_media(
        self, chat: str, kind: str, data: bytes, mime: str, filename: str, caption: str
    ) -> tuple[Any, str | None]:
        body: dict[str, Any] = {
            "to": chat,
            "media": f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}",
        }
        if caption:
            body["caption"] = caption
        if kind == "document" and filename:
            body["filename"] = filename
        return await self._call("post", f"/messages/{kind}", "send media", json=body)

    async def react(self, message_id: str, emoji: str) -> tuple[Any, str | None]:
        return await self._call("put", f"/messages/{message_id}/reaction", "react to message", json={"emoji": emoji})

    async def send_poll(self, payload: dict[str, Any]) -> tuple[Any, str | None]:
        return await self._call("post", "/messages/poll", "send poll", json=payload)

    async def get_message(self, message_id: str) -> tuple[Any, str | None]:
        return await self._call("get", f"/messages/{message_id}", "read message")

    async def edit_text(self, chat: str, message_id: str, text: str) -> tuple[Any, str | None]:
        # Whapi edits through the text send, the edited message's id in ``edit``.
        body = {"to": chat, "body": text, "edit": message_id}
        return await self._call("post", "/messages/text", "edit message", json=body)

    async def delete_message(self, message_id: str) -> tuple[Any, str | None]:
        return await self._call("delete", f"/messages/{message_id}", "delete message")

    async def list_sent(self, chat: str, count: int) -> tuple[Any, str | None]:
        params = {"count": count, "from_me": "true", "sort": "desc"}
        return await self._call("get", f"/messages/list/{list_chat_id(chat)}", "list sent messages", params=params)

    async def list_groups(self, count: int, offset: int) -> tuple[Any, str | None]:
        return await self._call("get", "/groups", "list groups", params={"count": count, "offset": offset})

    async def get_group(self, group_id: str) -> tuple[Any, str | None]:
        return await self._call("get", f"/groups/{group_id}", "read group")

    async def download_media(self, media_id: str, max_bytes: int) -> tuple[bytes, str | None]:
        if not re.fullmatch(r"[A-Za-z0-9._=-]{1,200}", media_id or ""):
            return b"", "not a media id"
        headers = {k: v for k, v in self.headers.items() if k.lower() != "content-type"}
        async with egress_client(timeout=60) as client:
            resp, err = await safe_request(
                client, "get", f"{self.base_url}/media/{media_id}", _PROVIDER, "download media", headers=headers
            )
        if err:
            return b"", err
        data = resp.content or b""
        if len(data) > max_bytes:
            return b"", f"larger than {max_bytes // (1024 * 1024)} MB"
        return data, None


#: Gateway vendors by key: a second vendor is one subclass added here.
GATEWAY_CLIENTS: dict[str, type[GatewayClient]] = {WhapiClient.vendor: WhapiClient}


def parse_gateway_token(token: str) -> tuple[str, str, str] | None:
    """``(vendor, base_url, channel_token)`` of a gateway credential, or ``None``."""
    if not is_gateway_token(token):
        return None
    _, vendor, base_url, channel_token = (p.strip() for p in parse_compound_token(token, 4, greedy_last=True))
    vendor = vendor or DEFAULT_VENDOR
    if vendor not in GATEWAY_CLIENTS or not channel_token:
        return None
    return vendor, base_url or WHAPI_BASE_URL, channel_token


def sniffed_mime(data: bytes) -> str:
    """The type of a downloaded media from its first bytes (the gateway's download answers no type)."""
    head = data[:12]
    if head.startswith(b"OggS"):
        return "audio/ogg"
    if head[4:8] == b"ftyp":
        return "audio/mp4" if head[8:11] in {b"M4A", b"m4a"} else "video/mp4"
    if head.startswith(b"ID3") or head[:2] in {b"\xff\xfb", b"\xff\xf3"}:
        return "audio/mpeg"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith(b"\x89PNG"):
        return "image/png"
    if head.startswith(b"RIFF") and head[8:12] == b"WEBP":
        return "image/webp"
    if head.startswith(b"%PDF"):
        return "application/pdf"
    return "application/octet-stream"


def _media_kind(mime: str) -> str:
    if mime in {"image/jpeg", "image/png", "image/webp"}:
        return "image"
    if mime.startswith("video/"):
        return "video"
    if mime.startswith("audio/"):
        return "audio"
    return "document"


def _sent_id(data: Any) -> str:
    message = data.get("message") if isinstance(data, dict) else None
    if isinstance(message, dict) and message.get("id"):
        return str(message["id"])
    return "unknown"


def _error(text: str) -> ToolResult:
    return ToolResult(tool_call_id="", content=text, is_error=True)


_RECIPIENT_ERROR = "Error: 'to' must be a phone number in international format or a group id ending in @g.us."


def gateway_only_definitions() -> list[ToolDefinition]:
    """The tools only the gateway backend has (the catalog lists them for both backends)."""
    return [d for d in _definitions() if d.name in GATEWAY_ONLY_TOOLS]


GATEWAY_ONLY_TOOLS = frozenset(
    {
        "send_whatsapp_poll",
        "get_whatsapp_poll_results",
        "get_whatsapp_pairing_status",
        "get_whatsapp_pairing_qr",
        "edit_whatsapp_message",
        "delete_whatsapp_message",
    }
)


def build_tools(token: str, executor: CallableToolExecutor, **kwargs: Any) -> list[ToolDefinition]:
    """Build the gateway tools; *token* is a :func:`gateway_token`."""
    parsed = parse_gateway_token(token)
    if parsed is None:
        logger.warning("whatsapp gateway token invalid - expected linked_device_gateway||vendor||base_url||token")
        return []
    vendor, base_url, channel_token = parsed
    client = GATEWAY_CLIENTS[vendor](base_url, channel_token)
    persist_file_fn = kwargs.get("persist_file_fn")
    integration_id = str(kwargs.get("integration_id") or "")

    async def get_whatsapp_pairing_status() -> str:
        data, err = await client.health()
        if err:
            return err
        state = client.pairing_state(data)
        hint = {
            "connected": "The number is paired and online.",
            "qr": "Not paired: open get_whatsapp_pairing_qr and scan the code from WhatsApp > Linked devices.",
            "banned": "WhatsApp banned this number. Pair another, dedicated number.",
            "sync_error": "The gateway lost sync with the phone: keep the phone online, then pair again if it stays.",
            "starting": "The channel is starting; check again in a minute.",
        }.get(state, "The gateway answered an unknown status.")
        return f"WhatsApp gateway ({vendor}) pairing status: {state}. {hint}"

    async def get_whatsapp_pairing_qr() -> str | ToolResult:
        data, err = await client.login_qr()
        if err:
            return err
        encoded = str((data or {}).get("base64") or "") if isinstance(data, dict) else ""
        if not encoded:
            status = (data or {}).get("status") if isinstance(data, dict) else None
            return f"No QR code to scan (gateway status: {status or 'unknown'}); the number may already be paired."
        raw = encoded.split(",", 1)[1] if encoded.startswith("data:") else encoded
        try:
            image = base64.b64decode(raw, validate=False)
        except ValueError:
            return _error("Error: the gateway returned a QR code that is not base64.")
        return await deliver_file(
            data=image,
            filename="whatsapp-pairing-qr.png",
            content_type="image/png",
            source="WhatsApp gateway",
            persist_file_fn=persist_file_fn,
        )

    async def send_whatsapp_message(to: str, text: str) -> str:
        chat = chat_id(to)
        if chat is None:
            return _RECIPIENT_ERROR
        if not (text or "").strip():
            return "Error: text is empty."
        # WhatsApp draws no Markdown: a link is sent as its full URL (#10115).
        text = markdown_links_as_plain_urls(text)
        refused = await pace_send(integration_id, chat)
        if refused:
            return refused
        logger.debug("whatsapp gateway send to %s (%d characters)", chat, len(text))
        data, err = await client.send_text(chat, text[:_MAX_TEXT], typing=typing_seconds(text[:_MAX_TEXT]))
        if err:
            return err
        cut = f" (cut to {_MAX_TEXT} characters)" if len(text) > _MAX_TEXT else ""
        return f"WhatsApp message sent to {chat}{cut} (message ID: {_sent_id(data)})."

    async def send_whatsapp_media(
        to: str, file_id: str, caption: str = "", filename: str = "", file_context: FileContext | None = None
    ) -> str | ToolResult:
        chat = chat_id(to)
        if chat is None:
            return _error(_RECIPIENT_ERROR)
        piped = await read_pipe_file(file_context, file_id, max_bytes=_MAX_MEDIA_BYTES, target="WhatsApp")
        if isinstance(piped, str):
            return _error(piped)
        data, piped_name, mime = piped
        kind = _media_kind(mime)
        refused = await pace_send(integration_id, chat)
        if refused:
            return _error(refused)
        sent, err = await client.send_media(chat, kind, data, mime, (filename or "").strip() or piped_name, caption)
        if err:
            return err
        return f"WhatsApp {kind} sent to {chat} (message ID: {_sent_id(sent)})."

    async def react_whatsapp_message(to: str, message_id: str, emoji: str) -> str:
        if not (message_id or "").strip():
            return "Error: message_id is required."
        _, err = await client.react(message_id.strip(), emoji or "")
        if err:
            return err
        return f"Reacted to message {message_id} with {emoji}." if emoji else f"Reaction removed from {message_id}."

    async def edit_whatsapp_message(to: str, message_id: str, text: str) -> str | ToolResult:
        chat = chat_id(to)
        if chat is None:
            return _error(_RECIPIENT_ERROR)
        message_id = (message_id or "").strip()
        if not message_id:
            return _error("Error: message_id is required (the id send_whatsapp_message answered).")
        if not (text or "").strip():
            return _error("Error: text is empty; to remove the message use delete_whatsapp_message.")
        if len(text) > _MAX_TEXT:
            return _error(f"Error: text is over {_MAX_TEXT} characters; nothing was changed.")
        message, _ = await client.get_message(message_id)
        refused = edit_refusal(message, message_id, time.time())
        if refused:
            return _error(refused)
        refused = await pace_send(integration_id, chat)
        if refused:
            return _error(refused)
        logger.debug("whatsapp gateway edit in %s (%d characters)", chat, len(text))
        _, err = await client.edit_text(chat, message_id, text)
        if err:
            return err
        return f"WhatsApp message {message_id} in {chat} edited; the recipient sees it marked Edited."

    async def delete_whatsapp_message(to: str, message_id: str, for_everyone: bool = True) -> str | ToolResult:
        chat = chat_id(to)
        if chat is None:
            return _error(_RECIPIENT_ERROR)
        message_id = (message_id or "").strip()
        if not message_id:
            return _error("Error: message_id is required.")
        if not for_everyone:
            return _error(
                "Error: the gateway deletes a message for everyone only; a delete for this device alone "
                "is not supported. Nothing was deleted."
            )
        _, err = await client.delete_message(message_id)
        if err:
            return err
        return f"WhatsApp message {message_id} in {chat} deleted for everyone."

    async def send_whatsapp_poll(to: str, title: str, options: list[str], multiple_answers: bool = False) -> str:
        payload = poll_payload(to, title, options, multiple_answers)
        if isinstance(payload, str):
            return payload
        refused = await pace_send(integration_id, str(payload["to"]))
        if refused:
            return refused
        data, err = await client.send_poll(payload)
        if err:
            return err
        msg_id = _sent_id(data)
        return (
            f'Poll "{payload["title"]}" sent to {payload["to"]} with {len(payload["options"])} options '
            f"(message ID: {msg_id}). Read the votes with get_whatsapp_poll_results(message_id='{msg_id}')."
        )

    async def get_whatsapp_poll_results(message_id: str) -> str:
        message_id = (message_id or "").strip()
        if not message_id:
            return "Error: message_id is required."
        if integration_id:
            # The votes the inbound webhook stored; the gateway is asked
            # only for a poll the webhook never saw.
            from app.services.whatsapp_gateway_inbound import stored_tally

            stored = await stored_tally(integration_id, message_id)
            if stored is not None:
                return format_tally(message_id, stored)
        data, err = await client.get_message(message_id)
        if err:
            return err
        tally = poll_tally(data or {})
        if tally is None:
            return f"Message {message_id} is not a poll."
        return format_tally(message_id, tally)

    async def list_whatsapp_groups(limit: int = 25, after: str = "") -> str:
        count = max(1, min(int(limit or 25), 500))
        offset = int(after) if str(after or "").isdigit() else 0
        data, err = await client.list_groups(count, offset)
        if err:
            return err
        groups = (data or {}).get("groups") if isinstance(data, dict) else None
        if not groups:
            return "The paired number is in no WhatsApp group."
        lines = [
            f"- {g.get('name') or '(no subject)'}: group id {g.get('id')}, "
            f"{len(g.get('participants') or [])} participant(s)"
            for g in groups
            if isinstance(g, dict)
        ]
        total = int((data or {}).get("total") or 0)
        more = f"\nMore groups: call again with after='{offset + count}'." if total and offset + count < total else ""
        return f"{len(lines)} WhatsApp group(s):\n" + "\n".join(lines) + more

    async def get_whatsapp_group(group_id: str) -> str:
        group = (group_id or "").strip()
        if not _GROUP_RE.match(group):
            return "Error: group_id must be a group id ending in @g.us (from list_whatsapp_groups)."
        data, err = await client.get_group(group)
        if err:
            return err
        participants = (data or {}).get("participants") or []
        members = [
            f"- {p.get('id')}{' (' + p['rank'] + ')' if p.get('rank') and p.get('rank') != 'member' else ''}"
            for p in participants
            if isinstance(p, dict)
        ]
        return (
            f"WhatsApp group {data.get('name') or '(no subject)'} ({group}), {len(members)} participant(s):\n"
            + "\n".join(members)
        )

    async def download_whatsapp_media(media_id: str, filename: str = "") -> str | ToolResult:
        """A received media (a voice note's raw audio included) as a file of the conversation."""
        media_id = (media_id or "").strip()
        if not media_id:
            return _error("Error: media_id is required (read_whatsapp_messages names it on a voice note).")
        data, err = await client.download_media(media_id, _MAX_MEDIA_BYTES)
        if err:
            return _error(f"Error: media {media_id} not downloaded: {err}")
        if not data:
            return _error(f"Error: media {media_id} downloaded empty.")
        return await deliver_file(
            data=data,
            filename=(filename or "").strip() or f"whatsapp-{media_id}",
            content_type=sniffed_mime(data),
            source="WhatsApp gateway",
            persist_file_fn=persist_file_fn,
        )

    async def list_whatsapp_conversations(limit: int = 25) -> str | ToolResult:
        from app.services import whatsapp_inbound

        if not integration_id:
            return _error("Error: received messages are only available to an agent bound to the integration.")
        return await whatsapp_inbound.conversations_text(
            integration_id,
            limit,
            "No WhatsApp message received yet. Inbound messages need the gateway channel's webhook pointed at "
            "this integration's webhook URL (Integrations > WhatsApp) with its webhook_secret.",
        )

    async def read_whatsapp_messages(conversation_id: str, limit: int = 50) -> str | ToolResult:
        from app.services import whatsapp_inbound

        if not integration_id:
            return _error("Error: received messages are only available to an agent bound to the integration.")
        conversation_id = (conversation_id or "").strip()
        if not conversation_id:
            return _error("Error: conversation_id is required (a phone number or a group id).")
        received = await whatsapp_inbound.messages_text(integration_id, conversation_id, limit)
        chat = chat_id(conversation_id)
        if chat is None:
            return received
        # What the number sent is not in the inbound store: read it from the gateway.
        data, err = await client.list_sent(chat, max(1, min(int(limit or 50), _MAX_SENT_READ)))
        if err:
            return f"{received}\n\nSent messages not read: {err}"
        lines = sent_lines((data or {}).get("messages") if isinstance(data, dict) else None)
        if not lines:
            return f"{received}\n\nNo message sent by this number in WhatsApp conversation {conversation_id}."
        from app.services.content_safety import wrap_untrusted

        sent = wrap_untrusted(
            f"{len(lines)} message(s) sent by this number (outgoing) in WhatsApp conversation "
            f"{conversation_id}, oldest first:\n" + "\n".join(lines),
            "WhatsApp sent messages",
        )
        return f"{received}\n\n{sent}"

    handlers = {
        "list_whatsapp_conversations": list_whatsapp_conversations,
        "read_whatsapp_messages": read_whatsapp_messages,
        "get_whatsapp_pairing_status": get_whatsapp_pairing_status,
        "get_whatsapp_pairing_qr": get_whatsapp_pairing_qr,
        "send_whatsapp_message": send_whatsapp_message,
        "send_whatsapp_media": send_whatsapp_media,
        "react_whatsapp_message": react_whatsapp_message,
        "edit_whatsapp_message": edit_whatsapp_message,
        "delete_whatsapp_message": delete_whatsapp_message,
        "send_whatsapp_poll": send_whatsapp_poll,
        "get_whatsapp_poll_results": get_whatsapp_poll_results,
        "list_whatsapp_groups": list_whatsapp_groups,
        "get_whatsapp_group": get_whatsapp_group,
        "download_whatsapp_media": download_whatsapp_media,
    }
    for name, handler in handlers.items():
        executor.register(name, handler)
    return _definitions()


def _definitions() -> list[ToolDefinition]:
    to_prop = {
        "type": "string",
        "description": "A phone number in international format (e.g. '14155552671') or a group id ending in @g.us",
    }
    return [
        ToolDefinition(
            name="get_whatsapp_pairing_status",
            # Polled: the same call later answers a state that moved on its own.
            live_state=True,
            description=(
                "Linked-device gateway backend: whether the WhatsApp number is paired "
                "(connected, qr, banned, sync_error, starting)."
            ),
            input_schema={"type": "object", "properties": {}},
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="get_whatsapp_pairing_qr",
            description=(
                "Linked-device gateway backend: the QR code to scan from WhatsApp > Linked devices on the "
                "phone, as an image file of the conversation. Empty when the number is already paired."
            ),
            input_schema={"type": "object", "properties": {}},
            source=_SOURCE,
            priority=2,
        ),
        ToolDefinition(
            name="send_whatsapp_message",
            description=(
                "Send a text (up to 4096 characters) to a WhatsApp number or group. Answers the sent message id "
                "(for edit_whatsapp_message or delete_whatsapp_message)."
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
            name="send_whatsapp_media",
            description=(
                "Send a file of this conversation to a WhatsApp number or group: an image, a video, audio or "
                "a document, with an optional caption."
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
            name="react_whatsapp_message",
            description="React to a WhatsApp message with one emoji (an empty emoji removes the reaction).",
            input_schema={
                "type": "object",
                "properties": {
                    "to": to_prop,
                    "message_id": {"type": "string", "description": "WhatsApp message id"},
                    "emoji": {"type": "string", "description": "One Unicode emoji"},
                },
                "required": ["to", "message_id", "emoji"],
            },
            source=_SOURCE,
            priority=2,
        ),
        ToolDefinition(
            name="edit_whatsapp_message",
            description=(
                "Linked-device gateway backend: replace the text of a text message this number sent, in place "
                "(the recipient sees it marked Edited). WhatsApp allows it within 15 minutes of sending; past "
                "that, or on a message the number did not send, it is refused with the reason."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "to": to_prop,
                    "message_id": {
                        "type": "string",
                        "description": (
                            "The message id send_whatsapp_message answered, or one read_whatsapp_messages lists as sent"
                        ),
                    },
                    "text": {"type": "string", "description": "The new text (up to 4096 characters)"},
                },
                "required": ["to", "message_id", "text"],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="delete_whatsapp_message",
            description=(
                "Linked-device gateway backend: delete a message for everyone in its chat (the chat shows "
                "'This message was deleted')."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "to": to_prop,
                    "message_id": {"type": "string", "description": "The message id"},
                    "for_everyone": {
                        "type": "boolean",
                        "default": True,
                        "description": "Delete for every participant (the only mode the gateway supports)",
                    },
                },
                "required": ["to", "message_id"],
            },
            source=_SOURCE,
            priority=2,
        ),
        ToolDefinition(
            name="send_whatsapp_poll",
            description=(
                "Linked-device gateway backend: send a WhatsApp poll (2 to 12 distinct options) to a number or "
                "a group, single choice by default. Read the votes with get_whatsapp_poll_results."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "to": to_prop,
                    "title": {"type": "string", "description": "The poll question"},
                    "options": {
                        "type": "array",
                        "items": {"type": "string"},
                        "minItems": _MIN_POLL_OPTIONS,
                        "maxItems": _MAX_POLL_OPTIONS,
                        "description": "2 to 12 distinct options",
                    },
                    "multiple_answers": {
                        "type": "boolean",
                        "default": False,
                        "description": "Let a voter select several options",
                    },
                },
                "required": ["to", "title", "options"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="get_whatsapp_poll_results",
            # Polled: the same call later answers a state that moved on its own.
            live_state=True,
            description=(
                "Linked-device gateway backend: the tally of a poll this number sent or received: votes "
                "per option and who voted for what (from the votes the webhook received, else live from the gateway)."
            ),
            input_schema={
                "type": "object",
                "properties": {"message_id": {"type": "string", "description": "The poll's message id"}},
                "required": ["message_id"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="list_whatsapp_groups",
            description="List the WhatsApp groups the number is in (id ending in @g.us, subject, participants).",
            input_schema={
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "default": 25, "description": "Groups per page"},
                    "after": {"type": "string", "description": "Cursor from a previous page"},
                },
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="get_whatsapp_group",
            description="Read one WhatsApp group: subject and participants with their role.",
            input_schema={
                "type": "object",
                "properties": {"group_id": {"type": "string", "description": "Group id ending in @g.us"}},
                "required": ["group_id"],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="download_whatsapp_media",
            workspace_ingest=True,
            description=(
                "Download a received WhatsApp media by its media id into the conversation: a download card "
                "and a file_id to forward to any tool that takes a file. A voice note's line in "
                "read_whatsapp_messages names its media id, so its raw audio can be heard again."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "media_id": {"type": "string", "description": "WhatsApp media id"},
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
                "List the WhatsApp conversations (a phone number or a group id) the paired number received "
                "messages in, newest first, with the last message (polls and group events included). "
                "Messages are kept for the integration's retention (30 days by default)."
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
                "message id (for react, or get_whatsapp_poll_results on a poll) and text; on the linked-device "
                "gateway backend also the messages this number sent there, flagged outgoing, with their id."
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
    ]
