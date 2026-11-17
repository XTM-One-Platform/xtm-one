# SPDX-License-Identifier: Apache-2.0

"""Personal API keys: the owner's CRUD and the administrators' view.

Users create keys in their profile. The raw key is returned only once, at
creation; every read shows the prefix. A key carries its scopes (the surfaces
it may call, read-only or read-write, optionally a set of agents), an optional
expiry bounded by the administrator's maximum lifetime, and optional limits of
its own (requests per minute, a quota per day or month). What they mean on each
surface is ``app/services/api_key_access.py``, the one place a key is checked.

A key created with a name only keeps the access every key had before scopes
existed: every surface, read-write, every agent. Creating, changing, deleting
and revoking a key are audited. Administrators list every key and revoke one
(``admin_router``, mounted at ``/api/v1/admin/api-keys``).
"""

import logging
import uuid
from datetime import UTC, datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, require_admin
from app.api.v1.helpers import get_user_group_ids, parse_path_id, parse_uuid, user_visible_filter
from app.api.v1.schemas.column_limits import column_length_validator
from app.core.database import get_db
from app.core.security import bcrypt_hashpw_async
from app.models.agent import Agent
from app.models.api_key import UserApiKey
from app.models.user import User
from app.services import api_key_access as access
from app.services.audit import log_audit

logger = logging.getLogger(__name__)

router = APIRouter()
admin_router = APIRouter()

KEY_PREFIX = access.API_KEY_PREFIX

QuotaPeriod = Literal["daily", "monthly"]


# ── Schemas ─────────────────────────────────────────────────────────────────


class CreateApiKeyRequest(BaseModel):
    name: str
    #: The surfaces the key may call; omitted = every surface.
    surfaces: list[str] | None = None
    read_only: bool = False
    #: The agents the key may reach; omitted = no restriction.
    agent_ids: list[str] | None = None
    #: Omitted = the longest the maximum lifetime allows, or never without one.
    expires_at: datetime | None = None
    rate_limit_per_minute: int | None = Field(default=None, ge=1, le=access.MAX_RATE_LIMIT_PER_MINUTE)
    quota_limit: int | None = Field(default=None, ge=1, le=access.MAX_QUOTA_LIMIT)
    quota_period: QuotaPeriod | None = None

    _fit_columns = column_length_validator(UserApiKey, "name", stripped=True)


class UpdateApiKeyRequest(BaseModel):
    """Only the fields sent change; an explicit ``null`` clears one (every surface, no agent restriction, ...)."""

    name: str | None = None
    surfaces: list[str] | None = None
    read_only: bool | None = None
    agent_ids: list[str] | None = None
    expires_at: datetime | None = None
    rate_limit_per_minute: int | None = Field(default=None, ge=1, le=access.MAX_RATE_LIMIT_PER_MINUTE)
    quota_limit: int | None = Field(default=None, ge=1, le=access.MAX_QUOTA_LIMIT)
    quota_period: QuotaPeriod | None = None

    _fit_columns = column_length_validator(UserApiKey, "name", stripped=True)


class ApiKeyAgent(BaseModel):
    id: str
    #: ``None`` once the agent is gone: the restriction then reaches nothing.
    name: str | None = None


class ApiKeyResponse(BaseModel):
    id: str
    name: str
    key_prefix: str
    is_active: bool
    is_from_env: bool
    last_used_at: str | None
    request_count: int
    created_at: str
    #: The surfaces the key may call, every one this deployment serves when unrestricted.
    surfaces: list[str] = Field(default_factory=list)
    #: No surface restriction: every surface, including any added later.
    all_surfaces: bool = True
    read_only: bool = False
    agent_ids: list[str] | None = None
    agents: list[ApiKeyAgent] = Field(default_factory=list)
    expires_at: str | None = None
    expired: bool = False
    revoked_at: str | None = None
    rate_limit_per_minute: int | None = None
    quota_limit: int | None = None
    quota_period: str | None = None
    #: Requests counted against the key's quota in the current period.
    quota_used: int | None = None
    #: Every surface, read-write, every agent: what a key had before scopes existed.
    full_scope: bool = True
    #: Expires later than the maximum lifetime allows, or never while one is set.
    beyond_max_lifetime: bool = False


