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

"""Shared builtin tool builders and prompt helpers for the agentic loop.

This package hosts every built-in tool builder (code interpreter, image and
file generation, KB search/store, agent memory, web fetch, browser, STIX
bundle validation, sub-agent handover, the agent builder), the MCP/custom
tool loaders, integration tool filtering, and the prompt/identity helpers.
These factory functions eliminate duplication across ``agent_chat``,
``general_chat``, and ``assignment_engine`` by providing a single source of
truth for each built-in tool's definition, description, and registration.

Every function returns a ``(list[ToolDefinition], ToolExecutor)`` tuple (or
*None* when the tool cannot be initialised) so callers can plug the result
straight into a :class:`CompositeToolExecutor`.

This package was mechanically split from the original single-file
``app/services/builtin_tools.py`` module.  This ``__init__`` is a
compatibility facade: every name that used to be importable from
``app.services.builtin_tools`` is re-exported here, so existing imports
keep working unchanged.
"""

from __future__ import annotations

import sys
from types import ModuleType
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.tools.base import CompositeToolExecutor, ToolDefinition

from . import (
    _shared,
    agent_builder,
    document_vision,
    evaluations,
    filtering,
    fimi_math,
    fimi_metrics,
    handover,
    kb,
    loaders,
    media,
    memory,
    prompting,
    shared_state,
    skills,
    stix,
    stix_comparator,
    stix_extraction,
    timeline_diff,
    wait,
    web,
    work_items,
)
from ._shared import (
    _TASK_ID_DISPLAY_BUDGET as _TASK_ID_DISPLAY_BUDGET,
)
from ._shared import (
    _TASK_ID_EXAMPLE as _TASK_ID_EXAMPLE,
)
from ._shared import (
    _bounded_display as _bounded_display,
)
from ._shared import (
    _invalid_task_id_message as _invalid_task_id_message,
)
from ._shared import (
    _is_uuid_like as _is_uuid_like,
)
from ._shared import (
    _persist_generated_file as _persist_generated_file,
)
from ._shared import (
    _platform_label as _platform_label,
)
from ._shared import (
    logger,
)
from ._shared import (
    persist_tool_artifact as persist_tool_artifact,
)
from .agent_builder import (
    _load_caller_user_for_builder as _load_caller_user_for_builder,
)
from .agent_builder import (
    _load_manageable_entity_for_builder as _load_manageable_entity_for_builder,
)
from .agent_builder import (
    _load_writable_agent_for_builder as _load_writable_agent_for_builder,
)
from .agent_builder import (
    _load_writable_assignment_for_builder as _load_writable_assignment_for_builder,
)
from .agent_builder import (
    _validate_id_arg as _validate_id_arg,
)
from .agent_builder import (
    build_agent_builder_tool,
)
from .computer_use import (
    build_computer_use_tools,
)
from .ctem import (
    CTEM_GROUP,
    build_ctem_tools,
    ctem_tools_for,
    offers_ctem_tools,
)
from .ctem_cases import (
    CTEM_CASE_TOOL_NAMES,
    build_ctem_case_tools,
    offers_ctem_case_tools,
)
from .ctem_read import (
    CTEM_READ_GROUP,
    CTEM_READ_TOOL_NAMES,
    build_ctem_read_tools,
    ctem_read_tools_for,
    offers_ctem_read_tools,
)
from .ctem_repair import (
    CTEM_REPAIR_GROUP,
    CTEM_REPAIR_TOOL_NAMES,
    build_ctem_repair_tools,
    ctem_repair_tools_for,
    offers_ctem_repair_tools,
)
from .ctem_signals import (
    CTEM_SIGNAL_TOOL_NAMES,
    build_ctem_signal_tools,
    offers_ctem_signal_tools,
)
from .custom_dashboard import (
    build_custom_dashboard_tools,
)
from .document_vision import (
    build_document_vision_tools,
)
from .evaluations import (
    build_evaluation_tools,
)
from .filtering import (
    AGENT_KEYED_BUILTIN_TOOL_IDS,
    agent_keyed_session_additions,
    apply_tool_deny_list,
    arsenal_findings_required,
    filter_builtin_tools,
    filter_integration_tools,
    instance_base_tool_name,
    is_advisory_forbidden_arsenal_action,
    is_openaev_arsenal_advisory_agent,
    is_openaev_orchestrator_agent,
    is_openaev_write_tool,
    is_orchestrator_forbidden_arsenal_action,
    is_tradecraft_writer_agent,
    merge_tool_allow_lists,
    narrow_orchestrator_arsenal_tools,
    orchestrator_may_delete_action,
    orchestrator_phase_gate_blocked,
    record_orchestrator_arsenal_authored,
    record_orchestrator_session_action,
    record_orchestrator_step_authored,
    reset_advisory_arsenal_read_only,
    reset_advisory_openaev_writes,
    reset_arsenal_findings_required,
    reset_orchestrator_arsenal_writes,
    reset_orchestrator_phase_counter,
    reset_orchestrator_session_actions,
    set_advisory_arsenal_read_only,
    set_advisory_openaev_writes,
    set_arsenal_findings_required,
    set_orchestrator_arsenal_writes,
    set_orchestrator_phase_counter,
    set_orchestrator_session_actions,
    strip_openaev_write_tools,
)
from .fimi_math import (
    build_fimi_math_tool,
)
from .fimi_metrics import (
    build_fimi_metrics_tool,
)
from .fleets import build_fleet_tools
from .flow_builder import build_flow_builder_tool
from .flow_lab import FLOW_LAB_READ_TOOLS, build_flow_lab_tools
from .frictions import build_friction_tools
from .handover import (
    agent_config_revision,
    build_sub_agent_tools,
    consult_refusal,
    consulted_peer_handover,
    delegated_parent_id,
    extract_visited_agents,
    format_handover_context_prompt,
    format_self_fanout_prompt,
    handover_cycle_error,
    handover_peer_tag,
    handover_peers_by_type,
    handover_tool_types,
    live_consult_target,
    normalize_visited_agents,
    offered_handover_types,
    owned_consult_target,
    owned_target_resolver,
    reachable_handover_peers,
    resolve_handover_agent,
    resolve_owned_delegation_target,
    self_fanout_enabled,
)
from .health_experiments import (
    build_health_experiment_tools,
)
from .health_quality import (
    build_health_diagnosis_tools,
)
from .html_render import (
    build_render_html_tool,
)
from .image_assets import (
    build_fetch_image_asset_tool,
)
from .investigation import (
    build_deep_investigation_tool,
)
from .kb import (
    build_kb_document_read_tools,
    build_kb_document_update_tool,
    build_kb_search_tool,
    build_kb_store_tool,
    format_knowledge_base_prompt,
    load_kb_names,
    load_write_locked_kb_ids,
)
from .learned_skill import (
    LEARNED_LESSON_TOOL,
    build_learned_lesson_tool,
    offers_learned_lesson_tool,
)
from .loaders import (
    _MCP_TOOL_NAMESPACE_SEPARATOR as _MCP_TOOL_NAMESPACE_SEPARATOR,
)
from .loaders import (
    _deduplicate_namespaced_tools as _deduplicate_namespaced_tools,
)
from .loaders import (
    cleanup_mcp_manager,
    format_mcp_auth_broken_prompt,
    format_mcp_unavailable_prompt,
    load_custom_and_mcp_tools,
    load_custom_tools,
    load_mcp_tools,
)
from .media import (
    DOCX_CONTENT_TYPE,
    PDF_RENDER_FORMAT,
    PDF_RENDER_MARGIN,
    build_code_interpreter_tool,
    build_generate_file_tool,
    build_image_generation_tool,
)
from .media import (
    _render_text_to_docx as _render_text_to_docx,
)
from .media import (
    _render_text_to_pdf as _render_text_to_pdf,
)
from .memory import (
    build_memory_search_tool,
    build_memory_store_tool,
)
from .memory_steward import (
    MEMORY_STEWARD_SUITE_ID,
    build_memory_steward_tools,
)
from .organization_context import (
    ORGANIZATION_CONTEXT_TOOL,
    build_organization_context_tool,
)
from .organization_context_write import (
    ADMIN_BOUND_BUILTIN_TOOL_IDS,
    ORGANIZATION_CONTEXT_WRITE_GROUP,
    ORGANIZATION_UPDATE_TOOL,
    build_organization_context_write_tools,
    offers_organization_context_write,
    refused_admin_bound_ids,
)
from .organization_onboarding import (
    ORGANIZATION_ONBOARDING_TOOL,
    build_organization_onboarding_tool,
)
from .organization_proposals import (
    ORGANIZATION_PROPOSAL_TOOL,
    build_organization_proposal_tool,
    offers_organization_proposals,
)
from .phone import (
    build_phone_tools,
)
from .platform_insights import (
    PLATFORM_INSIGHTS_SUITE_ID,
    build_platform_insights_tools,
)
from .presentation import (
    build_presentation_tools,
    ensure_deck_tools,
)
from .prompting import (
    ACTING_FOR_VERB,
    PLATFORM_IDENTITY_KEYS,
    build_agent_context,
    build_identity_context,
    format_ctem_vertical_line,
    format_datetime_prompt,
    format_element_bindings_prompt,
    format_user_identity,
    load_agent_prompts,
    serialize_messages,
)
from .prompting import (
    _UserLike as _UserLike,
)
from .report import (
    build_report_tools,
)
from .scheduling import (
    SCHEDULING_TOOL_NAMES,
    build_scheduling_tools,
    refuse_core_tool_denials,
)
from .shared_state import (
    build_shared_state_tools,
)
from .skills import (
    MAX_CATALOG_DESCRIPTION_CHARS,
    ResolvedSkill,
    SkillAccessContext,
    SkillSubstitute,
    build_pinned_skills_prompt,
    build_skill_access_context,
    build_skill_tools,
    build_skills_catalog_prompt,
    resolve_agent_skills,
)
from .stix import (
    _fix_bundle_uuid_errors as _fix_bundle_uuid_errors,
)
from .stix import (
    _generate_standard_id_from_stix as _generate_standard_id_from_stix,
)
from .stix import (
    _get_pycti_stix2_helper as _get_pycti_stix2_helper,
)
from .stix import (
    _is_valid_stix_id as _is_valid_stix_id,
)
from .stix import (
    _rewrite_bundle_uuids as _rewrite_bundle_uuids,
)
from .stix import (
    _rewrite_refs_with_remap as _rewrite_refs_with_remap,
)
from .stix import (
    build_stix_bundle_validator_tool,
)
from .stix_comparator import (
    build_stix_comparator_tool,
)
from .stix_extraction import (
    build_stix_extraction_tool,
)
from .template_authoring import (
    build_template_authoring_tools,
)
from .templates import (
    build_template_search_tools,
)
from .timeline_diff import (
    build_timeline_diff_tool,
)
from .timers import build_timer_tools
from .traces import (
    build_trace_tools,
)
from .user_admin import (
    USER_ADMIN_GROUP,
    build_user_admin_tools,
    offers_user_admin,
)
from .video_studio import (
    build_video_studio_tools,
)
from .wait import (
    make_task_condition,
)
from .web import (
    build_browser_tool,
    build_web_fetch_tool,
)
from .work_items import (
    build_work_item_tools,
    format_coordination_tools_prompt,
    load_work_item_peers,
)
from .workset import (
    CONVERSATION_WORKSET_TTL,
    build_workset_tools,
    chat_workset_key,
)


