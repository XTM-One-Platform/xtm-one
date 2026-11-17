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

"""Saving an agent account: the one writer of its configuration.

The REST routes (``api/v1/agent_accounts.py``) and the agent tools
(``builtin_tools/agent_accounts.py``) create, change and delete accounts here,
so every rule an account must satisfy is checked in one place:

- the integration is one the OWNER can see and whose token they may use, of
  the provider's integration provider; the agent one the owner can see, enabled;
- one enabled agent account per platform account, and per provider and
  integration (:func:`claim`, with the unique indexes behind it, :func:`flush_claimed`);
- the rules, the schedule and the mailbox actions are normalized by the engine.

A refusal is an :class:`AccountRefusedError`: the status the REST routes answer,
and the sentence the tools return.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.schemas.agent_account import (
    AgentAccountCreate,
    AgentAccountResponse,
    AgentAccountSyncReport,
    AgentAccountUpdate,
    AgentAccountWrite,
    SimulateRequest,
    SimulateResponse,
)
from app.core.utils import safe_uuid
from app.models.agent import Agent
from app.models.agent_account import AgentAccount, AgentAccountReply, AgentAccountSubscription
from app.models.integration import Integration
from app.models.user import User
from app.services.agent_accounts import state as account_state
from app.services.agent_accounts import triage
from app.services.agent_accounts.events import AccountEvent, AccountIdentity
from app.services.agent_accounts.listening import operator_of, release_account, schedule_sync
from app.services.agent_accounts.pipeline import (
    AccountUnavailableError,
    open_integration_session,
    sender_directory,
)
from app.services.agent_accounts.providers import AccountProvider, ProviderError, get_provider
from app.services.agent_accounts.reactions import resume_checks as resume_reaction_checks
from app.services.agent_accounts.rules import (
    AccountSettings,
    EventState,
    RuleError,
    evaluate,
    label_text,
    listening_plan,
    normalize_active_hours,
    normalize_rules,
    preset,
    rule_group_names,
)
from app.services.audit import log_audit
from app.services.email_signatures import signature_providers
from app.services.integration_credentials import oauth_credentials_usable
from app.services.reaction_feedback import ReactionFeedbackConfig, effective_config

if TYPE_CHECKING:
    from fastapi import Request

logger = logging.getLogger(__name__)

NOT_FOUND = "Agent account not found"
#: How long a save waits for the provider to say who an integration is signed in as.
IDENTITY_TIMEOUT_SECONDS = 10.0

#: The scalar settings a write copies as given (``apply_settings``).
_SCALAR_SETTINGS = (
    "name",
    "description",
    "enabled",
    "response_mode",
    "ai_label_enabled",
    "ai_label_text",
    "ai_label_position",
    "grace_period_seconds",
    "owner_takeover_minutes",
    "max_replies_per_hour",
    "max_turns_per_hour",
    "max_reply_recipients",
    "context_messages",
    "instructions",
    "notify_on_send",
)


class AccountRefusedError(Exception):
    """A write or a lookup the account's rules refuse: the REST status, and why in words."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _id(value: str | uuid.UUID | None, not_found: str) -> uuid.UUID:
    """A client id naming one row; a malformed one names none (the unknown id's 404)."""
    parsed = value if isinstance(value, uuid.UUID) else safe_uuid(value)
    if parsed is None:
        raise AccountRefusedError(404, not_found)
    return parsed


def provider_for(key: str) -> AccountProvider:
    provider = get_provider(key)
    if provider is None:
        raise AccountRefusedError(400, f"Unknown provider {key!r}.")
    return provider


async def owner_of(db: AsyncSession, account: AgentAccount, caller: User) -> User:
    if account.user_id == caller.id:
        return caller
    owner = await db.get(User, account.user_id)
    if owner is None:
        raise AccountRefusedError(404, NOT_FOUND)
    return owner


