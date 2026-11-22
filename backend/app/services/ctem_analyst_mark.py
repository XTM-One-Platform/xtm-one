# SPDX-License-Identifier: Apache-2.0

"""The analyst's mark in OpenCTI resumes a CTEM run waiting on its review (CTEM-16, #1089).

The review step of the CTEM loop is the gate ``ctem.analyst_review``: it maps
onto the coverage request (``manage_opencti_security_coverage`` action
``create``). Out of the box the gate runs; pinned ``wait`` (or at Full human
gates) the request pauses the run as a decision of the unified system
(``dev-docs/human-gates.md``). This module is the other way to answer it: the
analyst sets the mark label (:data:`DEFAULT_MARK_LABEL`, or the policy's
``analyst_review_label``) on the Grouping in OpenCTI, and the run resumes with
the request approved, exactly as if the decision had been answered.

A provisioned OpenCTI trigger (the vulnerability fleet's listen-only
"Analyst review mark" assignment: Grouping updated) delivers to the
per-assignment webhook route, which hands every verified delivery to
:func:`note_event`. No model call, no run of its own: the delivery approves the
pending coverage request whose ``covered_id`` names the marked Grouping (its
OpenCTI id or its STIX id), on every run waiting on it, and resumes them. A
forged delivery (not verified against the integration's webhook secret)
approves nothing. The resolution entry names the mark, so the loop trace and
the decision record show who answered and how.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)

#: The tool whose ``create`` action the gate ``ctem.analyst_review`` maps onto.
COVERAGE_TOOL = "manage_opencti_security_coverage"
#: The label an analyst sets on the Grouping when the policy names none.
DEFAULT_MARK_LABEL = "analyst-reviewed"
#: The OpenCTI object type a threat package is drafted as.
MARKED_TYPE = "grouping"
#: The surface stamped on the resolution entry and the decision.
SURFACE = "opencti_mark"
#: Most waiting runs one delivery is matched against.
SCAN_LIMIT = 200


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def mark_label(policy: Any) -> str:
    """The label that marks a reviewed Grouping (the policy's, else the default), lower case."""
    label = _text(getattr(policy, "analyst_review_label", "")) if policy is not None else ""
    return (label or DEFAULT_MARK_LABEL).casefold()


def marked_grouping(payload: Mapping[str, Any], label: str) -> set[str]:
    """The ids of the Grouping a delivery marks reviewed (empty when it marks nothing). Pure."""
    data = payload.get("data") if isinstance(payload, Mapping) else None
    if not isinstance(data, Mapping) or _text(data.get("type")).casefold() != MARKED_TYPE:
        return set()
    labels = data.get("labels")
    if not isinstance(labels, list) or label not in {_text(lb).casefold() for lb in labels}:
        return set()
    ids = {_text(data.get("id"))}
    facts = payload.get("_opencti_event")
    if isinstance(facts, Mapping):
        ids |= {_text(facts.get("entity_id")), _text(facts.get("stix_id"))}
    ext = data.get("extensions")
    if isinstance(ext, Mapping):
        for value in ext.values():
            if isinstance(value, Mapping):
                ids.add(_text(value.get("id")))
    return {i for i in ids if i}


def waiting_calls(pending: Iterable[Any], grouping_ids: set[str]) -> list[str]:
    """The pending coverage requests on the marked Grouping, by ``tool_call_id``. Pure."""
    out: list[str] = []
    for proposal in pending or []:
        if not isinstance(proposal, Mapping) or proposal.get("tool_name") != COVERAGE_TOOL:
            continue
        args = proposal.get("arguments")
        if not isinstance(args, Mapping) or _text(args.get("action")) != "create":
            continue
        if _text(args.get("covered_id")) in grouping_ids and proposal.get("tool_call_id"):
            out.append(str(proposal["tool_call_id"]))
    return out


def approvals_for(pending: Iterable[Any], approved: list[str]) -> dict[str, dict[str, Any]] | None:
    """The decision set the mark gives a paused run, ``None`` when it cannot answer it. Pure.

    A run decides every pending call at once (``_apply_decisions_to_run``): the
    mark answers a pause only when every pending call is a coverage request on
    the marked Grouping, so it never approves a call it was not about.
    """
    ids = [str(p.get("tool_call_id")) for p in pending or [] if isinstance(p, Mapping)]
    if not ids or sorted(ids) != sorted(approved):
        return None
    return {i: {"tool_call_id": i, "decision": "approve", "rejection_reason": None} for i in ids}


def resolution_entry(question: str | None, label: str, approved: list[str], now: datetime) -> dict[str, Any]:
    """The ``human_input_history`` entry of an answer given by the mark. Pure."""
    return {
        "question": question,
        "response": f"Approved by the analyst's mark '{label}' in OpenCTI: " + ", ".join(approved),
        "resolution": "answered",
        "responded_at": now.isoformat(),
        "responded_by": "OpenCTI analyst mark",
        "responded_by_id": None,
        "surface": SURFACE,
    }


async def resume_marked(db: Any, grouping_ids: set[str], label: str, *, now: datetime | None = None) -> list[str]:
    """Approve the coverage requests waiting on the marked Grouping and resume their runs; the run ids."""
    from sqlalchemy import select

    from app.models.assignment import AssignmentRun
    from app.models.decision_hooks import answer_surface
    from app.services.assignment_engine import resume_assignment_run
    from app.services.push_action_tokens import void_run_tokens

    now = now or datetime.now(UTC)
    rows = (
        (
            await db.execute(
                select(AssignmentRun)
                .where(AssignmentRun.status == "awaiting_human", AssignmentRun.pending_approvals.isnot(None))
                .order_by(AssignmentRun.created_at.desc())
                .limit(SCAN_LIMIT)
            )
        )
        .scalars()
        .all()
    )
    resumed: list[str] = []
    for candidate in rows:
        if not waiting_calls(candidate.pending_approvals, grouping_ids):
            continue
        # The row lock of every answer path: a person answering the same pause
        # at the same moment is serialised, and the second answer finds it decided.
        run = (
            await db.execute(select(AssignmentRun).where(AssignmentRun.id == candidate.id).with_for_update())
        ).scalar_one_or_none()
        if run is None or run.status != "awaiting_human" or run.approval_decisions:
            continue
        approved = waiting_calls(run.pending_approvals, grouping_ids)
        decisions = approvals_for(run.pending_approvals, approved)
        if decisions is None:
            continue
        # The decision hook stamps the decision row and its ``answered`` event
        # with the surface of this contextvar, as the router does for its answers.
        token = answer_surface.set(SURFACE)
        try:
            run.approval_decisions = decisions
            run.human_input_history = [
                *(run.human_input_history or []),
                resolution_entry(run.human_input_question, label, approved, now),
            ]
            await resume_assignment_run(db, run)
        finally:
            answer_surface.reset(token)
        # Decided: the Approve / Reject buttons already pushed to a phone stop
        # working at once, as on every other answer path.
        await void_run_tokens(run.id)
        resumed.append(str(run.id))
    return resumed


async def note_event(provider: str, payload: Mapping[str, Any], *, verified: bool) -> list[str]:
    """Resume the runs an OpenCTI delivery's analyst mark answers; never raises. The run ids."""
    from app.core.config import settings

    if provider != "opencti" or not verified or settings.is_copilot_like:
        return []
    data = payload.get("data") if isinstance(payload, Mapping) else None
    if not isinstance(data, Mapping) or _text(data.get("type")).casefold() != MARKED_TYPE:
        return []
    from app.core.database import async_session
    from app.services.ctem_trigger_rules import _primary_profile

    try:
        async with async_session() as db:
            profile = await _primary_profile(db)
            label = mark_label(profile.prioritization_policy if profile is not None else None)
            ids = marked_grouping(payload, label)
            if not ids:
                return []
            return await resume_marked(db, ids, label)
    except Exception:
        logger.warning("CTEM runs not resumed by an OpenCTI analyst mark", exc_info=True)
        return []
