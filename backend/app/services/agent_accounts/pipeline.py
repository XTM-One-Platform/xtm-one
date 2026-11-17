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

"""From a message to an answer: the agent-account pipeline, the same for every provider.

``process_event`` is the one entry point, whatever brought the message (a push
notification, a poll, the end of a grace period). It reads the conversation
state, asks the rule engine, waits out a grace period without holding a
processing slot, re-checks that the account's human did not answer meanwhile,
replays the platform thread to the agent, and then posts, drafts for approval
or only records the answer, as the decision says. On a provider that triages
(a mailbox), the agent answers in a structure - reply, hand it to the
owner, or leave it - and the message is filed as the account's mailbox
actions say (``triage.py``); a reply it sends carries the signature its
owner chose (``_sign``). Whoever writes to the account never sets
what it spends nor whom it reaches: each turn takes one of the account's
hourly turns (``state.take_turn``), and a reply that would reach more people
than the account allows is left to the owner (``too_many_recipients``).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import uuid
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import async_session
from app.core.security import decrypt_value
from app.models.agent import Agent
from app.models.agent_account import AgentAccount, AgentAccountReply
from app.models.integration import Integration
from app.services import email_signatures, event_run_dedup, inbound_claims
from app.services.agent_accounts import (
    activity,
    cut_turn_checkpoint,
    sender_turns,
    state,
    triage,
    turns,
    voice_notes,
)
from app.services.agent_accounts.conversation import account_brief, build_turn, message_context
from app.services.agent_accounts.events import AccountEvent
from app.services.agent_accounts.providers import (
    AccountProvider,
    AccountSignature,
    DuplicateReplyError,
    MessageMark,
    ProviderError,
    ProviderSession,
    ReplyFile,
    get_provider,
    split_file_markers,
)
from app.services.agent_accounts.rules import (
    AccountSettings,
    Decision,
    EventState,
    apply_ai_label,
    evaluate,
    follows_channel_threads,
    hears_channel_in_full,
    label_text,
    listening_plan,
    rule_group_names,
)

logger = logging.getLogger(__name__)

#: The longest inbound message a reply record keeps.
INBOUND_TEXT_LIMIT = 8000
#: The bell's ``notification_source`` of a reply waiting for its owner's approval.
NOTIFICATION_SOURCE = "agent_account"


class AccountUnavailableError(Exception):
    """The account cannot act right now (no integration, a token it may not use...)."""


# ---------------------------------------------------------------------------
# Acting as the account
# ---------------------------------------------------------------------------


async def open_integration_session(
    db: AsyncSession,
    *,
    integration_id: uuid.UUID | None,
    owner_id: uuid.UUID,
    provider: AccountProvider,
    account_external_id: str | None = None,
) -> ProviderSession:
    """A provider session acting as the account an integration is signed in as.

    The integration must be the provider's, connected, and its token one
    *owner_id* may use - the rule every token reader follows
    (``integration_credentials``). The token is the OAuth access token, or
    the stored API key for a provider whose ``credential`` is ``api_key``;
    the extra config keys the provider names travel in ``session.config``.
    """
    from app.services.integration_tools import oauth_refresh_fn
    from app.services.integration_user_credentials import (
        holder_connected,
        holder_ref,
        reader_clause,
        resolve_token_holder,
    )
    from app.services.relays import integration_egress_route

    if integration_id is None:
        raise AccountUnavailableError("The account has no integration: connect one signed in as the account.")
    integration = (
        await db.execute(
            select(Integration).where(
                Integration.id == integration_id,
                reader_clause(owner_id),
            )
        )
    ).scalar_one_or_none()
    # The owner's own account on a per_user integration.
    holder = await resolve_token_holder(db, integration, owner_id) if integration is not None else None
    if integration is None or holder is None:
        raise AccountUnavailableError("The integration is gone, or its token may not be used by the account's owner.")
    if integration.provider != provider.integration_provider:
        raise AccountUnavailableError(f"The integration is not a {provider.label} integration.")
    # The holder's status: a person's own account works while the integration's is disconnected.
    if provider.credential == "api_key":
        return await _api_key_session(db, integration, holder, provider, account_external_id)
    if not holder_connected(holder) or not holder.access_token_enc:
        raise AccountUnavailableError("The integration is not connected: sign it in again.")
    return ProviderSession(
        token=decrypt_value(holder.access_token_enc),
        refresh=oauth_refresh_fn(holder_ref(holder)),
        route=await integration_egress_route(db, integration),
        account_external_id=account_external_id,
        config=await _session_config(holder, provider),
        integration_id=str(integration.id),
    )


async def _session_config(holder: Any, provider: AccountProvider) -> dict[str, str]:
    """The extra config keys *provider* asks for, as text, a vault reference resolved (#6080); empty when unreadable.

    A referenced value its vault cannot answer is refused: the account says so
    rather than call the provider without it.
    """
    from app.services.vault import SecretUnavailableError, reveal_json_field

    if not provider.session_config_keys or not getattr(holder, "extra_config_enc", None):
        return {}
    try:
        extra = await reveal_json_field(holder, "extra_config_enc")
    except SecretUnavailableError:
        raise AccountUnavailableError(
            "A setting of the integration is read from a vault that did not answer: check the reference."
        ) from None
    except Exception:
        return {}
    if not isinstance(extra, dict):
        return {}
    return {key: str(extra.get(key) or "").strip() for key in provider.session_config_keys}


async def _api_key_session(
    db: AsyncSession,
    integration: Integration,
    holder: Any,
    provider: AccountProvider,
    account_external_id: str | None,
) -> ProviderSession:
    """A session acting with the token the integration stores (no refresh)."""
    from app.services.integration_user_credentials import holder_connected
    from app.services.relays import integration_egress_route
    from app.services.vault import reveal_field

    if not holder_connected(holder):
        raise AccountUnavailableError("The integration is not connected: connect it again.")
    token = await reveal_field(holder, "api_key_enc") if getattr(holder, "api_key_enc", None) else None
    if not token:
        raise AccountUnavailableError("The integration holds no access token: edit it and set one.")
    config = await _session_config(holder, provider)
    # A provider with several backends needs some keys on one of them alone.
    missing = [key for key in provider.required_config_keys(config) if not config.get(key)]
    if missing:
        # The backend checked is named, so the owner knows which fields apply.
        label = provider.config_label(config)
        where = f" (checked for {label})" if label else ""
        raise AccountUnavailableError(f"The integration has no {', '.join(missing)}{where}: edit it and set it.")
    # The integration's own store (received messages) is read by its id.
    config["integration_id"] = str(integration.id)
    return ProviderSession(
        token=token,
        route=await integration_egress_route(db, integration),
        account_external_id=account_external_id,
        config=config,
        integration_id=str(integration.id),
    )


async def open_session(db: AsyncSession, account: AgentAccount, provider: AccountProvider) -> ProviderSession:
    """A provider session acting as *account*, through its integration."""
    return await open_integration_session(
        db,
        integration_id=account.integration_id,
        owner_id=account.user_id,
        provider=provider,
        account_external_id=account.account_external_id,
    )


def _agent_ready(agent: Agent | None) -> bool:
    return agent is not None and bool(agent.enabled)


# ---------------------------------------------------------------------------
# Processing one message
# ---------------------------------------------------------------------------


def dispatch(account_id: str, event: AccountEvent) -> None:
    """Process *event* in a bounded background task (the caller has answered its sender)."""
    from app.channels.base import spawn_channel_task

    spawn_channel_task(process_event(account_id, event), name="agent-account-event")


def _schedule_after_grace(delay: float, member: str, account_id: str, event: AccountEvent) -> None:
    """Run a parked message at the end of its grace period, holding no slot meanwhile."""
    from app.channels.base import spawn_channel_task

    async def _run() -> None:
        if await state.claim(member):
            await process_event(account_id, event, after_grace=True)

    loop = asyncio.get_running_loop()
    loop.call_later(delay, lambda: spawn_channel_task(_run(), name="agent-account-grace"))


#: How long the echo of a post waits for the post's id while that post is in flight.
ECHO_WAIT_SECONDS = 3.0


async def _note_account_message(account: AgentAccount, event: AccountEvent) -> None:
    """The account wrote: the agent's own post, or its human taking over the conversation.

    The platform can notify the post before the send call returns its id: a
    message arriving while a post is in flight in that conversation is checked
    again once the post had time to be recorded.
    """
    account_id = str(account.id)
    if event.from_agent_account or await state.was_sent_by_agent(account_id, event.conversation_id, event.message_id):
        return
    if await state.is_sending(account_id, event.conversation_id):
        await asyncio.sleep(ECHO_WAIT_SECONDS)
        if await state.was_sent_by_agent(account_id, event.conversation_id, event.message_id):
            return
    if account.owner_takeover_minutes > 0:
        await state.mark_owner_active(account_id, event.conversation_id, account.owner_takeover_minutes)
    await publish_owner_sent(account, event)


async def publish_owner_sent(account: AgentAccount, event: AccountEvent) -> None:
    """Raise ``agent_account.owner_sent`` for an email the mailbox's owner sent themselves.

    A mailbox only (a chat's own posts are no flag to clear). The event id is
    the account and the message, so a message both pushed and polled starts
    one run per subscriber.
    """
    provider = get_provider(account.provider)
    if provider is None or provider.kind != "mailbox":
        return
    from app.services import platform_events

    await platform_events.publish(
        "agent_account.owner_sent",
        platform_events.agent_account_message_data(account, event),
        event_id=f"agent_account.owner_sent:{account.id}:{event.message_id}",
    )


@dataclass(frozen=True)
class SenderDirectory:
    """Who a sender is in XTM One: the active user one of their addresses names, and its groups."""

    emails: tuple[str, ...]
    known: bool
    group_ids: frozenset[str]
    #: The XTM One user the sender is, when ``known``.
    user_id: str | None = None

    def explain(self, named_groups: dict[str, str]) -> dict[str, Any]:
        """What the decision log and the tester show: only the groups the rules name.

        A user's other memberships are not the account owner's to read (group
        members are an administrator's list).
        """
        return {
            "known": self.known,
            "emails": list(self.emails),
            "groups": [{"id": gid, "name": name} for gid, name in named_groups.items() if gid in self.group_ids],
        }


async def sender_directory(db: AsyncSession, addresses: tuple[str, ...]) -> SenderDirectory:
    """The XTM One user whose email is one of *addresses* (the first that names one), and its groups.

    The provider reports the sender's addresses from its directory (the
    person typed none of it), so they name the XTM One account the same
    person signs in with (invariant 8) - by its mail address or its sign-in
    name, which a directory may hold apart. Read on every message: a member
    removed from a group stops reaching what that group could at their next one.
    """
    from app.api.v1.helpers import get_user_group_ids
    from app.models.user import User

    emails = tuple(a for a in addresses if "@" in a)
    if not emails:
        return SenderDirectory(emails, False, frozenset())
    rows = (
        await db.execute(
            select(User.id, func.lower(User.email)).where(
                func.lower(User.email).in_([e.lower() for e in emails]), User.is_active.is_(True)
            )
        )
    ).all()
    by_email = {email: user_id for user_id, email in rows}
    user_id = next((by_email[e.lower()] for e in emails if e.lower() in by_email), None)
    if user_id is None:
        return SenderDirectory(emails, False, frozenset())
    groups = frozenset(str(group_id).lower() for group_id in await get_user_group_ids(db, user_id))
    return SenderDirectory(emails, True, groups, str(user_id))


async def sender_is_owner(
    db: AsyncSession, account: AgentAccount, event: AccountEvent, *, verified_sender: bool
) -> bool:
    """Whether the person writing is the account's owner, as the provider's directory says.

    Only on a provider whose sender is the signed-in author the platform
    authenticated (*verified_sender*, ``ProviderCapabilities.verified_sender``):
    an email's ``From`` is written by whoever sends it, so a mailbox never
    vouches for its owner. The addresses then come from the provider's
    directory, never from what the sender typed (invariant 8), so they name
    the XTM One user the owner signs in with. A message the account itself or
    an application wrote is never the owner writing. The owner's turn is
    their own configuration: it gets what a chat with the agent gets.
    """
    if not verified_sender or not account.user_id:
        return False
    if event.from_account or event.from_application or event.from_agent_account:
        return False
    directory = await sender_directory(db, event.sender_addresses)
    return directory.known and directory.user_id == str(account.user_id)


async def decide(account: AgentAccount, event: AccountEvent, db: AsyncSession | None = None) -> Decision:
    """The engine's decision on *event* with the conversation state as it is now."""
    thread_active, owner_active, replies = await state.conversation_state(str(account.id), event)
    agent_post = event.from_agent_account or await state.is_agent_post(
        account.provider, event.conversation_id, event.message_id
    )
    settings_ = AccountSettings.of(account)
    turns = await state.account_turns(str(account.id)) if settings_.max_turns_per_hour > 0 else 0
    named = rule_group_names(settings_)
    directory = await sender_directory(db, event.sender_addresses) if db is not None and named else None
    decision = evaluate(
        settings_,
        event,
        EventState(
            thread_active=thread_active,
            owner_active=owner_active,
            replies_last_hour=replies,
            turns_last_hour=turns,
            agent_post=agent_post,
            sender_group_ids=directory.group_ids if directory else frozenset(),
        ),
    )
    return replace(decision, sender=directory.explain(named)) if directory else decision


#: A rule's access list -> the ``run_agent_chat`` argument it narrows, and the agent attribute it narrows.
_ACCESS_SESSION_ARGS = (
    ("knowledge_base_ids", "session_knowledge_base_ids", "knowledge_base_ids"),
    ("mcp_server_ids", "session_mcp_server_ids", "mcp_server_ids"),
    ("builtin_tool_ids", "session_builtin_tool_ids", "builtin_tool_ids"),
    ("custom_tool_ids", "session_custom_tool_ids", "custom_tool_ids"),
    ("skill_ids", "session_skill_ids", "skill_ids"),
)


def agent_integration_providers(agent: Agent) -> set[str]:
    """The integration providers an agent uses: bound, or declared as per-user templates."""
    bound = {str(key).split("#", 1)[0] for key in (agent.integration_bindings or {})}
    return bound | {str(provider) for provider in agent.integration_template_bindings or []}


def message_reference(provider: Any, event: AccountEvent) -> dict[str, str]:
    """What the provider's tools take besides the message id to act on *event*, or ``{}``."""
    reference = getattr(provider, "message_reference", None)
    return reference(event) if callable(reference) else {}


def access_arguments(agent: Agent, access: dict[str, Any] | None) -> dict[str, Any]:
    """The ``run_agent_chat`` arguments that hold a turn to a rule's *access*.

    Every list is intersected with the agent's own, so a rule only ever
    narrows what the agent has: an id the agent lost since the rule was saved
    is not granted back. ``None`` leaves the agent's set as it is; memory
    ``off`` keeps every memory tier out of the turn, read and write.
    """
    if not access:
        return {}
    arguments: dict[str, Any] = {}
    for key, argument, attribute in _ACCESS_SESSION_ARGS:
        allowed = access.get(key)
        if allowed is None:
            continue
        wanted = {str(value) for value in allowed}
        arguments[argument] = [str(value) for value in getattr(agent, attribute, None) or [] if str(value) in wanted]
    providers = access.get("integrations")
    if providers is not None:
        arguments["allowed_integration_providers"] = agent_integration_providers(agent) & set(providers)
    if access.get("memory") == "off":
        arguments["memory_overrides"] = {"short_term": False, "medium_term": False, "persistent": False, "user": False}
    return arguments


async def process_event(account_id: str, event: AccountEvent, *, after_grace: bool = False) -> None:
    """Decide on *event* and act on the decision. Never raises."""
    try:
        await _process_event(account_id, event, after_grace=after_grace)
    except Exception:
        logger.exception("Agent account %s failed on a message", account_id)


async def _process_event(account_id: str, event: AccountEvent, *, after_grace: bool) -> None:
    if (
        not after_grace
        and not event.from_account
        and not await state.first_delivery(account_id, event.conversation_id, event.message_id)
    ):
        return
    async with async_session() as db:
        account = await db.get(AgentAccount, uuid.UUID(account_id))
        if account is None:
            return
        if event.from_account:
            await _note_account_message(account, event)
            return
        if event.self_chat and await state.is_sending(account_id, event.conversation_id):
            # The owner's self-chat echoes the agent's own reply, maybe before
            # the send returned its id: let the post be recorded, then
            # ``decide`` tells it apart (agent_author), never a new turn.
            await asyncio.sleep(ECHO_WAIT_SECONDS)
        decision = await decide(account, event, db)
        if not decision.respond:
            await activity.record_decision(account_id, event, decision, decision.reason)
            return
        from app.services.agentic_gate import agentic_refusal

        if await agentic_refusal(db):
            await activity.record_decision(account_id, event, decision, "ai_unavailable")
            return
        account.last_event_at = datetime.now(UTC)
        await db.commit()
        # One answer per email on the agent, whichever of its pipelines hears it
        # first: an assignment of the same agent, or another account of
        # it on another mailbox, that already took the mail leaves it alone.
        claim = _InboundClaim.of(account, event)
        holder = await claim.take()
        if holder is not None:
            taken_by = inbound_claims.describe_holder(holder)
            logger.info("Agent account %s leaves message %s: %s already took it", account_id, event.message_id, taken_by)
            # The holder as data, worded by the activity page in the viewer's
            # language; the note stays the agent's own line.
            await activity.record_decision(account_id, event, decision, ALREADY_HANDLED, holder=holder)
            return
        if decision.grace_period_seconds > 0 and not after_grace:
            member = await state.park(account_id, event, time.time() + decision.grace_period_seconds)
            if member is not None:
                await activity.record_decision(account_id, event, decision, "waiting")
                _schedule_after_grace(decision.grace_period_seconds, member, account_id, event)
                return
        turn_holder = turns.new_holder() if takes_turns(account) else None
        taken = await turns.take(account_id, event.conversation_id, turn_holder) if turn_holder else None
        if taken is None and not await _answer(db, account, event, decision, after_grace=after_grace):
            # Nothing posted, filed or drafted: another pipeline may still answer.
            await claim.give_back()
    # The conversation's turns run outside this session: the coalescing window
    # and the turns behind it never hold a database connection idle.
    if turn_holder is None or taken is None:
        return
    if taken:
        await hold_turns(
            account_id,
            event.conversation_id,
            turn_holder,
            first=event,
            decision=decision,
            claim=claim,
            after_grace=after_grace,
        )
        return
    if not await turns.enqueue(account_id, event.conversation_id, event):
        # Not queued (Redis failed meanwhile): answered on its own, as before.
        async with async_session() as db:
            account = await db.get(AgentAccount, uuid.UUID(account_id))
            if account is not None and not await _answer(db, account, event, decision, after_grace=after_grace):
                await claim.give_back()
        return
    # A turn is in flight in this conversation: this message joins its next
    # one instead of starting a second answer.
    await activity.record_decision(account_id, event, decision, QUEUED)
    if await turns.take(account_id, event.conversation_id, turn_holder):
        # That turn ended between the two calls: nobody else drains the queue.
        await hold_turns(account_id, event.conversation_id, turn_holder)


#: The outcome of a message queued behind its conversation's turn in flight.
QUEUED = "queued"
#: The outcome of a message answered within a later message's turn.
COALESCED = "coalesced"


def takes_turns(account: AgentAccount) -> bool:
    """Whether the account answers one turn at a time per conversation.

    Every chat provider does (WhatsApp, Teams, Slack): a burst of messages in
    one chat is one answer. A provider that triages (a mailbox) answers each
    email on its own, as an email is not a line of a chat.
    """
    provider = get_provider(account.provider)
    return provider is not None and not provider.capabilities.triage


async def hold_turns(
    account_id: str,
    conversation_id: str,
    holder: str,
    *,
    first: AccountEvent | None = None,
    decision: Decision | None = None,
    claim: _InboundClaim | None = None,
    after_grace: bool = False,
) -> None:
    """Answer the conversation one turn at a time while *holder* holds it.

    *first* is the message that took the conversation, answered after the
    coalescing window (``settings.chat_event_coalesce_window_seconds``); then
    every message queued meanwhile becomes ONE next turn, answered on the
    latest of them (the turn replays the platform conversation, so it reads
    every one of them and the reply before), the others recorded
    ``coalesced``. The conversation is released once its queue is empty, and
    taken again when a message was queued during the release. Never raises.
    """
    try:
        while True:
            try:
                await _turn_loop(account_id, conversation_id, first, decision, claim, after_grace)
            finally:
                await turns.release(account_id, conversation_id, holder)
            first, decision, claim = None, None, None
            if not await turns.has_queued(account_id, conversation_id):
                return
            if not await turns.take(account_id, conversation_id, holder):
                return
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("Agent account %s failed on a conversation turn", account_id)


async def _turn_loop(
    account_id: str,
    conversation_id: str,
    first: AccountEvent | None,
    decision: Decision | None,
    claim: _InboundClaim | None,
    after_grace: bool,
) -> None:
    from app.services.agentic_gate import agentic_refusal

    pending: list[AccountEvent] = [first] if first is not None else []
    window = max(0, int(settings.chat_event_coalesce_window_seconds or 0))
    if first is not None and window:
        await asyncio.sleep(window)
    while True:
        pending += await turns.drain(account_id, conversation_id)
        if not pending:
            return
        # The turn answers the message WRITTEN last, not the one that reached
        # the queue last: a grace timer, a poll batch or a second
        # sender can deliver them out of order, and the provider's context of
        # an older message leaves the newer ones out of the prompt.
        pending.sort(key=lambda m: m.created_at)
        latest, absorbed = pending[-1], pending[:-1]
        pending = []
        async with async_session() as db:
            account = await db.get(AgentAccount, uuid.UUID(account_id))
            if account is None:
                return
            turn_decision = decision if latest is first and decision is not None else await decide(account, latest, db)
            for message in absorbed:
                await activity.record_decision(account_id, message, turn_decision, COALESCED)
            if not turn_decision.respond:
                await activity.record_decision(account_id, latest, turn_decision, turn_decision.reason)
                continue
            if latest is not first and await agentic_refusal(db):
                await activity.record_decision(account_id, latest, turn_decision, "ai_unavailable")
                continue
            traced: dict[str, Any] = {}
            acted = await _answer(
                db,
                account,
                latest,
                turn_decision,
                after_grace=after_grace and latest is first,
                absorbed=absorbed,
                trace_out=traced,
            )
            if traced.get("trace_id"):
                # Every message of the turn names the trace that answered it.
                await activity.record_turn_trace(
                    account_id,
                    conversation_id,
                    [m.message_id for m in (*absorbed, latest)],
                    traced["trace_id"],
                )
            if latest is first and claim is not None and not acted:
                # Nothing posted, filed or drafted: another pipeline may still answer.
                await claim.give_back()


#: The outcome of a message another pipeline of the agent already took.
ALREADY_HANDLED = "already_handled"


@dataclass(frozen=True)
class _InboundClaim:
    """The agent-wide claim of one email (``services/inbound_claims.py``).

    Keyed by the mail's RFC 5322 Message-ID as the engine keys an assignment
    run of the same mail (``event_run_dedup.mail_dedup_key``), so an
    ``integration_event`` assignment and an agent account of one agent never
    both answer it. A message without a Message-ID (a chat message) has no
    claim: ``take`` lets it through.
    """

    agent_id: Any
    key: str | None
    holder: str

    @classmethod
    def of(cls, account: AgentAccount, event: AccountEvent) -> _InboundClaim:
        key = (
            event_run_dedup.mail_dedup_key({"internet_message_id": event.internet_message_id})
            if event.internet_message_id
            else None
        )
        return cls(account.agent_id, key, inbound_claims.account_holder(account.id))

    async def take(self) -> str | None:
        """``None`` when this account may answer; else who took the mail first."""
        return await inbound_claims.claim(
            self.agent_id, self.key, self.holder, window_seconds=settings.assignment_event_dedup_window_seconds
        )

    async def give_back(self) -> None:
        await inbound_claims.release(self.agent_id, self.key, self.holder)


@dataclass
class _AnswerProgress:
    """How far an answer went: once it acted on the platform, a cut does not run it again."""

    acted: bool = False
    #: The checkpoint writer of the turn's tool rounds, once the agent runs (``cut_turn_checkpoint``).
    checkpoint: Any = None


async def _answer(
    db: AsyncSession,
    account: AgentAccount,
    event: AccountEvent,
    decision: Decision,
    *,
    after_grace: bool,
    absorbed: list[AccountEvent] | None = None,
    trace_out: dict[str, Any] | None = None,
) -> bool:
    """Answer *event*; whether it acted on the platform (a send, a mailbox action, a draft, a record).

    A platform stop cutting it before it acted hands the message on.

    The whole turn up to its first action on the platform (opening the
    session, the owner-reply check, the context, the files, the agent, the
    triage) is covered: cut there, nothing was posted, filed or drafted, so
    the message is parked for the next process, at most ``MAX_AUTO_RESUMES``
    times. Cut once it acted (a send, a mailbox action, a draft), it is not
    run again: a duplicate on the platform is worse than the reply's record.
    """
    account_id = str(account.id)
    progress = _AnswerProgress()
    # The turn's trace id, read on a cut to link the cut trace to its resume.
    trace_out = trace_out if trace_out is not None else {}
    provider = get_provider(getattr(account, "provider", "") or "")
    label = provider.label if provider is not None else str(getattr(account, "provider", "") or "")
    agent_id = getattr(account, "agent_id", None)
    # One turn at a time per sender of the agent, whatever account brought the
    # message (``sender_turns.py``): a turn of the same person in flight on
    # another channel reads this message mid-run, and this turn runs after it.
    for message in (*(absorbed or []), event):
        await sender_turns.note(agent_id, account_id, message, label=label)
    try:
        async with sender_turns.hold(agent_id, account_id, event, label=label, before_wait=getattr(db, "commit", None)):
            await _answer_turn(
                db,
                account,
                event,
                decision,
                after_grace=after_grace,
                progress=progress,
                absorbed=absorbed or [],
                trace_out=trace_out,
            )
    except asyncio.CancelledError as exc:
        parked = False
        if not progress.acted:
            parked = await _park_cut_turn(account_id, event, exc)
        # Nothing is posted in the conversation: the next process answers the
        # parked message as if the cut never happened (issue #10772).
        await _close_checkpoint(progress, account_id, event, keep=parked)
        await _note_cut_trace(account_id, event, exc, trace_out.get("trace_id"), parked=parked, acted=progress.acted)
        raise
    except BaseException:
        await _close_checkpoint(progress, account_id, event, keep=False)
        raise
    await _close_checkpoint(progress, account_id, event, keep=False)
    return progress.acted


async def _answer_turn(
    db: AsyncSession,
    account: AgentAccount,
    event: AccountEvent,
    decision: Decision,
    *,
    after_grace: bool,
    progress: _AnswerProgress,
    absorbed: list[AccountEvent] | None = None,
    trace_out: dict[str, Any] | None = None,
) -> None:
    from app.channels.router import NO_RESPONSE_MARKER
    from app.services.agent_chat import run_agent_chat
    from app.services.langfuse_tracing import next_root_trace

    account_id = str(account.id)
    provider = get_provider(account.provider)
    agent = await db.get(Agent, account.agent_id) if account.agent_id else None
    if provider is None or not _agent_ready(agent):
        await activity.record_decision(account_id, event, decision, "agent_unavailable")
        return
    try:
        session = await open_session(db, account, provider)
    except AccountUnavailableError as exc:
        await _record_failure(db, account, event, decision, str(exc))
        return

    if after_grace and await _owner_replied_since(provider, session, account, event, decision):
        await activity.record_decision(account_id, event, decision, "owner_replied")
        return

    # The engine read the hour's turns when the message arrived; this takes
    # one atomically, so messages arriving together never all pass.
    if not await state.take_turn(account_id, account.max_turns_per_hour or 0):
        await activity.record_decision(account_id, event, decision, "account_rate_limited")
        return

    try:
        context = await provider.fetch_context(session, event, account.context_messages)
    except ProviderError as exc:
        logger.info("Agent account %s answers without context: %s", account_id, exc)
        context = []
    absorbed = absorbed or []
    # A voice note is heard before the turn is built: its transcript is the
    # message's text, so queueing and the cross-channel merge see it as text.
    heard: list[str] = []
    files = await _message_files(db, provider, session, account, event, voice_out=heard)
    if heard:
        event = replace(event, text=voice_notes.with_voice_notes(event.text, heard))
    history, user_message = build_turn(context, event, account.account_display_name, absorbed)
    # What the same person wrote to the agent on its other channels meanwhile,
    # and what the agent answered there: one request across channels is one loop.
    elsewhere = await sender_turns.recent_elsewhere(getattr(account, "agent_id", None), account_id, event)
    user_message = sender_turns.with_recent(user_message, elsewhere, getattr(event, "sender_name", None))
    merged = len(absorbed) + 1 if absorbed else 0
    brief = account_brief(
        mode=account.mode,
        platform_label=provider.label,
        account_name=account.account_display_name,
        conversation_name=event.conversation_name if event.conversation_type != "direct" else None,
        instructions=decision.instructions,
        rule_name=decision.rule_name,
        label=label_text(account.mode, account.ai_label_text, account.account_display_name) if decision.ai_label else None,
        merged=merged,
        mention_hint=getattr(provider, "reply_mention_hint", ""),
    )
    triaging = provider.capabilities.triage
    errors: dict[str, Any] = {}
    approvals: list = []
    # The tool work of an earlier attempt a platform stop cut goes right before
    # the message, and this attempt checkpoints its own, so a cut resumes from
    # its last completed tool call instead of starting over.
    replay = await cut_turn_checkpoint.load(account_id, event)
    if replay.history:
        history = [*history, *replay.history]
    progress.checkpoint = cut_turn_checkpoint.writer(account_id, event, replay)
    cut_trace_id = await cut_turn_checkpoint.cut_trace(account_id, event) if replay.history else None
    resumed_from = cut_turn_checkpoint.trace_meta(account_id, event, replay, cut_trace_id=cut_trace_id)
    try:
        owner_writing = await sender_is_owner(db, account, event, verified_sender=provider.capabilities.verified_sender)
        with next_root_trace(resumed_from) if resumed_from else contextlib.nullcontext():
            text, _transfer = await run_agent_chat(
                db,
                str(account.user_id),
                agent,
                history,
                user_message,
                context=message_context(
                    event,
                    decision,
                    platform=provider.platform,
                    brief=brief,
                    triage=triaging,
                    merged_message_ids=[m.message_id for m in absorbed] if absorbed else None,
                    message_reference=message_reference(provider, event),
                ),
                files=files or None,
                error_out=errors,
                approvals_out=approvals,
                allow_handover=False,
                pinned_integrations={provider.integration_provider: str(account.integration_id)},
                identity_from_platform=True,
                owner_writing=owner_writing,
                **({"session_output_schema": triage.output_schema(must_reply=decision.must_reply)} if triaging else {}),
                **access_arguments(agent, decision.access),
                trace_out=trace_out,
                steering_queue=sender_turns.current_queue(),
                on_checkpoint=progress.checkpoint.offer,
            )
    except Exception as exc:
        logger.exception("Agent account %s: the agent turn failed", account_id)
        await _link_resumed_trace(db, cut_trace_id, trace_out)
        await _record_failure(db, account, event, decision, f"The agent failed: {type(exc).__name__}")
        return
    await _link_resumed_trace(db, cut_trace_id, trace_out)
    if errors.get("error_code"):
        await _record_failure(db, account, event, decision, f"The agent did not answer ({errors['error_code']}).")
        return
    if approvals:
        # What the turn returned explains the pause: never posted as the account.
        await _record_failure(
            db, account, event, decision, "The agent stopped at a tool that needs approval; nothing was posted."
        )
        return
    text = (text or "").strip()
    note: str | None = None
    if triaging:
        triaged = (
            triage.unanswered(must_reply=decision.must_reply)
            if text == NO_RESPONSE_MARKER
            else triage.read(text, must_reply=decision.must_reply)
        )
        if triaged is None:
            await _record_failure(
                db, account, event, decision, "The agent did not say whether to reply, hand it over or leave it."
            )
            return
        if triaged.decision != "reply":
            # A mailbox action from here on: not run again if cut.
            progress.acted = True
            await _triaged(db, session, provider, account, agent, event, decision, triaged)
            return
        text, note = triaged.reply, triaged.note
    if not text or text == NO_RESPONSE_MARKER:
        await activity.record_decision(account_id, event, decision, "silent")
        return
    too_many = too_many_recipients(account, event)
    if too_many:
        if triaging:
            handed = triage.Triage("needs_owner", "", too_many)
            # A mailbox action from here on: not run again if cut.
            progress.acted = True
            await _triaged(db, session, provider, account, agent, event, decision, handed)
        else:
            await _record_failure(db, account, event, decision, too_many)
        return
    # The agent account that posted the message may record it only once its
    # send returns, after the notification reached this one: asked again now.
    if await state.is_agent_post(account.provider, event.conversation_id, event.message_id):
        await activity.record_decision(account_id, event, decision, "agent_author")
        return

    # A send, a draft or a shadow record from here on: not run again if cut.
    progress.acted = True
    reply = _new_reply(account, event, decision, draft=text)
    if decision.response_mode == "shadow":
        reply.status = "shadow"
    elif decision.response_mode == "approval":
        reply.status = "pending"
    else:
        try:
            await _post(db, session, provider, account, reply, text)
        except DuplicateReplyError as exc:
            # The mailbox already answered the conversation (a mail tool's send,
            # or another turn) and nothing new arrived since: nothing was sent.
            reply.status = "silent"
            reply.error = str(exc)[:2000]
        except ProviderError as exc:
            reply.status = "failed"
            reply.error = str(exc)[:2000]
            account.last_error = reply.error
    db.add(reply)
    await db.flush()
    if reply.status == "pending":
        await _notify_pending(db, account, agent, reply)
    elif reply.status == "sent" and account.notify_on_send:
        await _notify_sent(db, account, agent, reply)
    await db.commit()
    await activity.record_decision(account_id, event, decision, reply.status, reply_id=reply.id, note=note)
    await sender_turns.note(
        getattr(account, "agent_id", None),
        account_id,
        event,
        label=provider.label,
        direction="out",
        text=text,
        status=reply.status,
    )


async def _owner_replied_since(
    provider: AccountProvider, session: ProviderSession, account: AgentAccount, event: AccountEvent, decision: Decision
) -> bool:
    """Whether the owner answered *event* themselves while it waited; a parked message is then left.

    Read on a message run again after its grace period or after a platform
    stop cut its turn (``after_grace``): a post of the account in the thread
    after the message that the agent did not send is its owner typing.

    Never on the agent's own account (``ai_user``) without a grace period: no
    person writes from that account, so a post the agent made by another path
    (a tool, a notification) is not an answer, and a turn a rollout cut must
    run and post its real reply (issue #10779).
    """
    if getattr(account, "mode", None) == "ai_user" and not (getattr(decision, "grace_period_seconds", 0) or 0):
        return False
    try:
        written = await provider.account_messages_since(session, event)
    except ProviderError:
        return False
    account_id = str(account.id)
    for message_id in written:
        if not await state.was_sent_by_agent(account_id, event.conversation_id, message_id):
            return True
    return False


#: The ``channel`` an agent account's cut turns count their automatic resumes under.
ACCOUNT_RESUME_CHANNEL = "agent_account"


#: ``resume_skipped`` on the trace of a cut turn that had already acted on the platform.
RESUME_SKIPPED_ACTED = "acted_on_the_platform"
#: ``resume_skipped`` on the trace of a cut turn that was not handed on (not a platform stop, or the cap reached).
RESUME_SKIPPED_NOT_HANDED_ON = "not_handed_on"


async def _note_cut_trace(
    account_id: str, event: AccountEvent, exc: BaseException, trace_id: object, *, parked: bool, acted: bool
) -> None:
    """Say on the cut turn's trace what follows it. Never raises.

    Handed on: its id is kept for the resume, which names it
    (``resumed_from.trace_id``) and marks it ``resumed_as``. Not handed on
    after a graceful stop: the trace says why (``resume_skipped``), so a cut
    trace with neither key is a resume that was lost. Another cancellation
    (a person stopping the turn) marks nothing.
    """
    from app.core.utils import is_graceful_shutdown_cancellation

    if not trace_id:
        return
    if parked:
        await cut_turn_checkpoint.remember_cut_trace(account_id, event, trace_id)
        return
    if not is_graceful_shutdown_cancellation(exc):
        return
    reason = RESUME_SKIPPED_ACTED if acted else RESUME_SKIPPED_NOT_HANDED_ON

    async def _mark() -> None:
        from app.services.cut_turn_resume import mark_trace_resume_skipped

        async with async_session() as session:
            await mark_trace_resume_skipped(session, trace_id, reason)

    try:
        from app.core.shutdown_finalizers import start_shutdown_finalizer

        start_shutdown_finalizer(_mark(), name=f"cut-trace-skip:{trace_id}")
    except Exception as mark_exc:
        logger.warning("Agent account %s: could not mark the cut trace %s: %s", account_id, trace_id, mark_exc)


async def _link_resumed_trace(db: AsyncSession, cut_trace_id: str | None, trace_out: dict[str, Any] | None) -> None:
    """Mark the cut turn's trace with the trace that resumed it (``resumed_as``). Never raises."""
    resumed = (trace_out or {}).get("trace_id")
    if not cut_trace_id or not resumed:
        return
    from app.services.cut_turn_resume import mark_trace_resumed_as

    await mark_trace_resumed_as(db, cut_trace_id, resumed)


async def _close_checkpoint(progress: _AnswerProgress, account_id: str, event: AccountEvent, *, keep: bool) -> None:
    """Close the turn's checkpoint writer: flushed and left to the resume when *keep*, else deleted. Never raises."""
    checkpoint, progress.checkpoint = progress.checkpoint, None
    if checkpoint is None:
        return
    try:
        await checkpoint.aclose(flush=keep)
        if not keep:
            await cut_turn_checkpoint.discard(account_id, event)
    except Exception as exc:
        logger.warning("Agent account %s: could not close the turn checkpoint: %s", account_id, exc)


async def _park_cut_turn(account_id: str, event: AccountEvent, exc: BaseException) -> bool:
    """Hand the message of a turn a platform stop cut to the next process; whether it was.

    Parked due now, so the scheduler's ``state.overdue`` sweep runs it
    again once this process is gone (``PENDING_STALE_SECONDS`` later), as
    after a grace period: a message the owner answered meanwhile is left. A
    cancellation that is not the platform's, or the cap of automatic resumes
    reached, parks nothing. Logs one WARNING either way; never raises.
    """
    from app.core.utils import is_graceful_shutdown_cancellation
    from app.core.worker_shutdown import drain_cut_cause
    from app.services.chat_turn_guard import claim_channel_resume
    from app.services.cut_turn_resume import MAX_AUTO_RESUMES, resumable_cause

    # Only a stopping server's drain raises this cancellation: a platform stop
    # even when the stop hook recorded no cause, as the web turn reads it
    # (``drain_cut_cause``). ``cut_cause()`` alone left such a turn unparked,
    # dropped with no answer (issue #10779).
    cause = drain_cut_cause()
    if not is_graceful_shutdown_cancellation(exc) or not resumable_cause(cause):
        return False
    try:
        if not await claim_channel_resume(
            f"{ACCOUNT_RESUME_CHANNEL}:{account_id}", event.conversation_id, event.message_id
        ):
            logger.warning(
                "Agent account %s: turn on message %s cut by a platform %s again after %d automatic "
                "resume(s): not handed on",
                account_id,
                event.message_id,
                cause,
                MAX_AUTO_RESUMES,
            )
            return False
        member = await state.park(account_id, event, time.time())
    except Exception as park_exc:
        logger.warning(
            "Agent account %s: could not hand on the turn a platform stop cut: %s", account_id, park_exc
        )
        return False
    if member is None:
        return False
    logger.warning(
        "Agent account %s: turn on message %s cut by a platform %s, handed to the next process",
        account_id,
        event.message_id,
        cause,
    )
    return True


#: The ``FileAttachment.source`` of a file a message to an agent account carried.
MESSAGE_FILE_SOURCE_PREFIX = "account:"


async def _voice_note_line(db: AsyncSession, account: AgentAccount, audio: bytes, filename: str) -> str:
    """The turn line of one voice note: its transcript, or why it could not be heard.

    Transcribed for the account's owner with the platform's one
    transcription, the language detected; the transcript is never logged.
    """
    return await voice_notes.heard_line(db, account.user_id, audio, filename, where=f"Agent account {account.id}")


async def _message_files(
    db: AsyncSession,
    provider: AccountProvider,
    session: ProviderSession,
    account: AgentAccount,
    event: AccountEvent,
    *,
    voice_out: list[str] | None = None,
) -> list:
    """The files and pasted images *event* carries, stored as files of the turn.

    Each is downloaded as the account (``provider.download_attachment``),
    under the upload cap, stored like an upload of the account's owner and
    handed to ``run_agent_chat(files=...)``: ``read_file_content`` reads it
    and an image reaches a model that sees images. One that cannot be read
    is skipped and logged; the agent answers with the others, or the text.

    An audio file (a voice message) is not stored: it is transcribed
    for the account's owner (``app.llm.transcription``) and its turn line
    (``voice_notes``) appended to *voice_out*, a failure to download or to
    transcribe it included, so the turn never sees a voice note as nothing.
    """
    if not event.attachments:
        return []
    import hashlib

    from app.integrations.file_delivery import file_content_type, with_extension
    from app.llm.base import FileRef
    from app.models.file_attachment import FileAttachment
    from app.services.file_cleanup import purge_storage_objects
    from app.services.file_storage import MAX_UPLOAD_SIZE_BYTES, file_storage

    refs: list = []
    stored: list[str] = []
    # A message with no text of its own: a file it carries that cannot be read
    # is stated, so the turn never sees it as nothing (a voice clip whose
    # media type is not declared included).
    textless = str(getattr(event, "text", "") or "").strip() in voice_notes.EMPTY_TEXT_MARKERS
    for attachment in event.attachments:
        audio_declared = voice_notes.is_audio(attachment.content_type, attachment.name)
        try:
            data, declared = await provider.download_attachment(session, attachment, MAX_UPLOAD_SIZE_BYTES)
        except ProviderError as exc:
            logger.info("Agent account %s: a file of the message is skipped: %s", account.id, exc)
            if voice_out is not None and audio_declared:
                voice_out.append(voice_notes.failed_line(f"the audio could not be downloaded ({exc})"))
            elif voice_out is not None and textless:
                voice_out.append(voice_notes.unread_line(f"the download failed ({exc})"))
            continue
        if not data:
            if voice_out is not None and audio_declared:
                voice_out.append(voice_notes.failed_line("the audio downloaded empty"))
            elif voice_out is not None and textless:
                voice_out.append(voice_notes.unread_line("it downloaded empty"))
            continue
        content_type = attachment.content_type
        if content_type == "application/octet-stream":
            content_type = file_content_type(declared, attachment.name)
        filename = with_extension(attachment.name, content_type)
        if voice_out is not None and voice_notes.is_audio(content_type, filename):
            voice_out.append(await _voice_note_line(db, account, data, filename))
            continue
        try:
            key = await file_storage.upload(
                data=data, filename=filename, content_type=content_type, prefix=f"accounts/{account.id}"
            )
        except Exception:
            logger.warning("Agent account %s: a file of the message could not be stored", account.id, exc_info=True)
            continue
        stored.append(key)
        row = FileAttachment(
            id=uuid.uuid4(),
            user_id=account.user_id,
            filename=filename,
            content_type=content_type,
            size=len(data),
            s3_object_name=key,
            sha256=hashlib.sha256(data).hexdigest(),
            source=f"{MESSAGE_FILE_SOURCE_PREFIX}{provider.key}"[:50],
        )
        db.add(row)
        refs.append(FileRef.from_attachment(row))
    if not refs:
        return []
    try:
        # Committed before the turn: a tool reads the file by its id on its own session.
        await db.commit()
    except Exception:
        await db.rollback()
        await purge_storage_objects(stored)
        logger.warning("Agent account %s: the message's files could not be recorded", account.id, exc_info=True)
        return []
    return refs


async def _triaged(
    db: AsyncSession,
    session: ProviderSession,
    provider: AccountProvider,
    account: AgentAccount,
    agent: Agent | None,
    event: AccountEvent,
    decision: Decision,
    triaged: triage.Triage,
) -> None:
    """The agent handed the message to the owner, or found it needs no reply.

    The mailbox shows it as the owner chose (``mailbox_actions``) and a
    handed-over message notifies them. Shadow mode sends nothing and files
    nothing, but a handed-over message is still flagged when the owner asked
    for it: the flag is how the owner finds what the account handed over.
    A flag that fails records the decision as ``failed`` (the account's last
    error says why), never a silent "handed to you".
    """
    actions = triage.effective_mailbox_actions(account.mailbox_actions)
    handed_over = triaged.decision == "needs_owner"
    shadow = decision.response_mode == "shadow"
    mark = triage.mark_for(triaged.decision, actions, shadow=shadow)
    failure = await _mark(session, provider, account, event.route, mark)
    if not shadow and handed_over and actions["notify_needs_owner"]:
        await _notify_flagged(db, account, agent, event, triaged.note)
    if not mark.empty or not shadow:
        await db.commit()
    outcome = triage.OUTCOMES[triaged.decision]
    if failure and mark.flag:
        outcome = "failed"
    await activity.record_decision(str(account.id), event, decision, outcome, note=triaged.note)


async def _mark(
    session: ProviderSession, provider: AccountProvider, account: AgentAccount, route: dict[str, Any], mark: MessageMark
) -> str | None:
    """File *mark* on the message: the failure's text, or None.

    A failure is logged as an error and kept as the account's last error, never the reply's.
    """
    if mark.empty:
        return None
    try:
        await provider.mark_message(session, route, mark)
    except ProviderError as exc:
        logger.error("Agent account %s could not file a message: %s", account.id, exc)
        account.last_error = f"Could not file the message in the mailbox: {exc}"[:2000]
        return str(exc) or "error"
    return None


def _new_reply(account: AgentAccount, event: AccountEvent, decision: Decision, *, draft: str) -> AgentAccountReply:
    return AgentAccountReply(
        # Known before the flush: a posted answer is scheduled for its reaction checks by id.
        id=uuid.uuid4(),
        account_id=account.id,
        user_id=account.user_id,
        agent_id=account.agent_id,
        status="pending",
        conversation_type=event.conversation_type,
        conversation_id=event.conversation_id[:500],
        conversation_name=(event.conversation_name or None) and event.conversation_name[:500],
        thread_id=event.thread_id[:500],
        message_id=event.message_id[:255],
        sender_id=event.sender_id[:255] or None,
        sender_name=event.sender_name[:255] or None,
        sender_email=event.sender_email,
        inbound_text=event.text[:INBOUND_TEXT_LIMIT],
        draft_text=draft,
        rule_id=decision.rule_id,
        rule_name=decision.rule_name,
        ai_label=decision.ai_label,
        route=dict(event.route),
    )


def too_many_recipients(account: AgentAccount, event: AccountEvent) -> str | None:
    """Why no reply to *event* goes out on its own, when it would reach more people than the account allows.

    The sender of an email chooses who a reply-all reaches: an account never
    answers more of them on its own than ``max_reply_recipients`` (0 = no limit).
    """
    limit = account.max_reply_recipients or 0
    if limit <= 0 or event.reply_recipients <= limit:
        return None
    return (
        f"A reply would go to {event.reply_recipients} other recipients, more than the {limit} "
        "the account answers on its own: nothing was sent."
    )


def labelled(account: AgentAccount, text: str, ai_label: bool) -> str:
    """*text* as posted: with the account's AI label when the decision asks for it."""
    if not ai_label:
        return text
    return apply_ai_label(
        text,
        label_text(account.mode, account.ai_label_text, account.account_display_name),
        account.ai_label_position,
    )


async def _mailbox_signature(db: AsyncSession, account: AgentAccount) -> AccountSignature | None:
    """The signature the mail tools of the account's mailbox sign with, for an account on its own signature.

    One signature pipeline for every message a mailbox sends: when the
    integration the account sends through resolves a signature integration
    (its Email signature setting, ``email_signature_settings.plan_signatures``),
    the account's reply carries that signature, as a mail tool's send from
    the same mailbox does, and the provider embeds its images inline. ``None``
    when the integration resolves none or it cannot be read: the provider then
    copies the account's own signature from its sent mail, as before.
    """
    from app.services.email_signature_settings import plan_signatures

    if account.integration_id is None or not account.account_email:
        return None
    integration = await db.get(Integration, account.integration_id)
    if integration is None:
        return None
    key = str(integration.id)
    try:
        plan = (await plan_signatures(db, {key: integration}, acting_user_id=account.user_id)).get(key)
    except Exception:
        logger.info("Agent account %s: the mailbox's signature setting could not be read", account.id, exc_info=True)
        return None
    if plan is None or not plan.signs or not plan.integration_id:
        return None
    try:
        found = await email_signatures.read_signature(db, plan.integration_id, account.account_email)
    except email_signatures.SignatureUnavailableError as exc:
        logger.info("Agent account %s: the mailbox's signature integration could not be read: %s", account.id, exc)
        return None
    if found is None:
        return None
    return AccountSignature("given", found.for_reply(True))


async def _owner_email(db: AsyncSession, account: AgentAccount) -> str | None:
    """The account owner's address, lower-cased: who keeps their place in Cc on a reply-all."""
    from app.models.user import User

    owner = await db.get(User, account.user_id) if account.user_id else None
    address = str(getattr(owner, "email", "") or "").strip().lower()
    return address or None


async def _sign(db: AsyncSession, provider: AccountProvider, account: AgentAccount, session: ProviderSession) -> str | None:
    """Put the signature the owner chose on *session*; why it fell back on the account's own, if it did."""
    if not provider.capabilities.signature:
        return None
    source = account.signature_source or "own"
    if source == "none":
        session.signature = AccountSignature("none")
        return None
    if source != "integration":
        session.signature = await _mailbox_signature(db, account) or AccountSignature("own")
        return None
    session.signature = AccountSignature("own")
    try:
        found = await email_signatures.read_signature(db, account.signature_integration_id, account.account_email or "")
    except email_signatures.SignatureUnavailableError as exc:
        return f"The signature integration could not be read, so the reply carried the account's own signature: {exc}"
    if found is None:
        return (
            f"The signature integration holds no signature for {account.account_email}, "
            "so the reply carried the account's own signature."
        )
    session.signature = AccountSignature("given", found.for_reply(True))
    return None


#: The most bytes of files one reply carries; a file past it is named, not attached.
REPLY_FILES_MAX_BYTES = 100 * 1024 * 1024


async def _reply_files(db: AsyncSession, account: AgentAccount, file_ids: list[str]) -> list[ReplyFile]:
    """The files the ``[[FILE:id]]`` markers of a reply name, read from storage.

    Only a file of the account's owner is read: the marker is the model's
    text. An id that names none is dropped; a file that cannot be read, or
    past :data:`REPLY_FILES_MAX_BYTES`, is handed over empty so the provider
    names it in one plain line instead of attaching it.
    """
    if not file_ids:
        return []
    from app.models.file_attachment import FileAttachment
    from app.services.file_storage import file_storage

    ids = [uuid.UUID(file_id) for file_id in file_ids]
    rows = (
        await db.execute(
            select(FileAttachment).where(FileAttachment.id.in_(ids), FileAttachment.user_id == account.user_id)
        )
    ).scalars().all()
    by_id = {row.id: row for row in rows}
    files: list[ReplyFile] = []
    total = 0
    for file_id in ids:
        row = by_id.get(file_id)
        if row is None:
            continue
        data = b""
        if total + (row.size or 0) <= REPLY_FILES_MAX_BYTES:
            try:
                data = await file_storage.download(row.s3_object_name)
            except Exception:
                logger.warning("Agent account %s: a file of the reply could not be read", account.id, exc_info=True)
        total += len(data)
        files.append(ReplyFile(name=row.filename or "file", content_type=row.content_type or "", data=data))
    return files


async def _markers_as_links(db: AsyncSession, account: AgentAccount, text: str) -> str:
    """*text* with its deck, report, video and dashboard markers as links to their web-app page.

    Teams, Outlook, Gmail and WhatsApp draw none of the web chat's cards: a
    raw ``[[PRESENTATION:id#/slide]]`` reached the reader (#11475). The
    resolvers of the channel replies and the send tools run here, scoped to
    the account's owner: an id of another user is dropped, never linked,
    never left raw. The ``[[FILE:id]]`` markers are split before, so their
    files still go out as attachments.
    """
    from app.services.outbound_markers import has_artifact_marker, markers_as_outbound_links

    if not has_artifact_marker(text):
        return text
    return await markers_as_outbound_links(db, text, user_id=account.user_id)


async def _post(
    db: AsyncSession,
    session: ProviderSession,
    provider: AccountProvider,
    account: AgentAccount,
    reply: AgentAccountReply,
    text: str,
) -> None:
    text, file_ids = split_file_markers(text)
    files = await _reply_files(db, account, file_ids)
    text = await _markers_as_links(db, account, text)
    body = labelled(account, text, reply.ai_label)
    signature_note = await _sign(db, provider, account, session)
    session.owner_email = await _owner_email(db, account)
    session.answers_message_id = reply.message_id or None
    await state.mark_sending(str(account.id), reply.conversation_id, True)
    delivery = getattr(session, "delivery", None)
    if isinstance(delivery, dict):
        delivery.clear()
    try:
        if files:
            sent_id = await provider.send_reply_files(session, reply.route or {}, body, files)
        else:
            sent_id = await provider.send_reply(session, reply.route or {}, body)
        await state.mark_sent(str(account.id), reply.conversation_id, sent_id)
        await state.mark_agent_post(account.provider, reply.conversation_id, sent_id)
    finally:
        await state.mark_sending(str(account.id), reply.conversation_id, False)
    reply.status = "sent"
    reply.sent_text = body
    reply.sent_message_id = sent_id
    if delivery:
        # What the provider made of the text (the Teams mentions resolved and
        # those left as written), kept with the reply it was posted as.
        reply.origin = {**(reply.origin or {}), "delivery": dict(delivery)}
        logger.info("Agent account %s: reply delivered with %s", account.id, delivery)
    account.last_reply_at = datetime.now(UTC)
    account.last_error = signature_note
    await state.mark_replied(
        str(account.id), conversation_id=reply.conversation_id, thread_id=reply.thread_id, message_id=reply.message_id
    )
    if provider.capabilities.triage:
        actions = triage.effective_mailbox_actions(account.mailbox_actions)
        await _mark(session, provider, account, reply.route or {}, triage.mark_for("reply", actions))
    from app.services.agent_accounts.reactions import schedule_first_check

    await schedule_first_check(account, reply)
    await _follow_thread(provider, account, reply)


async def _follow_thread(provider: AccountProvider, account: AgentAccount, reply: AgentAccountReply) -> None:
    """Read the channel thread the account answered in for its follow-ups, when no subscription brings them.

    A channel heard for mentions alone never notifies a follow-up that does
    not mention the account.
    """
    if reply.conversation_type != "channel" or not provider.capabilities.threads:
        return
    settings_ = AccountSettings.of(account)
    if hears_channel_in_full(listening_plan(settings_), reply.conversation_id):
        return
    if not follows_channel_threads(settings_, reply.conversation_id):
        return
    await state.follow_thread(str(account.id), reply.conversation_id, reply.thread_id, dict(reply.route or {}))


async def _record_failure(
    db: AsyncSession, account: AgentAccount, event: AccountEvent, decision: Decision, error: str
) -> None:
    account.last_error = error[:2000]
    reply = _new_reply(account, event, decision, draft="")
    reply.draft_text = None
    reply.status = "failed"
    reply.error = error[:2000]
    db.add(reply)
    await db.commit()
    await activity.record_decision(str(account.id), event, decision, "failed", reply_id=reply.id)


async def _notify_pending(db: AsyncSession, account: AgentAccount, agent: Agent | None, reply: AgentAccountReply) -> None:
    """Tell the owner a reply waits for them: the notification bell and a live toast, theirs alone."""
    from app.services.notify import notify_web_user

    await notify_web_user(
        db,
        str(account.user_id),
        title=f"{account.name}: a reply waits for your approval",
        summary=f"{reply.sender_name or 'Someone'}: {reply.inbound_text[:200]}",
        source_label=agent.name if agent else account.name,
        extra_data={
            "notification_source": NOTIFICATION_SOURCE,
            "notification_kind": "pending",
            "account_id": str(account.id),
            "reply_id": str(reply.id),
        },
    )


async def _notify_sent(db: AsyncSession, account: AgentAccount, agent: Agent | None, reply: AgentAccountReply) -> None:
    """Tell the owner the account just posted a reply on its own (``notify_on_send``).

    The bell and a live toast, theirs alone: an owner who does not follow the
    platform's conversations still sees what is said there in their name.
    """
    from app.services.notify import notify_web_user

    where = reply.conversation_name or reply.sender_name or "a conversation"
    await notify_web_user(
        db,
        str(account.user_id),
        title=f"{account.name} replied in {where}",
        summary=f"To {reply.sender_name or 'someone'}: {(reply.sent_text or '')[:200]}",
        source_label=agent.name if agent else account.name,
        extra_data={
            "notification_source": NOTIFICATION_SOURCE,
            "notification_kind": "sent",
            "account_id": str(account.id),
            "reply_id": str(reply.id),
        },
    )


async def _notify_flagged(
    db: AsyncSession, account: AgentAccount, agent: Agent | None, event: AccountEvent, note: str
) -> None:
    """Tell the owner the agent handed them a message: which one, and why it left it to them."""
    from app.services.notify import notify_web_user

    about = event.conversation_name or event.text[:160]
    await notify_web_user(
        db,
        str(account.user_id),
        title=f"{account.name}: {event.sender_name or 'Someone'} needs you",
        summary=(f"{about} - {note}" if note else about)[:400],
        source_label=agent.name if agent else account.name,
        extra_data={
            "notification_source": NOTIFICATION_SOURCE,
            "notification_kind": "flagged",
            "account_id": str(account.id),
        },
    )


# ---------------------------------------------------------------------------
# Approvals
# ---------------------------------------------------------------------------


async def send_pending(
    db: AsyncSession, account: AgentAccount, reply: AgentAccountReply, *, text: str | None, decided_by: uuid.UUID
) -> AgentAccountReply:
    """Post a pending reply, as drafted or as its owner edited it; raises on a provider failure."""
    provider = get_provider(account.provider)
    if provider is None:
        raise AccountUnavailableError("The account's provider is not available.")
    session = await open_session(db, account, provider)
    body = (text if text is not None else reply.draft_text or "").strip()
    if not body:
        raise ValueError("The reply is empty.")
    reply.draft_text = body
    await _post(db, session, provider, account, reply, body)
    reply.decided_by_user_id = decided_by
    reply.decided_at = datetime.now(UTC)
    return reply
