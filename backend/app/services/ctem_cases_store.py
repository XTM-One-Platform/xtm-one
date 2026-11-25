# SPDX-License-Identifier: Apache-2.0

"""CTEM Cases persistence: merge a recompute, change a case, read cases (#11545).

The rules are in ``services/ctem_cases.py`` (pure); this module writes what
they decide and records every change as a ``ctem_case_events`` row. Recompute
writes membership and the system transitions only; state, owner, due date and
notes are written by :func:`update_case` alone.

Cases are shared by the organization and hold ids and workflow only (#11613):
no name, CVE, score, asset, priority or count is stored. What a viewer sees of
them is decided at read time by ``services/ctem_cases_view.py`` from the
viewer's own source read. A recompute MERGES one user's view: it adds and
reopens what that user sees open and resolves only what that user's read
confirmed closed, so a narrower view never closes or shrinks a case.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.ctem_case import OPEN_STATES, CtemCase, CtemCaseEvent, CtemCaseIssue
from app.services import ctem_case_version_history as version_history
from app.services import ctem_case_versions
from app.services import ctem_cases as rules
from app.services.ctem_fixed_version import fold_by_platform

if TYPE_CHECKING:
    from app.services.ctem_case_loop import PendingEvents

#: Most cases one list page returns.
MAX_PAGE = 200

#: The scope gate severity a reader case takes from its priority (dev-docs/ctem-cases.md section 4.3).
PRIORITY_SEVERITY: dict[str, str] = {"critical": "P1", "high": "P2", "medium": "P3", "low": "P3"}
#: The case event that records a case was announced to the loop (``ctem_case.opened`` raised).
ANNOUNCED = "announced"


def announce(
    db: AsyncSession, case: CtemCase, events: PendingEvents | None, at: datetime, *, reason: str | None = None
) -> None:
    """Queue ``ctem_case.opened`` for *case* and record it with an ``announced`` event, so it is raised once.

    Nothing is written without *events*: the scheduled backfill
    (``ctem_case_loop.announce_unannounced``) announces the case later.
    """
    if events is None:
        return
    from app.services import ctem_case_loop as loop

    db.add(_event(case.id, rules.SYSTEM_ACTOR, ANNOUNCED, to_value=loop.CASE_OPENED, reason=reason, at=at))
    events.verticals.add(case.vertical)
    events.frame(case, "created", at)
    events.add(loop.CASE_OPENED, loop.case_data(case), loop.case_event_id(loop.CASE_OPENED, case, "open"))


def _state_event(case: CtemCase, events: PendingEvents | None, at: datetime) -> None:
    """Queue ``ctem_case.resolved`` or ``ctem_case.reopened`` for a state the recompute just wrote."""
    if events is None:
        return
    from app.services import ctem_case_loop as loop

    name = loop.CASE_RESOLVED if case.state == "resolved" else loop.CASE_REOPENED
    events.verticals.add(case.vertical)
    events.frame(case, "state", at)
    events.add(name, loop.case_data(case), loop.case_event_id(name, case, at.isoformat()))


def _rank(case: CtemCase, members: list[rules.SourceIssue], now: datetime) -> bool:
    """Give an open reader case the severity and SLA of its priority; a more urgent read escalates, never relaxes.

    *members* are the open issues of the case this read sees. True when the
    severity changed.
    """
    from app.services import ctem_case_loop as loop

    if not members or case.state not in OPEN_STATES:
        return False
    severity = PRIORITY_SEVERITY[rules.default_priority(members)]
    current = case.severity
    if current in loop.DEFAULT_SLA_HOURS and loop.severity_rank(current) <= loop.severity_rank(severity):
        return False
    case.severity = severity
    due = now + timedelta(hours=loop.DEFAULT_SLA_HOURS[severity])
    sla = rules.as_utc(case.sla_due_at)
    if sla is None or sla > due:
        case.sla_due_at = due
    return True


def _event(case_id: uuid.UUID, actor: str, kind: str, **kw: Any) -> CtemCaseEvent:
    return CtemCaseEvent(case_id=case_id, actor=actor, kind=kind, at=kw.pop("at", None) or datetime.now(UTC), **kw)


def _split_member(member_id: str) -> tuple[str, str]:
    source, _, external_id = member_id.partition(":")
    return source, external_id


async def _scope_cases(db: AsyncSession, organization_id: uuid.UUID, vertical: str) -> list[CtemCase]:
    return list(
        (
            await db.execute(
                select(CtemCase).where(CtemCase.organization_id == organization_id, CtemCase.vertical == vertical)
            )
        ).scalars()
    )


async def _members(db: AsyncSession, case_ids: list[uuid.UUID]) -> dict[uuid.UUID, list[CtemCaseIssue]]:
    out: dict[uuid.UUID, list[CtemCaseIssue]] = {cid: [] for cid in case_ids}
    if not case_ids:
        return out
    for row in (await db.execute(select(CtemCaseIssue).where(CtemCaseIssue.case_id.in_(case_ids)))).scalars():
        out.setdefault(row.case_id, []).append(row)
    return out


async def member_ids(db: AsyncSession, *, organization_id: uuid.UUID, vertical: str) -> list[str]:
    """Every member id of the organization's cases (what a source read looks up by id)."""
    cases = await _scope_cases(db, organization_id, vertical)
    members = await _members(db, [c.id for c in cases])
    return sorted({f"{r.source}:{r.external_id}" for rows in members.values() for r in rows})


