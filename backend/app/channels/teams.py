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

"""Microsoft Teams channel adapter – Entra ID + Bot Framework.

Credentials (tenant_id + app_id + app_password) are stored per channel bot
in the DB as a "teams_bot" provider, not in environment variables.
The adapter authenticates via the tenant-specific Entra ID endpoint.
"""

from __future__ import annotations

import asyncio
import html as html_module
import json
import logging
import mimetypes
import re
import secrets
from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode, urlparse

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

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
    warn_unauthenticated_once,
)
from app.channels.base import FILE_ONLY_PLACEHOLDER as _FILE_ONLY_PLACEHOLDER
from app.channels.router import (
    handle_message,
    resolve_user_by_channel_identity,
    resolve_user_by_email,
)
from app.channels.teams_graph_verdict import (
    _BatchDecision,
    _grace_open,
    _issued_client_state,
    _lifecycle_notification_proven,
    _notification_verdict,
    _parse_graph_resource,
    _read_subscription_record,
    _record_state,
    _subscription_key,
    decide_post,
    summarize_post,
)
from app.channels.teams_graph_worker import run_decision
from app.core.config import settings
from app.core.csp import PagePolicy, register_page_policy, script_hash_source
from app.core.database import get_db
from app.core.http_body import read_bounded_body
from app.core.security import decrypt_value, encrypt_value
from app.services.integration_notification_auth import (
    GRAPH_RECORD_FETCH_CHUNK,
    log_rate_limited,
    verification_required,
)
from app.services.teams_format import markdown_to_teams_text
from app.services.vault import reveal_field, reveal_json_field

logger = logging.getLogger(__name__)
router = APIRouter()


# ---------------------------------------------------------------------------
# Graph API subscription support for private/shared channel threads
# ---------------------------------------------------------------------------
#
# Standard (public) channels receive messages via Bot Framework (RSC grant).
# Private and shared channels don't — Bot Framework only delivers @mentions.
#
# When the bot is @mentioned in a private/shared channel thread:
#   1. Bot Framework delivers the @mention message (normal flow)
#   2. After responding, we create a Graph API subscription for the channel
#   3. Follow-up messages in the thread arrive via Graph notification
#   4. Messages where the bot is @mentioned are skipped (Bot Framework
#      handles those — no duplicates)
#   5. Threads inactive for 7 days are cleaned up + a notice is posted
#
# This gives private channels the same "smart thread continuation" behavior
# that standard channels get natively via Bot Framework.
# ---------------------------------------------------------------------------

from app.core.utils import format_exception_message, is_graceful_shutdown_cancellation
from app.integrations.providers.microsoft import GRAPH_API, _encode_share_id
from app.services.channel_notices import notice, notice_language

GRAPH_SUB_MAX_MINUTES = 4230  # ~2.94 days — max for channel message subscriptions
PRIV_THREAD_TTL = (
    60 * 60 * 24 * 7
)  # 7 days — thread monitoring window & cleanup threshold


class TeamsChannel(BaseChannel):
    channel_type = ChannelType.TEAMS

    def __init__(
        self,
        service_url: str,
        conversation_id: str,
        access_token: str,
        bot_id: str = "",
        conversation_type: str = "",
    ):
        self.service_url = service_url.rstrip("/")
        self.conversation_id = conversation_id
        self.access_token = access_token
        self.bot_id = bot_id
        # Bot Framework conversation type ("personal", "groupChat", "channel").
        # Only "channel" renders full-width Adaptive Cards; chat surfaces cap
        # cards to the message-bubble width, so display cards are sent there
        # as full-width Markdown text instead.
        self.conversation_type = conversation_type

    async def send_message(self, message: OutboundMessage) -> str | None:
        """Send *message* to the conversation, splitting into activities as needed.

        Returns the activity id of the first sent activity, ``""`` when the
        platform accepted the message asynchronously (HTTP 202 with an empty
        body — Bot Framework does this for Teams private/shared channels
        routed via Graph), or ``None`` when delivery failed.  Callers MUST
        treat ``""`` as success (check ``is not None``) — treating 202 as a
        failure previously disabled private-channel thread monitoring
        entirely, because the monitoring setup was gated on a truthy id.
        """
        activities = self._build_activities(message)
        if not activities:
            return None

        first_result: str | None = None
        for idx, activity in enumerate(activities):
            result = await self._post_activity(activity)
            if idx == 0:
                first_result = result
            if result is None:
                # First activity failing means the message was not delivered
                # at all; a later failure (e.g. one image card) is logged but
                # doesn't turn an already-delivered reply into a failure.
                if idx == 0:
                    return None
                logger.warning(
                    "Teams send_message: activity %d/%d failed after the "
                    "message body was delivered",
                    idx + 1,
                    len(activities),
                )
        return first_result

    def _build_activities(self, message: OutboundMessage) -> list[dict[str, Any]]:
        """Build the Bot Framework activities for an outbound message.

        A single logical message can require several activities:

        - Long text is chunked to stay under the Teams activity size cap
          (``CHANNEL_MAX_CHARS["teams"]``) — previously an oversized reply
          was rejected with HTTP 400 and silently lost.
        - Embedded images are delivered as SEPARATE hero-card activities.
          The Teams/Skype connector rejects an activity that carries BOTH
          renderable ``text`` and ``attachments`` with HTTP 400 ``Activity
          resulted into multiple skype activities`` (see the summary-vs-text
          note below), so images must never ride on the text activity.
        """
        activities: list[dict[str, Any]] = []

        activities.extend(self._build_card_or_text_activities(message))

        # Deliver embedded images as standalone hero-card activities so
        # Teams renders them natively — never combined with a text activity
        # (see the multiple-skype-activities constraint above).
        for img_info in message.metadata.get("embedded_images", []):
            img_activity: dict[str, Any] = {
                "type": "message",
                "attachments": [
                    {
                        "contentType": "application/vnd.microsoft.card.hero",
                        "content": {
                            "title": img_info.get("alt_text", notice("generated_image", notice_language())),
                            "images": [
                                {"url": img_info["public_url"]},
                            ],
                        },
                    }
                ],
            }
            if self.bot_id:
                img_activity["from"] = {"id": self.bot_id}
            activities.append(img_activity)

        return activities

    def _build_card_or_text_activities(
        self, message: OutboundMessage
    ) -> list[dict[str, Any]]:
        """Build the main body activities (Adaptive Card or chunked text)."""
        activity: dict[str, Any] = {"type": "message"}
        # Bot Framework requires Activity.From on replies — include the bot
        # identity so Web Chat and other channels don't reject with 400.
        if self.bot_id:
            activity["from"] = {"id": self.bot_id}

        # Support Adaptive Cards via metadata — agents can set
        # metadata["adaptive_card"] to a full Adaptive Card body (dict).
        adaptive_card = message.metadata.get("adaptive_card")
        if adaptive_card and isinstance(adaptive_card, dict):
            from app.integrations.providers.teams_bot import (
                _card_must_stay_card,
                _card_to_markdown,
                _ensure_full_width_card,
                _is_team_channel_conversation,
            )

            # In chat surfaces (personal / group chat) Teams caps Adaptive
            # Cards to the message-bubble width regardless of msteams.width
            # =Full — only real channels render edge-to-edge. So deliver a
            # display-only card as full-width Markdown text in chats; cards
            # Markdown cannot represent (inputs/buttons, charts, tables,
            # media) — and channel cards — stay cards.
            #
            # Trust ``conversation_type`` only when it is a KNOWN Bot Framework
            # surface ("channel" / "personal" / "groupChat"). For anything else
            # — missing, or an unknown/placeholder value like "?" (the default
            # _process_teams_message uses when metadata lacks it), or call sites
            # that construct ``TeamsChannel`` without it (sub_agent.py, the
            # monitoring helpers) — fall back to inferring channel-ness from the
            # conversation id suffix, so a real channel card isn't silently
            # downgraded to Markdown by a non-authoritative value.
            if self.conversation_type == "channel":
                is_channel = True
            elif self.conversation_type in ("personal", "groupChat"):
                is_channel = False
            else:
                is_channel = _is_team_channel_conversation(self.conversation_id)
            card_markdown = (
                ""
                if (is_channel or _card_must_stay_card(adaptive_card))
                else _card_to_markdown(adaptive_card)
            )
            if card_markdown:
                # Display-only card downgraded to Markdown text — falls
                # through to the chunked-text path below.
                return self._text_activities(card_markdown)

            activity["attachments"] = [
                {
                    "contentType": "application/vnd.microsoft.card.adaptive",
                    "content": _ensure_full_width_card(adaptive_card),
                }
            ]
            # Carry the fallback on ``summary`` (notification-preview
            # metadata), never ``text``: the Teams/Skype connector turns
            # an activity that has BOTH renderable ``text`` and
            # ``attachments`` into two Skype activities and rejects it
            # with HTTP 400 "Activity resulted into multiple skype
            # activities". ``summary`` keeps a useful toast preview while
            # keeping the activity a single message — mirroring
            # ``_build_card_activity`` in teams_bot.py.
            if message.text:
                activity["summary"] = message.text
            return [activity]

        if not message.text:
            return []
        return self._text_activities(message.text)

    def _text_activities(self, text: str) -> list[dict[str, Any]]:
        """Build one or more text activities from *text*, chunked to the cap.

        Teams rejects activities above ~28k characters with HTTP 400, so a
        long agent reply is split into multiple messages (code-fence-aware,
        prefers paragraph boundaries).  Bot Framework renders ``text`` as
        Markdown but not headings or tables, so each chunk is normalized via
        :func:`markdown_to_teams_text` (headings downgrade to bold, GFM
        tables become record lines).
        """
        activities: list[dict[str, Any]] = []
        for chunk in chunk_message(text, max_chars=CHANNEL_MAX_CHARS["teams"]):
            activity: dict[str, Any] = {
                "type": "message",
                "text": markdown_to_teams_text(chunk),
                "textFormat": "markdown",
            }
            if self.bot_id:
                activity["from"] = {"id": self.bot_id}
            activities.append(activity)
        return activities

    async def _post_activity(self, activity: dict[str, Any]) -> str | None:
        """POST a single activity to the conversation.

        Returns the new activity id (200/201), ``""`` when the platform
        accepted the activity asynchronously with an empty body (202 — NOT a
        failure; Bot Framework does this for Teams private/shared channels
        routed via Graph), or ``None`` on failure.
        """
        url = f"{self.service_url}/v3/conversations/{self.conversation_id}/activities"
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                url,
                headers={"Authorization": f"Bearer {self.access_token}"},
                json=activity,
            )
            if resp.status_code in (200, 201):
                try:
                    return resp.json().get("id") or ""
                except ValueError:
                    return ""
            if resp.status_code == 202:
                # Queued for asynchronous delivery — success without an id.
                return ""
            logger.error(
                "Teams send_message failed: %s %s", resp.status_code, resp.text[:500]
            )
            return None

    async def send_typing_indicator(self, channel_thread_id: str | None = None) -> None:
        url = f"{self.service_url}/v3/conversations/{self.conversation_id}/activities"
        activity: dict[str, Any] = {"type": "typing"}
        if self.bot_id:
            activity["from"] = {"id": self.bot_id}
        async with httpx.AsyncClient(timeout=15) as client:
            await client.post(
                url,
                headers={"Authorization": f"Bearer {self.access_token}"},
                json=activity,
            )


async def _get_teams_creds() -> tuple[str, str] | None:
    """Obtain Bot Framework access token + app ID, preferring company-managed bots.

    Returns ``(access_token, app_id)`` on success, or ``None`` on failure.
    Uses the tenant-specific Entra ID endpoint when a tenant_id is stored
    in extra_config; falls back to the legacy ``botframework.com`` endpoint.
    """
    bot = await get_channel_bot("teams_bot")
    if not bot:
        return None

    app_id = decrypt_value(bot.client_id_enc) if bot.client_id_enc else ""
    app_password = (await reveal_field(bot, "client_secret_enc")) or ""
    if not app_id or not app_password:
        return None

    # Extract tenant_id from extra_config (modern Entra ID setup)
    tenant_id = ""
    if bot.extra_config_enc:
        try:
            extra = await reveal_json_field(bot, "extra_config_enc") or {}
            tenant_id = extra.get("tenant_id", "")
        except Exception as exc:
            logger.debug(
                "Failed to parse Teams bot extra_config for tenant_id: %s", format_exception_message(exc)
            )
    # The process-wide app-token cache the Teams bot tools already use: every
    # viewer of a pasted card refreshes it, and a client-credentials
    # exchange per call was paid on every inbound message too.
    from app.integrations.providers.teams_bot import _acquire_token

    token = await _acquire_token(
        tenant_id, app_id, app_password, scope="https://api.botframework.com/.default"
    )
    return (token, app_id) if token else None


async def _resolve_teams_email(
    service_url: str,
    conversation_id: str,
    user_aad_id: str,
    token: str,
) -> str | None:
    """Resolve a Teams user's email via the Bot Framework Get Member API.

    The Bot Framework message payload doesn't always include the user's email.
    This fallback queries the conversation member endpoint to get it.
    """
    url = f"{service_url.rstrip('/')}/v3/conversations/{conversation_id}/members/{user_aad_id}"
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                url,
                headers={"Authorization": f"Bearer {token}"},
                timeout=10,
            )
            if resp.status_code == 200:
                data = resp.json()
                return data.get("email") or data.get("userPrincipalName")
    except Exception as e:
        logger.warning("Failed to resolve Teams email for %s: %s", user_aad_id, format_exception_message(e))
    return None


# Bot Framework attachment service domain. Microsoft uses
# ``smba.trafficmanager.net`` as the canonical host (with regional path
# prefixes like ``/amer/``, ``/emea/``, ``/fr-fr/``) and may serve regional
# subdomains under that label (e.g. ``amer.smba.trafficmanager.net``).
#
# Security: the allowlist is intentionally narrowed to
# ``smba.trafficmanager.net`` (exact) and any direct subdomain
# (``*.smba.trafficmanager.net``).  Azure Traffic Manager allows ANYONE
# to register arbitrary ``*.trafficmanager.net`` profiles, so a broader
# ``trafficmanager.net`` suffix check would let an attacker host
# ``attacker.trafficmanager.net/v3/attachments/...`` and receive the
# bot bearer token at download time.  Microsoft Bot Framework attachment
# URLs only ever resolve under the ``smba.trafficmanager.net`` label
# (see https://learn.microsoft.com/en-us/azure/bot-service/), so this
# narrower allowlist matches the legitimate surface exactly.
_BOTFRAMEWORK_ATTACHMENT_HOST_SUFFIX = "smba.trafficmanager.net"


def _is_botframework_attachment_url(url: str) -> bool:
    """Return True when the URL points at the Bot Framework attachment API.

    Only ``https://`` is accepted: the Authorization bearer token forwarded
    to this URL must never travel over plaintext HTTP, even on regional
    Bot Framework variants. The hostname check accepts
    ``smba.trafficmanager.net`` (the canonical Bot Framework attachment
    host) and any of its direct subdomains
    (``*.smba.trafficmanager.net``).

    Security: ``smba.``-scoped allowlist
    ------------------------------------
    The suffix is anchored at ``smba.trafficmanager.net`` rather than the
    bare ``trafficmanager.net`` parent because Azure Traffic Manager
    permits anyone to register an arbitrary ``*.trafficmanager.net``
    profile.  A broader suffix would let an attacker stand up
    ``attacker.trafficmanager.net/v3/attachments/x`` and trick this
    helper into attaching the bot bearer token to a request they
    receive, leaking the credential.  The ``smba.``-scoped check
    matches the legitimate Bot Framework attachment surface exactly.

    Security: userinfo-injection guard
    ----------------------------------
    The host check uses ``parsed.hostname`` rather than ``parsed.netloc``
    string-splitting because ``netloc`` includes any ``userinfo@`` prefix.
    A crafted URL of the form
    ``https://smba.trafficmanager.net:443@attacker.example.com/v3/attachments/x``
    has ``netloc = 'smba.trafficmanager.net:443@attacker.example.com'``
    but ``hostname = 'attacker.example.com'`` — the latter is what the
    HTTP client actually connects to.  Using ``netloc`` would
    misclassify this URL as Bot Framework and leak the bot bearer token
    to ``attacker.example.com``.  As an additional belt-and-braces
    guard we also reject any URL whose ``netloc`` contains an ``@`` so
    even a parser bug in ``urlparse`` can't slip a credential-leaking
    URL through.
    """
    if not url:
        return False
    try:
        parsed = urlparse(url)
    except Exception:
        return False
    # No userinfo allowed — Bot Framework URLs never carry one, and
    # accepting them would re-open the very leak this guard exists for.
    if "@" in (parsed.netloc or ""):
        return False
    host = (parsed.hostname or "").lower()
    if not host:
        return False
    return (
        parsed.scheme == "https"
        and (
            host == _BOTFRAMEWORK_ATTACHMENT_HOST_SUFFIX
            or host.endswith("." + _BOTFRAMEWORK_ATTACHMENT_HOST_SUFFIX)
        )
        and "/v3/attachments/" in parsed.path
    )


def _is_botframework_service_url(url: str) -> bool:
    """Return True when *url* is a legitimate Bot Framework service URL.

    The ``serviceUrl`` field arrives in the (attacker-controllable) webhook
    payload and is later used as the base URL for calls that carry a real
    bot bearer token (``send_message``, ``_resolve_teams_email``).  Without
    this allowlist a forged POST could exfiltrate the bot token to any host.
    Applies the same hardened rules as ``_is_botframework_attachment_url``:
    https only, no userinfo, host anchored at ``smba.trafficmanager.net``.
    """
    if not url:
        return False
    try:
        parsed = urlparse(url)
    except Exception:
        return False
    if "@" in (parsed.netloc or ""):
        return False
    host = (parsed.hostname or "").lower()
    if not host:
        return False
    return parsed.scheme == "https" and (
        host == _BOTFRAMEWORK_ATTACHMENT_HOST_SUFFIX
        or host.endswith("." + _BOTFRAMEWORK_ATTACHMENT_HOST_SUFFIX)
    )


