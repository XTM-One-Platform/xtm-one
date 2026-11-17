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

"""What a placebo comparison's run replaces and stands in, as the run shows it to the model.

``dev-docs/agent-placebo-evaluation.md`` FR-5, FR-6 and FR-15: the sources of a
target are the run's own version snapshots (its pinned persona, Prompts and
skills, resolved for the evaluation's owner as ``load_skill`` resolves them);
a control run builds its placebo in its model's tokens before its first entry
and stores it; its overrides overlay the run's snapshot overrides through the
keys ``run_agent_chat`` reads (``persona``, ``prompt_fragment``,
``skill_substitutes``, ``skill_ids_removed``).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict, dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.utils import format_exception_message
from app.models.agent import Agent
from app.models.evaluation import EvaluationPlaceboComparison, EvaluationRun
from app.services.evaluation.placebo_arms import (
    ARM_NO_TARGET,
    TargetSources,
    placebo_seed,
    target_sources,
)
from app.services.evaluation_placebo import (
    CHARACTER_MEASURER,
    LengthMeasurer,
    PlaceboLeakError,
    PlaceboSource,
    PlaceboTarget,
    build_placebo,
)
from app.services.tracing.cost_calculator import input_output_tokens

logger = logging.getLogger(__name__)

#: The user turn of a token probe: the system message is what is measured.
_PROBE_USER_MESSAGE = "."
#: Room for the probe's answer, which nobody reads.
_PROBE_MAX_TOKENS = 16
#: Probe calls in flight at once for one placebo, so a large skill set does
#: not fire every measure at the provider together.
_PROBE_CONCURRENCY = 4


class PlaceboUnavailableError(RuntimeError):
    """A control run's placebo cannot be built: the run fails with this reason."""


async def _live_prompt_sources(db: AsyncSession, prompt_ids: list | None) -> list[PlaceboSource]:
    """The agent's attached Prompts as ``load_agent_prompts`` injects them."""
    from app.services.builtin_tools.prompting import load_attached_prompts

    return [
        PlaceboSource(content=row.content, name=row.name, description=row.description or "", ref_id=str(row.id))
        for row in await load_attached_prompts(db, prompt_ids)
    ]


async def _owner_variables(db: AsyncSession, user_id: str | None) -> dict[str, str]:
    """The variables a run of the owner's resolves, as ``run_agent_chat`` loads them."""
    from app.api.v1.helpers import get_user_group_ids
    from app.core.utils import safe_uuid
    from app.services.variable_resolver import load_variables

    owner = safe_uuid(user_id)
    groups = await get_user_group_ids(db, owner) if owner else []
    return await load_variables(db, owner, groups)


def _with_variables(source: PlaceboSource, variables: dict[str, str]) -> PlaceboSource:
    """The source as the model reads it: its ``{{var}}`` placeholders resolved.

    Only the content: a placebo matches its length, and only names and
    descriptions decide which words a placebo may not carry.
    """
    from dataclasses import replace

    from app.services.variable_resolver import substitute_variables

    return replace(source, content=substitute_variables(source.content, variables))


async def load_placebo_target_sources(
    db: AsyncSession,
    agent: Agent,
    *,
    user_id: str | None,
    persona: str | None = None,
    prompt_versions: dict | None = None,
    skill_versions: dict | None = None,
) -> TargetSources:
    """What the agent's placebo targets replace, as its runs show them to the model.

    ``persona`` is the persona a run's snapshot pins (the agent's own when
    ``None``); ``prompt_versions`` and ``skill_versions`` are a run's snapshots
    (the live Prompts and skills when empty). Skills resolve as the run's own
    ``load_skill`` resolves them, for the same identity, and every content
    reads as the model reads it, with the owner's variables resolved.
    """
    from app.services.evaluation_engine import versioned_prompt_snapshots

    if prompt_versions:
        pinned, _ = await versioned_prompt_snapshots(db, prompt_versions)
        prompts = [
            PlaceboSource(
                content=prompt.content,
                name=prompt.name,
                description=prompt.description or "",
                ref_id=str(prompt.prompt_id),
                version=prompt.version_number,
            )
            for prompt in pinned
        ]
    else:
        prompts = await _live_prompt_sources(db, agent.prompt_ids)
    shown_persona = (agent.persona or "") if persona is None else persona
    skills = await _skill_sources(db, agent, user_id=user_id, skill_versions=skill_versions)
    variables = await _owner_variables(db, user_id)
    if variables:
        from app.services.variable_resolver import substitute_variables

        shown_persona = substitute_variables(shown_persona, variables)
        prompts = [_with_variables(prompt, variables) for prompt in prompts]
        skills = tuple(_with_variables(skill, variables) for skill in skills)
    return TargetSources(persona=shown_persona, prompts=tuple(prompts), skills=skills)


