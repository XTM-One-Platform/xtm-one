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

"""How messages reach the agent-account pipeline: subscriptions, polls, notifications.

The account's rules decide what it must hear (``rules.listening_plan``); this
module keeps the provider's push subscriptions in line with that plan -
creating, renewing and deleting them, each with a random ``clientState`` kept
Fernet-encrypted - and polls the chats when push cannot reach the platform.
The notification endpoint trusts an entry only when it names a subscription
recorded here and echoes its ``clientState``.

``run_maintenance`` is called by the scheduler tick; a lease keeps it to one
process at a time across replicas.
"""

from __future__ import annotations

import logging
import secrets
import time
import uuid
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit

from sqlalchemy import and_, delete, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import async_session
from app.core.security import decrypt_value, encrypt_value, timing_safe_str_eq
from app.models.agent_account import AgentAccount, AgentAccountReply, AgentAccountSubscription
from app.services.agent_accounts import activity, state, turns
from app.services.agent_accounts.pipeline import (
    AccountUnavailableError,
    dispatch,
    open_session,
    process_event,
)
from app.services.agent_accounts.problems import Problem, report
from app.services.agent_accounts.providers import (
    AccountProvider,
    DesiredSubscription,
    ExistingSubscription,
    InboundNotification,
    ProviderError,
    ProviderSession,
    UnheardConversation,
    get_provider,
)
from app.services.agent_accounts.rules import AccountSettings, hears_channel_in_full, listening_plan, plan_hears

logger = logging.getLogger(__name__)

NOTIFICATION_PATH = "/api/v1/agent-accounts/notifications"
#: A subscription this close to its expiry is renewed.
RENEW_BEFORE = timedelta(hours=24)
#: An account whose settings did not change is re-synced at least this often.
RESYNC_EVERY = timedelta(hours=1)
#: A reply nobody approved within this delay expires: an answer that late reads oddly.
APPROVAL_TTL = timedelta(hours=24)
MAINTENANCE_LEASE_SECONDS = 50
#: The longest one account's sync keeps others from syncing it at the same time.
SYNC_LEASE_SECONDS = 600
#: Followed threads read per maintenance pass; the others wait for the next one.
FOLLOWED_THREADS_PER_PASS = 100
#: The provider errors after which a subscription is asked for again with its
#: ``fallback_change_type``: a refusal, or an answer the platform cannot read.
#: Throttling, an unavailable provider or a revoked sign-in fail the same way
#: whatever the subscription asks for.
FALLBACK_KINDS = frozenset({"refused", "error"})
#: How often an account heard by a push that may be lost (Gmail through Pub/Sub)
#: is polled all the same (``ProviderCapabilities.poll_fallback``).
FALLBACK_POLL_SECONDS = 300
#: The longest one push-woken read keeps another from starting for the same account.
PUSH_POLL_LEASE_SECONDS = 60


def notification_url(provider_key: str) -> str:
    return f"{settings.base_url.rstrip('/')}{NOTIFICATION_PATH}/{provider_key}"


def push_reachable() -> bool:
    """Whether a provider can notify this platform: it only calls an HTTPS address."""
    return settings.base_url.startswith("https://")


@dataclass
class SyncResult:
    transport: str
    status: str
    problems: list[Problem] = field(default_factory=list)
    events: int = 0
    #: The subscriptions the rules need, and those of them the provider holds.
    wanted: int = 0
    heard: int = 0


@dataclass(frozen=True)
class Operator:
    """The agent account that already operates a platform account."""

    account_id: uuid.UUID
    name: str
    user_id: uuid.UUID

    def message(self, platform_label: str, viewer_id: uuid.UUID) -> str:
        """Why another account stands aside, naming this one to its own owner only."""
        if self.user_id == viewer_id:
            return (
                f"Your agent account '{self.name}' already operates this {platform_label} account: "
                "one agent account per platform account. Add rules to that one, or disable it."
            )
        return (
            f"Another user's agent account already operates this {platform_label} account: "
            "one agent account per platform account."
        )


