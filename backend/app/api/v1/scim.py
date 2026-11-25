# SPDX-License-Identifier: Apache-2.0

"""SCIM 2.0 provisioning (RFC 7643 / RFC 7644) for an identity provider such
as Microsoft Entra, and the administrator's SCIM tokens.

Two routers, both mounted under ``/api/v1/scim``:

- ``tokens_router`` (``/tokens``, admin): generate a token (shown once), list,
  revoke;
- ``scim_router`` (``/v2``, a SCIM bearer token, nothing else):
  ``ServiceProviderConfig``, ``ResourceTypes``, ``Schemas``, ``Users`` and
  ``Groups``.

Every refusal on ``/v2`` is a SCIM error body (``application/scim+json``),
never FastAPI's ``{"detail": ...}``. Every write is audited, as
``system:scim:<token name>``. An account SCIM deactivates (``active`` false)
or deletes goes through the one cascade of ``services/user_deactivation.py``;
a SCIM DELETE is a deactivation that also stamps ``deprovisioned_at``: nothing
is purged. An account SCIM creates whose ``userName`` already exists (an SSO or
local account) is linked, never duplicated. Contract: dev-docs/scim.md.
"""

from __future__ import annotations

import logging
import secrets
import uuid
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse, Response
from fastapi.routing import APIRoute
from pydantic import BaseModel, Field
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_admin
from app.api.v1.helpers import parse_path_id
from app.core.config import settings
from app.core.database import get_db
from app.core.rate_limit import enforce_rate_limit
from app.core.security import hash_token
from app.core.utils import external_base
from app.models.agent import Agent
from app.models.group import Group, GroupMember
from app.models.scim_token import SCIM_TOKEN_PREFIX, ScimToken
from app.models.user import User, admin_email_change_refusal
from app.services import scim as s
from app.services.audit import log_audit, log_system_audit

logger = logging.getLogger(__name__)

#: Calls one SCIM token may make per minute. A full Entra sync of a large
#: tenant pages through users and groups in bursts; the limit stops a leaked
#: token from enumerating faster than that.
SCIM_RATE_LIMIT_PER_MINUTE = 600

#: How a SCIM-created account and a SCIM-sourced membership are marked.
SCIM_SOURCE = "scim"


# ---------------------------------------------------------------------------
# The SCIM error shape on every refusal of /v2
# ---------------------------------------------------------------------------


def _scim_json(body: Any, status_code: int = 200) -> JSONResponse:
    return JSONResponse(body, status_code=status_code, media_type=s.SCIM_CONTENT_TYPE)


class ScimRoute(APIRoute):
    """Answers a :class:`~app.services.scim.ScimError` or an ``HTTPException``
    raised by the route or its dependencies in the SCIM error shape."""

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        handler = super().get_route_handler()

        async def _handler(request: Request) -> Response:
            try:
                return await handler(request)
            except s.ScimError as exc:
                return _scim_json(exc.body(), exc.status)
            except HTTPException as exc:
                error = s.ScimError(exc.status_code, str(exc.detail))
                response = _scim_json(error.body(), exc.status_code)
                for name, value in (exc.headers or {}).items():
                    response.headers[name] = value
                return response

        return _handler


# ---------------------------------------------------------------------------
# Authentication: a SCIM token and nothing else
# ---------------------------------------------------------------------------


