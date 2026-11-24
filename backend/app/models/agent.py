# SPDX-License-Identifier: Apache-2.0

"""Agent model – the primary user-configured AI entity.

An Agent is a named, configured AI assistant that belongs to a user or is
company-managed (shared with all users, admin-only editable).

It has a persona (system prompt), bound integrations and MCP servers,
and one or more Assignments that define its autonomous behavior.

Examples:
  - "Email Assistant" (integrations=[M365])
  - "GitHub Ops Agent" (integrations=[GitHub], mcp=[github])
  - "Company FAQ Bot" (is_company_managed=True, user_id=NULL)
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSON, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

DEFAULT_AGENT_HANDOVER_PROMPT = (
    "Delegate to peer agents when beneficial. "
    "`consult_agent`: ask a question, get an answer, stay in control. "
    "`transfer_to_agent`: hand over the conversation permanently. "
    "`spawn_background_task`: delegate work that runs in the background "
    "while you keep working. "
    "Prefer consult for quick lookups; transfer when the other agent should "
    "fully take over; use background tasks for long-running work. After "
    "spawning, continue independent tools in this turn - only wait when you "
    "are blocked on those tasks."
)

DEFAULT_ASSIGNMENT_HANDOVER_PROMPT = (
    "Delegate to peer agents when beneficial using `spawn_background_task`. "
    "Provide the target agent's ID or slug and a detailed task description. "
    "The target agent works asynchronously - keep doing independent work. "
    "Use `check_task_status` / `get_task_result` when you need the outcome, "
    "not as the next action after spawning."
)


class Agent(Base):
    """A named, configured AI agent belonging to a user or the company."""

    __tablename__ = "agents"
    # ``user_id``, ``template_slug``, ``slug`` and ``web_token`` are indexed
    # inline. ``enabled`` supports active-agent counts/routing; the
    # company-managed flag is filtered on by every list endpoint's three-tier
    # visibility query.
    __table_args__ = (
        Index("ix_agents_enabled", "enabled"),
        Index("ix_agents_is_company_managed", "is_company_managed"),
        # Supports the per-user grant lookup in ``user_visible_filter``,
        # mirroring ``ix_integrations_shared_group_ids_jsonb_gin``. Agents are
        # resolved on every chat message, picker render and catalog query, so
        # this predicate is the hot one; the other shared entities are looked
        # up rarely enough that a sequential scan over a small config table
        # costs less than the index maintenance would.
        Index(
            "ix_agents_shared_user_ids_jsonb_gin",
            text("(shared_user_ids::jsonb)"),
            postgresql_using="gin",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # The owning user — ALWAYS set (the creator, or the admin for seeded
    # agents). It is NOT nulled when the agent becomes company-managed; the
    # owner is preserved for provenance and per-user resolution (e.g. template
    # integration bindings). (Column stays nullable only to avoid a NOT NULL
    # backfill on legacy rows.)
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )

    # Company-managed agents are shared with all users; only admins can edit
    # them. This is purely a visibility/permission flag — it does NOT affect
    # ``user_id`` (the owner is always preserved).
    is_company_managed: Mapped[bool] = mapped_column(Boolean, default=False)

    # A sandbox copy the Platform Health experiments on (#8255): hidden from every
    # library, picker, analytics view and map, and every run of it has its
    # writes intercepted (``services/health/sandbox``).
    is_sandbox: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")

    # Hide this company-managed agent's runs, traces, work items and shared
    # state from non-administrators (#11624, ``services/activity_visibility``).
    # Off by default; an administrator sets it; a duplicate never copies it.
    hide_activity_from_non_admins: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )

    # The customer layer of a built-in agent (#914,
    # ``services/builtin_customer_layer``): organization notes appended after
    # the seeded persona, and the customer-layer fields a person set, which the
    # seed leaves as they are on every boot. NULL on every other agent.
    customer_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    customer_fields: Mapped[list | None] = mapped_column(
        JSON(none_as_null=True), nullable=True, default=None
    )

    # Group sharing: list of Group UUIDs this entity is shared with.
    # Members of these groups can see (but not edit) the entity.
    # NULL or empty = not shared with any group (private or company-managed).
    shared_group_ids: Mapped[list | None] = mapped_column(
        JSON(none_as_null=True), nullable=True, default=None
    )
    shared_group_permissions: Mapped[dict | None] = mapped_column(
        JSON(none_as_null=True), nullable=True, default=None
    )

    # ---- Per-user sharing ------------------------------------------
    # The same shape as the group columns above, keyed by User UUID instead of
    # Group UUID, so "share with Alice" is expressible without inventing a
    # two-person group. Both principal kinds are unioned in
    # ``app.api.v1.helpers.user_visible_filter`` and ranked together in
    # ``get_effective_permission``, so a user grant is never a weaker concept
    # than a group grant.
    #
    # These are JSON id arrays rather than a join table, deliberately: the
    # visibility helper is generic over every entity in ``VisibilityQueryable``
    # and resolves in a single indexed JSONB predicate with no join, which is
    # what lets ~200 call sites inherit per-user sharing unchanged. The cost is
    # that a deleted user leaves an id behind, so ``users.delete_user`` prunes
    # grants explicitly — a stale UUID grants nothing (UUIDs are never reused)
    # but would otherwise linger in the sharing UI as a ghost row.
    shared_user_ids: Mapped[list | None] = mapped_column(
        JSON(none_as_null=True), nullable=True, default=None
    )
    shared_user_permissions: Mapped[dict | None] = mapped_column(
        JSON(none_as_null=True), nullable=True, default=None
    )

    # Restricts a company-managed entity to its explicit grants.
    #
    # ``is_company_managed`` normally means "visible to every user". When this
    # flag is set that blanket clause is dropped, so visibility falls back to
    # owner + group grants + user grants — letting an admin publish a company
    # agent to just the HR group without exposing it platform-wide.
    #
    # It is a no-op on a personal entity, whose visibility is already grant-
    # based; the API rejects setting it without ``is_company_managed`` rather
    # than storing a flag that silently does nothing.
    access_restricted: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false", default=False
    )

    # Built-in agents are seeded at startup and cannot be modified by anyone
    # (not even admins). Their config is fully synced on every restart.
    is_builtin: Mapped[bool] = mapped_column(Boolean, default=False)

    # Template reference (optional – agent may be created from scratch)
    template_slug: Mapped[str | None] = mapped_column(
        String(100), nullable=True, index=True
    )

    # Display
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    slug: Mapped[str | None] = mapped_column(
        String(200), nullable=True, unique=True, index=True
    )
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    icon: Mapped[str] = mapped_column(
        String(50), nullable=False, default="Bot"
    )  # Lucide icon name

    # Persona – the system prompt that defines who the agent is and how it behaves.
    # This is where the user describes their role, preferences, communication style.
    # e.g. "You are the assistant of Jane Doe, Engineering Manager at Acme Corp..."
    persona: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Output format – the expected format for agent responses.
    # Valid values: "markdown" (default), "html", "json", "plain_text".
    # When the agent is reached via an external channel (Slack, Teams, etc.),
    # the format is overridden to match the channel's native formatting.
    output_format: Mapped[str] = mapped_column(
        String(20), nullable=False, default="markdown"
    )

    # Optional JSON Schema for structured output when output_format is "json".
    # When set, the schema is injected into the system prompt and used for
    # response validation (Schema-Aligned Parsing). Providers with native
    # structured output (OpenAI, Gemini) also use it as response_format.
    output_schema: Mapped[dict | None] = mapped_column(
        JSON, nullable=True, default=None
    )

    # Whether a caller may impose its own answer shape on this agent for one
    # call (``output_schema`` of consult_agent / spawn_background_task). Off by
    # default: the agent answers in its configured format and such a call is
    # refused before the agent runs (``services.output_format``
    # ``caller_output_format_refusal``).
    accept_caller_output_format: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )

    # Enabled / disabled
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)

    # Integration bindings: maps provider keys → lists of Integration UUIDs
    # e.g. {"microsoft": ["550e8400-..."], "linkedin": ["660e8400-...", "770e8400-..."]}
    integration_bindings: Mapped[dict | None] = mapped_column(
        JSON, nullable=True, default=dict
    )

    # The owner's personal integrations this agent LENDS to the people it is
    # shared with: used in their chats with this agent only, under its persona,
    # guardrails and tool scoping; never listed as an integration of theirs,
    # never bindable elsewhere, the credential never shown. Unlike a share of
    # the integration itself (``Integration.shared_group_ids``), nothing is
    # granted outside this agent's runs.
    # {"integration_ids": [...], "set_by": "<user id>", "set_at": "<iso>"};
    # NULL = lends nothing. ``integration_credentials.lent_integration_ids``
    # reads it, the agent update route writes it.
    integration_lending: Mapped[dict | None] = mapped_column(
        JSON, nullable=True, default=None
    )

    # Integration template bindings: list of provider keys for per-user
    # resolution at runtime.  Instead of a fixed Integration UUID, the system
    # looks up the current user's connected integration for each provider.
    # Used by pre-packaged agents so every user gets their own credentials.
    # e.g. ["opencti", "openaev"]
    integration_template_bindings: Mapped[list | None] = mapped_column(
        JSON, nullable=True, default=list
    )

    # MCP server bindings: list of MCP server UUIDs this agent can use
    mcp_server_ids: Mapped[list | None] = mapped_column(
        JSON, nullable=True, default=list
    )

    # Tool-level scoping: restrict which tools are available per source.
    # integration_allowed_tools: maps provider key → list of tool names.
    #   e.g. {"microsoft": ["list_microsoft_emails", "search_microsoft_emails"], "github": null}
    #   Missing key or null value means ALL tools from that integration are available.
    #   A "<provider>@<integration id>" key scopes ONE integration (OpenAPI:
    #   each integration is its own API) — see filtering.instance_scope_key.
    # mcp_allowed_tools: maps MCP server UUID → list of tool names.
    #   Same semantics: missing or null = all tools.
    integration_allowed_tools: Mapped[dict | None] = mapped_column(
        JSON, nullable=True, default=None
    )
    mcp_allowed_tools: Mapped[dict | None] = mapped_column(
        JSON, nullable=True, default=None
    )
    # builtin_allowed_tools: maps built-in tool group id (an entry of
    #   builtin_tool_ids, e.g. "agent_builder") → list of function names.
    #   Same semantics: missing or null = every function of the group; a
    #   ``core`` function is never removed (filtering.filter_builtin_tools).
    builtin_allowed_tools: Mapped[dict | None] = mapped_column(
        JSON, nullable=True, default=None
    )

    # Tool deny list: explicitly block specific tools regardless of allow lists.
    # Deny takes precedence over allow — use to block dangerous operations
    # (e.g. delete endpoints, admin actions) even when an integration is fully enabled.
    # Format: list of tool name strings (applies across all sources: integrations,
    # MCP servers, custom tools, and built-in tools).
    # NULL / empty = no denials.
    tool_deny_list: Mapped[list | None] = mapped_column(
        JSON, nullable=True, default=None
    )

    # Read-only integration bindings: maps provider key → the bound
    # Integration UUIDs whose WRITE tools are withheld and refused at execution
    # time (``services/integration_read_only.py``, classified by
    # ``services/tool_classification.py``). Same shape as
    # ``integration_bindings``; only ids that are also bound there are kept.
    # NULL / empty = every binding keeps its full toolbox.
    integration_read_only: Mapped[dict | None] = mapped_column(
        JSON(none_as_null=True), nullable=True, default=None
    )
    # Read-only template bindings: provider keys of
    # ``integration_template_bindings`` whose integration, resolved per user at
    # run time, is read-only like an ``integration_read_only`` binding (its
    # write tools withheld and refused). Only template-bound providers are kept.
    # NULL / empty = every template binding keeps its full toolbox.
    integration_template_read_only: Mapped[list | None] = mapped_column(
        JSON(none_as_null=True), nullable=True, default=None
    )

    # Email signature overrides per binding: maps a bound email-sending
    # Integration UUID → {"signature_mode": "inherit" | "on" | "off",
    # "signature_integration_id": "<UUID>"} (``services/email_signature_settings``).
    # Only bound integrations keep an entry. NULL / empty = every binding
    # inherits its integration's Email signature setting.
    integration_signatures: Mapped[dict | None] = mapped_column(
        JSON(none_as_null=True), nullable=True, default=None
    )

    # OpenCTI Live writes overrides per binding: maps a bound OpenCTI
    # Integration UUID (or the provider key "opencti" of a template binding)
    # -> "on" (Live) | "off" (Draft only)
    # (``services/integration_live_writes``). Only bound keys are kept.
    # NULL / empty = every binding inherits its integration's Live writes.
    integration_live_writes: Mapped[dict | None] = mapped_column(
        JSON(none_as_null=True), nullable=True, default=None
    )

    # Browser profiles: the saved sign-ins this agent's ``browser``
    # tool may open, as Browser profile UUIDs. A run opens one only when the
    # person it acts for may use it (``services/browser_profiles/agent_access.py``).
    # ``browser_profile_read_only`` lists the bound ones opened read-only (no
    # click, type, key press or script); only ids also in
    # ``browser_profile_ids`` are kept. NULL / empty = no profile.
    browser_profile_ids: Mapped[list | None] = mapped_column(
        JSON(none_as_null=True), nullable=True, default=None
    )
    browser_profile_read_only: Mapped[list | None] = mapped_column(
        JSON(none_as_null=True), nullable=True, default=None
    )

    # Integration element bindings: scope this agent to specific sub-elements
    # within integrations (e.g. GitHub orgs/repos, Teams channels, Slack channels).
    # Maps "{provider}:{element_type}" → list of selected element objects.
    # Each element has: id, name, group, metadata (provider-specific).
    # Example: {"github:organization": [{"id": "filigran", "name": "Filigran", ...}]}
    # NULL = no element binding (agent operates on all elements, default).
    # Assignments can refine (narrow) from the agent-level selection.
    integration_element_bindings: Mapped[dict | None] = mapped_column(
        JSON, nullable=True, default=None
    )
    # The element types whose selection above is a hard boundary on the
    # integration tools rather than prompt guidance: the "Enforce as a
    # boundary" switch of the Scope(s) card. A list of element-binding keys a
    # provider enforces (``services.element_scope.enforceable_scope_keys``),
    # kept only while that type is selected.
    # NULL = every selection is guidance (default, and every existing row).
    integration_element_enforcement: Mapped[list | None] = mapped_column(
        JSON, nullable=True, default=None
    )

    @staticmethod
    def element_binding_provider(key: str) -> str:
        """Extract provider from element-binding key in format "{provider}:{type}"."""
        return key.partition(":")[0]

    @property
    def active_integration_providers(self) -> set[str]:
        """All active integration providers from fixed + template bindings."""
        providers = set((self.integration_bindings or {}).keys())
        providers.update(self.integration_template_bindings or [])
        return providers

    def prune_orphaned_integration_scopes(self) -> None:
        """Drop integration scope/tool entries for providers no longer active.

        A per-integration tool scope (``"<provider>@<integration id>"``) is
        kept only while that integration is still bound.
        """
        remaining = self.active_integration_providers
        bound_ids = {
            str(integration_id)
            for ids in (self.integration_bindings or {}).values()
            for integration_id in (ids if isinstance(ids, list) else [ids])
        }

        def _scope_active(key: str) -> bool:
            provider, sep, integration_id = key.partition("@")
            if not sep:
                return key in remaining
            return provider in remaining and integration_id in bound_ids

        if self.integration_element_bindings:
            self.integration_element_bindings = {
                k: v
                for k, v in self.integration_element_bindings.items()
                if self.element_binding_provider(k) in remaining
            } or None
        self.prune_orphaned_element_enforcement()

        if self.integration_allowed_tools:
            self.integration_allowed_tools = {
                k: v
                for k, v in self.integration_allowed_tools.items()
                if _scope_active(k)
            } or None
        self.prune_orphaned_integration_read_only()
        self.prune_orphaned_integration_template_read_only()
        self.prune_orphaned_integration_signatures()
        self.prune_orphaned_integration_live_writes()

    def prune_orphaned_integration_live_writes(self) -> None:
        """Keep the Live writes overrides of OpenCTI bindings still bound."""
        if not self.integration_live_writes:
            return
        from app.services.integration_live_writes import pruned_live_writes

        kept = pruned_live_writes(
            self.integration_live_writes,
            self.integration_bindings,
            self.integration_template_bindings,
        )
        if kept != self.integration_live_writes:
            self.integration_live_writes = kept

    def prune_orphaned_integration_signatures(self) -> None:
        """Keep the signature overrides of integrations still bound to the agent."""
        if not self.integration_signatures:
            return
        from app.services.email_signature_settings import pruned_signatures

        kept = pruned_signatures(self.integration_signatures, self.integration_bindings)
        if kept != self.integration_signatures:
            self.integration_signatures = kept

    def prune_orphaned_integration_template_read_only(self) -> None:
        """Keep the read-only providers still bound by template."""
        if not self.integration_template_read_only:
            return
        templated = set(self.integration_template_bindings or [])
        kept = [p for p in self.integration_template_read_only if p in templated]
        if kept != self.integration_template_read_only:
            self.integration_template_read_only = kept or None

    def prune_orphaned_integration_read_only(self) -> None:
        """Keep the read-only entries of integrations still bound to the agent."""
        if not self.integration_read_only:
            return
        bound = {
            (provider, str(iid))
            for provider, ids in (self.integration_bindings or {}).items()
            for iid in (ids if isinstance(ids, list) else [ids])
        }
        kept: dict[str, list[str]] = {}
        for provider, ids in self.integration_read_only.items():
            still = [
                str(iid)
                for iid in (ids if isinstance(ids, list) else [ids])
                if (provider, str(iid)) in bound
            ]
            if still:
                kept[provider] = still
        if kept != self.integration_read_only:
            self.integration_read_only = kept or None

    def prune_orphaned_element_enforcement(self) -> None:
        """Keep the enforcement flags of the element types still selected."""
        from app.services.element_scope import normalize_scope_enforcement

        kept = normalize_scope_enforcement(
            self.integration_element_enforcement, self.integration_element_bindings
        )
        if kept != self.integration_element_enforcement:
            self.integration_element_enforcement = kept

    # Custom tool bindings: list of CustomTool UUIDs this agent can use
    custom_tool_ids: Mapped[list | None] = mapped_column(
        JSON, nullable=True, default=list
    )

    # Built-in tool group ids enabled for this agent: the groups of
    # ``BUILTIN_TOOL_GROUPS`` (``services/builtin_tool_groups.py``); a
    # [DEPRECATED] former id (``BUILTIN_GROUP_ALIASES``) is stored as its group.
    # Empty list (default) = no built-in tools.
    builtin_tool_ids: Mapped[list | None] = mapped_column(
        JSON, nullable=True, default=list
    )

    # Runner bindings (native OS-control daemons, ``computer_use`` tools).
    # Which runners this agent may drive, as an explicit allow-list of Runner
    # UUIDs. Empty / null = the agent may use ANY runner visible to the acting
    # user (the common single-machine case). A non-empty list restricts the
    # agent to exactly those runners.
    runner_ids: Mapped[list | None] = mapped_column(JSON, nullable=True, default=list)

    # Runner label selector: when set, the agent may only target runners whose
    # ``labels`` overlap this list (e.g. ["audit"]). Combined with
    # ``runner_ids`` as an AND refinement. Empty / null = no label restriction.
    runner_labels: Mapped[list | None] = mapped_column(
        JSON, nullable=True, default=list
    )

    # Prompt bindings: list of Prompt UUIDs whose content is injected
    # into the agent's system prompt at runtime.
    prompt_ids: Mapped[list | None] = mapped_column(JSON, nullable=True, default=list)

    # Skill bindings: list of AgentSkill UUIDs whose instructions are injected
    # into the agent's system prompt at runtime (agentskills.io specification).
    skill_ids: Mapped[list | None] = mapped_column(JSON, nullable=True, default=list)

    # Per-skill load mode — maps AgentSkill UUID string → "progressive" | "always".
    # The system prompt carries a name/description catalog and bodies are
    # pulled in on demand by the ``load_skill`` tool.
    # "progressive" (the default for any bound skill with no entry here) =
    # catalog only; "always" pins the body inline exactly like the earlier
    # behaviour, for the single-critical-skill agent where a missed load would
    # be a regression.  Only keys also present in ``skill_ids`` are meaningful;
    # stale entries are ignored at resolution time and pruned on update.
    skill_load_modes: Mapped[dict | None] = mapped_column(
        JSON, nullable=True, default=None
    )

    @property
    def always_load_skill_ids(self) -> list[str]:
        """Return bound skill ids whose body is pinned inline.

        A skill is pinned when it is both bound (in ``skill_ids``) and flagged
        ``"always"`` in ``skill_load_modes``.  Stale entries for unbound skills
        are ignored.
        """
        modes = self.skill_load_modes or {}
        bound = [str(s) for s in (self.skill_ids or [])]
        return [s for s in bound if modes.get(s) == "always"]

    def prune_orphaned_skill_load_modes(self) -> None:
        """Drop load-mode entries for skills no longer bound to the agent."""
        if not self.skill_load_modes:
            return
        bound = {str(s) for s in (self.skill_ids or [])}
        self.skill_load_modes = {
            k: v for k, v in self.skill_load_modes.items() if k in bound
        } or None

    # Knowledge base bindings: list of KnowledgeBase UUIDs for RAG.
    # Every bound KB is READABLE (searchable via ``search_knowledge``).
    knowledge_base_ids: Mapped[list | None] = mapped_column(
        JSON, nullable=True, default=list
    )

    # Per-KB access permission — maps KnowledgeBase UUID string → "read" | "write".
    # Read is the default: a bound KB that has no entry here (or an explicit
    # "read") is searchable only. "write" additionally grants the agent the
    # ``store_knowledge`` tool for that KB in BOTH chat and assignment runs.
    # Only keys that are also present in ``knowledge_base_ids`` are meaningful;
    # stale entries are ignored at resolution time.
    # NULL / empty = all bound KBs are read-only (the safe default).
    knowledge_base_permissions: Mapped[dict | None] = mapped_column(
        JSON, nullable=True, default=None
    )

    @property
    def writable_knowledge_base_ids(self) -> list[str]:
        """Return bound KB ids the agent is allowed to write to.

        A KB is writable when it is both bound (in ``knowledge_base_ids``) and
        flagged ``"write"`` in ``knowledge_base_permissions``. Stale permission
        entries for unbound KBs are ignored.
        """
        perms = self.knowledge_base_permissions or {}
        bound = [str(k) for k in (self.knowledge_base_ids or [])]
        return [k for k in bound if perms.get(k) == "write"]

    def prune_orphaned_knowledge_base_permissions(self) -> None:
        """Drop permission entries for KBs no longer bound to the agent."""
        if not self.knowledge_base_permissions:
            return
        bound = {str(k) for k in (self.knowledge_base_ids or [])}
        self.knowledge_base_permissions = {
            k: v for k, v in self.knowledge_base_permissions.items() if k in bound
        } or None

    # Handover: list of Agent UUIDs this agent can delegate to during chat.
    # When set, the agent gets handover tools based on ``handover_types``.
    # Prevents loops: delegated agents never receive handover tools themselves.
    handover_agent_ids: Mapped[list | None] = mapped_column(
        JSON, nullable=True, default=list
    )

    # Per-target handover type — maps agent UUID string → type.
    # Valid types: "delegate" (spawn_background_task), "consult" (consult_agent),
    # "transfer" (transfer_to_agent).
    # When a target agent has no entry, it defaults to "delegate".
    handover_types: Mapped[dict | None] = mapped_column(
        JSON, nullable=True, default=dict
    )

    # Per-target routing condition for mandatory_transfer handovers.
    # Maps agent UUID string → natural language condition string.
    # When multiple mandatory_transfer targets exist, conditions are
    # evaluated by a lightweight LLM call after the agent loop completes.
    # First matching condition wins (order = handover_agent_ids order).
    # A target with no condition (or empty string) acts as the fallback.
    handover_conditions: Mapped[dict | None] = mapped_column(
        JSON, nullable=True, default=dict
    )

    # User-editable prompt that instructs the LLM when/how to delegate to peer agents.
    handover_prompt: Mapped[str] = mapped_column(
        Text, nullable=False, default=DEFAULT_AGENT_HANDOVER_PROMPT
    )

    # Self-fan-out: whether this agent may spawn background copies of ITSELF
    # (``spawn_background_task(agent_id="self")``) to split a large workload.
    # On by default; off removes only the self target - peer delegation keeps
    # working. The platform switch ``AGENT_SELF_FANOUT_ENABLED`` still wins
    # (``builtin_tools.self_fanout_enabled``). Read with ``is not False``: a
    # column the startup safety net added bare holds NULL, which means on.
    allow_self_fanout: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=text("true")
    )

    # Work-item peer roster: the agents this agent may assign work items
    # to (the ``owner_agent_id`` of a work item it creates). DISTINCT from
    # handover — handover is synchronous/in-conversation delegation, while a
    # work item is durable, queued, asynchronous delegation; an agent may have
    # different peers for each. Configured on the "Work Items" built-in tool.
    # The roster is surfaced to the LLM in the create_work_item tool description
    # (so the agent is AWARE of which peers exist) and create_work_item rejects
    # an owner that is not in it. NULL / empty = no roster: ``owner_agent_id`` is
    # not validated (back-compatible) - the agent may set any owner, or leave it
    # empty to keep the item in its own backlog.
    work_item_owner_agent_ids: Mapped[list | None] = mapped_column(
        JSON, nullable=True, default=list
    )

    # Trace supervision allow-list: target agent IDs this agent may inspect via
    # ``inspect_agent_traces`` when the acting user is not an admin.
    # NULL / empty = no non-admin supervision targets are allowed.
    trace_supervision_target_agent_ids: Mapped[list | None] = mapped_column(
        JSON, nullable=True, default=list
    )

    # Platform Health (#8254, ``services/health/``). The improvement
    # opt-in: whoever may manage the agent lets Platform Health read its
    # memories and conversation content (``consent.py``). Off by default.
    health_enrolled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    # Platform Health content opt-in (dev-docs/platform-health.md 1.2): the
    # second, separate opt-in inside an enrolled agent that lets the analysis
    # layer read its content. Carried from the former improvement opt-in by
    # migration h118health01; off by default.
    health_content_opt_in: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    # The agent's role in the Platform Health fleet, which makes its runs act as the
    # Platform Health identity (``identity.py``). Written by the seed alone, never by
    # a REST route or an agent tool. NULL for every other agent.
    health_role: Mapped[str | None] = mapped_column(String(32), nullable=True)

    # Coordination access: which AGENTS may read and write the company-managed
    # work items this agent creates and the company-managed shared state it
    # owns - ``{"read": "all" | "agents" | "self", "write": ..., "read_agent_ids":
    # [...], "write_agent_ids": [...]}`` in the form
    # ``services/company_managed_writes.normalize_coordination_access`` stores.
    # NULL = the default: everyone reads them, every company-managed agent
    # writes them. It only ever narrows that rule, and is read live on every
    # access, so a change applies to the rows the agent already governs.
    coordination_access: Mapped[dict | None] = mapped_column(
        JSON(none_as_null=True), nullable=True, default=None
    )

    # Tags – free-form labels for filtering and organisation
    tags: Mapped[list | None] = mapped_column(JSON, nullable=True, default=list)

    # Intents this agent can fulfill (e.g. ["opencti.report.summarize", ...]).
    # Used for pre-filtering in intent binding UI; agents with no intents
    # are not eligible for intent routing.
    supported_intents: Mapped[list | None] = mapped_column(
        JSON, nullable=True, default=list
    )

    # Business verticals this agent is qualified for (e.g. ["fimi", "cti"]).
    # NULL or empty = universal (eligible as fallback for any vertical).
    supported_verticals: Mapped[list | None] = mapped_column(
        JSON, nullable=True, default=list
    )

    # Agent-specific config (free-form JSON)
    config: Mapped[dict | None] = mapped_column(JSON, nullable=True, default=dict)

    # LLM override (falls back to user default if null)
    llm_provider: Mapped[str | None] = mapped_column(String(50), nullable=True)
    llm_provider_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    llm_model: Mapped[str | None] = mapped_column(String(100), nullable=True)
    # Where the model comes from. NULL = the fields above, then the
    # usual fallbacks. "routing" = the platform's lightweight routing model
    # (``PlatformSettings.routing_model`` / ``routing_provider_id``), for an
    # agent that takes a narrow, repeated decision a small model handles well;
    # it is what a built-in agent can declare, since a model id written in a
    # seed exists only on some deployments. Resolved in ONE place:
    # ``app.llm.agent_model.agent_model_selection``.
    llm_model_source: Mapped[str | None] = mapped_column(String(16), nullable=True)

    # LLM temperature override (0.0–2.0). NULL = use provider default.
    temperature: Mapped[float | None] = mapped_column(nullable=True)

    # Reasoning effort override for thinking/reasoning models.
    # Values: "low", "medium", "high", "max" (max is Anthropic Opus 4.6 only).
    # NULL = use platform default or no reasoning effort.
    reasoning_effort: Mapped[str | None] = mapped_column(String(20), nullable=True)

    # Organization summary in the system prompt (#11533): "inherit" follows
    # the platform setting, "always" or "off" overrides it; an assignment's
    # own explicit mode wins over this one
    # (``services/organization_context_injection.py``).
    organization_context_mode: Mapped[str] = mapped_column(
        String(10), nullable=False, default="inherit", server_default="inherit"
    )
    # Categories preloaded after the basic block (#11533): NULL inherits the
    # platform's list, a list (empty included) wins; an assignment's own
    # explicit list wins over this one.
    organization_context_categories: Mapped[list[str] | None] = mapped_column(JSON, nullable=True, default=None)

    # Max iterations for the agentic loop (chat & assignment).
    # Controls how many LLM ↔ tool-calling rounds the agent may perform
    # before being forced to synthesise a final answer.
    # NULL = platform default (10 for chat, assignment has its own field).
    max_iterations: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Per-agent override of the ``wait`` policy:
    # ``{per_call_seconds, total_seconds, unbounded_with_until}``, any subset.
    # NULL = the platform's (Settings > Agents and automations > Agent runs). Bounded by the
    # platform values; an assignment's own ``wait_policy`` wins over it
    # (``app.tools.wait_policy.resolve_wait_policy``).
    wait_policy: Mapped[dict | None] = mapped_column(JSON(none_as_null=True), nullable=True, default=None)

    # Per-agent run budget — the Budget tab.
    #
    # A PARTIAL override of the platform's own budget configuration
    # (``AGENT_LOOP_MAX_*``), shaped exactly like
    # ``app.services.budget_policy.BudgetOverrides.to_dict()``: only the keys
    # this agent actually pins, so an agent that cares about money alone sets
    # ``max_cost_usd`` and keeps inheriting the platform's token / tool-call /
    # duration limits instead of restating (and then drifting from) all four.
    # NULL or {} = no override at all, i.e. byte-identical behaviour to before
    # this column existed.
    #
    # JSON rather than four columns on purpose: the shape is one cohesive
    # policy object that the flow builder's Budget node and the sub-flow
    # propagation carry around verbatim, and it is read as a whole or not at
    # all — never filtered or aggregated on, so nothing here wants an index.
    #
    # Authority lives at the API edge, NOT here: a value above the platform
    # ceiling can only be written by an admin
    # (``budget_policy.ceiling_violations``), which is what makes the env
    # setting a budget a user cannot exceed on their own.
    budget_config: Mapped[dict | None] = mapped_column(
        JSON(none_as_null=True), nullable=True, default=None
    )

    # Guardrails – safety constraints always injected into the system prompt,
    # separate from the persona so they can't be accidentally overwritten.
    guardrails: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Pipeline: prepare → [LLM?] → finalize
    #
    # Optional tool references that run before and after the LLM loop.
    # Each ref is a JSON object identifying a tool already bound to the agent:
    #   {"source": "custom_tool"|"integration"|"mcp",
    #    "tool_id"|"server_id": "<uuid>", "provider"?: "<str>",
    #    "tool_name": "<str>"}
    #
    # v1: only no-parameter tools are supported.
    # - prepare runs with no arguments; its return value is prepended to
    #   the LLM context (or becomes the pipeline output when skip_llm=True).
    # - finalize receives the LLM response (or prepare output) as a raw
    #   string; its return value replaces the final response.
    prepare_tool_ref: Mapped[dict | None] = mapped_column(
        JSON, nullable=True, default=None
    )
    finalize_tool_ref: Mapped[dict | None] = mapped_column(
        JSON, nullable=True, default=None
    )

    # When True, skip the LLM agentic loop entirely — prepare output flows
    # directly to finalize (or becomes the response if no finalize is set).
    skip_llm: Mapped[bool] = mapped_column(Boolean, default=False)

    # Memory – three independently toggleable tiers.  All default to True.
    #
    # Short-term  (Redis, 24 h rolling 10 entries) – immediate context.
    # Medium-term (Redis, 30 d LLM-consolidated summary) – accumulated knowledge.
    # Persistent  (PostgreSQL + pgvector, durable) – extracted facts, style
    #             notes, and semantic-searchable knowledge.
    memory_short_term_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    memory_medium_term_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    memory_persistent_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # User memory – cross-agent personalization (Redis, per-user).
    # When disabled the agent neither loads nor stores user memory,
    # allowing fully stateless / memoryless chat sessions.
    memory_user_enabled: Mapped[bool] = mapped_column(Boolean, default=True)

    # --- Human in the loop (agent-level default) ---
    # Agent-level HITL default.  Assignments inherit this value unless they
    # explicitly override it.  False by default everywhere.
    human_in_the_loop: Mapped[bool] = mapped_column(Boolean, default=False)
    human_in_the_loop_prompt: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Per-tool approval gate: pause before any tool the HITL whitelist
    # does not exempt. Separate from ``human_in_the_loop`` above, which keeps
    # its prompt-driven meaning. NULL follows the install-wide
    # ``platform_settings.approve_tool_calls_default``.
    approve_tool_calls: Mapped[bool | None] = mapped_column(Boolean, nullable=True)

    # --- Run visibility (agent-level default) ---
    # When True, runs of this *company-managed* agent are hidden from OTHER
    # users: each run is visible only to the user it executed as, instead of the
    # default company-managed behavior where every run is visible to everyone.
    # Realized via the per-run ``AssignmentRun.is_company_managed`` flag stamped
    # at run creation: a hidden run is born ``is_company_managed=False``, so the
    # standard visibility filters (``run_visible_filter`` / ``require_run_access``)
    # scope it to the executing user ALONE (its ``user_id``) - admins get no
    # special access to another user's hidden run, since admin run access is gated
    # on ``is_company_managed=True``. No effect on personal agents (their runs are
    # already private). Assignments may override.
    hide_runs: Mapped[bool] = mapped_column(Boolean, default=False)

    # Public catalog – when True, the agent is listed in the Filigran AI Catalog.
    # Only company-managed agents can be published (is_company_managed must be True).
    is_public_catalog: Mapped[bool] = mapped_column(Boolean, default=False)

    # Whether the catalog download carries the code of the custom tools bound
    # to this agent that are not company-managed. The publisher's
    # choice, off for a new publish; agents already published when the column
    # was added were set to True, their behaviour until then.
    catalog_include_personal_tools: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false")
    )

    @property
    def catalog_download_includes_personal_tools(self) -> bool:
        """The switch as the download reads it. NULL is only possible when the
        startup safety net created the column bare before its migration ran,
        so it never went through the backfill: it keeps today's behaviour."""
        return self.catalog_include_personal_tools is not False

    # Chat / LLM proxy suitability – when True, the agent is hidden from the LLM
    # proxy ``/v1/models`` listing and the profile page's available agents section,
    # and appears greyed-out in the chat agent picker (still selectable with a
    # warning).  Useful for agents that only make sense as assignment engines.
    disable_chat: Mapped[bool] = mapped_column(Boolean, default=False)

    # Channel availability — which messaging channels can route conversations to
    # this agent.  NULL = all channels (legacy agents).  An explicit list restricts
    # to only those channels (e.g. ["web", "slack"]).
    # New agents default to ["web"] (web chat only) — external channels must be
    # explicitly enabled.  The default is applied in CreateAgentRequest, not here,
    # so that existing NULL rows keep their "all channels" semantics.
    # Valid values: "web", "slack", "teams", "whatsapp", "telegram".
    # NOTE: For non-web channels this is now derived from channel_config on save.
    available_channel_types: Mapped[list | None] = mapped_column(
        JSON, nullable=True, default=None
    )

    # Unified channel/bot configuration — maps channel type to its config.
    # Keys: "slack", "teams", "whatsapp", "telegram".
    # Each value: {
    #   "integration_id": "uuid",       # bot integration from Settings
    #   "receive_enabled": bool,         # agent responds to incoming messages
    #   "publish_enabled": bool,         # agent can send via bot API tools
    #   "receive_channels": [...],       # scope for receive (empty = all)
    #   "publish_channels": [...],       # scope for publish (empty = all)
    #   "allowed_tools": [...] | null,   # tool-level scoping (null = all)
    # }
    # NULL = no channel/bot configuration.
    channel_config: Mapped[dict | None] = mapped_column(
        JSON, nullable=True, default=None
    )

    # Web exposure – when enabled, the agent is accessible via a public embed widget.
    # web_token is a unique secret used to identify the agent in public URLs (avoids
    # exposing the internal UUID).  Auto-generated the first time web is enabled.
    web_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    web_token: Mapped[str | None] = mapped_column(
        String(64), nullable=True, unique=True, index=True
    )

    # Suggested prompts displayed in the web widget greeting screen.
    # A JSON list of short prompt strings that visitors can click to start a conversation.
    # Example: ["What does your product do?", "How can I get started?"]
    web_suggested_prompts: Mapped[list | None] = mapped_column(
        JSON, nullable=True, default=list
    )

    # Whether a visitor of the public widget may attach a file to a message
    # (``services/widget_uploads.py``). Off for every widget until its
    # owner turns it on; the Security Audit reports how many allow it.
    web_uploads_enabled: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false")
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )
