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

"""Who may read and write a company-managed shared-state entry or work item.

Base rule, always on. A company-managed entry or item is the fleet's: everyone
who can use the company-managed agents reads it. Writing it - creating,
changing, merging into, claiming (taking over), completing, releasing,
resolving, deleting - is reserved to:

* a company-managed agent, whoever its chat turn, run, sub-agent task or flow
  step acts for (using the agent is what the row is shared for);
* a company-managed flow, on the work items its trigger claims and settles;
* an administrator, on the REST API;
* the platform's own bookkeeping of a run it drives.

Every other writer only reads it: a personal agent (an administrator's
included), a personal flow, a non-admin on the REST API. A private row is never
gated here, since its visibility (its own user) already scopes it.

Agent-level narrowing, opt-in. The agent that governs a company-managed row -
the agent that created a work item (``created_by_agent_id``), the agent that
owns a shared-state entry (``owner_agent_id``, the creating agent unless one was
named) - may narrow which AGENTS read and write it, through its
``coordination_access``: ``all`` (the default, the base rule above), ``agents``
(itself plus the agents it lists) or ``self`` (itself alone), for reading and
writing separately; writing implies reading. A work item's assignee
(``owner_agent_id``) and its current claimer are always admitted, so a hand-off
reaches its target and a claim can always be finished. A listed agent counts
only while it is company-managed. The policy is read LIVE from the governing
agent on every access, so a change applies to the rows it already governs; a
row whose governor is gone (the foreign key sets it NULL) falls back to the base
rule. It narrows agents only: an administrator on the REST API, a
company-managed flow and the platform keep the base rule, and a person's REST
reads are unchanged. An agent whose turn or run acts for an administrator
(``acting_admin``) reads past every narrowing (Sam, 25 Nov 2026, #2161); its
writes stay narrowed. It never widens: a personal agent can never be granted
write on a company-managed row.

Every write function of ``services/shared_state.py`` and ``services/work_item.py``
takes the writer as a required argument and refuses before anything is written;
their agent-facing reads take it as ``reader``. The rules are pure functions
here, with :func:`restricted_governors` as their SQL mirror for the listings.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

# ``coordination_access`` scopes, per mode (read / write).
SCOPE_ALL = "all"
SCOPE_AGENTS = "agents"
SCOPE_SELF = "self"
COORDINATION_SCOPES: tuple[str, ...] = (SCOPE_ALL, SCOPE_AGENTS, SCOPE_SELF)
READ = "read"
WRITE = "write"
# The most agents one policy may list, per mode.
MAX_COORDINATION_AGENTS = 100


def _as_uuid(value: Any) -> uuid.UUID | None:
    if value is None or value == "":
        return None
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        return None


@dataclass(frozen=True, slots=True)
class CompanyManagedWriter:
    """The writer of a shared-state entry or work item, as this rule sees it."""

    may_write: bool
    # Who may write, as a refusal names them ("Only admins can ...").
    allowed: str
    # The acting agent when an agent acts: the rows' governing agents narrow
    # what it reads and writes. ``None`` for a person, a flow, the platform.
    agent_id: uuid.UUID | None = None
    # The acting flow when a flow's node writes: carried on the platform events
    # the write raises (``actor_flow_id``), so the flow never hears its own.
    flow_id: uuid.UUID | None = None
    # The person the agent's turn or run acts for is an administrator: the
    # agent reads past every governor's narrowing (writes stay narrowed).
    acting_admin: bool = False


def person_writer(*, is_admin: bool) -> CompanyManagedWriter:
    """A person on the REST API: an admin writes company-managed rows."""
    return CompanyManagedWriter(bool(is_admin), "admins")


def agent_writer(
    *,
    is_company_managed: bool,
    agent_id: str | uuid.UUID | None = None,
    acting_admin: bool = False,
) -> CompanyManagedWriter:
    """An agent's tools, dispatch or run: a company-managed agent writes them.

    *agent_id* is the acting agent, whose admission the governing agents'
    ``coordination_access`` decides; *acting_admin* (the person it acts for is
    an administrator) lets it read past every narrowing.
    """
    return CompanyManagedWriter(
        bool(is_company_managed),
        "company-managed agents",
        _as_uuid(agent_id),
        acting_admin=bool(acting_admin),
    )


def flow_writer(
    *, is_company_managed: bool, flow_id: str | uuid.UUID | None = None
) -> CompanyManagedWriter:
    """A flow's trigger and nodes: a company-managed flow writes them.

    *flow_id* is the writing flow, named on the platform events its writes raise.
    """
    return CompanyManagedWriter(
        bool(is_company_managed), "company-managed flows", flow_id=_as_uuid(flow_id)
    )


# The OpenAEV autonomous cycle clearing the decision signal of the run it drives.
PLATFORM_WRITER = CompanyManagedWriter(True, "the platform")


class CompanyManagedWriteRefusedError(Exception):
    """A write of a company-managed row by a writer that may only read it."""


class CompanyManagedKeyHiddenError(CompanyManagedWriteRefusedError):
    """A shared-state key held by a company-managed entry the agent cannot read."""


def refuse_company_managed_write(
    writer: CompanyManagedWriter,
    *,
    is_company_managed: bool,
    action: str,
    subject: str,
) -> None:
    """Raise :class:`CompanyManagedWriteRefusedError` when *writer* may not write the row.

    ``action`` and ``subject`` word the refusal: ``Only admins can modify
    company-managed shared state``.
    """
    if is_company_managed and not writer.may_write:
        raise CompanyManagedWriteRefusedError(
            f"Only {writer.allowed} can {action} company-managed {subject}"
        )


# ── The agent-level narrowing ────────────────────────────────────────────────


class CoordinationAccessError(ValueError):
    """A ``coordination_access`` value the API refuses to store."""


@dataclass(frozen=True, slots=True)
class CoordinationAccess:
    """A governing agent's narrowing, as the rules read it."""

    read: str = SCOPE_ALL
    write: str = SCOPE_ALL
    read_agent_ids: frozenset[uuid.UUID] = frozenset()
    write_agent_ids: frozenset[uuid.UUID] = frozenset()

    def scope(self, mode: str) -> str:
        return self.read if mode == READ else self.write

    def listed(self, mode: str) -> frozenset[uuid.UUID]:
        return self.read_agent_ids if mode == READ else self.write_agent_ids

    @property
    def restricted(self) -> bool:
        return self.read != SCOPE_ALL or self.write != SCOPE_ALL


