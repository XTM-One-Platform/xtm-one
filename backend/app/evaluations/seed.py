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

"""Seed the built-in benchmark datasets and evaluations.

In every mode where AI runs (``AI_MODES``: ``xtm_one``, ``copilot``, ``dev``),
the modes where evaluation can run and its page is shown; the
caller skips both seeds while evaluation is off. The agents the evaluations
link ship in every mode the benchmark does (their ``platform_targets``). A
built-in row is identified by its ``slug``; a row without one was created by a
person and is never touched.

Datasets are FULL-SYNCED on every startup, like built-in agents: name,
description, instructions, family and entries follow ``seed_data/datasets/*.json``.
Entries are only rewritten when their text or their declared document changed,
because a rewrite mints new entry ids and ``evaluation_results.entry_id``
cascades - rewriting on every boot would wipe the benchmark's history, and
keeping an entry whose document changed would mix scores measured on two
documents. The documents are rendered from the seed file (``documents.py``),
stored under a key carrying both the declaration fingerprint and the bytes
digest, and re-uploaded only when their bytes change (a renderer dependency
upgrade swaps the file, not the test case); an object-store failure leaves a
new dataset's text in place and an existing one as it was, and is retried on
the next boot. A run reads the
entries and their files once, when it starts, so a dataset a run is going
through keeps both until the next boot. A sync that changes what a version
snapshots (the entries, their files, the vocabulary, the answer shape) cuts a new version once the
documents are attached, as an edit in the UI does: a run pinned to the
previous one keeps scoring what it ran on, the next run scores the new documents.

Evaluations are CREATED ONCE and then belong to the administrators: which
models to compare, which agents to run and when to run it is their call, so
the seed never overwrites an existing built-in evaluation (it only fills in a
missing owner and a missing family). A new one waits until its dataset
holds every declared document. Both kinds are removed once their seed file is
gone.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import pathlib
import re
import uuid

from sqlalchemy import delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import AI_MODES
from app.core.utils import safe_uuid
from app.evaluations.documents import (
    RenderedDocument,
    document_fingerprint,
    document_markdown,
    render_document,
    validate_document,
)
from app.models.dataset import Dataset, DatasetEntry, EvaluationDataset
from app.models.evaluation import (
    Evaluation,
    EvaluationAgent,
    EvaluationResult,
    EvaluationRun,
)
from app.models.file_attachment import FileAttachment
from app.services.evaluation import dataset_versioning

logger = logging.getLogger(__name__)

SEED_DATA_DIR = pathlib.Path(__file__).parent / "seed_data"
#: Object-key prefix of every seeded document (key shape: :func:`seed_object_name`).
SEED_FILE_PREFIX = "dataset/builtin"
SEED_FILE_SOURCE = "dataset"
_SEED_DIGEST_CHARS = 16
#: The platform modes that seed the benchmark: those where evaluation can run.
#: A mode left out deletes the benchmark and its runs there at the next boot
#: while evaluation is on; adding one only creates rows. A seeded evaluation
#: carries no schedule (``trigger_config`` empty): nothing runs at boot.
_SEED_MODES = AI_MODES


def load_seed_files(kind: str) -> list[dict]:
    """The seed declarations of *kind* (``datasets`` or ``evaluations``), by file name."""
    folder = SEED_DATA_DIR / kind
    seeds: list[dict] = []
    for path in sorted(folder.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("slug") != path.stem:
            raise ValueError(f"{path.name}: slug {data.get('slug')!r} must match the file name")
        _number_entries(path.name, data.get("entries", []))
        seeds.append(data)
    return seeds


def _number_entries(file_name: str, entries: list[dict]) -> None:
    """Give each entry its declared position, else its index, and refuse a tie.

    Positions are unique in a dataset: a tie would fail the sync's insert
    at boot, so it is named here, at load.
    """
    entry_at: dict[int, int] = {}
    for index, entry in enumerate(entries):
        position = entry.setdefault("position", index)
        if position in entry_at:
            raise ValueError(f"{file_name}: entries {entry_at[position]} and {index} share position {position}")
        entry_at[position] = index


def seed_entry_fields(entry: dict) -> dict:
    """The column values of one seeded entry; a declared document supplies the expected answer."""
    document = entry.get("document")
    if document is not None:
        validate_document(document)
        expected = document_markdown(document)
    else:
        expected = entry["expected_answer"]
    return {
        "position": entry["position"],
        "description": entry.get("description"),
        "input_prompt": entry.get("input_prompt", ""),
        "expected_answer": expected,
        "evaluation_prompt": entry.get("evaluation_prompt"),
    }


def seed_object_name(dataset_slug: str, document: RenderedDocument) -> str:
    """``<prefix>/<slug>/<declaration fingerprint>-<hash16 of the bytes>-<file name>``.

    The bytes digest makes a key name one content forever (the download ETag is
    derived from the key); the fingerprint tells the next boot which declared
    document an entry's file was rendered from.
    """
    digest = hashlib.sha256(document.data).hexdigest()
    return (
        f"{SEED_FILE_PREFIX}/{dataset_slug}/{document.fingerprint}-{digest[:_SEED_DIGEST_CHARS]}-{document.file_name}"
    )


def seeded_fingerprint(object_name: str) -> str:
    """The declaration fingerprint a seeded object key carries ('' for any other key)."""
    head = object_name.rpartition("/")[2].partition("-")[0]
    return head if re.fullmatch(r"[0-9a-f]{16}", head) else ""


def _entry_key(fields: dict, fingerprint: str) -> tuple:
    return (
        fields["position"],
        fields["description"],
        fields["input_prompt"],
        fields["expected_answer"],
        fields["evaluation_prompt"],
        fingerprint,
    )


def _db_entry_key(entry: DatasetEntry, fingerprint: str) -> tuple:
    return (
        entry.position,
        entry.description,
        entry.input_prompt,
        entry.expected_answer,
        entry.evaluation_prompt,
        fingerprint,
    )


async def _owner_id(db: AsyncSession) -> uuid.UUID | None:
    from app.api.v1.helpers import resolve_company_managed_owner_id

    return await resolve_company_managed_owner_id(db)


async def _seeded_file_rows(db: AsyncSession, dataset_slug: str | None = None) -> list[FileAttachment]:
    """Every seeded row of *dataset_slug*, orphans included.

    A prefix match no index serves, so a scan of ``file_attachments``: only for
    the rare syncs that can hold orphans, never on an ordinary boot.
    """
    prefix = f"{SEED_FILE_PREFIX}/{dataset_slug}/" if dataset_slug else f"{SEED_FILE_PREFIX}/"
    result = await db.execute(
        select(FileAttachment).where(
            FileAttachment.source == SEED_FILE_SOURCE,
            FileAttachment.s3_object_name.startswith(prefix, autoescape=True),
        )
    )
    return list(result.scalars().all())


async def _attached_file_rows(
    db: AsyncSession, dataset_slug: str, file_ids: list[str], keys: list[str] | None = None
) -> list[FileAttachment]:
    """The seeded rows of *dataset_slug* among *file_ids* and object *keys*, through their indexes."""
    ids = [fid for fid in (safe_uuid(value) for value in file_ids) if fid is not None]
    wanted = [FileAttachment.id.in_(ids)] if ids else []
    if keys:
        wanted.append(FileAttachment.s3_object_name.in_(keys))
    if not wanted:
        return []
    prefix = f"{SEED_FILE_PREFIX}/{dataset_slug}/"
    rows = (await db.execute(select(FileAttachment).where(or_(*wanted)))).scalars().all()
    return [row for row in rows if row.source == SEED_FILE_SOURCE and row.s3_object_name.startswith(prefix)]


# ---------------------------------------------------------------------------
# Datasets
# ---------------------------------------------------------------------------


async def seed_builtin_datasets(db: AsyncSession, *, platform_mode: str = "copilot") -> int:
    """Synchronise the built-in datasets. Returns how many were created.

    Obsolete objects go only once no committed row names them, through
    ``purge_storage_objects``, which stops at the first unreachable-store
    failure: a leaked object, never a boot waiting out one timeout per object.
    """
    from app.services.file_cleanup import purge_storage_objects

    seeds = load_seed_files("datasets") if platform_mode in _SEED_MODES else []
    wanted = {seed["slug"] for seed in seeds}

    # Retired datasets: their entries cascade, their documents go with them.
    stale = (await db.execute(select(Dataset).where(Dataset.slug.is_not(None)))).scalars().all()
    obsolete: list[str] = []
    for dataset in stale:
        if dataset.slug in wanted:
            continue
        for row in await _seeded_file_rows(db, dataset.slug):
            obsolete.append(row.s3_object_name)
            await db.delete(row)
        await db.execute(delete(Dataset).where(Dataset.id == dataset.id))
        logger.info("Removed retired built-in dataset %s", dataset.slug)
    await db.commit()
    await purge_storage_objects(obsolete)

    owner_id = await _owner_id(db) if seeds else None
    created = 0
    for seed in seeds:
        if await _being_evaluated(db, seed["slug"]):
            logger.info(
                "The built-in dataset %s has a run pending or running; its sync waits for the next startup",
                seed["slug"],
            )
            continue
        dataset, was_created, rewritten = await _sync_dataset(db, seed, owner_id)
        created += was_created
        if was_created or not rewritten:
            # A new dataset's text lands whatever the object store does (its
            # evaluation waits for the documents); rewritten entries of an
            # existing one only land with the documents they attach, since its
            # evaluation may already run. What lands now is versioned with it,
            # so a failed document sync never leaves the version a run pins
            # behind the live dataset.
            await dataset_versioning.cut_version_if_changed(db, dataset)
            await db.commit()
        try:
            obsolete = await _sync_dataset_documents(db, dataset, seed, rewritten=rewritten)
            # With the documents attached, in their transaction.
            await dataset_versioning.cut_version_if_changed(db, dataset)
            await db.commit()
        except Exception:  # boot must survive an object-store or rendering failure
            await db.rollback()
            logger.warning(
                "Documents of the built-in dataset %s could not be synced; will retry on next startup",
                seed["slug"],
                exc_info=True,
            )
            continue
        await purge_storage_objects(obsolete)
    return created


async def _being_evaluated(db: AsyncSession, dataset_slug: str) -> bool:
    """Whether a run over the built-in dataset *dataset_slug* is pending or running.

    Replacing its entries under it would fail its results, and swapping a file
    would score an entry with no document. A pending run counts: it is pinned
    to the current version from its creation, and may wait a long time to
    start, held while AI is unavailable, so the next version would
    leave it scoring deleted documents.
    """
    running = await db.execute(
        select(EvaluationRun.id)
        .join(EvaluationDataset, EvaluationDataset.evaluation_id == EvaluationRun.evaluation_id)
        .join(Dataset, Dataset.id == EvaluationDataset.dataset_id)
        .where(Dataset.slug == dataset_slug, EvaluationRun.status.in_(("pending", "running")))
        .limit(1)
    )
    return running.first() is not None


async def _sync_dataset(db: AsyncSession, seed: dict, owner_id: uuid.UUID | None) -> tuple[Dataset, bool, bool]:
    """Sync one dataset and its entries. Returns it, whether it was created and whether its entries were rewritten."""
    slug = seed["slug"]
    dataset = (await db.execute(select(Dataset).where(Dataset.slug == slug).limit(1))).scalar_one_or_none()
    created = dataset is None
    if dataset is None:
        dataset = Dataset(id=uuid.uuid4(), slug=slug, user_id=owner_id)
        db.add(dataset)
    dataset.name = seed["name"]
    dataset.description = seed.get("description")
    dataset.evaluation_instructions = seed.get("evaluation_instructions")
    dataset.family = seed.get("family")
    dataset.is_company_managed = True
    dataset.is_read_only = True
    if owner_id is not None:
        dataset.user_id = owner_id
    await db.flush()

    # An entry is the same test case only if its text AND the document it
    # attaches are unchanged: a new declaration under the same text (a scan
    # where a PDF was, another layout) must not inherit the old scores.
    declared = [
        (seed_entry_fields(entry), document_fingerprint(entry["document"]) if entry.get("document") else "")
        for entry in seed.get("entries", [])
    ]
    fields = [values for values, _ in declared]
    existing = (await db.execute(select(DatasetEntry).where(DatasetEntry.dataset_id == dataset.id))).scalars().all()
    attached = {
        str(row.id): seeded_fingerprint(row.s3_object_name)
        for row in await _attached_file_rows(db, slug, [fid for entry in existing for fid in entry.file_ids or []])
    }
    current_keys = sorted(
        _db_entry_key(entry, "".join(attached.get(fid, "?") for fid in (entry.file_ids or []))) for entry in existing
    )
    rewritten = current_keys != sorted(_entry_key(values, fingerprint) for values, fingerprint in declared)
    if rewritten:
        # ``EvaluationResult.entry_id`` is SET NULL, not CASCADE, so a run
        # stays readable through its pinned version snapshot after a curated dataset
        # is edited. A built-in document is rewritten under the user instead, so a
        # score measured on the old one must not survive attached to the run: its
        # results are deleted explicitly, for this built-in dataset only.
        await db.execute(
            delete(EvaluationResult).where(
                EvaluationResult.entry_id.in_(
                    select(DatasetEntry.id).where(DatasetEntry.dataset_id == dataset.id)
                )
            )
        )
        await db.execute(delete(DatasetEntry).where(DatasetEntry.dataset_id == dataset.id))
        for values in fields:
            db.add(DatasetEntry(dataset_id=dataset.id, **values))
        await db.flush()
        logger.info("Synced the entries of the built-in dataset %s", slug)
    if created:
        logger.info("Created the built-in dataset %s", slug)
    return dataset, created, rewritten


async def _sync_dataset_documents(db: AsyncSession, dataset: Dataset, seed: dict, *, rewritten: bool) -> list[str]:
    """Attach each entry's rendered document. Returns the object keys no row names any more.

    The files are system uploads, owned by nobody: an owner would see them in
    Work > Files and could delete one there, leaving its entry without the
    document and making the next boot rewrite every entry of the dataset.
    """
    from app.services.file_storage import file_storage

    declared = {
        entry["position"]: entry["document"] for entry in seed.get("entries", []) if entry.get("document")
    }
    rendered = {position: await asyncio.to_thread(render_document, document) for position, document in declared.items()}
    entries = (await db.execute(select(DatasetEntry).where(DatasetEntry.dataset_id == dataset.id))).scalars().all()
    if rewritten:
        # The replaced entries took the only references to their files with
        # them; a failed sync here is seen as a rewrite again at the next boot.
        rows = await _seeded_file_rows(db, dataset.slug)
    else:
        file_ids = [fid for entry in entries for fid in entry.file_ids or []]
        keys = [seed_object_name(dataset.slug, document) for document in rendered.values()]
        rows = await _attached_file_rows(db, dataset.slug, file_ids, keys)
    rows_by_key = {row.s3_object_name: row for row in rows}

    kept: set[str] = set()
    for entry in entries:
        document = rendered.get(entry.position)
        if document is None:
            continue
        key = seed_object_name(dataset.slug, document)
        row = rows_by_key.get(key)
        if row is None:
            await file_storage.upload_to(document.data, key, document.content_type)
            row = FileAttachment(
                id=uuid.uuid4(),
                filename=document.file_name,
                content_type=document.content_type,
                size=len(document.data),
                s3_object_name=key,
                sha256=hashlib.sha256(document.data).hexdigest(),
                source=SEED_FILE_SOURCE,
            )
            db.add(row)
            rows_by_key[key] = row
            logger.info("Seeded the benchmark document %s", document.file_name)
        elif row.user_id is not None:
            row.user_id = None
        kept.add(key)
        if entry.file_ids != [str(row.id)]:
            entry.file_ids = [str(row.id)]

    obsolete: list[str] = []
    for key, row in rows_by_key.items():
        if key not in kept:
            obsolete.append(key)
            await db.delete(row)
    return obsolete


# ---------------------------------------------------------------------------
# Evaluations
# ---------------------------------------------------------------------------


async def _documents_attached(db: AsyncSession, dataset_id: uuid.UUID, dataset_slug: str) -> bool:
    """Whether every entry the dataset's seed file gives a document has its file attached."""
    seed = next((s for s in load_seed_files("datasets") if s["slug"] == dataset_slug), {})
    declared = {entry["position"] for entry in seed.get("entries", []) if entry.get("document")}
    rows = await db.execute(
        select(DatasetEntry.position, DatasetEntry.file_ids).where(DatasetEntry.dataset_id == dataset_id)
    )
    return declared <= {position for position, file_ids in rows.all() if file_ids}


