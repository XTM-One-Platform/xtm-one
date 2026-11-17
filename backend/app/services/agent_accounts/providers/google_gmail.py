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

"""Gmail as an agent-account provider: a mailbox, through the account's delegated Google token.

Listening: the mailbox's history (``users.history.list``) from the last
``historyId`` the account read, new inbox messages only. The cursor is that
``historyId``: a new cursor starts at the mailbox's current one, so its past
is never replayed. A cursor Gmail no longer holds (404: history is kept about
a week) is recovered by reading the inbox of the last
:data:`RESYNC_DAYS` days, oldest first, the pipeline's dedup dropping what
was already heard, then the cursor restarts at the current ``historyId``.

An email maps onto the engine's conversations as an Outlook one does: sent to
the account alone, a direct message; with other recipients, a group; the
account in ``To`` is the account addressed. Its thread is its ``threadId``.
Automated mail (RFC 3834 headers, ``List-*`` headers) and the Promotions,
Social, Updates and Forums categories are an application's: a rule answers
them only when it answers applications.

Answering: a reply in the thread (``threadId``, ``In-Reply-To`` and
``References`` from the answered email), carrying :data:`AGENT_HEADER`, with
the signature the owner chose: the account's own Gmail signature (its
primary send-as address's), one read from a signature integration, or none.
A personal mailbox never gets a signature it did not choose.

Triage (``triage.py``): ``mark_message`` stars the email (Gmail's follow-up
flag, seen on every Gmail client), removes ``UNREAD`` when asked and adds a
label, created when the mailbox has none of that name. Nothing is ever moved,
archived or deleted: ``INBOX`` is never removed.
"""

from __future__ import annotations

import base64
import json
import logging
import re
import time
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from email.utils import getaddresses, parseaddr
from typing import Any

from bs4 import BeautifulSoup

from app.core.egress import egress_route
from app.services import mail_send_guard
from app.services.agent_accounts.events import AccountEvent, AccountIdentity, ContextMessage
from app.services.agent_accounts.providers import (
    AccountProvider,
    AccountSignature,
    CreatedSubscription,
    DesiredSubscription,
    DuplicateReplyError,
    InboundNotification,
    MessageMark,
    ProviderCapabilities,
    ProviderError,
    ProviderSession,
)
from app.services.agent_accounts.rules import ListeningPlan
from app.services.email_signatures import text_to_html

logger = logging.getLogger(__name__)

GMAIL = "https://gmail.googleapis.com/gmail/v1/users/me"
#: The header every email an agent account sends carries (the Outlook provider's).
AGENT_HEADER = "X-XTM-One-Agent-Account"
#: How far back a lost history cursor is recovered.
RESYNC_DAYS = 7
#: Messages a poll reads at most (history entries and the resync alike).
POLL_MESSAGES = 50
#: Messages of a thread read at most for the context.
THREAD_READ = 100
TEXT_LIMIT = 12000
MAX_ATTACHMENT_NAMES = 20
#: Gmail's categories whose mail is an application's, not a person's.
APPLICATION_CATEGORIES = frozenset({"CATEGORY_PROMOTIONS", "CATEGORY_SOCIAL", "CATEGORY_UPDATES", "CATEGORY_FORUMS"})
#: Labels whose messages are never heard.
_UNHEARD_LABELS = frozenset({"DRAFT", "SPAM", "TRASH"})
_AUTOMATED_PRECEDENCE = frozenset({"bulk", "junk", "list"})
_LIST_HEADERS = ("list-id", "list-unsubscribe", "list-post")
_ID = re.compile(r"^[A-Za-z0-9_-]{1,256}$")
_KIND_BY_STATUS = {401: "reauthorize", 403: "refused", 404: "not_found", 410: "not_found", 429: "throttled"}
#: The integration settings that switch push on (``users.watch`` to a Pub/Sub topic).
PUBSUB_TOPIC_KEY = "gmail_pubsub_topic"
PUSH_SERVICE_ACCOUNT_KEY = "gmail_push_service_account"
_TOPIC = re.compile(r"^projects/[a-z][a-z0-9-]{4,28}[a-z0-9]/topics/[A-Za-z][A-Za-z0-9._~%+-]{2,254}$")
#: The resource of the one watch a mailbox holds: its inbox.
WATCH_RESOURCE = "INBOX"
#: The labels a watch and a history read follow: what arrives, and what the owner sends.
HEARD_LABELS = ("INBOX", "SENT")
_WATCH_PREFIX = "gmail-watch:"
_GOOGLE_ISSUERS = frozenset({"https://accounts.google.com", "accounts.google.com"})
_JWKS_TIMEOUT_SECONDS = 5


