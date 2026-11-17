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

"""``computer_use`` built-in tools – drive native OS-control Runners.

The agent-facing half of XTM One Runners. These tools let an agent capture a
**genuine full desktop** (the audit-evidence use case — real OS clock, taskbar,
signed-in identity, browser chrome), drive a real authenticated local browser,
synthesize mouse/keyboard input, run shell commands, and read/write files on an
enrolled machine.

Every call funnels through the same gate:

1. resolve which runner to target (agent selector -> assignment refinement ->
   user visibility -> online), requiring an explicit ``runner_id`` when more
   than one machine is online (the fleet-safety rule the user asked for);
2. authorize the command against the runner's ``permission_mode`` + capability
   allow-list (:func:`app.services.runners.authorize_command`);
3. dispatch it over the event-driven transport and await the terminal result;
4. surface a binary result (a screenshot, a file read) as an inline chat image
   / downloadable file via the shared ``FileContext`` — identical to how
   ``generate_image`` and the ``browser`` tool render.

Registered when the agent has the ``computer_use`` built-in tool enabled and
``settings.runners_enabled`` is on; the tools themselves report cleanly when no
runner is online rather than vanishing from the tool list.

**XTM One Desktop.** A chat turn sent from the Desktop app names the
runner the app embeds (``local_runner``): that machine is then the DEFAULT
target of every tool - a call without ``runner_id`` acts on the machine the
user is sitting at, whatever the agent's runner selector says (the user chose
their own machine by chatting from it; the selector scopes fleet targeting).
The agent learns which machine "this machine" is from the tool descriptions,
never from a system-prompt block. With ``interactive_approval`` a shell command
the runner's policy holds for approval is offered INLINE - a
``runner_approval_required`` progress frame the chat renders as an approval
card - and waited for (bounded), so the decision and the command's output land
in the same turn. Unattended runs (assignments, headless fleets) keep the
queue-and-return behaviour.

**Machine activity.** Every command that goes through the funnel is
also reported to the chat as a display record (``services/runner_activity.py``)
- the card the transcript shows while it runs and keeps with the message: the
command line and its output, the file, the folder, the capture. The prose the
model reads is unchanged; outside a chat turn the report is a no-op.
"""

from __future__ import annotations

import asyncio
import functools
import time
import uuid
from typing import TYPE_CHECKING

from app.core.config import settings
from app.core.database import async_session
from app.services.builtin_tool_aliases import RENAMED_BUILTIN_FUNCTIONS
from app.services.runner_activity import (
    STATUS_AWAITING_APPROVAL,
    STATUS_BUSY,
    STATUS_FAILED,
    STATUS_NOT_APPROVED,
    STATUS_REFUSED,
    STATUS_REJECTED,
    STATUS_UNAVAILABLE,
    RunnerActivity,
)
from app.services.runner_transport import (
    RUNNER_UPLOAD_RETRY_BUDGET_SECONDS,
    await_result,
    cancel_held_command,
    default_result_wait_seconds,
    dispatch_command,
    give_up_on_command,
    load_command_status,
    runner_execution_ceiling_seconds,
)
from app.services.runners import (
    INLINE_TEXT_LIMIT_KEY,
    MAX_INLINE_BYTES_KEY,
    RUNNER_INLINE_TEXT_BYTES,
    SHELL_JOBS_MIN_RUNNER_VERSION,
    authorize_command,
    command_requires_approval,
    delivery_payload,
    effective_runner_capabilities,
    is_runner_online,
    runner_lacks_shell_jobs,
)
from app.tools.base import CallableToolExecutor, ToolDefinition
from app.tools.tool_events import (
    RUNNER_APPROVAL_REQUIRED_EVENT,
    RUNNER_APPROVAL_RESOLVED_EVENT,
    current_tool_deadline,
    emit_tool_event,
)

from ._shared import logger

if TYPE_CHECKING:
    from app.models.runner import Runner
    from app.tools.base import FileContext, ToolResult

#: How long an interactive (Desktop chat) turn waits for the user to approve or
#: reject a held command before giving up on the inline decision. Bounded well
#: below the loop's per-tool ceiling (``TOOL_EXECUTION_TIMEOUT_SECONDS`` = 300 s)
#: so that approval + execution + result upload retries
#: (``runner_report_window_seconds``) + the answer reserve still fit inside one
#: tool call; a decision that comes later is not lost - the command stays
#: queued and runs from the Desktop page's approval list.
DESKTOP_APPROVAL_WAIT_SECONDS = 90

#: What a call keeps of its time after the result wait - recording the give-up,
#: loading a file result, reporting the card. The wait stops this long before
#: the agent loop would cancel the call, so the agent reads what became
#: of the command rather than the loop's bare timeout.
TOOL_ANSWER_RESERVE_SECONDS = 5

#: Where a human manages machines, approvals and the Desktop app itself.
DESKTOP_PAGE_HINT = "the Desktop page (sidebar > XTM One Desktop)"

#: Poll cadence while waiting for an inline approval decision. The approve /
#: reject routes flip the row directly (no result-channel publish), so a short
#: DB re-read is the wake mechanism here.
_APPROVAL_POLL_SECONDS = 1.0


def _as_uuid(value: str | None) -> uuid.UUID | None:
    if not value:
        return None
    try:
        return uuid.UUID(value)
    except (ValueError, AttributeError, TypeError):
        return None


#: The actions of the ``shell_jobs`` tool (the runner's ``shell_job`` command).
SHELL_JOB_ACTIONS: tuple[str, ...] = ("list", "tail", "stop")


def approval_summary(kind: str, payload: dict | None) -> str:
    """One line naming what a held command would do, for the approval card."""
    payload = payload or {}
    if kind == "run_shell":
        command = str(payload.get("command") or "").strip()
        cwd = str(payload.get("cwd") or "").strip()
        return f"{command}  (in {cwd})" if cwd else command
    if kind in ("read_file", "write_file", "list_dir"):
        return str(payload.get("path") or "")
    if kind == "browser_control":
        action = str(payload.get("action") or "")
        target = str(payload.get("url") or payload.get("tab_id") or "")
        return f"{action} {target}".strip()
    if kind == "computer_action":
        return str(payload.get("action") or "")
    return kind


