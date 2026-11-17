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

"""Prompt/persona versioning service functions."""

from __future__ import annotations

import asyncio
import difflib
import uuid

from fastapi import HTTPException
from sqlalchemy import delete, func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.agent import Agent
from app.models.evaluation import EvaluationRun
from app.models.prompt import Prompt
from app.models.prompt_version import PromptVersion
from app.models.user import User

# Evaluation-run statuses that still need their pinned snapshots intact.
# A run in any other state has either finished (results already
# materialised, snapshots no longer needed for replay) or never reached
# the executor — so retention may safely prune their referenced
# versions.  Kept as a module-level frozenset so callers can audit/extend
# without re-scanning the service code.
_NON_TERMINAL_EVALUATION_STATUSES: frozenset[str] = frozenset({"pending", "running"})

# Explicit marker persisted on ``EvaluationRun.persona_version_number`` when the
# run was created while the agent had no persona configured. Distinguishes
# "snapshotted default/no-persona" from legacy "no snapshot override".
DEFAULT_PERSONA_SNAPSHOT_MARKER = 0

# The agent row lock that numbers versions: each attempt waits at most
# ``_VERSION_LOCK_TIMEOUT`` (a ``lock_timeout``), then backs off; the attempts
# together stay well under the 30 s statement timeout.
_VERSION_LOCK_TIMEOUT = "3s"
_VERSION_LOCK_BACKOFF_SECONDS: tuple[float, ...] = (0.5, 1.0)
# ``lock_not_available`` (the ``lock_timeout`` fired) and ``query_canceled``
# (the statement timeout): the wait ran out, nothing else went wrong.
_LOCK_WAIT_EXPIRED = frozenset({"55P03", "57014"})

AGENT_VERSION_LOCK_BUSY_DETAIL = (
    "The agent is being updated by another operation; nothing was saved. Retry in a few seconds."
)


class AgentVersionLockBusyError(HTTPException):
    """The agent row was not free for versioning within the bounded wait.

    An ``HTTPException`` so a REST write answers 503 with ``Retry-After``
    without a handler of its own; the agent builder tool catches it, rolls
    its session back and hands ``detail`` to the model as a retryable answer.
    """

    def __init__(self) -> None:
        super().__init__(
            status_code=503,
            detail=AGENT_VERSION_LOCK_BUSY_DETAIL,
            headers={"Retry-After": "5"},
        )


def _lock_wait_expired(exc: DBAPIError) -> bool:
    orig = getattr(exc, "orig", None)
    code = getattr(orig, "sqlstate", None) or getattr(orig, "pgcode", None)
    return code in _LOCK_WAIT_EXPIRED


async def lock_agent_for_versioning(db: AsyncSession, agent_id: uuid.UUID) -> None:
    """Hold the agent row until the caller's commit, so version numbers never collide.

    ``FOR NO KEY UPDATE``, not ``FOR UPDATE``: the lock only serialises the
    writers that number versions. ``FOR UPDATE`` also conflicts with ``FOR KEY
    SHARE``, the lock PostgreSQL takes on the agent row for every insert of a
    row that references it (a delegation record, a run, a friction), and that
    lock is held until the inserting transaction commits - for an agent's own
    chat turn, across its model calls. The agent then could not update itself
    from its own run: the versioning lock waited out the statement timeout.

    The wait is bounded by a short ``lock_timeout``, set for this transaction
    and restored once the lock is held: first for the flush of what the caller
    already changed (its UPDATE of the row waits on the same holders; past the
    wait the transaction is lost, so :class:`AgentVersionLockBusyError` is
    raised at once and the caller rolls back), then for each attempt at the
    lock, in a savepoint so an expired attempt leaves the transaction usable
    and the next one backs off. After the last attempt the error is raised.
    """
    with db.no_autoflush:
        previous = (await db.execute(text("SELECT current_setting('lock_timeout')"))).scalar_one()
        await db.execute(
            text("SELECT set_config('lock_timeout', :timeout, true)"),
            {"timeout": _VERSION_LOCK_TIMEOUT},
        )
    try:
        await db.flush()
    except DBAPIError as exc:
        if _lock_wait_expired(exc):
            raise AgentVersionLockBusyError() from exc
        raise
    attempts = len(_VERSION_LOCK_BACKOFF_SECONDS) + 1
    try:
        for attempt in range(attempts):
            try:
                async with db.begin_nested():
                    await db.execute(select(Agent.id).where(Agent.id == agent_id).with_for_update(key_share=True))
                break
            except DBAPIError as exc:
                if not _lock_wait_expired(exc):
                    raise
                if attempt + 1 == attempts:
                    raise AgentVersionLockBusyError() from exc
                await asyncio.sleep(_VERSION_LOCK_BACKOFF_SECONDS[attempt])
    finally:
        # Each failed attempt rolled back its savepoint alone, so the
        # transaction is usable and gets its own lock_timeout back.
        with db.no_autoflush:
            await db.execute(
                text("SELECT set_config('lock_timeout', :timeout, true)"),
                {"timeout": previous},
            )


