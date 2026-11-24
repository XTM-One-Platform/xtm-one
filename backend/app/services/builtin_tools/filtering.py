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

"""Integration tool filtering and tool allow/deny list helpers.

Moved verbatim from the original ``app/services/builtin_tools.py`` module
as part of the package split (mechanical refactor - no behavior change).
"""

from __future__ import annotations

import contextvars
from collections.abc import Iterable
from copy import deepcopy
from dataclasses import replace
from typing import Any

# ── Integration Tool Filtering ───────────────────────────────────────────────

# Separator between a base tool name and the per-instance slug when several
# integrations of the SAME provider are bound. NOT assumed to be
# unambiguous inside tool names: custom providers accept user-authored names
# verbatim, which may legitimately contain ``__``. The authoritative base is
# the ``base_name`` recorded on the ToolDefinition at rename time. Keep in
# sync with ``integration_tools._INSTANCE_SUFFIX_SEP``.
INSTANCE_SUFFIX_SEP = "__"


def instance_base_tool_name(td_or_name) -> str:
    """Return the base tool name of a (possibly instance-suffixed) tool.

    Multi-instance integration bindings expose ``{base}__{instance_slug}``
    tool names so each bound account gets its own uniquely named tools.
    Allow/deny lists are authored against BASE names (the tool
    scoping UI lists one integration at a time), so gates must resolve a
    suffixed variant back to its base before matching — otherwise a
    configured tool scope silently drops every instance-suffixed tool, and a
    denied base name fails to block its per-instance variants.

    Resolution is metadata-first: prefer the ``base_name`` recorded on the
    ToolDefinition by ``integration_tools._suffix_instance_tools`` at rename
    time. A definition WITHOUT a recorded base was never renamed — its name
    IS the base, and it must be matched exactly (custom providers accept
    user-authored tool names verbatim, so a name like ``sync__all`` may
    legitimately contain ``__``; splitting it would silently WIDEN an allow
    list to a base that was never granted and over-deny legitimate tools).
    The string split survives only for callers that genuinely hold a bare
    name in a fail-closed context (the OpenAEV write gate, where resolving
    too eagerly can only strip more, never less).
    """
    base = getattr(td_or_name, "base_name", None)
    if base:
        return base
    name = getattr(td_or_name, "name", None)
    if isinstance(name, str):
        return name
    return str(td_or_name).split(INSTANCE_SUFFIX_SEP, 1)[0]


# Separator of an allow-list key that scopes ONE integration rather than a
# whole provider: ``"<provider>@<integration id>"``. Used for providers whose
# integrations each expose their own tools under a shared provider key (every
# imported OpenAPI spec is provider ``openapi``), where one list per provider
# would make scoping one API hide or reveal another. ``@`` appears neither in
# provider keys (``custom:<slug>`` uses ``:``) nor in flattened binding keys
# (``provider#N``). Mirrored by ``toolScopeKey`` in the frontend's
# ``lib/tool-scope.ts``.
INSTANCE_SCOPE_SEP = "@"


def instance_scope_key(provider: str, integration_id: str) -> str:
    """The allow-list key that scopes one integration of *provider*."""
    return f"{provider}{INSTANCE_SCOPE_SEP}{integration_id}"


def _instance_scoped_providers() -> frozenset[str]:
    """Providers whose integrations are each their own API (a shared provider key)."""
    from app.services.openapi_import.service import PER_USER_UNRESOLVABLE_PROVIDERS

    return PER_USER_UNRESOLVABLE_PROVIDERS


def _names_any(allow_list: list, names: set[str]) -> bool:
    """Whether *allow_list* names one of *names* (a ``__<slug>`` suffixed entry included)."""
    listed = {str(n) for n in allow_list}
    return any(
        name in listed or any(entry.startswith(f"{name}{INSTANCE_SUFFIX_SEP}") for entry in listed)
        for name in names
    )


def fold_unbound_instance_scopes(allowed: Any, bound_ids: set[str]) -> Any:
    """Fold the per-integration keys of integrations not bound here into their provider's list.

    An integration id is only meaningful on the install it came from: an
    imported agent or assignment (a bundle, a catalog clone) is bound to its
    new owner's integrations, so ``openapi@<source id>`` would never match and
    its restriction would silently disappear. Folded into the provider-wide
    list, it is read per integration at runtime (:func:`filter_integration_tools`):
    an integration whose tools it names is restricted to them. The instance
    lists are united; only when every folded list is empty does the provider
    list restrict everything.
    """
    if not isinstance(allowed, dict):
        return allowed
    kept: dict[str, Any] = {}
    folded: dict[str, list[list]] = {}
    for key, value in allowed.items():
        provider, sep, integration_id = str(key).partition(INSTANCE_SCOPE_SEP)
        if sep and integration_id not in bound_ids:
            if isinstance(value, list):
                folded.setdefault(provider, []).append(value)
            continue
        kept[key] = value
    for provider, lists in folded.items():
        names = sorted({str(n) for values in lists for n in values})
        existing = kept.get(provider)
        if isinstance(existing, list):
            names = sorted({*names, *(str(n) for n in existing)})
        kept[provider] = names
    return kept or None


def filter_integration_tools(tool_defs: list, allowed_map: dict) -> list:
    """Filter integration tool definitions based on per-provider allow lists.

    ``allowed_map`` maps provider keys to lists of tool names.
    Tools whose source is ``integration:{provider}`` are filtered.
    If a provider is absent from the map or its value is ``None``, all its
    tools pass through.

    A key ``"<provider>@<integration id>"`` (:func:`instance_scope_key`)
    scopes only the tools of that integration (matched on the definition's
    ``entity_id``). When both a provider list and an instance list exist, a
    tool must pass both. For a provider whose integrations are each their own
    API (OpenAPI), a provider-wide list is read per integration, as the
    editors write it: it restricts an integration whose tools it names (or
    every one, when empty) and leaves one it does not mention alone, so a
    list naming one API's tools never empties another.

    Allow lists contain BASE tool names; a multi-instance suffixed tool
    passes when either its full name or its RECORDED base name is allowed.
    The base comes from ``ToolDefinition.base_name`` (stamped at
    rename time), never from splitting the name — a user-authored custom
    tool name containing ``__`` must not widen the allow list.
    """
    if not allowed_map:
        return tool_defs
    instance_scoped = _instance_scoped_providers()
    # Every tool name (full and base) of each integration of those providers.
    entity_names: dict[tuple[str, str], set[str]] = {}
    for td in tool_defs:
        source = getattr(td, "source", "") or ""
        provider = source.split(":", 1)[1] if source.startswith("integration:") else None
        entity_id = getattr(td, "entity_id", None)
        if provider in instance_scoped and entity_id:
            entity_names.setdefault((provider, str(entity_id)), set()).update(
                {td.name, instance_base_tool_name(td)}
            )
    filtered = []
    for td in tool_defs:
        provider = (
            td.source.split(":", 1)[1] if td.source.startswith("integration:") else None
        )
        if provider:
            entity_id = getattr(td, "entity_id", None)
            provider_list = allowed_map.get(provider)
            if (
                provider_list
                and provider in instance_scoped
                and entity_id
                and not _names_any(provider_list, entity_names.get((provider, str(entity_id)), set()))
            ):
                provider_list = None
            allow_lists = [provider_list]
            if entity_id:
                allow_lists.append(
                    allowed_map.get(instance_scope_key(provider, str(entity_id)))
                )
            if any(
                allow_list is not None
                and td.name not in allow_list
                and instance_base_tool_name(td) not in allow_list
                for allow_list in allow_lists
            ):
                continue
        filtered.append(td)
    return filtered


