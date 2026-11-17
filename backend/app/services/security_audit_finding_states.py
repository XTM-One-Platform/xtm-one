# SPDX-License-Identifier: Apache-2.0

"""Dismissed Security Audit findings and the notification memory.

The audit computes its findings on every run; what the platform remembers about
one is a ``security_audit_finding_states`` row keyed by the finding's stable id
(``<category>.<name>``). Every rule about that row lives here:

- **Dismissal.** An administrator may accept a ``critical`` / ``warning`` /
  ``info`` finding the current run reports (never a ``pass``, never an id the
  run does not report: an id is refused unless the audit states it now). A
  dismissed finding stays in the report, marked, and leaves every count and
  the overall score; it is never notified. Dismissing and restoring are
  ``log_audit`` entries (``dismiss`` / ``restore`` on a
  ``security_audit_finding``).
- **Re-opening.** A dismissal is about the severity it was made at. A run
  that reports the finding at a HIGHER severity no longer applies it: the
  report shows the finding active, counted and marked re-opened, computed in
  memory (``finding_views`` writes nothing). The re-opening is RECORDED by the
  hourly notifier and by the dismiss / restore routes (``persist_reopenings``):
  the dismissal is cleared - its who / severity / reason kept as history on the
  row - and the finding is notified once more. The platform does it, so it is a
  ``log_system_audit`` entry anchored on the administrator who had dismissed it.
- **Restore.** Clears the dismissal in force and keeps the last one's who /
  severity / reason on the row, with who restored it and when.
- **Notification memory.** ``notified_at`` is when the administrators were told
  about the finding. It is notified once, ever (``notification_due``): no
  second notice when it disappears and comes back, none on escalation - the
  one exception is a re-opened dismissal. It lives in PostgreSQL, so losing
  Redis re-notifies nothing.

Reading the rows never fails the audit: ``finding_views`` answers "no state"
and logs a warning.
"""

from __future__ import annotations

import logging
import re
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, fields
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Protocol

from sqlalchemy import exists, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.core.utils import format_exception_message
from app.models.security_audit_finding_state import (
    DISMISSAL_REASON_MAX_CHARS,
    FINDING_ID_MAX_CHARS,
    SecurityAuditFindingState,
)
from app.models.user import User
from app.services.audit import log_audit, log_system_audit

if TYPE_CHECKING:
    from fastapi import Request
    from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

#: The order of the audit's severities; a finding re-opens past its dismissal
#: only by climbing it. An unknown severity ranks with ``pass``.
SEVERITY_RANK: Mapping[str, int] = {"pass": 0, "info": 1, "warning": 2, "critical": 3}
DISMISSABLE_SEVERITIES = frozenset({"critical", "warning", "info"})

#: The shape of every finding id the audit builds (``<category>.<name>``).
FINDING_ID_PATTERN = r"^[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$"
_FINDING_ID_RE = re.compile(r"[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*")

AUDIT_ENTITY_TYPE = "security_audit_finding"
SYSTEM_ACTOR = "security_audit"

_TABLE = SecurityAuditFindingState.__table__


class ReportedFinding(Protocol):
    id: str | None
    severity: str
    title: str


@dataclass(frozen=True)
class FindingState:
    """One ``security_audit_finding_states`` row, detached from any session."""

    finding_id: str
    notified_at: datetime | None = None
    notified_severity: str | None = None
    dismissed_at: datetime | None = None
    dismissed_by_user_id: uuid.UUID | None = None
    dismissed_severity: str | None = None
    dismissal_reason: str | None = None
    reopened_at: datetime | None = None
    reopened_severity: str | None = None
    restored_at: datetime | None = None
    restored_by_user_id: uuid.UUID | None = None


_STATE_FIELDS = tuple(field.name for field in fields(FindingState))


@dataclass(frozen=True)
class DismissalView:
    """A dismissal: the one in force, or (``at`` None) the one a re-opening cleared."""

    at: datetime | None
    severity: str
    reason: str | None
    by_user_id: uuid.UUID | None
    by: str | None  # the administrator's name, or email; None once the account is gone


