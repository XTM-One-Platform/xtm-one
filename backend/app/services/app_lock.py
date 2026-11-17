# SPDX-License-Identifier: Apache-2.0

"""The companion's PIN lock: nothing shown, nothing approved until the PIN is entered.

The mobile companion can be locked behind a PIN the person chooses, so that a
phone picked up by someone else shows nothing and decides nothing. It is not a
second sign-in factor: the person is already signed in on the phone; the PIN
keeps their data out of reach of whoever holds it.

The lock is enforced by the platform, not only drawn by the app:

* when the companion boots on a phone it records its web session
  (``mark_companion_session``); that record is permanent for the session;
* while the PIN applies to the person (their choice, or the
  ``companion_pin_required`` policy) and the session is not unlocked, every
  request of that session is refused with **423 Locked** (:func:`enforce`) on
  every surface that accepts a web session: the REST dependency
  (``deps.get_current_user``), the activity stream, the chat WebSocket and
  the MCP OAuth consent (``mcp_oauth._resolve_session_user``: a client's token
  would outlive the lock); only the lock's own endpoints answer
  (``api/v1/app_lock.py``);
* an unlock lasts :data:`UNLOCK_SECONDS` and is extended only by the person's
  own activity (``POST /auth/session/activity``), never by the app's polls;
  the app relocks itself when it leaves the screen;
* :data:`MAX_FAILED_ATTEMPTS` wrong PINs end the session: the phone is signed
  out, whatever its token says. The current PIN a change asks of the phone
  (a new PIN, the lock turned off) counts against the same attempts, under
  the same rate limit (:func:`check_current_pin`);
* a person's phone notifications carry no content and no Approve / Reject
  buttons while the PIN applies (``mobile_push``): a lock screen shows nothing
  and decides nothing either.

A computer's session is never recorded, so the platform on a computer is not
affected; it is also where a forgotten PIN is reset.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.app_lock import AppLockPin, AppLockSession

PIN_MIN_DIGITS = 6
PIN_MAX_DIGITS = 8
#: Wrong PINs that end the session.
MAX_FAILED_ATTEMPTS = 5
#: How long an unlock lasts without the person using the app.
UNLOCK_SECONDS = 10 * 60

#: The 423 the app reads to show its lock screen.
LOCKED = "app_locked"
#: The 423 the app reads to have the person choose a PIN first.
SETUP_REQUIRED = "app_pin_setup_required"
#: The 401 of a session ended by too many wrong PINs.
ENDED = "app_lock_ended"


class PinError(ValueError):
    """A PIN refused, with the reason shown to the person."""


def pin_problem(pin: object) -> str | None:
    """Why ``pin`` cannot be a PIN, or ``None``."""
    if not isinstance(pin, str) or not pin.isdigit() or not pin.isascii():
        return "A PIN is made of digits only."
    if not PIN_MIN_DIGITS <= len(pin) <= PIN_MAX_DIGITS:
        return f"A PIN has {PIN_MIN_DIGITS} to {PIN_MAX_DIGITS} digits."
    if len(set(pin)) == 1:
        return "Choose a PIN that is not one digit repeated."
    digits = [int(d) for d in pin]
    steps = {b - a for a, b in pairwise(digits)}
    if steps in ({1}, {-1}):
        return "Choose a PIN that is not a run of consecutive digits."
    return None


def pin_required() -> bool:
    from app.core.security_options import security_option

    return bool(security_option("companion_pin_required"))


def session_id(user: Any) -> str | None:
    """The ``sid`` of the web session a request comes from, or ``None`` (an API key, a platform JWT)."""
    session = getattr(user, "web_session", None)
    sid = getattr(session, "claims", {}).get("sid") if session is not None else None
    return sid if isinstance(sid, str) and 0 < len(sid) <= 64 else None


async def load_pin(db: AsyncSession, user_id: uuid.UUID) -> AppLockPin | None:
    return (await db.execute(select(AppLockPin).where(AppLockPin.user_id == user_id))).scalar_one_or_none()


@dataclass(frozen=True, slots=True)
class LockState:
    #: The PIN applies to the person (their choice, or the policy).
    applies: bool
    #: The policy makes it mandatory.
    required: bool
    has_pin: bool
    enabled: bool
    #: This session is a companion's.
    companion: bool
    locked: bool
    setup_required: bool
    attempts_left: int


async def lock_state(db: AsyncSession, user: Any, *, now: datetime | None = None) -> LockState:
    moment = now or datetime.now(UTC)
    pin = await load_pin(db, user.id)
    required = pin_required()
    enabled = bool(pin and pin.enabled)
    applies = required or enabled
    sid = session_id(user)
    row = await db.get(AppLockSession, sid) if sid else None
    companion = row is not None and row.user_id == user.id
    locked = companion and applies and (row.unlocked_until is None or row.unlocked_until <= moment)
    return LockState(
        applies=applies,
        required=required,
        has_pin=pin is not None,
        enabled=enabled,
        companion=companion,
        locked=locked,
        setup_required=companion and required and pin is None,
        attempts_left=MAX_FAILED_ATTEMPTS - (row.failed_attempts if companion else 0),
    )


async def enforce(db: AsyncSession, user: Any, *, now: datetime | None = None) -> None:
    """Refuse a request of a locked companion session (423), or of one its PIN ended (401).

    While "MOBILE_COMPANION" is off no lock screen can be shown, so the lock
    never refuses (a phone locked when the flag went off is not locked out); a
    session its PIN ended stays ended (the person signs in again).
    """
    from app.core.feature_flags import is_feature_enabled

    sid = session_id(user)
    if sid is None:
        return
    row = await db.get(AppLockSession, sid)
    if row is None or row.user_id != user.id:
        # Not a companion's session: the platform on a computer, or an
        # impersonation started from a companion session (it carries its sid).
        return
    if row.ended:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail={"code": ENDED})
    if not is_feature_enabled("MOBILE_COMPANION"):
        return
    pin = await load_pin(db, user.id)
    if pin is None:
        if pin_required():
            raise HTTPException(status_code=status.HTTP_423_LOCKED, detail={"code": SETUP_REQUIRED})
        return
    if not (pin.enabled or pin_required()):
        return
    moment = now or datetime.now(UTC)
    if row.unlocked_until is None or row.unlocked_until <= moment:
        raise HTTPException(status_code=status.HTTP_423_LOCKED, detail={"code": LOCKED})


async def mark_companion_session(db: AsyncSession, user: Any) -> bool:
    """Record the request's session as a companion's (once, for good). False when it has no session."""
    sid = session_id(user)
    if sid is None:
        return False
    await db.execute(
        pg_insert(AppLockSession)
        .values(sid=sid, user_id=user.id, failed_attempts=0, ended=False, created_at=datetime.now(UTC))
        .on_conflict_do_nothing(index_elements=[AppLockSession.sid])
    )
    return True


async def _session_row(db: AsyncSession, user: Any, *, for_update: bool = False) -> AppLockSession | None:
    sid = session_id(user)
    if sid is None:
        return None
    query = select(AppLockSession).where(AppLockSession.sid == sid, AppLockSession.user_id == user.id)
    if for_update:
        query = query.with_for_update()
    return (await db.execute(query)).scalar_one_or_none()


async def unlock(db: AsyncSession, user: Any, pin: str, *, now: datetime | None = None) -> bool:
    """Check ``pin`` for the request's companion session: True unlocks it.

    A wrong PIN counts; the :data:`MAX_FAILED_ATTEMPTS`-th ends the session.
    The row is locked so two guesses at once cannot both be the free one.
    """
    from app.core.security import verify_password_async

    moment = now or datetime.now(UTC)
    row = await _session_row(db, user, for_update=True)
    stored = await load_pin(db, user.id)
    if row is None or row.ended or stored is None:
        return False
    if await verify_password_async(pin, stored.pin_hash):
        row.failed_attempts = 0
        row.unlocked_until = moment + timedelta(seconds=UNLOCK_SECONDS)
        return True
    row.failed_attempts += 1
    row.unlocked_until = None
    if row.failed_attempts >= MAX_FAILED_ATTEMPTS:
        row.ended = True
    return False


async def check_current_pin(db: AsyncSession, user: Any, pin: object) -> bool:
    """The current PIN a change asks of a companion session, counted like :func:`unlock`.

    A wrong one counts against the same :data:`MAX_FAILED_ATTEMPTS` (the
    last ends the session); a right one clears the count without unlocking.
    An ended session, or one with no lock row, is refused. Without the count,
    the routes changing the PIN would let a phone in the wrong hands guess it
    without limit.
    """
    from app.core.security import verify_password_async

    row = await _session_row(db, user, for_update=True)
    stored = await load_pin(db, user.id)
    if row is None or row.ended or stored is None:
        return False
    if isinstance(pin, str) and await verify_password_async(pin, stored.pin_hash):
        row.failed_attempts = 0
        return True
    row.failed_attempts += 1
    row.unlocked_until = None
    if row.failed_attempts >= MAX_FAILED_ATTEMPTS:
        row.ended = True
    return False


async def lock(db: AsyncSession, user: Any) -> None:
    row = await _session_row(db, user, for_update=True)
    if row is not None:
        row.unlocked_until = None


async def extend(db: AsyncSession, user: Any, *, now: datetime | None = None) -> None:
    """The person used the app: an unlocked session stays unlocked a while longer."""
    moment = now or datetime.now(UTC)
    row = await _session_row(db, user)
    if row is not None and not row.ended and row.unlocked_until is not None and row.unlocked_until > moment:
        row.unlocked_until = moment + timedelta(seconds=UNLOCK_SECONDS)


async def set_pin(db: AsyncSession, user: Any, pin: str, *, enabled: bool | None = None) -> AppLockPin:
    from app.core.security import hash_password_async

    problem = pin_problem(pin)
    if problem:
        raise PinError(problem)
    hashed = await hash_password_async(pin)
    row = await load_pin(db, user.id)
    if row is None:
        row = AppLockPin(user_id=user.id, pin_hash=hashed, enabled=True if enabled is None else enabled)
        db.add(row)
    else:
        row.pin_hash = hashed
        if enabled is not None:
            row.enabled = enabled
    await db.flush()
    return row


async def pin_applies(db: AsyncSession, user_id: uuid.UUID) -> bool:
    """Whether the person's companion is PIN-locked (their phones then get no content in pushes)."""
    if pin_required():
        return True
    pin = await load_pin(db, user_id)
    return bool(pin and pin.enabled)