#: Prefix of the ``ToolDefinition.source`` every built-in function carries
#: (``builtin_tool_groups.builtin_source``): ``builtin:<group id>``.
BUILTIN_SOURCE_PREFIX = "builtin:"


def filter_builtin_tools(tool_defs: list, allowed_map: dict | None) -> list:
    """Filter built-in functions on per-group allow lists.

    ``allowed_map`` maps a built-in group id (``agent_builder``, ``web``...)
    to the function names it keeps, with the semantics of
    ``mcp_allowed_tools``: a group absent from the map or mapped to ``None``
    keeps every function; a list keeps only the functions it names (an empty
    list keeps none). Tools whose source is not ``builtin:<id>`` pass through
    untouched (integration, MCP, custom, channel tools have their own lists).

    A ``core`` tool (``ToolDefinition.core``, the scheduling tools every agent
    carries) is never removed, as in :func:`apply_tool_deny_list`.

    Applied BEFORE :func:`apply_tool_deny_list` and before the progressive
    tool gate, so a scoped group offers, and loads on demand, only
    the functions it keeps.
    """
    kept, _outside = _split_builtin_scope(tool_defs, allowed_map)
    return kept


def builtin_tools_outside_scope(tool_defs: list[Any], allowed_map: dict[str, Any] | None) -> dict[str, str]:
    """The built-in functions *allowed_map* leaves out of *tool_defs*: name -> group id.

    The functions :func:`filter_builtin_tools` drops: they exist in a group the
    agent carries, outside its ``builtin_allowed_tools`` list for that group (a
    function shipped in the group after the list was written included). The
    callers record them on the executor (``CompositeToolExecutor.withhold``
    with ``registered=True``) so ``load_tools`` and a refused call name them as
    outside the allowlist instead of answering "no tool matches" (#12192).
    """
    return _split_builtin_scope(tool_defs, allowed_map)[1]


def describe_outside_builtin_scope(group: str, *, scope: str = "this agent's") -> str:
    """Why a function of built-in group *group* is not offered: the allowlist leaves it out."""
    return (
        f"it exists in the built-in group {group} but is not in {scope} "
        f"builtin_allowed_tools.{group}; the agent's owner adds it to that list "
        "(update_agent builtin_allowed_tools) to grant it"
    )


def _split_builtin_scope(tool_defs: list[Any], allowed_map: dict[str, Any] | None) -> tuple[list[Any], dict[str, str]]:
    """*tool_defs* split into (kept, outside: name -> group) by the built-in allow lists."""
    if not allowed_map:
        return tool_defs, {}
    # A list keyed by a [DEPRECATED] id applies to its group.
    from app.services.builtin_tool_aliases import canonical_builtin_selection, current_function_name

    allowed_map = canonical_builtin_selection((), allowed_map)[1]
    if not allowed_map:
        return tool_defs, {}
    # A list naming a [DEPRECATED] function name keeps the renamed function.
    allowed_map = {
        group: [current_function_name(str(fn)) for fn in names] for group, names in allowed_map.items()
    }
    kept = []
    outside: dict[str, str] = {}
    for td in tool_defs:
        source = getattr(td, "source", "") or ""
        if not source.startswith(BUILTIN_SOURCE_PREFIX) or getattr(td, "core", False):
            kept.append(td)
            continue
        group = source[len(BUILTIN_SOURCE_PREFIX) :]
        allow_list = allowed_map.get(group)
        if allow_list is not None and td.name not in allow_list:
            outside[td.name] = group
            continue
        kept.append(td)
    return kept, outside


def merge_tool_allow_lists(agent_allowed: dict, assignment_allowed: dict) -> dict:
    """Merge agent-level and assignment-level integration tool allow lists.

    Returns a combined map where the effective tools for each provider are
    the **intersection** of both levels.

    - Neither level restricts → provider absent from result (all tools).
    - Only one level restricts → use that level's list.
    - Both restrict → intersection of the two lists.
    """
    if not agent_allowed and not assignment_allowed:
        return {}

    providers = set(agent_allowed.keys()) | set(assignment_allowed.keys())
    merged: dict = {}

    for provider in providers:
        a_list = agent_allowed.get(provider)
        w_list = assignment_allowed.get(provider)
        a_present = provider in agent_allowed
        w_present = provider in assignment_allowed

        if not a_present and not w_present:
            continue
        elif a_present and not w_present:
            if a_list is not None:
                merged[provider] = a_list
        elif w_present and not a_present:
            if w_list is not None:
                merged[provider] = w_list
        else:
            if a_list is None and w_list is None:
                merged[provider] = None
            elif a_list is None:
                merged[provider] = w_list
            elif w_list is None:
                merged[provider] = a_list
            else:
                merged[provider] = [t for t in a_list if t in w_list]

    return merged


def apply_tool_deny_list(
    tool_defs: list,
    deny_list: list[str] | None,
) -> list:
    """Remove tools whose names appear in the deny list.

    Deny takes **absolute precedence** over allow lists — if a tool is
    denied, it is removed regardless of any allow list that includes it.
    This provides a security backstop for blocking dangerous operations
    (e.g. ``delete_all_issues``) even when the integration is fully enabled.

    A denied BASE name also blocks its multi-instance suffixed variants
    (``tool__account``) — otherwise a second bound account of the
    same provider would escape the deny backstop. The base is the
    ``base_name`` RECORDED on the definition at rename time, never inferred
    by splitting the name, so an MCP or custom tool that legitimately
    contains ``__`` in its name is never over-denied (and denying such a
    tool's real full name keeps working).

    A ``core`` tool (``ToolDefinition.core``, the scheduling tools every agent
    carries) is never removed: the agent APIs refuse to deny one, and a
    list that names one anyway (written before, or by an import) leaves it.

    Args:
        tool_defs: Tool definition objects (must have a ``.name`` attribute).
        deny_list: List of tool name strings to block.  ``None`` or empty
            means no denials.

    Returns:
        Filtered list with denied tools removed.
    """
    if not deny_list:
        return tool_defs
    from app.services.builtin_tool_aliases import current_function_name

    # A [DEPRECATED] function name still denies the renamed function.
    denied = set(deny_list) | {current_function_name(str(name)) for name in deny_list}
    kept = []
    for td in tool_defs:
        if getattr(td, "core", False):
            kept.append(td)
            continue
        if td.name in denied or instance_base_tool_name(td) in denied:
            continue
        kept.append(td)
    return kept


