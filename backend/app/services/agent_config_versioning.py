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

"""Agent config field versioning service functions."""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterable
from copy import deepcopy
from typing import Any

from fastapi import HTTPException
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.agent import Agent
from app.models.agent_config_version import (
    TRACKED_AGENT_CONFIG_FIELDS,
    AgentConfigVersion,
)
from app.models.evaluation import EvaluationRun
from app.models.prompt_version import PromptVersion
from app.models.user import User
from app.services.prompt_versioning import (
    _NON_TERMINAL_EVALUATION_STATUSES,
    build_version_diff,
    create_agent_persona_version,
    lock_agent_for_versioning,
)
from app.services.prompt_versioning import (
    _next_version_number as _next_persona_version_number,
)


async def _config_versions_pinned_by_evaluation_runs(
    db: AsyncSession,
    *,
    agent_id: uuid.UUID,
    field_name: str,
) -> set[int]:
    """Return ``AgentConfigVersion.version_number`` values pinned by
    pending/running evaluation runs of ``agent_id`` for ``field_name``.

    Mirrors :func:`prompt_versioning._versions_pinned_by_evaluation_runs`
    semantics: a queued evaluation that references config version N for
    a tracked field must still find that exact snapshot at execute time
    even if newer versions have since been created.  Best-effort —
    returns an empty set on any lookup error so retention still makes
    forward progress.
    """
    pinned: set[int] = set()
    try:
        stmt = select(EvaluationRun).where(
            EvaluationRun.agent_id == agent_id,
            EvaluationRun.status.in_(_NON_TERMINAL_EVALUATION_STATUSES),
        )
        result = await db.execute(stmt)
        runs = result.scalars().all()
    except Exception:
        return pinned

    for run in runs:
        config_versions = getattr(run, "agent_config_versions", None) or {}
        if not isinstance(config_versions, dict):
            continue
        raw_version = config_versions.get(field_name)
        if raw_version is None:
            continue
        try:
            pinned.add(int(raw_version))
        except (TypeError, ValueError):
            continue
    return pinned


async def is_agent_config_version_pinned_by_evaluation_runs(
    db: AsyncSession,
    *,
    agent_id: uuid.UUID,
    field_name: str,
    version_number: int,
) -> bool:
    """Return whether an agent config-field version is pinned by pending/running evaluations.

    Mirrors :func:`prompt_versioning.is_prompt_version_pinned_by_evaluation_runs`
    so admin deletes are blocked while a queued evaluation run still
    depends on the snapshot — without this guard, deleting a pinned
    config version would silently break the queued run at execute
    time with ``missing snapshot: <field> vN``.
    """
    ensure_tracked_field_name(field_name)
    pinned_versions = await _config_versions_pinned_by_evaluation_runs(
        db,
        agent_id=agent_id,
        field_name=field_name,
    )
    return version_number in pinned_versions


# Re-export so existing callers (`from app.services.agent_config_versioning
# import TRACKED_AGENT_CONFIG_FIELDS`) keep working — the canonical source
# now lives next to the SQLAlchemy `CheckConstraint` it must agree with.
__all__ = [
    "TRACKED_AGENT_CONFIG_FIELDS",
    "build_agent_config_version_diff",
    "create_agent_config_version",
    "ensure_tracked_field_name",
    "format_agent_config_content",
    "get_active_agent_config_version_number",
    "get_agent_config_field_value",
    "get_agent_config_version",
    "is_agent_config_version_pinned_by_evaluation_runs",
    "list_agent_config_versions",
    "resolve_author_names",
    "rollback_agent_config_to_version",
]


def ensure_tracked_field_name(field_name: str) -> str:
    if field_name not in TRACKED_AGENT_CONFIG_FIELDS:
        raise HTTPException(status_code=422, detail="Unsupported versioned config field")
    return field_name


def _normalize_commit_message(commit_message: str | None) -> str | None:
    if commit_message is None:
        return None
    cleaned = commit_message.strip()
    return cleaned or None


