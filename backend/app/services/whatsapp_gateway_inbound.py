# SPDX-License-Identifier: Apache-2.0

"""Inbound webhook of the linked-device gateway backend of ``whatsapp``.

A ``whatsapp`` integration on ``backend = linked_device_gateway`` (Whapi.Cloud
first) is given one webhook URL of its own:
``POST /api/channels/whatsapp/gateway/{integration_id}`` (``app/channels/whatsapp.py``).
The gateway posts there every message the paired number receives, every poll
update and the group events; the delivery is authenticated by the
integration's ``webhook_secret`` (extra field), sent as the ``token`` query
parameter of the URL or the ``X-Webhook-Secret`` header, compared in constant
time. No secret configured refuses every delivery: the URL alone is not a
credential.

What is stored, in the backend-agnostic store
(``services/whatsapp_inbound.record_rows``, shared with the Meta Cloud API):

- received messages: text, link previews, media captions, a poll in words
  (question and options), a group action (member added, removed...) in words;
  messages the number sent to others are not stored; what it writes in its own
  "message yourself" chat is (``chat_type = "self"``), and reaches an agent only
  when the integration's ``self_chat_agent_id`` names one
  (``agent_accounts.providers.whatsapp.dispatch_stored``);
- polls and votes in ``whatsapp_poll_tallies``: the options with their id and
  the voters of each, a vote replacing the voter's previous choice, so
  ``get_whatsapp_poll_results`` answers from the store.

Payloads are vendor-shaped; :data:`PARSERS` maps a vendor to its parser, so a
second gateway vendor is one parser added there.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, select

from app.core.database import async_session
from app.core.utils import format_exception_message
from app.models.integration import Integration
from app.models.whatsapp_poll_tally import WhatsAppPollTally
from app.services import whatsapp_inbound
from app.services.vault import reveal_json_field

logger = logging.getLogger(__name__)

BACKEND_GATEWAY = "linked_device_gateway"
SECRET_FIELD = "webhook_secret"  # noqa: S105 - the extra field's name, not a secret
_MEDIA_TYPES = frozenset({"image", "video", "gif", "short", "audio", "voice", "document", "sticker"})
_MAX_TEXT = 8000
_GROUP_SUFFIX = "@g.us"
_GROUP_ACTIONS = {
    "add": "added {who}",
    "remove": "removed {who}",
    "leave": "{who} left",
    "join": "{who} joined",
    "promote": "made {who} an admin",
    "demote": "removed {who} as admin",
    "subject": "changed the group subject",
    "description": "changed the group description",
    "icon": "changed the group icon",
    "create": "created the group",
}


@dataclass(frozen=True, slots=True)
class GatewayIntegration:
    """A connected ``whatsapp`` integration on the gateway backend, with its webhook secret."""

    target: whatsapp_inbound.InboundIntegration
    vendor: str
    webhook_secret: str
    #: Gateway API base URL and channel token: a voice note is downloaded with them.
    base_url: str = ""
    channel_token: str = field(default="", repr=False)
    #: The integration's owner: the voice notes are transcribed with their provider.
    owner_id: uuid.UUID | None = None


@dataclass(frozen=True, slots=True)
class VoiceNote:
    """A received voice note or audio, to transcribe before its row is stored."""

    media_id: str
    seconds: int
    mime: str = ""


@dataclass(slots=True)
class PollUpdate:
    """A poll seen, or votes on it: the full option list when known, else one voter's choice."""

    poll_id: str
    conversation_id: str = ""
    title: str = ""
    #: ``[{"id", "name", "voters"}]`` when the payload carries the whole poll.
    options: list[dict[str, Any]] | None = None
    voter: str = ""
    #: The option ids the voter chose (empty: the voter withdrew).
    votes: list[str] = field(default_factory=list)