async def _skill_sources(
    db: AsyncSession, agent: Agent, *, user_id: str | None, skill_versions: dict | None
) -> tuple[PlaceboSource, ...]:
    """The agent's skills as its run's catalog and ``load_skill`` resolve them."""
    from app.services.builtin_tools import build_skill_access_context, resolve_agent_skills

    access = await build_skill_access_context(db, agent.user_id, fallback_user_id=user_id)
    skills = await resolve_agent_skills(
        db,
        agent.skill_ids,
        access=access,
        always_load_ids=set(agent.always_load_skill_ids),
        version_pins=skill_versions or None,
    )
    return tuple(
        PlaceboSource(
            content=skill.content,
            name=skill.name,
            description="\n".join(part for part in (skill.description, skill.compatibility) if part),
            always_load=skill.always_load,
            ref_id=str(skill.id),
            version=skill.version_number,
        )
        for skill in skills
    )


@dataclass
class ProbeUsage:
    """The token probes one placebo cost: stored with it, so a comparison reports them."""

    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0


async def probe_token_measurer(
    db: AsyncSession, model_id: str, *, user_id: str | None, usage: ProbeUsage | None = None
) -> LengthMeasurer:
    """Lengths in the run model's own tokens (FR-6).

    A text's length is the input tokens the provider reports for a probe whose
    system message is that text, minus those of a probe without one. Probes are
    cached by text, so the original measured for every rescale is called once.
    Each call is counted in ``usage``. Raises when the provider reports no
    input tokens.
    """
    from app.llm.base import ChatMessage
    from app.services.evaluation_engine import _get_llm_provider_and_model

    llm, resolved_model = await _get_llm_provider_and_model(db, model_id, user_id=user_id)
    gate = asyncio.Semaphore(_PROBE_CONCURRENCY)
    measured: dict[str, int] = {}

    async def _input_tokens(system: str | None) -> int:
        messages = [ChatMessage(role="system", content=system)] if system else []
        messages.append(ChatMessage(role="user", content=_PROBE_USER_MESSAGE))
        async with gate:
            response = await llm.chat(
                messages=messages, model=resolved_model, temperature=0.0, max_tokens=_PROBE_MAX_TOKENS
            )
        tokens, output = input_output_tokens(response.usage)
        if usage is not None:
            usage.calls += 1
            usage.input_tokens += tokens
            usage.output_tokens += output
        if tokens <= 0:
            raise ValueError(f"{resolved_model} reported no input tokens")
        return tokens

    bare = await _input_tokens(None)

    async def _measure(text: str) -> int:
        if not text:
            return 0
        if text not in measured:
            measured[text] = max(await _input_tokens(text) - bare, 0)
        return measured[text]

    return LengthMeasurer("tokens", _measure)


async def sibling_snapshot(db: AsyncSession, run: EvaluationRun) -> dict | None:
    """The placebo another trial of the same arm already built, if any.

    Same comparison, agent, model and target: the same seed and the same model,
    so the same placebo. Taken instead of probing the model again per trial.
    """
    siblings = (
        await db.execute(
            select(EvaluationRun.placebo_target, EvaluationRun.placebo_snapshot).where(
                EvaluationRun.placebo_comparison_id == run.placebo_comparison_id,
                EvaluationRun.agent_id == run.agent_id,
                EvaluationRun.model_id == run.model_id,
                EvaluationRun.arm == run.arm,
                EvaluationRun.id != run.id,
                EvaluationRun.placebo_snapshot.isnot(None),
            )
        )
    ).all()
    return next((snapshot for target, snapshot in siblings if target == run.placebo_target), None)