async def load_account(
    db: AsyncSession, account_id: str | uuid.UUID, user: User, *, is_admin: bool
) -> AgentAccount:
    """The account, for its owner or (*is_admin*) an administrator; anyone else gets the unknown id's 404."""
    query = select(AgentAccount).where(AgentAccount.id == _id(account_id, NOT_FOUND))
    if not is_admin:
        query = query.where(AgentAccount.user_id == user.id)
    account = (await db.execute(query)).scalar_one_or_none()
    if account is None:
        raise AccountRefusedError(404, NOT_FOUND)
    return account


async def _visible_integration(db: AsyncSession, owner: User, integration_id: uuid.UUID) -> Integration | None:
    from app.api.v1.helpers import get_user_group_ids, user_visible_filter

    groups = await get_user_group_ids(db, owner.id)
    return (
        await db.execute(
            select(Integration).where(Integration.id == integration_id, user_visible_filter(Integration, owner.id, groups))
        )
    ).scalar_one_or_none()


async def check_integration(
    db: AsyncSession, owner: User, provider: AccountProvider, integration_id: str | uuid.UUID
) -> Integration:
    integration = await _visible_integration(db, owner, _id(integration_id, "Integration not found"))
    if integration is None:
        raise AccountRefusedError(400, "The integration is not one the account's owner can see.")
    if integration.provider != provider.integration_provider:
        raise AccountRefusedError(400, f"The integration is not a {provider.label} integration.")
    if not oauth_credentials_usable(integration, owner.id):
        raise AccountRefusedError(
            400,
            "The integration's token is not shared with the account's owner: connect one signed in as the account.",
        )
    return integration


async def usable_integrations(db: AsyncSession, owner: User, provider: AccountProvider) -> list[Integration]:
    """The integrations *owner* could give an account of *provider*: what :func:`check_integration` accepts."""
    from app.api.v1.helpers import get_user_group_ids, user_visible_filter

    groups = await get_user_group_ids(db, owner.id)
    rows = (
        await db.execute(
            select(Integration)
            .where(
                Integration.provider == provider.integration_provider,
                user_visible_filter(Integration, owner.id, groups),
            )
            .order_by(Integration.name)
        )
    ).scalars()
    return [integration for integration in rows if oauth_credentials_usable(integration, owner.id)]


async def check_agent(db: AsyncSession, owner: User, agent_id: str | uuid.UUID) -> Agent:
    from app.api.v1.helpers import get_user_group_ids, user_visible_filter

    parsed = _id(agent_id, "Agent not found")
    groups = await get_user_group_ids(db, owner.id)
    agent = (
        await db.execute(select(Agent).where(Agent.id == parsed, user_visible_filter(Agent, owner.id, groups)))
    ).scalar_one_or_none()
    if agent is None:
        raise AccountRefusedError(400, "The agent is not one the account's owner can see.")
    if not agent.enabled:
        raise AccountRefusedError(400, "The agent is disabled.")
    return agent


async def apply_signature(
    db: AsyncSession, account: AgentAccount, owner: User, provider: AccountProvider, body: AgentAccountWrite
) -> None:
    """The signature source *body* sets: a signature integration the owner can see, on a signed provider."""
    if body.signature_source is None and body.signature_integration_id is None:
        return
    source = body.signature_source or account.signature_source or "own"
    if source != "own" and not provider.capabilities.signature:
        raise AccountRefusedError(400, f"{provider.label} replies carry no signature to choose.")
    integration_id = account.signature_integration_id
    if body.signature_integration_id is not None:
        integration_id = None
        if body.signature_integration_id:
            integration = await _visible_integration(
                db, owner, _id(body.signature_integration_id, "Integration not found")
            )
            if integration is None:
                raise AccountRefusedError(400, "The integration is not one the account's owner can see.")
            if integration.provider not in signature_providers():
                raise AccountRefusedError(400, "The integration does not hold email signatures.")
            integration_id = integration.id
    if source == "integration" and integration_id is None:
        raise AccountRefusedError(400, "Pick the signature integration the replies' signature is read from.")
    account.signature_source = source
    account.signature_integration_id = integration_id if source == "integration" else None


