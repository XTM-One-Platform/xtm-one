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

"""Run parameter logging – queryable experimental conditions.

Each evaluation run logs an exhaustive, queryable set of key/value parameters
describing the experimental condition it was executed under: the model, the
execution mode, the scoring configuration, the pinned dataset version(s), and
the agent config-version snapshot. Params carry a ``source``:

- ``auto`` — captured by the engine from the resolved run/agent/evaluation
- ``evaluation`` — copied from ``Evaluation.custom_params``
- ``manual`` — supplied on the trigger request

On conflicting keys, precedence is **manual > evaluation > auto** and is
enforced at upsert time so the highest-precedence source wins regardless of the
order in which the sources are captured. The
``(run_id, key)`` uniqueness constraint prevents duplicates.
"""

from __future__ import annotations

import json
import uuid

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.evaluation_param import EvaluationRunParam

# Higher wins: manual overrides evaluation overrides auto.
SOURCE_PRECEDENCE: dict[str, int] = {"auto": 0, "evaluation": 1, "manual": 2}


def serialize_value(value: object) -> str | None:
    """Coerce a param value to its stored text form.

    Strings are stored verbatim; ``None`` stays ``None``; every other value is
    JSON-encoded (dicts/lists with sorted keys) so the stored form is stable and
    comparable across runs.
    """
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)


def should_overwrite(existing_source: str, new_source: str) -> bool:
    """Whether a param from ``new_source`` may replace one from ``existing_source``.

    A source overwrites another when its precedence is greater than *or equal
    to* the existing one — equality lets a re-capture from the same source
    refresh the value idempotently.
    """
    return SOURCE_PRECEDENCE.get(new_source, -1) >= SOURCE_PRECEDENCE.get(
        existing_source, -1
    )


async def upsert_param(
    db: AsyncSession,
    run_id: uuid.UUID,
    key: str,
    value: object,
    source: str,
) -> None:
    """Insert or update a single run param honouring source precedence.

    No commit — the caller owns the transaction so the whole param capture rolls
    back atomically with the run creation on failure.
    """
    result = await db.execute(
        select(EvaluationRunParam).where(
            EvaluationRunParam.run_id == run_id,
            EvaluationRunParam.key == key,
        )
    )
    existing = result.scalar_one_or_none()
    stored = serialize_value(value)
    if existing is None:
        db.add(
            EvaluationRunParam(
                run_id=run_id, key=key, value=stored, source=source
            )
        )
        return
    if should_overwrite(existing.source, source):
        existing.value = stored
        existing.source = source


async def capture_params(
    db: AsyncSession,
    run_id: uuid.UUID,
    params: dict[str, object],
    source: str,
) -> None:
    """Upsert a flat key/value mapping under a single ``source``."""
    for key, value in (params or {}).items():
        await upsert_param(db, run_id, str(key), value, source)


async def capture_params_for_runs(
    db: AsyncSession,
    run_ids: list[uuid.UUID],
    params: dict[str, object],
    source: str,
) -> None:
    """Bulk-upsert a flat key/value mapping across a batch of runs.

    One ``INSERT ... ON CONFLICT`` statement per key for the whole batch, with
    the precedence ``should_overwrite`` enforces: an existing row whose source
    outranks *source* keeps its value, an equal-or-lower one is overwritten (the
    ``<=`` in the ``WHERE`` below is ``should_overwrite``'s ``>=`` seen from the
    existing row).

    Safe for any batch of runs, already parametrized or not, because the
    conflict is resolved in the statement. Two writers of the same key in one
    request (``trigger_evaluation``'s ``params`` map and its ``reuse`` override
    can both name ``"reuse"``) resolve instead of colliding on
    ``uq_eval_run_param_key``.
    """
    if not run_ids:
        return
    # Dedup, keeping order: a duplicate `run_id` puts two VALUES rows on the same
    # conflict target in one statement, which PostgreSQL rejects with
    # `CardinalityViolation`. `ON CONFLICT` resolves only against rows
    # that existed before the statement, never a sibling VALUES row.
    run_ids = list(dict.fromkeys(run_ids))

    allowed_existing_sources = [
        s for s, p in SOURCE_PRECEDENCE.items() if p <= SOURCE_PRECEDENCE[source]
    ]
    for key, value in (params or {}).items():
        stored = serialize_value(value)
        stmt = pg_insert(EvaluationRunParam).values(
            [
                {"run_id": run_id, "key": str(key), "value": stored, "source": source}
                for run_id in run_ids
            ]
        )
        stmt = stmt.on_conflict_do_update(
            constraint="uq_eval_run_param_key",
            set_={"value": stmt.excluded.value, "source": stmt.excluded.source},
            where=EvaluationRunParam.source.in_(allowed_existing_sources),
        )
        await db.execute(stmt)


