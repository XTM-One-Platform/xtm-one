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

"""Fleet health findings: what an orchestrator advises its fleet's owner on (#11700).

``build_fleet_health`` (:mod:`app.services.fleet_views`) answers one verdict per
member. This module adds the findings an owner acts on, each with the ids it
was read from, so one read tells a fleet orchestrator everything the seeded
``fleet-health-advisory`` skill classifies:

* ``member_failing`` / ``member_disabled``: the member verdicts that are faults.
* ``assignment_failing``: an enabled assignment of a home member whose last
  :data:`FAILING_RUNS` judged runs all failed (a platform interruption is not
  judged, as in :func:`app.services.run_outcome.member_verdict`).
* ``assignment_silent``: an enabled schedule assignment older than
  :data:`SILENT_AFTER` that has not fired for that long.
* ``orphaned_claim``: an open item claimed or in progress by a run that ended.
  The one SAFE fix: :func:`apply_safe_fixes` releases it with the platform's own
  sweep (``work_item.release_ended_run_claims``), scoped to those runs.
* ``past_due``, ``blocked_without_blocker``: open items nothing will move.
* ``not_picked_up``: an open, due item no inbox will take: no owner, an owner
  with no work-item inbox, or an owner whose inboxes all filter it out (its
  correlation prefix or priority), read with the dispatch's own rule
  (:func:`app.services.work_item_context.pickup_state`). An item its owner's
  enabled ``platform_event`` assignment hears on ``work_item.*`` events through a
  ``correlation_key`` filter is worked by that listener and left out.
* ``decision_card_not_readable``: an open item waiting for a person whose ask
  is missing, incomplete or holds ids and timestamps
  (:func:`app.services.work_item_ask_backfill.card_unreadable`, #776).
* ``wait_cycle``: blocked items waiting on each other, through ``blocked_on`` or a
  pull request a review item gates (:mod:`app.services.work_item_wait_cycles`, #606).
* ``blocked_chain_stalled``: a chain of blocked items waiting on each other, every
  one quiet for two hours and none held by a live run.
* ``disabled_owner``: a disabled member that still owns open items.
* ``oversize_state``: an active shared-state entry of the fleet over the soft
  size limit (``shared_state_value_soft_limit_chars``).
* ``handover_not_allowed``: a handover into a non-exposed member
  (:func:`app.services.fleets.fleet_boundary_violations`).
* ``cost_spike``: a member whose tokens of the last day are over
  :data:`SPIKE_FACTOR` times its own daily average of the week before.

Reads only, on the caller's session and under the caller's visibility (the
runs of ``run_visible_filter``, the work items and entries the caller may
read); a fleet with no finding is healthy and costs no model call when an
activation check gates on it.

Every finding carries a stable ``id`` (:func:`app.services.fleet_health_dismissals.finding_id`);
a finding the fleet's owner dismissed (``Fleet.health_dismissals``, #12014) is left
out of ``findings`` and of ``healthy`` and listed under ``dismissed``.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import String, and_, cast, func, not_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.fleet import Fleet
from app.services.fleet_health_dismissals import finding_id, split_findings
from app.services.run_outcome import OUTCOME_FAILED, OUTCOME_OK, run_outcome

#: Judged runs in a row that make an assignment failing.
FAILING_RUNS = 3
#: How long an enabled schedule may stay silent before it is reported.
SILENT_AFTER = timedelta(days=14)
#: A day's tokens over this many times the member's own daily average is a spike.
SPIKE_FACTOR = 3.0
#: Below this many tokens in the day no spike is reported, whatever the ratio.
SPIKE_MIN_TOKENS = 200_000
#: Evidence ids kept per finding (the count is always whole).
MAX_EVIDENCE = 20

#: How each kind is fixed: ``auto`` (the orchestrator applies it, safe and
#: reversible), ``owner`` (the fleet's owner decides).
FIX_AUTO = "auto"
FIX_OWNER = "owner"
FINDING_FIX: dict[str, str] = {
    "member_failing": FIX_OWNER,
    "member_disabled": FIX_OWNER,
    "assignment_failing": FIX_OWNER,
    "assignment_silent": FIX_OWNER,
    "orphaned_claim": FIX_AUTO,
    "past_due": FIX_OWNER,
    "not_picked_up": FIX_OWNER,
    "blocked_without_blocker": FIX_OWNER,
    "decision_card_not_readable": FIX_OWNER,
    "wait_cycle": FIX_OWNER,
    "blocked_chain_stalled": FIX_OWNER,
    "disabled_owner": FIX_OWNER,
    "oversize_state": FIX_OWNER,
    "handover_not_allowed": FIX_OWNER,
    "cost_spike": FIX_OWNER,
}

#: The recommendation each kind carries, one line.
FINDING_ADVICE: dict[str, str] = {
    "member_failing": "Open the member's last failed run and fix its cause before the next trigger.",
    "member_disabled": "Enable the member or remove it from the fleet.",
    "assignment_failing": "Read the last failed run of the assignment; fix it or pause the assignment.",
    "assignment_silent": "Check the schedule and its gate; delete the assignment if its work is over.",
    "orphaned_claim": "Released by the platform's claim sweep: the item goes back to its owner's inbox.",
    "past_due": "Move the due date or hand the item to someone who can finish it.",
    "not_picked_up": (
        "Give it an owner whose work-item inbox takes its correlation key and priority, or widen that "
        "inbox; cancel it when nobody needs it."
    ),
    "blocked_without_blocker": (
        "Give it an owner and a blocker the platform releases (blocked_on, blocked_on_refs), or park it for a "
        "person with needs_human and an ask card; else reopen it (a deferral is open with not_before)."
    ),
    "decision_card_not_readable": (
        "Rewrite the item's ask for its reader: a plain question, 2 to 4 context lines, 2 options or more; "
        "ids and timestamps go in blocked_reason."
    ),
    "wait_cycle": "Block the review on the fix item and let the merge wait on the review, or drop one wait.",
    "blocked_chain_stalled": "Read the chain's first blocker: unblock it, re-point the waits or cancel the chain.",
    "disabled_owner": "Hand the items to an enabled member or cancel them.",
    "oversize_state": "Archive or split the entry (keep_last_json with archive_key, or one key per object).",
    "handover_not_allowed": (
        "The sources are agents outside this fleet (each named with its home fleet): point their handover at "
        "the fleet's orchestrator, or remove it. Exposing the target lets every other fleet reach it, so "
        "expose it only when that is wanted."
    ),
    "cost_spike": "Read the member's runs of the day for a loop or an oversized read.",
}


@dataclass(frozen=True)
class FleetFinding:
    """One thing the fleet's owner should know, with the ids it was read from."""

    kind: str
    summary: str
    evidence: list[str] = field(default_factory=list)
    count: int = 0
    #: Stable per fleet, kind and whole evidence set (#12014); set by the report.
    id: str = ""
    #: Every evidence id, uncapped: what the id is computed on (never answered).
    full_evidence: tuple[str, ...] = field(default=(), repr=False)

    @property
    def all_evidence(self) -> Sequence[str]:
        return self.full_evidence or self.evidence

    @property
    def fix(self) -> str:
        return FINDING_FIX.get(self.kind, FIX_OWNER)

    @property
    def advice(self) -> str:
        return FINDING_ADVICE.get(self.kind, "")

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data.pop("full_evidence", None)
        return {**data, "fix": self.fix, "advice": self.advice}


def _finding(kind: str, summary: str, ids: Sequence[object]) -> FleetFinding:
    names = [str(i) for i in ids]
    return FleetFinding(
        kind=kind, summary=summary, evidence=names[:MAX_EVIDENCE], count=len(names), full_evidence=tuple(names)
    )


# ── Pure rules (unit-tested without a database) ─────────────────────────────


def assignment_is_failing(runs: Sequence[tuple[str | None, str | None]]) -> bool:
    """Whether the newest :data:`FAILING_RUNS` judged runs, newest first, all failed."""
    judged = [o for o in (run_outcome(s, e) for s, e in runs) if o in (OUTCOME_OK, OUTCOME_FAILED)]
    return len(judged) >= FAILING_RUNS and all(o == OUTCOME_FAILED for o in judged[:FAILING_RUNS])


def schedule_is_silent(*, created_at: datetime | None, last_triggered_at: datetime | None, now: datetime) -> bool:
    """Whether an enabled schedule has not fired for :data:`SILENT_AFTER` (and is that old)."""
    if created_at is None or created_at > now - SILENT_AFTER:
        return False
    return last_triggered_at is None or last_triggered_at < now - SILENT_AFTER


def is_cost_spike(*, today_tokens: int, week_tokens: int, week_days: int = 7) -> bool:
    """Whether a day's tokens spike over the member's own daily average of the week before."""
    if today_tokens < SPIKE_MIN_TOKENS:
        return False
    average = week_tokens / max(week_days, 1)
    if average <= 0:
        return True
    return today_tokens > SPIKE_FACTOR * average