# Bot Framework inbound-activity JWT validation (Bot Connector service).
_BOTFRAMEWORK_ISSUER = "https://api.botframework.com"
_BOTFRAMEWORK_JWKS_URL = "https://login.botframework.com/v1/.well-known/keys"
_botframework_jwks_client = None


def _verify_botframework_jwt(token: str, app_id: str) -> bool:
    """Verify the Bot Connector JWT attached to an inbound Teams activity.

    Microsoft signs every activity delivery with a JWT whose issuer is
    ``https://api.botframework.com`` and whose audience is the bot's app id.
    Runs synchronously (PyJWKClient uses urllib) — call via ``to_thread``.
    """
    import jwt as pyjwt

    global _botframework_jwks_client
    try:
        if _botframework_jwks_client is None:
            _botframework_jwks_client = pyjwt.PyJWKClient(
                _BOTFRAMEWORK_JWKS_URL, cache_keys=True
            )
        signing_key = _botframework_jwks_client.get_signing_key_from_jwt(token)
        pyjwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            audience=app_id,
            issuer=_BOTFRAMEWORK_ISSUER,
            # Bot Framework guidance allows 5 minutes of clock skew — without
            # leeway a slightly skewed host intermittently 401s legitimate
            # deliveries on exp/iat.
            leeway=300,
        )
        return True
    except Exception as exc:
        logger.warning("Teams Bot Framework JWT verification failed: %s", format_exception_message(exc))
        return False


async def _get_teams_app_id() -> str:
    """Return the configured Teams bot app id (no token minting)."""
    bot = await get_channel_bot("teams_bot")
    if not bot or not bot.client_id_enc:
        return ""
    try:
        return decrypt_value(bot.client_id_enc)
    except Exception:
        logger.warning("Failed to decrypt Teams bot client_id")
        # Credentials exist but are unreadable — fail closed, not open: the
        # sentinel never matches a JWT audience, so verification always
        # rejects instead of being skipped entirely.
        return DECRYPT_FAILED_CREDENTIAL


def _apply_teams_attachment_auth_headers(
    attachments: list[InboundFileAttachment],
    token: str,
) -> int:
    """Attach Bot Framework bearer auth to Teams attachment URLs when needed."""
    if not token:
        return 0

    applied = 0
    auth_value = f"Bearer {token}"
    for att in attachments:
        if not _is_botframework_attachment_url(att.url):
            continue
        if att.auth_headers.get("Authorization"):
            continue
        att.auth_headers = {
            **att.auth_headers,
            "Authorization": auth_value,
        }
        applied += 1
    return applied


def _graph_share_download_url(sharepoint_url: str) -> str:
    """Build the Graph ``/shares`` content-download URL for a SharePoint link.

    Encoding per the Graph shares API: unpadded base64url of the UTF-8 URL
    with a ``u!`` prefix (via the canonical ``_encode_share_id``).  The
    resulting endpoint (``/shares/{token}/driveItem/content``) downloads
    the file bytes with the bot's app-only Graph token (``Files.Read.All``
    / ``Files.ReadWrite.All``) and 302-redirects to a pre-authenticated
    location.
    """
    return f"{GRAPH_API}/shares/{_encode_share_id(sharepoint_url)}/driveItem/content"


def _guess_content_type(filename: str, file_type: str = "") -> str:
    """Best-effort MIME type from a filename or Teams ``fileType`` extension."""
    name = filename or (f"file.{file_type}" if file_type else "")
    guessed, _ = mimetypes.guess_type(name)
    return guessed or "application/octet-stream"


def _extract_teams_file_attachments(data: dict) -> list[InboundFileAttachment]:
    """Extract downloadable file attachments from a Bot Framework activity.

    Teams delivers files in two very different shapes:

    - **Personal (1:1) chats** — ``application/vnd.microsoft.teams.file.download.info``
      with a pre-authenticated ``content.downloadUrl`` (plain GET, valid for
      a few minutes; requires ``supportsFiles: true`` in the app manifest).
      The previous extractor skipped EVERY ``application/vnd.microsoft.*``
      content type, which silently dropped exactly these attachments.
    - **Group chats / channels** — a ``reference`` attachment whose
      ``contentUrl`` is the SharePoint file URL.  That URL needs Graph auth,
      so it is rewritten to the Graph ``/shares/{token}/driveItem/content``
      endpoint; the bot's app-only Graph token is attached later in
      processing (see ``_apply_graph_attachment_auth_headers``).

    Card attachments (adaptive/hero/…) and the ``text/html`` file-card
    markup that accompanies shared files are skipped.
    """
    attachments: list[InboundFileAttachment] = []
    for att in data.get("attachments", []):
        ct = att.get("contentType", "")
        content = att.get("content") if isinstance(att.get("content"), dict) else {}
        name = att.get("name") or "file"

        if ct == "application/vnd.microsoft.teams.file.download.info":
            download_url = content.get("downloadUrl", "")
            if download_url:
                attachments.append(
                    InboundFileAttachment(
                        url=download_url,
                        filename=name,
                        content_type=_guess_content_type(
                            name, content.get("fileType", "")
                        ),
                        size=0,
                        platform_file_id=content.get("uniqueId", ""),
                        # Pre-authenticated URL — no auth headers needed.
                    )
                )
            continue

        if ct == "reference":
            content_url = att.get("contentUrl", "")
            if content_url and content_url.lower().startswith("https://"):
                attachments.append(
                    InboundFileAttachment(
                        url=_graph_share_download_url(content_url),
                        filename=name,
                        content_type=_guess_content_type(name),
                        size=0,
                        # Graph token attached later in processing.
                    )
                )
            continue

        # Cards and the file-card HTML preview are not files.
        if ct.startswith("application/vnd.microsoft.") or ct == "text/html":
            continue

        # Anything else with a direct contentUrl (e.g. inline images pasted
        # into the chat, served from the Bot Framework attachment API —
        # the bearer token for those is attached later).
        content_url = att.get("contentUrl", "")
        if content_url:
            attachments.append(
                InboundFileAttachment(
                    url=content_url,
                    filename=name,
                    content_type=ct or "application/octet-stream",
                    size=0,
                    platform_file_id=(
                        content.get("uniqueId", "") if content else ""
                    ),
                )
            )
    return attachments


def _extract_graph_message_attachments(msg: dict) -> list[InboundFileAttachment]:
    """Extract ``reference`` file attachments from a Graph ``chatMessage``.

    Files shared in group chats and channels surface in the Graph message's
    ``attachments`` array as ``reference`` entries whose ``contentUrl`` is
    the SharePoint file URL.  The URL is rewritten to the Graph ``/shares``
    download endpoint (app-only token attached later, same contract as
    :func:`_extract_teams_file_attachments`).

    This is the fallback source of truth for conversations where the Bot
    Framework activity carries only the ``text/html`` file-card stub and
    omits the ``reference`` attachment entirely — observed in group chats
    and (private) channels.
    """
    attachments: list[InboundFileAttachment] = []
    for att in msg.get("attachments") or []:
        if not isinstance(att, dict):
            continue
        kind = str(att.get("contentType") or "")
        if kind.lower().startswith("audio/"):
            # A voice clip recorded in the chat or channel: served by Graph
            # itself, the app-only token attached later; the router
            # transcribes it (``router._hear_voice_notes``).
            audio_url = str(att.get("contentUrl") or "")
            if audio_url.startswith(f"{GRAPH_API}/"):
                attachments.append(
                    InboundFileAttachment(
                        url=audio_url,
                        filename=str(att.get("name") or "").strip() or "teams-voice-note",
                        content_type=kind.split(";", 1)[0].strip().lower(),
                        size=0,
                        platform_file_id=att.get("id", ""),
                    )
                )
            continue
        if kind != "reference":
            continue
        content_url = att.get("contentUrl") or ""
        if not content_url.lower().startswith("https://"):
            continue
        name = att.get("name") or "file"
        attachments.append(
            InboundFileAttachment(
                url=_graph_share_download_url(content_url),
                filename=name,
                content_type=_guess_content_type(name),
                size=0,
                platform_file_id=att.get("id", ""),
                # Graph token attached later in processing.
            )
        )
    return attachments


def _apply_graph_attachment_auth_headers(
    attachments: list[InboundFileAttachment],
    graph_token: str,
) -> int:
    """Attach the app-only Graph bearer token to ``/shares`` download URLs.

    Only URLs this adapter constructed itself (``https://graph.microsoft.com/…``,
    built by :func:`_graph_share_download_url`) receive the token — never
    arbitrary attacker-suppliable hosts.
    """
    if not graph_token:
        return 0

    applied = 0
    auth_value = f"Bearer {graph_token}"
    for att in attachments:
        if not _is_graph_download(att):
            continue
        if att.auth_headers.get("Authorization"):
            continue
        att.auth_headers = {**att.auth_headers, "Authorization": auth_value}
        applied += 1
    return applied


def _is_graph_download(att: InboundFileAttachment) -> bool:
    """Whether *att* is downloaded from Graph with the app-only token.

    A ``/shares`` URL this adapter built, or a voice clip Graph serves itself
    (an ``audio/*`` attachment on a ``graph.microsoft.com`` address).
    """
    if att.url.startswith(f"{GRAPH_API}/shares/"):
        return True
    return att.url.startswith(f"{GRAPH_API}/") and str(att.content_type or "").lower().startswith("audio/")


_TEAMS_JS_SDK_ORIGIN = "https://res.cdn.office.net"
_TEAMS_JS_SDK_URL = f"{_TEAMS_JS_SDK_ORIGIN}/teams-js/2.31.1/js/MicrosoftTeams.min.js"
_TEAMS_TAB_SCRIPT = """
  microsoftTeams.app.initialize().then(function(){
    microsoftTeams.appInitialization.notifySuccess();
  });
"""

# Framed by Teams / Microsoft 365 hosts, which CI cannot exercise: the tab
# stub's policy is only ever reported, never enforced.
register_page_policy(
    "/api/channels/teams/tab",
    PagePolicy(
        sources={"script-src": (_TEAMS_JS_SDK_ORIGIN,)},
        script_hashes=lambda: (script_hash_source(_TEAMS_TAB_SCRIPT),),
        frame_ancestors="any",
        always_report_only=True,
    ),
)


@router.get("/teams/tab")
async def teams_tab(db: AsyncSession = Depends(get_db)):
    """Minimal personal tab page for the Teams app.

    Teams requires a real contentUrl for static tabs — the reserved
    ``entityId: "conversations"`` pattern alone is not enough.  This
    endpoint returns a tiny HTML page that loads the Teams JS SDK,
    calls ``app.initialize()`` and ``appInitialization.notifySuccess()``
    so the tab renders properly without "There was a problem reaching
    this app" errors.

    The page title / body brand name is sourced from the admin-editable
    central branding resolver, so customised deployments see their own brand
    instead of the deployment-mode default.
    """
    from fastapi.responses import HTMLResponse

    from app.services.branding import get_cached_branding

    app_name = html_module.escape(get_cached_branding().name)
    html = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>__APP_NAME__</title>
<script src="__TEAMS_JS_SDK__"></script>
<style>
  body{margin:0;background:#0b0f1a;color:#fff;font-family:'IBM Plex Sans',sans-serif;
       display:flex;align-items:center;justify-content:center;height:100vh}
  .c{text-align:center;opacity:.7}
