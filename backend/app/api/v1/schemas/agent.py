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

"""Pydantic schemas for agent endpoints."""

import json
from typing import Annotated, Literal

from pydantic import BaseModel, Field, StringConstraints, field_validator, model_validator

from app.api.v1.schemas.column_limits import column_length_validator, lone_surrogate_validator
from app.llm.agent_model import LLM_MODEL_SOURCE_ROUTING
from app.models.agent import Agent
from app.models.agent_template import AgentTemplate
from app.services.element_scope import parse_scope_enforcement

#: The agent columns a create or update writes as sent. ``slug`` is bounded by
#: ``validate_slug`` (a 400, after stripping), and ``template_slug`` only looks
#: a template up (a 404 for a slug no template has).
_AGENT_COLUMN_FIELDS = ("name", "icon", "output_format", "llm_provider", "llm_model", "reasoning_effort")
_TEMPLATE_COLUMN_FIELDS = ("name", "icon", "category", "default_output_format")
#: The agent's own model, which an agent on the routing model keeps empty.
_OWN_MODEL_FIELDS = ("llm_provider", "llm_provider_id", "llm_model")


class AgentBudgetConfig(BaseModel):
    """A per-agent run-budget override. Every field is optional.

    ``None`` on a field means "inherit" — the platform value
    (``AGENT_LOOP_MAX_*``) applies, or a nearer layer in a flow does. That is
    why this is a partial object and not four required numbers: an agent that
    only cares about money pins ``max_cost_usd`` and keeps tracking the
    platform's other limits instead of freezing a copy of them.

    ``0`` is accepted as a synonym for "no limit" (it is what the platform
    settings use) and normalised to ``None`` so the stored object never holds a
    limit that means the opposite of what it looks like.

    Whether a submitted value is ALLOWED is not decided here — a limit above
    the platform ceiling needs an admin, which depends on the caller rather
    than the value. See ``app.services.budget_policy.ceiling_violations`` and
    the agents endpoints that call it.
    """

    max_cost_usd: float | None = Field(default=None, ge=0)
    max_total_tokens: int | None = Field(default=None, ge=0)
    max_tool_calls: int | None = Field(default=None, ge=0)
    max_duration_seconds: int | None = Field(default=None, ge=0)
    final_answer_reserve: float | None = Field(default=None, ge=0, le=0.5)
    fallback_enabled: bool | None = None
    fallback_model: str | None = None

    @field_validator(
        "max_cost_usd",
        "max_total_tokens",
        "max_tool_calls",
        "max_duration_seconds",
    )
    @classmethod
    def _zero_is_unset(cls, v: float | int | None) -> float | int | None:
        return None if v is not None and v <= 0 else v

    @field_validator("fallback_model")
    @classmethod
    def _blank_is_unset(cls, v: str | None) -> str | None:
        return (v or "").strip() or None


class AgentCoordinationAccess(BaseModel):
    """Which agents read and write the company-managed work items this agent
    creates and the company-managed shared state it owns.

    ``all`` (the default for both) is the always-on rule: everyone reads them,
    every company-managed agent writes them. ``agents`` narrows to this agent
    plus the listed company-managed agents, ``self`` to this agent alone.
    Writing implies reading; ``null`` / both ``all`` stores nothing. The
    agents endpoints store it through
    ``services/company_managed_writes.normalize_coordination_access``.
    """

    read: Literal["all", "agents", "self"] = "all"
    write: Literal["all", "agents", "self"] = "all"
    read_agent_ids: list[str] = Field(default_factory=list)
    write_agent_ids: list[str] = Field(default_factory=list)


class BudgetPolicyResponse(BaseModel):
    """What the platform bounds, and whether the caller may go above it.

    Served by ``GET /agents/budget-policy``. The Budget tab renders each empty
    field as "inherits <ceiling>" from this, and hides (or badges) the ability
    to raise a limit based on ``can_exceed_ceiling`` — so a non-admin learns
    the rule from the form instead of from a 403.

    ``ceilings`` values are ``None`` for a dimension the platform leaves
    unbounded. That is not the same as ``0``: unbounded means "no limit
    applies", and setting one for a single agent is a tightening any user may
    do.
    """

    can_exceed_ceiling: bool
    ceilings: dict[str, float | None]
    final_answer_reserve: float | None = None
    fallback_enabled: bool = False
    fallback_model: str | None = None


def _normalize_kb_permissions(
    v: dict[str, str] | None,
) -> dict[str, str] | None:
    """Validate ``knowledge_base_permissions`` values to the supported union.

    The whole model assumes only ``"read"`` / ``"write"`` (e.g.
    ``Agent.writable_knowledge_base_ids`` keys off ``== "write"`` and the
    frontend TS type is a ``"read" | "write"`` union). Without this guard an API
    client could persist arbitrary values (``"WRITE"``, ``"admin"``) that the
    execution path would silently treat as non-write. Case is normalized so a
    client sending ``"Write"`` round-trips correctly; anything else is rejected.
    """
    if v is None:
        return v
    normalized: dict[str, str] = {}
    for kb_id, perm in v.items():
        p = (perm or "").strip().lower()
        if p not in ("read", "write"):
            raise ValueError(
                "knowledge_base_permissions values must be 'read' or 'write' "
                f"(got {perm!r} for {kb_id!r})"
            )
        normalized[kb_id] = p
    return normalized


