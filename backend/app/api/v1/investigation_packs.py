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

"""Deep Investigation pack builder routes.

CRUD, versions and sharing of the packs users build, the built-in packs to
duplicate, and a preview of what a pack would do on a subject for the caller.
Three-tier visibility like every Agent Studio entity; who may edit is
``helpers.can_manage``. A built pack runs from the picker like any pack, as
``pack:<uuid>``, resolved for the runner by `investigation_pack_resolver`.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, require_agentic_ai, require_ai_enabled
from app.api.v1.helpers import (
    ENTITY_LIST_SCOPE_DESCRIPTION,
    EntityListScope,
    apply_group_share_fields,
    apply_owner_transfer,
    apply_user_share_fields,
    can_manage,
    get_manageable_entity,
    get_user_group_ids,
    handle_company_managed_toggle,
    list_scope_admin_bypass,
    parse_path_id,
    require_admin_for_company_managed,
    resolve_owner_names,
    user_visible_filter,
    user_visible_query,
    wants_access_restriction,
)
from app.api.v1.schemas.investigation_pack import (
    BuiltinPackItem,
    InvestigationPackCreate,
    InvestigationPackResponse,
    InvestigationPackUpdate,
    InvestigationPackVersionDetail,
    InvestigationPackVersionItem,
    PackAssistRequest,
    PackAssistResponse,
    PackPreviewRequest,
    PackPreviewResponse,
    PreviewAction,
    PreviewSource,
)
from app.core.database import get_db
from app.core.rate_limit import enforce_rate_limit
from app.core.utils import format_exception_message
from app.models.investigation_pack import InvestigationPack, InvestigationPackVersion
from app.models.user import User
from app.services.audit import log_audit
from app.services.investigation_pack_builder import (
    MAX_PACKS_PER_OWNER,
    PackDefinitionError,
    PackFields,
    built_pack_slug,
    definition_from_builtin,
    parse_definition,
)

router = APIRouter()
logger = logging.getLogger(__name__)

_NOT_FOUND = "Investigation pack not found"


def _to_response(pack: InvestigationPack, *, can_manage: bool, owner_name: str | None = None) -> InvestigationPackResponse:
    return InvestigationPackResponse(
        id=str(pack.id),
        slug=built_pack_slug(pack.id),
        user_id=str(pack.user_id) if pack.user_id else None,
        owner_name=owner_name,
        name=pack.name,
        description=pack.description or "",
        subject_types=list(pack.subject_types or []),
        recommended=bool(pack.recommended),
        max_iterations=int(pack.max_iterations or 10),
        definition=dict(pack.definition or {}),
        version_number=int(pack.version_number or 1),
        is_company_managed=bool(pack.is_company_managed),
        shared_group_ids=pack.shared_group_ids,
        shared_group_permissions=pack.shared_group_permissions,
        shared_user_ids=pack.shared_user_ids,
        shared_user_permissions=pack.shared_user_permissions,
        access_restricted=pack.access_restricted,
        can_manage=can_manage,
        created_at=pack.created_at.isoformat(),
        updated_at=pack.updated_at.isoformat(),
    )


async def _visible_pack(db: AsyncSession, user: User, pack_id: str, group_ids: list[str]) -> InvestigationPack:
    """A pack the caller can see (admins: any), or the unknown-id 404."""
    pack_uuid = parse_path_id(pack_id, not_found_detail=_NOT_FOUND)
    result = await db.execute(
        select(InvestigationPack).where(
            InvestigationPack.id == pack_uuid,
            user_visible_filter(InvestigationPack, user.id, group_ids, is_admin=user.is_admin),
        )
    )
    pack = result.scalar_one_or_none()
    if pack is None:
        raise HTTPException(status_code=404, detail=_NOT_FOUND)
    return pack


async def _owner_name(db: AsyncSession, pack: InvestigationPack) -> str | None:
    if not pack.user_id:
        return None
    return (await db.execute(select(User.full_name).where(User.id == pack.user_id))).scalar_one_or_none()


def _content(pack: InvestigationPack, body: PackFields) -> bool:
    """Write the content of ``body``; True when the method itself changed (a
    new version), not only its name or description."""
    definition = body.definition.model_dump(mode="json", exclude_none=True)
    changed = definition != (pack.definition or {}) or list(body.subject_types) != list(pack.subject_types or [])
    pack.name = body.name
    pack.description = body.description
    pack.subject_types = list(body.subject_types)
    pack.recommended = body.recommended
    pack.max_iterations = body.max_iterations
    pack.definition = definition
    return changed


async def _add_version(db: AsyncSession, pack: InvestigationPack, user: User, message: str | None) -> InvestigationPackVersion:
    version = InvestigationPackVersion(
        pack_id=pack.id,
        version_number=pack.version_number,
        definition=pack.definition,
        subject_types=list(pack.subject_types or []),
        author_id=user.id,
        commit_message=message,
    )
    db.add(version)
    return version


# ── The built-in packs, to duplicate ────────────────────────────────────────


def _builtin_packs() -> dict:
    """The built-in packs with every source they can hold: a copy is the
    method, not what the copier's integrations happen to allow (the copy is
    resolved for each runner like any built pack)."""
    from app.services import investigation_packs as packs

    out = {slug: pack for slug, pack in packs.BUILTIN_PACKS.items()}
    disinfo = packs.disinfo_infrastructure_pack(("virustotal", "VirusTotal"), ("opencti", "OpenCTI"))
    out[disinfo.slug] = disinfo
    return out


@router.get("/catalog")
async def get_builder_catalog(user: User = Depends(get_current_user)):
    """What the builder offers: source kinds, reference sources, keyword and
    fingerprint sets, icons, the settings a source exposes, the limits."""
    from app.services.investigation_pack_builder import builder_catalog

    return builder_catalog()


@router.get("/builtin", response_model=list[BuiltinPackItem])
async def list_builtin_packs(user: User = Depends(get_current_user)):
    """The built-in packs, offered to duplicate."""
    return [
        BuiltinPackItem(slug=p.slug, label=p.label, description=p.description, subject_types=list(p.subject_types))
        for p in _builtin_packs().values()
    ]


@router.post("/from-builtin/{slug}", response_model=InvestigationPackResponse, status_code=status.HTTP_201_CREATED)
async def duplicate_builtin_pack(
    slug: str,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _agentic_gate: None = Depends(require_agentic_ai),
):
    """A private copy of a built-in pack, to edit."""
    builtin = _builtin_packs().get(slug)
    if builtin is None:
        raise HTTPException(status_code=404, detail="Built-in pack not found")
    body = InvestigationPackCreate.model_validate(definition_from_builtin(builtin))
    return await _create(db, user, body, request, summary=f"Duplicated the built-in pack '{builtin.label}'")


# ── CRUD ────────────────────────────────────────────────────────────────────


@router.get("", response_model=list[InvestigationPackResponse])
async def list_investigation_packs(
    scope: EntityListScope = Query("visible", description=ENTITY_LIST_SCOPE_DESCRIPTION),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Packs visible to the caller (own, shared, company-managed); ``scope=all``
    lists every pack for an admin."""
    group_ids = await get_user_group_ids(db, user.id)
    result = await db.execute(
        user_visible_query(
            InvestigationPack, user.id, group_ids, is_admin=list_scope_admin_bypass(scope, user)
        )
    )
    packs = result.scalars().all()
    names = await resolve_owner_names(db, packs)
    return [
        _to_response(p, can_manage=can_manage(user, p, group_ids), owner_name=names.get(str(p.user_id)))
        for p in packs
    ]


