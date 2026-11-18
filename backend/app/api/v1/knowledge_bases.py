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

"""Knowledge Base endpoints – CRUD + document management.

Knowledge bases are vectorized document collections used by agents for RAG.
Users can upload files, paste text/markdown, or provide URLs to crawl.
"""

import asyncio
import contextlib
import hashlib
import io
import json
import logging
import re
import string
import uuid
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import PurePosixPath
from typing import Annotated, Any, Literal, cast

import httpx
import numpy as np
from fastapi import (
    APIRouter,
    Depends,
    File,
    HTTPException,
    Query,
    Request,
    UploadFile,
    status,
)
from pydantic import ValidationError
from sqlalchemy import ColumnElement, Integer, and_, case, delete, func, literal, or_, select, text, update
from sqlalchemy import Text as SAText
from sqlalchemy import cast as sa_cast
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import (
    get_current_user,
    require_admin,
    require_agentic_ai,
    require_ai_enabled,
)
from app.api.v1.helpers import (
    ENTITY_LIST_SCOPE_DESCRIPTION,
    EntityListScope,
    apply_group_share_fields,
    apply_owner_transfer,
    apply_user_share_fields,
    assert_knowledge_base_not_package_origin,
    assert_knowledge_base_not_workspace_managed,
    assert_writable_knowledge_base,
    can_manage,
    can_manage_entity,
    enqueue_document_processing,
    enqueue_documents,
    get_user_group_ids,
    get_user_knowledge_base,
    handle_company_managed_toggle,
    integration_visible_to_user,
    is_package_origin_knowledge_base,
    knowledge_base_content_lock,
    list_scope_admin_bypass,
    log_kb_activity,
    owning_workspace_id,
    package_origin_knowledge_base_ids,
    parse_path_id,
    parse_uuid,
    require_admin_for_company_managed,
    resolve_owner_names,
    user_visible_filter,
    user_visible_query,
    wants_access_restriction,
)
from app.api.v1.schemas.knowledge_base import (
    AddTextDocumentRequest,
    CrawlURLRequest,
    CreateKnowledgeBaseRequest,
    CreateSyncSourceRequest,
    GlobalKnowledgeDocumentResponse,
    GlobalKnowledgeDocumentsResponse,
    GlobalTestSearchRequest,
    GlobalTestSearchResponse,
    GlobalTestSearchResultItem,
    KBActivityLogResponse,
    KBExportDocument,
    KBExportKnowledgeBase,
    KBExportManifest,
    KBExportPackageResponse,
    KBExportResponse,
    KBExportSyncSource,
    KnowledgeBaseResponse,
    KnowledgeDocumentDetailResponse,
    KnowledgeDocumentResponse,
    ManagingWorkspace,
    SyncDriveRequest,
    SyncGitHubRequest,
    SyncGoogleDriveRequest,
    SyncNotionRequest,
    SyncResponse,
    SyncSourceResponse,
    TestSearchRequest,
    TestSearchResponse,
    TestSearchResultItem,
    UpdateKnowledgeBaseRequest,
    UpdateKnowledgeDocumentRequest,
    UpdateSyncSourceRequest,
)
from app.core.archive_reading import ArchiveBudget, ArchiveTooLargeError
from app.core.database import get_db
from app.core.egress import egress_route
from app.core.utils import build_content_disposition, format_exception_message, safe_uuid, slugify
from app.models.embedding import Embedding
from app.models.knowledge_base import (
    KBActivityLog,
    KBExportPackage,
    KBSyncSource,
    KnowledgeBase,
    KnowledgeDocument,
)
from app.models.user import User
from app.services import kb_documents
from app.services.audit import log_audit
from app.services.chunking import chunk_params_problem, normalize_chunk_params
from app.services.file_storage import file_storage
from app.services.integration_credentials import canonical_integration_ids
from app.services.integration_user_credentials import holder_connected, usable_credential_holder
from app.services.kb_document_errors import (
    EMBEDDING_FAILED_EVENT,
    EMBEDDING_FAILED_PREFIX,
    EMBEDDING_FAILED_SEPARATOR,
    INTERNAL_ERROR_MESSAGE,
    PLATFORM_SENTENCES,
    UNSHAPED_EMBEDDING_FAILED_DETAILS,
    VALUED_SENTENCES_FOR_SQL,
    public_error_code,
    public_error_message,
)
from app.services.kb_embedding_text import normalize_html_ingestion, renders_html
from app.services.kb_figures import export_fields, figure_state, imported_reading
from app.services.kb_helpers import (
    bump_kb_revision,
    cancel_document_jobs_after_commit,
    enqueue_reembedding,
    kb_sync_log_context,
    reset_changed_document,
)
from app.services.kb_sync_errors import (
    ACTIVITY_READER_RULES,
    LAST_SYNC_ERROR,
    READER_SENTENCES,
    READER_VISIBLE_SYNC_EVENTS,
    SYNC_EVENT_PREFIX,
    UNLISTED_SYNC_EVENT_RULE,
    served_sync_text,
)

logger = logging.getLogger(__name__)

router = APIRouter()

# Strong references to in-flight background sync tasks. asyncio only keeps
# weak references to running tasks, so a bare create_task() result can be
# garbage-collected mid-run and silently kill the sync.
_BACKGROUND_SYNC_TASKS: set = set()


def _spawn_sync_task(coro) -> None:
    """Launch a background KB sync, retaining a strong task reference."""
    import asyncio

    task = asyncio.create_task(coro)
    _BACKGROUND_SYNC_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_SYNC_TASKS.discard)


_KB_EXPORT_FORMAT = "xtm-one-knowledge-base"
_KB_EXPORT_VERSION = 1
_KB_EXPORT_PART_TARGET_BYTES = 45 * 1024 * 1024
_KB_EXPORT_PART_MAX_BYTES = 50 * 1024 * 1024
_KB_EXPORT_TTL_HOURS = 24
# Cap the total raw document content held in memory while building an export.
# Mirrors the import-side total cap below: an export larger than this could not
# be re-imported anyway, and the bound prevents an unbounded in-memory build.
_KB_EXPORT_MAX_TOTAL_BYTES = 500 * 1024 * 1024
# Cap the slug used in object keys / Content-Disposition; KB names are unbounded.
_KB_EXPORT_SLUG_MAX_LEN = 60

_KB_IMPORT_MAX_PACKAGE_BYTES = 50 * 1024 * 1024
# Per-file import cap must be >= the export per-document cap, otherwise a
# document between the two limits would export successfully but be impossible to
# re-import (non-roundtrippable). Tie them together so they can never drift.
_KB_IMPORT_MAX_FILE_BYTES = _KB_EXPORT_PART_MAX_BYTES
_KB_IMPORT_MAX_TOTAL_UNCOMPRESSED_BYTES = 500 * 1024 * 1024
_KB_IMPORT_MAX_FILE_COUNT = 10_000
# Upper bound on the number of uploaded ZIP parts accepted up-front. A legitimate
# export is bounded to a handful of parts (total content cap / part size), so
# this only blocks an attacker flooding many small per-ZIP-valid parts to drive
# CPU/DB work before the aggregate uncompressed-size guard trips.
_KB_IMPORT_MAX_PARTS = 100

_SAFE_EXPORT_NAME_RE = re.compile(r"[^a-zA-Z0-9._-]+")


def _sanitize_export_filename(filename: str, *, fallback: str) -> str:
    """Return a ZIP-safe filename fragment for exported document payloads."""
    name = (filename or "").strip()
    if not name:
        name = fallback
    name = name.replace("\\", "/")
    name = name.rsplit("/", 1)[-1]
    name = _SAFE_EXPORT_NAME_RE.sub("-", name).strip("-._")
    return name[:180] or fallback


def _content_extension_for_source(source_type: str) -> str:
    mapping = {
        "markdown": ".md",
        "text": ".txt",
        "json": ".json",
        "csv": ".csv",
        "url": ".md",
        "pdf": ".txt",
        "doc": ".txt",
        "docx": ".txt",
        "pptx": ".txt",
        "xls": ".txt",
        "xlsx": ".txt",
    }
    return mapping.get((source_type or "").lower(), ".txt")


def _safe_zip_names(raw: bytes) -> list[str]:
    """Validate ZIP metadata before extraction to prevent zip bombs/slip."""
    if len(raw) > _KB_IMPORT_MAX_PACKAGE_BYTES:
        raise HTTPException(
            status_code=400,
            detail=(
                "ZIP package exceeds maximum size of "
                f"{_KB_IMPORT_MAX_PACKAGE_BYTES // (1024 * 1024)} MB."
            ),
        )

    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            infos = [info for info in zf.infolist() if not info.filename.endswith("/")]
    except zipfile.BadZipFile as err:
        raise HTTPException(status_code=400, detail="Invalid ZIP file.") from err

    if not infos:
        raise HTTPException(status_code=400, detail="ZIP package is empty.")
    if len(infos) > _KB_IMPORT_MAX_FILE_COUNT:
        raise HTTPException(
            status_code=400,
            detail=f"ZIP contains too many files (max {_KB_IMPORT_MAX_FILE_COUNT}).",
        )

    total_uncompressed = sum(info.file_size for info in infos)
    if total_uncompressed > _KB_IMPORT_MAX_TOTAL_UNCOMPRESSED_BYTES:
        raise HTTPException(
            status_code=400,
            detail=(
                "ZIP decompressed size exceeds maximum of "
                f"{_KB_IMPORT_MAX_TOTAL_UNCOMPRESSED_BYTES // (1024 * 1024)} MB."
            ),
        )

    names = [info.filename for info in infos]
    for name in names:
        if "\\" in name:
            raise HTTPException(status_code=400, detail=f"Unsafe ZIP path: {name}")
        path = PurePosixPath(name)
        if path.is_absolute() or ".." in path.parts:
            raise HTTPException(status_code=400, detail=f"Unsafe ZIP path: {name}")

    return names


def _combined_size_error() -> HTTPException:
    return HTTPException(
        status_code=400,
        detail=(
            "Combined decompressed size of the provided ZIP parts "
            "exceeds the maximum of "
            f"{_KB_IMPORT_MAX_TOTAL_UNCOMPRESSED_BYTES // (1024 * 1024)} MB."
        ),
    )


def _read_zip_member(
    zf: zipfile.ZipFile, name: str, *, error: str, budget: ArchiveBudget
) -> bytes:
    """A ZIP member's bytes, spent against the import's *budget*.

    The declared size is an early refusal only: whoever built the archive sets
    it. ``ArchiveBudget.read`` decompresses at most one byte past the smaller of
    the per-file cap, what is left of the import's budget and what the member
    declares, so an under-declared member is refused as unreadable having
    decompressed no more than it declared. Synchronous: the route runs it with
    ``asyncio.to_thread``.
    """
    try:
        info = zf.getinfo(name)
    except KeyError as err:
        raise HTTPException(status_code=400, detail=error) from err

    too_large = HTTPException(
        status_code=400,
        detail=(
            f"ZIP file '{name}' exceeds max size "
            f"{_KB_IMPORT_MAX_FILE_BYTES // (1024 * 1024)} MB."
        ),
    )
    if info.file_size > _KB_IMPORT_MAX_FILE_BYTES:
        raise too_large
    try:
        return budget.read(zf, info, max_member=_KB_IMPORT_MAX_FILE_BYTES)
    except ArchiveTooLargeError:
        if budget.remaining >= _KB_IMPORT_MAX_FILE_BYTES:
            raise too_large from None
        raise _combined_size_error() from None
    except zipfile.BadZipFile as err:
        raise HTTPException(status_code=400, detail="Invalid ZIP file.") from err


def _read_document_payloads(
    zf: zipfile.ZipFile, names: list[str], *, budget: ArchiveBudget
) -> list[str]:
    """Each of *names* read with ``_read_zip_member`` and decoded as UTF-8, in one
    worker thread; a payload's bytes go as soon as it is decoded."""
    contents: list[str] = []
    for name in names:
        payload = _read_zip_member(
            zf, name, error=f"Cannot read document payload '{name}'", budget=budget
        )
        try:
            contents.append(payload.decode("utf-8"))
        except UnicodeDecodeError as exc:
            raise HTTPException(
                status_code=400,
                detail=f"Document payload '{name}' must be UTF-8 text",
            ) from exc
    return contents


def _partition_export_items(
    items: list[dict[str, object]],
    *,
    target_part_bytes: int,
) -> list[list[dict[str, object]]]:
    """Partition document items into stable chunks using raw-content size."""
    if not items:
        return [[]]

    chunks: list[list[dict[str, object]]] = []
    current: list[dict[str, object]] = []
    current_size = 0

    for item in items:
        content_bytes = cast(bytes | None, item.get("content_bytes"))
        item_size = len(content_bytes) if content_bytes is not None else 1

        # Single huge source cannot be split across parts.
        if content_bytes is not None and item_size > _KB_EXPORT_PART_MAX_BYTES:
            doc = cast(KnowledgeDocument, item["doc"])
            raise HTTPException(
                status_code=413,
                detail=(
                    f"Document '{doc.filename}' is too large to export "
                    f"(>{_KB_EXPORT_PART_MAX_BYTES // (1024 * 1024)} MB)."
                ),
            )

        if current and (current_size + item_size) > target_part_bytes:
            chunks.append(current)
            current = []
            current_size = 0

        current.append(item)
        current_size += item_size

    if current:
        chunks.append(current)

    return chunks or [[]]


def _build_kb_export_manifest(
    *,
    kb: KnowledgeBase,
    export_id: uuid.UUID,
    part_number: int,
    total_parts: int,
    documents: list[KBExportDocument],
    sync_sources: list[KBExportSyncSource],
) -> KBExportManifest:
    # The values the collection is actually chunked with: a row written before
    # the bounds existed exports as its clamped, importable values.
    chunk_size, chunk_overlap = normalize_chunk_params(kb.chunk_size, kb.chunk_overlap)
    return KBExportManifest(
        format=_KB_EXPORT_FORMAT,
        version=_KB_EXPORT_VERSION,
        exported_at=datetime.now(UTC).isoformat(),
        export_id=str(export_id),
        part=part_number,
        total_parts=total_parts,
        knowledge_base=KBExportKnowledgeBase(
            name=kb.name,
            description=kb.description,
            tags=kb.tags or [],
            embedding_model=kb.embedding_model,
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
            html_ingestion=normalize_html_ingestion(getattr(kb, "html_ingestion", None)),
            describe_figures=bool(getattr(kb, "describe_figures", False)),
        ),
        documents=documents,
        sync_sources=sync_sources,
    )


def _render_export_zip_part(
    *,
    kb: KnowledgeBase,
    export_id: uuid.UUID,
    items: list[dict[str, object]],
    part_number: int,
    total_parts: int,
    sync_sources: list[KBExportSyncSource],
) -> tuple[bytes, list[KBExportDocument]]:
    """Render one export ZIP part and return bytes + part document manifest."""
    doc_entries: list[KBExportDocument] = []
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for item in items:
            doc = cast(KnowledgeDocument, item["doc"])
            archive_path = cast(str | None, item.get("archive_path"))
            content_bytes = cast(bytes | None, item.get("content_bytes"))

            if archive_path and content_bytes is not None:
                zf.writestr(archive_path, content_bytes)

            doc_entries.append(
                KBExportDocument(
                    filename=doc.filename,
                    source_type=doc.source_type,
                    source_url=doc.source_url,
                    archive_path=archive_path,
                    **export_fields(getattr(doc, "figure_reading", None), getattr(doc, "content", None)),
                )
            )

        manifest = _build_kb_export_manifest(
            kb=kb,
            export_id=export_id,
            part_number=part_number,
            total_parts=total_parts,
            documents=doc_entries,
            sync_sources=sync_sources,
        )
        zf.writestr(
            "manifest.json",
            json.dumps(manifest.model_dump(), ensure_ascii=False, indent=2),
        )

    return buffer.getvalue(), doc_entries


def _kb_export_package_to_response(pkg: KBExportPackage) -> KBExportPackageResponse:
    return KBExportPackageResponse(
        id=str(pkg.id),
        export_id=str(pkg.export_id),
        part_number=pkg.part_number,
        total_parts=pkg.total_parts,
        filename=pkg.filename,
        file_size=pkg.file_size,
        expires_at=pkg.expires_at.isoformat(),
        download_url=f"/api/v1/knowledge-bases/exports/{pkg.id}/download",
    )


# ---------------------------------------------------------------------------
# Knowledge Base CRUD
# ---------------------------------------------------------------------------


@router.get("", response_model=list[KnowledgeBaseResponse])
async def list_knowledge_bases(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    integration_id: str | None = None,
    scope: EntityListScope = Query("visible", description=ENTITY_LIST_SCOPE_DESCRIPTION),
):
    # Own + group-shared + company-managed KBs; every KB for an admin reading
    # ``scope=all`` (the knowledge bases a detail page links to).
    user_group_ids = await get_user_group_ids(db, user.id)
    query = user_visible_query(
        KnowledgeBase,
        user.id,
        user_group_ids,
        is_admin=list_scope_admin_bypass(scope, user),
    )

    # Optional filter: only KBs with sync sources bound to a specific integration
    if integration_id:
        sub = (
            select(KBSyncSource.knowledge_base_id)
            .where(KBSyncSource.integration_id == parse_uuid(integration_id, label="integration ID"))
            .distinct()
        )
        query = query.where(KnowledgeBase.id.in_(sub))

    result = await db.execute(query)
    kbs: list[KnowledgeBase] = cast(list[KnowledgeBase], result.scalars().all())
    names = await resolve_owner_names(db, kbs)
    stale_info_by_kb = await _load_kb_stale_embedding_info(db, kbs)
    # Batch the document status counts + latest error across the whole page
    # (two queries total) so ``_kb_to_response`` fires no per-KB queries —
    # the N+1 that 500s this endpoint under load.
    counts_by_kb = await _load_kb_status_counts(db, kbs)
    errors_by_kb = await _load_kb_latest_errors(db, kbs)
    next_retries_by_kb = await _load_kb_next_auto_retries(db, kbs)
    from_packages = await package_origin_knowledge_base_ids(db, [kb.id for kb in kbs])
    workspace_names = await _load_workspace_names(db, kbs)
    responses: list[KnowledgeBaseResponse] = []
    for kb in kbs:
        retry_info = next_retries_by_kb.get(str(kb.id))
        responses.append(
            await _kb_to_response(
                kb,
                db,
                can_manage=can_manage(user, kb, user_group_ids),
                is_package_origin=str(kb.id) in from_packages,
                owner_name=names.get(str(kb.user_id)),
                stale_info=stale_info_by_kb.get(str(kb.id)),
                status_counts=counts_by_kb.get(str(kb.id), {}),
                latest_error=errors_by_kb.get(str(kb.id)),
                next_retry=retry_info[0] if retry_info else None,
                retryable_error_count=retry_info[1] if retry_info else 0,
                workspace_names=workspace_names,
            )
        )
    return responses


