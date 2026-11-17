# SPDX-License-Identifier: Apache-2.0

"""``POST /api/v1/platform/access-notices``: tell someone a registered platform gave them access.

A platform calls it on behalf of the administrator who gave someone access
(OpenCRQ: a member added to a tenant), so the person learns where to sign in:
an access email when they have an XTM One account, an invitation to create one
when they do not and the acting user may invite.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, EmailStr, Field, field_validator, model_validator
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_db
from app.api.v1.helpers import parse_path_id
from app.core.rate_limit import enforce_rate_limit
from app.core.security_options import security_option
from app.models.user import User
from app.services import token_store
from app.services.audit import log_audit
from app.services.email import (
    SUPPLIED_NAME_MAX_LENGTH,
    access_granted_email,
    platform_invitation_email,
    send_email,
)
from app.services.platform_access_notices import (
    NOTICE_PATH,
    NoticePlatform,
    acting_platform,
    invitation_destination,
    may_invite,
    notice_link,
    notice_made,
)

router = APIRouter()

#: Notices and invitations one administrator may send through a platform per
#: hour: people are added one by one, so a large first onboarding fits and a
#: runaway loop does not.
SENDER_LIMIT_PER_HOUR = 200
#: Notices and invitations one registered platform may send per hour, all its administrators together.
PLATFORM_LIMIT_PER_HOUR = 1000
#: Notices or invitations one person may receive per hour, whoever sends them.
RECIPIENT_LIMIT_PER_HOUR = 5
RECIPIENT_NOT_FOUND = "User not found"
INVITE_REFUSED = "You may not invite someone without an account"
PENDING_INVITATION = "A pending invitation already exists for this email. Revoke it before sending a new one."


class AccessNoticeRequest(BaseModel):
    recipient_user_id: str | None = None
    recipient_email: EmailStr | None = None
    scope_name: str = Field(min_length=1, max_length=SUPPLIED_NAME_MAX_LENGTH)
    path: str = Field(default="/", max_length=512)

    @field_validator("scope_name")
    @classmethod
    def _one_printable_line(cls, value: str) -> str:
        value = value.strip()
        if not value or not value.isprintable():
            raise ValueError("scope_name must be one line of printable text")
        return value

    @field_validator("path")
    @classmethod
    def _path_on_the_platform(cls, value: str) -> str:
        if not NOTICE_PATH.fullmatch(value):
            raise ValueError("path must be an absolute path on the platform, such as /tenant-id")
        return value

    @model_validator(mode="after")
    def _one_recipient(self) -> AccessNoticeRequest:
        if (self.recipient_user_id is None) == (self.recipient_email is None):
            raise ValueError("Give exactly one of recipient_user_id and recipient_email")
        return self


class AccessNoticeResponse(BaseModel):
    #: ``notified``: an account holder got the access email; ``invited``: an
    #: invitation to create an account was made (or reused) and emailed, or
    #: handed back as ``link`` when it could not be.
    outcome: Literal["notified", "invited"]
    email_sent: bool
    #: The recipient's XTM One id, when ``notified``.
    user_id: str | None = None
    #: The invitation's registration link, when ``invited`` and not emailed:
    #: only its hash is stored, so this answer is the one place it survives.
    #: Never once emailed, as ``POST /invitations`` returns it only unemailed.
    link: str | None = None


async def _recipient(db: AsyncSession, body: AccessNoticeRequest) -> User | None:
    if body.recipient_email is not None:
        email = str(body.recipient_email).strip().lower()
        return (await db.execute(select(User).where(func.lower(User.email) == email))).scalar_one_or_none()
    return await db.get(User, parse_path_id(body.recipient_user_id, not_found_detail=RECIPIENT_NOT_FOUND))


async def _charge_recipient(request: Request, bucket: str) -> None:
    await enforce_rate_limit(
        request,
        "platform-access-notice-recipient",
        limit=RECIPIENT_LIMIT_PER_HOUR,
        window_seconds=3600,
        bucket=bucket,
    )


@router.post("/access-notices", response_model=AccessNoticeResponse)
async def send_access_notice(
    body: AccessNoticeRequest,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Tell the recipient that the calling platform gave them access to *scope_name*.

    An active account holder (by id or email) gets the access email; an email
    no account holds gets an invitation when the acting user may invite (403
    otherwise, nothing created); a pending invitation a notice made is sent
    again, any other pending one for that email answers 409, left as it is.
    ``email_sent`` is false when SMTP is not configured or the send failed:
    the platform then tells the person another way, with the invitation's
    ``link`` when one was made.
    """
    platform = await acting_platform(db, user)
    if platform is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only a registered platform can send access notices",
        )
    await enforce_rate_limit(
        request,
        "platform-access-notice-sender",
        limit=SENDER_LIMIT_PER_HOUR,
        window_seconds=3600,
        bucket=str(user.id),
    )
    await enforce_rate_limit(
        request,
        "platform-access-notice-platform",
        limit=PLATFORM_LIMIT_PER_HOUR,
        window_seconds=3600,
        bucket=platform.identifier,
    )

    recipient = await _recipient(db, body)
    if recipient is not None and recipient.is_active:
        return await _notify(db, request, body, user, platform, recipient)
    # A deactivated account answers as the directory shows it, never invited:
    # by id, the 404 of an unknown id (``GET /users/directory?ids=``); by email,
    # the 403 of an unknown email to a user who may not invite (the directory's
    # search shows active accounts only), and to one who may, a 404: an
    # administrator's ``GET /users`` lists deactivated accounts.
    if body.recipient_email is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=RECIPIENT_NOT_FOUND)
    if not may_invite(user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=INVITE_REFUSED)
    if recipient is not None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=RECIPIENT_NOT_FOUND)
    return await _invite(db, request, body, user, platform, str(body.recipient_email).strip().lower())


async def _notify(
    db: AsyncSession,
    request: Request,
    body: AccessNoticeRequest,
    user: User,
    platform: NoticePlatform,
    recipient: User,
) -> AccessNoticeResponse:
    await _charge_recipient(request, str(recipient.id))
    html, text = access_granted_email(
        link=notice_link(platform.public_url, body.path),
        platform_name=platform.title,
        scope_name=body.scope_name,
        granted_by=user.full_name or user.email,
    )
    email_sent = await send_email(
        to=recipient.email,
        subject=f"You have access to {platform.title}",
        html=html,
        text=text,
    )

    await log_audit(
        db,
        user=user,
        action="create",
        entity_type="access_notice",
        entity_id=recipient.id,
        entity_name=recipient.email,
        summary=(
            f"{platform.title} notified '{recipient.email}' of access to '{body.scope_name}'"
            + ("" if email_sent else " (email not sent)")
        ),
        details={"platform": platform.title, "scope_name": body.scope_name, "email_sent": email_sent},
        request=request,
    )
    # Commit before answering: the get_db teardown only commits after the response is sent.
    await db.commit()
    return AccessNoticeResponse(outcome="notified", email_sent=email_sent, user_id=str(recipient.id))


async def _invite(
    db: AsyncSession,
    request: Request,
    body: AccessNoticeRequest,
    user: User,
    platform: NoticePlatform,
    email: str,
) -> AccessNoticeResponse:
    await _charge_recipient(request, email)
    destination = invitation_destination(platform, body.path)
    pending = await token_store.find_invitation_by_email(email)
    if pending is not None and not notice_made(pending):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=PENDING_INVITATION)
    ttl_hours = security_option("invitation_expire_hours")
    issued = (
        await token_store.reissue_invitation(pending["id"], ttl_hours=ttl_hours, destination=destination)
        if pending
        else None
    )
    reused = issued is not None
    if issued is None:
        issued = await token_store.create_invitation(
            email=email,
            role="user",
            created_by_id=user.id,
            ttl_hours=ttl_hours,
            destination=destination,
        )
    raw_token, _token_hash, expires_at = issued

    link = token_store.invitation_link(raw_token)
    hours_left = max(1, math.ceil((expires_at - datetime.now(UTC)).total_seconds() / 3600))
    html, text = platform_invitation_email(
        link=link,
        platform_name=platform.title,
        scope_name=body.scope_name,
        invited_by=user.full_name or user.email,
        expire_hours=hours_left,
    )
    email_sent = await send_email(
        to=email,
        subject=f"You're invited to {platform.title}",
        html=html,
        text=text,
    )

    await log_audit(
        db,
        user=user,
        action="update" if reused else "create",
        entity_type="invitation",
        entity_name=email,
        summary=(
            f"{platform.title} invited '{email}' to '{body.scope_name}'"
            + (" (pending invitation reused)" if reused else "")
            + ("" if email_sent else " (email not sent)")
        ),
        details={
            "platform": platform.title,
            "scope_name": body.scope_name,
            "email_sent": email_sent,
            "reused": reused,
        },
        request=request,
    )
    # Commit before answering: the get_db teardown only commits after the response is sent.
    await db.commit()
    return AccessNoticeResponse(outcome="invited", email_sent=email_sent, link=None if email_sent else link)
