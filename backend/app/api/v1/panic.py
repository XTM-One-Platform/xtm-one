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

"""Emergency stop ("panic button") — ``/api/v1/panic``.

Deliberately NOT behind ``require_ai_enabled`` or the agentic licence gate:
the one moment this endpoint matters is when something is running that
should not be, and a gate that refuses the stop because AI is "disabled"
(the stop itself disables it) would make the second press fail.

The client gates the call behind a typed challenge code; the server does not
re-verify that code — it is a UI safeguard against a slip of the mouse, not
an authentication factor. Authorization is the session: a user may stop their
own activity, an administrator may also stop the platform's.
"""

from __future__ import annotations

import logging
import uuid
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, require_admin
from app.core.config import settings
from app.core.database import get_db
from app.core.rate_limit import enforce_rate_limit
from app.models.user import User
from app.services import panic

logger = logging.getLogger(__name__)

router = APIRouter()

PanicScope = Literal["self", "platform"]


class EmergencyStopRequest(BaseModel):
    scope: PanicScope = "self"


def _non_blank(value: str) -> str:
    """A reason made of whitespace would pass ``min_length`` and land in the
    audit trail as an empty explanation — refuse it at the boundary."""
    value = value.strip()
    if not value:
        raise ValueError("reason must not be blank")
    return value


class EmergencyResumeRequest(BaseModel):
    scope: PanicScope = "self"
    #: Why the stop is lifted — asked before acting, recorded on every row.
    reason: str = Field(min_length=1, max_length=2000)

    _strip_reason = field_validator("reason")(_non_blank)


class EmergencyStopReasonRequest(BaseModel):
    #: Why the stop was pressed — asked right AFTER the stop, never before it.
    reason: str = Field(min_length=1, max_length=2000)

    _strip_reason = field_validator("reason")(_non_blank)


class EmergencyStopReasonResponse(BaseModel):
    audit_id: str
    assignments_annotated: int


class EmergencyStopResponse(BaseModel):
    scope: PanicScope
    runs_cancelled: int
    executions_cancelled: int
    sub_agent_tasks_cancelled: int
    chat_turns_cancelled: int
    flow_runs_cancelled: int
    runner_commands_cancelled: int
    evaluation_runs_cancelled: int
    autonomous_runs_cancelled: int
    assignments_paused: int
    ai_disabled: bool
    #: Runs admitted while the stop was in flight and cancelled by the
    #: post-commit sweep — already counted in ``runs_cancelled``.
    late_admissions_cancelled: int = 0
    #: ``False`` when cancelling the live work failed after the door was
    #: closed: the post-commit sweeps cancelled what they reach, and the
    #: counts are what was really halted.
    completed: bool = True
    #: Hand this to ``POST /panic/stop/{audit_id}/reason`` once the operator
    #: has said why.
    audit_id: str | None
    stopped_at: str | None


class EmergencyResumeResponse(BaseModel):
    scope: PanicScope
    assignments_resumed: int
    ai_enabled: bool


class LastEmergencyStop(BaseModel):
    at: str | None
    by: str | None
    scope: PanicScope | None
    reason: str | None


class EmergencyTokenStatus(BaseModel):
    configured: bool
    prefix: str | None
    created_at: str | None
    created_by: str | None
    last_used_at: str | None


class EmergencyTokenCreated(BaseModel):
    #: Shown exactly once; only a bcrypt hash is kept.
    token: str
    prefix: str
    created_at: str
    created_by: str


class EmergencyTokenStopRequest(BaseModel):
    #: Optional here — a runbook usually states the cause, a monitoring hook
    #: may not. Recorded on the audit row when given.
    reason: str | None = Field(default=None, max_length=2000)


class EmergencyStopStatus(BaseModel):
    #: Assignments a ``self`` resume would re-enable.
    self_paused_assignments: int
    #: The caller's own stop is in effect and still covers a paused
    #: assignment (one another open stop may cover too): their resume stays
    #: on offer even when it would re-enable nothing yet.
    self_stop_active: bool = False
    #: Assignments a ``platform`` resume would re-enable. ``None`` for non-admins.
    platform_paused_assignments: int | None
    #: Whether the platform AI override is currently OFF. ``None`` for non-admins.
    ai_disabled: bool | None
    #: A platform-wide stop is in effect (latest platform stop not yet lifted).
    #: Visible to every viewer, admin or not.
    platform_stop_active: bool
    #: A stop is in effect for the caller (the platform one, or their own).
    active: bool
    #: Who pressed the most recent stop that concerns the caller, when and why.
    last_stop: LastEmergencyStop | None


def _require_scope(user: User, scope: PanicScope) -> None:
    if scope == "platform" and not user.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin privileges required for the platform scope",
        )


