# SPDX-License-Identifier: Apache-2.0

"""Fleet model: a named, owned group of agents with a hierarchy.

A **Fleet** is the organisation of agents that work together: an orchestrator
and its desks. It is not a coordination container. The coordination container
already exists, the :class:`~app.models.workspace.Workspace` (work items,
shared state, worksets, knowledge bases, skills, instructions), so every fleet
is bound to exactly ONE workspace (``workspace_id`` NOT NULL) and its
coordination IS that workspace: no fleet scope, no key namespace, no
``fleet_id`` on work items, shared state or runs. A workspace may host several
fleets; a fleet never spans two. ``RESTRICT`` on the foreign key: deleting a
workspace that still hosts a fleet is refused with a reason
(``services.workspaces.remove_workspace``), deleting a fleet never deletes its
workspace.

## Membership: one home fleet, many consumers

``fleet_members`` holds two kinds of row (``role``):

* ``member``: the agent's HOME fleet. An agent has at most one, enforced by
  the partial unique index ``uq_fleet_members_home_agent``; the home fleet
  carries the hierarchy (``parent_member_id``), the run defaults
  (``Fleet.budget_config``, ``Fleet.max_iterations``) and the cost
  attribution (``services.fleet_runs``). An agent in no fleet is in the
  playground.
* ``connected``: an agent, or a whole fleet (``target_fleet_id``), that this
  fleet calls without owning it, with a ``collaboration_instruction`` (when to
  call it) and a ``context_inclusion`` (``none`` | ``summary`` | ``full``). A
  connected agent is not moved, not re-owned, not billed to the consumer. A
  connected fleet is resolved to its entry agent (the orchestrator) at run
  time, so the target may change its orchestrator without breaking callers.

Membership is a row, never an agent edit: no config version, no evaluation
trigger, the agent copy manifests untouched.

## Fleet to fleet

Every fleet publishes a Fleet Card (``services.fleets.build_fleet_card``) and
EXPOSES some of its home members (``is_exposed``; the orchestrator by default).
A handover configured from a member of one fleet to a non-exposed member of
another is refused when it is written; one that predates the rule, or that an
exposure change made invalid, is kept and flagged, never broken.

## Visibility

The full three-tier column set (:class:`VisibilityColumns`), and ``Fleet`` is
in the ``CompanyManagedEntity`` union, like ``Workspace``. Every column with a
DDL default in the migration carries the same ``server_default`` here (see the
note on ``Workspace``): ``tests/test_workspaces_postgres.py`` compares both
install paths.
"""

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Index, Integer, String, Text, func, text
from sqlalchemy.dialects.postgresql import JSON, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.visibility import VisibilityColumns

#: Lucide icon used when the creator picks none.
FLEET_DEFAULT_ICON = "Network"

#: ``fleet_members.role`` values.
FLEET_ROLE_MEMBER = "member"
FLEET_ROLE_CONNECTED = "connected"
FLEET_MEMBER_ROLES: tuple[str, ...] = (FLEET_ROLE_MEMBER, FLEET_ROLE_CONNECTED)

#: ``fleet_members.context_inclusion`` values: how much of the calling
#: conversation a connected agent receives. ``none`` is the default, the least
#: it can be given.
FLEET_CONTEXT_INCLUSIONS: tuple[str, ...] = ("none", "summary", "full")

#: The handover types a fleet may accept from another fleet, as the agents'
#: own ``handover_types`` name them.
FLEET_HANDOVER_TYPES: tuple[str, ...] = ("consult", "delegate", "transfer")


def _now() -> datetime:
    return datetime.now(UTC)


