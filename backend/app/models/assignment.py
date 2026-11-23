# SPDX-License-Identifier: Apache-2.0

"""Assignment model – event-driven automation rules attached to agents.

An Assignment defines: when something happens (trigger), what should the AI do
(prompt + tools), and how to handle the output (draft, notify).

Trigger types:
  - schedule:          Cron expression, runs periodically
  - webhook:           Custom HTTP endpoint, receives external POST payloads
  - integration_event: Reacts to events from integrations (new email, issue closed, etc.)
  - manual:            Triggered from UI or API

The AI handles orchestration: given the trigger payload and prompt, it uses
available tools (MCP servers, built-in) to analyze and act. No hard-coded
step graphs — the intelligence comes from the LLM.
"""

import uuid
from copy import deepcopy
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    event,
    text,
)
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.dialects.postgresql import JSON, UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.orm.attributes import set_committed_value

from app.core.database import Base
from app.core.security_options import security_option

#: The ``trigger_config`` key an integration-event trigger names its event in.
#: Every reader reads it; ``LEGACY_TRIGGER_EVENT_KEY`` is what the agent
#: builder documented and stored earlier, and it is read only on a trigger
#: that matches exactly by its own marker (``read_trigger_config``).
TRIGGER_EVENT_KEY = "event"
LEGACY_TRIGGER_EVENT_KEY = "event_type"