def blocked_without_blocker(
    *,
    blocked_on: object,
    blocked_on_refs: object,
    blocked_reason: str | None = None,
    needs_human: bool | None,
    owner_agent_id: object = None,
    ask: object = None,
    cancelled_blockers: Iterable[str] | None = None,
    payload: object = None,
) -> bool:
    """Whether a blocked item is not black or white (#762).

    No owner, no blocker the platform releases (``blocked_reason`` alone is
    free text), ``needs_human`` without an ask card, or every ``blocked_on``
    entry cancelled or gone (*cancelled_blockers*: those ids) with nothing
    else to release it. An open pull request bound in *payload* is a blocker
    the platform releases (its CI completion or its closing hands it back).
    """
    from app.services.work_item_blocked_refs import blocked_defects, bound_open_pull_request

    bound = bound_open_pull_request(payload)
    if blocked_defects(
        owner_agent_id=owner_agent_id,
        blocked_on=blocked_on,
        blocked_on_refs=blocked_on_refs,
        needs_human=needs_human,
        ask=ask,
        bound_open_pull_request=bound,
    ):
        return True
    lost = set(cancelled_blockers or ())
    waits = [str(b) for b in blocked_on] if isinstance(blocked_on, list) else []
    return bool(waits) and set(waits) <= lost and not (blocked_on_refs or needs_human or bound)


# ── The read ─────────────────────────────────────────────────────────────────


@dataclass
class FleetHealthReport:
    fleet_id: str
    fleet_name: str
    status: str
    members: list[dict[str, Any]]
    findings: list[FleetFinding]
    #: The live dismissals of the fleet (#12014), each with ``hides``.
    dismissed: list[dict[str, Any]] = field(default_factory=list)

    @property
    def healthy(self) -> bool:
        return not self.findings

    def structured(self) -> dict[str, Any]:
        """What an activation check reads: ``healthy`` false is the red signal."""
        return {
            "fleet_id": self.fleet_id,
            "healthy": self.healthy,
            "status": self.status,
            "findings": [f.as_dict() for f in self.findings],
            "dismissed": [dict(d) for d in self.dismissed],
        }