class ApiKeyCreatedResponse(ApiKeyResponse):
    """Returned only at creation time — includes the full key."""

    key: str


class ApiKeyPolicyResponse(BaseModel):
    surfaces: list[str]
    max_lifetime_days: int
    quota_periods: list[str]
    max_rate_limit_per_minute: int
    max_quota_limit: int
    max_agents: int


class AdminApiKeyResponse(ApiKeyResponse):
    owner_id: str
    owner_email: str
    owner_name: str | None = None
    owner_active: bool = True


class AdminApiKeySummary(BaseModel):
    """Totals over every key the filters match, not only the page served."""

    #: Keys that authenticate now: not revoked, not expired.
    usable: int = 0
    request_count: int = 0
    last_used_at: str | None = None
    last_used_key_name: str | None = None
    most_active_key_name: str | None = None
    most_active_request_count: int = 0


class AdminApiKeyPage(BaseModel):
    items: list[AdminApiKeyResponse]
    total: int
    summary: AdminApiKeySummary | None = None


# ── Helpers ─────────────────────────────────────────────────────────────────


def _generate_key() -> str:
    """Generate a random API key: fcp-<48 hex chars>."""
    return access.generate_api_key()


async def _hash_key(raw_key: str) -> str:
    return (await bcrypt_hashpw_async(raw_key.encode("utf-8"))).decode("utf-8")


def _aware(moment: datetime | None) -> datetime | None:
    if moment is None or moment.tzinfo is not None:
        return moment
    return moment.replace(tzinfo=UTC)


def _iso(moment: datetime | None) -> str | None:
    moment = _aware(moment)
    return moment.isoformat() if moment else None


def _unprocessable(detail: str) -> HTTPException:
    return HTTPException(status_code=422, detail=detail)


def _validated_surfaces(value: list[str] | None) -> list[str] | None:
    """The surfaces asked for, canonical and in order; ``None`` = every surface."""
    if value is None:
        return None
    available = access.available_surfaces()
    chosen: set[str] = set()
    for entry in value:
        surface = str(entry).strip().lower()
        if surface not in available:
            raise _unprocessable(f"Unknown surface '{entry}': use {', '.join(available)}.")
        chosen.add(surface)
    if not chosen:
        raise _unprocessable("Choose at least one surface, or leave surfaces out for every surface.")
    return [surface for surface in access.API_KEY_SURFACES if surface in chosen]


async def _validated_agent_ids(db: AsyncSession, user: User, value: list[str] | None) -> list[str] | None:
    """The agents asked for, each one the owner can see; ``None`` = no restriction."""
    if value is None:
        return None
    wanted: list[str] = []
    for entry in value:
        canonical = access.canonical_agent_id(entry)
        if canonical is None:
            raise _unprocessable(f"'{entry}' is not an agent id.")
        if canonical not in wanted:
            wanted.append(canonical)
    if not wanted:
        raise _unprocessable("Choose at least one agent, or leave agent_ids out for every agent.")
    if len(wanted) > access.MAX_AGENT_IDS:
        raise _unprocessable(f"A key may be restricted to at most {access.MAX_AGENT_IDS} agents.")
    group_ids = await get_user_group_ids(db, user.id)
    visible = {
        str(agent_id)
        for agent_id in (
            await db.execute(
                select(Agent.id).where(
                    Agent.id.in_([uuid.UUID(agent_id) for agent_id in wanted]),
                    user_visible_filter(Agent, user.id, group_ids, is_admin=user.is_admin),
                )
            )
        ).scalars()
    }
    missing = [agent_id for agent_id in wanted if agent_id not in visible]
    if missing:
        raise _unprocessable(f"Agent {missing[0]} not found.")
    return wanted


