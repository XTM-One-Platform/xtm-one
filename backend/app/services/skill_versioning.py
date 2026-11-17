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

"""Skill versioning service — create / list / get / rollback.

Mirrors the surface of :mod:`app.services.prompt_versioning` for the
``AgentSkill`` model: every edit that mutates a versionable field
creates a new :class:`SkillVersion` row, the active version is
implicitly the highest ``version_number`` per skill, and a rollback
restores the parent skill's content to that snapshot AND records the
restoration as a fresh forward version (gap-tolerant) so version
history stays append-only.

Why "active = highest version_number" instead of an
``active_version_id`` pointer on ``AgentSkill``: rollback is a
destination-content reset, not a pointer swap, because the parent
skill's body is what runtime code normally reads. Mirroring the
prompt-versioning shape exactly: rollback = "create a new version whose
content equals the target version's content, then update the parent's
content to match" — which leaves the audit trail intact.

Skills DO have a version-pinned replay surface: an
evaluation run snapshots the active version of every attached skill in
``EvaluationRun.skill_versions`` and ``load_skill`` resolves that
snapshot instead of the live body. Retention and admin deletes therefore
have to spare versions a queued run still depends on — see
:func:`skill_versions_pinned_by_evaluation_runs`.
"""

from __future__ import annotations

import difflib
import logging
import uuid

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.agent_skill import AgentSkill
from app.models.evaluation import EvaluationRun
from app.models.skill_version import SkillVersion
from app.models.user import User

logger = logging.getLogger(__name__)

# Evaluation-run statuses that still need their pinned snapshots intact —
# same contract (and same values) as ``prompt_versioning``.
_NON_TERMINAL_EVALUATION_STATUSES: frozenset[str] = frozenset({"pending", "running"})


async def skill_versions_pinned_by_evaluation_runs(
    db: AsyncSession,
    *,
    skill_id: uuid.UUID,
) -> set[int]:
    """Return skill version numbers pending/running evaluation runs depend on.

    Sibling of ``prompt_versioning._versions_pinned_by_evaluation_runs``,
    reading :attr:`EvaluationRun.skill_versions` (``{skill_id: version}``).
    A queued run must still find the version it snapshotted when
    ``load_skill`` runs, so those versions are excluded from the retention
    prune set and from admin deletes.

    Non-terminal runs are typically single-digit on a healthy deployment,
    so the filtering happens in Python rather than through a JSON-path
    query — that keeps this portable across ``JSON`` / ``JSONB`` columns,
    exactly like the prompt-version sibling.

    Best-effort: if the lookup raises (e.g. an older evaluation schema
    without the column), an empty set is returned so retention still makes
    forward progress; ``resolve_run_snapshot_overrides`` then reports the
    missing snapshot at execute time.
    """
    pinned: set[int] = set()
    try:
        result = await db.execute(
            select(EvaluationRun).where(
                EvaluationRun.status.in_(_NON_TERMINAL_EVALUATION_STATUSES),
                EvaluationRun.skill_versions.is_not(None),
            )
        )
        runs = result.scalars().all()
    except Exception:
        return pinned

    skill_id_str = str(skill_id)
    for run in runs:
        skill_versions = getattr(run, "skill_versions", None) or {}
        if not isinstance(skill_versions, dict):
            continue
        raw_version = skill_versions.get(skill_id_str)
        if raw_version is None:
            continue
        try:
            pinned.add(int(raw_version))
        except (TypeError, ValueError):
            continue
    return pinned


async def is_skill_version_pinned_by_evaluation_runs(
    db: AsyncSession,
    *,
    skill_id: uuid.UUID,
    version_number: int,
) -> bool:
    """Return whether a skill version is pinned by pending/running evaluations."""
    return version_number in await skill_versions_pinned_by_evaluation_runs(
        db, skill_id=skill_id
    )