def build_auto_params(run, agent, evaluation, *, execution_mode: str) -> dict[str, object]:
    """Assemble the ``source=auto`` param set from resolved run context.

    Captures at minimum the model, execution mode, scoring configuration,
    evaluator model, per-dataset pinned version, KB/tool counts, and the agent
    config-version snapshot values.
    """
    params: dict[str, object] = {
        "model": run.model_id,
        "execution_mode": execution_mode,
        # What scores the run's quality, logged as it is rather than as the
        # evaluation's setting: an evaluation still set to the retired F1 is
        # scored by the judge too, and its new runs say so.
        "quality_source": "judge",
        "evaluator_model": evaluation.evaluator_model or "platform_default",
        "reuse": "on" if getattr(evaluation, "reuse_default", "off") == "on" else "off",
    }

    # Per-dataset pinned version. One param per linked dataset so the
    # exact scored snapshot is queryable alongside the rest of the condition.
    for dataset_id, version in (run.pinned_dataset_versions or {}).items():
        params[f"dataset_version:{dataset_id}"] = version

    if agent is not None:
        kb_count = len(agent.knowledge_base_ids or [])
        tool_count = (
            len(agent.mcp_server_ids or [])
            + len(agent.custom_tool_ids or [])
            + len(agent.active_integration_providers)
        )
        params["kb_count"] = kb_count
        params["tool_count"] = tool_count

    # Agent config-version snapshot (persona/prompt/config versions pinned on
    # the run) so a config change between runs surfaces in the param diff.
    if run.persona_version_number is not None:
        params["persona_version"] = run.persona_version_number
    for name, version in (run.agent_config_versions or {}).items():
        params[f"config_version:{name}"] = version
    for prompt_id, version in (run.prompt_versions or {}).items():
        params[f"prompt_version:{prompt_id}"] = version

    return params


async def capture_run_params(
    db: AsyncSession,
    run,
    agent,
    evaluation,
    *,
    execution_mode: str,
    manual_params: dict[str, object] | None = None,
) -> None:
    """Capture the full param set for a run (auto + evaluation + manual).

    Order does not affect the result: ``upsert_param`` resolves precedence so
    manual wins over evaluation wins over auto. No commit — caller owns it.
    """
    await capture_params(
        db, run.id, build_auto_params(run, agent, evaluation, execution_mode=execution_mode), "auto"
    )
    await capture_params(db, run.id, evaluation.custom_params or {}, "evaluation")
    if manual_params:
        await capture_params(db, run.id, manual_params, "manual")


async def get_run_params(db: AsyncSession, run_id: uuid.UUID) -> list[dict]:
    """Return a run's params as ``[{key, value, source}]`` ordered by key."""
    result = await db.execute(
        select(EvaluationRunParam)
        .where(EvaluationRunParam.run_id == run_id)
        .order_by(EvaluationRunParam.key)
    )
    return [
        {"key": p.key, "value": p.value, "source": p.source}
        for p in result.scalars().all()
    ]


def compute_param_diff(
    params_a: list[dict], params_b: list[dict]
) -> dict[str, list[dict]]:
    """Diff two runs' params into ``changed`` and ``identical``.

    A key present in both runs with the same value is ``identical``; any key
    whose values differ — including a key present in only one run (the missing
    side is ``None``) — is ``changed``. Each entry surfaces both values and both
    sources (``{key, value_a, value_b, source_a, source_b}``).
    """
    map_a = {p["key"]: p for p in params_a}
    map_b = {p["key"]: p for p in params_b}
    changed: list[dict] = []
    identical: list[dict] = []
    for key in sorted(set(map_a) | set(map_b)):
        pa = map_a.get(key)
        pb = map_b.get(key)
        entry = {
            "key": key,
            "value_a": pa["value"] if pa else None,
            "value_b": pb["value"] if pb else None,
            "source_a": pa["source"] if pa else None,
            "source_b": pb["source"] if pb else None,
        }
        if pa and pb and pa["value"] == pb["value"]:
            identical.append(entry)
        else:
            changed.append(entry)
    return {"changed": changed, "identical": identical}
