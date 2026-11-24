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

"""Google Chat channel adapter – Webhook / Service Account.

Receives messages via webhook from Google Chat and sends responses
back via the Google Chat REST API.  Credentials are stored per channel bot
in the database (``googlechat_bot`` provider), not in environment variables.

Google Chat uses a service account or app-level credentials.  The API key
field stores the service account JSON key (base64-encoded or raw JSON).

Setup:
  1. In Google Cloud Console, enable the Google Chat API.
  2. Create a Google Chat App in the Chat API settings.
  3. Set the connection settings: use HTTPS endpoint URL.
  4. Set the endpoint URL to: ``{base_url}/api/channels/googlechat/webhook``
  5. Create a service account and download the JSON key.
  6. Create a ``googlechat_bot`` channel bot in Copilot with the JSON key.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from typing import Any

import httpx
from fastapi import APIRouter, Request
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
    spawn_channel_task,
    typing_keepalive,
    warn_unauthenticated_once,
)
from app.channels.router import handle_message
from app.core.utils import format_exception_message, markdown_to_slack
from app.services.channel_notices import notice, notice_language
from app.services.vault import reveal_field, reveal_json_field

router = APIRouter()
logger = logging.getLogger(__name__)

# Google Chat message character limit
GOOGLE_CHAT_MAX_MESSAGE_LENGTH = CHANNEL_MAX_CHARS.get("google_chat", 4096)


class GoogleChatChannel(BaseChannel):
    """Google Chat channel adapter using REST API."""

    channel_type = ChannelType.GOOGLE_CHAT
    # The Chat REST API exposes no typing endpoint — skip the keepalive loop.
    supports_typing = False

    def __init__(self, credentials_json: str, space_name: str | None = None):
        self.credentials_json = credentials_json
        self.space_name = space_name
        self._base_url = "https://chat.googleapis.com/v1"

    async def _get_access_token(self) -> str:
        """Get an OAuth2 access token using the service account credentials.

        A new ``GoogleChatChannel`` instance is built per message, so the
        token is cached in the shared process-wide app-token cache (keyed on
        a hash of the credentials) — otherwise every single message pays a
        full JWT-grant token exchange.
        """
        from app.integrations.http import (
            app_token_cache_key,
            get_cached_app_token,
            set_cached_app_token,
        )

        creds_str = (
            self.credentials_json
            if isinstance(self.credentials_json, str)
            else json.dumps(self.credentials_json)
        )
        cache_key = app_token_cache_key("googlechat_bot", creds_str)
        cached = get_cached_app_token(cache_key)
        if cached:
            return cached

        try:
            import jwt as pyjwt

            creds = (
                json.loads(self.credentials_json)
                if isinstance(self.credentials_json, str)
                else self.credentials_json
            )

            now = int(time.time())
            payload = {
                "iss": creds["client_email"],
                "scope": "https://www.googleapis.com/auth/chat.bot",
                "aud": "https://oauth2.googleapis.com/token",
                "iat": now,
                "exp": now + 3600,
            }
            signed_jwt = pyjwt.encode(payload, creds["private_key"], algorithm="RS256")

            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    "https://oauth2.googleapis.com/token",
                    data={
                        "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                        "assertion": signed_jwt,
                    },
                    timeout=15,
                )
                if resp.status_code == 200:
                    token_data = resp.json()
                    token = token_data["access_token"]
                    set_cached_app_token(
                        cache_key, token, token_data.get("expires_in", 3600)
                    )
                    return token
                logger.error("Google Chat OAuth failed: %s", resp.text[:500])
        except ImportError:
            logger.error("pyjwt is required for Google Chat service account auth")
        except Exception as exc:
            logger.error(
                "Google Chat auth error: %s",
                format_exception_message(exc),
                exc_info=True,
            )

        return ""

    async def send_message(self, message: OutboundMessage) -> str | None:
        """Send a message to a Google Chat space via REST API."""
        space = message.channel_thread_id or self.space_name
        if not space:
            logger.error("No space_name for Google Chat message")
            return None

        token = await self._get_access_token()
        if not token:
            logger.error("No access token for Google Chat")
            return None

        # Convert standard Markdown to Google Chat's native markup —
        # Google Chat uses *bold* / _italic_ / ~strike~ / <url|text> (the
        # same surface as Slack mrkdwn) and renders ``**bold**`` and ``##``
        # headings literally. Deterministic conversion mirrors the Slack
        # and Teams adapters instead of relying on the prompt-level format
        # override alone.
        text = markdown_to_slack(message.text or "")
        # Split long messages using code-fence-aware chunking
        chunks = chunk_message(text, max_chars=GOOGLE_CHAT_MAX_MESSAGE_LENGTH)

        # Determine the parent resource name for the message
        # Space name format: spaces/XXXXXXXX
        parent = space if space.startswith("spaces/") else f"spaces/{space}"

        # If we have a thread name, reply in the thread
        thread_body: dict[str, Any] = {}
        thread_name = message.metadata.get("thread_name") if message.metadata else None
        if thread_name:
            thread_body["thread"] = {"name": thread_name}
            thread_body["threadReply"] = True

        # Build Cards v2 for embedded images (Google Chat renders these natively)
        cards_v2: list[dict[str, Any]] = []
        embedded_images = message.metadata.get("embedded_images", [])
        for img_info in embedded_images:
            cards_v2.append(
                {
                    "cardId": f"img_{img_info['image_id'][:8]}",
                    "card": {
                        "sections": [
                            {
                                "widgets": [
                                    {
                                        "image": {
                                            "imageUrl": img_info["public_url"],
                                            "altText": img_info.get(
                                                "alt_text", notice("generated_image", notice_language())
                                            ),
                                        }
                                    }
                                ],
                            }
                        ],
                    },
                }
            )

        last_msg_name: str | None = None
        async with httpx.AsyncClient() as client:
            for chunk in chunks:
                body: dict[str, Any] = {
                    "text": chunk,
                    **thread_body,
                }
                if cards_v2:
                    body["cardsV2"] = cards_v2
                    cards_v2 = []  # only attach to the first chunk

                resp = await client.post(
                    f"{self._base_url}/{parent}/messages",
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Content-Type": "application/json",
                    },
                    json=body,
                    timeout=15,
                )
                if resp.status_code == 200:
                    data = resp.json()
                    last_msg_name = data.get("name", "")
                else:
                    logger.error(
                        "Google Chat sendMessage failed: status=%s body=%s",
                        resp.status_code,
                        resp.text[:500],
                    )
        return last_msg_name

    async def send_typing_indicator(self, channel_thread_id: str | None = None) -> None:
        """Google Chat doesn't support typing indicators via REST API."""
        # No-op — Google Chat API doesn't provide a typing indicator endpoint


