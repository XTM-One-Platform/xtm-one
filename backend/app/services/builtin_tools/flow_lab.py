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

"""Flow Lab tools - Platform Health diagnoses flows and measures them in shadow (#8260).

Built-in group ``flow_lab`` (PRD ``platform-concierge.md`` sections 6.9 and 8):

* reads, for the Flow Lab, the Experimenter and the platform MCP server:
  ``get_flow_diagnosis`` (repair, optimize), ``get_assignment_path`` (agent to
  flow), ``find_flow_candidates`` (new flow), ``get_flow_shadow``,
  ``get_flow_run_comparison`` and ``get_flow_node_catalog``;
* writes, for Platform Health agents only and on sandbox copies only:
  ``create_sandbox_flow`` / ``update_sandbox_flow`` (level 2's copy service,
  with the flow builder's validation) and ``run_flow_shadow`` (a sandbox flow
  measured on an assignment's live triggers, every write intercepted).

``flow_builder`` acts as a user row, so a scheduled (ownerless) Platform Health
cycle cannot use it; these writes are its authoring path, and when an admin
chats with the Flow Lab ``flow_builder`` lands on sandbox copies too
(level 2). Nothing here writes a production flow: the only path to
production is an approved proposal (level 3, ``services/health/flow_proposals``).

Each call opens its own session and resolves its caller (Platform Health
principal, else the user), and every target goes through
``services/health/flow_lab_access``: one rule on every path, by id and by
name. Values read from runs come back inside untrusted-content blocks, never
as instructions. Errors are generic, reasons in the logs. Built in with
Platform Health, not gated by ``FLOW_BUILDER``, which hides the builder's UI only
(``dev-docs/flow-builder-feature-flag.md``).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

# Registers the flow experiment kinds and the flow finding rule with level 6
# when the tool registry loads, so the Experimenter can measure flow variants.
import app.services.health.flow_experiments  # noqa: F401
from app.core import database
from app.core.utils import format_exception_message
from app.tools.base import CallableToolExecutor, ToolDefinition

logger = logging.getLogger(__name__)

#: Its functions belong to Platform Health's one internal group (#1943).
FLOW_LAB_GROUP_ID = "health_specialists"
SOURCE = f"builtin:{FLOW_LAB_GROUP_ID}"

#: The functions the platform MCP server may serve to a person.
FLOW_LAB_READ_TOOLS = frozenset(
    {
        "get_flow_diagnosis",
        "get_assignment_path",
        "find_flow_candidates",
        "get_flow_shadow",
        "get_flow_run_comparison",
        "get_flow_node_catalog",
    }
)

_OFF = "Error: Platform Health is not enabled on this platform."
_FAILED = "Error: the Flow Lab could not complete this request right now."
_NO_CALLER = "Error: this tool needs an authenticated caller."
_HEALTH_ONLY = "Error: only a Platform Health agent can do this, on sandbox copies."

#: How many replay cases ``get_flow_diagnosis`` lists with their payloads.
_LISTED_CASES = 20


def _enabled() -> bool:
    from app.services.health.availability import health_enabled

    return bool(health_enabled())


def _untrusted(text: str, source: str) -> str:
    from app.services.content_safety import wrap_untrusted

    return wrap_untrusted(text, source)


def _not_found(what: str) -> str:
    from app.services.health.flow_lab_access import NOT_FOUND

    return f"Error: {what} {NOT_FOUND}."


async def _run(
    user_id: str, name: str, fn: Callable[..., Awaitable[str]], args: dict[str, Any], *, agent_id: str | None = None
) -> str:
    from app.services.health.flow_lab_access import resolve_caller

    if not _enabled():
        return _OFF
    try:
        async with database.async_session() as db:
            caller = await resolve_caller(db, user_id or None, agent_id=agent_id)
            if caller is None:
                return _NO_CALLER
            return await fn(db, caller, **args)
    except TypeError as exc:
        logger.info("Flow Lab tool %s refused its arguments: %s", name, format_exception_message(exc))
        return "Error: unexpected arguments for this tool."
    except Exception as exc:
        logger.error("Flow Lab tool %s failed: %s", name, format_exception_message(exc), exc_info=True)
        return _FAILED


# ── Reads ──────────────────────────────────────────────────────────────────


async def _get_flow_diagnosis(db: AsyncSession, caller: Any, flow: str = "", last_runs: int = 50) -> str:
    from app.models.flow_run import FlowRun
    from app.services.health import flow_lab_access as access
    from app.services.health.flow_lab import PAYLOAD_EXCERPT_CHARS, diagnose_flow, excerpt

    target = await access.resolve_flow(db, caller, flow)
    if target is None:
        return _not_found("flow")

    async def allowed(run: Any) -> bool:
        return await access.flow_run_content_allowed(db, caller, target, run)

    diagnosis = await diagnose_flow(db, target, content_allowed=allowed, last_runs=int(last_runs or 50))
    lines = [
        f"Flow `{target.id}` v{diagnosis.version_number}: {diagnosis.runs} settled runs, "
        f"{diagnosis.failed} failed ({round((diagnosis.failure_rate or 0) * 100)} %).",
        "Nodes (executions / failures / p50 ms / p95 ms):",
    ]
    for node in diagnosis.nodes:
        lines.append(
            f"- {node.node_id} ({node.node_kind}): {node.executions} / {node.failures} / "
            f"{node.duration_p50_ms} / {node.duration_p95_ms}"
        )
        lines.extend("  " + _untrusted(error, f"step error of node {node.node_id}") for error in node.errors)
    lines.append(f"Replay cases: {len(diagnosis.cases)}, content withheld by consent on {diagnosis.withheld}.")
    # Every run whose content this answer carries (a payload, a step error) is
    # an audited read, listed or not.
    for case in diagnosis.cases:
        if case.trigger_payload is not None:
            await access.audit_flow_run_read(db, caller, target, await db.get(FlowRun, case.run_id))
    for case in diagnosis.cases[:_LISTED_CASES]:
        line = f"- run `{case.run_id}` {case.status} ({case.trigger_type})"
        if case.trigger_payload is not None:
            line += "\n  " + _untrusted(
                excerpt(case.trigger_payload, PAYLOAD_EXCERPT_CHARS), f"trigger payload of run {case.run_id}"
            )
        lines.append(line)
    # The content-read audit entries.
    await db.commit()
    return "\n".join(lines)


async def _get_assignment_path(db: AsyncSession, caller: Any, assignment: str = "", last_runs: int = 30) -> str:
    from app.services.health import flow_lab_access as access
    from app.services.health.flow_lab import assignment_path

    target = await access.resolve_assignment(db, caller, assignment)
    if target is None:
        return _not_found("assignment")
    path = await assignment_path(db, target, last_runs=int(last_runs or 30))
    # Tool names are a rollup, but still values a run produced: data, not instructions.
    return _untrusted(
        json.dumps(
            {
                "assignment_id": str(path.assignment_id),
                "trigger_type": path.trigger_type,
                "runs": path.runs,
                "path": list(path.path),
                "share": path.share,
                "stable": path.stable,
                "switchable": path.switchable,
                "reasons": list(path.reasons),
            },
            ensure_ascii=False,
        ),
        "assignment path",
    )


async def _find_flow_candidates(
    db: AsyncSession, caller: Any, agent: str = "", days: int = 30, min_occurrences: int = 5
) -> str:
    from app.services.health import flow_lab_access as access
    from app.services.health.flow_lab import repeated_sequences

    target = await access.resolve_agent(db, caller, agent)
    if target is None:
        return _not_found("agent")
    candidates = await repeated_sequences(
        db, [target.id], days=int(days or 30), min_occurrences=max(2, int(min_occurrences or 5))
    )
    if not candidates:
        return "No tool-call sequence recurs across several users in that window."
    return _untrusted(
        "\n".join(
            f"- {' -> '.join(c.sequence)}: {c.conversations} conversations, {c.users} users" for c in candidates
        ),
        "recurring tool-call sequences",
    )


async def _get_flow_shadow(db: AsyncSession, caller: Any, shadow_id: str = "") -> str:
    from sqlalchemy import func, select

    from app.models.assignment import Assignment
    from app.models.flow_shadow import FlowShadow, FlowShadowPair
    from app.services.health import flow_lab_access as access

    sid = access.parse_id(shadow_id)
    shadow = await db.get(FlowShadow, sid) if sid else None
    assignment = await db.get(Assignment, shadow.assignment_id) if shadow else None
    # Who may read a shadow is who may experiment on the assignment it shadows:
    # its figures are about that assignment's runs.
    if assignment is None or not await access.assignment_reachable(db, caller, assignment):
        return _not_found("shadow")
    counts = dict(
        (
            await db.execute(
                select(FlowShadowPair.status, func.count())
                .where(FlowShadowPair.shadow_id == shadow.id)
                .group_by(FlowShadowPair.status)
            )
        ).all()
    )
    return json.dumps(
        {
            "shadow_id": str(shadow.id),
            "purpose": shadow.purpose,
            "status": shadow.status,
            "assignment_id": str(shadow.assignment_id),
            "sandbox_flow_id": str(shadow.flow_id) if shadow.flow_id else None,
            "production_flow_id": str(shadow.production_flow_id) if shadow.production_flow_id else None,
            "ends_at": shadow.ends_at.isoformat(),
            "watch_until": shadow.watch_until.isoformat() if shadow.watch_until else None,
            "pairs": {status: int(count) for status, count in counts.items()},
            "verdict": shadow.verdict,
            "note": shadow.note,
        },
        ensure_ascii=False,
        default=str,
    )


async def _get_flow_run_comparison(
    db: AsyncSession, caller: Any, left_kind: str = "", left_id: str = "", right_kind: str = "", right_id: str = ""
) -> str:
    from app.services.health import flow_lab_access as access
    from app.services.health.flow_comparison import compare_sides, run_side

    left = await access.resolve_run(db, caller, left_kind, left_id)
    right = await access.resolve_run(db, caller, right_kind, right_id)
    if left is None or right is None:
        return _not_found("run")
    comparison = compare_sides(await run_side(db, left_kind, left), await run_side(db, right_kind, right))
    return json.dumps(comparison.as_dict(), ensure_ascii=False)


async def _get_flow_node_catalog(db: AsyncSession, caller: Any) -> str:
    """The node library, as ``flow_builder``'s catalog shows it, without the caller's bindable entities."""
    from app.services.flow_access import available_node_kinds
    from app.services.flow_catalog import EDGE_KINDS
    from app.services.flow_graph import MAX_EDGES, MAX_NODES

    kinds = [
        {
            "kind": nk["kind"],
            "category": nk["category"],
            "description": nk["description"],
            "inputs": [p["id"] for p in nk.get("inputs") or []],
            "outputs": [p["id"] for p in nk.get("outputs") or []],
            "config_fields": [
                {"name": f["name"], "type": f["type"], **({"required": True} if f.get("required") else {})}
                for f in nk.get("config_fields") or []
            ],
            "output_variables": [v["name"] for v in nk.get("output_variables") or []],
        }
        for nk in available_node_kinds()
    ]
    return json.dumps(
        {"node_kinds": kinds, "edge_kinds": list(EDGE_KINDS), "limits": {"max_nodes": MAX_NODES, "max_edges": MAX_EDGES}},
        ensure_ascii=False,
    )


# ── Writes (Platform Health, sandbox copies only) ────────────────────────────

#: How many of a source flow's latest settled runs a copy may replay writes from.
_REPLAY_RUNS = 50


def _checked_graph(graph: Any) -> tuple[dict | None, str]:
    """``(normalized graph, "")``, or ``(None, why)`` - the flow builder's own parsing and validation."""
    from app.services.builtin_tools.flow_builder import _issues_text, _parse_graph
    from app.services.flow_graph import auto_layout, normalize_graph, validate_graph

    parsed = _parse_graph(graph)
    if parsed is None:
        return None, "Error: the graph must be a JSON object (see get_flow_node_catalog)."
    laid_out = auto_layout(parsed)
    result = validate_graph(laid_out)
    if not result.is_valid:
        return None, "Error: the graph cannot be saved as it stands:\n" + _issues_text(result.errors)
    return normalize_graph(laid_out), ""


