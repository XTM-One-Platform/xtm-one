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

"""Fleet rules shared by every surface: membership, exposure, the card.

The model and its rationale live in :mod:`app.models.fleet`. This module is the
one place that decides what a fleet write may do, so the REST routes and, from
phase 4, the agent_builder tools refuse the same things with the same words.

* **One home fleet.** Adding an agent as a ``member`` of a fleet while it is
  the home member of another is refused (409) with the other fleet's name; the
  partial unique index stays the authority on a race.
* **Connected, not moved.** A ``connected`` row names an agent or a whole
  fleet the fleet calls without owning it: no manage right on the agent is
  needed, only the right to see it.
* **Exposure.** Other fleets reach a fleet through its EXPOSED home members
  only: the orchestrator always, plus the members flagged ``is_exposed``. A
  handover configured from outside a fleet to one of its non-exposed members
  is refused when it is written (:func:`refused_cross_fleet_handovers`); one
  that predates the rule, or that a membership or exposure change made
  invalid, is kept and reported (:func:`fleet_boundary_violations`), never
  broken.
* **Workspace.** A fleet is bound to exactly one workspace, its coordination;
  created with the fleet when the caller names none.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent import Agent
from app.models.fleet import (
    FLEET_CONTEXT_INCLUSIONS,
    FLEET_HANDOVER_TYPES,
    FLEET_ROLE_CONNECTED,
    FLEET_ROLE_MEMBER,
    Fleet,
    FleetMember,
)
from app.models.workspace import Workspace
from app.services.workspaces import SLUG_MAX_LENGTH, slugify

_MAX_SLUG_ATTEMPTS = 50


class FleetActionRefusedError(Exception):
    """A fleet write the product refuses, with the status REST answers it with."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


# ── Slugs ────────────────────────────────────────────────────────────────────


async def unique_fleet_slug(db: AsyncSession, user_id: uuid.UUID | None, base: str) -> str:
    """A slug free for this owner among their fleets, suffixed if taken.

    Advisory like the workspace one: ``uq_fleets_slug_per_user`` is the
    authority and :func:`create_fleet` retries once on a race.
    """
    base = slugify(base)
    if base == "workspace":
        base = "fleet"
    taken = set((await db.execute(select(Fleet.slug).where(Fleet.user_id == user_id))).scalars().all())
    if base not in taken:
        return base
    for n in range(2, _MAX_SLUG_ATTEMPTS + 2):
        suffix = f"-{n}"
        candidate = base[: SLUG_MAX_LENGTH - len(suffix)] + suffix
        if candidate not in taken:
            return candidate
    tail = f"-{uuid.uuid4().hex[:8]}"
    return base[: SLUG_MAX_LENGTH - len(tail)] + tail


async def create_fleet(
    db: AsyncSession,
    *,
    user_id: uuid.UUID | None,
    name: str,
    workspace_id: uuid.UUID,
    **fields,
) -> Fleet:
    """Insert a fleet, retrying once if its slug was taken mid-flight.

    A new fleet acts on its computed approval catalog (``approval_pins`` ``{}``,
    #10153 C1) and, without a declared catalog, starts at ``autonomous``: the
    level it shows is the one that holds (C2), and a person moves the cursor to
    make its actions wait. A caller that passes either field keeps its value
    (a fork passes its source's catalog and ``approval_pins=None``).
    """
    fields.setdefault("approval_pins", {})
    if fields.get("approval_gates") is None:
        fields.setdefault("autonomy_level", "autonomous")
    await db.flush()
    for attempt in (1, 2):
        fleet = Fleet(
            user_id=user_id,
            name=name,
            slug=await unique_fleet_slug(db, user_id, name),
            workspace_id=workspace_id,
            **fields,
        )
        try:
            async with db.begin_nested():
                db.add(fleet)
                await db.flush()
            return fleet
        except IntegrityError:
            if attempt == 2:
                raise
    raise AssertionError("unreachable")  # pragma: no cover


# ── Validation of the fleet's own fields ─────────────────────────────────────


def normalize_accepted_handover_types(value: Sequence[str] | None) -> list[str] | None:
    """The stored form of ``accepted_handover_types``: ``None`` is all of them.

    422 for a type no fleet can accept; duplicates dropped, order kept.
    """
    if value is None:
        return None
    unknown = [t for t in value if t not in FLEET_HANDOVER_TYPES]
    if unknown:
        raise FleetActionRefusedError(
            422,
            f"Unknown handover type(s): {', '.join(sorted(set(unknown)))}. "
            f"A fleet accepts {', '.join(FLEET_HANDOVER_TYPES)}.",
        )
    return list(dict.fromkeys(value))


def ensure_workspace_can_host(fleet_company_managed: bool, workspace: Workspace) -> None:
    """Refuse binding a fleet to a workspace its audience could not use.

    A company-managed fleet coordinates in a company-managed workspace: in a
    personal one, every member's work items and shared state would sit where
    only the workspace's owner sees them. An archived workspace is refused like
    filing a conversation into one.
    """
    if workspace.is_archived:
        raise FleetActionRefusedError(400, "That workspace is archived.")
    if fleet_company_managed and not workspace.is_company_managed:
        raise FleetActionRefusedError(
            400, "A company-managed fleet needs a company-managed workspace."
        )


# ── Membership ───────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class HomeFleet:
    """An agent's home fleet, as the agents API and the card read it."""

    fleet_id: uuid.UUID
    fleet_name: str
    member_id: uuid.UUID
    is_orchestrator: bool
    is_exposed: bool


async def home_fleets_of(db: AsyncSession, agent_ids: Iterable[uuid.UUID]) -> dict[uuid.UUID, HomeFleet]:
    """The home fleet of each agent that has one, in one query."""
    ids = list({a for a in agent_ids if a is not None})
    if not ids:
        return {}
    rows = (
        await db.execute(
            select(
                FleetMember.agent_id,
                FleetMember.id,
                FleetMember.is_exposed,
                Fleet.id,
                Fleet.name,
                Fleet.orchestrator_agent_id,
            )
            .join(Fleet, Fleet.id == FleetMember.fleet_id)
            .where(FleetMember.role == FLEET_ROLE_MEMBER, FleetMember.agent_id.in_(ids))
        )
    ).all()
    out: dict[uuid.UUID, HomeFleet] = {}
    for agent_id, member_id, is_exposed, fleet_id, fleet_name, orchestrator_id in rows:
        is_orchestrator = orchestrator_id == agent_id
        out[agent_id] = HomeFleet(
            fleet_id=fleet_id,
            fleet_name=fleet_name,
            member_id=member_id,
            is_orchestrator=is_orchestrator,
            is_exposed=bool(is_exposed) or is_orchestrator,
        )
    return out


async def fleet_members(db: AsyncSession, fleet_id: uuid.UUID) -> list[FleetMember]:
    """Every row of a fleet, home members first, then connected, oldest first."""
    return list(
        (
            await db.execute(
                select(FleetMember)
                .where(FleetMember.fleet_id == fleet_id)
                .order_by(FleetMember.role.desc(), FleetMember.created_at, FleetMember.id)
            )
        )
        .scalars()
        .all()
    )


async def _member_row(db: AsyncSession, fleet: Fleet, member_id: uuid.UUID) -> FleetMember:
    row = (
        await db.execute(
            select(FleetMember).where(FleetMember.id == member_id, FleetMember.fleet_id == fleet.id)
        )
    ).scalar_one_or_none()
    if row is None:
        raise FleetActionRefusedError(404, "Fleet member not found")
    return row


ARCHIVED_FLEET_MEMBERS_REFUSAL = "This fleet is archived: restore it to change its members."

#: The only fields a caller may write on a built-in fleet (``Fleet.is_builtin``):
#: the deployment's own switches, never part of the seeded definition, the
#: fleet counterpart of ``ADMIN_OPT_IN_WRITABLE_FIELDS``. Everything else
#: (name, members, parents, exposure, orchestrator, connected) is rewritten by
#: the seed on every boot (``app.agents.fleet_seed``).
BUILTIN_FLEET_WRITABLE_FIELDS: frozenset[str] = frozenset(
    {
        "is_archived",
        "budget_config",
        "max_iterations",
        # The fleet's run budget (#1526): the deployment's choice, never seeded.
        "run_budget",
        "autonomy_level",
        # Per-gate pins (#916, CTEM-34): the customer's choice, never written by the seed, so it survives an upgrade.
        "approval_pins",
        # Gates a person added to the seeded catalog (#916, CTEM-34): customer layer, never seeded.
        "customer_gates",
        # The tool-level enforcement opt-in (#1467): the customer's, never seeded.
        "enforce_gates_at_tool_level",
        "prerequisite_confirmations",
        "health_dismissals",
        "health_check",
        "health_enrolled",
        "hide_activity_from_non_admins",
    }
)