async def operator_of(
    db: AsyncSession,
    provider_key: str,
    external_id: str | None,
    *,
    excluding: AgentAccount | None = None,
    integration_id: uuid.UUID | None = None,
) -> Operator | None:
    """The enabled agent account, other than *excluding*, operating that platform account.

    The platform account is *external_id* on *provider_key*, or anything signed
    in through *integration_id* on that provider. Two accounts would both
    answer one message and compete for the provider's subscriptions, which it
    grants once per resource; the partial unique indexes
    ``uq_agent_accounts_operated`` / ``uq_agent_accounts_provider_integration``
    are the backstop of this check. Another provider on the same integration
    hears other conversations (a Teams account and an Outlook mailbox).
    """
    same = []
    if external_id:
        same.append(
            and_(AgentAccount.provider == provider_key, AgentAccount.account_external_id == external_id)
        )
    if integration_id is not None:
        same.append(and_(AgentAccount.provider == provider_key, AgentAccount.integration_id == integration_id))
    if not same:
        return None
    query = select(AgentAccount.id, AgentAccount.name, AgentAccount.user_id).where(
        or_(*same), AgentAccount.enabled.is_(True)
    )
    if excluding is not None and excluding.id is not None:
        query = query.where(AgentAccount.id != excluding.id)
    with db.no_autoflush:
        row = (await db.execute(query.order_by(AgentAccount.created_at, AgentAccount.id).limit(1))).first()
    return Operator(row.id, row.name, row.user_id) if row is not None else None


async def _list_held(provider: AccountProvider, session: ProviderSession) -> list[ExistingSubscription]:
    try:
        return await provider.list_subscriptions(session)
    except ProviderError as exc:
        logger.info("Could not list the %s subscriptions of an agent account: %s", provider.label, exc)
        return []


async def _drop_unrecorded(
    db: AsyncSession, provider: AccountProvider, session: ProviderSession
) -> list[ExistingSubscription]:
    """Delete the provider's subscriptions to this platform's address that no account recorded.

    A sync stopped between the provider's answer and the commit (a restart, a
    timeout) leaves a subscription only the provider knows, and a provider may
    grant one per resource: until it expires, the account could never subscribe
    to that resource again. Returns everything the provider listed, for what a
    refusal is then explained by.
    """
    url = notification_url(provider.key)
    held = await _list_held(provider, session)
    listed = [sub for sub in held if sub.notification_url == url]
    if not listed:
        return held
    recorded = set(
        (
            await db.execute(
                select(AgentAccountSubscription.external_id).where(
                    AgentAccountSubscription.external_id.in_([sub.external_id for sub in listed])
                )
            )
        ).scalars()
    )
    for sub in listed:
        if sub.external_id not in recorded:
            await provider.delete_subscription(session, sub.external_id)
    return held


class _Refusals:
    """Why a provider refused a subscription, from what the platform knows.

    A provider granting one subscription per conversation and application
    refuses a conversation another subscription holds as it refuses a missing
    permission. The platform tells the first apart: another agent account
    recorded a subscription to that conversation, or the provider lists one of
    the account's to another address (another platform on the same
    application). A creation that failed while the provider never called the
    notification address back means it could not reach the platform.
    """

    def __init__(
        self,
        db: AsyncSession,
        account: AgentAccount,
        provider: AccountProvider,
        session: ProviderSession,
        held: list[ExistingSubscription] | None,
    ) -> None:
        self.db = db
        self.account = account
        self.provider = provider
        self.session = session
        self._held = held
        self._others: list[tuple[str, str, uuid.UUID]] | None = None

    async def _other_accounts(self) -> list[tuple[str, str, uuid.UUID]]:
        if self._others is None:
            with self.db.no_autoflush:
                rows = (
                    await self.db.execute(
                        select(AgentAccountSubscription.resource, AgentAccount.name, AgentAccount.user_id)
                        .join(AgentAccount, AgentAccount.id == AgentAccountSubscription.account_id)
                        .where(AgentAccount.provider == self.account.provider, AgentAccount.id != self.account.id)
                    )
                ).all()
            self._others = [(row.resource, row.name, row.user_id) for row in rows]
        return self._others

    async def _listed(self) -> list[ExistingSubscription]:
        if self._held is None:
            self._held = await _list_held(self.provider, self.session)
        return self._held

    async def explain(self, exc: ProviderError, desired: DesiredSubscription, started: float) -> Problem:
        if exc.kind == "error" and await state.handshake_since(self.provider.key, started) is False:
            return Problem.of(exc, desired, kind="unreachable")
        if exc.kind != "refused":
            return Problem.of(exc, desired)
        slot = self.provider.subscription_slot(desired.resource)
        for resource, name, user_id in await self._other_accounts():
            if self.provider.subscription_slot(resource) == slot:
                # Another user's account is never named to this one's owner.
                holder = name if user_id == self.account.user_id else None
                return Problem.of(exc, desired, kind="taken_by_account", holder=holder)
        ours = notification_url(self.provider.key)
        for sub in await self._listed():
            if sub.notification_url != ours and self.provider.subscription_slot(sub.resource) == slot:
                host = urlsplit(sub.notification_url).hostname or None
                return Problem.of(exc, desired, kind="taken_elsewhere", holder=host)
        return Problem.of(exc, desired)


