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

"""Template container API — CRUD + independent asset management.

A ``TemplateContainer`` holds the structured metadata of a reusable design
template; its binaries (fonts, images) live in S3/MinIO as ``FileAttachment``
rows tagged ``source = "template_asset"`` and pointed at the container by FK.

This surface is deliberately **separate from the Knowledge Base API**: nothing
here chunks, embeds or vectorizes anything, and creating or updating a container
never enqueues document processing. Retrieval is structured filtering
(``app.services.template_containers``), shared with the agent-facing
``search_templates`` built-in tool.

Assets are managed independently of the container's metadata: add, relabel,
replace or remove one asset without touching (or re-sending) any other field.
Any authenticated user can create containers; company-managed ones
(``is_company_managed=True``) are visible to everyone but admin-editable only.
"""

from __future__ import annotations

import contextlib
import io
import logging
from typing import Annotated

from fastapi import (
    APIRouter,
    Depends,
    File,
    Header,
    HTTPException,
    Query,
    Request,
    UploadFile,
    status,
)
from fastapi.responses import JSONResponse, Response, StreamingResponse
from sqlalchemy import select
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
    user_visible_query,
    wants_access_restriction,
)
from app.api.v1.schemas.template_container import (
    TemplateAssetResponse,
    TemplateContainerCreate,
    TemplateContainerDetailResponse,
    TemplateContainerResponse,
    TemplateContainerUpdate,
    UpdateAssetRoleRequest,
)
from app.core.database import get_db, release_db_connection_before_loop
from app.core.utils import build_content_disposition, fit_copy_name, if_none_match_hits
from app.models.file_attachment import FileAttachment
from app.models.template_container import (
    ASSET_ROLES,
    MAX_ASSET_DESCRIPTION_CHARS,
    MAX_ASSETS_PER_CONTAINER,
    TEMPLATE_ASSET_SOURCE,
    TemplateContainer,
)
from app.models.user import User
from app.services.audit import log_audit
from app.services.file_responses import file_not_modified, file_response_headers
from app.services.template_containers import (
    MAX_SEARCH_RESULTS,
    PRESENTATION_KIND,
    VIDEO_KIND,
    apply_structured_filters,
    coerce_metadata_to_kind,
    container_supported_schemes,
    count_assets_by_container,
    derived_description,
    filter_by_theme,
    is_builtin_container,
    kind_problem,
    load_assets,
    normalize_asset_description,
    normalize_kind,
    normalize_tags,
    resolve_kind_update,
    serialize_asset,
    template_asset_sha256,
    unsafe_theme_value_problems,
    validate_chrome,
    validate_layouts,
    validate_slide_cap,
    validate_supported_schemes,
    validate_template_heroes,
    validate_theme_config,
    validate_theme_variants,
    validate_video_style,
)
from app.services.template_package import (
    EXPORTED_FIELDS,
    MAX_PACKAGE_BYTES,
    PackagedAsset,
    TemplatePackage,
    TemplatePackageError,
    build_manifest,
    build_package_zip,
    parse_package,
    remap_asset_ids,
)
from app.services.template_thumbnails import (
    DEFAULT_THUMBNAIL_SIZE,
    SVG_CONTENT_TYPE,
    THUMBNAIL_CONTENT_TYPE,
    THUMBNAIL_SIZES,
    ThumbnailRefusedError,
    render_thumbnail_async,
    thumbnail_cache_headers,
    thumbnail_etag,
    thumbnail_headers,
    thumbnail_kind,
    thumbnail_render_gate,
)

logger = logging.getLogger(__name__)
router = APIRouter()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _to_response(
    container: TemplateContainer,
    *,
    can_manage: bool,
    owner_name: str | None = None,
    asset_count: int = 0,
) -> TemplateContainerResponse:
    return TemplateContainerResponse(
        id=str(container.id),
        user_id=str(container.user_id) if container.user_id else None,
        owner_name=owner_name,
        name=container.name,
        description=container.description,
        kind=container.kind,
        tags=container.tags or [],
        theme=container.theme,
        theme_config=container.theme_config or {},
        # Derived on every read, never stored: the deck editor
        # restricts its theme picker to this list once the template is linked.
        supported_schemes=container_supported_schemes(container),
        aspect_ratio=container.aspect_ratio,
        layouts=container.layouts or [],
        extra_metadata=container.extra_metadata or {},
        lookup_count=container.lookup_count,
        asset_count=asset_count,
        is_company_managed=container.is_company_managed,
        is_builtin=is_builtin_container(container),
        shared_group_ids=container.shared_group_ids,
        shared_group_permissions=container.shared_group_permissions,
        shared_user_ids=container.shared_user_ids,
        shared_user_permissions=container.shared_user_permissions,
        access_restricted=container.access_restricted,
        can_manage=can_manage,
        created_at=container.created_at.isoformat(),
        updated_at=container.updated_at.isoformat(),
    )


async def _to_detail_response(
    db: AsyncSession,
    container: TemplateContainer,
    *,
    can_manage: bool,
    owner_name: str | None = None,
) -> TemplateContainerDetailResponse:
    assets = await load_assets(db, container.id)
    base = _to_response(
        container,
        can_manage=can_manage,
        owner_name=owner_name,
        asset_count=len(assets),
    )
    return TemplateContainerDetailResponse(
        **base.model_dump(),
        assets=[TemplateAssetResponse(**serialize_asset(a)) for a in assets],
    )


async def _get_visible_container(
    db: AsyncSession,
    user: User,
    container_id: str,
    *,
    user_group_ids: list[str] | None = None,
) -> TemplateContainer:
    """Load a container the user may READ (own + group-shared + company-managed)."""
    if user_group_ids is None:
        user_group_ids = await get_user_group_ids(db, user.id)
    result = await db.execute(
        user_visible_query(
            TemplateContainer, user.id, user_group_ids, is_admin=user.is_admin
        ).where(
            TemplateContainer.id
            == parse_path_id(container_id, not_found_detail="Template container not found")
        )
    )
    container = result.scalar_one_or_none()
    if not container:
        raise HTTPException(status_code=404, detail="Template container not found")
    return container


