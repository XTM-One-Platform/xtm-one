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

"""Which :class:`~app.models.agent.Agent` fields travel when an agent is copied.

Four manifests live here, one per kind of copy:

- :data:`AGENT_COPY_FIELDS` / :data:`AGENT_COPY_SKIP_FIELDS` — an **in-place**
  copy (``POST /agents/{id}/duplicate``), where every row the source points at
  still exists, so almost everything can be carried verbatim;
- :data:`AGENT_BUNDLE_PORTABLE_FIELDS` / :data:`AGENT_BUNDLE_EXPORT_ONLY_FIELDS`
  / :data:`AGENT_BUNDLE_SKIP_FIELDS` — a copy that crosses an **instance
  boundary** (export → import), where a UUID from the source instance resolves
  to nothing and must not travel;
- :data:`ASSIGNMENT_COPY_FIELDS` / :data:`ASSIGNMENT_COPY_SKIP_FIELDS` — the
  assignments that ride along with an in-place agent copy, which had the same
  hand-written-list problem one level down;
- :data:`AGENT_CATALOG_CLONE_FIELDS` / :data:`AGENT_CATALOG_CLONE_SKIP_FIELDS`
  and their assignment pair — a Filigran AI Catalog "Add to my account", a copy
  on the same instance into ANOTHER account.

Why a manifest instead of a kwargs list
---------------------------------------
``POST /agents/{id}/duplicate`` used to hand-write every field it carried onto
the copy. That makes *silently dropped* the default for any column added later,
and that has a cost:

- ``integration_template_bindings`` was missing, so duplicating any built-in
  OpenCTI/OpenAEV agent produced a copy with **no per-user integration tools at
  all** — the copy answered as if the platform were not connected;
- ``tool_deny_list`` was missing, so a duplicate silently **re-granted** tools
  the source explicitly denied (deny is documented as taking precedence over
  allow lists precisely so dangerous operations stay blocked);
- the agent-level ``human_in_the_loop`` pair, ``handover_prompt``,
  ``supported_verticals`` and ``web_suggested_prompts`` were missing too, while
  their siblings (the *assignment*-level HITL fields, ``supported_intents``)
  were copied — so the copy was internally inconsistent.

This module inverts the default: **every** mapped column of ``Agent`` must
appear either in :data:`AGENT_COPY_FIELDS` (carried onto the copy) or in
:data:`AGENT_COPY_SKIP_FIELDS` (deliberately not carried, with the reason
spelled out). ``tests/test_agent_copy_manifest.py`` asserts the partition is
exact, so adding a column to ``Agent`` without deciding fails the suite instead
of quietly shipping another dropped field.

Adding a column to ``Agent``? Put it in one bucket of each manifest. Copying is
the right answer unless the value is an identity, a secret, a sharing grant or
an activation state — those are the four kinds of thing the skip lists hold.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from copy import deepcopy
from types import MappingProxyType
from typing import Any

from sqlalchemy import inspect

from app.core.utils import (
    is_valid_uuid,
    redact_secret_args,
    redact_secret_text,
    strip_webhook_secret,
)
from app.models.agent import Agent
from app.models.assignment import Assignment

#: Columns carried onto the copy, deep-copied so that later edits on the copy
#: can never leak back into the source through a shared nested list/dict.
#: Declaration order mirrors ``Agent`` so the two read side by side.
AGENT_COPY_FIELDS: tuple[str, ...] = (
    "template_slug",
    "description",
    "icon",
    "persona",
    "output_format",
    "output_schema",
    "accept_caller_output_format",
    "integration_bindings",
    # The per-user integration resolution list (e.g. ["opencti"]).
    # Every built-in agent relies on it for OpenCTI / OpenAEV / OpenCRQ access.
    "integration_template_bindings",
    "mcp_server_ids",
    "integration_allowed_tools",
    # Built-in group ids and function names are portable (no instance id).
    "builtin_allowed_tools",
    "mcp_allowed_tools",
    # Deny takes precedence over every allow list — a copy that dropped
    # it silently re-enabled the operations the source blocked.
    "tool_deny_list",
    # The bindings whose write tools are withheld. A copy that dropped
    # it would give the copy write access the source never had.
    "integration_read_only",
    # The template-bound providers kept read-only: provider keys, portable
    # like integration_template_bindings, so a copy never gains their writes.
    "integration_template_read_only",
    # Same bindings, so the same signature overrides.
    "integration_signatures",
    # Same bindings, so the same Live writes overrides: a copy never writes
    # live where the source was held to drafts, nor the reverse.
    "integration_live_writes",
    # Same instance, so the profiles still exist; a run of the copy opens
    # one only for a person who may use it. The read-only list travels with
    # them: a copy that dropped it would click where the source only reads.
    "browser_profile_ids",
    "browser_profile_read_only",
    "integration_element_bindings",
    # Which of those selections is a boundary. A copy that dropped it
    # would turn every enforced selection back into guidance.
    "integration_element_enforcement",
    "custom_tool_ids",
    "builtin_tool_ids",
    "runner_ids",
    "runner_labels",
    "prompt_ids",
    "skill_ids",
    "skill_load_modes",
    "knowledge_base_ids",
    "knowledge_base_permissions",
    "handover_agent_ids",
    "handover_types",
    "handover_conditions",
    "handover_prompt",
    "allow_self_fanout",
    "work_item_owner_agent_ids",
    "trace_supervision_target_agent_ids",
    # Same instance: the agents it lists still exist, and a narrowing that a
    # copy dropped would open the rows the copy governs to every agent.
    "coordination_access",
    "tags",
    "supported_intents",
    "supported_verticals",
    "config",
    "llm_provider",
    "llm_provider_id",
    "llm_model",
    # Portable: it names the platform routing model, not one instance's model.
    "llm_model_source",
    "temperature",
    "reasoning_effort",
    "max_iterations",
    # Wait-budget override, bounded at run time by the platform ceilings.
    "wait_policy",
    "organization_context_mode",
    "organization_context_categories",
    "budget_config",
    "guardrails",
    "prepare_tool_ref",
    "finalize_tool_ref",
    "skip_llm",
    "memory_short_term_enabled",
    "memory_medium_term_enabled",
    "memory_persistent_enabled",
    "memory_user_enabled",
    "human_in_the_loop",
    "human_in_the_loop_prompt",
    "approve_tool_calls",
    "hide_runs",
    "disable_chat",
    "available_channel_types",
    "channel_config",
    "web_suggested_prompts",
)

#: Columns deliberately NOT carried onto the copy, each with the reason.
#: A caller may still set any of them explicitly via
#: :func:`build_agent_copy_kwargs` overrides — that is how the copy gets its own
#: owner, name and slug.
AGENT_COPY_SKIP_FIELDS: Mapping[str, str] = MappingProxyType(
    {
        # --- Platform Health (#8254): consent and identity never travel ---
        "health_enrolled": "Consent is given by the managers of each agent, never inherited.",
        "health_content_opt_in": "Consent is given by the managers of each agent, never inherited.",
        "health_role": "Only the seeded fleet acts as the Platform Health identity; a copy never does.",
        # --- Identity: the copy is a new row ---
        "id": "Primary key of a new row.",
        "created_at": "Timestamps belong to the copy's own lifecycle.",
        "updated_at": "Timestamps belong to the copy's own lifecycle.",
        "name": "The copy needs a distinct name; the caller supplies it.",
        "slug": "Unique per agent; the caller generates a fresh one.",
        # --- Ownership and visibility: the copy is private to its creator ---
        "user_id": "The copy is owned by whoever asked for it, not by the source's owner.",
        "is_company_managed": (
            "A copy is always personal: promoting it to company-managed is an "
            "explicit, admin-gated action."
        ),
        "is_sandbox": "A duplicate is an ordinary agent; only the sandbox sets it, on its own copies (#8255).",
        "hide_activity_from_non_admins": (
            "An administrator's switch on a company-managed agent (#11624); a personal copy never hides its activity."
        ),
        "customer_notes": "The customer layer of a built-in on this instance (#914); a copy edits its own persona.",
        "customer_fields": "Seed bookkeeping of a built-in's customer layer (#914); a copy is never seeded.",
        "shared_group_ids": "Sharing grants are re-granted deliberately, never inherited.",
        "shared_group_permissions": "Paired with shared_group_ids.",
        "shared_user_ids": "Sharing grants are re-granted deliberately, never inherited.",
        "shared_user_permissions": "Paired with shared_user_ids.",
        "integration_lending": (
            "The source owner's choice to lend their personal integrations to the "
            "people the agent is shared with; the copy is private to its creator, "
            "who lends their own deliberately (#8824)."
        ),
        "access_restricted": (
            "Only meaningful on a company-managed agent, which a copy never is."
        ),
        # --- Platform-managed state ---
        "is_builtin": (
            "Built-in agents are seeded and fully re-synced on every boot; a copy "
            "must stay a normal, editable agent."
        ),
        "is_public_catalog": (
            "Publication to the Filigran AI Catalog is an explicit action on a "
            "company-managed agent."
        ),
        "catalog_include_personal_tools": (
            "A publishing choice, paired with is_public_catalog, which a copy "
            "does not carry either."
        ),
        # --- Activation state: a copy starts in the safe position ---
        "enabled": "A freshly created copy starts enabled (the model default).",
        "web_enabled": "Public web exposure must be re-enabled explicitly on the copy.",
        "web_uploads_enabled": "Visitor uploads on the public widget must be allowed explicitly on the copy.",
        "web_token": (
            "Live credential in public embed URLs — regenerated when web exposure "
            "is enabled on the copy."
        ),
    }
)


def agent_mapped_columns() -> tuple[str, ...]:
    """Every mapped column name of ``Agent``, in declaration order."""
    return tuple(attr.key for attr in inspect(Agent).mapper.column_attrs)


def assignment_mapped_columns() -> tuple[str, ...]:
    """Every mapped column name of ``Assignment``, in declaration order."""
    return tuple(attr.key for attr in inspect(Assignment).mapper.column_attrs)


def _reject_unknown_columns(
    overrides: Mapping[str, Any], columns: tuple[str, ...], model: str
) -> None:
    """Fail on an override that does not name a column of ``model``.

    A mistyped override is the one way a caller can still lose a field silently:
    ``integration_template_binding=[...]`` would raise from SQLAlchemy's
    constructor with no hint of which manifest field it meant to replace.
    """
    unknown = set(overrides) - set(columns)
    if unknown:
        raise ValueError(
            f"Unknown {model} column(s) in copy overrides: {sorted(unknown)}"
        )


#: Tags the platform writes to mark an agent it manages: ``builtin`` (a seeded
#: built-in agent) and the ``org-fill:`` marker of an organization fill agent
#: (``services/organization_context_fill_agent.py``). A copy is never the
#: platform's agent, so it never carries them (#12345).
RESERVED_AGENT_TAGS: frozenset[str] = frozenset({"builtin"})
RESERVED_AGENT_TAG_PREFIXES: tuple[str, ...] = ("org-fill:",)


def is_reserved_agent_tag(tag: object) -> bool:
    """Whether *tag* is one the platform alone writes on an agent."""
    return isinstance(tag, str) and (
        tag in RESERVED_AGENT_TAGS or tag.startswith(RESERVED_AGENT_TAG_PREFIXES)
    )


def split_reserved_agent_tags(tags: Any) -> tuple[list[Any] | None, list[str]]:
    """*tags* without the reserved ones, and the reserved ones dropped.

    ``None`` stays ``None``; a list emptied by the drop becomes ``[]``.
    """
    if not isinstance(tags, list):
        return tags, []
    dropped = [t for t in tags if is_reserved_agent_tag(t)]
    return [t for t in tags if not is_reserved_agent_tag(t)], dropped


def build_agent_copy_kwargs(source: Agent, **overrides: Any) -> dict[str, Any]:
    """Return ``Agent(...)`` kwargs that copy ``source``, plus ``overrides``.

    Every value is deep-copied: a plain ``dict()``/``list()`` would only shallow-
    copy and leave nested containers shared between source and copy, so
    ``copy.integration_allowed_tools["github"].append(...)`` would mutate the
    source row too.

    ``None`` is carried as ``None`` rather than normalised to an empty container:
    a NULL column is a meaningful state on several of these fields (NULL
    ``available_channel_types`` means "every channel" for legacy rows) and every
    read site already treats NULL and empty alike via ``or []`` / ``or {}``.

    ``overrides`` win over the copied value and must name real ``Agent``
    columns, so a typo raises here instead of being swallowed by ``**kwargs``.

    The reserved tags (:func:`is_reserved_agent_tag`) never travel: a copy is
    not the platform's agent.
    """
    _reject_unknown_columns(overrides, agent_mapped_columns(), "Agent")
    kwargs: dict[str, Any] = {
        name: deepcopy(getattr(source, name)) for name in AGENT_COPY_FIELDS
    }
    kwargs["tags"], _dropped = split_reserved_agent_tags(kwargs.get("tags"))
    kwargs.update(overrides)
    return kwargs


# --- Bundle field manifest ------------------------------------------
#
# Export and import used to keep two independent hand-written field lists, so a
# newly added ``Agent`` column defaulted to *silently not travelling*. That is
# how an exported OpenCTI agent lost ``integration_template_bindings`` — and
# with it every per-user OpenCTI tool — on re-import, and how ``hide_runs``
# ended up referenced by the AgentSchema portability report while never being
# written to the bundle at all.
#
# Every mapped column of ``Agent`` now belongs to exactly one of the three
# collections below, and ``tests/test_agent_copy_manifest.py`` asserts that
# partition is exact — adding a column without deciding fails the suite.

#: Carried in the bundle's ``agent`` section AND applied on import. The whole
#: set is portable by construction: scalars, free text, and maps keyed by
#: provider name (never by a UUID from the exporting instance).
AGENT_BUNDLE_PORTABLE_FIELDS: tuple[str, ...] = (
    "name",
    "description",
    "icon",
    "persona",
    "output_format",
    "output_schema",
    "accept_caller_output_format",
    # Provider *names* ("opencti", "openaev"), resolved per user at runtime —
    # portable, unlike the UUIDs in ``integration_bindings``. Dropping this was
    # The re-imported agent had no OpenCTI tools at all.
    "integration_template_bindings",
    # Provider names too, and it narrows: carried fail-closed with them.
    "integration_template_read_only",
    # Both of these NARROW what the agent may touch, so carrying them is the
    # fail-closed choice (same reasoning as ``runner_ids`` below): an omitted
    # scope re-imports as "everything this provider offers", silently widening
    # tool access the moment the user connects their own integration.
    "integration_allowed_tools",
    # Built-in group ids and function names are portable (no instance id).
    "builtin_allowed_tools",
    "integration_element_bindings",
    # Travels with the selection it applies to; the import keeps only
    # the flags of the types the imported selection has.
    "integration_element_enforcement",
    "tool_deny_list",
    "builtin_tool_ids",
    # Runner selectors travel so an agent that keeps the ``computer_use`` tool
    # re-imports with the SAME machine scope; a cross-tenant runner id simply
    # dangles (targets nothing), which is the fail-closed default.
    "runner_ids",
    "runner_labels",
    "handover_types",
    "handover_conditions",
    "handover_prompt",
    "allow_self_fanout",
    # The scopes travel (fail-closed, like the scopes above); the import keeps
    # none of its agent ids, which name agents of the exporting instance
    # (``portable_coordination_access``).
    "coordination_access",
    "tags",
    "supported_intents",
    "supported_verticals",
    "config",
    "llm_provider",
    "llm_model",
    # Portable: it names the platform routing model, not one instance's model.
    "llm_model_source",
    "temperature",
    "reasoning_effort",
    "max_iterations",
    # Portable: bounded at run time by the importing install's ceilings.
    "wait_policy",
    "organization_context_mode",
    "organization_context_categories",
    "budget_config",
    "guardrails",
    "memory_short_term_enabled",
    "memory_medium_term_enabled",
    "memory_persistent_enabled",
    "memory_user_enabled",
    "human_in_the_loop",
    "human_in_the_loop_prompt",
    "hide_runs",
    "disable_chat",
    "web_suggested_prompts",
)

#: Exported for information (and for the AgentSchema portability report), but
#: NOT applied on import — the values name rows in the exporting instance.
AGENT_BUNDLE_EXPORT_ONLY_FIELDS: Mapping[str, str] = MappingProxyType(
    {
        "work_item_owner_agent_ids": (
            "Peer agent UUIDs from the source instance; they do not resolve "
            "here and would dangle, so the roster is cleared on import."
        ),
        "trace_supervision_target_agent_ids": (
            "Target agent UUIDs from the source instance; cleared on import "
            "for the same reason."
        ),
        "approve_tool_calls": (
            "Feeds the AgentSchema approvalMode; the importing install's own "
            "approval posture applies."
        ),
    }
)

#: Absent from the bundle entirely, with the reason. Two distinct kinds live
#: here: values that cannot cross an instance boundary, and values that DO
#: travel but out of band (re-keyed onto rows the import creates).
AGENT_BUNDLE_SKIP_FIELDS: Mapping[str, str] = MappingProxyType(
    {
        # --- Platform Health (#8254): consent and identity never travel ---
        "health_enrolled": "Consent is given by the managers of each agent, never inherited.",
        "health_content_opt_in": "Consent is given by the managers of each agent, never inherited.",
        "health_role": "Only the seeded fleet acts as the Platform Health identity; a copy never does.",
        # --- Identity / ownership / platform state, re-established on import ---
        "id": "Primary key of a new row.",
        "created_at": "Timestamps belong to the imported row's own lifecycle.",
        "updated_at": "Timestamps belong to the imported row's own lifecycle.",
        "slug": "Regenerated so it is unique on the importing instance.",
        "user_id": "The import is owned by whoever performed it.",
        "is_company_managed": "An imported agent is always personal.",
        "is_sandbox": "An imported agent is an ordinary agent, never a sandbox copy (#8255).",
        "hide_activity_from_non_admins": "An administrator's switch on this instance (#11624), never imported.",
        "customer_notes": "The customer layer of a built-in on this instance (#914), never imported.",
        "customer_fields": "Seed bookkeeping of a built-in on this instance (#914), never imported.",
        "shared_group_ids": "Sharing grants name groups of the source instance.",
        "shared_group_permissions": "Paired with shared_group_ids.",
        "shared_user_ids": "Sharing grants name users of the source instance.",
        "shared_user_permissions": "Paired with shared_user_ids.",
        "access_restricted": "Only meaningful on a company-managed agent.",
        "is_builtin": "Built-in agents are seeded, never imported.",
        "is_public_catalog": "Catalog publication is an explicit local action.",
        "catalog_include_personal_tools": "A publishing choice, paired with is_public_catalog.",
        "enabled": "An imported agent starts enabled (the model default).",
        "web_enabled": "Public web exposure must be enabled explicitly.",
        "web_uploads_enabled": "Visitor uploads on the public widget must be allowed explicitly.",
        "web_token": "Live credential in public embed URLs — never exported.",
        "template_slug": (
            "Names an AgentTemplate of the source instance; the create path "
            "validates that the template exists, so an unresolvable slug is "
            "dropped rather than stored."
        ),
        # --- Cross-instance references that cannot be resolved ---
        "integration_bindings": (
            "Integration UUIDs of the source instance. The bundle carries the "
            "``integration_providers`` section instead (provider/type only, no "
            "secrets) so the importer knows what to connect."
        ),
        "integration_read_only": (
            "Keyed by integration UUIDs of the source instance, like "
            "integration_bindings, which do not travel: an imported agent has no "
            "bound integration for it to narrow."
        ),
        "integration_lending": (
            "Integration UUIDs and a user id of the source instance, and the "
            "exporting owner's grant: never travels (#8824)."
        ),
        "integration_signatures": (
            "Keyed by integration UUIDs of the source instance, like "
            "integration_bindings: each binding of the imported agent inherits "
            "its integration's Email signature setting."
        ),
        "integration_live_writes": (
            "Keyed by integration UUIDs of the source instance, like "
            "integration_bindings: each OpenCTI binding of the imported agent "
            "inherits its integration's Live writes setting (off by default)."
        ),
        "browser_profile_ids": (
            "Browser profile UUIDs of the source instance: saved logins, which "
            "never leave the instance that holds them."
        ),
        "browser_profile_read_only": "Paired with browser_profile_ids, which does not travel.",
        "llm_provider_id": (
            "LLMConfig UUID of the source instance; the portable "
            "``llm_provider``/``llm_model`` pair is carried instead."
        ),
        "knowledge_base_ids": (
            "Knowledge bases are not exported (see ``knowledge_base_notes``)."
        ),
        "knowledge_base_permissions": "Keyed by knowledge base UUID, which is not carried.",
        "handover_agent_ids": "Peer agent UUIDs of the source instance do not resolve here.",
        "channel_config": (
            "Holds channel bot integration UUIDs — importing them would point "
            "the agent at another account's bot credentials."
        ),
        "available_channel_types": (
            "Derived from channel_config, which is not carried; an imported "
            "agent starts web-only so external channels stay opt-in."
        ),
        "prepare_tool_ref": (
            "Pipeline refs identify a tool by id/provider on the source "
            "instance and do not resolve here."
        ),
        "finalize_tool_ref": "Same as prepare_tool_ref.",
        "skip_llm": (
            "Deliberately tied to the pipeline refs above: carrying "
            "skip_llm=True without them would import an agent whose LLM loop "
            "is disabled and whose prepare tool is missing — it would answer "
            "nothing at all."
        ),
        # --- Carried out of band, re-keyed onto the rows the import creates ---
        "custom_tool_ids": "The ``custom_tools`` section is recreated; new ids are bound.",
        "mcp_server_ids": "The ``mcp_servers`` section is recreated; new ids are bound.",
        "mcp_allowed_tools": (
            "Travels as ``allowed_tools`` inside each ``mcp_servers`` entry and "
            "is re-keyed onto the newly created server ids — an id-keyed map "
            "would not resolve, and losing it would silently widen MCP tool "
            "access. Mirrors ``skill_load_modes``."
        ),
        "skill_ids": "The ``skills`` section is recreated; new ids are bound.",
        "skill_load_modes": (
            "Travels as ``load_mode`` inside each ``skills`` entry and is "
            "re-keyed onto the newly created skill ids."
        ),
        "prompt_ids": "The ``prompts`` section is recreated; new ids are bound.",
    }
)


def build_agent_import_kwargs(
    agent_data: dict[str, Any], **overrides: Any
) -> dict[str, Any]:
    """``Agent(...)`` kwargs for the portable fields present in ``agent_data``.

    A key that is absent or ``null`` is left out entirely so the column default
    applies, which is what the per-field ``agent_data.get(name, <default>)``
    calls this replaced did. ``overrides`` win, and are applied by the caller
    for the fields that need local validation (model availability, iteration
    clamping) or a variable-id rewrite.
    """
    _reject_unknown_columns(overrides, agent_mapped_columns(), "Agent")
    kwargs: dict[str, Any] = {
        name: agent_data[name]
        for name in AGENT_BUNDLE_PORTABLE_FIELDS
        if agent_data.get(name) is not None
    }
    kwargs.update(overrides)
    return kwargs


# --- Assignment field manifest --------------------------------------
#
# Duplicating an agent duplicates its assignments, and that loop had the same
# hand-written kwargs list — dropping the assignment's ``runner_ids``
# refinement, its ``integration_element_bindings`` scope, its ``hide_runs``
# override, its ``manual_trigger_parameters`` schema and, worst of the set,
# ``ignore_self_triggered_events``: the copy came back with the loop guard off,
# so enabling it could re-trigger itself forever on its own writes.

#: Assignment columns carried onto the duplicated assignment, deep-copied.
ASSIGNMENT_COPY_FIELDS: tuple[str, ...] = (
    # Per-assignment override of the agent's run-visibility default; tri-state
    # (NULL = inherit the agent), so it is copied as-is rather than defaulted.
    "hide_runs",
    "name",
    "description",
    "trigger_type",
    "activation_prompt",
    # The tool-capable gate: a copy without it would run every time.
    "activation_check",
    "prompt",
    "delay",
    "tool_sources",
    "integration_bindings",
    "integration_allowed_tools",
    # Built-in group ids and function names are portable (no instance id).
    "builtin_allowed_tools",
    "integration_element_bindings",
    "integration_element_enforcement",
    "channel_config",
    # Tri-state runner refinement (NULL = inherit the agent, [] = none, [ids] =
    # subset). Dropping it re-imported as "inherit everything", widening the
    # machine scope of an assignment that keeps the ``computer_use`` tool.
    "runner_ids",
    "use_knowledge_bases",
    "read_knowledge_base_ids",
    "write_knowledge_base_ids",
    "write_knowledge_base_enabled",
    "target_knowledge_base_id",
    "ai_label",
    "ai_label_prompt",
    "notify_on_complete",
    "show_in_digest",
    "max_iterations",
    "timeout_seconds",
    # Wait-budget override, bounded at run time by the platform ceilings.
    "wait_policy",
    "organization_context_mode",
    "organization_context_categories",
    "retry_on_failure",
    "max_retries",
    "human_in_the_loop",
    "human_in_the_loop_prompt",
    "dry_run",
    # The self-trigger loop guard: a copy without it can react to its own
    # writes forever once enabled.
    "ignore_self_triggered_events",
    "manual_trigger_parameters",
)

#: Assignment columns deliberately NOT carried onto the copy, each with the
#: reason. As with the agent manifest, the caller may set any of them via
#: :func:`build_assignment_copy_kwargs` overrides.
ASSIGNMENT_COPY_SKIP_FIELDS: Mapping[str, str] = MappingProxyType(
    {
        "id": "Primary key of a new row.",
        "created_at": "Timestamps belong to the copy's own lifecycle.",
        "updated_at": "Timestamps belong to the copy's own lifecycle.",
        "agent_id": "The copy belongs to the duplicated agent.",
        "user_id": "The copy is owned by whoever asked for the duplicate.",
        "is_company_managed": "The duplicated agent is personal, so its assignments are too.",
        "run_as_user_id": (
            "Execution identity: an admin-only setting on a company-managed "
            "agent that makes runs execute with ANOTHER user's integrations and "
            "credentials. A duplicate is personal and can be made by any user "
            "who can see the source, so the identity is reset to the copy's own "
            "owner rather than inherited."
        ),
        "run_as_all_users": (
            "Fan-out to one run per active user — same admin-only, "
            "company-managed-only concept as run_as_user_id."
        ),
        "trigger_config": (
            "Copied by the caller after stripping the source's webhook signing "
            "secret: the duplicate of a company-managed agent is owned by a user "
            "who may not be allowed to see it, so webhook copies get a fresh "
            "secret of their own (SEC-5)."
        ),
        "next_assignment_id": (
            "Chain target, re-keyed by the caller onto the duplicated "
            "counterpart once the copies have ids. A target outside the "
            "duplicated set is dropped rather than wiring the copy into the "
            "source agent's pipeline."
        ),
        "enabled": "Duplicated assignments start disabled for safety.",
        # Pause provenance — describes the SOURCE's current pause.
        # Carrying it would make the copy claim it was quarantined by an
        # integration failure it never had. The caller re-stamps the copy with
        # its own ``created_disabled`` reason instead.
        "paused_at": "Pause provenance describes the source's pause, not the copy's.",
        "paused_reason": "Pause provenance describes the source's pause, not the copy's.",
        "paused_by_user_id": "Pause provenance describes the source's pause, not the copy's.",
        "paused_by_label": "Pause provenance describes the source's pause, not the copy's.",
        "paused_details": "Pause provenance describes the source's pause, not the copy's.",
        "last_paused_at": (
            "Streak boundary over the source's run history; the copy "
            "has no runs of its own."
        ),
        "execution_count": "Run history belongs to the source.",
        "events_filtered_count": "Run history belongs to the source.",
        "last_triggered_at": "Run history belongs to the source.",
        "schedule_resumed_at": "Schedule anchor of the source's last resume.",
    }
)


def build_assignment_copy_kwargs(
    source: Assignment, **overrides: Any
) -> dict[str, Any]:
    """Return ``Assignment(...)`` kwargs that copy ``source``, plus overrides.

    Same contract as :func:`build_agent_copy_kwargs`: deep-copied values, NULL
    carried as NULL (it is a distinct "inherit the agent" state on the tri-state
    refinement columns), and overrides validated against the real columns.
    """
    _reject_unknown_columns(overrides, assignment_mapped_columns(), "Assignment")
    kwargs: dict[str, Any] = {
        name: deepcopy(getattr(source, name)) for name in ASSIGNMENT_COPY_FIELDS
    }
    kwargs.update(overrides)
    return kwargs


# --- Catalog clone manifest -----------------------------------------
#
# "Add to my account" on a Filigran AI Catalog agent is a THIRD kind of copy:
# the same instance (every UUID still resolves), but a DIFFERENT account - the
# installer is any authenticated user, the source a published company-managed
# agent. That changes the calculus on both sides of the two manifests above:
#
# - an id that resolves is not an id the installer may use: the publisher's
#   channel-bot integration in ``channel_config`` would let the installer
#   publish through a bot they do not own, and its handover targets or
#   supervised agents would give the copy reach into agents that are not
#   theirs - those are dropped, as the bundle drops them;
# - references to catalog resources are re-keyed by the route onto what the
#   installer ends up with (a company-managed row bound as is, a private one
#   recreated as the installer's own copy, a company-managed one narrowed to
#   an audience the installer is not in left out - a knowledge base recreated
#   empty), so the id-keyed maps that ride on them (``mcp_allowed_tools``,
#   ``skill_load_modes``, ``knowledge_base_permissions``) are re-keyed with
#   them;
# - every field that NARROWS what the copy may do travels verbatim - a clone
#   that dropped ``tool_deny_list`` or ``human_in_the_loop`` would silently
#   widen what a published agent does in the installer's account.
#
# The hand-written kwargs this replaced carried 16 of the ~70 columns.

#: Agent columns carried verbatim (deep-copied) onto a catalog clone.
AGENT_CATALOG_CLONE_FIELDS: tuple[str, ...] = (
    # Same instance: the template the source was created from still exists.
    "template_slug",
    "description",
    "icon",
    # Carried, then rewritten by the route onto the variables it clones.
    "persona",
    "output_format",
    "output_schema",
    "accept_caller_output_format",
    # Provider NAMES resolved per user at runtime: the installer's own
    # OpenCTI / OpenAEV connection, never the publisher's.
    "integration_template_bindings",
    # Narrows those providers to their read tools: carried fail-closed.
    "integration_template_read_only",
    # Everything that narrows tool access, carried fail-closed.
    "integration_allowed_tools",
    # Built-in group ids and function names are portable (no instance id).
    "builtin_allowed_tools",
    "tool_deny_list",
    "integration_element_bindings",
    # Which of those selections is a boundary: dropping it would
    # install every enforced selection as guidance.
    "integration_element_enforcement",
    "builtin_tool_ids",
    # Runner selectors narrow the machine scope; a runner the installer cannot
    # see is filtered out at run time, so a publisher's private runner id
    # targets nothing (the fail-closed default the bundle uses too).
    "runner_ids",
    "runner_labels",
    # Free text: how to delegate. The targets themselves are dropped below.
    "handover_prompt",
    # Whether the agent may split its work across copies of itself: no id.
    "allow_self_fanout",
    "tags",
    "supported_intents",
    "supported_verticals",
    "config",
    "llm_provider",
    # Platform-wide LLM configuration: resolves for every user of the instance.
    "llm_provider_id",
    "llm_model",
    # Portable: it names the platform routing model, not one instance's model.
    "llm_model_source",
    "temperature",
    "reasoning_effort",
    # Carried, then clamped by the route to the shared iteration ceiling.
    "max_iterations",
    # Carried: bounded at run time by the installer's platform ceilings.
    "wait_policy",
    "organization_context_mode",
    "organization_context_categories",
    # Carried, then lowered by the route to what the INSTALLER may hold
    # (``lower_budget_to_ceiling``): the publisher typed it, not the installer,
    # so it is lowered rather than refused - the duplicate / import treatment.
    "budget_config",
    # Carried, then rewritten by the route onto the variables it clones.
    "guardrails",
    "memory_short_term_enabled",
    "memory_medium_term_enabled",
    "memory_persistent_enabled",
    "memory_user_enabled",
    # The approval gate: dropping it would install a gated agent ungated.
    "human_in_the_loop",
    "human_in_the_loop_prompt",
    "approve_tool_calls",
    "hide_runs",
    "disable_chat",
    "web_suggested_prompts",
)

#: Agent columns NOT carried verbatim onto a catalog clone, with the reason.
#: "Re-keyed" entries are set by the route through overrides.
AGENT_CATALOG_CLONE_SKIP_FIELDS: Mapping[str, str] = MappingProxyType(
    {
        # --- Platform Health (#8254): consent and identity never travel ---
        "health_enrolled": "Consent is given by the managers of each agent, never inherited.",
        "health_content_opt_in": "Consent is given by the managers of each agent, never inherited.",
        "health_role": "Only the seeded fleet acts as the Platform Health identity; a copy never does.",
        # --- Identity ---
        "id": "Primary key of a new row.",
        "created_at": "Timestamps belong to the clone's own lifecycle.",
        "updated_at": "Timestamps belong to the clone's own lifecycle.",
        "name": "The installer names the clone (setup body, or '<source> (copy)').",
        "slug": "Unique per agent; the new row gets its own.",
        # --- Ownership and sharing: the clone is the installer's, privately ---
        "user_id": "The clone is owned by the installer.",
        "is_company_managed": "A catalog install is always personal.",
        "is_sandbox": "A catalog install is an ordinary agent, never a sandbox copy (#8255).",
        "hide_activity_from_non_admins": "An administrator's switch on this instance (#11624), never installed.",
        "customer_notes": "The customer layer of a built-in on this instance (#914), never installed.",
        "customer_fields": "Seed bookkeeping of a built-in on this instance (#914), never installed.",
        "shared_group_ids": "The publisher's sharing grants are never inherited.",
        "shared_group_permissions": "Paired with shared_group_ids.",
        "shared_user_ids": "The publisher's sharing grants are never inherited.",
        "shared_user_permissions": "Paired with shared_user_ids.",
        "access_restricted": "Only meaningful on a company-managed agent.",
        # --- Platform state ---
        "is_builtin": "Built-in agents are seeded; a clone is a normal agent.",
        "is_public_catalog": "Publishing is an explicit action on a company-managed agent.",
        "catalog_include_personal_tools": "The publisher's download choice, paired with is_public_catalog.",
        "enabled": "A fresh clone starts enabled (the model default).",
        # --- Public exposure ---
        "web_enabled": "Public web exposure must be enabled explicitly on the clone.",
        "web_uploads_enabled": "Visitor uploads on the public widget must be allowed explicitly on the clone.",
        "web_token": "Live credential in public embed URLs - never copied.",
        # --- Channels: the publisher's bots ---
        "channel_config": (
            "Holds the publisher's channel-bot integration UUIDs, which resolve "
            "on this instance: carrying them would let the installer publish "
            "through a bot they do not own."
        ),
        "available_channel_types": (
            "Forced to web by the route: external channels stay opt-in once "
            "the installer has configured their own bots."
        ),
        # --- Re-keyed by the route onto what the installer ends up with ---
        # "Bindable" below means company-managed AND visible to the installer
        # through the three-tier predicate. A company-managed row narrowed to
        # an audience (``access_restricted``) the installer is not in is never
        # bound - a run uses a bound row's credentials without re-checking who
        # owns the agent - and its content is never copied either: it is not
        # the installer's to read. "Private" means not company-managed.
        "integration_bindings": (
            "Re-bound: bindable integrations of the source are kept, then the "
            "installer's own per-provider choice from the setup body wins - "
            "refused unless the installer may use it. Any other integration "
            "of the source never travels."
        ),
        "integration_lending": (
            "The publisher's lending of their own integrations to the people "
            "the agent is shared with: never the installer's grant (#8824)."
        ),
        "integration_read_only": (
            "Re-keyed with integration_bindings (``carried_read_only``): "
            "a kept binding stays read-only, and the installer's choice for a "
            "provider takes that provider's read-only mark, so a replacement "
            "never gains write tools the publisher withheld."
        ),
        "integration_signatures": (
            "The publisher's signature choices for its bindings: each binding "
            "of the clone inherits its integration's Email signature setting."
        ),
        "integration_live_writes": (
            "The publisher's Live writes choices for its bindings: each OpenCTI "
            "binding of the clone inherits its integration's Live writes setting."
        ),
        "browser_profile_ids": (
            "A saved login of the publisher's team: an installer binds the "
            "browser profiles they may use on the clone, deliberately."
        ),
        "browser_profile_read_only": "Paired with browser_profile_ids, which a clone does not carry.",
        "knowledge_base_ids": (
            "Re-keyed: bindable KBs are bound, any other recreated as an empty "
            "KB owned by the installer."
        ),
        "knowledge_base_permissions": (
            "Re-keyed onto the recreated KBs only. A write grant onto a bound "
            "company-managed KB is not inherited - granting write on shared "
            "content is the installer's own, deliberate act."
        ),
        "mcp_server_ids": (
            "Re-keyed: bindable servers are bound, a private one cloned "
            "without secrets (a credential in its arguments or URL masked), "
            "a narrowed one left out."
        ),
        "mcp_allowed_tools": (
            "Re-keyed onto the bound / cloned server ids - dropping it would "
            "widen MCP tool access to everything the server offers."
        ),
        "custom_tool_ids": "Re-keyed: bindable custom tools are bound, any other skipped.",
        "skill_ids": (
            "Re-keyed: bindable skills are bound, a private one cloned, a "
            "narrowed one left out."
        ),
        "skill_load_modes": "Re-keyed onto the bound / cloned skill ids.",
        "prompt_ids": (
            "Re-keyed: bindable prompts are bound, a private one cloned, a "
            "narrowed one left out."
        ),
        # --- Reach into the publisher's other agents: dropped ---
        "handover_agent_ids": (
            "The publisher's peer agents: a clone must not delegate into agents "
            "that are not the installer's."
        ),
        "handover_types": "Keyed by handover_agent_ids, which is dropped.",
        "handover_conditions": "Keyed by handover_agent_ids, which is dropped.",
        "work_item_owner_agent_ids": "The publisher's agents; the roster is cleared.",
        "trace_supervision_target_agent_ids": (
            "Would let the clone inspect the traces of the publisher's agents; "
            "cleared."
        ),
        "coordination_access": (
            "Re-keyed by the route: the scopes are kept, the agents it lists "
            "(the publisher's) are not (``portable_coordination_access``)."
        ),
        # --- Pipeline refs: dropped together, as the bundle does ---
        "prepare_tool_ref": "Names a tool of the publisher by id/provider.",
        "finalize_tool_ref": "Same as prepare_tool_ref.",
        "skip_llm": (
            "Tied to the pipeline refs above: skip_llm=True without them would "
            "install an agent that answers nothing."
        ),
    }
)

#: Assignment columns carried verbatim (deep-copied) onto a catalog clone's
#: assignments.
ASSIGNMENT_CATALOG_CLONE_FIELDS: tuple[str, ...] = (
    "hide_runs",
    "name",
    "description",
    "trigger_type",
    # Carried, then rewritten by the route onto the variables it clones. The
    # activation gate: dropping it would run a gated assignment unconditionally.
    "activation_prompt",
    # The tool-capable gate, for the same reason; ``{{agent.id}}`` in its
    # arguments names the clone's own agent.
    "activation_check",
    "prompt",
    "delay",
    "integration_allowed_tools",
    # Built-in group ids and function names are portable (no instance id).
    "builtin_allowed_tools",
    "integration_element_bindings",
    "integration_element_enforcement",
    "runner_ids",
    "use_knowledge_bases",
    "write_knowledge_base_enabled",
    "ai_label",
    "ai_label_prompt",
    "notify_on_complete",
    "show_in_digest",
    "max_iterations",
    "timeout_seconds",
    # Carried: bounded at run time by the installer's platform ceilings.
    "wait_policy",
    "organization_context_mode",
    "organization_context_categories",
    "retry_on_failure",
    "max_retries",
    "human_in_the_loop",
    "human_in_the_loop_prompt",
    "dry_run",
    # The self-trigger loop guard (default OFF): a clone without it can react
    # to its own writes forever once enabled.
    "ignore_self_triggered_events",
    "manual_trigger_parameters",
)

#: Assignment columns NOT carried verbatim onto a catalog clone, with the reason.
ASSIGNMENT_CATALOG_CLONE_SKIP_FIELDS: Mapping[str, str] = MappingProxyType(
    {
        "id": "Primary key of a new row.",
        "created_at": "Timestamps belong to the clone's own lifecycle.",
        "updated_at": "Timestamps belong to the clone's own lifecycle.",
        "agent_id": "The clone belongs to the cloned agent.",
        "user_id": "Owned by the installer.",
        "is_company_managed": "The cloned agent is personal, so its assignments are too.",
        "run_as_user_id": (
            "Execution identity of a company-managed agent: runs would execute "
            "with ANOTHER user's integrations. Never inherited by a personal copy."
        ),
        "run_as_all_users": "Same admin-only, company-managed-only concept as run_as_user_id.",
        "trigger_config": (
            "Copied by the route without the publisher's webhook signing secret "
            "(webhook clones get a fresh one, SEC-5) and without the keys that "
            "name the publisher's rows (TRIGGER_CONFIG_INTERNAL_ID_KEYS); the "
            "polled integration is kept only when the clone binds it."
        ),
        "tool_sources": (
            "Re-keyed: entries naming a server by id are mapped onto the bound / "
            "cloned server ids, names pass through, and an id the clone does "
            "not bind becomes REDACTED_MCP_SERVER_ID - an empty list means "
            "every server of the agent, so dropping it would widen the scope."
        ),
        "integration_bindings": (
            "Re-keyed by the route, keeping its tri-state: NULL stays inherit, "
            "{} stays none, a subset is mapped onto the clone's re-bound "
            "integrations and becomes {} (never NULL) when nothing survives."
        ),
        "channel_config": "The publisher's channel bots; the clone inherits the agent's (NULL).",
        "read_knowledge_base_ids": "Re-keyed onto the clone's knowledge bases.",
        "write_knowledge_base_ids": "Re-keyed onto the clone's knowledge bases.",
        "target_knowledge_base_id": "Deprecated single-KB target; re-keyed like the lists.",
        "next_assignment_id": (
            "Chain target inside the source agent; re-keyed by the route onto "
            "the cloned counterpart, dropped when it points elsewhere."
        ),
        "enabled": "Catalog installs start disabled for safety.",
        "paused_at": "Pause provenance describes the source's pause, not the clone's.",
        "paused_reason": "Pause provenance describes the source's pause, not the clone's.",
        "paused_by_user_id": "Pause provenance describes the source's pause, not the clone's.",
        "paused_by_label": "Pause provenance describes the source's pause, not the clone's.",
        "paused_details": "Pause provenance describes the source's pause, not the clone's.",
        "last_paused_at": (
            "Streak boundary over the source's run history; the clone "
            "has no runs of its own."
        ),
        "execution_count": "Run history belongs to the source.",
        "events_filtered_count": "Run history belongs to the source.",
        "last_triggered_at": "Run history belongs to the source.",
        "schedule_resumed_at": "Schedule anchor of the source's last resume.",
    }
)


def build_agent_catalog_clone_kwargs(
    source: Agent, **overrides: Any
) -> dict[str, Any]:
    """Return ``Agent(...)`` kwargs for a catalog clone of ``source``.

    Same contract as :func:`build_agent_copy_kwargs` (deep copies, NULL kept as
    NULL, overrides validated against the real columns), over the catalog
    manifest. The caller supplies identity, ownership and every re-keyed field.
    """
    _reject_unknown_columns(overrides, agent_mapped_columns(), "Agent")
    kwargs: dict[str, Any] = {
        name: deepcopy(getattr(source, name)) for name in AGENT_CATALOG_CLONE_FIELDS
    }
    kwargs.update(overrides)
    return kwargs


def build_assignment_catalog_clone_kwargs(
    source: Assignment, **overrides: Any
) -> dict[str, Any]:
    """Return ``Assignment(...)`` kwargs for a catalog clone's assignment."""
    _reject_unknown_columns(overrides, assignment_mapped_columns(), "Assignment")
    kwargs: dict[str, Any] = {
        name: deepcopy(getattr(source, name))
        for name in ASSIGNMENT_CATALOG_CLONE_FIELDS
    }
    kwargs.update(overrides)
    return kwargs