def _refuse_per_user_unresolvable(v: list[str] | None) -> list[str] | None:
    """Refuse per-user (template) bindings no integration can satisfy.

    Every OpenAPI integration is a different API, so "each user's own OpenAPI
    integration" names no API in particular; the runtime never resolves one,
    and accepting it would leave the agent silently without those tools.
    """
    from app.services.openapi_import.service import per_user_binding_error

    error = per_user_binding_error(v)
    if error:
        raise ValueError(error)
    return v


def _normalize_skill_load_modes(
    v: dict[str, str] | None,
) -> dict[str, str] | None:
    """Validate ``skill_load_modes`` values to the supported union.

    Same contract as :func:`_normalize_kb_permissions`: the runtime keys off
    ``== "always"`` (``Agent.always_load_skill_ids``) and the frontend type is
    a ``"progressive" | "always"`` union, so an arbitrary value would be
    silently read as "progressive" — i.e. a user asking to pin a skill inline
    would get catalog-only behaviour with no error. Case is normalized so
    ``"Always"`` round-trips; anything else is rejected.
    """
    if v is None:
        return v
    normalized: dict[str, str] = {}
    for skill_id, mode in v.items():
        m = (mode or "").strip().lower()
        if m not in ("progressive", "always"):
            raise ValueError(
                "skill_load_modes values must be 'progressive' or 'always' "
                f"(got {mode!r} for {skill_id!r})"
            )
        normalized[skill_id] = m
    return normalized


def _normalize_integration_read_only(
    v: dict[str, list[str]] | None,
) -> dict[str, list[str]] | None:
    """Canonical ``{provider: [integration_id]}`` for ``integration_read_only``."""
    if v is None:
        return v
    from app.services.integration_read_only import parse_integration_read_only

    value, error, _provided = parse_integration_read_only(v)
    if error:
        raise ValueError(error)
    return value


def _normalize_integration_template_read_only(v: list[str] | None) -> list[str] | None:
    """Canonical provider keys for ``integration_template_read_only``."""
    if v is None:
        return v
    from app.services.integration_read_only import parse_integration_template_read_only

    value, error, _provided = parse_integration_template_read_only(v)
    if error:
        raise ValueError(error)
    return value


def _normalize_integration_signatures(v: object) -> dict[str, dict[str, str]] | None:
    """Canonical ``{integration_id: {signature_mode, signature_integration_id?}}``."""
    if v is None:
        return v
    from app.services.email_signature_settings import (
        compact_signatures,
        parse_integration_signatures,
    )

    value, error, _provided = parse_integration_signatures(v)
    if error:
        raise ValueError(error)
    return compact_signatures(value)


def _normalize_integration_live_writes(v: object) -> dict[str, str] | None:
    """Canonical ``{integration_id or "opencti": "on" | "off" | "inherit"}``.

    ``inherit`` entries are kept so an update removes the override they name.
    """
    if v is None:
        return v
    from app.services.integration_live_writes import parse_integration_live_writes

    value, error, _provided = parse_integration_live_writes(v)
    if error:
        raise ValueError(error)
    return value


def _normalize_browser_profile_ids(v: list[str] | None) -> list[str] | None:
    """Canonical browser profile ids: UUIDs, deduplicated, at most ``MAX_BOUND_PROFILES``."""
    if v is None:
        return v
    from app.services.browser_profiles.agent_access import normalize_profile_ids

    return normalize_profile_ids(v) or []


class TemplateSetupRequirement(BaseModel):
    """One mandatory setup step the creation wizard renders for a template.

    Typed rather than a free dict for the same reason as
    ``_normalize_kb_permissions`` above: the wizard gates its Next button on
    these entries, so a typoed ``kind`` would be silently dropped and a typoed
    integration ``target`` would render a requirement that can never be
    satisfied — a 422 at write time beats a wedged wizard at read time.
    """

    kind: Literal["integration", "knowledge_base", "persona_field"]
    target: str = Field(min_length=1, max_length=200)
    instruction: str = Field(min_length=1, max_length=500)


#: One ``default_skills`` entry: a built-in skill slug, the ``slug:<x>`` tag
#: the seed gives every company-managed built-in skill.
TemplateSkillSlug = Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9-]*$", max_length=100)]


class AgentTemplateResponse(BaseModel):
    id: str
    slug: str
    name: str
    description: str | None = None
    icon: str
    category: str
    disable_chat: bool = False
    default_output_format: str = "markdown"
    required_integrations: list[str] | None = None
    required_integration_types: list[str] | None = None
    recommended_mcp: list[str] | None = None
    #: What the user still has to configure after creating an agent from this
    #: template — each entry ({kind, target, instruction}) drives one mandatory
    #: step of the creation wizard.
    must_be_defined: list[dict] | None = None
    default_persona: str | None = None
    #: The built-in skill slugs an agent created from this template starts
    #: with, and the ids they resolve to for the caller (the skills it may
    #: attach, in template order) - what the creation wizard preselects.
    default_skills: list[str] | None = None
    default_skill_ids: list[str] = []
    assignment_count: int = 0
    is_builtin: bool = False
    enabled: bool = True