@dataclass(slots=True)
class ParsedDelivery:
    rows: list[dict[str, Any]] = field(default_factory=list)
    polls: list[PollUpdate] = field(default_factory=list)
    #: Voice notes by message id (``wamid``): transcribed into their row's text.
    voice: dict[str, VoiceNote] = field(default_factory=dict)


_VOICE_TYPES = frozenset({"voice", "audio"})


def duration_words(seconds: int) -> str:
    """``0:42`` of a duration in seconds."""
    seconds = max(0, int(seconds))
    return f"{seconds // 60}:{seconds % 60:02d}"


def voice_words(seconds: int, transcript: str | None = None, failure: str | None = None) -> str:
    """How a voice note reads in the store and in a turn: its transcript, or why there is none."""
    length = duration_words(seconds)
    if failure:
        return f"[voice note, {length}: transcription failed: {failure}]"
    return f"[voice note, {length}, transcribed] {transcript or ''}".strip()


def _bare(jid: Any) -> str:
    """A number without its ``@s.whatsapp.net`` / ``@c.us`` suffix; a group id unchanged."""
    text = str(jid or "").strip()
    if text.endswith(_GROUP_SUFFIX):
        return text
    return text.split("@", 1)[0]


def _received_at(value: Any) -> datetime:
    try:
        return datetime.fromtimestamp(int(str(value)), UTC)
    except (TypeError, ValueError, OverflowError, OSError):
        return datetime.now(UTC)


def _poll_options(poll: dict[str, Any]) -> list[dict[str, Any]]:
    """The options of a Whapi ``poll`` object: id, name and voters, in order."""
    results = poll.get("results") if isinstance(poll.get("results"), list) else []
    options: list[dict[str, Any]] = []
    for item in results:
        if not isinstance(item, dict):
            continue
        voters = [_bare(v.get("id") if isinstance(v, dict) else v) for v in item.get("voters") or []]
        options.append({"id": str(item.get("id") or ""), "name": str(item.get("name") or ""), "voters": voters})
    if not options:
        options = [{"id": "", "name": str(name), "voters": []} for name in poll.get("options") or []]
    return options


def _poll_words(poll: dict[str, Any]) -> str:
    names = [str(o) for o in poll.get("options") or []] or [o["name"] for o in _poll_options(poll)]
    return f"[poll] {poll.get('title') or ''}: " + " / ".join(names)


def _action_words(action: dict[str, Any], sender: str) -> str:
    kind = str(action.get("type") or "")
    who = ", ".join(_bare(p) for p in action.get("participants") or [action.get("participant")] if p) or "a member"
    template = _GROUP_ACTIONS.get(kind)
    if template is None:
        return f"[group event: {kind or 'unknown'}]"
    return f"[group event] {sender or 'someone'} " + template.format(who=who)


