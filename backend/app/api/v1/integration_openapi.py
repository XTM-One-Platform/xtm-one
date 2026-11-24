# SPDX-License-Identifier: Apache-2.0

"""OpenAPI / Swagger integrations: preview, spec detail, selection, refresh, replace.

Creating one goes through the ordinary ``POST /integrations`` (with
``provider = "openapi"`` and an ``openapi`` block): every sharing, licensing
and audit rule of integration creation applies unchanged, and the spec row is
written in the same transaction. The helpers that path needs live here, next
to the endpoints that manage the spec afterwards.

Any user may import: the fetch reaches public and private-network hosts
(loopback, link-local and cloud-metadata addresses are refused), the same
network scope the resulting tools call. Admins can switch the whole provider
off from the integration catalog, which also closes these endpoints.

Routes (mounted under ``/api/v1/integrations``):

* ``POST /openapi/preview``                 — parse a URL, file or archive, list operations
* ``GET  /{integration_id}/openapi``        — the stored spec and its selection
* ``PUT  /{integration_id}/openapi/operations`` — change which operations are tools
* ``POST /{integration_id}/openapi/refresh``    — re-fetch a URL-sourced spec
* ``PUT  /{integration_id}/openapi``            — replace the spec (new URL / file)
* ``PUT / DELETE /{integration_id}/openapi/client-certificate`` — the client
  certificate presented on every call (mutual TLS)
* ``POST /{integration_id}/openapi/device/poll`` — whether a device-code
  sign-in (started by ``GET /{integration_id}/authorize``) completed
* ``POST /openapi/oauth/implicit``           — the token of an OAuth2 implicit
  sign-in, posted by the frontend page the callback redirects to
"""

from __future__ import annotations

import base64
import binascii
import json
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, require_agentic_ai_licensed
from app.api.v1.helpers import (
    can_authorize_integration,
    can_manage,
    get_user_group_ids,
    get_user_integration,
    require_admin_for_company_managed,
)
from app.api.v1.schemas.integration_openapi import (
    ClientCertificateInfo,
    ClientCertificateRequest,
    DeviceAuthorizationInfo,
    DevicePollRequest,
    DevicePollResponse,
    ImplicitTokenRequest,
    ImplicitTokenResponse,
    OpenAPIImportOptions,
    OpenAPIOperationInfo,
    OpenAPIPreviewResponse,
    OpenAPIRefreshResponse,
    OpenAPISelectionRequest,
    OpenAPIServerInfo,
    OpenAPISourceRequest,
    OpenAPISpecResponse,
    OpenAPITagInfo,
    auth_option_info,
    overrides_payload,
)
from app.core.database import get_db
from app.core.rate_limit import enforce_rate_limit
from app.core.security import decrypt_value, encrypt_value
from app.integrations.client_certificate import (
    CERTIFICATE_KEY,
    CERTIFICATE_KEYS,
    PASSPHRASE_KEY,
    PRIVATE_KEY_KEY,
    ClientCertificateError,
    certificate_info,
    parse_client_certificate,
    stored_certificate,
)
from app.integrations.granted_scopes import record_granted_scopes
from app.integrations.oauth import apply_oauth_token_expiry, get_state_data
from app.integrations.oauth_device import (
    STATUS_CONNECTED,
    cancel_device_authorization,
    poll_device_authorization,
    start_device_authorization,
)
from app.integrations.oauth_token_grants import TokenGrantError, check_bearer
from app.integrations.providers.openapi import decrypt_extra, integration_ssl_context
from app.models.integration import Integration
from app.models.integration_openapi_spec import IntegrationOpenAPISpec
from app.models.user import User
from app.services.audit import log_audit
from app.services.declarative_http_executor import same_origin
from app.services.integration_user_credentials import (
    holder_ref,
    may_connect_own_account,
    signs_in_own_account,
    store_tokens,
    upsert_user_credential,
)
from app.services.openapi_import import (
    AUTH_TYPE_CLIENT_CREDENTIALS,
    AUTH_TYPE_OAUTH_PASSWORD,
    KIND_MUTUAL_TLS,
    OPENAPI_PROVIDER,
    AuthOption,
    ImportPlan,
    OpenAPIImportError,
    SpecAnalysis,
    analyze_source,
    apply_plan,
    carry_tool_overrides,
    get_spec,
    new_spec_row,
    prepare_import,
    update_selection,
)
from app.services.openapi_import.service import resolve_auth_option
from app.services.vault import reveal_field


async def _ensure_provider_enabled(db: AsyncSession = Depends(get_db)) -> None:
    """Refuse every OpenAPI route while an admin has the provider switched off."""
    await ensure_openapi_enabled(db)


async def ensure_openapi_enabled(db: AsyncSession) -> None:
    """403 while an admin has the OpenAPI provider switched off.

    The routes' dependency, and the agent builder's integration tools' check
    before they import or replace a spec.
    """
    from app.api.v1.platform_settings_helpers import get_platform_settings

    ps = await get_platform_settings(db)
    if OPENAPI_PROVIDER in set(ps.disabled_integration_providers or []):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="OpenAPI integrations are disabled by an admin.",
        )