class Fleet(VisibilityColumns, Base):
    """A named, owned group of agents bound to one workspace."""

    __tablename__ = "fleets"
    __table_args__ = (
        Index("ix_fleets_user_id", "user_id"),
        Index("ix_fleets_workspace_id", "workspace_id"),
        Index("ix_fleets_orchestrator_agent_id", "orchestrator_agent_id"),
        Index("ix_fleets_is_company_managed", "is_company_managed"),
        CheckConstraint(
            "default_context_inclusion IN ('none', 'summary', 'full')",
            name="ck_fleets_default_context_inclusion",
        ),
        # The slug is the human handle, unique per owner like a workspace's;
        # ownerless rows are outside the predicate, said rather than implied.
        Index(
            "uq_fleets_slug_per_user",
            "user_id",
            "slug",
            unique=True,
            postgresql_where=text("user_id IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    #: Owner. NULL only on an ownerless, company-seeded row.
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=True
    )
    #: The fleet's home workspace: its coordination (see the module docstring).
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workspaces.id", ondelete="RESTRICT"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    #: Derived from the name on create, then stable, like a workspace's.
    slug: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    icon: Mapped[str] = mapped_column(
        String(50), nullable=False, default=FLEET_DEFAULT_ICON, server_default=FLEET_DEFAULT_ICON
    )
    color: Mapped[str] = mapped_column(String(30), nullable=False, default="", server_default="")
    #: The entry agent: the root of the hierarchy and of the Fleet Card. Always
    #: a home member while set; SET NULL when the agent is deleted.
    orchestrator_agent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agents.id", ondelete="SET NULL"), nullable=True
    )
    #: What the fleet does, as the card publishes it: a list of
    #: ``{"name", "description", "tags"}``. NULL is no skill declared.
    skills: Mapped[list | None] = mapped_column(JSON(none_as_null=True), nullable=True, default=None)
    #: The handover types the fleet accepts from other fleets, a subset of
    #: :data:`FLEET_HANDOVER_TYPES`. NULL is all of them.
    accepted_handover_types: Mapped[list | None] = mapped_column(JSON(none_as_null=True), nullable=True, default=None)
    is_archived: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    #: Seeded by the platform from ``BUILTIN_FLEETS`` (``app.agents.fleet_seed``):
    #: company-managed, upserted by slug on every boot, read-only through the
    #: API and the tools except :data:`app.services.fleets.BUILTIN_FLEET_WRITABLE_FIELDS`.
    is_builtin: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    #: Run budget default of the HOME members, the partial override
    #: shape of the agent Budget tab (``budget_policy.BudgetOverrides``). An
    #: agent's own ``budget_config`` wins per dimension; NULL = no default.
    #: Written under the agents API's authority rule (platform ceiling).
    budget_config: Mapped[dict | None] = mapped_column(JSON(none_as_null=True), nullable=True, default=None)
    #: Iteration cap default of the HOME members; an agent's own
    #: ``max_iterations`` wins, the platform ceiling clamps. NULL = none.
    max_iterations: Mapped[int | None] = mapped_column(Integer, nullable=True, default=None)
    #: Optional run budget of the fleet (#1526, ``services/fleet_run_budget``):
    #: ``{runs_per_hour, tokens_per_hour}``, each optional, over the home
    #: members' runs of the last rolling hour. Over it, new runs are held in the
    #: queue (never failed) and the owner is told once an hour. NULL = none.
    run_budget: Mapped[dict[str, int] | None] = mapped_column(JSON(none_as_null=True), nullable=True, default=None)
    #: Member defaults (dev-docs/ctem-fleets.md section 10.3): what a connected
    #: row inherits while its own field is NULL. Read through
    #: ``app.services.fleets.effective_member_settings``, never raw.
    default_context_inclusion: Mapped[str] = mapped_column(
        String(10), nullable=False, default="none", server_default="none"
    )
    default_collaboration_instruction: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    #: The organizations a member reasons about by default (section 10.3):
    #: ``{"mode": "primary" | "monitored" | "list", "organization_ids": [...]}``.
    #: Read through ``effective_member_settings``, never raw.
    organization_scope: Mapped[dict] = mapped_column(
        JSON, nullable=False, default=lambda: {"mode": "primary"}, server_default=text('\'{"mode": "primary"}\'')
    )
    #: Provenance of a fork (``app.services.fleet_fork``): ``{fleet_id, slug,
    #: name, is_builtin, fingerprint, forked_at}`` of the source at fork time.
    #: ``fingerprint`` hashes the source's structure, so a later change of the
    #: source is detectable. NULL on a fleet that was not forked.
    forked_from: Mapped[dict | None] = mapped_column(JSON(none_as_null=True), nullable=True, default=None)
    #: The fleet's documentation (``app.services.fleet_docs``): ``{sections:
    #: {key: markdown}, version, updated_at, updated_by, fingerprint}``. The
    #: seed writes it on a built-in fleet (read only there), a fork copies it,
    #: the owner edits it on a custom fleet. NULL until one is written.
    documentation: Mapped[dict | None] = mapped_column(JSON(none_as_null=True), nullable=True, default=None)
    #: How many of the fleet's gated actions wait for a person (#9769), one of
    #: ``app.services.fleet_autonomy.AUTONOMY_LEVELS``. ``full_gates`` (every
    #: gated action waits) is the default of every custom fleet; a built-in
    #: fleet ships at ``low`` (``fleet_seed.SEEDED_FLEET_AUTONOMY_LEVEL``, #10005).
    #: Read through ``fleet_autonomy.effective_level``, never raw.
    autonomy_level: Mapped[str] = mapped_column(
        String(20), nullable=False, default="full_gates", server_default="full_gates"
    )
    #: The fleet's approval gate catalog (#10000): the gated actions the
    #: autonomy level acts on, each ``app.services.fleet_docs.DocGate`` (key,
    #: action, module, approver, impact, tools). Configuration, not
    #: documentation: the seed writes it on a built-in fleet, a fork copies it,
    #: a documentation regenerate never writes it. NULL = none stored yet, the
    #: gates of the documentation are read instead
    #: (``fleet_autonomy.fleet_gates``).
    approval_gates: Mapped[list | None] = mapped_column(JSON(none_as_null=True), nullable=True, default=None)
    #: Per-action overrides of the level (#10153, slice B2 of #10000):
    #: ``{gate key: "wait" | "run"}`` (``fleet_autonomy.APPROVAL_PINS``). Not
    #: NULL also means the level acts on the computed catalog of a custom fleet
    #: (``fleet_gate_proposals.fleet_effective_gates``): ``{}`` on every new
    #: custom fleet; NULL keeps a legacy custom fleet on its declared gates
    #: until a person applies a level through ``PUT /fleets/{id}/autonomy``.
    approval_pins: Mapped[dict | None] = mapped_column(JSON(none_as_null=True), nullable=True, default=None)
    #: Gates a person added to a built-in fleet (#916, CTEM-34): the customer
    #: layer of its catalog, same shape as ``approval_gates``, read after the
    #: seeded gates (``fleet_autonomy.effective_gates``). The seed never writes
    #: it, so an added gate survives every upgrade. NULL = none added.
    customer_gates: Mapped[list[dict[str, Any]] | None] = mapped_column(
        JSON(none_as_null=True), nullable=True, default=None
    )
    #: Whether the platform also pauses the tool calls a waiting gate maps to
    #: (#1467). Off by default and never written by the seed: by design XTM One
    #: enables no programmatic tool-level gate, a fleet's gates are instructions
    #: to its members (a decision item with its ask card). An administrator
    #: opts a fleet in (``PUT /fleets/{id}/autonomy``, ``enforce_at_tool_level``).
    enforce_gates_at_tool_level: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    #: Where the fleet's instructions (persona, guardrails, skills) put it on
    #: the four-level scale, between-step positions allowed (#10168, B2-I of
    #: #10000; ``fleet_autonomy.INSTRUCTIONS_POSITIONS``). Never a platform
    #: gate: display, and a statement to the members once announced.
    instructions_position: Mapped[str | None] = mapped_column(String(20), nullable=True, default=None)
    #: Whether the members are told the position in their run prompt: true
    #: once a person moved it; NULL/false = display only.
    instructions_position_announced: Mapped[bool | None] = mapped_column(Boolean, nullable=True, default=None)
    #: The documented ``manual`` prerequisites a person confirmed (#10469 S2):
    #: ``{manual key: {by, name, at, label}}`` (``fleet_about.manual_key``).
    #: Deployment state, not documentation: a regenerate never writes it. NULL = none.
    prerequisite_confirmations: Mapped[dict | None] = mapped_column(
        JSON(none_as_null=True), nullable=True, default=None
    )
    #: The health findings a fleet manager dismissed with a reason (#12014):
    #: ``[{id, kind, scope, summary, reason, by, by_name, at, until}]``
    #: (``app.services.fleet_health_dismissals``). Operations state, not
    #: documentation: a regenerate never writes it, a fork never copies it. NULL = none.
    health_dismissals: Mapped[list | None] = mapped_column(JSON(none_as_null=True), nullable=True, default=None)
    #: The fleet's weekly health check (``app.services.fleets.fleet_health_check_mode``):
    #: NULL = the platform-seeded check on the orchestrator (the default);
    #: ``{mode: "off", by, at}`` = opted out, the seeded check is never re-created;
    #: ``{mode: "own", assignment_id, by, at}`` = one of the fleet's own
    #: assignments is its health check. A deletion of the seeded check records
    #: ``off``. Operations state: a regenerate never writes it, a fork never copies it.
    health_check: Mapped[dict | None] = mapped_column(JSON(none_as_null=True), nullable=True, default=None)
    #: Platform Health enrolment of the fleet (#101 S2, dev-docs/platform-health.md
    #: 1.2): its home members are checked while they stay in it. Off by default;
    #: operations state, a regenerate never writes it, a fork never copies it.
    health_enrolled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    #: Hide the activity of the home members and of the fleet's workspace
    #: (runs, traces, work items, shared state) from non-administrators
    #: (#11624, ``services/activity_visibility``). Off by default, built-in
    #: fleets included; an administrator sets it; a fork never copies it.
    hide_activity_from_non_admins: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    #: The CTEM cases this fleet runs (#1966, ``services/ctem_case_routing.py``):
    #: ``{"paths": null | [trigger path, ...], "key": "ctem:..."}``, ``paths``
    #: null = every path, ``key`` the prefix of its case work items
    #: (``<key>:case:<case id>``). Taken only while one of its home members'
    #: CTEM case automations is on. The seed writes it on a built-in fleet
    #: (``case_ownership`` of ``BUILTIN_FLEETS``), a fork copies it, an
    #: administrator sets it on any other fleet. NULL = runs no case.
    case_ownership: Mapped[dict | None] = mapped_column(JSON(none_as_null=True), nullable=True, default=None)
    #: Since when the fleet's value (ROI) is measured (#2580): a factory reset
    #: (``services/fleet_reset.py``) stamps it, and every value read of the fleet
    #: starts at max(window start, this time), records and AI cost alike
    #: (``services/fleet_value_marks.py``). Nothing is deleted. NULL = never reset,
    #: the whole window counts. Operations state: a fork never copies it.
    value_measured_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, default=None)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now, onupdate=_now, server_default=func.now()
    )


