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

"""Microsoft Outlook as an agent-account provider: a mailbox, through the account's delegated Graph token.

Listening: one Graph subscription to the account's inbox
(``/users/{id}/mailFolders('inbox')/messages``, delegated ``Mail.Read``),
created with immutable ids so an email keeps its id when it is moved. Without
push (no public HTTPS address) the inbox is polled on the time mail is
received. A notification only says where to look: the email is read back
with the account's token.

An email maps onto the engine's conversations: sent to the account alone, it
is a direct message; with other recipients, a group conversation; the account
in ``To`` is the account addressed, ``Cc`` alone is not. Its thread is its
``conversationId``. Automated mail - what RFC 3834 asks an automatic
responder to leave alone (``Auto-Submitted`` other than ``no``,
``Precedence: bulk``, ``junk`` or ``list``, the ``List-*`` headers of RFC
2369 / 2919) and an email with no sender - is an application's. A calendar
invitation is the calendar's, and is not heard; a meeting response (accepted,
tentative, declined) or a cancellation is heard only when its sender wrote
something in it (a counter-proposal, a reason), its sender being who acted on
the calendar (Graph ``sender``, the delegate of "X on behalf of Y"); a bare
response is the calendar's bookkeeping and is not heard.

Answering: ``createReply`` (``createReplyAll`` when other people are on the
email - how many is ``AccountEvent.reply_recipients``, which the account
bounds) keeps the quoted thread; the answer goes above it with the signature
the owner chose - the account's Outlook signature read from its sent
mail, one read from a signature integration, or none - then the draft is sent. Every
email an agent account sends carries :data:`AGENT_HEADER`, so another agent
account reading it leaves it alone.

Triage (``triage.py``): ``mark_message`` flags the email for follow-up, marks
it read and adds a category.
"""

from __future__ import annotations

import logging
import re
import time
from datetime import UTC, datetime
from typing import Any

from bs4 import BeautifulSoup

from app.integrations.providers.microsoft._draft_body import HTML_BODY_PREFERENCE, is_html_body, text_body_as_html
from app.integrations.providers.microsoft._outlook import (
    _extract_signature_block,
    sent_mail_signature,
    signature_block,
)
from app.integrations.providers.microsoft._signature_images import inline_signature_images
from app.services import mail_send_guard
from app.services.agent_accounts.events import AccountEvent, ContextMessage
from app.services.agent_accounts.providers import (
    AccountSignature,
    DesiredSubscription,
    DuplicateReplyError,
    InboundNotification,
    MessageMark,
    ProviderCapabilities,
    ProviderError,
    ProviderSession,
)
from app.services.agent_accounts.providers.microsoft_graph import (
    GRAPH,
    MicrosoftGraphProvider,
    _parse_time,
    _segment,
    _TTLCache,
)
from app.services.agent_accounts.rules import ListeningPlan
from app.services.email_html import build_graph_email_body, close_before_signature

logger = logging.getLogger(__name__)