async def _assert_kb_manageable(
    db: AsyncSession,
    user: User,
    kb: KnowledgeBase,
    action: str = "modify",
    *,
    user_group_ids: list[str] | None = None,
) -> None:
    """Enforce the full write gate on a knowledge base.

    ``get_user_knowledge_base`` only enforces *visibility* (own + group-shared +
    company-managed), so mutation routes must additionally check that the user
    may *manage* the KB: company-managed KBs require admin, and group-shared KBs
    require config-tier permission (a ``read_only`` member can view but not
    mutate). Mirrors the prompts / custom-tools / MCP-servers write contract.
    Content writes go through :func:`assert_writable_knowledge_base` instead,
    which runs this same rule between the KB's own locks.
    """
    if owning_workspace_id(kb) is not None:
        # A workspace's knowledge base is managed by whoever manages the
        # workspace: its own share columns are empty by design.
        if user_group_ids is None:
            user_group_ids = await get_user_group_ids(db, user.id)
        if not await can_manage_entity(db, user, kb, user_group_ids):
            raise HTTPException(
                status_code=403,
                detail=f"You don't have permission to {action} this knowledge base.",
            )
        return
    require_admin_for_company_managed(
        kb, user, f"Only admins can {action} company-managed knowledge bases"
    )
    if user_group_ids is None:
        user_group_ids = await get_user_group_ids(db, user.id)
    if not can_manage(user, kb, user_group_ids):
        raise HTTPException(
            status_code=403,
            detail=f"You don't have permission to {action} this knowledge base.",
        )


@router.post(
    "", response_model=KnowledgeBaseResponse, status_code=status.HTTP_201_CREATED
)
async def create_knowledge_base(
    body: CreateKnowledgeBaseRequest,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _agentic_gate: None = Depends(require_agentic_ai),
):
    require_admin_for_company_managed(
        body, user, "Only admins can create company-managed knowledge bases"
    )

    # Resolve embedding model: use platform default when not explicitly specified
    embedding_model = body.embedding_model
    if not embedding_model:
        try:
            from app.llm.router import get_embedding_provider

            _, platform_embed_model, _provider_id = await get_embedding_provider(db)
            embedding_model = platform_embed_model
        except ValueError:
            embedding_model = "text-embedding-3-large"  # last-resort fallback

    kb = KnowledgeBase(
        user_id=user.id,
        is_company_managed=body.is_company_managed,
        name=body.name,
        description=body.description,
        tags=body.tags or [],
        is_read_only=body.is_read_only,
        html_ingestion=body.html_ingestion,
        describe_figures=body.describe_figures,
        embedding_model=embedding_model,
        chunk_size=body.chunk_size,
        chunk_overlap=body.chunk_overlap,
    )
    await apply_group_share_fields(db, kb, body, partial=False)
    await apply_user_share_fields(db, kb, body, partial=False)
    db.add(kb)
    await db.flush()
    await log_audit(
        db,
        user=user,
        action="create",
        entity_type="knowledge_base",
        entity_id=kb.id,
        entity_name=kb.name,
        summary=f"Created knowledge base '{kb.name}'",
        request=request,
        details={
            "is_company_managed": kb.is_company_managed,
            "is_read_only": kb.is_read_only,
            "html_ingestion": kb.html_ingestion,
            "describe_figures": bool(getattr(kb, "describe_figures", False)),
            "tags": kb.tags or [],
            "embedding_model": kb.embedding_model,
        },
    )
    # Commit before answering: the page opens the new knowledge base right away,
    # and the get_db teardown only commits after the response is sent.
    await db.commit()
    return await _kb_to_response(
        kb,
        db,
        can_manage=can_manage(user, kb),
        # Created here, not by a package import: no origin link exists.
        is_package_origin=False,
        owner_name=user.full_name,
    )


#: The ``detail.code`` of the 404 a workset's storage answers with.
WORKSET_STORAGE_CODE = "workset_storage"


async def _workset_storage_of(db: AsyncSession, user: User, kb_uuid: uuid.UUID) -> dict[str, str] | None:
    """What ``GET /knowledge-bases/{id}`` says of a workset's internal corpus.

    That knowledge base is hidden from every read (``is_internal``), so an old
    link or an audit entry to it answers 404; the detail names the workset that
    owns it when the caller may read that workset (own, company-managed, or an
    admin), so the page can say what it is and where to find it. Anything else
    (no such id, a workset the caller cannot read) stays a plain 404.
    """
    from app.models.workset import Workset

    workset = (await db.execute(select(Workset).where(Workset.knowledge_base_id == kb_uuid))).scalars().first()
    if workset is None:
        return None
    if not (user.is_admin or workset.is_company_managed or workset.user_id == user.id):
        return None
    return {
        "code": WORKSET_STORAGE_CODE,
        "message": "This knowledge base is the internal storage of a workset.",
        "workset_id": str(workset.id),
        "workset_name": workset.label or workset.key,
    }