async def _versions_pinned_by_evaluation_runs(
    db: AsyncSession,
    *,
    prompt_id: uuid.UUID | None,
    agent_id: uuid.UUID | None,
) -> set[int]:
    """Return version numbers that pending/running evaluation runs depend on.

    A pinned version is one referenced by a non-terminal
    :class:`EvaluationRun` via its ``persona_version_number`` (when
    pruning persona versions for ``agent_id``) or its
    ``prompt_versions`` JSON dict keyed by ``prompt_id`` (when pruning
    prompt versions).  These versions are excluded from the retention
    prune set so that a queued evaluation can still resolve its
    deterministic-replay snapshot at execution time — without this
    guard, an aggressive ``prompt_version_retention_count`` would
    spuriously fail valid pending runs as soon as a newer version is
    created.

    The lookup loads non-terminal runs into Python (typically
    single-digit on a healthy deployment) and filters in-memory rather
    than building a JSON-path SQL query — this keeps the retention
    path portable across the JSON column type (``JSON`` vs ``JSONB``)
    and avoids a hard SQL dependency on PostgreSQL semantics.

    Best-effort: if the lookup itself raises (e.g. the evaluation
    schema is older than this code), an empty set is returned so
    retention still makes forward progress — the runtime
    ``resolve_run_snapshot_overrides`` then reports the missing
    snapshot at execute time exactly as it did before.
    """
    pinned: set[int] = set()
    try:
        stmt = select(EvaluationRun).where(
            EvaluationRun.status.in_(_NON_TERMINAL_EVALUATION_STATUSES)
        )
        if agent_id is not None:
            stmt = stmt.where(EvaluationRun.agent_id == agent_id)
        if prompt_id is not None:
            # Prompt-version pruning should not load every non-terminal
            # run in the system.  Keep the lookup portable by using a
            # coarse SQL filter here and preserve the exact per-prompt
            # key lookup in Python below.
            stmt = stmt.where(EvaluationRun.prompt_versions.is_not(None))
        result = await db.execute(stmt)
        runs = result.scalars().all()
    except Exception:
        return pinned

    prompt_id_str = str(prompt_id) if prompt_id is not None else None
    for run in runs:
        # Persona pruning (``agent_id is not None and prompt_id is None``):
        # pin the run's ``persona_version_number`` if any.
        if (
            prompt_id is None
            and agent_id is not None
            and getattr(run, "persona_version_number", None) is not None
        ):
            try:
                version_number = int(run.persona_version_number)
                if version_number != DEFAULT_PERSONA_SNAPSHOT_MARKER:
                    pinned.add(version_number)
            except (TypeError, ValueError):
                continue
        # Prompt-version pruning: look up the per-prompt entry in the
        # ``prompt_versions`` JSON map.  The map is keyed by stringified
        # UUID and stores the active version number at run-creation
        # time.
        if prompt_id_str is not None:
            prompt_versions = getattr(run, "prompt_versions", None) or {}
            if isinstance(prompt_versions, dict):
                raw_version = prompt_versions.get(prompt_id_str)
                if raw_version is not None:
                    try:
                        pinned.add(int(raw_version))
                    except (TypeError, ValueError):
                        continue
    return pinned


