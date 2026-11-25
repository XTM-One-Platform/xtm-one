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

"""Platform Health's evaluation tools (#8257, PRD section 8): ``build_dataset_from_traces``,
``run_health_evaluation``, ``get_health_evaluation``, ``compare_health_runs``.

The built-in group ``health_evaluation`` for Platform Health's agents, and the
same functions on the platform MCP server (``/mcp/xtm``). One access rule on
every surface (:func:`evaluation_tool_refusal`): Platform Health available, and
the caller Platform Health, or an administrator while the evaluation surface is
on (decision B9: in XTM One mode no person launches an evaluation). The
targets' rule is the services' own, read on the rows: the supervision scope,
consent, a Platform Health-built dataset or evaluation. Refusals are generic; the
reason is logged. Values read from production are data in the results.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.builtin_tool_aliases import RENAMED_BUILTIN_FUNCTIONS
from app.services.health import audit_actions
from app.tools.base import CallableToolExecutor, ToolDefinition

logger = logging.getLogger(__name__)

#: Its functions belong to Platform Health's one internal group (#1943).
EVALUATION_SUITE_ID = "health_specialists"
_SOURCE = f"builtin:{EVALUATION_SUITE_ID}"
REFUSED = "Refused: Platform Health evaluation is not available to this caller for this target."


def _former_names(current: str) -> tuple[str, ...]:
    """The [DEPRECATED] ``*_concierge_*`` names of *current*, declared as its aliases."""
    return tuple(former for former, name in RENAMED_BUILTIN_FUNCTIONS.items() if name == current)


async def evaluation_tool_refusal(db: AsyncSession, user_id: str | None) -> str | None:
    """Why the caller may not use the Platform Health evaluation tools (logged), ``None`` when it may."""
    from app.models.user import User
    from app.services.evaluation.availability import evaluation_enabled
    from app.services.health.availability import health_enabled
    from app.services.health.identity import in_health_run

    if not health_enabled():
        return "Platform Health is not available"
    if in_health_run():
        return None
    try:
        caller = await db.get(User, uuid.UUID(str(user_id))) if user_id else None
    except (TypeError, ValueError):
        caller = None
    if caller is None or not caller.is_admin or getattr(caller, "is_active", True) is False:
        return "the caller is neither Platform Health nor an administrator"
    if not evaluation_enabled():
        return "the evaluation surface is off for people"
    return None


def _uuid(value: Any) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value).strip())
    except (TypeError, ValueError, AttributeError):
        return None


def _uuids(value: Any) -> list[uuid.UUID] | None:
    """A list of ids (a JSON array, a comma list or a list), ``None`` when one is not an id."""
    if value in (None, ""):
        return []
    if isinstance(value, str):
        try:
            value = json.loads(value) if value.strip().startswith("[") else value.split(",")
        except ValueError:
            return None
    if not isinstance(value, list):
        return None
    parsed = [_uuid(v) for v in value if str(v).strip()]
    return None if any(p is None for p in parsed) else parsed  # type: ignore[return-value]


def build_health_evaluation_tools(
    db: AsyncSession, user_id: str | None, *, run_id: str | None = None
) -> tuple[list[ToolDefinition], CallableToolExecutor]:
    """The Platform Health evaluation functions acting for *user_id* (``None`` in a Platform Health run),
    in the assignment run *run_id*, if any: a launch in a Platform Health run reserves on its cycle."""
    from app.services.evaluation.launch import LaunchRefusedError
    from app.services.health import budget, datasets, evaluations
    from app.services.health.consent import ConsentDeniedError

    executor = CallableToolExecutor()

    async def _refusal() -> str | None:
        reason = await evaluation_tool_refusal(db, user_id)
        if reason is not None:
            logger.info("Platform Health evaluation tool refused for %s: %s", user_id or "Platform Health", reason)
            return REFUSED
        return None

    async def _cycle_id() -> uuid.UUID | None:
        from app.services.health.identity import in_health_run
        from app.services.health.memory_steward_deps import run_cycle_id

        run = _uuid(run_id) if run_id and in_health_run() else None
        return await run_cycle_id(db, run) if run is not None else None

    async def _audit_admin(action: str, entity_type: str, entity_id: uuid.UUID, summary: str) -> None:
        from app.models.user import User
        from app.services.audit import log_audit
        from app.services.health.identity import in_health_run

        if in_health_run():
            return
        caller = await db.get(User, uuid.UUID(str(user_id)))
        await log_audit(db, user=caller, action=action, entity_type=entity_type, entity_id=entity_id, summary=summary)

    async def _build_dataset_from_traces(
        target_type: str,
        target_id: str,
        max_entries: int = 50,
        negative: bool = True,
        failed: bool = True,
        positive: bool = True,
        since: str | None = None,
        dataset_id: str | None = None,
    ) -> str:
        if (refused := await _refusal()) is not None:
            return refused
        target, dataset = _uuid(target_id), _uuid(dataset_id) if dataset_id else None
        if target_type not in ("agent", "assignment", "flow") or target is None or (dataset_id and dataset is None):
            return "target_type is agent, assignment or flow; target_id and dataset_id are ids."
        try:
            since_at = datetime.fromisoformat(since) if since else None
            limit = int(max_entries)
            # A date or time with no offset is UTC, never the server's zone.
            if since_at is not None and since_at.tzinfo is None:
                since_at = since_at.replace(tzinfo=UTC)
        except (TypeError, ValueError):
            return "`since` is an ISO 8601 date; `max_entries` is a number."
        if not all(isinstance(flag, bool) for flag in (negative, failed, positive)):
            return "`negative`, `failed` and `positive` are booleans."
        try:
            result = await datasets.build_dataset_from_traces(
                db,
                target_type=target_type,  # type: ignore[arg-type]
                target_id=target,
                max_entries=limit,
                negative=negative,
                failed=failed,
                positive=positive,
                since=since_at,
                dataset_id=dataset,
            )
        except (datasets.DatasetBuildError, ConsentDeniedError) as exc:
            await db.rollback()
            logger.info("build_dataset_from_traces refused: %s", exc)
            return REFUSED
        await _audit_admin(audit_actions.DATASET_BUILD, "dataset", result.dataset_id, "Built a dataset from production")
        await db.commit()
        return json.dumps({"dataset_id": str(result.dataset_id), "added": result.added, "skipped": result.skipped})

    async def _run_health_evaluation(
        dataset_id: str, agent_ids: Any = None, flow_ids: Any = None, model_ids: Any = None, name: str | None = None
    ) -> str:
        if (refused := await _refusal()) is not None:
            return refused
        dataset, agents, flows = _uuid(dataset_id), _uuids(agent_ids), _uuids(flow_ids)
        models = model_ids if isinstance(model_ids, list) else []
        if dataset is None or agents is None or flows is None or not all(isinstance(m, str) for m in models):
            return "dataset_id, agent_ids and flow_ids are ids; model_ids is a list of model ids."
        try:
            launch = await evaluations.launch_health_evaluation(
                db,
                dataset_id=dataset,
                subject_agent_ids=agents,
                subject_flow_ids=flows,
                model_ids=models,
                name=name,
                cycle_id=await _cycle_id(),
            )
        except budget.BudgetRefusedError as exc:
            await db.rollback()
            return f"Not launched: {exc.message}"
        except evaluations.HealthEvaluationError as exc:
            await db.rollback()
            logger.info("run_health_evaluation refused: %s", exc)
            return REFUSED
        except LaunchRefusedError as exc:
            await db.rollback()
            return f"Not launched: {exc.detail}"
        await _audit_admin(
            audit_actions.EVALUATION_LAUNCH, "evaluation", launch.evaluation_id, "Launched a Platform Health evaluation"
        )
        await db.commit()
        return json.dumps({"evaluation_id": str(launch.evaluation_id), "run_ids": [str(r) for r in launch.run_ids]})

    async def _get_health_evaluation(evaluation_id: str) -> str:
        if (refused := await _refusal()) is not None:
            return refused
        evaluation = _uuid(evaluation_id)
        if evaluation is None:
            return REFUSED
        try:
            runs = await evaluations.health_evaluation_runs(db, evaluation)
        except evaluations.HealthEvaluationError:
            return REFUSED
        return json.dumps(
            {
                "evaluation_id": str(evaluation),
                "runs": [
                    {
                        "run_id": str(run.id),
                        "subject": run.subject_kind,
                        "subject_id": str(run.subject_id),
                        "model_id": run.model_id,
                        "status": run.status,
                        "entries": f"{run.completed_entries}/{run.total_entries}",
                        "quality_score": run.quality_score,
                        "performance_score": run.performance_score,
                        "judge_score": run.score,
                        "withdrawn_entries": (run.headline_details or {}).get("withdrawn_entries", 0),
                        "error": (run.error or "")[:300] or None,
                    }
                    for run in runs
                ],
            }
        )

    async def _compare_health_runs(baseline_run_id: str, candidate_run_id: str) -> str:
        if (refused := await _refusal()) is not None:
            return refused
        baseline, candidate = _uuid(baseline_run_id), _uuid(candidate_run_id)
        if baseline is None or candidate is None:
            return REFUSED
        try:
            comparison = await evaluations.compare_health_runs(db, baseline_run_id=baseline, candidate_run_id=candidate)
        except evaluations.HealthEvaluationError:
            return REFUSED
        return json.dumps(comparison.to_dict())

    handlers = {
        "build_dataset_from_traces": _build_dataset_from_traces,
        "run_health_evaluation": _run_health_evaluation,
        "get_health_evaluation": _get_health_evaluation,
        "compare_health_runs": _compare_health_runs,
    }
    for name, handler in handlers.items():
        executor.register(name, handler)
        # The [DEPRECATED] ``*_concierge_*`` names run the same handlers for a
        # caller holding this executor directly; no definition is offered under them.
        for former in _former_names(name):
            executor.register(former, handler)
    return _DEFINITIONS, executor


_ID = {"type": "string", "format": "uuid"}
_IDS = {"type": "array", "items": _ID}
_DEFINITIONS = [
    ToolDefinition(
        name="build_dataset_from_traces",
        description=(
            "Build an evaluation dataset from a target's consented production work: negatively rated answers and "
            "failed runs become cases to fix, positively rated answers (completed runs for a flow) non-regression "
            "cases, each with the tool calls it recorded. Only opted-in targets of the supervision scope, never an "
            "opted-out user's content. With dataset_id, adds the new cases to that Platform Health dataset. Returns the "
            "dataset id and the counts added and left out."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "target_type": {"type": "string", "enum": ["agent", "assignment", "flow"]},
                "target_id": _ID,
                "max_entries": {"type": "integer", "minimum": 1, "maximum": 200, "default": 50},
                "negative": {"type": "boolean", "default": True},
                "failed": {"type": "boolean", "default": True},
                "positive": {"type": "boolean", "default": True},
                "since": {"type": "string", "description": "ISO 8601 date: only work since then."},
                "dataset_id": _ID,
            },
            "required": ["target_type", "target_id"],
        },
        source=_SOURCE,
    ),
    ToolDefinition(
        name="run_health_evaluation",
        description=(
            "Evaluate sandbox copies of agents and flows (create_sandbox_copy; never production ones) on a Platform Health dataset. "
            "Without model_ids each agent runs on its own model; a flow runs on its nodes' models. Returns the "
            "evaluation id and its run ids; read them with get_health_evaluation."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "dataset_id": _ID,
                "agent_ids": _IDS,
                "flow_ids": _IDS,
                "model_ids": {"type": "array", "items": {"type": "string"}},
                "name": {"type": "string"},
            },
            "required": ["dataset_id"],
        },
        source=_SOURCE,
        aliases=_former_names("run_health_evaluation"),
    ),
    ToolDefinition(
        name="get_health_evaluation",
        description="A Platform Health evaluation's runs: subject, model, status, progress and headline scores.",
        input_schema={"type": "object", "properties": {"evaluation_id": _ID}, "required": ["evaluation_id"]},
        source=_SOURCE,
        aliases=_former_names("get_health_evaluation"),
    ),
    ToolDefinition(
        name="compare_health_runs",
        description=(
            "Compare two completed runs of one Platform Health evaluation: the quality verdict against the noise margin "
            "(progression, neutral, regression, or none with the reason), every compared score, cost, tokens and "
            "latency, and how many writes a model simulated in each. State the simulated writes beside any verdict."
        ),
        input_schema={
            "type": "object",
            "properties": {"baseline_run_id": _ID, "candidate_run_id": _ID},
            "required": ["baseline_run_id", "candidate_run_id"],
        },
        source=_SOURCE,
        aliases=_former_names("compare_health_runs"),
    ),
]