@router.get("/{kb_id}", response_model=KnowledgeBaseResponse)
async def get_knowledge_base(
    kb_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    kb_uuid = parse_path_id(kb_id, not_found_detail="Knowledge base not found")
    user_group_ids = await get_user_group_ids(db, user.id)
    try:
        kb = await get_user_knowledge_base(db, user.id, kb_id, user_group_ids, is_admin=user.is_admin)
    except HTTPException as exc:
        if exc.status_code == 404 and (storage := await _workset_storage_of(db, user, kb_uuid)):
            raise HTTPException(status_code=404, detail=storage) from exc
        raise
    owner_name = None
    if kb.user_id:
        owner_row = await db.execute(
            select(User.full_name).where(User.id == kb.user_id)
        )
        owner_name = owner_row.scalar_one_or_none()
    stale_info_by_kb = await _load_kb_stale_embedding_info(db, [kb])
    return await _kb_to_response(
        kb,
        db,
        can_manage=can_manage(user, kb, user_group_ids),
        is_package_origin=await is_package_origin_knowledge_base(db, kb.id),
        owner_name=owner_name,
        stale_info=stale_info_by_kb.get(str(kb.id)),
    )


@router.put("/{kb_id}", response_model=KnowledgeBaseResponse)
async def update_knowledge_base(
    kb_id: str,
    body: UpdateKnowledgeBaseRequest,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _ai_gate: None = Depends(require_ai_enabled),
):
    user_group_ids = await get_user_group_ids(db, user.id)
    kb = await get_user_knowledge_base(
        db, user.id, kb_id, user_group_ids, is_admin=user.is_admin
    )
    await assert_knowledge_base_not_package_origin(db, kb.id)
    await assert_knowledge_base_not_workspace_managed(db, kb)

    if kb.is_builtin:
        raise HTTPException(
            status_code=403, detail="Built-in knowledge bases cannot be modified"
        )

    await _assert_kb_manageable(db, user, kb, "edit", user_group_ids=user_group_ids)

    if body.name is not None:
        kb.name = body.name
    if body.description is not None:
        kb.description = body.description
    if body.tags is not None:
        kb.tags = body.tags
    # The content lock is config, not content: whoever may administer the KB may
    # lock and unlock it. That is deliberate — the flag protects an authoritative
    # collection from *agents* and from accidental edits, and unlocking is an
    # explicit, audited act by someone who already owns the collection.
    if body.is_read_only is not None:
        kb.is_read_only = body.is_read_only
    # How HTML documents are embedded. Config, gated and audited like
    # the rest of this request. It re-embeds nothing here: documents processed
    # afterwards use it, and the existing ones follow on an explicit reprocess.
    if body.html_ingestion is not None:
        kb.html_ingestion = body.html_ingestion
    # Whether uploaded PDFs get their figures described. Config, gated and
    # audited like the rest; it applies to PDFs uploaded afterwards.
    if body.describe_figures is not None:
        kb.describe_figures = body.describe_figures

    # Group sharing
    await apply_group_share_fields(db, kb, body, partial=True)

    # Toggle company-managed status (admin only)
    await handle_company_managed_toggle(
        db,
        user,
        kb,
        body.is_company_managed,
        agent_field="knowledge_base_ids",
        keep_grants=wants_access_restriction(body, kb),
    )

    # Per-user sharing — after the company-managed toggle, since
    # ``access_restricted`` is only meaningful relative to it.
    await apply_user_share_fields(db, kb, body, partial=True)

    # Owner transfer (admin only)
    await apply_owner_transfer(
        db,
        actor=user,
        entity=kb,
        new_owner_id=body.user_id,
        entity_type="knowledge_base",
        entity_name=kb.name,
        request=request,
    )

    await log_audit(
        db,
        user=user,
        action="update",
        entity_type="knowledge_base",
        entity_id=kb.id,
        entity_name=kb.name,
        summary=f"Updated knowledge base '{kb.name}'",
        request=request,
        details={
            "is_company_managed": kb.is_company_managed,
            "is_read_only": kb.is_read_only,
            "html_ingestion": kb.html_ingestion,
            "describe_figures": bool(getattr(kb, "describe_figures", False)),
            "tags": kb.tags or [],
            "embedding_model": kb.embedding_model,
            "document_count": kb.document_count or 0,
        },
    )
    # Commit HERE, not on ``get_db`` teardown, which runs after the response is
    # sent: the page refetches the knowledge base the moment this answers, and
    # a Re-embed clicked right after must process documents under the new
    # settings, not the ones a refetch that beat the commit would still read.
    await db.commit()

    owner_name = None
    if kb.user_id:
        owner_row = await db.execute(
            select(User.full_name).where(User.id == kb.user_id)
        )
        owner_name = owner_row.scalar_one_or_none()
    return await _kb_to_response(
        kb,
        db,
        can_manage=can_manage(user, kb, user_group_ids),
        # The route refused a package-installed KB before writing anything.
        is_package_origin=False,
        owner_name=owner_name,
    )


@router.post(
    "/{kb_id}/documents/crawl",
    response_model=KnowledgeDocumentResponse,
    status_code=status.HTTP_201_CREATED,
)
async def crawl_url_document(
    kb_id: str,
    body: CrawlURLRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _ai_gate: None = Depends(require_ai_enabled),
):
    """Crawl a URL, extract text, and add as a document to the knowledge base."""
    kb = await get_user_knowledge_base(db, user.id, kb_id, is_admin=user.is_admin)
    await assert_writable_knowledge_base(
        db, kb, user, action="add documents to", content_action="add documents to it"
    )
    try:
        doc = await kb_documents.add_url_document(db, kb, url=body.url)
    except kb_documents.DocumentRefusedError as exc:
        raise HTTPException(status_code=400, detail=exc.detail) from None

    doc_id = str(doc.id)
    await db.commit()
    await enqueue_document_processing(doc_id)

    return _doc_to_response(doc, kb)


@router.delete("/{kb_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_knowledge_base(
    kb_id: str,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _ai_gate: None = Depends(require_ai_enabled),
):
    from sqlalchemy import delete as sql_delete

    from app.models.agent import Agent
    from app.models.embedding import Embedding

    kb = await get_user_knowledge_base(db, user.id, kb_id, is_admin=user.is_admin)
    # A workspace's own knowledge base goes with its workspace, never alone.
    await assert_knowledge_base_not_workspace_managed(db, kb)
    await assert_writable_knowledge_base(
        db, kb, user, action="delete", content_action="delete it", participle="deleted"
    )

    await log_audit(
        db,
        user=user,
        action="delete",
        entity_type="knowledge_base",
        entity_id=kb.id,
        entity_name=kb.name,
        summary=f"Deleted knowledge base '{kb.name}'",
        request=request,
        details={
            "is_company_managed": kb.is_company_managed,
            "tags": kb.tags or [],
        },
    )

    # Block deletion of system-managed KBs that are still bound to an agent
    if kb.is_system_managed:
        agents_result = await db.execute(select(Agent).where(Agent.user_id == user.id))
        for agent in agents_result.scalars().all():
            if agent.knowledge_base_ids and str(kb.id) in agent.knowledge_base_ids:
                raise HTTPException(
                    status_code=400,
                    detail=f"This knowledge base is system-managed and bound to agent '{agent.name}'. Delete the agent first, or unbind it in the agent configuration.",
                )

    # Purge stale references from agents and assignments
    from app.models.assignment import Assignment

    # Pre-filter both scans with a text ``contains`` so only rows that
    # actually reference this KB are loaded (the exact checks below stay
    # authoritative) instead of materializing every agent/assignment.
    kid = str(kb.id)
    agents_result = await db.execute(
        select(Agent).where(sa_cast(Agent.knowledge_base_ids, SAText).contains(kid))
    )
    for agent in agents_result.scalars().all():
        if agent.knowledge_base_ids and kid in agent.knowledge_base_ids:
            agent.knowledge_base_ids = [v for v in agent.knowledge_base_ids if v != kid]

    assignments_result = await db.execute(
        select(Assignment).where(
            or_(
                sa_cast(Assignment.read_knowledge_base_ids, SAText).contains(kid),
                sa_cast(Assignment.write_knowledge_base_ids, SAText).contains(kid),
                sa_cast(Assignment.target_knowledge_base_id, SAText) == kid,
            )
        )
    )
    for assignment in assignments_result.scalars().all():
        if (
            assignment.read_knowledge_base_ids
            and kid in assignment.read_knowledge_base_ids
        ):
            assignment.read_knowledge_base_ids = [
                v for v in assignment.read_knowledge_base_ids if v != kid
            ]
        if (
            assignment.write_knowledge_base_ids
            and kid in assignment.write_knowledge_base_ids
        ):
            # Keep an emptied subset as [] (write "none"), never None — None
            # would silently widen write access to ALL agent-writable KBs.
            assignment.write_knowledge_base_ids = [
                v for v in assignment.write_knowledge_base_ids if v != kid
            ]
        if (
            assignment.target_knowledge_base_id
            and str(assignment.target_knowledge_base_id) == kid
        ):
            assignment.target_knowledge_base_id = None

    # Clean up embeddings and documents first
    doc_ids_result = await db.execute(
        select(KnowledgeDocument.id).where(KnowledgeDocument.knowledge_base_id == kb.id)
    )
    doc_ids = [str(doc_id) for doc_id in doc_ids_result.scalars().all()]

    await db.execute(sql_delete(Embedding).where(Embedding.knowledge_base_id == kb.id))
    await db.execute(
        sql_delete(KnowledgeDocument).where(
            KnowledgeDocument.knowledge_base_id == kb.id
        )
    )
    cancel_document_jobs_after_commit(db, doc_ids)
    await db.delete(kb)
    # Commit before answering: the get_db teardown only commits after the response is sent.
    await db.commit()


@router.get("/{kb_id}/exports", response_model=list[KBExportPackageResponse])
async def list_kb_export_packages(
    kb_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List active export packages generated by the current user for a KB."""
    kb = await get_user_knowledge_base(db, user.id, kb_id, is_admin=user.is_admin)
    now = datetime.now(UTC)
    result = await db.execute(
        select(KBExportPackage)
        .where(
            KBExportPackage.knowledge_base_id == kb.id,
            KBExportPackage.user_id == user.id,
            KBExportPackage.expires_at > now,
        )
        .order_by(
            KBExportPackage.expires_at.desc(),
            KBExportPackage.export_id.asc(),
            KBExportPackage.part_number.asc(),
        )
    )
    return [_kb_export_package_to_response(p) for p in result.scalars().all()]


@router.get("/exports/{package_id}/download")
async def download_kb_export_package(
    package_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Serve an exported KB package part as an authenticated ZIP download.

    The object is fetched from storage and returned in full (buffered, not
    chunk-streamed) through this same-origin endpoint, behind the auth /
    ownership / expiry / KB-access / audit checks below, instead of redirecting
    to a pre-signed object-store URL: the SPA authenticates with a bearer token,
    which a raw browser redirect cannot carry, and same-origin delivery also
    avoids cross-origin object-store fetches. Mirrors the chat file and KB
    document download endpoints (per-part size is bounded on export).
    """
    from fastapi.responses import Response

    from app.services.file_storage import FileNotFoundInStorageError

    parsed_id = parse_path_id(package_id, not_found_detail="Export package not found")

    result = await db.execute(
        select(KBExportPackage).where(KBExportPackage.id == parsed_id)
    )
    package = result.scalar_one_or_none()
    if not package:
        raise HTTPException(status_code=404, detail="Export package not found")

    if not user.is_admin and package.user_id != user.id:
        raise HTTPException(status_code=404, detail="Export package not found")

    now = datetime.now(UTC)
    if package.expires_at <= now:
        await file_storage.delete(package.s3_object_name)
        await db.delete(package)
        await db.commit()
        raise HTTPException(status_code=410, detail="Export package expired")

    # Re-verify the caller still has access to the underlying knowledge base.
    # Owning the package row is not sufficient: a user whose KB access was
    # revoked (e.g. company-managed -> private, or removed from a sharing
    # group) must not keep downloading snapshots for the package's lifetime.
    # Mirrors download_document, which gates every download on live KB access.
    # Raises 404 when the KB is no longer visible to the caller.
    await get_user_knowledge_base(
        db, user.id, str(package.knowledge_base_id), is_admin=user.is_admin
    )

    try:
        data = await file_storage.download(package.s3_object_name)
    except FileNotFoundInStorageError:
        raise HTTPException(
            status_code=404,
            detail="Export package file is no longer available",
        ) from None

    # Audit only after the object is successfully fetched, so a missing-object
    # 404 never records a successful download in the audit trail.
    await log_audit(
        db,
        user=user,
        action="download",
        entity_type="knowledge_base",
        entity_id=package.knowledge_base_id,
        entity_name=None,
        summary="Downloaded knowledge base export package",
        details={
            "package_id": str(package.id),
            "export_id": str(package.export_id),
            "filename": package.filename,
        },
    )
    # Commit before answering: the get_db teardown only commits after the response is sent.
    await db.commit()

    return Response(
        content=data,
        media_type="application/zip",
        headers={
            "Content-Disposition": build_content_disposition(package.filename),
        },
    )


@router.post(
    "/{kb_id}/export",
    response_model=KBExportResponse,
    status_code=status.HTTP_201_CREATED,
)
async def export_knowledge_base(
    kb_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Export a knowledge base to one or multiple ZIP package parts."""
    kb = await get_user_knowledge_base(db, user.id, kb_id, is_admin=user.is_admin)

    docs_result = await db.execute(
        select(KnowledgeDocument)
        .where(KnowledgeDocument.knowledge_base_id == kb.id)
        .order_by(KnowledgeDocument.created_at.asc(), KnowledgeDocument.id.asc())
    )
    docs = list(docs_result.scalars().all())

    sync_result = await db.execute(
        select(KBSyncSource)
        .where(KBSyncSource.knowledge_base_id == kb.id)
        .order_by(KBSyncSource.created_at.asc(), KBSyncSource.id.asc())
    )
    sync_sources = list(sync_result.scalars().all())
    sync_payload = [
        KBExportSyncSource(
            source_type=s.source_type,
            config=s.config or {},
            sync_interval=s.sync_interval,
        )
        for s in sync_sources
    ]

    export_items: list[dict[str, object]] = []
    total_content_bytes = 0
    content_less = 0
    for idx, doc in enumerate(docs, start=1):
        # A document with NULL content has no stored text (still processing or
        # failed extraction). It cannot be represented in the package — it would
        # carry no payload member and be silently dropped on import — so fail the
        # whole export loudly (below) instead of emitting a non-roundtrippable
        # package. An empty-string content is NOT None and still round-trips.
        if doc.content is None:
            content_less += 1
            continue

        content_bytes = doc.content.encode("utf-8")
        total_content_bytes += len(content_bytes)
        if total_content_bytes > _KB_EXPORT_MAX_TOTAL_BYTES:
            raise HTTPException(
                status_code=413,
                detail=(
                    "Knowledge base is too large to export "
                    f"(> {_KB_EXPORT_MAX_TOTAL_BYTES // (1024 * 1024)} MB of "
                    "document content)."
                ),
            )
        # The exported payload is always UTF-8 text, so force a text
        # extension from the source type rather than preserving a
        # misleading binary one (e.g. report.pdf -> report.txt). The
        # original filename is still recorded in the manifest.
        content_ext = _content_extension_for_source(doc.source_type)
        fallback_name = f"document-{idx:04d}{content_ext}"
        safe_name = _sanitize_export_filename(doc.filename, fallback=fallback_name)
        base = safe_name.rsplit(".", 1)[0] if "." in safe_name else safe_name
        base = base or f"document-{idx:04d}"
        archive_path = f"documents/{idx:04d}-{base}{content_ext}"

        export_items.append(
            {
                "doc": doc,
                "archive_path": archive_path,
                "content_bytes": content_bytes,
            }
        )

    if content_less:
        raise HTTPException(
            status_code=409,
            detail=(
                f"{content_less} document(s) in this knowledge base have no "
                "stored content yet (still processing or failed extraction) and "
                "cannot be exported faithfully. Reprocess or remove them, then "
                "retry the export."
            ),
        )

    chunks = _partition_export_items(
        export_items,
        target_part_bytes=_KB_EXPORT_PART_TARGET_BYTES,
    )

    fit_chunks: list[list[dict[str, object]]] = []
    queue = list(chunks)
    while queue:
        chunk = queue.pop(0)
        preview_bytes, _ = _render_export_zip_part(
            kb=kb,
            export_id=uuid.UUID(int=0),
            items=chunk,
            part_number=1,
            total_parts=1,
            sync_sources=sync_payload,
        )
        if len(preview_bytes) <= _KB_EXPORT_PART_MAX_BYTES:
            fit_chunks.append(chunk)
            continue

        if len(chunk) <= 1:
            doc = cast(KnowledgeDocument, chunk[0]["doc"]) if chunk else None
            doc_name = doc.filename if doc is not None else "document"
            raise HTTPException(
                status_code=413,
                detail=(
                    f"Document '{doc_name}' is too large to fit in a "
                    f"{_KB_EXPORT_PART_MAX_BYTES // (1024 * 1024)} MB export part."
                ),
            )

        pivot = len(chunk) // 2
        queue.insert(0, chunk[pivot:])
        queue.insert(0, chunk[:pivot])

    export_id = uuid.uuid4()
    expires_at = datetime.now(UTC) + timedelta(hours=_KB_EXPORT_TTL_HOURS)
    total_parts = len(fit_chunks)
    slug = (slugify(kb.name) or "knowledge-base")[:_KB_EXPORT_SLUG_MAX_LEN]
    slug = slug.rstrip("-_.") or "knowledge-base"

    uploaded_objects: list[str] = []
    package_rows: list[KBExportPackage] = []
    try:
        for part_idx, chunk in enumerate(fit_chunks, start=1):
            zip_bytes, _ = _render_export_zip_part(
                kb=kb,
                export_id=export_id,
                items=chunk,
                part_number=part_idx,
                total_parts=total_parts,
                sync_sources=sync_payload,
            )

            if len(zip_bytes) > _KB_EXPORT_PART_MAX_BYTES:
                raise HTTPException(
                    status_code=413,
                    detail=(
                        "Generated export part exceeds max size of "
                        f"{_KB_EXPORT_PART_MAX_BYTES // (1024 * 1024)} MB."
                    ),
                )

            filename = (
                f"knowledge-base-{slug}.zip"
                if total_parts == 1
                else f"knowledge-base-{slug}-part-{part_idx:02d}-of-{total_parts:02d}.zip"
            )
            object_name = await file_storage.upload(
                data=zip_bytes,
                filename=filename,
                content_type="application/zip",
                prefix=f"kb-exports/{kb.id}/{export_id}",
            )
            uploaded_objects.append(object_name)

            pkg = KBExportPackage(
                knowledge_base_id=kb.id,
                user_id=user.id,
                export_id=export_id,
                part_number=part_idx,
                total_parts=total_parts,
                filename=filename,
                s3_object_name=object_name,
                file_size=len(zip_bytes),
                expires_at=expires_at,
            )
            db.add(pkg)
            await db.flush()
            package_rows.append(pkg)

        await log_kb_activity(
            db,
            kb.id,
            "export_generated",
            details=(
                f"Exported KB into {total_parts} package part(s); "
                f"expires in {_KB_EXPORT_TTL_HOURS}h"
            ),
            document_count=len(docs),
        )
        await log_audit(
            db,
            user=user,
            action="export",
            entity_type="knowledge_base",
            entity_id=kb.id,
            entity_name=kb.name,
            summary=f"Exported knowledge base '{kb.name}'",
            details={
                "export_id": str(export_id),
                "total_parts": total_parts,
                "expires_at": expires_at.isoformat(),
            },
        )
        await db.commit()
    except Exception as exc:
        await db.rollback()
        for object_name in uploaded_objects:
            await file_storage.delete(object_name)
        if isinstance(exc, HTTPException):
            raise
        logger.error("KB export generation failed: %s", format_exception_message(exc), exc_info=True)
        raise HTTPException(
            status_code=500,
            detail="Failed to generate export packages",
        ) from exc

    return KBExportResponse(
        export_id=str(export_id),
        packages=[_kb_export_package_to_response(pkg) for pkg in package_rows],
    )


@router.post(
    "/import",
    response_model=KnowledgeBaseResponse,
    status_code=status.HTTP_201_CREATED,
)
async def import_knowledge_base(
    files: list[UploadFile] = File(...),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _agentic_gate: None = Depends(require_agentic_ai),
):
    """Import a KB package ZIP (single or multipart) with zip-bomb defenses."""
    if not files:
        raise HTTPException(status_code=400, detail="No ZIP package provided")
    if len(files) > _KB_IMPORT_MAX_PARTS:
        raise HTTPException(
            status_code=400,
            detail=f"Too many package parts provided (max {_KB_IMPORT_MAX_PARTS}).",
        )

    kb_def: KBExportKnowledgeBase | None = None
    expected_export_id: str | None = None
    imported_docs: list[tuple[KBExportDocument, str]] = []
    seen_archive_paths: set[str] = set()
    sync_sources: dict[str, KBExportSyncSource] = {}
    manifest_total_parts: int | None = None
    seen_parts: set[int] = set()
    total_uncompressed = 0
    # What every member of every part really expands to, spent as it is read: the
    # declared sizes summed below are an early refusal, never the bound.
    budget = ArchiveBudget(_KB_IMPORT_MAX_TOTAL_UNCOMPRESSED_BYTES)

    for upload in files:
        # Read at most one byte over the cap so an oversized upload bounds memory
        # here too: a file larger than the cap yields cap+1 bytes, which
        # `_safe_zip_names` then rejects (its `len(raw) > cap` guard still fires).
        raw = await upload.read(_KB_IMPORT_MAX_PACKAGE_BYTES + 1)
        names = _safe_zip_names(raw)
        if "manifest.json" not in names:
            raise HTTPException(
                status_code=400,
                detail=f"ZIP '{upload.filename or 'package'}' is missing manifest.json",
            )

        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            # Aggregate decompressed-size cap across ALL parts. `_safe_zip_names`
            # enforces the limit per ZIP, but without a running total an attacker
            # could upload many parts whose combined decompressed size blows past
            # the intended bound (memory / DB write amplification).
            total_uncompressed += sum(
                info.file_size
                for info in zf.infolist()
                if not info.filename.endswith("/")
            )
            if total_uncompressed > _KB_IMPORT_MAX_TOTAL_UNCOMPRESSED_BYTES:
                raise _combined_size_error()
            manifest_raw = await asyncio.to_thread(
                _read_zip_member,
                zf,
                "manifest.json",
                error="manifest.json is missing from ZIP package",
                budget=budget,
            )
            try:
                manifest = KBExportManifest.model_validate_json(manifest_raw)
            except ValidationError as exc:
                raise HTTPException(
                    status_code=400,
                    detail=f"Invalid manifest.json: {exc.errors()[0]['msg']}",
                ) from exc

            if manifest.format != _KB_EXPORT_FORMAT:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "Unsupported package format. "
                        f"Expected '{_KB_EXPORT_FORMAT}'."
                    ),
                )
            if manifest.version != _KB_EXPORT_VERSION:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"Unsupported package version '{manifest.version}'. "
                        f"Expected '{_KB_EXPORT_VERSION}'."
                    ),
                )

            if expected_export_id is None:
                expected_export_id = manifest.export_id
            elif expected_export_id != manifest.export_id:
                raise HTTPException(
                    status_code=400,
                    detail="Provided ZIP files belong to different exports",
                )

            # Multipart consistency: every part of one export must agree on
            # total_parts, carry a unique in-range part number, and — checked
            # after the loop — together cover the full 1..total_parts range.
            # Without this, omitting or duplicating a ZIP silently produces a
            # partially-restored KB.
            if manifest_total_parts is None:
                manifest_total_parts = manifest.total_parts
            elif manifest_total_parts != manifest.total_parts:
                raise HTTPException(
                    status_code=400,
                    detail="Provided ZIP files report inconsistent total_parts",
                )
            if manifest.part < 1 or manifest.part > manifest.total_parts:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"Package part {manifest.part} is out of range "
                        f"(expected 1..{manifest.total_parts})"
                    ),
                )
            if manifest.part in seen_parts:
                raise HTTPException(
                    status_code=400,
                    detail=f"Duplicate package part {manifest.part} provided",
                )
            seen_parts.add(manifest.part)

            if kb_def is None:
                kb_def = manifest.knowledge_base
                # Same bounds as creating a knowledge base: a package must not
                # carry chunking the worker cannot finish.
                problem = chunk_params_problem(kb_def.chunk_size, kb_def.chunk_overlap)
                if problem:
                    raise HTTPException(
                        status_code=422,
                        detail=f"Invalid manifest.json: {problem}",
                    )
            elif kb_def.model_dump() != manifest.knowledge_base.model_dump():
                raise HTTPException(
                    status_code=400,
                    detail="Provided ZIP files contain inconsistent KB definitions",
                )

            for source in manifest.sync_sources:
                source_key = json.dumps(
                    {
                        "source_type": source.source_type,
                        "config": source.config,
                        "sync_interval": source.sync_interval,
                    },
                    sort_keys=True,
                )
                sync_sources[source_key] = source

            part_docs: list[KBExportDocument] = []
            for doc in manifest.documents:
                if not doc.archive_path:
                    continue
                # A valid export assigns each document a unique archive_path
                # (zero-padded global index), so the same payload path appearing
                # twice across the provided parts means a corrupted / tampered
                # package — reject it rather than silently keeping first-seen
                # (which would hide data loss).
                if doc.archive_path in seen_archive_paths:
                    raise HTTPException(
                        status_code=400,
                        detail=(
                            "Duplicate document payload "
                            f"'{doc.archive_path}' across the provided package "
                            "part(s)"
                        ),
                    )
                if doc.archive_path not in names:
                    raise HTTPException(
                        status_code=400,
                        detail=(
                            f"ZIP '{upload.filename or 'package'}' is missing "
                            f"document payload '{doc.archive_path}'"
                        ),
                    )

                seen_archive_paths.add(doc.archive_path)
                part_docs.append(doc)

            # ``part_docs`` only holds documents with an ``archive_path``.
            paths = [cast(str, doc.archive_path) for doc in part_docs]
            contents = await asyncio.to_thread(_read_document_payloads, zf, paths, budget=budget)
            imported_docs.extend(zip(part_docs, contents, strict=True))

    if kb_def is None:
        raise HTTPException(status_code=400, detail="Invalid import package")

    if manifest_total_parts is not None and len(seen_parts) != manifest_total_parts:
        missing = sorted(set(range(1, manifest_total_parts + 1)) - seen_parts)
        raise HTTPException(
            status_code=400,
            detail=(
                "Incomplete multipart import: missing part(s) "
                f"{missing} of {manifest_total_parts}. Provide every package part."
            ),
        )

    embedding_model = kb_def.embedding_model
    if not embedding_model:
        try:
            from app.llm.router import get_embedding_provider

            _, platform_embed_model, _provider_id = await get_embedding_provider(db)
            embedding_model = platform_embed_model
        except ValueError:
            embedding_model = "text-embedding-3-large"

    kb = KnowledgeBase(
        user_id=user.id,
        name=kb_def.name,
        description=kb_def.description,
        tags=kb_def.tags,
        embedding_model=embedding_model,
        chunk_size=kb_def.chunk_size,
        chunk_overlap=kb_def.chunk_overlap,
        html_ingestion=kb_def.html_ingestion,
        describe_figures=kb_def.describe_figures,
    )
    db.add(kb)
    await db.flush()

    # Insert in the stable export order (archive_path carries the zero-padded
    # 0001/0002/... global document index) rather than the order the browser
    # handed us the ZIP parts, so insertion / created_at order is deterministic
    # and matches the source KB regardless of multipart selection order.
    imported_docs.sort(key=lambda item: item[0].archive_path or "")

    imported_doc_ids: list[str] = []
    for doc_meta, content in imported_docs:
        source_type = (doc_meta.source_type or "text").strip().lower()
        if not source_type:
            source_type = "text"

        doc_row = KnowledgeDocument(
            knowledge_base_id=kb.id,
            filename=doc_meta.filename,
            source_type=source_type,
            source_url=doc_meta.source_url,
            content=content,
            content_hash=hashlib.sha256(content.encode("utf-8")).hexdigest(),
            status="pending",
            figure_reading=imported_reading(doc_meta.figure_status, doc_meta.figure_pages, content),
        )
        db.add(doc_row)
        await db.flush()
        imported_doc_ids.append(str(doc_row.id))

    kb.document_count = len(imported_doc_ids)

    for source in sync_sources.values():
        if source.sync_interval not in VALID_SYNC_INTERVALS:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Invalid sync_interval in manifest: "
                    f"'{source.sync_interval}'"
                ),
            )
        if source.source_type not in VALID_SYNC_SOURCE_TYPES:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Invalid source_type in manifest: "
                    f"'{source.source_type}'"
                ),
            )

        db.add(
            KBSyncSource(
                knowledge_base_id=kb.id,
                user_id=user.id,
                source_type=source.source_type,
                config=source.config,
                sync_interval=source.sync_interval,
                integration_id=None,
            )
        )

    await log_kb_activity(
        db,
        kb.id,
        "imported_package",
        details=(
            f"Imported KB package ({len(files)} ZIP file(s), "
            f"{len(imported_doc_ids)} document(s))"
        ),
        document_count=len(imported_doc_ids),
    )

    await db.commit()
    if imported_doc_ids:
        await enqueue_documents(imported_doc_ids)

    return await _kb_to_response(
        kb,
        db,
        can_manage=can_manage(user, kb),
        # A package-file import creates a plain KB: no skill package origin.
        is_package_origin=False,
        owner_name=user.full_name,
    )


# ---------------------------------------------------------------------------
# Documents
# ---------------------------------------------------------------------------


@router.post("/global/reprocess-stale")
async def reprocess_all_stale(
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    _ai_gate: None = Depends(require_ai_enabled),
):
    """Re-enqueue all documents in KBs that have stale embedding tuples.

    A KB is considered to have stale embeddings when stored embeddings contain
    tuples whose provider/model/dimensions differ from the current platform
    embedding target.  Only KBs visible to the calling user are included.

    For each affected KB, ALL documents are reset to ``pending`` so the worker
    re-embeds them with the current model.  Existing stale embeddings are
    deleted in this same transaction so that search results do not return
    mismatched-dimension chunks during the re-embedding window — the worker
    would otherwise only purge per-document on re-processing, leaving the
    rest of the KB queryable with stale data.
    """
    from app.api.v1.platform_settings_helpers import (
        get_platform_settings as _load_ps,
    )

    ps = await _load_ps(db)
    expected_model = ps.embedding_model
    expected_dims = ps.embedding_dimensions

    if not expected_model or expected_dims is None:
        return {
            "requeued": 0,
            "kbs_affected": 0,
            "message": "Platform embedding model/dimensions not configured — nothing to do.",
        }

    # Find all KB IDs that have at least one stale tuple
    user_group_ids = await get_user_group_ids(db, user.id)
    kb_query = user_visible_query(KnowledgeBase, user.id, user_group_ids)
    kb_rows = await db.execute(kb_query)
    visible_kbs = kb_rows.scalars().all()
    visible_kb_ids = [str(kb.id) for kb in visible_kbs]
    if not visible_kb_ids:
        return {"requeued": 0, "kbs_affected": 0, "message": "No knowledge bases found."}

    stale_kb_rows = await db.execute(
        text(
            """
            SELECT DISTINCT e.knowledge_base_id
            FROM embeddings e
            -- Ready documents only, as ``recover_stale_documents``: a refused
            -- re-embed keeps its vectors and its error.
            JOIN knowledge_documents d
              ON d.id::text = e.source_id AND d.status = 'ready'
            WHERE e.knowledge_base_id = ANY(:kb_ids)
              AND e.source_type = 'document'
              AND (
                embedding_model IS DISTINCT FROM :expected_model
                OR COALESCE(embedding_dimensions, vector_dims(embedding))
                   IS DISTINCT FROM :expected_dims
              )
            """
        ),
        {
            "kb_ids": visible_kb_ids,
            "expected_model": expected_model,
            "expected_dims": expected_dims,
        },
    )
    stale_kb_ids = [str(r[0]) for r in stale_kb_rows.all()]
    # A package-installed KB refuses a re-embed (its reprocess routes do too):
    # it is skipped, and the answer says so rather than calling it
    # up to date.
    from_packages = await package_origin_knowledge_base_ids(
        db, [uuid.UUID(kid) for kid in stale_kb_ids]
    )
    skipped = sum(1 for kb_id in stale_kb_ids if kb_id in from_packages)
    stale_kb_ids = [kb_id for kb_id in stale_kb_ids if kb_id not in from_packages]
    skipped_note = (
        f" {skipped} knowledge base(s) installed by a skill package skipped: "
        "a manual re-embed does not apply to them."
        if skipped
        else ""
    )

    if not stale_kb_ids:
        return {
            "requeued": 0,
            "kbs_affected": 0,
            "kbs_skipped": skipped,
            "message": (
                f"No knowledge base to re-embed.{skipped_note}"
                if skipped
                else "All knowledge bases are up to date."
            ),
        }

    # Reset all documents in stale KBs to pending
    doc_rows = await db.execute(
        select(KnowledgeDocument).where(
            KnowledgeDocument.knowledge_base_id.in_(stale_kb_ids)
        )
    )
    docs = doc_rows.scalars().all()
    for doc in docs:
        doc.status = "pending"
        doc.error_message = None
        # Manual reprocess gives the doc a fresh automatic-retry budget.
        doc.embedding_retry_count = 0
        doc.embedding_next_retry_at = None

    # The stale vectors stay searchable until the processor swaps them; a
    # re-embed the dimension guard refuses leaves them in place.
    await db.commit()

    doc_ids = [str(d.id) for d in docs]
    if doc_ids:
        await enqueue_reembedding(doc_ids)

    return {
        "requeued": len(doc_ids),
        "kbs_affected": len(stale_kb_ids),
        "kbs_skipped": skipped,
        "message": (
            f"{len(doc_ids)} document(s) across {len(stale_kb_ids)} knowledge base(s) "
            f"re-queued for re-embedding with the current model.{skipped_note}"
        ),
    }


def _global_documents_order(sort: str, direction: str) -> list[ColumnElement[Any]]:
    """The ORDER BY of the documents page: the chosen column, then newest first and the id."""
    column = {
        "filename": func.lower(KnowledgeDocument.filename),
        "knowledge_base": func.lower(KnowledgeBase.name),
        "status": KnowledgeDocument.status,
    }.get(sort, KnowledgeDocument.created_at)
    primary = column.asc() if direction == "asc" else column.desc()
    if sort not in {"filename", "knowledge_base", "status"}:
        return [primary, KnowledgeDocument.id.desc()]
    return [primary, KnowledgeDocument.created_at.desc(), KnowledgeDocument.id.desc()]


@router.get("/global/documents", response_model=GlobalKnowledgeDocumentsResponse)
async def list_global_documents(
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    status: str | None = Query(
        None,
        description="Comma-separated statuses: ready,pending,processing,error",
    ),
    kb_name: str | None = Query(None, description="Case-insensitive KB name filter"),
    kb_ids: str | None = Query(None, description="Comma-separated KB UUID list"),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    q: Annotated[
        str | None,
        Query(max_length=200, description="Words the file name or the knowledge base name holds"),
    ] = None,
    sort: Annotated[
        Literal["created_at", "filename", "knowledge_base", "status"],
        Query(description="Order by creation date, file name, knowledge base name or status"),
    ] = "created_at",
    direction: Annotated[
        Literal["asc", "desc"], Query(description="Order direction; newest first breaks ties")
    ] = "desc",
):
    """Every document of the knowledge bases the caller sees, one page at a time.

    The documents page's data table searches (``q``: file name or knowledge base
    name), filters (``status``, ``kb_ids``), sorts (``sort``, ``direction``) and
    pages through this route; ``kb_name`` stays for older links.
    """
    user_group_ids = await get_user_group_ids(db, user.id)
    where_clauses = [
        user_visible_filter(
            KnowledgeBase,
            user.id,
            user_group_ids,
            is_admin=user.is_admin,
        )
    ]

    if kb_name:
        escaped = kb_name.replace("%", r"\%").replace("_", r"\_")
        where_clauses.append(KnowledgeBase.name.ilike(f"%{escaped}%"))

    words = (q or "").strip()
    if words:
        pattern = "%" + words.replace("%", r"\%").replace("_", r"\_") + "%"
        where_clauses.append(
            or_(KnowledgeDocument.filename.ilike(pattern), KnowledgeBase.name.ilike(pattern))
        )

    if kb_ids:
        parsed_kb_ids: list[uuid.UUID] = []
        for raw in kb_ids.split(","):
            value = raw.strip()
            if not value:
                continue
            try:
                parsed_kb_ids.append(uuid.UUID(value))
            except ValueError as exc:
                raise HTTPException(
                    status_code=400,
                    detail=f"Invalid kb_ids UUID: {value}",
                ) from exc
        if parsed_kb_ids:
            where_clauses.append(KnowledgeBase.id.in_(parsed_kb_ids))

    if status:
        allowed = {"ready", "pending", "processing", "error"}
        statuses = [s.strip().lower() for s in status.split(",") if s.strip()]
        invalid = [s for s in statuses if s not in allowed]
        if invalid:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid status value(s): {', '.join(invalid)}",
            )
        if statuses:
            where_clauses.append(KnowledgeDocument.status.in_(statuses))

    count_query = (
        select(func.count())
        .select_from(KnowledgeDocument)
        .join(KnowledgeBase, KnowledgeDocument.knowledge_base_id == KnowledgeBase.id)
        .where(*where_clauses)
    )
    total = int((await db.execute(count_query)).scalar_one() or 0)

    docs_query = (
        select(KnowledgeDocument, KnowledgeBase.name)
        .join(KnowledgeBase, KnowledgeDocument.knowledge_base_id == KnowledgeBase.id)
        .where(*where_clauses)
        .order_by(*_global_documents_order(sort, direction))
        .offset(offset)
        .limit(limit)
    )
    rows = (await db.execute(docs_query)).all()
    from_packages = await package_origin_knowledge_base_ids(
        db, list({doc.knowledge_base_id for doc, _ in rows})
    )
    items = [
        GlobalKnowledgeDocumentResponse(
            id=str(doc.id),
            knowledge_base_id=str(doc.knowledge_base_id),
            knowledge_base_name=kb_name_row,
            knowledge_base_is_package_origin=str(doc.knowledge_base_id) in from_packages,
            filename=doc.filename,
            source_type=doc.source_type,
            source_url=doc.source_url,
            status=doc.status,
            chunk_count=doc.chunk_count,
            error_message=public_error_message(doc.error_message),
            error_code=public_error_code(doc.error_message),
            created_at=doc.created_at.isoformat(),
        )
        for doc, kb_name_row in rows
    ]

    return GlobalKnowledgeDocumentsResponse(
        items=items,
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/{kb_id}/documents", response_model=list[KnowledgeDocumentResponse])
async def list_documents(
    kb_id: str,
    limit: int = 50,
    offset: int = 0,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    kb = await get_user_knowledge_base(db, user.id, kb_id, is_admin=user.is_admin)
    query = (
        select(KnowledgeDocument)
        .where(KnowledgeDocument.knowledge_base_id == kb_id)
        .order_by(KnowledgeDocument.created_at.desc())
        .offset(offset)
        .limit(min(limit, 500))
    )
    result = await db.execute(query)
    return [_doc_to_response(d, kb) for d in result.scalars().all()]


@router.post(
    "/{kb_id}/documents/text",
    response_model=KnowledgeDocumentResponse,
    status_code=status.HTTP_201_CREATED,
)
async def add_text_document(
    kb_id: str,
    body: AddTextDocumentRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _ai_gate: None = Depends(require_ai_enabled),
):
    """Add a text or markdown document to the knowledge base."""
    kb = await get_user_knowledge_base(db, user.id, kb_id, is_admin=user.is_admin)
    await assert_writable_knowledge_base(
        db, kb, user, action="add documents to", content_action="add documents to it"
    )
    try:
        doc = await kb_documents.add_text_document(
            db,
            kb,
            filename=body.filename,
            content=body.content,
            source_type=body.source_type,
        )
    except kb_documents.DocumentRefusedError as exc:
        raise HTTPException(status_code=400, detail=exc.detail) from None

    # Enqueue background processing (chunk + embed) once the row is committed
    doc_id = str(doc.id)
    await db.commit()
    await enqueue_document_processing(doc_id)

    return _doc_to_response(doc, kb)


@router.post(
    "/{kb_id}/documents/upload",
    response_model=KnowledgeDocumentResponse,
    status_code=status.HTTP_201_CREATED,
)
async def upload_document(
    kb_id: str,
    file: UploadFile = File(...),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _ai_gate: None = Depends(require_ai_enabled),
):
    """Upload a file to the knowledge base. Supports .txt, .md, .csv, .json, .pdf, .docx, .xls, .xlsx."""
    kb = await get_user_knowledge_base(db, user.id, kb_id, is_admin=user.is_admin)
    await assert_writable_knowledge_base(
        db,
        kb,
        user,
        action="upload documents to",
        content_action="upload documents to it",
    )

    doc = await kb_documents.add_file_document(
        db, kb, filename=file.filename or "uploaded_file", data=await file.read()
    )

    # Enqueue background processing once the row is committed
    doc_id = str(doc.id)
    await db.commit()
    await enqueue_document_processing(doc_id)

    return _doc_to_response(doc, kb)


# ---------------------------------------------------------------------------
# External source sync endpoints
# ---------------------------------------------------------------------------

# Sync source CRUD schemas
VALID_SYNC_INTERVALS = {"manual", "6h", "12h", "daily", "weekly"}

# Allowed KB sync source types — single source of truth shared by the
# create-sync-source endpoint and the import-package validation so the two
# can't drift as new source types are added.
VALID_SYNC_SOURCE_TYPES = {
    "notion",
    "microsoft-drive",
    "google-drive",
    "github",
    "github_public",
    "website",
    "drata",
}


@router.post(
    "/{kb_id}/sync/notion",
    response_model=SyncResponse,
    status_code=status.HTTP_201_CREATED,
)
async def sync_from_notion(
    kb_id: str,
    body: SyncNotionRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _ai_gate: None = Depends(require_ai_enabled),
):
    """Import pages from Notion into the knowledge base."""
    from app.services.kb_sync import (
        NotionSyncTotalFailureError,
        sync_egress_route,
        sync_notion_pages,
    )

    kb = await get_user_knowledge_base(db, user.id, kb_id, is_admin=user.is_admin)
    await assert_writable_knowledge_base(
        db,
        kb,
        user,
        action="sync content into",
        content_action="import content into it",
    )

    # The context manager wraps the try/except (not just the sync call) so
    # the HTTP-error warning below also carries the structured KB
    # identifiers — an except handler runs after an inner ``with`` unwinds.
    with (
        kb_sync_log_context(kb_id=kb.id, kb_name=kb.name, source_type="notion"),
        egress_route(await sync_egress_route(db, user.id, "notion")),
    ):
        try:
            doc_ids = await sync_notion_pages(
                db,
                kb,
                user.id,
                page_ids=body.page_ids,
                search_query=body.search_query,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except httpx.HTTPStatusError as exc:
            logger.warning(
                "Notion sync HTTP error for KB '%s' (%s): %s",
                kb.name,
                kb.id,
                format_exception_message(exc),
            )
            raise HTTPException(
                status_code=502,
                detail=f"Notion API error: {exc.response.status_code} — {exc.response.text[:200]}",
            ) from exc
        except NotionSyncTotalFailureError as exc:
            # Raised by sync_notion_pages when EVERY queued page failed —
            # surface the aggregated per-page errors as a
            # clean upstream-failure response instead of a bare 500.
            logger.warning(
                "Notion sync total failure for KB '%s' (%s): %s",
                kb.name,
                kb.id,
                format_exception_message(exc),
            )
            raise HTTPException(status_code=502, detail=str(exc)[:500]) from exc
    return SyncResponse(documents_synced=len(doc_ids), document_ids=doc_ids)


@router.post(
    "/{kb_id}/sync/microsoft-drive",
    response_model=SyncResponse,
    status_code=status.HTTP_201_CREATED,
)
async def sync_from_microsoft_drive(
    kb_id: str,
    body: SyncDriveRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _ai_gate: None = Depends(require_ai_enabled),
):
    """Import files from OneDrive or SharePoint into the knowledge base."""
    from app.services.kb_sync import sync_egress_route, sync_microsoft_drive

    kb = await get_user_knowledge_base(db, user.id, kb_id, is_admin=user.is_admin)
    await assert_writable_knowledge_base(
        db,
        kb,
        user,
        action="sync content into",
        content_action="import content into it",
    )

    # Context manager outside the try/except so the HTTP-error warning also
    # carries the structured KB identifiers (see the Notion endpoint above).
    with (
        kb_sync_log_context(kb_id=kb.id, kb_name=kb.name, source_type="microsoft-drive"),
        egress_route(await sync_egress_route(db, user.id, "microsoft")),
    ):
        try:
            doc_ids = await sync_microsoft_drive(
                db,
                kb,
                user.id,
                folder_path=body.folder_path,
                site_id=body.site_id,
                recursive=body.recursive,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except httpx.HTTPStatusError as exc:
            logger.warning(
                "Microsoft Drive sync HTTP error for KB '%s' (%s): %s",
                kb.name,
                kb.id,
                format_exception_message(exc),
            )
            raise HTTPException(
                status_code=502,
                detail=f"Microsoft API error: {exc.response.status_code} — {exc.response.text[:200]}",
            ) from exc
    return SyncResponse(documents_synced=len(doc_ids), document_ids=doc_ids)


@router.post(
    "/{kb_id}/sync/google-drive",
    response_model=SyncResponse,
    status_code=status.HTTP_201_CREATED,
)
async def sync_from_google_drive(
    kb_id: str,
    body: SyncGoogleDriveRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _ai_gate: None = Depends(require_ai_enabled),
):
    """Import files from Google Drive into the knowledge base."""
    from app.services.kb_sync import sync_egress_route, sync_google_drive

    kb = await get_user_knowledge_base(db, user.id, kb_id, is_admin=user.is_admin)
    await assert_writable_knowledge_base(
        db,
        kb,
        user,
        action="sync content into",
        content_action="import content into it",
    )

    # Context manager outside the try/except so the HTTP-error warning also
    # carries the structured KB identifiers (see the Notion endpoint above).
    with (
        kb_sync_log_context(kb_id=kb.id, kb_name=kb.name, source_type="google-drive"),
        egress_route(await sync_egress_route(db, user.id, "google")),
    ):
        try:
            doc_ids = await sync_google_drive(
                db, kb, user.id, folder_id=body.folder_id
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except httpx.HTTPStatusError as exc:
            logger.warning(
                "Google Drive sync HTTP error for KB '%s' (%s): %s",
                kb.name,
                kb.id,
                format_exception_message(exc),
            )
            raise HTTPException(
                status_code=502,
                detail=f"Google API error: {exc.response.status_code} — {exc.response.text[:200]}",
            ) from exc
    return SyncResponse(documents_synced=len(doc_ids), document_ids=doc_ids)


@router.post(
    "/{kb_id}/sync/github",
    response_model=SyncResponse,
    status_code=status.HTTP_201_CREATED,
)
async def sync_from_github(
    kb_id: str,
    body: SyncGitHubRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _ai_gate: None = Depends(require_ai_enabled),
):
    """Import files from a GitHub repository into the knowledge base."""
    from app.services.kb_sync import sync_egress_route, sync_github_repo

    kb = await get_user_knowledge_base(db, user.id, kb_id, is_admin=user.is_admin)
    await assert_writable_knowledge_base(
        db,
        kb,
        user,
        action="sync content into",
        content_action="import content into it",
    )

    # Context manager outside the try/except so the HTTP-error warning also
    # carries the structured KB identifiers (see the Notion endpoint above).
    with (
        kb_sync_log_context(kb_id=kb.id, kb_name=kb.name, source_type="github"),
        egress_route(await sync_egress_route(db, user.id, "github")),
    ):
        try:
            doc_ids = await sync_github_repo(
                db,
                kb,
                user.id,
                repo=body.repo,
                branch=body.branch,
                path=body.path,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except httpx.HTTPStatusError as exc:
            logger.warning(
                "GitHub sync HTTP error for KB '%s' (%s): %s",
                kb.name,
                kb.id,
                format_exception_message(exc),
            )
            detail = f"GitHub API error: {exc.response.status_code}"
            if exc.response.status_code == 404:
                detail = f"Repository '{body.repo}' not found (branch: {body.branch}). Check the repo name, branch, and that your GitHub integration has access."
            elif exc.response.status_code == 403:
                detail = "GitHub API rate limit exceeded or insufficient permissions. Check your GitHub integration token."
            raise HTTPException(status_code=502, detail=detail) from exc
    return SyncResponse(documents_synced=len(doc_ids), document_ids=doc_ids)


@router.get("/{kb_id}/documents/{doc_id}/download")
async def download_document(
    kb_id: str,
    doc_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Download the stored content of a document as a file."""
    from fastapi.responses import Response

    await get_user_knowledge_base(db, user.id, kb_id, is_admin=user.is_admin)
    doc = await _get_kb_document_or_404(db, kb_id, doc_id)
    if not doc.content:
        raise HTTPException(status_code=404, detail="Document has no content")

    content_type_map = {
        "markdown": "text/markdown",
        "text": "text/plain",
        "json": "application/json",
        "csv": "text/csv",
        "pdf": "text/plain",
        "docx": "text/plain",
        "xls": "text/plain",
        "xlsx": "text/plain",
        "url": "text/markdown",
    }
    media_type = content_type_map.get(doc.source_type, "text/plain")
    return Response(
        content=doc.content.encode("utf-8"),
        media_type=media_type,
        headers={
            "Content-Disposition": build_content_disposition(doc.filename),
        },
    )


@router.delete("/{kb_id}/documents/{doc_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_document(
    kb_id: str,
    doc_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _ai_gate: None = Depends(require_ai_enabled),
):
    kb = await get_user_knowledge_base(db, user.id, kb_id, is_admin=user.is_admin)
    await assert_writable_knowledge_base(
        db,
        kb,
        user,
        action="delete documents from",
        content_action="delete documents from it",
    )

    doc = await _get_kb_document_or_404(db, str(kb.id), doc_id)
    await log_kb_activity(
        db,
        kb.id,
        "document_deleted",
        details=f"Deleted document '{doc.filename}'",
        source_type=doc.source_type,
    )
    cancel_document_jobs_after_commit(db, [doc.id])
    # Also delete the document's embedding chunks: they are keyed by
    # ``source_id == str(doc.id)`` (a plain string, not a FK — only
    # ``knowledge_base_id`` cascades), so deleting the document row alone would
    # orphan them and they would keep surfacing in RAG / hybrid search. Keep
    # the KB's denormalised ``chunk_count`` in sync.
    doc_chunks = doc.chunk_count or 0
    await db.execute(
        delete(Embedding).where(
            Embedding.knowledge_base_id == kb.id,
            Embedding.source_type == "document",
            Embedding.source_id == str(doc.id),
        )
    )
    await db.delete(doc)
    kb.document_count = max(0, (kb.document_count or 0) - 1)
    await bump_kb_revision(db, kb.id)
    # Decrement whenever the document had chunks. ``chunk_count`` is expected to
    # be non-null but may be NULL/desynced on legacy or inconsistent rows (hence
    # the ``(kb.chunk_count or 0)`` guard below); a falsy-``kb.chunk_count``
    # guard here would skip a legitimate, ``max(0, …)``-floored decrement and
    # leave the counter wrong.
    if doc_chunks:
        kb.chunk_count = max(0, (kb.chunk_count or 0) - doc_chunks)
    # Commit before answering: the get_db teardown only commits after the response is sent.
    await db.commit()


@router.post("/global/test-search", response_model=GlobalTestSearchResponse)
async def global_test_search(
    body: GlobalTestSearchRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _ai_gate: None = Depends(require_ai_enabled),
):
    """Test semantic search against currently visible KBs, optionally filtered by IDs."""
    user_group_ids = await get_user_group_ids(db, user.id)
    kb_query = select(KnowledgeBase.id, KnowledgeBase.name).where(
        user_visible_filter(
            KnowledgeBase,
            user.id,
            user_group_ids,
            is_admin=user.is_admin,
        )
    )

    parsed_kb_ids: list[uuid.UUID] = []
    if body.kb_ids:
        for raw in body.kb_ids:
            value = raw.strip()
            if not value:
                continue
            try:
                parsed_kb_ids.append(uuid.UUID(value))
            except ValueError as exc:
                raise HTTPException(
                    status_code=400,
                    detail=f"Invalid kb_ids UUID: {value}",
                ) from exc
        if parsed_kb_ids:
            kb_query = kb_query.where(KnowledgeBase.id.in_(parsed_kb_ids))

    rows = (await db.execute(kb_query)).all()
    if not rows:
        return GlobalTestSearchResponse(query=body.query, results=[], total=0)

    kb_ids = [str(kb_id) for kb_id, _name in rows]
    kb_names = {str(kb_id): name for kb_id, name in rows}

    from app.llm.router import get_embedding_provider
    from app.services.vector import search_knowledge_bases

    try:
        provider, platform_model, _provider_id = await get_embedding_provider(db)
        query_embedding = await provider.embed(body.query, model=platform_model)
    except (ValueError, NotImplementedError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # Reuse the canonical KB search helper so dimension/model filtering
    # stays in sync with regular RAG queries (vector.search_knowledge_bases
    # already supports expected_dimensions / expected_model).
    embeddings = await search_knowledge_bases(
        db,
        user_id=str(user.id),
        knowledge_base_ids=kb_ids,
        query_embedding=query_embedding,
        top_k=min(body.top_k, 20),
        expected_dimensions=len(query_embedding) or None,
        expected_model=platform_model,
    )

    query_vec = np.array(query_embedding)
    query_norm = np.linalg.norm(query_vec)

    results: list[GlobalTestSearchResultItem] = []
    for emb in embeddings:
        emb_vec = np.array(emb.embedding)
        emb_norm = np.linalg.norm(emb_vec)
        similarity = (
            float(np.dot(query_vec, emb_vec) / (query_norm * emb_norm))
            if query_norm > 0 and emb_norm > 0
            else 0.0
        )
        score = round(max(0.0, min(1.0, similarity)), 4)
        kb_id = str(emb.knowledge_base_id) if emb.knowledge_base_id else ""
        results.append(
            GlobalTestSearchResultItem(
                knowledge_base_id=kb_id,
                knowledge_base_name=kb_names.get(kb_id, "Unknown"),
                content=emb.content,
                source_type=emb.source_type,
                source_id=emb.source_id,
                score=score,
                created_at=emb.created_at.isoformat(),
            )
        )

    return GlobalTestSearchResponse(
        query=body.query,
        results=results,
        total=len(results),
    )


@router.post("/{kb_id}/test-search", response_model=TestSearchResponse)
async def test_search(
    kb_id: str,
    body: TestSearchRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _ai_gate: None = Depends(require_ai_enabled),
):
    """Test RAG search against this knowledge base.

    Embeds the query, runs a semantic search on the KB's embeddings,
    and returns the top-K results with similarity scores.
    """
    kb = await get_user_knowledge_base(db, user.id, kb_id, is_admin=user.is_admin)

    if kb.chunk_count == 0:
        return TestSearchResponse(query=body.query, results=[], total=0)

    # Generate query embedding
    from app.llm.router import get_embedding_provider

    try:
        provider, platform_model, _provider_id = await get_embedding_provider(db)
        query_embedding = await provider.embed(body.query, model=platform_model)
    except (ValueError, NotImplementedError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # Search this KB
    from app.services.vector import search_knowledge_bases

    embeddings = await search_knowledge_bases(
        db,
        user_id=str(user.id),
        knowledge_base_ids=[str(kb.id)],
        query_embedding=query_embedding,
        top_k=min(body.top_k, 20),
    )

    # Compute cosine similarity scores
    query_vec = np.array(query_embedding)
    query_norm = np.linalg.norm(query_vec)

    results = []
    for emb in embeddings:
        emb_vec = np.array(emb.embedding)
        emb_norm = np.linalg.norm(emb_vec)
        score = (
            float(np.dot(query_vec, emb_vec) / (query_norm * emb_norm))
            if query_norm > 0 and emb_norm > 0
            else 0.0
        )
        results.append(
            TestSearchResultItem(
                content=emb.content,
                source_type=emb.source_type,
                source_id=emb.source_id,
                score=round(score, 4),
                created_at=emb.created_at.isoformat(),
            )
        )

    return TestSearchResponse(query=body.query, results=results, total=len(results))


@router.post("/{kb_id}/documents/reprocess")
async def reprocess_documents(
    kb_id: str,
    include_ready: bool = Query(
        default=False,
        description="When true, also re-enqueue ready documents for full re-embedding.",
    ),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _ai_gate: None = Depends(require_ai_enabled),
):
    """Re-enqueue KB documents for embedding.

    By default, only non-ready documents (pending, processing, error) are
    requeued. When ``include_ready=true``, ready documents are also requeued
    for a full re-embedding pass.
    """
    kb = await get_user_knowledge_base(db, user.id, kb_id, is_admin=user.is_admin)
    await assert_knowledge_base_not_package_origin(db, kb.id)

    target_statuses = ["pending", "processing", "error"]
    if include_ready:
        target_statuses.append("ready")

    count_result = await db.execute(
        select(func.count())
        .select_from(KnowledgeDocument)
        .where(
            KnowledgeDocument.knowledge_base_id == kb.id,
            KnowledgeDocument.status.in_(target_statuses),
        )
    )
    total = count_result.scalar() or 0
    if not total:
        return {
            "requeued": 0,
            "message": (
                "No documents to re-embed"
                if include_ready
                else "No documents to reprocess"
            ),
        }

    batch_size = 500
    docs: list = []
    offset = 0
    while True:
        result = await db.execute(
            select(KnowledgeDocument)
            .where(
                KnowledgeDocument.knowledge_base_id == kb.id,
                KnowledgeDocument.status.in_(target_statuses),
            )
            .limit(batch_size)
            .offset(offset)
        )
        batch = result.scalars().all()
        if not batch:
            break
        for doc in batch:
            doc.status = "pending"
            doc.error_message = None
            # Manual reprocess gives the doc a fresh automatic-retry budget.
            doc.embedding_retry_count = 0
            doc.embedding_next_retry_at = None
        docs.extend(batch)
        if len(batch) < batch_size:
            break
        offset += batch_size

    await log_kb_activity(
        db,
        kb.id,
        "reprocess_triggered",
        details=(
            f"Re-enqueued {len(docs)} document(s) for full re-embedding"
            if include_ready
            else f"Re-enqueued {len(docs)} document(s) for embedding"
        ),
        document_count=len(docs),
    )
    await db.commit()

    # Now enqueue – documents are committed and visible to worker
    doc_ids = [str(d.id) for d in docs]
    await enqueue_reembedding(doc_ids)

    return {
        "requeued": len(doc_ids),
        "message": f"{len(doc_ids)} documents re-enqueued for processing",
    }


@router.post("/{kb_id}/documents/{doc_id}/reprocess")
async def reprocess_document(
    kb_id: str,
    doc_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _ai_gate: None = Depends(require_ai_enabled),
):
    """Re-enqueue a single document for embedding.

    Resets the document status to ``pending``, clears any error message, and
    enqueues it for re-processing; its embeddings stay until replaced.
    """
    kb = await get_user_knowledge_base(db, user.id, kb_id, is_admin=user.is_admin)
    await assert_knowledge_base_not_package_origin(db, kb.id)

    doc = await _get_kb_document_or_404(db, str(kb.id), doc_id)

    doc.status = "pending"
    doc.error_message = None
    # Manual reprocess gives the doc a fresh automatic-retry budget.
    doc.embedding_retry_count = 0
    doc.embedding_next_retry_at = None
    await db.commit()

    await enqueue_reembedding([str(doc.id)])

    await log_kb_activity(
        db,
        kb.id,
        "reprocess_triggered",
        details=f"Re-enqueued document '{doc.filename}' for embedding",
        document_count=1,
    )
    await db.commit()

    return {"requeued": 1, "message": f"Document '{doc.filename}' re-enqueued for processing"}


@router.get("/{kb_id}/embedding-stats")
async def get_embedding_dimension_stats(
    kb_id: str,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """List stored embedding stats grouped by provider+model+dimension."""
    kb = await get_user_knowledge_base(db, user.id, kb_id, is_admin=True)

    result = await db.execute(
        text(
            """
            SELECT
              embedding_provider,
              embedding_model,
              vector_dims(embedding) AS dimension,
              COUNT(*) AS chunk_count
            FROM embeddings
            WHERE knowledge_base_id = :kb_id
            GROUP BY 1, 2, 3
            ORDER BY 1 NULLS LAST, 2 NULLS LAST, 3
            """
        ),
        {"kb_id": kb.id},
    )
    rows = result.mappings().all()
    return [
        {
            "provider": r.get("embedding_provider"),
            "model": r.get("embedding_model"),
            "dimension": int(r["dimension"]),
            "chunk_count": int(r["chunk_count"]),
        }
        for r in rows
    ]


@router.delete("/{kb_id}/embeddings/dimension/{dimension}")
async def delete_embeddings_by_dimension(
    kb_id: str,
    dimension: int,
    request: Request,
    provider: str | None = Query(default=None),
    model: str | None = Query(default=None),
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    _ai_gate: None = Depends(require_ai_enabled),
):
    """Delete KB embeddings for a dimension (optionally scoped by provider/model)."""
    if dimension <= 0:
        raise HTTPException(status_code=400, detail="Dimension must be positive")

    kb = await get_user_knowledge_base(db, user.id, kb_id, is_admin=True)
    await assert_knowledge_base_not_package_origin(db, kb.id)
    await assert_knowledge_base_not_workspace_managed(db, kb)

    where_clauses = [
        Embedding.knowledge_base_id == kb.id,
        Embedding.source_type == "document",
        Embedding.source_id.is_not(None),
        func.vector_dims(Embedding.embedding) == dimension,
    ]
    if provider is not None:
        where_clauses.append(Embedding.embedding_provider == provider)
    if model is not None:
        where_clauses.append(Embedding.embedding_model == model)

    affected_doc_rows = await db.execute(
        select(Embedding.source_id).where(*where_clauses).distinct()
    )
    affected_doc_ids_raw = [r[0] for r in affected_doc_rows.all()]

    affected_doc_ids: list[uuid.UUID] = []
    enqueue_ids: list[str] = []
    for doc_id in affected_doc_ids_raw:
        if parsed := safe_uuid(str(doc_id) if doc_id is not None else None):
            affected_doc_ids.append(parsed)
            enqueue_ids.append(str(parsed))

    deleted_result = await db.execute(delete(Embedding).where(*where_clauses))
    deleted_chunks = int(getattr(deleted_result, "rowcount", 0) or 0)

    if affected_doc_ids:
        await db.execute(
            update(KnowledgeDocument)
            .where(KnowledgeDocument.id.in_(affected_doc_ids))
            .values(
                status="pending",
                error_message=None,
                chunk_count=0,
                embedding_retry_count=0,
                embedding_next_retry_at=None,
            )
        )

    if deleted_chunks > 0 and kb.chunk_count:
        kb.chunk_count = max(0, int(kb.chunk_count) - deleted_chunks)

    await log_kb_activity(
        db,
        kb.id,
        "embedding_dimension_deleted",
        details=(
            f"Deleted {deleted_chunks} embedding chunk(s) at {dimension} dimensions"
            + (f" for provider={provider}" if provider is not None else "")
            + (f" model={model}" if model is not None else "")
            + "; "
            f"re-queued {len(enqueue_ids)} document(s)"
        ),
        document_count=len(enqueue_ids),
    )

    await db.commit()

    if enqueue_ids:
        await enqueue_reembedding(enqueue_ids)

    await log_audit(
        db,
        user=user,
        action="delete",
        entity_type="knowledge_base",
        entity_id=kb.id,
        entity_name=kb.name,
        summary=f"Deleted {dimension}-dim embeddings for knowledge base '{kb.name}'",
        details={
            "dimension": dimension,
            "provider": provider,
            "model": model,
            "deleted_chunks": deleted_chunks,
            "requeued_documents": len(enqueue_ids),
        },
        request=request,
    )
    # Commit before answering: the get_db teardown only commits after the response is sent.
    await db.commit()

    return {
        "deleted_chunks": deleted_chunks,
        "requeued_documents": len(enqueue_ids),
        "dimension": dimension,
        "provider": provider,
        "model": model,
    }


# ---------------------------------------------------------------------------
# Activity Logs
# ---------------------------------------------------------------------------


def _served_valued_sentence(
    reason: ColumnElement[str], pattern: str, template: str, integer_fields: frozenset[str]
) -> tuple[ColumnElement[list[str]], ColumnElement[str]]:
    """``(captured groups, sentence as served)`` of a valued sentence, in SQL.

    The sentence is rebuilt from the groups through the writer's own template,
    as ``public_document_error`` rebuilds it, so the stored suffix of a
    legacy row is never part of what the search reads.
    """
    groups = func.regexp_match(reason, pattern, type_=postgresql.ARRAY(SAText))
    fields: list[str] = []
    parts: list[object] = []
    for literal_text, field, _spec, _conversion in string.Formatter().parse(template):
        if literal_text:
            parts.append(literal_text)
        if field is None:
            continue
        if field not in fields:
            fields.append(field)
        value = groups[fields.index(field) + 1]
        parts.append(sa_cast(sa_cast(value, Integer), SAText) if field in integer_fields else value)
    return groups, func.concat(*parts, type_=SAText)


def _reader_served_details(served: ColumnElement[str]) -> ColumnElement[str]:
    """What a reader is served for an entry's details (``served_sync_text``), in SQL.

    *served* is what a manager is served. A sync entry whose details may carry
    a sync error is served to a reader as its fixed sentence, picked by the
    same rules, so its stored text is never part of what a reader's search
    reads.
    """
    event, details = KBActivityLog.event, KBActivityLog.details
    has_text = func.coalesce(details, "") != ""
    whens: list[tuple[ColumnElement[bool], ColumnElement[str]]] = []
    for sync_event, rule in ACTIVITY_READER_RULES.items():
        entry = and_(event == sync_event, has_text)
        for prefix, code in rule.prefixes:
            whens.append(
                (
                    and_(entry, details.startswith(prefix, autoescape=True)),
                    literal(READER_SENTENCES[code], type_=SAText),
                )
            )
        whens.append((entry, literal(READER_SENTENCES[rule.default], type_=SAText)))
    unlisted = and_(
        event.startswith(SYNC_EVENT_PREFIX, autoescape=True),
        event.not_in(sorted(READER_VISIBLE_SYNC_EVENTS)),
        has_text,
    )
    whens.append(
        (unlisted, literal(READER_SENTENCES[UNLISTED_SYNC_EVENT_RULE.default], type_=SAText))
    )
    return case(*whens, else_=served)


def _activity_search_filter(search: str, *, can_manage: bool) -> ColumnElement[bool]:
    """The ``search`` filter of the activity list and of its count.

    Matches the event and the details AS SERVED to the caller
    (``served_sync_text``): an older ``embedding_failed`` entry
    may hold raw exception text that is never served, and matching the stored
    column would let the count answer whether a hidden term is in it. So an
    embedding failure is searched as "Failed to embed '<filename>': " followed
    by the sentence it is served with: its stored reason when that is a
    platform sentence, a valued sentence rebuilt from its parsed values, else
    the generic one. A caller who cannot manage the knowledge base is served a
    sync error entry as a fixed sentence, and searched the same way
    (``_reader_served_details``).
    """
    escaped_search = search.replace("%", r"\%").replace("_", r"\_")
    pattern = f"%{escaped_search}%"
    details = KBActivityLog.details
    rest = func.substr(details, len(EMBEDDING_FAILED_PREFIX) + 1, type_=SAText)
    separator_at = func.strpos(rest, EMBEDDING_FAILED_SEPARATOR, type_=Integer)
    in_processor_shape = and_(
        details.startswith(EMBEDDING_FAILED_PREFIX, autoescape=True),
        separator_at > 0,
    )
    reason = func.substr(rest, separator_at + len(EMBEDDING_FAILED_SEPARATOR), type_=SAText)
    # "Failed to embed '<filename>': " - everything before the reason.
    served_head = func.substr(
        details,
        1,
        separator_at + len(EMBEDDING_FAILED_PREFIX) + len(EMBEDDING_FAILED_SEPARATOR) - 1,
        type_=SAText,
    )
    valued = []
    for sql_pattern, template, integer_fields in VALUED_SENTENCES_FOR_SQL:
        groups, sentence = _served_valued_sentence(reason, sql_pattern, template, integer_fields)
        valued.append(
            (and_(in_processor_shape, groups.is_not(None)), func.concat(served_head, sentence, type_=SAText))
        )
    searchable = case(
        (
            or_(
                KBActivityLog.event != EMBEDDING_FAILED_EVENT,
                func.coalesce(details, "") == "",
            ),
            details,
        ),
        (and_(in_processor_shape, reason.in_(sorted(PLATFORM_SENTENCES))), details),
        *valued,
        (in_processor_shape, func.concat(served_head, INTERNAL_ERROR_MESSAGE, type_=SAText)),
        else_=literal(UNSHAPED_EMBEDDING_FAILED_DETAILS, type_=SAText),
    )
    if not can_manage:
        searchable = _reader_served_details(searchable)
    return or_(KBActivityLog.event.ilike(pattern), searchable.ilike(pattern))


async def _visible_kb_for(
    db: AsyncSession, user: User, kb_id: str
) -> tuple[KnowledgeBase, bool]:
    """The knowledge base *user* may see, and whether they may manage it."""
    user_group_ids = await get_user_group_ids(db, user.id)
    kb = await get_user_knowledge_base(
        db, user.id, kb_id, user_group_ids, is_admin=user.is_admin
    )
    return kb, await can_manage_entity(db, user, kb, user_group_ids)


#: The event types of the activity filter, each the event name prefixes it
#: covers: the tab's Event type pill sends one or several of these keys.
ACTIVITY_EVENT_TYPES: dict[str, tuple[str, ...]] = {
    "sync": ("sync_",),
    "documents": ("document_",),
    "embeddings": ("embedding_", "reprocess_"),
}

ActivityEventType = Literal["sync", "documents", "embeddings"]


def _activity_event_type_filter(event_types: list[str]) -> ColumnElement[bool]:
    """The ``event_type`` filter of the activity list and of its count.

    An entry matches when its event starts with a prefix of any type asked;
    the prefixes are fixed identifiers, so no escaping is needed beyond the
    ``_`` wildcard ``startswith(autoescape=True)`` takes care of.
    """
    prefixes = [prefix for key in event_types for prefix in ACTIVITY_EVENT_TYPES[key]]
    return or_(*(KBActivityLog.event.startswith(prefix, autoescape=True) for prefix in prefixes))


@router.get("/{kb_id}/activity", response_model=list[KBActivityLogResponse])
async def list_activity_logs(
    kb_id: str,
    limit: int = 50,
    offset: int = 0,
    search: str | None = None,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    event_type: Annotated[
        list[ActivityEventType] | None,
        Query(description="Event types to keep (sync, documents, embeddings), repeated"),
    ] = None,
    direction: Annotated[Literal["asc", "desc"], Query(description="Order by time, newest first by default")] = "desc",
):
    """List activity logs for a knowledge base (most recent first).

    A sync error in an entry is served in full to a caller who can manage the
    knowledge base, and as a fixed sentence to any other reader
    (``served_sync_text``).

    Query params:
      - search: case-insensitive search across event type and details, as
        served to the caller (``_activity_search_filter``)
      - event_type: one or several of ``ACTIVITY_EVENT_TYPES`` (repeated)
      - direction: ``desc`` (default, newest first) or ``asc``
      - offset: number of rows to skip (for pagination)
    """
    _kb, manages_kb = await _visible_kb_for(db, user, kb_id)
    query = select(KBActivityLog).where(KBActivityLog.knowledge_base_id == kb_id)
    if search:
        query = query.where(_activity_search_filter(search, can_manage=manages_kb))
    if event_type:
        query = query.where(_activity_event_type_filter(list(event_type)))
    order = KBActivityLog.created_at.asc() if direction == "asc" else KBActivityLog.created_at.desc()
    result = await db.execute(
        query.order_by(order, KBActivityLog.id)
        .offset(offset)
        .limit(min(limit, 200))
    )
    return [
        KBActivityLogResponse(
            id=str(log.id),
            event=log.event,
            details=served_sync_text(log.event, log.details, can_manage=manages_kb),
            source_type=log.source_type,
            document_count=log.document_count,
            created_at=log.created_at.isoformat(),
        )
        for log in result.scalars().all()
    ]


@router.get("/{kb_id}/activity/count")
async def count_activity_logs(
    kb_id: str,
    search: str | None = None,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    event_type: Annotated[
        list[ActivityEventType] | None,
        Query(description="Event types to keep (sync, documents, embeddings), repeated"),
    ] = None,
):
    """Return the total number of activity logs matching the given filters."""
    _kb, manages_kb = await _visible_kb_for(db, user, kb_id)
    query = (
        select(func.count())
        .select_from(KBActivityLog)
        .where(
            KBActivityLog.knowledge_base_id == kb_id,
        )
    )
    if search:
        query = query.where(_activity_search_filter(search, can_manage=manages_kb))
    if event_type:
        query = query.where(_activity_event_type_filter(list(event_type)))
    result = await db.execute(query)
    return {"count": result.scalar_one()}


@router.get("/{kb_id}/documents/count")
async def count_documents(
    kb_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Return the total number of documents in a knowledge base."""
    await get_user_knowledge_base(db, user.id, kb_id, is_admin=user.is_admin)
    result = await db.execute(
        select(func.count())
        .select_from(KnowledgeDocument)
        .where(
            KnowledgeDocument.knowledge_base_id == kb_id,
        )
    )
    return {"count": result.scalar_one()}


# ---------------------------------------------------------------------------
# Single-document read / update
#
# These MUST stay below every literal ``/{kb_id}/documents/<literal>`` route
# (notably ``/documents/count`` above): FastAPI matches in declaration order, so
# a bare ``/{kb_id}/documents/{doc_id}`` declared earlier would swallow the
# literal paths and shadow those endpoints.
# ---------------------------------------------------------------------------


@router.get(
    "/{kb_id}/documents/{doc_id}",
    response_model=KnowledgeDocumentDetailResponse,
)
async def get_document(
    kb_id: str,
    doc_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Return one document with its complete, verbatim stored source.

    The vector layer is lossy by design — chunk retrieval finds relevant
    fragments but cannot reconstruct a document faithfully. This is the
    de-chunked read: the original text as stored at ingest time, plus the
    embedding state so a caller can tell whether search has caught up
    (write-then-read). Unlike ``/download`` it returns JSON, so a client gets
    the metadata and the text in one call.
    """
    kb = await get_user_knowledge_base(db, user.id, kb_id, is_admin=user.is_admin)
    doc = await _get_kb_document_or_404(db, kb_id, doc_id)
    return _doc_to_detail_response(doc, kb)


@router.put(
    "/{kb_id}/documents/{doc_id}",
    response_model=KnowledgeDocumentDetailResponse,
)
async def update_document(
    kb_id: str,
    doc_id: str,
    body: UpdateKnowledgeDocumentRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _ai_gate: None = Depends(require_ai_enabled),
):
    """Replace (or append to) a document's source and re-embed it.

    Re-embedding is queued rather than run inline, exactly like the reprocess
    endpoint: the document returns to ``pending`` and becomes searchable again
    once the worker has re-chunked and re-embedded it. Its existing vectors are
    dropped up front so search never returns the previous content as if it were
    current — the source text stays readable through this endpoint throughout.

    Only work that is actually needed is done. A request that changes nothing is
    a no-op, and a rename alone does not touch the vectors (they are built from
    the content) unless, in a knowledge base that renders HTML, it changes
    whether the document reads as a page. Re-embedding fires when the source
    changed, or when the document is not currently ``ready`` — re-submitting
    identical content is how a failed embed is retried.
    """
    kb = await get_user_knowledge_base(db, user.id, kb_id, is_admin=user.is_admin)
    await assert_writable_knowledge_base(
        db,
        kb,
        user,
        action="update documents in",
        content_action="update its documents",
    )

    # Same guard as the text-document endpoint: PostgreSQL text columns cannot
    # store NUL bytes, so a binary payload posted as text would otherwise fail
    # with an opaque 500 on flush.
    if "\x00" in body.content:
        raise HTTPException(
            status_code=400,
            detail=(
                "Document content contains binary data and cannot be stored as "
                "text. Upload binary files (PDF, DOCX, PPTX, XLSX) via the file "
                "upload endpoint, which extracts their text automatically."
            ),
        )

    doc = await _get_kb_document_or_404(db, str(kb.id), doc_id)

    # Validate everything BEFORE the destructive delete below, so a rejected
    # request never leaves work half-done even if the rollback path changes.
    new_filename: str | None = None
    if body.filename is not None:
        new_filename = body.filename.strip()
        if not new_filename:
            raise HTTPException(
                status_code=400, detail="filename cannot be empty"
            )

    previous = doc.content or ""
    if body.mode == "append":
        separator = "\n\n" if previous and body.content else ""
        new_content = f"{previous}{separator}{body.content}"
    else:
        new_content = body.content

    content_changed = new_content != previous
    rename_requested = new_filename is not None and new_filename != doc.filename

    # Nothing to apply and nothing to repair: the stored state already matches
    # the request. Returning early keeps a no-op PUT from dropping the vectors
    # and opening a needless unsearchable window (and from spending a full
    # re-embed) — the same contract ``update_knowledge_document`` gives agents.
    if not content_changed and not rename_requested and doc.status == "ready":
        return _doc_to_detail_response(doc, kb)

    # Re-embed when the SOURCE changed, or when the document is not currently
    # ``ready`` — re-submitting identical content is how a failed embed is
    # retried. A rename alone does NOT invalidate the vectors: the embeddings are
    # built from the content, so renaming and re-embedding are independent -
    # except where the name decides whether a knowledge base that renders HTML
    # reads the document as a page.
    html_ingestion = getattr(kb, "html_ingestion", None)
    reclassified = rename_requested and renders_html(
        new_content, doc.filename, html_ingestion
    ) != renders_html(new_content, new_filename, html_ingestion)
    needs_reembed = content_changed or doc.status != "ready" or reclassified

    if needs_reembed:
        await reset_changed_document(db, doc, kb=kb)

    if rename_requested:
        doc.filename = new_filename
    if content_changed:
        doc.content = new_content
        doc.content_hash = hashlib.sha256(new_content.encode()).hexdigest()
    if content_changed or rename_requested:
        await bump_kb_revision(db, kb.id)

    if content_changed:
        activity_detail = (
            f"Updated document '{doc.filename}' "
            f"({body.mode}, {len(new_content)} characters)"
        )
    elif rename_requested:
        activity_detail = f"Renamed document to '{doc.filename}'"
    else:
        activity_detail = (
            f"Re-embedding document '{doc.filename}' (content unchanged)"
        )
    await log_kb_activity(
        db,
        kb.id,
        "document_updated",
        details=activity_detail,
        source_type=doc.source_type,
        document_count=1,
    )
    await db.commit()
    if needs_reembed:
        await enqueue_document_processing(str(doc.id))

    await db.refresh(doc)
    return _doc_to_detail_response(doc, kb)


# ---------------------------------------------------------------------------
# Sync Sources – persistent sync configurations with optional schedule
# ---------------------------------------------------------------------------


async def _assert_sync_sources_licensed(db: AsyncSession) -> None:
    """Gate KB sync-source mutations behind the ONE licence answer.

    Automated import sources are an Enterprise capability, so the frontend
    renders the panel read-only while AI is not licensed. Enforce the same gate
    server-side so the UI restriction can't be bypassed via direct API calls.
    ``is_agentic_ai_licensed`` rather than the edition: Copilot mode has no
    edition (``""``), and reading the edition alone let an unlicensed Copilot
    deployment write sync sources while every other agentic surface refused.
    """
    from app.api.deps import licence_required_detail
    from app.api.v1.platform import is_agentic_ai_licensed

    if not await is_agentic_ai_licensed(db):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=licence_required_detail(),
        )


@router.get("/{kb_id}/sync-sources", response_model=list[SyncSourceResponse])
async def list_sync_sources(
    kb_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List all configured sync sources for a knowledge base.

    ``last_sync_error`` is served in full to a caller who can manage the
    knowledge base, and as a fixed sentence to any other reader
    (``served_sync_text``).
    """
    kb, manages_kb = await _visible_kb_for(db, user, kb_id)
    result = await db.execute(
        select(KBSyncSource)
        .where(KBSyncSource.knowledge_base_id == kb.id)
        .order_by(KBSyncSource.created_at.desc())
    )
    sources = result.scalars().all()

    # Batch-load integration labels for sources with explicit bindings
    from app.models.integration import Integration as IntegrationModel

    integration_ids = {
        s.integration_id for s in sources if getattr(s, "integration_id", None)
    }
    labels: dict[str, str] = {}
    if integration_ids:
        int_result = await db.execute(
            select(IntegrationModel.id, IntegrationModel.name).where(
                IntegrationModel.id.in_(integration_ids)
            )
        )
        labels = {str(row.id): row.name for row in int_result.all()}

    return [
        _sync_source_to_response(
            s, integration_name=labels.get(str(s.integration_id)), can_manage=manages_kb
        )
        for s in sources
    ]


async def _assert_may_bind_integration(
    db: AsyncSession, user: User, integration: Any
) -> None:
    """Who may bind *integration* to a sync source: an admin, its owner, or
    anyone who may see it when it is company-managed.

    ``access_restricted`` narrows a company-managed integration to its user
    and group grants, so whoever binds one - the knowledge base's
    owner or a co-manager - must be one of them. A colleague's
    personal integration is never bound here, even when it is shared.
    """
    if user.role == "admin" or str(integration.user_id) == str(user.id):
        return
    if integration.is_company_managed and await integration_visible_to_user(
        db, integration.id, user.id
    ):
        return
    raise HTTPException(
        status_code=403, detail="You don't have access to this integration"
    )


@router.post(
    "/{kb_id}/sync-sources",
    response_model=SyncSourceResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_sync_source(
    kb_id: str,
    body: CreateSyncSourceRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _ai_gate: None = Depends(require_ai_enabled),
):
    """Create a new sync source with optional schedule.

    Import sources are editable even on built-in knowledge bases (the rest of
    the built-in config stays read-only). This lets operators point sources at
    internal mirrors / airgapped URLs. Once modified, startup seeding stops
    overwriting this KB's sync sources (see ``sync_sources_customized``).
    """
    kb = await get_user_knowledge_base(db, user.id, kb_id, is_admin=user.is_admin)
    await assert_knowledge_base_not_package_origin(db, kb.id)
    await _assert_sync_sources_licensed(db)

    await _assert_kb_manageable(db, user, kb, "add sync sources to")

    if body.sync_interval not in VALID_SYNC_INTERVALS:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid sync_interval. Must be one of: {', '.join(sorted(VALID_SYNC_INTERVALS))}",
        )

    if body.source_type not in VALID_SYNC_SOURCE_TYPES:
        raise HTTPException(
            status_code=400,
            detail=(
                "Invalid source_type. Must be one of: "
                f"{', '.join(sorted(VALID_SYNC_SOURCE_TYPES))}"
            ),
        )

    # Validate integration_id if provided
    integration_name = None
    if body.integration_id:
        from app.models.integration import Integration as IntegrationModel

        int_result = await db.execute(
            select(IntegrationModel).where(IntegrationModel.id == body.integration_id)
        )
        integration = int_result.scalar_one_or_none()
        if not integration:
            raise HTTPException(status_code=400, detail="Integration not found")
        await _assert_may_bind_integration(db, user, integration)
        # The binder's own account on a per_user integration, whose
        # status is the one that counts.
        holder = await usable_credential_holder(db, integration, user.id, is_admin=user.role == "admin")
        if not holder_connected(holder):
            raise HTTPException(status_code=400, detail="Integration is not connected")
        integration_name = integration.name

    source = KBSyncSource(
        knowledge_base_id=kb.id,
        user_id=user.id,
        source_type=body.source_type,
        config=body.config,
        sync_interval=body.sync_interval,
        integration_id=body.integration_id,
    )
    db.add(source)
    # Mark built-in KB sources as user-customized so seeding stops overwriting them
    if kb.is_builtin:
        kb.sync_sources_customized = True
    # Commit before answering: the get_db teardown only commits after the response is sent.
    await db.commit()
    return _sync_source_to_response(
        source, integration_name=integration_name, can_manage=True
    )


@router.put("/{kb_id}/sync-sources/{source_id}", response_model=SyncSourceResponse)
async def update_sync_source(
    kb_id: str,
    source_id: str,
    body: UpdateSyncSourceRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _ai_gate: None = Depends(require_ai_enabled),
):
    """Update a sync source configuration or schedule."""
    kb = await get_user_knowledge_base(db, user.id, kb_id, is_admin=user.is_admin)
    await assert_knowledge_base_not_package_origin(db, kb.id)
    await _assert_sync_sources_licensed(db)

    await _assert_kb_manageable(db, user, kb, "edit sync sources on")

    result = await db.execute(
        select(KBSyncSource).where(
            KBSyncSource.id == parse_path_id(source_id, not_found_detail="Sync source not found"),
            KBSyncSource.knowledge_base_id == kb.id,
        )
    )
    source = result.scalar_one_or_none()
    if not source:
        raise HTTPException(status_code=404, detail="Sync source not found")

    # Re-scoping a source that stays bound keeps its binding: a KB manager may
    # edit what a source syncs.
    if body.config is not None:
        source.config = body.config
    if body.sync_interval is not None:
        if body.sync_interval not in VALID_SYNC_INTERVALS:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid sync_interval. Must be one of: {', '.join(sorted(VALID_SYNC_INTERVALS))}",
            )
        source.sync_interval = body.sync_interval
    if body.enabled is not None:
        if body.enabled and not source.enabled:
            # Re-enabling is a documented recovery path for a quarantined
            # source: refresh every watchdog
            # budget so the source gets a full run of attempts again instead
            # of being re-quarantined after a single further hang or failure,
            # and drop the scheduled backoff retry (which can sit up to 24h
            # out at quarantine-level failure counts) so the re-enabled
            # source retries promptly via the flat error cooldown.
            source.sync_stale_count = 0
            source.sync_failure_count = 0
            source.sync_perm_fail_count = 0
            source.sync_partial_fail_count = 0
            source.sync_next_retry_at = None
            # A scheduler-written auto-disable banner (quarantine or config
            # error) is stale the moment the source is enabled
            # again — clear it so the UI doesn't keep showing "Source
            # quarantined ..." / "Source disabled ..." until the next sync
            # attempt. Only scheduler-written messages are dropped: a genuine
            # last error on a manually disabled source stays visible for
            # diagnosis.
            from app.services.kb_sync_scheduler import (
                CONFIG_ERROR_PREFIX,
                PERM_FAIL_ERROR_PREFIX,
                QUARANTINE_ERROR_PREFIX,
                _reset_notion_stale_page_config,
            )

            error_msg = source.last_sync_error or ""
            if (
                error_msg.startswith(QUARANTINE_ERROR_PREFIX)
                or error_msg.startswith(PERM_FAIL_ERROR_PREFIX)
                or error_msg.startswith(CONFIG_ERROR_PREFIX)
            ):
                source.last_sync_error = None
            # Also reset per-page stale quarantine so all Notion pages get a
            # fresh start when the source is re-enabled.
            if source.source_type == "notion":
                _reset_notion_stale_page_config(source)
        source.enabled = body.enabled

    # Update integration binding
    integration_name = None
    unchanged_binding = body.integration_id is not None and bool(
        canonical_integration_ids([body.integration_id])
        & canonical_integration_ids([getattr(source, "integration_id", None)])
    )
    if unchanged_binding:
        # The integration the source already holds, sent back with an edit of
        # something else: not a new binding, so it is not re-checked against
        # whoever edits the source.
        from app.models.integration import Integration as IntegrationModel

        integration_name = (
            await db.execute(
                select(IntegrationModel.name).where(
                    IntegrationModel.id == source.integration_id
                )
            )
        ).scalar_one_or_none()
    elif body.integration_id is not None:
        if body.integration_id == "" or body.integration_id == "auto":
            source.integration_id = None
        else:
            from app.models.integration import Integration as IntegrationModel

            int_result = await db.execute(
                select(IntegrationModel).where(
                    IntegrationModel.id == body.integration_id
                )
            )
            integration = int_result.scalar_one_or_none()
            if not integration:
                raise HTTPException(status_code=400, detail="Integration not found")
            await _assert_may_bind_integration(db, user, integration)
            # The binder's own account on a per_user integration, whose
            # status is the one that counts.
            holder = await usable_credential_holder(db, integration, user.id, is_admin=user.role == "admin")
            if not holder_connected(holder):
                raise HTTPException(status_code=400, detail="Integration is not connected")
            source.integration_id = integration.id
            integration_name = integration.name
    elif getattr(source, "integration_id", None):
        # Resolve existing label for response
        from app.models.integration import Integration as IntegrationModel

        int_result = await db.execute(
            select(IntegrationModel.name).where(
                IntegrationModel.id == source.integration_id
            )
        )
        row = int_result.scalar_one_or_none()
        if row:
            integration_name = row

    # Mark built-in KB sources as user-customized so seeding stops overwriting them
    if kb.is_builtin:
        kb.sync_sources_customized = True

    # Commit before answering: the get_db teardown only commits after the response is sent.
    await db.commit()
    return _sync_source_to_response(
        source, integration_name=integration_name, can_manage=True
    )


@router.delete(
    "/{kb_id}/sync-sources/{source_id}", status_code=status.HTTP_204_NO_CONTENT
)
async def delete_sync_source(
    kb_id: str,
    source_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _ai_gate: None = Depends(require_ai_enabled),
):
    """Remove a sync source."""
    kb = await get_user_knowledge_base(db, user.id, kb_id, is_admin=user.is_admin)
    await assert_knowledge_base_not_package_origin(db, kb.id)
    await _assert_sync_sources_licensed(db)

    await _assert_kb_manageable(db, user, kb, "remove sync sources from")

    result = await db.execute(
        select(KBSyncSource).where(
            KBSyncSource.id == parse_path_id(source_id, not_found_detail="Sync source not found"),
            KBSyncSource.knowledge_base_id == kb.id,
        )
    )
    source = result.scalar_one_or_none()
    if not source:
        raise HTTPException(status_code=404, detail="Sync source not found")
    await db.delete(source)
    # Mark built-in KB sources as user-customized so seeding doesn't re-add this
    if kb.is_builtin:
        kb.sync_sources_customized = True
    # Commit before answering: the get_db teardown only commits after the response is sent.
    await db.commit()


@router.post(
    "/{kb_id}/sync-sources/{source_id}/trigger", status_code=status.HTTP_202_ACCEPTED
)
async def trigger_sync_source(
    kb_id: str,
    source_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _ai_gate: None = Depends(require_ai_enabled),
):
    """Manually trigger a configured sync source (runs in background).

    Returns immediately with 202 Accepted. The sync runs asynchronously;
    poll the sync source to track status (``last_sync_status`` will be
    ``syncing`` → ``success`` or ``error``).
    """

    kb = await get_user_knowledge_base(db, user.id, kb_id, is_admin=user.is_admin)
    await assert_knowledge_base_not_package_origin(db, kb.id)
    await _assert_sync_sources_licensed(db)

    await _assert_kb_manageable(db, user, kb, "trigger syncs on")

    result = await db.execute(
        select(KBSyncSource).where(
            KBSyncSource.id == parse_path_id(source_id, not_found_detail="Sync source not found"),
            KBSyncSource.knowledge_base_id == kb.id,
        )
    )
    source = result.scalar_one_or_none()
    if not source:
        raise HTTPException(status_code=404, detail="Sync source not found")

    # Reject if already syncing
    if source.last_sync_status == "syncing":
        raise HTTPException(status_code=409, detail="Sync already in progress")

    # Mark as syncing immediately (persisted before response)
    source.last_sync_status = "syncing"
    source.last_sync_error = None
    # Committed, not only flushed, before the sync starts on its own session:
    # the get_db teardown commits after the response, late enough to overwrite
    # the status a quick sync already finished with.
    await db.commit()

    # Launch sync in background with a fresh DB session
    _spawn_sync_task(
        _run_sync_background(
            str(source.id),
            str(kb.id),
            str(user.id),
        )
    )

    return {"status": "syncing", "message": "Sync started in background"}


@router.post(
    "/{kb_id}/sync-sources/{source_id}/force-resync",
    status_code=status.HTTP_202_ACCEPTED,
)
async def force_resync_source(
    kb_id: str,
    source_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _ai_gate: None = Depends(require_ai_enabled),
):
    """Force re-sync: reset stuck status, purge synced documents, start fresh.

    Use when a sync is stuck in ``syncing`` state with no activity.
    Deletes the documents this source owns (``kb_sync.owned_sync_documents``:
    the ones it imported, plus the unowned ones of its type - never another
    source's), clears the associated vector embeddings, resets the sync
    status, and launches a fresh sync in the background.
    """
    from app.services.kb_sync import owned_sync_documents

    kb = await get_user_knowledge_base(db, user.id, kb_id, is_admin=user.is_admin)
    await assert_knowledge_base_not_package_origin(db, kb.id)
    await _assert_sync_sources_licensed(db)

    await _assert_kb_manageable(db, user, kb, "force re-sync on")

    result = await db.execute(
        select(KBSyncSource).where(
            KBSyncSource.id == parse_path_id(source_id, not_found_detail="Sync source not found"),
            KBSyncSource.knowledge_base_id == kb.id,
        )
    )
    source = result.scalar_one_or_none()
    if not source:
        raise HTTPException(status_code=404, detail="Sync source not found")

    # 1. Delete the documents this source owns, plus their
    #    embedding chunks. Embeddings are keyed by ``source_id == str(doc.id)``
    #    (a plain string, not a FK — only ``knowledge_base_id`` cascades), so
    #    they must be purged explicitly or they keep surfacing in RAG search.
    #    Bulk statements instead of per-row ORM deletes: a large synced KB can
    #    hold thousands of documents.
    doc_rows_result = await db.execute(
        select(KnowledgeDocument.id, KnowledgeDocument.chunk_count).where(
            KnowledgeDocument.knowledge_base_id == kb.id,
            owned_sync_documents(source.source_type, source.id),
        )
    )
    doc_rows = doc_rows_result.all()
    doc_ids = [str(doc_id) for doc_id, _ in doc_rows]
    purged_chunks = sum(chunk_count or 0 for _, chunk_count in doc_rows)
    purged_count = len(doc_ids)
    if doc_ids:
        await db.execute(
            delete(Embedding).where(
                Embedding.knowledge_base_id == kb.id,
                Embedding.source_type == "document",
                Embedding.source_id.in_(doc_ids),
            )
        )
        await db.execute(
            delete(KnowledgeDocument).where(
                KnowledgeDocument.knowledge_base_id == kb.id,
                KnowledgeDocument.id.in_([doc_id for doc_id, _ in doc_rows]),
            )
        )
        cancel_document_jobs_after_commit(db, doc_ids)
    if purged_count:
        kb.document_count = max(0, (kb.document_count or 0) - purged_count)
        await bump_kb_revision(db, kb.id)
        # Keep chunk_count in sync too — it feeds the KB stats UI and the
        # "empty KB" health checks. Relying on the post-sync recount alone
        # leaves it permanently inflated if the fresh sync fails or yields
        # zero documents.
        kb.chunk_count = max(0, (kb.chunk_count or 0) - purged_chunks)
        logger.info(
            "Force re-sync: purged %d documents for source %s (type=%s)",
            purged_count,
            source_id,
            source.source_type,
        )

    # 2. Reset sync status and clear auto-disable state so a source that was
    #    auto-disabled by a watchdog (stale-reset, error-count,
    #    or permanent-failure) or by a permanent configuration error
    #    can be recovered via Force Re-sync without needing a
    #    separate settings edit.
    #    Auto-disable is detected by the error-message prefix the scheduler
    #    writes — NOT by ``enabled`` alone: a disabled source whose
    #    ``last_sync_error`` carries no known prefix (e.g. one an operator
    #    disabled manually) stays disabled, while a disabled source still
    #    carrying a scheduler-written banner is re-enabled (force re-sync
    #    still runs one sync either way, matching the pre-quarantine
    #    behaviour of this endpoint). Re-enabling a source whose config is
    #    still broken is safe: the next sync fast-fails with
    #    KBSyncConfigError and re-disables it on the first attempt.
    from app.services.kb_sync_scheduler import (
        CONFIG_ERROR_PREFIX,
        PERM_FAIL_ERROR_PREFIX,
        QUARANTINE_ERROR_PREFIX,
        _reset_notion_stale_page_config,
    )

    error_msg = source.last_sync_error or ""
    was_quarantined = not source.enabled and (
        error_msg.startswith(QUARANTINE_ERROR_PREFIX)
        or error_msg.startswith(PERM_FAIL_ERROR_PREFIX)
    )
    was_config_disabled = not source.enabled and error_msg.startswith(
        CONFIG_ERROR_PREFIX
    )
    if was_quarantined or was_config_disabled:
        source.enabled = True
    source.last_sync_status = "syncing"
    source.last_sync_error = None
    source.last_sync_documents = 0
    source.sync_stale_count = 0
    source.sync_perm_fail_count = 0
    source.sync_partial_fail_count = 0
    source.sync_failure_count = 0
    source.sync_next_retry_at = None
    # Also reset any per-page stale-page quarantine so all Notion pages get
    # a fresh start after a force re-sync.
    if source.source_type == "notion":
        _reset_notion_stale_page_config(source)
    # Clear the stale Notion child-database tracking state — the counted 404
    # tracking dict (``stale_child_dbs``), the non-404 failure
    # counters (``stale_child_databases``), and the legacy one-shot
    # 404 skip list (``skipped_child_db_ids``, superseded by the counted
    # dict) — so the fresh sync re-attempts previously failing references.
    # Force re-sync is the documented recovery path when a child database is
    # re-shared with the integration or restored from trash. Reassign a
    # fresh dict so the ORM change tracker picks up the JSON-column mutation.
    src_config = source.config or {}
    if (
        src_config.get("skipped_child_db_ids")
        or src_config.get("stale_child_dbs")
        or src_config.get("stale_child_databases")
    ):
        config = dict(src_config)
        config.pop("skipped_child_db_ids", None)
        config.pop("stale_child_dbs", None)
        config.pop("stale_child_databases", None)
        source.config = config
    # Committed, not only flushed, before the sync starts on its own session:
    # it must see the purge and the reset, not wait on their row locks.
    await db.commit()

    # 3. Launch fresh sync in background
    _spawn_sync_task(_run_sync_background(str(source.id), str(kb.id), str(user.id)))

    activity_detail = (
        f"Force re-sync started for {source.source_type} (purged {purged_count} documents)"
        + (" — quarantine cleared" if was_quarantined else "")
        + (" — configuration-error disable cleared" if was_config_disabled else "")
    )
    await log_kb_activity(
        db,
        kb.id,
        "force_resync",
        details=activity_detail,
        source_type=source.source_type,
    )
    # Commit before answering: the get_db teardown only commits after the response is sent.
    await db.commit()

    return {
        "status": "syncing",
        "message": (
            f"Force re-sync started. {purged_count} existing documents purged."
            + (" Quarantine cleared — source re-enabled." if was_quarantined else "")
            + (
                " Configuration-error disable cleared — source re-enabled."
                if was_config_disabled
                else ""
            )
        ),
        "purged_documents": purged_count,
    }


async def _run_sync_background(source_id: str, kb_id: str, user_id: str) -> None:
    """Execute sync in a background task with its own DB session."""
    import uuid

    from app.core.database import async_session
    from app.models.knowledge_base import KnowledgeBase

    # Bind the identifiers known from the arguments around the WHOLE body —
    # including the except handlers below — so the not-found / failure /
    # session-error records also carry structured kb_id / sync_source_id
    # (an except handler runs after an inner ``with`` has unwound).
    with kb_sync_log_context(kb_id=kb_id, sync_source_id=source_id):
        try:
            async with async_session() as db:
                try:
                    src_result = await db.execute(
                        select(KBSyncSource).where(
                            KBSyncSource.id == uuid.UUID(source_id)
                        )
                    )
                    source = src_result.scalar_one_or_none()
                    if not source:
                        logger.error(
                            "Background sync: source %s not found (kb_id=%s)",
                            source_id,
                            kb_id,
                        )
                        return

                    kb_result = await db.execute(
                        select(KnowledgeBase).where(
                            KnowledgeBase.id == uuid.UUID(kb_id)
                        )
                    )
                    kb = kb_result.scalar_one_or_none()
                    if not kb:
                        logger.error("Background sync: KB %s not found", kb_id)
                        return

                    from app.services.kb_sync_scheduler import execute_sync_source

                    # Captured as plain strings up front so the log calls
                    # below never touch the ORM objects after
                    # execute_sync_source's commits/rollbacks may have
                    # degraded the session (an expired-attribute access
                    # would lazy-load → MissingGreenlet).
                    kb_name = kb.name
                    source_type = source.source_type

                    # Enrich the context with the loaded name/type; the
                    # try/except lives INSIDE the block so the failure
                    # record also carries the full identifiers.
                    with kb_sync_log_context(
                        kb_name=kb_name, source_type=source_type
                    ):
                        try:
                            await execute_sync_source(db, source, kb, user_id)
                            await db.commit()
                            logger.info(
                                "Background sync completed for source %s (%s) "
                                "on KB '%s' (%s)",
                                source_id,
                                source_type,
                                kb_name,
                                kb_id,
                            )
                        except Exception as exc:
                            # Commit the error status set by execute_sync_source
                            await db.commit()
                            logger.error(
                                "Background sync failed for source %s "
                                "(kb_id=%s): %s",
                                source_id,
                                kb_id,
                                format_exception_message(exc),
                            )

                except Exception as exc:
                    # Load/setup failure before the sync started (the sync
                    # failure path is handled with full context above).
                    with contextlib.suppress(Exception):
                        await db.rollback()
                    logger.error(
                        "Background sync setup failed for source %s (kb_id=%s): %s",
                        source_id,
                        kb_id,
                        format_exception_message(exc),
                    )
        except Exception as exc:
            logger.error(
                "Background sync session error for source %s (kb_id=%s): %s",
                source_id,
                kb_id,
                format_exception_message(exc),
            )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


# Sentinel distinguishing "latest_error not precomputed → query it" from a
# precomputed ``None`` (the KB genuinely has no error doc).
_UNSET: object = object()


async def _load_workspace_names(
    db: AsyncSession, kbs: list[KnowledgeBase]
) -> dict[str, str]:
    """``{workspace_id: name}`` for the workspaces owning *kbs* - one query."""
    ids = {wid for kb in kbs if (wid := owning_workspace_id(kb)) is not None}
    if not ids:
        return {}
    from app.models.workspace import Workspace

    rows = await db.execute(select(Workspace.id, Workspace.name).where(Workspace.id.in_(ids)))
    return {str(wid): name for wid, name in rows.all()}


async def _kb_to_response(
    kb: KnowledgeBase,
    db: AsyncSession,
    *,
    can_manage: bool,
    is_package_origin: bool,
    owner_name: str | None = None,
    stale_info: dict[str, int] | None = None,
    status_counts: dict[str, int] | None = None,
    latest_error: str | None | object = _UNSET,
    next_retry: str | None | object = _UNSET,
    retryable_error_count: int | object = _UNSET,
    workspace_names: dict[str, str] | None = None,
) -> KnowledgeBaseResponse:
    # Document status breakdown. The list endpoint precomputes these for the
    # whole page in two batched queries (``_load_kb_status_counts`` /
    # ``_load_kb_latest_errors``) and passes them in, so we don't fire two
    # queries per KB — the N+1 that storms ``knowledge_documents`` and 500s
    # the endpoint under load. Single-KB callers omit them and fall
    # back to the per-KB queries below.
    if status_counts is None:
        from sqlalchemy import func as sa_func

        result = await db.execute(
            select(
                sa_func.count()
                .filter(KnowledgeDocument.status == "ready")
                .label("ready"),
                sa_func.count()
                .filter(KnowledgeDocument.status.in_(["pending", "processing"]))
                .label("pending"),
                sa_func.count()
                .filter(KnowledgeDocument.status == "error")
                .label("error"),
            ).where(KnowledgeDocument.knowledge_base_id == kb.id)
        )
        row = result.one()
        ready_count = row.ready or 0
        pending_count = row.pending or 0
        error_count = row.error or 0
    else:
        ready_count = status_counts.get("ready", 0)
        pending_count = status_counts.get("pending", 0)
        error_count = status_counts.get("error", 0)

    if latest_error is _UNSET:
        latest_error_result = await db.execute(
            select(KnowledgeDocument.error_message)
            .where(
                KnowledgeDocument.knowledge_base_id == kb.id,
                KnowledgeDocument.status == "error",
                KnowledgeDocument.error_message.is_not(None),
            )
            .order_by(KnowledgeDocument.created_at.desc())
            .limit(1)
        )
        latest_error_message = latest_error_result.scalar_one_or_none()
    else:
        latest_error_message = cast(str | None, latest_error)

    if next_retry is _UNSET or retryable_error_count is _UNSET:
        next_retry_result = await db.execute(
            select(
                func.min(KnowledgeDocument.embedding_next_retry_at).label(
                    "next_retry"
                ),
                func.count().label("retryable_count"),
            ).where(
                KnowledgeDocument.knowledge_base_id == kb.id,
                KnowledgeDocument.status == "error",
                KnowledgeDocument.embedding_next_retry_at.is_not(None),
            )
        )
        retry_row = next_retry_result.one()
        next_auto_retry_at = (
            retry_row.next_retry.isoformat() if retry_row.next_retry else None
        )
        retryable_count = int(retry_row.retryable_count or 0)
    else:
        next_auto_retry_at = cast(str | None, next_retry)
        retryable_count = cast(int, retryable_error_count)

    workspace_id = owning_workspace_id(kb)
    managed_by_workspace = None
    if workspace_id is not None:
        if workspace_names is None:
            workspace_names = await _load_workspace_names(db, [kb])
        managed_by_workspace = ManagingWorkspace(
            id=str(workspace_id), name=workspace_names.get(str(workspace_id), "")
        )

    return KnowledgeBaseResponse(
        id=str(kb.id),
        user_id=str(kb.user_id) if kb.user_id else None,
        owner_name=owner_name,
        name=kb.name,
        description=kb.description,
        embedding_model=kb.embedding_model,
        is_system_managed=kb.is_system_managed,
        is_builtin=kb.is_builtin,
        sync_sources_customized=kb.sync_sources_customized,
        is_read_only=bool(kb.is_read_only),
        html_ingestion=normalize_html_ingestion(getattr(kb, "html_ingestion", None)),
        describe_figures=bool(getattr(kb, "describe_figures", False)),
        is_company_managed=kb.is_company_managed,
        shared_group_ids=kb.shared_group_ids,
        shared_group_permissions=kb.shared_group_permissions,
        shared_user_ids=kb.shared_user_ids,
        shared_user_permissions=kb.shared_user_permissions,
        access_restricted=kb.access_restricted,
        # A workspace's knowledge base is managed from the workspace only.
        can_manage=can_manage and workspace_id is None,
        is_package_origin=is_package_origin,
        content_lock=knowledge_base_content_lock(
            kb, package_installed=is_package_origin
        ),
        managed_by_workspace=managed_by_workspace,
        tags=kb.tags or [],
        document_count=kb.document_count,
        chunk_count=kb.chunk_count,
        query_count=kb.query_count,
        ready_count=ready_count,
        pending_count=pending_count,
        error_count=error_count,
        latest_error_message=public_error_message(latest_error_message),
        latest_error_code=public_error_code(latest_error_message),
        next_auto_retry_at=next_auto_retry_at,
        retryable_error_count=retryable_count,
        has_stale_embeddings=bool(stale_info and stale_info.get("stale_chunk_count", 0) > 0),
        stale_tuple_count=(stale_info or {}).get("stale_tuple_count", 0),
        stale_chunk_count=(stale_info or {}).get("stale_chunk_count", 0),
        created_at=kb.created_at.isoformat(),
        updated_at=kb.updated_at.isoformat(),
    )


async def _load_kb_status_counts(
    db: AsyncSession,
    kbs: list[KnowledgeBase],
) -> dict[str, dict[str, int]]:
    """Return per-KB document status breakdowns in a single grouped query.

    Replaces the per-KB count query in ``_kb_to_response`` for the list
    endpoint: instead of N ``count(*) FILTER … WHERE knowledge_base_id = $1``
    statements (the N+1 that, on a slow scan, storms ``knowledge_documents``
    and 500s ``list_knowledge_bases`` under load), one
    ``GROUP BY knowledge_base_id`` covers the whole page and rides the new
    ``(knowledge_base_id, status)`` index. KBs with no documents are absent
    from the result (callers default to zeros).
    """
    if not kbs:
        return {}

    kb_ids = [kb.id for kb in kbs]
    result = await db.execute(
        select(
            KnowledgeDocument.knowledge_base_id,
            func.count().filter(KnowledgeDocument.status == "ready").label("ready"),
            func.count()
            .filter(KnowledgeDocument.status.in_(["pending", "processing"]))
            .label("pending"),
            func.count().filter(KnowledgeDocument.status == "error").label("error"),
        )
        .where(KnowledgeDocument.knowledge_base_id.in_(kb_ids))
        .group_by(KnowledgeDocument.knowledge_base_id)
    )
    return {
        str(row.knowledge_base_id): {
            "ready": row.ready or 0,
            "pending": row.pending or 0,
            "error": row.error or 0,
        }
        for row in result.all()
    }


async def _load_kb_latest_errors(
    db: AsyncSession,
    kbs: list[KnowledgeBase],
) -> dict[str, str]:
    """Return the most recent error message per KB in a single query.

    ``DISTINCT ON (knowledge_base_id) … ORDER BY knowledge_base_id,
    created_at DESC`` collapses to one row (the newest error) per KB, so the
    list endpoint avoids the per-KB ``ORDER BY created_at DESC LIMIT 1`` that
    compounded the N+1. Only KBs that actually have an error doc with
    a message appear in the result.
    """
    if not kbs:
        return {}

    kb_ids = [kb.id for kb in kbs]
    result = await db.execute(
        select(
            KnowledgeDocument.knowledge_base_id,
            KnowledgeDocument.error_message,
        )
        .where(
            KnowledgeDocument.knowledge_base_id.in_(kb_ids),
            KnowledgeDocument.status == "error",
            KnowledgeDocument.error_message.is_not(None),
        )
        .order_by(
            KnowledgeDocument.knowledge_base_id,
            KnowledgeDocument.created_at.desc(),
        )
        .distinct(KnowledgeDocument.knowledge_base_id)
    )
    return {
        str(row.knowledge_base_id): row.error_message for row in result.all()
    }


async def _load_kb_next_auto_retries(
    db: AsyncSession,
    kbs: list[KnowledgeBase],
) -> dict[str, tuple[str, int]]:
    """Return ``(earliest auto-retry, retryable doc count)`` per KB, batched.

    Failed (``error``) documents with a non-NULL ``embedding_next_retry_at``
    are picked up automatically by the ``recover_failed_documents`` sweeper;
    surfacing the earliest schedule per KB lets the dashboard warning banner
    tell the user *when* the self-heal will happen instead of looking
    permanently stuck. The retryable count lets the UI distinguish "all
    failed docs will self-heal" from a mixed state where some docs failed
    structurally (NULL ``embedding_next_retry_at``) and still need manual
    action. Batched like ``_load_kb_latest_errors`` to avoid the per-KB N+1.
    Only KBs with at least one scheduled retry appear in the result.
    """
    if not kbs:
        return {}

    kb_ids = [kb.id for kb in kbs]
    result = await db.execute(
        select(
            KnowledgeDocument.knowledge_base_id,
            func.min(KnowledgeDocument.embedding_next_retry_at).label("next_retry"),
            func.count().label("retryable_count"),
        )
        .where(
            KnowledgeDocument.knowledge_base_id.in_(kb_ids),
            KnowledgeDocument.status == "error",
            KnowledgeDocument.embedding_next_retry_at.is_not(None),
        )
        .group_by(KnowledgeDocument.knowledge_base_id)
    )
    return {
        str(row.knowledge_base_id): (
            row.next_retry.isoformat(),
            int(row.retryable_count or 0),
        )
        for row in result.all()
        if row.next_retry is not None
    }


async def _load_kb_stale_embedding_info(
    db: AsyncSession,
    kbs: list[KnowledgeBase],
) -> dict[str, dict[str, int]]:
    """Return per-KB stale tuple/chunk counts based on stored embeddings.

    Stale is evaluated against current platform embedding target:
    - model mismatch OR dimension mismatch (using stored embedding_dimensions,
      falling back to vector_dims when metadata is null).
    """
    if not kbs:
        return {}

    kb_ids = [str(kb.id) for kb in kbs]
    # No chunks => cannot have stored stale tuples
    if not any((kb.chunk_count or 0) > 0 for kb in kbs):
        return {}

    from app.api.v1.platform_settings_helpers import (
        get_platform_settings as _load_ps,
    )

    ps = await _load_ps(db)
    expected_model = ps.embedding_model
    expected_dims = ps.embedding_dimensions

    # If platform target isn't set, stale cannot be evaluated reliably.
    if not expected_model or expected_dims is None:
        return {}

    result = await db.execute(
        text(
            """
            WITH grouped AS (
              SELECT
                e.knowledge_base_id,
                e.embedding_model,
                COALESCE(e.embedding_dimensions, vector_dims(e.embedding)) AS effective_dims,
                COUNT(*) AS chunk_count
              FROM embeddings e
              -- Ready documents only, as ``recover_stale_documents``.
              JOIN knowledge_documents d
                ON d.id::text = e.source_id AND d.status = 'ready'
              WHERE e.knowledge_base_id = ANY(:kb_ids)
                AND e.source_type = 'document'
              GROUP BY 1, 2, 3
            )
            SELECT
              knowledge_base_id,
              COUNT(*)::int AS stale_tuple_count,
              COALESCE(SUM(chunk_count), 0)::int AS stale_chunk_count
            FROM grouped
            WHERE embedding_model IS DISTINCT FROM :expected_model
               OR effective_dims IS DISTINCT FROM :expected_dims
            GROUP BY 1
            """
        ),
        {
            "kb_ids": kb_ids,
            "expected_model": expected_model,
            "expected_dims": expected_dims,
        },
    )

    info: dict[str, dict[str, int]] = {}
    for row in result.mappings().all():
        kb_id = str(row["knowledge_base_id"])
        info[kb_id] = {
            "stale_tuple_count": int(row["stale_tuple_count"]),
            "stale_chunk_count": int(row["stale_chunk_count"]),
        }
    return info


def _doc_figure_fields(d: KnowledgeDocument, kb: KnowledgeBase | None) -> dict[str, object]:
    """The figure pass of *d* as the document responses carry it.

    Only while its knowledge base describes figures: with the setting off the
    descriptions are not indexed, so a document says nothing about them.
    """
    if kb is None or not getattr(kb, "describe_figures", False):
        return {}
    status, note, pages = figure_state(getattr(d, "figure_reading", None), d.content)
    return {"figure_status": status, "figure_note": note, "figure_pages": pages}


def _doc_to_response(d: KnowledgeDocument, kb: KnowledgeBase | None = None) -> KnowledgeDocumentResponse:
    return KnowledgeDocumentResponse(
        id=str(d.id),
        filename=d.filename,
        source_type=d.source_type,
        source_url=d.source_url,
        status=d.status,
        chunk_count=d.chunk_count,
        error_message=public_error_message(d.error_message),
        error_code=public_error_code(d.error_message),
        **_doc_figure_fields(d, kb),
        created_at=d.created_at.isoformat(),
    )


def _doc_to_detail_response(
    d: KnowledgeDocument,
    kb: KnowledgeBase | None = None,
) -> KnowledgeDocumentDetailResponse:
    return KnowledgeDocumentDetailResponse(
        id=str(d.id),
        knowledge_base_id=str(d.knowledge_base_id),
        filename=d.filename,
        source_type=d.source_type,
        source_url=d.source_url,
        status=d.status,
        chunk_count=d.chunk_count,
        error_message=public_error_message(d.error_message),
        error_code=public_error_code(d.error_message),
        content=d.content,
        content_hash=d.content_hash,
        content_length=len(d.content) if d.content is not None else 0,
        **_doc_figure_fields(d, kb),
        created_at=d.created_at.isoformat(),
        updated_at=d.updated_at.isoformat(),
    )


async def _get_kb_document_or_404(
    db: AsyncSession, kb_id: str, doc_id: str
) -> KnowledgeDocument:
    """Load a document scoped to its knowledge base, or raise 404.

    The ``knowledge_base_id`` predicate is what keeps a document id from being
    readable through a knowledge base the caller happens to have access to; the
    caller is responsible for having already authorized *kb_id*.
    """
    doc_uuid = parse_path_id(doc_id, not_found_detail="Document not found")
    result = await db.execute(
        select(KnowledgeDocument).where(
            KnowledgeDocument.id == doc_uuid,
            KnowledgeDocument.knowledge_base_id == kb_id,
        )
    )
    doc = result.scalar_one_or_none()
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")
    return doc


def _sync_source_to_response(
    s: KBSyncSource,
    *,
    integration_name: str | None = None,
    can_manage: bool,
) -> SyncSourceResponse:
    return SyncSourceResponse(
        id=str(s.id),
        source_type=s.source_type,
        config=s.config or {},
        sync_interval=s.sync_interval,
        enabled=s.enabled,
        integration_id=(
            str(s.integration_id) if getattr(s, "integration_id", None) else None
        ),
        integration_name=integration_name,
        last_synced_at=s.last_synced_at.isoformat() if s.last_synced_at else None,
        last_sync_status=s.last_sync_status,
        last_sync_error=served_sync_text(
            LAST_SYNC_ERROR, s.last_sync_error, can_manage=can_manage
        ),
        last_sync_documents=s.last_sync_documents,
        created_at=s.created_at.isoformat(),
        updated_at=s.updated_at.isoformat(),
    )
