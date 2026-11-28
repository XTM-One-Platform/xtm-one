# SPDX-License-Identifier: Apache-2.0

"""Retention of gate-discarded, watchlist and unmatched CTEM signals (#2581, #2726).

A signal is otherwise only deleted by the administrator's purge (#2528) or with
its organization; this sweep keeps the table from growing with signals nobody
will act on. Three windows, keys of ``platform_settings.coordination_retention``
(``services/coordination_retention.py``) next to the other retention windows:

- ``ctem_signal_discarded_days`` (default ``CTEM_SIGNAL_DISCARDED_RETENTION_DAYS``,
  30): a signal the scope gate discarded and not seen since (``last_seen_at``)
  is deleted. A later sighting records it again and the gate decides it anew.
- ``ctem_signal_watchlist_days`` (default ``CTEM_SIGNAL_WATCHLIST_RETENTION_DAYS``,
  90): a watchlist signal not seen since is deleted. Every new sighting of a
  watchlist signal is decided again by the gate
  (``ctem_scope_gate_store.evaluate_after_record``).
- ``ctem_signal_unmatched_days`` (default ``CTEM_SIGNAL_UNMATCHED_RETENTION_DAYS``,
  30, #2726): an unmatched (``to_validate``) signal not seen since is deleted.
  The same window bounds the profile-change re-decision
  (``ctem_scope_gate_store.recheck_parked``): an older unmatched signal is left
  as it is until its next sighting decides it again.

0 turns a rule off. A signal is never deleted when:

- a person dismissed it (``decision.reason_code`` ``dismissed_by_user``): deleting
  it would bring it back at its next sighting;
- it is linked to a case: it names one (``case_id``) or a case holds it as a
  ``signal`` member;
- a purge of the signals runs (the whole sweep is skipped).

Deletes go :data:`SIGNAL_RETENTION_BATCH` per transaction; one pass deletes at
most :data:`MAX_BATCHES_PER_PASS` batches per state and answers whether more may
be left, so the retention drain of the hourly tick runs it again. One platform
audit row per pass that deleted anything, with the counts per state.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import String, cast, delete, exists, or_, select

from app.models.ctem_case import CtemCaseIssue
from app.models.ctem_signal import CtemSignal

logger = logging.getLogger(__name__)

#: Signals one delete transaction removes.
SIGNAL_RETENTION_BATCH = 1000
#: Batches one pass deletes per state at most; a pass that reached it may have left more.
MAX_BATCHES_PER_PASS = 5
#: The ``system:<actor>`` the audit row of a pass names.
SIGNAL_RETENTION_AUDIT_ACTOR = "ctem_signal_retention"
#: The ``decision.reason_code`` of a signal a person dismissed (``ctem_scope_gate_store.DISMISSED_CODE``).
DISMISSED_CODE = "dismissed_by_user"
#: The states the sweep deletes, and the window key of each.
STATE_WINDOWS: dict[str, str] = {
    "discarded": "ctem_signal_discarded_days",
    "watchlist": "ctem_signal_watchlist_days",
    "to_validate": "ctem_signal_unmatched_days",
}
#: The states whose rule keeps a signal a person dismissed (#2726: unmatched signals too).
_DISMISSAL_GUARDED: frozenset[str] = frozenset({"discarded", "to_validate"})


def _not_dismissed() -> Any:
    """SQL: the signal is not one a person dismissed (no decision, or another reason)."""
    reason = CtemSignal.decision["reason_code"].as_string()
    return or_(CtemSignal.decision.is_(None), reason.is_(None), reason != DISMISSED_CODE)


def _not_in_a_case() -> Any:
    """SQL: no case is named by the signal and no case holds it as a ``signal`` member."""
    member = exists().where(
        CtemCaseIssue.source == "signal",
        CtemCaseIssue.external_id == cast(CtemSignal.id, String),
    )
    return CtemSignal.case_id.is_(None) & ~member


def expired_rules(state: str, cutoff: datetime) -> list[Any]:
    """SQL: a *state* signal not seen since *cutoff*, neither dismissed by a person nor in a case."""
    rules = [CtemSignal.state == state, CtemSignal.last_seen_at < cutoff, _not_in_a_case()]
    if state in _DISMISSAL_GUARDED:
        rules.append(_not_dismissed())
    return rules


async def purge_running() -> bool:
    """True while a purge of every signal (#2528) runs or holds intake paused, on any pod.

    Reads the purge job's own guards: its in-process jobs, the intake pause and the
    job lock in Redis. A Redis error answers False for the lock (the pause read fails
    open the same way): a lost store never stops retention for good.
    """
    from app.services import ctem_signal_purge as purge

    if any(not task.done() for task in purge._JOBS):
        return True
    if await purge.intake_paused():
        return True
    try:
        client = await purge._redis()
        return bool(await client.exists(purge.JOB_LOCK_KEY))
    except Exception:
        return False


async def _delete_batch(db: Any, rules: list[Any], limit: int) -> int:
    """Delete up to *limit* signals matching *rules*, oldest sighting first; the rows deleted.

    The DELETE re-applies every rule, so a signal seen again, decided again or
    joined to a case between the SELECT and the DELETE is left alone.
    """
    ids = list(
        (
            await db.execute(select(CtemSignal.id).where(*rules).order_by(CtemSignal.last_seen_at.asc()).limit(limit))
        ).scalars()
    )
    if not ids:
        return 0
    result = await db.execute(
        delete(CtemSignal).where(CtemSignal.id.in_(ids), *rules).execution_options(synchronize_session=False)
    )
    return int(getattr(result, "rowcount", 0) or 0)


async def purge_expired_signals(
    windows: dict[str, int],
    *,
    now: datetime | None = None,
    batch: int = SIGNAL_RETENTION_BATCH,
    max_batches: int = MAX_BATCHES_PER_PASS,
    sessions: Any = None,
) -> tuple[dict[str, int], bool]:
    """One bounded pass: ``({state: deleted}, more)``.

    *windows* holds the effective ``ctem_signal_*_days`` keys; a window ``<= 0``
    skips its state and opens no session. Each batch is its own transaction.
    """
    if sessions is None:
        from app.core.database import async_session as sessions

    at = now or datetime.now(UTC)
    deleted: dict[str, int] = dict.fromkeys(STATE_WINDOWS, 0)
    more = False
    for state, key in STATE_WINDOWS.items():
        days = int(windows.get(key) or 0)
        if days <= 0:
            continue
        rules = expired_rules(state, at - timedelta(days=days))
        for _ in range(max(1, max_batches)):
            async with sessions() as db:
                done = await _delete_batch(db, rules, max(1, batch))
                await db.commit()
            deleted[state] += done
            if done < batch:
                break
        else:
            more = True
    return deleted, more


async def audit_signal_retention_pass(deleted: dict[str, int], windows: dict[str, int]) -> None:
    """One platform audit row for a pass that deleted signals: the counts per state and the windows."""
    from app.core.database import async_session
    from app.services.audit import log_system_audit
    from app.services.run_retention import _audit_anchor

    total = sum(deleted.values())
    if total <= 0:
        return
    async with async_session() as db:
        anchor = await _audit_anchor(db)
        if anchor is None:
            logger.warning(
                "CTEM signal retention deleted %d signal(s) and found no administrator to audit it on", total
            )
            return
        await log_system_audit(
            db,
            actor=SIGNAL_RETENTION_AUDIT_ACTOR,
            anchor_user_id=anchor,
            action="retention_purge",
            entity_type="ctem_signal",
            summary=(
                f"Deleted {total} CTEM signal(s) past retention: "
                f"{deleted.get('discarded', 0)} discarded, {deleted.get('watchlist', 0)} watchlist, "
                f"{deleted.get('to_validate', 0)} unmatched"
            ),
            details={
                "deleted": dict(deleted),
                "retention_days": {state: windows.get(key, 0) for state, key in STATE_WINDOWS.items()},
            },
        )
        await db.commit()


async def run_pass() -> tuple[dict[str, int], bool]:
    """The pass the hourly tick runs: skipped during a purge, audited when it deleted anything."""
    from app.services.coordination_retention import effective_coordination_retention

    if await purge_running():
        logger.info("CTEM signal retention skipped: a purge of the signals is running")
        return dict.fromkeys(STATE_WINDOWS, 0), False
    windows = await effective_coordination_retention()
    deleted, more = await purge_expired_signals(windows)
    if any(deleted.values()):
        await audit_signal_retention_pass(deleted, windows)
    return deleted, more
