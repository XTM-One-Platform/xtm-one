# SPDX-License-Identifier: Apache-2.0

"""Integration endpoints – full credential management in the UI.

No more environment variables for OAuth client IDs/secrets or bot tokens.
Everything is configured per-integration in the database, encrypted at rest.

Supports:
  - OAuth integrations (Google, Microsoft, GitHub, Slack) with per-integration
    client_id/client_secret configured in the UI
  - API key integrations (any provider)
  - Bot/channel integrations (Slack bot, Teams bot, WhatsApp) with all
    credentials stored in the DB
  - Multiple integrations of the same provider per user
"""

import asyncio
import io
import json
import logging
import re
import time
import uuid
from base64 import b64decode, b64encode
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from datetime import UTC, datetime
from typing import Any

import httpx
from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile, status
from fastapi.responses import StreamingResponse
from sqlalchemy import Text, and_, cast, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

# Import provider bootstraps for their registration side-effects (lifecycle
# hooks, connectivity probes). Nova, the A2A providers and OpenAPI ship no
# agent tools, so they are never reached through PROVIDER_TOOLS — they register
# here, where the integrations router is always loaded.
#
# DO NOT "clean up" these imports. They bind no name anyone reads, so every
# static measure calls them unused and an auto-fix will offer to delete them
# — which silently unregisters every hook and probe. There is no error, just
# a Nova integration whose connect / disconnect hooks never run and a Test
# Connection that verifies nothing.
# ``tests/test_a2a_implicit_exposure.py`` pins them.
import app.integrations.providers.entra_a2a
import app.integrations.providers.google_a2a
import app.integrations.providers.lmstudio
import app.integrations.providers.nova
import app.integrations.providers.oauth_a2a
import app.integrations.providers.openapi
import app.services.lmstudio_llm_provider
import app.services.openrouter_llm_provider  # noqa: F401
from app.api.deps import (
    AfterCommit,
    get_current_user,
    require_agentic_ai_licensed,
    require_chat_enabled,
    run_after_commit,
)
from app.api.v1.binding_access import checked_relay_group_id
from app.api.v1.helpers import (
    ENTITY_LIST_SCOPE_DESCRIPTION,
    EntityListScope,
    apply_group_share_fields,
    apply_owner_transfer,
    apply_user_share_fields,
    can_authorize_integration,
    can_manage,
    get_user_group_ids,
    get_user_integration,
    list_scope_admin_bypass,
    parse_path_id,
    require_admin_for_company_managed,
    require_allowed_base_urls,
    require_allowed_token_url,
    resolve_owner_names,
    user_visible_query,
    wants_access_restriction,
)
from app.api.v1.integration_catalog import (
    PLATFORM_MANAGED_NEEDS_BASE_URL,
    PROVIDER_CATALOG,
    build_dummy_token,
    build_provider_tools,
    integration_owns_oauth_scopes,
    invalid_extra_fields,
    is_certified_provider,
    is_registration_only_provider,
    missing_required_extra_fields,
    missing_required_extra_fields_message,
    newly_missing_required_extra_fields,
    normalize_typed_extra_fields,
    platform_base_url,
    registration_editable_extra_fields,
    supported_auth_types,
    unsupported_auth_type_message,
)
from app.api.v1.integration_openapi import limit_spec_fetches as _limit_spec_fetches
from app.api.v1.integration_openapi import openapi_audit_details as _openapi_audit_details
from app.api.v1.integration_openapi import openapi_extra_config as _openapi_extra_config
from app.api.v1.integration_openapi import prepare_openapi_import as _prepare_openapi_import
from app.api.v1.integration_openapi import start_device_sign_in as _start_device_sign_in
from app.api.v1.integration_openapi import token_grant_status as _token_grant_status
from app.api.v1.integration_testing import test_provider_connectivity
from app.api.v1.schemas.integration import (
    CreateIntegrationRequest,
    CustomIntegrationProviderStatusResponse,
    ExtraFieldOption,
    IntegrationBundleImportResponse,
    IntegrationBundleImportValidationResponse,
    IntegrationResponse,
    IntegrationSignatureSetting,
    IntegrationTypeItem,
    IntegrationUserCredentialItem,
    PerUserProviderReadiness,
    PerUserReadinessResponse,
    ProviderCatalogItem,
    ProviderCatalogVariant,
    ProviderElementType,
    ProviderEvent,
    ProviderTool,
    SetupGuide,
    SetupGuidePermission,
    UpdateIntegrationRequest,
    UserCredentialStatus,
    safe_link_url,
)
from app.api.v1.schemas.vault_references import (
    attach_vault_failures,
    require_secret_variable_references,
    require_vault_writer,
    vault_status,
)
from app.core.config import settings
from app.core.database import get_db
from app.core.egress import egress_client, egress_route
from app.core.security import decrypt_json, decrypt_stored_value, decrypt_value, encrypt_value
from app.core.utils import build_content_disposition, format_exception_message, safe_uuid
from app.integrations.base_url_policy import base_url_keys, probe_transport
from app.integrations.connectivity import has_connectivity_probe
from app.integrations.granted_scopes import missing_scopes, split_scopes
from app.integrations.http import PLATFORM_USER_AGENT
from app.integrations.lifecycle import (
    IntegrationUpdate,
    dispatch_on_connect,
    dispatch_on_disconnect,
    dispatch_on_update,
)
from app.models.agent import Agent
from app.models.custom_integration_definition import CustomIntegrationDefinition
from app.models.integration import Integration
from app.models.oauth_default import OAuthDefault
from app.models.user import User
from app.services.audit import log_audit
from app.services.email import is_email_configured
from app.services.hitl_whitelist import platform_approval_by_provider
from app.services.integration_bundle_manager import (
    INTEGRATION_BUNDLE_MAX_BYTES,
    build_integration_bundle_zip,
    bundle_oauth_endpoints,
    is_valid_semver,
    normalize_custom_provider_slug,
    parse_integration_bundle_zip,
)
from app.services.integration_credentials import (
    SHARE_CONSENT_REFUSED_DETAIL,
    ShareConsentRefusedError,
    apply_oauth_share_consent,
    may_consent_to_share,
    oauth_credentials_usable,
)
from app.services.integration_state import (
    UNREADABLE_ADMIN_CLIENT_ID,
    admin_oauth_app,
    api_key_binding_of,
    forget_key_bound_elsewhere,
    repair_onto_admin_oauth_app,
    replace_oauth_client_id,
    takes_admin_oauth_app,
    uses_admin_oauth_app,
)
from app.services.integration_status import current_reason
from app.services.integration_user_credentials import (
    apply_share_choice,
    connected_user_counts,
    credential_connected,
    delete_user_credentials,
    integration_of,
    is_per_user,
    is_user_credential,
    may_connect_own_account,
    prune_unreachable_user_credentials,
    signs_in_own_account,
    usable_credential_holder,
    user_credentials_by_integration,
    visible_per_user_integration,
)
from app.services.openapi_import import (
    AUTH_TYPE_CLIENT_CREDENTIALS,
    AUTH_TYPE_OAUTH_PASSWORD,
    KIND_OAUTH2_DEVICE,
    KIND_OAUTH2_IMPLICIT,
    OPENAPI_PROVIDER,
    AuthOption,
    effective_tools,
    get_spec,
    provider_resolves_per_user,
    summarize_tools,
)
from app.services.vault import (
    retained_reference_writer_problem,
    reveal_field,
    secret_fields_problem,
    variable_reference_problem,
)
from app.services.vault.config import VaultPrincipal
from app.services.vault.store import principal_from, vault_principal

#: Auth types whose access token the platform requests itself (no sign-in).
_TOKEN_GRANT_AUTH_TYPES = frozenset({AUTH_TYPE_CLIENT_CREDENTIALS, AUTH_TYPE_OAUTH_PASSWORD})

logger = logging.getLogger(__name__)

router = APIRouter()


# ---------------------------------------------------------------------------
# Integration types (categories) – derived from PROVIDER_CATALOG
# ---------------------------------------------------------------------------

# Labels and descriptions for each integration type key.
# Provider lists are computed automatically from PROVIDER_CATALOG entries.
_INTEGRATION_TYPE_META: dict[str, dict[str, str]] = {
    "email": {"label": "Email", "description": "Email services (Outlook, Gmail)"},
    "legal": {"label": "Legal", "description": "Legal AI platforms (Wordsmith)"},
    "calendar": {
        "label": "Calendar",
        "description": "Calendar services (Outlook Calendar, Google Calendar)",
    },
    "messaging": {
        "label": "Messaging & Chat",
        "description": "Team messaging platforms (Teams, Slack, Google Chat)",
    },
    "storage": {
        "label": "Cloud Storage",
        "description": "File storage (OneDrive, SharePoint, Google Drive)",
    },
    "repository": {
        "label": "Code Repository",
        "description": "Source code hosting (GitHub, GitLab)",
    },
    "crm": {"label": "CRM", "description": "Customer relationship management"},
    "documentation": {"label": "Documentation", "description": "Documentation & wikis"},
    "travel": {"label": "Travel & Booking", "description": "Travel management"},
    "compliance": {"label": "Compliance", "description": "Compliance & security"},
    "credentials": {
        "label": "Credentials & Badges",
        "description": "Digital credentials & badge management",
    },
    "hr": {"label": "HR & People", "description": "HR management"},
    "design": {"label": "Design", "description": "Design tools"},
    "threat_intel": {
        "label": "Threat Intelligence",
        "description": "Threat intelligence platforms",
    },
    "ddi": {
        "label": "DDI (DNS/DHCP/IPAM)",
        "description": "DNS, DHCP, and IP address management (DDI)",
    },
    "vulnerability_management": {
        "label": "Vulnerability Management",
        "description": "Vulnerability scanning, detection, and management platforms",
    },
    "news": {
        "label": "News & Research",
        "description": "News aggregation, RSS feeds, and research sources",
    },
    "search": {
        "label": "Web Search",
        "description": "Search the internet for information, articles, and documentation",
    },
    "llm": {
        "label": "AI Models",
        "description": "Query and evaluate external AI models directly as tools",
    },
    # Agent-to-agent interoperability: configuration that lets agents on
    # OTHER platforms reach agents here (or vice versa). Keyed on the
    # protocol name the rest of the feature already uses, and named for the
    # capability rather than for one mechanism, so a second inbound trust
    # provider joins the same tag. Deliberately NOT "federation": in this
    # codebase that word already means identity federation (SSO), and a chip
    # reading "Federation" on an Entra-logo tile would be read as SAML/OIDC.
    "a2a": {
        "label": "Agent to Agent (A2A)",
        "description": (
            "Agent-to-agent interoperability — let agents on other "
            "platforms discover and task agents here"
        ),
    },
}


def _build_integration_types() -> dict[str, dict]:
    """Derive INTEGRATION_TYPES from PROVIDER_CATALOG (single source of truth)."""
    types: dict[str, dict] = {}
    for key, provider in PROVIDER_CATALOG.items():
        if provider.get("category") != "integration":
            continue
        for itype in provider.get("integration_types", []):
            if itype not in types:
                meta = _INTEGRATION_TYPE_META.get(
                    itype, {"label": itype.replace("_", " ").title(), "description": ""}
                )
                types[itype] = {
                    "label": meta["label"],
                    "description": meta["description"],
                    "providers": [],
                }
            types[itype]["providers"].append(key)
    return types


INTEGRATION_TYPES = _build_integration_types()


_SEMVER_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)(?:[-+].*)?$")


_PrereleaseKey = tuple[tuple[int, int, str], ...]


def _prerelease_key(suffix: str) -> _PrereleaseKey:
    """Comparable key implementing SemVer pre-release precedence.

    Per the spec: identifiers are dot-separated; numeric identifiers compare
    numerically and always have LOWER precedence than alphanumeric ones; when
    all preceding identifiers are equal, the shorter identifier list sorts
    lower (1.0.0-alpha < 1.0.0-alpha.1) — Python tuple comparison gives the
    last rule for free.
    """
    if not suffix:
        return ()
    return tuple(
        (0, int(ident), "") if ident.isdigit() else (1, 0, ident)
        for ident in suffix.lstrip("-").split(".")
    )


def _semver_key(version: str) -> tuple[int, int, int, int, _PrereleaseKey]:
    v = (version or "").strip()
    m = _SEMVER_RE.match(v)
    if not m:
        # Keep non-semver values comparable without crashing; validator should
        # already reject malformed versions.
        return (0, 0, 0, 0, ((1, 0, v),))
    # Per semver, a pre-release (1.0.0-alpha) sorts BELOW the plain release
    # (1.0.0), and build metadata (1.0.0+build1) MUST NOT affect precedence —
    # strip it before comparing. Pre-release identifiers follow full SemVer
    # precedence (numeric identifiers numeric, numeric < alphanumeric), so
    # 1.0.0-alpha.10 correctly ranks above 1.0.0-alpha.2.
    suffix = v[m.end(3) :].split("+", 1)[0]
    is_release = 0 if suffix.startswith("-") else 1
    return (
        int(m.group(1)),
        int(m.group(2)),
        int(m.group(3)),
        is_release,
        _prerelease_key(suffix),
    )


#: Built-in catalog fields that hold a credential but whose NAMES defeat the
#: heuristic below. ``service_account_json`` carries a Google service account's
#: PRIVATE KEY and contains none of the words the heuristic looks for - not
#: "secret", not "token", not even "key". Custom bundles can declare a secret
#: in their schema; a built-in provider has nowhere to declare one, so it is
#: named here.
_KNOWN_SECRET_SETTING_NAMES = frozenset({"service_account_json"})


def _is_sensitive_setting_name(key: str) -> bool:
    lowered = key.lower()
    if lowered in _KNOWN_SECRET_SETTING_NAMES:
        return True
    # Identifier fields (access_key_id, api_key_id, ...) and endpoint fields
    # (token_url, card_urls, ...) are not secrets: masking them would break
    # config verification and hide which partner a registration points at.
    # Custom bundles can still force-mask any key via a schema-declared
    # `type` of "secret".
    if lowered.endswith(("_id", "_url", "_urls")):
        return False
    # A connection string (``event_hub_connection_string``) embeds its shared
    # access key: ``Endpoint=...;SharedAccessKey=...``.
    return any(token in lowered for token in ("secret", "token", "key", "password", "connection_string"))


# Placeholder returned instead of secret extra_config values in API responses.
# The update endpoint treats an incoming value equal to this mask as
# "keep the currently stored value", so the redacted read -> edit -> save
# roundtrip never destroys a stored secret.
EXTRA_CONFIG_MASK = "********"

# Deadline of a custom bundle's ``test_connection`` probe, end to end: httpx's
# timeout bounds each phase (connect, every read), not the whole exchange.
_CUSTOM_PROBE_TIMEOUT_S = 15.0


def _secret_keys_from_schema(settings_schema: list | None) -> frozenset[str]:
    """Extract schema-declared secret setting keys (``type: "secret"``)."""
    return frozenset(
        str(row.get("key"))
        for row in settings_schema or []
        if isinstance(row, dict)
        and str(row.get("type") or "").strip().lower() == "secret"
        and row.get("key")
    )


async def _read_bundle_upload(bundle: UploadFile) -> bytes:
    """Read an uploaded bundle bounded to the size limit (+1 byte).

    Rejects an oversized upload as soon as the limit is crossed instead of
    buffering the whole file in memory first.
    """
    raw = await bundle.read(INTEGRATION_BUNDLE_MAX_BYTES + 1)
    if len(raw) > INTEGRATION_BUNDLE_MAX_BYTES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "Bundle exceeds maximum size of "
                f"{INTEGRATION_BUNDLE_MAX_BYTES // (1024 * 1024)} MB."
            ),
        )
    return raw


async def _custom_secret_config_keys(
    db: AsyncSession, provider: str
) -> frozenset[str]:
    """Schema-declared secret setting keys for a custom provider.

    Custom bundle schemas can declare secrets under non-obvious keys (e.g.
    ``auth``) that the ``_is_sensitive_setting_name`` heuristic misses, so
    responses must also redact based on the stored settings schema.
    """
    if not provider.startswith("custom:"):
        return frozenset()
    result = await db.execute(
        select(CustomIntegrationDefinition).where(
            CustomIntegrationDefinition.provider_key == provider
        )
    )
    row = result.scalar_one_or_none()
    return _secret_keys_from_schema(row.settings_schema) if row else frozenset()


async def _custom_base_url_keys(db: AsyncSession, provider: str) -> list[str]:
    """The ``extra_config`` keys a custom provider reads a base URL from."""
    result = await db.execute(
        select(CustomIntegrationDefinition).where(
            CustomIntegrationDefinition.provider_key == provider
        )
    )
    row = result.scalar_one_or_none()
    return base_url_keys(row.metadata_json if row is not None else None)


async def _custom_secret_config_keys_bulk(
    db: AsyncSession, providers: set[str]
) -> dict[str, frozenset[str]]:
    """Bulk variant of :func:`_custom_secret_config_keys` (one query total)."""
    custom = {p for p in providers if p.startswith("custom:")}
    if not custom:
        return {}
    result = await db.execute(
        select(CustomIntegrationDefinition).where(
            CustomIntegrationDefinition.provider_key.in_(custom)
        )
    )
    return {
        row.provider_key: _secret_keys_from_schema(row.settings_schema)
        for row in result.scalars().all()
    }


def _guess_mime_from_logo_path(path: str | None) -> str:
    lowered = (path or "").lower()
    if lowered.endswith(".svg"):
        return "image/svg+xml"
    if lowered.endswith(".jpg") or lowered.endswith(".jpeg"):
        return "image/jpeg"
    if lowered.endswith(".webp"):
        return "image/webp"
    if lowered.endswith(".gif"):
        return "image/gif"
    return "image/png"


def _encode_logo_data_url(raw: bytes | None, path: str | None) -> str | None:
    if not raw:
        return None
    mime = _guess_mime_from_logo_path(path)
    return f"data:{mime};base64,{b64encode(raw).decode('ascii')}"


def _decode_logo_data_url(data_url: str | None) -> bytes | None:
    if not data_url:
        return None
    if not data_url.startswith("data:"):
        return None
    try:
        _, payload = data_url.split(",", 1)
    except ValueError:
        return None
    try:
        return b64decode(payload)
    except Exception:
        return None


def _build_bundle_integration_instance_payload(integration: Integration) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "name": integration.name,
        "auth_type": integration.auth_type,
    }

    if integration.description:
        payload["description"] = integration.description
    if integration.tags:
        payload["tags"] = list(integration.tags)
    if integration.is_company_managed:
        payload["is_company_managed"] = True
    if integration.authorize_url:
        payload["authorize_url"] = integration.authorize_url
    if integration.token_url:
        payload["token_url"] = integration.token_url
    if integration.scopes:
        payload["scopes"] = integration.scopes
    if integration.client_id_enc:
        payload["client_id"] = decrypt_value(integration.client_id_enc)
    # As stored: a vault reference stays a reference, never its value.
    if integration.client_secret_enc:
        payload["client_secret"] = decrypt_stored_value(integration.client_secret_enc)
    if integration.api_key_enc:
        payload["api_key"] = decrypt_stored_value(integration.api_key_enc)

    extra = decrypt_json(integration.extra_config_enc)
    if isinstance(extra, dict) and extra:
        payload["extra_config"] = extra

    return payload


def _settings_schema_to_extra_fields(
    settings_schema: list[dict] | None,
) -> tuple[dict[str, str], dict[str, list[dict[str, str]]]]:
    extra_fields: dict[str, str] = {}
    extra_field_options: dict[str, list[dict[str, str]]] = {}
    for row in settings_schema or []:
        key = str(row.get("key") or "").strip()
        label = str(row.get("label") or "").strip()
        if not key or not label:
            continue
        extra_fields[key] = label
        options = row.get("options")
        if isinstance(options, list) and options:
            parsed: list[dict[str, str]] = []
            for opt in options:
                if not isinstance(opt, dict):
                    continue
                value = str(opt.get("value") or "").strip()
                opt_label = str(opt.get("label") or "").strip()
                if value and opt_label:
                    parsed.append({"value": value, "label": opt_label})
            if parsed:
                extra_field_options[key] = parsed
    return extra_fields, extra_field_options


def _settings_schema_to_catalog_fields(
    auth_type: str,
    settings_schema: list[dict] | None,
) -> list[str]:
    fields: list[str] = []
    if auth_type == "oauth":
        fields.extend(["client_id", "client_secret"])
    elif auth_type in {"api_key", "bot_token"}:
        fields.append("api_key")

    # Some bundles may define credentials in settings only; keep compatibility.
    for row in settings_schema or []:
        if not isinstance(row, dict):
            continue
        key = str(row.get("key") or "").strip().lower()
        row_type = str(row.get("type") or "").strip().lower()
        if row_type != "secret":
            continue
        if key in {"api_key", "token", "bot_token"} and "api_key" not in fields:
            fields.append("api_key")
    return fields


