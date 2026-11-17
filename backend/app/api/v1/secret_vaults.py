# SPDX-License-Identifier: Apache-2.0

"""Secret vault providers: connected, granted and tested in the UI, picked from in every secret field.

Two routers:

* :data:`admin_router` (``/api/v1/settings/secret-vaults``, administrators):
  list every provider - the ones connected here and the ones declared in the
  environment (``SECRET_VAULT_<NAME>_*``, read-only) - with its status and
  where it is used; create, edit, enable, disable, test and delete one. Every
  change and every test is audited. A provider's credentials are write-only:
  a response says which ones are set, never their value.
* :data:`router` (``/api/v1/secret-vaults``, any user): the secret picker -
  the providers the caller may use, the secret names under a prefix, the key
  names of one secret, and a check of one reference. Each answer is filtered
  by the caller's grants, rate-limited and logged; none ever holds a value or
  a credential.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, require_admin
from app.api.v1.helpers import parse_path_id
from app.core.database import get_db
from app.core.rate_limit import enforce_rate_limit
from app.core.secret_refs import (
    PROVIDER_NAME_PATTERN,
    SecretReference,
    SecretReferenceSyntaxError,
    parse_secret_reference,
)
from app.models.secret_vault_provider import SecretVaultProvider
from app.models.user import User
from app.services.audit import log_audit
from app.services.vault import reference_access_problem, reference_problem
from app.services.vault.base import MAX_PAGE_SIZE, ListedSecret, entry_names
from app.services.vault.checks import ConnectionReport, test_connection
from app.services.vault.config import (
    DATABASE_CREDENTIALS,
    DATABASE_SETTINGS,
    SOURCE_DATABASE,
    SOURCE_ENVIRONMENT,
    PathPrefix,
    VaultConfigError,
    VaultPrincipal,
    VaultProviderConfig,
    parse_grants,
)
from app.services.vault.errors import REASON_DESCRIPTIONS, VaultReadError
from app.services.vault.resolver import get_resolver, select_value
from app.services.vault.store import (
    config_of_row,
    encrypt_credentials,
    refresh_vault_providers,
    stored_credentials,
    vault_principal,
)
from app.services.vault.usage import ReferenceUse, provider_uses, recent_reference_uses

logger = logging.getLogger(__name__)

admin_router = APIRouter(dependencies=[Depends(require_admin)])
router = APIRouter()

ProviderType = Literal["hashicorp", "aws", "gcp", "azure", "onepassword"]
ProviderStatus = Literal["connected", "failing", "disabled", "not_checked", "misconfigured", "environment", "shadowed"]

#: The picker's budget per user: browsing a folder tree is a click per request.
PICKER_RATE_LIMIT = 120
PICKER_RATE_WINDOW_SECONDS = 60
#: An administrator's budget of connection tests: each one reaches the vault and writes an audit entry.
TEST_RATE_LIMIT = 20
TEST_RATE_WINDOW_SECONDS = 60


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class VaultGrantModel(BaseModel):
    principal: Literal["everyone", "group", "user"]
    id: str | None = None
    paths: list[str] = []
    #: The group's or user's name, for the form (responses only).
    label: str | None = None


class CheckStepModel(BaseModel):
    step: str
    ok: bool | None
    reason: str | None = None
    description: str | None = None
    detail: str | None = None


class ConnectionReportModel(BaseModel):
    ok: bool
    steps: list[CheckStepModel]
    mounts: list[dict[str, str]] | None = None
    checked_at: datetime


class ReferenceUseModel(BaseModel):
    kind: str
    id: str
    name: str
    field: str


class SecretVaultProviderOut(BaseModel):
    """A provider as the Settings page shows it: never a credential."""

    id: str | None
    name: str
    display_name: str
    type: str
    source: Literal["database", "environment"]
    enabled: bool
    status: ProviderStatus
    status_reason: str | None = None
    status_description: str | None = None
    status_detail: str | None = None
    last_check_at: datetime | None = None
    #: The settings that open nothing, by lowercase name (an environment provider: what may be shown of it).
    settings: dict[str, str] = {}
    #: Which credentials are stored, by lowercase name: ``True`` when set. Never the value.
    credentials_set: dict[str, bool] = {}
    grants: list[VaultGrantModel] = []
    browsable: bool = False
    cache_seconds: int | None = None
    #: An environment provider's policy, read-only.
    writers: str | None = None
    paths: list[str] = []
    usage_count: int = 0
    created_at: datetime | None = None
    updated_at: datetime | None = None
    updated_by_name: str | None = None


class SecretVaultListResponse(BaseModel):
    providers: list[SecretVaultProviderOut]
    default_cache_seconds: int


class SecretVaultProviderCreate(BaseModel):
    name: str = Field(pattern=f"^{PROVIDER_NAME_PATTERN}$")
    display_name: str = Field(min_length=1, max_length=200)
    type: ProviderType
    settings: dict[str, str | None] = {}
    #: Write-only: a value sets the credential.
    credentials: dict[str, str | None] = {}
    enabled: bool = True
    grants: list[VaultGrantModel] = []
    browsable: bool = False
    cache_seconds: int | None = Field(default=None, ge=0, le=86400)


class SecretVaultProviderUpdate(BaseModel):
    name: str | None = Field(default=None, pattern=f"^{PROVIDER_NAME_PATTERN}$")
    display_name: str | None = Field(default=None, min_length=1, max_length=200)
    #: Replaces every setting that opens nothing.
    settings: dict[str, str | None] | None = None
    #: Write-only: a value replaces the credential, ``null`` clears it, a missing key keeps it.
    credentials: dict[str, str | None] | None = None
    enabled: bool | None = None
    grants: list[VaultGrantModel] | None = None
    browsable: bool | None = None
    cache_seconds: int | None = Field(default=None, ge=0, le=86400)
    #: ``cache_seconds`` is cleared (back to the platform default) when this is true.
    clear_cache_seconds: bool = False


class SecretVaultDraftTest(SecretVaultProviderCreate):
    #: Testing an edit: the stored credentials of this provider fill the ones the draft does not send.
    provider_id: str | None = None
    name: str = Field(default="draft", pattern=f"^{PROVIDER_NAME_PATTERN}$")
    display_name: str = Field(default="draft", min_length=1, max_length=200)


class PickerProvider(BaseModel):
    name: str
    display_name: str
    type: str
    source: str
    #: Whether the caller may list secret names (otherwise they type the path).
    browsable: bool
    #: The path prefixes the caller may use; ``None``: every path.
    paths: list[str] | None


class PickerEntry(BaseModel):
    name: str
    path: str
    folder: bool


class PickerUnavailable(BaseModel):
    reason: str
    description: str


class PickerListing(BaseModel):
    entries: list[PickerEntry]
    next_page_token: str | None = None
    #: The vault refused the listing (the picker falls back to typing a path).
    unavailable: PickerUnavailable | None = None


class PickerKeys(BaseModel):
    #: The entry names of the secret (a KV secret, a JSON object); empty for a single value.
    keys: list[str]
    #: How a key is shown when it is an id (a 1Password field's label), by key.
    labels: dict[str, str] = {}
    single_value: bool
    unavailable: PickerUnavailable | None = None


class CheckRequest(BaseModel):
    reference: str = Field(min_length=1, max_length=1024)


class CheckResponse(BaseModel):
    ok: bool
    reason: str | None = None
    description: str | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _lower_keys(values: Any) -> dict[str, str]:
    """Submitted settings by lowercase name, an empty or ``null`` value left out."""
    return {str(key).lower(): str(value) for key, value in (values or {}).items() if value not in (None, "")}


def _shown_settings(row: SecretVaultProvider) -> dict[str, str]:
    allowed = {setting.lower() for setting in DATABASE_SETTINGS.get(row.type, frozenset())}
    return {key: str(value) for key, value in (row.settings or {}).items() if key in allowed and value is not None}


def _status_of_row(row: SecretVaultProvider) -> tuple[ProviderStatus, str | None, str | None, str | None]:
    resolver = get_resolver()
    if row.name in resolver.shadowed:
        return "shadowed", None, resolver.shadowed[row.name], None
    if not row.enabled:
        return "disabled", None, None, None
    if row.name in resolver.errors:
        return "misconfigured", "misconfigured", REASON_DESCRIPTIONS["misconfigured"], resolver.errors[row.name]
    if row.last_check_ok is None:
        return "not_checked", None, None, None
    if row.last_check_ok:
        return "connected", None, None, None
    reason = row.last_check_reason or "unreachable"
    return "failing", reason, REASON_DESCRIPTIONS.get(reason), row.last_check_detail


async def _grant_labels(db: AsyncSession, grants: list[dict[str, Any]]) -> dict[tuple[str, str], str]:
    from app.models.group import Group

    group_ids = [g.get("id") for g in grants if g.get("principal") == "group" and g.get("id")]
    user_ids = [g.get("id") for g in grants if g.get("principal") == "user" and g.get("id")]
    labels: dict[tuple[str, str], str] = {}
    valid_groups = [gid for gid in group_ids if _is_uuid(gid)]
    valid_users = [uid for uid in user_ids if _is_uuid(uid)]
    if valid_groups:
        for gid, name in (await db.execute(select(Group.id, Group.name).where(Group.id.in_(valid_groups)))).all():
            labels[("group", str(gid))] = name
    if valid_users:
        rows = await db.execute(select(User.id, User.full_name, User.email).where(User.id.in_(valid_users)))
        for uid, full_name, email in rows.all():
            labels[("user", str(uid))] = full_name or email
    return labels


def _is_uuid(value: Any) -> bool:
    try:
        uuid.UUID(str(value))
    except ValueError:
        return False
    return True


async def _row_out(
    db: AsyncSession,
    row: SecretVaultProvider,
    usage: dict[str, list[ReferenceUse]],
    users: dict[str, str],
    labels: dict[tuple[str, str], str] | None = None,
) -> SecretVaultProviderOut:
    """One provider as the page shows it; *labels* holds its grants' names when the caller read them for a whole list."""
    state, reason, description, detail = _status_of_row(row)
    credentials = stored_credentials(row)
    if labels is None:
        labels = await _grant_labels(db, list(row.grants or []))
    return SecretVaultProviderOut(
        id=str(row.id),
        name=row.name,
        display_name=row.display_name,
        type=row.type,
        source=SOURCE_DATABASE,
        enabled=bool(row.enabled),
        status=state,
        status_reason=reason,
        status_description=description,
        status_detail=detail,
        last_check_at=row.last_check_at,
        settings=_shown_settings(row),
        credentials_set={
            setting.lower(): bool(credentials.get(setting)) for setting in sorted(DATABASE_CREDENTIALS.get(row.type, ()))
        },
        grants=[
            VaultGrantModel(
                principal=grant.get("principal", "everyone"),
                id=grant.get("id"),
                paths=list(grant.get("paths") or []),
                label=labels.get((str(grant.get("principal")), str(grant.get("id")))),
            )
            for grant in row.grants or []
        ],
        browsable=bool(row.browsable),
        cache_seconds=row.cache_seconds,
        usage_count=len(usage.get(row.name, [])),
        created_at=row.created_at,
        updated_at=row.updated_at,
        updated_by_name=users.get(str(row.updated_by_id)) if row.updated_by_id else None,
    )


def _environment_out(name: str, usage: dict[str, list[ReferenceUse]]) -> SecretVaultProviderOut:
    resolver = get_resolver()
    provider = resolver.providers.get(name)
    details = provider.describe() if provider is not None else {}
    policy = resolver.policy(name)
    error = resolver.errors.get(name)
    config = resolver.config_of(name)
    return SecretVaultProviderOut(
        id=None,
        name=name,
        display_name=name,
        type=details.get("type") or (config.type if config is not None else ""),
        source=SOURCE_ENVIRONMENT,
        enabled=True,
        status="misconfigured" if error else "environment",
        status_reason="misconfigured" if error else None,
        status_description=REASON_DESCRIPTIONS["misconfigured"] if error else None,
        status_detail=error,
        settings={key: value for key, value in details.items() if key not in ("name", "type", "writers", "paths")},
        writers=getattr(policy, "writers", None),
        paths=["/".join(prefix) for prefix in getattr(policy, "paths", ())],
        usage_count=len(usage.get(name, [])),
    )


async def _user_names(db: AsyncSession, rows: list[SecretVaultProvider]) -> dict[str, str]:
    ids = {row.updated_by_id for row in rows if row.updated_by_id}
    if not ids:
        return {}
    found = await db.execute(select(User.id, User.full_name, User.email).where(User.id.in_(ids)))
    return {str(uid): full_name or email for uid, full_name, email in found.all()}