async def _create(
    db: AsyncSession, user: User, body: InvestigationPackCreate, request: Request, *, summary: str
) -> InvestigationPackResponse:
    require_admin_for_company_managed(body, user, "Only admins can create company-managed investigation packs")
    owned = (
        await db.execute(select(func.count()).select_from(InvestigationPack).where(InvestigationPack.user_id == user.id))
    ).scalar_one()
    if owned >= MAX_PACKS_PER_OWNER:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"You already own {MAX_PACKS_PER_OWNER} investigation packs. Delete one to create another.",
        )
    pack = InvestigationPack(
        # Company-managed = no owner (invariant 9).
        user_id=None if body.is_company_managed else user.id,
        is_company_managed=bool(body.is_company_managed),
        version_number=1,
    )
    await apply_group_share_fields(db, pack, body, partial=False)
    _content(pack, body)
    await apply_user_share_fields(db, pack, body, partial=False)
    db.add(pack)
    await db.flush()
    await _add_version(db, pack, user, body.commit_message or "Initial version")
    await log_audit(
        db,
        user=user,
        action="create",
        entity_type="investigation_pack",
        entity_id=pack.id,
        entity_name=pack.name,
        summary=summary,
        details={
            "subject_types": pack.subject_types,
            "steps": len(pack.definition.get("steps", [])),
            "is_company_managed": pack.is_company_managed,
        },
        request=request,
    )
    await db.commit()
    await db.refresh(pack)
    return _to_response(pack, can_manage=can_manage(user, pack), owner_name=user.full_name if pack.user_id else None)


