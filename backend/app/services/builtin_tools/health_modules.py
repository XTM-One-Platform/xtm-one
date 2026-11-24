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

"""The Module Analyst's platform tools (#101 S14a-2b, ``dev-docs/platform-health.md`` section 12).

The ``health_modules`` group: ``list_module_findings`` (read) and
``record_finding_recommendation`` and ``record_module_check_report`` (S15c, section 13.6:
the module checker's own writes). All act inside
XTM One only: the findings are the health engine's rows, the recommendation a
``health_finding_recommendations`` row. Nothing here reaches a module; the module
reads are the registration integration's own read tools behind
``services/health/module_analyst.MODULE_CHECKER_TOOLS`` (S14a-2b-2).

Access, read on the rows at every call:

* ``list_module_findings``: Platform Health's Module Analyst, or an administrator
  (module surfaces are for administrators, section 8.1).
* ``record_finding_recommendation``: the Module Analyst alone, in one of its cycles,
  on a finding of the registration that cycle investigates.
* ``record_module_check_report``: a module checker alone, in one of its cycles, on the
  registration that cycle checks; its verdicts drive the ``<module>.agentic.<check>``
  findings (``services/health/check_reports.py``).

Every value that came from a module or a model is returned inside an untrusted-data
block; errors are generic, reasons in logs.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from app.tools.base import CallableToolExecutor, ToolDefinition

from .health_quality import RefusalError, build_group, parse_uuid, untrusted_json

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from sqlalchemy import Select
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.services.health.access import Reader

MODULES_GROUP_ID = "health_modules"
#: The S14 role, kept as an alias of the module checkers' roles (section 13.1).
ANALYST_ROLE = "module_analyst"
#: The read functions of the group.
MODULES_READ_TOOLS: frozenset[str] = frozenset({"list_module_findings"})
#: The previous recommendations returned per finding, newest first (section 12.3).
PREVIOUS_RECOMMENDATIONS = 3
#: The most evidence entries one recommendation keeps.
MAX_EVIDENCE_ENTRIES = 20

_NOT_ALLOWED = "Error: only an administrator or Platform Health's Module Analyst reads module findings."
_NOT_IN_CYCLE = "Error: a recommendation is recorded only inside a Module Analyst cycle."
_NOT_IN_CYCLE_REPORT = "Error: a check report is recorded only inside a module checker cycle."


def _is_analyst(reader: Reader) -> bool:
    from app.services.health.identity import MODULE_CHECKER_ROLES

    return reader.is_health_principal and getattr(reader.principal, "role", None) in MODULE_CHECKER_ROLES


def _may_read(reader: Reader) -> bool:
    return _is_analyst(reader) or bool(reader.user is not None and getattr(reader.user, "is_admin", False))


async def _module_registration(db: AsyncSession, registration_id: uuid.UUID) -> str:
    """The module provider of *registration_id*, refused when it is no module registration."""
    from sqlalchemy import select

    from app.models.platform_registration import PlatformRegistration
    from app.services.health.module_analyst import MODULE_PROVIDERS

    provider = (
        await db.execute(
            select(PlatformRegistration.platform_identifier).where(PlatformRegistration.id == registration_id)
        )
    ).scalar_one_or_none()
    if provider not in MODULE_PROVIDERS:
        raise RefusalError("Error: registration not found.")
    return str(provider)


def _registration_of(finding: Any) -> str | None:
    subject = finding.subject if isinstance(finding.subject, dict) else {}
    value = subject.get("registration_id")
    return str(value) if value else None


def _open_findings_query(module: str, registration: uuid.UUID, limit: int) -> Select[Any]:
    """The open findings of *registration*, most severe first, then by check and id.

    The order is the engine's severity weight (critical, warning, info), never the
    severity string, so a ``limit`` drops info findings before warnings.
    """
    from sqlalchemy import case, select

    from app.models.health_check import STATUS_OPEN, HealthCheckFinding
    from app.services.health.engine import SEVERITY_WEIGHTS

    severity_weight = case(SEVERITY_WEIGHTS, value=HealthCheckFinding.severity, else_=0)
    return (
        select(HealthCheckFinding)
        .where(
            HealthCheckFinding.module == module,
            HealthCheckFinding.status == STATUS_OPEN,
            HealthCheckFinding.subject["registration_id"].astext == str(registration),
        )
        .order_by(severity_weight.desc(), HealthCheckFinding.check_id, HealthCheckFinding.id)
        .limit(limit)
    )


async def _list_module_findings(db: AsyncSession, reader: Reader, registration_id: str = "", limit: int = 50) -> str:
    from sqlalchemy import select

    from app.models.health_module_analyst import HealthFindingRecommendation

    if not _may_read(reader):
        raise RefusalError(_NOT_ALLOWED)
    registration = parse_uuid(registration_id, "registration_id")
    module = await _module_registration(db, registration)
    limit = max(1, min(int(limit or 50), 100))
    rows = (await db.execute(_open_findings_query(module, registration, limit))).scalars().all()
    previous: dict[str, list[dict[str, Any]]] = {}
    if rows:
        recs = (
            (
                await db.execute(
                    select(HealthFindingRecommendation)
                    .where(HealthFindingRecommendation.finding_id.in_([row.id for row in rows]))
                    .order_by(HealthFindingRecommendation.created_at.desc())
                )
            )
            .scalars()
            .all()
        )
        for rec in recs:
            kept = previous.setdefault(rec.finding_id, [])
            if len(kept) < PREVIOUS_RECOMMENDATIONS:
                kept.append(
                    {
                        "text": rec.text,
                        "confidence": rec.confidence,
                        "cycle_id": str(rec.cycle_id),
                        "created_at": rec.created_at.isoformat() if rec.created_at else None,
                    }
                )
    findings = [
        {
            "id": row.id,
            "check_id": row.check_id,
            "category": row.category,
            "severity": row.severity,
            "subject": row.subject or {},
            "evidence": row.evidence or {},
            "recommendation": row.recommendation,
            "first_seen": row.first_seen.isoformat() if row.first_seen else None,
            "last_seen": row.last_seen.isoformat() if row.last_seen else None,
            "previous_recommendations": previous.get(row.id, []),
        }
        for row in rows
    ]
    return untrusted_json(
        {"registration_id": str(registration), "module": module, "findings": findings}, source="platform health"
    )


def _clean_evidence(evidence: Any) -> list[dict[str, Any]]:
    if evidence is None:
        return []
    if not isinstance(evidence, list) or len(evidence) > MAX_EVIDENCE_ENTRIES:
        raise RefusalError(f"Error: evidence must be a list of at most {MAX_EVIDENCE_ENTRIES} entries.")
    cleaned: list[dict[str, Any]] = []
    for entry in evidence:
        if not isinstance(entry, dict) or not str(entry.get("tool") or "").strip():
            raise RefusalError("Error: each evidence entry names the tool it read.")
        ids = entry.get("object_ids") or []
        cleaned.append(
            {
                "tool": str(entry["tool"]).strip()[:128],
                "object_ids": [str(i)[:256] for i in ids][:20] if isinstance(ids, list) else [],
                "values": entry.get("values") if isinstance(entry.get("values"), dict) else {},
                "read_at": str(entry.get("read_at") or "")[:64] or None,
            }
        )
    return cleaned


async def _record_finding_recommendation(
    db: AsyncSession,
    reader: Reader,
    finding_id: str = "",
    text: str = "",
    confidence: str = "",
    evidence: list[dict[str, Any]] | None = None,
) -> str:
    from sqlalchemy import select

    from app.models.health_check import STATUS_OPEN, HealthCheckFinding
    from app.models.health_cycle import HealthCycleRun
    from app.models.health_module_analyst import (
        RECOMMENDATION_CONFIDENCES,
        RECOMMENDATION_MAX_CHARS,
        HealthFindingRecommendation,
    )
    from app.services.health.access import WRITE_REFUSED
    from app.services.health.module_analyst import MODULE_PROVIDERS

    # Reserved to a module checker's own principal (any of the three roles, section 13.1).
    principal_role = str(getattr(reader.principal, "role", "") or "")
    refusal = reader.write_refusal(principal_role if _is_analyst(reader) else ANALYST_ROLE)
    if refusal or not _is_analyst(reader):
        return refusal or WRITE_REFUSED
    if reader.cycle_id is None:
        raise RefusalError(_NOT_IN_CYCLE)
    body = (text or "").strip()
    if not body or len(body) > RECOMMENDATION_MAX_CHARS:
        raise RefusalError(f"Error: text must hold 1 to {RECOMMENDATION_MAX_CHARS} characters.")
    if confidence not in RECOMMENDATION_CONFIDENCES:
        raise RefusalError("Error: confidence must be high, medium or low.")
    checked = _clean_evidence(evidence)
    cycle = (
        await db.execute(
            select(HealthCycleRun.target_type, HealthCycleRun.target_id).where(
                HealthCycleRun.cycle_id == reader.cycle_id
            )
        )
    ).one_or_none()
    if cycle is None or cycle.target_type != "module" or cycle.target_id is None:
        raise RefusalError(_NOT_IN_CYCLE)
    finding = await db.get(HealthCheckFinding, str(finding_id or "").strip())
    if finding is None or finding.module not in MODULE_PROVIDERS or _registration_of(finding) != str(cycle.target_id):
        # The same answer for a finding of another registration: no existence leak.
        raise RefusalError("Error: finding not found.")
    if finding.status != STATUS_OPEN:
        raise RefusalError("Error: the finding is no longer open.")
    row = (
        await db.execute(
            select(HealthFindingRecommendation).where(
                HealthFindingRecommendation.finding_id == finding.id,
                HealthFindingRecommendation.cycle_id == reader.cycle_id,
            )
        )
    ).scalar_one_or_none()
    created = row is None
    if row is None:
        row = HealthFindingRecommendation(
            finding_id=finding.id,
            registration_id=cycle.target_id,
            cycle_id=reader.cycle_id,
            module=finding.module,
            text=body,
            evidence=checked,
            confidence=confidence,
        )
        db.add(row)
    else:
        # One recommendation per finding and cycle: a second call in the cycle revises it.
        row.text, row.evidence, row.confidence = body, checked, confidence
    await db.commit()
    return json.dumps({"recommendation_id": str(row.id), "finding_id": finding.id, "created": created})


async def _record_module_check_report(
    db: AsyncSession,
    reader: Reader,
    checks: list[dict[str, Any]] | None = None,
    started_at: str = "",
) -> str:
    from datetime import UTC, datetime

    from sqlalchemy import select

    from app.models.health_cycle import HealthCycleRun
    from app.models.health_module_analyst import HealthModuleCheckReport
    from app.services.health.access import WRITE_REFUSED
    from app.services.health.check_reports import (
        CheckReportError,
        apply_verdicts,
        check_logic_version,
        clean_checks,
        counts,
    )

    # The module checker's own write, inside its own cycle (section 13.6).
    principal_role = str(getattr(reader.principal, "role", "") or "")
    refusal = reader.write_refusal(principal_role if _is_analyst(reader) else ANALYST_ROLE)
    if refusal or not _is_analyst(reader):
        return refusal or WRITE_REFUSED
    if reader.cycle_id is None:
        raise RefusalError(_NOT_IN_CYCLE_REPORT)
    try:
        verdicts = clean_checks(checks)
    except CheckReportError as exc:
        raise RefusalError(str(exc)) from exc
    cycle = (
        await db.execute(
            select(HealthCycleRun.target_type, HealthCycleRun.target_id).where(
                HealthCycleRun.cycle_id == reader.cycle_id
            )
        )
    ).one_or_none()
    if cycle is None or cycle.target_type != "module" or cycle.target_id is None:
        raise RefusalError(_NOT_IN_CYCLE_REPORT)
    module = await _module_registration(db, cycle.target_id)
    now = datetime.now(UTC)
    started = _parse_time(started_at)
    finding_ids, findings = await apply_verdicts(
        db, module=module, registration_id=cycle.target_id, verdicts=verdicts, now=now
    )
    logic = check_logic_version(module)
    entries = [v.as_entry(fid, logic) for v, fid in zip(verdicts, finding_ids, strict=True)]
    tally = counts(verdicts)
    row = (
        await db.execute(select(HealthModuleCheckReport).where(HealthModuleCheckReport.cycle_id == reader.cycle_id))
    ).scalar_one_or_none()
    created = row is None
    if row is None:
        row = HealthModuleCheckReport(registration_id=cycle.target_id, cycle_id=reader.cycle_id, module=module)
        db.add(row)
    # One report per cycle: a second call replaces it.
    row.started_at, row.finished_at, row.checks = started, now, entries
    row.passed, row.warned, row.failed, row.not_run = (
        tally["passed"],
        tally["warned"],
        tally["failed"],
        tally["not_run"],
    )
    await db.commit()
    return json.dumps({"report_id": str(row.id), "created": created, **tally, "findings": findings})


def _parse_time(value: str) -> Any:
    from datetime import datetime

    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).strip())
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


# ── Definitions ──────────────────────────────────────────────────────────────


def _definitions() -> list[ToolDefinition]:
    from app.models.health_check import CATEGORIES
    from app.models.health_module_analyst import CHECK_VERDICTS, RECOMMENDATION_CONFIDENCES, RECOMMENDATION_MAX_CHARS
    from app.services.builtin_tool_groups import builtin_source
    from app.services.health.check_reports import MAX_CHECK_EVIDENCE, MAX_CHECKS

    source = builtin_source(MODULES_GROUP_ID)
    return [
        ToolDefinition(
            name="list_module_findings",
            description=(
                "Open health findings of one registered module (a platform registration id), most severe first: "
                "check, category, severity, subject, evidence, the deterministic recommendation, and the latest "
                "Module Analyst recommendations already attached, so a known cause is said once. Read-only."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "registration_id": {"type": "string", "description": "The platform registration id."},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 50},
                },
                "required": ["registration_id"],
            },
            source=source,
        ),
        ToolDefinition(
            name="record_finding_recommendation",
            description=(
                "Attach a recommendation to an open finding of the module this cycle investigates: what explains "
                "it and what to do, the module reads that show it, and how sure you are. One per finding and "
                "cycle; calling again in the cycle revises it. Never changes the module. Write in the platform "
                "language."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "finding_id": {"type": "string"},
                    "text": {"type": "string", "maxLength": RECOMMENDATION_MAX_CHARS},
                    "confidence": {"type": "string", "enum": list(RECOMMENDATION_CONFIDENCES)},
                    "evidence": {
                        "type": "array",
                        "maxItems": MAX_EVIDENCE_ENTRIES,
                        "items": {
                            "type": "object",
                            "properties": {
                                "tool": {"type": "string"},
                                "object_ids": {"type": "array", "items": {"type": "string"}},
                                "values": {"type": "object"},
                                "read_at": {"type": "string"},
                            },
                            "required": ["tool"],
                        },
                    },
                },
                "required": ["finding_id", "text", "confidence"],
            },
            source=source,
        ),
        ToolDefinition(
            name="record_module_check_report",
            description=(
                "End your round: every playbook check with its verdict and evidence; recommendation on warn and "
                "fail, reason on not_run. Warn or fail opens its agentic finding, pass resolves it."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "checks": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": MAX_CHECKS,
                        "items": {
                            "type": "object",
                            "properties": {
                                "id": {"type": "string"},
                                "verdict": {"type": "string", "enum": list(CHECK_VERDICTS)},
                                "category": {"type": "string", "enum": list(CATEGORIES)},
                                "recommendation": {"type": "string"},
                                "reason": {"type": "string"},
                                "evidence": {
                                    "type": "array",
                                    "maxItems": MAX_CHECK_EVIDENCE,
                                    "items": {
                                        "type": "object",
                                        "properties": {
                                            "tool": {"type": "string"},
                                            "arguments": {"type": "object"},
                                            "values": {"type": "object"},
                                            "read_at": {"type": "string"},
                                        },
                                        "required": ["tool"],
                                    },
                                },
                            },
                            "required": ["id", "verdict"],
                        },
                    },
                    "started_at": {"type": "string"},
                },
                "required": ["checks"],
            },
            source=source,
        ),
    ]


def build_health_module_tools(
    user_id: str, *, read_only: bool = False, run_id: str | None = None, agent_id: str | None = None
) -> tuple[list[ToolDefinition], CallableToolExecutor]:
    """The Module Analyst's platform tools acting for *user_id* (``read_only``: the reads alone),
    called by *agent_id* in the assignment run *run_id*, if any."""
    handlers: dict[str, Callable[..., Awaitable[str]]] = {
        "list_module_findings": _list_module_findings,
        "record_finding_recommendation": _record_finding_recommendation,
        "record_module_check_report": _record_module_check_report,
    }
    return build_group(
        user_id,
        _definitions(),
        handlers,
        MODULES_READ_TOOLS if read_only else None,
        run_id=run_id,
        agent_id=agent_id,
    )


def module_tools_offered() -> bool:
    """XTM One mode only (section 12.1); each call checks Platform Health's availability again."""
    from app.core.config import settings

    return bool(settings.is_xtm_one)


