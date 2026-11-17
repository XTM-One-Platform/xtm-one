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

"""The runs of a placebo comparison and what each arm changes.

A placebo launch (``dev-docs/agent-placebo-evaluation.md``, level 2) creates,
for every agent x model of the evaluation and every trial, one ``real`` run
and, per target the agent has, one ``placebo`` run and - unless the
configuration leaves it out - one ``no_target`` run. Each trial is one run: the
vertical runs an entry once per run (one episode per run and entry).

This module holds the rules that need neither the database nor a model: which
targets apply to an agent, the run plan in the order the runs are created and
claimed, the seed of each placebo, the tool sources whose calls a comparison
repeats, and what a control arm changes in a run's overrides.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass, field

from app.services.evaluation_placebo import PlaceboConfig, PlaceboSource, PlaceboTarget

ARM_REAL = "real"
ARM_PLACEBO = "placebo"
ARM_NO_TARGET = "no_target"

#: Agent executions (runs x entries) one placebo launch may create (FR-13).
MAX_COMPARISON_EXECUTIONS = 2000


@dataclass(frozen=True)
class TargetSources:
    """What an agent's targets would replace: its persona, Prompts and skills, as the model sees them.

    ``skills`` carry the skill id in ``ref_id``; a skill's ``description`` holds
    every line of its catalog entry under the name.
    """

    persona: str
    prompts: tuple[PlaceboSource, ...] = ()
    skills: tuple[PlaceboSource, ...] = ()

    @property
    def names(self) -> tuple[str, ...]:
        """Every Prompt and skill name the agent shows: no placebo name may take one."""
        return tuple(source.name for source in (*self.prompts, *self.skills))


def target_sources(target: PlaceboTarget, sources: TargetSources) -> tuple[list[PlaceboSource], str | None]:
    """The elements ``target`` replaces for this agent, or ``([], reason)`` when it has none.

    A ``skill`` target resolves among the skills this agent can load, never by
    its id alone: a configuration cannot reach a skill the agent does not have.
    A target the agent does not have gets no arm (FR-3); the reason goes to
    the comparison's warnings.
    """
    if target.kind == "persona":
        if not sources.persona.strip():
            return [], "the agent has no persona"
        return [PlaceboSource(content=sources.persona)], None
    if target.kind == "prompts":
        if not sources.prompts:
            return [], "the agent has no attached Prompt"
        return list(sources.prompts), None
    if target.kind == "skills":
        if not sources.skills:
            return [], "the agent has no skill it can load"
        return list(sources.skills), None
    wanted = str(target.skill_id)
    matching = [source for source in sources.skills if source.ref_id == wanted]
    if not matching:
        return [], "the skill is not bound to the agent, or the agent cannot load it"
    return matching, None


@dataclass(frozen=True)
class PlannedRun:
    """One run of a placebo comparison."""

    agent_id: str
    model_id: str
    arm: str
    trial: int
    #: ``PlaceboTarget`` as JSON; ``None`` on the real arm.
    target: dict | None = None


@dataclass
class ComparisonPlan:
    """The runs of a comparison, in the order they are created, and the targets left out."""

    runs: list[PlannedRun] = field(default_factory=list)
    #: ``{"agent_id", "agent_name", "target", "reason"}`` per target left out.
    skipped: list[dict] = field(default_factory=list)

    def executions(self, entries: int) -> int:
        """Agent executions: every run executes every entry once."""
        return len(self.runs) * entries


def plan_comparison(
    config: PlaceboConfig,
    agents: Sequence[tuple[str, str, Sequence[str], TargetSources]],
    *,
    seed: str,
) -> ComparisonPlan:
    """The runs for ``agents`` - ``(agent_id, agent_name, model_ids, sources)`` each.

    Per agent x model and trial: the real arm, then per target the agent has a
    placebo arm and, with ``include_no_target``, a no-target arm. The runs are
    ordered trial by trial, the arms of a trial in an order hashed from
    ``seed``: claimed in that order, every arm meets the same provider
    conditions (FR-17).
    """
    plan = ComparisonPlan()
    arms: list[tuple[str, str, str, dict | None]] = []
    for agent_id, agent_name, model_ids, sources in agents:
        applicable: list[dict] = []
        for target in config.targets:
            _, reason = target_sources(target, sources)
            if reason is not None:
                plan.skipped.append(
                    {"agent_id": agent_id, "agent_name": agent_name, "target": target.key, "reason": reason}
                )
                continue
            applicable.append(target.model_dump(mode="json"))
        for model_id in model_ids:
            arms.append((agent_id, model_id, ARM_REAL, None))
            for target in applicable:
                arms.append((agent_id, model_id, ARM_PLACEBO, target))
                if config.include_no_target:
                    arms.append((agent_id, model_id, ARM_NO_TARGET, target))
    for trial in range(config.trials_per_entry):
        order = sorted(
            range(len(arms)),
            key=lambda index, trial=trial: hashlib.sha256(f"{seed}\x1f{trial}\x1f{index}".encode()).hexdigest(),
        )
        for index in order:
            agent_id, model_id, arm, target = arms[index]
            plan.runs.append(PlannedRun(agent_id=agent_id, model_id=model_id, arm=arm, trial=trial, target=target))
    return plan


def placebo_seed(comparison_seed: str, agent_id: str, target_key: str) -> str:
    """The seed of one agent's placebo for one target: the same on every model, trial and replay."""
    return f"{comparison_seed}:{agent_id}:{target_key}"


def write_tool_sources(agent: object) -> list[str]:
    """The agent's tool sources whose calls a comparison repeats (FR-14).

    Everything the agentic loop can call beyond the model: integrations, MCP
    servers, custom tools and built-in suites. Listing the tools themselves
    would need every MCP server discovered at launch time.
    """
    sources: list[str] = [f"integration {name}" for name in sorted(getattr(agent, "integration_bindings", None) or {})]
    mcp = len(getattr(agent, "mcp_server_ids", None) or [])
    if mcp:
        sources.append(f"{mcp} MCP server(s)")
    custom = len(getattr(agent, "custom_tool_ids", None) or [])
    if custom:
        sources.append(f"{custom} custom tool(s)")
    sources.extend(f"built-in {suite}" for suite in sorted(getattr(agent, "builtin_tool_ids", None) or []))
    return sources