class CreateTemplateRequest(BaseModel):
    slug: str
    name: str
    description: str | None = None
    icon: str = "Bot"
    category: str = "general"
    disable_chat: bool = False
    default_output_format: str = "markdown"
    default_persona: str | None = None
    required_integrations: list[str] | None = None
    required_integration_types: list[str] | None = None
    recommended_mcp: list[str] | None = None
    must_be_defined: list[TemplateSetupRequirement] | None = None
    default_skills: list[TemplateSkillSlug] | None = None
    default_assignments: list[dict] | None = None

    _fit_columns = column_length_validator(AgentTemplate, "slug", *_TEMPLATE_COLUMN_FIELDS)
    # ``default_assignments`` become assignments: a lone surrogate would be copied into each.
    _no_lone_surrogates = lone_surrogate_validator(AgentTemplate)


class UpdateTemplateRequest(BaseModel):
    name: str | None = None
    description: str | None = None
    icon: str | None = None
    category: str | None = None
    disable_chat: bool | None = None
    default_output_format: str | None = None
    default_persona: str | None = None
    required_integrations: list[str] | None = None
    required_integration_types: list[str] | None = None
    recommended_mcp: list[str] | None = None
    must_be_defined: list[TemplateSetupRequirement] | None = None
    default_skills: list[TemplateSkillSlug] | None = None
    default_assignments: list[dict] | None = None
    enabled: bool | None = None

    _fit_columns = column_length_validator(AgentTemplate, *_TEMPLATE_COLUMN_FIELDS)
    _no_lone_surrogates = lone_surrogate_validator(AgentTemplate)


class AgentFleetRef(BaseModel):
    """The agent's home fleet, read-only: membership is written on the fleet."""

    fleet_id: str
    fleet_name: str
    member_id: str
    is_orchestrator: bool = False
    #: Whether other fleets may hand over to it (the orchestrator always is).
    is_exposed: bool = False


