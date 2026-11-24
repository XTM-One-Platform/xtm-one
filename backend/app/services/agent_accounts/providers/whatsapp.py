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

"""WhatsApp agent accounts: an agent operates a business number.

The account is a ``whatsapp`` integration (WhatsApp Cloud API, one business
phone number, a system user access token): ``credential = "api_key"``, the
``phone_number_id`` carried in ``session.config``. Modes ``ai_user`` (the
number is the agent's own) and ``on_behalf`` (the number is a person's, the
agent answers as them) go through the engine like every provider.

Inbound: Meta posts every message of the number to the platform's single
WhatsApp callback; ``services/whatsapp_inbound.record_change`` stores it
and hands every newly stored message to the enabled accounts on the
integration (:func:`dispatch_stored`). No subscription, no poll, no second
webhook. The context is replayed from that store.

Outbound: a reply is one Cloud API text message, sent as a reply to the
message answered, through the slice 1a helpers (``recipient_fields``,
``whatsapp_headers``).

Backends: the integration's ``backend`` extra field picks the Cloud
API (default, a ``phone_number_id``) or a linked-device gateway, whose
channel token is the integration's key: the number is read from the gateway's
health, replies go through the gateway, and nothing asks for a
``phone_number_id``. Inbound reaches the same store on both backends.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

from app.core.egress import egress_client, egress_route
from app.integrations.providers.whatsapp import GRAPH_API, recipient_fields, whatsapp_headers
from app.integrations.providers.whatsapp_gateway import (
    BACKEND_GATEWAY,
    DEFAULT_VENDOR,
    GATEWAY_CLIENTS,
    WHAPI_BASE_URL,
    GatewayClient,
    chat_id,
)
from app.services.agent_accounts.events import (
    AccountEvent,
    AccountIdentity,
    ContextMessage,
    ConversationOption,
)
from app.services.agent_accounts.providers import (
    AccountProvider,
    ProviderCapabilities,
    ProviderError,
    ProviderSession,
)

logger = logging.getLogger(__name__)

KEY = "whatsapp"
#: The Cloud API's longest text message.
MAX_REPLY_CHARS = 4096
#: Stored message types that are not something said (never answered).
# Group events of the gateway webhook are stored for reading, never answered.
_NOT_MESSAGES = frozenset({"reaction", "group_event"})
_KIND_BY_STATUS = {401: "reauthorize", 403: "refused", 404: "not_found", 429: "throttled"}


def _graph_error(resp: Any) -> ProviderError:
    try:
        error = resp.json().get("error") or {}
        message = error.get("message") or error.get("code")
    except Exception:
        message = None
    status = resp.status_code
    kind = _KIND_BY_STATUS.get(status) or ("unavailable" if status >= 500 else "error")
    return ProviderError(f"WhatsApp answered {status}" + (f": {message}" if message else ""), kind=kind)


def is_gateway(config: dict[str, str]) -> bool:
    """Whether the integration a session acts with is on the linked-device gateway backend."""
    return (config.get("backend") or "").strip() == BACKEND_GATEWAY


def conversation_type(chat_type: str | None) -> str:
    """The engine's conversation type of a stored ``chat_type`` (the self-chat is a direct chat)."""
    return "group" if chat_type == "group" else "direct"


def is_self_chat(row: Any) -> bool:
    """Whether a stored row is the paired number's own "message yourself" chat."""
    from app.services.whatsapp_inbound import SELF_CHAT

    value = row.get("chat_type") if isinstance(row, dict) else getattr(row, "chat_type", None)
    return value == SELF_CHAT


def self_chat_agent(extra: Any) -> uuid.UUID | None:
    """The agent the integration routes its self-chat to (``self_chat_agent_id``); ``None``: off."""
    from app.services.whatsapp_inbound import SELF_CHAT_AGENT_FIELD

    raw = str((extra or {}).get(SELF_CHAT_AGENT_FIELD) or "").strip() if isinstance(extra, dict) else ""
    try:
        return uuid.UUID(raw) if raw else None
    except ValueError:
        return None


