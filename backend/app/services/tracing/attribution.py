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

"""Who a trace's usage counts for, and the department it lands in: one rule, one module.

A trace keeps the ``user_id`` its producer sent; many have none (an assignment
run, a sub-agent, a webhook, a channel message, a background copy). The person
the usage is ATTRIBUTED to is resolved by the first rule that names someone:

* ``user`` - the trace's own user;
* ``assignment`` - its assignment run's acting identity (the person who
  triggered it, else the run-as user, else the assignment owner:
  ``AssignmentRun.user_id``), else the assignment's owner;
* ``parent`` - the person its parent trace is attributed to (a sub-agent, a
  consulted agent, a flow step), resolved the same way up the chain;
* ``conversation`` - the owner of its conversation;
* ``agent_owner`` - the owner of its agent (a webhook, a channel bot);

and otherwise nobody, for a reason (:data:`RESIDUAL_ATTRIBUTIONS`):
``unknown`` (it names an agent, a run or a parent that no longer resolves to a
person), ``anonymous_web`` (a conversation with no person behind it),
``system`` (nothing to attribute: the platform's own calls).

The department is the attributed person's when the trace is written
(``services/departments.py``), stored with it so a later move does not rewrite
history, and only once a department exists: until then it is left unresolved
for the backfill to resolve from the first mapping.

:func:`stamp_traces` is shared by the trace sink (one trace) and the backfill
(a batch), so the two can never disagree.
"""

from __future__ import annotations

import uuid
from collections.abc import Hashable, Iterable, Mapping
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent import Agent
from app.models.assignment import Assignment, AssignmentRun
from app.models.chat import Conversation
from app.models.llm_trace import LLMTrace
from app.services.departments import DepartmentMap, load_department_map, resolve_user_departments

#: The rules that name a person, in the order they are tried.
PERSON_ATTRIBUTIONS: tuple[str, ...] = ("user", "assignment", "parent", "conversation", "agent_owner")
#: Why a trace is attributed to nobody.
RESIDUAL_ATTRIBUTIONS: tuple[str, ...] = ("system", "anonymous_web", "unknown")
RESIDUAL_NOTES: dict[str, str] = {
    "system": "the platform's own calls (memory, evaluation, background work): no agent, run or person behind them",
    "anonymous_web": "a conversation with no person behind it and no agent owner to fall back on",
    "unknown": "an agent, run or parent trace that no longer resolves to a person (deleted since)",
}

#: How far up a chain of parent traces a person is looked for.
_PARENT_DEPTH = 4


@dataclass(frozen=True)
class TraceRefs:
    """What a trace names, as stored on ``llm_traces``."""

    user_id: uuid.UUID | None = None
    agent_id: uuid.UUID | None = None
    assignment_run_id: uuid.UUID | None = None
    parent_trace_id: uuid.UUID | None = None
    conversation_id: uuid.UUID | None = None


@dataclass(frozen=True)
class TraceStamp:
    attributed_user_id: uuid.UUID | None
    attribution: str
    #: Whether a department was resolved (``department_resolved_at`` is set): only once one exists.
    department_resolved: bool
    department_id: uuid.UUID | None
    agent_name_snapshot: str | None


def _ids(values: Iterable[uuid.UUID | None]) -> list[uuid.UUID]:
    return sorted({value for value in values if value is not None}, key=str)


