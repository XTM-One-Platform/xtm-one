# SPDX-License-Identifier: Apache-2.0

"""Linked phones API, and the buttons of a phone notification.

``/mobile-devices``: a person links the phone they are signed in on (its Web
Push subscription), lists, tests, renames, mutes and unlinks their phones.
A phone is personal: anyone else gets the 404 of an unknown id. An
administrator may list and unlink a person's phones (offboarding), audited.

``POST /mobile/push-actions``: the Approve / Reject button of a notification,
sent by the phone's service worker with the one-time token the push carried
(``services/push_action_tokens.py``). No session: the token is the
credential. Rate limited per client, refusing when the limiter cannot count
(fail closed). Every refusal answers the same message; the reason is logged.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.api.v1.helpers import parse_path_id
from app.core.database import get_db
from app.core.rate_limit import enforce_rate_limit
from app.core.security import encrypt_value
from app.models.mobile_device import MobileDevice
from app.models.user import User
from app.services.audit import log_audit
from app.services.mobile_push import PushEndpointError, Subscription, get_vapid_keys
from app.services.notification_feed import NOTIFICATION_KINDS

logger = logging.getLogger(__name__)

router = APIRouter()
actions_router = APIRouter()

_NOT_FOUND = "Phone not found"
#: Test notifications a person may send per hour.
TEST_PUSHES_PER_HOUR = 10
#: Button presses a client may send per minute.
PUSH_ACTIONS_PER_MINUTE = 20
#: The one answer to a refused button press.
ACTION_REFUSED = "This action is no longer available. Open the app to decide."

_TIME_RE = r"^([01]\d|2[0-3]):[0-5]\d$"


class QuietHours(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start: Annotated[str, Field(pattern=_TIME_RE)]
    end: Annotated[str, Field(pattern=_TIME_RE)]
    timezone: Annotated[str, Field(min_length=1, max_length=64)]

    @field_validator("timezone")
    @classmethod
    def _known_zone(cls, value: str) -> str:
        from zoneinfo import ZoneInfo

        try:
            ZoneInfo(value)
        except (LookupError, ValueError) as exc:
            raise ValueError("unknown time zone") from exc
        return value


class SubscriptionIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    endpoint: Annotated[str, Field(min_length=1, max_length=2048)]
    keys: dict[str, Annotated[str, Field(max_length=200)]]


class LinkPhoneRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    subscription: SubscriptionIn
    name: Annotated[str, Field(min_length=1, max_length=120)] = "Phone"
    platform: Literal["android", "ios", "other"] = "other"


class UpdatePhoneRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: Annotated[str | None, Field(default=None, min_length=1, max_length=120)] = None
    push_enabled: bool | None = None
    muted_kinds: Annotated[list[str] | None, Field(default=None, max_length=len(NOTIFICATION_KINDS))] = None
    quiet_hours: QuietHours | None = None
    #: True clears the quiet hours (``quiet_hours: null`` cannot say it).
    clear_quiet_hours: bool = False

    @field_validator("muted_kinds")
    @classmethod
    def _known_kinds(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        unknown = sorted(set(value) - set(NOTIFICATION_KINDS))
        if unknown:
            raise ValueError(f"unknown notification kinds: {', '.join(unknown)}")
        return sorted(set(value))


class PhoneResponse(BaseModel):
    id: str
    name: str
    platform: str
    push_enabled: bool
    muted_kinds: list[str]
    quiet_hours: dict | None
    last_seen_at: str | None
    last_push_at: str | None
    created_at: str | None
    #: The phone this request comes from (the companion shows "This phone").
    current: bool = False


class PushActionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    token: Annotated[str, Field(min_length=20, max_length=100)]


class PushActionResponse(BaseModel):
    outcome: Literal["approved", "rejected"]


def _response(device: MobileDevice, *, current_hash: str | None = None) -> PhoneResponse:
    return PhoneResponse(
        id=str(device.id),
        name=device.name,
        platform=device.platform,
        push_enabled=device.push_enabled,
        muted_kinds=list(device.muted_kinds or []),
        quiet_hours=device.quiet_hours,
        last_seen_at=device.last_seen_at.isoformat() if device.last_seen_at else None,
        last_push_at=device.last_push_at.isoformat() if device.last_push_at else None,
        created_at=device.created_at.isoformat() if device.created_at else None,
        current=current_hash is not None and device.endpoint_hash == current_hash,
    )


async def _load_own(db: AsyncSession, user: User, device_id: str, *, admin_may: bool = False) -> MobileDevice:
    doc_id = parse_path_id(device_id, not_found_detail=_NOT_FOUND)
    device = await db.get(MobileDevice, doc_id)
    if device is None or (device.user_id != user.id and not (admin_may and user.is_admin)):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_NOT_FOUND)
    return device


@router.get("/vapid-public-key")
async def get_vapid_public_key(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """The key a phone's browser subscribes to push with."""
    del user
    keys = await get_vapid_keys(db)
    return {"public_key": keys.public_key}