BUILTIN_READ_ONLY_DETAIL = (
    "Built-in template containers are read-only: they are re-synced from the "
    "platform on every startup, so an edit here would be reverted by the next "
    "deploy. Duplicate it (POST /template-containers/{id}/duplicate) to get an "
    "editable copy."
)


async def _get_writable_container(
    db: AsyncSession,
    user: User,
    container_id: str,
    *,
    action: str,
    user_group_ids: list[str] | None = None,
) -> TemplateContainer:
    """Load a container the user may WRITE (creator, group member, or admin).

    A **built-in** container is refused for everyone, admins included,
    with a 403 that points at the duplicate route. The refusal is not a
    permission question — the seed full-syncs every field and deletes rows it no
    longer declares on every boot, so a write here is not "not allowed", it is
    silently undone; saying so up front is the only honest answer. Deletion is
    refused for the same reason: the container is recreated on the next boot,
    which is the "always there" contract the brand deck is seeded under.
    """
    container = await get_manageable_entity(
        db,
        TemplateContainer,
        user,
        container_id,
        action=action,
        entity_label="Template container",
        user_group_ids=user_group_ids,
    )
    if is_builtin_container(container):
        raise HTTPException(status_code=403, detail=BUILTIN_READ_ONLY_DETAIL)
    return container


async def _get_asset(
    db: AsyncSession, container: TemplateContainer, asset_id: str
) -> FileAttachment:
    """Load one asset, scoped to *container* so ids can't be probed across containers."""
    result = await db.execute(
        select(FileAttachment).where(
            FileAttachment.id == parse_path_id(asset_id, not_found_detail="Template asset not found"),
            FileAttachment.template_container_id == container.id,
            FileAttachment.source == TEMPLATE_ASSET_SOURCE,
        )
    )
    asset = result.scalar_one_or_none()
    if not asset:
        raise HTTPException(status_code=404, detail="Template asset not found")
    return asset


def _normalize_role(role: str | None) -> str | None:
    """Normalize an asset role label.

    Unknown values are accepted (the container is a generic building block and a
    new consumer feature must not need a code change to label its own asset
    kinds) but normalized to lower-case so filtering stays predictable.
    ``ASSET_ROLES`` documents the recognised set rather than restricting it.
    """
    if role is None:
        return None
    cleaned = role.strip().lower()
    if not cleaned:
        return None
    if cleaned not in ASSET_ROLES:
        logger.debug("Template asset uses a non-standard role %r", cleaned)
    return cleaned[:50]


async def _read_upload(file: UploadFile) -> tuple[bytes, str, str]:
    """Read and size-check an upload, returning ``(bytes, filename, content_type)``."""
    from app.services.file_storage import MAX_UPLOAD_SIZE_BYTES, guess_content_type

    raw_bytes = await file.read()
    if not raw_bytes:
        raise HTTPException(status_code=400, detail="Uploaded file is empty")
    if len(raw_bytes) > MAX_UPLOAD_SIZE_BYTES:
        raise HTTPException(
            status_code=413,
            detail=(
                f"File too large ({len(raw_bytes):,} bytes). Maximum is "
                f"{MAX_UPLOAD_SIZE_BYTES // (1024 * 1024)} MB."
            ),
        )
    filename = file.filename or "template_asset"
    return raw_bytes, filename, file.content_type or guess_content_type(filename)


async def _store_asset(
    container: TemplateContainer, raw_bytes: bytes, filename: str, content_type: str
) -> tuple[bytes, str]:
    """Sanitize + upload through the one template-asset write path.

    Returns ``(stored bytes, object key)``; an SVG that cannot be parsed is a
    400 naming the file, not a stored-but-unrenderable asset.
    """
    from app.services.svg_sanitizer import SvgSanitizeError
    from app.services.template_containers import store_template_asset_bytes

    try:
        return await store_template_asset_bytes(
            container.id, raw_bytes, filename=filename, content_type=content_type
        )
    except SvgSanitizeError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"'{filename}' is not a valid SVG document and cannot be stored: {exc}",
        ) from exc


async def _asset_geometry(stored: bytes, content_type: str) -> dict | None:
    """The trim box and ink of an image asset, read from the bytes just stored.

    Judged by the bytes, not the declared type (a PNG uploaded as
    `application/octet-stream` is still a logo); ``None`` for a non-image.
    """
    from app.services.image_geometry import image_geometry

    return await image_geometry(stored, content_type)


# ---------------------------------------------------------------------------
# Portability
# ---------------------------------------------------------------------------


