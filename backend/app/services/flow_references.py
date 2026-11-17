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

"""Cross-flow references: cycles and dangling calls.

A `flow.call` node makes one flow hand off to another, which is how the fleet
already composes — an agent handing over to another agent is implicitly calling
that agent's whole downstream behaviour. Modelling it explicitly is what stops
an import from flattening everything into one blob.

It also introduces the one failure mode a single-graph validator cannot see:
**A calls B calls A**. Nothing detects that inside either document, because
each is perfectly well-formed on its own. So the check lives here, where the
other flows can be loaded, and runs on write.

Kept out of ``flow_graph.py`` deliberately: that module is pure and total —
same input, same answer, no I/O — which is what makes it cheap to call on every
keystroke from ``POST /flows/validate``. Reaching into the database from there
would quietly turn the canvas's live feedback into a query per edit.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.helpers import user_visible_filter
from app.models.flow import Flow
from app.models.user import User

CALL_KIND = "flow.call"


def called_flow_ids(graph: Any) -> list[str]:
    """The flow ids a graph hands off to, in document order."""
    if not isinstance(graph, dict):
        return []
    out: list[str] = []
    for node in graph.get("nodes") or []:
        if not isinstance(node, dict) or node.get("kind") != CALL_KIND:
            continue
        flow_id = (node.get("config") or {}).get("flow_id")
        if isinstance(flow_id, str) and flow_id:
            out.append(flow_id)
    return out


def _as_uuid(value: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(value)
    except (ValueError, AttributeError, TypeError):
        return None


async def check_flow_references(
    db: AsyncSession,
    user: User,
    *,
    flow_id: uuid.UUID,
    graph: Any,
    user_group_ids: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Validate what a graph's `flow.call` nodes point at.

    Returns issues in the same node-addressed shape the canvas already renders,
    so a cross-flow problem is pinned on the offending node like any other.

    * calling a flow that does not exist, or that this user cannot see →
      **warning**. It does not corrupt anything, and refusing the save would
      strand a flow whose callee was deleted out from under it.
    * a call that closes a cycle → **warning**. This was an error in the first
      cut, on the reasoning that it "would recurse forever the moment flows
      run". The fleet's own data says otherwise: ten of the assistants
      (CTEM ⇄ OpenCTI ⇄ OpenAEV ⇄ XTM-One …) hand work to each other in both
      directions, and that is normal delegation, not a defect. Refusing it
      would leave the builder unable to describe the platform it models, and
      would force every import back into one flat blob.

      The platform already survives this at the agent level by bounding it —
      "delegated agents never receive handover tools themselves". The runner
      will need the same kind of depth limit; until it exists, the loop is
      surfaced loudly on the node rather than pretended away.
    """
    issues: list[dict[str, Any]] = []
    targets = called_flow_ids(graph)
    if not targets:
        return issues

    # Resolve every reachable flow once, following calls outward from this one.
    edges: dict[uuid.UUID, list[uuid.UUID]] = {}
    seen: set[uuid.UUID] = set()
    frontier = {t for t in (_as_uuid(x) for x in targets) if t is not None}
    edges[flow_id] = sorted(frontier)

    while frontier:
        rows = (
            await db.execute(
                select(Flow).where(
                    Flow.id.in_(frontier),
                    user_visible_filter(
                        Flow, user.id, user_group_ids, is_admin=user.is_admin
                    ),
                )
            )
        ).scalars().all()
        seen |= frontier
        nxt: set[uuid.UUID] = set()
        for row in rows:
            children = {
                c for c in (_as_uuid(x) for x in called_flow_ids(row.graph)) if c
            }
            edges[row.id] = sorted(children)
            nxt |= children - seen
        frontier = nxt

    # Per node, so the message lands on the one the user must fix.
    for node in graph.get("nodes") or []:
        if not isinstance(node, dict) or node.get("kind") != CALL_KIND:
            continue
        raw = (node.get("config") or {}).get("flow_id")
        target = _as_uuid(raw) if isinstance(raw, str) else None
        if target is None:
            continue
        # `edges` only gained an entry for rows the visibility filter returned,
        # so "absent" covers both deleted and not-visible-to-this-user.
        if target not in edges:
            issues.append(
                {
                    "code": "flow.call_missing",
                    "message": (
                        "This step calls a flow that no longer exists, or that "
                        "you cannot access."
                    ),
                    "severity": "warning",
                    "node_id": node.get("id"),
                    "edge_id": None,
                    "field": "flow_id",
                }
            )
            continue
        if _reaches(edges, target, flow_id):
            issues.append(
                {
                    "code": "flow.call_cycle",
                    "message": (
                        "This call loops back to this flow. That mirrors how "
                        "your agents hand work to each other, so it is allowed "
                        "— but it will need a depth limit once flows run."
                    ),
                    "severity": "warning",
                    "node_id": node.get("id"),
                    "edge_id": None,
                    "field": "flow_id",
                }
            )
    return issues


