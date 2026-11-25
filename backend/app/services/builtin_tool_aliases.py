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

"""[DEPRECATED] built-in tool ids and the group each one now belongs to.

The built-in tools consolidation merges built-in ids into fewer groups. A
former id is never removed: agents, assignments, flows, imports, chat sessions
and the builder tools keep sending it, so every one of them stays accepted and
resolves to its new group, scoped to the functions the former id granted.

``BUILTIN_GROUP_ALIASES`` maps a former id to ``(group id, functions)``.
Everything that reads a built-in id list goes through the helpers below, so a
row written before the merge and a row written after it behave the same:

- :func:`canonical_builtin_selection` turns ids plus ``builtin_allowed_tools``
  into the canonical form (new group ids, the alias scoping folded into the
  group's allow list). The data migration and the readers share it.
- :func:`alias_scoped_functions` answers which functions an id grants when it
  is a former id, for ``builtin_allowed_tools`` validation and filtering.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

#: Former id -> (the group it merged into, the functions it granted).
#: The function tuples are pinned against the builders by
#: ``tests/test_builtin_tool_aliases.py`` (``test_alias_functions_match_the_builders``).
BUILTIN_GROUP_ALIASES: dict[str, tuple[str, tuple[str, ...]]] = {
    # Traces.
    "inspect_my_traces": ("observability", ("inspect_my_traces", "get_trace_payload")),
    "inspect_agent_traces": ("observability", ("inspect_agent_traces", "get_trace_payload")),
    # Stage 2b PR 5: one card per capability.
    "template_search": ("design", ("search_templates", "get_template")),
    "template_authoring": (
        "design",
        (
            "create_template",
            "update_template",
            "delete_template",
            "attach_template_asset",
            "detach_template_asset",
            "validate_presentation",
        ),
    ),
    "document_vision": (
        "design",
        ("render_source_pages", "inspect_pptx_identity", "inspect_html_identity", "compare_palette"),
    ),
    "web_fetch": ("web", ("web_fetch", "fetch_image_asset")),
    "browser": ("web", ("browser",)),
    "stix_bundle_validator": ("cti", ("stix_bundle_validator",)),
    "fimi_analytics": (
        "cti",
        (
            "compare_stix_bundles",
            "analyze_stix_timeline",
            "compute_fimi_metrics",
            "run_statistical_test",
            "compute_fimi_derived_metrics",
        ),
    ),
    "work_items": (
        "coordination",
        (
            "create_work_item",
            "update_work_item",
            "list_work_items",
            "get_work_item",
            "claim_work_item",
            "complete_work_item",
            "delegate_to_assignment",
            # Programs ride with work items (services/work_item_programs.py).
            "list_programs",
            "get_program_status",
            # An agent DMs a waiting item's decision card (#1156).
            "send_decision_card",
            # The person's notification bell, read (#2198).
            "list_my_notifications",
        ),
    ),
    "shared_state": (
        "coordination",
        (
            "get_shared_state",
            "set_shared_state",
            "list_shared_state",
            "resolve_shared_state",
            "delete_shared_state",
            # The friction ledger rides with shared state (services/frictions.py).
            "record_friction",
            "list_frictions",
            "dispose_friction",
            # Per-object timers ride with shared state (services/platform_timers.py).
            "arm_timer",
            "cancel_timer",
            "list_timers",
        ),
    ),
    "workset": (
        "coordination",
        (
            "workset_add_item",
            "workset_next_pending",
            "workset_set_summary",
            "workset_set_priority",
            "workset_discard_item",
            "workset_list_items",
            "workset_search",
            "workset_read_item",
            "workset_digest",
            "workset_progress",
            "workset_declare_total",
            "workset_seal",
            "workset_resolve",
        ),
    ),
    "generate_file": (
        "files",
        (
            "generate_file",
            "read_file_content",
            "query_file_json",
            "edit_file_content",
            "annotate_file",
            "render_html_to_image",
        ),
    ),
    "generate_image": ("files", ("generate_image",)),
    # Stage 2b PR 6: one platform group with three sub-areas. The
    # trace functions ``agent_builder`` grants stay owned by ``observability``
    # (listed first in the catalog); the alias still grants them, wired by
    # ``agent_chat._turn_trace_tool_ids`` from the expanded former id.
    "agent_builder": (
        "platform",
        (
            "list_agent_templates",
            "list_available_resources",
            "create_agent",
            "clone_agent",
            "run_agent_once",
            "list_integration_tools",
            "create_assignment",
            "get_agent",
            "list_agents",
            "list_groups",
            "list_runs",
            "cancel_runs",
            "get_run",
            "update_agent",
            "delete_agent",
            "list_assignments",
            "get_assignment",
            "update_assignment",
            "delete_assignment",
            "trigger_assignment",
            "list_knowledge_bases",
            "create_knowledge_base",
            "add_kb_document",
            "list_custom_tools",
            "get_custom_tool",
            "list_integrations",
            "create_custom_tool",
            "update_custom_tool",
            "delete_custom_tool",
            "list_platform_events",
            "watch_platform_event",
            "list_platform_event_watches",
            "cancel_platform_event_watch",
            "list_integration_catalog",
            "get_integration",
            "create_integration",
            "update_integration",
            "delete_integration",
            "import_agent_memory",
            "list_prompts",
            "get_prompt",
            "create_prompt",
            "update_prompt",
            "delete_prompt",
            "list_skills",
            "search_config_text",
            "get_skill",
            "create_skill",
            "update_skill",
            "delete_skill",
            "list_variables",
            "get_variable",
            "create_variable",
            "update_variable",
            "delete_variable",
            "list_mcp_servers",
            "get_mcp_server",
            "create_mcp_server",
            "update_mcp_server",
            "delete_mcp_server",
            "get_knowledge_base",
            "update_knowledge_base",
            "delete_knowledge_base",
            "list_kb_documents",
            "get_kb_document",
            "delete_kb_document",
            "update_kb_document",
            "list_workspaces",
            "get_workspace",
            "create_workspace",
            "update_workspace",
            "delete_workspace",
            "set_default_workspace",
            "update_workspace_knowledge_bases",
            "list_conversations",
            "move_conversations_to_workspace",
            "list_agent_accounts",
            "get_agent_account",
            "list_agent_account_options",
            "create_agent_account",
            "update_agent_account",
            "edit_agent_account_rule",
            "delete_agent_account",
            "preview_agent_account_decision",
            "list_agent_account_decisions",
            "list_agent_intent_bindings",
            "bind_agent_to_intent",
            "unbind_agent_from_intent",
            "get_knowledge_base_sources",
            "update_knowledge_base_source",
            "resync_knowledge_base",
            "inspect_my_traces",
            "inspect_agent_traces",
            "get_trace_payload",
            "list_evaluations",
            "get_evaluation",
            "list_evaluation_runs",
            "get_evaluation_run_values",
            "trigger_evaluation",
            "get_placebo_comparison",
            "compare_evaluation_runs",
            "get_agent_feedback",
        ),
    ),
    "fleets": (
        "platform",
        (
            "list_fleets",
            "get_fleet",
            "create_fleet",
            "update_fleet",
            "set_fleet_automations",
            "delete_fleet",
            "set_fleet_members",
            "set_fleet_exposure",
            "share_fleet_workspace",
            "set_fleet_member_parent",
            "move_fleet_member",
            "get_fleet_prerequisites",
            "get_fleet_setup",
            "get_fleet_health",
            "dismiss_fleet_health_findings",
            "confirm_fleet_prerequisite",
            "regenerate_fleet_documentation",
            "list_fleet_doc_findings",
            "export_fleet_spec",
            "apply_fleet_spec",
            "generate_fleet_spec",
        ),
    ),
    "flow_builder": (
        "platform",
        (
            "get_node_catalog",
            "list_flows",
            "get_flow",
            "get_flow_examples",
            "validate_flow_graph",
            "create_flow",
            "update_flow_graph",
            "create_flow_event_type",
            "list_flow_event_types",
            "run_flow",
            "get_flow_run",
            "activate_flow",
            "deactivate_flow",
        ),
    ),
    # ROI and outcomes as agent tools (#10006 S2): read the report, declare what a subject counts.
    "roi": (
        "platform",
        (
            "list_roi_units",
            "get_roi_declarations",
            "set_roi_declaration",
            "delete_roi_declaration",
            "get_roi_report",
            "list_roi_records",
            "get_roi_chat_series",
            "adjust_roi_chat_line",
            # The unit catalog (#11881): the handlers refuse a caller who is not an administrator.
            "create_roi_unit",
            "update_roi_unit",
            "archive_roi_unit",
        ),
    ),
}

#: Functions of the platform group a caller who is not an administrator may
#: never run: each handler refuses such a caller outright (``cancel_runs``,
#: the evaluation reads and trigger). Sam's decision 2: the group
#: stays on by default, and these are not wired into a non-admin's turn
#: instead of being refused after the call. Everything else in the group is
#: scoped per entity by the handler (a company-managed write needs an admin,
#: the rest acts within what the caller may manage).
PLATFORM_ADMIN_FUNCTIONS: frozenset[str] = frozenset(
    {
        "cancel_runs",
        "list_evaluations",
        "get_evaluation",
        "list_evaluation_runs",
        "get_evaluation_run_values",
        "trigger_evaluation",
        "get_placebo_comparison",
        "compare_evaluation_runs",
    }
)


def caller_may_run(name: str, *, is_admin: bool) -> bool:
    """Whether a platform function is offered to the caller (decision 2)."""
    return is_admin or name not in PLATFORM_ADMIN_FUNCTIONS


#: Former ids behind a feature flag. A group that reaches such an id
#: only through its own unscoped selection (the card switched on) wires it
#: only while the flag is on, as the separate card was only offered then; a
#: row that named the id, or a function of it, keeps it whatever the flag, so
#: an existing agent never loses a tool it carried.
ALIAS_FEATURE_FLAGS: dict[str, str] = {"flow_builder": "FLOW_BUILDER"}

#: The label a former id carries wherever it is reported (``get_agent``, the API).
DEPRECATED_ALIAS_LABEL = "[DEPRECATED]"

#: Former function name -> its current name (decision 3 of the
#: consolidation): the Computer use file functions carry the ``runner_``
#: prefix so they are not mistaken for the conversation file and sandbox
#: tools. A call under the former name still runs (each definition declares
#: its former name in ``ToolDefinition.aliases``), and a ``builtin_allowed_tools`` list
#: or a deny list naming it keeps its meaning (:func:`current_function_name`).
RENAMED_BUILTIN_FUNCTIONS: dict[str, str] = {
    "read_file": "runner_read_file",
    "write_file": "runner_write_file",
    "list_dir": "runner_list_dir",
    # Platform Health (dev-docs/platform-health.md section 2.3): the Concierge
    # orchestrator reads are named after the suite.
    "list_concierge_signals": "list_health_signals",
    "list_concierge_cycles": "list_health_cycles",
    "get_concierge_cycle": "get_health_cycle",
    "request_concierge_cycle": "request_health_cycle",
    # ... and so are its evaluation tools.
    "run_concierge_evaluation": "run_health_evaluation",
    "get_concierge_evaluation": "get_health_evaluation",
    "compare_concierge_runs": "compare_health_runs",
}


def current_function_name(name: str) -> str:
    """*name* under its current name: a [DEPRECATED] function name is renamed."""
    return RENAMED_BUILTIN_FUNCTIONS.get(name, name)


#: Former group id -> its current id, a pure rename: the group grants the same
#: functions under either id (unlike ``BUILTIN_GROUP_ALIASES``, which scopes a
#: merged group to what each former id granted). Platform Health
#: (dev-docs/platform-health.md section 2.3): the Concierge groups are named
#: after the suite. A row, an API call or a ``builtin_allowed_tools`` key
#: naming a former id keeps working and is stored under the current id
#: (:func:`canonical_builtin_selection`, migration ``h119healthgrp``). The
#: specialists' former ids were deleted with their groups, no alias kept
#: (#1943, migration ``h1943healthgrp01``).
RENAMED_BUILTIN_GROUPS: dict[str, str] = {
    "concierge_orchestrator": "health_orchestrator",
}


def current_group_id(tool_id: str) -> str:
    """*tool_id* under its current id: a [DEPRECATED] renamed group id is renamed."""
    return RENAMED_BUILTIN_GROUPS.get(tool_id, tool_id)


def is_former_builtin_id(tool_id: str) -> bool:
    """Whether *tool_id* is a [DEPRECATED] id: merged into a group or renamed."""
    return tool_id in BUILTIN_GROUP_ALIASES or tool_id in RENAMED_BUILTIN_GROUPS


def _renamed_map(allowed_map: Mapping[str, list[str] | None] | None) -> dict[str, list[str] | None]:
    """*allowed_map* with every renamed group key under its current id.

    Two keys that land on one group (the former and the current id) both
    apply: the group keeps the functions both keep.
    """
    out: dict[str, list[str] | None] = {}
    for raw_key, functions in (allowed_map or {}).items():
        key = current_group_id(str(raw_key))
        if key in out and out[key] is not None and functions is not None:
            out[key] = [fn for fn in out[key] or [] if fn in functions]
        elif key not in out or out[key] is None:
            out[key] = None if functions is None else list(functions)
    return out


def alias_group(tool_id: str) -> str | None:
    """The group a former id merged into, ``None`` for a current id."""
    entry = BUILTIN_GROUP_ALIASES.get(tool_id)
    return entry[0] if entry else None


def alias_scoped_functions(tool_id: str) -> tuple[str, ...]:
    """The functions a former id granted (empty for a current id)."""
    entry = BUILTIN_GROUP_ALIASES.get(tool_id)
    return entry[1] if entry else ()


def group_aliases(group_id: str) -> tuple[str, ...]:
    """The former ids that resolve to *group_id*, in declaration order."""
    return tuple(alias for alias, (group, _fns) in BUILTIN_GROUP_ALIASES.items() if group == group_id)


def group_alias_functions(group_id: str) -> tuple[str, ...]:
    """Every function the former ids of *group_id* granted, in declaration order."""
    out: list[str] = []
    for alias in group_aliases(group_id):
        out.extend(fn for fn in alias_scoped_functions(alias) if fn not in out)
    return tuple(out)


def _key_fold(alias: str, functions: Iterable[str]) -> list[str]:
    """The group allow list an allow list keyed by former id *alias* stands for.

    Before the merge such a list scoped that id alone: the functions only the
    group's other former ids grant were not restricted by it. So the list
    keeps what it names among the functions *alias* granted, plus every
    function *alias* did not grant. Without the id list a function two former
    ids share is kept only when named (the stricter reading);
    :func:`canonical_builtin_selection`, which knows the ids, is exact.
    """
    group = alias_group(alias) or alias
    granted = alias_scoped_functions(alias)
    named = {str(fn) for fn in functions}
    return [fn for fn in group_alias_functions(group) if fn not in granted or fn in named]


def fold_allowed_map(allowed_map: Mapping[str, list[str] | None] | None) -> dict[str, list[str]]:
    """*allowed_map* with every key a group id: former-id keys folded into their group.

    A key mapped to ``None`` restricts nothing and is dropped. Several lists
    that land on one group (former ids, the group itself) all apply: the group
    keeps the functions every one of them keeps.
    """
    out: dict[str, list[str]] = {}
    for raw_key, functions in _renamed_map(allowed_map).items():
        key = str(raw_key)
        if functions is None:
            continue
        group = alias_group(key)
        kept = _key_fold(key, functions) if group is not None else [str(fn) for fn in functions]
        target = group or key
        if target in out:
            out[target] = [fn for fn in out[target] if fn in kept]
        else:
            out[target] = list(dict.fromkeys(kept))
    return out


def describe_builtin_tool_id(tool_id: str) -> str:
    """*tool_id* as a reader should see it: a former id names its group."""
    group = alias_group(tool_id) or (current_group_id(tool_id) if tool_id in RENAMED_BUILTIN_GROUPS else None)
    if group is None:
        return tool_id
    return f"{tool_id} {DEPRECATED_ALIAS_LABEL} alias of {group}"


def canonical_builtin_selection(
    tool_ids: Iterable[str] | None,
    allowed_map: Mapping[str, list[str] | None] | None,
) -> tuple[list[str], dict[str, list[str]] | None]:
    """*tool_ids* and *allowed_map* with every former id folded into its group.

    - A former id becomes its group id (once, at the position of the first one).
    - A group that comes only through former ids (the group itself is not
      listed) keeps exactly the functions it gave before the merge: a function
      is kept when one of those former ids granted it and that id's own allow
      list (if any) keeps it, then an allow list keyed by the group applies.
      No allow list when that is every function of the group.
    - Any other allow list keyed by a former id is folded into its group's
      key (:func:`fold_allowed_map`).

    - A renamed group id (``RENAMED_BUILTIN_GROUPS``) becomes its current id,
      ids and allow-list keys alike, with nothing scoped.

    Pure and idempotent: a canonical selection comes back unchanged.
    """
    ids_out: list[str] = []
    seen: set[str] = set()
    direct: set[str] = set()
    via_alias: dict[str, list[str]] = {}
    allowed_map = _renamed_map(allowed_map) if allowed_map else allowed_map
    for raw in tool_ids or ():
        tool_id = current_group_id(str(raw))
        group = alias_group(tool_id)
        target = group or tool_id
        if group is None:
            direct.add(tool_id)
        else:
            via_alias.setdefault(group, []).append(tool_id)
        if target not in seen:
            seen.add(target)
            ids_out.append(target)

    source_map: Mapping[str, list[str] | None] = allowed_map or {}
    exact: dict[str, list[str] | None] = {}
    for group, aliases in via_alias.items():
        if group in direct:
            continue
        kept: list[str] = []
        # The former ids in their declaration order, whatever their order in
        # the row, so a canonical row is stable (three aliases, one group).
        declared = list(BUILTIN_GROUP_ALIASES)
        for alias in sorted(aliases, key=declared.index):
            own_list = source_map.get(alias)
            for fn in alias_scoped_functions(alias):
                if fn not in kept and (own_list is None or fn in own_list):
                    kept.append(fn)
        group_list = source_map.get(group)
        if group_list is not None:
            kept = [fn for fn in kept if fn in group_list]
        # A row that reached a flag-gated former id keeps an explicit list, so
        # the flag never takes from it what it named.
        flagged = any(alias in ALIAS_FEATURE_FLAGS for alias in aliases)
        exact[group] = None if set(kept) >= set(group_alias_functions(group)) and not flagged else kept

    allowed_out = fold_allowed_map(
        {key: fns for key, fns in source_map.items() if (alias_group(str(key)) or str(key)) not in exact}
    )
    for group, kept_fns in exact.items():
        if kept_fns is not None:
            allowed_out[group] = kept_fns

    return ids_out, (allowed_out or None)


def fold_builtin_selection(target: Any) -> bool:
    """Fold *target*'s ``builtin_tool_ids`` / ``builtin_allowed_tools`` to canonical form.

    *target* is any object carrying both attributes (an ``Agent`` row, an API
    response). A former id is written back as its group, scoped to what it
    granted, so the builder shows one group card for it and a save never
    stores the former id next to its group. Returns
    whether anything changed; a canonical selection is left untouched.

    A group the fold scopes while no list of the row named it (nor one of its
    former ids) is a list the platform wrote: when *target* carries a
    ``config`` it is marked so (``IMPLICIT_SCOPE_CONFIG_KEY``, #39) and the
    functions later releases add to the group reach the agent.
    """
    ids = list(getattr(target, "builtin_tool_ids", None) or [])
    allowed = getattr(target, "builtin_allowed_tools", None)
    if not any(is_former_builtin_id(str(tool_id)) for tool_id in ids) and not any(
        is_former_builtin_id(str(key)) for key in (allowed or {})
    ):
        return False
    folded_ids, folded_allowed = canonical_builtin_selection(ids, allowed)
    target.builtin_tool_ids = folded_ids
    target.builtin_allowed_tools = folded_allowed
    implicit = platform_scoped_groups(allowed, folded_allowed)
    if implicit and hasattr(target, "config"):
        from app.services.builtin_allowed_tools import with_implicit_scope

        target.config = with_implicit_scope(getattr(target, "config", None), add=implicit)
    return True


def platform_scoped_groups(
    allowed_before: Mapping[str, list[str] | None] | None,
    allowed_after: Mapping[str, list[str] | None] | None,
) -> list[str]:
    """The groups *allowed_after* scopes that no key of *allowed_before* scoped. Pure.

    A key of *allowed_before* scopes its group when it is the group or one of
    its former ids and maps to a list. Such a group got its list from the
    fold of former ids alone: the platform wrote it, no person did (#39).
    """
    named = {
        alias_group(str(key)) or current_group_id(str(key))
        for key, fns in (allowed_before or {}).items()
        if fns is not None
    }
    return [str(g) for g, fns in (allowed_after or {}).items() if fns is not None and str(g) not in named]


def runtime_builtin_tool_ids(
    tool_ids: Iterable[str] | None,
    allowed_map: Mapping[str, list[str] | None] | None,
) -> list[str]:
    """The ids a turn wires from, former ids and merged groups alike.

    The runtime still wires a merged group function by function under the ids
    it had before the merge (the trace wiring reads ``inspect_my_traces`` /
    ``inspect_agent_traces``). So the selection is first made canonical,
    then each merged group present is kept AND expanded into the former ids
    whose own functions (those no other alias of the group shares) its allow
    list keeps: an unscoped group expands into every former id but those
    whose feature flag is off (``ALIAS_FEATURE_FLAGS``).
    """
    ids, allowed = canonical_builtin_selection(tool_ids, allowed_map)
    flags_off = _flags_off()
    out: list[str] = []
    for tool_id in ids:
        if tool_id not in out:
            out.append(tool_id)
        aliases = group_aliases(tool_id)
        if not aliases:
            continue
        keep = (allowed or {}).get(tool_id)
        for alias in aliases:
            shared = {
                fn
                for other in aliases
                if other != alias
                for fn in alias_scoped_functions(other)
            }
            own = [fn for fn in alias_scoped_functions(alias) if fn not in shared]
            if keep is None and alias in flags_off:
                continue
            if (keep is None or any(fn in keep for fn in own)) and alias not in out:
                out.append(alias)
    return out


def _flags_off() -> frozenset[str]:
    """The former ids of ``ALIAS_FEATURE_FLAGS`` whose flag is off now."""
    if not ALIAS_FEATURE_FLAGS:
        return frozenset()
    from app.core.feature_flags import is_feature_enabled

    return frozenset(alias for alias, flag in ALIAS_FEATURE_FLAGS.items() if not is_feature_enabled(flag))
