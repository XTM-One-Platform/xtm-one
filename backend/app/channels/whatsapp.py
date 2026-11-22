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

"""WhatsApp channel adapter – Meta Cloud API.

Supports both **individual (DM)** and **group** conversations.

- DMs: ``channel_thread_id`` is the sender's phone number.
- Groups: ``channel_thread_id`` is the group WA ID (``group_id``
  from the webhook payload).  Replies use ``recipient_type: "group"``.

Credentials (access_token, phone_number_id, verify_token) are stored
per channel bot in the DB as a "whatsapp_bot" provider.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import mimetypes
from dataclasses import dataclass
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from app.channels.base import (
    CHANNEL_MAX_CHARS,
    DECRYPT_FAILED_CREDENTIAL,
    FILE_ONLY_PLACEHOLDER,
    BaseChannel,
    ChannelType,
    InboundFileAttachment,
    InboundMessage,
    OutboundMessage,
    chunk_message,
    get_channel_bot,
    log_id,
    spawn_channel_task,
    timing_safe_str_eq,
    warn_unauthenticated_once,
)
from app.channels.router import handle_message
from app.core.utils import format_exception_message
from app.services import whatsapp_inbound
from app.services.channel_notices import notice, notice_language
from app.services.outbound_links import markdown_links_as_plain_urls
from app.services.vault import reveal_field, reveal_json_field

logger = logging.getLogger(__name__)
router = APIRouter()

_BASE_URL = "https://graph.facebook.com/v19.0"


class WhatsAppChannel(BaseChannel):
    channel_type = ChannelType.WHATSAPP
    # The Cloud API exposes no typing endpoint — skip the keepalive loop.
    supports_typing = False

    def __init__(self, phone_number_id: str, access_token: str):
        self.phone_number_id = phone_number_id
        self.access_token = access_token

    async def send_message(self, message: OutboundMessage) -> str | None:
        url = f"{_BASE_URL}/{self.phone_number_id}/messages"

        # Build payload — group replies use recipient_type "group" and the
        # group WA ID as the recipient instead of an individual phone number.
        meta = message.metadata or {}
        group_id = meta.get("group_id")
        recipient = group_id or message.channel_user_id

        # Send embedded images natively via WhatsApp image messages
        embedded_images = meta.get("embedded_images", [])
        if embedded_images and recipient:
            await self._send_images(url, recipient, group_id, embedded_images)

        # Split long messages using code-fence-aware chunking. WhatsApp draws
        # no Markdown: a link reaches the reader as its full URL (#10115).
        text = markdown_links_as_plain_urls(message.text or "")
        chunks = chunk_message(text, max_chars=CHANNEL_MAX_CHARS.get("whatsapp", 4096))

        last_msg_id: str | None = None
        async with httpx.AsyncClient(timeout=30) as client:
            for chunk in chunks:
                payload: dict[str, object] = {
                    "messaging_product": "whatsapp",
                    "type": "text",
                    "text": {"body": chunk},
                }
                if group_id:
                    payload["recipient_type"] = "group"
                    payload["to"] = group_id
                else:
                    payload["to"] = message.channel_user_id

                resp = await client.post(
                    url,
                    headers={"Authorization": f"Bearer {self.access_token}"},
                    json=payload,
                )
                if resp.status_code == 200:
                    data = resp.json()
                    msgs = data.get("messages", [])
                    if msgs:
                        last_msg_id = msgs[0]["id"]
                else:
                    logger.error(
                        "WhatsApp send_message failed: %s %s",
                        resp.status_code,
                        resp.text[:500],
                    )

        return last_msg_id

    async def _send_images(
        self,
        url: str,
        recipient: str,
        group_id: str | None,
        embedded_images: list[dict],
    ) -> None:
        """Send images via WhatsApp Cloud API using public URLs."""
        for img_info in embedded_images:
            try:
                payload: dict[str, object] = {
                    "messaging_product": "whatsapp",
                    "type": "image",
                    "image": {
                        "link": img_info["public_url"],
                        "caption": img_info.get("alt_text", notice("generated_image", notice_language())),
                    },
                    "to": recipient,
                }
                if group_id:
                    payload["recipient_type"] = "group"

                async with httpx.AsyncClient() as client:
                    resp = await client.post(
                        url,
                        headers={"Authorization": f"Bearer {self.access_token}"},
                        json=payload,
                        timeout=30,
                    )
                    if resp.status_code != 200:
                        logger.warning(
                            "WhatsApp image send failed: %s %s",
                            resp.status_code,
                            resp.text[:300],
                        )
            except Exception as exc:
                logger.warning("WhatsApp image send error: %s", format_exception_message(exc))

    async def send_typing_indicator(self, channel_thread_id: str | None = None) -> None:
        pass


# ── Inbound media ────────────────────────────────────────────────────────────

#: The media message types handed to the router. A sticker is not a file the
#: agent reads.
_MEDIA_TYPES = ("image", "document", "audio", "video")


@dataclass(frozen=True, slots=True)
class _WhatsAppMedia:
    """A media a WhatsApp message carries, before ``GET /{media-id}`` gives its URL."""

    media_id: str
    #: The sender's file name (a document's), empty for other media.
    filename: str
    #: The name of a media without one, before its extension.
    stem: str
    content_type: str


def _str_field(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _message_media(msg: dict) -> tuple[_WhatsAppMedia, str] | None:
    """The media of *msg* and its caption, or ``None`` for a message that carries none.

    A media id is all digits: it is a URL path segment the access token is sent to.
    """
    kind = msg.get("type")
    media = msg.get(kind) if kind in _MEDIA_TYPES else None
    if not isinstance(media, dict):
        return None
    media_id = _str_field(media.get("id"))
    if not (media_id.isascii() and media_id.isdigit()):
        return None
    stem = "voice" if kind == "audio" and media.get("voice") is True else str(kind)
    return (
        _WhatsAppMedia(
            media_id=media_id,
            filename=_str_field(media.get("filename")),
            stem=stem,
            content_type=_str_field(media.get("mime_type")).split(";", 1)[0].strip(),
        ),
        _str_field(media.get("caption")),
    )


def _declared_size(value: object) -> int:
    """``file_size`` of ``GET /{media-id}``: a number, or its digits."""
    if isinstance(value, int) and value > 0:
        return value
    if isinstance(value, str) and value.isascii() and value.isdigit() and len(value) <= 20:
        return int(value)
    return 0


async def _resolve_whatsapp_media(
    access_token: str, phone_number_id: str, media: _WhatsAppMedia, max_bytes: int
) -> InboundFileAttachment | None:
    """The download of *media* ``GET /{media-id}`` names, or ``None`` when WhatsApp cannot serve it.

    The media URL expires after five minutes and is downloaded with the
    access token, which is only ever sent to a Meta host. ``phone_number_id``
    makes WhatsApp answer only for media of this bot's number. No line logs
    the media URL or a file name.
    """
    from app.integrations.providers.whatsapp_bot import _is_meta_media_host

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                f"{_BASE_URL}/{media.media_id}",
                params={"phone_number_id": phone_number_id},
                headers={"Authorization": f"Bearer {access_token}"},
            )
            resp.raise_for_status()
            meta = resp.json()
    except httpx.HTTPStatusError as exc:
        logger.warning(
            "Failed to resolve WhatsApp media: %s (HTTP %d)", media.content_type, exc.response.status_code
        )
        return None
    except Exception as exc:
        # The lookup URL carries the media id only, never the token (a header).
        logger.warning("Failed to resolve WhatsApp media: %s (%s)", media.content_type, format_exception_message(exc))
        return None
    url = _str_field(meta.get("url")) if isinstance(meta, dict) else ""
    if not _is_meta_media_host(url):
        logger.warning("Failed to resolve WhatsApp media: %s (no Meta media URL)", media.content_type)
        return None
    size = _declared_size(meta.get("file_size"))
    if size > max_bytes:
        logger.warning("WhatsApp media too large (%d bytes > %d): %s", size, max_bytes, media.content_type)
        return None
    content_type = (
        media.content_type
        or _str_field(meta.get("mime_type")).split(";", 1)[0].strip()
        or "application/octet-stream"
    )
    return InboundFileAttachment(
        url=url,
        filename=media.filename or f"{media.stem}{mimetypes.guess_extension(content_type) or ''}",
        content_type=content_type,
        size=size,
        platform_file_id=media.media_id,
        auth_headers={"Authorization": f"Bearer {access_token}"},
    )


async def _attach_whatsapp_media(
    inbound: InboundMessage, access_token: str, phone_number_id: str, media: list[_WhatsAppMedia] | None
) -> None:
    """Add the downloads of *media* to *inbound*'s ``file_attachments``.

    A media WhatsApp cannot serve is left out and the message still goes on,
    as a file the router fails to download is: its caption, or the file-only
    placeholder, is what the agent answers.
    """
    if not media:
        return
    from app.services.file_storage import MAX_UPLOAD_SIZE_BYTES

    resolved = await asyncio.gather(
        *(_resolve_whatsapp_media(access_token, phone_number_id, m, MAX_UPLOAD_SIZE_BYTES) for m in media)
    )
    inbound.file_attachments.extend(attachment for attachment in resolved if attachment)
    logger.info(
        "WhatsApp processing: %d of %d file attachment(s) resolved",
        len(inbound.file_attachments),
        len(media),
    )


async def _get_whatsapp_creds() -> tuple[str, str, str, str] | None:
    """Load WhatsApp credentials from the first connected whatsapp_bot channel bot."""
    bot = await get_channel_bot("whatsapp_bot")
    if not bot:
        return None

    access_token = (await reveal_field(bot, "api_key_enc")) or ""
    phone_number_id = ""
    verify_token = ""
    app_secret = ""
    if bot.extra_config_enc:
        try:
            extra = await reveal_json_field(bot, "extra_config_enc") or {}
            phone_number_id = extra.get("phone_number_id", "")
            verify_token = extra.get("verify_token", "")
            app_secret = extra.get("app_secret", "")
        except Exception as exc:
            logger.warning("Failed to parse WhatsApp bot extra_config: %s", format_exception_message(exc))
            # Credentials exist but are unreadable — fail closed, not open:
            # verification runs with a never-matching value and rejects.
            verify_token = DECRYPT_FAILED_CREDENTIAL
            app_secret = DECRYPT_FAILED_CREDENTIAL

    return access_token, phone_number_id, verify_token, app_secret


def _verify_meta_signature(body: bytes, app_secret: str, signature: str) -> bool:
    """Verify Meta's ``X-Hub-Signature-256`` HMAC over the raw POST body."""
    if not signature.startswith("sha256="):
        return False
    expected = (
        "sha256=" + hmac.new(app_secret.encode(), body, hashlib.sha256).hexdigest()
    )
    # timing_safe_str_eq (not hmac.compare_digest on str): the header value is
    # attacker-controlled and non-ASCII input must yield a mismatch, not a 500.
    return timing_safe_str_eq(expected, signature)


