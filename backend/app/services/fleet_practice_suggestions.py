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

"""Good-practice suggestions of a fleet, computed from its configuration (#11948 S2).

The practices of the built-in skill "Fleet design and operations" that a read
of the configuration can check, shown on the About page next to the findings
of the generated documentation, with the same chip, Dismiss and Restore:

* ``member_cannot_consult_orchestrator``: a home member holds no handover to
  the orchestrator that runs as Consult. One click adds it (:func:`apply_back_consult`).
* ``member_chat_open``: a home member other than the orchestrator takes chats.
* ``owner_without_inbox``: a home member owns open work items and has no
  enabled work_item automation to take them.
* ``orchestrator_without_listeners``: the orchestrator has no enabled
  platform_event automation on ``work_item.completed``, ``run.orphaned`` or
  ``run.failed``.

The knowledge practices (#11972), read off each home agent's ``knowledge_base_ids``
and ``knowledge_base_permissions``; the fleet's *working* knowledge bases are
the ones bound to the orchestrator and every home member, plus the ones the
orchestrator writes that another home member reads (:func:`working_knowledge`):

* ``fleet_without_working_knowledge``: the fleet has none. Informational.
* ``orchestrator_cannot_write_knowledge``: a knowledge base bound to the
  orchestrator and every home member that the orchestrator cannot write. One
  click grants it write (:func:`apply_knowledge_suggestion`).
* ``member_missing_working_knowledge``: a home member is not bound to a
  knowledge base the orchestrator writes for the fleet. One click binds it,
  read-only. Dismissible with a reason (a member kept isolated on purpose).
* ``several_writers_on_working_knowledge``: more than one home agent writes a
  working knowledge base. Informational.

Suggestions, never rules: nothing here refuses a save, and nothing is applied
without a person's click. Computed on each read, no model call, never stored;
a fixed suggestion goes at the next read. A dismissal is stored like a
finding's (``documentation.dismissed_findings``) and holds while the member it
names is unchanged. New kinds join :data:`PRACTICE_KINDS` and :func:`practice_suggestions`.

Only what the reader can change is suggested (#1510): every kind, computed here or written by the
model with the documentation, names the surface its fix changes (:data:`KIND_SURFACES`), and
:func:`suggestion_editable` drops a suggestion whose surface is locked: on a built-in fleet every
surface but the ones the built-in lock leaves to the customer (:func:`builtin_editable_surfaces`, read
off the lock's own field sets), and on a built-in member agent its seeded mechanics. A fork of a
built-in fleet is a normal fleet and gets every suggestion.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent import Agent
from app.models.fleet import FLEET_ROLE_MEMBER, Fleet
from app.services.fleet_doc_findings import finding_id

#: The kinds computed here, in the order the page lists them.
PRACTICE_KINDS: tuple[str, ...] = (
    "member_cannot_consult_orchestrator",
    "member_chat_open",
    "owner_without_inbox",
    "orchestrator_without_listeners",
    "fleet_without_working_knowledge",
    "orchestrator_cannot_write_knowledge",
    "member_missing_working_knowledge",
    "several_writers_on_working_knowledge",
)
#: The knowledge practices (#11972), applied by :func:`apply_knowledge_suggestion` when applicable.
KNOWLEDGE_KINDS: frozenset[str] = frozenset(
    {
        "fleet_without_working_knowledge",
        "orchestrator_cannot_write_knowledge",
        "member_missing_working_knowledge",
        "several_writers_on_working_knowledge",
    }
)
#: The kinds a person applies in one click.
APPLICABLE_KINDS: frozenset[str] = frozenset(
    {"member_cannot_consult_orchestrator", "orchestrator_cannot_write_knowledge", "member_missing_working_knowledge"}
)
#: The events an orchestrator listens on to close the loop of its fleet.
LISTENER_EVENTS: frozenset[str] = frozenset({"work_item.completed", "run.orphaned", "run.failed"})
#: The skill section the listener suggestion points to.
SKILL_NAME = "Fleet design and operations"

#: The surface the fix of each kind changes (#1510). ``agent_mechanics``: the seeded definition of a member
#: (chat, handovers, assignments, persona, knowledge wiring the persona relies on); ``agent_integrations``:
#: its integration bindings; ``fleet_members``: the membership and role labels; ``fleet_gates``: the gates
#: a person adds to the fleet. A kind not listed is ``agent_mechanics``, the locked default.
KIND_SURFACES: dict[str, str] = {
    "member_cannot_consult_orchestrator": "agent_mechanics",
    "member_chat_open": "agent_mechanics",
    "owner_without_inbox": "agent_mechanics",
    "orchestrator_without_listeners": "agent_mechanics",
    # A working knowledge base only works when the orchestrator's persona writes it and the members'
    # read it: seeded mechanics on a built-in, so the binding alone is not a fix the reader can make.
    "fleet_without_working_knowledge": "agent_mechanics",
    "orchestrator_cannot_write_knowledge": "agent_mechanics",
    "member_missing_working_knowledge": "agent_mechanics",
    "several_writers_on_working_knowledge": "agent_mechanics",
    # The kinds the model writes with the documentation (``fleet_doc_findings.FINDING_KINDS``).
    "description_mismatch": "agent_mechanics",
    "missing_automation": "agent_mechanics",
    "no_gate_on_sensitive_action": "fleet_gates",
    "missing_integration": "agent_integrations",
    "member_without_role": "fleet_members",
    "other": "agent_mechanics",
}


def surface_of(kind: object) -> str:
    """The surface a suggestion of *kind* changes (:data:`KIND_SURFACES`, else ``agent_mechanics``)."""
    return KIND_SURFACES.get(str(kind or ""), "agent_mechanics")


def builtin_editable_surfaces() -> frozenset[str]:
    """The surfaces a person may still change on a built-in fleet, read off the built-in lock itself.

    ``fleet_gates`` while ``customer_gates`` is a writable field of a built-in fleet
    (``fleets.BUILTIN_FLEET_WRITABLE_FIELDS``); ``agent_integrations`` while ``integration_bindings`` is
    in the customer layer of a built-in agent (``builtin_customer_layer.INTEGRATION_LAYER_FIELDS``). The
    membership and the seeded mechanics are never writable there.
    """
    from app.services.builtin_customer_layer import INTEGRATION_LAYER_FIELDS
    from app.services.fleets import BUILTIN_FLEET_WRITABLE_FIELDS

    out: set[str] = set()
    if "customer_gates" in BUILTIN_FLEET_WRITABLE_FIELDS:
        out.add("fleet_gates")
    if "integration_bindings" in INTEGRATION_LAYER_FIELDS:
        out.add("agent_integrations")
    return frozenset(out)


def agent_locked(agent: object) -> bool:
    """Whether *agent*'s seeded mechanics are locked to a reader: a built-in in XTM One mode, as
    ``helpers.writable_agent_error`` refuses it."""
    from app.core.config import settings

    return bool(getattr(agent, "is_builtin", False)) and bool(settings.is_xtm_one)


def suggestion_editable(fleet: object, suggestion: Mapping[str, Any], locked_agent_ids: Iterable[str] = ()) -> bool:
    """Whether a reader managing *fleet* can apply the fix of *suggestion* (#1510).

    On a built-in fleet, only the surfaces of :func:`builtin_editable_surfaces`. Elsewhere, a suggestion
    on a member in *locked_agent_ids* (its ``agent_id``) keeps only those surfaces too; every other
    suggestion stays.
    """
    surface = surface_of(suggestion.get("kind"))
    if getattr(fleet, "is_builtin", False):
        return surface in builtin_editable_surfaces()
    if surface.startswith("agent_") and str(suggestion.get("agent_id") or "") in set(locked_agent_ids):
        return surface in builtin_editable_surfaces()
    return True


def is_practice_kind(kind: object) -> bool:
    """Whether *kind* is computed here (never stored with the generated findings)."""
    return str(kind or "") in PRACTICE_KINDS


def can_consult(target_id: str, handover_ids: Iterable[object], handover_types: Mapping[str, str] | None) -> bool:
    """Whether an agent with these handovers may consult *target_id*.

    A map with no type (the legacy mode, ``handover_types``) gives every listed target every type.
    """
    if target_id not in {str(i) for i in handover_ids or []}:
        return False
    if not handover_types:
        return True
    return handover_types.get(target_id, "delegate") == "consult"


def back_consult_handovers(
    target_id: str, handover_ids: Iterable[object], handover_types: Mapping[str, str] | None
) -> tuple[list[str], dict[str, str]]:
    """The handovers once a Consult to *target_id* is added, every other target running as before.

    In the legacy mode (no type in the map) with targets listed, the new target joins them with every
    type, so no type is written and the others keep every type; otherwise the target is typed ``consult``.
    """
    ids = [str(i) for i in handover_ids or []]
    types = dict(handover_types or {})
    legacy_with_targets = not types and bool(ids)
    if target_id not in ids:
        ids.append(target_id)
    if not legacy_with_targets:
        types[target_id] = "consult"
    return ids, types


def bound_knowledge(agent: object) -> list[str]:
    """The knowledge base ids bound to *agent*, as strings, in their order."""
    return [str(k) for k in (getattr(agent, "knowledge_base_ids", None) or [])]


def written_knowledge(agent: object) -> set[str]:
    """The bound knowledge base ids *agent* may write (a ``write`` grant on a bound id)."""
    perms = getattr(agent, "knowledge_base_permissions", None) or {}
    return {k for k in bound_knowledge(agent) if perms.get(k) == "write"}


def working_knowledge(orchestrator: object, members: Iterable[object]) -> list[str]:
    """The working knowledge bases of a fleet, in the orchestrator's binding order.

    Bound to the orchestrator and every other home member, or written by the orchestrator and bound to at
    least one other home member. A fleet with no other member has none.
    """
    others = [set(bound_knowledge(m)) for m in members]
    if not others:
        return []
    everyone = set.intersection(*others)
    read_by_some = set().union(*others)
    written = written_knowledge(orchestrator)
    return [k for k in bound_knowledge(orchestrator) if k in everyone or (k in written and k in read_by_some)]


def _suggestion(kind: str, agent: Agent, subject: str, finding: str, fix: str) -> dict[str, Any]:
    member = agent.name or ""
    item: dict[str, Any] = {
        "kind": kind,
        "member": member[:120],
        "subject": subject[:120],
        "finding": finding[:300],
        "suggested_fix": fix[:300],
        "source": "practice",
        "applicable": kind in APPLICABLE_KINDS,
    }
    item["id"] = finding_id(item)
    item["agent_id"] = str(agent.id)
    return item


#: The platform event a decision listener hears: an answered item going from blocked back to open.
WORK_ITEM_LISTENER_EVENTS: frozenset[str] = frozenset({"work_item.status_changed"})


def takes_work_items(assignment: object) -> bool:
    """Whether *assignment* takes its agent's work items: a work_item trigger, or a platform_event
    listener on their status changes (the seeded "Gate answered" listener, #1388)."""
    trigger = getattr(assignment, "trigger_type", None)
    if trigger == "work_item":
        return True
    return trigger == "platform_event" and bool(
        _event_names(getattr(assignment, "trigger_config", None)) & WORK_ITEM_LISTENER_EVENTS
    )


def _event_names(config: object) -> set[str]:
    raw = (config or {}).get("event") if isinstance(config, dict) else None
    if isinstance(raw, str):
        return {raw}
    if isinstance(raw, list):
        return {str(e) for e in raw}
    return set()


async def practice_suggestions(db: AsyncSession, fleet: Fleet) -> list[dict[str, Any]]:
    """Every practice suggestion of *fleet* the reader can apply, as it is configured now, each shaped like a
    finding.

    ``{id, kind, member, subject, finding, suggested_fix, source: "practice", applicable, agent_id}``; the id
    hashes the kind, the member and the subject (:func:`fleet_doc_findings.finding_id`). None on a built-in
    fleet, and none on a built-in member: their fixes change locked configuration (#1510).
    """
    from app.models.assignment import Assignment
    from app.models.work_item import WORK_ITEM_OPEN_STATUSES, WorkItem
    from app.services.fleets import fleet_members

    # The practices are about a fleet around its orchestrator: a fleet without one gets none yet.
    orchestrator_id = getattr(fleet, "orchestrator_agent_id", None)
    if orchestrator_id is None:
        return []
    # Every practice changes a member's mechanics: nothing to read on a built-in fleet (#1510).
    if not any(suggestion_editable(fleet, {"kind": kind}) for kind in PRACTICE_KINDS):
        return []
    rows = await fleet_members(db, fleet.id)
    home_ids = [r.agent_id for r in rows if r.agent_id is not None and r.role == FLEET_ROLE_MEMBER]
    if orchestrator_id not in home_ids:
        home_ids.append(orchestrator_id)
    agents: dict[uuid.UUID, Agent] = {
        a.id: a for a in (await db.execute(select(Agent).where(Agent.id.in_(home_ids)))).scalars().all()
    }
    assignments = (
        (await db.execute(select(Assignment).where(Assignment.agent_id.in_(home_ids), Assignment.enabled.is_(True))))
        .scalars()
        .all()
    )
    inbox = {a.agent_id for a in assignments if takes_work_items(a)}
    counted = (
        await db.execute(
            select(WorkItem.owner_agent_id, func.count())
            .where(WorkItem.owner_agent_id.in_(home_ids), WorkItem.status.in_(WORK_ITEM_OPEN_STATUSES))
            .group_by(WorkItem.owner_agent_id)
        )
    ).all()
    open_owned: dict[Any, int] = {row[0]: int(row[1]) for row in counted}
    orchestrator = agents.get(orchestrator_id)
    target = str(orchestrator_id)
    out: list[dict[str, Any]] = []
    for agent_id in home_ids:
        agent = agents.get(agent_id)
        if agent is None or agent_id == orchestrator_id:
            continue
        if orchestrator is not None and not can_consult(target, agent.handover_agent_ids or [], agent.handover_types):
            out.append(
                _suggestion(
                    "member_cannot_consult_orchestrator",
                    agent,
                    orchestrator.name,
                    f"{agent.name} cannot consult {orchestrator.name} for a route, scope or context question.",
                    f"Add a Consult handover from {agent.name} to {orchestrator.name}.",
                )
            )
        if not agent.disable_chat:
            out.append(
                _suggestion(
                    "member_chat_open",
                    agent,
                    "chat",
                    f"{agent.name} takes chats; people usually reach a fleet through its orchestrator.",
                    f"Turn chat off on {agent.name} unless people talk to it directly.",
                )
            )
    for agent_id in home_ids:
        agent = agents.get(agent_id)
        if agent is None or not open_owned.get(agent_id) or agent_id in inbox:
            continue
        out.append(
            _suggestion(
                "owner_without_inbox",
                agent,
                "work_item trigger",
                f"{agent.name} owns open work items and no work_item automation takes them.",
                f"Give {agent.name} an automation triggered by its work items.",
            )
        )
    if orchestrator is not None:
        heard = set().union(
            *(
                _event_names(a.trigger_config)
                for a in assignments
                if a.agent_id == orchestrator_id and a.trigger_type == "platform_event"
            )
        )
        if not heard & LISTENER_EVENTS:
            out.append(
                _suggestion(
                    "orchestrator_without_listeners",
                    orchestrator,
                    "platform_event listeners",
                    f"{orchestrator.name} listens on none of work_item.completed, run.orphaned or run.failed.",
                    f"Add the listeners the skill {SKILL_NAME} describes to {orchestrator.name}.",
                )
            )
    if orchestrator is not None:
        members = [agents[a] for a in home_ids if a != orchestrator_id and a in agents]
        out.extend(await _knowledge_suggestions(db, orchestrator, members))
    locked = {str(a.id) for a in agents.values() if agent_locked(a)}
    return [s for s in out if suggestion_editable(fleet, s, locked)]


async def _knowledge_names(db: AsyncSession, ids: Iterable[str]) -> dict[str, str]:
    """The names of the knowledge bases *ids*; an id that names none is left out."""
    from app.models.knowledge_base import KnowledgeBase

    uuids = [u for u in (_uuid(i) for i in ids) if u is not None]
    if not uuids:
        return {}
    rows = (await db.execute(select(KnowledgeBase.id, KnowledgeBase.name).where(KnowledgeBase.id.in_(uuids)))).all()
    return {str(row[0]): str(row[1] or "") for row in rows}


def _uuid(value: object) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value))
    except ValueError:
        return None