async def read_identity(
    db: AsyncSession, provider: AccountProvider, *, owner_id: uuid.UUID, integration_id: uuid.UUID
) -> AccountIdentity | None:
    """Who the integration is signed in as; ``None`` when the provider cannot say in time.

    The background sync reads it again, and refuses there what is refused here.
    """
    try:
        signed_in = await open_integration_session(
            db, integration_id=integration_id, owner_id=owner_id, provider=provider
        )
        return await asyncio.wait_for(provider.identify(signed_in), timeout=IDENTITY_TIMEOUT_SECONDS)
    except (AccountUnavailableError, ProviderError, TimeoutError) as exc:
        logger.info("Agent account: could not read who integration %s is signed in as: %s", integration_id, exc)
        return None


async def claim(
    db: AsyncSession,
    provider: AccountProvider,
    caller: User,
    *,
    owner_id: uuid.UUID,
    integration_id: uuid.UUID | None,
    external_id: str | None,
    excluding: AgentAccount | None = None,
) -> AccountIdentity | None:
    """Refuse (409) a second enabled agent account on one integration or one platform account.

    *external_id* is what the account is known to be signed in as, ``None`` for
    a new sign-in: the identity is then read from the provider and returned,
    for the caller to store.
    """
    identity = None
    with db.no_autoflush:
        if external_id is None and integration_id is not None:
            identity = await read_identity(db, provider, owner_id=owner_id, integration_id=integration_id)
            external_id = identity.external_id if identity else None
        operator = await operator_of(
            db, provider.key, external_id, excluding=excluding, integration_id=integration_id
        )
    if operator is not None:
        raise AccountRefusedError(409, operator.message(provider.label, caller.id))
    return identity


def adopt_identity(account: AgentAccount, identity: AccountIdentity | None) -> None:
    if identity is None:
        return
    account.account_external_id = identity.external_id
    account.account_display_name = account.account_display_name or identity.display_name
    account.account_email = identity.email


async def flush_claimed(db: AsyncSession) -> None:
    """Write the account; the unique indexes refuse what a concurrent save claimed first."""
    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        raise AccountRefusedError(
            409, "Another agent account already operates this account: one agent account per platform account."
        ) from None


def apply_settings(
    account: AgentAccount,
    body: AgentAccountWrite,
    provider: AccountProvider,
    *,
    clear_active_hours: bool = False,
) -> None:
    """Copy the scalar settings *body* sets, rules and schedule normalized by the engine.

    The rules are checked against what *provider* can serve. The reaction and
    mailbox settings are merged key by key over what the account holds: a key
    the body leaves out keeps its stored value, so a write naming one setting
    never resets the others.
    """
    for field in _SCALAR_SETTINGS:
        value = getattr(body, field)
        if value is not None:
            setattr(account, field, value.strip() if isinstance(value, str) and field != "instructions" else value)
    try:
        if body.rules is not None:
            account.rules = normalize_rules(body.rules, capabilities=provider.capabilities)
        if body.reaction_feedback is not None:
            account.reaction_feedback = merged_reaction_feedback(account.reaction_feedback, body.reaction_feedback)
        if body.mailbox_actions is not None:
            account.mailbox_actions = triage.merge_mailbox_actions(account.mailbox_actions, body.mailbox_actions)
        if clear_active_hours:
            account.active_hours = None
        elif body.active_hours is not None:
            account.active_hours = normalize_active_hours(body.active_hours)
    except RuleError as exc:
        raise AccountRefusedError(400, str(exc)) from None