async def require_scim_token(request: Request, db: AsyncSession = Depends(get_db)) -> ScimToken:
    header = request.headers.get("authorization", "")
    scheme, _, raw = header.partition(" ")
    raw = raw.strip()
    if scheme.lower() != "bearer" or not raw.startswith(SCIM_TOKEN_PREFIX):
        raise s.ScimError(401, "A SCIM bearer token is required")
    token = (
        await db.execute(
            select(ScimToken).where(ScimToken.token_hash == hash_token(raw), ScimToken.revoked_at.is_(None))
        )
    ).scalar_one_or_none()
    if token is None:
        raise s.ScimError(401, "Invalid or revoked SCIM token")
    await enforce_rate_limit(
        request, "scim", limit=SCIM_RATE_LIMIT_PER_MINUTE, window_seconds=60, bucket=f"scim:{token.id}"
    )
    # Stamped in its own short transaction, so the routes that only read
    # leave nothing uncommitted on the request session.
    from app.core.database import async_session

    async with async_session() as stamp:
        await stamp.execute(
            update(ScimToken).where(ScimToken.id == token.id).values(last_used_at=datetime.now(UTC))
        )
        await stamp.commit()
    return token


def _base(request: Request) -> str:
    return f"{external_base(request)}/api/v1/scim/v2"


def _actor(token: ScimToken) -> str:
    return f"scim:{token.name}"


def _label(token: ScimToken) -> str:
    return f"SCIM ({token.name})"


async def _audit(
    db: AsyncSession,
    token: ScimToken,
    *,
    anchor: uuid.UUID | None,
    action: str,
    entity_type: str,
    entity_id: uuid.UUID | None,
    entity_name: str | None,
    summary: str,
    details: dict[str, Any] | None = None,
) -> None:
    """One audit row per SCIM write, anchored on the account it concerns (or,
    for a group, the administrator who generated the token)."""
    anchor = anchor or token.created_by
    if anchor is None:
        logger.info("SCIM %s %s not audited: no account to anchor it on (%s)", action, entity_type, summary)
        return
    await log_system_audit(
        db,
        actor=_actor(token),
        anchor_user_id=anchor,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        entity_name=entity_name,
        summary=summary,
        details={**(details or {}), "scim_token_id": str(token.id)},
    )