def _whapi_message(msg: dict[str, Any], delivery: ParsedDelivery) -> None:
    """One Whapi ``messages[]`` item: a stored row and, for a poll or a vote, a poll update."""
    msg_id = str(msg.get("id") or "").strip()
    chat = str(msg.get("chat_id") or "").strip()
    if not msg_id or not chat:
        return
    kind = str(msg.get("type") or "unknown")
    group = chat.endswith(_GROUP_SUFFIX)
    conversation = chat if group else _bare(chat)
    sender = _bare(msg.get("from") or ("" if group else chat))
    body = msg.get(kind) if isinstance(msg.get(kind), dict) else {}
    if kind == "poll":
        delivery.polls.append(
            PollUpdate(
                poll_id=msg_id,
                conversation_id=conversation,
                title=str(body.get("title") or ""),
                options=_poll_options(body),
            )
        )
    action = msg.get("action") if isinstance(msg.get("action"), dict) else {}
    if kind == "action" and action.get("type") == "vote":
        target = str(action.get("target") or "").strip()
        if target and sender:
            delivery.polls.append(
                PollUpdate(
                    poll_id=target,
                    conversation_id=conversation,
                    voter=sender,
                    votes=[str(v) for v in action.get("votes") or []],
                )
            )
        return
    # The paired number's own "message yourself" chat: a message the number
    # sent in the chat that IS the number. Named by the payload's explicit
    # ``from`` (never the chat fallback above): an outgoing message to a
    # contact is never mistaken for it.
    own = _bare(msg.get("from"))
    self_chat = bool(msg.get("from_me")) and not group and bool(own) and own == conversation
    if msg.get("from_me") and not self_chat:
        return  # what the number sent to someone else is not a received message
    media_id: str | None = None
    if kind == "text":
        text = str(body.get("body") or "")
    elif kind in {"link_preview", "group_invite"}:
        text = str(body.get("body") or body.get("url") or "")
    elif kind in _VOICE_TYPES and str(body.get("id") or "").strip():
        try:
            seconds = int(body.get("seconds") or 0)
        except (TypeError, ValueError):
            seconds = 0
        delivery.voice[msg_id[:255]] = VoiceNote(
            media_id=str(body["id"]).strip(), seconds=seconds, mime=str(body.get("mime_type") or "")
        )
        text = voice_words(seconds, failure="not transcribed")
        media_id = str(body["id"]).strip()
    elif kind in _MEDIA_TYPES:
        text = f"[{kind}] {body.get('caption') or body.get('file_name') or body.get('filename') or ''}".strip()
    elif kind == "poll":
        text = _poll_words(body)
    elif kind == "action":
        text = _action_words(action, str(msg.get("from_name") or sender))
    elif kind == "reaction" or action.get("type") == "reaction":
        text = f"[reaction {action.get('emoji') or body.get('emoji') or '(removed)'} to {action.get('target') or '?'}]"
        kind = "reaction"
    else:
        text = whatsapp_inbound.UNSUPPORTED_WORDS.format(kind=kind).replace("the Cloud API", "the gateway")
    delivery.rows.append(
        {
            "wamid": msg_id[:255],
            "conversation_id": conversation[:255],
            "chat_type": whatsapp_inbound.SELF_CHAT if self_chat else ("group" if group else "private"),
            "sender": (sender or conversation)[:64],
            "sender_name": str(msg.get("from_name") or "")[:255] or None,
            "message_type": kind[:32],
            "text": text[:_MAX_TEXT],
            # A voice note keeps its media id: download_whatsapp_media fetches its raw audio again.
            "media_id": media_id if media_id and len(media_id) <= whatsapp_inbound.MAX_MEDIA_ID else None,
            "received_at": _received_at(msg.get("timestamp")),
        }
    )


def parse_whapi(payload: Any) -> ParsedDelivery:
    """The rows and poll updates of one Whapi.Cloud webhook delivery.

    Handles ``messages`` (new messages, polls, votes and group actions posted as
    messages), ``messages_updates`` (a poll's ``after_update`` carries the whole
    tally; its ``trigger`` the vote) and ``groups_participants`` (members added,
    removed, promoted...). Anything else is ignored.
    """
    delivery = ParsedDelivery()
    if not isinstance(payload, dict):
        return delivery
    for msg in payload.get("messages") or []:
        if isinstance(msg, dict):
            _whapi_message(msg, delivery)
    for update in payload.get("messages_updates") or []:
        if not isinstance(update, dict):
            continue
        after = update.get("after_update") if isinstance(update.get("after_update"), dict) else {}
        poll = after.get("poll") if isinstance(after.get("poll"), dict) else None
        if poll is not None and after.get("id"):
            chat = str(after.get("chat_id") or "")
            delivery.polls.append(
                PollUpdate(
                    poll_id=str(after["id"]),
                    conversation_id=chat if chat.endswith(_GROUP_SUFFIX) else _bare(chat),
                    title=str(poll.get("title") or ""),
                    options=_poll_options(poll),
                )
            )
        elif isinstance(update.get("trigger"), dict):
            _whapi_message(update["trigger"], delivery)
    for event in payload.get("groups_participants") or []:
        if not isinstance(event, dict) or not str(event.get("group_id") or "").endswith(_GROUP_SUFFIX):
            continue
        group = str(event["group_id"])
        action = {"type": event.get("action"), "participants": event.get("participants") or []}
        stamp = event.get("timestamp") or int(datetime.now(UTC).timestamp())
        who = ",".join(_bare(p) for p in action["participants"])
        delivery.rows.append(
            {
                "wamid": f"group-event:{group}:{action['type']}:{who}:{stamp}"[:255],
                "conversation_id": group[:255],
                "chat_type": "group",
                "sender": "",
                "sender_name": None,
                "message_type": "group_event",
                "text": _action_words(action, "")[:_MAX_TEXT],
                "media_id": None,
                "received_at": _received_at(stamp),
            }
        )
    return delivery