CALL_TOOL_KIND = "action.call_tool"


def _call_tool_names(config: Any) -> tuple[str, list[str]]:
    """``(tool_name, candidate_tools)`` as a Call-tool node stores them."""
    if not isinstance(config, dict):
        return "", []
    tool_name = config.get("tool_name")
    declared = config.get("candidate_tools")
    if isinstance(declared, str):  # the runner tolerates a single value
        declared = [declared]
    candidates = [n for n in declared if isinstance(n, str) and n] if isinstance(declared, list) else []
    return (tool_name.strip() if isinstance(tool_name, str) else ""), candidates


def _llm_wired(graph: dict, node_id: Any) -> bool:
    """The runner's rule (``FlowWalker._wired_llm``): an LLM node wired into
    this node's ``llm`` socket puts it in LLM mode."""
    kinds = {
        n.get("id"): n.get("kind") for n in graph.get("nodes") or [] if isinstance(n, dict)
    }
    return any(
        isinstance(e, dict)
        and e.get("target") == node_id
        and (e.get("target_handle") or "") == "llm"
        and kinds.get(e.get("source")) == "capability.llm"
        for e in graph.get("edges") or []
    )


def _graph_tool_names(graph: Any) -> set[str]:
    """Every tool name a graph's Call-tool nodes store, in either mode."""
    names: set[str] = set()
    if not isinstance(graph, dict):
        return names
    for node in graph.get("nodes") or []:
        if isinstance(node, dict) and node.get("kind") == CALL_TOOL_KIND:
            tool_name, candidates = _call_tool_names(node.get("config"))
            names.update(candidates)
            if tool_name:
                names.add(tool_name)
    return names