def rekey_id_map(value: Any, id_map: Mapping[str, str]) -> dict[str, Any] | None:
    """Re-key an id-keyed map (``mcp_allowed_tools``, ``skill_load_modes``, ...)
    through ``id_map``; entries whose key is not in the map are dropped."""
    if not isinstance(value, Mapping):
        return None
    out = {
        id_map[str(key)]: deepcopy(item)
        for key, item in value.items()
        if str(key) in id_map
    }
    return out or None


def rekey_id_list(value: Any, id_map: Mapping[str, str]) -> list[str] | None:
    """Re-key an id list through ``id_map``, keeping the NULL / list distinction
    (NULL is "inherit" on the tri-state refinement columns); unmapped ids are
    dropped."""
    if value is None:
        return None
    if not isinstance(value, list):
        return None
    return [id_map[str(item)] for item in value if str(item) in id_map]


# --- Trigger configuration ------------------------------------------
#
# ``Assignment.trigger_config`` is free-form JSON - every trigger type adds its
# own keys - so it travels whole: a key allow-list would silently drop a
# trigger's own settings. The keys that name a row of THIS instance by id are
# handled by name instead, and ``tests/test_catalog_public_download.py`` pins
# every key the frontend's ``TriggerConfig`` writes against the two
# collections below, so a new id-bearing key cannot ship without a decision.

