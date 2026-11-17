# SPDX-License-Identifier: Apache-2.0

"""The companion's PIN lock API (``services/app_lock.py``).

These routes depend on ``get_current_user_ungated``: they are the only ones a
locked companion session can reach. ``POST /session`` records the session as a
companion's (and locks it) each time the app opens on a phone; ``POST /unlock`` checks the PIN
(:data:`app_lock.MAX_FAILED_ATTEMPTS` wrong ones end the session);
``POST /lock`` relocks when the app leaves the screen. ``PUT /pin`` chooses or
changes the PIN and ``PATCH`` turns the lock on or off: from a companion
session both need the current PIN; a computer's session is where a forgotten
PIN is reset.
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user_ungated
from app.core.database import get_db
from app.core.rate_limit import enforce_rate_limit
from app.models.user import User
from app.services import app_lock
from app.services.audit import log_audit

logger = logging.getLogger(__name__)
router = APIRouter()

#: PIN checks per person per 15 minutes, every session together (fail closed).
UNLOCKS_PER_WINDOW = 20

Pin = Annotated[str, Field(min_length=1, max_length=16)]


class AppLockStatus(BaseModel):
    applies: bool
    required: bool
    has_pin: bool
    enabled: bool
    companion: bool
    locked: bool
    setup_required: bool
    attempts_left: int
    min_digits: int = app_lock.PIN_MIN_DIGITS
    max_digits: int = app_lock.PIN_MAX_DIGITS


class UnlockRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pin: Pin


class SetPinRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pin: Pin
    current_pin: Pin | None = None


class UpdateLockRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool
    current_pin: Pin | None = None


async def _status(db: AsyncSession, user: User) -> AppLockStatus:
    state = await app_lock.lock_state(db, user)
    return AppLockStatus(**{k: getattr(state, k) for k in AppLockStatus.model_fields if hasattr(state, k)})


async def _require_current_pin_on_companion(db: AsyncSession, user: User, current_pin: str | None, request: Request) -> None:
    """From a companion session, a change needs the current PIN (a phone in the wrong hands): checked like an
    unlock - the same per-person budget, refused when unsure, and a wrong one counted, the last ending the session."""
    state = await app_lock.lock_state(db, user)
    if not (state.companion and state.has_pin):
        return
    await enforce_rate_limit(
        None, "app_lock_unlock", limit=UNLOCKS_PER_WINDOW, window_seconds=900, bucket=str(user.id), fail_closed=True
    )
    if await app_lock.check_current_pin(db, user, current_pin):
        return
    after = await app_lock.lock_state(db, user)
    ended = after.attempts_left <= 0
    if ended:
        await log_audit(
            db,
            user=user,
            action="session_ended",
            entity_type="app_lock",
            entity_id=user.id,
            entity_name=None,
            summary="Ended a phone session after too many wrong PINs",
            details={"attempts": app_lock.MAX_FAILED_ATTEMPTS},
            request=request,
        )
    # The count is kept whatever the answer.
    await db.commit()
    if ended:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail={"code": app_lock.ENDED})
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN, detail={"code": "app_pin_wrong", "attempts_left": max(after.attempts_left, 0)}
    )


@router.get("", response_model=AppLockStatus)
async def get_app_lock(user: User = Depends(get_current_user_ungated), db: AsyncSession = Depends(get_db)):
    return await _status(db, user)


@router.post("/session", response_model=AppLockStatus)
async def mark_companion_session(user: User = Depends(get_current_user_ungated), db: AsyncSession = Depends(get_db)):
    """The companion opened on this session: from now on its PIN lock applies to it.

    Every opening locks it again, so the app asks the PIN each time it is
    opened, not only after it was left in the background.
    """
    await app_lock.mark_companion_session(db, user)
    await app_lock.lock(db, user)
    await db.commit()
    return await _status(db, user)


@router.post("/unlock", response_model=AppLockStatus)
async def unlock_app(
    body: UnlockRequest,
    request: Request,
    user: User = Depends(get_current_user_ungated),
    db: AsyncSession = Depends(get_db),
):
    await enforce_rate_limit(
        None, "app_lock_unlock", limit=UNLOCKS_PER_WINDOW, window_seconds=900, bucket=str(user.id), fail_closed=True
    )
    unlocked = await app_lock.unlock(db, user, body.pin)
    state_after = await app_lock.lock_state(db, user)
    ended = not unlocked and state_after.companion and state_after.attempts_left <= 0
    if ended:
        await log_audit(
            db,
            user=user,
            action="session_ended",
            entity_type="app_lock",
            entity_id=user.id,
            entity_name=None,
            summary="Ended a phone session after too many wrong PINs",
            details={"attempts": app_lock.MAX_FAILED_ATTEMPTS},
            request=request,
        )
    await db.commit()
    if ended:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail={"code": app_lock.ENDED})
    if not unlocked:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "app_pin_wrong", "attempts_left": max(state_after.attempts_left, 0)},
        )
    return state_after


@router.post("/lock", response_model=AppLockStatus)
async def lock_app(user: User = Depends(get_current_user_ungated), db: AsyncSession = Depends(get_db)):
    await app_lock.lock(db, user)
    await db.commit()
    return await _status(db, user)


@router.put("/pin", response_model=AppLockStatus)
async def set_app_pin(
    body: SetPinRequest,
    request: Request,
    user: User = Depends(get_current_user_ungated),
    db: AsyncSession = Depends(get_db),
):
    """Choose a PIN (it turns the lock on), or change it."""
    await _require_current_pin_on_companion(db, user, body.current_pin, request)
    try:
        await app_lock.set_pin(db, user, body.pin, enabled=True)
    except app_lock.PinError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc
    # A companion session that just chose its PIN is unlocked by it.
    if (await app_lock.lock_state(db, user)).companion:
        await app_lock.unlock(db, user, body.pin)
    await log_audit(
        db,
        user=user,
        action="update",
        entity_type="app_lock",
        entity_id=user.id,
        entity_name=None,
        summary="Set the mobile companion PIN",
        details={},
        request=request,
    )
    await db.commit()
    return await _status(db, user)


@router.patch("", response_model=AppLockStatus)
async def update_app_lock(
    body: UpdateLockRequest,
    request: Request,
    user: User = Depends(get_current_user_ungated),
    db: AsyncSession = Depends(get_db),
):
    """Turn the lock on or off (off is refused while the policy requires it)."""
    if not body.enabled and app_lock.pin_required():
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Your organization requires a PIN.")
    await _require_current_pin_on_companion(db, user, body.current_pin, request)
    pin = await app_lock.load_pin(db, user.id)
    if pin is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Choose a PIN first.")
    pin.enabled = body.enabled
    await log_audit(
        db,
        user=user,
        action="update",
        entity_type="app_lock",
        entity_id=user.id,
        entity_name=None,
        summary=f"Turned the mobile companion PIN {'on' if body.enabled else 'off'}",
        details={"enabled": body.enabled},
        request=request,
    )
    await db.commit()
    return await _status(db, user)