@router.get("/whatsapp/webhook")
async def whatsapp_verify(
    hub_mode: str = Query(None, alias="hub.mode"),
    hub_verify_token: str = Query(None, alias="hub.verify_token"),
    hub_challenge: str = Query(None, alias="hub.challenge"),
):
    creds = await _get_whatsapp_creds()
    # The bot's verify token, then every whatsapp integration's: one Meta app
    # callback URL serves both.
    tokens = [t for t in [creds[2] if creds else ""] if t]
    tokens += await whatsapp_inbound.verify_tokens()
    if not tokens:
        raise HTTPException(
            status_code=403,
            detail="No WhatsApp bot or integration configured or verify token is empty",
        )
    matched = False
    for token in tokens:
        # No early exit: every token is compared, whichever matches.
        matched = timing_safe_str_eq(hub_verify_token or "", token) or matched
    if hub_mode == "subscribe" and matched:
        if not hub_challenge:
            raise HTTPException(
                status_code=400, detail="Missing hub.challenge parameter"
            )
        try:
            return int(hub_challenge)
        except ValueError:
            raise HTTPException(
                status_code=400, detail="Invalid hub.challenge parameter"
            ) from None
    raise HTTPException(status_code=403, detail="Verification failed")


async def _route_to_integrations(body: bytes, data: object, signature: str) -> list[dict]:
    """Store the changes of a ``whatsapp`` integration's number; return the changes left to the bot.

    A change whose ``metadata.phone_number_id`` belongs to a connected
    ``whatsapp`` integration is verified with that integration's
    ``app_secret`` (401 on a mismatch) and recorded for it
    (``services/whatsapp_inbound``), never answered by the channel bot.
    """
    left: list[dict] = []
    if not isinstance(data, dict):
        return left
    for entry in data.get("entry") or []:
        for change in (entry or {}).get("changes") or [] if isinstance(entry, dict) else []:
            value = (change or {}).get("value") if isinstance(change, dict) else None
            if not isinstance(value, dict):
                continue
            phone_number_id = str((value.get("metadata") or {}).get("phone_number_id") or "")
            target = await whatsapp_inbound.integration_for_phone_number(phone_number_id)
            if target is None:
                left.append(change)
                continue
            if target.app_secret:
                if not _verify_meta_signature(body, target.app_secret, signature):
                    logger.warning("WhatsApp webhook rejected: invalid X-Hub-Signature-256 for an integration number")
                    raise HTTPException(status_code=401, detail="Invalid signature")
            else:
                warn_unauthenticated_once(
                    f"whatsapp-integration-{target.integration_id}",
                    "WhatsApp integration webhook is UNAUTHENTICATED: no app_secret on "
                    "the whatsapp integration - add the Meta app secret to enable "
                    "X-Hub-Signature-256 verification",
                )
            stored = await whatsapp_inbound.record_change(target, value)
            logger.info(
                "WhatsApp webhook stored for an integration: integration_id=%s, messages=%d",
                target.integration_id,
                stored,
            )
    return left


