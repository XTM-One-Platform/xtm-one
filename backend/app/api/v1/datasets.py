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

"""Dataset API routes – CRUD, entry management, export/import, HuggingFace."""

import asyncio
import copy
import hashlib
import io
import json
import logging
import uuid
import zipfile
from collections.abc import Iterable, Set
from datetime import UTC, datetime, timedelta
from itertools import batched
from types import SimpleNamespace
from typing import Any, NamedTuple, cast

import httpx
from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ValidationError
from sqlalchemy import ColumnElement, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute

from app.api.deps import require_admin, require_agentic_ai, require_ai_enabled
from app.api.v1.helpers import (
    ENTITY_LIST_SCOPE_DESCRIPTION,
    EntityListScope,
    apply_group_share_fields,
    apply_user_share_fields,
    get_user_agent,
    get_user_group_ids,
    handle_company_managed_toggle,
    list_scope_admin_bypass,
    parse_path_id,
    parse_uuid,
    require_admin_for_company_managed,
    user_visible_query,
    validation_detail,
    wants_access_restriction,
)
from app.api.v1.schemas.dataset import (
    CreateDatasetRequest,
    DatasetDetailResponse,
    DatasetEntryRequest,
    DatasetEntryResponse,
    DatasetFamilyConflict,
    DatasetPackageEntry,
    DatasetPackageFile,
    DatasetResponse,
    DatasetVersionDetailResponse,
    DatasetVersionEntryResponse,
    DatasetVersionResponse,
    HFImportRequest,
    LinkedDatasetResponse,
    UpdateDatasetRequest,
)
from app.core.archive_reading import ArchiveBudget, ArchiveTooLargeError
from app.core.database import get_db
from app.core.utils import fit_copy_name, safe_uuid
from app.models.dataset import Dataset, DatasetEntry, EvaluationDataset
from app.models.dataset_version import DatasetVersion
from app.models.evaluation import Evaluation
from app.models.user import User
from app.services.audit import log_audit
from app.services.evaluation import dataset_versioning
from app.services.evaluation.dataset_conformance import (
    first_conformance_failure,
    needs_expected_answers,
)
from app.services.evaluation.dataset_family_compatibility import (
    check_family_compatibility,
)
from app.services.evaluation.metric_package.families import (
    CLASSIFICATION,
    STIX_BUNDLE,
    STRUCTURED_JSON,
    family_is_valid,
)
from app.services.evaluation.metric_package.quality_adapter import (
    normalize_label,
    stix_reference_error,
)
from app.services.evaluation.scorers.classification_vocabulary import (
    validate_reference_labels,
)
from app.services.evaluation.scorers.structured_output import validate_output_schema
from app.services.file_storage import objects_deleted_on_failure

logger = logging.getLogger(__name__)
router = APIRouter()

HF_API_BASE = "https://datasets-server.huggingface.co"
MAX_ZIP_SIZE = 500 * 1024 * 1024  # 500 MB
#: What a package's members may expand to in all once uncompressed, spent
#: through ``ArchiveBudget`` as they are read.
MAX_UNCOMPRESSED_SIZE = 2 * 1024 * 1024 * 1024  # 2 GB
#: What one JSON member of a package may expand to: its parse holds a copy of
#: it and the object graph built from it. Room for a manifest's entries, which no row
#: limit bounds, and for an evaluation run's results with their traces.
MAX_JSON_MEMBER_SIZE = 256 * 1024 * 1024  # 256 MB


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _compute_content_hash(entries: list[DatasetEntry]) -> str:
    rows = sorted(
        [
            {
                "input_prompt": e.input_prompt or "",
                "expected_answer": e.expected_answer or "",
                "evaluation_prompt": e.evaluation_prompt or "",
            }
            for e in entries
        ],
        key=lambda r: r["input_prompt"],
    )
    return hashlib.sha256(
        json.dumps(rows, sort_keys=True, ensure_ascii=True).encode()
    ).hexdigest()


async def _get_user_dataset(
    db: AsyncSession,
    user_id: uuid.UUID,
    dataset_id: str,
    user_group_ids: list[str] | None = None,
    *,
    is_admin: bool = False,
) -> Dataset:
    did = parse_path_id(dataset_id, not_found_detail="Dataset not found")
    query = user_visible_query(
        Dataset, user_id, user_group_ids, is_admin=is_admin
    ).where(Dataset.id == did)
    result = await db.execute(query)
    dataset = result.scalar_one_or_none()
    if not dataset:
        raise HTTPException(status_code=404, detail="Dataset not found")
    return dataset


async def _get_user_dataset_entry_with_dataset(
    db: AsyncSession,
    user: User,
    entry_id: str,
) -> tuple[DatasetEntry, Dataset]:
    eid = parse_path_id(entry_id, not_found_detail="Dataset entry not found")
    user_group_ids = await get_user_group_ids(db, user.id)
    visible_dataset_ids = user_visible_query(
        Dataset, user.id, user_group_ids, is_admin=user.is_admin
    ).with_only_columns(Dataset.id)
    result = await db.execute(
        select(DatasetEntry, Dataset)
        .join(Dataset, Dataset.id == DatasetEntry.dataset_id)
        .where(
            DatasetEntry.id == eid,
            Dataset.id.in_(visible_dataset_ids),
        )
    )
    row = result.one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="Dataset entry not found")
    return row[0], row[1]


def _assert_output_schema_allowed(family: str | None, output_schema: dict | None) -> None:
    """A declared answer shape only applies to STRUCTURED_JSON.

    The per-field scoring it feeds (``json.field_accuracy``) is specific to that family:
    one saved on a STIX/PROSE/CLASSIFICATION dataset would silently do nothing at run
    time. Mirrors ``_apply_dataset_label_vocabulary``'s family gate for CLASSIFICATION.
    """
    if output_schema and family != STRUCTURED_JSON:
        raise HTTPException(
            status_code=422,
            detail="output_schema can only be set on a STRUCTURED_JSON dataset.",
        )


async def _apply_dataset_output_schema(
    db: AsyncSession, dataset: Dataset, output_schema: dict
) -> None:
    """Validate and set the dataset's declared answer shape.

    Called once the request's family is final (see ``update_dataset``), so the
    STRUCTURED_JSON gate sees a same-request family change.

    Checks, in order: the family gate (an author fixing a family mismatch should not also
    fight a ReDoS or shape error on a schema they are about to discard); the wrapper shape
    and ``check_schema``, before the schema is used to check anything; the ReDoS
    pre-screen, before any pattern is compiled against real input; then whether the
    ground truth holds to it.

    An empty object clears the declaration: ``None`` already means "leave this field
    alone" on a partial update.
    """
    if not output_schema:
        dataset.output_schema = None
        return

    _assert_output_schema_allowed(dataset.family, output_schema)

    schema_err = validate_output_schema(output_schema)
    if schema_err:
        raise HTTPException(status_code=422, detail=schema_err)

    # Only a `json_schema` has expected-answer conformance to check: legacy `regex`/
    # `tool_sequence` rows describe the agent's output, not a stored answer's shape.
    # Asking first spares such a save a full read of every entry's answer text.
    if needs_expected_answers(output_schema):
        rows = (
            await db.execute(
                select(DatasetEntry.id, DatasetEntry.expected_answer, DatasetEntry.output_schema)
                .where(DatasetEntry.dataset_id == dataset.id)
                .order_by(DatasetEntry.position)
            )
        ).all()
        # An entry declaring its own shape is held to that one, not this.
        failure = first_conformance_failure(
            output_schema,
            [(str(entry_id), answer) for entry_id, answer, own in rows if not own],
        )
        if failure is not None:
            raise HTTPException(status_code=422, detail=failure.as_detail())

    dataset.output_schema = output_schema


def _validate_family(family: str | None) -> None:
    """Reject an unrecognized family value."""
    if family is not None and not family_is_valid(family):
        raise HTTPException(status_code=422, detail=f"Unknown dataset family: {family}")



def _apply_dataset_label_vocabulary(
    dataset: Dataset, family: str | None, label_vocabulary: list[str] | None
) -> None:
    """Set the CLASSIFICATION-only declared vocabulary.

    A vocabulary on any other family is rejected rather than ignored: it would sit
    unused on the row, indistinguishable from a declaration that has not taken effect.

    Omitted (``None``) means "no change"; the caller handles an explicit ``null``
    ("clear"). An empty list (``[]``) is neither: it breaks "non-empty when provided"
    and is rejected, so a client that sends an empty array by accident does not think
    it declared a vocabulary.
    """
    if label_vocabulary is None:
        return
    effective_family = family if family is not None else dataset.family
    if not label_vocabulary:
        raise HTTPException(
            status_code=422,
            detail="label_vocabulary must be non-empty; send null to clear it.",
        )
    if effective_family != CLASSIFICATION:
        raise HTTPException(
            status_code=422,
            detail="label_vocabulary can only be set on a CLASSIFICATION dataset.",
        )
    cleaned = [label.strip() for label in label_vocabulary]
    # Stored as typed: labels that differ only by case or by the marks around them
    # (``Phishing``, ``phishing.``) are one label when scored, so a vocabulary
    # declaring both is harmless and an older export keeps importing. A label made only of
    # marks could never match anything.
    if any(not normalize_label(label) for label in cleaned):
        raise HTTPException(
            status_code=422,
            detail="label_vocabulary entries must hold more than whitespace and punctuation.",
        )
    dataset.label_vocabulary = cleaned


async def _dataset_family_conflicts(
    db: AsyncSession, dataset: Dataset
) -> list[DatasetFamilyConflict]:
    """Re-check every evaluation already linked to ``dataset``.

    Run when the dataset's family changes: every existing link is re-checked and its
    conflicts reported, rather than left stale until an unrelated link attempt
    surfaces them.
    """
    rows = (
        await db.execute(
            select(Evaluation.id, Evaluation.name, Evaluation.family)
            .join(EvaluationDataset, EvaluationDataset.evaluation_id == Evaluation.id)
            .where(EvaluationDataset.dataset_id == dataset.id)
        )
    ).all()
    conflicts: list[DatasetFamilyConflict] = []
    for eval_id, eval_name, eval_family in rows:
        message = check_family_compatibility(dataset.family, eval_family)
        if message is not None:
            conflicts.append(
                DatasetFamilyConflict(
                    evaluation_id=str(eval_id),
                    evaluation_name=eval_name,
                    message=message,
                )
            )
    return conflicts