async def _enforce_retention(db: AsyncSession, *, skill_id: uuid.UUID) -> None:
    """Prune old unlocked skill versions when ``prompt_version_retention_count`` is set.

    Reuses the existing ``prompt_version_retention_count`` knob - the
    setting docstring already documents it as the "single knob" applied
    "uniformly to ALL versioned snapshot types", so skills fall under
    the same cap rather than introducing a separate setting that admins
    would have to discover and tune in parallel.

    Semantics (mirrors ``app.services.prompt_versioning._enforce_retention``):

    * ``cap <= 0`` → unlimited history, no-op.
    * Among the unlocked versions, we keep the latest ``cap`` (sorted
      by ``version_number`` descending) and prune everything older via
      ``.offset(cap)``. So ``cap=1`` keeps exactly the head, not
      "head + 1".
    * Locked versions are excluded from the prune candidate set and
      are preserved regardless of N. This is why the ``is_locked``
      column exists in the first place - without retention enforcement
      it was dead.

    * Versions pinned by a pending/running evaluation run are excluded
      too — see :func:`skill_versions_pinned_by_evaluation_runs`. A run
      snapshots the skill versions it was created with
      (``EvaluationRun.skill_versions``) and ``load_skill`` resolves that
      snapshot, so pruning a pinned version would silently degrade a
      queued replay to live content.
    """
    cap = settings.prompt_version_retention_count
    if cap <= 0:
        return

    pinned_versions = await skill_versions_pinned_by_evaluation_runs(
        db, skill_id=skill_id
    )

    candidate_result = await db.execute(
        select(SkillVersion.id, SkillVersion.version_number)
        .where(
            SkillVersion.skill_id == skill_id,
            SkillVersion.is_locked.is_(False),
        )
        .order_by(SkillVersion.version_number.desc())
        .offset(cap)
    )
    to_prune_ids = [
        version_id
        for version_id, version_number in candidate_result.all()
        if version_number not in pinned_versions
    ]
    if to_prune_ids:
        await db.execute(
            delete(SkillVersion).where(SkillVersion.id.in_(to_prune_ids))
        )


def _normalize_commit_message(message: str | None) -> str | None:
    """Trim + length-cap user-supplied commit messages to keep
    them grep-friendly in audit-log surfaces."""
    if message is None:
        return None
    trimmed = message.strip()
    if not trimmed:
        return None
    return trimmed[:500]


async def _next_version_number(db: AsyncSession, skill_id: uuid.UUID) -> int:
    """Return the next per-skill version number.

    Gap-tolerant: takes ``max(version_number) + 1`` rather than counting
    rows, so a future retention prune that removes intermediate
    versions doesn't trigger a version-number collision against the
    ``uq_skill_versions_skill_ver`` unique constraint.
    """
    result = await db.execute(
        select(func.coalesce(func.max(SkillVersion.version_number), 0)).where(
            SkillVersion.skill_id == skill_id
        )
    )
    current_max = int(result.scalar() or 0)
    return current_max + 1


async def create_skill_version(
    db: AsyncSession,
    skill: AgentSkill,
    *,
    author_id: uuid.UUID | None,
    commit_message: str | None = None,
) -> SkillVersion:
    """Snapshot the current state of *skill* as a new :class:`SkillVersion`.

    The caller is responsible for committing the surrounding
    transaction. This function only flushes the insert so the assigned
    ``id`` / ``version_number`` are available on the returned row.

    Skills with NULL ``content`` (URL-backed skills that haven't been
    fetched yet) are NOT versioned — the snapshot would be of an empty
    body, which would corrupt the audit trail. Callers should wait
    for the fetch to populate ``content`` before creating the first
    version. The defensive guard here surfaces the contract via a
    clear ``ValueError`` rather than silently creating a degenerate
    snapshot.
    """
    if skill.content is None:
        raise ValueError(
            f"Cannot version skill {skill.id}: ``content`` is NULL. "
            "URL-backed skills must be fetched before the first "
            "snapshot can be created."
        )

    # Row lock on the parent skill avoids duplicate ``version_number``
    # under concurrent edits — without this, two simultaneous create
    # calls compute the same ``current_max + 1`` and one of them
    # trips the ``uq_skill_versions_skill_ver`` unique constraint.
    # Mirrors the row-lock pattern in
    # :func:`app.services.prompt_versioning.create_prompt_version`.
    await db.execute(
        select(AgentSkill.id).where(AgentSkill.id == skill.id).with_for_update()
    )

    version = SkillVersion(
        skill_id=skill.id,
        version_number=await _next_version_number(db, skill.id),
        content=skill.content,
        description=skill.description,
        compatibility=skill.compatibility,
        author_id=author_id,
        commit_message=_normalize_commit_message(commit_message),
    )
    db.add(version)
    await db.flush()
    # Enforce retention AFTER the flush so the new row is visible to
    # the ``.offset(cap)`` query — the head is preserved, only older
    # unlocked rows are candidates for pruning. Mirrors the call
    # placement in ``app.services.prompt_versioning.create_prompt_version``.
    await _enforce_retention(db, skill_id=skill.id)
    return version