#: Webhook parsers by gateway vendor (``gateway_vendor`` extra field).
PARSERS = {"whapi": parse_whapi}


def _missed(integration_id: object, reason: str) -> None:
    logger.warning("WhatsApp gateway webhook: no gateway integration %s: %s", integration_id, reason)


async def gateway_integration(integration_id: str) -> GatewayIntegration | None:
    """The ``whatsapp`` integration *integration_id* if it runs on the gateway backend, else ``None``.

    Checked as the tool build checks it (``integration_tools._get_token``):
    the provider and the ``backend`` extra field, never the stored ``status``,
    which an ``api_key`` integration's tools do not depend on either, so a
    gateway whose tools work is never refused here. The delivery is then
    authenticated by the webhook secret. Every miss is logged with its reason.
    """
    try:
        key = uuid.UUID(str(integration_id))
    except ValueError:
        _missed(integration_id, "not an integration id")
        return None
    try:
        async with async_session() as db:
            integ = (await db.execute(select(Integration).where(Integration.id == key))).scalar_one_or_none()
    except Exception as exc:
        logger.error("WhatsApp gateway integration %s not read: %s", key, format_exception_message(exc))
        return None
    if integ is None:
        _missed(key, "no integration with this id")
        return None
    if integ.provider != whatsapp_inbound.PROVIDER:
        _missed(key, f"provider is {integ.provider!r}, not {whatsapp_inbound.PROVIDER!r}")
        return None
    try:
        extra = await reveal_json_field(integ, "extra_config_enc") if integ.extra_config_enc else {}
    except Exception as exc:
        _missed(key, f"extra config unreadable: {format_exception_message(exc)}")
        return None
    extra = extra if isinstance(extra, dict) else {}
    backend = str(extra.get("backend") or "").strip()
    if backend != BACKEND_GATEWAY:
        _missed(key, f"backend is {backend or 'unset'!r}, not {BACKEND_GATEWAY!r}")
        return None
    return GatewayIntegration(
        target=whatsapp_inbound.InboundIntegration(
            integration_id=integ.id,
            app_secret="",
            verify_token="",
            retention_days=whatsapp_inbound.retention_days(extra),
        ),
        vendor=str(extra.get("gateway_vendor") or "").strip() or "whapi",
        webhook_secret=str(extra.get(SECRET_FIELD) or ""),
        base_url=str(extra.get("base_url") or "").strip(),
        channel_token=await _channel_token(integ),
        owner_id=integ.user_id,
    )


async def _channel_token(integ: Integration) -> str:
    """The gateway's channel token (the integration's API key, a vault reference resolved); empty when unreadable."""
    from app.services.vault import reveal_field

    try:
        return (await reveal_field(integ, "api_key_enc") or "") if integ.api_key_enc else ""
    except Exception as exc:
        logger.warning(
            "WhatsApp gateway integration %s: channel token unreadable: %s", integ.id, exc.__class__.__name__
        )
        return ""