async def _sync_subscriptions(
    db: AsyncSession,
    account: AgentAccount,
    provider: AccountProvider,
    session: ProviderSession,
    wanted: list[DesiredSubscription],
) -> tuple[set[str], list[Problem]]:
    """Make the account's subscriptions exactly *wanted*: ``(active resources, problems)``.

    A subscription is its resource AND what it is notified of: one recorded
    with other change types (reactions switched on or off) is deleted
    and created again - a provider may not change them in place, and may hold
    one subscription per resource, so the deletion comes first.

    A subscription the provider refuses with its change types is created with
    its ``fallback_change_type`` instead (the new messages alone), so the
    deletion never leaves the conversation unheard; the refusal is reported
    and stands for ``state.REACTIONS_REFUSED_TTL_SECONDS``, the following
    syncs keeping the fallback rather than recreating it every time.
    """
    existing = (
        await db.execute(select(AgentAccountSubscription).where(AgentAccountSubscription.account_id == account.id))
    ).scalars().all()
    now = datetime.now(UTC)
    active: set[str] = set()
    problems: list[Problem] = []
    wanted = list(wanted)
    for index, desired in enumerate(wanted):
        if desired.fallback_change_type is None:
            continue
        refused = await state.reactions_refused(str(account.id), desired.resource)
        if refused is not None:
            problems.append(_reactions_refused(refused, desired))
            wanted[index] = replace(desired, change_type=desired.fallback_change_type, fallback_change_type=None)
    by_resource = {d.resource: d for d in wanted}
    wanted_keys = {(d.resource, d.change_type) for d in wanted}
    recorded_resources = {sub.resource for sub in existing}
    held = None
    if any(d.resource not in recorded_resources for d in wanted):
        held = await _drop_unrecorded(db, provider, session)
    refusals = _Refusals(db, account, provider, session, held)
    deleted = False
    for sub in existing:
        if (sub.resource, sub.change_type) not in wanted_keys:
            await provider.delete_subscription(session, sub.external_id)
            await db.delete(sub)
            deleted = True
            continue
        if sub.expires_at - now < RENEW_BEFORE:
            try:
                renewed = await provider.renew_subscription(session, sub.external_id)
            except ProviderError as exc:
                problems.append(Problem.of(exc, by_resource.get(sub.resource)))
                active.add(sub.resource)
                continue
            if renewed is None:
                await db.delete(sub)
                continue
            sub.expires_at = renewed
        active.add(sub.resource)
    if deleted:
        # The deletions reach the database before the creations they make room for.
        await db.flush()
    for desired in wanted:
        if desired.resource in active:
            continue
        client_state = secrets.token_urlsafe(32)
        url = notification_url(provider.key)
        started = time.time()
        change_type = desired.change_type
        try:
            created = await provider.create_subscription(
                session,
                desired.resource,
                notification_url=url,
                lifecycle_url=f"{url}?lifecycle=1",
                client_state=client_state,
                change_type=change_type,
            )
        except ProviderError as exc:
            if desired.fallback_change_type is None or exc.kind not in FALLBACK_KINDS:
                problems.append(await refusals.explain(exc, desired, started))
                continue
            logger.info(
                "Agent account %s: a subscription with its changes was refused: %r", account.id, str(exc)[:300]
            )
            change_type = desired.fallback_change_type
            started = time.time()
            try:
                created = await provider.create_subscription(
                    session,
                    desired.resource,
                    notification_url=url,
                    lifecycle_url=f"{url}?lifecycle=1",
                    client_state=client_state,
                    change_type=change_type,
                )
            except ProviderError as fallback_exc:
                problems.append(await refusals.explain(fallback_exc, desired, started))
                continue
            await state.note_reactions_refused(str(account.id), desired.resource, str(exc))
            problems.append(_reactions_refused(str(exc), desired))
        db.add(
            AgentAccountSubscription(
                account_id=account.id,
                external_id=created.external_id,
                resource=desired.resource,
                change_type=change_type,
                client_state_enc=encrypt_value(created.client_state or client_state),
                expires_at=created.expires_at,
            )
        )
        active.add(desired.resource)
    return active, problems