# ── Agent-keyed built-in suites ──────────────────────────────────────────────

#: Built-in suites whose tools act as the AGENT rather than as the person
#: chatting with it: its work-item queue and peer roster, its shared state
#: (fleet-wide when the agent is company-managed), its workset corpus, its
#: traces and the traces its supervision allow-list reaches - and the Deep
#: Investigation, which spends a run on the agent's behalf, files its report
#: into workspace state, and which the General Assistant does not run. Switching one on is
#: part of configuring the agent. The frontend registry marks every one
#: of them ``agent_keyed`` (``BUILTIN_TOOL_GROUPS``, ``builtin_tool_groups.py``), pinned by
#: ``tests/test_general_assistant_tools.py``.
AGENT_KEYED_BUILTIN_TOOL_IDS: frozenset[str] = frozenset(
    {
        "coordination",
        # [DEPRECATED] ids of ``coordination``, still accepted on a session.
        "work_items",
        "shared_state",
        "workset",
        "observability",
        "deep_investigation",
        # The grounded STIX extraction: its own group, for agents built to extract.
        "stix_extractor",
        # Platform Health's one internal group (#1943): its fleet agents alone carry it.
        "health_specialists",
        "health_orchestrator",
        # The write side of the organization profile (#11594): bound to an
        # agent by an administrator, wired for an administrator's turn alone.
        "organization_context",
        # User and group administration: the same, bound by an administrator
        # and wired for an administrator's turn alone.
        "user_admin",
        # Read-only CTEM signals and cases (#1337): wired by the agent paths only.
        "ctem_read",
        # CTEM administrator repairs (#1778): wired by the agent paths only.
        "ctem_repair",
        # CTEM signal and case tools on any agent, the owner's CTEM rights (#1748).
        "ctem",
    }
)

#: The trace group and its [DEPRECATED] ids: read the agent's
#: conversations, so decided on every turn, not by configuring the agent.
_TRACE_GROUP_IDS: frozenset[str] = frozenset({"observability", "inspect_my_traces", "inspect_agent_traces"})


def agent_keyed_session_additions(
    session_tool_ids: Iterable[str], agent_tool_ids: Iterable[str] | None
) -> set[str]:
    """What a chat session's built-in selection adds that would act as the agent.

    A session selection REPLACES the agent's own list, so every id it names
    that the agent does not carry is an addition. Returns the agent-keyed
    additions that configuring the agent decides. The trace suites are not
    among them: they read the agent's conversations, so whoever may read those
    decides them on every turn, the ones ``agent_builder`` implies included
    (``agent_chat._turn_trace_tool_ids``).
    """
    carried = {str(tool_id) for tool_id in agent_tool_ids or ()}
    added = {str(tool_id) for tool_id in session_tool_ids} - carried
    return added & (AGENT_KEYED_BUILTIN_TOOL_IDS - _TRACE_GROUP_IDS)


# --- OpenAEV write-tool stripping (advisory specialists during a run) ---

# Explicit OpenAEV mutation tools that do NOT share the ``manage_openaev_``
# prefix (the run-scoped autonomous authoring/writing tools). The unified
# ``manage_openaev_*`` tools are all mutation-capable (create/update/delete via
# an ``action`` arg), so they are matched by prefix instead of being listed.
_OPENAEV_EXTRA_WRITE_TOOLS = frozenset(
    {
        "author_openaev_attack_path_step",
        "update_openaev_attack_path_step",
        # Deleting a step is a run-scoped MUTATION: an advisory specialist must
        # have it stripped like author/update, or a consulted agent could delete
        # the orchestrator's attack-path steps and break the single-author boundary.
        "delete_openaev_attack_path_step",
        "evaluate_openaev_attack_path",
        "promote_openaev_finding_to_asset",
        "ensure_openaev_target_team",
        "set_openaev_run_scope",
        "record_openaev_event",
        "update_openaev_run_status",
        # Marks pending operator directives consumed (POST .../directives/consume).
        # A consulted specialist must never call it: it would acknowledge (and so
        # hide) operator steering before the orchestrator - the single author -
        # ever sees it.
        "consume_openaev_directives",
    }
)


def _openaev_gate_bases(td_or_name) -> tuple[str, str]:
    """Candidate base names for OpenAEV gate matching (fail-closed).

    Returns both the metadata-recorded base (exact, via
    :func:`instance_base_tool_name`) and the raw ``__``-split prefix. The
    OpenAEV gates match against BOTH: unlike allow/deny lists — where a wrong
    base resolution silently widens a scope — an OpenAEV gate decision can
    only get STRICTER from an extra candidate (a tool gets stripped or
    narrowed), so the split fallback closes the "suffixed name with missing
    metadata" hole without reintroducing the custom-``__`` false-positive
    risk that matters for scoping lists.
    """
    base = instance_base_tool_name(td_or_name)
    name = getattr(td_or_name, "name", None)
    raw = name if isinstance(name, str) else str(td_or_name)
    return base, raw.split(INSTANCE_SUFFIX_SEP, 1)[0]


def is_openaev_write_tool(td_or_name) -> bool:
    """True for any OpenAEV tool that can MUTATE OpenAEV state.

    Covers every unified ``manage_openaev_*`` tool (create/update/delete) plus
    the run-scoped autonomous authoring/writing tools. Read-only OpenAEV tools
    (``search_openaev_*`` / ``read_openaev_*`` / ``get_openaev_*``) are NOT
    matched, so a consulted specialist keeps its ability to look things up.

    Matched on the instance-BASE name (see :func:`_openaev_gate_bases`): with
    two OpenAEV platform integrations bound to one agent, multi-instance
    suffixing renames the tools to ``set_openaev_run_scope__acme``
    etc., and an exact-name match would let a suffixed run-scoped write slip
    past the advisory strip.
    """
    return any(
        b.startswith("manage_openaev_") or b in _OPENAEV_EXTRA_WRITE_TOOLS
        for b in _openaev_gate_bases(td_or_name)
    )