async def _enforce_retention(
    db: AsyncSession,
    *,
    agent_id: uuid.UUID,
    field_name: str,
) -> None:
    """Prune old unlocked versions to keep history bounded per series.

    Mirrors ``prompt_versioning._enforce_retention``: locked versions are
    preserved regardless of N, and ``cap`` is the total number of
    *unlocked* versions retained per ``(agent_id, field_name)`` series.
    ``order_by(version_number.desc())`` puts the active head at offset 0
    so ``.offset(cap)`` keeps the top ``cap`` (including head) and prunes
    everything older — cap=1 keeps exactly 1 unlocked version (the head).
    """
    cap = settings.prompt_version_retention_count
    if cap <= 0:
        return

    # Exclude config versions pinned by non-terminal evaluation runs —
    # see ``prompt_versioning._versions_pinned_by_evaluation_runs``
    # for the rationale.  Without this guard an aggressive cap would
    # spuriously fail queued evaluations that reference a freshly
    # superseded config field snapshot.
    pinned_versions = await _config_versions_pinned_by_evaluation_runs(
        db, agent_id=agent_id, field_name=field_name
    )

    candidate_result = await db.execute(
        select(AgentConfigVersion.id, AgentConfigVersion.version_number)
        .where(
            AgentConfigVersion.agent_id == agent_id,
            AgentConfigVersion.field_name == field_name,
            AgentConfigVersion.is_locked.is_(False),
        )
        .order_by(AgentConfigVersion.version_number.desc())
        .offset(cap)
    )
    to_prune_ids = [
        version_id
        for version_id, version_number in candidate_result.all()
        if version_number not in pinned_versions
    ]
    if to_prune_ids:
        await db.execute(delete(AgentConfigVersion).where(AgentConfigVersion.id.in_(to_prune_ids)))


async def _next_version_number(
    db: AsyncSession,
    *,
    agent_id: uuid.UUID,
    field_name: str,
) -> int:
    current_max = await db.scalar(
        select(func.max(AgentConfigVersion.version_number)).where(
            AgentConfigVersion.agent_id == agent_id,
            AgentConfigVersion.field_name == field_name,
        )
    )
    return int(current_max or 0) + 1


def get_agent_config_field_value(agent: Agent, field_name: str) -> Any:
    ensure_tracked_field_name(field_name)
    return getattr(agent, field_name)


def format_agent_config_content(*, field_name: str, value_json: Any) -> str:
    # Keep guardrails as plain text for better readability in diff dialogs.
    if field_name == "guardrails":
        return value_json or ""

    if value_json is None:
        return "null"
    return json.dumps(value_json, ensure_ascii=True, indent=2, sort_keys=True)


async def create_agent_config_version(
    db: AsyncSession,
    *,
    agent_id: uuid.UUID,
    field_name: str,
    value_json: Any,
    author_id: uuid.UUID | None,
    commit_message: str | None = None,
) -> AgentConfigVersion:
    ensure_tracked_field_name(field_name)
    # Row lock avoids duplicate version numbers under concurrent updates.
    await lock_agent_for_versioning(db, agent_id)
    version_number = await _next_version_number(
        db,
        agent_id=agent_id,
        field_name=field_name,
    )

    version = AgentConfigVersion(
        agent_id=agent_id,
        field_name=field_name,
        version_number=version_number,
        value_json=value_json,
        author_id=author_id,
        commit_message=_normalize_commit_message(commit_message),
    )
    db.add(version)
    await db.flush()
    await _enforce_retention(db, agent_id=agent_id, field_name=field_name)
    return version


async def list_agent_config_versions(
    db: AsyncSession,
    *,
    agent_id: uuid.UUID,
    field_name: str,
    limit: int,
    offset: int,
) -> tuple[list[AgentConfigVersion], int]:
    ensure_tracked_field_name(field_name)
    total = int(
        await db.scalar(
            select(func.count(AgentConfigVersion.id)).where(
                AgentConfigVersion.agent_id == agent_id,
                AgentConfigVersion.field_name == field_name,
            )
        )
        or 0
    )

    result = await db.execute(
        select(AgentConfigVersion)
        .where(
            AgentConfigVersion.agent_id == agent_id,
            AgentConfigVersion.field_name == field_name,
        )
        .order_by(AgentConfigVersion.version_number.desc())
        .limit(limit)
        .offset(offset)
    )
    return result.scalars().all(), total


async def get_agent_config_version(
    db: AsyncSession,
    *,
    agent_id: uuid.UUID,
    field_name: str,
    version_number: int,
) -> AgentConfigVersion | None:
    ensure_tracked_field_name(field_name)
    result = await db.execute(
        select(AgentConfigVersion).where(
            AgentConfigVersion.agent_id == agent_id,
            AgentConfigVersion.field_name == field_name,
            AgentConfigVersion.version_number == version_number,
        )
    )
    return result.scalar_one_or_none()