async def check_tool_references(
    db: AsyncSession,
    user: User,
    *,
    graph: Any,
    user_group_ids: list[str] | None = None,
    previous_graph: Any = None,
    flow_owner_id: Any = None,
    is_company_managed: bool = False,
    admin_bypass: bool | None = None,
    check_co_editors: bool = True,
) -> list[dict[str, Any]]:
    """Validate the tools a graph's Call-tool nodes store.

    A node is read the way a run reads it (``flow_runner``): the tool picked
    for a name (``tool_id``, ``candidate_tool_ids``) while it is an enabled
    tool of that name in the flow's reach, else the tool the name designates
    among the custom tools of the flow's reach (its owner's, every tool for
    a company-managed flow), and in LLM mode only those the person running
    it can use too.

    * **warning** - a name that designates no tool, or several, in the mode
      the node runs in, and a picked tool that is no longer available: the
      flow still saves, and the step does the same when it runs. A candidate
      list is judged for its writer, the runner the warning speaks to.
    * **error** - a name a writer ADDS, in either mode, that designates no
      tool they can use themselves: the flow's reach may hold a tool its
      writer cannot see, and a run would hand them its output, which is what
      ``check_integration_references`` refuses for an integration. With a
      picked tool they can use, the name only decides once that tool is
      gone, so only a name that would then designate a tool they cannot use
      is refused (an ambiguous one fails the step instead). Checked for a
      co-editor, who writes a flow in its owner's reach, and for an admin
      without *admin_bypass* (``agent_tool_admin_bypass()``: an admin's
      agent under ``AGENT_TOOLS_STRICT_ADMIN_ACCESS``, held to the three
      tiers). An owner writes in their own reach; an admin is not checked.
      A missing and a hidden tool are refused alike. A run (no
      *previous_graph*, ``check_co_editors=False``) checks every name, for
      that admin only, like ``check_three_tier_references``. The picked ids
      themselves are held to what their writer can see by
      ``check_source_references``.

    Needs the database, which is why it lives here and not in the pure
    validator the canvas calls on every keystroke.
    """
    from app.services.flow_runner import (
        CustomToolScope,
        ambiguous_tool_message,
        candidate_tool_pins,
        custom_tool_scope_for_flow,
        flow_builtin_tools,
        pinned_custom_tools,
        resolve_custom_tools_by_name,
        tool_pin,
        unavailable_tool_message,
        unpinned_tool_message,
        usable_custom_tool_ids,
        usable_custom_tools,
    )
    from app.services.flow_tool_selector import MAX_CANDIDATE_TOOLS

    issues: list[dict[str, Any]] = []
    if not isinstance(graph, dict):
        return issues
    nodes = [
        node
        for node in (graph.get("nodes") or [])
        if isinstance(node, dict) and node.get("kind") == CALL_TOOL_KIND
    ]
    stored = [(node, *_call_tool_names(node.get("config"))) for node in nodes]
    if not any(tool_name or candidates for _, tool_name, candidates in stored):
        return issues

    builtins = {d.name for d, _ in flow_builtin_tools()}
    names = _graph_tool_names(graph) - builtins
    owner_id = _as_uuid(str(flow_owner_id)) if flow_owner_id is not None else None
    scope = await custom_tool_scope_for_flow(
        db, owner_id=owner_id, is_company_managed=is_company_managed, fallback=user
    )
    designated, ambiguous = await resolve_custom_tools_by_name(db, names, scope)
    # A built-in wins its name wherever a node names it, so a pin on that
    # name never runs (``_call_tool_by_name``, ``resolve_callable_definitions``).
    node_pins = [
        (
            {n: p for n, p in tool_pin(node.get("config")).items() if n not in builtins},
            {n: p for n, p in candidate_tool_pins(node.get("config")).items() if n in candidates and n not in builtins},
        )
        for node, _tool_name, candidates in stored
    ]
    pinnable = await usable_custom_tools(
        db, scope, [pin for pins in node_pins for field_pins in pins for pin in field_pins.values()]
    )
    bypass = user.is_admin if admin_bypass is None else admin_bypass
    co_editor = (
        check_co_editors
        and owner_id is not None
        and owner_id != user.id
        and not is_company_managed
    )
    checked = not bypass and (co_editor or user.is_admin)
    if user_group_ids is None:
        from app.api.v1.helpers import get_user_group_ids

        user_group_ids = await get_user_group_ids(db, user.id)
    writer = CustomToolScope(
        user.id, tuple(user_group_ids), user.is_admin and not checked
    )
    usable = await usable_custom_tool_ids(db, writer, [t.id for t in (*designated.values(), *pinnable.values())])
    writer_uses = {name for name, tool in designated.items() if tool.id in usable} | builtins
    added = (_graph_tool_names(graph) - _graph_tool_names(previous_graph)) if checked else set()

    def writer_can(name: str, pinned: dict[str, Any]) -> bool:
        """The writer can use what *name* runs as on this node."""
        tool = pinned.get(name)
        return tool.id in usable if tool is not None else name in writer_uses

    def refusal(name: str, pinned: dict[str, Any]) -> str | None:
        if name not in added or name in builtins:
            return None
        tool = pinned.get(name)
        if tool is not None and tool.id in usable:
            fallback = designated.get(name)
            if fallback is None or fallback.id in usable:
                return None
            return (
                f"You cannot use `{name}` here: should the tool you picked become "
                "unavailable, this flow would run another tool of that name that "
                "you cannot use. Give your tool a distinct name, or ask its owner "
                "to share theirs."
            )
        if name in writer_uses:
            return None
        return (
            f"You cannot use `{name}` here: no tool of that name that you can use "
            "is available to this flow. Pick one you can see, or ask its owner to "
            "share it."
        )

    def name_issue(name: str, pins: dict[str, str], *, missing: bool) -> tuple[str, str] | None:
        """What a name that runs as no picked tool says: several tools carry
        it, none does (*missing*), or the tool picked for it is gone."""
        if name in ambiguous:
            return "call_tool.ambiguous", ambiguous_tool_message(name)
        if name not in designated:
            return ("call_tool.tool_missing", unavailable_tool_message(name)) if missing else None
        if name in pins:
            return "call_tool.pin_unavailable", unpinned_tool_message(name)
        return None

    def issue(code: str, message: str, severity: str, node: dict, field: str) -> dict:
        return {
            "code": code,
            "message": message,
            "severity": severity,
            "node_id": node.get("id"),
            "edge_id": None,
            "field": field,
        }

    for (node, tool_name, candidates), (tool_pins, candidate_pins) in zip(stored, node_pins, strict=True):
        tool_pinned = pinned_custom_tools(tool_pins, pinnable)
        candidates_pinned = pinned_custom_tools(candidate_pins, pinnable)
        refused: set[tuple[str, str]] = set()
        for field, node_names, pinned in (
            ("tool_name", [tool_name] if tool_name else [], tool_pinned),
            ("candidate_tools", candidates, candidates_pinned),
        ):
            for name in node_names:
                message = refusal(name, pinned)
                if message is not None:
                    refused.add((field, name))
                    issues.append(issue("call_tool.not_usable", message, "error", node, field))
        # A tool name only runs while no LLM is wired in; the candidates are
        # judged whenever declared, as before.
        if (
            tool_name
            and ("tool_name", tool_name) not in refused
            and tool_name not in builtins
            and tool_name not in tool_pinned
            and not _llm_wired(graph, node.get("id"))
        ):
            found = name_issue(tool_name, tool_pins, missing=True)
            if found is not None:
                issues.append(issue(*found, "warning", node, "tool_name"))
        if not candidates:
            continue
        for name in candidates:
            if ("candidate_tools", name) in refused or name in candidates_pinned or name in builtins:
                continue
            # A missing candidate has its own warning below.
            found = name_issue(name, candidate_pins, missing=False)
            if found is not None:
                issues.append(issue(*found, "warning", node, "candidate_tools"))
        names = candidates
        if len(names) > MAX_CANDIDATE_TOOLS:
            # Checked on the DECLARED list: the runner's cap applies after
            # resolution, so 40 candidates of which 30 are stale would slip
            # past it and only fail at run time.
            issues.append(
                {
                    "code": "call_tool.too_many_candidates",
                    "message": (
                        f"{len(names)} tools are offered to the model; at most "
                        f"{MAX_CANDIDATE_TOOLS} may be. Split the step, or "
                        "narrow the list."
                    ),
                    "severity": "warning",
                    "node_id": node.get("id"),
                    "edge_id": None,
                    "field": "candidate_tools",
                }
            )
        missing = [
            n
            for n in names
            if ("candidate_tools", n) not in refused
            and not (n in ambiguous and n not in candidates_pinned)
            and not writer_can(n, candidates_pinned)
        ]
        if not missing:
            continue
        gone = ", ".join(f"`{n}`" for n in missing)
        # Losing SOME candidates narrows the choice; losing them all leaves the
        # model nothing to call, and the node fails at run time.
        if not any(writer_can(n, candidates_pinned) for n in names):
            message = (
                f"None of this node's tools are available to you ({gone}) — "
                "the model would have nothing to call."
            )
        else:
            message = (
                f"{gone} is no longer available to you and will be left out "
                "of what the model may choose from."
            )
        issues.append(
            {
                "code": "call_tool.candidate_missing",
                "message": message,
                "severity": "warning",
                "node_id": node.get("id"),
                "edge_id": None,
                "field": "candidate_tools",
            }
        )
    return issues