def _assert_entry_matches_dataset_vocabulary(
    dataset: Dataset, expected_answer: str | None
) -> None:
    """Validate an entry's reference answer against the dataset's declared
    CLASSIFICATION vocabulary, when one exists.

    The CLASSIFICATION counterpart of ``_assert_references_scorable``.
    """
    if not dataset.label_vocabulary:
        return
    label_err = validate_reference_labels(
        frozenset(dataset.label_vocabulary), expected_answer
    )
    if label_err:
        raise HTTPException(status_code=422, detail=label_err)


def _assert_references_scorable(
    family: str | None,
    dataset_schema: dict | None,
    entries: Iterable[tuple[str, str | None, dict | None]],
) -> None:
    """Refuse an expected answer the scorer could not read.

    A STIX reference must be a JSON object; a JSON one must hold to its effective schema,
    the entry's own when it declares one, else the dataset's. Without this check the run
    would leave the entry out for a reason unrelated to the agent. An empty answer is a
    draft and passes.
    """
    for entry_id, expected_answer, entry_schema in entries:
        if family == STIX_BUNDLE and (error := stix_reference_error(expected_answer)):
            raise HTTPException(
                status_code=422,
                detail=f"Expected answer of entry {entry_id} cannot be scored: {error}",
            )
        failure = first_conformance_failure(
            entry_schema or dataset_schema, [(entry_id, expected_answer)]
        )
        if failure is not None:
            raise HTTPException(status_code=422, detail=failure.as_detail())


async def _build_dataset_response(
    db: AsyncSession, dataset: Dataset, include_entries: bool = False
) -> dict:
    entry_count_result = await db.execute(
        select(func.count()).where(DatasetEntry.dataset_id == dataset.id)
    )
    entry_count = entry_count_result.scalar() or 0

    evaluation_count_result = await db.execute(
        select(func.count()).where(EvaluationDataset.dataset_id == dataset.id)
    )
    evaluation_count = evaluation_count_result.scalar() or 0

    resp: dict = {
        "id": str(dataset.id),
        "user_id": str(dataset.user_id) if dataset.user_id else None,
        "name": dataset.name,
        "description": dataset.description,
        "evaluation_instructions": dataset.evaluation_instructions,
        # As stored, byte for byte: neither re-serialised through a model nor
        # key-sorted, so an author recognises the document they submitted.
        "output_schema": dataset.output_schema,
        # A dataset without a family is a surfaced problem, not a passive absence,
        # hence the computed flag.
        "family": dataset.family,
        "label_vocabulary": dataset.label_vocabulary,
        "needs_family": dataset.family is None,
        "content_hash": dataset.content_hash,
        "current_version": dataset.current_version or 0,
        "is_company_managed": dataset.is_company_managed,
        "is_read_only": dataset.is_read_only,
        "shared_group_ids": dataset.shared_group_ids,
        "shared_group_permissions": dataset.shared_group_permissions,
        "shared_user_ids": dataset.shared_user_ids,
        "shared_user_permissions": dataset.shared_user_permissions,
        "access_restricted": dataset.access_restricted,
        "evaluation_count": evaluation_count,
        "entry_count": entry_count,
        "created_at": dataset.created_at.isoformat(),
        "updated_at": dataset.updated_at.isoformat(),
    }

    if include_entries:
        entries_result = await db.execute(
            select(DatasetEntry)
            .where(DatasetEntry.dataset_id == dataset.id)
            .order_by(DatasetEntry.position)
        )
        entries = entries_result.scalars().all()
        resp["entries"] = [await _build_entry_response(e, db) for e in entries]

    return resp


def _stored_file_uuids(file_ids: list | None) -> list[uuid.UUID]:
    """The attachment ids an entry's ``file_ids`` names, malformed values dropped.

    ``file_ids`` holds whatever the entry create / update body sent, so a value
    that is not a UUID names no attachment, exactly like a well-formed id nobody
    issued: it is skipped, never parsed with a bare ``uuid.UUID`` that would
    turn every later read, delete or export of the dataset into a 500.
    """
    return [parsed for fid in file_ids or [] if (parsed := safe_uuid(fid)) is not None]


async def _build_entry_response(
    entry: DatasetEntry, db: AsyncSession | None = None
) -> dict:
    files_meta: list[dict] | None = None
    if db and entry.file_ids:
        from app.models.file_attachment import FileAttachment

        # A list whatever the stored ids are: an entry naming only malformed
        # ids reads like one naming ids nobody issued, never ``null``.
        files_meta = []
        fids = _stored_file_uuids(entry.file_ids)
        if fids:
            result = await db.execute(
                select(FileAttachment).where(FileAttachment.id.in_(fids))
            )
            atts = {str(a.id): a for a in result.scalars().all()}
            files_meta = [
                {
                    "id": str(a.id),
                    "filename": a.filename,
                    "size": a.size,
                    "content_type": a.content_type,
                }
                for fid in entry.file_ids
                if (a := atts.get(fid))
            ]
    return {
        "id": str(entry.id),
        "dataset_id": str(entry.dataset_id),
        "position": entry.position,
        "description": entry.description,
        "input_prompt": entry.input_prompt or "",
        "expected_answer": entry.expected_answer,
        "evaluation_prompt": entry.evaluation_prompt,
        "file_ids": entry.file_ids,
        "output_schema": entry.output_schema,
        "files": files_meta,
        "created_at": entry.created_at.isoformat(),
    }


# ---------------------------------------------------------------------------
# HuggingFace proxy (read-only, shared with evaluation-level import)
# ---------------------------------------------------------------------------


@router.get("/hf-splits")
async def hf_splits(
    dataset: str = Query(..., description="HuggingFace dataset ID"),
    _user: User = Depends(require_admin),
):
    """List configs and splits for a HuggingFace dataset, including num_rows per split."""
    import asyncio

    async with httpx.AsyncClient(timeout=30) as client:
        splits_resp, info_resp = await asyncio.gather(
            client.get(f"{HF_API_BASE}/splits", params={"dataset": dataset}),
            client.get(f"{HF_API_BASE}/info", params={"dataset": dataset}),
            return_exceptions=True,
        )

    if isinstance(splits_resp, BaseException):
        detail = str(splits_resp)[:200]
        raise HTTPException(status_code=502, detail=f"HuggingFace API error: {detail}")
    splits_resp = cast(httpx.Response, splits_resp)
    if splits_resp.status_code != 200:
        detail = getattr(splits_resp, "text", str(splits_resp))[:200]
        raise HTTPException(status_code=502, detail=f"HuggingFace API error: {detail}")

    splits_data = splits_resp.json()

    # Merge num_rows from /info into each split (best-effort: ignored if /info fails).
    # /info exposes num_examples which matches the count shown on the HuggingFace dataset page,
    # whereas /size returns parquet-converted row counts which can differ.
    if not isinstance(info_resp, BaseException):
        info_resp = cast(httpx.Response, info_resp)
    if not isinstance(info_resp, BaseException) and info_resp.status_code == 200:
        num_examples_by_key: dict[tuple[str, str], int | None] = {}
        for config_name, config_info in info_resp.json().get("dataset_info", {}).items():
            for split_name, split_info in config_info.get("splits", {}).items():
                num_examples_by_key[(config_name, split_name)] = split_info.get("num_examples")
        for s in splits_data.get("splits", []):
            s["num_rows"] = num_examples_by_key.get((s["config"], s["split"]))

    return splits_data


@router.get("/hf-preview")
async def hf_preview(
    dataset: str = Query(...),
    config: str | None = Query(None),
    split: str = Query("train"),
    _user: User = Depends(require_admin),
):
    """Return column schema and sample rows (up to 5) for a HuggingFace split."""
    params: dict = {"dataset": dataset, "split": split}
    if config:
        params["config"] = config
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            f"{HF_API_BASE}/first-rows",
            params=params,
        )
    if resp.status_code != 200:
        raise HTTPException(
            status_code=resp.status_code,
            detail=f"HuggingFace API error: {resp.text[:200]}",
        )
    data = resp.json()
    # Return first 5 rows + column schema
    rows = (data.get("rows") or [])[:5]
    features = data.get("features") or []
    return {"rows": [r.get("row", r) for r in rows], "features": features}


# ---------------------------------------------------------------------------
# Dataset CRUD
# ---------------------------------------------------------------------------