#: ``trigger_config`` keys that name a row of this instance, with the reason
#: they must not follow a copy into another account. Absent, each falls back to
#: the copy's own row.
TRIGGER_CONFIG_INTERNAL_ID_KEYS: Mapping[str, str] = MappingProxyType(
    {
        "integration_id": (
            "The integration an integration_event trigger polls. The poller "
            "reads a non-OAuth integration whoever owns the agent, so the "
            "publisher's would feed their events into the copy; absent, it "
            "polls the copy's own binding for the provider."
        ),
        "owner_agent_id": (
            "The agent whose work-item queue the inbox drains - the source or "
            "one of the publisher's agents; absent, the copy drains its own."
        ),
        "fired_event_id": (
            "The platform event of this instance that fired a one-off watch "
            "(platform_event with once). Kept, it would leave the copy's watch "
            "spent before it ever saw an event; absent, the copy's watch is armed."
        ),
        "fired_at": (
            "When this instance's one-off watch fired, read with fired_event_id; absent, the copy's watch is armed."
        ),
        "opencti_provisioning": (
            "The ids of the trigger and notifier this instance provisioned in "
            "OpenCTI (services/opencti_trigger_provisioning.py); absent, the "
            "copy's provisioning tick creates its own from provision."
        ),
        "openaev_provisioning": (
            "The ids of the objects this instance provisioned in OpenAEV "
            "(services/openaev_trigger_provisioning.py); absent, the copy's "
            "provisioning tick creates its own from provision."
        ),
        "jira_webhook": (
            "The ids of the Jira dynamic webhook this instance registered "
            "(services/jira_webhooks.py); absent, the copy's provisioning tick "
            "registers its own."
        ),
    }
)

