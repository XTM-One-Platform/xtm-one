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

"""The providers of agent accounts: what a messaging platform must offer.

A provider reaches the platform AS the account, through the account's own
integration (a delegated OAuth sign-in, or the stored token of a business
number: ``AccountProvider.credential``), and translates between the platform and the
generic shapes of :mod:`..events`. Everything else - the rules, the grace
period, the approvals, the subscription bookkeeping, the notification
endpoint's verification - is the engine's and never re-implemented here.

Adding a platform is one module subclassing :class:`AccountProvider` and one
line in :data:`PROVIDERS`.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, ClassVar

from app.core.egress import EgressRoute
from app.integrations.http import AutoRefreshClient
from app.services.agent_accounts.events import (
    AccountAttachment,
    AccountEvent,
    AccountIdentity,
    ContextMessage,
    ConversationOption,
    PersonOption,
)
from app.services.agent_accounts.rules import ListeningPlan
from app.services.reaction_feedback import PlatformReaction

#: What can be done about a failed provider call: sign the integration
#: in again, get a permission or an access granted, drop a conversation that
#: is gone, or wait - the provider slowed the platform down or did not answer.
ERROR_KINDS = ("reauthorize", "refused", "not_found", "throttled", "unavailable", "error")


class ProviderError(Exception):
    """A provider call that failed, said in words a person can act on.

    ``kind`` (:data:`ERROR_KINDS`) is read by the provider from the status of
    its own answer, never from its words, which are kept as the message for
    the technical details.
    """

    def __init__(self, message: str, *, kind: str = "error") -> None:
        super().__init__(message)
        self.kind = kind if kind in ERROR_KINDS else "error"


class DuplicateReplyError(ProviderError):
    """A reply refused because the mailbox already answered the conversation (``mail_send_guard``).

    Nothing was sent: the same mailbox (the mail tools of a run, or another
    turn) sent a message in the conversation within the guard's window and no
    message from anyone else arrived since. The pipeline leaves the message
    instead of recording a failure.
    """


@dataclass(frozen=True)
class ProviderCapabilities:
    """What a provider supports, read by the API and the editor."""

    direct: bool = True
    group: bool = True
    channel: bool = True
    push: bool = True
    poll: bool = False
    people_search: bool = True
    #: Reactions on the account's answers can be read.
    reactions: bool = False
    #: The messages of a channel thread can be read (``thread_messages_since``).
    threads: bool = False
    #: A rule can name specific conversations, picked from ``list_conversations``.
    conversations: bool = True
    #: Every answering turn triages its message - reply, hand to the owner, no
    #: reply needed (``triage.py``) - and the provider marks it (``mark_message``).
    triage: bool = False
    #: What the account sends carries a signature, and its owner chooses which
    #: (``AgentAccount.signature_source``).
    signature: bool = False
    #: Inbound messages reach the platform through the integration's own webhook
    #: and are handed to the account as they are stored, with no subscription and
    #: no poll (WhatsApp): the account listens in real time as
    #: soon as its rules cover a chat.
    inbound_webhook: bool = False
    #: The sender of a message is the signed-in author the platform
    #: authenticated, read from its directory (a Teams user), not an address
    #: the sender wrote (an email's ``From``): only then may a turn trust that
    #: its owner is the one writing (``pipeline.sender_is_owner``).
    verified_sender: bool = False
    #: A push delivery says only that the mailbox changed (Gmail through Cloud
    #: Pub/Sub): it wakes a read of what arrived since the account's cursor
    #: (``poll``) instead of naming one message (``event_from_notification``).
    push_wakes_poll: bool = False
    #: The account is still polled while push hears it, at most every
    #: ``listening.FALLBACK_POLL_SECONDS``: a lost push costs minutes, never a message.
    poll_fallback: bool = False


#: Where an account's signature comes from: ``own`` - the provider
#: finds the account's own (an Outlook mailbox: the signature of its sent
#: mail); ``integration`` - read from a signature integration when each reply
#: is sent; ``none`` - none, the mail server signs.
SIGNATURE_SOURCES = ("own", "integration", "none")


@dataclass(frozen=True)
class AccountSignature:
    """The signature a reply of the account carries, as the pipeline resolved it.

    ``source`` ``own`` leaves it to the provider; ``given`` is :attr:`html`
    (a signature service's, its reply variant), for the provider to place;
    ``none`` sends none.
    """

    source: str = "own"
    html: str = ""


@dataclass
class ProviderSession:
    """A call context acting as the account: its token, its refresh hook, its route."""

    token: str
    refresh: Callable[[], Awaitable[str | None]] | None = None
    route: EgressRoute | None = None
    #: The account's id on the platform, once known.
    account_external_id: str | None = None
    #: The signature its next reply carries (``pipeline._post`` sets it).
    signature: AccountSignature | None = None
    #: The integration's settings the provider asked for (``AccountProvider.session_config_keys``).
    config: dict[str, str] = field(default_factory=dict)
    #: The integration the account sends through: the mailbox the mail tools'
    #: send guard is scoped to (``mail_send_guard``), so a tool send and an
    #: account reply of one mailbox share one guard.
    integration_id: str | None = None
    #: The account owner's address (``pipeline._post`` sets it): a reply-all
    #: to an email the owner wrote keeps the owner in Cc.
    owner_email: str | None = None
    #: The id of the inbound message the next reply answers (``pipeline._post``
    #: sets it): a Teams chat reply that lands after a newer message quotes it.
    answers_message_id: str | None = None
    #: What the provider did to the last reply it posted that the pipeline
    #: records (a Teams reply: the ``@`` names it mentioned and those it left as text).
    delivery: dict[str, Any] = field(default_factory=dict)
    headers: dict[str, str] = field(init=False)

    def __post_init__(self) -> None:
        self.headers = {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}

    def client(self, timeout: int = 30) -> AutoRefreshClient:
        return AutoRefreshClient(self.headers, self.refresh, timeout=timeout)

    def as_account(self, external_id: str) -> ProviderSession:
        """The same session, knowing the account's id (its token as refreshed so far)."""
        return ProviderSession(
            token=self.headers["Authorization"].removeprefix("Bearer "),
            refresh=self.refresh,
            route=self.route,
            account_external_id=external_id,
            signature=self.signature,
            config=dict(self.config),
            integration_id=self.integration_id,
            owner_email=self.owner_email,
            answers_message_id=self.answers_message_id,
        )


@dataclass(frozen=True)
class DesiredSubscription:
    """A push subscription the account needs, as the provider names its resource."""

    resource: str
    #: What it is notified of, as the provider spells it: another value is another subscription.
    change_type: str = "created"
    #: What it hears, as its owner sees it: ``chats`` (every chat of the account),
    #: ``inbox`` (a mailbox's), or ``channel``.
    covers: str = "channel"
    #: The conversation's name, when it has one.
    name: str | None = None
    #: The permission the provider needs for it, as the provider's administrators name it.
    permission: str | None = None
    #: What it is notified of when the provider refuses *change_type*: the new
    #: messages alone, the reactions then read periodically.
    fallback_change_type: str | None = None


@dataclass(frozen=True)
class UnheardConversation:
    """A conversation the rules cover that the account does not subscribe to, and why.

    A rule answering in every channel follows the channels the provider
    lists, up to a cap; a channel past it, or of a team whose channels
    cannot be listed, is reported instead of left out without a word.
    """

    name: str
    #: Why, in the platform's words, for the logs and the account's last error.
    detail: str
    #: ``channel``, or ``team`` for every channel of a team named by *name*.
    covers: str = "channel"
    #: The same reason as a code the frontend words in the viewer's language
    #: (``SYNC_PROBLEM_NOTES``), with the values it reads.
    note: str | None = None
    note_values: dict[str, str | int] | None = None


#: A ``[[FILE:id]]`` marker an agent writes in its answer to name a file of its run.
FILE_MARKER = re.compile(
    r"\[\[FILE:([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\]\]", re.IGNORECASE
)


def split_file_markers(text: str) -> tuple[str, list[str]]:
    """*text* without its ``[[FILE:id]]`` markers, and the ids they name in order, each once."""
    ids: list[str] = []
    for match in FILE_MARKER.finditer(text or ""):
        file_id = match.group(1).lower()
        if file_id not in ids:
            ids.append(file_id)
    if not ids:
        return text, []
    stripped = FILE_MARKER.sub("", text)
    stripped = re.sub(r"[ \t]+\n", "\n", stripped)
    return re.sub(r"\n{3,}", "\n\n", stripped).strip(), ids


def undelivered_file_line(name: str) -> str:
    """The plain line a reply carries for a file its platform could not deliver."""
    return f"File not attached: {name}"


def with_file_lines(text: str, names: list[str]) -> str:
    """*text* followed by one :func:`undelivered_file_line` per name."""
    lines = [undelivered_file_line(name) for name in names]
    return "\n\n".join(part for part in [text.strip(), "\n".join(lines)] if part)


@dataclass(frozen=True)
class ReplyFile:
    """A file of the agent's run its answer names, to send with the reply."""

    name: str
    content_type: str
    data: bytes


@dataclass(frozen=True)
class MessageMark:
    """What the account files on a message it triaged: a follow-up flag, read, a category."""

    flag: bool = False
    read: bool = False
    category: str | None = None

    @property
    def empty(self) -> bool:
        return not (self.flag or self.read or self.category)


@dataclass(frozen=True)
class UpdatedMessage:
    """A message a notification says changed (edited, reacted to): where it is, nothing more."""

    conversation_id: str
    message_id: str


@dataclass(frozen=True)
class CreatedSubscription:
    external_id: str
    expires_at: datetime
    #: What a delivery of this subscription proves, when the provider cannot echo
    #: the random ``clientState`` it was given (Gmail: the push service account a
    #: verified delivery token names). ``None`` keeps the random one.
    client_state: str | None = None


@dataclass(frozen=True)
class ExistingSubscription:
    """A subscription the provider holds for the signed-in account, recorded here or not."""

    external_id: str
    resource: str
    notification_url: str


@dataclass(frozen=True)
class InboundNotification:
    """One entry of a push delivery, before it is trusted."""

    subscription_id: str
    client_state: str
    #: A lifecycle event (``reauthorizationRequired``, ``subscriptionRemoved``, ``missed``), or None.
    lifecycle_event: str | None
    payload: dict[str, Any]


class AccountProvider(ABC):
    """A messaging platform an agent account can operate."""

    key: ClassVar[str]
    label: ClassVar[str]
    #: The ``Integration.provider`` the account signs in with.
    integration_provider: ClassVar[str]
    #: The ``platform`` the agent's message context names (output format, wording).
    platform: ClassVar[str]
    #: ``chat`` or ``mailbox``: how the editor words conversations, and the presets (``rules.preset``).
    kind: ClassVar[str] = "chat"
    capabilities: ClassVar[ProviderCapabilities] = ProviderCapabilities()
    #: How long a subscription lives before it must be renewed.
    subscription_lifetime_minutes: ClassVar[int] = 0
    #: What the subscription to the account's direct and group conversations
    #: ``covers`` (``DesiredSubscription.covers``): ``chats``, or a mailbox's ``inbox``.
    chats_covers: ClassVar[str] = "chats"
    #: The integration credential the session acts with: ``oauth`` (the
    #: signed-in account's access token, refreshed) or ``api_key`` (a token the
    #: integration stores, never refreshed: a business number's system user).
    credential: ClassVar[str] = "oauth"
    #: Keys of the integration's extra config the session carries in ``config``.
    session_config_keys: ClassVar[tuple[str, ...]] = ()
    #: What the poll cursor (``poll``) is, as ``get_agent_account`` words it.
    cursor_label: ClassVar[str] = "read cursor"
    #: The line of the account's brief that says how a reply mentions someone
    #: (``conversation.account_brief``); empty when the platform has no mentions.
    reply_mention_hint: ClassVar[str] = ""

    def required_config_keys(self, config: dict[str, str]) -> tuple[str, ...]:
        """The keys of ``session_config_keys`` the session cannot work without, given *config*.

        Every one by default; a provider with several backends (WhatsApp: the
        Cloud API or a linked-device gateway) needs some on one alone.
        """
        return self.session_config_keys

    def config_label(self, config: dict[str, str]) -> str:
        """What a refused session names besides the missing keys (a backend), or ``""``."""
        return ""

    async def verify_session(self, session: ProviderSession) -> None:
        """Check at every sync that the session can still act; raise :class:`ProviderError` when not.

        Nothing by default: ``identify`` checks it at the first sync. A provider
        whose account can lose its link without its token changing (a linked
        device unpaired) checks it here.
        """
        return None

    # -- identity -----------------------------------------------------------

    @abstractmethod
    async def identify(self, session: ProviderSession) -> AccountIdentity:
        """Who the token belongs to."""

    # -- listening ----------------------------------------------------------

    async def desired_subscriptions(
        self, session: ProviderSession, plan: ListeningPlan, *, reactions: bool = False
    ) -> list[DesiredSubscription]:
        """The push subscriptions *plan* needs; none for a provider without push.

        With *reactions*, the subscriptions that can also hear changes to the
        account's answers (a reaction) ask for them.
        """
        return []

    async def desired_listening(
        self, session: ProviderSession, plan: ListeningPlan, *, reactions: bool = False
    ) -> tuple[list[DesiredSubscription], list[UnheardConversation]]:
        """The push subscriptions *plan* needs, and the conversations it covers that none of them hears."""
        return await self.desired_subscriptions(session, plan, reactions=reactions), []

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
        raise ProviderError(f"{self.label} has no push subscriptions.")

    async def renew_subscription(self, session: ProviderSession, external_id: str) -> datetime | None:
        """The new expiry, or ``None`` when the subscription no longer exists."""
        return None

    async def delete_subscription(self, session: ProviderSession, external_id: str) -> bool:
        return True

    async def list_subscriptions(self, session: ProviderSession) -> list[ExistingSubscription]:
        """The subscriptions this application holds for the signed-in account."""
        return []

    def subscription_slot(self, resource: str) -> str:
        """What *resource* occupies at the provider: two resources of one slot are one subscription.

        A provider granting one subscription per conversation and application
        gives the conversation, whatever filter a resource adds to it.
        """
        return resource

    def handshake(self, query: dict[str, str]) -> str | None:
        """The body that validates a new notification URL, when *query* is that check."""
        return None

    def parse_notifications(self, body: bytes) -> list[InboundNotification]:
        """The entries of one push delivery; raises ``ValueError`` on a malformed body."""
        return []

    async def parse_delivery(self, body: bytes, headers: Mapping[str, str], url: str) -> list[InboundNotification]:
        """The entries of one push delivery to *url*, *headers* lower-cased.

        Each entry's ``client_state`` is what the delivery proves, compared with
        the one its subscription recorded (``listening.accept_notifications``).
        By default the body echoes it (``parse_notifications``); a provider whose
        deliveries are signed instead puts the signer it verified there.
        """
        return self.parse_notifications(body)

    async def event_from_notification(
        self, session: ProviderSession, notification: InboundNotification
    ) -> AccountEvent | None:
        """The message a trusted notification names, read from the platform."""
        return None

    async def poll(
        self, session: ProviderSession, plan: ListeningPlan, cursor: str | None
    ) -> tuple[list[AccountEvent], str | None]:
        """Messages since *cursor*, oldest first, and the next cursor."""
        return [], cursor

    async def thread_messages_since(
        self, session: ProviderSession, route: dict[str, Any], since: datetime
    ) -> list[AccountEvent]:
        """The messages of the channel thread *route* names posted after *since*, oldest first.

        How a thread the account answered in is followed when the channel is
        heard for mentions only (``capabilities.threads``).
        """
        return []

    # -- the message answered ------------------------------------------

    def message_reference(self, event: AccountEvent) -> dict[str, str]:
        """The ids the platform's own tools take, besides ``message_id``, to act on *event*.

        Named as those tools name their arguments (a chat id, a channel and
        its thread root), so the turn can react to or quote the message it
        answers. Empty when the message id alone is enough.
        """
        return {}

    # -- reactions -----------------------------------------------------

    def updated_message(self, notification: InboundNotification) -> UpdatedMessage | None:
        """The message a trusted notification says changed, read from the notification alone.

        No platform call: most changes are not to the account's answers, and the
        engine looks the message up among them before reading anything.
        """
        return None

    def reactions_pushed(self, route: dict[str, Any], subscriptions: list[tuple[str, str]]) -> bool:
        """Whether a change to an answer posted where *route* says reaches one of
        *subscriptions* (``(resource, change_type)``); otherwise it is checked periodically."""
        return False

    async def read_reactions(
        self, session: ProviderSession, route: dict[str, Any], message_id: str
    ) -> list[PlatformReaction] | None:
        """Every reaction on the account's message *message_id*, emojis in Unicode.

        ``None`` when the message is gone.
        """
        return None

    # -- answering ----------------------------------------------------------

    @abstractmethod
    async def fetch_context(
        self, session: ProviderSession, event: AccountEvent, limit: int
    ) -> list[ContextMessage]:
        """The messages before *event* in its thread or chat, oldest first."""

    @abstractmethod
    async def account_messages_since(self, session: ProviderSession, event: AccountEvent) -> list[str]:
        """The ids of the messages the account wrote in the thread of *event* after it."""

    @abstractmethod
    async def send_reply(self, session: ProviderSession, route: dict[str, Any], text: str) -> str:
        """Post *text* where *route* says; the new message's id.

        A provider whose replies are signed (``capabilities.signature``) signs
        it as ``session.signature`` says.
        """

    async def send_reply_files(
        self, session: ProviderSession, route: dict[str, Any], text: str, files: list[ReplyFile]
    ) -> str:
        """Post *text* with *files* attached where *route* says; the new message's id.

        A file that cannot be delivered is named by one plain line
        (:func:`undelivered_file_line`), never dropped without a word. The
        default, for a provider that does not deliver files, is that line for each.
        """
        return await self.send_reply(session, route, with_file_lines(text, [f.name for f in files]))

    async def download_attachment(
        self, session: ProviderSession, attachment: AccountAttachment, max_bytes: int
    ) -> tuple[bytes, str]:
        """The bytes of a file the message carries and the media type its download declared.

        Read as the account, never past *max_bytes*. Raises ``ProviderError``
        when it cannot be read: the agent then answers without it.
        """
        raise ProviderError(f"{self.label} does not hand a message's files to the agent.")

    async def mark_message(self, session: ProviderSession, route: dict[str, Any], mark: MessageMark) -> None:
        """File *mark* on the message *route* answers (``capabilities.triage``); nothing by default."""
        return None

    # -- editor -------------------------------------------------------------

    async def list_conversations(
        self, session: ProviderSession, query: str | None = None
    ) -> list[ConversationOption]:
        return []

    async def search_people(self, session: ProviderSession, query: str) -> list[PersonOption]:
        return []


def _registry() -> dict[str, AccountProvider]:
    from app.services.agent_accounts.providers.google_gmail import GmailProvider
    from app.services.agent_accounts.providers.microsoft_outlook import MicrosoftOutlookProvider
    from app.services.agent_accounts.providers.microsoft_teams import MicrosoftTeamsProvider
    from app.services.agent_accounts.providers.whatsapp import WhatsAppProvider

    providers: list[AccountProvider] = [
        MicrosoftTeamsProvider(),
        MicrosoftOutlookProvider(),
        GmailProvider(),
        WhatsAppProvider(),
    ]
    return {p.key: p for p in providers}


_PROVIDERS: dict[str, AccountProvider] | None = None


def providers() -> dict[str, AccountProvider]:
    """Every provider, by key."""
    global _PROVIDERS
    if _PROVIDERS is None:
        _PROVIDERS = _registry()
    return _PROVIDERS


def get_provider(key: str) -> AccountProvider | None:
    return providers().get(key)