router = APIRouter(dependencies=[Depends(_ensure_provider_enabled)])

#: Spec loads (preview, refresh, replace) one user may start per minute: each
#: can fetch up to hundreds of caller-chosen URLs.
_SPEC_FETCHES_PER_MINUTE = 20


# ---------------------------------------------------------------------------
# Helpers shared with POST /integrations
# ---------------------------------------------------------------------------


async def limit_spec_fetches(request: Request | None, user: User) -> None:
    """429 once *user* started too many spec loads this minute (each fetches URLs)."""
    await enforce_rate_limit(
        request,
        "openapi-spec-fetch",
        limit=_SPEC_FETCHES_PER_MINUTE,
        window_seconds=60,
        bucket=str(user.id),
    )



def _bad_request(exc: Exception) -> HTTPException:
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))


def _archive_bytes(source: Any) -> bytes | None:
    """The uploaded ZIP archive of a source request, decoded (None without one)."""
    raw = getattr(source, "archive", None)
    if not raw:
        return None
    try:
        return base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="The archive must be sent base64-encoded.",
        ) from None


async def prepare_openapi_import(body: Any) -> tuple[ImportPlan, IntegrationOpenAPISpec]:
    """Load, convert and validate a create request's ``openapi`` block.

    Returns the plan and its (not yet attached) spec row; raises a 400 with
    the document's problem so nothing is created from a bad spec.
    """
    return await plan_openapi_import(getattr(body, "openapi", None))


async def plan_openapi_import(
    options: OpenAPIImportOptions | None,
) -> tuple[ImportPlan, IntegrationOpenAPISpec]:
    """The plan and spec row of an ``openapi`` block (``POST /integrations``'s).

    Shared with the agent builder's ``create_integration`` tool, so a spec
    imported by an agent is loaded and validated exactly as the route does.
    """
    if options is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="An OpenAPI integration needs a spec: provide its URL or file content.",
        )
    try:
        plan = await prepare_import(
            url=options.url,
            content=options.content,
            filename=options.filename,
            selected_operations=options.selected_operations,
            auth_option_id=options.auth_option,
            tool_prefix=options.tool_prefix,
            tool_overrides=overrides_payload(options.tool_overrides),
            archive=_archive_bytes(options),
        )
        row = new_spec_row(None, plan)
    except OpenAPIImportError as exc:
        raise _bad_request(exc) from None
    return plan, row


def openapi_extra_config(extra: dict | None, plan: ImportPlan) -> dict:
    """The integration's config with a base URL — the spec's server by default.

    A base URL is mandatory: without one no tool can be called, and a spec
    whose servers are relative or absent (a file upload) cannot supply it.
    """
    config = dict(extra or {})
    base_url = str(config.get("base_url") or "").strip() or (plan.analysis.default_server_url or "")
    if not base_url:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Enter the API base URL: the spec does not declare an absolute server URL.",
        )
    if not base_url.lower().startswith(("http://", "https://")):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="The API base URL must start with http:// or https://.",
        )
    config["base_url"] = base_url.rstrip("/")
    _check_client_certificate(config, required=plan.auth_option.kind == KIND_MUTUAL_TLS)
    return config


def _check_client_certificate(config: dict[str, Any], *, required: bool) -> None:
    """Validate (and tidy) a client certificate given in a create request's config."""
    stored = stored_certificate(config)
    if stored is None:
        if any(config.get(key) for key in CERTIFICATE_KEYS):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Provide both the client certificate and its private key.",
            )
        for key in CERTIFICATE_KEYS:
            config.pop(key, None)
        if required:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="This API authenticates with a client certificate: add the certificate and its private key.",
            )
        return
    try:
        parse_client_certificate(*stored)
    except ClientCertificateError as exc:
        raise _bad_request(exc) from None
    config[CERTIFICATE_KEY], config[PRIVATE_KEY_KEY] = stored[0], stored[1]
    if stored[2]:
        config[PASSPHRASE_KEY] = stored[2]
    else:
        config.pop(PASSPHRASE_KEY, None)


def token_grant_status(integration: Integration) -> str:
    """``connected`` once a client-credentials or password-grant integration is complete.

    The password grant needs the user's login (``api_key``) and the token URL;
    its client id is optional. Client credentials need the client id and
    secret too.
    """
    has_token_url = bool((integration.token_url or "").strip())
    if integration.auth_type == AUTH_TYPE_OAUTH_PASSWORD:
        complete = has_token_url and bool(integration.api_key_enc)
    else:
        complete = has_token_url and bool(integration.client_id_enc and integration.client_secret_enc)
    return "connected" if complete else "disconnected"


def openapi_audit_details(plan: ImportPlan) -> dict[str, Any]:
    analysis = plan.analysis
    return {
        "source_type": plan.source_type,
        "source_url": plan.source_url,
        "spec_version": analysis.spec_version,
        "title": analysis.title,
        "operations_selected": len(plan.selected_keys),
        "operations_supported": len(analysis.supported_keys),
        "auth_option": plan.auth_option.id,
    }


# ---------------------------------------------------------------------------
# Serialisation
# ---------------------------------------------------------------------------