@router.get("/{container_id}/export")
async def export_template_container(
    container_id: str,
    format: str = Query(
        default="zip",
        description="`zip` for the complete package (metadata + assets), `json` "
        "for metadata only.",
    ),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Export a container as a package ZIP (default) or a metadata-only bundle.

    A **built-in** container is exportable on purpose: forking the seeded brand
    template is the documented way to get an editable copy of it, and the startup
    seed would revert any in-place edit anyway.

    ``json`` carries no binaries. It is offered because a palette-only template is
    a real thing to hand around, but a container WITH assets exported this way
    loses them, and the only signal is that the manifest's ``assets`` list is
    empty. Prefer ``zip`` unless the caller specifically wants metadata.
    """
    if format not in ("zip", "json"):
        raise HTTPException(
            status_code=422, detail="Unsupported export format. Use `zip` or `json`."
        )
    container = await _get_visible_container(db, user, container_id)
    assets = await load_assets(db, container.id)
    exported = {field: getattr(container, field, None) for field in EXPORTED_FIELDS}
    safe_name = (container.name or "template").strip().lower().replace(" ", "-")[:60]

    if format == "json":
        return JSONResponse(
            content=build_manifest(exported, []),
            headers={
                "Content-Disposition": build_content_disposition(
                    f"template-{safe_name}.json"
                )
            },
        )

    from app.services.file_storage import FileNotFoundInStorageError, file_storage

    packaged: list[PackagedAsset] = []
    for asset in assets:
        try:
            data = await file_storage.download(asset.s3_object_name)
        except FileNotFoundInStorageError:
            # A row whose binary vanished from storage must not sink the whole
            # export — the rest of the template is still worth having, and the
            # missing asset is visible as a gap in the manifest.
            logger.warning(
                "Template asset %s has no binary in storage; excluded from export",
                asset.id,
            )
            continue
        packaged.append(
            PackagedAsset(
                filename=asset.filename,
                data=data,
                asset_role=asset.asset_role,
                description=asset.description,
                content_type=asset.content_type,
                original_id=str(asset.id),
            )
        )

    return StreamingResponse(
        io.BytesIO(build_package_zip(exported, packaged)),
        media_type="application/zip",
        headers={
            "Content-Disposition": build_content_disposition(
                f"template-{safe_name}.zip"
            )
        },
    )


async def _persist_imported_package(
    db: AsyncSession,
    package: TemplatePackage,
    *,
    user: User,
    request: Request,
    audit_action: str = "import",
    audit_summary: str | None = None,
    audit_details: dict | None = None,
) -> TemplateContainer:
    """Create a container (and its assets) from a parsed package.

    Shared by both import entry points and by in-app duplication, which
    builds the package in memory from the source container instead of parsing an
    archive — the audit ``action`` / ``summary`` are overridable so the trail says
    "duplicate", not "import". Four rules are enforced here rather than left to
    the caller, because an import that skipped any of them would be the back door
    around a guarantee the rest of the surface upholds:

    1. **The existing validators decide what is storable.** An imported container
       is held to exactly the contract an authored one is, so a palette key the
       renderer ignores or a layout id that does not exist cannot land in the
       database and fail silently at render time - and a kind a template cannot
       be created with is refused by name, never coerced.
    2. **Never company-managed.** Publishing a template org-wide is a human action
       in the Templates UI, whatever the importer's role. An import is not
       a publishing channel.
    3. **Asset ids are remapped, not carried.** New rows mean new ids, so stored
       references in ``layouts`` / ``extra_metadata`` are rewritten.
    4. **The asset cap applies on the way in**, so an archive cannot create a
       container the asset API would refuse to add to.
    """
    data = package.container
    # The closed choice creating a template is held to: a blank kind is
    # a presentation template, anything else must be one of TEMPLATE_KINDS. An
    # older export of a retired kind (``asset-library``, ``dashboard``) is
    # REFUSED with the sentence every other surface uses, naming the kind, so
    # its owner edits one field of the export instead of receiving a
    # presentation template they never asked for. An import is not a way to
    # re-create a retired kind - and not a way to re-label one quietly either.
    kind = normalize_kind(data.get("kind"))
    if kind is None:
        raise HTTPException(
            status_code=422,
            detail=(
                "The package was NOT imported. Fix the export and try again: "
                + kind_problem(data.get("kind"))
            ),
        )

    # ``kind`` matters here: a report template honours a wider palette than a
    # deck, so validating without it would silently strip an imported
    # report template's risk colours and surfaces.
    accepted_config, config_problems = validate_theme_config(
        data.get("theme_config"), kind=kind
    )
    clean_layouts, layout_problems = validate_layouts(data.get("layouts"), kind=kind)
    extra_metadata, variant_problems = validate_theme_variants(
        data.get("extra_metadata") or {}, kind=kind
    )
    extra_metadata, scheme_problems = validate_supported_schemes(extra_metadata, kind=kind)
    extra_metadata, chrome_problems = validate_chrome(extra_metadata, kind=kind)
    extra_metadata, cap_problems = validate_slide_cap(extra_metadata, kind=kind)
    extra_metadata, hero_problems = validate_template_heroes(extra_metadata, kind=kind)
    extra_metadata, style_problems = validate_video_style(extra_metadata, kind=kind)
    problems = (
        config_problems
        + layout_problems
        + variant_problems
        + scheme_problems
        + chrome_problems
        + cap_problems
        + hero_problems
        + style_problems
    )
    if problems:
        raise HTTPException(
            status_code=422,
            detail=(
                "The package was NOT imported. Fix the export and try again: "
                + " ".join(problems)
            ),
        )

    if len(package.assets) > MAX_ASSETS_PER_CONTAINER:
        raise HTTPException(
            status_code=413,
            detail=(
                f"Package holds {len(package.assets)} assets; the maximum per "
                f"container is {MAX_ASSETS_PER_CONTAINER}."
            ),
        )

    from app.services.file_storage import MAX_UPLOAD_SIZE_BYTES, guess_content_type

    for asset in package.assets:
        if len(asset.data) > MAX_UPLOAD_SIZE_BYTES:
            raise HTTPException(
                status_code=413,
                detail=(
                    f"Asset '{asset.filename}' is too large "
                    f"({len(asset.data):,} bytes). Maximum is "
                    f"{MAX_UPLOAD_SIZE_BYTES // (1024 * 1024)} MB."
                ),
            )

    # ``parse_bundle`` already refuses a field of the wrong JSON type, so these
    # are belt-and-braces: this helper is also reachable from the seed script, and
    # a coercion here means neither caller can crash the insert on a stray type.
    container = TemplateContainer(
        user_id=user.id,
        name=str(data.get("name") or "").strip()[:255],
        description=str(data.get("description") or "").strip() or None,
        kind=kind,
        tags=normalize_tags(data.get("tags")),
        theme=str(data.get("theme") or "").strip() or None,
        theme_config=accepted_config,
        aspect_ratio=str(data.get("aspect_ratio") or "").strip() or None,
        layouts=clean_layouts,
        extra_metadata=extra_metadata or {},
        is_company_managed=False,
    )
    db.add(container)
    await db.flush()

    id_map: dict[str, str] = {}
    for asset in package.assets:
        content_type = asset.content_type or guess_content_type(asset.filename)
        stored, s3_object_name = await _store_asset(
            container, asset.data, asset.filename, content_type
        )
        row = FileAttachment(
            user_id=user.id,
            template_container_id=container.id,
            asset_role=_normalize_role(asset.asset_role),
            description=normalize_asset_description(asset.description),
            filename=asset.filename,
            content_type=content_type,
            size=len(stored),
            s3_object_name=s3_object_name,
            sha256=await template_asset_sha256(stored),
            source=TEMPLATE_ASSET_SOURCE,
            image_meta=await _asset_geometry(stored, content_type),
        )
        db.add(row)
        await db.flush()
        if asset.original_id:
            id_map[asset.original_id] = str(row.id)

    if id_map:
        container.layouts = remap_asset_ids(container.layouts, id_map)
        container.extra_metadata = remap_asset_ids(container.extra_metadata, id_map)
        # The palette names files too (``THEME_FILE_ID_KEYS``: the logo, its
        # light variant and, the mark the gradient cover
        # watermarks): an explicit id left as exported would point at a row of
        # the OTHER instance and the chrome or the hero would silently draw
        # nothing, while the role fallback only fills a key that is absent.
        # Colour values never collide with the mapping - its keys are UUIDs.
        container.theme_config = remap_asset_ids(container.theme_config, id_map)

    await log_audit(
        db,
        user=user,
        action=audit_action,
        entity_type="template_container",
        entity_id=container.id,
        entity_name=container.name,
        summary=audit_summary or f"Imported template container '{container.name}'",
        details={
            "kind": container.kind,
            "tags": container.tags or [],
            "asset_count": len(package.assets),
            "remapped_asset_references": len(id_map),
            **(audit_details or {}),
        },
        request=request,
    )
    await db.commit()
    await db.refresh(container)
    return container


async def _package_from_container(
    db: AsyncSession, container: TemplateContainer
) -> tuple[TemplatePackage, int]:
    """Build an in-memory package from a live container — the duplicate path.

    Returns ``(package, skipped_assets)``. An asset whose binary is gone from
    storage is skipped rather than failing the copy (same policy as export): the
    copy is still worth having, and the gap is reported in the audit details.
    """
    from app.services.file_storage import FileNotFoundInStorageError, file_storage

    exported = {field: getattr(container, field, None) for field in EXPORTED_FIELDS}
    packaged: list[PackagedAsset] = []
    skipped = 0
    for asset in await load_assets(db, container.id):
        try:
            data = await file_storage.download(asset.s3_object_name)
        except FileNotFoundInStorageError:
            logger.warning(
                "Template asset %s has no binary in storage; excluded from duplicate",
                asset.id,
            )
            skipped += 1
            continue
        packaged.append(
            PackagedAsset(
                filename=asset.filename,
                data=data,
                asset_role=asset.asset_role,
                description=asset.description,
                content_type=asset.content_type,
                original_id=str(asset.id),
            )
        )
    return TemplatePackage(container=exported, assets=packaged), skipped


@router.post(
    "/import",
    response_model=TemplateContainerDetailResponse,
    status_code=status.HTTP_201_CREATED,
)
async def import_template_container(
    request: Request,
    file: UploadFile = File(
        ..., description="A template package ZIP, or a metadata-only JSON export."
    ),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _agentic_gate: None = Depends(require_agentic_ai),
):
    """Import a container from an export package.

    One endpoint for both export formats: the parser sniffs a ZIP and falls back
    to a JSON bundle, so a caller never has to know which flavour they were given.

    Gated by ``require_agentic_ai`` for the same reason ``POST ""`` is: this
    creates a container, and an ungated import would be the way around the gate on
    creating one.
    """
    # Bounded read, the same idiom as ``import_agent_file``: reading the whole
    # upload and checking its length afterwards means an oversized file is already
    # in memory. One byte past the cap is enough to decide.
    raw = await file.read(MAX_PACKAGE_BYTES + 1)
    if len(raw) > MAX_PACKAGE_BYTES:
        raise HTTPException(
            status_code=413,
            detail=(
                f"The uploaded package exceeds the maximum size of "
                f"{MAX_PACKAGE_BYTES // (1024 * 1024)} MB."
            ),
        )
    try:
        package = parse_package(raw)
    except TemplatePackageError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    container = await _persist_imported_package(
        db, package, user=user, request=request
    )
    return await _to_detail_response(
        db,
        container,
        can_manage=can_manage(user, container),
        owner_name=user.full_name,
    )


@router.post(
    "/{container_id}/duplicate",
    response_model=TemplateContainerDetailResponse,
    status_code=status.HTTP_201_CREATED,
)
async def duplicate_template_container(
    container_id: str,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _agentic_gate: None = Depends(require_agentic_ai),
):
    """Copy a visible container — metadata AND assets — into a private one.

    The editable-copy path for the read-only built-in brand decks, and a
    convenience for any template the caller can see: read access is enough,
    because the copy is exactly what an export followed by an import would
    produce, and export needs only read access too. The copy goes through the
    same ``_persist_imported_package`` pipeline as an import, so it is held to the
    same validators, is never company-managed (publishing stays a human action
    in the UI) and has its asset ids remapped so stored references keep resolving.
    ``(copy)`` is appended to the name so the two are told apart in the list.
    """
    source = await _get_visible_container(db, user, container_id)
    package, skipped = await _package_from_container(db, source)
    base_name = (source.name or "Template").strip()
    package.container["name"] = fit_copy_name(
        base_name, TemplateContainer.__table__.c.name, suffix=" (copy)"
    )
    # A copy is a NEW row, and a row of a retired kind cannot be created,
    # so a legacy library is copied as a presentation template - its
    # assets are what the copy carries, and that is exactly what a presentation
    # template offers a deck. Unlike an import there is no export to edit, and
    # a read-only source cannot be moved off its kind first, so the mapping is
    # applied here and recorded in the audit trail rather than refused. The
    # row's metadata was stored for a renderer that no longer exists, so it is
    # reduced to what a presentation template honours on the same occasion:
    # held to that contract by the strict pipeline below, a layout id or a
    # palette key of the old kind would refuse the copy over something the
    # user cannot fix on the source. What is left out is recorded next to the
    # mapping. A source of a supported kind is not touched: a problem in a row
    # of the copy's own kind is a real problem in the source, and the pipeline
    # names it.
    source_kind = normalize_kind(source.kind)
    dropped: list[str] = []
    if source_kind is None:
        package.container, dropped = coerce_metadata_to_kind(
            package.container, kind=PRESENTATION_KIND
        )
    # A built-in describes its own read-only status in its description; the
    # copy is editable, so it says where it comes from instead.
    package.container["description"] = derived_description(
        base_name,
        package.container.get("description"),
        from_builtin=is_builtin_container(source),
    )
    container = await _persist_imported_package(
        db,
        package,
        user=user,
        request=request,
        audit_action="duplicate",
        audit_summary=(
            f"Duplicated template container '{source.name}' as '{package.container['name']}'"
        ),
        audit_details={
            "source_id": str(source.id),
            "source_is_builtin": is_builtin_container(source),
            "assets_skipped": skipped,
            **(
                {"kind_mapped_from": source.kind, "metadata_dropped": dropped}
                if source_kind is None
                else {}
            ),
        },
    )
    return await _to_detail_response(
        db,
        container,
        can_manage=can_manage(user, container),
        owner_name=user.full_name,
    )


# ---------------------------------------------------------------------------
# Container CRUD
# ---------------------------------------------------------------------------


@router.get("", response_model=list[TemplateContainerResponse])
async def list_template_containers(
    name: str | None = Query(None, description="Case-insensitive substring match"),
    kind: str | None = Query(None, description="Exact match, e.g. 'presentation'"),
    tag: list[str] | None = Query(None, description="Repeatable; OR unless match_all_tags"),
    theme: str | None = Query(
        None,
        description=(
            "'dark' or 'light': only the presentation templates able to dress a deck "
            "in that colour scheme - the legacy label is not consulted for a "
            "scheme. Any other value is matched exactly against the legacy free-text "
            "label."
        ),
    ),
    aspect_ratio: str | None = Query(None, description="Exact match, e.g. '16:9'"),
    match_all_tags: bool = Query(False, description="Require every tag instead of any"),
    scope: EntityListScope = Query("visible", description=ENTITY_LIST_SCOPE_DESCRIPTION),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List/filter template containers visible to the current user.

    Filtering is purely structured — the same ``apply_structured_filters`` +
    ``filter_by_theme`` pair the agent-facing ``search_templates`` tool uses, so
    the API and the tool can never disagree about what a filter means. No
    vector search is involved.

    Visibility is the three-tier predicate for everyone, admins included:
    the Templates library lists own + shared + company-managed like
    every other library page, and an admin finds other users' templates in
    Settings > Objects. The detail route (``_get_visible_container``) keeps the
    admin bypass, and so does ``scope=all`` - the template a colleague's deck
    is linked to, as the deck's Design panel shows it to an admin.
    """
    user_group_ids = await get_user_group_ids(db, user.id)
    query = apply_structured_filters(
        user_visible_query(
            TemplateContainer,
            user.id,
            user_group_ids,
            is_admin=list_scope_admin_bypass(scope, user),
        ),
        name=name,
        kind=kind,
        tags=tag,
        theme=theme,
        aspect_ratio=aspect_ratio,
        match_all_tags=match_all_tags,
    )
    result = await db.execute(query)
    # A colour scheme is derived from the palette, so it is applied to the rows.
    containers = filter_by_theme(list(result.scalars().all()), theme)
    owner_names = await resolve_owner_names(db, containers)
    counts = await count_assets_by_container(db, [c.id for c in containers])
    return [
        _to_response(
            c,
            can_manage=can_manage(user, c, user_group_ids),
            owner_name=owner_names.get(str(c.user_id)),
            asset_count=counts.get(str(c.id), 0),
        )
        for c in containers
    ]


@router.post(
    "",
    response_model=TemplateContainerDetailResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_template_container(
    body: TemplateContainerCreate,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _agentic_gate: None = Depends(require_agentic_ai),
):
    """Create a template container.

    Note what does NOT happen here, by design: no chunking, no
    embedding, no ``enqueue_document_processing``. The container's content is
    metadata, never RAG corpus.
    """
    require_admin_for_company_managed(
        body, user, "Only admins can create company-managed template containers"
    )

    # A closed choice: "Presentation" or "Report". The form offers
    # exactly these two, so a refusal here only ever meets a hand-written call.
    kind = normalize_kind(body.kind)
    if kind is None:
        raise HTTPException(status_code=422, detail=kind_problem(body.kind))
    # The Dark / Light switches of the form land here. A declaration naming no
    # scheme (or an unknown one) is refused rather than stored: the reader
    # would take it for "declares nothing" and fall back to the palette rule,
    # silently widening a Dark-only template back to both.
    extra_metadata, scheme_problems = validate_supported_schemes(
        body.extra_metadata or {}, kind=kind
    )
    if scheme_problems:
        raise HTTPException(status_code=422, detail=" ".join(scheme_problems))
    # The header / footer pattern: a typo in a placeholder is
    # reported here, never drawn literally on every slide.
    extra_metadata, chrome_problems = validate_chrome(extra_metadata, kind=kind)
    if chrome_problems:
        raise HTTPException(status_code=422, detail=" ".join(chrome_problems))
    # How many slides a deck of this template may hold: a value the
    # reader would clamp or ignore is refused here, never stored.
    extra_metadata, cap_problems = validate_slide_cap(extra_metadata, kind=kind)
    if cap_problems:
        raise HTTPException(status_code=422, detail=" ".join(cap_problems))
    # The covers of the template's own.
    extra_metadata, hero_problems = validate_template_heroes(extra_metadata, kind=kind)
    if hero_problems:
        raise HTTPException(status_code=422, detail=" ".join(hero_problems))
    # What a video template presets, adds and locks.
    extra_metadata, style_problems = validate_video_style(extra_metadata, kind=kind)
    if style_problems:
        raise HTTPException(status_code=422, detail=" ".join(style_problems))
    # A palette value every viewer's browser would load a URL for.
    theme_problems = unsafe_theme_value_problems(body.theme_config, extra_metadata, kind=kind)
    if theme_problems:
        raise HTTPException(status_code=422, detail=" ".join(theme_problems))

    container = TemplateContainer(
        user_id=user.id,
        name=body.name,
        description=body.description,
        kind=kind,
        tags=normalize_tags(body.tags),
        theme=body.theme,
        theme_config=body.theme_config or {},
        aspect_ratio=body.aspect_ratio,
        layouts=validate_layouts(body.layouts, kind=kind)[0] if kind == VIDEO_KIND else (body.layouts or []),
        extra_metadata=extra_metadata or {},
        is_company_managed=body.is_company_managed,
    )
    await apply_group_share_fields(db, container, body, partial=False)
    await apply_user_share_fields(db, container, body, partial=False)
    db.add(container)
    await db.flush()
    await log_audit(
        db,
        user=user,
        action="create",
        entity_type="template_container",
        entity_id=container.id,
        entity_name=container.name,
        summary=f"Created template container '{container.name}'",
        details={
            "kind": container.kind,
            "theme": container.theme,
            "aspect_ratio": container.aspect_ratio,
            "tags": container.tags or [],
            "layout_count": len(container.layouts or []),
            "is_company_managed": container.is_company_managed,
        },
        request=request,
    )
    await db.commit()
    await db.refresh(container)
    return await _to_detail_response(
        db,
        container,
        can_manage=can_manage(user, container),
        owner_name=user.full_name,
    )


@router.get("/{container_id}", response_model=TemplateContainerDetailResponse)
async def get_template_container(
    container_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get one container with its full asset list."""
    user_group_ids = await get_user_group_ids(db, user.id)
    container = await _get_visible_container(
        db, user, container_id, user_group_ids=user_group_ids
    )
    owner_names = await resolve_owner_names(db, [container])
    return await _to_detail_response(
        db,
        container,
        can_manage=can_manage(user, container, user_group_ids),
        owner_name=owner_names.get(str(container.user_id)),
    )


@router.put("/{container_id}", response_model=TemplateContainerDetailResponse)
async def update_template_container(
    container_id: str,
    body: TemplateContainerUpdate,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _ai_gate: None = Depends(require_ai_enabled),
):
    """Update a container's metadata (creator, group member, or admin).

    Assets are untouched by this endpoint — they have their own routes, so
    editing a theme never risks the asset list and vice versa.
    """
    user_group_ids = await get_user_group_ids(db, user.id)
    container = await _get_writable_container(
        db, user, container_id, action="edit", user_group_ids=user_group_ids
    )

    if body.name is not None:
        container.name = body.name
    if body.description is not None:
        container.description = body.description
    kind_changed = False
    if body.kind is not None:
        # Same closed choice as creation - for a CHANGE. A row of a
        # retired kind keeps its kind until it is set to one of the two, and the
        # form echoes the stored kind on every save, so an echo is a no-op
        # whatever the value (``resolve_kind_update``): the row is never refused
        # for merely still being what it was, or it could not save its name.
        kind = resolve_kind_update(container.kind, body.kind)
        if kind is None:
            raise HTTPException(status_code=422, detail=kind_problem(body.kind))
        kind_changed = kind != container.kind
        container.kind = kind
    # A palette value every viewer's browser would load a URL for,
    # held to the kind the row ends up with - before anything is stored.
    theme_problems = unsafe_theme_value_problems(
        body.theme_config, body.extra_metadata, kind=container.kind
    )
    if theme_problems:
        raise HTTPException(status_code=422, detail=" ".join(theme_problems))
    if body.tags is not None:
        # Normalised on update too, not just create: an un-normalised tag written
        # here would be invisible to the structured search that reads it.
        container.tags = normalize_tags(body.tags)
    if body.theme is not None:
        # Legacy free-text label: a blank clears it - never store "" as
        # a label the exact-match search would then have to spell.
        container.theme = body.theme.strip() or None
    if body.theme_config is not None:
        container.theme_config = body.theme_config
    if body.aspect_ratio is not None:
        container.aspect_ratio = body.aspect_ratio
    if body.layouts is not None or kind_changed:
        layouts = body.layouts if body.layouts is not None else (container.layouts or [])
        container.layouts = validate_layouts(layouts, kind=container.kind)[0] if container.kind == VIDEO_KIND else layouts
    if body.extra_metadata is not None or kind_changed:
        # Validated against the kind the row ENDS UP with: a presentation
        # template's Dark / Light declaration must name a real scheme, and the
        # key is dropped from a template that stops dressing decks. An echoed
        # kind changes nothing, so it re-validates nothing.
        extra_metadata, scheme_problems = validate_supported_schemes(
            body.extra_metadata
            if body.extra_metadata is not None
            else (container.extra_metadata or {}),
            kind=container.kind,
        )
        if scheme_problems:
            raise HTTPException(status_code=422, detail=" ".join(scheme_problems))
        extra_metadata, chrome_problems = validate_chrome(extra_metadata, kind=container.kind)
        if chrome_problems:
            raise HTTPException(status_code=422, detail=" ".join(chrome_problems))
        extra_metadata, cap_problems = validate_slide_cap(extra_metadata, kind=container.kind)
        if cap_problems:
            raise HTTPException(status_code=422, detail=" ".join(cap_problems))
        extra_metadata, hero_problems = validate_template_heroes(extra_metadata, kind=container.kind)
        if hero_problems:
            raise HTTPException(status_code=422, detail=" ".join(hero_problems))
        extra_metadata, style_problems = validate_video_style(extra_metadata, kind=container.kind)
        if style_problems:
            raise HTTPException(status_code=422, detail=" ".join(style_problems))
        container.extra_metadata = extra_metadata
    await apply_group_share_fields(db, container, body, partial=True)

    # ``agent_field=None``: agents never bind containers by id (they discover
    # them through ``search_templates``), so there is no stale binding to purge.
    await handle_company_managed_toggle(
        db,
        user,
        container,
        body.is_company_managed,
        agent_field=None,
        keep_grants=wants_access_restriction(body, container),
    )

    # Per-user sharing — after the company-managed toggle, since
    # ``access_restricted`` is only meaningful relative to it.
    await apply_user_share_fields(db, container, body, partial=True)

    await apply_owner_transfer(
        db,
        actor=user,
        entity=container,
        new_owner_id=body.user_id,
        entity_type="template_container",
        entity_name=container.name,
        request=request,
    )

    await log_audit(
        db,
        user=user,
        action="update",
        entity_type="template_container",
        entity_id=container.id,
        entity_name=container.name,
        summary=f"Updated template container '{container.name}'",
        details={
            "kind": container.kind,
            "theme": container.theme,
            "aspect_ratio": container.aspect_ratio,
            "tags": container.tags or [],
            "layout_count": len(container.layouts or []),
            "is_company_managed": container.is_company_managed,
        },
        request=request,
    )
    await db.commit()
    await db.refresh(container)
    owner_names = await resolve_owner_names(db, [container])
    return await _to_detail_response(
        db,
        container,
        can_manage=can_manage(user, container, user_group_ids),
        owner_name=owner_names.get(str(container.user_id)),
    )


@router.delete("/{container_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_template_container(
    container_id: str,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _ai_gate: None = Depends(require_ai_enabled),
):
    """Delete a container and cascade-delete its assets (DB rows + S3 objects).

    The FK is ``ON DELETE CASCADE``, which handles the rows; the explicit
    cleanup call is what removes the S3 objects so binaries never leak.
    """
    container = await _get_writable_container(db, user, container_id, action="delete")
    from app.services.file_cleanup import delete_file_attachments

    removed_assets = await delete_file_attachments(
        db, template_container_id=container.id
    )
    await log_audit(
        db,
        user=user,
        action="delete",
        entity_type="template_container",
        entity_id=container.id,
        entity_name=container.name,
        summary=f"Deleted template container '{container.name}'",
        details={
            "kind": container.kind,
            "is_company_managed": container.is_company_managed,
            "assets_deleted": removed_assets,
        },
        request=request,
    )
    await db.delete(container)
    await db.commit()


# ---------------------------------------------------------------------------
# Asset management — independent of the container's other metadata
# ---------------------------------------------------------------------------


@router.get("/{container_id}/assets", response_model=list[TemplateAssetResponse])
async def list_template_assets(
    container_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List a container's assets."""
    container = await _get_visible_container(db, user, container_id)
    assets = await load_assets(db, container.id)
    return [TemplateAssetResponse(**serialize_asset(a)) for a in assets]


@router.post(
    "/{container_id}/assets",
    response_model=TemplateAssetResponse,
    status_code=status.HTTP_201_CREATED,
)
async def add_template_asset(
    container_id: str,
    request: Request,
    file: UploadFile = File(...),
    asset_role: str | None = Query(
        None, description=f"Structured role label, e.g. one of {list(ASSET_ROLES)}"
    ),
    description: str | None = Query(
        None,
        max_length=MAX_ASSET_DESCRIPTION_CHARS,
        description=(
            "What the asset depicts, for the agent that has to choose one "
            "('OpenAEV architecture: agents, injectors and collectors')."
        ),
    ),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _ai_gate: None = Depends(require_ai_enabled),
):
    """Add one asset to a container, leaving all other metadata untouched."""
    container = await _get_writable_container(db, user, container_id, action="edit")
    existing = await load_assets(db, container.id)
    if len(existing) >= MAX_ASSETS_PER_CONTAINER:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Container already holds the maximum of "
                f"{MAX_ASSETS_PER_CONTAINER} assets. Remove one first."
            ),
        )

    raw_bytes, filename, content_type = await _read_upload(file)
    raw_bytes, s3_object_name = await _store_asset(container, raw_bytes, filename, content_type)
    asset = FileAttachment(
        user_id=user.id,
        template_container_id=container.id,
        asset_role=_normalize_role(asset_role),
        description=normalize_asset_description(description),
        filename=filename,
        content_type=content_type,
        size=len(raw_bytes),
        s3_object_name=s3_object_name,
        sha256=await template_asset_sha256(raw_bytes),
        source=TEMPLATE_ASSET_SOURCE,
        image_meta=await _asset_geometry(raw_bytes, content_type),
    )
    db.add(asset)
    await db.flush()
    await log_audit(
        db,
        user=user,
        action="update",
        entity_type="template_container",
        entity_id=container.id,
        entity_name=container.name,
        summary=f"Added asset '{filename}' to template container '{container.name}'",
        details={
            "asset_id": str(asset.id),
            "asset_role": asset.asset_role,
            "filename": filename,
            "size": len(raw_bytes),
        },
        request=request,
    )
    await db.commit()
    await db.refresh(asset)
    return TemplateAssetResponse(**serialize_asset(asset))


@router.put(
    "/{container_id}/assets/{asset_id}", response_model=TemplateAssetResponse
)
async def replace_template_asset(
    container_id: str,
    asset_id: str,
    request: Request,
    file: UploadFile = File(...),
    asset_role: str | None = Query(
        None, description="New role label; omit to keep the existing one"
    ),
    description: str | None = Query(
        None,
        max_length=MAX_ASSET_DESCRIPTION_CHARS,
        description="New description; omit to keep the existing one",
    ),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _ai_gate: None = Depends(require_ai_enabled),
):
    """Replace an asset's binary **in place**, keeping its id.

    The id is preserved on purpose: a consumer feature (or a stored layout) may
    reference a specific asset id, so swapping a logo must not invalidate those
    references. The old S3 object is deleted after the new one is uploaded — if
    the upload fails, the original asset is still intact.
    """
    container = await _get_writable_container(db, user, container_id, action="edit")
    asset = await _get_asset(db, container, asset_id)

    raw_bytes, filename, content_type = await _read_upload(file)
    from app.services.file_storage import file_storage

    previous_object = asset.s3_object_name
    previous_filename = asset.filename
    raw_bytes, new_object_name = await _store_asset(container, raw_bytes, filename, content_type)

    asset.filename = filename
    asset.content_type = content_type
    asset.size = len(raw_bytes)
    asset.s3_object_name = new_object_name
    asset.image_meta = await _asset_geometry(raw_bytes, content_type)
    asset.sha256 = await template_asset_sha256(raw_bytes)
    if asset_role is not None:
        asset.asset_role = _normalize_role(asset_role)
    if description is not None:
        asset.description = normalize_asset_description(description)

    await log_audit(
        db,
        user=user,
        action="update",
        entity_type="template_container",
        entity_id=container.id,
        entity_name=container.name,
        summary=(
            f"Replaced asset '{previous_filename}' with '{filename}' in "
            f"template container '{container.name}'"
        ),
        details={
            "asset_id": str(asset.id),
            "asset_role": asset.asset_role,
            "filename": filename,
            "size": len(raw_bytes),
        },
        request=request,
    )
    await db.commit()
    await db.refresh(asset)

    # Only now that the new object name is durably committed do we drop the old
    # binary — a failure here leaks one object rather than losing the asset.
    if previous_object and previous_object != new_object_name:
        try:
            await file_storage.delete(previous_object)
        except Exception:
            logger.warning(
                "Failed to delete replaced template asset object %s", previous_object
            )

    return TemplateAssetResponse(**serialize_asset(asset))


@router.patch(
    "/{container_id}/assets/{asset_id}", response_model=TemplateAssetResponse
)
async def update_template_asset_role(
    container_id: str,
    asset_id: str,
    body: UpdateAssetRoleRequest,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _ai_gate: None = Depends(require_ai_enabled),
):
    """Relabel and/or re-describe an asset without re-uploading its binary.

    Patch semantics per field (see ``UpdateAssetRoleRequest``): only the fields
    the caller actually sent are touched, so relabelling a role never wipes the
    description the seed or an admin wrote for the asset.
    """
    container = await _get_writable_container(db, user, container_id, action="edit")
    asset = await _get_asset(db, container, asset_id)

    sent = body.model_fields_set
    if "asset_role" in sent:
        asset.asset_role = _normalize_role(body.asset_role)
    if "description" in sent:
        asset.description = normalize_asset_description(body.description)
    await log_audit(
        db,
        user=user,
        action="update",
        entity_type="template_container",
        entity_id=container.id,
        entity_name=container.name,
        summary=(
            f"Relabelled asset '{asset.filename}' in template container "
            f"'{container.name}'"
        ),
        details={
            "asset_id": str(asset.id),
            "asset_role": asset.asset_role,
            "description": asset.description,
        },
        request=request,
    )
    await db.commit()
    await db.refresh(asset)
    return TemplateAssetResponse(**serialize_asset(asset))


@router.get("/{container_id}/assets/{asset_id}/download")
async def download_template_asset(
    container_id: str,
    asset_id: str,
    if_none_match: Annotated[str | None, Header()] = None,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Send an asset's binary back to the caller.

    Answered through the FileAttachment byte contract: an asset is
    replaced in place under the same id, so the answer is revalidated on every
    use and a request that already holds the current bytes gets a ``304``.
    """
    container = await _get_visible_container(db, user, container_id)
    asset = await _get_asset(db, container, asset_id)

    not_modified = file_not_modified(asset, if_none_match)
    if not_modified is not None:
        return not_modified

    from app.services.file_storage import FileNotFoundInStorageError, file_storage

    try:
        data = await file_storage.download(asset.s3_object_name)
    except FileNotFoundInStorageError as exc:
        raise HTTPException(
            status_code=404, detail="Asset binary is no longer in storage"
        ) from exc

    # Always a download: the Templates page saves the file under the name this
    # header carries (``useDownloadTemplateAsset``).
    return Response(
        content=data,
        media_type=asset.content_type or "application/octet-stream",
        headers=file_response_headers(asset, inline_images=False),
    )


@router.get("/{container_id}/assets/{asset_id}/thumbnail")
async def thumbnail_template_asset(
    container_id: str,
    asset_id: str,
    size: int = Query(
        DEFAULT_THUMBNAIL_SIZE, description=f"long edge in px, one of {', '.join(map(str, THUMBNAIL_SIZES))}"
    ),
    if_none_match: Annotated[str | None, Header()] = None,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """A bounded thumbnail of an image asset, for the tiles that list them.

    Same visibility as the download. A raster image is decoded in full and
    resized server-side to at most ``size`` px on its long edge (PNG), an SVG
    is passed through, any other asset or undecodable image is a 415.
    Revalidated like every file answer: a matching ``If-None-Match`` is
    a 304, answered before the object store is read.
    """
    if size not in THUMBNAIL_SIZES:
        raise HTTPException(
            status_code=422, detail=f"size must be one of {', '.join(map(str, THUMBNAIL_SIZES))}"
        )
    container = await _get_visible_container(db, user, container_id)
    asset = await _get_asset(db, container, asset_id)
    kind = thumbnail_kind(asset.content_type)
    if kind is None:
        raise HTTPException(status_code=415, detail="This asset is not an image and has no thumbnail")
    etag = thumbnail_etag(asset, size)
    if if_none_match_hits(if_none_match, etag):
        return Response(status_code=304, headers=thumbnail_cache_headers(asset, etag))

    from app.services.file_storage import FileNotFoundInStorageError, file_storage

    # Nothing below reads the database: the pooled connection is not held
    # while a render waits at the gate, which it enters before the storage
    # read so that a burst of cold tiles never queues downloaded blobs.
    await release_db_connection_before_loop(db)
    async with thumbnail_render_gate() if kind == "raster" else contextlib.nullcontext():
        try:
            data = await file_storage.download(asset.s3_object_name)
        except FileNotFoundInStorageError as exc:
            raise HTTPException(status_code=404, detail="Asset binary is no longer in storage") from exc
        if kind == "svg":
            body, content_type = data, SVG_CONTENT_TYPE
        else:
            try:
                body = await render_thumbnail_async(data, size)
            except ThumbnailRefusedError as exc:
                raise HTTPException(status_code=415, detail=f"No thumbnail for this asset: {exc}") from exc
            content_type = THUMBNAIL_CONTENT_TYPE
    return Response(content=body, media_type=content_type, headers=thumbnail_headers(asset, content_type, etag))


@router.delete(
    "/{container_id}/assets/{asset_id}", status_code=status.HTTP_204_NO_CONTENT
)
async def delete_template_asset(
    container_id: str,
    asset_id: str,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _ai_gate: None = Depends(require_ai_enabled),
):
    """Remove one asset (DB row + S3 object), leaving the container intact."""
    container = await _get_writable_container(db, user, container_id, action="edit")
    asset = await _get_asset(db, container, asset_id)
    filename = asset.filename

    from app.services.file_cleanup import delete_file_attachments

    await delete_file_attachments(db, id=asset.id)
    await log_audit(
        db,
        user=user,
        action="update",
        entity_type="template_container",
        entity_id=container.id,
        entity_name=container.name,
        summary=(
            f"Removed asset '{filename}' from template container '{container.name}'"
        ),
        details={"asset_id": asset_id, "filename": filename},
        request=request,
    )
    await db.commit()


# Re-exported so callers/tests can assert the shared search cap without
# reaching into the service module.
__all__ = ["MAX_SEARCH_RESULTS", "router"]