async def check_integration_references(
    db: AsyncSession,
    user: User,
    *,
    graph: Any,
    previous_graph: Any,
    flow_owner_id: Any,
) -> list[dict[str, Any]]:
    """Refuse an integration a writer puts on a flow they cannot see.

    A flow runs its nodes with the integrations they name, as stored. So whoever writes the graph - a co-editor, the owner, the creator of a new
    flow - may only add an integration they can see themselves: otherwise an
    owner could type a colleague's integration id, share the flow with them,
    and run with their credentials. Only an id the flow did not hold anywhere
    before this save is checked: a node configured earlier, moved or copied on
    the canvas, is never re-litigated. Admins are not checked. For a writer
    who is not a co-editor (``flow_owner_id`` is theirs, or ``None`` for a new
    flow), an id naming no integration saves as before: an imported flow may
    carry ids from another install, and the run reports them.
    """
    from app.services.integration_credentials import (
        flow_integration_refs,
        integration_ids_writer_cannot_see,
    )

    if user.is_admin:
        return []
    refs = flow_integration_refs(graph)
    if not refs:
        return []
    co_editor = flow_owner_id is not None and str(flow_owner_id) != str(user.id)
    refused = await integration_ids_writer_cannot_see(
        db,
        user.id,
        is_admin=False,
        ids=[ref[2] for ref in refs],
        stored=[ref[2] for ref in flow_integration_refs(previous_graph)],
        ignore_missing=not co_editor,
    )
    issues: list[dict[str, Any]] = []
    for node_id, field, raw in refs:
        parsed = _as_uuid(raw)
        if parsed is None or str(parsed) not in refused:
            continue
        issues.append(
            {
                "code": "integration.not_visible",
                "message": (
                    "You cannot use this integration: it is not shared with "
                    "you. Pick one you can see, or ask its owner to share it."
                ),
                "severity": "error",
                "node_id": node_id,
                "edge_id": None,
                "field": field,
            }
        )
    return issues


