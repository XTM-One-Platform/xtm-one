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

"""Runner API – enrollment, fleet management, and the pull-based transport.

Three audiences share this router:

* **Users / admins** manage runners and enrollment keys (three-tier
  visibility, per-runner permission model, revocation) — JWT-authenticated.
* **The native runner daemon** enrolls once (``renr-`` key -> ``run-`` key),
  then connects over a WebSocket (fast path) or HTTP long-poll (fallback),
  claims commands, and uploads results — ``run-`` credential authenticated.
* **Agents** never touch this router; they go through the ``computer_use``
  built-in tools, which call the transport service directly.

Everything is gated on ``settings.runners_enabled``.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import NoReturn

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
from fastapi.responses import FileResponse, RedirectResponse, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import defer
from starlette.datastructures import UploadFile
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.requests import ClientDisconnect

from app.api.deps import (
    credential_transport_route,
    get_current_runner,
    get_current_user,
    refuse_insecure_credential_transport,
)
from app.api.v1.helpers import (
    ENTITY_LIST_SCOPE_DESCRIPTION,
    EntityListScope,
    apply_group_share_fields,
    apply_user_share_fields,
    can_manage,
    can_manage_as,
    get_user_group_ids,
    handle_company_managed_toggle,
    list_scope_admin_bypass,
    parse_path_id,
    user_visible_query,
    wants_access_restriction,
)
from app.core.base_path import with_base_path
from app.core.bounded_receive import BoundedReceive
from app.core.config import settings
from app.core.database import get_db, release_db_connection_before_loop
from app.core.redaction import SECRET_MARKER_RE
from app.core.redis import redis_client
from app.core.utils import external_base, format_exception_message
from app.models.runner import (
    COMMAND_TERMINAL_STATUSES,
    RUNNER_CAPABILITIES,
    RUNNER_PERMISSION_MODES,
    RUNNER_PLATFORMS,
    RUNNER_RESULT_STATUSES,
    Runner,
    RunnerCommand,
    RunnerEnrollmentKey,
)
from app.models.user import User
from app.services import runner_transport
from app.services.audit import log_audit
from app.services.runners import (
    STOPPED_BY_KEY,
    apply_reenrollment,
    authorize_command,
    generate_enrollment_key,
    generate_runner_key,
    hash_key,
    is_runner_online,
    key_display_prefix,
    resolve_enrollment_key,
    resolve_runner_credential,
    runner_version_below,
    serialize_command,
    serialize_enrollment_key,
    serialize_runner,
)

logger = logging.getLogger(__name__)

router = APIRouter()

RUNNER_RESULT_SOURCE = "runner_capture"
# Companion JSON persisted next to every capture: SHA-256 + capture context
# (when / where / by which machine). This is the auditor-facing integrity
# sidecar — the proof that the PNG filed as evidence is the unmodified bytes
# the runner captured.
RUNNER_SIDECAR_SOURCE = "runner_capture_sidecar"

# Command kinds whose binary result is audit evidence and therefore gets the
# full integrity treatment (hash verification + metadata sidecar).
_CAPTURE_KINDS = ("screenshot", "browser_control")


def _require_enabled() -> None:
    if not settings.runners_enabled:
        raise HTTPException(status_code=404, detail="Runners are disabled")


# An admin reading the command history of a machine they neither own nor were
# granted is an audited oversight read: the rows carry shell output and
# inline file contents, so the trail must say WHO looked at WHOSE machine and
# when - the same standing the break-glass read of another user's
# conversation files has. The card behind the read polls every 15 seconds, so
# one entry per admin, per machine, per window (the length of the
# acknowledgement lease) is what keeps the trail readable instead of one row
# per poll - the marker that spaces them is written only once the row is
# committed (see ``_audit_runner_history_oversight``).
RUNNER_HISTORY_OVERSIGHT_AUDIT_WINDOW_SECONDS = 15 * 60
RUNNER_HISTORY_OVERSIGHT_ACTION = "break_glass"
RUNNER_HISTORY_OVERSIGHT_ENTITY_TYPE = "runner_commands"


def runner_history_oversight_key(admin_id: object, runner_id: object) -> str:
    """Redis marker for one admin's audited oversight window on one machine."""
    return f"runner:commands:oversight:{admin_id}:{runner_id}"


def is_admin_oversight_read(user: User, runner: Runner, group_ids: list[str]) -> bool:
    """True when *user* is admitted to the full history ONLY by the admin role.

    The owner, a ``config`` grantee (direct or through a group) and the
    administrators of a company-managed machine read what they were given;
    an admin on a colleague's PRIVATE machine reads through the by-id admin
    bypass ``_get_visible_runner`` deliberately keeps, which is the
    read worth a trail.
    """
    if not user.is_admin or runner.is_company_managed:
        return False
    return not can_manage_as(user.id, False, runner, group_ids)


async def _audit_runner_history_oversight(
    db: AsyncSession, user: User, runner: Runner, request: Request
) -> None:
    """Record an admin's oversight read of a colleague's command history.

    Throttled through a Redis marker so the polling card writes one entry per
    window. The marker is written only AFTER the audit row is committed: a
    marker set first would outlive a failed audit write and cover the retry
    that follows with no row behind it. Two first reads racing through the
    unmarked window may both audit - one row too many, never one too few -
    and a Redis failure audits anyway, for the same reason: a missing marker
    must never mean a missing record. Committed BEFORE the history is read,
    so the trail cannot be lost to a failure of the read it accounts for.
    """
    key = runner_history_oversight_key(user.id, runner.id)
    redis = None
    try:
        redis = await redis_client()
        if await redis.exists(key):
            return
    except Exception as exc:
        redis = None
        logger.warning(
            "Runner history oversight marker unavailable (auditing anyway) for "
            "admin=%s runner=%s: %s",
            user.id,
            runner.id,
            format_exception_message(exc),
        )

    owner_email: str | None = None
    if runner.user_id is not None:
        owner_result = await db.execute(
            select(User.email).where(User.id == runner.user_id)
        )
        owner_email = owner_result.scalar_one_or_none()

    await log_audit(
        db,
        user=user,
        action=RUNNER_HISTORY_OVERSIGHT_ACTION,
        entity_type=RUNNER_HISTORY_OVERSIGHT_ENTITY_TYPE,
        entity_id=runner.id,
        entity_name=runner.name,
        summary=(
            f"Admin '{user.email}' read the command history of runner "
            f"'{runner.name}'"
            + (f" owned by '{owner_email}'" if owner_email else "")
        ),
        details={
            "runner_id": str(runner.id),
            "runner_owner_id": str(runner.user_id) if runner.user_id else None,
            "runner_owner_email": owner_email,
            "window_seconds": RUNNER_HISTORY_OVERSIGHT_AUDIT_WINDOW_SECONDS,
            "reason": "admin_oversight_runner_commands",
        },
        request=request,
    )
    await db.commit()
    if redis is None:
        return
    try:
        await redis.set(key, "1", ex=RUNNER_HISTORY_OVERSIGHT_AUDIT_WINDOW_SECONDS)
    except Exception as exc:
        # The record is durable; the next poll audits again, which is the
        # cheaper failure.
        logger.warning(
            "Runner history oversight marker not written for admin=%s runner=%s: %s",
            user.id,
            runner.id,
            format_exception_message(exc),
        )


# ── Schemas ─────────────────────────────────────────────────────────────────


class EnrollmentKeyCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    # NULL = unlimited reusable fleet key; 1 = single-use.
    max_uses: int | None = Field(default=1, ge=1)
    default_labels: list[str] = Field(default_factory=list)
    default_capabilities: list[str] = Field(default_factory=list)
    default_permission_mode: str = "restricted"
    # Seeds ``Runner.require_approval`` on the enrolled machine: the
    # key form's "Shell commands need approval" switch, off unless the person
    # turns it on - no preset implies it.
    default_require_approval: bool = False
    enroll_as_company_managed: bool = False
    ttl_seconds: int | None = None


class EnrollRequest(BaseModel):
    enrollment_key: str
    platform: str
    hostname: str | None = None
    name: str | None = None
    host_facts: dict | None = None
    agent_version: str | None = None
    # Stable machine identity (salted SHA-256 of the OS machine id, hex) the
    # runner computes locally. Optional: builds that predate it simply enroll
    # without re-enrollment convergence.
    machine_id: str | None = Field(default=None, max_length=128)


class RunnerUpdate(BaseModel):
    name: str | None = None
    labels: list[str] | None = None
    tags: list[str] | None = None
    permission_mode: str | None = None
    capabilities: list[str] | None = None
    allowed_shell_commands: list[str] | None = None
    blocked_shell_commands: list[str] | None = None
    allowed_paths: list[str] | None = None
    require_approval: bool | None = None
    is_company_managed: bool | None = None
    shared_group_ids: list[str] | None = None
    shared_group_permissions: dict | None = None
    shared_user_ids: list[str] | None = None
    shared_user_permissions: dict[str, str] | None = None
    access_restricted: bool | None = None


class ApprovalDecision(BaseModel):
    """Optional body for approve/reject: persist the decision as policy.

    ``remember`` on approve adds the exact command string to the runner's
    shell allow-list (standing approval — it never queues again); on reject it
    adds it to the block-list (standing refusal — it never even queues again).
    Only meaningful for ``run_shell`` commands, which are the only kind the
    approval queue holds.
    """

    remember: bool = False


# ── Enrollment-key management (users/admins) ─────────────────────────────────