class AgentResponse(BaseModel):
    id: str
    user_id: str | None = None
    owner_name: str | None = None
    owner_email: str | None = None
    slug: str | None = None
    template_slug: str | None = None
    is_builtin: bool = False
    name: str
    description: str | None = None
    icon: str
    persona: str | None = None
    output_format: str = "markdown"
    output_schema: dict | None = None
    accept_caller_output_format: bool = False
    enabled: bool
    is_company_managed: bool = False
    #: Runs, traces, work items and state hidden from non-admins (#11624).
    hide_activity_from_non_admins: bool = False
    #: A built-in's organization notes, appended after its persona (#914).
    customer_notes: str | None = None
    #: The customer-layer fields set on a built-in, kept across seed runs (#914).
    customer_fields: list[str] | None = None
    shared_group_ids: list[str] | None = None
    shared_group_permissions: dict[str, str] | None = None
    shared_user_ids: list[str] | None = None
    shared_user_permissions: dict[str, str] | None = None
    access_restricted: bool | None = None
    #: Whether the REQUESTING user may edit it - ``helpers.can_manage``, share
    #: tiers included, so the UI never offers an edit the API refuses.
    #: A built-in stays read-only in XTM One mode whatever this says.
    can_manage: bool = False
    #: Why NOBODY may edit it, administrators included, in any mode: set for an
    #: agent of the built-in Platform Health fleet (``HEALTH_READ_ONLY_REFUSAL``),
    #: ``None`` otherwise. The UI hides every write control (integrations and
    #: memory included, which an administrator may otherwise change on a
    #: built-in) and shows this reason on the Read-only badge (#101 S1b).
    read_only_reason: str | None = None
    #: Whether the REQUESTING user may read its conversations -
    #: ``helpers.can_view_agent_chats``, the rule the chats routes enforce.
    can_view_chats: bool = False
    #: Its home fleet; ``None`` is the playground.
    fleet: AgentFleetRef | None = None
    tags: list[str] = []
    integration_bindings: dict[str, list[str]] | None = None
    #: ``Agent.integration_lending``: {integration_ids, set_by, set_at}.
    integration_lending: dict | None = None
    #: The owner's personal integrations this shared agent binds that not every
    #: one of its groups and people gets otherwise ({id, name, provider}), for a
    #: viewer who may edit it: an editor cannot read the owner's rows.
    #: ``None`` on list responses and for read-only viewers.
    shared_personal_integrations: list[dict] | None = None
    #: The integrations it binds that its owner can no longer see ({id, name,
    #: provider}): its runs refuse their tools until the owner's access comes
    #: back or the binding is removed. Sent by ``GET /agents/{id}`` only;
    #: ``None`` on list responses.
    orphaned_integrations: list[dict] | None = None
    integration_template_bindings: list[str] | None = None
    mcp_server_ids: list[str] | None = None
    custom_tool_ids: list[str] | None = None
    builtin_tool_ids: list[str] = []
    # Runner (native OS-control) selector: which enrolled machines this agent's
    # ``computer_use`` tools may drive. Empty/null ids = any visible runner;
    # ``runner_labels`` narrows by label. See fleet model in dev-docs.
    runner_ids: list[str] | None = None
    runner_labels: list[str] | None = None
    integration_allowed_tools: dict[str, list[str] | None] | None = None
    integration_element_bindings: dict[str, list[dict]] | None = None
    # The element types whose selection is a boundary on the tools;
    # null = every selection is guidance.
    integration_element_enforcement: list[str] | None = None
    mcp_allowed_tools: dict[str, list[str] | None] | None = None
    # Built-in group id -> function names it keeps; missing or null = all.
    builtin_allowed_tools: dict[str, list[str] | None] | None = None
    tool_deny_list: list[str] | None = None
    # The bound integrations whose write tools are withheld.
    integration_read_only: dict[str, list[str]] | None = None
    # The template-bound providers whose resolved integration is read-only.
    integration_template_read_only: list[str] | None = None
    # Email signature overrides per bound email-sending integration.
    integration_signatures: dict[str, dict[str, str]] | None = None
    # OpenCTI Live writes overrides per binding: "on" (Live) | "off" (Draft only).
    integration_live_writes: dict[str, str] | None = None
    browser_profile_ids: list[str] | None = None
    browser_profile_read_only: list[str] | None = None
    prompt_ids: list[str] | None = None
    skill_ids: list[str] | None = None
    skill_load_modes: dict[str, str] | None = None
    knowledge_base_ids: list[str] | None = None
    knowledge_base_permissions: dict[str, str] | None = None
    handover_agent_ids: list[str] | None = None
    handover_types: dict[str, str] | None = None
    work_item_owner_agent_ids: list[str] | None = None
    trace_supervision_target_agent_ids: list[str] | None = None
    # Who reads / writes the company-managed coordination rows this agent
    # governs - see AgentCoordinationAccess. None = the default rule.
    coordination_access: AgentCoordinationAccess | None = None
    handover_conditions: dict[str, str] | None = None
    # Whether the agent may spawn background copies of itself
    # (``spawn_background_task(agent_id="self")``); the platform switch
    # AGENT_SELF_FANOUT_ENABLED still wins.
    allow_self_fanout: bool = True
    config: dict | None = None
    llm_provider: str | None = None
    llm_provider_id: str | None = None
    llm_model: str | None = None
    # "routing" = run on the platform routing model, ignoring the three fields
    # above (``app.llm.agent_model``). NULL = the agent's own fields.
    llm_model_source: Literal["routing"] | None = None
    temperature: float | None = None
    reasoning_effort: str | None = None
    # Organization summary in the system prompt (#11533): inherit | always | off.
    organization_context_mode: Literal["inherit", "always", "off"] | None = None
    # Categories preloaded after the basic block (#11533): null = inherit, a list wins.
    organization_context_categories: list[str] | None = None
    max_iterations: int | None = None
    # Per-agent ``wait`` policy override: {per_call_seconds,
    # total_seconds, unbounded_with_until}, any subset; null = inherit.
    wait_policy: dict | None = None
    # Per-agent run budget — see AgentBudgetConfig. None/{} = inherit
    # the platform configuration entirely.
    budget_config: AgentBudgetConfig | None = None
    guardrails: str | None = None
    prepare_tool_ref: dict | None = None
    finalize_tool_ref: dict | None = None
    skip_llm: bool = False
    memory_short_term_enabled: bool = True
    memory_medium_term_enabled: bool = True
    memory_persistent_enabled: bool = True
    memory_user_enabled: bool = True
    human_in_the_loop: bool = False
    human_in_the_loop_prompt: str | None = None
    # Per-tool approval gate; None follows the install default.
    approve_tool_calls: bool | None = None
    available_channel_types: list[str] | None = None
    channel_config: dict[str, dict] | None = None
    is_public_catalog: bool = False
    catalog_include_personal_tools: bool = False
    disable_chat: bool = False
    hide_runs: bool = False
    web_enabled: bool = False
    web_token: str | None = None
    web_suggested_prompts: list[str] | None = None
    web_uploads_enabled: bool = False
    assignment_count: int = 0
    chat_query_count: int = 0
    created_at: str
    updated_at: str

    model_config = {"from_attributes": True}


class DuplicateAgentResponse(AgentResponse):
    """The duplicate, plus the bindings of the original it left out because
    the copier may not use them - counted per kind, never named - and the
    reserved tags it did not carry; ``memory_entries_copied`` is the number of
    memory entries copied with ``copy_memory`` (0 without it)."""

    warnings: list[str] = []
    memory_entries_copied: int = 0