async def is_prompt_version_pinned_by_evaluation_runs(
    db: AsyncSession,
    *,
    prompt_id: uuid.UUID,
    version_number: int,
) -> bool:
    """Return whether a prompt version is pinned by pending/running evaluations."""
    pinned_versions = await _versions_pinned_by_evaluation_runs(
        db,
        prompt_id=prompt_id,
        agent_id=None,
    )
    return version_number in pinned_versions


async def is_agent_persona_version_pinned_by_evaluation_runs(
    db: AsyncSession,
    *,
    agent_id: uuid.UUID,
    version_number: int,
) -> bool:
    """Return whether an agent persona version is pinned by pending/running evaluations.

    Mirrors :func:`is_prompt_version_pinned_by_evaluation_runs` so admin
    deletes of persona versions are blocked while a queued evaluation
    run still depends on them — without this guard, a delete would
    succeed and any subsequent ``resolve_run_snapshot_overrides`` would
    fail the queued run with ``missing snapshot: persona vN``.
    """
    pinned_versions = await _versions_pinned_by_evaluation_runs(
        db,
        prompt_id=None,
        agent_id=agent_id,
    )
    return version_number in pinned_versions


def _normalize_commit_message(commit_message: str | None) -> str | None:
    if commit_message is None:
        return None
    cleaned = commit_message.strip()
    return cleaned or None


async def _enforce_retention(
    db: AsyncSession,
    *,
    prompt_id: uuid.UUID | None = None,
    agent_id: uuid.UUID | None = None,
) -> None:
    """Prune old unlocked versions so retention matches the documented contract.

    Semantics — kept consistent with the docstring on
    ``settings.prompt_version_retention_count`` ("N > 0 = keep latest N
    versions per series, locked versions are NEVER pruned"):

    * Locked versions are excluded from the prune candidate set and are
      preserved regardless of N.
    * Among unlocked versions, we keep the latest ``cap`` (by descending
      ``version_number``).  Because ``order_by(version_number.desc())``
      puts the active head at offset 0, ``.offset(cap)`` naturally keeps
      the top ``cap`` (including head) and prunes everything older — so
      cap=1 keeps exactly 1 unlocked version (the head), not 1 + the head
      as the previous implementation did.
    """
    cap = settings.prompt_version_retention_count
    if cap <= 0:
        return

    conditions = []
    if prompt_id is not None:
        conditions.append(PromptVersion.prompt_id == prompt_id)
    if agent_id is not None:
        conditions.append(PromptVersion.agent_id == agent_id)
    if not conditions:
        return

    # ── Exclude versions pinned by non-terminal evaluation runs ───
    # An evaluation run created with a snapshot of a specific version
    # must still find that version at execute time, even if newer
    # versions have since been created.  Without this guard, an
    # aggressive ``prompt_version_retention_count`` would spuriously
    # fail valid pending runs ("missing snapshot: persona v3") as
    # soon as a newer persona version is created.  See
    # ``_versions_pinned_by_evaluation_runs`` for the lookup semantics.
    pinned_versions = await _versions_pinned_by_evaluation_runs(
        db, prompt_id=prompt_id, agent_id=agent_id
    )

    select_stmt = (
        select(PromptVersion.id, PromptVersion.version_number)
        .where(
            *conditions,
            PromptVersion.is_locked.is_(False),
        )
        .order_by(PromptVersion.version_number.desc())
        .offset(cap)
    )
    candidate_result = await db.execute(select_stmt)
    to_prune_ids = [
        version_id
        for version_id, version_number in candidate_result.all()
        if version_number not in pinned_versions
    ]
    if to_prune_ids:
        await db.execute(delete(PromptVersion).where(PromptVersion.id.in_(to_prune_ids)))