async def build_fleet_health_report(
    db: AsyncSession,
    fleet: Fleet,
    *,
    user_id: str,
    run_scope: Any,
    now: datetime | None = None,
) -> FleetHealthReport:
    """The member verdicts plus every finding of the module docstring."""
    from app.services.fleet_views import build_fleet_health

    now = now or datetime.now(UTC)
    status, members, _in_workspace = await build_fleet_health(db, fleet, user_id=user_id, run_scope=run_scope, now=now)
    ids = [m.agent_id for m in members]
    findings: list[FleetFinding] = []

    failing = [m.agent_id for m in members if m.status == "failing"]
    if failing:
        findings.append(_finding("member_failing", f"{len(failing)} member(s) failing", failing))
    disabled = [m.agent_id for m in members if not m.enabled]
    if disabled:
        findings.append(_finding("member_disabled", f"{len(disabled)} member(s) disabled", disabled))
    held = [m.agent_id for m in members if not m.enabled and m.open_work_items > 0]
    if held:
        findings.append(_finding("disabled_owner", f"{len(held)} disabled member(s) still own open work items", held))

    findings += await _assignment_findings(db, ids, run_scope=run_scope, now=now)
    findings += await _work_item_findings(db, fleet, ids, user_id=user_id, now=now)
    findings += await _shared_state_findings(db, fleet, ids, user_id=user_id)
    findings += await _handover_findings(db, fleet)
    findings += await _cost_findings(db, ids, run_scope=run_scope, now=now)
    findings = with_ids(fleet.id, findings)
    active, dismissed = split_findings(findings, getattr(fleet, "health_dismissals", None), now)

    return FleetHealthReport(
        fleet_id=str(fleet.id),
        fleet_name=fleet.name,
        status=status,
        members=[
            {
                "agent_id": str(m.agent_id),
                "status": m.status,
                "open_work_items": m.open_work_items,
                "stale_work_items": m.stale_work_items,
            }
            for m in members
        ],
        findings=active,
        dismissed=dismissed,
    )


def with_ids(fleet_id: object, findings: Sequence[FleetFinding]) -> list[FleetFinding]:
    """*findings* with their stable id (#12014), computed on the whole evidence set."""
    return [replace(f, id=finding_id(fleet_id, f.kind, f.all_evidence)) for f in findings]


async def _assignment_findings(
    db: AsyncSession, agent_ids: Sequence[uuid.UUID], *, run_scope: Any, now: datetime
) -> list[FleetFinding]:
    from app.models.assignment import Assignment, AssignmentRun

    if not agent_ids:
        return []
    rows = (
        await db.execute(
            select(Assignment.id, Assignment.trigger_type, Assignment.created_at, Assignment.last_triggered_at).where(
                Assignment.agent_id.in_(list(agent_ids)), Assignment.enabled.is_(True)
            )
        )
    ).all()
    if not rows:
        return []
    silent = [
        aid
        for aid, trigger, created, last in rows
        if trigger == "schedule" and schedule_is_silent(created_at=created, last_triggered_at=last, now=now)
    ]
    rank = (
        func.row_number()
        .over(
            partition_by=AssignmentRun.assignment_id,
            order_by=(AssignmentRun.created_at.desc(), AssignmentRun.id.desc()),
        )
        .label("rank")
    )
    ranked = (
        select(AssignmentRun.assignment_id, AssignmentRun.status, AssignmentRun.error, rank)
        .where(AssignmentRun.assignment_id.in_([r[0] for r in rows]), run_scope)
        .subquery()
    )
    # A window wide enough to find FAILING_RUNS judged runs among interruptions.
    run_rows = (
        await db.execute(
            select(ranked.c.assignment_id, ranked.c.status, ranked.c.error)
            .where(ranked.c.rank <= FAILING_RUNS * 3)
            .order_by(ranked.c.assignment_id, ranked.c.rank)
        )
    ).all()
    by_assignment: dict[uuid.UUID, list[tuple[str | None, str | None]]] = {}
    for aid, status, error in run_rows:
        by_assignment.setdefault(aid, []).append((status, error))
    failing = [aid for aid, runs in by_assignment.items() if assignment_is_failing(runs)]
    out: list[FleetFinding] = []
    if failing:
        out.append(
            _finding(
                "assignment_failing",
                f"{len(failing)} assignment(s) failed their last {FAILING_RUNS} runs",
                failing,
            )
        )
    if silent:
        out.append(
            _finding(
                "assignment_silent",
                f"{len(silent)} schedule(s) silent for {SILENT_AFTER.days} days or more",
                silent,
            )
        )
    return out