def _uuid_or_404(value: str, kind: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except (ValueError, AttributeError):
        raise s.ScimError(404, f"{kind} {value} not found") from None


scim_router = APIRouter(route_class=ScimRoute)


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


@scim_router.get("/ServiceProviderConfig")
async def service_provider_config(request: Request, _: ScimToken = Depends(require_scim_token)) -> Response:
    return _scim_json(s.service_provider_config(_base(request)))


@scim_router.get("/ResourceTypes")
async def resource_types(request: Request, _: ScimToken = Depends(require_scim_token)) -> Response:
    types = s.resource_types(_base(request))
    return _scim_json(s.list_response(types, len(types), 1))


@scim_router.get("/ResourceTypes/{type_id}")
async def resource_type(type_id: str, request: Request, _: ScimToken = Depends(require_scim_token)) -> Response:
    for entry in s.resource_types(_base(request)):
        if entry["id"] == type_id:
            return _scim_json(entry)
    raise s.ScimError(404, f"Resource type {type_id} not found")


@scim_router.get("/Schemas")
async def schemas(request: Request, _: ScimToken = Depends(require_scim_token)) -> Response:
    entries = s.schemas(_base(request))
    return _scim_json(s.list_response(entries, len(entries), 1))


@scim_router.get("/Schemas/{schema_id}")
async def schema(schema_id: str, request: Request, _: ScimToken = Depends(require_scim_token)) -> Response:
    for entry in s.schemas(_base(request)):
        if entry["id"] == schema_id:
            return _scim_json(entry)
    raise s.ScimError(404, f"Schema {schema_id} not found")


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------


async def _user_or_404(db: AsyncSession, user_id: str) -> User:
    user = await db.get(User, _uuid_or_404(user_id, "User"))
    if user is None:
        raise s.ScimError(404, f"User {user_id} not found")
    return user


async def _by_email(db: AsyncSession, email: str) -> User | None:
    return (await db.execute(select(User).where(func.lower(User.email) == email.lower()))).scalar_one_or_none()


async def _body(request: Request) -> Any:
    try:
        return await request.json()
    except ValueError:
        raise s.ScimError(400, "The body is not JSON", "invalidSyntax") from None


async def _apply_user(db: AsyncSession, user: User, changes: s.UserChanges, token: ScimToken) -> tuple[list[str], Any]:
    """Write *changes* on *user*: the changed field names, and the deactivation
    outcome when the account was deactivated (for ``after_commit``)."""
    from app.services import user_deactivation

    changed: list[str] = []
    if changes.user_name:
        email = changes.user_name.strip().lower()
        if email != user.email.lower():
            refusal = admin_email_change_refusal(user, email)
            if refusal:
                raise s.ScimError(400, refusal, "mutability")
            other = await _by_email(db, email)
            if other is not None and other.id != user.id:
                raise s.ScimError(409, f"userName {changes.user_name} is taken", "uniqueness")
            user.email = email
            changed.append("userName")
    full_name = changes.display_name(user.full_name)
    if full_name and full_name != user.full_name:
        user.full_name = full_name[:255]
        changed.append("displayName")
    if changes.title_set and changes.title != user.title:
        user.title = changes.title
        changed.append("title")
    if changes.department_set and changes.department != user.directory_department:
        user.directory_department = changes.department
        changed.append("department")
    if changes.external_id_set and changes.external_id != user.scim_external_id:
        user.scim_external_id = changes.external_id
        changed.append("externalId")
    outcome: Any = None
    if changes.active is not None and changes.active != bool(user.is_active):
        try:
            if changes.active:
                await user_deactivation.reactivate_user(db, user, source=SCIM_SOURCE, actor_label=_label(token))
            else:
                outcome = await user_deactivation.deactivate_user(
                    db, user, source=SCIM_SOURCE, actor_label=_label(token)
                )
        except user_deactivation.DeactivationError as exc:
            raise s.ScimError(400, str(exc), "mutability") from exc
        changed.append("active")
    return changed, outcome


async def _commit_user(db: AsyncSession, user: User, outcome: Any, token: ScimToken) -> None:
    """Commit, then what only follows a committed deactivation (partners, emails)."""
    from app.core.database import async_session
    from app.services import user_deactivation

    await db.commit()
    if outcome is not None and not getattr(outcome, "already", False):
        await user_deactivation.after_commit(async_session, user, outcome, _label(token))


@scim_router.get("/Users")
async def list_users(
    request: Request,
    filter: str | None = None,
    startIndex: int | None = None,  # noqa: N803 - the SCIM parameter's name
    count: int | None = None,
    db: AsyncSession = Depends(get_db),
    _: ScimToken = Depends(require_scim_token),
) -> Response:
    wanted = s.parse_filter(filter, {"username", "externalid", "emails.value"})
    query = select(User)
    if "username" in wanted:
        query = query.where(func.lower(User.email) == wanted["username"].lower())
    if "emails.value" in wanted:
        query = query.where(func.lower(User.email) == wanted["emails.value"].lower())
    if "externalid" in wanted:
        query = query.where(User.scim_external_id == wanted["externalid"])
    total = (await db.execute(select(func.count()).select_from(query.subquery()))).scalar_one()
    offset, limit = s.page(startIndex, count)
    rows = (await db.execute(query.order_by(User.created_at, User.id).offset(offset).limit(limit))).scalars().all()
    base = _base(request)
    return _scim_json(s.list_response([s.user_resource(u, base) for u in rows], total, offset + 1))


@scim_router.get("/Users/{user_id}")
async def get_user(
    user_id: str, request: Request, db: AsyncSession = Depends(get_db), _: ScimToken = Depends(require_scim_token)
) -> Response:
    return _scim_json(s.user_resource(await _user_or_404(db, user_id), _base(request)))


@scim_router.post("/Users")
async def create_user(
    request: Request, db: AsyncSession = Depends(get_db), token: ScimToken = Depends(require_scim_token)
) -> Response:
    changes = s.user_changes_from_resource(await _body(request))
    if not changes.user_name:
        raise s.ScimError(400, "userName is required", "invalidValue")
    existing = await _by_email(db, changes.user_name)
    if existing is not None:
        # An account SSO or an administrator created first: linked, never
        # duplicated. One the identity provider already knows under another
        # externalId is a real conflict.
        if existing.scim_external_id and changes.external_id and existing.scim_external_id != changes.external_id:
            raise s.ScimError(409, f"userName {changes.user_name} is taken", "uniqueness")
        changed, outcome = await _apply_user(db, existing, changes, token)
        await _audit(
            db,
            token,
            anchor=existing.id,
            action="update",
            entity_type="user",
            entity_id=existing.id,
            entity_name=existing.full_name,
            summary=f"SCIM linked the existing account {existing.email}",
            details={"changed": changed, "linked": True},
        )
        await _commit_user(db, existing, outcome, token)
        return _scim_json(s.user_resource(existing, _base(request)), status.HTTP_201_CREATED)

    from app.core.sso_helpers import get_sso_config

    cfg = await get_sso_config(db)
    email = changes.user_name.strip().lower()
    refusal = admin_email_change_refusal(None, email)
    if refusal:
        raise s.ScimError(400, refusal, "invalidValue")
    active = changes.active is not False
    now = datetime.now(UTC)
    user = User(
        email=email,
        hashed_password=None,
        full_name=(changes.display_name() or email)[:255],
        title=changes.title,
        directory_department=changes.department,
        role="user",
        sso_provider="saml" if cfg.saml_enabled else "oidc" if cfg.oidc_enabled else None,
        scim_external_id=changes.external_id,
        is_active=active,
        deactivated_at=None if active else now,
        deactivation_source=None if active else SCIM_SOURCE,
    )
    if settings.is_xtm_one:
        ctem_id = (
            await db.execute(select(Agent.id).where(Agent.slug == "ctem-assistant").limit(1))
        ).scalar_one_or_none()
        if ctem_id:
            user.default_agent_id = ctem_id
            user.default_agent_source = "platform"
            user.default_agent_set_at = datetime.now(UTC)
    db.add(user)
    await db.flush()
    await _audit(
        db,
        token,
        anchor=user.id,
        action="create",
        entity_type="user",
        entity_id=user.id,
        entity_name=user.full_name,
        summary=f"SCIM provisioned {user.email}" + ("" if active else " (inactive)"),
    )
    await db.commit()
    return _scim_json(s.user_resource(user, _base(request)), status.HTTP_201_CREATED)


async def _write_user(
    request: Request, db: AsyncSession, token: ScimToken, user: User, changes: s.UserChanges
) -> Response:
    changed, outcome = await _apply_user(db, user, changes, token)
    if changed:
        action = "update"
        if "active" in changed:
            action = "toggle"
        await _audit(
            db,
            token,
            anchor=user.id,
            action=action,
            entity_type="user",
            entity_id=user.id,
            entity_name=user.full_name,
            summary=f"SCIM changed {', '.join(changed)} of {user.email}",
            details={"changed": changed, "active": bool(user.is_active)},
        )
    await _commit_user(db, user, outcome, token)
    return _scim_json(s.user_resource(user, _base(request)))


@scim_router.put("/Users/{user_id}")
async def replace_user(
    user_id: str, request: Request, db: AsyncSession = Depends(get_db), token: ScimToken = Depends(require_scim_token)
) -> Response:
    user = await _user_or_404(db, user_id)
    return await _write_user(request, db, token, user, s.user_changes_from_resource(await _body(request)))


@scim_router.patch("/Users/{user_id}")
async def patch_user(
    user_id: str, request: Request, db: AsyncSession = Depends(get_db), token: ScimToken = Depends(require_scim_token)
) -> Response:
    user = await _user_or_404(db, user_id)
    return await _write_user(request, db, token, user, s.user_changes_from_patch(await _body(request)))


@scim_router.delete("/Users/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_user(
    user_id: str, db: AsyncSession = Depends(get_db), token: ScimToken = Depends(require_scim_token)
) -> Response:
    """Deprovision: the cascade's deactivation plus ``deprovisioned_at``. Nothing
    is purged; a later POST of the same userName links the account again."""
    from app.services import user_deactivation

    user = await _user_or_404(db, user_id)
    try:
        outcome = await user_deactivation.deactivate_user(
            db, user, source=SCIM_SOURCE, actor_label=_label(token), deprovision=True
        )
    except user_deactivation.DeactivationError as exc:
        raise s.ScimError(400, str(exc), "mutability") from exc
    await _audit(
        db,
        token,
        anchor=user.id,
        action="delete",
        entity_type="user",
        entity_id=user.id,
        entity_name=user.full_name,
        summary=f"SCIM deprovisioned {user.email}",
        details={"already_inactive": outcome.already},
    )
    await _commit_user(db, user, outcome, token)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------------------
# Groups
# ---------------------------------------------------------------------------


async def _group_or_404(db: AsyncSession, group_id: str) -> Group:
    group = await db.get(Group, _uuid_or_404(group_id, "Group"))
    if group is None:
        raise s.ScimError(404, f"Group {group_id} not found")
    return group


async def _members(db: AsyncSession, group_id: uuid.UUID) -> list[tuple[str, str]]:
    rows = (
        await db.execute(
            select(User.id, User.full_name)
            .join(GroupMember, GroupMember.user_id == User.id)
            .where(GroupMember.group_id == group_id)
            .order_by(User.full_name)
        )
    ).all()
    return [(str(uid), name) for uid, name in rows]


async def _set_name(db: AsyncSession, group: Group, name: str | None) -> bool:
    if not name or name == group.name:
        return False
    clash = (await db.execute(select(Group.id).where(Group.name == name, Group.id != group.id))).scalar_one_or_none()
    if clash is not None:
        raise s.ScimError(409, f"displayName {name} is taken", "uniqueness")
    group.name = name[:200]
    return True


async def _change_members(
    db: AsyncSession, group: Group, *, add: list[str], remove: list[str], replace: list[str] | None
) -> tuple[set[uuid.UUID], set[uuid.UUID]]:
    """Apply membership changes; SCIM removes only the memberships it created,
    an administrator's or SAML's stay. Answers (added, removed) user ids."""
    rows = (await db.execute(select(GroupMember).where(GroupMember.group_id == group.id))).scalars().all()
    current = {row.user_id: row for row in rows}

    def _ids(values: list[str]) -> set[uuid.UUID]:
        out: set[uuid.UUID] = set()
        for value in values:
            try:
                out.add(uuid.UUID(value))
            except (ValueError, AttributeError):
                raise s.ScimError(400, f"Unknown member {value}", "invalidValue") from None
        return out

    to_add = _ids(add)
    to_remove = _ids(remove)
    if replace is not None:
        wanted = _ids(replace)
        to_add |= wanted
        to_remove |= set(current) - wanted
    to_add -= set(current)
    if to_add:
        known = set((await db.execute(select(User.id).where(User.id.in_(to_add)))).scalars().all())
        unknown = to_add - known
        if unknown:
            raise s.ScimError(400, f"Unknown member {sorted(map(str, unknown))[0]}", "invalidValue")
    removed: set[uuid.UUID] = set()
    for uid in to_remove:
        row = current.get(uid)
        if row is not None and row.source == SCIM_SOURCE:
            await db.delete(row)
            removed.add(uid)
    for uid in to_add:
        db.add(GroupMember(group_id=group.id, user_id=uid, added_by=None, source=SCIM_SOURCE))
    await db.flush()
    return to_add, removed


async def _commit_group(db: AsyncSession, group: Group | None, user_ids: set[uuid.UUID]) -> None:
    """Commit, then drop the caches a membership change makes stale."""
    from app.services import group_ai_access
    from app.services.quota import invalidate_membership_quota_cache

    await db.commit()
    if user_ids:
        await invalidate_membership_quota_cache(user_ids)
        if group is None or not group_ai_access.group_ai_enabled(group):
            await group_ai_access.invalidate_cache_after_commit()


def _wants_members(excluded: str | None) -> bool:
    return "members" not in {a.strip().lower() for a in (excluded or "").split(",")}


@scim_router.get("/Groups")
async def list_groups(
    request: Request,
    filter: str | None = None,
    startIndex: int | None = None,  # noqa: N803 - the SCIM parameter's name
    count: int | None = None,
    excludedAttributes: str | None = None,  # noqa: N803 - the SCIM parameter's name
    db: AsyncSession = Depends(get_db),
    _: ScimToken = Depends(require_scim_token),
) -> Response:
    wanted = s.parse_filter(filter, {"displayname", "externalid", "id", "members.value"})
    query = select(Group)
    if "displayname" in wanted:
        query = query.where(Group.name == wanted["displayname"])
    if "externalid" in wanted:
        query = query.where(Group.scim_external_id == wanted["externalid"])
    if "id" in wanted:
        query = query.where(Group.id == _uuid_or_404(wanted["id"], "Group"))
    if "members.value" in wanted:
        member = _uuid_or_404(wanted["members.value"], "User")
        query = query.where(Group.id.in_(select(GroupMember.group_id).where(GroupMember.user_id == member)))
    total = (await db.execute(select(func.count()).select_from(query.subquery()))).scalar_one()
    offset, limit = s.page(startIndex, count)
    rows = (await db.execute(query.order_by(Group.created_at, Group.id).offset(offset).limit(limit))).scalars().all()
    base = _base(request)
    with_members = _wants_members(excludedAttributes)
    resources = [s.group_resource(g, await _members(db, g.id) if with_members else None, base) for g in rows]
    return _scim_json(s.list_response(resources, total, offset + 1))


@scim_router.get("/Groups/{group_id}")
async def get_group(
    group_id: str,
    request: Request,
    excludedAttributes: str | None = None,  # noqa: N803 - the SCIM parameter's name
    db: AsyncSession = Depends(get_db),
    _: ScimToken = Depends(require_scim_token),
) -> Response:
    group = await _group_or_404(db, group_id)
    members = await _members(db, group.id) if _wants_members(excludedAttributes) else None
    return _scim_json(s.group_resource(group, members, _base(request)))


@scim_router.post("/Groups")
async def create_group(
    request: Request, db: AsyncSession = Depends(get_db), token: ScimToken = Depends(require_scim_token)
) -> Response:
    body = await _body(request)
    if not isinstance(body, dict) or not isinstance(body.get("displayName"), str) or not body["displayName"].strip():
        raise s.ScimError(400, "displayName is required", "invalidValue")
    name = body["displayName"].strip()[:200]
    external_id = body.get("externalId") if isinstance(body.get("externalId"), str) else None
    group = (await db.execute(select(Group).where(Group.name == name))).scalar_one_or_none()
    linked = group is not None
    if group is not None:
        # A group an administrator or SAML created first is linked, unless the
        # identity provider already knows it under another externalId.
        if group.scim_external_id and external_id and group.scim_external_id != external_id:
            raise s.ScimError(409, f"displayName {name} is taken", "uniqueness")
        if external_id:
            group.scim_external_id = external_id
    else:
        group = Group(name=name, created_by=token.created_by, scim_external_id=external_id)
        db.add(group)
        await db.flush()
    added, _ = await _change_members(db, group, add=s.group_members_from_resource(body), remove=[], replace=None)
    await _audit(
        db,
        token,
        anchor=None,
        action="update" if linked else "create",
        entity_type="group",
        entity_id=group.id,
        entity_name=group.name,
        summary=f"SCIM {'linked' if linked else 'provisioned'} group '{group.name}' ({len(added)} member(s) added)",
    )
    await _commit_group(db, group, added)
    return _scim_json(s.group_resource(group, await _members(db, group.id), _base(request)), status.HTTP_201_CREATED)


async def _write_group(
    request: Request, db: AsyncSession, token: ScimToken, group: Group, changes: s.GroupChanges
) -> Response:
    renamed = await _set_name(db, group, changes.display_name)
    if changes.external_id_set:
        group.scim_external_id = changes.external_id
    added, removed = await _change_members(db, group, add=changes.add, remove=changes.remove, replace=changes.replace)
    if renamed or added or removed or changes.external_id_set:
        await _audit(
            db,
            token,
            anchor=None,
            action="update",
            entity_type="group",
            entity_id=group.id,
            entity_name=group.name,
            summary=f"SCIM changed group '{group.name}': {len(added)} added, {len(removed)} removed"
            + (", renamed" if renamed else ""),
            details={"added": sorted(map(str, added)), "removed": sorted(map(str, removed))},
        )
    await _commit_group(db, group, added | removed)
    return _scim_json(s.group_resource(group, await _members(db, group.id), _base(request)))


@scim_router.patch("/Groups/{group_id}")
async def patch_group(
    group_id: str, request: Request, db: AsyncSession = Depends(get_db), token: ScimToken = Depends(require_scim_token)
) -> Response:
    group = await _group_or_404(db, group_id)
    return await _write_group(request, db, token, group, s.group_changes_from_patch(await _body(request)))


@scim_router.put("/Groups/{group_id}")
async def replace_group(
    group_id: str, request: Request, db: AsyncSession = Depends(get_db), token: ScimToken = Depends(require_scim_token)
) -> Response:
    group = await _group_or_404(db, group_id)
    body = await _body(request)
    if not isinstance(body, dict):
        raise s.ScimError(400, "The body must be a JSON object", "invalidSyntax")
    changes = s.GroupChanges(
        display_name=body.get("displayName") if isinstance(body.get("displayName"), str) else None,
        external_id=body.get("externalId") if isinstance(body.get("externalId"), str) else None,
        external_id_set="externalId" in body,
        replace=s.group_members_from_resource(body),
    )
    return await _write_group(request, db, token, group, changes)


@scim_router.delete("/Groups/{group_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_group(
    group_id: str, db: AsyncSession = Depends(get_db), token: ScimToken = Depends(require_scim_token)
) -> Response:
    """Delete the group the way an administrator does (``api/v1/groups.py``):
    refused while an LLM provider excludes it, its grants pruned first."""
    from app.api.v1.helpers import prune_group_grants
    from app.llm.provider_access import providers_excluding_group

    group = await _group_or_404(db, group_id)
    await db.execute(select(Group.id).where(Group.id == group.id).with_for_update())
    excluding = await providers_excluding_group(db, group.id)
    if excluding:
        raise s.ScimError(
            409,
            "This group is excluded from the LLM provider(s) "
            + ", ".join(f"'{name}'" for name in excluding)
            + "; an administrator removes the exclusion before it can be deleted.",
        )
    member_ids = set((await db.execute(select(GroupMember.user_id).where(GroupMember.group_id == group.id))).scalars())
    await prune_group_grants(db, group.id)
    await _audit(
        db,
        token,
        anchor=None,
        action="delete",
        entity_type="group",
        entity_id=group.id,
        entity_name=group.name,
        summary=f"SCIM deleted group '{group.name}'",
    )
    await db.delete(group)
    await _commit_group(db, None, member_ids)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------------------
# Tokens (administrators)
# ---------------------------------------------------------------------------

tokens_router = APIRouter()


class ScimTokenCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)