# The ONE OpenAEV write an advisory specialist may keep on a consult. The threat
# arsenal is a GLOBAL, reusable library (``POST /api/threat_arsenals``): creating an
# action there has NO run / simulation / scope / discovery-mode implications - it
# just mints a reusable injector contract. Letting the consulted Payload Creator
# author it DIRECTLY is what makes "consult -> a payload actually gets created" hold,
# instead of the lossy JSON-spec hand-off the orchestrator had to re-serialize into a
# create call (the source of the 400s and the "delegated but nothing landed in the
# arsenal" failure). Every RUN-SCOPED write (author / evaluate / promote / scope /
# record / status / consume) still gets stripped, so the orchestrator remains the
# single author of the attack PATH and no out-of-band run entity can be created.
_OPENAEV_ADVISORY_ALLOWED_WRITE_TOOLS = frozenset({"manage_openaev_threat_arsenal"})

# Arsenal sub-actions an advisory specialist (the Payload Creator) must never
# run. delete / bulk_delete are destructive on the reusable library (a prompt
# injection could otherwise wipe it). fork / duplicate are refused because
# forking a payload-based dead-end is the Attack Path Orchestrator's OWN atomic
# 'fork' operation (D1): the copy's rename + AI tag + REGEX parsers + read-back
# verify happen in one call the orchestrator owns, and de-dup is code-level. The
# specialist creates ORIGINALS only; routing forks through it is exactly the
# "dumb PC double-dup" this removes. create / update (of an original) stay
# allowed. The allowlisted tool still supports every action for the orchestrator
# and for chat - this set only narrows the advisory surface.
_OPENAEV_ADVISORY_FORBIDDEN_ARSENAL_ACTIONS = frozenset(
    {"delete", "bulk_delete", "fork", "duplicate"}
)

# EVERY arsenal mutation. A CONSULT (advise-only, WS10 / D3) refuses all of
# these: "consult is consult, not creation". The DELEGATE path (identity-forced
# restrict) keeps create / update - it is exactly where the Payload Creator
# authors ORIGINALS out-of-band - so this wider set is gated on the read-only
# flag below, not on the plain advisory flag.
_OPENAEV_ARSENAL_WRITE_ACTIONS = frozenset(
    {"create", "update", "delete", "bulk_delete", "fork", "duplicate"}
)

# Set for the duration of a restrict_openaev_writes consult so the arsenal
# handler can reject forbidden sub-actions even if the LLM hallucinates them
# past the narrowed schema. Task-local: nested awaits in the same consult keep
# the flag; a sibling orchestrator turn is unaffected.
_advisory_openaev_writes: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "advisory_openaev_writes", default=False
)

# Set ONLY on the caller-driven consult path (WS10 / D3): the orchestrator
# consults the Payload Creator for a DECISION, never a mutation. Distinct from
# the identity-forced delegate/background path, where the same PC creates
# originals. When set, EVERY arsenal write is refused (create/update too), not
# just the destructive/fork ones - so a consulted PC can only advise. Separate
# ContextVar (not folded into set_advisory_openaev_writes) so the existing
# delegate-mode token/reset contract stays byte-for-byte unchanged.
_advisory_arsenal_read_only: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "advisory_arsenal_read_only", default=False
)


def set_advisory_openaev_writes(enabled: bool) -> contextvars.Token[bool]:
    """Mark this task as an advisory consult (arsenal create/update only)."""
    return _advisory_openaev_writes.set(enabled)


def reset_advisory_openaev_writes(token: contextvars.Token[bool]) -> None:
    _advisory_openaev_writes.reset(token)


def set_advisory_arsenal_read_only(enabled: bool) -> contextvars.Token[bool]:
    """Mark a consult as READ-ONLY on the arsenal (advise-only, WS10 / D3).

    Set alongside ``set_advisory_openaev_writes`` when the restrict is
    caller-driven (a consult). Every arsenal WRITE action - create and update
    included, not only delete/bulk_delete/fork/duplicate - is then refused, so
    "consult is consult, not creation" holds in code, not just persona. The
    delegate/background path (identity-forced restrict) leaves this False and
    keeps create/update for original authoring.
    """
    return _advisory_arsenal_read_only.set(enabled)


def reset_advisory_arsenal_read_only(token: contextvars.Token[bool]) -> None:
    _advisory_arsenal_read_only.reset(token)


def is_advisory_forbidden_arsenal_action(action: str | None) -> bool:
    """True when a consult must refuse this ``manage_openaev_threat_arsenal`` action.

    Two tiers, both gated on an active advisory consult:

    * read-only consult (WS10) -> refuse ANY write (create/update included), so
      an advise-only consult can never mutate the reusable library;
    * plain advisory (delegate/background) -> refuse only the destructive/fork
      actions, keeping create/update for original authoring.
    """
    if not _advisory_openaev_writes.get():
        return False
    normalized = (action or "").strip().lower()
    if _advisory_arsenal_read_only.get():
        return normalized in _OPENAEV_ARSENAL_WRITE_ACTIONS
    return normalized in _OPENAEV_ADVISORY_FORBIDDEN_ARSENAL_ACTIONS


# Built-in specialists that AUTHOR the reusable arsenal but must ALWAYS run with
# the advisory (create/update-only, no run-scoped, no delete/bulk_delete) OpenAEV
# surface - in EVERY execution context, not only a consult. The autonomous
# orchestrator consults the Payload Creator with ``restrict_openaev_writes=True``,
# but the Scenario Creator delegates the SAME agent through ``spawn_background_task``
# (``sub_agent.py``), whose ``run_agent_chat`` call never sets that flag. Keying the
# restriction to the agent identity - not to every caller remembering to pass the
# flag - is what keeps ``delete`` / ``bulk_delete`` off the global arsenal library on
# the background-task path too, restoring the blanket protection the old
# ``tool_deny_list`` entry gave before the arsenal write was allowed. Consumed by
# ``run_agent_chat`` (see :func:`is_openaev_arsenal_advisory_agent`).
_OPENAEV_ARSENAL_ADVISORY_AGENT_SLUGS = frozenset({"openaev-payload-creator"})


def is_openaev_arsenal_advisory_agent(slug: str | None, *, is_builtin: bool) -> bool:
    """True for a built-in specialist that must always run arsenal-advisory.

    Such an agent gets ``restrict_openaev_writes`` forced on regardless of how it
    was invoked (consult OR background task), so its OpenAEV surface is always
    the create/update-only arsenal tool plus reads - never a run-scoped write or
    a destructive arsenal sub-action.
    """
    return bool(is_builtin) and slug in _OPENAEV_ARSENAL_ADVISORY_AGENT_SLUGS