async def _version(db: AsyncSession, flow: Any, graph: dict, message: str) -> None:
    from app.services.flow_versioning import create_flow_version

    flow.graph = graph
    flow.version_number = (flow.version_number or 0) + 1
    await create_flow_version(db, flow_id=flow.id, graph=graph, author_id=None, commit_message=message)


def _reach_refusal(graph: dict, reach: set[str]) -> str | None:
    from app.services.health.flow_lab_access import node_config_ids

    outside = node_config_ids(graph) - reach
    if not outside:
        return None
    logger.info("Sandbox graph refused: %d ids outside its source's reach", len(outside))
    return (
        "Error: the graph names entities its source does not use. A sandbox flow keeps the integrations, "
        "agents and tools of the flow it copies or of the agent whose usage it automates."
    )


async def _create_sandbox_flow(
    db: AsyncSession, caller: Any, graph: Any = None, from_flow: str = "", basis_agent: str = "", name: str = ""
) -> str:
    from sqlalchemy import select

    from app.models.flow import Flow
    from app.models.flow_run import FlowRun
    from app.services.health import flow_lab_access as access
    from app.services.health.sandbox.copies import SandboxOverrides, create_sandbox_copy

    if not caller.is_health_principal:
        return _HEALTH_ONLY
    checked, refusal = _checked_graph(graph)
    if checked is None:
        return refusal
    source = await access.resolve_flow(db, caller, from_flow) if from_flow else None
    if from_flow and source is None:
        return _not_found("flow")
    agent = await access.resolve_agent(db, caller, basis_agent) if basis_agent else None
    if basis_agent and agent is None:
        return _not_found("agent")
    if source is None and agent is None:
        return "Error: name the flow it copies (from_flow) or the agent whose usage it automates (basis_agent)."
    # The copy runs as what it stands for, with live reads, and its output comes
    # back: the sandbox's own rule, opt-in and owner opt-out included, read
    # again at every run (``sandbox.tools.subject_refusal``).
    from app.services.health.sandbox.tools import subject_refusal

    for kind, row in (("flow", source), ("agent", agent)):
        if row is not None and await subject_refusal(db, kind, row.id) is not None:
            return _not_found(kind)
    refused = _reach_refusal(
        checked, await access.sandbox_reach(db, source_flow=source, basis_agent_id=agent.id if agent else None)
    )
    if refused:
        return refused
    replay_from: list[Any] = []
    if source is not None:
        # A repaired or optimized copy replays its source's recorded writes (B5).
        from app.services.health.consent import content_readable

        rows = (
            await db.execute(
                select(FlowRun.id, FlowRun.user_id)
                .where(FlowRun.flow_id == source.id, FlowRun.status.in_(("completed", "failed")))
                .order_by(FlowRun.created_at.desc())
                .limit(_REPLAY_RUNS)
            )
        ).all()
        # Recordings are content: only the runs consent admits are replayed.
        replay_from = [row.id for row in rows if await content_readable(db, flow_id=source.id, user_id=row.user_id)]
    copy = await create_sandbox_copy(
        db,
        source_type="flow",
        source_id=source.id if source else None,
        created_for=f"{access.BASIS_AGENT_PREFIX}{agent.id}" if agent else access.FLOW_LAB_COPY,
        created_by_agent_id=caller.principal.agent_id,
        overrides=SandboxOverrides(graph=checked),
        source_run_ids=replay_from,
    )
    flow = await db.get(Flow, copy.copy_flow_id)
    if name.strip():
        flow.name = name.strip()[:200]
    await db.commit()
    return f"Sandbox flow `{flow.id}` created (v{flow.version_number}), never listed in the library."


