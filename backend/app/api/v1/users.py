# SPDX-License-Identifier: Apache-2.0

"""Users admin endpoints – list users, manage roles, view user resources."""

import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

logger = logging.getLogger(__name__)

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, EmailStr
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import (
    AfterCommit,
    get_current_user,
    require_admin,
    run_after_commit,
    validate_password,
)
from app.api.v1.helpers import (
    parse_path_id,
    prune_user_grants,
    reassign_company_managed_entities,
)
from app.api.v1.integration_catalog import PROVIDER_CATALOG
from app.api.v1.schemas.column_limits import column_length_validator
from app.core.config import settings
from app.core.database import get_db
from app.core.security import create_access_token, hash_password_async
from app.core.utils import safe_uuid
from app.integrations.granted_scopes import missing_scopes
from app.models.agent import Agent
from app.models.api_key import UserApiKey
from app.models.assignment import Assignment, AssignmentRun
from app.models.audit_log import AuditLog
from app.models.chat import Conversation, Message
from app.models.custom_tool import CustomTool
from app.models.group import Group, GroupMember
from app.models.integration import Integration
from app.models.knowledge_base import KnowledgeBase
from app.models.mcp_server import MCPServer
from app.models.user import User, admin_email_change_refusal, is_platform_admin
from app.services import token_store
from app.services.assignment_pause import owner_not_deactivated
from app.services.audit import log_audit

router = APIRouter()


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class CreateUserRequest(BaseModel):
    email: EmailStr
    full_name: str
    password: str | None = (
        None  # Required for local accounts, omit for SSO pre-provisioning
    )
    role: str = "user"  # "admin" | "user"
    account_type: str = "local"  # "local" | "sso"

    _fit_columns = column_length_validator(User, "full_name")


class CreateUserResponse(BaseModel):
    id: str
    email: str
    full_name: str
    role: str
    sso_provider: str | None = None


class UserListItem(BaseModel):
    id: str
    email: str
    full_name: str
    title: str | None = None
    role: str
    is_active: bool
    is_platform_admin: bool = False
    sso_provider: str | None = None
    provisioning_source: str = "local"
    deprovisioned_at: str | None = None
    created_at: str
    integration_count: int = 0
    mcp_server_count: int = 0
    agent_count: int = 0
    knowledge_base_count: int = 0
    group_ids: list[str] = []

    model_config = {"from_attributes": True}


class UserDetailResponse(BaseModel):
    id: str
    email: str
    full_name: str
    title: str | None = None
    role: str
    is_active: bool
    is_platform_admin: bool = False
    sso_provider: str | None = None
    # True when the account has a local bcrypt password (set by registration,
    # admin set-password, or password reset). For hybrid accounts (SSO link +
    # local password) the admin Password card stays available so admins can
    # set/reset passwords; only the SSO-only case (``sso_provider`` set AND
    # ``hashed_password`` NULL) hides the card. The bcrypt hash itself is
    # never exposed — only this boolean is.
    has_local_password: bool = False
    default_agent_id: str | None = None
    created_at: str
    integration_count: int = 0
    mcp_server_count: int = 0
    agent_count: int = 0
    knowledge_base_count: int = 0
    # TOTP two-factor state. Status only — the secret and the recovery
    # codes are never exposed to anyone, admins included. Drives the admin
    # "Reset two-factor" action (``DELETE /users/{id}/mfa``) for a user who lost
    # their device.
    mfa_enabled: bool = False
    mfa_confirmed_at: str | None = None
    # Provisioning (#122): "scim" when an identity provider manages the
    # account, else "sso" or "local"; the deactivation state with its source.
    provisioning_source: str = "local"
    deactivated_at: str | None = None
    deactivation_source: str | None = None
    deprovisioned_at: str | None = None


def provisioning_source(user: User) -> str:
    """``scim`` for an account an identity provider manages, else ``sso`` or ``local``."""
    if user.scim_external_id:
        return "scim"
    return "sso" if user.sso_provider else "local"


class UserIntegrationItem(BaseModel):
    id: str
    provider: str
    auth_type: str
    status: str
    name: str
    is_company_managed: bool = False
    created_at: str
    # What the shown status is derived from, as on the integration detail
    # (#11837): a typed key nobody tested reads "Not tested", never
    # "Connected", on every page that lists it.
    connection_test_status: str | None = None
    has_access_token: bool = False
    scopes: str | None = None
    required_scopes: str | None = None
    missing_scopes: list[str] | None = None


class UserMCPServerItem(BaseModel):
    id: str
    name: str
    transport: str
    is_company_managed: bool = False
    enabled: bool = True
    created_at: str


class UserAgentItem(BaseModel):
    id: str
    name: str
    template_slug: str | None = None
    enabled: bool = True
    is_company_managed: bool = False
    created_at: str


class UserKnowledgeBaseItem(BaseModel):
    id: str
    name: str
    document_count: int = 0
    chunk_count: int = 0
    is_company_managed: bool = False
    is_system_managed: bool = False
    created_at: str


class UserCustomToolItem(BaseModel):
    id: str
    name: str
    enabled: bool = True
    is_company_managed: bool = False
    created_at: str


class UpdateRoleRequest(BaseModel):
    role: str  # "admin" | "user"


# ---------------------------------------------------------------------------
# Routes (all admin-only)
# ---------------------------------------------------------------------------


