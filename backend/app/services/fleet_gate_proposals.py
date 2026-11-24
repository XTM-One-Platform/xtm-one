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

"""Approval gates proposed from a fleet's configuration (#10014, slice A2 of #10000).

A fleet's autonomy level acts only on its approval gate catalog
(``Fleet.approval_gates``, read through ``fleet_autonomy.fleet_gates``). A
custom fleet usually stores none, so the level gates nothing there. This module
computes, from the configuration alone (no model call, no quota), the gates the
platform proposes: one per bound integration and impact class, naming the
write tools of that integration the fleet's members can call. A manager accepts
them and they become the catalog (``PUT /fleets/{id}/approval-gates``).

The impact of a tool comes from one table (:func:`tool_impact`), on top of the
shared read/write classifier (``tool_classification``):

* a read is never gated (``None``);
* a draft or an internal mark (a label, a flag, a pin) is ``low``;
* a deletion, an email or a signature request sent in a person's name, an
  action on production assets (isolate, contain, wipe, reboot) or a payment
  is ``destructive``;
* every other write (a ticket, a chat or channel message, a pull request, a
  post, a simulation launched, a rule deployed) is ``high``, as
  ``fleet_autonomy.GATE_IMPACTS`` defines it.

A proposal carries the integration it writes to (``provider``) and a plain
action sentence per impact class (:data:`IMPACT_ACTIONS`); the tool names are
in ``tools``, never in the sentence, so the page translates the action and
draws the integration's mark (UX round 1 B1, B2 on #10034).

The approvals that already exist outside the level (an agent's human in the
loop, its tool-call approval, an automation's human in the loop) are listed as
they are (:func:`existing_approvals`): the fleet gate only adds pauses, it never
replaces one of them.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from app.services.fleet_autonomy import GATE_IMPACTS
from app.services.fleet_docs import DOC_MODULES, GATES_MAX
from app.services.tool_classification import infer_tool_type

#: Tokens of a tool name that make a write ``destructive``: gone for good,
#: acting on production assets, paying or signing.
DESTRUCTIVE_TOKENS: frozenset[str] = frozenset(
    {
        "ban",
        "charge",
        "contain",
        "delete",
        "destroy",
        "isolate",
        "kick",
        "kill",
        "pay",
        "purge",
        "quarantine",
        "reboot",
        "refund",
        "revoke",
        "sign",
        "terminate",
        "transfer",
        "trash",
        "wipe",
    }
)

#: A send verb: ``destructive`` only with one of :data:`PERSON_SEND_OBJECTS`
#: (an email or a signature request goes out in a person's name and cannot be
#: taken back); a chat, channel or thread message is ``high``.
SEND_TOKENS: frozenset[str] = frozenset({"forward", "reply", "respond", "send"})
PERSON_SEND_OBJECTS: frozenset[str] = frozenset(
    {"draft", "email", "emails", "envelope", "gmail", "mail", "signature"}
)

#: The plain action of a proposal per impact class, ``{provider}`` the
#: integration's name. The page shows the same sentences from its catalogs.
IMPACT_ACTIONS: dict[str, str] = {
    "destructive": "Delete, or send in a person's name, in {provider}",
    "high": "Create or change content in {provider}",
    "low": "Mark, label or draft in {provider}",
}

#: Tokens of a tool name that make a write ``low``: a draft or an internal mark
#: (a label, a flag, a pin, a reaction).
LOW_TOKENS: frozenset[str] = frozenset(
    {
        "draft",
        "flag",
        "label",
        "like",
        "mark",
        "pin",
        "react",
        "reaction",
        "reactions",
        "star",
        "tag",
        "unflag",
        "unpin",
        "unstar",
    }
)

#: Leading verbs that only read, which the shared classifier leaves to its
#: fail-safe ``write`` default (``compare_github_commits``). Local to the
#: proposals: the shared classifier also decides which calls skip approval
#: deployment-wide, and is not widened here.
PROPOSAL_READ_VERBS: frozenset[str] = frozenset({"compare", "diff"})

#: The tools one gate names at most (``fleet_docs.DocGateTool``, ``DocGate.tools``).
#: One gate per integration and impact class: a split into identical numbered
#: rows told the reader nothing (UX round 2 B1 on #10034).
GATE_TOOLS_MAX = 200
_ACTION_MAX = 200


def tool_impact(tool_name: str) -> str | None:
    """The impact class of a call of *tool_name*; ``None`` for a read (never gated).

    A draft is internal until it is sent: creating, changing or deleting one
    is ``low``; sending it (``send_microsoft_draft``) is ``destructive``
    (UX round 2 B2 on #10034). Otherwise the destructive tokens win over the
    low ones.
    """
    parts = tool_name.lower().split("_")
    if infer_tool_type(tool_name) == "read" or parts[0] in PROPOSAL_READ_VERBS:
        return None
    tokens = set(parts)
    if "draft" in tokens and not tokens & {"send", "forward"}:
        return "low"
    if tokens & DESTRUCTIVE_TOKENS:
        return "destructive"
    if tokens & SEND_TOKENS:
        return "destructive" if tokens & PERSON_SEND_OBJECTS else "high"
    if tokens & LOW_TOKENS:
        return "low"
    return "high"


def _provider_module(provider: str) -> str:
    return provider if provider in DOC_MODULES else "platform"


def _writable_providers(agent: Any) -> set[str]:
    """The providers *agent* reaches with write tools: bound, not every binding read-only, or by template."""
    bindings = getattr(agent, "integration_bindings", None)
    read_only = getattr(agent, "integration_read_only", None)
    providers: set[str] = set()
    if isinstance(bindings, Mapping):
        for provider, ids in bindings.items():
            bound = {str(i) for i in (ids if isinstance(ids, list) else [ids]) if i}
            ro = (read_only or {}).get(provider) if isinstance(read_only, Mapping) else None
            ro_ids = {str(i) for i in (ro if isinstance(ro, list) else [ro]) if i} if ro else set()
            if bound and not bound <= ro_ids:
                providers.add(str(provider))
    providers |= {str(p) for p in (getattr(agent, "integration_template_bindings", None) or []) if p}
    return providers


def _allowed(agent: Any, provider: str, tool: str) -> bool:
    """Whether the agent's tool scoping keeps *tool* of *provider* (allow list, deny list)."""
    if tool in (getattr(agent, "tool_deny_list", None) or []):
        return False
    allowed = getattr(agent, "integration_allowed_tools", None)
    if isinstance(allowed, Mapping) and isinstance(allowed.get(provider), list):
        return tool in allowed[provider]
    return True


@dataclass(frozen=True)
class _Group:
    provider: str
    impact: str


def propose_gates(
    agents: Iterable[Any],
    tools_by_provider: Mapping[str, Iterable[str]],
    provider_labels: Mapping[str, str] | None = None,
) -> list[dict[str, Any]]:
    """The gates proposed for a fleet whose home members are *agents*, each a ``DocGate`` dict.

    One gate per provider and impact class, naming the write tools of that
    provider the members can call (at most :data:`GATE_TOOLS_MAX` per gate; a
    longer list is split, the key numbered). Sorted by impact (most severe
    first), then provider. Read tools are never proposed. At most
    :data:`~app.services.fleet_docs.GATES_MAX` gates, the bound of a stored
    catalog, so accepting every proposal always fits (UX round 4 B1 on #10034).
    """
    labels = provider_labels or {}
    grouped: dict[_Group, set[str]] = {}
    for agent in agents:
        for provider in _writable_providers(agent):
            for tool in tools_by_provider.get(provider) or ():
                impact = tool_impact(tool)
                if impact is None or not _allowed(agent, provider, tool):
                    continue
                grouped.setdefault(_Group(provider, impact), set()).add(tool)
    order = {impact: rank for rank, impact in enumerate(reversed(GATE_IMPACTS))}
    out: list[dict[str, Any]] = []
    for group in sorted(grouped, key=lambda g: (order[g.impact], g.provider)):
        tools = sorted(grouped[group])
        chunks = [tools[i : i + GATE_TOOLS_MAX] for i in range(0, len(tools), GATE_TOOLS_MAX)]
        label = labels.get(group.provider) or group.provider
        for index, chunk in enumerate(chunks, start=1):
            suffix = f"-{index}" if len(chunks) > 1 else ""
            action = IMPACT_ACTIONS[group.impact].format(provider=label)
            if len(chunks) > 1:
                action = f"{action} ({index}/{len(chunks)})"
            out.append(
                {
                    "key": f"{group.provider}.{group.impact}-writes{suffix}"[:60],
                    "action": action[:_ACTION_MAX],
                    "module": _provider_module(group.provider),
                    "provider": group.provider[:60],
                    "approver": "",
                    "impact": group.impact,
                    "tools": [{"name": name, "when": {}} for name in chunk],
                }
            )
    return out[:GATES_MAX]


def existing_approvals(agents: Iterable[Any], assignments: Iterable[Any]) -> list[dict[str, str]]:
    """The approvals the fleet already has outside the level: ``{member, approval}`` each.

    An agent's human in the loop, its tool-call approval, an automation's human
    in the loop. The level never reads nor lowers them.
    """
    by_agent: dict[Any, list[Any]] = {}
    for assignment in assignments:
        by_agent.setdefault(getattr(assignment, "agent_id", None), []).append(assignment)
    out: list[dict[str, str]] = []
    for agent in agents:
        name = str(getattr(agent, "name", "") or "")
        if getattr(agent, "human_in_the_loop", False):
            out.append({"member": name, "approval": "Asks a person before acting (human in the loop)."})
        if getattr(agent, "approve_tool_calls", None):
            out.append({"member": name, "approval": "Each tool call waits for approval."})
        for assignment in by_agent.get(getattr(agent, "id", None), []):
            if getattr(assignment, "human_in_the_loop", None):
                out.append(
                    {
                        "member": name,
                        "approval": f"Automation '{getattr(assignment, 'name', '')}' asks a person before acting.",
                    }
                )
    return out


@dataclass
class FleetConfiguration:
    """What the proposals are computed from: a fleet's home members, their automations and integrations."""

    agents: list[Any]
    assignments: list[Any]
    tools_by_provider: dict[str, list[str]]
    labels: dict[str, str]

    def proposals(self) -> list[dict[str, Any]]:
        return propose_gates(self.agents, self.tools_by_provider, self.labels)


async def fleet_configuration(db: Any, fleet_id: Any) -> FleetConfiguration:
    """Read the home members of *fleet_id*, their automations and the tools of their integrations; no model call."""
    from sqlalchemy import select

    from app.api.v1.integration_catalog import PROVIDER_CATALOG, build_tools_for_provider
    from app.models.agent import Agent
    from app.models.assignment import Assignment
    from app.models.fleet import FLEET_ROLE_MEMBER
    from app.services.fleets import fleet_members

    rows = await fleet_members(db, fleet_id)
    ids = [r.agent_id for r in rows if r.agent_id is not None and r.role == FLEET_ROLE_MEMBER]
    agents: list[Any] = []
    assignments: list[Any] = []
    if ids:
        agents = list((await db.execute(select(Agent).where(Agent.id.in_(ids)))).scalars().all())
        assignments = list((await db.execute(select(Assignment).where(Assignment.agent_id.in_(ids)))).scalars().all())
    providers: set[str] = set()
    for agent in agents:
        providers |= {str(p) for p in (agent.integration_bindings or {})}
        providers |= {str(p) for p in (agent.integration_template_bindings or []) if p}
    tools_by_provider = {p: [t.name for t in (build_tools_for_provider(p) or []) if t.name] for p in sorted(providers)}
    labels = {
        str(k): str(v.get("name") or k) for k, v in PROVIDER_CATALOG.items() if str(k) in providers and isinstance(v, dict)
    }
    return FleetConfiguration(agents, assignments, tools_by_provider, labels)


async def fleet_effective_gates(db: Any, fleet_id: Any) -> tuple[str, list[dict[str, Any]]] | None:
    """The level and the effective gates of *fleet_id* (``fleet_autonomy.effective_gates``); ``None`` when absent.

    A custom fleet's level acts on its declared gates plus the gates computed
    from its configuration (#10153 C1) once the computed catalog is on for it
    (``Fleet.approval_pins`` not ``NULL``: every new custom fleet, every custom
    fleet the migration found gating nothing, and every fleet whose level a
    person applied through ``PUT /fleets/{id}/autonomy``). A custom fleet still
    at ``NULL`` keeps acting on its declared gates alone, today's behaviour, so
    the deploy adds no pause anywhere. A built-in fleet acts on its seeded
    catalog alone (its configuration ships with the platform), plus the gates
    a person added to it (``Fleet.customer_gates``, #916).
    """
    from sqlalchemy import select

    from app.models.fleet import Fleet
    from app.services.fleet_autonomy import effective_gates, effective_level

    row = (
        await db.execute(
            select(
                Fleet.autonomy_level,
                Fleet.approval_gates,
                Fleet.documentation,
                Fleet.approval_pins,
                Fleet.is_builtin,
                Fleet.customer_gates,
            ).where(Fleet.id == fleet_id)
        )
    ).first()
    if row is None:
        return None
    level, stored, documentation, pins, is_builtin, customer = row
    computed = None if is_builtin or pins is None else (await fleet_configuration(db, fleet_id)).proposals()
    return effective_level(level), effective_gates(stored, documentation, computed, pins, customer)


async def fleets_gated_action_counts(db: Any, fleets: Iterable[Any]) -> dict[Any, int]:
    """How many actions the level of each of *fleets* acts on, by fleet id (#10457, the Fleets list card).

    The same count as the length of :func:`fleet_effective_gates` for each
    fleet, read for the whole list at once: one read of the home members and
    one of their agents, the tools of each provider built once, so the list
    never reads per fleet. A fleet whose computed catalog is off (built-in, or
    custom with ``approval_pins`` ``NULL``) counts its declared gates alone.
    """
    from sqlalchemy import select

    from app.api.v1.integration_catalog import PROVIDER_CATALOG, build_tools_for_provider
    from app.models.agent import Agent
    from app.models.fleet import FLEET_ROLE_MEMBER, FleetMember
    from app.services.fleet_autonomy import effective_gates

    fleets = list(fleets)
    computed_on = [
        f.id for f in fleets if not getattr(f, "is_builtin", False) and getattr(f, "approval_pins", None) is not None
    ]
    agents_by_fleet: dict[Any, list[Any]] = {}
    if computed_on:
        rows = (
            await db.execute(
                select(FleetMember.fleet_id, FleetMember.agent_id).where(
                    FleetMember.fleet_id.in_(computed_on),
                    FleetMember.role == FLEET_ROLE_MEMBER,
                    FleetMember.agent_id.is_not(None),
                )
            )
        ).all()
        agent_ids = {agent_id for _fleet_id, agent_id in rows}
        agents = (
            {a.id: a for a in (await db.execute(select(Agent).where(Agent.id.in_(agent_ids)))).scalars().all()}
            if agent_ids
            else {}
        )
        for fleet_id, agent_id in rows:
            if agent_id in agents:
                agents_by_fleet.setdefault(fleet_id, []).append(agents[agent_id])
    tools_by_provider: dict[str, list[str]] = {}
    labels: dict[str, str] = {}
    counts: dict[Any, int] = {}
    for fleet in fleets:
        computed = None
        if fleet.id in computed_on:
            members = agents_by_fleet.get(fleet.id, [])
            for agent in members:
                for provider in _writable_providers(agent):
                    if provider not in tools_by_provider:
                        tools_by_provider[provider] = [
                            t.name for t in (build_tools_for_provider(provider) or []) if t.name
                        ]
                        entry = PROVIDER_CATALOG.get(provider)
                        labels[provider] = str(entry.get("name") or provider) if isinstance(entry, dict) else provider
            computed = propose_gates(members, tools_by_provider, labels)
        counts[fleet.id] = len(
            effective_gates(
                getattr(fleet, "approval_gates", None),
                getattr(fleet, "documentation", None),
                computed,
                getattr(fleet, "approval_pins", None),
                getattr(fleet, "customer_gates", None),
            )
        )
    return counts


def _impact_rank(impact: Any) -> int:
    return GATE_IMPACTS.index(impact) if impact in GATE_IMPACTS else len(GATE_IMPACTS) - 1


def removes_a_gate(previous: Iterable[Mapping[str, Any]], new: Iterable[Mapping[str, Any]]) -> bool:
    """Whether replacing the catalog *previous* by *new* lets an action run with less approval.

    A gate whose key disappears, a tool it named that is no longer named, or a
    lower impact class: each makes the fleet more autonomous, so only an
    administrator may write it (the same rule as raising the level).
    """
    kept = {str(g.get("key") or ""): g for g in new}
    for gate in previous:
        key = str(gate.get("key") or "")
        after = kept.get(key)
        if after is None:
            return True
        if _impact_rank(after.get("impact") or "high") < _impact_rank(gate.get("impact") or "high"):
            return True
        names_before = {str(t.get("name")) for t in gate.get("tools") or () if isinstance(t, Mapping)}
        names_after = {str(t.get("name")) for t in after.get("tools") or () if isinstance(t, Mapping)}
        if not names_before <= names_after:
            return True
    return False