@dataclass(frozen=True)
class FindingView:
    """What the report says about one finding it reports.

    A re-opening not recorded yet has a ``reopened_severity`` and no
    ``reopened_at``.
    """

    dismissed: DismissalView | None = None
    reopened_at: datetime | None = None
    reopened_severity: str | None = None
    reopened_dismissal: DismissalView | None = None

    @property
    def reopened(self) -> bool:
        return self.reopened_severity is not None


def severity_rank(severity: str | None) -> int:
    return SEVERITY_RANK.get(severity or "", 0)


def valid_finding_id(finding_id: object) -> bool:
    return (
        isinstance(finding_id, str)
        and len(finding_id) <= FINDING_ID_MAX_CHARS
        and _FINDING_ID_RE.fullmatch(finding_id) is not None
    )


def clean_reason(reason: str | None) -> str | None:
    """A dismissal reason as stored: trimmed, blank = none, bounded."""
    text = (reason or "").strip()
    return text[:DISMISSAL_REASON_MAX_CHARS] or None


def reported_severities(findings: Iterable[ReportedFinding]) -> dict[str, str]:
    """Each finding id the run reports, at the highest severity it reports it."""
    severities: dict[str, str] = {}
    for finding in findings:
        if not finding.id:
            continue
        known = severities.get(finding.id)
        if known is None or severity_rank(finding.severity) > severity_rank(known):
            severities[finding.id] = finding.severity
    return severities


def reported_finding[F: ReportedFinding](findings: Iterable[F], finding_id: str) -> F | None:
    """The finding the run reports under *finding_id*, at the highest severity it reports it."""
    matches = [finding for finding in findings if finding.id == finding_id]
    return max(matches, key=lambda finding: severity_rank(finding.severity), default=None)


def escalated_past_dismissal(state: FindingState | None, severity: str) -> bool:
    """Whether the run reports a dismissed finding above the severity it was dismissed at."""
    return (
        state is not None
        and state.dismissed_at is not None
        and state.dismissed_severity is not None
        and severity_rank(severity) > severity_rank(state.dismissed_severity)
    )


def notification_due(state: FindingState | None) -> bool:
    """Whether a reported finding is still to be notified: never told, or re-opened since."""
    if state is None:
        return True
    if state.dismissed_at is not None:
        return False
    if state.notified_at is None:
        return True
    return state.reopened_at is not None and state.reopened_at > state.notified_at


async def load_states(db: AsyncSession) -> dict[str, FindingState]:
    """Every row, read as plain values (never ORM instances a later UPDATE would leave stale)."""
    rows = (await db.execute(select(_TABLE))).mappings().all()
    return {row["finding_id"]: FindingState(**{name: row[name] for name in _STATE_FIELDS}) for row in rows}


async def _admin_names(db: AsyncSession, user_ids: set[uuid.UUID]) -> dict[uuid.UUID, str]:
    if not user_ids:
        return {}
    rows = (await db.execute(select(User.id, User.full_name, User.email).where(User.id.in_(user_ids)))).all()
    return {row.id: (row.full_name or row.email) for row in rows}


def _dismissal_view(state: FindingState, names: Mapping[uuid.UUID, str]) -> DismissalView | None:
    if state.dismissed_severity is None:
        return None
    by_user_id = state.dismissed_by_user_id
    return DismissalView(
        at=state.dismissed_at,
        severity=state.dismissed_severity,
        reason=state.dismissal_reason,
        by_user_id=by_user_id,
        by=names.get(by_user_id) if by_user_id is not None else None,
    )


async def _reopen_anchor(db: AsyncSession, state: FindingState) -> uuid.UUID | None:
    """Who a re-opening is recorded against: the dismisser, else the longest-standing admin."""
    if state.dismissed_by_user_id is not None:
        return state.dismissed_by_user_id
    return (
        await db.execute(
            select(User.id)
            .where(User.role == "admin", User.is_active.is_(True))
            .order_by(User.created_at, User.id)
            .limit(1)
        )
    ).scalar_one_or_none()