async def apply_recompute(
    db: AsyncSession,
    *,
    organization_id: uuid.UUID,
    vertical: str,
    issues: Iterable[rules.SourceIssue],
    closed: Iterable[str] = (),
    actor: str = rules.SYSTEM_ACTOR,
    events: PendingEvents | None = None,
    now: datetime | None = None,
) -> dict[str, int]:
    """Merge one user's view into the shared cases of one vertical.

    *issues* are the open issues that user sees, *closed* the member ids their
    read confirmed closed. Answers the counts written: created, updated,
    resolved, reopened, signals, no_known_fix (counts of this user's own view only).

    The recompute never builds a case (#1529): an issue whose fix no case
    holds is recorded as a signal (``ctem_source_signals``) and its case opens
    through the scope gate and ``ctem_case_loop.open_from_signal`` only; an
    issue joining an existing case records its signal too, joined to that case.
    ``created`` counts the cases those signals opened in this pass.

    *events* collects the case events (``ctem_case.opened`` on a new case,
    ``reopened`` and ``resolved`` on a state the read moved) for the caller to
    publish after its commit (``ctem_case_loop.publish_pending``); a recompute
    that changes nothing collects none. An open case takes the severity and SLA
    of the priority of the issues this read sees (:func:`_rank`).
    """
    now = now or datetime.now(UTC)
    issues = list(issues)
    # A member the declared version puts outside every affected range is closed with that reason (CTEM-11).
    not_affected = {i.member_id: i.not_affected for i in issues if i.not_affected}
    closed = list(dict.fromkeys([*closed, *not_affected]))
    cases = await _scope_cases(db, organization_id, vertical)
    members = await _members(db, [c.id for c in cases])
    by_id = {str(c.id): c for c in cases}
    # An upgrade case the asset inventory shows fixed on every asset leaves the case scope (#1252): an open
    # one resolves, a resolved one is not reopened by issues still open at source on a version nobody runs.
    # One inventory read per upgrade case: it decides #1252 and records the per-device history (#1694).
    history = await events_of(db, [c.id for c in cases])
    fixed_in_inventory: set[uuid.UUID] = set()
    for c in cases:
        running = await ctem_case_versions.for_case(db, c, with_devices=True)
        await version_history.record(db, c, running, history.get(c.id, []), at=now)
        if (
            c.state in (*OPEN_STATES, "resolved")
            and c.fixed_version
            and running is not None
            and ctem_case_versions.all_on_fixed(running)
        ):
            fixed_in_inventory.add(c.id)
    # A resolved case closed by a decision holds while its issues stay as they were (#1646).
    decided = _decided_closures({c.id: history.get(c.id, []) for c in cases if c.state == "resolved"})
    snapshots = [
        rules.CaseSnapshot(
            id=str(c.id),
            keys=list(c.keys or []),
            state=c.state,
            pinned_issues=list(c.pinned_issues or []),
            # A signal member is not a source issue: recompute never adds, reopens or closes it.
            members={f"{r.source}:{r.external_id}": r.open for r in members[c.id] if r.source != "signal"},
            validation_plan=bool(getattr(c, "validation_plan", False)),
            fixed_in_inventory=c.id in fixed_in_inventory,
            decided_closure=decided.get(c.id),
        )
        for c in cases
    ]
    counts = {"created": 0, "updated": 0, "resolved": 0, "reopened": 0, "signals": 0}
    seen = {i.member_id: i for i in issues if not i.not_affected}
    opened: set[uuid.UUID] = set()
    joined: dict[uuid.UUID, set[str]] = {}
    for plan in rules.plan_recompute(snapshots, issues, closed):
        if plan.case_id is None:
            # A fix no case holds yet: each issue is a signal; the gate decides whether a case opens (#1529).
            remediation = plan.remediation
            if remediation is None:  # a new-case plan always carries its remediation
                continue
            for member_id in plan.add:
                issue = seen.get(member_id)
                if issue is None:
                    continue
                signal, created = await _source_signal(db, organization_id, issue, remediation, now)
                if signal is None:
                    continue
                counts["signals"] += int(created)
                case = await _case_of_signal(db, signal, vertical=vertical, events=events, now=now)
                if case is None:
                    continue
                if case.id not in by_id and case.id not in opened:
                    opened.add(case.id)
                    counts["created"] += 1
                # The case takes the severity of the most urgent issue this read sees; never relaxes.
                _rank(case, [issue], now)
                done = joined.setdefault(case.id, {f"{r.source}:{r.external_id}" for r in members.get(case.id, [])})
                if member_id not in done:
                    done.add(member_id)
                    source, external_id = _split_member(member_id)
                    db.add(CtemCaseIssue(case_id=case.id, source=source, external_id=external_id, open=True))
                    db.add(_event(case.id, actor, "issue_added", issue=member_id, at=now))
            continue
        case = by_id[plan.case_id]
        rows = {f"{r.source}:{r.external_id}": r for r in members[case.id]}
        if plan.add_keys:
            # The folded upgrade target moved: the case keeps its members and takes the new key (CTEM-11).
            case.keys = [*(case.keys or []), *(k for k in plan.add_keys if k not in (case.keys or []))]
            if plan.remediation is not None:
                case.title = plan.remediation.title
                case.fixed_version = plan.remediation.fixed_version
                case.fixed_versions = dict(plan.remediation.fixed_versions)
            for key in plan.add_keys:
                db.add(_event(case.id, actor, "retargeted", to_value=key, at=now))
        for member_id in plan.add:
            source, external_id = _split_member(member_id)
            db.add(CtemCaseIssue(case_id=case.id, source=source, external_id=external_id, open=True))
            db.add(_event(case.id, actor, "issue_added", issue=member_id, at=now))
        # Every issue of the case this read sees is a signal too, joined to the case that holds its fix
        # (#1529); a re-read moves the signal's last sighting, never adds one.
        platform_maps: list[dict[str, str]] = []
        for member_id in sorted({*plan.add, *plan.reopen, *(m for m, r in rows.items() if r.open)}):
            fix = rules.remediation_of(seen[member_id]) if member_id in seen else None
            if fix is None:
                continue
            if fix.key in (case.keys or []) and fix.fixed_version == case.fixed_version:
                platform_maps.append(dict(fix.fixed_versions))
            signal, created = await _source_signal(db, organization_id, seen[member_id], fix, now)
            counts["signals"] += int(created)
            if signal is not None:
                from app.services import ctem_case_loop as loop

                loop.join_case(db, signal, case, events=events, now=now)
        if platform_maps:
            # The fix of each platform follows what the members' source names now (#1572).
            platforms = fold_by_platform(platform_maps)
            if platforms != dict(case.fixed_versions or {}):
                case.fixed_versions = platforms
        for member_id in plan.reopen:
            rows[member_id].open = True
            db.add(_event(case.id, actor, "issue_reopened", issue=member_id, at=now))
        for member_id in plan.resolve:
            rows[member_id].open = False
            evidence = not_affected.get(member_id) or rules.CLOSED_AT_SOURCE
            db.add(_event(case.id, actor, "issue_closed", issue=member_id, evidence=evidence, at=now))
        if plan.add or plan.reopen or plan.resolve or plan.add_keys:
            counts["updated"] += 1
        if plan.new_state:
            db.add(
                _event(
                    case.id,
                    rules.SYSTEM_ACTOR,
                    "state",
                    from_value=case.state,
                    to_value=plan.new_state,
                    reason=plan.reason,
                    evidence=plan.evidence,
                    at=now,
                )
            )
            counts["resolved" if plan.new_state == "resolved" else "reopened"] += 1
            if plan.evidence == rules.FIXED_IN_INVENTORY:
                case.resolution_kind = "fixed"
            case.state = plan.new_state
            case.state_since = now
            _state_event(case, events, now)
        # The source issues this read sees escalate the case, never relax it; a case of signals alone
        # keeps the severity of its decision record (ctem_case_loop.open_from_signal).
        still_open = [
            seen[m] for m, row in rows.items() if m in seen and (row.open or m in plan.reopen) and m not in plan.resolve
        ]
        _rank(case, [*still_open, *(seen[m] for m in plan.add if m in seen)], now)
    grouping = rules.group_issues(issues)
    counts["no_known_fix"] = len(grouping.no_known_fix)
    counts["not_affected"] = len(grouping.not_affected)
    return counts


