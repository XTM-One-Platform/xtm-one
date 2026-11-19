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

"""MCP Server management API – CRUD, test connectivity, list tools.

Any authenticated user can create MCP servers.  Company-managed servers
(``is_company_managed=True``) are visible to all users but only admins can
edit/delete them; visibility comes from the flag and ``user_id`` keeps the
creator as owner.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import re
import time
import zlib
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import Text, cast, select
from sqlalchemy.exc import DataError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.a2a.client.destinations import (
    DestinationRefusedError,
    assert_allowed,
    assert_allowed_async,
)
from app.api.deps import get_current_user, require_agentic_ai
from app.api.v1.binding_access import checked_relay_group_id
from app.api.v1.helpers import (
    ENTITY_LIST_SCOPE_DESCRIPTION,
    EntityListScope,
    apply_group_share_fields,
    apply_owner_transfer,
    apply_user_share_fields,
    build_mcp_runtime,
    can_manage,
    get_manageable_entity,
    get_user_group_ids,
    get_user_mcp_server,
    handle_company_managed_toggle,
    list_scope_admin_bypass,
    require_admin_for_company_managed,
    require_allowed_mcp_server_url,
    require_allowed_token_url,
    resolve_owner_names,
    user_visible_query,
    wants_access_restriction,
)
from app.api.v1.schemas.mcp_server import (
    ImportMCPServerRequest,
    MCPConfigCandidate,
    MCPConfigImportRequest,
    MCPConfigImportResponse,
    MCPConfigPreviewRequest,
    MCPConfigPreviewResponse,
    MCPOAuthDiscoveryRequest,
    MCPOAuthDiscoveryResponse,
    MCPServerCreate,
    MCPServerResponse,
    MCPServerUpdate,
    MCPToolInfo,
    MCPToolTestRequest,
)
from app.api.v1.schemas.vault_references import attach_vault_failures, require_vault_writer, vault_status
from app.core.database import get_db, release_db_connection_before_loop
from app.core.egress import egress_route
from app.core.rate_limit import enforce_rate_limit
from app.core.security import encrypt_value
from app.core.security_options import security_option
from app.core.utils import (
    MCP_SERVER_URL_LABELS,
    MaskedArgumentError,
    format_exception_message,
    redact_secret_args,
    redact_secret_text,
    restore_masked_args,
    restore_masked_text,
    safe_uuid,
)
from app.models.a2a import UNUSABLE_CARD_MALFORMED, UNUSABLE_TRANSPORT_UNVERIFIED
from app.models.agent import Agent
from app.models.mcp_server import MCPServer
from app.models.user import User
from app.services.audit import log_audit
from app.services.mcp_config_import import (
    MAX_DOCUMENT_BYTES,
    MAX_SERVERS_PER_DOCUMENT,
    ConfigImportError,
    parse_mcp_config,
)
from app.services.relays import load_egress_routes, mcp_server_egress_route
from app.services.vault import (
    VaultPrincipal,
    holds_any_reference,
    retained_reference_writer_problem,
    secret_fields_problem,
    secret_fields_writer_problem,
)
from app.services.vault.store import principal_from, vault_principal
from app.tools.mcp_destinations import (
    REMOTE_TRANSPORTS,
    MCPServerURLRefusedError,
    assert_server_url_allowed,
    url_problem,
)

logger = logging.getLogger(__name__)
router = APIRouter()

#: What an edit may change while the server keeps a vault reference its editor
#: could not have written: none of these moves where the platform sends
#: the secret. Any other field it changes does.
_FIELDS_KEEPING_SECRET_DESTINATION = frozenset(
    {
        "name",
        "tags",
        "enabled",
        "hide_from_chat",
        "investigation_tool_name",
        "investigation_argument",
        "env",
        "headers",
        "client_id",
        "client_secret",
        "scopes",
        "credential_mode",
        "is_company_managed",
        "is_public_catalog",
        "user_id",
        "access_restricted",
        "shared_group_ids",
        "shared_group_permissions",
        "shared_user_ids",
        "shared_user_permissions",
    }
)
#: Stored stripped, a blank one as NULL (see ``update_mcp_server``).
_STRIPPED_URL_FIELDS = frozenset({"authorize_url", "token_url", "registration_url"})


def _update_repoints_mcp_server(srv: MCPServer, body: MCPServerUpdate) -> bool:
    """Whether *body* changes where *srv* sends its secrets: a field outside
    :data:`_FIELDS_KEEPING_SECRET_DESTINATION` set to something else than stored.

    The edit form sends its unchanged fields back, so a value equal to the
    stored one changes nothing.
    """
    for name in body.model_fields_set - _FIELDS_KEEPING_SECRET_DESTINATION:
        value = getattr(body, name)
        if name == "relay_group_id":
            # Set, moved or cleared (null = send directly): the requests, and the
            # secrets they carry, leave through another way.
            if safe_uuid(str(value or "")) != srv.relay_group_id:
                return True
            continue
        if value is None:
            continue
        if name in _STRIPPED_URL_FIELDS:
            value = value.strip() or None
        if value != getattr(srv, name, None):
            return True
    return False


def _refuse_stdio_relay(transport: str | None, relay_group_id: object) -> None:
    """A stdio server is a process on the platform host: no relay can carry it."""
    if relay_group_id and transport == "stdio":
        raise HTTPException(
            status_code=422,
            detail=(
                "A stdio MCP server runs on the platform itself and cannot be routed "
                "through a relay group. Use an sse or streamable_http server, or "
                "choose Direct."
            ),
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _my_oauth_statuses(db: AsyncSession, servers: Any, user_id: Any) -> dict[Any, str]:
    """The reader's own connection on each ``per_user`` server of *servers* (one query)."""
    from app.services.mcp_user_credentials import is_per_user, user_credentials_by_server

    per_user = [srv.id for srv in servers if is_per_user(srv)]
    if not per_user:
        return {}
    own = await user_credentials_by_server(db, per_user, user_id)
    return {sid: (own[sid].oauth_status if sid in own else "disconnected") for sid in per_user}


async def _apply_credential_mode(db: AsyncSession, srv: MCPServer, mode: str | None) -> dict[str, Any]:
    """Switch *srv* to *mode*; the audit details of the change (empty when none).

    ``shared`` -> ``per_user``: the token on the server row is dropped, nobody
    uses it any more. ``per_user`` -> ``shared``: every person's own credential
    is deleted (they connected for their own use, never to be lent), as for an
    integration (``dev-docs/mcp-server-user-credentials.md``).
    """
    from app.services.mcp_user_credentials import PER_USER, SHARED, clear_server_tokens, delete_user_credentials

    before = srv.credential_mode or SHARED
    if mode is None or mode == before:
        return {}
    srv.credential_mode = mode
    if mode == PER_USER:
        clear_server_tokens(srv)
        return {"credential_mode_from": before, "shared_token_dropped": True}
    removed = await delete_user_credentials(db, srv.id)
    return {"credential_mode_from": before, "user_credentials_deleted": removed}


