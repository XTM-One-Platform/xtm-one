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

"""Discord channel adapter – Gateway / Webhook.

Receives messages from the Discord Gateway via a background bot that connects
using ``discord.py`` and sends responses back via the same library.

Credentials are stored per channel bot in the database (``discord_bot``
provider), not in environment variables.

Setup:
  1. Go to https://discord.com/developers/applications and create a new app.
  2. Go to Bot → copy the Token.
  3. Enable "Message Content Intent" under Privileged Gateway Intents.
  4. Go to OAuth2 → URL Generator → select ``bot`` scope → select permissions:
     Send Messages, Read Message History, Add Reactions, Use Slash Commands.
  5. Use the generated URL to invite the bot to your server.
  6. Create a ``discord_bot`` channel bot in XTM One with the token.

Architecture:
  Unlike Slack/Teams/WhatsApp/Telegram which use webhooks, Discord uses a
  persistent WebSocket Gateway connection via ``discord.py``.  The bot runs
  as a background task started by the gateway manager.

  The adapter also exposes an HTTP endpoint ``/discord/interactions`` for
  Discord's Interaction endpoint (slash commands / component interactions),
  which is optional but useful if the admin configures an Interactions URL.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from typing import Any

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.channels.base import (
    CHANNEL_MAX_CHARS,
    DECRYPT_FAILED_CREDENTIAL,
    BaseChannel,
    ChannelType,
    InboundFileAttachment,
    InboundMessage,
    OutboundMessage,
    chunk_message,
    get_channel_bot,
    spawn_channel_task,
    typing_keepalive,
    warn_unauthenticated_once,
)
from app.channels.router import handle_message
from app.core.utils import format_exception_message
from app.services.channel_notices import notice, notice_language
from app.services.vault import invalidate_entity_secrets, reveal_field, reveal_json_field

router = APIRouter()
logger = logging.getLogger(__name__)

# Discord message character limit
DISCORD_MAX_MESSAGE_LENGTH = CHANNEL_MAX_CHARS.get("discord", 2000)

#: How long the gateway waits before it connects again after a session ended, doubled per failure up to the max.
GATEWAY_RECONNECT_SECONDS = 30.0
GATEWAY_RECONNECT_MAX_SECONDS = 600.0


class DiscordChannel(BaseChannel):
    """Discord channel adapter using REST API."""

    channel_type = ChannelType.DISCORD

    def __init__(self, bot_token: str, channel_id: str | None = None):
        self.bot_token = bot_token
        self.channel_id = channel_id
        self._base_url = "https://discord.com/api/v10"
        self._headers = {
            "Authorization": f"Bot {bot_token}",
            "Content-Type": "application/json",
        }

    async def send_message(self, message: OutboundMessage) -> str | None:
        """Send a message to a Discord channel via REST API."""
        channel_id = message.channel_thread_id or self.channel_id
        if not channel_id:
            logger.error("No channel_id for Discord message")
            return None

        # Upload embedded images as file attachments alongside the text
        embedded_images = message.metadata.get("embedded_images", [])
        if embedded_images:
            result = await self._send_with_images(
                channel_id,
                message.text or "",
                embedded_images,
            )
            if result:
                return result

        text = message.text or ""
        chunks = chunk_message(text, max_chars=DISCORD_MAX_MESSAGE_LENGTH)

        last_msg_id = None
        async with httpx.AsyncClient() as client:
            for chunk in chunks:
                resp = await client.post(
                    f"{self._base_url}/channels/{channel_id}/messages",
                    headers=self._headers,
                    json={"content": chunk},
                    timeout=15,
                )
                if resp.status_code == 200:
                    data = resp.json()
                    last_msg_id = data.get("id")
                else:
                    logger.error(
                        "Discord send_message failed: status=%s body=%s",
                        resp.status_code,
                        resp.text[:500],
                    )

        return last_msg_id

    async def _send_with_images(
        self,
        channel_id: str,
        text: str,
        embedded_images: list[dict],
    ) -> str | None:
        """Send a message with image file attachments to Discord."""
        from app.channels.base import fetch_image_bytes

        files_data: list[tuple[str, tuple[str, bytes, str]]] = []
        for i, img_info in enumerate(embedded_images):
            try:
                result = await fetch_image_bytes(
                    img_info["image_id"],
                    source=img_info.get("source", "images"),
                )
                if not result:
                    continue
                image_data, content_type = result
                filename = f"image_{i}.png"
                files_data.append((f"files[{i}]", (filename, image_data, content_type)))
            except Exception as exc:
                logger.warning("Discord image fetch error: %s", format_exception_message(exc))

        if not files_data:
            return None

        try:
            # Discord multipart: JSON payload_json + file parts
            chunks = chunk_message(text, max_chars=DISCORD_MAX_MESSAGE_LENGTH)
            first_chunk = chunks[0] if chunks else ""

            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    f"{self._base_url}/channels/{channel_id}/messages",
                    headers={"Authorization": f"Bot {self.bot_token}"},
                    data={"content": first_chunk},
                    files=files_data,
                    timeout=30,
                )
                if resp.status_code == 200:
                    last_msg_id = resp.json().get("id")
                    # Send remaining text chunks as follow-up messages
                    for chunk in chunks[1:]:
                        resp2 = await client.post(
                            f"{self._base_url}/channels/{channel_id}/messages",
                            headers=self._headers,
                            json={"content": chunk},
                            timeout=15,
                        )
                        if resp2.status_code == 200:
                            last_msg_id = resp2.json().get("id")
                    return last_msg_id
                else:
                    logger.error(
                        "Discord image send failed: status=%s body=%s",
                        resp.status_code,
                        resp.text[:500],
                    )
        except Exception as exc:
            logger.warning("Discord image send error: %s", format_exception_message(exc))

        return None

    async def send_typing_indicator(self, channel_thread_id: str | None = None) -> None:
        """Trigger typing indicator in a Discord channel."""
        channel_id = channel_thread_id or self.channel_id
        if not channel_id:
            return
        try:
            async with httpx.AsyncClient() as client:
                await client.post(
                    f"{self._base_url}/channels/{channel_id}/typing",
                    headers=self._headers,
                    timeout=5,
                )
        except Exception as exc:
            logger.warning("Discord typing indicator failed: %s", format_exception_message(exc))


# ── Gateway Bot (background task) ────────────────────────────────────────────

_gateway_task: asyncio.Task | None = None
_gateway_stop_event: asyncio.Event | None = None

# Every platform process opens its own gateway session - each uvicorn worker
# (WEB_WORKERS, 2 by default) and each replica - and Discord delivers every
# message to every session of the bot. The first process to claim a message id
# answers it; the others drop it. Discord does not redeliver a message over the
# gateway, so the window only has to outlive the fan-out.
_DEDUP_TTL = 600  # seconds


async def _claim_discord_message(message_id: str) -> bool:
    """True when THIS process should answer the message (first claim wins).

    Redis ``SET ... NX EX``, the Slack pattern: atomic across processes and
    replicas. Fails open when Redis is unavailable - a duplicate answer is
    better than a message nobody answers.
    """
    key = f"discord:dedup:{message_id}"
    try:
        from app.core.redis import redis_client

        redis = await redis_client()
        return bool(await redis.set(key, "1", nx=True, ex=_DEDUP_TTL))
    except Exception as exc:
        logger.warning(
            "Discord dedup Redis check failed (answering message): %s",
            format_exception_message(exc),
        )
        return True

__all__ = ["_gateway_stop_event", "_gateway_task"]


async def start_discord_gateway() -> None:
    """Start the Discord Gateway bot as a background task.

    Called during app startup.  If no discord_bot is configured, this
    is a no-op.
    """
    global _gateway_task, _gateway_stop_event

    try:
        creds = await _get_discord_creds()
    except Exception as exc:
        # The token cannot be read yet (its vault, or the database is down):
        # the supervisor reads it again until it can, or finds no bot.
        logger.warning(
            "Discord gateway: the bot token could not be read at startup, retrying: %s",
            format_exception_message(exc),
        )
        creds = {"token": None}
    if not creds:
        logger.info("No discord_bot configured — Discord gateway not started")
        return

    _gateway_stop_event = asyncio.Event()
    _gateway_task = asyncio.create_task(
        _supervise_discord_gateway(creds["token"], _gateway_stop_event),
        name="discord-gateway",
    )
    logger.info("Discord gateway bot started")


async def stop_discord_gateway() -> None:
    """Gracefully stop the Discord Gateway bot."""
    global _gateway_task, _gateway_stop_event

    if _gateway_stop_event:
        _gateway_stop_event.set()

    if _gateway_task and not _gateway_task.done():
        _gateway_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            _ = await _gateway_task  # wait for clean shutdown
    _gateway_task = None
    _gateway_stop_event = None
    logger.info("Discord gateway bot stopped")


async def _supervise_discord_gateway(token: str | None, stop: asyncio.Event) -> None:
    """Run gateway sessions until *stop*: after a session that ended, connect again with the bot token read afresh.

    A session keeps the token it connected with. When Discord ends it (a
    token rotated in its vault and the old one revoked, or a network
    failure), the next session reads the token again, its cached vault value
    dropped, so the bot comes back without a restart. Without a *token* (it
    could not be read at startup) the first session waits for the same read.
    Consecutive failures wait longer, up to :data:`GATEWAY_RECONNECT_MAX_SECONDS`.
    """
    delay = GATEWAY_RECONNECT_SECONDS
    while True:
        if token is not None:
            started = time.monotonic()
            if not await _run_discord_gateway(token):
                return
            if time.monotonic() - started > GATEWAY_RECONNECT_MAX_SECONDS:
                delay = GATEWAY_RECONNECT_SECONDS
            token = None
        while token is None:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), delay)
            if stop.is_set():
                return
            delay = min(delay * 2, GATEWAY_RECONNECT_MAX_SECONDS)
            try:
                creds = await _get_discord_creds(refresh=True)
            except Exception as exc:
                logger.warning("Discord gateway: the bot token could not be read: %s", format_exception_message(exc))
                continue
            if creds is None:
                logger.info("No discord_bot configured any more - Discord gateway stopped")
                return
            token = creds["token"]


async def _run_discord_gateway(token: str) -> bool:
    """Run one Discord gateway session; whether connecting again may help when it ends.

    Uses ``discord.py`` if available, otherwise falls back to a simple
    Gateway implementation using httpx + websockets.
    """
    try:
        import discord
    except ImportError:
        logger.warning(
            "discord.py not installed — Discord gateway will not start. Install with: pip install discord.py"
        )
        return False

    intents = discord.Intents.default()
    intents.message_content = True  # Required for reading message content
    intents.guilds = True
    intents.dm_messages = True

    client = discord.Client(intents=intents)

    @client.event
    async def on_ready():
        logger.info("Discord bot connected: %s (ID: %s)", client.user, client.user.id)

    @client.event
    async def on_message(message: discord.Message):
        # Ignore own messages
        if message.author == client.user:
            return

        # Ignore system messages
        if (
            message.type != discord.MessageType.default
            and message.type != discord.MessageType.reply
        ):
            return

        # Extract text content
        text = message.content
        if not text:
            return

        # Detect if bot was mentioned
        bot_was_mentioned = client.user in message.mentions if client.user else False

        # Determine channel type (DM vs guild)
        is_dm = isinstance(message.channel, (discord.DMChannel, discord.GroupChannel))

        # Strip bot mention from text
        if client.user and bot_was_mentioned:
            text = (
                text.replace(f"<@{client.user.id}>", "")
                .replace(f"<@!{client.user.id}>", "")
                .strip()
            )

        if not text:
            return  # Empty after stripping mention

        # Another process's gateway session received the same message.
        if not await _claim_discord_message(str(message.id)):
            return

        # Determine channel_thread_id — use channel ID for conversation tracking
        channel_id = str(message.channel.id)

        # User info
        user_name = str(message.author)
        user_id = str(message.author.id)

        # Guild (server) context
        guild_name = message.guild.name if message.guild else None
        channel_name = getattr(message.channel, "name", None)

        logger.info(
            "Discord message: guild=%s channel=%s user=%s dm=%s mentioned=%s text_length=%d",
            guild_name or "(DM)",
            channel_name or channel_id,
            user_id,
            is_dm,
            bot_was_mentioned,
            len(text),
        )

        # Extract Discord file attachments.
        # discord.py Attachment objects have .url (public CDN), .filename,
        # .content_type, .size — no auth headers needed for download.
        discord_file_attachments: list[InboundFileAttachment] = []
        for att in message.attachments:
            if att.url:
                discord_file_attachments.append(
                    InboundFileAttachment(
                        url=att.url,
                        filename=att.filename or "file",
                        content_type=att.content_type or "application/octet-stream",
                        size=att.size or 0,
                        platform_file_id=str(att.id),
                    )
                )
        if discord_file_attachments:
            logger.info(
                "Discord message: %d file attachment(s) detected",
                len(discord_file_attachments),
            )

        inbound = InboundMessage(
            channel=ChannelType.DISCORD,
            text=text,
            user_email=None,  # Discord doesn't expose email
            channel_user_id=user_id,
            channel_thread_id=channel_id,
            channel_message_id=str(message.id),
            file_attachments=discord_file_attachments,
            metadata={
                "username": user_name,
                "guild_name": guild_name,
                "channel_name": channel_name,
                "is_dm": is_dm,
                "bot_was_mentioned": bot_was_mentioned,
                "channel_type": "dm" if is_dm else "guild",
            },
        )

        # Process asynchronously
        spawn_channel_task(
            _process_discord_message(inbound, token, channel_id),
            name="discord-message",
        )

    try:
        await client.start(token)
    except asyncio.CancelledError:
        await client.close()
        raise
    except Exception as exc:
        logger.error("Discord gateway error: %s", format_exception_message(exc), exc_info=True)
        await client.close()
    return True


async def _process_discord_message(
    inbound: InboundMessage,
    bot_token: str,
    channel_id: str,
) -> None:
    """Process a Discord message in the background."""
    is_dm = bool(inbound.metadata.get("is_dm"))
    bot_mentioned = bool(inbound.metadata.get("bot_was_mentioned"))

    show_typing = is_dm or bot_mentioned
    channel = DiscordChannel(bot_token, channel_id=channel_id)

    try:
        if show_typing:
            async with typing_keepalive(channel, inbound.channel_thread_id):
                outbound = await handle_message(inbound)
        else:
            outbound = await handle_message(inbound)

        if outbound and not outbound.suppress:
            outbound.channel_thread_id = inbound.channel_thread_id
            logger.info(
                "Discord processing: sending reply — length=%d chars",
                len(outbound.text) if outbound.text else 0,
            )
            msg_id = await channel.send_message(outbound)
            if msg_id:
                logger.info("Discord processing: reply sent — message_id=%s", msg_id)
            else:
                logger.error("Discord processing: failed to send reply")
        elif outbound and outbound.suppress:
            logger.info(
                "Discord processing: suppressing reply — chat_type=%s, bot_mentioned=%s, channel_id=%s",
                inbound.metadata.get("channel_type"),
                bot_mentioned,
                channel_id,
            )
    except Exception as e:
        logger.error("Discord processing: EXCEPTION — %s", format_exception_message(e), exc_info=True)
        if is_dm or bot_mentioned:
            try:
                error_msg = OutboundMessage(
                    text=notice("adapter_error", notice_language()),
                    channel_thread_id=inbound.channel_thread_id,
                )
                await channel.send_message(error_msg)
            except Exception as send_exc:
                logger.warning("Failed to send error message to Discord: %s", format_exception_message(send_exc))


# ── HTTP endpoint (optional — for Interactions) ──────────────────────────────


def _verify_discord_signature(
    public_key_hex: str, signature_hex: str, timestamp: str, body: bytes
) -> bool:
    """Verify Discord's mandatory Ed25519 interaction signature."""
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    try:
        public_key = Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_key_hex))
        public_key.verify(bytes.fromhex(signature_hex), timestamp.encode() + body)
        return True
    except (InvalidSignature, ValueError):
        return False