class FleetMember(Base):
    """An agent's place in a fleet: its home (``member``) or a ``connected`` link.

    A ``member`` row names an agent and may name its parent row; a
    ``connected`` row names either an agent or a whole fleet
    (``target_fleet_id``), never both, never this fleet, and has no parent.
    Both foreign keys to the outside CASCADE: deleting an agent or a fleet
    removes the rows that name it and nothing else.
    """

    __tablename__ = "fleet_members"
    __table_args__ = (
        Index("ix_fleet_members_fleet_id", "fleet_id"),
        Index("ix_fleet_members_agent_id", "agent_id"),
        Index("ix_fleet_members_target_fleet_id", "target_fleet_id"),
        Index("ix_fleet_members_parent_member_id", "parent_member_id"),
        # One home fleet per agent (decision C): the database is the
        # authority, two concurrent adds on two replicas cannot both win.
        Index(
            "uq_fleet_members_home_agent",
            "agent_id",
            unique=True,
            postgresql_where=text("role = 'member'"),
        ),
        # An agent appears once per fleet, home or connected.
        Index(
            "uq_fleet_members_fleet_agent",
            "fleet_id",
            "agent_id",
            unique=True,
            postgresql_where=text("agent_id IS NOT NULL"),
        ),
        Index(
            "uq_fleet_members_fleet_target",
            "fleet_id",
            "target_fleet_id",
            unique=True,
            postgresql_where=text("target_fleet_id IS NOT NULL"),
        ),
        CheckConstraint(
            "(role = 'member' AND agent_id IS NOT NULL AND target_fleet_id IS NULL)"
            " OR (role = 'connected' AND parent_member_id IS NULL"
            " AND (agent_id IS NULL) <> (target_fleet_id IS NULL))",
            name="ck_fleet_members_role_shape",
        ),
        CheckConstraint(
            "target_fleet_id IS NULL OR target_fleet_id <> fleet_id",
            name="ck_fleet_members_not_self",
        ),
        CheckConstraint(
            # NULL (inherits the fleet default) passes a CHECK by SQL semantics.
            "context_inclusion IN ('none', 'summary', 'full')",
            name="ck_fleet_members_context_inclusion",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    fleet_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("fleets.id", ondelete="CASCADE"), nullable=False
    )
    agent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agents.id", ondelete="CASCADE"), nullable=True
    )
    #: A connected FLEET, resolved to its entry agent at run time.
    target_fleet_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("fleets.id", ondelete="CASCADE"), nullable=True
    )
    role: Mapped[str] = mapped_column(
        String(20), nullable=False, default=FLEET_ROLE_MEMBER, server_default=FLEET_ROLE_MEMBER
    )
    #: The member this one reports to; NULL reports to the orchestrator (or is
    #: the orchestrator). SET NULL: removing a parent lifts its reports one level.
    parent_member_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("fleet_members.id", ondelete="SET NULL"), nullable=True
    )
    #: Whether other fleets may hand over to this home member (the Fleet Card's
    #: exposed list). Meaningless on a connected row.
    is_exposed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    #: What a home member does in the fleet ("Triage", "Release manager"), as
    #: the fleet page shows it beside its name. A label only: it never
    #: changes the agent, its prompt or its handovers. Empty on a connected row.
    role_label: Mapped[str] = mapped_column(String(100), nullable=False, default="", server_default="")
    #: When the fleet calls a connected agent or fleet (Bedrock's
    #: collaboration instruction, Copilot Studio's connected-agent description).
    #: NULL inherits the fleet's ``default_collaboration_instruction``.
    collaboration_instruction: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)
    #: NULL inherits the fleet's ``default_context_inclusion``.
    context_inclusion: Mapped[str | None] = mapped_column(String(10), nullable=True, default=None)
    #: The organizations this member reasons about, on a home or a connected
    #: row (section 10.3). NULL inherits the fleet's ``organization_scope``.
    organization_scope: Mapped[dict | None] = mapped_column(JSON(none_as_null=True), nullable=True, default=None)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now, onupdate=_now, server_default=func.now()
    )


class FleetMemberPreset(Base):
    """A company-wide named set of member settings (dev-docs/ctem-fleets.md 10.6).

    Managed by administrators (decision R-D3 A: no personal preset). ``values``
    holds any of the three member fields (``context_inclusion``,
    ``collaboration_instruction``, ``organization_scope``) in the shape the
    bulk endpoint takes. Applying a preset writes those values to the fleet
    defaults or to the selected rows; no link is kept, so editing or deleting
    a preset changes no fleet. The three built-ins live in code
    (``app.services.fleet_member_presets.BUILTIN_PRESETS``), never in this table.
    """

    __tablename__ = "fleet_member_presets"
    __table_args__ = (Index("uq_fleet_member_presets_name", "name", unique=True),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    values: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    #: The administrator who created it; SET NULL when that account is deleted.
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now, onupdate=_now, server_default=func.now()
    )
