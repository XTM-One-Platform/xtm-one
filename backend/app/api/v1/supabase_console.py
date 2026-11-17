# SPDX-License-Identifier: Apache-2.0

"""Supabase database access probe - ``POST /integrations/{id}/supabase/probe``.

Offers the choices a database role is set from, as far as they can be read:

* with the integration's access token (the Management API): the projects, the
  project's login roles (the platform's own left out, a role that bypasses RLS
  flagged) and its connection hosts (session pooler, transaction pooler,
  direct);
* logged in as the role: the databases it may connect to, whether it bypasses
  RLS, the functions of the project's own schemas it may execute (from which
  the manager picks the allowed ones) and the tables and views it can still
  read directly - a role meant to reach the data through functions only
  should list none.

The settings probed are the stored ones, overridden by the body (so a role is
tried before it is saved); the stored password is used unless another one is
sent. Nothing is stored here: the choices are saved by the integration update.

Only the people who manage the integration (``helpers.can_manage``) may probe
it: an integration the caller cannot see is unknown (404), one they see and
may not manage a 403. A failure comes back as a fixed sentence in ``errors``;
the upstream or database wording stays in the log. A relay-routed integration
probes the Management API through its relay and refuses the database login
(a relay carries HTTP only).
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.api.v1.helpers import can_manage, get_user_group_ids, get_user_integration
from app.api.v1.integrations import EXTRA_CONFIG_MASK
from app.api.v1.schemas.supabase import (
    SupabaseProbeConnection,
    SupabaseProbeFunction,
    SupabaseProbeProject,
    SupabaseProbeRelation,
    SupabaseProbeRequest,
    SupabaseProbeResponse,
    SupabaseProbeRole,
)
from app.core.database import get_db
from app.core.egress import egress_route
from app.integrations.providers import supabase as client
from app.integrations.providers import supabase_postgres as pg
from app.models.integration import Integration
from app.models.user import User
from app.services.vault import SecretUnavailableError, reveal_field, reveal_json_field

logger = logging.getLogger(__name__)

router = APIRouter()

_NOT_FOUND = "Integration not found"


async def _load(db: AsyncSession, user: User, integration_id: str) -> Integration:
    """The Supabase integration ``integration_id``, for a caller who manages it."""
    group_ids = await get_user_group_ids(db, user.id)
    integration = await get_user_integration(db, user.id, integration_id, group_ids, is_admin=user.is_admin)
    if integration.provider != "supabase":
        raise HTTPException(status_code=404, detail=_NOT_FOUND)
    if not can_manage(user, integration, group_ids):
        raise HTTPException(
            status_code=403,
            detail="Only the people who manage this integration can probe its database access.",
        )
    return integration


# What the stored password was saved for: change one and it is not sent.
_LOGIN_KEYS = ("project_ref", "db_role", "db_host")


def _login_target(settings: dict[str, Any], key: str) -> str:
    value = str(settings.get(key) or "").strip()
    return client.normalize_project_ref(value) if key == "project_ref" else value.lower()


async def _stored_settings(integration: Integration, typed_password: str) -> dict[str, Any]:
    """The stored extra config, its vault references resolved.

    A typed password replaces the stored one, which is then never read from
    its vault. A stored password whose vault cannot be read is left out, so
    the probe asks for it instead of losing the other settings.
    """
    if not integration.extra_config_enc:
        return {}
    without: tuple[str, ...] = () if typed_password == EXTRA_CONFIG_MASK else ("db_password",)
    try:
        try:
            decoded = await reveal_json_field(integration, "extra_config_enc", without=without)
        except SecretUnavailableError:
            decoded = await reveal_json_field(integration, "extra_config_enc", without=("db_password",))
    except Exception:
        decoded = None
    return decoded if isinstance(decoded, dict) else {}


async def _settings(integration: Integration, body: SupabaseProbeRequest) -> dict[str, Any]:
    """The stored extra config with the body's fields laid over it."""
    submitted = body.model_dump(exclude_none=True)
    typed_password = submitted.pop("db_password", EXTRA_CONFIG_MASK)
    stored = await _stored_settings(integration, typed_password)
    settings = dict(stored)
    settings.update(submitted)
    if typed_password != EXTRA_CONFIG_MASK:
        settings["db_password"] = typed_password
    elif any(_login_target(settings, key) != _login_target(stored, key) for key in _LOGIN_KEYS):
        # The stored password goes only where it was saved for: another role,
        # host or project needs its own password typed.
        settings.pop("db_password", None)
    return settings


