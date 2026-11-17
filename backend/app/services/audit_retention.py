# SPDX-License-Identifier: Apache-2.0

"""Audit log retention: the hourly purge of ``audit_logs`` past the configured window.

``platform_settings.audit_log_retention_days`` (Settings > Usage and quotas >
Observability, 90 days by default) bounds how long an audit entry is kept.
The trace maintenance tick (``services/autonomous.py``) calls
:func:`purge_expired_audit_logs` hourly with that setting.

Deletion runs in batches of ids, each batch its own short transaction, so a
table of hundreds of thousands of rows is never locked for the whole purge and
a first purge after an upgrade does not hold one long transaction. The range
scan rides ``ix_audit_logs_created_at``. The purge is idempotent: a second run
over the same window deletes nothing.

One row per user outlives the window: the user's most recent completed sign-in
(``action = 'login'`` on ``entity_type = 'user'``, the predicate of
``ix_audit_logs_sign_ins``). The Users page's last login and the Security
Audit's dormant-account check read it, and an account nobody signed in to for
longer than the window would otherwise read as never signed in.
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime, timedelta

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

#: Default window, also the column's server default.
DEFAULT_AUDIT_LOG_RETENTION_DAYS = 90
#: Lower bound: the Security Audit's dormant-account check reads 30 days of sign-ins.
MIN_AUDIT_LOG_RETENTION_DAYS = 30
#: Upper bound: ten years.
MAX_AUDIT_LOG_RETENTION_DAYS = 3650

_BATCH_SIZE = 5000
#: Batches per run: bounds one tick's work; the next tick continues.
_MAX_BATCHES_PER_RUN = 200

# The literal values match the partial index ``ix_audit_logs_sign_ins`` so the
# plan can use it for the per-user latest sign-in.
_BATCH_SQL = text(
    """
    SELECT a.id
    FROM audit_logs a
    WHERE a.created_at < :cutoff
      AND a.id NOT IN (
          SELECT DISTINCT ON (s.user_id) s.id
          FROM audit_logs s
          WHERE s.action = 'login' AND s.entity_type = 'user'
          ORDER BY s.user_id, s.created_at DESC
      )
    ORDER BY a.created_at
    LIMIT :batch_size
    """
)

_DELETE_SQL = text("DELETE FROM audit_logs WHERE id = ANY(:ids)")


def audit_log_retention_days(value: int | None) -> int:
    """The effective window for a stored setting: the default when unset, clamped to the bounds."""
    if value is None:
        return DEFAULT_AUDIT_LOG_RETENTION_DAYS
    return max(MIN_AUDIT_LOG_RETENTION_DAYS, min(MAX_AUDIT_LOG_RETENTION_DAYS, int(value)))


def audit_purge_cutoff(retention_days: int, now: datetime | None = None) -> datetime:
    """Entries created before this instant are past the window."""
    return (now or datetime.now(UTC)) - timedelta(days=audit_log_retention_days(retention_days))


async def purge_expired_audit_logs(db: AsyncSession, retention_days: int | None, *, now: datetime | None = None) -> int:
    """Delete the audit entries older than *retention_days*, in batches; return the rows deleted.

    Each batch commits on its own. Logs one line with the rows purged, the
    cutoff and the duration when anything was deleted.
    """
    days = audit_log_retention_days(retention_days)
    cutoff = audit_purge_cutoff(days, now)
    started = time.monotonic()
    total = 0
    for _ in range(_MAX_BATCHES_PER_RUN):
        rows = await db.execute(_BATCH_SQL, {"cutoff": cutoff, "batch_size": _BATCH_SIZE})
        ids = [row[0] for row in rows.all()]
        if not ids:
            break
        await db.execute(_DELETE_SQL, {"ids": ids})
        await db.commit()
        total += len(ids)
        if len(ids) < _BATCH_SIZE:
            break
    if total:
        logger.info(
            "Audit log retention purge: %d row(s) older than %d day(s) (before %s) deleted in %.2fs",
            total,
            days,
            cutoff.isoformat(),
            time.monotonic() - started,
        )
    return total