async def reopen_escalated(
    db: AsyncSession,
    states: Mapping[str, FindingState],
    severities: Mapping[str, str],
    *,
    now: datetime,
) -> list[str]:
    """Re-open every dismissal the run reports above its severity; the ids re-opened.

    Conditional on the dismissal read: a concurrent restore or a new dismissal
    wins, and two runs re-open once. Not committed here.
    """
    reopened: list[str] = []
    for finding_id, severity in severities.items():
        state = states.get(finding_id)
        if state is None or not escalated_past_dismissal(state, severity):
            continue
        result = await db.execute(
            update(_TABLE)
            .where(_TABLE.c.finding_id == finding_id, _TABLE.c.dismissed_at == state.dismissed_at)
            .values(dismissed_at=None, reopened_at=now, reopened_severity=severity, updated_at=now)
        )
        if not result.rowcount:
            continue
        reopened.append(finding_id)
        anchor = await _reopen_anchor(db, state)
        if anchor is None:
            logger.warning(
                "Security audit: finding %s re-opened at %s; no administrator to record it against",
                finding_id,
                severity,
            )
            continue
        await log_system_audit(
            db,
            actor=SYSTEM_ACTOR,
            anchor_user_id=anchor,
            action="reopen",
            entity_type=AUDIT_ENTITY_TYPE,
            entity_name=finding_id,
            summary=(
                f"Re-opened the dismissed Security Audit finding {finding_id}: "
                f"reported as {severity}, dismissed as {state.dismissed_severity}"
            ),
            details={
                "finding_id": finding_id,
                "severity": severity,
                "dismissed_severity": state.dismissed_severity,
                "dismissed_at": state.dismissed_at.isoformat() if state.dismissed_at else None,
                "dismissed_by_user_id": str(state.dismissed_by_user_id) if state.dismissed_by_user_id else None,
                "reason": state.dismissal_reason,
            },
        )
    return reopened


async def persist_reopenings(
    db: AsyncSession, findings: Iterable[ReportedFinding], *, now: datetime | None = None
) -> list[str]:
    """Record the re-opening of every dismissal the run reports above its severity. Not committed.

    The notifier passes the ``now`` it records the notification at: a
    re-opening stamped after it would read as never notified at every run.
    """
    return await reopen_escalated(
        db, await load_states(db), reported_severities(findings), now=now or datetime.now(UTC)
    )


async def finding_views(db: AsyncSession, findings: Iterable[ReportedFinding]) -> dict[str, FindingView]:
    """The state of each finding the run reports. Reads only.

    A dismissal the run reports above its severity is not applied: the finding
    is shown re-opened, ahead of ``persist_reopenings`` recording it. Fails
    open: a read that fails leaves every finding counted, with a warning.
    """
    severities = reported_severities(findings)
    try:
        async with db.begin_nested():
            states = await load_states(db)
            names = await _admin_names(
                db,
                {state.dismissed_by_user_id for state in states.values() if state.dismissed_by_user_id is not None},
            )
    except Exception as exc:
        logger.warning("Security audit: cannot read the finding states: %s", format_exception_message(exc))
        return {}

    views: dict[str, FindingView] = {}
    for finding_id, severity in severities.items():
        state = states.get(finding_id)
        if state is None:
            continue
        if escalated_past_dismissal(state, severity):
            views[finding_id] = FindingView(
                reopened_severity=severity, reopened_dismissal=_dismissal_view(state, names)
            )
        elif state.dismissed_at is not None:
            views[finding_id] = FindingView(dismissed=_dismissal_view(state, names))
        elif state.reopened_at is not None:
            views[finding_id] = FindingView(
                reopened_at=state.reopened_at,
                reopened_severity=state.reopened_severity,
                reopened_dismissal=_dismissal_view(state, names),
            )
    return views


