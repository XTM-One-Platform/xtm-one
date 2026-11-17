# SPDX-License-Identifier: Apache-2.0

"""OpenCRQ version card - what the connected OpenCRQ lets the agents do (``/integrations/{id}/opencrq``).

``GET /compatibility`` answers the Overview card of an OpenCRQ integration.

Who may read it: everyone who can open the integration, as for the Overview page
the card sits on (an integration the caller cannot see is unknown, 404).

The tools follow the procedure manifest each OpenCRQ instance describes itself with
(``providers.opencrq.procedure_manifest``). The card reads the manifest the
registration stored, through the functions the tool build gates on
(``availability``, ``limited_fields``), so what it lists as hidden is what
``build_tools`` withholds. An integration no registration owns is not asked here: its
manifest is read from the instance at the first OpenCRQ call of a conversation or run.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.api.v1.helpers import get_user_group_ids, get_user_integration
from app.api.v1.schemas.opencrq import (
    CompatibilityResponse,
    HiddenToolResponse,
    LimitedFieldResponse,
    LimitedToolResponse,
    ManifestState,
)
from app.core.database import get_db
from app.core.platform_identity import ProcedureManifestStatus, procedure_manifest_status
from app.core.registration_token import registration_token_state
from app.integrations.providers.opencrq.input_contract import limited_fields
from app.integrations.providers.opencrq.procedure_manifest import availability, load_tool_procedures
from app.models.integration import Integration
from app.models.user import User

router = APIRouter()

PROVIDER = "opencrq"
_NOT_FOUND = "Integration not found"
# The reported version is a platform's own string: what the card shows stays short.
_MAX_VERSION_LENGTH = 64
_FINGERPRINT_LENGTH = 12


async def _load(db: AsyncSession, user: User, integration_id: str) -> Integration:
    """The OpenCRQ integration ``integration_id``, for a caller who can open it."""
    group_ids = await get_user_group_ids(db, user.id)
    integration = await get_user_integration(db, user.id, integration_id, group_ids, is_admin=user.is_admin)
    if integration.provider != PROVIDER:
        raise HTTPException(status_code=404, detail=_NOT_FOUND)
    return integration


def _state(status: ProcedureManifestStatus) -> ManifestState:
    """Where the registration stands: the manifest that decides first, then why there is none."""
    if status.manifest is not None:
        return "current"
    if registration_token_state() == "published_default":
        return "not_accepted_default_token"
    return "unavailable" if status.fingerprint is None else "pending"


@router.get("/{integration_id}/opencrq/compatibility", response_model=CompatibilityResponse)
async def get_compatibility(
    integration_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> CompatibilityResponse:
    """The version the OpenCRQ reported, the state of its manifest, the tools it hides and the fields it refuses."""
    integration = await _load(db, user, integration_id)
    tool_procedures = load_tool_procedures()
    status = await procedure_manifest_status(db, integration.id)
    if status is None:
        return CompatibilityResponse(manifest_source="none", tool_count=len(tool_procedures))
    state = _state(status)
    response = CompatibilityResponse(
        reported_version=status.platform_version[:_MAX_VERSION_LENGTH],
        manifest_source="registration",
        manifest_state=state,
        fingerprint=status.fingerprint[:_FINGERPRINT_LENGTH] if status.fingerprint else None,
        tool_count=len(tool_procedures),
    )
    if status.manifest is None:
        return response
    hidden = availability(status.manifest, tool_procedures)
    limited = limited_fields(status.manifest, hidden)
    response.received_at = status.received_at
    response.hidden_tools = [
        HiddenToolResponse(name=name, missing_procedures=list(gap.missing)) for name, gap in sorted(hidden.items())
    ]
    response.limited_tools = [
        LimitedToolResponse(
            name=name,
            fields=[LimitedFieldResponse(procedure=f.procedure, path=f.path, kind=f.kind) for f in fields],
        )
        for name, fields in limited.items()
    ]
    return response