BUILTIN_FLEET_REFUSAL = (
    "Built-in fleets ship with the platform and are read-only: only archiving, the run "
    "defaults (budget, iteration cap), the autonomy level, its per-action pins and added gates, the prerequisite confirmations, "
    "the dismissed health findings and the weekly health check can be changed."
)


def ensure_builtin_fleet_writable(fleet: Fleet, fields: Iterable[str] | None = None) -> None:
    """Refuse a write on a built-in fleet (403) unless it only sets writable switches.

    *fields* is the set of fleet fields the write changes; ``None`` is a write
    that is not a field update (a member, the exposure, a delete, a spec
    apply), always refused on a built-in fleet. The seed itself never calls it.
    """
    from app.services.health.read_only import HEALTH_READ_ONLY_REFUSAL, is_health_fleet

    if not getattr(fleet, "is_builtin", False):
        return
    # The Platform Health fleet takes no write at all, its switches included.
    if is_health_fleet(fleet):
        raise FleetActionRefusedError(403, HEALTH_READ_ONLY_REFUSAL)
    if fields is not None and set(fields) <= BUILTIN_FLEET_WRITABLE_FIELDS:
        return
    raise FleetActionRefusedError(403, BUILTIN_FLEET_REFUSAL)


def ensure_members_editable(fleet: Fleet) -> None:
    """An archived fleet freezes its members, connected rows and exposure (409).

    Every member write of this module calls it first, so the REST routes and
    the ``fleets`` built-in tools refuse alike. The fleet's own settings stay
    open (that is how it is restored), and deleting the fleet stays open too.
    """
    if fleet.is_archived:
        raise FleetActionRefusedError(409, ARCHIVED_FLEET_MEMBERS_REFUSAL)


async def add_home_member(
    db: AsyncSession,
    fleet: Fleet,
    agent: Agent,
    *,
    parent_member_id: uuid.UUID | None = None,
    is_exposed: bool = False,
) -> FleetMember:
    """Make *fleet* the home of *agent*. The caller checked the manage rights.

    Refused: an archived fleet (409), an agent already home in another fleet
    (409, naming it), already in this one (409), a personal agent in a
    company-managed fleet (400), a parent that is not a home member of this
    fleet (400).
    """
    ensure_members_editable(fleet)
    if fleet.is_company_managed and not agent.is_company_managed:
        raise FleetActionRefusedError(400, "A company-managed fleet contains only company-managed agents.")
    existing = (await home_fleets_of(db, [agent.id])).get(agent.id)
    if existing is not None:
        if existing.fleet_id == fleet.id:
            raise FleetActionRefusedError(409, f"'{agent.name}' is already a member of this fleet.")
        raise FleetActionRefusedError(
            409,
            f"'{agent.name}' is already a member of the fleet '{existing.fleet_name}'. "
            "An agent has one home fleet: remove it there first, or add it here as a connected agent.",
        )
    connected_here = (
        await db.execute(
            select(FleetMember.id).where(FleetMember.fleet_id == fleet.id, FleetMember.agent_id == agent.id)
        )
    ).first()
    if connected_here is not None:
        raise FleetActionRefusedError(
            409, f"'{agent.name}' is a connected agent of this fleet: remove that link first."
        )
    if parent_member_id is not None:
        parent = await _member_row(db, fleet, parent_member_id)
        if parent.role != FLEET_ROLE_MEMBER:
            raise FleetActionRefusedError(400, "A member reports to another member of the fleet, never to a connected one.")
    row = FleetMember(
        fleet_id=fleet.id,
        agent_id=agent.id,
        role=FLEET_ROLE_MEMBER,
        parent_member_id=parent_member_id,
        is_exposed=is_exposed,
    )
    try:
        async with db.begin_nested():
            db.add(row)
            await db.flush()
    except IntegrityError:
        # A concurrent add on another replica won the home-fleet index.
        raise FleetActionRefusedError(
            409, f"'{agent.name}' was just added to another fleet."
        ) from None
    return row


# ── Member settings: fleet defaults and per-row overrides ──────────────────

#: The member fields a fleet default covers (dev-docs/ctem-fleets.md 10.3).
MEMBER_SETTING_FIELDS: tuple[str, ...] = ("context_inclusion", "collaboration_instruction", "organization_scope")

#: The member settings that belong to a connected row only; the organization
#: scope applies to every member, home or connected.
CONNECTED_ONLY_SETTINGS: tuple[str, ...] = ("context_inclusion", "collaboration_instruction")

#: ``organization_scope.mode`` values: the primary organization, every tracked
#: organization whose monitoring is on (with the primary), or a list.
ORGANIZATION_SCOPE_MODES: tuple[str, ...] = ("primary", "monitored", "list")

#: The most organizations a ``list`` scope names.
MAX_SCOPE_ORGANIZATIONS = 50

PRIMARY_SCOPE: dict = {"mode": "primary"}

#: The most member rows one bulk call may change.
MAX_BULK_MEMBERS = 500

MEMBER_INSTRUCTION_MAX_LENGTH = 4000


@dataclass(frozen=True)
class MemberSettings:
    """A member row's effective settings and which of them the row overrides."""

    context_inclusion: str
    collaboration_instruction: str
    overridden: tuple[str, ...]
    organization_scope: dict = dataclass_field(default_factory=lambda: dict(PRIMARY_SCOPE))


def effective_member_settings(fleet: Fleet, row: FleetMember) -> MemberSettings:
    """The one reader of the member settings: the row's own value, else the fleet default.

    A NULL field on the row inherits the fleet default; every surface (the
    API, the graph, the tools, the spec) reads through this, never the raw
    columns. A home member carries no conversation or instruction override
    (those belong to connected rows); every row may override its
    organization scope.
    """
    default_inclusion = getattr(fleet, "default_context_inclusion", None) or "none"
    default_instruction = getattr(fleet, "default_collaboration_instruction", None) or ""
    overridden: list[str] = []
    inclusion = default_inclusion
    instruction = default_instruction
    own_scope = _stored_scope(getattr(row, "organization_scope", None))
    scope = resolve_organization_scope(getattr(fleet, "organization_scope", None), own_scope)
    if row.role != FLEET_ROLE_MEMBER:
        if row.context_inclusion is not None:
            inclusion = row.context_inclusion
            overridden.append("context_inclusion")
        if row.collaboration_instruction is not None:
            instruction = row.collaboration_instruction
            overridden.append("collaboration_instruction")
    if own_scope is not None:
        overridden.append("organization_scope")
    return MemberSettings(inclusion, instruction, tuple(overridden), scope)


def resolve_organization_scope(fleet_scope: object, member_scope: object) -> dict:
    """The effective scope from the raw columns: the row's, else the fleet's, else the primary.

    The one rule :func:`effective_member_settings` applies, for a reader that
    selected the two columns without loading the rows (the run defaults).
    """
    return _stored_scope(member_scope) or _stored_scope(fleet_scope) or dict(PRIMARY_SCOPE)


def _stored_scope(raw: object) -> dict | None:
    """A stored scope as the resolver reads it; ``None`` for NULL or a shape it cannot read."""
    if not isinstance(raw, dict):
        return None
    try:
        return normalize_organization_scope(raw)
    except FleetActionRefusedError:
        return None


def normalize_organization_scope(value: object) -> dict:
    """Validate an organization scope and answer it in its canonical shape.

    ``{"mode": "primary"}``, ``{"mode": "monitored"}`` or ``{"mode": "list",
    "organization_ids": [...]}`` (1 to :data:`MAX_SCOPE_ORGANIZATIONS` ids,
    duplicates dropped, order kept). Whether the ids exist is checked by
    :func:`check_scope_organizations`, which needs the database.
    """
    if not isinstance(value, dict):
        raise FleetActionRefusedError(422, "organization_scope is an object with a mode.")
    mode = str(value.get("mode") or "").strip().lower()
    if mode not in ORGANIZATION_SCOPE_MODES:
        raise FleetActionRefusedError(422, f"organization_scope.mode is one of {', '.join(ORGANIZATION_SCOPE_MODES)}.")
    if mode != "list":
        return {"mode": mode}
    raw_ids = value.get("organization_ids")
    if not isinstance(raw_ids, list) or not raw_ids:
        raise FleetActionRefusedError(422, "organization_scope.organization_ids names at least one organization.")
    ids: list[str] = []
    for raw in raw_ids:
        try:
            text = str(uuid.UUID(str(raw)))
        except (TypeError, ValueError):
            raise FleetActionRefusedError(422, f"organization_scope.organization_ids: '{raw}' is not an id.") from None
        if text not in ids:
            ids.append(text)
    if len(ids) > MAX_SCOPE_ORGANIZATIONS:
        raise FleetActionRefusedError(
            422, f"organization_scope.organization_ids names at most {MAX_SCOPE_ORGANIZATIONS} organizations."
        )
    return {"mode": "list", "organization_ids": ids}