def _flow_lab_refusal(caller: Any) -> str | None:
    """Findings and drafts are the Flow Lab agent's own writes (level 1's role on the agent row)."""
    if not caller.is_health_principal or getattr(caller.principal, "role", None) != "flow_lab":
        return "Error: only the Platform Health Flow Lab can do this."
    return None


async def _target_reachable(db: AsyncSession, caller: Any, target_type: str, target_id: Any) -> bool:
    from app.models.agent import Agent
    from app.models.assignment import Assignment
    from app.models.flow import Flow
    from app.services.health import flow_lab_access as access
    from app.services.health.consent import in_supervision_scope

    if target_type == "flow":
        flow = await db.get(Flow, target_id)
        return flow is not None and not access.is_sandbox_flow(flow) and await access.flow_reachable(db, caller, flow)
    if target_type == "assignment":
        assignment = await db.get(Assignment, target_id)
        return assignment is not None and await access.assignment_reachable(db, caller, assignment)
    if target_type == "agent":
        return await db.get(Agent, target_id) is not None and await in_supervision_scope(db, "agent", target_id)
    return False


async def _checked_run_ids(db: AsyncSession, target_type: str, target_id: Any, run_ids: Any) -> list[str]:
    """The run ids that really are the target's runs (a flow's or an assignment's), newest first; the rest is dropped."""
    from app.models.assignment import AssignmentRun
    from app.models.flow_run import FlowRun
    from app.services.health import flow_lab_access as access

    wanted = [rid for rid in (access.parse_id(str(r)) for r in (run_ids or [])[:50]) if rid is not None]
    if not wanted or target_type not in ("flow", "assignment"):
        return []
    model, column = (FlowRun, FlowRun.flow_id) if target_type == "flow" else (AssignmentRun, AssignmentRun.assignment_id)
    from sqlalchemy import select

    rows = await db.execute(
        select(model.id).where(model.id.in_(wanted), column == target_id).order_by(model.created_at.desc()).limit(20)
    )
    return [str(rid) for rid in rows.scalars()]