async def _transcribe_one(gateway: GatewayIntegration, note: VoiceNote) -> tuple[str | None, str | None]:
    """``(line, None)`` or ``(None, reason)``: the note downloaded from the gateway, transcribed, then judged.

    The line is the note's turn line (``voice_notes.judged_line``): its text
    heard, with the language heard, or why the text is not trusted (a note cut
    short, far too few words for its length, speech decoded unsure), never
    doubtful words passed off as the note. The audio is held in memory for the
    call only, never stored.
    """
    from app.integrations.providers.whatsapp_gateway import DEFAULT_VENDOR, GATEWAY_CLIENTS, WHAPI_BASE_URL
    from app.llm.transcription import MAX_AUDIO_BYTES, TranscriptionUnavailableError, resolve_transcriber
    from app.services.agent_accounts import voice_notes

    client_class = GATEWAY_CLIENTS.get(gateway.vendor or DEFAULT_VENDOR)
    if client_class is None or not gateway.channel_token:
        return None, "the gateway cannot be reached with this integration"
    if gateway.owner_id is None:
        return None, "the integration has no owner to transcribe for"
    client = client_class(gateway.base_url or WHAPI_BASE_URL, gateway.channel_token)
    audio, err = await client.download_media(note.media_id, MAX_AUDIO_BYTES)
    if err or not audio:
        return None, f"download failed ({err or 'empty file'})"
    try:
        async with async_session() as db:
            transcriber = await resolve_transcriber(db, gateway.owner_id)
        extension = "m4a" if "mp4" in note.mime or "m4a" in note.mime else "ogg"
        transcript = await transcriber.transcribe(audio, filename=f"voice.{extension}", words=True)
    except TranscriptionUnavailableError as exc:
        return None, str(exc)
    except Exception as exc:
        return None, format_exception_message(exc)
    if not transcript.text.strip():
        return None, "nothing was heard"
    return voice_notes.judged_line(transcript, note.seconds or None, say_language=True), None


async def transcribe_voice(gateway: GatewayIntegration, delivery: ParsedDelivery) -> int:
    """Replace each voice note's row text by its transcript, or by why there is none. Never raises.

    A note is said with its duration and the language heard (``[voice note,
    0:42, transcribed, heard in french] ...``); a transcript that cannot be
    trusted is said as such (``[voice note, 0:56, heard in french: transcription
    low confidence: ...]``); a failed download or transcription is stated, never
    an empty message. Returns the notes transcribed and trusted.
    """
    if not delivery.voice:
        return 0
    done = 0
    for row in delivery.rows:
        note = delivery.voice.get(row.get("wamid") or "")
        if note is None:
            continue
        try:
            line, failure = await _transcribe_one(gateway, note)
        except Exception as exc:
            line, failure = None, format_exception_message(exc)
        if failure:
            logger.warning(
                "WhatsApp integration %s: voice note %s not transcribed: %s",
                gateway.target.integration_id,
                row.get("wamid"),
                failure,
            )
            line = voice_words(note.seconds, failure=failure)
        elif "transcription low confidence" in str(line):
            logger.warning(
                "WhatsApp integration %s: voice note %s transcribed with low confidence",
                gateway.target.integration_id,
                row.get("wamid"),
            )
        else:
            done += 1
        line = str(line or voice_words(note.seconds, failure="nothing was heard"))
        row["text"] = whatsapp_inbound.with_audio_reference(line, row.get("media_id"))[:_MAX_TEXT]
    return done


def apply_poll_update(stored: list[dict[str, Any]] | None, update: PollUpdate) -> list[dict[str, Any]]:
    """The options of a poll after *update*: a full option list replaces them, a vote moves one voter."""
    if update.options is not None and (update.options or not stored):
        return [dict(o, voters=list(o.get("voters") or [])) for o in update.options]
    options = [dict(o, voters=[v for v in o.get("voters") or [] if v != update.voter]) for o in stored or []]
    known = {o.get("id") for o in options}
    for option_id in update.votes:
        if option_id not in known:
            options.append({"id": option_id, "name": "", "voters": []})
            known.add(option_id)
        for option in options:
            if option.get("id") == option_id and update.voter not in option["voters"]:
                option["voters"].append(update.voter)
    return options