@router.get("/whatsapp/gateway/{integration_id}")
async def whatsapp_gateway_webhook_probe(integration_id: str):
    """A browser opening the gateway webhook URL: say it takes POST, so a 404 is not misread."""
    return JSONResponse(
        status_code=405,
        content={
            "detail": "POST only: this is a WhatsApp gateway webhook URL (Integrations > WhatsApp > webhook URL)."
        },
        headers={"Allow": "POST"},
    )


@router.post("/whatsapp/gateway/{integration_id}")
async def whatsapp_gateway_webhook(integration_id: str, request: Request):
    """Inbound webhook of a ``whatsapp`` integration on the linked-device gateway backend.

    One URL per integration, set as the gateway channel's webhook (Whapi.Cloud:
    channel settings > Webhooks, events messages POST/PATCH and groups). The
    delivery carries the integration's ``webhook_secret`` as the ``token`` query
    parameter or the ``X-Webhook-Secret`` header; no secret configured, or a
    mismatch, answers 401 and nothing is stored. An unknown integration, or one
    on the Meta backend, answers 404.
    """
    from app.services import whatsapp_gateway_inbound

    gateway = await whatsapp_gateway_inbound.gateway_integration(integration_id)
    if gateway is None:
        raise HTTPException(status_code=404, detail="No WhatsApp gateway integration at this URL")
    presented = request.query_params.get("token") or request.headers.get("X-Webhook-Secret") or ""
    if not gateway.webhook_secret or not timing_safe_str_eq(presented, gateway.webhook_secret):
        logger.warning("WhatsApp gateway webhook rejected for integration %s: invalid secret", integration_id)
        raise HTTPException(status_code=401, detail="Invalid webhook secret")
    try:
        payload = json.loads(await request.body())
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid JSON") from None
    messages, polls = await whatsapp_gateway_inbound.record_delivery(gateway, payload)
    logger.info(
        "WhatsApp gateway webhook stored: integration_id=%s, messages=%d, polls=%d",
        integration_id,
        messages,
        polls,
    )
    return {"status": "ok", "messages": messages, "polls": polls}