@router.get("", response_model=list[DatasetResponse])
async def list_datasets(
    search: str | None = Query(None, max_length=200),
    scope: EntityListScope = Query("visible", description=ENTITY_LIST_SCOPE_DESCRIPTION),
    family: str | None = Query(
        None,
        description=(
            "Filter to datasets of exactly this family, plus family-less datasets "
            "-- the same 'matching family or needs family' set the "
            "evaluation dataset picker offers, so a family-less dataset is never "
            "silently hidden from a filtered list before its owner has fixed it."
        ),
    ),
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """List datasets visible to the caller.

    ``scope=all`` lists every dataset (what a detail page links to).
    """
    user_group_ids = await get_user_group_ids(db, user.id)
    query = user_visible_query(
        Dataset, user.id, user_group_ids, is_admin=list_scope_admin_bypass(scope, user)
    )

    if search:
        q = f"%{search.strip()}%"
        query = query.where(
            or_(Dataset.name.ilike(q), Dataset.description.ilike(q))
        )
    if family is not None:
        _validate_family(family)
        query = query.where(or_(Dataset.family == family, Dataset.family.is_(None)))

    query = query.order_by(Dataset.updated_at.desc())
    result = await db.execute(query)
    datasets = result.scalars().all()
    return [await _build_dataset_response(db, ds) for ds in datasets]


@router.post("", response_model=DatasetDetailResponse, status_code=status.HTTP_201_CREATED)
async def create_dataset(
    body: CreateDatasetRequest,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    _agentic_gate: None = Depends(require_agentic_ai),
):
    """Create a new dataset, optionally with inline entries."""
    _validate_family(body.family)
    _assert_output_schema_allowed(body.family, body.output_schema)

    dataset = Dataset(
        user_id=user.id,
        is_company_managed=body.is_company_managed,
        is_read_only=body.is_read_only,
        name=body.name,
        description=body.description,
        evaluation_instructions=body.evaluation_instructions,
        output_schema=body.output_schema or None,
        family=body.family,
    )
    # The CLASSIFICATION-only and non-blank vocabulary checks run before any per-entry
    # label matching, so a vocabulary sent on the wrong family (or with blank labels)
    # reports its own error rather than "Label(s) not in dataset vocabulary".
    _apply_dataset_label_vocabulary(dataset, body.family, body.label_vocabulary)

    if body.output_schema:
        schema_err = validate_output_schema(body.output_schema)
        if schema_err:
            raise HTTPException(status_code=422, detail=schema_err)

    if body.entries:
        for i, entry in enumerate(body.entries):
            _assert_output_schema_allowed(body.family, entry.output_schema)
            if entry.output_schema:
                entry_schema_err = validate_output_schema(entry.output_schema)
                if entry_schema_err:
                    raise HTTPException(
                        status_code=422, detail=f"(entry {i}) {entry_schema_err}"
                    )
        # Inline entries are checked here, not through ``_apply_dataset_output_schema``:
        # the dataset has no rows yet, so that helper's query would find nothing and
        # pass a contradiction.
        _assert_references_scorable(
            body.family,
            body.output_schema or None,
            [
                (f"(entry {i})", e.expected_answer, e.output_schema)
                for i, e in enumerate(body.entries)
            ],
        )

    if dataset.label_vocabulary and body.entries:
        for i, entry in enumerate(body.entries):
            label_err = validate_reference_labels(
                frozenset(dataset.label_vocabulary), entry.expected_answer
            )
            if label_err:
                raise HTTPException(
                    status_code=422, detail=f"(entry {i}) {label_err}"
                )
    await apply_group_share_fields(db, dataset, body, partial=False)
    await apply_user_share_fields(db, dataset, body, partial=False)
    db.add(dataset)
    await db.flush()

    if body.entries:
        for i, entry in enumerate(body.entries):
            db.add(
                DatasetEntry(
                    dataset_id=dataset.id,
                    position=i,
                    description=entry.description,
                    input_prompt=entry.input_prompt,
                    expected_answer=entry.expected_answer,
                    evaluation_prompt=entry.evaluation_prompt,
                    file_ids=entry.file_ids,
                    output_schema=entry.output_schema,
                )
            )

    await log_audit(
        db,
        user=user,
        action="create",
        entity_type="dataset",
        entity_id=str(dataset.id),
        entity_name=dataset.name,
        summary=f"Created dataset '{dataset.name}'",
    )
    await db.flush()
    if body.entries:
        await dataset_versioning.bump_version(db, dataset)
    await db.commit()
    return await _build_dataset_response(db, dataset, include_entries=True)


@router.get("/{dataset_id}", response_model=DatasetDetailResponse)
async def get_dataset(
    dataset_id: str,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Get dataset detail with all entries."""
    user_group_ids = await get_user_group_ids(db, user.id)
    dataset = await _get_user_dataset(
        db, user.id, dataset_id, user_group_ids, is_admin=user.is_admin
    )
    return await _build_dataset_response(db, dataset, include_entries=True)


@router.get("/{dataset_id}/seed-ground-truth")
async def seed_ground_truth(
    dataset_id: str,
    agent_id: str = Query(..., description="Agent to seed family-appropriate ground-truth material from"),
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    _ai_gate: None = Depends(require_ai_enabled),
):
    """Preview a dataset's family-appropriate ground-truth material seeded from an
    agent's own declaration.

    A preview only: nothing is written and no link to the source agent is kept; the
    caller saves it through the normal update path. Only STRUCTURED_JSON has an
    agent-side declaration to seed from (``Agent.output_schema``); every other family
    reports "nothing found to seed" rather than fabricating material.
    """
    user_group_ids = await get_user_group_ids(db, user.id)
    dataset = await _get_user_dataset(
        db, user.id, dataset_id, user_group_ids, is_admin=user.is_admin
    )

    aid = parse_uuid(agent_id, label="Agent ID")
    agent = await get_user_agent(
        db, user.id, str(aid), user_group_ids, is_admin=user.is_admin
    )

    if dataset.family == STRUCTURED_JSON and agent.output_schema:
        # Deep copy: editing the agent afterwards must never change what was seeded.
        return {
            "found": True,
            "family": STRUCTURED_JSON,
            "output_schema": copy.deepcopy(agent.output_schema),
        }

    return {
        "found": False,
        "family": dataset.family,
        "message": (
            f"Agent '{agent.name}' has no declaration to seed for family "
            f"'{dataset.family}'."
            if dataset.family
            else "This dataset has no family set, so there is nothing to seed."
        ),
    }


#: The update fields a read-only dataset still takes. Read-only locks the content,
#: not who can open the dataset, as on a read-only knowledge base: the sharing fields
#: change under the same rules as on any dataset. Any other field sent, a field added
#: later included, is refused until the same request unlocks it.
_READ_ONLY_EDITABLE_FIELDS = frozenset(
    {
        "is_read_only",
        "shared_group_ids",
        "shared_group_permissions",
        "shared_user_ids",
        "shared_user_permissions",
        "access_restricted",
        "is_company_managed",
    }
)


@router.put("/{dataset_id}", response_model=DatasetResponse)
async def update_dataset(
    dataset_id: str,
    body: UpdateDatasetRequest,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    _ai_gate: None = Depends(require_ai_enabled),
):
    """Update dataset metadata."""
    user_group_ids = await get_user_group_ids(db, user.id)
    dataset = await _get_user_dataset(
        db, user.id, dataset_id, user_group_ids, is_admin=user.is_admin
    )
    require_admin_for_company_managed(dataset, user)

    # ``model_fields_set``, not "is not None": an explicit ``null`` sent to clear
    # ``family``/``label_vocabulary`` is a mutation even though it reads as ``None``.
    fields_set = body.model_fields_set
    if dataset.is_read_only:
        # A request that also unlocks the dataset (``is_read_only: false``) is the
        # unlock itself: the save form always sends the toggle with the other (possibly
        # unchanged) fields in one PUT. Only block them when the dataset stays read-only.
        unlocking = body.is_read_only is False
        locked_fields = fields_set - _READ_ONLY_EDITABLE_FIELDS
        if locked_fields and not unlocking:
            raise HTTPException(
                status_code=403,
                detail="This dataset is read-only. Disable read-only before modifying it.",
            )
        if body.is_read_only is not None and body.is_read_only is True:
            raise HTTPException(
                status_code=403,
                detail="This dataset is already read-only",
            )

    # What a version snapshots besides the entries (``dataset_versioning.bump_version``).
    previous_shape = dataset_versioning.version_shape(dataset)

    if body.name is not None:
        dataset.name = body.name
    if body.description is not None:
        dataset.description = body.description or None
    if body.evaluation_instructions is not None:
        dataset.evaluation_instructions = body.evaluation_instructions or None
    # ``family``/``label_vocabulary``, unlike every other field here: omitted
    # (`"family" not in fields_set`) means "leave alone", explicit `null` (`"family" in
    # fields_set` but `body.family is None`) means "clear it". `body.family is not None`
    # alone cannot tell the two apart.
    previous_family = dataset.family
    if "family" in fields_set:
        if body.family is not None:
            _validate_family(body.family)
            dataset.family = body.family
        else:
            dataset.family = None
        if dataset.family == STIX_BUNDLE and previous_family != STIX_BUNDLE:
            # The entries were written under another family: each must still be readable
            # as a STIX reference. An unchanged family checks nothing, so a save
            # never fails on an entry written before the check.
            entry_rows = (
                await db.execute(
                    select(DatasetEntry.id, DatasetEntry.expected_answer).where(
                        DatasetEntry.dataset_id == dataset.id
                    )
                )
            ).all()
            _assert_references_scorable(
                STIX_BUNDLE, None, [(str(entry_id), answer, None) for entry_id, answer in entry_rows]
            )
    if "label_vocabulary" in fields_set:
        if body.label_vocabulary is not None:
            _apply_dataset_label_vocabulary(dataset, body.family, body.label_vocabulary)
        else:
            dataset.label_vocabulary = None
        if dataset.label_vocabulary:
            # Re-check every existing entry: a narrowed vocabulary must never
            # quietly invalidate a label an existing reference answer relies on.
            entry_rows = (
                await db.execute(
                    select(DatasetEntry.id, DatasetEntry.expected_answer).where(
                        DatasetEntry.dataset_id == dataset.id
                    )
                )
            ).all()
            vocab = frozenset(dataset.label_vocabulary)
            for entry_id, expected_answer in entry_rows:
                label_err = validate_reference_labels(vocab, expected_answer)
                if label_err:
                    raise HTTPException(
                        status_code=422, detail=f"(entry {entry_id}) {label_err}"
                    )
    elif (
        "family" in fields_set
        and dataset.family != CLASSIFICATION
        and dataset.label_vocabulary
    ):
        # This request moved ``family`` away from CLASSIFICATION with no
        # ``label_vocabulary`` instruction: clear the vocabulary rather than leave it on
        # a family it does not apply to. Gated on ``"family" in fields_set`` so an
        # unrelated update to a family-less dataset carrying a vocabulary cannot wipe it.
        dataset.label_vocabulary = None
    # Applied after the family block so ``_apply_dataset_output_schema``'s
    # STRUCTURED_JSON gate sees this request's family, not the stale one.
    if body.output_schema is not None:
        await _apply_dataset_output_schema(db, dataset, body.output_schema)
    elif (
        "family" in fields_set
        and dataset.family != STRUCTURED_JSON
        and dataset.output_schema
    ):
        # Same rule and gate as the label_vocabulary clear above: drop a schema left on
        # a family it does not apply to, never on an update that did not touch
        # ``family`` (e.g. a family-less legacy dataset).
        dataset.output_schema = None
    if body.is_read_only is not None:
        if not user.is_admin:
            raise HTTPException(status_code=403, detail="Only admins can set read-only")
        dataset.is_read_only = body.is_read_only
    await apply_group_share_fields(
        db, dataset, body, partial=True, null_keeps_stored=True
    )
    # Agents never bind a dataset by id: there is no reference to sweep when it
    # goes private.
    await handle_company_managed_toggle(
        db,
        user,
        dataset,
        body.is_company_managed,
        agent_field=None,
        keep_grants=wants_access_restriction(body, dataset),
    )

    # Per-user sharing — after the company-managed toggle, since
    # ``access_restricted`` is only meaningful relative to it.
    await apply_user_share_fields(db, dataset, body, partial=True)

    # A family change is re-checked against every already-linked evaluation.
    # Conflicts are reported, never auto-resolved: the links are left untouched.
    family_conflicts = (
        await _dataset_family_conflicts(db, dataset) if body.family is not None else None
    )

    # Runs read the vocabulary and answer shape from their pinned version, so a change
    # to either is a new version, as an entry write is. A dataset
    # with no version yet is snapshotted as it stands when first run.
    if dataset.current_version and previous_shape != dataset_versioning.version_shape(dataset):
        await dataset_versioning.bump_version(db, dataset)

    await log_audit(
        db,
        user=user,
        action="update",
        entity_type="dataset",
        entity_id=str(dataset.id),
        entity_name=dataset.name,
        summary=f"Updated dataset '{dataset.name}'",
    )
    await db.commit()
    resp = await _build_dataset_response(db, dataset)
    if family_conflicts:
        resp["family_conflicts"] = family_conflicts
    return resp


@router.delete("/{dataset_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_dataset(
    dataset_id: str,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    _ai_gate: None = Depends(require_ai_enabled),
):
    """Delete a dataset (cascades to entries)."""
    user_group_ids = await get_user_group_ids(db, user.id)
    dataset = await _get_user_dataset(
        db, user.id, dataset_id, user_group_ids, is_admin=user.is_admin
    )
    require_admin_for_company_managed(dataset, user)
    if dataset.is_read_only:
        raise HTTPException(
            status_code=403,
            detail="This dataset is read-only. Disable read-only before deleting it.",
        )

    # The entries' files: only the dataset uploads that nothing outside the
    # dataset uses go with it (``_delete_dataset_files_if_unreferenced``).
    from sqlalchemy import Text
    from sqlalchemy import cast as sa_cast

    from app.services.file_cleanup import purge_storage_objects

    entries_result = await db.execute(
        select(DatasetEntry.file_ids).where(
            DatasetEntry.dataset_id == dataset.id,
            DatasetEntry.file_ids.is_not(None),
            sa_cast(DatasetEntry.file_ids, Text).not_in(("null", "[]")),
        )
    )
    file_uuids: set[uuid.UUID] = set()
    for (fids,) in entries_result.all():
        file_uuids.update(_stored_file_uuids(fids))

    await log_audit(
        db,
        user=user,
        action="delete",
        entity_type="dataset",
        entity_id=str(dataset.id),
        entity_name=dataset.name,
        summary=f"Deleted dataset '{dataset.name}'",
    )
    await db.delete(dataset)
    await db.flush()  # the dataset and its entries go first, the file rows after
    keys = await _delete_dataset_files_if_unreferenced(db, file_uuids)
    await db.commit()
    await purge_storage_objects(keys)


# ---------------------------------------------------------------------------
# Entry management
# ---------------------------------------------------------------------------


def _check_entry_access(dataset: Dataset) -> None:
    if dataset.is_read_only:
        raise HTTPException(
            status_code=403, detail="This dataset is read-only"
        )


async def _lock_for_append(
    db: AsyncSession, parent: type[Dataset] | type[Evaluation], parent_id: uuid.UUID
) -> None:
    """Hold *parent*'s row until the commit, so appends under it take turns.

    An append reads ``max(position)`` and inserts the next one: two at once would
    read the same maximum and collide on the unique position. ``NO KEY UPDATE``
    does not block the key-share lock an inserted child's foreign key takes.
    """
    # ``key_share=True`` is ``FOR NO KEY UPDATE`` on PostgreSQL.
    await db.execute(select(parent.id).where(parent.id == parent_id).with_for_update(key_share=True))


@router.post(
    "/{dataset_id}/entries",
    response_model=DatasetEntryResponse,
    status_code=status.HTTP_201_CREATED,
)
async def add_entry(
    dataset_id: str,
    body: DatasetEntryRequest,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    _ai_gate: None = Depends(require_ai_enabled),
):
    """Add a single entry to a dataset."""
    user_group_ids = await get_user_group_ids(db, user.id)
    dataset = await _get_user_dataset(
        db, user.id, dataset_id, user_group_ids, is_admin=user.is_admin
    )
    require_admin_for_company_managed(dataset, user)
    _check_entry_access(dataset)
    _assert_output_schema_allowed(dataset.family, body.output_schema)
    schema_err = validate_output_schema(body.output_schema)
    if schema_err:
        raise HTTPException(status_code=422, detail=schema_err)
    _assert_references_scorable(
        dataset.family, dataset.output_schema, [("(new)", body.expected_answer, body.output_schema)]
    )
    _assert_entry_matches_dataset_vocabulary(dataset, body.expected_answer)

    await _lock_for_append(db, Dataset, dataset.id)
    # ``coalesce``, not ``or -1``: a maximum of 0 is not an empty list.
    max_pos = (
        await db.execute(
            select(func.coalesce(func.max(DatasetEntry.position), -1)).where(
                DatasetEntry.dataset_id == dataset.id
            )
        )
    ).scalar_one()

    entry = DatasetEntry(
        dataset_id=dataset.id,
        position=max_pos + 1,
        description=body.description,
        input_prompt=body.input_prompt,
        expected_answer=body.expected_answer,
        evaluation_prompt=body.evaluation_prompt,
        file_ids=body.file_ids,
        output_schema=body.output_schema,
    )
    db.add(entry)
    # Invalidate content hash since entries changed
    dataset.content_hash = None
    await db.flush()
    await dataset_versioning.bump_version(db, dataset)
    await db.commit()
    return await _build_entry_response(entry, db)


@router.put("/entries/{entry_id}", response_model=DatasetEntryResponse)
async def update_entry(
    entry_id: str,
    body: DatasetEntryRequest,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    _ai_gate: None = Depends(require_ai_enabled),
):
    """Update a single dataset entry."""
    entry, dataset = await _get_user_dataset_entry_with_dataset(db, user, entry_id)
    require_admin_for_company_managed(dataset, user)
    _check_entry_access(dataset)
    _assert_output_schema_allowed(dataset.family, body.output_schema)
    schema_err = validate_output_schema(body.output_schema)
    if schema_err:
        raise HTTPException(status_code=422, detail=schema_err)
    _assert_references_scorable(
        dataset.family,
        dataset.output_schema,
        [(str(entry.id), body.expected_answer, body.output_schema)],
    )
    _assert_entry_matches_dataset_vocabulary(dataset, body.expected_answer)

    entry.description = body.description
    entry.input_prompt = body.input_prompt
    entry.expected_answer = body.expected_answer
    entry.evaluation_prompt = body.evaluation_prompt
    entry.output_schema = body.output_schema
    if body.file_ids is not None:
        entry.file_ids = body.file_ids
    dataset.content_hash = None
    await db.flush()
    await dataset_versioning.bump_version(db, dataset)
    await db.commit()
    return await _build_entry_response(entry, db)


@router.delete("/entries/{entry_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_entry(
    entry_id: str,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    _ai_gate: None = Depends(require_ai_enabled),
):
    """Delete a dataset entry."""
    entry, dataset = await _get_user_dataset_entry_with_dataset(db, user, entry_id)
    require_admin_for_company_managed(dataset, user)
    _check_entry_access(dataset)

    # Only the entry's dataset uploads that nothing else uses go with it (see
    # ``_delete_dataset_files_if_unreferenced``); a malformed stored id names nothing.
    from app.services.file_cleanup import purge_storage_objects

    file_uuids = _stored_file_uuids(entry.file_ids)

    dataset.content_hash = None
    await db.delete(entry)
    await db.flush()  # the entry goes first, the file rows after
    keys = await _delete_dataset_files_if_unreferenced(db, file_uuids)
    await dataset_versioning.bump_version(db, dataset)
    await db.commit()
    await purge_storage_objects(keys)


@router.post("/entries/{entry_id}/upload")
async def upload_entry_file(
    entry_id: str,
    file: UploadFile = File(...),
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    _ai_gate: None = Depends(require_ai_enabled),
):
    """Upload a file attachment to a dataset entry."""
    from app.models.file_attachment import FileAttachment
    from app.services.file_storage import MAX_UPLOAD_SIZE_BYTES, file_storage, guess_content_type

    entry, dataset = await _get_user_dataset_entry_with_dataset(db, user, entry_id)
    require_admin_for_company_managed(dataset, user)
    _check_entry_access(dataset)

    # One byte past the cap decides, without holding an oversized upload.
    raw_bytes = await file.read(MAX_UPLOAD_SIZE_BYTES + 1)
    if len(raw_bytes) > MAX_UPLOAD_SIZE_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"File too large. Maximum is {MAX_UPLOAD_SIZE_BYTES // (1024 * 1024)} MB.",
        )
    filename = file.filename or "uploaded_file"
    content_type = file.content_type or guess_content_type(filename)

    s3_object_name = await file_storage.upload(
        data=raw_bytes,
        filename=filename,
        content_type=content_type,
        prefix=f"dataset/{entry_id}",
    )
    file_att = FileAttachment(
        user_id=user.id,
        filename=filename,
        content_type=content_type,
        size=len(raw_bytes),
        s3_object_name=s3_object_name,
        source="dataset",
    )
    db.add(file_att)
    await db.flush()

    file_id = str(file_att.id)
    entry.file_ids = [*(entry.file_ids or []), file_id]
    dataset.content_hash = None
    await db.flush()
    await dataset_versioning.bump_version(db, dataset)
    await db.commit()

    return {
        "status": "ok",
        "file_id": file_id,
        "filename": filename,
        "size": len(raw_bytes),
        "content_type": content_type,
    }


@router.delete(
    "/entries/{entry_id}/files/{file_id}", status_code=status.HTTP_204_NO_CONTENT
)
async def remove_entry_file(
    entry_id: str,
    file_id: str,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Remove a file attachment from a dataset entry.

    Acts only on a file the entry in the path actually lists. Any other id -
    malformed, unknown, already removed, or a real file attached elsewhere -
    is answered like a removal already done (204) and changes nothing. The
    entry's reference is always dropped; the stored file goes with it only
    when nothing else still uses it (:func:`_delete_dataset_files_if_unreferenced`).
    """
    from app.services.file_cleanup import purge_storage_objects

    entry, dataset = await _get_user_dataset_entry_with_dataset(db, user, entry_id)
    require_admin_for_company_managed(dataset, user)
    _check_entry_access(dataset)

    file_uuid = safe_uuid(file_id)
    listed = entry.file_ids or []
    kept = [f for f in listed if safe_uuid(f) != file_uuid]
    if file_uuid is None or len(kept) == len(listed):
        return

    entry.file_ids = kept
    dataset.content_hash = None
    await db.flush()  # the entry lets go first, the file row after
    keys = await _delete_dataset_files_if_unreferenced(db, {file_uuid})
    await dataset_versioning.bump_version(db, dataset)
    await db.commit()
    await purge_storage_objects(keys)


#: Ids per ``IN (...)`` list: asyncpg refuses a statement carrying more than
#: 32767 bind parameters, and a dataset may list more files than that.
_FILE_ID_BATCH = 1000

#: A maximal run of the characters a UUID is written with. Every spelling
#: ``uuid.UUID`` reads back - any case, hyphens anywhere or none, braces or a
#: ``urn:uuid:`` prefix around it - puts the id's 32 hex digits inside one such
#: run, in order and with nothing but hyphens between them.
_UUID_RUN_PATTERN = "[0123456789abcdefABCDEF-]{32,}"

#: A message naming a file is written after the file exists, so one older than
#: the oldest candidate cannot name any; the margin absorbs clock skew between
#: the replicas that stamped the two rows.
_MESSAGE_CLOCK_MARGIN = timedelta(hours=1)


def _file_attachment_foreign_keys() -> tuple[InstrumentedAttribute, ...]:
    """Every column holding a foreign key to ``file_attachments.id`` (all ``ON DELETE SET NULL``)."""
    from app.models.runner import RunnerCommand
    from app.models.workset import WorksetItem

    return (WorksetItem.file_attachment_id, RunnerCommand.result_file_id)


def _file_id_text_holders(
    oldest: datetime | None = None,
) -> tuple[tuple[ColumnElement, tuple[ColumnElement, ...]], ...]:
    """Every column that names a ``FileAttachment`` by id as text and reads it back later.

    ``(text, row criteria)`` pairs, cheapest first. A row that will never read
    its ids again is left out on purpose: a finished assignment run is never
    executed again (a retry is a new run, a follow-up carries no file ids), and
    a finished orchestration execution never rebuilds its files. *oldest* is
    the creation time of the oldest file asked about; no message written
    before it can name one.
    """
    from sqlalchemy import Text
    from sqlalchemy import cast as sa_cast

    from app.models.assignment import AssignmentRun
    from app.models.chat import Message
    from app.models.flow_run import FlowRun
    from app.models.orchestration import (
        EXECUTION_STATES,
        TERMINAL_EXECUTION_STATES,
        OrchestrationExecution,
    )
    from app.models.presentation import Presentation, PresentationVersion
    from app.models.template_container import TemplateContainer
    from app.services.assignment_engine import ACTIVE_RUN_STATUSES, QUEUED_RUN_STATUS

    live_states = [state for state in EXECUTION_STATES if state not in TERMINAL_EXECUTION_STATES]
    return (
        # A palette names its logo and mark by id, checked for shape only.
        (
            func.concat_ws(
                " ",
                sa_cast(TemplateContainer.theme_config, Text),
                sa_cast(TemplateContainer.layouts, Text),
                sa_cast(TemplateContainer.extra_metadata, Text),
            ),
            (),
        ),
        # A background task not yet run rebuilds its inherited files from ``file_ids``.
        (
            sa_cast(OrchestrationExecution.payload, Text),
            (OrchestrationExecution.state.in_(live_states),),
        ),
        # A run not yet finished loads ``trigger_payload["file_ids"]`` when it executes or resumes.
        (
            sa_cast(AssignmentRun.trigger_payload, Text),
            (AssignmentRun.status.in_([*ACTIVE_RUN_STATUSES, QUEUED_RUN_STATUS]),),
        ),
        # Another entry's list: evaluations, exports and the entry view resolve it.
        (sa_cast(DatasetEntry.file_ids, Text), (DatasetEntry.file_ids.is_not(None),)),
        # A user message's attachment block, read back into the conversation's
        # history: a steering message names any file its sender owns and binds
        # only one already in the conversation.
        (
            Message.metadata_json,
            (
                Message.role == "user",
                Message.metadata_json.is_not(None),
                *(() if oldest is None else (Message.created_at >= oldest - _MESSAGE_CLOCK_MARGIN,)),
            ),
        ),
        # A flow run's inputs (``trigger_payload["files"]``), reloaded by the runner.
        (sa_cast(FlowRun.trigger_payload, Text), ()),
        # A deck, and every retained revision of one (history preview, restore).
        (sa_cast(Presentation.spec, Text), ()),
        (sa_cast(PresentationVersion.spec, Text), ()),
    )


async def _file_ids_named_in(
    db: AsyncSession,
    text: ColumnElement,
    criteria: tuple[ColumnElement, ...],
    file_ids: Iterable[uuid.UUID],
) -> set[uuid.UUID]:
    """The ids among *file_ids* that *text* names in a row matching *criteria*.

    In any spelling ``uuid.UUID`` reads back - what one ``ILIKE '%<hex>%'`` per
    id finds once hyphens are ignored - in ONE pass over the rows, whatever the
    number of ids: every maximal run of UUID characters, without its hyphens, is
    cut into its 32-character windows and the windows are matched against the
    ids' lower-case ``hex``.
    """
    from sqlalchemy import Text, bindparam
    from sqlalchemy.dialects.postgresql import ARRAY

    by_hex = {file_id.hex: file_id for file_id in file_ids}
    matched = func.array_to_string(func.regexp_matches(text, _UUID_RUN_PATTERN, "g"), "")
    run = func.replace(func.lower(matched), "-", "")
    runs = select(run.label("run")).where(*criteria).distinct().cte("id_runs")
    windows = select(
        func.substr(runs.c.run, func.generate_series(1, func.length(runs.c.run) - 31), 32).label("token")
    ).subquery("id_windows")
    # ``IN (SELECT unnest(...))``, not ``= ANY(...)``: once a pooled connection
    # runs the statement on a generic plan, the array is a parameter and ``ANY``
    # searches it linearly for every window (30x slower on 90 MB of deck
    # history); the subquery is hashed under either plan.
    wanted = select(func.unnest(bindparam("hex_ids", list(by_hex), type_=ARRAY(Text))))
    found = await db.execute(select(windows.c.token).where(windows.c.token.in_(wanted)).distinct())
    return {by_hex[token] for token in found.scalars().all()}


async def _delete_dataset_files_if_unreferenced(
    db: AsyncSession, file_uuids: Iterable[uuid.UUID]
) -> list[str]:
    """Delete the files dataset entries just let go of that nothing else uses.

    The caller - removing an entry file, deleting an entry, deleting a dataset -
    has already flushed that change, so every ``file_ids`` list the scans below
    still read belongs to another entry. The ONE rule behind those three routes:
    never copy it.

    ``file_ids`` is written verbatim by the create and update routes, so an
    entry may list any file. A listed file is deleted only when all of this
    holds; otherwise only the reference went:

    * it is a dataset upload (``source == "dataset"``) bound to no
      conversation, message, run or template - whoever uploaded it, so a file
      only the released entries used is never left behind;
    * no foreign key points at it (:func:`_file_attachment_foreign_keys`) and
      no column naming files by id as text names it
      (:func:`_file_id_text_holders`): another entry, a chat message's
      attachment block, a template palette, a background task not yet run, an
      unfinished assignment run, a flow run, a deck or a retained deck revision.

    A running evaluation is not a holder: once an entry is deleted it can store
    neither that entry's episode (written before its files are read) nor its
    result - both reference the entry, ``NOT NULL``, ``ON DELETE CASCADE``.

    The rows go now; their stored objects only once the caller has committed:
    it hands the returned keys to ``purge_storage_objects``, so a rolled-back
    transaction never leaves a row whose bytes are gone.

    The candidate rows are read ``FOR UPDATE`` and stay locked until the
    caller commits: a concurrent write binding one to a conversation, message,
    run or template, or pointing a foreign key at it (the key check takes
    ``FOR KEY SHARE`` on it), waits for this decision. A reference held as
    text is written without touching the file row, so it is not serialized:
    one committed after the scan of its column began and before the caller's
    commit is not seen, and then names a file that is gone - which every
    reader of those columns handles as a file no longer available. That
    window runs from the scan of the column to the commit: milliseconds on a
    small install, the length of the remaining scans on a large one.

    Queries are bounded, not per file: one locked read and one check per
    foreign key per ``_FILE_ID_BATCH`` ids, then one pass per text column for
    all the candidates, stopping as soon as none is left.
    """
    from app.models.file_attachment import FileAttachment
    from app.services.file_cleanup import delete_file_attachment_rows

    candidates: set[uuid.UUID] = set()
    created: list[datetime | None] = []
    for batch in batched(sorted(set(file_uuids)), _FILE_ID_BATCH, strict=False):
        rows = await db.execute(
            select(FileAttachment.id, FileAttachment.created_at)
            .where(
                FileAttachment.id.in_(batch),
                FileAttachment.source == "dataset",
                FileAttachment.conversation_id.is_(None),
                FileAttachment.message_id.is_(None),
                FileAttachment.assignment_run_id.is_(None),
                FileAttachment.template_container_id.is_(None),
            )
            .order_by(FileAttachment.id)
            .with_for_update()
        )
        for file_id, created_at in rows.all():
            candidates.add(file_id)
            created.append(created_at)
    oldest = min(created) if created and None not in created else None

    for column in _file_attachment_foreign_keys():
        for batch in batched(sorted(candidates), _FILE_ID_BATCH, strict=False):
            used = await db.execute(select(column).where(column.in_(batch)).distinct())
            candidates.difference_update(used.scalars().all())
    for text, criteria in _file_id_text_holders(oldest):
        if not candidates:
            break
        candidates.difference_update(await _file_ids_named_in(db, text, criteria, candidates))

    keys: list[str] = []
    for batch in batched(sorted(candidates), _FILE_ID_BATCH, strict=False):
        keys.extend(await delete_file_attachment_rows(db, id=batch))
    return keys


# ---------------------------------------------------------------------------
# Version history
# ---------------------------------------------------------------------------


@router.get(
    "/{dataset_id}/versions", response_model=list[DatasetVersionResponse]
)
async def list_dataset_versions(
    dataset_id: str,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """List the immutable version snapshots of a dataset, newest first."""
    user_group_ids = await get_user_group_ids(db, user.id)
    dataset = await _get_user_dataset(
        db, user.id, dataset_id, user_group_ids, is_admin=user.is_admin
    )
    result = await db.execute(
        select(DatasetVersion)
        .where(DatasetVersion.dataset_id == dataset.id)
        .order_by(DatasetVersion.version_number.desc())
    )
    versions = result.scalars().all()
    return [
        DatasetVersionResponse(
            id=str(v.id),
            version_number=v.version_number,
            content_hash=v.content_hash,
            entry_count=v.entry_count,
            created_at=v.created_at.isoformat(),
        )
        for v in versions
    ]


@router.get(
    "/{dataset_id}/versions/{version_number}",
    response_model=DatasetVersionDetailResponse,
)
async def get_dataset_version(
    dataset_id: str,
    version_number: int,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Browse a single dataset version snapshot with its frozen entries."""
    user_group_ids = await get_user_group_ids(db, user.id)
    dataset = await _get_user_dataset(
        db, user.id, dataset_id, user_group_ids, is_admin=user.is_admin
    )
    version = await dataset_versioning.get_version(db, dataset.id, version_number)
    if version is None:
        raise HTTPException(status_code=404, detail="Dataset version not found")

    entries = await dataset_versioning.load_version_entries(db, version.id)
    return DatasetVersionDetailResponse(
        id=str(version.id),
        version_number=version.version_number,
        content_hash=version.content_hash,
        entry_count=version.entry_count,
        created_at=version.created_at.isoformat(),
        entries=[
            DatasetVersionEntryResponse(
                id=str(e.id),
                entry_id=str(e.entry_id) if e.entry_id else None,
                position=e.position,
                description=e.description,
                input_prompt=e.input_prompt,
                expected_answer=e.expected_answer,
                evaluation_prompt=e.evaluation_prompt,
                file_ids=e.file_ids,
                output_schema=e.output_schema,
            )
            for e in entries
        ],
    )


# ---------------------------------------------------------------------------
# Export / Import (ZIP)
# ---------------------------------------------------------------------------


@router.get("/{dataset_id}/export")
async def export_dataset(
    dataset_id: str,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Download the dataset as a portable ZIP package."""
    user_group_ids = await get_user_group_ids(db, user.id)
    dataset = await _get_user_dataset(
        db, user.id, dataset_id, user_group_ids, is_admin=user.is_admin
    )

    entries_result = await db.execute(
        select(DatasetEntry)
        .where(DatasetEntry.dataset_id == dataset.id)
        .order_by(DatasetEntry.position)
    )
    entries = entries_result.scalars().all()

    # Resolve file attachments
    all_file_uuids = [file_uuid for e in entries for file_uuid in _stored_file_uuids(e.file_ids)]

    file_map: dict[str, dict] = {}
    file_bytes: dict[str, bytes] = {}
    if all_file_uuids:
        from app.models.file_attachment import FileAttachment
        from app.services.file_storage import file_storage

        fa_result = await db.execute(
            select(FileAttachment).where(FileAttachment.id.in_(all_file_uuids))
        )
        for att in fa_result.scalars().all():
            fid = str(att.id)
            file_map[fid] = {
                "id": fid,
                "filename": att.filename,
                "content_type": att.content_type,
                "size": att.size,
            }
            try:
                file_bytes[fid] = await file_storage.download(att.s3_object_name)
            except Exception:
                logger.warning("Could not download file %s for export", fid)

    # Build manifest
    file_entries = []
    entry_dicts = []
    for e in entries:
        entry_file_ids = []
        for fid in (e.file_ids or []):
            if fid in file_map:
                entry_file_ids.append(fid)
        entry_dicts.append(
            {
                "position": e.position,
                "input_prompt": e.input_prompt or "",
                "expected_answer": e.expected_answer,
                "evaluation_prompt": e.evaluation_prompt,
                "description": e.description,
                "files": entry_file_ids,
                # The per-entry override of the dataset's declared answer shape;
                # without it a re-imported entry loses its override.
                "output_schema": e.output_schema,
            }
        )

    for idx, (fid, meta) in enumerate(file_map.items()):
        path_name = f"f{idx}_{meta['filename']}"
        sha256 = hashlib.sha256(file_bytes.get(fid, b"")).hexdigest()
        file_entries.append(
            {
                "id": f"f{idx}",
                "filename": meta["filename"],
                "content_type": meta["content_type"],
                "size": meta["size"],
                "sha256": sha256,
                "path": f"files/{path_name}",
                "_fid": fid,
                "_path_name": path_name,
            }
        )

    # Replace fid references in entries with f0/f1/... index
    fid_to_idx = {fe["_fid"]: fe["id"] for fe in file_entries}
    for ed in entry_dicts:
        ed["files"] = [fid_to_idx.get(fid, fid) for fid in ed["files"]]

    manifest = {
        # ``format`` stays v1 while ``version`` is 2: the added keys are additive, so an
        # older server still imports a new package, dropping the new fields. A new
        # format name would make new exports unreadable there for no gain.
        "format": "filigran-dataset-v1",
        "version": 2,
        "exported_at": datetime.now(UTC).isoformat(),
        "dataset": {
            "name": dataset.name,
            "description": dataset.description,
            "evaluation_instructions": dataset.evaluation_instructions,
            # The scoring contract. Without these three a re-imported
            # dataset comes back family-less (``needs_family``), with no schema and no
            # vocabulary.
            "family": dataset.family,
            "label_vocabulary": dataset.label_vocabulary,
            "output_schema": dataset.output_schema,
        },
        "entries": entry_dicts,
        "files": [
            {k: v for k, v in fe.items() if not k.startswith("_")}
            for fe in file_entries
        ],
    }

    # Build ZIP in memory
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        for fe in file_entries:
            data = file_bytes.get(fe["_fid"], b"")
            zf.writestr(f"files/{fe['_path_name']}", data)

    from app.core.utils import build_content_disposition

    buf.seek(0)
    safe_name = "".join(c if c.isalnum() or c in "-_" else "_" for c in dataset.name)
    date_str = datetime.now(UTC).strftime("%Y%m%d")
    filename = f"dataset_{safe_name}_{date_str}.zip"

    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={"Content-Disposition": build_content_disposition(filename)},
    )


def _manifest_position(entry: dict, index: int) -> object:
    """An entry's position in a manifest: its index in the list when it names none."""
    position = entry.get("position")
    return index if position is None else position


def _assert_manifest_shape(
    ds_meta: Any, entries_data: Any, files_meta: Any
) -> None:
    """Reject a manifest whose JSON types cannot carry what import reads.

    The three sections and the entry positions; each value is then checked by
    ``_validated_manifest``, and what it means by the helpers ``create_dataset`` uses.
    """
    if not isinstance(ds_meta, dict):
        raise HTTPException(
            status_code=422, detail="manifest 'dataset' must be an object."
        )
    if not isinstance(entries_data, list) or not all(
        isinstance(entry, dict) for entry in entries_data
    ):
        raise HTTPException(
            status_code=422, detail="manifest 'entries' must be a list of objects."
        )
    if not isinstance(files_meta, list) or not all(
        isinstance(meta, dict) for meta in files_meta
    ):
        raise HTTPException(
            status_code=422, detail="manifest 'files' must be a list of objects."
        )

    # The evaluation import finds an entry by its position.
    for i, entry in enumerate(entries_data):
        position = _manifest_position(entry, i)
        if isinstance(position, bool) or not isinstance(position, int) or not 0 <= position < 2**31:
            raise HTTPException(
                status_code=422, detail=f"(entry {i}) position must be a non-negative integer."
            )

    family = ds_meta.get("family")
    if family is not None and not isinstance(family, str):
        raise HTTPException(status_code=422, detail="family must be a string.")

    vocabulary = ds_meta.get("label_vocabulary")
    if vocabulary is not None and (
        not isinstance(vocabulary, list)
        or not all(isinstance(label, str) for label in vocabulary)
    ):
        raise HTTPException(
            status_code=422, detail="label_vocabulary must be a list of strings."
        )


def _read_and_hash(
    zf: zipfile.ZipFile, budget: ArchiveBudget, path: str, *, md5: bool
) -> tuple[bytes, str, str | None]:
    """*path*'s bytes and their SHA-256 (and MD5 when asked), in one worker thread."""
    data = budget.read(zf, path)
    legacy = hashlib.md5(data).hexdigest() if md5 else None  # noqa: S324 - older exports' checksum, compared only
    return data, hashlib.sha256(data).hexdigest(), legacy


#: The dataset fields a package carries, read as ``create_dataset`` reads its body.
_PACKAGE_DATASET_FIELDS = (
    "name",
    "description",
    "evaluation_instructions",
    "output_schema",
    "family",
    "label_vocabulary",
    "is_read_only",
)


def _validated_manifest(
    ds_meta: dict, entries_data: list[dict], files_meta: list[dict]
) -> tuple[dict, list[dict], list[dict]]:
    """Each manifest value checked as a create checks it, or a 422 naming it.

    The dataset through ``CreateDatasetRequest``, each entry through
    ``DatasetEntryRequest``'s fields and each file through ``DatasetPackageFile``, so a
    hand-edited package gets the answer a create would give, never a 500: a file
    without an ``id``, a number for a prompt, a name or a type longer than its column.
    Returned as plain values, what the import reads, with each default filled in.
    """

    def checked(model: type[BaseModel], data: dict, where: str) -> BaseModel:
        try:
            return model.model_validate(data)
        except ValidationError as exc:
            raise HTTPException(
                status_code=422, detail=f"manifest {where}: {validation_detail(exc, root=where)}"
            ) from None

    dataset = checked(
        CreateDatasetRequest,
        {"name": "Imported Dataset", **{k: ds_meta[k] for k in _PACKAGE_DATASET_FIELDS if k in ds_meta}},
        "dataset",
    )
    return (
        dataset.model_dump(include=set(_PACKAGE_DATASET_FIELDS)),
        [checked(DatasetPackageEntry, ed, f"entries[{i}]").model_dump() for i, ed in enumerate(entries_data)],
        [checked(DatasetPackageFile, fm, f"files[{i}]").model_dump() for i, fm in enumerate(files_meta)],
    )


@router.post("/import", response_model=DatasetDetailResponse, status_code=status.HTTP_201_CREATED)
async def import_dataset(
    file: UploadFile = File(...),
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    _ai_gate: None = Depends(require_ai_enabled),
):
    """Import a dataset from a ZIP package."""
    raw = await file.read(MAX_ZIP_SIZE + 1)
    async with objects_deleted_on_failure(db) as stored_objects:
        dataset = (await import_dataset_package(db, raw, user, stored_objects)).dataset
        await db.commit()
    return await _build_dataset_response(db, dataset, include_entries=True)


class ImportedDataset(NamedTuple):
    dataset: Dataset
    #: Each entry's id by its position in the manifest, tied positions left out.
    entry_ids_by_position: dict[int, uuid.UUID]


async def import_dataset_package(
    db: AsyncSession,
    raw: bytes,
    user: User,
    stored_objects: list[str],
    *,
    positions_in_use: Set[int] = frozenset(),
    budget: ArchiveBudget | None = None,
) -> ImportedDataset:
    """Create a dataset from a ZIP package, in the caller's transaction.

    Not committed: the dataset import commits it, the evaluation import with the
    rest of its package. The name of each file it stores is appended to
    *stored_objects*, for the caller to delete if the transaction fails
    (``objects_deleted_on_failure``).

    An older package can carry tied entry positions. A tie on one
    of *positions_in_use*, the positions the evaluation import's results name, is
    refused: which entry such a result answered is lost. Any other tie loses
    nothing, and the entries are then renumbered ``0..n-1`` in manifest order.
    """
    if len(raw) > MAX_ZIP_SIZE:
        raise HTTPException(status_code=413, detail="ZIP file exceeds 500 MB limit")

    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            # What the members declare: an early refusal, not a bound (whoever
            # built the archive sets it). The bound is ``budget``, spent on what each
            # member really expands to: a package nested in an evaluation's spends
            # the evaluation's.
            if budget is None:
                budget = ArchiveBudget(MAX_UNCOMPRESSED_SIZE)
            if sum(member.file_size for member in zf.infolist()) > MAX_UNCOMPRESSED_SIZE:
                raise HTTPException(
                    status_code=413,
                    detail=f"ZIP file exceeds {MAX_UNCOMPRESSED_SIZE // 2**20} MB uncompressed",
                )
            if "manifest.json" not in zf.namelist():
                raise HTTPException(status_code=400, detail="ZIP missing manifest.json")

            manifest_raw = await asyncio.to_thread(
                budget.read, zf, "manifest.json", max_member=MAX_JSON_MEMBER_SIZE
            )
            try:
                manifest = await asyncio.to_thread(json.loads, manifest_raw)
            except Exception as err:
                raise HTTPException(
                    status_code=400, detail="manifest.json is not valid JSON"
                ) from err

            if manifest.get("format") != "filigran-dataset-v1":
                raise HTTPException(
                    status_code=400,
                    detail="Unsupported format. Expected 'filigran-dataset-v1'.",
                )

            ds_meta = manifest.get("dataset", {})
            entries_data = manifest.get("entries", [])
            files_meta = manifest.get("files", [])

            # Shape first: ``create_dataset`` gets its values through Pydantic,
            # which rejects wrong types, while a manifest arrives as whatever the JSON
            # held, and every semantic check below assumes its types. Untyped,
            # ``"label_vocabulary": "abc"`` would persist ``['a','b','c']``, a
            # vocabulary nobody declared, while ``[1, 2]`` or a list ``family`` would
            # surface as a 500.
            _assert_manifest_shape(ds_meta, entries_data, files_meta)
            manifest_positions = [_manifest_position(ed, i) for i, ed in enumerate(entries_data)]
            tied = _tied_positions(manifest_positions, positions_in_use)
            ds_meta, entries_data, files_meta = _validated_manifest(ds_meta, entries_data, files_meta)

            # Every field below is optional: a package written before v2 carries none of
            # them and imports family-less and schema-less, in the ``needs_family``
            # state, rather than failing.
            #
            # A manifest can be hand-edited, so what it declares is validated exactly as
            # ``create_dataset`` validates it: a round trip of an accepted dataset always
            # passes, and an edited or foreign package cannot create rows no other
            # endpoint could. It runs before the uploads below so a refused manifest
            # leaves nothing in object storage.
            family = ds_meta.get("family")
            _validate_family(family)
            dataset_schema = ds_meta.get("output_schema")
            _assert_output_schema_allowed(family, dataset_schema)

            dataset = Dataset(
                user_id=user.id,
                name=ds_meta.get("name", "Imported Dataset"),
                description=ds_meta.get("description"),
                evaluation_instructions=ds_meta.get("evaluation_instructions"),
                is_read_only=ds_meta.get("is_read_only", False),
                family=family,
                output_schema=dataset_schema or None,
            )
            # Same order as ``create_dataset``: the CLASSIFICATION-only and non-blank
            # checks run before any label matching, so a vocabulary on the wrong family
            # reports that rather than "not in dataset vocabulary".
            _apply_dataset_label_vocabulary(
                dataset, family, ds_meta.get("label_vocabulary")
            )

            if dataset_schema:
                schema_err = validate_output_schema(dataset_schema)
                if schema_err:
                    raise HTTPException(status_code=422, detail=schema_err)

            for i, ed in enumerate(entries_data):
                entry_schema = ed.get("output_schema")
                _assert_output_schema_allowed(family, entry_schema)
                if entry_schema:
                    entry_schema_err = validate_output_schema(entry_schema)
                    if entry_schema_err:
                        raise HTTPException(
                            status_code=422, detail=f"(entry {i}) {entry_schema_err}"
                        )
            _assert_references_scorable(
                family,
                dataset_schema or None,
                [
                    (f"(entry {i})", ed.get("expected_answer", ""), ed.get("output_schema"))
                    for i, ed in enumerate(entries_data)
                ],
            )

            if dataset.label_vocabulary:
                for i, ed in enumerate(entries_data):
                    label_err = validate_reference_labels(
                        frozenset(dataset.label_vocabulary),
                        ed.get("expected_answer", ""),
                    )
                    if label_err:
                        raise HTTPException(
                            status_code=422, detail=f"(entry {i}) {label_err}"
                        )

            # Validate and upload embedded files
            fid_to_db_id: dict[str, str] = {}
            for fm in files_meta:
                path = fm.get("path", "")
                if path not in zf.namelist():
                    logger.warning("Import: file %s missing from ZIP, skipping", path)
                    continue

                data, actual_sha256, actual_md5 = await asyncio.to_thread(
                    _read_and_hash, zf, budget, path, md5=bool(fm.get("md5"))
                )
                # Backward compatibility for older exports that used md5.
                if fm.get("sha256") and actual_sha256 != fm["sha256"]:
                    logger.warning("Import: file %s SHA-256 mismatch, skipping", path)
                    continue
                if fm.get("md5") and actual_md5 != fm["md5"]:
                    # Legacy import compatibility for older dataset exports that stored only MD5.
                    # New exports use SHA-256; this fallback is not used for new security decisions.
                    logger.warning("Import: file %s MD5 mismatch, skipping", path)
                    continue

                from app.models.file_attachment import FileAttachment
                from app.services.file_storage import file_storage

                s3_obj = await file_storage.upload(
                    data=data,
                    filename=fm.get("filename", "file"),
                    content_type=fm.get("content_type", "application/octet-stream"),
                    prefix="dataset/import",
                )
                stored_objects.append(s3_obj)
                att = FileAttachment(
                    user_id=user.id,
                    filename=fm.get("filename", "file"),
                    content_type=fm.get("content_type", "application/octet-stream"),
                    size=len(data),
                    s3_object_name=s3_obj,
                    sha256=actual_sha256,
                    source="dataset",
                )
                db.add(att)
                await db.flush()
                fid_to_db_id[fm["id"]] = str(att.id)
    except zipfile.BadZipFile as err:
        raise HTTPException(status_code=400, detail="Invalid ZIP file") from err
    except ArchiveTooLargeError as err:
        raise HTTPException(
            status_code=413,
            detail=f"ZIP file is too large once uncompressed: {err}",
        ) from err

    db.add(dataset)
    await db.flush()

    positions = list(manifest_positions)
    if tied:
        # Ties broken by list order, so the import never writes a tied dataset.
        order = sorted(range(len(positions)), key=lambda i: (positions[i], i))
        for rank, i in enumerate(order):
            positions[i] = rank
    entry_ids_by_position: dict[int, uuid.UUID] = {}
    for i, ed in enumerate(entries_data):
        file_ids = [fid_to_db_id[fid] for fid in ed.get("files", []) if fid in fid_to_db_id]
        entry_id = uuid.uuid4()
        if manifest_positions[i] not in tied:
            entry_ids_by_position[manifest_positions[i]] = entry_id
        db.add(
            DatasetEntry(
                id=entry_id,
                dataset_id=dataset.id,
                position=positions[i],
                input_prompt=ed.get("input_prompt", ""),
                expected_answer=ed.get("expected_answer", ""),
                evaluation_prompt=ed.get("evaluation_prompt"),
                description=ed.get("description"),
                file_ids=file_ids or None,
                output_schema=ed.get("output_schema"),
            )
        )

    await log_audit(
        db,
        user=user,
        action="import",
        entity_type="dataset",
        entity_id=str(dataset.id),
        entity_name=dataset.name,
        summary=f"Imported dataset '{dataset.name}' from ZIP",
    )
    await db.flush()
    await dataset_versioning.bump_version(db, dataset)
    return ImportedDataset(dataset, entry_ids_by_position)


def _tied_positions(positions: list[int], positions_in_use: Set[int]) -> set[int]:
    """The positions several entries share; a 422 when one is in *positions_in_use*."""
    entry_at: dict[int, int] = {}
    tied: set[int] = set()
    for i, position in enumerate(positions):
        if position not in entry_at:
            entry_at[position] = i
            continue
        if position in positions_in_use:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"(entry {i}) position {position} is also entry {entry_at[position]}'s, "
                    "and a result names it: export the dataset again from an up-to-date platform."
                ),
            )
        tied.add(position)
    return tied


# ---------------------------------------------------------------------------
# HuggingFace import (creates a new Dataset)
# ---------------------------------------------------------------------------


@router.post("/hf-import")
async def hf_import_dataset(
    body: HFImportRequest,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    _ai_gate: None = Depends(require_ai_enabled),
):
    """Import a HuggingFace split as a new Dataset, streaming SSE progress events."""
    import asyncio

    page_size = 100
    concurrent_pages = 10
    total = body.row_limit or 500_000
    base_params: dict = {"dataset": body.hf_dataset_id, "split": body.split}
    if body.config:
        base_params["config"] = body.config

    def sse(payload: dict) -> str:
        return f"data: {json.dumps(payload)}\n\n"

    async def generate():
        try:
            async def fetch_page(client: httpx.AsyncClient, offset: int) -> list:
                length = min(page_size, total - offset)
                resp = await client.get(
                    f"{HF_API_BASE}/rows",
                    params={**base_params, "offset": offset, "length": length},
                )
                if resp.status_code != 200:
                    raise ValueError(f"HuggingFace API error: {resp.text[:200]}")
                return resp.json().get("rows", [])

            # Phase 1 — paginate HF rows and stream progress after each batch
            rows: list = []
            async with httpx.AsyncClient(timeout=60) as client:
                offsets = list(range(0, total, page_size))
                for batch_start in range(0, len(offsets), concurrent_pages):
                    batch = offsets[batch_start : batch_start + concurrent_pages]
                    pages = await asyncio.gather(*[fetch_page(client, off) for off in batch])
                    for page in pages:
                        rows.extend(page)
                    yield sse({"type": "progress", "fetched": min(len(rows), total), "total": total})
                    if any(len(p) < page_size for p in pages):
                        break

            rows = rows[:total]
            if not rows:
                yield sse({"type": "error", "message": "No rows returned from HuggingFace"})
                return

            # Phase 2 — persist to DB
            yield sse({"type": "saving"})

            entry_dicts = []
            for row_obj in rows:
                row = row_obj.get("row", row_obj)
                entry_dicts.append({
                    "input_prompt": str(row.get(body.input_column, "")),
                    "expected_answer": str(row.get(body.expected_answer_column, "")) if body.expected_answer_column else "",
                    "evaluation_prompt": (str(row.get(body.evaluation_prompt_column, "")) or None) if body.evaluation_prompt_column else None,
                    "description": (str(row.get(body.description_column, "")) or None) if body.description_column else None,
                })

            tmp_entries = [
                SimpleNamespace(
                    input_prompt=e["input_prompt"],
                    expected_answer=e["expected_answer"],
                    evaluation_prompt=e["evaluation_prompt"] or "",
                )
                for e in entry_dicts
            ]
            content_hash = _compute_content_hash(tmp_entries)  # type: ignore[arg-type]

            existing = await db.execute(select(Dataset).where(Dataset.content_hash == content_hash))
            existing_ds = existing.scalar_one_or_none()
            if existing_ds:
                yield sse({"type": "done", "id": str(existing_ds.id), "name": existing_ds.name, "reused": True})
                return

            config_part = f" / {body.config}" if body.config else ""
            ds_name = body.name or fit_copy_name(
                f"{body.hf_dataset_id}{config_part} / {body.split}", Dataset.__table__.c.name
            )
            dataset = Dataset(
                user_id=user.id,
                name=ds_name,
                description=body.description,
                evaluation_instructions=body.evaluation_instructions,
                content_hash=content_hash,
                is_read_only=body.is_read_only,
            )
            db.add(dataset)
            await db.flush()

            for i, ed in enumerate(entry_dicts):
                db.add(DatasetEntry(
                    dataset_id=dataset.id,
                    position=i,
                    input_prompt=ed["input_prompt"],
                    expected_answer=ed["expected_answer"],
                    evaluation_prompt=ed["evaluation_prompt"],
                    description=ed["description"],
                ))

            await log_audit(
                db,
                user=user,
                action="create",
                entity_type="dataset",
                entity_id=str(dataset.id),
                entity_name=dataset.name,
                summary=f"Imported dataset '{dataset.name}' from HuggingFace",
            )
            await db.flush()
            await dataset_versioning.bump_version(db, dataset)
            await db.commit()
            yield sse({"type": "done", "id": str(dataset.id), "name": dataset.name})

        except Exception as exc:
            logger.exception("HuggingFace dataset import failed", exc_info=exc)
            yield sse(
                {
                    "type": "error",
                    "message": "An internal error occurred while importing the dataset.",
                }
            )

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"},
    )