def build_fimi_analytics_composite() -> tuple[list[ToolDefinition], CompositeToolExecutor]:
    """Build the ``fimi_analytics`` tool group as one composite executor.

    The group bundles four sub-tools (STIX comparator, timeline diff, FIMI
    math, FIMI metrics). This is the single source of truth for that set so the
    chat/assignment wiring sites and the built-in tool catalog never drift on
    which sub-tools belong to ``fimi_analytics``. Returns the combined
    ``(definitions, CompositeToolExecutor)`` tuple ready to plug into a caller's
    composite via ``add_tools`` (or to return directly).
    """
    from app.tools.base import CompositeToolExecutor

    composite = CompositeToolExecutor()
    for _builder in (
        build_stix_comparator_tool,
        build_timeline_diff_tool,
        build_fimi_math_tool,
        build_fimi_metrics_tool,
    ):
        _defs, _exec = _builder()
        composite.add_tools(_defs, _exec)
    return composite.all_tools, composite


__all__ = [
    "ACTING_FOR_VERB",
    "ADMIN_BOUND_BUILTIN_TOOL_IDS",
    "AGENT_KEYED_BUILTIN_TOOL_IDS",
    "CONVERSATION_WORKSET_TTL",
    "CTEM_CASE_TOOL_NAMES",
    "CTEM_GROUP",
    "CTEM_READ_GROUP",
    "CTEM_READ_TOOL_NAMES",
    "CTEM_REPAIR_GROUP",
    "CTEM_REPAIR_TOOL_NAMES",
    "CTEM_SIGNAL_TOOL_NAMES",
    "DOCX_CONTENT_TYPE",
    "FLOW_LAB_READ_TOOLS",
    "LEARNED_LESSON_TOOL",
    "MAX_CATALOG_DESCRIPTION_CHARS",
    "MEMORY_STEWARD_SUITE_ID",
    "ORGANIZATION_CONTEXT_TOOL",
    "ORGANIZATION_CONTEXT_WRITE_GROUP",
    "ORGANIZATION_ONBOARDING_TOOL",
    "ORGANIZATION_PROPOSAL_TOOL",
    "ORGANIZATION_UPDATE_TOOL",
    "PDF_RENDER_FORMAT",
    "PDF_RENDER_MARGIN",
    "PLATFORM_IDENTITY_KEYS",
    "PLATFORM_INSIGHTS_SUITE_ID",
    "SCHEDULING_TOOL_NAMES",
    "USER_ADMIN_GROUP",
    "ResolvedSkill",
    "SkillAccessContext",
    "SkillSubstitute",
    "agent_config_revision",
    "agent_keyed_session_additions",
    "apply_tool_deny_list",
    "arsenal_findings_required",
    "build_agent_builder_tool",
    "build_agent_context",
    "build_browser_tool",
    "build_code_interpreter_tool",
    "build_computer_use_tools",
    "build_ctem_case_tools",
    "build_ctem_read_tools",
    "build_ctem_repair_tools",
    "build_ctem_signal_tools",
    "build_ctem_tools",
    "build_custom_dashboard_tools",
    "build_deep_investigation_tool",
    "build_document_vision_tools",
    "build_evaluation_tools",
    "build_fetch_image_asset_tool",
    "build_fimi_analytics_composite",
    "build_fimi_math_tool",
    "build_fimi_metrics_tool",
    "build_fleet_tools",
    "build_flow_builder_tool",
    "build_flow_lab_tools",
    "build_friction_tools",
    "build_generate_file_tool",
    "build_health_diagnosis_tools",
    "build_health_experiment_tools",
    "build_identity_context",
    "build_image_generation_tool",
    "build_kb_document_read_tools",
    "build_kb_document_update_tool",
    "build_kb_search_tool",
    "build_kb_store_tool",
    "build_learned_lesson_tool",
    "build_memory_search_tool",
    "build_memory_steward_tools",
    "build_memory_store_tool",
    "build_organization_context_tool",
    "build_organization_context_write_tools",
    "build_organization_onboarding_tool",
    "build_organization_proposal_tool",
    "build_phone_tools",
    "build_pinned_skills_prompt",
    "build_platform_insights_tools",
    "build_presentation_tools",
    "build_render_html_tool",
    "build_report_tools",
    "build_scheduling_tools",
    "build_shared_state_tools",
    "build_skill_access_context",
    "build_skill_tools",
    "build_skills_catalog_prompt",
    "build_stix_bundle_validator_tool",
    "build_stix_comparator_tool",
    "build_stix_extraction_tool",
    "build_sub_agent_tools",
    "build_template_authoring_tools",
    "build_template_search_tools",
    "build_timeline_diff_tool",
    "build_timer_tools",
    "build_trace_tools",
    "build_user_admin_tools",
    "build_video_studio_tools",
    "build_web_fetch_tool",
    "build_work_item_tools",
    "build_workset_tools",
    "chat_workset_key",
    "cleanup_mcp_manager",
    "consult_refusal",
    "consulted_peer_handover",
    "ctem_read_tools_for",
    "ctem_repair_tools_for",
    "ctem_tools_for",
    "delegated_parent_id",
    "ensure_deck_tools",
    "extract_visited_agents",
    "filter_builtin_tools",
    "filter_integration_tools",
    "format_coordination_tools_prompt",
    "format_ctem_vertical_line",
    "format_datetime_prompt",
    "format_element_bindings_prompt",
    "format_handover_context_prompt",
    "format_knowledge_base_prompt",
    "format_mcp_auth_broken_prompt",
    "format_mcp_unavailable_prompt",
    "format_self_fanout_prompt",
    "format_user_identity",
    "handover_cycle_error",
    "handover_peer_tag",
    "handover_peers_by_type",
    "handover_tool_types",
    "instance_base_tool_name",
    "is_advisory_forbidden_arsenal_action",
    "is_openaev_arsenal_advisory_agent",
    "is_openaev_orchestrator_agent",
    "is_openaev_write_tool",
    "is_orchestrator_forbidden_arsenal_action",
    "is_tradecraft_writer_agent",
    "live_consult_target",
    "load_agent_prompts",
    "load_custom_and_mcp_tools",
    "load_custom_tools",
    "load_kb_names",
    "load_mcp_tools",
    "load_work_item_peers",
    "load_write_locked_kb_ids",
    "logger",
    "make_task_condition",
    "merge_tool_allow_lists",
    "narrow_orchestrator_arsenal_tools",
    "normalize_visited_agents",
    "offered_handover_types",
    "offers_ctem_case_tools",
    "offers_ctem_read_tools",
    "offers_ctem_repair_tools",
    "offers_ctem_signal_tools",
    "offers_ctem_tools",
    "offers_learned_lesson_tool",
    "offers_organization_context_write",
    "offers_organization_proposals",
    "offers_user_admin",
    "orchestrator_may_delete_action",
    "orchestrator_phase_gate_blocked",
    "owned_consult_target",
    "owned_target_resolver",
    "persist_tool_artifact",
    "reachable_handover_peers",
    "record_orchestrator_arsenal_authored",
    "record_orchestrator_session_action",
    "record_orchestrator_step_authored",
    "refuse_core_tool_denials",
    "refused_admin_bound_ids",
    "reset_advisory_arsenal_read_only",
    "reset_advisory_openaev_writes",
    "reset_arsenal_findings_required",
    "reset_orchestrator_arsenal_writes",
    "reset_orchestrator_phase_counter",
    "reset_orchestrator_session_actions",
    "resolve_agent_skills",
    "resolve_handover_agent",
    "resolve_owned_delegation_target",
    "self_fanout_enabled",
    "serialize_messages",
    "set_advisory_arsenal_read_only",
    "set_advisory_openaev_writes",
    "set_arsenal_findings_required",
    "set_orchestrator_arsenal_writes",
    "set_orchestrator_phase_counter",
    "set_orchestrator_session_actions",
    "strip_openaev_write_tools",
]