async def get_active_agent_config_version_number(
    db: AsyncSession,
    *,
    agent_id: uuid.UUID,
    field_name: str,
) -> int | None:
    ensure_tracked_field_name(field_name)
    value = await db.scalar(
        select(func.max(AgentConfigVersion.version_number)).where(
            AgentConfigVersion.agent_id == agent_id,
            AgentConfigVersion.field_name == field_name,
        )
    )
    return int(value) if value is not None else None


async def get_active_agent_config_version_numbers(
    db: AsyncSession,
    *,
    agent_ids: list[uuid.UUID],
) -> dict[str, dict[str, int]]:
    """Batched variant of :func:`get_active_agent_config_version_number`.

    Returns ``{agent_id_str: {field_name: max_version_number}}`` for all
    *agent_ids* using a single ``GROUP BY agent_id, field_name`` query
    instead of one round-trip per ``(agent, field)`` pair. This keeps
    scheduled evaluation creation (``autonomous.py``) at O(1) DB calls
    per snapshot type even when many agents are seeded into the same
    evaluation.

    Only ``TRACKED_AGENT_CONFIG_FIELDS`` are surfaced in the result;
    other field rows in the table (if any) are filtered out so callers
    can rely on the same shape that the per-agent helper produces.
    """
    if not agent_ids:
        return {}
    rows = await db.execute(
        select(
            AgentConfigVersion.agent_id,
            AgentConfigVersion.field_name,
            func.max(AgentConfigVersion.version_number),
        )
        .where(AgentConfigVersion.agent_id.in_(agent_ids))
        .group_by(AgentConfigVersion.agent_id, AgentConfigVersion.field_name)
    )
    tracked = set(TRACKED_AGENT_CONFIG_FIELDS)
    result: dict[str, dict[str, int]] = {}
    for agent_id, field_name, version_number in rows.all():
        if agent_id is None or field_name is None or version_number is None:
            continue
        if field_name not in tracked:
            continue
        result.setdefault(str(agent_id), {})[field_name] = int(version_number)
    return result


async def resolve_author_names(
    db: AsyncSession,
    versions: list[AgentConfigVersion],
) -> dict[str, str]:
    author_ids = [v.author_id for v in versions if v.author_id is not None]
    if not author_ids:
        return {}
    rows = await db.execute(select(User.id, User.full_name).where(User.id.in_(author_ids)))
    return {str(user_id): full_name for user_id, full_name in rows.all()}


def build_agent_config_version_diff(
    *,
    field_name: str,
    from_value_json: Any,
    to_value_json: Any,
    from_version: int,
    to_version: int,
) -> dict:
    return build_version_diff(
        from_content=format_agent_config_content(
            field_name=field_name,
            value_json=from_value_json,
        ),
        to_content=format_agent_config_content(
            field_name=field_name,
            value_json=to_value_json,
        ),
        from_version=from_version,
        to_version=to_version,
    )


async def rollback_agent_config_to_version(
    db: AsyncSession,
    *,
    agent: Agent,
    field_name: str,
    target_version: AgentConfigVersion,
    author_id: uuid.UUID | None,
    commit_message: str | None = None,
) -> AgentConfigVersion:
    ensure_tracked_field_name(field_name)

    rollback_commit_message = _normalize_commit_message(commit_message)
    if rollback_commit_message is None:
        rollback_commit_message = (
            f"Rollback from version V{target_version.version_number}"
        )

    setattr(agent, field_name, target_version.value_json)
    return await create_agent_config_version(
        db,
        agent_id=agent.id,
        field_name=field_name,
        value_json=target_version.value_json,
        author_id=author_id,
        commit_message=rollback_commit_message,
    )


# ── One rule for the paths that write an agent ───────────────────────
#
# The REST create / update / duplicate / import (and the catalog clone, through
# the import's seeding) and the agent builder's create_agent / update_agent /
# clone_agent record the agent's history through these functions, and so does
# the built-in agent seed (``agents/seed.py``) when it creates or
# rewrites a built-in agent on boot; the persona improvement loop and the
# rollbacks write their own versions.

#: Commit message of the versions a new agent starts its history with.
INITIAL_COMMIT_MESSAGE = "Initial version"

#: Commit message of the version recording what an agent held before its
#: first recorded change, when nothing recorded it at creation.
BASELINE_COMMIT_MESSAGE = "Version before the first recorded change"