@router.post("", response_model=InvestigationPackResponse, status_code=status.HTTP_201_CREATED)
async def create_investigation_pack(
    body: InvestigationPackCreate,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _agentic_gate: None = Depends(require_agentic_ai),
):
    """Create a pack (version 1)."""
    return await _create(db, user, body, request, summary=f"Created investigation pack '{body.name}'")


@router.get("/{pack_id}", response_model=InvestigationPackResponse)
async def get_investigation_pack(
    pack_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    group_ids = await get_user_group_ids(db, user.id)
    pack = await _visible_pack(db, user, pack_id, group_ids)
    return _to_response(pack, can_manage=can_manage(user, pack, group_ids), owner_name=await _owner_name(db, pack))


async def _next_version(db: AsyncSession, pack: InvestigationPack) -> int:
    """The pack's next version number, read under its row lock.

    Two saves (or restores) of one pack each read version N when they loaded
    it, both inserted N+1 and one ended in a 500. The number is re-read here,
    under ``FOR UPDATE``, so the second waits for the first and takes N+2.
    Taken at allocation, not when the pack is loaded: by then the share edit
    has locked the accounts it names (``FOR KEY SHARE``), the order a user
    deletion takes too (the account, then the packs it prunes), so the two
    cannot deadlock. Only the column is read: the pack's edits stay as made.
    """
    current = (
        await db.execute(
            select(InvestigationPack.version_number).where(InvestigationPack.id == pack.id).with_for_update()
        )
    ).scalar_one()
    return int(current or 1) + 1


async def _commit_as_written(db: AsyncSession, pack: InvestigationPack) -> None:
    """Read the pack back, then commit: the answer is the version this write made.

    Refreshed after the commit, the pack was read once its row lock was
    released, so a save of the same pack committed in between was answered
    instead (two of three concurrent writes reported one number). Read inside
    the transaction, under the ``FOR UPDATE`` that ``_next_version`` took, it
    is this write's row; the session keeps it past the commit
    (``expire_on_commit=False``).
    """
    await db.flush()
    await db.refresh(pack)
    await db.commit()


@router.put("/{pack_id}", response_model=InvestigationPackResponse)
async def update_investigation_pack(
    pack_id: str,
    body: InvestigationPackUpdate,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _ai_gate: None = Depends(require_ai_enabled),
):
    """Replace the pack's content; a changed method is a new version."""
    group_ids = await get_user_group_ids(db, user.id)
    pack = await get_manageable_entity(
        db, InvestigationPack, user, pack_id, action="edit", entity_label="Investigation pack", user_group_ids=group_ids
    )
    # Pre-ticking a pack for everyone who sees it is its owner's choice: a
    # `config` grantee edits the method, not whether others find it ticked.
    if bool(body.recommended) != bool(pack.recommended) and not (user.is_admin or pack.user_id == user.id):
        raise HTTPException(
            status_code=403, detail="Only the pack's owner or an admin can change whether it is recommended"
        )
    changed = _content(pack, body)
    await apply_group_share_fields(db, pack, body, partial=True)
    await handle_company_managed_toggle(
        db, user, pack, body.is_company_managed, agent_field=None, keep_grants=wants_access_restriction(body, pack)
    )
    await apply_user_share_fields(db, pack, body, partial=True)
    await apply_owner_transfer(
        db,
        actor=user,
        entity=pack,
        new_owner_id=body.user_id,
        entity_type="investigation_pack",
        entity_name=pack.name,
        request=request,
    )
    if changed:
        pack.version_number = await _next_version(db, pack)
        await _add_version(db, pack, user, body.commit_message)
    await log_audit(
        db,
        user=user,
        action="update",
        entity_type="investigation_pack",
        entity_id=pack.id,
        entity_name=pack.name,
        summary=f"Updated investigation pack '{pack.name}'",
        details={"new_version": pack.version_number if changed else None, "is_company_managed": pack.is_company_managed},
        request=request,
    )
    await _commit_as_written(db, pack)
    return _to_response(pack, can_manage=can_manage(user, pack, group_ids), owner_name=await _owner_name(db, pack))


@router.delete("/{pack_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_investigation_pack(
    pack_id: str,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _ai_gate: None = Depends(require_ai_enabled),
):
    """Delete a pack and its versions. Past runs keep the steps they queried."""
    pack = await get_manageable_entity(
        db, InvestigationPack, user, pack_id, action="delete", entity_label="Investigation pack"
    )
    await log_audit(
        db,
        user=user,
        action="delete",
        entity_type="investigation_pack",
        entity_id=pack.id,
        entity_name=pack.name,
        summary=f"Deleted investigation pack '{pack.name}'",
        request=request,
    )
    await db.delete(pack)
    await db.commit()


# ── Versions ────────────────────────────────────────────────────────────────


async def _versions(db: AsyncSession, pack: InvestigationPack) -> list[InvestigationPackVersion]:
    result = await db.execute(
        select(InvestigationPackVersion)
        .where(InvestigationPackVersion.pack_id == pack.id)
        .order_by(InvestigationPackVersion.version_number.desc())
    )
    return list(result.scalars().all())


async def _author_names(db: AsyncSession, versions: list[InvestigationPackVersion]) -> dict[str, str]:
    ids = {v.author_id for v in versions if v.author_id}
    if not ids:
        return {}
    rows = (await db.execute(select(User.id, User.full_name).where(User.id.in_(ids)))).all()
    return {str(uid): name for uid, name in rows}


def _version_item(pack: InvestigationPack, version: InvestigationPackVersion, names: dict[str, str]) -> dict:
    return {
        "version_number": version.version_number,
        "author_id": str(version.author_id) if version.author_id else None,
        "author_name": names.get(str(version.author_id)) if version.author_id else None,
        "commit_message": version.commit_message,
        "created_at": version.created_at.isoformat(),
        "is_current": version.version_number == pack.version_number,
    }


@router.get("/{pack_id}/versions", response_model=list[InvestigationPackVersionItem])
async def list_investigation_pack_versions(
    pack_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    group_ids = await get_user_group_ids(db, user.id)
    pack = await _visible_pack(db, user, pack_id, group_ids)
    versions = await _versions(db, pack)
    names = await _author_names(db, versions)
    return [InvestigationPackVersionItem(**_version_item(pack, v, names)) for v in versions]


async def _version(db: AsyncSession, pack: InvestigationPack, number: int) -> InvestigationPackVersion:
    result = await db.execute(
        select(InvestigationPackVersion).where(
            InvestigationPackVersion.pack_id == pack.id, InvestigationPackVersion.version_number == number
        )
    )
    version = result.scalar_one_or_none()
    if version is None:
        raise HTTPException(status_code=404, detail="Version not found")
    return version


@router.get("/{pack_id}/versions/{version_number}", response_model=InvestigationPackVersionDetail)
async def get_investigation_pack_version(
    pack_id: str,
    version_number: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    group_ids = await get_user_group_ids(db, user.id)
    pack = await _visible_pack(db, user, pack_id, group_ids)
    version = await _version(db, pack, version_number)
    names = await _author_names(db, [version])
    return InvestigationPackVersionDetail(
        **_version_item(pack, version, names),
        subject_types=list(version.subject_types or []),
        definition=dict(version.definition or {}),
    )


@router.post("/{pack_id}/versions/{version_number}/restore", response_model=InvestigationPackResponse)
async def restore_investigation_pack_version(
    pack_id: str,
    version_number: int,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _ai_gate: None = Depends(require_ai_enabled),
):
    """Make an older version current again, as a new version: history is
    never rewritten. Anyone who may edit the pack may restore."""
    group_ids = await get_user_group_ids(db, user.id)
    pack = await get_manageable_entity(
        db, InvestigationPack, user, pack_id, action="edit", entity_label="Investigation pack", user_group_ids=group_ids
    )
    version = await _version(db, pack, version_number)
    try:
        parse_definition(version.definition)
    except PackDefinitionError:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="This version cannot be restored: this platform no longer runs one of its sources.",
        ) from None
    pack.definition = dict(version.definition)
    pack.subject_types = list(version.subject_types or [])
    pack.version_number = await _next_version(db, pack)
    await _add_version(db, pack, user, f"Restored version {version_number}")
    await log_audit(
        db,
        user=user,
        action="update",
        entity_type="investigation_pack",
        entity_id=pack.id,
        entity_name=pack.name,
        summary=f"Restored version {version_number} of investigation pack '{pack.name}'",
        details={"new_version": pack.version_number, "restored": version_number},
        request=request,
    )
    await _commit_as_written(db, pack)
    return _to_response(pack, can_manage=can_manage(user, pack, group_ids), owner_name=await _owner_name(db, pack))


# ── Preview ─────────────────────────────────────────────────────────────────


@router.post("/preview", response_model=PackPreviewResponse)
async def preview_investigation_pack(
    body: PackPreviewRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """What a pack would do on ``subject`` for the caller: the plan, which
    sources apply, what is not available to them. No source is queried."""
    from app.services.deep_investigation_chat import EmptySubjectError, TooManyDomainsError, validate_turn_request
    from app.services.investigation_pack_builder import build_source_pack
    from app.services.investigation_pack_resolver import caller_resources, with_caller_corpora
    from app.services.investigation_packs import UnknownPackOptionError, classify_subject, domain_list, with_seeds

    if (body.pack is None) == (body.pack_id is None):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Give a pack or a pack_id")
    try:
        subject = validate_turn_request(body.subject, None)
    except (EmptySubjectError, TooManyDomainsError) as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from None
    if body.pack is not None:
        fields, pack_id, version = body.pack, "preview", 0
    else:
        group_ids = await get_user_group_ids(db, user.id)
        row = await _visible_pack(db, user, body.pack_id, group_ids)
        try:
            fields = PackFields.model_validate(
                {
                    "name": row.name,
                    "description": row.description or "",
                    "subject_types": list(row.subject_types or []),
                    "recommended": bool(row.recommended),
                    "max_iterations": int(row.max_iterations or 10),
                    "definition": row.definition or {},
                }
            )
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="This pack cannot run on this platform.",
            ) from None
        pack_id, version = row.id, int(row.version_number or 1)
    resources, kb_rows = await caller_resources(db, user)
    pack = build_source_pack(
        pack_id=pack_id,
        name=fields.name,
        description=fields.description,
        subject_types=fields.subject_types,
        recommended=fields.recommended,
        max_iterations=fields.max_iterations,
        version=version,
        definition=fields.definition,
        resources=resources,
    )
    pack = with_caller_corpora(pack, kb_rows)
    subject_type = classify_subject(subject)
    try:
        pack = pack.with_options(body.options or {}, subject_type)
    except UnknownPackOptionError:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Unknown pack option") from None
    seeds = domain_list(subject) if subject_type == "domain_list" else None
    if seeds:
        pack = with_seeds(pack, seeds)
    plan = pack.goal
    path = plan.plan(plan.initial_state(subject_type)) if plan else None
    applicable = pack.sources_for(subject_type)
    by_action: dict[str, list[PreviewSource]] = {}
    for source in applicable:
        by_action.setdefault(source.action or "", []).append(PreviewSource(name=source.name, kind=source.kind))
    fed = {a.slug: a.fed_by for a in (plan.actions if plan else ())}

    def _servable(slug: str, seen: frozenset[str] = frozenset()) -> bool:
        if slug in by_action:
            return True
        feeder = fed.get(slug)
        return bool(feeder) and feeder not in seen and _servable(feeder, seen | {slug})

    actions = [
        PreviewAction(
            slug=a.slug,
            label=a.label,
            description=a.description,
            icon=a.icon,
            produces_report=a.produces_report,
            servable=a.produces_report or _servable(a.slug),
            sources=by_action.get(a.slug, []),
        )
        for a in (path or ())
    ]
    return PackPreviewResponse(
        subject=subject,
        subject_type=subject_type,
        reachable=path is not None and all(a.servable for a in actions),
        actions=actions,
        description=pack.description,
    )


# ── The assistant ───────────────────────────────────────────────────────────


#: Rounds one person may start a minute: each is up to four model calls (two
#: attempts, each with its cut retry) on the caller's budget.
ASSIST_RATE_LIMIT = 10
ASSIST_RATE_WINDOW_SECONDS = 60
_ASSIST_NO_PACK = "The assistant could not build a pack this platform accepts. Rephrase and try again."
_ASSIST_MODEL_FAILED = "The assistant could not reach its model. Try again in a moment."


@router.post("/assist", response_model=PackAssistResponse)
async def assist_investigation_pack(
    body: PackAssistRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _ai_gate: None = Depends(require_ai_enabled),
):
    """One round with the builder's assistant: a reply and a pack `PackFields`
    accepts, built with the caller's model from what the caller may use.
    Nothing is stored; the builder applies the pack to its draft.

    `ASSIST_RATE_LIMIT` rounds a minute per user (429 past it). A draft over
    the request's bound is a 422 before any model call. No provider the
    caller may use is a 403 (the provider rule's own sentence). No acceptable
    pack after the retry, or a model call that raises, is a 502 with a generic
    message; the cause goes to the logs only."""
    from app.llm.provider_access import ProviderNotUsableError
    from app.llm.router import get_llm_for_user
    from app.services.investigation_pack_assistant import PackAssistantError, assist
    from app.services.investigation_pack_resolver import caller_resources

    await enforce_rate_limit(
        None,
        "investigation_pack_assist",
        limit=ASSIST_RATE_LIMIT,
        window_seconds=ASSIST_RATE_WINDOW_SECONDS,
        bucket=str(user.id),
    )
    resources, kb_rows = await caller_resources(db, user)
    try:
        llm, model = await get_llm_for_user(db, str(user.id))
        reply, pack = await assist(
            llm,
            model,
            instruction=body.instruction,
            current=body.current,
            history=[turn.model_dump() for turn in body.history],
            resources=resources,
            kb_rows=kb_rows,
        )
    except ProviderNotUsableError as refused:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(refused)) from None
    except PackAssistantError as exc:
        logger.info("pack assistant gave up: %r", str(exc)[:300])
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=_ASSIST_NO_PACK) from None
    except Exception as exc:
        # No model to call, or a provider timeout, rate limit or auth
        # failure: the caller is told only that the model did not answer.
        logger.warning("pack assistant model call failed: %r", format_exception_message(exc)[:300])
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=_ASSIST_MODEL_FAILED) from None
    return PackAssistResponse(reply=reply, pack=pack)