async def _work_item_findings(
    db: AsyncSession, fleet: Fleet, agent_ids: Sequence[uuid.UUID], *, user_id: str, now: datetime
) -> list[FleetFinding]:
    from app.core.utils import safe_uuid
    from app.models.work_item import WORK_ITEM_OPEN_STATUSES, WorkItem
    from app.services.work_item import (
        CLAIM_HELD_STATUSES,
        _holder_alive_clause,
        _visible_clause,
        _workspace_tiers,
        fleet_work_item_clause,
    )

    readable, _ = await _workspace_tiers(db, user_id)
    scope = and_(
        _visible_clause(safe_uuid(user_id) if user_id else None, readable),
        fleet_work_item_clause(agent_ids, fleet.workspace_id),
        WorkItem.status.in_(WORK_ITEM_OPEN_STATUSES),
    )
    out: list[FleetFinding] = []
    orphaned = (
        (
            await db.execute(
                select(WorkItem.id).where(
                    scope,
                    WorkItem.status.in_(CLAIM_HELD_STATUSES),
                    WorkItem.claimed_by_run_id.isnot(None),
                    not_(_holder_alive_clause(WorkItem.claimed_by_run_id)),  # type: ignore[no-untyped-call]
                )
            )
        )
        .scalars()
        .all()
    )
    if orphaned:
        out.append(_finding("orphaned_claim", f"{len(orphaned)} item(s) held by a run that ended", orphaned))
    past_due = (
        (await db.execute(select(WorkItem.id).where(scope, WorkItem.due_at.isnot(None), WorkItem.due_at < now)))
        .scalars()
        .all()
    )
    if past_due:
        out.append(_finding("past_due", f"{len(past_due)} open item(s) past their due date", past_due))
    stranded = await _not_picked_up(db, scope, now)
    if stranded:
        out.append(
            _finding(
                "not_picked_up",
                f"{len(stranded)} open item(s) no inbox will take: no owner, an owner with no work-item "
                "inbox, or inboxes that filter them out",
                stranded,
            )
        )
    blocked = (
        await db.execute(
            select(
                WorkItem.id,
                WorkItem.blocked_on,
                WorkItem.blocked_on_refs,
                WorkItem.blocked_reason,
                WorkItem.needs_human,
                WorkItem.owner_agent_id,
                WorkItem.ask,
                WorkItem.payload,
            )
            # A scheduled one-off blocked or missed is released by its own reschedule
            # and cancel actions, outside the black-or-white rule (#862).
            .where(scope, WorkItem.status == "blocked", WorkItem.action_mode.is_(None))
        )
    ).all()
    waited = {safe_uuid(str(b)) for row in blocked for b in (row[1] or [])} - {None}
    live = (
        {
            str(i)
            for i in (
                await db.execute(select(WorkItem.id).where(WorkItem.id.in_(waited), WorkItem.status != "cancelled"))
            )
            .scalars()
            .all()
        }
        if waited
        else set()
    )
    bare = [
        wid
        for wid, on, refs, reason, human, owner, ask, payload in blocked
        if blocked_without_blocker(
            blocked_on=on,
            blocked_on_refs=refs,
            blocked_reason=reason,
            needs_human=human,
            owner_agent_id=owner,
            ask=ask,
            cancelled_blockers={str(b) for b in on or []} - live,
            payload=payload,
        )
    ]
    if bare:
        out.append(
            _finding(
                "blocked_without_blocker",
                f"{len(bare)} blocked item(s) not black or white: no owner, no real blocker, "
                "or a person's decision without its ask card",
                bare,
            )
        )
    from app.services.work_item_ask_backfill import card_unreadable

    waiting = (await db.execute(select(WorkItem.id, WorkItem.ask).where(scope, WorkItem.needs_human.is_(True)))).all()
    unreadable = [wid for wid, ask in waiting if card_unreadable(ask)]
    if unreadable:
        out.append(
            _finding(
                "decision_card_not_readable",
                f"{len(unreadable)} item(s) wait for a person on a card they cannot read",
                unreadable,
            )
        )
    out += await _wait_graph_findings(db, scope, now)
    return out