async def _probe_database(settings: dict[str, Any], response: SupabaseProbeResponse) -> None:
    project_ref = client.normalize_project_ref(str(settings.get("project_ref") or ""))
    segment = pg.encode_access(settings)
    if not segment:
        return
    # Everything but the password is checked first: an unusable host is the
    # error to fix, whether or not a password was typed.
    has_password = bool(settings.get("db_password"))
    document = pg.decode_access(segment if has_password else pg.encode_access({**settings, "db_password": "-"}))
    access, problem = pg.validate_access(document, project_ref)
    if access is None:
        response.errors["database"] = f"The database role is unusable: {problem}."
        return
    if not has_password:
        response.errors["database"] = (
            "Type the role's password: the stored one is sent only to the role, host and project it was saved for."
        )
        return

    # What the probe was reading when the database refused it.
    step = "checking the role"

    async def _read(connection: Any) -> tuple[Any, ...]:
        nonlocal step
        step = "reading the role's attributes"
        attributes = await connection.fetchrow(pg.ROLE_ATTRIBUTES_SQL)
        step = "listing the databases"
        databases = await connection.fetch(pg.DATABASES_SQL)
        step = "listing the functions the role may execute"
        functions = await connection.fetch(pg.EXECUTABLE_FUNCTIONS_SQL, list(pg.SYSTEM_SCHEMAS))
        step = "listing the tables the role can read"
        relations = await connection.fetch(pg.READABLE_RELATIONS_SQL, list(pg.SYSTEM_SCHEMAS))
        return attributes, databases, functions, relations

    try:
        attributes, databases, functions, relations = await pg.session(
            access, project_ref, _read, allow_rls_bypass=True
        )
    except pg.DatabaseAccessError as exc:
        message = str(exc).removeprefix("Error: ")
        if exc.untrusted:
            logger.info("Supabase probe as %s refused while %s: %r", access.role, step, exc.untrusted[:300])
            message = f"{message.removesuffix('.')} while {step}."
        response.errors["database"] = message
        return
    if attributes is not None:
        response.role_bypasses_rls = bool(attributes["bypass_rls"] or attributes["superuser"])
    response.databases = [str(row["name"]) for row in databases]
    response.functions = [
        SupabaseProbeFunction(
            schema_name=row["schema"],
            name=row["name"],
            signature=row["signature"] or "",
            returns=row["returns"] or "",
            returns_set=bool(row["returns_set"]),
            security_definer=bool(row["security_definer"]),
            volatility=row["volatility"] or "",
            comment=(row["comment"] or "")[:500] or None,
        )
        for row in pg.function_rows(functions)
    ]
    response.readable_relations = [
        SupabaseProbeRelation(
            schema_name=row["schema"],
            name=row["name"],
            kind=row["kind"],
            rls_enabled=bool(row["rls_enabled"]),
        )
        for row in relations
    ]


@router.post(
    "/{integration_id}/supabase/probe",
    response_model=SupabaseProbeResponse,
    response_model_by_alias=True,
)
async def probe_supabase_database_access(
    integration_id: str,
    body: SupabaseProbeRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SupabaseProbeResponse:
    """The projects, roles, connections, databases and functions a database role is set from."""
    from app.services.relays import integration_egress_route

    integration = await _load(db, user, integration_id)
    settings = await _settings(integration, body)
    try:
        access_token = (await reveal_field(integration, "api_key_enc")) or ""
    except SecretUnavailableError:
        access_token = ""
    route = await integration_egress_route(db, integration)
    response = SupabaseProbeResponse()
    with egress_route(route):
        management = await client.probe_management(access_token, str(settings.get("project_ref") or ""))
        await _probe_database(settings, response)
    if management["projects"] is not None:
        response.projects = [SupabaseProbeProject(**project) for project in management["projects"]]
    if management["roles"] is not None:
        response.roles = [SupabaseProbeRole(**role) for role in management["roles"]]
    if management["connections"] is not None:
        response.connections = [SupabaseProbeConnection(**item) for item in management["connections"]]
    response.errors = {**management["errors"], **response.errors}
    return response