async def list_skill_versions(
    db: AsyncSession,
    skill_id: uuid.UUID,
    *,
    limit: int | None = None,
    offset: int = 0,
) -> tuple[list[SkillVersion], int]:
    """Return versions of *skill_id* newest-first.

    The ordering is stable across rollbacks because we order by
    ``version_number`` (monotonic-per-skill) rather than ``created_at``
    (which can technically tie when two rollbacks land in the same
    millisecond).
    """
    total_result = await db.execute(
        select(func.count())
        .select_from(SkillVersion)
        .where(SkillVersion.skill_id == skill_id)
    )
    total = int(total_result.scalar() or 0)

    stmt = (
        select(SkillVersion)
        .where(SkillVersion.skill_id == skill_id)
        .order_by(SkillVersion.version_number.desc())
    )
    if offset > 0:
        stmt = stmt.offset(offset)
    if limit is not None and limit > 0:
        stmt = stmt.limit(limit)
    result = await db.execute(stmt)
    return list(result.scalars().all()), total


async def get_skill_version(
    db: AsyncSession,
    skill_id: uuid.UUID,
    version_number: int,
) -> SkillVersion | None:
    """Look up a single version by ``(skill_id, version_number)``."""
    result = await db.execute(
        select(SkillVersion).where(
            SkillVersion.skill_id == skill_id,
            SkillVersion.version_number == version_number,
        )
    )
    return result.scalar_one_or_none()


async def get_active_skill_version_number(
    db: AsyncSession, skill_id: uuid.UUID
) -> int | None:
    """Return the highest version number for *skill_id*, or None when
    no version has been snapshotted yet."""
    result = await db.execute(
        select(func.max(SkillVersion.version_number)).where(
            SkillVersion.skill_id == skill_id
        )
    )
    current_max = result.scalar()
    return int(current_max) if current_max is not None else None


async def get_active_skill_version_numbers(
    db: AsyncSession,
    *,
    skill_ids: list[uuid.UUID],
) -> dict[str, int]:
    """Batched variant of :func:`get_active_skill_version_number`.

    Mirrors ``prompt_versioning.get_active_prompt_version_numbers``: one
    ``GROUP BY`` instead of one query per skill, so snapshotting an
    evaluation run stays a fixed number of round-trips no matter how many
    skills the agent has attached.  Skills with no version row are absent
    from the result (there is nothing to pin).
    """
    if not skill_ids:
        return {}

    rows = await db.execute(
        select(SkillVersion.skill_id, func.max(SkillVersion.version_number))
        .where(SkillVersion.skill_id.in_(skill_ids))
        .group_by(SkillVersion.skill_id)
    )

    versions: dict[str, int] = {}
    for skill_id, version_number in rows.all():
        if skill_id is not None and version_number is not None:
            versions[str(skill_id)] = int(version_number)
    return versions