def merged_reaction_feedback(stored: object, patch: ReactionFeedbackConfig) -> dict[str, Any]:
    """The fields *patch* sets over the setting *stored* holds, checked again. Raises :class:`RuleError`."""
    merged = effective_config(stored).model_dump()
    merged.update(patch.model_dump(include=patch.model_fields_set))
    try:
        return ReactionFeedbackConfig.model_validate(merged).model_dump()
    except ValidationError as exc:
        raise RuleError(exc.errors()[0].get("msg", "Invalid reaction feedback setting.")) from None


def _sync_report(raw: Any) -> AgentAccountSyncReport | None:
    """The stored report as served; one that no longer reads is the report of no sync."""
    if not isinstance(raw, dict):
        return None
    try:
        return AgentAccountSyncReport.model_validate(raw)
    except ValidationError:
        logger.warning("Unreadable agent account sync report: served as none")
        return None


async def serialize(db: AsyncSession, accounts: list[AgentAccount], caller: User) -> list[AgentAccountResponse]:
    """The accounts as the REST routes serve them, with the names of what they bind."""
    if not accounts:
        return []
    ids = [a.id for a in accounts]
    pending = dict(
        (
            await db.execute(
                select(AgentAccountReply.account_id, func.count())
                .where(AgentAccountReply.account_id.in_(ids), AgentAccountReply.status == "pending")
                .group_by(AgentAccountReply.account_id)
            )
        ).all()
    )
    subscriptions: dict[Any, tuple[int, Any]] = {
        account_id: (int(count), first_expiry)
        for account_id, count, first_expiry in (
            await db.execute(
                select(
                    AgentAccountSubscription.account_id,
                    func.count(),
                    func.min(AgentAccountSubscription.expires_at),
                )
                .where(AgentAccountSubscription.account_id.in_(ids))
                .group_by(AgentAccountSubscription.account_id)
            )
        ).all()
    }
    # Where each account's read stands (a Gmail mailbox: its last history id).
    cursors = await account_state.read_cursors([str(i) for i in ids])
    integration_ids = {a.integration_id for a in accounts if a.integration_id} | {
        a.signature_integration_id for a in accounts if a.signature_integration_id
    }
    agent_ids = {a.agent_id for a in accounts if a.agent_id}
    owner_ids = {a.user_id for a in accounts}
    integrations = dict(
        (await db.execute(select(Integration.id, Integration.name).where(Integration.id.in_(integration_ids)))).all()
    ) if integration_ids else {}
    agents = dict(
        (await db.execute(select(Agent.id, Agent.name).where(Agent.id.in_(agent_ids)))).all()
    ) if agent_ids else {}
    owners = dict((await db.execute(select(User.id, User.full_name).where(User.id.in_(owner_ids)))).all())
    out = []
    for a in accounts:
        provider = get_provider(a.provider)
        out.append(
            AgentAccountResponse(
                id=str(a.id),
                user_id=str(a.user_id),
                owner_name=owners.get(a.user_id),
                name=a.name,
                description=a.description,
                provider=a.provider,
                provider_label=provider.label if provider else None,
                integration_id=str(a.integration_id) if a.integration_id else None,
                integration_name=integrations.get(a.integration_id),
                agent_id=str(a.agent_id) if a.agent_id else None,
                agent_name=agents.get(a.agent_id),
                mode=a.mode,
                enabled=a.enabled,
                response_mode=a.response_mode,
                ai_label_enabled=a.ai_label_enabled,
                ai_label_text=a.ai_label_text,
                ai_label_preview=label_text(a.mode, a.ai_label_text, a.account_display_name),
                ai_label_position=a.ai_label_position,
                grace_period_seconds=a.grace_period_seconds,
                owner_takeover_minutes=a.owner_takeover_minutes,
                max_replies_per_hour=a.max_replies_per_hour,
                max_turns_per_hour=a.max_turns_per_hour,
                max_reply_recipients=a.max_reply_recipients,
                context_messages=a.context_messages,
                active_hours=a.active_hours,
                instructions=a.instructions,
                rules=list(a.rules or []),
                reaction_feedback=effective_config(a.reaction_feedback).model_dump(),
                notify_on_send=a.notify_on_send,
                mailbox_actions=triage.effective_mailbox_actions(a.mailbox_actions),
                signature_source=a.signature_source or "own",
                signature_integration_id=str(a.signature_integration_id) if a.signature_integration_id else None,
                signature_integration_name=integrations.get(a.signature_integration_id),
                account_external_id=a.account_external_id,
                account_display_name=a.account_display_name,
                account_email=a.account_email,
                transport=a.transport,
                status=a.status,
                last_error=a.last_error,
                sync_report=_sync_report(a.sync_report),
                last_event_at=_iso(a.last_event_at),
                last_reply_at=_iso(a.last_reply_at),
                pending_count=int(pending.get(a.id, 0)),
                subscription_count=subscriptions.get(a.id, (0, None))[0],
                subscription_expires_at=_iso(subscriptions.get(a.id, (0, None))[1]),
                read_cursor=cursors.get(str(a.id)),
                read_cursor_label=(provider.cursor_label if provider else None) if str(a.id) in cursors else None,
                can_manage=a.user_id == caller.id or bool(caller.is_admin),
                created_at=_iso(a.created_at) or "",
                updated_at=_iso(a.updated_at) or "",
            )
        )
    return out