#: Open items read per fleet for ``not_picked_up``, newest first.
NOT_PICKED_UP_SCAN = 500
#: The pickup states that mean nothing will ever take the item by itself.
STRANDED_PICKUP_STATES = frozenset({"unassigned", "manual", "filtered_out"})


async def _not_picked_up(db: AsyncSession, scope: Any, now: datetime) -> list[uuid.UUID]:
    """The open, unscheduled items of *scope* in a :data:`STRANDED_PICKUP_STATES` state.

    The pickup is the dispatch's own rule (``work_item_context.pickup_inboxes``
    then ``pickup_state``), so an item whose pull request CI runs, or that is
    deferred by ``not_before`` to an inbox that takes it, is never reported.
    """
    from app.models.work_item import WorkItem
    from app.services.work_item_context import pickup_inboxes, pickup_state

    rows = (
        await db.execute(
            select(
                WorkItem.id, WorkItem.owner_agent_id, WorkItem.not_before, WorkItem.payload, WorkItem.correlation_key
            )
            .where(scope, WorkItem.status == "open", WorkItem.action_mode.is_(None))
            .order_by(WorkItem.created_at.desc())
            .limit(NOT_PICKED_UP_SCAN)
        )
    ).all()
    items = [
        {
            "id": str(wid),
            "status": "open",
            "owner_agent_id": str(owner) if owner else None,
            "not_before": not_before.isoformat() if not_before else None,
            "payload": payload or {},
            "scheduled": None,
        }
        for wid, owner, not_before, payload, _key in rows
    ]
    if not items:
        return []
    inboxes = await pickup_inboxes(db, items)
    stranded = [
        row
        for row, item in zip(rows, items, strict=True)
        if pickup_state(item, inboxes.get(row[0], []), now) in STRANDED_PICKUP_STATES
    ]
    listeners = await _work_item_listeners(db, {row[1] for row in stranded if row[1]})
    return [row[0] for row in stranded if not _heard_by_listener(row[1], row[4], listeners)]


async def _work_item_listeners(db: AsyncSession, owners: set[uuid.UUID]) -> dict[uuid.UUID, list[Any]]:
    """Per owner, the ``correlation_key`` match values of its enabled ``platform_event``
    assignments that hear ``work_item.*`` events on a filtered key.

    Such an assignment works the items its key filter names (a seeded CTEM loop's
    ``ctem:f1:case:`` item is moved by its "Gate answered" listener), so an open item
    it hears is not stranded though no work-item inbox takes it.
    """
    from app.models.assignment import Assignment
    from app.services.integration_event_filters import trigger_filter

    if not owners:
        return {}
    rows = (
        await db.execute(
            select(Assignment.agent_id, Assignment.trigger_config).where(
                Assignment.agent_id.in_(list(owners)),
                Assignment.enabled.is_(True),
                Assignment.trigger_type == "platform_event",
            )
        )
    ).all()
    out: dict[uuid.UUID, list[Any]] = {}
    for agent_id, config in rows:
        event = (config or {}).get("event") if isinstance(config, dict) else None
        events = [event] if isinstance(event, str) else list(event or [])
        if not any(isinstance(e, str) and e.startswith("work_item.") for e in events):
            continue
        key_match = trigger_filter(config).get("correlation_key")
        if key_match is not None:
            out.setdefault(agent_id, []).append(key_match)
    return out


def _heard_by_listener(owner: uuid.UUID | None, key: str | None, listeners: dict[uuid.UUID, list[Any]]) -> bool:
    """Whether an event listener of *owner* filters on a key that *key* matches.

    An item with no owner or no key is never heard: it stays stranded.
    """
    from app.services.integration_event_filters import field_matches

    if owner is None or not key:
        return False
    item = {"correlation_key": key}
    return any(field_matches(item, "correlation_key", match) for match in listeners.get(owner, []))