def _names_an_event(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def normalize_trigger_event_key(trigger_type: Any, config: Any) -> Any:
    """*config* with a legacy ``event_type`` moved to ``event`` (copy-on-write).

    Only for ``integration_event`` triggers. The legacy value fills an absent
    ``event`` (missing, ``null`` or ``""``) as it is - one that names no event
    then matches nothing instead of reading as no selection at all - and
    replaces an ``event`` naming none when it names one itself. A present
    ``event`` that names nothing (``[]``, ``False``, ``0``) is never replaced
    by a legacy value that names nothing either: that would turn a selection
    admitting nothing into no selection. Anything else is returned unchanged.
    """
    if trigger_type != "integration_event" or not isinstance(config, dict):
        return config
    if LEGACY_TRIGGER_EVENT_KEY not in config:
        return config
    normalized = dict(config)
    legacy = normalized.pop(LEGACY_TRIGGER_EVENT_KEY)
    current = normalized.get(TRIGGER_EVENT_KEY)
    absent = current is None or current == ""
    if absent or (not _names_an_event(current) and _names_an_event(legacy)):
        normalized[TRIGGER_EVENT_KEY] = legacy
    return normalized


#: How an integration-event or webhook trigger matches a delivery's event key.
#: ``exact`` is written on a trigger created from scratch, on one whose
#: events are chosen again, and on one a person switches (``settle_event_matching``).
#: A trigger without it keeps the prefix match it always had (``pull_request``
#: also fires on ``pull_request_review``) through every other save, a copy
#: included, unless ``WEBHOOK_EXACT_EVENT_MATCHING`` applies exact matching
#: everywhere.
EVENT_MATCHING_KEY = "event_matching"
EVENT_MATCHING_EXACT = "exact"
EVENT_MATCHING_TRIGGER_TYPES = frozenset({"integration_event", "webhook"})

#: The ``trigger_config`` keys a trigger chooses its events with: a save that
#: changes one chooses them again. ``event_type`` is not one of them: on a
#: trigger without the marker it is not read at all.
EVENT_SELECTION_KEYS: tuple[str, ...] = ("provider", TRIGGER_EVENT_KEY, "events", "path")


def marked_exact(config: Any) -> bool:
    """Whether a stored ``trigger_config`` carries the exact-matching marker itself."""
    return isinstance(config, dict) and config.get(EVENT_MATCHING_KEY) == EVENT_MATCHING_EXACT


def uses_exact_event_matching(config: Any) -> bool:
    """Whether a stored ``trigger_config`` matches its event exactly.

    Its own marker, or ``WEBHOOK_EXACT_EVENT_MATCHING`` (Settings > Access and security),
    which only narrows the match: it never reads a legacy ``event_type``.
    """
    return marked_exact(config) or bool(security_option("webhook_exact_event_matching"))


def read_trigger_config(trigger_type: Any, config: Any) -> Any:
    """*config* as the receivers and the poller read it.

    A trigger marked exact reads a legacy ``event_type`` as its event
    (``normalize_trigger_event_key``). Any other is read as it always was:
    ``event`` alone, so a row the agent builder stored with ``event_type``
    keeps running on every delivery of its provider, and stays unpolled,
    until its own trigger is switched.
    """
    if not marked_exact(config):
        return config
    return normalize_trigger_event_key(trigger_type, config)


def mark_exact_event_matching(trigger_type: Any, config: Any) -> Any:
    """*config* with the exact-matching marker (copy-on-write).

    Only for the trigger types that select events; anything else, and a
    config already marked, is returned unchanged.
    """
    if trigger_type not in EVENT_MATCHING_TRIGGER_TYPES or not isinstance(config, dict):
        return config
    if marked_exact(config):
        return config
    return {**config, EVENT_MATCHING_KEY: EVENT_MATCHING_EXACT}


def new_trigger_config(trigger_type: Any, config: Any) -> Any:
    """The ``trigger_config`` of a trigger created from scratch: its event under
    ``event``, matched exactly.

    Only for a trigger nobody had before - the REST create, the agent builder's
    ``create_assignment``, an agent template's assignments. A copy (duplicate,
    import, catalog clone) keeps its source's config as it is, marker or not.
    """
    return mark_exact_event_matching(trigger_type, normalize_trigger_event_key(trigger_type, config))


def _event_selection(config: Any) -> tuple[Any, ...]:
    if not isinstance(config, dict):
        return (config,)
    # Absent, null and every other falsy value select nothing on a trigger
    # saved before exact matching: one selection, whichever a client sends.
    return tuple(config.get(key) or None for key in EVENT_SELECTION_KEYS)


def settle_event_matching(
    previous_type: Any, previous_config: Any, trigger_type: Any, config: Any
) -> Any:
    """The ``trigger_config`` a save stores, in the matching mode it keeps or takes.

    A save keeps the stored trigger's mode through anything that does not
    choose its events: a new name, prompt, delay, filter or integration leaves
    a trigger saved before exact matching on its prefix match, and an exact
    trigger stays exact when the config sent lost the marker. The trigger
    matches exactly from the save that asks for it (``event_matching: exact``
    in the config sent), that changes one of ``EVENT_SELECTION_KEYS``, or that
    turns another trigger type into this one.
    """
    if trigger_type not in EVENT_MATCHING_TRIGGER_TYPES or not isinstance(config, dict):
        return config
    if (
        marked_exact(config)
        or marked_exact(previous_config)
        or previous_type != trigger_type
        or _event_selection(previous_config) != _event_selection(config)
    ):
        return mark_exact_event_matching(trigger_type, config)
    return config


class Assignment(Base):
    """An automation rule belonging to an Agent."""

    __tablename__ = "assignments"
    # Hot-path indexes for the scheduler and active-assignment counts. The
    # single-column FK indexes (agent_id, user_id, run_as_user_id) are declared
    # inline via ``index=True`` below.
    __table_args__ = (
        Index("ix_assignments_enabled", "enabled"),
        Index("ix_assignments_trigger_type", "trigger_type"),
        Index("ix_assignments_agent_enabled", "agent_id", "enabled"),
        Index("ix_assignments_trigger_enabled", "trigger_type", "enabled"),
        # Partial index for the company-managed arm of the three-tier
        # visibility filter (``OR is_company_managed IS TRUE``). Without it
        # the planner cannot bitmap-OR that arm with the user_id index and
        # falls back to a sequential scan. Tiny (company rows only).
        Index(
            "ix_assignments_company_managed",
            "created_at",
            postgresql_where=text("is_company_managed IS TRUE"),
        ),
        # Supports the ``paused_by_user_id`` FK's ON DELETE SET NULL action,
        # not any query. Partial because only pauses with a human
        # actor populate the column, so a full index would be almost entirely
        # NULL entries. Declaration must match the migration's
        # (``a1paus3198``) definition exactly — same name, same predicate — or
        # the index reconciler would try to create a second, different index
        # under the same name on installs that already ran the migration.
        Index(
            "ix_assignments_paused_by_user_id",
            "paused_by_user_id",
            postgresql_where=text("paused_by_user_id IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    agent_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # The owning user — ALWAYS set (the creator, or the admin for seeded
    # assignments). It is never nulled when the parent agent becomes
    # company-managed; ``is_company_managed`` is the visibility flag and is
    # kept in sync with the parent agent. (Column stays nullable only to avoid
    # a NOT NULL backfill on legacy rows.)
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )

    # Visibility flag mirrored from the parent agent. When True the assignment
    # is shared with all users and only admins can edit it. Kept in sync with
    # the agent on create and on the company-managed toggle. Replaces the old
    # "user_id IS NULL means company-managed" convention.
    is_company_managed: Mapped[bool] = mapped_column(Boolean, default=False)

    # Optional "run as" identity for company-managed agents. When set, runs of
    # this assignment execute under this user's context — their connected
    # integrations (incl. per-user template bindings), LLM model preference and
    # memory. Only meaningful/editable for company-managed agents (admin-only).
    # NULL = run as the assignment owner (``user_id``, i.e. the admin by
    # default). FK SET NULL so deleting the user falls back to the owner.
    run_as_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    # When True, an automatic (schedule) trigger fans OUT to one run per ACTIVE
    # user, each executing under that user's identity (their integrations / JWT /
    # memory). This turns a single company-managed automation into per-user
    # results (e.g. the exposure briefing, scoped to what each user can see in
    # OpenCTI/OpenAEV). Only meaningful for company-managed, schedule-triggered
    # assignments. Default OFF. Pairs naturally with ``hide_runs`` so each user
    # only sees their own run.
    run_as_all_users: Mapped[bool] = mapped_column(Boolean, default=False)

    # Per-assignment override of the agent's ``hide_runs`` default.
    # NULL = inherit the agent. True/False = explicit override. Controls whether
    # this assignment's runs are hidden from other users (resolved into the
    # per-run ``is_company_managed`` visibility flag at run creation).
    hide_runs: Mapped[bool | None] = mapped_column(Boolean, nullable=True, default=None)

    # Display
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    # --- Trigger ---
    # "schedule" | "webhook" | "integration_event" | "manual"
    trigger_type: Mapped[str] = mapped_column(String(30), nullable=False)

    # Trigger-specific config:
    # schedule:          { "cron": "*/5 * * * *" }
    # webhook:           {}  → /api/hooks/assignments/{assignment_id}
    # integration_event: { "provider": "microsoft", "event": "new_email", "filter": {...} }
    # manual:            {}
    trigger_config: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)

    # --- Activation ---
    # Optional "should I run?" prompt evaluated BEFORE the main prompt.
    # If set, the LLM is asked this question first with the trigger payload.
    # If the LLM answers "no" / "skip" / "not relevant", the assignment is skipped.
    # This saves cost and avoids noise — e.g. "Is this message a question about
    # our product in one of my monitored channels? Answer YES or NO."
    activation_prompt: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Optional tool-capable gate evaluated by the platform, with no model call,
    # before the run is charged or set up, and before ``activation_prompt``:
    # ``{"tool", "arguments", "condition"}`` - one read-only tool call and a
    # condition on its result (``services/assignment_activation_check.py``).
    # NULL = no tool check.
    activation_check: Mapped[dict | None] = mapped_column(JSON, nullable=True, default=None)

    # --- Execution ---
    # The main AI prompt: what should the agent do when triggered?
    # Only runs if activation_prompt passes (or if activation_prompt is not set).
    # Can use template variables: {{trigger.payload}}, {{agent.persona}}, etc.
    prompt: Mapped[str] = mapped_column(Text, nullable=False)

    # Optional delay before execution (e.g. "10m", "1h", "30s")
    # Useful for "wait 10 minutes then check" patterns.
    delay: Mapped[str | None] = mapped_column(String(20), nullable=True)

    # Tool access for this assignment:
    # List of MCP server names or IDs the AI can use during execution.
    # If empty, uses the agent's default mcp_server_ids.
    tool_sources: Mapped[list | None] = mapped_column(JSON, nullable=True, default=list)

    # Integration bindings for this specific assignment.
    # Maps provider name → integration ID.
    #   NULL  → inherit ALL integration bindings from the parent agent (default).
    #   {}    → explicitly no integrations (the assignment gets zero integration tools).
    #   {...} → custom subset of the agent's integrations.
    integration_bindings: Mapped[dict | None] = mapped_column(
        JSON, nullable=True, default=None
    )

    # Per-provider tool allow lists for this assignment.
    # Maps provider key → list of tool names (or null to allow all).
    # NULL means no assignment-level restriction (use agent-level scoping only).
    # When set, the effective tools are the INTERSECTION of agent-level and
    # assignment-level allowed tools.
    integration_allowed_tools: Mapped[dict | None] = mapped_column(
        JSON, nullable=True, default=None
    )

    # Per-group built-in function allow lists for this assignment.
    # Maps built-in tool group id → list of function names (or null to allow
    # all). NULL means no assignment-level restriction; when set, the
    # effective functions are the INTERSECTION with the agent's
    # ``builtin_allowed_tools`` (filtering.merge_tool_allow_lists).
    builtin_allowed_tools: Mapped[dict | None] = mapped_column(
        JSON, nullable=True, default=None
    )

    # Integration element bindings: scope this assignment to specific sub-elements
    # within an integration (e.g. specific Teams channels, Slack channels, GitHub repos).
    # Maps "{provider}:{element_type}" → list of selected element objects.
    # Each element has: id, name, group, metadata (provider-specific).
    # Example: {"microsoft:teams_channel": [{"id": "team:ch", "name": "General", ...}]}
    # NULL = no element binding (assignment operates on all elements, default).
    integration_element_bindings: Mapped[dict | None] = mapped_column(
        JSON, nullable=True, default=None
    )
    # The element types whose selection above is a hard boundary on the
    # integration tools rather than prompt guidance, as on the agent.
    # A type the agent enforces is a boundary for its assignments whatever
    # this says: the stricter of the two wins.
    # NULL = every selection is guidance (default, and every existing row).
    integration_element_enforcement: Mapped[list | None] = mapped_column(
        JSON, nullable=True, default=None
    )

    # Unified channel/bot configuration for this assignment.
    # Same structure as Agent.channel_config.
    # NULL = inherit from parent agent (default).
    # {} = explicitly no channels for this assignment.
    # Partial dict = assignment-specific config (must be subset of agent config).
    channel_config: Mapped[dict | None] = mapped_column(
        JSON, nullable=True, default=None
    )

    # Runner refinement for this assignment (native OS-control daemons).
    # Non-destructive refinement of the parent agent's runner access (mirrors
    # tool / KB scoping):
    #   NULL  → inherit the agent's runner selection (the default).
    #   [ids] → refine to that subset (stale ids not available to the agent are
    #           dropped at resolution time).
    #   []    → explicitly no runners (the assignment gets no ``computer_use``
    #           tools even if the agent has runners).
    runner_ids: Mapped[list | None] = mapped_column(JSON, nullable=True, default=None)

    # DEPRECATED — superseded by the ``read_knowledge_base_ids`` semantics
    # below. Read has no "none" state (NULL / [] both mean "inherit all agent
    # KBs"), so this flag can no longer be honored and is no longer consulted
    # during execution; it is kept only so legacy rows still deserialize.
    use_knowledge_bases: Mapped[bool] = mapped_column(Boolean, default=True)

    # KB IDs the assignment may READ (RAG search), as a non-destructive
    # refinement of the agent's bound knowledge bases (mirrors integration/tool
    # scoping). Read is bulletproof: it never resolves to "no search" while the
    # agent has KBs.
    #   NULL / [] → inherit ALL of the agent's KBs (the default).
    #   [ids]     → refine to that subset (stale ids not bound to the agent are
    #               dropped; if nothing survives, it falls back to ALL agent KBs).
    # Empty selections are normalized to NULL on save so the column stays clean.
    read_knowledge_base_ids: Mapped[list | None] = mapped_column(JSON, nullable=True)

    # KB IDs the assignment may WRITE to (``store_knowledge``), as a refinement
    # of the agent's WRITABLE knowledge bases (those flagged "write" in
    # ``Agent.knowledge_base_permissions``):
    #   NULL    → inherit ALL of the agent's writable KBs (the default).
    #   [ids]   → refine to that subset (ids not writable by the agent are
    #             dropped at resolution time).
    #   []      → explicitly none (the assignment never writes, even if the
    #             agent could).
    write_knowledge_base_ids: Mapped[list | None] = mapped_column(
        JSON, nullable=True
    )

    # DEPRECATED — write access is now driven by the agent's per-KB
    # ``knowledge_base_permissions`` plus the assignment's
    # ``write_knowledge_base_ids`` refinement. These two columns are retained
    # for backward-compatible deserialization and are backfilled into the new
    # model by migration; they are no longer consulted at execution time.
    write_knowledge_base_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    target_knowledge_base_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("knowledge_bases.id", ondelete="SET NULL"),
        nullable=True,
    )

    # --- Output behavior ---
    # ai_label: Prepend "[AI-generated]" or similar to outgoing messages
    ai_label: Mapped[bool] = mapped_column(Boolean, default=True)

    # Custom system prompt for AI label. NULL = use platform default.
    ai_label_prompt: Mapped[str | None] = mapped_column(Text, nullable=True)

    # notify_on_complete: Send a notification (bell icon) to the user when the assignment finishes
    notify_on_complete: Mapped[bool] = mapped_column(Boolean, default=False)

    # show_in_digest: Show the result in "Latest Results" on the dashboard (for digests, reports, analyses)
    show_in_digest: Mapped[bool] = mapped_column(Boolean, default=False)

    # max_iterations: per-assignment cap for the agentic loop.
    # NULL = inherit: the assignment falls back to the agent's max_iterations
    # override, then to the platform default (GeneralAssistantConfig). When a
    # value IS set it takes PRECEDENCE over the agent's override (the assignment
    # is the most specific scope). Resolved in assignment_engine.
    max_iterations: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Per-assignment override of the ``wait`` policy, same shape as
    # ``Agent.wait_policy``; wins over the agent's, bounded by the platform's.
    # NULL = inherit.
    wait_policy: Mapped[dict | None] = mapped_column(JSON(none_as_null=True), nullable=True, default=None)

    # Organization summary in the run's system prompt (#11533): "inherit"
    # follows the agent, then the platform; "always" or "off" wins over both
    # (``services/organization_context_injection.py``).
    organization_context_mode: Mapped[str] = mapped_column(
        String(10), nullable=False, default="inherit", server_default="inherit"
    )
    # Categories preloaded for this assignment's runs (#11533): NULL inherits
    # the agent's, then the platform's; a list (empty included) wins.
    organization_context_categories: Mapped[list[str] | None] = mapped_column(JSON, nullable=True, default=None)

    # timeout_seconds: Maximum wall-clock time for a single run.
    # If the agentic loop exceeds this, the run is cancelled.
    # Default 300s (5 minutes). Set to 0 to disable.
    # At runtime the engine floors the effective budget to
    # max(600 s, max_iterations × 20 s/iter) — see
    # assignment_engine._apply_iteration_timeout_floor —
    # so a stored value below the floor is raised (never reduced); only 0
    # (disabled) bypasses the floor.
    timeout_seconds: Mapped[int] = mapped_column(Integer, default=300)

    # --- Retry policy ---
    # retry_on_failure: Automatically re-execute when a run fails.
    retry_on_failure: Mapped[bool] = mapped_column(Boolean, default=False)
    # max_retries: Number of retry attempts before giving up (used when retry_on_failure=True).
    max_retries: Mapped[int] = mapped_column(Integer, default=3)

    # --- Chaining ---
    # Optional: trigger another assignment when this one completes successfully.
    # The next assignment receives the result of this one as its trigger payload.
    next_assignment_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("assignments.id", ondelete="SET NULL"),
        nullable=True,
    )

    # --- Human in the loop ---
    # NULL = inherit from agent.  True/False = explicit override.
    # Resolution: assignment override → agent default → False.
    human_in_the_loop: Mapped[bool | None] = mapped_column(
        Boolean, nullable=True, default=None
    )

    # Custom system prompt for HITL. NULL = use platform default.
    human_in_the_loop_prompt: Mapped[str | None] = mapped_column(Text, nullable=True)

    # --- Dry run ---
    # When true, the assignment logs what it would do without taking real actions.
    # Tool calls are intercepted and logged instead of executed.
    dry_run: Mapped[bool] = mapped_column(Boolean, default=False)

    # --- Self-triggered event suppression (loop protection) ---
    # When true, integration events caused by the agent's OWN writes are
    # dropped before they create a run. This breaks the feedback loop where an
    # assignment reacts to an event (e.g. a GitHub issue edit), performs a write
    # of its own (renames the title, posts a comment, sets labels) and that very
    # write fires another event that re-triggers the same assignment forever.
    #
    # Detection is target-based, not actor-based: the agent commonly acts
    # through the owner's own credentials, so the inbound event's author equals
    # the human. When a GitHub write tool runs, a short-lived marker keyed by
    # (integration, repo, issue/PR number) is recorded; an incoming event for a
    # target with a live marker is treated as the agent's own echo and skipped.
    # Default OFF — opt-in per assignment. See services/github_echo.py.
    ignore_self_triggered_events: Mapped[bool] = mapped_column(
        Boolean, default=False
    )

    # --- Manual trigger parameters ---
    # Defines input parameters shown in the UI when manually triggering.
    # Each entry: { key, label, type, description?, required?, default?, options? }
    # where `type` is "text" | "textarea" | "number" | "boolean" | "select".
    # Values become available as {{trigger.<key>}} in the prompt.
    manual_trigger_parameters: Mapped[list | None] = mapped_column(JSON, nullable=True)

    # --- State ---
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)

    # --- Pause provenance ---
    # ``enabled = False`` is rendered as "Paused" in the UI, and until these
    # columns existed it was a dead end: five unrelated code paths write it
    # (a human toggling the switch, an agent calling the ``update_assignment``
    # builder tool, the integration auth-failure quarantine, the scheduler
    # disabling a cron it cannot parse, and the copy paths — duplicate /
    # import / catalog install — that create an assignment switched off) and
    # none of them left anything behind. An assignment that silently stopped
    # firing was indistinguishable from one somebody paused on purpose last
    # month.
    #
    # These five columns describe the CURRENT pause only — they are set
    # together by ``services/assignment_pause.pause_assignment`` and cleared
    # together by ``resume_assignment``, so they are never stale on an enabled
    # assignment. The historical trail (every pause AND resume, with IP) lives
    # in ``audit_logs`` under the ``pause`` / ``resume`` actions; these columns
    # are what the assignment page can render without a join.
    paused_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # One of ``services/assignment_pause.PAUSE_REASONS`` — the machine-readable
    # cause. The actor class is derivable from it (user toggle / agent tool /
    # system quarantine), which is why there is no separate actor column.
    paused_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # The human behind the pause: the user who flipped the switch, or the user
    # an agent was acting as. NULL for autonomous system pauses (quarantine,
    # invalid schedule) — those have no human to blame. SET NULL so deleting
    # the user keeps the pause explanation readable via ``paused_by_label``.
    # Indexed (partially, ``WHERE paused_by_user_id IS NOT NULL``) — not for
    # querying, nothing filters on it, but for the ``ON DELETE SET NULL``
    # referential action: without an index the RI trigger scans the whole
    # ``assignments`` table on every user deletion.
    paused_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        # Explicitly named so ``create_all`` (fresh installs) and the migration
        # (existing installs) produce the SAME constraint — there is no
        # metadata naming convention on this Base, so an unnamed FK would get
        # an auto-generated name on one path only and leave
        # ``alembic revision --autogenerate`` proposing a diff forever.
        ForeignKey(
            "users.id",
            ondelete="SET NULL",
            name="fk_assignments_paused_by_user_id_users",
        ),
        nullable=True,
    )
    # Denormalized display label for the actor — a user's email, an agent name,
    # or the system component. Denormalized on purpose (same reasoning as
    # ``AuditLog.user_email`` / ``Activity.agent_name``): the assignment
    # serializer is synchronous and has no session, and the label must survive
    # the actor being deleted.
    paused_by_label: Mapped[str | None] = mapped_column(String(320), nullable=True)
    # Free-text, already-composed explanation shown to the user ("3 consecutive
    # runs failed integration authentication", "invalid cron expression ...").
    paused_details: Mapped[str | None] = mapped_column(Text, nullable=True)
    # When the assignment was last paused, whatever the reason - and, unlike
    # the columns above, KEPT when it is resumed. A failure streak that
    # pauses the assignment counts only the runs created after it, so resuming
    # starts a fresh streak instead of re-pausing on the next failing run.
    last_paused_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    execution_count: Mapped[int] = mapped_column(Integer, default=0)
    # Integration events the trigger's filter dropped before any run was
    # created (``services/integration_event_filters.py``). NULL reads as 0.
    events_filtered_count: Mapped[int | None] = mapped_column(Integer, nullable=True, default=None)
    last_triggered_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # When a schedule assignment was last enabled again (resume, re-enable).
    # The cron advances from the later of this and ``last_triggered_at``
    # (``core.utils.schedule_base``), so enabling never fires a run by itself:
    # no catch-up of the occurrences missed while it was off or never ran.
    schedule_resumed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )


def _read_stored_trigger_config(target: Assignment) -> None:
    config = target.__dict__.get("trigger_config")
    # What the row holds, for a save that meets the attribute expired.
    target._stored_trigger_config = deepcopy(config)
    read = read_trigger_config(target.__dict__.get("trigger_type"), config)
    target._event_from_legacy_key = (
        read is not config
        and not _names_an_event(config.get(TRIGGER_EVENT_KEY))
        and _names_an_event(read.get(TRIGGER_EVENT_KEY))
    )
    if read is not config:
        set_committed_value(target, "trigger_config", read)


@event.listens_for(Assignment, "load")
def _load_trigger_config(target: Assignment, _context: Any) -> None:
    """A row marked exact and stored with ``event_type`` loads with ``event`` set.

    Committed, not dirty: loading a row never writes it back. Such a row is
    flagged (``event_read_from_legacy_key``); a row without the marker loads
    as it is stored (``read_trigger_config``).
    """
    _read_stored_trigger_config(target)


@event.listens_for(Assignment, "refresh")
def _refresh_trigger_config(target: Assignment, _context: Any, attrs: Any) -> None:
    if attrs is None or "trigger_config" in attrs or "trigger_type" in attrs:
        _read_stored_trigger_config(target)


def event_read_from_legacy_key(assignment: Any) -> bool:
    """Whether a loaded row selects its event only through the legacy ``event_type``.

    Only a row marked exact reads that key, and such a row - written by the
    agent builder earlier, switched since - was never polled: the poller
    read ``event`` alone and skipped it.
    """
    return bool(getattr(assignment, "_event_from_legacy_key", False))