async def _load_row(db: AsyncSession, provider_id: str, *, lock: bool = False) -> SecretVaultProvider:
    row_id = parse_path_id(provider_id, not_found_detail="Vault provider not found")
    stmt = select(SecretVaultProvider).where(SecretVaultProvider.id == row_id)
    if lock:
        stmt = stmt.with_for_update()
    row = (await db.execute(stmt)).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Vault provider not found")
    return row


async def _checked_grants(db: AsyncSession, grants: list[VaultGrantModel]) -> list[dict[str, Any]]:
    """The grants as stored: valid, and naming groups and users that exist (422 otherwise)."""
    raw = [{"principal": grant.principal, "id": grant.id, "paths": grant.paths} for grant in grants]
    try:
        parsed = parse_grants(raw)
    except VaultConfigError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    labels = await _grant_labels(db, raw)
    for index, grant in enumerate(parsed):
        if grant.principal != "everyone" and (grant.principal, grant.id) not in labels:
            raise HTTPException(status_code=422, detail=f"grants[{index}].id names no existing {grant.principal}")
    return [grant.to_json() for grant in parsed]


async def _require_allowed_addresses(config: VaultProviderConfig) -> None:
    """422 when the provider names an address a vault connected in the UI may not be reached at.

    Checked on save and before every test, since a name may resolve elsewhere
    by then; every connection is checked again (``app.services.vault.destinations``).
    """
    from app.services.vault.destinations import VaultAddressRefusedError, assert_provider_addresses_allowed

    try:
        await assert_provider_addresses_allowed(config)
    except VaultAddressRefusedError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None


