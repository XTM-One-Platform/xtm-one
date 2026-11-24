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

"""The sandbox tools (#8255, PRD section 8): ``create_sandbox_copy``,
``run_sandbox``, ``get_sandbox_run``.

The built-in group ``health_sandbox`` for Platform Health's agents, and the
same functions on the platform MCP server (``/mcp/xtm``). One access rule on
every surface, read on the rows (:func:`sandbox_access_refusal`): the
Platform Health available, the caller Platform Health or an administrator, the copy's
subject - its source, else the agent whose usage it automates as the server
recorded it (``flow_lab_access.copy_subject``) - in the supervision scope and
not one of the fleet's agents, its content readable
(``consent.content_readable``: opted in, its owner not opted out) at every
call, and a run or a read only on a sandbox copy. A copy standing for nothing
is refused: the tool makes none (a prototype is the Flow Lab's
``create_sandbox_flow``, which records its basis), and a ``created_for`` a
caller passes never names one. Refusals are generic; the reason is logged.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.tools.base import CallableToolExecutor, ToolDefinition

logger = logging.getLogger(__name__)

#: Its functions belong to Platform Health's one internal group (#1943).
SANDBOX_SUITE_ID = "health_specialists"
_SOURCE = f"builtin:{SANDBOX_SUITE_ID}"
REFUSED = "Refused: the sandbox is not available to this caller for this target."
#: Recorded traces or runs one copy takes.
_MAX_SOURCES = 200


async def sandbox_access_refusal(
    db: AsyncSession, user_id: str | None, *, source_type: str | None = None, source_id: Any = None
) -> str | None:
    """Why the caller may not use the sandbox on this source (logged), ``None`` when it may."""
    from app.models.user import User
    from app.services.health.availability import health_enabled
    from app.services.health.identity import in_health_run

    if not health_enabled():
        return "Platform Health is not available"
    if not in_health_run():
        try:
            caller = await db.get(User, uuid.UUID(str(user_id))) if user_id else None
        except (TypeError, ValueError):
            caller = None
        if caller is None or not caller.is_admin or getattr(caller, "is_active", True) is False:
            return "the caller is neither Platform Health nor an administrator"
    return await subject_refusal(db, source_type, source_id)


async def subject_refusal(db: AsyncSession, source_type: str | None, source_id: Any) -> str | None:
    """Why a copy of this subject may not be made or run, whoever asks (logged), ``None`` when it may.

    Read again at every run of the copy (``runs``), an evaluation's included.
    """
    from app.models.agent import Agent
    from app.models.flow import Flow
    from app.services.health.consent import content_readable, in_supervision_scope
    from app.services.health.identity import is_health_agent

    if source_type not in ("agent", "flow") or source_id is None:
        return "the copy has no source nor an agent it automates"
    try:
        source_id = uuid.UUID(str(source_id))
    except (TypeError, ValueError):
        return "the source id is not valid"
    source = await db.get(Agent if source_type == "agent" else Flow, source_id)
    if source is None or source.is_sandbox or (source_type == "agent" and is_health_agent(source)):
        return "the source is missing, a copy, or one of the fleet"
    if not await in_supervision_scope(db, source_type, source_id):
        return "the source is outside the supervision scope"
    # A copy runs as its source's owner with live reads, and its output comes
    # back to the caller: content, so the opt-in and the owner's opt-out apply.
    target = {"agent_id": source_id} if source_type == "agent" else {"flow_id": source_id}
    if not await content_readable(db, **target, user_id=getattr(source, "user_id", None)):
        return "the source is not opted in, or its owner opted out"
    return None


def _parse_ids(value: Any) -> list[str]:
    if isinstance(value, str):
        try:
            value = json.loads(value) if value.strip().startswith("[") else [v for v in value.split(",") if v.strip()]
        except ValueError:
            return []
    return [str(v).strip() for v in value][:_MAX_SOURCES] if isinstance(value, list) else []


def build_sandbox_tools(db: AsyncSession, user_id: str | None) -> tuple[list[ToolDefinition], CallableToolExecutor]:
    """The sandbox functions acting for *user_id* (``None`` in a Platform Health run)."""
    from app.models.sandbox_copy import SandboxRun
    from app.services.health.sandbox import copies, runs

    executor = CallableToolExecutor()

    async def _refused(reason: str) -> str:
        logger.info("Sandbox tool refused for %s: %s", user_id or "Platform Health", reason)
        return REFUSED

    async def _copy_for_run(copy_id: str) -> tuple[Any, str | None]:
        from app.services.health.flow_lab_access import copy_subject

        copy = await copies.get_sandbox_copy(db, copy_id)
        if copy is None:
            return None, "no such copy"
        subject = await copy_subject(db, copy)
        refusal = await sandbox_access_refusal(
            db, user_id, source_type=subject[0] if subject else None, source_id=subject[1] if subject else None
        )
        return copy, refusal

    async def _create_sandbox_copy(
        source_type: str,
        created_for: str,
        source_id: str | None = None,
        persona: str | None = None,
        model_id: str | None = None,
        graph: Any = None,
        source_trace_ids: Any = None,
        source_run_ids: Any = None,
    ) -> str:
        from app.services.health.flow_lab_access import is_server_marker
        from app.services.health.identity import current_health_principal

        if source_type not in ("agent", "flow"):
            return "source_type is `agent` or `flow`."
        if not source_id:
            return "source_id is the agent or flow to copy; a new flow is the Flow Lab's create_sandbox_flow."
        if is_server_marker(created_for):
            return "created_for names the finding or proposal the copy serves, in your own words."
        refusal = await sandbox_access_refusal(db, user_id, source_type=source_type, source_id=source_id)
        if refusal is not None:
            return await _refused(refusal)
        if isinstance(graph, str):
            try:
                graph = json.loads(graph)
            except ValueError:
                return "`graph` must be a JSON object."
        principal = current_health_principal()
        try:
            copy = await copies.create_sandbox_copy(
                db,
                source_type=source_type,  # type: ignore[arg-type]
                source_id=source_id,
                created_for=created_for,
                created_by_agent_id=principal.agent_id if principal else None,
                overrides=copies.SandboxOverrides(
                    persona=persona, model_id=model_id, graph=graph if isinstance(graph, dict) else None
                ),
                source_trace_ids=_parse_ids(source_trace_ids),
                source_run_ids=_parse_ids(source_run_ids),
            )
        except copies.SandboxCopyError as exc:
            return str(exc)
        await db.commit()
        return json.dumps(
            {"copy_id": str(copy.id), "copied_id": str(copy.copy_id), "expires_at": copy.expires_at.isoformat()}
        )

    async def _run_sandbox(
        copy_id: str, input: str | None = None, parameters: Any = None, case_ref: str | None = None
    ) -> str:
        from app.services import orchestration_engine as engine
        from app.services.health.identity import in_health_run

        copy, refusal = await _copy_for_run(copy_id)
        if refusal is not None:
            return await _refused(refusal)
        # Only an administrator calling outside a Platform Health run is someone to
        # run an owner-less copy as; Platform Health never borrows an admin.
        requested_by = None
        if not in_health_run():
            # An administrator runs a copy as its source's owner would run it
            # (live reads with that owner's integrations): said in the audit log.
            await _audit_admin_run(copy)
            requested_by = uuid.UUID(str(user_id))
        try:
            if copy.copy_agent_id:
                if not (input or "").strip():
                    return "`input` is the message the agent copy answers."
                result = await runs.run_sandbox_agent(
                    db, copy, input_prompt=input, case_ref=case_ref, requested_by=requested_by
                )
                await db.commit()
                return json.dumps(
                    {
                        "sandbox_run_id": str(result.sandbox_run_id),
                        "output": result.output,
                        "report": _summary(result.report),
                    },
                    ensure_ascii=False,
                )
            payload = parameters if isinstance(parameters, dict) else {}
            run = await runs.start_sandbox_flow_run(
                db,
                copy,
                trigger_payload={"payload": payload, "prompt": input or ""},
                case_ref=case_ref,
                requested_by=requested_by,
            )
            await db.commit()
            await engine.notify_orchestration_wake()
            return json.dumps({"flow_run_id": str(run.id), "status": "started"})
        except runs.SandboxRunError as exc:
            return str(exc)

    async def _get_sandbox_run(run_id: str) -> str:
        try:
            parsed = uuid.UUID(str(run_id))
        except (TypeError, ValueError):
            return "No such sandbox run."
        row = (
            await db.execute(select(SandboxRun).where((SandboxRun.id == parsed) | (SandboxRun.flow_run_id == parsed)))
        ).scalar_one_or_none()
        if row is None:
            return "No such sandbox run."
        _copy, refusal = await _copy_for_run(str(row.copy_id))
        if refusal is not None:
            return await _refused(refusal)
        report = await runs.sandbox_run_report(db, sandbox_run_id=row.id)
        return json.dumps(
            {"status": row.status, "case_ref": row.case_ref, "report": _summary(report) if report else None},
            ensure_ascii=False,
        )

    async def _audit_admin_run(copy: Any) -> None:
        from app.models.user import User
        from app.services.audit import log_audit
        from app.services.health.audit_actions import SANDBOX_RUN

        caller = await db.get(User, uuid.UUID(str(user_id)))
        await log_audit(
            db,
            user=caller,
            action=SANDBOX_RUN,
            entity_type=copy.source_type,
            entity_id=copy.source_id,
            summary="Ran a sandbox copy as its source's owner",
            details={"copy_id": str(copy.id)},
        )

    executor.register("create_sandbox_copy", _create_sandbox_copy)
    executor.register("run_sandbox", _run_sandbox)
    executor.register("get_sandbox_run", _get_sandbox_run)
    return _DEFINITIONS, executor


def _summary(report: Any) -> dict:
    """The counts a caller states beside a verdict, and the calls answered."""
    data = report.to_dict()
    data["calls"] = data["calls"][:50]
    return data


_ID = {"type": "string", "format": "uuid"}
_DEFINITIONS = [
    ToolDefinition(
        name="create_sandbox_copy",
        description=(
            "Copy an agent or a flow into the sandbox: a hidden copy whose runs replay the calls recorded in the "
            "given source traces (agent) or runs (flow) and never execute a write. Changes under test go in "
            "persona / model_id (agent) or graph (flow). A new flow is the Flow Lab's create_sandbox_flow. "
            "Returns the copy id."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "source_type": {"type": "string", "enum": ["agent", "flow"]},
                "source_id": _ID,
                "created_for": {"type": "string", "description": "The finding or proposal this copy serves."},
                "persona": {"type": "string"},
                "model_id": {"type": "string"},
                "graph": {"type": "object", "description": "The flow graph the copy runs."},
                "source_trace_ids": {"type": "array", "items": _ID},
                "source_run_ids": {"type": "array", "items": _ID},
            },
            "required": ["source_type", "source_id", "created_for"],
        },
        source=_SOURCE,
    ),
    ToolDefinition(
        name="run_sandbox",
        description=(
            "Run a sandbox copy on one case. An agent copy answers `input` and the result carries its report: "
            "replayed calls, live reads, and how many writes were simulated by a model (state it beside any "
            "verdict). A flow copy starts a run with `parameters`; read it with get_sandbox_run."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "copy_id": _ID,
                "input": {"type": "string"},
                "parameters": {"type": "object"},
                "case_ref": {"type": "string"},
            },
            "required": ["copy_id"],
        },
        source=_SOURCE,
    ),
    ToolDefinition(
        name="get_sandbox_run",
        description="A sandbox run's status and report, by its id or its flow run's id.",
        input_schema={"type": "object", "properties": {"run_id": _ID}, "required": ["run_id"]},
        source=_SOURCE,
    ),
]