async def _record_flow_finding(
    db: AsyncSession,
    caller: Any,
    target_type: str = "",
    target_id: str = "",
    pattern_kind: str = "",
    pattern_key: str = "",
    hypothesis: str = "",
    suggested_experiment: str = "",
    run_ids: Any = None,
    counts: Any = None,
) -> str:
    from app.services.health import flow_lab_access as access
    from app.services.health.findings import Evidence, FindingError, record_finding

    refusal = _flow_lab_refusal(caller)
    if refusal:
        return refusal
    tid = access.parse_id(target_id)
    if target_type not in ("flow", "assignment", "agent") or tid is None or not await _target_reachable(db, caller, target_type, tid):
        return _not_found("target")
    checked = await _checked_run_ids(db, target_type, tid, run_ids)
    numbers = {
        str(k)[:64]: v for k, v in (counts or {}).items() if isinstance(v, (int, float)) and not isinstance(v, bool)
    } if isinstance(counts, dict) else {}
    try:
        recorded = await record_finding(
            db,
            target_type=target_type,
            target_id=tid,
            pattern_kind=pattern_kind,
            pattern_key=pattern_key,
            hypothesis=hypothesis,
            suggested_experiment=suggested_experiment,
            evidence=Evidence(run_ids=tuple(checked), counts=dict(list(numbers.items())[:20])),
            # A run id leads to its payloads: content evidence, redacted when
            # the opt-in is revoked (level 6).
            content_evidence=bool(checked),
            author_agent_id=caller.principal.agent_id,
        )
    except FindingError as exc:
        return f"Error: {exc}"
    await db.commit()
    return json.dumps(
        {
            "finding_id": str(recorded.finding.id),
            "new": recorded.new,
            "reopened": recorded.reopened,
            "status": recorded.finding.status,
            "run_ids_dropped": len(run_ids or []) - len(checked) if isinstance(run_ids, list) else 0,
        }
    )