def preview_response(analysis: SpecAnalysis) -> OpenAPIPreviewResponse:
    selected = set(analysis.default_selection)
    return OpenAPIPreviewResponse(
        spec_version=analysis.spec_version,
        document_format=analysis.document_format,
        title=analysis.title,
        api_version=analysis.api_version,
        description=analysis.description,
        source_url=analysis.source_url,
        servers=[OpenAPIServerInfo(**s) for s in analysis.servers],
        default_server_url=analysis.default_server_url,
        auth_options=[auth_option_info(o) for o in analysis.auth_options],
        default_auth_option=analysis.default_auth_option,
        tool_prefix=analysis.tool_prefix,
        tags=[OpenAPITagInfo(**t) for t in analysis.tags],
        operations=[
            OpenAPIOperationInfo(
                **op.catalogue_entry(),
                selected=op.key in selected,
                tool_description=(op.tool or {}).get("description"),
                default_tool_name=op.tool_name,
                default_tool_description=(op.tool or {}).get("description"),
            )
            for op in analysis.operations
        ],
        default_selection=analysis.default_selection,
        warnings=analysis.warnings,
        operation_count=len(analysis.operations),
        supported_count=len(analysis.supported_keys),
    )


def spec_response(
    row: IntegrationOpenAPISpec, integration: Integration | None = None
) -> OpenAPISpecResponse:
    selected = set(row.selected_operation_keys or [])
    operations = [op for op in row.operations or [] if isinstance(op, dict)]
    tags = [OpenAPITagInfo(**t) for t in row.tags or [] if isinstance(t, dict) and t.get("name")]
    known = {t.name for t in tags}
    for op in operations:
        for tag in op.get("tags") or []:
            if tag not in known:
                known.add(tag)
                tags.append(OpenAPITagInfo(name=tag))
    certificate = certificate_info(decrypt_extra(integration)) if integration is not None else None
    auth_options = [o for o in row.auth_options or [] if AuthOption.from_dict(o)]
    return OpenAPISpecResponse(
        integration_id=str(row.integration_id),
        source_type=row.source_type,
        source_url=row.source_url,
        source_filename=row.source_filename,
        spec_version=row.spec_version,
        document_format=row.document_format,
        title=row.api_title,
        api_version=row.api_version,
        description=row.api_description,
        default_server_url=row.default_server_url,
        tool_prefix=row.tool_prefix,
        auth_option=auth_option_info(row.auth_option) if AuthOption.from_dict(row.auth_option) else None,
        auth_options=[auth_option_info(o) for o in auth_options],
        tags=tags,
        operations=[_operation_info(op, row, selected) for op in operations],
        selected_count=len(row.selected_operation_keys or []),
        supported_count=sum(1 for op in operations if op.get("supported")),
        warnings=list(row.warnings or []),
        client_certificate=ClientCertificateInfo(**certificate.to_dict()) if certificate else None,
        created_at=row.created_at,
        updated_at=row.updated_at,
        refreshed_at=row.refreshed_at,
    )


def _operation_info(
    op: dict[str, Any], row: IntegrationOpenAPISpec, selected: set[str]
) -> OpenAPIOperationInfo:
    """A stored operation with its tool as agents see it and as generated."""
    key = op.get("key")
    tool = (row.tool_definitions or {}).get(key)
    tool = tool if isinstance(tool, dict) else {}
    override = (row.tool_overrides or {}).get(key)
    override = override if isinstance(override, dict) else {}
    fields = {k: v for k, v in op.items() if k in OpenAPIOperationInfo.model_fields}
    fields["default_tool_name"] = op.get("tool_name")
    fields["default_tool_description"] = tool.get("description")
    fields["tool_name"] = override.get("name") or op.get("tool_name")
    fields["tool_description"] = override.get("description") or tool.get("description")
    return OpenAPIOperationInfo(**fields, selected=key in selected)


# ---------------------------------------------------------------------------
# Access
# ---------------------------------------------------------------------------


async def _openapi_integration(
    db: AsyncSession, user: User, integration_id: str, *, manage: bool
) -> Integration:
    """A visible (or, with *manage*, editable) OpenAPI integration.

    Edit authority is exactly ``PUT /integrations/{id}``'s: admin for a
    company-managed integration, config-tier for a group-shared one, never a
    built-in.
    """
    user_group_ids = await get_user_group_ids(db, user.id)
    integration = await get_user_integration(
        db, user.id, integration_id, user_group_ids, is_admin=user.is_admin
    )
    if integration.provider != OPENAPI_PROVIDER:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This integration is not an OpenAPI integration.",
        )
    if manage:
        require_admin_for_company_managed(
            integration, user, "Only admins can edit company-managed integrations"
        )
        if not can_manage(user, integration, user_group_ids) or integration.is_builtin:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You don't have permission to edit this integration.",
            )
    return integration


async def _spec_or_404(db: AsyncSession, integration: Integration) -> IntegrationOpenAPISpec:
    row = await get_spec(db, integration.id)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No API specification is attached to this integration.",
        )
    return row