def _source_models() -> dict[str, Any]:
    """The table behind each id-valued dynamic source (integrations have
    their own check)."""
    from app.models.agent import Agent
    from app.models.agent_skill import AgentSkill
    from app.models.assignment import Assignment
    from app.models.custom_tool import CustomTool
    from app.models.fleet import Fleet
    from app.models.flow_event import FlowEventType
    from app.models.knowledge_base import KnowledgeBase
    from app.models.llm_config import LLMProviderConfig
    from app.models.relay import RelayGroup
    from app.models.runner import Runner
    from app.models.template_container import TemplateContainer
    from app.models.variable import Variable
    from app.services.flow_catalog import (
        SOURCE_AGENTS,
        SOURCE_ASSIGNMENTS,
        SOURCE_CUSTOM_TOOLS,
        SOURCE_EVENT_TYPES,
        SOURCE_FLEETS,
        SOURCE_FLOWS,
        SOURCE_KNOWLEDGE_BASES,
        SOURCE_LLM_PROVIDERS,
        SOURCE_RELAY_GROUPS,
        SOURCE_REPORT_TEMPLATES,
        SOURCE_RUNNERS,
        SOURCE_SKILLS,
        SOURCE_TOOLS,
        SOURCE_VARIABLES,
    )

    return {
        SOURCE_AGENTS: Agent,
        SOURCE_ASSIGNMENTS: Assignment,
        SOURCE_CUSTOM_TOOLS: CustomTool,
        SOURCE_TOOLS: CustomTool,
        SOURCE_EVENT_TYPES: FlowEventType,
        SOURCE_FLEETS: Fleet,
        SOURCE_FLOWS: Flow,
        SOURCE_KNOWLEDGE_BASES: KnowledgeBase,
        SOURCE_LLM_PROVIDERS: LLMProviderConfig,
        SOURCE_RELAY_GROUPS: RelayGroup,
        SOURCE_REPORT_TEMPLATES: TemplateContainer,
        SOURCE_RUNNERS: Runner,
        SOURCE_SKILLS: AgentSkill,
        SOURCE_VARIABLES: Variable,
        VARIABLE_PLACEHOLDERS: Variable,
    }


#: ``{{var_id.UUID}}`` placeholders in any config string: the run substitutes
#: them for whoever runs the flow, secrets included (``load_variables``).
VARIABLE_PLACEHOLDERS = "variable_placeholders"


