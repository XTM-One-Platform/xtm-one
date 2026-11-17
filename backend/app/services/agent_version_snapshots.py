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

"""The persona, prompt, config-field and skill versions an agent runs on now.

An evaluation run pins them so it can be replayed on exactly the versions it
measured, the candidate run of an approved persona rewrite included.
:func:`resolve_agent_version_snapshots` reads them for many agents in a fixed
number of queries, :func:`resolve_agent_version_snapshot` for one.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent import Agent


async def resolve_agent_version_snapshots(
    db: AsyncSession,
    agent_ids: list[uuid.UUID],
) -> dict[
    uuid.UUID,
    tuple[
        int | None,
        dict[str, int] | None,
        dict[str, int | None] | None,
        dict[str, int] | None,
    ],
]:
    """Batched resolver for persona / prompt / config-field / skill versions.

    Returns
    ``{agent_id: (persona_version, prompt_versions, config_versions, skill_versions)}``
    for every *agent_id*, using a fixed number of DB round-trips
    regardless of the number of agents:

    * 1 query for ``Agent.prompt_ids`` + ``Agent.skill_ids`` (composite ``IN``)
    * 1 batched call to ``get_active_agent_persona_version_numbers``
      (single ``GROUP BY agent_id``)
    * 1 batched call to ``get_active_prompt_version_numbers`` for the
      union of every agent's prompt ids (single ``GROUP BY prompt_id``)
    * 1 batched call to ``get_active_agent_config_version_numbers``
      (single ``GROUP BY agent_id, field_name``)
    * 1 batched call to ``get_active_skill_version_numbers`` for the union
      of every agent's skill ids (single ``GROUP BY skill_id``)

    ``persona_version`` may be
    ``DEFAULT_PERSONA_SNAPSHOT_MARKER`` when the agent has no persona at
    run-creation time, which preserves deterministic replay of the
    "default/no-persona" state.

    ``config_versions`` values may be ``None`` for tracked fields that were
    explicitly unset at run-creation time (default marker), mirroring the
    deterministic-replay semantics used by run snapshots.

    This replaces the previous per-agent loop which issued
    ``1 + 1 + 1 + len(TRACKED_AGENT_CONFIG_FIELDS)`` round-trips per
    agent — quadratic-ish when an evaluation fan-out triggered tens of
    agents.
    """
    if not agent_ids:
        return {}

    from app.services.agent_config_versioning import (
        TRACKED_AGENT_CONFIG_FIELDS,
        get_active_agent_config_version_numbers,
    )
    from app.services.prompt_versioning import (
        DEFAULT_PERSONA_SNAPSHOT_MARKER,
        get_active_agent_persona_version_numbers,
        get_active_prompt_version_numbers,
    )
    from app.services.skill_versioning import get_active_skill_version_numbers

    # De-duplicate while keeping a stable iteration order for the result
    # dict (mostly cosmetic — callers index by id).
    unique_agent_ids: list[uuid.UUID] = list(dict.fromkeys(agent_ids))

    # 1. ``Agent.prompt_ids`` / ``Agent.skill_ids`` for every agent in one query.
    prompt_ids_rows = await db.execute(
        select(
            Agent.id,
            Agent.prompt_ids,
            Agent.skill_ids,
            Agent.persona,
            Agent.guardrails,
            Agent.temperature,
            Agent.reasoning_effort,
            Agent.max_iterations,
        ).where(Agent.id.in_(unique_agent_ids))
    )
    agent_prompt_uuid_map: dict[uuid.UUID, list[uuid.UUID]] = {}
    agent_skill_uuid_map: dict[uuid.UUID, list[uuid.UUID]] = {}
    agent_has_no_persona_map: dict[uuid.UUID, bool] = {}
    agent_null_config_fields_map: dict[uuid.UUID, set[str]] = {}
    all_prompt_uuids: set[uuid.UUID] = set()
    all_skill_uuids: set[uuid.UUID] = set()
    tracked_fields = list(TRACKED_AGENT_CONFIG_FIELDS)
    for row in prompt_ids_rows.all():
        (
            agent_id,
            prompt_ids_raw,
            skill_ids_raw,
            persona_raw,
            *config_field_values,
        ) = tuple(row)
        prompt_uuid_ids: list[uuid.UUID] = []
        for pid in prompt_ids_raw or []:
            try:
                prompt_uuid_ids.append(uuid.UUID(str(pid)))
            except (TypeError, ValueError):
                continue
        agent_prompt_uuid_map[agent_id] = prompt_uuid_ids
        all_prompt_uuids.update(prompt_uuid_ids)
        skill_uuid_ids: list[uuid.UUID] = []
        for sid in skill_ids_raw or []:
            try:
                skill_uuid_ids.append(uuid.UUID(str(sid)))
            except (TypeError, ValueError):
                continue
        agent_skill_uuid_map[agent_id] = skill_uuid_ids
        all_skill_uuids.update(skill_uuid_ids)
        agent_has_no_persona_map[agent_id] = not (
            isinstance(persona_raw, str) and persona_raw.strip()
        )
        null_fields: set[str] = set()
        if len(config_field_values) >= len(tracked_fields):
            for index, field_name in enumerate(tracked_fields):
                if config_field_values[index] is None:
                    null_fields.add(field_name)
        agent_null_config_fields_map[agent_id] = null_fields

    # 2. Persona, prompt and config max-versions in three batched calls.
    persona_versions_by_agent = await get_active_agent_persona_version_numbers(
        db, agent_ids=unique_agent_ids
    )
    prompt_versions_by_prompt = await get_active_prompt_version_numbers(
        db, prompt_ids=list(all_prompt_uuids)
    )
    config_versions_by_agent = await get_active_agent_config_version_numbers(
        db, agent_ids=unique_agent_ids
    )
    skill_versions_by_skill = await get_active_skill_version_numbers(
        db, skill_ids=list(all_skill_uuids)
    )

    # 3. Assemble per-agent tuples in the shape callers expect.
    snapshots: dict[
        uuid.UUID,
        tuple[
            int | None,
            dict[str, int] | None,
            dict[str, int | None] | None,
            dict[str, int] | None,
        ],
    ] = {}
    for agent_id in unique_agent_ids:
        persona_version = persona_versions_by_agent.get(str(agent_id))
        if (
            persona_version is None
            and agent_has_no_persona_map.get(agent_id, False)
        ):
            persona_version = DEFAULT_PERSONA_SNAPSHOT_MARKER
        prompt_versions = {
            str(pid): version
            for pid in agent_prompt_uuid_map.get(agent_id, [])
            if (version := prompt_versions_by_prompt.get(str(pid))) is not None
        }
        config_versions: dict[str, int | None] = dict(
            config_versions_by_agent.get(str(agent_id), {})
        )
        for field_name in agent_null_config_fields_map.get(agent_id, set()):
            config_versions.setdefault(field_name, None)
        # Skills with no snapshotted version yet (URL-backed skills that were
        # never versioned) are simply absent: there is nothing to pin, and
        # ``load_skill`` falls back to live content for them.
        skill_versions = {
            str(sid): version
            for sid in agent_skill_uuid_map.get(agent_id, [])
            if (version := skill_versions_by_skill.get(str(sid))) is not None
        }
        snapshots[agent_id] = (
            persona_version,
            (prompt_versions or None),
            (config_versions or None),
            (skill_versions or None),
        )
    return snapshots


async def resolve_agent_version_snapshot(
    db: AsyncSession,
    agent_id: uuid.UUID,
) -> tuple[
    int | None,
    dict[str, int] | None,
    dict[str, int | None] | None,
    dict[str, int] | None,
]:
    """Resolve persona, prompt, and config-field versions active for an agent.

    Thin single-agent wrapper around :func:`resolve_agent_version_snapshots`
    for callers that only need one agent's snapshot. New call sites should
    prefer the batched variant directly; this wrapper exists to keep the
    test surface stable and to make one-shot lookups ergonomic.
    """
    snapshots = await resolve_agent_version_snapshots(db, [agent_id])
    return snapshots.get(agent_id, (None, None, None, None))