def versioned_agent_values(agent: Agent) -> dict[str, Any]:
    """The persona and every tracked config field, as they are now.

    Taken before a write and handed back to :func:`record_agent_version_changes`.
    A copy, so a value the write changes in place still compares as changed.
    """
    return deepcopy({
        "persona": agent.persona,
        **{name: get_agent_config_field_value(agent, name) for name in TRACKED_AGENT_CONFIG_FIELDS},
    })


async def seed_initial_agent_versions(
    db: AsyncSession, *, agent: Agent, author_id: uuid.UUID | None
) -> None:
    """The "Initial version" of a new agent's persona and set tracked fields."""
    if agent.persona is not None:
        await create_agent_persona_version(
            db,
            agent_id=agent.id,
            content=agent.persona,
            author_id=author_id,
            commit_message=INITIAL_COMMIT_MESSAGE,
        )
    for field_name in TRACKED_AGENT_CONFIG_FIELDS:
        value = get_agent_config_field_value(agent, field_name)
        if value is None:
            continue
        await create_agent_config_version(
            db,
            agent_id=agent.id,
            field_name=field_name,
            value_json=value,
            author_id=author_id,
            commit_message=INITIAL_COMMIT_MESSAGE,
        )


async def record_agent_version_changes(
    db: AsyncSession,
    *,
    agent: Agent,
    before: dict[str, Any],
    written: Iterable[str] | None = None,
    author_id: uuid.UUID | None,
    commit_message: str | None = None,
    record_baseline: bool = True,
) -> tuple[PromptVersion | None, dict[str, int]]:
    """Version every versioned value a write changed.

    *before* is :func:`versioned_agent_values` taken before the write.
    *written*, when given, names the fields the caller set (``persona`` and
    tracked field names; others are ignored), so a value the write did not
    ask for - a legacy value the REST update clamps on the way - is not
    versioned as a change; ``None`` versions every value that changed.

    The baseline rule, one for the persona and the config fields: a changed
    value with no version yet (an agent created by a path that did not seed
    it, or a value unset at creation) first gets its previous state recorded,
    unset included - ``None`` for a config field (the platform default, which
    a rollback restores), an empty persona for an agent that had none - so
    the first change never loses what it replaced. *record_baseline* is
    ``False`` only where that previous state is the agent's creation default
    (``create_agent`` configuring the agent it just created). Returns the
    persona version created, if any, and the version number of each config
    field versioned.
    """
    written = None if written is None else set(written)
    persona_changed = (
        (written is None or "persona" in written)
        and agent.persona is not None
        and agent.persona != (before.get("persona") or "")
    )
    changed_fields = [
        name
        for name in TRACKED_AGENT_CONFIG_FIELDS
        if (written is None or name in written)
        and get_agent_config_field_value(agent, name) != before.get(name)
    ]
    if not persona_changed and not changed_fields:
        return None, {}
    # Held until the caller's commit, before the baseline checks below: two
    # first edits of a history-less agent must not both record a baseline.
    # Bounded: raises AgentVersionLockBusyError past a few seconds.
    await lock_agent_for_versioning(db, agent.id)

    persona_version = None
    if persona_changed:
        if record_baseline and await _next_persona_version_number(db, agent_id=agent.id) == 1:
            await create_agent_persona_version(
                db,
                agent_id=agent.id,
                content=before.get("persona") or "",
                author_id=None,
                commit_message=BASELINE_COMMIT_MESSAGE,
            )
        persona_version = await create_agent_persona_version(
            db,
            agent_id=agent.id,
            content=agent.persona,
            author_id=author_id,
            commit_message=commit_message,
        )

    config_versions: dict[str, int] = {}
    for field_name in changed_fields:
        if record_baseline and await _next_version_number(
            db, agent_id=agent.id, field_name=field_name
        ) == 1:
            await create_agent_config_version(
                db,
                agent_id=agent.id,
                field_name=field_name,
                value_json=before.get(field_name),
                author_id=None,
                commit_message=BASELINE_COMMIT_MESSAGE,
            )
        version = await create_agent_config_version(
            db,
            agent_id=agent.id,
            field_name=field_name,
            value_json=get_agent_config_field_value(agent, field_name),
            author_id=author_id,
            commit_message=commit_message,
        )
        config_versions[field_name] = version.version_number
    return persona_version, config_versions
