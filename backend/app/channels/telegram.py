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

"""Telegram channel adapter – Bot API.

Receives updates via webhook from the Telegram Bot API and sends responses
back via the sendMessage endpoint.  Credentials are stored per channel bot
in the database (``telegram_bot`` provider), not in environment variables.

Setup:
  1. Create a bot via @BotFather on Telegram.
  2. Copy the bot token.
  3. Create a ``telegram_bot`` channel bot in Copilot with the token.
  4. Set the webhook URL in Telegram (include a secret token so inbound
     requests can be authenticated — store the same value as
     ``webhook_secret_token`` in the channel bot's extra config):
     POST https://api.telegram.org/bot<token>/setWebhook
     { "url": "{base_url}/api/channels/telegram/webhook",
       "secret_token": "<webhook_secret_token>" }
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass

import httpx
from fastapi import APIRouter, HTTPException, Request
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
    log_preview,
    spawn_channel_task,
    timing_safe_str_eq,
    typing_keepalive,
    warn_unauthenticated_once,
)
from app.channels.router import handle_message
from app.core.utils import format_exception_message
from app.services.channel_notices import notice, notice_language
from app.services.vault import reveal_field, reveal_json_field

router = APIRouter()
logger = logging.getLogger(__name__)


_MD_BOLD_RE = re.compile(r"\*\*(.+?)\*\*", re.DOTALL)
_MD_HEADING_RE = re.compile(r"^#{1,6}\s+(.+)$", re.MULTILINE)
_MD_STRIKE_RE = re.compile(r"~~(.+?)~~", re.DOTALL)


def _markdown_to_telegram(text: str) -> str:
    """Convert standard Markdown to Telegram's legacy Markdown surface.

    Telegram's ``parse_mode: "Markdown"`` uses single-asterisk bold and has
    no heading or strikethrough syntax — ``**bold**`` renders as literal
    asterisks and ``## Heading`` renders literally. Links ``[text](url)``
    and inline code pass through unchanged.
    """
    if not text:
        return text
    text = _MD_HEADING_RE.sub(lambda m: f"*{m.group(1).strip()}*", text)
    text = _MD_BOLD_RE.sub(r"*\1*", text)
    # Legacy Markdown has no strikethrough — drop the markers.
    text = _MD_STRIKE_RE.sub(r"\1", text)
    return text


class TelegramChannel(BaseChannel):
    """Telegram Bot API channel adapter."""

    channel_type = ChannelType.TELEGRAM

    def __init__(self, bot_token: str, chat_id: str | None = None):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self._base_url = f"https://api.telegram.org/bot{bot_token}"

    async def send_message(self, message: OutboundMessage) -> str | None:
        """Send a message via the Telegram Bot API."""
        chat_id = message.channel_thread_id or self.chat_id
        if not chat_id:
            logger.error("No chat_id for Telegram message")
            return None

        # Send embedded images natively via sendPhoto before the text
        embedded_images = message.metadata.get("embedded_images", [])
        if embedded_images:
            await self._send_photos(chat_id, embedded_images)

        # Convert standard agent Markdown to Telegram's legacy Markdown
        # surface (single-asterisk bold, no headings) so common formatting
        # renders instead of tripping the parse-error fallback that
        # silently strips ALL formatting from the message.
        text = _markdown_to_telegram(message.text or "")
        # Telegram has a 4096 char limit — split long messages
        chunks = chunk_message(text, max_chars=CHANNEL_MAX_CHARS.get("telegram", 4096))

        last_msg_id = None
        async with httpx.AsyncClient() as client:
            for chunk in chunks:
                resp = await client.post(
                    f"{self._base_url}/sendMessage",
                    json={
                        "chat_id": chat_id,
                        "text": chunk,
                        "parse_mode": "Markdown",
                    },
                    timeout=15,
                )
                if resp.status_code == 200:
                    data = resp.json()
                    last_msg_id = str(data.get("result", {}).get("message_id", ""))
                else:
                    # Retry without Markdown in case of parse errors
                    resp2 = await client.post(
                        f"{self._base_url}/sendMessage",
                        json={"chat_id": chat_id, "text": chunk},
                        timeout=15,
                    )
                    if resp2.status_code == 200:
                        data = resp2.json()
                        last_msg_id = str(data.get("result", {}).get("message_id", ""))
                    else:
                        logger.error("Telegram sendMessage failed: %s", resp2.text)

        return last_msg_id

    async def _send_photos(
        self,
        chat_id: str,
        embedded_images: list[dict],
    ) -> None:
        """Upload images directly to Telegram via sendPhoto."""
        from app.channels.base import fetch_image_bytes

        for img_info in embedded_images:
            try:
                result = await fetch_image_bytes(
                    img_info["image_id"],
                    source=img_info.get("source", "images"),
                )
                if not result:
                    continue
                image_data, content_type = result
                filename = f"{img_info.get('alt_text', 'image')[:50]}.png"

                async with httpx.AsyncClient() as client:
                    resp = await client.post(
                        f"{self._base_url}/sendPhoto",
                        data={
                            "chat_id": chat_id,
                            "caption": img_info.get("alt_text", ""),
                        },
                        files={"photo": (filename, image_data, content_type)},
                        timeout=30,
                    )
                    if resp.status_code != 200:
                        logger.warning("Telegram sendPhoto failed: %s", resp.text[:300])
            except Exception as exc:
                logger.warning("Telegram photo upload error: %s", format_exception_message(exc))

    async def send_typing_indicator(self, channel_thread_id: str | None = None) -> None:
        """Show 'typing...' in the Telegram chat."""
        chat_id = channel_thread_id or self.chat_id
        if not chat_id:
            return
        try:
            async with httpx.AsyncClient() as client:
                await client.post(
                    f"{self._base_url}/sendChatAction",
                    json={"chat_id": chat_id, "action": "typing"},
                    timeout=5,
                )
        except Exception as exc:
            logger.warning("Telegram typing indicator failed: %s", format_exception_message(exc))


# ── Inbound files ────────────────────────────────────────────────────────────

#: The largest file the Bot API's ``getFile`` serves ("bots can download files
#: of up to 20MB in size"): a larger one is refused, never downloaded.
TELEGRAM_GETFILE_MAX_BYTES = 20 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class _TelegramFile:
    """A file a Telegram message carries, before ``getFile`` gives its download path."""

    file_id: str
    filename: str
    content_type: str
    size: int


#: The file fields of a Telegram ``Message`` handed to the router, with the
#: name and type used when Telegram reports none. A sticker is not a file the
#: agent reads, and an animation also sets ``document``, where it is read.
_FILE_FIELDS: tuple[tuple[str, str, str], ...] = (
    ("document", "document", "application/octet-stream"),
    ("photo", "photo.jpg", "image/jpeg"),
    ("audio", "audio.mp3", "audio/mpeg"),
    ("video", "video.mp4", "video/mp4"),
    ("voice", "voice.ogg", "audio/ogg"),
    ("video_note", "video_note.mp4", "video/mp4"),
)


def _str_field(value: object) -> str:
    return value if isinstance(value, str) else ""


def _size_field(value: object) -> int:
    return value if isinstance(value, int) and value > 0 else 0


def _largest_photo(sizes: object) -> dict | None:
    """The largest size of a ``photo`` field, an array of sizes of one picture.

    Largest by pixels: ``file_size`` is optional on a ``PhotoSize`` and only
    breaks a tie, so a size that omits it is never passed over for a thumbnail.
    """
    if not isinstance(sizes, list):
        return None
    candidates = [size for size in sizes if isinstance(size, dict) and _str_field(size.get("file_id"))]
    if not candidates:
        return None
    return max(
        candidates,
        key=lambda size: (
            _size_field(size.get("width")) * _size_field(size.get("height")),
            _size_field(size.get("file_size")),
        ),
    )


def _message_files(message: dict) -> list[_TelegramFile]:
    """The files of *message* the router attaches, in ``_FILE_FIELDS`` order."""
    files: list[_TelegramFile] = []
    for field_name, default_name, default_type in _FILE_FIELDS:
        item = _largest_photo(message.get(field_name)) if field_name == "photo" else message.get(field_name)
        if not isinstance(item, dict) or not _str_field(item.get("file_id")):
            continue
        files.append(
            _TelegramFile(
                file_id=item["file_id"],
                filename=_str_field(item.get("file_name")) or default_name,
                content_type=_str_field(item.get("mime_type")) or default_type,
                size=_size_field(item.get("file_size")),
            )
        )
    return files


async def _resolve_telegram_file(
    bot_token: str, tg_file: _TelegramFile, max_bytes: int
) -> InboundFileAttachment | None:
    """The download of *tg_file* ``getFile`` names, or ``None`` when Telegram cannot serve it.

    Both Bot API URLs carry the bot token, so no line logs a URL or an error's
    message; nor the file name, which the sender chose.
    """
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                f"https://api.telegram.org/bot{bot_token}/getFile",
                params={"file_id": tg_file.file_id},
            )
            resp.raise_for_status()
            result = resp.json().get("result")
    except httpx.HTTPStatusError as exc:
        logger.warning(
            "Failed to resolve Telegram file: %s (HTTP %d)",
            tg_file.content_type,
            exc.response.status_code,
        )
        return None
    except Exception as exc:
        logger.warning("Failed to resolve Telegram file: %s (%s)", tg_file.content_type, type(exc).__name__)
        return None
    file_path = _str_field(result.get("file_path")) if isinstance(result, dict) else ""
    if not file_path:
        logger.warning("Failed to resolve Telegram file: %s (no file path)", tg_file.content_type)
        return None
    size = tg_file.size or _size_field(result.get("file_size"))
    if size > max_bytes:
        logger.warning("Telegram file too large (%d bytes > %d): %s", size, max_bytes, tg_file.content_type)
        return None
    return InboundFileAttachment(
        url=f"https://api.telegram.org/file/bot{bot_token}/{file_path}",
        filename=tg_file.filename,
        content_type=tg_file.content_type,
        size=size,
        platform_file_id=tg_file.file_id,
    )


async def _resolve_telegram_files(
    bot_token: str, files: list[_TelegramFile]
) -> list[InboundFileAttachment]:
    """The downloads of *files* Telegram serves, their ``getFile`` calls made concurrently.

    A file declared larger than ``getFile`` serves or the router stores is not
    requested. The router checks the bytes it downloads against its limit again.
    """
    from app.services.file_storage import MAX_UPLOAD_SIZE_BYTES

    max_bytes = min(TELEGRAM_GETFILE_MAX_BYTES, MAX_UPLOAD_SIZE_BYTES)
    requested: list[_TelegramFile] = []
    for tg_file in files:
        if tg_file.size > max_bytes:
            logger.warning(
                "Telegram file too large (%d bytes > %d): %s", tg_file.size, max_bytes, tg_file.content_type
            )
        else:
            requested.append(tg_file)
    resolved = await asyncio.gather(
        *(_resolve_telegram_file(bot_token, tg_file, max_bytes) for tg_file in requested)
    )
    return [attachment for attachment in resolved if attachment]


async def _attach_telegram_files(
    inbound: InboundMessage, bot_token: str, files: list[_TelegramFile] | None
) -> None:
    """Add the downloads of *files* to *inbound*'s ``file_attachments``.

    A file Telegram cannot serve is left out and the message still goes on,
    as a file the router fails to download is: its caption, or the file-only
    placeholder, is what the agent answers.
    """
    if not files:
        return
    inbound.file_attachments.extend(await _resolve_telegram_files(bot_token, files))
    logger.info(
        "Telegram processing: %d of %d file attachment(s) resolved",
        len(inbound.file_attachments),
        len(files),
    )


# ── Bot mentions ─────────────────────────────────────────────────────────────


def _entity_bounds(entity: dict) -> tuple[int, int] | None:
    """Where a message entity starts and ends in its text, in UTF-16 code units.

    Telegram counts ``offset`` and ``length`` in UTF-16 code units: an emoji
    before a mention counts two, where a ``str`` index counts one.
    """
    offset, length = entity.get("offset"), entity.get("length")
    if not isinstance(offset, int) or not isinstance(length, int) or offset < 0 or length <= 0:
        return None
    return offset, offset + length


def _strip_bot_mentions(text: str, entities: object, bot_username: str) -> tuple[bool, str]:
    """Whether *entities* mention the bot, and *text* without those mentions.

    *entities* are the message's ``entities``, or its ``caption_entities`` when
    *text* is a caption. ``@botname`` (``mention`` / ``text_mention``) is
    removed and ``/command@botname`` becomes ``/command``, mirroring Slack and
    Teams; any other ``@user`` mention is kept. A name is compared whole, and
    only the entity itself is rewritten: ``@botname_backup`` is not
    ``@botname``, in a command or next to one.
    """
    if not bot_username or not isinstance(entities, list):
        return False, text
    bot = bot_username.lower()
    units = text.encode("utf-16-le", "surrogatepass")
    edits: list[tuple[int, int, str]] = []
    for entity in entities:
        bounds = _entity_bounds(entity) if isinstance(entity, dict) else None
        if bounds is None:
            continue
        start, end = bounds
        span = units[2 * start : 2 * end].decode("utf-16-le", "surrogatepass")
        kind = entity.get("type")
        if kind in ("mention", "text_mention") and span.lstrip("@").lower() == bot:
            edits.append((start, end, ""))
        elif kind == "bot_command" and "@" in span and span.rsplit("@", 1)[1].lower() == bot:
            edits.append((start, end, span.rsplit("@", 1)[0]))
    if not edits:
        return False, text
    # Rewrite from the end so an edit never moves the offsets of the next one;
    # an entity overlapping one already rewritten is left alone.
    last_start = len(units) // 2
    for start, end, replacement in sorted(edits, reverse=True):
        if end > last_start:
            continue
        units = units[: 2 * start] + replacement.encode("utf-16-le", "surrogatepass") + units[2 * end :]
        last_start = start
    return True, units.decode("utf-16-le", "surrogatepass").strip()


# ── Webhook endpoint ─────────────────────────────────────────────────────────


@router.post("/telegram/webhook")
async def telegram_webhook(request: Request) -> JSONResponse:
    """Receive Telegram Bot API updates via webhook."""
    # Load credentials first so the request can be authenticated before any
    # payload content is trusted.
    creds = await _get_telegram_creds()
    if not creds:
        logger.warning("No telegram_bot channel bot configured")
        return JSONResponse({"ok": True})

    # Webhook authentication: Telegram echoes the secret_token passed to
    # setWebhook in the X-Telegram-Bot-Api-Secret-Token header. Reject
    # mismatches when a secret is configured; warn loudly when it is not.
    expected_secret = creds.get("webhook_secret_token", "")
    if expected_secret:
        provided = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if not timing_safe_str_eq(provided, expected_secret):
            logger.warning(
                "Telegram webhook rejected: invalid X-Telegram-Bot-Api-Secret-Token"
            )
            raise HTTPException(status_code=401, detail="Invalid secret token")
    else:
        warn_unauthenticated_once(
            "telegram",
            "Telegram webhook is UNAUTHENTICATED: no webhook_secret_token "
            "configured on the telegram_bot channel bot — set one and pass it "
            "as secret_token to setWebhook to enable request authentication",
        )

    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"ok": True})  # Always return 200 to Telegram

    if not isinstance(payload, dict):
        return JSONResponse({"ok": True})

    # Extract the message from the update. An edited message is processed
    # again only when it is a text message.
    message = payload.get("message")
    is_edit = not message
    if is_edit:
        message = payload.get("edited_message")
    if not isinstance(message, dict):
        return JSONResponse({"ok": True})

    # A message with a file carries its text in ``caption`` (its entities in
    # ``caption_entities``), and a bare file has neither.
    files = _message_files(message)
    is_caption = not (isinstance(message.get("text"), str) and message["text"])
    if not is_caption:
        text, entities = message["text"], message.get("entities")
    elif files and not is_edit:
        text, entities = _str_field(message.get("caption")), message.get("caption_entities")
    else:
        # Neither text nor a file the router attaches (a sticker, a service
        # message, a caption on another kind of media), or an edited caption.
        return JSONResponse({"ok": True})

    chat = message.get("chat")
    if not isinstance(chat, dict) or "id" not in chat:
        return JSONResponse({"ok": True})  # Malformed message — missing chat info

    chat_id = str(chat["id"])
    message_id = str(message.get("message_id", ""))

    # Extract user info
    from_user = message.get("from")
    if not isinstance(from_user, dict):
        from_user = {}
    username = from_user.get("username", "")
    first_name = from_user.get("first_name", "")
    last_name = from_user.get("last_name", "")
    telegram_user_id = str(from_user.get("id", ""))

    # Ignore bot commands like /start that aren't real messages
    if not is_caption and text.startswith("/start"):
        return JSONResponse({"ok": True})

    bot_token = creds["token"]

    # Detect whether the bot was explicitly @mentioned (a ``mention`` /
    # ``text_mention`` entity naming the bot's username, or a
    # ``/command@botname``) and strip the mention for cleaner agent input
    # (mirrors Slack/Teams behaviour).
    original_text = text
    bot_was_mentioned, text = _strip_bot_mentions(text, entities, creds.get("bot_username", ""))

    chat_type = chat.get("type", "private")

    logger.info(
        "Telegram webhook received: chat_type=%s, chat_id=%s, user=%s, bot_mentioned=%s, "
        "text_length=%d, text_source=%s, files=%d",
        chat_type,
        chat_id,
        telegram_user_id,
        bot_was_mentioned,
        len(original_text),
        "caption" if is_caption else "text",
        len(files),
    )

    if original_text != text:
        logger.debug(
            "Telegram message: stripped bot mention: %s → %s",
            log_preview(original_text),
            log_preview(text),
        )

    # After stripping mentions, the text may be empty (bare @mention). A
    # message carrying files is still for the agent, which needs a user turn
    # to react to (as on Teams).
    if not text:
        if not files:
            logger.info("Telegram message: text empty after stripping mentions — skipping")
            return JSONResponse({"ok": True})
        text = FILE_ONLY_PLACEHOLDER

    inbound = InboundMessage(
        channel=ChannelType.TELEGRAM,
        text=text,
        user_email=None,  # Telegram doesn't provide email
        channel_user_id=telegram_user_id,
        channel_thread_id=chat_id,
        channel_message_id=message_id,
        metadata={
            "username": username,
            "first_name": first_name,
            "last_name": last_name,
            "chat_type": chat_type,
            "bot_was_mentioned": bot_was_mentioned,
        },
    )

    # Process asynchronously (return 200 immediately). The files' getFile
    # round trips are made there too, off the webhook's answer.
    spawn_channel_task(
        _process_telegram_message(inbound, bot_token, files), name="telegram-message"
    )

    return JSONResponse({"ok": True})


async def _process_telegram_message(
    inbound: InboundMessage,
    bot_token: str,
    files: list[_TelegramFile] | None = None,
) -> None:
    """Process a Telegram message in the background.

    *files* are the message's files: their downloads are resolved here and
    handed to the router as ``file_attachments``, which the router downloads,
    stores and attaches like any channel's.
    """
    chat_type = inbound.metadata.get("chat_type", "private")
    is_private = chat_type == "private"
    bot_mentioned = bool(inbound.metadata.get("bot_was_mentioned"))

    # Only show typing indicator when the message is likely to produce
    # a response.  In group chats where the bot was NOT @mentioned, the
    # message may be silently dropped by the only_mention gate — showing
    # "typing…" and then going silent is confusing.
    show_typing = is_private or bot_mentioned

    logger.info(
        "Telegram processing: started — chat_type=%s, is_private=%s, bot_mentioned=%s, show_typing=%s, "
        "text_length=%d, files=%d",
        chat_type,
        is_private,
        bot_mentioned,
        show_typing,
        len(inbound.text or ""),
        len(files or ()),
    )

    channel = TelegramChannel(bot_token, chat_id=inbound.channel_thread_id)

    try:
        if show_typing:
            async with typing_keepalive(channel, inbound.channel_thread_id):
                await _attach_telegram_files(inbound, bot_token, files)
                outbound = await handle_message(inbound)
        else:
            await _attach_telegram_files(inbound, bot_token, files)
            outbound = await handle_message(inbound)

        if outbound and not outbound.suppress:
            outbound.channel_thread_id = inbound.channel_thread_id
            logger.info(
                "Telegram processing: sending reply — length=%d chars",
                len(outbound.text) if outbound.text else 0,
            )
            msg_id = await channel.send_message(outbound)
            if msg_id:
                logger.info("Telegram processing: reply sent — message_id=%s", msg_id)
            else:
                logger.error("Telegram processing: failed to send reply")
        elif outbound and outbound.suppress:
            logger.info(
                "Telegram processing: suppressing reply — message was "
                "silently dropped (only_mention gate, [NO_RESPONSE], "
                "or agent rejected channel). chat_type=%s, "
                "bot_mentioned=%s, chat_id=%s",
                chat_type,
                bot_mentioned,
                inbound.channel_thread_id,
            )
    except Exception as e:
        logger.error("Telegram processing: EXCEPTION — %s", format_exception_message(e), exc_info=True)
        # Only send an error message for private/DM conversations where the
        # message was clearly directed at the bot.  In group chats, sending
        # an error for a message that might not have been aimed at the bot
        # would be confusing.
        if is_private or bot_mentioned:
            try:
                error_msg = OutboundMessage(
                    text=notice("adapter_error", notice_language()),
                    channel_thread_id=inbound.channel_thread_id,
                )
                await channel.send_message(error_msg)
            except Exception as send_exc:
                logger.warning("Failed to send error message to Telegram: %s", format_exception_message(send_exc))


# ── Credential loading ───────────────────────────────────────────────────────


async def _get_telegram_creds() -> dict | None:
    """Load Telegram bot credentials from the database."""
    bot = await get_channel_bot("telegram_bot")
    if not bot:
        return None

    token = await reveal_field(bot, "api_key_enc") or ""
    if not token:
        return None

    # Try to extract bot_username from extra_config for mention detection
    bot_username = ""
    webhook_secret_token = ""
    if bot.extra_config_enc:
        try:
            extra = await reveal_json_field(bot, "extra_config_enc") or {}
            bot_username = extra.get("bot_username", "")
            webhook_secret_token = extra.get("webhook_secret_token", "")
        except Exception as exc:
            logger.warning("Failed to decrypt Telegram bot extra config: %s", format_exception_message(exc))
            # Credentials exist but are unreadable — fail closed, not open.
            webhook_secret_token = DECRYPT_FAILED_CREDENTIAL
    # Fallback: derive from token via getMe (cached in Redis)
    if not bot_username:
        bot_username = await _resolve_telegram_bot_username(token)

    return {
        "token": token,
        "channel_bot_id": str(bot.id),
        "bot_username": bot_username,
        "webhook_secret_token": webhook_secret_token,
    }


async def _resolve_telegram_bot_username(token: str) -> str:
    """Resolve the bot's username via the Telegram getMe API (cached)."""
    try:
        from app.core.redis import redis_client
        from app.integrations.http import app_token_cache_key

        redis = await redis_client()
        # Hash the token into the key — never persist secret material
        # (even a prefix) in plaintext Redis key names.
        cache_key = f"telegram:bot_username:{app_token_cache_key('telegram', token)}"
        cached = await redis.get(cache_key)
        if cached:
            return cached

        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"https://api.telegram.org/bot{token}/getMe",
                timeout=10,
            )
            if resp.status_code == 200:
                data = resp.json()
                username = data.get("result", {}).get("username", "")
                if username:
                    await redis.set(cache_key, username, ex=60 * 60 * 24)  # 24h cache
                    return username
    except Exception as exc:
        logger.warning("Failed to resolve Telegram bot username: %s", format_exception_message(exc))
    return ""


# ── Helpers ──────────────────────────────────────────────────────────────────
