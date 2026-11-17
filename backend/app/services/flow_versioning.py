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

"""Flow version snapshots + retention.

Mirrors ``app/services/prompt_versioning.py``: every save writes an immutable
snapshot, locked versions are never pruned, and the row lock on the parent
prevents two concurrent saves from claiming the same version number.

Retention is a fixed constant (:data:`app.models.flow.FLOW_VERSION_RETENTION`)
rather than a setting — the issue asks for "at minimum the last 20", and there
is no operator reason to tune it per install.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.flow import FLOW_VERSION_RETENTION, Flow, FlowVersion
from app.models.user import User


def _normalize_commit_message(commit_message: str | None) -> str | None:
    if commit_message is None:
        return None
    cleaned = commit_message.strip()
    return cleaned or None


async def _enforce_retention(db: AsyncSession, flow_id: uuid.UUID) -> None:
    """Prune old unlocked versions down to :data:`FLOW_VERSION_RETENTION`.

    ``order_by(version_number.desc()).offset(cap)`` puts the active head at
    offset 0, so the newest ``cap`` unlocked versions (head included) survive
    and everything older is deleted. Locked versions are excluded from the
    candidate set entirely and are kept regardless.
    """
    result = await db.execute(
        select(FlowVersion.id)
        .where(FlowVersion.flow_id == flow_id, FlowVersion.is_locked.is_(False))
        .order_by(FlowVersion.version_number.desc())
        .offset(FLOW_VERSION_RETENTION)
    )
    to_prune = list(result.scalars().all())
    if to_prune:
        await db.execute(delete(FlowVersion).where(FlowVersion.id.in_(to_prune)))


async def _next_version_number(db: AsyncSession, flow_id: uuid.UUID) -> int:
    current_max = await db.scalar(
        select(func.max(FlowVersion.version_number)).where(
            FlowVersion.flow_id == flow_id
        )
    )
    return int(current_max or 0) + 1


async def create_flow_version(
    db: AsyncSession,
    *,
    flow_id: uuid.UUID,
    graph: dict[str, Any],
    author_id: uuid.UUID | None,
    commit_message: str | None = None,
) -> FlowVersion:
    """Snapshot *graph* as the next version of the flow."""
    # Row lock avoids duplicate version numbers under concurrent saves.
    await db.execute(select(Flow.id).where(Flow.id == flow_id).with_for_update())
    version_number = await _next_version_number(db, flow_id)

    version = FlowVersion(
        flow_id=flow_id,
        version_number=version_number,
        graph=graph,
        author_id=author_id,
        commit_message=_normalize_commit_message(commit_message),
    )
    db.add(version)
    await db.flush()
    await _enforce_retention(db, flow_id)
    return version


async def list_flow_versions(
    db: AsyncSession, flow_id: uuid.UUID
) -> list[FlowVersion]:
    """Newest first."""
    result = await db.execute(
        select(FlowVersion)
        .where(FlowVersion.flow_id == flow_id)
        .order_by(FlowVersion.version_number.desc())
    )
    return list(result.scalars().all())


async def get_flow_version(
    db: AsyncSession, flow_id: uuid.UUID, version_number: int
) -> FlowVersion | None:
    result = await db.execute(
        select(FlowVersion).where(
            FlowVersion.flow_id == flow_id,
            FlowVersion.version_number == version_number,
        )
    )
    return result.scalar_one_or_none()


async def resolve_author_names(
    db: AsyncSession, versions: list[FlowVersion]
) -> dict[str, str]:
    """Batch-resolve ``author_id`` → ``full_name`` for a version list."""
    author_ids = {v.author_id for v in versions if v.author_id is not None}
    if not author_ids:
        return {}
    result = await db.execute(
        select(User.id, User.full_name).where(User.id.in_(author_ids))
    )
    return {str(uid): name for uid, name in result.all()}


async def restore_flow_version(
    db: AsyncSession,
    *,
    flow: Flow,
    version_number: int,
    author_id: uuid.UUID | None,
    commit_message: str | None = None,
    graph: dict | None = None,
) -> FlowVersion | None:
    """Restore a previous version, snapshotting the current graph first.

    The pre-restore snapshot is what makes the operation undoable — without it,
    restoring v3 over unsaved-but-committed v7 work would destroy v7 for good.
    Returns the newly created version, or ``None`` when *version_number* does
    not exist.

    ``graph`` restores that document instead of the version's own — for a
    caller that had to adjust it before it may come back into force (the run
    budget's platform ceiling). It must be a COPY: a ``FlowVersion`` is
    an immutable record, so editing the stored graph in place would rewrite
    history rather than restore it.
    """
    target = await get_flow_version(db, flow.id, version_number)
    if target is None:
        return None

    graph = graph if graph is not None else (target.graph or {})
    message = _normalize_commit_message(commit_message) or (
        f"Restored version {version_number}"
    )
    version = await create_flow_version(
        db,
        flow_id=flow.id,
        graph=graph,
        author_id=author_id,
        commit_message=message,
    )
    flow.graph = graph
    flow.version_number = version.version_number
    return version