async def check_scope_organizations(db: AsyncSession, scope: dict | None) -> None:
    """Refuse a ``list`` scope naming an organization that does not exist or is archived."""
    if not scope or scope.get("mode") != "list":
        return
    from app.models.organization import Organization

    ids = [uuid.UUID(i) for i in scope.get("organization_ids") or []]
    found = set(
        (await db.execute(select(Organization.id).where(Organization.id.in_(ids), Organization.archived_at.is_(None))))
        .scalars()
        .all()
    )
    missing = [str(i) for i in ids if i not in found]
    if missing:
        raise FleetActionRefusedError(
            422, f"organization_scope names no active organization: {', '.join(missing)}. Nothing was saved."
        )


def check_member_setting(name: str, value: object) -> str | dict | None:
    """Validate one member setting; ``None`` resets the field to the fleet default."""
    if value is None:
        return None
    if name == "organization_scope":
        return normalize_organization_scope(value)  # type: ignore[return-value]
    if name == "context_inclusion":
        text = str(value).strip().lower()
        if text not in FLEET_CONTEXT_INCLUSIONS:
            raise FleetActionRefusedError(422, f"context_inclusion is one of {', '.join(FLEET_CONTEXT_INCLUSIONS)}.")
        return text
    text = str(value)
    if len(text) > MEMBER_INSTRUCTION_MAX_LENGTH:
        raise FleetActionRefusedError(
            422, f"collaboration_instruction is at most {MEMBER_INSTRUCTION_MAX_LENGTH} characters."
        )
    return text


def set_member_defaults(fleet: Fleet, fields: dict) -> list[str]:
    """Write the fleet's member defaults from *fields*; the names changed.

    ``None`` puts a default back to its column default (nothing, empty).
    """
    ensure_members_editable(fleet)
    if set(fields) - set(MEMBER_SETTING_FIELDS):
        raise FleetActionRefusedError(422, f"member_defaults takes {', '.join(MEMBER_SETTING_FIELDS)}.")
    values = {name: check_member_setting(name, fields[name]) for name in MEMBER_SETTING_FIELDS if name in fields}
    if "context_inclusion" in values:
        fleet.default_context_inclusion = values["context_inclusion"] or "none"
    if "collaboration_instruction" in values:
        fleet.default_collaboration_instruction = values["collaboration_instruction"] or ""
    if "organization_scope" in values:
        fleet.organization_scope = values["organization_scope"] or dict(PRIMARY_SCOPE)
    return list(values)


async def bulk_update_members(
    db: AsyncSession,
    fleet: Fleet,
    member_ids: Sequence[uuid.UUID],
    *,
    fields: dict,
) -> list[FleetMember]:
    """Set the member settings of several connected rows at once, all or nothing.

    *fields* holds any of :data:`MEMBER_SETTING_FIELDS`; ``None`` resets that
    field to the fleet default. Refused before any write, naming the rows: a
    row of another fleet or unknown (404), a home member given a connected-only
    setting (400), a scope naming no active organization (422). The
    organization scope alone may be set on home and connected rows together.
    The caller commits (one transaction) and audits.
    """
    ensure_members_editable(fleet)
    if not fields:
        raise FleetActionRefusedError(422, f"set names at least one of {', '.join(MEMBER_SETTING_FIELDS)}.")
    unknown = set(fields) - set(MEMBER_SETTING_FIELDS)
    if unknown:
        raise FleetActionRefusedError(422, f"set takes {', '.join(MEMBER_SETTING_FIELDS)}.")
    ids = list(dict.fromkeys(member_ids))
    if not ids:
        raise FleetActionRefusedError(422, "member_ids names at least one member.")
    if len(ids) > MAX_BULK_MEMBERS:
        raise FleetActionRefusedError(422, f"At most {MAX_BULK_MEMBERS} members per call.")
    values = {name: check_member_setting(name, fields[name]) for name in fields}
    rows = (
        (
            await db.execute(
                select(FleetMember)
                .where(FleetMember.fleet_id == fleet.id, FleetMember.id.in_(ids))
                .with_for_update()
            )
        )
        .scalars()
        .all()
    )
    by_id = {r.id: r for r in rows}
    missing = [str(i) for i in ids if i not in by_id]
    if missing:
        raise FleetActionRefusedError(404, f"Not members of this fleet: {', '.join(missing)}. Nothing was saved.")
    await check_scope_organizations(db, values.get("organization_scope"))  # type: ignore[arg-type]
    home = (
        [str(r.id) for r in rows if r.role == FLEET_ROLE_MEMBER] if set(values) & set(CONNECTED_ONLY_SETTINGS) else []
    )
    if home:
        raise FleetActionRefusedError(
            400,
            f"These are home members, the settings belong to connected agents: {', '.join(home)}. Nothing was saved.",
        )
    ordered = [by_id[i] for i in ids]
    for row in ordered:
        for name, value in values.items():
            setattr(row, name, value)
    await db.flush()
    return ordered


async def add_connected(
    db: AsyncSession,
    fleet: Fleet,
    *,
    agent: Agent | None = None,
    target_fleet: Fleet | None = None,
    collaboration_instruction: str | None = None,
    context_inclusion: str | None = None,
) -> FleetMember:
    """Connect an agent or a whole fleet to *fleet*. The caller checked visibility.

    A setting left ``None`` inherits the fleet default.

    Exactly one of *agent* and *target_fleet*. Refused: an archived fleet
    (409), this fleet itself, an agent home in this fleet, a link that already
    exists (409).
    """
    ensure_members_editable(fleet)
    if (agent is None) == (target_fleet is None):
        raise FleetActionRefusedError(422, "Connect either an agent or a fleet.")
    if target_fleet is not None and target_fleet.id == fleet.id:
        raise FleetActionRefusedError(400, "A fleet cannot be connected to itself.")
    if agent is not None:
        home = (await home_fleets_of(db, [agent.id])).get(agent.id)
        if home is not None and home.fleet_id == fleet.id:
            raise FleetActionRefusedError(409, f"'{agent.name}' is already a member of this fleet.")
    clause = (
        FleetMember.agent_id == agent.id if agent is not None else FleetMember.target_fleet_id == target_fleet.id  # type: ignore[union-attr]
    )
    if (await db.execute(select(FleetMember.id).where(FleetMember.fleet_id == fleet.id, clause))).first():
        raise FleetActionRefusedError(409, "Already connected to this fleet.")
    row = FleetMember(
        fleet_id=fleet.id,
        agent_id=agent.id if agent is not None else None,
        target_fleet_id=target_fleet.id if target_fleet is not None else None,
        role=FLEET_ROLE_CONNECTED,
        collaboration_instruction=collaboration_instruction,
        context_inclusion=context_inclusion,
    )
    db.add(row)
    await db.flush()
    return row


async def update_member(
    db: AsyncSession,
    fleet: Fleet,
    member_id: uuid.UUID,
    *,
    fields: dict,
) -> FleetMember:
    """Apply the fields set in a member update; refuses what the role forbids."""
    ensure_members_editable(fleet)
    row = await _member_row(db, fleet, member_id)
    await _settle_doc_dismissals(db, fleet)
    if "organization_scope" in fields:
        # Every row may set its own scope; ``None`` inherits the fleet's again.
        scope = check_member_setting("organization_scope", fields["organization_scope"])
        await check_scope_organizations(db, scope)  # type: ignore[arg-type]
        row.organization_scope = scope  # type: ignore[assignment]
    if row.role == FLEET_ROLE_MEMBER:
        if "collaboration_instruction" in fields or "context_inclusion" in fields:
            raise FleetActionRefusedError(400, "A collaboration instruction belongs to a connected agent.")
        if "parent_member_id" in fields:
            parent_id = fields["parent_member_id"]
            if parent_id is not None:
                await _ensure_parent(db, fleet, row, parent_id)
            row.parent_member_id = parent_id
        if "is_exposed" in fields:
            row.is_exposed = bool(fields["is_exposed"])
        if "role_label" in fields:
            row.role_label = (fields["role_label"] or "").strip()
    else:
        if "role_label" in fields:
            raise FleetActionRefusedError(400, "A role label belongs to a home member.")
        if "parent_member_id" in fields or "is_exposed" in fields:
            raise FleetActionRefusedError(400, "A connected agent has no place in the hierarchy and is not exposed.")
        # ``None`` resets the field: the row inherits the fleet default again.
        for name in CONNECTED_ONLY_SETTINGS:
            if name in fields:
                setattr(row, name, check_member_setting(name, fields[name]))
    await db.flush()
    return row


async def _settle_doc_dismissals(db: AsyncSession, fleet: Fleet) -> None:
    """Before a member edit, move the documentation's former dismissal records to their facet print (#12181).

    A role label, parent or exposure edit then lapses no dismissal of a point about the member's description
    or tools. Imported here: the documentation module reads this one.
    """
    from app.services.fleet_docs import settle_dismissals

    await settle_dismissals(db, fleet)