# ── Inbound files ────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class _ChatUpload:
    """A file uploaded to Chat with a message, before its download is authorized."""

    resource_name: str
    filename: str
    content_type: str


def _str_field(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _message_files(message: dict) -> tuple[list[_ChatUpload], list[str]]:
    """The files of *message*: those uploaded to Chat, and a line naming each Drive file.

    Content uploaded to Chat carries an ``attachmentDataRef``, whose
    ``resourceName`` ``media.download`` serves to the app's own credentials
    (the ``chat.bot`` scope). A Drive file carries a ``driveDataRef``
    instead: ``media.download`` does not serve Drive files and the Drive API
    needs a user's authorization, so the agent is told the file is there and
    how a Google integration reads it.
    """
    raw = message.get("attachment")
    uploads: list[_ChatUpload] = []
    drive_lines: list[str] = []
    for att in raw if isinstance(raw, list) else []:
        if not isinstance(att, dict):
            continue
        name = _str_field(att.get("contentName")) or "attachment"
        content_type = _str_field(att.get("contentType")) or "application/octet-stream"
        data_ref = att.get("attachmentDataRef")
        drive_ref = att.get("driveDataRef")
        resource = _str_field(data_ref.get("resourceName")) if isinstance(data_ref, dict) else ""
        drive_id = _str_field(drive_ref.get("driveFileId")) if isinstance(drive_ref, dict) else ""
        if resource:
            uploads.append(_ChatUpload(resource, name, content_type))
        elif drive_id:
            link = _str_field(att.get("downloadUri"))
            drive_lines.append(
                f"[Google Drive file: {name} ({content_type}) - Drive file_id {drive_id}"
                + (f" - link: {link}" if link else "")
                + " - a Chat app cannot download Drive files; a Google integration reads it"
                " with download_google_drive_file]"
            )
    return uploads, drive_lines


async def _attach_googlechat_files(
    inbound: InboundMessage, channel: GoogleChatChannel, uploads: list[_ChatUpload] | None
) -> None:
    """Hand the router the ``media.download`` of each of *uploads*, with the app's token.

    A file whose download cannot be authorized is left out and the message
    still goes on, as a file the router fails to download is.
    """
    if not uploads:
        return
    from app.integrations.providers.google import chat_media_url

    token = await channel._get_access_token()
    if not token:
        logger.warning("Google Chat processing: %d file attachment(s) left out - no access token", len(uploads))
        return
    for upload in uploads:
        url = chat_media_url(upload.resource_name)
        if url is None:
            logger.warning("Google Chat file left out: %s (not a media resource name)", upload.content_type)
            continue
        inbound.file_attachments.append(
            InboundFileAttachment(
                url=f"{url}?alt=media",
                filename=upload.filename,
                content_type=upload.content_type,
                platform_file_id=upload.resource_name,
                auth_headers={"Authorization": f"Bearer {token}"},
            )
        )
    logger.info(
        "Google Chat processing: %d of %d file attachment(s) resolved",
        len(inbound.file_attachments),
        len(uploads),
    )


# ── Webhook endpoint ─────────────────────────────────────────────────────────

# Issuer of the bearer JWT Google Chat attaches to every webhook request.
_CHAT_ISSUER = "chat@system.gserviceaccount.com"
_CHAT_JWKS_URL = f"https://www.googleapis.com/service_accounts/v1/jwk/{_CHAT_ISSUER}"
_jwks_client: Any = None


def _verify_google_chat_jwt(token: str, project_number: str) -> bool:
    """Verify the Google-issued bearer JWT on an inbound Chat webhook.

    Google signs the token with the ``chat@system.gserviceaccount.com``
    system account; the audience is the app's Cloud project number.
    Runs synchronously (PyJWKClient uses urllib) — call via ``to_thread``.
    """
    import jwt as pyjwt

    global _jwks_client
    try:
        if _jwks_client is None:
            _jwks_client = pyjwt.PyJWKClient(_CHAT_JWKS_URL, cache_keys=True)
        signing_key = _jwks_client.get_signing_key_from_jwt(token)
        pyjwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            audience=project_number,
            issuer=_CHAT_ISSUER,
            # Allow 5 minutes of clock skew so slightly skewed hosts don't
            # intermittently reject legitimate deliveries on exp/iat.
            leeway=300,
        )
        return True
    except Exception as exc:
        logger.warning("Google Chat JWT verification failed: %s", format_exception_message(exc))
        return False


