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

"""Agent accounts: an agent operating a real user account of a messaging platform.

Owner-only: an account acts with its owner's integration token and agent, so
every route serves the caller's own accounts, and an administrator every
account (``?scope=all`` on the lists). The bindings an account names are
checked against its OWNER: the integration must be one they can see and whose
OAuth token they may use, the agent one they can see. Those rules, and every
write of an account, are ``services/agent_accounts/management.py``'s, which
the agent tools share; a refusal there is answered here with its status.

``notification_router`` is the providers' push endpoint: public, an entry is
trusted only when it names a subscription recorded for an account and echoes
that subscription's random ``clientState`` (``listening.accept_notifications``).
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.responses import PlainTextResponse
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.api.v1.helpers import (
    ENTITY_LIST_SCOPE_DESCRIPTION,
    EntityListScope,
    list_scope_admin_bypass,
    parse_path_id,
)
from app.api.v1.schemas.agent_account import (
    AgentAccountActivity,
    AgentAccountCatalog,
    AgentAccountCreate,
    AgentAccountDecisionItem,
    AgentAccountDecisionPage,
    AgentAccountReplyPage,
    AgentAccountReplyResponse,
    AgentAccountResponse,
    AgentAccountUpdate,
    ApproveReplyRequest,
    PickerConversation,
    PickerIdentity,
    PickerPerson,
    ProviderCatalogItem,
    SimulateRequest,
    SimulateResponse,
)
from app.core.database import get_db
from app.core.http_body import read_bounded_body
from app.models.agent_account import AgentAccount, AgentAccountReply
from app.models.annotation import Annotation
from app.models.user import User
from app.services import inbound_claims
from app.services.agent_accounts import activity, management, state, triage
from app.services.agent_accounts.events import AccountIdentity
from app.services.agent_accounts.listening import (
    accept_notifications,
    handle_notification,
    notification_url,
    operator_of,
    push_reachable,
)
from app.services.agent_accounts.pipeline import (
    AccountUnavailableError,
    open_integration_session,
    send_pending,
)
from app.services.agent_accounts.providers import (
    AccountProvider,
    ProviderError,
    ProviderSession,
    get_provider,
    providers,
)
from app.services.agent_accounts.rules import DEFAULT_LABEL, MODES, preset
from app.services.audit import log_audit
from app.services.email_signatures import signature_providers

logger = logging.getLogger(__name__)

router = APIRouter()
notification_router = APIRouter()

_NOT_FOUND = management.NOT_FOUND
_REPLY_NOT_FOUND = "Reply not found"
#: A push delivery larger than this is refused unread.
NOTIFICATION_MAX_BODY_BYTES = 4 * 1024 * 1024


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


@contextmanager
def _answered() -> Iterator[None]:
    """A refusal of the account's rules, answered with its status."""
    try:
        yield
    except management.AccountRefusedError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from None


async def _load_account(db: AsyncSession, account_id: str, user: User) -> AgentAccount:
    """The account, for its owner or an administrator; anyone else gets the unknown id's 404."""
    parsed = parse_path_id(account_id, not_found_detail=_NOT_FOUND)
    with _answered():
        return await management.load_account(db, parsed, user, is_admin=bool(user.is_admin))


# ---------------------------------------------------------------------------
# Catalog, simulator, pickers (literal paths first)
# ---------------------------------------------------------------------------


@router.get("/catalog", response_model=AgentAccountCatalog)
async def agent_account_catalog(_user: User = Depends(get_current_user)) -> AgentAccountCatalog:
    """The providers an account can operate, and the two modes' starting points on each."""
    return AgentAccountCatalog(
        providers=[
            ProviderCatalogItem(
                key=p.key,
                label=p.label,
                integration_provider=p.integration_provider,
                kind=p.kind,
                capabilities=dict(vars(p.capabilities)),
                push_reachable=push_reachable(),
                presets={mode: preset(mode, p.kind) for mode in MODES},
            )
            for p in providers().values()
        ],
        presets={mode: preset(mode) for mode in MODES},
        default_labels=dict(DEFAULT_LABEL),
        mailbox_defaults=dict(triage.DEFAULT_MAILBOX_ACTIONS),
        signature_providers=sorted(signature_providers()),
    )