#: The other ``trigger_config`` keys the platform writes: settings of the
#: trigger itself, portable. (``webhook_secret`` is a live credential, removed
#: by ``strip_webhook_secret``.)
TRIGGER_CONFIG_PORTABLE_KEYS: tuple[str, ...] = (
    "cron",
    "path",
    "provider",
    "event",
    # The key the agent builder stored the event under earlier, read
    # only on a trigger marked exact.
    "event_type",
    "events",
    "event_matching",
    "filter",
    "correlation_prefix",
    "priority_min",
    "notify_via",
    # The assignment queue's per-assignment cap and ordering payload path
    # (``services/assignment_dispatch.py``).
    "max_concurrent_runs",
    "ordering_key",
    # A work_item inbox's express lane: extra slots for some correlation
    # prefixes (``services/assignment_dispatch.py``).
    "express_lane",
    # A platform_event trigger's subscription scope (own or all) and its
    # one-off watch switch (``services/platform_events.py``), and its
    # deadline: a date-time, no row of this instance.
    "scope",
    "once",
    "expires_at",
    # The OpenCTI / OpenAEV trigger an assignment declares (fleet, purpose,
    # filter), provisioned by each instance on its own platform.
    "provision",
    # CTEM-08: keep a relationship only when its source runs in the profile
    # (``services/ctem_trigger_rules.py``), read against each instance's own profile.
    "runs_in_profile",
    # CTEM-08: hold a PIR playbook delivery to the policy's PIR score thresholds,
    # read against each instance's own profile and OpenCTI.
    "pir_min_score",
    # A platform_event trigger's declared role, "observer" or "worker": an
    # observer of case events is never counted as the case worker
    # (``services/platform_events.py``); a word, no row of this instance.
    "role",
)