def _strings(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _strings(item)


def _checked_refs(graph: Any) -> list[tuple[Any, str, str, str]]:
    """``graph_source_refs`` plus the platform variables a run resolves for
    whoever runs the flow: the Value node's picker (``PrimitiveExtras``, not
    a catalog field; its value is published) and every ``{{var_id.UUID}}``
    placeholder of a config string (field = the top-level config key)."""
    from app.services.flow_catalog import SOURCE_VARIABLES
    from app.services.variable_resolver import extract_variable_ids

    refs = graph_source_refs(graph)
    if isinstance(graph, dict):
        for node in graph.get("nodes") or []:
            if not isinstance(node, dict) or not isinstance(node.get("config"), dict):
                continue
            config = node["config"]
            if node.get("kind") == "util.primitive":
                raw = config.get("variable_id")
                parsed = _as_uuid(raw) if isinstance(raw, str) else None
                if parsed is not None:
                    refs.append((node.get("id"), "variable_id", SOURCE_VARIABLES, str(parsed)))
            for key, value in config.items():
                ids = {vid for text in _strings(value) for vid in extract_variable_ids(text)}
                refs.extend(
                    (node.get("id"), key, VARIABLE_PLACEHOLDERS, str(parsed))
                    for parsed in sorted(filter(None, (_as_uuid(v) for v in ids)), key=str)
                )
    return refs


async def _allowed_ids(
    db: AsyncSession, user: User, refs: list[tuple[Any, str, str, str]]
) -> dict[str, set[str]]:
    """What each source offers *user* without an admin bypass: the catalog's
    pickers (``catalog_sources``) and, for a placeholder, every variable
    their own run would resolve (secrets they can see included)."""
    from app.services.flow_access import catalog_sources

    sources = await catalog_sources(db, user, is_admin=False)
    allowed = {key: {str(o.get("value")) for o in options} for key, options in sources.items()}
    if any(ref[2] == VARIABLE_PLACEHOLDERS for ref in refs):
        from app.api.v1.helpers import get_user_group_ids
        from app.models.variable import Variable

        groups = await get_user_group_ids(db, user.id)
        allowed[VARIABLE_PLACEHOLDERS] = {
            str(row)
            for row in (
                await db.execute(select(Variable.id).where(user_visible_filter(Variable, user.id, groups)))
            ).scalars().all()
        }
    return allowed


async def _unseen_source_refs(
    db: AsyncSession,
    user: User,
    *,
    graph: Any,
    previous_graph: Any,
    missing_is_seen: bool,
) -> list[tuple[Any, str, str, str]]:
    """The refs of *graph* a non-admin *user* adds (absent from
    *previous_graph* for the same table) and the catalog does not offer them;
    with *missing_is_seen*, an id naming no row is not one of them."""
    models = _source_models()
    stored = {
        (models[source], value)
        for _node_id, _field, source, value in _checked_refs(previous_graph)
        if source in models
    }
    refs = [
        ref
        for ref in _checked_refs(graph)
        if ref[2] in models and (models[ref[2]], ref[3]) not in stored
    ]
    if not refs:
        return []
    allowed = await _allowed_ids(db, user, refs)
    unseen = [ref for ref in refs if ref[3] not in allowed.get(ref[2], set())]
    if not unseen or not missing_is_seen:
        return unseen
    by_model: dict[Any, set[uuid.UUID]] = {}
    for _node_id, _field, source, value in unseen:
        by_model.setdefault(models[source], set()).add(uuid.UUID(value))
    existing: set[tuple[Any, str]] = set()
    for model, ids in by_model.items():
        existing |= {
            (model, str(row))
            for row in (await db.execute(select(model.id).where(model.id.in_(ids)))).scalars().all()
        }
    return [ref for ref in unseen if (models[ref[2]], ref[3]) in existing]


def _without_placeholders(value: Any, ids: set[str]) -> Any:
    """*value* with every ``{{var_id.UUID}}`` of *ids* removed from its strings."""
    if isinstance(value, str):
        from app.services.variable_resolver import VAR_ID_PATTERN

        return VAR_ID_PATTERN.sub(
            lambda m: "" if str(_as_uuid(m.group(1))) in ids else m.group(0), value
        )
    if isinstance(value, dict):
        return {k: _without_placeholders(v, ids) for k, v in value.items()}
    if isinstance(value, list):
        return [_without_placeholders(v, ids) for v in value]
    return value


def _names_one_of(value: Any, ids: set[str]) -> bool:
    return str(_as_uuid(value) if isinstance(value, str) else value) in ids


async def strip_unseen_references(
    db: AsyncSession, user: User, graph: Any
) -> list[dict[str, Any]]:
    """Drop, in place, every integration and other id of *graph* its non-admin
    *user* cannot see, for a copy they will own (duplicate): a copy leaves out
    what its copier could not bind, with a warning per node. An id
    naming no row is kept, as on a write."""
    from app.services.integration_credentials import (
        flow_integration_refs,
        integration_ids_writer_cannot_see,
    )

    if user.is_admin or not isinstance(graph, dict):
        return []
    dropped = await _unseen_source_refs(
        db, user, graph=graph, previous_graph=None, missing_is_seen=True
    )
    integration_refs = flow_integration_refs(graph)
    if integration_refs:
        hidden = await integration_ids_writer_cannot_see(
            db,
            user.id,
            is_admin=False,
            ids=[ref[2] for ref in integration_refs],
            stored=[],
            ignore_missing=True,
        )
        dropped += [
            (node_id, field, "integrations", str(_as_uuid(raw)))
            for node_id, field, raw in integration_refs
            if _as_uuid(raw) is not None and str(_as_uuid(raw)) in hidden
        ]
    by_node: dict[Any, dict[str, dict[str, set[str]]]] = {}
    for node_id, field, source, value in dropped:
        kind = "placeholder" if source == VARIABLE_PLACEHOLDERS else "id"
        by_node.setdefault(node_id, {}).setdefault(field, {}).setdefault(kind, set()).add(value)
    issues: list[dict[str, Any]] = []
    for node in graph.get("nodes") or []:
        if not isinstance(node, dict) or node.get("id") not in by_node:
            continue
        config = node.get("config")
        if not isinstance(config, dict):
            continue
        for field, kinds in by_node[node.get("id")].items():
            values = kinds.get("id", set())
            current = config.get(field)
            if kinds.get("placeholder"):
                config[field] = _without_placeholders(current, kinds["placeholder"])
            elif isinstance(current, list):
                config[field] = [v for v in current if not _names_one_of(v, values)]
            elif isinstance(current, dict):
                config[field] = {k: v for k, v in current.items() if not _names_one_of(v, values)}
            else:
                config[field] = None
            issues.append(
                {
                    "code": "reference.not_copied",
                    "message": (
                        f"The copy leaves out what the original's {field} names "
                        "here: you cannot use it. Pick one you can see, or ask "
                        "its owner to share it."
                    ),
                    "severity": "warning",
                    "node_id": node.get("id"),
                    "edge_id": None,
                    "field": field,
                }
            )
    return issues


async def check_source_references(
    db: AsyncSession,
    user: User,
    *,
    graph: Any,
    previous_graph: Any,
    flow_owner_id: Any,
) -> list[dict[str, Any]]:
    """Refuse an agent, assignment, custom tool, knowledge base, skill, flow,
    event type or runner a writer puts on a flow they cannot see.

    ``check_integration_references``'s rule for every other id a config
    field fills from a dynamic source: a run reads each id as stored,
    so the write is where an id its writer could never have picked is
    refused. An id counts as seen when the catalog offers it to the writer
    (``catalog_sources``, the builder's own pickers). Only an id the flow did
    not hold anywhere before this save is checked; admins are not checked
    (their agents are held by ``check_three_tier_references``). For a writer
    who is not a co-editor, an id naming no row saves as before (an imported
    flow may carry ids from another install); a co-editor's is refused like
    a hidden one.
    """
    if user.is_admin:
        return []
    co_editor = flow_owner_id is not None and str(flow_owner_id) != str(user.id)
    unseen = await _unseen_source_refs(
        db, user, graph=graph, previous_graph=previous_graph, missing_is_seen=not co_editor
    )
    issues: list[dict[str, Any]] = []
    for node_id, field, source, value in unseen:
        issues.append(
            {
                "code": "reference.not_visible",
                "message": (
                    f"You cannot use {value} here: it is not among the "
                    f"{source.replace('_', ' ')} you can see. Pick one you can "
                    "see, or ask its owner to share it."
                ),
                "severity": "error",
                "node_id": node_id,
                "edge_id": None,
                "field": field,
            }
        )
    # Every flow write calls this check, so trace inspection is held here too.
    return issues + await check_trace_tool_references(
        db, user, graph=graph, previous_graph=previous_graph
    )


def _wired_trace_suites(graph: Any) -> list[tuple[Any, str, set[str]]]:
    """``(agent node id, agent id, trace suites)`` for every agent node a Tool
    node wires trace inspection into, read the way a run reads it."""
    from app.services.builtin_tool_aliases import runtime_builtin_tool_ids
    from app.services.builtin_tools.traces import TRACE_SUITE_IDS
    from app.services.flow_runner import _nodes_by_id, wired_capabilities

    if not isinstance(graph, dict):
        return []
    nodes = _nodes_by_id(graph)
    wired: list[tuple[Any, str, set[str]]] = []
    for node_id, node in nodes.items():
        agent_id = _as_uuid(str((node.get("config") or {}).get("agent_id") or ""))
        if node.get("kind") != "agent" or agent_id is None:
            continue
        tools = wired_capabilities(graph, node_id, nodes)["tools"]
        suites = set(
            runtime_builtin_tool_ids(
                [value.removeprefix("builtin:") for value in tools if value.startswith("builtin:")], None
            )
        )
        if suites & TRACE_SUITE_IDS:
            wired.append((node_id, str(agent_id), suites & TRACE_SUITE_IDS))
    return wired


async def check_trace_tool_references(
    db: AsyncSession,
    user: User,
    *,
    graph: Any,
    previous_graph: Any,
) -> list[dict[str, Any]]:
    """Refuse trace inspection a writer wires onto an agent whose traces they may not read.

    A Tool node extends the agent it feeds, and trace inspection reads
    that agent's own traces - every user's conversations with it - so the
    writer must pass the rule the chats follow (``traces.may_read_agent_traces``).
    Only a suite the flow did not already wire onto that agent is checked, and
    not one the agent carries itself; an agent id naming no row saves (the run
    reports it). The run holds the flow's acting user to the same rule
    (``agent_chat._turn_trace_tool_ids``), for a flow saved before this check.
    """
    from app.api.v1.helpers import get_user_group_ids
    from app.models.agent import Agent
    from app.services.builtin_tool_aliases import runtime_builtin_tool_ids
    from app.services.builtin_tools.traces import may_read_agent_traces

    stored = {
        (agent_id, suite)
        for _node_id, agent_id, suites in _wired_trace_suites(previous_graph)
        for suite in suites
    }
    added = [
        (node_id, agent_id, {suite for suite in suites if (agent_id, suite) not in stored})
        for node_id, agent_id, suites in _wired_trace_suites(graph)
    ]
    added = [entry for entry in added if entry[2]]
    if not added:
        return []
    ids = {uuid.UUID(agent_id) for _node_id, agent_id, _suites in added}
    agents = {
        str(agent.id): agent
        for agent in (await db.execute(select(Agent).where(Agent.id.in_(ids)))).scalars().all()
    }
    group_ids = await get_user_group_ids(db, user.id)
    issues: list[dict[str, Any]] = []
    for node_id, agent_id, suites in added:
        agent = agents.get(agent_id)
        if agent is None or not suites - set(
            runtime_builtin_tool_ids(agent.builtin_tool_ids, getattr(agent, "builtin_allowed_tools", None))
        ):
            continue
        if await may_read_agent_traces(db, user, agent, group_ids):
            continue
        issues.append(
            {
                "code": "trace_tools.not_readable",
                "message": (
                    "You cannot give this agent trace inspection: its traces hold "
                    "its users' conversations, which you may not read. Remove the "
                    "trace tool, or ask an administrator or the agent's owner for "
                    "chat access."
                ),
                "severity": "error",
                "node_id": node_id,
                "edge_id": None,
                "field": "agent_id",
            }
        )
    return issues


def graph_source_refs(graph: Any) -> list[tuple[Any, str, str, str]]:
    """``(node_id, field, source, value)`` for every id-shaped value a graph
    holds in a config field the catalog fills from a dynamic source: the
    value itself, the items of a list, the values of a mapping (a Call-tool
    node's ``candidate_tool_ids``)."""
    from app.services.flow_catalog import get_node_kind

    refs: list[tuple[Any, str, str, str]] = []
    if not isinstance(graph, dict):
        return refs
    for node in graph.get("nodes") or []:
        if not isinstance(node, dict):
            continue
        spec = get_node_kind(str(node.get("kind") or ""))
        config = node.get("config") or {}
        if spec is None or not isinstance(config, dict):
            continue
        for field in spec.config_fields:
            if not field.source:
                continue
            value = config.get(field.name)
            if isinstance(value, dict):
                items = list(value.values())
            else:
                items = value if isinstance(value, (list, tuple)) else [value]
            for item in items:
                parsed = _as_uuid(item) if isinstance(item, str) else None
                if parsed is not None:
                    refs.append((node.get("id"), field.name, field.source, str(parsed)))
    return refs


async def check_three_tier_references(
    db: AsyncSession,
    user: User,
    *,
    graph: Any,
    previous_graph: Any = None,
) -> list[dict[str, Any]]:
    """Refuse an object an admin's agent puts on, or runs in, a flow past the
    admin's own, group-shared and company-managed ones while
    ``AGENT_TOOLS_STRICT_ADMIN_ACCESS`` is set.

    A flow runs the agents, assignments, tools, knowledge bases, integrations
    and sub-flows its nodes name, as stored, so keeping the tool's catalog to
    the three tiers is not enough: an id typed into the graph reaches past it.
    Every id a dynamic source fills must be one of the options that source
    offers the admin's three tiers (``catalog_sources(is_admin=False)``).
    With *previous_graph*, only an id it did not hold is checked (a save); a
    run passes none and checks them all. Empty outside strict mode and for a
    member, whose reach the tools already share with REST.
    """
    from app.api.v1.helpers import agent_tool_admin_bypass

    if not user.is_admin or agent_tool_admin_bypass(user):
        return []
    # The platform variables too (``_checked_refs``), and a baseline per
    # source so an id stored in one field is not waved through in another.
    refs = _checked_refs(graph)
    stored = {(ref[2], ref[3]) for ref in _checked_refs(previous_graph)}
    refs = [ref for ref in refs if (ref[2], ref[3]) not in stored]
    if not refs:
        return []
    allowed = await _allowed_ids(db, user, refs)
    issues: list[dict[str, Any]] = []
    for node_id, field, source, value in refs:
        if source not in allowed or value in allowed[source]:
            continue
        issues.append(
            {
                "code": "reference.outside_tiers",
                "message": (
                    f"{value} is not among the {source.replace('_', ' ')} your own, "
                    "group-shared and company-managed ones include, which an "
                    "agent may not use while AGENT_TOOLS_STRICT_ADMIN_ACCESS is set."
                ),
                "severity": "error",
                "node_id": node_id,
                "edge_id": None,
                "field": field,
            }
        )
    return issues


def _reaches(
    edges: dict[uuid.UUID, list[uuid.UUID]], start: uuid.UUID, goal: uuid.UUID
) -> bool:
    """Can *start* reach *goal* by following calls? (start == goal counts.)"""
    stack, seen = [start], set()
    while stack:
        current = stack.pop()
        if current == goal:
            return True
        if current in seen:
            continue
        seen.add(current)
        stack.extend(edges.get(current, ()))
    return False