def _keep_auth_option(row: IntegrationOpenAPISpec, analysis: SpecAnalysis) -> AuthOption:
    """The stored option re-read in a new version of the document.

    A spec that stopped declaring the scheme keeps the stored option: the
    integration's credential still matches it, and switching silently would
    send it differently.
    """
    stored = AuthOption.from_dict(row.auth_option)
    if stored is not None:
        current = analysis.auth_option(stored.id)
        if current is not None and current.supported and current.kind == stored.kind:
            return current
        return stored
    return resolve_auth_option(analysis, None)


#: The OAuth settings holding an endpoint (the rest, ``scopes``, is not a URL).
_OAUTH_ENDPOINT_SETTINGS = frozenset({"authorize_url", "token_url"})


def _follow_oauth_option(
    integration: Integration, previous: AuthOption | None, current: AuthOption
) -> tuple[list[str], list[str]]:
    """Move the integration's OAuth endpoints and scopes to *current*'s.

    A setting follows the spec where the option's value for it changed
    (another option, or the same one redeclared); one the spec left as it was
    keeps any edit made to it, and an empty one is filled. An endpoint is
    never moved to ANOTHER origin, though: the stored client secret, the
    password of a password grant or a device code would go to a server the
    user never chose, on a re-published (or compromised) spec. Returns
    ``(settings changed, endpoints kept because the spec moved them)``.
    """
    if integration.auth_type not in ("oauth", AUTH_TYPE_CLIENT_CREDENTIALS, AUTH_TYPE_OAUTH_PASSWORD):
        return [], []
    before = previous.oauth_settings() if previous is not None else {}
    changed: list[str] = []
    kept: list[str] = []
    for name, value in current.oauth_settings().items():
        stored = getattr(integration, name)
        if stored != value and (value != before.get(name) or not stored):
            if name in _OAUTH_ENDPOINT_SETTINGS and stored and value and not same_origin(value, stored):
                kept.append(name)
                continue
            setattr(integration, name, value)
            changed.append(name)
    return changed, kept