async def _draft_flow_variant(
    db: AsyncSession, caller: Any, finding_id: str = "", change_type: str = "", sandbox_flow_id: str = "", summary: str = ""
) -> str:
    from app.models.health_finding import HealthFinding
    from app.services.health import flow_lab_access as access
    from app.services.health.experiments import ExperimentError, draft
    from app.services.health.flow_experiments import FLOW_CHANGE_TYPES

    refusal = _flow_lab_refusal(caller)
    if refusal:
        return refusal
    if change_type not in FLOW_CHANGE_TYPES:
        return f"Error: change_type must be one of: {', '.join(FLOW_CHANGE_TYPES)}."
    fid = access.parse_id(finding_id)
    finding = await db.get(HealthFinding, fid) if fid else None
    if finding is None or not await _target_reachable(db, caller, finding.target_type, finding.target_id):
        return _not_found("finding")
    try:
        experiment, created = await draft(
            db,
            finding=finding,
            change_type=change_type,
            target_id=finding.target_id,
            payload={"sandbox_flow_id": sandbox_flow_id, "summary": summary},
            author_agent_id=caller.principal.agent_id,
        )
    except ExperimentError as exc:
        return f"Error: {exc}"
    await db.commit()
    return json.dumps({"experiment_id": str(experiment.id), "created": created, "status": experiment.status})