def event_from_row(row: Any, *, owner_email: str | None = None) -> AccountEvent:
    """The event of one stored inbound message (a ``WhatsAppInboundMessage`` or its fields).

    *owner_email*: the self-chat's sender is the integration's owner, named by
    the integration (never by the message), so the rules and the directory see
    the XTM One user who paired the number.
    """

    def get(name: str) -> Any:
        return row.get(name) if isinstance(row, dict) else getattr(row, name, None)

    received = get("received_at")
    if not isinstance(received, datetime):
        received = datetime.now(UTC)
    sender = str(get("sender") or "")
    conversation_id = str(get("conversation_id") or sender)
    kind = conversation_type(get("chat_type"))
    message_id = str(get("wamid") or "")
    return AccountEvent(
        provider=KEY,
        conversation_type=kind,
        conversation_id=conversation_id,
        thread_id=conversation_id,
        message_id=message_id,
        sender_id=sender,
        sender_name=str(get("sender_name") or sender),
        text=str(get("text") or ""),
        created_at=received if received.tzinfo else received.replace(tzinfo=UTC),
        sender_email=owner_email or None,
        self_chat=is_self_chat(row),
        route={"to": conversation_id, "reply_to": message_id, "conversation_type": kind},
    )


async def dispatch_stored(integration_id: uuid.UUID | str, rows: Iterable[dict[str, Any]]) -> int:
    """Hand newly stored messages to the enabled WhatsApp accounts on *integration_id*.

    Called by ``whatsapp_inbound.record_change`` with the rows it inserted
    (a redelivered message is not inserted twice, so never answered twice).
    Reactions are not messages and are skipped. Returns the events
    dispatched; never raises.

    The self-chat (``chat_type = "self"``, the paired number writing to
    itself) is an instruction channel of the integration's owner: it goes
    ONLY to the enabled accounts of the agent the integration's
    ``self_chat_agent_id`` names, never to the others, and to none when the
    field is empty (the default). Every other chat is dispatched as before.
    """
    from sqlalchemy import select

    from app.core.database import async_session
    from app.core.utils import format_exception_message
    from app.models.agent_account import AgentAccount
    from app.models.integration import Integration
    from app.models.user import User
    from app.services.agent_accounts.pipeline import dispatch
    from app.services.vault import reveal_json_field

    rows = [row for row in rows if str(row.get("message_type") or "") not in _NOT_MESSAGES]
    if not rows:
        return 0
    others = [row for row in rows if not is_self_chat(row)]
    own = [row for row in rows if is_self_chat(row)]
    try:
        key = uuid.UUID(str(integration_id))
        async with async_session() as db:
            accounts = (
                await db.execute(
                    select(AgentAccount.id, AgentAccount.agent_id).where(
                        AgentAccount.integration_id == key,
                        AgentAccount.provider == KEY,
                        AgentAccount.enabled.is_(True),
                    )
                )
            ).all()
            routed_agent: uuid.UUID | None = None
            owner_email: str | None = None
            if own and accounts:
                integ = await db.get(Integration, key)
                extra = (
                    await reveal_json_field(integ, "extra_config_enc")
                    if integ is not None and integ.extra_config_enc
                    else {}
                )
                routed_agent = self_chat_agent(extra)
                if routed_agent is not None and integ is not None and integ.user_id is not None:
                    owner = await db.get(User, integ.user_id)
                    owner_email = owner.email if owner is not None and owner.is_active else None
    except Exception as exc:
        logger.error(
            "WhatsApp integration %s: agent accounts not read: %s", integration_id, format_exception_message(exc)
        )
        return 0
    events = [event_from_row(row) for row in others]
    # No owner known (a shared integration): the self-chat is not routed.
    own_events = [event_from_row(row, owner_email=owner_email) for row in own] if owner_email else []
    dispatched = 0
    for account_id, agent_id in accounts:
        targets = events + (own_events if routed_agent is not None and agent_id == routed_agent else [])
        for event in targets:
            dispatch(str(account_id), event)
            dispatched += 1
    return dispatched