@router.post("", response_model=CreateUserResponse, status_code=status.HTTP_201_CREATED)
async def create_user(
    body: CreateUserRequest,
    request: Request,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Create a new user (admin-only).

    For local accounts, a password is required.
    For SSO accounts, no password is needed — the user is pre-provisioned so
    that when they first log in via SSO, their role and profile are already set.
    """
    if body.role not in ("admin", "user"):
        raise HTTPException(status_code=400, detail="Role must be 'admin' or 'user'")

    if body.account_type not in ("local", "sso"):
        raise HTTPException(
            status_code=400, detail="Account type must be 'local' or 'sso'"
        )

    if body.account_type == "local" and not body.password:
        raise HTTPException(
            status_code=400, detail="Password is required for local accounts"
        )

    if body.password:
        validate_password(body.password)

    # Normalise the email so the duplicate check below and the eventual
    # ``User.email`` value agree on the canonical form with every other
    # creation path (self-service ``auth.register``, invitation issuance
    # in ``invitations.create_invitation``, and consumption in
    # ``register_with_invitation``).  Postgres ``UNIQUE(text)`` is
    # case-sensitive, so creating ``Foo@Example.com`` while a row for
    # ``foo@example.com`` already exists would otherwise have slipped past
    # this check and leaked a 500 from the unique constraint at flush
    # time — there is no IntegrityError handler on this admin path.
    email = str(body.email).strip().lower()

    # Case-insensitive duplicate check (matches every other email-identity
    # path in the platform).
    exists = await db.execute(
        select(User).where(func.lower(User.email) == email)
    )
    if exists.scalar_one_or_none():
        raise HTTPException(
            status_code=409, detail="A user with this email already exists"
        )

    # Resolve the actual SSO provider from platform config so the label
    # matches what _get_or_create_sso_user will set on first login.
    sso_provider: str | None = None
    if body.account_type == "sso":
        from app.core.sso_helpers import get_sso_config

        cfg = await get_sso_config(db)
        if cfg.saml_enabled:
            sso_provider = "saml"
        elif cfg.oidc_enabled:
            sso_provider = "oidc"
        else:
            # Fallback: generic label (shouldn't happen — SSO is off)
            sso_provider = "sso"

    user = User(
        email=email,
        hashed_password=(
            await hash_password_async(body.password) if body.password else None
        ),
        full_name=body.full_name,
        role=body.role,
        sso_provider=sso_provider,
    )

    # Stay in lockstep with the self-service ``/register``, invitation
    # consumption, and SSO first-login paths so admin-created accounts
    # also land on the CTEM Assistant by default in XTM One mode.
    # Without this, the chat UI falls back to its "first company-managed
    # chat agent" heuristic — which picks whichever built-in agent has
    # the most recent ``created_at`` rather than the intended router.
    if settings.is_xtm_one:
        ctem_r = await db.execute(
            select(Agent.id).where(Agent.slug == "ctem-assistant").limit(1)
        )
        ctem_id = ctem_r.scalar_one_or_none()
        if ctem_id:
            user.default_agent_id = ctem_id
            user.default_agent_source = "platform"
            user.default_agent_set_at = datetime.now(UTC)

    db.add(user)
    await db.flush()
    # Every new account joins "CTEM Viewers" once, at its creation (#2441).
    from app.services.ctem_groups import join_viewers_at_creation

    await join_viewers_at_creation(db, user)

    await log_audit(
        db,
        user=admin,
        action="create",
        entity_type="user",
        entity_id=user.id,
        entity_name=user.full_name,
        summary=f"Created user '{user.email}' with role '{user.role}'",
        request=request,
    )
    # Commit before responding rather than on ``get_db`` teardown, which runs
    # after the response is sent: a caller signing in as this user on its next
    # request would otherwise race the transaction and be refused.
    await db.commit()

    return CreateUserResponse(
        id=str(user.id),
        email=user.email,
        full_name=user.full_name,
        role=user.role,
        sso_provider=user.sso_provider,
    )


class UserDirectoryItem(BaseModel):
    """The minimum needed to name a person in a share picker."""

    id: str
    full_name: str
    email: str
    # Only ever False in ``ids`` mode: search results are active-only, but a
    # grant that predates its grantee's deactivation still has to render as a
    # named person rather than a bare UUID.
    is_active: bool = True


#: Backslash is the escape character declared on the ``LIKE`` below. It has to
#: be doubled first, or escaping ``%`` would itself introduce an unescaped one.
_LIKE_ESCAPE = "\\"


def escape_like(value: str) -> str:
    """Neutralise ``LIKE`` metacharacters in a user-supplied search term.

    ``%`` and ``_`` are wildcards, so an unescaped term decides its own match
    breadth: ``%%`` matches every row, which would turn a deliberately narrow
    lookup into a bulk directory dump. Not an injection — values are always
    bound — but a silent breach of the caller's stated contract, which is worse
    for being invisible.

    Callers must pass ``escape=_LIKE_ESCAPE`` to ``.like()`` for this to take
    effect; escaping the term alone does nothing on its own.
    """
    return (
        value.replace(_LIKE_ESCAPE, _LIKE_ESCAPE * 2)
        .replace("%", f"{_LIKE_ESCAPE}%")
        .replace("_", f"{_LIKE_ESCAPE}_")
    )


# Declared BEFORE ``/{user_id}`` — FastAPI matches routes in declaration order,
# so a later registration would be shadowed by the id path and 422 on "directory".
@router.get("/directory", response_model=list[UserDirectoryItem])
async def search_user_directory(
    q: str | None = Query(None, min_length=2, max_length=100),
    ids: str | None = Query(None, max_length=4000),
    limit: int = Query(20, ge=1, le=50),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Search active users by name or email, for per-user share pickers.

    ``GET /users`` is admin-only and returns roles plus per-user resource
    counts, which is far more than a picker needs and more than a non-admin
    should see. Sharing an entity with a colleague is an owner-level action,
    though, so the owner has to be able to find that colleague — the same
    reasoning that already makes ``GET /groups`` readable by non-admins for
    ``GroupSharingCard``.

    The exposure is deliberately bounded:

    * name and email only — no role, no activity, no owned-resource counts;
    * a search term is **required** (min 2 chars), so this answers "is Alice on
      this platform" but does not hand out the staff directory in one call;
    * results are capped and cover active accounts only;
    * the caller is excluded — sharing with yourself grants nothing, and
      offering it in the picker just invites a confusing no-op.

    ``ids`` is the second, narrower mode: resolve specific user ids that are
    **already** stored as grants back into names, so the sharing card can render
    "Alice Martin (alice@…)" instead of a bare UUID on load. It adds no
    enumeration reach — the caller must already hold the ids — so unlike search
    it deliberately returns **deactivated** accounts too (flagged ``is_active``)
    and does not exclude the caller: a grant that outlives its grantee's
    deactivation must still be legible and removable, and one naming the entity
    owner can exist on an imported or cloned entity.
    """
    if (q is None) == (ids is None):
        raise HTTPException(
            status_code=422,
            detail="Provide exactly one of 'q' (search) or 'ids' (resolve).",
        )

    if ids is not None:
        wanted: list[uuid.UUID] = []
        for raw in ids.split(","):
            raw = raw.strip()
            if not raw:
                continue
            try:
                wanted.append(uuid.UUID(raw))
            except ValueError as exc:
                raise HTTPException(
                    status_code=422, detail=f"Invalid user id: {raw!r}"
                ) from exc
        if not wanted:
            return []
        result = await db.execute(
            select(User).where(User.id.in_(wanted[:100])).order_by(User.full_name)
        )
    else:
        term = f"%{escape_like(q.strip().lower())}%"
        result = await db.execute(
            select(User)
            .where(
                User.is_active.is_(True),
                User.id != user.id,
                or_(
                    func.lower(User.full_name).like(term, escape=_LIKE_ESCAPE),
                    func.lower(User.email).like(term, escape=_LIKE_ESCAPE),
                ),
            )
            .order_by(User.full_name)
            .limit(limit)
        )

    return [
        UserDirectoryItem(
            id=str(u.id),
            full_name=u.full_name or "",
            email=u.email,
            is_active=u.is_active,
        )
        for u in result.scalars().all()
    ]


#: The columns of the Users table the API sorts on (``sort=``); ``id`` breaks
#: every tie so offset pages never overlap (#9378 slice 6z, #11940).
USER_SORTS: dict[str, Any] = {
    "name": func.lower(User.full_name),
    "email": func.lower(User.email),
    "role": User.role,
    "status": User.is_active,
    "sign_in": func.coalesce(User.sso_provider, ""),
    "created": User.created_at,
}

#: The resource each count column of the table counts, by its ``sort=`` key.
_COUNTED: dict[str, Any] = {
    "integrations": Integration,
    "mcp_servers": MCPServer,
    "agents": Agent,
    "knowledge_bases": KnowledgeBase,
}

#: The "Created" filter of the table: a user falls in every window its creation is inside.
CREATED_WINDOWS: dict[str, timedelta] = {
    "24h": timedelta(days=1),
    "7d": timedelta(days=7),
    "30d": timedelta(days=30),
    "90d": timedelta(days=90),
}


def _owned_count(model: Any) -> Any:
    return select(func.count(model.id)).where(model.user_id == User.id).correlate(User).scalar_subquery()


def filter_values(raw: str | None) -> list[str]:
    """The values of a filter given once or comma-separated (``role=admin,user``)."""
    if not raw:
        return []
    return [value for value in (part.strip() for part in raw.split(",")) if value]


def user_order(sort: str | None, direction: str | None) -> list[Any]:
    """The ORDER BY of a page: by name then email when ``sort`` is unknown or
    absent (the order of the full list), else the column of ``sort``, ascending
    unless ``direction`` is ``desc``, then the id the same way."""
    column = USER_SORTS.get(sort or "")
    if column is None and sort in _COUNTED:
        column = _owned_count(_COUNTED[sort])
    if column is None:
        return [func.lower(User.full_name).asc(), User.email.asc(), User.id.asc()]
    if direction == "desc":
        return [column.desc(), User.id.desc()]
    return [column.asc(), User.id.asc()]


def user_filters(
    search: str | None,
    role: str | None,
    status_filter: str | None,
    group: str | None,
    created: str | None = None,
    now: datetime | None = None,
) -> list[Any]:
    """The WHERE clauses of the Users table: a search on name, email and title,
    roles, ``active`` / ``disabled``, group ids (``none``: in no group) and
    creation windows (``24h``, ``7d``, ``30d``, ``90d``: the widest one given)."""
    clauses: list[Any] = []
    windows = [CREATED_WINDOWS[value] for value in filter_values(created) if value in CREATED_WINDOWS]
    if windows:
        clauses.append(User.created_at >= (now or datetime.now(UTC)) - max(windows))
    term = (search or "").strip()
    if term:
        pattern = f"%{term}%"
        clauses.append(
            or_(
                User.full_name.ilike(pattern),
                User.email.ilike(pattern),
                User.title.ilike(pattern),
            )
        )
    roles = filter_values(role)
    if roles:
        clauses.append(User.role.in_(roles))
    statuses = set(filter_values(status_filter)) & {"active", "disabled"}
    if len(statuses) == 1:
        clauses.append(User.is_active.is_("active" in statuses))
    groups = filter_values(group)
    if groups:
        ids = [gid for gid in (safe_uuid(value) for value in groups) if gid]
        member_of = []
        if ids:
            member_of.append(
                User.id.in_(
                    select(GroupMember.user_id).where(GroupMember.group_id.in_(ids))
                )
            )
        if "none" in groups:
            member_of.append(~User.id.in_(select(GroupMember.user_id)))
        clauses.append(or_(*member_of) if member_of else User.id.is_(None))
    return clauses


@router.get("/count")
async def count_users(
    search: str | None = Query(None, description="Name, email or title contains"),
    role: str | None = Query(None, description="One role or several comma-separated"),
    status_filter: str | None = Query(
        None, alias="status", description="active or disabled"
    ),
    group: str | None = Query(
        None, description="Group ids comma-separated; none = in no group"
    ),
    created: str | None = Query(None, description="Created within: 24h, 7d, 30d or 90d"),
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """How many users match the filters of ``GET /users`` (admin-only): the table's total."""
    clauses = user_filters(search, role, status_filter, group, created)
    count = await db.scalar(select(func.count(User.id)).where(*clauses))
    return {"count": count or 0}


@router.get("", response_model=list[UserListItem])
async def list_users(
    search: str | None = Query(None, description="Name, email or title contains"),
    role: str | None = Query(None, description="One role or several comma-separated"),
    status_filter: str | None = Query(
        None, alias="status", description="active or disabled"
    ),
    group: str | None = Query(
        None, description="Group ids comma-separated; none = in no group"
    ),
    created: str | None = Query(None, description="Created within: 24h, 7d, 30d or 90d"),
    sort: str | None = Query(
        None,
        description="Column to sort on: name, email, role, status, sign_in, created, integrations, mcp_servers, agents, knowledge_bases",
    ),
    direction: str | None = Query(None, description="asc (default) or desc"),
    limit: int | None = Query(None, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """List users with resource counts (admin-only). Without a parameter every
    user, by name; the Users table passes its search, filters, sort and page."""
    clauses = user_filters(search, role, status_filter, group, created)
    # Subqueries for counts
    integ_count = (
        select(func.count(Integration.id))
        .where(Integration.user_id == User.id)
        .correlate(User)
        .scalar_subquery()
    )
    mcp_count = (
        select(func.count(MCPServer.id))
        .where(MCPServer.user_id == User.id)
        .correlate(User)
        .scalar_subquery()
    )
    agent_count = (
        select(func.count(Agent.id))
        .where(Agent.user_id == User.id)
        .correlate(User)
        .scalar_subquery()
    )
    kb_count = (
        select(func.count(KnowledgeBase.id))
        .where(KnowledgeBase.user_id == User.id)
        .correlate(User)
        .scalar_subquery()
    )

    result = await db.execute(
        select(
            User,
            integ_count.label("integ_count"),
            mcp_count.label("mcp_count"),
            agent_count.label("agent_count"),
            kb_count.label("kb_count"),
        )
        .where(*clauses)
        .order_by(*user_order(sort, direction))
        .limit(limit)
        .offset(offset)
    )
    rows = result.all()

    # Batch-load the group memberships of the users listed
    gm_query = select(GroupMember.user_id, GroupMember.group_id)
    if limit is not None or clauses:
        gm_query = gm_query.where(GroupMember.user_id.in_([u.id for u, *_ in rows]))
    gm_rows = await db.execute(gm_query)
    user_group_map: dict[str, list[str]] = {}
    for row in gm_rows.all():
        user_group_map.setdefault(str(row.user_id), []).append(str(row.group_id))

    return [
        UserListItem(
            id=str(u.id),
            email=u.email,
            full_name=u.full_name,
            title=u.title,
            role=u.role,
            is_active=u.is_active,
            is_platform_admin=is_platform_admin(u),
            sso_provider=u.sso_provider,
            provisioning_source=provisioning_source(u),
            deprovisioned_at=u.deprovisioned_at.isoformat() if u.deprovisioned_at else None,
            created_at=u.created_at.isoformat() if u.created_at else "",
            integration_count=ic or 0,
            mcp_server_count=mc or 0,
            agent_count=ac or 0,
            knowledge_base_count=kc or 0,
            group_ids=user_group_map.get(str(u.id), []),
        )
        for u, ic, mc, ac, kc in rows
    ]


# ---------------------------------------------------------------------------
# Deactivation review (#122): the cascade's episodes and what an administrator
# must look at. Declared before ``/{user_id}`` so the literal paths win.
# ---------------------------------------------------------------------------


class ReviewResolution(BaseModel):
    key: str
    resolution: str


def _episode_dict(episode: Any, user: User | None) -> dict[str, Any]:
    items = list(episode.review_items or [])
    return {
        "id": str(episode.id),
        "user_id": str(episode.user_id),
        "user_email": user.email if user is not None else None,
        "user_name": user.full_name if user is not None else None,
        "deprovisioned": bool(user is not None and user.deprovisioned_at is not None),
        "source": episode.source,
        "actor_label": episode.actor_label,
        "successor_user_id": str(episode.successor_user_id) if episode.successor_user_id else None,
        "counts": dict(episode.counts or {}),
        "review_items": items,
        "open_review_count": sum(1 for i in items if not i.get("reviewed_at")),
        "deactivated_at": episode.deactivated_at.isoformat() if episode.deactivated_at else None,
        "reactivated_at": episode.reactivated_at.isoformat() if episode.reactivated_at else None,
    }


@router.get("/deactivations")
async def list_deactivations(
    open_only: bool = Query(True, alias="open", description="Only the episodes of accounts still deactivated"),
    limit: int = Query(100, ge=1, le=500),
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> list[dict[str, Any]]:
    """The deactivation episodes, newest first, with their review list."""
    from app.models.user_deactivation import UserDeactivation

    query = select(UserDeactivation, User).join(User, User.id == UserDeactivation.user_id)
    if open_only:
        query = query.where(UserDeactivation.reactivated_at.is_(None))
    rows = (await db.execute(query.order_by(UserDeactivation.deactivated_at.desc()).limit(limit))).all()
    return [_episode_dict(episode, user) for episode, user in rows]


@router.post("/deactivations/{deactivation_id}/review")
async def resolve_deactivation_review_item(
    deactivation_id: str,
    body: ReviewResolution,
    request: Request,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Mark one review item done (``keep``, ``reassign`` or ``delete``)."""
    from app.models.user_deactivation import UserDeactivation
    from app.services import user_deactivation

    episode = await db.get(UserDeactivation, parse_path_id(deactivation_id, not_found_detail="Deactivation not found"))
    if episode is None:
        raise HTTPException(status_code=404, detail="Deactivation not found")
    try:
        item = await user_deactivation.resolve_review_item(
            db, episode, body.key, resolution=body.resolution, admin=admin
        )
    except user_deactivation.DeactivationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    user = await db.get(User, episode.user_id)
    await log_audit(
        db,
        user=admin,
        action="update",
        entity_type="user",
        entity_id=episode.user_id,
        entity_name=user.email if user is not None else None,
        summary=f"Reviewed '{item.get('name')}' ({item.get('kind')}) after the deactivation: {body.resolution}",
        details={"deactivation_id": str(episode.id), "key": body.key, "resolution": body.resolution},
        request=request,
    )
    await db.commit()
    return _episode_dict(episode, user)


@router.get("/{user_id}/deactivation-preview")
async def preview_user_deactivation(
    user_id: str,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """What deactivating the account would do, read without writing."""
    from app.services import user_deactivation

    user = await db.get(User, parse_path_id(user_id, not_found_detail="User not found"))
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    preview = await user_deactivation.preview_deactivation(db, user)
    if preview["refusal"] is None and user.id == admin.id:
        preview["refusal"] = "Cannot deactivate yourself"
    return preview


@router.get("/{user_id}", response_model=UserDetailResponse)
async def get_user(
    user_id: str,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Get detailed info for a specific user (admin-only)."""
    result = await db.execute(select(User).where(User.id == parse_path_id(user_id, not_found_detail="User not found")))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    # Count personal resources (single query each)
    integ_count = (
        await db.execute(
            select(func.count(Integration.id)).where(Integration.user_id == user.id)
        )
    ).scalar() or 0
    mcp_count = (
        await db.execute(
            select(func.count(MCPServer.id)).where(MCPServer.user_id == user.id)
        )
    ).scalar() or 0
    agent_count = (
        await db.execute(select(func.count(Agent.id)).where(Agent.user_id == user.id))
    ).scalar() or 0
    kb_count = (
        await db.execute(
            select(func.count(KnowledgeBase.id)).where(KnowledgeBase.user_id == user.id)
        )
    ).scalar() or 0

    return UserDetailResponse(
        id=str(user.id),
        email=user.email,
        full_name=user.full_name,
        title=user.title,
        role=user.role,
        is_active=user.is_active,
        is_platform_admin=is_platform_admin(user),
        sso_provider=user.sso_provider,
        has_local_password=bool(user.hashed_password),
        default_agent_id=str(user.default_agent_id) if user.default_agent_id else None,
        created_at=user.created_at.isoformat() if user.created_at else "",
        integration_count=integ_count,
        mcp_server_count=mcp_count,
        agent_count=agent_count,
        knowledge_base_count=kb_count,
        mfa_enabled=bool(getattr(user, "mfa_enabled", False)),
        mfa_confirmed_at=(
            user.mfa_confirmed_at.isoformat()
            if getattr(user, "mfa_confirmed_at", None)
            else None
        ),
        provisioning_source=provisioning_source(user),
        deactivated_at=user.deactivated_at.isoformat() if user.deactivated_at else None,
        deactivation_source=user.deactivation_source,
        deprovisioned_at=user.deprovisioned_at.isoformat() if user.deprovisioned_at else None,
    )


class UserStatsResponse(BaseModel):
    agent_count: int = 0
    active_assignment_count: int = 0
    conversation_count: int = 0
    message_count: int = 0
    assignment_run_count: int = 0
    notification_count: int = 0
    integration_count: int = 0
    connected_integration_count: int = 0
    login_count: int = 0
    last_login_at: str | None = None
    api_key_count: int = 0
    api_key_total_requests: int = 0


@router.get("/{user_id}/stats", response_model=UserStatsResponse)
async def get_user_stats(
    user_id: str,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Get aggregated statistics for a specific user (admin-only)."""
    uid = parse_path_id(user_id, not_found_detail="User not found")

    # Verify user exists
    user_result = await db.execute(select(User).where(User.id == uid))
    if not user_result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="User not found")

    # Single consolidated query for most counts
    row = (
        await db.execute(
            select(
                # Agents
                (
                    select(func.count(Agent.id))
                    .where(Agent.user_id == uid)
                    .correlate(None)
                    .scalar_subquery()
                ).label("agents"),
                # Active assignments: not those paused while the owner's
                # account is deactivated
                (
                    select(func.count(Assignment.id))
                    .where(
                        Assignment.user_id == uid,
                        Assignment.enabled.is_(True),
                        owner_not_deactivated(),
                    )
                    .correlate(None)
                    .scalar_subquery()
                ).label("active_wf"),
                # Conversations
                (
                    select(func.count(Conversation.id))
                    .where(Conversation.user_id == uid)
                    .correlate(None)
                    .scalar_subquery()
                ).label("conversations"),
                # Messages (via conversation join)
                (
                    select(func.count(Message.id))
                    .join(Conversation, Message.conversation_id == Conversation.id)
                    .where(Conversation.user_id == uid)
                    .correlate(None)
                    .scalar_subquery()
                ).label("messages"),
                # Assignment runs
                (
                    select(func.count(AssignmentRun.id))
                    .where(AssignmentRun.user_id == uid)
                    .correlate(None)
                    .scalar_subquery()
                ).label("runs"),
                # Notifications (runs with notification_sent = true)
                (
                    select(func.count(AssignmentRun.id))
                    .where(
                        AssignmentRun.user_id == uid,
                        AssignmentRun.notification_sent.is_(True),
                    )
                    .correlate(None)
                    .scalar_subquery()
                ).label("notifications"),
                # Integrations
                (
                    select(func.count(Integration.id))
                    .where(Integration.user_id == uid)
                    .correlate(None)
                    .scalar_subquery()
                ).label("integrations"),
                # Connected integrations
                (
                    select(func.count(Integration.id))
                    .where(
                        Integration.user_id == uid, Integration.status == "connected"
                    )
                    .correlate(None)
                    .scalar_subquery()
                ).label("connected_integrations"),
                # Login count (audit log)
                (
                    select(func.count(AuditLog.id))
                    .where(AuditLog.user_id == uid, AuditLog.action == "login")
                    .correlate(None)
                    .scalar_subquery()
                ).label("logins"),
                # Last login
                (
                    select(func.max(AuditLog.created_at))
                    .where(AuditLog.user_id == uid, AuditLog.action == "login")
                    .correlate(None)
                    .scalar_subquery()
                ).label("last_login"),
                # API keys
                (
                    select(func.count(UserApiKey.id))
                    .where(UserApiKey.user_id == uid)
                    .correlate(None)
                    .scalar_subquery()
                ).label("api_keys"),
                # API key total requests
                (
                    select(func.coalesce(func.sum(UserApiKey.request_count), 0))
                    .where(UserApiKey.user_id == uid)
                    .correlate(None)
                    .scalar_subquery()
                ).label("api_key_requests"),
            )
        )
    ).one()

    return UserStatsResponse(
        agent_count=row.agents or 0,
        active_assignment_count=row.active_wf or 0,
        conversation_count=row.conversations or 0,
        message_count=row.messages or 0,
        assignment_run_count=row.runs or 0,
        notification_count=row.notifications or 0,
        integration_count=row.integrations or 0,
        connected_integration_count=row.connected_integrations or 0,
        login_count=row.logins or 0,
        last_login_at=row.last_login.isoformat() if row.last_login else None,
        api_key_count=row.api_keys or 0,
        api_key_total_requests=row.api_key_requests or 0,
    )


@router.get("/{user_id}/integrations", response_model=list[UserIntegrationItem])
async def list_user_integrations(
    user_id: str,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """List a user's personal integrations (admin-only)."""
    owner_uuid = safe_uuid(user_id)
    if owner_uuid is None:
        # Names no user, like an unknown id: an empty list.
        return []
    result = await db.execute(
        select(Integration)
        .where(Integration.user_id == owner_uuid)
        .order_by(Integration.created_at.desc())
    )
    return [_user_integration_item(i) for i in result.scalars().all()]


def _user_integration_item(i: Integration) -> UserIntegrationItem:
    """One row of a user's integrations, with what its shown status reads (#11837)."""
    # Imported here, as the other routers do, to keep the integrations router
    # out of this module's import time.
    from app.api.v1.integrations import _resolve_linkedin_scopes

    catalog = PROVIDER_CATALOG.get(i.provider, {})
    if i.provider == "linkedin":
        required_scopes = _resolve_linkedin_scopes(catalog, i.extra_config_enc)
    else:
        required_scopes = catalog.get("scopes") or None
    has_access_token = bool(i.access_token_enc)
    granted = i.granted_scopes if has_access_token else None
    return UserIntegrationItem(
        id=str(i.id),
        provider=i.provider,
        auth_type=i.auth_type,
        status=i.status,
        name=i.name,
        is_company_managed=i.is_company_managed,
        created_at=i.created_at.isoformat() if i.created_at else "",
        connection_test_status=i.connection_test_status,
        has_access_token=has_access_token,
        scopes=i.scopes,
        required_scopes=required_scopes,
        missing_scopes=missing_scopes(i.scopes or required_scopes, granted, catalog.get("optional_scopes")),
    )


@router.get("/{user_id}/mcp-servers", response_model=list[UserMCPServerItem])
async def list_user_mcp_servers(
    user_id: str,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """List a user's personal MCP servers (admin-only)."""
    owner_uuid = safe_uuid(user_id)
    if owner_uuid is None:
        # Names no user, like an unknown id: an empty list.
        return []
    result = await db.execute(
        select(MCPServer)
        .where(MCPServer.user_id == owner_uuid)
        .order_by(MCPServer.created_at.desc())
    )
    return [
        UserMCPServerItem(
            id=str(s.id),
            name=s.name,
            transport=s.transport,
            is_company_managed=s.is_company_managed,
            enabled=s.enabled,
            created_at=s.created_at.isoformat() if s.created_at else "",
        )
        for s in result.scalars().all()
    ]


@router.get("/{user_id}/agents", response_model=list[UserAgentItem])
async def list_user_agents(
    user_id: str,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """List a user's agents (admin-only)."""
    owner_uuid = safe_uuid(user_id)
    if owner_uuid is None:
        # Names no user, like an unknown id: an empty list.
        return []
    result = await db.execute(
        select(Agent)
        .where(Agent.user_id == owner_uuid)
        .order_by(Agent.created_at.desc())
    )
    return [
        UserAgentItem(
            id=str(a.id),
            name=a.name,
            template_slug=a.template_slug,
            enabled=a.enabled,
            is_company_managed=a.is_company_managed,
            created_at=a.created_at.isoformat() if a.created_at else "",
        )
        for a in result.scalars().all()
    ]


@router.get("/{user_id}/knowledge-bases", response_model=list[UserKnowledgeBaseItem])
async def list_user_knowledge_bases(
    user_id: str,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """List a user's knowledge bases (admin-only)."""
    owner_uuid = safe_uuid(user_id)
    if owner_uuid is None:
        # Names no user, like an unknown id: an empty list.
        return []
    result = await db.execute(
        select(KnowledgeBase)
        .where(
            KnowledgeBase.user_id == owner_uuid,
            # This route selects by owner rather than through
            # `user_visible_filter`, so the internal-corpus exclusion has to be
            # repeated here: a workset's storage is not one of the user's
            # collections, whoever is asking.
            KnowledgeBase.is_internal.is_(False),
            # Nor is a workspace's own knowledge base: it belongs to the
            # workspace, whose owner it names only for attribution.
            KnowledgeBase.owner_workspace_id.is_(None),
        )
        .order_by(KnowledgeBase.created_at.desc())
    )
    return [
        UserKnowledgeBaseItem(
            id=str(kb.id),
            name=kb.name,
            document_count=kb.document_count or 0,
            chunk_count=kb.chunk_count or 0,
            is_company_managed=kb.is_company_managed,
            is_system_managed=kb.is_system_managed,
            created_at=kb.created_at.isoformat() if kb.created_at else "",
        )
        for kb in result.scalars().all()
    ]


@router.get("/{user_id}/custom-tools", response_model=list[UserCustomToolItem])
async def list_user_custom_tools(
    user_id: str,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """List a user's custom tools (admin-only)."""
    owner_uuid = safe_uuid(user_id)
    if owner_uuid is None:
        # Names no user, like an unknown id: an empty list.
        return []
    result = await db.execute(
        select(CustomTool)
        .where(CustomTool.user_id == owner_uuid)
        .order_by(CustomTool.created_at.desc())
    )
    return [
        UserCustomToolItem(
            id=str(t.id),
            name=t.name,
            enabled=t.enabled,
            is_company_managed=t.is_company_managed,
            created_at=t.created_at.isoformat() if t.created_at else "",
        )
        for t in result.scalars().all()
    ]


class UserApiKeyItem(BaseModel):
    id: str
    name: str
    key_prefix: str
    is_active: bool
    last_used_at: str | None = None
    request_count: int = 0
    created_at: str


@router.get("/{user_id}/api-keys", response_model=list[UserApiKeyItem])
async def list_user_api_keys(
    user_id: str,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """List a user's API keys with usage stats (admin-only)."""
    uid = parse_path_id(user_id, not_found_detail="User not found")

    # Verify user exists
    user_result = await db.execute(select(User).where(User.id == uid))
    if not user_result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="User not found")

    result = await db.execute(
        select(UserApiKey)
        .where(UserApiKey.user_id == uid)
        .order_by(UserApiKey.created_at.desc())
    )
    return [
        UserApiKeyItem(
            id=str(k.id),
            name=k.name,
            key_prefix=k.key_prefix,
            is_active=k.is_active,
            last_used_at=k.last_used_at.isoformat() if k.last_used_at else None,
            request_count=k.request_count or 0,
            created_at=k.created_at.isoformat() if k.created_at else "",
        )
        for k in result.scalars().all()
    ]


@router.delete(
    "/{user_id}/integrations/{integration_id}", status_code=status.HTTP_204_NO_CONTENT
)
async def delete_user_integration(
    user_id: str,
    integration_id: str,
    request: Request,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Admin deletes a user's integration."""
    result = await db.execute(
        select(Integration).where(
            Integration.id == parse_path_id(integration_id, not_found_detail="Integration not found"),
            Integration.user_id == parse_path_id(user_id, not_found_detail="Integration not found"),
        )
    )
    integ = result.scalar_one_or_none()
    if not integ:
        raise HTTPException(status_code=404, detail="Integration not found")
    await log_audit(
        db,
        user=admin,
        action="delete",
        entity_type="integration",
        entity_id=integ.id,
        entity_name=integ.name,
        summary=f"Admin deleted user integration '{integ.name}' (provider: {integ.provider})",
        details={"target_user_id": user_id},
        request=request,
    )
    await db.delete(integ)
    # Commit before answering: the get_db teardown only commits after the response is sent.
    await db.commit()


@router.delete(
    "/{user_id}/mcp-servers/{server_id}", status_code=status.HTTP_204_NO_CONTENT
)
async def delete_user_mcp_server(
    user_id: str,
    server_id: str,
    request: Request,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Admin deletes a user's MCP server."""
    result = await db.execute(
        select(MCPServer).where(
            MCPServer.id == parse_path_id(server_id, not_found_detail="MCP server not found"),
            MCPServer.user_id == parse_path_id(user_id, not_found_detail="MCP server not found"),
        )
    )
    srv = result.scalar_one_or_none()
    if not srv:
        raise HTTPException(status_code=404, detail="MCP server not found")
    await log_audit(
        db,
        user=admin,
        action="delete",
        entity_type="mcp_server",
        entity_id=srv.id,
        entity_name=srv.name,
        summary=f"Admin deleted user MCP server '{srv.name}'",
        details={"target_user_id": user_id},
        request=request,
    )
    await db.delete(srv)
    # Commit before answering: the get_db teardown only commits after the response is sent.
    await db.commit()


@router.delete("/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_user(
    user_id: str,
    request: Request,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Permanently delete a user and all associated data (admin-only).

    PostgreSQL CASCADE constraints handle deletion of all user-owned entities
    (agents, assignments, runs, integrations, KBs, MCP servers, custom tools,
    conversations, messages, embeddings, API keys, audit logs, etc.).
    Redis memory (agent short-term / medium-term, user memory) is also cleaned.
    """
    uid = parse_path_id(user_id, not_found_detail="User not found")

    result = await db.execute(select(User).where(User.id == uid))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    # Prevent deleting yourself
    if user.id == admin.id:
        raise HTTPException(status_code=400, detail="Cannot delete yourself")

    # The platform admin (from ADMIN_EMAIL env) cannot be deleted
    if is_platform_admin(user):
        raise HTTPException(
            status_code=400, detail="The platform admin account cannot be deleted"
        )

    user_email = user.email
    user_name = user.full_name

    from app.a2a.tasks import lock_tasks_before_deletion

    # Before the first write (the reassignment below updates runs): the A2A tasks
    # the cascade reaches are the first rows every other writer of a task and
    # its run takes.
    await lock_tasks_before_deletion(db, user_id=uid)

    # Preserve shared platform resources before the cascade. Company-managed
    # entities now keep a real ``user_id`` owner (always-set-owner invariant),
    # and that FK is ``ON DELETE CASCADE`` — so without this the deleted
    # owner would take every company-managed agent, assignment, run,
    # integration, KB, MCP server, custom tool, channel bot, prompt and variable
    # down with them. Hand them to the acting admin (a guaranteed-surviving
    # admin: self-deletion and platform-admin deletion are both blocked above).
    # Private rows are intentionally left to CASCADE — they are personal.
    await reassign_company_managed_entities(db, uid, admin.id)

    # Per-user share grants live in JSON arrays with no foreign key, so nothing
    # cascades them out. Drop them before the user row goes, or every entity
    # they were shared with keeps a grant to a nonexistent account.
    await prune_user_grants(db, uid)

    # The private agents the cascade is about to delete, whose Redis memory goes
    # with them. Read AFTER the reassignment above so company-managed agents
    # that now belong to the admin (and survive the delete) keep their memory.
    agent_result = await db.execute(select(Agent.id).where(Agent.user_id == uid))
    agent_ids = [str(row[0]) for row in agent_result.all()]

    # Audit BEFORE delete (the user row is about to be removed)
    await log_audit(
        db,
        user=admin,
        action="delete",
        entity_type="user",
        entity_name=user_name,
        summary=f"Deleted user '{user_email}' and all associated data",
        details={"deleted_user_id": user_id, "deleted_user_email": user_email},
        request=request,
    )

    # Again in the statement before the cascading DELETE: a task created since
    # the first lock goes too, and must be held first like the others.
    await lock_tasks_before_deletion(db, user_id=uid)
    # Delete the user — PostgreSQL ON DELETE CASCADE handles all child rows
    await db.delete(user)
    # Commit before responding, not on ``get_db`` teardown (after the response
    # is sent): a caller re-creating this address right away would otherwise
    # still find the old row and be refused with a 409.
    await db.commit()

    from app.services.quota import invalidate_membership_quota_cache

    # The cascade removed the account's group memberships with it.
    await invalidate_membership_quota_cache([uid])
    # Only once the account is really gone: these deletes cannot be undone, so
    # a failed commit must not leave the user in place with no memory.
    await _clear_deleted_user_memory(str(uid), agent_ids, db=db)


async def _clear_deleted_user_memory(
    user_id: str, agent_ids: list[str], *, db: AsyncSession | None = None
) -> None:
    """Drop the Redis memory of an account whose deletion has committed.

    The cascade deleted the account's private agents (*agent_ids*) and its
    persistent memory rows; this removes what Redis holds for them: every lane
    of those agents, the user's own lanes with the agents that survive, and
    their cross-agent profile. The account and the agents are tombstoned before
    their keys go, so a consolidation still in flight for them (detached from
    the chat turn) cannot recreate them. Best effort - the account is already
    gone.

    A platform account comes back under its deterministic id, and its
    re-creation lifts the account tombstone (``deps._lift_memory_tombstone``).
    One re-created while this ran would find the tombstone buried again after
    that lift, so when *db* shows the account exists again the tombstone is
    lifted once more, after the purge. The tombstone names this deletion and
    only that lift removes it: if the account is deleted again meanwhile, the
    tombstone of that later deletion stays.
    """
    try:
        from app.core.redis import redis_client
        from app.services.agent_memory import (
            clear_deleted_agent_memory,
            clear_user_scoped_memory,
        )
        from app.services.memory_tombstones import account_tombstone, bury, lift
        from app.services.user_memory import clear_user_memory

        redis = await redis_client()
        deletion = uuid.uuid4().hex
        # Once, before both purges: a second bury could land after a later
        # deletion's and write this deletion's name back over it.
        await bury(redis, account_tombstone(user_id), deletion=deletion)
        await clear_user_memory(user_id)
        await clear_user_scoped_memory(user_id)
        await clear_deleted_agent_memory(*agent_ids)
        if agent_ids:
            await redis.delete(*(f"chat:active:{agent_id}" for agent_id in agent_ids))
        if db is not None:
            found = await db.execute(select(User.id).where(User.id == uuid.UUID(user_id)))
            if found.scalar_one_or_none() is not None:
                await lift(redis, account_tombstone(user_id), deletion)
    except Exception:
        logger.warning("Redis cleanup failed during user deletion", exc_info=True)


@router.put("/{user_id}/role")
async def update_user_role(
    user_id: str,
    body: UpdateRoleRequest,
    request: Request,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Change a user's role (admin-only)."""
    if body.role not in ("admin", "user"):
        raise HTTPException(status_code=400, detail="Role must be 'admin' or 'user'")

    result = await db.execute(select(User).where(User.id == parse_path_id(user_id, not_found_detail="User not found")))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    # The platform admin (from ADMIN_EMAIL env) cannot be demoted
    if is_platform_admin(user) and body.role != "admin":
        raise HTTPException(
            status_code=400, detail="The platform admin account cannot be demoted"
        )

    user.role = body.role
    await db.flush()
    await log_audit(
        db,
        user=admin,
        action="update",
        entity_type="user",
        entity_id=user.id,
        entity_name=user.full_name,
        summary=f"Changed role of '{user.email}' to '{body.role}'",
        request=request,
    )
    # Commit before responding, not on ``get_db`` teardown (after the response
    # is sent): authorization re-reads the role on every request, so the next
    # one would otherwise race the transaction and still see the old role.
    await db.commit()
    return {"status": "ok", "user_id": user_id, "role": body.role}


class UpdateEmailRequest(BaseModel):
    email: EmailStr


@router.put("/{user_id}/email")
async def update_user_email(
    user_id: str,
    body: UpdateEmailRequest,
    request: Request,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Change a user's email (admin-only), the one way an email changes.

    Other products find their account by this address (the cross-platform JWT
    and the product sign-in carry it), so the person never sets it themselves
    (``PUT /settings/preferences`` refuses it). It applies to an SSO account too,
    to align it with the identity provider. The password reset links sent to
    the old address stop working, and when SMTP is configured a notice goes to
    the old and the new address.
    """
    from app.core.email_domains import email_domain_allowed
    from app.core.sso_helpers import get_sso_config

    result = await db.execute(select(User).where(User.id == parse_path_id(user_id, not_found_detail="User not found")))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    email = str(body.email).strip().lower()
    old_email = user.email
    if email == (old_email or "").strip().lower():
        return {"status": "ok", "user_id": user_id, "email": old_email}

    refusal = admin_email_change_refusal(user, email)
    if refusal:
        raise HTTPException(status_code=400, detail=refusal)

    domain_list = (await get_sso_config(db)).allowed_domain_list
    if not email_domain_allowed(email, domain_list):
        raise HTTPException(
            status_code=400,
            detail=f"Email addresses are restricted to: {', '.join(domain_list)}",
        )

    taken = await db.execute(select(User).where(func.lower(User.email) == email, User.id != user.id))
    if taken.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="A user with this email already exists")

    user.email = email
    try:
        await db.flush()
    except IntegrityError as exc:
        # Taken in between: the unique index on lower(email) refuses it.
        await db.rollback()
        raise HTTPException(status_code=409, detail="A user with this email already exists") from exc
    await log_audit(
        db,
        user=admin,
        action="update",
        entity_type="email",
        entity_id=user.id,
        entity_name=user.full_name,
        summary=f"Changed email of '{old_email}' to '{email}'",
        details={"old_email": old_email, "new_email": email},
        request=request,
    )
    # Commit before answering: the next sign-in and the next platform token
    # carry the new address.
    await db.commit()

    from app.services import email as email_service

    if email_service.is_email_configured():
        html, text = email_service.email_changed_email(old_email=old_email, new_email=email)
        for recipient in dict.fromkeys((old_email, email)):
            await email_service.send_email(
                to=recipient, subject="Your email address changed", html=html, text=text
            )
    return {"status": "ok", "user_id": user_id, "email": email}


@router.put("/{user_id}/active")
async def toggle_user_active(
    user_id: str,
    request: Request,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Toggle a user's active status (admin-only)."""
    result = await db.execute(select(User).where(User.id == parse_path_id(user_id, not_found_detail="User not found")))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    # Prevent admin from deactivating themselves
    if user.id == admin.id:
        raise HTTPException(status_code=400, detail="Cannot deactivate yourself")

    # The platform admin (from ADMIN_EMAIL env) cannot be disabled
    if is_platform_admin(user):
        raise HTTPException(
            status_code=400, detail="The platform admin account cannot be disabled"
        )

    # Share grants are deliberately NOT pruned here, unlike ``delete_user``.
    # Deactivation is reversible: pruning would silently destroy sharing
    # configuration that should come back intact when the account is
    # re-enabled, and a disabled account cannot authenticate, so the grant
    # confers nothing meanwhile. The read side is what accommodates this —
    # ``normalize_user_grants`` grandfathers already-stored ids past its
    # active-account check, and ``GET /users/directory?ids=`` still resolves the
    # name so the grant stays legible and removable.
    #
    # Everything else the account owns or that acts as it goes through the one
    # cascade every deactivation path shares (#122).
    from app.core.database import async_session
    from app.services import user_deactivation

    label = (admin.full_name or "").strip() or admin.email
    outcome: user_deactivation.DeactivationOutcome | user_deactivation.ReactivationOutcome
    try:
        if user.is_active:
            outcome = await user_deactivation.deactivate_user(db, user, source="admin", actor=admin, actor_label=label)
        else:
            outcome = await user_deactivation.reactivate_user(db, user, source="admin", actor=admin, actor_label=label)
    except user_deactivation.DeactivationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    # Commit before responding rather than on ``get_db`` teardown, which runs
    # after the response is sent: a caller acting on this toggle right away
    # (the A2A card / token surface stops resolving for a deactivated run-as
    # user) would otherwise race the transaction and still see the old state.
    await db.commit()
    if isinstance(outcome, user_deactivation.DeactivationOutcome):
        await user_deactivation.after_commit(async_session, user, outcome, label)
    return {
        "status": "ok",
        "user_id": user_id,
        "is_active": user.is_active,
        "cascade": outcome.as_dict(),
    }


# ---------------------------------------------------------------------------
# Password management (admin)
# ---------------------------------------------------------------------------


class SetPasswordRequest(BaseModel):
    new_password: str


class PasswordResetResponse(BaseModel):
    email_sent: bool
    expires_at: str
    # link is returned ONLY when send_email=False (seed/automation); otherwise
    # it is emailed to the user and never returned.
    link: str | None = None


@router.put("/{user_id}/password", status_code=status.HTTP_204_NO_CONTENT)
async def set_user_password(
    user_id: str,
    body: SetPasswordRequest,
    request: Request,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Set a user's password directly (admin-only).

    The admin chooses the password; they must then communicate it to the user.
    Prefer the reset-link flow when the admin should not know the password.
    """
    result = await db.execute(select(User).where(User.id == parse_path_id(user_id, not_found_detail="User not found")))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    if user.sso_provider and not user.hashed_password:
        raise HTTPException(
            status_code=400,
            detail="This account uses SSO for authentication. Password cannot be set — authentication is managed by the identity provider.",
        )

    validate_password(body.new_password)
    user.hashed_password = await hash_password_async(body.new_password)
    user.password_changed_at = datetime.now(UTC)
    await db.flush()
    await log_audit(
        db,
        user=admin,
        action="update",
        entity_type="password",
        entity_id=user.id,
        entity_name=user.full_name,
        summary=f"Set password for '{user.email}'",
        request=request,
    )
    # Commit before responding, not on ``get_db`` teardown (after the response
    # is sent): the next step is signing in with this password, which would
    # otherwise race the transaction and still be checked against the old hash.
    await db.commit()


@router.delete("/{user_id}/mfa", status_code=status.HTTP_204_NO_CONTENT)
async def reset_user_mfa(
    # Typed as ``UUID`` rather than ``str`` so FastAPI validates the path and
    # returns 422 on a malformed id. Parsing a ``str`` by hand — the older
    # pattern still used by this module's other routes — raises ``ValueError``
    # inside the handler, and there is no global handler for it, so a
    # copy-paste slip surfaces as an opaque 500 on a support endpoint.
    user_id: uuid.UUID,
    request: Request,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Clear a user's TOTP enrollment (admin-only).

    The lockout escape hatch: a lost or wiped phone otherwise leaves an account
    permanently unreachable, because the secret is unrecoverable by design and
    the recovery codes were on paper the user no longer has.

    Deliberately blunt — it disables MFA outright rather than re-issuing
    recovery codes, so the user re-enrolls from scratch on a device they
    actually hold. The admin cannot learn the old secret in the process.

    Idempotent: resetting a user who has no MFA is a no-op 204 rather than a
    404, so a support script does not need to probe first. The audit entry is
    written unconditionally — the *attempt* is the security-relevant event, and
    an admin quietly clearing second factors should be visible either way.
    """
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    was_enabled = bool(getattr(user, "mfa_enabled", False))
    user.mfa_enabled = False
    user.mfa_secret = None
    user.mfa_confirmed_at = None
    user.mfa_last_used_step = None
    user.mfa_recovery_codes = None
    await db.flush()

    await log_audit(
        db,
        user=admin,
        action="disable",
        entity_type="mfa",
        entity_id=user.id,
        entity_name=user.full_name,
        summary=f"Reset two-factor authentication for '{user.email}'",
        details={"was_enabled": was_enabled},
        request=request,
    )
    # Commit before responding, not on ``get_db`` teardown (after the response
    # is sent): the next step is the user signing in without the factor, which
    # would otherwise race the transaction and still be challenged for it.
    await db.commit()


@router.post(
    "/{user_id}/password-reset",
    response_model=PasswordResetResponse,
    status_code=status.HTTP_201_CREATED,
)
async def send_password_reset(
    user_id: str,
    request: Request,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    send_email: bool = True,
):
    """Issue a single-use password reset link for a user (admin-only).

    By default the link is emailed to the user and never returned. Pass
    ``?send_email=false`` to skip the email and get the link in the response
    instead (for seed scripts / automation).
    """
    result = await db.execute(select(User).where(User.id == parse_path_id(user_id, not_found_detail="User not found")))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    if user.sso_provider and not user.hashed_password:
        raise HTTPException(
            status_code=400,
            detail="This account uses SSO for authentication. Password reset is not available — authentication is managed by the identity provider.",
        )

    from app.services.email import is_email_configured, password_reset_email
    from app.services.email import send_email as _send_email

    # Same fail-fast contract as ``invitations.py::create_invitation``:
    # don't write a reset token to Redis we can't actually deliver.  In a
    # non-debug build the link would otherwise be silently dropped (no
    # email, no log line), leaving the admin with a useless promise.
    if send_email and not is_email_configured():
        raise HTTPException(
            status_code=503,
            detail=(
                "Email delivery is not configured (SMTP_HOSTNAME is unset), so "
                "the password reset link cannot be sent. Configure SMTP and "
                "retry, or pass ``send_email=false`` to retrieve the link "
                "directly."
            ),
        )

    from app.core.security_options import security_option

    ttl_hours = security_option("password_reset_expire_hours")
    raw_token, expires_at = await token_store.create_password_reset(
        user_id=user.id,
        created_by_id=admin.id,
        ttl_hours=ttl_hours,
        email=user.email,
    )

    # Reset link targets the frontend ``/reset-password`` route — use
    # ``frontend_url`` when frontend and backend live on different
    # origins (matching ``oauth_callbacks`` and ``auth_sso``), falling
    # back to ``base_url`` for single-origin deployments.
    frontend_base = (settings.frontend_url or settings.base_url).rstrip("/")
    link = f"{frontend_base}/reset-password?token={raw_token}"

    email_sent = False
    return_link: str | None = None
    if send_email:
        # SMTP is configured at this point (checked above); a False return
        # here means the actual SMTP exchange failed.  The token still lives
        # in Redis — the admin sees ``email_sent=false`` and can re-issue.
        html, text = password_reset_email(link, ttl_hours)
        email_sent = await _send_email(
            to=user.email, subject="Reset your password", html=html, text=text
        )
    else:
        return_link = link

    await log_audit(
        db,
        user=admin,
        action="create",
        entity_type="password",
        entity_id=user.id,
        entity_name=user.full_name,
        summary=f"Issued password reset for '{user.email}'"
        + ("" if send_email else " (link returned, email skipped)"),
        request=request,
    )
    # Commit before answering: the get_db teardown only commits after the response is sent.
    await db.commit()

    return PasswordResetResponse(
        email_sent=email_sent,
        expires_at=expires_at.isoformat(),
        link=return_link,
    )


# ---------------------------------------------------------------------------
# User groups
# ---------------------------------------------------------------------------


class UserGroupItem(BaseModel):
    id: str
    name: str
    description: str | None = None
    member_count: int = 0
    added_at: str


@router.get("/{user_id}/groups", response_model=list[UserGroupItem])
async def list_user_groups(
    user_id: str,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """List all groups a user belongs to (admin-only)."""
    uid = parse_path_id(user_id, not_found_detail="User not found")

    # Verify user exists
    user_result = await db.execute(select(User).where(User.id == uid))
    if not user_result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="User not found")

    # Fetch groups with member counts
    member_count = (
        select(func.count(GroupMember.id))
        .where(GroupMember.group_id == Group.id)
        .correlate(Group)
        .scalar_subquery()
    )

    result = await db.execute(
        select(
            Group, GroupMember.created_at.label("added_at"), member_count.label("mc")
        )
        .join(GroupMember, GroupMember.group_id == Group.id)
        .where(GroupMember.user_id == uid)
        .order_by(Group.name.asc())
    )

    return [
        UserGroupItem(
            id=str(g.id),
            name=g.name,
            description=g.description,
            member_count=mc or 0,
            added_at=added.isoformat() if added else "",
        )
        for g, added, mc in result.all()
    ]


# ---------------------------------------------------------------------------
# Channel identities (cross-channel identity linking)
# ---------------------------------------------------------------------------


class ChannelIdentityItem(BaseModel):
    channel_type: str
    platform_id: str


class LinkChannelIdentityRequest(BaseModel):
    channel_type: str  # e.g. "slack", "teams", "whatsapp", "telegram", "discord"
    platform_id: str  # e.g. Slack user ID, Teams user ID, WhatsApp phone number


@router.get("/{user_id}/channel-identities", response_model=list[ChannelIdentityItem])
async def list_channel_identities(
    user_id: str,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """List all linked channel identities for a user (admin-only)."""
    uid = parse_path_id(user_id, not_found_detail="User not found")
    result = await db.execute(select(User).where(User.id == uid))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    identities = user.channel_identities or {}
    items: list[ChannelIdentityItem] = []
    for key in identities:
        parts = key.split(":", 1)
        if len(parts) == 2:
            items.append(
                ChannelIdentityItem(channel_type=parts[0], platform_id=parts[1])
            )
    return items


@router.post("/{user_id}/channel-identities", status_code=status.HTTP_201_CREATED)
async def add_channel_identity(
    user_id: str,
    body: LinkChannelIdentityRequest,
    request: Request,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Link a channel identity to a user (admin-only).

    This allows the platform to recognise the user when they send messages
    from an external channel, even without email matching.
    """
    uid = parse_path_id(user_id, not_found_detail="User not found")
    result = await db.execute(select(User).where(User.id == uid))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    identity_key = f"{body.channel_type}:{body.platform_id}"
    current = user.channel_identities or {}
    if identity_key in current:
        raise HTTPException(
            status_code=409, detail="This channel identity is already linked"
        )

    # Check no other user already owns this identity
    existing = await db.execute(
        select(User).where(
            User.is_active.is_(True),
            User.id != uid,
            User.channel_identities.op("?")(identity_key),
        )
    )
    if existing.scalar_one_or_none():
        raise HTTPException(
            status_code=409,
            detail="This channel identity is already linked to another user",
        )

    # Create a new dict so SQLAlchemy detects the JSON column change
    # (in-place mutation of the same object is not tracked by the JSON type)
    user.channel_identities = {**current, identity_key: True}
    await db.flush()

    await log_audit(
        db,
        user=admin,
        action="update",
        entity_type="user",
        entity_id=user.id,
        entity_name=user.full_name,
        summary=f"Linked channel identity '{identity_key}' to user '{user.email}'",
        details={"channel_identity": identity_key},
        request=request,
    )
    # Commit before answering: the get_db teardown only commits after the response is sent.
    await db.commit()
    return {"status": "ok", "identity": identity_key}


@router.delete(
    "/{user_id}/channel-identities/{channel_type}/{platform_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def remove_channel_identity(
    user_id: str,
    channel_type: str,
    platform_id: str,
    request: Request,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Unlink a channel identity from a user (admin-only)."""
    uid = parse_path_id(user_id, not_found_detail="User not found")
    result = await db.execute(select(User).where(User.id == uid))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    identity_key = f"{channel_type}:{platform_id}"
    current = user.channel_identities or {}
    if identity_key not in current:
        raise HTTPException(status_code=404, detail="Channel identity not found")

    # Create a new dict without the identity so SQLAlchemy detects the change
    # (in-place mutation of the same object is not tracked by the JSON type)
    user.channel_identities = {k: v for k, v in current.items() if k != identity_key}
    await db.flush()

    await log_audit(
        db,
        user=admin,
        action="update",
        entity_type="user",
        entity_id=user.id,
        entity_name=user.full_name,
        summary=f"Unlinked channel identity '{identity_key}' from user '{user.email}'",
        details={"channel_identity": identity_key},
        request=request,
    )
    # Commit before answering: the get_db teardown only commits after the response is sent.
    await db.commit()


# ---------------------------------------------------------------------------
# Impersonation (admin-only)
# ---------------------------------------------------------------------------


class ImpersonationResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"  # noqa: S105
    # OAuth token type label, not a secret.
    target_user_id: str
    target_user_name: str
    target_user_email: str


@router.post("/{user_id}/impersonate", response_model=ImpersonationResponse)
async def impersonate_user(
    user_id: str,
    request: Request,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Start impersonating a user (admin-only).

    Returns a JWT that authenticates as the target user but carries the
    ``impersonated_by`` claim so the platform can display a banner and
    audit-log all actions performed while impersonating.
    """
    target_uid = parse_path_id(user_id, not_found_detail="User not found")

    # Cannot impersonate yourself
    if target_uid == admin.id:
        raise HTTPException(status_code=400, detail="Cannot impersonate yourself")

    result = await db.execute(select(User).where(User.id == target_uid))
    target = result.scalar_one_or_none()
    if not target:
        raise HTTPException(status_code=404, detail="User not found")
    if not target.is_active:
        raise HTTPException(
            status_code=400, detail="Cannot impersonate a disabled user"
        )

    # Create a JWT for the target user with impersonation metadata.
    # Only the ``impersonated_by`` session marker is embedded — never the
    # role, which is re-derived server-side and surfaced via GET /auth/me.
    # It carries the administrator's own session on: it ends when that
    # session does, and activity on either keeps both alive.
    admin_session = getattr(admin, "web_session", None)
    token = create_access_token(
        str(target.id),
        extra={"impersonated_by": str(admin.id)},
        continues=admin_session.claims if admin_session is not None else None,
    )

    await log_audit(
        db,
        user=admin,
        action="impersonate",
        entity_type="user",
        entity_id=target.id,
        entity_name=target.full_name,
        summary=f"Started impersonating '{target.email}'",
        details={"target_user_id": str(target.id)},
        request=request,
    )
    # Commit before answering: the get_db teardown only commits after the response is sent.
    await db.commit()

    return ImpersonationResponse(
        access_token=token,
        target_user_id=str(target.id),
        target_user_name=target.full_name,
        target_user_email=target.email,
    )


# ── User Quota Management ────────────────────────────────────────────────


@router.get("/{user_id}/quotas")
async def get_user_quotas(
    user_id: uuid.UUID,
    _admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Get resolved quota config and current usage for a user (admin only)."""
    from app.api.v1.schemas.settings import (
        QuotaConfig,
        QuotaUsageEntry,
        UserQuotaInfoResponse,
    )
    from app.core.redis import redis_client
    from app.services.quota import (
        get_user_quota_usage,
        legacy_quota_notices,
        read_override_config,
        resolve_full_quota_info,
    )

    target = await db.get(User, user_id)
    if not target:
        raise HTTPException(status_code=404, detail="User not found")

    redis = await redis_client()
    resolved, sources = await resolve_full_quota_info(db, redis, user_id)
    usage = await get_user_quota_usage(db, user_id)

    # ``user_config`` is the user's raw override (``User.quota_config``
    # column), distinct from ``resolved_config`` which walks the full
    # resolution chain (user → group → platform → license). The editor
    # form must bind to the override only, otherwise an empty override
    # looks pre-filled with the platform default and the "Clear" /
    # "inherit" workflow is broken — the user clears, saves, the
    # backend correctly nulls the override, but on reload the resolver
    # still returns the platform default and it looks like nothing
    # happened. Read like the resolver reads it: an
    # emptied type is absent, a legacy value with no limit is Unlimited.
    user_cfg = read_override_config(target.quota_config) or {}

    return UserQuotaInfoResponse(
        user_config=QuotaConfig(**user_cfg) if user_cfg else None,
        user_config_notices=await legacy_quota_notices(
            db, target.quota_config, tier="user"
        ),
        resolved_config=QuotaConfig(**resolved) if resolved else None,
        usage=[QuotaUsageEntry(**e) for e in usage],
        sources=sources,
    )


class UpdateUserQuotaRequest(BaseModel):
    quota_config: dict | None = None


@router.put("/{user_id}/quotas")
async def update_user_quotas(
    user_id: uuid.UUID,
    body: UpdateUserQuotaRequest,
    request: Request,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    after_commit: AfterCommit = Depends(run_after_commit, scope="function"),
):
    """Set user-level quota overrides (admin only)."""
    from app.services.quota import (
        get_xtm_agentic_ceilings,
        invalidate_quota_cache,
        normalize_override_for_write,
    )

    target = await db.get(User, user_id)
    if not target:
        raise HTTPException(status_code=404, detail="User not found")

    # An emptied type inherits and is dropped; Unlimited is stored explicitly.
    cfg = normalize_override_for_write(body.quota_config)
    if cfg:
        from app.core.config import settings as app_settings

        if app_settings.is_xtm_one:
            cfg.pop("llm_proxy", None)

        agentic = cfg.get("agentic")
        if agentic and isinstance(agentic, dict):
            agentic_default = agentic.get("default")
            agentic_period = agentic.get("period", "monthly")
            if agentic_default is not None:
                ceilings = await get_xtm_agentic_ceilings(db)
                if ceilings:
                    ceiling = ceilings.get(agentic_period)
                    if ceiling is not None and agentic_default > ceiling:
                        raise HTTPException(
                            status_code=400,
                            detail=f"Agentic execution quota cannot exceed license limit ({ceiling}/{agentic_period})",
                        )

    target.quota_config = cfg if cfg else None
    await db.flush()
    # Once the change has committed: dropped before, a quota check landing in
    # between re-caches the old override for the cache's whole TTL.
    after_commit.add(invalidate_quota_cache, user_id)

    await log_audit(
        db,
        user=admin,
        action="configure",
        entity_type="user",
        entity_id=target.id,
        entity_name=target.full_name,
        summary=f"Updated quota configuration for '{target.email}'",
        request=request,
    )
    # Commit before answering: the get_db teardown only commits after the response is sent.
    await db.commit()

    return {"quota_config": target.quota_config}


@router.post("/{user_id}/quotas/reset", status_code=status.HTTP_204_NO_CONTENT)
async def reset_user_quotas(
    user_id: uuid.UUID,
    request: Request,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Reset quota usage counters for a user (admin only)."""
    from app.services.quota import QuotaClearBusyError, reset_user_quota_usage

    target = await db.get(User, user_id)
    if not target:
        raise HTTPException(status_code=404, detail="User not found")

    # Audit first: ``reset_user_quota_usage`` commits, and that one commit lands
    # the reset together with its audit row, before the answer.
    await log_audit(
        db,
        user=admin,
        action="configure",
        entity_type="user",
        entity_id=target.id,
        entity_name=target.full_name,
        summary=f"Reset quota usage counters for '{target.email}'",
        request=request,
    )
    try:
        await reset_user_quota_usage(db, user_id)
    except QuotaClearBusyError as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": "quota_flush_busy", "message": str(exc)},
            headers={"Retry-After": "5"},
        ) from exc