@router.post("/googlechat/webhook")
async def googlechat_webhook(request: Request) -> JSONResponse:
    """Receive Google Chat events via webhook.

    Google Chat sends events as JSON with a ``type`` field:
    - ``ADDED_TO_SPACE``: Bot added to a space
    - ``REMOVED_FROM_SPACE``: Bot removed from a space
    - ``MESSAGE``: User sent a message
    """
    # Authenticate the request before trusting any payload content. Google
    # attaches a bearer JWT (issuer chat@system.gserviceaccount.com,
    # audience = Cloud project number) to every webhook call.
    auth_creds = await _get_googlechat_creds()
    project_number = (auth_creds or {}).get("project_number", "")
    if project_number:
        auth_header = request.headers.get("Authorization", "")
        token = auth_header.removeprefix("Bearer ").strip()
        if not token or not await asyncio.to_thread(
            _verify_google_chat_jwt, token, project_number
        ):
            logger.warning("Google Chat webhook rejected: invalid bearer JWT")
            return JSONResponse({"error": "Unauthorized"}, status_code=401)
    else:
        warn_unauthenticated_once(
            "google_chat",
            "Google Chat webhook is UNAUTHENTICATED: no project_number "
            "configured on the googlechat_bot channel bot — add the Google "
            "Cloud project number in Settings > Integrations > Channels & Bots to enable bearer "
            "JWT verification",
        )

    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"text": ""})

    event_type = payload.get("type", "")

    # Handle bot added/removed events
    if event_type == "ADDED_TO_SPACE":
        space_type = payload.get("space", {}).get("type", "")
        logger.info("Google Chat bot added to %s space", space_type)
        return JSONResponse({"text": notice("google_chat_added", notice_language())})

    if event_type == "REMOVED_FROM_SPACE":
        logger.info("Google Chat bot removed from space")
        return JSONResponse({})

    if event_type == "CARD_CLICKED":
        # A decision card's button (#842, S11): answered only on a verified
        # request, so an unauthenticated deployment answers no click.
        if not project_number:
            logger.warning("Google Chat card click refused: no project_number to verify it")
            return JSONResponse({"error": "Unauthorized"}, status_code=401)
        return JSONResponse(await _decision_click(payload))

    # Handle message events
    if event_type != "MESSAGE":
        return JSONResponse({})

    message = payload.get("message", {})
    text = message.get("argumentText", "") or message.get("text", "")
    uploads, drive_lines = _message_files(message)
    if not text and not uploads and not drive_lines:
        return JSONResponse({})
    # A message carrying files is for the agent even without text (as on
    # Teams and Telegram); the Drive files it cannot download are named in it.
    text = (text if isinstance(text, str) else "").strip()
    if not text and (uploads or drive_lines):
        text = FILE_ONLY_PLACEHOLDER
    if drive_lines:
        text = "\n".join([text, *drive_lines])

    # Extract user info
    sender = message.get("sender", {})
    sender_name = sender.get("displayName", "")
    sender_email = sender.get("email", "")
    sender_type = sender.get("type", "")

    # Ignore bot messages
    if sender_type == "BOT":
        return JSONResponse({})

    # Extract space info
    space = payload.get("space", {})
    space_name = space.get("name", "")
    space_type = space.get("type", "")  # DM, ROOM, SPACE

    # Extract thread info
    thread = message.get("thread", {})
    thread_name = thread.get("name", "")

    # Detect DM vs group
    is_dm = space_type == "DM"

    # Detect mention — Google Chat provides annotations for @mentions
    bot_was_mentioned = False
    for annotation in message.get("annotations", []):
        if annotation.get("type") == "USER_MENTION":
            mention_user = annotation.get("userMention", {})
            if mention_user.get("type") == "BOT":
                bot_was_mentioned = True
                break

    logger.info(
        "Google Chat webhook: space=%s type=%s sender=%s has_email=%s dm=%s mentioned=%s text_length=%d "
        "files=%d drive_files=%d",
        space_name,
        space_type,
        sender.get("name", ""),
        bool(sender_email),
        is_dm,
        bot_was_mentioned,
        len(text),
        len(uploads),
        len(drive_lines),
    )

    # Credentials were already loaded for request authentication above
    creds = auth_creds
    if not creds:
        logger.warning("No googlechat_bot channel bot configured")
        return JSONResponse({"text": notice("google_chat_not_configured", notice_language())})

    # Build inbound message
    inbound = InboundMessage(
        channel=ChannelType.GOOGLE_CHAT,
        text=text,
        user_email=sender_email or None,
        channel_user_id=sender.get("name", ""),
        channel_thread_id=space_name,
        channel_message_id=message.get("name", ""),
        metadata={
            "sender_name": sender_name,
            "sender_email": sender_email,
            "space_type": space_type,
            "is_dm": is_dm,
            "bot_was_mentioned": bot_was_mentioned,
            "thread_name": thread_name,
        },
    )

    # Process asynchronously (return acknowledgment immediately). The files'
    # download token is acquired there too, off the webhook's answer.
    spawn_channel_task(
        _process_googlechat_message(inbound, creds["credentials_json"], uploads),
        name="googlechat-message",
    )

    # Return empty response (async processing will send reply)
    return JSONResponse({})