#: Graph keeps a subscription to Outlook messages at most 10080 minutes; this leaves a margin.
SUBSCRIPTION_MINUTES = 10000
MAIL_PERMISSION = "Mail.Read"
#: The header every email an agent account sends carries (Graph only sets ``x-`` headers).
AGENT_HEADER = "X-XTM-One-Agent-Account"
#: Ids that survive a move between folders, and bodies read as text.
_IMMUTABLE_IDS = 'IdType="ImmutableId"'
_READ_HEADERS = {"Prefer": f'{_IMMUTABLE_IDS}, outlook.body-content-type="text"'}
_WRITE_HEADERS = {"Prefer": _IMMUTABLE_IDS}
#: A reply draft's creation and read: its body (the native quote) in HTML, never text.
_DRAFT_HEADERS = {"Prefer": f"{_IMMUTABLE_IDS}, {HTML_BODY_PREFERENCE}"}
_INBOX = "mailFolders('inbox')/messages"
_MESSAGE_SELECT = (
    "id,conversationId,subject,from,sender,toRecipients,ccRecipients,receivedDateTime,sentDateTime,"
    "uniqueBody,bodyPreview,hasAttachments,isDraft,internetMessageHeaders,internetMessageId"
)
_CONTEXT_SELECT = "id,from,sender,subject,receivedDateTime,sentDateTime,uniqueBody,bodyPreview,isDraft"
#: What the owner-takeover check reads of a thread's emails: who sent each, and when.
_HEADER_SELECT = "id,from,sender,sentDateTime,isDraft"
#: Emails a poll reads at most, oldest first.
POLL_MESSAGES = 50
#: Emails of a thread read per page, with their text (the context) or their headers alone.
CONTEXT_PAGE = 50
HEADER_PAGE = 250
#: Emails of a thread read at most. Graph does not order a ``conversationId``
#: filter, so the thread is read whole up to this bound and sorted here.
THREAD_READ = 500
#: Sent emails scanned for the account's signature.
SIGNATURE_SCAN = 30
MAX_ATTACHMENT_NAMES = 20
TEXT_LIMIT = 12000
_AUTOMATED_PRECEDENCE = frozenset({"bulk", "junk", "list"})
_LIST_HEADERS = ("list-id", "list-unsubscribe", "list-post")
_MESSAGE_RESOURCE = re.compile(r"messages(?:\('([^']+)'\)|/([^/?#']+))\s*$", re.IGNORECASE)
_OWN_MESSAGE_TYPE = "#microsoft.graph.message"
#: Calendar messages heard when their sender wrote a body: a response
#: (accepted, tentative, declined) and a cancellation. An invitation
#: (``eventMessageRequest``) is never heard.
_CALENDAR_REPLY_TYPES = frozenset({"#microsoft.graph.eventMessageResponse", "#microsoft.graph.eventMessage"})
#: Exchange's first-contact safety tip, which it writes into the body of an
#: email from an unfamiliar sender: "[You don't often get email from x. Learn
#: why this is important at https://aka.ms/LearnAboutSenderIdentification ]".
#: It is not a word of the sender, so a calendar response holding only it is
#: bare. In an HTML body the link is an ``href``: its text reads only
#: "Learn why this is important".
_SENDER_IDENTIFICATION_TIP = re.compile(
    r"\[?\s*(?:Some people who received this message )?(?:You )?don[’']t often get email from"
    r"[\s\S]{0,400}?"
    r"(?:Learn why this is important(?:\s+at\s+<?\S*LearnAboutSenderIdentification[^\]\n]{0,10})?"
    r"|\S*LearnAboutSenderIdentification[^\]\n]{0,10})"
    r"\s*\]?",
    re.IGNORECASE,
)

_addresses_of_accounts = _TTLCache()
_signatures = _TTLCache()
#: What the reply guard reads of the email answered: its thread and its people.
_ANSWERED_SELECT = "id,conversationId,subject,from,sender,toRecipients,ccRecipients"
#: Sent emails read for an earlier send in the conversation, within the guard's window.
_GUARD_SENT_PAGE = 25
_CID_IMAGE = re.compile(r"<img\b[^>]*\bsrc\s*=\s*([\"'])cid:", re.IGNORECASE)


def _addresses(recipients: Any) -> set[str]:
    out: set[str] = set()
    for recipient in recipients or []:
        address = ((recipient or {}).get("emailAddress") or {}).get("address") if isinstance(recipient, dict) else None
        if isinstance(address, str) and address.strip():
            out.add(address.strip().lower())
    return out


def _headers(msg: dict) -> dict[str, str]:
    out: dict[str, str] = {}
    for header in msg.get("internetMessageHeaders") or []:
        if isinstance(header, dict) and isinstance(header.get("name"), str):
            out.setdefault(header["name"].strip().lower(), str(header.get("value") or "").strip())
    return out


def _automated(headers: dict[str, str]) -> bool:
    """Whether RFC 3834 asks an automatic responder to leave the email alone."""
    auto = headers.get("auto-submitted", "").lower()
    if auto and auto != "no":
        return True
    if headers.get("precedence", "").lower() in _AUTOMATED_PRECEDENCE:
        return True
    return any(name in headers for name in _LIST_HEADERS)


def _as_text(content: Any, content_type: Any) -> str:
    if not isinstance(content, str):
        return ""
    if str(content_type or "").lower() == "html":
        content = BeautifulSoup(content, "html.parser").get_text("\n")
    return re.sub(r"\n{3,}", "\n\n", content.replace("\r\n", "\n")).strip()