def _kept_endpoints_warning(integration: Integration, kept: list[str]) -> str:
    labels = {"authorize_url": "authorization URL", "token_url": "token URL"}
    named = " and ".join(f"{labels[name]} ({getattr(integration, name)})" for name in kept)
    return (
        f"The spec now places its OAuth endpoints on another server: the integration keeps its "
        f"{named}. Change it in the integration's configuration if the API moved."
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.post("/openapi/preview", response_model=OpenAPIPreviewResponse)
async def preview_openapi_spec(
    body: OpenAPISourceRequest,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _agentic_gate: None = Depends(require_agentic_ai_licensed),
):
    """Parse a Swagger / OpenAPI document (JSON or YAML) and list its operations.

    Nothing is stored. The same source is sent again with the chosen
    operations and authentication when the integration is created.

    Every source may make the platform fetch URLs the caller chose (the spec,
    the documents it references, its discovery documents), on private
    networks too: the route is rate-limited per user, and a URL source is
    audited whether it loads or not.
    """
    await limit_spec_fetches(request, user)
    source_url = (body.url or "").strip()
    try:
        _document, analysis = await analyze_source(
            url=body.url,
            content=body.content,
            tool_prefix=body.tool_prefix,
            archive=_archive_bytes(body),
        )
    except OpenAPIImportError as exc:
        if source_url:
            await log_audit(
                db,
                user=user,
                action="preview_openapi_spec",
                entity_type="integration",
                entity_name=source_url[:200],
                summary="Tried to preview an OpenAPI spec from a URL",
                request=request,
                details={"source_url": source_url, "error": str(exc)[:300]},
            )
            await db.commit()
        raise _bad_request(exc) from None
    if source_url:
        # A server-side fetch of a user-chosen URL: keep a trace of who
        # pointed the platform where.
        await log_audit(
            db,
            user=user,
            action="preview_openapi_spec",
            entity_type="integration",
            entity_name=analysis.title,
            summary=f"Previewed OpenAPI spec '{analysis.title}' from a URL",
            request=request,
            details={"source_url": source_url, "spec_version": analysis.spec_version},
        )
        await db.commit()
    return preview_response(analysis)


@router.get("/{integration_id}/openapi", response_model=OpenAPISpecResponse)
async def get_openapi_spec(
    integration_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """The integration's spec: source, operations (with selection), auth option."""
    integration = await _openapi_integration(db, user, integration_id, manage=False)
    return spec_response(await _spec_or_404(db, integration), integration)


@router.put("/{integration_id}/openapi/operations", response_model=OpenAPISpecResponse)
async def update_openapi_operations(
    integration_id: str,
    body: OpenAPISelectionRequest,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Choose which operations are tools (no re-import needed)."""
    integration = await _openapi_integration(db, user, integration_id, manage=True)
    row = await _spec_or_404(db, integration)
    previous = list(row.selected_operation_keys or [])
    previous_overrides = dict(row.tool_overrides or {})
    try:
        selected = update_selection(
            row, body.selected_operations, overrides_payload(body.tool_overrides)
        )
    except OpenAPIImportError as exc:
        raise _bad_request(exc) from None
    await log_audit(
        db,
        user=user,
        action="update",
        entity_type="integration",
        entity_id=integration.id,
        entity_name=integration.name,
        summary=f"Changed the operations exposed by '{integration.name}' ({len(selected)} selected)",
        request=request,
        details={
            "openapi_operations_added": [k for k in selected if k not in previous][:50],
            "openapi_operations_removed": [k for k in previous if k not in selected][:50],
            "openapi_tools_renamed": sorted(
                k
                for k, v in (row.tool_overrides or {}).items()
                if previous_overrides.get(k) != v
            )[:50],
        },
    )
    await db.commit()
    return spec_response(row, integration)


@router.post("/{integration_id}/openapi/refresh", response_model=OpenAPIRefreshResponse)
async def refresh_openapi_spec(
    integration_id: str,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Re-fetch a URL-sourced spec, keeping the selection and the auth option.

    Newly added operations stay OFF (reported as ``added_operations``): an
    API growing a DELETE endpoint must not hand it to agents unasked.
    Selected operations that vanished are dropped (``removed_operations``).
    OAuth endpoints and scopes the new version changes are taken over.
    """
    integration = await _openapi_integration(db, user, integration_id, manage=True)
    row = await _spec_or_404(db, integration)
    await limit_spec_fetches(request, user)
    if row.source_type != "url" or not row.source_url:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This spec was uploaded as a file: replace it with a new file instead.",
        )
    try:
        document, analysis = await analyze_source(
            url=row.source_url, content=None, tool_prefix=row.tool_prefix
        )
    except OpenAPIImportError as exc:
        raise _bad_request(exc) from None

    previous = list(row.selected_operation_keys or [])
    supported = set(analysis.supported_keys)
    kept = [k for k in previous if k in supported]
    known_before = {op.get("key") for op in row.operations or [] if isinstance(op, dict)}
    added = [k for k in analysis.supported_keys if k not in known_before]
    removed = [k for k in previous if k not in supported]
    changed = document.sha256 != row.document_sha256
    previous_option = AuthOption.from_dict(row.auth_option)

    plan = ImportPlan(
        document=document,
        analysis=analysis,
        selected_keys=[k for k in analysis.supported_keys if k in set(kept)],
        auth_option=_keep_auth_option(row, analysis),
        source_type="url",
        source_url=row.source_url,
        source_filename=None,
        tool_overrides=carry_tool_overrides(row.tool_overrides, analysis.tool_definitions()),
    )
    try:
        apply_plan(row, plan)
    except OpenAPIImportError as exc:
        raise _bad_request(exc) from None
    details: dict[str, Any] = {
        "source_url": row.source_url,
        "changed": changed,
        "openapi_operations_new": added[:50],
        "openapi_operations_removed": removed[:50],
    }
    oauth_changed, oauth_kept = _follow_oauth_option(integration, previous_option, plan.auth_option)
    if oauth_changed:
        details["oauth_settings_updated"] = oauth_changed
    if oauth_kept:
        details["oauth_endpoints_not_moved"] = oauth_kept
        row.warnings = [*(row.warnings or []), _kept_endpoints_warning(integration, oauth_kept)]
    await log_audit(
        db,
        user=user,
        action="update",
        entity_type="integration",
        entity_id=integration.id,
        entity_name=integration.name,
        summary=f"Refreshed the OpenAPI spec of '{integration.name}'",
        request=request,
        details=details,
    )
    await db.commit()
    return OpenAPIRefreshResponse(
        changed=changed,
        added_operations=added,
        removed_operations=removed,
        spec=spec_response(row, integration),
    )


@router.put("/{integration_id}/openapi", response_model=OpenAPISpecResponse)
async def replace_openapi_spec(
    integration_id: str,
    body: OpenAPIImportOptions,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Replace the spec with a new URL or file (and a new selection).

    An OpenAPI integration holding no spec yet (created before the spec
    import, or whose import never ran) gets its first one here, under the
    same rules: see :func:`attach_spec`.

    The authentication option may change only within the integration's
    credential type — an API key scheme for another API key scheme, say —
    because the stored credential is what the new option will send. OAuth
    endpoints and scopes follow the chosen option (a device-code option keeps
    its device authorization endpoint as ``authorize_url``).
    """
    integration = await _openapi_integration(db, user, integration_id, manage=True)
    # The integration row locked before the spec is read (as the builder's
    # update_integration loads it): a second first attach waits, then finds
    # the winner's spec and replaces it. The unique index answers 409 below
    # for a writer that did not take the lock.
    await db.refresh(integration, with_for_update=True)
    # The sharing or company-managed state may have changed while this call
    # waited on the lock: the edit authority is checked again on the locked row.
    integration = await _openapi_integration(db, user, integration_id, manage=True)
    row = await get_spec(db, integration.id)
    await limit_spec_fetches(request, user)
    if row is None:
        row, details = await attach_spec(db, integration, body)
        summary = f"Attached an OpenAPI spec to '{integration.name}'"
    else:
        details = await replace_spec(integration, row, body)
        summary = f"Replaced the OpenAPI spec of '{integration.name}'"
    await log_audit(
        db,
        user=user,
        action="update",
        entity_type="integration",
        entity_id=integration.id,
        entity_name=integration.name,
        summary=summary,
        request=request,
        details=details,
    )
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A spec was attached to this integration meanwhile: send the request again to replace it.",
        ) from None
    return spec_response(row, integration)


def _integration_server_url(integration: Integration, row: IntegrationOpenAPISpec) -> str | None:
    """The server URL the integration already calls: its base URL, else the stored spec's server.

    What a replacement document with no ``servers`` and no location of its own
    (an uploaded file) resolves its relative OAuth URLs against.
    """
    try:
        stored = decrypt_extra(integration).get("base_url")
    except Exception:  # an unreadable config only loses the fallback
        stored = None
    for candidate in (stored, row.default_server_url):
        if isinstance(candidate, str) and candidate.strip().lower().startswith(("http://", "https://")):
            return candidate.strip()
    return None


async def replace_spec(
    integration: Integration, row: IntegrationOpenAPISpec, body: OpenAPIImportOptions
) -> dict[str, Any]:
    """Load *body*'s spec and write it onto *row*; the audit details of the change.

    Shared by ``PUT /{integration_id}/openapi`` and the agent builder's
    ``update_integration`` tool, so both validate a replacement the same way.
    Raises a 400 with the document's problem; nothing is committed here.
    """
    previous_option = AuthOption.from_dict(row.auth_option)
    try:
        plan = await prepare_import(
            server_url_fallback=_integration_server_url(integration, row),
            url=body.url,
            content=body.content,
            filename=body.filename,
            selected_operations=body.selected_operations,
            auth_option_id=body.auth_option,
            tool_prefix=body.tool_prefix if body.tool_prefix is not None else row.tool_prefix,
            tool_overrides=overrides_payload(body.tool_overrides),
            archive=_archive_bytes(body),
        )
    except OpenAPIImportError as exc:
        raise _bad_request(exc) from None
    if body.tool_overrides is None:
        plan.tool_overrides = carry_tool_overrides(
            row.tool_overrides, plan.analysis.tool_definitions()
        )
    if plan.auth_option.integration_auth_type != integration.auth_type:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"'{plan.auth_option.label}' needs a different kind of credential than "
                "this integration stores. Create a new integration for it, or pick an "
                "option using the same kind of credential."
            ),
        )
    try:
        apply_plan(row, plan)
    except OpenAPIImportError as exc:
        raise _bad_request(exc) from None
    if (
        plan.auth_option.kind == KIND_MUTUAL_TLS
        and stored_certificate(decrypt_extra(integration)) is None
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This API authenticates with a client certificate: add one before switching to it.",
        )
    details = openapi_audit_details(plan)
    oauth_changed, oauth_kept = _follow_oauth_option(integration, previous_option, plan.auth_option)
    if oauth_changed:
        details["oauth_settings_updated"] = oauth_changed
    if oauth_kept:
        details["oauth_endpoints_not_moved"] = oauth_kept
        row.warnings = [*(row.warnings or []), _kept_endpoints_warning(integration, oauth_kept)]
    return details


async def attach_spec(
    db: AsyncSession, integration: Integration, body: OpenAPIImportOptions
) -> tuple[IntegrationOpenAPISpec, dict[str, Any]]:
    """Give a spec-less OpenAPI integration its first spec; the row and the audit details.

    The replacement's own validation (:func:`replace_spec`: same credential
    type, client certificate, OAuth endpoints) on a new row, so the stored
    credential is kept as it is. The base URL is the integration's, or the
    spec's absolute server when it has none, as at creation. Raises a 400;
    nothing is committed here.
    """
    row = IntegrationOpenAPISpec(integration_id=integration.id)
    details = await replace_spec(integration, row, body)
    extra = decrypt_extra(integration)
    stored = extra.get("base_url")
    stored_base = stored.strip() if isinstance(stored, str) else ""
    malformed = stored_base and not stored_base.lower().startswith(("http://", "https://"))
    if malformed or (stored and not isinstance(stored, str)):
        # Creation's rule (openapi_extra_config), applied to a legacy value.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="The integration's API base URL must start with http:// or https://: fix it first.",
        )
    if not stored_base:
        server = (row.default_server_url or "").strip()
        if not server.lower().startswith(("http://", "https://")):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    "Enter the API base URL in the integration's configuration: "
                    "the spec does not declare an absolute server URL."
                ),
            )
        extra["base_url"] = server.rstrip("/")
        _save_extra(integration, extra)
        details["base_url_set"] = extra["base_url"]
    # The spec may have filled the token URL a token grant was missing (as
    # creation does): its status follows.
    if integration.auth_type in (AUTH_TYPE_CLIENT_CREDENTIALS, AUTH_TYPE_OAUTH_PASSWORD):
        integration.status = token_grant_status(integration)
    db.add(row)
    details["spec_attached"] = True
    return row, details


# ---------------------------------------------------------------------------
# Client certificate (mutual TLS)
# ---------------------------------------------------------------------------


def _save_extra(integration: Integration, extra: dict[str, Any]) -> None:
    integration.extra_config_enc = encrypt_value(json.dumps(extra))


@router.put("/{integration_id}/openapi/client-certificate", response_model=ClientCertificateInfo)
async def set_client_certificate(
    integration_id: str,
    body: ClientCertificateRequest,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Set (or replace) the client certificate every call presents.

    The certificate must be PEM (a chain is accepted, the client's own first),
    the private key PEM and matching it, the passphrase the one that opens an
    encrypted key. Only the certificate's description ever comes back.
    """
    integration = await _openapi_integration(db, user, integration_id, manage=True)
    try:
        info = parse_client_certificate(body.certificate, body.private_key, body.passphrase)
    except ClientCertificateError as exc:
        raise _bad_request(exc) from None
    extra = decrypt_extra(integration)
    extra[CERTIFICATE_KEY] = body.certificate.strip()
    extra[PRIVATE_KEY_KEY] = body.private_key.strip()
    if body.passphrase:
        extra[PASSPHRASE_KEY] = body.passphrase
    else:
        extra.pop(PASSPHRASE_KEY, None)
    _save_extra(integration, extra)
    await log_audit(
        db,
        user=user,
        action="update",
        entity_type="integration",
        entity_id=integration.id,
        entity_name=integration.name,
        summary=f"Set the client certificate of '{integration.name}'",
        request=request,
        details={
            "client_certificate": {
                "subject": info.subject,
                "issuer": info.issuer,
                "not_after": info.not_after,
                "fingerprint_sha256": info.fingerprint_sha256,
            }
        },
    )
    await db.commit()
    return ClientCertificateInfo(**info.to_dict())


@router.delete(
    "/{integration_id}/openapi/client-certificate", status_code=status.HTTP_204_NO_CONTENT
)
async def delete_client_certificate(
    integration_id: str,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Stop presenting a client certificate.

    Refused while the certificate IS the integration's authentication (the
    spec's mutual TLS option): replace it instead.
    """
    integration = await _openapi_integration(db, user, integration_id, manage=True)
    row = await _spec_or_404(db, integration)
    option = AuthOption.from_dict(row.auth_option)
    if option is not None and option.kind == KIND_MUTUAL_TLS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This integration authenticates with its client certificate: replace it instead.",
        )
    extra = decrypt_extra(integration)
    if any(key in extra for key in CERTIFICATE_KEYS):
        for key in CERTIFICATE_KEYS:
            extra.pop(key, None)
        _save_extra(integration, extra)
        await log_audit(
            db,
            user=user,
            action="update",
            entity_type="integration",
            entity_id=integration.id,
            entity_name=integration.name,
            summary=f"Removed the client certificate of '{integration.name}'",
            request=request,
        )
        await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------------------
# Sign-in flows that do not end on the OAuth callback
# ---------------------------------------------------------------------------


async def _client(integration: Integration) -> tuple[str, str]:
    """The integration's OAuth client; its secret may be a vault reference."""
    client_id = decrypt_value(integration.client_id_enc) if integration.client_id_enc else ""
    client_secret = await reveal_field(integration, "client_secret_enc") or ""
    return client_id, client_secret


def _ssl_context_or_400(integration: Integration) -> Any:
    try:
        return integration_ssl_context(integration)
    except ClientCertificateError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"The client certificate cannot be used: {exc}",
        ) from None


def _grant_error(exc: TokenGrantError) -> HTTPException:
    text = str(exc)
    return HTTPException(
        status_code=status.HTTP_502_BAD_GATEWAY,
        detail=f"{text[:1].upper()}{text[1:]}.",
    )


async def start_device_sign_in(integration: Integration, user: User) -> DeviceAuthorizationInfo:
    """Ask the API's device authorization endpoint for a sign-in code.

    The integration's ``authorize_url`` holds the device authorization
    endpoint for this kind of option.
    """
    client_id, client_secret = await _client(integration)
    if not integration.authorize_url or not integration.token_url:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Set the device authorization URL and the token URL first.",
        )
    try:
        flow = await start_device_authorization(
            user_id=str(user.id),
            integration_id=str(integration.id),
            device_authorization_url=integration.authorize_url,
            token_url=integration.token_url,
            client_id=client_id,
            client_secret=client_secret,
            scopes=integration.scopes,
            ssl_context=_ssl_context_or_400(integration),
        )
    except TokenGrantError as exc:
        raise _grant_error(exc) from None
    return DeviceAuthorizationInfo(**flow.to_dict())


async def _store_user_tokens(
    db: AsyncSession, integration: Integration, user: User, tokens: dict[str, Any]
) -> Any:
    """The tokens a user's sign-in produced, stored exactly as the OAuth callback stores them.

    On an integration whose people each connect their own account, a person's
    sign-in goes to their own credential. Returns the token holder.
    """
    from app.api.oauth_callbacks import mark_authorized, store_authorized_tokens

    refresh = tokens.get("refresh_token")
    access_token = str(tokens["access_token"]).strip()
    refresh_token = refresh if isinstance(refresh, str) and refresh else None
    if signs_in_own_account(user, integration):
        # ``_signing_in_integration`` let this person in for their own account.
        credential = await upsert_user_credential(db, integration, user.id)
        store_tokens(credential, access_token=access_token, refresh_token=refresh_token)
        apply_oauth_token_expiry(credential, tokens)
        record_granted_scopes(credential, tokens, keep_when_absent=False)
        return credential
    await store_authorized_tokens(
        db,
        integration,
        str(user.id),
        access_token=access_token,
        refresh_token=refresh_token,
    )
    apply_oauth_token_expiry(integration, tokens)
    record_granted_scopes(integration, tokens, keep_when_absent=False)
    await mark_authorized(db, integration, str(user.id))
    return integration


async def _clear_refresh_cooldown(holder: Any) -> None:
    """A fresh sign-in ends any refresh back-off of the replaced tokens (after the commit)."""
    from app.services.token_refresh import clear_refresh_cooldown, refresh_cooldown_key

    await clear_refresh_cooldown(refresh_cooldown_key(holder_ref(holder)))


async def _signing_in_integration(db: AsyncSession, user: User, integration_id: str) -> Integration:
    """An OAuth OpenAPI integration *user* may authorize (as ``GET /authorize``)."""
    integration = await _openapi_integration(db, user, integration_id, manage=False)
    if signs_in_own_account(user, integration):
        # Their own account: only where they see the integration as
        # anyone in their place would, the rule the runs apply.
        if not await may_connect_own_account(db, user, integration):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="This integration is not shared with you: there is no account of your own to connect on it.",
            )
    elif not can_authorize_integration(user, integration):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "Only admins can authorize company-managed integrations"
                if integration.is_company_managed
                else "Only this integration's owner can authorize it."
            ),
        )
    if integration.auth_type != "oauth":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This integration does not use an OAuth sign-in.",
        )
    return integration


