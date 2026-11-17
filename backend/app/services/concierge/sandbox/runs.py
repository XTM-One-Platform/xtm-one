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

"""Running sandbox copies (#8255): the entry points the upper levels call.

- :func:`run_sandbox_agent` runs one case on an agent copy and returns its
  answer with the case's exact report;
- :func:`sandbox_case` binds a case's session around a turn the caller runs
  itself (an evaluation entry, level 4);
- :func:`start_sandbox_flow_run` starts a run of a flow copy (an experiment,
  or a shadow run on a production trigger's payload, level 7).

Every run leaves a ``sandbox_runs`` row with its report, so
:func:`simulated_write_counts` states per case how many writes were simulated.
A copy runs as its subject would (``flow_lab_access.copy_subject``: its
source, else the agent a prototype automates): for that subject's owner, so it
reaches no credential its subject does not hold, and only while the subject is
in the supervision scope and its content readable, read again at every run
(``tools.subject_refusal``, PRD 9). It never runs as an administrator the
Concierge acts for: only an administrator's own call, outside a Concierge run,
runs an owner-less copy as that administrator (``requested_by``).
"""

from __future__ import annotations

import contextlib
import logging
import uuid
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.sandbox_copy import (
    PURPOSE_EXPERIMENT,
    RUN_COMPLETED,
    RUN_FAILED,
    RUN_RUNNING,
    SandboxCopy,
    SandboxRun,
)
from app.services.concierge.sandbox.replay import RecordedCall
from app.services.concierge.sandbox.session import SandboxReport, SandboxSession, sandbox_session

logger = logging.getLogger(__name__)

#: What a sandbox run is answered with when its source has no one to run as.
NO_RUNNER = (
    "This sandbox copy has no user to run as: its source has no owner and no "
    "administrator asked for the run."
)
#: What a run of a copy whose subject is out of reach is answered with (the reason is logged).
SUBJECT_REFUSED = "This sandbox copy cannot run: its source is no longer within the Concierge's reach."


class SandboxRunError(Exception):
    """A sandbox run could not start; the message is safe to return."""


@dataclass
class SandboxAgentRun:
    output: str
    report: SandboxReport
    trace_ids: list[str] = field(default_factory=list)
    sandbox_run_id: uuid.UUID | None = None


async def runner_user_id(
    db: AsyncSession, copy: SandboxCopy, *, requested_by: uuid.UUID | None = None
) -> uuid.UUID | None:
    """Whom a copy runs as: its subject's owner, else *requested_by* (an
    administrator's own call, never the Concierge's), else nobody (``None``)."""
    from app.models.agent import Agent
    from app.models.flow import Flow
    from app.services.concierge.flow_lab_access import copy_subject
    from app.services.concierge.identity import in_concierge_run

    subject = await copy_subject(db, copy)
    owner = None
    if subject is not None:
        model = Agent if subject[0] == "agent" else Flow
        owner = await db.scalar(select(model.user_id).where(model.id == subject[1]))
    if owner is not None:
        return owner
    return None if in_concierge_run() else requested_by


async def _subject_refusal(db: AsyncSession, copy: SandboxCopy) -> None:
    """Raise when *copy*'s subject is out of reach now (``tools.subject_refusal``), whoever runs it."""
    from app.services.concierge.flow_lab_access import copy_subject
    from app.services.concierge.sandbox.tools import subject_refusal

    subject = await copy_subject(db, copy)
    refusal = await subject_refusal(db, subject[0] if subject else None, subject[1] if subject else None)
    if refusal is not None:
        logger.info("Sandbox run of copy %s refused: %s", copy.id, refusal)
        raise SandboxRunError(SUBJECT_REFUSED)


def _record(copy: SandboxCopy, report: SandboxReport, *, case_ref: str | None, status: str, **extra: Any) -> SandboxRun:
    return SandboxRun(
        copy_id=copy.id,
        case_ref=(case_ref or None) and case_ref[:255],
        purpose=PURPOSE_EXPERIMENT,
        status=status,
        report=report.to_dict(),
        simulated_writes=report.simulated_writes,
        finished_at=datetime.now(UTC),
        **extra,
    )