# The Attack Path Orchestrator must NEVER create original arsenal junk
# (password-spray Commands, invented scanners). It MAY adapt a payload-based
# dead-end with the atomic ``fork`` op (duplicate -> rename -> AI tag -> REGEX
# parsers -> read-back verify, in ONE call, with code-level cross-cycle dedup).
# Native injectors (no payload) cannot be forked - reuse their id. Delegate the
# Payload Creator only for original/complex create. Gating is identity-keyed AND
# execution-time via a ContextVar set from the CURRENT agent in
# ``run_agent_chat``. Nested consults/delegates share the asyncio task, so they
# inherit ContextVars: the nested call MUST set this flag from the target slug
# (Payload Creator -> False) or create would be blocked on the specialist that
# is supposed to author original payloads.
_OPENAEV_ORCHESTRATOR_AGENT_SLUGS = frozenset({"openaev-attack-path-orchestrator"})

# The orchestrator is deliberately FREE with the arsenal. Samuel's standing
# instruction (do not re-litigate it in a future sweep): NEVER forbid authoring
# sub-actions to the orchestrator. It is smarter than a static allow-list at
# deciding when to reuse, fork, duplicate+update, or create outright - and an
# over-tight gate is exactly what produced weeks of thrash (an earlier sweep
# forbade ``duplicate``/``create`` "to stop orphans", which just made the model
# fail differently). We are OUTCOME-driven, not tool-restrictive: a run is judged
# by whether the arsenal it leaves behind is CLEAN - as few new payloads as
# possible, each producing REAL findings, each USED in an authored step, and
# nothing chain-useless orphaned - NOT by which sub-action it took to get there.
#
# Those outcomes are guaranteed by mechanisms that never remove a tool from the
# model's hand:
#   * findings param + strict normalizer + read-back verify -> payloads produce
#     real findings (see _common._expand_findings / _normalize_output_parsers);
#   * step-before-more phase gate (below) -> every payload gets wired into a step;
#   * Semaphore(3) write cap (threat_arsenal._arsenal_write_guard) -> Samuel's
#     "max 3 at once", which also kills the parallel-burst orphan race;
#   * session-scoped delete (orchestrator_may_delete_action) -> it cleans up its
#     OWN unused/bad copies instead of us pre-forbidding creation.
#
# So the ONLY sub-action refused here is ``bulk_delete``: its scope is a FILTER,
# not an id list, so it cannot be bounded to "only what THIS run created", and the
# one hard line Samuel drew is that the orchestrator must NEVER delete a payload
# from another session or authored by someone else. Plain per-id ``delete`` is
# ALLOWED, bounded to this run's own creations by orchestrator_may_delete_action.
# create / duplicate / update / fork / reads are all allowed. Do NOT add create,
# duplicate, or delete back to this set in a future sweep - that regression is
# the exact thing this comment exists to prevent.
_ORCHESTRATOR_FORBIDDEN_ARSENAL_ACTIONS = frozenset({"bulk_delete"})

_orchestrator_arsenal_writes: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "orchestrator_arsenal_writes", default=False
)


def is_openaev_orchestrator_agent(slug: str | None, *, is_builtin: bool) -> bool:
    """True for the built-in OpenAEV Attack Path Orchestrator.

    That agent is FREE across the arsenal: list/get/counts, and every authoring
    path - reuse, fork, duplicate, update, create - plus session-scoped cleanup
    (delete an action IT created this run). It is judged on the OUTCOME (few
    payloads, all producing real findings, all used in a step, nothing orphaned),
    not on which sub-action it picks. Only ``bulk_delete`` is refused, because its
    filter scope cannot be bounded to this run's own creations and the
    orchestrator must never remove another session's / someone else's payloads.
    """
    return bool(is_builtin) and slug in _OPENAEV_ORCHESTRATOR_AGENT_SLUGS


def set_orchestrator_arsenal_writes(enabled: bool) -> contextvars.Token[bool]:
    """Mark this task as the orchestrator's own run.

    This marker gates the few orchestrator-specific arsenal behaviours (the
    ``bulk_delete`` refusal, the session-scoped delete check, the step-before-more
    phase gate) - NOT a broad write lock. Always call with the CURRENT agent's
    identity: True only for the orchestrator, False for everyone else (including a
    nested Payload Creator consult, which keeps the full unrestricted contract).
    """
    return _orchestrator_arsenal_writes.set(enabled)


def reset_orchestrator_arsenal_writes(token: contextvars.Token[bool]) -> None:
    _orchestrator_arsenal_writes.reset(token)


def is_orchestrator_forbidden_arsenal_action(action: str | None) -> bool:
    """True when the orchestrator must refuse this arsenal action.

    False unless the orchestrator-run marker is set (so chat / Payload Creator /
    tests are unaffected). The orchestrator is FREE across the arsenal: allowed
    when set are list, list_non_tabletop, get, create, duplicate, update, fork,
    delete (session-scoped - see :func:`orchestrator_may_delete_action`), the
    counts, argument_types, security_platforms, and schemas. Only ``bulk_delete``
    is refused, because its filter scope cannot be bounded to this run's own
    creations.
    """
    if not _orchestrator_arsenal_writes.get():
        return False
    return (action or "").strip().lower() in _ORCHESTRATOR_FORBIDDEN_ARSENAL_ACTIONS


# Per-run ledger of the arsenal action ids THIS orchestrator run created (via
# create / fork / duplicate). It exists for exactly one reason: to let the
# orchestrator clean up its OWN mess - purge a payload it authored this run but
# never wired into a step - WITHOUT ever being able to delete a payload from
# another session, authored by a human, or authored by another agent (Samuel's
# one hard line). A tag/filter scope cannot express "only what this run made"
# safely; an explicit id ledger can. Same mutable-shared-object discipline as the
# phase counter: one set created before the loop, then MUTATED in place by every
# asyncio.gather child (we add to the set, never rebind the ContextVar, so the
# children's copied context still points at the same set object).
_orchestrator_session_actions: contextvars.ContextVar[set[str] | None] = (
    contextvars.ContextVar("orchestrator_session_actions", default=None)
)


def set_orchestrator_session_actions(
    enabled: bool,
) -> contextvars.Token[set[str] | None]:
    """Start (orchestrator) or clear (everyone else) the per-run created-id ledger.

    Call with the CURRENT agent's identity next to
    :func:`set_orchestrator_arsenal_writes`. A fresh empty set for the
    orchestrator; ``None`` for everyone else, so a nested Payload Creator run on
    the same task neither populates nor consults the parent's ledger.
    """
    return _orchestrator_session_actions.set(set() if enabled else None)


