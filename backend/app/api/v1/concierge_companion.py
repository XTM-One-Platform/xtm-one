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

"""The Concierge companion REST API (#8261): the pet library, the user's companion, what it shows.

Every route needs the ``CONCIERGE`` flag.

* ``GET /pets``, ``GET /pets/{slug}/spritesheet``: every signed-in user.
* ``POST /pets``, ``DELETE /pets/{slug}``: administrators. An upload is
  hostile input (``services/concierge/pets.py``); a refusal is a 422 with the
  format's rule it broke, never a 500 and never the input echoed back.
* ``GET`` / ``PUT /me/companion``, ``GET /me/activity``: the signed-in user's
  own, nobody else's.

None of these runs an agent or a model, so none waits on agentic AI: the
companion only shows what the user may already read.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, File, HTTPException, Request, Response, UploadFile, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, require_admin
from app.core.database import get_db
from app.core.feature_flags import require_feature
from app.models.user import User
from app.services.audit import log_audit
from app.services.concierge import pets
from app.services.concierge.companion import CompanionPreference, companion_activity, read_preference

logger = logging.getLogger(__name__)

router = APIRouter(dependencies=[Depends(require_feature("CONCIERGE"))])

PET_NOT_FOUND = "Pet not found"
#: The files one upload may carry: a zip, or pet.json and its spritesheet.
MAX_UPLOAD_FILES = 2
_SLUG = r"^[a-z0-9][a-z0-9-]{0,63}$"


class PetResponse(BaseModel):
    slug: str
    display_name: str
    description: str | None
    sprite_version: int
    builtin: bool
    #: Changes with the spritesheet: the web app keys its cache on it.
    sha256: str


class CompanionResponse(BaseModel):
    enabled: bool
    #: The pet shown: the user's choice while it is in the library, else :data:`default_pet`.
    pet: str
    default_pet: str


class CompanionUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool
    pet: str | None = Field(default=None, pattern=_SLUG)


class ActivityResponse(BaseModel):
    state: Literal["idle", "running", "waiting", "failed", "review"]
    running: int
    waiting: int
    review: int
    failed_at: datetime | None


def _pet(info: pets.PetInfo) -> PetResponse:
    return PetResponse(
        slug=info.slug,
        display_name=info.display_name,
        description=info.description,
        sprite_version=info.sprite_version,
        builtin=info.builtin,
        sha256=info.sha256,
    )


# ── The library ────────────────────────────────────────────────────────────


@router.get("/pets", response_model=list[PetResponse])
async def list_pets(_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    return [_pet(info) for info in await pets.list_pets(db)]


@router.get("/pets/{slug}/spritesheet")
async def get_spritesheet(
    slug: str, _user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> Response:
    """The pet's spritesheet as the platform encoded it, WebP."""
    found = await pets.load_spritesheet(db, slug) if len(slug) <= 64 else None
    if found is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=PET_NOT_FOUND)
    data, sha = found
    return Response(
        content=data,
        media_type="image/webp",
        headers={
            "Cache-Control": "private, max-age=86400",
            "ETag": f'"{sha}"',
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'none'",
        },
    )


async def _read_bounded(upload: UploadFile) -> bytes:
    data = await upload.read(pets.MAX_PACKAGE_BYTES + 1)
    if len(data) > pets.MAX_PACKAGE_BYTES:
        raise pets.PetPackageError("too_large")
    return data


@router.post("/pets", response_model=PetResponse, status_code=status.HTTP_201_CREATED)
async def upload_pet(
    request: Request,
    files: list[UploadFile] = File(..., description="One .zip, or pet.json and its spritesheet."),
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Add a Codex-format pet to the library, its spritesheet decoded and re-encoded by the platform."""
    try:
        if not files or len(files) > MAX_UPLOAD_FILES:
            raise pets.PetPackageError("not_a_package", f"{len(files)} file(s)")
        parsed = pets.parse_package([((upload.filename or "")[:255], await _read_bounded(upload)) for upload in files])
        pet = await pets.add_pet(db, parsed, actor_id=admin.id)
    except pets.PetPackageError as exc:
        logger.info("Concierge pet upload refused: %.200r", str(exc))
        status_code = status.HTTP_409_CONFLICT if exc.code == "slug_taken" else 422
        raise HTTPException(status_code=status_code, detail={"code": exc.code, "message": exc.message}) from None
    await log_audit(
        db,
        user=admin,
        action="create",
        entity_type="concierge_pet",
        entity_id=pet.id,
        entity_name=pet.display_name,
        summary="Added a pet to the Concierge companion library",
        details={"slug": pet.slug, "sprite_version": pet.sprite_version},
        request=request,
    )
    await db.commit()
    return PetResponse(
        slug=pet.slug,
        display_name=pet.display_name,
        description=pet.description,
        sprite_version=pet.sprite_version,
        builtin=False,
        sha256=pet.spritesheet_sha256,
    )


@router.delete("/pets/{slug}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_pet(
    slug: str, request: Request, admin: User = Depends(require_admin), db: AsyncSession = Depends(get_db)
) -> Response:
    """Remove an uploaded pet; its users see the default pet. A built-in pet is never removed."""
    if slug in pets.BUILTIN_SLUGS:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="A built-in pet cannot be removed.")
    pet = await pets.remove_pet(db, slug) if len(slug) <= 64 else None
    if pet is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=PET_NOT_FOUND)
    await log_audit(
        db,
        user=admin,
        action="delete",
        entity_type="concierge_pet",
        entity_id=pet.id,
        entity_name=pet.display_name,
        summary="Removed a pet from the Concierge companion library",
        details={"slug": pet.slug},
        request=request,
    )
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ── The user's companion ───────────────────────────────────────────────────


async def _companion(db: AsyncSession, preference: CompanionPreference) -> CompanionResponse:
    chosen = preference.pet
    shown = chosen if chosen and await pets.pet_exists(db, chosen) else pets.DEFAULT_PET
    return CompanionResponse(enabled=preference.enabled, pet=shown, default_pet=pets.DEFAULT_PET)


@router.get("/me/companion", response_model=CompanionResponse)
async def get_companion(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    return await _companion(db, read_preference(user))


@router.put("/me/companion", response_model=CompanionResponse)
async def put_companion(
    body: CompanionUpdate, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
):
    if body.pet is not None and not await pets.pet_exists(db, body.pet):
        raise HTTPException(status_code=422, detail="This pet is not in the library.")
    row = await db.get(User, user.id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    preference = CompanionPreference(enabled=body.enabled, pet=body.pet)
    row.concierge_companion = preference.model_dump()
    await db.commit()
    return await _companion(db, preference)


@router.get("/me/activity", response_model=ActivityResponse)
async def get_activity(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """What the companion shows: the Concierge's work and the user's own runs, from what they may read."""
    activity = await companion_activity(db, user)
    return ActivityResponse(
        state=activity.state,
        running=activity.running,
        waiting=activity.waiting,
        review=activity.review,
        failed_at=activity.failed_at,
    )
