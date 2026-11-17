# SPDX-License-Identifier: Apache-2.0

"""XTM One's registration with the XTM Hub (Settings > Platform, home dashboard).

- ``GET /registration`` (admin): the registration and what the Hub's register
  page needs (``register_params``), never the token.
- ``GET /registration/status`` (any user): the coarse status the home
  dashboard pill and the product switcher show, with the default Hub URL.
- ``POST /registration`` (admin): store the platform token the Hub's page
  posted back, once the Hub confirms it.
- ``PUT /registration/auto`` (admin): register with ``XTM_HUB_DEFAULT_URL``
  under the platform token the Hub issued for a trial deployment; the XTM
  Platform deployer calls it with the ``ADMIN_TOKEN`` key (``204``; ``400``
  for a token that is not one, ``502`` when the Hub refuses or cannot be
  reached).
- ``POST /registration/check`` (admin): ask the Hub now.
- ``DELETE /registration`` (admin): forget the registration locally (the Hub
  side is removed from the Hub's unregister page).

The rules live in ``services/xtm_hub_registration.py``.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, require_admin
from app.core.database import get_db
from app.integrations.providers.xtm_hub import xtm_hub_default_url
from app.models.user import User
from app.services import xtm_hub_registration as registration_service
from app.services.audit import log_audit

logger = logging.getLogger(__name__)

router = APIRouter()

RegistrationStatus = Literal[
    "unregistered", "registered", "lost_connectivity", "needs_reregistration"
]


class RegisterParams(BaseModel):
    """What the Hub's register page is opened with (all public values)."""

    platform_id: str
    platform_version: str
    platform_identifier: str = registration_service.PLATFORM_IDENTIFIER


class XtmHubRegistrationResponse(BaseModel):
    status: RegistrationStatus
    hub_url: str | None
    default_hub_url: str = Field(default_factory=xtm_hub_default_url)
    registered_at: datetime | None
    last_check_at: datetime | None
    last_check_result: str | None
    register_params: RegisterParams


class XtmHubRegistrationStatusResponse(BaseModel):
    status: RegistrationStatus
    hub_url: str | None
    default_hub_url: str = Field(default_factory=xtm_hub_default_url)


class RegisterRequest(BaseModel):
    hub_url: str = Field(min_length=1, max_length=2048)
    token: str = Field(min_length=1, max_length=64)

    @field_validator("hub_url")
    @classmethod
    def _hub_url_allowed(cls, value: str) -> str:
        bound = registration_service.registration_hub_url(value)
        if not bound:
            raise ValueError(
                "must be an https URL (http only for an XTM Hub on this machine)"
            )
        return bound

    @field_validator("token")
    @classmethod
    def _token_is_a_uuid(cls, value: str) -> str:
        # The Hub issues platform tokens as UUIDs; anything else is refused
        # before it is sent anywhere.
        try:
            return str(uuid.UUID(value.strip()))
        except ValueError as exc:
            raise ValueError("is not an XTM Hub platform token") from exc


class AutoRegisterRequest(BaseModel):
    token: str = Field(min_length=1, max_length=64)


async def _response(db: AsyncSession) -> XtmHubRegistrationResponse:
    view = registration_service.view_of(await registration_service.load_registration(db))
    return XtmHubRegistrationResponse(
        status=view.status,
        hub_url=view.hub_url,
        registered_at=view.registered_at,
        last_check_at=view.last_check_at,
        last_check_result=view.last_check_result,
        register_params=RegisterParams(
            platform_id=str(await registration_service.current_platform_id(db)),
            platform_version=registration_service.registration_platform_version(),
        ),
    )


@router.get("/registration", response_model=XtmHubRegistrationResponse)
async def get_registration(
    _admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> XtmHubRegistrationResponse:
    return await _response(db)


@router.get("/registration/status", response_model=XtmHubRegistrationStatusResponse)
async def get_registration_status(
    _user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> XtmHubRegistrationStatusResponse:
    view = registration_service.view_of(await registration_service.load_registration(db))
    return XtmHubRegistrationStatusResponse(status=view.status, hub_url=view.hub_url)


@router.post("/registration", response_model=XtmHubRegistrationResponse)
async def register_platform(
    body: RegisterRequest,
    request: Request,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> XtmHubRegistrationResponse:
    try:
        await registration_service.register(db, hub_url=body.hub_url, token=body.token)
    except registration_service.RegistrationRefusedError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    await log_audit(
        db,
        user=admin,
        action="create",
        entity_type="xtm_hub_registration",
        entity_name="XTM Hub registration",
        summary=f"Registered this platform with the XTM Hub at {body.hub_url}",
        details={"hub_url": body.hub_url},
        request=request,
    )
    # Commit before answering: the get_db teardown only commits after the
    # response is sent, and the page re-reads the registration right away.
    await db.commit()
    return await _response(db)


@router.put("/registration/auto", status_code=status.HTTP_204_NO_CONTENT)
async def auto_register_platform(
    body: AutoRegisterRequest,
    request: Request,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> Response:
    try:
        # The same check as the manual registration's, answered 400 as
        # OpenCTI and OpenAEV answer their deployer.
        token = str(uuid.UUID(body.token.strip()))
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="The token is not an XTM Hub platform token."
        ) from exc
    try:
        registration = await registration_service.auto_register(db, token=token)
    except registration_service.RegistrationRefusedError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
    await log_audit(
        db,
        user=admin,
        action="create",
        entity_type="xtm_hub_registration",
        entity_name="XTM Hub registration",
        summary=f"Registered this platform with the XTM Hub at {registration.hub_url} automatically",
        details={"hub_url": registration.hub_url, "automatic": True},
        request=request,
    )
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/registration/check", response_model=XtmHubRegistrationResponse)
async def check_registration(
    request: Request,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> XtmHubRegistrationResponse:
    before = registration_service.view_of(await registration_service.load_registration(db))
    after = await registration_service.check_now(db)
    if after.status != before.status:
        # The Hub no longer knowing it removed the row: a delete, as the
        # heartbeat records the same transition.
        forgotten = after.status == registration_service.UNREGISTERED.status
        await log_audit(
            db,
            user=admin,
            action="delete" if forgotten else "update",
            entity_type="xtm_hub_registration",
            entity_name="XTM Hub registration",
            summary=f"XTM Hub registration checked: {before.status} -> {after.status}",
            details={"previous_status": before.status, "status": after.status},
            request=request,
        )
    await db.commit()
    return await _response(db)


@router.delete("/registration", response_model=XtmHubRegistrationResponse)
async def forget_registration(
    request: Request,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> XtmHubRegistrationResponse:
    if await registration_service.forget(db):
        await log_audit(
            db,
            user=admin,
            action="delete",
            entity_type="xtm_hub_registration",
            entity_name="XTM Hub registration",
            summary="Forgot this platform's XTM Hub registration locally",
            request=request,
        )
    await db.commit()
    return await _response(db)