async def _wait_graph_findings(db: AsyncSession, scope: Any, now: datetime) -> list[FleetFinding]:
    """``wait_cycle`` and ``blocked_chain_stalled`` over the fleet's open items (#606)."""
    from app.services.work_item_wait_cycles import find_cycles, load_nodes, stalled_chains

    nodes = await load_nodes(db, scope=scope)
    cycles = find_cycles(nodes)
    out: list[FleetFinding] = []
    in_cycles = sorted({i for cycle in cycles for _, i in cycle})
    if in_cycles:
        out.append(
            _finding("wait_cycle", f"{len(cycles)} wait cycle(s): blocked items waiting on each other", in_cycles)
        )
    chains = stalled_chains(nodes, now, in_cycles=in_cycles)
    stalled = [i for chain in chains for i in chain]
    if stalled:
        out.append(
            _finding(
                "blocked_chain_stalled",
                f"{len(chains)} blocked chain(s) with no progress for 2 hours or more",
                stalled,
            )
        )
    return out


async def _shared_state_findings(
    db: AsyncSession, fleet: Fleet, agent_ids: Sequence[uuid.UUID], *, user_id: str
) -> list[FleetFinding]:
    from app.core.config import settings
    from app.core.utils import safe_uuid
    from app.models.shared_state import SHARED_STATE_ACTIVE_STATUS, SharedState
    from app.services.shared_state import _workspace_tiers, shared_state_visible_clause

    limit = int(settings.shared_state_value_soft_limit_chars)
    readable, _ = await _workspace_tiers(db, user_id)
    parts = [SharedState.workspace_id == fleet.workspace_id] if fleet.workspace_id else []
    if agent_ids:
        parts.append(SharedState.owner_agent_id.in_(list(agent_ids)))
    if not parts:
        return []
    keys = (
        (
            await db.execute(
                select(SharedState.key).where(
                    shared_state_visible_clause(safe_uuid(user_id) if user_id else None, readable),
                    or_(*parts),
                    SharedState.status == SHARED_STATE_ACTIVE_STATUS,
                    func.length(cast(SharedState.value, String)) > limit,
                )
            )
        )
        .scalars()
        .all()
    )
    if not keys:
        return []
    return [_finding("oversize_state", f"{len(keys)} shared-state entr(y/ies) over {limit:,} characters", keys)]


async def _handover_findings(db: AsyncSession, fleet: Fleet) -> list[FleetFinding]:
    from app.services.fleets import fleet_boundary_violations

    violations = await fleet_boundary_violations(db, fleet)
    if not violations:
        return []
    pairs = [f"{v.source_agent_id}->{v.target_agent_id}" for v in violations]
    return [
        _finding(
            "handover_not_allowed",
            f"{len(pairs)} handover(s) into a member the fleet does not expose: "
            + await _handover_lines(db, violations),
            pairs,
        )
    ]


#: Handovers named one by one in the summary (the evidence keeps every id).
MAX_NAMED_HANDOVERS = 5


async def _handover_lines(db: AsyncSession, violations: Sequence[Any]) -> str:
    """Each handover as 'source (home fleet or no fleet, outside this one) -> target' (#2132).

    The ask read from ``source->target`` ids alone suggested the sources were
    members; naming them with their home fleet shows they are outside it.
    """
    from app.models.agent import Agent
    from app.services.fleets import home_fleets_of

    shown = list(violations)[:MAX_NAMED_HANDOVERS]
    ids = {v.source_agent_id for v in shown} | {v.target_agent_id for v in shown}
    names = dict((await db.execute(select(Agent.id, Agent.name).where(Agent.id.in_(list(ids))))).all())
    homes = await home_fleets_of(db, [v.source_agent_id for v in shown])

    def where(agent_id: uuid.UUID) -> str:
        home = homes.get(agent_id)
        return f"fleet {home.fleet_name}" if home else "no fleet"

    lines = [
        f"{names.get(v.source_agent_id, v.source_agent_id)} ({where(v.source_agent_id)}) -> "
        f"{names.get(v.target_agent_id, v.target_agent_id)}"
        for v in shown
    ]
    more = len(violations) - len(shown)
    return "; ".join(lines) + (f"; +{more} more" if more > 0 else "")