async def _ensure_parent(db: AsyncSession, fleet: Fleet, row: FleetMember, parent_id: uuid.UUID) -> None:
    """A parent is a home member of the same fleet that does not report to *row*."""
    if parent_id == row.id:
        raise FleetActionRefusedError(400, "A member cannot report to itself.")
    parent = await _member_row(db, fleet, parent_id)
    if parent.role != FLEET_ROLE_MEMBER:
        raise FleetActionRefusedError(400, "A member reports to another member of the fleet, never to a connected one.")
    rows = {m.id: m for m in await fleet_members(db, fleet.id)}
    seen: set[uuid.UUID] = set()
    cursor: uuid.UUID | None = parent_id
    while cursor is not None and cursor not in seen:
        if cursor == row.id:
            raise FleetActionRefusedError(400, "That parent reports to this member: the hierarchy would loop.")
        seen.add(cursor)
        cursor = rows[cursor].parent_member_id if cursor in rows else None


async def remove_member(db: AsyncSession, fleet: Fleet, member_id: uuid.UUID) -> FleetMember:
    """Delete one row; the agent keeps every handover, assignment and binding.

    Removing the orchestrator clears it on the fleet (the card then has no
    entry agent until one is set); its reports are lifted by ``SET NULL``.
    Refused on an archived fleet (409); deleting the fleet does not come here.
    """
    ensure_members_editable(fleet)
    row = await _member_row(db, fleet, member_id)
    if row.role == FLEET_ROLE_MEMBER and fleet.orchestrator_agent_id == row.agent_id:
        fleet.orchestrator_agent_id = None
    await db.delete(row)
    await db.flush()
    return row


async def _lock_fleets(db: AsyncSession, fleets: Sequence[Fleet]) -> None:
    """Hold the rows of *fleets* ``FOR UPDATE`` in id order and re-read what a move checks.

    Two moves in opposite directions take the locks in the same order, so one
    waits for the other instead of deadlocking; an archive or an orchestrator
    change committed since the caller loaded a fleet is seen.
    """
    ids = sorted({f.id for f in fleets})
    await db.execute(select(Fleet.id).where(Fleet.id.in_(ids)).order_by(Fleet.id).with_for_update())
    for fleet in fleets:
        await db.refresh(fleet, attribute_names=["is_archived", "is_company_managed", "orchestrator_agent_id"])


async def move_member(
    db: AsyncSession,
    source: Fleet,
    member_id: uuid.UUID,
    target: Fleet,
    *,
    manageable_agent: Callable[[uuid.UUID], Awaitable[Agent]],
) -> FleetMember:
    """Move a home member of *source* to *target* in one transaction.

    The caller checked the manage rights on both fleets; *manageable_agent*
    loads the member's agent with the caller's manage right on it (the REST
    route and the ``fleets`` tools each pass their own). The source row is
    deleted and the target row added under one savepoint: any refusal of the
    join (a personal agent in a company-managed fleet, the agent connected to
    *target*, a concurrent add that won the home-fleet index) rolls the
    savepoint back and leaves the source row whole, its parent, exposure and
    role label included. The source fleet's orchestrator is cleared only once
    the join succeeded. The agent itself is not written: its handovers,
    assignments and bindings stay, and no config version or evaluation
    follows.

    Refused: the same fleet (400), a connected row (400), an archived source
    or target (409), and every refusal of :func:`add_home_member`.
    """
    if source.id == target.id:
        raise FleetActionRefusedError(400, "The member is already in this fleet: pick another fleet.")
    await _lock_fleets(db, [source, target])
    ensure_members_editable(source)
    ensure_members_editable(target)
    row = await _member_row(db, source, member_id)
    if row.role != FLEET_ROLE_MEMBER or row.agent_id is None:
        raise FleetActionRefusedError(400, "Only a home member moves to another fleet; a connected row is removed.")
    agent = await manageable_agent(row.agent_id)
    was_orchestrator = source.orchestrator_agent_id == agent.id
    async with db.begin_nested():
        await db.delete(row)
        await db.flush()
        moved = await add_home_member(db, target, agent)
    if was_orchestrator:
        source.orchestrator_agent_id = None
        await db.flush()
    return moved