@router.post("/simulate", response_model=SimulateResponse)
async def simulate_agent_account(
    body: SimulateRequest,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> SimulateResponse:
    """What the rule engine decides on a sample message: nothing is sent, nothing written.

    The sender's groups are the ones given, or those of the XTM One user whose
    email the sample message carries - read as the pipeline reads them.
    """
    with _answered():
        return await management.simulate(db, body)


async def _picker_session(
    db: AsyncSession, user: User, provider_key: str, integration_id: str
) -> tuple[AccountProvider, ProviderSession, AccountIdentity]:
    with _answered():
        provider = management.provider_for(provider_key)
        integration = await management.check_integration(db, user, provider, integration_id)
    try:
        signed_in = await open_integration_session(
            db, integration_id=integration.id, owner_id=user.id, provider=provider
        )
        identity = await provider.identify(signed_in)
    except (AccountUnavailableError, ProviderError) as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from None
    return provider, signed_in.as_account(identity.external_id), identity


@router.get("/pickers/{provider_key}/identity", response_model=PickerIdentity)
async def pick_identity(
    provider_key: str,
    integration_id: str = Query(...),
    account_id: str | None = Query(None, description="The account being edited, left out of the check."),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> PickerIdentity:
    """The account an integration is signed in as, and whether an agent account already operates it."""
    provider, _acting, identity = await _picker_session(db, user, provider_key, integration_id)
    editing = await _load_account(db, account_id, user) if account_id else None
    operator = await operator_of(
        db,
        provider.key,
        identity.external_id,
        excluding=editing,
        integration_id=parse_path_id(integration_id, not_found_detail="Integration not found"),
    )
    own = operator is not None and operator.user_id == user.id
    return PickerIdentity(
        external_id=identity.external_id,
        display_name=identity.display_name,
        email=identity.email,
        operated_by_account_id=str(operator.account_id) if own and operator else None,
        operated_by_account_name=operator.name if own and operator else None,
        operated_by_other_user=operator is not None and not own,
    )


@router.get("/pickers/{provider_key}/conversations", response_model=list[PickerConversation])
async def pick_conversations(
    provider_key: str,
    integration_id: str = Query(...),
    q: str | None = Query(None, max_length=200),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[PickerConversation]:
    """The chats and channels of the account an integration is signed in as."""
    provider, acting, _identity = await _picker_session(db, user, provider_key, integration_id)
    try:
        options = await provider.list_conversations(acting, q)
    except ProviderError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from None
    return [PickerConversation(id=o.id, name=o.name, type=o.type, team_id=o.team_id) for o in options]


@router.get("/pickers/{provider_key}/people", response_model=list[PickerPerson])
async def pick_people(
    provider_key: str,
    integration_id: str = Query(...),
    q: str = Query(..., min_length=2, max_length=100),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[PickerPerson]:
    """People of the account's directory, for a rule's focus or exclusion list."""
    provider, acting, _identity = await _picker_session(db, user, provider_key, integration_id)
    try:
        people = await provider.search_people(acting, q)
    except ProviderError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from None
    return [PickerPerson(id=p.id, name=p.name, email=p.email) for p in people]


# ---------------------------------------------------------------------------
# Replies: the approval queue
# ---------------------------------------------------------------------------


async def _reply_ratings(db: AsyncSession, reply_ids: list[uuid.UUID]) -> dict[uuid.UUID, dict[str, int]]:
    """``{reply_id: {rating: count}}``: every rating of the answers, reactions included."""
    if not reply_ids:
        return {}
    rows = await db.execute(
        select(Annotation.target_id, Annotation.rating, func.count())
        .where(Annotation.target_type == "agent_account_reply", Annotation.target_id.in_(reply_ids))
        .group_by(Annotation.target_id, Annotation.rating)
    )
    out: dict[uuid.UUID, dict[str, int]] = {}
    for target_id, rating, count in rows.all():
        out.setdefault(target_id, {})[rating] = int(count)
    return out


def _serialize_reply(
    reply: AgentAccountReply,
    account_name: str | None,
    ratings: dict[str, int] | None = None,
    *,
    provider: str | None = None,
) -> AgentAccountReplyResponse:
    ratings = ratings or {}
    return AgentAccountReplyResponse(
        id=str(reply.id),
        account_id=str(reply.account_id),
        account_name=account_name,
        provider=provider,
        status=reply.status,
        conversation_type=reply.conversation_type,
        conversation_name=reply.conversation_name,
        sender_name=reply.sender_name,
        sender_email=reply.sender_email,
        inbound_text=reply.inbound_text,
        draft_text=reply.draft_text,
        sent_text=reply.sent_text,
        rule_name=reply.rule_name,
        ai_label=reply.ai_label,
        error=reply.error,
        decided_at=_iso(reply.decided_at),
        created_at=_iso(reply.created_at) or "",
        kind=reply.kind or "reply",
        origin=reply.origin if reply.kind == "proactive" else None,
        rating_positive=ratings.get("positive", 0),
        rating_negative=ratings.get("negative", 0),
    )


@router.get("/replies", response_model=list[AgentAccountReplyResponse])
async def list_agent_account_replies(
    status_filter: str | None = Query(None, alias="status", max_length=20),
    account_id: str | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    scope: EntityListScope = Query("visible", description=ENTITY_LIST_SCOPE_DESCRIPTION),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[AgentAccountReplyResponse]:
    """The replies of the caller's accounts, newest first."""
    query = (
        select(AgentAccountReply, AgentAccount.name, AgentAccount.provider)
        .join(AgentAccount, AgentAccount.id == AgentAccountReply.account_id)
        .order_by(AgentAccountReply.created_at.desc())
        .limit(limit)
    )
    if not list_scope_admin_bypass(scope, user):
        query = query.where(AgentAccount.user_id == user.id)
    if status_filter:
        query = query.where(AgentAccountReply.status == status_filter)
    if account_id:
        query = query.where(
            AgentAccountReply.account_id == parse_path_id(account_id, not_found_detail=_NOT_FOUND)
        )
    rows = (await db.execute(query)).all()
    ratings = await _reply_ratings(db, [reply.id for reply, _name, _provider in rows])
    return [
        _serialize_reply(reply, name, ratings.get(reply.id), provider=provider) for reply, name, provider in rows
    ]


async def _load_reply(db: AsyncSession, reply_id: str, user: User) -> tuple[AgentAccountReply, AgentAccount]:
    parsed = parse_path_id(reply_id, not_found_detail=_REPLY_NOT_FOUND)
    row = (
        await db.execute(
            select(AgentAccountReply, AgentAccount)
            .join(AgentAccount, AgentAccount.id == AgentAccountReply.account_id)
            .where(AgentAccountReply.id == parsed)
            .with_for_update(of=AgentAccountReply)
        )
    ).first()
    if row is None or (row[1].user_id != user.id and not user.is_admin):
        raise HTTPException(status_code=404, detail=_REPLY_NOT_FOUND)
    return row[0], row[1]


@router.post("/replies/{reply_id}/approve", response_model=AgentAccountReplyResponse)
async def approve_agent_account_reply(
    reply_id: str,
    body: ApproveReplyRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> AgentAccountReplyResponse:
    """Post a pending (or shadow) reply as the account, as drafted or as edited."""
    reply, account = await _load_reply(db, reply_id, user)
    if reply.status not in ("pending", "shadow"):
        raise HTTPException(status_code=409, detail=f"This reply is {reply.status}: nothing to approve.")
    try:
        await send_pending(db, account, reply, text=body.text, decided_by=user.id)
    except (AccountUnavailableError, ProviderError) as exc:
        await db.rollback()
        raise HTTPException(status_code=502, detail=str(exc)) from None
    except ValueError as exc:
        await db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from None
    await log_audit(
        db,
        user=user,
        action="approve",
        entity_type="agent_account",
        entity_id=account.id,
        entity_name=account.name,
        summary=f"Sent a reply of agent account '{account.name}'",
        details={"reply_id": str(reply.id), "edited": body.text is not None},
        request=request,
    )
    await db.commit()
    return _serialize_reply(reply, account.name, provider=account.provider)


@router.post("/replies/{reply_id}/reject", response_model=AgentAccountReplyResponse)
async def reject_agent_account_reply(
    reply_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> AgentAccountReplyResponse:
    """Drop a pending reply: nothing is posted."""
    reply, account = await _load_reply(db, reply_id, user)
    if reply.status != "pending":
        raise HTTPException(status_code=409, detail=f"This reply is {reply.status}: nothing to reject.")
    reply.status = "rejected"
    reply.decided_by_user_id = user.id
    reply.decided_at = datetime.now(UTC)
    await log_audit(
        db,
        user=user,
        action="reject",
        entity_type="agent_account",
        entity_id=account.id,
        entity_name=account.name,
        summary=f"Rejected a reply of agent account '{account.name}'",
        details={"reply_id": str(reply.id)},
        request=request,
    )
    await db.commit()
    return _serialize_reply(reply, account.name, provider=account.provider)


# ---------------------------------------------------------------------------
# Accounts
# ---------------------------------------------------------------------------


@router.get("", response_model=list[AgentAccountResponse])
async def list_agent_accounts(
    scope: EntityListScope = Query("visible", description=ENTITY_LIST_SCOPE_DESCRIPTION),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[AgentAccountResponse]:
    """The caller's accounts (every account for an administrator with ``scope=all``)."""
    query = select(AgentAccount).order_by(AgentAccount.created_at.desc())
    if not list_scope_admin_bypass(scope, user):
        query = query.where(AgentAccount.user_id == user.id)
    accounts = list((await db.execute(query)).scalars().all())
    return await management.serialize(db, accounts, user)


@router.post("", response_model=AgentAccountResponse, status_code=status.HTTP_201_CREATED)
async def create_agent_account(
    body: AgentAccountCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> AgentAccountResponse:
    """Create an account for the caller, starting from its mode's preset."""
    with _answered():
        account = await management.create_account(db, user, body, request=request)
    return (await management.serialize(db, [account], user))[0]


@router.get("/{account_id}", response_model=AgentAccountResponse)
async def get_agent_account(
    account_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> AgentAccountResponse:
    account = await _load_account(db, account_id, user)
    return (await management.serialize(db, [account], user))[0]


@router.put("/{account_id}", response_model=AgentAccountResponse)
async def update_agent_account(
    account_id: str,
    body: AgentAccountUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> AgentAccountResponse:
    """Edit an account; a changed integration or agent is checked against the account's owner."""
    account = await _load_account(db, account_id, user)
    with _answered():
        await management.update_account(db, account, user, body, request=request)
    return (await management.serialize(db, [account], user))[0]


@router.post("/{account_id}/sync", response_model=AgentAccountResponse)
async def sync_agent_account(
    account_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> AgentAccountResponse:
    """Re-read the account's identity and bring its subscriptions up to date, in the background."""
    account = await _load_account(db, account_id, user)
    await management.resync_account(db, account)
    return (await management.serialize(db, [account], user))[0]


def _decision_item(entry: dict[str, Any]) -> AgentAccountDecisionItem:
    d = entry["decision"]
    holder_kind, holder_account_id = inbound_claims.holder_parts(getattr(d, "holder", None))
    return AgentAccountDecisionItem(
        id=str(d.id),
        at=_iso(d.created_at) or "",
        updated_at=_iso(d.updated_at) or "",
        message_at=_iso(d.message_at),
        outcome=entry["outcome"],
        group=entry["group"],
        reason=d.reason,
        respond=d.respond,
        waited=d.waited,
        conversation_type=d.conversation_type,
        conversation_id=d.conversation_id,
        conversation_name=d.conversation_name,
        sender_id=d.sender_id,
        sender_name=d.sender_name,
        preview=d.preview,
        mentions_account=d.mentions_account,
        addressed=d.addressed,
        thread_continuation=d.thread_continuation,
        rule_id=d.rule_id,
        rule_name=d.rule_name,
        sender=d.sender_match,
        note=d.note,
        holder_kind=holder_kind,
        holder_account_id=holder_account_id,
        trace_id=getattr(d, "trace_id", None),
        reply_id=str(d.reply_id) if d.reply_id else None,
        reply_status=entry["reply_status"],
        reply_text=entry["reply_text"],
        reply_error=entry["reply_error"],
        kind=entry.get("kind") or "reply",
        origin=entry.get("origin"),
    )


@router.get("/{account_id}/decisions", response_model=AgentAccountDecisionPage)
async def list_agent_account_decisions(
    account_id: str,
    group: list[str] | None = Query(None, max_length=30, description="Repeat for several outcome groups (any of them)"),
    conversation_type: list[str] | None = Query(
        None, max_length=20, description="Repeat for several kinds of conversation (any of them)"
    ),
    conversation_id: str | None = Query(None, max_length=500),
    sender: str | None = Query(None, max_length=255),
    q: str | None = Query(None, max_length=200),
    kind: str | None = Query(None, pattern="^(reply|proactive)$"),
    sort: Literal["date", "conversation", "sender"] = Query(
        "date", description="Column to order by: 'date' (default), 'conversation' or 'sender' (name)"
    ),
    direction: Literal["asc", "desc"] = Query("desc", description="Order direction; newest first breaks ties"),
    limit: int = Query(25, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> AgentAccountDecisionPage:
    """Every message the account heard and every message the agent sent on its own through it.

    Newest first unless ``sort`` says otherwise, filtered and paged; ``kind`` keeps one of the two.
    """
    account = await _load_account(db, account_id, user)
    filters = activity.DecisionFilters(
        groups=tuple(group or ()),
        conversation_types=tuple(conversation_type or ()),
        conversation_id=conversation_id,
        sender=sender,
        text=q,
        kind=kind,
    )
    items, total = await activity.list_decisions(
        db, account.id, filters, limit=limit, offset=offset, sort=sort, direction=direction
    )
    return AgentAccountDecisionPage(items=[_decision_item(entry) for entry in items], total=total)


@router.get("/{account_id}/replies", response_model=AgentAccountReplyPage)
async def list_account_replies(
    account_id: str,
    status_filter: list[str] | None = Query(
        None, alias="status", max_length=10, description="Repeat for several statuses (any of them)"
    ),
    kind: str | None = Query(None, pattern="^(reply|proactive)$"),
    q: str | None = Query(None, max_length=200),
    sort: Literal["date", "conversation"] = Query(
        "date", description="Column to order by: 'date' (default) or 'conversation' (name)"
    ),
    direction: Literal["asc", "desc"] = Query("desc", description="Order direction; newest first breaks ties"),
    limit: int = Query(25, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> AgentAccountReplyPage:
    """Every message the account sent, newest first unless ``sort`` says otherwise, filtered and paged.

    The replies to a message it heard and the messages the agent sent through it
    on its own (``kind``: ``reply`` or ``proactive``); ``status`` repeated keeps any of them.
    """
    account = await _load_account(db, account_id, user)
    query = select(AgentAccountReply).where(AgentAccountReply.account_id == account.id)
    statuses = [value for value in status_filter or () if value]
    if statuses:
        query = query.where(AgentAccountReply.status.in_(statuses))
    if kind == "proactive":
        query = query.where(AgentAccountReply.kind == "proactive")
    elif kind == "reply":
        query = query.where(AgentAccountReply.kind.is_distinct_from("proactive"))
    words = (q or "").strip()
    if words:
        query = query.where(
            or_(
                AgentAccountReply.inbound_text.icontains(words, autoescape=True),
                AgentAccountReply.sent_text.icontains(words, autoescape=True),
                AgentAccountReply.draft_text.icontains(words, autoescape=True),
                AgentAccountReply.sender_name.icontains(words, autoescape=True),
                AgentAccountReply.conversation_name.icontains(words, autoescape=True),
            )
        )
    total = (await db.execute(select(func.count()).select_from(query.subquery()))).scalar_one()
    order = [AgentAccountReply.created_at.desc(), AgentAccountReply.id.desc()]
    if sort == "conversation":
        name = func.lower(AgentAccountReply.conversation_name)
        order.insert(0, (name.asc() if direction == "asc" else name.desc()).nulls_last())
    elif direction == "asc":
        order = [AgentAccountReply.created_at.asc(), AgentAccountReply.id.asc()]
    replies = (
        await db.execute(query.order_by(*order).limit(limit).offset(offset))
    ).scalars().all()
    ratings = await _reply_ratings(db, [reply.id for reply in replies])
    return AgentAccountReplyPage(
        items=[
            _serialize_reply(reply, account.name, ratings.get(reply.id), provider=account.provider) for reply in replies
        ],
        total=int(total),
    )


@router.get("/{account_id}/activity", response_model=AgentAccountActivity)
async def agent_account_activity(
    account_id: str,
    period: Literal["24h", "7d", "30d"] = Query("7d"),
    tz: str | None = Query(None, max_length=64),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> AgentAccountActivity:
    """What the account did over the period: totals, timeline, where, for whom, why, how fast."""
    account = await _load_account(db, account_id, user)
    return AgentAccountActivity(**await activity.activity_stats(db, account.id, period=period, tz=tz))


@router.delete("/{account_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_agent_account(
    account_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Response:
    """Delete an account, its subscriptions at the provider first."""
    account = await _load_account(db, account_id, user)
    await management.delete_account(db, account, user, request=request)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------------------
# Push notifications (public, authenticated by clientState)
# ---------------------------------------------------------------------------


@notification_router.post("/notifications/{provider_key}")
async def agent_account_notifications(provider_key: str, request: Request) -> Response:
    """A provider's push delivery: validated, verified entry by entry, processed in the background.

    Every entry that names no recorded subscription or does not echo its
    ``clientState`` is dropped, and the answer is the same whatever was wrong,
    so a sender learns nothing about which subscriptions exist.
    """
    from app.channels.base import spawn_channel_task

    provider = get_provider(provider_key)
    if provider is None:
        raise HTTPException(status_code=404, detail="Not found")
    challenge = provider.handshake(dict(request.query_params))
    if challenge is not None:
        # What tells a refused subscription from one the provider could not validate.
        await state.note_handshake(provider.key)
        return PlainTextResponse(content=challenge, media_type="text/plain")
    body = await read_bounded_body(request, NOTIFICATION_MAX_BODY_BYTES)
    if body is None:
        raise HTTPException(status_code=413, detail="Payload too large")
    try:
        entries = await provider.parse_delivery(
            body, {k.lower(): v for k, v in request.headers.items()}, notification_url(provider.key)
        )
    except (ValueError, RecursionError):
        raise HTTPException(status_code=400, detail="Invalid notification payload") from None
    for account_id, entry in await accept_notifications(provider, entries):
        spawn_channel_task(
            handle_notification(account_id, provider.key, entry), name="agent-account-notification"
        )
    return Response(status_code=status.HTTP_202_ACCEPTED)