async def _cost_findings(
    db: AsyncSession, agent_ids: Sequence[uuid.UUID], *, run_scope: Any, now: datetime
) -> list[FleetFinding]:
    from app.models.assignment import AssignmentRun

    if not agent_ids:
        return []
    day = now - timedelta(days=1)
    week = now - timedelta(days=8)
    tokens = func.coalesce(AssignmentRun.total_input_tokens, 0) + func.coalesce(AssignmentRun.total_output_tokens, 0)
    rows = (
        await db.execute(
            select(
                AssignmentRun.agent_id,
                func.sum(tokens).filter(AssignmentRun.created_at >= day),
                func.sum(tokens).filter(AssignmentRun.created_at < day),
            )
            .where(AssignmentRun.agent_id.in_(list(agent_ids)), AssignmentRun.created_at >= week, run_scope)
            .group_by(AssignmentRun.agent_id)
        )
    ).all()
    spikes = [
        aid for aid, today, before in rows if is_cost_spike(today_tokens=int(today or 0), week_tokens=int(before or 0))
    ]
    if not spikes:
        return []
    return [
        _finding(
            "cost_spike",
            f"{len(spikes)} member(s) used over {SPIKE_FACTOR:g}x their daily tokens in the last day",
            spikes,
        )
    ]


# ── The safe fixes ───────────────────────────────────────────────────────────


async def apply_safe_fixes(report: FleetHealthReport) -> dict[str, int]:
    """Apply the ``auto`` fixes of *report*; ``{kind: rows changed}``.

    Only the release of orphaned claims, through the platform's own sweep scoped
    to the items' ended runs: it never touches a claim a live run holds.
    """
    from app.core import database
    from app.models.work_item import WorkItem
    from app.services.work_item import release_ended_run_claims

    applied: dict[str, int] = {}
    for finding in report.findings:
        if finding.kind != "orphaned_claim" or not finding.evidence:
            continue
        item_ids = [uuid.UUID(i) for i in finding.evidence]
        async with database.async_session() as db:
            run_ids = (
                (await db.execute(select(WorkItem.claimed_by_run_id).where(WorkItem.id.in_(item_ids)))).scalars().all()
            )
        runs = [r for r in run_ids if r is not None]
        applied["orphaned_claim"] = await release_ended_run_claims(run_ids=runs) if runs else 0
    return applied


def format_report(report: FleetHealthReport, *, applied: dict[str, int] | None = None) -> str:
    """The report as the agent reads it: one line per finding, evidence ids, fix and advice."""
    head = f"Fleet **{report.fleet_name}** ({report.fleet_id}): status {report.status}, "
    lines = [head + ("healthy: no finding." if report.healthy else f"{len(report.findings)} finding(s).")]
    for f in report.findings:
        more = f" (+{f.count - len(f.evidence)} more)" if f.count > len(f.evidence) else ""
        lines.append(f"  • `{f.id}` [{f.kind}] {f.summary}; fix: {f.fix}; {f.advice}")
        lines.append(f"    evidence: {', '.join(f.evidence)}{more}")
    if applied:
        lines.append("Applied: " + ", ".join(f"{k} {v}" for k, v in applied.items()))
    if report.dismissed:
        lines.append(f"Dismissed ({len(report.dismissed)}, restore with dismiss_fleet_health_findings):")
        for d in report.dismissed:
            who = d.get("by_name") or d.get("by") or "?"
            lapse = f", until {d['until']}" if d.get("until") else ""
            scope = "every finding of the kind" if d.get("scope") == "kind" else d.get("kind")
            lines.append(
                f"  • `{d['id']}` {scope}: {d.get('reason')} (by {who} at {d.get('at')}{lapse}; hides {d.get('hides', 0)})"
            )
    return "\n".join(lines)