class CreateAgentRequest(BaseModel):
    id: str | None = (
        None  # Optional pre-generated UUID (frontend generates for webhook URLs)
    )
    slug: str | None = (
        None  # Optional custom slug (auto-generated from name if not provided)
    )
    template_slug: str | None = None
    name: str
    description: str | None = None
    icon: str = "Bot"
    persona: str | None = None
    output_format: str = "markdown"
    output_schema: dict | None = None
    accept_caller_output_format: bool = False
    is_company_managed: bool = False
    shared_group_ids: list[str] | None = None
    shared_group_permissions: dict[str, str] | None = None
    shared_user_ids: list[str] | None = None
    shared_user_permissions: dict[str, str] | None = None
    access_restricted: bool | None = None
    tags: list[str] | None = None
    integration_bindings: dict[str, list[str]] | None = None
    integration_template_bindings: list[str] | None = None
    mcp_server_ids: list[str] | None = None
    custom_tool_ids: list[str] | None = None
    builtin_tool_ids: list[str] | None = None
    runner_ids: list[str] | None = None
    runner_labels: list[str] | None = None
    integration_allowed_tools: dict[str, list[str] | None] | None = None
    integration_element_bindings: dict[str, list[dict]] | None = None
    # The element types whose selection is a boundary on the tools;
    # null = every selection is guidance.
    integration_element_enforcement: list[str] | None = None
    mcp_allowed_tools: dict[str, list[str] | None] | None = None
    # Built-in group id -> function names it keeps; missing or null = all.
    builtin_allowed_tools: dict[str, list[str] | None] | None = None
    tool_deny_list: list[str] | None = None
    integration_read_only: dict[str, list[str]] | None = None
    integration_template_read_only: list[str] | None = None
    integration_signatures: dict[str, dict[str, str]] | None = None
    integration_live_writes: dict[str, str] | None = None
    browser_profile_ids: list[str] | None = None
    browser_profile_read_only: list[str] | None = None
    prompt_ids: list[str] | None = None
    skill_ids: list[str] | None = None
    skill_load_modes: dict[str, str] | None = None
    knowledge_base_ids: list[str] | None = None
    knowledge_base_permissions: dict[str, str] | None = None
    handover_agent_ids: list[str] | None = None
    handover_types: dict[str, str] | None = None
    work_item_owner_agent_ids: list[str] | None = None
    trace_supervision_target_agent_ids: list[str] | None = None
    coordination_access: AgentCoordinationAccess | None = None
    handover_conditions: dict[str, str] | None = None
    allow_self_fanout: bool = True
    config: dict | None = None
    llm_provider: str | None = None
    llm_provider_id: str | None = None
    llm_model: str | None = None
    # "routing" = run on the platform routing model, ignoring the three fields
    # above (``app.llm.agent_model``). NULL = the agent's own fields.
    llm_model_source: Literal["routing"] | None = None
    temperature: float | None = None
    reasoning_effort: str | None = None
    # Organization summary in the system prompt (#11533): inherit | always | off.
    organization_context_mode: Literal["inherit", "always", "off"] | None = None
    # Categories preloaded after the basic block (#11533): null = inherit, a list wins.
    organization_context_categories: list[str] | None = None
    max_iterations: int | None = None
    # Per-agent ``wait`` policy override: {per_call_seconds,
    # total_seconds, unbounded_with_until}, any subset; null = inherit.
    wait_policy: dict | None = None
    # Per-agent run budget — see AgentBudgetConfig. None/{} = inherit
    # the platform configuration entirely.
    budget_config: AgentBudgetConfig | None = None
    guardrails: str | None = None
    prepare_tool_ref: dict | None = None
    finalize_tool_ref: dict | None = None
    skip_llm: bool = False
    available_channel_types: list[str] | None = [
        "web"
    ]  # Default: web chat only; external channels must be explicitly enabled
    channel_config: dict[str, dict] | None = None
    memory_short_term_enabled: bool = True
    memory_medium_term_enabled: bool = True
    memory_persistent_enabled: bool = True
    memory_user_enabled: bool = True
    human_in_the_loop: bool = False
    human_in_the_loop_prompt: str | None = None
    approve_tool_calls: bool | None = None
    disable_chat: bool = False
    hide_runs: bool = False
    create_default_assignments: bool = True

    _fit_columns = column_length_validator(Agent, *_AGENT_COLUMN_FIELDS)

    @field_validator("output_schema")
    @classmethod
    def validate_output_schema(cls, v: dict | None) -> dict | None:
        if v is None:
            return v
        raw = json.dumps(v)
        if len(raw) > 50_000:
            raise ValueError("output_schema must be under 50 KB")
        from jsonschema import Draft7Validator, SchemaError

        try:
            Draft7Validator.check_schema(v)
        except SchemaError as exc:
            raise ValueError(f"Invalid JSON Schema: {exc.message}") from exc
        return v

    @field_validator("knowledge_base_permissions")
    @classmethod
    def validate_kb_permissions(
        cls, v: dict[str, str] | None
    ) -> dict[str, str] | None:
        return _normalize_kb_permissions(v)

    @field_validator("skill_load_modes")
    @classmethod
    def validate_skill_load_modes(
        cls, v: dict[str, str] | None
    ) -> dict[str, str] | None:
        return _normalize_skill_load_modes(v)

    @field_validator("integration_template_bindings")
    @classmethod
    def validate_template_bindings(cls, v: list[str] | None) -> list[str] | None:
        return _refuse_per_user_unresolvable(v)

    @field_validator("integration_element_enforcement")
    @classmethod
    def validate_integration_element_enforcement(
        cls, v: list[str] | None
    ) -> list[str] | None:
        # Only the types a provider enforces: a switch that does nothing is
        # refused instead of stored.
        return parse_scope_enforcement(v)

    @field_validator("integration_read_only")
    @classmethod
    def validate_integration_read_only(
        cls, v: dict[str, list[str]] | None
    ) -> dict[str, list[str]] | None:
        return _normalize_integration_read_only(v)

    @field_validator("integration_template_read_only")
    @classmethod
    def validate_integration_template_read_only(cls, v: list[str] | None) -> list[str] | None:
        return _normalize_integration_template_read_only(v)

    @field_validator("integration_signatures", mode="before")
    @classmethod
    def validate_integration_signatures(
        cls, v: object
    ) -> dict[str, dict[str, str]] | None:
        return _normalize_integration_signatures(v)

    @field_validator("integration_live_writes", mode="before")
    @classmethod
    def validate_integration_live_writes(cls, v: object) -> dict[str, str] | None:
        return _normalize_integration_live_writes(v)

    @field_validator("browser_profile_ids", "browser_profile_read_only")
    @classmethod
    def validate_browser_profile_ids(cls, v: list[str] | None) -> list[str] | None:
        return _normalize_browser_profile_ids(v)

    @field_validator("tool_deny_list")
    @classmethod
    def validate_tool_deny_list(cls, v: list[str] | None) -> list[str] | None:
        return _normalize_tool_deny_list(v)

    @model_validator(mode="after")
    def _refuse_routing_with_own_model(self):
        return _refuse_routing_with_own_model(self)