def _reactions_refused(detail: str, desired: DesiredSubscription) -> Problem:
    """The conversation is heard, its reactions read periodically: the provider refused to notify them."""
    return Problem("reactions_refused", detail, covers=desired.covers, name=desired.name)


def schedule_sync(account_id: str, *, poll: bool = False) -> None:
    """Sync the account in a background task, never in the request or the scheduler tick.

    A provider may take long to accept a subscription - Microsoft Graph calls
    the notification address back before it answers, ten seconds at most per
    address - so whoever waited for it would hold a request, a database
    connection and every step behind it.
    """
    from app.channels.base import spawn_channel_task

    spawn_channel_task(_sync_in_background(account_id, poll=poll), name="agent-account-sync")


async def sync_integration_accounts(integration_id: str) -> None:
    """Schedule a sync of every enabled agent account acting with integration *integration_id*.

    Called once an edit of the integration is committed: an account otherwise
    resyncs only on its own change or every ``RESYNC_EVERY``, so a fixed
    integration would keep showing the old error (or a broken one the old
    status) for up to an hour. Never raises: the edit is already saved.
    """
    try:
        async with async_session() as db:
            rows = await db.execute(
                select(AgentAccount.id).where(
                    AgentAccount.integration_id == uuid.UUID(str(integration_id)),
                    AgentAccount.enabled.is_(True),
                )
            )
            account_ids = [str(account_id) for account_id in rows.scalars().all()]
    except Exception:
        logger.exception("Integration %s: its agent accounts could not be listed for a sync", integration_id)
        return
    for account_id in account_ids:
        schedule_sync(account_id)


async def _sync_in_background(account_id: str, *, poll: bool) -> None:
    async with async_session() as db:
        try:
            account = await db.get(AgentAccount, uuid.UUID(account_id))
            if account is not None and account.enabled:
                await sync_account(db, account, poll=poll)
        except Exception:
            await db.rollback()
            logger.exception("Agent account %s: the sync failed", account_id)


async def sync_account(db: AsyncSession, account: AgentAccount, *, poll: bool = True) -> SyncResult | None:
    """Bring *account*'s listening in line with its rules, and poll it when push cannot serve it.

    ``None`` when another process is syncing the account: two syncs at once
    would both create the subscriptions the account lacks.
    """
    lease = f"sync:{account.id}"
    if not await state.take_lease(lease, SYNC_LEASE_SECONDS):
        return None
    try:
        return await _sync_account(db, account, poll=poll)
    finally:
        await state.release_lease(lease)


