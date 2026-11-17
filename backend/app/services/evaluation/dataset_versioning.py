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

"""Dataset version snapshotting – reproducibility for evaluation runs.

Every dataset mutation bumps ``Dataset.current_version`` and writes an
immutable ``DatasetVersion`` header plus a frozen ``DatasetVersionEntry`` per
live entry.  Evaluation runs pin the current version at trigger time and score
against these snapshots, so editing or deleting an entry never changes
historical results.

The snapshot write and the ``current_version`` increment happen in a single
transaction (the caller owns the ``commit``): any failure rolls back fully so
no partial version rows persist and the counter does not advance.
"""

import hashlib
import json
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.dataset import Dataset, DatasetEntry
from app.models.dataset_version import DatasetVersion, DatasetVersionEntry


def compute_version_hash(entries: list[DatasetEntry]) -> str:
    """Deterministic SHA-256 over the canonical content of ``entries``.

    Includes everything that affects scoring reproducibility: prompts,
    expected answer, per-entry evaluation prompt, structured-output schema and
    attached file ids.  Order-independent (sorted by content).
    """
    rows = sorted(
        [
            {
                "input_prompt": e.input_prompt or "",
                "expected_answer": e.expected_answer or "",
                "evaluation_prompt": e.evaluation_prompt or "",
                "output_schema": e.output_schema or None,
                "file_ids": sorted(e.file_ids or []),
            }
            for e in entries
        ],
        key=lambda r: (r["input_prompt"], r["expected_answer"], r["evaluation_prompt"]),
    )
    return hashlib.sha256(
        json.dumps(rows, sort_keys=True, ensure_ascii=True).encode()
    ).hexdigest()


def version_shape(source: Dataset | DatasetVersion) -> tuple:
    """What a run reads from its pinned version besides the entries, as it reads it.

    Read off a dataset or one of its versions alike, so the API and the
    built-in seed decide the same way whether a change makes a version.
    The vocabulary is compared as a set: scoring reads it as one, so a reorder
    changes nothing a run reads and makes no version. Not the family: a
    run takes it from the evaluation.
    """
    vocabulary = source.label_vocabulary
    return (
        frozenset(vocabulary) if vocabulary is not None else None,
        source.output_schema,
    )


async def _load_entries(db: AsyncSession, dataset_id: uuid.UUID) -> list[DatasetEntry]:
    result = await db.execute(
        select(DatasetEntry)
        .where(DatasetEntry.dataset_id == dataset_id)
        .order_by(DatasetEntry.position)
    )
    return list(result.scalars().all())


async def bump_version(
    db: AsyncSession, dataset: Dataset, *, entries: list[DatasetEntry] | None = None
) -> DatasetVersion:
    """Increment ``dataset.current_version`` and snapshot the live entries.

    Atomic with the surrounding transaction – the caller is responsible for
    committing.  Returns the newly created :class:`DatasetVersion`.  If any
    part of the snapshot write fails, the whole transaction (including the
    counter increment) rolls back so no partial version is persisted.
    *entries* are the live entries, when the caller already loaded them.
    """
    if entries is None:
        entries = await _load_entries(db, dataset.id)

    new_version = (dataset.current_version or 0) + 1
    dataset.current_version = new_version

    version = DatasetVersion(
        dataset_id=dataset.id,
        version_number=new_version,
        content_hash=compute_version_hash(entries),
        entry_count=len(entries),
        # Snapshotted like the entries: a completed run must stay interpretable
        # after the dataset moves on. Without it, resolution silently falls through to
        # the live dataset and re-describes every past run under a schema it was never
        # scored against.
        output_schema=dataset.output_schema,
        # Same for the vocabulary: the engine reads it from THIS snapshot.
        label_vocabulary=dataset.label_vocabulary,
    )
    db.add(version)
    await db.flush()

    for entry in entries:
        db.add(
            DatasetVersionEntry(
                version_id=version.id,
                entry_id=entry.id,
                position=entry.position,
                description=entry.description,
                input_prompt=entry.input_prompt,
                expected_answer=entry.expected_answer,
                evaluation_prompt=entry.evaluation_prompt,
                file_ids=entry.file_ids,
                output_schema=entry.output_schema,
            )
        )
    await db.flush()
    return version


async def cut_version_if_changed(db: AsyncSession, dataset: Dataset) -> DatasetVersion | None:
    """Cut a version when the live dataset no longer reads as its current one.

    For a writer that replaces a dataset wholesale, as the built-in seed does at
    every start: compared with the stored version, so a sync whose version an
    earlier start failed to cut is caught by the next one, and a start that
    changes nothing cuts none. A dataset never versioned yet is left to its
    first run, which snapshots it. The caller owns the commit.
    """
    # The comparison and the cut under the dataset's row lock, on the version
    # number as committed: two writers never cut the same number. The seed is
    # already one at a time (``bootstrap_lock``); another caller may not be.
    await db.execute(select(Dataset.id).where(Dataset.id == dataset.id).with_for_update())
    await db.refresh(dataset, ["current_version"])
    if not dataset.current_version:
        return None
    current = await get_version(db, dataset.id, dataset.current_version)
    entries = await _load_entries(db, dataset.id)
    if (
        current is not None
        and version_shape(current) == version_shape(dataset)
        and _frozen(await load_version_entries(db, current.id)) == _frozen(entries)
    ):
        return None
    return await bump_version(db, dataset, entries=entries)