def reset_orchestrator_session_actions(
    token: contextvars.Token[set[str] | None],
) -> None:
    _orchestrator_session_actions.reset(token)


def record_orchestrator_session_action(action_id: str | None) -> None:
    """Remember that THIS orchestrator run authored this arsenal action id.

    No-op outside the orchestrator's own run or without an id, so chat, other
    agents, and a delegated Payload Creator never populate the ledger. Called
    once for each id THIS run MINTS - on create, on a fresh fork duplicate, and
    on duplicate - but NEVER on the fork REUSE path, whose id can belong to a
    prior run (cross-cycle dedup); recording that would let this run delete
    another session's payload. See the fork handling in ``threat_arsenal.py``.
    """
    if not _orchestrator_arsenal_writes.get():
        return
    ledger = _orchestrator_session_actions.get()
    if ledger is not None and action_id:
        ledger.add(str(action_id))


def orchestrator_may_delete_action(action_id: str | None) -> bool:
    """True when a ``delete`` is allowed for the caller.

    Unrestricted for any non-orchestrator caller (chat / other agents / the
    Payload Creator keep the full delete contract). During the orchestrator's own
    run, allowed ONLY for an id this run created (present in the session ledger) -
    never a payload from another session or authored elsewhere. An empty ledger
    means the run has created nothing yet, so every delete is refused.
    """
    if not _orchestrator_arsenal_writes.get():
        return True
    ledger = _orchestrator_session_actions.get()
    return bool(action_id) and ledger is not None and str(action_id) in ledger


# Step-before-more phase gate. The conclusive symptom of the broken runs was 79
# iterations where EVERY call was manage_openaev_threat_arsenal and not one was
# author_openaev_attack_path_step - the orchestrator batched arsenal writes and
# never authored a step, so the Logic graph had zero event nodes. Persona prose
# ("max 3", "step by step") is not an authorization boundary; this is the code
# invariant. A per-run mutable counter (a dict, NOT an int - a ContextVar holding
# an int would not share increments with the loop's asyncio.gather children,
# which each get a COPY of the context; a dict set ONCE before the loop is the
# SAME object they all mutate) tracks authored arsenal items vs authored steps.
# When arsenal runs PHASE_GATE_LEAD ahead of steps, further fork/create is
# refused until a step lands - forcing "available-finding action -> fork ->
# step" interleaving. Gated on the orchestrator-run marker so chat, other
# agents, and a delegated Payload Creator (its own run sets the marker False) are
# never throttled.
PHASE_GATE_LEAD = 3

_orchestrator_phase_counter: contextvars.ContextVar[dict[str, int] | None] = (
    contextvars.ContextVar("orchestrator_phase_counter", default=None)
)


def set_orchestrator_phase_counter(
    enabled: bool,
) -> contextvars.Token[dict[str, int] | None]:
    """Start (orchestrator) or clear (everyone else) the per-run phase counter.

    Call with the CURRENT agent's identity right next to
    :func:`set_orchestrator_arsenal_writes`, so the counter's lifetime matches
    the orchestrator-run marker. A fresh mutable dict for the orchestrator (so
    every asyncio.gather child in the loop shares it); ``None`` for everyone
    else, so a nested Payload Creator run on the same task does not inherit the
    parent's live counter.
    """
    return _orchestrator_phase_counter.set(
        {"arsenal": 0, "steps": 0} if enabled else None
    )


def reset_orchestrator_phase_counter(
    token: contextvars.Token[dict[str, int] | None],
) -> None:
    _orchestrator_phase_counter.reset(token)


def record_orchestrator_arsenal_authored() -> None:
    """Count one successfully authored arsenal item (fork/create) for the gate.

    No-op unless it is the orchestrator's own run (marker set) with a live
    counter - a delegated Payload Creator create (marker False) must not consume
    the orchestrator's arsenal budget.
    """
    if not _orchestrator_arsenal_writes.get():
        return
    counter = _orchestrator_phase_counter.get()
    if counter is not None:
        counter["arsenal"] = counter.get("arsenal", 0) + 1


def record_orchestrator_step_authored() -> None:
    """Count one authored attack-path step, relieving the phase gate."""
    if not _orchestrator_arsenal_writes.get():
        return
    counter = _orchestrator_phase_counter.get()
    if counter is not None:
        counter["steps"] = counter.get("steps", 0) + 1


def orchestrator_phase_gate_blocked() -> bool:
    """True when the orchestrator has authored too many arsenal items w/o steps.

    Only meaningful during the orchestrator's own run (marker set, counter
    present). Blocks a NEW fork/create when authored arsenal items run
    ``PHASE_GATE_LEAD`` or more ahead of authored steps, so the model must wire
    one of them (or an already-available finding-producing action) into a step
    before authoring more.
    """
    if not _orchestrator_arsenal_writes.get():
        return False
    counter = _orchestrator_phase_counter.get()
    if counter is None:
        return False
    return (counter.get("arsenal", 0) - counter.get("steps", 0)) >= PHASE_GATE_LEAD


# The orchestrator MINTS people (create) to obtain a player_id for the gated
# ensure_openaev_target_team, and looks them up - but must never UPDATE, DELETE,
# or UPSERT OpenAEV users. manage_openaev_player is a full CRUD tool, so adding
# it to the orchestrator's allow-list (human-targeting) also technically reaches
# mutation of arbitrary users, and a persona instruction is not an authorization
# boundary. upsert is refused alongside update/delete: OpenAEV's
# /api/players/upsert rewrites an existing user matched by email - profile
# fields, tags, and team memberships - so it is a mutation path, not a mint.
# Plain create (/api/players) is idempotent mint-or-resolve: an existing email is
# attached to the tenant and returned UNCHANGED, a new email is created - so
# create alone covers the mint-a-person-to-get-a-player_id need with no way to
# alter an existing user. Refuse the mutating sub-actions in code, keyed to the
# SAME orchestrator-run marker as the arsenal gate above (set from the current
# agent's identity in run_agent_chat), so chat / other agents / the Payload
# Creator are unaffected (default False).
_ORCHESTRATOR_FORBIDDEN_PLAYER_ACTIONS = frozenset({"update", "delete", "upsert"})


def is_orchestrator_forbidden_player_action(action: str | None) -> bool:
    """True when the orchestrator must refuse this manage_openaev_player action.

    False unless the orchestrator-run marker is set (so chat, other agents, the
    Payload Creator, and tests keep the full CRUD contract). Allowed when set:
    list, create (idempotent mint-or-resolve, then wrap via
    ensure_openaev_target_team). Refused: update, delete, upsert - the
    orchestrator must not mutate or remove OpenAEV users, and upsert rewrites an
    existing user matched by email (profile fields, tags, team memberships).
    """
    if not _orchestrator_arsenal_writes.get():
        return False
    return (action or "").strip().lower() in _ORCHESTRATOR_FORBIDDEN_PLAYER_ACTIONS