def strip_trigger_config_internal_ids(value: Any) -> Any:
    """``trigger_config`` without its webhook secret and internal-id keys.

    A value that is not an object is returned unchanged: the importer refuses
    it with a precise 400, which is better than a silent ``{}``.
    """
    if not isinstance(value, Mapping):
        return value
    out = strip_webhook_secret(dict(value)) or {}
    return {k: v for k, v in out.items() if k not in TRIGGER_CONFIG_INTERNAL_ID_KEYS}


# --- Assignment integration override across a bundle ----------------
#
# ``Assignment.integration_bindings`` is tri-state: NULL inherits every
# integration of the agent, ``{}`` means none, a subset names integration ids -
# which never exist on the importing install. The bundle carries the MODE (and
# the provider names of a subset, for the import report), never the ids, and
# the import fails closed: a subset arrives as "none", never widened to
# "inherit". A bundle that predates the key imports as "inherit", as before.

#: Bundle key, on each assignment entry, carrying the override's mode.
ASSIGNMENT_INTEGRATION_OVERRIDE_KEY = "integration_override"
INTEGRATION_OVERRIDE_INHERIT = "inherit"
INTEGRATION_OVERRIDE_NONE = "none"
INTEGRATION_OVERRIDE_SUBSET = "subset"


def export_integration_override(value: Any) -> dict[str, Any]:
    """The portable form of an assignment's ``integration_bindings``."""
    if value is None:
        return {"mode": INTEGRATION_OVERRIDE_INHERIT}
    if isinstance(value, Mapping) and value:
        return {
            "mode": INTEGRATION_OVERRIDE_SUBSET,
            "providers": sorted(str(provider) for provider in value),
        }
    return {"mode": INTEGRATION_OVERRIDE_NONE}