def _body(msg: dict) -> str:
    unique = msg.get("uniqueBody") if isinstance(msg.get("uniqueBody"), dict) else {}
    text = _as_text(unique.get("content"), unique.get("contentType"))
    return text or _as_text(msg.get("bodyPreview"), "text")


def _calendar_message(msg: dict) -> bool:
    return str(msg.get("@odata.type") or _OWN_MESSAGE_TYPE) in _CALENDAR_REPLY_TYPES


def _written_body(msg: dict) -> str:
    """The body without Exchange's first-contact safety tip: what the sender wrote."""
    return re.sub(r"\n{3,}", "\n\n", _SENDER_IDENTIFICATION_TIP.sub("", _body(msg))).strip()


def _message_text(msg: dict) -> str:
    subject = str(msg.get("subject") or "").strip()
    body = _written_body(msg) if _calendar_message(msg) else _body(msg)
    text = f"Subject: {subject}\n\n{body}" if subject and body else subject or body
    return text[:TEXT_LIMIT]


def _people(msg: dict) -> list[tuple[str, str]]:
    """``(address, name)`` of who wrote the email (``from``) and who sent it (``sender``), each once."""
    out: list[tuple[str, str]] = []
    for key in ("from", "sender"):
        person = (msg.get(key) or {}).get("emailAddress") if isinstance(msg.get(key), dict) else None
        address = person.get("address") if isinstance(person, dict) else None
        if isinstance(address, str) and address.strip() and address.strip().lower() not in {a.lower() for a, _ in out}:
            out.append((address.strip(), str(person.get("name") or address).strip()))
    return out


def _sender(msg: dict) -> tuple[str, str]:
    """``(address, name)`` of who wrote the email: its ``from``, else who sent it."""
    people = _people(msg)
    return people[0] if people else ("", "")


def _heard_type(msg: dict) -> str | None:
    """Whether the email is heard, by its Graph type: ``"mail"``, ``"calendar"`` (a response or cancellation with a written body) or ``None``."""
    message_type = str(msg.get("@odata.type") or _OWN_MESSAGE_TYPE)
    if message_type == _OWN_MESSAGE_TYPE:
        return "mail"
    if message_type in _CALENDAR_REPLY_TYPES and _written_body(msg):
        return "calendar"
    return None


def _escaped(value: str) -> str:
    """*value* as an OData string literal's content."""
    return value.replace("'", "''")


