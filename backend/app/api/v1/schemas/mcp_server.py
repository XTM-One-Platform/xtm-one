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

"""Pydantic schemas for MCP server API routes."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, field_validator

from app.api.v1.schemas.column_limits import column_length_validator
from app.api.v1.schemas.vault_references import (
    VaultSecretStatus,
    no_vault_reference_validator,
    vault_reference_validator,
)
from app.models.mcp_server import MCPServer
from app.tools.mcp_destinations import url_problem

_MCP_SERVER_COLUMN_FIELDS = (
    "name",
    "transport",
    "command",
    "url",
    "auth_mode",
    "investigation_tool_name",
    "investigation_argument",
)
_OAUTH_URL_FIELDS = ("authorize_url", "token_url", "registration_url")

#: The values the MCP client knows; anything else is a 422, never a server
#: that fails when it is first used.
MCPTransport = Literal["stdio", "sse", "streamable_http"]
MCPAuthMode = Literal["headers", "oauth"]


def _checked_server_url(cls: type, value: str | None) -> str | None:
    """The part of the MCP server URL policy no lookup is needed for (422).

    The rest - the addresses a name resolves to, which depend on the relay
    group - is the route's (``helpers.require_allowed_mcp_server_url``).
    """
    problem = url_problem(value)
    if problem:
        raise ValueError(problem)
    return value


class MCPServerCreate(BaseModel):
    name: str
    transport: MCPTransport = "stdio"

    # stdio fields
    command: str | None = None
    args: list[str] | None = None
    env: dict[str, str] | None = None  # plain env vars (encrypted on save)

    # sse / streamable_http fields
    url: str | None = None
    headers: dict[str, str] | None = None  # plain headers (encrypted on save)

    # Authentication mode for remote transports
    auth_mode: MCPAuthMode = "headers"

    # OAuth fields (used when auth_mode == "oauth").
    # All of them are optional: with none set, the platform discovers the
    # authorization server from ``url`` and registers itself dynamically
    # (RFC 7591) the first time the connection is authorized.
    client_id: str | None = None  # plain (encrypted on save)
    client_secret: str | None = None  # plain (encrypted on save)
    scopes: str | None = None
    authorize_url: str | None = None
    token_url: str | None = None
    registration_url: str | None = None

    tags: list[str] | None = None
    enabled: bool = True
    hide_from_chat: bool = False
    is_company_managed: bool = False
    # --- Deep Investigation source designation ---
    # None/"" = not an investigation source. Never inferred from
    # ``discovered_tools``: see ``app.models.mcp_server`` for why a tool an
    # investigation may call has to be named by a person.
    investigation_tool_name: str | None = None
    # None = read the argument off the tool's input schema at call time.
    investigation_argument: str | None = None
    # The relay group a remote server is reached through; one the
    # writer can see. None = reached directly. Never used by stdio.
    relay_group_id: str | None = None
    shared_group_ids: list[str] | None = None
    shared_group_permissions: dict[str, str] | None = None
    shared_user_ids: list[str] | None = None
    shared_user_permissions: dict[str, str] | None = None
    access_restricted: bool | None = None

    _fit_columns = column_length_validator(MCPServer, *_MCP_SERVER_COLUMN_FIELDS, *_OAUTH_URL_FIELDS)
    _checked_url = field_validator("url")(_checked_server_url)
    _vault_references = vault_reference_validator("env", "headers", "client_secret")
    _no_vault_references = no_vault_reference_validator("client_id")


class MCPServerUpdate(BaseModel):
    name: str | None = None
    transport: MCPTransport | None = None
    command: str | None = None
    args: list[str] | None = None
    env: dict[str, str] | None = None
    url: str | None = None
    headers: dict[str, str] | None = None

    auth_mode: MCPAuthMode | None = None
    client_id: str | None = None
    client_secret: str | None = None
    scopes: str | None = None
    authorize_url: str | None = None
    token_url: str | None = None
    registration_url: str | None = None

    tags: list[str] | None = None
    enabled: bool | None = None
    hide_from_chat: bool | None = None
    is_company_managed: bool | None = None
    # --- Deep Investigation source designation ---
    # None/"" = not an investigation source. Never inferred from
    # ``discovered_tools``: see ``app.models.mcp_server`` for why a tool an
    # investigation may call has to be named by a person.
    investigation_tool_name: str | None = None
    # None = read the argument off the tool's input schema at call time.
    investigation_argument: str | None = None
    # Owner (``user_id``) transfer — admin only. Ignored when omitted/unchanged.
    user_id: str | None = None
    is_public_catalog: bool | None = None
    # Sent as null it reaches the server directly again; omitted, unchanged.
    relay_group_id: str | None = None
    shared_group_ids: list[str] | None = None
    shared_group_permissions: dict[str, str] | None = None
    shared_user_ids: list[str] | None = None
    shared_user_permissions: dict[str, str] | None = None
    access_restricted: bool | None = None

    _fit_columns = column_length_validator(MCPServer, *_MCP_SERVER_COLUMN_FIELDS)
    # Stripped by the route before they are written.
    _fit_oauth_urls = column_length_validator(MCPServer, *_OAUTH_URL_FIELDS, stripped=True)
    _checked_url = field_validator("url")(_checked_server_url)
    _vault_references = vault_reference_validator("env", "headers", "client_secret")
    _no_vault_references = no_vault_reference_validator("client_id")


class MCPToolInfo(BaseModel):
    name: str
    description: str
    input_schema: dict | None = None


class MCPServerResponse(BaseModel):
    id: str
    user_id: str | None = None
    owner_name: str | None = None
    name: str
    transport: str
    command: str | None = None
    args: list[str] | None = None  # Masked: tokens/secrets replaced with ***
    has_env: bool = False  # True if env vars are configured (secrets not exposed)
    url: str | None = None
    has_headers: bool = False

    # OAuth
    auth_mode: str = "headers"
    has_client_id: bool = False
    #: Whether an OAuth client secret (a value or a vault reference) is stored; never the value.
    has_client_secret: bool = False
    oauth_status: str = "disconnected"
    scopes: str | None = None
    authorize_url: str | None = None
    token_url: str | None = None
    registration_url: str | None = None
    # True when ``has_client_id`` came from dynamic registration rather than
    # operator-entered credentials — the UI labels it so nobody goes looking
    # for a client ID they never created.
    dcr_registered: bool = False

    tags: list[str] = []
    enabled: bool
    hide_from_chat: bool = False
    is_company_managed: bool = False
    is_public_catalog: bool = False
    shared_group_ids: list[str] | None = None
    shared_group_permissions: dict[str, str] | None = None
    shared_user_ids: list[str] | None = None
    shared_user_permissions: dict[str, str] | None = None
    access_restricted: bool | None = None
    #: Whether the REQUESTING user may edit it - ``helpers.can_manage``, share
    #: tiers included, so the UI never offers an edit the API refuses.
    can_manage: bool = False
    # --- Deep Investigation source designation ---
    # None/"" = not an investigation source. Never inferred from
    # ``discovered_tools``: see ``app.models.mcp_server`` for why a tool an
    # investigation may call has to be named by a person.
    investigation_tool_name: str | None = None
    # None = read the argument off the tool's input schema at call time.
    investigation_argument: str | None = None
    relay_group_id: str | None = None
    discovered_tools: list[MCPToolInfo] | None = None
    tool_count: int | None = None
    tools_discovered_at: str | None = None
    call_count: int = 0
    last_called_at: str | None = None
    #: The headers / environment read from a vault, and their recent failures.
    vault_secrets: VaultSecretStatus | None = None
    created_at: str
    updated_at: str


class MCPToolTestRequest(BaseModel):
    tool_name: str
    arguments: dict = {}


class MCPOAuthDiscoveryRequest(BaseModel):
    """Probe a remote MCP server for its OAuth metadata before saving it."""

    url: str
    # Optional hints for a server that publishes no metadata: given an
    # Authorize URL, discovery still looks for a registration endpoint at its
    # authorization server.
    authorize_url: str | None = None
    token_url: str | None = None
    # The relay group the server will be reached through: discovery
    # then probes from that network instead of the platform's.
    relay_group_id: str | None = None


class MCPOAuthDiscoveryResponse(BaseModel):
    authorize_url: str | None = None
    token_url: str | None = None
    #: The registration endpoint that would actually be used. Either the one
    #: the authorization server advertises or, when it advertises none, the
    #: conventional ``{origin}/register``. Non-null exactly when
    #: ``supports_dynamic_registration`` is true and registration is enabled.
    registration_url: str | None = None
    #: Whether ``registration_url`` was advertised by the server rather than
    #: derived from convention — a derived one may simply 404.
    registration_url_advertised: bool = False
    resource: str | None = None
    scopes: str | None = None
    issuer: str | None = None
    #: The server answered at least one well-known probe.
    discovered: bool = False
    #: A ``client_id`` can be obtained automatically — no credentials needed.
    supports_dynamic_registration: bool = False


class ImportMCPServerRequest(BaseModel):
    """Import an MCP server from an export bundle."""

    version: int
    export_type: str = "mcp_server"
    mcp_server: dict


# ---------------------------------------------------------------------------
# Importing an MCP *configuration* document
#
# Distinct from ``ImportMCPServerRequest`` above, which restores this
# platform's own export bundle. These carry the files the rest of the
# ecosystem writes — a client's ``mcpServers`` block, or a vendor's
# ``server.json`` — and are parsed by ``app.services.mcp_config_import``.
# ---------------------------------------------------------------------------


class MCPConfigPreviewRequest(BaseModel):
    """A configuration to read, given either inline or by URL.

    Exactly one of the two is expected. ``content`` is what the browser read
    from a file the user picked — sent as text rather than multipart because
    the document is small and this keeps one code path for both sources.
    ``url`` is fetched by the backend, since the browser cannot: an arbitrary
    raw-content host will not send CORS headers.
    """

    content: str | None = None
    url: str | None = None


class MCPConfigCandidate(BaseModel):
    """One importable server, already in this platform's vocabulary."""

    name: str
    transport: str
    command: str | None = None
    args: list[str] = []
    env: dict[str, str] = {}
    url: str | None = None
    headers: dict[str, str] = {}
    #: Keys the document declared as needed but gave no usable value for.
    #: Advisory only — the server is still importable without them.
    missing_secrets: list[str] = []
    #: Where in the document this came from, so a preview row can explain
    #: itself when several entries share a name.
    source: str = ""


class MCPConfigPreviewResponse(BaseModel):
    #: "mcp_servers" for a client config, "server_json" for a registry entry.
    format: str
    candidates: list[MCPConfigCandidate] = []
    #: Entries that could not be used, each with the reason. Never fatal: a
    #: document can yield candidates and warnings at the same time.
    warnings: list[str] = []


class MCPConfigImportRequest(BaseModel):
    """The subset of a preview the user confirmed."""

    servers: list[MCPConfigCandidate]
    tags: list[str] | None = None


class MCPConfigImportResponse(BaseModel):
    created: list[MCPServerResponse] = []
    #: Names that could not be created, with the reason — a duplicate name,
    #: say. Reported per entry so one collision does not lose the batch.
    failed: list[dict[str, str]] = []
