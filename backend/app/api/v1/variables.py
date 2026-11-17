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

"""Variables API – CRUD for platform-level template variables."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, require_agentic_ai_licensed
from app.api.v1.helpers import (
    ENTITY_LIST_SCOPE_DESCRIPTION,
    EntityListScope,
    apply_group_share_fields,
    apply_user_share_fields,
    can_manage,
    get_user_group_ids,
    list_scope_admin_bypass,
    parse_path_id,
    require_admin_for_company_managed,
    user_visible_filter,
    user_visible_query,
)
from app.api.v1.schemas.column_limits import column_length_validator
from app.api.v1.schemas.vault_references import (
    VaultSecretStatus,
    attach_vault_failures,
    require_vault_writer,
    vault_status,
)
from app.core.database import get_db
from app.core.security import encrypt_value
from app.core.security_options import security_option
from app.models.user import User
from app.models.variable import Variable
from app.services.audit import log_audit
from app.services.vault import VaultPrincipal, holds_any_reference, reference_problem
from app.services.vault.store import principal_from, vault_principal

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Feature-flag guard
# ---------------------------------------------------------------------------


def _require_custom_variables_enabled(
    _current_user: User = Depends(get_current_user),
) -> None:
    """Raise 403 when the platform is configured with
    ``DISABLE_CUSTOM_VARIABLES=true``.

    Declaring ``get_current_user`` as a parameter ensures authentication
    is checked **before** the feature-flag test.  Unauthenticated or
    broken-auth requests receive a 401 (not a 403), so the flag state
    is never leaked to anonymous callers.

    Wired in as a router-level dependency so every endpoint on this
    router (GET list, GET detail, POST, PUT, DELETE) is gated
    consistently — no per-handler ``if security_option("disable_custom_variables")``
    duplication, no GET endpoints accidentally leaking variable
    values when the flag is set, and the same status/detail surfaces
    to the LLM and the UI everywhere.
    """
    if security_option("disable_custom_variables"):
        raise HTTPException(
            status_code=403,
            detail="Custom variables are disabled on this platform",
        )


router = APIRouter(dependencies=[Depends(_require_custom_variables_enabled)])


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class VariableCreate(BaseModel):
    name: str
    value: str | None = None
    is_secret: bool = False
    description: str | None = None
    tags: list[str] | None = None
    # Sharing at creation time. Previously only ``VariableUpdate`` carried
    # these, so sharing a new variable meant a create-then-update round trip.
    shared_group_ids: list[str] | None = None
    shared_group_permissions: dict[str, str] | None = None
    shared_user_ids: list[str] | None = None
    shared_user_permissions: dict[str, str] | None = None
    access_restricted: bool | None = None

    _fit_columns = column_length_validator(Variable, "name", stripped=True)


class VariableUpdate(BaseModel):
    name: str | None = None
    value: str | None = None
    is_secret: bool | None = None
    description: str | None = None
    tags: list[str] | None = None
    is_company_managed: bool | None = None
    shared_group_ids: list[str] | None = None
    shared_group_permissions: dict[str, str] | None = None
    shared_user_ids: list[str] | None = None
    shared_user_permissions: dict[str, str] | None = None
    access_restricted: bool | None = None

    _fit_columns = column_length_validator(Variable, "name", stripped=True)


class VariableResponse(BaseModel):
    id: str
    name: str
    value: str | None
    is_secret: bool
    description: str | None
    tags: list[str]
    user_id: str | None
    is_company_managed: bool
    shared_group_ids: list[str] | None
    shared_group_permissions: dict[str, str] | None = None
    shared_user_ids: list[str] | None = None
    shared_user_permissions: dict[str, str] | None = None
    access_restricted: bool = False
    #: The value read from a vault, and its recent failures.
    vault_secrets: VaultSecretStatus | None = None
    created_at: str
    updated_at: str


async def _check_value(db: AsyncSession, value: str | None, is_secret: bool, user: User) -> None:
    """A vault reference is the value of a secret variable only, names a known provider, and is one *user* may write."""
    if value is None:
        return
    if is_secret:
        problem = reference_problem(value)
    else:
        problem = "a vault reference can only be the value of a secret variable" if holds_any_reference(value) else None
    if problem:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=f"value: {problem}")
    await require_vault_writer(db, user, value=value)


def _to_response(v: Variable, vault_viewer: VaultPrincipal | None = None) -> VariableResponse:
    val = None
    if v.is_secret:
        val = "••••••••"
    elif v.value is not None:
        val = v.value

    return VariableResponse(
        id=str(v.id),
        name=v.name,
        value=val,
        is_secret=v.is_secret,
        description=v.description,
        tags=v.tags or [],
        user_id=str(v.user_id) if v.user_id else None,
        is_company_managed=v.is_company_managed,
        shared_group_ids=v.shared_group_ids,
        shared_group_permissions=v.shared_group_permissions,
        shared_user_ids=v.shared_user_ids,
        shared_user_permissions=v.shared_user_permissions,
        access_restricted=v.access_restricted,
        vault_secrets=vault_status(v, vault_viewer) if v.is_secret else None,
        created_at=v.created_at.isoformat(),
        updated_at=v.updated_at.isoformat(),
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("", response_model=list[VariableResponse])
async def list_variables(
    scope: EntityListScope = Query("visible", description=ENTITY_LIST_SCOPE_DESCRIPTION),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    user_group_ids = await get_user_group_ids(db, user.id)
    stmt = user_visible_query(
        Variable, user.id, user_group_ids, is_admin=list_scope_admin_bypass(scope, user)
    )
    result = await db.execute(stmt)
    viewer = principal_from(user, user_group_ids)
    responses = [
        _to_response(v, viewer if can_manage(user, v, user_group_ids) else None) for v in result.scalars().all()
    ]
    await attach_vault_failures(responses, Variable.__tablename__)
    return responses


@router.post("", response_model=VariableResponse, status_code=status.HTTP_201_CREATED)
async def create_variable(
    body: VariableCreate,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _agentic_gate: None = Depends(require_agentic_ai_licensed),
):
    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Variable name is required")
    await _check_value(db, body.value, body.is_secret, user)

    var = Variable(
        user_id=user.id,
        name=name,
        is_secret=body.is_secret,
        description=body.description,
        tags=body.tags or [],
    )
    await apply_group_share_fields(db, var, body, partial=False)
    await apply_user_share_fields(db, var, body, partial=False)

    if body.is_secret and body.value:
        var.value_enc = encrypt_value(body.value)
        var.value = None
    else:
        var.value = body.value

    db.add(var)
    await db.flush()

    await log_audit(
        db,
        user=user,
        action="create",
        entity_type="variable",
        entity_id=var.id,
        entity_name=var.name,
        summary=f"Created variable '{var.name}'",
        request=request,
    )
    await db.commit()
    return _to_response(var, await vault_principal(db, user))


@router.get("/{variable_id}", response_model=VariableResponse)
async def get_variable(
    variable_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    var = await _get_user_variable(db, user, variable_id)
    user_group_ids = await get_user_group_ids(db, user.id)
    response = _to_response(
        var, principal_from(user, user_group_ids) if can_manage(user, var, user_group_ids) else None
    )
    await attach_vault_failures([response], Variable.__tablename__)
    return response


@router.put("/{variable_id}", response_model=VariableResponse)
async def update_variable(
    variable_id: str,
    body: VariableUpdate,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    var = await _get_user_variable(db, user, variable_id)

    # Full write gate: company-managed requires admin; group-shared requires
    # config-tier permission. Matches prompts / custom-tools so the shared
    # permission map means the same thing for every entity type.
    if not can_manage(user, var, await get_user_group_ids(db, user.id)):
        detail = (
            "Only admins can edit company-managed variables"
            if var.is_company_managed
            else "You don't have permission to edit this variable"
        )
        raise HTTPException(status_code=403, detail=detail)

    if body.is_company_managed is not None:
        require_admin_for_company_managed(
            body, user, "Only admins can manage company-managed variables"
        )
        if body.is_company_managed and not var.is_company_managed:
            # Owner is always preserved; only visibility flips. Backfill the
            # owner for legacy rows that never had one.
            var.is_company_managed = True
            if var.user_id is None:
                var.user_id = user.id
        elif not body.is_company_managed and var.is_company_managed:
            var.is_company_managed = False
            if var.user_id is None:
                var.user_id = user.id
            var.shared_group_ids = None

    if body.name is not None:
        var.name = body.name.strip()
    if body.description is not None:
        var.description = body.description
    if body.tags is not None:
        var.tags = body.tags
    await apply_group_share_fields(
        db, var, body, partial=True, null_keeps_stored=True
    )

    # Per-user sharing — after the company-managed handling above,
    # since ``access_restricted`` is only meaningful relative to it. This is
    # what lets a secret variable follow an agent share without its plaintext
    # ever being handed over: the recipient resolves it at runtime but
    # still reads a masked value from the API.
    await apply_user_share_fields(db, var, body, partial=True)

    was_secret = var.is_secret
    if body.is_secret is not None:
        var.is_secret = body.is_secret

    # Turning secrecy OFF without a new value would orphan the secret: it lives
    # encrypted in ``value_enc`` and is NOT decrypted into the plaintext
    # ``value`` column here, so the variable would resolve to an empty string at
    # runtime (see ``variable_resolver``). Require an explicit new value.
    if (
        was_secret
        and not var.is_secret
        and body.value is None
        and var.value_enc is not None
    ):
        raise HTTPException(
            status_code=400,
            detail=(
                "Turning off 'is_secret' requires a new 'value': the existing "
                "secret is stored encrypted and is not revealed into the "
                "plaintext column."
            ),
        )

    if body.value is not None:
        await _check_value(db, body.value, var.is_secret, user)
        if var.is_secret:
            var.value_enc = encrypt_value(body.value)
            var.value = None
        else:
            var.value = body.value
            var.value_enc = None
    elif var.is_secret and var.value is not None:
        # No new value, but the variable is secret with a plaintext value still
        # present — either it was just switched to secret in this request, or
        # it's a legacy/inconsistent secret row. Either way, migrate the
        # plaintext into the encrypted column so it isn't left unencrypted at
        # rest (the "secrets encrypted at rest" contract).
        await _check_value(db, var.value, True, user)
        var.value_enc = encrypt_value(var.value)
        var.value = None

    await log_audit(
        db,
        user=user,
        action="update",
        entity_type="variable",
        entity_id=var.id,
        entity_name=var.name,
        summary=f"Updated variable '{var.name}'",
        request=request,
    )
    await db.commit()
    await db.refresh(var)
    return _to_response(var, await vault_principal(db, user))


@router.delete("/{variable_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_variable(
    variable_id: str,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    var = await _get_user_variable(db, user, variable_id)

    if not can_manage(user, var, await get_user_group_ids(db, user.id)):
        detail = (
            "Only admins can delete company-managed variables"
            if var.is_company_managed
            else "You don't have permission to delete this variable"
        )
        raise HTTPException(status_code=403, detail=detail)

    await log_audit(
        db,
        user=user,
        action="delete",
        entity_type="variable",
        entity_id=var.id,
        entity_name=var.name,
        summary=f"Deleted variable '{var.name}'",
        request=request,
    )
    await db.delete(var)
    await db.commit()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _get_user_variable(
    db: AsyncSession, user: User, variable_id: str
) -> Variable:
    uid = parse_path_id(variable_id, not_found_detail="Variable not found")
    user_group_ids = await get_user_group_ids(db, user.id)

    result = await db.execute(
        select(Variable).where(
            Variable.id == uid,
            user_visible_filter(Variable, user.id, user_group_ids, is_admin=user.is_admin),
        )
    )
    var = result.scalar_one_or_none()
    if not var:
        raise HTTPException(status_code=404, detail="Variable not found")
    return var