@contextlib.asynccontextmanager
async def sandbox_case(
    db: AsyncSession,
    copy: SandboxCopy,
    *,
    case_ref: str | None = None,
    recorded_calls: Sequence[RecordedCall] | None = None,
    user_id: str | None = None,
    model: str | None = None,
) -> AsyncIterator[SandboxSession]:
    """Bind one case's session around the caller's turn of an agent copy.

    *recorded_calls* (an evaluation entry's) replace the copy's source traces.
    Refused (:class:`SandboxRunError`) while the copy's subject is out of reach.
    On exit the case's ``sandbox_runs`` row is added with its report (failed
    when the turn raised); the caller commits.
    """
    from app.services.concierge.sandbox.session import current_sandbox_session
    from app.services.concierge.sandbox.turns import build_sandbox_session, copy_recordings

    if current_sandbox_session() is not None:
        # Refused before anything is recorded: no case ran.
        raise SandboxRunError("A sandbox case cannot start inside another sandbox run.")
    await _subject_refusal(db, copy)
    recordings = list(recorded_calls) if recorded_calls is not None else await copy_recordings(db, copy)
    session = build_sandbox_session(
        user_id=user_id or "anonymous", model=model, recorded_calls=recordings, case_id=case_ref
    )
    session.run_id = uuid.uuid4()
    status = RUN_FAILED
    try:
        with sandbox_session(session):
            yield session
        status = RUN_COMPLETED
    finally:
        db.add(_record(copy, session.report, case_ref=case_ref, status=status, id=session.run_id))
        await db.flush()


async def run_sandbox_agent(
    db: AsyncSession,
    copy: SandboxCopy,
    *,
    input_prompt: str,
    case_ref: str | None = None,
    recorded_calls: Sequence[RecordedCall] | None = None,
    run_overrides: dict[str, Any] | None = None,
    model_id: str | None = None,
    requested_by: uuid.UUID | None = None,
) -> SandboxAgentRun:
    """Run one case on an agent copy as an evaluation turn runs (no memory, no
    stored conversation, *run_overrides* overlaid like ``evaluation_overrides``).
    *requested_by*: the administrator whose own call this is (:func:`runner_user_id`).
    The caller commits."""
    from app.models.agent import Agent
    from app.services.agent_chat import run_agent_chat

    agent = await db.get(Agent, copy.copy_agent_id) if copy.copy_agent_id else None
    if agent is None or not agent.is_sandbox:
        raise SandboxRunError("This sandbox copy is not an agent.")
    # Its subject's reach is read again by ``sandbox_case``.
    runner = await runner_user_id(db, copy, requested_by=requested_by)
    if runner is None:
        # Never the company-managed tier on the Concierge's behalf: that scope
        # is opened only by the code that established the run has no account.
        raise SandboxRunError(NO_RUNNER)
    user_id = str(runner)
    eval_result: dict[str, Any] = {}
    async with sandbox_case(
        db, copy, case_ref=case_ref, recorded_calls=recorded_calls, user_id=user_id, model=model_id or agent.llm_model
    ) as session:
        output, _ = await run_agent_chat(
            db,
            user_id,
            agent,
            [],
            input_prompt,
            model_id_override=model_id,
            evaluation_mode=True,
            evaluation_overrides=run_overrides,
            # Nobody approves a sandbox call: the interceptor decides each one.
            evaluation_tool_approval=False,
            eval_result_out=eval_result,
        )
    trace_id = eval_result.get("llm_trace_id")
    return SandboxAgentRun(
        output=output or "",
        report=session.report,
        trace_ids=[trace_id] if isinstance(trace_id, str) and trace_id else [],
        sandbox_run_id=session.run_id,
    )


async def _graph_reach_refusal(
    db: AsyncSession, copy: SandboxCopy, graph: dict, source: Any, runner_id: uuid.UUID
) -> str | None:
    """Why a copy's *graph* may not run as *runner_id*, or ``None``.

    The graph is the Concierge's, not its runner's: it names nothing its
    source flow or basis agent does not (``flow_lab_access.sandbox_reach``,
    PRD 9), or, without either (a copy the server runs as a user it decided,
    a switch-over shadow), nothing its runner could not put on a flow of their
    own; then the checks of an agent's flow run (``run_flow``).
    """
    from app.api.v1.helpers import agent_tool_admin_bypass
    from app.models.user import User
    from app.services.concierge.flow_lab_access import copy_subject, node_config_ids, sandbox_reach
    from app.services.flow_references import (
        check_integration_references,
        check_source_references,
        check_three_tier_references,
        check_tool_references,
    )

    user = await db.get(User, runner_id)
    if user is None:
        return NO_RUNNER
    subject = await copy_subject(db, copy) if source is None else None
    basis_agent_id = subject[1] if subject is not None and subject[0] == "agent" else None
    issues: list[dict[str, Any]] = []
    if source is not None or basis_agent_id is not None:
        reach = await sandbox_reach(db, source_flow=source, basis_agent_id=basis_agent_id)
        if node_config_ids(graph) - reach:
            return "This sandbox flow names agents, integrations or other objects its source does not use."
    else:
        issues += await check_integration_references(db, user, graph=graph, previous_graph=None, flow_owner_id=user.id)
        issues += await check_source_references(db, user, graph=graph, previous_graph=None, flow_owner_id=user.id)
    issues += await check_three_tier_references(db, user, graph=graph)
    issues += await check_tool_references(
        db,
        user,
        graph=graph,
        flow_owner_id=user.id,
        is_company_managed=bool(source is not None and source.is_company_managed),
        admin_bypass=agent_tool_admin_bypass(user),
        check_co_editors=False,
    )
    if any(issue.get("severity", "error") == "error" for issue in issues):
        return "This sandbox flow names agents, integrations or tools the user it runs as may not use."
    return None