def import_integration_override(
    assignment_data: Mapping[str, Any], assignment_name: str
) -> tuple[dict | None, str | None]:
    """``(integration_bindings, report line or None)`` for an imported assignment.

    Absent or null: the bundle predates the key, so the assignment inherits as
    it always did. "none" stays none. A subset, or a value this importer does
    not recognise, arrives as none - its integrations must be re-selected.
    """
    override = assignment_data.get(ASSIGNMENT_INTEGRATION_OVERRIDE_KEY)
    if override is None:
        return None, None
    mode = override.get("mode") if isinstance(override, Mapping) else None
    if mode == INTEGRATION_OVERRIDE_INHERIT:
        return None, None
    if mode == INTEGRATION_OVERRIDE_NONE:
        return {}, None
    if mode == INTEGRATION_OVERRIDE_SUBSET:
        providers = override.get("providers")
        named = (
            ", ".join(str(p) for p in providers)
            if isinstance(providers, list) and providers
            else "specific integrations"
        )
        return {}, (
            f"Assignment '{assignment_name}' was limited to specific integrations "
            f"({named}) that do not exist on this install: it was imported with "
            "no integrations - re-select them in its settings."
        )
    return {}, (
        f"Assignment '{assignment_name}' carries an integration setting this "
        "install does not recognise: it was imported with no integrations - "
        "re-select them in its settings."
    )