_UNKNOWN = object()


def _previous_value(history: Any, stored: Any) -> Any:
    if history.deleted:
        return history.deleted[0]
    if history.unchanged:
        return history.unchanged[0]
    return stored


@event.listens_for(Assignment, "before_update")
def _saved_trigger_keeps_its_matching(_mapper: Any, _connection: Any, target: Assignment) -> None:
    """A save keeps a trigger's matching mode unless it chooses its events again.

    See ``settle_event_matching``. A row written back unchanged is not an
    update of these columns: SQLAlchemy records no change for an equal value.
    A trigger created from scratch is marked by its creation path
    (``new_trigger_config``); nothing marks an inserted row here, so a copy
    keeps its source's mode.
    """
    if "trigger_config" not in target.__dict__:
        return
    attrs = sa_inspect(target).attrs
    config_history = attrs.trigger_config.history
    type_history = attrs.trigger_type.history
    if not (config_history.has_changes() or type_history.has_changes()):
        return
    previous_config = _previous_value(
        config_history, target.__dict__.get("_stored_trigger_config", _UNKNOWN)
    )
    if previous_config is _UNKNOWN:
        return
    previous_type = type_history.deleted[0] if type_history.deleted else target.trigger_type
    settled = settle_event_matching(
        previous_type, previous_config, target.trigger_type, target.trigger_config
    )
    if settled is not target.trigger_config:
        target.trigger_config = settled