def _recipients(addresses: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Graph recipients, each address once, in order."""
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for recipient in addresses:
        address = ((recipient or {}).get("emailAddress") or {}).get("address") if isinstance(recipient, dict) else None
        key = str(address or "").strip().lower()
        if key and key not in seen:
            seen.add(key)
            out.append(recipient)
    return out


def _owner_stays_in_cc(
    answered: dict[str, Any], draft: dict[str, Any], owner: str, own: frozenset[str]
) -> dict[str, list[dict[str, Any]]] | None:
    """The recipients of a reply-all to an email the account's owner wrote to others, the owner kept in Cc.

    Graph's ``createReplyAll`` puts the sender of the email answered in To. When
    that sender is the account's owner, who wrote to other people with the
    account in copy, the reply goes to those people and the owner stays in Cc,
    the role they gave themselves. ``None`` when nothing changes: another
    sender, an email to the account alone, or a draft without recipients.
    """
    owner = (owner or "").strip().lower()
    sender, _name = _sender(answered)
    if not owner or sender.lower() != owner:
        return None
    others = _addresses(answered.get("toRecipients")) - own - {owner}
    to = draft.get("toRecipients")
    if not others or not isinstance(to, list):
        return None
    owner_entries = [r for r in to if owner in _addresses([r])]
    if not owner_entries:
        return None
    kept = [r for r in to if owner not in _addresses([r])]
    if not kept:
        return None
    cc = [r for r in draft.get("ccRecipients") or [] if isinstance(r, dict)]
    return {"toRecipients": _recipients(kept), "ccRecipients": _recipients([*cc, *owner_entries])}


def _compose(draft_html: str, block: str) -> str:
    """The draft's HTML with *block* above its quoted thread, as Outlook places a reply."""
    separator = re.search(r"<hr\b[^>]*>", draft_html, re.IGNORECASE)
    if separator:
        return draft_html[: separator.start()] + block + draft_html[separator.start():]
    body = re.search(r"<body[^>]*>", draft_html, re.IGNORECASE)
    if body:
        return draft_html[: body.end()] + block + draft_html[body.end():]
    return block + draft_html


class MicrosoftOutlookProvider(MicrosoftGraphProvider):
    key = "microsoft_outlook"
    label = "Microsoft Outlook"
    platform = "email"
    kind = "mailbox"
    capabilities = ProviderCapabilities(
        direct=True, group=True, channel=False, push=True, poll=True, conversations=False, triage=True, signature=True
    )
    subscription_lifetime_minutes = SUBSCRIPTION_MINUTES
    subscription_headers = _WRITE_HEADERS
    chats_covers = "inbox"

    # -- the account --------------------------------------------------------

    async def _own_addresses(self, session: ProviderSession) -> frozenset[str]:
        """Every address the account receives mail at, lowercased: its mail, sign-in name and aliases."""
        account_id = session.account_external_id or ""
        cached = _addresses_of_accounts.get(account_id) if account_id else None
        if cached is not None:
            return cached
        try:
            me = await self._call(
                session, "GET", f"{GRAPH}/me", params={"$select": "mail,userPrincipalName,proxyAddresses"}
            ) or {}
        except ProviderError:
            # Not remembered: the next email asks again.
            return frozenset()
        addresses = {v.strip().lower() for v in (me.get("mail"), me.get("userPrincipalName")) if isinstance(v, str) and v.strip()}
        for proxy in me.get("proxyAddresses") or []:
            if isinstance(proxy, str) and proxy.lower().startswith("smtp:"):
                addresses.add(proxy[5:].strip().lower())
        found = frozenset(addresses)
        if account_id and found:
            _addresses_of_accounts.put(account_id, found)
        return found

    async def _signature(self, session: ProviderSession) -> str:
        """The account's Outlook signature, from its recent sent mail; empty when it has none."""
        account_id = session.account_external_id or ""
        cached = _signatures.get(account_id) if account_id else None
        if cached is not None:
            return cached
        try:
            page = await self._call(
                session,
                "GET",
                f"{GRAPH}/me/mailFolders/sentitems/messages",
                params={"$top": str(SIGNATURE_SCAN), "$orderby": "sentDateTime desc", "$select": "body"},
            ) or {}
        except ProviderError as exc:
            logger.info("Could not read the sent mail of an agent account for its signature: %s", exc)
            return ""
        signature = ""
        fallback = ""
        for msg in page.get("value") or []:
            content = ((msg or {}).get("body") or {}).get("content") if isinstance(msg, dict) else None
            if not isinstance(content, str) or not (found := sent_mail_signature(content)):
                continue
            # Never another thread's quote nor that email's own cid: images,
            # which point at its attachments: a copy that had any lost its
            # images, so a sent email whose signature kept its links wins.
            if _CID_IMAGE.search(_extract_signature_block(content)):
                fallback = fallback or found
                continue
            signature = found
            break
        signature = signature or fallback
        if account_id:
            _signatures.put(account_id, signature)
        return signature

    async def _reply_signature(self, session: ProviderSession) -> str:
        """The signature the owner chose for the account's replies, as Outlook's block."""
        chosen = session.signature or AccountSignature()
        if chosen.source == "none":
            return ""
        if chosen.source == "given":
            given = signature_block(chosen.html)
            if given:
                return given
        return await self._signature(session)

    # -- normalization ------------------------------------------------------

    async def _attachment_names(self, session: ProviderSession, message_id: str) -> list[str]:
        try:
            page = await self._call(
                session,
                "GET",
                f"{GRAPH}/me/messages/{_segment(message_id, 'message id')}/attachments",
                params={"$select": "name,isInline", "$top": str(MAX_ATTACHMENT_NAMES)},
                headers=_WRITE_HEADERS,
            ) or {}
        except ProviderError:
            return []
        return [
            str(item["name"])
            for item in page.get("value") or []
            if isinstance(item, dict) and isinstance(item.get("name"), str) and not item.get("isInline")
        ]

    async def _mail_event(self, session: ProviderSession, msg: dict) -> AccountEvent | None:
        message_id = str(msg.get("id") or "")
        heard = _heard_type(msg)
        if not message_id or msg.get("isDraft") or heard is None:
            return None
        own = await self._own_addresses(session)
        people = _people(msg)
        if heard == "calendar":
            # Who acted on the calendar: the delegate of "X on behalf of Y" (``sender``) first.
            people.sort(key=lambda person: 0 if person[0].lower() in _addresses([msg.get("sender")]) else 1)
        address, name = people[0] if people else ("", "")
        to = _addresses(msg.get("toRecipients"))
        others = (to | _addresses(msg.get("ccRecipients"))) - own
        addressed = bool(to & own)
        headers = _headers(msg)
        text = _message_text(msg)
        if msg.get("hasAttachments"):
            names = await self._attachment_names(session, message_id)
            if names:
                text = f"{text}\n\nAttachments: {', '.join(names)}"
        conversation_id = str(msg.get("conversationId") or message_id)
        return AccountEvent(
            provider=self.key,
            conversation_type="direct" if addressed and not others else "group",
            conversation_id=conversation_id,
            thread_id=conversation_id,
            message_id=message_id,
            sender_id=address.lower(),
            sender_name=name,
            sender_email=address or None,
            sender_emails=tuple(a for a, _name in people),
            text=text,
            created_at=_parse_time(msg.get("receivedDateTime") or msg.get("sentDateTime")),
            conversation_name=str(msg.get("subject") or "").strip()[:500] or None,
            mentions_account=addressed,
            from_account=bool(address) and address.lower() in own,
            from_application=not address or _automated(headers),
            from_agent_account=AGENT_HEADER.lower() in headers,
            reply_recipients=len(others - {address.lower()}),
            route={"kind": "mail", "message_id": message_id, "reply_all": bool(others)},
            internet_message_id=str(msg.get("internetMessageId") or "").strip() or None,
        )

    async def _read_message(self, session: ProviderSession, message_id: str) -> dict | None:
        return await self._call(
            session,
            "GET",
            f"{GRAPH}/me/messages/{_segment(message_id, 'message id')}",
            params={"$select": _MESSAGE_SELECT},
            headers=_READ_HEADERS,
            allow_missing=True,
        )

    # -- push ---------------------------------------------------------------

    async def desired_subscriptions(
        self, session: ProviderSession, plan: ListeningPlan, *, reactions: bool = False
    ) -> list[DesiredSubscription]:
        account_id = session.account_external_id
        if not account_id or not plan.chats:
            return []
        return [
            DesiredSubscription(
                f"/users/{_segment(account_id, 'user id')}/{_INBOX}", "created",
                covers=self.chats_covers, permission=MAIL_PERMISSION,
            )
        ]

    async def event_from_notification(
        self, session: ProviderSession, notification: InboundNotification
    ) -> AccountEvent | None:
        resource = notification.payload.get("resource")
        if not isinstance(resource, str) or notification.payload.get("changeType") not in (None, "created"):
            return None
        found = _MESSAGE_RESOURCE.search(resource)
        if not found:
            return None
        msg = await self._read_message(session, found.group(1) or found.group(2))
        return await self._mail_event(session, msg) if msg else None

    # -- poll ---------------------------------------------------------------

    async def poll(
        self, session: ProviderSession, plan: ListeningPlan, cursor: str | None
    ) -> tuple[list[AccountEvent], str | None]:
        if not plan.chats:
            return [], cursor
        if cursor is None:
            # A new cursor starts now: the mailbox's history is never replayed.
            return [], datetime.now(UTC).isoformat()
        since = _parse_time(cursor)
        page = await self._call(
            session,
            "GET",
            f"{GRAPH}/me/{_INBOX}",
            params={
                # ``ge``: an email received in the cursor's second is read again, and dropped by the dedup.
                "$filter": f"receivedDateTime ge {since.astimezone(UTC).strftime('%Y-%m-%dT%H:%M:%SZ')}",
                "$orderby": "receivedDateTime asc",
                "$top": str(POLL_MESSAGES),
                "$select": _MESSAGE_SELECT,
            },
            headers=_READ_HEADERS,
        ) or {}
        events: list[AccountEvent] = []
        newest = since
        for msg in page.get("value") or []:
            if not isinstance(msg, dict):
                continue
            newest = max(newest, _parse_time(msg.get("receivedDateTime")))
            event = await self._mail_event(session, msg)
            if event is not None:
                events.append(event)
        events.sort(key=lambda e: e.created_at)
        return events, newest.isoformat()

    # -- answering ----------------------------------------------------------

    async def _thread(
        self, session: ProviderSession, conversation_id: str, *, select: str, page: int
    ) -> list[dict]:
        """The emails of a thread, in no order: every page, up to ``THREAD_READ``.

        Graph refuses ``$orderby`` with a ``conversationId`` filter, so a long
        thread's newest emails may come on its last page; the caller sorts.
        """
        if not conversation_id or len(conversation_id) > 1000:
            return []
        return await self._collect(
            session,
            f"{GRAPH}/me/messages",
            {
                "$filter": f"conversationId eq '{_escaped(conversation_id)}'",
                "$select": select,
                "$top": str(page),
            },
            THREAD_READ,
            headers=_READ_HEADERS,
        )

    async def fetch_context(
        self, session: ProviderSession, event: AccountEvent, limit: int
    ) -> list[ContextMessage]:
        if limit <= 0:
            return []
        own = await self._own_addresses(session)
        out: list[ContextMessage] = []
        for msg in await self._thread(session, event.conversation_id, select=_CONTEXT_SELECT, page=CONTEXT_PAGE):
            if msg.get("isDraft") or str(msg.get("id") or "") == event.message_id:
                continue
            created = _parse_time(msg.get("receivedDateTime") or msg.get("sentDateTime"))
            if created > event.created_at:
                continue
            address, name = _sender(msg)
            out.append(
                ContextMessage(
                    message_id=str(msg.get("id") or ""),
                    sender_name=name or "Someone",
                    text=_message_text(msg),
                    created_at=created,
                    from_account=bool(address) and address.lower() in own,
                )
            )
        out.sort(key=lambda m: m.created_at)
        return out[-limit:]

    async def account_messages_since(self, session: ProviderSession, event: AccountEvent) -> list[str]:
        own = await self._own_addresses(session)
        if not own:
            return []
        written = []
        for msg in await self._thread(session, event.conversation_id, select=_HEADER_SELECT, page=HEADER_PAGE):
            address, _name = _sender(msg)
            sent = _parse_time(msg.get("sentDateTime") or msg.get("receivedDateTime"))
            if not msg.get("isDraft") and address.lower() in own and sent > event.created_at:
                written.append(str(msg.get("id") or ""))
        return written

    async def _draft_html(self, session: ProviderSession, draft_url: str, draft: dict[str, Any]) -> str:
        """The HTML of a reply draft, its native quote kept: re-read in HTML when Graph answered text."""
        body = draft.get("body") or {}
        if is_html_body(body):
            return str(body.get("content") or "")
        reread = await self._call(
            session, "GET", draft_url, params={"$select": "body"}, headers=_DRAFT_HEADERS, allow_missing=True
        )
        again = (reread or {}).get("body") or {}
        if again.get("contentType") and is_html_body(again) and again.get("content"):
            return str(again["content"])
        return text_body_as_html(str(body.get("content") or ""))

    async def _answered(self, session: ProviderSession, message_id: str) -> dict[str, Any]:
        """The email a reply answers: its thread, subject and people; empty when it cannot be read."""
        try:
            msg = await self._call(
                session,
                "GET",
                f"{GRAPH}/me/messages/{message_id}",
                params={"$select": _ANSWERED_SELECT},
                headers=_WRITE_HEADERS,
                allow_missing=True,
            )
        except ProviderError as exc:
            logger.info("Could not read the email an agent account answers: %s", exc)
            return {}
        return msg if isinstance(msg, dict) else {}

    async def _earlier_send(
        self, session: ProviderSession, conversation_id: str, *, now: float, window: int
    ) -> mail_send_guard.EarlierSend | None:
        """A message of the mailbox's Sent Items in *conversation_id*, sent within the window."""
        try:
            page = await self._call(
                session,
                "GET",
                f"{GRAPH}/me/mailFolders/sentitems/messages",
                params={
                    "$filter": f"sentDateTime ge {mail_send_guard.since_iso(now, window)}",
                    "$orderby": "sentDateTime desc",
                    "$top": str(_GUARD_SENT_PAGE),
                    "$select": "id,subject,toRecipients,ccRecipients,sentDateTime,conversationId,webLink",
                },
                headers=_WRITE_HEADERS,
            ) or {}
        except ProviderError as exc:
            # The guard's own record still holds; a failed read is no refusal.
            logger.info("Could not read the sent mail of an agent account for its send guard: %s", exc)
            return None
        for msg in page.get("value") or []:
            if not isinstance(msg, dict) or str(msg.get("conversationId") or "") != conversation_id:
                continue
            earlier = mail_send_guard.recent_send(
                sent_at=msg.get("sentDateTime"),
                subject=msg.get("subject"),
                recipients=sorted(_addresses(msg.get("toRecipients")) | _addresses(msg.get("ccRecipients"))),
                message_id=msg.get("id"),
                web_link=msg.get("webLink"),
                conversation_id=msg.get("conversationId"),
                now=now,
                window=window,
            )
            if earlier is not None:
                return earlier
        return None

    async def _inbound_since(
        self, session: ProviderSession, conversation_id: str, earlier: mail_send_guard.EarlierSend
    ) -> bool | None:
        """Whether someone other than the mailbox wrote in the conversation after *earlier*; ``None`` when unknown."""
        if earlier.sent_at is None:
            return None
        try:
            page = await self._call(
                session,
                "GET",
                f"{GRAPH}/me/messages",
                params={
                    "$filter": (
                        f"conversationId eq '{_escaped(conversation_id)}' and "
                        f"receivedDateTime gt {mail_send_guard.iso_instant(earlier.sent_at)}"
                    ),
                    "$top": "10",
                    # Subject and headers tell an automatic (out-of-office)
                    # reply, which is nothing new to answer.
                    "$select": "from,sender,receivedDateTime,isDraft,subject,internetMessageHeaders",
                },
                headers=_WRITE_HEADERS,
            ) or {}
        except ProviderError:
            return None
        own = await self._own_addresses(session)
        if not own:
            return None
        others = [m for m in page.get("value") or [] if isinstance(m, dict) and _sender(m)[0].lower() not in own]
        return mail_send_guard.inbound_after(others, after=earlier.sent_at, own_address=next(iter(own)))

    async def _guard(self, session: ProviderSession, answered: dict[str, Any]) -> mail_send_guard.SendGuard | None:
        """The mail tools' send guard on the conversation, for this mailbox; raises on a duplicate.

        Scoped like the mail tools' (``mailbox:`` and the integration id), so
        a tool send and an account reply of one mailbox share one lock and one
        record, and the mailbox's Sent Items are read when no record is found.
        An earlier send within the window is a duplicate unless someone other
        than the mailbox wrote in the conversation since. ``None`` when nothing
        is guarded (no integration, no conversation, the guard off).
        """
        conversation_id = str(answered.get("conversationId") or "").strip()
        if not session.integration_id or not conversation_id:
            return None
        window = mail_send_guard.window_seconds()
        guard = mail_send_guard.SendGuard(
            f"mailbox:{session.integration_id}", mail_send_guard.thread_key(conversation_id), window=window
        )
        if not guard.active:
            return None
        if not await guard.acquire():
            raise DuplicateReplyError(
                "Not sent: another send in this conversation from this mailbox is in progress right now "
                "(a mail tool or another reply of the account)."
            )
        now = time.time()
        earlier = await guard.recorded() or await self._earlier_send(session, conversation_id, now=now, window=window)
        if earlier is None:
            return guard
        moved_on = await self._inbound_since(session, conversation_id, earlier)
        if moved_on:
            return guard
        await guard.release()
        raise DuplicateReplyError(
            mail_send_guard.refusal(
                earlier, window=window, tool="", now=now, inbound_checked=moved_on is False, forceable=False
            )
        )

    async def send_reply(self, session: ProviderSession, route: dict[str, Any], text: str) -> str:
        message_id = _segment(str(route.get("message_id") or ""), "message id")
        answered = await self._answered(session, message_id)
        guard = await self._guard(session, answered)
        try:
            draft_id = await self._send_reply(session, route, message_id, answered, text)
        except BaseException:
            if guard is not None:
                await guard.release()
            raise
        if guard is not None:
            await guard.sent(
                subject=answered.get("subject"),
                recipients=sorted(_addresses(answered.get("toRecipients")) | _addresses(answered.get("ccRecipients"))),
                message_id=draft_id,
                conversation_id=answered.get("conversationId"),
            )
        return draft_id

    async def _send_reply(
        self, session: ProviderSession, route: dict[str, Any], message_id: str, answered: dict[str, Any], text: str
    ) -> str:
        reply_all = bool(route.get("reply_all"))
        url = f"{GRAPH}/me/messages/{message_id}/{'createReplyAll' if reply_all else 'createReply'}"
        marked = {"message": {"internetMessageHeaders": [{"name": AGENT_HEADER, "value": "reply"}]}}
        try:
            draft = await self._call(session, "POST", url, payload=marked, headers=_DRAFT_HEADERS) or {}
        except ProviderError as exc:
            if exc.kind != "error":
                raise
            # A mailbox refusing the header on a reply draft gets the reply without it.
            draft = await self._call(session, "POST", url, payload={}, headers=_DRAFT_HEADERS) or {}
        draft_id = draft.get("id")
        if not isinstance(draft_id, str) or not draft_id:
            raise ProviderError("Microsoft Graph created no reply draft.")
        draft_url = f"{GRAPH}/me/messages/{_segment(draft_id, 'message id')}"
        try:
            reply = f"<div>{build_graph_email_body(text)['content']}</div>"
            signature = await self._reply_signature(session)
            existing = await self._draft_html(session, draft_url, draft)
            # The sign-off keeps no bottom margin: the signature follows it
            # directly, as on every send path of the mail tools.
            block = (close_before_signature(reply) + f"\r\n{signature}\r\n") if signature else reply
            content = _compose(existing, block)
            if signature:
                # The signature's image links, which Outlook blocks, attached
                # once each as inline images.
                content, images, _note = await inline_signature_images(content, signature)
                for image in images:
                    await self._call(session, "POST", f"{draft_url}/attachments", payload=image, headers=_WRITE_HEADERS)
            patch: dict[str, Any] = {"body": {"contentType": "HTML", "content": content}}
            if reply_all and session.owner_email:
                roles = _owner_stays_in_cc(answered, draft, session.owner_email, await self._own_addresses(session))
                if roles:
                    patch.update(roles)
            await self._call(session, "PATCH", draft_url, payload=patch, headers=_WRITE_HEADERS)
            await self._call(session, "POST", f"{draft_url}/send", headers=_WRITE_HEADERS)
        except ProviderError:
            try:
                await self._call(session, "DELETE", draft_url, headers=_WRITE_HEADERS, allow_missing=True)
            except ProviderError:
                logger.info("Could not delete the unsent reply draft of an agent account")
            raise
        # An immutable id: the sent email keeps the draft's.
        return draft_id

    async def mark_message(self, session: ProviderSession, route: dict[str, Any], mark: MessageMark) -> None:
        if mark.empty or route.get("kind") != "mail":
            return
        url = f"{GRAPH}/me/messages/{_segment(str(route.get('message_id') or ''), 'message id')}"
        payload: dict[str, Any] = {}
        if mark.flag:
            payload["flag"] = {"flagStatus": "flagged"}
        if mark.read:
            payload["isRead"] = True
        if mark.category:
            current = await self._call(
                session, "GET", url, params={"$select": "categories"}, headers=_WRITE_HEADERS, allow_missing=True
            )
            if current is None:
                return
            categories = [c for c in current.get("categories") or [] if isinstance(c, str)]
            if mark.category.lower() not in {c.lower() for c in categories}:
                payload["categories"] = [*categories, mark.category]
        if payload:
            await self._call(session, "PATCH", url, payload=payload, headers=_WRITE_HEADERS, allow_missing=True)