</style>
</head><body>
<div class="c"><p>Use the chat below to talk to __APP_NAME__.</p></div>
<script>__TEAMS_TAB_SCRIPT__</script>
</body></html>"""
    html = (
        html.replace("__TEAMS_JS_SDK__", _TEAMS_JS_SDK_URL)
        .replace("__TEAMS_TAB_SCRIPT__", _TEAMS_TAB_SCRIPT)
        .replace("__APP_NAME__", app_name)
    )
    return HTMLResponse(content=html)


@router.post("/teams/messages")
async def teams_messages(request: Request):
    # Authenticate the request before trusting any payload content. The Bot
    # Connector attaches a JWT (issuer api.botframework.com, audience = bot
    # app id) to every activity delivery. Without this check a forged POST
    # can inject messages AND exfiltrate the bot bearer token via a crafted
    # serviceUrl (see _is_botframework_service_url).
    app_id = await _get_teams_app_id()
    if app_id:
        auth_header = request.headers.get("Authorization", "")
        token = auth_header.removeprefix("Bearer ").strip()
        if not token or not await asyncio.to_thread(
            _verify_botframework_jwt, token, app_id
        ):
            logger.warning("Teams webhook rejected: invalid Bot Framework JWT")
            return Response(status_code=401)
    else:
        warn_unauthenticated_once(
            "teams",
            "Teams webhook is UNAUTHENTICATED: no teams_bot channel bot with "
            "a client_id is configured — inbound activities cannot be verified",
        )

    try:
        data = await request.json()
    except Exception:
        logger.warning("Teams webhook received invalid JSON payload")
        return Response(status_code=200)

    activity_type = data.get("type", "unknown")
    conversation = data.get("conversation", {})
    conversation_type = conversation.get("conversationType", "personal")
    from_user = data.get("from", {})
    from_name = (
        from_user.get("name")
        or from_user.get("aadObjectId")
        or from_user.get("id")
        or "unknown"
    )

    logger.info(
        "Teams webhook received: type=%s, conversation_type=%s, from=%s",
        activity_type,
        conversation_type,
        from_user.get("aadObjectId") or from_user.get("id") or "unknown",
    )

    # The bot is in this conversation (a 1:1, a group chat or a channel): a
    # decision card sent there later is posted by the bot and answered in place
    # (#842, S9; human-gates.md I.4 path (a)).
    _bot_service_url = str(data.get("serviceUrl") or "")
    if conversation.get("id") and _is_botframework_service_url(_bot_service_url):
        from app.services.decision_card_delivery import remember_bot_conversation

        await remember_bot_conversation(str(conversation["id"]), _bot_service_url, conversation_type)

    if activity_type == "conversationUpdate":
        recipient_id = data.get("recipient", {}).get("id", "")
        members_added = data.get("membersAdded", [])
        bot_was_added = (
            any(m.get("id") == recipient_id for m in members_added)
            if recipient_id
            else bool(members_added)
        )
        if bot_was_added and conversation_type == "groupChat":
            conv_id = conversation.get("id", "")
            if conv_id:
                logger.info(
                    "Teams: bot added to groupChat — scheduling context backfill "
                    "(conversation_id=%s)",
                    conv_id[:60],
                )
                spawn_channel_task(
                    _backfill_chat_context(conv_id), name="teams-backfill"
                )
        else:
            logger.info(
                "Teams webhook: conversationUpdate (bot_added=%s, type=%s) — no backfill needed",
                bot_was_added,
                conversation_type,
            )
        return Response(status_code=200)

    if activity_type == "invoke":
        # An invoke shows a run to someone or decides it: never from an
        # activity the Bot Framework JWT did not vouch for.
        if not app_id:
            return Response(status_code=401)
        return await _handle_invoke(data)

    if activity_type != "message":
        logger.info(
            "Teams webhook: ignoring non-message activity type=%s",
            activity_type,
        )
        return Response(status_code=200)

    text = (data.get("text") or "").strip()
    service_url = data.get("serviceUrl", "")
    conversation_id = conversation.get("id") or ""
    channel_data = data.get("channelData", {})
    tenant_id = channel_data.get("tenant", {}).get("id")
    team_name = channel_data.get("team", {}).get("name", "")
    channel_name = channel_data.get("channel", {}).get("name", "")

    # Defence in depth: never use a serviceUrl outside the Bot Framework
    # domain — replies and member lookups attach a real bot bearer token
    # to this base URL.
    if service_url and not _is_botframework_service_url(service_url):
        logger.warning(
            "Teams webhook rejected: serviceUrl %r is not a Bot Framework host",
            service_url[:200],
        )
        return Response(status_code=200)

    logger.info(
        "Teams message: text_length=%d, conversation_id=%s, conversation_type=%s, "
        "team=%s, channel=%s, service_url=%s, tenant_id=%s",
        len(text or ""),
        conversation_id[:60],
        conversation_type,
        team_name or "(n/a)",
        channel_name or "(n/a)",
        service_url,
        tenant_id or "(n/a)",
    )

    # Extract Teams file attachments from the Bot Framework payload early —
    # a file shared without any accompanying text is still a message the
    # agent must see (text falls back to a placeholder below).
    teams_file_attachments = _extract_teams_file_attachments(data)
    if teams_file_attachments:
        logger.info(
            "Teams message: %d file attachment(s) detected",
            len(teams_file_attachments),
        )

    # Group-chat / channel activities often carry only a ``text/html``
    # file-card stub for shared files — the real attachment is only on the
    # Graph copy of the message.  Such activities must not be dropped here;
    # the Graph fallback in ``_process_teams_message`` resolves them.
    may_have_graph_files = conversation_type in ("groupChat", "channel") and any(
        (a.get("contentType") or "") in ("text/html", "reference")
        for a in data.get("attachments", [])
    )

    if not text and not teams_file_attachments and not may_have_graph_files:
        logger.info("Teams message: empty text — skipping")
        return Response(status_code=200)

    # Detect whether the bot was explicitly @mentioned.
    # Compare the mentioned entity's ID against the bot's own recipient ID
    # to avoid false positives when other users/teams are @mentioned.
    recipient_id = data.get("recipient", {}).get("id", "")
    bot_was_mentioned = False
    for entity in data.get("entities", []):
        if entity.get("type") != "mention":
            continue
        mentioned_id = entity.get("mentioned", {}).get("id", "")
        if recipient_id and mentioned_id == recipient_id:
            bot_was_mentioned = True
            break
        if not recipient_id:
            # Fallback: if we can't determine the bot's ID, treat any
            # mention as a potential bot mention (conservative approach).
            bot_was_mentioned = True
            break

    # Strip only the bot's own mention from the text (keep @user mentions
    # intact so the agent sees them as context).
    original_text = text
    for entity in data.get("entities", []):
        if entity.get("type") != "mention":
            continue
        mentioned_id = entity.get("mentioned", {}).get("id", "")
        # Only strip if this mention matches the bot's ID, or if we can't
        # determine the bot's ID (conservative fallback).
        if recipient_id and mentioned_id != recipient_id:
            continue
        mention_text = entity.get("text", "")
        text = text.replace(mention_text, "").strip()
        logger.debug("Teams message: stripped bot mention %s", log_preview(mention_text))

    if original_text != text:
        logger.debug(
            "Teams message: after mention stripping: %s (was %s)",
            log_preview(text),
            log_preview(original_text),
        )

    # After stripping mentions, the text may be empty (bare @mention with no
    # question).  A message that carries file attachment(s) — or may carry
    # them on its Graph copy — is still actionable: substitute a placeholder
    # (the Graph fallback replaces empty text itself) so the agent reacts.
    if not text:
        if teams_file_attachments:
            text = _FILE_ONLY_PLACEHOLDER
        elif not may_have_graph_files:
            logger.info(
                "Teams message: text empty after stripping mentions — skipping"
            )
            return Response(status_code=200)

    # Resolve user email — payload may or may not include it
    user_email = from_user.get("email")
    user_aad_id = from_user.get("aadObjectId") or from_user.get("id")

    logger.info(
        "Teams message: processing — has_email=%s, aad_id=%s, text_length=%d",
        bool(user_email),
        user_aad_id,
        len(text or ""),
    )

    inbound = InboundMessage(
        channel=ChannelType.TEAMS,
        text=text,
        user_email=user_email,
        channel_user_id=from_user.get("id"),
        channel_thread_id=conversation_id,
        channel_message_id=data.get("id"),
        file_attachments=teams_file_attachments,
        raw=data,
        metadata={
            "service_url": service_url,
            "tenant_id": tenant_id,
            "user_aad_id": user_aad_id,
            "conversation_type": conversation_type,
            "bot_was_mentioned": bot_was_mentioned,
            "team_name": team_name,
            # Prefer the Entra ID group GUID when Bot Framework includes it —
            # channel whitelists built from the Graph picker key teams by
            # that GUID, while ``team.id`` is the thread-style conversation
            # ID.  (Router-side matching also normalizes to the channel-id
            # component as a fallback for payloads without aadGroupId.)
            "team_id": (
                channel_data.get("team", {}).get("aadGroupId")
                or channel_data.get("team", {}).get("id", "")
            ),
            "channel_name": channel_name,
            "channel_id": channel_data.get("channel", {}).get("id", ""),
            # Teams display name from the Bot Framework payload — used as
            # platform-reported identity for anonymous users (matches
            # WhatsApp contact_name, Telegram first_name/last_name, etc.)
            "contact_name": from_name,
            # Thread root ID: in Teams channel threads, replyToId always
            # points to the root message.  Used by the router for thread
            # continuation tracking (only_mention smart follow-up).
            "reply_to_id": data.get("replyToId"),
        },
    )

    spawn_channel_task(
        _process_teams_message(inbound, service_url, conversation_id),
        name="teams-message",
    )

    return Response(status_code=200)


# ---------------------------------------------------------------------------
# Pasted XTM One links: link unfurling and the card's actions
# ---------------------------------------------------------------------------

#: Card renders and actions one Teams user may ask for per minute. Every viewer
#: of a pasted link refreshes it, so the bucket is the person, not the tenant.
_CARD_INVOKES_PER_MINUTE = 120
#: How long the email of a Teams user, read from the Bot Framework member API,
#: is reused for the cards they view.
_INVOKE_EMAIL_TTL_SECONDS = 600


def _invoke_error(status_code: int, code: str) -> JSONResponse:
    """An ``adaptiveCard/action`` refusal: generic, the reason stays in the logs."""
    return JSONResponse(
        {
            "statusCode": status_code,
            "type": "application/vnd.microsoft.error",
            "value": {"code": code, "message": "The card could not be updated."},
        }
    )


async def _invoke_user_email(data: dict) -> str | None:
    """The email of the Teams user behind *data*, through the Bot Framework member API.

    Cached per Entra object id: every viewer of a card refreshes it, and the
    lookup needs a bot token and a round trip.
    """
    from app.core.redis import redis_client

    from_user = data.get("from") or {}
    aad_id = from_user.get("aadObjectId")
    service_url = data.get("serviceUrl") or ""
    conversation_id = (data.get("conversation") or {}).get("id") or ""
    if not aad_id or not conversation_id or not _is_botframework_service_url(service_url):
        return None
    cache_key = f"teams:invoke_email:{aad_id}"
    try:
        redis = await redis_client()
        cached = await redis.get(cache_key)
        if cached:
            return cached.decode() if isinstance(cached, bytes) else str(cached)
    except Exception as exc:
        redis = None
        logger.debug("Teams card: email cache unreadable: %s", format_exception_message(exc))
    creds = await _get_teams_creds()
    if not creds:
        return None
    email = await _resolve_teams_email(service_url, conversation_id, aad_id, creds[0])
    if email and redis is not None:
        try:
            await redis.set(cache_key, email, ex=_INVOKE_EMAIL_TTL_SECONDS)
        except Exception as exc:
            logger.debug("Teams card: email cache not written: %s", format_exception_message(exc))
    return email


async def _configured_teams_tenant() -> str:
    """The Entra tenant the Teams bot is registered in, ``""`` when none is configured."""
    bot = await get_channel_bot("teams_bot")
    if not bot or not bot.extra_config_enc:
        return ""
    try:
        return str((await reveal_json_field(bot, "extra_config_enc") or {}).get("tenant_id") or "")
    except Exception as exc:
        logger.debug("Failed to parse Teams bot extra_config for tenant_id: %s", format_exception_message(exc))
        return ""


async def _invoke_user(db: AsyncSession, data: dict):
    """The platform user behind an invoke: by email, then by a linked Teams identity."""
    from_user = data.get("from") or {}
    email = from_user.get("email") or await _invoke_user_email(data)
    if email:
        user = await resolve_user_by_email(db, email)
        if user is not None:
            return user
    channel_user_id = from_user.get("id")
    if channel_user_id:
        return await resolve_user_by_channel_identity(db, ChannelType.TEAMS.value, channel_user_id)
    return None


async def _handle_invoke(data: dict) -> Response:
    """Answer a Teams invoke: a pasted link to unfurl, or a card to render or act on.

    ``composeExtension/queryLink`` answers with the neutral card and reads
    nothing: the posted message must not say whether the run exists, nor show
    anything about it to people Teams never refreshes the card for. The
    details come with ``adaptiveCard/action`` - the refresh Teams sends for
    each viewer, and the buttons - rendered for the person who sent it.
    """
    from app.core.database import async_session
    from app.core.rate_limit import enforce_rate_limit
    from app.services import teams_link_cards as cards

    name = data.get("name")
    value = data.get("value") if isinstance(data.get("value"), dict) else {}

    if name == "composeExtension/queryLink":
        target = cards.parse_link(value.get("url"))
        if target is None:
            # Not one of our pages: no card, and Teams keeps its own preview.
            return Response(status_code=200)
        return JSONResponse(cards.query_link_response(target, notice_language()))

    if name != "adaptiveCard/action":
        logger.info("Teams webhook: ignoring invoke %r", str(name)[:80])
        return Response(status_code=200)

    action = value.get("action") if isinstance(value.get("action"), dict) else {}
    card_data = action.get("data") if isinstance(action.get("data"), dict) else {}
    # A card shows and answers runs as the person its activity names, matched
    # by email: someone from another organisation with the same address is not
    # them. So the activity must come from the bot's own tenant, and a bot with
    # no tenant configured answers no card action (it keeps the neutral card).
    expected_tenant = await _configured_teams_tenant()
    activity_tenant = ((data.get("channelData") or {}).get("tenant") or {}).get("id") or ""
    if not expected_tenant or activity_tenant.lower() != expected_tenant.lower():
        logger.warning("Teams card action refused: no tenant configured for the bot, or the activity's differs")
        return _invoke_error(403, "Forbidden")
    from_user = data.get("from") or {}
    bucket = from_user.get("aadObjectId") or from_user.get("id")
    if not bucket:
        return _invoke_error(400, "BadRequest")
    try:
        await enforce_rate_limit(
            None,
            "teams_card",
            limit=_CARD_INVOKES_PER_MINUTE,
            window_seconds=60,
            bucket=str(bucket)[:128],
            quiet=True,
        )
    except HTTPException as exc:
        return _invoke_error(exc.status_code, "TooManyRequests" if exc.status_code == 429 else "ServiceUnavailable")

    async with async_session() as db:
        user = await _invoke_user(db, data)
        language = notice_language(user)
        try:
            card = await cards.act(db, str(action.get("verb") or ""), card_data, user, language)
        except ValueError as exc:
            logger.info("Teams card action refused: %s", exc)
            return _invoke_error(400, "BadRequest")
        except Exception:
            logger.exception("Teams card action failed")
            return _invoke_error(500, "InternalServerError")
    return JSONResponse({"statusCode": 200, "type": cards.ADAPTIVE_CARD, "value": card})


async def _process_teams_message(
    inbound: InboundMessage, service_url: str, conversation_id: str
) -> None:
    conv_type = inbound.metadata.get("conversation_type", "?")
    logger.info(
        "Teams processing: started — conversation_type=%s, text_length=%d",
        conv_type,
        len(inbound.text or ""),
    )
    try:
        creds = await _get_teams_creds()
        if not creds:
            logger.error(
                "Teams processing: FAILED — could not obtain Bot Framework token. "
                "Check teams_bot channel bot config (client_id, client_secret, tenant_id)."
            )
            return

        token, bot_app_id = creds
        logger.debug(
            "Teams processing: Bot Framework token acquired (bot_id=%s)", bot_app_id
        )

        auth_applied = _apply_teams_attachment_auth_headers(
            inbound.file_attachments, token
        )
        if auth_applied:
            logger.info(
                "Teams processing: attached Bot Framework auth headers to %d file attachment(s)",
                auth_applied,
            )

        # Group-chat / channel messages need the Graph copy of the message
        # for two independent fallbacks:
        #
        #   1. File attachments: the Bot Framework activity usually carries
        #      only the ``text/html`` file-card stub — the real ``reference``
        #      attachments live on the Graph copy (requires Chat.Read.All /
        #      ChannelMessage.Read.All).
        #   2. Bot @mention detection: with RSC receive-all permissions
        #      (ChatMessage.Read.Chat / ChannelMessage.Read.Group — both in
        #      the shipped Teams manifest), Teams can deliver an @mention
        #      activity WITHOUT the mention entity: ``entities`` carries only
        #      ``clientInfo`` and the bot name is stripped from the leading
        #      text (MicrosoftDocs/msteams-docs#14422).  The webhook's
        #      entities-based detection then never fires and the
        #      ``only_mention`` gate silently drops genuine @mentions.  The
        #      Graph copy retains the resolved ``mentions``, so it is the
        #      reliable source for the flag.
        _needs_attachment_fallback = not any(
            att.url.startswith(f"{GRAPH_API}/shares/")
            for att in inbound.file_attachments
        )
        _needs_mention_fallback = not inbound.metadata.get("bot_was_mentioned")
        # Graph credentials are acquired at most ONCE per inbound message and
        # shared between the message fallback below and the /shares/ auth
        # header block further down.
        graph_creds: tuple[str, str, str] | None = None
        graph_creds_attempted = False
        if conv_type in ("groupChat", "channel") and (
            _needs_attachment_fallback or _needs_mention_fallback
        ):
            graph_creds = await _get_teams_graph_creds()
            graph_creds_attempted = True
            if not graph_creds and _needs_mention_fallback:
                logger.debug(
                    "Teams processing: no Graph credentials (tenant_id "
                    "missing?) — cannot recover a possibly RSC-stripped "
                    "bot @mention for this %s message",
                    conv_type,
                )
            if graph_creds:
                msg = await _fetch_graph_inbound_message(inbound, graph_creds[0])
                if msg:
                    if _needs_attachment_fallback:
                        graph_atts = _extract_graph_message_attachments(msg)
                        if graph_atts:
                            inbound.file_attachments.extend(graph_atts)
                            logger.info(
                                "Teams processing: found %d file attachment(s) "
                                "via Graph message fallback",
                                len(graph_atts),
                            )
                            if inbound.text == "":
                                inbound.text = _FILE_ONLY_PLACEHOLDER
                    if _needs_mention_fallback and _graph_message_mentions_bot(
                        msg, graph_creds[1]
                    ):
                        inbound.metadata["bot_was_mentioned"] = True
                        logger.info(
                            "Teams processing: bot @mention recovered from the "
                            "Graph message copy (RSC delivery stripped the "
                            "mention entity) — conversation_type=%s",
                            conv_type,
                        )

        # A stub-only activity whose Graph copy yielded nothing (e.g. an
        # empty rich-text message) has no content for the agent — drop it.
        if not inbound.text and not inbound.file_attachments:
            logger.info(
                "Teams processing: empty message after Graph fallback — skipping"
            )
            return

        # Group-chat / channel file attachments are downloaded through the
        # Graph /shares endpoint and need the app-only Graph token.  Only
        # mint it when such attachments are actually present — and reuse the
        # credentials already acquired by the fallback block above instead of
        # hitting the channel-bot config (DB + decrypt) a second time.
        if any(_is_graph_download(att) for att in inbound.file_attachments):
            if not graph_creds_attempted:
                graph_creds = await _get_teams_graph_creds()
            if graph_creds:
                graph_applied = _apply_graph_attachment_auth_headers(
                    inbound.file_attachments, graph_creds[0]
                )
                logger.info(
                    "Teams processing: attached Graph auth headers to %d "
                    "shared-file attachment(s)",
                    graph_applied,
                )
            else:
                logger.warning(
                    "Teams processing: message carries SharePoint file "
                    "attachment(s) but no Graph credentials are available "
                    "(tenant_id missing?) — files will be skipped"
                )

        # If email wasn't in the payload, resolve it via Bot Framework API
        if not inbound.user_email:
            user_aad_id = inbound.metadata.get("user_aad_id")
            if user_aad_id and token:
                logger.info(
                    "Teams processing: no email in payload, resolving via Bot Framework API for aad_id=%s",
                    user_aad_id,
                )
                email = await _resolve_teams_email(
                    service_url,
                    conversation_id,
                    user_aad_id,
                    token,
                )
                if email:
                    inbound.user_email = email
                    logger.info("Teams processing: resolved the email for aad_id=%s", user_aad_id)
                else:
                    logger.warning(
                        "Teams processing: could not resolve email for aad_id=%s",
                        user_aad_id,
                    )

        # Cache the service URL per tenant for proactive messaging via Bot
        # Framework (used by the teams_bot tools in teams_bot.py).
        tenant_id = inbound.metadata.get("tenant_id")
        if tenant_id:
            await _store_service_url(tenant_id, service_url)

        # Store conversation reference for proactive messaging (notifications)
        if inbound.user_email:
            await _store_conversation_reference(
                inbound.user_email,
                service_url,
                conversation_id,
            )
            # The user's personal chat with the bot, where a web conversation
            # can be continued.
            if conv_type == "personal":
                await _store_conversation_reference(
                    inbound.user_email,
                    service_url,
                    conversation_id,
                    personal=True,
                )

        # Only show the typing indicator when the message is likely to
        # produce a response.  In team channels where the bot was NOT
        # @mentioned, the message may be dropped by the only_mention gate
        # — showing "typing…" and then going silent is confusing.
        # DMs (personal), explicit @mentions, and threaded replies always
        # get the typing indicator.
        is_personal = conv_type == "personal"
        bot_mentioned = bool(inbound.metadata.get("bot_was_mentioned"))
        has_reply_to = bool(inbound.metadata.get("reply_to_id"))
        show_typing = is_personal or bot_mentioned or has_reply_to

        logger.info(
            "Teams processing: routing to handle_message — has_email=%s, "
            "conversation_type=%s, bot_mentioned=%s, show_typing=%s",
            bool(inbound.user_email),
            conv_type,
            bot_mentioned,
            show_typing,
        )

        channel = TeamsChannel(
            service_url,
            conversation_id,
            token,
            bot_id=bot_app_id,
            conversation_type=conv_type,
        )
        if show_typing:
            async with typing_keepalive(channel):
                outbound = await handle_message(inbound)
        else:
            outbound = await handle_message(inbound)

        if outbound.suppress:
            logger.info(
                "Teams processing: suppressing reply — message was silently "
                "dropped (only_mention gate, [NO_RESPONSE], publish disabled, "
                "or agent rejected channel). conversation_type=%s, "
                "bot_mentioned=%s, conversation_id=%s",
                conv_type,
                bot_mentioned,
                conversation_id[:60],
            )
            return

        logger.info(
            "Teams processing: sending reply — length=%d chars",
            len(outbound.text) if outbound.text else 0,
        )
        msg_id = await channel.send_message(outbound)
        # ``""`` means the platform accepted the activity asynchronously
        # (HTTP 202 — the norm for private/shared channels routed via
        # Graph); only ``None`` is a failure.  Gating on truthiness here
        # previously skipped private-channel monitoring setup for exactly
        # the conversations that need it.
        if msg_id is not None:
            logger.info(
                "Teams processing: reply sent — activity_id=%s",
                msg_id or "(accepted asynchronously)",
            )

            # If this was a channel message where the bot was @mentioned,
            # check asynchronously whether it's a private/shared channel
            # and set up Graph subscription monitoring for the thread.
            if conv_type == "channel" and bot_mentioned:
                logger.info(
                    "Teams processing: scheduling private-channel monitoring "
                    "check (conversation_id=%s)",
                    conversation_id[:80],
                )
                spawn_channel_task(
                    _setup_private_channel_monitoring(inbound),
                    name="teams-priv-channel-monitor",
                )
            elif conv_type == "channel":
                logger.info(
                    "Teams processing: NOT scheduling private-channel "
                    "monitoring — bot was not @mentioned "
                    "(conversation_id=%s)",
                    conversation_id[:80],
                )
        else:
            logger.error("Teams processing: failed to send reply")
    except asyncio.CancelledError as cut:
        # Cut by a platform update (a rolling deploy, a restart): said in the
        # chat, never silence, unless the router handed the turn on and said so.
        if is_graceful_shutdown_cancellation(cut) and (
            conv_type == "personal" or inbound.metadata.get("bot_was_mentioned")
        ):

            async def _post(text: str) -> None:
                creds = await _get_teams_creds()
                if creds:
                    await TeamsChannel(
                        service_url,
                        conversation_id,
                        creds[0],
                        bot_id=creds[1],
                        conversation_type=conv_type,
                    ).send_message(OutboundMessage(text=text))

            await tell_cut_turn(inbound, _post)
        raise
    except Exception as e:
        logger.error("Teams processing: EXCEPTION — %s", format_exception_message(e), exc_info=True)
        # Match the Telegram / Discord / Google Chat adapters: in DMs and
        # explicit @mentions, tell the user something went wrong instead of
        # showing a typing indicator followed by permanent silence.
        if conv_type == "personal" or inbound.metadata.get("bot_was_mentioned"):
            try:
                creds = await _get_teams_creds()
                if creds:
                    token, bot_app_id = creds
                    channel = TeamsChannel(
                        service_url,
                        conversation_id,
                        token,
                        bot_id=bot_app_id,
                        conversation_type=conv_type,
                    )
                    await channel.send_message(
                        OutboundMessage(
                            text=notice("adapter_error", notice_language())
                        )
                    )
            except Exception:
                logger.debug(
                    "Teams processing: failed to send error notice", exc_info=True
                )


async def run_queued_message(inbound: InboundMessage) -> None:
    """Answer a personal-chat message the router queued behind a running turn.

    Processed like a message just received; its files were stored when it was
    queued (a personal chat's download URL lasts minutes), and the router
    loads them. No Bot Framework service URL, or no Teams bot connected any
    more, is a ``QueuedMessageUndeliverableError``: the drain starts it again,
    then drops it.
    """
    from app.services.channel_continuation import QueuedMessageUndeliverableError

    service_url = str((inbound.metadata or {}).get("service_url") or "")
    if not _is_botframework_service_url(service_url) or not inbound.channel_thread_id:
        raise QueuedMessageUndeliverableError("no Bot Framework service URL")
    if not await _get_teams_creds():
        raise QueuedMessageUndeliverableError("no Microsoft Teams bot is connected")
    await _process_teams_message(inbound, service_url, inbound.channel_thread_id)


async def post_queued_notice(inbound: InboundMessage, text: str) -> bool:
    """Post *text* in the personal chat of a queued message; ``False`` when it cannot be reached."""
    service_url = str((inbound.metadata or {}).get("service_url") or "")
    if not _is_botframework_service_url(service_url) or not inbound.channel_thread_id:
        return False
    creds = await _get_teams_creds()
    if not creds:
        return False
    token, app_id = creds
    channel = TeamsChannel(
        service_url,
        inbound.channel_thread_id,
        token,
        bot_id=app_id,
        conversation_type="personal",
    )
    return await channel.send_message(OutboundMessage(text=text)) is not None


def _conversation_reference_key(user_email: str, *, personal: bool = False) -> str:
    kind = "personal_ref" if personal else "conv_ref"
    return f"teams:{kind}:{user_email.lower()}"


async def _store_conversation_reference(
    user_email: str,
    service_url: str,
    conversation_id: str,
    *,
    personal: bool = False,
) -> None:
    """Persist the Teams conversation reference in Redis for proactive messaging.

    Called on every inbound message so the reference stays current: the last
    conversation the user wrote from, and with *personal* their personal chat
    with the bot. The key expires after 30 days of inactivity.
    """
    from app.core.redis import redis_client

    try:
        redis = await redis_client()
        key = _conversation_reference_key(user_email, personal=personal)
        payload = json.dumps(
            {
                "service_url": service_url,
                "conversation_id": conversation_id,
            }
        )
        await redis.set(key, payload, ex=60 * 60 * 24 * 30)  # 30-day TTL
    except Exception as exc:
        logger.warning("Failed to store Teams conversation reference: %s", format_exception_message(exc))


async def _store_service_url(tenant_id: str, service_url: str) -> None:
    """Cache the Bot Framework service URL for a tenant in Redis.

    The service URL is region-specific (e.g. ``smba.trafficmanager.net/amer/``
    vs ``/emea/``).  The Teams bot tools (``teams_bot.py``) look this up when
    sending proactive channel messages via Bot Framework.
    """
    from app.core.redis import redis_client

    try:
        redis = await redis_client()
        key = f"teams:service_url:{tenant_id}"
        await redis.set(key, service_url, ex=60 * 60 * 24 * 90)  # 90-day TTL
    except Exception as exc:
        logger.warning("Failed to store Teams service URL: %s", format_exception_message(exc))


async def get_conversation_reference(
    user_email: str, *, personal: bool = False
) -> dict | None:
    """Retrieve a stored Teams conversation reference for proactive messaging."""
    from app.core.redis import redis_client

    try:
        redis = await redis_client()
        key = _conversation_reference_key(user_email, personal=personal)
        raw = await redis.get(key)
        if raw:
            return json.loads(raw)
    except Exception as exc:
        logger.warning("Failed to load Teams conversation reference: %s", format_exception_message(exc))
    return None


def _is_personal_reference(ref: object) -> bool:
    if not isinstance(ref, dict):
        return False
    conversation_id = ref.get("conversation_id")
    return (
        isinstance(conversation_id, str)
        and conversation_id.startswith("a:")
        and isinstance(ref.get("service_url"), str)
        and _is_botframework_service_url(ref["service_url"])
    )


async def get_personal_conversation_reference(user_email: str) -> dict | None:
    """The user's personal chat with the bot, or ``None`` when none is known.

    Recorded on every personal message; the reference of the last
    conversation the user wrote from stands in when that conversation was the
    personal chat (written by a release that did not record it apart).
    """
    for personal in (True, False):
        ref = await get_conversation_reference(user_email, personal=personal)
        if _is_personal_reference(ref):
            return ref
    return None


async def post_to_teams_personal_chat(user_email: str, text: str) -> str:
    """Post *text* in a user's personal chat with the bot.

    Returns the chat's conversation id - the thread the router reads off
    every message of that chat. A personal chat has no threads: the
    conversation bound to it is continued by every message the user sends
    there, until ``/new`` or another conversation is continued in it. The bot
    can only write where the user wrote to it first.
    """
    from app.services.channel_continuation import (
        REASON_DELIVERY_FAILED,
        REASON_NOT_CONFIGURED,
        REASON_TEAMS_CHAT_UNKNOWN,
        ChannelContinuationError,
    )

    ref = await get_personal_conversation_reference(user_email)
    if ref is None:
        raise ChannelContinuationError(
            REASON_TEAMS_CHAT_UNKNOWN,
            "Send a message to the bot in Microsoft Teams once, then try again.",
        )
    try:
        creds = await _get_teams_creds()
        if not creds:
            raise ChannelContinuationError(
                REASON_NOT_CONFIGURED, "No Microsoft Teams bot is connected."
            )
        token, app_id = creds
        channel = TeamsChannel(
            ref["service_url"],
            ref["conversation_id"],
            token,
            bot_id=app_id,
            conversation_type="personal",
        )
        sent = await channel.send_message(OutboundMessage(text=text))
    except (httpx.HTTPError, ValueError) as exc:
        # A timeout or a refused connection, at the token or at the post.
        logger.warning(
            "Teams continuation: Microsoft Teams could not be reached: %s",
            format_exception_message(exc),
        )
        raise ChannelContinuationError(
            REASON_DELIVERY_FAILED,
            "Microsoft Teams could not be reached. Try again in a moment.",
        ) from None
    if sent is None:
        raise ChannelContinuationError(
            REASON_DELIVERY_FAILED, "Microsoft Teams did not accept the message."
        )
    return ref["conversation_id"]


# ---------------------------------------------------------------------------
# Chat context backfill — fetch prior messages when bot joins a conversation
# ---------------------------------------------------------------------------

BACKFILL_MESSAGE_LIMIT = 50
BACKFILL_REDIS_TTL = 60 * 60 * 24 * 7  # 7 days


async def fetch_teams_chat_context(conversation_id: str) -> str | None:
    """Fetch prior conversation context from a Teams chat via Graph API.

    Reads recent messages and members from the chat so the bot understands
    what was being discussed before it was added.  Works for group chats
    (``19:…@thread.v2``) and ``@unq.gbl.spaces`` conversation IDs.

    Returns a formatted summary string, or ``None`` if unavailable.
    Requires ``Chat.Read.All`` or ``ChatMessage.Read.All`` application
    permission on the Entra ID app registration.
    """
    creds = await _get_teams_graph_creds()
    if not creds:
        logger.debug("Cannot fetch Teams chat context — no Graph credentials")
        return None

    graph_token, _app_id, _tenant_id = creds
    headers = {
        "Authorization": f"Bearer {graph_token}",
        "Content-Type": "application/json",
    }

    parts: list[str] = []

    async with httpx.AsyncClient(timeout=30) as client:
        # 1. Chat members
        try:
            members_resp = await client.get(
                f"{GRAPH_API}/chats/{conversation_id}/members",
                headers=headers,
            )
            if members_resp.status_code == 200:
                members = members_resp.json().get("value", [])
                if members:
                    member_lines: list[str] = []
                    for m in members:
                        name = m.get("displayName", "Unknown")
                        email = m.get("email", "")
                        if email:
                            member_lines.append(f"{name} ({email})")
                        else:
                            member_lines.append(name)
                    parts.append(
                        "Conversation participants: " + ", ".join(member_lines)
                    )
            else:
                logger.debug(
                    "Teams chat members request returned HTTP %d for %s",
                    members_resp.status_code,
                    conversation_id[:60],
                )
        except Exception as exc:
            logger.debug("Failed to fetch Teams chat members: %s", format_exception_message(exc))

        # 2. Recent messages (newest first from API, reversed to chronological)
        try:
            msgs_resp = await client.get(
                f"{GRAPH_API}/chats/{conversation_id}/messages"
                f"?$top={BACKFILL_MESSAGE_LIMIT}"
                "&$orderby=createdDateTime desc",
                headers=headers,
            )
            if msgs_resp.status_code == 200:
                raw_msgs = msgs_resp.json().get("value", [])
                # Shared card-aware formatter: renders Adaptive Card
                # / hero-card attachments and application senders instead of
                # dropping card-only messages.
                msg_lines = [
                    line
                    for m in reversed(raw_msgs)
                    if (line := _format_channel_message(m))
                ]
                if msg_lines:
                    parts.append(
                        "Recent conversation history:\n" + "\n".join(msg_lines)
                    )
            elif msgs_resp.status_code in (401, 403):
                logger.info(
                    "Cannot read Teams chat messages (HTTP %d) — "
                    "Chat.Read.All permission may not be granted on the app registration",
                    msgs_resp.status_code,
                )
            else:
                logger.debug(
                    "Teams chat messages request returned HTTP %d for %s",
                    msgs_resp.status_code,
                    conversation_id[:60],
                )
        except Exception as exc:
            logger.debug("Failed to fetch Teams chat messages: %s", format_exception_message(exc))

    if not parts:
        return None

    return "\n\n".join(parts)


def _adaptive_card_text(card: dict[str, Any]) -> str:
    """Extract the readable text of an Adaptive Card payload.

    Walks TextBlocks, FactSets (title + value) and nested containers/column
    sets, then appends action titles (with URLs for OpenUrl actions).  Used
    to surface bot-posted cards (ticket escalations, digests, approvals) in
    thread context — without this the agent sees an empty root message and
    loses the thread topic.
    """
    lines: list[str] = []

    def _walk(items: Any) -> None:
        if not isinstance(items, list):
            return
        for el in items:
            if not isinstance(el, dict):
                continue
            el_type = el.get("type")
            if el_type == "TextBlock":
                text = str(el.get("text") or "").strip()
                if text:
                    lines.append(text)
            elif el_type == "FactSet":
                for fact in el.get("facts") or []:
                    if not isinstance(fact, dict):
                        continue
                    title = str(fact.get("title") or "").strip()
                    value = str(fact.get("value") or "").strip()
                    if title or value:
                        lines.append(f"{title} {value}".strip())
            elif el_type == "ColumnSet":
                for col in el.get("columns") or []:
                    if isinstance(col, dict):
                        _walk(col.get("items"))
            else:
                # Container and other item-bearing elements.
                _walk(el.get("items"))

    _walk(card.get("body"))
    for action in card.get("actions") or []:
        if not isinstance(action, dict):
            continue
        title = str(action.get("title") or "").strip()
        url = str(action.get("url") or "").strip()
        if title and url:
            lines.append(f"[{title}]({url})")
        elif title:
            lines.append(f"[Action: {title}]")
    return " | ".join(lines)


def _message_attachments_text(msg: dict[str, Any]) -> str:
    """Render a Graph message's card/file attachments as readable text.

    Graph puts card payloads in ``attachments[].content`` (a JSON string)
    while ``body.content`` only carries an ``<attachment id>`` placeholder,
    so a formatter that reads the body alone silently drops every card.
    """
    parts: list[str] = []
    for att in msg.get("attachments") or []:
        if not isinstance(att, dict):
            continue
        ctype = att.get("contentType") or ""
        content = att.get("content")
        if isinstance(content, str) and content.strip().startswith("{"):
            try:
                content = json.loads(content)
            except ValueError:
                content = None
        if ctype == "application/vnd.microsoft.card.adaptive" and isinstance(
            content, dict
        ):
            card_text = _adaptive_card_text(content)
            if card_text:
                parts.append(f"[Adaptive Card] {card_text}")
                continue
        if ctype in (
            "application/vnd.microsoft.card.hero",
            "application/vnd.microsoft.card.thumbnail",
        ) and isinstance(content, dict):
            card_lines = [
                str(content.get(k) or "").strip()
                for k in ("title", "subtitle", "text")
            ]
            card_text = " | ".join(line for line in card_lines if line)
            if card_text:
                parts.append(f"[Card] {card_text}")
                continue
        name = str(att.get("name") or "").strip()
        if name:
            parts.append(f"[Attachment: {name}]")
    return " ".join(parts)


def _format_channel_message(msg: dict[str, Any]) -> str | None:
    """Format a single Graph API channel message into a readable line.

    Returns ``None`` for non-user messages (system events, truly empty
    messages).  Card and file attachments are rendered as text so that
    bot-posted Adaptive Cards (the root of many action threads) keep the
    thread topic visible to the agent.
    """
    if msg.get("messageType") != "message":
        return None
    content = msg.get("body", {}).get("content", "") or ""
    if msg.get("body", {}).get("contentType") == "html":
        # Drop <attachment id="..."/> placeholders entirely (the payload is
        # rendered from msg.attachments below), then strip remaining tags.
        content = re.sub(
            r"<attachment[^>]*>.*?</attachment>|<attachment[^>]*/>",
            "",
            content,
            flags=re.S,
        )
        content = re.sub(r"<[^>]+>", "", content).strip()
    content = content.strip()[:500]
    attachments_text = _message_attachments_text(msg)
    if attachments_text:
        content = f"{content} {attachments_text}".strip() if content else attachments_text
    content = content[:800]
    if not content:
        return None
    frm = msg.get("from") or {}
    sender = (
        (frm.get("user") or {}).get("displayName")
        or (frm.get("application") or {}).get("displayName")
        or "Unknown"
    )
    ts = (msg.get("createdDateTime") or "")[:16]
    return f"[{ts}] {sender}: {content}"


async def fetch_teams_channel_thread_context(
    team_id: str,
    channel_id: str,
    reply_to_id: str | None = None,
) -> str | None:
    """Fetch prior conversation context from a Teams channel thread.

    When ``reply_to_id`` is provided the message is a thread reply — fetches
    the root message plus its replies.  When ``reply_to_id`` is ``None`` the
    message is a new root message — fetches recent root messages from the
    channel so the agent sees the surrounding conversation.

    Returns a formatted summary string, or ``None`` if unavailable.
    Requires ``ChannelMessage.Read.All`` application permission.
    """
    creds = await _get_teams_graph_creds()
    if not creds:
        logger.debug("Cannot fetch channel thread context — no Graph credentials")
        return None

    graph_token, _app_id, _tenant_id = creds
    headers = {
        "Authorization": f"Bearer {graph_token}",
        "Content-Type": "application/json",
    }

    msg_lines: list[str] = []

    async with httpx.AsyncClient(timeout=30) as client:
        if reply_to_id:
            # Thread reply — fetch root message + replies
            try:
                root_resp = await client.get(
                    f"{GRAPH_API}/teams/{team_id}/channels/{channel_id}"
                    f"/messages/{reply_to_id}",
                    headers=headers,
                    timeout=15,
                )
                if root_resp.status_code == 200:
                    line = _format_channel_message(root_resp.json())
                    if line:
                        msg_lines.append(line)
            except Exception as exc:
                logger.debug("Failed to fetch thread root message: %s", format_exception_message(exc))

            try:
                replies_resp = await client.get(
                    f"{GRAPH_API}/teams/{team_id}/channels/{channel_id}"
                    f"/messages/{reply_to_id}/replies"
                    f"?$top={BACKFILL_MESSAGE_LIMIT}",
                    headers=headers,
                    timeout=15,
                )
                if replies_resp.status_code == 200:
                    raw = replies_resp.json().get("value", [])
                    for m in reversed(raw):
                        line = _format_channel_message(m)
                        if line:
                            msg_lines.append(line)
                elif replies_resp.status_code in (401, 403):
                    logger.info(
                        "Cannot read Teams channel replies (HTTP %d) — "
                        "ChannelMessage.Read.All permission may not be granted",
                        replies_resp.status_code,
                    )
                else:
                    logger.debug(
                        "Teams channel replies returned HTTP %d for %s/%s/%s",
                        replies_resp.status_code,
                        team_id[:20],
                        channel_id[:20],
                        reply_to_id[:20],
                    )
            except Exception as exc:
                logger.debug("Failed to fetch thread replies: %s", format_exception_message(exc))
        else:
            # Root message (no thread) — fetch a small set of recent
            # channel messages for general context.  Keep the count low
            # (10) to avoid injecting noise from unrelated conversations.
            root_context_limit = 10
            try:
                msgs_resp = await client.get(
                    f"{GRAPH_API}/teams/{team_id}/channels/{channel_id}"
                    f"/messages?$top={root_context_limit}",
                    headers=headers,
                    timeout=15,
                )
                if msgs_resp.status_code == 200:
                    raw = msgs_resp.json().get("value", [])
                    for m in reversed(raw):
                        line = _format_channel_message(m)
                        if line:
                            msg_lines.append(line)
                elif msgs_resp.status_code in (401, 403):
                    logger.info(
                        "Cannot read Teams channel messages (HTTP %d) — "
                        "ChannelMessage.Read.All permission may not be granted",
                        msgs_resp.status_code,
                    )
                else:
                    logger.debug(
                        "Teams channel messages returned HTTP %d for %s/%s",
                        msgs_resp.status_code,
                        team_id[:20],
                        channel_id[:20],
                    )
            except Exception as exc:
                logger.debug("Failed to fetch channel messages: %s", format_exception_message(exc))

    if not msg_lines:
        return None

    if reply_to_id:
        return "Thread messages:\n" + "\n".join(msg_lines)
    return "Recent channel activity (not a specific thread):\n" + "\n".join(msg_lines)


async def _backfill_chat_context(conversation_id: str) -> None:
    """Fetch and cache conversation context when the bot is added to a chat.

    Called asynchronously from the ``conversationUpdate`` handler.  The
    cached context is later injected into the system prompt on the first
    message the bot processes in this conversation.
    """
    try:
        from app.core.redis import redis_client as _redis

        context = await fetch_teams_chat_context(conversation_id)
        if context:
            redis = await _redis()
            key = f"teams:chat_backfill:{conversation_id}"
            await redis.set(key, context, ex=BACKFILL_REDIS_TTL)
            logger.info(
                "Teams: cached chat backfill for %s (%d chars)",
                conversation_id[:60],
                len(context),
            )
        else:
            logger.info(
                "Teams: no chat context available to backfill for %s",
                conversation_id[:60],
            )
    except Exception as exc:
        logger.warning("Teams: failed to backfill chat context: %s", format_exception_message(exc))


async def load_teams_chat_backfill(conversation_id: str) -> str | None:
    """Load cached conversation backfill from Redis.

    Called by the router on the first message in a new Teams conversation.
    Returns the backfill text if available, or ``None``.
    """
    from app.core.redis import redis_client as _redis

    try:
        redis = await _redis()
        key = f"teams:chat_backfill:{conversation_id}"
        raw = await redis.get(key)
        if raw:
            return raw if isinstance(raw, str) else raw.decode()
    except Exception as exc:
        logger.debug("Failed to load Teams chat backfill: %s", format_exception_message(exc))
    return None


# ---------------------------------------------------------------------------
# Private / shared channel — Graph API helpers
# ---------------------------------------------------------------------------


async def _get_teams_graph_creds() -> tuple[str, str, str] | None:
    """Obtain a Graph API token + app_id + tenant_id from channel bot creds.

    Returns ``(graph_token, app_id, tenant_id)`` or ``None``.
    Uses the same Entra ID app registration as the Bot Framework channel
    but requests the ``https://graph.microsoft.com/.default`` scope.
    """
    bot = await get_channel_bot("teams_bot")
    if not bot:
        return None

    app_id = decrypt_value(bot.client_id_enc) if bot.client_id_enc else ""
    app_password = (await reveal_field(bot, "client_secret_enc")) or ""
    if not app_id or not app_password:
        return None

    tenant_id = ""
    if bot.extra_config_enc:
        try:
            extra = await reveal_json_field(bot, "extra_config_enc") or {}
            tenant_id = extra.get("tenant_id", "")
        except Exception as exc:
            logger.warning("Failed to decrypt Teams bot extra config: %s", format_exception_message(exc))

    if not tenant_id:
        logger.debug("No tenant_id in teams_bot config — cannot acquire Graph token")
        return None

    from app.integrations.providers.teams_bot import _acquire_token

    token = await _acquire_token(
        tenant_id,
        app_id,
        app_password,
        scope="https://graph.microsoft.com/.default",
    )
    if token:
        return token, app_id, tenant_id
    return None


async def _get_channel_membership_type(
    team_id: str,
    channel_id: str,
    graph_token: str,
) -> str | None:
    """Return the membership type of a Teams channel (``standard`` / ``private`` / ``shared``).

    Cached in Redis for 30 days to avoid repeated Graph calls.
    """
    from app.core.redis import redis_client

    cache_key = f"teams:ch_type:{team_id}:{channel_id}"
    redis = await redis_client()
    cached = await redis.get(cache_key)
    if cached:
        return cached

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{GRAPH_API}/teams/{team_id}/channels/{channel_id}",
                headers={"Authorization": f"Bearer {graph_token}"},
                params={"$select": "id,displayName,membershipType"},
                timeout=10,
            )
            if resp.status_code == 200:
                data = resp.json()
                mtype = data.get("membershipType", "standard")
                await redis.set(cache_key, mtype, ex=60 * 60 * 24 * 30)
                return mtype
            logger.warning(
                "Graph channel type lookup failed: %s %s",
                resp.status_code,
                resp.text[:200],
            )
    except Exception as e:
        logger.warning("Graph channel type lookup error: %s", format_exception_message(e))
    return None


# ---------------------------------------------------------------------------
# Graph subscription lifecycle (create / renew / delete)
# ---------------------------------------------------------------------------
#
# Graph's rules shape everything below: one subscription per app and channel
# (another is refused with 403, one with the same changeType and resource with
# 409), a clientState fixed at creation (a PATCH changes only
# expirationDateTime / notificationUrl), and a lifecycleNotificationUrl
# required when a Teams subscription lasts more than an hour. So a
# subscription is replaced by deleting it first, and every subscription gets
# lifecycle notifications, answered by ``_process_graph_lifecycle_notification``.

_GRAPH_NOTIFICATION_PATH = "/api/channels/teams/graph-notifications"


# A notification that does not prove its subscription is refused only under
# Settings > Access and security "Require verified integration notifications"
# (INTEGRATION_NOTIFICATIONS_REQUIRE_VERIFICATION, ``verification_required()``);
# off by default, one earlier releases acted on is still acted on, and
# reported.

# When notifications were last acted on unverified, read by the Security Audit.
UNVERIFIED_NOTIFICATIONS_KEY = "teams:graph_unverified"
_UNVERIFIED_NOTIFICATIONS_TTL = 7 * 24 * 60 * 60

# Graph retries a notification it could not deliver for up to four hours.
_GRAPH_RETRY_HORIZON = timedelta(hours=4)

# How long the notifications of a replaced subscription are still accepted:
# the ones on their way when it was deleted, and Graph's retries of those it
# could not deliver - the replacement runs on the first pass after a restart,
# while what failed during the restart is being retried.
_REPLACED_SUBSCRIPTION_GRACE = _GRAPH_RETRY_HORIZON

# Outlasts one channel's turn in the renewal pass with every Graph call timing
# out (a deletion, two creations around a paged listing and an orphan's
# deletion, then the catch-up: about six minutes); a holder that dies costs
# that channel one pass, which runs hourly.
_SUBSCRIPTION_LOCK_TTL = 600

# Reauthorization challenges repeat every few minutes; one renewal answers them.
_REAUTHORIZE_THROTTLE_SECS = 300

# Graph delivers a notification at least once, and retries one it could not
# deliver for up to four hours.
_NOTIFICATION_SEEN_TTL = 24 * 60 * 60

_SUBSCRIPTION_LIST_MAX_PAGES = 10

# Graph documents no limit on a notification batch. A notification without
# resource data (what these subscriptions receive) is about 1 KB, so 4 MiB
# holds thousands - the bound the integration notification endpoints use too.
GRAPH_NOTIFICATION_MAX_BODY_BYTES = 4 * 1024 * 1024

# A POST at least this large is parsed and decided in a worker process
# (``teams_graph_worker``); below it the work costs less than the hop.
GRAPH_NOTIFICATION_OFF_LOOP_BYTES = 64 * 1024

# First-time setup waits this long for a worker maintaining the channel: that
# worker may be creating the subscription the setup would otherwise create.
_ENSURE_LOCK_WAIT = 60

# A notification for a channel message reaches us within a minute (Graph's
# documented maximum latency for chatMessage).
_GRAPH_NOTIFICATION_MAX_LATENCY = timedelta(minutes=1)

# How far back a catch-up reads: Graph's own retry horizon. It stays well
# inside ``_NOTIFICATION_SEEN_TTL``, so every message processed live within it
# is still marked and is never processed a second time.
_CATCH_UP_MAX_LOOKBACK = _GRAPH_RETRY_HORIZON

# Clock difference between this platform and the createdDateTime Graph stamps.
_CATCH_UP_CLOCK_SKEW = timedelta(seconds=5)

_CATCH_UP_MAX_THREADS = 50
_CATCH_UP_MAX_PAGES = 5
# A catch-up runs under the channel lock (``_SUBSCRIPTION_LOCK_TTL``).
_CATCH_UP_TIME_BUDGET = 90

# Compare-and-delete in one step: a GET then a DEL could delete the lock of a
# worker that took it over once ours expired in between.
_RELEASE_LOCK_SCRIPT = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('DEL', KEYS[1])
end
return 0
"""


def _gap_key(team_id: str, channel_id: str) -> str:
    """Since when the channel may have missed notifications (ISO timestamp)."""
    return f"teams:priv_ch_gap:{team_id}:{channel_id}"


def _threads_key(team_id: str, channel_id: str) -> str:
    return f"teams:priv_threads:{team_id}:{channel_id}"


def _subscription_expiry() -> datetime:
    return datetime.now(UTC) + timedelta(minutes=GRAPH_SUB_MAX_MINUTES - 10)


def _graph_datetime(value: datetime) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%S.0000000Z")


async def _write_subscription_record(redis: Any, sub_key: str, record: dict) -> None:
    """Store *record* for as long as the subscription it describes lives."""
    try:
        ttl = int((datetime.fromisoformat(record["expires_at"]) - datetime.now(UTC)).total_seconds())
    except (KeyError, TypeError, ValueError):
        ttl = int((GRAPH_SUB_MAX_MINUTES - 10) * 60)
    if ttl > 0:
        await redis.set(sub_key, json.dumps(record), ex=ttl)
    else:
        await redis.delete(sub_key)


@asynccontextmanager
async def _channel_subscription_lock(
    redis: Any, team_id: str, channel_id: str, wait: float = 0, stop_waiting: Any = None
):
    """Hold a channel's subscription against other workers; yields whether it is held.

    The renewal pass and the lifecycle handler read the record, call Graph and
    write the record back: two of them interleaving would write a stale one.
    *stop_waiting* (an awaitable predicate) ends the wait early without the lock.
    """
    key = f"teams:priv_ch_sub_lock:{team_id}:{channel_id}"
    token = secrets.token_hex(16)
    deadline = asyncio.get_running_loop().time() + wait
    held = bool(await redis.set(key, token, nx=True, ex=_SUBSCRIPTION_LOCK_TTL))
    while not held and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.2)
        if stop_waiting is not None and await stop_waiting():
            break
        held = bool(await redis.set(key, token, nx=True, ex=_SUBSCRIPTION_LOCK_TTL))
    try:
        yield held
    finally:
        # Released only while still ours: past its TTL another worker may hold it.
        if held:
            await redis.eval(_RELEASE_LOCK_SCRIPT, 1, key, token)