@router.get("/status", response_model=EmergencyStopStatus)
async def emergency_stop_status(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> EmergencyStopStatus:
    """What a resume would touch — lets the menu offer "resume" only when relevant."""
    return EmergencyStopStatus(**await panic.emergency_stop_status(db, actor=user))


@router.post("/stop", response_model=EmergencyStopResponse)
async def emergency_stop(
    body: EmergencyStopRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> EmergencyStopResponse:
    """Halt every agentic activity in scope and pause its assignments.

    ``self``: everything running for the caller. ``platform`` (admin): every
    user's activity, the ownerless surfaces (evaluations, autonomous runs) and
    the platform AI kill switch. Commits, then runs the after-commit work
    (cache invalidation, partner notifications, live-update frames).
    """
    _require_scope(user, body.scope)
    # Two transactions on purpose. The first closes the door — assignments
    # paused, AI off — and is committed so every replica sees it; the second
    # cancels what is live, which is then the complete set: nothing can be
    # admitted between the two. The post-commit sweep stays as the backstop
    # for a replica that read the pre-commit state a moment earlier.
    outcome = await panic.arm_admission_barrier(
        db, actor=user, scope=body.scope, request=request
    )
    await db.commit()
    if outcome.ai_disabled:
        from app.services import ai_state

        ai_state.invalidate_cache()
    # Commits; a failure past the barrier is rolled back and reported as
    # ``completed = False``, and the after-commit sweeps still run.
    outcome = await panic.cancel_live_work(
        db, actor=user, scope=body.scope, request=request, outcome=outcome
    )
    await panic.after_commit(outcome)
    return EmergencyStopResponse(**outcome.response())


@router.post("/stop/{audit_id}/reason", response_model=EmergencyStopReasonResponse)
async def emergency_stop_reason(
    audit_id: uuid.UUID,
    body: EmergencyStopReasonRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> EmergencyStopReasonResponse:
    """Record why a stop was pressed, after the fact.

    The stop never waits for this: the client shows the reason form once the
    stop has succeeded, and this call annotates the audit row it returned
    (and the assignments it paused). Only the operator who pressed it can."""
    try:
        annotated = await panic.record_stop_reason(
            db, actor=user, audit_id=audit_id, reason=body.reason
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    await db.commit()
    return EmergencyStopReasonResponse(
        audit_id=str(audit_id), assignments_annotated=annotated
    )


@router.post("/resume", response_model=EmergencyResumeResponse)
async def emergency_stop_resume(
    body: EmergencyResumeRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> EmergencyResumeResponse:
    """Re-enable what an emergency stop paused in scope (and, for the
    platform scope, lift the AI kill switch). Assignments paused by hand are
    left alone. The reason is mandatory and lands on every audit row."""
    _require_scope(user, body.scope)
    try:
        outcome = await panic.resume_after_emergency_stop(
            db, actor=user, scope=body.scope, reason=body.reason, request=request
        )
    except panic.PlatformStopActiveError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "platform_stop_active", "message": str(exc)},
        ) from exc
    await db.commit()
    await panic.after_resume_commit(outcome)
    return EmergencyResumeResponse(**outcome.counts())


# ---------------------------------------------------------------------------
# The token path: an emergency stop without a session
# ---------------------------------------------------------------------------

#: ``auto_error=False`` so a missing header reaches the handler and is logged
#: as a refused attempt like a wrong token, instead of a bare 403 from FastAPI.
_emergency_bearer = HTTPBearer(auto_error=False)


@router.get("/token", response_model=EmergencyTokenStatus)
async def emergency_stop_token_status(
    db: AsyncSession = Depends(get_db),
    _admin: User = Depends(require_admin),
) -> EmergencyTokenStatus:
    """Whether an emergency stop API token exists (never the token itself)."""
    return EmergencyTokenStatus(**await panic.emergency_token_status(db))


@router.post("/token", response_model=EmergencyTokenCreated)
async def emergency_stop_token_generate(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_admin),
) -> EmergencyTokenCreated:
    """Generate — or rotate — the emergency stop API token (admin only).

    The token is returned exactly once. Print the procedure now: only a
    bcrypt hash is stored, so it can never be shown again."""
    created = await panic.generate_emergency_token(db, actor=user, request=request)
    await db.commit()
    return EmergencyTokenCreated(**created)


@router.delete("/token", status_code=status.HTTP_204_NO_CONTENT)
async def emergency_stop_token_revoke(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_admin),
) -> None:
    """Revoke the emergency stop API token (admin only)."""
    if not await panic.revoke_emergency_token(db, actor=user, request=request):
        raise HTTPException(status_code=404, detail="No emergency stop token configured")
    await db.commit()


@router.post("/emergency", response_model=EmergencyStopResponse)
async def emergency_stop_by_token(
    request: Request,
    body: EmergencyTokenStopRequest | None = None,
    credentials: HTTPAuthorizationCredentials | None = Depends(_emergency_bearer),
    db: AsyncSession = Depends(get_db),
) -> EmergencyStopResponse:
    """Press the emergency stop with the API token — no session needed.

    ``Authorization: Bearer xes-…``. Always the platform scope: cancels every
    run, background task, chat turn, flow run and pending runner command,
    pauses every assignment and switches AI off. Attributed to the
    administrator who minted the token. A wrong or missing token is refused
    with 401 and recorded as a security event."""
    # Counted per client IP before the token is looked at: probing the token
    # is what this ceiling makes slow and visible (each refusal is also a
    # security event). Fail-closed like the other pre-auth limiters, except
    # for a request carrying the current token's prefix: the stop must not be
    # locked out by whoever spent the ceiling from the same address, nor by a
    # Redis outage.
    raw = credentials.credentials if credentials else None
    try:
        await enforce_rate_limit(
            request,
            "panic_emergency",
            limit=settings.panic_emergency_rate_limit,
            window_seconds=settings.panic_emergency_rate_window_seconds,
            fail_closed=True,
        )
    except HTTPException as exc:
        if exc.status_code not in (
            status.HTTP_429_TOO_MANY_REQUESTS,
            status.HTTP_503_SERVICE_UNAVAILABLE,
        ) or not await panic.carries_current_token_prefix(db, raw):
            raise
    actor = await panic.resolve_emergency_token(db, raw, request=request)
    if actor is None:
        await db.commit()  # the refusal is in the trail even though nothing else is
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing emergency stop token",
        )
    # Commits both phases itself, like the session route.
    outcome = await panic.emergency_stop_with_token(
        db, actor=actor, reason=body.reason if body else None, request=request
    )
    await panic.after_commit(outcome)
    return EmergencyStopResponse(**outcome.response())