def _refuse_routing_with_own_model[B: BaseModel](body: B) -> B:
    """A body putting the agent on the routing model names no model of its own.

    The routing source keeps the agent's own model empty, so a model sent with
    it would be validated, then dropped without a word: a 422 instead. Null and
    empty values are not a model; the source left out of an update keeps the
    stored one, which a model picked alone replaces.
    """
    if getattr(body, "llm_model_source", None) != LLM_MODEL_SOURCE_ROUTING:
        return body
    own = [name for name in _OWN_MODEL_FIELDS if getattr(body, name, None)]
    if own:
        raise ValueError(
            "llm_model_source 'routing' runs the agent on the platform routing "
            f"model, which keeps no model of its own: leave {', '.join(own)} "
            "empty, or leave llm_model_source out to pick the agent's own model."
        )
    return body


def _normalize_tool_deny_list(v: list[str] | None) -> list[str] | None:
    """The deny list as stored, or a 422: the agent builder's rule
    (``normalize_tool_deny_list``)."""
    from app.services.builtin_tools.scheduling import normalize_tool_deny_list

    return normalize_tool_deny_list(v)


#: A built-in function as the release notice names it: ``group.function``.
BuiltinFunctionName = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=3, max_length=200, pattern=r"^[a-z0-9_]+\.[A-Za-z0-9_]+$")
]


class BuiltinToolsNoticeRequest(BaseModel):
    """Dismiss built-in functions from the release notice of an agent (#12245).

    ``dismiss`` lists the ``group.function`` names the owner leaves out on
    purpose; an empty list dismisses nothing and only marks read the release
    notices left with nothing to act on (after an Add).
    """

    dismiss: list[BuiltinFunctionName] = Field(default_factory=list, max_length=200)


class BuiltinToolsNoticeResponse(BaseModel):
    #: Every ``group.function`` dismissed on the agent, oldest first.
    dismissed: list[str]
    #: The bell notifications this call marked read.
    resolved_notifications: list[str]