async def remap_members(
    db: AsyncSession,
    *,
    organization_id: uuid.UUID,
    vertical: str,
    remap: dict[str, str | None],
    retired_line: str,
    actor: str = rules.SYSTEM_ACTOR,
    now: datetime | None = None,
) -> dict[str, int]:
    """Move the members a source re-keyed onto their new id, in place; answers the counts.

    *remap* maps ``source:old`` to ``source:new``, or to ``None`` to retire the member. A moved
    member keeps its row (so the case keeps its history, owner, tickets, SLA and validations);
    when the case already holds the new id (a duplicate joined since), the two rows merge into
    one, open when either was. A retired member leaves the case with *retired_line* in its
    history and counts neither open nor closed. Pinned issues follow their member.
    """
    now = now or datetime.now(UTC)
    counts = {"remapped": 0, "merged": 0, "retired": 0}
    if not remap:
        return counts
    cases = await _scope_cases(db, organization_id, vertical)
    members = await _members(db, [c.id for c in cases])
    for case in cases:
        rows = {f"{r.source}:{r.external_id}": r for r in members.get(case.id, [])}
        pinned = list(case.pinned_issues or [])
        for old, new in remap.items():
            row = rows.get(old)
            if row is None:
                continue
            if new is None:
                await db.delete(row)
                rows.pop(old)
                pinned = [p for p in pinned if p != old]
                db.add(_event(case.id, actor, "note", issue=old, reason=retired_line, at=now))
                counts["retired"] += 1
                continue
            kept = rows.get(new)
            if kept is not None:
                kept.open = kept.open or row.open
                await db.delete(row)
                counts["merged"] += 1
            else:
                row.source, row.external_id = _split_member(new)
                rows[new] = row
                counts["remapped"] += 1
            rows.pop(old)
            pinned = list(dict.fromkeys(new if p == old else p for p in pinned))
            db.add(
                _event(
                    case.id,
                    actor,
                    "note",
                    issue=new,
                    from_value=old,
                    to_value=new,
                    reason=f"{retired_line}: {old} is now {new}",
                    at=now,
                )
            )
        if pinned != list(case.pinned_issues or []):
            case.pinned_issues = pinned
    await db.flush()
    return counts


