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

"""``until`` resolution for the ``wait`` primitive.

The timer itself — policy, clamping, the interruptible sleep and the tool schema
— lives in :mod:`app.tools.wait_policy` and is executed inside the agentic loop,
not through the tool executor: the bounds a wait must respect (the per-call and
per-run caps, the run's wall-clock deadline) and the events it must react to
(the user's cancel, the user's skip) are all the loop's own state.

What the loop cannot know is *what* the agent is waiting for.  This module
supplies that half: a condition resolver bound to one user, which reports whether
the work named in ``until`` has reached a terminal state - the background tasks
the agent spawned, the assignment and flow runs it started, the
Video Studio jobs it queued and the work it delegated to an A2A partner: an id
that is not a task is looked up as a run the person may see, then as a
recording, AI shot, AI track or render they may read, then as a partner task
this agent delegated for this person, so a wait on work that already finished
ends instead of sleeping its whole timer.  That is what turns a requested five-minute pause into a
forty-second one when the delegated work finishes in forty seconds.
"""

from __future__ import annotations

from app.core.utils import format_exception_message, safe_uuid
from app.services.sub_agent_result_text import clipped
from app.tools.wait_policy import WAIT_TERMINAL_STATUSES, WaitCondition

from ._shared import logger

#: A run's terminal status in the wait's words (``WAIT_TERMINAL_STATUSES``);
#: every other status is reported as it is and keeps the wait going.
_RUN_TERMINAL_AS_WAIT = {
    "completed": "completed",
    "failed": "failed",
    "cancelled": "cancelled",
    "skipped": "cancelled",
}

#: How much of a finished run's summary the wait result carries.
_EXTRACT_MAX_CHARS = 1200

#: A Video Studio job's state (``jobs.job_state``) in the wait's words.
_JOB_STATE_AS_WAIT = {"queued": "queued", "running": "running", "ready": "completed", "failed": "failed"}

#: An outbound A2A delegation's stored outcome in the wait's words. A row with
#: no outcome is outstanding (``running``); an outcome missing here (timeout,
#: unreachable, a refusal) is ``failed``. A partner that asked a question is
#: stored ``completed``: its answer is ready to collect.
_DELEGATION_OUTCOME_AS_WAIT = {"completed": "completed", "cancelled": "cancelled"}


async def watched_run_statuses(run_ids: list[str], user_id: str) -> dict[str, dict]:
    """The assignment and flow runs among *run_ids* that *user_id* may watch.

    ``{id: {"status", "name", "summary", "started_at"}}``, keyed by the id as
    given, with a terminal status in the wait's words and the ISO start the
    wait card counts from. An assignment run is watched under
    the agent tools' run rule (own + company-managed, every run for an admin
    unless strict admin access) plus the runs the person started; a flow run
    under its flow's (``flow_access.get_visible_run``). Any other id is left
    out, so it stays pending like an unknown task id.
    """
    from fastapi import HTTPException
    from sqlalchemy import or_, select

    from app.api.v1.helpers import agent_tool_admin_bypass, run_visible_filter
    from app.core.database import async_session
    from app.models.assignment import Assignment, AssignmentRun
    from app.models.flow import Flow
    from app.models.flow_run import FlowRun
    from app.models.user import User
    from app.services.flow_access import get_visible_run

    uid = safe_uuid(user_id)
    given = {parsed: rid for rid in run_ids if (parsed := safe_uuid(rid)) is not None}
    if uid is None or not given:
        return {}

    def _entry(status: str | None, name: str | None, summary: str | None, error: str | None, started_at) -> dict:
        state = str(status or "")
        return {
            "status": _RUN_TERMINAL_AS_WAIT.get(state, state),
            "name": name,
            "summary": (summary or error or "").strip(),
            "started_at": started_at.isoformat() if started_at else None,
        }

    out: dict[str, dict] = {}
    async with async_session() as db:
        user = await db.get(User, uid)
        if user is None:
            return {}
        admin = agent_tool_admin_bypass(user)
        query = (
            select(
                AssignmentRun.id,
                AssignmentRun.status,
                AssignmentRun.result_summary,
                AssignmentRun.error,
                Assignment.name,
                AssignmentRun.started_at,
            )
            .outerjoin(Assignment, Assignment.id == AssignmentRun.assignment_id)
            .where(AssignmentRun.id.in_(list(given)))
        )
        if not admin:
            query = query.where(
                or_(run_visible_filter(uid), AssignmentRun.triggered_by_user_id == uid)
            )
        for run_id, status, summary, error, name, started_at in (await db.execute(query)).all():
            out[given[run_id]] = _entry(status, name, summary, error, started_at)

        rest = [parsed for parsed, rid in given.items() if rid not in out]
        if rest:
            rows = await db.execute(
                select(
                    FlowRun.id, FlowRun.status, FlowRun.result_summary, FlowRun.error, Flow.name, FlowRun.started_at
                )
                .outerjoin(Flow, Flow.id == FlowRun.flow_id)
                .where(FlowRun.id.in_(rest))
            )
            for run_id, status, summary, error, name, started_at in rows.all():
                try:
                    await get_visible_run(
                        db, user, str(run_id), is_admin=agent_tool_admin_bypass(user)
                    )
                except HTTPException:
                    continue
                out[given[run_id]] = _entry(status, name, summary, error, started_at)
    return out


