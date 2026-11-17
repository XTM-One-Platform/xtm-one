# SPDX-License-Identifier: Apache-2.0

"""Organizations API (#9157, slice M1): the primary organization and the tracked ones.

``GET /api/v1/organizations`` lists them (every signed-in user, the primary
first); ``POST`` adds a tracked organization, ``PATCH /{id}`` edits it and
``POST /{id}/archive`` archives it (administrators). Each organization's
profile is ``GET`` / ``PUT /{id}/context`` with the sources of
``/api/v1/organization-context``, which stays and answers the primary.
Monitoring is never switched on here: a tracked organization starts ``off``
(``dev-docs/ctem-fleets.md`` section 9.7). The rules are
``services/organizations.py``'s.

Slice M3 (section 9.4): ``GET /opencti-identities?search=`` lists the
OpenCTI Organizations an administrator picks from (``GET
/opencti-identities/{identity_id}`` reads one, so a linked organization is
named in the Edit drawer, #9375 slice 3), ``POST
/{id}/opencti-identity`` links one (or creates it on ``create``), and
``POST /opencti-pirs`` plans the PIRs of the tracked organizations (a dry
run unless ``apply``), ``services/organization_opencti.py``'s rules.

Slice S-c of #9484 (section 6.1, "Tracked organizations"): ``POST
/tracked/ai-draft`` drafts tracked organizations with a model from one name,
a pasted list or a CSV or XLSX file, stored as one ``new_tracked`` proposal
(nothing created); ``GET /tracked/ai-draft/{proposal_id}`` reads it back
for the review page; the decision is ``POST
/api/v1/organization-context/proposals/{id}/decide``.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_db, require_admin, require_agentic_ai
from app.models.organization import Organization
from app.models.user import User
from app.services import organization_context as org_context
from app.services import organizations as orgs
from app.services.organization_opencti import Gql

router = APIRouter()

Relationship = Literal["provider", "customer", "partner", "subsidiary"]
Criticality = Literal["low", "medium", "high", "critical"]


class OrganizationResponse(BaseModel):
    id: str
    name: str
    role: str
    relationship: str | None
    criticality: str
    opencti_organization_id: str | None
    monitoring: str
    daily_run_cap: int = 5
    created_at: datetime
    archived_at: datetime | None


class CreateOrganizationRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    relationship: Relationship
    criticality: Criticality = "medium"
    opencti_organization_id: str | None = Field(default=None, max_length=100)


class UpdateOrganizationRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    relationship: Relationship | None = None
    criticality: Criticality | None = None
    opencti_organization_id: str | None = Field(default=None, max_length=100)
    #: Runs its OpenCTI events may start per day (UTC); the extra ones run the next day as one digest (M6).
    daily_run_cap: int | None = Field(default=None, ge=1, le=100)


class RunEstimateResponse(BaseModel):
    organization_id: str
    monitoring: str
    daily_run_cap: int
    window_days: int
    assignments: list[dict[str, Any]]
    expected_runs_per_month: int


class OrganizationContextResponse(BaseModel):
    organization_id: str
    version: int | None
    source: str | None
    created_at: datetime | None
    profile: dict[str, Any]
    missing_fields: list[str]


class SaveContextRequest(BaseModel):
    profile: dict[str, Any]
    source: Literal["form", "draft", "api"] = "form"


class OpenCtiIdentity(BaseModel):
    id: str
    name: str
    aliases: list[str]
    organization_type: str | None


class LinkIdentityRequest(BaseModel):
    #: The OpenCTI Organization picked; empty with ``create`` to create one named like the organization.
    identity_id: str = Field(default="", max_length=100)
    create: bool = False


class LinkIdentityResponse(BaseModel):
    organization: OrganizationResponse
    organization_type_set: str | None


class PirSyncRequest(BaseModel):
    apply: bool = False
    #: ``pir_filters``: confidence at least this (0: no filter).
    min_confidence: int = Field(default=0, ge=0, le=100)


class PirSyncResponse(BaseModel):
    applied: bool
    report: dict[str, Any]


class TrackedAiDraftResponse(BaseModel):
    #: The ``new_tracked`` proposal holding the rows: the review page's URL and the decide call.
    proposal_id: str
    status: str
    #: One row per name (``organization_tracked_ai_draft.Row``; ``status`` pending, conflict, error,
    #: accepted or rejected).
    rows: list[dict[str, Any]]
    #: Names read from the inputs; this run drafts at most the first 50.
    names_read: int
    names_drafted: int
    #: Metered agentic requests this run used (one per batch of 10 names not yet tracked).
    requests: int
    #: Why OpenCTI was not searched, when it was not.
    opencti_unavailable: str | None = None
    created_at: datetime


def _tracked_draft_out(row: Any, *, names_read: int, requests: int, unavailable: str | None) -> TrackedAiDraftResponse:
    rows = list(row.proposals or [])
    return TrackedAiDraftResponse(
        proposal_id=str(row.id),
        status=row.status,
        rows=rows,
        names_read=names_read,
        names_drafted=len(rows),
        requests=requests,
        opencti_unavailable=unavailable,
        created_at=row.created_at,
    )


@router.post("/tracked/ai-draft", response_model=TrackedAiDraftResponse, status_code=201)
async def ai_draft_tracked_organizations(
    names: str = Form(default=""),
    relationship: Relationship = Form(default="provider"),
    file: UploadFile | None = File(default=None),
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
    # A model call on the administrator's click: refused when AI is off or unlicensed.
    _gate: None = Depends(require_agentic_ai),
) -> TrackedAiDraftResponse:
    """Draft tracked organizations with a model (administrators); nothing is created.

    *names* holds one name per line; *file* is a CSV or XLSX whose first
    column is read. At most 50 names per run, one metered agentic request per
    batch of 10 names not yet tracked, the first refused (429) before any
    model call. The rows are stored as one pending ``new_tracked`` proposal;
    accepting rows creates them with monitoring off.
    """
    from app.llm.router import get_llm_for_user
    from app.services import organization_context_proposals as proposals
    from app.services import organization_tracked_ai_draft as tracked_ai
    from app.services.agent_chat import enforce_agentic_quota
    from app.services.agentic_executions import SOURCE_ORGANIZATION_AI_DRAFT
    from app.services.organization_opencti import opencti_gql, search_identities

    content = await file.read(tracked_ai.FILE_MAX_BYTES + 1) if file is not None else None
    try:
        read = tracked_ai.read_names(names, (file.filename or "") if file is not None else "", content)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    if not read:
        raise HTTPException(status_code=422, detail="No organization name was read: give one name per line or a file.")
    existing = {o.name.lower(): str(o.id) for o in await orgs.list_organizations(db) if o.role == "tracked"}
    fresh = [n for n in read[: tracked_ai.MAX_NAMES] if n.lower() not in existing]
    metered = 0
    if fresh:
        # The first batch is metered before anything else runs, so a spent quota is a plain 429.
        refusal = await enforce_agentic_quota(db, str(admin.id), source=SOURCE_ORGANIZATION_AI_DRAFT)
        if refusal:
            raise HTTPException(status_code=429, detail=refusal)
        metered = 1
        llm, model = await get_llm_for_user(db, str(admin.id))
    else:
        llm, model = None, None
    primary = await org_context.current_version(db)
    model_call = tracked_ai.model_batch_call(
        llm, model, default_relationship=relationship, primary=dict(primary.profile) if primary else {}
    )
    calls = 0

    async def call_batch(batch: list[str]) -> list[tracked_ai.Row]:
        nonlocal calls, metered
        calls += 1
        if calls > metered:
            refusal = await enforce_agentic_quota(db, str(admin.id), source=SOURCE_ORGANIZATION_AI_DRAFT)
            if refusal:
                raise tracked_ai.TrackedDraftError(refusal)
            metered += 1
        return await model_call(batch)

    gql = await opencti_gql(db, str(admin.id))
    unavailable = gql if isinstance(gql, str) else None
    search: tracked_ai.IdentitySearch | None = None
    if not isinstance(gql, str):
        connected = gql

        async def search(name: str) -> list[dict[str, Any]]:
            return await search_identities(connected, name)

    rows = await tracked_ai.draft_rows(read, call_batch=call_batch, tracked=existing, search=search)
    try:
        stored = await proposals.store_tracked_draft(db, author_user_id=admin.id, rows=[r.as_dict() for r in rows])
        await db.commit()
    except proposals.ProposalError as exc:
        await db.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from None
    await db.refresh(stored)
    return _tracked_draft_out(stored, names_read=len(read), requests=metered, unavailable=unavailable)


@router.get("/tracked/ai-draft/{proposal_id}", response_model=TrackedAiDraftResponse)
async def read_tracked_organizations_ai_draft(
    proposal_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _admin: User = Depends(require_admin),
) -> TrackedAiDraftResponse:
    """One tracked-organizations draft by its proposal id, for the review page (administrators)."""
    from app.models.organization_context import OrganizationContextProposal

    row = await db.get(OrganizationContextProposal, proposal_id)
    if row is None or row.kind != "new_tracked":
        raise HTTPException(status_code=404, detail="No such draft.")
    return _tracked_draft_out(row, names_read=len(row.proposals or []), requests=0, unavailable=None)


async def _opencti(db: AsyncSession, admin: User) -> Gql:
    from app.services.organization_opencti import opencti_gql

    gql = await opencti_gql(db, str(admin.id))
    if isinstance(gql, str):
        raise HTTPException(status_code=409, detail=gql)
    return gql


def _out(o: Organization) -> OrganizationResponse:
    return OrganizationResponse(
        id=str(o.id),
        name=o.name,
        role=o.role,
        relationship=o.relationship,
        criticality=o.criticality,
        opencti_organization_id=o.opencti_organization_id,
        monitoring=o.monitoring,
        daily_run_cap=o.daily_run_cap or 5,
        created_at=o.created_at,
        archived_at=o.archived_at,
    )


async def _load(db: AsyncSession, organization_id: uuid.UUID) -> Organization:
    found = await orgs.get_organization(db, organization_id)
    if found is None:
        raise HTTPException(status_code=404, detail="Organization not found")
    return found


async def _context(db: AsyncSession, organization: Organization) -> OrganizationContextResponse:
    row = await org_context.current_version(db, organization.id)
    profile = orgs.parse_for(organization, row.profile if row else {})
    return OrganizationContextResponse(
        organization_id=str(organization.id),
        version=row.version if row else None,
        source=row.source if row else None,
        created_at=row.created_at if row else None,
        profile=profile.model_dump(mode="json"),
        missing_fields=orgs.missing_for(organization, profile),
    )


@router.get("", response_model=list[OrganizationResponse])
async def list_organizations(
    include_archived: bool = False,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> list[OrganizationResponse]:
    """The primary organization first, then the tracked ones by name."""
    return [_out(o) for o in await orgs.list_organizations(db, include_archived=include_archived)]


@router.get("/opencti-identities", response_model=list[OpenCtiIdentity])
async def search_opencti_identities(
    search: str,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> list[OpenCtiIdentity]:
    """OpenCTI Organizations matching *search* by name or alias, exact matches first (administrators)."""
    from app.services.organization_opencti import search_identities

    if not search.strip():
        raise HTTPException(status_code=422, detail="search is required")
    gql = await _opencti(db, admin)
    try:
        nodes = await search_identities(gql, search.strip()[:200])
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)[:300]) from None
    return [_identity_out(n) for n in nodes]


@router.get("/opencti-identities/{identity_id}", response_model=OpenCtiIdentity)
async def read_opencti_identity(
    identity_id: str,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> OpenCtiIdentity:
    """One OpenCTI Organization by id, so a linked organization is named, never shown as its id (administrators)."""
    from app.services.organization_opencti import read_identity

    if not identity_id.strip() or len(identity_id) > 100:
        raise HTTPException(status_code=422, detail="identity_id must be 1 to 100 characters")
    gql = await _opencti(db, admin)
    try:
        node = await read_identity(gql, identity_id.strip())
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)[:300]) from None
    if not node or not node.get("id"):
        raise HTTPException(status_code=404, detail="OpenCTI holds no such Organization")
    return _identity_out(node)


def _identity_out(n: dict[str, Any]) -> OpenCtiIdentity:
    return OpenCtiIdentity(
        id=str(n["id"]),
        name=str(n.get("name") or ""),
        aliases=[str(a) for a in (n.get("x_opencti_aliases") or [])],
        organization_type=n.get("x_opencti_organization_type") or None,
    )


@router.post("/opencti-pirs", response_model=PirSyncResponse)
async def sync_opencti_pirs(
    body: PirSyncRequest,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> PirSyncResponse:
    """Plan the PIRs of the tracked organizations (D2 = A); ``apply`` writes them and each profile's ``pir_ids``."""
    from app.services.organization_opencti import sync_pirs

    gql = await _opencti(db, admin)
    try:
        report = await sync_pirs(db, gql, user_id=admin.id, apply=body.apply, min_confidence=body.min_confidence)
        await db.commit()
    except RuntimeError as exc:
        await db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)[:300]) from None
    except IntegrityError:
        await db.rollback()
        raise HTTPException(
            status_code=409, detail="Another version was saved at the same time. Run the sync again."
        ) from None
    return PirSyncResponse(applied=body.apply, report=report.summary())


