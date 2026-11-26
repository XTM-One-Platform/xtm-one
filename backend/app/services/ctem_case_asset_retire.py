# SPDX-License-Identifier: Apache-2.0

"""Close the CTEM cases of an asset the inventory archived or removed (ctem-autoresolve-archived-asset).

A case opened from asset signals (control decay, blind spot, an exposure of a
host) stayed open, and its validation gate kept asking a person to test the
asset, after the inventory marked that asset ``inactive`` (an archived code
repository, a retired host) or after its last record was deleted. This pass,
run by the scheduled case sweep (:func:`app.services.ctem_case_loop.sweep`),
closes them on that deterministic read of the inventory, rules only:

- a signal member is retired when its subject names an inventory asset (the
  record's name or external id) whose every record is ``inactive``, or when no
  record holds the name any more and the change log shows a record of that name
  deleted; a subject the inventory never held is never retired (an outside-lens
  hostname with no record stays open: doubt moves forward);
- a case is resolved only when every open member is such a signal; a case with a
  source issue still open, or one live asset among its signals, keeps its state;
- the case is resolved ``not_exposed`` with the evidence ``asset archived`` or
  ``asset removed`` and raises ``ctem_case.resolved``; each open decision item
  of the case (``ctem:<gate>:<case id>``) is cancelled with that reason, so no
  person is asked to test an asset that no longer exists.

The caller commits and publishes the pending events.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.ctem_case import OPEN_STATES, CtemCase, CtemCaseIssue
from app.models.ctem_signal import CtemSignal

logger = logging.getLogger(__name__)

#: The subject kind of the signals an inventory asset retires.
ASSET_SUBJECT = "asset"
#: The evidence written on the closed members and the resolved case.
ARCHIVED = "asset archived"
REMOVED = "asset removed"
#: The inventory status of an archived or retired asset (``AssetEntry.status``).
INACTIVE = "inactive"
#: The resolution of a case whose asset left the inventory: nothing is exposed any more.
RESOLUTION = "not_exposed"
#: Most asset signals of open cases one pass reads.
PASS_BATCH = 2000
#: The result written on a decision item this pass cancelled.
CANCELLED_ITEM_RESULT = "Cancelled: CTEM case {case_id} resolved, {evidence} ({subject}): nothing is left to decide."
#: The outcome written on a timer this pass cancelled.
CANCELLED_TIMER_OUTCOME = "cancelled: CTEM case {case_id} resolved, {evidence}"


def subject_key(value: Any) -> str:
    """The key an asset subject is matched on (the records' ``name_key``: whitespace folded, case-folded)."""
    return " ".join(str(value or "").split()).casefold()


def retired_of(
    keys: Iterable[str],
    records: Iterable[tuple[str, str, str]],
    deleted_names: Iterable[str],
) -> dict[str, str]:
    """``{key: evidence}`` of the keys the inventory retired. Pure.

    *records* are ``(name key, external id key, status)`` of the live records that
    match a key, *deleted_names* the name keys of the deleted records. A key with a
    live record is retired only when every one of its records is inactive; a key
    with none is retired only when a record of that name was deleted.
    """
    statuses: dict[str, list[str]] = {}
    for name_key, external_key, status in records:
        for key in {name_key, external_key} - {""}:
            statuses.setdefault(key, []).append((status or "").strip().lower())
    deleted = set(deleted_names)
    out: dict[str, str] = {}
    for key in dict.fromkeys(k for k in keys if k):
        held = statuses.get(key)
        if held:
            if all(s == INACTIVE for s in held):
                out[key] = ARCHIVED
        elif key in deleted:
            out[key] = REMOVED
    return out


async def _retired(db: AsyncSession, organization_id: uuid.UUID, keys: list[str]) -> dict[str, str]:
    from app.models.organization_asset_record import OrganizationAssetChange, OrganizationAssetRecord

    if not keys:
        return {}
    records = (
        await db.execute(
            select(
                OrganizationAssetRecord.name_key,
                func.lower(OrganizationAssetRecord.external_id),
                OrganizationAssetRecord.fields["status"].astext,
            ).where(
                OrganizationAssetRecord.organization_id == organization_id,
                or_(
                    OrganizationAssetRecord.name_key.in_(keys),
                    func.lower(OrganizationAssetRecord.external_id).in_(keys),
                ),
            )
        )
    ).all()
    held = {k for name_key, external_key, _ in records for k in (name_key, external_key)}
    missing = [k for k in keys if k not in held]
    deleted: list[str] = []
    if missing:
        name = func.lower(OrganizationAssetChange.before["name"].astext)
        deleted = [
            str(row[0])
            for row in (
                await db.execute(
                    select(name)
                    .where(
                        OrganizationAssetChange.organization_id == organization_id,
                        OrganizationAssetChange.entity == "record",
                        OrganizationAssetChange.op == "delete",
                        name.in_(missing),
                    )
                    .distinct()
                )
            ).all()
            if row[0]
        ]
    return retired_of(keys, [(str(a or ""), str(b or ""), str(c or "")) for a, b, c in records], deleted)


async def _cancel_decisions(db: AsyncSession, case: CtemCase, *, evidence: str, subject: str, now: datetime) -> int:
    """Cancel the case's open items and armed timers, as a delete and a person's close do (#2494)."""
    from app.services.ctem_case_delete import cancel_case_work

    items, _ = await cancel_case_work(
        db,
        case,
        item_result=CANCELLED_ITEM_RESULT.format(case_id=case.id, evidence=evidence, subject=subject[:200]),
        timer_outcome=CANCELLED_TIMER_OUTCOME.format(case_id=case.id, evidence=evidence),
        now=now,
    )
    return len(items)


async def close_retired_asset_cases(db: AsyncSession, *, events: Any = None, now: datetime | None = None) -> int:
    """Resolve the open cases whose every open member is a signal on an archived or removed asset.

    Answers how many cases were resolved. The caller commits and publishes *events*
    (a ``ctem_case_loop.PendingEvents``).
    """
    from app.services import ctem_case_loop as loop
    from app.services import ctem_cases as rules

    now = now or datetime.now(UTC)
    signals = list(
        (
            await db.execute(
                select(CtemSignal)
                .join(CtemCase, CtemCase.id == CtemSignal.case_id)
                .where(CtemCase.state.in_(OPEN_STATES), CtemSignal.subject_kind == ASSET_SUBJECT)
                .limit(PASS_BATCH)
            )
        )
        .scalars()
        .all()
    )
    if not signals:
        return 0
    by_org: dict[uuid.UUID, list[CtemSignal]] = {}
    for signal in signals:
        by_org.setdefault(signal.organization_id, []).append(signal)
    retired_signal: dict[str, tuple[str, str]] = {}
    for organization_id, rows in by_org.items():
        retired = await _retired(db, organization_id, list(dict.fromkeys(subject_key(s.subject_ref) for s in rows)))
        for signal in rows:
            evidence = retired.get(subject_key(signal.subject_ref))
            if evidence:
                retired_signal[str(signal.id)] = (evidence, signal.subject_ref)
    if not retired_signal:
        return 0
    case_ids = list(dict.fromkeys(s.case_id for s in signals if str(s.id) in retired_signal and s.case_id))
    resolved = 0
    for case_id in case_ids:
        members = list(
            (
                await db.execute(
                    select(CtemCaseIssue).where(CtemCaseIssue.case_id == case_id, CtemCaseIssue.open.is_(True))
                )
            )
            .scalars()
            .all()
        )
        if not members or any(m.source != loop.SIGNAL_SOURCE or m.external_id not in retired_signal for m in members):
            continue
        case = await loop.locked_case(db, case_id)
        if case is None or case.state not in OPEN_STATES:
            continue
        found = [retired_signal[m.external_id] for m in members]
        evidence = REMOVED if all(e == REMOVED for e, _ in found) else ARCHIVED
        subject = ", ".join(dict.fromkeys(s for _, s in found))
        for member, (member_evidence, member_subject) in zip(members, found, strict=True):
            member.open = False
            db.add(
                loop._event(
                    case.id,
                    rules.SYSTEM_ACTOR,
                    "issue_closed",
                    now,
                    issue=f"{loop.SIGNAL_SOURCE}:{member.external_id}",
                    evidence=member_evidence,
                    reason=f"{member_evidence}: {member_subject}"[:300],
                )
            )
        db.add(loop._transition(case, "resolved", now, reason=f"{evidence}: {subject}"[:300], evidence=evidence))
        case.resolution_kind = RESOLUTION
        cancelled = await _cancel_decisions(db, case, evidence=evidence, subject=subject, now=now)
        resolved += 1
        logger.info("CTEM case %s resolved: %s (%s), %d decision(s) cancelled", case.id, evidence, subject, cancelled)
        if events is not None:
            events.add(
                loop.CASE_RESOLVED,
                loop.case_data(case),
                loop._event_id(loop.CASE_RESOLVED, case, f"retired:{now.isoformat()}"),
            )
            events.verticals.add(case.vertical)
            events.frame(case, "state", now)
    if resolved:
        await db.flush()
    return resolved


__all__ = [
    "ARCHIVED",
    "REMOVED",
    "RESOLUTION",
    "close_retired_asset_cases",
    "retired_of",
    "subject_key",
]