def _audit_details(details: dict[str, Any] | None, via: str | None) -> dict[str, Any] | None:
    if via is None:
        return details
    return {**(details or {}), "via": via}


async def create_account(
    db: AsyncSession,
    caller: User,
    body: AgentAccountCreate,
    *,
    request: Request | None = None,
    via: str | None = None,
) -> AgentAccount:
    """Create an account for *caller* from its mode's preset; committed, its sync scheduled.

    *via* marks the audit row of a write the caller asked an agent for (``agent``).
    """
    provider = provider_for(body.provider)
    integration = await check_integration(db, caller, provider, body.integration_id)
    agent = await check_agent(db, caller, body.agent_id)
    identity = (
        await claim(db, provider, caller, owner_id=caller.id, integration_id=integration.id, external_id=None)
        if body.enabled is not False
        else None
    )
    base = preset(body.mode, provider.kind)
    account = AgentAccount(
        user_id=caller.id,
        name=body.name.strip(),
        provider=provider.key,
        integration_id=integration.id,
        agent_id=agent.id,
        mode=body.mode,
        enabled=True,
        response_mode=base["response_mode"],
        ai_label_enabled=base["ai_label_enabled"],
        ai_label_position="suffix",
        grace_period_seconds=base["grace_period_seconds"],
        owner_takeover_minutes=base["owner_takeover_minutes"],
        max_replies_per_hour=base["max_replies_per_hour"],
        max_turns_per_hour=base["max_turns_per_hour"],
        max_reply_recipients=base["max_reply_recipients"],
        context_messages=base["context_messages"],
        rules=base["rules"],
        transport="none",
        status="setup",
    )
    apply_settings(account, body, provider)
    await apply_signature(db, account, caller, provider, body)
    adopt_identity(account, identity)
    if not account.enabled:
        account.status = "paused"
    db.add(account)
    await flush_claimed(db)
    await log_audit(
        db,
        user=caller,
        action="create",
        entity_type="agent_account",
        entity_id=account.id,
        entity_name=account.name,
        summary=f"Created agent account '{account.name}' ({body.mode}, {provider.label})",
        details=_audit_details({"mode": body.mode, "provider": provider.key, "agent_id": str(agent.id)}, via),
        request=request,
    )
    await db.commit()
    if account.enabled:
        schedule_sync(str(account.id))
    return account