async def _sync_account(db: AsyncSession, account: AgentAccount, *, poll: bool) -> SyncResult:
    provider = get_provider(account.provider)
    if provider is None:
        problem = Problem("error", f"Unknown provider {account.provider!r}.")
        return await _settle(db, account, SyncResult("none", "error", [problem]))
    settings_ = AccountSettings.of(account)
    plan = listening_plan(settings_)
    try:
        session = await open_session(db, account, provider)
        if not account.account_external_id:
            identity = await provider.identify(session)
            # Checked before the identity is stored: the unique index would
            # refuse the commit of a second account operating it.
            clash = await operator_of(db, account.provider, identity.external_id, excluding=account)
            if clash is not None:
                try:
                    await _sync_subscriptions(db, account, provider, session, [])
                except ProviderError:
                    logger.info("Agent account %s: could not release its subscriptions", account.id)
                own = clash.user_id == account.user_id
                problem = Problem(
                    "operated",
                    clash.message(provider.label, account.user_id),
                    holder=clash.name if own else None,
                    note="operated_own" if own else "operated",
                    note_values={"holder": clash.name} if own else None,
                )
                return await _settle(db, account, SyncResult("none", "error", [problem]))
            account.account_external_id = identity.external_id
            account.account_display_name = account.account_display_name or identity.display_name
            account.account_email = identity.email
            session = session.as_account(identity.external_id)
        else:
            # Still able to act (a linked device still paired).
            await provider.verify_session(session)
    except AccountUnavailableError as exc:
        return await _settle(db, account, SyncResult("none", "error", [Problem("integration", str(exc))]))
    except ProviderError as exc:
        return await _settle(db, account, SyncResult("none", "error", [Problem.of(exc)]))

    wanted: list[DesiredSubscription] = []
    unheard: list[UnheardConversation] = []
    problems: list[Problem] = []
    if not plan.empty and provider.capabilities.push and push_reachable():
        from app.services.agent_accounts.reactions import reactions_wanted

        try:
            wanted, unheard = await provider.desired_listening(
                session, plan, reactions=reactions_wanted(account, provider)
            )
        except ProviderError as exc:
            problems.append(Problem.of(exc))
        else:
            # A conversation the rules cover that no subscription hears is said, never left out.
            problems.extend(
                Problem(
                    "not_subscribed", u.detail, covers=u.covers, name=u.name, note=u.note, note_values=u.note_values
                )
                for u in unheard
            )
    try:
        active, sub_problems = await _sync_subscriptions(db, account, provider, session, wanted)
    except ProviderError as exc:
        active, sub_problems = set(), [Problem.of(exc)]
    problems.extend(sub_problems)

    # Chats with no live push subscription are polled, when the provider can;
    # a provider whose push may be lost is polled anyway, at a slower pace.
    chats_pushed = any(d.covers == provider.chats_covers and d.resource in active for d in wanted)
    fallback = chats_pushed and provider.capabilities.poll_fallback
    poll_chats = bool(plan.chats) and provider.capabilities.poll and (not chats_pushed or fallback)
    # Chats the integration's own webhook delivers are heard in real time, with no subscription.
    webhook_chats = bool(plan.chats) and provider.capabilities.inbound_webhook
    events = 0
    if poll_chats and poll and (not fallback or await state.take_lease(f"fallback-poll:{account.id}", FALLBACK_POLL_SECONDS)):
        try:
            events = await _poll_into_pipeline(account, provider, session, plan)
        except ProviderError as exc:
            problems.append(Problem.of(exc, covers=provider.chats_covers))
    pushed = bool(active) or webhook_chats
    if fallback:
        # Pushed, the poll a safety net only: the account is heard by push.
        transport = "push"
    else:
        transport = "mixed" if pushed and poll_chats else "push" if pushed else "poll" if poll_chats else "none"
    if plan.empty:
        status = "paused"
    elif problems and transport == "none":
        status = "error"
    else:
        status = "active"
    if not push_reachable() and provider.capabilities.channel and (plan.channels or plan.all_channels):
        problems.append(
            Problem(
                "no_public_address",
                "Channels are only heard through push notifications, which need a public HTTPS BASE_URL.",
                note="no_public_address",
            )
        )
    heard = len(active & {d.resource for d in wanted})
    # "Hears N of M" counts the conversations no subscription covers among those wanted.
    return await _settle(
        db,
        account,
        SyncResult(transport, status, problems, events, wanted=len(wanted) + len(unheard), heard=heard),
    )


async def _poll_into_pipeline(account: AgentAccount, provider: AccountProvider, session: ProviderSession, plan) -> int:
    """Read what arrived since the account's cursor and hand it to the pipeline; how many messages."""
    found, cursor = await provider.poll(session, plan, await state.read_cursor(str(account.id)))
    await state.write_cursor(str(account.id), cursor)
    for event in found:
        dispatch(str(account.id), event)
    return len(found)