async def _people(
    db: AsyncSession, refs: Mapping[Hashable, TraceRefs], *, depth: int
) -> dict[Hashable, tuple[uuid.UUID | None, str]]:
    """``key -> (person, attribution)`` of every trace of *refs*."""
    runs: dict[uuid.UUID, uuid.UUID | None] = {}
    run_ids = _ids(r.assignment_run_id for r in refs.values() if r.user_id is None)
    if run_ids:
        rows = await db.execute(
            select(AssignmentRun.id, AssignmentRun.user_id, Assignment.user_id.label("owner_id"))
            .outerjoin(Assignment, Assignment.id == AssignmentRun.assignment_id)
            .where(AssignmentRun.id.in_(run_ids))
        )
        runs = {row.id: row.user_id or row.owner_id for row in rows.all()}

    parents: dict[Hashable, tuple[uuid.UUID | None, str]] = {}
    pending = {
        key: r.parent_trace_id
        for key, r in refs.items()
        if r.user_id is None and runs.get(r.assignment_run_id) is None and r.parent_trace_id is not None
    }
    if pending and depth > 0:
        rows = (
            await db.execute(
                select(
                    LLMTrace.id,
                    LLMTrace.attributed_user_id,
                    LLMTrace.attribution,
                    LLMTrace.user_id,
                    LLMTrace.agent_id,
                    LLMTrace.assignment_run_id,
                    LLMTrace.parent_trace_id,
                    LLMTrace.conversation_id,
                ).where(LLMTrace.id.in_(_ids(pending.values())))
            )
        ).all()
        stamped = {row.id: row.attributed_user_id for row in rows if row.attribution in PERSON_ATTRIBUTIONS}
        unstamped = {
            row.id: TraceRefs(row.user_id, row.agent_id, row.assignment_run_id, row.parent_trace_id, row.conversation_id)
            for row in rows
            if row.id not in stamped
        }
        resolved = await _people(db, unstamped, depth=depth - 1) if unstamped else {}
        for key, parent_id in pending.items():
            person = stamped.get(parent_id) or (resolved.get(parent_id) or (None, ""))[0]
            if person is not None:
                parents[key] = (person, "parent")

    conversations: dict[uuid.UUID, uuid.UUID | None] = {}
    conversation_ids = _ids(r.conversation_id for key, r in refs.items() if r.user_id is None and key not in parents)
    if conversation_ids:
        rows = await db.execute(select(Conversation.id, Conversation.user_id).where(Conversation.id.in_(conversation_ids)))
        conversations = {row.id: row.user_id for row in rows.all()}

    owners: dict[uuid.UUID, uuid.UUID | None] = {}
    agent_ids = _ids(r.agent_id for key, r in refs.items() if r.user_id is None and key not in parents)
    if agent_ids:
        rows = await db.execute(select(Agent.id, Agent.user_id).where(Agent.id.in_(agent_ids)))
        owners = {row.id: row.user_id for row in rows.all()}

    people: dict[Hashable, tuple[uuid.UUID | None, str]] = {}
    for key, r in refs.items():
        if r.user_id is not None:
            people[key] = (r.user_id, "user")
        elif runs.get(r.assignment_run_id) is not None:
            people[key] = (runs[r.assignment_run_id], "assignment")
        elif key in parents:
            people[key] = parents[key]
        elif conversations.get(r.conversation_id) is not None:
            people[key] = (conversations[r.conversation_id], "conversation")
        elif owners.get(r.agent_id) is not None:
            people[key] = (owners[r.agent_id], "agent_owner")
        elif r.agent_id or r.assignment_run_id or r.parent_trace_id:
            people[key] = (None, "unknown")
        elif r.conversation_id:
            people[key] = (None, "anonymous_web")
        else:
            people[key] = (None, "system")
    return people


async def attribute(
    db: AsyncSession, refs: Mapping[Hashable, TraceRefs]
) -> dict[Hashable, tuple[uuid.UUID | None, str]]:
    """``key -> (person, attribution)`` of every ref, by the rules above, without the department.

    The one entry point for another record that must name its person the way a
    trace does (the ROI ledger's run records, ``services/roi_ledger.py``).
    """
    if not refs:
        return {}
    return await _people(db, refs, depth=_PARENT_DEPTH)


async def stamp_traces(
    db: AsyncSession,
    refs: Mapping[Hashable, TraceRefs],
    *,
    mapping: DepartmentMap | None = None,
) -> dict[Hashable, TraceStamp]:
    """The attribution, department and agent name of every trace of *refs*, by its key."""
    if not refs:
        return {}
    mapping = mapping or await load_department_map(db)
    people = await _people(db, refs, depth=_PARENT_DEPTH)
    persons = _ids(person for person, _attribution in people.values())
    departments = await resolve_user_departments(db, persons, mapping) if mapping.configured else {}
    agent_ids = _ids(r.agent_id for r in refs.values())
    names: dict[uuid.UUID, str] = {}
    if agent_ids:
        names = {row.id: row.name for row in (await db.execute(select(Agent.id, Agent.name).where(Agent.id.in_(agent_ids)))).all()}
    return {
        key: TraceStamp(
            attributed_user_id=people[key][0],
            attribution=people[key][1],
            department_resolved=mapping.configured,
            department_id=departments.get(people[key][0]) if people[key][0] is not None else None,
            agent_name_snapshot=names.get(r.agent_id) if r.agent_id is not None else None,
        )
        for key, r in refs.items()
    }
