# SPDX-License-Identifier: Apache-2.0

"""WhatsApp messages received for a ``whatsapp`` integration.

Meta posts every notification of an app to one callback URL
(``/api/channels/whatsapp/webhook``). The channel adapter asks
:func:`integration_for_phone_number` whether the ``phone_number_id`` of a
change belongs to a connected ``whatsapp`` integration: if so the change is
verified with that integration's ``app_secret`` and its messages are recorded
here (:func:`record_change`) instead of being answered by the ``whatsapp_bot``
channel bot. The integration's ``list_whatsapp_conversations`` and
``read_whatsapp_messages`` tools read them back, and every newly stored
message is handed to the WhatsApp agent accounts on the integration.

A message WhatsApp gives no content for (a member's poll, any ``unsupported``
message) is recorded in words, so the agent knows it happened. Rows older than
the integration's ``inbound_retention_days`` (extra field, 30 by default,
1 to 365) are purged on every write.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.core.database import async_session
from app.core.utils import format_exception_message
from app.models.integration import Integration
from app.models.whatsapp_inbound_message import WhatsAppInboundMessage
from app.services.vault import reveal_json_field

logger = logging.getLogger(__name__)

PROVIDER = "whatsapp"
DEFAULT_RETENTION_DAYS = 30
MAX_RETENTION_DAYS = 365
_MEDIA_TYPES = ("image", "document", "audio", "video", "sticker")
_MAX_TEXT = 8000
POLL_WORDS = (
    "[A member posted or answered a WhatsApp poll; its question, options and votes are "
    "not available through the WhatsApp Cloud API.]"
)
UNSUPPORTED_WORDS = "[A member sent a WhatsApp message type the Cloud API does not deliver ({kind}).]"


#: ``chat_type`` of the paired number's own "message yourself" chat (gateway backend):
#: what the owner writes to themself. Stored like any chat; it reaches an agent only
#: when the integration's ``self_chat_agent_id`` names one.
SELF_CHAT = "self"
#: Extra field naming the agent the self-chat routes to (empty: off, the default).
SELF_CHAT_AGENT_FIELD = "self_chat_agent_id"


@dataclass(frozen=True, slots=True)
class InboundIntegration:
    """The ``whatsapp`` integration a business number belongs to, with its webhook secrets."""

    integration_id: uuid.UUID
    app_secret: str
    verify_token: str
    retention_days: int


def retention_days(extra: dict[str, Any]) -> int:
    """``inbound_retention_days`` of an integration's extra config, bounded; the default when unset or invalid."""
    try:
        days = int(str(extra.get("inbound_retention_days") or DEFAULT_RETENTION_DAYS).strip())
    except ValueError:
        return DEFAULT_RETENTION_DAYS
    return max(1, min(days, MAX_RETENTION_DAYS))


async def _connected_integrations() -> list[tuple[Integration, dict[str, Any]]]:
    """The connected ``whatsapp`` integrations with their decrypted extra config; empty on a database error."""
    try:
        async with async_session() as db:
            rows = (
                (
                    await db.execute(
                        select(Integration).where(Integration.provider == PROVIDER, Integration.status == "connected")
                    )
                )
                .scalars()
                .all()
            )
    except Exception as exc:
        logger.error("WhatsApp integrations not read: %s", format_exception_message(exc))
        return []
    found = []
    for integ in rows:
        try:
            raw = await reveal_json_field(integ, "extra_config_enc") if integ.extra_config_enc else {}
        except Exception as exc:
            logger.warning(
                "WhatsApp integration %s: extra config unreadable: %s", integ.id, format_exception_message(exc)
            )
            continue
        found.append((integ, raw if isinstance(raw, dict) else {}))
    return found


async def integration_for_phone_number(phone_number_id: str) -> InboundIntegration | None:
    """The connected ``whatsapp`` integration that operates *phone_number_id*, or ``None``.

    ``None`` leaves the notification to the ``whatsapp_bot`` channel bot.
    """
    phone_number_id = (phone_number_id or "").strip()
    if not phone_number_id:
        return None
    for integ, extra in await _connected_integrations():
        if str(extra.get("phone_number_id") or "").strip() == phone_number_id:
            return InboundIntegration(
                integration_id=integ.id,
                app_secret=str(extra.get("app_secret") or ""),
                verify_token=str(extra.get("verify_token") or ""),
                retention_days=retention_days(extra),
            )
    return None


