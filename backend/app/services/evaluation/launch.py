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

"""What every way of starting evaluation runs checks and records.

The Run button, the API and the ``trigger_evaluation`` agent tool, replay, bulk
"Re-run full", the schedule and a persona-improvement candidate all pass these
checks, so none of them starts a run another would refuse, and the schedule
records the same version snapshot as the Run button.

``enabled`` is not one of the checks: it pauses the automatic launches (the
schedule, on agent change) and never a run a person asks for.

On agent change creates no run yet: it stages an execution with none
to run. When it does, it creates them through :func:`plan_launch` and
:func:`add_launch_runs` like the schedule.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent import Agent
from app.models.dataset import Dataset, DatasetEntry, EvaluationDataset
from app.models.evaluation import Evaluation, EvaluationAgent, EvaluationPlaceboComparison, EvaluationRun
from app.models.user import User
from app.services.evaluation.constants import MODEL_INDEPENDENT
from app.services.evaluation.fanout import evaluation_has_runs, resolve_effective_models
from app.services.evaluation.metric_package import family_is_valid
from app.services.evaluation.resume_secret import callback_unreachable

if TYPE_CHECKING:
    from app.llm.provider_access import ProviderRouting
    from app.services.evaluation.placebo_arms import PlannedRun


class LaunchRefusedError(Exception):
    """A launch that may not create runs: the reason, and the HTTP status a request gets."""

    def __init__(self, detail: str, *, status_code: int = 400) -> None:
        super().__init__(detail)
        self.detail = detail
        self.status_code = status_code


def family_gate_error(family: str | None) -> str | None:
    """Why *family* cannot score, or ``None``: an unknown family.

    Every known family computes package metrics, so a known family yields
    a quality signal, Judge on or off. A ``None`` family passes here: it is allowed
    on write and refused at launch by :func:`check_family`.
    """
    if family is not None and not family_is_valid(family):
        return f"Unknown evaluation family: {family}"
    return None


def check_family(evaluation: object) -> None:
    """Refuse an evaluation with no family, or an unknown one.

    There is no default family.
    """
    family = getattr(evaluation, "family", None)
    if not family:
        raise LaunchRefusedError(
            "This evaluation has no output family set. Select an evaluation "
            "family before running it."
        )
    error = family_gate_error(family)
    if error is not None:
        raise LaunchRefusedError(error)


def check_callback_secret(evaluation: object) -> None:
    """Refuse callback mode with no resume secret.

    No callback could resume its entries: each would park until its callback
    timeout, after the agent's first turn ran. A rescore calls no agent and
    skips this check.
    """
    if callback_unreachable(getattr(evaluation, "trigger_config", None)):
        raise LaunchRefusedError(
            "Callback mode needs a resume secret: set trigger_config.resume_secret "
            "before running this evaluation."
        )


async def check_dataset_families(db: AsyncSession, evaluation: Evaluation) -> None:
    """Refuse a launch while a linked dataset has no family, naming each one."""
    rows = (
        await db.execute(
            select(Dataset.id, Dataset.name)
            .join(EvaluationDataset, EvaluationDataset.dataset_id == Dataset.id)
            .where(
                EvaluationDataset.evaluation_id == evaluation.id,
                Dataset.family.is_(None),
            )
        )
    ).all()
    offenders = [f"{row.name} ({row.id})" for row in rows]
    if offenders:
        raise LaunchRefusedError(
            "The following linked dataset(s) have no family set and must be fixed "
            f"before this evaluation can run: {', '.join(offenders)}"
        )


#: Why an evaluation whose owner lost the right to run it does not launch.
OWNER_CANNOT_RUN = (
    "This evaluation's owner is no longer an active administrator, so it does not run. "
    "Copy it to run it as yourself."
)


async def owner_refusal(db: AsyncSession, evaluation: Evaluation) -> str | None:
    """Why *evaluation*'s owner may not run it now, or ``None``.

    Its runs act as its owner, whoever starts them (a schedule, an approval,
    another admin's trigger): evaluations are an administrators' tool, so an
    owner demoted or deactivated since stops them - at launch, when a run
    starts or resumes, and when a callback resumes an episode. An ownerless
    evaluation (Platform Health's, behind its own consent gate) has no owner
    to check.
    """
    owner_id = getattr(evaluation, "user_id", None)
    if owner_id is None:
        return None
    owner = (await db.execute(select(User.role, User.is_active).where(User.id == owner_id))).one_or_none()
    if owner is None or owner.role != "admin" or not owner.is_active:
        return OWNER_CANNOT_RUN
    return None


async def check_owner_may_run(db: AsyncSession, evaluation: Evaluation) -> None:
    """Refuse a launch :func:`owner_refusal` refuses (409)."""
    if (reason := await owner_refusal(db, evaluation)) is not None:
        raise LaunchRefusedError(reason, status_code=409)


async def check_launchable(db: AsyncSession, evaluation: Evaluation) -> None:
    """The checks every launch that runs an agent passes: an owner who may run it,
    family, callback secret, dataset families."""
    await check_owner_may_run(db, evaluation)
    check_family(evaluation)
    check_callback_secret(evaluation)
    await check_dataset_families(db, evaluation)


async def check_models_served(
    db: AsyncSession,
    evaluation: Evaluation,
    model_ids: Iterable[str],
    *,
    routing: ProviderRouting | None = None,
) -> None:
    """Refuse up front a model no provider of the owner offers, as a 422 naming each.

    The caller leaves out an agent's own default model and a model-independent
    agent's run, which the engine does not check either. *routing* is the
    owner's, when the caller already read it. Saving an evaluation refuses the
    models it adds the same way.
    """
    from app.services.evaluation_engine import unavailable_model_message, unserved_model_ids

    unavailable = await unserved_model_ids(db, evaluation, model_ids, routing=routing)
    if unavailable:
        raise LaunchRefusedError(unavailable_model_message(unavailable), status_code=422)


async def check_runs_served(
    db: AsyncSession,
    evaluation: Evaluation,
    runs: Iterable[tuple[uuid.UUID | None, str, bool]],
) -> None:
    """:func:`check_models_served` for runs given as ``(agent_id, model_id, is_default_model)``.

    Checks neither an agent's own default model nor a model-independent
    (``skip_llm``) agent's run, as the engine does not when the run starts, nor
    a flow subject's run (``agent_id`` None, #8257), whose models are per node.
    """
    pending = [
        (agent_id, model_id)
        for agent_id, model_id, is_default in runs
        if not is_default and agent_id is not None
    ]
    if not pending:
        return
    model_independent = set(
        (
            await db.execute(
                select(Agent.id).where(
                    Agent.id.in_({agent_id for agent_id, _ in pending}),
                    Agent.skip_llm.is_(True),
                )
            )
        ).scalars()
    )
    await check_models_served(
        db,
        evaluation,
        (model_id for agent_id, model_id in pending if agent_id not in model_independent),
    )


@dataclass(slots=True)
class LaunchPlan:
    """The runs a launch creates: per agent, its default model and its models; one per flow."""

    agent_ids: list[uuid.UUID]
    skip_llm_agent_ids: set[uuid.UUID]
    agent_model_map: dict[uuid.UUID, tuple[str | None, list[str]]]
    entry_count: int
    #: The flow subjects (#8257), each one run on ``MODEL_INDEPENDENT``.
    flow_ids: list[uuid.UUID] = field(default_factory=list)


async def plan_launch(
    db: AsyncSession,
    evaluation: Evaluation,
    *,
    model_ids: list[str],
    use_default: bool,
    routing: ProviderRouting | None = None,
) -> LaunchPlan:
    """Each agent's models for a launch on *model_ids*, plus its own model when *use_default*.

    Refuses a launch with no subject, no model for its agents, no entry or a
    model no provider of the owner offers, before any run exists. A flow
    subject (#8257) needs no model: it runs once, on its nodes' own models.
    """
    links = (
        await db.execute(
            select(EvaluationAgent.agent_id, EvaluationAgent.flow_id).where(
                EvaluationAgent.evaluation_id == evaluation.id
            )
        )
    ).all()
    agent_ids = [link.agent_id for link in links if link.agent_id is not None]
    flow_ids = [link.flow_id for link in links if link.flow_id is not None]
    if not agent_ids and not flow_ids:
        raise LaunchRefusedError("No agents in this evaluation")
    if agent_ids and not use_default and not model_ids:
        raise LaunchRefusedError("No models configured")

    entry_count = (
        await db.scalar(
            select(func.count(DatasetEntry.id))
            .join(EvaluationDataset, EvaluationDataset.dataset_id == DatasetEntry.dataset_id)
            .where(EvaluationDataset.evaluation_id == evaluation.id)
        )
    ) or 0
    if entry_count == 0:
        raise LaunchRefusedError("No dataset entries")

    agent_model_map: dict[uuid.UUID, tuple[str | None, list[str]]] = {}
    if use_default:
        from app.llm.agent_model import agent_model_selection
        from app.llm.router import get_platform_default_model

        agent_rows = await db.execute(
            select(Agent.id, Agent.llm_model, Agent.llm_model_source).where(
                Agent.id.in_(agent_ids)
            )
        )
        platform_default = await get_platform_default_model(db)
        for row in agent_rows.all():
            # The routing model for an agent that runs on it.
            default_model = (await agent_model_selection(db, row)).model or platform_default
            models = [default_model] if default_model else []
            models += [mid for mid in model_ids if mid not in models]
            agent_model_map[row.id] = (default_model, models)
    else:
        agent_model_map = {aid: (None, list(model_ids)) for aid in agent_ids}

    # A model-independent agent collapses to one run on no model.
    skip_llm_agent_ids = set(
        (
            await db.execute(
                select(Agent.id).where(Agent.id.in_(agent_ids), Agent.skip_llm.is_(True))
            )
        ).scalars()
    )
    if agent_ids and not evaluation_has_runs(agent_ids, skip_llm_agent_ids, agent_model_map):
        raise LaunchRefusedError("No models could be resolved")

    await check_models_served(
        db,
        evaluation,
        (
            mid
            for aid, (default_mid, models) in agent_model_map.items()
            if aid not in skip_llm_agent_ids
            for mid in models
            if mid != default_mid
        ),
        routing=routing,
    )
    return LaunchPlan(agent_ids, skip_llm_agent_ids, agent_model_map, entry_count, flow_ids)


def active_model_ids(evaluation: Evaluation) -> list[str]:
    """The evaluation's extra models a launch runs: every one not disabled (FR-017)."""
    disabled = set(evaluation.disabled_model_ids or [])
    return [mid for mid in (evaluation.model_ids or []) if mid not in disabled]


async def add_launch_runs(
    db: AsyncSession,
    evaluation: Evaluation,
    plan: LaunchPlan,
    *,
    trigger_reason: str,
    owner_execution_id: uuid.UUID,
    tags: dict[str, str] | None = None,
    comparison: tuple[EvaluationPlaceboComparison, Sequence[PlannedRun]] | None = None,
) -> list[EvaluationRun]:
    """Add one pending run per agent and model of *plan*, with its version snapshot and pinned datasets.

    The snapshot holds the persona, prompt, config and skill versions, so a
    replay grades the same prompt. Each run is owned by
    *owner_execution_id*, the execution the caller staged in this transaction.
    Nothing is committed.

    With *comparison* (a placebo launch), the runs are its planned
    arms x trials instead, in the planned order: their creation times follow
    it, so the dispatcher claims them interleaved. A placebo applies to agents
    only: a launch with a flow subject is refused.

    Each flow subject (#8257) gets one run on ``MODEL_INDEPENDENT``, stamped with
    the flow's version, which the run stamps again when it starts.
    """
    from app.models.flow import Flow
    from app.services.agent_version_snapshots import resolve_agent_version_snapshots
    from app.services.evaluation import dataset_versioning

    snapshots = await resolve_agent_version_snapshots(db, plan.agent_ids)
    pinned = await dataset_versioning.pin_evaluation_versions(db, evaluation.id)
    defaults: dict[uuid.UUID, str | None] = {}
    agent_models: list[tuple[uuid.UUID, str]] = []
    for agent_id in plan.agent_ids:
        default_mid, model_list = plan.agent_model_map.get(agent_id, (None, []))
        models, defaults[agent_id] = resolve_effective_models(
            agent_id, plan.skip_llm_agent_ids, model_list, default_mid
        )
        agent_models.extend((agent_id, model_id) for model_id in models)

    def _run(agent_id: uuid.UUID, model_id: str, **arm: object) -> EvaluationRun:
        persona_version, prompt_versions, config_versions, skill_versions = snapshots.get(
            agent_id, (None, None, None, None)
        )
        run = EvaluationRun(
            evaluation_id=evaluation.id,
            agent_id=agent_id,
            model_id=model_id,
            is_default_model=(model_id == defaults.get(agent_id)),
            status="pending",
            trigger_reason=trigger_reason,
            total_entries=plan.entry_count,
            persona_version_number=persona_version,
            prompt_versions=prompt_versions,
            agent_config_versions=config_versions,
            skill_versions=skill_versions,
            pinned_dataset_versions=pinned or None,
            tags=dict(tags) if tags else None,
            owner_execution_id=owner_execution_id,
            **arm,
        )
        db.add(run)
        return run

    if comparison is None:
        runs = [_run(agent_id, model_id) for agent_id, model_id in agent_models]
        if plan.flow_ids:
            flow_versions = {
                row.id: row.version_number
                for row in (
                    await db.execute(select(Flow.id, Flow.version_number).where(Flow.id.in_(plan.flow_ids)))
                ).all()
            }
            for flow_id in plan.flow_ids:
                run = EvaluationRun(
                    evaluation_id=evaluation.id,
                    flow_id=flow_id,
                    flow_version_number=flow_versions.get(flow_id),
                    model_id=MODEL_INDEPENDENT,
                    is_default_model=False,
                    status="pending",
                    trigger_reason=trigger_reason,
                    total_entries=plan.entry_count,
                    pinned_dataset_versions=pinned or None,
                    tags=dict(tags) if tags else None,
                    owner_execution_id=owner_execution_id,
                )
                db.add(run)
                runs.append(run)
        return runs
    if plan.flow_ids:
        raise LaunchRefusedError("A placebo comparison applies to agents only.")
    comparison_row, planned = comparison
    started = datetime.now(UTC)
    return [
        _run(
            uuid.UUID(planned_run.agent_id),
            planned_run.model_id,
            arm=planned_run.arm,
            trial_index=planned_run.trial,
            placebo_target=planned_run.target,
            placebo_comparison_id=comparison_row.id,
            created_at=started + timedelta(microseconds=index),
        )
        for index, planned_run in enumerate(planned)
    ]
