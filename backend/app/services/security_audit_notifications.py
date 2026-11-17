# SPDX-License-Identifier: Apache-2.0

"""Notify administrators of critical and warning Security Audit findings, once each.

The Security Audit (Settings > Access and security, ``GET /api/v1/security-audit``) only
runs when an administrator opens it. The scheduler asks
:func:`notify_new_findings_if_due` on every tick; a Redis lease starts the
audit at most once per :data:`NOTIFY_INTERVAL_SECONDS` install-wide, in the
background (the tick never waits for it), through the builder the route
answers with (``security_audit.build_security_audit_report``), and a finding
at one of :data:`NOTIFIED_SEVERITIES` the administrators were never told about
reaches the notification bell of every active administrator: one notification
per run, critical first, opening the audit on them.

A finding is notified ONCE, keyed by its stable id: not again when it
disappears and comes back, not again when it escalates. A dismissed finding is
never notified; the one exception to "once" is a dismissal re-opened because
the finding is now reported at a higher severity than it was dismissed at,
which is notified once more (``services/security_audit_finding_states.py``).
What was notified is the ``notified_at`` of the finding's row in PostgreSQL,
written in the transaction that commits the notifications, so a failed
delivery is retried by the next run and losing Redis re-notifies nothing:
Redis holds the hourly lease only. With no active administrator nothing is
recorded, so the first one is told. A run whose lease cannot be taken (Redis
unreachable) is skipped, and the next tick asks again.

Upgrade from the earlier version, which remembered the notified findings in Redis
(:data:`LEGACY_STATE_KEY`): while no row records a notification, each run
imports that key first - every finding it held counts as notified - so the
deploy announces nothing the administrators were already told. A run that
cannot read it notifies nothing.

A notification carries the finding titles only: the listings (names, emails)
stay on the admin page. A run that finds nothing to notify logs at DEBUG.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

from sqlalchemy import select

from app.core.database import async_session
from app.core.redis import redis_client
from app.core.utils import format_exception_message
from app.models.user import User
from app.services import security_audit_finding_states as finding_states
from app.services.notify import notify_web_user

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.api.v1.security_audit import AuditFinding

logger = logging.getLogger(__name__)

NOTIFY_INTERVAL_SECONDS = 3600
#: Severity -> rank of the severities that are notified.
NOTIFIED_SEVERITIES: Mapping[str, int] = {"critical": 2, "warning": 1}
LEASE_KEY = "security_audit:notify_lease"
#: The earlier Redis memory, read once at the upgrade.
LEGACY_STATE_KEY = "security_audit:notified"
#: What is left of it after the import, kept for a rollback.
LEGACY_KEY_RETENTION_SECONDS = 7 * 86400
NOTIFICATION_SOURCE = "security_audit"
SOURCE_LABEL = "Security audit"
AUDIT_PATH = "/settings/security"
_LISTED_TITLES = 10
_LINKED_FINDINGS = 20
#: The runs this process started, referenced until they end.
_running: set[asyncio.Task] = set()
#: Whether this process already knows the legacy Redis memory needs no import.
_legacy_memory_settled = False


def _rank(severity: object) -> int:
    return NOTIFIED_SEVERITIES.get(severity, 0) if isinstance(severity, str) else 0


def legacy_notified(raw: str | None) -> dict[str, str]:
    """``{finding id: severity}`` of every finding the earlier Redis memory held; unreadable reads as none."""
    try:
        state = json.loads(raw) if raw else {}
    except ValueError:
        return {}
    findings = state.get("findings") if isinstance(state, dict) else None
    if not isinstance(findings, dict):
        return {}
    return {
        finding_id: entry["severity"]
        for finding_id, entry in findings.items()
        if finding_states.valid_finding_id(finding_id) and isinstance(entry, dict) and _rank(entry.get("severity"))
    }


def due_findings(
    findings: Iterable[AuditFinding], states: Mapping[str, finding_states.FindingState]
) -> list[AuditFinding]:
    """The findings to notify now, critical first: one per id, at its highest notified severity.

    A finding is due unless it is dismissed or the administrators were told
    about it since it was last re-opened (``finding_states.notification_due``).
    """
    due: list[AuditFinding] = []
    seen: set[str] = set()
    for finding in sorted(findings, key=lambda item: -_rank(item.severity)):
        if not _rank(finding.severity) or not finding.id or finding.id in seen:
            continue
        seen.add(finding.id)
        if finding.dismissed is None and finding_states.notification_due(states.get(finding.id)):
            due.append(finding)
    return due


def build_notification(due: list[AuditFinding]) -> tuple[str, str, dict[str, Any]]:
    """Title, summary and ``extra_data`` of the notification announcing *due* (critical first).

    A finding whose dismissal it escalated past says it was re-opened.
    """
    reopened = {finding.id for finding in due if finding.reopened is not None}
    critical = sum(1 for finding in due if finding.severity == "critical")
    if len(due) == 1:
        title = f"{'Re-opened' if due[0].id in reopened else 'New'} {due[0].severity} security audit finding"
    else:
        counts = [
            f"{count} {label}" for count, label in ((critical, "critical"), (len(due) - critical, "warning")) if count
        ]
        kind = "new" if not reopened else "re-opened" if len(reopened) == len(due) else "new or re-opened"
        title = f"{len(due)} {kind} security audit findings ({', '.join(counts)})"
    titles = [finding.title + (" (re-opened)" if finding.id in reopened else "") for finding in due[:_LISTED_TITLES]]
    if len(due) > len(titles):
        titles.append(f"and {len(due) - len(titles)} more")
    ids = [finding.id for finding in due if finding.id]
    audit_path = AUDIT_PATH + "?" + "&".join(f"finding={quote(finding_id)}" for finding_id in ids[:_LINKED_FINDINGS])
    extra = {
        "notification_source": NOTIFICATION_SOURCE,
        "severity": due[0].severity,
        "finding_ids": ids,
        "audit_path": audit_path,
    }
    return title, "; ".join(titles), extra


async def notify_new_findings_if_due() -> bool:
    """Start :func:`notify_new_findings` when this process takes the hourly lease; returns whether it did.

    The run goes on in the background: the scheduler tick holds a lock the next
    ticks skip on, so waiting for the whole audit there would hold back every
    scheduled assignment for as long as it takes.
    """
    try:
        redis = await redis_client()
        if not await redis.set(LEASE_KEY, "1", nx=True, ex=NOTIFY_INTERVAL_SECONDS):
            return False
    except Exception as exc:
        logger.debug("Security audit notification lease not taken: %s", format_exception_message(exc))
        return False
    task = asyncio.create_task(_notify_logged(redis), name="security-audit-notifications")
    _running.add(task)
    task.add_done_callback(_running.discard)
    return True


async def _notify_logged(redis: Any) -> None:
    try:
        await notify_new_findings(redis)
    except Exception as exc:
        logger.error("Security audit notification error: %s", format_exception_message(exc), exc_info=True)


async def import_legacy_memory(db: AsyncSession, redis: Any, *, now: datetime) -> int:
    """Carry the earlier Redis memory into PostgreSQL while no row records a notification; the count.

    A Redis error propagates: the run then notifies nothing rather than every
    finding the administrators were already told about. Checked once per
    process: once the memory holds a row or the import ran, nothing writes the
    legacy key any more.
    """
    global _legacy_memory_settled
    if _legacy_memory_settled:
        return 0
    if not await finding_states.notification_memory_empty(db):
        _legacy_memory_settled = True
        return 0
    notified = legacy_notified(await redis.get(LEGACY_STATE_KEY))
    if not notified:
        _legacy_memory_settled = True
        return 0
    await finding_states.record_notified(db, notified, now=now, only_if_unset=True)
    await db.commit()
    _legacy_memory_settled = True
    logger.info("Security audit: %d finding(s) notified before the upgrade carried over from Redis", len(notified))
    try:
        await redis.expire(LEGACY_STATE_KEY, LEGACY_KEY_RETENTION_SECONDS)
    except Exception as exc:
        logger.debug("Security audit: legacy notification memory left as is: %s", format_exception_message(exc))
    return len(notified)


async def notify_new_findings(redis: Any) -> int:
    """Run the audit and notify every active administrator of the findings due. Returns their count."""
    from app.api.v1.security_audit import build_security_audit_report

    async with async_session() as db:
        report = await build_security_audit_report(db)
        now = datetime.now(UTC)
        await import_legacy_memory(db, redis, now=now)
        # The report shows an escalated dismissal re-opened without writing it;
        # this run records it, so it is notified below and audited once.
        if await finding_states.persist_reopenings(db, report.findings, now=now):
            await db.commit()
        due = due_findings(report.findings, await finding_states.load_states(db))
        if not due:
            logger.debug("Security audit: no finding to notify")
            return 0
        admin_ids = (
            (await db.execute(select(User.id).where(User.role == "admin", User.is_active.is_(True)))).scalars().all()
        )
        if not admin_ids:
            # Nobody to tell: nothing is recorded, so the first active administrator is.
            logger.debug("Security audit: %d finding(s) and no active administrator to notify", len(due))
            return 0
        title, summary, extra = build_notification(due)
        for admin_id in admin_ids:
            await notify_web_user(
                db, str(admin_id), title=title, summary=summary, source_label=SOURCE_LABEL, extra_data={**extra}
            )
        await finding_states.record_notified(
            db, {finding.id: finding.severity for finding in due if finding.id}, now=now
        )
        await db.commit()
        logger.info(
            "Security audit: %d finding(s) notified to %d administrator(s): %s",
            len(due),
            len(admin_ids),
            ", ".join(extra["finding_ids"]),
        )
    return len(due)
