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

"""Flow-builder tools — an agent authors flows end to end.

The visual builder's whole REST surface existed, but no agent could reach
it: ``agent_builder`` covers agents/assignments/KBs/tools and knows nothing
about flows. This module is the missing seam — a builtin tool set (id
``flow_builder``) wrapping the SAME service layer the REST handlers use
(``validate_graph``, ``normalize_graph``, ``create_flow_version``,
``create_flow_run``), so an assistant can turn a user's prompt into a
running flow: discover the node catalog, compose the graph JSON, validate
it dry, save it (auto-layout fills missing positions — an LLM has no
mouse), run it, and read the step journal to self-correct.

Operations return strings (markdown / compact JSON) like every builtin
tool.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.concierge.sandbox.guards import guard_flow_builder, in_concierge_run
from app.tools.base import CallableToolExecutor, ToolDefinition

logger = logging.getLogger(__name__)

_GRAPH_PRIMER = (
    "Graph JSON: {\"nodes\": [{\"id\", \"kind\", \"config\", \"position\"?}], "
    "\"edges\": [{\"id\", \"source\", \"source_handle\", \"target\", "
    "\"target_handle\", \"kind\", \"label\"?}], \"viewport\"?}. Node ids: "
    "letters/digits/-/_ only (no dots). Reference upstream outputs as "
    "{{node_id.output_variable}} (see output_variables in the catalog); "
    "trigger.manual declared parameters read as "
    "{{trigger_id.payload.param_name}}. Edge handles default to out/in; "
    "handover edges may carry a label (consult/delegate/transfer/"
    "mandatory_transfer) to route by handover type. A key-value field "
    "(action.http_request headers) is a JSON object {name: value}; a field "
    "bound to a catalog source (relay_group_id, agent_id, ...) takes one of "
    "that source's values. Omit position — the server auto-layouts nodes "
    "that lack one."
)


def _parse_graph(graph: Any) -> dict | None:
    if isinstance(graph, dict):
        return graph
    if isinstance(graph, str):
        try:
            parsed = json.loads(graph)
        except ValueError:
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


def _issues_text(issues: list) -> str:
    lines = []
    for issue in issues:
        where = issue.node_id or issue.edge_id or "graph"
        lines.append(f"- [{issue.severity}] {issue.code} @ {where}: {issue.message}")
    return "\n".join(lines) or "(none)"


def _reference_issues_text(issues: list[dict]) -> str:
    return "\n".join(f"- [error] {i['code']} @ {i['node_id']}: {i['message']}" for i in issues)


def _event_type_refusal(exc: ValidationError) -> str:
    """One actionable sentence for the first rule a declaration breaks."""
    from app.api.v1.schemas.flow import EVENT_TYPE_NAME_MAX_LENGTH

    error = exc.errors()[0]
    loc = error["loc"]
    if loc[:1] == ("name",):
        if error["type"] == "string_too_long":
            return (
                "The event type name is too long - keep it to "
                f"{EVENT_TYPE_NAME_MAX_LENGTH} characters or fewer."
            )
        return "Give the event type a name."
    if len(loc) == 3 and loc[0] == "fields" and loc[2] in ("name", "type"):
        return (
            f"Field {loc[2]} `{str(error['input'])[:80]}` is refused: "
            f"{error['msg']}. Field names are identifiers (letters, digits and "
            "underscores, not starting with a digit); types are string, "
            "number, boolean or json."
        )
    where = ".".join(str(part) for part in loc)
    return f"The event type cannot be declared - {where}: {error['msg']}."


def build_flow_builder_tool(
    db: AsyncSession,
    user_id: str,
) -> tuple[list[ToolDefinition], CallableToolExecutor]:
    """Build the flow-builder tool set for *user_id*.

    Thin wrappers over the flow service layer with the same permission
    gates as the REST API: visibility is own + group-shared +
    company-managed, writes need ``helpers.can_manage()`` (the creator, an
    admin or a ``config`` grantee; admins only for a company-managed flow).
    """
    executor = CallableToolExecutor()
    caller_id = uuid.UUID(str(user_id))

    async def _caller():
        from app.models.user import User

        return (
            await db.execute(select(User).where(User.id == caller_id))
        ).scalar_one()

    async def _integration_refusals(
        graph: Any,
        previous_graph: Any,
        flow_owner_id: Any = None,
        is_company_managed: bool = False,
    ) -> str:
        """The REST routes' integration, reference and tool-name checks and
        the strict-mode reference check, as the tool's refusal text ("" when
        none)."""
        from app.api.v1.helpers import agent_tool_admin_bypass
        from app.services.flow_references import (
            check_integration_references,
            check_source_references,
            check_three_tier_references,
            check_tool_references,
        )

        user = await _caller()
        issues = await check_integration_references(
            db,
            user,
            graph=graph,
            previous_graph=previous_graph,
            flow_owner_id=flow_owner_id,
        )
        issues += await check_source_references(
            db,
            user,
            graph=graph,
            previous_graph=previous_graph,
            flow_owner_id=flow_owner_id,
        )
        issues += await check_three_tier_references(
            db, user, graph=graph, previous_graph=previous_graph
        )
        issues += [
            issue
            for issue in await check_tool_references(
                db,
                user,
                graph=graph,
                previous_graph=previous_graph,
                flow_owner_id=flow_owner_id,
                is_company_managed=is_company_managed,
                admin_bypass=agent_tool_admin_bypass(user),
            )
            if issue["severity"] == "error"
        ]
        if not issues:
            return ""
        return "The graph cannot be saved as it stands:\n" + _reference_issues_text(issues)

    async def _visible_flow(flow_id: str):
        from app.api.v1.helpers import agent_tool_admin_bypass, get_user_group_ids, user_visible_query
        from app.models.flow import Flow

        user = await _caller()
        group_ids = await get_user_group_ids(db, user.id)
        try:
            fid = uuid.UUID(str(flow_id))
        except ValueError:
            return None
        flow = (
            await db.execute(
                user_visible_query(
                    Flow, user.id, group_ids, is_admin=agent_tool_admin_bypass(user)
                ).where(Flow.id == fid)
            )
        ).scalar_one_or_none()
        if flow is None and in_concierge_run():
            # A Concierge run works on its sandbox copies, which no listing shows (#8255).
            flow = (
                await db.execute(select(Flow).where(Flow.id == fid, Flow.is_sandbox.is_(True)))
            ).scalar_one_or_none()
        return flow

    async def _manage_refusal(flow: Any, user: Any, verb: str) -> str | None:
        """Why *user* may not change *flow*, by the rule the REST routes apply
        (``get_manageable_entity`` -> ``can_manage``); None when they may."""
        from app.api.v1.helpers import can_manage, get_user_group_ids

        if flow.is_sandbox and in_concierge_run():
            return None
        if can_manage(user, flow, await get_user_group_ids(db, user.id)):
            return None
        if flow.is_company_managed:
            return f"Only admins can {verb} company-managed flows."
        return (
            f"Only the flow's owner, an admin or a config-tier collaborator "
            f"can {verb} this flow."
        )

    async def _audit(
        user: Any,
        action: str,
        flow: Any,
        summary: str,
        details: dict | None = None,
    ) -> None:
        """The entry the REST route writes for the same act (flushed, never
        committed here: the caller's commit carries it)."""
        from app.services.audit import log_audit

        await log_audit(
            db,
            user=user,
            action=action,
            entity_type="flow",
            entity_id=flow.id,
            entity_name=flow.name,
            summary=summary,
            details={**(details or {}), "via": "flow_builder"},
        )

    # ── Discover ──────────────────────────────────────────────────────

    async def _get_node_catalog(detail: str = "summary") -> str:
        from app.services.flow_access import available_node_kinds, catalog_sources
        from app.services.flow_catalog import EDGE_KINDS, SOURCE_RELAY_GROUPS
        from app.services.flow_graph import MAX_EDGES, MAX_NODES

        # The same filter as the REST catalog: one helper, never two copies.
        kinds = available_node_kinds()

        def _trim(nk: dict) -> dict:
            out = {
                "kind": nk["kind"],
                "category": nk["category"],
                "label": nk["label"],
                "description": nk["description"],
                "inputs": [
                    {"id": p["id"], "types": p["types"], "multiple": p["multiple"]}
                    for p in nk.get("inputs") or []
                ],
                "outputs": [
                    {"id": p["id"], "types": p["types"], "multiple": p["multiple"]}
                    for p in nk.get("outputs") or []
                ],
                "config_fields": [
                    {
                        k: v
                        for k, v in {
                            "name": f["name"],
                            "type": f["type"],
                            "required": f["required"],
                            "options": [o.get("value") for o in f.get("options") or []]
                            or None,
                            "source": f.get("source"),
                            "help": (f.get("help") or "")[:200] or None,
                        }.items()
                        if v not in (None, False, [])
                    }
                    for f in nk.get("config_fields") or []
                ],
                "output_variables": [
                    {
                        "name": v["name"],
                        "description": v["description"],
                        # `text` has no fields: `{{n.output.x}}` renders empty.
                        **({"type": v["type"]} if v.get("type", "any") != "any" else {}),
                    }
                    for v in nk.get("output_variables") or []
                ],
            }
            if nk.get("is_annotation"):
                out["is_annotation"] = True
            return out

        from app.api.v1.helpers import agent_tool_admin_bypass

        user = await _caller()
        try:
            sources = await catalog_sources(db, user, is_admin=agent_tool_admin_bypass(user))
        except Exception:
            # take the whole catalog down; the node kinds alone are enough
            # to author a graph.
            await db.rollback()
            sources = {}
        wanted_sources = {
            key: [
                {"value": o["value"], "label": o["label"]}
                for o in sources.get(key) or []
            ]
            for key in (
                "agents",
                "flows",
                "event_types",
                "custom_tools",
                "integrations",
                "assignments",
                "knowledge_bases",
                SOURCE_RELAY_GROUPS,
            )
        }
        payload = {
            "node_kinds": [
                nk if detail == "full" else _trim(nk) for nk in kinds
            ],
            "edge_kinds": list(EDGE_KINDS),
            "limits": {"max_nodes": MAX_NODES, "max_edges": MAX_EDGES},
            "sources": wanted_sources,
            "authoring": _GRAPH_PRIMER,
        }
        return json.dumps(payload, ensure_ascii=False)

    async def _list_flows(search: str = "") -> str:
        from app.api.v1.helpers import agent_tool_admin_bypass, get_user_group_ids, user_visible_query
        from app.models.flow import Flow

        user = await _caller()
        group_ids = await get_user_group_ids(db, user.id)
        query = user_visible_query(
            Flow, user.id, group_ids, is_admin=agent_tool_admin_bypass(user)
        ).order_by(Flow.updated_at.desc()).limit(50)
        rows = (await db.execute(query)).scalars().all()
        needle = (search or "").strip().lower()
        rows = [
            f
            for f in rows
            if not needle or needle in (f.name or "").lower()
        ]
        if not rows:
            return "No flows visible to you." + (
                f" (search: {search})" if search else ""
            )
        return "\n".join(
            f"- {f.id} · **{f.name}** · {f.status} · v{f.version_number}"
            for f in rows
        )

    async def _get_flow(flow_id: str) -> str:
        flow = await _visible_flow(flow_id)
        if flow is None:
            return f"Flow `{flow_id}` not found."
        return json.dumps(
            {
                "id": str(flow.id),
                "name": flow.name,
                "description": flow.description,
                "status": flow.status,
                "version": flow.version_number,
                "graph": flow.graph or {},
            },
            ensure_ascii=False,
        )

    async def _get_flow_examples(name: str = "") -> str:
        from sqlalchemy import cast
        from sqlalchemy.dialects.postgresql import JSONB

        from app.core.json_sql import when_json_parseable
        from app.models.flow import Flow

        # ``tags`` is a ``json`` column, which has no equality operator on
        # PostgreSQL: containment through ``jsonb``. A row whose stored tags
        # ``jsonb`` refuses would abort the whole lookup; it is returned and
        # decided on its decoded tags instead, as ``@>`` decides the others.
        query = (
            select(Flow)
            .where(
                Flow.is_company_managed.is_(True),
                when_json_parseable(
                    Flow.tags,
                    cast(Flow.tags, JSONB).contains(["example"]),
                    otherwise=True,
                ),
            )
            .order_by(Flow.name)
        )
        rows = [
            f
            for f in (await db.execute(query)).scalars().all()
            if isinstance(f.tags, list) and "example" in f.tags
        ]
        if not rows:
            return "No example flows are seeded on this platform."
        needle = (name or "").strip().lower()
        if needle:
            match = next(
                (f for f in rows if needle in (f.name or "").lower()), None
            )
            if match is None:
                return "No example matches. Available:\n" + "\n".join(
                    f"- {f.name}" for f in rows
                )
            return json.dumps(
                {"name": match.name, "graph": match.graph or {}},
                ensure_ascii=False,
            )
        return (
            "Seeded examples (pass `name` to fetch one graph as a template):\n"
            + "\n".join(f"- {f.name}" for f in rows)
        )

    # ── Author ─────────────────────────────────────────────────────────

    async def _validate_flow_graph(graph: Any) -> str:
        from app.services.flow_graph import auto_layout, validate_graph

        parsed = _parse_graph(graph)
        if parsed is None:
            return "The graph must be a JSON object. " + _GRAPH_PRIMER
        result = validate_graph(auto_layout(parsed))
        verdict = "VALID" if result.is_valid else "INVALID (errors block saving)"
        return f"{verdict}\n{_issues_text(result.issues)}"

    async def _create_flow(
        name: str, graph: Any, description: str = ""
    ) -> str:
        from app.models.flow import Flow
        from app.services.flow_graph import (
            auto_layout,
            normalize_graph,
            validate_graph,
        )
        from app.services.flow_versioning import create_flow_version

        parsed = _parse_graph(graph)
        if parsed is None:
            return "The graph must be a JSON object. " + _GRAPH_PRIMER
        if not (name or "").strip():
            return "Give the flow a name."
        laid_out = auto_layout(parsed)
        result = validate_graph(laid_out)
        if not result.is_valid:
            return (
                "The graph cannot be saved as it stands:\n"
                + _issues_text(result.errors)
            )
        normalized = normalize_graph(laid_out)
        refused = await _integration_refusals(normalized, None)
        if refused:
            return refused
        if in_concierge_run():
            # A Concierge prototype lands in the sandbox (#8255): a production
            # flow comes only from an approved proposal.
            from app.services.concierge.sandbox.copies import SandboxOverrides, create_sandbox_copy

            copy = await create_sandbox_copy(
                db,
                source_type="flow",
                source_id=None,
                created_for="flow_builder",
                overrides=SandboxOverrides(graph=normalized),
            )
            flow = await db.get(Flow, copy.copy_flow_id)
            flow.name = name.strip()[:200]
            flow.description = (description or "").strip()[:2000]
            await db.commit()
            return (
                f"Sandbox flow **{flow.name}** created — id `{flow.id}`, draft v1.\n"
                f"Warnings:\n{_issues_text(result.warnings)}"
            )
        flow = Flow(
            id=uuid.uuid4(),
            user_id=caller_id,
            name=name.strip()[:200],
            description=(description or "").strip()[:2000],
            status="draft",
            graph=normalized,
            version_number=1,
        )
        db.add(flow)
        await db.flush()
        await create_flow_version(
            db,
            flow_id=flow.id,
            graph=normalized,
            author_id=caller_id,
            commit_message="Created by the flow-builder tool",
        )
        await _audit(await _caller(), "create", flow, f"Created flow '{flow.name}'")
        await db.commit()
        warnings = _issues_text(result.warnings)
        return (
            f"Flow **{flow.name}** created — id `{flow.id}`, draft v1.\n"
            f"Warnings:\n{warnings}"
        )

    async def _update_flow_graph(flow_id: str, graph: Any) -> str:
        from app.services.flow_graph import (
            auto_layout,
            normalize_graph,
            validate_graph,
        )
        from app.services.flow_versioning import create_flow_version

        flow = await _visible_flow(flow_id)
        if flow is None:
            return f"Flow `{flow_id}` not found."
        user = await _caller()
        refusal = await _manage_refusal(flow, user, "modify")
        if refusal:
            return refusal
        parsed = _parse_graph(graph)
        if parsed is None:
            return "The graph must be a JSON object. " + _GRAPH_PRIMER
        laid_out = auto_layout(parsed)
        result = validate_graph(laid_out)
        if not result.is_valid:
            return (
                "The graph cannot be saved as it stands:\n"
                + _issues_text(result.errors)
            )
        normalized = normalize_graph(laid_out)
        refused = await _integration_refusals(
            normalized, flow.graph, flow.user_id, flow.is_company_managed
        )
        if refused:
            return refused
        flow.graph = normalized
        flow.version_number = (flow.version_number or 1) + 1
        await create_flow_version(
            db,
            flow_id=flow.id,
            graph=flow.graph,
            author_id=caller_id,
            commit_message="Updated by the flow-builder tool",
        )
        await _audit(user, "update", flow, f"Updated flow '{flow.name}'")
        await db.commit()
        return (
            f"Flow **{flow.name}** updated to v{flow.version_number}.\n"
            f"Warnings:\n{_issues_text(result.warnings)}"
        )

    async def _create_flow_event_type(
        name: str, fields: Any = None, description: str = ""
    ) -> str:
        from app.api.v1.schemas.flow import FlowEventTypeCreate
        from app.models.flow_event import FlowEventType

        parsed_fields: list[dict] = []
        raw = fields
        if isinstance(raw, str) and raw.strip():
            try:
                raw = json.loads(raw)
            except ValueError:
                return (
                    "`fields` must be JSON, e.g. "
                    '[{"name": "title", "type": "string", "required": true}].'
                )
        if isinstance(raw, list):
            for f in raw:
                if isinstance(f, dict) and (f.get("name") or "").strip():
                    parsed_fields.append(
                        {
                            "name": str(f["name"]).strip(),
                            "type": str(f.get("type") or "string"),
                            "required": bool(f.get("required")),
                            "description": str(f.get("description") or "")[:500],
                        }
                    )
        names = [f["name"] for f in parsed_fields]
        if len(names) != len(set(names)):
            return "Field names must be unique."
        try:
            declared = FlowEventTypeCreate(
                name=name,
                description=(description or "").strip()[:2000],
                fields=parsed_fields,
            )
        except ValidationError as exc:
            return _event_type_refusal(exc)
        event_type = FlowEventType(
            id=uuid.uuid4(),
            user_id=caller_id,
            name=declared.name,
            description=declared.description,
            fields=[f.model_dump() for f in declared.fields],
        )
        db.add(event_type)
        await db.commit()
        return f"Event type **{event_type.name}** declared — id `{event_type.id}`."

    async def _list_flow_event_types() -> str:
        from app.api.v1.helpers import agent_tool_admin_bypass, get_user_group_ids, user_visible_query
        from app.models.flow_event import FlowEventType

        user = await _caller()
        group_ids = await get_user_group_ids(db, user.id)
        rows = (
            (
                await db.execute(
                    user_visible_query(
                        FlowEventType, user.id, group_ids, is_admin=agent_tool_admin_bypass(user)
                    ).order_by(FlowEventType.name)
                )
            )
            .scalars()
            .all()
        )
        if not rows:
            return "No event types declared yet."
        return "\n".join(
            f"- {et.id} · **{et.name}** · fields: "
            + (
                ", ".join(
                    f"{f.get('name')}:{f.get('type')}" for f in et.fields or []
                )
                or "(none)"
            )
            for et in rows
        )

    # ── Run & observe ──────────────────────────────────────────────────

    async def _run_flow(flow_id: str, parameters: Any = None) -> str:
        from app.services import orchestration_engine as engine
        from app.services.flow_graph import validate_graph
        from app.services.flow_runner import (
            TriggerParametersError,
            create_flow_run,
            find_trigger_nodes,
            manual_run_trigger,
        )

        flow = await _visible_flow(flow_id)
        if flow is None:
            return f"Flow `{flow_id}` not found."
        from app.services.assignment_pause import (
            FLOW_OWNER_DEACTIVATED_MESSAGE,
            is_paused_by_deactivated_owner,
        )

        if await is_paused_by_deactivated_owner(db, flow):
            return f"Flow **{flow.name}** was not run. {FLOW_OWNER_DEACTIVATED_MESSAGE}"
        result = validate_graph(flow.graph or {})
        if not result.is_valid:
            return (
                "Fix the validation errors before running:\n"
                + _issues_text(result.errors)
            )
        if not find_trigger_nodes(flow.graph or {}):
            return "Add a trigger node — a run needs an entry point."
        from app.api.v1.helpers import agent_tool_admin_bypass
        from app.services.flow_references import (
            check_three_tier_references,
            check_tool_references,
        )

        caller = await _caller()
        outside = await check_three_tier_references(db, caller, graph=flow.graph or {})
        outside += [
            issue
            for issue in await check_tool_references(
                db,
                caller,
                graph=flow.graph or {},
                flow_owner_id=flow.user_id,
                is_company_managed=flow.is_company_managed,
                admin_bypass=agent_tool_admin_bypass(caller),
                check_co_editors=False,
            )
            if issue["severity"] == "error"
        ]
        if outside:
            return "This flow cannot be run by an agent:\n" + _reference_issues_text(outside)
        raw_params = parameters
        if isinstance(raw_params, str) and raw_params.strip():
            try:
                raw_params = json.loads(raw_params)
            except ValueError:
                return "`parameters` must be a JSON object."
        provided = raw_params if isinstance(raw_params, dict) else {}
        try:
            _entry, trigger_type, trigger_payload = manual_run_trigger(flow.graph or {}, provided)
        except TriggerParametersError as exc:
            return "Run parameters do not match the trigger's declaration:\n- " + "\n- ".join(exc.errors)

        from app.services.assignment_pause import FlowOwnerDeactivatedError

        try:
            run = await create_flow_run(
                db,
                flow,
                trigger_type=trigger_type,
                trigger_payload=trigger_payload,
                trigger_context={"source": "flow_builder_tool"},
                user_id=caller_id,
            )
        except FlowOwnerDeactivatedError:
            # Deactivated since the check above.
            return f"Flow **{flow.name}** was not run. {FLOW_OWNER_DEACTIVATED_MESSAGE}"
        await _audit(
            caller,
            "flow.run",
            flow,
            f"Manually ran flow '{flow.name}'",
            {"run_id": str(run.id)},
        )
        await db.commit()
        await engine.notify_orchestration_wake()
        return (
            f"Run `{run.id}` started on **{flow.name}**. "
            "Poll get_flow_run for the step journal."
        )

    async def _get_flow_run(run_id: str) -> str:
        from fastapi import HTTPException

        from app.api.v1.helpers import agent_tool_admin_bypass
        from app.models.flow_run import FlowRunStep
        from app.services.flow_access import get_visible_run

        try:
            rid = uuid.UUID(str(run_id))
        except ValueError:
            return f"Invalid run id: {run_id}"
        user = await _caller()
        try:
            run = await get_visible_run(db, user, str(rid), is_admin=agent_tool_admin_bypass(user))
        except HTTPException:
            return f"Run `{run_id}` not found."
        steps = (
            (
                await db.execute(
                    select(FlowRunStep)
                    .where(FlowRunStep.flow_run_id == rid)
                    .order_by(FlowRunStep.seq)
                )
            )
            .scalars()
            .all()
        )
        lines = [f"Run `{run.id}` — status: **{run.status}**"]
        if run.error:
            lines.append(f"Run error: {str(run.error)[:500]}")
        for step in steps:
            line = f"- {step.node_id} ({step.node_kind}): {step.status}"
            if step.error:
                line += f" — error: {str(step.error)[:300]}"
            elif getattr(step, "warning", None):
                line += f" — warning: {str(step.warning)[:300]}"
            elif isinstance(step.output, dict):
                preview = {
                    k: (str(v)[:120] if isinstance(v, str) else v)
                    for k, v in step.output.items()
                    if not k.startswith("_")
                }
                line += f" — output: {json.dumps(preview, ensure_ascii=False)[:400]}"
            lines.append(line)
        return "\n".join(lines)

    async def _set_flow_status(flow_id: str, status: str) -> str:
        flow = await _visible_flow(flow_id)
        if flow is None:
            return f"Flow `{flow_id}` not found."
        user = await _caller()
        refusal = await _manage_refusal(flow, user, "change")
        if refusal:
            return refusal
        notes = ""
        if status == "active":
            from app.services.flow_graph import validate_graph
            from app.services.flow_runner import find_trigger_nodes

            result = validate_graph(flow.graph or {})
            if not result.is_valid:
                return (
                    "Fix the validation errors before activating:\n"
                    + _issues_text(result.errors)
                )
            triggers = find_trigger_nodes(flow.graph or {})
            if not triggers:
                return "Add a trigger node — an active flow needs a way to start."
            if find_trigger_nodes(flow.graph or {}, "trigger.webhook"):
                # The REST activation mints the delivery secret and returns it
                # once; a tool result would put it in a model's context.
                return (
                    "This flow has a webhook trigger: activate it from the "
                    "flow's page, which shows the delivery secret once."
                )
            if flow.user_id is None and any(
                (n.get("kind") or "") not in ("trigger.manual", "trigger.chat")
                for n in triggers
            ):
                notes = (
                    " This company-managed flow has no owner: its automatic "
                    "triggers will not fire until an owner is assigned. "
                    "Manual runs work."
                )
        flow.status = status
        await _audit(
            user,
            "flow.activate" if status == "active" else "flow.deactivate",
            flow,
            f"{'Activated' if status == 'active' else 'Deactivated'} flow '{flow.name}'",
        )
        await db.commit()
        return f"Flow **{flow.name}** is now {status}.{notes}"

    async def _activate_flow(flow_id: str) -> str:
        return await _set_flow_status(flow_id, "active")

    async def _deactivate_flow(flow_id: str) -> str:
        return await _set_flow_status(flow_id, "draft")

    # ── Registration ───────────────────────────────────────────────────

    executor.register("get_node_catalog", _get_node_catalog)
    executor.register("list_flows", _list_flows)
    executor.register("get_flow", _get_flow)
    executor.register("get_flow_examples", _get_flow_examples)
    executor.register("validate_flow_graph", _validate_flow_graph)
    executor.register("create_flow", _create_flow)
    executor.register("update_flow_graph", _update_flow_graph)
    executor.register("create_flow_event_type", _create_flow_event_type)
    executor.register("list_flow_event_types", _list_flow_event_types)
    executor.register("run_flow", _run_flow)
    executor.register("get_flow_run", _get_flow_run)
    executor.register("activate_flow", _activate_flow)
    executor.register("deactivate_flow", _deactivate_flow)

    graph_schema = {
        "type": "object",
        "properties": {
            "graph": {
                "type": "string",
                "description": (
                    "The canvas document as JSON ({nodes, edges}); its authoring rules are the "
                    "'authoring' field of get_node_catalog, and a malformed graph is refused with them."
                ),
            },
        },
        "required": ["graph"],
    }
    definitions = [
        ToolDefinition(
            name="get_node_catalog",
            description=(
                "The flow node library: every node kind with its ports, "
                "config fields, published output variables ({{refs}}), plus "
                "the caller's live agents / flows / event types / tools to "
                "bind. ALWAYS call this before composing a graph. "
                "detail='full' adds icons and labels (rarely needed)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "detail": {
                        "type": "string",
                        "description": "'summary' (default) or 'full'.",
                        "default": "summary",
                    }
                },
                "required": [],
            },
            source="builtin:platform",
        ),
        ToolDefinition(
            name="list_flows",
            description="List the flows visible to the user (id, name, status).",
            input_schema={
                "type": "object",
                "properties": {
                    "search": {"type": "string", "description": "Name filter."}
                },
                "required": [],
            },
            source="builtin:platform",
        ),
        ToolDefinition(
            name="get_flow",
            description="Fetch one flow with its full graph JSON.",
            input_schema={
                "type": "object",
                "properties": {"flow_id": {"type": "string"}},
                "required": ["flow_id"],
            },
            source="builtin:platform",
        ),
        ToolDefinition(
            name="get_flow_examples",
            description=(
                "Seeded educational example flows. Without `name`: the list. "
                "With `name`: that example's full graph — a few-shot template "
                "for composing your own."
            ),
            input_schema={
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": [],
            },
            source="builtin:platform",
        ),
        ToolDefinition(
            name="validate_flow_graph",
            description=(
                "Dry-run validation of a graph — nothing is saved. Errors "
                "block saving; warnings ride along. Fix errors, then "
                "create_flow / update_flow_graph."
            ),
            input_schema=graph_schema,
            source="builtin:platform",
        ),
        ToolDefinition(
            name="create_flow",
            description=(
                "Create a flow from a graph. Nodes without position are "
                "auto-laid-out. Validation errors refuse the save and are "
                "returned for you to fix."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                    "graph": graph_schema["properties"]["graph"],
                },
                "required": ["name", "graph"],
            },
            source="builtin:platform",
        ),
        ToolDefinition(
            name="update_flow_graph",
            description=(
                "Replace a flow's graph (bumps its version). Same "
                "validation/auto-layout as create_flow."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "flow_id": {"type": "string"},
                    "graph": graph_schema["properties"]["graph"],
                },
                "required": ["flow_id", "graph"],
            },
            source="builtin:platform",
        ),
        ToolDefinition(
            name="create_flow_event_type",
            description=(
                "Declare a typed event signature for Emit / On event / Wait "
                "nodes. fields: JSON list of "
                '{"name", "type": string|number|boolean|json, "required"}.'
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                    "fields": {"type": "string", "description": "JSON list."},
                },
                "required": ["name"],
            },
            source="builtin:platform",
        ),
        ToolDefinition(
            name="list_flow_event_types",
            description="List declared event types (id, name, fields).",
            input_schema={"type": "object", "properties": {}, "required": []},
            source="builtin:platform",
        ),
        ToolDefinition(
            name="run_flow",
            description=(
                "Run a flow now (drafts too — this is the test button). "
                "parameters: JSON object matching the manual trigger's "
                "declared parameters."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "flow_id": {"type": "string"},
                    "parameters": {
                        "type": "string",
                        "description": "JSON object of trigger parameters.",
                    },
                },
                "required": ["flow_id"],
            },
            source="builtin:platform",
        ),
        ToolDefinition(
            name="get_flow_run",
            description=(
                "A run's status and step journal (per-node status, outputs, "
                "errors) — read it to verify the flow and self-correct."
            ),
            input_schema={
                "type": "object",
                "properties": {"run_id": {"type": "string"}},
                "required": ["run_id"],
            },
            source="builtin:platform",
        ),
        ToolDefinition(
            name="activate_flow",
            description="Activate a flow (its triggers go live).",
            input_schema={
                "type": "object",
                "properties": {"flow_id": {"type": "string"}},
                "required": ["flow_id"],
            },
            source="builtin:platform",
        ),
        ToolDefinition(
            name="deactivate_flow",
            description="Deactivate a flow back to draft.",
            input_schema={
                "type": "object",
                "properties": {"flow_id": {"type": "string"}},
                "required": ["flow_id"],
            },
            source="builtin:platform",
        ),
    ]
    guard_flow_builder(db, executor)
    return definitions, executor