def _bounded_expiry(requested: datetime | None, now: datetime, *, default_to_latest: bool) -> datetime | None:
    """*requested* held to the maximum lifetime; a missing one is the longest allowed when *default_to_latest*."""
    latest = access.latest_allowed_expiry(now)
    days = access.max_lifetime_days()
    if requested is None:
        if default_to_latest:
            return latest
        if latest is not None:
            raise _unprocessable(f"Personal API keys may last at most {days} days: an expiry is required.")
        return None
    requested = _aware(requested)
    if requested <= now:
        raise _unprocessable("The expiry must be in the future.")
    if latest is not None and requested > latest:
        raise _unprocessable(
            f"Personal API keys may last at most {days} days: choose an expiry on or before "
            f"{latest.date().isoformat()}."
        )
    return requested


def _quota(limit: int | None, period: str | None) -> tuple[int | None, str | None]:
    if limit is None:
        return None, None
    return limit, period or "monthly"


def _scope_summary(row: UserApiKey) -> dict[str, Any]:
    """What the audit trail records of a key's scopes and limits."""
    return {
        "surfaces": row.surfaces,
        "read_only": bool(row.read_only),
        "agent_ids": row.agent_ids,
        "expires_at": _iso(row.expires_at),
        "rate_limit_per_minute": row.rate_limit_per_minute,
        "quota_limit": row.quota_limit,
        "quota_period": row.quota_period,
    }


async def _agent_names(db: AsyncSession, rows: list[UserApiKey]) -> dict[str, str]:
    ids = {
        canonical
        for row in rows
        for entry in (row.agent_ids or [])
        if (canonical := access.canonical_agent_id(entry))
    }
    if not ids:
        return {}
    result = await db.execute(select(Agent.id, Agent.name).where(Agent.id.in_([uuid.UUID(i) for i in ids])))
    return {str(agent_id): name for agent_id, name in result.all()}


async def _buffered_stats(rows: list[UserApiKey]) -> dict[str, tuple[int, str | None]]:
    """Usage not yet flushed to the rows: ``{key id: (requests, last used)}``, best effort."""
    if not rows:
        return {}
    try:
        from app.core.redis import redis_client

        redis = await redis_client()
        pipe = redis.pipeline()
        for row in rows:
            pipe.hgetall(f"{access.APIKEY_STATS_PREFIX}{row.id}")
        results = await pipe.execute()
    except Exception:
        logger.warning("Failed to read Redis API key stats buffer", exc_info=True)
        return {}
    buffered: dict[str, tuple[int, str | None]] = {}
    for row, raw in zip(rows, results, strict=False):
        if not raw:
            continue
        count = int(raw.get(b"count", raw.get("count", 0)))
        last_raw = raw.get(b"last_used", raw.get("last_used"))
        buffered[str(row.id)] = (count, last_raw.decode() if isinstance(last_raw, bytes) else last_raw)
    return buffered


def _usage(row: UserApiKey, stats: tuple[int, str | None] | None) -> tuple[int, str | None]:
    """A key's requests and last use, the row's plus what the Redis buffer has not flushed yet."""
    extra_count, redis_last_used = stats or (0, None)
    db_last = _iso(row.last_used_at)
    last_used = max(db_last, redis_last_used) if redis_last_used and db_last else redis_last_used or db_last
    return (row.request_count or 0) + extra_count, last_used


def _summary(rows: list[UserApiKey], stats: dict[str, tuple[int, str | None]], now: datetime) -> AdminApiKeySummary:
    summary = AdminApiKeySummary()
    for row in rows:
        requests, last_used = _usage(row, stats.get(str(row.id)))
        summary.request_count += requests
        if row.is_active and not access.is_expired(row.expires_at, now):
            summary.usable += 1
        if last_used and (summary.last_used_at is None or last_used > summary.last_used_at):
            summary.last_used_at, summary.last_used_key_name = last_used, row.name
        if requests > summary.most_active_request_count:
            summary.most_active_request_count, summary.most_active_key_name = requests, row.name
    return summary