async def _next_version_number(
    db: AsyncSession,
    *,
    prompt_id: uuid.UUID | None = None,
    agent_id: uuid.UUID | None = None,
) -> int:
    conditions = []
    if prompt_id is not None:
        conditions.append(PromptVersion.prompt_id == prompt_id)
    if agent_id is not None:
        conditions.append(PromptVersion.agent_id == agent_id)
    if not conditions:
        raise ValueError("Either prompt_id or agent_id must be provided")

    current_max = await db.scalar(
        select(func.max(PromptVersion.version_number)).where(*conditions)
    )
    return int(current_max or 0) + 1


async def create_prompt_version(
    db: AsyncSession,
    *,
    prompt_id: uuid.UUID,
    content: str,
    author_id: uuid.UUID | None,
    commit_message: str | None = None,
) -> PromptVersion:
    # Row lock avoids duplicate version numbers under concurrent updates.
    await db.execute(select(Prompt.id).where(Prompt.id == prompt_id).with_for_update())
    version_number = await _next_version_number(db, prompt_id=prompt_id)

    version = PromptVersion(
        prompt_id=prompt_id,
        version_number=version_number,
        content=content,
        author_id=author_id,
        commit_message=_normalize_commit_message(commit_message),
    )
    db.add(version)
    await db.flush()
    await _enforce_retention(db, prompt_id=prompt_id)
    return version


async def create_agent_persona_version(
    db: AsyncSession,
    *,
    agent_id: uuid.UUID,
    content: str,
    author_id: uuid.UUID | None,
    commit_message: str | None = None,
) -> PromptVersion:
    # Row lock avoids duplicate version numbers under concurrent updates.
    await lock_agent_for_versioning(db, agent_id)
    version_number = await _next_version_number(db, agent_id=agent_id)

    version = PromptVersion(
        agent_id=agent_id,
        version_number=version_number,
        content=content,
        author_id=author_id,
        commit_message=_normalize_commit_message(commit_message),
    )
    db.add(version)
    await db.flush()
    await _enforce_retention(db, agent_id=agent_id)
    return version


async def list_prompt_versions(
    db: AsyncSession,
    *,
    prompt_id: uuid.UUID,
    limit: int,
    offset: int,
) -> tuple[list[PromptVersion], int]:
    total = int(
        await db.scalar(
            select(func.count(PromptVersion.id)).where(PromptVersion.prompt_id == prompt_id)
        )
        or 0
    )

    result = await db.execute(
        select(PromptVersion)
        .where(PromptVersion.prompt_id == prompt_id)
        .order_by(PromptVersion.version_number.desc())
        .limit(limit)
        .offset(offset)
    )
    return result.scalars().all(), total


async def list_agent_persona_versions(
    db: AsyncSession,
    *,
    agent_id: uuid.UUID,
    limit: int,
    offset: int,
) -> tuple[list[PromptVersion], int]:
    total = int(
        await db.scalar(
            select(func.count(PromptVersion.id)).where(PromptVersion.agent_id == agent_id)
        )
        or 0
    )

    result = await db.execute(
        select(PromptVersion)
        .where(PromptVersion.agent_id == agent_id)
        .order_by(PromptVersion.version_number.desc())
        .limit(limit)
        .offset(offset)
    )
    return result.scalars().all(), total


async def get_prompt_version(
    db: AsyncSession,
    *,
    prompt_id: uuid.UUID,
    version_number: int,
) -> PromptVersion | None:
    result = await db.execute(
        select(PromptVersion).where(
            PromptVersion.prompt_id == prompt_id,
            PromptVersion.version_number == version_number,
        )
    )
    return result.scalar_one_or_none()