async def verify_tokens() -> list[str]:
    """The non-empty ``verify_token`` of every connected ``whatsapp`` integration (GET verification)."""
    return [token for _, extra in await _connected_integrations() if (token := str(extra.get("verify_token") or ""))]


def message_row(msg: dict[str, Any], contacts: list[Any]) -> dict[str, Any] | None:
    """The stored fields of one webhook message, or ``None`` for one without an id or sender.

    Text and captions are kept; a poll and any ``unsupported`` message are
    recorded in words; a reaction or an interactive reply is recorded as what
    it says.
    """
    if not isinstance(msg, dict):
        return None
    wamid = str(msg.get("id") or "").strip()
    sender = str(msg.get("from") or "").strip()
    if not wamid or not sender:
        return None
    kind = str(msg.get("type") or "unknown")
    group_id = msg.get("group_id") or (msg.get("context") or {}).get("group_id")
    media_id: str | None = None
    body = msg.get(kind) if isinstance(msg.get(kind), dict) else {}
    if kind == "text":
        text = str(body.get("body") or "")
    elif kind in _MEDIA_TYPES:
        text = str(body.get("caption") or body.get("filename") or "")
        candidate = str(body.get("id") or "")
        media_id = candidate if candidate.isascii() and candidate.isdigit() else None
        text = f"[{kind}{' ' + media_id if media_id else ''}] {text}".strip()
    elif kind == "reaction":
        text = f"[reaction {body.get('emoji') or '(removed)'} to {body.get('message_id') or '?'}]"
    elif kind == "interactive":
        reply = body.get("button_reply") or body.get("list_reply") or {}
        text = f"[choice] {reply.get('title') or reply.get('id') or ''}".strip()
    elif kind == "button":
        text = f"[choice] {body.get('text') or body.get('payload') or ''}".strip()
    elif kind == "unsupported":
        sub = str(body.get("type") or "")
        errors = " ".join(str(e.get("title") or "") for e in msg.get("errors") or [] if isinstance(e, dict))
        text = POLL_WORDS if "poll" in f"{sub} {errors}".lower() else UNSUPPORTED_WORDS.format(kind=sub or "unknown")
    else:
        text = UNSUPPORTED_WORDS.format(kind=kind)
    name = ""
    for contact in contacts or []:
        if isinstance(contact, dict) and contact.get("wa_id") == sender:
            name = str((contact.get("profile") or {}).get("name") or "")
            break
    try:
        received = datetime.fromtimestamp(int(str(msg.get("timestamp") or "")), UTC)
    except ValueError:
        received = datetime.now(UTC)
    return {
        "wamid": wamid[:255],
        "conversation_id": str(group_id or sender)[:255],
        "chat_type": "group" if group_id else "private",
        "sender": sender[:64],
        "sender_name": name[:255] or None,
        "message_type": kind[:32],
        "text": text[:_MAX_TEXT],
        "media_id": media_id,
        "received_at": received,
    }


async def record_change(target: InboundIntegration, value: dict[str, Any]) -> int:
    """Store the messages of one webhook change for *target*; returns the rows written. Never raises.

    A voice note or audio (``audio``) is transcribed before it is stored, so
    ``read_whatsapp_messages`` and the agent accounts read what was said.
    """
    contacts = value.get("contacts") or []
    rows = [row for msg in value.get("messages") or [] if (row := message_row(msg, contacts))]
    await transcribe_audio_rows(target, rows)
    return await record_rows(target, rows)


async def _cloud_api_hearing(integration_id: uuid.UUID) -> tuple[str, uuid.UUID | None]:
    """The integration's access token (a vault reference resolved) and owner; empty token when unreadable."""
    from app.services.vault import reveal_field

    async with async_session() as db:
        integ = await db.get(Integration, integration_id)
    if integ is None:
        return "", None
    token = (await reveal_field(integ, "api_key_enc") or "") if integ.api_key_enc else ""
    return token, integ.user_id


