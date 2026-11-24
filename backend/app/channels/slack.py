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

"""Slack channel adapter – Bot Events API + Web API.

Credentials are stored per channel bot in the DB (not env vars).
The Slack bot is created via the UI under Settings > Integrations > Channels & Bots.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import TYPE_CHECKING, Any

import httpx
from fastapi import APIRouter, HTTPException, Request

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
    log_preview,
    spawn_channel_task,
    tell_cut_turn,
    typing_keepalive,
)
from app.channels.router import handle_message
from app.core.security import verify_slack_signature
from app.core.utils import format_exception_message, is_graceful_shutdown_cancellation, markdown_to_slack
from app.services.agent_posts import record_slack_post
from app.services.channel_notices import notice, notice_language
from app.services.vault import reveal_field, reveal_json_field

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.models.user import User

logger = logging.getLogger(__name__)
router = APIRouter()

SLACK_API_BASE = "https://slack.com/api"

# ---------------------------------------------------------------------------
# Event deduplication (Redis-backed)
# ---------------------------------------------------------------------------
# When a Slack app subscribes to both ``message.channels`` and ``app_mention``,
# an @mention fires both events for the same underlying message.  We use
# Redis SETNX with a short TTL for atomic, cross-worker deduplication.
_DEDUP_TTL = 60  # seconds


async def _is_duplicate_event(channel_id: str, message_ts: str) -> bool:
    """Return True if this message was already dispatched for processing.

    Uses Redis ``SET … NX EX`` for atomic check-and-set that works across
    multiple uvicorn workers.  Falls back to allowing the message through
    if Redis is unavailable (better to double-process than silently drop).
    """
    key = f"slack:dedup:{channel_id}:{message_ts}"
    try:
        from app.core.redis import redis_client

        redis = await redis_client()
        # SET … NX returns True if the key was set (first time), None if it
        # already existed (duplicate).
        was_set = await redis.set(key, "1", nx=True, ex=_DEDUP_TTL)
        return was_set is None  # None → key existed → duplicate
    except Exception as exc:
        logger.warning("Slack dedup Redis check failed (allowing message): %s", format_exception_message(exc))
        return False  # fail open — allow processing


class SlackChannel(BaseChannel):
    channel_type = ChannelType.SLACK

    def __init__(self, bot_token: str):
        self.bot_token = bot_token

    async def send_message(self, message: OutboundMessage) -> str | None:
        channel_id = message.channel_user_id or message.channel_thread_id
        thread_ts = message.metadata.get("thread_ts")

        # Upload embedded images natively via Slack's external upload flow
        embedded_images = message.metadata.get("embedded_images", [])
        if embedded_images:
            await self._upload_images(channel_id, thread_ts, embedded_images)

        headers = {
            "Authorization": f"Bearer {self.bot_token}",
            "Content-Type": "application/json; charset=utf-8",
        }

        # Split long replies — Slack rejects chat.postMessage bodies over
        # ~40k characters with ``msg_too_long``, so an oversized agent reply
        # was previously dropped entirely.  Chunk on the original Markdown
        # (code-fence-aware) and convert each chunk to mrkdwn.
        chunks = chunk_message(
            message.text or "", max_chars=CHANNEL_MAX_CHARS.get("slack", 39000)
        )

        first_ts: str | None = None
        async with httpx.AsyncClient(timeout=30) as client:
            for chunk in chunks:
                payload: dict[str, str | None] = {
                    "channel": channel_id,
                    "text": markdown_to_slack(chunk),
                }
                if thread_ts:
                    payload["thread_ts"] = thread_ts

                resp = await client.post(
                    f"{SLACK_API_BASE}/chat.postMessage",
                    headers=headers,
                    json=payload,
                )
                data = resp.json()

                # If threading fails, retry without thread_ts (post to the
                # channel directly) and keep it off for the later chunks.
                if (
                    not data.get("ok")
                    and data.get("error") == "cannot_reply_to_message"
                    and thread_ts
                ):
                    logger.warning(
                        "Slack cannot_reply_to_message for thread_ts=%s, retrying without thread",
                        thread_ts,
                    )
                    thread_ts = None
                    payload.pop("thread_ts", None)
                    resp = await client.post(
                        f"{SLACK_API_BASE}/chat.postMessage",
                        headers=headers,
                        json=payload,
                    )
                    data = resp.json()

                if not data.get("ok"):
                    logger.error("Slack send_message failed: %s", data)
                    return first_ts
                await record_slack_post(data)
                if first_ts is None:
                    first_ts = data.get("ts")

        return first_ts

    async def _upload_images(
        self,
        channel_id: str | None,
        thread_ts: str | None,
        embedded_images: list[dict],
    ) -> None:
        """Upload embedded images to Slack via the external upload flow.

        ``files.uploadV2`` is an SDK-only wrapper, not an HTTP method — the
        real API is the two-step ``files.getUploadURLExternal`` (reserve an
        upload URL) + raw POST of the bytes + ``files.completeUploadExternal``
        (finalize and share to the channel/thread).
        """
        from app.channels.base import fetch_embedded_image, image_upload_filename

        for img_info in embedded_images:
            try:
                result = await fetch_embedded_image(img_info)
                if not result:
                    continue
                image_data, content_type = result
                filename = image_upload_filename(img_info.get("alt_text", "image")[:50] or "image", content_type)

                async with httpx.AsyncClient(timeout=30) as client:
                    # Step 1: reserve an upload URL
                    resp = await client.get(
                        f"{SLACK_API_BASE}/files.getUploadURLExternal",
                        headers={"Authorization": f"Bearer {self.bot_token}"},
                        params={
                            "filename": filename,
                            "length": str(len(image_data)),
                        },
                    )
                    data = resp.json()
                    if not data.get("ok"):
                        logger.warning("Slack getUploadURLExternal failed: %s", data)
                        continue
                    upload_url = data["upload_url"]
                    file_id = data["file_id"]

                    # Step 2: upload the raw bytes
                    up_resp = await client.post(
                        upload_url,
                        content=image_data,
                        headers={"Content-Type": content_type},
                    )
                    if up_resp.status_code != 200:
                        logger.warning(
                            "Slack file byte upload failed: %s %s",
                            up_resp.status_code,
                            up_resp.text[:200],
                        )
                        continue

                    # Step 3: finalize and share to the channel/thread
                    complete_payload: dict = {
                        "files": [
                            {
                                "id": file_id,
                                "title": img_info.get("alt_text", notice("generated_image", notice_language())),
                            }
                        ],
                    }
                    if channel_id:
                        complete_payload["channel_id"] = channel_id
                    if thread_ts:
                        complete_payload["thread_ts"] = thread_ts
                    resp = await client.post(
                        f"{SLACK_API_BASE}/files.completeUploadExternal",
                        headers={
                            "Authorization": f"Bearer {self.bot_token}",
                            "Content-Type": "application/json; charset=utf-8",
                        },
                        json=complete_payload,
                    )
                    data = resp.json()
                    if not data.get("ok"):
                        logger.warning("Slack completeUploadExternal failed: %s", data)
            except Exception as exc:
                logger.warning("Slack image upload error: %s", format_exception_message(exc))

    async def send_typing_indicator(self, channel_thread_id: str | None = None) -> None:
        """Show a typing indicator via Slack's assistant.threads.setStatus.

        Requires the ``assistant:write`` bot scope.  If the scope is missing
        or the API call fails for any reason, the error is logged at debug
        level and silently ignored so message processing is never blocked.

        ``channel_thread_id`` is expected in the ``{channel_id}:{thread_ts}``
        format produced by the Slack event handler.
        """
        if not channel_thread_id or ":" not in channel_thread_id:
            return
        channel_id, thread_ts = channel_thread_id.split(":", 1)
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    f"{SLACK_API_BASE}/assistant.threads.setStatus",
                    headers={
                        "Authorization": f"Bearer {self.bot_token}",
                        "Content-Type": "application/json; charset=utf-8",
                    },
                    json={
                        "channel_id": channel_id,
                        "thread_ts": thread_ts,
                        "status": "is thinking…",
                    },
                    timeout=5,
                )
                data = resp.json()
                if not data.get("ok"):
                    logger.debug(
                        "Slack assistant.threads.setStatus failed: %s",
                        data.get("error", "unknown"),
                    )
        except Exception:
            logger.warning("Slack typing indicator failed", exc_info=True)

    async def clear_typing_status(self, channel_thread_id: str | None = None) -> None:
        """Clear the 'is thinking…' status in Slack.

        Call this after the message is processed when the agent decides NOT
        to reply (e.g. ``[NO_RESPONSE]``), so the typing indicator doesn't
        persist forever in the thread.
        """
        if not channel_thread_id or ":" not in channel_thread_id:
            return
        channel_id, thread_ts = channel_thread_id.split(":", 1)
        try:
            async with httpx.AsyncClient() as client:
                await client.post(
                    f"{SLACK_API_BASE}/assistant.threads.setStatus",
                    headers={
                        "Authorization": f"Bearer {self.bot_token}",
                        "Content-Type": "application/json; charset=utf-8",
                    },
                    json={
                        "channel_id": channel_id,
                        "thread_ts": thread_ts,
                        "status": "",
                    },
                    timeout=5,
                )
        except Exception:
            logger.warning("Slack clear typing status failed", exc_info=True)


async def _resolve_slack_user(
    bot_token: str, slack_user_id: str
) -> tuple[str | None, str]:
    """Resolve a Slack user's email and display name via the ``users.info`` API.

    Returns ``(email, display_name)`` — both may be empty/None when the
    API call fails.  The display name is extracted at zero extra cost from
    the same API response used for email resolution.
    """
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                f"{SLACK_API_BASE}/users.info",
                headers={"Authorization": f"Bearer {bot_token}"},
                params={"user": slack_user_id},
            )
            data = resp.json()
            if data.get("ok"):
                profile = data.get("user", {}).get("profile", {})
                email = profile.get("email")
                display_name = (
                    profile.get("real_name")
                    or profile.get("display_name")
                    or data.get("user", {}).get("real_name", "")
                )
                return email, display_name
    except Exception as exc:
        logger.warning(
            "Failed to resolve Slack user info for %s: %s", slack_user_id, format_exception_message(exc)
        )
    return None, ""


BACKFILL_MESSAGE_LIMIT = 25


async def fetch_slack_thread_context(
    channel_id: str,
    thread_ts: str,
) -> str | None:
    """Fetch prior conversation context from a Slack thread.

    Uses the ``conversations.replies`` API to load the root message and
    all replies in the thread.  Returns a formatted summary string, or
    ``None`` if unavailable.

    Requires the ``channels:history`` (public) or ``groups:history``
    (private) bot token scope.
    """
    bot = await get_channel_bot("slack_bot")
    if not bot or not bot.api_key_enc:
        logger.debug("Cannot fetch Slack thread context — no bot credentials")
        return None

    bot_token = await reveal_field(bot, "api_key_enc") or ""
    if not bot_token:
        return None

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(
                f"{SLACK_API_BASE}/conversations.replies",
                headers={"Authorization": f"Bearer {bot_token}"},
                params={
                    "channel": channel_id,
                    "ts": thread_ts,
                    "limit": str(BACKFILL_MESSAGE_LIMIT),
                },
                timeout=15,
            )
            data = resp.json()
            if not data.get("ok"):
                err = data.get("error", "unknown")
                logger.info(
                    "Cannot read Slack thread (channel=%s, ts=%s): %s",
                    channel_id[:20],
                    thread_ts[:20],
                    err,
                )
                return None

            messages = data.get("messages", [])
            if not messages:
                return None

            lines: list[str] = []
            for msg in messages:
                user_id = msg.get("user", "bot")
                text = (msg.get("text") or "")[:500]
                ts = msg.get("ts", "")
                if text:
                    lines.append(f"[{ts}] <{user_id}>: {text}")

            if not lines:
                return None

            return (
                "--- Prior Slack thread messages ---\n"
                + "\n".join(lines)
                + "\n--- End of thread context ---"
            )
    except Exception as exc:
        logger.debug("Failed to fetch Slack thread context: %s", format_exception_message(exc))
        return None


async def _get_slack_bot_creds() -> tuple[str, str] | None:
    """Load Slack bot credentials, preferring company-managed channel bots."""
    bot = await get_channel_bot("slack_bot")
    if not bot:
        return None

    bot_token = (await reveal_field(bot, "api_key_enc")) or ""
    signing_secret = ""
    if bot.extra_config_enc:
        try:
            extra = await reveal_json_field(bot, "extra_config_enc") or {}
            signing_secret = extra.get("signing_secret", "")
        except Exception as exc:
            logger.warning("Failed to decrypt Slack extra config: %s", format_exception_message(exc))
            # Credentials exist but are unreadable — fail closed, not open:
            # signature verification runs with a never-matching secret.
            signing_secret = DECRYPT_FAILED_CREDENTIAL

    return bot_token, signing_secret


async def open_slack_dm_thread(user_email: str, text: str) -> str:
    """Post *text* in a user's direct messages with the bot.

    Returns the thread the router reads off a reply in it
    (``<DM channel>:<root ts>``): the conversation continued in Slack is bound
    to it. The user is found by their platform email (``users.lookupByEmail``,
    the ``users:read.email`` scope every Slack bot already needs), and a
    message posted to their member id lands in their DM with the bot
    (``chat:write``).
    """
    from app.services.channel_continuation import (
        REASON_DELIVERY_FAILED,
        REASON_NOT_CONFIGURED,
        REASON_USER_NOT_FOUND,
        ChannelContinuationError,
    )

    creds = await _get_slack_bot_creds()
    bot_token = creds[0] if creds else ""
    if not bot_token:
        raise ChannelContinuationError(REASON_NOT_CONFIGURED, "No Slack bot is connected.")
    headers = {"Authorization": f"Bearer {bot_token}"}
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            lookup = await client.get(
                f"{SLACK_API_BASE}/users.lookupByEmail",
                headers=headers,
                params={"email": user_email},
            )
            lookup_data = lookup.json()
            if not isinstance(lookup_data, dict):
                lookup_data = {}
            slack_user_id = (
                (lookup_data.get("user") or {}).get("id") if lookup_data.get("ok") else None
            )
            if not slack_user_id:
                lookup_error = str(lookup_data.get("error") or "unknown")
                if lookup_error == "users_not_found":
                    logger.info("Slack continuation: no Slack member for the user")
                    raise ChannelContinuationError(
                        REASON_USER_NOT_FOUND,
                        "No member of the Slack workspace uses your email address.",
                    )
                # Rate limited, a revoked token, a missing scope, an outage.
                logger.warning("Slack continuation: the member lookup failed (%s)", lookup_error)
                raise ChannelContinuationError(
                    REASON_DELIVERY_FAILED,
                    "Slack could not look your account up. Try again in a moment.",
                )
            resp = await client.post(
                f"{SLACK_API_BASE}/chat.postMessage",
                headers={**headers, "Content-Type": "application/json; charset=utf-8"},
                json={"channel": slack_user_id, "text": markdown_to_slack(text)},
            )
            posted = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        # A timeout, a refused connection, or a body that is not JSON (an
        # outage page).
        logger.warning(
            "Slack continuation: Slack could not be reached: %s",
            format_exception_message(exc),
        )
        raise ChannelContinuationError(
            REASON_DELIVERY_FAILED,
            "Slack could not be reached. Try again in a moment.",
        ) from None
    if not isinstance(posted, dict):
        posted = {}
    dm_channel = posted.get("channel") if posted.get("ok") else None
    root_ts = posted.get("ts") if posted.get("ok") else None
    if not dm_channel or not root_ts:
        logger.warning(
            "Slack continuation: the opening message was refused (%s)",
            posted.get("error", "unknown"),
        )
        raise ChannelContinuationError(
            REASON_DELIVERY_FAILED,
            "Slack did not accept the message. Check that the bot can send direct messages.",
        )
    return f"{dm_channel}:{root_ts}"


@router.post("/slack/events")
async def slack_events(request: Request):
    body = await request.body()
    try:
        data = json.loads(body)
    except (json.JSONDecodeError, Exception):
        logger.warning("Slack webhook received invalid JSON payload")
        return {"ok": True}

    # Load credentials from DB
    creds = await _get_slack_bot_creds()
    if not creds:
        logger.warning("No slack_bot channel bot configured")
        return {"ok": True}

    bot_token, signing_secret = creds

    # Signature verification — fail closed: a bot saved without a signing
    # secret must not silently become an unauthenticated webhook.
    if not signing_secret:
        logger.error(
            "Slack webhook rejected: the slack_bot channel bot has no "
            "signing_secret configured — add it in Settings > Integrations > Channels & Bots "
            "(Slack app > Basic Information > Signing Secret)"
        )
        raise HTTPException(status_code=401, detail="Slack signing secret not configured")
    sig = request.headers.get("X-Slack-Signature", "")
    ts = request.headers.get("X-Slack-Request-Timestamp", "")
    if not verify_slack_signature(body, signing_secret, sig, ts):
        raise HTTPException(status_code=401, detail="Invalid Slack signature")

    # URL verification challenge — answered only AFTER the signature check
    # (Slack signs url_verification requests too); before it, the endpoint
    # would be an unauthenticated reflection oracle.
    if data.get("type") == "url_verification":
        return {"challenge": data["challenge"]}

    if data.get("type") == "event_callback":
        event = data.get("event", {})
        event_type = event.get("type", "unknown")

        if event.get("bot_id") or event.get("subtype") == "bot_message":
            logger.debug(
                "Slack webhook: ignoring bot message (bot_id=%s)", event.get("bot_id")
            )
            return {"ok": True}

        if event_type in ("message", "app_mention"):
            text = (event.get("text") or "").strip()
            slack_user_id = event.get("user")
            channel_id = event.get("channel")
            channel_type = event.get("channel_type", "")
            thread_ts = event.get("thread_ts") or event.get("ts")

            # --- Bot mention detection ---
            # Extract the bot's own user ID from the event callback's
            # authorizations field so we can detect <@BOT_ID> in text
            # even when the event type is "message" (not "app_mention").
            bot_user_id = ""
            authorizations = data.get("authorizations", [])
            if authorizations:
                bot_user_id = authorizations[0].get("user_id", "")

            # Build a regex to match bot mentions: <@U123> or <@U123|name>
            bot_mention_re = (
                re.compile(rf"<@{re.escape(bot_user_id)}(?:\|[^>]*)?>")
                if bot_user_id
                else None
            )

            # Detect bot mention: app_mention event OR <@BOT_ID> in text
            bot_was_mentioned = event_type == "app_mention"
            if not bot_was_mentioned and bot_mention_re and bot_mention_re.search(text):
                bot_was_mentioned = True

            logger.info(
                "Slack webhook received: event_type=%s, channel_type=%s, "
                "channel=%s, user=%s, bot_mentioned=%s, text_length=%d",
                event_type,
                channel_type or "(unknown)",
                channel_id,
                slack_user_id,
                bot_was_mentioned,
                len(text or ""),
            )

            if not text or not slack_user_id or not bot_token:
                logger.info("Slack webhook: missing text/user/token — skipping")
                return {"ok": True}

            # Dedup: skip if we've already dispatched this message for
            # processing.  Slack fires both ``app_mention`` and ``message``
            # events for @mentions when both subscriptions are active.
            message_ts = event.get("ts", "")
            if (
                channel_id
                and message_ts
                and await _is_duplicate_event(channel_id, message_ts)
            ):
                logger.info(
                    "Slack webhook: skipping duplicate event — channel=%s "
                    "ts=%s event_type=%s (already processing from a prior "
                    "event for the same message)",
                    channel_id,
                    message_ts,
                    event_type,
                )
                return {"ok": True}

            # Strip bot mention(s) from text for cleaner agent input
            # (mirrors Teams behaviour which strips @mention entities).
            original_text = text
            if bot_mention_re:
                text = bot_mention_re.sub("", text).strip()
            if original_text != text:
                logger.debug(
                    "Slack message: stripped bot mention: %s → %s",
                    log_preview(original_text),
                    log_preview(text),
                )

            # After stripping mentions, the text may be empty (bare @mention)
            if not text:
                logger.info(
                    "Slack message: text empty after stripping mentions — skipping"
                )
                return {"ok": True}

            # Resolve Slack user → Copilot email + display name
            email, slack_display_name = await _resolve_slack_user(
                bot_token, slack_user_id
            )
            logger.info(
                "Slack message: resolved has_email=%s, has_name=%s for slack_user=%s",
                bool(email),
                bool(slack_display_name),
                slack_user_id,
            )

            # Detect if this message is a reply inside a thread.
            # When thread_ts != ts, the message is a threaded reply (not a
            # top-level channel message).  This signals that the user is
            # in an ongoing conversation — likely with the bot.
            raw_thread_ts = event.get("thread_ts")
            raw_ts = event.get("ts", "")
            is_threaded_reply = bool(raw_thread_ts and raw_thread_ts != raw_ts)

            # Extract Slack file attachments (shared via the message).
            # Each file has url_private_download which needs Bearer auth.
            slack_file_attachments: list[InboundFileAttachment] = []
            for slack_file in event.get("files", []):
                download_url = slack_file.get("url_private_download") or slack_file.get("url_private")
                if download_url and slack_file.get("mode") != "tombstone":
                    slack_file_attachments.append(
                        InboundFileAttachment(
                            url=download_url,
                            filename=slack_file.get("name", "file"),
                            content_type=slack_file.get("mimetype", "application/octet-stream"),
                            size=slack_file.get("size", 0),
                            platform_file_id=slack_file.get("id", ""),
                            auth_headers={"Authorization": f"Bearer {bot_token}"},
                        )
                    )
            if slack_file_attachments:
                logger.info(
                    "Slack message: %d file attachment(s) detected",
                    len(slack_file_attachments),
                )

            inbound = InboundMessage(
                channel=ChannelType.SLACK,
                text=text,
                user_email=email,
                # The sender's own member id, never the channel: the router
                # resolves and links a sender by it, and a channel id would
                # resolve every member of the channel as one user.
                # Replies target the channel of ``channel_thread_id``.
                channel_user_id=slack_user_id,
                channel_thread_id=(
                    f"{channel_id}:{thread_ts}" if thread_ts else channel_id
                ),
                channel_message_id=raw_ts,
                file_attachments=slack_file_attachments,
                raw=event,
                metadata={
                    "channel_type": channel_type,
                    "event_type": event_type,  # "message" or "app_mention"
                    "bot_was_mentioned": bot_was_mentioned,
                    "is_threaded_reply": is_threaded_reply,
                    # Slack display name from users.info — used as
                    # platform-reported identity for anonymous users
                    # (matches WhatsApp contact_name, Teams contact_name, etc.)
                    "contact_name": slack_display_name,
                },
            )

            spawn_channel_task(
                _process_slack_message(inbound, bot_token, channel_id, thread_ts),
                name="slack-message",
            )

    return {"ok": True}


async def _process_slack_message(
    inbound: InboundMessage, bot_token: str, channel_id: str, thread_ts: str | None
) -> None:
    channel_type = inbound.metadata.get("channel_type", "?")
    event_type = inbound.metadata.get("event_type", "?")
    bot_mentioned = inbound.metadata.get("bot_was_mentioned", False)
    is_dm = channel_type == "im"
    logger.info(
        "Slack processing: started — event_type=%s, channel_type=%s, bot_mentioned=%s, is_dm=%s, has_email=%s, text_length=%d",
        event_type,
        channel_type,
        bot_mentioned,
        is_dm,
        bool(inbound.user_email),
        len(inbound.text or ""),
    )
    try:
        channel = SlackChannel(bot_token)

        # Only show the typing indicator ("is thinking…") when the message
        # is likely to produce a response.  In group channels where the bot
        # was NOT @mentioned, the message will probably be dropped by the
        # only_mention gate — showing "Analyzing…" and then going silent is
        # confusing for users.  DMs, explicit @mentions, and threaded replies
        # (replies inside a thread — likely a conversation with the bot)
        # always get the typing indicator.
        is_threaded_reply = bool(inbound.metadata.get("is_threaded_reply"))
        show_typing = is_dm or bot_mentioned or is_threaded_reply

        logger.info(
            "Slack processing: routing to handle_message — has_email=%s, "
            "channel_type=%s, channel_thread_id=%s, show_typing=%s",
            bool(inbound.user_email),
            channel_type,
            inbound.channel_thread_id,
            show_typing,
        )
        if show_typing:
            async with typing_keepalive(channel, inbound.channel_thread_id):
                outbound = await handle_message(inbound)
        else:
            outbound = await handle_message(inbound)
        if outbound.suppress:
            logger.info(
                "Slack processing: suppressing reply — message was silently "
                "dropped (only_mention gate, [NO_RESPONSE], publish disabled, "
                "or agent rejected channel). event_type=%s, channel_type=%s, "
                "bot_mentioned=%s, channel_thread_id=%s",
                event_type,
                channel_type,
                bot_mentioned,
                inbound.channel_thread_id,
            )
            # Clear the typing indicator so "is thinking…" doesn't persist
            # forever in the thread when no reply is sent.
            if show_typing:
                await channel.clear_typing_status(inbound.channel_thread_id)
            return
        outbound.channel_user_id = channel_id
        outbound.metadata["thread_ts"] = thread_ts
        logger.info(
            "Slack processing: sending reply — length=%d chars",
            len(outbound.text) if outbound.text else 0,
        )
        msg_ts = await channel.send_message(outbound)
        if msg_ts:
            logger.info("Slack processing: reply sent — ts=%s", msg_ts)
        else:
            logger.error("Slack processing: failed to send reply")
    except asyncio.CancelledError as cut:
        # Cut by a platform update (a rolling deploy, a restart): said in the
        # thread, never silence, unless the router handed the turn on and said so.
        if is_graceful_shutdown_cancellation(cut) and (is_dm or bot_mentioned):

            async def _post(text: str) -> None:
                await SlackChannel(bot_token).send_message(
                    OutboundMessage(
                        text=text,
                        channel_user_id=channel_id,
                        metadata={"thread_ts": thread_ts} if thread_ts else {},
                    )
                )

            await tell_cut_turn(inbound, _post)
        raise
    except Exception as e:
        logger.error("Slack processing: EXCEPTION — %s", format_exception_message(e), exc_info=True)
        # Match the Telegram / Discord / Google Chat adapters: in DMs and
        # explicit @mentions, tell the user something went wrong instead of
        # showing "is thinking…" followed by permanent silence.
        if is_dm or bot_mentioned:
            try:
                error_outbound = OutboundMessage(
                    text=notice("adapter_error", notice_language()),
                    channel_user_id=channel_id,
                    metadata={"thread_ts": thread_ts} if thread_ts else {},
                )
                await SlackChannel(bot_token).send_message(error_outbound)
            except Exception:
                logger.debug(
                    "Slack processing: failed to send error notice", exc_info=True
                )


async def run_queued_message(inbound: InboundMessage) -> None:
    """Answer a direct message the router queued behind a running turn.

    Processed like a message just received; its files were stored when it was
    queued, and the router loads them. No Slack bot connected any more is a
    ``QueuedMessageUndeliverableError``: the drain starts it again, then drops it.
    """
    from app.services.channel_continuation import QueuedMessageUndeliverableError

    creds = await _get_slack_bot_creds()
    bot_token = creds[0] if creds else ""
    if not bot_token:
        raise QueuedMessageUndeliverableError("no Slack bot is connected")
    # The channel comes from the thread id: ``channel_user_id`` is the sender.
    channel_id, _, thread_ts = (inbound.channel_thread_id or "").partition(":")
    if not channel_id:
        raise QueuedMessageUndeliverableError("the queued message names no Slack channel")
    await _process_slack_message(inbound, bot_token, channel_id, thread_ts or None)


async def post_queued_notice(inbound: InboundMessage, text: str) -> bool:
    """Post *text* in the thread of a queued direct message; ``False`` when no bot is connected or Slack refused it."""
    creds = await _get_slack_bot_creds()
    bot_token = creds[0] if creds else ""
    if not bot_token:
        return False
    channel_id, _, thread_ts = (inbound.channel_thread_id or "").partition(":")
    if not channel_id:
        return False
    sent = await SlackChannel(bot_token).send_message(
        OutboundMessage(
            text=text,
            channel_user_id=channel_id,
            metadata={"thread_ts": thread_ts} if thread_ts else {},
        )
    )
    return sent is not None


# ---------------------------------------------------------------------------
# Interactivity: the decision card's buttons and modal (#842, S10, I.3)
# ---------------------------------------------------------------------------


async def _verified_form(request: Request) -> tuple[dict[str, Any] | None, str]:
    """The ``payload`` of a signed interactivity request, and the bot token.

    Fails closed exactly as ``/slack/events``: no bot, no signing secret or a
    bad signature is a 401 and nothing is read.
    """
    from urllib.parse import parse_qs

    body = await request.body()
    creds = await _get_slack_bot_creds()
    if not creds:
        raise HTTPException(status_code=401, detail="Slack bot not configured")
    bot_token, signing_secret = creds
    if not signing_secret:
        logger.error("Slack interaction rejected: the slack_bot channel bot has no signing_secret configured")
        raise HTTPException(status_code=401, detail="Slack signing secret not configured")
    sig = request.headers.get("X-Slack-Signature", "")
    ts = request.headers.get("X-Slack-Request-Timestamp", "")
    if not verify_slack_signature(body, signing_secret, sig, ts):
        raise HTTPException(status_code=401, detail="Invalid Slack signature")
    try:
        raw = parse_qs(body.decode()).get("payload", [""])[0]
        payload = json.loads(raw)
    except (UnicodeDecodeError, ValueError):
        return None, bot_token
    return (payload if isinstance(payload, dict) else None), bot_token


async def _interaction_user(db: AsyncSession, bot_token: str, slack_user_id: str) -> User | None:
    """The platform user behind a click: by Slack email, then by the linked Slack identity."""
    from app.channels.router import resolve_user_by_channel_identity, resolve_user_by_email

    if not slack_user_id:
        return None
    email, _name = await _resolve_slack_user(bot_token, slack_user_id)
    if email:
        user = await resolve_user_by_email(db, email)
        if user is not None:
            return user
    return await resolve_user_by_channel_identity(db, ChannelType.SLACK.value, slack_user_id)


async def _slack_api(bot_token: str, method: str, body: dict[str, Any]) -> None:
    """One Web API call of the interactivity path; a refusal is logged, never raised."""
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                f"{SLACK_API_BASE}/{method}",
                headers={"Authorization": f"Bearer {bot_token}", "Content-Type": "application/json; charset=utf-8"},
                json=body,
            )
            data = resp.json()
        if not isinstance(data, dict) or not data.get("ok"):
            logger.warning("Slack %s refused: %s", method, (data or {}).get("error", "unknown"))
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("Slack %s failed: %s", method, format_exception_message(exc))


async def _settle_click(
    bot_token: str,
    *,
    value: dict[str, str],
    slack_user_id: str,
    channel: str,
    ts: str,
    option: str | None = None,
    text: str | None = None,
) -> None:
    """Answer a click or a modal submission, then update the card or tell the clicker why not."""
    from app.core.database import async_session
    from app.services import slack_decision_cards as cards

    async with async_session() as db:
        user = await _interaction_user(db, bot_token, slack_user_id)
        language = notice_language(user)
        message, ephemeral = await cards.answer(db, value, user, language, option=option, text=text)
    if message is not None and channel and ts:
        await _slack_api(bot_token, "chat.update", {"channel": channel, "ts": ts, **message})
    if ephemeral and channel and slack_user_id:
        await _slack_api(
            bot_token, "chat.postEphemeral", {"channel": channel, "user": slack_user_id, "text": ephemeral}
        )


async def _open_text_modal(bot_token: str, *, value: dict[str, str], trigger_id: str, channel: str, ts: str) -> None:
    from app.core.database import async_session
    from app.services import slack_decision_cards as cards

    async with async_session() as db:
        view = await cards.view_for(db, value, None, notice_language())
    if view is None or not trigger_id:
        return
    modal = cards.text_modal(view, notice_language(), channel=channel, ts=ts)
    await _slack_api(bot_token, "views.open", {"trigger_id": trigger_id, "view": modal})


@router.post("/slack/interactions")
async def slack_interactions(request: Request) -> dict[str, Any]:
    """Slack's interactivity Request URL: a decision card's button or its text modal.

    Answers 200 at once (Slack waits 3 seconds) and does the work in a tracked
    task; a modal submission answers ``{}`` so Slack closes the modal. The
    answer itself goes through the decision router as the clicker (I.3).
    """
    from app.services import slack_decision_cards as cards

    payload, bot_token = await _verified_form(request)
    if payload is None:
        return {"ok": True}
    kind = payload.get("type")
    slack_user_id = str((payload.get("user") or {}).get("id") or "")
    if kind == "block_actions":
        actions = payload.get("actions") or []
        action = actions[0] if actions and isinstance(actions[0], dict) else {}
        action_id = str(action.get("action_id") or "")
        value = cards.parse_value(action.get("value"))
        if value is None:
            # A link button, or a click we did not draw: nothing to answer.
            return {"ok": True}
        channel = str((payload.get("channel") or {}).get("id") or "")
        ts = str((payload.get("container") or {}).get("message_ts") or (payload.get("message") or {}).get("ts") or "")
        if action_id == cards.ACTION_TEXT:
            spawn_channel_task(
                _open_text_modal(
                    bot_token, value=value, trigger_id=str(payload.get("trigger_id") or ""), channel=channel, ts=ts
                ),
                name="slack-decision-modal",
            )
        elif action_id.startswith(cards.ACTION_ANSWER + ":"):
            spawn_channel_task(
                _settle_click(
                    bot_token,
                    value=value,
                    slack_user_id=slack_user_id,
                    channel=channel,
                    ts=ts,
                    option=value.get("option"),
                ),
                name="slack-decision-answer",
            )
        return {"ok": True}
    if kind == "view_submission":
        view = payload.get("view") or {}
        if view.get("callback_id") != cards.MODAL_CALLBACK:
            return {}
        meta = cards.parse_value(view.get("private_metadata"))
        if meta is None:
            return {}
        values = ((view.get("state") or {}).get("values") or {}).get(cards.MODAL_BLOCK) or {}
        text = str((values.get(cards.MODAL_INPUT) or {}).get("value") or "")
        spawn_channel_task(
            _settle_click(
                bot_token,
                value=meta,
                slack_user_id=slack_user_id,
                channel=meta.get("channel", ""),
                ts=meta.get("ts", ""),
                text=text,
            ),
            name="slack-decision-text",
        )
        return {}
    return {"ok": True}