def _token_dict(token: ScimToken) -> dict[str, Any]:
    return {
        "id": str(token.id),
        "name": token.name,
        "token_prefix": token.token_prefix,
        "created_at": token.created_at.isoformat() if token.created_at else None,
        "last_used_at": token.last_used_at.isoformat() if token.last_used_at else None,
        "revoked_at": token.revoked_at.isoformat() if token.revoked_at else None,
    }


@tokens_router.get("")
async def list_scim_tokens(
    request: Request, admin: User = Depends(require_admin), db: AsyncSession = Depends(get_db)
) -> dict[str, Any]:
    """The SCIM endpoint URL and every token, never a raw value."""
    rows = (await db.execute(select(ScimToken).order_by(ScimToken.created_at.desc()))).scalars().all()
    return {"endpoint": _base(request), "tokens": [_token_dict(t) for t in rows]}


def _toggled_state(action: str, details: dict[str, Any] | None) -> bool | None:
    """The account state a ``toggle`` row recorded, else ``None``."""
    if action != "toggle" or not isinstance(details, dict):
        return None
    active = details.get("active")
    return active if isinstance(active, bool) else None


@tokens_router.get("/activity")
async def list_scim_activity(
    limit: int = 50, admin: User = Depends(require_admin), db: AsyncSession = Depends(get_db)
) -> list[dict[str, Any]]:
    """The latest SCIM writes, newest first: every SCIM write is audited under
    ``system:scim:<token name>`` (reads and refused calls are not audited).

    ``active`` is the account's state after a ``toggle`` (the identity provider
    deactivated or reactivated it), read from the audit details; ``None`` on
    every other row, so the screen names the direction in the app language."""
    from app.models.audit_log import AuditLog
    from app.services.audit import SYSTEM_ACTOR_PREFIX

    rows = (
        (
            await db.execute(
                select(AuditLog)
                .where(AuditLog.user_email.like(f"{SYSTEM_ACTOR_PREFIX}scim:%"))
                .order_by(AuditLog.created_at.desc())
                .limit(max(1, min(limit, 200)))
            )
        )
        .scalars()
        .all()
    )
    return [
        {
            "id": str(row.id),
            "token": row.user_email.removeprefix(f"{SYSTEM_ACTOR_PREFIX}scim:"),
            "action": row.action,
            "entity_type": row.entity_type,
            "entity_name": row.entity_name,
            "summary": row.summary,
            "active": _toggled_state(row.action, row.details),
            "created_at": row.created_at.isoformat() if row.created_at else None,
        }
        for row in rows
    ]