async def _cycle_module_target(
    db: AsyncSession, trigger_context: object, agent_id: uuid.UUID
) -> tuple[str, uuid.UUID] | None:
    """``(provider, integration id)`` of the registration the run's own Module Analyst cycle
    investigates; ``None`` for any other run (no cycle, another target type or agent, an
    inactive registration or one without its integration)."""
    from sqlalchemy import select

    from app.models.health_cycle import HealthCycleRun
    from app.models.platform_registration import PlatformRegistration
    from app.services.health.memory_steward_deps import cycle_id_of
    from app.services.health.module_analyst import MODULE_PROVIDERS

    cycle_id = cycle_id_of(trigger_context)
    if cycle_id is None:
        return None
    cycle = (
        await db.execute(
            select(HealthCycleRun.target_type, HealthCycleRun.target_id, HealthCycleRun.agent_id).where(
                HealthCycleRun.cycle_id == cycle_id
            )
        )
    ).one_or_none()
    if cycle is None or cycle.target_type != "module" or cycle.target_id is None or cycle.agent_id != agent_id:
        return None
    row = (
        await db.execute(
            select(
                PlatformRegistration.platform_identifier,
                PlatformRegistration.status,
                PlatformRegistration.integration_id,
            ).where(PlatformRegistration.id == cycle.target_id)
        )
    ).one_or_none()
    if row is None or row.platform_identifier not in MODULE_PROVIDERS:
        return None
    if row.status != "active" or row.integration_id is None:
        return None
    return str(row.platform_identifier), row.integration_id