def _custom_setup_guide_from_metadata(
    metadata_json: dict | None,
    *,
    provider_name: str,
) -> dict:
    fallback = {
        "title": f"Configure {provider_name}",
        "steps": ["Fill the settings from your imported integration bundle."],
        "permissions": [],
        "notes": "This provider was imported from a custom bundle.",
    }
    if not isinstance(metadata_json, dict):
        return fallback

    raw = metadata_json.get("setup_guide")
    if not isinstance(raw, dict):
        return fallback

    title_raw = raw.get("title")
    title = title_raw.strip() if isinstance(title_raw, str) and title_raw.strip() else fallback["title"]

    # Rendered as the guide's console link: a bundle's javascript: or
    # userinfo-disguised URL is dropped, not linked.
    url_raw = raw.get("url")
    url = (
        safe_link_url(url_raw.strip(), schemes=("http", "https"))
        if isinstance(url_raw, str)
        else None
    )

    steps_raw = raw.get("steps")
    steps: list[str] = []
    if isinstance(steps_raw, list):
        for step in steps_raw:
            if isinstance(step, str) and step.strip():
                steps.append(step.strip())
    if not steps:
        steps = fallback["steps"]

    permissions_raw = raw.get("permissions")
    permissions: list[dict[str, str]] = []
    if isinstance(permissions_raw, list):
        for permission in permissions_raw:
            if not isinstance(permission, dict):
                continue
            scope_raw = permission.get("scope")
            description_raw = permission.get("description")
            if not isinstance(scope_raw, str) or not scope_raw.strip():
                continue
            if not isinstance(description_raw, str) or not description_raw.strip():
                continue
            permissions.append(
                {
                    "scope": scope_raw.strip(),
                    "description": description_raw.strip(),
                }
            )

    notes_raw = raw.get("notes")
    notes = notes_raw.strip() if isinstance(notes_raw, str) and notes_raw.strip() else fallback["notes"]

    apis_to_enable_raw = raw.get("apis_to_enable")
    apis_to_enable: list[str] | None = None
    if isinstance(apis_to_enable_raw, list):
        apis = [item.strip() for item in apis_to_enable_raw if isinstance(item, str) and item.strip()]
        if apis:
            apis_to_enable = apis

    guide: dict[str, Any] = {
        "title": title,
        "steps": steps,
        "permissions": permissions,
        "notes": notes,
    }
    if url is not None:
        guide["url"] = url
    if apis_to_enable is not None:
        guide["apis_to_enable"] = apis_to_enable
    return guide


def _infer_tool_type_from_name(name: str) -> str:
    lowered = name.lower()
    write_prefixes = (
        "create_",
        "update_",
        "delete_",
        "set_",
        "post_",
        "put_",
        "patch_",
        "send_",
    )
    return "write" if lowered.startswith(write_prefixes) else "read"


def _custom_tools_from_metadata(metadata_json: dict | None) -> list[dict[str, Any]]:
    if not isinstance(metadata_json, dict):
        return []

    raw_tools = metadata_json.get("tools")
    if not isinstance(raw_tools, list):
        raw_tools = metadata_json.get("available_tools")
    if not isinstance(raw_tools, list):
        return []

    tools: list[dict[str, Any]] = []
    for idx, row in enumerate(raw_tools):
        if not isinstance(row, dict):
            continue
        name_raw = row.get("name")
        description_raw = row.get("description")
        if not isinstance(name_raw, str) or not name_raw.strip():
            continue
        if not isinstance(description_raw, str) or not description_raw.strip():
            continue

        name = name_raw.strip()
        description = description_raw.strip()
        tool_type_raw = row.get("tool_type")
        if isinstance(tool_type_raw, str) and tool_type_raw.strip().lower() in {
            "read",
            "write",
        }:
            tool_type = tool_type_raw.strip().lower()
        else:
            tool_type = _infer_tool_type_from_name(name)

        input_schema = row.get("input_schema")
        if not isinstance(input_schema, dict):
            input_schema = {"type": "object", "properties": {}}

        tools.append(
            {
                "name": name,
                "description": description,
                "tool_type": tool_type,
                "input_schema": input_schema,
                "order": idx,
            }
        )

    return tools


async def _load_custom_catalog_rows(db: AsyncSession) -> list[CustomIntegrationDefinition]:
    result = await db.execute(select(CustomIntegrationDefinition))
    return result.scalars().all()


RELAY_REQUIRED_DETAIL = (
    "This integration is reached only through a relay group: choose the group whose relay "
    "can reach the service."
)


def _require_relay_when_needed(catalog: dict, relay_group_id) -> None:
    """422 when a provider reached only through a relay (``requires_relay``) is given none."""
    if catalog.get("requires_relay") and relay_group_id is None:
        raise HTTPException(status_code=422, detail=RELAY_REQUIRED_DETAIL)


async def _get_provider_catalog_entry(
    db: AsyncSession, provider: str
) -> dict | None:
    if provider in PROVIDER_CATALOG:
        return PROVIDER_CATALOG[provider]

    row_result = await db.execute(
        select(CustomIntegrationDefinition).where(
            CustomIntegrationDefinition.provider_key == provider,
        )
    )
    row = row_result.scalar_one_or_none()
    if not row:
        return None

    return _custom_row_to_catalog_entry(row)


def _custom_row_to_catalog_entry(row: CustomIntegrationDefinition) -> dict:
    settings_schema = row.settings_schema or []
    extra_fields, extra_field_options = _settings_schema_to_extra_fields(settings_schema)
    custom_tools = _custom_tools_from_metadata(row.metadata_json)
    available_events: list[dict] = []
    if row.supports_polling:
        available_events.append(
            {
                "event": "custom_polling_event",
                "label": "Polling event",
                "description": "Custom provider polling trigger",
                "mechanism": "polling",
            }
        )
    if row.supports_webhook:
        available_events.append(
            {
                "event": "custom_webhook_event",
                "label": "Webhook event",
                "description": "Custom provider webhook trigger",
                "mechanism": "webhook",
            }
        )

    return {
        "name": row.name,
        "description": row.description,
        "logo": row.logo_data_url,
        "category": "integration",
        "integration_category": "business_operations",
        "integration_types": ["automation"],
        "auth_type": row.auth_type,
        "fields": _settings_schema_to_catalog_fields(row.auth_type, settings_schema),
        "extra_fields": extra_fields,
        "extra_field_options": extra_field_options,
        "available_events": available_events,
        "available_tools": [
            {
                "name": tool["name"],
                "description": tool["description"],
                "tool_type": tool["tool_type"],
            }
            for tool in custom_tools
        ],
        "setup_guide": _custom_setup_guide_from_metadata(
            row.metadata_json,
            provider_name=row.name,
        ),
        "is_custom": True,
        "settings_schema": settings_schema,
        # An OAuth bundle's endpoints (``oauth`` block), which an integration
        # created from the catalog takes as its authorize / token URLs and
        # scopes, like a native provider's catalog entry.
        **_custom_oauth_endpoints(row),
    }


def _custom_oauth_endpoints(row: CustomIntegrationDefinition) -> dict:
    if row.auth_type != "oauth":
        return {}
    oauth = bundle_oauth_endpoints(row.metadata_json)
    if oauth is None:
        return {}
    return {
        key: oauth[key]
        for key in ("authorize_url", "token_url", "scopes")
        if oauth.get(key)
    }


# ---------------------------------------------------------------------------
# Provider catalog endpoint
# ---------------------------------------------------------------------------


@router.get("/catalog", response_model=list[ProviderCatalogItem])
async def list_provider_catalog(
    category: str | None = None,
    include_disabled: bool = False,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List available provider types. Filter by category: 'integration' or 'channel'.

    Admin-disabled providers are omitted for non-admins unless
    ``include_disabled`` is set — the marketplace asks for them so it can show
    the tile greyed out with a "disabled by your administrator" banner instead
    of silently hiding a provider the user may be looking for. Creating one
    stays a 403 either way.
    """
    # Fetch which providers have admin OAuth defaults (and whether extra config is set)
    defaults_result = await db.execute(
        select(OAuthDefault.provider, OAuthDefault.extra_config_enc)
    )
    providers_with_defaults: dict[str, bool] = {}
    for row in defaults_result.all():
        providers_with_defaults[row[0]] = bool(row[1])

    # Fetch disabled providers from platform settings
    from app.api.v1.platform_settings_helpers import (
        get_platform_settings as _load_ps,
    )

    ps = await _load_ps(db)
    disabled_providers: set[str] = set(ps.disabled_integration_providers or [])

    is_admin = user.role == "admin"

    # Resolve {base_url} and {APP_NAME} placeholders in setup guide steps.
    # Resolve through the branding service so a customized brand set in
    # Settings → Branding flows through to integration setup instructions
    # instead of always rendering the deployment-mode default.
    actual_base_url = settings.base_url.rstrip("/")
    from app.services.branding import get_branding

    app_name = (await get_branding(db)).name

    # Build tool metadata from provider tool builders
    provider_tools = build_provider_tools()

    custom_rows = await _load_custom_catalog_rows(db)
    custom_disabled: set[str] = {
        row.provider_key for row in custom_rows if not bool(row.enabled)
    }
    merged_catalog: dict[str, dict] = dict(PROVIDER_CATALOG)
    for row in custom_rows:
        merged_catalog[row.provider_key] = _custom_row_to_catalog_entry(row)

    items = []
    def _resolve_guide(guide_data) -> SetupGuide | None:
        """Project a catalog guide, with ``{base_url}`` / ``{APP_NAME}`` filled in."""
        if not guide_data:
            return None
        resolved_steps = [
            step.replace("{base_url}", actual_base_url).replace(
                "{APP_NAME}", app_name
            )
            for step in guide_data.get("steps", [])
        ]
        resolved_notes = guide_data.get("notes")
        if resolved_notes:
            resolved_notes = resolved_notes.replace(
                "{base_url}", actual_base_url
            ).replace("{APP_NAME}", app_name)
        return SetupGuide(
            title=guide_data["title"],
            url=guide_data.get("url"),
            steps=resolved_steps,
            permissions=[
                SetupGuidePermission(**p) for p in guide_data.get("permissions", [])
            ],
            apis_to_enable=guide_data.get("apis_to_enable"),
            notes=resolved_notes,
            open_source=guide_data.get("open_source"),
        )

    for key, val in merged_catalog.items():
        if category and val.get("category") != category:
            continue

        # A provider that sends through the deployment's SMTP is offered only
        # while SMTP is configured.
        if val.get("requires_platform_smtp") and not is_email_configured():
            continue
        # Only an administrator creates a company-managed-only provider.
        if val.get("company_managed_only") and not is_admin:
            continue
        provider_disabled = (key in disabled_providers) or (key in custom_disabled)

        # Non-admin users: skip disabled providers unless explicitly requested
        if provider_disabled and not is_admin and not include_disabled:
            continue

        guide = _resolve_guide(val.get("setup_guide"))
        auth_guides = {
            mode: resolved
            for mode, mode_guide in (val.get("auth_setup_guides") or {}).items()
            if (resolved := _resolve_guide(mode_guide)) is not None
        } or None
        # The facets one selector value changes; guides resolved like the base.
        variants = {
            value: ProviderCatalogVariant(
                extra_fields=variant.get("extra_fields"),
                setup_guide=_resolve_guide(variant.get("setup_guide")),
            )
            for value, variant in (val.get("variants") or {}).items()
        } or None
        # Build extra_field_options (e.g. Drata region dropdown)
        extra_field_options_raw = val.get("extra_field_options")
        extra_field_options = None
        if extra_field_options_raw:
            extra_field_options = {
                k: [ExtraFieldOption(**opt) for opt in opts]
                for k, opts in extra_field_options_raw.items()
            }

        available_tools = provider_tools.get(key)
        if available_tools is None:
            available_tools = [
                ProviderTool(**tool) for tool in val.get("available_tools", [])
            ]

        items.append(
            ProviderCatalogItem(
                provider=key,
                name=val["name"],
                category=val.get("category", "integration"),
                integration_category=val.get("integration_category", "business_operations"),
                auth_type=val["auth_type"],
                supported_auth_types=val.get("supported_auth_types"),
                fields=val["fields"],
                # A2A only. Absent from this list, the catalog endpoint drops
                # them and the creation form cannot offer a direction — which
                # makes every integration an operator creates inbound and the
                # whole outbound feature unreachable from the product.
                a2a_directions=val.get("a2a_directions"),
                requires_relay=bool(val.get("requires_relay")),
                company_managed_only=bool(val.get("company_managed_only")),
                variant_selector=val.get("variant_selector"),
                variants=variants,
                extra_fields=val.get("extra_fields"),
                extra_field_options=extra_field_options,
                extra_field_visible_when=val.get("extra_field_visible_when"),
                extra_field_inputs=val.get("extra_field_inputs"),
                description=val.get("description"),
                logo=val.get("logo"),
                integration_types=val.get("integration_types", []),
                available_events=[
                    ProviderEvent(**e) for e in val.get("available_events", [])
                ],
                available_tools=available_tools,
                # A2A only, and dropped the same way ``a2a_directions`` was
                # if omitted: the two direction panels render empty and the
                # tool an inbound binding confers is invisible.
                a2a_inbound_agent_tools=[
                    ProviderTool(**t) for t in val.get("a2a_inbound_agent_tools", [])
                ]
                or None,
                a2a_outbound_exposed=[
                    ProviderTool(**t) for t in val.get("a2a_outbound_exposed", [])
                ]
                or None,
                element_types=[
                    ProviderElementType(**et) for et in val.get("element_types", [])
                ],
                webhook_path=val.get("webhook_path"),
                needs_shared_webhook=val.get("needs_shared_webhook", False),
                supports_assignment_webhooks=val.get(
                    "supports_assignment_webhooks", False
                ),
                supports_webhook_management=val.get(
                    "supports_webhook_management", False
                ),
                setup_guide=guide,
                auth_setup_guides=auth_guides,
                has_oauth_defaults=key in providers_with_defaults,
                has_oauth_extra_config=providers_with_defaults.get(key, False),
                is_custom=bool(val.get("is_custom", False)),
                # A customer's imported bundle is not ours to qualify.
                is_certified=not val.get("is_custom") and is_certified_provider(key),
                settings_schema=val.get("settings_schema"),
                spec_import=val.get("spec_import"),
                is_disabled=provider_disabled,
                is_registration_only=is_registration_only_provider(key),
                registration_editable_extra_fields=sorted(
                    registration_editable_extra_fields(key)
                )
                or None,
            )
        )
    return items


@router.get("/types", response_model=list[IntegrationTypeItem])
async def list_integration_types():
    """List integration categories (email, calendar, messaging, etc.)."""
    return [
        IntegrationTypeItem(key=key, **val) for key, val in INTEGRATION_TYPES.items()
    ]


@router.get("/events")
async def list_available_events():
    """List all available integration events grouped by provider.

    Returns a mapping of provider → events that can be used as
    ``integration_event`` assignment triggers.
    """
    result: dict[str, list] = {}
    for key, spec in PROVIDER_CATALOG.items():
        events = spec.get("available_events", [])
        if events:
            result[key] = events
    return result


@router.get("/per-user-readiness", response_model=PerUserReadinessResponse)
async def get_per_user_readiness(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Report, per provider, whether "resolved per user at runtime" will work.

    Per-user (template) bindings resolve to the acting user's OWN integration,
    falling back to a genuine company-wide one — never to a colleague's personal
    account. That makes two states worth distinguishing in the UI, which
    a flat list of integrations cannot express:

      * a company-wide fallback exists → users who have not connected their own
        account still get tools, from a company-owned instance;
      * none exists → every user must connect their own account first, and until
        they do the agent silently has no tools for that provider.

    ``de_facto_shared_owners`` additionally flags the shape behind the original
    incident: a *personal* instance group-shared to the team, with no
    company-managed instance for that connector. It looks like the shared one in
    the integrations list but is not eligible for per-user resolution.
    """
    # Lazy import: ``integration_tools`` pulls in every provider tool builder.
    from app.services.integration_tools import is_company_wide_integration

    user_group_ids = await get_user_group_ids(db, user.id)
    result = await db.execute(user_visible_query(Integration, user.id, user_group_ids))
    # A provider whose every integration is a different API (OpenAPI) is never
    # resolved per user, so it has no readiness to report.
    visible = [
        r
        for r in result.scalars().all()
        if r.status == "connected" and provider_resolves_per_user(r.provider)
    ]
    owner_names = await resolve_owner_names(db, visible)

    providers: dict[str, PerUserProviderReadiness] = {}
    de_facto: dict[str, list[str]] = {}
    for integ in visible:
        entry = providers.setdefault(
            integ.provider,
            PerUserProviderReadiness(
                provider=integ.provider,
                has_company_wide_fallback=False,
                company_wide_instance_count=0,
                user_has_own_instance=False,
                resolvable_for_user=False,
            ),
        )
        if is_company_wide_integration(integ):
            # An unacknowledged shared OAuth row lends its token to nobody but
            # its owner, so it is no fallback for anyone else.
            if oauth_credentials_usable(integ, None):
                entry.has_company_wide_fallback = True
                entry.company_wide_instance_count += 1
        elif integ.user_id == user.id:
            entry.user_has_own_instance = True
        elif integ.shared_group_ids:
            de_facto.setdefault(integ.provider, []).append(
                owner_names.get(str(integ.user_id)) or "another user"
            )

    for provider, entry in providers.items():
        entry.resolvable_for_user = (
            entry.user_has_own_instance or entry.has_company_wide_fallback
        )
        # Only a real risk while the connector has no company-wide instance —
        # otherwise the shared personal row is just an extra personal instance.
        if not entry.has_company_wide_fallback:
            entry.de_facto_shared_owners = sorted(set(de_facto.get(provider, [])))

    return PerUserReadinessResponse(
        providers=sorted(providers.values(), key=lambda p: p.provider)
    )


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------


@router.get("", response_model=list[IntegrationResponse])
async def list_integrations(
    scope: EntityListScope = Query("visible", description=ENTITY_LIST_SCOPE_DESCRIPTION),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    # Own + group-shared + company-managed integrations; every integration for
    # an admin reading ``scope=all`` (the integrations a detail page links to).
    user_group_ids = await get_user_group_ids(db, user.id)
    result = await db.execute(
        user_visible_query(
            Integration,
            user.id,
            user_group_ids,
            is_admin=list_scope_admin_bypass(scope, user),
        )
    )
    integrations = result.scalars().all()
    names = await resolve_owner_names(db, integrations)
    secret_keys_by_provider = await _custom_secret_config_keys_bulk(
        db, {r.provider for r in integrations}
    )
    platform_approval = await platform_approval_by_provider(db)
    admin_client_ids = await _admin_oauth_client_ids(db, integrations)
    own_credentials = await _own_credential_fields(db, user, integrations, user_group_ids)
    viewer = principal_from(user, user_group_ids)
    directions = await _a2a_directions(db, integrations)
    responses = [
        _to_response(
            r,
            can_manage=can_manage(user, r, user_group_ids),
            can_authorize=can_authorize_integration(user, r),
            owner_name=names.get(str(r.user_id)),
            secret_config_keys=secret_keys_by_provider.get(r.provider, frozenset()),
            platform_approval=platform_approval,
            admin_client_ids=admin_client_ids,
            vault_viewer=viewer,
            a2a_direction=directions.get(str(r.id)),
            **own_credentials.get(str(r.id), {}),
        )
        for r in integrations
    ]
    await attach_vault_failures(responses, Integration.__tablename__)
    return responses


# ---------------------------------------------------------------------------
# Admin: Integration bundle validation
# ---------------------------------------------------------------------------


@router.post(
    "/admin/import/validate",
    response_model=IntegrationBundleImportValidationResponse,
)
async def validate_integration_bundle(
    request: Request,
    bundle: UploadFile = File(...),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Validate an integration bundle ZIP and return metadata preview.

    This endpoint performs a safe parse only (no persistence yet) and is
    restricted to platform admins.
    """
    if user.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only admins can validate integration bundles.",
        )

    raw = await _read_bundle_upload(bundle)
    try:
        parsed = parse_integration_bundle_zip(raw)
    except ValueError as err:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(err)) from err

    await log_audit(
        db,
        user=user,
        action="validate_bundle",
        entity_type="integration_bundle",
        entity_id=None,
        entity_name=parsed.metadata.name,
        summary=f"Validated integration bundle '{parsed.metadata.name}'",
        request=request,
        details={
            "provider": parsed.metadata.provider,
            "version": parsed.metadata.version,
            "auth_type": parsed.metadata.auth_type,
            "supports_webhook": parsed.metadata.supports_webhook,
            "supports_polling": parsed.metadata.supports_polling,
        },
    )
    # Commit before answering: the get_db teardown only commits after the response is sent.
    await db.commit()

    return IntegrationBundleImportValidationResponse(
        format=parsed.metadata.format,
        version=parsed.metadata.version,
        name=parsed.metadata.name,
        slug=parsed.metadata.slug,
        normalized_provider=normalize_custom_provider_slug(parsed.metadata.provider),
        description=parsed.metadata.description,
        author=parsed.metadata.author,
        auth_type=parsed.metadata.auth_type,
        icon=parsed.metadata.icon,
        settings=[
            {
                "key": s.key,
                "label": s.label,
                "type": s.type,
                "required": s.required,
                "placeholder": s.placeholder,
                "options": s.options,
            }
            for s in parsed.metadata.settings
        ],
        supports_polling=parsed.metadata.supports_polling,
        supports_webhook=parsed.metadata.supports_webhook,
        test_connection=(
            {
                "mode": parsed.metadata.test_connection.mode,
                "path": parsed.metadata.test_connection.path,
                "method": parsed.metadata.test_connection.method,
            }
            if parsed.metadata.test_connection
            else None
        ),
        has_integration_instance=parsed.integration_instance is not None,
        integration_instance_name=(
            parsed.integration_instance.name if parsed.integration_instance else None
        ),
        checksum_sha256=parsed.checksum_sha256,
    )


@router.post(
    "/admin/import",
    response_model=IntegrationBundleImportResponse,
)
async def import_integration_bundle(
    request: Request,
    bundle: UploadFile = File(...),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Import a custom integration provider from a ZIP bundle.

    Re-import with a higher semantic version upgrades the existing provider
    in place. Native provider slugs are never overridable.
    """
    if user.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only admins can import integration bundles.",
        )

    raw = await _read_bundle_upload(bundle)
    try:
        parsed = parse_integration_bundle_zip(raw)
    except ValueError as err:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(err)) from err

    normalized_provider = normalize_custom_provider_slug(parsed.metadata.provider)
    if parsed.metadata.provider in PROVIDER_CATALOG or normalized_provider in PROVIDER_CATALOG:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "Bundle provider conflicts with a native integration provider. "
                "Use a unique custom slug."
            ),
        )

    existing_result = await db.execute(
        select(CustomIntegrationDefinition).where(
            CustomIntegrationDefinition.provider_key == normalized_provider
        )
    )
    existing = existing_result.scalar_one_or_none()
    upgraded = False
    if existing is not None:
        if _semver_key(parsed.metadata.version) <= _semver_key(existing.version):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"Provider '{normalized_provider}' already installed at version "
                    f"{existing.version}. Import a higher version to upgrade."
                ),
            )
        upgraded = True

    # Before anything is written, so a refused token URL leaves the provider
    # untouched. The instance's provider is always a ``custom:`` slug, never an
    # A2A one, so the token endpoint policy always applies.
    if parsed.integration_instance is not None:
        await require_allowed_token_url(parsed.integration_instance.token_url)
        # An imported instance is never routed through a relay group.
        await require_allowed_base_urls(
            parsed.integration_instance.extra_config,
            base_url_keys(parsed.metadata_raw),
            routed=False,
        )

    logo_data_url = _encode_logo_data_url(parsed.icon_bytes, parsed.metadata.icon)
    # Never persist the embedded integration_instance in the provider
    # definition: it can carry plaintext credentials (api_key, client_secret,
    # extra_config secrets). The instance is materialized as an encrypted
    # Integration row below; keeping it in metadata_json would store secrets
    # unencrypted AND silently re-embed them in every later export of the
    # custom provider (which does not require ?integration_id= nor show the
    # credential warning).
    stored_metadata = {
        k: v for k, v in parsed.metadata_raw.items() if k != "integration_instance"
    }
    settings_schema = [
        {
            "key": s.key,
            "label": s.label,
            "type": s.type,
            "required": s.required,
            "placeholder": s.placeholder,
            "options": s.options,
        }
        for s in parsed.metadata.settings
    ]

    if existing is None:
        row = CustomIntegrationDefinition(
            provider_key=normalized_provider,
            slug=parsed.metadata.slug,
            name=parsed.metadata.name,
            description=parsed.metadata.description,
            author=parsed.metadata.author,
            version=parsed.metadata.version,
            auth_type=parsed.metadata.auth_type,
            metadata_json=stored_metadata,
            settings_schema=settings_schema,
            logo_data_url=logo_data_url,
            logo_path=parsed.metadata.icon,
            checksum_sha256=parsed.checksum_sha256,
            supports_polling=parsed.metadata.supports_polling,
            supports_webhook=parsed.metadata.supports_webhook,
            enabled=True,
            created_by=user.id,
        )
        db.add(row)
    else:
        existing.slug = parsed.metadata.slug
        existing.name = parsed.metadata.name
        existing.description = parsed.metadata.description
        existing.author = parsed.metadata.author
        existing.version = parsed.metadata.version
        existing.auth_type = parsed.metadata.auth_type
        existing.metadata_json = stored_metadata
        existing.settings_schema = settings_schema
        existing.logo_data_url = logo_data_url
        existing.logo_path = parsed.metadata.icon
        existing.checksum_sha256 = parsed.checksum_sha256
        existing.supports_polling = parsed.metadata.supports_polling
        existing.supports_webhook = parsed.metadata.supports_webhook
        existing.enabled = True
        existing.updated_at = datetime.now(UTC)

    created_integration: Integration | None = None
    if parsed.integration_instance is not None:
        instance = parsed.integration_instance
        if instance.auth_type != parsed.metadata.auth_type:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    "integration_instance.auth_type must match the provider auth_type "
                    "defined in metadata.json."
                ),
            )
        if problem := secret_fields_problem(
            {"api_key": instance.api_key, "client_secret": instance.client_secret},
            {"client_id": instance.client_id, "extra_config": instance.extra_config},
        ):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"integration_instance.{problem}",
            )
        if problem := await variable_reference_problem(
            db, {"api_key": instance.api_key, "client_secret": instance.client_secret}, viewer_ids=[user.id]
        ):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"integration_instance.{problem}",
            )

        created_integration = Integration(
            user_id=user.id,
            provider=normalized_provider,
            auth_type=instance.auth_type,
            name=instance.name,
            description=instance.description,
            tags=instance.tags or [],
            requires_approval=bool(getattr(instance, "requires_approval", True)),
            scopes=instance.scopes,
            authorize_url=instance.authorize_url,
            token_url=instance.token_url,
            is_company_managed=instance.is_company_managed,
        )

        if instance.client_id:
            created_integration.client_id_enc = encrypt_value(instance.client_id)
        if instance.client_secret:
            created_integration.client_secret_enc = encrypt_value(instance.client_secret)
        if instance.api_key:
            created_integration.api_key_enc = encrypt_value(instance.api_key)
        if instance.extra_config:
            created_integration.extra_config_enc = encrypt_value(
                json.dumps(instance.extra_config)
            )

        if created_integration.auth_type in {"api_key", "bot_token"} and instance.api_key:
            created_integration.status = "connected"
        elif created_integration.auth_type == "none":
            has_extra_fields = bool(settings_schema)
            if instance.extra_config or not has_extra_fields:
                created_integration.status = "connected"
        elif created_integration.auth_type == "platform_managed":
            created_integration.status = "connected"
        elif created_integration.auth_type == "oauth" and instance.client_id:
            created_integration.status = "configured"

        # A company-managed OAuth instance shares the account it gets
        # connected with, as the UI's default choice for a manual creation
        # does: the importer is an admin, whose consent it is to give.
        # An instance holds no token here; an admin authorizes it later.
        apply_oauth_share_consent(
            created_integration, share_credentials=None, sharing_changed=True
        )

        db.add(created_integration)

    await db.flush()

    await log_audit(
        db,
        user=user,
        action="import_bundle",
        entity_type="integration_bundle",
        entity_id=None,
        entity_name=parsed.metadata.name,
        summary=(
            f"Upgraded integration bundle '{parsed.metadata.name}' to {parsed.metadata.version}"
            if upgraded
            else f"Imported integration bundle '{parsed.metadata.name}' ({parsed.metadata.version})"
        ),
        request=request,
        details={
            "provider": normalized_provider,
            "version": parsed.metadata.version,
            "upgraded": upgraded,
            "auth_type": parsed.metadata.auth_type,
            "supports_webhook": parsed.metadata.supports_webhook,
            "supports_polling": parsed.metadata.supports_polling,
            "integration_created": created_integration is not None,
            "integration_id": (
                str(created_integration.id) if created_integration is not None else None
            ),
        },
    )
    # Commit before answering: the get_db teardown only commits after the response is sent.
    await db.commit()

    return IntegrationBundleImportResponse(
        provider=normalized_provider,
        version=parsed.metadata.version,
        upgraded=upgraded,
        name=parsed.metadata.name,
        integration_created=created_integration is not None,
        integration_id=(
            str(created_integration.id) if created_integration is not None else None
        ),
        integration_name=(
            created_integration.name if created_integration is not None else None
        ),
    )