def _knowledge(kind: str, agent: Agent, kb_id: str, kb_name: str, finding: str, fix: str) -> dict[str, Any]:
    item = _suggestion(kind, agent, kb_name, finding, fix)
    item["knowledge_base_id"] = kb_id
    return item


async def _knowledge_suggestions(db: AsyncSession, orchestrator: Agent, members: list[Agent]) -> list[dict[str, Any]]:
    """The knowledge practices (#11972) of a fleet: *orchestrator* and its other home *members*."""
    if not members:
        return []
    working = working_knowledge(orchestrator, members)
    if not working:
        return [
            _suggestion(
                "fleet_without_working_knowledge",
                orchestrator,
                "working knowledge",
                f"The fleet has no knowledge base that {orchestrator.name} writes and every member reads.",
                f"Create one working knowledge base for the fleet, written by {orchestrator.name} and read by every member.",
            )
        ]
    names = await _knowledge_names(db, working)
    written = written_knowledge(orchestrator)
    out: list[dict[str, Any]] = []
    for kb_id in working:
        name = names.get(kb_id)
        if name is None:
            continue  # a dangling id: nothing to suggest about it
        if kb_id not in written:
            out.append(
                _knowledge(
                    "orchestrator_cannot_write_knowledge",
                    orchestrator,
                    kb_id,
                    name,
                    f"The whole fleet reads {name}, and {orchestrator.name} cannot write it.",
                    f"Give {orchestrator.name} write access to {name}.",
                )
            )
            continue
        for member in members:
            if kb_id not in bound_knowledge(member):
                out.append(
                    _knowledge(
                        "member_missing_working_knowledge",
                        member,
                        kb_id,
                        name,
                        f"{member.name} does not read {name}, which {orchestrator.name} writes for the fleet.",
                        f"Bind {name} to {member.name}, read-only.",
                    )
                )
        writers = [m for m in members if kb_id in written_knowledge(m)]
        if writers:
            out.append(
                _knowledge(
                    "several_writers_on_working_knowledge",
                    orchestrator,
                    kb_id,
                    name,
                    f"{name} has more than one writer: {orchestrator.name}, "
                    + ", ".join(m.name for m in writers)
                    + ".",
                    f"Keep {orchestrator.name} as the only writer of {name}; members propose updates in their results.",
                )
            )
    return out