@tokens_router.post("", status_code=status.HTTP_201_CREATED)
async def create_scim_token(
    body: ScimTokenCreate, request: Request, admin: User = Depends(require_admin), db: AsyncSession = Depends(get_db)
) -> dict[str, Any]:
    """Generate a token: its raw value is in this answer and nowhere else."""
    raw = SCIM_TOKEN_PREFIX + secrets.token_urlsafe(32)
    token = ScimToken(
        name=body.name.strip(),
        token_hash=hash_token(raw),
        token_prefix=raw[:12],
        created_by=admin.id,
    )
    db.add(token)
    await db.flush()
    await log_audit(
        db,
        user=admin,
        action="create",
        entity_type="scim_token",
        entity_id=token.id,
        entity_name=token.name,
        summary=f"Generated SCIM token '{token.name}'",
        request=request,
    )
    await db.commit()
    return {**_token_dict(token), "token": raw, "endpoint": _base(request)}


@tokens_router.delete("/{token_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_scim_token(
    token_id: str, request: Request, admin: User = Depends(require_admin), db: AsyncSession = Depends(get_db)
) -> Response:
    tid = parse_path_id(token_id, not_found_detail="SCIM token not found")
    token = await db.get(ScimToken, tid)
    if token is None:
        raise HTTPException(status_code=404, detail="SCIM token not found")
    if token.revoked_at is None:
        token.revoked_at = datetime.now(UTC)
        await log_audit(
            db,
            user=admin,
            action="delete",
            entity_type="scim_token",
            entity_id=token.id,
            entity_name=token.name,
            summary=f"Revoked SCIM token '{token.name}'",
            request=request,
        )
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