@router.get(
    "/admin/export/{provider_key}",
)
async def export_integration_bundle(
    provider_key: str,
    request: Request,
    integration_id: str | None = None,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Export a native or imported integration provider as a ZIP bundle."""
    if user.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only admins can export integration bundles.",
        )

    assets: dict[str, bytes] = {}
    metadata: dict

    if provider_key in PROVIDER_CATALOG:
        spec = PROVIDER_CATALOG[provider_key]
        settings_schema: list[dict] = []
        for key in spec.get("fields", []):
            if key in {"client_id", "client_secret"}:
                continue
            settings_schema.append(
                {
                    "key": key,
                    "label": key.replace("_", " ").title(),
                    "type": "secret" if _is_sensitive_setting_name(key) else "string",
                    "required": True,
                }
            )
        for key, label in (spec.get("extra_fields") or {}).items():
            row: dict = {
                "key": key,
                "label": label,
                "type": "secret" if _is_sensitive_setting_name(key) else "string",
                "required": False,
            }
            options = spec.get("extra_field_options", {}).get(key)
            if options:
                row["type"] = "select"
                row["options"] = [
                    {"value": o.get("value", ""), "label": o.get("label", "")}
                    for o in options
                ]
            settings_schema.append(row)

        supports_polling = any(
            (e.get("mechanism") in {"polling", "hybrid"})
            for e in spec.get("available_events", [])
            if isinstance(e, dict)
        )
        supports_webhook = any(
            (e.get("mechanism") in {"webhook", "hybrid"})
            for e in spec.get("available_events", [])
            if isinstance(e, dict)
        )

        # Native catalog keys may contain underscores (e.g. "mitre_attack"),
        # which the bundle slug rules reject — export with a hyphenated slug.
        bundle_slug = provider_key.replace("_", "-")
        # Version native exports with the running platform version so a newer
        # instance's export can be imported as an upgrade over an older one
        # (a hardcoded "1.0.0" would always 409 as a non-upgrade). Guard
        # against a non-semver APP_VERSION and fall back to "1.0.0".
        bundle_version = (
            settings.app_version if is_valid_semver(settings.app_version) else "1.0.0"
        )
        metadata = {
            "format": "filigran-integration-v1",
            "version": bundle_version,
            "name": spec.get("name") or provider_key,
            "slug": bundle_slug,
            "description": spec.get("description")
            or f"Exported integration provider '{provider_key}'.",
            "author": "Filigran",
            "provider": bundle_slug,
            "auth_type": spec.get("auth_type", "api_key"),
            "icon": None,
            "settings": settings_schema,
            "supports_polling": supports_polling,
            "supports_webhook": supports_webhook,
            "test_connection": {"mode": "none"},
            "exported_at": datetime.now(UTC).isoformat(),
        }
    else:
        row_result = await db.execute(
            select(CustomIntegrationDefinition).where(
                CustomIntegrationDefinition.provider_key == provider_key
            )
        )
        row = row_result.scalar_one_or_none()
        if row is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Unknown integration provider: {provider_key}",
            )

        metadata = dict(row.metadata_json or {})
        # Defense in depth for definitions imported before instance stripping:
        # an embedded instance (with credentials) must only ever be exported
        # via the explicit, audited ?integration_id= path below.
        metadata.pop("integration_instance", None)
        metadata["provider"] = row.slug
        metadata["slug"] = row.slug
        metadata["name"] = row.name
        metadata["version"] = row.version
        metadata["description"] = row.description
        metadata["author"] = row.author
        metadata["auth_type"] = row.auth_type
        metadata["supports_polling"] = row.supports_polling
        metadata["supports_webhook"] = row.supports_webhook
        metadata["settings"] = row.settings_schema or []
        metadata["exported_at"] = datetime.now(UTC).isoformat()

        icon_raw = _decode_logo_data_url(row.logo_data_url)
        if icon_raw and row.logo_path:
            metadata["icon"] = row.logo_path
            assets[row.logo_path] = icon_raw
        else:
            metadata["icon"] = None

    if integration_id:
        user_group_ids = await get_user_group_ids(db, user.id)
        integration = await get_user_integration(
            db,
            user.id,
            integration_id,
            user_group_ids,
            is_admin=user.is_admin,
        )
        if integration.provider != provider_key:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    "Selected integration does not match the provider requested for export."
                ),
            )
        metadata["integration_instance"] = _build_bundle_integration_instance_payload(
            integration
        )

    try:
        zip_bytes = build_integration_bundle_zip(metadata=metadata, assets=assets)
    except ValueError as err:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(err)) from err

    await log_audit(
        db,
        user=user,
        action="export_bundle",
        entity_type="integration_bundle",
        entity_id=None,
        entity_name=str(metadata.get("name") or provider_key),
        summary=f"Exported integration bundle '{provider_key}'",
        request=request,
        details={
            "provider": provider_key,
            "version": str(metadata.get("version") or "1.0.0"),
            "includes_integration_instance": bool(integration_id),
            "integration_id": integration_id,
        },
    )
    # Commit before answering: the get_db teardown only commits after the response is sent.
    await db.commit()

    safe_slug = str(metadata.get("slug") or provider_key).strip().lower().replace(":", "-")
    safe_version = str(metadata.get("version") or "1.0.0")
    filename = f"{safe_slug}-{safe_version}.zip"

    return StreamingResponse(
        io.BytesIO(zip_bytes),
        media_type="application/zip",
        headers={"Content-Disposition": build_content_disposition(filename)},
    )


@router.delete(
    "/admin/custom/{provider_key}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def uninstall_custom_integration_provider(
    provider_key: str,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Uninstall an imported custom provider and clean dependent integrations."""
    if user.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only admins can uninstall custom integration providers.",
        )

    normalized = normalize_custom_provider_slug(provider_key)
    row_result = await db.execute(
        select(CustomIntegrationDefinition).where(
            CustomIntegrationDefinition.provider_key == normalized
        )
    )
    row = row_result.scalar_one_or_none()
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Custom integration provider not found: {normalized}",
        )

    integrations_result = await db.execute(
        select(Integration).where(Integration.provider == normalized)
    )
    integrations_to_delete = integrations_result.scalars().all()
    deleted_ids = {str(integration.id) for integration in integrations_to_delete}
    if integrations_to_delete:
        # Bindings are keyed by provider, so one query on the provider key
        # covers every agent bound to a deleted instance; the OR over ids is a
        # defensive belt for ids stored under an unexpected key. All deleted
        # ids are stripped in a single pass per agent (no per-integration
        # Agent query).
        agents_result = await db.execute(
            select(Agent).where(
                or_(
                    cast(Agent.integration_bindings, Text).contains(normalized),
                    *[
                        cast(Agent.integration_bindings, Text).contains(iid)
                        for iid in deleted_ids
                    ],
                )
            )
        )
        for agent in agents_result.scalars().all():
            bindings = agent.integration_bindings or {}
            if any(
                any(v in deleted_ids for v in ids) for ids in bindings.values()
            ):
                cleaned = {
                    k: [v for v in ids if v not in deleted_ids]
                    for k, ids in bindings.items()
                }
                agent.integration_bindings = {k: v for k, v in cleaned.items() if v}
        for integration in integrations_to_delete:
            await db.delete(integration)

    await db.delete(row)
    await db.flush()

    await log_audit(
        db,
        user=user,
        action="uninstall_bundle",
        entity_type="integration_bundle",
        entity_id=None,
        entity_name=row.name,
        summary=f"Uninstalled custom integration provider '{normalized}'",
        request=request,
        details={
            "provider": normalized,
            "deleted_integrations": len(integrations_to_delete),
        },
    )
    # Commit before answering: the get_db teardown only commits after the response is sent.
    await db.commit()