class UpdateAgentRequest(BaseModel):
    name: str | None = None
    slug: str | None = None
    description: str | None = None
    icon: str | None = None
    persona: str | None = None
    output_format: str | None = None
    output_schema: dict | None = None
    accept_caller_output_format: bool | None = None
    enabled: bool | None = None
    is_company_managed: bool | None = None
    #: Admin only (#11624): hide this agent's activity from non-admins.
    hide_activity_from_non_admins: bool | None = None
    #: A built-in's organization notes (#914), appended after its persona;
    #: null or "" clears them. Bounded by CUSTOMER_NOTES_MAX_CHARS.
    customer_notes: str | None = Field(default=None, max_length=4000)
    # Owner (``user_id``) transfer — admin only. Reassigns the agent (and its
    # assignments) to another user. Ignored when omitted or unchanged.
    user_id: str | None = None
    shared_group_ids: list[str] | None = None
    shared_group_permissions: dict[str, str] | None = None
    shared_user_ids: list[str] | None = None
    shared_user_permissions: dict[str, str] | None = None
    access_restricted: bool | None = None
    tags: list[str] | None = None
    integration_bindings: dict[str, list[str]] | None = None
    #: The owner's personal integrations this agent lends to the people it is
    #: shared with (used through this agent only); ``[]`` lends nothing.
    #: Owner only, each id bound by the agent.
    lent_integration_ids: list[str] | None = None
    integration_template_bindings: list[str] | None = None
    mcp_server_ids: list[str] | None = None
    custom_tool_ids: list[str] | None = None
    builtin_tool_ids: list[str] | None = None
    runner_ids: list[str] | None = None
    runner_labels: list[str] | None = None
    integration_allowed_tools: dict[str, list[str] | None] | None = None
    integration_element_bindings: dict[str, list[dict]] | None = None
    # The element types whose selection is a boundary on the tools;
    # null = every selection is guidance.
    integration_element_enforcement: list[str] | None = None
    mcp_allowed_tools: dict[str, list[str] | None] | None = None
    # Built-in group id -> function names it keeps; missing or null = all.
    builtin_allowed_tools: dict[str, list[str] | None] | None = None
    tool_deny_list: list[str] | None = None
    integration_read_only: dict[str, list[str]] | None = None
    integration_template_read_only: list[str] | None = None
    integration_signatures: dict[str, dict[str, str]] | None = None
    integration_live_writes: dict[str, str] | None = None
    browser_profile_ids: list[str] | None = None
    browser_profile_read_only: list[str] | None = None
    prompt_ids: list[str] | None = None
    skill_ids: list[str] | None = None
    skill_load_modes: dict[str, str] | None = None
    knowledge_base_ids: list[str] | None = None
    knowledge_base_permissions: dict[str, str] | None = None
    handover_agent_ids: list[str] | None = None
    handover_types: dict[str, str] | None = None
    work_item_owner_agent_ids: list[str] | None = None
    trace_supervision_target_agent_ids: list[str] | None = None
    coordination_access: AgentCoordinationAccess | None = None
    handover_conditions: dict[str, str] | None = None
    allow_self_fanout: bool | None = None
    config: dict | None = None
    llm_provider: str | None = None
    llm_provider_id: str | None = None
    llm_model: str | None = None
    # "routing" = run on the platform routing model, ignoring the three fields
    # above (``app.llm.agent_model``). NULL = the agent's own fields.
    llm_model_source: Literal["routing"] | None = None
    temperature: float | None = None
    reasoning_effort: str | None = None
    # Organization summary in the system prompt (#11533): inherit | always | off.
    organization_context_mode: Literal["inherit", "always", "off"] | None = None
    # Categories preloaded after the basic block (#11533): null = inherit, a list wins.
    organization_context_categories: list[str] | None = None
    max_iterations: int | None = None
    # Per-agent ``wait`` policy override: {per_call_seconds,
    # total_seconds, unbounded_with_until}, any subset; null = inherit.
    wait_policy: dict | None = None
    # Per-agent run budget — see AgentBudgetConfig. None/{} = inherit
    # the platform configuration entirely.
    budget_config: AgentBudgetConfig | None = None
    guardrails: str | None = None
    prepare_tool_ref: dict | None = None
    finalize_tool_ref: dict | None = None
    skip_llm: bool | None = None
    available_channel_types: list[str] | None = None
    channel_config: dict[str, dict] | None = None
    memory_short_term_enabled: bool | None = None
    memory_medium_term_enabled: bool | None = None
    memory_persistent_enabled: bool | None = None
    memory_user_enabled: bool | None = None
    human_in_the_loop: bool | None = None
    human_in_the_loop_prompt: str | None = None
    # Null is meaningful here: follow the install default.
    approve_tool_calls: bool | None = None
    is_public_catalog: bool | None = None
    catalog_include_personal_tools: bool | None = None
    disable_chat: bool | None = None
    hide_runs: bool | None = None
    web_enabled: bool | None = None
    web_suggested_prompts: list[str] | None = None
    web_uploads_enabled: bool | None = None
    commit_message: str | None = Field(default=None, max_length=500)

    _fit_columns = column_length_validator(Agent, *_AGENT_COLUMN_FIELDS)

    @field_validator("output_schema")
    @classmethod
    def validate_output_schema(cls, v: dict | None) -> dict | None:
        if v is None:
            return v
        raw = json.dumps(v)
        if len(raw) > 50_000:
            raise ValueError("output_schema must be under 50 KB")
        from jsonschema import Draft7Validator, SchemaError

        try:
            Draft7Validator.check_schema(v)
        except SchemaError as exc:
            raise ValueError(f"Invalid JSON Schema: {exc.message}") from exc
        return v

    @field_validator("knowledge_base_permissions")
    @classmethod
    def validate_kb_permissions(
        cls, v: dict[str, str] | None
    ) -> dict[str, str] | None:
        return _normalize_kb_permissions(v)

    @field_validator("skill_load_modes")
    @classmethod
    def validate_skill_load_modes(
        cls, v: dict[str, str] | None
    ) -> dict[str, str] | None:
        return _normalize_skill_load_modes(v)

    @field_validator("integration_template_bindings")
    @classmethod
    def validate_template_bindings(cls, v: list[str] | None) -> list[str] | None:
        return _refuse_per_user_unresolvable(v)

    @field_validator("integration_element_enforcement")
    @classmethod
    def validate_integration_element_enforcement(
        cls, v: list[str] | None
    ) -> list[str] | None:
        # Only the types a provider enforces: a switch that does nothing is
        # refused instead of stored.
        return parse_scope_enforcement(v)

    @field_validator("integration_read_only")
    @classmethod
    def validate_integration_read_only(
        cls, v: dict[str, list[str]] | None
    ) -> dict[str, list[str]] | None:
        return _normalize_integration_read_only(v)

    @field_validator("integration_template_read_only")
    @classmethod
    def validate_integration_template_read_only(cls, v: list[str] | None) -> list[str] | None:
        return _normalize_integration_template_read_only(v)

    @field_validator("integration_signatures", mode="before")
    @classmethod
    def validate_integration_signatures(
        cls, v: object
    ) -> dict[str, dict[str, str]] | None:
        return _normalize_integration_signatures(v)

    @field_validator("integration_live_writes", mode="before")
    @classmethod
    def validate_integration_live_writes(cls, v: object) -> dict[str, str] | None:
        return _normalize_integration_live_writes(v)

    @field_validator("browser_profile_ids", "browser_profile_read_only")
    @classmethod
    def validate_browser_profile_ids(cls, v: list[str] | None) -> list[str] | None:
        return _normalize_browser_profile_ids(v)

    @field_validator("tool_deny_list")
    @classmethod
    def validate_tool_deny_list(cls, v: list[str] | None) -> list[str] | None:
        return _normalize_tool_deny_list(v)

    @model_validator(mode="after")
    def _refuse_routing_with_own_model(self):
        return _refuse_routing_with_own_model(self)