def _frozen(entries: list[DatasetEntry] | list[DatasetVersionEntry]) -> list[tuple]:
    """What a version freezes of each entry, the entry it points to included.

    Not the content hash, which leaves out the description, the position and the
    entry ids: an entry recreated with the same content (the seed rewrites every
    entry when one changes) would leave the version pointing at deleted entries,
    and a run on it would write results linked to none.
    """
    rows = [
        (
            str(entry.entry_id if isinstance(entry, DatasetVersionEntry) else entry.id),
            entry.position,
            entry.description,
            entry.input_prompt,
            entry.expected_answer,
            entry.evaluation_prompt,
            json.dumps(entry.file_ids, sort_keys=True),
            json.dumps(entry.output_schema, sort_keys=True),
        )
        for entry in entries
    ]
    # By text: two entries of a version can share a missing entry id and a
    # position, and None does not compare with a string.
    return sorted(rows, key=repr)


async def pin_evaluation_versions(
    db: AsyncSession, evaluation_id: uuid.UUID
) -> dict[str, int]:
    """Pin (and, if needed, create) dataset versions for an evaluation trigger.

    For every dataset linked to the evaluation, ensure it has at least one
    immutable version — a legacy dataset (``current_version == 0``) is
    snapshotted now so the run has a stable version to reference — then return
    ``{dataset_id: current_version}`` for embedding in
    ``EvaluationRun.pinned_dataset_versions``. The caller owns the
    surrounding transaction/commit.
    """
    from app.models.dataset import EvaluationDataset

    result = await db.execute(
        select(Dataset)
        .join(EvaluationDataset, EvaluationDataset.dataset_id == Dataset.id)
        .where(EvaluationDataset.evaluation_id == evaluation_id)
    )
    datasets = list(result.scalars().all())
    pinned: dict[str, int] = {}
    for ds in datasets:
        if (ds.current_version or 0) == 0:
            await bump_version(db, ds)
        pinned[str(ds.id)] = ds.current_version
    return pinned


async def get_version(
    db: AsyncSession, dataset_id: uuid.UUID, version_number: int
) -> DatasetVersion | None:
    result = await db.execute(
        select(DatasetVersion).where(
            DatasetVersion.dataset_id == dataset_id,
            DatasetVersion.version_number == version_number,
        )
    )
    return result.scalar_one_or_none()


async def load_version_entries(
    db: AsyncSession, version_id: uuid.UUID
) -> list[DatasetVersionEntry]:
    result = await db.execute(
        select(DatasetVersionEntry)
        .where(DatasetVersionEntry.version_id == version_id)
        .order_by(DatasetVersionEntry.position)
    )
    return list(result.scalars().all())


async def count_pinned_entries(
    db: AsyncSession, evaluation_id: uuid.UUID, pinned: dict[str, int]
) -> int:
    """Count the frozen entries a pinned run will actually score against.

    A pinned run's ``total_entries`` MUST come from this, not from a live
    dataset count: the frozen snapshot can hold a different entry
    count than the live dataset. With the live count, ``completed_entries``
    never reaches ``total_entries`` (or reaches it early), so the completion
    gate in ``evaluation_engine.execute_evaluation_run`` leaves the run stuck
    ``"running"`` or reports an in-flight run complete.

    Sums each pinned version's denormalized ``entry_count``, no extra query.
    Only datasets still linked to the evaluation count, as in
    ``load_pinned_entries``: a pin for a dataset unlinked since scores nothing
    and must not inflate the total.
    """
    from app.models.dataset import EvaluationDataset

    if not pinned:
        return 0

    linked_dataset_ids = (
        await db.execute(
            select(EvaluationDataset.dataset_id).where(
                EvaluationDataset.evaluation_id == evaluation_id
            )
        )
    ).scalars().all()

    total = 0
    for dataset_id in linked_dataset_ids:
        version_number = pinned.get(str(dataset_id))
        if version_number is None:
            continue
        version = await get_version(db, dataset_id, version_number)
        if version is not None:
            total += version.entry_count
    return total


async def load_pinned_entries(
    db: AsyncSession, evaluation_id: uuid.UUID, pinned: dict[str, int]
) -> list[tuple[DatasetVersionEntry, str | None, dict | None, list | None]]:
    """Resolve the frozen entries an evaluation run should score against.

    Returns an ordered list of ``(DatasetVersionEntry, dataset_instructions,
    dataset_output_schema, dataset_label_vocabulary)`` for the versions pinned in
    ``pinned`` (``{dataset_id: version_number}``), ordered by the evaluation's dataset
    ordering then entry position -- mirroring the live-entry ordering so pinned runs are
    reproducible.

    The third and fourth elements are the SNAPSHOT's ``output_schema`` /
    ``label_vocabulary``, not the live dataset's. Reading the live values
    would let a schema or vocabulary edit silently re-describe every completed run.
    """
    from app.models.dataset import EvaluationDataset

    ds_rows = (
        await db.execute(
            select(
                EvaluationDataset.dataset_id,
                Dataset.evaluation_instructions,
            )
            .join(Dataset, Dataset.id == EvaluationDataset.dataset_id)
            .where(EvaluationDataset.evaluation_id == evaluation_id)
            .order_by(EvaluationDataset.position)
        )
    ).all()

    out: list[tuple[DatasetVersionEntry, str | None, dict | None, list | None]] = []
    for dataset_id, instructions in ds_rows:
        version_number = pinned.get(str(dataset_id))
        if version_number is None:
            continue
        version = await get_version(db, dataset_id, version_number)
        if version is None:
            continue
        version_schema = getattr(version, "output_schema", None)
        version_vocabulary = getattr(version, "label_vocabulary", None)
        for entry in await load_version_entries(db, version.id):
            out.append((entry, instructions, version_schema, version_vocabulary))
    return out