async def seed_builtin_evaluations(db: AsyncSession, *, platform_mode: str = "copilot") -> int:
    """Create the missing built-in evaluations; remove the retired ones. Returns how many were created.

    Runs after :func:`seed_builtin_datasets` and after the built-in agents:
    a new evaluation links both by slug. It is only created once its dataset
    holds every declared document, so an object-store outage defers it to a
    later boot instead of shipping a runnable benchmark with no files.
    """
    from app.models.agent import Agent

    seeds = load_seed_files("evaluations") if platform_mode in _SEED_MODES else []
    wanted = {seed["slug"] for seed in seeds}
    await db.execute(delete(Evaluation).where(Evaluation.slug.is_not(None), Evaluation.slug.notin_(wanted)))

    owner_id = await _owner_id(db) if seeds else None
    created = 0
    for seed in seeds:
        slug = seed["slug"]
        existing = (await db.execute(select(Evaluation).where(Evaluation.slug == slug).limit(1))).scalar_one_or_none()
        if existing is not None:
            if existing.user_id is None and owner_id is not None:
                existing.user_id = owner_id
            # Filled when missing, never overwritten: an administrator's family stays.
            if existing.family is None and seed.get("family"):
                existing.family = seed["family"]
            continue
        dataset_slug = seed.get("dataset_slug")
        dataset_id = None
        if dataset_slug:
            dataset_id = (
                await db.execute(select(Dataset.id).where(Dataset.slug == dataset_slug).limit(1))
            ).scalar_one_or_none()
            if dataset_id is None or not await _documents_attached(db, dataset_id, dataset_slug):
                logger.info(
                    "Built-in evaluation %s waits for the documents of dataset %s; will retry on next startup",
                    slug,
                    dataset_slug,
                )
                continue
        evaluation = Evaluation(
            id=uuid.uuid4(),
            slug=slug,
            user_id=owner_id,
            is_company_managed=True,
            name=seed["name"],
            description=seed.get("description"),
            family=seed.get("family"),
            execution_mode=seed.get("execution_mode", "full_loop"),
            use_agent_default_model=seed.get("use_agent_default_model", True),
            model_ids=list(seed.get("model_ids", [])),
            disabled_model_ids=[],
            trigger_config=dict(seed.get("trigger_config", {})),
            timeout_seconds=seed.get("timeout_seconds", 3600),
            enabled=True,
        )
        db.add(evaluation)
        await db.flush()
        for agent_slug in seed.get("agent_slugs", []):
            agent_id = (
                await db.execute(select(Agent.id).where(Agent.slug == agent_slug, Agent.is_builtin.is_(True)).limit(1))
            ).scalar_one_or_none()
            if agent_id is None:
                logger.warning("Built-in evaluation %s: agent %s not found, not linked", slug, agent_slug)
                continue
            db.add(EvaluationAgent(evaluation_id=evaluation.id, agent_id=agent_id))
        if dataset_id is not None:
            db.add(EvaluationDataset(evaluation_id=evaluation.id, dataset_id=dataset_id, position=0))
        created += 1
        logger.info("Created the built-in evaluation %s", slug)
    await db.commit()
    return created
