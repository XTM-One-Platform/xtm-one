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

"""Relay API - relay groups, enrollment, and the relay-facing transport.

Two audiences share this router:

* **Users / admins** manage relay groups (three-tier visibility; admins
  create them, sharing decides who may route a resource through them), their
  enrollment keys and relays, read the request log, and test a destination
  from the customer's network - JWT-authenticated.
* **The relay daemon** (``apps/relay``) enrolls once (``rlenr-`` key ->
  ``rly-`` key), then holds a WebSocket (``/connect``) or long-polls
  (``/hello``, ``/poll``, ``/frames``, ``/heartbeat``) - ``rly-`` credential
  authenticated. ``/update-manifest`` and ``/dist/{asset}`` serve the builds
  embedded in this image.

Everything is gated on :func:`app.services.relays.relays_active`.
"""

from __future__ import annotations

import contextlib
import logging
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import NoReturn
from urllib.parse import urlsplit

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Query,
    Request,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field
from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.requests import ClientDisconnect

from app.api.deps import (
    credential_transport_route,
    get_current_relay,
    get_current_user,
    refuse_insecure_credential_transport,
)
from app.api.v1.helpers import (
    ENTITY_LIST_SCOPE_DESCRIPTION,
    EntityListScope,
    apply_group_share_fields,
    apply_user_share_fields,
    can_manage,
    get_user_group_ids,
    handle_company_managed_toggle,
    list_scope_admin_bypass,
    parse_path_id,
    user_visible_filter,
    user_visible_query,
    wants_access_restriction,
)
from app.core.config import settings
from app.core.database import get_db, release_db_connection_before_loop
from app.core.utils import external_base
from app.models.relay import (
    RELAY_CAPABILITIES,
    RELAY_PLATFORMS,
    RELAY_PROTOCOL_VERSION,
    RELAY_RESERVED_CAPABILITIES,
    RELAY_SOURCE_TYPES,
    Relay,
    RelayEnrollmentKey,
    RelayGroup,
    RelayRequestLog,
)
from app.models.user import User
from app.services import relay_transport
from app.services.audit import log_audit
from app.services.relays import (
    DestinationPatternError,
    RoutedResources,
    apply_relay_reenrollment,
    enrollment_key_usable,
    generate_relay_enrollment_key,
    generate_relay_key,
    group_relay_counts,
    group_request_stats,
    hash_key,
    key_display_prefix,
    normalize_destination_patterns,
    relay_version_below,
    relays_active,
    resolve_relay_credential,
    resolve_relay_enrollment_key,
    routed_resource_counts,
    routed_resources,
    serialize_enrollment_key,
    serialize_group,
    serialize_relay,
    serialize_request_log,
    unique_group_slug,
)

logger = logging.getLogger(__name__)

router = APIRouter()

_GROUP_NOT_FOUND = "Relay group not found"
_RELAY_NOT_FOUND = "Relay not found"
_KEY_NOT_FOUND = "Enrollment key not found"
_CANNOT_MANAGE = "You cannot manage this relay group"
_INVALID_ENROLLMENT_KEY = "Invalid or expired enrollment key"


def _require_enabled() -> None:
    if not relays_active():
        raise HTTPException(status_code=404, detail="Relays are disabled")


# ── Schemas ─────────────────────────────────────────────────────────────────


class RelayGroupCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    zone: str | None = Field(default=None, max_length=200)
    description: str | None = Field(default=None, max_length=4000)
    # NULL = no platform-side restriction (each relay's local list decides).
    allowed_destinations: list[str] | None = None
    tags: list[str] = Field(default_factory=list)
    is_company_managed: bool = False
    shared_group_ids: list[str] | None = None
    shared_group_permissions: dict | None = None
    shared_user_ids: list[str] | None = None
    shared_user_permissions: dict[str, str] | None = None
    access_restricted: bool = False


class RelayGroupUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    zone: str | None = Field(default=None, max_length=200)
    description: str | None = Field(default=None, max_length=4000)
    allowed_destinations: list[str] | None = None
    tags: list[str] | None = None
    is_company_managed: bool | None = None
    shared_group_ids: list[str] | None = None
    shared_group_permissions: dict | None = None
    shared_user_ids: list[str] | None = None
    shared_user_permissions: dict[str, str] | None = None
    access_restricted: bool | None = None


class EnrollmentKeyCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    # NULL = reusable (a container image rolled out to many replicas);
    # 1 = single-use.
    max_uses: int | None = Field(default=1, ge=1)
    ttl_seconds: int | None = Field(default=None, ge=0)


class EnrollRequest(BaseModel):
    enrollment_key: str
    platform: str
    hostname: str | None = Field(default=None, max_length=255)
    name: str | None = Field(default=None, max_length=200)
    host_facts: dict | None = None
    agent_version: str | None = Field(default=None, max_length=40)
    # Salted SHA-256 of the OS machine id (or of a container's persistent
    # volume id): the re-enrollment convergence key.
    machine_id: str | None = Field(default=None, max_length=128)


class RelayUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)


class TestDestinationRequest(BaseModel):
    url: str = Field(min_length=1, max_length=2000)
    # Test from this relay of the group rather than whichever claims it.
    relay_id: str | None = None