# ---------------------------------------------------------------------------
# Link / Unlink to Evaluation
# ---------------------------------------------------------------------------


@router.post("/{dataset_id}/link/{evaluation_id}", status_code=status.HTTP_201_CREATED)
async def link_dataset_to_evaluation(
    dataset_id: str,
    evaluation_id: str,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    _ai_gate: None = Depends(require_ai_enabled),
):
    """Link a dataset to an evaluation."""
    user_group_ids = await get_user_group_ids(db, user.id)
    dataset = await _get_user_dataset(
        db, user.id, dataset_id, user_group_ids, is_admin=user.is_admin
    )

    eid = parse_path_id(evaluation_id, not_found_detail="Evaluation not found")
    eval_result = await db.execute(
        select(Evaluation).where(Evaluation.id == eid)
    )
    evaluation = eval_result.scalar_one_or_none()
    if not evaluation:
        raise HTTPException(status_code=404, detail="Evaluation not found")

    existing = await db.execute(
        select(EvaluationDataset).where(
            EvaluationDataset.evaluation_id == eid,
            EvaluationDataset.dataset_id == dataset.id,
        )
    )
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="Dataset already linked to this evaluation")

    # Reject only when both sides declare a family and they conflict: a
    # family-less side never blocks a link.
    conflict = check_family_compatibility(dataset.family, evaluation.family)
    if conflict is not None:
        raise HTTPException(status_code=409, detail=conflict)

    await _lock_for_append(db, Evaluation, eid)
    # ``coalesce``, not ``or -1``: a maximum of 0 is not an empty list.
    max_pos = (
        await db.execute(
            select(func.coalesce(func.max(EvaluationDataset.position), -1)).where(
                EvaluationDataset.evaluation_id == eid
            )
        )
    ).scalar_one()

    db.add(
        EvaluationDataset(
            evaluation_id=eid,
            dataset_id=dataset.id,
            position=max_pos + 1,
        )
    )
    await db.commit()
    return {"status": "linked", "dataset_id": str(dataset.id), "evaluation_id": evaluation_id}