async def _recorded_subscription_id(redis: Any, team_id: str, channel_id: str) -> tuple[str | None, bool]:
    """The subscription id the channel's record names, and whether a record exists at all."""
    raw = await redis.get(_subscription_key(team_id, channel_id))
    record = _read_subscription_record(raw) if raw else None
    sub_id = record.get("subscription_id") if record else None
    return (sub_id if isinstance(sub_id, str) and sub_id else None), bool(raw)


async def _ensure_channel_subscription(
    team_id: str,
    channel_id: str,
    tenant_id: str,
    graph_token: str,
) -> str | None:
    """Ensure a Graph subscription exists for a private/shared channel.

    Creates one if it doesn't exist or has expired.
    Returns the ``subscription_id`` or ``None``.
    """
    from app.core.redis import redis_client

    redis = await redis_client()
    existing_id, _ = await _recorded_subscription_id(redis, team_id, channel_id)
    if not existing_id:

        async def _recorded_meanwhile() -> bool:
            return (await _recorded_subscription_id(redis, team_id, channel_id))[0] is not None

        # Two mentions answered at once would both create: the second one's
        # 409 would then read the first one's fresh subscription as an orphan.
        # The one that waits adopts what the other records as soon as it does.
        async with _channel_subscription_lock(
            redis, team_id, channel_id, wait=_ENSURE_LOCK_WAIT, stop_waiting=_recorded_meanwhile
        ) as held:
            existing_id, unreadable = await _recorded_subscription_id(redis, team_id, channel_id)
            if not existing_id:
                if not held:
                    logger.warning(
                        "Graph subscription for channel %s:%s is being maintained by another worker - not creating one",
                        team_id,
                        channel_id,
                    )
                    return None
                if unreadable:
                    logger.warning(
                        "Unreadable Graph subscription record for channel %s:%s - creating one", team_id, channel_id
                    )
                return await _create_channel_subscription(team_id, channel_id, tenant_id, graph_token)
    logger.info(
        "Graph subscription already exists for channel %s:%s — reusing sub_id=%s",
        team_id,
        channel_id,
        existing_id,
    )
    # The renewal pass only visits the channels in this set.
    await redis.sadd("teams:priv_ch_subs", f"{team_id}:{channel_id}")
    return existing_id