async def _source_signal(
    db: AsyncSession,
    organization_id: uuid.UUID,
    issue: rules.SourceIssue,
    fix: rules.Remediation,
    now: datetime,
) -> tuple[Any, bool]:
    """Record the signal of one source issue keyed on *fix*, gated once when new; ``(row | None, created)``.

    Idempotent on the source, the external id and the fix key
    (``ctem_source_signals.signal_of``): a re-read moves ``last_seen_at``. A
    gate failure leaves the signal ``new`` for the on-demand pass.
    """
    import logging

    from app.services import ctem_scope_gate_store as gate_store
    from app.services import ctem_signals, ctem_source_signals

    inp = ctem_source_signals.signal_of(issue, fix)
    if inp is None:
        return None, False
    async with db.begin_nested():
        row, created = await ctem_signals.record_signal(
            db,
            organization_id=organization_id,
            signal=inp,
            recorded_via="listener",
            recorded_by=rules.SYSTEM_ACTOR,
            now=now,
        )
    if created:
        try:
            async with db.begin_nested():
                await gate_store.evaluate(db, row, now=now)
        except Exception:
            logging.getLogger(__name__).warning(
                "CTEM scope gate not run on source signal %s; it stays new", row.id, exc_info=True
            )
    return row, created


async def _case_of_signal(
    db: AsyncSession, signal: Any, *, vertical: str, events: PendingEvents | None, now: datetime
) -> CtemCase | None:
    """The case a source signal opened or joined: ``open_from_signal`` is the only way in (#1529)."""
    from app.services import ctem_case_loop as loop

    if signal.case_id is not None:
        return await db.get(CtemCase, signal.case_id)
    async with db.begin_nested():
        return await loop.open_from_signal(db, signal, vertical=vertical, events=events, now=now)