async def rollback_skill_to_version(
    db: AsyncSession,
    skill: AgentSkill,
    target_version_number: int,
    *,
    author_id: uuid.UUID | None,
    commit_message: str | None = None,
) -> SkillVersion:
    """Restore *skill*'s content from a prior version.

    Append-only history: the restoration is itself recorded as a new
    forward version (the version row is a snapshot of the target
    version's body but with a fresh ``version_number`` and the
    ``commit_message`` augmented to identify the rollback source).
    Rolling back does NOT delete or rewrite any existing version
    row.

    Raises ``ValueError`` when the target version doesn't exist (so
    the API layer can surface a 404 rather than silently no-op).
    """
    target = await get_skill_version(db, skill.id, target_version_number)
    if target is None:
        raise ValueError(
            f"Skill {skill.id} has no version {target_version_number}"
        )

    # Restore the live skill content from the target snapshot.
    skill.content = target.content
    skill.description = target.description
    skill.compatibility = target.compatibility

    # Record the restoration as a new forward version so the audit
    # trail is append-only and tools that "show me the live history"
    # see the rollback as a discrete event (not as a silent reset of
    # the row).
    rollback_msg = _normalize_commit_message(commit_message) or (
        f"Rollback to version {target_version_number}"
    )

    # Same row-lock rationale as :func:`create_skill_version` — without
    # this, a concurrent edit landing between ``_next_version_number``
    # and the insert would trip the ``uq_skill_versions_skill_ver``
    # unique constraint.
    await db.execute(
        select(AgentSkill.id).where(AgentSkill.id == skill.id).with_for_update()
    )

    restoration = SkillVersion(
        skill_id=skill.id,
        version_number=await _next_version_number(db, skill.id),
        content=target.content,
        description=target.description,
        compatibility=target.compatibility,
        author_id=author_id,
        commit_message=rollback_msg,
    )
    db.add(restoration)
    await db.flush()
    # Enforce retention AFTER the rollback insert. Repeated rollbacks
    # would otherwise bypass the cap — each rollback creates a new
    # forward version, so without this call the unlocked-history grows
    # unbounded even when ``prompt_version_retention_count`` is set.
    # Same call placement as the create path.
    await _enforce_retention(db, skill_id=skill.id)
    logger.info(
        "Skill %s rolled back to version %d (new forward version %d)",
        skill.id,
        target_version_number,
        restoration.version_number,
    )
    return restoration


async def resolve_author_names(
    db: AsyncSession,
    versions: list[SkillVersion],
) -> dict[str, str]:
    author_ids = list({v.author_id for v in versions if v.author_id is not None})
    if not author_ids:
        return {}
    rows = await db.execute(
        select(User.id, User.full_name).where(User.id.in_(author_ids))
    )
    return {str(user_id): full_name for user_id, full_name in rows.all()}


def build_version_diff(
    *,
    from_content: str,
    to_content: str,
    from_version: int,
    to_version: int,
) -> dict:
    from_lines = from_content.splitlines()
    to_lines = to_content.splitlines()

    old_line = 1
    new_line = 1
    added = 0
    removed = 0
    lines: list[dict] = []

    for line in difflib.ndiff(from_lines, to_lines):
        marker = line[:2]
        text = line[2:]
        if marker == "- ":
            removed += 1
            lines.append(
                {
                    "type": "remove",
                    "content": text,
                    "old_line": old_line,
                    "new_line": None,
                }
            )
            old_line += 1
        elif marker == "+ ":
            added += 1
            lines.append(
                {
                    "type": "add",
                    "content": text,
                    "old_line": None,
                    "new_line": new_line,
                }
            )
            new_line += 1
        elif marker == "  ":
            lines.append(
                {
                    "type": "context",
                    "content": text,
                    "old_line": old_line,
                    "new_line": new_line,
                }
            )
            old_line += 1
            new_line += 1

    unified_diff = "\n".join(
        difflib.unified_diff(
            from_lines,
            to_lines,
            fromfile=f"v{from_version}",
            tofile=f"v{to_version}",
            lineterm="",
        )
    )

    return {
        "from_version": from_version,
        "to_version": to_version,
        "added": added,
        "removed": removed,
        "lines": lines,
        "unified_diff": unified_diff,
    }