async def _hear_one(token: str, owner_id: uuid.UUID | None, media_id: str) -> str:
    """The turn line of one Cloud API voice note: its transcript, or why there is none. Never raises."""
    from app.integrations.providers.whatsapp import download_media_bytes
    from app.llm.transcription import MAX_AUDIO_BYTES, TranscriptionUnavailableError, resolve_transcriber
    from app.services.agent_accounts import voice_notes

    if owner_id is None:
        return voice_notes.failed_line("the integration has no owner to transcribe for")
    audio, err = await download_media_bytes(token, media_id, MAX_AUDIO_BYTES)
    if err or not audio:
        return voice_notes.failed_line(f"download failed ({err or 'empty file'})")
    try:
        async with async_session() as db:
            transcriber = await resolve_transcriber(db, owner_id)
        transcript = await transcriber.transcribe(audio, filename="voice.ogg", words=True)
    except TranscriptionUnavailableError as exc:
        return voice_notes.failed_line(str(exc))
    except Exception as exc:
        return voice_notes.failed_line(format_exception_message(exc))
    if not transcript.text.strip():
        return voice_notes.failed_line("nothing was heard", transcript.duration)
    # Judged before it is trusted: a note cut short or decoded unsure says so, never doubtful words.
    return voice_notes.judged_line(transcript, say_language=True)


async def transcribe_audio_rows(target: InboundIntegration, rows: list[dict[str, Any]]) -> int:
    """Replace the text of each Cloud API audio row by its transcript, or by why there is none. Never raises.

    The audio is downloaded with the integration's access token, transcribed
    for the integration's owner (``app.llm.transcription``) and held in memory
    only; the transcript is not logged. A caption is kept before the note.
    Returns the notes transcribed.
    """
    from app.services.agent_accounts import voice_notes

    audio_rows = [r for r in rows if r.get("message_type") == "audio" and r.get("media_id")]
    if not audio_rows:
        return 0
    try:
        token, owner_id = await _cloud_api_hearing(target.integration_id)
    except Exception as exc:
        token, owner_id = "", None
        logger.warning(
            "WhatsApp integration %s: credentials for voice notes unreadable: %s",
            target.integration_id,
            exc.__class__.__name__,
        )
    done = 0
    for row in audio_rows:
        try:
            line = await _hear_one(token, owner_id, str(row["media_id"]))
        except Exception as exc:
            line = voice_notes.failed_line(format_exception_message(exc))
        if "transcription failed" in line or "transcription low confidence" in line:
            logger.warning(
                "WhatsApp integration %s: voice note %s not transcribed", target.integration_id, row.get("wamid")
            )
        else:
            done += 1
        prefix = f"[audio {row['media_id']}]"
        caption = str(row.get("text") or "").removeprefix(prefix).strip()
        line = with_audio_reference(line, str(row["media_id"]))
        row["text"] = voice_notes.with_voice_notes(caption, [line])[:_MAX_TEXT]
    return done


#: Longest media id the store keeps (``WhatsAppInboundMessage.media_id``).
MAX_MEDIA_ID = 64


def with_audio_reference(line: str, media_id: str | None) -> str:
    """*line* followed by where the note's raw audio is, so an agent can fetch it and hear it again.

    The transcript (or the reason there is none) stays the line; the audio is
    named by its media id for ``download_whatsapp_media`` on either backend,
    which delivers it as a file of the conversation. A note whose media id is
    unknown or longer than the store keeps is left as it is.
    """
    media_id = str(media_id or "").strip()
    if not media_id or len(media_id) > MAX_MEDIA_ID:
        return line
    return f"{line}\n[audio of this voice note: media id {media_id}, download_whatsapp_media fetches it as a file]"