async def _settle(db: AsyncSession, account: AgentAccount, result: SyncResult) -> SyncResult:
    account.transport = result.transport
    account.status = result.status
    account.sync_report = report(
        result.problems, wanted=result.wanted, heard=result.heard, checked_at=datetime.now(UTC)
    )
    # A sync that leaves the account deaf says why where its owner looks first.
    account.last_error = sync_error(result.problems) if result.status == "error" else None
    await db.commit()
    await state.mark_synced(str(account.id), account.updated_at)
    return result


def sync_error(problems: list[Problem]) -> str:
    """The reason a sync left the account in error, in the words of its first problem."""
    if not problems:
        return "The last sync failed without a reason."
    first = problems[0]
    more = f" (and {len(problems) - 1} more problem(s))" if len(problems) > 1 else ""
    return f"The last sync failed ({first.kind}): {first.detail.strip()[:500]}{more}"


async def release_account(db: AsyncSession, account: AgentAccount) -> None:
    """Delete the account's push subscriptions at the provider, before the account goes."""
    provider = get_provider(account.provider)
    subs = (
        await db.execute(select(AgentAccountSubscription).where(AgentAccountSubscription.account_id == account.id))
    ).scalars().all()
    if provider is not None and subs:
        try:
            session = await open_session(db, account, provider)
        except AccountUnavailableError:
            session = None
        if session is not None:
            for sub in subs:
                await provider.delete_subscription(session, sub.external_id)
    await db.execute(delete(AgentAccountSubscription).where(AgentAccountSubscription.account_id == account.id))
    await state.forget_account(str(account.id))


# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------


async def accept_notifications(
    provider: AccountProvider, entries: list[InboundNotification]
) -> list[tuple[str, InboundNotification]]:
    """The entries that name a recorded subscription and echo its ``clientState``, with their account.

    A change to an existing message (``provider.updated_message``) is kept
    only when the message is one of the account's sent answers: a subscription
    that hears changes is notified of every edit and reaction in its
    conversations, and that noise must cost neither a task nor a database
    session of its own. One query per delivery, in this session.
    """
    entries = [e for e in entries if e.client_state]
    if not entries:
        return []
    ids = {e.subscription_id for e in entries}
    async with async_session() as db:
        rows = (
            await db.execute(
                select(AgentAccountSubscription.external_id, AgentAccountSubscription.account_id,
                       AgentAccountSubscription.client_state_enc, AgentAccount.provider)
                .join(AgentAccount, AgentAccount.id == AgentAccountSubscription.account_id)
                .where(AgentAccountSubscription.external_id.in_(ids))
            )
        ).all()
        by_id = {row.external_id: row for row in rows}
        accepted: list[tuple[str, InboundNotification]] = []
        for entry in entries:
            row = by_id.get(entry.subscription_id)
            if row is None or row.provider != provider.key:
                continue
            try:
                expected = decrypt_value(row.client_state_enc)
            except Exception:
                # A secret that no longer decrypts (a changed SECRET_KEY) proves nothing.
                logger.warning("Agent account subscription %s has an unreadable clientState", entry.subscription_id)
                continue
            if timing_safe_str_eq(entry.client_state, expected):
                accepted.append((str(row.account_id), entry))
        return await _changes_to_answers_only(db, provider, accepted)


async def _changes_to_answers_only(
    db: AsyncSession, provider: AccountProvider, accepted: list[tuple[str, InboundNotification]]
) -> list[tuple[str, InboundNotification]]:
    """*accepted* without the changes to a message that is not one of the account's sent answers."""
    changes: dict[int, tuple[str, str, str]] = {}
    for index, (account_id, entry) in enumerate(accepted):
        updated = None if entry.lifecycle_event else provider.updated_message(entry)
        if updated is not None:
            changes[index] = (account_id, updated.conversation_id, updated.message_id)
    if not changes:
        return accepted
    rows = await db.execute(
        select(AgentAccountReply.account_id, AgentAccountReply.conversation_id, AgentAccountReply.sent_message_id)
        .where(
            AgentAccountReply.account_id.in_({uuid.UUID(account_id) for account_id, _, _ in changes.values()}),
            AgentAccountReply.sent_message_id.in_({message_id for _, _, message_id in changes.values()}),
            AgentAccountReply.status == "sent",
        )
    )
    answers = {(str(account_id), conversation_id, message_id) for account_id, conversation_id, message_id in rows.all()}
    return [item for index, item in enumerate(accepted) if index not in changes or changes[index] in answers]