DEFAULT_ACCESS = CoordinationAccess()


def _id_list(raw: Any, *, field: str, self_id: uuid.UUID | None) -> list[str]:
    if raw is None:
        return []
    if not isinstance(raw, list | tuple):
        raise CoordinationAccessError(f"{field} must be a list of agent ids")
    out: list[str] = []
    for entry in raw:
        parsed = _as_uuid(entry)
        if parsed is None:
            raise CoordinationAccessError(f"{field} holds an invalid agent id: {entry!r}")
        text = str(parsed)
        if parsed != self_id and text not in out:
            out.append(text)
    if len(out) > MAX_COORDINATION_AGENTS:
        raise CoordinationAccessError(
            f"{field} lists more than {MAX_COORDINATION_AGENTS} agents"
        )
    return out


def normalize_coordination_access(
    value: Mapping[str, Any] | None, *, self_id: str | uuid.UUID | None = None
) -> dict[str, Any] | None:
    """The stored form of a ``coordination_access`` value; ``None`` is the default.

    Refuses (:class:`CoordinationAccessError`) an unknown scope, a malformed id
    or an over-long list. The agent itself is dropped from its lists (it is
    always admitted), a list is kept only for the ``agents`` scope it belongs
    to, and writing implies reading: the read scope is widened to admit every
    writer (``all`` writers means ``all`` readers).
    """
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise CoordinationAccessError("coordination_access must be an object")
    unknown = set(value) - {READ, WRITE, "read_agent_ids", "write_agent_ids"}
    if unknown:
        raise CoordinationAccessError(
            f"coordination_access has unknown field(s): {', '.join(sorted(unknown))}"
        )
    own = _as_uuid(self_id)
    scopes: dict[str, str] = {}
    for mode in (READ, WRITE):
        scope = value.get(mode) or SCOPE_ALL
        if scope not in COORDINATION_SCOPES:
            raise CoordinationAccessError(
                f"coordination_access.{mode} must be one of "
                f"{', '.join(COORDINATION_SCOPES)}"
            )
        scopes[mode] = scope
    ids = {
        mode: _id_list(
            value.get(f"{mode}_agent_ids"), field=f"{mode}_agent_ids", self_id=own
        )
        for mode in (READ, WRITE)
    }
    read, write = scopes[READ], scopes[WRITE]
    if write == SCOPE_ALL:
        read = SCOPE_ALL
    elif write == SCOPE_AGENTS:
        if read == SCOPE_SELF:
            read = SCOPE_AGENTS
        if read == SCOPE_AGENTS:
            ids[READ] = ids[READ] + [i for i in ids[WRITE] if i not in ids[READ]]
    if read == SCOPE_ALL and write == SCOPE_ALL:
        return None
    return {
        READ: read,
        WRITE: write,
        "read_agent_ids": ids[READ] if read == SCOPE_AGENTS else [],
        "write_agent_ids": ids[WRITE] if write == SCOPE_AGENTS else [],
    }