def _address_changed(row: SecretVaultProvider, settings: dict[str, str]) -> bool:
    """Whether an edit changes one of the addresses the provider sends requests to."""
    from app.services.vault.destinations import ADDRESS_SETTINGS

    stored = dict(row.settings or {})
    return any(settings.get(key.lower()) != stored.get(key.lower()) for key in ADDRESS_SETTINGS.get(row.type, ()))


def _validated_config(
    *,
    name: str,
    type: str,
    settings: dict[str, str],
    credentials: dict[str, str],
    enabled: bool,
    grants: list[dict[str, Any]],
    browsable: bool,
    cache_seconds: int | None,
    display_name: str,
) -> VaultProviderConfig:
    """The provider these values declare, built once to check it (422 naming what to fix)."""
    from app.services.vault.config import database_config
    from app.services.vault.resolver import build_provider

    unknown = sorted(set(settings) - {s.lower() for s in DATABASE_SETTINGS[type]})
    if unknown:
        raise HTTPException(status_code=422, detail=f"settings: {', '.join(unknown)} is not a setting of a {type} provider")
    unknown = sorted(set(credentials) - {s.lower() for s in DATABASE_CREDENTIALS[type]})
    if unknown:
        raise HTTPException(status_code=422, detail=f"credentials: {', '.join(unknown)} is not a credential of a {type} provider")
    try:
        config = database_config(
            name=name,
            type=type,
            settings=settings,
            credentials=credentials,
            display_name=display_name,
            enabled=enabled,
            grants=grants,
            browsable=browsable,
            cache_seconds=cache_seconds,
        )
        build_provider(config)
    except VaultConfigError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    return config


def _name_problem(name: str, *, current: str | None = None) -> str | None:
    resolver = get_resolver()
    if name != current and resolver.sources.get(name) == SOURCE_ENVIRONMENT:
        return f"'{name}' is the name of a provider declared in the environment (SECRET_VAULT_{name.upper()}_*)"
    return None


def _report_model(report: ConnectionReport, at: datetime) -> ConnectionReportModel:
    return ConnectionReportModel(
        ok=report.ok,
        steps=[CheckStepModel(**step.__dict__) for step in report.steps],
        mounts=report.mounts,
        checked_at=at,
    )


def _audit_details(row_or_body: Any) -> dict[str, Any]:
    """What an audit entry records of a provider: its shape, never a credential."""
    return {
        "name": getattr(row_or_body, "name", None),
        "type": getattr(row_or_body, "type", None),
        "enabled": getattr(row_or_body, "enabled", None),
        "browsable": getattr(row_or_body, "browsable", None),
        "grants": len(getattr(row_or_body, "grants", None) or []),
    }


# ---------------------------------------------------------------------------
# Administrators: Settings > Access and security > Secret vaults
# ---------------------------------------------------------------------------


@admin_router.get("", response_model=SecretVaultListResponse)
async def list_secret_vaults(db: AsyncSession = Depends(get_db)) -> SecretVaultListResponse:
    """Every provider: connected here first, then declared in the environment, with status and use count."""
    from app.core.config import settings as app_settings

    rows = list((await db.execute(select(SecretVaultProvider).order_by(SecretVaultProvider.display_name))).scalars().all())
    usage = (await recent_reference_uses(db)).by_provider()
    users = await _user_names(db, rows)
    # The grants of every provider named in one query each for groups and users, not one per provider.
    labels = await _grant_labels(db, [grant for row in rows for grant in row.grants or []])
    providers = [await _row_out(db, row, usage, users, labels) for row in rows]
    resolver = get_resolver()
    environment = sorted(name for name, source in resolver.sources.items() if source == SOURCE_ENVIRONMENT)
    providers.extend(_environment_out(name, usage) for name in environment)
    return SecretVaultListResponse(providers=providers, default_cache_seconds=app_settings.secret_vault_cache_seconds)