# --- Public catalog download ----------------------------------------
#
# A catalog download is an export bundle handed to any authenticated user of
# THIS instance, where every id of the publisher's rows still resolves - so the
# export's cross-instance reasoning ("an id dangles on the importing instance")
# does not hold. What it may carry is an ALLOW-LIST of its own, per bundle
# section: anything the export builder emits that is not listed below is
# dropped, so a key added to the export tomorrow stays out of public downloads
# until it is classified here. ``tests/test_catalog_public_download.py`` runs
# the real export builder and fails on any key that is not.
#
# Ids are handled two ways, on purpose:
#
# - ids the import never applies (export-only rosters, the assignments' KB
#   refinements, handover maps keyed by the publisher's agents) are dropped;
# - selectors that NARROW a scope, where empty means "everything" (runner ids,
#   an assignment's MCP servers), are redacted: each id is replaced by a
#   placeholder that resolves to nothing, so the imported copy is scoped to no
#   machine / no server rather than widened to all of them.

#: Placeholder written in place of each runner id in a public download.
REDACTED_RUNNER_ID = "redacted-runner"

#: Placeholder written in place of each MCP server id an assignment's
#: ``tool_sources`` names outside the bundle. A syntactically valid UUID that no
#: row is ever given (the nil UUID): ``load_mcp_tools`` parses every entry with
#: ``uuid.UUID(...)``, so a free-text placeholder failed the whole MCP load of
#: the run - valid servers included - instead of matching no server. The
#: catalog clone writes the same placeholder for an id it cannot map.
REDACTED_MCP_SERVER_ID = "00000000-0000-0000-0000-000000000000"

#: Top-level sections of the bundle a public download carries.
PUBLIC_DOWNLOAD_BUNDLE_KEYS: tuple[str, ...] = (
    "version",
    "export_type",
    "exported_at",
    "agent",
    "assignments",
    "custom_tools",
    "mcp_servers",
    "integration_providers",
    "skills",
    "prompts",
    "knowledge_base_notes",
    "variables",
)