def keep_allow_listed_reads(
    provider: str, definitions: list[ToolDefinition], executor: CallableToolExecutor
) -> list[ToolDefinition]:
    """The module tools the analyst sees: allow-listed and read (both locks of section 12.3);
    every other tool of the binding is dropped and a call to it by name refused."""
    from app.services.health.module_analyst import REFUSAL_REASON, refuse_module_tool
    from app.services.integration_read_only import withhold_tools

    return withhold_tools(
        definitions,
        executor,
        {
            d.name: REFUSAL_REASON
            for d in definitions
            if refuse_module_tool(provider, d.base_name or d.name) is not None
        },
    )


async def add_module_read_tools(
    db: AsyncSession,
    composite: Any,
    builtin_tool_ids: list[str],
    *,
    trigger_context: object,
    agent_id: uuid.UUID,
    user_id: str | None,
    run_id: str,
) -> int:
    """Wire the module reads into a Module Analyst cycle's run (section 12.3); the tools added.

    The registration the cycle investigates is read from the cycle row, never from the
    payload, and its integration (the one the registration created) is built read-only:
    write tools are withheld by the read-only binding rule, then everything outside
    ``MODULE_CHECKER_TOOLS`` is withheld too. Any other run gets nothing.
    """
    if MODULES_GROUP_ID not in builtin_tool_ids or not module_tools_offered():
        return 0
    target = await _cycle_module_target(db, trigger_context, agent_id)
    if target is None:
        return 0
    provider, integration_id = target
    from app.services.health.run_as import configured_run_as, reaches_integration, run_as_needed_reason
    from app.services.integration_read_only import READ_ONLY_CONTEXT_KEY
    from app.services.integration_tools import build_integration_tools

    # The reads act as the "Run checks as" user alone (12.3): never the run's user, the
    # service account, the seed owner or an administrator by default.
    reader = await configured_run_as(db)
    if reader is None or not await reaches_integration(db, reader, integration_id):
        logger.warning(
            "Platform Health %s checker run %s: no run-as user with access to integration %s, no module read wired",
            provider,
            run_id,
            integration_id,
        )
        composite.withhold_integrations({provider: run_as_needed_reason(provider)})
        return 0
    context = {
        "source": "health_module_analyst",
        "agent_id": str(agent_id),
        "run_id": run_id,
        "autonomous": True,
        "hitl_enabled": False,
        "user_id": str(reader),
        READ_ONLY_CONTEXT_KEY: frozenset({str(integration_id)}),
    }
    definitions, executor = await build_integration_tools(db, {provider: str(integration_id)}, context=context)
    kept = keep_allow_listed_reads(provider, definitions, executor)
    if kept:
        composite.add_tools(kept, executor)
    else:
        # Never a silent "0 tools": the checker names a platform-side defect, not the allow-list.
        logger.error(
            "Platform Health %s checker run %s: module tools not wired (integration %s gave no allow-listed read)",
            provider,
            run_id,
            integration_id,
        )
        composite.withhold_integrations({provider: MODULE_TOOLS_NOT_WIRED})
    return len(kept)


#: The reason a checker run whose module reads could not be built gives its checks (13.6).
MODULE_TOOLS_NOT_WIRED = (
    "Module tools not wired: the platform could not build this module's read tools for the run "
    "(a platform-side defect, not the checker's allow-list). Report every check as not run for this reason."
)
