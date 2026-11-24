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

"""Visual flow builder API.

CRUD for :class:`app.models.flow.Flow`, plus everything the builder canvas
needs: the node catalog, dry validation, version history and JSON
import/export.

Visibility follows the standard three-tier model (own + group-shared +
company-managed) via the shared helpers in ``app/api/v1/helpers.py``.

**Authoring only.** ``status`` is writable between ``draft`` and ``archived``
only; ``active`` is refused, because activation is an explicit act with its own
preconditions (zero validation errors + at least one trigger) and lives in
``flow_runs.py``. Accepting an ``active`` flow that nothing can start would be
worse than refusing it.
"""

from __future__ import annotations

import logging
import uuid
from copy import deepcopy
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, require_agentic_ai_licensed
from app.api.v1.helpers import (
    ENTITY_LIST_SCOPE_DESCRIPTION,
    EntityListScope,
    apply_group_share_fields,
    apply_owner_transfer,
    assert_budget_within_ceiling,
    can_manage,
    get_manageable_entity,
    get_user_group_ids,
    handle_company_managed_toggle,
    list_scope_admin_bypass,
    parse_path_id,
    require_admin_for_company_managed,
    resolve_owner_names,
    user_visible_filter,
    user_visible_query,
)
from app.api.v1.schemas.flow import (
    FlowCallableToolResponse,
    FlowChatEntryResponse,
    FlowCreate,
    FlowDetachReportResponse,
    FlowDetachRequest,
    FlowDetachResponse,
    FlowImportReportResponse,
    FlowImportRequest,
    FlowImportResponse,
    FlowIntegrationOperationResponse,
    FlowNodeCatalogResponse,
    FlowOutputSchemasRequest,
    FlowOutputSchemasResponse,
    FlowResponse,
    FlowRestoreRequest,
    FlowSummaryResponse,
    FlowUpdate,
    FlowValidateRequest,
    FlowValidationResponse,
    FlowVersionDetailResponse,
    FlowVersionListItemResponse,
    FlowVersionListResponse,
)
from app.core.database import get_db
from app.core.security_options import security_option
from app.models.flow import (
    FLOW_STATUS_ACTIVE,
    FLOW_STATUS_ARCHIVED,
    FLOW_STATUS_DRAFT,
    FLOW_VERSION_RETENTION,
    Flow,
    FlowVersion,
)
from app.models.integration import Integration
from app.models.user import User
from app.services.assignment_pause import (
    is_paused_by_deactivated_owner,
    owner_not_deactivated,
)
from app.services.audit import log_audit
from app.services.budget_policy import (
    ALL_KEYS as BUDGET_OVERRIDE_KEYS,
)
from app.services.budget_policy import (
    ceiling_violations,
    clamp_to_ceilings,
)
from app.services.budget_policy import (
    parse_overrides as parse_budget_overrides,
)
from app.services.flow_access import available_node_kinds, catalog_sources
from app.services.flow_catalog import (
    EDGE_KINDS,
    categories,
)
from app.services.flow_detach import CHILD_FLOW_SENTINEL, collapse_graph, split_graph
from app.services.flow_graph import (
    MAX_EDGES,
    MAX_NODES,
    empty_graph,
    normalize_graph,
    validate_graph,
)
from app.services.flow_import import (
    FlowImportError,
    export_flow,
    import_flow_document,
)
from app.services.flow_references import (
    check_flow_references,
    check_integration_references,
    check_source_references,
    check_tool_references,
    strip_unseen_references,
)
from app.services.flow_versioning import (
    create_flow_version,
    get_flow_version,
    list_flow_versions,
    resolve_author_names,
    restore_flow_version,
)
from app.services.integration_user_credentials import usable_credential_holder

logger = logging.getLogger(__name__)

router = APIRouter()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


# Config keys that are scratch/test content, not flow definition — typing a
# test message on the Chat trigger's embedded input must not read as "the
# flow changed" (no version, no unsaved-changes gate), exactly like moving a
# node. Keyed by node kind.
_TEST_ONLY_CONFIG_KEYS: dict[str, frozenset[str]] = {
    "trigger.chat": frozenset({"default_message"}),
}

# Presentation-only config keys on EVERY kind — a utility's flowchart shape
# changes how the node draws, never what it does.
_PRESENTATION_CONFIG_KEYS: frozenset[str] = frozenset({"shape", "breakpoint"})


def _strip_test_only_config(node: dict[str, Any]) -> dict[str, Any]:
    scratch = _TEST_ONLY_CONFIG_KEYS.get(node.get("kind") or "", frozenset())
    dropped = scratch | _PRESENTATION_CONFIG_KEYS
    config = node.get("config")
    if not isinstance(config, dict):
        return node
    return {
        **node,
        "config": {k: v for k, v in config.items() if k not in dropped},
    }


def _semantic_shape(graph: dict[str, Any]) -> dict[str, Any]:
    """The graph minus its layout — what versioning should care about.

    Node positions and the viewport are presentation, and a Chat trigger's
    embedded test message is scratch content; two graphs that differ only
    there DO the same thing, so an update carrying such a change persists
    without minting a version.
    """
    return {
        "nodes": [
            _strip_test_only_config({k: v for k, v in node.items() if k != "position"})
            for node in graph.get("nodes") or []
            if isinstance(node, dict)
        ],
        "edges": [
            {k: v for k, v in edge.items() if k != "path_style"}
            for edge in graph.get("edges") or []
            if isinstance(edge, dict)
        ],
    }


def _node_count(graph: Any) -> int:
    if isinstance(graph, dict) and isinstance(graph.get("nodes"), list):
        return len(graph["nodes"])
    return 0


def _to_summary(
    flow: Flow,
    *,
    can_manage: bool,
    owner_name: str | None = None,
    owner_paused: bool = False,
) -> FlowSummaryResponse:
    return FlowSummaryResponse(
        id=str(flow.id),
        user_id=str(flow.user_id) if flow.user_id else None,
        owner_name=owner_name,
        name=flow.name,
        description=flow.description or "",
        status=flow.status,
        version_number=flow.version_number,
        node_count=_node_count(flow.graph),
        tags=flow.tags or [],
        is_company_managed=flow.is_company_managed,
        shared_group_ids=flow.shared_group_ids,
        can_manage=can_manage,
        paused_by_owner_deactivation=owner_paused,
        created_at=flow.created_at.isoformat(),
        updated_at=flow.updated_at.isoformat(),
    )