async def expire_acceptances(db: AsyncSession, *, now: datetime | None = None) -> int:
    """Return every case whose accepted risk expired to ``new``; answers how many."""
    now = now or datetime.now(UTC)
    rows = (
        await db.execute(select(CtemCase).where(CtemCase.state == "accepted_risk", CtemCase.accepted_until <= now))
    ).scalars()
    expired = 0
    for case in rows:
        if not rules.expired_acceptance(case.state, case.accepted_until, now):
            continue
        db.add(
            _event(
                case.id,
                rules.SYSTEM_ACTOR,
                "state",
                from_value="accepted_risk",
                to_value="new",
                reason="accepted risk expired",
                at=now,
            )
        )
        case.state = "new"
        case.state_since = now
        expired += 1
    return expired


async def close_without_retest_answered(db: AsyncSession, case_id: uuid.UUID) -> bool:
    """Whether a person answered the ``ctem.close_without_retest`` decision of the case (#1675).

    Read from the case's gate events (``ctem_case_loop.record_gate``: kind
    ``gate``, the key in ``from_value``, the outcome in ``to_value``). The agent
    read the answer and decided to close; the rule never refuses that decision.
    """
    found = await db.scalar(
        select(CtemCaseEvent.id)
        .where(
            CtemCaseEvent.case_id == case_id,
            CtemCaseEvent.kind == "gate",
            CtemCaseEvent.from_value == "ctem.close_without_retest",
            CtemCaseEvent.to_value == "answered",
        )
        .limit(1)
    )
    return found is not None