async def _process_googlechat_message(
    inbound: InboundMessage,
    credentials_json: str,
    uploads: list[_ChatUpload] | None = None,
) -> None:
    """Process a Google Chat message in the background.

    *uploads* are the files the message uploaded to Chat: handed to the router
    as ``file_attachments``, which it downloads, stores and attaches like any
    channel's.
    """
    is_dm = bool(inbound.metadata.get("is_dm"))
    bot_mentioned = bool(inbound.metadata.get("bot_was_mentioned"))

    show_typing = is_dm or bot_mentioned
    channel = GoogleChatChannel(credentials_json, space_name=inbound.channel_thread_id)

    try:
        await _attach_googlechat_files(inbound, channel, uploads)
        if show_typing:
            async with typing_keepalive(channel, inbound.channel_thread_id):
                outbound = await handle_message(inbound)
        else:
            outbound = await handle_message(inbound)

        if outbound and not outbound.suppress:
            outbound.channel_thread_id = inbound.channel_thread_id
            # Pass thread info for reply threading
            if not outbound.metadata:
                outbound.metadata = {}
            outbound.metadata["thread_name"] = inbound.metadata.get("thread_name", "")

            logger.info(
                "Google Chat processing: sending reply — length=%d chars",
                len(outbound.text) if outbound.text else 0,
            )
            msg_id = await channel.send_message(outbound)
            if msg_id:
                logger.info("Google Chat processing: reply sent — name=%s", msg_id)
            else:
                logger.error("Google Chat processing: failed to send reply")
        elif outbound and outbound.suppress:
            logger.info(
                "Google Chat processing: suppressing reply — space_type=%s, bot_mentioned=%s",
                inbound.metadata.get("space_type"),
                bot_mentioned,
            )
    except Exception as e:
        logger.error("Google Chat processing: EXCEPTION — %s", format_exception_message(e), exc_info=True)
        if is_dm or bot_mentioned:
            try:
                error_msg = OutboundMessage(
                    text=notice("adapter_error", notice_language()),
                    channel_thread_id=inbound.channel_thread_id,
                    metadata={"thread_name": inbound.metadata.get("thread_name", "")},
                )
                await channel.send_message(error_msg)
            except Exception as send_exc:
                logger.debug(
                    "Failed to send error message to Google Chat: %s", format_exception_message(send_exc)
                )