class HeartbeatRequest(BaseModel):
    agent_version: str | None = Field(default=None, max_length=40)


# ── Lookups ─────────────────────────────────────────────────────────────────


async def _get_visible_group(
    group_id: str, user: User, db: AsyncSession
) -> tuple[RelayGroup, list[str]]:
    """A group by id as the caller sees it, else the unknown-id 404.

    Keeps the admin bypass, like every other by-id route: only the library
    list is three-tier for admins.
    """
    gid = parse_path_id(group_id, not_found_detail=_GROUP_NOT_FOUND)
    group_ids = await get_user_group_ids(db, user.id)
    group = (
        await db.execute(
            user_visible_query(RelayGroup, user.id, group_ids, is_admin=user.is_admin).where(
                RelayGroup.id == gid
            )
        )
    ).scalar_one_or_none()
    if group is None:
        raise HTTPException(status_code=404, detail=_GROUP_NOT_FOUND)
    return group, group_ids


async def _get_manageable_group(
    group_id: str, user: User, db: AsyncSession
) -> tuple[RelayGroup, list[str]]:
    group, group_ids = await _get_visible_group(group_id, user, db)
    if not can_manage(user, group, group_ids):
        raise HTTPException(status_code=403, detail=_CANNOT_MANAGE)
    return group, group_ids


async def _get_visible_relay(
    relay_id: str, user: User, db: AsyncSession
) -> tuple[Relay, RelayGroup, list[str]]:
    """A relay as the caller sees it: through its group's visibility."""
    rid = parse_path_id(relay_id, not_found_detail=_RELAY_NOT_FOUND)
    relay = await db.get(Relay, rid)
    if relay is None:
        raise HTTPException(status_code=404, detail=_RELAY_NOT_FOUND)
    try:
        group, group_ids = await _get_visible_group(str(relay.group_id), user, db)
    except HTTPException as exc:
        if exc.status_code == 404:
            raise HTTPException(status_code=404, detail=_RELAY_NOT_FOUND) from None
        raise
    return relay, group, group_ids


def _patterns_or_422(values: list[str] | None) -> list[str] | None:
    try:
        return normalize_destination_patterns(values)
    except DestinationPatternError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None


async def _visible_routed(
    db: AsyncSession, user: User, group_ids: list[str], routed: RoutedResources
) -> dict:
    """The routed resources the reader may see by name, the rest as a count."""
    from app.models.flow import Flow
    from app.models.integration import Integration
    from app.models.mcp_server import MCPServer

    out: dict = {"hidden": 0}
    for key, model, rows in (
        ("integrations", Integration, routed.integrations),
        ("mcp_servers", MCPServer, routed.mcp_servers),
        ("flows", Flow, routed.flows),
    ):
        ids = [uuid.UUID(row["id"]) for row in rows]
        visible: set[str] = set()
        if ids:
            result = await db.execute(
                select(model.id).where(
                    model.id.in_(ids),
                    user_visible_filter(model, user.id, group_ids, is_admin=user.is_admin),
                )
            )
            visible = {str(v) for v in result.scalars().all()}
        out[key] = [
            {k: v for k, v in row.items() if k != "user_id"}
            for row in rows
            if row["id"] in visible
        ]
        out["hidden"] += len(rows) - len(out[key])
    out["total"] = routed.total
    return out


# ── Static vocabulary ───────────────────────────────────────────────────────


@router.get("/meta")
async def relay_meta(request: Request, user: User = Depends(get_current_user)):
    """What the UI needs to explain and enroll relays."""
    _require_enabled()
    return {
        "platforms": list(RELAY_PLATFORMS),
        "capabilities": list(RELAY_CAPABILITIES),
        "reserved_capabilities": list(RELAY_RESERVED_CAPABILITIES),
        "source_types": list(RELAY_SOURCE_TYPES),
        "protocol_version": RELAY_PROTOCOL_VERSION,
        "min_supported_version": settings.relay_min_supported_version or None,
        "relay_version": _dist_version() or None,
        "server_url": external_base(request),
        "install_scripts": _scripts_table(external_base(request)),
        "can_create_groups": bool(user.is_admin),
    }


# ── Relay groups (users / admins) ───────────────────────────────────────────