def is_orchestrator_player_team_binding_forbidden(
    team_ids: list[str] | None,
) -> bool:
    """True when the orchestrator passed team_ids to manage_openaev_player.

    The action gate above lets the orchestrator MINT a bare person (create), but
    create still accepts team_ids, which the tool forwards as user_teams to
    /api/players - so on its own the action gate would let the orchestrator
    hand-stitch membership in arbitrary OpenAEV teams. Team membership must
    instead go through the run-scoped ensure_openaev_target_team gate, which
    OpenAEV checks against the acting agent's discovery mode + scope; that gate
    wraps players by their ids and never routes through this tool's team_ids, so
    refusing team_ids here does not break the legitimate path. False unless the
    orchestrator-run marker is set (chat / other agents keep the full contract)
    and at least one team id was actually supplied.
    """
    if not _orchestrator_arsenal_writes.get():
        return False
    return bool(team_ids)


# WS4-C: a COMMAND/EXECUTABLE arsenal action authored during an autonomous
# engagement (the orchestrator's own fork, or the Payload Creator's delegate
# create) MUST produce findings - a parser-less command runs and emits output
# nobody captures, and OpenAEV chaining is findings-based, so the action is
# chain-useless the moment it is minted. This is enforced in code at create/fork
# time, but ONLY for those autonomous authors: a human (or any other agent)
# building a findings-less PREVENTION/DETECTION-only command test through the
# same tool is a legitimate, non-chaining use and must not be blocked. Like the
# orchestrator gate above, it is identity-keyed AND execution-time via a
# ContextVar set from the CURRENT agent in ``run_agent_chat`` - the Payload
# Creator's delegate task runs its own ``run_agent_chat`` (in the worker) in the
# SAME asyncio task as the tool call, so it sees the flag; a general chat caller
# never sets it and is unaffected.
_arsenal_findings_required: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "arsenal_findings_required", default=False
)


def set_arsenal_findings_required(enabled: bool) -> contextvars.Token[bool]:
    """Mark this task as an autonomous arsenal author (findings gate active).

    Always call with the CURRENT agent's identity: True for the orchestrator or
    the Payload Creator, False for everyone else, so a general chat create keeps
    the unrestricted contract.
    """
    return _arsenal_findings_required.set(enabled)


def reset_arsenal_findings_required(token: contextvars.Token[bool]) -> None:
    _arsenal_findings_required.reset(token)


def arsenal_findings_required() -> bool:
    """True when the current task must enforce the findings-required create gate."""
    return _arsenal_findings_required.get()


# Built-in agents allowed to write AGENT-LEVEL tradecraft memory. A
# tradecraft row is stored with ``user_id = NULL`` and is therefore injected into
# every other user's conversation with the same agent, so the write is a
# cross-user promotion primitive, not an ordinary memory write. Offering it on
# every memory-enabled agent would let any user of a shared/company-managed agent
# ask it to publish arbitrary text to all the others; the target-pattern guard in
# ``persistent_memory`` rejects hosts and ids but is NOT an authorization
# boundary. Gated on agent identity for the same reason as the arsenal-advisory
# list above: a caller-passed flag is one forgotten argument away from the leak.
# The orchestrator qualifies because it is company-managed AND ``disable_chat``,
# so the only thing that can drive it is an authorized autonomous run.
_TRADECRAFT_WRITER_AGENT_SLUGS = frozenset({"openaev-attack-path-orchestrator"})


def is_tradecraft_writer_agent(slug: str | None, *, is_builtin: bool) -> bool:
    """True for a built-in agent allowed to write agent-level tradecraft memory.

    Every other agent keeps the ordinary user-scoped memory surface: a
    ``tradecraft`` write from one is refused rather than silently downgraded, so
    the caller learns the capability is not available instead of believing it
    published a durable cross-user lesson.
    """
    return bool(is_builtin) and slug in _TRADECRAFT_WRITER_AGENT_SLUGS


def _narrow_advisory_arsenal_tool(td):
    """Drop delete / bulk_delete / fork / duplicate from the delegate schema.

    The identity-forced delegate/background path where the Payload Creator
    authors ORIGINALS: create/update stay, the destructive and fork actions are
    dropped (fork is the orchestrator's own atomic op, D1).
    """
    schema = deepcopy(td.input_schema) if td.input_schema else {}
    action_prop = (schema.get("properties") or {}).get("action")
    if not isinstance(action_prop, dict):
        return td
    enum = action_prop.get("enum")
    if not isinstance(enum, list):
        return td
    action_prop["enum"] = [
        item
        for item in enum
        if item not in _OPENAEV_ADVISORY_FORBIDDEN_ARSENAL_ACTIONS
    ]
    return replace(td, input_schema=schema)


# A consulted Payload Creator ADVISES; it never mutates the reusable library
# (WS10 / D3). Replaces the shared create-oriented description so the model does
# not try to create on a consult - enum narrowing alone is not enough, tool-
# calling models still follow the prose.
_ADVISORY_READONLY_ARSENAL_DESCRIPTION = (
    "Read-only on a consult: search and inspect the OpenAEV Threat Arsenal "
    "(list, list_non_tabletop, get, counts, schemas, argument_types, "
    "security_platforms) to inform your ADVICE. You are being CONSULTED for a "
    "decision, not asked to build - creating, updating, forking or duplicating "
    "an arsenal action is NOT available here. Return the reuse / fork / create "
    "decision and a compact spec; the orchestrator forks dead-ends itself and "
    "DELEGATES you (a background task) when an original actually needs creating."
)


def _read_only_arsenal_tool(td):
    """Narrow the arsenal schema to READ actions only (advise-only consult).

    Drops EVERY write action (create/update as well as fork/duplicate/delete/
    bulk_delete) so a consulted specialist can only search and inspect. The
    execution-time backstop is :func:`is_advisory_forbidden_arsenal_action`
    under the read-only flag.
    """
    schema = deepcopy(td.input_schema) if td.input_schema else {}
    action_prop = (schema.get("properties") or {}).get("action")
    if not isinstance(action_prop, dict):
        return replace(td, description=_ADVISORY_READONLY_ARSENAL_DESCRIPTION)
    enum = action_prop.get("enum")
    if not isinstance(enum, list):
        return replace(td, description=_ADVISORY_READONLY_ARSENAL_DESCRIPTION)
    action_prop["enum"] = [
        item for item in enum if item not in _OPENAEV_ARSENAL_WRITE_ACTIONS
    ]
    return replace(
        td,
        input_schema=schema,
        description=_ADVISORY_READONLY_ARSENAL_DESCRIPTION,
    )