async def sent_replies(integration_id: str, conversation_id: str, limit: int) -> list[ContextMessage]:
    """The last *limit* replies the integration's WhatsApp accounts sent in *conversation_id*, oldest first.

    Each is a ``from_account`` message whose id is the sent message's id: the
    gateway's echo of it in the number's own chat carries that id.
    """
    from sqlalchemy import select

    from app.core.database import async_session
    from app.models.agent_account import AgentAccount, AgentAccountReply

    key = uuid.UUID(str(integration_id))
    r = AgentAccountReply
    async with async_session() as db:
        found = (
            await db.execute(
                select(r.sent_message_id, r.draft_text, r.sent_text, r.decided_at, r.created_at)
                .join(AgentAccount, AgentAccount.id == r.account_id)
                .where(
                    AgentAccount.integration_id == key,
                    AgentAccount.provider == KEY,
                    r.conversation_id == conversation_id.strip(),
                    r.status == "sent",
                    r.sent_message_id.is_not(None),
                )
                .order_by(r.created_at.desc())
                .limit(max(1, limit))
            )
        ).all()
    out = [
        ContextMessage(
            message_id=str(sent_id),
            sender_name="",
            text=str(draft or sent or ""),
            created_at=_aware(decided or created),
            from_account=True,
        )
        for sent_id, draft, sent, decided, created in found
        if (draft or sent) and (decided or created)
    ]
    return list(reversed(out))


def _aware(moment: datetime) -> datetime:
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def with_own_replies(
    rows: Iterable[Any], replies: Iterable[ContextMessage], event: AccountEvent, limit: int
) -> list[ContextMessage]:
    """The context of *event*: the stored *rows* before it and the account's *replies*, oldest first.

    A stored row that is the echo of a reply (same message id: the gateway
    stores the number's own chat, replies included) is the reply, placed at
    the echo's time, never a message of someone else. Without the replies the
    turn took every earlier message for a new one and answered it again.
    """
    by_id = {reply.message_id: reply for reply in replies if reply.message_id}
    out: list[ContextMessage] = []
    for row in rows:
        if row.wamid == event.message_id or _aware(row.received_at) > event.created_at:
            continue
        reply = by_id.pop(row.wamid, None)
        if reply is not None:
            out.append(
                ContextMessage(
                    message_id=reply.message_id,
                    sender_name=reply.sender_name,
                    text=reply.text,
                    created_at=_aware(row.received_at),
                    from_account=True,
                )
            )
            continue
        out.append(
            ContextMessage(
                message_id=row.wamid,
                sender_name=row.sender_name or row.sender,
                text=row.text or "",
                created_at=_aware(row.received_at),
            )
        )
    out = out[-limit:]
    # Replies with no echo (the Cloud API echoes nothing): placed at their
    # send time, within the window the stored messages cover.
    since = out[0].created_at if len(out) >= limit else None
    out.extend(
        reply
        for reply in by_id.values()
        if reply.created_at <= event.created_at and (since is None or reply.created_at >= since)
    )
    out.sort(key=lambda m: m.created_at)
    return out