async def start_sandbox_flow_run(
    db: AsyncSession,
    copy: SandboxCopy,
    *,
    trigger_type: str = "manual",
    trigger_payload: dict[str, Any] | None = None,
    case_ref: str | None = None,
    purpose: Literal["experiment", "shadow"] = "experiment",
    run_as_user_id: uuid.UUID | None = None,
    entry_id: uuid.UUID | None = None,
    requested_by: uuid.UUID | None = None,
) -> Any:
    """Start a run of a flow copy; its bursts are intercepted by the walker.

    *run_as_user_id* is whom the run acts as when the server decided it (a
    shadow run acts as the assignment it shadows acts, and was checked
    against it); never taken from a tool argument. Without it, the copy's
    subject must be in reach (``tools.subject_refusal``) and the run acts as
    :func:`runner_user_id` (*requested_by*: the administrator whose own call
    this is). *entry_id*: the
    evaluation entry the run answers, whose recorded calls the bursts replay
    (``flows.flow_burst_session``). Returns the ``FlowRun`` (flushed). The
    caller commits, then calls ``orchestration_engine.notify_orchestration_wake()``.
    """
    from app.models.flow import Flow
    from app.services.assignment_pause import FLOW_OWNER_DEACTIVATED_MESSAGE, is_paused_by_deactivated_owner
    from app.services.concierge.sandbox.flows import ENTRY_KEY, SANDBOX_RUN_SOURCE
    from app.services.flow_runner import create_flow_run

    flow = await db.get(Flow, copy.copy_flow_id) if copy.copy_flow_id else None
    if flow is None or not flow.is_sandbox:
        raise SandboxRunError("This sandbox copy is not a flow.")
    # The copy runs as its source would, so not while its source is paused by
    # its owner's deactivation (#4323): that owner is whom it would run as.
    source = await db.get(Flow, copy.source_id) if copy.source_id else None
    if source is not None and await is_paused_by_deactivated_owner(db, source):
        raise SandboxRunError(FLOW_OWNER_DEACTIVATED_MESSAGE)
    if run_as_user_id is None:
        await _subject_refusal(db, copy)
    runner = run_as_user_id or await runner_user_id(db, copy, requested_by=requested_by)
    if runner is None:
        raise SandboxRunError(NO_RUNNER)
    refusal = await _graph_reach_refusal(db, copy, flow.graph or {}, source, runner)
    if refusal is not None:
        raise SandboxRunError(refusal)
    run = await create_flow_run(
        db,
        flow,
        trigger_type=trigger_type,
        trigger_payload=trigger_payload or {},
        trigger_context={
            "source": SANDBOX_RUN_SOURCE,
            "purpose": purpose,
            **({ENTRY_KEY: str(entry_id)} if entry_id else {}),
        },
        user_id=runner,
    )
    db.add(
        SandboxRun(
            copy_id=copy.id,
            case_ref=(case_ref or None) and case_ref[:255],
            purpose=purpose,
            status=RUN_RUNNING,
            flow_run_id=run.id,
            report=SandboxReport(case_id=case_ref).to_dict(),
        )
    )
    await db.flush()
    return run


async def sandbox_run_report(
    db: AsyncSession, *, sandbox_run_id: Any = None, flow_run_id: Any = None
) -> SandboxReport | None:
    """A sandbox run's report, by its id or its flow run's."""
    try:
        if sandbox_run_id is not None:
            clause = SandboxRun.id == uuid.UUID(str(sandbox_run_id))
        else:
            clause = SandboxRun.flow_run_id == uuid.UUID(str(flow_run_id))
    except (TypeError, ValueError):
        return None
    row = (await db.execute(select(SandboxRun).where(clause))).scalar_one_or_none()
    return SandboxReport.from_dict(row.report) if row is not None else None


async def simulated_write_counts(
    db: AsyncSession, *, copy_id: Any, since: datetime | None = None
) -> dict[str | None, int]:
    """``{case_ref: simulated writes}`` over a copy's runs (since *since*)."""
    query = select(SandboxRun.case_ref, func.sum(SandboxRun.simulated_writes)).where(
        SandboxRun.copy_id == uuid.UUID(str(copy_id))
    )
    if since is not None:
        query = query.where(SandboxRun.created_at >= since)
    rows = await db.execute(query.group_by(SandboxRun.case_ref))
    return {case_ref: int(total or 0) for case_ref, total in rows.all()}