async def update_case(
    db: AsyncSession,
    case: CtemCase,
    *,
    actor: str,
    actor_kind: str,
    state: str | None = None,
    owner_user_id: uuid.UUID | None = None,
    clear_owner: bool = False,
    due_at: datetime | None = None,
    reason: str | None = None,
    evidence: str | None = None,
    accepted_until: datetime | None = None,
    note: str | None = None,
    now: datetime | None = None,
) -> CtemCase:
    """Change owner, due date, note or state of one case; every change is an event.

    The caller has checked that the actor sees the case and, for a new owner,
    that the owner sees it too (``ctem_cases_view.owner_sees``). Raises
    :class:`ctem_cases.CaseRuleError` when the state rules refuse it; nothing
    is written then.
    """
    now = now or datetime.now(UTC)
    due_at = rules.as_utc(due_at)
    accepted_until = rules.as_utc(accepted_until)
    new_owner = None if clear_owner else (owner_user_id or case.owner_user_id)
    if state is None:
        rules.check_owner(case.state, new_owner)
    else:
        answered = False
        if (
            state in ("resolved", "mitigated_watching")
            and actor_kind != "user"
            and (state == "mitigated_watching" or getattr(case, "validation_plan", False))
            and not rules.passing(rules.latest_validation(getattr(case, "validations", None)))
        ):
            answered = await close_without_retest_answered(db, case.id)
        rules.check_transition(
            current=case.state,
            target=state,
            actor_kind=actor_kind,
            owner_user_id=new_owner,
            reason=reason,
            evidence=evidence,
            accepted_until=accepted_until,
            now=now,
            validation_plan=bool(getattr(case, "validation_plan", False)),
            validations=getattr(case, "validations", None),
            close_without_retest_answered=answered,
        )
    if new_owner != case.owner_user_id:
        db.add(
            _event(
                case.id,
                actor,
                "owner",
                from_value=str(case.owner_user_id) if case.owner_user_id else None,
                to_value=str(new_owner) if new_owner else None,
                at=now,
            )
        )
        case.owner_user_id = new_owner
    if due_at is not None and due_at != case.due_at:
        db.add(
            _event(
                case.id,
                actor,
                "due",
                from_value=case.due_at.isoformat() if case.due_at else None,
                to_value=due_at.isoformat(),
                at=now,
            )
        )
        case.due_at = due_at
    if note and note.strip():
        db.add(_event(case.id, actor, "note", reason=note.strip(), at=now))
    if state is not None:
        db.add(
            _event(
                case.id,
                actor,
                "state",
                from_value=case.state,
                to_value=state,
                reason=(reason or "").strip() or None,
                evidence=(evidence or "").strip() or None,
                at=now,
            )
        )
        case.state = state
        case.state_since = now
        if state == "accepted_risk":
            case.accepted_reason = (reason or "").strip()
            case.accepted_until = accepted_until
        else:
            case.accepted_reason = None
            case.accepted_until = None
    return case