async def _runs_or_none(run_ids: list[str], user_id: str) -> dict[str, dict]:
    """:func:`watched_run_statuses`, an empty answer when it cannot be read."""
    if not run_ids:
        return {}
    try:
        return await watched_run_statuses(run_ids, user_id)
    except Exception as exc:
        logger.warning(
            "wait: could not read run statuses: %s", format_exception_message(exc)
        )
        return {}


async def watched_job_statuses(
    job_ids: list[str], user_id: str, *, told_to: str | None = None
) -> dict[str, dict]:
    """The Video Studio jobs among *job_ids* that *user_id* may read.

    ``{id: {"kind", "status", "name", "noun", "reason", "started_at"}}``,
    keyed by the id as given: a recording, an AI shot, an AI track or a render
    (a scene preview included), read under ``jobs.may_read_job`` - the rule
    ``get_video_jobs`` reports them under - with its state in the wait's words
    (a cancelled job reads ``cancelled``). *told_to* is the conversation the
    wait reports to: the jobs it reads ended are no longer owed to it
    (``jobs.mark_told``). Any other id is left out.
    """
    from app.models.video_studio import JOB_CANCELLED
    from app.services.video_studio import jobs

    out: dict[str, dict] = {}
    by_kind: dict[str, list] = {}
    for jid, (kind, row, name) in (await jobs.watched_jobs(job_ids, user_id)).items():
        state = jobs.job_state(kind, row)
        started_at = getattr(row, "started_at", None)
        out[jid] = {
            "kind": kind,
            "status": "cancelled" if row.status == JOB_CANCELLED else _JOB_STATE_AS_WAIT[state.status],
            "name": name,
            "noun": jobs.noun(kind, row),
            "reason": state.reason,
            "started_at": started_at.isoformat() if started_at else None,
        }
        by_kind.setdefault(kind, []).append(row)
    conversation = safe_uuid(told_to) if told_to else None
    if conversation is not None:
        try:
            for kind, rows in by_kind.items():
                await jobs.mark_told(kind, rows, conversation)
        except Exception as exc:
            logger.warning("wait: could not mark the ended jobs told: %s", format_exception_message(exc))
    return out


async def _jobs_or_none(job_ids: list[str], user_id: str, *, told_to: str | None = None) -> dict[str, dict]:
    """:func:`watched_job_statuses`, an empty answer when it cannot be read."""
    if not job_ids:
        return {}
    try:
        return await watched_job_statuses(job_ids, user_id, told_to=told_to)
    except Exception as exc:
        logger.warning("wait: could not read Video Studio job statuses: %s", format_exception_message(exc))
        return {}


