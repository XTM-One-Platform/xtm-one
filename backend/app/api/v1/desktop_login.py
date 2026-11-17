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

"""Browser sign-in handoff for XTM One Desktop - the HTTP half.

Three audiences, one flow (the design and its security argument are in
``app.services.desktop_login``):

* **the Desktop app**, with its runner credential: ``POST /start`` opens a
  pending login, ``POST /exchange`` polls for the session and receives it once;
* **the browser** the SSO ran in, with the freshly issued user session:
  ``GET /{login_id}`` reads which machine is asking (for the confirmation the
  login page shows), ``POST /{login_id}/complete`` hands the session over,
  ``POST /{login_id}/decline`` refuses and drops the request (audited);
* nobody else: every route is either runner-authenticated, user-authenticated
  with a first-hand browser session, or (the read) answers only what the
  confirmation page needs to show.

Gated on ``settings.runners_enabled`` like the rest of the runner surface: no
runner subsystem, no Desktop app, no handoff.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_runner, get_current_user_ungated, get_current_user_unlocked
from app.core.config import settings
from app.core.database import get_db
from app.core.security import create_access_token
from app.core.sessions import AUTH_TIME_CLAIM, session_started_at
from app.models.runner import Runner
from app.models.user import User
from app.services import desktop_login as svc
from app.services.audit import log_audit
from app.services.desktop_session import load_desktop_context

logger = logging.getLogger(__name__)

router = APIRouter()


def _require_enabled() -> None:
    if not settings.runners_enabled:
        raise HTTPException(status_code=404, detail="Runners are disabled")


class ExchangeRequest(BaseModel):
    login_id: str


async def _enrolled_by(db: AsyncSession, runner: Runner) -> str | None:
    """The display name of the user who enrolled the machine - what the
    confirmation page shows next to its name; ``None`` for a company-managed
    machine (no owner)."""
    if runner.is_company_managed or runner.user_id is None:
        return None
    owner = await db.get(User, runner.user_id)
    return owner.full_name if owner is not None else None


@router.post("/start", status_code=status.HTTP_201_CREATED)
async def start_desktop_login(runner: Runner = Depends(get_current_runner), db: AsyncSession = Depends(get_db)):
    """Open a pending browser sign-in for the calling Desktop app.

    Runner-authenticated: the handoff is bound to an enrolled machine from the
    first request, which is what keeps a pasted link from turning someone
    else's sign-in into a session on an attacker's machine.
    """
    _require_enabled()
    record = await svc.start_login(
        runner_id=str(runner.id),
        runner_name=runner.name,
        platform=runner.platform,
        enrolled_by=await _enrolled_by(db, runner),
    )
    return {
        "login_id": record.login_id,
        "expires_in_seconds": svc.LOGIN_TTL_SECONDS,
    }


@router.post("/exchange")
async def exchange_desktop_login(body: ExchangeRequest, runner: Runner = Depends(get_current_runner)):
    """The Desktop app's poll: the session once it is there, ``202`` until then.

    Single-use: a completed record is deleted as it is handed out. ``404`` is
    the terminal "gone" - expired, cancelled, or never this runner's - so the
    app stops polling instead of waiting out a window nothing will fill.
    """
    _require_enabled()
    record = await svc.load_login(body.login_id)
    if record is None or record.runner_id != str(runner.id):
        raise HTTPException(status_code=404, detail="No such pending sign-in")
    if record.status != svc.STATUS_COMPLETED or not record.user_id:
        return JSONResponse(status_code=status.HTTP_202_ACCEPTED, content={"status": svc.STATUS_PENDING})
    # GETDEL: of two polls that both read ``completed`` above, exactly one
    # receives the record here. The binding and the state are re-read off the
    # consumed value rather than trusted from the read a moment earlier.
    consumed = await svc.consume_login(body.login_id)
    if (
        consumed is None
        or consumed.runner_id != str(runner.id)
        or consumed.status != svc.STATUS_COMPLETED
        or not consumed.user_id
    ):
        # Consumed by a concurrent poll a moment ago: the first one won.
        raise HTTPException(status_code=404, detail="No such pending sign-in")
    # Minted here, for the user who confirmed, at the one moment it is handed
    # out - Redis never held a token, and none exists for a claim never made.
    logger.info(
        "desktop login %s exchanged by runner %s for user %s",
        body.login_id[:8],
        runner.id,
        consumed.user_id,
    )
    # The app's own session (its own idle deadline), counting its absolute
    # lifetime from the confirming browser's sign-in and ending no later than
    # that session: a handoff never extends a session.
    carried = {AUTH_TIME_CLAIM: consumed.auth_time, "exp": consumed.expires_at}
    continues = {name: value for name, value in carried.items() if value is not None} or None
    return {
        "status": svc.STATUS_COMPLETED,
        "access_token": create_access_token(consumed.user_id, continues=continues),
    }


@router.get("/{login_id}")
async def read_desktop_login(login_id: str):
    """What the browser's confirmation page shows: which machine is asking.

    Unauthenticated (the person may not be signed in yet when the page first
    renders) and deliberately thin: the machine's display name, its platform
    and who enrolled it - never the runner id, never who confirmed, and not
    the state either, so the id alone is not an oracle for whether someone
    signed in.
    """
    _require_enabled()
    record = await svc.load_login(login_id)
    if record is None:
        raise HTTPException(status_code=404, detail="This sign-in request has expired")
    return {
        "runner_name": record.runner_name,
        "platform": record.platform,
        "enrolled_by": record.enrolled_by,
        "expires_in_seconds": await svc.remaining_ttl(login_id),
    }


def _require_first_hand_session(user: User) -> None:
    """The bearer must be the browser's own, first-hand session.

    ``get_current_user_ungated`` also resolves ``fcp-`` API keys, trusted
    cross-platform JWTs and impersonation sessions - none of which may become
    a plain 7-day session on a machine: the handoff mints a token carrying
    none of their marks (no confinement, no ``impersonated_by``), so an
    impersonating admin would escape the banner and the trail, and a leaked
    ToU-exempt API key plus any runner credential would become an interactive
    login. Same rule the platform applies to minting API keys while
    impersonating; ``auth_credential_kind`` is the tag ``deps`` sets.
    """
    if (
        getattr(user, "auth_credential_kind", None) != "session_jwt"
        or getattr(user, "impersonated_by", None) is not None
    ):
        raise HTTPException(
            status_code=403,
            detail=(
                "Sign in with your own browser session to hand it to XTM One "
                "Desktop - not with an API key, a platform token or while "
                "impersonating another user."
            ),
        )


def _handoff_allowed(runner: Runner, user: User) -> bool:
    """Whether *user* may land a session on the Desktop app embedding *runner*.

    Stricter than "may target it": the Desktop app on a machine is its
    enroller's app, and the session it receives is the one the app then acts
    with. A group-shared machine is a machine its owner lets colleagues DRIVE
    (``computer_use`` under their own account); it is not a machine a
    colleague should ever sign in ON - a confirmation there would send the
    colleague's session to the owner's polling app, the login-CSRF this flow
    is designed against. So: the runner's own enroller, or a company-managed
    machine (a shared workstation, no owner), and nobody else.
    """
    if runner.is_company_managed:
        return True
    return runner.user_id is not None and runner.user_id == user.id


@router.post("/{login_id}/complete")
async def complete_desktop_login(
    login_id: str,
    request: Request,
    user: User = Depends(get_current_user_unlocked),
    db: AsyncSession = Depends(get_db),
):
    """Hand the browser's session to the Desktop app (the person confirmed).

    The bearer is the session the SSO callback just issued to this browser
    (``_require_first_hand_session``: a real one, not an API key, a platform
    token or an impersonation). The record then remembers WHO confirmed; the
    app's FRESH session is minted by ``exchange`` when it is claimed, never
    forwarded and never stored. Two checks first: the signed-in user must be
    allowed to target the runner the app embeds (visible and active,
    ``load_desktop_context``) AND must be its enroller - or the machine
    company-managed (``_handoff_allowed``). Otherwise the handoff is refused
    with the reason - there is a human reading this page.

    Authentication only, no Terms of Use gate (``get_current_user_unlocked``):
    this IS the sign-in step. With the terms published and not yet accepted,
    the gate would refuse the session before the app ever holds one - so an
    admin could never sign in on the desktop to accept them, and every other
    user would be locked out of the waiting screen too. The app's own requests
    hit the gate normally once it holds the session, exactly like a browser
    that just signed in. A companion locked behind its PIN hands nothing over.
    """
    _require_enabled()
    _require_first_hand_session(user)
    record = await svc.load_login(login_id)
    if record is None:
        raise HTTPException(status_code=404, detail="This sign-in request has expired")
    if record.status != svc.STATUS_PENDING:
        raise HTTPException(status_code=409, detail="This sign-in request was already used")
    ctx = await load_desktop_context(db, user_id=user.id, runner_id=record.runner_id)
    if ctx is None:
        raise HTTPException(
            status_code=403,
            detail=("This machine is not one you can use: it is enrolled by someone else and not shared with you."),
        )
    if not _handoff_allowed(ctx.runner, user):
        raise HTTPException(
            status_code=403,
            detail=(
                "This machine was enrolled by someone else. You can sign in to "
                "XTM One Desktop only on a machine you enrolled yourself or on a "
                "company-managed one."
            ),
        )
    # Audit FIRST, grant second - fail closed. The audit row is the durable
    # account of who put their session on which machine; once the Redis record
    # reads completed the app can claim the session, so a grant that outran a
    # failed audit write would be an unaudited login. The row says what the
    # person DID (confirmed), which stays true if the compare-and-set below
    # then loses to a concurrent confirmation; the exchange log records the
    # session actually minted.
    await log_audit(
        db,
        user=user,
        action="login",
        entity_type="user",
        entity_id=user.id,
        entity_name=user.full_name,
        summary=(
            f"Confirmed a sign-in to XTM One Desktop on '{record.runner_name}' ({user.email})"
        ),
        details={"runner_id": record.runner_id, "login_id": login_id[:8]},
        request=request,
    )
    await db.commit()
    web_session = getattr(user, "web_session", None)
    auth_time = session_started_at(web_session.claims) if web_session is not None else None
    expires_at = web_session.expires_at if web_session is not None else None
    if not await svc.complete_login(record, str(user.id), auth_time=auth_time, expires_at=expires_at):
        raise HTTPException(status_code=409, detail="This sign-in request was already used")
    return {"status": svc.STATUS_COMPLETED, "runner_name": record.runner_name}


@router.post("/{login_id}/decline", status_code=status.HTTP_204_NO_CONTENT)
async def decline_desktop_login(
    login_id: str,
    request: Request,
    user: User = Depends(get_current_user_ungated),
    db: AsyncSession = Depends(get_db),
):
    """"Not my machine": the person refused to hand their session over.

    Drops the pending login so nobody else can complete THIS request within
    its window, and writes the refusal to the audit trail - the page tells the
    person to warn their administrator, and this is what the administrator
    finds. Any signed-in person may decline a request shown to them: it was
    their session being asked for. Idempotent on a record already gone.
    """
    _require_enabled()
    _require_first_hand_session(user)
    record = await svc.load_login(login_id)
    if record is None:
        return
    await svc.cancel_login(login_id)
    # Never ``login``: every reader of a ``login`` row takes it for a sign-in.
    await log_audit(
        db,
        user=user,
        action="login_declined",
        entity_type="user",
        entity_id=user.id,
        entity_name=user.full_name,
        summary=(
            f"Declined a sign-in request from XTM One Desktop on "
            f"'{record.runner_name}' ({user.email})"
        ),
        details={"runner_id": record.runner_id, "login_id": login_id[:8]},
        request=request,
    )
    await db.commit()


@router.delete("/{login_id}", status_code=status.HTTP_204_NO_CONTENT)
async def cancel_desktop_login(login_id: str, runner: Runner = Depends(get_current_runner)):
    """The Desktop app gave up (the person cancelled): drop the pending login."""
    _require_enabled()
    record = await svc.load_login(login_id)
    if record is not None and record.runner_id == str(runner.id):
        await svc.cancel_login(login_id)