async def create_manual_case(
    db: AsyncSession,
    *,
    organization_id: uuid.UUID,
    vertical: str,
    title: str,
    member_ids: Iterable[str],
    visible: set[str],
    actor: str,
    reason: str | None = None,
    events: PendingEvents | None = None,
    now: datetime | None = None,
) -> CtemCase:
    """Split open issues out of their cases into a new manual case (``dev-docs/ctem-cases.md`` section 5).

    *events* collects ``ctem_case.opened`` for the new case, published by the
    caller after its commit; without it the scheduled backfill announces it.

    ``member_ids`` are ``source:external_id`` of open issues of a case that
    the actor sees (*visible*, from the actor's own source read): an issue the
    actor cannot see is answered exactly like an unknown one. The new case
    pins them; the cases they leave get a ``split`` event. Raises
    :class:`ctem_cases.CaseRuleError` on an empty title, no member, or an id
    that is not an open issue of a case the actor sees.
    """
    now = now or datetime.now(UTC)
    title = (title or "").strip()
    if not title:
        raise rules.CaseRuleError("a case needs a title")
    wanted = list(dict.fromkeys(m.strip() for m in member_ids if m and m.strip()))
    if not wanted:
        raise rules.CaseRuleError("a case needs at least one issue")
    cases = {c.id: c for c in await _scope_cases(db, organization_id, vertical)}
    rows = [r for members in (await _members(db, list(cases))).values() for r in members if r.open]
    by_member = {f"{r.source}:{r.external_id}": r for r in rows if f"{r.source}:{r.external_id}" in visible}
    unknown = [m for m in wanted if m not in by_member]
    if unknown:
        raise rules.CaseRuleError(f"not an open issue of a case: {', '.join(unknown[:5])}")
    moved = [by_member[m] for m in wanted]
    case = CtemCase(
        id=uuid.uuid4(),
        organization_id=organization_id,
        vertical=vertical,
        title=title[:300],
        remediation_kind="manual",
        keys=[],
        pinned_issues=wanted,
        state="new",
        state_since=now,
    )
    db.add(case)
    await db.flush()
    left: dict[uuid.UUID, list[str]] = {}
    for row, member_id in zip(moved, wanted, strict=True):
        left.setdefault(row.case_id, []).append(member_id)
        db.add(CtemCaseIssue(case_id=case.id, source=row.source, external_id=row.external_id, open=True))
        await db.delete(row)
    db.add(_event(case.id, actor, "created", to_value="manual", reason=reason, at=now))
    announce(db, case, events, now, reason="manual split")
    for source_id, ids in left.items():
        source = cases[source_id]
        source.pinned_issues = [p for p in (source.pinned_issues or []) if p not in wanted]
        for member_id in ids:
            db.add(_event(source_id, actor, "split", issue=member_id, to_value=str(case.id), reason=reason, at=now))
    return case


async def merge_cases(
    db: AsyncSession,
    *,
    organization_id: uuid.UUID,
    vertical: str,
    target_id: uuid.UUID,
    source_ids: Iterable[uuid.UUID],
    actor: str,
    reason: str | None = None,
    now: datetime | None = None,
) -> CtemCase:
    """Merge cases into *target_id* (``dev-docs/ctem-cases.md`` section 5, manual merge).

    The surviving case keeps its state, owner and due date; it takes every key,
    pinned issue, member and history event of the merged cases, so a recompute
    maps all of their keys to it. The merged cases are removed; the survivor
    records one ``merge`` event per merged case (its id as ``from``, its keys as
    ``to``). Raises :class:`ctem_cases.CaseRuleError` when the target is among
    the sources, a case is unknown in this organization and vertical, or
    nothing is left to merge.
    """
    now = now or datetime.now(UTC)
    wanted = list(dict.fromkeys(source_ids))
    if target_id in wanted:
        raise rules.CaseRuleError("the surviving case cannot also be merged into itself")
    if not wanted:
        raise rules.CaseRuleError("a merge needs at least one other case")
    cases = {c.id: c for c in await _scope_cases(db, organization_id, vertical)}
    unknown = [str(c) for c in [target_id, *wanted] if c not in cases]
    if unknown:
        raise rules.CaseRuleError(f"not a case of this organization: {', '.join(unknown[:5])}")
    target = cases[target_id]
    members = await _members(db, [target_id, *wanted])
    held = {f"{r.source}:{r.external_id}" for r in members[target_id]}
    keys = list(target.keys or [])
    pinned = list(target.pinned_issues or [])
    for source_id in wanted:
        source = cases[source_id]
        for key in source.keys or []:
            if key not in keys:
                keys.append(key)
        for member_id in source.pinned_issues or []:
            if member_id not in pinned:
                pinned.append(member_id)
        for row in members[source_id]:
            member_id = f"{row.source}:{row.external_id}"
            if member_id not in held:
                held.add(member_id)
                db.add(CtemCaseIssue(case_id=target_id, source=row.source, external_id=row.external_id, open=row.open))
            await db.delete(row)
        # The merged case's history moves to the survivor: nothing of it is lost.
        await db.execute(update(CtemCaseEvent).where(CtemCaseEvent.case_id == source_id).values(case_id=target_id))
        # So do the signals that opened or joined it (#1529): a signal never outlives its case.
        from app.models.ctem_signal import CtemSignal

        await db.execute(update(CtemSignal).where(CtemSignal.case_id == source_id).values(case_id=target_id))
        db.add(
            _event(
                target_id,
                actor,
                "merge",
                from_value=str(source_id),
                to_value=(", ".join(source.keys or []) or source.title)[:300],
                reason=reason,
                at=now,
            )
        )
        await db.delete(source)
    target.keys = keys
    target.pinned_issues = pinned
    return target