@router.put(
    "/admin/custom/{provider_key}/enabled/{enabled}",
    response_model=CustomIntegrationProviderStatusResponse,
)
async def set_custom_integration_provider_enabled(
    provider_key: str,
    enabled: bool,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Enable or disable an imported custom provider in the catalog."""
    if user.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only admins can change custom provider status.",
        )

    normalized = normalize_custom_provider_slug(provider_key)
    row_result = await db.execute(
        select(CustomIntegrationDefinition).where(
            CustomIntegrationDefinition.provider_key == normalized
        )
    )
    row = row_result.scalar_one_or_none()
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Custom integration provider not found: {normalized}",
        )

    row.enabled = enabled
    row.updated_at = datetime.now(UTC)

    await log_audit(
        db,
        user=user,
        action="toggle_bundle",
        entity_type="integration_bundle",
        entity_id=None,
        entity_name=row.name,
        summary=(
            f"Enabled custom integration provider '{normalized}'"
            if enabled
            else f"Disabled custom integration provider '{normalized}'"
        ),
        request=request,
        details={"provider": normalized, "enabled": enabled},
    )
    # Commit before answering: the get_db teardown only commits after the response is sent.
    await db.commit()

    return CustomIntegrationProviderStatusResponse(provider=normalized, enabled=enabled)


#: Request fields that share an integration or change how it is shared. An
#: update touching one of them settles the OAuth share consent as a share.
_SHARING_FIELDS = frozenset(
    {
        "shared_group_ids",
        "shared_group_permissions",
        "shared_user_ids",
        "shared_user_permissions",
        "access_restricted",
        "is_company_managed",
        "auth_type",
    }
)


def _requested_credential_share(
    body: CreateIntegrationRequest | UpdateIntegrationRequest,
    integration: Integration,
    *,
    sharing_changed: bool,
) -> bool | None:
    """The request's choice about sharing the connected account.

    ``keep_oauth_credentials_on_share: true`` shares it, ``false`` shares the
    integration without it. Omitted (or null), the baseline meaning holds:
    a request that shares an account nobody consented
    for removes the token, as sharing always did, and any other request
    leaves the consent as it is. The UI always sends the choice.
    """
    from app.services.integration_credentials import requested_share_choice

    return requested_share_choice(
        body.keep_oauth_credentials_on_share, integration, sharing_changed=sharing_changed
    )


async def _forget_oauth_share_dismissal(
    db: AsyncSession, integration: Integration, *, actor: User
) -> None:
    """Drop ``integration`` from its owner's closed OAuth-share reminders.

    Called when ``oauth_share_acknowledged`` flips back to False — sharing was
    turned off, the token stripped, or the auth type moved away from OAuth —
    and when the integration is deleted. The dashboard reminder's dismissal
    is a decision about a share that exists; keeping it would leave the
    reminder muted if the owner shares the same integration again later, and a
    deleted id would count against the cap of every later dismissal. The owner is usually
    the actor; an admin editing someone else's integration loads them. A no-op
    when nothing was dismissed, so an unrelated edit never dirties the owner's
    row.
    """
    owner_id = integration.user_id
    if owner_id is None:
        return
    owner = actor if actor.id == owner_id else await db.get(User, owner_id)
    if owner is None:
        return
    closed = getattr(owner, "oauth_share_dismissed_integration_ids", None) or []
    integration_id = str(integration.id)
    if not any(str(x) == integration_id for x in closed):
        return
    owner.oauth_share_dismissed_integration_ids = [
        x for x in closed if str(x) != integration_id
    ]


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


def _require_catalog_creation_rules(
    catalog: Mapping[str, Any], provider: str, user: User, is_company_managed: bool
) -> None:
    """Refuse a creation its catalog entry rules out.

    ``requires_platform_smtp``: the provider sends through the deployment's SMTP,
    so it exists only while SMTP is configured. ``company_managed_only``: an
    administrator creates it, company-managed, never as a personal integration.
    """
    if catalog.get("requires_platform_smtp") and not is_email_configured():
        raise HTTPException(
            status_code=400,
            detail=f"Provider '{provider}' needs the platform's SMTP server, and none is configured (SMTP_HOSTNAME).",
        )
    if catalog.get("company_managed_only") and not (user.is_admin and is_company_managed):
        raise HTTPException(
            status_code=403,
            detail=f"Provider '{provider}' is created by an administrator, as a company-managed integration only.",
        )


def _validate_a2a_federation_config(
    provider: str, direction: str | None, extra_config: dict
) -> None:
    """422 when an INBOUND federation config does not validate.

    Runs the SAME parser the A2A auth path applies per request — Entra's
    three pinned facts, or Google's audience and allowed caller — so an
    integration this API accepts is exactly one whose tokens the task surface
    will consider.

    Gated on DIRECTION: only an inbound registration's config is a set of
    pins. An outbound one holds card URLs and OUR OWN credentials, and
    legitimately has no ``audience`` — validating it as pins would refuse it
    with a 422 naming a field its direction does not have.
    """
    from app.a2a.providers import federation_config_problem

    if problem := federation_config_problem(provider, direction, extra_config):
        raise HTTPException(status_code=422, detail=problem)


@router.post(
    "", response_model=IntegrationResponse, status_code=status.HTTP_201_CREATED
)
async def create_integration(
    body: CreateIntegrationRequest,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _agentic_gate: None = Depends(require_agentic_ai_licensed),
):
    """Create a new integration with all credentials configured via the UI."""
    await require_vault_writer(db, user, api_key=body.api_key, client_secret=body.client_secret)
    await require_secret_variable_references(
        db, {"api_key": body.api_key, "client_secret": body.client_secret}, viewer_ids=[user.id]
    )
    catalog = await _get_provider_catalog_entry(db, body.provider)
    if not catalog:
        raise HTTPException(
            status_code=400, detail=f"Unknown integration provider: {body.provider}"
        )

    # An A2A integration is offered exactly while agentic chat is, both
    # directions alike, and must declare its direction.
    from app.a2a.providers import ALL_A2A_PROVIDERS
    from app.models.a2a import DIRECTION_INBOUND, DIRECTION_OUTBOUND

    # An A2A token endpoint is held to the A2A destination guard when a token
    # is minted; every other provider's token URL to the token endpoint policy.
    if body.provider not in ALL_A2A_PROVIDERS:
        await require_allowed_token_url(body.token_url)

    a2a_direction = None
    if body.provider in ALL_A2A_PROVIDERS:
        await require_chat_enabled(db)
        a2a_direction = body.a2a_direction or DIRECTION_INBOUND
        if a2a_direction not in (DIRECTION_INBOUND, DIRECTION_OUTBOUND):
            raise HTTPException(
                status_code=400,
                detail="a2a_direction must be 'inbound' or 'outbound'.",
            )

    # Federation pins are trust configuration consumed unattended by the A2A
    # auth path, so a bad value must fail HERE, in front of the operator —
    # not as unexplained partner 401s later. Absent config is allowed (the
    # integration just stays disconnected until filled in).
    if body.extra_config:
        _validate_a2a_federation_config(
            body.provider, a2a_direction, body.extra_config
        )

    from app.api.v1.platform_settings_helpers import get_platform_settings as _load_ps

    ps = await _load_ps(db)
    disabled_providers = set(ps.disabled_integration_providers or [])
    if body.provider in disabled_providers:
        raise HTTPException(
            status_code=403,
            detail=f"Provider '{body.provider}' is currently disabled by an admin.",
        )
    _require_catalog_creation_rules(catalog, body.provider, user, bool(body.is_company_managed))

    if body.provider.startswith("custom:"):
        custom_row_result = await db.execute(
            select(CustomIntegrationDefinition).where(
                CustomIntegrationDefinition.provider_key == body.provider
            )
        )
        custom_row = custom_row_result.scalar_one_or_none()
        if custom_row is not None and not custom_row.enabled:
            raise HTTPException(
                status_code=403,
                detail=f"Custom provider '{body.provider}' is disabled.",
            )

    # In XTM One mode, registration-only integrations can only be created via
    # platform registration — block manual creation from the UI/API.
    if is_registration_only_provider(body.provider):
        raise HTTPException(
            status_code=400,
            detail=f"{body.provider} integrations are managed via platform registration and cannot be created manually.",
        )

    auth_type = body.auth_type or catalog["auth_type"]

    # OpenAPI integrations: load + convert the spec BEFORE anything is written,
    # so a bad document is a 400 with nothing half-created. The auth type is
    # the chosen authentication option's — never the request's own claim.
    openapi_plan = None
    openapi_spec_row = None
    if body.provider == OPENAPI_PROVIDER:
        await _limit_spec_fetches(request, user)
        openapi_plan, openapi_spec_row = await _prepare_openapi_import(body)
        auth_type = openapi_plan.auth_option.integration_auth_type

    # The provider's auth types (#8386): its supported_auth_types, else exactly
    # its auth_type. The OpenAPI type comes from the spec's option, never the request.
    supported = supported_auth_types(catalog)
    if openapi_plan is None and supported and auth_type not in supported:
        raise HTTPException(
            status_code=400,
            detail=unsupported_auth_type_message(auth_type, body.provider, supported),
        )
    if auth_type == "platform_managed" and not platform_base_url(body.extra_config):
        raise HTTPException(status_code=400, detail=PLATFORM_MANAGED_NEEDS_BASE_URL)
    # A config given must be complete for its provider (#9898); a create with
    # none at all is filled in later and stays without a credential until then.
    if body.extra_config and (
        missing := missing_required_extra_fields(body.provider, body.extra_config)
    ):
        raise HTTPException(
            status_code=400,
            detail=missing_required_extra_fields_message(body.provider, missing),
        )
    # Typed values within their rule (a domain list, a bounded number).
    if body.extra_config and (invalid := invalid_extra_fields(body.provider, body.extra_config)):
        raise HTTPException(status_code=422, detail=" ".join(invalid))

    # Only admins can create company-managed integrations
    require_admin_for_company_managed(
        body, user, "Only admins can create company-managed integrations"
    )

    # ── Auto-fill from admin OAuth defaults when user doesn't supply creds ──
    effective_client_id = body.client_id
    effective_client_secret = body.client_secret
    # A domain list sent as a JSON list is stored one entry per line, as the form stores it.
    effective_extra = (
        normalize_typed_extra_fields(body.provider, body.extra_config) if body.extra_config else body.extra_config
    )
    uses_admin_oauth_credentials = False

    # The one rule, shared with the agent builder's create_integration.
    if takes_admin_oauth_app(
        auth_type,
        client_id_given=bool(body.client_id),
        is_company_managed=bool(body.is_company_managed),
    ):
        admin_app = await admin_oauth_app(db, body.provider)
        if admin_app is not None:
            effective_client_id = admin_app.client_id
            # As stored: a vault reference stays a reference on the integration,
            # whose column accepts one and whose readers resolve it.
            effective_client_secret = admin_app.client_secret
            uses_admin_oauth_credentials = True
            if admin_app.extra_config and not effective_extra:
                effective_extra = dict(admin_app.extra_config)
            logger.info(
                "Auto-filled OAuth credentials from admin defaults for %s",
                body.provider,
            )

    relay_group_id = await checked_relay_group_id(db, user=user, value=body.relay_group_id)
    _require_relay_when_needed(catalog, relay_group_id)

    # A custom bundle's base URL is user-supplied: held to the base URL
    # policy here, before anything is written, and on every connection.
    if body.provider.startswith("custom:"):
        await require_allowed_base_urls(
            effective_extra,
            base_url_keys(custom_row.metadata_json if custom_row is not None else None),
            routed=relay_group_id is not None,
        )

    # Use pre-generated ID if the frontend provided one (enables webhook URL preview)
    integ_kwargs: dict = {}
    if body.id:
        try:
            integ_kwargs["id"] = uuid.UUID(body.id)
        except ValueError as exc:
            raise HTTPException(
                status_code=400,
                detail="Invalid integration ID format (must be a valid UUID)",
            ) from exc

    default_scopes = catalog.get("scopes")
    default_authorize_url = catalog.get("authorize_url")
    default_token_url = catalog.get("token_url")
    if openapi_plan is not None:
        oauth_defaults = openapi_plan.auth_option.oauth_settings()
        default_scopes = oauth_defaults["scopes"]
        default_authorize_url = oauth_defaults["authorize_url"]
        default_token_url = oauth_defaults["token_url"]
        effective_extra = _openapi_extra_config(effective_extra, openapi_plan)

    integration = Integration(
        **integ_kwargs,
        user_id=user.id,
        is_company_managed=body.is_company_managed,
        provider=body.provider,
        auth_type=auth_type,
        name=body.name,
        description=body.description,
        tags=body.tags or [],
        scopes=body.scopes if body.scopes is not None else default_scopes,
        authorize_url=body.authorize_url or default_authorize_url,
        token_url=body.token_url or default_token_url,
        uses_admin_oauth_credentials=uses_admin_oauth_credentials,
        relay_group_id=relay_group_id,
    )
    await apply_group_share_fields(db, integration, body, partial=False)

    if effective_client_id:
        integration.client_id_enc = encrypt_value(effective_client_id)
    if effective_client_secret:
        integration.client_secret_enc = encrypt_value(effective_client_secret)
    elif auth_type == "oauth" and not integration.client_id_enc:
        # A provider whose clients register themselves (catalog
        # ``oauth_registration``): registered here, so the integration page
        # offers Authorize at once. A refusal (the redirect URI not on the
        # provider's allowlist) is said before anything is stored.
        from app.services.integration_oauth_registration import (
            IntegrationOAuthRegistrationError,
            register_integration_client,
        )

        try:
            await register_integration_client(db, integration, commit=False)
        except IntegrationOAuthRegistrationError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    if body.api_key:
        integration.api_key_enc = encrypt_value(body.api_key)
        # Bot tokens are immediately "connected"
        if auth_type in ("api_key", "bot_token"):
            integration.status = "connected"
    if effective_extra:
        integration.extra_config_enc = encrypt_value(json.dumps(effective_extra))

    # For non-OAuth integrations, mark as connected if credentials are provided
    if auth_type in ("api_key", "bot_token") and (body.api_key or effective_client_id):
        integration.status = "connected"

    # No-auth integrations (e.g. RSS) are connected as soon as extra_config is set,
    # or immediately if the provider has no extra_fields at all. Providers whose
    # extra fields are ALL optional tuning knobs (MITRE ATT&CK / ATLAS — flagged
    # with ``extra_fields_optional`` in the catalog) work out of the box, so
    # they are connected even when every field was left blank.
    if auth_type == "none":
        has_extra_fields = bool(catalog.get("extra_fields"))
        if (
            effective_extra
            or not has_extra_fields
            or catalog.get("extra_fields_optional")
        ):
            integration.status = "connected"

    # Platform-managed integrations are credential-less — always connected
    if auth_type == "platform_managed":
        integration.status = "connected"

    # The API key of an OpenAPI integration is optional: it works without one.
    if openapi_plan is not None and auth_type == "api_key":
        integration.status = "connected"

    # For OAuth, status stays "disconnected" until OAuth flow completes
    if auth_type == "oauth" and effective_client_id:
        integration.status = "configured"  # configured but not yet authorized

    # OAuth2 client credentials and password grant: usable as soon as they
    # are complete — tokens are fetched per call, there is nothing to authorize.
    if auth_type in _TOKEN_GRANT_AUTH_TYPES:
        integration.status = _token_grant_status(integration)

    # A typed credential is stored, not checked: "Not tested" until a test runs.
    if typed_credential_untested(auth_type, api_key=body.api_key, client_secret=effective_client_secret):
        integration.connection_test_status = CONNECTION_TEST_UNTESTED

    await apply_user_share_fields(db, integration, body, partial=False)

    # The share choice also records whose account the people it is shared
    # with use; nobody connected their own yet.
    apply_oauth_share_consent(
        integration,
        share_credentials=_requested_credential_share(
            body, integration, sharing_changed=True
        ),
        sharing_changed=True,
        implicit_consent=False,
    )

    db.add(integration)
    await db.flush()
    if openapi_spec_row is not None:
        openapi_spec_row.integration_id = integration.id
        db.add(openapi_spec_row)
        await db.flush()
    # An A2A integration is not valid without a direction: the exposure hook
    # and the tool seam both read it, and a row without one belongs to neither
    # direction — inert in a way an operator would read as a broken feature.
    # Created here, immediately after the integration, so no code path can
    # produce one without the other.
    # The card URLs the operator supplied become the agents an outbound
    # registration can reach, in the same step (``provision_settings``).
    if a2a_direction is not None:
        from app.a2a.providers import provision_settings

        await provision_settings(db, integration, a2a_direction)
    # Provider lifecycle seam: run the provider's setup when it lands connected
    # (e.g. Nova creating its rule-sync source). Best-effort — never fails the
    # connect. OAuth providers reach "connected" later, in the callback.
    if integration.status == "connected":
        await dispatch_on_connect(db, integration)
    await log_audit(
        db,
        user=user,
        action="create",
        entity_type="integration",
        entity_id=integration.id,
        entity_name=integration.name,
        summary=f"Created integration '{integration.name}' ({integration.provider})",
        request=request,
        details={
            "provider": integration.provider,
            "integration_type": catalog.get("integration_types", []),
            "is_company_managed": integration.is_company_managed,
            "tags": integration.tags or [],
            # Which way an A2A partner relationship points is the single most
            # consequential thing about it, so it belongs in the audit record
            # rather than only in the row (FR-039).
            **({"a2a_direction": a2a_direction} if a2a_direction else {}),
            **(
                {"openapi": _openapi_audit_details(openapi_plan)}
                if openapi_plan is not None
                else {}
            ),
            **({"relay_group_id": str(gid)} if (gid := getattr(integration, "relay_group_id", None)) else {}),
        },
    )
    await db.commit()
    return _to_response(
        integration,
        can_manage=can_manage(user, integration),
        can_authorize=can_authorize_integration(user, integration),
        owner_name=user.full_name,
        secret_config_keys=await _custom_secret_config_keys(db, integration.provider),
        platform_approval=await platform_approval_by_provider(db),
        admin_client_ids=await _admin_oauth_client_ids(db, [integration]),
        vault_viewer=await vault_principal(db, user),
        a2a_direction=a2a_direction,
    )


# ---------------------------------------------------------------------------
# Get
# ---------------------------------------------------------------------------


@router.get("/{integration_id}", response_model=IntegrationResponse)
async def get_integration(
    integration_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get a single integration (must be own, group-shared, company-managed, or admin)."""
    user_group_ids = await get_user_group_ids(db, user.id)
    integration = await get_user_integration(
        db, user.id, integration_id, user_group_ids, is_admin=user.is_admin
    )
    owner_name = None
    if integration.user_id:
        owner_row = await db.execute(
            select(User.full_name).where(User.id == integration.user_id)
        )
        owner_name = owner_row.scalar_one_or_none()
    own_credentials = await _own_credential_fields(db, user, [integration], user_group_ids)
    response = _to_response(
        integration,
        can_manage=can_manage(user, integration, user_group_ids),
        can_authorize=can_authorize_integration(user, integration),
        owner_name=owner_name,
        token_owner_name=await _token_owner_name(db, integration),
        secret_config_keys=await _custom_secret_config_keys(db, integration.provider),
        platform_approval=await platform_approval_by_provider(db),
        admin_client_ids=await _admin_oauth_client_ids(db, [integration]),
        vault_viewer=principal_from(user, user_group_ids),
        a2a_direction=(await _a2a_directions(db, [integration])).get(str(integration.id)),
        **own_credentials.get(str(integration.id), {}),
    )
    if response.a2a_direction == "outbound":
        from app.a2a.client.credential_state import describe
        from app.a2a.providers import get_settings

        response.a2a_credential = describe(await get_settings(db, integration.id), integration)
    elif response.a2a_direction == "inbound" and response.can_manage:
        # The last token request this credential refused (#105): its managers
        # alone, the token endpoint answering every caller invalid_client.
        from app.a2a.providers import get_settings
        from app.a2a.refusals import describe_refusal

        refusal = describe_refusal(await get_settings(db, integration.id))
        if refusal is not None:
            response.a2a_credential = {"last_refused_token_request": refusal}
    await attach_vault_failures([response], Integration.__tablename__)
    return response


# ---------------------------------------------------------------------------
# Update
# ---------------------------------------------------------------------------

#: What an edit may change while the integration keeps a vault reference its
#: editor could not have written: none of these moves where the
#: platform sends the secret. Any other field it changes does.
_FIELDS_KEEPING_SECRET_DESTINATION = frozenset(
    {
        "name",
        "description",
        "tags",
        "requires_approval",
        "signature_enabled",
        "signature_integration_id",
        "api_key",
        "client_id",
        "client_secret",
        "scopes",
        "keep_oauth_credentials_on_share",
        "is_company_managed",
        "user_id",
        "access_restricted",
        "shared_group_ids",
        "shared_group_permissions",
        "shared_user_ids",
        "shared_user_permissions",
    }
)


def merge_extra_config_values(
    stored: dict[str, Any],
    submitted: dict[str, Any],
    is_masked_secret: Callable[[str, object], bool] = lambda _key, _value: False,
) -> dict[str, Any]:
    """*submitted* merged into *stored*, key by key: the extra config an update stores.

    A key left out keeps its stored value; a key sent as ``None`` (JSON
    ``null``) is removed; an object sent where an object is stored is merged
    the same way, one level down and further; any other value replaces the
    stored one. A top-level secret sent back as the response mask keeps its
    stored value. An update never deletes a key it does not name: a caller
    setting one key cannot drop the keys an integration needs to work
    (a backend selector, an id) by leaving them out.
    """

    def _merge(base: dict[str, Any], patch: dict[str, Any], top: bool) -> dict[str, Any]:
        out = dict(base)
        for key, value in patch.items():
            if value is None:
                out.pop(key, None)
            elif top and is_masked_secret(key, value):
                # The response mask round-tripped: keep what is stored (nothing
                # stored keeps nothing, never the mask itself).
                if key not in base:
                    out.pop(key, None)
            elif isinstance(value, dict) and isinstance(base.get(key), dict):
                out[key] = _merge(base[key], value, False)
            else:
                out[key] = value
        return out

    return _merge(stored if isinstance(stored, dict) else {}, submitted, True)


async def _merge_masked_extra_config(
    db: AsyncSession, integration: Integration, submitted: dict[str, Any]
) -> dict[str, Any]:
    """The extra config an edit stores: the stored one with *submitted* merged in.

    :func:`merge_extra_config_values` decides: a key left out is kept, ``None``
    removes it. Responses redact secret values with EXTRA_CONFIG_MASK; an edit
    that round-trips the mask means "keep the currently stored value". The
    preservation is scoped to exactly the keys the response redacts
    (schema-declared secrets + sensitive-name heuristic) so a non-secret field
    can still be set to the literal mask string intentionally.
    """
    secret_keys = await _custom_secret_config_keys(db, integration.provider)

    def _is_masked_secret(key: str, value: object) -> bool:
        return value == EXTRA_CONFIG_MASK and (
            key in secret_keys or _is_sensitive_setting_name(key)
        )

    try:
        stored = decrypt_json(integration.extra_config_enc) or {}
    except Exception:
        stored = {}
    return merge_extra_config_values(stored, submitted, _is_masked_secret)


# A registration-editable field is a short text (an OpenAEV tenant id is a UUID).
_REGISTRATION_FIELD_MAX_LENGTH = 200


def _merge_registration_extra_config(
    integration: Integration, submitted: dict[str, Any]
) -> dict[str, Any]:
    """The extra config an edit of a registration-only integration stores.

    Only the catalog's ``registration_editable_extra_fields`` change (text; empty removes
    the key); any other key sent with a new value is refused, not silently dropped. An
    editable secret (``webhook_secret``) sent back as the response mask keeps its stored
    value, the read -> edit -> save rule of every other integration.
    """
    try:
        stored = decrypt_json(integration.extra_config_enc) or {}
    except Exception as exc:
        # Storing the edit alone would erase the registration's own keys.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="The stored configuration cannot be read, so it cannot be edited here.",
        ) from exc
    if not isinstance(stored, dict):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="The stored configuration cannot be read, so it cannot be edited here.",
        )
    merged = dict(stored)
    editable = registration_editable_extra_fields(integration.provider)
    choices = PROVIDER_CATALOG.get(integration.provider, {}).get("extra_field_options") or {}
    for key, value in submitted.items():
        if key in editable:
            if value == EXTRA_CONFIG_MASK and _is_sensitive_setting_name(key):
                continue
            if value is not None and not isinstance(value, str):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"'{key}' must be a string.",
                )
            text = (value or "").strip()
            if len(text) > _REGISTRATION_FIELD_MAX_LENGTH:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"'{key}' is too long.",
                )
            # A field the catalog declares as a choice (Live writes) takes one of its values.
            allowed = {o.get("value") for o in choices.get(key) or ()}
            if text and allowed and text not in allowed:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"'{key}' must be one of: {', '.join(sorted(allowed))}.",
                )
            if text:
                merged[key] = text
            else:
                merged.pop(key, None)
        elif value is None:
            # A removal of a key the registration sets keeps it, as leaving
            # the key out does (an update merges, ``merged_extra_config``).
            continue
        elif value != stored.get(key) and value != EXTRA_CONFIG_MASK:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f"'{key}' is set by the platform registration and cannot be "
                    "edited here."
                ),
            )
    return merged


def required_extra_refusal(integration: Integration, new_extra: dict[str, Any]) -> str | None:
    """Why storing *new_extra* on *integration* is refused, else None (#9898).

    A required key (catalog ``extra_field_required_when``) the write would newly
    leave missing: dropped, or required by a selector the write switched. A key
    already missing before is not held against the edit. The one check the REST
    update and the agent builder's ``update_integration`` apply before writing.
    """
    try:
        stored = decrypt_json(integration.extra_config_enc) or {}
    except Exception:
        stored = {}
    missing = newly_missing_required_extra_fields(integration.provider, stored, new_extra)
    return missing_required_extra_fields_message(integration.provider, missing) if missing else None


def _refuse_newly_missing_required_extra(
    integration: Integration, new_extra: dict[str, Any]
) -> None:
    if problem := required_extra_refusal(integration, new_extra):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=problem)


async def merged_extra_config(
    db: AsyncSession, integration: Integration, submitted: dict[str, Any]
) -> dict[str, Any]:
    """The extra config an edit of *integration* stores, the one rule for every write path.

    A registration-only integration changes only its catalog-listed keys
    (:func:`_merge_registration_extra_config`, which refuses any other change with an
    ``HTTPException``); any other integration stores the stored config with *submitted*
    merged in key by key, ``None`` removing a key and its masked secrets kept
    (:func:`_merge_masked_extra_config`). The REST update, its re-point check
    and the agent builder's ``update_integration`` all read it, so they cannot drift.
    """
    if is_registration_only_provider(integration.provider):
        return _merge_registration_extra_config(integration, submitted)
    return await _merge_masked_extra_config(db, integration, submitted)


async def _update_repoints_integration(
    db: AsyncSession, integration: Integration, body: UpdateIntegrationRequest
) -> bool:
    """Whether *body* changes where *integration* sends its secrets: a field outside
    :data:`_FIELDS_KEEPING_SECRET_DESTINATION` set to something else than stored.

    The edit form sends its unchanged fields back, so a value equal to the
    stored one (the extra config after the mask is restored) changes nothing.
    """
    for name in sorted(body.model_fields_set - _FIELDS_KEEPING_SECRET_DESTINATION):
        value = getattr(body, name)
        if name == "relay_group_id":
            # Set, moved or cleared (null = send directly): the requests, and the
            # secrets they carry, leave through another way.
            if safe_uuid(str(value or "")) != integration.relay_group_id:
                return True
            continue
        if value is None:
            continue
        if name == "extra_config":
            try:
                stored = decrypt_json(integration.extra_config_enc) or {}
            except Exception:
                stored = {}
            if await merged_extra_config(db, integration, value) != stored:
                return True
        elif name in ("authorize_url", "token_url", "auth_type"):
            if (value or None) != (getattr(integration, name) or None):
                return True
        else:
            return True
    return False


async def _apply_signature_setting(
    db: AsyncSession, integration: Integration, body: UpdateIntegrationRequest, user: User
) -> None:
    """The Email signature setting *body* sets, on an integration whose tools send email.

    A signature integration newly chosen must be one the editor can see, of a
    provider that holds signatures (``email_signature_settings``).
    """
    from app.services.email_signature_settings import (
        SignatureSettingError,
        checked_signature_integration,
    )
    from app.services.email_signatures import email_sender_providers

    if integration.provider not in email_sender_providers():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Only an integration whose tools send email has an Email signature setting.",
        )
    if body.signature_enabled is not None:
        integration.signature_enabled = body.signature_enabled
    if "signature_integration_id" in body.model_fields_set:
        try:
            integration.signature_integration_id = await checked_signature_integration(
                db, user.id, body.signature_integration_id, baseline=integration.signature_integration_id
            )
        except SignatureSettingError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc


async def apply_integration_company_managed(
    db: AsyncSession,
    integration: Integration,
    company_managed: bool,
    *,
    actor: User,
    keep_grants: bool = False,
) -> None:
    """Make *integration* company-managed or private again; the caller checked the admin rule.

    The one write of the flag, read by the REST update and the agent builder's
    ``update_integration``. Going company-managed keeps the owner for
    provenance and clears the group grants unless *keep_grants* (the same
    request narrows it with ``access_restricted``: the grants are its audience).
    Going private gives it back to its owner (*actor* for a legacy row with
    none) and unbinds it from other owners' agents and from every
    company-managed agent, which binds company-managed integrations only (#11866).
    """
    integration.is_company_managed = company_managed
    if company_managed:
        if not keep_grants:
            integration.shared_group_ids = None
            integration.shared_group_permissions = None
        return
    if integration.user_id is None:
        integration.user_id = actor.id
    owner_id = integration.user_id
    integ_id_str = str(integration.id)
    agents_result = await db.execute(
        select(Agent).where(
            or_(
                and_(Agent.user_id != owner_id, Agent.user_id.isnot(None)),
                Agent.is_company_managed.is_(True),
            )
        )
    )
    for agent in agents_result.scalars().all():
        if agent.integration_bindings and any(
            integ_id_str in ids for ids in (agent.integration_bindings or {}).values()
        ):
            cleaned = {k: [iid for iid in ids if iid != integ_id_str] for k, ids in agent.integration_bindings.items()}
            agent.integration_bindings = {k: v for k, v in cleaned.items() if v}