def _response_fields(
    row: UserApiKey,
    *,
    now: datetime,
    stats: tuple[int, str | None] | None = None,
    quota_used: int | None = None,
    agent_names: dict[str, str] | None = None,
) -> dict[str, Any]:
    request_count, last_used = _usage(row, stats)
    names = agent_names or {}
    agent_ids = (
        None
        if row.agent_ids is None
        else [canonical for entry in row.agent_ids if (canonical := access.canonical_agent_id(entry))]
    )
    return {
        "id": str(row.id),
        "name": row.name,
        "key_prefix": row.key_prefix,
        "is_active": row.is_active,
        "is_from_env": row.is_from_env,
        "last_used_at": last_used,
        "request_count": request_count,
        "created_at": _iso(row.created_at) or "",
        "surfaces": list(row.surfaces) if row.surfaces is not None else list(access.available_surfaces()),
        "all_surfaces": row.surfaces is None,
        "read_only": bool(row.read_only),
        "agent_ids": agent_ids,
        "agents": [ApiKeyAgent(id=agent_id, name=names.get(agent_id)) for agent_id in agent_ids or []],
        "expires_at": _iso(row.expires_at),
        "expired": access.is_expired(row.expires_at, now),
        "revoked_at": _iso(row.revoked_at),
        "rate_limit_per_minute": row.rate_limit_per_minute,
        "quota_limit": row.quota_limit,
        "quota_period": row.quota_period,
        "quota_used": quota_used if row.quota_limit else None,
        "full_scope": access.has_full_scope(row),
        "beyond_max_lifetime": access.beyond_max_lifetime(row.expires_at, now),
    }


async def _responses(db: AsyncSession, rows: list[UserApiKey]) -> list[ApiKeyResponse]:
    now = datetime.now(UTC)
    stats = await _buffered_stats(rows)
    usage = await access.read_quota_usage(rows)
    names = await _agent_names(db, rows)
    return [
        ApiKeyResponse(
            **_response_fields(
                row, now=now, stats=stats.get(str(row.id)), quota_used=usage.get(str(row.id), 0), agent_names=names
            )
        )
        for row in rows
    ]


def _refuse_while_impersonating(user: User, verb: str) -> None:
    if getattr(user, "impersonated_by", None) is not None:
        raise HTTPException(status_code=403, detail=f"Cannot {verb} API key while impersonating")


async def _owned_key(db: AsyncSession, user: User, key_id: str) -> UserApiKey:
    uid = parse_path_id(key_id, not_found_detail="API key not found")
    key = (
        await db.execute(select(UserApiKey).where(UserApiKey.id == uid, UserApiKey.user_id == user.id))
    ).scalar_one_or_none()
    if not key:
        raise HTTPException(status_code=404, detail="API key not found")
    return key


# ── Owner endpoints ─────────────────────────────────────────────────────────


@router.get("/policy", response_model=ApiKeyPolicyResponse)
async def get_api_key_policy(_user: User = Depends(get_current_user)):
    """What a new key may be given on this deployment: the surfaces it serves and the bounds."""
    return ApiKeyPolicyResponse(
        surfaces=list(access.available_surfaces()),
        max_lifetime_days=access.max_lifetime_days(),
        quota_periods=list(access.QUOTA_PERIODS),
        max_rate_limit_per_minute=access.MAX_RATE_LIMIT_PER_MINUTE,
        max_quota_limit=access.MAX_QUOTA_LIMIT,
        max_agents=access.MAX_AGENT_IDS,
    )