async def update_account(
    db: AsyncSession,
    account: AgentAccount,
    caller: User,
    body: AgentAccountUpdate,
    *,
    request: Request | None = None,
    via: str | None = None,
) -> AgentAccount:
    """Edit *account*; a changed integration or agent is checked against the account's OWNER.

    Committed: an enabled account is synced in the background, one this save
    switched off releases its subscriptions and is paused.
    """
    owner = await owner_of(db, account, caller)
    provider = provider_for(account.provider)
    was_enabled = account.enabled
    reactions_before = effective_config(account.reaction_feedback)
    new_integration = None
    if body.integration_id is not None and body.integration_id != str(account.integration_id):
        new_integration = await check_integration(db, owner, provider, body.integration_id)
    if body.agent_id is not None and body.agent_id != str(account.agent_id):
        account.agent_id = (await check_agent(db, owner, body.agent_id)).id
    if body.mode is not None:
        account.mode = body.mode
    with db.no_autoflush:
        apply_settings(account, body, provider, clear_active_hours=body.clear_active_hours)
        await apply_signature(db, account, owner, provider, body)
        identity = None
        if account.enabled and (new_integration is not None or not was_enabled or not account.account_external_id):
            # Before anything is released: a refused save leaves the account as it was.
            identity = await claim(
                db,
                provider,
                caller,
                owner_id=account.user_id,
                integration_id=new_integration.id if new_integration else account.integration_id,
                external_id=None if new_integration else account.account_external_id,
                excluding=account,
            )
        if new_integration is not None:
            # The subscriptions belong to the previous sign-in: released with its token.
            await release_account(db, account)
            account.integration_id = new_integration.id
            # A new sign-in may be another account: what it is signed in as now.
            account.account_external_id = None
            account.account_display_name = None
            account.account_email = None
        adopt_identity(account, identity)
        if account.enabled:
            # Synced in the background (``schedule_sync``): the list polls until it settles.
            account.status = "setup"
    await flush_claimed(db)
    await log_audit(
        db,
        user=caller,
        action="update",
        entity_type="agent_account",
        entity_id=account.id,
        entity_name=account.name,
        summary=f"Updated agent account '{account.name}'",
        details=_audit_details({"fields": sorted(body.model_dump(exclude_unset=True))}, via),
        request=request,
    )
    await db.commit()
    if account.enabled:
        schedule_sync(str(account.id))
        if not was_enabled or effective_config(account.reaction_feedback) != reactions_before:
            # Reactions count again, or count otherwise: every answer still in
            # its window is read again under the setting.
            await resume_reaction_checks(db, account)
    elif was_enabled:
        await release_account(db, account)
        account.transport = "none"
        account.status = "paused"
        await db.commit()
    return account


async def resync_account(db: AsyncSession, account: AgentAccount) -> None:
    """Read the account's identity again and bring its subscriptions up to date, in the background."""
    account.account_external_id = None
    if account.enabled:
        account.status = "setup"
    await db.commit()
    if account.enabled:
        schedule_sync(str(account.id))


async def delete_account(
    db: AsyncSession,
    account: AgentAccount,
    caller: User,
    *,
    request: Request | None = None,
    via: str | None = None,
) -> None:
    """Delete *account*, its subscriptions at the provider first; committed."""
    await release_account(db, account)
    await log_audit(
        db,
        user=caller,
        action="delete",
        entity_type="agent_account",
        entity_id=account.id,
        entity_name=account.name,
        summary=f"Deleted agent account '{account.name}'",
        details=_audit_details(None, via),
        request=request,
    )
    await db.delete(account)
    await db.commit()


def saved_settings(account: AgentAccount) -> AgentAccountWrite:
    """*account*'s settings as a draft the simulator reads: a preview of the saved account.

    Not validated again: what the account holds was normalized when it was saved.
    """
    return AgentAccountWrite.model_construct(
        enabled=account.enabled,
        response_mode=account.response_mode,
        ai_label_enabled=account.ai_label_enabled,
        grace_period_seconds=account.grace_period_seconds,
        owner_takeover_minutes=account.owner_takeover_minutes,
        max_replies_per_hour=account.max_replies_per_hour,
        active_hours=account.active_hours,
        instructions=account.instructions,
        rules=list(account.rules or []),
    )