@router.put("/{integration_id}", response_model=IntegrationResponse)
async def update_integration(
    integration_id: str,
    body: UpdateIntegrationRequest,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    after_commit: AfterCommit = Depends(run_after_commit, scope="function"),
):
    user_group_ids = await get_user_group_ids(db, user.id)
    # Locked until the commit: the stored-reference check below must see what an
    # edit committed meanwhile wrote, or an admin's reference could follow this
    # editor's new destination.
    integration = await get_user_integration(
        db, user.id, integration_id, user_group_ids, is_admin=user.is_admin, lock=True
    )

    # Only admins can edit company-managed integrations
    require_admin_for_company_managed(
        integration, user, "Only admins can edit company-managed integrations"
    )

    # Group-shared integrations require config-tier permission: `get_user_integration`
    # only enforces visibility, so without this a group `read_only` member could
    # rewrite credentials / OAuth endpoints or self-escalate share permissions.
    if not can_manage(user, integration, user_group_ids):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You don't have permission to edit this integration.",
        )

    # Built-in integrations are not editable
    if integration.is_builtin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Built-in integrations cannot be edited.",
        )

    await require_vault_writer(db, user, api_key=body.api_key, client_secret=body.client_secret)
    # Resolved with the owner's visibility at use time: both must see it.
    await require_secret_variable_references(
        db,
        {"api_key": body.api_key, "client_secret": body.client_secret},
        viewer_ids=[user.id, integration.user_id],
    )
    # A vault reference the editor could not have written may not follow a
    # destination they chose: such an edit replaces it, or is refused.
    replaced = {
        column
        for column, value in (("api_key_enc", body.api_key), ("client_secret_enc", body.client_secret))
        if value is not None
    }
    # Cleared below only by a switch to platform_managed, never by an edit of one.
    if body.auth_type == "platform_managed" and integration.auth_type != "platform_managed":
        replaced |= {"api_key_enc", "client_secret_enc"}
    if (
        retained := retained_reference_writer_problem(
            integration, replaced=replaced, principal=await vault_principal(db, user)
        )
    ) and await _update_repoints_integration(db, integration, body):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=retained)
    # Read before the configuration below can move the integration elsewhere.
    key_binding_before = api_key_binding_of(integration)

    # Sharing the connected account is the account holder's consent to give
    # (the owner, the token's recorded owner, an admin). Refused before
    # anything is written; an implicit share by anyone else saves without it.
    may_consent = may_consent_to_share(integration, user.id, is_admin=user.is_admin)
    if (
        not may_consent
        and body.keep_oauth_credentials_on_share is True
        and not integration.oauth_share_acknowledged
        and (body.auth_type or integration.auth_type) == "oauth"
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail=SHARE_CONSENT_REFUSED_DETAIL
        )

    # Before anything is written, for the same reason as the direction below.
    # A2A token endpoints are held to the A2A destination guard instead.
    from app.a2a.providers import ALL_A2A_PROVIDERS as _A2A_PROVIDERS

    if integration.provider not in _A2A_PROVIDERS:
        await require_allowed_token_url(body.token_url)

    if "relay_group_id" in body.model_fields_set:
        relay_group_id = await checked_relay_group_id(
            db, user=user, value=body.relay_group_id, baseline=integration.relay_group_id
        )
        _require_relay_when_needed(PROVIDER_CATALOG.get(integration.provider) or {}, relay_group_id)
        integration.relay_group_id = relay_group_id

    # Direction, before anything else is written. A refusal must leave the
    # integration untouched, and applying half an update and then refusing the
    # direction would do the opposite.
    if body.a2a_direction is not None:
        from app.a2a.providers import (
            ALL_A2A_PROVIDERS,
            DirectionChangeRefusedError,
            change_direction,
        )
        from app.models.a2a import DIRECTION_INBOUND, DIRECTION_OUTBOUND

        if integration.provider not in ALL_A2A_PROVIDERS:
            raise HTTPException(
                status_code=400,
                detail="a2a_direction applies only to A2A integrations.",
            )
        if body.a2a_direction not in (DIRECTION_INBOUND, DIRECTION_OUTBOUND):
            raise HTTPException(
                status_code=400,
                detail="a2a_direction must be 'inbound' or 'outbound'.",
            )
        # One gate covers both directions.
        await require_chat_enabled(db)
        try:
            await change_direction(db, integration, body.a2a_direction)
        except DirectionChangeRefusedError as exc:
            # 409, not 400: the request is well-formed and the operator has
            # authority — it conflicts with current state, and the message
            # names the agents so they know what to unbind (FR-057).
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    if body.name is not None:
        # A rename never touches ``tool_slug``: the tool names agents,
        # skills and assignment gates cite stay byte-identical.
        integration.name = body.name
    if body.tool_slug is not None and body.tool_slug != integration.tool_slug:
        from app.services.integration_tool_slugs import ToolSlugError, validate_tool_slug_change

        try:
            integration.tool_slug = await validate_tool_slug_change(db, integration, body.tool_slug)
        except ToolSlugError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    if body.description is not None:
        integration.description = body.description if body.description else None
    if body.tags is not None:
        integration.tags = body.tags
    if body.requires_approval is not None:
        # Turning this off exempts every tool this integration provides from
        # human approval. Audited like any other configuration change.
        #
        # Not for a registered Suite platform, though: its approval is one
        # decision made per platform, and the gate reads that policy alone.
        # Storing a contradicting value here would be worse than useless — it
        # would answer future reads with a preference nothing enforces.
        governed, _ = await platform_approval_by_provider(db)

        if integration.provider in governed:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    "Approval for a registered platform is set once for the "
                    "platform, in Settings → Access and security → Tool approvals, not per integration."
                ),
            )
        integration.requires_approval = body.requires_approval
    if {"signature_enabled", "signature_integration_id"} & body.model_fields_set:
        await _apply_signature_setting(db, integration, body, user)
    if (
        body.client_id is not None
        and replace_oauth_client_id(integration, body.client_id)
        and is_per_user(integration)
    ):
        # A new OAuth app: the people's own grants were given to the old one.
        await delete_user_credentials(db, integration.id)
    if body.client_secret is not None:
        integration.client_secret_enc = (
            encrypt_value(body.client_secret) if body.client_secret else None
        )
    if (body.client_id is not None or body.client_secret) and integration.provider == "oauth_a2a":
        # A new outbound A2A partner credential: its rejection no longer holds,
        # the next mint is the test (#12346). An inbound row is left alone.
        from app.a2a.client.credential_state import clear_on_credential_edit

        await clear_on_credential_edit(db, integration)
    if body.api_key is not None:
        integration.api_key_enc = encrypt_value(body.api_key) if body.api_key else None
        if body.api_key and integration.auth_type in ("api_key", "bot_token"):
            integration.status = "connected"
    # A new credential makes the last test's result stale.
    if typed_credential_untested(integration.auth_type, api_key=body.api_key, client_secret=body.client_secret):
        integration.connection_test_status = CONNECTION_TEST_UNTESTED
    if body.extra_config is not None:
        new_extra = await merged_extra_config(
            db, integration, normalize_typed_extra_fields(integration.provider, body.extra_config)
        )
        _refuse_newly_missing_required_extra(integration, new_extra)
        if invalid := invalid_extra_fields(integration.provider, body.extra_config):
            raise HTTPException(status_code=422, detail=" ".join(invalid))
        if integration.provider.startswith("custom:"):
            # The base URL policy, as on create; the session rolls back on
            # the refusal, so nothing assigned above is stored.
            await require_allowed_base_urls(
                new_extra,
                await _custom_base_url_keys(db, integration.provider),
                routed=integration.relay_group_id is not None,
            )

        # One settings read serves both halves below. Direction comes from the
        # ROW rather than the request: an update cannot change it
        # (``change_direction`` is a separate call, and it refuses while the
        # integration is bound), so the stored value is what the config being
        # saved will be read as.
        from app.a2a.client.credentials import configured_card_urls
        from app.a2a.providers import (
            ALL_A2A_PROVIDERS,
            get_settings,
            reconcile_configured_targets,
        )
        from app.models.a2a import DIRECTION_OUTBOUND

        _a2a_direction = None
        _previous_card_urls: list[str] = []
        if integration.provider in ALL_A2A_PROVIDERS:
            _settings_row = await get_settings(db, integration.id)
            _a2a_direction = (
                _settings_row.direction if _settings_row is not None else None
            )
            # A federation config is trust configuration the A2A auth path
            # consumes unattended; an edit that would break it (or blank it)
            # must fail in front of the operator, not as partner 401s later.
            _validate_a2a_federation_config(
                integration.provider, _a2a_direction, new_extra
            )
            # Read before the write below replaces the config it lives in.
            if _a2a_direction == DIRECTION_OUTBOUND:
                _previous_card_urls = configured_card_urls(integration)

        integration.extra_config_enc = encrypt_value(json.dumps(new_extra))
        if integration.auth_type == "none":
            integration.status = "connected"

        # The operator's edit, not a replacement: the table also holds the
        # agents discovery added, which a save must leave alone.
        if _a2a_direction == DIRECTION_OUTBOUND:
            # The partner credential may live in the config: a saved config is
            # a new credential to try (#12346).
            from app.a2a.client.credential_state import clear_on_credential_edit

            await clear_on_credential_edit(db, integration)
            await reconcile_configured_targets(
                db,
                integration.id,
                previous=_previous_card_urls,
                current=configured_card_urls(integration),
            )
    if body.authorize_url is not None:
        integration.authorize_url = body.authorize_url
    if body.token_url is not None:
        integration.token_url = body.token_url
    if body.scopes is not None:
        integration.scopes = body.scopes
    if integration.auth_type in _TOKEN_GRANT_AUTH_TYPES and (
        body.client_id is not None
        or body.client_secret is not None
        or body.token_url is not None
        or body.scopes is not None
        or body.api_key is not None
    ):
        # New client, login or scopes: the cached access token belongs to the
        # old configuration and is simply never looked up again (the cache
        # key covers every part of it).
        integration.status = _token_grant_status(integration)

    # Read before the auth-type switch and the share consent below, both of
    # which can reset the acknowledgement; compared again after them.
    was_oauth_share_acknowledged = integration.oauth_share_acknowledged

    # Auth type switching (e.g. api_key ↔ platform_managed)
    if body.auth_type is not None and body.auth_type != integration.auth_type:
        if integration.provider == OPENAPI_PROVIDER:
            # The credential type follows the spec's authentication option
            # (header / query / bearer / OAuth…); switching it alone would
            # leave the tools placing a credential of the wrong kind.
            raise HTTPException(
                status_code=400,
                detail=(
                    "An OpenAPI integration's authentication follows its spec's "
                    "authentication option. Create a new integration to use another one."
                ),
            )
        catalog = await _get_provider_catalog_entry(db, integration.provider) or {}
        supported = supported_auth_types(catalog)
        if supported and body.auth_type not in supported:
            raise HTTPException(
                status_code=400,
                detail=unsupported_auth_type_message(body.auth_type, integration.provider, supported),
            )
        # Read after the extra config above: a base_url sent with the switch counts.
        if body.auth_type == "platform_managed" and not platform_base_url(
            decrypt_json(integration.extra_config_enc) if integration.extra_config_enc else None
        ):
            raise HTTPException(status_code=400, detail=PLATFORM_MANAGED_NEEDS_BASE_URL)

        old_auth_type = integration.auth_type
        integration.auth_type = body.auth_type
        # A personal OAuth token must never survive a switch away from
        # "oauth": once auth_type != "oauth", the personal-token rule
        # (``integration_credentials``) stops applying to this row, so a
        # leftover token would become usable by anyone who can see it.
        if old_auth_type == "oauth" and body.auth_type != "oauth":
            integration.access_token_enc = None
            integration.refresh_token_enc = None
            integration.token_expires_at = None
            integration.oauth_share_acknowledged = False
            integration.oauth_token_owner_id = None
            integration.uses_admin_oauth_credentials = False
        if body.auth_type == "platform_managed":
            integration.api_key_enc = None
            integration.access_token_enc = None
            integration.refresh_token_enc = None
            integration.client_secret_enc = None
            integration.status = "connected"
        elif body.auth_type in _TOKEN_GRANT_AUTH_TYPES:
            integration.status = _token_grant_status(integration)
        elif old_auth_type == "platform_managed":
            integration.status = "not_connected"

    api_key_forgotten = forget_key_bound_elsewhere(
        integration, key_binding_before, new_key_given=bool(body.api_key)
    )

    # An OAuth integration left without an app (created before the admin
    # fallback reached its path, or switched back to OAuth) gets its provider's
    # admin OAuth app, as at creation; a request that sets the client id decides.
    if body.client_id is None:
        await repair_onto_admin_oauth_app(db, integration)

    # Group sharing
    await apply_group_share_fields(db, integration, body, partial=True)

    # Toggle company-managed status (admin only)
    if (
        body.is_company_managed is not None
        and body.is_company_managed != integration.is_company_managed
    ):
        if not user.is_admin:
            raise HTTPException(
                status_code=403, detail="Only admins can change company-managed status"
            )
        await apply_integration_company_managed(
            db,
            integration,
            body.is_company_managed,
            actor=user,
            keep_grants=wants_access_restriction(body, integration),
        )

    # Per-user sharing is applied before the consent below, so it sees
    # the final grant state: sharing with Alice is a share like sharing with
    # Alice's group.
    await apply_user_share_fields(db, integration, body, partial=True)

    # One decision over the fully-updated state, whichever field above shared
    # the integration: a shared OAuth integration shares its
    # connected account when this request says so, and an edit that makes no
    # choice keeps the baseline meaning. Not sharing it
    # means everyone else authorizes their own account; lending it
    # again, or a switch away from OAuth, disconnects those accounts.
    _sharing_changed = bool(_SHARING_FIELDS & body.model_fields_set)
    try:
        await apply_share_choice(
            db,
            integration,
            share_credentials=_requested_credential_share(
                body, integration, sharing_changed=_sharing_changed
            ),
            sharing_changed=_sharing_changed,
            may_consent=may_consent,
            implicit_consent=False,
        )
    except ShareConsentRefusedError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc

    # The dashboard reminder's dismissal is a decision about a share
    # that exists: once the acknowledgement is gone, so is the dismissal, or
    # sharing this integration again later would find its reminder already
    # muted. Before the owner transfer, so it is the owner who dismissed whose
    # list is cleaned.
    if was_oauth_share_acknowledged and not integration.oauth_share_acknowledged:
        await _forget_oauth_share_dismissal(db, integration, actor=user)

    # Owner transfer (admin only). The stored OAuth token stays and is not the
    # new owner's: record whose it is, so the page can say so.
    previous_owner_id = integration.user_id
    if await apply_owner_transfer(
        db,
        actor=user,
        entity=integration,
        new_owner_id=body.user_id,
        entity_type="integration",
        entity_name=integration.name,
        request=request,
    ):
        from app.services.integration_credentials import record_token_owner_on_transfer

        record_token_owner_on_transfer(integration, previous_owner_id)
        # Shared without its connected account: the stored account is
        # its owner's only, so the previous owner's goes and the new owner
        # connects their own.
        if is_per_user(integration):
            from app.services.integration_credentials import drop_previous_owner_token

            drop_previous_owner_token(integration)

    await db.flush()
    # Who can see it changed: the own accounts of the people who no longer can
    # go with it. The resolver checks visibility at use as well.
    if is_per_user(integration) and (
        _sharing_changed or "is_company_managed" in body.model_fields_set
    ):
        await prune_unreachable_user_credentials(db, integration)
    # Provider lifecycle seam: carry the edit over to what the provider set up
    # (OpenRouter: a replaced API key into the LLM provider it feeds).
    await dispatch_on_update(
        db,
        integration,
        IntegrationUpdate(actor=user, request=request, after_commit=after_commit),
    )
    # The agent accounts acting with it are checked again once the edit is
    # committed, not at their next hourly resync (#9903).
    from app.services.agent_accounts.listening import sync_integration_accounts

    after_commit.add(sync_integration_accounts, str(integration.id))
    catalog = PROVIDER_CATALOG.get(integration.provider, {})
    await log_audit(
        db,
        user=user,
        action="update",
        entity_type="integration",
        entity_id=integration.id,
        entity_name=integration.name,
        summary=f"Updated integration '{integration.name}'",
        request=request,
        details={
            "provider": integration.provider,
            "integration_type": catalog.get("integration_types", []),
            "is_company_managed": integration.is_company_managed,
            "tags": integration.tags or [],
            "relay_group_id": str(gid) if (gid := getattr(integration, "relay_group_id", None)) else None,
            **({"api_key_forgotten": True} if api_key_forgotten else {}),
        },
    )
    owner_name = None
    if integration.user_id:
        owner_row = await db.execute(
            select(User.full_name).where(User.id == integration.user_id)
        )
        owner_name = owner_row.scalar_one_or_none()
    token_owner_name = await _token_owner_name(db, integration)
    own_credentials = await _own_credential_fields(db, user, [integration], user_group_ids)
    # Read before the commit: a direction change of this edit is in the session.
    directions = await _a2a_directions(db, [integration])
    await db.commit()
    return _to_response(
        integration,
        can_manage=can_manage(user, integration, user_group_ids),
        can_authorize=can_authorize_integration(user, integration),
        owner_name=owner_name,
        token_owner_name=token_owner_name,
        secret_config_keys=await _custom_secret_config_keys(db, integration.provider),
        platform_approval=await platform_approval_by_provider(db),
        admin_client_ids=await _admin_oauth_client_ids(db, [integration]),
        vault_viewer=principal_from(user, user_group_ids),
        a2a_direction=directions.get(str(integration.id)),
        **own_credentials.get(str(integration.id), {}),
    )


# ---------------------------------------------------------------------------
# OAuth authorize (uses per-integration credentials from DB)
# ---------------------------------------------------------------------------


@router.get("/{integration_id}/authorize")
async def start_oauth(
    integration_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Start OAuth flow using the integration's own client_id/client_secret.

    The consent screen requests the provider catalog's current scopes, so a
    scope drift is resolved by re-authorizing. They are not stored here: the
    callback records them once the provider grants them, so an abandoned,
    denied or failed consent leaves the drift visible.
    """
    from app.integrations.oauth import build_authorize_url_for_integration

    user_group_ids = await get_user_group_ids(db, user.id)
    integration = await get_user_integration(
        db, user.id, integration_id, user_group_ids, is_admin=user.is_admin
    )

    # On an integration shared without its connected account,
    # everyone but its holder signs in for themselves: the callback stores the
    # token on their own credential, never on the integration. They must see
    # it as anyone in their place would (no admin bypass), as the callback and
    # the runs require.
    own_account = signs_in_own_account(user, integration)
    if own_account and not await may_connect_own_account(db, user, integration):
        raise HTTPException(
            status_code=403,
            detail="This integration is not shared with you: there is no account of your own to connect on it.",
        )

    # The stored token is the authorizing user's, and the credential rule
    # trusts ``user_id`` as its owner: a personal integration is only
    # authorized by its owner, a company-managed one by an admin (the callback
    # moves it to that admin). The same rule the responses serve as
    # ``can_authorize``, so the page never offers an Authorize refused here.
    if not own_account and not can_authorize_integration(user, integration):
        raise HTTPException(
            status_code=403,
            detail=(
                "Only admins can authorize company-managed integrations"
                if integration.is_company_managed
                else (
                    "Only this integration's owner can authorize it: the OAuth "
                    "connection would store your personal credentials under their "
                    "name. Connect your own integration instead."
                )
            ),
        )

    # A provider whose clients register themselves (catalog
    # ``oauth_registration``) gets its client now when it has none yet.
    if not integration.client_id_enc:
        from app.services.integration_oauth_registration import (
            IntegrationOAuthRegistrationError,
            register_integration_client,
        )

        try:
            await register_integration_client(db, integration, commit=False)
        except IntegrationOAuthRegistrationError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        await db.commit()
    if not integration.client_id_enc:
        raise HTTPException(
            status_code=400, detail="Integration has no OAuth client ID configured"
        )

    # Request the catalog definition's scopes. For GitHub Apps they are
    # intentionally empty (permissions are configured on the App itself), so
    # the authorize URL omits the scope parameter even when an older
    # authorization stored some, and GitHub issues a proper App
    # user-to-server token. Imported providers and OpenAPI integrations own
    # their scopes (they come from the imported spec, not from a catalog-wide
    # list), so their stored scopes are requested as they are.
    catalog_scopes: str | None = None
    if not integration_owns_oauth_scopes(integration.provider):
        catalog = PROVIDER_CATALOG.get(integration.provider, {})
        if integration.provider == "linkedin":
            catalog_scopes = _resolve_linkedin_scopes(catalog, integration.extra_config_enc)
        else:
            catalog_scopes = catalog.get("scopes", "")

    # An OpenAPI integration signs in the way its spec's option says: a
    # redirect returning a code or the token itself (implicit), or a code the
    # user enters on the API's own page (device authorization).
    implicit = False
    if integration.provider == OPENAPI_PROVIDER:
        spec_row = await get_spec(db, integration.id, runtime_only=True)
        option = AuthOption.from_dict(spec_row.auth_option) if spec_row else None
        if option is not None and option.kind == KIND_OAUTH2_DEVICE:
            device = await _start_device_sign_in(integration, user)
            return {"device_authorization": device.model_dump()}
        implicit = option is not None and option.kind == KIND_OAUTH2_IMPLICIT

    url = await build_authorize_url_for_integration(
        integration,
        str(user.id),
        scopes=catalog_scopes,
        implicit=implicit,
        own_account=own_account,
    )
    return {"authorize_url": url}


# ---------------------------------------------------------------------------
# Each person's own account on a per_user integration
# ---------------------------------------------------------------------------


async def _own_account_integration(db: AsyncSession, user: User, integration_id: str) -> Integration:
    """A ``per_user`` integration *user* can see, three-tier (no admin bypass)."""
    integration = await visible_per_user_integration(
        db, parse_path_id(integration_id, not_found_detail="Integration not found"), user.id
    )
    if integration is None:
        raise HTTPException(
            status_code=404,
            detail="No integration shared with you without its connected account has this id.",
        )
    return integration


@router.delete("/{integration_id}/my-credential", status_code=status.HTTP_204_NO_CONTENT)
async def delete_my_credential(
    integration_id: str,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Disconnect the caller's own account from a ``per_user`` integration."""
    integration = await _own_account_integration(db, user, integration_id)
    removed = await delete_user_credentials(db, integration.id, user_ids=[user.id])
    if removed:
        await log_audit(
            db,
            user=user,
            action="update",
            entity_type="integration",
            entity_id=integration.id,
            entity_name=integration.name,
            summary=f"Disconnected their own account from '{integration.name}'",
            request=request,
            details={"credential": "own_account"},
        )
    await db.commit()


async def _managed_per_user_integration(db: AsyncSession, user: User, integration_id: str) -> Integration:
    """A ``per_user`` integration *user* manages (``can_manage``)."""
    user_group_ids = await get_user_group_ids(db, user.id)
    integration = await get_user_integration(
        db, user.id, integration_id, user_group_ids, is_admin=user.is_admin
    )
    if not can_manage(user, integration, user_group_ids):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You don't have permission to manage this integration.",
        )
    if not is_per_user(integration):
        raise HTTPException(
            status_code=400,
            detail="This integration shares one connected account: nobody connects their own.",
        )
    return integration