class WhatsAppProvider(AccountProvider):
    """A WhatsApp business number, operated through its ``whatsapp`` integration."""

    key = KEY
    label = "WhatsApp"
    integration_provider = "whatsapp"
    platform = "whatsapp"
    kind = "chat"
    capabilities = ProviderCapabilities(
        direct=True,
        group=True,
        channel=False,
        push=False,
        poll=False,
        people_search=False,
        conversations=True,
        # Both backends store inbound through the integration's webhook.
        inbound_webhook=True,
    )
    credential = "api_key"
    session_config_keys = ("phone_number_id", "backend", "gateway_vendor", "base_url")

    def required_config_keys(self, config: dict[str, str]) -> tuple[str, ...]:
        """The Cloud API needs its ``phone_number_id``; a linked-device gateway nothing more."""
        return () if is_gateway(config) else ("phone_number_id",)

    def config_label(self, config: dict[str, str]) -> str:
        """The backend the session was checked for, and the stored ``backend`` value it read.

        A gateway integration whose ``backend`` selector was lost reads as a
        Cloud API one: naming the value read shows the owner the cause.
        """
        if is_gateway(config):
            return "the linked-device gateway backend"
        stored = (config.get("backend") or "").strip()
        read = f"'{stored}'" if stored else "empty"
        return f"the Cloud API backend; stored backend: {read}"

    async def verify_session(self, session: ProviderSession) -> None:
        """On the gateway, the number must still be paired at every sync; the Cloud API needs nothing."""
        if is_gateway(session.config):
            await self._gateway_identify(session)

    # -- gateway backend ---------------------------------------------

    def _gateway(self, session: ProviderSession) -> GatewayClient:
        vendor = (session.config.get("gateway_vendor") or "").strip() or DEFAULT_VENDOR
        client_class = GATEWAY_CLIENTS.get(vendor)
        if client_class is None:
            raise ProviderError(
                f"The WhatsApp gateway vendor {vendor!r} is not supported (checked for the linked-device gateway "
                "backend).",
                kind="refused",
            )
        base_url = (session.config.get("base_url") or "").strip() or WHAPI_BASE_URL
        return client_class(base_url, session.token)

    async def _gateway_identify(self, session: ProviderSession) -> AccountIdentity:
        client = self._gateway(session)
        with egress_route(session.route):
            health, err = await client.health()
        if err:
            raise ProviderError(
                f"The WhatsApp gateway could not be read (linked-device gateway backend): {err}", kind="unavailable"
            )
        state = client.pairing_state(health)
        user = health.get("user") if isinstance(health, dict) else None
        number = str((user or {}).get("id") or "").split("@", 1)[0].strip() if isinstance(user, dict) else ""
        if state != "connected" or not number:
            raise ProviderError(
                f"The WhatsApp gateway number is not paired (state {state}, linked-device gateway backend): "
                "pair it from WhatsApp > Linked devices (tool get_whatsapp_pairing_qr).",
                kind="reauthorize",
            )
        name = str(user.get("name") or user.get("pushname") or "").strip() if isinstance(user, dict) else ""
        return AccountIdentity(external_id=number, display_name=name or f"+{number}")

    async def _gateway_send(self, session: ProviderSession, route: dict[str, Any], text: str) -> str:
        chat = chat_id(str(route.get("to") or ""))
        if chat is None:
            raise ProviderError("The WhatsApp conversation has no recipient.", kind="not_found")
        with egress_route(session.route):
            data, err = await self._gateway(session).send_text(chat, text[:MAX_REPLY_CHARS])
        if err:
            raise ProviderError(f"The WhatsApp gateway did not send the reply: {err}", kind="unavailable")
        message = data.get("message") if isinstance(data, dict) else None
        return str(message.get("id") or "") if isinstance(message, dict) else ""

    # -- HTTP ---------------------------------------------------------------

    def _phone_number_id(self, session: ProviderSession) -> str:
        phone_number_id = (session.config.get("phone_number_id") or "").strip()
        if not phone_number_id.isdigit():
            raise ProviderError(
                "The WhatsApp integration has no valid phone_number_id (checked for the Cloud API backend).",
                kind="refused",
            )
        return phone_number_id

    async def _call(
        self,
        session: ProviderSession,
        method: str,
        url: str,
        *,
        payload: dict | None = None,
        params: dict | None = None,
    ) -> dict:
        kwargs: dict[str, Any] = {"headers": whatsapp_headers(session.token)}
        if payload is not None:
            kwargs["json"] = payload
        if params:
            kwargs["params"] = params
        try:
            with egress_route(session.route):
                async with egress_client(timeout=30) as client:
                    resp = await client.request(method, url, **kwargs)
        except Exception as exc:
            raise ProviderError(f"WhatsApp could not be reached: {type(exc).__name__}", kind="unavailable") from None
        if resp.status_code >= 400:
            raise _graph_error(resp)
        try:
            data = resp.json()
        except ValueError:
            raise ProviderError("WhatsApp answered with a body that is not JSON.") from None
        return data if isinstance(data, dict) else {}

    # -- identity -----------------------------------------------------------

    async def identify(self, session: ProviderSession) -> AccountIdentity:
        """The business number: its id, its verified name (else its display number).

        On a linked-device gateway, the paired number and its profile name.
        """
        if is_gateway(session.config):
            return await self._gateway_identify(session)
        phone_number_id = self._phone_number_id(session)
        data = await self._call(
            session,
            "GET",
            f"{GRAPH_API}/{phone_number_id}",
            params={"fields": "verified_name,display_phone_number"},
        )
        name = str(data.get("verified_name") or data.get("display_phone_number") or phone_number_id)
        return AccountIdentity(external_id=phone_number_id, display_name=name)

    # -- answering ----------------------------------------------------------

    async def fetch_context(self, session: ProviderSession, event: AccountEvent, limit: int) -> list[ContextMessage]:
        """The messages of the conversation before *event*, oldest first, the account's own replies included.

        The store holds what the number received; the replies the integration's
        accounts sent in the conversation are read from their record
        (:func:`sent_replies`) and marked ``from_account``, so the turn sees
        what was already answered as history and only the new messages as the
        user message (:func:`with_own_replies`).
        """
        from app.services.whatsapp_inbound import read_messages

        integration_id = (session.config.get("integration_id") or "").strip()
        if not integration_id or limit <= 0:
            return []
        try:
            rows = await read_messages(integration_id, event.conversation_id, limit + 1)
        except Exception as exc:
            raise ProviderError(f"The WhatsApp conversation could not be read: {type(exc).__name__}") from None
        try:
            replies = await sent_replies(integration_id, event.conversation_id, limit)
        except Exception as exc:
            # The stored messages alone still give the turn its context.
            logger.info("WhatsApp integration %s: own replies not read: %s", integration_id, type(exc).__name__)
            replies = []
        return with_own_replies(rows, replies, event, limit)

    async def account_messages_since(self, session: ProviderSession, event: AccountEvent) -> list[str]:
        """None known: the Cloud API does not echo what the number sends."""
        return []

    async def send_reply(self, session: ProviderSession, route: dict[str, Any], text: str) -> str:
        """One text message to the conversation, as a reply to the message answered."""
        if is_gateway(session.config):
            return await self._gateway_send(session, route, text)
        phone_number_id = self._phone_number_id(session)
        recipient = recipient_fields(str(route.get("to") or ""))
        if recipient is None:
            raise ProviderError("The WhatsApp conversation has no recipient.", kind="not_found")
        payload: dict[str, Any] = {
            "messaging_product": "whatsapp",
            **recipient,
            "type": "text",
            "text": {"body": text[:MAX_REPLY_CHARS]},
        }
        if route.get("reply_to"):
            payload["context"] = {"message_id": str(route["reply_to"])}
        data = await self._call(session, "POST", f"{GRAPH_API}/{phone_number_id}/messages", payload=payload)
        messages = data.get("messages")
        if isinstance(messages, list) and messages and isinstance(messages[0], dict):
            return str(messages[0].get("id") or "")
        return ""

    # -- editor -------------------------------------------------------------

    async def list_conversations(self, session: ProviderSession, query: str | None = None) -> list[ConversationOption]:
        """The conversations the number received messages in, newest first."""
        from app.services.whatsapp_inbound import list_conversations

        integration_id = (session.config.get("integration_id") or "").strip()
        if not integration_id:
            return []
        try:
            rows = await list_conversations(integration_id, 100)
        except Exception as exc:
            raise ProviderError(f"The WhatsApp conversations could not be read: {type(exc).__name__}") from None
        wanted = (query or "").strip().lower()
        out = []
        for row in rows:
            name = str(row.get("last_sender") or row["conversation_id"])
            if row.get("chat_type") == "group":
                name = f"Group {row['conversation_id']}"
            if wanted and wanted not in name.lower() and wanted not in str(row["conversation_id"]).lower():
                continue
            out.append(
                ConversationOption(
                    id=str(row["conversation_id"]), name=name, type=conversation_type(row.get("chat_type"))
                )
            )
        return out