async def dismiss_finding(
    db: AsyncSession,
    *,
    admin: User,
    finding: ReportedFinding,
    reason: str | None,
    request: Request | None = None,
) -> None:
    """Record *admin*'s dismissal of a finding the run reports, and audit it. Not committed."""
    if not finding.id or not valid_finding_id(finding.id):
        raise ValueError("Only a finding with a stable id can be dismissed")
    now = datetime.now(UTC)
    reason = clean_reason(reason)
    statement = pg_insert(_TABLE).values(
        finding_id=finding.id,
        dismissed_at=now,
        dismissed_by_user_id=admin.id,
        dismissed_severity=finding.severity,
        dismissal_reason=reason,
        created_at=now,
        updated_at=now,
    )
    await db.execute(
        statement.on_conflict_do_update(
            index_elements=[_TABLE.c.finding_id],
            set_={
                "dismissed_at": statement.excluded.dismissed_at,
                "dismissed_by_user_id": statement.excluded.dismissed_by_user_id,
                "dismissed_severity": statement.excluded.dismissed_severity,
                "dismissal_reason": statement.excluded.dismissal_reason,
                "reopened_at": None,
                "reopened_severity": None,
                "restored_at": None,
                "restored_by_user_id": None,
                "updated_at": statement.excluded.updated_at,
            },
        )
    )
    await log_audit(
        db,
        user=admin,
        action="dismiss",
        entity_type=AUDIT_ENTITY_TYPE,
        entity_name=finding.id,
        summary=f"Dismissed the {finding.severity} Security Audit finding {finding.id}",
        details={
            "finding_id": finding.id,
            "severity": finding.severity,
            "title": finding.title,
            "reason": reason,
        },
        request=request,
    )


async def restore_finding(
    db: AsyncSession,
    *,
    admin: User,
    finding_id: str,
    request: Request | None = None,
) -> bool:
    """Undo a dismissal and audit it; ``False`` when the finding is not dismissed. Not committed.

    The last dismissal's who / severity / reason stay on the row as history.
    """
    row = (
        (
            await db.execute(
                select(_TABLE)
                .where(_TABLE.c.finding_id == finding_id, _TABLE.c.dismissed_at.is_not(None))
                .with_for_update()
            )
        )
        .mappings()
        .first()
    )
    if row is None:
        return False
    now = datetime.now(UTC)
    await db.execute(
        update(_TABLE)
        .where(_TABLE.c.finding_id == finding_id)
        .values(
            dismissed_at=None,
            reopened_at=None,
            reopened_severity=None,
            restored_at=now,
            restored_by_user_id=admin.id,
            updated_at=now,
        )
    )
    await log_audit(
        db,
        user=admin,
        action="restore",
        entity_type=AUDIT_ENTITY_TYPE,
        entity_name=finding_id,
        summary=f"Restored the Security Audit finding {finding_id}, dismissed as {row['dismissed_severity']}",
        details={
            "finding_id": finding_id,
            "dismissed_severity": row["dismissed_severity"],
            "dismissed_at": row["dismissed_at"].isoformat(),
            "dismissed_by_user_id": str(row["dismissed_by_user_id"]) if row["dismissed_by_user_id"] else None,
            "reason": row["dismissal_reason"],
        },
        request=request,
    )
    return True


async def notification_memory_empty(db: AsyncSession) -> bool:
    """Whether no finding was ever recorded as notified."""
    return not (await db.execute(select(exists().where(_TABLE.c.notified_at.is_not(None))))).scalar()


async def record_notified(
    db: AsyncSession,
    notified: Mapping[str, str],
    *,
    now: datetime,
    only_if_unset: bool = False,
) -> None:
    """Record ``{finding id: severity}`` as notified at *now*. Not committed.

    ``only_if_unset`` keeps a notification already recorded (the legacy import).
    """
    rows = [
        {
            "finding_id": finding_id,
            "notified_at": now,
            "notified_severity": severity,
            "created_at": now,
            "updated_at": now,
        }
        for finding_id, severity in notified.items()
        if valid_finding_id(finding_id)
    ]
    if not rows:
        return
    statement = pg_insert(_TABLE).values(rows)
    await db.execute(
        statement.on_conflict_do_update(
            index_elements=[_TABLE.c.finding_id],
            set_={
                "notified_at": statement.excluded.notified_at,
                "notified_severity": statement.excluded.notified_severity,
                "updated_at": statement.excluded.updated_at,
            },
            where=_TABLE.c.notified_at.is_(None) if only_if_unset else None,
        )
    )