async def handle_notification(account_id: str, provider_key: str, entry: InboundNotification) -> None:
    """Read the message a trusted notification names and process it; a lifecycle event re-syncs."""
    provider = get_provider(provider_key)
    if provider is None:
        return
    updated = None if entry.lifecycle_event else provider.updated_message(entry)
    if updated is not None:
        # A change to an existing message: a reaction on one of the account's
        # answers. It opens no session here: ``on_updated_message``
        # reads the account and the answer in its own.
        from app.services.agent_accounts.reactions import on_updated_message

        await on_updated_message(account_id, updated)
        return
    event = None
    try:
        async with async_session() as db:
            account = await db.get(AgentAccount, uuid.UUID(account_id))
            if account is None:
                return
            if entry.lifecycle_event:
                await _lifecycle(db, account, provider, entry)
                return
            if not account.enabled:
                return
            session = await open_session(db, account, provider)
            plan = listening_plan(AccountSettings.of(account))
            if provider.capabilities.push_wakes_poll:
                # The delivery names no message: what arrived since the cursor is read now.
                await _poll_on_push(account, provider, session, plan)
                return
            event = await provider.event_from_notification(session, entry)
    except (AccountUnavailableError, ProviderError) as exc:
        logger.info("Agent account %s could not read a notified message: %s", account_id, exc)
        return
    except Exception:
        logger.exception("Agent account %s failed on a notification", account_id)
        return
    if event is None:
        return
    if not plan_hears(plan, event):
        # A subscription the rules no longer want (a sync still running, or one
        # that failed): the scheduler's next pass syncs the account again.
        await state.clear_synced(account_id)
    await process_event(account_id, event)


async def _poll_on_push(account: AgentAccount, provider: AccountProvider, session: ProviderSession, plan) -> None:
    """One read of the account's new messages; a read already running for it covers this push.

    A push landing while a read is past its history call waits for the
    fallback poll (``FALLBACK_POLL_SECONDS``) at worst; the pipeline's dedup
    drops a message two reads bring.
    """
    if not plan.chats:
        return
    lease = f"push-poll:{account.id}"
    if not await state.take_lease(lease, PUSH_POLL_LEASE_SECONDS):
        return
    try:
        await _poll_into_pipeline(account, provider, session, plan)
    finally:
        await state.release_lease(lease)


async def _lifecycle(
    db: AsyncSession, account: AgentAccount, provider: AccountProvider, entry: InboundNotification
) -> None:
    sub = (
        await db.execute(
            select(AgentAccountSubscription).where(AgentAccountSubscription.external_id == entry.subscription_id)
        )
    ).scalar_one_or_none()
    if sub is None:
        return
    if entry.lifecycle_event == "reauthorizationRequired":
        session = await open_session(db, account, provider)
        renewed = await provider.renew_subscription(session, sub.external_id)
        if renewed is None:
            await db.delete(sub)
        else:
            sub.expires_at = renewed
        await db.commit()
    elif entry.lifecycle_event == "subscriptionRemoved":
        await db.delete(sub)
        await db.commit()
        await state.clear_synced(str(account.id))
    else:
        logger.info("Agent account %s: lifecycle event %s", account.id, entry.lifecycle_event)


# ---------------------------------------------------------------------------
# The scheduler's step
# ---------------------------------------------------------------------------