async def build_placebo_snapshot(
    db: AsyncSession,
    run: EvaluationRun,
    agent: Agent,
    comparison: EvaluationPlaceboComparison,
    overrides: dict[str, object],
    *,
    user_id: str | None,
) -> dict:
    """The placebo a control run stands in, measured in its model's tokens.

    The sources are what this run shows the model (its version snapshots); the
    seed is the comparison's for this agent and target - the same on every
    model, trial and replay. When the provider cannot be probed, the placebo is
    matched in characters and says so (``token_probe_failed``).
    """
    target = PlaceboTarget.model_validate(run.placebo_target)
    persona = overrides.get("persona")
    sources = await load_placebo_target_sources(
        db,
        agent,
        user_id=user_id,
        persona=persona if isinstance(persona, str) else None,
        prompt_versions=run.prompt_versions,
        skill_versions=run.skill_versions,
    )
    elements, reason = target_sources(target, sources)
    if reason is not None:
        raise PlaceboUnavailableError(f"No placebo for the '{target.key}' target: {reason}")
    config = comparison.config or {}

    async def _build(measurer: LengthMeasurer):
        return await build_placebo(
            target.kind,
            elements,
            agent_name=agent.name,
            seed=placebo_seed(comparison.seed, str(agent.id), target.key),
            measurer=measurer,
            reserved_names=sources.names,
            custom_text=config.get("custom_text"),
        )

    extra_warnings: list[str] = []
    usage = ProbeUsage()
    try:
        build = await _build(await probe_token_measurer(db, run.model_id, user_id=user_id, usage=usage))
    except PlaceboLeakError:
        raise
    except Exception as probe_err:
        logger.warning(
            "Placebo of run %s measured in characters: token probe on %s failed: %s",
            run.id,
            run.model_id,
            format_exception_message(probe_err),
        )
        extra_warnings.append("token_probe_failed")
        build = await _build(CHARACTER_MEASURER)
    snapshot = build.snapshot()
    snapshot["warnings"] = [*snapshot["warnings"], *extra_warnings]
    snapshot["probe_usage"] = asdict(usage)
    return snapshot


def control_arm_overrides(run: EvaluationRun, agent: Agent, snapshot: dict | None) -> dict[str, object]:
    """What a control arm changes in the run's ``evaluation_overrides``.

    No target: the target removed - the platform's default persona, no attached
    Prompt, the skills unbound. Placebo: the snapshot's elements in the
    target's place, through the same mechanism.
    """
    from app.services.builtin_tools import SkillSubstitute
    from app.services.evaluation_engine import _build_versioned_prompts_fragment

    target = PlaceboTarget.model_validate(run.placebo_target)
    if run.arm == ARM_NO_TARGET:
        if target.kind == "persona":
            return {"persona": ""}
        if target.kind == "prompts":
            return {"prompt_fragment": ""}
        if target.kind == "skills":
            return {"skill_ids_removed": [str(skill_id) for skill_id in agent.skill_ids or []]}
        return {"skill_ids_removed": [str(target.skill_id)]}
    elements = (snapshot or {}).get("elements") or []
    if target.kind == "persona":
        return {"persona": elements[0]["content"] if elements else ""}
    if target.kind == "prompts":
        return {
            "prompt_fragment": _build_versioned_prompts_fragment(
                [(element["name"], element["description"] or None, element["content"]) for element in elements]
            )
        }
    return {
        "skill_substitutes": {
            element["ref_id"]: SkillSubstitute(
                name=element["name"], description=element["description"], content=element["content"]
            )
            for element in elements
        }
    }