@router.get("/companion-link")
async def get_companion_link(user: User = Depends(get_current_user)):
    """The address to open on a phone, and its QR code (an address only, no credential).

    The QR code is rendered here, as for two-factor enrollment
    (``services/mfa.qr_data_uri``): no third-party QR service is contacted.
    """
    from app.core.config import settings
    from app.services.mfa import qr_data_uri

    del user
    url = (settings.frontend_url or settings.base_url or "").rstrip("/") + "/"
    return {"url": url, "qr_code": qr_data_uri(url)}


@router.get("", response_model=list[PhoneResponse])
async def list_phones(
    user_id: str | None = Query(default=None, description="An administrator lists another person's phones."),
    endpoint_hash: str | None = Query(default=None, max_length=64, description="Marks the phone asking."),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    owner = user.id
    if user_id is not None:
        target = parse_path_id(user_id, not_found_detail="User not found")
        if target != user.id and not user.is_admin:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
        owner = target
    rows = (
        await db.execute(
            select(MobileDevice).where(MobileDevice.user_id == owner).order_by(MobileDevice.created_at.desc())
        )
    ).scalars()
    return [_response(d, current_hash=endpoint_hash) for d in rows]


@router.post("", response_model=PhoneResponse, status_code=status.HTTP_201_CREATED)
async def link_phone(
    body: LinkPhoneRequest,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Link the phone this request comes from, or refresh its subscription.

    A subscription belongs to a browser on a phone: linked again by another
    account (someone else signed in on it), it moves to that account, so the
    previous one stops receiving pushes on a phone that is no longer theirs.
    """
    try:
        subscription = Subscription.parse(body.subscription.model_dump())
    except PushEndpointError as exc:
        logger.info("Refused a phone subscription: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="This browser's notifications cannot be used. Try another browser.",
        ) from exc
    now = datetime.now(UTC)
    device = (
        await db.execute(select(MobileDevice).where(MobileDevice.endpoint_hash == subscription.endpoint_hash))
    ).scalar_one_or_none()
    created = device is None
    if device is None:
        device = MobileDevice(id=uuid.uuid4(), user_id=user.id, endpoint_hash=subscription.endpoint_hash)
        db.add(device)
    elif device.user_id != user.id:
        from app.services.push_action_tokens import void_device_tokens

        await void_device_tokens(device.id)
        device.user_id = user.id
        device.muted_kinds = []
        device.quiet_hours = None
    device.name = body.name
    device.platform = body.platform
    device.push_subscription = encrypt_value(subscription.to_json())
    device.push_enabled = True
    device.last_seen_at = now
    if created:
        device.muted_kinds = []
    await log_audit(
        db,
        user=user,
        action="create" if created else "update",
        entity_type="mobile_device",
        entity_id=device.id,
        entity_name=device.name,
        summary=f"Linked phone '{device.name}'",
        details={"platform": device.platform},
        request=request,
    )
    await db.commit()
    await db.refresh(device)
    return _response(device, current_hash=subscription.endpoint_hash)


@router.patch("/{device_id}", response_model=PhoneResponse)
async def update_phone(
    device_id: str,
    body: UpdatePhoneRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    device = await _load_own(db, user, device_id)
    if body.name is not None:
        device.name = body.name
    if body.push_enabled is not None:
        device.push_enabled = body.push_enabled
    if body.muted_kinds is not None:
        device.muted_kinds = body.muted_kinds
    if body.clear_quiet_hours:
        device.quiet_hours = None
    elif body.quiet_hours is not None:
        device.quiet_hours = body.quiet_hours.model_dump()
    await db.commit()
    await db.refresh(device)
    return _response(device)


@router.delete("/{device_id}", status_code=status.HTTP_204_NO_CONTENT)
async def unlink_phone(
    device_id: str,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Unlink a phone: no more pushes, and its notification buttons stop working."""
    from app.services.push_action_tokens import void_device_tokens

    device = await _load_own(db, user, device_id, admin_may=True)
    await log_audit(
        db,
        user=user,
        action="delete",
        entity_type="mobile_device",
        entity_id=device.id,
        entity_name=device.name,
        summary=f"Unlinked phone '{device.name}'",
        details={"owner_id": str(device.user_id), "by_admin": device.user_id != user.id},
        request=request,
    )
    await db.delete(device)
    await db.commit()
    await void_device_tokens(device.id)


@router.post("/{device_id}/test")
async def send_test_notification(
    device_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Send a test notification to one of the person's phones."""
    from app.services.mobile_push import push_item_to_user
    from app.services.notification_feed import NotificationItem

    device = await _load_own(db, user, device_id)
    await enforce_rate_limit(
        None, "phone_test_push", limit=TEST_PUSHES_PER_HOUR, window_seconds=3600, bucket=str(user.id)
    )
    if not device.push_enabled:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Notifications are off on this phone.")
    item = NotificationItem(
        id=f"test-{uuid.uuid4()}",
        kind="agent_message",
        created_at=datetime.now(UTC).isoformat(),
        route="/",
        title="Notifications work",
        summary="This phone receives your XTM One notifications.",
    )
    counts = await push_item_to_user(db, user.id, item, only_device=device.id)
    return {"sent": counts.get("sent", 0) > 0}


@actions_router.post("/push-actions", response_model=PushActionResponse)
async def press_notification_button(
    body: PushActionRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Approve or reject a run's pending tool calls from its phone notification."""
    from app.api.v1.assignments import respond_to_human_input
    from app.api.v1.schemas.assignment import ApprovalDecision, RespondToHumanInputRequest
    from app.services.push_action_tokens import consume_token, void_run_tokens

    await enforce_rate_limit(
        request, "phone_push_action", limit=PUSH_ACTIONS_PER_MINUTE, window_seconds=60, fail_closed=True
    )
    action = await consume_token(body.token)
    if action is None:
        logger.info("Refused a notification button: unknown, spent or expired token")
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=ACTION_REFUSED)
    device = await db.get(MobileDevice, action.device_id)
    user = await db.get(User, action.user_id)
    if device is None or device.user_id != action.user_id or user is None or not user.is_active:
        logger.info("Refused a notification button: phone unlinked or user inactive")
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=ACTION_REFUSED)
    decisions = [ApprovalDecision(tool_call_id=call_id, decision=action.decision) for call_id in action.tool_call_ids]
    try:
        await respond_to_human_input(
            run_id=str(action.run_id),
            body=RespondToHumanInputRequest(decisions=decisions),
            user=user,
            db=db,
        )
    except HTTPException as exc:
        # The run moved on, the pending calls changed, or the person may no
        # longer decide: the route said why, the button does not.
        logger.info("Refused a notification button on run %s: %s", action.run_id, json.dumps(exc.detail, default=str))
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=ACTION_REFUSED) from exc
    await void_run_tokens(action.run_id)
    device.last_seen_at = datetime.now(UTC)
    await log_audit(
        db,
        user=user,
        action=action.decision,
        entity_type="assignment_run_approval",
        entity_id=action.run_id,
        entity_name=None,
        summary=f"{'Approved' if action.decision == 'approve' else 'Rejected'} pending tool calls from a phone notification",
        details={"surface": "phone_notification", "device_id": str(device.id), "calls": len(action.tool_call_ids)},
        request=request,
    )
    await db.commit()
    return PushActionResponse(outcome="approved" if action.decision == "approve" else "rejected")
