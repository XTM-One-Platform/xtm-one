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

"""Pydantic schemas for the Fleet API.

A Fleet is a named, owned group of agents bound to one workspace; the model
and its rules are documented in ``app.models.fleet`` and
``app.services.fleets``. ``slug`` is response-only, like a workspace's.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from app.api.v1.schemas.agent import AgentBudgetConfig

ContextInclusion = Literal["none", "summary", "full"]
FleetHandoverType = Literal["consult", "delegate", "transfer"]


class FleetSkill(BaseModel):
    """One thing the fleet does, as its card publishes it."""

    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=2000)
    tags: list[str] = Field(default_factory=list, max_length=20)


class FleetRunBudget(BaseModel):
    """The optional run budget of a fleet (#1526): over the home members' runs of the last rolling hour, new
    runs are held in the queue (never failed) until usage is back under it. A bound left out is unbounded."""

    runs_per_hour: int | None = Field(default=None, ge=1, le=100_000)
    tokens_per_hour: int | None = Field(default=None, ge=1, le=10_000_000_000)


class FleetCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str = ""
    icon: str = Field(default="", max_length=50)
    color: str = Field(default="", max_length=30)
    #: The workspace the fleet coordinates in. Omitted: a workspace named
    #: after the fleet is created with it.
    workspace_id: str | None = None
    skills: list[FleetSkill] | None = Field(default=None, max_length=50)
    accepted_handover_types: list[FleetHandoverType] | None = None
    is_company_managed: bool = False
    shared_group_ids: list[str] | None = None
    shared_group_permissions: dict[str, str] | None = None
    #: Run budget default of the home members; their own wins.
    budget_config: AgentBudgetConfig | None = None
    #: Iteration cap default of the home members; their own wins.
    max_iterations: int | None = Field(default=None, ge=1)
    #: Optional run budget of the fleet (#1526); omitted = none.
    run_budget: FleetRunBudget | None = None


class OrganizationScope(BaseModel):
    """The organizations a member reasons about (dev-docs/ctem-fleets.md 10.3).

    ``primary``: the platform's own organization; ``monitored``: it and every
    tracked organization whose monitoring is on; ``list``: it and the tracked
    organizations named in ``organization_ids``.
    """

    mode: Literal["primary", "monitored", "list"] = "primary"
    organization_ids: list[str] | None = Field(default=None, max_length=50)


class FleetMemberDefaults(BaseModel):
    """What a member row inherits while its own field is unset."""

    context_inclusion: ContextInclusion | None = None
    collaboration_instruction: str | None = Field(default=None, max_length=4000)
    #: Every member, home or connected.
    organization_scope: OrganizationScope | None = None


class FleetUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = None
    icon: str | None = Field(default=None, max_length=50)
    color: str | None = Field(default=None, max_length=30)
    workspace_id: str | None = None
    #: A home member's agent id; ``null`` clears the orchestrator.
    orchestrator_agent_id: str | None = None
    skills: list[FleetSkill] | None = Field(default=None, max_length=50)
    accepted_handover_types: list[FleetHandoverType] | None = None
    is_archived: bool | None = None
    is_company_managed: bool | None = None
    user_id: str | None = None  # owner transfer (admin only)
    shared_group_ids: list[str] | None = None
    shared_group_permissions: dict[str, str] | None = None
    shared_user_ids: list[str] | None = None
    shared_user_permissions: dict[str, str] | None = None
    access_restricted: bool | None = None
    #: ``null`` clears the fleet's budget default.
    budget_config: AgentBudgetConfig | None = None
    #: ``null`` clears the fleet's iteration default.
    max_iterations: int | None = Field(default=None, ge=1)
    #: ``null`` clears the fleet's run budget (#1526).
    run_budget: FleetRunBudget | None = None
    #: The fields given replace the fleet's member defaults; ``null`` in one
    #: puts it back to the column default.
    member_defaults: FleetMemberDefaults | None = None
    #: How many gated actions wait for a person (#9769); raising it is admin-only.
    autonomy_level: Literal["full_gates", "medium", "low", "autonomous"] | None = None
    #: Admin only (#11624): hide the members' and the workspace's activity from non-admins.
    hide_activity_from_non_admins: bool | None = None
    #: The weekly health check: ``default`` (the seeded one), ``off``, or the id
    #: of an assignment of a home member that is the fleet's own check.
    health_check: str | None = Field(default=None, max_length=64)


class FleetMemberCreate(BaseModel):
    """A home member (``agent_id``), or a connected agent or fleet."""

    role: Literal["member", "connected"] = "member"
    agent_id: str | None = None
    #: ``connected`` only: a whole fleet, resolved to its entry agent at run time.
    target_fleet_id: str | None = None
    #: ``member`` only.
    parent_member_id: str | None = None
    is_exposed: bool = False
    #: ``member`` only: what the member does in the fleet, a label.
    role_label: str = Field(default="", max_length=100)
    #: ``connected`` only; omitted inherits the fleet default.
    collaboration_instruction: str | None = Field(default=None, max_length=4000)
    context_inclusion: ContextInclusion | None = None


class FleetMemberUpdate(BaseModel):
    parent_member_id: str | None = None
    is_exposed: bool | None = None
    role_label: str | None = Field(default=None, max_length=100)
    #: ``null`` resets the field: the row inherits the fleet default.
    collaboration_instruction: str | None = Field(default=None, max_length=4000)
    context_inclusion: ContextInclusion | None = None
    #: Any member; ``null`` inherits the fleet's scope again.
    organization_scope: OrganizationScope | None = None


class FleetMemberPresetValues(BaseModel):
    """What a preset sets: any of the member settings, at least one (checked by the service)."""

    context_inclusion: ContextInclusion | None = None
    collaboration_instruction: str | None = Field(default=None, max_length=4000)
    organization_scope: OrganizationScope | None = None


class FleetMemberPresetCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    values: FleetMemberPresetValues


class FleetMemberPresetUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    #: Replaces the preset's values whole.
    values: FleetMemberPresetValues | None = None


class FleetMemberPresetResponse(BaseModel):
    #: ``builtin:<key>`` for a built-in preset, else the stored row's id.
    id: str
    name: str
    values: dict[str, str | dict]
    builtin: bool


class FleetMembersBulkUpdate(BaseModel):
    """Set the member settings of several connected rows at once, all or nothing."""

    member_ids: list[str] = Field(min_length=1, max_length=500)
    #: Any of the member settings; ``null`` resets that field to the fleet default.
    set: FleetMemberDefaults


class FleetMemberResponse(BaseModel):
    id: str
    fleet_id: str
    role: str
    agent_id: str | None
    #: The agent's name, or the connected fleet's; empty when the caller
    #: cannot see it (drawn as a placeholder).
    name: str = ""
    target_fleet_id: str | None
    parent_member_id: str | None
    is_orchestrator: bool = False
    #: Reachable from other fleets: its own flag, or the orchestrator (always exposed).
    is_exposed: bool
    #: ``member`` only: the stored flag alone, without the orchestrator's implicit
    #: exposure; what an exposure edit starts from.
    exposed_explicitly: bool = False
    role_label: str = ""
    #: Effective values: the row's own, else the fleet default.
    collaboration_instruction: str
    context_inclusion: str
    #: Effective organization scope: ``{mode, organization_ids?}``.
    organization_scope: dict = Field(default_factory=lambda: {"mode": "primary"})
    #: The settings the row sets itself; the others inherit the fleet default.
    overridden: list[str] = Field(default_factory=list)
    created_at: str


class FleetSlotIntegration(BaseModel):
    """An integration as a capability slot shows it (#914 slice 2)."""

    integration_id: str
    provider: str
    #: Empty when the reader cannot see the bound integration.
    name: str = ""


class FleetSlotBlockedIntegration(FleetSlotIntegration):
    """An integration of the capability the member cannot take, and why (#1373).

    ``not_company_managed``: connected, but the member agent is company-managed
    and takes company-managed integrations only. ``not_connected``: visible to
    the reader but not connected yet (a sign-in to finish).
    """

    reason: Literal["not_company_managed", "not_connected"]


class FleetSlotBuiltin(BaseModel):
    """A capability's built-in option (#1201): served by XTM One, never stored as a binding."""

    key: str
    label: str
    #: Always active: an external integration adds to it and it cannot be removed.
    always_on: bool = True


class FleetCapabilitySlot(BaseModel):
    """One capability a member's role can use, and how it is filled."""

    capability: str
    label: str
    #: ``filled`` (bound), ``empty`` (a connected one can be bound) or
    #: ``unavailable`` (none connected).
    state: Literal["filled", "empty", "unavailable"]
    bound: list[FleetSlotIntegration] = Field(default_factory=list)
    candidates: list[FleetSlotIntegration] = Field(default_factory=list)
    #: The built-in option, when the capability has one: it fills the slot by itself.
    builtin: FleetSlotBuiltin | None = None
    #: Integrations of the capability this member cannot take, with the reason (#1373).
    blocked: list[FleetSlotBlockedIntegration] = Field(default_factory=list)


class FleetMemberSlots(BaseModel):
    """The capability slots of one home member."""

    member_id: str
    agent_id: str
    name: str = ""
    role_label: str = ""
    #: Whether the reader may bind or unbind on this member's agent.
    can_edit: bool = False
    slots: list[FleetCapabilitySlot] = Field(default_factory=list)


class FleetCapabilityTypeMember(BaseModel):
    """One member using a capability type, in the expanded type row (#1201)."""

    member_id: str
    agent_id: str
    name: str = ""
    role_label: str = ""
    can_edit: bool = False
    bound: list[FleetSlotIntegration] = Field(default_factory=list)
    #: Its bound set differs from the fleet choice: an override ("Custom").
    custom: bool = False


class FleetCapabilityType(BaseModel):
    """A capability across the fleet's members: one row of the type view (#1201)."""

    capability: str
    label: str
    #: ``connected`` (every member filled), ``partial`` (some), ``not_bound``
    #: (none, a connected one can be bound), ``not_usable`` (none can be bound,
    #: one of the type exists but is ``blocked``, #1373) or ``none_connected``.
    state: Literal["connected", "partial", "not_bound", "not_usable", "none_connected"]
    #: The fleet choice: the bound set most members share.
    choice: list[FleetSlotIntegration] = Field(default_factory=list)
    candidates: list[FleetSlotIntegration] = Field(default_factory=list)
    members: list[FleetCapabilityTypeMember] = Field(default_factory=list)
    builtin: FleetSlotBuiltin | None = None
    #: What no member can take, with the reason (#1373): the row says it instead of "Connect one".
    blocked: list[FleetSlotBlockedIntegration] = Field(default_factory=list)


class FleetCapabilitySlotsResponse(BaseModel):
    fleet_id: str
    members: list[FleetMemberSlots] = Field(default_factory=list)
    #: The same slots grouped by capability (#1201): one row per type.
    types: list[FleetCapabilityType] = Field(default_factory=list)


class FleetCapabilityTypeSet(BaseModel):
    """The fleet choice of a capability, written on every member slot of that type (#1201)."""

    #: Exactly the integrations bound; empty leaves the built-in option alone (when any).
    integration_ids: list[str] = Field(default_factory=list, max_length=20)
    #: Also write the members whose slot is custom (an override); otherwise they keep it.
    override_custom: bool = False


class FleetSlotSet(BaseModel):
    """Exactly the integrations bound on one member's slot: an override or a reset (#1201)."""

    integration_ids: list[str] = Field(default_factory=list, max_length=20)


class FleetSlotBind(BaseModel):
    """Bind one connected integration to a member's slot."""

    integration_id: str = Field(min_length=1, max_length=64)


class FleetMemberMove(BaseModel):
    """Move a home member to another fleet in one transaction."""

    target_fleet_id: str = Field(min_length=1, max_length=64)


class FleetExposureUpdate(BaseModel):
    """The complete exposure list (replace); the orchestrator is always exposed."""

    agent_ids: list[str] = Field(max_length=500)


class FleetBoundaryViolationResponse(BaseModel):
    """A handover into the fleet that its exposure does not allow, kept as is."""

    source_agent_id: str
    target_agent_id: str


class FleetCaseOwnershipRequest(BaseModel):
    """``PUT /fleets/{id}/case-ownership`` (#1966): the CTEM cases a fleet runs.

    ``paths`` null = every trigger path, else a non-empty list of trigger
    paths; ``key`` the prefix of its case work items (``<key>:case:<case id>``),
    default ``ctem:fleet-<id>``. ``clear`` true removes the declaration.
    """

    paths: list[str] | None = None
    key: str | None = Field(default=None, max_length=60)
    clear: bool = False


class FleetResponse(BaseModel):
    id: str
    user_id: str | None
    owner_name: str | None = None
    name: str
    slug: str
    description: str
    icon: str
    color: str
    workspace_id: str
    workspace_name: str = ""
    orchestrator_agent_id: str | None
    skills: list[dict] | None
    accepted_handover_types: list[str] | None
    is_archived: bool
    is_company_managed: bool
    #: Runs, traces, work items and state hidden from non-admins (#11624).
    hide_activity_from_non_admins: bool = False
    #: Seeded by the platform (``BUILTIN_FLEETS``): read-only except archiving
    #: and the run defaults (``services.fleets.BUILTIN_FLEET_WRITABLE_FIELDS``).
    is_builtin: bool = False
    shared_group_ids: list[str] | None
    shared_group_permissions: dict[str, str] | None
    shared_user_ids: list[str] | None = None
    shared_user_permissions: dict[str, str] | None = None
    access_restricted: bool = False
    #: Whether the REQUESTING user may change it; always false on the built-in
    #: Platform Health fleet, which takes no write at all (#101 S1b).
    can_manage: bool
    #: Why NOBODY may change it, administrators included: set on the built-in
    #: Platform Health fleet (``HEALTH_READ_ONLY_REFUSAL``), ``None`` otherwise.
    read_only_reason: str | None = None
    member_count: int = 0
    connected_count: int = 0
    exposed_agent_ids: list[str] = Field(default_factory=list)
    #: Run defaults of the home members, ``null`` = none.
    budget_config: dict | None = None
    max_iterations: int | None = None
    #: The fleet's run budget (#1526), ``null`` = none.
    run_budget: dict[str, int] | None = None
    #: What a member row inherits while its own field is unset: the two
    #: connected settings (strings) and ``organization_scope`` (an object).
    member_defaults: dict[str, str | dict] = Field(default_factory=dict)
    #: How many gated actions wait for a person (#9769): ``full_gates`` |
    #: ``medium`` | ``low`` | ``autonomous``.
    autonomy_level: str = "full_gates"
    #: How many actions that level acts on (#10457): the length of the About
    #: page's ``autonomy_catalog``; ``0`` = the level gates nothing here.
    gated_action_count: int = 0
    #: Where the fleet's instructions put it on the scale (#10168); ``null`` = unset.
    instructions_position: str | None = None
    #: The weekly health check: ``null`` = the seeded one; ``{mode: off | own,
    #: assignment_id?, by, at}`` = the fleet opted out of it.
    health_check: dict | None = None
    #: Provenance of a fork (#9174): the source's id, slug, name, built-in
    #: flag, fingerprint, member mode and fork time; ``null`` = not a fork.
    forked_from: dict | None = None
    #: The CTEM cases it runs (#1966): ``{paths: null | [trigger path], key}``; ``null`` = none.
    case_ownership: dict | None = None
    #: The opt-in state of its automations (#9977 S2, the Fleets list card):
    #: the same counts as ``GET /fleets/{id}/health``.
    automations: FleetAutomationsResponse | None = None
    #: The newest run of a home member the caller may see; ``null`` = never run.
    last_run_at: str | None = None
    #: The Platform Health fleet only (#974): what starts it and what it covers, from
    #: Settings > Platform Health; ``null`` on every other fleet.
    health_starts: FleetHealthStartsResponse | None = None
    created_at: str
    updated_at: str


class FleetAgentCostEntry(BaseModel):
    agent_id: str | None
    agent_name: str
    trace_count: int
    total_cost: float
    total_tokens: int


class FleetCostResponse(BaseModel):
    """The cost of the runs attributed to a fleet over a window.

    A connected member's runs count for its own home fleet, never for the
    fleet that called it.
    """

    fleet_id: str
    days: int
    since: str
    total_cost: float
    total_tokens: int
    trace_count: int
    agents: list[FleetAgentCostEntry]


class FleetOrganizationRunEstimate(BaseModel):
    """The expected runs per month one organization adds to a fleet (section 10.3 of ctem-fleets.md)."""

    organization_id: str
    name: str
    role: str
    monitoring: str
    daily_run_cap: int | None
    #: One entry per enabled assignment of a member whose trigger serves the organization.
    assignments: list[dict[str, object]]
    expected_runs_per_month: int
    #: Why no figure could be read (no OpenCTI connection, the primary's own triggers), else null.
    note: str | None = None


class FleetSetupStep(BaseModel):
    """One step of the CTEM loop guided setup (#1087 slice 2)."""

    key: Literal["organization", "modules", "ecosystem", "gates", "preview", "enable"]
    #: ``done`` / ``skipped`` as recorded for the current setup version, else ``ready`` (already in
    #: place on the platform) or ``todo``.
    state: Literal["done", "skipped", "ready", "todo"]
    #: What the step shows, read live (counts, connected modules, slot states, level, opt-in).
    facts: dict[str, Any] = Field(default_factory=dict)


class FleetSetupStatusResponse(BaseModel):
    """Where the CTEM loop guided setup stands; read only, administrators."""

    version: str
    #: The CTEM loop fleet, null when it is not seeded on this instance.
    fleet_id: str | None = None
    steps: list[FleetSetupStep] = Field(default_factory=list)
    #: The step the flow opens on; null when every step is done or skipped.
    resume_on: str | None = None


class FleetRunEstimateResponse(BaseModel):
    """Expected runs per month of a fleet's members, per organization of a scope; nothing saved."""

    fleet_id: str
    window_days: int
    organizations: list[FleetOrganizationRunEstimate]
    expected_runs_per_month: int


class FleetGraphNode(BaseModel):
    """A node of the fleet graph: a home member, a connected agent or fleet,
    or an agent outside the fleet a handover reaches or comes from."""

    id: str
    kind: Literal["member", "connected", "external", "fleet"]
    #: Empty when the caller cannot see it (a placeholder).
    name: str = ""
    icon: str | None = None
    agent_id: str | None = None
    #: The node's home fleet (``None``: the playground); a fleet node's own id.
    fleet_id: str | None = None
    fleet_name: str = ""
    member_id: str | None = None
    parent_agent_id: str | None = None
    is_orchestrator: bool = False
    is_exposed: bool = False
    enabled: bool = True
    visible: bool = True
    collaboration_instruction: str = ""
    context_inclusion: str | None = None
    #: A connected fleet's entry agent (its orchestrator).
    entry_agent_id: str | None = None


class FleetGraphEdge(BaseModel):
    """A configured handover. ``scope`` places it against the fleet boundary;
    a crossing edge with ``governed`` false is drawn dashed (no connected row
    covers it), ``allowed`` false flags a target its fleet does not expose."""

    source: str
    target: str
    type: str
    scope: Literal["internal", "outbound", "inbound"]
    governed: bool = True
    allowed: bool = True
    source_fleet_id: str | None = None
    target_fleet_id: str | None = None


class FleetGraphResponse(BaseModel):
    fleet_id: str
    orchestrator_agent_id: str | None
    nodes: list[FleetGraphNode]
    edges: list[FleetGraphEdge]


class FleetFlowNode(BaseModel):
    """A node of the fleet data flow (#9829): a trigger source, a home member
    or an output, placed in its lane left to right."""

    id: str
    lane: Literal["source", "agent", "output"]
    #: ``integration``, ``platform_event``, ``schedule``, ``webhook``,
    #: ``work_item``, ``manual``, ``agent`` or ``work_items``.
    kind: str
    #: Empty when the caller cannot see it (a placeholder).
    name: str = ""
    icon: str | None = None
    provider: str | None = None
    integration_id: str | None = None
    agent_id: str | None = None
    #: Event names drawn as chips (integration or platform events).
    events: list[str] = []
    enabled: bool = True
    #: Why it is greyed: ``disabled``, ``agent disabled``, ``not connected``, ``paused``.
    reason: str | None = None
    visible: bool = True


class FleetFlowEdge(BaseModel):
    """``trigger`` (source to member, one per assignment), ``handover``
    (member to member), ``write`` or ``read`` (member to output)."""

    source: str
    target: str
    kind: Literal["trigger", "handover", "write", "read"]
    assignment_id: str | None = None
    assignment_name: str = ""
    events: list[str] = []
    #: A schedule in words with its timezone, a work item prefix.
    detail: str = ""
    #: A schedule trigger's cron and timezone, so the client words it in the reader's language.
    cron: str = ""
    timezone: str = ""
    #: The trigger's filter in one line, as stored.
    filter: str = ""
    #: The activation gate in one line (tool check, activation question).
    gate: str = ""
    type: str | None = None
    enabled: bool = True
    reason: str | None = None


class FleetFlowResponse(BaseModel):
    fleet_id: str
    nodes: list[FleetFlowNode]
    edges: list[FleetFlowEdge]


class FleetHierarchyNode(BaseModel):
    """One home member in the declared tree, depth first."""

    member_id: str
    agent_id: str
    name: str = ""
    parent_agent_id: str | None
    depth: int
    is_orchestrator: bool


class FleetMemberHealthResponse(BaseModel):
    agent_id: str
    name: str = ""
    status: Literal["failing", "disabled", "stale", "idle", "ok"]
    enabled: bool
    last_run_at: str | None
    last_run_status: str | None
    #: ``run_outcome.run_outcome`` of the last run: ``interrupted`` when the
    #: platform ended it (a rollout cut, a restart), never counted as failing.
    last_run_outcome: str | None = None
    runs_in_flight: int
    #: Failed runs among the judged ones of the verdict's window (``run_outcome.member_verdict``).
    failures_24h: int
    #: Ended runs of the window the platform did not interrupt (the tooltip's "of the last N").
    recent_runs: int = 0
    #: Runs of the window the platform interrupted.
    recent_interruptions: int = 0
    open_work_items: int
    stale_work_items: int


class FleetHealthStartsResponse(BaseModel):
    """What starts the Platform Health fleet (#974, ``fleet_views.HealthFleetStarts``):
    its Settings schedules and signals, never its ``manual`` assignments."""

    #: A signal pass is on: an investigation starts on an event.
    events: bool = False
    #: The schedules switched on in Settings > Platform Health.
    schedules: int = 0
    #: The soonest next start of those schedules (ISO 8601); ``null`` when none is on.
    next_run_at: str | None = None
    #: The modules it covers, XTM One modes only (``xtm_one`` then the registered ones).
    modules: list[str] = Field(default_factory=list)
    #: Where an administrator switches and schedules it.
    settings_route: str = ""
    #: Platform Health is on in Settings; off, ``next_run_at`` is ``null`` (nothing starts).
    active: bool = True


class FleetAutomationsResponse(BaseModel):
    """The opt-in state of a fleet's automations (#9566,
    ``app.services.fleet_views.automation_counts``): on a built-in fleet only
    its seeded fleet automations, never the quota-exempt dashboard ones."""

    total: int = 0
    enabled: int = 0
    #: Switched off after being on (a person, a quarantine), not off as seeded.
    paused: int = 0
    #: Of ``paused``, the ones the fleet's "Pause all" switched off: what "Resume" switches back on.
    fleet_paused: int = 0
    #: The names of the counted automations that are not on, sorted (#11996): the home widget's tooltip.
    off_names: list[str] = Field(default_factory=list)


class FleetControlResponse(BaseModel):
    """The outcome of a fleet's "Pause all" or "Resume" (#9769 S2)."""

    fleet_id: str
    #: The automations this call switched off (pause) or back on (resume).
    changed: int
    #: The fleet's automations after the call.
    automations: FleetAutomationsResponse


class FleetEnableRequest(BaseModel):
    """Enable fleet (#9769 S4b): switch on every automation of the fleet that is off.

    Who the seeded fleet automations act as in each module (#2650): ``run_as_by_module``
    maps ``opencti`` / ``openaev`` / ``opencrq`` to a user id, ``run_as_user_id`` is used
    for every module the map does not name. Nothing defaults to the caller: a module
    with no choice refuses the enable (400 naming it; a dry run reports it in
    ``unchosen``). Another user than the caller is an administrator's choice.
    ``dry_run`` answers the plan for the setup dialog without writing.
    ``assignment_ids`` picks some of the automations that are off (partial
    enable, #9173 S5); absent, every one is picked."""

    run_as_user_id: str | None = None
    run_as_by_module: dict[str, str] | None = None
    dry_run: bool = False
    assignment_ids: list[str] | None = Field(default=None, max_length=500)


class FleetEnableItemResponse(BaseModel):
    """One automation of the fleet that is off: Enable fleet switches it on when it is selected."""

    id: str
    name: str
    #: Module providers it acts through (``opencti``, ``openaev``, ``opencrq``).
    providers: list[str]
    #: Of ``providers``, the ones the run-as user has not connected: Enable is refused until none is.
    missing: list[str]
    #: Part of this call (every automation when the request named none).
    selected: bool = True
    #: Runs a day its schedule starts (0 for a trigger that is not a schedule, #1538).
    runs_per_day: float = 0.0
    #: It runs once per event: its daily count is not known ahead.
    event_driven: bool = False


class FleetEnableEstimateResponse(BaseModel):
    """The expected load of the automations picked (#1538): an order of magnitude, not a quote."""

    #: Runs a day the picked schedules start (their cron over 30 days, divided by 30).
    scheduled_runs_per_day: float
    #: Picked automations that run once per event: their rate depends on the events.
    event_automations: int
    #: Mean cost of one run of the picked automations over ``cost_days``; None when none ran.
    avg_cost_per_run: float | None = None
    #: ``avg_cost_per_run`` times ``scheduled_runs_per_day``; None without a cost history.
    cost_per_day: float | None = None
    cost_days: int


class FleetEnableResponse(BaseModel):
    """The plan (``dry_run``) or the outcome of Enable fleet."""

    fleet_id: str
    dry_run: bool
    #: The run-as passed for every module the map does not name; None when none was.
    run_as_user_id: str | None = None
    #: The user chosen per module (#2650).
    run_as_by_module: dict[str, str] = {}
    #: The modules the selected automations act in: one "Act in <module> as" choice each.
    modules: list[str] = []
    #: Of ``modules``, those nobody was chosen for yet: Enable is refused until each is.
    unchosen: list[str] = []
    #: The automations switched on (or, on a dry run, that would be).
    changed: int
    #: False when nothing is picked or a picked automation misses a module: the dialog's Confirm stays disabled.
    ready: bool
    #: Every provider some selected automation misses, in a stable order.
    missing: list[str]
    items: list[FleetEnableItemResponse]
    #: The fleet's automations after the call.
    automations: FleetAutomationsResponse
    #: On a dry run, the expected runs a day and cost of the picked automations (#1538); None otherwise.
    estimate: FleetEnableEstimateResponse | None = None


class FleetResetRequest(BaseModel):
    """Reset a built-in fleet to its factory state (#1508). ``dry_run`` answers the counts
    for the confirmation dialog; a real reset needs ``confirm_name``, the fleet's name typed."""

    dry_run: bool = False
    confirm_name: str | None = Field(default=None, max_length=200)


class FleetResetResponse(BaseModel):
    """What a reset changed (or, on a dry run, would change)."""

    fleet_id: str
    dry_run: bool
    #: Automations switched off or re-stamped as never set up.
    automations: int
    #: Automations whose run-as user is cleared.
    run_as_cleared: int
    #: Integrations, knowledge bases, skills, MCP servers and custom tools unbound from the members.
    bindings: int
    #: Approval pins removed.
    pins: int
    #: Gates a person added, removed.
    gates: int
    #: The level the fleet moves back to, ``null`` when it is already there.
    autonomy_level: str | None = None
    #: Member agents whose configuration is rewritten from the seed.
    agents: int
    #: Fleet settings put back to their default, by name.
    settings: list[str]
    #: Whether a local documentation version is replaced by the shipped one.
    documentation: bool
    #: Automations an emergency stop holds, left as they are.
    held_by_stop: list[str]
    #: Open decision items (needs_human) of the home members, kept by the reset (#2134).
    open_gates: int = 0
    #: Open work items of the home members, kept by the reset (#2134).
    open_items: int = 0
    #: The fleet's automations after the call.
    automations_state: FleetAutomationsResponse


FleetBulkActionName = Literal["pause", "resume", "set_autonomy"]

#: The most fleets one bulk call acts on by id; the emergency switch (``all_fleets``) has no cap.
MAX_BULK_FLEETS = 100


class FleetBulkAction(BaseModel):
    """One action on several fleets at once (#9769 S3): the Fleets list's selection or,
    with ``all_fleets`` (administrators, ``pause`` only), every fleet automation of the
    platform. ``dry_run`` answers what the call would change without writing, for the
    confirmation dialog's count."""

    action: FleetBulkActionName
    fleet_ids: list[str] = Field(default_factory=list, max_length=MAX_BULK_FLEETS)
    all_fleets: bool = False
    autonomy_level: Literal["full_gates", "medium", "low", "autonomous"] | None = None
    dry_run: bool = False


class FleetBulkOutcome(BaseModel):
    """What the bulk action did, or would do, on one fleet."""

    fleet_id: str
    name: str = ""
    #: False when the fleet was refused (not found, not yours, a raise by a non-admin...).
    ok: bool
    #: Automations switched (pause / resume), or 1 when the autonomy level changed.
    changed: int = 0
    #: Why the fleet was refused, in the API's own words; empty when ``ok``.
    detail: str = ""


class FleetBulkResponse(BaseModel):
    action: FleetBulkActionName
    dry_run: bool
    #: Sum of ``changed`` over the fleets that passed.
    changed: int
    refused: int
    fleets: list[FleetBulkOutcome]


class FleetRunStatsResponse(BaseModel):
    """The fleet's runs over the last 30 days (``fleet_views.FleetRunStats``, #10123): the About KPI row."""

    days: int = 30
    runs: int = 0
    succeeded: int = 0
    #: Failed runs the platform did not interrupt.
    failed: int = 0
    #: One count per day, oldest first.
    by_day: list[int] = Field(default_factory=list)
    #: Runs of the fleet waiting for a person now.
    awaiting_approval: int = 0
    #: ``by_day`` split per outcome (#11996): succeeded, and failed runs the platform did not interrupt.
    succeeded_by_day: list[int] = Field(default_factory=list)
    failed_by_day: list[int] = Field(default_factory=list)
    #: The one-line result of the newest ended run the caller may see; ``null`` when none has one.
    last_summary: str | None = None


class FleetSharedStateHygieneResponse(BaseModel):
    """Shared-state hygiene of the fleet's entries the caller can read (#11696)."""

    #: Active entries over the soft size limit.
    oversize: int = 0
    #: Active entries whose value has not changed for the staleness window, or past their date.
    stale: int = 0
    #: Entries held by a run that ended; the hourly sweep releases them.
    orphaned_leases: int = 0
    #: Active entries whose linked work item is done, cancelled or gone; the sweep resolves them.
    terminal_linked: int = 0


class FleetHealthResponse(BaseModel):
    fleet_id: str
    #: The worst member status (``ok`` for an empty fleet; ``idle`` only when every member is idle).
    status: str
    members: list[FleetMemberHealthResponse]
    #: Open work items of the fleet's hosting workspace the caller can see:
    #: filed there, or owned or created by a current home member, the
    #: population the workspace's own counts read.
    workspace_open_work_items: int
    #: Whether the fleet is set up: its automations, on and paused.
    automations: FleetAutomationsResponse = Field(default_factory=FleetAutomationsResponse)
    #: Its runs over the last 30 days, the caller's visible ones (#10123).
    run_stats: FleetRunStatsResponse = Field(default_factory=FleetRunStatsResponse)
    #: Its shared-state hygiene, the counts the hourly sweep reports to the orchestrator.
    shared_state_hygiene: FleetSharedStateHygieneResponse = Field(default_factory=FleetSharedStateHygieneResponse)


class FleetRunEntry(BaseModel):
    id: str
    agent_id: str
    assignment_id: str | None
    status: str
    created_at: str
    completed_at: str | None
    #: ``run_outcome.run_outcome``, the rule the health badge reads.
    outcome: str = "other"
    #: What started the run, from ``trigger_context``: its ``type``
    #: (``schedule``, ``integration_event``, ``webhook``, ``work_item``,
    #: ``platform_event``, ``chain``...), and for an integration event the
    #: ``provider`` and the ``event`` it answered. ``None`` when not recorded.
    trigger_type: str | None = None
    trigger_provider: str | None = None
    trigger_event: str | None = None
    #: The CTEM case the run works on, when a case event started it (#915). An
    #: id only: the case is read through ``GET /ctem-cases/{id}``, per viewer.
    case_id: str | None = None


class CaseRunEntry(BaseModel):
    """One run working on a CTEM case (#915), for the case page."""

    id: str
    agent_id: str
    agent_name: str | None = None
    status: str
    created_at: str
    completed_at: str | None
    outcome: str = "other"


class CaseRunsResponse(BaseModel):
    """The runs the caller may see that a CTEM case's events started, oldest first."""

    case_id: str
    runs: list[CaseRunEntry]


class FleetActivityResponse(BaseModel):
    """What the fleet's members did lately: their runs the caller may see and
    the caller's delegations from or to a member, newest first."""

    fleet_id: str
    runs: list[FleetRunEntry]
    delegations: list[dict]


class FleetSpecApply(BaseModel):
    """A fleet spec to apply: the spec object, or its YAML / JSON text."""

    spec: dict | None = None
    spec_text: str | None = Field(default=None, max_length=2_000_000)
    workspace_id: str | None = None
    allow_partial: bool = False


class FleetFork(BaseModel):
    """Fork a fleet: an editable personal copy (#9174)."""

    #: Name of the copy; empty is ``Copy of`` the source's name.
    name: str | None = Field(default=None, max_length=200)
    #: Workspace to bind the copy to; empty creates a personal one.
    workspace_id: str | None = None
    #: ``copy``: every member becomes an editable copy. ``keep``: the
    #: orchestrator is copied, the other members are linked as connected agents.
    members: Literal["copy", "keep"] = "copy"


class FleetForkResponse(BaseModel):
    """The fork, the agents it copied and what was left out."""

    fleet: FleetResponse
    copied_agent_ids: dict[str, str]
    kept_agent_ids: list[str]
    assignments_copied: int
    #: Every copied assignment is switched off, whatever the source's state.
    assignments_enabled: int = 0
    warnings: list[str]


class FleetSourceChange(BaseModel):
    """One object of a fork's source that moved since the fork."""

    object: Literal["fleet", "member", "connected"]
    #: The fleet's name, a member's agent slug, a connected agent or fleet id.
    name: str
    action: Literal["added", "removed", "changed"]
    #: The fields that moved, on a change.
    fields: list[str] = Field(default_factory=list)


class FleetSourceSuccessor(BaseModel):
    """The seeded fleet that took over a retired built-in fleet (CTEM-31)."""

    fleet_id: str
    slug: str
    name: str


class FleetSourceChangesResponse(BaseModel):
    """Whether, and how, the source of a fork changed since the fork (#9174)."""

    is_fork: bool
    source_exists: bool
    #: ``null`` when unknown: not a fork, or the source is gone.
    changed: bool | None
    #: False on a fork older than the kept structure: the diff is not known.
    diff_available: bool
    changes: list[FleetSourceChange]
    #: The seeded fleet that took over a retired built-in source (CTEM-31), else ``null``.
    successor: FleetSourceSuccessor | None = None


class FleetPrerequisiteState(BaseModel):
    """One live line of the About page checklist, evaluated for the reader."""

    check: str
    label: str
    #: An app route that fixes it, or empty.
    fix_route: str
    #: ``null`` for a ``manual`` check, never evaluated.
    met: bool | None


class FleetLivePrerequisite(BaseModel):
    """One line of the checklist derived from the fleet's configuration (#10258), checked live for the reader."""

    check: str
    #: ``integration`` | ``agent_account`` | ``handover`` | ``knowledge_base`` | ``mcp_server`` |
    #: ``automations`` | ``profile`` | ``manual``.
    kind: str
    label: str
    #: ``met`` | ``not_met`` | ``unreadable`` (not shared with the reader) | ``to_confirm`` (a manual line).
    state: str
    #: Why it is not met, in plain words; empty when met.
    reason: str
    #: The home members that need it (agent ids).
    member_ids: list[str]
    #: A real app path that fixes it, or empty.
    fix_route: str
    #: The integration provider key (the logo), empty otherwise.
    provider: str = ""
    #: The reason as a code (``fleet_about.REASON_CODES``) and its values, for the page to translate.
    reason_code: str = ""
    reason_params: dict[str, str] = Field(default_factory=dict)
    #: The label as a catalog key and its values when the platform wrote it; empty for a name.
    label_key: str = ""
    label_params: dict[str, str] = Field(default_factory=dict)
    #: Whether the reader may open ``fix_route`` (an administrator's page is an admin's alone).
    can_fix: bool = False
    #: A ``manual`` line a person confirmed (#10469 S2): display name and ISO time; kept, with the
    #: line back to ``to_confirm``, once the confirmation is older than 90 days.
    confirmed_by: str = ""
    confirmed_at: str = ""
    #: Whether the reader may confirm or undo this ``manual`` line (a fleet manager).
    can_confirm: bool = False


class FleetDocFindingsDismissRequest(BaseModel):
    """Dismiss configuration suggestions (#10536): the ids given, or every visible one when ``ids`` is null."""

    ids: list[str] | None = Field(default=None, max_length=100)
    #: Optional one line: "standing decision", "false positive" or free text; fed back to the generator.
    reason: str = Field(default="", max_length=200)


class FleetDocFindingsRestoreRequest(BaseModel):
    """Bring back dismissed suggestions (#10536): the ids given, or every one when ``ids`` is null."""

    ids: list[str] | None = Field(default=None, max_length=100)


class FleetPrerequisiteConfirmRequest(BaseModel):
    """Confirm or undo one ``manual`` line of the About checklist (#10469 S2)."""

    #: The line's ``check`` as the checklist sends it (``manual:<key>``).
    check: str = Field(min_length=1, max_length=200)
    confirmed: bool = True


class FleetDocumentationRestoredFrom(BaseModel):
    """Where a restored version came from: its text's author and the version it was (#9768 S2)."""

    #: The version brought back; ``null`` for the shipped (Filigran) text.
    version: int | None = None
    #: Who wrote the text brought back.
    author: Literal["seed", "fork", "ai"]


class FleetDocumentationResponse(BaseModel):
    """A fleet's documentation as its About page reads it."""

    #: Every section key, in reading order, written or not.
    section_keys: list[str]
    sections: dict[str, str]
    #: 0 while nothing is written.
    version: int
    updated_at: str | None
    #: A user id, ``seed`` (built-in) or ``fork`` (copied at the fork).
    updated_by: str | None
    #: Whether the reader may regenerate it: a fleet the reader manages, built-in or custom.
    editable: bool
    #: A built-in fleet regenerated locally: "Restore shipped version" brings back the seed.
    local: bool = False
    #: Set when the version shown is a restore.
    restored_from: FleetDocumentationRestoredFrom | None = None
    #: ``null`` while nothing is written; true once the fleet's structure or
    #: member configuration moved since the last save.
    out_of_date: bool | None
    #: The structured parts the About page draws (trigger cards, steps, gates,
    #: objects, prerequisites, quota); ``null`` when the fleet has none.
    structured: dict | None = None
    #: ``structured.prerequisites`` evaluated for the reader (rows only, no run).
    prerequisite_states: list[FleetPrerequisiteState] = Field(default_factory=list)
    #: The checklist derived from the members' configuration, checked live (#10258).
    live_prerequisites: list[FleetLivePrerequisite] = Field(default_factory=list)
    #: The fleet's autonomy level and its declared gates, each with whether it
    #: waits at that level and at every level (``fleet_autonomy.gate_catalog``,
    #: #9769 S3): the autonomy control says, per step, what becomes automatic.
    autonomy_level: str = "full_gates"
    autonomy_catalog: list[dict] = Field(default_factory=list)
    #: What the AI found wrong with the fleet's configuration (#10246), each ``{id, kind, member, finding,
    #: suggested_fix}`` (``fleet_doc_findings``); dismissed ones left out while their member is unchanged;
    #: empty for a reader who cannot manage the fleet. Generator notes are on the version history only.
    findings: list[dict] = Field(default_factory=list)
    #: The dismissals that hold (#10536), newest first: ``{id, kind, member, subject, finding, at, by,
    #: by_name, reason}``; empty for a reader who cannot manage the fleet.
    dismissed_findings: list[dict] = Field(default_factory=list)
    #: A generation runs on the server: the first one after the fleet's creation (#10316) or a
    #: regenerate from the page or the fleet tool (#10817). The About page spins and polls.
    generating: bool = False
    #: When the running generation started (ISO 8601 UTC); None when nothing runs or unknown.
    generating_started_at: str | None = None
    #: Why the last generation failed, for a few minutes after it and while nothing runs.
    generation_failed: str | None = None


class FleetApprovalGatesResponse(BaseModel):
    """A fleet's approval gate catalog and the gates proposed from its configuration (#10014)."""

    #: The catalog the level acts on now (stored, else the documentation's).
    catalog: list[dict] = Field(default_factory=list)
    #: Whether the catalog is stored on the fleet (configuration) rather than read from its documentation.
    stored: bool = False
    #: The gates computed from the members' configuration, ready to accept; no model call.
    proposals: list[dict] = Field(default_factory=list)
    #: Approvals the fleet already has outside the level: ``{member, approval}``.
    existing_approvals: list[dict] = Field(default_factory=list)
    #: Whether the reader may write the catalog (a manager of a custom fleet).
    editable: bool = False
    #: Gates a person added to a built-in fleet (#916, CTEM-34), kept across seed upgrades.
    customer_gates: list[dict[str, object]] = Field(default_factory=list)
    #: Whether the reader may add gates to a built-in fleet (its manager): ``PUT approval-gates`` writes them.
    customer_gates_editable: bool = False
    #: The fleet's level and the gates it acts on (#10153): declared + computed,
    #: each with ``source``, ``pin``, ``tools``, ``gated`` and ``by_level``
    #: (``fleet_autonomy.gate_catalog``), so the cursor previews every step.
    autonomy_level: str = "full_gates"
    effective_catalog: list[dict] = Field(default_factory=list)
    #: Whether the level acts on the computed catalog (``approval_pins`` stored).
    computed_catalog_on: bool = False
    #: Where the fleet's instructions put it, between-step positions allowed
    #: (#10168, ``fleet_autonomy.INSTRUCTIONS_POSITIONS``); ``None`` = not set.
    #: Never enforced by the platform.
    instructions_position: str | None = None
    #: Whether the members are told it in their run prompt (a person moved it).
    instructions_position_announced: bool = False
    #: Whether the platform also pauses the tool calls of a waiting gate (#1467).
    #: Off by default: the gates are instructions to the members.
    enforce_at_tool_level: bool = False


class FleetInstructionsPositionRequest(BaseModel):
    """Set where a fleet's instructions put it (#10168); ``None`` clears it."""

    position: (
        Literal["full_gates", "full_gates_medium", "medium", "medium_low", "low", "low_autonomous", "autonomous"]
        | None
    )
    #: Tell the members (one line in their run prompt). ``False`` records an
    #: initial reading of how the fleet runs today, display only (B2-I rule 4).
    announce: bool = True


class FleetAutonomyApplyRequest(BaseModel):
    """Apply a level and the per-action pins in one call (#10153 C3)."""

    level: Literal["full_gates", "medium", "low", "autonomous"]
    #: ``{gate key: "wait" | "run"}``; omitted, the stored pins are kept.
    pins: dict[str, Literal["wait", "run"]] | None = Field(default=None, max_length=200)
    #: Opt the fleet in to (or out of) tool-level pauses (#1467), administrators
    #: only; omitted, the stored value is kept.
    enforce_at_tool_level: bool | None = None


class FleetApprovalGatesUpdateRequest(BaseModel):
    """The catalog to store, each gate a ``DocGate`` (key, action, module, approver, impact, tools)."""

    #: ``app.services.fleet_docs.GATES_MAX`` (a test keeps them equal): every
    #: proposal is accepted at once (UX round 4 B1 on #10034).
    gates: list[dict] = Field(default_factory=list, max_length=200)


class FleetDocumentationRegenerateRequest(BaseModel):
    """Regenerate a fleet's documentation with AI and save it (#9768 S2): never typed by hand."""

    #: One section key to regenerate; omitted, every section and the structured parts.
    section: str | None = Field(default=None, max_length=40)
    #: The version the page shows; a regenerate over a newer one is refused (409).
    base_version: int | None = Field(default=None, ge=0)


class FleetDocumentationVersion(BaseModel):
    """One version of a fleet's documentation in its history (#9768 S2), without its text."""

    version: int
    updated_at: str | None
    #: A user id, ``seed`` (built-in) or ``fork`` (copied at the fork).
    updated_by: str | None
    #: The sections written in that version, in reading order.
    section_keys: list[str]
    #: The version the About page shows now.
    current: bool
    #: Set when that version is a restore.
    restored_from: FleetDocumentationRestoredFrom | None = None
    #: What the generator left out or derived for that version, each ``{code, params}``
    #: (``fleet_docs.NOTE_TEXTS``); empty for a reader who cannot manage the fleet (#10246).
    notes: list[dict] = Field(default_factory=list)
    #: That version lost a visual block or misses a section: Regenerate is offered (#10246).
    needs_regenerate: bool = False


class FleetDocumentationHistoryResponse(BaseModel):
    """The current version then the kept ones, newest first."""

    versions: list[FleetDocumentationVersion]


class FleetDocumentationRestoreRequest(BaseModel):
    """Bring a kept version back as the next version (#9768 S2)."""

    version: int = Field(ge=1)
    #: The version the page shows; a restore over a newer one is refused (409).
    base_version: int | None = Field(default=None, ge=0)


class FleetMemberInstructionDraftRequest(BaseModel):
    """Ask for an AI draft of a connected member's collaboration instruction (#9162 R5)."""

    #: What the instruction should convey, in the person's words.
    brief: str = Field(min_length=2, max_length=2000)
    #: The text in the editor now; an "Improve with AI" round keeps its points.
    current_text: str | None = Field(default=None, max_length=4000)


class FleetMemberInstructionDraftResponse(BaseModel):
    """A draft for the editor; nothing was saved."""

    text: str
    #: Still cut at the output limit after one retry: returned as drafted.
    truncated: bool = False


class FleetSpecChange(BaseModel):
    object: str
    name: str
    action: Literal["create", "update", "add", "remove", "unchanged"]
    detail: str = ""


class FleetSpecPlanResponse(BaseModel):
    """What an apply did, or in a dry run would do."""

    dry_run: bool
    applied: bool
    fleet_id: str | None
    fleet_slug: str
    changes: list[FleetSpecChange]
    unresolved: list[str]
    warnings: list[str]


class FleetSpecGenerate(BaseModel):
    """Create a fleet from a prompt: what the person wants."""

    prompt: str = Field(min_length=3, max_length=8000)
    #: A spec to refine instead of starting over.
    current: dict | None = None
    workspace_id: str | None = None


class FleetSpecGenerateResponse(BaseModel):
    """The generated spec and its dry-run plan; nothing was written."""

    spec: dict
    plan: FleetSpecPlanResponse
    #: What was adjusted in the model's answer (a key slugified, a field left out).
    notes: list[str]


class FleetCardAgent(BaseModel):
    id: str
    name: str
    description: str = ""


class FleetCardResponse(BaseModel):
    """The fleet as other fleets see it (A2A-shaped)."""

    id: str
    name: str
    slug: str
    description: str
    skills: list[dict]
    entry_agent: FleetCardAgent | None
    exposed_agents: list[FleetCardAgent]
    accepted_handover_types: list[str]
    workspace_name: str


class AgentHomeFleet(BaseModel):
    """The agent's home fleet, as its agent page shows it."""

    fleet_id: str
    fleet_name: str
    member_id: str
    #: The member it reports to; ``None`` reports to the orchestrator (top level).
    parent_member_id: str | None = None
    #: The parent's agent name; empty when the caller cannot see that agent.
    parent_name: str = ""
    role_label: str = ""
    is_orchestrator: bool = False
    #: The fleet names an orchestrator; a top-level member of a fleet without one reports to nobody.
    has_orchestrator: bool = False
    #: Reachable from other fleets: its own flag, or the orchestrator (always exposed).
    is_exposed: bool = False
    is_archived: bool = False
    can_manage: bool = False


class AgentConnectedFleet(BaseModel):
    """A fleet that connects the agent without being its home."""

    fleet_id: str
    fleet_name: str
    member_id: str
    collaboration_instruction: str = ""
    context_inclusion: str = "none"
    is_archived: bool = False
    can_manage: bool = False


class AgentFleetsResponse(BaseModel):
    """Where an agent sits among the fleets the caller can see.

    ``home`` is ``None`` when the agent is in the playground, or when its home
    fleet is not visible to the caller (sharing a fleet gives nothing on the
    agents, and the reverse holds too); ``home_hidden`` tells the two apart
    without naming the fleet.
    """

    agent_id: str
    home: AgentHomeFleet | None = None
    #: The agent has a home fleet the caller cannot see (never named).
    home_hidden: bool = False
    connected_to: list[AgentConnectedFleet] = Field(default_factory=list)
