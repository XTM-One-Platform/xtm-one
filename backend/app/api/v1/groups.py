# SPDX-License-Identifier: Apache-2.0

"""Groups admin endpoints – CRUD for user groups and group membership."""

import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import AfterCommit, get_current_user, require_admin, run_after_commit
from app.api.v1.helpers import parse_path_id, prune_group_grants
from app.api.v1.schemas.column_limits import column_length_validator
from app.core.database import get_db
from app.core.utils import safe_uuid
from app.llm.provider_access import providers_excluding_group
from app.models.group import GROUP_KINDS, Group, GroupManager, GroupMember
from app.models.user import User
from app.services import ctem_access, group_ai_access
from app.services.audit import log_audit
from app.services.group_managers import managed_groups, team_member_clause
from app.services.quota import legacy_quota_notices, read_override_config

router = APIRouter()


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class CreateGroupRequest(BaseModel):
    name: str
    description: str | None = None

    _fit_columns = column_length_validator(Group, "name", stripped=True)


class UpdateGroupRequest(BaseModel):
    name: str | None = None
    description: str | None = None
    quota_config: dict | None = None
    # Switch AI on (true) or off (false) for the members, positive on the wire
    # like the platform switch (`services/group_ai_access.py` flips it). `null`
    # or an omitted field leaves it as it is.
    ai_enabled: bool | None = None
    # What the group is for the analytics: one of `GROUP_KINDS`.
    kind: str | None = None
    # The CTEM right granted to the members (`services/ctem_access.py`):
    # `view`, `decide`, or `none` to remove it; `null` or omitted leaves it.
    ctem_access: str | None = None
    # With `decide`: notify the members of the case gates (`null` leaves it).
    ctem_notify: bool | None = None
    # People who sign in through single sign-on join the group at every sign-in
    # (`services/group_auto_join.py`); `null` or omitted leaves it.
    auto_join: bool | None = None

    _fit_columns = column_length_validator(Group, "name", "kind", "ctem_access", stripped=True)


class GroupListItem(BaseModel):
    id: str
    name: str
    description: str | None = None
    member_count: int = 0
    created_by: str | None = None
    quota_config: dict | None = None
    ai_enabled: bool = True
    kind: str = "auto"
    ctem_access: str | None = None
    ctem_notify: bool = True
    auto_join: bool = False
    created_at: str
    updated_at: str


class GroupMemberItem(BaseModel):
    id: str
    user_id: str
    email: str
    full_name: str
    role: str
    added_at: str
    # Whether the member is also one of the group's managers.
    is_manager: bool = False


class PaginatedGroupMembers(BaseModel):
    items: list[GroupMemberItem]
    total: int


class GroupManagerItem(BaseModel):
    user_id: str
    email: str
    full_name: str
    # A manager need not be a member of the group they manage.
    is_member: bool
    assigned_at: str


class AddManagersRequest(BaseModel):
    user_ids: list[str]


class ManagedGroupItem(BaseModel):
    id: str
    name: str
    # The members whose usage the manager reads: administrators left out.
    member_count: int = 0


class GroupDetailResponse(BaseModel):
    id: str
    name: str
    description: str | None = None
    member_count: int = 0
    created_by: str | None = None
    quota_config: dict | None = None
    # quota_type -> notice code for a legacy value the next save would change.
    quota_notices: dict[str, str] = {}
    ai_enabled: bool = True
    kind: str = "auto"
    ctem_access: str | None = None
    ctem_notify: bool = True
    auto_join: bool = False
    created_at: str
    updated_at: str


class AddMembersRequest(BaseModel):
    user_ids: list[str]


# ---------------------------------------------------------------------------
# Routes (all admin-only)
# ---------------------------------------------------------------------------