def watch_id(address: str) -> str:
    """The id a mailbox's watch is recorded under: Gmail gives a watch none, one per mailbox."""
    return f"{_WATCH_PREFIX}{address.strip().lower()}"


def _expiry(answer: dict[str, Any]) -> datetime:
    """A watch's expiry from Gmail's ``expiration`` (epoch milliseconds); 7 days when absent."""
    try:
        return datetime.fromtimestamp(int(str(answer.get("expiration"))) / 1000, UTC)
    except (TypeError, ValueError):
        return datetime.now(UTC) + timedelta(days=7)


async def verified_push_signer(authorization: str, audience: str) -> str:
    """The service account a Pub/Sub push's OIDC token names, lowercased; ``""`` unless Google signed it for *audience*.

    Never raises: an unverifiable delivery proves nothing and is dropped like
    one naming no subscription.
    """
    import jwt
    from jwt import PyJWKClientError

    from app.a2a.google_federation import google_jwks_url
    from app.core.jwks import resolve_signing_key
    from app.core.security import JWT_CLOCK_SKEW_LEEWAY

    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return ""
    token = token.strip()
    try:
        key = await resolve_signing_key(google_jwks_url(), token, timeout=_JWKS_TIMEOUT_SECONDS)
        claims = jwt.decode(
            token,
            key.key,
            algorithms=["RS256"],
            audience=audience,
            leeway=JWT_CLOCK_SKEW_LEEWAY,
            options={"require": ["exp", "iss", "aud"]},
        )
    except (PyJWKClientError, jwt.PyJWTError) as exc:
        logger.info("Gmail push refused: its token does not verify: %s", exc)
        return ""
    if claims.get("iss") not in _GOOGLE_ISSUERS or not claims.get("email_verified"):
        return ""
    return str(claims.get("email") or "").strip().lower()


def push_entries(body: bytes, signer: str) -> list[InboundNotification]:
    """The entry of one Pub/Sub push: the mailbox that changed, proven by *signer*."""
    try:
        envelope = json.loads(body or b"{}")
    except (ValueError, UnicodeDecodeError):
        raise ValueError("not JSON") from None
    message = envelope.get("message") if isinstance(envelope, dict) else None
    if not isinstance(message, dict):
        raise ValueError("no message")
    data = _decode(message.get("data"))
    try:
        change = json.loads(data) if data else {}
    except ValueError:
        raise ValueError("undecodable data") from None
    if not isinstance(change, dict):
        return []
    address = str(change.get("emailAddress") or "").strip().lower()
    if not address:
        return []
    return [
        InboundNotification(
            subscription_id=watch_id(address),
            client_state=signer,
            lifecycle_event=None,
            payload={"historyId": str(change.get("historyId") or "")},
        )
    ]


def _gmail_id(value: object, label: str) -> str:
    """A Gmail id safe in a URL path; raises on anything else."""
    text = str(value or "")
    if not _ID.match(text):
        raise ProviderError(f"Invalid Gmail {label}.")
    return text


def _gmail_error(resp: Any) -> ProviderError:
    try:
        error = resp.json().get("error") or {}
        message = error.get("message") or error.get("status")
    except Exception:
        message = None
    status = resp.status_code
    kind = _KIND_BY_STATUS.get(status) or ("unavailable" if status >= 500 else "error")
    return ProviderError(f"Gmail answered {status}" + (f": {message}" if message else ""), kind=kind)