async def _update_sandbox_flow(db: AsyncSession, caller: Any, sandbox_flow_id: str = "", graph: Any = None) -> str:
    from app.services.health import flow_lab_access as access

    if not caller.is_health_principal:
        return _HEALTH_ONLY
    flow = await access.resolve_sandbox_flow(db, caller, sandbox_flow_id)
    if flow is None:
        return _not_found("sandbox flow")
    checked, refusal = _checked_graph(graph)
    if checked is None:
        return refusal
    from app.models.flow import Flow
    from app.services.health.sandbox.copies import sandbox_copy_of

    copy = await sandbox_copy_of(db, flow_id=flow.id)
    source = await db.get(Flow, copy.source_id) if copy is not None and copy.source_id else None
    reach = await access.sandbox_reach(
        db, source_flow=source, basis_agent_id=access.basis_agent_of(copy.created_for if copy else None)
    )
    refused = _reach_refusal(checked, reach)
    if refused:
        return refused
    await _version(db, flow, checked, "Flow Lab variant")
    await db.commit()
    return f"Sandbox flow `{flow.id}` updated to v{flow.version_number}."



async def _run_flow_shadow(db: AsyncSession, caller: Any, assignment: str = "", sandbox_flow_id: str = "") -> str:
    from app.models.flow_shadow import SHADOW_PURPOSE_MEASURE
    from app.services.health import flow_lab_access as access
    from app.services.health.flow_shadow import ShadowError, start_shadow

    if not caller.is_health_principal:
        return _HEALTH_ONLY
    target = await access.resolve_assignment(db, caller, assignment)
    flow = await access.resolve_sandbox_flow(db, caller, sandbox_flow_id)
    if target is None or flow is None:
        return _not_found("assignment or sandbox flow")
    # Its runs act as the assignment's identity: the graph may name only what
    # the assignment and its agent already reach, whichever copy it is.
    reach = await access.sandbox_reach(db, source_flow=None, basis_agent_id=target.agent_id)
    if access.node_config_ids(flow.graph) - (reach | access.graph_reference_ids(target.trigger_config)):
        return "Error: the sandbox flow names entities this assignment's agent does not use, so it cannot shadow it."
    from app.services.health.consent import content_readable

    # A shadow reads the assignment's events and outputs: content, so the
    # agent must be opted in (each run's user is checked again at read time).
    if not await content_readable(db, agent_id=target.agent_id):
        return "Error: this assignment's agent is not opted in to improvement, so it cannot be shadowed."
    try:
        shadow = await start_shadow(db, assignment=target, flow=flow, purpose=SHADOW_PURPOSE_MEASURE)
    except ShadowError as exc:
        await db.rollback()
        return f"Error: {exc}"
    await db.commit()
    return (
        f"Shadow `{shadow.id}` runs until {shadow.ends_at.isoformat()}: every live run of the assignment gets "
        "a paired run of the sandbox flow, every write intercepted. Read it with get_flow_shadow."
    )