# ── Decision cards (#842, S11) ───────────────────────────────────────────────


async def _click_user(db: Any, user: dict[str, Any]) -> Any:
    """The platform user behind a Chat clicker: by email, else by the linked channel identity."""
    from app.channels.router import resolve_user_by_channel_identity, resolve_user_by_email

    email = str(user.get("email") or "")
    if email:
        found = await resolve_user_by_email(db, email)
        if found is not None:
            return found
    name = str(user.get("name") or "")
    if not name:
        return None
    return await resolve_user_by_channel_identity(db, ChannelType.GOOGLE_CHAT.value, name)


async def _decision_click(payload: dict[str, Any]) -> dict[str, Any]:
    """Answer a verified ``CARD_CLICKED`` on a decision card as the clicker (surface ``google_chat_card``)."""
    from app.core.database import async_session
    from app.services import google_chat_decision_cards as cards

    click = cards.parse_click(payload)
    if click is None:
        # A link button, or a click we did not draw: nothing to answer.
        return {}
    method, value, text = click
    raw_clicker = payload.get("user")
    clicker: dict[str, Any] = raw_clicker if isinstance(raw_clicker, dict) else {}
    async with async_session() as db:
        user = await _click_user(db, clicker)
        language = notice_language(user)
        if method == cards.ACTION_TEXT:
            message, private = await cards.answer(db, value, user, language, text=text)
        else:
            message, private = await cards.answer(db, value, user, language, option=value.get("option"), text=text)
    return cards.click_response(message, private, str(clicker.get("name") or ""))


# ── Credential loading ───────────────────────────────────────────────────────


async def _get_googlechat_creds() -> dict[str, Any] | None:
    """Load Google Chat bot credentials from the database."""
    bot = await get_channel_bot("googlechat_bot")
    if not bot:
        return None

    credentials_json = await reveal_field(bot, "api_key_enc") or ""
    if not credentials_json:
        return None

    project_number = ""
    if bot.extra_config_enc:
        try:
            extra = await reveal_json_field(bot, "extra_config_enc") or {}
            project_number = str(extra.get("project_number", "") or "")
        except Exception as exc:
            logger.warning("Failed to decrypt Google Chat bot extra config: %s", format_exception_message(exc))
            # Credentials exist but are unreadable — fail closed, not open:
            # the sentinel never matches a JWT audience, so verification
            # always rejects.
            project_number = DECRYPT_FAILED_CREDENTIAL

    return {
        "credentials_json": credentials_json,
        "channel_bot_id": str(bot.id),
        "project_number": project_number,
    }