def coordination_access_of(stored: Any) -> CoordinationAccess:
    """Read a stored value, fail-closed: an unknown scope reads as ``self``.

    A missing value (or a missing mode) is the default; anything that is not
    an object is too, so a row whose governor cannot be read keeps the base
    rule rather than vanishing.
    """
    if not isinstance(stored, Mapping):
        return DEFAULT_ACCESS

    def _scope(mode: str) -> str:
        # The SQL mirror's reading: absent / null is the default, any other
        # value that is not a scope is the narrowest.
        scope = stored.get(mode)
        if scope is None:
            return SCOPE_ALL
        return scope if scope in COORDINATION_SCOPES else SCOPE_SELF

    def _ids(mode: str) -> frozenset[uuid.UUID]:
        raw = stored.get(f"{mode}_agent_ids")
        if not isinstance(raw, list | tuple):
            return frozenset()
        return frozenset(i for i in (_as_uuid(e) for e in raw) if i is not None)

    return CoordinationAccess(_scope(READ), _scope(WRITE), _ids(READ), _ids(WRITE))


def coordination_access_payload(stored: Any) -> dict[str, Any] | None:
    """A stored value as the rules enforce it, for an API response (``None`` =
    the default): what the screen shows is what applies."""
    access = coordination_access_of(stored)
    if not access.restricted:
        return None

    def _ids(mode: str) -> list[str]:
        if access.scope(mode) != SCOPE_AGENTS:
            return []
        out: list[str] = []
        for entry in stored.get(f"{mode}_agent_ids") or []:
            parsed = _as_uuid(entry)
            if parsed is not None and str(parsed) not in out:
                out.append(str(parsed))
        return out

    return {
        READ: access.read,
        WRITE: access.write,
        "read_agent_ids": _ids(READ),
        "write_agent_ids": _ids(WRITE),
    }


def portable_coordination_access(stored: Any) -> dict[str, Any] | None:
    """A value copied to another instance or account: its scopes, no agent id.

    The listed agents name rows of the source (another instance, a
    publisher's agents), so they never travel; a narrowed scope with nobody
    listed admits the agent alone, the fail-closed reading of the source's
    intent.
    """
    access = coordination_access_of(stored)
    if not access.restricted:
        return None
    return {READ: access.read, WRITE: access.write, "read_agent_ids": [], "write_agent_ids": []}


def coordination_admits(
    access: CoordinationAccess,
    actor: CompanyManagedWriter,
    *,
    mode: str,
    governor_id: Any,
    participants: Iterable[Any] = (),
) -> bool:
    """Whether the governing agent's *access* lets *actor* read / write its row.

    Only agents are narrowed (``actor.agent_id``); the base rule's own check
    (:func:`refuse_company_managed_write`) still applies to writes.
    """
    agent_id = actor.agent_id
    governor = _as_uuid(governor_id)
    if agent_id is None or governor is None or agent_id == governor:
        return True
    if agent_id in {_as_uuid(p) for p in participants}:
        return True
    if mode == READ and actor.acting_admin:
        return True
    scope = access.scope(mode)
    if scope == SCOPE_ALL:
        return True
    # A listed agent counts only while it is company-managed.
    return scope == SCOPE_AGENTS and actor.may_write and agent_id in access.listed(mode)