@router.post("/{integration_id}/openapi/device/poll", response_model=DevicePollResponse)
async def poll_device_sign_in(
    integration_id: str,
    body: DevicePollRequest,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Whether the user entered the device code and approved the sign-in.

    Call it at the returned ``interval``; faster calls answer ``pending``
    without asking the API. ``connected`` means the token is stored.
    """
    integration = await _signing_in_integration(db, user, integration_id)
    client_id, client_secret = await _client(integration)
    result = await poll_device_authorization(
        body.flow_id,
        user_id=str(user.id),
        integration_id=str(integration.id),
        token_url=integration.token_url or "",
        client_id=client_id,
        client_secret=client_secret,
        ssl_context=_ssl_context_or_400(integration),
    )
    if result.status == STATUS_CONNECTED:
        holder = await _store_user_tokens(db, integration, user, result.tokens)
        await log_audit(
            db,
            user=user,
            action="update",
            entity_type="integration",
            entity_id=integration.id,
            entity_name=integration.name,
            summary=f"Authorized '{integration.name}' with a device code",
            request=request,
        )
        await db.commit()
        await _clear_refresh_cooldown(holder)
    return DevicePollResponse(status=result.status, interval=result.interval, message=result.message)


@router.delete(
    "/{integration_id}/openapi/device/{flow_id}", status_code=status.HTTP_204_NO_CONTENT
)
async def cancel_device_sign_in(
    integration_id: str,
    flow_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Abandon a pending device-code sign-in."""
    await _openapi_integration(db, user, integration_id, manage=False)
    await cancel_device_authorization(flow_id, user_id=str(user.id))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/openapi/oauth/implicit", response_model=ImplicitTokenResponse)
async def complete_implicit_sign_in(
    body: ImplicitTokenRequest,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Store the token of an OAuth2 implicit sign-in.

    The provider returns it in the URL fragment of the redirect, which only
    the browser reads; the frontend's ``/oauth/implicit`` page posts it here
    with the ``state``. The state is single-use and binds the token to the
    user and the integration that started the sign-in: an unknown, expired,
    reused or another user's state is refused.
    """
    state = await get_state_data(body.state)
    if (
        not isinstance(state, dict)
        or state.get("flow") != "implicit"
        or state.get("user_id") != str(user.id)
        or not state.get("integration_id")
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This sign-in expired or was not started from this account. Click Authorize again.",
        )
    try:
        uuid.UUID(str(state["integration_id"]))
    except ValueError:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid sign-in state.") from None
    integration = await _signing_in_integration(db, user, str(state["integration_id"]))
    if body.error:
        reason = f"{body.error}: {body.error_description}" if body.error_description else body.error
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"The sign-in was refused ({reason[:300]}).",
        )
    if not (body.access_token or "").strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="The sign-in returned no access token.",
        )
    try:
        check_bearer(body.token_type)
    except TokenGrantError as exc:
        raise _bad_request(exc) from None
    holder = await _store_user_tokens(
        db, integration, user, {"access_token": body.access_token, "expires_in": body.expires_in}
    )
    await log_audit(
        db,
        user=user,
        action="update",
        entity_type="integration",
        entity_id=integration.id,
        entity_name=integration.name,
        summary=f"Authorized '{integration.name}' (OAuth2 implicit)",
        request=request,
    )
    await db.commit()
    await _clear_refresh_cooldown(holder)
    return ImplicitTokenResponse(integration_id=str(integration.id), provider=integration.provider)