async def record_polls(target: whatsapp_inbound.InboundIntegration, updates: list[PollUpdate]) -> int:
    """Upsert the tallies of *updates* for *target*; returns the polls written. Never raises."""
    if not updates:
        return 0
    written = 0
    try:
        async with async_session() as db:
            for update in updates:
                row = (
                    await db.execute(
                        select(WhatsAppPollTally)
                        .where(
                            WhatsAppPollTally.integration_id == target.integration_id,
                            WhatsAppPollTally.poll_id == update.poll_id[:255],
                        )
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                stored = json.loads(row.options) if row is not None and row.options else None
                options = apply_poll_update(stored, update)
                now = datetime.now(UTC)
                if row is None:
                    db.add(
                        WhatsAppPollTally(
                            id=uuid.uuid4(),
                            integration_id=target.integration_id,
                            poll_id=update.poll_id[:255],
                            conversation_id=update.conversation_id[:255],
                            title=update.title,
                            options=json.dumps(options),
                            updated_at=now,
                        )
                    )
                else:
                    row.options = json.dumps(options)
                    row.title = update.title or row.title
                    row.conversation_id = row.conversation_id or update.conversation_id[:255]
                    row.updated_at = now
                written += 1
                await db.flush()
            cutoff = datetime.now(UTC) - timedelta(days=target.retention_days)
            await db.execute(
                delete(WhatsAppPollTally).where(
                    WhatsAppPollTally.integration_id == target.integration_id,
                    WhatsAppPollTally.updated_at < cutoff,
                )
            )
            await db.commit()
    except Exception as exc:
        logger.error(
            "WhatsApp integration %s: poll tallies not stored: %s", target.integration_id, format_exception_message(exc)
        )
        return 0
    return written


async def record_delivery(gateway: GatewayIntegration, payload: Any) -> tuple[int, int]:
    """Store one webhook delivery: ``(messages written, polls written)``. Never raises."""
    parser = PARSERS.get(gateway.vendor)
    if parser is None:
        logger.warning("WhatsApp gateway webhook: no parser for vendor %r", gateway.vendor)
        return 0, 0
    delivery = parser(payload)
    await transcribe_voice(gateway, delivery)
    messages = await whatsapp_inbound.record_rows(gateway.target, delivery.rows) if delivery.rows else 0
    polls = await record_polls(gateway.target, delivery.polls)
    return messages, polls


async def stored_tally(integration_id: str, poll_id: str) -> dict[str, Any] | None:
    """The stored tally of *poll_id* in the shape of ``whatsapp_gateway.poll_tally``, or ``None``."""
    try:
        key = uuid.UUID(str(integration_id))
        async with async_session() as db:
            row = (
                await db.execute(
                    select(WhatsAppPollTally).where(
                        WhatsAppPollTally.integration_id == key, WhatsAppPollTally.poll_id == poll_id
                    )
                )
            ).scalar_one_or_none()
    except Exception as exc:
        logger.warning("WhatsApp poll tally not read: %s", format_exception_message(exc))
        return None
    if row is None:
        return None
    return tally_of(row.title, json.loads(row.options or "[]"))


def tally_of(title: str, options: list[dict[str, Any]]) -> dict[str, Any]:
    """``{"title", "total", "options": [{"id", "name", "count", "voters"}]}`` of stored options."""
    shaped = [
        {
            "id": str(o.get("id") or ""),
            "name": str(o.get("name") or ""),
            "count": len(o.get("voters") or []),
            "voters": list(o.get("voters") or []),
        }
        for o in options
    ]
    return {"title": title, "total": sum(o["count"] for o in shaped), "options": shaped}