# The orchestrator copy keeps every authoring path in the schema (only
# bulk_delete is dropped; see _ORCHESTRATOR_FORBIDDEN_ARSENAL_ACTIONS) but the
# description STEERS a strict precedence: reuse first, fork a payload-based
# dead-end (the default), create an original only to fill a genuine gap. The gate
# stays permissive; the steering is what stops "always creates, never forks".
_ORCHESTRATOR_ARSENAL_DESCRIPTION = (
    "Read AND author the OpenAEV Threat Arsenal. Follow this precedence STRICTLY, "
    "in order - it is what keeps the arsenal clean: (1) REUSE a contract that "
    "already emits the finding type you need and wire its id into a step - always "
    "try installed first. (2) FORK a relevant payload-based contract that produces "
    "NONE - this is the default, not an option. The platform ships payloads that "
    "do the technique but emit nothing (a PowerShell password-history dump, a "
    "Wi-Fi extraction with no parser); action='fork' duplicates the source, strips "
    "'(duplicate)', stamps 'ai generated', applies your findings, reads back to "
    "verify, and REUSES its own copy on retry so it never orphans. Never skip an "
    "interesting payload-based action to avoid a fork, and never write a fresh "
    "original in its place. (3) CREATE an original ONLY to fill a genuine gap "
    "nothing installed or forkable serves - the LAST resort, never the opening "
    "move; for a genuinely complex original delegate the Payload Creator "
    "(spawn_background_task, agent openaev-payload-creator). Authoring an original "
    "when a fork would have served is the exact failure this precedence stops. "
    "Across all of it the outcome that matters: as FEW new payloads as possible, "
    "every one PRODUCES real findings, every one is USED in an authored step, "
    "nothing chain-useless left behind. Add outputs via the dedicated 'findings' "
    'param - a flat list like findings=[{"type": "credentials", "regex": '
    '"([^:]+):(.+)", "fields": {"username": 1, "password": 2}}] - and the tool '
    "builds OpenAEV's nested output-parser DTO for you; do NOT hand-build "
    "action_output_parsers in config. Rename via action='update' (name field). To "
    "purge an unused or bad copy YOU created this run, action='delete' its "
    "action_id - but you can NEVER delete a payload from another session or "
    "authored elsewhere, and bulk_delete is unavailable. NEVER fork/duplicate a "
    "native injector (no payload) - reuse its id. Interleave: author a step that "
    "USES each item soon after you author it - do not batch many payloads before "
    "authoring any steps."
)


def _narrow_orchestrator_arsenal_tool(td):
    """Drop only ``bulk_delete`` from the schema; keep every authoring action.

    Also swap in the precedence-steering orchestrator description so the model
    reuses first, FORKS payload-based dead-ends (the default), and creates an
    original only to fill a genuine gap - the schema stays permissive, the
    description enforces the order. Enum narrowing alone is not enough:
    tool-calling models still follow the description.
    """
    schema = deepcopy(td.input_schema) if td.input_schema else {}
    action_prop = (schema.get("properties") or {}).get("action")
    if isinstance(action_prop, dict):
        enum = action_prop.get("enum")
        if isinstance(enum, list):
            action_prop["enum"] = [
                item
                for item in enum
                if item not in _ORCHESTRATOR_FORBIDDEN_ARSENAL_ACTIONS
            ]
    return replace(
        td,
        input_schema=schema,
        description=_ORCHESTRATOR_ARSENAL_DESCRIPTION,
    )


def narrow_orchestrator_arsenal_tools(tool_defs: list) -> list:
    """Keep every tool; only ``bulk_delete`` is stripped for the orchestrator.

    Only ``manage_openaev_threat_arsenal`` is rewritten (enum + description).
    Reads and all authoring actions (create, duplicate, update, fork, plus
    session-scoped delete) stay; only ``bulk_delete`` is dropped from the schema
    enum, and the shared create-oriented description is replaced with the
    outcome-driven one. Execution-time rejection is
    :func:`is_orchestrator_forbidden_arsenal_action` (bulk_delete) and the
    session-scope check :func:`orchestrator_may_delete_action` (delete).
    """
    narrowed = []
    for td in tool_defs:
        # Instance-base match: a multi-instance OpenAEV binding
        # renames the tool to ``manage_openaev_threat_arsenal__<slug>`` and an
        # exact match would leave bulk_delete in that variant's schema.
        if "manage_openaev_threat_arsenal" in _openaev_gate_bases(td):
            narrowed.append(_narrow_orchestrator_arsenal_tool(td))
        else:
            narrowed.append(td)
    return narrowed


def strip_openaev_write_tools(tool_defs: list, *, allow_arsenal: bool = True) -> list:
    """Remove OpenAEV mutation tools from ``tool_defs``, except the arsenal library.

    Enforces the "specialists are ADVISORY on the run" contract for an agent
    consulted during an autonomous run: the orchestrator stays the single author
    of the attack path and records everything a specialist surfaces through the
    policy-checked, run-scoped tools (with the right ``acting_agent_id``). A
    consulted specialist has no run/simulation context, so a RUN-SCOPED write
    would land OUTSIDE the attack path and OUTSIDE the discovery-mode/scope
    enforcement - this closes that bypass in code rather than trusting the persona
    alone.

    ``manage_openaev_threat_arsenal`` (see
    :data:`_OPENAEV_ADVISORY_ALLOWED_WRITE_TOOLS`) is the reusable-library write
    with no run-scope implications, and it is handled by mode (WS10 / D3):

    * ``allow_arsenal=True`` (the DELEGATE / identity-forced path) - kept, but
      narrowed to create/update of ORIGINALS (delete/bulk_delete/fork/duplicate
      dropped). This is where the Payload Creator authors originals out-of-band.
    * ``allow_arsenal=False`` (a caller-driven CONSULT) - narrowed to READ-ONLY
      so a consulted PC can only advise; "consult is consult, not creation".

    Execution-time backstop is :func:`is_advisory_forbidden_arsenal_action`
    (wider when the read-only flag is set).
    """
    kept = []
    for td in tool_defs:
        if not is_openaev_write_tool(td):
            kept.append(td)
        elif any(
            b in _OPENAEV_ADVISORY_ALLOWED_WRITE_TOOLS
            for b in _openaev_gate_bases(td)
        ):
            # Instance-base match: a multi-instance suffixed arsenal
            # tool must be narrowed like its base, not silently dropped.
            if allow_arsenal:
                kept.append(_narrow_advisory_arsenal_tool(td))
            else:
                kept.append(_read_only_arsenal_tool(td))
    return kept