def headers_of(msg: dict[str, Any]) -> dict[str, str]:
    """A message's headers by lowercased name, the first of each."""
    out: dict[str, str] = {}
    for header in ((msg.get("payload") or {}).get("headers")) or []:
        if isinstance(header, dict) and isinstance(header.get("name"), str):
            out.setdefault(header["name"].strip().lower(), str(header.get("value") or "").strip())
    return out


def _addresses(value: str) -> set[str]:
    return {a.strip().lower() for _n, a in getaddresses([value or ""]) if a and "@" in a}


def automated(headers: dict[str, str], labels: set[str]) -> bool:
    """Whether the email is an application's: RFC 3834 headers, a list, or an application category."""
    auto = headers.get("auto-submitted", "").lower()
    if auto and auto != "no":
        return True
    if headers.get("precedence", "").lower() in _AUTOMATED_PRECEDENCE:
        return True
    if any(h in headers for h in _LIST_HEADERS):
        return True
    return bool(labels & APPLICATION_CATEGORIES)


def _decode(data: object) -> str:
    if not isinstance(data, str) or not data:
        return ""
    try:
        return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", errors="replace")
    except (ValueError, TypeError):
        return ""


def _parts(payload: dict[str, Any]) -> list[dict[str, Any]]:
    out = [payload]
    for part in payload.get("parts") or []:
        if isinstance(part, dict):
            out.extend(_parts(part))
    return out


def body_text(msg: dict[str, Any]) -> str:
    """The message's text: its text/plain part, else its HTML part as text."""
    parts = _parts(msg.get("payload") or {})
    for mime in ("text/plain", "text/html"):
        for part in parts:
            if part.get("mimeType") == mime and not part.get("filename"):
                text = _decode((part.get("body") or {}).get("data"))
                if text:
                    if mime == "text/html":
                        text = BeautifulSoup(text, "html.parser").get_text("\n")
                    return re.sub(r"\n{3,}", "\n\n", text).strip()
    return str(msg.get("snippet") or "").strip()


def attachment_names(msg: dict[str, Any]) -> list[str]:
    names = [
        str(p["filename"])
        for p in _parts(msg.get("payload") or {})
        if isinstance(p.get("filename"), str) and p["filename"] and (p.get("body") or {}).get("attachmentId")
    ]
    return names[:MAX_ATTACHMENT_NAMES]


def _received(msg: dict[str, Any]) -> datetime:
    try:
        return datetime.fromtimestamp(int(str(msg.get("internalDate"))) / 1000, UTC)
    except (TypeError, ValueError):
        return datetime.now(UTC)


def _reply_subject(subject: str) -> str:
    return subject if subject.lower().startswith("re:") else f"Re: {subject}".strip()