# ── Definitions ────────────────────────────────────────────────────────────

_RUN_KIND = {"type": "string", "enum": ["flow_run", "assignment_run"]}


def _definition(name: str, description: str, properties: dict, required: list[str]) -> ToolDefinition:
    return ToolDefinition(
        name=name,
        description=description,
        input_schema={"type": "object", "properties": properties, "required": required},
        source=SOURCE,
    )


def _definitions() -> list[ToolDefinition]:
    return [
        _definition(
            "get_flow_diagnosis",
            "Repair or optimize a flow: its last settled runs - failure rate, per-node executions, failures, "
            "errors and duration percentiles - and its runs as replay cases, failed first.",
            {"flow": {"type": "string", "description": "Flow id or exact name."}, "last_runs": {"type": "integer"}},
            ["flow"],
        ),
        _definition(
            "get_assignment_path",
            "Agent to flow: whether an assignment's completed runs follow one stable tool-call path, and "
            "whether a flow could later take its trigger over.",
            {
                "assignment": {"type": "string", "description": "Assignment id or exact name."},
                "last_runs": {"type": "integer"},
            },
            ["assignment"],
        ),
        _definition(
            "find_flow_candidates",
            "New flow: tool-call sequences recurring in an agent's chats across several users.",
            {
                "agent": {"type": "string", "description": "Agent id or exact name."},
                "days": {"type": "integer"},
                "min_occurrences": {"type": "integer"},
            },
            ["agent"],
        ),
        _definition(
            "get_flow_shadow",
            "A shadow's status, pair counts, verdict (agreement, success rates, simulated writes) and watch.",
            {"shadow_id": {"type": "string"}},
            ["shadow_id"],
        ),
        _definition(
            "get_flow_run_comparison",
            "Compare two ended runs (flow_run or assignment_run): status, output agreement, tokens, cost, "
            "duration and simulated writes, with one verdict.",
            {
                "left_kind": _RUN_KIND,
                "left_id": {"type": "string"},
                "right_kind": _RUN_KIND,
                "right_id": {"type": "string"},
            },
            ["left_kind", "left_id", "right_kind", "right_id"],
        ),
        _definition(
            "get_flow_node_catalog",
            "The flow node library (kinds, ports, config fields, output variables) to compose a sandbox graph. "
            "Reference an upstream output as {{node_id.output_variable}}.",
            {},
            [],
        ),
        _definition(
            "create_sandbox_flow",
            "Create a hidden sandbox flow, never a library flow: a copy of a flow to repair or optimize "
            "(from_flow; its recorded writes are replayed), or a prototype automating an agent's usage "
            "(basis_agent). It may only name what that flow or agent already uses.",
            {
                "graph": {"type": "string", "description": "The canvas document as JSON."},
                "from_flow": {"type": "string", "description": "Flow id or exact name to copy."},
                "basis_agent": {"type": "string", "description": "Agent id or exact name, for a prototype."},
                "name": {"type": "string"},
            },
            ["graph"],
        ),
        _definition(
            "update_sandbox_flow",
            "Replace a sandbox flow's graph (bumps its version); a production flow is refused.",
            {"sandbox_flow_id": {"type": "string"}, "graph": {"type": "string", "description": "The canvas document as JSON."}},
            ["sandbox_flow_id", "graph"],
        ),
        _definition(
            "record_flow_finding",
            "Record a flow or assignment pattern once (repeat calls merge into the same finding): "
            "target_type flow, assignment or agent; evidence as run ids of that target and counts.",
            {
                "target_type": {"type": "string", "enum": ["flow", "assignment", "agent"]},
                "target_id": {"type": "string"},
                "pattern_kind": {"type": "string"},
                "pattern_key": {"type": "string"},
                "hypothesis": {"type": "string"},
                "suggested_experiment": {"type": "string"},
                "run_ids": {"type": "array", "items": {"type": "string"}},
                "counts": {"type": "object"},
            },
            ["target_type", "target_id", "pattern_kind", "hypothesis"],
        ),
        _definition(
            "draft_flow_variant",
            "Hand a sandbox flow to the Experimenter as the variant of a finding: flow_repair or "
            "flow_optimization (a copy of the finding's flow), new_flow (a prototype for the finding's agent), "
            "agent_to_flow (a prototype with exactly the finding's assignment trigger).",
            {
                "finding_id": {"type": "string"},
                "change_type": {"type": "string", "enum": ["flow_repair", "flow_optimization", "new_flow", "agent_to_flow"]},
                "sandbox_flow_id": {"type": "string"},
                "summary": {"type": "string"},
            },
            ["finding_id", "change_type", "sandbox_flow_id", "summary"],
        ),
        _definition(
            "run_flow_shadow",
            "Measure a sandbox flow on an assignment's live triggers for the shadow window: each live run gets "
            "a paired flow run, every write intercepted, the outputs compared.",
            {"assignment": {"type": "string"}, "sandbox_flow_id": {"type": "string"}},
            ["assignment", "sandbox_flow_id"],
        ),
    ]