async def await_approval_decision(command_id: str, timeout: float) -> str:
    """Block until a held command leaves ``pending_approval`` or *timeout* lapses.

    Returns the command's status at the end of the wait: ``pending`` /
    ``dispatched`` / a terminal status once a human decided, or
    ``pending_approval`` when nobody did in time (the caller then leaves the
    command queued for the Desktop page). ``missing`` when the row is gone.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    status = await load_command_status(command_id)
    while status == "pending_approval" and loop.time() < deadline:
        await asyncio.sleep(_APPROVAL_POLL_SECONDS)
        status = await load_command_status(command_id)
    return status or "missing"


#: ``status`` of the ``runner_approval_resolved`` frame when the chat turn
#: ended (user Stop, wall-clock deadline, tool ceiling) before anyone decided:
#: the held command is cancelled with it, never left for a later approval.
APPROVAL_ABANDONED_STATUS = "abandoned"

#: What the cancelled row says happened.
APPROVAL_ABANDONED_REASON = "Chat turn ended before the command was approved"

#: The machine-activity status of a held command whose inline wait ended
#: without running it, keyed by the row's status at the end of the wait.
_DECISION_STATUSES = {
    "pending_approval": STATUS_NOT_APPROVED,
    "cancelled": STATUS_REJECTED,
    "missing": STATUS_FAILED,
}


async def abandon_inline_approval(command_id: str) -> None:
    """Close an inline approval whose turn is being cancelled.

    Cancels the held row (guarded - a decision that already landed stands)
    and tells the chat the card is over. Best effort on both counts and never
    raises: this runs from a ``CancelledError`` handler.
    """
    from app.core.utils import format_exception_message

    try:
        await cancel_held_command(command_id, APPROVAL_ABANDONED_REASON)
    except Exception as exc:
        logger.warning(
            "held command %s could not be cancelled after the turn ended: %s",
            command_id,
            format_exception_message(exc),
        )
    try:
        await emit_tool_event(
            RUNNER_APPROVAL_RESOLVED_EVENT,
            command_id=command_id,
            status=APPROVAL_ABANDONED_STATUS,
        )
    except Exception:
        logger.debug("approval resolution frame for %s not delivered", command_id)


#: What a command still queued says when its chat turn ended mid-wait.
TURN_ENDED_REASON = "Chat turn ended before the command's result came back"


async def give_up_quietly(command_id: str, reason: str) -> None:
    """Record a give-up from a ``CancelledError`` handler: never raises."""
    from app.core.utils import format_exception_message

    try:
        await give_up_on_command(command_id, reason, undelivered_status="cancelled")
    except Exception as exc:
        logger.warning(
            "runner command %s could not be given up after the turn ended: %s",
            command_id,
            format_exception_message(exc),
        )


def _answer_deadline() -> float | None:
    """When the result wait must end for the call to answer before the agent
    loop cancels it (``time.monotonic()``); ``None`` outside the loop."""
    deadline = current_tool_deadline()
    return None if deadline is None else deadline - TOOL_ANSWER_RESERVE_SECONDS


#: The end of the reserve, kept for the reply (the outcome, the card) when
#: recording a give-up runs long.
_REPLY_SECONDS = 1.0

#: Give-ups still being recorded after their call answered, held until done:
#: the event loop keeps only a weak reference to a task.
_GIVE_UPS_IN_FLIGHT: set[asyncio.Future] = set()


def _log_failed_give_up(recording: asyncio.Future, command_id: str) -> None:
    if recording.cancelled() or recording.exception() is None:
        return
    from app.core.utils import format_exception_message

    logger.warning(
        "runner command %s could not be given up after its call answered: %s",
        command_id,
        format_exception_message(recording.exception()),
    )


async def _give_up_in_time(command_id: str, reason: str, waited: dict) -> dict | None:
    """Record the give-up of a command whose wait ended; the row as it now
    stands (a result may have landed meanwhile).

    It takes the command's row lock, which a result upload in flight holds
    while it stores its file. Inside the agent loop it gets the reserve but
    its last second: past that it goes on recording by itself - the command is
    still never sent again - and the reply is built from what the wait saw,
    instead of the loop cutting the call with a bare timeout.
    """
    deadline = current_tool_deadline()
    if deadline is None:
        return await give_up_on_command(command_id, reason)
    recording = asyncio.ensure_future(give_up_on_command(command_id, reason))
    _GIVE_UPS_IN_FLIGHT.add(recording)
    recording.add_done_callback(_GIVE_UPS_IN_FLIGHT.discard)
    orphaned = functools.partial(_log_failed_give_up, command_id=command_id)
    try:
        return await asyncio.wait_for(
            asyncio.shield(recording),
            max(0.0, deadline - _REPLY_SECONDS - time.monotonic()),
        )
    except asyncio.CancelledError:
        recording.add_done_callback(orphaned)
        raise
    except TimeoutError:
        recording.add_done_callback(orphaned)
    if waited.get("status") == "pending":
        return {**waited, "status": "timeout", "error": reason}
    return waited


#: Said of every command the wait ended on while a machine held it: it is
#: never delivered again, and this is where its output lands if it finishes.
_NOT_SENT_AGAIN = (
    " It will not be sent again; if it finishes, its output is in the machine's "
    "command history, not in this chat - check there before retrying a command "
    "that changes the machine."
)

#: Why the wait ended when it was the agent run's limit, not the command's.
_CALL_LIMIT_REACHED = (
    "this tool call reached the time limit the agent run sets for one tool call"
)


def _undelivered_reason(runner_name: str, *, cut_short: bool) -> str:
    """What a command no machine picked up is finished with."""
    if cut_short:
        return (
            f"Runner '{runner_name}' had not picked the command up when "
            f"{_CALL_LIMIT_REACHED}. It was cancelled so it cannot run later with "
            "nobody waiting for it."
        )
    return (
        f"Runner '{runner_name}' never picked the command up (the machine "
        "is offline or unreachable). It was cancelled so it cannot run later "
        "with nobody waiting for it; retry once the machine is back online."
    )


def _unfinished_result(runner_name: str, *, cut_short: bool) -> str:
    """What the agent reads when its wait ended while a machine held the command."""
    fixed = settings.runner_result_wait_timeout_seconds
    if cut_short:
        reason = f"{_CALL_LIMIT_REACHED} before its result came back"
        if fixed is None:
            reason += (
                f" (the machine has up to {default_result_wait_seconds()} s from "
                "delivery to report it)"
            )
        return (
            f"Runner '{runner_name}' took the command and may still be running "
            f"it: {reason}." + _NOT_SENT_AGAIN
        )
    if fixed is not None:
        return (
            f"Runner '{runner_name}' took the command but reported no result "
            f"within the {fixed} s this platform waits for one "
            "(RUNNER_RESULT_WAIT_TIMEOUT_SECONDS)." + _NOT_SENT_AGAIN
        )
    return (
        f"Runner '{runner_name}' took the command but no result came back within "
        f"its window: {runner_execution_ceiling_seconds()} s to run it (its turn "
        f"on the machine included) and {RUNNER_UPLOAD_RETRY_BUDGET_SECONDS} s to "
        "retry the result upload." + _NOT_SENT_AGAIN
    )


def build_computer_use_tools(
    *,
    user_id: str | None,
    is_admin: bool,
    user_group_ids: list[str] | None,
    agent_runner_ids: list[str] | None,
    agent_runner_labels: list[str] | None,
    assignment_runner_ids: list[str] | None = None,
    agent_id: str | None = None,
    conversation_id: str | None = None,
    assignment_run_id: str | None = None,
    local_runner: Runner | None = None,
    interactive_approval: bool = False,
    approver_is_admin: bool = False,
) -> tuple[list[ToolDefinition], CallableToolExecutor]:
    """Build the ``computer_use`` tool suite bound to one run's context.

    The agent's runner selector (``agent_runner_ids`` / ``agent_runner_labels``)
    and the assignment's ``assignment_runner_ids`` refinement (``None`` =
    inherit, ``[]`` = none, subset = refine) are captured here and enforced on
    every call, so an assignment can never reach a machine the agent could not.

    ``local_runner`` is the machine an XTM One Desktop chat was sent from:
    always eligible when the user can see it, and the default target
    of every call that names no ``runner_id``. ``interactive_approval`` makes a
    held shell command wait for an inline decision instead of returning
    "queued" - only a live chat should set it, and per call it only holds when
    the acting user may actually DECIDE for the machine the call SELECTED
    (``can_manage_as`` with ``approver_is_admin``: its enroller, a ``config``
    grantee, an admin on a company-managed one - the same gate the approve /
    reject routes apply). Otherwise every button of the card would answer 403
    and the tool would wait out the window for nothing; the command is queued
    for the Desktop page instead, like an unattended run.

    ``approver_is_admin`` is the caller's REAL role and serves that decision
    only; ``is_admin`` governs targeting, where the chat builders deliberately
    pass ``False`` (an agent never drives another user's private machine on
    the strength of its user's role). The two are different questions.
    """

    from app.api.v1.helpers import can_manage_as, user_visible_query
    from app.models.runner import Runner

    user_uuid = _as_uuid(user_id)
    agent_uuid = _as_uuid(agent_id)
    conversation_uuid = _as_uuid(conversation_id)
    assignment_run_uuid = _as_uuid(assignment_run_id)
    local_runner_id = str(local_runner.id) if local_runner is not None else None

    def _may_decide_inline(target: Runner) -> bool:
        """Whether the acting user could press the card's buttons for *target*."""
        return interactive_approval and can_manage_as(
            user_uuid, approver_is_admin, target, user_group_ids
        )

    async def _eligible_runners() -> list[Runner]:
        """Runners this agent may target, after every scoping layer.

        With a resolved user we use the standard three-tier visibility (own +
        group-shared + company-managed). A company-managed agent / assignment
        run has no owner (``user_uuid is None``); rather than reaching nothing,
        it targets the org's company-managed runners only — the audit-fleet
        case — never any user's private machine.

        The Desktop's local machine is re-admitted after the agent's selector
        and the assignment refinement, but only from the VISIBLE rows: the pin
        widens what an agent targets by default, never who may target what.
        """
        from sqlalchemy import select

        async with async_session() as db:
            if user_uuid is not None:
                # Execution targeting uses NORMAL three-tier visibility (own +
                # group-shared + company-managed) even for an admin. Admin-wide
                # visibility is a fleet-MANAGEMENT power (the list/CRUD API);
                # applying it here would let an admin's agent drive another
                # user's PRIVATE machine, which the runner contract forbids. So
                # is_admin is deliberately NOT passed through to this query.
                query = user_visible_query(
                    Runner, user_uuid, user_group_ids, is_admin=False
                )
            else:
                query = select(Runner).where(Runner.is_company_managed.is_(True))
            result = await db.execute(query)
            rows = list(result.scalars().all())

        return filter_eligible_runners(
            rows,
            agent_runner_ids=agent_runner_ids,
            agent_runner_labels=agent_runner_labels,
            assignment_runner_ids=assignment_runner_ids,
            local_runner_id=local_runner_id,
        )

    def _select(eligible: list[Runner], runner_id: str | None) -> Runner | str:
        return select_target_runner(
            eligible, runner_id, local_runner_id=local_runner_id
        )

    async def _run_command(
        kind: str,
        payload: dict,
        runner_id: str | None,
        file_context: FileContext | None,
    ) -> str | ToolResult:
        activity = RunnerActivity(kind, payload)
        try:
            return await _drive_command(activity, kind, payload, runner_id, file_context)
        except Exception as exc:
            # The card must not spin on a command whose tool failed outright.
            # (A cancelled turn propagates untouched: the chat closes its live
            # cards as interrupted.)
            from app.core.utils import format_exception_message

            activity.settle(STATUS_FAILED, error=format_exception_message(exc))
            await activity.report()
            raise

    async def _drive_command(
        activity: RunnerActivity,
        kind: str,
        payload: dict,
        runner_id: str | None,
        file_context: FileContext | None,
    ) -> str | ToolResult:
        eligible = await _eligible_runners()
        target = _select(eligible, runner_id)
        if isinstance(target, str):
            if runner_id is None and local_runner is not None:
                activity.bind_runner(local_runner, local=True)
            activity.settle(STATUS_UNAVAILABLE)
            await activity.report()
            from app.services.frictions import note_runner_failure

            note_runner_failure(kind, f"no runner to run on: {target}")
            return target
        activity.bind_runner(target, local=str(target.id) == local_runner_id)

        refusal = authorize_command(target, kind, payload)
        if refusal:
            activity.settle(STATUS_REFUSED, error=refusal)
            await activity.report()
            from app.services.frictions import note_runner_failure

            note_runner_failure(kind, f"refused by the runner's permission policy: {refusal}")
            return f"Refused by the runner's permission policy: {refusal}"

        if runner_lacks_shell_jobs(kind, payload, target.agent_version):
            # An older runner would run a background command in the foreground
            # (the call blocking for the whole command) and rejects shell_job:
            # refuse before dispatch, saying how to get there.
            outdated = _outdated_for_shell_jobs(target, kind)
            activity.settle(STATUS_REFUSED, error=outdated)
            await activity.report()
            return outdated

        requires_approval = command_requires_approval(target, kind, payload)
        command_id, status = await dispatch_command(
            runner_id=target.id,
            kind=kind,
            payload=payload,
            user_id=user_uuid,
            agent_id=agent_uuid,
            conversation_id=conversation_uuid,
            assignment_run_id=assignment_run_uuid,
            requires_approval=requires_approval,
        )
        if status == "revoked":
            activity.settle(STATUS_UNAVAILABLE)
            await activity.report()
            return f"Runner '{target.name}' was revoked and can no longer run commands."
        if status == "busy":
            activity.settle(STATUS_BUSY)
            await activity.report()
            return (
                f"Runner '{target.name}' is at its concurrent-command limit "
                "right now. Wait for the in-flight actions to finish, then "
                "retry."
            )
        try:
            return await _follow_command(
                activity, target, kind, payload, command_id, status, file_context
            )
        except asyncio.CancelledError:
            # The turn is ending (Stop, deadline, tool ceiling) while the
            # command exists: nobody will read its result, so it must not run
            # later for nobody. Shielded, like the approval card's cleanup.
            await asyncio.shield(give_up_quietly(command_id, TURN_ENDED_REASON))
            raise

    async def _follow_command(
        activity: RunnerActivity,
        target: Runner,
        kind: str,
        payload: dict,
        command_id: str,
        status: str,
        file_context: FileContext | None,
    ) -> str | ToolResult:
        """Everything after the command was committed, up to its outcome."""
        activity.command_id = command_id
        if status == "pending_approval":
            # Inline only when this user could actually decide for THIS
            # target: an explicit runner_id may have picked a machine they
            # can see but not manage (read-only share, company-managed).
            if not _may_decide_inline(target):
                activity.settle(STATUS_NOT_APPROVED)
                await activity.report()
                return (
                    f"Command queued for human approval (id {command_id}). It "
                    f"will run once an operator approves it on {DESKTOP_PAGE_HINT}."
                )
            activity.status = STATUS_AWAITING_APPROVAL
            await activity.report()
            decision, decided = await _await_inline_decision(target, kind, payload, command_id)
            if decided is not None:
                activity.settle(
                    _DECISION_STATUSES.get(decision, STATUS_FAILED),
                    error=(
                        "The command could not be found after dispatch."
                        if decision == "missing"
                        else None
                    ),
                )
                await activity.report()
                return decided

        activity.mark_running()
        await activity.report()
        not_after = _answer_deadline()
        result = await await_result(command_id, not_after=not_after)
        cut_short = False
        if result is not None and result.get("status") in ("pending", "dispatched"):
            # Ended by the call's own limit rather than by the command's window.
            cut_short = not_after is not None and time.monotonic() >= not_after
            # The row as it stands once the give-up is recorded: it may hold
            # the real result that landed meanwhile.
            result = await _give_up_in_time(
                command_id, _undelivered_reason(target.name, cut_short=cut_short), result
            )
        outcome = await _format_result(
            target, kind, result, file_context, payload, cut_short=cut_short
        )
        activity.settle_from_result(result)
        await activity.report()
        return outcome

    async def _await_inline_decision(
        target: Runner, kind: str, payload: dict, command_id: str
    ) -> tuple[str, str | None]:
        """Offer the held command to the user in the chat and wait for them.

        Returns the command's status when the wait ended, with a tool result
        when it ended WITHOUT an executable command (rejected, or nobody
        decided in time) or ``None`` when the command was approved and is on
        its way to the runner - the caller then awaits its result like any
        other dispatch.
        """
        summary = approval_summary(kind, payload)
        await emit_tool_event(
            RUNNER_APPROVAL_REQUIRED_EVENT,
            command_id=command_id,
            runner_id=str(target.id),
            runner_name=target.name,
            kind=kind,
            summary=summary,
            expires_in_seconds=DESKTOP_APPROVAL_WAIT_SECONDS,
        )
        try:
            status = await await_approval_decision(
                command_id, DESKTOP_APPROVAL_WAIT_SECONDS
            )
        except asyncio.CancelledError:
            # The turn is ending under the card (Stop, deadline, tool
            # ceiling): a command offered with THIS chat's context must not
            # sit in the queue to run later without it. Shielded, because
            # the cancellation that brought us here would cut the cleanup too.
            await asyncio.shield(abandon_inline_approval(command_id))
            raise
        await emit_tool_event(
            RUNNER_APPROVAL_RESOLVED_EVENT, command_id=command_id, status=status
        )
        if status == "pending_approval":
            return status, (
                f"The user has not approved '{summary}' yet (command id "
                f"{command_id}). It stays queued and runs once they approve it "
                f"on {DESKTOP_PAGE_HINT}; its output will then be in the "
                "machine's command history, not in this chat. Do not retry the "
                "same command - tell the user it is waiting for their approval."
            )
        if status == "cancelled":
            return status, (
                f"The user REJECTED '{summary}' on '{target.name}'. Do not retry "
                "it or a variant of it; ask what they prefer or propose another "
                "approach."
            )
        if status == "missing":
            return status, "The command could not be found after dispatch."
        return status, None

    async def _format_result(
        runner: Runner,
        kind: str,
        result: dict | None,
        file_context: FileContext | None,
        request: dict | None = None,
        *,
        cut_short: bool = False,
    ) -> str | ToolResult:
        if result is None:
            return "The command could not be found after dispatch."
        if result["status"] != "completed":
            if result["status"] == "dispatched":
                return _unfinished_result(runner.name, cut_short=cut_short)
            if result["status"] == "pending":
                return (
                    "Timed out waiting for the runner to complete the command. "
                    "The machine may be busy or slow; try again."
                )
            err = result.get("error") or "unknown error"
            ended = f"Runner command {result['status']}: {err}"
            partial = result.get("result")
            if kind == "run_shell" and isinstance(partial, dict) and partial.get("stopped"):
                # Ended before it exited: what it printed until then
                # is the agent's to read, so the turn goes on from it instead
                # of running the command again blindly.
                return _stopped_shell_result(runner, ended, partial)
            return ended

        result_file_id = result.get("result_file_id")
        if result_file_id:
            return await _file_result(
                runner, kind, result, result_file_id, file_context, request
            )

        payload = result.get("result") or {}
        return _format_text_result(runner, kind, payload, request)

    async def _file_result(
        runner: Runner,
        kind: str,
        result: dict,
        result_file_id: str,
        file_context: FileContext | None,
        request: dict | None = None,
    ) -> str | ToolResult:
        from app.llm.base import FileRef
        from app.models.file_attachment import FileAttachment
        from app.tools.base import ToolResult as _ToolResult

        file_uuid = _as_uuid(result_file_id)
        if file_uuid is None:
            return "The runner reported an invalid file result id."
        async with async_session() as db:
            att = await db.get(FileAttachment, file_uuid)
        if att is None:
            return "The runner reported a file result but it could not be loaded."

        file_ref = FileRef.from_attachment(att)
        if file_context is not None:
            file_context.add(file_ref)
        url = f"/api/v1/chat/files/{file_ref.file_id}/download"

        if kind in ("screenshot", "browser_control"):
            meta = result.get("result") or {}

            # The backend persisted an integrity sidecar (SHA-256 + capture
            # context) next to the capture. Load it into the run's FileContext
            # too, so the agent can forward BOTH files to an evidence tool in
            # this same run.
            sidecar_ref = None
            sidecar_uuid = _as_uuid(meta.get("sidecar_file_id"))
            if sidecar_uuid is not None:
                async with async_session() as db:
                    sidecar_att = await db.get(FileAttachment, sidecar_uuid)
                if sidecar_att is not None:
                    sidecar_ref = FileRef.from_attachment(sidecar_att)
                    if file_context is not None:
                        file_context.add(sidecar_ref)

            label = (
                "Full-desktop screenshot"
                if kind == "screenshot"
                else "Browser page screenshot"
            )
            lines = [
                f"{label} captured on runner '{runner.name}' ({att.size} "
                "bytes, stored byte-for-byte — never re-rendered).",
                f"- source_file_id: {file_ref.file_id} — pass this id to an "
                "evidence tool that accepts source_file_id (e.g. Drata "
                "external evidence upload) to file the EXACT captured bytes.",
            ]
            if meta.get("sha256"):
                verified = meta.get("sha256_verified")
                if verified is True:
                    verdict = "verified against the runner's capture-time hash"
                elif verified is False:
                    verdict = (
                        "MISMATCH vs the runner's capture-time hash — treat "
                        "as tampered and recapture"
                    )
                else:
                    verdict = "no capture-time hash reported by the runner"
                lines.append(f"- SHA-256: {meta['sha256']} ({verdict}).")
            provenance: list[str] = []
            if meta.get("captured_at"):
                provenance.append(f"captured at {meta['captured_at']} (UTC)")
            if meta.get("captured_at_local"):
                provenance.append(f"{meta['captured_at_local']} local time")
            if meta.get("url"):
                provenance.append(f"page URL {meta['url']}")
            if meta.get("title"):
                provenance.append(f"page title '{meta['title']}'")
            if meta.get("monitor"):
                provenance.append(f"monitor {meta['monitor']}")
            if meta.get("width") and meta.get("height"):
                provenance.append(f"{meta['width']}x{meta['height']}px")
            if provenance:
                lines.append("- Provenance: " + ", ".join(provenance) + ".")
            if sidecar_ref is not None:
                lines.append(
                    "- Integrity sidecar (JSON: hash + capture context): "
                    f"source_file_id {sidecar_ref.file_id} — file it "
                    "alongside the image as supporting evidence."
                )
            lines.append(
                "The PNG is attached as a native vision block on this tool "
                "result - you can see the pixels. Read the URL bar, labels, "
                "errors, and OS clock yourself. Never ask the user to describe "
                "or verify the screenshot, and never say it is for the user "
                "rather than for you. Also include this EXACT Markdown in your "
                "reply (relative path verbatim, do NOT prepend any domain) so "
                f"it renders for the human: ![{label}]({url})"
            )
            files = [file_ref] + ([sidecar_ref] if sidecar_ref else [])
            return _ToolResult(
                tool_call_id="",
                content="\n".join(lines),
                files=files,
            )
        meta = result.get("result") or {}
        cut = _read_cut_notice(runner, meta, request) if kind == "read_file" else ""
        return _ToolResult(
            tool_call_id="",
            content=(
                _runner_warning(meta)
                + cut
                + f"File retrieved from '{runner.name}' ({att.size} bytes). "
                f"Download / reference it here: [[FILE:{file_ref.file_id}]] "
                f"({url})"
            ),
            files=[file_ref],
        )

    def _runner_warning(payload: dict) -> str:
        """The warning a runner attached to a file result, as a leading line."""
        warning = payload.get("warning")
        return f"Warning from the runner: {warning}\n\n" if isinstance(warning, str) and warning else ""

    def _read_cut_notice(runner: Runner, payload: dict, request: dict | None) -> str:
        """A leading line when a ``read_file`` stopped before the end of the file.

        The runner says so (``truncated``). A runner from before that field
        cannot, so a read that came back at exactly the size it was asked for
        may have been cut.
        """
        size = payload.get("size")
        if isinstance(size, bool) or not isinstance(size, int):
            return ""
        truncated = payload.get("truncated")
        if truncated is True:
            notice = (
                f"Only the first {size} bytes were read: the file is longer, so "
                "this content is incomplete."
            )
        elif truncated is None:
            asked = delivery_payload(runner, "read_file", request).get("max_bytes")
            if isinstance(asked, bool) or not isinstance(asked, int) or not 0 < asked <= size:
                return ""
            notice = (
                f"The read stopped at the {asked} bytes it was asked for: the file "
                "may be longer, so this content may be incomplete."
            )
        else:
            return ""
        limit = settings.runner_max_result_bytes
        # A runner that honours ``max_inline_bytes`` says so in every inline
        # result (``inline_text_limit``), reads that did not need it included:
        # its text reads are not capped at that limit, up to the inline size.
        inline = "content" in payload and any(
            isinstance(payload.get(key), int)
            for key in (INLINE_TEXT_LIMIT_KEY, MAX_INLINE_BYTES_KEY)
        )
        ceiling = max(limit, RUNNER_INLINE_TEXT_BYTES) if limit > 0 and inline else limit
        if limit > 0 and size >= ceiling > limit:
            notice += (
                f" {ceiling} bytes is the most text a runner returns inline, and a "
                "longer read comes back as a file, which RUNNER_MAX_RESULT_BYTES "
                f"({limit} bytes) refuses: read the rest another way, for example "
                "with a shell command that splits or compresses the file."
            )
        elif limit > 0 and size >= ceiling:
            notice += (
                f" {limit} bytes is the largest file a runner may return "
                "(RUNNER_MAX_RESULT_BYTES): read the rest another way, for "
                "example with a shell command that splits or compresses the file."
            )
        elif limit > 0:
            notice += f" Call runner_read_file again with a larger max_bytes, up to {ceiling}, to read more."
        else:
            notice += " Call runner_read_file again with a larger max_bytes to read more."
        return f"{notice}\n\n"

    def _outdated_for_shell_jobs(runner: Runner, kind: str) -> str:
        """Why a runner too old for background jobs was not sent the command."""
        what = "run_shell with background: true" if kind == "run_shell" else "shell_jobs"
        parts = [
            f"Runner '{runner.name}' does not support background jobs: it runs "
            f"version {runner.agent_version} and {what} needs "
            f">= {SHELL_JOBS_MIN_RUNNER_VERSION}. Nothing was sent to it.",
            "It updates itself from this instance when automatic updates are on "
            "(XTM One Desktop checks every few hours; a headless runner on its "
            "next check); otherwise its user clicks Check for updates in XTM One "
            "Desktop (connection panel or app menu).",
        ]
        if kind == "run_shell":
            parts.append("Until then, run the command without background (it blocks until it exits).")
        return " ".join(parts)

    def _stopped_shell_result(runner: Runner, ended: str, payload: dict) -> str:
        """A ``run_shell`` ended before it exited, with the output it had produced."""
        if payload.get("stopped") == "user":
            # Stopped from its chat card: the user's choice, not a
            # failure to work around, so the agent must not run it again.
            at = time.strftime("%H:%M:%S", time.gmtime())
            parts = [
                f"The user stopped this command at {at} UTC on '{runner.name}', "
                "with every process it started. Do not run it again unless the "
                "user asks; below is what it printed until then.",
            ]
        else:
            parts = [
                ended,
                f"The command was ended on '{runner.name}' before it exited, with every "
                "process it started; below is what it printed until then.",
            ]
        stdout = payload.get("stdout") or ""
        stderr = payload.get("stderr") or ""
        if stdout:
            parts.append(f"stdout (partial):\n{stdout}")
        if stderr:
            parts.append(f"stderr (partial):\n{stderr}")
        if not stdout and not stderr:
            parts.append("It had printed nothing.")
        return "\n\n".join(parts)

    def _format_text_result(
        runner: Runner, kind: str, payload: dict, request: dict | None = None
    ) -> str:
        import json as _json

        if kind == "run_shell" and payload.get("background"):
            # A background job: started, not finished.
            first = payload.get("first_output") or ""
            parts = [
                f"Started in the background on '{runner.name}' as job "
                f"{payload.get('job_id')} (pid {payload.get('pid')}, status "
                f"{payload.get('status')}). It keeps running after this turn. "
                "Read its output with shell_jobs action 'tail' (job_id, offset), "
                "list jobs with action 'list', stop it with action 'stop'."
            ]
            parts.append(f"First output:\n{first}" if first else "It has printed nothing yet.")
            return "\n\n".join(parts)
        if kind == "shell_job":
            return _json.dumps(payload, default=str)
        if kind == "run_shell":
            exit_code = payload.get("exit_code")
            stdout = payload.get("stdout", "")
            stderr = payload.get("stderr", "")
            parts = [f"Runner '{runner.name}' exit code: {exit_code}"]
            if stdout:
                parts.append(f"stdout:\n{stdout}")
            if stderr:
                parts.append(f"stderr:\n{stderr}")
            return "\n\n".join(parts)
        if kind == "list_dir":
            listing = {"path": payload.get("path"), "entries": payload.get("entries", [])}
            if payload.get("warning"):
                listing["warning"] = payload["warning"]
            return _json.dumps(listing, default=str)
        if kind == "read_file":
            cut = _read_cut_notice(runner, payload, request)
            content = payload.get("content", "") or ("(nothing read)" if cut else "(empty file)")
            return _runner_warning(payload) + cut + content
        if kind == "host_facts":
            return _json.dumps(payload, default=str, indent=2)
        if kind == "browser_control" and payload.get("action") == "get_content":
            return _page_text_result(payload)
        return _json.dumps(payload, default=str)

    # ── Tool callables ──────────────────────────────────────────────────────

    async def _list_runners() -> str:
        import json as _json

        eligible = await _eligible_runners()
        if not eligible:
            return (
                "No runners are available to this agent. The user can install "
                f"XTM One Desktop (or the headless runner on a server) from "
                f"{DESKTOP_PAGE_HINT}."
            )
        out = []
        for r in eligible:
            entry = {
                "id": str(r.id),
                "name": r.name,
                "platform": r.platform,
                "online": is_runner_online(r),
                "permission_mode": r.permission_mode,
                "capabilities": effective_runner_capabilities(r),
                "labels": r.labels or [],
                "host_facts": r.host_facts,
            }
            if local_runner_id is not None:
                # The machine the user is chatting from (XTM One Desktop):
                # the default target of every other tool in this suite.
                entry["local"] = str(r.id) == local_runner_id
            out.append(entry)
        return _json.dumps(out, default=str)

    async def _resolve_typed_text(text: str | None) -> tuple[str | None, str | None]:
        """*text* with its platform variable placeholders resolved, or a refusal.

        Text an agent types on a machine (a sign-in form) may name a secret
        variable as ``{{var_id.UUID}}``: it is resolved here, with the run's own
        identity (the visibility persona substitution uses), so the runner types
        the value. ``load_variables`` remembers each secret in the open scope:
        ``dispatch_command`` stores it as a marker bound to the command and only
        the delivery to the runner restores it, so no trace, log or stored row
        holds it. A placeholder this identity cannot resolve is refused, never
        typed as literal braces.
        """
        from app.services.variable_resolver import (
            extract_variable_ids,
            load_variables,
            substitute_variables,
        )

        wanted = extract_variable_ids(text)
        if not text or not wanted:
            return text, None
        async with async_session() as db:
            variables = await load_variables(db, user_uuid, user_group_ids)
        missing = sorted(var_id for var_id in wanted if var_id not in variables)
        if missing:
            return None, (
                "Nothing typed: the text names platform variable(s) "
                + ", ".join(missing)
                + " that do not exist, have no readable value, or are not shared "
                "with this run's identity. Check the variable id and its sharing."
            )
        return substitute_variables(text, variables), None

    async def _computer_screenshot(
        runner_id: str | None = None,
        display: str | None = None,
        file_context: FileContext | None = None,
    ) -> str | ToolResult:
        payload = {"display": display} if display else {}
        return await _run_command("screenshot", payload, runner_id, file_context)

    async def _computer_action(
        action: str,
        x: int | None = None,
        y: int | None = None,
        x2: int | None = None,
        y2: int | None = None,
        text: str | None = None,
        keys: str | None = None,
        button: str | None = None,
        amount: int | None = None,
        runner_id: str | None = None,
        file_context: FileContext | None = None,
    ) -> str | ToolResult:
        text, refusal = await _resolve_typed_text(text)
        if refusal:
            return refusal
        payload = {
            "action": action,
            "x": x,
            "y": y,
            "x2": x2,
            "y2": y2,
            "text": text,
            "keys": keys,
            "button": button,
            "amount": amount,
        }
        payload = {k: v for k, v in payload.items() if v is not None}
        return await _run_command("computer_action", payload, runner_id, file_context)

    async def _run_shell(
        command: str,
        cwd: str | None = None,
        timeout: int | None = None,
        background: bool | None = None,
        runner_id: str | None = None,
        file_context: FileContext | None = None,
    ) -> str | ToolResult:
        payload: dict = {"command": command}
        if cwd:
            payload["cwd"] = cwd
        if timeout and not background:
            payload["timeout"] = timeout
        if background:
            payload["background"] = True
        return await _run_command("run_shell", payload, runner_id, file_context)

    async def _shell_jobs(
        action: str = "list",
        job_id: str | None = None,
        offset: int | None = None,
        max_bytes: int | None = None,
        runner_id: str | None = None,
        file_context: FileContext | None = None,
    ) -> str | ToolResult:
        if action not in SHELL_JOB_ACTIONS:
            return f"Unknown action '{action}': use one of {', '.join(SHELL_JOB_ACTIONS)}."
        if action != "list" and not job_id:
            return f"Action '{action}' needs the job_id run_shell answered."
        payload: dict = {"action": action}
        if job_id:
            payload["job_id"] = job_id
        if offset is not None and offset >= 0:
            payload["offset"] = offset
        if max_bytes:
            payload["max_bytes"] = max_bytes
        return await _run_command("shell_job", payload, runner_id, file_context)

    async def _read_file(
        path: str,
        max_bytes: int | None = None,
        runner_id: str | None = None,
        file_context: FileContext | None = None,
    ) -> str | ToolResult:
        payload: dict = {"path": path}
        if max_bytes:
            payload["max_bytes"] = max_bytes
        return await _run_command("read_file", payload, runner_id, file_context)

    async def _write_file(
        path: str,
        content: str,
        runner_id: str | None = None,
        file_context: FileContext | None = None,
    ) -> str | ToolResult:
        payload = {"path": path, "content": content}
        return await _run_command("write_file", payload, runner_id, file_context)

    async def _list_dir(
        path: str,
        runner_id: str | None = None,
        file_context: FileContext | None = None,
    ) -> str | ToolResult:
        return await _run_command("list_dir", {"path": path}, runner_id, file_context)

    async def _browser_control(
        action: str,
        url: str | None = None,
        selector: str | None = None,
        text: str | None = None,
        browser: str | None = None,
        tab_id: str | None = None,
        expression: str | None = None,
        timeout: float | None = None,
        frame: str | None = None,
        runner_id: str | None = None,
        file_context: FileContext | None = None,
    ) -> str | ToolResult:
        text, refusal = await _resolve_typed_text(text)
        if refusal:
            return refusal
        payload = {
            "action": action,
            "url": url,
            "selector": selector,
            "text": text,
            "browser": browser,
            "tab_id": tab_id,
            "expression": expression,
            "timeout": timeout,
            "frame": frame,
        }
        payload = {k: v for k, v in payload.items() if v is not None}
        return await _run_command("browser_control", payload, runner_id, file_context)

    executor = CallableToolExecutor()
    executor.register("list_runners", _list_runners)
    executor.register("computer_screenshot", _computer_screenshot)
    executor.register("computer_action", _computer_action)
    executor.register("run_shell", _run_shell)
    executor.register("shell_jobs", _shell_jobs)
    # ``runner_`` names; the former names resolve as the aliases each
    # definition declares (``ToolDefinition.aliases``). The runner command kinds keep their names.
    executor.register("runner_read_file", _read_file)
    executor.register("runner_write_file", _write_file)
    executor.register("runner_list_dir", _list_dir)
    # The [DEPRECATED] names run the same handlers for a caller holding this
    # executor directly; no definition is offered under them.
    executor.register("read_file", _read_file)
    executor.register("write_file", _write_file)
    executor.register("list_dir", _list_dir)
    executor.register("browser_control", _browser_control)

    definitions = _tool_definitions(local_runner)
    logger.debug(
        "computer_use tools built (user=%s, agent=%s, local_runner=%s)",
        user_id,
        agent_id,
        local_runner_id,
    )
    return definitions, executor


#: Characters of page text a runner ``get_content`` answers, as the headless browser's own get_content.
PAGE_TEXT_MAX_CHARS = 50_000


def _page_text_result(payload: dict) -> str:
    """A runner ``get_content`` answer as the page's reading-order text, never its raw HTML.

    The runner sends the document's HTML (``content``); scripts, styles and markup cost the context
    several times the text a reader sees. The text is cut at ``PAGE_TEXT_MAX_CHARS``, the cut said;
    ``evaluate`` still reads any markup the task needs.
    """
    from app.services.attachment_extract import extract_html_document

    html = str(payload.get("content") or "")
    page = extract_html_document(html)
    text = page.text.strip()
    head = [f"Frame: {payload['frame']}"] if payload.get("frame") else []
    if page.title:
        head.append(f"Title: {page.title}")
    head.append(f"Page text ({len(text):,} characters from {len(html):,} of HTML; evaluate reads the markup):")
    if len(text) > PAGE_TEXT_MAX_CHARS:
        text = text[:PAGE_TEXT_MAX_CHARS] + f"\n\n[Content truncated at {PAGE_TEXT_MAX_CHARS:,} characters]"
    return "\n".join(head) + "\n\n" + (text or "(the page shows no text)")


def filter_eligible_runners(
    rows: list[Runner],
    *,
    agent_runner_ids: list[str] | None,
    agent_runner_labels: list[str] | None,
    assignment_runner_ids: list[str] | None,
    local_runner_id: str | None = None,
) -> list[Runner]:
    """Apply the agent selector + assignment refinement to VISIBLE runners.

    Pure so the scoping rule is testable without a database. ``rows`` must
    already be the runners the acting user may see; this only narrows them.

    The Desktop's local machine (``local_runner_id``) is re-admitted after
    the selector and the refinement when it is among ``rows``: the user chose
    that machine by chatting from it, and the agent's selector describes the
    fleet it drives by default, not the desk its user sits at. It is never
    ADDED from outside ``rows`` - visibility is the boundary, and the pin
    cannot cross it.
    """
    allowed_ids = {str(x) for x in (agent_runner_ids or [])}
    allowed_labels = {str(x) for x in (agent_runner_labels or [])}
    refine = (
        None if assignment_runner_ids is None
        else {str(x) for x in assignment_runner_ids}
    )

    eligible: list[Runner] = []
    for r in rows:
        if allowed_ids and str(r.id) not in allowed_ids:
            continue
        if allowed_labels and not (set(r.labels or []) & allowed_labels):
            continue
        if refine is not None and str(r.id) not in refine:
            continue
        eligible.append(r)

    if local_runner_id is not None and not any(
        str(r.id) == local_runner_id for r in eligible
    ):
        local = next((r for r in rows if str(r.id) == local_runner_id), None)
        if local is not None:
            eligible.append(local)
    return eligible


def select_target_runner(
    eligible: list[Runner],
    runner_id: str | None,
    *,
    local_runner_id: str | None = None,
) -> Runner | str:
    """Pick the target runner or return a user-facing error string.

    An explicit ``runner_id`` always wins. Otherwise the Desktop's local
    machine is the default when one is pinned: online -> it is the target;
    offline -> an explicit refusal naming it, never a silent fallback to some
    other machine of the fleet (a command the user typed at their own desk must
    not land on a server because their daemon was reconnecting). Without a pin
    the fleet rule applies: exactly one online runner auto-selects, several
    require a choice.
    """
    if runner_id:
        match = next((r for r in eligible if str(r.id) == runner_id), None)
        if match is None:
            return (
                f"No runner with id '{runner_id}' is available to this "
                "agent. Call list_runners to see targetable machines."
            )
        if not is_runner_online(match):
            return (
                f"Runner '{match.name}' is offline right now. Start it on "
                "the machine and retry."
            )
        return match
    if local_runner_id is not None:
        local = next((r for r in eligible if str(r.id) == local_runner_id), None)
        if local is not None:
            if is_runner_online(local):
                return local
            return (
                f"This machine ('{local.name}') is not connected to the platform "
                "right now - XTM One Desktop is reconnecting, or its runner is "
                "stopped (the app's connection panel shows the state). Retry in "
                "a moment, or pass runner_id to act on another enrolled machine "
                "instead (call list_runners)."
            )
    online = [r for r in eligible if is_runner_online(r)]
    if not online:
        if not eligible:
            return (
                "No runners are available to this agent. The user can install "
                "XTM One Desktop (or the headless runner on a server) from "
                f"{DESKTOP_PAGE_HINT}, then enroll the machine."
            )
        return (
            "The runner(s) for this agent are offline. Start the runner on "
            "the target machine and retry."
        )
    if len(online) > 1:
        listing = "; ".join(f"{r.name} (id {r.id})" for r in online)
        return (
            "Multiple runners are online — pass runner_id to choose one: "
            f"{listing}."
        )
    return online[0]


def _former_names(current: str) -> tuple[str, ...]:
    """The [DEPRECATED] names of the function *current*, declared as its aliases."""
    return tuple(former for former, name in RENAMED_BUILTIN_FUNCTIONS.items() if name == current)


def _tool_definitions(local_runner: Runner | None = None) -> list[ToolDefinition]:
    if local_runner is not None:
        # XTM One Desktop: the grounding for "this machine" lives HERE, in the
        # tool contract the model reads on every call - not in the system
        # prompt (AGENTS.md invariant 15).
        runner_id_prop = {
            "type": "string",
            "description": (
                "Target runner id. OMIT IT to act on the user's own machine, "
                f"'{local_runner.name}' ({local_runner.platform}), where they "
                "are running XTM One Desktop right now - that machine is the "
                "default for this chat, so 'here', 'my machine', 'locally' and "
                "'this computer' all mean it. Pass an id (from list_runners) "
                "only to reach ANOTHER enrolled machine."
            ),
        }
        list_hint = (
            " In this chat the entry with local: true is the machine the user "
            "is sitting at (XTM One Desktop); the other tools already default "
            "to it, so you only need this list to reach a different machine."
        )
    else:
        runner_id_prop = {
            "type": "string",
            "description": (
                "Target runner id. Required only when more than one runner is "
                "online; call list_runners first to choose. Omit to auto-select "
                "the single online runner."
            ),
        }
        list_hint = ""
    return [
        ToolDefinition(
            name="list_runners",
            description=(
                "List the native OS-control runners this agent may drive, with "
                "their online status, platform, permission mode, granted "
                "capabilities (the effective ones: a full_control runner "
                "grants every capability) and reported host facts (OS build, available "
                "shells + versions, installed/authenticated browsers, monitor "
                "topology). Call this first to pick a runner_id for the other "
                "computer_use tools." + list_hint
            ),
            input_schema={"type": "object", "properties": {}},
            source="builtin:computer_use",
        ),
        ToolDefinition(
            name="computer_screenshot",
            description=(
                "Capture a GENUINE full-desktop screenshot on a runner — the "
                "entire screen including the OS clock/taskbar, the signed-in "
                "user identity and real browser chrome (not a headless render). "
                "This is the primary tool for compliance audit evidence. The "
                "PNG is attached as a native vision block you can read "
                "(URL bar, labels, errors, clock) AND as inline Markdown for "
                "the human; include the Markdown verbatim in your reply. Never "
                "ask the user to describe or verify the capture. The result "
                "also gives the capture's source_file_id, its SHA-256 "
                "(hash-verified end to end), and an integrity-sidecar JSON — "
                "pass those source_file_ids to an evidence tool (e.g. Drata "
                "external evidence) to file the exact captured bytes."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "runner_id": runner_id_prop,
                    "display": {
                        "type": "string",
                        "description": (
                            "Which display to capture: 'primary' (default), "
                            "'all' for a combined virtual desktop, or a monitor "
                            "index like '1'."
                        ),
                    },
                },
            },
            source="builtin:computer_use",
        ),
        ToolDefinition(
            name="computer_action",
            description=(
                "Synthesize real mouse / keyboard input on a runner's desktop "
                "(move, click, double_click, right_click, drag, scroll, type, "
                "key). Coordinates are absolute desktop pixels. Requires the "
                "'computer_action' capability."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": [
                            "move",
                            "click",
                            "double_click",
                            "right_click",
                            "drag",
                            "scroll",
                            "type",
                            "key",
                        ],
                    },
                    "x": {"type": "integer"},
                    "y": {"type": "integer"},
                    "x2": {"type": "integer", "description": "Drag end X."},
                    "y2": {"type": "integer", "description": "Drag end Y."},
                    "text": {"type": "string", "description": "Text for 'type'."},
                    "keys": {
                        "type": "string",
                        "description": "Key combo for 'key', e.g. 'ctrl+s', 'Enter'.",
                    },
                    "button": {
                        "type": "string",
                        "enum": ["left", "right", "middle"],
                    },
                    "amount": {
                        "type": "integer",
                        "description": "Scroll amount (+ up / - down).",
                    },
                    "runner_id": runner_id_prop,
                },
                "required": ["action"],
            },
            source="builtin:computer_use",
        ),
        ToolDefinition(
            name="run_shell",
            description=(
                "Run a shell command on a runner and return stdout, stderr and "
                "the exit code. On Windows the runner uses PowerShell; on "
                "macOS/Linux, the default shell. Requires the 'run_shell' "
                "capability (and any configured command allow-list). With "
                "background: true it returns at once with a job id, the pid and "
                "the first lines, and the command keeps running after the turn "
                "(a long download, a server, a VM boot): follow it with shell_jobs."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                    "cwd": {"type": "string", "description": "Working directory."},
                    "timeout": {
                        "type": "integer",
                        "description": "Max seconds before the runner aborts it (not for background).",
                    },
                    "background": {
                        "type": "boolean",
                        "description": (
                            "Start it as a background job: answered at once, output kept "
                            "on the machine, read with shell_jobs."
                        ),
                    },
                    "runner_id": runner_id_prop,
                },
                "required": ["command"],
            },
            source="builtin:computer_use",
        ),
        ToolDefinition(
            name="shell_jobs",
            description=(
                "Follow the background jobs run_shell started on a runner: "
                "'list' the jobs (id, command, status running/exited/stopped, "
                "exit code, output size); 'tail' a job's output, the end by "
                "default or from 'offset' (the next_offset of the previous read) "
                "to page through it; 'stop' a job with every process it started. "
                "Requires the 'run_shell' capability."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": list(SHELL_JOB_ACTIONS)},
                    "job_id": {"type": "string", "description": "The job id run_shell answered."},
                    "offset": {"type": "integer", "description": "tail: byte offset to read from."},
                    "max_bytes": {"type": "integer", "description": "tail: most bytes to read (64 KB at most)."},
                    "runner_id": runner_id_prop,
                },
                "required": ["action"],
            },
            source="builtin:computer_use",
        ),
        ToolDefinition(
            name="runner_read_file",
            description=(
                "Read a file from a runner's filesystem. Small text is returned "
                "inline; larger/binary content is saved as a downloadable file. "
                "Requires the 'file_ops' capability (and any allowed-paths list)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "max_bytes": {"type": "integer"},
                    "runner_id": runner_id_prop,
                },
                "required": ["path"],
            },
            source="builtin:computer_use",
            aliases=_former_names("runner_read_file"),
        ),
        ToolDefinition(
            name="runner_write_file",
            description=(
                "Write UTF-8 text to a file on a runner's filesystem. Requires "
                "the 'file_ops' capability (and any allowed-paths list)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                    "runner_id": runner_id_prop,
                },
                "required": ["path", "content"],
            },
            source="builtin:computer_use",
            aliases=_former_names("runner_write_file"),
        ),
        ToolDefinition(
            name="runner_list_dir",
            description=(
                "List the entries of a directory on a runner. Requires the "
                "'file_ops' capability (and any allowed-paths list)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "runner_id": runner_id_prop,
                },
                "required": ["path"],
            },
            source="builtin:computer_use",
            aliases=_former_names("runner_list_dir"),
        ),
        ToolDefinition(
            name="browser_control",
            description=(
                "Drive a REAL, already-authenticated local browser on a runner "
                "(not a headless render): navigate, click, type, and screenshot "
                "the actual browser window with the user's live session and the "
                "browser chrome visible. Use for audit evidence that must show "
                "the signed-in account. Requires the 'browser_control' "
                "capability. The screenshot action returns an inline image plus "
                "its source_file_id, SHA-256, live page URL/title, and an "
                "integrity-sidecar JSON for evidence filing. Actions act on one "
                "tab, kept across calls: list_tabs lists the open tabs (tab_id, "
                "title, url, active = the one the user used last, driven = the "
                "one acted on); select_tab switches to tab_id, or to the active "
                "tab without one (e.g. after the user signed in in a new tab). "
                "evaluate runs a JavaScript expression in the tab and answers the "
                "result as text. click, type and get_content take frame (a CSS "
                "selector of an iframe); a click or type the top document does not "
                "match also searches its same-origin iframes. Works on a locked "
                "session, where computer_action is refused. "
                "NOTE: a page screenshot shows the page only — when the evidence "
                "must show the OS clock/taskbar too, navigate here first, then "
                "call computer_screenshot for the full desktop."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": [
                            "navigate",
                            "screenshot",
                            "click",
                            "type",
                            "get_content",
                            "list_tabs",
                            "select_tab",
                            "evaluate",
                        ],
                    },
                    "url": {"type": "string"},
                    "selector": {"type": "string"},
                    "text": {"type": "string"},
                    "expression": {
                        "type": "string",
                        "description": "evaluate: the JavaScript expression; promises are awaited.",
                    },
                    "timeout": {
                        "type": "number",
                        "description": "evaluate: seconds it may run (default 30, at most 120).",
                    },
                    "frame": {
                        "type": "string",
                        "description": (
                            "click, type, get_content: CSS selector of the iframe to act in "
                            "(e.g. a portal blade). Omit it for the top document; a click or "
                            "type it misses then searches the same-origin iframes."
                        ),
                    },
                    "tab_id": {
                        "type": "string",
                        "description": (
                            "select_tab: the tab_id list_tabs gave. Omit it to switch to the tab the user used last."
                        ),
                    },
                    "browser": {
                        "type": "string",
                        "description": (
                            "Which installed browser to drive (e.g. 'chrome', "
                            "'edge', 'firefox'). Defaults to the system default."
                        ),
                    },
                    "runner_id": runner_id_prop,
                },
                "required": ["action"],
            },
            source="builtin:computer_use",
        ),
    ]