class GmailProvider(AccountProvider):
    key = "google_gmail"
    label = "Gmail"
    integration_provider = "google"
    platform = "email"
    kind = "mailbox"
    capabilities = ProviderCapabilities(
        direct=True,
        group=True,
        channel=False,
        push=True,
        poll=True,
        people_search=False,
        conversations=False,
        triage=True,
        signature=True,
        push_wakes_poll=True,
        poll_fallback=True,
    )
    chats_covers = "inbox"
    #: A Gmail watch lapses after 7 days; the maintenance pass renews it a day before.
    subscription_lifetime_minutes = 7 * 24 * 60
    #: The Cloud Pub/Sub topic Gmail publishes to, and the service account the
    #: topic's push subscription signs its deliveries with. Both optional: an
    #: integration without them is polled, as before.
    session_config_keys = (PUBSUB_TOPIC_KEY, PUSH_SERVICE_ACCOUNT_KEY)
    #: The cursor is the mailbox's ``historyId`` the account read up to.
    cursor_label = "last history id"

    def required_config_keys(self, config: dict[str, str]) -> tuple[str, ...]:
        return ()

    def __init__(self) -> None:
        self._addresses: dict[str, tuple[float, frozenset[str]]] = {}
        self._signatures: dict[str, tuple[float, str]] = {}
        self._labels: dict[str, tuple[float, dict[str, str]]] = {}

    # -- HTTP ---------------------------------------------------------------

    async def _call(
        self,
        session: ProviderSession,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        payload: dict[str, Any] | None = None,
        allow_missing: bool = False,
    ) -> dict[str, Any] | None:
        """One Gmail call as the account; ``None`` for a 404 when *allow_missing*."""
        kwargs: dict[str, Any] = {}
        if params:
            kwargs["params"] = params
        if payload is not None:
            kwargs["json"] = payload
        try:
            with egress_route(session.route):
                async with session.client() as client:
                    resp = await client.request(method, url, **kwargs)
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError(f"Gmail could not be reached: {type(exc).__name__}", kind="unavailable") from None
        if resp.status_code == 404 and allow_missing:
            return None
        if resp.status_code >= 400:
            raise _gmail_error(resp)
        if resp.status_code == 204 or not resp.content:
            return {}
        try:
            data = resp.json()
        except ValueError:
            raise ProviderError("Gmail answered with a body that is not JSON.") from None
        return data if isinstance(data, dict) else {}

    # -- identity -----------------------------------------------------------

    async def _profile(self, session: ProviderSession) -> dict[str, Any]:
        return await self._call(session, "GET", f"{GMAIL}/profile") or {}

    async def identify(self, session: ProviderSession) -> AccountIdentity:
        address = str((await self._profile(session)).get("emailAddress") or "").strip().lower()
        if not address:
            raise ProviderError("Gmail did not say which mailbox the integration is signed in to.")
        return AccountIdentity(external_id=address, display_name=address, email=address)

    async def _send_as(self, session: ProviderSession) -> list[dict[str, Any]]:
        try:
            page = await self._call(session, "GET", f"{GMAIL}/settings/sendAs") or {}
        except ProviderError as exc:
            logger.info("Could not read the send-as addresses of a Gmail agent account: %s", exc)
            return []
        return [s for s in page.get("sendAs") or [] if isinstance(s, dict)]

    async def own_addresses(self, session: ProviderSession) -> frozenset[str]:
        """Every address the mailbox sends as, lowercased, the signed-in one included."""
        key = session.account_external_id or ""
        hit = self._addresses.get(key)
        if key and hit and hit[0] > time.monotonic():
            return hit[1]
        found = {key} if key else set()
        found |= {str(s["sendAsEmail"]).strip().lower() for s in await self._send_as(session) if s.get("sendAsEmail")}
        result = frozenset(a for a in found if a)
        if key and result:
            self._addresses[key] = (time.monotonic() + 600, result)
        return result

    async def _own_signature(self, session: ProviderSession) -> str:
        """The account's Gmail signature: its primary send-as address's; empty without one."""
        key = session.account_external_id or ""
        hit = self._signatures.get(key)
        if key and hit and hit[0] > time.monotonic():
            return hit[1]
        entries = await self._send_as(session)
        primary = next((s for s in entries if s.get("isPrimary")), entries[0] if entries else {})
        signature = str(primary.get("signature") or "").strip()
        if key:
            self._signatures[key] = (time.monotonic() + 600, signature)
        return signature

    async def reply_signature(self, session: ProviderSession) -> str:
        """The signature the owner chose for the account's replies, as markup; empty for none."""
        chosen = session.signature or AccountSignature()
        if chosen.source == "none":
            return ""
        if chosen.source == "given":
            return chosen.html.strip()
        return await self._own_signature(session)

    # -- reading ------------------------------------------------------------

    async def _message(self, session: ProviderSession, message_id: str) -> dict[str, Any] | None:
        return await self._call(
            session,
            "GET",
            f"{GMAIL}/messages/{_gmail_id(message_id, 'message id')}",
            params={"format": "full"},
            allow_missing=True,
        )

    async def mail_event(self, session: ProviderSession, msg: dict[str, Any]) -> AccountEvent | None:
        """The engine's event for one Gmail message; ``None`` for what is never heard."""
        message_id = str(msg.get("id") or "")
        labels = {str(label) for label in msg.get("labelIds") or []}
        if not message_id or labels & _UNHEARD_LABELS:
            return None
        own = await self.own_addresses(session)
        headers = headers_of(msg)
        name, address = parseaddr(headers.get("from", ""))
        address = address.strip()
        to = _addresses(headers.get("to", ""))
        others = (to | _addresses(headers.get("cc", ""))) - own
        addressed = bool(to & own)
        subject = headers.get("subject", "").strip()
        body = body_text(msg)
        text = f"Subject: {subject}\n\n{body}" if subject and body else subject or body
        names = attachment_names(msg)
        if names:
            text = f"{text}\n\nAttachments: {', '.join(names)}"
        thread_id = str(msg.get("threadId") or message_id)
        return AccountEvent(
            provider=self.key,
            conversation_type="direct" if addressed and not others else "group",
            conversation_id=thread_id,
            thread_id=thread_id,
            message_id=message_id,
            sender_id=address.lower(),
            sender_name=name or address,
            sender_email=address or None,
            sender_emails=(address,) if address else (),
            text=text[:TEXT_LIMIT],
            created_at=_received(msg),
            conversation_name=subject[:500] or None,
            mentions_account=addressed,
            from_account=bool(address) and address.lower() in own,
            from_application=not address or automated(headers, labels),
            from_agent_account=AGENT_HEADER.lower() in headers,
            reply_recipients=len(others - {address.lower()}),
            route={"kind": "mail", "message_id": message_id, "thread_id": thread_id, "reply_all": bool(others)},
            internet_message_id=headers.get("message-id") or None,
        )

    # -- poll ---------------------------------------------------------------

    async def _history(self, session: ProviderSession, cursor: str) -> tuple[list[str], str] | None:
        """The ids of the inbox and sent messages added since *cursor*, oldest first, and the newest history id.

        A history read follows every label (``history.list`` takes one label
        only): an added message is kept when Gmail says it carries one of
        :data:`HEARD_LABELS`, or says nothing of its labels. ``None`` when
        Gmail no longer holds *cursor* (404).
        """
        ids: list[str] = []
        newest = cursor
        page_token: str | None = None
        while len(ids) < POLL_MESSAGES:
            params: dict[str, Any] = {
                "startHistoryId": cursor,
                "historyTypes": "messageAdded",
                "maxResults": 100,
            }
            if page_token:
                params["pageToken"] = page_token
            page = await self._call(session, "GET", f"{GMAIL}/history", params=params, allow_missing=True)
            if page is None:
                return None
            newest = str(page.get("historyId") or newest)
            for entry in page.get("history") or []:
                for added in (entry or {}).get("messagesAdded") or []:
                    message = (added or {}).get("message") or {}
                    msg_id = str(message.get("id") or "")
                    labels = message.get("labelIds")
                    if isinstance(labels, list) and not set(labels) & set(HEARD_LABELS):
                        continue
                    if msg_id and msg_id not in ids:
                        ids.append(msg_id)
            page_token = page.get("nextPageToken")
            if not page_token:
                break
        return ids[:POLL_MESSAGES], newest

    async def _recent_inbox(self, session: ProviderSession) -> list[str]:
        """The inbox messages of the last :data:`RESYNC_DAYS` days, oldest first, at most :data:`POLL_MESSAGES`."""
        page = (
            await self._call(
                session,
                "GET",
                f"{GMAIL}/messages",
                params={"labelIds": "INBOX", "q": f"newer_than:{RESYNC_DAYS}d", "maxResults": POLL_MESSAGES},
            )
            or {}
        )
        ids = [str(m.get("id")) for m in page.get("messages") or [] if isinstance(m, dict) and m.get("id")]
        return list(reversed(ids))

    async def poll(
        self, session: ProviderSession, plan: ListeningPlan, cursor: str | None
    ) -> tuple[list[AccountEvent], str | None]:
        if not plan.chats:
            return [], cursor
        if cursor is None:
            # A new cursor starts now: the mailbox's history is never replayed.
            return [], str((await self._profile(session)).get("historyId") or "") or None
        found = await self._history(session, cursor)
        if found is None:
            # History lost: the last days of the inbox, deduplicated by the pipeline.
            logger.info("Gmail agent account: history cursor expired, reading the last %s days", RESYNC_DAYS)
            ids = await self._recent_inbox(session)
            newest = str((await self._profile(session)).get("historyId") or "") or cursor
            since = datetime.now(UTC) - timedelta(days=RESYNC_DAYS)
        else:
            ids, newest = found
            since = None
        events: list[AccountEvent] = []
        for msg_id in ids:
            msg = await self._message(session, msg_id)
            # A message the owner sent is heard too: the pipeline raises the
            # owner-sent event for it (``from_account``), never a turn.
            if not msg or not set(msg.get("labelIds") or []) & set(HEARD_LABELS):
                continue
            event = await self.mail_event(session, msg)
            if event is not None and (since is None or event.created_at >= since):
                events.append(event)
        events.sort(key=lambda e: e.created_at)
        return events, newest

    # -- push ---------------------------------------------------------------

    @staticmethod
    def push_settings(session: ProviderSession) -> tuple[str, str] | None:
        """The Pub/Sub topic and push signer the integration names, or ``None`` when push is off."""
        topic = (session.config.get(PUBSUB_TOPIC_KEY) or "").strip()
        signer = (session.config.get(PUSH_SERVICE_ACCOUNT_KEY) or "").strip().lower()
        if not topic or not signer:
            return None
        if not _TOPIC.match(topic):
            raise ProviderError(
                f"The integration's {PUBSUB_TOPIC_KEY} is not a Pub/Sub topic name, such as projects/my-project/topics/gmail-inbox.",
                kind="refused",
            )
        return topic, signer

    async def desired_subscriptions(
        self, session: ProviderSession, plan: ListeningPlan, *, reactions: bool = False
    ) -> list[DesiredSubscription]:
        """One watch of the inbox when the rules hear it and the integration names a topic."""
        if not plan.chats or self.push_settings(session) is None:
            return []
        return [DesiredSubscription(resource=WATCH_RESOURCE, covers="inbox", permission="gmail.readonly")]

    async def _watch(self, session: ProviderSession) -> tuple[dict[str, Any], str] | None:
        settings_ = self.push_settings(session)
        if settings_ is None:
            return None
        topic, signer = settings_
        answer = await self._call(
            session,
            "POST",
            f"{GMAIL}/watch",
            payload={"topicName": topic, "labelIds": list(HEARD_LABELS), "labelFilterBehavior": "include"},
        )
        return answer or {}, signer

    async def create_subscription(
        self,
        session: ProviderSession,
        resource: str,
        *,
        notification_url: str,
        lifecycle_url: str,
        client_state: str,
        change_type: str = "created",
    ) -> CreatedSubscription:
        """``users.watch`` on the inbox. Gmail echoes no secret: a delivery proves the push signer instead."""
        if resource != WATCH_RESOURCE or not session.account_external_id:
            raise ProviderError("Gmail watches the inbox of a known mailbox only.")
        watched = await self._watch(session)
        if watched is None:
            raise ProviderError("The integration names no Pub/Sub topic for Gmail push.", kind="refused")
        answer, signer = watched
        return CreatedSubscription(
            external_id=watch_id(session.account_external_id), expires_at=_expiry(answer), client_state=signer
        )

    async def renew_subscription(self, session: ProviderSession, external_id: str) -> datetime | None:
        """Calling ``users.watch`` again extends the mailbox's one watch; ``None`` once push is switched off."""
        watched = await self._watch(session)
        return None if watched is None else _expiry(watched[0])

    async def delete_subscription(self, session: ProviderSession, external_id: str) -> bool:
        await self._call(session, "POST", f"{GMAIL}/stop", payload={})
        return True

    async def parse_delivery(self, body: bytes, headers: Mapping[str, str], url: str) -> list[InboundNotification]:
        """A Pub/Sub push: its OIDC token is verified for *url*, the signer it names stands as the proof."""
        signer = await verified_push_signer(headers.get("authorization", ""), url)
        return push_entries(body, signer)

    # -- answering ----------------------------------------------------------

    async def _thread(self, session: ProviderSession, thread_id: str) -> list[dict[str, Any]]:
        thread = (
            await self._call(
                session,
                "GET",
                f"{GMAIL}/threads/{_gmail_id(thread_id, 'thread id')}",
                params={"format": "full"},
                allow_missing=True,
            )
            or {}
        )
        msgs = [m for m in thread.get("messages") or [] if isinstance(m, dict)][-THREAD_READ:]
        return sorted(msgs, key=_received)

    async def fetch_context(self, session: ProviderSession, event: AccountEvent, limit: int) -> list[ContextMessage]:
        own = await self.own_addresses(session)
        out: list[ContextMessage] = []
        for msg in await self._thread(session, event.thread_id):
            if str(msg.get("id")) == event.message_id:
                break
            if "DRAFT" in (msg.get("labelIds") or []):
                continue
            name, address = parseaddr(headers_of(msg).get("from", ""))
            out.append(
                ContextMessage(
                    message_id=str(msg.get("id")),
                    sender_name=name or address,
                    text=body_text(msg)[:TEXT_LIMIT],
                    created_at=_received(msg),
                    from_account=address.strip().lower() in own,
                )
            )
        return out[-limit:] if limit > 0 else []

    async def account_messages_since(self, session: ProviderSession, event: AccountEvent) -> list[str]:
        own = await self.own_addresses(session)
        return [
            str(msg.get("id"))
            for msg in await self._thread(session, event.thread_id)
            if _received(msg) > event.created_at
            and "DRAFT" not in (msg.get("labelIds") or [])
            and parseaddr(headers_of(msg).get("from", ""))[1].strip().lower() in own
        ]

    def compose_reply(
        self,
        answered: dict[str, Any],
        text: str,
        signature: str,
        own: frozenset[str],
        *,
        reply_all: bool,
        owner_email: str | None,
    ) -> EmailMessage:
        """The reply to *answered*: threaded, marked as an agent account's, signed with *signature* (markup)."""
        hdrs = headers_of(answered)
        sender = hdrs.get("reply-to") or hdrs.get("from", "")
        msg = EmailMessage()
        msg["To"] = sender
        if reply_all:
            skip = own | _addresses(sender)
            cc = [a for a in sorted(_addresses(hdrs.get("to", "")) | _addresses(hdrs.get("cc", ""))) if a not in skip]
            if owner_email and owner_email.lower() not in own and owner_email.lower() not in cc:
                cc.append(owner_email.lower())
            if cc:
                msg["Cc"] = ", ".join(cc)
        msg["Subject"] = _reply_subject(hdrs.get("subject", ""))
        parent = hdrs.get("message-id", "")
        if parent:
            msg["In-Reply-To"] = parent
            references = hdrs.get("references", "")
            msg["References"] = f"{references} {parent}".strip() if parent not in references else references
        msg[AGENT_HEADER] = "reply"
        plain = text.strip()
        html = text_to_html(plain)
        if signature:
            sig_text = BeautifulSoup(signature, "html.parser").get_text("\n").strip()
            plain = f"{plain}\n\n-- \n{sig_text}" if sig_text else plain
            html = f'{html}<br><div class="gmail_signature">{signature}</div>'
        msg.set_content(plain)
        msg.add_alternative(html, subtype="html")
        return msg

    async def _guard(self, session: ProviderSession, thread_id: str) -> mail_send_guard.SendGuard | None:
        """The mail tools' send guard on the thread, for this mailbox; raises on a duplicate within the window."""
        if not session.integration_id or not thread_id:
            return None
        window = mail_send_guard.window_seconds()
        guard = mail_send_guard.SendGuard(
            f"mailbox:{session.integration_id}", mail_send_guard.thread_key(thread_id), window=window
        )
        if not guard.active:
            return None
        if not await guard.acquire():
            raise DuplicateReplyError(
                "Not sent: another send in this conversation from this mailbox is in progress right now "
                "(a mail tool or another reply of the account)."
            )
        earlier = await guard.recorded()
        if earlier is None:
            return guard
        await guard.release()
        raise DuplicateReplyError(
            mail_send_guard.refusal(
                earlier, window=window, tool="", now=time.time(), inbound_checked=False, forceable=False
            )
        )

    async def send_reply(self, session: ProviderSession, route: dict[str, Any], text: str) -> str:
        message_id = _gmail_id(route.get("message_id"), "message id")
        answered = (
            await self._call(
                session,
                "GET",
                f"{GMAIL}/messages/{message_id}",
                params={
                    "format": "metadata",
                    "metadataHeaders": ["From", "Reply-To", "To", "Cc", "Subject", "Message-ID", "References"],
                },
            )
            or {}
        )
        thread_id = str(answered.get("threadId") or route.get("thread_id") or "")
        guard = await self._guard(session, thread_id)
        own = await self.own_addresses(session)
        try:
            reply = self.compose_reply(
                answered,
                text,
                await self.reply_signature(session),
                own,
                reply_all=bool(route.get("reply_all")),
                owner_email=session.owner_email,
            )
            raw = base64.urlsafe_b64encode(reply.as_bytes()).decode("ascii")
            payload: dict[str, Any] = {"raw": raw}
            if thread_id:
                payload["threadId"] = thread_id
            sent = await self._call(session, "POST", f"{GMAIL}/messages/send", payload=payload) or {}
        except BaseException:
            if guard is not None:
                await guard.release()
            raise
        sent_id = str(sent.get("id") or "")
        if not sent_id:
            raise ProviderError("Gmail sent no message.")
        if guard is not None:
            hdrs = headers_of(answered)
            await guard.sent(
                subject=hdrs.get("subject"),
                recipients=sorted(_addresses(hdrs.get("to", "")) | _addresses(hdrs.get("cc", ""))),
                message_id=sent_id,
                conversation_id=thread_id,
            )
        return sent_id

    # -- triage -------------------------------------------------------------

    async def _label_id(self, session: ProviderSession, name: str) -> str | None:
        """The id of the user label *name*, created when the mailbox has none of that name."""
        key = session.account_external_id or ""
        hit = self._labels.get(key)
        labels = hit[1] if hit and hit[0] > time.monotonic() else None
        if labels is None:
            page = await self._call(session, "GET", f"{GMAIL}/labels") or {}
            labels = {
                str(item["name"]).lower(): str(item["id"])
                for item in page.get("labels") or []
                if isinstance(item, dict) and item.get("name") and item.get("id")
            }
            self._labels[key] = (time.monotonic() + 600, labels)
        found = labels.get(name.lower())
        if found:
            return found
        created = (
            await self._call(
                session,
                "POST",
                f"{GMAIL}/labels",
                payload={"name": name, "labelListVisibility": "labelShow", "messageListVisibility": "show"},
            )
            or {}
        )
        label_id = str(created.get("id") or "")
        if label_id:
            labels[name.lower()] = label_id
        return label_id or None

    async def mark_message(self, session: ProviderSession, route: dict[str, Any], mark: MessageMark) -> None:
        if mark.empty or route.get("kind") != "mail":
            return
        message_id = _gmail_id(route.get("message_id"), "message id")
        add: list[str] = []
        remove: list[str] = []
        if mark.flag:
            add.append("STARRED")
        if mark.read:
            remove.append("UNREAD")
        if mark.category:
            label_id = await self._label_id(session, mark.category)
            if label_id:
                add.append(label_id)
        if add or remove:
            # Never INBOX among the removed: nothing is moved or archived.
            await self._call(
                session,
                "POST",
                f"{GMAIL}/messages/{message_id}/modify",
                payload={"addLabelIds": add, "removeLabelIds": remove},
                allow_missing=True,
            )