@router.post("", response_model=GroupListItem, status_code=status.HTTP_201_CREATED)
async def create_group(
    body: CreateGroupRequest,
    request: Request,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Create a new user group (admin-only)."""
    if not body.name or not body.name.strip():
        raise HTTPException(status_code=400, detail="Group name is required")

    # Check for duplicate name
    exists = await db.execute(select(Group).where(Group.name == body.name.strip()))
    if exists.scalar_one_or_none():
        raise HTTPException(
            status_code=409, detail="A group with this name already exists"
        )

    group = Group(
        name=body.name.strip(),
        description=body.description,
        created_by=admin.id,
    )
    db.add(group)
    await db.flush()

    await log_audit(
        db,
        user=admin,
        action="create",
        entity_type="group",
        entity_id=group.id,
        entity_name=group.name,
        summary=f"Created group '{group.name}'",
        request=request,
    )
    # Commit before answering: the get_db teardown only commits after the response is sent.
    await db.commit()

    return GroupListItem(
        id=str(group.id),
        name=group.name,
        description=group.description,
        member_count=0,
        created_by=str(group.created_by) if group.created_by else None,
        created_at=group.created_at.isoformat() if group.created_at else "",
        updated_at=group.updated_at.isoformat() if group.updated_at else "",
    )


def filter_values(raw: str | None) -> list[str]:
    """The values of a filter given once or comma-separated (``kind=org_unit,access``)."""
    if not raw:
        return []
    return [value for value in (part.strip() for part in raw.split(",")) if value]


def _member_count():
    return (
        select(func.count(GroupMember.id))
        .where(GroupMember.group_id == Group.id)
        .correlate(Group)
        .scalar_subquery()
    )


def group_sorts() -> dict[str, Any]:
    """The columns of the Groups table the API sorts on (``sort=``, #11940)."""
    return {
        "name": func.lower(Group.name),
        "description": func.lower(func.coalesce(Group.description, "")),
        "kind": Group.kind,
        "members": _member_count(),
        "created": Group.created_at,
    }


def group_order(sort: str | None, direction: str | None) -> list[Any]:
    """By name when ``sort`` is unknown or absent, else the column of ``sort``,
    ascending unless ``direction`` is ``desc``; the id breaks every tie."""
    column = group_sorts().get(sort or "")
    if column is None:
        return [func.lower(Group.name).asc(), Group.id.asc()]
    if direction == "desc":
        return [column.desc(), Group.id.desc()]
    return [column.asc(), Group.id.asc()]


def group_filters(search: str | None, kind: str | None, members: str | None) -> list[Any]:
    """The WHERE clauses of the Groups table: name or description contains,
    kinds (``auto`` also matches an unset kind), ``some`` / ``empty`` members."""
    clauses: list[Any] = []
    term = (search or "").strip()
    if term:
        pattern = f"%{term}%"
        clauses.append(or_(Group.name.ilike(pattern), Group.description.ilike(pattern)))
    kinds = filter_values(kind)
    if kinds:
        kind_clause = Group.kind.in_(kinds)
        clauses.append(or_(kind_clause, Group.kind.is_(None)) if "auto" in kinds else kind_clause)
    wanted = set(filter_values(members)) & {"some", "empty"}
    if len(wanted) == 1:
        clauses.append(_member_count() > 0 if "some" in wanted else _member_count() == 0)
    return clauses


@router.get("/count")
async def count_groups(
    search: str | None = Query(None, description="Name or description contains"),
    kind: str | None = Query(None, description="One kind or several comma-separated"),
    members: str | None = Query(None, description="some or empty"),
    _user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """How many groups match the filters of ``GET /groups``: the table's total.

    Declared before ``/{group_id}`` so the literal path is not read as an id.
    """
    count = await db.scalar(select(func.count(Group.id)).where(*group_filters(search, kind, members)))
    return {"count": count or 0}


@router.get("", response_model=list[GroupListItem])
async def list_groups(
    search: str | None = Query(None, description="Name or description contains"),
    kind: str | None = Query(None, description="One kind or several comma-separated"),
    members: str | None = Query(None, description="some or empty"),
    sort: str | None = Query(None, description="Column to sort on: name, description, kind, members, created"),
    direction: str | None = Query(None, description="asc (default) or desc"),
    limit: int | None = Query(None, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    _user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List groups with member counts (any authenticated user). Without a
    parameter every group, by name; the Groups table passes its search,
    filters, sort and page."""
    member_count = _member_count()

    result = await db.execute(
        select(Group, member_count.label("member_count"))
        .where(*group_filters(search, kind, members))
        .order_by(*group_order(sort, direction))
        .limit(limit)
        .offset(offset)
    )

    return [
        GroupListItem(
            id=str(g.id),
            name=g.name,
            description=g.description,
            member_count=mc or 0,
            created_by=str(g.created_by) if g.created_by else None,
            kind=g.kind or "auto",
            ctem_access=ctem_access.group_ctem_access(g),
            ctem_notify=ctem_access.group_ctem_notify(g),
            auto_join=bool(g.auto_join),
            created_at=g.created_at.isoformat() if g.created_at else "",
            updated_at=g.updated_at.isoformat() if g.updated_at else "",
        )
        for g, mc in result.all()
    ]


@router.get("/managed", response_model=list[ManagedGroupItem])
async def list_managed_groups(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """The groups the caller manages, whose members' LLM Analytics they read.

    Declared before ``/{group_id}`` so the literal path is not read as an id.
    """
    groups = await managed_groups(db, user.id)
    if not groups:
        return []
    counts = dict(
        (
            await db.execute(
                select(GroupMember.group_id, func.count(GroupMember.id))
                .where(GroupMember.group_id.in_([uuid.UUID(group.id) for group in groups]), team_member_clause())
                .group_by(GroupMember.group_id)
            )
        ).all()
    )
    return [
        ManagedGroupItem(id=group.id, name=group.name, member_count=int(counts.get(uuid.UUID(group.id), 0)))
        for group in groups
    ]


@router.get("/{group_id}", response_model=GroupDetailResponse)
async def get_group(
    group_id: str,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Get group details (admin-only)."""
    gid = parse_path_id(group_id, not_found_detail="Group not found")
    result = await db.execute(select(Group).where(Group.id == gid))
    group = result.scalar_one_or_none()
    if not group:
        raise HTTPException(status_code=404, detail="Group not found")

    member_count = (
        await db.execute(
            select(func.count(GroupMember.id)).where(GroupMember.group_id == group.id)
        )
    ).scalar() or 0

    return GroupDetailResponse(
        id=str(group.id),
        name=group.name,
        description=group.description,
        member_count=member_count,
        created_by=str(group.created_by) if group.created_by else None,
        quota_config=read_override_config(group.quota_config),
        quota_notices=await legacy_quota_notices(db, group.quota_config, tier="group"),
        ai_enabled=group_ai_access.group_ai_enabled(group),
        kind=group.kind or "auto",
        ctem_access=ctem_access.group_ctem_access(group),
        ctem_notify=ctem_access.group_ctem_notify(group),
        auto_join=bool(group.auto_join),
        created_at=group.created_at.isoformat() if group.created_at else "",
        updated_at=group.updated_at.isoformat() if group.updated_at else "",
    )


@router.get("/{group_id}/members", response_model=PaginatedGroupMembers)
async def list_group_members(
    group_id: str,
    search: str | None = Query(None, description="Filter by name or email"),
    role: str | None = Query(None, description="User role (admin or user), one or several comma-separated"),
    manager: str | None = Query(None, description="yes: the group's managers; no: the other members"),
    sort: str | None = Query(None, description="Column to sort on: name, email, role, added"),
    direction: str | None = Query(None, description="asc (default) or desc"),
    limit: int = Query(20, ge=1, le=500),
    offset: int = Query(0, ge=0),
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """List group members with search, filters, sort and pagination (admin-only)."""
    gid = parse_path_id(group_id, not_found_detail="Group not found")
    result = await db.execute(select(Group).where(Group.id == gid))
    group = result.scalar_one_or_none()
    if not group:
        raise HTTPException(status_code=404, detail="Group not found")

    # Base query
    base = (
        select(GroupMember, User)
        .join(User, GroupMember.user_id == User.id)
        .where(GroupMember.group_id == group.id)
    )

    # Search filter
    if search and search.strip():
        q = f"%{search.strip().lower()}%"
        base = base.where(
            or_(
                func.lower(User.full_name).like(q),
                func.lower(User.email).like(q),
            )
        )

    # Role filter
    roles = filter_values(role)
    if roles:
        base = base.where(User.role.in_(roles))

    # Manager filter
    managers = select(GroupManager.user_id).where(GroupManager.group_id == group.id)
    if manager == "yes":
        base = base.where(User.id.in_(managers))
    elif manager == "no":
        base = base.where(~User.id.in_(managers))

    # Count total before pagination
    count_query = select(func.count()).select_from(base.subquery())
    total = (await db.execute(count_query)).scalar() or 0

    # Apply pagination and ordering
    paginated = (
        base.order_by(*member_order(sort, direction))
        .limit(limit)
        .offset(offset)
    )
    rows = (await db.execute(paginated)).all()
    manager_ids = await _manager_ids(db, group.id)

    items = [
        GroupMemberItem(
            id=str(gm.id),
            user_id=str(u.id),
            email=u.email,
            full_name=u.full_name,
            role=u.role,
            added_at=gm.created_at.isoformat() if gm.created_at else "",
            is_manager=u.id in manager_ids,
        )
        for gm, u in rows
    ]

    return PaginatedGroupMembers(items=items, total=total)


#: The columns of the members table the API sorts on (``sort=``, #11940).
MEMBER_SORTS: dict[str, Any] = {
    "name": func.lower(User.full_name),
    "email": func.lower(User.email),
    "role": User.role,
    "added": GroupMember.created_at,
}


def member_order(sort: str | None, direction: str | None) -> list[Any]:
    """By name then email when ``sort`` is unknown or absent, else the column of
    ``sort``, ascending unless ``direction`` is ``desc``; the id breaks every tie."""
    column = MEMBER_SORTS.get(sort or "")
    if column is None:
        return [func.lower(User.full_name).asc(), User.email.asc(), GroupMember.id.asc()]
    if direction == "desc":
        return [column.desc(), GroupMember.id.desc()]
    return [column.asc(), GroupMember.id.asc()]


async def _manager_ids(db: AsyncSession, group_id: uuid.UUID) -> set[uuid.UUID]:
    rows = await db.execute(select(GroupManager.user_id).where(GroupManager.group_id == group_id))
    return {user_id for (user_id,) in rows.all()}


async def _load_group(db: AsyncSession, group_id: str, *, not_found_detail: str = "Group not found") -> Group:
    gid = parse_path_id(group_id, not_found_detail=not_found_detail)
    group = (await db.execute(select(Group).where(Group.id == gid))).scalar_one_or_none()
    if group is None:
        raise HTTPException(status_code=404, detail=not_found_detail)
    return group


@router.get("/{group_id}/managers", response_model=list[GroupManagerItem])
async def list_group_managers(
    group_id: str,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """The group's managers (admin-only), by name."""
    group = await _load_group(db, group_id)
    member_ids = set(
        (await db.execute(select(GroupMember.user_id).where(GroupMember.group_id == group.id))).scalars().all()
    )
    rows = (
        await db.execute(
            select(GroupManager, User)
            .join(User, GroupManager.user_id == User.id)
            .where(GroupManager.group_id == group.id)
            .order_by(func.lower(User.full_name).asc(), User.email.asc())
        )
    ).all()
    return [
        GroupManagerItem(
            user_id=str(user.id),
            email=user.email,
            full_name=user.full_name,
            is_member=user.id in member_ids,
            assigned_at=manager.created_at.isoformat() if manager.created_at else "",
        )
        for manager, user in rows
    ]


@router.post("/{group_id}/managers", response_model=list[GroupManagerItem])
async def add_managers(
    group_id: str,
    body: AddManagersRequest,
    request: Request,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Elect one or more users manager of a group (admin-only).

    A manager reads the AI usage of the group's current members; they need
    not be a member. Unknown, malformed and already-elected ids are skipped,
    like ``POST /{group_id}/members``.
    """
    group = await _load_group(db, group_id)
    requested = [parsed for value in body.user_ids if (parsed := safe_uuid(value)) is not None]
    if not requested:
        return []
    users = (await db.execute(select(User).where(User.id.in_(requested)))).scalars().all()
    existing = await _manager_ids(db, group.id)
    member_ids = set(
        (await db.execute(select(GroupMember.user_id).where(GroupMember.group_id == group.id))).scalars().all()
    )
    added: list[GroupManagerItem] = []
    for user in sorted(users, key=lambda item: (item.full_name or "").casefold()):
        if user.id in existing:
            continue
        manager = GroupManager(group_id=group.id, user_id=user.id, assigned_by=admin.id)
        db.add(manager)
        await db.flush()
        existing.add(user.id)
        added.append(
            GroupManagerItem(
                user_id=str(user.id),
                email=user.email,
                full_name=user.full_name,
                is_member=user.id in member_ids,
                assigned_at=manager.created_at.isoformat() if manager.created_at else "",
            )
        )
    if added:
        await log_audit(
            db,
            user=admin,
            action="bind",
            entity_type="group",
            entity_id=group.id,
            entity_name=group.name,
            summary=(
                f"Elected {len(added)} manager(s) of group '{group.name}': "
                + ", ".join(item.full_name or item.email for item in added)
            ),
            details={"role": "manager", "user_ids": [item.user_id for item in added]},
            request=request,
        )
    # Commit before answering: the get_db teardown only commits after the response is sent.
    await db.commit()
    return added


@router.delete("/{group_id}/managers/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_manager(
    group_id: str,
    user_id: str,
    request: Request,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Remove a user from a group's managers (admin-only)."""
    gid = parse_path_id(group_id, not_found_detail="Group manager not found")
    uid = parse_path_id(user_id, not_found_detail="Group manager not found")
    manager = (
        await db.execute(select(GroupManager).where(GroupManager.group_id == gid, GroupManager.user_id == uid))
    ).scalar_one_or_none()
    if manager is None:
        raise HTTPException(status_code=404, detail="Group manager not found")
    group = (await db.execute(select(Group).where(Group.id == gid))).scalar_one_or_none()
    user = (await db.execute(select(User).where(User.id == uid))).scalar_one_or_none()
    await log_audit(
        db,
        user=admin,
        action="unbind",
        entity_type="group",
        entity_id=gid,
        entity_name=group.name if group else None,
        summary=(
            f"Removed '{user.full_name if user else user_id}' from the managers of group "
            f"'{group.name if group else group_id}'"
        ),
        details={"role": "manager", "user_ids": [str(uid)]},
        request=request,
    )
    await db.delete(manager)
    # Commit before answering: the get_db teardown only commits after the response is sent.
    await db.commit()


@router.put("/{group_id}", response_model=GroupListItem)
async def update_group(
    group_id: str,
    body: UpdateGroupRequest,
    request: Request,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    after_commit: AfterCommit = Depends(run_after_commit, scope="function"),
):
    """Update a group's name, description, quotas, AI switch or kind (admin-only)."""
    gid = parse_path_id(group_id, not_found_detail="Group not found")
    result = await db.execute(select(Group).where(Group.id == gid))
    group = result.scalar_one_or_none()
    if not group:
        raise HTTPException(status_code=404, detail="Group not found")

    if body.name is not None:
        name = body.name.strip()
        if not name:
            raise HTTPException(status_code=400, detail="Group name cannot be empty")
        # Check unique name (excluding self)
        exists = await db.execute(
            select(Group).where(Group.name == name, Group.id != group.id)
        )
        if exists.scalar_one_or_none():
            raise HTTPException(
                status_code=409, detail="A group with this name already exists"
            )
        group.name = name

    if body.description is not None:
        group.description = body.description or None

    # ``null`` or an omitted field leaves the override as it is - API clients
    # send the whole group back on a rename - and ``{}`` clears it.
    if body.quota_config is not None:
        from app.services.quota import get_xtm_agentic_ceilings, invalidate_quota_cache, normalize_override_for_write

        # An omitted type inherits; one holding no limit keeps meaning
        # Unlimited and is stored explicitly.
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

        group.quota_config = cfg or None
        # Once the change has committed: dropped before, a quota check landing
        # in between re-caches the old config for the cache's whole TTL.
        after_commit.add(invalidate_quota_cache)

    ai_change: dict[str, bool] | None = None
    if body.ai_enabled is not None and body.ai_enabled != group_ai_access.group_ai_enabled(group):
        previous_ai_enabled = group_ai_access.set_group_ai_enabled(group, body.ai_enabled)
        # The change that cuts the most people off from the platform's purpose:
        # its values are in the audit entry. Dropped once it has committed,
        # before it a concurrent gate would re-cache the old set for a TTL.
        ai_change = {"ai_enabled": body.ai_enabled, "previous": previous_ai_enabled}
        after_commit.add(group_ai_access.invalidate_cache_after_commit)

    if body.kind is not None:
        kind = body.kind.strip().lower()
        if kind not in GROUP_KINDS:
            raise HTTPException(status_code=422, detail=f"A group's kind must be one of {', '.join(GROUP_KINDS)}.")
        group.kind = kind

    ctem_change: dict[str, object] = {}
    if "ctem_access" in body.model_fields_set and body.ctem_access is not None:
        try:
            right = ctem_access.normalize_right(body.ctem_access)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
        if right != ctem_access.group_ctem_access(group):
            ctem_change["ctem_access"] = right
            ctem_change["previous_ctem_access"] = ctem_access.group_ctem_access(group)
            group.ctem_access = right
    if body.ctem_notify is not None and body.ctem_notify != ctem_access.group_ctem_notify(group):
        ctem_change["ctem_notify"] = body.ctem_notify
        group.ctem_notify = body.ctem_notify
    # No cache to drop: the CTEM reach is read on every call (signal members included).

    auto_join_change: dict[str, bool] | None = None
    if body.auto_join is not None and body.auto_join != bool(group.auto_join):
        # Audited with the value it replaces; it grants memberships at the next
        # single sign-on of each person, none now.
        auto_join_change = {"auto_join": body.auto_join, "previous_auto_join": bool(group.auto_join)}
        group.auto_join = body.auto_join

    await db.flush()

    # A request that also changes the name, description or quotas keeps the
    # generic summary, so the AI switch never hides the rest of the update.
    other_fields = sorted(body.model_fields_set - {"ai_enabled"})
    summary = f"Updated group '{group.name}'"
    details: dict[str, Any] | None = dict(ctem_change) if ctem_change else None
    if auto_join_change:
        details = {**(details or {}), **auto_join_change}
        if not (body.model_fields_set - {"auto_join"}):
            summary = f"Switched auto join {'on' if auto_join_change['auto_join'] else 'off'} for group '{group.name}'"
    if ai_change:
        ai_text = "on" if ai_change["ai_enabled"] else "off"
        details = {**(details or {}), **ai_change}
        if other_fields:
            summary += f" and switched AI {ai_text}"
        else:
            summary = f"Switched AI {ai_text} for group '{group.name}'"

    await log_audit(
        db,
        user=admin,
        action="update",
        entity_type="group",
        entity_id=group.id,
        entity_name=group.name,
        summary=summary,
        details=details,
        request=request,
    )

    member_count = (
        await db.execute(
            select(func.count(GroupMember.id)).where(GroupMember.group_id == group.id)
        )
    ).scalar() or 0
    # Commit before answering: the get_db teardown only commits after the response is sent.
    await db.commit()

    return GroupListItem(
        id=str(group.id),
        name=group.name,
        description=group.description,
        member_count=member_count,
        created_by=str(group.created_by) if group.created_by else None,
        quota_config=read_override_config(group.quota_config),
        ai_enabled=group_ai_access.group_ai_enabled(group),
        kind=group.kind or "auto",
        ctem_access=ctem_access.group_ctem_access(group),
        ctem_notify=ctem_access.group_ctem_notify(group),
        auto_join=bool(group.auto_join),
        created_at=group.created_at.isoformat() if group.created_at else "",
        updated_at=group.updated_at.isoformat() if group.updated_at else "",
    )


@router.delete("/{group_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_group(
    group_id: str,
    request: Request,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    after_commit: AfterCommit = Depends(run_after_commit, scope="function"),
):
    """Delete a group and clean up shared_group_ids references (admin-only)."""
    gid = parse_path_id(group_id, not_found_detail="Group not found")
    result = await db.execute(select(Group).where(Group.id == gid))
    group = result.scalar_one_or_none()
    if not group:
        raise HTTPException(status_code=404, detail="Group not found")

    # An LLM provider excluding the group keeps it: deleting it would
    # quietly hand its members a vendor they are kept off. Locked first, so an
    # exclusion being added either committed before this read or waits.
    await db.execute(select(Group.id).where(Group.id == group.id).with_for_update())
    excluding = await providers_excluding_group(db, group.id)
    if excluding:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "This group is excluded from the LLM provider(s) "
                + ", ".join(f"'{name}'" for name in excluding)
                + ". Remove it from their exclusions in Settings > AI and models > Models before deleting it."
            ),
        )

    # Group grants live in JSON arrays with no foreign key, so nothing cascades
    # them out: drop them from every entity carrying them before the row goes.
    await prune_group_grants(db, group.id)

    await log_audit(
        db,
        user=admin,
        action="delete",
        entity_type="group",
        entity_id=group.id,
        entity_name=group.name,
        summary=f"Deleted group '{group.name}'",
        request=request,
    )

    had_quota = bool(group.quota_config)
    # Read before the cascade removes them.
    member_ids = (
        await db.execute(
            select(GroupMember.user_id).where(GroupMember.group_id == group.id)
        )
    ).scalars().all()
    from app.services.quota import (
        invalidate_membership_quota_cache,
        invalidate_quota_cache,
    )

    # Once the deletion has committed.
    if had_quota:
        # Its quota no longer takes part in anyone's resolution, a member
        # added while it was being deleted included.
        after_commit.add(invalidate_quota_cache)
    else:
        after_commit.add(invalidate_membership_quota_cache, member_ids)
    if not group_ai_access.group_ai_enabled(group):
        # Its members get AI back the moment it is gone.
        after_commit.add(group_ai_access.invalidate_cache_after_commit)
    await db.delete(group)
    # Commit before answering: the get_db teardown only commits after the response is sent.
    await db.commit()


@router.post("/{group_id}/members", response_model=list[GroupMemberItem])
async def add_members(
    group_id: str,
    body: AddMembersRequest,
    request: Request,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    after_commit: AfterCommit = Depends(run_after_commit, scope="function"),
):
    """Add one or more users to a group (admin-only)."""
    gid = parse_path_id(group_id, not_found_detail="Group not found")
    result = await db.execute(select(Group).where(Group.id == gid))
    group = result.scalar_one_or_none()
    if not group:
        raise HTTPException(status_code=404, detail="Group not found")

    added = []
    for uid_str in body.user_ids:
        try:
            uid = uuid.UUID(uid_str)
        except (ValueError, AttributeError):
            continue  # Skip invalid UUIDs

        # Check user exists
        user_result = await db.execute(select(User).where(User.id == uid))
        user = user_result.scalar_one_or_none()
        if not user:
            continue  # Skip invalid user IDs

        # Check for existing membership
        existing = await db.execute(
            select(GroupMember).where(
                GroupMember.group_id == group.id,
                GroupMember.user_id == uid,
            )
        )
        if existing.scalar_one_or_none():
            continue  # Already a member

        member = GroupMember(
            group_id=group.id,
            user_id=uid,
            added_by=admin.id,
        )
        db.add(member)
        await db.flush()

        added.append(
            GroupMemberItem(
                id=str(member.id),
                user_id=str(user.id),
                email=user.email,
                full_name=user.full_name,
                role=user.role,
                added_at=member.created_at.isoformat() if member.created_at else "",
            )
        )

    if added:
        from app.services.quota import invalidate_membership_quota_cache

        # Once the memberships have committed: a user's resolved quota includes
        # their groups' quotas.
        after_commit.add(
            invalidate_membership_quota_cache, [member.user_id for member in added]
        )
        names = ", ".join(m.full_name for m in added)
        await log_audit(
            db,
            user=admin,
            action="bind",
            entity_type="group",
            entity_id=group.id,
            entity_name=group.name,
            summary=f"Added {len(added)} member(s) to group '{group.name}': {names}",
            request=request,
        )
    # Commit before answering: the get_db teardown only commits after the response is sent.
    await db.commit()

    return added


@router.delete("/{group_id}/members/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_member(
    group_id: str,
    user_id: str,
    request: Request,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    after_commit: AfterCommit = Depends(run_after_commit, scope="function"),
):
    """Remove a user from a group (admin-only)."""
    gid = parse_path_id(group_id, not_found_detail="Group membership not found")
    uid = parse_path_id(user_id, not_found_detail="Group membership not found")

    result = await db.execute(
        select(GroupMember).where(
            GroupMember.group_id == gid,
            GroupMember.user_id == uid,
        )
    )
    member = result.scalar_one_or_none()
    if not member:
        raise HTTPException(status_code=404, detail="Group membership not found")
    from app.services.quota import invalidate_membership_quota_cache

    # Once the removal has committed: the user's resolved quota includes their
    # groups' quotas.
    after_commit.add(invalidate_membership_quota_cache, [uid])

    # Fetch group name for audit
    group_result = await db.execute(select(Group).where(Group.id == gid))
    group = group_result.scalar_one_or_none()

    # Fetch user name for audit
    user_result = await db.execute(select(User).where(User.id == uid))
    user = user_result.scalar_one_or_none()

    await log_audit(
        db,
        user=admin,
        action="unbind",
        entity_type="group",
        entity_id=gid,
        entity_name=group.name if group else None,
        summary=f"Removed '{user.full_name if user else user_id}' from group '{group.name if group else group_id}'",
        request=request,
    )

    await db.delete(member)
    from app.services.agent_bindings import unlink_agents_shared_with_group

    await unlink_agents_shared_with_group(db, gid)
    # Commit before answering: the get_db teardown only commits after the response is sent.
    await db.commit()
