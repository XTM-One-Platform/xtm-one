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

"""The Quality Analyst's tools (Platform Health, #8259), and what the
Platform Health's tool groups share.

One access rule (``services/health/access.py``) read on the rows at every
call, on every surface (agent tools, ``/mcp/xtm``). The ``health_diagnosis``
group: ``get_quality_signals``, ``list_findings`` (reads), ``record_finding``,
``draft_variant`` (Platform Health's Quality Analyst only). The Experimenter's
group is ``health_experiments.py``, on the same helpers.

No tool here writes an agent, a skill or an assignment: a variant lives in its
experiment row and in sandbox copies, and the only path to production is the
proposal its managers approve (level 3).

Every value that came from a trace, a rating, a tool name or a model is
returned inside an untrusted-data block; errors are generic, reasons in logs.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from app.core import database
from app.core.utils import format_exception_message
from app.tools.base import CallableToolExecutor, ToolDefinition

from ._shared import logger

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.services.health.access import Reader

DIAGNOSIS_GROUP_ID = "health_diagnosis"

#: The read functions of the group: the only ones the platform MCP server serves.
DIAGNOSIS_READ_TOOLS: frozenset[str] = frozenset({"get_quality_signals", "list_findings"})

_OFF = "Error: Platform Health is not enabled on this platform."
_FAILED = "Error: Platform Health could not complete this request right now."
_NO_READER = "Error: this tool needs an authenticated caller."


class RefusalError(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def health_enabled() -> bool:
    from app.services.health.availability import health_enabled

    return bool(health_enabled())


def untrusted_json(payload: Any, source: str) -> str:
    """*payload* as JSON inside an untrusted-data block: its values came from traces, ratings, models."""
    from app.services.content_safety import wrap_untrusted

    return wrap_untrusted(json.dumps(payload, ensure_ascii=False, default=str), source=source)


def parse_uuid(value: Any, label: str) -> uuid.UUID:
    try:
        return uuid.UUID(str(value or "").strip())
    except (ValueError, TypeError):
        raise RefusalError(f"Error: {label} must be a valid UUID.") from None


async def run_tool(
    user_id: str,
    name: str,
    fn: Callable[..., Awaitable[str]],
    args: dict[str, Any],
    *,
    run_id: str | None = None,
    agent_id: str | None = None,
) -> str:
    from app.services.health.access import load_reader
    from app.services.health.experiments import ExperimentError
    from app.services.health.findings import FindingError
    from app.services.health.quality_deps import ProposalRefusedError
    from app.services.health.variants import VariantError

    if not health_enabled():
        return _OFF
    try:
        async with database.async_session() as db:
            reader = await load_reader(db, user_id, run_id=run_id, agent_id=agent_id)
            if reader is None:
                return _NO_READER
            return await fn(db, reader, **args)
    except RefusalError as refusal:
        return refusal.message
    except (FindingError, VariantError, ExperimentError, ProposalRefusedError) as exc:
        return f"Error: {exc}"
    except TypeError as exc:
        # An argument the schema does not name.
        logger.info("Platform Health tool %s refused its arguments: %s", name, format_exception_message(exc))
        return "Error: unexpected arguments for this tool."
    except Exception as exc:
        logger.error("Platform Health tool %s failed: %s", name, format_exception_message(exc), exc_info=True)
        return _FAILED


async def agent_access_or_refuse(db: AsyncSession, reader: Reader, agent_id: uuid.UUID):
    from app.services.health.access import NOT_IN_REACH

    access = await reader.agent_access(db, agent_id)
    if access is None:
        raise RefusalError(NOT_IN_REACH)
    return access


async def finding_for(db: AsyncSession, reader: Reader, finding_id: Any):
    from app.models.health_finding import HealthFinding
    from app.services.health.access import NOT_IN_REACH, target_access

    finding = await db.get(HealthFinding, parse_uuid(finding_id, "finding_id"))
    if finding is None:
        raise RefusalError("Error: finding not found.")
    access = await target_access(db, reader, finding.target_type, finding.target_id)
    if access is None:
        # The same answer as a missing finding would leak its existence.
        raise RefusalError("Error: finding not found." if not reader.is_health_principal else NOT_IN_REACH)
    return finding, access


def _finding_payload(finding: Any, *, content: bool) -> dict[str, Any]:
    from app.services.health.access import redact_finding

    payload = {
        "id": str(finding.id),
        "target_type": finding.target_type,
        "target_id": str(finding.target_id),
        "pattern_kind": finding.pattern_kind,
        "pattern_key": finding.pattern_key,
        "status": finding.status,
        "hypothesis": finding.hypothesis,
        "suggested_experiment": finding.suggested_experiment,
        "evidence": finding.evidence or {},
        "content_evidence": bool(finding.content_evidence),
        "occurrences": finding.occurrences,
        "first_seen_at": finding.first_seen_at.isoformat() if finding.first_seen_at else None,
        "last_seen_at": finding.last_seen_at.isoformat() if finding.last_seen_at else None,
        "reported_at": finding.reported_at.isoformat() if finding.reported_at else None,
        "proposal_id": str(finding.proposal_id) if finding.proposal_id else None,
    }
    return redact_finding(payload, content=content)


# ── Diagnosis ────────────────────────────────────────────────────────────────


async def _get_quality_signals(db: AsyncSession, reader: Reader, agent_id: str = "", period_days: int = 7) -> str:
    from app.services.health.diagnosis import quality_signals

    access = await agent_access_or_refuse(db, reader, parse_uuid(agent_id, "agent_id"))
    signals = await quality_signals(db, reader, access, days=period_days)
    await db.commit()  # the content-read audit entries
    return untrusted_json(signals, "Platform Health quality signals")


async def _list_findings(
    db: AsyncSession, reader: Reader, target_id: str = "", status: str = "", limit: int = 20
) -> str:
    from app.services.health import findings as finding_store
    from app.services.health.access import target_access

    limit = max(1, min(int(limit or 20), 50))
    target_ids: list[uuid.UUID] | None = None
    if (target_id or "").strip():
        target_ids = [parse_uuid(target_id, "target_id")]
    elif not reader.is_health_principal:
        raise RefusalError("Error: name the agent or assignment whose findings to list (target_id).")
    shown: list[dict[str, Any]] = []
    access_by_target: dict[tuple[str, uuid.UUID], Any] = {}
    # Three times *limit* read, so the findings the caller may not read rarely shorten the page.
    rows = await finding_store.list_findings(db, target_ids=target_ids, status=status or None, limit=limit * 3)
    for finding in rows:
        key = (finding.target_type, finding.target_id)
        if key not in access_by_target:
            access_by_target[key] = await target_access(db, reader, *key)
        access = access_by_target[key]
        if access is None:
            continue
        payload = _finding_payload(finding, content=access.content)
        if reader.opted_out_user_ids and access.content and access.agent is not None:
            # Evidence of a user who opted out since: read from the rows again.
            payload["evidence"] = await _without_opted_out(db, reader, access, payload["evidence"])
        shown.append(payload)
        if len(shown) >= limit:
            break
    return untrusted_json({"count": len(shown), "findings": shown}, "Platform Health findings")


async def _without_opted_out(db: AsyncSession, reader: Reader, access: Any, evidence: dict[str, Any]) -> dict[str, Any]:
    from app.services.health.diagnosis import verify_evidence

    checked, _dropped = await verify_evidence(db, reader, access, evidence)
    return {**checked.as_json(), "counts": evidence.get("counts") or {}}


async def _record_finding(
    db: AsyncSession,
    reader: Reader,
    target_type: str = "agent",
    target_id: str = "",
    pattern_kind: str = "",
    pattern_key: str = "",
    hypothesis: str = "",
    suggested_experiment: str = "",
    evidence: dict | None = None,
) -> str:
    from app.services.health.access import agent_of_target
    from app.services.health.diagnosis import verify_evidence
    from app.services.health.findings import record_finding

    refusal = reader.write_refusal("quality_analyst")
    if refusal:
        return refusal
    if target_type not in ("agent", "assignment"):
        raise RefusalError("Error: target_type must be agent or assignment.")
    target = parse_uuid(target_id, "target_id")
    agent_id = await agent_of_target(db, target_type, target)
    if agent_id is None:
        raise RefusalError("Error: target not found.")
    access = await agent_access_or_refuse(db, reader, agent_id)
    checked, dropped = await verify_evidence(db, reader, access, evidence)
    # Read with content access, the hypothesis may draw on it whatever refs it cites.
    content_evidence = access.content
    recorded = await record_finding(
        db,
        target_type=target_type,
        target_id=target,
        pattern_kind=pattern_kind,
        pattern_key=pattern_key,
        hypothesis=hypothesis,
        suggested_experiment=suggested_experiment,
        evidence=checked,
        content_evidence=content_evidence,
        author_agent_id=getattr(reader.principal, "agent_id", None),
    )
    await db.commit()
    return json.dumps(
        {
            "finding_id": str(recorded.finding.id),
            "new": recorded.new,
            "reopened": recorded.reopened,
            "status": recorded.finding.status,
            "occurrences": recorded.finding.occurrences,
            "evidence_refs_dropped": dropped,
        }
    )


async def _draft_variant(
    db: AsyncSession,
    reader: Reader,
    finding_id: str = "",
    change_type: str = "",
    target_id: str = "",
    text: str = "",
    summary: str = "",
) -> str:
    from app.services.health import text_kinds  # noqa: F401 - registers level 6's kinds
    from app.services.health.experiments import draft

    refusal = reader.write_refusal("quality_analyst")
    if refusal:
        return refusal
    finding, _ = await finding_for(db, reader, finding_id)
    experiment, created = await draft(
        db,
        finding=finding,
        change_type=change_type,
        target_id=parse_uuid(target_id, "target_id"),
        payload={"text": text, "summary": summary},
        author_agent_id=getattr(reader.principal, "agent_id", None),
    )
    await db.commit()
    return json.dumps({"experiment_id": str(experiment.id), "created": created, "status": experiment.status})


# ── Definitions ──────────────────────────────────────────────────────────────

_EVIDENCE_SCHEMA = {
    "type": "object",
    "description": "Refs that show the pattern, checked against the rows: ids that are not the target's are dropped.",
    "properties": {
        "trace_ids": {"type": "array", "items": {"type": "string"}, "maxItems": 20},
        "rating_ids": {"type": "array", "items": {"type": "string"}, "maxItems": 20},
        "run_ids": {"type": "array", "items": {"type": "string"}, "maxItems": 20, "description": "Assignment run ids."},
        "counts": {"type": "object", "description": 'Numeric figures, e.g. {"failed": 7, "runs": 40}.'},
    },
}


def _diagnosis_definitions() -> list[ToolDefinition]:
    from app.models.health_finding import FINDING_STATUSES, PATTERN_KINDS
    from app.services.builtin_tool_groups import builtin_source

    source = builtin_source(DIAGNOSIS_GROUP_ID)
    return [
        ToolDefinition(
            name="get_quality_signals",
            description=(
                "Quality signals of one supervised agent over a window and the window before: runs, failure rate "
                "and failure causes, tool calls and errors per tool, cost and tokens per run, p50/p90 latency, "
                "ratings (users and platform reactions apart), criteria, the latest negative ratings, and the "
                "findings already known for it. Trace ids and rating comments only for an agent opted in to "
                "improvement. Read-only."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "agent_id": {"type": "string", "description": "The agent's id."},
                    "period_days": {"type": "integer", "minimum": 1, "maximum": 30, "default": 7},
                },
                "required": ["agent_id"],
            },
            source=source,
        ),
        ToolDefinition(
            name="list_findings",
            description=(
                "Findings recorded on an agent or an assignment, latest seen first: pattern, evidence refs, "
                "hypothesis, the experiment it calls for, status, occurrences. A finding is recorded once and "
                "kept across cycles. Read-only."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "target_id": {"type": "string", "description": "Agent or assignment id."},
                    "status": {"type": "string", "enum": list(FINDING_STATUSES)},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 20},
                },
            },
            source=source,
        ),
        ToolDefinition(
            name="record_finding",
            description=(
                "Record a pattern found on a supervised agent or assignment, with its evidence, the hypothesis "
                "that explains it and the experiment it calls for. The same pattern (target, kind, key) is one "
                "finding across cycles: recording it again updates it and answers new=false. Write the "
                "hypothesis in the platform language."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "target_type": {"type": "string", "enum": ["agent", "assignment"]},
                    "target_id": {"type": "string"},
                    "pattern_kind": {"type": "string", "enum": list(PATTERN_KINDS)},
                    "pattern_key": {
                        "type": "string",
                        "description": (
                            "The instance inside the kind: the failure cause, the tool name, the criterion or "
                            "evaluation id, a short slug for a behaviour; empty for cost_drift and latency. "
                            "Reuse the key of a known finding."
                        ),
                    },
                    "hypothesis": {"type": "string", "maxLength": 2000},
                    "suggested_experiment": {"type": "string", "maxLength": 2000},
                    "evidence": _EVIDENCE_SCHEMA,
                },
                "required": ["target_type", "target_id", "pattern_kind", "hypothesis"],
            },
            source=source,
        ),
        ToolDefinition(
            name="draft_variant",
            description=(
                "Draft the variant an experiment will measure for a finding: the agent's persona, the content of a "
                "skill it uses, or an assignment's prompt, rewritten in that text's own language with targeted "
                "changes only (at most +50 % or +2,000 characters, the template placeholders kept). Nothing is "
                "applied: the Experimenter measures it on sandbox copies."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "finding_id": {"type": "string"},
                    "change_type": {"type": "string", "enum": ["persona", "skill", "assignment_prompt"]},
                    "target_id": {
                        "type": "string",
                        "description": "The agent (persona), the skill, or the assignment the variant replaces text of.",
                    },
                    "text": {"type": "string", "description": "The complete new text."},
                    "summary": {"type": "string", "description": "What changes and which evidence calls for it."},
                },
                "required": ["finding_id", "change_type", "target_id", "text", "summary"],
            },
            source=source,
        ),
    ]


def register_handlers(
    executor: CallableToolExecutor,
    user_id: str,
    handlers: dict[str, Callable[..., Awaitable[str]]],
    *,
    run_id: str | None = None,
    agent_id: str | None = None,
) -> None:
    for name, fn in handlers.items():

        def _bind(tool_name: str = name, handler: Callable[..., Awaitable[str]] = fn):
            async def _call(**args: Any) -> str:
                return await run_tool(user_id, tool_name, handler, args, run_id=run_id, agent_id=agent_id)

            return _call

        executor.register(name, _bind())


def build_health_diagnosis_tools(
    user_id: str, *, read_only: bool = False, run_id: str | None = None, agent_id: str | None = None
) -> tuple[list[ToolDefinition], CallableToolExecutor]:
    """The Quality Analyst's tools acting for *user_id* (``read_only``: the reads alone, as on ``/mcp/xtm``),
    called by *agent_id* in the assignment run *run_id*, if any."""
    handlers: dict[str, Callable[..., Awaitable[str]]] = {
        "get_quality_signals": _get_quality_signals,
        "list_findings": _list_findings,
        "record_finding": _record_finding,
        "draft_variant": _draft_variant,
    }
    return build_group(
        user_id,
        _diagnosis_definitions(),
        handlers,
        DIAGNOSIS_READ_TOOLS if read_only else None,
        run_id=run_id,
        agent_id=agent_id,
    )


def health_tools_offered() -> bool:
    """Whether Platform Health's tools are offered at all: its availability (each call checks it again)."""
    return health_enabled()