async def _owner_paused(db: AsyncSession, flow: Flow, user: User) -> bool:
    """The owner-paused flag for one flow. The caller is signed in, so active."""
    if flow.user_id == user.id:
        return False
    return await is_paused_by_deactivated_owner(db, flow)


def _to_response(
    flow: Flow,
    *,
    can_manage: bool,
    owner_name: str | None = None,
    issues: list[dict[str, Any]] | None = None,
    owner_paused: bool = False,
) -> FlowResponse:
    return FlowResponse(
        **_to_summary(
            flow,
            can_manage=can_manage,
            owner_name=owner_name,
            owner_paused=owner_paused,
        ).model_dump(),
        graph=flow.graph or empty_graph(),
        issues=issues or [],
    )


def _to_version_item(
    version: FlowVersion, *, author_name: str | None, is_active: bool
) -> FlowVersionListItemResponse:
    return FlowVersionListItemResponse(
        id=str(version.id),
        version_number=version.version_number,
        author_id=str(version.author_id) if version.author_id else None,
        author_name=author_name,
        commit_message=version.commit_message,
        is_active=is_active,
        is_locked=version.is_locked,
        node_count=_node_count(version.graph),
        created_at=version.created_at.isoformat(),
    )


def _budget_node_configs(graph: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    """The Budget nodes of a graph, by node id."""
    return {
        str(node.get("id")): (node.get("config") or {})
        for node in ((graph or {}).get("nodes") or [])
        if (node.get("kind") or "") == "capability.budget"
        and isinstance(node.get("id"), str)
    }


def _lower_budget_nodes_to_ceiling(graph: dict[str, Any], user: User) -> None:
    """Lower every Budget node in a COPIED graph to the platform ceiling.

    The graph-level counterpart of ``helpers.lower_budget_to_ceiling``, for the
    paths that carry a budget the caller did not author: duplicating a flow,
    importing a document, restoring an older version. Refusing those over a
    number somebody else typed would block routine work — duplicating a
    company-managed flow is how you adapt one — to prevent an escalation that
    lowering the value already prevents.

    The directly authored paths (create, update, detach) refuse instead, so the
    author gets a message naming the dimension and the ceiling. An admin's copy
    is untouched: they could have typed the value anyway.

    Mutates ``graph`` in place, since every caller is about to persist it.
    """
    if user.is_admin:
        return
    for node in graph.get("nodes") or []:
        if not isinstance(node, dict) or node.get("kind") != "capability.budget":
            continue
        config = node.get("config")
        if not isinstance(config, dict):
            continue
        lowered = clamp_to_ceilings(parse_budget_overrides(config)).to_dict()
        # Keep the node's non-budget config (a label, a future field) and
        # replace only the policy keys, so an unrelated key is never dropped.
        node["config"] = {
            **{k: v for k, v in config.items() if k not in BUDGET_OVERRIDE_KEYS},
            **lowered,
        }


def _budget_node_targets(
    graph: dict[str, Any] | None,
) -> dict[str, set[tuple[str, str]]]:
    """What each Budget node is wired to, by node id.

    ``{budget_node_id: {(target_node_id, target_handle)}}``. The handle is part
    of the key because the same node legitimately feeds an agent's `budget`
    socket and a Flow node's, and those are different grants.
    """
    budget_ids = set(_budget_node_configs(graph))
    targets: dict[str, set[tuple[str, str]]] = {}
    for edge in ((graph or {}).get("edges") or []):
        if not isinstance(edge, dict):
            continue
        source = edge.get("source")
        if source not in budget_ids:
            continue
        target = edge.get("target")
        if not isinstance(target, str):
            continue
        targets.setdefault(source, set()).add(
            (target, str(edge.get("target_handle") or ""))
        )
    return targets


def _assert_budget_nodes_authorised(
    graph: dict[str, Any],
    previous_graph: dict[str, Any] | None,
    user: User,
) -> None:
    """A Budget node may not raise a limit above the platform ceiling.

    The same authority rule the agent's Budget tab enforces, applied to the
    canvas: a flow is just another place to write a spending policy, and the
    platform's ``AGENT_LOOP_MAX_*`` settings would mean nothing if a
    non-admin could route around them by wiring a Budget node instead.

    Compared per NODE ID against what is stored, so a value an admin already
    authorised is not re-litigated: a non-admin moving nodes around, renaming
    the flow or editing an unrelated branch re-sends the whole graph, and that
    must not fail on a budget they never touched.

    The VALUE is not the whole story, though. This feature's own premise is
    that "the wiring is the distribution", so an above-ceiling node an admin
    authorised for one expensive agent could otherwise be re-wired by a
    non-admin to every other agent in the flow — identical config, zero value
    violations, the admin's exception quietly spread across the canvas. So the
    edge set of such a node is checked too: a non-admin may remove targets
    (that only tightens the distribution) but not add them.
    """
    stored = _budget_node_configs(previous_graph)
    stored_targets = _budget_node_targets(previous_graph)
    current_targets = _budget_node_targets(graph)
    for node_id, config in _budget_node_configs(graph).items():
        assert_budget_within_ceiling(
            config,
            previous=stored.get(node_id),
            user=user,
            what="a Budget node",
        )
        if user.is_admin:
            continue
        # Only a node that actually exceeds the ceiling is edge-checked. A node
        # within the platform budget is one any user could have drawn from
        # scratch, so where it is wired is nobody's business but the author's.
        if not ceiling_violations(
            parse_budget_overrides(config), is_admin=False
        ):
            continue
        added = current_targets.get(node_id, set()) - stored_targets.get(
            node_id, set()
        )
        if added:
            raise HTTPException(
                status_code=403,
                detail=(
                    "This Budget node exceeds the platform budget, so only an "
                    "administrator can wire it to more agents. It stays "
                    "attached to the ones it already serves."
                ),
            )


async def _assert_writable(
    db: AsyncSession,
    user: User,
    graph: dict[str, Any],
    *,
    flow_id: uuid.UUID | None = None,
    previous_graph: dict[str, Any] | None = None,
    flow_owner_id: uuid.UUID | None = None,
    is_company_managed: bool = False,
) -> list[dict[str, Any]]:
    """Full write-time validation: the pure graph rules PLUS cross-flow ones.

    `flow.call` turns a set of flows into a graph of its own, and "A calls B
    calls A" is invisible inside either document. That check needs the database,
    so it lives here rather than in the pure validator the canvas calls on every
    keystroke.

    It is also the single choke point every graph write passes through, which
    is why the Budget nodes' admin gate lives here rather than in each
    endpoint.
    """
    warnings = _assert_graph_valid(graph)
    _assert_budget_nodes_authorised(graph, previous_graph, user)
    user_group_ids = await get_user_group_ids(db, user.id)
    cross = await check_flow_references(
        db,
        user,
        # No id yet = a flow nothing can point at, so no cycle can close on it.
        # The dangling-call warnings still apply, which is why the check runs.
        flow_id=flow_id or uuid.uuid4(),
        graph=graph,
        user_group_ids=user_group_ids,
    )
    # The tool names it stores, read the way a run reads them; a co-editor
    # may only add one that designates a tool they can use.
    cross += await check_tool_references(
        db,
        user,
        graph=graph,
        user_group_ids=user_group_ids,
        previous_graph=previous_graph,
        flow_owner_id=flow_owner_id,
        is_company_managed=is_company_managed,
    )
    # An existing flow edited by someone other than its owner: an integration
    # they put on it must be one they can see.
    cross += await check_integration_references(
        db,
        user,
        graph=graph,
        previous_graph=previous_graph,
        flow_owner_id=flow_owner_id,
    )
    # The same for every other id a node names (an agent, a custom tool, a
    # knowledge base ...): a run reads them as stored.
    cross += await check_source_references(
        db,
        user,
        graph=graph,
        previous_graph=previous_graph,
        flow_owner_id=flow_owner_id,
    )
    errors = [i for i in cross if i["severity"] == "error"]
    if errors:
        raise HTTPException(
            status_code=422,
            detail={
                "message": "This flow cannot be saved as it stands.",
                "issues": cross,
            },
        )
    return warnings + cross


def _assert_graph_valid(graph: dict[str, Any]) -> list[dict[str, Any]]:
    """Validate a graph, refusing the write on errors.

    Returns the *warnings* so they can ride along in the response — a draft
    with a missing required field still saves, and the builder needs to know.
    """
    result = validate_graph(graph)
    if not result.is_valid:
        raise HTTPException(
            status_code=422,
            detail={
                "message": "This flow cannot be saved as it stands.",
                "issues": [i.to_dict() for i in result.issues],
            },
        )
    return [i.to_dict() for i in result.warnings]


def _resolve_status(requested: str | None, current: str) -> str:
    """Phase 1 lifecycle: draft ⇄ archived. ``active`` is not yet reachable."""
    if requested is None:
        return current
    if requested == FLOW_STATUS_ACTIVE:
        raise HTTPException(
            status_code=400,
            detail=(
                "Activation is an explicit act with its own gates — use "
                "POST /flows/{id}/activate instead of writing status."
            ),
        )
    if requested not in (FLOW_STATUS_DRAFT, FLOW_STATUS_ARCHIVED):
        raise HTTPException(status_code=400, detail=f"Unknown status '{requested}'.")
    return requested


async def _get_visible_flow(
    db: AsyncSession,
    user: User,
    flow_id: str,
    *,
    user_group_ids: list[str] | None = None,
) -> Flow:
    """Load a flow the user may *read*, or 404."""
    if user_group_ids is None:
        user_group_ids = await get_user_group_ids(db, user.id)
    result = await db.execute(
        select(Flow).where(
            Flow.id == parse_path_id(flow_id, not_found_detail="Flow not found"),
            user_visible_filter(Flow, user.id, user_group_ids, is_admin=user.is_admin),
        )
    )
    flow = result.scalar_one_or_none()
    if not flow:
        raise HTTPException(status_code=404, detail="Flow not found")
    return flow


# ---------------------------------------------------------------------------
# Node catalog
# ---------------------------------------------------------------------------


# NOTE: declared before `/{flow_id}` so the literal path is not swallowed by the
# dynamic one.
@router.get("/node-catalog", response_model=FlowNodeCatalogResponse)
async def get_node_catalog(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """The node library: static node kinds plus the caller's live entities.

    The builder renders both the library panel and every node configuration
    form from this payload, which is what keeps the UI and the server-side
    validator from drifting apart.
    """
    return FlowNodeCatalogResponse(
        categories=categories(),
        node_kinds=available_node_kinds(),
        edge_kinds=list(EDGE_KINDS),
        sources=await catalog_sources(db, user),
        limits={"max_nodes": MAX_NODES, "max_edges": MAX_EDGES},
    )


class AutoLayoutRequest(BaseModel):
    graph: dict = {}


@router.post("/auto-layout")
async def auto_layout_flow_graph(
    body: AutoLayoutRequest,
    _user: User = Depends(get_current_user),
):
    """Fill missing node positions with a topological layout.

    Headless authoring parity: an agent (or an import) composing a graph
    without positions gets a legible canvas instead of a validation error.
    Nodes that already carry a position keep it.
    """
    from app.services.flow_graph import auto_layout

    return {"graph": auto_layout(dict(body.graph or {}))}


class LintCodeRequest(BaseModel):
    code: str = ""


@router.post("/lint-code")
async def lint_flow_code(
    body: LintCodeRequest,
    _user: User = Depends(get_current_user),
):
    """Syntax-check a Run-code script without executing it.

    The expanded editor's Validate button. Parse-only (``ast.parse``) — the
    walker hands the code to the code interpreter verbatim, so raw-code
    syntax is execution-equivalent. Mirrors the node's own gating: hidden
    node, refused check.
    """
    import ast as _ast


    if security_option("disable_code_interpreter"):
        raise HTTPException(
            status_code=403,
            detail="The code interpreter is disabled by the platform administrator.",
        )
    try:
        _ast.parse(body.code or "")
        return {"status": "ok"}
    except SyntaxError as exc:
        return {
            "status": "error",
            "message": exc.msg or "Invalid Python syntax.",
            "line": exc.lineno,
        }


@router.post("/validate", response_model=FlowValidationResponse)
async def validate_flow_graph(
    body: FlowValidateRequest,
    _user: User = Depends(get_current_user),
):
    """Dry validation — no write. The canvas calls this as the graph changes."""
    return FlowValidationResponse(**validate_graph(body.graph).to_dict())


@router.post("/output-schemas", response_model=FlowOutputSchemasResponse)
async def flow_output_schemas(
    body: FlowOutputSchemasRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """What each node of a draft graph declares for its outputs.

    The builder's typed paths (`{{a1.fields.severity}}`) come from here, so
    the rule of which output carries which structure stays the run's own
    (``flow_output_schemas``). Agents and event types are read with the
    caller's visibility. No write. A draft larger than a save accepts is a
    422 (``FlowOutputSchemasRequest``).
    """
    from app.services.flow_output_schemas import declared_output_schemas

    return FlowOutputSchemasResponse(nodes=await declared_output_schemas(db, user, body.graph))


@router.get(
    "/integration-operations/{integration_id}",
    response_model=list[FlowIntegrationOperationResponse],
)
async def list_integration_operations(
    integration_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """The callable operations of one connected integration.

    Surfaces the SAME ToolDefinitions the agent runtime builds (name,
    description, JSON Schema of the arguments), so the Integration node's
    drawer can offer a real picker + a typed parameters form. Declared
    before ``/{flow_id}`` so the literal path is not swallowed by it.
    """
    from app.services.integration_tools import BindingBasis, ToolBindings, build_integration_tools

    integration = (
        await db.execute(
            select(Integration).where(
                Integration.id == parse_path_id(integration_id, not_found_detail="Integration not found"),
                user_visible_filter(
                    Integration,
                    user.id,
                    await get_user_group_ids(db, user.id),
                    is_admin=user.is_admin,
                ),
            )
        )
    ).scalar_one_or_none()
    if integration is None:
        raise HTTPException(status_code=404, detail="Integration not found")
    # The caller's own account on a per_user integration.
    await usable_credential_holder(db, integration, user.id, is_admin=user.is_admin)
    try:
        definitions, _executor = await build_integration_tools(
            db,
            ToolBindings(
                {integration.provider: str(integration.id)},
                basis=BindingBasis.CALLER_CHECKED,
                reason="visible to the caller and usable_credential_holder",
            ),
            context={"source": "flow_operations", "credential_user_id": str(user.id)},
        )
    except Exception:  # pragma: no cover — a broken provider must not 500 the drawer
        logger.exception(
            "Could not build operations for integration %s", integration.id
        )
        definitions = []
    return [
        FlowIntegrationOperationResponse(
            name=d.name,
            description=d.description or "",
            input_schema=d.input_schema or {},
        )
        for d in definitions
    ]


@router.get("/callable-tools", response_model=list[FlowCallableToolResponse])
async def list_callable_tools(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Tools the Call-tool node can invoke directly, with their signatures.

    Built-ins are the flow-callable subset (self-contained executors — no
    agent context needed); custom tools are the enabled ones the caller can
    use, each with its id: the drawer stores the id of the tool it picks next
    to its name, so tools sharing a name are all offered (``origin``
    and ``owner_name`` tell them apart), and ``designated`` marks the one a
    node storing the name alone runs for the caller
    (``resolve_custom_tools_by_name``). A custom tool named like a
    built-in is left out: the built-in wins that name wherever a node names
    it. Integration operations have their own node and endpoint. Declared
    before ``/{flow_id}`` so the literal path is not swallowed by it.
    """
    from app.models.custom_tool import CustomTool
    from app.services.flow_runner import (
        custom_tool_scope_of,
        flow_builtin_tools,
        resolve_custom_tools_by_name,
    )

    items: list[FlowCallableToolResponse] = []
    builtin_names: set[str] = set()
    for definition, _executor in flow_builtin_tools():
        builtin_names.add(definition.name)
        items.append(
            FlowCallableToolResponse(
                name=definition.name,
                label=definition.name.replace("_", " ").capitalize(),
                description=definition.description or "",
                source="builtin",
                input_schema=definition.input_schema or {},
            )
        )
    scope = await custom_tool_scope_of(db, user)
    custom_tools = [
        tool
        for tool in (
            await db.execute(
                user_visible_query(CustomTool, user.id, list(scope.group_ids), is_admin=user.is_admin).where(
                    CustomTool.enabled.is_(True)
                )
            )
        )
        .scalars()
        .all()
        if tool.name not in builtin_names
    ]
    designated, _ambiguous = await resolve_custom_tools_by_name(db, {tool.name for tool in custom_tools}, scope)
    designated_ids = {tool.id for tool in designated.values()}
    group_ids = {str(g) for g in scope.group_ids}
    owner_names = await resolve_owner_names(db, custom_tools)

    def origin(tool: CustomTool) -> str:
        if tool.user_id == user.id:
            return "own"
        if tool.is_company_managed:
            return "company"
        shared_users = {str(u) for u in tool.shared_user_ids or []}
        if str(user.id) in shared_users or group_ids & {str(g) for g in tool.shared_group_ids or []}:
            return "shared"
        return "other"

    for tool in custom_tools:
        items.append(
            FlowCallableToolResponse(
                id=str(tool.id),
                name=tool.name,
                label=tool.name,
                description=(tool.description or "")[:200],
                source="custom",
                input_schema=tool.input_schema or {},
                designated=tool.id in designated_ids,
                origin=origin(tool),
                owner_name=owner_names.get(str(tool.user_id)) if tool.user_id else None,
            )
        )
    return items


@router.get("/chat-entries", response_model=list[FlowChatEntryResponse])
async def list_flow_chat_entries(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Conversational entries the chat picker shows for flows.

    An ACTIVE, visible flow whose Chat start point leads to a chat-enabled
    agent node is a chat target — exposed under the FLOW's name (activation
    is the publish act), with the underlying agent as the endpoint the
    conversation binds to.
    """
    from app.services.flow_runner import find_trigger_nodes

    user_group_ids = await get_user_group_ids(db, user.id)
    rows = (
        (
            await db.execute(
                user_visible_query(
                    Flow, user.id, user_group_ids, is_admin=user.is_admin
                ).where(
                    Flow.status == FLOW_STATUS_ACTIVE,
                    # A paused flow is not offered as a chat target.
                    owner_not_deactivated(Flow),
                )
            )
        )
        .scalars()
        .all()
    )
    entries: list[FlowChatEntryResponse] = []
    for flow in rows:
        graph = flow.graph or {}
        nodes_by_id = {
            n.get("id"): n
            for n in graph.get("nodes") or []
            if isinstance(n, dict)
        }
        for chat_node in find_trigger_nodes(graph, "trigger.chat"):
            edge = next(
                (
                    e
                    for e in graph.get("edges") or []
                    if isinstance(e, dict)
                    and e.get("source") == chat_node.get("id")
                    and (e.get("source_handle") or "out") == "out"
                ),
                None,
            )
            target = nodes_by_id.get((edge or {}).get("target"))
            if not isinstance(target, dict) or target.get("kind") != "agent":
                continue
            config = target.get("config") or {}
            agent_id = config.get("agent_id")
            if not agent_id or not config.get("chat_enabled"):
                continue
            entries.append(
                FlowChatEntryResponse(
                    flow_id=str(flow.id),
                    flow_name=flow.name,
                    description=flow.description,
                    agent_id=str(agent_id),
                    greeting=(chat_node.get("config") or {}).get("greeting")
                    or None,
                )
            )
            break  # one entry per flow — the first chat trigger wins
    entries.sort(key=lambda e: e.flow_name.lower())
    return entries


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


@router.get("", response_model=list[FlowSummaryResponse])
async def list_flows(
    scope: EntityListScope = Query("visible", description=ENTITY_LIST_SCOPE_DESCRIPTION),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List flows visible to the current user (own + group-shared + company-managed).

    ``scope=all`` lists every flow for an admin (what a detail page links to).
    """
    user_group_ids = await get_user_group_ids(db, user.id)
    result = await db.execute(
        user_visible_query(
            Flow, user.id, user_group_ids, is_admin=list_scope_admin_bypass(scope, user)
        ).add_columns(
            # Read with the row, so the pause costs no extra query.
            (~owner_not_deactivated(Flow)).label("owner_paused")
        )
    )
    pairs = result.all()
    flows = [flow for flow, _ in pairs]
    owner_paused = {flow.id: bool(paused) for flow, paused in pairs}
    names = await resolve_owner_names(db, flows)
    return [
        _to_summary(
            f,
            can_manage=can_manage(user, f, user_group_ids),
            owner_name=names.get(str(f.user_id)),
            owner_paused=owner_paused[f.id],
        )
        for f in flows
    ]


@router.post("", response_model=FlowResponse, status_code=status.HTTP_201_CREATED)
async def create_flow(
    body: FlowCreate,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _agentic_gate: None = Depends(require_agentic_ai_licensed),
):
    """Create a flow — blank, or pre-filled from a graph (duplicate / import)."""
    require_admin_for_company_managed(
        body, user, "Only admins can create company-managed flows"
    )

    graph = normalize_graph(body.graph) if body.graph is not None else empty_graph()
    warnings = await _assert_writable(
        db, user, graph, is_company_managed=body.is_company_managed
    )

    flow = Flow(
        user_id=None if body.is_company_managed else user.id,
        name=body.name,
        description=body.description,
        status=FLOW_STATUS_DRAFT,
        graph=graph,
        version_number=1,
        tags=body.tags or [],
        is_company_managed=body.is_company_managed,
    )
    await apply_group_share_fields(db, flow, body, partial=False)
    db.add(flow)
    await db.flush()
    await create_flow_version(
        db,
        flow_id=flow.id,
        graph=graph,
        author_id=user.id,
        commit_message="Initial version",
    )
    await log_audit(
        db,
        user=user,
        action="create",
        entity_type="flow",
        entity_id=flow.id,
        entity_name=flow.name,
        summary=f"Created flow '{flow.name}'",
        details={
            "node_count": _node_count(graph),
            "is_company_managed": flow.is_company_managed,
            "tags": flow.tags or [],
        },
        request=request,
    )
    await db.commit()
    await db.refresh(flow)
    return _to_response(
        flow,
        can_manage=can_manage(user, flow),
        owner_name=user.full_name,
        issues=warnings,
    )


@router.get("/{flow_id}", response_model=FlowResponse)
async def get_flow(
    flow_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Load a flow with its full graph."""
    user_group_ids = await get_user_group_ids(db, user.id)
    flow = await _get_visible_flow(db, user, flow_id, user_group_ids=user_group_ids)
    owner_name = None
    if flow.user_id:
        owner_name = await db.scalar(
            select(User.full_name).where(User.id == flow.user_id)
        )
    # Re-validate on read: the catalog (and the caller's visible agents) can
    # change under a saved flow, so warnings are computed fresh rather than
    # cached at write time.
    result = validate_graph(flow.graph or empty_graph())
    return _to_response(
        flow,
        can_manage=can_manage(user, flow, user_group_ids),
        owner_name=owner_name,
        issues=[i.to_dict() for i in result.issues],
        owner_paused=await _owner_paused(db, flow, user),
    )


def _assert_base_version(flow: Flow, base_version: int | None) -> None:
    """Optimistic lock: refuse a graph save built on a stale version.

    Real incident: a builder tab left open on an old graph re-saved it over
    an edit made in parallel, silently reverting a handover edge label and
    minting a new version. With ``base_version`` sent by the client, that
    save now 409s with the server's current version so the caller can reload
    and rebase instead of clobbering. ``None`` keeps the historical
    last-write-wins for callers that do not send it.
    """
    if base_version is None:
        return
    current = int(flow.version_number or 1)
    if int(base_version) != current:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "flow.version_conflict",
                "message": (
                    f"This flow changed while you were editing — it is now at "
                    f"v{current}, you loaded v{base_version}. Reload to "
                    "continue from the latest version."
                ),
                "current_version": current,
            },
        )


@router.put("/{flow_id}", response_model=FlowResponse)
async def update_flow(
    flow_id: str,
    body: FlowUpdate,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Update a flow. A graph change snapshots a new version."""
    user_group_ids = await get_user_group_ids(db, user.id)
    flow = await get_manageable_entity(
        db,
        Flow,
        user,
        flow_id,
        action="edit",
        entity_label="Flow",
        user_group_ids=user_group_ids,
    )

    warnings: list[dict[str, Any]] = []
    graph_changed = False
    if body.graph is not None:
        if body.base_version is not None:
            # Serialize the version check with the version MINT. Checking
            # against the in-memory row before ``create_flow_version`` takes
            # its ``FOR UPDATE`` lock lets two concurrent saves both pass the
            # check: the loser then waits on the lock, reads a fresh MAX() and
            # cheerfully writes the *next* version on top of the winner's —
            # a silent lost update, which is exactly the outcome the 409
            # contract exists to prevent (and the builder pins its "reload to
            # continue" UX on). Taking the same row lock first, then
            # re-reading the row, makes the check authoritative.
            await db.execute(
                select(Flow.id).where(Flow.id == flow.id).with_for_update()
            )
            await db.refresh(flow)
        _assert_base_version(flow, body.base_version)
        graph = normalize_graph(body.graph)
        warnings = await _assert_writable(
            db,
            user,
            graph,
            flow_id=flow.id,
            previous_graph=flow.graph,
            flow_owner_id=flow.user_id,
            is_company_managed=flow.is_company_managed,
        )
        # Layout-only saves (moved nodes, pan/zoom — the builder auto-saves
        # them) persist the graph but must not mint a version: versions are
        # the flow's SEMANTIC history, not a log of canvas tidying.
        graph_changed = _semantic_shape(graph) != _semantic_shape(flow.graph or {})
        flow.graph = graph

    if body.name is not None:
        flow.name = body.name
    if body.description is not None:
        flow.description = body.description
    if body.tags is not None:
        flow.tags = body.tags
    flow.status = _resolve_status(body.status, flow.status)
    await apply_group_share_fields(db, flow, body, partial=True)

    await handle_company_managed_toggle(
        db, user, flow, body.is_company_managed, agent_field=None
    )
    await apply_owner_transfer(
        db,
        actor=user,
        entity=flow,
        new_owner_id=body.user_id,
        entity_type="flow",
        entity_name=flow.name,
        request=request,
    )

    created_version = None
    if graph_changed:
        created_version = await create_flow_version(
            db,
            flow_id=flow.id,
            graph=flow.graph,
            author_id=user.id,
            commit_message=body.commit_message,
        )
        flow.version_number = created_version.version_number

    await log_audit(
        db,
        user=user,
        action="update",
        entity_type="flow",
        entity_id=flow.id,
        entity_name=flow.name,
        summary=f"Updated flow '{flow.name}'",
        details={
            "node_count": _node_count(flow.graph),
            "status": flow.status,
            "new_version": created_version.version_number if created_version else None,
        },
        request=request,
    )
    try:
        await db.commit()
    except IntegrityError as exc:
        # Belt for the version-number unique constraint. The ``FOR UPDATE``
        # lock above should make this unreachable, but if any path ever mints
        # a version without it, the honest answer is the same 409 the stale-tab
        # case returns — never a 500 the builder cannot interpret.
        await db.rollback()
        if "uq_flow_versions_flow_ver" not in str(getattr(exc, "orig", exc)):
            raise
        current = await db.scalar(
            select(Flow.version_number).where(Flow.id == flow.id)
        )
        raise HTTPException(
            status_code=409,
            detail={
                "code": "flow.version_conflict",
                "message": (
                    "This flow was saved by someone else while you were "
                    "editing. Reload to continue from the latest version."
                ),
                "current_version": int(current or 1),
            },
        ) from exc
    await db.refresh(flow)
    return _to_response(
        flow,
        can_manage=can_manage(user, flow, user_group_ids),
        issues=warnings,
        owner_paused=await _owner_paused(db, flow, user),
    )


@router.delete("/{flow_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_flow(
    flow_id: str,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Delete a flow and its version history."""
    flow = await get_manageable_entity(
        db, Flow, user, flow_id, action="delete", entity_label="Flow"
    )
    name = flow.name
    await log_audit(
        db,
        user=user,
        action="delete",
        entity_type="flow",
        entity_id=flow.id,
        entity_name=name,
        summary=f"Deleted flow '{name}'",
        request=request,
    )
    # flow_versions cascades on the FK.
    await db.delete(flow)
    await db.commit()


@router.post(
    "/{flow_id}/duplicate",
    response_model=FlowResponse,
    status_code=status.HTTP_201_CREATED,
)
async def duplicate_flow(
    flow_id: str,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _agentic_gate: None = Depends(require_agentic_ai_licensed),
):
    """Copy a visible flow into a new private draft owned by the caller."""
    from app.core.utils import fit_copy_name

    source = await _get_visible_flow(db, user, flow_id)
    # The copy is private and editable by its new owner, so it must not carry a
    # budget that owner could not have written — but the duplicate itself is
    # routine, so the values are lowered rather than the action refused.
    copied_graph = deepcopy(source.graph or empty_graph())
    _lower_budget_nodes_to_ceiling(copied_graph, user)
    # A copy of the caller's own leaves out what they could not bind:
    # a run of it would read every id as stored.
    copy_warnings = await strip_unseen_references(db, user, copied_graph)

    copy = Flow(
        user_id=user.id,
        name=fit_copy_name(source.name, Flow.__table__.c.name, suffix=" (copy)"),
        description=source.description or "",
        status=FLOW_STATUS_DRAFT,
        graph=copied_graph,
        version_number=1,
        tags=list(source.tags or []),
        is_company_managed=False,
        shared_group_ids=None,
    )
    db.add(copy)
    await db.flush()
    await create_flow_version(
        db,
        flow_id=copy.id,
        graph=copy.graph,
        author_id=user.id,
        commit_message=f"Duplicated from '{source.name}'",
    )
    await log_audit(
        db,
        user=user,
        action="create",
        entity_type="flow",
        entity_id=copy.id,
        entity_name=copy.name,
        summary=f"Duplicated flow '{source.name}'",
        details={"source_flow_id": str(source.id)},
        request=request,
    )
    await db.commit()
    await db.refresh(copy)
    return _to_response(
        copy,
        can_manage=can_manage(user, copy),
        owner_name=user.full_name,
        issues=copy_warnings,
    )


# ---------------------------------------------------------------------------
# Versions
# ---------------------------------------------------------------------------


@router.post(
    "/{flow_id}/detach",
    response_model=FlowDetachResponse,
    status_code=status.HTTP_201_CREATED,
)
async def detach_selection(
    flow_id: str,
    body: FlowDetachRequest,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _agentic_gate: None = Depends(require_agentic_ai_licensed),
):
    """Move a selection out of this flow and into a new one.

    Deliberately leaves **no reference** behind: a sub-flow node would have to
    mean something at execution time, and nothing executes yet.

    Both halves are written in one transaction, and the parent is versioned
    first — so an unwanted detach is one restore away, which is what makes it
    acceptable to drop the edges that crossed the selection boundary rather
    than refusing the whole operation.
    """
    user_group_ids = await get_user_group_ids(db, user.id)
    flow = await get_manageable_entity(
        db,
        Flow,
        user,
        flow_id,
        action="edit",
        entity_label="Flow",
        user_group_ids=user_group_ids,
    )

    # Split the graph the user is looking at, not the last saved one.
    graph = normalize_graph(body.graph)
    _assert_graph_valid(graph)
    # That graph comes from the CLIENT, so it is an authoring write like any
    # other and its Budget nodes need the same gate — a detach must not be a
    # side door for a budget a plain save would refuse. Checked against the
    # stored graph, so budgets the caller did not touch pass through.
    _assert_budget_nodes_authorised(graph, flow.graph, user)
    # Same side door for an integration, a tool name or any other id: the
    # split graph is the client's.
    integration_issues = await check_integration_references(
        db, user, graph=graph, previous_graph=flow.graph, flow_owner_id=flow.user_id
    )
    integration_issues += await check_source_references(
        db, user, graph=graph, previous_graph=flow.graph, flow_owner_id=flow.user_id
    )
    integration_issues += [
        issue
        for issue in await check_tool_references(
            db,
            user,
            graph=graph,
            user_group_ids=user_group_ids,
            previous_graph=flow.graph,
            flow_owner_id=flow.user_id,
            is_company_managed=flow.is_company_managed,
        )
        if issue["severity"] == "error"
    ]
    if integration_issues:
        raise HTTPException(
            status_code=422,
            detail={
                "message": "This flow cannot be saved as it stands.",
                "issues": integration_issues,
            },
        )
    collapse = body.mode == "collapse"
    parent_graph, child_graph, report = (
        collapse_graph(graph, body.node_ids)
        if collapse
        else split_graph(graph, body.node_ids)
    )
    if report["nodes_detached"] == 0:
        raise HTTPException(
            status_code=400,
            detail="None of the selected nodes are in this flow.",
        )

    child_graph = normalize_graph(child_graph)
    child_warnings = _assert_graph_valid(child_graph)
    # The child is a NEW flow of the caller's: an id the parent held is not
    # one they may keep in a flow of their own unless they can see it.
    child_issues = await check_integration_references(
        db, user, graph=child_graph, previous_graph=None, flow_owner_id=None
    )
    child_issues += await check_source_references(
        db, user, graph=child_graph, previous_graph=None, flow_owner_id=None
    )
    if child_issues:
        raise HTTPException(
            status_code=422,
            detail={
                "message": "The detached flow cannot be saved as it stands.",
                "issues": child_issues,
            },
        )

    child = Flow(
        user_id=user.id,
        name=(
            body.name
            or (f"{flow.name} — sous-flow" if collapse else f"{flow.name} — detached")
        )[:200],
        description=(
            f"Collapsed from '{flow.name}'."
            if collapse
            else f"Detached from '{flow.name}'."
        ),
        status=FLOW_STATUS_DRAFT,
        graph=child_graph,
        version_number=1,
        tags=list(flow.tags or []),
        is_company_managed=False,
    )
    db.add(child)
    await db.flush()
    await create_flow_version(
        db,
        flow_id=child.id,
        graph=child_graph,
        author_id=user.id,
        commit_message=f"Detached from '{flow.name}'",
    )

    if collapse:
        # The service could not know the child's id before the flush — the
        # sentinel in the parent's flow.call node points here.
        for node in parent_graph.get("nodes") or []:
            if (node.get("config") or {}).get("flow_id") == CHILD_FLOW_SENTINEL:
                node["config"]["flow_id"] = str(child.id)
    parent_graph = normalize_graph(parent_graph)
    parent_warnings = _assert_graph_valid(parent_graph)

    flow.graph = parent_graph
    parent_version = await create_flow_version(
        db,
        flow_id=flow.id,
        graph=parent_graph,
        author_id=user.id,
        commit_message=f"Detached {report['nodes_detached']} nodes into '{child.name}'",
    )
    flow.version_number = parent_version.version_number

    await log_audit(
        db,
        user=user,
        action="update",
        entity_type="flow",
        entity_id=flow.id,
        entity_name=flow.name,
        summary=f"Detached {report['nodes_detached']} nodes from '{flow.name}' into '{child.name}'",
        details={**report, "child_flow_id": str(child.id)},
        request=request,
    )
    await db.commit()
    await db.refresh(flow)
    await db.refresh(child)

    return FlowDetachResponse(
        flow=_to_response(
            child,
            can_manage=can_manage(user, child),
            owner_name=user.full_name,
            issues=child_warnings,
        ),
        parent=_to_response(
            flow,
            can_manage=can_manage(user, flow, user_group_ids),
            issues=parent_warnings,
            owner_paused=await _owner_paused(db, flow, user),
        ),
        report=FlowDetachReportResponse(**report),
    )


@router.get("/{flow_id}/versions", response_model=FlowVersionListResponse)
async def list_versions(
    flow_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Version history, newest first. Graphs are omitted — see the detail route."""
    flow = await _get_visible_flow(db, user, flow_id)
    versions = await list_flow_versions(db, flow.id)
    authors = await resolve_author_names(db, versions)
    items = [
        _to_version_item(
            v,
            author_name=authors.get(str(v.author_id)),
            is_active=v.version_number == flow.version_number,
        )
        for v in versions
    ]
    return FlowVersionListResponse(
        items=items, total=len(items), max_entries=FLOW_VERSION_RETENTION
    )


@router.get(
    "/{flow_id}/versions/{version_number}", response_model=FlowVersionDetailResponse
)
async def get_version(
    flow_id: str,
    version_number: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """One version, with its full graph — used to preview before restoring."""
    flow = await _get_visible_flow(db, user, flow_id)
    version = await get_flow_version(db, flow.id, version_number)
    if version is None:
        raise HTTPException(status_code=404, detail="Version not found")
    authors = await resolve_author_names(db, [version])
    item = _to_version_item(
        version,
        author_name=authors.get(str(version.author_id)),
        is_active=version.version_number == flow.version_number,
    )
    return FlowVersionDetailResponse(
        **item.model_dump(), graph=version.graph or empty_graph()
    )


@router.post(
    "/{flow_id}/versions/{version_number}/restore", response_model=FlowResponse
)
async def restore_version(
    flow_id: str,
    version_number: int,
    body: FlowRestoreRequest,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Restore a previous version.

    The restore itself creates a NEW version holding the restored graph, so the
    state that was current beforehand is still in the history and the operation
    is undoable.
    """
    user_group_ids = await get_user_group_ids(db, user.id)
    flow = await get_manageable_entity(
        db,
        Flow,
        user,
        flow_id,
        action="edit",
        entity_label="Flow",
        user_group_ids=user_group_ids,
    )
    # Restoring a graph IS writing it, and a version an admin authored may hold
    # a Budget node above the platform ceiling — a non-admin must not bring
    # that value back into force. Lowered in place rather than refused: the
    # restore is usually about some other part of the flow, and the new version
    # this mints records exactly what was brought back.
    _target = await get_flow_version(db, flow.id, version_number)
    if _target is None:
        raise HTTPException(status_code=404, detail="Version not found")
    # Restored on a COPY: a FlowVersion is an immutable record, so lowering the
    # stored graph in place would rewrite history instead of restoring it.
    _restored = deepcopy(_target.graph or {})
    _lower_budget_nodes_to_ceiling(_restored, user)
    # What the version brings back that the flow no longer holds is added by
    # this write, and held to the rule a save follows.
    restore_issues = await check_integration_references(
        db, user, graph=_restored, previous_graph=flow.graph, flow_owner_id=flow.user_id
    )
    restore_issues += await check_source_references(
        db, user, graph=_restored, previous_graph=flow.graph, flow_owner_id=flow.user_id
    )
    restore_issues += [
        issue
        for issue in await check_tool_references(
            db,
            user,
            graph=_restored,
            user_group_ids=user_group_ids,
            previous_graph=flow.graph,
            flow_owner_id=flow.user_id,
            is_company_managed=flow.is_company_managed,
        )
        if issue["severity"] == "error"
    ]
    if restore_issues:
        raise HTTPException(
            status_code=422,
            detail={
                "message": "This version cannot be restored as it stands.",
                "issues": restore_issues,
            },
        )

    version = await restore_flow_version(
        db,
        flow=flow,
        version_number=version_number,
        author_id=user.id,
        commit_message=body.commit_message,
        graph=_restored,
    )
    if version is None:
        raise HTTPException(status_code=404, detail="Version not found")

    await log_audit(
        db,
        user=user,
        action="update",
        entity_type="flow",
        entity_id=flow.id,
        entity_name=flow.name,
        summary=f"Restored flow '{flow.name}' to version {version_number}",
        details={
            "restored_version": version_number,
            "new_version": version.version_number,
        },
        request=request,
    )
    await db.commit()
    await db.refresh(flow)
    result = validate_graph(flow.graph or empty_graph())
    return _to_response(
        flow,
        can_manage=can_manage(user, flow, user_group_ids),
        issues=[i.to_dict() for i in result.issues],
        owner_paused=await _owner_paused(db, flow, user),
    )


# ---------------------------------------------------------------------------
# Import / export
# ---------------------------------------------------------------------------


@router.get("/{flow_id}/export")
async def export_flow_json(
    flow_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Download a flow as native JSON. Re-importable through `/flows/import`."""
    flow = await _get_visible_flow(db, user, flow_id)
    return export_flow(flow)


@router.post(
    "/import", response_model=FlowImportResponse, status_code=status.HTTP_201_CREATED
)
async def import_flow(
    body: FlowImportRequest,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _agentic_gate: None = Depends(require_agentic_ai_licensed),
):
    """Create a flow from a native XTM One, LangFlow or n8n export.

    Foreign nodes with no local equivalent are imported as ``unresolved``
    nodes rather than dropped, and n8n nodes that map but could not be carried
    over faithfully carry review notes that keep them from running; the
    response's ``report`` counts both, so a partial import is never silent.
    """
    try:
        imported = import_flow_document(body.document)
    except FlowImportError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    graph = normalize_graph(imported["graph"])
    _lower_budget_nodes_to_ceiling(graph, user)
    await _assert_writable(db, user, graph)

    flow = Flow(
        user_id=user.id,
        name=(body.name or imported["name"])[:200],
        description=imported["description"],
        status=FLOW_STATUS_DRAFT,
        graph=graph,
        version_number=1,
        tags=[],
        is_company_managed=False,
    )
    db.add(flow)
    await db.flush()
    await create_flow_version(
        db,
        flow_id=flow.id,
        graph=graph,
        author_id=user.id,
        commit_message=f"Imported from {imported['report']['format']}",
    )
    await log_audit(
        db,
        user=user,
        action="create",
        entity_type="flow",
        entity_id=flow.id,
        entity_name=flow.name,
        summary=f"Imported flow '{flow.name}'",
        details=imported["report"],
        request=request,
    )
    await db.commit()
    await db.refresh(flow)

    result = validate_graph(graph)
    return FlowImportResponse(
        flow=_to_response(
            flow,
            can_manage=can_manage(user, flow),
            owner_name=user.full_name,
            issues=[i.to_dict() for i in result.issues],
        ),
        report=FlowImportReportResponse(**imported["report"]),
    )