async def record_rows(target: InboundIntegration, rows: list[dict[str, Any]]) -> int:
    """Store already parsed rows (any backend) for *target*; returns the rows written. Never raises.

    The one write path of the store: the Meta change above and the linked-device
    gateway webhook (``services/whatsapp_gateway_inbound``) both end here,
    so deduplication, retention and the hand-off to the agent accounts are shared.
    """
    inserted: list[dict[str, Any]] = []
    try:
        async with async_session() as db:
            written = 0
            for row in rows:
                result = await db.execute(
                    pg_insert(WhatsAppInboundMessage)
                    .values(id=uuid.uuid4(), integration_id=target.integration_id, **row)
                    .on_conflict_do_nothing(constraint="uq_whatsapp_inbound_messages_wamid")
                )
                if result.rowcount:
                    inserted.append(row)
                written += result.rowcount or 0
            cutoff = datetime.now(UTC) - timedelta(days=target.retention_days)
            await db.execute(
                delete(WhatsAppInboundMessage).where(
                    WhatsAppInboundMessage.integration_id == target.integration_id,
                    WhatsAppInboundMessage.received_at < cutoff,
                )
            )
            await db.commit()
    except Exception as exc:
        logger.error(
            "WhatsApp integration %s: inbound messages not stored: %s",
            target.integration_id,
            format_exception_message(exc),
        )
        return 0
    # The agent accounts on the integration hear what was newly stored:
    # a redelivered message is not inserted again, so never answered twice.
    if inserted:
        from app.services.agent_accounts.providers.whatsapp import dispatch_stored

        await dispatch_stored(target.integration_id, inserted)
    return written


async def conversations_text(integration_id: str, limit: int, empty: str) -> str:
    """The answer of ``list_whatsapp_conversations`` on either backend; *empty* when nothing was received."""
    from app.services.content_safety import wrap_untrusted

    conversations = await list_conversations(integration_id, limit)
    if not conversations:
        return empty
    lines = [
        f"- {c['conversation_id']} ({c['chat_type']}): {c['messages']} message(s), last "
        f"{c['last_at']:%Y-%m-%d %H:%M} UTC from {c['last_sender']}: {c['last_text'][:200]}"
        for c in conversations
    ]
    return wrap_untrusted(f"{len(lines)} WhatsApp conversation(s):\n" + "\n".join(lines), "WhatsApp received messages")


async def messages_text(integration_id: str, conversation_id: str, limit: int) -> str:
    """The answer of ``read_whatsapp_messages`` on either backend."""
    from app.services.content_safety import wrap_untrusted

    messages = await read_messages(integration_id, conversation_id, limit)
    if not messages:
        return f"No received message in WhatsApp conversation {conversation_id}."
    lines = [
        f"[{m.received_at:%Y-%m-%d %H:%M} UTC] {m.sender_name or m.sender} ({m.sender}), message id {m.wamid}: {m.text}"
        for m in messages
    ]
    return wrap_untrusted(
        f"{len(lines)} message(s) of WhatsApp conversation {conversation_id}:\n" + "\n".join(lines),
        "WhatsApp received messages",
    )


async def list_conversations(integration_id: str, limit: int = 25) -> list[dict[str, Any]]:
    """The integration's conversations, newest first: id, type, last sender, count, last message time and text."""
    key = uuid.UUID(str(integration_id))
    limit = max(1, min(int(limit or 25), 100))
    m = WhatsAppInboundMessage
    async with async_session() as db:
        summary = (
            await db.execute(
                select(m.conversation_id, func.count(), func.max(m.received_at).label("last"))
                .where(m.integration_id == key)
                .group_by(m.conversation_id)
                .order_by(func.max(m.received_at).desc())
                .limit(limit)
            )
        ).all()
        conversations = []
        for conversation_id, count, last in summary:
            latest = (
                await db.execute(
                    select(m)
                    .where(m.integration_id == key, m.conversation_id == conversation_id)
                    .order_by(m.received_at.desc())
                    .limit(1)
                )
            ).scalar_one()
            conversations.append(
                {
                    "conversation_id": conversation_id,
                    "chat_type": latest.chat_type,
                    "messages": count,
                    "last_at": last,
                    "last_sender": latest.sender_name or latest.sender,
                    "last_text": latest.text,
                }
            )
    return conversations


async def read_messages(integration_id: str, conversation_id: str, limit: int = 50) -> list[WhatsAppInboundMessage]:
    """The latest *limit* messages of one conversation, oldest first."""
    key = uuid.UUID(str(integration_id))
    limit = max(1, min(int(limit or 50), 200))
    m = WhatsAppInboundMessage
    async with async_session() as db:
        rows = (
            (
                await db.execute(
                    select(m)
                    .where(m.integration_id == key, m.conversation_id == conversation_id.strip())
                    .order_by(m.received_at.desc())
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )
    return list(reversed(rows))