async def run_maintenance() -> int:
    """One pass: parked messages a dead process left, expired approvals, every account's
    listening, the reactions of the answers no subscription hears."""
    if not await state.take_lease("maintenance", MAINTENANCE_LEASE_SECONDS):
        return 0
    actions = 0
    from app.channels.base import spawn_channel_task
    from app.services.agent_accounts.reactions import run_due_checks

    # Provider reads, never in the scheduler tick (the same reason as ``schedule_sync``).
    spawn_channel_task(run_due_checks(), name="agent-account-reaction-checks")
    spawn_channel_task(follow_threads(), name="agent-account-thread-follow")
    for _member, account_id, event in await state.overdue():
        dispatch_after_grace(account_id, event)
        actions += 1
    # Messages queued behind a turn whose process died: a new turn.
    for account_id, conversation_id in await turns.orphaned():
        dispatch_orphaned_turn(account_id, conversation_id)
        actions += 1
    async with async_session() as db:
        expired = await db.execute(
            update(AgentAccountReply)
            .where(
                AgentAccountReply.status == "pending",
                AgentAccountReply.created_at < datetime.now(UTC) - APPROVAL_TTL,
            )
            .values(status="expired", updated_at=datetime.now(UTC))
        )
        actions += expired.rowcount or 0
        actions += await activity.purge_old_decisions(db)
        await db.commit()
        account_ids = (await db.execute(select(AgentAccount.id).where(AgentAccount.enabled.is_(True)))).scalars().all()
    for account_id in account_ids:
        async with async_session() as db:
            try:
                account = await db.get(AgentAccount, account_id)
                due = account is not None and account.enabled and await _due(db, account)
            except Exception:
                logger.exception("Agent account %s maintenance failed", account_id)
                continue
        if due:
            schedule_sync(str(account_id), poll=True)
            actions += 1
    return actions


async def follow_threads() -> int:
    """Read the channel threads the accounts answered in for follow-ups no subscription brings.

    A channel heard for mentions alone notifies nothing else: the replies of a
    thread the account answered in are read every ``state.FOLLOW_EVERY_SECONDS``
    while the thread stays active, and go through the pipeline like a
    notified message (its dedup drops one a mention already brought).
    """
    read = 0
    for account_id, conversation_id, thread_id, route, since in await state.due_followed_threads(
        FOLLOWED_THREADS_PER_PASS
    ):
        up_to = since
        try:
            async with async_session() as db:
                account = await db.get(AgentAccount, uuid.UUID(account_id))
                provider = get_provider(account.provider) if account is not None else None
                if account is None or provider is None or not account.enabled:
                    continue
                if hears_channel_in_full(listening_plan(AccountSettings.of(account)), conversation_id):
                    continue
                session = await open_session(db, account, provider)
                found = await provider.thread_messages_since(session, route, datetime.fromtimestamp(since, UTC))
        except (AccountUnavailableError, ProviderError) as exc:
            logger.info("Agent account %s could not read a followed thread: %s", account_id, exc)
            found = []
        except Exception:
            logger.exception("Agent account %s failed on a followed thread", account_id)
            found = []
        for event in found:
            up_to = max(up_to, event.created_at.timestamp())
            dispatch(account_id, event)
        await state.followed_thread_read(account_id, conversation_id, thread_id, route, up_to)
        read += 1
    return read


def dispatch_after_grace(account_id: str, event) -> None:
    from app.channels.base import spawn_channel_task

    spawn_channel_task(process_event(account_id, event, after_grace=True), name="agent-account-grace")


async def resume_orphaned_turn(account_id: str, conversation_id: str) -> bool:
    """Answer the queue a dead process left in a conversation; whether this process took it."""
    from app.services.agent_accounts.pipeline import hold_turns

    holder = turns.new_holder()
    if not await turns.take(account_id, conversation_id, holder):
        return False
    await hold_turns(account_id, conversation_id, holder)
    return True


def dispatch_orphaned_turn(account_id: str, conversation_id: str) -> None:
    from app.channels.base import spawn_channel_task

    spawn_channel_task(resume_orphaned_turn(account_id, conversation_id), name="agent-account-turn-resume")


async def _due(db: AsyncSession, account: AgentAccount) -> bool:
    """Whether the account needs a sync now: it polls, its settings changed, a subscription expires, or an hour passed."""
    if account.transport in ("poll", "mixed"):
        return True
    provider = get_provider(account.provider)
    if account.transport == "push" and provider is not None and provider.capabilities.poll_fallback:
        # Its fallback poll is paced by its own lease in ``_sync_account``.
        return True
    if not await state.synced_since(str(account.id), account.updated_at, RESYNC_EVERY):
        return True
    soon = datetime.now(UTC) + RENEW_BEFORE
    expiring = (
        await db.execute(
            select(AgentAccountSubscription.id).where(
                AgentAccountSubscription.account_id == account.id,
                AgentAccountSubscription.expires_at < soon,
            ).limit(1)
        )
    ).first()
    return expiring is not None