async def watched_delegation_statuses(
    task_ids: list[str], user_id: str, agent_id: str | None
) -> dict[str, dict]:
    """The outbound A2A delegations among *task_ids* that *agent_id* sent for *user_id*.

    ``{id: {"status", "name", "started_at"}}``, keyed by the partner task id
    the delegation tool handed back (``collect_task_id`` / ``continue_task_id``),
    with the partner's name as the card shows it and the submission time. Scoped
    to the same agent and the same user, the rule a collect's ownership check
    (``delegation._may_continue``) applies: another user's or another agent's
    delegation is left out, so it stays pending like an unknown task id. Where
    one task id has several rows, an outstanding one wins, then the newest.
    """
    from sqlalchemy import select

    from app.a2a.client.tool import _card_text
    from app.core.database import async_session
    from app.models.a2a import A2AClientTarget, A2ADelegation
    from app.models.integration import Integration

    uid, aid = safe_uuid(user_id), safe_uuid(agent_id) if agent_id else None
    ids = [tid for tid in dict.fromkeys(task_ids) if tid]
    if uid is None or aid is None or not ids:
        return {}
    query = (
        select(
            A2ADelegation.partner_task_id,
            A2ADelegation.outcome,
            A2ADelegation.submitted_at,
            A2AClientTarget.declared_name,
            Integration.name,
        )
        .outerjoin(A2AClientTarget, A2AClientTarget.id == A2ADelegation.target_id)
        .outerjoin(Integration, Integration.id == A2ADelegation.integration_id)
        .where(
            A2ADelegation.partner_task_id.in_(ids),
            A2ADelegation.user_id == uid,
            A2ADelegation.agent_id == aid,
        )
        .order_by(A2ADelegation.outcome.is_(None).desc(), A2ADelegation.submitted_at.desc())
    )
    out: dict[str, dict] = {}
    async with async_session() as db:
        for task_id, outcome, submitted_at, declared, integration_name in (await db.execute(query)).all():
            if task_id in out:
                continue
            out[task_id] = {
                "status": "running" if outcome is None else _DELEGATION_OUTCOME_AS_WAIT.get(outcome, "failed"),
                "name": _card_text(declared) or _card_text(integration_name) or None,
                "started_at": submitted_at.isoformat() if submitted_at else None,
            }
    return out


async def _delegations_or_none(task_ids: list[str], user_id: str, agent_id: str | None) -> dict[str, dict]:
    """:func:`watched_delegation_statuses`, an empty answer when it cannot be read."""
    if not task_ids or not agent_id:
        return {}
    try:
        return await watched_delegation_statuses(task_ids, user_id, agent_id=agent_id)
    except Exception as exc:
        logger.warning("wait: could not read A2A delegation statuses: %s", format_exception_message(exc))
        return {}


def _delegation_extract(task_id: str, delegation: dict) -> str:
    """What a concluded delegation says in the wait result: never the partner's text, only the collect to make."""
    partner = f"'{delegation['name']}'" if delegation["name"] else "the partner"
    return (
        f"Delegation to {partner} ({delegation['status']}). Its answer is not shown here: call that "
        f"partner's delegation tool with continue_task_id='{task_id}' and no message to collect it."
    )


def _job_extract(job_id: str, job: dict) -> str:
    """What a finished Video Studio job says in the wait result, and the call that reports it in full."""
    from app.services.video_studio.jobs import JOB_ID_ARGUMENTS

    noun = job["noun"]
    label = f"{noun[:1].upper()}{noun[1:]}" + (f" '{job['name']}'" if job["name"] else "")
    reason = str(job["reason"] or "").strip().rstrip(".") if job["status"] == "failed" else ""
    if len(reason) > _EXTRACT_MAX_CHARS:
        reason = reason[:_EXTRACT_MAX_CHARS].rstrip() + "..."
    said = f"{label} ({job['status']}){f': {reason}' if reason else ''}."
    return f"{said} get_video_jobs({JOB_ID_ARGUMENTS[job['kind']]}=['{job_id}']) reports it in full."