#: Agent-section keys a public download carries. Explicit rather than derived
#: from :data:`AGENT_BUNDLE_PORTABLE_FIELDS`: "portable across instances" is
#: not "safe to hand to any user of this one".
PUBLIC_DOWNLOAD_AGENT_FIELDS: tuple[str, ...] = (
    "name",
    "description",
    "icon",
    "persona",
    "output_format",
    "output_schema",
    "accept_caller_output_format",
    "integration_template_bindings",
    # Provider keys that narrow those bindings to read tools: no instance id,
    # carried fail-closed so a download never gains their writes.
    "integration_template_read_only",
    "integration_allowed_tools",
    # Built-in group ids and function names are portable (no instance id).
    "builtin_allowed_tools",
    "integration_element_bindings",
    "integration_element_enforcement",
    "tool_deny_list",
    "builtin_tool_ids",
    # Redacted, not dropped (see the section comment).
    "runner_ids",
    "runner_labels",
    "handover_prompt",
    "allow_self_fanout",
    # Its scopes only: the agent ids it lists are dropped below.
    "coordination_access",
    "tags",
    "supported_intents",
    "supported_verticals",
    "config",
    "llm_provider",
    "llm_model",
    # Portable: it names the platform routing model, not one instance's model.
    "llm_model_source",
    "temperature",
    "reasoning_effort",
    "max_iterations",
    "wait_policy",
    "organization_context_mode",
    "organization_context_categories",
    "budget_config",
    "guardrails",
    "memory_short_term_enabled",
    "memory_medium_term_enabled",
    "memory_persistent_enabled",
    "memory_user_enabled",
    "human_in_the_loop",
    "human_in_the_loop_prompt",
    "hide_runs",
    "disable_chat",
    "web_suggested_prompts",
)

#: Agent-section keys the export emits and a public download drops, with why.
PUBLIC_DOWNLOAD_DROPPED_AGENT_FIELDS: Mapping[str, str] = MappingProxyType(
    {
        "approve_tool_calls": "The downloading install's approval posture applies.",
        "work_item_owner_agent_ids": (
            "The publisher's peer agents, which resolve on this instance; the "
            "import clears the roster anyway."
        ),
        "trace_supervision_target_agent_ids": (
            "The publisher's agents whose traces the source inspects; cleared "
            "on import anyway."
        ),
        "handover_types": (
            "Keyed by the publisher's peer-agent ids, which resolve on this "
            "instance. The import has no handover targets to apply it to "
            "(handover_agent_ids never travels), so it carries only the ids."
        ),
        "handover_conditions": "Keyed like handover_types; dropped for the same reason.",
    }
)

#: Assignment keys a public download carries.
PUBLIC_DOWNLOAD_ASSIGNMENT_FIELDS: tuple[str, ...] = (
    "name",
    "description",
    "trigger_type",
    # Without TRIGGER_CONFIG_INTERNAL_ID_KEYS and the webhook secret.
    "trigger_config",
    "activation_prompt",
    "activation_check",
    "prompt",
    "delay",
    # Redacted, not dropped (see the section comment).
    "tool_sources",
    # The override's mode and provider names only - never an integration id.
    ASSIGNMENT_INTEGRATION_OVERRIDE_KEY,
    "use_knowledge_bases",
    "ai_label",
    "ai_label_prompt",
    "notify_on_complete",
    "show_in_digest",
    "human_in_the_loop",
    "human_in_the_loop_prompt",
    "max_iterations",
    "timeout_seconds",
    "retry_on_failure",
    "max_retries",
    "dry_run",
    "ignore_self_triggered_events",
    "integration_allowed_tools",
    # Built-in group ids and function names are portable (no instance id).
    "builtin_allowed_tools",
    "integration_element_bindings",
    "integration_element_enforcement",
    "hide_runs",
    "manual_trigger_parameters",
    "enabled",
    "write_knowledge_base_enabled",
    # Redacted, not dropped (see the section comment).
    "runner_ids",
)

#: Assignment keys the export emits and a public download drops, with why.
PUBLIC_DOWNLOAD_DROPPED_ASSIGNMENT_FIELDS: Mapping[str, str] = MappingProxyType(
    {
        "read_knowledge_base_ids": (
            "Knowledge-base ids of the publisher: knowledge bases are not "
            "exported, and the import never applies the refinement."
        ),
        "write_knowledge_base_ids": "Same as read_knowledge_base_ids.",
    }
)

#: Per-entry keys a public download carries in each list section.
PUBLIC_DOWNLOAD_ENTRY_FIELDS: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        "custom_tools": (
            "name",
            "description",
            "icon_url",
            "input_schema",
            "js_function",
            "language",
            "timeout",
            "tags",
        ),
        "mcp_servers": (
            # A bundle-local reference an assignment's ``tool_sources`` may name
            # the server by - never an id of this instance.
            "ref",
            "name",
            "transport",
            "command",
            # A credential passed as an argument or in the URL is masked
            # (``redact_secret_args`` / ``redact_secret_text``).
            "args",
            "url",
            "tags",
            "allowed_tools",
        ),
        "integration_providers": ("provider", "name", "type"),
        "skills": (
            "name",
            "description",
            "url",
            "content",
            "compatibility",
            "license",
            "tags",
            "load_mode",
        ),
        "prompts": ("name", "description", "content", "tags"),
        "knowledge_base_notes": ("name", "description", "note"),
        # ``id`` travels: the prompts reference ``{{var_id.<id>}}`` and the
        # import remaps them through it. ``value`` is only ever present for a
        # variable that is not secret.
        "variables": ("id", "name", "key", "description", "tags", "is_secret", "value"),
    }
)


def _redact_runner_ids(value: Any) -> Any:
    if not isinstance(value, list) or not value:
        return value
    return [REDACTED_RUNNER_ID for _ in value]


def _redact_tool_source_ids(value: Any) -> Any:
    if not isinstance(value, list):
        return value
    return [
        REDACTED_MCP_SERVER_ID if is_valid_uuid(str(entry)) else entry for entry in value
    ]


def _project(entry: Any, fields: Iterable[str]) -> dict[str, Any] | None:
    """The keys of ``entry`` listed in ``fields``; ``None`` for a non-object."""
    if not isinstance(entry, Mapping):
        return None
    return {key: entry[key] for key in fields if key in entry}


def strip_internal_ids_for_public_download(bundle: dict[str, Any]) -> dict[str, Any]:
    """Project an export bundle onto what a public download may carry.

    See the section comment: an allow-list per section, with the ids the
    import never applies dropped and the scope-narrowing ones redacted. The
    input is not mutated.
    """
    source = deepcopy(bundle)
    out: dict[str, Any] = {
        key: source[key] for key in PUBLIC_DOWNLOAD_BUNDLE_KEYS if key in source
    }

    if "agent" in out:
        agent = _project(out["agent"], PUBLIC_DOWNLOAD_AGENT_FIELDS)
        if agent is None:
            del out["agent"]
        else:
            if "runner_ids" in agent:
                agent["runner_ids"] = _redact_runner_ids(agent["runner_ids"])
            if "coordination_access" in agent:
                # The publisher's agents, which resolve on this instance.
                from app.services.company_managed_writes import (
                    portable_coordination_access,
                )

                agent["coordination_access"] = portable_coordination_access(
                    agent["coordination_access"]
                )
            out["agent"] = agent

    if isinstance(out.get("assignments"), list):
        assignments: list[dict[str, Any]] = []
        for entry in out["assignments"]:
            assignment = _project(entry, PUBLIC_DOWNLOAD_ASSIGNMENT_FIELDS)
            if assignment is None:
                continue
            if "trigger_config" in assignment:
                assignment["trigger_config"] = strip_trigger_config_internal_ids(
                    assignment["trigger_config"]
                )
            if "runner_ids" in assignment:
                assignment["runner_ids"] = _redact_runner_ids(assignment["runner_ids"])
            if "tool_sources" in assignment:
                assignment["tool_sources"] = _redact_tool_source_ids(
                    assignment["tool_sources"]
                )
            assignments.append(assignment)
        out["assignments"] = assignments

    for section, fields in PUBLIC_DOWNLOAD_ENTRY_FIELDS.items():
        if isinstance(out.get(section), list):
            out[section] = [
                projected
                for entry in out[section]
                if (projected := _project(entry, fields)) is not None
            ]
    if isinstance(out.get("mcp_servers"), list):
        for server in out["mcp_servers"]:
            if "args" in server:
                server["args"] = redact_secret_args(server["args"])
            if "url" in server:
                server["url"] = redact_secret_text(server["url"])
    return out