async def _post_graph_subscription(payload: dict, graph_token: str) -> tuple[int | None, str | None, str]:
    """POST a subscription: ``(status, id, detail)``, the id only when Graph created it."""
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{GRAPH_API}/subscriptions",
                headers={
                    "Authorization": f"Bearer {graph_token}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=30,
            )
    except Exception as e:
        logger.error(
            "Graph subscription creation error: %s",
            format_exception_message(e),
            exc_info=True,
        )
        return None, None, ""
    if resp.status_code in (200, 201):
        sub_id = resp.json().get("id")
        if isinstance(sub_id, str) and sub_id:
            return resp.status_code, sub_id, ""
    return resp.status_code, None, resp.text[:300]


async def _create_channel_subscription(
    team_id: str,
    channel_id: str,
    tenant_id: str,
    graph_token: str,
    *,
    replaces: str | None = None,
    replaced: list[dict] | None = None,
) -> str | None:
    """Create a Graph subscription for a channel and record it; ``None`` on failure.

    The record is written only once Graph has created the subscription.
    *replaces* is the subscription the channel's record still names, already
    deleted; *replaced* the grace entries the new record keeps for it.

    A refusal (403 / 409) means the channel already has a subscription. One
    another worker has just recorded is adopted. One no record describes is an
    orphan holding the channel's only slot: it is deleted and the creation
    tried once more - by default only an orphan this release created (it has a
    lifecycle URL and its clientState was in the lost record, so nothing it
    sends is acted on). One an earlier release created may still be the
    channel's working subscription, its notifications acted on as before, so
    it is kept until it expires unless verification is required.
    """
    from app.core.redis import redis_client

    redis = await redis_client()
    sub_key = _subscription_key(team_id, channel_id)
    resource = f"/teams/{team_id}/channels/{channel_id}/messages"
    notification_url = f"{settings.base_url}{_GRAPH_NOTIFICATION_PATH}"
    # A lifecycle notification names its subscription but not its channel.
    lifecycle_url = f"{notification_url}?{urlencode({'team': team_id, 'channel': channel_id})}"
    status: int | None = None
    detail = ""
    for attempt in range(2):
        expiration = _subscription_expiry()
        # Random per-subscription secret echoed back by Graph on every
        # notification (``clientState``), stored Fernet-encrypted and checked by
        # ``_record_proves``. A derivable value (team ids appear in
        # conversation ids, deep links, and logs) would let anyone who knows the
        # team id forge notifications.
        client_state = secrets.token_urlsafe(32)
        status, sub_id, detail = await _post_graph_subscription(
            {
                "changeType": "created",
                "notificationUrl": notification_url,
                "lifecycleNotificationUrl": lifecycle_url,
                "resource": resource,
                "expirationDateTime": _graph_datetime(expiration),
                "clientState": client_state,
            },
            graph_token,
        )
        if sub_id:
            record = {
                "subscription_id": sub_id,
                "team_id": team_id,
                "channel_id": channel_id,
                "tenant_id": tenant_id,
                "expires_at": expiration.isoformat(),
                "client_state_enc": encrypt_value(client_state),
            }
            if replaced:
                record["replaced_subscriptions"] = replaced
            await _write_subscription_record(redis, sub_key, record)
            await redis.sadd("teams:priv_ch_subs", f"{team_id}:{channel_id}")
            logger.info(
                "Graph subscription created for private channel: sub_id=%s, team=%s, channel=%s",
                sub_id,
                team_id,
                channel_id,
            )
            # Notifications flow again: read what was posted while none did.
            await _catch_up_channel(redis, team_id, channel_id, graph_token)
            return sub_id
        if attempt or status not in (403, 409):
            break
        raw = await redis.get(sub_key)
        current = _read_subscription_record(raw) if raw else None
        current_id = current.get("subscription_id") if current else None
        if isinstance(current_id, str) and current_id and current_id != replaces:
            return current_id
        orphans = await _find_graph_subscriptions(resource, graph_token)
        if not verification_required():
            kept = [o for o in orphans if not o.get("lifecycleNotificationUrl")]
            orphans = [o for o in orphans if o.get("lifecycleNotificationUrl")]
            if kept and not orphans:
                logger.info(
                    "Kept %d Graph subscription(s) an earlier release created for team=%s, channel=%s "
                    "until they expire (their notifications are acted on as before); a new one is "
                    "created once Graph has room for it",
                    len(kept),
                    team_id,
                    channel_id,
                )
                return None
        if not orphans:
            break
        # Between the deletion and the creation nothing notifies the channel.
        await _mark_notification_gap(redis, team_id, channel_id, datetime.now(UTC))
        for orphan in orphans:
            await _delete_graph_subscription(orphan["id"], graph_token)
        logger.warning(
            "Deleted %d Graph subscription(s) no record described for team=%s, channel=%s",
            len(orphans),
            team_id,
            channel_id,
        )
    if status is not None:
        logger.error("Graph subscription creation failed: %s %s", status, detail)
    return None