@router.get("/{integration_id}/credentials", response_model=list[IntegrationUserCredentialItem])
async def list_user_credentials(
    integration_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Who connected their own account to a ``per_user`` integration (its managers).

    Names and states only: a person's account is theirs, and nobody else may
    read or use it.
    """
    from app.models.integration_user_credential import IntegrationUserCredential

    integration = await _managed_per_user_integration(db, user, integration_id)
    rows = (
        await db.execute(
            select(IntegrationUserCredential, User.full_name, User.email)
            .join(User, User.id == IntegrationUserCredential.user_id)
            .where(IntegrationUserCredential.integration_id == integration.id)
            .order_by(User.full_name, User.email)
        )
    ).all()
    return [
        IntegrationUserCredentialItem(
            **_credential_status(credential).model_dump(),
            user_id=str(credential.user_id),
            user_name=full_name,
            user_email=email,
        )
        for credential, full_name, email in rows
    ]


@router.delete(
    "/{integration_id}/credentials/{user_id}", status_code=status.HTTP_204_NO_CONTENT
)
async def delete_user_credential(
    integration_id: str,
    user_id: str,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Disconnect someone's own account from a ``per_user`` integration (its managers)."""
    integration = await _managed_per_user_integration(db, user, integration_id)
    target = parse_path_id(user_id, not_found_detail="No such connected account")
    removed = await delete_user_credentials(db, integration.id, user_ids=[target])
    if not removed:
        raise HTTPException(status_code=404, detail="No such connected account")
    await log_audit(
        db,
        user=user,
        action="update",
        entity_type="integration",
        entity_id=integration.id,
        entity_name=integration.name,
        summary=f"Disconnected a person's own account from '{integration.name}'",
        request=request,
        details={"credential": "own_account", "disconnected_user_id": str(target)},
    )
    await db.commit()


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------


@router.delete("/{integration_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_integration(
    integration_id: str,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    user_group_ids = await get_user_group_ids(db, user.id)
    integration = await get_user_integration(
        db, user.id, integration_id, user_group_ids, is_admin=user.is_admin
    )

    # Only admins can delete company-managed integrations
    require_admin_for_company_managed(
        integration, user, "Only admins can delete company-managed integrations"
    )

    # Group-shared integrations require config-tier permission (read_only members
    # can view but not delete). Mirrors the update route and can_manage().
    if not can_manage(user, integration, user_group_ids):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You don't have permission to delete this integration.",
        )

    # Built-in integrations cannot be deleted
    if integration.is_builtin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Built-in integrations cannot be deleted.",
        )

    catalog = PROVIDER_CATALOG.get(integration.provider, {})
    # Provider lifecycle seam: let the provider tear down what on_connect set
    # up (e.g. Nova's rule-sync source) before the row is deleted.
    await dispatch_on_disconnect(db, integration)
    await log_audit(
        db,
        user=user,
        action="delete",
        entity_type="integration",
        entity_id=integration.id,
        entity_name=integration.name,
        summary=f"Deleted integration '{integration.name}'",
        request=request,
        details={
            "provider": integration.provider,
            "integration_type": catalog.get("integration_types", []),
            "is_company_managed": integration.is_company_managed,
        },
    )

    # If this integration is linked to a platform registration, delete the
    # registration too so the platform disappears from the dashboard.
    from app.models.platform_registration import PlatformRegistration

    reg_result = await db.execute(
        select(PlatformRegistration).where(
            PlatformRegistration.integration_id == integration.id
        )
    )
    linked_reg = reg_result.scalar_one_or_none()
    if linked_reg:
        await db.delete(linked_reg)
        logger.info(
            "Deleted platform registration '%s' (linked to deleted integration '%s')",
            linked_reg.platform_identifier,
            integration.name,
        )

    # Purge stale references from all agents' integration_bindings (pre-filtered
    # by a text ``contains`` so only rows mentioning the id are loaded).
    iid = str(integration.id)
    agents_result = await db.execute(
        select(Agent).where(cast(Agent.integration_bindings, Text).contains(iid))
    )
    for agent in agents_result.scalars().all():
        bindings = agent.integration_bindings or {}
        if any(iid in ids for ids in bindings.values()):
            cleaned = {k: [v for v in ids if v != iid] for k, ids in bindings.items()}
            agent.integration_bindings = {k: v for k, v in cleaned.items() if v}

    # Same for the owner's closed OAuth-share reminders: a deleted id is
    # inert for display, but every one left behind counts against the cap of the
    # next dismissal, and the write-time filter can only drop it once a write
    # happens. A no-op unless this integration's reminder was closed.
    await _forget_oauth_share_dismissal(db, integration, actor=user)

    await db.delete(integration)
    # Commit before answering: the integrations list refetched right after the
    # 204 must not still hold the deleted row, and the get_db teardown only
    # commits after the response is sent.
    await db.commit()