def _to_response(
    srv: MCPServer,
    *,
    can_manage: bool,
    owner_name: str | None = None,
    vault_viewer: VaultPrincipal | None = None,
    my_oauth_status: str | None = None,
) -> MCPServerResponse:
    # *my_oauth_status*: on a per_user server, the reader's own connection
    # (``disconnected`` when they never connected); ``None`` elsewhere.
    # Everyone who can see the server reads it - a read_only grantee, every
    # user for a company-managed one - so a credential in its arguments or URLs
    # is masked the way the export masks it for a non-manager.
    # ``update_mcp_server`` puts a masked value sent back to what it stands for.
    masked_args = redact_secret_args(srv.args) if srv.args else None
    tools = (
        [
            MCPToolInfo(
                name=t["name"],
                description=t.get("description", ""),
                input_schema=t.get("input_schema"),
            )
            for t in srv.discovered_tools
        ]
        if srv.discovered_tools
        else None
    )
    return MCPServerResponse(
        id=str(srv.id),
        user_id=str(srv.user_id) if srv.user_id else None,
        owner_name=owner_name,
        name=srv.name,
        transport=srv.transport,
        command=srv.command,
        args=masked_args,
        has_env=srv.env_enc is not None and srv.env_enc != "",
        url=redact_secret_text(srv.url),
        has_headers=srv.headers_enc is not None and srv.headers_enc != "",
        auth_mode=srv.auth_mode,
        credential_mode=srv.credential_mode or "shared",
        my_oauth_status=my_oauth_status,
        has_client_id=srv.client_id_enc is not None and srv.client_id_enc != "",
        has_client_secret=bool(srv.client_secret_enc),
        oauth_status=srv.oauth_status,
        scopes=srv.scopes,
        authorize_url=redact_secret_text(srv.authorize_url),
        token_url=redact_secret_text(srv.token_url),
        registration_url=redact_secret_text(srv.registration_url),
        dcr_registered=srv.dcr_registered_at is not None,
        tags=srv.tags or [],
        enabled=srv.enabled,
        hide_from_chat=srv.hide_from_chat,
        investigation_tool_name=srv.investigation_tool_name,
        investigation_argument=srv.investigation_argument,
        relay_group_id=str(srv.relay_group_id) if srv.relay_group_id else None,
        is_company_managed=srv.is_company_managed,
        is_public_catalog=srv.is_public_catalog,
        shared_group_ids=srv.shared_group_ids,
        shared_group_permissions=srv.shared_group_permissions,
        shared_user_ids=srv.shared_user_ids,
        shared_user_permissions=srv.shared_user_permissions,
        access_restricted=srv.access_restricted,
        can_manage=can_manage,
        discovered_tools=tools,
        tool_count=len(srv.discovered_tools) if srv.discovered_tools else None,
        tools_discovered_at=(
            srv.tools_discovered_at.isoformat() if srv.tools_discovered_at else None
        ),
        call_count=srv.call_count or 0,
        last_called_at=(srv.last_called_at.isoformat() if srv.last_called_at else None),
        vault_secrets=vault_status(srv, vault_viewer if can_manage else None),
        created_at=srv.created_at.isoformat(),
        updated_at=srv.updated_at.isoformat(),
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("", response_model=list[MCPServerResponse])
async def list_mcp_servers(
    scope: EntityListScope = Query("visible", description=ENTITY_LIST_SCOPE_DESCRIPTION),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List MCP servers visible to the current user (own + group-shared + company-managed).

    ``scope=all`` lists every server for an admin (what a detail page links to).
    """
    user_group_ids = await get_user_group_ids(db, user.id)
    result = await db.execute(
        user_visible_query(
            MCPServer, user.id, user_group_ids, is_admin=list_scope_admin_bypass(scope, user)
        )
    )
    servers = result.scalars().all()
    names = await resolve_owner_names(db, servers)
    viewer = principal_from(user, user_group_ids)
    mine = await _my_oauth_statuses(db, servers, user.id)
    responses = [
        _to_response(
            srv,
            can_manage=can_manage(user, srv, user_group_ids),
            owner_name=names.get(str(srv.user_id)),
            vault_viewer=viewer,
            my_oauth_status=mine.get(srv.id),
        )
        for srv in servers
    ]
    await attach_vault_failures(responses, MCPServer.__tablename__)
    return responses


@router.post("", response_model=MCPServerResponse, status_code=status.HTTP_201_CREATED)
async def create_mcp_server(
    body: MCPServerCreate,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _agentic_gate: None = Depends(require_agentic_ai),
):
    """Create a new MCP server configuration."""
    # Only admins can create company-managed servers
    require_admin_for_company_managed(
        body, user, "Only admins can create company-managed MCP servers"
    )
    await require_vault_writer(db, user, headers=body.headers, env=body.env, client_secret=body.client_secret)
    await require_allowed_token_url(body.token_url)
    relay_group_id = await checked_relay_group_id(db, user=user, value=body.relay_group_id)
    _refuse_stdio_relay(body.transport, relay_group_id)
    await require_allowed_mcp_server_url(
        body.url, transport=body.transport, routed=relay_group_id is not None
    )

    srv = MCPServer(
        user_id=user.id,
        name=body.name,
        transport=body.transport,
        command=body.command,
        args=body.args,
        env_enc=encrypt_value(json.dumps(body.env)) if body.env else None,
        url=body.url,
        headers_enc=encrypt_value(json.dumps(body.headers)) if body.headers else None,
        auth_mode=body.auth_mode,
        credential_mode=body.credential_mode,
        client_id_enc=encrypt_value(body.client_id) if body.client_id else None,
        client_secret_enc=(
            encrypt_value(body.client_secret) if body.client_secret else None
        ),
        scopes=body.scopes,
        authorize_url=body.authorize_url,
        token_url=body.token_url,
        registration_url=body.registration_url,
        tags=body.tags or [],
        enabled=body.enabled,
        hide_from_chat=body.hide_from_chat,
        investigation_tool_name=(body.investigation_tool_name or "").strip() or None,
        investigation_argument=(body.investigation_argument or "").strip() or None,
        relay_group_id=relay_group_id,
        is_company_managed=body.is_company_managed,
    )
    await apply_group_share_fields(db, srv, body, partial=False)
    await apply_user_share_fields(db, srv, body, partial=False)
    db.add(srv)
    await db.flush()
    await log_audit(
        db,
        user=user,
        action="create",
        entity_type="mcp_server",
        entity_id=srv.id,
        entity_name=srv.name,
        summary=f"Created MCP server '{srv.name}'",
        request=request,
        details={
            "transport": srv.transport,
            "url": redact_secret_text(srv.url),
            "is_company_managed": srv.is_company_managed,
            "tags": srv.tags or [],
            "hide_from_chat": srv.hide_from_chat,
            "relay_group_id": str(gid) if (gid := getattr(srv, "relay_group_id", None)) else None,
        },
    )
    await db.commit()
    await db.refresh(srv)
    return _to_response(
        srv, can_manage=can_manage(user, srv), owner_name=user.full_name
    )


@router.get("/{server_id}", response_model=MCPServerResponse)
async def get_mcp_server(
    server_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get a single MCP server (must be own, group-shared, company-managed, or admin)."""
    user_group_ids = await get_user_group_ids(db, user.id)
    srv = await get_user_mcp_server(
        db, user.id, server_id, user_group_ids, is_admin=user.is_admin
    )
    owner_name = None
    if srv.user_id:
        owner_row = await db.execute(
            select(User.full_name).where(User.id == srv.user_id)
        )
        owner_name = owner_row.scalar_one_or_none()
    response = _to_response(
        srv,
        can_manage=can_manage(user, srv, user_group_ids),
        owner_name=owner_name,
        vault_viewer=principal_from(user, user_group_ids),
        my_oauth_status=(await _my_oauth_statuses(db, [srv], user.id)).get(srv.id),
    )
    await attach_vault_failures([response], MCPServer.__tablename__)
    return response


@router.put("/{server_id}", response_model=MCPServerResponse)
async def update_mcp_server(
    server_id: str,
    body: MCPServerUpdate,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Update an MCP server (creator or admin only)."""
    user_group_ids = await get_user_group_ids(db, user.id)
    # Locked until the commit: the stored-reference check below must see what an
    # edit committed meanwhile wrote, or an admin's reference could follow this
    # editor's new destination.
    srv = await get_manageable_entity(
        db,
        MCPServer,
        user,
        server_id,
        action="edit",
        entity_label="MCP server",
        user_group_ids=user_group_ids,
        lock=True,
    )
    # Before anything is written, so a refusal leaves the server untouched.
    await require_vault_writer(db, user, headers=body.headers, env=body.env, client_secret=body.client_secret)
    # The arguments and URLs are read masked (``_to_response``): a masked
    # value sent back keeps the one it stands for. Restored into the body
    # before the re-point check below, which compares them to the stored ones,
    # so a value sent back as shown changes no destination.
    try:
        new_args = (
            restore_masked_args(body.args, srv.args) if body.args is not None else None
        )
    except MaskedArgumentError:
        raise HTTPException(
            status_code=422,
            detail=(
                "An argument is still masked and cannot be matched to exactly one "
                "stored value. Type the full value of each masked argument again."
            ),
        ) from None
    if new_args is not None:
        body.args = new_args
    urls: dict[str, str | None] = {}
    for field, label in MCP_SERVER_URL_LABELS.items():
        try:
            urls[field] = restore_masked_text(getattr(body, field), getattr(srv, field))
        except MaskedArgumentError:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"The {label} is still masked and does not match the stored "
                    f"one. Type the full {label} again."
                ),
            ) from None
        if urls[field] != getattr(body, field):
            setattr(body, field, urls[field])
    # A vault reference the editor could not have written may not follow a
    # destination they chose: such an edit replaces it, or is refused.
    replaced = {column for column, value in (("headers_enc", body.headers), ("env_enc", body.env), ("client_secret_enc", body.client_secret)) if value is not None}
    if (
        retained := retained_reference_writer_problem(
            srv, replaced=replaced, principal=principal_from(user, user_group_ids)
        )
    ) and _update_repoints_mcp_server(srv, body):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=retained)
    await require_allowed_token_url(urls["token_url"])
    # URLs compared trimmed: the client connects to the trimmed URL, and the
    # edit form sends it trimmed. The masked ones are restored into the body
    # above, so both sides compare real URLs.
    destination_before = ((srv.url or "").strip(), srv.transport, srv.relay_group_id)
    if "relay_group_id" in body.model_fields_set:
        srv.relay_group_id = await checked_relay_group_id(
            db, user=user, value=body.relay_group_id, baseline=srv.relay_group_id
        )

    _refuse_stdio_relay(body.transport or srv.transport, srv.relay_group_id)
    # Judged as the server will be once saved, and only when where it connects
    # changes: a new URL, or the stored one under a new transport or route (a
    # server taken off its relay group is reached from the platform's network
    # from then on). The edit form sends url and transport on every save, so
    # comparing values, not the fields sent, is what lets a server saved
    # before a stricter policy still be renamed or re-shared; the policy still
    # refuses its connections.
    destination_after = (
        (body.url if body.url is not None else srv.url or "").strip(),
        body.transport or srv.transport,
        srv.relay_group_id,
    )
    if destination_after != destination_before:
        await require_allowed_mcp_server_url(
            destination_after[0],
            transport=destination_after[1],
            routed=destination_after[2] is not None,
        )

    if body.name is not None:
        srv.name = body.name
    if body.transport is not None:
        srv.transport = body.transport
    if body.command is not None:
        srv.command = body.command
    if body.args is not None:
        srv.args = body.args
    if body.env is not None:
        srv.env_enc = encrypt_value(json.dumps(body.env))
    if body.url is not None:
        srv.url = urls["url"]
    if body.headers is not None:
        srv.headers_enc = encrypt_value(json.dumps(body.headers))
    if body.auth_mode is not None:
        srv.auth_mode = body.auth_mode
        # Reset OAuth tokens when switching auth mode
        if body.auth_mode == "headers":
            srv.oauth_status = "disconnected"
            srv.access_token_enc = None
            srv.refresh_token_enc = None
            srv.token_expires_at = None
            srv.token_issued_at = None
            # Nor any person's own OAuth account.
            from app.services.mcp_user_credentials import delete_user_credentials

            await delete_user_credentials(db, srv.id)
    mode_change = await _apply_credential_mode(db, srv, body.credential_mode)
    if body.client_id is not None:
        # An operator-entered client ID replaces any dynamically registered
        # one, so the row must stop claiming the credential is disposable —
        # otherwise a later ``invalid_client`` would silently discard
        # credentials only this operator can restore.
        srv.client_id_enc = encrypt_value(body.client_id) if body.client_id else None
        srv.dcr_registered_at = None
    if body.client_secret is not None:
        srv.client_secret_enc = (
            encrypt_value(body.client_secret) if body.client_secret else None
        )
    # A cleared field means "unset", which must land as NULL: an empty string
    # would be a configured-but-blank endpoint that discovery then refuses to
    # fill in (it only fills what is missing).
    if body.scopes is not None:
        srv.scopes = body.scopes.strip() or None
    if urls["authorize_url"] is not None:
        srv.authorize_url = urls["authorize_url"].strip() or None
    if urls["token_url"] is not None:
        srv.token_url = urls["token_url"].strip() or None
    if urls["registration_url"] is not None:
        srv.registration_url = urls["registration_url"].strip() or None
    if body.tags is not None:
        srv.tags = body.tags
    if body.enabled is not None:
        srv.enabled = body.enabled
    if body.hide_from_chat is not None:
        srv.hide_from_chat = body.hide_from_chat
    # `model_fields_set`, not `is not None`: withdrawing a designation means
    # writing NULL, which `is not None` can never express — the tool would
    # stay callable by every investigation until the server was deleted.
    if "investigation_tool_name" in body.model_fields_set:
        srv.investigation_tool_name = (body.investigation_tool_name or "").strip() or None
    if "investigation_argument" in body.model_fields_set:
        srv.investigation_argument = (body.investigation_argument or "").strip() or None
    # Group sharing
    await apply_group_share_fields(db, srv, body, partial=True)

    await handle_company_managed_toggle(
        db,
        user,
        srv,
        body.is_company_managed,
        agent_field="mcp_server_ids",
        keep_grants=wants_access_restriction(body, srv),
    )

    # Per-user sharing — after the company-managed toggle, since
    # ``access_restricted`` is only meaningful relative to it.
    await apply_user_share_fields(db, srv, body, partial=True)

    # Owner transfer (admin only)
    await apply_owner_transfer(
        db,
        actor=user,
        entity=srv,
        new_owner_id=body.user_id,
        entity_type="mcp_server",
        entity_name=srv.name,
        request=request,
    )

    # Public catalog toggle (admin-only for company-managed entities)
    if body.is_public_catalog is not None:
        if srv.is_company_managed and not user.is_admin:
            raise HTTPException(
                status_code=403,
                detail="Only admins can publish company-managed entities to the public catalog",
            )
        srv.is_public_catalog = body.is_public_catalog

    await log_audit(
        db,
        user=user,
        action="update",
        entity_type="mcp_server",
        entity_id=srv.id,
        entity_name=srv.name,
        summary=f"Updated MCP server '{srv.name}'",
        request=request,
        details={
            "transport": srv.transport,
            "url": redact_secret_text(srv.url),
            "is_company_managed": srv.is_company_managed,
            "tags": srv.tags or [],
            "hide_from_chat": srv.hide_from_chat,
            "relay_group_id": str(gid) if (gid := getattr(srv, "relay_group_id", None)) else None,
            "credential_mode": srv.credential_mode,
            **mode_change,
        },
    )
    await db.commit()
    await db.refresh(srv)
    owner_name = None
    if srv.user_id:
        owner_row = await db.execute(
            select(User.full_name).where(User.id == srv.user_id)
        )
        owner_name = owner_row.scalar_one_or_none()
    return _to_response(
        srv,
        can_manage=can_manage(user, srv, user_group_ids),
        owner_name=owner_name,
        my_oauth_status=(await _my_oauth_statuses(db, [srv], user.id)).get(srv.id),
    )


@router.delete("/{server_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_mcp_server(
    server_id: str,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Delete an MCP server (creator or admin only)."""
    srv = await get_manageable_entity(
        db, MCPServer, user, server_id, action="delete", entity_label="MCP server"
    )
    await log_audit(
        db,
        user=user,
        action="delete",
        entity_type="mcp_server",
        entity_id=srv.id,
        entity_name=srv.name,
        summary=f"Deleted MCP server '{srv.name}'",
        request=request,
        details={
            "transport": srv.transport,
            "is_company_managed": srv.is_company_managed,
        },
    )
    # Purge stale references from all agents' mcp_server_ids (pre-filtered by a
    # text ``contains`` so only rows mentioning the id are loaded).
    sid = str(srv.id)
    agents_result = await db.execute(
        select(Agent).where(cast(Agent.mcp_server_ids, Text).contains(sid))
    )
    for agent in agents_result.scalars().all():
        if agent.mcp_server_ids and sid in agent.mcp_server_ids:
            agent.mcp_server_ids = [v for v in agent.mcp_server_ids if v != sid]

    await db.delete(srv)
    await db.commit()


# ---------------------------------------------------------------------------
# OAuth authorize (uses per-server credentials from DB)
# ---------------------------------------------------------------------------


@router.post("/oauth/discover", response_model=MCPOAuthDiscoveryResponse)
async def discover_mcp_oauth(
    body: MCPOAuthDiscoveryRequest,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Probe a remote MCP server for its OAuth metadata.

    Lets the "Add server" form show what a URL supports *before* anything is
    saved — in particular whether the server allows dynamic client registration,
    which is what decides if the operator has to go and create an OAuth app by
    hand or can simply click Connect.

    Read-only and side-effect free: it fetches well-known documents and returns
    the endpoint URLs it found, never the remote response bodies.
    """
    from app.services.mcp_oauth_discovery import (
        discover_oauth_endpoints,
        fallback_registration_url,
        is_safe_endpoint,
    )

    url = (body.url or "").strip()
    if not url or not is_safe_endpoint(url):
        # State the actual rule. "Use https" alone would leave someone whose
        # URL was rejected believing plain HTTP is never allowed, when a
        # loopback host is exactly how a locally-run MCP server is reached.
        raise HTTPException(
            status_code=400,
            detail=(
                "Enter an https:// URL for the MCP server "
                "(http:// is accepted only for localhost)"
            ),
        )

    # One call fans out to several outbound requests against a caller-chosen
    # host, so throttle per user rather than per IP — the caller is
    # authenticated and the cost is theirs.
    from app.core.rate_limit import enforce_rate_limit

    await enforce_rate_limit(
        request,
        "mcp-oauth-discover",
        limit=20,
        window_seconds=60,
        bucket=str(user.id),
    )

    route = None
    relay_group_id = await checked_relay_group_id(db, user=user, value=body.relay_group_id)
    if relay_group_id is not None:
        route = (
            await load_egress_routes(db, [("mcp_server", "discovery", relay_group_id)])
        ).get("discovery")

    # Authenticating the caller opened a transaction that would pin a pool
    # connection for the whole probe chain (up to DISCOVERY_BUDGET_SECONDS of
    # outbound HTTP). Hand it back first — there is no further DB work here.
    from app.core.database import release_db_connection_before_loop

    await release_db_connection_before_loop(db)

    with egress_route(route):
        endpoints = await discover_oauth_endpoints(
            server_url=url,
            authorize_url=(body.authorize_url or "").strip() or None,
            token_url=(body.token_url or "").strip() or None,
        )
    # Report the endpoint registration would actually use, not just the
    # advertised one. Returning the advertised value while deriving
    # ``supports_dynamic_registration`` from the resolved one produced a
    # payload that claimed registration was possible with no URL to show for
    # it — and left the form with nothing to pre-fill in exactly the case the
    # operator most needs to see (a server that publishes no registration
    # endpoint, where the conventional ``{origin}/register`` is the only lead).
    registration_url = endpoints.registration_url or fallback_registration_url(
        endpoints
    )

    return MCPOAuthDiscoveryResponse(
        authorize_url=endpoints.authorize_url,
        token_url=endpoints.token_url,
        registration_url=registration_url,
        registration_url_advertised=endpoints.registration_url is not None,
        resource=endpoints.resource,
        scopes=endpoints.scopes,
        issuer=endpoints.issuer,
        discovered=endpoints.discovered,
        supports_dynamic_registration=bool(
            registration_url and security_option("mcp_outbound_dcr_enabled")
        ),
    )


@router.get("/{server_id}/authorize")
async def start_mcp_oauth(
    server_id: str,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Start the OAuth authorization-code flow for an MCP server.

    Client credentials are optional. With no ``client_id`` configured, the
    authorization server is discovered from the server URL (RFC 9728 ->
    RFC 8414) and this platform registers itself dynamically (RFC 7591) to mint
    one, which is then stored encrypted like any other credential. That is the
    MCP spec's intended onboarding path and it is what lets an operator connect
    a server knowing nothing but its URL.
    """
    from app.integrations.oauth import build_authorize_url_for_mcp_server
    from app.services.mcp_oauth_discovery import (
        MCPOAuthDiscoveryError,
        ensure_mcp_oauth_client,
    )
    from app.services.mcp_user_credentials import is_per_user

    user_group_ids = await get_user_group_ids(db, user.id)
    srv = await get_user_mcp_server(db, user.id, server_id, user_group_ids, is_admin=user.is_admin)

    per_user = is_per_user(srv)
    if not per_user:
        # A company-managed server holds one shared connection — its token lives on
        # the server row, exactly like the shared API key of a header-authenticated
        # company-managed server — so authorizing it grants every user of the
        # platform that access. Only an admin may do so.
        require_admin_for_company_managed(
            srv, user, "Only admins can authorize a company-managed MCP server"
        )
    # per_user: anyone who can see the server connects their own account; the
    # callback stores it on their own credential, never on the server row.

    if srv.auth_mode != "oauth":
        raise HTTPException(
            status_code=400,
            detail="MCP server is not configured for OAuth authentication",
        )

    try:
        with egress_route(await mcp_server_egress_route(db, srv)):
            setup = await ensure_mcp_oauth_client(db, srv)
    except MCPOAuthDiscoveryError as exc:
        # The message names the missing piece of configuration, and the person
        # who hit this button is the one who can supply it.
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if setup.registered:
        await log_audit(
            db,
            user=user,
            action="update",
            entity_type="mcp_server",
            entity_id=srv.id,
            entity_name=srv.name,
            summary=f"Registered an OAuth client for MCP server '{srv.name}'",
            details={"registration_url": redact_secret_text(srv.registration_url)},
            request=request,
        )
        await db.commit()

    # A refresh token, where the authorization server issues one on request:
    # without it a short-lived access token cannot be refreshed.
    if setup.offline_access_added:
        await log_audit(
            db,
            user=user,
            action="update",
            entity_type="mcp_server",
            entity_id=srv.id,
            entity_name=srv.name,
            summary=f"Requested a refresh token (offline_access) for MCP server '{srv.name}'",
            details={"scopes": srv.scopes},
            request=request,
        )
        await db.commit()

    url = await build_authorize_url_for_mcp_server(srv, str(user.id), per_user=per_user)
    return {"authorize_url": url}


@router.delete("/{server_id}/my-credential", status_code=status.HTTP_204_NO_CONTENT)
async def disconnect_my_mcp_credential(
    server_id: str,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Disconnect one's own account from a ``per_user`` MCP server (idempotent)."""
    from app.services.mcp_user_credentials import user_credential

    user_group_ids = await get_user_group_ids(db, user.id)
    srv = await get_user_mcp_server(db, user.id, server_id, user_group_ids, is_admin=user.is_admin)
    credential = await user_credential(db, srv.id, user.id)
    if credential is None:
        return None
    await db.delete(credential)
    await log_audit(
        db,
        user=user,
        action="update",
        entity_type="mcp_server",
        entity_id=srv.id,
        entity_name=srv.name,
        summary=f"Disconnected their own account from MCP server '{srv.name}'",
        request=request,
    )
    await db.commit()
    return None


@router.post("/{server_id}/test")
async def test_mcp_server(
    server_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Test connectivity to an MCP server and persist discovered tools.

    Three outcomes, not two. ``auth_required`` is the one that matters: a
    remote MCP server commonly answers ``initialize`` / ``tools/list`` to
    anyone and gates ``tools/call`` behind a bearer token, so reporting a plain
    success there tells the user everything is fine right up until every real
    call fails. It is returned both when the endpoint answered ``401`` and when
    it connected but advertises OAuth protection with no credential in hand.
    """
    srv = await get_manageable_entity(
        db, MCPServer, user, server_id, action="test", entity_label="MCP server"
    )

    from app.services.mcp_user_credentials import credential_for_runtime
    from app.tools.mcp_client import MCPManager

    # On a per_user server the test acts as the caller, with their own account.
    runtime = await build_mcp_runtime(srv, await credential_for_runtime(db, srv, user.id))
    manager = MCPManager()
    test_result = await manager.test_connection(runtime)
    status_value = test_result.get("status", "error")

    dirty = False

    # Remember where the server said its metadata lives. RFC 9728 allows that
    # URL to be somewhere no well-known guess would find, so a challenge seen
    # once is the only reliable way to discover such a server later.
    resource_metadata = test_result.get("resource_metadata")
    if resource_metadata and srv.oauth_metadata_url != resource_metadata:
        srv.oauth_metadata_url = resource_metadata
        dirty = True

    # Persist discovered tools whenever the server actually returned some —
    # including the auth_required case, where the catalog is real even though
    # calls will be refused until the connection is authorized.
    if status_value in ("ok", "auth_required") and test_result.get("tools"):
        srv.discovered_tools = test_result["tools"]
        srv.tools_discovered_at = datetime.now(UTC)
        dirty = True

    await db.commit()
    if dirty:
        await db.refresh(srv)

    safe_result: dict[str, Any] = {
        "status": status_value,
        "tools": test_result.get("tools"),
        "tool_count": test_result.get("tool_count"),
    }
    if status_value == "auth_required":
        safe_result["message"] = (
            "This server requires authorization. Connect it with OAuth to "
            "allow tool calls."
        )
        # Tells the UI whether the fix is one click (OAuth is configured) or
        # needs the auth mode switched over first.
        safe_result["oauth_configured"] = srv.auth_mode == "oauth"
    elif test_result.get("redirect_location"):
        # A redirect the MCP transport does not follow: the one
        # failure whose fix is a value to copy, so it is named. The location
        # comes redacted from ``_unfollowed_redirect``; the sentence is fixed.
        safe_result["message"] = (
            "Connection test failed: the server redirects to "
            f"{test_result['redirect_location']}, which the connection does not "
            "follow. If that is the intended server, save it with that URL."
        )
    elif status_value != "ok":
        safe_result["message"] = (
            "Connection test failed. Check the server configuration."
        )
    return safe_result


@router.post("/{server_id}/test-tool")
async def test_mcp_tool(
    server_id: str,
    body: MCPToolTestRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Test a single MCP tool by executing it with provided arguments."""
    srv = await get_manageable_entity(
        db, MCPServer, user, server_id, action="test tool on", entity_label="MCP server"
    )

    from app.services.mcp_user_credentials import credential_for_runtime
    from app.tools.base import ToolCall
    from app.tools.mcp_client import MCPArtifactScope, MCPManager

    runtime = await build_mcp_runtime(srv, await credential_for_runtime(db, srv, user.id))
    # A binary result is the tester's own file (listed in their Files), never
    # an unscoped row nobody but an admin could read.
    manager = MCPManager(artifact_scope=MCPArtifactScope(user_id=str(user.id)))
    try:
        # force=True: an explicit admin-initiated tool test must always
        # reach the wire, even while the connection circuit breaker is
        # open after repeated failures (same contract as test_connection).
        await manager.connect_server(runtime, force=True)
        call = ToolCall(id="test", name=body.tool_name, arguments=body.arguments)
        start = time.monotonic()
        result = await manager.execute_tool(call)
        duration_ms = int((time.monotonic() - start) * 1000)
        await manager.disconnect_all()
        if result.is_error:
            return {
                "status": "error",
                "message": result.content,
                "duration_ms": duration_ms,
            }
        return {"status": "ok", "output": result.content, "duration_ms": duration_ms}
    except Exception as exc:
        await manager.disconnect_all()
        logger.error("MCP tool test failed: %s", format_exception_message(exc), exc_info=True)
        return {
            "status": "error",
            "message": "Tool execution failed. Check the MCP server connection and tool configuration.",
        }


@router.post("/{server_id}/refresh-tools", response_model=MCPServerResponse)
async def refresh_mcp_tools(
    server_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Connect to the MCP server, rediscover tools, and persist them."""
    user_group_ids = await get_user_group_ids(db, user.id)
    srv = await get_manageable_entity(
        db,
        MCPServer,
        user,
        server_id,
        action="refresh tools on",
        entity_label="MCP server",
        user_group_ids=user_group_ids,
    )

    from app.services.mcp_user_credentials import credential_for_runtime
    from app.tools.mcp_client import MCPManager

    runtime = await build_mcp_runtime(srv, await credential_for_runtime(db, srv, user.id))
    manager = MCPManager()
    test_result = await manager.test_connection(runtime)

    if test_result.get("status") != "ok":
        raise HTTPException(
            status_code=502,
            detail=test_result.get("message", "Failed to connect to MCP server"),
        )

    srv.discovered_tools = test_result.get("tools", [])
    srv.tools_discovered_at = datetime.now(UTC)
    await db.commit()
    await db.refresh(srv)
    owner_name = None
    if srv.user_id:
        owner_row = await db.execute(
            select(User.full_name).where(User.id == srv.user_id)
        )
        owner_name = owner_row.scalar_one_or_none()
    return _to_response(
        srv,
        can_manage=can_manage(user, srv, user_group_ids),
        owner_name=owner_name,
    )


# ---------------------------------------------------------------------------
# Export / Import
# ---------------------------------------------------------------------------


@router.get("/{server_id}/export")
async def export_mcp_server(
    server_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Export an MCP server as a portable JSON bundle.

    The environment, the headers and the OAuth client credentials never
    travel. A caller who can see the server but not manage it also gets a
    credential in its arguments or URLs masked, as the Filigran AI Catalog
    shows a server.
    """
    from fastapi.responses import JSONResponse

    user_group_ids = await get_user_group_ids(db, user.id)
    srv = await get_user_mcp_server(
        db, user.id, server_id, user_group_ids, is_admin=user.is_admin
    )
    redact = not can_manage(user, srv, user_group_ids)

    def _url(value: str | None) -> str | None:
        return redact_secret_text(value) if redact else value

    bundle = {
        "version": 1,
        "export_type": "mcp_server",
        "exported_at": datetime.now(UTC).isoformat(),
        "mcp_server": {
            "name": srv.name,
            "transport": srv.transport,
            "command": srv.command,
            "args": redact_secret_args(srv.args) if redact else srv.args,
            "url": _url(srv.url),
            "auth_mode": srv.auth_mode,
            "scopes": srv.scopes,
            "authorize_url": _url(srv.authorize_url),
            "token_url": _url(srv.token_url),
            "registration_url": _url(srv.registration_url),
            "tags": srv.tags or [],
        },
    }
    from app.core.utils import build_content_disposition

    safe_name = srv.name.lower().replace(" ", "-")
    return JSONResponse(
        content=bundle,
        headers={
            "Content-Disposition": build_content_disposition(
                f"mcp-server-{safe_name}.json"
            )
        },
    )


# How long to wait on a configuration URL. Short on purpose: a user is sitting
# in front of a dialog waiting for it, and a raw-content host that has not
# answered in this long is not about to.
_CONFIG_FETCH_TIMEOUT_SECONDS = 10.0
_CONFIG_FETCH_MAX_REDIRECTS = 5
# The timeout above bounds each connect and each read, not their sum: a server
# that sends a byte every few seconds never trips it, and would hold the
# request for as long as it liked. This bounds the whole fetch.
_CONFIG_FETCH_TOTAL_SECONDS = 30.0

# Per user: one preview fans out to several outbound requests against a host
# the caller chose, so the cost is theirs (same budget as ``/oauth/discover``).
_CONFIG_FETCH_RATE_LIMIT = 20
_CONFIG_FETCH_RATE_WINDOW_SECONDS = 60

# Every fetch that produces no document answers this, whatever stopped it: a
# name that does not resolve or resolves inward, a refused or filtered port, a
# TLS failure, an HTTP error, a redirect loop or a refused redirect target, a
# refused peer address, a body over the cap (before or after decompression) or
# not UTF-8. Told apart, they would turn the URL box into a probe of what the
# backend's network position can reach; the cause goes to the log instead.
# What a caller can still measure is how long the answer took - a closed port
# answers at once, a filtered one after the connect timeout. That is not hidden:
# making every failure wait out the budget would cost every user, and the rate
# limit bounds what timing can teach. Refusals decided from the typed URL alone
# (not https, not parseable, a literal private address) stay specific: they say
# nothing the caller did not type, and they are what the user can act on.
_CONFIG_FETCH_FAILED_DETAIL = (
    "Could not fetch a configuration document from that URL. Check that it is a "
    "public https address that serves the file, or import the file instead."
)

_CONFIG_FETCH_HEADERS = {
    "Accept": "application/json, text/plain",
    # Asked for uncompressed, because a compressed body is decoded here, under
    # the cap (see _decoded_body), rather than by httpx without a bound.
    "Accept-Encoding": "identity",
}
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})

# A server may compress anyway (a pre-gzipped object in a bucket does). The
# codings decoded, with the zlib window to try for each, in order: "deflate" is
# a zlib stream by the RFC and sometimes a bare one in practice.
_CONTENT_CODING_WBITS: dict[str, tuple[int, ...]] = {
    "gzip": (zlib.MAX_WBITS | 16,),
    "x-gzip": (zlib.MAX_WBITS | 16,),
    "deflate": (zlib.MAX_WBITS, -zlib.MAX_WBITS),
}

_URL_IN_TEXT = re.compile(r"\b[a-z][a-z0-9+.-]*://\S+", re.IGNORECASE)

# Mirror ``MCPServer``'s own columns. Kept here rather than derived from the
# model so a column that shrinks fails a test instead of failing an import.
_MAX_NAME_LEN = 200
_MAX_COMMAND_LEN = 500
_MAX_URL_LEN = 2000


def _refusal_detail(exc: DestinationRefusedError, *, url: str | None = None) -> str:
    """Say why a URL was refused, in the words of the task at hand.

    The guard is shared with A2A and its messages name A2A partners, which is
    a puzzle for someone importing an MCP configuration — nothing on their
    screen mentions A2A. The machine-readable ``reason`` is what the guard
    promises to callers, so it is what gets translated here; the underlying
    text is kept only where it is already neutral.
    """
    where = f" ({url})" if url else ""
    if exc.reason == UNUSABLE_TRANSPORT_UNVERIFIED:
        return (
            f"A configuration URL must use https{where}. Plain http is refused "
            "rather than silently upgraded."
        )
    if exc.reason == UNUSABLE_CARD_MALFORMED:
        return f"That is not a usable URL{where}."
    return (
        f"That URL points somewhere this platform will not fetch from{where}: "
        f"{str(exc).rstrip('.')}."
    )


class _NoDocumentError(Exception):
    """A fetch that produced no document. The message is for the log only."""


def _url_host(url: str) -> str:
    try:
        return httpx.URL(url).host or "<no host>"
    except Exception:
        return "<unparseable>"


def _hosts_only(text: str) -> str:
    """``text`` with every URL in it reduced to its host.

    A configuration URL can carry a token in its query, credentials in its
    userinfo, and a path that is the secret itself (a secret gist), and an
    exception's text may quote any of them. The host is what makes a failure
    traceable; it is all the log gets.
    """
    return _URL_IN_TEXT.sub(lambda match: _url_host(match.group(0)), text)


def _no_document(url: str, exc: Exception) -> HTTPException:
    """Log why a fetch of ``url`` produced no document; answer the one way."""
    level = logging.INFO
    if isinstance(exc, _NoDocumentError):
        cause = str(exc)
    elif isinstance(exc, TimeoutError):
        cause = "timed out"
    elif isinstance(exc, httpx.HTTPError | httpx.InvalidURL | OSError | ValueError):
        cause = format_exception_message(exc)
    else:
        # Nothing a fetch is expected to raise, so worth a louder line - but no
        # traceback, whose exception line could quote the URL in full.
        level = logging.WARNING
        cause = f"unexpected {type(exc).__name__}: {format_exception_message(exc)}"
    logger.log(
        level, "MCP configuration fetch from %s failed: %s", _url_host(url), _hosts_only(cause)
    )
    return HTTPException(status_code=400, detail=_CONFIG_FETCH_FAILED_DETAIL)


def _checked_config_url(url: str) -> str:
    """The typed URL in the form the fetch will request, or a refusal saying why not.

    Decided from the string alone - no DNS, no connection - so a refusal here
    tells the caller nothing about the network and can name the problem. The
    URL is parsed by httpx, which makes the request, and the guard reads that
    same form, so the two cannot disagree about the host.
    """
    try:
        target = str(httpx.URL(url.strip()))
        assert_allowed(target, resolve=False)
    except DestinationRefusedError as exc:
        raise HTTPException(status_code=400, detail=_refusal_detail(exc, url=url)) from exc
    except (httpx.InvalidURL, ValueError) as exc:
        # Neither parser can read it (a port out of range, a stray control
        # character, an unbalanced bracket): the guard's own "malformed" answer.
        refusal = DestinationRefusedError(
            format_exception_message(exc), reason=UNUSABLE_CARD_MALFORMED
        )
        raise HTTPException(status_code=400, detail=_refusal_detail(refusal, url=url)) from exc
    return target


async def _assert_destination_allowed(url: str) -> None:
    """The guard on a URL about to be requested, DNS included.

    Its refusal is something the backend learned - what a name resolves to
    from here, where a server redirected - so it is a fetch that produced no
    document, not a message for the caller.
    """
    try:
        await assert_allowed_async(url)
    except DestinationRefusedError as exc:
        raise _NoDocumentError(f"refused: {exc}") from exc


async def _assert_peer_allowed(response: httpx.Response) -> None:
    """Re-check the address the socket actually reached.

    ``assert_allowed_async`` resolves the hostname, then httpx resolves it
    again when it connects — and a name the attacker controls can answer
    differently the second time. That DNS-to-connect gap is what makes an
    allow-check on the *name* insufficient; this closes it by asking the
    connection where it actually went, which is the one answer nothing can
    rewrite after the fact.

    Called before any body is read, so a rebound host is refused before it
    gets to say anything.
    """
    stream = response.extensions.get("network_stream")
    peer = stream.get_extra_info("server_addr") if stream else None
    if not peer:
        # No address to check rather than an address that passed. Refused
        # rather than waved through: this is the check that matters most, and
        # a transport that cannot answer is not a reason to skip it.
        raise _NoDocumentError("could not confirm the address connected to")
    host = str(peer[0])
    try:
        ipaddress.ip_address(host)
    except ValueError as exc:
        # Told not to resolve, the guard waves a hostname through unchecked, so
        # anything but an address here would pass the check that matters most.
        raise _NoDocumentError(f"connected to {host!r}, which is not an address") from exc
    literal = f"[{host}]" if ":" in host else host
    try:
        # resolve=False because there is nothing left to resolve — the literal
        # address is checked by the same classification a hostname gets.
        await assert_allowed_async(f"https://{literal}", resolve=False)
    except DestinationRefusedError as exc:
        raise _NoDocumentError(f"connected to a refused address: {exc}") from exc


def _decoded_body(raw: bytes, content_encoding: str | None) -> bytes:
    """Undo the response's content coding without inflating past the cap."""
    codings = [
        coding
        for coding in (part.strip().lower() for part in (content_encoding or "").split(","))
        if coding and coding != "identity"
    ]
    if not codings:
        return raw
    if len(codings) > 1 or codings[0] not in _CONTENT_CODING_WBITS:
        raise _NoDocumentError(f"unsupported Content-Encoding {(content_encoding or '')[:40]!r}")
    for wbits in _CONTENT_CODING_WBITS[codings[0]]:
        try:
            body = zlib.decompressobj(wbits).decompress(raw, MAX_DOCUMENT_BYTES + 1)
        except zlib.error:
            continue
        if len(body) > MAX_DOCUMENT_BYTES:
            raise _NoDocumentError("the decoded body is over the size cap")
        return body
    raise _NoDocumentError(f"the body is not valid {codings[0]}")


async def _read_capped(response: httpx.Response) -> str:
    """Read at most ``MAX_DOCUMENT_BYTES``, then stop.

    Streamed rather than buffered: ``.get()`` reads the whole body before any
    size check can run, so a reachable public URL could return a multi-gigabyte
    response and exhaust the worker long before the limit was consulted. Here
    the limit is enforced while reading, and one byte past it ends the transfer.

    Raw bytes rather than httpx's decoded stream, which inflates each chunk as
    it arrives with no bound: a few KB of gzip became tens of MB in memory
    before the size was ever compared. The cap holds on both sides of the
    decoder instead.
    """
    chunks: list[bytes] = []
    total = 0
    async for chunk in response.aiter_raw():
        total += len(chunk)
        if total > MAX_DOCUMENT_BYTES:
            raise _NoDocumentError("the body is over the size cap")
        chunks.append(chunk)
    body = _decoded_body(b"".join(chunks), response.headers.get("content-encoding"))
    try:
        return body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _NoDocumentError("the body is not UTF-8 text") from exc


async def _fetch_config_document(url: str) -> str:
    """Fetch a configuration document the user pointed at, or say there is none.

    The browser cannot do this itself — a raw-content host sends no CORS
    headers — so the backend makes the request, which turns a URL box into a
    request-forgery vector: whatever the user types, this server connects to.
    Three things stand between the two:

    * ``assert_allowed_async`` on the name, the same guard A2A applies to
      partner URLs (loopback, private, link-local, cloud metadata);
    * :func:`_assert_peer_allowed` on the address actually connected to, which
      is what makes the first check hold against a rebinding answer;
    * :func:`_read_capped`, so the size limit bounds memory rather than being
      consulted after the body is already in it.

    Redirects are followed by hand so every hop gets all three. ``url`` has
    been through :func:`_checked_config_url`; everything past that point that
    stops the fetch - the guard included - answers ``_CONFIG_FETCH_FAILED_DETAIL``.
    """
    hop = url
    try:
        async with (
            asyncio.timeout(_CONFIG_FETCH_TOTAL_SECONDS),
            httpx.AsyncClient(timeout=_CONFIG_FETCH_TIMEOUT_SECONDS, follow_redirects=False) as client,
        ):
            for _ in range(_CONFIG_FETCH_MAX_REDIRECTS):
                await _assert_destination_allowed(hop)
                async with client.stream("GET", hop, headers=_CONFIG_FETCH_HEADERS) as resp:
                    await _assert_peer_allowed(resp)
                    if resp.status_code in _REDIRECT_STATUSES:
                        location = resp.headers.get("location")
                        if not location:
                            raise _NoDocumentError(f"HTTP {resp.status_code} without a Location")
                        hop = str(httpx.URL(hop).join(location))
                        continue
                    if resp.status_code >= 400:
                        raise _NoDocumentError(f"HTTP {resp.status_code}")
                    return await _read_capped(resp)
            raise _NoDocumentError("too many redirects")
    except Exception as exc:
        # Deliberately broad: a failure type nobody anticipated must not become
        # a 500, which would be one more answer a probe could tell apart.
        raise _no_document(hop, exc) from exc


@router.post("/import/config/preview", response_model=MCPConfigPreviewResponse)
async def preview_mcp_config_import(
    body: MCPConfigPreviewRequest,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _agentic_gate: None = Depends(require_agentic_ai),
):
    """Read a configuration document and report what could be imported.

    Nothing is written. The user sees what was found — and what was not, and
    why — before anything reaches their list, which is the whole point of
    splitting this from the import itself.
    """
    if bool(body.content) == bool(body.url):
        raise HTTPException(
            status_code=400, detail="Provide either a file's content or a URL, not both."
        )

    raw = body.content
    if body.url:
        # Decided from the string alone, so a URL that could never be fetched
        # spends neither the caller's budget nor an outbound request.
        target = _checked_config_url(body.url)
        await enforce_rate_limit(
            request,
            "mcp-config-fetch",
            limit=_CONFIG_FETCH_RATE_LIMIT,
            window_seconds=_CONFIG_FETCH_RATE_WINDOW_SECONDS,
            bucket=str(user.id),
        )
        # Authenticating the caller opened a transaction; hand its pool
        # connection back before up to five slow outbound hops.
        await release_db_connection_before_loop(db)
        raw = await _fetch_config_document(target)

    try:
        preview = parse_mcp_config(raw or "")
    except ConfigImportError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return MCPConfigPreviewResponse(
        format=preview.format,
        candidates=[MCPConfigCandidate(**asdict(c)) for c in preview.candidates],
        warnings=preview.warnings,
    )


@router.post("/import/config", response_model=MCPConfigImportResponse)
async def import_mcp_config(
    body: MCPConfigImportRequest,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _agentic_gate: None = Depends(require_agentic_ai),
):
    """Create the servers the user picked out of a previewed configuration.

    Each entry is committed on its own: a document that carries one server
    whose name is already taken should still import the other nine, and the
    response says which ones did not land rather than failing the batch.
    """
    if not body.servers:
        raise HTTPException(status_code=400, detail="No servers selected.")
    if len(body.servers) > MAX_SERVERS_PER_DOCUMENT:
        raise HTTPException(
            status_code=400,
            detail=f"At most {MAX_SERVERS_PER_DOCUMENT} servers can be imported at once.",
        )

    created: list[MCPServerResponse] = []
    failed: list[dict[str, str]] = []
    principal: VaultPrincipal | None = None

    for candidate in body.servers:
        name = candidate.name.strip()
        if not name:
            failed.append({"name": candidate.name, "error": "Name is required."})
            continue
        if candidate.transport not in {"stdio", "sse", "streamable_http"}:
            failed.append({"name": name, "error": f"Unknown transport '{candidate.transport}'."})
            continue
        if candidate.transport == "stdio" and not (candidate.command or "").strip():
            failed.append({"name": name, "error": "A stdio server needs a command."})
            continue
        if candidate.transport != "stdio" and not (candidate.url or "").strip():
            failed.append({"name": name, "error": "A remote server needs a URL."})
            continue

        # The columns are bounded (name 200, command 500, url 2000) and the
        # values come from a file or a URL, so nothing has trimmed them. Left
        # to the database these raise DataError mid-batch; checked here they
        # are one reported entry, with a message naming the field.
        too_long = next(
            (
                f"{label} is too long ({len(value)} characters, limit {limit})."
                for label, value, limit in (
                    ("Name", name, _MAX_NAME_LEN),
                    ("Command", (candidate.command or ""), _MAX_COMMAND_LEN),
                    ("URL", (candidate.url or ""), _MAX_URL_LEN),
                )
                if len(value) > limit
            ),
            None,
        )
        if too_long:
            failed.append({"name": name[:_MAX_NAME_LEN], "error": too_long})
            continue

        # A remote URL taken from an imported document is a destination this
        # platform will later connect to, so it is held to the rule of one
        # typed into the form (the MCP server URL policy) and, since nobody
        # typed it, to the A2A destination guard too: https, public addresses.
        if candidate.url:
            # Both guards may wait on DNS (seconds each, per candidate): no
            # pool connection is held meanwhile.
            await release_db_connection_before_loop(db)
            try:
                await assert_allowed_async(candidate.url)
            except DestinationRefusedError as exc:
                failed.append({"name": name, "error": _refusal_detail(exc)})
                continue
            if candidate.transport in REMOTE_TRANSPORTS:
                try:
                    await assert_server_url_allowed(candidate.url, routed=False)
                except MCPServerURLRefusedError as exc:
                    failed.append({"name": name, "error": str(exc)})
                    continue

        # A vault reference in an imported document is held to the rules of
        # one typed into the form: a known provider, an allowed path, a role
        # the provider lets write it.
        secret_fields = {"env": candidate.env, "headers": candidate.headers}
        vault_problem = secret_fields_problem(secret_fields)
        if vault_problem is None and any(holds_any_reference(value) for value in secret_fields.values()):
            principal = principal or await vault_principal(db, user)
            vault_problem = secret_fields_writer_problem(secret_fields, principal=principal)
        if vault_problem:
            failed.append({"name": name, "error": vault_problem})
            continue

        srv = MCPServer(
            user_id=user.id,
            name=name,
            transport=candidate.transport,
            command=candidate.command if candidate.transport == "stdio" else None,
            args=candidate.args if candidate.transport == "stdio" else None,
            env_enc=(
                encrypt_value(json.dumps(candidate.env))
                if candidate.transport == "stdio" and candidate.env
                else None
            ),
            url=candidate.url if candidate.transport != "stdio" else None,
            headers_enc=(
                encrypt_value(json.dumps(candidate.headers))
                if candidate.transport != "stdio" and candidate.headers
                else None
            ),
            auth_mode="headers",
            tags=body.tags or [],
            enabled=True,
            is_company_managed=False,
        )
        db.add(srv)
        try:
            await db.flush()
        except (IntegrityError, DataError) as exc:
            # IntegrityError is the duplicate name; DataError backstops any
            # value the checks above do not bound (a long args entry, say).
            # Either way one entry fails and the batch carries on.
            await db.rollback()
            failed.append({"name": name, "error": format_exception_message(exc)})
            continue

        await log_audit(
            db,
            user=user,
            action="import",
            entity_type="mcp_server",
            entity_id=srv.id,
            entity_name=srv.name,
            summary=f"Imported MCP server '{srv.name}' from a configuration file",
            request=request,
            details={"transport": srv.transport, "url": redact_secret_text(srv.url), "source": candidate.source},
        )
        await db.commit()
        await db.refresh(srv)
        created.append(
            _to_response(
                srv, can_manage=can_manage(user, srv), owner_name=user.full_name
            )
        )

    return MCPConfigImportResponse(created=created, failed=failed)


@router.post(
    "/import", response_model=MCPServerResponse, status_code=status.HTTP_201_CREATED
)
async def import_mcp_server(
    body: ImportMCPServerRequest,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Import an MCP server from an export bundle."""
    if body.version != 1:
        raise HTTPException(
            status_code=400, detail=f"Unsupported export version: {body.version}"
        )
    if body.export_type != "mcp_server":
        raise HTTPException(
            status_code=400, detail=f"Unsupported export type: {body.export_type}"
        )

    data = body.mcp_server
    if not data.get("name"):
        raise HTTPException(status_code=400, detail="MCP server name is required")
    token_url = data.get("token_url")
    if token_url is not None and not isinstance(token_url, str):
        raise HTTPException(status_code=400, detail="token_url must be a string")
    await require_allowed_token_url(token_url)
    # An export bundle is untyped JSON: held to what the form's model checks.
    transport = data.get("transport", "sse")
    if transport not in {"stdio", "sse", "streamable_http"}:
        raise HTTPException(
            status_code=422, detail="transport must be one of stdio, sse, streamable_http"
        )
    if data.get("auth_mode") not in {None, "headers", "oauth"}:
        raise HTTPException(status_code=422, detail="auth_mode must be headers or oauth")
    url = data.get("url")
    if url is not None and not isinstance(url, str):
        raise HTTPException(status_code=422, detail="url must be a string")
    if problem := url_problem(url):
        raise HTTPException(status_code=422, detail=problem)
    await require_allowed_mcp_server_url(url, transport=transport, routed=False)

    srv = MCPServer(
        user_id=user.id,
        name=data["name"],
        transport=transport,
        command=data.get("command"),
        args=data.get("args"),
        url=url,
        auth_mode=data.get("auth_mode"),
        scopes=data.get("scopes"),
        authorize_url=data.get("authorize_url"),
        token_url=token_url,
        registration_url=data.get("registration_url"),
        tags=data.get("tags", []),
        is_company_managed=False,
    )
    db.add(srv)
    await db.flush()
    await log_audit(
        db,
        user=user,
        action="import",
        entity_type="mcp_server",
        entity_id=srv.id,
        entity_name=srv.name,
        summary=f"Imported MCP server '{srv.name}'",
        request=request,
        details={
            "transport": srv.transport,
            "url": redact_secret_text(srv.url),
            "tags": srv.tags or [],
        },
    )
    await db.commit()
    await db.refresh(srv)
    return _to_response(
        srv, can_manage=can_manage(user, srv), owner_name=user.full_name
    )