def add_health_quality_tools(
    composite: Any,
    user_id: str,
    builtin_tool_ids: list[str],
    *,
    agent_id: str | None,
    run_id: str | None = None,
) -> None:
    """Wire the groups *builtin_tool_ids* carries into a turn's *composite*, while Platform Health is available.

    The one wiring of agent chat and the assignment engine. A run of the
    Platform Health may have no user (a company-managed scheduled run): the tools
    then act as its principal alone, and only for *agent_id*, the principal's
    own agent: an agent its run executes inline reads as its runner. *run_id*
    (an assignment run) names the Platform Health cycle an experiment's spend is
    booked on.
    """
    from .health_experiments import EXPERIMENTS_GROUP_ID, build_health_experiment_tools
    from .health_modules import MODULES_GROUP_ID, build_health_module_tools, module_tools_offered

    if not any(gid in builtin_tool_ids for gid in (DIAGNOSIS_GROUP_ID, EXPERIMENTS_GROUP_ID, MODULES_GROUP_ID)):
        return
    if not health_tools_offered():
        return
    if DIAGNOSIS_GROUP_ID in builtin_tool_ids:
        composite.add_tools(*build_health_diagnosis_tools(user_id, run_id=run_id, agent_id=agent_id))
    if EXPERIMENTS_GROUP_ID in builtin_tool_ids:
        composite.add_tools(*build_health_experiment_tools(user_id, run_id=run_id, agent_id=agent_id))
    if MODULES_GROUP_ID in builtin_tool_ids and module_tools_offered():
        composite.add_tools(*build_health_module_tools(user_id, run_id=run_id, agent_id=agent_id))


def build_group(
    user_id: str,
    definitions: list[ToolDefinition],
    handlers: dict[str, Callable[..., Awaitable[str]]],
    read_tools: frozenset[str] | None,
    *,
    run_id: str | None = None,
    agent_id: str | None = None,
) -> tuple[list[ToolDefinition], CallableToolExecutor]:
    """A group's definitions and executor; *read_tools* keeps the reads alone (``/mcp/xtm``)."""
    if read_tools is not None:
        definitions = [d for d in definitions if d.name in read_tools]
        handlers = {name: fn for name, fn in handlers.items() if name in read_tools}
    executor = CallableToolExecutor()
    register_handlers(executor, user_id, handlers, run_id=run_id, agent_id=agent_id)
    return definitions, executor