class ImportAgentRequest(BaseModel):
    """The full export bundle to import (supports v1, v2, and v3)."""

    version: int
    export_type: str = "agent"
    agent: dict
    assignments: list[dict] = []
    custom_tools: list[dict] = []
    mcp_servers: list[dict] = []
    skills: list[dict] = []
    prompts: list[dict] = []
    integration_providers: list[dict] = []
    knowledge_base_notes: list[dict] = []
    referenced_variables: list[str] = []
    variables: list[dict] = []
    # Persistent memory entries, as the native export carries them.
    memories: list[dict] = []


#: Portable formats an agent can be exported in. ``native`` is the XTM One
#: bundle (lossless, the default); ``agentschema`` is a Microsoft AgentSchema
#: ``kind: prompt`` document. Mirrors ``ImportFormat`` in
#: ``app.services.agentschema.detect`` — the same vocabulary is used for the
#: export query parameter, the detected import format, audit details and
#: telemetry.
ExportFormat = Literal["native", "agentschema"]


class VariableNeedingValueResponse(BaseModel):
    """A variable an agent references that has no value yet - a
    secret a catalog install copied without its value, typically - or, when
    ``deleted``, that no variable carries any more, with no name."""

    id: str
    name: str | None
    is_secret: bool
    deleted: bool = False


class AgentPortabilityEntryResponse(BaseModel):
    """One agent capability the target export format cannot carry as-is."""

    #: Stable machine key (the XTM One field or feature name).
    key: str
    label: str
    #: What happens to it, and why — rendered verbatim in the export dialog.
    detail: str
    #: ``"omitted"`` — absent from the exported document's standard fields
    #: entirely. ``"flattened"`` — the content survives but is merged into a
    #: spec field and is no longer a distinct, separately-managed entity.
    disposition: Literal["omitted", "flattened"]


class AgentExportPortabilityResponse(BaseModel):
    """What exporting an agent to a given format would not carry.

    Read *before* the export runs, so the loss is never silent.
    """

    format: ExportFormat
    format_label: str
    #: AgentSchema spec version the report was computed against; absent for the
    #: native format, which has no external specification.
    spec_version: str | None = None
    entries: list[AgentPortabilityEntryResponse] = []
    #: True when the omitted data is round-tripped through the document's
    #: ``metadata`` namespace, so re-importing into XTM One is lossless.
    preserved_in_metadata: bool


class ImportAgentFileResponse(BaseModel):
    """Result of a file upload import, with the detection/validation verdict.

    The created agent is returned alongside *how* the file was read, so the UI
    can tell the user which format was detected instead of leaving the
    auto-detection invisible.
    """

    agent: AgentResponse
    detected_format: ExportFormat
    detected_format_label: str
    #: Leniencies applied while reading the document (a missing ``kind``, a
    #: shorthand ``model``, ...). Informational — the import succeeded.
    notes: list[str] = []
    #: References the file makes that this instance cannot satisfy (external
    #: MCP servers, knowledge bases, tools with no implementation). Surfaced to
    #: the user rather than silently ignored.
    warnings: list[str] = []


class ImportCheckStageResponse(BaseModel):
    """One step of the import verification loop, with its verdict."""

    key: Literal["parse", "detect", "schema", "references"]
    status: Literal["passed", "failed", "skipped"]
    #: Present on a failed stage: the message plus per-field problems.
    problems: list[str] = []


class ImportCorrectionResponse(BaseModel):
    """One optional, deterministic fix the user can apply before importing.

    The id is self-describing (``kind:subject``) so the confirming call can
    replay the selection statelessly; the UI renders the human wording from
    ``kind`` + ``subject``.
    """

    id: str
    kind: Literal["drop_custom_tool", "drop_mcp_server"]
    subject: str


class ImportAgentFileCheckResponse(BaseModel):
    """Dry-run verdict on an uploaded agent file. Nothing was persisted.

    Mirrors what ``POST /agents/import-file`` would have accepted or rejected,
    stage by stage, so the import page can show where the verification loop
    stopped and what the platform could not satisfy — before the user commits.
    """

    ok: bool
    #: None when detection itself failed.
    detected_format: ExportFormat | None = None
    detected_format_label: str | None = None
    #: The imported agent's display name, pulled from the mapped bundle.
    agent_name: str | None = None
    stages: list[ImportCheckStageResponse]
    notes: list[str] = []
    warnings: list[str] = []
    #: Placeholder entries the user may choose to drop before importing.
    corrections: list[ImportCorrectionResponse] = []
    #: The upload mapped onto the native bundle shape — feed it to
    #: ``POST /agents/import/confirm`` to import exactly what was checked.
    bundle: dict | None = None


class ConfirmAgentImportRequest(BaseModel):
    """Import a bundle previously returned by the dry-run check endpoint."""

    bundle: dict
    #: Provenance for the audit trail — which format the original upload was
    #: detected as. The bundle itself is always in native shape.
    detected_format: ExportFormat = "native"
    #: Correction ids (from the check report) to apply before importing.
    corrections: list[str] = []


class SuggestImportFixesRequest(BaseModel):
    """Ask the platform LLM how to finish setting up an imported agent."""

    agent_name: str | None = None
    #: The agent's persona/instructions, so suggestions can point at the
    #: passages that reference unavailable tools. Bounded server-side.
    persona: str | None = None
    #: The check report's warnings — what this platform cannot satisfy.
    warnings: list[str] = []


class SuggestImportFixesResponse(BaseModel):
    suggestions: str