async def _find_graph_subscriptions(resource: str, graph_token: str) -> list[dict]:
    """This app's Graph subscriptions to *resource*, as Graph lists them (never with their clientState)."""
    wanted = resource.strip("/").lower()
    found: list[dict] = []
    url: Any = f"{GRAPH_API}/subscriptions"
    try:
        async with httpx.AsyncClient() as client:
            for _ in range(_SUBSCRIPTION_LIST_MAX_PAGES):
                resp = await client.get(url, headers={"Authorization": f"Bearer {graph_token}"}, timeout=15)
                if resp.status_code != 200:
                    logger.warning("Graph subscription listing failed: %s %s", resp.status_code, resp.text[:200])
                    break
                page = resp.json()
                for sub in page.get("value") or []:
                    if (
                        isinstance(sub, dict)
                        and isinstance(sub.get("id"), str)
                        and isinstance(sub.get("resource"), str)
                        and sub["resource"].strip("/").lower() == wanted
                    ):
                        found.append(sub)
                url = page.get("@odata.nextLink")
                # Only Graph's own paging link: the token goes nowhere else.
                if not (isinstance(url, str) and url.startswith(f"{GRAPH_API}/")):
                    break
    except Exception as e:
        logger.warning("Graph subscription listing error: %s", format_exception_message(e))
    return found


async def _delete_graph_subscription(
    subscription_id: str,
    graph_token: str,
) -> bool:
    """Delete a Graph API subscription; ``True`` once it no longer exists.

    A 404 says it already did not (expired, or deleted before).
    """
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.delete(
                f"{GRAPH_API}/subscriptions/{subscription_id}",
                headers={"Authorization": f"Bearer {graph_token}"},
                timeout=15,
            )
            if resp.status_code in (200, 204):
                logger.info("Graph subscription deleted: %s", subscription_id)
                return True
            if resp.status_code == 404:
                logger.info("Graph subscription %s was already gone", subscription_id)
                return True
            logger.warning(
                "Graph subscription delete failed: %s %s",
                resp.status_code,
                resp.text[:200],
            )
            return False
    except Exception as e:
        logger.warning("Graph subscription delete error: %s", format_exception_message(e))
    return False


async def _renew_graph_subscription(redis: Any, sub_key: str, record: dict, graph_token: str) -> str:
    """Extend the subscription *record* describes: ``renewed``, ``gone`` (404) or ``failed``.

    A renewal also answers a reauthorization challenge.
    """
    sub_id = record["subscription_id"]
    new_exp = _subscription_expiry()
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.patch(
                f"{GRAPH_API}/subscriptions/{sub_id}",
                headers={
                    "Authorization": f"Bearer {graph_token}",
                    "Content-Type": "application/json",
                },
                json={"expirationDateTime": _graph_datetime(new_exp)},
                timeout=15,
            )
    except Exception as e:
        logger.warning("Graph subscription renewal error (%s): %s", sub_id, format_exception_message(e))
        return "failed"
    if resp.status_code == 200:
        record["expires_at"] = new_exp.isoformat()
        await _write_subscription_record(redis, sub_key, record)
        logger.info("Graph subscription renewed: %s", sub_id)
        return "renewed"
    if resp.status_code == 404:
        logger.info("Graph subscription %s no longer exists", sub_id)
        return "gone"
    logger.warning(
        "Graph subscription renewal failed (%s): %s %s",
        sub_id,
        resp.status_code,
        resp.text[:200],
    )
    return "failed"


def _prune_replaced_subscriptions(record: dict) -> bool:
    """Drop the replaced subscriptions past their grace period; whether any was dropped."""
    if "replaced_subscriptions" not in record:
        return False
    replaced = record["replaced_subscriptions"]
    now = datetime.now(UTC)
    kept = [e for e in replaced if isinstance(e, dict) and _grace_open(e, now)] if isinstance(replaced, list) else []
    if kept == replaced:
        return False
    if kept:
        record["replaced_subscriptions"] = kept
    else:
        del record["replaced_subscriptions"]
    return True


# Why a notification was acted on without proving its subscription, as logged.
_UNVERIFIED_REASONS = {
    "no_record": "its channel has no subscription record, and it carries the value subscriptions were created "
    "with before 2026-07-02",
    "unreadable_record": "its channel's subscription record cannot be read, and it carries the value "
    "subscriptions were created with before 2026-07-02",
    "derivable_client_state": "it carries the value subscriptions were created with before 2026-07-02, "
    "not under the subscription id recorded for its channel",
    "subscription_not_matched": "it carries its channel's clientState under another subscription id or tenant",
    "unreadable_client_state": "its channel's clientState cannot be decrypted (a SECRET_KEY change), and it "
    "names the subscription recorded for the channel",
}


async def _report_unverified_notification(redis: Any, reason: str) -> None:
    """Say that a notification was acted on unverified: a rate-limited warning and the audit's marker."""
    log_rate_limited(
        f"teams-graph-{reason}",
        logging.WARNING,
        "Teams channel Graph notification on %s accepted without proof of its subscription: %s, as "
        "before notifications were checked against their subscription. Set "
        "INTEGRATION_NOTIFICATIONS_REQUIRE_VERIFICATION=true to refuse it.",
        _GRAPH_NOTIFICATION_PATH,
        _UNVERIFIED_REASONS.get(reason, reason),
    )
    try:
        await redis.hincrby(UNVERIFIED_NOTIFICATIONS_KEY, "count", 1)
        await redis.hset(
            UNVERIFIED_NOTIFICATIONS_KEY,
            mapping={"last_at": datetime.now(UTC).isoformat(), "last_reason": reason},
        )
        await redis.expire(UNVERIFIED_NOTIFICATIONS_KEY, _UNVERIFIED_NOTIFICATIONS_TTL)
    except Exception as exc:
        logger.debug("Could not record an unverified Graph notification: %s", format_exception_message(exc))


async def _graph_notification_accepted(notif: dict, team_id: str, channel_id: str) -> bool:
    """Whether *notif* is acted on: it proves a subscription recorded for its channel, or - unless
    verification is required - earlier releases acted on it (``_notification_verdict``).

    A refusal on a monitored channel whose record is missing or can prove
    nothing may be a genuine notification the platform cannot verify: the
    channel is marked, and once its subscription exists again the messages
    are read back from Graph (``_catch_up_channel``) - never from the refused
    notification itself.
    """
    from app.core.redis import redis_client

    redis = await redis_client()
    raw = await redis.get(_subscription_key(team_id, channel_id))
    record = _read_subscription_record(raw) if raw else None
    verdict, reason = _notification_verdict(record, notif, team_id, recorded=bool(raw))
    if verdict == "proven":
        return True
    if verdict == "unverified" and not verification_required():
        await _report_unverified_notification(redis, reason)
        return True
    if (record is None or _record_state(record, team_id) == "unprovable") and await redis.sismember(
        "teams:priv_ch_subs", f"{team_id}:{channel_id}"
    ):
        await _mark_notification_gap(redis, team_id, channel_id, datetime.now(UTC) - _GRAPH_NOTIFICATION_MAX_LATENCY)
    return False


async def _replace_channel_subscription(
    redis: Any,
    sub_key: str,
    record: dict,
    team_id: str,
    channel_id: str,
    tenant_id: str,
    graph_token: str,
) -> bool:
    """Replace a subscription whose clientState is derivable or unreadable; whether anything changed.

    Its clientState cannot be changed and no second subscription can exist
    beside it, so it is deleted first: if that fails, nothing changes and the
    next pass tries again. The notifications it had already sent are accepted
    for ``_REPLACED_SUBSCRIPTION_GRACE``. A message posted between the deletion
    and the new subscription is never notified, so it is read back from Graph
    once the new one exists (``_catch_up_channel``).
    """
    old_id = record.get("subscription_id")
    if not (isinstance(old_id, str) and old_id):
        old_id = None
    gap_starts = datetime.now(UTC)
    if old_id and not await _delete_graph_subscription(old_id, graph_token):
        return False
    await _mark_notification_gap(redis, team_id, channel_id, gap_starts)
    replaced = []
    issued = _issued_client_state(record, team_id)
    if old_id and issued:
        replaced.append(
            {
                "id": old_id,
                "client_state_enc": encrypt_value(issued),
                "accept_until": (datetime.now(UTC) + _REPLACED_SUBSCRIPTION_GRACE).isoformat(),
            }
        )
    new_id = await _create_channel_subscription(
        team_id, channel_id, tenant_id, graph_token, replaces=old_id, replaced=replaced
    )
    if new_id:
        logger.info("Graph subscription %s replaced by %s with a random clientState", old_id, new_id)
    else:
        # Nothing notifies for the channel any more: the record would only
        # vouch for forgeries. The next pass creates a subscription.
        await redis.delete(sub_key)
    return True


# The channels whose record the Security Audit reads, at most.
_HEALTH_MAX_CHANNELS = 1000


@dataclass(frozen=True)
class GraphNotificationHealth:
    """What the Security Audit says about the Teams Graph notification endpoint."""

    monitored: int
    #: How many of the monitored channels were read (at most ``_HEALTH_MAX_CHANNELS``).
    checked: int
    #: Subscriptions created with the derivable ``copilot_{team_id}`` value.
    derivable: int
    #: Records that cannot be read, or whose clientState cannot be decrypted.
    unreadable: int
    #: Monitored channels with no subscription record.
    missing: int
    #: Notifications acted on without proving their subscription, last seven days.
    unverified: int
    unverified_last_at: str | None


async def graph_notification_health() -> GraphNotificationHealth:
    """Classify the monitored channels' records and read the unverified-notification marker.

    Read-only, and never a secret: only how each record is stored.
    """
    from app.core.redis import redis_client

    redis = await redis_client()
    channels = sorted(await redis.smembers("teams:priv_ch_subs"))
    checked = [key for key in channels if ":" in key][:_HEALTH_MAX_CHANNELS]
    derivable = unreadable = missing = 0
    for start in range(0, len(checked), 200):
        chunk = checked[start : start + 200]
        raws = await redis.mget([_subscription_key(*key.split(":", 1)) for key in chunk])
        for key, raw in zip(chunk, raws, strict=True):
            if not raw:
                missing += 1
                continue
            record = _read_subscription_record(raw)
            state = _record_state(record, key.split(":", 1)[0]) if record is not None else "unprovable"
            if state == "legacy":
                derivable += 1
            elif state == "unprovable":
                unreadable += 1
    marker = await redis.hgetall(UNVERIFIED_NOTIFICATIONS_KEY) or {}
    try:
        unverified = int(marker.get("count") or 0)
    except (TypeError, ValueError):
        unverified = 0
    last_at = marker.get("last_at")
    return GraphNotificationHealth(
        monitored=len(channels),
        checked=len(checked),
        derivable=derivable,
        unreadable=unreadable,
        missing=missing,
        unverified=unverified,
        unverified_last_at=last_at if isinstance(last_at, str) else None,
    )


# ---------------------------------------------------------------------------
# Catching up on messages posted while no subscription notified
# ---------------------------------------------------------------------------
#
# Graph says so itself: after a subscription is recreated, the app fetches the
# changes it was not notified of. Only replies in the channel's monitored
# threads are acted on (a root post is never a follow-up), so the catch-up
# reads those threads' replies (``GET .../messages/{root}/replies``, the same
# ChannelMessage.Read.Group permission as reading one message) and hands each
# one to the path a notification takes, behind the same once-per-message mark.


async def _mark_notification_gap(
    redis: Any, team_id: str, channel_id: str, since: datetime, *, best_effort: bool = True
) -> None:
    """Remember that the channel may have missed notifications since *since* (the earliest wins).

    *best_effort*: a failed write is logged. The route passes ``False``: a
    refused batch is only answered 2xx once its channel's gap is recorded.
    """
    try:
        await redis.set(
            _gap_key(team_id, channel_id),
            since.isoformat(),
            nx=True,
            ex=int(_CATCH_UP_MAX_LOOKBACK.total_seconds()),
        )
    except Exception as exc:
        if not best_effort:
            raise
        logger.warning("Could not record a Graph notification gap: %s", format_exception_message(exc))


def _graph_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


async def _replies_since(
    client: httpx.AsyncClient, graph_token: str, team_id: str, channel_id: str, root_id: str, since: datetime, deadline: float
) -> tuple[list[dict], bool]:
    """The replies to *root_id* created since *since*, oldest first, and whether the read was complete.

    Graph lists replies newest first, 50 at most per page: paging stops at the
    first reply older than *since* - on a page that is newest first, since
    only that order says every later page is older still.
    """
    found: list[tuple[datetime, dict]] = []

    def _oldest_first() -> list[dict]:
        return [reply for _created, reply in sorted(found, key=lambda item: item[0])]

    url: Any = f"{GRAPH_API}/teams/{team_id}/channels/{channel_id}/messages/{root_id}/replies"
    params: dict | None = {"$top": "50"}
    for _ in range(_CATCH_UP_MAX_PAGES):
        if asyncio.get_running_loop().time() > deadline:
            return _oldest_first(), False
        try:
            resp = await client.get(url, headers={"Authorization": f"Bearer {graph_token}"}, params=params, timeout=15)
        except Exception as exc:
            logger.warning("Graph replies listing error: %s", format_exception_message(exc))
            return _oldest_first(), False
        if resp.status_code != 200:
            logger.warning("Graph replies listing failed: %s %s", resp.status_code, resp.text[:200])
            # A thread whose root is gone has nothing left to catch up on.
            return _oldest_first(), resp.status_code == 404
        reached_older = False
        newest_first = True
        previous: datetime | None = None
        page = resp.json()
        for reply in page.get("value") or []:
            created = _graph_timestamp(reply.get("createdDateTime")) if isinstance(reply, dict) else None
            if created is None:
                continue
            if previous is not None and created > previous:
                newest_first = False
            previous = created
            if created < since:
                reached_older = True
            else:
                found.append((created, reply))
        url = page.get("@odata.nextLink")
        params = None
        # Only Graph's own paging link: the token goes nowhere else.
        if (reached_older and newest_first) or not (isinstance(url, str) and url.startswith(f"{GRAPH_API}/")):
            return _oldest_first(), True
    return _oldest_first(), False


async def _catch_up_channel(redis: Any, team_id: str, channel_id: str, graph_token: str) -> int:
    """Process the replies the channel's monitored threads received during its recorded gap.

    Each reply goes through the once-per-message mark notifications use, so a
    reply that was also notified (by a replaced subscription's last deliveries,
    or by the new one) is processed exactly once. The gap is forgotten only
    once every thread was read. Returns the number of replies queued.
    """
    gap_key = _gap_key(team_id, channel_id)
    try:
        raw = await redis.get(gap_key)
        if not raw:
            return 0
        now = datetime.now(UTC)
        since = _graph_timestamp(raw) or now - _CATCH_UP_MAX_LOOKBACK
        since = max(since, now - _CATCH_UP_MAX_LOOKBACK) - _CATCH_UP_CLOCK_SKEW
        threads = await redis.zrangebyscore(_threads_key(team_id, channel_id), "-inf", "+inf")
        # Sorted by last activity: the most recent threads are kept.
        roots = [t.split(";messageid=", 1)[1] for t in threads if ";messageid=" in t][-_CATCH_UP_MAX_THREADS:]
        deadline = asyncio.get_running_loop().time() + _CATCH_UP_TIME_BUDGET
        complete = True
        queued = 0
        async with httpx.AsyncClient() as client:
            for root_id in roots:
                replies, read_all = await _replies_since(
                    client, graph_token, team_id, channel_id, root_id, since, deadline
                )
                complete = complete and read_all
                for reply in replies:
                    reply_id = reply.get("id")
                    if not (isinstance(reply_id, str) and reply_id):
                        continue
                    if not await _first_delivery(team_id, channel_id, reply_id):
                        continue
                    spawn_channel_task(
                        _process_caught_up_message(reply, team_id, channel_id), name="teams-graph-catch-up"
                    )
                    queued += 1
        if complete:
            await redis.delete(gap_key)
        logger.info(
            "Graph catch-up for team=%s, channel=%s: %d repl%s queued since %s%s",
            team_id,
            channel_id,
            queued,
            "y" if queued == 1 else "ies",
            since.isoformat(),
            "" if complete else " (incomplete - the next pass reads the rest)",
        )
        return queued
    except Exception as exc:
        logger.warning("Graph catch-up error for team=%s: %s", team_id, format_exception_message(exc))
        return 0


async def _process_caught_up_message(msg: dict, team_id: str, channel_id: str) -> None:
    """Act on a reply read back from Graph exactly as on a notified one."""
    try:
        graph_creds = await _get_teams_graph_creds()
        if not graph_creds:
            return
        graph_token, app_id, tenant_id = graph_creds
        await _route_graph_channel_message(msg, team_id, channel_id, str(msg.get("id")), graph_token, app_id, tenant_id)
    except Exception as e:
        logger.error("Graph catch-up processing error: %s", format_exception_message(e), exc_info=True)


# ---------------------------------------------------------------------------
# Private channel thread tracking
# ---------------------------------------------------------------------------


async def _mark_private_thread(
    team_id: str,
    channel_id: str,
    thread_key: str,
) -> None:
    """Track a private-channel thread for subscription lifecycle management.

    Uses a sorted set: members are thread keys, scores are Unix timestamps.
    """
    from app.core.redis import redis_client

    redis = await redis_client()
    key = f"teams:priv_threads:{team_id}:{channel_id}"
    score = datetime.now(UTC).timestamp()
    await redis.zadd(key, {thread_key: score})
    await redis.expire(key, PRIV_THREAD_TTL + 86400)


async def _extend_thread_ttl(thread_key: str) -> None:
    """Extend ``active_thread`` TTL to 7 days for private-channel threads.

    Called *after* ``handle_message()`` to override the router's default
    24-hour TTL with the longer private-channel monitoring window.
    """
    from app.core.redis import redis_client

    redis = await redis_client()
    key = f"active_thread:teams:{thread_key}"
    if await redis.exists(key):
        await redis.expire(key, PRIV_THREAD_TTL)
    else:
        await redis.set(key, "1", ex=PRIV_THREAD_TTL)