@router.post("/discord/interactions")
async def discord_interactions(request: Request) -> JSONResponse:
    """Handle Discord Interaction endpoint (slash commands).

    This is optional — only needed if the admin configures an
    Interactions Endpoint URL in the Discord Developer Portal.
    """
    body = await request.body()

    # Discord REQUIRES Ed25519 signature verification on this endpoint (the
    # Developer Portal probes with bad signatures expecting a 401 before it
    # accepts the URL). The application public key comes from the bot's
    # extra config.
    creds = await _get_discord_creds()
    public_key = (creds or {}).get("public_key", "")
    if public_key:
        signature = request.headers.get("X-Signature-Ed25519", "")
        timestamp = request.headers.get("X-Signature-Timestamp", "")
        if not (
            signature
            and timestamp
            and _verify_discord_signature(public_key, signature, timestamp, body)
        ):
            logger.warning("Discord interaction rejected: invalid Ed25519 signature")
            return JSONResponse({"error": "Invalid request signature"}, status_code=401)
    else:
        warn_unauthenticated_once(
            "discord",
            "Discord interactions endpoint is UNAUTHENTICATED: no public_key "
            "configured on the discord_bot channel bot — add the application "
            "public key in Settings > Integrations > Channels & Bots to enable Ed25519 verification",
        )

    try:
        payload = json.loads(body)
    except Exception:
        return JSONResponse({"error": "Invalid payload"}, status_code=400)

    # Discord verification ping
    if payload.get("type") == 1:
        return JSONResponse({"type": 1})

    # Slash command or component interaction
    if payload.get("type") in (2, 3):
        # For now, acknowledge the interaction
        # Full slash command support can be added later
        return JSONResponse(
            {
                "type": 4,  # CHANNEL_MESSAGE_WITH_SOURCE
                "data": {
                    "content": notice("discord_processing", notice_language()),
                    "flags": 64,  # Ephemeral
                },
            }
        )

    return JSONResponse({"type": 1})


# ── Credential loading ───────────────────────────────────────────────────────


async def _get_discord_creds(*, refresh: bool = False) -> dict[str, Any] | None:
    """Load Discord bot credentials from the database; *refresh* reads a vault-held token again rather than from memory."""
    bot = await get_channel_bot("discord_bot")
    if not bot:
        return None

    if refresh:
        await invalidate_entity_secrets(bot, ("api_key_enc",))
    token = await reveal_field(bot, "api_key_enc") or ""
    if not token:
        return None

    public_key = ""
    if bot.extra_config_enc:
        try:
            extra = await reveal_json_field(bot, "extra_config_enc") or {}
            public_key = extra.get("public_key", "")
        except Exception as exc:
            logger.warning("Failed to decrypt Discord bot extra config: %s", format_exception_message(exc))
            # Credentials exist but are unreadable — fail closed, not open:
            # the sentinel is not valid hex, so verification always rejects.
            public_key = DECRYPT_FAILED_CREDENTIAL

    return {
        "token": token,
        "channel_bot_id": str(bot.id),
        "public_key": public_key,
    }