@router.post("/whatsapp/webhook")
async def whatsapp_webhook(request: Request):
    body = await request.body()

    try:
        data = json.loads(body)
    except Exception:
        data = None
    has_changes = isinstance(data, dict) and any(
        isinstance(e, dict) and e.get("changes") for e in data.get("entry") or []
    )
    if has_changes:
        bot_changes = await _route_to_integrations(
            body, data, request.headers.get("X-Hub-Signature-256", "")
        )
        if not bot_changes:
            return {"ok": True}
        data = {**data, "entry": [{"changes": bot_changes}]}

    # Payload authentication: verify Meta's X-Hub-Signature-256 HMAC when an
    # app secret is configured. Without it anyone who discovers the URL can
    # inject forged inbound messages.
    creds = await _get_whatsapp_creds()
    app_secret = creds[3] if creds else ""
    if app_secret:
        signature = request.headers.get("X-Hub-Signature-256", "")
        if not _verify_meta_signature(body, app_secret, signature):
            logger.warning("WhatsApp webhook rejected: invalid X-Hub-Signature-256")
            raise HTTPException(status_code=401, detail="Invalid signature")
    else:
        warn_unauthenticated_once(
            "whatsapp",
            "WhatsApp webhook is UNAUTHENTICATED: no app_secret configured on "
            "the whatsapp_bot channel bot — add the Meta app secret in "
            "Settings > Integrations > Channels & Bots to enable X-Hub-Signature-256 verification",
        )

    if not isinstance(data, dict):
        logger.warning("WhatsApp webhook received invalid JSON payload")
        return {"ok": True}

    entries = data.get("entry", [])
    for entry in entries:
        changes = entry.get("changes", [])
        for change in changes:
            value = change.get("value", {})
            messages = value.get("messages", [])
            contacts = value.get("contacts", [])

            for msg in messages:
                # A decision button or list reply (#842, S12): answered by its
                # payload id, never by its text, and never routed to an agent.
                if (decision_reply := _decision_reply_id(msg)) is not None:
                    spawn_channel_task(
                        _answer_decision_reply(msg.get("from", ""), decision_reply),
                        name="whatsapp-decision-reply",
                    )
                    continue
                # A text message, or a media message whose text is its
                # caption (a bare media gets the file-only placeholder).
                media: list[_WhatsAppMedia] = []
                if msg.get("type") == "text":
                    text = ((msg.get("text") or {}).get("body") or "").strip()
                elif found := _message_media(msg):
                    media = [found[0]]
                    text = found[1] or FILE_ONLY_PLACEHOLDER
                else:
                    continue
                from_number = msg.get("from", "")
                msg_id = msg.get("id", "")

                if not text:
                    continue

                # Detect group messages — the Cloud API includes group_id
                # at the message level or inside the context object.
                group_id = msg.get("group_id") or (
                    (msg.get("context") or {}).get("group_id")
                )
                is_group = bool(group_id)

                contact_name = ""
                for contact in contacts:
                    if contact.get("wa_id") == from_number:
                        contact_name = contact.get("profile", {}).get("name", "")
                        break

                logger.info(
                    "WhatsApp webhook received: chat_type=%s, group_id=%s, msg_id=%s, text_length=%d, files=%d",
                    "group" if is_group else "private",
                    log_id(ChannelType.WHATSAPP, group_id),
                    log_id(ChannelType.WHATSAPP, msg_id),
                    len(text),
                    len(media),
                )

                inbound = InboundMessage(
                    channel=ChannelType.WHATSAPP,
                    text=text,
                    user_email=None,
                    channel_user_id=from_number,
                    # Groups: thread by group ID.  DMs: thread by phone number.
                    channel_thread_id=group_id if is_group else from_number,
                    channel_message_id=msg_id,
                    raw=msg,
                    metadata={
                        "contact_name": contact_name,
                        "chat_type": "group" if is_group else "private",
                        "group_id": group_id,
                    },
                )

                # The media's ``GET /{media-id}`` is made there too, off the
                # webhook's answer.
                spawn_channel_task(
                    _process_whatsapp_message(inbound, media), name="whatsapp-message"
                )

    return {"ok": True}