def _compute_thread_key(inbound: InboundMessage) -> str | None:
    """Compute the Teams thread key for an inbound channel message.

    Mirrors ``_get_thread_key()`` from ``router.py`` for Teams.
    Thread keys include ``;messageid=<root>`` — plain channel IDs are
    too broad and return ``None``.
    """
    thread_id = inbound.channel_thread_id or ""
    if ";messageid=" in thread_id:
        return thread_id
    return None


# ---------------------------------------------------------------------------
# Private-channel monitoring setup (called after responding to @mention)
# ---------------------------------------------------------------------------


async def _setup_private_channel_monitoring(
    inbound: InboundMessage,
) -> None:
    """Create a Graph subscription for a private/shared channel after the
    bot responds to an @mention in it.

    Called asynchronously after the bot sends its reply.
    """
    try:
        channel_data = (inbound.raw or {}).get("channelData", {})
        team_id = channel_data.get("team", {}).get("id")
        channel_id = channel_data.get("channel", {}).get("id")

        if not team_id or not channel_id:
            logger.warning(
                "Private channel monitoring: missing team_id or channel_id "
                "in activity channelData (team=%r, channel=%r) — cannot "
                "set up thread monitoring",
                team_id,
                channel_id,
            )
            return

        logger.info(
            "Private channel monitoring: checking channel type — team=%s, "
            "channel=%s",
            team_id,
            channel_id,
        )

        graph_creds = await _get_teams_graph_creds()
        if not graph_creds:
            logger.warning(
                "Private channel monitoring: could not get Graph credentials "
                "(client_id/client_secret/tenant_id missing on the teams_bot "
                "channel bot?) — thread monitoring disabled"
            )
            return

        graph_token, _app_id, tenant_id = graph_creds

        # Check whether this is actually a private / shared channel.
        membership_type = await _get_channel_membership_type(
            team_id,
            channel_id,
            graph_token,
        )
        if membership_type is None:
            logger.warning(
                "Private channel monitoring: channel membershipType lookup "
                "FAILED (see 'Graph channel type lookup' log above for the "
                "Graph status/body) — team=%s, channel=%s. Cannot determine "
                "whether this is a private/shared channel, so thread "
                "monitoring is NOT set up.",
                team_id,
                channel_id,
            )
            return
        if membership_type not in ("private", "shared"):
            logger.info(
                "Private channel monitoring: channel is %r — standard "
                "channels receive all messages via Bot Framework (RSC), no "
                "Graph subscription needed (team=%s, channel=%s)",
                membership_type,
                team_id,
                channel_id,
            )
            return  # Standard channel — Bot Framework already delivers all msgs

        logger.info(
            "Private channel detected (type=%s): team=%s, channel=%s — "
            "setting up Graph subscription for thread monitoring",
            membership_type,
            team_id,
            channel_id,
        )

        # Ensure a Graph subscription exists for this channel
        sub_id = await _ensure_channel_subscription(
            team_id,
            channel_id,
            tenant_id,
            graph_token,
        )
        if not sub_id:
            logger.error(
                "Failed to create Graph subscription for private channel "
                "team=%s channel=%s — follow-up messages in this thread "
                "will NOT reach the bot (see 'Graph subscription creation "
                "failed' log above for the Graph status/body; a 403 here "
                "means Microsoft blocks /channels/{id}/messages "
                "subscriptions for RSC apps in private/shared channels)",
                team_id,
                channel_id,
            )
            return

        # Track the thread and extend its TTL to 7 days
        thread_key = _compute_thread_key(inbound)
        if thread_key:
            await _mark_private_thread(team_id, channel_id, thread_key)
            await _extend_thread_ttl(thread_key)
            logger.info(
                "Private channel monitoring ACTIVE: sub_id=%s, thread=%s "
                "(follow-ups without @mention will be processed for %d days)",
                sub_id,
                thread_key,
                PRIV_THREAD_TTL // 86_400,
            )
        else:
            logger.warning(
                "Private channel monitoring: subscription %s created but the "
                "inbound conversation id %r carries no ';messageid=' thread "
                "root — thread not tracked, follow-ups will be skipped by "
                "the active-thread gate",
                sub_id,
                inbound.channel_thread_id,
            )

    except Exception as e:
        logger.error(
            "Private channel monitoring setup error: %s",
            format_exception_message(e),
            exc_info=True,
        )


# ---------------------------------------------------------------------------
# Graph change-notification endpoint (private/shared channel messages)
# ---------------------------------------------------------------------------


@router.post("/teams/graph-notifications")
async def teams_graph_notifications(request: Request):
    """Receive Graph API change and lifecycle notifications for private/shared channels.

    Microsoft Graph sends a ``validationToken`` query param when a subscription
    is created, to its notification URL and to its lifecycle URL alike.
    Notifications arrive as POST bodies with a ``value`` array. Anything but a
    2xx only makes Graph retry for hours, so every POST within the size bound
    is acknowledged, refused entries included. Each entry is decided
    (``_decide_notifications``) before a task is queued for any: a refused one
    never becomes one. Two answers are not 2xx: 413 past
    ``GRAPH_NOTIFICATION_MAX_BODY_BYTES``, and 503 when Redis cannot be read to
    decide - nothing is queued, and Graph delivers the batch again. From
    ``GRAPH_NOTIFICATION_OFF_LOOP_BYTES`` the body is parsed and decided in a
    worker process (``teams_graph_worker``), the Redis reads staying here.
    """
    # Subscription validation handshake
    validation = request.query_params.get("validationToken")
    if validation:
        logger.info(
            "Teams graph-notifications: subscription validation handshake "
            "received"
        )
        return PlainTextResponse(content=validation, media_type="text/plain")

    raw_body = await read_bounded_body(request, GRAPH_NOTIFICATION_MAX_BODY_BYTES)
    if raw_body is None:
        log_rate_limited(
            "teams-graph-too-large",
            logging.WARNING,
            "Teams graph-notifications: POST larger than %d bytes refused (413)",
            GRAPH_NOTIFICATION_MAX_BODY_BYTES,
        )
        return Response(status_code=413)
    # The lifecycle URL names the channel (see ``_create_channel_subscription``).
    team_id = request.query_params.get("team")
    channel_id = request.query_params.get("channel")
    off_loop = len(raw_body) >= GRAPH_NOTIFICATION_OFF_LOOP_BYTES
    count, channel_keys, candidates, ignored = await _cpu_step(
        off_loop, summarize_post, raw_body, team_id, channel_id
    )
    if count is None:
        logger.warning(
            "Teams graph-notifications: POST with unparseable JSON body — "
            "ignoring"
        )
        return Response(status_code=200)

    if not count:
        logger.info(
            "Teams graph-notifications: POST received with no notifications "
            "in body"
        )
        return Response(status_code=202)

    logger.info(
        "Teams graph-notifications: %d notification(s) received",
        count,
    )

    try:
        decision = await _decide_notifications(
            raw_body, team_id, channel_id, channel_keys, candidates, ignored, off_loop=off_loop
        )
    except Exception as exc:
        # Nothing is queued yet: answered 503, Graph delivers the batch again
        # and it is decided once Redis answers.
        log_rate_limited(
            "teams-graph-undecided",
            logging.WARNING,
            "Teams graph-notifications: POST answered 503, its notifications could not be checked against "
            "their subscriptions (%s); Graph delivers it again",
            format_exception_message(exc),
        )
        return Response(status_code=503)
    if decision.refused:
        log_rate_limited(
            "teams-graph-refused",
            logging.WARNING,
            "Teams channel Graph notification(s) refused on %s: %d of the %d in one POST prove no "
            "subscription recorded for their channel",
            _GRAPH_NOTIFICATION_PATH,
            decision.refused,
            count,
        )
    if decision.ignored:
        log_rate_limited(
            "teams-graph-ignored",
            logging.INFO,
            "Teams graph-notifications: %d of the %d notification(s) in one POST ignored - not a new "
            "channel message or reply",
            decision.ignored,
            count,
        )
    for notif in decision.accepted:
        if "lifecycleEvent" in notif:
            spawn_channel_task(
                _process_graph_lifecycle_notification(notif, team_id, channel_id),
                name="teams-graph-lifecycle-notification",
            )
        else:
            spawn_channel_task(
                _process_graph_notification(notif), name="teams-graph-notification"
            )

    return Response(status_code=202)


async def _cpu_step[T](off_loop: bool, fn: Callable[..., T], /, *args) -> T:
    """``fn(*args)``, in the worker process when *off_loop* (``run_decision``)."""
    return await run_decision(fn, *args) if off_loop else fn(*args)


async def _decide_notifications(
    raw_body: bytes,
    lifecycle_team: str | None,
    lifecycle_channel: str | None,
    channel_keys: list[str],
    candidates: int,
    ignored: int,
    *,
    off_loop: bool = False,
) -> _BatchDecision:
    """Decide every entry of a batch before any task is queued for one.

    A change notification is decided by ``_notification_verdict``, as in
    ``_graph_notification_accepted``: acted on when proven, or unverified while
    verification is not required (``verification_required()``). A lifecycle
    notification must prove the channel's current subscription
    (``_lifecycle_notification_proven``). The channels the batch names are read
    in chunked MGETs and each record decrypted once (``_IssuedStates``), so a
    forged batch costs one read per channel whatever its number of entries.
    An accepted change notification naming a message another entry already
    named is queued once: each would read that message from Graph before its
    once-per-message mark (``_first_delivery``). The task queued for an entry
    decides again against the record as it is by then, and reports one acted
    on unverified.

    As in ``_graph_notification_accepted``, a refused change notification on a
    monitored channel whose record is missing or can prove nothing marks the
    channel's notification gap, so its replies are read back from Graph once a
    subscription exists again. A Redis failure, a gap that cannot be recorded
    included, raises before anything is queued.

    *channel_keys*, *candidates* and *ignored* are what ``summarize_post``
    returned for *raw_body*; ``decide_post`` reads the batch again from it.
    With *off_loop* both run in the worker process, the Redis reads and the
    gap markers here between them, and only the records that exist cross over.
    """
    if not channel_keys:
        return _BatchDecision([], candidates, ignored)

    from app.core.redis import redis_client

    redis = await redis_client()
    records: dict[str, Any] = {}
    for start in range(0, len(channel_keys), GRAPH_RECORD_FETCH_CHUNK):
        chunk = channel_keys[start : start + GRAPH_RECORD_FETCH_CHUNK]
        records.update((key, raw) for key, raw in zip(chunk, await redis.mget(chunk), strict=True) if raw)
    decision, pending = await _cpu_step(
        off_loop, decide_post, raw_body, lifecycle_team, lifecycle_channel, records, verification_required()
    )
    if pending:
        since = datetime.now(UTC) - _GRAPH_NOTIFICATION_MAX_LATENCY
        for start in range(0, len(pending), GRAPH_RECORD_FETCH_CHUNK):
            chunk = pending[start : start + GRAPH_RECORD_FETCH_CHUNK]
            monitored = await redis.smismember("teams:priv_ch_subs", [f"{t}:{c}" for t, c in chunk])
            for (t, c), is_monitored in zip(chunk, monitored, strict=True):
                if is_monitored:
                    await _mark_notification_gap(redis, t, c, since, best_effort=False)
    return decision


async def _first_delivery(team_id: str, channel_id: str, message_id: str) -> bool:
    """Whether a channel message reaches processing for the first time (fails open).

    Shared by notifications (Graph delivers at least once) and the catch-up,
    so a message both notified and read back is processed once.
    """
    from app.core.redis import redis_client

    try:
        redis = await redis_client()
        key = f"teams:graph_msg_seen:{team_id}:{channel_id}:{message_id}"
        return bool(await redis.set(key, "1", nx=True, ex=_NOTIFICATION_SEEN_TTL))
    except Exception as exc:
        logger.warning("Graph notification dedup check failed (processing it): %s", format_exception_message(exc))
        return True


async def _process_graph_notification(notif: dict) -> None:
    """Process a single Graph change notification for a private channel message."""
    try:
        resource = notif.get("resource")
        if notif.get("changeType") != "created" or not isinstance(resource, str):
            return

        # Parse resource → (team_id, channel_id, message_id); a reply is
        # named under its thread's root.
        team_id, channel_id, message_id, reply_id = _parse_graph_resource(resource)
        root_id = message_id if reply_id else None
        message_id = reply_id or message_id
        if not all([team_id, channel_id, message_id]):
            logger.warning(
                "Graph notification: could not parse resource=%r — skipping",
                resource[:200],
            )
            return

        # The notification endpoint is public: a notification is checked
        # against the subscription recorded for its channel before the message
        # is fetched or anything runs (``_graph_notification_accepted``).
        if not await _graph_notification_accepted(notif, team_id, channel_id):
            logger.warning(
                "Graph notification rejected: it does not prove the subscription "
                "recorded for team=%r",
                team_id,
            )
            return

        # Acquire Graph token for reading the message
        graph_creds = await _get_teams_graph_creds()
        if not graph_creds:
            return
        graph_token, app_id, tenant_id = graph_creds

        # Fetch the full message from Graph API
        msg = await _fetch_graph_message(
            graph_token,
            team_id,
            channel_id,
            message_id,
            root_id,
        )
        if not msg:
            return
        # The thread is the one Teams says the reply belongs to, never one
        # the notification names.
        if root_id is not None and msg.get("replyToId") != root_id:
            log_rate_limited(
                "teams-graph-reply-thread",
                logging.WARNING,
                "Teams channel Graph notification skipped: the message Graph returned does not reply to the "
                "thread the notification names",
            )
            return
        # Marked only once the message is read: a failed read, or a
        # notification naming a message that is not there, leaves the genuine
        # delivery free to be processed.
        if not await _first_delivery(team_id, channel_id, message_id):
            logger.info(
                "Graph notification: %s already processed — duplicate delivery ignored",
                resource[:200],
            )
            return

        logger.info("Graph notification: processing — resource=%s", resource[:200])
        await _route_graph_channel_message(msg, team_id, channel_id, message_id, graph_token, app_id, tenant_id)

    except Exception as e:
        logger.error(
            "Graph notification processing error: %s",
            format_exception_message(e),
            exc_info=True,
        )


async def _route_graph_channel_message(
    msg: dict,
    team_id: str,
    channel_id: str,
    message_id: str,
    graph_token: str,
    app_id: str,
    tenant_id: str,
) -> None:
    """Act on a channel message read from Graph: a follow-up in a monitored thread is answered."""
    from app.core.redis import redis_client as _redis_client

    # Skip messages sent by the bot itself (GUIDs compare
    # case-insensitively — Graph and Entra ID may differ in casing).
    # A system event message (a member added, a pin) has ``"from": null``.
    from_app = (msg.get("from") or {}).get("application") or {}
    if (from_app.get("id") or "").lower() == app_id.lower():
        return

    # Skip if the bot is @mentioned — Bot Framework handles those
    if _graph_message_mentions_bot(msg, app_id):
        logger.info(
            "Graph notification: skipping message %s — bot is @mentioned (Bot Framework handles it)",
            message_id,
        )
        return

    # Only process replies (messages with replyToId).  Root-level
    # messages in the channel are new conversations, not follow-ups
    # in a monitored thread.
    reply_to_id = msg.get("replyToId")
    if not reply_to_id:
        logger.info(
            "Graph notification: message %s is a root-level channel "
            "post (no replyToId) — not a monitored-thread follow-up, "
            "skipping",
            message_id,
        )
        return

    # A reply another bot or app posted: answering it could make two bots
    # answer each other for as long as the thread is monitored (Bot
    # Framework never delivers another bot's message either).
    sender = msg.get("from") or {}
    if sender.get("application") and not sender.get("user"):
        logger.info("Graph notification: skipping reply %s - posted by an application, not a person", message_id)
        return

    # Build the thread key in the same format as Bot Framework
    thread_key = f"{channel_id};messageid={reply_to_id}"

    # Check if this thread has been muted via /stop
    from app.channels.router import _is_thread_muted

    if await _is_thread_muted(ChannelType.TEAMS, thread_key):
        logger.info(
            "Graph notification: thread %s is muted — skipping",
            thread_key,
        )
        return

    # Check if this thread is actively monitored
    redis = await _redis_client()
    active_key = f"active_thread:teams:{thread_key}"
    if not await redis.exists(active_key):
        logger.info(
            "Graph notification: thread %s not active (no prior bot "
            "response tracked in this thread) — skipping",
            thread_key,
        )
        return

    # Extract plain text + file attachments from the message.  A file
    # shared without any text is still actionable.
    text = _extract_text_from_graph_message(msg)
    file_attachments = _extract_graph_message_attachments(msg)
    if file_attachments:
        _apply_graph_attachment_auth_headers(file_attachments, graph_token)
        logger.info(
            "Graph notification: %d file attachment(s) detected",
            len(file_attachments),
        )
    if not text:
        if file_attachments:
            text = _FILE_ONLY_PLACEHOLDER
        else:
            return

    # Resolve sender info
    sender = (msg.get("from") or {}).get("user") or {}
    user_email = sender.get("email")
    user_id = sender.get("id", "")
    user_name = sender.get("displayName", "unknown")

    if not user_email and user_id:
        user_email = await _resolve_graph_user_email(graph_token, user_id)

    logger.info(
        "Graph notification: processing private channel message — team=%s, channel=%s, msg=%s, from=%s, thread=%s",
        team_id,
        channel_id,
        message_id,
        user_id,
        reply_to_id,
    )

    # Look up the cached Bot Framework service URL for this tenant
    service_url = await redis.get(f"teams:service_url:{tenant_id}")
    if not service_url:
        service_url = "https://smba.trafficmanager.net/amer/"

    g_team_name, g_channel_name = await _fetch_graph_channel_names(
        graph_token,
        team_id,
        channel_id,
    )

    # Build an InboundMessage identical to what Bot Framework produces
    conversation_id = thread_key  # includes ;messageid=<root>
    inbound = InboundMessage(
        channel=ChannelType.TEAMS,
        text=text,
        user_email=user_email,
        channel_user_id=user_id,
        channel_thread_id=conversation_id,
        channel_message_id=message_id,
        file_attachments=file_attachments,
        raw={},
        metadata={
            "service_url": service_url,
            "tenant_id": tenant_id,
            "user_aad_id": user_id,
            "conversation_type": "channel",
            "bot_was_mentioned": False,
            "team_name": g_team_name,
            "channel_name": g_channel_name,
            "contact_name": user_name,
            "reply_to_id": reply_to_id,
            "source": "graph_subscription",
        },
    )

    # Get Bot Framework credentials for sending the reply
    bf_creds = await _get_teams_creds()
    if not bf_creds:
        logger.error("Graph notification: no Bot Framework creds for reply")
        return
    bf_token, bot_app_id = bf_creds

    channel = TeamsChannel(
        service_url,
        conversation_id,
        bf_token,
        bot_id=bot_app_id,
        conversation_type="channel",
    )
    async with typing_keepalive(channel):
        outbound = await handle_message(inbound)

    if outbound.suppress:
        logger.info(
            "Graph notification: response suppressed for message %s",
            message_id,
        )
        return

    msg_id = await channel.send_message(outbound)
    # ``""`` = accepted asynchronously (HTTP 202, the norm for private/
    # shared channels routed via Graph) — still a success, so the
    # monitoring window must be refreshed.
    if msg_id is not None:
        logger.info(
            "Graph notification: reply sent — activity_id=%s",
            msg_id or "(accepted asynchronously)",
        )
        # Refresh the 7-day monitoring window
        await _extend_thread_ttl(thread_key)
        await _mark_private_thread(team_id, channel_id, thread_key)
    else:
        logger.error("Graph notification: failed to send reply")