#: Submodules of the package, in facade lookup order.
_SUBMODULES: tuple[ModuleType, ...] = (
    _shared,
    stix,
    stix_comparator,
    stix_extraction,
    timeline_diff,
    fimi_math,
    fimi_metrics,
    media,
    kb,
    memory,
    web,
    handover,
    agent_builder,
    document_vision,
    evaluations,
    filtering,
    prompting,
    loaders,
    work_items,
    shared_state,
    skills,
    wait,
)


def __getattr__(name: str) -> object:
    """Forward reads of un-rebound names (e.g. mutable module-level caches
    such as ``stix._PYCTI_STIX2_HELPER``) to the owning submodule so the
    facade never serves a stale snapshot."""
    for mod in _SUBMODULES:
        if name in mod.__dict__:
            return mod.__dict__[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


class _CompatModule(ModuleType):
    """Module subclass that mirrors attribute writes onto submodules.

    Before the package split, the test-suite (and potentially external
    callers) monkeypatched attributes on ``app.services.builtin_tools``
    (e.g. ``_persist_generated_file``, ``_render_text_to_pdf``,
    ``_load_caller_user_for_builder``) and the patched value was picked up
    by the tool closures because they read the rebound module global at
    call time.  To preserve those semantics verbatim, writes to this facade
    are mirrored onto every submodule that defines the same name.
    """

    def __setattr__(self, name: str, value: object) -> None:
        super().__setattr__(name, value)
        for mod in _SUBMODULES:
            if name in mod.__dict__:
                ModuleType.__setattr__(mod, name, value)


sys.modules[__name__].__class__ = _CompatModule
