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
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.ctem_case import CtemCase, CtemCaseEvent, CtemCaseIssue
from app.services import ctem_cases as rules

#: Most cases one list page returns.
MAX_PAGE = 200


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
    now: datetime | None = None,
) -> dict[str, int]:
    """Merge one user's view into the shared cases of one vertical.

    *issues* are the open issues that user sees, *closed* the member ids their
    read confirmed closed. Answers the counts written: created, updated,
    resolved, reopened, no_known_fix (counts of this user's own view only).
    """
    now = now or datetime.now(UTC)
    issues = list(issues)
    cases = await _scope_cases(db, organization_id, vertical)
    members = await _members(db, [c.id for c in cases])
    by_id = {str(c.id): c for c in cases}
    snapshots = [
        rules.CaseSnapshot(
            id=str(c.id),
            keys=list(c.keys or []),
            state=c.state,
            pinned_issues=list(c.pinned_issues or []),
            members={f"{r.source}:{r.external_id}": r.open for r in members[c.id]},
        )
        for c in cases
    ]
    counts = {"created": 0, "updated": 0, "resolved": 0, "reopened": 0}
    for plan in rules.plan_recompute(snapshots, issues, closed):
        if plan.case_id is None:
            remediation = plan.remediation
            if remediation is None:  # a new-case plan always carries its remediation
                continue
            case = CtemCase(
                id=uuid.uuid4(),
                organization_id=organization_id,
                vertical=vertical,
                title=remediation.title,
                remediation_kind=remediation.kind,
                product_label=remediation.product_label,
                fixed_version=remediation.fixed_version,
                keys=[remediation.key],
                pinned_issues=[],
                state="needs_attention",
                state_since=now,
            )
            db.add(case)
            await db.flush()
            for member_id in plan.add:
                source, external_id = _split_member(member_id)
                db.add(CtemCaseIssue(case_id=case.id, source=source, external_id=external_id, open=True))
            db.add(_event(case.id, actor, "created", to_value=remediation.key, at=now))
            counts["created"] += 1
            continue
        case = by_id[plan.case_id]
        rows = {f"{r.source}:{r.external_id}": r for r in members[case.id]}
        for member_id in plan.add:
            source, external_id = _split_member(member_id)
            db.add(CtemCaseIssue(case_id=case.id, source=source, external_id=external_id, open=True))
            db.add(_event(case.id, actor, "issue_added", issue=member_id, at=now))
        for member_id in plan.reopen:
            rows[member_id].open = True
            db.add(_event(case.id, actor, "issue_reopened", issue=member_id, at=now))
        for member_id in plan.resolve:
            rows[member_id].open = False
            db.add(_event(case.id, actor, "issue_closed", issue=member_id, evidence=rules.CLOSED_AT_SOURCE, at=now))
        if plan.add or plan.reopen or plan.resolve:
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
            case.state = plan.new_state
            case.state_since = now
    counts["no_known_fix"] = len(rules.group_issues(issues).no_known_fix)
    return counts


async def expire_acceptances(db: AsyncSession, *, now: datetime | None = None) -> int:
    """Return every case whose accepted risk expired to needs attention; answers how many."""
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
                to_value="needs_attention",
                reason="accepted risk expired",
                at=now,
            )
        )
        case.state = "needs_attention"
        case.state_since = now
        expired += 1
    return expired


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
        rules.check_transition(
            current=case.state,
            target=state,
            actor_kind=actor_kind,
            owner_user_id=new_owner,
            reason=reason,
            evidence=evidence,
            accepted_until=accepted_until,
            now=now,
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
    now: datetime | None = None,
) -> CtemCase:
    """Split open issues out of their cases into a new manual case (``dev-docs/ctem-cases.md`` section 5).

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
        state="needs_attention",
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
