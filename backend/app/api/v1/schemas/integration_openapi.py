# SPDX-License-Identifier: Apache-2.0

"""Pydantic schemas for OpenAPI / Swagger integrations.

A source is EITHER a ``url`` (fetched server-side), ``content`` — the text of
an uploaded ``.json`` / ``.yaml`` / ``.yml`` file — OR ``archive``, a ZIP of a
spec split across files, base64-encoded. The browser reads the file and sends
it as JSON (no multipart: the same shape serves preview, create, replace).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from app.services.openapi_import import MAX_ARCHIVE_BYTES, MAX_OPERATIONS, MAX_SPEC_BYTES

#: Base64 length of the largest archive (4 characters per 3 bytes).
_MAX_ARCHIVE_CHARS = (MAX_ARCHIVE_BYTES + 2) // 3 * 4


class OpenAPISourceRequest(BaseModel):
    """Where the document comes from (exactly one of ``url`` / ``content`` / ``archive``)."""

    url: str | None = Field(default=None, max_length=2000)
    content: str | None = Field(default=None, max_length=MAX_SPEC_BYTES)
    #: A ZIP archive of the spec's files, base64-encoded.
    archive: str | None = Field(default=None, max_length=_MAX_ARCHIVE_CHARS)
    filename: str | None = Field(default=None, max_length=255)
    #: Prefix for every tool name ("" for none); default: derived from the title.
    tool_prefix: str | None = Field(default=None, max_length=64)


class OpenAPIToolOverride(BaseModel):
    """A tool name and description set by the user (empty = the generated one)."""

    name: str | None = Field(default=None, max_length=64)
    description: str | None = Field(default=None, max_length=2000)


def overrides_payload(overrides: dict[str, OpenAPIToolOverride] | None) -> dict[str, dict] | None:
    """Plain dicts for the import service (``None`` stays ``None``)."""
    if overrides is None:
        return None
    return {key: value.model_dump(exclude_none=True) for key, value in overrides.items()}


class OpenAPIImportOptions(OpenAPISourceRequest):
    """A source plus the choices made on its preview."""

    #: Operation keys ("GET /pets/{petId}") to expose; ``None`` = the default
    #: selection (every supported, non-deprecated operation).
    selected_operations: list[str] | None = Field(default=None, max_length=MAX_OPERATIONS)
    #: Id of the authentication option (from the preview); ``None`` = default.
    auth_option: str | None = Field(default=None, max_length=300)
    #: Tool names and descriptions by operation key.
    tool_overrides: dict[str, OpenAPIToolOverride] | None = Field(default=None, max_length=MAX_OPERATIONS)


class OpenAPISelectionRequest(BaseModel):
    selected_operations: list[str] = Field(max_length=MAX_OPERATIONS)
    #: Replaces the tool names and descriptions; ``None`` keeps them.
    tool_overrides: dict[str, OpenAPIToolOverride] | None = Field(default=None, max_length=MAX_OPERATIONS)


class OpenAPIServerInfo(BaseModel):
    url: str
    description: str | None = None
    name: str | None = None


class OpenAPITagInfo(BaseModel):
    name: str
    description: str | None = None
    #: OpenAPI 3.2: a short display name, and the tag this one nests under.
    summary: str | None = None
    parent: str | None = None


class OpenAPIAuthOptionInfo(BaseModel):
    id: str
    kind: str
    label: str
    scheme_name: str | None = None
    location: str | None = None
    param_name: str | None = None
    authorize_url: str | None = None
    token_url: str | None = None
    device_authorization_url: str | None = None
    scopes: dict[str, str] = {}
    #: Scopes to request by default when not every declared one should be.
    default_scopes: list[str] | None = None
    description: str | None = None
    supported: bool = True
    unsupported_reason: str | None = None
    deprecated: bool = False
    #: The integration ``auth_type`` storing this option's credential.
    integration_auth_type: str


class OpenAPIOperationInfo(BaseModel):
    key: str
    method: str
    path: str
    operation_id: str | None = None
    #: The tool's name and description as agents see them.
    tool_name: str | None = None
    tool_description: str | None = None
    #: What the conversion generated, before any user override.
    default_tool_name: str | None = None
    default_tool_description: str | None = None
    summary: str = ""
    tags: list[str] = []
    deprecated: bool = False
    tool_type: str = "read"
    sends_credential: bool = True
    supported: bool = True
    skip_reason: str | None = None
    #: Whether a tool argument carries a file.
    accepts_files: bool = False
    warnings: list[str] = []
    selected: bool = False


class OpenAPIPreviewResponse(BaseModel):
    spec_version: str
    document_format: str
    title: str
    api_version: str
    description: str = ""
    source_url: str | None = None
    servers: list[OpenAPIServerInfo] = []
    default_server_url: str | None = None
    auth_options: list[OpenAPIAuthOptionInfo]
    default_auth_option: str
    tool_prefix: str
    tags: list[OpenAPITagInfo] = []
    operations: list[OpenAPIOperationInfo]
    default_selection: list[str]
    warnings: list[str] = []
    operation_count: int
    supported_count: int


class ClientCertificateInfo(BaseModel):
    """The client certificate an integration presents (never its key)."""

    subject: str
    issuer: str
    not_before: str
    not_after: str
    fingerprint_sha256: str
    expired: bool
    #: Expires within 30 days.
    expires_soon: bool = False
    chain_length: int


class ClientCertificateRequest(BaseModel):
    """A PEM certificate (chain) and its private key, optionally encrypted."""

    certificate: str = Field(max_length=64 * 1024)
    private_key: str = Field(max_length=32 * 1024)
    passphrase: str | None = Field(default=None, max_length=1024)


class OpenAPISpecResponse(BaseModel):
    integration_id: str
    source_type: str
    source_url: str | None = None
    source_filename: str | None = None
    spec_version: str
    document_format: str
    title: str
    api_version: str
    description: str | None = None
    default_server_url: str | None = None
    tool_prefix: str
    auth_option: OpenAPIAuthOptionInfo | None = None
    auth_options: list[OpenAPIAuthOptionInfo] = []
    tags: list[OpenAPITagInfo] = []
    operations: list[OpenAPIOperationInfo]
    selected_count: int
    supported_count: int
    warnings: list[str] = []
    #: The client certificate presented on every call (mutual TLS).
    client_certificate: ClientCertificateInfo | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    refreshed_at: datetime | None = None


class OpenAPIRefreshResponse(BaseModel):
    changed: bool
    #: Operations now in the spec that are not selected (new ones stay off).
    added_operations: list[str] = []
    #: Previously selected operations that are gone or no longer supported.
    removed_operations: list[str] = []
    spec: OpenAPISpecResponse


class DeviceAuthorizationInfo(BaseModel):
    """What the user needs to sign in with a code (device authorization)."""

    flow_id: str
    user_code: str
    verification_uri: str
    verification_uri_complete: str | None = None
    expires_in: int
    interval: int


class DevicePollRequest(BaseModel):
    flow_id: str = Field(max_length=100)


class DevicePollResponse(BaseModel):
    #: pending | connected | denied | expired | failed
    status: str
    interval: int | None = None
    message: str | None = None


class ImplicitTokenRequest(BaseModel):
    """The URL fragment of an OAuth2 implicit redirect, as the frontend read it."""

    state: str = Field(max_length=200)
    access_token: str | None = Field(default=None, max_length=16 * 1024)
    token_type: str | None = Field(default=None, max_length=40)
    expires_in: int | str | None = None
    scope: str | None = Field(default=None, max_length=4000)
    error: str | None = Field(default=None, max_length=200)
    error_description: str | None = Field(default=None, max_length=1000)


class ImplicitTokenResponse(BaseModel):
    integration_id: str
    provider: str


def auth_option_info(option: Any) -> OpenAPIAuthOptionInfo:
    """Serialise an ``AuthOption`` (object or stored dict)."""
    from app.services.openapi_import import AuthOption

    if isinstance(option, dict):
        option = AuthOption.from_dict(option)
    return OpenAPIAuthOptionInfo(
        id=option.id,
        kind=option.kind,
        label=option.label,
        scheme_name=option.scheme_name,
        location=option.location,
        param_name=option.param_name,
        authorize_url=option.authorize_url,
        token_url=option.token_url,
        device_authorization_url=option.device_authorization_url,
        scopes=dict(option.scopes or {}),
        default_scopes=list(option.default_scopes) if option.default_scopes is not None else None,
        description=option.description,
        supported=option.supported,
        unsupported_reason=option.unsupported_reason,
        deprecated=option.deprecated,
        integration_auth_type=option.integration_auth_type,
    )