@router.post("/enrollment-keys", status_code=status.HTTP_201_CREATED)
async def create_enrollment_key(
    body: EnrollmentKeyCreate,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Create an enrollment key. The raw ``renr-`` secret is returned once."""
    _require_enabled()
    if body.default_permission_mode not in RUNNER_PERMISSION_MODES:
        raise HTTPException(status_code=422, detail="Invalid permission_mode")
    invalid = set(body.default_capabilities) - set(RUNNER_CAPABILITIES)
    if invalid:
        raise HTTPException(
            status_code=422, detail=f"Unknown capabilities: {sorted(invalid)}"
        )
    if body.enroll_as_company_managed and not user.is_admin:
        raise HTTPException(
            status_code=403,
            detail="Only admins can create company-managed fleet enrollment keys",
        )

    raw_key = generate_enrollment_key()
    ttl = body.ttl_seconds
    if ttl is None and body.max_uses == 1:
        ttl = settings.runner_enrollment_ttl_seconds
    expires_at = (
        datetime.now(UTC) + timedelta(seconds=ttl) if ttl and ttl > 0 else None
    )

    key = RunnerEnrollmentKey(
        user_id=user.id,
        name=body.name.strip(),
        key_prefix=key_display_prefix(raw_key),
        key_hash=await hash_key(raw_key),
        max_uses=body.max_uses,
        default_labels=body.default_labels,
        default_capabilities=body.default_capabilities,
        default_permission_mode=body.default_permission_mode,
        default_require_approval=body.default_require_approval,
        enroll_as_company_managed=body.enroll_as_company_managed,
        expires_at=expires_at,
    )
    db.add(key)
    await db.flush()
    await log_audit(
        db,
        user=user,
        action="create",
        entity_type="runner_enrollment_key",
        entity_id=key.id,
        entity_name=key.name,
        summary=f"Created runner enrollment key '{key.name}'",
        request=request,
    )
    await db.commit()
    return {**serialize_enrollment_key(key), "key": raw_key}


@router.get("/enrollment-keys")
async def list_enrollment_keys(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    _require_enabled()
    result = await db.execute(
        select(RunnerEnrollmentKey)
        .where(RunnerEnrollmentKey.user_id == user.id)
        .order_by(RunnerEnrollmentKey.created_at.desc())
    )
    return [serialize_enrollment_key(k) for k in result.scalars().all()]


@router.delete("/enrollment-keys/{key_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_enrollment_key(
    key_id: str,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    _require_enabled()
    kid = parse_path_id(key_id, not_found_detail="Enrollment key not found")
    key = await db.get(RunnerEnrollmentKey, kid)
    if not key or (key.user_id != user.id and not user.is_admin):
        raise HTTPException(status_code=404, detail="Enrollment key not found")
    await log_audit(
        db,
        user=user,
        action="delete",
        entity_type="runner_enrollment_key",
        entity_id=key.id,
        entity_name=key.name,
        summary=f"Revoked runner enrollment key '{key.name}'",
        request=request,
    )
    await db.delete(key)
    await db.commit()


# ── Enrollment (runner daemon, ``renr-`` credential) ─────────────────────────


async def enroll_runner(
    body: EnrollRequest,
    db: AsyncSession = Depends(get_db),
):
    """Exchange a ``renr-`` enrollment key for a durable ``run-`` credential.

    One row per MACHINE, not per call: when the runner reports a
    ``machine_id`` that matches an active runner in the same identity scope,
    the enrollment converges on that row (credential rotated, facts
    refreshed, operator config preserved) instead of duplicating the machine
    in the fleet list. Otherwise a new :class:`Runner` is created, stamped
    with the enrollment key's defaults so an unattended MDM roll-out lands
    fully configured. The raw ``run-`` key is returned exactly once — the
    runner stores it in the OS keychain and presents it on every call.
    """
    _require_enabled()
    if body.platform not in RUNNER_PLATFORMS:
        raise HTTPException(status_code=422, detail="Invalid platform")

    key = await resolve_enrollment_key(body.enrollment_key, db)
    if key is None:
        raise HTTPException(
            status_code=401, detail="Invalid or expired enrollment key"
        )

    # Consume the key atomically: lock the row and re-check active / expiry /
    # remaining-uses INSIDE this transaction. Without the lock two concurrent
    # enrollments can both pass a max_uses=1 gate before either increments
    # use_count (TOCTOU), enrolling two runners off a single-use key.
    locked = await db.execute(
        select(RunnerEnrollmentKey)
        .where(RunnerEnrollmentKey.id == key.id)
        .with_for_update()
    )
    key = locked.scalar_one_or_none()
    if key is None or not key.is_active:
        raise HTTPException(
            status_code=401, detail="Invalid or expired enrollment key"
        )
    if key.expires_at is not None:
        expires = key.expires_at
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=UTC)
        if expires < datetime.now(UTC):
            raise HTTPException(
                status_code=401, detail="Invalid or expired enrollment key"
            )
    if key.max_uses is not None and key.use_count >= key.max_uses:
        raise HTTPException(
            status_code=409, detail="Enrollment key has no uses remaining"
        )

    # Re-enrollment convergence: the same machine enrolling again (reinstall,
    # credential loss, install-script re-run on a machine that already has the
    # app) folds into its existing row instead of duplicating the fleet list.
    # Matched STRICTLY on the runner-reported machine identity within the same
    # identity scope — hostname is deliberately NOT a fallback key: cloned VM
    # fleets legitimately share one, and a false merge would invalidate a
    # DIFFERENT machine's live credential, which is far worse than a cosmetic
    # duplicate. Revoked rows never match: revocation is a security tombstone,
    # so a revoked machine deliberately comes back as a NEW identity. Locked
    # FOR UPDATE so two concurrent re-enrolls of one machine serialize.
    machine_id = (body.machine_id or "").strip() or None
    existing: Runner | None = None
    if machine_id:
        scope_filter = (
            Runner.is_company_managed.is_(True)
            if key.enroll_as_company_managed
            else (Runner.user_id == key.user_id)
        )
        found = await db.execute(
            select(Runner)
            .where(
                Runner.machine_id == machine_id,
                Runner.platform == body.platform,
                Runner.is_active.is_(True),
                scope_filter,
            )
            .order_by(Runner.created_at.asc())
            .limit(1)
            .with_for_update()
        )
        existing = found.scalars().first()

    # Fleet bound: cap active runners per identity so an unattended reusable
    # key can't create an unbounded number of Runner rows (0 = unlimited).
    # A convergent re-enrollment adds no row, so it is exempt.
    if existing is None and settings.runner_max_per_user:
        from sqlalchemy import func

        if key.enroll_as_company_managed:
            count_q = (
                select(func.count())
                .select_from(Runner)
                .where(Runner.is_company_managed.is_(True), Runner.is_active.is_(True))
            )
        else:
            count_q = (
                select(func.count())
                .select_from(Runner)
                .where(Runner.user_id == key.user_id, Runner.is_active.is_(True))
            )
        active_count = await db.scalar(count_q)
        if active_count is not None and active_count >= settings.runner_max_per_user:
            raise HTTPException(
                status_code=409,
                detail=(
                    "Runner limit reached for this account "
                    f"({settings.runner_max_per_user}). Revoke an unused runner "
                    "or raise RUNNER_MAX_PER_USER."
                ),
            )

    raw_runner_key = generate_runner_key()
    now = datetime.now(UTC)
    # Presence alternates (see runner_transport.touch_runner): a re-enrolled
    # machine that was already online is not a new "Runner online".
    came_online = existing is None or existing.status != "online"
    if existing is not None:
        runner = existing
        apply_reenrollment(
            runner,
            key_prefix=key_display_prefix(raw_runner_key),
            key_hash=await hash_key(raw_runner_key),
            hostname=body.hostname,
            host_facts=body.host_facts,
            agent_version=body.agent_version,
            now=now,
        )
    else:
        runner = Runner(
            user_id=None if key.enroll_as_company_managed else key.user_id,
            name=(body.name or body.hostname or "Runner").strip()[:200],
            platform=body.platform,
            hostname=body.hostname,
            machine_id=machine_id,
            key_prefix=key_display_prefix(raw_runner_key),
            key_hash=await hash_key(raw_runner_key),
            permission_mode=key.default_permission_mode,
            capabilities=list(key.default_capabilities or []),
            # The Desktop preset holds shell commands for the user's
            # approval in the chat from the very first command.
            require_approval=bool(key.default_require_approval),
            labels=list(key.default_labels or []),
            host_facts=body.host_facts,
            agent_version=body.agent_version,
            is_company_managed=key.enroll_as_company_managed,
            status="online",
            last_seen_at=now,
            enrolled_at=now,
        )
        db.add(runner)

    key.use_count += 1
    key.last_used_at = now
    if key.max_uses is not None and key.use_count >= key.max_uses:
        key.is_active = False

    await db.flush()
    owner = await db.get(User, key.user_id)
    if owner is not None:
        verb = "re-enrolled" if existing is not None else "enrolled"
        await log_audit(
            db,
            user=owner,
            action="update" if existing is not None else "create",
            entity_type="runner",
            entity_id=runner.id,
            entity_name=runner.name,
            summary=f"Runner '{runner.name}' {verb} ({runner.platform})",
        )
    # A machine joining the fleet IS a presence transition — and since
    # enrollment presets status=online + last_seen_at=now, the heartbeat
    # hook in touch_runner will never see it as an offline→online flip.
    # Fail-open + SAVEPOINT: the flow bus must never break enrollment.
    if came_online:
        with contextlib.suppress(Exception):
            from app.services.runner_flow_events import emit_runner_presence

            async with db.begin_nested():
                await emit_runner_presence(db, runner.id, online=True)
    await db.commit()
    if came_online:
        with contextlib.suppress(Exception):
            await runner_transport._notify_flow_engine()

    return {
        "runner": serialize_runner(runner, now=now),
        "key": raw_runner_key,
        "heartbeat_interval_seconds": settings.runner_heartbeat_interval_seconds,
        "long_poll_timeout_seconds": settings.runner_long_poll_timeout_seconds,
    }


router.add_api_route(
    "/enroll",
    enroll_runner,
    methods=["POST"],
    status_code=status.HTTP_201_CREATED,
    route_class_override=credential_transport_route(refuse_insecure_credential_transport),
)


# ── Runner management (users/admins) ─────────────────────────────────────────


@router.get("")
async def list_runners(
    scope: EntityListScope = Query("visible", description=ENTITY_LIST_SCOPE_DESCRIPTION),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    _require_enabled()
    group_ids = await get_user_group_ids(db, user.id)
    # the XTM One Desktop page is a PER-USER fleet list: own + group-shared +
    # company-managed only, admins included, like every other library list -
    # an admin's own Desktop page must not surface every user's PRIVATE laptop.
    # Cross-user fleet oversight is the admin object browser's job (the
    # ``runner`` arm of ``admin_objects.ENTITY_MAP``, whose rows open
    # ``/runners/{id}``). ``scope=all`` is the detail pages' read: an admin
    # editing a colleague's agent or assignment sees the machines it targets.
    result = await db.execute(
        user_visible_query(
            Runner, user.id, group_ids, is_admin=list_scope_admin_bypass(scope, user)
        )
    )
    runners = result.scalars().all()
    now = datetime.now(UTC)
    return [
        serialize_runner(
            r, now=now, can_manage=can_manage(user, r, group_ids)
        )
        for r in runners
    ]


@router.get("/capabilities")
async def list_capabilities():
    """Static vocabulary for the Profile UI (capabilities + modes + platforms)."""
    _require_enabled()
    return {
        "capabilities": list(RUNNER_CAPABILITIES),
        "permission_modes": list(RUNNER_PERMISSION_MODES),
        "platforms": list(RUNNER_PLATFORMS),
    }


async def _get_visible_runner(
    runner_id: str, user: User, db: AsyncSession
) -> tuple[Runner, list[str]]:
    # By-id fetch for the detail / manage / revoke / delete endpoints. This
    # DELIBERATELY keeps the is_admin bypass (unlike list_runners):
    # administrative action on a KNOWN runner id is the same contract every
    # other personal entity uses (get_user_agent(..., is_admin=True) et al.).
    # can_manage() still gates the mutating callers. The admin can no longer
    # discover another user's private runner id from the list, only act on one
    # they already hold.
    rid = parse_path_id(runner_id, not_found_detail="Runner not found")
    group_ids = await get_user_group_ids(db, user.id)
    result = await db.execute(
        user_visible_query(Runner, user.id, group_ids, is_admin=user.is_admin).where(
            Runner.id == rid
        )
    )
    runner = result.scalar_one_or_none()
    if runner is None:
        raise HTTPException(status_code=404, detail="Runner not found")
    return runner, group_ids


async def _command_runner(
    cmd: RunnerCommand, user: User, db: AsyncSession, *, not_found: str
) -> tuple[Runner, list[str]]:
    """The runner of *cmd* as the caller sees it, or the command's own unknown-id 404.

    A command on a runner the caller cannot see is answered like a command
    that does not exist; the runner's "Runner not found" would confirm it does.
    """
    try:
        return await _get_visible_runner(str(cmd.runner_id), user, db)
    except HTTPException as exc:
        if exc.status_code == 404:
            raise HTTPException(status_code=404, detail=not_found) from None
        raise


@router.patch("/{runner_id}")
async def update_runner(
    runner_id: str,
    body: RunnerUpdate,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    _require_enabled()
    runner, group_ids = await _get_visible_runner(runner_id, user, db)
    if not can_manage(user, runner, group_ids):
        raise HTTPException(status_code=403, detail="You cannot manage this runner")

    if body.permission_mode is not None:
        if body.permission_mode not in RUNNER_PERMISSION_MODES:
            raise HTTPException(status_code=422, detail="Invalid permission_mode")
        runner.permission_mode = body.permission_mode
    if body.capabilities is not None:
        invalid = set(body.capabilities) - set(RUNNER_CAPABILITIES)
        if invalid:
            raise HTTPException(
                status_code=422, detail=f"Unknown capabilities: {sorted(invalid)}"
            )
        runner.capabilities = body.capabilities
    if body.name is not None:
        runner.name = body.name.strip()[:200]
    if body.labels is not None:
        runner.labels = body.labels
    if body.tags is not None:
        runner.tags = body.tags
    if body.allowed_shell_commands is not None:
        runner.allowed_shell_commands = body.allowed_shell_commands or None
    if body.blocked_shell_commands is not None:
        runner.blocked_shell_commands = body.blocked_shell_commands or None
    if body.allowed_paths is not None:
        runner.allowed_paths = body.allowed_paths or None
    if body.require_approval is not None:
        runner.require_approval = body.require_approval

    # Company-managed toggle sweeps stale agent.runner_ids bindings.
    await handle_company_managed_toggle(
        db,
        user,
        runner,
        body.is_company_managed,
        agent_field="runner_ids",
        keep_grants=wants_access_restriction(body, runner),
    )
    if not runner.is_company_managed:
        await apply_group_share_fields(
            db, runner, body, partial=True, null_keeps_stored=True
        )

    # Per-user sharing — after the company-managed toggle, since
    # ``access_restricted`` is only meaningful relative to it.
    await apply_user_share_fields(db, runner, body, partial=True)

    await log_audit(
        db,
        user=user,
        action="update",
        entity_type="runner",
        entity_id=runner.id,
        entity_name=runner.name,
        summary=f"Updated runner '{runner.name}'",
        request=request,
    )
    await db.commit()
    await db.refresh(runner)
    return serialize_runner(runner, can_manage=True)


@router.post("/{runner_id}/revoke")
async def revoke_runner(
    runner_id: str,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Instantly revoke a runner's credential (it can no longer authenticate)."""
    _require_enabled()
    runner, group_ids = await _get_visible_runner(runner_id, user, db)
    if not can_manage(user, runner, group_ids):
        raise HTTPException(status_code=403, detail="You cannot manage this runner")
    # Decided on the row under its lock, not on the one loaded above: a
    # concurrent revoke, or a presence sweep that already declared the machine
    # offline, must not fire a second "Runner offline".
    await db.refresh(runner, with_for_update=True)
    # A machine revoked while online leaves the fleet: that is its
    # "Runner offline" (the presence sweep only flips `online` rows, and a
    # socket closing no longer declares anything).
    went_offline = runner.is_active and runner.status == "online"
    runner.is_active = False
    runner.status = "revoked"
    await log_audit(
        db,
        user=user,
        action="update",
        entity_type="runner",
        entity_id=runner.id,
        entity_name=runner.name,
        summary=f"Revoked runner '{runner.name}'",
        request=request,
    )
    # Nothing reaches a revoked machine any more, so its outstanding commands
    # end with the revocation instead of leaving their waiters hanging.
    ended = await runner_transport.end_revoked_runner_commands(
        db, runner.id, "The runner was revoked; nothing more is accepted from it."
    )
    if went_offline:
        with contextlib.suppress(Exception):
            from app.services.runner_flow_events import emit_runner_presence

            async with db.begin_nested():
                await emit_runner_presence(db, runner.id, online=False)
    await db.commit()
    await runner_transport.announce_finished_commands(ended, "cancelled")
    if went_offline:
        with contextlib.suppress(Exception):
            await runner_transport._notify_flow_engine()
    # Drop any live socket now — the credential is already invalid.
    await runner_transport.publish_runner_control(runner.id, "revoke")
    return serialize_runner(runner, can_manage=True)


@router.delete("/{runner_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_runner(
    runner_id: str,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    _require_enabled()
    runner, group_ids = await _get_visible_runner(runner_id, user, db)
    if not can_manage(user, runner, group_ids):
        raise HTTPException(status_code=403, detail="You cannot manage this runner")
    await log_audit(
        db,
        user=user,
        action="delete",
        entity_type="runner",
        entity_id=runner.id,
        entity_name=runner.name,
        summary=f"Deleted runner '{runner.name}'",
        request=request,
    )
    runner_id_for_control = runner.id
    await db.delete(runner)
    await db.commit()
    # Drop any live socket now — the credential no longer resolves.
    await runner_transport.publish_runner_control(runner_id_for_control, "revoke")


@router.get("/{runner_id}/commands")
async def list_runner_commands(
    runner_id: str,
    request: Request,
    limit: int = Query(default=50, ge=1, le=200),
    include_result: bool = Query(default=True),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Audit trail: the most recent commands dispatched to a runner.

    Visibility to the runner is not enough to read its full command history:
    a company-managed runner is visible to every user, and the command rows
    carry shell output and inline file contents. Managers (owner / admin /
    group write) see the whole fleet audit; everyone else is scoped to the
    commands they themselves dispatched. An admin admitted to a colleague's
    PRIVATE machine by the role alone - the Objects browser opens every
    enrolled machine on ``/runners/{id}`` - leaves an audit entry
    (:func:`_audit_runner_history_oversight`).

    ``include_result=false`` serves every ``result`` as ``null`` without
    loading it: a result may hold up to ``RUNNER_MAX_RESULT_META_BYTES`` of
    JSON, which a list of what ran and how it ended does not need.
    """
    _require_enabled()
    runner, group_ids = await _get_visible_runner(runner_id, user, db)
    stmt = select(RunnerCommand).where(RunnerCommand.runner_id == runner.id)
    if not can_manage(user, runner, group_ids):
        stmt = stmt.where(RunnerCommand.user_id == user.id)
    elif is_admin_oversight_read(user, runner, group_ids):
        await _audit_runner_history_oversight(db, user, runner, request)
    if not include_result:
        stmt = stmt.options(defer(RunnerCommand.result))
    stmt = stmt.order_by(RunnerCommand.created_at.desc()).limit(limit)
    result = await db.execute(stmt)
    return [
        serialize_command(c, include_result=include_result)
        for c in result.scalars().all()
    ]


# ── HITL command approval (users/admins) ─────────────────────────────────────

def _remember_shell_decision(runner: Runner, cmd: RunnerCommand, field: str) -> bool:
    """Persist the command string into ``field`` (allow/block list) on the
    runner. Returns True when the command is on the list afterwards.

    The command is saved whole, whatever its length: exact equality is
    the first shape ``_shell_allowed`` matches, so a whole entry is never
    wider than what the operator decided on, while a shortened one would be
    (prefix semantics) - a command is never cut, and never silently dropped.
    """
    if cmd.kind != "run_shell":
        return False
    command = str((cmd.payload or {}).get("command", "")).strip()
    # A secret variable in it: its stored marker is bound to this
    # command and matches no other, and its value is never written to a list.
    if not command or SECRET_MARKER_RE.search(command):
        return False
    current = [e for e in (getattr(runner, field) or []) if isinstance(e, str)]
    if any(e.strip().lower() == command.lower() for e in current):
        return True  # already remembered — treat as applied
    # Reassign (never mutate in place) so SQLAlchemy sees the JSON change.
    setattr(runner, field, [*current, command])
    return True


async def _lock_held_command(db: AsyncSession, command_id: object) -> RunnerCommand | None:
    """Lock a command for a decision, its runner's row first.

    Runner then command is the order the revoke route and ``dispatch_command``
    take them in, so a decision that also writes the runner (``remember``)
    never deadlocks against a revoke that is ending the runner's commands.
    """
    runner_id = await db.scalar(
        select(RunnerCommand.runner_id).where(RunnerCommand.id == command_id)
    )
    if runner_id is None:
        return None
    await db.execute(select(Runner.id).where(Runner.id == runner_id).with_for_update())
    return (
        await db.execute(
            select(RunnerCommand).where(RunnerCommand.id == command_id).with_for_update()
        )
    ).scalar_one_or_none()


@router.post("/commands/{command_id}/approve")
async def approve_command(
    command_id: str,
    request: Request,
    body: ApprovalDecision | None = None,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Approve a command held for human review, dispatching it to the runner.

    With ``remember: true`` the exact command string is also added to the
    runner's shell allow-list, so the same command never queues for approval
    again (a standing approval the operator can revoke in the permission
    editor).
    """
    _require_enabled()
    cid = parse_path_id(command_id, not_found_detail="No pending command to approve")
    # Lock the command row for this transaction so a concurrent approve/reject
    # cannot also observe pending_approval and overwrite this decision (a race
    # could otherwise re-queue a command an operator just rejected, or cancel
    # one just approved). The loser blocks here, then re-reads the now-terminal
    # status below and gets the 404; the lock is released by the commit /
    # rollback that ends this request.
    cmd = await _lock_held_command(db, cid)
    if cmd is None or cmd.status != "pending_approval":
        raise HTTPException(status_code=404, detail="No pending command to approve")
    runner, group_ids = await _command_runner(cmd, user, db, not_found="No pending command to approve")
    if not can_manage(user, runner, group_ids):
        raise HTTPException(status_code=403, detail="You cannot manage this runner")

    # Persist the standing approval BEFORE re-authorizing so the dispatch
    # decision reflects the policy the operator just chose. The block-list
    # still wins inside authorize_command, so remembering can never unblock.
    remembered = False
    if body and body.remember:
        remembered = _remember_shell_decision(runner, cmd, "allowed_shell_commands")
        if remembered:
            await log_audit(
                db,
                user=user,
                action="update",
                entity_type="runner",
                entity_id=runner.id,
                entity_name=runner.name,
                summary=(
                    "Added a shell command to the allow-list of runner "
                    f"'{runner.name}' while approving command {cmd.id}"
                ),
                request=request,
            )

    # Re-authorize against the runner's CURRENT policy before dispatching: the
    # capability list, shell scoping or permission_mode may have been
    # tightened while the command sat awaiting approval, and an operator must
    # not be able to approve a command that is now forbidden. Cancel it instead.
    # Checked as the runner would get it, its secret variables restored.
    payload, _ = await runner_transport.restored_command_payload(db, cmd)
    refusal = authorize_command(runner, cmd.kind, payload)
    if refusal:
        cmd.status = "cancelled"
        cmd.error = f"Blocked by current runner policy at approval time: {refusal}"
        cmd.completed_at = datetime.now(UTC)
        # Terminal transition outside submit_result → same flow-event
        # contract (fail-open): listeners hear the cancellation, parked
        # flow nodes wake.
        with contextlib.suppress(Exception):
            from app.services.runner_flow_events import (
                emit_runner_command_completed,
            )

            # SAVEPOINT: an emission failure must not poison the
            # transaction that owns the cancellation write.
            async with db.begin_nested():
                await emit_runner_command_completed(db, cmd, runner)
        await db.commit()
        with contextlib.suppress(Exception):
            await runner_transport._notify_flow_engine()
        raise HTTPException(status_code=409, detail=refusal)
    cmd.status = "pending"
    await db.commit()
    await runner_transport._notify_new_command(cmd.runner_id)
    return {"ok": True, "status": "pending", "remembered": remembered}


@router.post("/commands/{command_id}/reject")
async def reject_command(
    command_id: str,
    request: Request,
    body: ApprovalDecision | None = None,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Reject a command held for human review.

    With ``remember: true`` the exact command string is also added to the
    runner's shell block-list, so the same command is refused outright from
    now on — it never even reaches the approval queue again.
    """
    _require_enabled()
    cid = parse_path_id(command_id, not_found_detail="No pending command to reject")
    # Lock the command row (see approve_command): serializes a concurrent
    # approve/reject so only one terminal decision wins. The loser blocks, then
    # re-reads the now-terminal status and gets the 404.
    cmd = await _lock_held_command(db, cid)
    if cmd is None or cmd.status != "pending_approval":
        raise HTTPException(status_code=404, detail="No pending command to reject")
    runner, group_ids = await _command_runner(cmd, user, db, not_found="No pending command to reject")
    if not can_manage(user, runner, group_ids):
        raise HTTPException(status_code=403, detail="You cannot manage this runner")

    remembered = False
    if body and body.remember:
        remembered = _remember_shell_decision(runner, cmd, "blocked_shell_commands")
        if remembered:
            await log_audit(
                db,
                user=user,
                action="update",
                entity_type="runner",
                entity_id=runner.id,
                entity_name=runner.name,
                summary=(
                    "Added a shell command to the block-list of runner "
                    f"'{runner.name}' while rejecting command {cmd.id}"
                ),
                request=request,
            )

    cmd.status = "cancelled"
    cmd.error = "Rejected by operator"
    cmd.completed_at = datetime.now(UTC)
    # Terminal transition outside submit_result → same flow-event contract
    # (fail-open): listeners hear the rejection, parked flow nodes wake.
    with contextlib.suppress(Exception):
        from app.services.runner_flow_events import (
            emit_runner_command_completed,
        )

        async with db.begin_nested():
            await emit_runner_command_completed(db, cmd, runner)
    await db.commit()
    with contextlib.suppress(Exception):
        await runner_transport._notify_flow_engine()
    return {"ok": True, "status": "cancelled", "remembered": remembered}


_NO_RUNNING_COMMAND = "No running command to stop"


@router.post("/commands/{command_id}/stop")
async def stop_command(
    command_id: str,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Stop a shell command running on its machine (the Stop on its chat card).

    Allowed to whoever the command runs for, or to whoever manages its runner.
    The runner ends the command's whole process tree and reports it with
    ``stopped: "user"`` and the output printed until then; the tool waiting on
    it hands that to the agent. Who asked is kept on the row before the signal
    leaves and lands in the result as ``stopped_by``, so the card says
    who stopped it rather than "you" to every viewer. A command that is no
    longer running (it exited meanwhile) is answered ``stopping: false``, not
    an error: the click lost a race it could not see.
    """
    _require_enabled()
    cid = parse_path_id(command_id, not_found_detail=_NO_RUNNING_COMMAND)
    cmd = await db.get(RunnerCommand, cid)
    if cmd is None or cmd.kind != "run_shell":
        raise HTTPException(status_code=404, detail=_NO_RUNNING_COMMAND)
    runner, group_ids = await _command_runner(cmd, user, db, not_found=_NO_RUNNING_COMMAND)
    if cmd.user_id != user.id and not can_manage(user, runner, group_ids):
        raise HTTPException(status_code=403, detail="You cannot stop this command")
    # A background job is answered ``completed`` as soon as it
    # started and keeps running on the machine under the command's id.
    payload = getattr(cmd, "payload", None) or {}
    background_job = cmd.status == "completed" and bool(payload.get("background"))
    if cmd.status != "dispatched" and not background_job:
        return {"ok": True, "stopping": False, "status": cmd.status}
    cmd.payload = {**(cmd.payload or {}), STOPPED_BY_KEY: {"user_id": str(user.id), "name": user.full_name}}
    await log_audit(
        db,
        user=user,
        action="update",
        entity_type="runner",
        entity_id=runner.id,
        entity_name=runner.name,
        summary=f"Stopped shell command {cmd.id} on runner '{runner.name}'",
        request=request,
    )
    # Read before the commit expires the row: a background job answers
    # ``completed``, a foreground command ``dispatched``.
    status_ = cmd.status
    command_id_ = cmd.id
    # Committed before the signal leaves: the runner's report can come back
    # within milliseconds, and its result reads who stopped it from the row.
    await db.commit()
    await runner_transport.publish_runner_control(runner.id, runner_transport.stop_signal(command_id_))
    return {"ok": True, "stopping": True, "status": status_}


#: How long the Machines page waits for a runner to list its background jobs.
JOBS_LIST_WAIT_SECONDS = 10.0


@router.get("/{runner_id}/jobs")
async def list_runner_jobs(
    runner_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """The background shell jobs a machine holds (the Machines page).

    Asked to the machine itself with a ``shell_job`` ``list`` command, the one
    the ``shell_jobs`` tool sends: the runner is the only place a job lives.
    Managers only, like the full command history, since a job line carries its
    command. Answered ``available: false`` with the reason, never an error,
    when the machine is offline, busy, lacks the shell capability or does not
    answer within :data:`JOBS_LIST_WAIT_SECONDS`. Each ``job_id`` is the id of
    the ``run_shell`` command that started it, so
    ``POST /runners/commands/{job_id}/stop`` stops it.
    """
    _require_enabled()
    runner, group_ids = await _get_visible_runner(runner_id, user, db)
    if not can_manage(user, runner, group_ids):
        raise HTTPException(status_code=403, detail="You cannot list this machine's jobs")
    if not is_runner_online(runner):
        return {"available": False, "reason": "offline", "jobs": []}
    payload = {"action": "list"}
    if authorize_command(runner, "shell_job", payload):
        return {"available": False, "reason": "not_allowed", "jobs": []}
    target_id = runner.id
    await release_db_connection_before_loop(db)
    command_id, status_ = await runner_transport.dispatch_command(
        runner_id=target_id, kind="shell_job", payload=payload, user_id=user.id
    )
    if not command_id:
        return {"available": False, "reason": status_, "jobs": []}
    result = await runner_transport.await_result(command_id, timeout=JOBS_LIST_WAIT_SECONDS)
    if result is None or result.get("status") not in COMMAND_TERMINAL_STATUSES:
        await runner_transport.give_up_on_command(
            command_id, "The Machines page stopped waiting for the job list."
        )
        return {"available": False, "reason": "no_answer", "jobs": []}
    body = result.get("result") if result.get("status") == "completed" else None
    jobs = body.get("jobs") if isinstance(body, dict) else None
    if not isinstance(jobs, list):
        return {"available": False, "reason": "no_answer", "jobs": []}
    return {"available": True, "reason": None, "jobs": [j for j in jobs if isinstance(j, dict)]}


# ── Transport: runner daemon (``run-`` credential) ───────────────────────────


@router.post("/heartbeat")
async def runner_heartbeat(
    body: dict,
    runner: Runner = Depends(get_current_runner),
):
    """Long-poll transport keepalive + host-facts refresh."""
    _require_enabled()
    await runner_transport.touch_runner(
        runner.id,
        transport="long_poll",
        host_facts=body.get("host_facts"),
        agent_version=body.get("agent_version"),
    )
    # Protocol floor on the fallback transport too: tell an out-of-date runner
    # to self-update (it stops claiming commands and runs its updater).
    if runner_version_below(
        body.get("agent_version"), settings.runner_min_supported_version
    ):
        return {
            "ok": True,
            "update_required": True,
            "min_version": settings.runner_min_supported_version,
        }
    return {"ok": True}


@router.get("/poll")
async def poll_commands(
    runner: Runner = Depends(get_current_runner),
    version: str | None = Query(None),
):
    """HTTP long-poll fallback: block until a command is available or timeout.

    Event-driven despite being a poll: it waits on the runner's Redis wake
    channel and claims the moment a command is enqueued, so latency matches the
    WebSocket fast-path. Returns ``{"command": null}`` on timeout so the runner
    immediately re-polls.
    """
    _require_enabled()
    import asyncio

    from app.core.database import async_session
    from app.core.redis import close_pubsub, open_pubsub, redis_pubsub_client

    # Protocol floor: never hand a command to an out-of-date runner over the
    # fallback path — signal an update instead (matches the WS 4426 close).
    if runner_version_below(version, settings.runner_min_supported_version):
        return {
            "command": None,
            "update_required": True,
            "min_version": settings.runner_min_supported_version,
        }

    await runner_transport.touch_runner(runner.id, transport="long_poll")

    async with async_session() as db:
        cmd = await runner_transport.claim_next_command(db, runner.id)
    if cmd is not None:
        return {"command": cmd}

    loop = asyncio.get_event_loop()
    deadline = loop.time() + settings.runner_long_poll_timeout_seconds
    pubsub_client = await redis_pubsub_client()
    pubsub = await open_pubsub(pubsub_client, runner_transport._wake_channel(runner.id))
    try:
        while loop.time() < deadline:
            with contextlib.suppress(Exception):
                await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
            async with async_session() as db:
                cmd = await runner_transport.claim_next_command(db, runner.id)
            if cmd is not None:
                return {"command": cmd}
        return {"command": None}
    finally:
        await close_pubsub(pubsub, pubsub_client)


#: What Starlette's form parser says of a text field over ``max_part_size``
#: (pinned by ``tests/test_runner_result_upload.py``); its other refusals are
#: about the form's shape, not its size.
_PART_TOO_LARGE = "exceeded maximum size"
#: Room for a result upload's multipart framing - boundaries, part headers,
#: the file's name - past its two bounded parts.
_RESULT_UPLOAD_FRAMING_BYTES = 64 * 1024


def _result_upload_limit() -> int:
    """The largest body a result upload may be: its ``meta``, its file, their framing."""
    return (
        runner_transport.RUNNER_MAX_RESULT_META_BYTES
        + max(settings.runner_max_result_bytes, 0)
        + _RESULT_UPLOAD_FRAMING_BYTES
    )


def _form_refusal(
    exc: StarletteHTTPException, body: BoundedReceive
) -> tuple[int, str, str]:
    """The status, detail and agent-facing reason for a form the parser refused."""
    limit_mib = runner_transport.RUNNER_MAX_RESULT_META_BYTES // (1024 * 1024)
    if body.exceeded:
        return (
            413,
            "Result upload too large",
            (
                f"the upload is larger than {body.limit:,} bytes: a result carries "
                f"at most {limit_mib} MiB of data once written as JSON and a file "
                "of at most RUNNER_MAX_RESULT_BYTES "
                f"({settings.runner_max_result_bytes:,} bytes)."
            ),
        )
    detail = str(exc.detail)
    if _PART_TOO_LARGE in detail:
        reason = (
            f"{detail} A result's data may take at most {limit_mib} MiB once "
            "written as JSON: ask the machine for less at once (a narrower "
            "command, one folder, part of a file)."
        )
    else:
        reason = f"{detail} A result upload carries one 'meta' field and at most one file."
    return exc.status_code, detail, reason


@router.post("/commands/{command_id}/result")
async def submit_command_result(
    command_id: str,
    request: Request,
    runner: Runner = Depends(get_current_runner),
    db: AsyncSession = Depends(get_db),
):
    """Runner uploads a command's terminal result: the JSON ``meta`` field and
    an optional binary ``file``.

    The form is read once the runner is authenticated, under the upload
    contract's own bounds - ``RUNNER_MAX_RESULT_META_BYTES`` for ``meta``, one
    file of at most ``RUNNER_MAX_RESULT_BYTES``, and a body cut off past the
    two (:class:`~app.core.bounded_receive.BoundedReceive`) - and not the framework's default of 1 MiB
    per field, below what a runner may send.
    """
    _require_enabled()
    cid = parse_path_id(command_id, not_found_detail="Command not found")
    # The upload may take a while to arrive: the credential lookup's
    # transaction must not stay open while it does.
    await release_db_connection_before_loop(db)
    body = BoundedReceive(request.receive, _result_upload_limit(), refusal="Result upload too large")
    try:
        # ``max_part_size`` bounds the text field alone: the parser spools a
        # file part to disk whatever its size, which ``body`` bounds.
        form = await Request(request.scope, body).form(
            max_files=1,
            max_fields=1,
            max_part_size=runner_transport.RUNNER_MAX_RESULT_META_BYTES,
        )
    except ClientDisconnect as exc:
        # Not a refusal: the runner retries an upload whose connection dropped
        # (``is_transient`` in ``apps/runner/src/api.rs``), and a result that
        # never arrives is the command lease's to recover.
        raise HTTPException(
            status_code=400, detail="There was an error parsing the body"
        ) from exc
    except StarletteHTTPException as exc:
        status_code, detail, reason = _form_refusal(exc, body)
        await _refuse_result(
            db,
            await _lock_awaited_command(db, cid, runner),
            status_code,
            detail,
            reason=reason,
        )
    try:
        meta = form.get("meta")
        file = form.get("file")
        if not isinstance(meta, str) or not (file is None or isinstance(file, UploadFile)):
            await _refuse_result(
                db,
                await _lock_awaited_command(db, cid, runner),
                422,
                "Expected a 'meta' field and at most one 'file'",
                reason="the upload did not carry the result where it belongs.",
            )
        return await _accept_command_result(cid, meta, file, runner, db)
    finally:
        await form.close()


async def _lock_awaited_command(
    db: AsyncSession, cid: uuid.UUID, runner: Runner
) -> RunnerCommand | None:
    """The command a result is uploaded for, locked; ``None`` once it is finished."""
    # Claim the result transition ATOMICALLY: lock the command row for this
    # transaction so concurrent at-least-once deliveries serialize here. The
    # loser blocks on the lock, then observes the terminal state and no-ops
    # WITHOUT persisting a second artifact - that is what makes "first terminal
    # result wins" true end to end (artifact + sidecar + result write), not
    # just for the final status update. The lock is released by the commit in
    # ``submit_result`` (which therefore must run on THIS session).
    locked = await db.execute(
        select(RunnerCommand).where(RunnerCommand.id == cid).with_for_update()
    )
    cmd = locked.scalar_one_or_none()
    if cmd is None or cmd.runner_id != runner.id:
        raise HTTPException(status_code=404, detail="Command not found")
    # A result for an already-terminal command is an idempotent no-op
    # (at-least-once redelivery can produce a duplicate upload) and must NOT
    # re-persist the file or overwrite the recorded evidence. Anything else
    # that is not ``dispatched`` (pending, pending_approval) was never handed
    # to the runner, so a result for it is illegitimate: a runner must not be
    # able to complete queue entries it never received.
    if cmd.status in COMMAND_TERMINAL_STATUSES:
        return None
    if cmd.status != "dispatched":
        raise HTTPException(
            status_code=409,
            detail=f"Command is not awaiting a result (status: {cmd.status})",
        )
    return cmd


async def _refuse_result(
    db: AsyncSession,
    cmd: RunnerCommand | None,
    status_code: int,
    detail: str,
    *,
    reason: str,
) -> NoReturn:
    """Refuse a result upload, ending the command it is for as ``failed``.

    The runner never sends a refused result again: unrecorded, the command
    would stay ``dispatched``, its waiter would wait out the whole result
    window for nothing, and an unwatched command would run again once its
    lease ran out. ``reason`` is what the agent reads; a finished command
    (``cmd`` is ``None``) is left as it is.
    """
    if cmd is not None:
        await runner_transport.submit_result(
            cmd.id,
            status="failed",
            error=f"The platform refused this command's result: {reason}",
            db=db,
        )
    raise HTTPException(status_code=status_code, detail=detail)


async def _accept_command_result(
    cid: uuid.UUID,
    meta: str,
    file: UploadFile | None,
    runner: Runner,
    db: AsyncSession,
) -> dict:
    """Record the result of a command: its ``meta`` JSON and optional file.

    Binary results (screenshots, file reads) are persisted as a
    ``FileAttachment`` scoped to the originating conversation / assignment run,
    so the evidence surfaces in chat and downloads through the existing
    file-access path.

    Locks the command row on ``db`` itself (:func:`_lock_awaited_command`),
    until the commit in ``runner_transport.submit_result``: a caller must not
    hold that row locked on another session.
    """
    cmd = await _lock_awaited_command(db, cid, runner)
    if cmd is None:
        return {"ok": True}

    try:
        meta_obj = json.loads(meta) if meta else {}
    except json.JSONDecodeError:
        meta_obj = None
    if not isinstance(meta_obj, dict):
        await _refuse_result(
            db, cmd, 422, "Invalid meta JSON", reason="its data is not a JSON object."
        )

    # A runner reports exactly one of the protocol's result statuses. An
    # unrecognized value (a malformed or newer runner) must NOT be coerced to
    # "completed" - that would file its uploaded artifact as valid evidence of a
    # successful command. Reject it before anything is persisted.
    result_status = meta_obj.get("status", "completed")
    if result_status not in RUNNER_RESULT_STATUSES:
        unsupported = (
            f"Unsupported result status '{result_status}' "
            f"(expected one of: {', '.join(RUNNER_RESULT_STATUSES)})"
        )
        await _refuse_result(db, cmd, 422, unsupported, reason=f"{unsupported}.")

    raw_result = meta_obj.get("result")
    result_payload: dict | None = raw_result if isinstance(raw_result, dict) else None

    result_file_id = None
    if file is not None:
        # Read at most the cap + 1 byte so a compromised runner cannot force an
        # arbitrarily large in-memory allocation before the size check runs. A
        # cap below zero still reads one byte: read(-1) would read everything.
        max_bytes = settings.runner_max_result_bytes
        raw = await file.read(max(max_bytes, 0) + 1)
        if len(raw) > max_bytes:
            await _refuse_result(
                db,
                cmd,
                413,
                "Result file too large",
                reason=(
                    "its file is larger than RUNNER_MAX_RESULT_BYTES "
                    f"({max_bytes:,} bytes), the largest file a machine may return."
                ),
            )
        from app.services.builtin_tools._shared import _persist_generated_file

        filename = file.filename or "runner-result.bin"
        content_type = file.content_type or "application/octet-stream"
        conversation_id = str(cmd.conversation_id) if cmd.conversation_id else None
        assignment_run_id = (
            str(cmd.assignment_run_id) if cmd.assignment_run_id else None
        )
        file_att = await _persist_generated_file(
            user_id=str(cmd.user_id) if cmd.user_id else "",
            filename=filename,
            raw_bytes=raw,
            content_type=content_type,
            conversation_id=conversation_id,
            assignment_run_id=assignment_run_id,
            source=RUNNER_RESULT_SOURCE,
        )
        result_file_id = file_att.id

        # ── Integrity red-line ────────────────────────────────────────────
        # Hash EXACTLY the bytes we stored (the authoritative hash), compare
        # with the hash the runner computed at capture time, and record the
        # verdict. A mismatch is never fatal — the evidence still lands — but
        # it is permanently visible to the agent and in the sidecar.
        sha256 = hashlib.sha256(raw).hexdigest()
        reported = (result_payload or {}).get("sha256")
        sha256_verified = (
            reported.lower() == sha256 if isinstance(reported, str) else None
        )

        integrity: dict = {
            "sha256": sha256,
            "sha256_verified": sha256_verified,
            "byte_size": len(raw),
        }

        if cmd.kind in _CAPTURE_KINDS:
            base_meta = result_payload or {}
            host_facts = runner.host_facts if isinstance(runner.host_facts, dict) else {}
            sidecar = {
                "kind": "xtm_one_runner_capture",
                "command_id": str(cmd.id),
                "command_kind": cmd.kind,
                "capture_file": filename,
                "content_type": content_type,
                "byte_size": len(raw),
                "sha256": sha256,
                "sha256_reported_by_runner": reported,
                "sha256_verified": sha256_verified,
                "captured_at": base_meta.get("captured_at"),
                "captured_at_local": base_meta.get("captured_at_local"),
                "received_at": datetime.now(UTC).isoformat(),
                "display": base_meta.get("display"),
                "monitor": base_meta.get("monitor"),
                "width": base_meta.get("width"),
                "height": base_meta.get("height"),
                "url": base_meta.get("url"),
                "title": base_meta.get("title"),
                "runner_id": str(runner.id),
                "runner_name": runner.name,
                "runner_platform": runner.platform,
                "runner_version": runner.agent_version,
                "hostname": host_facts.get("hostname"),
                "os_version": host_facts.get("os_version"),
                "logged_in_user": host_facts.get("user"),
            }
            sidecar_att = await _persist_generated_file(
                user_id=str(cmd.user_id) if cmd.user_id else "",
                filename=f"{filename}.evidence.json",
                raw_bytes=json.dumps(sidecar, indent=2, default=str).encode("utf-8"),
                content_type="application/json",
                conversation_id=conversation_id,
                assignment_run_id=assignment_run_id,
                source=RUNNER_SIDECAR_SOURCE,
            )
            integrity["sidecar_file_id"] = str(sidecar_att.id)
            integrity["runner_name"] = runner.name
            integrity["hostname"] = host_facts.get("hostname")

        result_payload = {**(result_payload or {}), **integrity}

    # Record on THIS session: it holds the FOR UPDATE lock claimed above, and a
    # second session would deadlock waiting for it. The commit inside releases
    # the lock, letting a blocked duplicate delivery observe the terminal state.
    await runner_transport.submit_result(
        cid,
        status=result_status,
        result=result_payload if result_payload is not None else raw_result,
        error=meta_obj.get("error"),
        result_file_id=result_file_id,
        db=db,
    )
    return {"ok": True}


@router.websocket("/connect")
async def runner_websocket(websocket: WebSocket):
    """Event-driven fast path: the runner holds this socket; we push commands.

    Authenticated by the ``run-`` credential in the ``Authorization: Bearer``
    header (preferred — query strings are routinely retained by proxies / access
    logs, so a credential granting OS control must not ride in the URL), falling
    back to the legacy ``token`` query parameter for older runners. Control
    frames only — results always upload over HTTP so large binaries never
    traverse the socket.
    """
    if not settings.runners_enabled:
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
        runner = await resolve_runner_credential(token, db)
    if runner is None:
        await websocket.close(code=4401)
        return

    await websocket.accept()

    # Protocol floor: refuse an out-of-date runner and tell it to self-update,
    # rather than pushing commands it can't understand. Close code 4426 mirrors
    # HTTP 426 Upgrade Required so the daemon distinguishes this from an auth or
    # transient drop and triggers its updater instead of reconnect-storming.
    if runner_version_below(reported_version, settings.runner_min_supported_version):
        with contextlib.suppress(Exception):
            await websocket.send_json(
                {
                    "type": "update_required",
                    "min_version": settings.runner_min_supported_version,
                    "current_version": reported_version,
                }
            )
        await websocket.close(code=4426)
        return

    try:
        await runner_transport.serve_runner_websocket(websocket, runner)
    except WebSocketDisconnect:
        pass
    except Exception:
        logger.debug("Runner WS error", exc_info=True)


# ── Downloads + self-update (native bundles embedded in this image) ──────────
#
# The release pipeline (``release.yml`` of XTM-One-Platform/release, which
# runs ``apps/runner/scripts/release-compile.sh`` and ``release-stage.sh``)
# builds the native runner for every OS and architecture as part of the
# platform release and embeds the artifacts into this image under
# ``settings.runner_dist_dir``:
#
#   * stable-named one-click installers (below) — "next, next, finish", no CLI;
#   * signed Tauri v2 updater artifacts (+ detached ``.sig``) for the GUI app,
#     served through a dynamically built update manifest;
#   * bare-binary archives (+ ``.sha256`` / ``.minisig``) for headless service
#     installs and the daemon self-updater;
#   * the headless install scripts;
#   * ``VERSION`` — the embedded runner build's own MAJOR.YYMMDD.SEQ.
#
# There is deliberately NO external download URL and NO separate runner
# release: the platform image is the single distribution channel, so every
# deployment serves exactly the runner build it shipped with — which is what
# version-matches a self-hosted runner to its own instance's release. The
# platform never holds the signing key: CI signs each artifact and the runner
# verifies against the public key compiled into its binary. A source/dev
# deployment has an empty dist directory and reports downloads as unavailable
# instead of failing.

# Architectures the distribution speaks, in the exact vocabulary the runner
# reports (``apps/runner/src/enroll.rs::detect_arch``). ``x64`` is what an
# architecture-less request resolves to: a browser hitting /download/linux
# carries no CPU information, and x64 remains the common desktop case.
_RUNNER_ARCHES = ("x64", "arm64")
_DEFAULT_ARCH = "x64"

# One-click installer per (OS, arch). macOS ships ONE universal binary (Intel
# + Apple Silicon), so both Mac architectures resolve to the same bundle;
# Windows and Linux ship a native build per architecture, from the ARM legs of
# the release workflow's runner-build matrix. Linux defaults to the portable
# AppImage, with .deb / .rpm offered as alternates.
_PLATFORM_INSTALLER = {
    ("windows", "x64"): "xtm-one-runner-windows-x64-setup.exe",
    ("windows", "arm64"): "xtm-one-runner-windows-arm64-setup.exe",
    ("macos", "x64"): "xtm-one-runner-macos-universal.dmg",
    ("macos", "arm64"): "xtm-one-runner-macos-universal.dmg",
    ("linux", "x64"): "xtm-one-runner-linux-x64.AppImage",
    ("linux", "arm64"): "xtm-one-runner-linux-arm64.AppImage",
}

_RUNNER_PLATFORMS_WITH_DIST = tuple(
    dict.fromkeys(os_key for os_key, _ in _PLATFORM_INSTALLER)
)

# Alternate Linux package formats (same build, different packaging), per arch.
_LINUX_FORMAT_NAMES = ("appimage", "deb", "rpm")
_LINUX_FORMATS = {
    ("x64", "appimage"): "xtm-one-runner-linux-x64.AppImage",
    ("x64", "deb"): "xtm-one-runner-linux-x64.deb",
    ("x64", "rpm"): "xtm-one-runner-linux-x64.rpm",
    ("arm64", "appimage"): "xtm-one-runner-linux-arm64.AppImage",
    ("arm64", "deb"): "xtm-one-runner-linux-arm64.deb",
    ("arm64", "rpm"): "xtm-one-runner-linux-arm64.rpm",
}

# Bare-binary archives for headless (service) installs and the daemon
# self-updater, keyed by (os, arch) exactly as the runner reports them. The
# macOS archive is a universal binary, so both Mac architectures map to it.
_BARE_ARCHIVES = {
    ("windows", "x64"): "xtm-one-runner-windows-x64.zip",
    ("windows", "arm64"): "xtm-one-runner-windows-arm64.zip",
    ("macos", "x64"): "xtm-one-runner-macos-universal.tar.gz",
    ("macos", "arm64"): "xtm-one-runner-macos-universal.tar.gz",
    ("linux", "x64"): "xtm-one-runner-linux-x64.tar.gz",
    ("linux", "arm64"): "xtm-one-runner-linux-arm64.tar.gz",
}

# Headless-flavor archives. Same source, built without the `desktop`
# feature, so the binary links no X11 / Wayland stack and starts on a server
# where the desktop build dies in the loader. Linux only — it exists to unblock
# servers, and Windows / macOS service installs run the desktop build fine.
# There is no installer and no package: a headless host takes the bare archive.
_HEADLESS_ARCHIVES = {
    ("linux", "x64"): "xtm-one-runner-linux-x64-headless.tar.gz",
    ("linux", "arm64"): "xtm-one-runner-linux-arm64-headless.tar.gz",
}

# The build flavors a runner can report. A headless install must only ever be
# offered another headless build: updating it into the desktop flavor would
# leave it unable to start at all.
_RUNNER_FLAVORS = ("desktop", "headless")
_DEFAULT_FLAVOR = "desktop"

# Tauri v2 bundle-updater artifacts by manifest platform key. Each has a
# detached ``.sig`` (minisign) produced by CI; the GUI updater verifies it
# against the public key compiled into the bundle. These are the v2 forms
# (``createUpdaterArtifacts: true``): the artifact is the bundle itself —
# the NSIS setup.exe on Windows, the .app.tar.gz on macOS, and the raw
# .AppImage on Linux. The v1-era ``.AppImage.tar.gz`` only exists in the
# bundler's "v1Compatible" mode, which this pipeline does not use.
# Keyed ``{os}-{arch}``: the entry an install falls back to.
_UPDATER_ARTIFACTS = {
    "windows-x86_64": "xtm-one-runner-windows-x64-setup.exe",
    "windows-aarch64": "xtm-one-runner-windows-arm64-setup.exe",
    "darwin-x86_64": "xtm-one-runner-macos-universal.app.tar.gz",
    "darwin-aarch64": "xtm-one-runner-macos-universal.app.tar.gz",
    "linux-x86_64": "xtm-one-runner-linux-x64.AppImage",
    "linux-aarch64": "xtm-one-runner-linux-arm64.AppImage",
}

# Installer-specific entries, keyed ``{os}-{arch}-{installer}``, which the
# updater looks up BEFORE the ``{os}-{arch}`` entry above. Linux ships three
# install kinds, and the updater of each only accepts its own format:
# ``install_deb`` / ``install_rpm`` refuse anything else. So a ``.deb`` /
# ``.rpm`` install is served its own package, and ``linux-<arch>`` stays the
# AppImage the AppImage install falls back to.
_UPDATER_INSTALLER_ARTIFACTS = {
    "linux-x86_64-deb": "xtm-one-runner-linux-x64.deb",
    "linux-x86_64-rpm": "xtm-one-runner-linux-x64.rpm",
    "linux-aarch64-deb": "xtm-one-runner-linux-arm64.deb",
    "linux-aarch64-rpm": "xtm-one-runner-linux-arm64.rpm",
}

# Headless install scripts (shipped from source into the dist dir at image
# build, so `curl <instance>/api/v1/runners/dist/... | bash` always works).
_INSTALL_SCRIPTS = (
    "xtm-one-runner-installer.sh",
    "xtm-one-runner-installer.ps1",
)

_NO_DIST_DETAIL = (
    "This deployment was built without embedded runner artifacts "
    "(source/dev build); release images ship them automatically."
)


def _dist_assets() -> frozenset[str]:
    """Every file name the dist endpoint may serve (fixed allow-list)."""
    names: set[str] = set(_PLATFORM_INSTALLER.values())
    names.update(_LINUX_FORMATS.values())
    names.update(_INSTALL_SCRIPTS)
    for artifact in (*_UPDATER_ARTIFACTS.values(), *_UPDATER_INSTALLER_ARTIFACTS.values()):
        names.add(artifact)
        names.add(artifact + ".sig")
    for archive in (*_BARE_ARCHIVES.values(), *_HEADLESS_ARCHIVES.values()):
        names.add(archive)
        names.add(archive + ".sha256")
        names.add(archive + ".minisig")
    return frozenset(names)


_DIST_ASSETS = _dist_assets()


def _dist_dir() -> Path | None:
    """The embedded distribution directory, or None when absent (dev build)."""
    d = Path(settings.runner_dist_dir)
    return d if d.is_dir() else None


def _dist_file(name: str) -> Path | None:
    """A present, non-empty dist file by exact name, else None."""
    d = _dist_dir()
    if d is None:
        return None
    f = d / name
    try:
        if f.is_file() and f.stat().st_size > 0:
            return f
    except OSError:
        return None
    return None


def _dist_version() -> str:
    """The embedded runner build's version (``VERSION`` marker), or ""."""
    f = _dist_file("VERSION")
    if f is None:
        return ""
    try:
        return f.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _external_base(request: Request) -> str:
    """Shared proxy-aware base-URL derivation — see ``core.utils.external_base``."""
    return external_base(request)


def _dist_url(base: str, name: str) -> str:
    return f"{base}/api/v1/runners/dist/{name}"


def _archive_entry(base: str, archive: str) -> dict | None:
    """URL + integrity/signature URLs for one bare archive, or None if absent."""
    if _dist_file(archive) is None:
        return None
    entry: dict = {"url": _dist_url(base, archive)}
    if _dist_file(archive + ".sha256") is not None:
        entry["sha256_url"] = _dist_url(base, archive + ".sha256")
    if _dist_file(archive + ".minisig") is not None:
        entry["signature_url"] = _dist_url(base, archive + ".minisig")
    return entry


def _artifacts_table(base: str) -> dict[str, dict[str, dict]]:
    """Present bare-binary archives as ``{os: {arch: {url, ...}}}``.

    Only what actually exists on disk is advertised; the integrity/signature
    URLs are included per-file so a partially staged dist degrades to "no
    update offered" rather than a broken download.

    Where a headless build exists for that (os, arch) it hangs off the same
    entry under ``headless`` — same machine, other flavor. A headless runner
    reads that sub-entry and nothing else, so it can never be handed the
    desktop archive its host cannot run.
    """
    table: dict[str, dict[str, dict]] = {}
    for (os_key, arch_key), archive in _BARE_ARCHIVES.items():
        entry = _archive_entry(base, archive)
        if entry is None:
            continue
        headless = _HEADLESS_ARCHIVES.get((os_key, arch_key))
        if headless is not None:
            headless_entry = _archive_entry(base, headless)
            if headless_entry is not None:
                entry["headless"] = headless_entry
        table.setdefault(os_key, {})[arch_key] = entry
    return table


def _platform_entry(base: str, os_key: str, arch: str) -> dict:
    """Installer (+ Linux package formats) for one (OS, arch) pair."""
    asset = _PLATFORM_INSTALLER[(os_key, arch)]
    entry: dict = {
        "installer_url": (
            _dist_url(base, asset) if _dist_file(asset) is not None else None
        )
    }
    if os_key == "linux":
        entry["formats"] = {}
        for fmt in _LINUX_FORMAT_NAMES:
            name = _LINUX_FORMATS[(arch, fmt)]
            entry["formats"][fmt] = (
                _dist_url(base, name) if _dist_file(name) is not None else None
            )
    return entry


def _platforms_table(base: str) -> dict[str, dict]:
    """Per-OS installer links for the Profile UI (null when not embedded).

    Each entry keeps the flat ``installer_url`` / ``formats`` fields the UI has
    always read — they describe the default (x64) build — and adds an
    ``architectures`` map carrying one entry per architecture we ship. macOS
    reports ``universal: true``: its single binary already covers both Mac
    architectures, so there is no second download to offer there.
    """
    table: dict[str, dict] = {}
    for os_key in _RUNNER_PLATFORMS_WITH_DIST:
        arches = {a: _platform_entry(base, os_key, a) for a in _RUNNER_ARCHES}
        entry = dict(arches[_DEFAULT_ARCH])
        entry["architectures"] = arches
        entry["universal"] = (
            len({_PLATFORM_INSTALLER[(os_key, a)] for a in _RUNNER_ARCHES}) == 1
        )
        table[os_key] = entry
    return table


def _scripts_table(base: str) -> dict[str, str | None]:
    """Headless install-script URLs (null when not embedded).

    ``sh`` covers macOS + Linux (the script detects the OS and installs a
    LaunchAgent / systemd --user service); ``ps1`` is the Windows counterpart.
    The Profile UI renders its command-line install instructions from these,
    so a deployment that does not serve the scripts never shows commands that
    would 404.
    """
    return {
        key: (_dist_url(base, name) if _dist_file(name) is not None else None)
        for key, name in (
            ("sh", "xtm-one-runner-installer.sh"),
            ("ps1", "xtm-one-runner-installer.ps1"),
        )
    }


@router.get("/update-manifest")
async def update_manifest(
    request: Request,
    os_name: str | None = Query(None, alias="os"),
    arch: str | None = Query(None),
    current_version: str | None = Query(None),
    # Plain default, not Query(None), on purpose: FastAPI reads it as a query
    # parameter either way, but a Query(...) default materialises as the Query
    # OBJECT when the coroutine is called directly - which the alias below and
    # the tests do. `flavor` is the one parameter callers routinely omit, and
    # a Query object reaching the validation below would blow up on .strip().
    flavor: str | None = None,
):
    """Downloads + self-update manifest for the embedded runner build.

    Two consumers share it:

    * The XTM One Desktop page renders the installer buttons + version badge
      from ``platforms`` / ``download_configured``.
    * The daemon self-updater and the headless install scripts call it with
      ``?os=&arch=&current_version=`` and read the ``artifacts`` table plus the
      per-machine ``target`` decision block.

    An update is only advertised (``update_available``) when the caller is
    provably OLDER than the embedded build AND the archive carries both its
    checksum and its minisign signature — the runner refuses unsigned code, so
    offering an unverifiable build would only produce retry noise.

    ``?flavor=headless`` resolves ``target`` to the headless archive for that
    machine. A headless runner reports it on every check, and a host without a
    headless build simply gets no target — far better than updating a server
    into a desktop binary it cannot even load. An unrecognised flavor
    is rejected rather than defaulted: falling back to ``desktop`` on a typo
    would hand a server the one build that cannot start, which is the whole
    failure this endpoint exists to prevent.
    """
    _require_enabled()
    requested_flavor = (flavor or _DEFAULT_FLAVOR).strip().lower()
    if requested_flavor not in _RUNNER_FLAVORS:
        raise HTTPException(
            status_code=400, detail=f"Unknown runner flavor '{flavor}'"
        )
    base = _external_base(request)
    dist_version = _dist_version()
    artifacts = _artifacts_table(base)
    platforms = _platforms_table(base)

    out: dict = {
        "version": settings.app_version,
        "runner_version": dist_version or None,
        "min_supported_version": settings.runner_min_supported_version or None,
        "download_configured": any(
            p["installer_url"] is not None for p in platforms.values()
        ),
        "platforms": platforms,
        "artifacts": artifacts,
        "install_scripts": _scripts_table(base),
    }
    if os_name and arch:
        entry = artifacts.get(os_name, {}).get(arch, {})
        if requested_flavor == "headless":
            entry = entry.get("headless") or {}
        update_available = bool(
            entry.get("url")
            and entry.get("sha256_url")
            and entry.get("signature_url")
            and dist_version
            and runner_version_below(current_version, dist_version)
        )
        out["target"] = {
            "flavor": requested_flavor,
            "version": dist_version or None,
            "url": entry.get("url"),
            "sha256_url": entry.get("sha256_url"),
            "signature_url": entry.get("signature_url"),
            "update_available": update_available,
            "needs_update": runner_version_below(
                current_version, settings.runner_min_supported_version
            ),
        }
    return out


@router.get("/download/manifest")
async def download_manifest(
    request: Request,
    os_name: str | None = Query(None, alias="os"),
    arch: str | None = Query(None),
    current_version: str | None = Query(None),
    flavor: str | None = None,
):
    """Alias of ``/update-manifest`` kept for API stability."""
    return await update_manifest(request, os_name, arch, current_version, flavor)


@router.api_route("/dist/{asset}", methods=["GET", "HEAD"])
async def download_dist_asset(asset: str):
    """Serve one embedded distribution artifact by its stable name.

    The path segment is matched against a fixed allow-list of artifact names
    and never used as a filesystem path, so there is no traversal surface.
    Unauthenticated on purpose: installers, integrity files and install
    scripts are public release artifacts.

    HEAD is served alongside GET (``FileResponse`` sends headers only): the
    download surface is probed by download managers, link checkers and
    browser extensions before they fetch, and a 405 there makes an
    intercepted click die with no download and no visible error.
    """
    _require_enabled()
    if asset not in _DIST_ASSETS:
        raise HTTPException(status_code=404, detail="Unknown artifact")
    f = _dist_file(asset)
    if f is None:
        raise HTTPException(status_code=404, detail=_NO_DIST_DETAIL)
    return FileResponse(f, filename=asset)


def _redirect_to_asset(asset: str) -> RedirectResponse:
    """307 to the stable ``/dist/{asset}`` URL (or 404 when not embedded).

    A redirect (rather than serving bytes here) keeps one canonical URL per
    artifact and lets the install scripts discover the sibling ``.sha256`` by
    appending it to the redirect target they landed on.
    """
    if _dist_file(asset) is None:
        raise HTTPException(status_code=404, detail=_NO_DIST_DETAIL)
    return RedirectResponse(url=with_base_path(f"/api/v1/runners/dist/{asset}"))


@router.api_route("/download/{platform}/{fmt}", methods=["GET", "HEAD"])
async def download_runner_format(platform: str, fmt: str, arch: str = _DEFAULT_ARCH):
    """A specific artifact form.

    ``fmt`` is one of:

    * an alternate Linux package format (``appimage`` / ``deb`` / ``rpm``),
      whose architecture comes from ``?arch=``, defaulting to x64;
    * ``headless`` (Linux), the flavor that links no desktop stack and is the
      only build that runs on a server;
    * an architecture (``x64`` / ``arm64``), which resolves to the desktop
      bare-binary archive for ``{platform}/{arch}``.
    """
    _require_enabled()
    if platform == "linux" and fmt == "headless":
        asset = _HEADLESS_ARCHIVES.get((platform, arch))
        if asset is None:
            raise HTTPException(status_code=404, detail="Unknown architecture")
        return _redirect_to_asset(asset)
    if platform == "linux" and fmt in _LINUX_FORMAT_NAMES:
        asset = _LINUX_FORMATS.get((arch, fmt))
        if asset is None:
            raise HTTPException(status_code=404, detail="Unknown architecture")
        return _redirect_to_asset(asset)
    archive = _BARE_ARCHIVES.get((platform, fmt))
    if archive is None:
        raise HTTPException(status_code=404, detail="Unknown platform/format")
    return _redirect_to_asset(archive)


@router.api_route("/download/{platform}", methods=["GET", "HEAD"])
async def download_runner(platform: str, arch: str = _DEFAULT_ARCH):
    """Redirect to the default one-click installer for a platform.

    ``?arch=`` selects the build (x64 by default, ``arm64`` for ARM desktops);
    macOS resolves both to its universal bundle.
    """
    _require_enabled()
    if platform not in _RUNNER_PLATFORMS_WITH_DIST:
        raise HTTPException(status_code=404, detail="Unknown platform")
    asset = _PLATFORM_INSTALLER.get((platform, arch))
    if asset is None:
        raise HTTPException(status_code=404, detail="Unknown architecture")
    return _redirect_to_asset(asset)


@router.get("/updater/{target}/{arch}/{current_version}")
async def runner_updater(
    request: Request, target: str, arch: str, current_version: str
):
    """Tauri v2 updater endpoint the GUI app's bundle updater polls.

    The updater substitutes its own ``{target}`` / ``{arch}`` /
    ``{current_version}`` into this path. The manifest is built dynamically
    from the embedded artifacts: the version comes from the dist ``VERSION``
    marker, each platform entry carries the artifact URL on THIS instance plus
    the detached minisign signature CI produced, and the updater verifies the
    signature itself against its compiled-in public key (the platform never
    holds the private key).

    ``204 No Content`` when no signed updater artifacts are embedded (source
    build, or CI built without a signing key), which the Tauri updater reads
    as "no update available". The path parameters are matched by the updater
    against the manifest's own keys, so they are not needed server-side.
    """
    _require_enabled()
    version = _dist_version()
    base = _external_base(request)
    platforms: dict[str, dict[str, str]] = {}
    for key, artifact in (*_UPDATER_ARTIFACTS.items(), *_UPDATER_INSTALLER_ARTIFACTS.items()):
        sig = _dist_file(artifact + ".sig")
        if sig is None or _dist_file(artifact) is None:
            continue
        try:
            signature = sig.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        platforms[key] = {"signature": signature, "url": _dist_url(base, artifact)}
    if not version or not platforms:
        return Response(status_code=204)
    marker = _dist_file("VERSION")
    pub_date = datetime.fromtimestamp(marker.stat().st_mtime, tz=UTC).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    return {
        "version": version,
        "notes": f"XTM One Desktop {version}",
        "pub_date": pub_date,
        "platforms": platforms,
    }


# NOTE: this single-segment dynamic GET route is declared LAST on purpose.
# Starlette matches routes in declaration order, so a ``GET /{runner_id}`` placed
# before the static runner-daemon GET routes (``/poll``, ``/update-manifest``)
# would swallow them as ``runner_id`` and 404 them. Keeping it after every
# static GET route lets those resolve to their own handlers.
@router.get("/{runner_id}")
async def get_runner(
    runner_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    _require_enabled()
    runner, group_ids = await _get_visible_runner(runner_id, user, db)
    return serialize_runner(
        runner, can_manage=can_manage(user, runner, group_ids)
    )