async def get_agent_persona_version(
    db: AsyncSession,
    *,
    agent_id: uuid.UUID,
    version_number: int,
) -> PromptVersion | None:
    result = await db.execute(
        select(PromptVersion).where(
            PromptVersion.agent_id == agent_id,
            PromptVersion.version_number == version_number,
        )
    )
    return result.scalar_one_or_none()


async def get_active_prompt_version_number(
    db: AsyncSession,
    *,
    prompt_id: uuid.UUID,
) -> int | None:
    value = await db.scalar(
        select(func.max(PromptVersion.version_number)).where(
            PromptVersion.prompt_id == prompt_id
        )
    )
    return int(value) if value is not None else None


async def get_active_prompt_version_numbers(
    db: AsyncSession,
    *,
    prompt_ids: list[uuid.UUID],
) -> dict[str, int]:
    if not prompt_ids:
        return {}

    rows = await db.execute(
        select(PromptVersion.prompt_id, func.max(PromptVersion.version_number))
        .where(PromptVersion.prompt_id.in_(prompt_ids))
        .group_by(PromptVersion.prompt_id)
    )

    versions: dict[str, int] = {}
    for prompt_id, version_number in rows.all():
        if prompt_id is not None and version_number is not None:
            versions[str(prompt_id)] = int(version_number)
    return versions


async def get_active_agent_persona_version_number(
    db: AsyncSession,
    *,
    agent_id: uuid.UUID,
) -> int | None:
    value = await db.scalar(
        select(func.max(PromptVersion.version_number)).where(
            PromptVersion.agent_id == agent_id
        )
    )
    return int(value) if value is not None else None


async def get_active_agent_persona_version_numbers(
    db: AsyncSession,
    *,
    agent_ids: list[uuid.UUID],
) -> dict[str, int]:
    """Batched variant of :func:`get_active_agent_persona_version_number`.

    Returns ``{agent_id_str: max_version_number}`` for *agent_ids* using
    a single ``GROUP BY agent_id`` query instead of one round-trip per
    agent — used by scheduled evaluation creation to keep its DB cost
    constant w.r.t. the agent fan-out.
    """
    if not agent_ids:
        return {}
    rows = await db.execute(
        select(PromptVersion.agent_id, func.max(PromptVersion.version_number))
        .where(PromptVersion.agent_id.in_(agent_ids))
        .group_by(PromptVersion.agent_id)
    )
    result: dict[str, int] = {}
    for agent_id, version_number in rows.all():
        if agent_id is not None and version_number is not None:
            result[str(agent_id)] = int(version_number)
    return result


async def resolve_author_names(
    db: AsyncSession,
    versions: list[PromptVersion],
) -> dict[str, str]:
    author_ids = [v.author_id for v in versions if v.author_id is not None]
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
        # Ignore '? ' helper lines emitted by ndiff.

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


async def rollback_prompt_to_version(
    db: AsyncSession,
    *,
    prompt: Prompt,
    target_version: PromptVersion,
    author_id: uuid.UUID | None,
    commit_message: str | None = None,
) -> PromptVersion:
    rollback_commit_message = _normalize_commit_message(commit_message)
    if rollback_commit_message is None:
        rollback_commit_message = (
            f"Rollback from version V{target_version.version_number}"
        )

    prompt.content = target_version.content
    return await create_prompt_version(
        db,
        prompt_id=prompt.id,
        content=target_version.content,
        author_id=author_id,
        commit_message=rollback_commit_message,
    )


async def rollback_agent_persona_to_version(
    db: AsyncSession,
    *,
    agent: Agent,
    target_version: PromptVersion,
    author_id: uuid.UUID | None,
    commit_message: str | None = None,
) -> PromptVersion:
    rollback_commit_message = _normalize_commit_message(commit_message)
    if rollback_commit_message is None:
        rollback_commit_message = (
            f"Rollback from version V{target_version.version_number}"
        )

    agent.persona = target_version.content
    return await create_agent_persona_version(
        db,
        agent_id=agent.id,
        content=target_version.content,
        author_id=author_id,
        commit_message=rollback_commit_message,
    )