def _decision_reply_id(msg: dict[str, Any]) -> str | None:
    """The reply id of a 1:1 decision button or list reply, or ``None`` for any other message."""
    from app.services.decision_whatsapp import parse_reply_id

    if msg.get("type") != "interactive" or msg.get("group_id"):
        return None
    interactive = msg.get("interactive") or {}
    if not isinstance(interactive, dict):
        return None
    reply = interactive.get("button_reply") or interactive.get("list_reply") or {}
    raw = reply.get("id") if isinstance(reply, dict) else None
    return str(raw) if parse_reply_id(raw) is not None else None


async def _answer_decision_reply(from_number: str, raw_reply_id: str) -> None:
    """Answer the decision a reply names as the sender's linked account, then say the outcome in the chat."""
    from app.core.database import async_session
    from app.services.decision_whatsapp import answer_reply

    language = notice_language()
    try:
        async with async_session() as db:
            text = await answer_reply(db, from_number, raw_reply_id, language)
    except Exception as exc:
        logger.error("WhatsApp decision reply: EXCEPTION, %s", format_exception_message(exc), exc_info=True)
        text = notice("card_decision_failed", language)
    creds = await _get_whatsapp_creds()
    if not creds or not creds[0] or not creds[1]:
        logger.warning("WhatsApp decision reply: answered, but no whatsapp_bot channel bot to confirm it")
        return
    await WhatsAppChannel(creds[1], creds[0]).send_message(OutboundMessage(text=text, channel_user_id=from_number))