async def list_cases(
    db: AsyncSession, *, organization_id: uuid.UUID, vertical: str
) -> list[tuple[CtemCase, list[CtemCaseIssue]]]:
    """Every case of one vertical with its members; ``ctem_cases_view`` filters it per viewer."""
    cases = await _scope_cases(db, organization_id, vertical)
    members = await _members(db, [c.id for c in cases])
    return [(c, members[c.id]) for c in cases]


#: The evidence of the resolutions recompute itself writes: they are not a decision (#1646).
_RECOMPUTE_EVIDENCE = (rules.CLOSED_AT_SOURCE, rules.FIXED_IN_INVENTORY)


def _decided_closures(history: dict[uuid.UUID, list[CtemCaseEvent]]) -> dict[uuid.UUID, str]:
    """Per resolved case, the evidence of the decision that resolved it (#1646); absent when recompute did.

    *history* is oldest first: the newest transition to ``resolved`` is the closure in force. A
    resolution a person, an agent or a gate wrote carries its own evidence (a gate answer, a ticket,
    a passing re-test), never the evidence recompute writes.
    """
    out: dict[uuid.UUID, str] = {}
    for case_id, rows in history.items():
        closure = next((r for r in reversed(rows) if r.kind == "state" and r.to_value == "resolved"), None)
        if closure is None or (closure.evidence or "") in _RECOMPUTE_EVIDENCE:
            continue
        out[case_id] = (closure.evidence or closure.reason or "resolved by a decision").strip()[:200]
    return out


async def events_of(db: AsyncSession, case_ids: list[uuid.UUID]) -> dict[uuid.UUID, list[CtemCaseEvent]]:
    """The history of several cases, oldest first (the loop measures, CTEM-30 #937)."""
    out: dict[uuid.UUID, list[CtemCaseEvent]] = {cid: [] for cid in case_ids}
    if not case_ids:
        return out
    rows = await db.execute(
        select(CtemCaseEvent).where(CtemCaseEvent.case_id.in_(case_ids)).order_by(CtemCaseEvent.at.asc())
    )
    for row in rows.scalars():
        out.setdefault(row.case_id, []).append(row)
    return out


async def get_case(
    db: AsyncSession, case_id: uuid.UUID, *, organization_id: uuid.UUID
) -> tuple[CtemCase, list[CtemCaseIssue], list[CtemCaseEvent]] | None:
    """One case with its members and history; the caller hydrates it for one viewer."""
    case = (
        await db.execute(select(CtemCase).where(CtemCase.id == case_id, CtemCase.organization_id == organization_id))
    ).scalar_one_or_none()
    if case is None:
        return None
    issues = list((await db.execute(select(CtemCaseIssue).where(CtemCaseIssue.case_id == case.id))).scalars())
    events = list(
        (
            await db.execute(
                select(CtemCaseEvent).where(CtemCaseEvent.case_id == case.id).order_by(CtemCaseEvent.at.desc())
            )
        ).scalars()
    )
    return case, issues, events