@router.post("", response_model=OrganizationResponse, status_code=201)
async def create_organization(
    body: CreateOrganizationRequest,
    db: AsyncSession = Depends(get_db),
    _admin: User = Depends(require_admin),
) -> OrganizationResponse:
    """Add a tracked organization (administrators); monitoring starts off."""
    try:
        organization = await orgs.create_tracked(
            db,
            name=body.name,
            relationship=body.relationship,
            criticality=body.criticality,
            opencti_organization_id=body.opencti_organization_id,
        )
        await db.commit()
    except ValueError as exc:
        await db.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from None
    await db.refresh(organization)
    return _out(organization)


@router.patch("/{organization_id}", response_model=OrganizationResponse)
async def update_organization(
    organization_id: uuid.UUID,
    body: UpdateOrganizationRequest,
    db: AsyncSession = Depends(get_db),
    _admin: User = Depends(require_admin),
) -> OrganizationResponse:
    """Edit an organization (administrators); the primary keeps its role and no relationship."""
    organization = await _load(db, organization_id)
    if body.name is not None:
        organization.name = body.name.strip()[:200]
    if body.relationship is not None:
        if organization.role == "primary":
            raise HTTPException(status_code=422, detail="the primary organization has no relationship")
        organization.relationship = body.relationship
    if body.criticality is not None:
        organization.criticality = body.criticality
    if "opencti_organization_id" in body.model_fields_set:
        organization.opencti_organization_id = body.opencti_organization_id or None
    if body.daily_run_cap is not None:
        if organization.role == "primary":
            raise HTTPException(status_code=422, detail="the daily run cap applies to tracked organizations")
        organization.daily_run_cap = body.daily_run_cap
    await db.commit()
    await db.refresh(organization)
    return _out(organization)