async def _a2a_inbound_client_id(db: AsyncSession, integration_id: uuid.UUID) -> str | None:
    """The client id of an inbound integration's current credential, if any."""
    from app.models.oauth_client import OAuthClient

    return (
        await db.execute(
            select(OAuthClient.client_id)
            .where(OAuthClient.a2a_integration_id == integration_id)
            .order_by(OAuthClient.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


@router.post("/{integration_id}/a2a-credential")
async def regenerate_a2a_credential(
    integration_id: str,
    request: Request,
    rotate_client_id: bool = False,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, str]:
    """Generate, or rotate, the partner credential of an ``oauth_a2a`` integration.

    The secret is returned in clear HERE and nowhere else — it is stored only
    as a bcrypt hash. A regenerate rotates the SECRET and keeps the client id
    (#105), so the partner pastes one value; the old secret stops working
    immediately on every agent this integration is bound to. ``?rotate_client_id=true``
    is the explicit "Rotate client id and secret" action: both change, and the
    partner must paste both.

    Requires management authority over the integration, like any other edit.
    """
    from app.a2a.providers import OAUTH_A2A_PROVIDER
    from app.integrations.providers.oauth_a2a import mint_credential

    await require_chat_enabled(db)

    user_group_ids = await get_user_group_ids(db, user.id)
    integration = await get_user_integration(
        db, user.id, integration_id, user_group_ids, is_admin=user.is_admin
    )
    require_admin_for_company_managed(
        integration, user, "Only admins can edit company-managed integrations"
    )
    if not can_manage(user, integration, user_group_ids):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You don't have permission to edit this integration.",
        )
    if integration.provider != OAUTH_A2A_PROVIDER:
        raise HTTPException(
            status_code=422,
            detail="Only OAuth A2A integrations carry a partner credential.",
        )

    # Direction, not just provider. An OUTBOUND registration presents a
    # credential the PARTNER issued; minting here would be this platform
    # issuing itself one the partner has never heard of — and worse,
    # ``mint_credential`` writes its client id into ``extra_config``, which is
    # where the partner-issued one lives. One click would have silently
    # replaced a working outbound credential with a meaningless inbound one.
    from app.a2a.providers import get_settings
    from app.models.a2a import DIRECTION_INBOUND

    settings_row = await get_settings(db, integration.id)
    if settings_row is None or settings_row.direction != DIRECTION_INBOUND:
        raise HTTPException(
            status_code=422,
            detail=(
                "Only an inbound A2A integration mints a partner credential. "
                "An outbound one presents credentials the partner issued you — "
                "edit them in the integration's configuration."
            ),
        )

    previous_client_id = await _a2a_inbound_client_id(db, integration.id)
    client_id, client_secret = await mint_credential(db, integration, rotate_client_id=rotate_client_id)
    if previous_client_id is None:
        what = "generated the A2A partner credential"
    elif client_id == previous_client_id:
        what = "rotated the A2A partner secret (client id kept)"
    else:
        what = "rotated the A2A partner client id and secret"
    await log_audit(
        db,
        user=user,
        action="configure",
        entity_type="integration",
        entity_id=integration.id,
        entity_name=integration.name,
        summary=(
            f"{user.email} {what} for '{integration.name}'"
            + (" — the previous secret no longer works" if previous_client_id else "")
        ),
        details={
            "client_id": client_id,
            "previous_client_id": previous_client_id,
            "client_id_changed": previous_client_id is not None and client_id != previous_client_id,
        },
        request=request,
    )
    await db.commit()
    return {"client_id": client_id, "client_secret": client_secret}


# ---------------------------------------------------------------------------
# A2A partner activity (both A2A providers)
# ---------------------------------------------------------------------------


@router.get("/{integration_id}/a2a-activity")
async def get_a2a_federation_activity(
    integration_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, str | None]:
    """When THIS partner last submitted work through this integration.

    Attribution is by the calling registration recorded on each task, never
    merely by "some task on an agent this integration is bound to". Two
    partners may be bound to one agent, and an agent may carry history from
    before this integration existed — counting those would report a partner
    as active before it had ever connected.

    Derived at read time rather than from a counter maintained on the request
    path: ``tasks/get`` is a polling method, so a per-call write would be a
    hot-row update for a number nobody reads more than once a day.
    """
    from app.a2a.providers import A2A_PROVIDERS, partner_client_ids

    await require_chat_enabled(db)

    user_group_ids = await get_user_group_ids(db, user.id)
    integration = await get_user_integration(
        db, user.id, integration_id, user_group_ids, is_admin=user.is_admin
    )
    if integration.provider not in A2A_PROVIDERS:
        raise HTTPException(
            status_code=422,
            detail="Only A2A integrations have partner activity.",
        )

    from app.models.a2a import A2ATask

    client_ids = await partner_client_ids(db, integration)

    last = (
        await db.execute(
            select(func.max(A2ATask.created_at)).where(
                A2ATask.client_id.in_(client_ids)
            )
        )
    ).scalar_one_or_none()
    return {"last_activity_at": last.isoformat() if last else None}


# ---------------------------------------------------------------------------
# Test connection
# ---------------------------------------------------------------------------


@router.post("/{integration_id}/test")
async def test_integration(
    integration_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Quick connectivity test for an integration.

    Decrypts the stored credential and makes a lightweight API call to the
    provider to verify the token is still valid - through the integration's
    relay group when it is routed through one, so the test checks the
    path its tools take.
    """
    from app.services.relays import integration_egress_route

    user_group_ids = await get_user_group_ids(db, user.id)
    integration = await get_user_integration(
        db, user.id, integration_id, user_group_ids, is_admin=user.is_admin
    )
    # The live call runs as the credential's owner; a personal OAuth token is
    # theirs to exercise unless they shared it. On an integration whose
    # people each connect their own account, the caller tests theirs.
    holder = await usable_credential_holder(db, integration, user.id, is_admin=user.is_admin)
    route = await integration_egress_route(db, integration)
    with egress_route(route):
        result = await _test_integration(holder, user, user_group_ids, db)
    # The integration's own credential was tested: its page follows the result.
    # A person's own account (per-user mode) says nothing about the integration's.
    if not is_user_credential(holder):
        integration.connection_test_status = connection_test_outcome(result.get("status"))
        await db.commit()
    return result


CONNECTION_TEST_UNTESTED = "untested"
# The auth types whose typed secret is stored as given and only a test checks;
# OAuth and the token grants read their status from the token they hold.
_TYPED_SECRET_AUTH_TYPES = frozenset({"api_key", "bot_token"})


def typed_credential_untested(auth_type: str, *, api_key: str | None, client_secret: str | None) -> bool:
    """Whether a write stores a typed secret no test has checked yet (#11837)."""
    return auth_type in _TYPED_SECRET_AUTH_TYPES and bool(api_key or client_secret)


def connection_test_outcome(test_status: str | None) -> str:
    """The stored result of a connectivity test: ``ok`` (info counts: it works,
    with a caveat) or ``error``."""
    return "ok" if test_status in ("ok", "info") else "error"


async def _test_integration(
    integration: Integration,
    user: User,
    user_group_ids: list[str],
    db: AsyncSession,
) -> dict:
    # Providers that own their verdict through a registered connectivity probe
    # (app.integrations.connectivity) bypass every credential-shaped
    # precondition below: their health is internal state, and reporting *why*
    # they are unhealthy is exactly what the probe is for.
    probe_backed = has_connectivity_probe(integration.provider)

    # Credential-less providers have nothing to authenticate, so a stale
    # "not_connected" status (e.g. an integration created before the
    # extra_fields_optional catalog flag existed) must not block the probe —
    # a successful test self-heals the status below.
    if (
        integration.status not in ("connected", "configured")
        and integration.auth_type != "none"
        and not probe_backed
    ):
        return {"status": "error", "message": "Integration is not connected"}

    # CrowdStrike (Falcon and Next-Gen SIEM) uses an OAuth2 client-credentials
    # API key (client ID/secret in the dedicated columns, cloud region in
    # extra_config) — it stores no single bearer token, so verify by performing
    # a token exchange against its cloud. Next-Gen SIEM also probes that the
    # API client may read NGSIEM.
    if integration.provider in ("crowdstrike", "crowdstrike_ngsiem"):
        from app.integrations.providers.crowdstrike import resolve_base_url

        if integration.provider == "crowdstrike_ngsiem":
            from app.integrations.providers.crowdstrike_ngsiem import (
                validate_connection,
            )
        else:
            from app.integrations.providers.crowdstrike import validate_connection

        cs_extra: dict = {}
        if integration.extra_config_enc:
            with suppress(Exception):
                cs_extra = decrypt_json(integration.extra_config_enc) or {}
        cs_client_id = (
            decrypt_value(integration.client_id_enc)
            if integration.client_id_enc
            else ""
        )
        cs_client_secret = await reveal_field(integration, "client_secret_enc") or ""
        cs_base_url = resolve_base_url(
            (cs_extra.get("cloud") or cs_extra.get("base_url") or "").strip()
        )
        ok, message = await validate_connection(
            cs_base_url, cs_client_id, cs_client_secret
        )
        return {"status": "ok" if ok else "error", "message": message}

    # ThreatConnect signs every request with HMAC-SHA256 (Access ID + Secret
    # Key) — there is no bearer token, and the signature depends on the exact
    # request path, so verify with the provider's own signed owners lookup.
    if integration.provider == "threatconnect":
        from app.integrations.providers.threatconnect import (
            validate_connection as tc_validate,
        )

        tc_extra: dict = {}
        if integration.extra_config_enc:
            with suppress(Exception):
                tc_extra = decrypt_json(integration.extra_config_enc) or {}
        tc_secret = (await reveal_field(integration, "api_key_enc") or "").strip()
        ok, message = await tc_validate(
            (tc_extra.get("base_url") or "").strip(),
            (tc_extra.get("access_id") or "").strip(),
            tc_secret,
        )
        return {"status": "ok" if ok else "error", "message": message}

    # Cyware Intel Exchange signs every request with HMAC-SHA1 query params
    # (Access ID + Secret Key) — there is no bearer token, and the signature
    # expires within 30 seconds, so verify with the provider's own signed
    # ping probe.
    if integration.provider == "cyware":
        from app.integrations.providers.cyware import (
            validate_connection as cyware_validate,
        )

        cw_extra: dict = {}
        if integration.extra_config_enc:
            with suppress(Exception):
                cw_extra = decrypt_json(integration.extra_config_enc) or {}
        cw_secret = (await reveal_field(integration, "api_key_enc") or "").strip()
        ok, message = await cyware_validate(
            (cw_extra.get("base_url") or "").strip(),
            (cw_extra.get("access_id") or "").strip(),
            cw_secret,
        )
        return {"status": "ok" if ok else "error", "message": message}

    # ThreatQ uses an OAuth2 password grant (client ID + email + password in
    # extra_config / api_key_enc) — it stores no single bearer token, so verify
    # by performing the token exchange and a whoami call against the appliance.
    if integration.provider == "threatq":
        from app.integrations.providers.threatq import (
            validate_connection as threatq_validate,
        )

        tq_extra: dict = {}
        if integration.extra_config_enc:
            with suppress(Exception):
                tq_extra = decrypt_json(integration.extra_config_enc) or {}
        tq_password = (
            (await reveal_field(integration, "api_key_enc")) or ""
        )
        ok, message = await threatq_validate(
            (tq_extra.get("base_url") or "").strip().rstrip("/"),
            (tq_extra.get("client_id") or "").strip(),
            (tq_extra.get("email") or "").strip(),
            tq_password,
        )
        return {"status": "ok" if ok else "error", "message": message}

    # Picus authenticates by exchanging a stored refresh token for a
    # short-lived access token — it stores no single bearer token, so verify by
    # performing the token exchange against the manager (refresh token in
    # api_key_enc, base_url in extra_config).
    if integration.provider == "picus":
        from app.integrations.providers.picus import (
            validate_connection as picus_validate,
        )

        picus_extra: dict = {}
        if integration.extra_config_enc:
            with suppress(Exception):
                picus_extra = decrypt_json(integration.extra_config_enc) or {}
        picus_refresh = await reveal_field(integration, "api_key_enc") or ""
        ok, message = await picus_validate(
            (picus_extra.get("base_url") or "").strip(), picus_refresh
        )
        return {"status": "ok" if ok else "error", "message": message}

    # Cybersixgill authenticates with an OAuth2 client-credentials grant
    # (client ID + client secret exchanged for a short-lived bearer token) — it
    # stores no single bearer token, so verify by performing the token exchange
    # against the API host (client secret in api_key_enc, base_url + client ID
    # in extra_config).
    if integration.provider == "cybersixgill":
        from app.integrations.providers.cybersixgill import (
            validate_connection as cybersixgill_validate,
        )

        csg_extra: dict = {}
        if integration.extra_config_enc:
            with suppress(Exception):
                csg_extra = decrypt_json(integration.extra_config_enc) or {}
        csg_secret = await reveal_field(integration, "api_key_enc") or ""
        ok, message = await cybersixgill_validate(
            (csg_extra.get("base_url") or "").strip().rstrip("/"),
            (csg_extra.get("client_id") or "").strip(),
            csg_secret,
        )
        return {"status": "ok" if ok else "error", "message": message}

    # Wiz authenticates with an OAuth2 client-credentials grant (service
    # account client ID + client secret exchanged for a short-lived bearer
    # token) — it stores no single bearer token, so verify by performing the
    # token exchange plus a no-scope GraphQL probe against the tenant endpoint
    # (client secret in api_key_enc, API endpoint URL + client ID in
    # extra_config).
    if integration.provider == "wiz":
        from app.integrations.providers.wiz import (
            validate_connection as wiz_validate,
        )

        wiz_extra: dict = {}
        if integration.extra_config_enc:
            with suppress(Exception):
                wiz_extra = decrypt_json(integration.extra_config_enc) or {}
        wiz_secret = await reveal_field(integration, "api_key_enc") or ""
        ok, message = await wiz_validate(
            (wiz_extra.get("api_endpoint_url") or "").strip(),
            (wiz_extra.get("client_id") or "").strip(),
            wiz_secret,
        )
        return {"status": "ok" if ok else "error", "message": message}

    # Google SecOps authenticates via a service-account JWT-bearer flow — the
    # SA JSON lives in api_key_enc and the region/project/customer in
    # extra_config. Validate by minting an OAuth2 access token from the key.
    if integration.provider == "google_secops":
        from app.integrations.providers.google_secops import (
            validate_connection as secops_validate,
        )

        gs_extra: dict = {}
        if integration.extra_config_enc:
            with suppress(Exception):
                gs_extra = decrypt_json(integration.extra_config_enc) or {}
        gs_sa = await reveal_field(integration, "api_key_enc") or ""
        ok, message = await secops_validate(
            (gs_extra.get("region") or "").strip(),
            (gs_extra.get("project_id") or "").strip(),
            (gs_extra.get("customer_id") or "").strip(),
            gs_sa,
        )
        return {"status": "ok" if ok else "error", "message": message}

    # Amazon SES signs every request with AWS SigV4 — the Secret Access Key
    # lives in api_key_enc and the Access Key ID + region in extra_config, so
    # there is no single bearer token. Validate by calling GetAccount.
    if integration.provider == "ses":
        from app.integrations.providers.ses import (
            validate_connection as ses_validate,
        )

        ses_extra: dict = {}
        if integration.extra_config_enc:
            with suppress(Exception):
                ses_extra = decrypt_json(integration.extra_config_enc) or {}
        ses_secret = (
            (await reveal_field(integration, "api_key_enc")) or ""
        )
        ok, message = await ses_validate(
            (ses_extra.get("region") or "").strip(),
            (ses_extra.get("access_key_id") or "").strip(),
            ses_secret,
        )
        return {"status": "ok" if ok else "error", "message": message}

    # AWS Security (GuardDuty / Security Hub / CloudTrail) signs every request
    # with AWS SigV4 — the Secret Access Key lives in api_key_enc and the
    # Access Key ID + region in extra_config, so there is no single bearer
    # token. Validate by calling STS GetCallerIdentity (needs no permissions).
    if integration.provider == "aws_security":
        from app.integrations.providers.aws_security import (
            validate_connection as aws_security_validate,
        )

        aws_extra: dict = {}
        if integration.extra_config_enc:
            with suppress(Exception):
                aws_extra = decrypt_json(integration.extra_config_enc) or {}
        # Strip the secret - a pasted trailing newline/space would break SigV4.
        aws_secret = (
            (await reveal_field(integration, "api_key_enc")) or ""
        ).strip()
        ok, message = await aws_security_validate(
            (aws_extra.get("region") or "").strip(),
            (aws_extra.get("access_key_id") or "").strip(),
            aws_secret,
        )
        return {"status": "ok" if ok else "error", "message": message}

    # Aikido Security mints a bearer from an API client (client credentials)
    # on the workspace's regional host: the secret is in api_key_enc, the
    # region and client id in extra_config. Validate by reading the workspace.
    if integration.provider == "aikido":
        from app.integrations.providers.aikido import (
            token_from_config as aikido_token_from_config,
        )
        from app.integrations.providers.aikido import (
            validate_connection as aikido_validate,
        )

        aikido_extra: dict[str, object] = {}
        if integration.extra_config_enc:
            with suppress(Exception):
                decrypted = decrypt_json(integration.extra_config_enc)
                aikido_extra = decrypted if isinstance(decrypted, dict) else {}
        aikido_secret = (await reveal_field(integration, "api_key_enc")) or ""
        try:
            ok, message = await aikido_validate(aikido_token_from_config(aikido_secret, aikido_extra))
        except Exception:
            logger.debug("Aikido connectivity check failed", exc_info=True)
            ok, message = False, f"{integration.name} connection failed"
        return {"status": "ok" if ok else "error", "message": message}

    # DealHub: the API token is in api_key_enc, the instance host in
    # extra_config. Validate by reading the account's active users.
    if integration.provider == "dealhub":
        from app.integrations.providers.dealhub import (
            build_dealhub_token,
        )
        from app.integrations.providers.dealhub import (
            validate_connection as dealhub_validate,
        )

        dealhub_extra: dict[str, object] = {}
        if integration.extra_config_enc:
            with suppress(Exception):
                decrypted = decrypt_json(integration.extra_config_enc)
                dealhub_extra = decrypted if isinstance(decrypted, dict) else {}
        dealhub_host = dealhub_extra.get("instance_host")
        dealhub_secret = (await reveal_field(integration, "api_key_enc")) or ""
        try:
            ok, message = await dealhub_validate(
                build_dealhub_token(dealhub_host if isinstance(dealhub_host, str) else "", dealhub_secret)
            )
        except Exception:
            logger.debug("DealHub connectivity check failed", exc_info=True)
            ok, message = False, f"{integration.name} connection failed"
        return {"status": "ok" if ok else "error", "message": message}

    # Airwallex exchanges a client id and an API key for a 30-minute bearer:
    # the key is in api_key_enc, the environment, client id and login_as in
    # extra_config. Validate by reading the account.
    if integration.provider == "airwallex":
        from app.integrations.providers.airwallex import (
            token_from_config as airwallex_token_from_config,
        )
        from app.integrations.providers.airwallex import (
            validate_connection as airwallex_validate,
        )

        airwallex_extra: dict[str, object] = {}
        if integration.extra_config_enc:
            with suppress(Exception):
                decrypted = decrypt_json(integration.extra_config_enc)
                airwallex_extra = decrypted if isinstance(decrypted, dict) else {}
        airwallex_secret = (await reveal_field(integration, "api_key_enc")) or ""
        try:
            ok, message = await airwallex_validate(airwallex_token_from_config(airwallex_secret, airwallex_extra))
        except Exception:
            logger.debug("Airwallex connectivity check failed", exc_info=True)
            ok, message = False, f"{integration.name} connection failed"
        return {"status": "ok" if ok else "error", "message": message}

    # Rydoo mints a bearer from an API client (client credentials): the secret
    # is in api_key_enc, the client id in extra_config. Validate by listing the
    # company's branches (its legal entities).
    if integration.provider == "rydoo":
        from app.integrations.providers.rydoo import (
            token_from_config as rydoo_token_from_config,
        )
        from app.integrations.providers.rydoo import (
            validate_connection as rydoo_validate,
        )

        rydoo_extra: dict[str, object] = {}
        if integration.extra_config_enc:
            with suppress(Exception):
                decrypted = decrypt_json(integration.extra_config_enc)
                rydoo_extra = decrypted if isinstance(decrypted, dict) else {}
        rydoo_secret = (await reveal_field(integration, "api_key_enc")) or ""
        try:
            ok, message = await rydoo_validate(rydoo_token_from_config(rydoo_secret, rydoo_extra))
        except Exception:
            logger.debug("Rydoo connectivity check failed", exc_info=True)
            ok, message = False, f"{integration.name} connection failed"
        return {"status": "ok" if ok else "error", "message": message}

    # Spendesk mints a bearer from a Public API client (client credentials):
    # the secret is in api_key_enc, the environment and client id in
    # extra_config. Validate by listing the organisation's entities.
    if integration.provider == "spendesk":
        from app.integrations.providers.spendesk import (
            token_from_config as spendesk_token_from_config,
        )
        from app.integrations.providers.spendesk import (
            validate_connection as spendesk_validate,
        )

        spendesk_extra: dict[str, object] = {}
        if integration.extra_config_enc:
            with suppress(Exception):
                decrypted = decrypt_json(integration.extra_config_enc)
                spendesk_extra = decrypted if isinstance(decrypted, dict) else {}
        spendesk_secret = (await reveal_field(integration, "api_key_enc")) or ""
        try:
            ok, message = await spendesk_validate(spendesk_token_from_config(spendesk_secret, spendesk_extra))
        except Exception:
            logger.debug("Spendesk connectivity check failed", exc_info=True)
            ok, message = False, f"{integration.name} connection failed"
        return {"status": "ok" if ok else "error", "message": message}

    # OVHcloud signs each request (application key mode) or mints an OAuth2
    # bearer (service account mode): the secret is in api_key_enc, the rest of
    # the credential in extra_config. Validate with the provider's own read of
    # the account and, in key mode, of the credential's access rules.
    if integration.provider == "ovhcloud":
        from app.integrations.providers.ovhcloud import (
            token_from_config as ovh_token_from_config,
        )
        from app.integrations.providers.ovhcloud import (
            validate_connection as ovh_validate,
        )

        ovh_extra: dict = {}
        if integration.extra_config_enc:
            with suppress(Exception):
                ovh_extra = decrypt_json(integration.extra_config_enc) or {}
        ovh_secret = (await reveal_field(integration, "api_key_enc")) or ""
        try:
            ok, message = await ovh_validate(ovh_token_from_config(ovh_secret, ovh_extra))
        except Exception:
            logger.debug("OVHcloud connectivity check failed", exc_info=True)
            ok, message = False, f"{integration.name} connection failed"
        return {"status": "ok" if ok else "error", "message": message}

    # Defender XDR / Sentinel in app-only (API-key) mode store the service
    # principal credentials in the client_id/client_secret columns (and tenant
    # in extra_config) — there is no single bearer token to resolve below, so
    # the generic path would wrongly report "No credentials stored". Validate
    # by performing an Azure AD client-credentials token exchange.
    if integration.provider in ("defender", "sentinel") and (
        integration.auth_type == "api_key"
    ):
        az_extra: dict = {}
        if integration.extra_config_enc:
            with suppress(Exception):
                az_extra = decrypt_json(integration.extra_config_enc) or {}
        az_tenant = (az_extra.get("tenant_id") or "").strip()
        az_client_id = (
            decrypt_value(integration.client_id_enc)
            if integration.client_id_enc
            else (az_extra.get("client_id") or "").strip()
        )
        az_client_secret = (
            (await reveal_field(integration, "client_secret_enc"))
            or (az_extra.get("client_secret") or "").strip()
        )
        if not (az_tenant and az_client_id and az_client_secret):
            return {
                "status": "error",
                "message": (
                    "Missing tenant ID, client ID, or client secret for app-only "
                    "(API Key) authentication."
                ),
            }
        if integration.provider == "defender":
            from app.integrations.providers.defender import _GRAPH_SCOPE, _get_token

            az_token = await _get_token(
                az_tenant, az_client_id, az_client_secret, _GRAPH_SCOPE
            )
        else:
            from app.integrations.providers.sentinel import _get_mgmt_token

            az_token = await _get_mgmt_token(az_tenant, az_client_id, az_client_secret)
        if az_token:
            return {
                "status": "ok",
                "message": f"{integration.name} authenticated with Azure AD (app-only).",
            }
        return {
            "status": "error",
            "message": (
                "Azure AD authentication failed — check the tenant ID, client ID, "
                "and client secret, and ensure admin consent was granted."
            ),
        }

    # Microsoft Entra ID in app-only (API-key) mode stores the service
    # principal credentials in the client_id/client_secret columns (tenant in
    # extra_config) — there is no single bearer token to resolve below. Validate
    # by minting a Microsoft Graph token via the client-credentials grant.
    if integration.provider == "entra" and integration.auth_type == "api_key":
        from app.integrations.providers.entra import get_app_token

        entra_extra: dict = {}
        if integration.extra_config_enc:
            with suppress(Exception):
                entra_extra = decrypt_json(integration.extra_config_enc) or {}
        entra_tenant = (entra_extra.get("tenant_id") or "").strip()
        # Strip the decrypted credentials too (not just the extra_config
        # fallback): a client id/secret copy/pasted into the encrypted column
        # with a trailing newline would otherwise be sent verbatim to the
        # client-credentials token mint and fail the connection test spuriously.
        entra_client_id = (
            decrypt_value(integration.client_id_enc)
            if integration.client_id_enc
            else (entra_extra.get("client_id") or "")
        ).strip()
        entra_client_secret = (
            (await reveal_field(integration, "client_secret_enc"))
            or (entra_extra.get("client_secret") or "")
        ).strip()
        if not (entra_tenant and entra_client_id and entra_client_secret):
            return {
                "status": "error",
                "message": (
                    "Missing tenant ID, client ID, or client secret for app-only "
                    "(API Key) authentication."
                ),
            }
        entra_token = await get_app_token(
            entra_tenant, entra_client_id, entra_client_secret
        )
        if entra_token:
            return {
                "status": "ok",
                "message": f"{integration.name} authenticated with Microsoft Graph (app-only).",
            }
        return {
            "status": "error",
            "message": (
                "Microsoft Graph authentication failed — check the tenant ID, "
                "client ID, and client secret, and ensure admin consent was granted "
                "for the required application permissions."
            ),
        }

    # DocuSign: the provider's compound credential (environment, account, the
    # OAuth access token or the JWT Grant key material) is what its tools run
    # on, so the test assembles it from the row the gate above cleared (the
    # token funnel's resolver stays private to the funnel) and hands it to the
    # provider's validate_connection (test_provider_connectivity's branch).
    if integration.provider == "docusign":
        from app.integrations.providers.docusign import compound_credential

        docusign_extra: dict = {}
        if integration.extra_config_enc:
            with suppress(Exception):
                docusign_extra = decrypt_json(integration.extra_config_enc) or {}
        if integration.auth_type == "oauth":
            docusign_secret = (
                decrypt_value(integration.access_token_enc) if integration.access_token_enc else ""
            ).strip()
            docusign_token = compound_credential(docusign_secret, docusign_extra, jwt_grant=False)
        else:
            docusign_secret = ((await reveal_field(integration, "api_key_enc")) or "").strip()
            docusign_token = compound_credential(
                docusign_secret,
                docusign_extra,
                jwt_grant=True,
                integration_key=decrypt_value(integration.client_id_enc) if integration.client_id_enc else "",
            )
        if not docusign_secret or not docusign_token:
            return {
                "status": "error",
                "message": (
                    "Missing credentials: authorize the integration (OAuth), or set the RSA "
                    "private key, the integration key and the user ID (JWT Grant)."
                ),
            }
        return await test_provider_connectivity(
            "docusign", docusign_token, {}, integration.name, db=db, integration=integration
        )

    # NetSuite: the compound credential (account, mode, identifiers and the
    # secret of the API Key field) is assembled from the row the gate above
    # cleared and handed to the provider's validate_connection.
    if integration.provider == "netsuite":
        from app.integrations.providers.netsuite import compound_credential as netsuite_credential

        netsuite_extra: dict = {}
        if integration.extra_config_enc:
            with suppress(Exception):
                netsuite_extra = decrypt_json(integration.extra_config_enc) or {}
        netsuite_token = netsuite_credential(
            ((await reveal_field(integration, "api_key_enc")) or "").strip(), netsuite_extra
        )
        if not netsuite_token:
            return {
                "status": "error",
                "message": (
                    "Missing credentials: set the account ID and, for OAuth 2.0 machine-to-machine, the client "
                    "ID, the certificate ID and the certificate's private key; for Token-Based Authentication, "
                    "the consumer key and secret, the token ID and the token secret."
                ),
            }
        return await test_provider_connectivity(
            "netsuite", netsuite_token, {}, integration.name, db=db, integration=integration
        )

    # Microsoft Purview in app-only (API-key) mode stores the service principal
    # credentials in the client_id/client_secret columns (tenant + optional
    # account_name in extra_config) — there is no single bearer token to resolve
    # below. Validate by minting a Microsoft Graph token via the
    # client-credentials grant (the compliance pillar audience).
    if integration.provider == "purview" and integration.auth_type == "api_key":
        from app.integrations.providers.purview import get_app_graph_token

        purview_extra: dict = {}
        if integration.extra_config_enc:
            with suppress(Exception):
                purview_extra = decrypt_json(integration.extra_config_enc) or {}
        purview_tenant = (purview_extra.get("tenant_id") or "").strip()
        purview_client_id = (
            decrypt_value(integration.client_id_enc)
            if integration.client_id_enc
            else (purview_extra.get("client_id") or "")
        ).strip()
        purview_client_secret = (
            (await reveal_field(integration, "client_secret_enc"))
            or (purview_extra.get("client_secret") or "")
        ).strip()
        if not (purview_tenant and purview_client_id and purview_client_secret):
            return {
                "status": "error",
                "message": (
                    "Missing tenant ID, client ID, or client secret for app-only "
                    "(API Key) authentication."
                ),
            }
        purview_token = await get_app_graph_token(
            purview_tenant, purview_client_id, purview_client_secret
        )
        if purview_token:
            return {
                "status": "ok",
                "message": f"{integration.name} authenticated with Microsoft Graph (app-only).",
            }
        return {
            "status": "error",
            "message": (
                "Microsoft Graph authentication failed — check the tenant ID, "
                "client ID, and client secret, and ensure admin consent was granted "
                "for the required application permissions."
            ),
        }

    # Resolve the credential to test with
    token: str | None = None
    if integration.access_token_enc:
        token = decrypt_value(integration.access_token_enc)
    elif integration.api_key_enc:
        token = await reveal_field(integration, "api_key_enc") or ""
    elif (
        integration.provider in ("ransomware_live", "vuln_intel", "xtm_hub")
        and integration.auth_type == "none"
    ):
        # Free (unauthenticated) tier: no credential is stored. Surface the
        # no-auth sentinel so the connectivity test can probe the free API
        # (Ransomware.live free v2; Vulnerability Intelligence keyless NVD;
        # the XTM Hub's public catalog).
        token = "__no_auth__"  # noqa: S105 - tier marker, not a secret
    elif integration.provider == "rss" and integration.extra_config_enc:
        # RSS is credential-less — the feed URLs act as the "token". Keyed on
        # the provider (not ``auth_type == "none"``) because other
        # credential-less providers (mitre_attack, filigran_ai) don't store
        # feed_urls, and a bad/non-JSON extra_config must not 500 the test.
        with suppress(Exception):
            extra = decrypt_json(integration.extra_config_enc) or {}
            token = str(extra.get("feed_urls", "") or "").strip() or None

    # MITRE ATT&CK and ATLAS are credential-less (free public datasets) —
    # synthesize a placeholder token so the connectivity test proceeds; their
    # provider branches in test_provider_connectivity only read extra_config
    # (source_url).
    if not token and integration.provider in ("mitre_attack", "mitre_atlas"):
        token = "public"  # noqa: S105 — placeholder, not a credential

    # Normalize the credential once: strip surrounding whitespace/newlines and
    # treat a blank result as missing. A copy/pasted token often carries a
    # trailing newline, which would make ``Authorization: Bearer {token}`` an
    # illegal header value and raise an opaque httpx LocalProtocolError; a
    # whitespace-only value is treated as missing so the test returns a clear
    # "re-authorize" message instead of surfacing that raw exception text.
    if token is not None:
        token = token.strip() or None

    if not token:
        # A probe-backed provider decides for itself whether a missing
        # credential matters — most have none to store. Skipping credential
        # resolution here is what makes them testable at all; without it they
        # never reach their probe and every test reports the misleading
        # "No credentials stored for this integration".
        if probe_backed:
            pass
        # platform_managed integrations don't store credentials — generate a
        # short-lived EdDSA JWT on behalf of the current user for the test.
        elif integration.auth_type == "platform_managed":
            from app.core.platform_identity import platform_token_audience
            from app.services.integration_tools import _generate_platform_jwt

            _base_url = ""
            if integration.extra_config_enc:
                try:
                    _xt = decrypt_json(integration.extra_config_enc) or {}
                    _base_url = (_xt.get("base_url") or "").rstrip("/")
                except Exception:
                    logger.debug(
                        "Failed to parse extra_config for platform-managed integration %s",
                        integration.id,
                        exc_info=True,
                    )
            if not _base_url:
                return {
                    "status": "error",
                    "message": "Integration is missing base_url in extra_config — cannot generate platform JWT.",
                }
            _audience_url = await platform_token_audience(db, integration.id, _base_url)
            token = await _generate_platform_jwt(db, str(user.id), _audience_url)
            if not token:
                return {
                    "status": "error",
                    "message": "Failed to generate platform JWT for connectivity test.",
                }
        elif integration.provider.startswith("custom:") and integration.auth_type == "none":
            # Credential-less custom bundle — the bundle-declared HTTP probe
            # (or the "appears configured" fallback) runs without auth, so a
            # missing token must not short-circuit the test.
            pass
        else:
            return {
                "status": "error",
                "message": "No credentials stored for this integration",
            }

    # Parse extra_config for providers that need it (OpenCTI, Metabase, etc.).
    # Narrow to a dict at the source: corrupted data could decode to a
    # non-object, and downstream code (custom probe, provider checks) calls
    # ``extra.get(...)`` unconditionally.
    extra: dict = {}
    if integration.extra_config_enc:
        with suppress(Exception):
            decoded = json.loads(decrypt_value(integration.extra_config_enc))
            if isinstance(decoded, dict):
                extra = decoded

    # Custom providers: use the bundle's declared test_connection spec instead
    # of the static checks table in test_provider_connectivity (which doesn't
    # know about dynamically imported providers).
    if integration.provider.startswith("custom:"):
        custom_def_result = await db.execute(
            select(CustomIntegrationDefinition).where(
                CustomIntegrationDefinition.provider_key == integration.provider
            )
        )
        custom_def = custom_def_result.scalar_one_or_none()
        tc = (custom_def.metadata_json or {}).get("test_connection") if custom_def else None
        if tc and isinstance(tc, dict) and tc.get("mode") == "http":
            tc_path = tc.get("path") or "/health"
            if not isinstance(tc_path, str) or not tc_path.strip():
                tc_path = "/health"
            tc_path = tc_path.strip()
            if not tc_path.startswith("/"):
                tc_path = "/" + tc_path

            # Same defensive narrowing as tc_path: rows persisted before
            # parse-time validation may carry a non-string / unsafe method,
            # and a connectivity probe must only ever use safe methods.
            tc_method_raw = tc.get("method")
            tc_method = (
                tc_method_raw.strip().upper()
                if isinstance(tc_method_raw, str)
                else "GET"
            )
            if tc_method not in ("GET", "POST", "HEAD"):
                tc_method = "GET"
            # Strip before rstrip so a pasted base_url with surrounding
            # whitespace normalises cleanly (and a whitespace-only value is
            # treated as missing instead of producing an invalid URL).
            base_url_raw = extra.get("base_url", "")
            base_url = (
                base_url_raw.strip().rstrip("/") if isinstance(base_url_raw, str) else ""
            )
            if not base_url:
                return {
                    "status": "error",
                    "message": "Missing base_url in integration configuration.",
                }
            probe_url = f"{base_url}{tc_path}"

            # Honor the bundle-declared auth style for the probe (mirrors
            # _resolve_auth_header in the declarative executor): explicit
            # test_connection.auth wins, otherwise fall back to bearer unless
            # the integration is credential-less.
            auth_headers: dict[str, str] = {}
            tc_auth = tc.get("auth")
            if isinstance(tc_auth, dict):
                probe_auth_type = (tc_auth.get("type") or "bearer").lower()
            else:
                probe_auth_type = "none" if integration.auth_type == "none" else "bearer"

            if probe_auth_type == "none" or not token or token == "__no_auth__":  # noqa: S105 - tier marker, not a secret
                pass
            elif probe_auth_type == "header" and isinstance(tc_auth, dict):
                # Same header-syntax guard as validate_execution_spec: an
                # invalid header name / control-char prefix would make httpx
                # raise a bare ValueError inside the broad handler below and
                # surface a generic "connection failed" that hides the real
                # configuration error.
                from app.services.declarative_http_executor import (
                    _HEADER_NAME_RE,
                    _HEADER_VALUE_SAFE_RE,
                )

                header_name = tc_auth.get("header")
                if not isinstance(header_name, str) or not header_name.strip():
                    header_name = "Authorization"
                prefix = tc_auth.get("prefix")
                if not isinstance(prefix, str):
                    prefix = "Bearer"
                if not _HEADER_NAME_RE.fullmatch(header_name) or not _HEADER_VALUE_SAFE_RE.fullmatch(prefix):
                    return {
                        "status": "error",
                        "message": (
                            "Invalid test_connection.auth in the bundle: the "
                            "header name must use RFC 9110 token characters and "
                            "the prefix must be printable ASCII."
                        ),
                    }
                auth_headers[header_name] = f"{prefix} {token}" if prefix else token
            else:
                auth_headers["Authorization"] = f"Bearer {token}"
            try:
                # Only the status code is used: ask for the raw bytes (nothing
                # to decompress) and never read the body (see the stream
                # below). ``update`` is case-insensitive, so a bundle auth
                # header spelled ``user-agent`` replaces the default instead of
                # being sent beside it; it is built in here because a stored
                # credential httpx cannot encode raises, and must end as the
                # generic error below, not a 500.
                headers = httpx.Headers(
                    {"Accept-Encoding": "identity", "User-Agent": PLATFORM_USER_AGENT}
                )
                headers.update(auth_headers)
                # The bundle's endpoint is a third party: no HTTP(S)_PROXY /
                # SSL_CERT_* from the environment, no redirect followed, one
                # deadline for the whole exchange, and the body is never
                # pulled off the socket - leaving the stream closes the
                # response unread (the bundle tool calls get the same).
                # The base URL is user-supplied: the connection goes only to
                # an address the base URL policy admits, the one checked;
                # a refusal ends as "connection failed" below.
                async with (
                    asyncio.timeout(_CUSTOM_PROBE_TIMEOUT_S),
                    egress_client(
                        timeout=_CUSTOM_PROBE_TIMEOUT_S,
                        trust_env=False,
                        follow_redirects=False,
                        transport=probe_transport(),
                    ) as _client,
                    _client.stream(tc_method, probe_url, headers=headers) as resp,
                ):
                    status_code = resp.status_code
                    is_redirect = resp.has_redirect_location
                if 200 <= status_code < 300:
                    result = {"status": "ok", "message": f"{integration.name} is connected and responding"}
                elif is_redirect:
                    result = {
                        "status": "error",
                        "message": (
                            f"{integration.name} answered with a redirect (HTTP {status_code}), "
                            "which is not followed — check the base URL in the "
                            "integration configuration."
                        ),
                    }
                elif status_code in (401, 403):
                    result = {
                        "status": "error",
                        "message": "Authentication failed — the API key may be invalid or revoked.",
                    }
                else:
                    result = {"status": "error", "message": f"{integration.name} returned HTTP {status_code}"}
            except Exception:
                logger.debug(
                    "Custom-provider connectivity probe failed for %s (%s)",
                    integration.name,
                    integration.provider,
                    exc_info=True,
                )
                result = {"status": "error", "message": f"{integration.name} connection failed"}
        else:
            # No HTTP test_connection or mode=none — just confirm configured
            result = {"status": "ok", "message": f"{integration.name} appears configured"}
    else:
        # *integration* is the token holder: the probe and the manage
        # check read the integration itself, and act as the tester.
        result = await test_provider_connectivity(
            integration.provider,
            token,
            extra,
            integration.name,
            db=db,
            integration=integration_of(integration),
            caller_can_manage=can_manage(user, integration_of(integration), user_group_ids),
            acting_user_id=str(user.id),
        )
    # Self-heal: a credential-less integration that just proved its data
    # source reachable IS connected — persist it so the UI badge and the
    # tool-test gate stop reporting a stale "not_connected". "info" counts: a
    # probe reporting degraded means the integration works, with a caveat.
    if (
        result.get("status") in ("ok", "info")
        and integration.auth_type == "none"
        and integration.status != "connected"
    ):
        integration.status = "connected"
        await db.commit()
    # Operator escape hatch: a successful Notion connectivity test
    # clears every cross-run stale-reference counter for this integration,
    # so an admin who just repaired sharing resets the agent-side quarantine
    # in one click instead of waiting for the recheck window.
    if result.get("status") == "ok" and integration.provider == "notion":
        from app.integrations.providers.notion import clear_stale_ref_counters

        await clear_stale_ref_counters(str(integration_of(integration).id))
    return {
        "status": result.get("status", "error"),
        "message": result.get("message", "Connection test completed."),
    }


@router.get("/{integration_id}/tools")
async def list_integration_tools(
    integration_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List available tools for an integration (based on its provider).

    Returns the tool definitions the agent would receive when this integration
    is bound — useful for the frontend tool-scoping UI.
    """
    user_group_ids = await get_user_group_ids(db, user.id)
    integration = await get_user_integration(
        db, user.id, integration_id, user_group_ids, is_admin=user.is_admin
    )

    from app.services.integration_tools import PROVIDER_TOOLS as _BUILDERS
    from app.tools.base import CallableToolExecutor

    # OpenAPI integrations list THEIR spec's selected tools, exactly as an
    # agent receives them (credential parameters removed, same runtime view).
    if integration.provider == OPENAPI_PROVIDER:
        spec = await get_spec(db, integration.id, runtime_only=True)
        tools = effective_tools(spec.active_tools, spec.auth_option) if spec else []
        return {"tools": summarize_tools(tools)}

    builder = _BUILDERS.get(integration.provider)
    if not builder:
        if integration.provider.startswith("custom:"):
            custom_result = await db.execute(
                select(CustomIntegrationDefinition).where(
                    CustomIntegrationDefinition.provider_key == integration.provider
                )
            )
            custom_row = custom_result.scalar_one_or_none()
            if custom_row:
                custom_tools = _custom_tools_from_metadata(custom_row.metadata_json)
                return {
                    "tools": [
                        {
                            "name": tool["name"],
                            "description": tool["description"],
                            "input_schema": tool["input_schema"],
                        }
                        for tool in custom_tools
                    ]
                }
        return {"tools": []}

    # Build tools with a dummy token / executor just to get definitions.
    # Compound-token providers need their specific token shape or their
    # build_tools() early-returns — build_dummy_token() is the single
    # source of truth.
    dummy_exec = CallableToolExecutor()
    try:
        defs = builder(
            build_dummy_token(integration.provider),
            dummy_exec,
            refresh_fn=lambda: None,
        )
    except Exception:
        logger.debug(
            "Failed to build tools for integration %s (%s)",
            integration.id,
            integration.provider,
            exc_info=True,
        )
        return {"tools": []}

    return {
        "tools": [
            {
                "name": d.name,
                "description": d.description,
                "input_schema": d.input_schema,
            }
            for d in defs
        ]
    }


# ---------------------------------------------------------------------------
# Test a single integration tool
# ---------------------------------------------------------------------------


@router.post("/{integration_id}/test-tool")
async def test_integration_tool(
    integration_id: str,
    body: dict,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Test a single integration tool by executing it with provided arguments."""
    user_group_ids = await get_user_group_ids(db, user.id)
    integration = await get_user_integration(
        db, user.id, integration_id, user_group_ids, is_admin=user.is_admin
    )
    # The caller's own account on a per_user integration.
    await usable_credential_holder(db, integration, user.id, is_admin=user.is_admin)

    tool_name: str = body.get("tool_name", "")
    arguments: dict = body.get("arguments", {})

    if not tool_name:
        return {"status": "error", "message": "tool_name is required"}

    if integration.status not in ("connected", "configured"):
        return {"status": "error", "message": "Integration is not connected"}

    from app.services.integration_tools import BindingBasis, ToolBindings, build_integration_tools
    from app.tools.base import ToolCall

    bindings = {integration.provider: str(integration.id)}
    try:
        defs, executor = await build_integration_tools(
            db,
            ToolBindings(bindings, basis=BindingBasis.CALLER_CHECKED, reason="the route's integration access check"),
            context={
                "source": "tool_test",
                "integration": integration.name,
                "credential_user_id": str(user.id),
            },
        )
    except Exception as exc:
        logger.error("Failed to build integration tools: %s", format_exception_message(exc), exc_info=True)
        return {
            "status": "error",
            "message": "Failed to build tools. Check the integration connection and configuration.",
        }

    if tool_name not in executor.tool_names:
        available = [d.name for d in defs]
        return {
            "status": "error",
            "message": f"Tool '{tool_name}' not found. Available: {available}",
        }

    # Mirror the agent loop's CORE-2 pre-dispatch validation (agent_loop.py):
    # the loop validates every tool call's arguments against the tool's
    # input_schema BEFORE executing, but this tool-test path dispatched
    # straight to the executor - so schema-violating arguments (a JSON boolean
    # for a string flag, a string for an integer, a missing required field)
    # reached the provider handler raw and surfaced as an opaque exception or
    # silent misbehaviour, one handler at a time. Validating here makes the
    # test endpoint a faithful simulation of the agent path and returns
    # controlled feedback for EVERY integration tool, not just the one being
    # patched this week.
    from app.tools.agent_loop import validate_tool_arguments

    schema = next((d.input_schema for d in defs if d.name == tool_name), None)
    validation_error = validate_tool_arguments(tool_name, arguments, schema)
    if validation_error:
        return {"status": "error", "message": validation_error}

    call = ToolCall(id="test", name=tool_name, arguments=arguments)
    start = time.monotonic()
    try:
        result = await executor.execute_tool(call)
        duration_ms = int((time.monotonic() - start) * 1000)
        if result.is_error:
            return {
                "status": "error",
                "message": result.content,
                "duration_ms": duration_ms,
            }
        return {"status": "ok", "output": result.content, "duration_ms": duration_ms}
    except Exception as exc:
        duration_ms = int((time.monotonic() - start) * 1000)
        logger.error("Integration tool execution failed: %s", format_exception_message(exc), exc_info=True)
        return {
            "status": "error",
            "message": "Tool execution failed. Check the integration connection.",
            "duration_ms": duration_ms,
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolve_linkedin_scopes(catalog: dict, extra_config_enc: str | None) -> str:
    """Resolve LinkedIn scopes based on account_type in extra_config.

    Personal accounts only need basic scopes (Sign In + Share on LinkedIn).
    Page accounts additionally need organization-level scopes (Community Management API).
    """
    account_type = "personal"
    if extra_config_enc:
        try:
            extra = decrypt_json(extra_config_enc) or {}
            account_type = extra.get("account_type", "personal")
        except Exception:
            logger.debug(
                "Failed to decrypt extra_config for LinkedIn scope resolution",
                exc_info=True,
            )
    if account_type == "page":
        return catalog.get("scopes_page", catalog.get("scopes", ""))
    return catalog.get("scopes_personal", catalog.get("scopes", ""))


async def _token_owner_name(db: AsyncSession, integration: Integration) -> str | None:
    """The name of whoever connected the stored token, when it is not the owner."""
    from app.services.integration_credentials import oauth_token_owner_differs

    if not oauth_token_owner_differs(integration):
        return None
    return (
        await db.execute(
            select(User.full_name).where(User.id == integration.oauth_token_owner_id)
        )
    ).scalar_one_or_none()


_UNREADABLE = UNREADABLE_ADMIN_CLIENT_ID


async def _admin_oauth_client_ids(db: AsyncSession, rows) -> dict[str, object]:
    """The current OAuth default client id of each provider an unrecorded row uses.

    Loaded only for rows ``i4234credsrc1`` left NULL, in one query. A default
    whose client id cannot be decrypted maps to ``_UNREADABLE``; a provider
    without a default is absent.
    """
    providers = {r.provider for r in rows if r.uses_admin_oauth_credentials is None}
    if not providers:
        return {}
    result = await db.execute(
        select(OAuthDefault).where(OAuthDefault.provider.in_(providers))
    )
    client_ids: dict[str, object] = {}
    for default in result.scalars().all():
        try:
            client_ids[default.provider] = decrypt_value(default.client_id_enc) or _UNREADABLE
        except Exception:
            client_ids[default.provider] = _UNREADABLE
    return client_ids


def _effective_uses_admin_oauth_credentials(
    r: Integration, admin_client_ids: dict[str, object] | None
) -> bool | None:
    """The recorded value, else - for a row the migration left NULL - its stored
    client id compared with the provider's current OAuth default: the rule the
    provider-level webhook, the LinkedIn notifications and the Slack signing
    secrets deliver by (``integration_state.uses_admin_oauth_app``). ``None``
    only when that cannot be read, which the frontend answers with the
    provider-wide flag.
    """
    if admin_client_ids is None:
        return r.uses_admin_oauth_credentials
    return uses_admin_oauth_app(
        r.uses_admin_oauth_credentials, r.client_id_enc, admin_client_ids.get(r.provider)
    )


def _signature_setting(r: Integration) -> IntegrationSignatureSetting | None:
    """The Email signature setting of an integration whose tools send email; None otherwise."""
    from app.services.email_signatures import email_sender_providers

    if r.provider not in email_sender_providers():
        return None
    return IntegrationSignatureSetting(
        enabled=getattr(r, "signature_enabled", True) is not False,
        integration_id=str(r.signature_integration_id) if getattr(r, "signature_integration_id", None) else None,
    )


async def _a2a_directions(db: AsyncSession, integrations: Sequence[Integration]) -> dict[str, str]:
    """The direction of each A2A integration among *integrations*, by id: one query, none without one."""
    from app.a2a.providers import ALL_A2A_PROVIDERS, directions_of

    ids = [r.id for r in integrations if r.provider in ALL_A2A_PROVIDERS]
    return await directions_of(db, ids) if ids else {}


def _to_response(
    r: Integration,
    *,
    can_manage: bool,
    can_authorize: bool,
    owner_name: str | None = None,
    token_owner_name: str | None = None,
    secret_config_keys: frozenset[str] = frozenset(),
    platform_approval: tuple[frozenset[str], frozenset[str]] = (
        frozenset(),
        frozenset(),
    ),
    admin_client_ids: dict[str, object] | None = None,
    uses_own_credential: bool = False,
    my_credential: UserCredentialStatus | None = None,
    own_account_status: str | None = None,
    connected_user_count: int | None = None,
    vault_viewer: VaultPrincipal | None = None,
    a2a_direction: str | None = None,
) -> IntegrationResponse:
    from app.services.integration_credentials import oauth_token_owner_differs

    catalog = PROVIDER_CATALOG.get(r.provider, {})
    connected_by_other = oauth_token_owner_differs(r)

    # For a registered Suite platform the stored ``requires_approval`` column is
    # inert — the platform policy is what the gate reads — so report the
    # policy. Passed in rather than queried here so a listing resolves it once
    # for the whole page instead of once per row.
    platform_governed, platform_exempt = platform_approval
    managed_by_platform = r.provider in platform_governed
    requires_approval = (
        r.provider not in platform_exempt if managed_by_platform
        else bool(r.requires_approval)
    )

    # LinkedIn: required_scopes depend on the selected account type
    if r.provider == "linkedin":
        required_scopes = _resolve_linkedin_scopes(catalog, r.extra_config_enc)
    else:
        required_scopes = catalog.get("scopes") or None

    # Someone acting with their own account sees its state, never the
    # holder's: its status and token, and no scopes (theirs are not recorded).
    status, has_access_token, scopes = r.status, bool(r.access_token_enc), r.scopes
    # Why and since when (#1665): the holder's verdict, never shown for someone's own account.
    status_reason, status_reason_at = current_reason(r)
    # What the stored token was granted, and what it asked for and did not get.
    granted = getattr(r, "granted_scopes", None) if has_access_token else None
    granted_scopes = None if granted is None else split_scopes(granted)
    missing = missing_scopes(scopes or required_scopes, granted, catalog.get("optional_scopes"))
    if uses_own_credential:
        status = own_account_status or "configured"
        has_access_token = bool(my_credential and my_credential.has_access_token)
        scopes = required_scopes = None
        granted_scopes = missing = None
        status_reason = status_reason_at = None

    return IntegrationResponse(
        id=str(r.id),
        user_id=str(r.user_id) if r.user_id else None,
        owner_name=owner_name,
        provider=r.provider,
        auth_type=r.auth_type,
        status=status,
        connection_test_status=getattr(r, "connection_test_status", None),
        status_reason=status_reason,
        status_reason_at=status_reason_at,
        name=r.name,
        tool_slug=r.tool_slug,
        description=r.description,
        is_company_managed=r.is_company_managed,
        is_builtin=r.is_builtin,
        is_custom=r.provider.startswith("custom:"),
        shared_group_ids=r.shared_group_ids,
        shared_group_permissions=r.shared_group_permissions,
        shared_user_ids=r.shared_user_ids,
        shared_user_permissions=r.shared_user_permissions,
        access_restricted=r.access_restricted,
        can_manage=can_manage,
        can_authorize=can_authorize,
        is_registration_only=is_registration_only_provider(r.provider),
        a2a_direction=a2a_direction,
        tags=r.tags or [],
        requires_approval=requires_approval,
        approval_managed_by_platform=managed_by_platform,
        signature=_signature_setting(r),
        scopes=scopes,
        required_scopes=required_scopes,
        granted_scopes=granted_scopes,
        missing_scopes=missing,
        authorize_url=r.authorize_url,
        token_url=r.token_url,
        has_client_id=bool(r.client_id_enc),
        has_client_secret=bool(r.client_secret_enc),
        has_access_token=has_access_token,
        has_api_key=bool(r.api_key_enc),
        has_extra_config=bool(r.extra_config_enc),
        oauth_share_acknowledged=r.oauth_share_acknowledged,
        oauth_connected_by_other=connected_by_other,
        oauth_token_owner_name=token_owner_name if connected_by_other else None,
        oauth_token_owner_id=(
            str(r.oauth_token_owner_id)
            if connected_by_other and getattr(r, "oauth_token_owner_id", None)
            else None
        ),
        credential_mode=r.credential_mode or "shared",
        uses_own_credential=uses_own_credential,
        my_credential=my_credential,
        connected_user_count=connected_user_count,
        uses_admin_oauth_credentials=_effective_uses_admin_oauth_credentials(
            r, admin_client_ids
        ),
        extra_config=_decrypt_extra_config(
            r.extra_config_enc, secret_keys=secret_config_keys
        ),
        vault_secrets=vault_status(r, vault_viewer if can_manage else None),
        scope_config=r.scope_config,
        relay_group_id=(
            str(r.relay_group_id) if getattr(r, "relay_group_id", None) else None
        ),
        call_count=r.call_count or 0,
        last_called_at=r.last_called_at,
        created_at=r.created_at,
        updated_at=r.updated_at,
    )


def _own_account_status(credential: Any) -> str:
    """The status a person acting with their own account sees: their
    credential's, else ``configured`` - set up, waiting for them to authorize,
    as an OAuth integration is before its first sign-in (still offered to
    their agents' bindings)."""
    if credential is not None and (credential.status == "reauthorize" or credential_connected(credential)):
        return credential.status
    return "configured"


def _credential_status(credential: Any) -> UserCredentialStatus:
    """A person's own account as the API shows it: its state, never its secret."""
    return UserCredentialStatus(
        status=credential.status,
        has_access_token=bool(credential.access_token_enc),
        token_expires_at=credential.token_expires_at,
        connected_at=credential.created_at,
        updated_at=credential.updated_at,
    )


async def _own_credential_fields(
    db: AsyncSession,
    user: User,
    integrations: Sequence[Integration],
    user_group_ids: list[str] | None,
) -> dict[str, dict[str, Any]]:
    """``_to_response`` keywords for the ``per_user`` integrations among *integrations*.

    For the requesting user: whether they act with their own account there,
    and their own account's state; for those who manage it, how many people
    connected one. Two queries for a whole listing.
    """
    per_user = [r for r in integrations if is_per_user(r)]
    if not per_user:
        return {}
    own = await user_credentials_by_integration(db, [r.id for r in per_user], user.id)
    # Where the person may connect their own account: their sign-in is for
    # themselves and they see it three-tier (``may_connect_own_account``), one
    # query for the whole listing.
    from app.services.integration_credentials import visible_integration_ids

    seen = await visible_integration_ids(db, user.id, [r.id for r in per_user])
    managed = {str(r.id) for r in per_user if can_manage(user, r, user_group_ids)}
    counts = await connected_user_counts(db, managed)
    fields: dict[str, dict[str, Any]] = {}
    for r in per_user:
        key = str(r.id)
        credential = own.get(key)
        fields[key] = {
            "uses_own_credential": signs_in_own_account(user, r) and key in seen,
            "my_credential": _credential_status(credential) if credential is not None else None,
            "own_account_status": _own_account_status(credential),
            "connected_user_count": counts.get(key, 0) if key in managed else None,
        }
    return fields


def _decrypt_extra_config(
    enc: str | None, *, secret_keys: frozenset[str] = frozenset()
) -> dict | None:
    """Decrypt extra_config for display. Returns None if empty or decryption fails.

    Returns full JSON values (including booleans/arrays) for typed provider
    settings rendered by custom integration bundles.

    Secret values are replaced with ``EXTRA_CONFIG_MASK``: keys matching the
    conventional name heuristic (secret/token/key/password) plus any
    ``secret_keys`` declared by a custom bundle schema. The update endpoint
    understands the mask as "keep the stored value", so the read -> edit ->
    save roundtrip is loss-free.
    """
    if not enc:
        return None
    try:
        raw = decrypt_json(enc)
        if not raw:
            return None
        return {
            k: (
                EXTRA_CONFIG_MASK
                if v not in (None, "")
                and (k in secret_keys or _is_sensitive_setting_name(k))
                else v
            )
            for k, v in raw.items()
        }
    except Exception:
        return None


async def masked_stored_extra_config(db: AsyncSession, integration: Integration) -> dict:
    """The stored extra config of *integration*, secrets masked as the REST responses mask them.

    The read-back of an update outside this module (the agent builder's
    ``update_integration``): the sensitive-name heuristic plus the secrets a
    custom bundle's schema declares.
    """
    secret_keys = await _custom_secret_config_keys(db, integration.provider)
    return _decrypt_extra_config(integration.extra_config_enc, secret_keys=secret_keys) or {}