def refuse_restricted_write(
    access: CoordinationAccess,
    writer: CompanyManagedWriter,
    *,
    governor_id: Any,
    participants: Iterable[Any] = (),
    action: str,
    subject: str,
    governor_label: str,
) -> None:
    """Raise when the governing agent's narrowing keeps *writer* from writing."""
    if not coordination_admits(
        access,
        writer,
        mode=WRITE,
        governor_id=governor_id,
        participants=participants,
    ):
        raise CompanyManagedWriteRefusedError(
            f"Only the agents {governor_label} allows can {action} this "
            f"company-managed {subject}"
        )


def may_hand_to(
    access: CoordinationAccess, *, governor_id: Any, target_id: Any
) -> bool:
    """Whether a row under *access* may be handed to *target_id* by a non-governor.

    A restricted write scope admits only the governor and its listed writers;
    handing the row (a work item's assignee, a shared-state entry's owner) to
    anyone else would admit that agent through the back door.
    """
    if access.write == SCOPE_ALL:
        return True
    target = _as_uuid(target_id)
    if target is None or target == _as_uuid(governor_id):
        return True
    return access.write == SCOPE_AGENTS and target in access.write_agent_ids


async def load_coordination_access(db, agent_ids: Iterable[Any]) -> dict[uuid.UUID, CoordinationAccess]:
    """The narrowing of each governing agent in *agent_ids*, in one query.

    Agents without a narrowing are left out (they are :data:`DEFAULT_ACCESS`).
    """
    from sqlalchemy import select

    from app.models.agent import Agent

    ids = {i for i in (_as_uuid(a) for a in agent_ids) if i is not None}
    if not ids:
        return {}
    rows = (
        await db.execute(
            select(Agent.id, Agent.coordination_access).where(
                Agent.id.in_(ids), Agent.coordination_access.is_not(None)
            )
        )
    ).all()
    out: dict[uuid.UUID, CoordinationAccess] = {}
    for row in rows:
        access = coordination_access_of(row[1])
        governor = _as_uuid(row[0])
        if access.restricted and governor is not None:
            out[governor] = access
    return out


def access_of(policies: Mapping[uuid.UUID, CoordinationAccess], governor_id: Any) -> CoordinationAccess:
    """The narrowing :func:`load_coordination_access` found for *governor_id*."""
    governor = _as_uuid(governor_id)
    return policies.get(governor, DEFAULT_ACCESS) if governor is not None else DEFAULT_ACCESS


def restricted_governors(actor: CompanyManagedWriter, *, mode: str):
    """``SELECT agents.id`` of the governors whose narrowing keeps *actor* out.

    The SQL mirror of :func:`coordination_admits` for the governor and list
    halves (a listing adds the participant half itself): a scope of ``agents``
    admits *actor* only while it is company-managed and listed, anything other
    than ``all`` / ``agents`` reads as ``self``. The agent itself is never a
    governor that keeps itself out, and an agent acting for an administrator is
    kept out of nothing it reads.
    """
    from sqlalchemy import Text, and_, cast, false, func, literal, not_, select
    from sqlalchemy.dialects.postgresql import ARRAY, JSONB

    from app.models.agent import Agent

    access = Agent.coordination_access
    scope = func.coalesce(access[mode].astext, literal(SCOPE_ALL))
    restricts = scope != SCOPE_ALL
    if actor.may_write and actor.agent_id is not None:
        listed = func.coalesce(
            cast(access[f"{mode}_agent_ids"], JSONB).op("?|")(
                cast([str(actor.agent_id)], ARRAY(Text))
            ),
            false(),
        )
        restricts = and_(restricts, not_(and_(scope == SCOPE_AGENTS, listed)))
    stmt = select(Agent.id).where(access.is_not(None), restricts)
    if mode == READ and actor.acting_admin:
        stmt = stmt.where(false())
    if actor.agent_id is not None:
        stmt = stmt.where(Agent.id != actor.agent_id)
    return stmt
