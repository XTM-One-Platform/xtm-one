# SPDX-License-Identifier: Apache-2.0

"""The status a CTEM case is in, moved by the loop itself (#1503).

A case walks a fixed set of statuses (``models/ctem_case.CASE_STATES``):
``new`` -> ``analyzing`` -> ``waiting_decision`` -> ``remediating`` ->
``validating`` -> ``mitigated_watching`` / ``resolved`` (or ``accepted_risk``
by a person). No person has to move a case for its status to stay true: every
loop step that changes where the case stands writes the move itself, as one
``state`` event with its actor (the agent id, a user id or ``system``), so the
case history shows who moved it and why.

Rules here, no I/O; the writers (``ctem_case_loop``, ``ctem_cases_store``)
call :func:`move` and add the event it answers:

- :func:`after_triage`: a triage write (exploitation, attack path, threat
  package) moves a ``new`` case to ``analyzing``.
- :func:`after_gate`: a gate that waits on a person moves an open case to
  ``waiting_decision``; the answer (or an automatic pass) moves it to the
  status the gate's next step implies (:data:`GATE_NEXT`).
- :func:`after_ticket`: a ticket or a remediation plan moves the case to
  ``remediating``; the ticket done on a case with a re-test plan moves it to
  ``validating``.
- :func:`after_validation`: a pending re-test moves it to ``validating``; a
  failed one sends an open case back to ``remediating``.

A move only goes forward through the open statuses (:data:`RANK`), except the
moves out of ``waiting_decision`` and back from ``validating`` on a failed
re-test: a late event never rewinds a case.
"""

from __future__ import annotations

from datetime import datetime

from app.models.ctem_case import CLOSED_STATES, LEGACY_STATES, OPEN_STATES, CtemCase, CtemCaseEvent

#: Lifecycle order of the open statuses; a loop move never goes back in it (but see the module doc).
RANK: dict[str, int] = {state: index for index, state in enumerate(OPEN_STATES)}

#: The status a gate's next step implies once it is answered or passed (CTEM gate catalog,
#: ``builtin_fleet_docs.CTEM_GATE_KEYS``). ``None``: the gate does not move the case by itself
#: (accepting risk and closing without a re-test are a person's state change).
GATE_NEXT: dict[str, str | None] = {
    # #2798: the close at scoping is the agent's own state change, recorded with its reason.
    "ctem.scoping.not_relevant": None,
    "ctem.analyst_review": "analyzing",
    "ctem.remediation.decision": "remediating",
    "ctem.mobilize": "remediating",
    "ctem.production_change": "remediating",
    "ctem.validation.non_readonly": "validating",
    "ctem.accept_risk": None,
    "ctem.close_without_retest": None,
}

#: Ticket statuses that say the change is in flight, and the one that says it landed.
TICKET_ACTIVE = ("open", "in_progress")
TICKET_DONE = "done"


def normalize(state: str | None) -> str | None:
    """A status as the case model knows it today: the two first-model states read as their new names."""
    if state is None:
        return None
    return LEGACY_STATES.get(state, state)


def move(
    case: CtemCase,
    target: str,
    at: datetime,
    *,
    actor: str,
    reason: str | None = None,
    evidence: str | None = None,
) -> CtemCaseEvent | None:
    """Move *case* to *target* and answer its ``state`` event; ``None`` when it already is there.

    The caller adds the event to the session. Accepted-risk fields are cleared
    on any status but ``accepted_risk``.
    """
    if case.state == target:
        return None
    row = CtemCaseEvent(
        case_id=case.id,
        actor=actor,
        kind="state",
        at=at,
        from_value=case.state,
        to_value=target,
        reason=(reason or "").strip()[:1000] or None,
        evidence=(evidence or "").strip()[:1000] or None,
    )
    case.state = target
    case.state_since = at
    if target != "accepted_risk":
        case.accepted_reason = None
        case.accepted_until = None
    return row


def _forward(current: str, target: str) -> bool:
    """True when *target* is later than *current* in the open lifecycle (both open)."""
    return current in RANK and target in RANK and RANK[target] > RANK[current]