async def _process_graph_lifecycle_notification(
    notif: dict, team_id: str | None, channel_id: str | None
) -> None:
    """Answer a lifecycle notification about a channel's current subscription.

    ``reauthorizationRequired``: renew it, or Graph eventually pauses its
    notifications. ``subscriptionRemoved``: create a new one. ``missed`` (not
    sent for channel messages) is only logged. The channel comes from the
    query of the lifecycle URL, and the notification must prove that
    channel's subscription like any other.
    """
    event = notif.get("lifecycleEvent")
    try:
        if not (team_id and channel_id):
            logger.warning("Graph lifecycle notification %r names no channel - ignored", event)
            return

        from app.core.redis import redis_client

        redis = await redis_client()
        sub_key = _subscription_key(team_id, channel_id)
        raw = await redis.get(sub_key)
        record = _read_subscription_record(raw) if raw else None
        sub_id = notif.get("subscriptionId")
        if not _lifecycle_notification_proven(record, notif, team_id):
            logger.warning(
                "Graph lifecycle notification rejected: it does not prove the "
                "current subscription of team=%r",
                team_id,
            )
            return
        if event not in ("reauthorizationRequired", "subscriptionRemoved"):
            logger.info("Graph lifecycle notification %r for subscription %s - nothing to do", event, sub_id)
            return

        graph_creds = await _get_teams_graph_creds()
        if not graph_creds:
            return
        graph_token, _app_id, tenant_id = graph_creds
        async with _channel_subscription_lock(redis, team_id, channel_id, wait=5) as held:
            if not held:
                return
            raw = await redis.get(sub_key)
            record = _read_subscription_record(raw) if raw else None
            if record is None or record.get("subscription_id") != sub_id:
                return  # replaced meanwhile
            if event == "reauthorizationRequired":
                throttle = f"teams:graph_sub_reauthorized:{sub_id}"
                if not await redis.set(throttle, "1", nx=True, ex=_REAUTHORIZE_THROTTLE_SECS):
                    return
                outcome = await _renew_graph_subscription(redis, sub_key, record, graph_token)
                if outcome == "failed":
                    await redis.delete(throttle)
                if outcome != "gone":
                    return
            logger.info("Graph subscription %s is gone (%s) - creating a new one", sub_id, event)
            # Nothing is notified from the removal until the new subscription:
            # Graph asks the app to fetch those changes itself.
            await _mark_notification_gap(
                redis, team_id, channel_id, datetime.now(UTC) - _GRAPH_NOTIFICATION_MAX_LATENCY
            )
            await redis.delete(sub_key)
            await _create_channel_subscription(team_id, channel_id, tenant_id, graph_token, replaces=sub_id)
    except Exception as e:
        logger.error(
            "Graph lifecycle notification processing error: %s",
            format_exception_message(e),
            exc_info=True,
        )


# ---------------------------------------------------------------------------
# Graph notification helpers
# ---------------------------------------------------------------------------


async def _fetch_graph_message(
    token: str,
    team_id: str,
    channel_id: str,
    message_id: str,
    root_id: str | None = None,
) -> dict | None:
    """Fetch a single channel message from the Graph API, a reply under its thread's root."""
    url = f"{GRAPH_API}/teams/{team_id}/channels/{channel_id}/messages/"
    url += f"{root_id}/replies/{message_id}" if root_id else message_id
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                url,
                headers={"Authorization": f"Bearer {token}"},
                timeout=15,
            )
            if resp.status_code == 200:
                return resp.json()
            logger.warning(
                "Graph message fetch failed: %s %s",
                resp.status_code,
                resp.text[:200],
            )
    except Exception as e:
        logger.warning("Graph message fetch error: %s", format_exception_message(e))
    return None


async def _fetch_graph_inbound_message(
    inbound: InboundMessage,
    graph_token: str,
) -> dict | None:
    """Fetch the Graph ``chatMessage`` behind a Bot Framework activity.

    Used as the file-attachment fallback for group chats and channels,
    where the webhook activity often carries only the ``text/html``
    file-card stub while the Graph copy of the message exposes the real
    ``reference`` attachments.

    Bot Framework activity IDs and Graph message IDs share the same
    timestamp-based value, so the activity ID can be used directly.
    """
    message_id = inbound.channel_message_id or ""
    if not message_id:
        return None

    conv_type = inbound.metadata.get("conversation_type", "")
    urls: list[str] = []

    if conv_type == "groupChat":
        # The Bot Framework conversation ID (19:…@thread.v2) IS the Graph
        # chat ID.  Requires Chat.Read.All / ChatMessage.Read.All.
        chat_id = (inbound.channel_thread_id or "").split(";")[0]
        if not chat_id:
            return None
        urls.append(f"{GRAPH_API}/chats/{chat_id}/messages/{message_id}")
    elif conv_type == "channel":
        # Requires ChannelMessage.Read.All.  Replies live under the root
        # message, so try the /replies endpoint first when applicable.
        team_id = inbound.metadata.get("team_id") or ""
        channel_id = inbound.metadata.get("channel_id") or (
            inbound.channel_thread_id or ""
        ).split(";")[0]
        if not team_id or not channel_id:
            return None
        base = f"{GRAPH_API}/teams/{team_id}/channels/{channel_id}/messages"
        reply_to = inbound.metadata.get("reply_to_id")
        if reply_to and reply_to != message_id:
            urls.append(f"{base}/{reply_to}/replies/{message_id}")
        urls.append(f"{base}/{message_id}")
    else:
        return None

    for url in urls:
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(
                    url,
                    headers={"Authorization": f"Bearer {graph_token}"},
                    timeout=15,
                )
            if resp.status_code == 200:
                return resp.json()
            logger.debug(
                "Graph inbound message fetch: %s → %s",
                url,
                resp.status_code,
            )
        except Exception as e:
            logger.debug("Graph inbound message fetch error: %s", format_exception_message(e))
    return None


def _graph_message_mentions_bot(msg: dict, app_id: str) -> bool:
    """Return True when the Graph ``chatMessage`` @mentions the bot itself.

    Bot mentions resolve on the Graph copy as
    ``mentions[].mentioned.application.id == <bot app id>``; GUIDs compare
    case-insensitively.  Single source of truth for Graph-side bot-mention
    detection — used by the RSC mention-detection fallback in
    ``_process_teams_message`` (when receive-all RSC permissions are
    granted, the Bot Framework activity for an @mention in a group chat /
    channel can arrive without any mention entity, see
    MicrosoftDocs/msteams-docs#14422, so the Graph copy is the only
    reliable way to tell the bot was addressed) and by
    ``_process_graph_notification`` (to skip Bot Framework-handled
    messages in monitored private/shared channel threads).
    """
    if not app_id:
        return False
    for mention in msg.get("mentions") or []:
        application = ((mention.get("mentioned") or {}).get("application")) or {}
        if (application.get("id") or "").lower() == app_id.lower():
            return True
    return False


def _extract_text_from_graph_message(msg: dict) -> str:
    """Extract plain text from a Graph ``chatMessage``, stripping HTML."""
    body = msg.get("body", {})
    content = body.get("content", "")
    content_type = body.get("contentType", "text")

    if content_type == "html":
        text = re.sub(r"<[^>]+>", "", content)
        text = (
            text.replace("&amp;", "&")
            .replace("&lt;", "<")
            .replace("&gt;", ">")
            .replace("&nbsp;", " ")
            .replace("&#8203;", "")
        )
        return text.strip()

    return content.strip()


async def _resolve_graph_user_email(
    token: str,
    user_id: str,
) -> str | None:
    """Resolve a user's email from their AAD ID via the Graph ``/users`` API."""
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{GRAPH_API}/users/{user_id}",
                headers={"Authorization": f"Bearer {token}"},
                params={"$select": "mail,userPrincipalName"},
                timeout=10,
            )
            if resp.status_code == 200:
                data = resp.json()
                return data.get("mail") or data.get("userPrincipalName")
    except Exception as e:
        logger.warning("Graph user email resolution failed: %s", format_exception_message(e))
    return None


async def _fetch_graph_channel_names(
    token: str,
    team_id: str,
    channel_id: str,
) -> tuple[str, str]:
    """Return ``(team_name, channel_name)`` for a Teams channel via Graph API.

    Results are cached in Redis for 24 h to avoid repeated calls.
    Returns ``("", "")`` on any failure.
    """
    from app.core.redis import redis_client

    redis = await redis_client()
    cache_key = f"teams:channel_names:{team_id}:{channel_id}"
    cached = await redis.get(cache_key)
    if cached:
        parts = cached.split("||", 1)
        return (parts[0], parts[1]) if len(parts) == 2 else ("", "")

    team_name = ""
    channel_name = ""
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{GRAPH_API}/teams/{team_id}/channels/{channel_id}",
                headers={"Authorization": f"Bearer {token}"},
                params={"$select": "displayName"},
                timeout=10,
            )
            if resp.status_code == 200:
                channel_name = resp.json().get("displayName", "")
            resp2 = await client.get(
                f"{GRAPH_API}/teams/{team_id}",
                headers={"Authorization": f"Bearer {token}"},
                params={"$select": "displayName"},
                timeout=10,
            )
            if resp2.status_code == 200:
                team_name = resp2.json().get("displayName", "")
    except Exception as e:
        logger.warning("Graph channel name resolution failed: %s", format_exception_message(e))

    await redis.set(cache_key, f"{team_name}||{channel_name}", ex=86400)
    return team_name, channel_name


# ---------------------------------------------------------------------------
# Subscription renewal & stale-thread cleanup (called by autonomous scheduler)
# ---------------------------------------------------------------------------


async def renew_private_channel_subscriptions() -> int:
    """Renew Graph subscriptions and clean up stale private-channel threads.

    Called periodically by the autonomous scheduler.
    Returns the number of actions taken (renewals + cleanups).
    """
    from app.core.redis import redis_client

    redis = await redis_client()
    actions = 0

    channels = await redis.smembers("teams:priv_ch_subs")
    if not channels:
        return 0

    graph_creds = await _get_teams_graph_creds()
    if not graph_creds:
        return 0
    graph_token, _app_id, tenant_id = graph_creds

    bf_creds = await _get_teams_creds()
    now = datetime.now(UTC).timestamp()

    for ch_key_raw in channels:
        ch_key = ch_key_raw
        try:
            team_id, channel_id = ch_key.split(":", 1)
        except ValueError:
            await redis.srem("teams:priv_ch_subs", ch_key_raw)
            continue

        sub_key = _subscription_key(team_id, channel_id)
        threads_key = f"teams:priv_threads:{team_id}:{channel_id}"

        # Safety-net: remove orphaned set entries with no subscription and no threads
        if not await redis.exists(sub_key) and not await redis.exists(threads_key):
            await redis.srem("teams:priv_ch_subs", ch_key)
            continue

        try:
            # Passes can overlap (the scheduler's window marker lasts 55 s): a
            # channel another worker is maintaining is left to it.
            async with _channel_subscription_lock(redis, team_id, channel_id) as held:
                if held:
                    actions += await _maintain_channel_subscription(
                        redis, ch_key, team_id, channel_id, tenant_id, graph_token, bf_creds, now
                    )
        except Exception as e:
            logger.warning(
                "Subscription renewal error for %s: %s",
                ch_key,
                format_exception_message(e),
            )

    return actions


def _hours_left(record: dict) -> float:
    """Hours until the recorded subscription expires; 0 when the record cannot say."""
    try:
        return (datetime.fromisoformat(record["expires_at"]) - datetime.now(UTC)).total_seconds() / 3600
    except (KeyError, TypeError, ValueError):
        return 0.0


async def _maintain_channel_subscription(
    redis: Any,
    ch_key: str,
    team_id: str,
    channel_id: str,
    tenant_id: str,
    graph_token: str,
    bf_creds: tuple[str, str] | None,
    now: float,
) -> int:
    """One channel's turn in the renewal pass; returns the number of actions."""
    actions = 0
    sub_key = _subscription_key(team_id, channel_id)
    threads_key = f"teams:priv_threads:{team_id}:{channel_id}"

    # ── Clean up stale threads (inactive > 7 days) ───────────────
    stale_cutoff = now - PRIV_THREAD_TTL
    stale_threads = await redis.zrangebyscore(
        threads_key,
        "-inf",
        stale_cutoff,
    )
    for raw_tk in stale_threads:
        thread_key = raw_tk
        # Send "stopped monitoring" notice to the thread
        if bf_creds:
            await _send_monitoring_stopped(thread_key, bf_creds, tenant_id)
        await redis.zrem(threads_key, thread_key)
        await redis.delete(f"active_thread:teams:{thread_key}")
        actions += 1
        logger.info(
            "Cleaned up stale private thread: %s",
            thread_key,
        )

    sub_raw = await redis.get(sub_key)
    record = _read_subscription_record(sub_raw) if sub_raw else None

    # ── If no threads remain, delete the subscription ─────────────
    if await redis.zcard(threads_key) == 0:
        sub_id = record.get("subscription_id") if record else None
        if isinstance(sub_id, str) and sub_id:
            if not await _delete_graph_subscription(sub_id, graph_token):
                # Still live: the record and the channel stay so the next
                # pass deletes it, instead of leaving it untracked.
                return actions
            actions += 1
        if sub_raw:
            await redis.delete(sub_key)
        await redis.srem("teams:priv_ch_subs", ch_key)
        logger.info(
            "Removed subscription for channel %s (no active threads)",
            ch_key,
        )
        return actions

    # ── Recreate a subscription whose record expired or is unreadable ─
    if record is None:
        if sub_raw:
            await redis.delete(sub_key)
        await _create_channel_subscription(team_id, channel_id, tenant_id, graph_token)
        return actions + 1

    # ── Bring a record from an earlier release to the current form ──
    if _prune_replaced_subscriptions(record):
        await _write_subscription_record(redis, sub_key, record)
    state = _record_state(record, team_id)
    if state == "plaintext":
        # Graph cannot change a clientState: only how it is stored changes.
        record["client_state_enc"] = encrypt_value(record.pop("client_state"))
        await _write_subscription_record(redis, sub_key, record)
    elif state != "current" and verification_required():
        if await _replace_channel_subscription(
            redis, sub_key, record, team_id, channel_id, tenant_id, graph_token
        ):
            return actions + 1
        # It could not be deleted, so no other can exist: it keeps notifying,
        # and the next pass tries again.
    # Otherwise a derivable or unreadable clientState is kept and renewed as
    # before: deleting the subscription could silence the channel if Graph
    # then refused the new one.
    sub_id = record.get("subscription_id")
    if not (isinstance(sub_id, str) and sub_id):
        return actions  # names no subscription to renew: it expires with its record

    # ── Renew the subscription if close to expiring ───────────────
    if _hours_left(record) < 12:
        outcome = await _renew_graph_subscription(redis, sub_key, record, graph_token)
        if outcome == "gone":
            # Graph removed it without telling us when (an older subscription
            # has no lifecycle notifications): read back as far as a catch-up goes.
            await _mark_notification_gap(redis, team_id, channel_id, datetime.now(UTC) - _CATCH_UP_MAX_LOOKBACK)
            await redis.delete(sub_key)
            await _create_channel_subscription(team_id, channel_id, tenant_id, graph_token, replaces=sub_id)
            return actions + 1
        if outcome == "failed":
            # Kept with its record: while it exists no other can be created,
            # and the next pass retries. The catch-up waits for one that notifies.
            return actions
        actions += 1
    # A gap left by a refused notification, or a catch-up cut short by its
    # time budget, is read back now that the subscription notifies again.
    if await redis.exists(_gap_key(team_id, channel_id)):
        await _catch_up_channel(redis, team_id, channel_id, graph_token)
    return actions


async def _send_monitoring_stopped(
    thread_key: str,
    bf_creds: tuple[str, str],
    tenant_id: str,
) -> None:
    """Post a notice in a thread that the bot has stopped monitoring it."""
    from app.core.redis import redis_client

    try:
        token, bot_app_id = bf_creds
        redis = await redis_client()
        service_url = await redis.get(f"teams:service_url:{tenant_id}")
        if not service_url:
            service_url = "https://smba.trafficmanager.net/amer/"
        channel = TeamsChannel(
            service_url,
            thread_key,
            token,
            bot_id=bot_app_id,
        )
        msg = OutboundMessage(
            text=notice("teams_monitoring_stopped", notice_language()),
        )
        await channel.send_message(msg)
    except Exception as e:
        logger.warning("Failed to send monitoring-stopped message: %s", format_exception_message(e))
