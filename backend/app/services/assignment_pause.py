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

"""Pause provenance for assignments.

An assignment that stops firing shows up in the UI as a "Paused" badge and
nothing else.  Seven unrelated code paths write ``Assignment.enabled = False``:

===================== ====================================================
``user_toggle``       a human flipped the switch (REST ``PUT /assignments``)
``agent_tool``        an agent called the ``update_assignment`` builder tool
``auth_quarantine``   the engine quarantined it after N consecutive runs
                      whose integration tool calls failed authentication
                      (``assignment_engine._track_assignment_auth_failures``)
``stale_reference``   the engine paused it after N consecutive scheduled
                      runs failed on a resource their integration reports
                      missing (``assignment_engine._track_stale_reference_
                      failure``)
``invalid_schedule``  the scheduler could not parse its cron expression
                      (``autonomous._flag_assignment_cron_misconfiguration``)
``created_disabled``  it was created switched off on purpose — an agent
                      duplicate, an agent import, or a catalog install, all of
                      which start disabled so a copied automation cannot fire
                      before it has been reviewed
``system_seed``       startup seeding defines this built-in assignment as
                      disabled in the current deployment configuration
                      (``agents/seed.py``)
``panic_stop``        a human pressed the emergency stop — every assignment in
                      their scope was paused at once (``services/panic.py``);
                      the only reason the bulk resume re-enables
``retire_after_run``  ``delete_assignment(after_run=true)`` while a run was
                      in flight: deleted on the scheduler tick once its runs
                      end (``services/assignment_retire.py``)
===================== ====================================================

Before this module, telling those six apart after the fact was impossible:
the first two were indistinguishable inside a generic "Updated assignment"
audit row, the next two left only a log line on a worker nobody reads, and a
copy or a seeded row born switched off looked exactly like one somebody had
switched off.

Every pause now goes through :func:`pause_assignment` and every resume through
:func:`resume_assignment`.  Together they maintain two records:

* **The current state** — the ``paused_*`` columns on the assignment, set and
  cleared as one block so they are never stale on an enabled assignment.  This
  is what the assignment page renders.
* **The history** — an ``audit_logs`` row per transition, action ``pause`` /
  ``resume`` on entity type ``assignment``, carrying the reason code and the
  details.  A pause with a human actor is recorded under that person.  An
  autonomous pause is never attributed to one: ``AuditLog.user_id`` is NOT
  NULL, and naming the owner as the actor would be a lie in the one table
  that must not contain any.  :func:`record_system_pause_audit` writes it
  under a SYSTEM actor instead (``user_email = system:<component>``,
  ``services.audit.log_system_audit``), anchored on the user it concerns;
  the stale-reference pause uses it.  The others stay traceable
  through the columns, the ``Activity`` notification the engine writes, and
  the WARNING logged here.

Neither record is allowed to break the caller: audit failures are swallowed
(the pause itself must still commit), and the columns are plain in-session
mutations the caller commits with the rest of its transaction.

One pause is NOT among these reasons and is never written: an assignment
whose owner's account is deactivated, unless it is company-managed,
and the same for a flow. See the "Owner deactivation" section at the end of this
module.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Literal, Protocol

from fastapi import HTTPException, Request, status
from sqlalchemy import ColumnElement, exists, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.assignment import Assignment
from app.models.flow import Flow
from app.models.user import User

logger = logging.getLogger(__name__)

PauseReason = Literal[
    "user_toggle",
    "agent_tool",
    "auth_quarantine",
    "stale_reference",
    "invalid_schedule",
    "created_disabled",
    "system_seed",
    "panic_stop",
    "retire_after_run",
    "fleet_pause",
    "owner_deactivated",
]

#: The reason stamped by the emergency stop (``services/panic.py``). A module
#: constant so the stop, the bulk resume and the status probe agree on the
#: one string that makes a panic pause distinguishable from a manual one.
PANIC_STOP_REASON = "panic_stop"

#: Reason code -> short English label. The frontend translates these itself
#: (it knows the codes); the label here is what lands in logs and in the audit
#: summary, which are read by operators, not end users.
PAUSE_REASONS: dict[str, str] = {
    "user_toggle": "paused by a user",
    "agent_tool": "paused by an agent",
    "auth_quarantine": "auto-paused after repeated integration auth failures",
    "stale_reference": "auto-paused after repeated stale integration references",
    "invalid_schedule": "auto-paused because its schedule is invalid",
    "created_disabled": "created switched off, awaiting review",
    "system_seed": "switched off by the deployment configuration",
    "panic_stop": "paused by an emergency stop",
    "retire_after_run": "to be deleted once its runs end",
    "concierge_switch_over": "replaced by a flow after an approved Platform Health proposal",
    "fleet_pause": "paused with every automation of its fleet",
    "owner_deactivated": "paused because the account it acts as was deactivated",
}

#: The reason stamped by the user-deactivation cascade
#: (``services/user_deactivation.py``) on an assignment that would act with a
#: deactivated account's identity. Lifted only by that account's reactivation,
#: which re-enables exactly the assignments the cascade recorded.
OWNER_DEACTIVATED_REASON = "owner_deactivated"

#: The reason stamped by a fleet's "Pause all" (``services/fleet_control.py``):
#: the fleet's "Resume" re-enables exactly the assignments carrying it.
FLEET_PAUSE_REASON = "fleet_pause"

#: Reasons with no human actor. Never attributed to a user (an audit row, when
#: one is written, goes through :func:`record_system_pause_audit`) and always
#: carry a component name as ``paused_by_label``. Enforced in
#: :func:`pause_assignment` — a system reason arriving with a user id means a
#: caller is about to attribute an autonomous decision to a person, which is
#: the one mistake an audit feature must not make quietly.
SYSTEM_REASONS: frozenset[str] = frozenset(
    {
        "auth_quarantine",
        "stale_reference",
        "invalid_schedule",
        "system_seed",
        "concierge_switch_over",
        "owner_deactivated",
    }
)

#: Reasons that are ROUTINE rather than anomalous — a copy born switched off,
#: a seed row the deployment defines as disabled. These log at INFO; every
#: other reason means something a human may need to act on and logs at WARNING.
#: Bulk operations (duplicating an agent with N assignments, importing a
#: bundle) go through here N times, and N WARNINGs for expected behaviour is
#: how a log stops being read.
EXPECTED_REASONS: frozenset[str] = frozenset({"created_disabled", "system_seed"})

#: The routine stamp of an automation that is off because it is not set up
#: yet: born switched off, switched off before its first run, or set back to
#: "not set up" with its fleet (``fleet_control.unset_fleet``). A fleet whose
#: automations are all off with this stamp reads "Not set up", never "Paused".
CREATED_DISABLED_REASON = "created_disabled"


#: The stamps of a switch-off a person decided: from the UI, or by an agent
#: acting as them (``created_disabled`` when it had never run). The seed keeps
#: such a pause on a built-in assignment instead of reverting it at boot (#2192).
PERSON_PAUSE_REASONS: frozenset[str] = frozenset({"user_toggle", "agent_tool", CREATED_DISABLED_REASON})


def is_paused_by_person(assignment: Assignment) -> bool:
    """Whether *assignment* is off because a person switched it off.

    A ``created_disabled`` stamp counts only with a person on it: the seed and
    copies stamp it with nobody.
    """
    if assignment.enabled or assignment.paused_reason not in PERSON_PAUSE_REASONS:
        return False
    return assignment.paused_by_user_id is not None


def has_never_run(assignment: Assignment) -> bool:
    """Whether *assignment* has never been triggered: no run counted, no trigger time.

    Read from the two columns every run start writes
    (``assignment_engine`` increments ``execution_count`` and sets
    ``last_triggered_at``), so no query is needed. Both are required: the
    counter can be reset by hand while the trigger time stays. Anything that
    is not a plain integer count reads as "has run", the safe side.
    """
    count = getattr(assignment, "execution_count", None)
    if count is not None and (not isinstance(count, int) or isinstance(count, bool) or count != 0):
        return False
    return getattr(assignment, "last_triggered_at", None) is None


def switch_off_reason(assignment: Assignment, toggle_reason: PauseReason | str) -> str:
    """The stamp of a person's or an agent's switch-off: ``created_disabled`` before the first run.

    An automation switched off before it ever ran is still being set up: stamping
    it ``user_toggle`` / ``agent_tool`` would make its fleet read "Paused" for
    ever although nothing was ever on (#11060).
    """
    return CREATED_DISABLED_REASON if has_never_run(assignment) else str(toggle_reason)


def is_emergency_stopped(assignment: Assignment) -> bool:
    """Whether *assignment* is held paused by an emergency stop.

    The one predicate behind every path that could re-enable an assignment:
    the power toggle, the agent-builder tool and startup seeding. Only the
    emergency-stop resume (``services/panic.py``) lifts such a pause.
    """
    return not assignment.enabled and assignment.paused_reason == PANIC_STOP_REASON


def ensure_not_emergency_stopped(assignment: Assignment) -> None:
    """Refuse a generic re-enable of an assignment the emergency stop paused.

    The stop's control boundary would otherwise be undone one assignment at a
    time through the ordinary power toggle (or the agent-builder tool), with
    no resume reason recorded. The one way back is the emergency-stop resume
    (``services/panic.py``), which asks why and lifts the whole scope.
    """
    if is_emergency_stopped(assignment):
        from fastapi import HTTPException

        raise HTTPException(
            status_code=409,
            detail={
                "code": "emergency_stop",
                "message": (
                    "This assignment was paused by an emergency stop. Lift the "
                    "emergency stop (profile menu or banner) to resume it."
                ),
            },
        )


def actor_display_name(user: User) -> str:
    """Display label for a human actor — name first, email only as a fallback.

    ``paused_by_label`` is rendered on the assignment page to everyone who can
    read the assignment, which on a company-managed or group-shared one is a
    broader audience than the admin-scoped audit log. An email address is
    needlessly identifying for a label whose only job is "who switched this
    off"; the precise identity is still recorded as ``paused_by_user_id`` (and
    in the audit row, which carries the email as it always has).

    Mirrors the existing ``responded_by`` convention on the HITL history.
    """
    return (user.full_name or "").strip() or user.email


def pause_assignment(
    assignment: Assignment,
    *,
    reason: PauseReason | str,
    details: str,
    actor_user_id: uuid.UUID | str | None = None,
    actor_label: str,
    now: datetime | None = None,
) -> None:
    """Flip *assignment* to paused and stamp WHO/WHAT/WHEN/WHY on it.

    Pure in-session mutation — the caller owns the commit, so the provenance
    can never be committed apart from the ``enabled = False`` it explains.

    Parameters
    ----------
    reason
        A key of :data:`PAUSE_REASONS`. An unknown value is logged and stored
        anyway (truncated to the column width): losing the pause record over a
        typo would be worse than storing an unrecognized code.
    details
        Already-composed, human-readable explanation shown in the UI.
    actor_user_id
        The human behind the pause (the one who toggled it, or the one an agent
        acted as). ``None`` for autonomous system pauses.
    actor_label
        Display label — a user's email, an agent name, or a component name.
    """
    if reason not in PAUSE_REASONS:
        logger.warning(
            "Unknown assignment pause reason %r for assignment '%s' — stored "
            "truncated to the column width",
            reason,
            assignment.name,
        )
    if reason in SYSTEM_REASONS and actor_user_id is not None:
        # A system reason means nobody decided this. Attributing it to a person
        # anyway is the one mistake an audit feature must not make quietly, so
        # drop the id rather than persist a false accusation — and say so.
        logger.warning(
            "Assignment pause reason %r is autonomous but was given actor "
            "user id %s for assignment '%s' — dropping the attribution",
            reason,
            actor_user_id,
            assignment.name,
        )
        actor_user_id = None
    assignment.enabled = False
    assignment.paused_at = now or datetime.now(UTC)
    assignment.last_paused_at = assignment.paused_at
    # String(32) column. A known reason is well under it; an unknown one is
    # arbitrary caller input, and letting it overflow would raise on flush and
    # lose the entire pause record — the exact outcome the tolerant branch
    # above exists to avoid.
    assignment.paused_reason = str(reason)[:32] if reason else None
    assignment.paused_by_user_id = (
        uuid.UUID(str(actor_user_id)) if actor_user_id else None
    )
    # String(320) column: truncate rather than let an oversized label abort
    # the very write that records why the assignment stopped.
    assignment.paused_by_label = actor_label[:320] if actor_label else None
    assignment.paused_details = details or None
    # Level by reason, not one blanket WARNING: ``created_disabled`` and
    # ``system_seed`` are ROUTINE — duplicating an agent with N assignments,
    # importing a bundle or booting the platform would otherwise emit N
    # WARNING lines for expected behaviour, which is how a log stops being
    # read. The anomalous reasons stay at WARNING.
    log = logger.info if reason in EXPECTED_REASONS else logger.warning
    log(
        "Assignment '%s' (%s) paused — reason=%s actor=%s user_id=%s: %s",
        assignment.name,
        # A not-yet-flushed row has no PK: the column default is applied at
        # INSERT, so the copy / import / catalog / seed-insert paths reach here
        # with ``id`` still None. Say "unsaved" rather than print None.
        assignment.id or "unsaved",
        reason,
        actor_label,
        actor_user_id or "-",
        details,
    )


def clear_pause_provenance(assignment: Assignment) -> None:
    """Drop the pause stamp — used when an assignment is re-enabled.

    Separate from :func:`resume_assignment` so non-interactive re-enable paths
    that have no acting user and no audit story (startup seeding force-enabling
    a built-in assignment) can clear the stamp without inventing an actor.
    Leaving it behind would make an *enabled* assignment claim it is paused.
    """
    assignment.paused_at = None
    assignment.paused_reason = None
    assignment.paused_by_user_id = None
    assignment.paused_by_label = None
    assignment.paused_details = None


def mark_schedule_resumed(assignment: Assignment, now: datetime | None = None) -> None:
    """Anchor the schedule at the moment the assignment is enabled again.

    Called on a disabled-to-enabled transition only: the cron then advances
    from this instant (``core.utils.schedule_base``), so enabling never fires
    a run by itself and the occurrences missed while it was off are not
    caught up. A deliberate immediate run stays ``trigger_assignment``.
    """
    assignment.schedule_resumed_at = now or datetime.now(UTC)


async def record_pause_audit(
    db: AsyncSession,
    *,
    assignment: Assignment,
    user: User,
    reason: PauseReason | str,
    details: str,
    request: Request | None = None,
) -> None:
    """Append a ``pause`` row to the audit trail. Best-effort.

    Kept apart from :func:`pause_assignment` because the state stamp is a
    synchronous field write every caller can do, while the audit row needs a
    session, a resolved :class:`User` and (optionally) the HTTP request. A
    failure here must never roll back the pause, so everything is swallowed.
    """
    await _log_transition(
        db,
        assignment=assignment,
        user=user,
        action="pause",
        summary=(
            f"Paused assignment '{assignment.name}' "
            f"({PAUSE_REASONS.get(reason, reason)})"
        ),
        reason=reason,
        details=details,
        request=request,
    )


async def record_pause_audits(
    db: AsyncSession,
    *,
    assignments: Iterable[Assignment],
    user: User,
    reason: PauseReason | str,
    details: str,
    request: Request | None = None,
) -> None:
    """The ``pause`` rows of :func:`record_pause_audit`, written in one flush.

    For a caller pausing many assignments at once while it holds a lock (the
    emergency stop's barrier): one row per assignment costs a flush and a
    savepoint each there. Same rows and the same best-effort contract: a
    failure loses the rows, never the pauses.
    """
    assignments = list(assignments)
    if not assignments:
        return
    try:
        from app.services.audit import audit_entry

        label = PAUSE_REASONS.get(reason, reason)
        entries = [
            audit_entry(
                user=user,
                action="pause",
                entity_type="assignment",
                entity_id=assignment.id,
                entity_name=assignment.name,
                summary=f"Paused assignment '{assignment.name}' ({label})",
                details=_transition_details(assignment, reason, details),
                request=request,
            )
            for assignment in assignments
        ]
        await db.flush()
        async with db.begin_nested():
            db.add_all(entries)
            await db.flush()
    except Exception:
        logger.warning(
            "Could not write the 'pause' audit entries of %d assignment(s) — the "
            "pauses themselves are unaffected",
            len(assignments),
            exc_info=True,
        )


async def record_system_pause_audit(
    db: AsyncSession,
    *,
    assignment: Assignment,
    actor: str,
    anchor_user_id: uuid.UUID | str,
    reason: PauseReason | str,
    details: str,
    extra: dict | None = None,
) -> None:
    """Append a ``pause`` row for an autonomous pause, under a system actor. Best-effort.

    *anchor_user_id* is the user the pause concerns — the owner, or for a
    company-managed assignment the identity its runs execute as — because
    ``AuditLog.user_id`` cannot be empty. The actor recorded is *actor*,
    never them.
    """
    await _log_transition(
        db,
        assignment=assignment,
        user=None,
        system_actor=actor,
        anchor_user_id=anchor_user_id,
        action="pause",
        summary=(
            f"Paused assignment '{assignment.name}' "
            f"({PAUSE_REASONS.get(reason, reason)})"
        ),
        reason=reason,
        details=details,
        request=None,
        extra=extra,
    )


async def resume_assignment(
    db: AsyncSession,
    *,
    assignment: Assignment,
    user: User,
    request: Request | None = None,
    note: str | None = None,
) -> None:
    """Re-enable *assignment*, clear its pause stamp and audit the resume.

    The resume row matters as much as the pause row: "this has been paused
    since Tuesday" and "this was paused and un-paused four times today" are
    very different problems, and only the audit trail can tell them apart.

    ``note`` is the human's stated reason for resuming (the emergency-stop
    lift asks for one); it lands in the audit row's ``details.note``.
    """
    previous_reason = assignment.paused_reason
    previous_details = assignment.paused_details
    assignment.enabled = True
    clear_pause_provenance(assignment)
    mark_schedule_resumed(assignment)
    logger.info(
        "Assignment '%s' (%s) resumed by %s (was paused: reason=%s)",
        assignment.name,
        assignment.id,
        user.email,
        previous_reason or "unknown",
    )
    await _log_transition(
        db,
        assignment=assignment,
        user=user,
        action="resume",
        summary=f"Resumed assignment '{assignment.name}'",
        reason=previous_reason,
        details=previous_details,
        request=request,
        extra={"note": note} if note else None,
    )


def _transition_details(
    assignment: Assignment,
    reason: str | None,
    details: str | None,
    extra: dict | None = None,
) -> dict:
    """The ``details`` of a pause / resume audit row."""
    return {
        "agent_id": str(assignment.agent_id),
        "trigger_type": assignment.trigger_type,
        "reason": reason,
        "reason_label": PAUSE_REASONS.get(reason or "", reason),
        "details": details,
        **(extra or {}),
    }


async def _log_transition(
    db: AsyncSession,
    *,
    assignment: Assignment,
    user: User | None,
    action: str,
    summary: str,
    reason: str | None,
    details: str | None,
    request: Request | None,
    system_actor: str | None = None,
    anchor_user_id: uuid.UUID | str | None = None,
    extra: dict | None = None,
) -> None:
    """Write one pause/resume audit row, swallowing every failure.

    Under *user* when a person acted, under *system_actor* (anchored on
    *anchor_user_id*) when nobody did.

    Swallowing the exception is not enough on its own: ``log_audit`` ends in
    ``db.flush()``, and a failed flush leaves the ``AsyncSession`` needing a
    rollback, so the caller's next ``commit()`` would raise
    ``PendingRollbackError`` — a 500, and the pause lost. That is the exact
    opposite of this module's guarantee.

    So the caller's pending state is flushed FIRST (the ``enabled = False``
    and its stamp reach the outer transaction), then the audit insert — its
    own flush included — is wrapped in a SAVEPOINT, confining any failure to
    this one row. Same shape as ``assignment_engine._handle_assignment_auth_
    failure`` and ``kb_sync_scheduler._emit_sync_notification``.
    """
    try:
        from app.services.audit import log_audit, log_system_audit

        audit_details = _transition_details(assignment, reason, details, extra)
        await db.flush()
        async with db.begin_nested():
            if user is not None:
                await log_audit(
                    db,
                    user=user,
                    action=action,
                    entity_type="assignment",
                    entity_id=assignment.id,
                    entity_name=assignment.name,
                    summary=summary,
                    details=audit_details,
                    request=request,
                )
            else:
                await log_system_audit(
                    db,
                    actor=system_actor or "platform",
                    anchor_user_id=anchor_user_id,
                    action=action,
                    entity_type="assignment",
                    entity_id=assignment.id,
                    entity_name=assignment.name,
                    summary=summary,
                    details=audit_details,
                )
    except Exception:
        logger.warning(
            "Could not write the '%s' audit entry for assignment '%s' — the "
            "state change itself is unaffected",
            action,
            assignment.name,
            exc_info=True,
        )


# ---------------------------------------------------------------------------
# Owner deactivation
# ---------------------------------------------------------------------------
#
# An assignment whose owner's account is deactivated does not run, from any
# trigger, unless it is company-managed. Unlike every pause above it is DERIVED, never written: the
# owner's ``is_active`` decides it at the moment a trigger fires, while
# ``enabled`` and the ``paused_*`` stamp keep describing the owner's own
# choices. Reactivating the account therefore resumes each assignment exactly
# as the owner left it, and one the owner had switched off stays off.
#
# Company-managed assignments are exempt: they belong to the organisation, and
# their ``user_id`` (always set, see the model) is only who created them. Every
# other one pauses, shared with a group or a person or not: the people who rely
# on a shared one get it back through an administrator, who transfers its agent
# to an active owner or makes it company-managed.
#
# The rule has ONE definition, the SQL clause the discovery queries filter on
# and the lists select as a column (:func:`owner_not_deactivated`). The batch
# form (:func:`paused_entity_ids`) and, built on it, the instance form of the
# trigger funnels (:func:`is_paused_by_deactivated_owner`) run that clause, so
# they cannot disagree with it; they only skip the query for an entity whose
# own columns already exempt it.
#
# Flows follow the SAME rule, not a copy of it: a flow carries the same
# two columns, and the SQL form takes the model it filters. A company-managed
# flow may keep its creator's ``user_id`` (``toggle_company_managed_list_field``
# preserves it), which is why the exemption reads ``is_company_managed``,
# never the owner.

#: ``detail.code`` of the refusal, alongside the agentic gate's ``ai_disabled``
#: / ``license_required`` codes, so a client branches on a value.
OWNER_DEACTIVATED_CODE = "owner_deactivated"

OWNER_DEACTIVATED_MESSAGE = (
    "This assignment is paused because its owner's account is deactivated. "
    "It resumes when the account is reactivated."
)

#: ``error`` of a run that existed before the deactivation and was ended when it
#: would have started: skipped, not failed - nothing broke.
OWNER_DEACTIVATED_RUN_ERROR = (
    "Not run: this assignment is paused because its owner's account is "
    "deactivated. It resumes when the account is reactivated."
)

#: ``error`` of a run that had already started (resumed after a partner's
#: callback or a human answer, re-claimed after a lost lease) and was stopped
#: when it would have gone on: it did part of its work, so not "Not run".
OWNER_DEACTIVATED_RESUME_ERROR = (
    "Stopped before resuming: this assignment is paused because its owner's "
    "account is deactivated. It resumes when the account is reactivated."
)


FLOW_OWNER_DEACTIVATED_MESSAGE = (
    "This flow is paused because its owner's account is deactivated. "
    "It resumes when the account is reactivated."
)

#: ``result_summary`` of a flow run that was queued before the deactivation and
#: was ended when it would have started: skipped, not failed.
FLOW_OWNER_DEACTIVATED_RUN_ERROR = (
    "Not run: this flow is paused because its owner's account is "
    "deactivated. It resumes when the account is reactivated."
)


class OwnedEntity(Protocol):
    """What the rule reads: an assignment or a flow."""

    id: uuid.UUID
    user_id: uuid.UUID | None
    is_company_managed: bool


class OwnerDeactivatedError(HTTPException):
    """The refusal of a trigger on a paused entity: 409 ``owner_deactivated``.

    An ``HTTPException`` like the agentic gate's refusals, so a request path
    that does not catch it still answers with the reason rather than a 500.
    """

    def __init__(self, message: str) -> None:
        super().__init__(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": OWNER_DEACTIVATED_CODE, "message": message},
        )


class AssignmentOwnerDeactivatedError(OwnerDeactivatedError):
    """The refusal on a paused assignment."""

    def __init__(self, assignment: Assignment) -> None:
        super().__init__(OWNER_DEACTIVATED_MESSAGE)
        self.assignment_id = assignment.id


class FlowOwnerDeactivatedError(OwnerDeactivatedError):
    """The refusal on a paused flow."""

    def __init__(self, flow: OwnedEntity) -> None:
        super().__init__(FLOW_OWNER_DEACTIVATED_MESSAGE)
        self.flow_id = flow.id


def owner_not_deactivated(
    model: type[Assignment] | type[Flow] = Assignment,
) -> ColumnElement[bool]:
    """SQL form of the rule - its definition - for the queries that pick
    *model* rows to fire.

    True for a company-managed row, and for any other one unless an owner row
    with ``is_active = false`` exists. ``NOT EXISTS`` rather than
    ``EXISTS (... is_active)`` so a row without an owner passes.
    """
    return or_(
        model.is_company_managed.is_(True),
        ~exists().where(User.id == model.user_id, User.is_active.is_(False)),
    )


def _owner_pausable(entity: OwnedEntity) -> bool:
    """Whether *entity*'s own columns leave the rule anything to decide:
    company-managed and ownerless ones are exempt without a query."""
    return not entity.is_company_managed and entity.user_id is not None


async def paused_entity_ids(
    db: AsyncSession, entities: Iterable[OwnedEntity]
) -> set[uuid.UUID]:
    """The ids of the *entities* the rule pauses (batch form).

    One query per model, running :func:`owner_not_deactivated` itself; none
    when no entity is left to decide.
    """
    candidates: dict[type[Assignment] | type[Flow], set[uuid.UUID]] = {}
    for entity in entities:
        if _owner_pausable(entity):
            model = Flow if isinstance(entity, Flow) else Assignment
            candidates.setdefault(model, set()).add(entity.id)
    paused: set[uuid.UUID] = set()
    for model, ids in candidates.items():
        rows = await db.execute(
            select(model.id).where(model.id.in_(ids), ~owner_not_deactivated(model))
        )
        paused.update(row[0] for row in rows.all())
    return paused


async def is_paused_by_deactivated_owner(
    db: AsyncSession, entity: OwnedEntity
) -> bool:
    """Instance form of the rule: the batch form for one entity."""
    if not _owner_pausable(entity):
        return False
    return entity.id in await paused_entity_ids(db, [entity])


async def refuse_if_owner_deactivated(db: AsyncSession, assignment: Assignment) -> None:
    """Raise :class:`AssignmentOwnerDeactivatedError` when the rule applies."""
    if await is_paused_by_deactivated_owner(db, assignment):
        logger.info(
            "Assignment '%s' (%s) not triggered: its owner's account %s is "
            "deactivated",
            assignment.name,
            assignment.id,
            assignment.user_id,
        )
        raise AssignmentOwnerDeactivatedError(assignment)


async def refuse_flow_if_owner_deactivated(db: AsyncSession, flow: Flow) -> None:
    """Raise :class:`FlowOwnerDeactivatedError` when the rule applies."""
    if await is_paused_by_deactivated_owner(db, flow):
        logger.info(
            "Flow '%s' (%s) not run: its owner's account %s is deactivated",
            flow.name,
            flow.id,
            flow.user_id,
        )
        raise FlowOwnerDeactivatedError(flow)


async def is_flow_paused_by_deactivated_owner(
    db: AsyncSession, flow_id: uuid.UUID
) -> bool:
    """The rule for a flow known by id, in one statement (the run gate)."""
    paused = await db.execute(
        select(Flow.id).where(Flow.id == flow_id, ~owner_not_deactivated(Flow))
    )
    return paused.first() is not None