_HANDLERS: dict[str, Callable[..., Awaitable[str]]] = {
    "get_flow_diagnosis": _get_flow_diagnosis,
    "get_assignment_path": _get_assignment_path,
    "find_flow_candidates": _find_flow_candidates,
    "get_flow_shadow": _get_flow_shadow,
    "get_flow_run_comparison": _get_flow_run_comparison,
    "run_flow_shadow": _run_flow_shadow,
    "get_flow_node_catalog": _get_flow_node_catalog,
    "create_sandbox_flow": _create_sandbox_flow,
    "update_sandbox_flow": _update_sandbox_flow,
    "record_flow_finding": _record_flow_finding,
    "draft_flow_variant": _draft_flow_variant,
}


def build_flow_lab_tools(
    user_id: str, *, read_only: bool = False, agent_id: str | None = None
) -> tuple[list[ToolDefinition], CallableToolExecutor]:
    """The Flow Lab tools acting for *user_id*, or for the Platform Health principal when *agent_id* is its own
    (``read_only``: the reads alone, ``/mcp/xtm``, where no agent calls).

    A function left out is not registered, so it cannot be called by guessing its name.
    """
    definitions = _definitions()
    if read_only:
        definitions = [d for d in definitions if d.name in FLOW_LAB_READ_TOOLS]
    executor = CallableToolExecutor()
    for definition in definitions:

        def _bind(tool_name: str = definition.name):
            async def _call(**args: Any) -> str:
                return await _run(user_id, tool_name, _HANDLERS[tool_name], args, agent_id=agent_id)

            return _call

        executor.register(definition.name, _bind())
    return definitions, executor


def add_flow_lab_tools(composite: Any, user_id: str, builtin_tool_ids: list[str], *, agent_id: str | None) -> None:
    """Wire the ``flow_lab`` group into a turn's *composite* when the agent carries it and Platform Health is available.

    The one wiring of agent chat and the assignment engine; a Platform Health run may
    have no user, the tools then act as its principal alone, and only for
    *agent_id*, the principal's own agent: an agent its run executes inline
    reads as its runner.
    """
    if FLOW_LAB_GROUP_ID in builtin_tool_ids and _enabled():
        composite.add_tools(*build_flow_lab_tools(user_id, agent_id=agent_id))