async def _process_whatsapp_message(
    inbound: InboundMessage, media: list[_WhatsAppMedia] | None = None
) -> None:
    """Process a WhatsApp message in the background.

    *media* is the message's media: its download is resolved here and handed
    to the router as ``file_attachments``, which the router downloads, stores
    and attaches like any channel's.
    """
    chat_type = (inbound.metadata or {}).get("chat_type", "private")

    logger.info(
        "WhatsApp processing: started — chat_type=%s, msg_id=%s, text_length=%d",
        chat_type,
        log_id(inbound.channel, inbound.channel_message_id),
        len(inbound.text or ""),
    )

    try:
        creds = await _get_whatsapp_creds()
        if not creds:
            logger.warning(
                "WhatsApp processing: FAILED — no whatsapp_bot channel bot configured"
            )
            return

        access_token, phone_number_id = creds[0], creds[1]
        if not phone_number_id or not access_token:
            logger.warning("WhatsApp processing: FAILED — credentials incomplete")
            return

        logger.info(
            "WhatsApp processing: routing to handle_message — msg_id=%s, chat_type=%s",
            log_id(inbound.channel, inbound.channel_message_id),
            chat_type,
        )

        # No typing_keepalive here: WhatsAppChannel.send_typing_indicator is
        # a no-op (the Cloud API has no typing signal), so the keepalive loop
        # would just burn a wakeup every 3 seconds for nothing.
        channel = WhatsAppChannel(phone_number_id, access_token)
        await _attach_whatsapp_media(inbound, access_token, phone_number_id, media)
        outbound = await handle_message(inbound)

        if outbound.suppress:
            logger.info(
                "WhatsApp processing: suppressing reply — message was "
                "silently dropped ([NO_RESPONSE] or agent rejected channel). "
                "chat_type=%s, msg_id=%s",
                chat_type,
                log_id(inbound.channel, inbound.channel_message_id),
            )
            return

        # Carry group metadata to the outbound so send_message knows
        # whether to use recipient_type "group" and the group WA ID.
        group_id = (inbound.metadata or {}).get("group_id")
        if group_id:
            outbound.metadata = {**(outbound.metadata or {}), "group_id": group_id}

        outbound.channel_user_id = inbound.channel_user_id

        logger.info(
            "WhatsApp processing: sending reply — length=%d chars",
            len(outbound.text) if outbound.text else 0,
        )
        msg_id = await channel.send_message(outbound)
        if msg_id:
            logger.info("WhatsApp processing: reply sent — wamid=%s", log_id(ChannelType.WHATSAPP, msg_id))
        else:
            logger.error("WhatsApp processing: failed to send reply")

    except Exception as e:
        logger.error("WhatsApp processing: EXCEPTION — %s", format_exception_message(e), exc_info=True)
        # Match the Telegram / Discord / Google Chat adapters: in DMs, tell
        # the user something went wrong instead of staying silent.
        if chat_type == "private":
            try:
                creds = await _get_whatsapp_creds()
                if creds and creds[0] and creds[1]:
                    access_token, phone_number_id = creds[0], creds[1]
                    await WhatsAppChannel(phone_number_id, access_token).send_message(
                        OutboundMessage(
                            text=notice("adapter_error", notice_language()),
                            channel_user_id=inbound.channel_user_id,
                        )
                    )
            except Exception:
                logger.debug(
                    "WhatsApp processing: failed to send error notice",
                    exc_info=True,
                )