@admin_router.get("/usage/{name}", response_model=list[ReferenceUseModel])
async def secret_vault_usage(name: str, db: AsyncSession = Depends(get_db)) -> list[ReferenceUseModel]:
    """The secret fields referencing provider *name*: the entity and the field, never the path."""
    return [
        ReferenceUseModel(kind=use.kind, id=use.id, name=use.name, field=use.field)
        for use in await provider_uses(db, name)
    ]


@admin_router.post("", response_model=SecretVaultProviderOut, status_code=status.HTTP_201_CREATED)
async def create_secret_vault(
    body: SecretVaultProviderCreate,
    request: Request,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> SecretVaultProviderOut:
    if problem := _name_problem(body.name):
        raise HTTPException(status_code=409, detail=problem)
    existing = (await db.execute(select(SecretVaultProvider.id).where(SecretVaultProvider.name == body.name))).first()
    if existing is not None:
        raise HTTPException(status_code=409, detail=f"A vault provider named '{body.name}' already exists")
    grants = await _checked_grants(db, body.grants)
    settings = _lower_keys(body.settings)
    credentials = _lower_keys(body.credentials)
    config = _validated_config(
        name=body.name,
        type=body.type,
        settings=settings,
        credentials=credentials,
        enabled=body.enabled,
        grants=grants,
        browsable=body.browsable,
        cache_seconds=body.cache_seconds,
        display_name=body.display_name,
    )
    await _require_allowed_addresses(config)
    row = SecretVaultProvider(
        name=body.name,
        display_name=body.display_name.strip() or body.name,
        type=body.type,
        settings=settings,
        credentials_enc=encrypt_credentials({key.upper(): value for key, value in credentials.items()}),
        enabled=body.enabled,
        grants=grants,
        browsable=body.browsable,
        cache_seconds=body.cache_seconds,
        created_by_id=user.id,
        updated_by_id=user.id,
    )
    db.add(row)
    await db.flush()
    await log_audit(
        db,
        user=user,
        action="create",
        entity_type="secret_vault",
        entity_id=row.id,
        entity_name=row.display_name,
        summary=f"Connected the {row.type} vault provider '{row.name}'",
        details=_audit_details(row),
        request=request,
    )
    await db.commit()
    await refresh_vault_providers()
    usage: dict[str, list[ReferenceUse]] = {}
    return await _row_out(db, row, usage, {str(user.id): user.full_name or user.email})


@admin_router.patch("/{provider_id}", response_model=SecretVaultProviderOut)
async def update_secret_vault(
    provider_id: str,
    body: SecretVaultProviderUpdate,
    request: Request,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> SecretVaultProviderOut:
    row = await _load_row(db, provider_id, lock=True)
    before_name, before_enabled = row.name, row.enabled
    if body.name is not None and body.name != row.name:
        if problem := _name_problem(body.name, current=row.name):
            raise HTTPException(status_code=409, detail=problem)
        taken = (await db.execute(select(SecretVaultProvider.id).where(SecretVaultProvider.name == body.name))).first()
        if taken is not None:
            raise HTTPException(status_code=409, detail=f"A vault provider named '{body.name}' already exists")
        uses = await provider_uses(db, row.name)
        if uses:
            raise HTTPException(
                status_code=409,
                detail={
                    "message": f"'{row.name}' is the name {len(uses)} stored reference(s) use: it cannot change",
                    "uses": [ReferenceUseModel(kind=u.kind, id=u.id, name=u.name, field=u.field).model_dump() for u in uses],
                },
            )
    credentials = stored_credentials(row)
    if body.credentials is not None:
        for key, value in body.credentials.items():
            setting = str(key).upper()
            if value is None or value == "":
                credentials.pop(setting, None)
            else:
                credentials[setting] = value
    settings = _lower_keys(body.settings) if body.settings is not None else dict(row.settings or {})
    grants = await _checked_grants(db, body.grants) if body.grants is not None else list(row.grants or [])
    name = body.name or row.name
    display_name = (body.display_name or row.display_name).strip() or name
    enabled = row.enabled if body.enabled is None else body.enabled
    browsable = row.browsable if body.browsable is None else body.browsable
    cache_seconds = None if body.clear_cache_seconds else (body.cache_seconds if body.cache_seconds is not None else row.cache_seconds)
    config = _validated_config(
        name=name,
        type=row.type,
        settings=settings,
        credentials={key.lower(): value for key, value in credentials.items()},
        enabled=enabled,
        grants=grants,
        browsable=browsable,
        cache_seconds=cache_seconds,
        display_name=display_name,
    )
    if _address_changed(row, settings):
        # Only a new address is judged here: a provider whose name resolves elsewhere since can still be disabled.
        await _require_allowed_addresses(config)
    opening_changed = settings != dict(row.settings or {}) or credentials != stored_credentials(row)
    row.name = name
    row.display_name = display_name
    row.settings = settings
    row.credentials_enc = encrypt_credentials(credentials)
    row.enabled = enabled
    row.grants = grants
    row.browsable = browsable
    row.cache_seconds = cache_seconds
    row.updated_by_id = user.id
    row.updated_at = datetime.now(UTC)
    if opening_changed:
        # A test of the old settings says nothing of the new ones.
        row.last_check_at = row.last_check_ok = row.last_check_reason = row.last_check_detail = None
    await db.flush()
    if before_enabled != enabled:
        action, summary = ("enable", "Enabled") if enabled else ("disable", "Disabled")
        await log_audit(
            db,
            user=user,
            action=action,
            entity_type="secret_vault",
            entity_id=row.id,
            entity_name=row.display_name,
            summary=f"{summary} the vault provider '{row.name}'",
            details=_audit_details(row),
            request=request,
        )
    changed = sorted(
        field
        for field in ("name", "display_name", "settings", "credentials", "grants", "browsable", "cache_seconds")
        if getattr(body, field) is not None or (field == "cache_seconds" and body.clear_cache_seconds)
    )
    if changed:
        await log_audit(
            db,
            user=user,
            action="update",
            entity_type="secret_vault",
            entity_id=row.id,
            entity_name=row.display_name,
            summary=f"Updated the vault provider '{row.name}'"
            + (f" (renamed from '{before_name}')" if before_name != row.name else ""),
            details={**_audit_details(row), "changed": changed},
            request=request,
        )
    await db.commit()
    await refresh_vault_providers()
    # The count only: the scan this process read last (the rename guard's, when it ran) while it is recent.
    usage = (await recent_reference_uses(db)).by_provider()
    return await _row_out(db, row, usage, {str(user.id): user.full_name or user.email})


@admin_router.delete("/{provider_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_secret_vault(
    provider_id: str,
    request: Request,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> None:
    row = await _load_row(db, provider_id, lock=True)
    uses = await provider_uses(db, row.name)
    if uses:
        raise HTTPException(
            status_code=409,
            detail={
                "message": f"{len(uses)} secret field(s) read from '{row.name}': replace them before deleting it",
                "uses": [ReferenceUseModel(kind=u.kind, id=u.id, name=u.name, field=u.field).model_dump() for u in uses],
            },
        )
    await log_audit(
        db,
        user=user,
        action="delete",
        entity_type="secret_vault",
        entity_id=row.id,
        entity_name=row.display_name,
        summary=f"Deleted the vault provider '{row.name}'",
        details=_audit_details(row),
        request=request,
    )
    await db.delete(row)
    await db.commit()
    await refresh_vault_providers()


async def _limit_tests(request: Request | None, user: User) -> None:
    await enforce_rate_limit(
        request, "secret_vault_test", limit=TEST_RATE_LIMIT, window_seconds=TEST_RATE_WINDOW_SECONDS, bucket=str(user.id)
    )


async def _record_test(
    db: AsyncSession, user: User, request: Request, *, name: str, entity_id: Any, label: str, report: ConnectionReport
) -> None:
    failure = report.failure
    await log_audit(
        db,
        user=user,
        action="test",
        entity_type="secret_vault",
        entity_id=entity_id,
        entity_name=label,
        summary=f"Tested the vault provider '{name}': " + ("passed" if report.ok else f"failed ({failure.reason if failure else 'not run'})"),
        details={"name": name, "ok": report.ok, "steps": {step.step: step.ok for step in report.steps}},
        request=request,
    )


@admin_router.post("/{provider_id}/test", response_model=ConnectionReportModel)
async def test_secret_vault(
    provider_id: str,
    request: Request,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> ConnectionReportModel:
    """Test a stored provider as it is, and record the result as its status."""
    await _limit_tests(request, user)
    row = await _load_row(db, provider_id)
    try:
        config = config_of_row(row)
    except VaultConfigError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    await _require_allowed_addresses(config)
    # A client of its own: this process may not have applied the row's latest settings yet.
    report = await test_connection(config)
    at = datetime.now(UTC)
    failure = report.failure
    row.last_check_at = at
    row.last_check_ok = report.ok
    row.last_check_reason = failure.reason if failure else None
    row.last_check_detail = failure.detail if failure else None
    await _record_test(db, user, request, name=row.name, entity_id=row.id, label=row.display_name, report=report)
    await db.commit()
    return _report_model(report, at)


@admin_router.post("/test", response_model=ConnectionReportModel)
async def test_secret_vault_draft(
    body: SecretVaultDraftTest,
    request: Request,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> ConnectionReportModel:
    """Test the settings of the create or edit form before they are saved; nothing is stored."""
    from app.services.vault.config import database_config

    await _limit_tests(request, user)
    credentials = _lower_keys(body.credentials)
    entity_id: Any = None
    if body.provider_id:
        row = await _load_row(db, body.provider_id)
        if row.type != body.type:
            raise HTTPException(status_code=422, detail="type: a provider's type does not change")
        entity_id = row.id
        stored = {key.lower(): value for key, value in stored_credentials(row).items()}
        for key, value in (body.credentials or {}).items():
            if value is None or value == "":
                stored.pop(str(key).lower(), None)
        credentials = {**stored, **credentials}
    try:
        config = database_config(
            name=body.name,
            type=body.type,
            settings=_lower_keys(body.settings),
            credentials=credentials,
            display_name=body.display_name,
        )
    except VaultConfigError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    await _require_allowed_addresses(config)
    report = await test_connection(config)
    await _record_test(db, user, request, name=body.name, entity_id=entity_id, label=body.display_name, report=report)
    await db.commit()
    return _report_model(report, datetime.now(UTC))


@admin_router.post("/environment/{name}/test", response_model=ConnectionReportModel)
async def test_environment_secret_vault(
    name: str,
    request: Request,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> ConnectionReportModel:
    """Test a provider declared in the environment, with this process's own client."""
    await _limit_tests(request, user)
    resolver = get_resolver()
    if resolver.sources.get(name) != SOURCE_ENVIRONMENT:
        raise HTTPException(status_code=404, detail="No vault provider of that name is declared in the environment")
    provider = resolver.providers.get(name)
    config = resolver.config_of(name)
    if provider is None or config is None:
        report = ConnectionReport()
        from app.services.vault.checks import CheckStep

        report.steps.append(
            CheckStep(
                step="configuration",
                ok=False,
                reason="misconfigured",
                description=REASON_DESCRIPTIONS["misconfigured"],
                detail=resolver.errors.get(name),
            )
        )
    else:
        report = await test_connection(config, provider)
    await _record_test(db, user, request, name=name, entity_id=None, label=name, report=report)
    await db.commit()
    return _report_model(report, datetime.now(UTC))


# ---------------------------------------------------------------------------
# Everyone: the secret picker
# ---------------------------------------------------------------------------


async def _picker_caller(request: Request, db: AsyncSession, user: User) -> VaultPrincipal:
    await enforce_rate_limit(
        request,
        "secret_vault_picker",
        limit=PICKER_RATE_LIMIT,
        window_seconds=PICKER_RATE_WINDOW_SECONDS,
        bucket=str(user.id),
    )
    return await vault_principal(db, user)


def _usable_provider(name: str, principal: VaultPrincipal) -> None:
    """404 for a provider the caller may not use: one they are not granted is not shown to exist."""
    resolver = get_resolver()
    if name not in resolver.providers or not resolver.policy(name).usable_by(principal):
        raise HTTPException(status_code=404, detail="Vault provider not found")


def _parsed_path(name: str, path: str, *, label: str) -> SecretReference:
    try:
        return parse_secret_reference(f"vault://{name}/{path}")
    except SecretReferenceSyntaxError as exc:
        raise HTTPException(status_code=422, detail=f"{label}: {exc}") from None


def _visible(entry: ListedSecret, prefixes: tuple[PathPrefix, ...] | None) -> bool:
    """Whether a listed name is inside the caller's prefixes, or a folder on the way to one."""
    if prefixes is None:
        return True
    segments = tuple(entry.path.strip("/").split("/"))
    for prefix in prefixes:
        if segments[: len(prefix)] == prefix:
            return True
        if entry.folder and prefix[: len(segments)] == segments:
            return True
    return False


def _unavailable(exc: VaultReadError) -> PickerUnavailable:
    return PickerUnavailable(reason=exc.reason, description=REASON_DESCRIPTIONS.get(exc.reason, exc.reason))


@router.get("", response_model=list[PickerProvider])
async def list_picker_providers(
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[PickerProvider]:
    """The providers the caller may pick a secret from, with the paths they may use."""
    principal = await _picker_caller(request, db, user)
    resolver = get_resolver()
    found = []
    for name in resolver.usable_by(principal):
        config = resolver.config_of(name)
        policy = resolver.policy(name)
        prefixes = policy.prefixes_for(principal)
        found.append(
            PickerProvider(
                name=name,
                display_name=(config.display_name if config is not None and config.display_name else name),
                type=config.type if config is not None else "",
                source=resolver.sources.get(name, SOURCE_ENVIRONMENT),
                browsable=policy.browsable_by(principal),
                paths=None if prefixes is None else ["/".join(prefix) for prefix in prefixes],
            )
        )
    return sorted(found, key=lambda provider: provider.display_name.lower())


@router.get("/{name}/secrets", response_model=PickerListing)
async def list_picker_secrets(
    name: str,
    request: Request,
    prefix: str = Query("", max_length=1024),
    page_token: str | None = Query(None, max_length=4096),
    page_size: int = Query(100, ge=1, le=MAX_PAGE_SIZE),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> PickerListing:
    """The secret names and folders under *prefix* the caller may see; never a value."""
    principal = await _picker_caller(request, db, user)
    _usable_provider(name, principal)
    resolver = get_resolver()
    policy = resolver.policy(name)
    if not policy.browsable_by(principal):
        raise HTTPException(status_code=403, detail="Listing this vault is not allowed: type the path of the secret")
    base = prefix.strip("/")
    if base:
        _parsed_path(name, base, label="prefix")
    prefixes = policy.prefixes_for(principal)
    if base and prefixes is not None:
        folder = ListedSecret(name=base, path=f"{base}/", folder=True)
        if not _visible(folder, prefixes):
            raise HTTPException(status_code=403, detail="the prefix is outside the paths you may use on this vault")
    logger.info("Vault picker: user=%s listed provider=%s prefix=%s", user.id, name, base or "-")
    try:
        listing = await resolver.list_secrets(name, base, page_token=page_token, page_size=page_size)
    except VaultReadError as exc:
        logger.warning("Vault picker: provider=%s could not list prefix=%s reason=%s detail=%s", name, base or "-", exc.reason, exc.detail or "-")
        return PickerListing(entries=[], unavailable=_unavailable(exc))
    except Exception as exc:
        logger.warning("Vault picker: provider=%s could not list prefix=%s error=%s", name, base or "-", type(exc).__name__)
        return PickerListing(entries=[], unavailable=_unavailable(VaultReadError("unreachable", type(exc).__name__)))
    return PickerListing(
        entries=[
            PickerEntry(name=entry.name, path=entry.path, folder=entry.folder)
            for entry in listing.entries
            if _visible(entry, prefixes)
        ],
        next_page_token=listing.next_page_token,
    )


@router.get("/{name}/keys", response_model=PickerKeys)
async def list_picker_keys(
    name: str,
    request: Request,
    path: str = Query(..., min_length=1, max_length=1024),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> PickerKeys:
    """The entry names of the secret at *path* (read on the server, its values dropped); never a value."""
    principal = await _picker_caller(request, db, user)
    _usable_provider(name, principal)
    reference = _parsed_path(name, path.strip("/"), label="path")
    if problem := reference_access_problem(reference, principal):
        raise HTTPException(status_code=403, detail=problem)
    logger.info("Vault picker: user=%s read the keys of provider=%s path=%s", user.id, name, reference.path)
    try:
        payload = await get_resolver().payload(reference)
    except VaultReadError as exc:
        return PickerKeys(keys=[], single_value=False, unavailable=_unavailable(exc))
    keys = entry_names(payload)
    provider = get_resolver().providers.get(name)
    labels = provider.entry_labels(reference.path) if provider is not None else {}
    return PickerKeys(
        keys=keys,
        labels={key: label for key, label in labels.items() if key in keys and label and label != key},
        single_value=not keys,
    )


@router.post("/check", response_model=CheckResponse)
async def check_reference(
    body: CheckRequest,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> CheckResponse:
    """Resolve one reference on the server and say whether it works, never its value."""
    principal = await _picker_caller(request, db, user)
    try:
        reference = parse_secret_reference(body.reference)
    except SecretReferenceSyntaxError as exc:
        raise HTTPException(status_code=422, detail=f"reference: {exc}") from None
    _usable_provider(reference.provider, principal)
    if problem := reference_problem(str(reference)):
        raise HTTPException(status_code=422, detail=f"reference: {problem}")
    if problem := reference_access_problem(reference, principal):
        raise HTTPException(status_code=403, detail=problem)
    logger.info("Vault picker: user=%s checked provider=%s path=%s key=%s", user.id, reference.provider, reference.path, reference.key or "-")
    try:
        payload = await get_resolver().payload(reference)
        select_value(payload, reference.key)
    except VaultReadError as exc:
        return CheckResponse(ok=False, reason=exc.reason, description=REASON_DESCRIPTIONS.get(exc.reason, exc.reason))
    return CheckResponse(ok=True)