@router.get("/{organization_id}/run-estimate", response_model=RunEstimateResponse)
async def organization_run_estimate(
    organization_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> RunEstimateResponse:
    """Expected runs per month before monitoring is switched on (administrators, M6).

    One read-only OpenCTI count per enabled assignment whose trigger serves
    this organization, over the last 30 days, bounded by its daily cap. No
    model call, nothing saved (``dev-docs/ctem-fleets.md`` section 9.7).
    """
    from app.services.organization_run_cap import estimate_runs

    organization = await _load(db, organization_id)
    if organization.role != "tracked":
        raise HTTPException(status_code=422, detail="the run estimate applies to tracked organizations")
    gql = await _opencti(db, admin)
    return RunEstimateResponse(**await estimate_runs(db, gql, organization))


@router.post("/{organization_id}/opencti-identity", response_model=LinkIdentityResponse)
async def link_opencti_identity(
    organization_id: uuid.UUID,
    body: LinkIdentityRequest,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> LinkIdentityResponse:
    """Link a tracked organization to its OpenCTI Organization (administrators).

    The type is set only when OpenCTI holds none; a new Organization is created only with ``create``.
    """
    from app.services.organization_opencti import link_identity

    organization = await _load(db, organization_id)
    if organization.role != "tracked":
        raise HTTPException(status_code=422, detail="only a tracked organization links to an OpenCTI identity")
    if not body.identity_id and not body.create:
        raise HTTPException(status_code=422, detail="pick an OpenCTI Organization, or confirm its creation")
    gql = await _opencti(db, admin)
    try:
        identity_id, type_set = await link_identity(
            gql,
            name=organization.name,
            relationship=organization.relationship or "",
            identity_id=body.identity_id.strip(),
            create=body.create,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)[:300]) from None
    organization.opencti_organization_id = identity_id
    await db.commit()
    await db.refresh(organization)
    return LinkIdentityResponse(organization=_out(organization), organization_type_set=type_set)


@router.post("/{organization_id}/archive", response_model=OrganizationResponse)
async def archive_organization(
    organization_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _admin: User = Depends(require_admin),
) -> OrganizationResponse:
    """Archive a tracked organization (administrators): kept with its versions, out of the list."""
    organization = await _load(db, organization_id)
    try:
        orgs.archive(organization)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    await db.commit()
    await db.refresh(organization)
    return _out(organization)


@router.get("/{organization_id}/context", response_model=OrganizationContextResponse)
async def get_organization_profile(
    organization_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> OrganizationContextResponse:
    """The current profile of one organization (empty, every field missing, before its first save)."""
    return await _context(db, await _load(db, organization_id))


@router.put("/{organization_id}/context", response_model=OrganizationContextResponse)
async def save_organization_profile(
    organization_id: uuid.UUID,
    body: SaveContextRequest,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> OrganizationContextResponse:
    """Save the next version of one organization's profile (administrators); 422 names the bad field."""
    organization = await _load(db, organization_id)
    try:
        profile = orgs.parse_for(organization, body.profile)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    try:
        await org_context.save_version(
            db, profile, source=body.source, author_user_id=admin.id, organization_id=organization.id
        )
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(
            status_code=409, detail="Another version was saved at the same time. Reload and save again."
        ) from None
    return await _context(db, organization)