async def set_orchestrator(db: AsyncSession, fleet: Fleet, agent_id: uuid.UUID | None) -> None:
    """Make a home member the fleet's entry agent (``None`` clears it).

    The orchestrator is the root: its own parent is cleared.
    """
    if agent_id is None:
        if fleet.orchestrator_agent_id is not None:
            await drop_fleet_health_check(db, fleet.orchestrator_agent_id)
        fleet.orchestrator_agent_id = None
        return
    row = (
        await db.execute(
            select(FleetMember).where(
                FleetMember.fleet_id == fleet.id,
                FleetMember.agent_id == agent_id,
                FleetMember.role == FLEET_ROLE_MEMBER,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise FleetActionRefusedError(400, "The orchestrator must be a member of the fleet.")
    row.parent_member_id = None
    previous = fleet.orchestrator_agent_id
    fleet.orchestrator_agent_id = agent_id
    if previous is not None and previous != agent_id:
        await drop_fleet_health_check(db, previous)
    agent = await db.get(Agent, agent_id)
    if agent is not None:
        grant_program_status(agent)
        await grant_fleet_health_advisory(db, agent)
        await ensure_fleet_health_check(db, fleet, agent)
        await ensure_fleet_health_answered(db, fleet, agent)
        await ensure_fleet_past_due_listener(db, fleet, agent)


#: The live program reads every fleet orchestrator carries (#10979): a fleet's
#: multi-step efforts are program roots, read with these, never a register.
ORCHESTRATOR_PROGRAM_FUNCTIONS: tuple[str, ...] = ("list_programs", "get_program_status")


def grant_program_status(agent: Agent) -> bool:
    """Give *agent* the program reads of the ``coordination`` group; whether it changed.

    The selection is made canonical first (a former id such as ``shared_state``
    becomes ``coordination`` scoped to what it granted), then: an unscoped
    ``coordination`` already offers them; a scoped one gets them added to its
    allow list; none gets ``coordination`` scoped to these two functions alone.
    Nothing else of the agent's selection changes. Idempotent.
    """
    return grant_group_functions(agent, "coordination", ORCHESTRATOR_PROGRAM_FUNCTIONS)


def grant_group_functions(agent: Agent, group: str, functions: Sequence[str]) -> bool:
    """Give *agent* *functions* of the built-in *group*; whether it changed.

    The rule of :func:`grant_program_status` for any group: an unscoped group
    already offers them, a scoped one gets them added, none gets the group
    scoped to these functions alone. Idempotent.

    A group this adds scoped is a list the platform wrote (#39): it is marked
    in the agent's ``config`` so the functions later releases add to the
    group reach the agent without an allowlist notice.
    """
    from app.services.builtin_allowed_tools import with_implicit_scope
    from app.services.builtin_tool_aliases import canonical_builtin_selection

    before_ids = list(agent.builtin_tool_ids or [])
    before_allowed = getattr(agent, "builtin_allowed_tools", None) or None
    ids, allowed = canonical_builtin_selection(before_ids, before_allowed)
    allowed = dict(allowed or {})
    if group not in ids:
        ids.append(group)
        allowed[group] = list(functions)
        agent.config = with_implicit_scope(getattr(agent, "config", None), add=[group])
    elif allowed.get(group) is not None:
        kept = list(allowed[group])
        allowed[group] = kept + [fn for fn in functions if fn not in kept]
    after_allowed = allowed or None
    if canonical_builtin_selection(before_ids, before_allowed) == (ids, after_allowed):
        return False
    agent.builtin_tool_ids = ids
    agent.builtin_allowed_tools = after_allowed
    return True


#: What every fleet orchestrator carries to advise on its fleet's health (#11700).
#: ``dismiss_fleet_health_findings`` applies an owner's "ignore" answer (#1406).
FLEET_HEALTH_FUNCTIONS: tuple[str, ...] = ("get_fleet_health", "dismiss_fleet_health_findings")
#: The documentation tools every fleet orchestrator persona calls (#1631): the
#: regeneration after a configuration change, and the About-page suggestions
#: ``get_fleet_health`` counts and points to.
FLEET_DOC_FUNCTIONS: tuple[str, ...] = ("regenerate_fleet_documentation", "list_fleet_doc_findings")
FLEET_HEALTH_SKILL_SLUG = "fleet-health-advisory"
#: The suggested practices of a fleet (#11948), worded as recommendations.
FLEET_DESIGN_SKILL_SLUG = "fleet-design-practices"
#: The seeded skills every fleet orchestrator carries, bound in this order.
FLEET_ORCHESTRATOR_SKILL_SLUGS: tuple[str, ...] = (FLEET_HEALTH_SKILL_SLUG, FLEET_DESIGN_SKILL_SLUG)


async def grant_fleet_health_advisory(db: AsyncSession, agent: Agent) -> bool:
    """Give *agent* ``get_fleet_health`` and the documentation tools
    (:data:`FLEET_DOC_FUNCTIONS`), and bind the seeded orchestrator skills
    (:data:`FLEET_ORCHESTRATOR_SKILL_SLUGS`: ``fleet-health-advisory`` and
    ``fleet-design-practices``); whether anything changed. A skill this instance
    has not seeded is skipped (not an error). Idempotent."""
    from app.models.agent_skill import AgentSkill

    changed = grant_group_functions(agent, "platform", (*FLEET_HEALTH_FUNCTIONS, *FLEET_DOC_FUNCTIONS))
    rows = (
        await db.execute(select(AgentSkill.id, AgentSkill.tags).where(AgentSkill.is_company_managed.is_(True)))
    ).all()
    current = [str(s) for s in (agent.skill_ids or [])]
    for slug in FLEET_ORCHESTRATOR_SKILL_SLUGS:
        tag = f"slug:{slug}"
        skill_id = next((str(sid) for sid, tags in rows if tag in (tags or [])), None)
        if skill_id is None or skill_id in current:
            continue
        current.append(skill_id)
        agent.skill_ids = [*(agent.skill_ids or []), skill_id]
        changed = True
    return changed


#: The weekly built-in health check of a fleet (#11700 S2): one assignment on
#: its orchestrator, born off (Enable fleet lists it), gated at zero tokens on
#: ``get_fleet_health`` so a healthy fleet costs no model call.
FLEET_HEALTH_CHECK_NAME = "Weekly fleet health check"
#: Mondays 07:00 UTC, before the working week.
FLEET_HEALTH_CHECK_CRON = "0 7 * * 1"
FLEET_HEALTH_CHECK_MAX_ITERATIONS = 25
FLEET_HEALTH_CHECK_DETAILS = "Fleet health check: off until the fleet is enabled."
#: Key prefix of the owner's decision items the health check files (``fleet-health:<fleet id>``,
#: suffixes allowed), taken by the "Fleet health answered" listener (#1406).
FLEET_HEALTH_KEY_PREFIX = "fleet-health:"
#: The listener that applies an owner's answer to a fleet health decision item (#1406).
FLEET_HEALTH_ANSWERED_NAME = "Fleet health answered"
#: The listener that reacts when a member's work item crosses its due date (#1662).
FLEET_PAST_DUE_NAME = "Overdue work items"
#: The fleet service's own assignments of an orchestrator: never seed rows, never exported.
FLEET_HEALTH_ASSIGNMENT_NAMES: frozenset[str] = frozenset(
    {FLEET_HEALTH_CHECK_NAME, FLEET_HEALTH_ANSWERED_NAME, FLEET_PAST_DUE_NAME}
)


def fleet_health_check_gate(fleet_id: uuid.UUID) -> dict[str, object]:
    """The activation check: run only when ``get_fleet_health`` reads the fleet unhealthy."""
    return {
        "tool": "get_fleet_health",
        "arguments": {"fleet_id": str(fleet_id)},
        "condition": {"path": "$.healthy", "in": [False]},
    }


def fleet_health_check_prompt(fleet: Fleet) -> str:
    return (
        f"Weekly health check of the fleet '{fleet.name}' (fleet_id {fleet.id}). The gate starts this run only "
        "when it read the fleet unhealthy; when it could not read it, the run record names the check as not "
        "evaluated, so never take the finding for granted. Follow the skill 'Fleet health advisory': read "
        "get_fleet_health yourself first; when it does not answer for this fleet, stop and file nothing. "
        "Otherwise apply the safe fixes once "
        "(apply_safe_fixes=true), then send the fleet owner one deduplicated advisory for the owner "
        "findings, with a recommendation and the evidence ids per finding. A decision item for the "
        f"owner is keyed {FLEET_HEALTH_KEY_PREFIX}{fleet.id} (a suffix after it when there are several). "
        "Nothing else."
    )


def fleet_health_answered_trigger() -> dict[str, object]:
    """The answered decision items of the health check: blocked back to open, own items, its key."""
    return {
        "event": "work_item.status_changed",
        "filter": {
            "previous_status": "blocked",
            "status": "open",
            "correlation_key": {"starts_with": [FLEET_HEALTH_KEY_PREFIX]},
        },
        "scope": "own",
    }


def fleet_health_answered_prompt(fleet: Fleet) -> str:
    return (
        f"The owner of the fleet '{fleet.name}' (fleet_id {fleet.id}) answered a fleet health decision: "
        "the work item in the trigger below went from blocked to open. Claim it first with "
        "claim_work_item; when the claim is refused another run already applies this answer: stop there. "
        "Read it with get_work_item: payload.answer holds the option chosen or a free text. An ignore "
        "(the finding needs nothing): dismiss each finding the item names once with "
        "dismiss_fleet_health_findings(fleet_id, ids, reason), the owner's words as the reason. A fix: "
        "apply it only when it is a safe fix of the skill 'Fleet health advisory' (section 3); an owner "
        "fix is the owner's to make, so record it as theirs. Complete the item with the decision and "
        "what was applied. Nothing else."
    )


def is_fleet_health_answered(assignment: object) -> bool:
    """Whether *assignment* is a fleet's seeded "Fleet health answered" listener."""
    config = getattr(assignment, "trigger_config", None)
    return (
        getattr(assignment, "name", None) == FLEET_HEALTH_ANSWERED_NAME
        and getattr(assignment, "trigger_type", None) == "platform_event"
        and isinstance(config, dict)
        and config.get("event") == "work_item.status_changed"
    )


def fleet_past_due_trigger() -> dict[str, object]:
    """The work items of the fleet's members that crossed their due date (#1662)."""
    return {"event": "work_item.past_due", "scope": "fleet"}


def fleet_past_due_prompt(fleet: Fleet) -> str:
    return (
        f"A work item of the fleet '{fleet.name}' (fleet_id {fleet.id}) crossed its due date: the trigger "
        "below names it (work_item_id, owner_agent_id, due_at, overdue_seconds). Read it with get_work_item. "
        "When it is done or cancelled meanwhile, or its due date moved ahead, stop there. Otherwise make it "
        "move once: a member owns it and it waits for nothing, add a note naming the overdue date and the "
        "next step so its owner's inbox takes it; it waits on a blocker, a person or no owner, follow the "
        "skill 'Fleet health advisory' for a past_due finding (the owner decides: a new due date, a new "
        "owner or a cancellation, with one deduplicated advisory). Never set a due date of your own. "
        "Nothing else."
    )


def is_fleet_past_due_listener(assignment: object) -> bool:
    """Whether *assignment* is a fleet's seeded "Overdue work items" listener."""
    config = getattr(assignment, "trigger_config", None)
    return (
        getattr(assignment, "name", None) == FLEET_PAST_DUE_NAME
        and getattr(assignment, "trigger_type", None) == "platform_event"
        and isinstance(config, dict)
        and config.get("event") == "work_item.past_due"
    )


async def ensure_fleet_past_due_listener(db: AsyncSession, fleet: Fleet, agent: Agent, check: Any = None) -> bool:
    """Give the orchestrator *agent* the listener of its members' overdue items; whether it was created.

    Same life as "Fleet health answered": born with the switch and run-as of
    the orchestrator's seeded health check, none without one nor on the
    Platform Health fleet; an existing one keeps its switch and follows *fleet*.
    """
    from app.services.health.read_only import is_health_fleet

    if is_health_fleet(fleet):
        return False
    from app.models.assignment import Assignment
    from app.services.assignment_pause import CREATED_DISABLED_REASON, is_emergency_stopped, pause_assignment

    existing = (
        (
            await db.execute(
                select(Assignment).where(Assignment.agent_id == agent.id, Assignment.name == FLEET_PAST_DUE_NAME)
            )
        )
        .scalars()
        .all()
    )
    found = next((row for row in existing if is_fleet_past_due_listener(row)), None)
    if found is not None:
        found.prompt = fleet_past_due_prompt(fleet)
        found.ai_label = False
        return False
    if check is None:
        check = next(iter(await _seeded_health_checks(db, agent.id)), None)
    if check is None:
        return False
    listener = Assignment(
        agent_id=agent.id,
        user_id=agent.user_id,
        is_company_managed=bool(agent.is_company_managed),
        name=FLEET_PAST_DUE_NAME,
        description="Makes a member's work item move once it crosses its due date, or asks its owner.",
        trigger_type="platform_event",
        trigger_config=fleet_past_due_trigger(),
        prompt=fleet_past_due_prompt(fleet),
        enabled=bool(getattr(check, "enabled", False)) and not is_emergency_stopped(check),
        # Platform-seeded: no AI label prompt, so its work items and notes carry no "[AI-generated]" prefix.
        ai_label=False,
        max_iterations=FLEET_HEALTH_CHECK_MAX_ITERATIONS,
        run_as_all_users=False,
    )
    listener.run_as_user_id = getattr(check, "run_as_user_id", None)
    if not listener.enabled:
        pause_assignment(
            listener,
            reason=CREATED_DISABLED_REASON,
            details=FLEET_HEALTH_CHECK_DETAILS,
            actor_user_id=None,
            actor_label="Fleet health",
        )
    db.add(listener)
    return True


async def ensure_fleet_health_answered(db: AsyncSession, fleet: Fleet, agent: Agent, check: Any = None) -> bool:
    """Give the orchestrator *agent* the listener of its health decisions; whether it was created.

    Born with the switch and run-as of the orchestrator's seeded health check,
    so a fleet whose check already runs gets it running on upgrade, and one not
    set up lists both in Enable fleet (off, ``created_disabled``). An existing
    one keeps its switch and follows *fleet*. The Platform Health fleet gets
    none, as it gets no check. Idempotent.
    """
    from app.services.health.read_only import is_health_fleet

    if is_health_fleet(fleet):
        return False
    from app.models.assignment import Assignment
    from app.services.assignment_pause import CREATED_DISABLED_REASON, is_emergency_stopped, pause_assignment

    existing = (
        (
            await db.execute(
                select(Assignment).where(Assignment.agent_id == agent.id, Assignment.name == FLEET_HEALTH_ANSWERED_NAME)
            )
        )
        .scalars()
        .all()
    )
    found = next((row for row in existing if is_fleet_health_answered(row)), None)
    if found is not None:
        found.prompt = fleet_health_answered_prompt(fleet)
        found.ai_label = False
        return False
    if check is None:
        check = next(iter(await _seeded_health_checks(db, agent.id)), None)
    if check is None:
        # No seeded check (the fleet opted out): nothing files the decision items it would take.
        return False
    listener = Assignment(
        agent_id=agent.id,
        user_id=agent.user_id,
        is_company_managed=bool(agent.is_company_managed),
        name=FLEET_HEALTH_ANSWERED_NAME,
        description="Applies the owner's answer to a fleet health decision: dismiss, or record the fix.",
        trigger_type="platform_event",
        trigger_config=fleet_health_answered_trigger(),
        prompt=fleet_health_answered_prompt(fleet),
        enabled=bool(getattr(check, "enabled", False)) and not is_emergency_stopped(check),
        # Platform-seeded: no AI label prompt, so its work items and notes carry no "[AI-generated]" prefix.
        ai_label=False,
        max_iterations=FLEET_HEALTH_CHECK_MAX_ITERATIONS,
        run_as_all_users=False,
    )
    listener.run_as_user_id = getattr(check, "run_as_user_id", None)
    if not listener.enabled:
        pause_assignment(
            listener,
            reason=CREATED_DISABLED_REASON,
            details=FLEET_HEALTH_CHECK_DETAILS,
            actor_user_id=None,
            actor_label="Fleet health",
        )
    db.add(listener)
    return True


def is_fleet_health_check(assignment: object) -> bool:
    """Whether *assignment* is a fleet's weekly built-in health check."""
    check = getattr(assignment, "activation_check", None)
    return (
        getattr(assignment, "name", None) == FLEET_HEALTH_CHECK_NAME
        and isinstance(check, dict)
        and check.get("tool") == "get_fleet_health"
    )


#: ``Fleet.health_check`` modes: the seeded check (``NULL``), none, or one of the fleet's own.
HEALTH_CHECK_DEFAULT = "default"
HEALTH_CHECK_OFF = "off"
HEALTH_CHECK_OWN = "own"


def fleet_health_check_mode(fleet: object) -> str:
    """How *fleet* checks its health: ``default`` (the seeded check), ``off`` or ``own``."""
    stored = getattr(fleet, "health_check", None)
    mode = stored.get("mode") if isinstance(stored, dict) else None
    return mode if mode in (HEALTH_CHECK_OFF, HEALTH_CHECK_OWN) else HEALTH_CHECK_DEFAULT


def _health_check_record(mode: str, actor: str | None, assignment_id: uuid.UUID | None = None) -> dict[str, object]:
    from datetime import UTC, datetime

    record: dict[str, object] = {"mode": mode, "by": actor, "at": datetime.now(UTC).isoformat()}
    if assignment_id is not None:
        record["assignment_id"] = str(assignment_id)
    return record


async def _seeded_health_checks(db: AsyncSession, agent_id: uuid.UUID) -> list[object]:
    from app.models.assignment import Assignment

    rows = (
        (
            await db.execute(
                select(Assignment).where(Assignment.agent_id == agent_id, Assignment.name == FLEET_HEALTH_CHECK_NAME)
            )
        )
        .scalars()
        .all()
    )
    return [row for row in rows if is_fleet_health_check(row)]


async def _seeded_health_listeners(db: AsyncSession, agent_id: uuid.UUID) -> list[Any]:
    from app.models.assignment import Assignment

    rows = (
        (
            await db.execute(
                select(Assignment).where(
                    Assignment.agent_id == agent_id,
                    Assignment.name.in_((FLEET_HEALTH_ANSWERED_NAME, FLEET_PAST_DUE_NAME)),
                )
            )
        )
        .scalars()
        .all()
    )
    return [row for row in rows if is_fleet_health_answered(row) or is_fleet_past_due_listener(row)]


async def set_fleet_health_check(
    db: AsyncSession, fleet: Fleet, choice: str, *, actor: str | None
) -> dict[str, object] | None:
    """Set how *fleet* checks its health; the stored record (``None`` = the default).

    *choice* is ``default`` (the seeded check, created again when missing),
    ``off`` (no check) or the id of an assignment of a home member, which then
    is the fleet's health check. Leaving the default deletes the seeded check of
    the orchestrator, its runs kept. Refused (400) on any other value.
    """
    from app.models.assignment import Assignment
    from app.services.assignment_retire import detach_runs_clause

    text = (choice or "").strip()
    lowered = text.lower()
    if lowered == HEALTH_CHECK_DEFAULT:
        fleet.health_check = None
        if fleet.orchestrator_agent_id is not None:
            agent = await db.get(Agent, fleet.orchestrator_agent_id)
            if agent is not None:
                await ensure_fleet_health_check(db, fleet, agent)
                await ensure_fleet_health_answered(db, fleet, agent)
                await ensure_fleet_past_due_listener(db, fleet, agent)
        return None
    if lowered == HEALTH_CHECK_OFF:
        record = _health_check_record(HEALTH_CHECK_OFF, actor)
    else:
        try:
            assignment_id = uuid.UUID(text)
        except ValueError:
            raise FleetActionRefusedError(
                400, "health_check is 'default', 'off' or the id of an assignment of a home member."
            ) from None
        member_ids = (
            (
                await db.execute(
                    select(FleetMember.agent_id).where(
                        FleetMember.fleet_id == fleet.id, FleetMember.role == FLEET_ROLE_MEMBER
                    )
                )
            )
            .scalars()
            .all()
        )
        assignment = await db.get(Assignment, assignment_id)
        if assignment is None or assignment.agent_id not in set(member_ids) or is_fleet_health_check(assignment):
            raise FleetActionRefusedError(
                400, "The health check must be an assignment of a home member of the fleet, not the seeded one."
            )
        record = _health_check_record(HEALTH_CHECK_OWN, actor, assignment_id)
    fleet.health_check = record
    if fleet.orchestrator_agent_id is not None:
        for seeded in [
            *await _seeded_health_checks(db, fleet.orchestrator_agent_id),
            *await _seeded_health_listeners(db, fleet.orchestrator_agent_id),
        ]:
            await db.execute(detach_runs_clause(seeded.id))
            await db.delete(seeded)
    return record


async def record_health_check_deleted(db: AsyncSession, assignment: object, *, actor: str | None) -> bool:
    """Record the deletion of a seeded health check as its fleet's opt-out; whether it did.

    Called by every person-driven deletion of an assignment (the API, the
    ``delete_assignment`` tool, the retirement after a run), so the boot
    backfill never re-creates a check a person deleted. A fleet already off or
    on its own check keeps its record. The caller commits.
    """
    if not is_fleet_health_check(assignment):
        return False
    arguments = (getattr(assignment, "activation_check", None) or {}).get("arguments") or {}
    try:
        fleet_id = uuid.UUID(str(arguments.get("fleet_id")))
    except ValueError:
        return False
    fleet = await db.get(Fleet, fleet_id)
    if fleet is None or fleet_health_check_mode(fleet) != HEALTH_CHECK_DEFAULT:
        return False
    if fleet.orchestrator_agent_id != getattr(assignment, "agent_id", None):
        return False
    fleet.health_check = _health_check_record(HEALTH_CHECK_OFF, actor)
    return True


async def ensure_fleet_health_check(db: AsyncSession, fleet: Fleet, agent: Agent) -> bool:
    """Give *fleet*'s orchestrator *agent* the weekly health check; whether it was created.

    Born switched off with the ``created_disabled`` stamp, so the fleet reads
    "Not set up" and Enable fleet lists it; an existing one keeps its switch
    and only has its gate re-pointed at *fleet*. A fleet that opted out
    (:func:`fleet_health_check_mode` ``off`` or ``own``) gets none. Idempotent.
    The Platform Health fleet gets none either (#974): Platform Health checks
    itself, and the read-only fleet could never switch the check on.
    """
    from app.models.assignment import Assignment
    from app.services.assignment_pause import CREATED_DISABLED_REASON, pause_assignment
    from app.services.health.read_only import is_health_fleet

    if is_health_fleet(fleet):
        return False

    existing = (
        (
            await db.execute(
                select(Assignment).where(Assignment.agent_id == agent.id, Assignment.name == FLEET_HEALTH_CHECK_NAME)
            )
        )
        .scalars()
        .first()
    )
    gate = fleet_health_check_gate(fleet.id)
    if existing is not None:
        if existing.activation_check != gate:
            existing.activation_check = gate
            existing.prompt = fleet_health_check_prompt(fleet)
        existing.ai_label = False
        return False
    if fleet_health_check_mode(fleet) != HEALTH_CHECK_DEFAULT:
        return False
    assignment = Assignment(
        agent_id=agent.id,
        user_id=agent.user_id,
        is_company_managed=bool(agent.is_company_managed),
        name=FLEET_HEALTH_CHECK_NAME,
        description="Weekly fleet health advisory; runs only when the fleet has a finding.",
        trigger_type="schedule",
        trigger_config={"cron": FLEET_HEALTH_CHECK_CRON},
        prompt=fleet_health_check_prompt(fleet),
        activation_check=gate,
        enabled=False,
        ai_label=False,
        max_iterations=FLEET_HEALTH_CHECK_MAX_ITERATIONS,
        run_as_all_users=False,
    )
    pause_assignment(
        assignment,
        reason=CREATED_DISABLED_REASON,
        details=FLEET_HEALTH_CHECK_DETAILS,
        actor_user_id=None,
        actor_label="Fleet health",
    )
    db.add(assignment)
    return True


async def drop_fleet_health_check(db: AsyncSession, agent_id: uuid.UUID) -> bool:
    """Delete the weekly health check of a former orchestrator, if it never ran.

    One that ran is left as it is (its runs are history); it gates on a fleet
    the agent no longer leads, which reads healthy or refuses, so it stays quiet.
    """
    from app.models.assignment import Assignment
    from app.services.assignment_pause import has_never_run

    rows = (
        (
            await db.execute(
                select(Assignment).where(Assignment.agent_id == agent_id, Assignment.name == FLEET_HEALTH_CHECK_NAME)
            )
        )
        .scalars()
        .all()
    )
    dropped = False
    candidates: list[Any] = [*rows, *await _seeded_health_listeners(db, agent_id)]
    for assignment in candidates:
        seeded = (
            is_fleet_health_check(assignment)
            or is_fleet_health_answered(assignment)
            or is_fleet_past_due_listener(assignment)
        )
        if seeded and not assignment.enabled and has_never_run(assignment):
            await db.delete(assignment)
            dropped = True
    return dropped


def _gated_fleet_id(assignment: object) -> uuid.UUID | None:
    """The fleet id a seeded health check gates on, ``None`` when it names none."""
    arguments = (getattr(assignment, "activation_check", None) or {}).get("arguments") or {}
    try:
        return uuid.UUID(str(arguments.get("fleet_id")))
    except ValueError:
        return None


#: How a work item left open by a health check gated on a deleted fleet is closed.
MOOT_HEALTH_ITEM_RESULT = (
    "Cancelled as moot by the platform: it came from a weekly fleet health check gated on fleet {fleet_id}, "
    "which no longer exists (#1400). The check was {action}."
)


async def _cancel_moot_health_items(
    db: AsyncSession, fleet_id: uuid.UUID, run_ids: Sequence[uuid.UUID], action: str
) -> int:
    """Cancel the open items a health check gated on the gone *fleet_id* created; how many.

    Those keyed ``fleet-health:<fleet_id>:*`` and those its runs (*run_ids*)
    filed, ownerless ones included.
    """
    from sqlalchemy import or_

    from app.models.work_item import WORK_ITEM_TERMINAL_STATUSES, WorkItem

    clauses = [WorkItem.correlation_key.like(f"fleet-health:{fleet_id}:%")]
    if run_ids:
        clauses.append(WorkItem.origin_run_id.in_(list(run_ids)))
    items = (
        (await db.execute(select(WorkItem).where(or_(*clauses), WorkItem.status.notin_(WORK_ITEM_TERMINAL_STATUSES))))
        .scalars()
        .all()
    )
    for item in items:
        item.status = "cancelled"
        item.needs_human = False
        item.result = MOOT_HEALTH_ITEM_RESULT.format(fleet_id=fleet_id, action=action)
    return len(items)


async def repair_orphaned_fleet_health_checks(db: AsyncSession) -> int:
    """Re-point or delete the seeded health checks gated on a fleet that no longer exists (#1400); how many.

    A fleet deleted or absorbed (a built-in fleet swept by its successor)
    leaves its former orchestrator's weekly check gated on a missing id, ran
    or not. The check is re-pointed at the live fleet the agent leads when
    that fleet has no seeded check of the agent yet, else deleted with its
    runs kept; the open items its runs filed are cancelled as moot. Run at
    every boot and after a sweep, so an instance seeded before the fix is
    repaired at source. The caller commits.
    """
    import logging

    from app.models.assignment import Assignment, AssignmentRun
    from app.services.assignment_retire import detach_runs_clause

    logger = logging.getLogger(__name__)
    checks = [
        row
        for row in (await db.execute(select(Assignment).where(Assignment.name == FLEET_HEALTH_CHECK_NAME)))
        .scalars()
        .all()
        if is_fleet_health_check(row)
    ]
    if not checks:
        return 0
    gated = {row.id: _gated_fleet_id(row) for row in checks}
    wanted = {fid for fid in gated.values() if fid is not None}
    live = set((await db.execute(select(Fleet.id).where(Fleet.id.in_(wanted)))).scalars().all() if wanted else [])
    repaired = 0
    for row in checks:
        fleet_id = gated[row.id]
        if fleet_id is None or fleet_id in live:
            continue
        led = (await db.execute(select(Fleet).where(Fleet.orchestrator_agent_id == row.agent_id))).scalars().first()
        already = led is not None and any(
            other.agent_id == row.agent_id and gated[other.id] == led.id for other in checks if other is not row
        )
        run_ids = (
            (await db.execute(select(AssignmentRun.id).where(AssignmentRun.assignment_id == row.id))).scalars().all()
        )
        if led is not None and not already:
            row.activation_check = fleet_health_check_gate(led.id)
            row.prompt = fleet_health_check_prompt(led)
            gated[row.id] = led.id
            action = f"re-pointed at fleet {led.id}"
        else:
            await db.execute(detach_runs_clause(row.id))
            await db.delete(row)
            action = "deleted"
        cancelled = await _cancel_moot_health_items(db, fleet_id, run_ids, action)
        logger.info(
            "Fleet health check %s of agent %s gated on missing fleet %s: %s; %d moot item(s) cancelled",
            row.id,
            row.agent_id,
            fleet_id,
            action,
            cancelled,
        )
        repaired += 1
    if repaired:
        await db.flush()
    return repaired


async def gate_fleet_refusal(db: AsyncSession, check: object) -> str | None:
    """Why an activation check may not be saved: a ``fleet_id`` it names does not exist (#1400).

    Read on every check of a chain (``also`` / ``or``); a placeholder or a
    value that is not an id is left to the run. ``None`` when it may be saved.
    """
    from app.services.assignment_activation_check import chained_checks, stored_activation_check

    stored = stored_activation_check(check)
    if stored is None:
        return None
    for one in chained_checks(stored):
        raw = (one.get("arguments") or {}).get("fleet_id")
        if not isinstance(raw, str):
            continue
        try:
            fleet_id = uuid.UUID(raw.strip())
        except ValueError:
            continue
        if await db.get(Fleet, fleet_id) is None:
            return (
                f"The gate check {one.get('tool')} names fleet_id {fleet_id}, which does not exist: "
                "it could never be evaluated. Name an existing fleet."
            )
    return None


def grant_health_check_tools(agent: Agent) -> bool:
    """Give *agent*, which holds a weekly health check, the tools its gate and run call (#1428).

    Its built-in selection is made canonical (a former id such as
    ``work_items`` becomes its group), then the ``platform`` group gets
    :data:`FLEET_HEALTH_FUNCTIONS`: scoped to those alone when the agent did
    not enable it, added to a scoped list, an unscoped group left as is. An
    allow-list entry left for a group the agent does not enable is dropped.
    Whether anything changed. Idempotent.
    """
    from app.services.builtin_allowed_tools import without_groups_not_enabled
    from app.services.builtin_tool_aliases import fold_builtin_selection

    folded = fold_builtin_selection(agent)
    granted = grant_group_functions(agent, "platform", FLEET_HEALTH_FUNCTIONS)
    pruned = without_groups_not_enabled(agent.builtin_tool_ids or [], agent.builtin_allowed_tools)
    stale = pruned != (agent.builtin_allowed_tools or None)
    if stale:
        agent.builtin_allowed_tools = pruned
    return folded or granted or stale


async def grant_health_check_holders(db: AsyncSession) -> int:
    """Grant :func:`grant_health_check_tools` to every agent holding a weekly health check; how many changed.

    A desk lead seeded with a check but without the ``platform`` group had
    its gate skipped as misconfigured on every firing (#1428). Run at boot
    after the seed rewrote the built-in agents' ids, so an instance seeded
    before the fix is repaired without a manual change. The caller commits.
    """
    from app.models.assignment import Assignment

    rows = (await db.execute(select(Assignment).where(Assignment.name == FLEET_HEALTH_CHECK_NAME))).scalars().all()
    agent_ids = list(dict.fromkeys(row.agent_id for row in rows if is_fleet_health_check(row) and row.agent_id))
    changed = 0
    for agent_id in agent_ids:
        agent = await db.get(Agent, agent_id)
        if agent is not None and grant_health_check_tools(agent):
            changed += 1
    if changed:
        await db.flush()
    return changed


async def backfill_fleet_health_advisory(db: AsyncSession) -> int:
    """Give every fleet orchestrator the health advisory (#11700); orchestrators changed.

    Run at boot after the skills and the built-in fleets are seeded, so a fleet
    created before the skill existed gets it too: the tool, the skill and the
    weekly health check (born off). Idempotent. First, the checks gated on a
    fleet that no longer exists are re-pointed or deleted (#1400).
    """
    repaired = await repair_orphaned_fleet_health_checks(db)
    fleets = (await db.execute(select(Fleet).where(Fleet.orchestrator_agent_id.isnot(None)))).scalars().all()
    changed = repaired
    for fleet in fleets:
        agent = await db.get(Agent, fleet.orchestrator_agent_id)
        if agent is None:
            continue
        granted = await grant_fleet_health_advisory(db, agent)
        created = await ensure_fleet_health_check(db, fleet, agent)
        listening = await ensure_fleet_health_answered(db, fleet, agent)
        overdue = await ensure_fleet_past_due_listener(db, fleet, agent)
        if granted or created or listening or overdue:
            changed += 1
    changed += await grant_health_check_holders(db)
    if changed:
        await db.commit()
    return changed


async def set_exposed_agents(db: AsyncSession, fleet: Fleet, agent_ids: Sequence[uuid.UUID]) -> list[uuid.UUID]:
    """Replace the exposure list (the orchestrator stays exposed regardless).

    Every id must be a home member of the fleet (400 naming the first that is
    not); refused on an archived fleet (409). Returns the exposed ids as
    stored, the orchestrator included.
    """
    ensure_members_editable(fleet)
    await _settle_doc_dismissals(db, fleet)
    wanted = set(agent_ids)
    homes = [m for m in await fleet_members(db, fleet.id) if m.role == FLEET_ROLE_MEMBER]
    home_ids = {m.agent_id for m in homes}
    stray = [a for a in agent_ids if a not in home_ids]
    if stray:
        raise FleetActionRefusedError(400, f"Agent {stray[0]} is not a member of this fleet.")
    for m in homes:
        m.is_exposed = m.agent_id in wanted
    await db.flush()
    return exposed_agent_ids(fleet, homes)


def exposed_agent_ids(fleet: Fleet, members: Iterable[FleetMember]) -> list[uuid.UUID]:
    """The home members other fleets may reach: the orchestrator, then the flagged."""
    out: list[uuid.UUID] = []
    if fleet.orchestrator_agent_id is not None:
        out.append(fleet.orchestrator_agent_id)
    for m in members:
        if m.role == FLEET_ROLE_MEMBER and m.is_exposed and m.agent_id not in out and m.agent_id is not None:
            out.append(m.agent_id)
    return out


# ── Handovers across a fleet boundary ────────────────────────────────────────


def _as_uuids(raw: Iterable[object] | None) -> list[uuid.UUID]:
    out: list[uuid.UUID] = []
    for value in raw or []:
        try:
            out.append(value if isinstance(value, uuid.UUID) else uuid.UUID(str(value)))
        except (TypeError, ValueError):
            continue
    return out


async def _crossing_unexposed(
    db: AsyncSession, source_id: uuid.UUID | None, target_ids: list[uuid.UUID]
) -> dict[uuid.UUID, HomeFleet]:
    """The targets home in a fleet the source is not home in, and not exposed by it."""
    homes = await home_fleets_of(db, [*target_ids, *([source_id] if source_id else [])])
    source_fleet = homes.get(source_id).fleet_id if source_id and source_id in homes else None
    return {
        t: homes[t]
        for t in target_ids
        if t in homes and homes[t].fleet_id != source_fleet and not homes[t].is_exposed
    }


async def refused_cross_fleet_handovers(
    db: AsyncSession,
    *,
    source_agent_id: uuid.UUID | None,
    handover_agent_ids: Iterable[object] | None,
    baseline: Iterable[object] | None = None,
) -> None:
    """Refuse (422) a NEW handover to a non-exposed member of another fleet.

    Only the ids added by this write are checked: one already configured
    (*baseline*) is kept and reported by :func:`fleet_boundary_violations`,
    never broken by an unrelated edit of the agent. An agent in no fleet is
    outside every fleet, so the rule applies to it as to a member of another.
    """
    before = set(_as_uuids(baseline))
    added = [t for t in _as_uuids(handover_agent_ids) if t not in before and t != source_agent_id]
    if not added:
        return
    refused = await _crossing_unexposed(db, source_agent_id, added)
    if not refused:
        return
    names = dict(
        (await db.execute(select(Agent.id, Agent.name).where(Agent.id.in_(list(refused))))).all()
    )
    first_id, first = next(iter(refused.items()))
    raise FleetActionRefusedError(
        422,
        f"'{names.get(first_id, first_id)}' is a member of the fleet '{first.fleet_name}' and is not exposed "
        "to other fleets. Hand over to the fleet's orchestrator or one of its exposed agents, "
        "or ask the fleet's owner to expose this agent.",
    )


@dataclass(frozen=True)
class BoundaryViolation:
    """A configured handover into this fleet that its exposure no longer allows."""

    source_agent_id: uuid.UUID
    target_agent_id: uuid.UUID


async def fleet_boundary_violations(db: AsyncSession, fleet: Fleet) -> list[BoundaryViolation]:
    """Handovers from outside *fleet* to its non-exposed members, kept and flagged.

    Read from the agents' own ``handover_agent_ids``: a membership or an
    exposure change never edits an agent, so what it made invalid is reported
    here for the owner to settle.
    """
    members = await fleet_members(db, fleet.id)
    homes = [m for m in members if m.role == FLEET_ROLE_MEMBER and m.agent_id is not None]
    home_ids = {m.agent_id for m in homes}
    hidden = home_ids - set(exposed_agent_ids(fleet, homes))
    if not hidden:
        return []
    # A JSON list column: filter in Python over the agents that hand over at
    # all, outside this fleet. The candidates are the agents that name a
    # hidden member; the table scan is bounded by agents with handovers.
    rows = (
        await db.execute(
            select(Agent.id, Agent.handover_agent_ids).where(
                Agent.handover_agent_ids.isnot(None), Agent.id.notin_(list(home_ids))
            )
        )
    ).all()
    out: list[BoundaryViolation] = []
    for source_id, targets in rows:
        for target in _as_uuids(targets):
            if target in hidden:
                out.append(BoundaryViolation(source_agent_id=source_id, target_agent_id=target))
    return out


# ── The Fleet Card ───────────────────────────────────────────────────────────


async def build_fleet_card(db: AsyncSession, fleet: Fleet) -> dict:
    """The fleet as other fleets see it: an A2A-shaped card.

    Name, description, skills, the entry agent (the orchestrator), the exposed
    agents, the handover types accepted, the workspace's name. Nothing about the
    non-exposed members: the card is what crosses the boundary.
    """
    members = await fleet_members(db, fleet.id)
    exposed = exposed_agent_ids(fleet, members)
    agents = {
        a.id: a
        for a in (await db.execute(select(Agent).where(Agent.id.in_(exposed)))).scalars().all()
    } if exposed else {}
    workspace_name = (
        await db.execute(select(Workspace.name).where(Workspace.id == fleet.workspace_id))
    ).scalar_one_or_none()

    def _agent(agent_id: uuid.UUID) -> dict:
        a = agents.get(agent_id)
        return {
            "id": str(agent_id),
            "name": a.name if a else "",
            "description": (a.description or "") if a else "",
        }

    return {
        "id": str(fleet.id),
        "name": fleet.name,
        "slug": fleet.slug,
        "description": fleet.description or "",
        "skills": list(fleet.skills or []),
        "entry_agent": _agent(fleet.orchestrator_agent_id) if fleet.orchestrator_agent_id else None,
        "exposed_agents": [_agent(a) for a in exposed],
        "accepted_handover_types": list(fleet.accepted_handover_types or FLEET_HANDOVER_TYPES),
        "workspace_name": workspace_name or "",
    }