def apply_knowledge_suggestion(fleet: Fleet, agent: Agent, kind: str, kb_id: str) -> bool:
    """Apply a knowledge suggestion (#11972) to *agent*; whether anything changed.

    ``orchestrator_cannot_write_knowledge`` grants the orchestrator write on *kb_id* (already bound);
    ``member_missing_working_knowledge`` binds *kb_id* to a member, read-only. The caller checks the
    suggestion, the permissions and the binding, versions, audits and commits.
    """
    bound = bound_knowledge(agent)
    if kind == "orchestrator_cannot_write_knowledge":
        if agent.id != fleet.orchestrator_agent_id or kb_id not in bound or kb_id in written_knowledge(agent):
            return False
        agent.knowledge_base_permissions = {**(agent.knowledge_base_permissions or {}), kb_id: "write"}
        return True
    if kind == "member_missing_working_knowledge":
        if agent.id == fleet.orchestrator_agent_id or kb_id in bound:
            return False
        agent.knowledge_base_ids = [*bound, kb_id]
        return True
    return False


async def apply_back_consult(db: AsyncSession, fleet: Fleet, member_agent: Agent) -> bool:
    """Give *member_agent* a Consult handover to *fleet*'s orchestrator; whether anything changed.

    The caller checks the suggestion, the permissions, versions, audits and commits.
    """
    if fleet.orchestrator_agent_id is None or member_agent.id == fleet.orchestrator_agent_id:
        return False
    target = str(fleet.orchestrator_agent_id)
    if can_consult(target, member_agent.handover_agent_ids or [], member_agent.handover_types):
        return False
    ids, types = back_consult_handovers(target, member_agent.handover_agent_ids or [], member_agent.handover_types)
    member_agent.handover_agent_ids = ids
    member_agent.handover_types = types
    return True


__all__ = [
    "APPLICABLE_KINDS",
    "KIND_SURFACES",
    "KNOWLEDGE_KINDS",
    "LISTENER_EVENTS",
    "PRACTICE_KINDS",
    "agent_locked",
    "apply_back_consult",
    "apply_knowledge_suggestion",
    "back_consult_handovers",
    "bound_knowledge",
    "builtin_editable_surfaces",
    "can_consult",
    "is_practice_kind",
    "practice_suggestions",
    "suggestion_editable",
    "surface_of",
    "working_knowledge",
    "written_knowledge",
]