async def simulate(db: AsyncSession, body: SimulateRequest) -> SimulateResponse:
    """What the rule engine decides on a sample message: nothing is sent, nothing written.

    The sender's groups are the ones given, or those of the XTM One user whose
    email the sample message carries - read as the pipeline reads them.
    """
    # An unknown provider is refused by ``SimulateRequest`` (422).
    simulated = get_provider(body.provider) if body.provider else None
    base = preset(body.mode, simulated.kind if simulated else "chat")
    draft = body.settings
    try:
        rules = (
            normalize_rules(draft.rules, capabilities=simulated.capabilities if simulated else None)
            if draft.rules is not None
            else base["rules"]
        )
        active_hours = normalize_active_hours(draft.active_hours) if draft.active_hours else None
    except RuleError as exc:
        raise AccountRefusedError(400, str(exc)) from None
    settings_ = AccountSettings(
        enabled=True if draft.enabled is None else draft.enabled,
        mode=body.mode,
        response_mode=draft.response_mode or base["response_mode"],
        ai_label_enabled=base["ai_label_enabled"] if draft.ai_label_enabled is None else draft.ai_label_enabled,
        grace_period_seconds=(
            base["grace_period_seconds"] if draft.grace_period_seconds is None else draft.grace_period_seconds
        ),
        owner_takeover_minutes=(
            base["owner_takeover_minutes"] if draft.owner_takeover_minutes is None else draft.owner_takeover_minutes
        ),
        max_replies_per_hour=(
            base["max_replies_per_hour"] if draft.max_replies_per_hour is None else draft.max_replies_per_hour
        ),
        active_hours=active_hours,
        instructions=draft.instructions or "",
        rules=rules,
    )
    e = body.event
    event = AccountEvent(
        provider="simulation",
        conversation_type=e.conversation_type,
        conversation_id=e.conversation_id or "conversation",
        thread_id=e.conversation_id or "conversation",
        message_id="simulated",
        sender_id=e.sender_id,
        sender_name="Sender",
        sender_email=e.sender_email,
        text=e.text,
        created_at=datetime.now(UTC),
        mentions_account=e.mentions_account,
        from_account=e.from_account,
        from_application=e.from_application,
    )
    named = rule_group_names(settings_)
    directory = None
    if body.state.sender_group_ids:
        group_ids = frozenset(group_id.lower() for group_id in body.state.sender_group_ids)
    elif named:
        directory = await sender_directory(db, event.sender_addresses)
        group_ids = directory.group_ids
    else:
        group_ids = frozenset()
    decision = evaluate(
        settings_,
        event,
        EventState(
            thread_active=body.state.thread_active,
            owner_active=body.state.owner_active,
            replies_last_hour=body.state.replies_last_hour,
            sender_group_ids=group_ids,
        ),
    )
    sender = directory.explain(named) if directory else None
    plan = listening_plan(settings_)
    return SimulateResponse(
        decision={**decision.as_dict(), **({"sender": sender} if sender else {})},
        sender=sender,
        listening={
            "chats": plan.chats,
            "channels": [{"id": c.id, "mentions_only": c.mentions_only} for c in plan.channels],
            "all_channels": plan.all_channels,
            "all_channels_mentions_only": plan.all_channels_mentions_only,
        },
    )


async def operated_by(db: AsyncSession, agent_id: uuid.UUID, user_id: uuid.UUID) -> bool:
    """Whether *agent_id* operates an agent account of *user_id*: the agent then configures it for them."""
    return (
        await db.execute(
            select(AgentAccount.id).where(AgentAccount.agent_id == agent_id, AgentAccount.user_id == user_id).limit(1)
        )
    ).first() is not None