@router.get("/groups")
async def list_relay_groups(
    scope: EntityListScope = Query("visible", description=ENTITY_LIST_SCOPE_DESCRIPTION),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    _require_enabled()
    group_ids = await get_user_group_ids(db, user.id)
    groups = (
        (
            await db.execute(
                user_visible_query(
                    RelayGroup,
                    user.id,
                    group_ids,
                    is_admin=list_scope_admin_bypass(scope, user),
                ).order_by(RelayGroup.name)
            )
        )
        .scalars()
        .all()
    )
    ids = [g.id for g in groups]
    counts = await group_relay_counts(db, ids)
    routed = await routed_resource_counts(db, ids)
    return [
        serialize_group(
            g,
            can_manage=can_manage(user, g, group_ids),
            online=counts.get(g.id, (0, 0))[0],
            total=counts.get(g.id, (0, 0))[1],
            routed=routed.get(g.id, 0),
        )
        for g in groups
    ]


@router.post("/groups", status_code=status.HTTP_201_CREATED)
async def create_relay_group(
    body: RelayGroupCreate,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Create a relay group (admins only: a group names a customer network)."""
    _require_enabled()
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Only admins can create relay groups")
    name = body.name.strip()
    group = RelayGroup(
        user_id=None if body.is_company_managed else user.id,
        name=name,
        slug=await unique_group_slug(db, name),
        zone=(body.zone or "").strip() or None,
        description=(body.description or "").strip() or None,
        allowed_destinations=_patterns_or_422(body.allowed_destinations),
        tags=body.tags,
        is_company_managed=body.is_company_managed,
    )
    await apply_group_share_fields(db, group, body, partial=False)
    db.add(group)
    await apply_user_share_fields(db, group, body, partial=False)
    await db.flush()
    await log_audit(
        db,
        user=user,
        action="create",
        entity_type="relay_group",
        entity_id=group.id,
        entity_name=group.name,
        summary=f"Created relay group '{group.name}'",
        request=request,
    )
    await db.commit()
    await db.refresh(group)
    return serialize_group(group, can_manage=True)


@router.get("/groups/{group_id}")
async def get_relay_group(
    group_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    _require_enabled()
    group, group_ids = await _get_visible_group(group_id, user, db)
    online, total = (await group_relay_counts(db, [group.id])).get(group.id, (0, 0))
    routed = (await routed_resource_counts(db, [group.id])).get(group.id, 0)
    return serialize_group(
        group,
        can_manage=can_manage(user, group, group_ids),
        online=online,
        total=total,
        routed=routed,
    )


@router.patch("/groups/{group_id}")
async def update_relay_group(
    group_id: str,
    body: RelayGroupUpdate,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    _require_enabled()
    group, _group_ids = await _get_manageable_group(group_id, user, db)
    fields = body.model_fields_set
    if body.name is not None and body.name.strip() != group.name:
        group.name = body.name.strip()
        group.slug = await unique_group_slug(db, group.name, exclude_id=group.id)
    if "zone" in fields:
        group.zone = (body.zone or "").strip() or None
    if "description" in fields:
        group.description = (body.description or "").strip() or None
    if "allowed_destinations" in fields:
        group.allowed_destinations = _patterns_or_422(body.allowed_destinations)
    if body.tags is not None:
        group.tags = body.tags
    # A relay group is bound by integrations, MCP servers and flow nodes - not
    # by an agent list - so there is no agent reference to sweep.
    await handle_company_managed_toggle(
        db,
        user,
        group,
        body.is_company_managed,
        agent_field=None,
        keep_grants=wants_access_restriction(body, group),
    )
    if not group.is_company_managed:
        await apply_group_share_fields(
            db, group, body, partial=True, null_keeps_stored=True
        )
    await apply_user_share_fields(db, group, body, partial=True)
    await log_audit(
        db,
        user=user,
        action="update",
        entity_type="relay_group",
        entity_id=group.id,
        entity_name=group.name,
        summary=f"Updated relay group '{group.name}'",
        details={"fields": sorted(fields)},
        request=request,
    )
    await db.commit()
    await db.refresh(group)
    online, total = (await group_relay_counts(db, [group.id])).get(group.id, (0, 0))
    return serialize_group(group, can_manage=True, online=online, total=total)


@router.delete("/groups/{group_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_relay_group(
    group_id: str,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Delete a group and its relays; refused while anything routes through it.

    Silently sending a routed resource direct would be fail-open: its
    requests would leave from the platform's own network instead.
    """
    _require_enabled()
    group, group_ids = await _get_manageable_group(group_id, user, db)
    await db.refresh(group, with_for_update=True)
    routed = await routed_resources(db, group.id)
    if routed.total:
        raise HTTPException(
            status_code=409,
            detail={
                "message": (
                    f"{routed.total} resource(s) still route through this relay group. "
                    "Route them directly or through another group first."
                ),
                "routed": await _visible_routed(db, user, group_ids, routed),
            },
        )
    relay_ids = list(
        (await db.execute(select(Relay.id).where(Relay.group_id == group.id))).scalars().all()
    )
    await log_audit(
        db,
        user=user,
        action="delete",
        entity_type="relay_group",
        entity_id=group.id,
        entity_name=group.name,
        summary=f"Deleted relay group '{group.name}' ({len(relay_ids)} relay(s))",
        request=request,
    )
    await db.delete(group)
    await db.commit()
    for rid in relay_ids:
        await relay_transport.signal_relay_revoked(rid)


@router.get("/groups/{group_id}/routed")
async def list_routed_resources(
    group_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """What routes through the group: by name what the reader sees, else a count."""
    _require_enabled()
    group, group_ids = await _get_visible_group(group_id, user, db)
    return await _visible_routed(db, user, group_ids, await routed_resources(db, group.id))


@router.get("/groups/{group_id}/stats")
async def relay_group_stats(
    group_id: str,
    hours: int = Query(24, ge=1, le=24 * 30),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    _require_enabled()
    group, _ = await _get_visible_group(group_id, user, db)
    return await group_request_stats(db, group.id, hours=hours)


@router.get("/groups/{group_id}/requests")
async def list_relay_requests(
    group_id: str,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    relay_id: list[str] | None = Query(
        None, max_length=50, description="Repeat for several relays of the group (any of them)"
    ),
    outcome: str | None = Query(None, pattern="^(ok|error)$"),
    host: str | None = Query(None, max_length=255),
    source_type: str | None = Query(None),
    direction: str = Query("desc", pattern="^(asc|desc)$"),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """The request log of a group: metadata only, never a header or a body.

    Ordered by date, newest first unless ``direction`` is ``asc``.
    """
    _require_enabled()
    group, _ = await _get_visible_group(group_id, user, db)
    clauses = [RelayRequestLog.group_id == group.id]
    if relay_id:
        rids = [parse_path_id(value, not_found_detail=_RELAY_NOT_FOUND) for value in relay_id]
        clauses.append(RelayRequestLog.relay_id.in_(rids))
    if outcome == "error":
        clauses.append(
            or_(RelayRequestLog.error_code.is_not(None), RelayRequestLog.status_code >= 500)
        )
    elif outcome == "ok":
        clauses.append(
            and_(
                RelayRequestLog.error_code.is_(None),
                or_(RelayRequestLog.status_code.is_(None), RelayRequestLog.status_code < 500),
            )
        )
    if host:
        clauses.append(RelayRequestLog.host.ilike(f"%{host.strip()}%"))
    if source_type:
        if source_type not in RELAY_SOURCE_TYPES:
            raise HTTPException(status_code=422, detail="Unknown source_type")
        clauses.append(RelayRequestLog.source_type == source_type)
    total = int(
        await db.scalar(select(func.count(RelayRequestLog.id)).where(*clauses)) or 0
    )
    rows = await db.execute(
        select(RelayRequestLog)
        .where(*clauses)
        .order_by(
            RelayRequestLog.created_at.asc()
            if direction == "asc"
            else RelayRequestLog.created_at.desc(),
            RelayRequestLog.id.asc() if direction == "asc" else RelayRequestLog.id.desc(),
        )
        .limit(limit)
        .offset(offset)
    )
    return {
        "items": [serialize_request_log(r) for r in rows.scalars().all()],
        "total": total,
    }


@router.post("/groups/{group_id}/test")
async def test_relay_destination(
    group_id: str,
    body: TestDestinationRequest,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Send one ``GET`` to *url* from the customer's network and say what happened."""
    _require_enabled()
    group, _ = await _get_manageable_group(group_id, user, db)
    relay: Relay | None = None
    if body.relay_id:
        rid = parse_path_id(body.relay_id, not_found_detail=_RELAY_NOT_FOUND)
        relay = await db.get(Relay, rid)
        if relay is None or relay.group_id != group.id or not relay.is_active:
            raise HTTPException(status_code=404, detail=_RELAY_NOT_FOUND)
    url = body.url.strip()
    host = urlsplit(url).hostname
    await log_audit(
        db,
        user=user,
        action="trigger",
        entity_type="relay_group",
        entity_id=group.id,
        entity_name=group.name,
        summary=f"Tested destination '{host or '?'}' through relay group '{group.name}'",
        details={"host": host, "relay_id": str(relay.id) if relay else None},
        request=request,
    )
    group_uuid, group_name, allowed = group.id, group.name, group.allowed_destinations
    target = relay.id if relay else None
    await db.commit()
    await release_db_connection_before_loop(db)
    return await relay_transport.test_destination(
        group_uuid,
        url,
        group_name=group_name,
        allowed_destinations=allowed,
        relay_id=target,
    )


# ── Enrollment keys ─────────────────────────────────────────────────────────


@router.post("/groups/{group_id}/enrollment-keys", status_code=status.HTTP_201_CREATED)
async def create_relay_enrollment_key(
    group_id: str,
    body: EnrollmentKeyCreate,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Create an enrollment key for the group. The raw ``rlenr-`` secret is returned once."""
    _require_enabled()
    group, _ = await _get_manageable_group(group_id, user, db)
    raw_key = generate_relay_enrollment_key()
    ttl = body.ttl_seconds
    if ttl is None and body.max_uses == 1:
        ttl = settings.relay_enrollment_ttl_seconds
    key = RelayEnrollmentKey(
        group_id=group.id,
        user_id=user.id,
        name=body.name.strip(),
        key_prefix=key_display_prefix(raw_key),
        key_hash=await hash_key(raw_key),
        max_uses=body.max_uses,
        expires_at=datetime.now(UTC) + timedelta(seconds=ttl) if ttl else None,
    )
    db.add(key)
    await db.flush()
    await log_audit(
        db,
        user=user,
        action="create",
        entity_type="relay_enrollment_key",
        entity_id=key.id,
        entity_name=key.name,
        summary=f"Created an enrollment key '{key.name}' for relay group '{group.name}'",
        request=request,
    )
    await db.commit()
    return {
        **serialize_enrollment_key(key),
        "key": raw_key,
        "group_slug": group.slug,
        "server_url": external_base(request),
    }


@router.get("/groups/{group_id}/enrollment-keys")
async def list_relay_enrollment_keys(
    group_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    _require_enabled()
    group, _ = await _get_manageable_group(group_id, user, db)
    rows = await db.execute(
        select(RelayEnrollmentKey)
        .where(RelayEnrollmentKey.group_id == group.id)
        .order_by(RelayEnrollmentKey.created_at.desc())
    )
    return [serialize_enrollment_key(k) for k in rows.scalars().all()]


@router.delete(
    "/enrollment-keys/{key_id}", status_code=status.HTTP_204_NO_CONTENT
)
async def delete_relay_enrollment_key(
    key_id: str,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    _require_enabled()
    kid = parse_path_id(key_id, not_found_detail=_KEY_NOT_FOUND)
    key = await db.get(RelayEnrollmentKey, kid)
    if key is None:
        raise HTTPException(status_code=404, detail=_KEY_NOT_FOUND)
    try:
        group, _ = await _get_manageable_group(str(key.group_id), user, db)
    except HTTPException as exc:
        if exc.status_code == 404:
            raise HTTPException(status_code=404, detail=_KEY_NOT_FOUND) from None
        raise
    await log_audit(
        db,
        user=user,
        action="delete",
        entity_type="relay_enrollment_key",
        entity_id=key.id,
        entity_name=key.name,
        summary=f"Revoked the enrollment key '{key.name}' of relay group '{group.name}'",
        request=request,
    )
    await db.delete(key)
    await db.commit()


# ── Relays of a group ───────────────────────────────────────────────────────


@router.get("/groups/{group_id}/relays")
async def list_group_relays(
    group_id: str,
    include_revoked: bool = Query(False),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    _require_enabled()
    group, _ = await _get_visible_group(group_id, user, db)
    stmt = select(Relay).where(Relay.group_id == group.id)
    if not include_revoked:
        stmt = stmt.where(Relay.is_active.is_(True))
    relays = (await db.execute(stmt.order_by(Relay.name))).scalars().all()
    now = datetime.now(UTC)
    return [
        serialize_relay(r, now=now, inflight=await relay_transport.relay_inflight_count(r.id))
        for r in relays
    ]


# ── Enrollment (relay daemon, ``rlenr-`` credential) ────────────────────────


async def enroll_relay(body: EnrollRequest, db: AsyncSession = Depends(get_db)):
    """Exchange an ``rlenr-`` enrollment key for a durable ``rly-`` credential.

    One row per MACHINE in the key's group: a relay reporting a ``machine_id``
    that matches an active relay of the same group converges on that row
    (credential rotated, facts refreshed, name kept) instead of duplicating
    it. The raw ``rly-`` key is returned exactly once.
    """
    _require_enabled()
    if body.platform not in RELAY_PLATFORMS:
        raise HTTPException(status_code=422, detail="Invalid platform")
    key = await resolve_relay_enrollment_key(body.enrollment_key, db)
    if key is None:
        raise HTTPException(status_code=401, detail=_INVALID_ENROLLMENT_KEY)
    # Every enrollment into the group waits on the group row, whatever key it
    # holds: the machine match and the relay cap below read the whole group.
    # Then the key row, re-checked, so a single-use key passes once. Group
    # before key: the order deleting a group takes (its keys cascade).
    await db.execute(
        select(RelayGroup.id).where(RelayGroup.id == key.group_id).with_for_update()
    )
    await db.refresh(key, with_for_update=True)
    if not enrollment_key_usable(key):
        raise HTTPException(status_code=401, detail=_INVALID_ENROLLMENT_KEY)

    machine_id = (body.machine_id or "").strip() or None
    existing: Relay | None = None
    if machine_id:
        # Never matched on hostname: cloned VMs share one, and a false merge
        # would invalidate another machine's live credential. Revoked rows
        # never match - a revoked machine comes back as a new identity.
        existing = (
            (
                await db.execute(
                    select(Relay)
                    .where(
                        Relay.group_id == key.group_id,
                        Relay.machine_id == machine_id,
                        Relay.platform == body.platform,
                        Relay.is_active.is_(True),
                    )
                    .order_by(Relay.created_at.asc())
                    .limit(1)
                    .with_for_update()
                )
            )
            .scalars()
            .first()
        )

    if existing is None and settings.relay_max_per_group:
        active = await db.scalar(
            select(func.count(Relay.id)).where(
                Relay.group_id == key.group_id, Relay.is_active.is_(True)
            )
        )
        if active is not None and active >= settings.relay_max_per_group:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Relay limit reached for this group ({settings.relay_max_per_group}). "
                    "Revoke an unused relay or raise RELAY_MAX_PER_GROUP."
                ),
            )

    raw_relay_key = generate_relay_key()
    now = datetime.now(UTC)
    if existing is not None:
        relay = existing
        apply_relay_reenrollment(
            relay,
            key_prefix=key_display_prefix(raw_relay_key),
            key_hash=await hash_key(raw_relay_key),
            hostname=body.hostname,
            host_facts=body.host_facts,
            agent_version=body.agent_version,
            now=now,
        )
    else:
        relay = Relay(
            group_id=key.group_id,
            enrolled_by_user_id=key.user_id,
            name=(body.name or body.hostname or "Relay").strip()[:200] or "Relay",
            platform=body.platform,
            hostname=body.hostname,
            machine_id=machine_id,
            key_prefix=key_display_prefix(raw_relay_key),
            key_hash=await hash_key(raw_relay_key),
            host_facts=body.host_facts,
            agent_version=body.agent_version,
            status="offline",
            enrolled_at=now,
        )
        db.add(relay)

    key.use_count += 1
    key.last_used_at = now
    if key.max_uses is not None and key.use_count >= key.max_uses:
        key.is_active = False

    await db.flush()
    owner = await db.get(User, key.user_id) if key.user_id else None
    if owner is not None:
        verb = "re-enrolled" if existing is not None else "enrolled"
        await log_audit(
            db,
            user=owner,
            action="update" if existing is not None else "create",
            entity_type="relay",
            entity_id=relay.id,
            entity_name=relay.name,
            summary=f"Relay '{relay.name}' {verb} ({relay.platform})",
            details={"group_id": str(key.group_id), "enrollment_key_id": str(key.id)},
        )
    group = await db.get(RelayGroup, key.group_id)
    await db.commit()
    return {
        "relay": serialize_relay(relay, now=now),
        "key": raw_relay_key,
        "group": {
            "id": str(key.group_id),
            "name": group.name if group else None,
            "slug": group.slug if group else None,
        },
        "protocol_version": RELAY_PROTOCOL_VERSION,
        "heartbeat_interval_seconds": settings.relay_heartbeat_interval_seconds,
        "long_poll_timeout_seconds": settings.relay_long_poll_timeout_seconds,
    }


router.add_api_route(
    "/enroll",
    enroll_relay,
    methods=["POST"],
    status_code=status.HTTP_201_CREATED,
    route_class_override=credential_transport_route(refuse_insecure_credential_transport),
)


# ── Transport: relay daemon (``rly-`` credential) ───────────────────────────


def _update_required(version: str | None) -> dict | None:
    floor = settings.relay_min_supported_version
    if relay_version_below(version, floor):
        return {"type": "update_required", "min_version": floor, "current_version": version}
    return None


@router.websocket("/connect")
async def relay_websocket(websocket: WebSocket):
    """The fast path: the relay holds this socket, opens are pushed to it.

    The ``rly-`` credential rides in the ``Authorization: Bearer`` header
    (query strings end up in proxy logs), with the ``token`` query parameter
    as a fallback for clients that cannot set a header on an upgrade.
    """
    if not relays_active():
        await websocket.close(code=4404)
        return
    token = ""
    auth_header = websocket.headers.get("authorization", "")
    if auth_header[:7].lower() == "bearer ":
        token = auth_header[7:].strip()
    if not token:
        token = websocket.query_params.get("token", "")
    reported_version = websocket.query_params.get("version")

    from app.core.database import async_session

    async with async_session() as db:
        relay = await resolve_relay_credential(token, db)
    if relay is None:
        await websocket.close(code=4401)
        return

    await websocket.accept()
    # 4426 mirrors HTTP 426: the relay runs its updater instead of
    # reconnect-storming with a protocol the platform no longer speaks.
    update = _update_required(reported_version)
    if update is not None:
        with contextlib.suppress(Exception):
            await websocket.send_bytes(relay_transport.encode_frame(update))
        await websocket.close(code=4426)
        return
    try:
        await relay_transport.serve_relay_websocket(websocket, relay)
    except WebSocketDisconnect:
        pass
    except Exception:
        logger.debug("Relay WS error", exc_info=True)


@router.post("/hello")
async def relay_hello(
    body: dict,
    version: str | None = Query(None),
    relay: Relay = Depends(get_current_relay),
):
    """The long-poll transport's hello: the same contract as the WS first frame."""
    _require_enabled()
    update = _update_required(version or body.get("version"))
    if update is not None:
        raise HTTPException(status_code=426, detail=update)
    refusal = relay_transport.check_hello(body)
    if refusal:
        raise HTTPException(status_code=400, detail=refusal)
    if not await relay_transport.touch_relay(relay.id, transport="long_poll", hello=body):
        raise HTTPException(status_code=401, detail="Invalid or revoked relay credential")
    await relay_transport.touch_presence(
        relay.group_id, relay_transport.poll_presence_member(relay.id)
    )
    return relay_transport.hello_ack(relay)


@router.get("/poll")
async def relay_poll(
    free: int = Query(1, ge=0, le=1024),
    wait: bool = Query(True),
    version: str | None = Query(None),
    relay: Relay = Depends(get_current_relay),
    db: AsyncSession = Depends(get_db),
):
    """One long poll: opens to start and frames for the requests the relay holds.

    The body is concatenated frames (``application/octet-stream``), empty when
    the poll timed out with nothing to hand over.
    """
    _require_enabled()
    update = _update_required(version)
    if update is not None:
        return Response(
            content=relay_transport.encode_frame(update),
            media_type="application/octet-stream",
        )
    if not await relay_transport.touch_relay(relay.id, transport="long_poll"):
        raise HTTPException(status_code=401, detail="Invalid or revoked relay credential")
    await release_db_connection_before_loop(db)
    frames = await relay_transport.serve_relay_poll(relay, free=free, wait=wait)
    return Response(
        content=relay_transport.encode_frames(frames),
        media_type="application/octet-stream",
    )


@router.post("/frames")
async def relay_upload_frames(
    request: Request,
    relay: Relay = Depends(get_current_relay),
    db: AsyncSession = Depends(get_db),
):
    """Response frames from a long-polling relay (concatenated, each with ``len``)."""
    _require_enabled()
    await release_db_connection_before_loop(db)
    limit = relay_transport.MAX_UPLOAD_BODY_BYTES
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > limit:
        raise HTTPException(status_code=413, detail="Frame body too large")
    body = bytearray()
    try:
        async for chunk in request.stream():
            body += chunk
            if len(body) > limit:
                raise HTTPException(status_code=413, detail="Frame body too large")
    except ClientDisconnect:
        raise HTTPException(status_code=400, detail="Upload interrupted") from None
    try:
        frames = relay_transport.decode_frames(bytes(body))
    except relay_transport.FrameError as exc:
        raise HTTPException(status_code=400, detail=f"Malformed frames: {exc}") from None
    accepted = await relay_transport.accept_relay_upload(relay, frames)
    return {"accepted": accepted, "received": len(frames)}


@router.post("/heartbeat")
async def relay_heartbeat(
    body: HeartbeatRequest,
    relay: Relay = Depends(get_current_relay),
):
    """Long-poll keepalive while requests are in flight."""
    _require_enabled()
    if not await relay_transport.touch_relay(relay.id, transport="long_poll"):
        raise HTTPException(status_code=401, detail="Invalid or revoked relay credential")
    await relay_transport.touch_presence(
        relay.group_id, relay_transport.poll_presence_member(relay.id)
    )
    update = _update_required(body.agent_version)
    if update is not None:
        return {"ok": True, "update_required": True, "min_version": update["min_version"]}
    return {"ok": True}


# ── Downloads + self-update (builds embedded in this image) ─────────────────
#
# The release workflow (XTM-One-Platform/release) builds ``xtm-one-relay``
# for every OS and architecture and embeds the archives (+ ``.sha256`` /
# ``.minisig``), the install scripts and a ``VERSION`` marker under
# ``settings.relay_dist_dir``. The platform image is the single distribution
# channel: every deployment serves exactly the relay build it shipped with.

_RELAY_ARCHES = ("x64", "arm64")
_RELAY_ARCHIVES = {
    ("windows", "x64"): "xtm-one-relay-windows-x64.zip",
    ("windows", "arm64"): "xtm-one-relay-windows-arm64.zip",
    ("macos", "x64"): "xtm-one-relay-macos-universal.tar.gz",
    ("macos", "arm64"): "xtm-one-relay-macos-universal.tar.gz",
    ("linux", "x64"): "xtm-one-relay-linux-x64.tar.gz",
    ("linux", "arm64"): "xtm-one-relay-linux-arm64.tar.gz",
}
_RELAY_INSTALL_SCRIPTS = (
    ("sh", "xtm-one-relay-installer.sh"),
    ("ps1", "xtm-one-relay-installer.ps1"),
)
_NO_DIST_DETAIL = (
    "This deployment was built without embedded relay artifacts "
    "(source/dev build); release images ship them automatically."
)


def _dist_assets() -> frozenset[str]:
    names: set[str] = {name for _, name in _RELAY_INSTALL_SCRIPTS}
    for archive in _RELAY_ARCHIVES.values():
        names.update({archive, archive + ".sha256", archive + ".minisig"})
    return frozenset(names)


_DIST_ASSETS = _dist_assets()


def _dist_file(name: str) -> Path | None:
    d = Path(settings.relay_dist_dir)
    if not d.is_dir():
        return None
    f = d / name
    try:
        if f.is_file() and f.stat().st_size > 0:
            return f
    except OSError:
        return None
    return None


def _dist_version() -> str:
    f = _dist_file("VERSION")
    if f is None:
        return ""
    try:
        return f.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _dist_url(base: str, name: str) -> str:
    return f"{base}/api/v1/relays/dist/{name}"


def _archive_entry(base: str, archive: str) -> dict | None:
    if _dist_file(archive) is None:
        return None
    entry: dict = {"url": _dist_url(base, archive)}
    if _dist_file(archive + ".sha256") is not None:
        entry["sha256_url"] = _dist_url(base, archive + ".sha256")
    if _dist_file(archive + ".minisig") is not None:
        entry["signature_url"] = _dist_url(base, archive + ".minisig")
    return entry


def _artifacts_table(base: str) -> dict[str, dict[str, dict]]:
    table: dict[str, dict[str, dict]] = {}
    for (os_key, arch_key), archive in _RELAY_ARCHIVES.items():
        entry = _archive_entry(base, archive)
        if entry is not None:
            table.setdefault(os_key, {})[arch_key] = entry
    return table


def _scripts_table(base: str) -> dict[str, str | None]:
    return {
        key: (_dist_url(base, name) if _dist_file(name) is not None else None)
        for key, name in _RELAY_INSTALL_SCRIPTS
    }


@router.get("/update-manifest")
async def relay_update_manifest(
    request: Request,
    os_name: str | None = Query(None, alias="os"),
    arch: str | None = Query(None),
    current_version: str | None = Query(None),
):
    """Archives + self-update decision for the embedded relay build.

    An update is only offered when the caller is provably older AND the
    archive carries both its checksum and its minisign signature: the relay
    refuses unsigned code.
    """
    _require_enabled()
    base = external_base(request)
    dist_version = _dist_version()
    artifacts = _artifacts_table(base)
    out: dict = {
        "version": settings.app_version,
        "relay_version": dist_version or None,
        "min_supported_version": settings.relay_min_supported_version or None,
        "artifacts": artifacts,
        "install_scripts": _scripts_table(base),
        "download_configured": bool(artifacts),
    }
    if os_name and arch:
        if arch not in _RELAY_ARCHES:
            raise HTTPException(status_code=400, detail=f"Unknown architecture '{arch}'")
        entry = artifacts.get(os_name, {}).get(arch, {})
        out["target"] = {
            "version": dist_version or None,
            "url": entry.get("url"),
            "sha256_url": entry.get("sha256_url"),
            "signature_url": entry.get("signature_url"),
            "update_available": bool(
                entry.get("url")
                and entry.get("sha256_url")
                and entry.get("signature_url")
                and dist_version
                and relay_version_below(current_version, dist_version)
            ),
            "needs_update": relay_version_below(
                current_version, settings.relay_min_supported_version
            ),
        }
    return out


def _not_embedded() -> NoReturn:
    raise HTTPException(status_code=404, detail=_NO_DIST_DETAIL)


@router.api_route("/dist/{asset}", methods=["GET", "HEAD"])
async def download_relay_asset(asset: str):
    """Serve one embedded relay artifact by its stable name.

    Matched against a fixed allow-list and never used as a filesystem path.
    Unauthenticated on purpose: archives, checksums, signatures and install
    scripts are public release artifacts.
    """
    _require_enabled()
    if asset not in _DIST_ASSETS:
        raise HTTPException(status_code=404, detail="Unknown artifact")
    f = _dist_file(asset)
    if f is None:
        _not_embedded()
    return FileResponse(f, filename=asset)


# ── One relay (dynamic routes last) ─────────────────────────────────────────


@router.get("/{relay_id}")
async def get_relay(
    relay_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    _require_enabled()
    relay, group, group_ids = await _get_visible_relay(relay_id, user, db)
    return {
        **serialize_relay(relay, inflight=await relay_transport.relay_inflight_count(relay.id)),
        "can_manage": can_manage(user, group, group_ids),
    }


@router.patch("/{relay_id}")
async def update_relay(
    relay_id: str,
    body: RelayUpdate,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    _require_enabled()
    relay, group, group_ids = await _get_visible_relay(relay_id, user, db)
    if not can_manage(user, group, group_ids):
        raise HTTPException(status_code=403, detail=_CANNOT_MANAGE)
    if body.name is not None:
        relay.name = body.name.strip()[:200]
    await log_audit(
        db,
        user=user,
        action="update",
        entity_type="relay",
        entity_id=relay.id,
        entity_name=relay.name,
        summary=f"Updated relay '{relay.name}'",
        request=request,
    )
    await db.commit()
    await db.refresh(relay)
    return {**serialize_relay(relay), "can_manage": True}


@router.post("/{relay_id}/revoke")
async def revoke_relay(
    relay_id: str,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Revoke a relay's credential now; its socket is closed after the commit."""
    _require_enabled()
    relay, group, group_ids = await _get_visible_relay(relay_id, user, db)
    if not can_manage(user, group, group_ids):
        raise HTTPException(status_code=403, detail=_CANNOT_MANAGE)
    await db.refresh(relay, with_for_update=True)
    relay.is_active = False
    relay.status = "revoked"
    await log_audit(
        db,
        user=user,
        action="update",
        entity_type="relay",
        entity_id=relay.id,
        entity_name=relay.name,
        summary=f"Revoked relay '{relay.name}' of group '{group.name}'",
        request=request,
    )
    await db.commit()
    await db.refresh(relay)
    await relay_transport.signal_relay_revoked(relay.id)
    return {**serialize_relay(relay), "can_manage": True}


@router.delete("/{relay_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_relay(
    relay_id: str,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    _require_enabled()
    relay, group, group_ids = await _get_visible_relay(relay_id, user, db)
    if not can_manage(user, group, group_ids):
        raise HTTPException(status_code=403, detail=_CANNOT_MANAGE)
    await log_audit(
        db,
        user=user,
        action="delete",
        entity_type="relay",
        entity_id=relay.id,
        entity_name=relay.name,
        summary=f"Deleted relay '{relay.name}' of group '{group.name}'",
        request=request,
    )
    rid = relay.id
    await db.delete(relay)
    await db.commit()
    await relay_transport.signal_relay_revoked(rid)