@router.get("", response_model=list[ApiKeyResponse])
async def list_api_keys(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List all API keys for the current user.

    Merges Redis-buffered usage stats (not yet flushed to DB) so the
    profile page always shows near-real-time request counts.
    """
    rows = list(
        (
            await db.execute(
                select(UserApiKey).where(UserApiKey.user_id == user.id).order_by(UserApiKey.created_at.desc())
            )
        )
        .scalars()
        .all()
    )
    return await _responses(db, rows)


@router.post(
    "", response_model=ApiKeyCreatedResponse, status_code=status.HTTP_201_CREATED
)
async def create_api_key(
    body: CreateApiKeyRequest,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Create a new API key. The full key is returned only in this response."""
    _refuse_while_impersonating(user, "create")

    now = datetime.now(UTC)
    surfaces = _validated_surfaces(body.surfaces)
    agent_ids = await _validated_agent_ids(db, user, body.agent_ids)
    expires_at = _bounded_expiry(body.expires_at, now, default_to_latest=True)
    quota_limit, quota_period = _quota(body.quota_limit, body.quota_period)
    access.require_within_caller_key(
        user,
        surfaces=surfaces,
        read_only=body.read_only,
        agent_ids=agent_ids,
        expires_at=expires_at,
        rate_limit_per_minute=body.rate_limit_per_minute,
        quota_limit=quota_limit,
        quota_period=quota_period,
        creates=True,
    )

    raw_key = _generate_key()
    api_key = UserApiKey(
        user_id=user.id,
        name=body.name.strip(),
        key_prefix=access.display_prefix(raw_key),
        key_hash=await _hash_key(raw_key),
        surfaces=surfaces,
        read_only=body.read_only,
        agent_ids=agent_ids,
        expires_at=expires_at,
        rate_limit_per_minute=body.rate_limit_per_minute,
        quota_limit=quota_limit,
        quota_period=quota_period,
        # A notice the key is already inside was seen at creation.
        expiry_notified_days=access.due_expiry_notice(expires_at, now),
        created_at=now,
    )
    db.add(api_key)
    await db.flush()
    await log_audit(
        db,
        user=user,
        action="create",
        entity_type="api_key",
        entity_id=api_key.id,
        entity_name=api_key.name,
        summary=f"Created API key '{api_key.name}'",
        details=_scope_summary(api_key),
        request=request,
    )
    # Commit before answering (``get_db`` commits after the response): the
    # caller authenticates with this key on its next request.
    await db.commit()

    names = await _agent_names(db, [api_key])
    return ApiKeyCreatedResponse(
        **_response_fields(api_key, now=now, quota_used=0, agent_names=names),
        key=raw_key,
    )


@router.patch("/{key_id}", response_model=ApiKeyResponse)
async def update_api_key(
    key_id: str,
    body: UpdateApiKeyRequest,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Change a key's name, scopes, expiry or limits. Only the fields sent change."""
    _refuse_while_impersonating(user, "change")
    key = await _owned_key(db, user, key_id)
    if key.is_from_env:
        raise HTTPException(status_code=403, detail="Environment-provisioned API keys cannot be changed via the API.")
    if not key.is_active:
        raise HTTPException(status_code=409, detail="This API key was revoked: create a new one instead.")

    sent = body.model_fields_set
    now = datetime.now(UTC)
    before = {"name": key.name, **_scope_summary(key)}

    name = body.name.strip() if "name" in sent and body.name is not None else key.name
    if not name:
        raise _unprocessable("A key needs a name.")
    surfaces = _validated_surfaces(body.surfaces) if "surfaces" in sent else key.surfaces
    read_only = bool(body.read_only) if "read_only" in sent and body.read_only is not None else bool(key.read_only)
    agent_ids = await _validated_agent_ids(db, user, body.agent_ids) if "agent_ids" in sent else key.agent_ids
    expires_at = (
        _bounded_expiry(body.expires_at, now, default_to_latest=False) if "expires_at" in sent else key.expires_at
    )
    rate_limit = body.rate_limit_per_minute if "rate_limit_per_minute" in sent else key.rate_limit_per_minute
    if "quota_limit" in sent or "quota_period" in sent:
        quota_limit, quota_period = _quota(
            body.quota_limit if "quota_limit" in sent else key.quota_limit,
            body.quota_period if "quota_period" in sent else key.quota_period,
        )
    else:
        quota_limit, quota_period = key.quota_limit, key.quota_period
    access.require_within_caller_key(
        user,
        surfaces=surfaces,
        read_only=read_only,
        agent_ids=agent_ids,
        expires_at=expires_at,
        rate_limit_per_minute=rate_limit,
        quota_limit=quota_limit,
        quota_period=quota_period,
        creates=False,
    )

    if _aware(expires_at) != _aware(key.expires_at):
        key.expiry_notified_days = access.due_expiry_notice(expires_at, now)
    key.name = name
    key.surfaces = surfaces
    key.read_only = read_only
    key.agent_ids = agent_ids
    key.expires_at = expires_at
    key.rate_limit_per_minute = rate_limit
    key.quota_limit = quota_limit
    key.quota_period = quota_period

    after = {"name": key.name, **_scope_summary(key)}
    changes = {field: {"from": before[field], "to": after[field]} for field in after if before[field] != after[field]}
    if changes:
        await log_audit(
            db,
            user=user,
            action="update",
            entity_type="api_key",
            entity_id=key.id,
            entity_name=key.name,
            summary=f"Changed API key '{key.name}': {', '.join(sorted(changes))}",
            details={"changes": changes},
            request=request,
        )
    # Commit before answering: the narrower scope applies from the caller's next request.
    await db.commit()
    return (await _responses(db, [key]))[0]


@router.delete("/{key_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_api_key(
    key_id: str,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Delete an API key (must belong to the current user)."""
    _refuse_while_impersonating(user, "delete")
    key = await _owned_key(db, user, key_id)

    if key.is_from_env:
        raise HTTPException(status_code=403, detail="Environment-provisioned API keys cannot be deleted via the API.")

    await log_audit(
        db,
        user=user,
        action="delete",
        entity_type="api_key",
        entity_id=key.id,
        entity_name=key.name,
        summary=f"Deleted API key '{key.name}'",
        request=request,
    )
    await db.delete(key)
    # Commit before answering (``get_db`` commits after the response): a
    # revoked key must stop authenticating the moment the caller is told so.
    await db.commit()


# ── Administrators: every key, revocation ───────────────────────────────────

AdminKeyStatus = Literal["all", "active", "expired", "revoked"]
AdminKeyIssue = Literal["no_expiry", "full_scope", "beyond_max_lifetime"]


MAX_ADMIN_OWNER_FILTER = 100


def _admin_filters(
    status_filter: str, issue: str | None, search: str | None, owner_ids: list[uuid.UUID], now: datetime
) -> list[Any]:
    clauses: list[Any] = []
    if owner_ids:
        clauses.append(UserApiKey.user_id.in_(owner_ids))
    unexpired = or_(UserApiKey.expires_at.is_(None), UserApiKey.expires_at > now)
    if status_filter == "active":
        clauses += [UserApiKey.is_active.is_(True), unexpired]
    elif status_filter == "expired":
        clauses += [UserApiKey.is_active.is_(True), UserApiKey.expires_at <= now]
    elif status_filter == "revoked":
        clauses.append(UserApiKey.is_active.is_(False))
    if issue is not None:
        # The keys the Security Audit reports: a deactivated account's keys authenticate nothing.
        clauses.append(User.is_active.is_(True))
    if issue == "no_expiry":
        clauses.append(UserApiKey.expires_at.is_(None))
    elif issue == "full_scope":
        # Read on the rows below (``has_full_scope``): the JSON shapes vary.
        clauses += [UserApiKey.read_only.is_(False), UserApiKey.agent_ids.is_(None)]
    if search:
        pattern = f"%{search.strip().lower()}%"
        clauses.append(
            or_(
                func.lower(UserApiKey.name).like(pattern),
                func.lower(UserApiKey.key_prefix).like(pattern),
                func.lower(User.email).like(pattern),
                func.lower(func.coalesce(User.full_name, "")).like(pattern),
            )
        )
    return clauses


@admin_router.get("", response_model=AdminApiKeyPage)
async def admin_list_api_keys(
    status_filter: AdminKeyStatus = Query("all", alias="status"),
    issue: AdminKeyIssue | None = Query(None),
    search: str | None = Query(None, max_length=200),
    owner_id: list[str] | None = Query(None, description="Repeat for several owners."),
    with_summary: bool = Query(False, description="Add totals over every matching key, not only the page."),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    _admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Every personal API key of the platform with its owner, scopes, expiry and last use (admin)."""
    now = datetime.now(UTC)
    if owner_id and len(owner_id) > MAX_ADMIN_OWNER_FILTER:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"At most {MAX_ADMIN_OWNER_FILTER} owners can be filtered on at once.",
        )
    owners = [parse_uuid(entry, label="owner_id") for entry in owner_id or [] if entry]
    clauses = _admin_filters(status_filter, issue, search, owners, now)
    base = select(UserApiKey, User).join(User, User.id == UserApiKey.user_id).where(*clauses)
    summary: AdminApiKeySummary | None = None
    stats: dict[str, tuple[int, str | None]]
    if issue in ("full_scope", "beyond_max_lifetime") or with_summary:
        # Read whole: an issue is decided per row by the access module, a summary covers
        # every matching key (one row per key).
        pairs = [
            (row, key_owner)
            for row, key_owner in (await db.execute(base.order_by(UserApiKey.created_at.desc()))).all()
            if issue not in ("full_scope", "beyond_max_lifetime")
            or (
                access.has_full_scope(row)
                if issue == "full_scope"
                else access.beyond_max_lifetime(row.expires_at, now)
            )
        ]
        total = len(pairs)
        if with_summary:
            matching = [row for row, _owner in pairs]
            stats = await _buffered_stats(matching)
            summary = _summary(matching, stats, now)
        pairs = pairs[offset : offset + limit]
    else:
        total = int(
            (
                await db.execute(
                    select(func.count())
                    .select_from(UserApiKey)
                    .join(User, User.id == UserApiKey.user_id)
                    .where(*clauses)
                )
            ).scalar_one()
        )
        pairs = list(
            (await db.execute(base.order_by(UserApiKey.created_at.desc()).offset(offset).limit(limit))).all()
        )
    rows = [row for row, _owner in pairs]
    if summary is None:
        stats = await _buffered_stats(rows)
    usage = await access.read_quota_usage(rows)
    names = await _agent_names(db, rows)
    return AdminApiKeyPage(
        items=[
            AdminApiKeyResponse(
                **_response_fields(
                    row,
                    now=now,
                    stats=stats.get(str(row.id)),
                    quota_used=usage.get(str(row.id), 0),
                    agent_names=names,
                ),
                owner_id=str(owner.id),
                owner_email=owner.email,
                owner_name=owner.full_name,
                owner_active=bool(owner.is_active),
            )
            for row, owner in pairs
        ],
        total=total,
        summary=summary,
    )


@admin_router.post("/{key_id}/revoke", response_model=AdminApiKeyResponse)
async def admin_revoke_api_key(
    key_id: str,
    request: Request,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Revoke a key (admin): it stops authenticating at once and its owner is told."""
    uid = parse_path_id(key_id, not_found_detail="API key not found")
    # Locked: two revocations of one key audit it and tell its owner once, the second reading it revoked.
    found = (
        await db.execute(
            select(UserApiKey, User)
            .join(User, User.id == UserApiKey.user_id)
            .where(UserApiKey.id == uid)
            .with_for_update(of=UserApiKey)
            .execution_options(populate_existing=True)
        )
    ).first()
    if found is None:
        raise HTTPException(status_code=404, detail="API key not found")
    key, owner = found
    if key.is_from_env:
        raise HTTPException(
            status_code=403,
            detail="Environment-provisioned API keys are managed by ADMIN_TOKEN: unset it to remove this key.",
        )
    now = datetime.now(UTC)
    if key.is_active:
        key.is_active = False
        key.revoked_at = now
        await log_audit(
            db,
            user=admin,
            action="revoke",
            entity_type="api_key",
            entity_id=key.id,
            entity_name=key.name,
            summary=f"Revoked API key '{key.name}' of {owner.email}",
            details={"owner_id": str(owner.id), "owner_email": owner.email},
            request=request,
        )
        if owner.id != admin.id:
            from app.services.api_key_notices import notify_key_revoked

            await notify_key_revoked(db, key)
    # Commit before answering: a revoked key must stop authenticating the moment the admin is told so.
    await db.commit()
    names = await _agent_names(db, [key])
    return AdminApiKeyResponse(
        **_response_fields(key, now=now, agent_names=names),
        owner_id=str(owner.id),
        owner_email=owner.email,
        owner_name=owner.full_name,
        owner_active=bool(owner.is_active),
    )
