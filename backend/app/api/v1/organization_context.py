# SPDX-License-Identifier: Apache-2.0

"""Organization profile API (#8987): read for every signed-in user, save for administrators.

``GET /api/v1/organization-context`` answers the current profile, its
version and the contract fields not filled; ``GET /versions`` lists the
saved versions; ``PUT`` saves a new version (administrators only);
``POST /draft`` proposes a profile from the connected platforms without
saving it; ``POST /ai-draft`` proposes field values with a model from the
inputs the administrator gives (#9484, nothing saved); ``GET /pirs`` lists
the connected OpenCTI's PIRs for the PIR picker (administrators);
``GET /proposals`` and ``POST /proposals/{id}/decide`` review the changes
proposed from chat (#9484, S-a2); ``GET /readers`` names the fleets that
read each part of the profile (#9173, S2); ``GET /setup-prompt`` and
``POST /setup-prompt`` carry the first-login setup proposal, shown once per
instance (#9173, S3); ``DELETE /setup-prompt`` forgets its answer so it is
proposed again (Settings > Platform, Reset onboarding). The
rules are ``services/organization_context.py``'s; agents read the same
profile through ``get_organization_context``.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any, Literal

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, Query, Response, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_db, require_admin, require_agentic_ai
from app.models.organization_context import OrganizationContextVersion
from app.models.user import User
from app.services import organization_context as org_context

router = APIRouter()


class OrganizationContextResponse(BaseModel):
    version: int | None
    source: str | None
    created_at: datetime | None
    author_user_id: str | None
    profile: dict[str, Any]
    missing_fields: list[str]
    #: On a save: list field -> the names of the rows it held in the version before and no longer holds (#1473).
    removed_rows: dict[str, list[str]] = {}
    #: The provenance a version keeps (a restore names the version it restored from, #1473).
    sources: dict[str, Any] | None = None


class OrganizationContextVersionSummary(BaseModel):
    version: int
    source: str
    created_at: datetime
    author_user_id: str | None
    missing_fields: list[str]
    #: The crown jewels the version holds (#1558): the restore picker shows which version still has them.
    crown_jewels: int = 0
    #: An ``ai_draft`` version's accepted proposals: field -> ``{source, basis}``.
    sources: dict[str, Any] | None = None


class SaveOrganizationContextRequest(BaseModel):
    profile: dict[str, Any]
    source: Literal["form", "draft", "api", "ai_draft"] = "form"
    #: With ``ai_draft``: the accepted proposals' provenance, field -> ``{source, basis}``.
    sources: dict[str, dict[str, str]] | None = None
    #: The version the profile was edited from: another version saved since answers 409, nothing saved (#1473).
    #: Omitted: no check (a client written before it).
    based_on_version: int | None = None


class RestoreOrganizationContextRequest(BaseModel):
    #: The profile fields to take from the past version (e.g. ``crown_jewels``); empty: every field.
    fields: list[str] = []
    #: The current version the restore starts from: another version saved since answers 409.
    based_on_version: int | None = None


class OpenCrqSyncRequest(BaseModel):
    #: False (default) answers the plan and writes nothing.
    apply: bool = False
    tenant_id: str = ""
    #: The lowest OpenCRQ criticality the check pulls (low, medium, high, very_high, #1558);
    #: empty: the saved crown-jewel sync's filter, else high.
    min_criticality: Literal["", "low", "medium", "high", "very_high"] = ""


class OpenCrqSyncResponse(BaseModel):
    applied: bool
    plan: dict[str, Any]
    errors: list[str]
    version: int | None


class DraftOrganizationContextRequest(BaseModel):
    #: OpenCRQ tenant, when the administrator belongs to several.
    tenant_id: str = ""


class OrganizationPir(BaseModel):
    id: str
    name: str


class OrganizationPirsResponse(BaseModel):
    pirs: list[OrganizationPir]
    #: ``None`` when read; ``not_connected`` or ``unreadable`` otherwise (the page shows the saved names).
    unavailable: Literal["not_connected", "unreadable"] | None = None
    #: The connected OpenCTI's base URL, to link each PIR; empty when not connected.
    opencti_url: str = ""


class DraftOrganizationContextResponse(BaseModel):
    #: The version the draft starts from (``None`` before the first save).
    based_on_version: int | None
    #: The proposed profile: the current one with its empty fields filled.
    profile: dict[str, Any]
    #: One entry per field the draft filled: ``field``, ``source`` platform, ``detail``.
    filled: list[dict[str, str]]
    #: The platforms that could not be read, and why.
    unavailable: list[str]
    missing_fields: list[str]


def _response(
    row: OrganizationContextVersion | None,
    removed_rows: dict[str, list[str]] | None = None,
    *,
    profile: org_context.OrganizationProfile | None = None,
) -> OrganizationContextResponse:
    profile = profile if profile is not None else org_context.parse_profile(row.profile if row else {})
    return OrganizationContextResponse(
        version=row.version if row else None,
        source=row.source if row else None,
        created_at=row.created_at if row else None,
        author_user_id=str(row.author_user_id) if row and row.author_user_id else None,
        profile=profile.model_dump(mode="json"),
        missing_fields=org_context.missing_fields(profile),
        removed_rows=removed_rows or {},
        sources=row.sources if row else None,
    )


def _stale_base(based_on_version: int | None, current: OrganizationContextVersion | None) -> None:
    """409 when *based_on_version* is given and another version was saved since (#1473)."""
    if based_on_version is None:
        return
    latest = current.version if current is not None else None
    if latest != based_on_version:
        raise HTTPException(
            status_code=409,
            detail=(
                f"The profile changed since version {based_on_version} "
                f"(now version {latest}). Reload and apply your change again: nothing was saved."
            ),
        )


@router.get("", response_model=OrganizationContextResponse)
async def get_organization_context(
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> OrganizationContextResponse:
    """The current profile (empty with every field missing before the first save).

    The asset and group lists are read from the asset records (#1543): the row
    API, agent and import writes land there, never in a profile version.
    """
    row, profile = await org_context.current_profile(db)
    return _response(row, profile=profile)


class OrganizationContextInjection(BaseModel):
    #: Whether every agent's system prompt starts with the organization summary
    #: (#11533); an agent or an assignment may override it (inherit | always | off).
    enabled: bool
    #: The categories every inheriting agent preloads after the basic block
    #: (none by default); an agent or an assignment may set its own list.
    #: Omitted on a write: unchanged.
    categories: list[str] | None = None
    #: Every category the model has, in order (read-only, for the selector).
    available_categories: list[str] = []


async def _injection(db: AsyncSession) -> OrganizationContextInjection:
    from app.services.organization_context_categories import available_names
    from app.services.organization_context_injection import platform_categories, platform_enabled

    return OrganizationContextInjection(
        enabled=await platform_enabled(db),
        categories=await platform_categories(db),
        # Copilot mode offers none: the profile is its identity alone (#1690).
        available_categories=list(available_names()),
    )


@router.get("/injection", response_model=OrganizationContextInjection)
async def get_organization_context_injection(
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> OrganizationContextInjection:
    """The platform default of the cascade assignment > agent > platform."""
    return await _injection(db)


@router.put("/injection", response_model=OrganizationContextInjection)
async def save_organization_context_injection(
    body: OrganizationContextInjection,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> OrganizationContextInjection:
    """Turn the organization summary on or off for every agent that inherits (administrators, audited)."""
    from app.api.v1.platform_settings_helpers import get_platform_settings
    from app.services.audit import log_audit
    from app.services.organization_context_categories import checked_for_api, normalize_categories

    settings_row = await get_platform_settings(db)
    categories = checked_for_api(body.categories) if body.categories is not None else None
    if categories is not None and categories != normalize_categories(settings_row.organization_context_categories):
        settings_row.organization_context_categories = categories
        await log_audit(
            db,
            user=admin,
            action="configure",
            entity_type="organization_context",
            entity_name="injection",
            summary="Organization context preloaded categories: " + (", ".join(categories) or "none"),
            details={"categories": categories},
        )
    if bool(settings_row.organization_context_enabled) != body.enabled:
        settings_row.organization_context_enabled = body.enabled
        await log_audit(
            db,
            user=admin,
            action="configure",
            entity_type="organization_context",
            entity_name="injection",
            summary=f"Organization summary in every agent: {'on' if body.enabled else 'off'}",
            details={"enabled": body.enabled},
        )
    await db.commit()
    return await _injection(db)


class AttackSurfaceProviders(BaseModel):
    #: The integration providers the attack surface pass reads (CTEM-26), in order.
    providers: list[str]


@router.get("/attack-surface/providers", response_model=AttackSurfaceProviders)
async def list_attack_surface_providers(
    _user: User = Depends(get_current_user),
) -> AttackSurfaceProviders:
    """The providers the outside lens can read, so the Settings picker grows with each new reader."""
    from app.services.ctem_attack_surface import SUPPORTED_PROVIDERS

    return AttackSurfaceProviders(providers=list(SUPPORTED_PROVIDERS))


@router.get("/versions", response_model=list[OrganizationContextVersionSummary])
async def list_organization_context_versions(
    limit: int = Query(default=20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    _admin: User = Depends(require_admin),
) -> list[OrganizationContextVersionSummary]:
    """The saved versions, newest first (administrators)."""
    rows = await org_context.list_versions(db, limit=limit)
    return [
        OrganizationContextVersionSummary(
            version=row.version,
            source=row.source,
            created_at=row.created_at,
            author_user_id=str(row.author_user_id) if row.author_user_id else None,
            missing_fields=org_context.missing_fields(org_context.parse_profile(row.profile)),
            crown_jewels=len((row.profile or {}).get("crown_jewels") or []),
            sources=row.sources,
        )
        for row in rows
    ]


async def _checked_profile(
    db: AsyncSession, admin: User, stored: dict[str, Any] | None, profile: org_context.OrganizationProfile
) -> org_context.OrganizationProfile:
    """The checks every hand save runs (form PUT and restore, #1473): 422 on the first problem.

    Links between lists (``check_links``), the #1367 lock on synced rows, the
    ticketing target; then each PIR is stored with its OpenCTI name (#11589).
    """
    from app.services.ctem_ticketing import target_problem
    from app.services.organization_assets_import import check_links
    from app.services.organization_context_draft import name_saved_pirs
    from app.services.organization_context_locks import synced_edit_problems

    problems = check_links(profile)
    if problems:
        raise HTTPException(status_code=422, detail=problems[0])
    # A synced row is read-only by hand on every surface: only its local fields change (#1367).
    locked = synced_edit_problems(stored, profile.model_dump(mode="json"))
    if locked:
        raise HTTPException(status_code=422, detail=locked[0])
    ticketing_problem = await target_problem(db, profile)
    if ticketing_problem:
        raise HTTPException(status_code=422, detail=ticketing_problem)
    return await name_saved_pirs(db, user_id=admin.id, profile=profile)


@router.put("", response_model=OrganizationContextResponse)
async def save_organization_context(
    body: SaveOrganizationContextRequest,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> OrganizationContextResponse:
    """Save *body.profile* as the next version (administrators); 422 names the bad field."""
    submitted = dict(body.profile or {})
    # #1383: the reference exchange rates are set by API or by an agent, not by the
    # settings form; #1432: the form sends the shipped products (an empty list clears
    # them), and a client that leaves the key out keeps the stored ones.
    # A save that does not send one keeps the stored value.
    kept = [key for key in ("fx_rates", "shipped_products") if key not in submitted]
    if kept:
        current = await org_context.current_version(db)
        for key in kept:
            if current is not None and (current.profile or {}).get(key):
                submitted[key] = current.profile[key]
    try:
        profile = org_context.parse_profile(submitted)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    current = await org_context.current_version(db)
    _stale_base(body.based_on_version, current)
    stored = org_context.parse_profile(current.profile).model_dump(mode="json") if current is not None else None
    profile = await _checked_profile(db, admin, stored, profile)
    try:
        row = await org_context.save_version(
            db,
            profile,
            source=body.source,
            author_user_id=admin.id,
            sources=org_context.accepted_sources(body.sources) if body.source == "ai_draft" else None,
        )
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(
            status_code=409, detail="Another version was saved at the same time. Reload and save again."
        ) from None
    await db.refresh(row)
    return _response(row, org_context.removed_rows(stored, profile.model_dump(mode="json")))


@router.get("/versions/{version}", response_model=OrganizationContextResponse)
async def get_organization_context_version(
    version: int,
    db: AsyncSession = Depends(get_db),
    _admin: User = Depends(require_admin),
) -> OrganizationContextResponse:
    """One saved version's full profile (administrators, #1473); 404 when it does not exist."""
    row = await org_context.get_version(db, version)
    if row is None:
        raise HTTPException(status_code=404, detail=f"Version {version} of the organization profile does not exist.")
    return _response(row)


@router.post("/versions/{version}/restore", response_model=OrganizationContextResponse)
async def restore_organization_context_version(
    version: int,
    body: RestoreOrganizationContextRequest,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> OrganizationContextResponse:
    """Save the next version with *body.fields* taken from version *version* (administrators, #1473).

    The current profile keeps every other field; the saved version's
    provenance names each restored field ``restored from v<version>``. 404
    for an unknown version, 422 for an unknown field, 409 when
    ``based_on_version`` is not the current version.
    """
    past = await org_context.get_version(db, version)
    if past is None:
        raise HTTPException(status_code=404, detail=f"Version {version} of the organization profile does not exist.")
    current = await org_context.current_version(db)
    _stale_base(body.based_on_version, current)
    stored = org_context.parse_profile(current.profile if current else {}).model_dump(mode="json")
    old = org_context.parse_profile(past.profile).model_dump(mode="json")
    try:
        merged, fields = org_context.restored_profile(stored, old, body.fields)
        profile = org_context.parse_profile(merged)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    # A restore is a hand save: the same checks as PUT (links, synced-row lock, ticketing, PIR names).
    profile = await _checked_profile(db, admin, stored if current is not None else None, profile)
    basis = f"restored from v{version} by an administrator"
    sources = {name: {"source": f"restored from v{version}", "basis": basis} for name in fields}
    try:
        row = await org_context.save_version(db, profile, source="api", author_user_id=admin.id, sources=sources)
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(
            status_code=409, detail="Another version was saved at the same time. Reload and restore again."
        ) from None
    await db.refresh(row)
    return _response(row, org_context.removed_rows(stored, profile.model_dump(mode="json")))


class EnterpriseKnowledgeLink(BaseModel):
    knowledge_base_id: str
    label: str
    kind: str
    #: Whether the caller can read (and so an agent acting for them can search) it.
    readable: bool


@router.get("/enterprise-knowledge", response_model=list[EnterpriseKnowledgeLink])
async def list_enterprise_knowledge(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[EnterpriseKnowledgeLink]:
    """The knowledge bases the profile links as enterprise knowledge (#11543), each readable or not for the caller.

    A knowledge base the caller cannot read is named by the label saved with
    the link, never by its own name.
    """
    from app.services.organization_enterprise_knowledge import linked_knowledge_bases

    row = await org_context.current_version(db)
    profile = org_context.parse_profile(row.profile if row else {})
    links = await linked_knowledge_bases(db, profile, user_id=user.id, is_admin=bool(user.is_admin))
    return [EnterpriseKnowledgeLink(**vars(link)) for link in links]


class AssetImportRowError(BaseModel):
    line: int
    name: str
    message: str


class AssetsImportResponse(BaseModel):
    #: False: a preview, nothing saved. True: the rows were saved as ``version``.
    applied: bool
    #: The rows the file holds, parsed (``source`` is ``csv``).
    assets: list[dict[str, Any]]
    #: The asset groups the file declares (rows typed ``asset_group``, #11781).
    groups: list[dict[str, Any]] = []
    #: crown jewel name -> asset names the file links to it.
    links: dict[str, list[str]]
    errors: list[AssetImportRowError]
    unknown_crown_jewels: list[str]
    #: Asset names already in the profile that the file replaces.
    replaced: list[str]
    version: int | None = None


@router.post("/assets/import", response_model=AssetsImportResponse)
async def import_organization_assets(
    file: UploadFile = File(...),
    apply: bool = Form(default=False),
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> AssetsImportResponse:
    """Assets and exposure from a CSV (administrators, #11543): a preview, then ``apply=true`` saves.

    The preview lists every row parsed or refused with its line and reason;
    ``apply=true`` with a row refused is 422 (fix the file, preview again).
    Rows are upserted by name, rows the file does not name are kept, and the
    crown jewel links the file names are added. 422 names a bad file.
    """
    from app.services import organization_assets_import as assets_import

    content = await file.read(assets_import.MAX_BYTES + 1)
    # Planned on the records' lists (#1543): the stored JSON misses the row writes, and its save would remove them.
    _row, profile = await org_context.current_profile(db)
    try:
        preview = assets_import.parse_assets_csv(content, profile)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    response = AssetsImportResponse(
        applied=False,
        assets=[asset.model_dump(mode="json") for asset in preview.assets],
        groups=[group.model_dump(mode="json") for group in preview.groups],
        links=preview.links,
        errors=[AssetImportRowError(line=e.line, name=e.name, message=e.message) for e in preview.errors],
        unknown_crown_jewels=preview.unknown_crown_jewels,
        replaced=preview.replaced,
    )
    if not apply:
        return response
    if preview.errors:
        raise HTTPException(
            status_code=422, detail=f"{len(preview.errors)} row(s) refused; fix the file and preview again."
        )
    try:
        merged = assets_import.merge_assets(profile, preview)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    try:
        saved = await org_context.save_version(db, merged, source="api", author_user_id=admin.id)
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(
            status_code=409, detail="Another version was saved at the same time. Reload and import again."
        ) from None
    response.applied = True
    response.version = saved.version
    return response


class CanonicalAssetsPage(BaseModel):
    #: One summary per canonical asset (one real thing across sources, #1178).
    items: list[dict[str, Any]]
    total: int
    offset: int
    limit: int


async def _built_store(db: AsyncSession) -> uuid.UUID | None:
    """The primary's store built from its current profile, committed when the read had to build it."""
    from app.services.organization_asset_store import ensure_built

    org = await ensure_built(db)
    await db.commit()
    return org


@router.get("/assets", response_model=CanonicalAssetsPage)
async def list_canonical_assets(
    q: str = Query("", max_length=200),
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    category: list[str] = Query(default_factory=list, max_length=50),
    subcategory: list[str] = Query(default_factory=list, max_length=50),
    criticality: list[str] = Query(default_factory=list, max_length=50),
    internet_facing: list[str] = Query(default_factory=list, max_length=50),
    source: list[str] = Query(default_factory=list, max_length=50),
    group: list[str] = Query(default_factory=list, max_length=50),
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> CanonicalAssetsPage:
    """The canonical assets (one per real thing, its sources and software count), paginated by name.

    Each filter (repeated for several values) keeps the assets holding one of its values; ``group``
    takes asset group ids and keeps the assets one of whose records is a member (#1556).
    """
    from app.services.organization_asset_store import list_assets

    org = await _built_store(db)
    if org is None:
        return CanonicalAssetsPage(items=[], total=0, offset=offset, limit=limit)
    filters = {
        "category": category,
        "subcategory": subcategory,
        "criticality": criticality,
        "internet_facing": internet_facing,
        "source": source,
        "group": group,
    }
    page = await list_assets(db, org, q=q, offset=offset, limit=limit, filters=filters)
    return CanonicalAssetsPage(**vars(page))


@router.get("/assets/facets")
async def read_asset_facets(
    q: str = Query("", max_length=200),
    category: list[str] = Query(default_factory=list, max_length=50),
    subcategory: list[str] = Query(default_factory=list, max_length=50),
    criticality: list[str] = Query(default_factory=list, max_length=50),
    internet_facing: list[str] = Query(default_factory=list, max_length=50),
    source: list[str] = Query(default_factory=list, max_length=50),
    group: list[str] = Query(default_factory=list, max_length=50),
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> dict[str, list[dict[str, Any]]]:
    """Each value the asset list filters on with its asset count, given the search and the other filters picked."""
    from app.services.organization_asset_store import FILTER_FIELDS, asset_facets

    org = await _built_store(db)
    if org is None:
        return {name: [] for name in (*FILTER_FIELDS, "source", "group")}
    filters = {
        "category": category,
        "subcategory": subcategory,
        "criticality": criticality,
        "internet_facing": internet_facing,
        "source": source,
        "group": group,
    }
    return await asset_facets(db, org, q=q, filters=filters)


@router.get("/assets/identity")
async def read_asset_identity(
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> dict[str, Any]:
    """How assets are matched and which source each field shows: the saved settings, the defaults, the effective.

    Saved with the profile (``asset_identity``, administrators, ``PUT /organization-context``).
    """
    from app.services import organization_asset_identity as identity
    from app.services.organization_asset_store import identity_settings
    from app.services.organization_context import current_version

    version = await current_version(db, None)
    profile = (version.profile if version is not None else None) or {}
    key_order, precedence = identity_settings(profile)
    return {
        "saved": {"key_order": list(key_order), "precedence": {k: list(v) for k, v in precedence.items()}},
        "defaults": {
            "key_order": list(identity.DEFAULT_KEY_ORDER),
            "weak_keys": sorted(identity.WEAK_KINDS),
            "precedence": {k: list(v) for k, v in identity.DEFAULT_PRECEDENCE.items()},
            "fallback_precedence": list(identity.FALLBACK_PRECEDENCE),
            "local_fields": sorted(identity.DEFAULT_LOCAL_FIELDS),
        },
        "effective": {
            "key_order": list(key_order or identity.DEFAULT_KEY_ORDER),
            "precedence": {k: list(v) for k, v in {**identity.DEFAULT_PRECEDENCE, **precedence}.items()},
        },
    }


class AssetLookupRequest(BaseModel):
    #: The identifiers a platform holds for its own objects: hostnames, FQDNs, IP or MAC addresses.
    keys: list[str] = Field(min_length=1, max_length=200)


class AssetLookupMatch(BaseModel):
    key: str
    id: str
    name: str
    category: Any = None
    criticality: Any = None
    internet_facing: Any = None
    owner: Any = None
    #: How many assets hold the key (a shared IP): the first by name answers.
    matches: int
    #: The asset's page in the CTEM inventory.
    url: str


class AssetLookupResponse(BaseModel):
    items: list[AssetLookupMatch]


@router.post("/assets/lookup", response_model=AssetLookupResponse)
async def lookup_canonical_assets(
    body: AssetLookupRequest,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> AssetLookupResponse:
    """The inventory assets the given identifiers name, for a platform's cross-product pivot.

    OpenCTI, OpenAEV and OpenCRQ call it with the user's own token (the user
    reads what the inventory list answers him); a key no asset holds is absent.
    """
    from app.services.email import platform_url
    from app.services.organization_asset_store import find_assets_by_identifier

    org = await _built_store(db)
    if org is None:
        return AssetLookupResponse(items=[])
    found = await find_assets_by_identifier(db, org, body.keys)
    base = platform_url()
    return AssetLookupResponse(
        items=[
            AssetLookupMatch(key=key, url=f"{base}/ctem/inventory/assets/{match['id']}", **match)
            for key, match in found.items()
        ]
    )


@router.get("/assets/by-software", response_model=CanonicalAssetsPage)
async def list_assets_of_software(
    product: str = Query(..., min_length=1, max_length=200),
    vendor: str = Query("", max_length=200),
    version: str = Query("", max_length=100),
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> CanonicalAssetsPage:
    """The assets running a software (vendor and version optional), with the version each runs."""
    from app.services.organization_asset_store import assets_of_software

    org = await _built_store(db)
    if org is None:
        return CanonicalAssetsPage(items=[], total=0, offset=offset, limit=limit)
    page = await assets_of_software(
        db, org, product=product, vendor=vendor, version=version, offset=offset, limit=limit
    )
    return CanonicalAssetsPage(**vars(page))


@router.get("/software", response_model=CanonicalAssetsPage)
async def list_organization_software(
    q: str = Query("", max_length=200),
    # Several values of one filter answer any of them (the Software tab's multi-select filters, #1495 slice 2).
    type: list[str] = Query(default_factory=list, max_length=10),
    vendor: str = Query("", max_length=200),
    source: str = Query("", max_length=80),
    origin: list[str] = Query(default_factory=list, max_length=10),
    cpe_state: list[str] = Query(default_factory=list, max_length=10),
    unknown_version: bool | None = Query(None),
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> CanonicalAssetsPage:
    """The organization's software (#1495): per software its type, CPE and its state, version and asset counts."""
    from app.services.organization_software_store import list_software

    org = await _built_store(db)
    if org is None:
        return CanonicalAssetsPage(items=[], total=0, offset=offset, limit=limit)
    page = await list_software(
        db,
        org,
        q=q,
        software_type=type,
        vendor=vendor,
        source=source,
        origin=origin,
        cpe_state=cpe_state,
        unknown_version=unknown_version,
        offset=offset,
        limit=limit,
    )
    return CanonicalAssetsPage(**vars(page))


@router.get("/software/versions/{version_id}/assets", response_model=CanonicalAssetsPage)
async def list_assets_of_software_version(
    version_id: uuid.UUID,
    through_dependencies: bool = Query(True),
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> CanonicalAssetsPage:
    """The assets where a software version is present, directly or through a software that depends on it."""
    from app.services.organization_software_store import assets_of_version

    org = await _built_store(db)
    page = (
        await assets_of_version(
            db, org, version_id, through_dependencies=through_dependencies, offset=offset, limit=limit
        )
        if org is not None
        else None
    )
    if page is None:
        raise HTTPException(status_code=404, detail="Software version not found")
    return CanonicalAssetsPage(**vars(page))


@router.get("/software/export.csv")
async def export_organization_software(
    q: str = Query("", max_length=200),
    # Several values of one filter answer any of them (the Software tab's multi-select filters, #1495 slice 2).
    type: list[str] = Query(default_factory=list, max_length=10),
    vendor: str = Query("", max_length=200),
    source: str = Query("", max_length=80),
    origin: list[str] = Query(default_factory=list, max_length=10),
    cpe_state: list[str] = Query(default_factory=list, max_length=10),
    unknown_version: bool | None = Query(None),
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> Response:
    """The organization's software as CSV, with the filters of the list (#1495); formula cells neutralised."""
    from app.core.utils import build_content_disposition
    from app.services.organization_software_store import CSV_COLUMNS, software_csv

    org = await _built_store(db)
    if org is not None:
        body = await software_csv(
            db,
            org,
            q=q,
            software_type=type,
            vendor=vendor,
            source=source,
            origin=origin,
            cpe_state=cpe_state,
            unknown_version=unknown_version,
        )
    else:
        body = ",".join(CSV_COLUMNS) + "\n"
    return Response(
        content=body,
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": build_content_disposition("software.csv"),
            "Cache-Control": "private, no-store",
        },
    )


class SoftwareCreateRequest(BaseModel):
    vendor: str = Field(default="", max_length=200)
    product: str = Field(min_length=1, max_length=200)
    #: The name shown; empty: ``vendor product``.
    name: str = Field(default="", max_length=300)
    type: str = Field(default="application", max_length=30)
    description: str = Field(default="", max_length=2000)
    #: A CPE 2.3 the administrator sets; it wins over every source.
    cpe: str = Field(default="", max_length=300)
    purl: str = Field(default="", max_length=500)


class SoftwareUpdateRequest(BaseModel):
    """The fields to change; a synced software takes ``cpe_override`` only (an empty one clears it)."""

    name: str | None = Field(default=None, max_length=300)
    type: str | None = Field(default=None, max_length=30)
    description: str | None = Field(default=None, max_length=2000)
    purl: str | None = Field(default=None, max_length=500)
    cpe_override: str | None = Field(default=None, max_length=300)


class SoftwareAttachRequest(BaseModel):
    asset_id: uuid.UUID
    #: The version installed; empty is ``unknown``.
    version: str = Field(default="", max_length=100)


def _software_http_error(exc: Exception) -> HTTPException:
    return HTTPException(status_code=getattr(exc, "status", 422), detail=str(exc))


async def _audit_software(
    db: AsyncSession, admin: User, action: str, entity_id: Any, summary: str, details: dict[str, Any]
) -> None:
    from app.services.audit import log_audit

    await log_audit(
        db,
        user=admin,
        action=action,
        entity_type="organization_context",
        entity_id=entity_id,
        entity_name="software",
        summary=summary,
        details=details,
    )


@router.post("/software", status_code=201)
async def create_organization_software(
    body: SoftwareCreateRequest,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> dict[str, Any]:
    """Type a software by hand (administrators, audited); 409 when the vendor and product exist, 422 a bad CPE or type."""
    from app.services.organization_software_store import SoftwareError, create_software

    org = await _built_store(db)
    if org is None:
        raise HTTPException(status_code=404, detail="No organization profile yet")
    try:
        soft = await create_software(
            db,
            org,
            vendor=body.vendor,
            product=body.product,
            name=body.name,
            software_type=body.type,
            description=body.description,
            cpe=body.cpe,
            purl=body.purl,
        )
    except SoftwareError as exc:
        await db.rollback()
        raise _software_http_error(exc) from None
    await _audit_software(
        db,
        admin,
        "create",
        soft["id"],
        f"Software {soft['name']} typed by hand",
        {k: soft[k] for k in ("vendor", "product", "type", "cpe_override")},
    )
    await db.commit()
    return soft


@router.patch("/software/{software_id}")
async def update_organization_software(
    software_id: uuid.UUID,
    body: SoftwareUpdateRequest,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> dict[str, Any]:
    """Change a software (administrators, audited); a synced one takes its CPE override only (422 otherwise)."""
    from app.services.organization_software_store import SoftwareError, update_software

    org = await _built_store(db)
    if org is None:
        raise HTTPException(status_code=404, detail="Software not found")
    try:
        soft, changed = await update_software(db, org, software_id, body.model_dump(exclude_none=True))
    except SoftwareError as exc:
        await db.rollback()
        raise _software_http_error(exc) from None
    if changed:
        await _audit_software(
            db, admin, "update", software_id, f"Software {soft['name']} changed: " + ", ".join(changed), changed
        )
    await db.commit()
    return soft


@router.delete("/software/{software_id}", status_code=204)
async def delete_organization_software(
    software_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> None:
    """Delete a hand-typed software and its hand attachments (administrators, audited); 422 for a synced one."""
    from app.services.organization_asset_store import rebuild_from_profile
    from app.services.organization_software_store import SoftwareError, delete_software

    org = await _built_store(db)
    if org is None:
        raise HTTPException(status_code=404, detail="Software not found")
    try:
        gone = await delete_software(db, org, software_id)
    except SoftwareError as exc:
        await db.rollback()
        raise _software_http_error(exc) from None
    if gone["detached"]:
        await rebuild_from_profile(db, org)
    await _audit_software(
        db,
        admin,
        "delete",
        software_id,
        f"Software {gone['name']} deleted",
        {"vendor": gone["vendor"], "product": gone["product"], "detached": gone["detached"]},
    )
    await db.commit()


@router.post("/software/{software_id}/installations", status_code=201)
async def attach_organization_software(
    software_id: uuid.UUID,
    body: SoftwareAttachRequest,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> dict[str, Any]:
    """Attach a hand-typed software to an asset at a version (administrators, audited); kept across rebuilds."""
    from app.services.organization_asset_store import rebuild_from_profile
    from app.services.organization_software_store import SoftwareError, attach_software

    org = await _built_store(db)
    if org is None:
        raise HTTPException(status_code=404, detail="Software not found")
    try:
        result = await attach_software(
            db, org, software_id, body.asset_id, version=body.version, author_user_id=admin.id
        )
    except SoftwareError as exc:
        await db.rollback()
        raise _software_http_error(exc) from None
    if result["added"]:
        await rebuild_from_profile(db, org)
        await _audit_software(
            db,
            admin,
            "configure",
            software_id,
            f"Software attached to an asset at version {result['version']}",
            result,
        )
    await db.commit()
    return result


@router.delete("/software/{software_id}/installations/{asset_id}", status_code=204)
async def detach_organization_software(
    software_id: uuid.UUID,
    asset_id: uuid.UUID,
    version: str = Query("", max_length=100),
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> None:
    """Remove a hand attachment from an asset (every version when ``version`` is empty; administrators, audited)."""
    from app.services.organization_asset_store import rebuild_from_profile
    from app.services.organization_software_store import SoftwareError, detach_software

    org = await _built_store(db)
    if org is None:
        raise HTTPException(status_code=404, detail="Software not found")
    try:
        removed = await detach_software(db, org, software_id, asset_id, version=version)
    except SoftwareError as exc:
        await db.rollback()
        raise _software_http_error(exc) from None
    await rebuild_from_profile(db, org)
    await _audit_software(
        db,
        admin,
        "configure",
        software_id,
        f"Software detached from an asset: {removed} installation(s)",
        {"asset_id": str(asset_id), "version": version or None, "removed": removed},
    )
    await db.commit()


@router.get("/software/{software_id}")
async def get_organization_software(
    software_id: uuid.UUID,
    installations_offset: int = Query(0, ge=0),
    installations_limit: int = Query(100, ge=1, le=200),
    installations_q: str = Query("", max_length=200),
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> dict[str, Any]:
    """One software: its versions with asset counts, its installations (searchable by asset name), what it depends on and what uses it."""
    from app.services.organization_software_store import get_software

    org = await _built_store(db)
    found = (
        await get_software(
            db,
            org,
            software_id,
            installations_offset=installations_offset,
            installations_limit=installations_limit,
            installations_q=installations_q,
        )
        if org is not None
        else None
    )
    if found is None:
        raise HTTPException(status_code=404, detail="Software not found")
    return found


class AssetDecisionRecord(BaseModel):
    source: str = Field(min_length=1, max_length=80)
    external_id: str = Field(min_length=1, max_length=200)


class AssetDecisionRequest(BaseModel):
    #: ``merge``: the records are one asset; ``split``: the two records are never one asset.
    kind: Literal["merge", "split"]
    records: list[AssetDecisionRecord] = Field(min_length=2, max_length=50)
    reason: str = Field(default="", max_length=500)


@router.get("/assets/decisions")
async def list_asset_decisions(
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> dict[str, Any]:
    """The administrators' merge and split decisions the store applies, newest first."""
    from app.services.organization_asset_store import list_decisions

    org = await _built_store(db)
    return {"items": await list_decisions(db, org) if org is not None else []}


@router.post("/assets/decisions", status_code=201)
async def record_asset_decision(
    body: AssetDecisionRequest,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> dict[str, Any]:
    """Merge source records into one asset, or keep two apart (administrators, audited)."""
    from app.services.audit import log_audit
    from app.services.organization_asset_store import DecisionError, decision_dict, record_decision

    org = await _built_store(db)
    if org is None:
        raise HTTPException(status_code=404, detail="No organization profile yet")
    try:
        row = await record_decision(
            db,
            org,
            kind=body.kind,
            records=[r.model_dump() for r in body.records],
            reason=body.reason,
            author_user_id=admin.id,
        )
    except DecisionError as exc:
        await db.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from None
    labels = [f"{r['source']}:{r['external_id']}" for r in row.records]
    await log_audit(
        db,
        user=admin,
        action="configure",
        entity_type="organization_context",
        entity_id=row.id,
        entity_name="asset_decision",
        summary=f"Asset {row.kind}: " + ", ".join(labels),
        details={"kind": row.kind, "records": row.records, "reason": row.reason},
    )
    await db.commit()
    return decision_dict(row)


@router.delete("/assets/decisions/{decision_id}", status_code=204)
async def remove_asset_decision(
    decision_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> None:
    """Drop a merge or split: the keys decide again (administrators, audited)."""
    from app.services.audit import log_audit
    from app.services.organization_asset_store import remove_decision

    org = await _built_store(db)
    row = await remove_decision(db, org, decision_id) if org is not None else None
    if row is None:
        raise HTTPException(status_code=404, detail="Decision not found")
    await log_audit(
        db,
        user=admin,
        action="delete",
        entity_type="organization_context",
        entity_id=decision_id,
        entity_name="asset_decision",
        summary=f"Asset {row.kind} removed: "
        + ", ".join(f"{r['source']}:{r['external_id']}" for r in row.records or []),
        details={"kind": row.kind, "records": row.records},
    )
    await db.commit()


# ── Row APIs on the asset records (#1543): one row in, one row out, never the profile ──


class AssetRecordRequest(BaseModel):
    """An asset row as the profile's ``AssetEntry`` takes it (validated by the record service)."""

    model_config = {"extra": "allow"}

    name: str = Field(min_length=1, max_length=300)


class AssetRecordPatchRequest(BaseModel):
    """The fields to change on one record; ``source`` and ``external_id`` never move."""

    model_config = {"extra": "allow"}


class AssetRecordBatchRequest(BaseModel):
    #: Rows matched on ``(source, external_id)``: an unknown pair is created, a different row updated.
    rows: list[dict[str, Any]] = Field(max_length=10_000)


class AssetRecordBulkDeleteRequest(BaseModel):
    ids: list[uuid.UUID] = Field(min_length=1, max_length=1000)


class AssetGroupRequest(BaseModel):
    model_config = {"extra": "allow"}


class CrownJewelLinksRequest(BaseModel):
    #: The asset record and asset group names the crown jewel covers (replaces its links).
    names: list[str] = Field(default_factory=list, max_length=1000)


class AssetChangesRestoreRequest(BaseModel):
    change_ids: list[int] = Field(min_length=1, max_length=1000)


def _record_http_error(exc: Exception) -> HTTPException:
    from app.services.organization_asset_records import RecordNotFoundError, StaleRowError

    if isinstance(exc, RecordNotFoundError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, StaleRowError):
        return HTTPException(status_code=409, detail=str(exc))
    return HTTPException(status_code=422, detail=str(exc))


def _if_match(value: str | None) -> int | None:
    """The row version an ``If-Match`` header names (``"3"`` or ``W/"3"``); absent: no check."""
    if value is None or not value.strip():
        return None
    raw = value.strip().removeprefix("W/").strip('"')
    try:
        return int(raw)
    except ValueError:
        raise HTTPException(status_code=422, detail="If-Match: a row version number") from None


async def _write_org(db: AsyncSession) -> uuid.UUID:
    org = await _built_store(db)
    if org is None:
        raise HTTPException(status_code=404, detail="No organization profile yet")
    return org


async def _audit_rows(
    db: AsyncSession, admin: User, action: str, entity_id: Any, entity_name: str, summary: str, details: dict[str, Any]
) -> None:
    from app.services.audit import log_audit

    await log_audit(
        db,
        user=admin,
        action=action,
        entity_type="organization_context",
        entity_id=entity_id,
        entity_name=entity_name,
        summary=summary[:500],
        details=details,
    )


def _actor(admin: User) -> str:
    return f"form:{admin.id}"


@router.post("/assets", status_code=201)
async def create_asset_record(
    body: AssetRecordRequest,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> dict[str, Any]:
    """Add one asset by hand (administrators, audited); 422 names the bad field or the missing group."""
    from app.services import organization_asset_records as records

    org = await _write_org(db)
    try:
        record = await records.create_record(db, org, body.model_dump(), origin="form", actor=_actor(admin))
    except records.RecordError as exc:
        await db.rollback()
        raise _record_http_error(exc) from None
    out = records.record_out(record)
    await _audit_rows(db, admin, "create", record.id, "asset", f"Asset {record.name} added", {"source": record.source})
    await db.commit()
    return out


@router.post("/assets/records/batch")
async def upsert_asset_records(
    body: AssetRecordBatchRequest,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> dict[str, Any]:
    """Create or update up to 10,000 rows by ``(source, external_id)``; only the rows that differ are written."""
    from app.services import organization_asset_records as records

    org = await _write_org(db)
    try:
        result = await records.upsert_records(db, org, body.rows, origin="api", actor=f"api:{admin.id}")
    except records.RecordError as exc:
        await db.rollback()
        raise _record_http_error(exc) from None
    counts = result.as_dict()
    if result.created or result.updated:
        await _audit_rows(db, admin, "update", None, "assets", f"Asset rows written: {counts}", counts)
    await db.commit()
    return counts


@router.patch("/assets/records/{record_id}")
async def update_asset_record(
    record_id: uuid.UUID,
    body: AssetRecordPatchRequest,
    if_match: str | None = Header(default=None, alias="If-Match"),
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> dict[str, Any]:
    """Change one asset record (administrators, audited); 409 on a stale ``If-Match``, 422 on a synced field."""
    from app.services import organization_asset_records as records

    org = await _write_org(db)
    changes = body.model_dump()
    try:
        record = await records.update_record(
            db, org, record_id, changes, origin="form", actor=_actor(admin), if_match=_if_match(if_match)
        )
    except records.RecordError as exc:
        await db.rollback()
        raise _record_http_error(exc) from None
    out = records.record_out(record)
    await _audit_rows(
        db, admin, "update", record.id, "asset", f"Asset {record.name} changed: " + ", ".join(sorted(changes)), {}
    )
    await db.commit()
    return out


@router.delete("/assets/records/{record_id}", status_code=204)
async def delete_asset_record(
    record_id: uuid.UUID,
    if_match: str | None = Header(default=None, alias="If-Match"),
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> Response:
    """Delete one asset record (administrators, audited); its identity cluster alone is re-matched."""
    from app.services import organization_asset_records as records

    org = await _write_org(db)
    try:
        record = await records.get_record(db, org, record_id)
        name = record.name
        await records.delete_records(
            db, org, [record_id], origin="form", actor=_actor(admin), if_match=_if_match(if_match)
        )
    except records.RecordError as exc:
        await db.rollback()
        raise _record_http_error(exc) from None
    await _audit_rows(db, admin, "delete", record_id, "asset", f"Asset {name} deleted", {})
    await db.commit()
    return Response(status_code=204)


@router.post("/assets/bulk-delete")
async def bulk_delete_asset_records(
    body: AssetRecordBulkDeleteRequest,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> dict[str, Any]:
    """Delete up to 1,000 asset records in one call (an id of another organization is skipped)."""
    from app.services import organization_asset_records as records

    org = await _write_org(db)
    try:
        gone = await records.delete_records(db, org, body.ids, origin="form", actor=_actor(admin))
    except records.RecordError as exc:
        await db.rollback()
        raise _record_http_error(exc) from None
    await _audit_rows(
        db, admin, "delete", None, "assets", f"{len(gone)} assets deleted", {"ids": [str(i) for i in gone]}
    )
    await db.commit()
    return {"removed": [str(i) for i in gone]}


@router.get("/assets/records/{record_id}")
async def get_asset_record(
    record_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> dict[str, Any]:
    """One asset record with its row version (what ``If-Match`` takes)."""
    from app.services import organization_asset_records as records

    org = await _built_store(db)
    if org is None:
        raise HTTPException(status_code=404, detail="No asset record with this id")
    try:
        return records.record_out(await records.get_record(db, org, record_id))
    except records.RecordError as exc:
        raise _record_http_error(exc) from None


@router.get("/asset-groups")
async def list_asset_groups(
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> dict[str, Any]:
    """Every asset group with its id, row version and asset count (what the row APIs take)."""
    from app.services import organization_asset_records as records

    org = await _built_store(db)
    return {"items": await records.groups_out(db, org) if org is not None else []}


@router.post("/asset-groups", status_code=201)
async def create_asset_group(
    body: AssetGroupRequest,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> dict[str, Any]:
    """Add an asset group (administrators, audited); the records that name it join it."""
    from app.services import organization_asset_records as records

    org = await _write_org(db)
    try:
        group = await records.upsert_group(db, org, body.model_dump(), origin="form", actor=_actor(admin))
    except records.RecordError as exc:
        await db.rollback()
        raise _record_http_error(exc) from None
    out = records.group_out(group)
    await _audit_rows(db, admin, "create", group.id, "asset_group", f"Asset group {group.name} added", {})
    await db.commit()
    return out


@router.patch("/asset-groups/{group_id}")
async def update_asset_group(
    group_id: uuid.UUID,
    body: AssetGroupRequest,
    if_match: str | None = Header(default=None, alias="If-Match"),
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> dict[str, Any]:
    """Change an asset group (administrators, audited); 409 on a stale ``If-Match``."""
    from app.services import organization_asset_records as records

    org = await _write_org(db)
    try:
        group = await records.upsert_group(
            db,
            org,
            body.model_dump(),
            origin="form",
            actor=_actor(admin),
            group_id=group_id,
            if_match=_if_match(if_match),
        )
    except records.RecordError as exc:
        await db.rollback()
        raise _record_http_error(exc) from None
    out = records.group_out(group)
    await _audit_rows(db, admin, "update", group.id, "asset_group", f"Asset group {group.name} changed", {})
    await db.commit()
    return out


@router.delete("/asset-groups/{group_id}", status_code=204)
async def delete_asset_group(
    group_id: uuid.UUID,
    if_match: str | None = Header(default=None, alias="If-Match"),
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> Response:
    """Delete an asset group; its assets stay and lose the group (each change logged)."""
    from app.services import organization_asset_records as records

    org = await _write_org(db)
    try:
        group = await records.get_group(db, org, group_id)
        name = group.name
        await records.delete_group(db, org, group_id, origin="form", actor=_actor(admin), if_match=_if_match(if_match))
    except records.RecordError as exc:
        await db.rollback()
        raise _record_http_error(exc) from None
    await _audit_rows(db, admin, "delete", group_id, "asset_group", f"Asset group {name} deleted", {})
    await db.commit()
    return Response(status_code=204)


@router.get("/asset-groups/{group_id}")
async def get_asset_group(
    group_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> dict[str, Any]:
    """One asset group (#1556): its row, its distinct asset count and the members its source named but the inventory lacks."""
    from app.services import organization_asset_records as records

    org = await _built_store(db)
    if org is None:
        raise HTTPException(status_code=404, detail="No asset group with this id")
    try:
        group = await records.get_group(db, org, group_id)
    except records.RecordError as exc:
        raise _record_http_error(exc) from None
    return await records.group_detail(db, org, group)


@router.get("/asset-groups/{group_id}/members")
async def list_asset_group_members(
    group_id: uuid.UUID,
    limit: int = Query(default=100, ge=1, le=1000),
    after_name: str | None = Query(default=None, max_length=200),
    after_id: uuid.UUID | None = Query(default=None),
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> dict[str, Any]:
    """One keyset page of a group's member records (#1556), each with who put it there (``via``).

    Pass the last item's ``name_key`` and ``id`` as ``after_name`` and ``after_id``
    for the next page; ``next`` is null on the last one.
    """
    from app.services import organization_asset_records as records

    org = await _built_store(db)
    if org is None:
        raise HTTPException(status_code=404, detail="No asset group with this id")
    try:
        group = await records.get_group(db, org, group_id)
    except records.RecordError as exc:
        raise _record_http_error(exc) from None
    after = (after_name, after_id) if after_name is not None and after_id is not None else None
    page = await records.group_members(db, org, group.id, limit=limit + 1, after=after)
    items = [{**records.record_out(r), "name_key": r.name_key, "via": via} for r, via in page[:limit]]
    last = items[-1] if len(page) > limit and items else None
    return {
        "group": records.group_out(group),
        "items": items,
        "next": {"after_name": last["name_key"], "after_id": last["id"]} if last else None,
    }


@router.put("/crown-jewels/{crown_jewel}/links")
async def set_crown_jewel_asset_links(
    crown_jewel: str,
    body: CrownJewelLinksRequest,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> dict[str, Any]:
    """Replace the assets and groups a crown jewel covers; 422 names the ones that match nothing."""
    from app.services import organization_asset_records as records

    org = await _write_org(db)
    current = await org_context.current_version(db)
    jewels = {
        str(j.get("name") or "").strip().lower()
        for j in ((current.profile or {}) if current else {}).get("crown_jewels") or []
    }
    if crown_jewel.strip().lower() not in jewels:
        raise HTTPException(status_code=404, detail=f"No crown jewel is named '{crown_jewel}'")
    missing = await records.set_crown_jewel_links(db, org, crown_jewel, body.names)
    if missing:
        await db.rollback()
        raise HTTPException(status_code=422, detail="No asset or asset group is named: " + ", ".join(missing))
    await _audit_rows(
        db, admin, "update", None, "crown_jewel_links", f"Crown jewel {crown_jewel} covers {len(body.names)} items", {}
    )
    await db.commit()
    return {"crown_jewel": crown_jewel, "names": body.names}


@router.get("/changes")
async def list_asset_changes(
    entity_id: uuid.UUID | None = None,
    since: datetime | None = None,
    before_id: int | None = Query(None, ge=1),
    limit: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    _admin: User = Depends(require_admin),
) -> dict[str, Any]:
    """The asset and group change log, newest first (one record or group with *entity_id*); page with *before_id*."""
    from app.services import organization_asset_records as records

    org = await _built_store(db)
    items = (
        await records.changes(db, org, entity_id=entity_id, since=since, before_id=before_id, limit=limit)
        if org is not None
        else []
    )
    return {"items": items, "next_before_id": items[-1]["id"] if len(items) == limit else None}


@router.post("/changes/restore")
async def restore_asset_changes(
    body: AssetChangesRestoreRequest,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> dict[str, Any]:
    """Undo record changes by id: a delete comes back, an update gets its old fields, a create goes."""
    from app.services import organization_asset_records as records

    org = await _write_org(db)
    try:
        result = await records.restore(db, org, body.change_ids, actor=f"restore:{admin.id}")
    except records.RecordError as exc:
        await db.rollback()
        raise _record_http_error(exc) from None
    counts = result.as_dict()
    await _audit_rows(
        db, admin, "update", None, "assets", f"Asset changes restored: {counts}", {"changes": body.change_ids}
    )
    await db.commit()
    return counts


class AssetSbomImportResponse(BaseModel):
    id: str
    #: ``cyclonedx`` or ``spdx``.
    format: str
    #: What the document describes; importing the same document again replaces it.
    document: str
    components: int
    unknown_versions: int
    bundled: int
    #: True: the same document was imported before on this asset and is replaced.
    replaced: bool


@router.post("/assets/{asset_id}/sbom", response_model=AssetSbomImportResponse, status_code=201)
async def import_asset_sbom(
    asset_id: uuid.UUID,
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> AssetSbomImportResponse:
    """Import a CycloneDX or SPDX JSON SBOM on an asset: its components join the asset's software (source ``sbom``).

    Administrators, audited. The same document again replaces its components;
    another document adds to them. 404: unknown asset; 422: a file refused
    (not JSON, not CycloneDX or SPDX, over 10,000 components or 20 MB).
    """
    from app.services import sbom_parser
    from app.services.audit import log_audit
    from app.services.organization_asset_store import SbomImportError, import_sbom

    data = await file.read(sbom_parser.MAX_BYTES + 1)
    org = await _built_store(db)
    if org is None:
        raise HTTPException(status_code=404, detail="Asset not found")
    try:
        result = await import_sbom(db, org, asset_id, data=data, file_name=file.filename or "", author_user_id=admin.id)
    except SbomImportError as exc:
        await db.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from None
    except sbom_parser.SbomError as exc:
        await db.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from None
    await log_audit(
        db,
        user=admin,
        action="configure",
        entity_type="organization_context",
        entity_id=asset_id,
        entity_name="asset_sbom",
        summary=f"SBOM {result['document']} imported on an asset: {result['components']} component(s)",
        details={k: result[k] for k in ("format", "document", "components", "replaced")},
    )
    await db.commit()
    return AssetSbomImportResponse(**result)


@router.get("/assets/{asset_id}")
async def get_canonical_asset(
    asset_id: uuid.UUID,
    software_offset: int = Query(0, ge=0),
    software_limit: int = Query(100, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> dict[str, Any]:
    """One canonical asset: each field with the source that set it and every source's value, its records, its software."""
    from app.services.organization_asset_store import get_asset

    org = await _built_store(db)
    found = (
        await get_asset(db, org, asset_id, software_offset=software_offset, software_limit=software_limit)
        if org is not None
        else None
    )
    if found is None:
        raise HTTPException(status_code=404, detail="Asset not found")
    return found


@router.post("/opencrq-sync", response_model=OpenCrqSyncResponse)
async def sync_organization_context_with_opencrq(
    body: OpenCrqSyncRequest,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> OpenCrqSyncResponse:
    """Reconcile the crown jewels with OpenCRQ's supporting assets both ways (administrators).

    A dry run answers the plan; ``apply`` saves the next profile version and
    writes the criticalities to OpenCRQ. 409 when OpenCRQ cannot be read.
    """
    from app.services.organization_context_opencrq import run_sync

    try:
        plan, errors = await run_sync(
            db, user_id=admin.id, apply=body.apply, tenant_id=body.tenant_id, min_criticality=body.min_criticality
        )
        if plan is None:
            raise HTTPException(status_code=409, detail="; ".join(errors))
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(
            status_code=409, detail="Another version was saved at the same time. Run the sync again."
        ) from None
    current = await org_context.current_version(db)
    return OpenCrqSyncResponse(
        applied=body.apply,
        plan=plan.summary(),
        errors=errors,
        version=current.version if current else None,
    )


class OrganizationFieldReader(BaseModel):
    fleet_id: str
    name: str


class OrganizationAgentReader(BaseModel):
    agent_id: str
    name: str


class OrganizationReadersResponse(BaseModel):
    #: Profile field -> the fleets the caller can see whose documentation needs it.
    readers: dict[str, list[OrganizationFieldReader]]
    #: Profile field -> the seeded agents the caller can see whose persona reads it (#10063 r1 A3).
    agents: dict[str, list[OrganizationAgentReader]] = {}


@router.get("/readers", response_model=OrganizationReadersResponse)
async def list_organization_context_readers(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> OrganizationReadersResponse:
    """The fleets and seeded agents that read each part of the profile.

    Fleets from their documented prerequisites, agents from what their persona
    reads. Rows only (no run, no model call); only what the caller can see.
    """
    from app.services.organization_context_readers import profile_field_readers

    readers, agents = await profile_field_readers(db, user)
    return OrganizationReadersResponse(
        readers={field: [OrganizationFieldReader(**row) for row in rows] for field, rows in readers.items()},
        agents={field: [OrganizationAgentReader(**row) for row in rows] for field, rows in agents.items()},
    )


@router.get("/pirs", response_model=OrganizationPirsResponse)
async def list_organization_pirs(
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> OrganizationPirsResponse:
    """The connected OpenCTI's PIRs, by name, for the PIR picker (administrators).

    Read with the administrator's own OpenCTI token; never fails: an OpenCTI
    not connected or without PIRs answers an empty list and the reason, and
    the form keeps the ids typed by hand.
    """
    from app.services.organization_context_draft import backfill_pir_names, list_pirs

    pirs, unavailable, opencti_url = await list_pirs(db, user_id=admin.id)
    if pirs:
        # The lazy backfill (#11589): a profile saved with ids alone gains the names OpenCTI just gave.
        await backfill_pir_names(db, pirs)
    return OrganizationPirsResponse(
        pirs=[OrganizationPir(**row) for row in pirs],
        unavailable=unavailable,  # type: ignore[arg-type]
        opencti_url=opencti_url,
    )


class AiDraftProposal(BaseModel):
    field: str
    value: Any
    source: str
    confidence: Literal["high", "medium", "low"]
    basis: str
    #: An inferred amount: shown "Check this", never pre-accepted.
    check: bool
    #: The current value; equal to ``value`` for an unchanged field (shown under "Unchanged (n)", never offered).
    current: Any
    #: The current version already holds a different value: never pre-accepted.
    conflict: bool


class AiDraftOrganizationContextResponse(BaseModel):
    based_on_version: int | None
    proposals: list[AiDraftProposal]
    #: Contract fields the profile misses and no input supports (never guessed).
    not_supported: list[str]
    #: PIRs the model suggests creating: suggestions only, never created.
    suggested_pirs: list[str]
    unavailable: list[str]
    notes: list[str]
    usage: dict[str, int]


@router.post("/ai-draft", response_model=AiDraftOrganizationContextResponse)
async def ai_draft_organization_context(
    description: str = Form(default=""),
    website: str = Form(default=""),
    use_platforms: bool = Form(default=True),
    tenant_id: str = Form(default=""),
    documents: list[UploadFile] | None = File(default=None),
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
    # A model call on the administrator's click: refused when AI is off or unlicensed.
    _gate: None = Depends(require_agentic_ai),
) -> AiDraftOrganizationContextResponse:
    """Per-field proposals from a model over the inputs given (administrators); nothing is saved.

    One metered agentic request on the administrator's quota, refused (429)
    before anything is read. Documents are read in memory for this request
    only; the website is fetched only when given. 422 names a bad input or an
    unusable model answer. Save the accepted fields with ``PUT`` and source
    ``ai_draft``.
    """
    from app.services import organization_context_ai_draft as ai
    from app.services.agent_chat import enforce_agentic_quota
    from app.services.agentic_executions import SOURCE_ORGANIZATION_AI_DRAFT

    documents = documents or []
    if len(documents) > ai.MAX_DOCUMENTS:
        raise HTTPException(status_code=422, detail=f"At most {ai.MAX_DOCUMENTS} documents can be read by one draft.")
    held: list[ai.DraftDocument] = []
    for upload in documents:
        content = await upload.read(ai.DOCUMENT_MAX_BYTES + 1)
        held.append(ai.DraftDocument(name=(upload.filename or "document")[:200], content=content))
    inputs = ai.AiDraftInputs(
        description=description, documents=held, website=website, use_platforms=use_platforms, tenant_id=tenant_id
    )
    try:
        inputs.validate()
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    refusal = await enforce_agentic_quota(db, str(admin.id), source=SOURCE_ORGANIZATION_AI_DRAFT)
    if refusal:
        raise HTTPException(status_code=429, detail=refusal)
    current = await org_context.current_version(db)
    try:
        draft = await ai.draft_with_ai(
            db,
            user_id=admin.id,
            inputs=inputs,
            current=org_context.parse_profile(current.profile if current else {}),
            based_on_version=current.version if current else None,
        )
    except (ai.AiDraftError, ValueError) as exc:
        await db.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from None
    await db.rollback()
    return AiDraftOrganizationContextResponse(
        based_on_version=draft.based_on_version,
        proposals=[AiDraftProposal(**p.as_dict()) for p in draft.proposals],
        not_supported=draft.not_supported,
        suggested_pirs=draft.suggested_pirs,
        unavailable=draft.unavailable,
        notes=draft.notes,
        usage=draft.usage,
    )


class OrganizationContextProposalResponse(BaseModel):
    id: str
    organization_id: str | None
    kind: Literal["fields", "new_tracked"]
    #: ``fields``: ``{field, value, basis, current, conflict}``; ``new_tracked``: ``{name, relationship, criticality, basis}``.
    proposals: list[dict[str, Any]]
    based_on_version: int | None
    status: str
    author_user_id: str | None
    conversation_id: str | None
    created_at: datetime


class DecideOrganizationContextProposalRequest(BaseModel):
    #: The fields (or organization names) accepted; empty rejects the proposal.
    accept: list[str] = []
    #: ``new_tracked``: accepted name -> the OpenCTI Organization picked (over the draft's own match).
    opencti_ids: dict[str, str] = {}


class DecideOrganizationContextProposalResponse(BaseModel):
    status: Literal["accepted", "rejected"]
    #: The profile version an accepted ``fields`` proposal saved.
    version: int | None
    #: The tracked organizations an accepted ``new_tracked`` proposal created (monitoring off).
    created: list[str]


@router.get("/proposals", response_model=list[OrganizationContextProposalResponse])
async def list_organization_context_proposals(
    status: Literal["pending", "accepted", "rejected"] = Query(default="pending"),
    limit: int = Query(default=50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    _admin: User = Depends(require_admin),
) -> list[OrganizationContextProposalResponse]:
    """The changes proposed from chat, newest first (administrators; #9484, S-a2)."""
    from app.services.organization_context_proposals import list_proposals

    return [
        OrganizationContextProposalResponse(
            id=str(row.id),
            organization_id=str(row.organization_id) if row.organization_id else None,
            kind=row.kind,  # type: ignore[arg-type]
            proposals=list(row.proposals or []),
            based_on_version=row.based_on_version,
            status=row.status,
            author_user_id=str(row.author_user_id) if row.author_user_id else None,
            conversation_id=str(row.conversation_id) if row.conversation_id else None,
            created_at=row.created_at,
        )
        for row in await list_proposals(db, status=status, limit=limit)
    ]


@router.post("/proposals/{proposal_id}/decide", response_model=DecideOrganizationContextProposalResponse)
async def decide_organization_context_proposal(
    proposal_id: uuid.UUID,
    body: DecideOrganizationContextProposalRequest,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> DecideOrganizationContextProposalResponse:
    """Accept the named entries of a pending proposal, or reject it with none (administrators).

    Accepting fields saves the next profile version with source ``ai_draft``;
    accepting organizations creates them tracked, monitoring off. 404 for an
    unknown proposal, 409 for one already decided or a version saved at the
    same time, 422 naming an entry that is not in the proposal.
    """
    from app.models.organization_context import OrganizationContextProposal
    from app.services import organization_context_proposals as proposals

    row = await db.get(OrganizationContextProposal, proposal_id, with_for_update=True)
    if row is None:
        raise HTTPException(status_code=404, detail="No such proposal.")
    if row.status != "pending":
        raise HTTPException(status_code=409, detail=f"This proposal is already {row.status}.")
    try:
        outcome = await proposals.decide(
            db, row, admin_user_id=admin.id, accept=body.accept, opencti_ids=body.opencti_ids
        )
        await db.commit()
    except proposals.ProposalError as exc:
        await db.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from None
    except IntegrityError:
        await db.rollback()
        raise HTTPException(
            status_code=409, detail="Another version was saved at the same time. Reload and decide again."
        ) from None
    return DecideOrganizationContextProposalResponse(**outcome)


@router.post("/draft", response_model=DraftOrganizationContextResponse)
async def draft_organization_context(
    body: DraftOrganizationContextRequest,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> DraftOrganizationContextResponse:
    """Propose a profile from what OpenCTI, OpenAEV and OpenCRQ hold (administrators).

    Only the empty fields of the current profile are filled; nothing is
    saved: the administrator reviews the draft and saves it with ``PUT``
    and source ``draft``.
    """
    from app.services.organization_context_draft import draft_profile

    draft, version = await draft_profile(db, user_id=admin.id, tenant_id=body.tenant_id)
    return DraftOrganizationContextResponse(
        based_on_version=version,
        profile=draft.profile.model_dump(mode="json"),
        filled=draft.filled,
        unavailable=draft.unavailable,
        missing_fields=org_context.missing_fields(draft.profile),
    )


class ImportSourceStatus(BaseModel):
    source: str
    available: bool
    #: Why the platform cannot feed the section (empty when it can).
    reason: str = ""
    #: The reason as a code the dialog words in the user's language: ``not_connected``, ``no_token`` or empty.
    reason_code: str = ""
    #: The filters this source supports for the section.
    filters: list[str]


class ImportSection(BaseModel):
    section: str
    #: The profile field the section writes.
    field: str
    sources: list[ImportSourceStatus]


class ImportSourcesResponse(BaseModel):
    sections: list[ImportSection]


class ImportRequest(BaseModel):
    section: Literal["assets", "asset_groups", "ai_systems", "tech_stack", "crown_jewels", "pirs"]
    source: Literal[
        "openaev", "opencti", "opencrq", "intune", "defender", "tanium", "crowdstrike", "servicenow", "github", "gitlab"
    ]
    filters: dict[str, Any] = {}
    #: The asset type of the batch, for a source without a type taxonomy (OpenCTI Systems).
    batch_type: str = ""
    #: With apply: the version the preview was computed on; another save since is 409.
    based_on_version: int | None = None
    #: Remove the rows this source wrote before that it no longer returns (default: kept).
    remove_gone: bool = False


class ImportPreviewResponse(BaseModel):
    based_on_version: int | None
    plan: dict[str, Any]
    applied: bool = False
    version: int | None = None


@router.get("/import/sources", response_model=ImportSourcesResponse)
async def list_organization_import_sources(
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> ImportSourcesResponse:
    """Which platform can feed each profile section for this administrator, and why not (#11780)."""
    from app.services import organization_context_import as imp

    statuses = await imp.source_statuses(db, user_id=admin.id)
    sections = [
        ImportSection(
            section=section,
            field=imp.SECTION_FIELD[section],
            sources=[
                ImportSourceStatus(
                    source=source,
                    available=statuses[source].available,
                    reason=statuses[source].reason,
                    reason_code=statuses[source].reason_code,
                    filters=list(imp.SOURCE_FILTERS.get((section, source), ())),
                )
                for source in imp.SECTION_SOURCES[section]
            ],
        )
        for section in imp.SECTION_SOURCES
    ]
    return ImportSourcesResponse(sections=sections)


class ImportFilterOption(BaseModel):
    value: str
    label: str
    #: One line about the value, as the source words it (empty when it has none).
    description: str = ""
    #: The category of a subcategory (empty otherwise).
    parent: str = ""


class ImportFilterOptionsResponse(BaseModel):
    #: filter key -> the values the source offers for it, read live.
    options: dict[str, list[ImportFilterOption]]
    #: filter key -> why the source offers none: not_connected, unreadable or no_system_taxonomy (an older OpenCTI).
    unavailable: dict[str, Literal["not_connected", "unreadable", "no_system_taxonomy"]]


@router.get("/import/options", response_model=ImportFilterOptionsResponse)
async def list_organization_import_options(
    section: Literal["assets", "asset_groups", "ai_systems", "tech_stack", "crown_jewels", "pirs"],
    source: Literal[
        "openaev", "opencti", "opencrq", "intune", "defender", "tanium", "crowdstrike", "servicenow", "github", "gitlab"
    ],
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> ImportFilterOptionsResponse:
    """The values each import filter offers in that platform, so the dialog picks instead of typing (#11886)."""
    from app.services import organization_context_import as imp

    found = await imp.filter_options(db, user_id=admin.id, section=section, source=source)
    return ImportFilterOptionsResponse(
        options={k: [ImportFilterOption(**o) for o in v] for k, v in found.options.items()},
        unavailable=found.unavailable,
    )


@router.post("/import/preview", response_model=ImportPreviewResponse)
async def preview_organization_import(
    body: ImportRequest,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> ImportPreviewResponse:
    """What importing one section from one platform would do, before anything is written (#11780).

    No model call: the source's own count of matching entities, then new,
    updated, unchanged and skipped rows, and a sample.
    """
    from app.services import organization_context_import as imp

    plan, version = await imp.preview_import(
        db,
        user_id=admin.id,
        section=body.section,
        source=body.source,
        filters=body.filters,
        batch_type=body.batch_type,
        remove_gone=body.remove_gone,
    )
    # The plan of assets and groups reads the records; their first touch imports the profile rows (#1543).
    await db.commit()
    return ImportPreviewResponse(based_on_version=version, plan=plan.as_dict())


@router.post("/import/apply", response_model=ImportPreviewResponse)
async def apply_organization_import(
    body: ImportRequest,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> ImportPreviewResponse:
    """Import one section from one platform and save the next profile version (#11780).

    The source is read again: what is saved is what the preview counted
    unless the platform changed in between. A platform that cannot be read is
    422 with its reason, nothing saved; another save since the preview is 409.
    """
    from app.services import organization_context_import as imp

    plan, version = await imp.preview_import(
        db,
        user_id=admin.id,
        section=body.section,
        source=body.source,
        filters=body.filters,
        batch_type=body.batch_type,
        remove_gone=body.remove_gone,
    )
    # The records' first-touch import (#1543) lands whatever the answer below.
    await db.commit()
    if plan.error:
        raise HTTPException(status_code=422, detail=plan.error)
    if body.based_on_version is not None and body.based_on_version != version:
        raise HTTPException(status_code=409, detail="The profile changed since the preview. Preview again.")
    response = ImportPreviewResponse(based_on_version=version, plan=plan.as_dict())
    if plan.on_records:
        # Assets and asset groups (#1543): row writes on the records, no profile version, no 5,000 cap.
        from app.services.audit import log_audit

        written = await imp.apply_record_import(db, plan, actor=f"import:{admin.id}") if plan.has_writes else 0
        changed = await imp.store_endpoint_software(db, plan, user_id=admin.id) if plan.software else 0
        await log_audit(
            db,
            user=admin,
            action="update",
            entity_type="organization_context",
            entity_id=str(plan.organization_id),
            entity_name=plan.section,
            summary=imp.provenance(plan, body.filters)[imp.SECTION_FIELD[plan.section]]["basis"],
            details={"section": plan.section, "source": plan.source, "rows_written": written},
        )
        await db.commit()
        response.applied = bool(written or changed)
        return response
    removes = plan.remove_gone and len(plan.gone) > len(plan.gone_kept)
    if not (plan.new or plan.updated or removes) or plan.profile is None:
        if not plan.error and plan.software:
            # An endpoint source (#1251): only the software changed (a re-run with new versions).
            changed = await imp.store_endpoint_software(db, plan, user_id=admin.id)
            await db.commit()
            response.applied = bool(changed)
        return response
    try:
        saved = await org_context.save_version(
            db, plan.profile, source="draft", author_user_id=admin.id, sources=imp.provenance(plan, body.filters)
        )
        if not plan.error and plan.software:
            await imp.store_endpoint_software(db, plan, user_id=admin.id)
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(
            status_code=409, detail="Another version was saved at the same time. Reload and import again."
        ) from None
    response.applied = True
    response.version = saved.version
    return response


class OrganizationSyncRequest(BaseModel):
    #: A section of the import, or ``all`` for the global schedule that runs every section sync.
    section: Literal["assets", "asset_groups", "ai_systems", "tech_stack", "crown_jewels", "pirs", "all"]
    source: Literal[
        "openaev",
        "opencti",
        "opencrq",
        "intune",
        "defender",
        "tanium",
        "crowdstrike",
        "servicenow",
        "github",
        "gitlab",
        "",
    ] = ""
    filters: dict[str, Any] = {}
    batch_type: str = ""
    #: A deletion upstream deletes the row here (default since #1134).
    remove_gone: bool = True
    #: The source ids this sync never imports (#1134); omitted: the stored list is kept.
    excluded_external_ids: list[str] | None = None
    #: inbound: the platform is the master; outbound: the XTM One list is (#1134 S3, GET /syncs/directions).
    #: Omitted: the stored direction is kept (inbound for a new sync), so an edit never resets an outbound sync.
    direction: Literal["inbound", "outbound"] | None = None
    frequency: Literal["hourly", "daily", "weekly"] = "daily"
    hour: int = 6
    minute: int = 0
    #: 0 Monday ... 6 Sunday (weekly).
    weekday: int = 0
    timezone: str = "UTC"
    enabled: bool = True


class OrganizationSyncResponse(BaseModel):
    id: str
    section: str
    source: str
    filters: dict[str, Any]
    batch_type: str
    remove_gone: bool
    excluded_external_ids: list[str] = []
    direction: str = "inbound"
    #: Outbound: how many platform rows this sync created and keeps in step.
    outbound_count: int = 0
    frequency: str
    hour: int
    minute: int
    weekday: int
    timezone: str
    enabled: bool
    #: Why the sync stopped by itself (a missing or revoked token); empty while it runs.
    paused_reason: str
    owner_email: str | None = None
    next_run_at: datetime | None = None
    last_run_at: datetime | None = None
    #: ``{added, updated, removed, version, error}`` of the last run.
    last_result: dict[str, Any] | None = None


async def _sync_response(db: AsyncSession, row: Any) -> OrganizationSyncResponse:
    owner = await db.get(User, row.owner_user_id) if row.owner_user_id else None
    return OrganizationSyncResponse(
        id=str(row.id),
        section=row.section,
        source=row.source,
        filters=row.filters or {},
        batch_type=row.batch_type,
        remove_gone=row.remove_gone,
        excluded_external_ids=list(row.excluded_external_ids or []),
        direction=getattr(row, "direction", None) or "inbound",
        outbound_count=len(getattr(row, "outbound_ids", None) or {}),
        frequency=row.frequency,
        hour=row.hour,
        minute=row.minute,
        weekday=row.weekday,
        timezone=row.timezone,
        enabled=row.enabled,
        paused_reason=row.paused_reason,
        owner_email=owner.email if owner else None,
        next_run_at=row.next_run_at,
        last_run_at=row.last_run_at,
        last_result=row.last_result,
    )


def _apply_sync_body(row: Any, body: OrganizationSyncRequest, admin: User) -> None:
    """Write *body* on *row*, refused with 422 when the scheduler could not run it."""
    from app.services import organization_context_sync as osync

    try:
        # Saving again is how a paused sync resumes: it now reads with this administrator's token.
        osync.define(row, owner_id=admin.id, **body.model_dump())
    except osync.SyncRefusedError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None


class SyncDirectionSupport(BaseModel):
    section: str
    source: str
    directions: list[str]
    #: Why outbound is not offered (empty when it is).
    reason: str = ""


@router.get("/syncs/directions", response_model=list[SyncDirectionSupport])
async def list_organization_sync_directions(
    admin: User = Depends(require_admin),
) -> list[SyncDirectionSupport]:
    """Per section and platform, the directions a sync may take and why outbound is not offered (#1134 S3)."""
    from app.services import organization_context_import as imp
    from app.services import organization_context_outbound as outbound

    return [
        SyncDirectionSupport(
            section=section,
            source=source,
            directions=list(outbound.directions(section, source)[0]),
            reason=outbound.directions(section, source)[1],
        )
        for section, sources in imp.SECTION_SOURCES.items()
        for source in sources
    ]


@router.get("/syncs", response_model=list[OrganizationSyncResponse])
async def list_organization_syncs(
    scope: Literal["profile", "inventory", ""] = "",
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> list[OrganizationSyncResponse]:
    """The scheduled syncs, global first (#11913); *scope* keeps one location's sections (#1134 S2)."""
    from app.services import organization_context_sync as osync

    return [await _sync_response(db, row) for row in await osync.all_syncs(db, scope)]


@router.post("/syncs", response_model=OrganizationSyncResponse, status_code=201)
async def create_organization_sync(
    body: OrganizationSyncRequest,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> OrganizationSyncResponse:
    """Keep one section in sync with one platform on a schedule, or schedule them all (#11913)."""
    from app.models.organization_context import OrganizationContextSync
    from app.services import organization_context_sync as osync
    from app.services.audit import log_audit

    if body.section == osync.ALL_SECTIONS and await osync.global_sync_exists(db):
        raise HTTPException(status_code=409, detail="A Sync all schedule exists already: edit it.")
    row = OrganizationContextSync()
    _apply_sync_body(row, body, admin)
    db.add(row)
    await db.flush()
    await log_audit(
        db,
        user=admin,
        action="create",
        entity_type="organization_context_sync",
        entity_id=row.id,
        entity_name=f"{row.section} from {row.source or 'every source'}",
        summary=f"Organization sync of {row.section} scheduled {row.frequency}",
        details=body.model_dump(),
    )
    await db.commit()
    return await _sync_response(db, row)


async def _sync_or_404(db: AsyncSession, sync_id: uuid.UUID) -> Any:
    from app.models.organization_context import OrganizationContextSync

    row = await db.get(OrganizationContextSync, sync_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Sync not found")
    return row


@router.put("/syncs/{sync_id}", response_model=OrganizationSyncResponse)
async def update_organization_sync(
    sync_id: uuid.UUID,
    body: OrganizationSyncRequest,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> OrganizationSyncResponse:
    """Change a sync; saving it again also resumes a paused one with this administrator's token (#11913)."""
    from app.services.audit import log_audit

    row = await _sync_or_404(db, sync_id)
    if (row.section == "all") != (body.section == "all"):
        raise HTTPException(status_code=422, detail="A section sync cannot become Sync all, nor the reverse.")
    _apply_sync_body(row, body, admin)
    await log_audit(
        db,
        user=admin,
        action="update",
        entity_type="organization_context_sync",
        entity_id=row.id,
        entity_name=f"{row.section} from {row.source or 'every source'}",
        summary=f"Organization sync of {row.section} changed ({'enabled' if row.enabled else 'disabled'})",
        details=body.model_dump(),
    )
    await db.commit()
    return await _sync_response(db, row)


@router.delete("/syncs/{sync_id}", status_code=204)
async def delete_organization_sync(
    sync_id: uuid.UUID,
    rows: Literal["leave", "release", "purge"] = "leave",
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> None:
    """Stop and remove a sync (#11913); *rows* says what becomes of the rows it wrote (#1276).

    ``leave`` (the default, unchanged for every caller) keeps them as they are; ``release`` keeps
    them and hands them to people (editable, provenance kept in the saved version); ``purge``
    removes only the rows this sync wrote, never a row a person or another source wrote.
    """
    from app.services.audit import log_audit

    row = await _sync_or_404(db, sync_id)
    plan_summary: dict[str, Any] | None = None
    if rows != "leave" and row.source:
        plan_summary = await _save_rows_plan(db, admin, source=row.source, mode=rows, sync=row)
    await log_audit(
        db,
        user=admin,
        action="delete",
        entity_type="organization_context_sync",
        entity_id=row.id,
        entity_name=f"{row.section} from {row.source or 'every source'}",
        summary=f"Organization sync of {row.section} deleted, its rows {_ROWS_VERB[rows]}"[:500],
        details={"rows": rows, **(plan_summary or {})},
    )
    await db.delete(row)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(
            status_code=409, detail="Another version was saved at the same time. Delete it again."
        ) from None


#: How the audit summary names what became of a deleted sync's rows.
_ROWS_VERB = {"leave": "kept", "release": "kept and handed to people", "purge": "purged"}


async def _save_rows_plan(
    db: AsyncSession, admin: User, *, source: str, mode: str, sync: Any = None
) -> dict[str, Any] | None:
    """Purge or release a source's rows (one sync's when *sync* is given) as a new version; the caller commits."""
    from app.services import organization_context_sources as osources

    # The asset lists come from the records (#1543): a plan on the stored JSON would re-save stale lists.
    _current, base = await org_context.current_profile(db)
    try:
        plan = osources.plan_rows(
            base,
            source=source,
            mode=mode,
            sync_id=str(sync.id) if sync is not None else "",
            fields=osources.sync_fields(sync.section) if sync is not None else None,
        )
    except osources.SourceRefusedError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    if not plan.count or plan.profile is None:
        return plan.as_dict()
    label = f"sync {sync.section} from {osources.display_name(osources.source_key(source))}" if sync is not None else ""
    sources = {
        attr: {"source": plan.source, "basis": osources.plan_basis(plan, who=admin.email, sync_label=label)}
        for attr in plan.touched
    }
    saved = await org_context.save_version(db, plan.profile, source="draft", author_user_id=admin.id, sources=sources)
    return {**plan.as_dict(), "version": saved.version}


class RowsPlanResponse(BaseModel):
    source: str
    mode: str
    count: int
    #: Profile field -> rows touched.
    counts: dict[str, int]
    #: Profile field -> the first names touched.
    rows: dict[str, list[str]]
    #: Profile field -> rows of other sources with the same name, kept as they are.
    kept_elsewhere: dict[str, list[str]]
    version: int | None = None


@router.get("/syncs/{sync_id}/rows-preview", response_model=RowsPlanResponse)
async def preview_organization_sync_rows(
    sync_id: uuid.UUID,
    mode: Literal["release", "purge"] = "purge",
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> RowsPlanResponse:
    """What deleting a sync with *mode* would do to the rows it wrote; nothing saved (#1276)."""
    from app.services import organization_context_sources as osources

    row = await _sync_or_404(db, sync_id)
    _current, base = await org_context.current_profile(db)
    if not row.source:
        return RowsPlanResponse(source="", mode=mode, count=0, counts={}, rows={}, kept_elsewhere={})
    plan = osources.plan_rows(
        base, source=row.source, mode=mode, sync_id=str(row.id), fields=osources.sync_fields(row.section)
    )
    return RowsPlanResponse(**plan.as_dict())


class SyncHistoryEntry(BaseModel):
    at: datetime
    action: str
    by: str | None = None
    summary: str
    trigger: str = ""
    added: int | None = None
    updated: int | None = None
    removed: int | None = None
    version: int | None = None
    error: str | None = None


@router.get("/syncs/{sync_id}/history", response_model=list[SyncHistoryEntry])
async def organization_sync_history(
    sync_id: uuid.UUID,
    limit: int = Query(default=20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> list[SyncHistoryEntry]:
    """The last runs of a sync that changed the profile or failed, and its saves, newest first (#1276)."""
    from app.services import organization_context_sources as osources

    await _sync_or_404(db, sync_id)
    return [SyncHistoryEntry(**entry) for entry in await osources.sync_history(db, sync_id, limit=limit)]


class OrganizationSourceResponse(BaseModel):
    key: str
    #: ``platform``, ``agent`` (an agent or a partner writing through the API), ``csv``, ``api`` or ``manual``.
    kind: str
    display_name: str
    #: Profile field -> rows of this source.
    rows: dict[str, int]
    total: int
    #: The agent that last wrote this source's rows, when a saved version names it.
    written_by_agent: str = ""
    sync_ids: list[str] = []


@router.get("/sources", response_model=list[OrganizationSourceResponse])
async def list_organization_sources(
    scope: Literal["profile", "inventory", ""] = "",
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> list[OrganizationSourceResponse]:
    """Every source of rows of the profile, with its kind, name, rows per field and syncs (#1276)."""
    from app.services import organization_context_sources as osources
    from app.services import organization_context_sync as osync

    _current, base = await org_context.current_profile(db)
    syncs = await osync.all_syncs(db, scope)
    writers = await osources.writer_agents(db)
    return [
        OrganizationSourceResponse(**summary.as_dict())
        for summary in osources.summarize(base, syncs, scope=scope, writers=writers)
    ]


@router.get("/sources/{source}/rows-preview", response_model=RowsPlanResponse)
async def preview_organization_source_rows(
    source: str,
    mode: Literal["release", "purge"] = "purge",
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> RowsPlanResponse:
    """What purging or releasing every row of *source* would do; nothing saved (#1276)."""
    from app.services import organization_context_sources as osources

    _current, base = await org_context.current_profile(db)
    try:
        plan = osources.plan_rows(base, source=source, mode=mode)
    except osources.SourceRefusedError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    return RowsPlanResponse(**plan.as_dict())


class SourceRowsRequest(BaseModel):
    mode: Literal["release", "purge"] = "purge"


@router.post("/sources/{source}/rows", response_model=RowsPlanResponse)
async def apply_organization_source_rows(
    source: str,
    body: SourceRowsRequest,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> RowsPlanResponse:
    """Purge or release every row *source* wrote (an agent, a partner, a CSV import), as a new version (#1276)."""
    from app.services.audit import log_audit

    outcome = await _save_rows_plan(db, admin, source=source, mode=body.mode) or {}
    if outcome.get("version") is not None:
        await log_audit(
            db,
            user=admin,
            action="delete" if body.mode == "purge" else "update",
            entity_type="organization_context",
            entity_name=source[:200],
            summary=f"Organization rows of {source} {_ROWS_VERB[body.mode]}: {outcome.get('count', 0)}"[:500],
            details=outcome,
        )
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status_code=409, detail="Another version was saved at the same time. Try again.") from None
    return RowsPlanResponse(**outcome)


@router.post("/syncs/{sync_id}/run", response_model=OrganizationSyncResponse)
async def run_organization_sync(
    sync_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> OrganizationSyncResponse:
    """Run a sync now, outside its schedule; its next scheduled run is unchanged (#11913)."""
    from app.services import organization_context_sync as osync

    row = await _sync_or_404(db, sync_id)
    try:
        await osync.run_now(db, row, trigger=f"run now by {admin.email}")
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(
            status_code=409, detail="Another version was saved at the same time. Run it again."
        ) from None
    return await _sync_response(db, row)


class FillAgentRequest(BaseModel):
    integration_id: str
    #: True: also create the scheduled assignment that keeps the assets in sync.
    schedule: bool = False
    cron: str = "0 6 * * *"
    timezone: str | None = None


class FillAgentResponse(BaseModel):
    agent_id: str
    agent_name: str
    created: bool
    assignment_id: str | None = None
    #: The preview instruction the chat starts with.
    prompt: str


@router.post("/fill-agent", response_model=FillAgentResponse)
async def create_organization_fill_agent(
    body: FillAgentRequest,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
    # It creates an agent (and a scheduled assignment): refused when AI is off or unlicensed.
    _gate: None = Depends(require_agentic_ai),
) -> FillAgentResponse:
    """An agent that fills the assets from one connected integration, and its optional sync (#11888).

    Bound to the integration read-only and to the ``organization_context``
    group, reused when the administrator already has one for that integration.
    No model call here: the chat or the scheduled run does the reading.
    """
    from sqlalchemy import select

    from app.api.v1.helpers import get_user_group_ids, parse_uuid, user_visible_filter
    from app.core.utils import validate_cron_expression
    from app.models.integration import Integration
    from app.services import organization_context_fill_agent as fill

    integration_id = parse_uuid(body.integration_id, label="integration_id")
    groups = await get_user_group_ids(db, admin.id)
    integration = (
        await db.execute(
            select(Integration).where(
                Integration.id == integration_id,
                user_visible_filter(Integration, admin.id, groups, is_admin=True),
            )
        )
    ).scalar_one_or_none()
    if integration is None:
        raise HTTPException(status_code=404, detail="Integration not found.")
    try:
        cron = validate_cron_expression(body.cron)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    made = await fill.ensure_fill_agent(
        db, admin_id=admin.id, integration=integration, schedule=body.schedule, cron=cron, timezone=body.timezone
    )
    await db.commit()
    return FillAgentResponse(
        agent_id=str(made.agent.id),
        agent_name=made.agent.name,
        created=made.created,
        assignment_id=str(made.assignment.id) if made.assignment else None,
        prompt=made.prompt,
    )


class SetupPromptResponse(BaseModel):
    #: True when the caller must be shown the proposal now.
    propose: bool
    #: The instance's recorded answer to the current version (``started``, ``later``,
    #: ``dismissed``), ``None`` while unanswered.
    outcome: str | None
    answered_at: datetime | None
    #: The setup version this build carries (``ctem-loop-1``).
    version: str
    #: The version the instance last answered (``legacy`` for the unversioned #9173 answer).
    answered_version: str | None = None
    #: Whether an organization profile was ever saved (the flow opens on that step when not).
    has_profile: bool = False
    #: Step key -> ``done`` or ``skipped`` for the current version (the flow resumes past them).
    steps: dict[str, str] = {}


class AnswerSetupPromptRequest(BaseModel):
    outcome: Literal["started", "later", "dismissed"]


if TYPE_CHECKING:
    from app.services.organization_setup_prompt import SetupPromptState


class SetupStepRequest(BaseModel):
    step: Literal["organization", "modules", "ecosystem", "gates", "preview", "enable"]
    state: Literal["done", "skipped"]


def _setup_response(state: SetupPromptState, *, has_profile: bool) -> SetupPromptResponse:
    return SetupPromptResponse(
        propose=state.propose,
        outcome=state.outcome,
        answered_at=state.answered_at,
        version=state.version,
        answered_version=state.answered_version,
        has_profile=has_profile,
        steps=dict(state.steps),
    )


@router.get("/setup-prompt", response_model=SetupPromptResponse)
async def get_setup_prompt(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> SetupPromptResponse:
    """Whether the first-connection setup is proposed to the caller (#9173 S3, versioned by #1087).

    Every signed-in user may ask; only an administrator of an XTM One
    platform on an instance that never answered the current setup version
    gets ``propose: true`` (at install, and once after an upgrade carrying a
    new version). A non-administrator reads ``propose: false`` and no step.
    A row read, no write.
    """
    from app.api.v1.platform_settings_helpers import get_platform_settings
    from app.core.config import settings
    from app.services import organization_setup_prompt as prompt

    row = await get_platform_settings(db)
    is_admin = bool(user.is_admin)
    propose = prompt.should_propose(is_admin=is_admin, is_xtm_one=settings.is_xtm_one, row=row)
    state = prompt.state_of(propose=propose, row=row)
    if not (is_admin and settings.is_xtm_one):
        return SetupPromptResponse(propose=False, outcome=None, answered_at=None, version=state.version)
    return _setup_response(state, has_profile=await prompt.has_saved_profile(db))


@router.post("/setup-prompt", response_model=SetupPromptResponse)
async def answer_setup_prompt(
    body: AnswerSetupPromptRequest,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> SetupPromptResponse:
    """Record the instance's answer to the current setup version, once (administrators, audited).

    The first answer to a version wins; a later call answers the recorded state unchanged.
    """
    from app.api.v1.platform_settings_helpers import get_platform_settings
    from app.services import organization_setup_prompt as prompt
    from app.services.audit import log_audit

    row = await get_platform_settings(db)
    if prompt.record_answer(row, outcome=body.outcome, user_id=admin.id):
        await log_audit(
            db,
            user=admin,
            action="configure",
            entity_type="organization_context",
            entity_name="setup_prompt",
            summary=f"Answered the setup proposal {prompt.SETUP_VERSION}: {body.outcome}",
            details={"outcome": body.outcome, "version": prompt.SETUP_VERSION},
        )
    await db.commit()
    state = prompt.state_of(propose=False, row=row)
    return _setup_response(state, has_profile=await prompt.has_saved_profile(db))


@router.put("/setup-prompt/steps", response_model=SetupPromptResponse)
async def record_setup_step(
    body: SetupStepRequest,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> SetupPromptResponse:
    """Record one step of the guided setup as done or skipped (administrators, #1087).

    Each step saves on its own so leaving the flow midway loses nothing; the
    next opening resumes past the recorded steps. A progress record only: the
    step's own change (profile, slot binding, autonomy, enable) goes through
    its own endpoint and its own audit.
    """
    from app.api.v1.platform_settings_helpers import get_platform_settings
    from app.services import organization_setup_prompt as prompt

    row = await get_platform_settings(db)
    prompt.record_step(row, step=body.step, state=body.state)
    await db.commit()
    state = prompt.state_of(propose=prompt.should_propose(is_admin=True, is_xtm_one=True, row=row), row=row)
    return _setup_response(state, has_profile=await prompt.has_saved_profile(db))


@router.delete("/setup-prompt", response_model=SetupPromptResponse)
async def reset_setup_prompt(
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> SetupPromptResponse:
    """Forget the answer and step progress of the setup proposal (administrators, audited).

    Settings > Platform "Reset onboarding": the guided setup is proposed again
    to administrators from its first step, as after a fresh install. Only the
    proposal state is cleared; the organization profile, integrations, fleets,
    automations and slot bindings are not changed. XTM One mode only: in
    Copilot mode there is no setup to propose, so the call is refused (409).
    """
    from app.api.v1.platform_settings_helpers import get_platform_settings
    from app.core.config import settings
    from app.services import organization_setup_prompt as prompt
    from app.services.audit import log_audit

    if settings.is_copilot_like:
        raise HTTPException(status_code=409, detail="The guided setup exists in XTM One mode only.")
    row = await get_platform_settings(db)
    if prompt.reset_answer(row):
        await log_audit(
            db,
            user=admin,
            action="configure",
            entity_type="organization_context",
            entity_name="setup_prompt",
            summary=f"Reset the setup proposal {prompt.SETUP_VERSION}",
            details={"version": prompt.SETUP_VERSION},
        )
    await db.commit()
    state = prompt.state_of(propose=prompt.should_propose(is_admin=True, is_xtm_one=True, row=row), row=row)
    return _setup_response(state, has_profile=await prompt.has_saved_profile(db))