@event.listens_for(Assignment, "after_insert")
@event.listens_for(Assignment, "after_update")
def _remember_written_trigger_config(_mapper: Any, _connection: Any, target: Assignment) -> None:
    if "trigger_config" in target.__dict__:
        target._stored_trigger_config = deepcopy(target.__dict__["trigger_config"])


class AssignmentRun(Base):
    """Execution log for a single assignment run."""

    __tablename__ = "assignment_runs"
    # Hot-path indexes for dashboard stats, run filtering and the scheduler's
    # active-run guard. The single-column FK indexes (assignment_id, agent_id,
    # user_id, parent_run_id) are declared inline via ``index=True`` below.
    __table_args__ = (
        Index("ix_assignment_runs_status", "status"),
        Index("ix_assignment_runs_created_at", "created_at"),
        Index("ix_assignment_runs_agent_created", "agent_id", "created_at"),
        Index(
            "ix_assignment_runs_user_status_created",
            "user_id",
            "status",
            "created_at",
        ),
        Index("ix_assignment_runs_assignment_status", "assignment_id", "status"),
        # Partial index for the company-managed arm of ``run_visible_filter``
        # (``OR is_company_managed IS TRUE``) — used by every runs list,
        # count and dashboard query. Without it the OR arm degrades those
        # queries to sequential scans on the largest table in the system.
        Index(
            "ix_assignment_runs_company_managed_created",
            "created_at",
            postgresql_where=text("is_company_managed IS TRUE"),
        ),
        # Failure-cause breakdowns. Partial: only failed runs carry a
        # cause, so a full index on the largest table in the system would be
        # almost entirely NULL entries. Declaration must match the migration's
        # (``f1fail3128``) definition exactly — same name, same predicate — or
        # the index reconciler would try to create a second, different index
        # under the same name on installs that already ran the migration.
        Index(
            "ix_assignment_runs_failure_cause",
            "failure_cause",
            postgresql_where=text("failure_cause IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    assignment_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("assignments.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    agent_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # The execution identity this run acts as — ALWAYS set. Precedence at
    # creation time: triggering user (manual UI / agent-builder) →
    # assignment ``run_as_user_id`` (company-managed agents) → assignment/agent
    # owner. This is what drives per-user resolution downstream (integration
    # template bindings, LLM model, memory) via
    # ``effective_user_id = run.user_id or agent.user_id``.
    #
    # It is NOT a pure ownership field: do NOT rewrite it to the owner on a
    # visibility change (that corrupts run-as / manual / follow-up
    # attribution). Visibility is the separate ``is_company_managed`` flag,
    # which is what's nulled-or-not — ``user_id`` is never nulled for
    # company-managed runs. (Column stays nullable only to avoid a NOT NULL
    # backfill on legacy rows.)
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )

    # Visibility flag mirrored from the parent agent/assignment at run-creation
    # time. When True the run is shared with all users and only admins can act
    # on it (delete / respond / dismiss). Replaces the old "user_id IS NULL
    # means company-managed" convention.
    is_company_managed: Mapped[bool] = mapped_column(Boolean, default=False)

    # --- Delegation chain ---
    # Self-referential FK: when a run is spawned by another run (via sub-agent
    # delegation), parent_run_id points to the originating run.  NULL for
    # top-level runs triggered by assignments directly.
    parent_run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("assignment_runs.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    # What triggered this run
    trigger_payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    # Rich metadata about the trigger origin (type, user, URL, headers, body, etc.)
    trigger_context: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    # Status: pending → delayed → running → awaiting_human → completed / failed
    status: Mapped[str] = mapped_column(String(20), default="pending")

    # AI conversation log (messages exchanged during execution)
    messages: Mapped[list | None] = mapped_column(JSON, nullable=True)

    # Actions taken by the AI during this run
    actions_taken: Mapped[list | None] = mapped_column(
        JSON, nullable=True, default=list
    )

    # Result summary
    result_summary: Mapped[str | None] = mapped_column(Text, nullable=True)

    # --- Observability / usage accounting (OBS-5 / OBS-6) ---
    # Langfuse trace id of the workflow root trace, persisted when tracing
    # is active so the run detail UI can deep-link to the trace.
    langfuse_trace_id: Mapped[str | None] = mapped_column(
        String(128), nullable=True
    )
    # Local trace id written to ``llm_traces`` asynchronously — no FK to
    # avoid a race between the synchronous commit and the async dispatcher.
    llm_trace_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        nullable=True,
        index=True,
    )
    # Accumulated LLM token usage across all agentic loop iterations.
    total_input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    total_output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Error details (if failed)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    # --- Structured failure attribution ---
    # ``error`` is the free text a human reads; these two are what a query
    # aggregates. Written by ``services/failure_causes.classify_run_failure``
    # at every site that fails a run, so "what is failing on this install, and
    # whose fault is it?" is answerable in SQL instead of by reading strings
    # one at a time.
    #
    # ``failure_determination`` records HOW the cause was reached (structural /
    # heuristic / unattributed) and is deliberately NOT derivable from the
    # cause: the same cause can be read off an SDK status code (trustworthy) or
    # recognised in a stringified message (not). Phase 2's billing table is
    # expected to admit structural determinations only, so it must be able to
    # tell them apart.
    failure_cause: Mapped[str | None] = mapped_column(String(48), nullable=True)
    failure_determination: Mapped[str | None] = mapped_column(
        String(16), nullable=True
    )

    # --- Unfinished completion ---
    # A ``completed`` run whose agent loop stopped before the task was done:
    # the loop's terminal ``stop_reason`` when it is one of
    # ``agent_loop.UNFINISHED_STOP_REASONS`` (``max_iterations``,
    # ``deadline``, ``budget_exhausted``, ``degenerate_repetition``,
    # ``timeout``, ``waiting_on_tasks``).
    # NULL when the run ended on its own terms, and on runs recorded before
    # this column existed. Not a failure: the run keeps the answer it
    # produced, it is just not passed off as complete.
    stop_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)

    # --- Degraded by integration auth failures (#1454) ---
    # ``[{integration, status, first_tool, tools}]``: the integrations whose
    # calls failed authentication in this run, ``status`` "401" (credential
    # rejected, no further call sent in the run) or "403" (forbidden on the
    # listed tools only). NULL when every integration answered. A completed
    # run carrying it is degraded: its result was produced without that data.
    integration_auth_failures: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON, nullable=True)

    # Retry tracking – how many times this run has been retried
    retry_count: Mapped[int] = mapped_column(Integer, default=0)

    # --- Human-in-the-loop state ---
    # When the LLM calls request_human_input, the run is paused here.
    # human_input_question: what the AI is asking the user
    # human_input_response: the user's answer (set by the respond endpoint)
    human_input_question: Mapped[str | None] = mapped_column(Text, nullable=True)
    human_input_response: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Durable HITL resolution trail. The live question/response fields above
    # are cleared by the engine when the run resumes, so the respond/dismiss
    # endpoints append an entry here instead: {question, response, resolution
    # ("answered" | "dismissed"), responded_at, responded_by,
    # responded_by_id}. Never cleared — powers the resolved HITL display in
    # the run panel after the run has resumed.
    human_input_history: Mapped[list | None] = mapped_column(JSON, nullable=True)

    # Who manually started this run, if anyone.
    #
    # NOT the same as ``user_id``: that is the run's execution identity and is
    # always populated (falling back to run-as or the assignment owner), so it
    # cannot answer "did a human start this?". Only a manual trigger sets this;
    # schedules, webhooks and integration events leave it NULL, which is what
    # makes it a usable interactivity signal.
    triggered_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )

    # --- Per-tool approval ---
    # Structured action proposals awaiting a decision, and the decisions once
    # made. Separate from human_input_question/response above because those are
    # single Text columns carrying free text: there is nowhere in them to put a
    # per-call decision or the proposed action itself.
    #
    # Keeping them separate is also what makes backward compatibility clean —
    # the resume path branches on approval_decisions being present, so a run
    # paused by request_human_input (including one paused before this feature
    # shipped) still resumes down the original free-text path.
    #
    # pending_approvals: [{tool_call_id, tool_name, tool_description,
    #   arguments, input_schema, source}]
    # approval_decisions: {tool_call_id: {decision, rejection_reason}}
    pending_approvals: Mapped[list | None] = mapped_column(JSON, nullable=True)
    approval_decisions: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    # --- Notification tracking ---
    # Persisted at execution time so the notification status is accurate even
    # if the assignment's notify_on_complete setting is changed later.
    # True  = a notification was delivered (or attempted) for this run.
    # False = no notification configured for this run.
    notification_sent: Mapped[bool] = mapped_column(Boolean, default=False)

    # --- Friction ledger (services/frictions.py) ---
    # How many frictions of the run had no disposition when it ended; NULL
    # when it left none (a run with no friction is unchanged).
    frictions_undisposed: Mapped[int | None] = mapped_column(Integer, nullable=True)

    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )


class AssignmentRunConversationNotice(Base):
    """The conversation an assignment run reports its end to.

    A run a chat turn triggered is owed to that conversation: once it ends, the
    worker resumes the conversation with its outcome
    (``services/assignment_run_wake.py``), unless a wait or a ``get_run`` of
    that conversation already told the agent (``told_at``). Keyed on the run
    with no foreign key, so creating the table locks no existing one; a notice
    whose run or conversation is gone matches nothing and ages out.
    """

    __tablename__ = "assignment_run_conversation_notices"
    __table_args__ = (
        Index(
            "ix_assignment_run_notices_untold",
            "created_at",
            postgresql_where=text("told_at IS NULL"),
        ),
    )

    run_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    conversation_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    told_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


#: The ``String(n)`` columns an assignment request writes as sent (REST, agent
#: import, agent templates, the agent-builder tools). The other text columns are
#: ``Text``, and the short pause columns are written by the server. The REST
#: schemas also hold ``organization_context_mode`` to its allowed values; an
#: imported bundle carries it as sent (#11598).
ASSIGNMENT_BOUNDED_TEXT_FIELDS: tuple[str, ...] = ("name", "trigger_type", "delay", "organization_context_mode")


def assignment_text_length_error(field: str, value: object) -> str | None:
    """The refusal for a value too long for its ``String(n)`` column, else ``None``.

    PostgreSQL refuses a longer value at the INSERT or UPDATE, which every write
    path answered as a 500. The limit is read off the column, so no caller holds
    a copy of it. A value that is not a string is left to the caller's own type
    check.
    """
    limit = Assignment.__table__.c[field].type.length
    if limit is None or not isinstance(value, str) or len(value) <= limit:
        return None
    return f"{field} must be at most {limit} characters (got {len(value)})."