def after_triage(case: CtemCase, at: datetime, *, actor: str, what: str) -> CtemCaseEvent | None:
    """A triage write (*what*: exploitation, attack path, threat package) starts the analysis of a new case."""
    if case.state != "new":
        return None
    return move(case, "analyzing", at, actor=actor, reason=f"triage started: {what}")


def after_gate(case: CtemCase, at: datetime, *, key: str, outcome: str, by: str, actor: str) -> CtemCaseEvent | None:
    """The status a gate event implies: waited parks the case on a person, passed moves it on.

    A person's answer never moves the case by code (#1675): approve, reject or a
    free-text answer, the Gate answered run reads it and sets the next state
    itself with update_ctem_case (back to analysis, validation again, remediate,
    close with evidence). Only the bookkeeping stays here: waited parks the case
    on the person, and a gate that passed without a person moves it on.
    """
    if case.state not in OPEN_STATES:
        return None
    if outcome == "waited":
        return move(case, "waiting_decision", at, actor=actor, reason=f"waits on {by or 'a person'} at gate {key}")
    if outcome == "answered":
        return None
    target = GATE_NEXT.get(key)
    if case.state == "waiting_decision":
        # Passed without a person: the gate's next step, else the analysis it paused.
        return move(case, target or "analyzing", at, actor=actor, reason=f"gate {key} passed")
    if target is not None and _forward(case.state, target):
        return move(case, target, at, actor=actor, reason=f"gate {key} passed automatically")
    return None


def after_ticket(
    case: CtemCase, at: datetime, *, status: str, actor: str, evidence: str | None = None
) -> CtemCaseEvent | None:
    """A ticket in flight means the remediation is under way; a ticket done hands a re-test plan to validation."""
    if case.state not in OPEN_STATES:
        return None
    if status in TICKET_ACTIVE and (case.state == "waiting_decision" or _forward(case.state, "remediating")):
        return move(case, "remediating", at, actor=actor, reason="remediation ticket in flight", evidence=evidence)
    if status == TICKET_DONE and case.validation_plan and case.state in ("new", "analyzing", "remediating"):
        return move(case, "validating", at, actor=actor, reason="ticket done, re-test pending", evidence=evidence)
    return None


def after_remediation_plan(case: CtemCase, at: datetime, *, actor: str, evidence: str | None) -> CtemCaseEvent | None:
    """A treatment plan attached to the case records the remediation decided."""
    if case.state not in OPEN_STATES or not (case.state == "waiting_decision" or _forward(case.state, "remediating")):
        return None
    return move(case, "remediating", at, actor=actor, reason="remediation decided", evidence=evidence)


def after_applied_change(case: CtemCase, at: datetime, *, actor: str, evidence: str) -> CtemCaseEvent | None:
    """A change applied through an integration (#2061) means the remediation is under way."""
    if case.state not in OPEN_STATES or not (case.state == "waiting_decision" or _forward(case.state, "remediating")):
        return None
    return move(case, "remediating", at, actor=actor, reason="change applied through an integration", evidence=evidence)


def after_validation(case: CtemCase, at: datetime, *, result: str, actor: str, evidence: str) -> CtemCaseEvent | None:
    """A pending re-test puts an open case in validation; a failed one on an open case sends it back to remediation.

    Passes and the reopen of a closed case stay with ``ctem_case_loop.record_validation``.
    """
    if case.state not in OPEN_STATES:
        return None
    if result == "pending" and case.state != "validating":
        return move(case, "validating", at, actor=actor, reason="re-test pending", evidence=evidence)
    if result == "failed" and case.state == "validating":
        return move(case, "remediating", at, actor=actor, reason=f"re-test failed: {evidence}")
    return None


def reopen_target(case: CtemCase) -> str:
    """Where a closed case goes when it reopens: back to remediation when a fix was in flight, else new."""
    if case.state in CLOSED_STATES and (case.tickets or []):
        return "remediating"
    return "new"