def make_task_condition(
    user_id: str | None, *, conversation_id: str | None = None, agent_id: str | None = None
) -> WaitCondition | None:
    """Build the ``until`` resolver for *user_id*, or ``None`` when unavailable.

    Returning ``None`` (no user in context — e.g. an anonymous embed turn) is
    deliberate: the loop then states in the tool result that ``until`` was
    ignored rather than silently waiting the full duration on a condition it
    can never observe.

    *conversation_id* is the chat the wait runs in: a run or a Video Studio job
    it reads ended is no longer owed to that conversation
    (``assignment_run_wake.mark_told``; ``jobs.mark_told``), so
    the conversation is not told twice.

    *agent_id* is the agent that waits: the A2A delegations it sent for
    *user_id* are watchable by their partner task id. Without it (a context
    with no agent, which cannot delegate) no id is read as a delegation.
    """
    if not user_id:
        return None

    async def _condition(task_ids: list[str]) -> tuple[bool, dict[str, str]]:
        """``(all_terminal, {task_id: status})`` for the watched tasks.

        A status read that fails is non-fatal: the wait keeps running on its
        timer instead of aborting a legitimate pause because of one flaky query.
        An id that is *not found* (unknown / other-user) stays pending —
        treating unknown as terminal made ``until=`` return "already done"
        on a typo and the parent exited while real work was still running.
        """
        from app.services.sub_agent import _STATUS_TERMINAL, get_task_statuses

        try:
            statuses = await get_task_statuses(task_ids, user_id)
        except Exception as exc:  # pragma: no cover - defensive
            # ``format_exception_message`` rather than a raw ``%s``: several
            # exception types (asyncio.TimeoutError among them) stringify to an
            # empty string, which would log a diagnosis-free line for the one
            # failure this handler exists to explain.
            logger.warning(
                "wait: could not read task statuses: %s",
                format_exception_message(exc),
            )
            return False, {}

        seen = {
            tid: str(status.get("status"))
            for tid, status in statuses.items()
            if status is not None
        }
        # An id that is no task may be a run the agent started.
        runs = await _runs_or_none([tid for tid in task_ids if tid not in seen], user_id)
        seen.update({tid: run["status"] for tid, run in runs.items()})
        ended_runs = [tid for tid, run in runs.items() if run["status"] in WAIT_TERMINAL_STATUSES]
        if ended_runs and conversation_id:
            from app.services.assignment_run_wake import mark_told

            await mark_told(ended_runs, conversation_id)
        # Nor a run: a Video Studio job the agent queued.
        video_jobs = await _jobs_or_none(
            [tid for tid in task_ids if tid not in seen], user_id, told_to=conversation_id
        )
        seen.update({tid: job["status"] for tid, job in video_jobs.items()})
        # Nor a job: a partner task the agent delegated over A2A.
        delegations = await _delegations_or_none([tid for tid in task_ids if tid not in seen], user_id, agent_id)
        seen.update({tid: d["status"] for tid, d in delegations.items()})
        unknown = [tid for tid in task_ids if tid not in seen]
        pending = [
            tid for tid, state in seen.items() if state not in _STATUS_TERMINAL
        ]
        pending.extend(unknown)
        reported = {**seen, **{tid: "unknown" for tid in unknown}}
        return not pending, reported

    async def _extracts(task_ids: list[str]) -> dict[str, str]:
        """Short result extracts for finished tasks, for the wait tool result."""
        from app.services.sub_agent import get_task_result

        out: dict[str, str] = {}
        # A finished run says what it did in its summary (or its error).
        for tid, run in (await _runs_or_none(task_ids, user_id)).items():
            body = run["summary"]
            if run["status"] not in WAIT_TERMINAL_STATUSES or not body:
                continue
            if len(body) > _EXTRACT_MAX_CHARS:
                body = body[:_EXTRACT_MAX_CHARS].rstrip() + "..."
            label = f"Run '{run['name']}'" if run["name"] else "Run"
            out[tid] = f"{label} ({run['status']}): {body}"
        for tid in task_ids:
            if tid in out:
                continue
            try:
                payload = await get_task_result(None, tid, user_id)  # type: ignore[arg-type]
            except Exception:
                logger.debug(
                    "wait: could not read result extract for %s",
                    tid[:12],
                    exc_info=True,
                )
                continue
            if not payload:
                continue
            body = str(payload.get("result") or payload.get("error") or "").strip()
            # ``get_full_task_result`` hands over the files of a completed
            # task AND the partial output of a failed one; the hint
            # below is only true for those (mirrors
            # ``handover._FILE_HANDOVER_STATUSES``).
            file_ids = (
                payload.get("result_file_ids")
                if payload.get("status") in ("completed", "failed")
                else None
            )
            file_count = len(file_ids) if isinstance(file_ids, list) else 0
            if not body and not file_count:
                continue
            # The extract names the cut and the call that reads the whole
            # answer (#1394): a bare "..." read as the end of it.
            body = clipped(body, _EXTRACT_MAX_CHARS, read_call=f'get_full_task_result(task_id="{tid}")')
            if file_count:
                # The wait runs inside the loop, not through the tool
                # executor, so it cannot hand refs to the FileContext itself:
                # say the files exist and which call attaches them.
                body = (
                    f"{body}\n[{file_count} file(s) produced - call "
                    f"get_full_task_result({tid}) to attach them to your "
                    "context, then read them with read_file_content]"
                ).strip()
            out[tid] = body
        rest = [tid for tid in task_ids if tid not in out]
        for tid, job in (await _jobs_or_none(rest, user_id)).items():
            if job["status"] in WAIT_TERMINAL_STATUSES:
                out[tid] = _job_extract(tid, job)
        rest = [tid for tid in task_ids if tid not in out]
        for tid, delegation in (await _delegations_or_none(rest, user_id, agent_id)).items():
            if delegation["status"] in WAIT_TERMINAL_STATUSES:
                out[tid] = _delegation_extract(tid, delegation)
        return out

    async def _labels(task_ids: list[str]) -> dict[str, dict[str, str | None]]:
        """``{task_id: {agent_name, task_description}}`` for the park message.

        The loop names still-running tasks to the user by agent + description;
        the transcript usually carries both, and this is the fallback
        for a task whose spawn result was pruned or predates the loaded
        history. A run is named by its assignment or flow, a Video Studio job
        by its name and kind. A read failure is non-fatal here too — the label
        is cosmetic and the caller degrades to a count.
        """
        from app.services.sub_agent import get_task_labels

        try:
            labels = await get_task_labels(task_ids, user_id)
        except Exception as exc:
            logger.warning(
                "wait: could not read task labels: %s",
                format_exception_message(exc),
            )
            labels = {}
        runs = await _runs_or_none([tid for tid in task_ids if tid not in labels], user_id)
        for tid, run in runs.items():
            labels[tid] = {"agent_name": run["name"], "task_description": "run"}
        for tid, job in (await _jobs_or_none([tid for tid in task_ids if tid not in labels], user_id)).items():
            labels[tid] = {"agent_name": job["name"], "task_description": job["noun"]}
        rest = [tid for tid in task_ids if tid not in labels]
        for tid, delegation in (await _delegations_or_none(rest, user_id, agent_id)).items():
            labels[tid] = {"agent_name": delegation["name"], "task_description": "partner delegation"}
        return labels

    async def _details(task_ids: list[str]) -> list[dict[str, str | None]]:
        """What the wait card shows of each watched id: kind, label, status, start.

        One entry per id, in order; an id neither a task, a run, a Video
        Studio job nor an A2A delegation the user may watch reads ``unknown``. Cosmetic: a
        failed read yields what was read.
        """
        from app.services.sub_agent import get_task_labels, get_task_statuses

        try:
            statuses = await get_task_statuses(task_ids, user_id)
        except Exception as exc:
            logger.debug("wait: could not read the watched tasks: %s", format_exception_message(exc))
            statuses = {}
        tasks = [tid for tid, status in statuses.items() if status is not None]
        try:
            labels = await get_task_labels(tasks, user_id) if tasks else {}
        except Exception as exc:
            logger.debug("wait: could not read the watched tasks' labels: %s", format_exception_message(exc))
            labels = {}
        runs = await _runs_or_none([tid for tid in task_ids if statuses.get(tid) is None], user_id)
        video_jobs = await _jobs_or_none(
            [tid for tid in task_ids if statuses.get(tid) is None and tid not in runs], user_id
        )
        delegations = await _delegations_or_none(
            [tid for tid in task_ids if statuses.get(tid) is None and tid not in runs and tid not in video_jobs],
            user_id,
            agent_id,
        )
        items: list[dict[str, str | None]] = []
        for tid in task_ids:
            task = statuses.get(tid)
            if task is not None:
                label = labels.get(tid) or {}
                items.append(
                    {
                        "id": tid,
                        "kind": "task",
                        "label": label.get("agent_name") or task.get("task_description"),
                        "status": str(task.get("status")),
                        "started_at": task.get("started_at"),
                    }
                )
            elif tid in runs:
                run = runs[tid]
                items.append(
                    {"id": tid, "kind": "run", "label": run["name"], "status": run["status"], "started_at": run["started_at"]}
                )
            elif tid in video_jobs:
                job = video_jobs[tid]
                items.append(
                    {"id": tid, "kind": "job", "label": job["name"], "status": job["status"], "started_at": job["started_at"]}
                )
            elif tid in delegations:
                delegation = delegations[tid]
                items.append(
                    {
                        "id": tid,
                        "kind": "a2a",
                        "label": delegation["name"],
                        "status": delegation["status"],
                        "started_at": delegation["started_at"],
                    }
                )
            else:
                items.append({"id": tid, "kind": None, "label": None, "status": "unknown", "started_at": None})
        return items

    _condition.extracts = _extracts  # type: ignore[attr-defined]
    _condition.labels = _labels  # type: ignore[attr-defined]
    _condition.details = _details  # type: ignore[attr-defined]
    return _condition