@router.delete("/{dataset_id}/link/{evaluation_id}", status_code=status.HTTP_204_NO_CONTENT)
async def unlink_dataset_from_evaluation(
    dataset_id: str,
    evaluation_id: str,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    _ai_gate: None = Depends(require_ai_enabled),
):
    """Unlink a dataset from an evaluation."""
    did = parse_path_id(dataset_id, not_found_detail="Dataset not linked to this evaluation")
    eid = parse_path_id(evaluation_id, not_found_detail="Dataset not linked to this evaluation")

    existing = await db.execute(
        select(EvaluationDataset).where(
            EvaluationDataset.evaluation_id == eid,
            EvaluationDataset.dataset_id == did,
        )
    )
    link = existing.scalar_one_or_none()
    if not link:
        raise HTTPException(status_code=404, detail="Dataset not linked to this evaluation")

    await db.delete(link)
    await db.commit()


# ---------------------------------------------------------------------------
# Linked datasets for an evaluation
# ---------------------------------------------------------------------------


@router.get("/for-evaluation/{evaluation_id}", response_model=list[LinkedDatasetResponse])
async def list_evaluation_datasets(
    evaluation_id: str,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """List all datasets linked to a specific evaluation.

    Each one says whether it blocks a launch (``needs_family``) and whether its family
    conflicts with the evaluation's (``family_conflict``): a link refuses a
    conflict, so one appears only once either side's family changed, and is reported,
    never fixed.
    """
    eid = safe_uuid(evaluation_id)
    if eid is None:
        return []
    evaluation_family = (
        await db.execute(select(Evaluation.family).where(Evaluation.id == eid))
    ).scalar_one_or_none()
    result = await db.execute(
        select(EvaluationDataset, Dataset)
        .join(Dataset, Dataset.id == EvaluationDataset.dataset_id)
        .where(EvaluationDataset.evaluation_id == eid)
        .order_by(EvaluationDataset.position)
    )
    rows = result.all()

    out = []
    for ed, ds in rows:
        entry_count_result = await db.execute(
            select(func.count()).where(DatasetEntry.dataset_id == ds.id)
        )
        entry_count = entry_count_result.scalar() or 0
        out.append(
            {
                "id": str(ed.id),
                "dataset_id": str(ds.id),
                "dataset_name": ds.name,
                "dataset_description": ds.description,
                "entry_count": entry_count,
                "position": ed.position,
                "created_at": ed.created_at.isoformat(),
                "family": ds.family,
                "needs_family": ds.family is None,
                "family_conflict": check_family_compatibility(
                    ds.family, evaluation_family
                ),
            }
        )
    return out
