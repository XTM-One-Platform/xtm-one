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

"""The Experimenter's tools (Platform Health, #8259).

The ``health_experiments`` group: ``list_experiments``, ``get_experiment``
(reads), ``run_experiment``, ``propose_from_experiment`` (Platform Health's
Experimenter only: the one actor that launches an experiment and files a
proposal). Same access rule and helpers as the Quality Analyst's
(``health_quality.py``).

No tool here writes an agent, a skill or an assignment: a variant lives in its
experiment row and in sandbox copies, and the only path to production is the
proposal its managers approve (level 3).
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from app.services.health import text_kinds  # noqa: F401 - registers level 6's kinds
from app.tools.base import CallableToolExecutor, ToolDefinition

from .health_quality import RefusalError, build_group, finding_for, parse_uuid, untrusted_json

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.services.health.access import Reader

#: Its functions belong to Platform Health's one internal group (#1943).
EXPERIMENTS_GROUP_ID = "health_specialists"

#: The read functions of the group: the only ones the platform MCP server serves.
EXPERIMENTS_READ_TOOLS: frozenset[str] = frozenset({"list_experiments", "get_experiment"})


async def _experiment_for(db: AsyncSession, reader: Reader, experiment_id: Any):
    from app.models.health_experiment import HealthExperiment

    experiment = await db.get(HealthExperiment, parse_uuid(experiment_id, "experiment_id"))
    if experiment is None:
        raise RefusalError("Error: experiment not found.")
    try:
        finding, access = await finding_for(db, reader, experiment.finding_id)
    except RefusalError:
        raise RefusalError("Error: experiment not found.") from None
    return experiment, finding, access


async def _list_experiments(
    db: AsyncSession, reader: Reader, finding_id: str = "", status: str = "", limit: int = 20
) -> str:
    from sqlalchemy import select

    from app.models.health_experiment import EXPERIMENT_STATUSES, HealthExperiment
    from app.services.health.experiments import experiment_payload

    finding, access = await finding_for(db, reader, finding_id)
    query = select(HealthExperiment).where(HealthExperiment.finding_id == finding.id)
    if status:
        if status not in EXPERIMENT_STATUSES:
            raise RefusalError(f"Error: status must be one of: {', '.join(EXPERIMENT_STATUSES)}.")
        query = query.where(HealthExperiment.status == status)
    rows = (
        (await db.execute(query.order_by(HealthExperiment.created_at.desc()).limit(max(1, min(int(limit or 20), 50)))))
        .scalars()
        .all()
    )
    return untrusted_json(
        {"count": len(rows), "experiments": [experiment_payload(e, content=access.content) for e in rows]},
        "Platform Health experiments",
    )


async def _get_experiment(db: AsyncSession, reader: Reader, experiment_id: str = "") -> str:
    from app.services.health.experiments import experiment_payload, settle

    experiment, _finding, access = await _experiment_for(db, reader, experiment_id)
    if reader.write_refusal("experimenter") is None:
        # Settling books the spend and records the result: the Experimenter's write.
        await settle(db, experiment)
        await db.commit()
    return untrusted_json(experiment_payload(experiment, content=access.content), "Platform Health experiment")


async def _run_experiment(
    db: AsyncSession,
    reader: Reader,
    experiment_id: str = "",
    max_cases: int = 20,
) -> str:
    from app.services.health.diagnosis import window_figures
    from app.services.health.experiments import experiment_payload, run

    refusal = reader.write_refusal("experimenter")
    if refusal:
        return refusal
    experiment, _finding, access = await _experiment_for(db, reader, experiment_id)
    # An agent's recent runs size the experiment; a flow kind sizes on the defaults.
    signals = await window_figures(db, reader, access.agent.id) if access.agent is not None else {}
    await run(
        db,
        experiment=experiment,
        content_readable=access.content,
        signals=signals,
        requested_cases=max_cases,
        author_agent_id=getattr(reader.principal, "agent_id", None),
        cycle_id=reader.cycle_id,
    )
    await db.commit()
    return untrusted_json(experiment_payload(experiment, content=access.content), "Platform Health experiment")


async def _propose_from_experiment(db: AsyncSession, reader: Reader, experiment_id: str = "") -> str:
    from app.models.agent import Agent
    from app.services.health.experiments import propose

    refusal = reader.write_refusal("experimenter")
    if refusal:
        return refusal
    from app.services.health.diagnosis import verify_evidence

    experiment, finding, access = await _experiment_for(db, reader, experiment_id)
    author = await db.get(Agent, reader.principal.agent_id)
    if author is None:
        return "Error: the filing agent is gone."
    evidence = finding.evidence or {}
    if access.agent is not None:
        # The refs as the consent rows allow them now: an opt-out since drops its own.
        checked, _dropped = await verify_evidence(db, reader, access, evidence)
        evidence = {**checked.as_json(), "counts": evidence.get("counts") or {}}
    proposal_id, created = await propose(
        db,
        experiment=experiment,
        finding=finding,
        author_agent=author,
        content_readable=access.content,
        evidence=evidence,
    )
    await db.commit()
    return json.dumps(
        {
            "proposal_id": str(proposal_id),
            "created": created,
            "experiment_verdict": (experiment.result or {}).get("verdict")
            if experiment.status == "completed"
            else "unmeasured",
        }
    )


def _experiment_definitions() -> list[ToolDefinition]:
    from app.models.health_experiment import EXPERIMENT_STATUSES
    from app.services.builtin_tool_groups import builtin_source

    source = builtin_source(EXPERIMENTS_GROUP_ID)
    return [
        ToolDefinition(
            name="list_experiments",
            description="The experiments of a finding, newest first, with status, sizing and result. Read-only.",
            input_schema={
                "type": "object",
                "properties": {
                    "finding_id": {"type": "string"},
                    "status": {"type": "string", "enum": list(EXPERIMENT_STATUSES)},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 20},
                },
                "required": ["finding_id"],
            },
            source=source,
        ),
        ToolDefinition(
            name="get_experiment",
            description=(
                "One experiment: status, sizing, and once both arms ended the comparison (quality with its noise "
                "margin, cost, latency) and how many writes the sandbox answered with a simulated result. "
                "An experiment stopped early is incomplete and has no verdict."
            ),
            input_schema={
                "type": "object",
                "properties": {"experiment_id": {"type": "string"}},
                "required": ["experiment_id"],
            },
            source=source,
        ),
        ToolDefinition(
            name="run_experiment",
            description=(
                "Launch a drafted experiment: the remaining Platform Health budget is read first and the number of cases "
                "sized to it; the dataset is built from the target's consented production cases; the current "
                "configuration and the variant run as sandbox copies on the same cases, each case once. Skipped, "
                "with the reason, when the budget, the consent, the dataset or the evaluation engine does not "
                "allow a measurement."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "experiment_id": {"type": "string"},
                    "max_cases": {"type": "integer", "minimum": 5, "maximum": 50, "default": 20},
                },
                "required": ["experiment_id"],
            },
            source=source,
        ),
        ToolDefinition(
            name="propose_from_experiment",
            description=(
                "File the improvement proposal an experiment earned, for the target's managers to approve: its "
                "measured verdict, or unmeasured with the reason when it could not be measured. A regression or a "
                "result with no gain is not filed. The same finding and change is never filed twice."
            ),
            input_schema={
                "type": "object",
                "properties": {"experiment_id": {"type": "string"}},
                "required": ["experiment_id"],
            },
            source=source,
        ),
    ]


def build_health_experiment_tools(
    user_id: str, *, read_only: bool = False, run_id: str | None = None, agent_id: str | None = None
) -> tuple[list[ToolDefinition], CallableToolExecutor]:
    """The Experimenter's tools acting for *user_id* (``read_only``: the reads alone, as on ``/mcp/xtm``),
    called by *agent_id* in the assignment run *run_id*, if any."""
    handlers: dict[str, Callable[..., Awaitable[str]]] = {
        "list_experiments": _list_experiments,
        "get_experiment": _get_experiment,
        "run_experiment": _run_experiment,
        "propose_from_experiment": _propose_from_experiment,
    }
    return build_group(
        user_id,
        _experiment_definitions(),
        handlers,
        EXPERIMENTS_READ_TOOLS if read_only else None,
        run_id=run_id,
        agent_id=agent_id,
    )
