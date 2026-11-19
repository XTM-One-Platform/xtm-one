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

"""Knowledge Base sync scheduler – runs periodic syncs for configured sources.

Sync intervals:
  - manual: no automatic sync
  - 6h: every 6 hours
  - 12h: every 12 hours
  - daily: once a day (every 24 hours)
  - weekly: once a week (every 168 hours)

The scheduler is called from the autonomous tick (every 60s) and checks
which sync sources are due based on their interval and last_synced_at.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import math
import random
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import Select, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.egress import egress_route
from app.core.utils import format_exception_message
from app.llm.provider_access import acting_for
from app.models.knowledge_base import KBSyncSource, KnowledgeBase
from app.services.kb_helpers import (
    WEBSITE_ALL_PERMANENT_FAILURES_MARKER,
    kb_sync_log_context,
)
from app.services.kb_sync_errors import (
    CONFIG_ERROR_PREFIX,
    PARTIAL_FAILURES_PREFIX,
    PERM_FAIL_ERROR_PREFIX,
    QUARANTINE_ERROR_PREFIX,
    REPEATED_FAILURES_PREFIX,
    STALE_PAGES_PREFIX,
    SYNC_STALLED_PREFIX,
    SYNC_TIMEOUT_PREFIX,
)

logger = logging.getLogger(__name__)

# Map sync_interval to timedelta
INTERVAL_DELTAS: dict[str, timedelta] = {
    "6h": timedelta(hours=6),
    "12h": timedelta(hours=12),
    "daily": timedelta(hours=24),
    "weekly": timedelta(days=7),
}

# Hard ceiling (24h) shared by every sync-timeout clamp in this module — the
# global setting, the per-source ``config.timeout_secs`` override, and the
# stale-detection window. A single constant so the bounds can never drift.
_MAX_SYNC_TIMEOUT_SECS = 86_400


def _kb_sync_timeout_secs() -> int:
    return max(1, min(int(settings.kb_sync_timeout_secs), _MAX_SYNC_TIMEOUT_SECS))


def _source_timeout_secs(source: KBSyncSource, *, default_timeout_secs: int) -> int:
    """Return per-source sync timeout, honoring an optional config override.

    ``config.timeout_secs`` lets large/slow sources (for example a very large
    GitHub/Notion space) run longer than the global default without forcing the
    same timeout on every source. Non-integer / invalid values are ignored.
    """
    cfg = getattr(source, "config", None)
    if not isinstance(cfg, dict):
        return default_timeout_secs

    raw = cfg.get("timeout_secs")
    # Reject booleans and floats explicitly — the contract is ints and
    # numeric strings only. ``int(True)`` is 1 and ``int(1.5)`` silently
    # truncates, so ``{"timeout_secs": true}`` / ``{"timeout_secs": 1.5}``
    # would otherwise clamp the sync to a 1-second timeout instead of
    # being ignored as invalid.
    if raw is None or isinstance(raw, bool | float):
        return default_timeout_secs

    try:
        return max(1, min(int(raw), _MAX_SYNC_TIMEOUT_SECS))
    except (TypeError, ValueError):
        return default_timeout_secs


def _stale_sync_window() -> timedelta:
    """Max time a sync may go without a heartbeat before it's stuck.

    Decoupled from the per-attempt execution timeout
    (``kb_sync_timeout_secs``): a legitimately long sync keeps bumping
    ``sync_heartbeat_at`` and is never reset, while an orphaned sync (its
    worker restarted, so the heartbeat went silent) is reset within this
    window instead of waiting ``timeout + 5 min``. Clamped to
    ``[60, 86_400]`` as defence in depth (tests monkeypatch the setting
    directly, bypassing the model validator).
    """
    return timedelta(
        seconds=max(
            60, min(int(settings.kb_sync_stale_after_secs), _MAX_SYNC_TIMEOUT_SECS)
        )
    )


def _heartbeat_interval_secs() -> int:
    # Floor at 5s so a misconfigured 0/negative value can't spin-loop the
    # heartbeat task; the model validator already keeps it below the stale
    # window, but the runtime clamp guards monkeypatched test values.
    return max(5, int(settings.kb_sync_heartbeat_interval_secs))


def _sync_retry_delay_seconds(
    failure_count: int, *, max_delay_override: int | None = None
) -> float:
    """Backoff (seconds) before the next automatic KB-sync retry.

    Exponential — ``base * 2^(failure_count - 1)`` capped at
    ``max_delay`` — plus random jitter so many sources that failed
    together (e.g. a provider outage) don't all retry on the same tick.
    ``failure_count`` is the post-increment consecutive-failure count, so
    the first failure (count 1) waits the base delay. The exponent is
    clamped to 32 so it can't overflow into a giant integer.

    ``max_delay_override`` lets a stuck/stale reset apply a *lower* cap
    (``kb_sync_stale_retry_max_delay_seconds``) than a genuine error: a
    reset is usually a transient infrastructure event, so it should retry
    promptly rather than escalate to the multi-hour backoff a genuinely
    broken source eventually reaches. The override is itself
    bounded by the hard ``kb_sync_retry_max_delay_seconds`` cap so it can
    never *raise* the ceiling.
    """
    base = max(1, settings.kb_sync_retry_base_delay_seconds)
    hard_cap = max(base, settings.kb_sync_retry_max_delay_seconds)
    max_delay = (
        max(base, min(int(max_delay_override), hard_cap))
        if max_delay_override is not None
        else hard_cap
    )
    exponent = min(max(failure_count - 1, 0), 32)
    delay = min(float(base) * float(2**exponent), float(max_delay))
    jitter = max(0, settings.kb_sync_retry_jitter_seconds)
    if jitter:
        # Non-cryptographic jitter to de-correlate a batch of failures.
        delay += random.uniform(0, jitter)  # noqa: S311
    return delay


def _record_sync_failure(
    source: KBSyncSource,
    *,
    prior_failure_count: int,
    error_message: str,
    now: datetime,
    source_id: str,
    source_type: str | None,
    max_delay_override: int | None = None,
) -> bool:
    """Mark *source* failed and schedule its next backoff retry.

    Increments the consecutive-failure counter, sets the error status /
    message, clears the liveness heartbeat (the sync is no longer live),
    and schedules ``sync_next_retry_at`` with exponential backoff so the
    scheduler retries it on that schedule rather than
    waiting the full sync interval or hammering it every tick.

    ``max_delay_override`` caps the backoff at a lower ceiling for
    stuck/stale resets so a transient infrastructure event (worker
    restart) doesn't jump straight to a multi-hour delay.

    Returns ``True`` when this failure crosses the
    ``kb_sync_retry_alert_threshold`` (the below→at/above transition) so
    the caller can emit a one-shot admin alert; also escalates to an
    error-level "persistent failure" log on that same transition.
    """
    new_count = (prior_failure_count or 0) + 1
    source.last_synced_at = now
    source.last_sync_status = "error"
    source.last_sync_error = error_message[:500]
    source.last_sync_documents = 0
    source.sync_failure_count = new_count
    # The sync is no longer in flight — drop the heartbeat so a later
    # successful run starts from a clean slate and the stale sweeper never
    # judges a freshly-reset source by a now-meaningless heartbeat.
    source.sync_heartbeat_at = None
    source.sync_next_retry_at = now + timedelta(
        seconds=_sync_retry_delay_seconds(
            new_count, max_delay_override=max_delay_override
        )
    )

    threshold = settings.kb_sync_retry_alert_threshold
    # Escalate only on the below→at/above transition so a persistently
    # failing source emits a SINGLE "persistent failure" alert when it
    # first crosses the threshold, not a fresh error-level line on every
    # backoff retry thereafter (avoids alert/log noise).
    escalated = threshold > 0 and (prior_failure_count or 0) < threshold <= new_count
    if escalated:
        logger.error(
            "KB sync source %s (%s) has failed %d consecutive times "
            "(persistent failure) — next retry at %s; last error: %s",
            source_id,
            source_type or "unknown",
            new_count,
            source.sync_next_retry_at.isoformat(),
            error_message[:200],
        )
    return escalated


async def _quarantine_audience(db: AsyncSession, kb_id: uuid.UUID | str) -> tuple[str | None, list[str]]:
    """The KB's name and, when it is company-managed, every active platform
    administrator's id: who a quarantine alert reaches besides the owner.

    ``(None, [])`` on any lookup failure (best-effort, like every alert path
    of this module); no administrator for a personal KB.
    """
    try:
        from app.models.user import User

        row = (
            await db.execute(
                select(KnowledgeBase.name, KnowledgeBase.is_company_managed).where(KnowledgeBase.id == kb_id)
            )
        ).one_or_none()
        if row is None:
            return None, []
        name, managed = row
        if not managed:
            return name, []
        admins = (
            (await db.execute(select(User.id).where(User.role == "admin", User.is_active.is_(True)))).scalars().all()
        )
    except Exception:
        logger.debug("Could not resolve the quarantine audience of KB %s", kb_id, exc_info=True)
        return None, []
    return name, [str(user_id) for user_id in admins]


async def _emit_sync_notification(
    db: AsyncSession,
    *,
    kb_id,
    source_type: str | None,
    activity_type: str,
    detail: str,
    summary: str,
    source_user_id: str | None = None,
    quarantine_alert: bool = False,
) -> None:
    """Record a KB activity event and notify the KB owner + company feed.

    Shared plumbing for every scheduler alert (persistent failure,
    stale quarantine, error quarantine, config error).
    Strictly best-effort: any failure here is swallowed so alerting can
    never break a scheduler tick — the quarantine/disable itself is
    persisted by the caller and can never be rolled back by a
    notification failure.

    Notification routing:
    - Always broadcasts to ``activity:company`` (company-wide SSE feed).
    - Resolves a personal recipient — the KB owner (``KnowledgeBase.user_id``),
      falling back to *source_user_id* (the sync source's configuring user)
      for company-managed KBs whose ``user_id`` is NULL — and, when one is
      found, persists a durable ``Activity`` record for them AND publishes
      to their ``activity:{user_id}`` SSE channel. The durable record
      mirrors ``services.notify`` — the personal activity feed reads
      ``Activity`` rows, so an SSE publish alone would be invisible (and
      lost entirely if the user is offline at delivery time).
    - No recipient at all (company-managed KB, ownerless source) relies
      solely on the company-wide broadcast.
    - *quarantine_alert* (the three quarantine alerts): when the KB is
      company-managed, every active platform administrator also gets the
      durable notification and the personal SSE publish, each person once
      (an owner who is an administrator is not notified twice), and the
      personal summary names the knowledge base (never the raw error).
    """
    try:
        from app.services.kb_helpers import log_kb_activity

        # Flush the caller's pending state first (e.g. the disable that
        # triggered this alert) so it lands in the outer transaction, then
        # wrap only the activity-log insert in a SAVEPOINT. A plain flush
        # failure here would leave the session in a failed state and break
        # the caller's later commit — losing e.g. the ``enabled = False``
        # auto-disable and violating the best-effort promise above. With
        # the savepoint, an insert failure rolls back only the log entry
        # and the caller's transaction stays committable.
        await db.flush()
        async with db.begin_nested():
            await log_kb_activity(
                db,
                kb_id,
                activity_type,
                source_type=source_type,
                details=detail,
            )
    except Exception:
        logger.debug(
            "KB activity log (%s) failed for KB %s", activity_type, kb_id, exc_info=True
        )

    # Resolve the personal recipient: KB owner first, then the sync source's
    # configuring user (company-managed KBs have ``user_id`` NULL, but the
    # person who configured the source is still the natural recipient — the
    # same fallback chain the scheduler uses to *execute* the sync).
    # Best-effort: a failed lookup degrades to the fallback / company-only.
    recipient_id: str | None = None
    try:
        owner_row = await db.execute(
            select(KnowledgeBase.user_id).where(KnowledgeBase.id == kb_id)
        )
        owner_uuid = owner_row.scalar_one_or_none()
        if owner_uuid is not None:
            recipient_id = str(owner_uuid)
    except Exception:
        logger.debug("Could not resolve KB owner for %s", kb_id, exc_info=True)
    if recipient_id is None and source_user_id:
        recipient_id = str(source_user_id)

    recipients: list[str] = [recipient_id] if recipient_id else []
    # What the people notified personally read. The company-wide broadcast
    # keeps the generic ``summary``: a personal KB's name never reaches
    # every user.
    personal_summary = summary
    if quarantine_alert:
        kb_name, admin_ids = await _quarantine_audience(db, kb_id)
        for admin_id in admin_ids:
            if admin_id not in recipients:
                recipients.append(admin_id)
        # Say which knowledge base: the summary alone names only the source
        # type. The last error stays out of every notification (it is served
        # per caller on the source, see app/services/kb_sync_errors.py).
        if kb_name:
            personal_summary = f'Knowledge base "{kb_name}": {personal_summary}'

    for person_id in recipients:
        # Durable personal notification — the activity feed endpoint reads
        # ``Activity`` rows, so this is what makes the alert visible to the
        # owner (including after a reconnect). Same SAVEPOINT isolation as
        # the KB activity log above: an insert failure (e.g. the recipient
        # user was deleted) rolls back only this row.
        try:
            from app.models.activity import Activity

            await db.flush()
            async with db.begin_nested():
                db.add(
                    Activity(
                        user_id=uuid.UUID(person_id),
                        activity_type="notification",
                        agent_name="Knowledge Base Sync",
                        title=(
                            "Knowledge Base sync "
                            f"{activity_type.removeprefix('sync_').replace('_', ' ')}"
                        ),
                        summary=personal_summary,
                        extra_data={
                            "kb_id": str(kb_id),
                            "kb_event": activity_type,
                            "source_type": source_type,
                        },
                    )
                )
        except Exception:
            logger.debug(
                "Personal notification activity (%s) failed for KB %s",
                activity_type,
                kb_id,
                exc_info=True,
            )

    try:
        from app.core.redis import redis_client

        redis = await redis_client()
        message = {
            "type": "notification",
            "agent_name": "Knowledge Base Sync",
            "assignment_name": f"{source_type or 'source'} sync",
            "summary": summary,
        }
        await redis.publish("activity:company", json.dumps(message))
        personal_payload = json.dumps({**message, "summary": personal_summary})
        for person_id in recipients:
            # Personal channel — delivers the notification directly to the
            # recipient's activity feed even if they are the only affected user.
            await redis.publish(f"activity:{person_id}", personal_payload)
    except Exception:
        logger.debug(
            "SSE broadcast (%s) failed for KB %s", activity_type, kb_id, exc_info=True
        )


async def _emit_persistent_failure_alert(
    db: AsyncSession,
    *,
    kb_id,
    source_type: str | None,
    failure_count: int,
    error_message: str,
    source_user_id: str | None = None,
) -> None:
    """Surface a persistent KB-sync failure to admins.

    Records a durable ``sync_alert`` KB activity event and broadcasts a
    company-wide SSE notification so the failure is visible in the UI
    rather than only in the logs.
    """
    await _emit_sync_notification(
        db,
        kb_id=kb_id,
        source_type=source_type,
        activity_type="sync_alert",
        detail=(
            f"{REPEATED_FAILURES_PREFIX}{failure_count} consecutive times "
            f"({source_type or 'unknown'}). Last error: {error_message[:200]}"
        ),
        summary=(
            f"A {source_type or 'knowledge-base'} sync has failed "
            f"{failure_count} times in a row and needs attention."
        ),
        source_user_id=source_user_id,
    )


async def _handle_sync_failure(
    db: AsyncSession,
    source: KBSyncSource,
    *,
    kb_id,
    prior_failure_count: int,
    prior_perm_fail_count: int,
    error_message: str,
    activity_detail: str,
    source_id: str,
    source_type: str | None,
    source_user_id: str | None = None,
) -> bool:
    """Record a KB-sync failure, surface it in the KB activity feed, and emit
    a persistent-failure alert when the consecutive-failure threshold is
    crossed.

    Returns ``True`` when the failure state (and any pending ORM mutations
    from the caller, e.g. the stale-page tracking config) was flushed
    successfully, ``False`` when the internal persistence attempt failed and
    was rolled back — callers must not emit alerts about rolled-back state.

    Single source of truth for *both* failure paths — the per-attempt
    exception path in :func:`execute_sync_source` and the scheduler-level
    timeout path in :func:`process_scheduled_kb_syncs`. Those paths kept
    separate copies and silently drifted: the timeout path recorded the
    error and scheduled the backoff retry but never wrote the user-visible
    ``sync_failed`` activity event the exception path did, so a 30-minute
    timeout vanished from the KB activity feed. Routing both through
    this helper guarantees a timeout is exactly as visible — and retried with
    the same exponential backoff — as any other failure, and that the two
    paths can never diverge again.

    When the consecutive-failure count reaches
    ``kb_sync_failure_quarantine_threshold`` the source is quarantined
    (``enabled = False``) so a permanently misconfigured source (e.g. a
    renamed SharePoint folder returning 404 on every attempt) stops
    consuming scheduler resources instead of retrying indefinitely.
    Both routed paths count toward that threshold — clean exceptions and
    scheduler timeouts share ``sync_failure_count``. Recovery is via Force
    Re-sync or by re-enabling the source in settings.

    Permanent errors (HTTP 404/410) additionally increment the dedicated
    ``sync_perm_fail_count`` budget; a non-permanent failure resets it so
    only genuinely *consecutive* 404/410s accumulate. When that counter
    reaches ``kb_sync_perm_fail_quarantine_threshold`` (stricter than the
    general threshold above — a 404 can never succeed without
    reconfiguration) the source is disabled and flagged as "needs
    reconfiguration".

    ``prior_perm_fail_count`` and ``source_user_id`` must be captured
    *before* the early commit (and any subsequent rollback) in the caller —
    a ``session.rollback()`` call expires all ORM attributes, clearing the
    values from ``source.__dict__``.  The parameter is deliberately
    *required* (no default) so a future call site cannot silently fall back
    to 0 and reintroduce the counter-reset bug.  Callers that
    re-fetch the source on a clean session (e.g. the timeout path) can read
    the attributes directly off the fresh instance.

    ``error_message`` is stored as ``last_sync_error`` (via
    :func:`_record_sync_failure`); ``activity_detail`` is the human-readable
    line shown in the activity feed. Best-effort: a persistence failure on a
    degraded session is logged (and rolled back), never raised — the caller
    commits right after this helper, so the session must be left usable.
    """
    from app.services.kb_helpers import log_kb_activity

    # Pre-compute new failure count so we can use it after a potential
    # rollback (a rollback expires ORM attributes; reading source.sync_failure_count
    # post-rollback would trigger a synchronous lazy-load on an async session).
    # ``or 0`` mirrors the guard in _record_sync_failure so both computations
    # can never diverge for a legacy NULL counter.
    new_count = (prior_failure_count or 0) + 1

    # ``prior_perm_fail_count`` is passed in by the caller (captured before
    # the early commit / subsequent rollback) — do NOT read it from
    # source.__dict__ here.  A session.rollback() call expires all ORM
    # attributes, removing the value from __dict__; reading __dict__ after
    # that always returns the default 0, silently resetting the counter and
    # preventing the permanent-failure quarantine from ever triggering.
    new_perm_fail_count = prior_perm_fail_count
    perm_quarantine_message = ""

    # ``source_user_id`` (the sync source's configuring user — the
    # personal-notification fallback for company-managed KBs)
    # is likewise passed by the caller: a ``__dict__`` read here would be
    # empty after the caller's rollback, silently dropping the personal
    # recipient on the exception path.

    escalated = False
    quarantined = False
    perm_quarantined = False
    persisted = True
    try:
        escalated = _record_sync_failure(
            source,
            prior_failure_count=prior_failure_count,
            error_message=error_message,
            now=datetime.now(UTC),
            source_id=source_id,
            source_type=source_type,
        )

        # A full-failure run interrupts the "consecutive partial-failure"
        # streak — reset the counter so only uninterrupted partial-success
        # runs accumulate toward the partial-failure alert threshold,
        # mirroring how a non-permanent failure resets the
        # permanent-error budget below.
        source.sync_partial_fail_count = 0

        # Error-based quarantine: disable sources that keep failing with clean
        # errors so they stop consuming scheduler resources indefinitely.
        # Distinct from the stale-reset quarantine which targets
        # sources stuck in 'syncing' without a heartbeat.
        quarantine_threshold = settings.kb_sync_failure_quarantine_threshold
        if quarantine_threshold > 0 and new_count >= quarantine_threshold:
            # Keep the underlying error visible: this message replaces
            # ``last_sync_error`` (the field the UI surfaces), and the
            # operator needs the root cause (e.g. the 404) to fix the
            # configuration — not just the fact of the quarantine.
            quarantine_message = (
                f"{QUARANTINE_ERROR_PREFIX} {new_count} consecutive failures "
                f"(type={source_type or 'unknown'}). The source configuration "
                "may be permanently invalid (e.g. a renamed folder or deleted "
                "path). Update the configuration and use Force Re-sync to retry, "
                f"or re-enable the source in settings. Last error: {error_message[:200]}"
            )
            source.enabled = False
            source.last_sync_error = quarantine_message[:500]
            quarantined = True
            logger.error(
                "Quarantining KB sync source %s (type=%s) after %d consecutive "
                "errors — disabling to stop the doomed-retry cycle",
                source_id,
                source_type or "unknown",
                new_count,
            )

        # Permanent-error quarantine: 404/410 responses mean
        # the configured path or resource no longer exists — a stricter,
        # dedicated budget than the general error-count quarantine above,
        # because such a source can never succeed without reconfiguration.
        # A non-permanent failure (timeout, 5xx, rate limit) RESETS the
        # counter so only genuinely consecutive 404/410s accumulate — a
        # provider outage interleaved with 404s cannot creep it toward the
        # threshold. Checked after the general quarantine so the more
        # actionable "needs reconfiguration" message wins ``last_sync_error``
        # when both thresholds cross on the same failure.
        if _is_permanent_sync_error(error_message, source_type=source_type):
            new_perm_fail_count = prior_perm_fail_count + 1
            source.sync_perm_fail_count = new_perm_fail_count
            perm_threshold = settings.kb_sync_perm_fail_quarantine_threshold
            if perm_threshold > 0 and new_perm_fail_count >= perm_threshold:
                perm_quarantined = True
                # Keep the root cause visible: this message replaces
                # ``last_sync_error`` (the field the UI surfaces) and the
                # operator needs the underlying 404 to fix the configuration.
                perm_quarantine_message = (
                    f"{PERM_FAIL_ERROR_PREFIX} {new_perm_fail_count} consecutive "
                    f"'not found' errors (type={source_type or 'unknown'}). "
                    "The configured path, folder, or resource no longer exists — "
                    "update the source configuration and use Force Re-sync to "
                    f"retry. Last error: {error_message[:200]}"
                )
                source.enabled = False
                source.last_sync_error = perm_quarantine_message[:500]
                logger.error(
                    "KB sync source %s (type=%s) quarantined after %d consecutive "
                    "permanent errors — disabling to stop retrying a broken "
                    "configuration; last error: %s",
                    source_id,
                    source_type or "unknown",
                    new_perm_fail_count,
                    error_message[:200],
                )
        else:
            new_perm_fail_count = 0
            source.sync_perm_fail_count = 0

        await log_kb_activity(
            db,
            kb_id,
            "sync_failed",
            source_type=source_type,
            details=activity_detail,
        )
        await db.flush()
    except Exception:
        logger.warning(
            "Failed to persist sync failure status for source %s — "
            "session unusable",
            source_id,
            exc_info=True,
        )
        # Best-effort rollback so an aborted transaction here doesn't turn a
        # recoverable persistence failure into a commit failure for the whole
        # scheduler tick — the caller commits immediately after this helper.
        with contextlib.suppress(Exception):
            await db.rollback()
        # The rollback reverted every ORM change above (failure counter,
        # enabled flag, quarantine message) — suppress the alerts so we never
        # notify about a state that was not actually committed. The next tick
        # retries the failure handling and re-emits on a successful pass.
        escalated = False
        quarantined = False
        perm_quarantined = False
        persisted = False

    if escalated:
        await _emit_persistent_failure_alert(
            db,
            kb_id=kb_id,
            source_type=source_type,
            # Use the pre-computed new_count rather than reading
            # ``source.sync_failure_count``: a rollback above expires ORM
            # attributes, and re-reading one would trigger a sync lazy-load
            # (MissingGreenlet) on the async session.
            failure_count=new_count,
            error_message=error_message,
            source_user_id=source_user_id,
        )
    if quarantined:
        await _emit_failure_quarantine_alert(
            db,
            kb_id=kb_id,
            source_type=source_type,
            failure_count=new_count,
            error_message=error_message,
            source_user_id=source_user_id,
        )
    if perm_quarantined:
        await _emit_perm_fail_quarantine_alert(
            db,
            kb_id=kb_id,
            source_type=source_type,
            perm_fail_count=new_perm_fail_count,
            quarantine_message=perm_quarantine_message,
            source_user_id=source_user_id,
        )

    return persisted


async def _run_sync_heartbeat(source_id: str) -> None:
    """Periodically bump ``sync_heartbeat_at`` while a sync is running.

    Runs as a sibling ``asyncio`` task on the same event loop as the sync
    itself, so it only advances while the loop is actually servicing the
    sync's await points. If the sync hangs (event loop blocked) or the
    worker dies, the heartbeat stops too — exactly the signal the stale
    sweeper needs to tell a live sync from a stuck/orphaned one. Uses a
    short-lived isolated session per beat so it never shares (or corrupts)
    the sync's session.
    """
    from app.core.database import async_session as _async_session

    interval = _heartbeat_interval_secs()
    src_uuid = uuid.UUID(str(source_id))
    while True:
        await asyncio.sleep(interval)
        try:
            async with _async_session() as hb_db:
                await hb_db.execute(
                    update(KBSyncSource)
                    .where(KBSyncSource.id == src_uuid)
                    .values(sync_heartbeat_at=datetime.now(UTC))
                )
                await hb_db.commit()
        except asyncio.CancelledError:
            raise
        except Exception:
            # A dropped beat is harmless — the next one (or the stale
            # sweeper) recovers. Never let it bubble into the sync.
            logger.debug(
                "KB sync heartbeat update failed for %s", source_id, exc_info=True
            )


async def execute_sync_source(
    db: AsyncSession,
    source: KBSyncSource,
    kb: KnowledgeBase,
    user_id: str,
    *,
    timeout_secs: int | None = None,
) -> list[str]:
    """Execute a single sync source and update its tracking fields.

    The heavy sync work runs in an **isolated** DB session to avoid
    corrupting the caller's session when per-file rollbacks or
    statement timeouts occur (SQLAlchemy MissingGreenlet).  The
    caller's *db* session is used only for lightweight status updates.

    All log records emitted during the sync — including the deep
    provider-level warnings in ``kb_sync.py`` — carry ``kb_id`` /
    ``kb_name`` / ``sync_source_id`` / ``source_type`` structured fields
    via :func:`kb_sync_log_context` so they are actionable without
    grepping the database.

    Returns list of created document IDs.
    """
    with kb_sync_log_context(
        kb_id=kb.id,
        kb_name=kb.name,
        sync_source_id=source.id,
        source_type=source.source_type,
    ):
        return await _execute_sync_source_inner(
            db, source, kb, user_id, timeout_secs=timeout_secs
        )


async def _report_unseen_bound_integration(
    db: AsyncSession, source_id: str, integration_id: str, acting_user_id: uuid.UUID | None
) -> None:
    """Log when the user a source syncs as cannot see its bound integration.

    The sync reads through the integration either way: nothing is withheld at
    run time. The check runs in a savepoint of the caller's session, so
    a failed read costs the warning, never the sync.
    """
    from app.services.trigger_integration_access import (
        TriggerIntegrationVisibility,
        log_unseen_sync_source_integration,
    )

    if acting_user_id is None:
        return
    try:
        async with db.begin_nested():
            hidden = await TriggerIntegrationVisibility().hides(db, acting_user_id, integration_id)
    except Exception:
        logger.debug(
            "Could not check whether sync source %s's user can see integration %s",
            source_id,
            integration_id,
            exc_info=True,
        )
        return
    if hidden:
        log_unseen_sync_source_integration(
            source_id, integration_id=integration_id, acting_user_id=acting_user_id
        )


async def _execute_sync_source_inner(
    db: AsyncSession,
    source: KBSyncSource,
    kb: KnowledgeBase,
    user_id: str,
    *,
    timeout_secs: int | None = None,
) -> list[str]:
    source_id = str(source.id)
    source_type = source.source_type
    kb_id = kb.id
    kb_name = kb.name

    # Captured BEFORE the early commit below so the error path doesn't have
    # to lazy-load an expired attribute through the (possibly degraded)
    # session — used to compute the exponential-backoff retry schedule.
    prior_failure_count = source.sync_failure_count or 0
    # Capture the permanent-failure counter here too, for the same reason:
    # the early commit is followed by a rollback on the error path which
    # calls session.expire_all(), clearing attribute values from __dict__.
    # Reading source.__dict__["sync_perm_fail_count"] AFTER that rollback
    # always returns 0 (the expired default), silently resetting the
    # counter on every failure and preventing quarantine from ever
    # triggering.  Capturing here, before the commit+rollback
    # cycle, preserves the real DB value across both paths.
    prior_perm_fail_count = getattr(source, "sync_perm_fail_count", 0) or 0
    # Same early capture for the source's configuring user — the personal
    # notification fallback for company-managed KBs.
    _src_user = getattr(source, "user_id", None)
    source_user_id = str(_src_user) if _src_user else None

    config = source.config or {}
    doc_ids: list[str] = []
    # When the caller doesn't supply a timeout (manual "Sync now" /
    # "Force re-sync" background path), honor the same per-source
    # ``config.timeout_secs`` override the scheduler applies — otherwise a
    # source tuned for a longer budget would still hit the global
    # statement timeout on manually triggered syncs.
    effective_timeout_secs = (
        timeout_secs
        if timeout_secs is not None
        else _source_timeout_secs(source, default_timeout_secs=_kb_sync_timeout_secs())
    )
    bound_integration_id = (
        str(source.integration_id) if getattr(source, "integration_id", None) else None
    )

    from app.services.kb_helpers import log_kb_activity
    from app.services.kb_sync import KBSyncConfigError
    from app.services.trigger_integration_access import sync_source_acting_user_id

    if bound_integration_id:
        # The source's own identity, whoever started this sync: a scheduled
        # sync and a "Sync now" say the same.
        await _report_unseen_bound_integration(
            db,
            source_id,
            bound_integration_id,
            sync_source_acting_user_id(source_user_id, getattr(kb, "user_id", None)),
        )

    await log_kb_activity(
        db,
        kb_id,
        "sync_started",
        source_type=source_type,
        details=f"Sync started for {source_type}",
    )
    await db.flush()

    # Mark as syncing and commit immediately — this releases the row lock
    # and makes the status visible to other transactions so concurrent
    # scheduler ticks skip this source (the `last_status == "syncing"` guard).
    # Without an early commit the flush-only row lock is held for the entire
    # sync duration and the next 60s tick blocks until statement_timeout fires.
    # Stamp an initial heartbeat so the stale sweeper has a fresh liveness
    # marker from the moment the sync starts.
    source.last_sync_status = "syncing"
    source.last_synced_at = datetime.now(UTC)
    source.sync_heartbeat_at = datetime.now(UTC)
    await db.commit()

    # Keep ``sync_heartbeat_at`` advancing for the duration of the sync so
    # the stale sweeper can distinguish a live long-running sync from an
    # orphaned/hung one. Cancelled in the ``finally`` below.
    heartbeat_task = asyncio.create_task(_run_sync_heartbeat(source_id))

    # Mutable collector for Notion page IDs that return HTTP 404/410 during
    # the sync.  Only populated for notion sources; empty for all others.
    # Used after a successful (or total-failure) sync to track per-page
    # failure counts and quarantine stale references.
    # The list is populated inside sync_notion_pages BEFORE any exception is
    # raised, so it is safe to consume in the failure path as well.
    _notion_not_found: list[str] = []

    try:
        try:
            # The documents it brings in are embedded for the source's user:
            # an embedding provider that excludes them never receives them
            # (the enqueue reads the binding).
            with acting_for(source_user_id or user_id):
                doc_ids, page_error_count = await _run_sync_in_isolated_session(
                    source_type=source_type,
                    config=config,
                    kb_id=kb_id,
                    user_id=user_id,
                    bound_integration_id=bound_integration_id,
                    statement_timeout_secs=effective_timeout_secs,
                    source_id=source_id,
                    _notion_not_found_out=_notion_not_found if source_type == "notion" else None,
                )

            source.last_synced_at = datetime.now(UTC)
            source.last_sync_status = "success"
            source.last_sync_error = None
            source.last_sync_documents = len(doc_ids)
            # Clear the auto-retry backoff state + heartbeat on success.
            source.sync_failure_count = 0
            source.sync_next_retry_at = None
            source.sync_heartbeat_at = None
            # A successful sync proves the source is no longer stuck —
            # reset the stale-reset counter so a previously recovered source
            # doesn't carry stale-count baggage into future retries.
            source.sync_stale_count = 0
            # A successful sync also clears any accumulated permanent-error
            # count so the quarantine budget resets for future failures.
            source.sync_perm_fail_count = 0

            # For Notion sources, update per-page 404/410 counters and
            # quarantine stale page references that have failed on every
            # consecutive sync run.  Also runs when the sync
            # had NO 404s but earlier runs left counters behind — a clean
            # run must reset them so the "consecutive" contract holds and a
            # much-later 404 starts counting from 1 again.  Must run BEFORE
            # the flush so the config mutation is persisted together with
            # the success status.  ``config`` is the dict captured before
            # the early commit — passed explicitly to avoid a lazy-load on
            # the expired ORM instance.
            newly_quarantined_pages: list[str] = []
            if source_type == "notion" and (
                _notion_not_found
                or config.get(_NOTION_CFG_NOT_FOUND_COUNTS)
                # A non-empty skip list must also enter the handler even on
                # a clean run: the periodic-recheck counter has to advance
                # and recovered pages have to be unquarantined.
                or config.get(_NOTION_CFG_SKIPPED_PAGE_IDS)
            ):
                # The isolated sync session may have committed config
                # updates of its own during the run (the stale
                # child-database tracking) —
                # refresh the row first so the reassignment below starts
                # from the current DB state instead of clobbering those
                # keys with the pre-sync snapshot. Best-effort: on failure
                # fall back to the already-loaded value via __dict__ (no
                # I/O — a plain attribute read could lazy-load an expired
                # instance and raise MissingGreenlet), then the captured
                # snapshot (never worse than before). ``is None`` (not
                # truthiness) so a legitimately refreshed empty config is
                # respected instead of resurrecting the stale snapshot.
                with contextlib.suppress(Exception):
                    await db.refresh(source, attribute_names=["config"])
                refreshed_config = source.__dict__.get("config")
                newly_quarantined_pages = _handle_notion_stale_pages(
                    source,
                    _notion_not_found,
                    base_config=config if refreshed_config is None else refreshed_config,
                    kb_id=kb_id,
                    source_id=source_id,
                    # Same deterministic predicate the isolated session used
                    # to decide whether to bypass the skip list this run.
                    rechecked=_is_notion_page_recheck_run(config),
                )
            # Track partial page failures (some pages failed, some succeeded).
            # Increment the consecutive partial-failure counter and emit a
            # one-shot alert when the threshold is first crossed so operators
            # can identify stale page references.
            # Plain attribute access is safe here (no rollback has occurred
            # on this path and sessions use expire_on_commit=False); getattr
            # keeps legacy test fixtures without the field working.
            prior_partial_fail_count = (
                getattr(source, "sync_partial_fail_count", 0) or 0
            )
            new_partial_fail_count = prior_partial_fail_count
            partial_alert_emitted = False
            if page_error_count > 0:
                new_partial_fail_count = prior_partial_fail_count + 1
                source.sync_partial_fail_count = new_partial_fail_count
                partial_threshold = settings.kb_sync_partial_fail_alert_threshold
                # Fire the alert only on the below→at/above crossing to keep
                # it a one-shot signal rather than repeating every sync tick.
                if (
                    partial_threshold > 0
                    and prior_partial_fail_count < partial_threshold
                    <= new_partial_fail_count
                ):
                    partial_alert_emitted = True
                    # Warning (not error): the sync as a whole succeeded and
                    # the emitted notification is informational — error-level
                    # logging here would create false-positive ops alerts.
                    logger.warning(
                        "KB sync source %s (%s) for KB '%s' has had "
                        "page-level failures in %d consecutive sync run(s) — "
                        "stale or inaccessible page references may need "
                        "cleanup",
                        source_id,
                        source_type,
                        kb_name,
                        new_partial_fail_count,
                    )
            else:
                # Clean sync — reset the partial-failure counter so a later
                # partial failure starts its own fresh accumulation window.
                source.sync_partial_fail_count = 0

            await log_kb_activity(
                db,
                kb_id,
                "sync_completed",
                source_type=source_type,
                document_count=len(doc_ids),
                details=f"Synced {len(doc_ids)} document(s) from {source_type}",
            )
            await db.flush()

            # Surface the page-level quarantine to the KB owner (the issue
            # explicitly asks for a visible "stale source reference" status,
            # not just a log line). After the flush, like every other
            # ``_emit_*_alert``, so a best-effort notification can never
            # roll back the config mutation above.
            if newly_quarantined_pages:
                await _emit_stale_page_quarantine_alert(
                    db,
                    kb_id=kb_id,
                    source_type=source_type,
                    page_ids=newly_quarantined_pages,
                    source_user_id=source_user_id,
                )

            logger.info(
                "Sync source %s (%s) for KB '%s': %d total document(s)",
                source_id,
                source_type,
                kb_name,
                len(doc_ids),
            )

            if partial_alert_emitted:
                await _emit_partial_fail_alert(
                    db,
                    kb_id=kb_id,
                    source_type=source_type,
                    partial_fail_count=new_partial_fail_count,
                    source_user_id=source_user_id,
                )

        except KBSyncConfigError as cfg_err:
            # Permanent configuration error — retrying will never succeed
            # without the user fixing the source settings. Disable the
            # source immediately to stop the daily retry cycle and notify
            # the owner with a clear, actionable message. Do NOT re-raise:
            # the caller commits right after, persisting the disabled state.
            with contextlib.suppress(Exception):
                await db.rollback()

            cfg_message = f"{CONFIG_ERROR_PREFIX} {cfg_err}"
            source.enabled = False
            source.last_synced_at = datetime.now(UTC)
            source.last_sync_status = "error"
            source.last_sync_error = cfg_message[:500]
            source.last_sync_documents = 0
            source.sync_heartbeat_at = None
            # A disabled source has no scheduled retry — clear the backoff
            # state so a later re-enable (after the user fixes the config)
            # starts from a clean slate instead of inheriting a stale
            # schedule / consecutive-failure count from the broken config.
            source.sync_failure_count = 0
            source.sync_next_retry_at = None
            # Same clean-slate rule for the consecutive-404 budget the
            # permanent-failure quarantine tracks.
            source.sync_perm_fail_count = 0
            # And for the partial-failure counter.
            source.sync_partial_fail_count = 0

            logger.error(
                "KB sync source %s (%s) for KB '%s' (%s) has a permanent "
                "configuration error — disabling to prevent futile retries. "
                "Error: %s",
                source_id,
                source_type,
                kb_name,
                kb_id,
                format_exception_message(cfg_err),
            )

            # The alert helper records the single ``sync_config_error``
            # activity event (best-effort, never raises) — don't log a
            # second copy here, and don't risk a nested rollback that would
            # discard the ``enabled = False`` mutation before the caller's
            # commit.
            await _emit_config_error_alert(
                db,
                kb_id=kb_id,
                source_type=source_type,
                error_message=str(cfg_err),
                source_user_id=source_user_id,
            )

        except Exception as e:
            with contextlib.suppress(Exception):
                await db.rollback()

            # ``str(e)`` is empty for some exception types (asyncio
            # TimeoutError, bare httpx errors) — use the shared formatter so
            # ``last_sync_error`` and the log are never blank.
            error_text = format_exception_message(e)

            # For Notion total failures, still run the per-page 404 stale
            # tracking so stale references are quarantined after
            # _NOTION_PAGE_STALE_THRESHOLD consecutive failures instead of
            # re-trying indefinitely each cycle.
            # _notion_not_found is populated inside sync_notion_pages BEFORE
            # the exception is raised, so it is safe to consume here.
            # The config mutation is persisted together with the failure
            # status in the db.flush() call inside _handle_sync_failure.
            # Best-effort as a whole: the tracking is auxiliary and must
            # never prevent _handle_sync_failure from persisting the failure
            # status (or mask the original sync exception) if it raises on
            # unexpected state.
            newly_quarantined_on_fail: list[str] = []
            if source_type == "notion" and _notion_not_found:
                try:
                    with contextlib.suppress(Exception):
                        # Best-effort refresh: the isolated sync session may
                        # have committed stale_child_dbs updates before
                        # raising — pick those up so we don't clobber them
                        # when we write config.
                        await db.refresh(source, attribute_names=["config"])
                    # ``is None`` (not truthiness) so a legitimately
                    # refreshed empty config is respected instead of
                    # resurrecting the stale pre-sync snapshot.
                    refreshed_fail_config = source.__dict__.get("config")
                    newly_quarantined_on_fail = _handle_notion_stale_pages(
                        source,
                        _notion_not_found,
                        base_config=(
                            config
                            if refreshed_fail_config is None
                            else refreshed_fail_config
                        ),
                        kb_id=kb_id,
                        source_id=source_id,
                    )
                except Exception:
                    logger.warning(
                        "Failed to update Notion stale-page tracking for "
                        "source %s on failure path — continuing with "
                        "failure-status persistence",
                        source_id,
                        exc_info=True,
                    )

            failure_persisted = await _handle_sync_failure(
                db,
                source,
                kb_id=kb_id,
                prior_failure_count=prior_failure_count,
                prior_perm_fail_count=prior_perm_fail_count,
                error_message=error_text,
                activity_detail=f"Sync failed: {error_text[:200]}",
                source_id=source_id,
                source_type=source_type,
                source_user_id=source_user_id,
            )

            # Only alert when the flush inside _handle_sync_failure actually
            # persisted the tracking config mutated above — its internal
            # rollback path reverts the quarantine state, and alerting about
            # a rolled-back quarantine would be a false notification. The
            # next tick re-runs the tracking and re-emits on a clean pass.
            if newly_quarantined_on_fail and failure_persisted:
                await _emit_stale_page_quarantine_alert(
                    db,
                    kb_id=kb_id,
                    source_type=source_type,
                    page_ids=newly_quarantined_on_fail,
                    source_user_id=source_user_id,
                )

            logger.warning(
                "Sync source %s (%s) for KB '%s' (%s) failed: %s",
                source_id,
                source_type,
                kb_name,
                kb_id,
                error_text,
            )
            raise
    finally:
        heartbeat_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await heartbeat_task

    return doc_ids


# The integration provider each sync source type reads through (a public
# GitHub repository and a website read through none).
_SYNC_SOURCE_PROVIDERS = {
    "github": "github",
    "notion": "notion",
    "microsoft-drive": "microsoft",
    "google-drive": "google",
    "drata": "drata",
}


async def _run_sync_in_isolated_session(
    *,
    source_type: str,
    config: dict,
    kb_id,
    user_id: str,
    bound_integration_id: str | None,
    statement_timeout_secs: int,
    source_id: str | None = None,
    _notion_not_found_out: list[str] | None = None,
) -> tuple[list[str], int]:
    """Run the actual sync work inside a fresh, isolated DB session.

    This prevents per-file rollbacks or statement timeouts from
    corrupting the caller's session (the MissingGreenlet / xd2s error).

    ``_notion_not_found_out``, when provided (Notion sources only), is
    populated with every page ID that returned HTTP 404/410 during the sync
    so the caller can track consecutive failures and quarantine stale
    references.

    Returns a ``(doc_ids, page_error_count)`` tuple. ``page_error_count``
    is the number of page-level fetch errors that occurred during the sync
    even though the run as a whole succeeded (partial failure).  Only
    populated for Notion syncs; always 0 for other source types.
    """
    import uuid as _uuid

    from app.core.database import async_session as _async_session
    from app.models.knowledge_base import KnowledgeBase
    from app.services.kb_sync import (
        _apply_sync_statement_timeout,
        _load_stale_child_dbs,
        _persist_stale_child_dbs,
        sync_drata_policies,
        sync_egress_route,
        sync_github_repo,
        sync_google_drive,
        sync_microsoft_drive,
        sync_notion_pages,
        sync_website,
    )

    async with _async_session() as sync_db:
        # Raise statement_timeout for sync sessions — large-content INSERTs and
        # embedding / stale-cleanup DELETEs on large documents can exceed the
        # default 30s global pool timeout.
        await _apply_sync_statement_timeout(sync_db, statement_timeout_secs)

        kb_result = await sync_db.execute(
            select(KnowledgeBase).where(
                KnowledgeBase.id
                == (kb_id if isinstance(kb_id, _uuid.UUID) else _uuid.UUID(str(kb_id)))
            )
        )
        sync_kb = kb_result.scalar_one_or_none()
        if not sync_kb:
            raise ValueError(f"Knowledge base {kb_id} not found")

        page_error_count = 0

        # The sync reads through its integration's relay group when that
        # integration is routed through one.
        sync_provider = _SYNC_SOURCE_PROVIDERS.get(source_type)
        route = (
            await sync_egress_route(
                sync_db, user_id, sync_provider, integration_id=bound_integration_id
            )
            if sync_provider
            else None
        )
        with egress_route(route):
            if source_type == "github":
                doc_ids = await sync_github_repo(
                    sync_db,
                    sync_kb,
                    user_id,
                    repo=config.get("repo", ""),
                    branch=config.get("branch", "main"),
                    path=config.get("path", ""),
                    integration_id=bound_integration_id,
                    source_id=source_id,
                    statement_timeout_secs=statement_timeout_secs,
                )
            elif source_type == "github_public":
                doc_ids = await sync_github_repo(
                    sync_db,
                    sync_kb,
                    user_id,
                    repo=config.get("repo", ""),
                    branch=config.get("branch", "main"),
                    path=config.get("path", ""),
                    public=True,
                    source_id=source_id,
                    statement_timeout_secs=statement_timeout_secs,
                )
            elif source_type == "notion":
                recursive_raw = config.get("recursive", "")
                recursive = str(recursive_raw).lower() in ("true", "1", "yes")
                # Load the quarantined-page skip list so the sync doesn't retry
                # pages that have already failed with 404/410 ``_NOTION_PAGE_STALE_THRESHOLD``
                # consecutive times.
                # Guard against corrupted config values, mirroring the
                # stale_child_databases guard below — a bad row must not crash
                # (or silently mis-skip) every subsequent sync cycle: tolerate a
                # non-list value AND non-string (potentially unhashable) items.
                _skip_raw = config.get(_NOTION_CFG_SKIPPED_PAGE_IDS)
                skip_raw = (
                    [p for p in _skip_raw if isinstance(p, str)]
                    if isinstance(_skip_raw, list)
                    else []
                )
                skip_page_ids = frozenset(skip_raw) if skip_raw else None
                # Periodic recovery probe: every Nth run with a non-empty
                # skip list, bypass the quarantine so previously-stale pages are
                # probed again. _handle_notion_stale_pages removes recovered
                # pages from the skip list afterwards; still-404 pages stay
                # quarantined.
                if skip_page_ids and _is_notion_page_recheck_run(config):
                    logger.info(
                        "Notion KB sync: periodic recheck run for source %s — "
                        "probing %d quarantined page(s) to detect recovery",
                        source_id,
                        len(skip_page_ids),
                    )
                    skip_page_ids = None
                notion_page_errors: list[tuple[str, str]] = []
                # Per-child-DB 404 tracking only applies to recursive syncs
                # (child databases are only discovered in recursive mode) — pass
                # None otherwise so non-recursive syncs never mutate or prune the
                # persisted state.  _load_stale_child_dbs sanitizes corrupt config
                # values and migrates the legacy skipped_child_db_ids list;
                # sync_notion_pages persists the updated dict back to the source
                # config itself.
                stale_child_dbs = _load_stale_child_dbs(config) if recursive else None
                stale_child_dbs_before = (
                    {k: dict(v) for k, v in stale_child_dbs.items()}
                    if isinstance(stale_child_dbs, dict)
                    else None
                )
                # Load the NON-404 failure counters (persisted across sync cycles
                # to quarantine repeatedly-failing child databases).  Guard
                # against a corrupted (non-dict) config value so a bad row can't
                # crash every subsequent sync cycle.
                _raw_stale = config.get("stale_child_databases")
                _orig_stale = dict(_raw_stale) if isinstance(_raw_stale, dict) else {}
                notion_stale_child_dbs: dict[str, int] = dict(_orig_stale)
                doc_ids = await sync_notion_pages(
                    sync_db,
                    sync_kb,
                    user_id,
                    page_ids=config.get("page_ids"),
                    search_query=config.get("search_query"),
                    page_id=config.get("page_id"),
                    database_id=config.get("database_id"),
                    recursive=recursive,
                    integration_id=bound_integration_id,
                    sync_source_id=source_id,
                    stale_child_dbs=stale_child_dbs,
                    stale_child_databases=notion_stale_child_dbs,
                    skip_page_ids=skip_page_ids,
                    _not_found_pages_out=_notion_not_found_out,
                    _error_collector=notion_page_errors,
                )
                page_error_count = len(notion_page_errors)
                # Persist the updated non-404 failure counters back to the source
                # config when they changed (incremented or reset).  The counted
                # 404 tracking dict (stale_child_dbs) is normally persisted AND
                # committed inside sync_notion_pages itself; the backstop below
                # re-runs the idempotent _persist_stale_child_dbs for the case
                # where that in-sync persist silently failed (it is best-effort
                # and swallows its own errors) — a no-op row-compare when
                # the state is already persisted, a real UPDATE when it was lost.
                # Without it the session would close without the 404 quarantine
                # state and the same stale child database would be retried (and
                # re-warned about) on every scheduled sync cycle.
                needs_source_commit = False
                if source_id and stale_child_dbs_before != stale_child_dbs:
                    await _persist_stale_child_dbs(sync_db, source_id, stale_child_dbs)
                    needs_source_commit = True
                if notion_stale_child_dbs != _orig_stale and source_id:
                    from app.models.knowledge_base import KBSyncSource as _KBSyncSource

                    src_row = await sync_db.get(_KBSyncSource, _uuid.UUID(source_id))
                    if src_row is not None:
                        new_cfg = dict(src_row.config or {})
                        if notion_stale_child_dbs:
                            new_cfg["stale_child_databases"] = notion_stale_child_dbs
                        else:
                            # All counts reset — drop the key to keep config clean.
                            new_cfg.pop("stale_child_databases", None)
                        src_row.config = new_cfg
                        needs_source_commit = True
                if needs_source_commit:
                    await sync_db.commit()
            elif source_type == "microsoft-drive":
                recursive_raw = config.get("recursive", "")
                recursive = str(recursive_raw).lower() in ("true", "1", "yes")
                doc_ids = await sync_microsoft_drive(
                    sync_db,
                    sync_kb,
                    user_id,
                    folder_path=config.get("folder_path", "/"),
                    site_id=config.get("site_id"),
                    integration_id=bound_integration_id,
                    recursive=recursive,
                    sync_source_id=source_id,
                    folder_paths=config.get("folder_paths"),
                    exclude=config.get("exclude"),
                )
            elif source_type == "google-drive":
                doc_ids = await sync_google_drive(
                    sync_db,
                    sync_kb,
                    user_id,
                    folder_id=config.get("folder_id", "root"),
                    integration_id=bound_integration_id,
                    sync_source_id=source_id,
                )
            elif source_type == "drata":
                statuses_raw = config.get("statuses", "ACTIVE")
                statuses = [s.strip() for s in statuses_raw.split(",") if s.strip()]
                doc_ids = await sync_drata_policies(
                    sync_db,
                    sync_kb,
                    user_id,
                    statuses=statuses or None,
                    integration_id=bound_integration_id,
                    sync_source_id=source_id,
                )
            elif source_type == "website":
                doc_ids = await sync_website(
                    sync_db,
                    sync_kb,
                    url=config.get("url", ""),
                    max_pages=int(config.get("max_pages", "50")),
                    max_depth=int(config.get("max_depth", "2")),
                    statement_timeout_secs=statement_timeout_secs,
                    sync_source_id=source_id,
                )
            else:
                raise ValueError(f"Unknown sync source type: {source_type}")

        return doc_ids, page_error_count


_ERROR_COOLDOWN = timedelta(minutes=10)


async def _emit_config_error_alert(
    db: AsyncSession,
    *,
    kb_id,
    source_type: str | None,
    error_message: str,
    source_user_id: str | None = None,
) -> None:
    """Surface a permanent configuration error to the KB owner.

    Records the single durable ``sync_config_error`` KB activity event and
    broadcasts a company-wide SSE notification.
    """
    await _emit_sync_notification(
        db,
        kb_id=kb_id,
        source_type=source_type,
        activity_type="sync_config_error",
        detail=(
            f"Sync source disabled due to a configuration error "
            f"({source_type or 'unknown'}): {error_message[:300]}"
        ),
        summary=(
            f"A {source_type or 'knowledge-base'} sync source has been "
            "disabled due to a permanent configuration error. "
            "Fix the source settings, then use Force Re-sync to re-enable it."
        ),
        source_user_id=source_user_id,
    )


# Number of consecutive sync runs in which a Notion page must return HTTP
# 404 or 410 before it is quarantined (skipped) in future syncs.  Consecutive
# means every run since the last success; a run that does NOT include the page
# (e.g. because Notion stopped returning it via a database query) resets the
# counter so only truly persistent missing references accumulate to the
# threshold.
_NOTION_PAGE_STALE_THRESHOLD = 3

# Config dict keys used to persist per-page 404 tracking state inside
# ``KBSyncSource.config`` (a JSON column) for Notion sources.  They are
# constants so every reader/writer shares the same spelling.
_NOTION_CFG_NOT_FOUND_COUNTS = "page_not_found_counts"
_NOTION_CFG_SKIPPED_PAGE_IDS = "skipped_stale_page_ids"
# Periodic recovery probe for quarantined pages: every Nth sync run
# with a non-empty skip list, the quarantine is bypassed so previously-stale
# pages are probed again — pages that no longer 404 are removed from
# ``skipped_stale_page_ids`` (they recovered), pages that still 404 stay
# quarantined.  Without this, the page quarantine was PERMANENT (recovery
# required a manual force re-sync even after the page was restored in
# Notion).  The run counter is persisted in the source config.
_NOTION_PAGE_RECHECK_EVERY = 7
_NOTION_CFG_PAGE_RECHECK_COUNTER = "stale_page_recheck_counter"


def _is_notion_page_recheck_run(config: dict) -> bool:
    """True when THIS run should probe quarantined Notion pages again.

    Shared trigger predicate between ``_run_sync_in_isolated_session``
    (decides whether to bypass the skip list) and
    ``_execute_sync_source_inner`` (tells ``_handle_notion_stale_pages``
    that quarantined pages were probed so recovered ones can be
    unquarantined).  Both read the same persisted counter value, so the
    decision is deterministic within one run.
    """
    _skip_raw = config.get(_NOTION_CFG_SKIPPED_PAGE_IDS)
    has_skips = isinstance(_skip_raw, list) and any(
        isinstance(p, str) for p in _skip_raw
    )
    if not has_skips:
        return False
    raw_counter = config.get(_NOTION_CFG_PAGE_RECHECK_COUNTER)
    # ``type() is int`` (not isinstance): bool is an int subclass and a
    # corrupted ``true`` would otherwise skew the cadence.
    counter = raw_counter if type(raw_counter) is int else 0
    return (counter + 1) % _NOTION_PAGE_RECHECK_EVERY == 0


def _is_permanent_sync_error(
    error_message: str, *, source_type: str | None = None
) -> bool:
    """Return True if *error_message* indicates a permanently-unresolvable
    configuration error.

    Globally, HTTP 404 (Not Found) and 410 (Gone) are treated as permanent:
    they indicate missing resources that require source reconfiguration.
    For Notion sync sources, stale/deleted page references also surface as HTTP
    400 from ``/v1/pages/{id}``, so that pattern is treated as permanent too.
    For website sync sources, ``sync_website`` aggregates many per-URL
    failures into one run-level error and embeds
    ``WEBSITE_ALL_PERMANENT_FAILURES_MARKER`` only when a zero-document crawl
    saw exclusively permanent failures (no transient signal), so that marker
    is treated as permanent as well.

    These status codes mean the configured path, folder, or resource no longer
    exists and will never succeed without changing the source configuration.
    They are distinct from transient errors (5xx, timeouts, rate limits) where
    the same configuration might succeed on the next retry. 403 is
    deliberately excluded: permission errors are often fixed on the provider
    side (expired token, revoked grant) without touching the source config.

    Matching is intentionally simple (substring) so it works against the
    ``str(e)`` output of ``httpx.HTTPStatusError``, ``requests.HTTPError``,
    and plain exception messages — all of which embed the status text.

    Examples of matching strings:
      "Client error '404 Not Found' for url '...'"
      "404 Not Found"
      "410 Gone"
    """
    msg = error_message.lower()
    if "404 not found" in msg or "410 gone" in msg:
        return True

    if source_type == "notion":
        return "400 bad request" in msg and "/v1/pages/" in msg

    if source_type == "website":
        return WEBSITE_ALL_PERMANENT_FAILURES_MARKER.lower() in msg

    return False


def _handle_notion_stale_pages(
    source: KBSyncSource,
    not_found_page_ids: list[str],
    *,
    base_config: dict,
    kb_id: object,
    source_id: str,
    rechecked: bool = False,
) -> list[str]:
    """Update the Notion source config to track per-page 404/410 counts.

    Increments the consecutive-404 counter for each page in
    ``not_found_page_ids`` and resets the counter for any page that is
    NOT in ``not_found_page_ids`` (it either succeeded or was not in scope
    this run — preserving the "consecutive" contract of the threshold).

    Pages that reach ``_NOTION_PAGE_STALE_THRESHOLD`` consecutive 404/410
    responses are quarantined: added to ``skipped_stale_page_ids`` in the
    source config so future scheduled syncs skip them instead of logging the
    same warning on every cycle.

    ``rechecked=True`` means THIS run bypassed the quarantine and probed the
    skipped pages (periodic recovery probe): quarantined pages that
    did NOT 404 this run are removed from the skip list (they recovered);
    pages that still 404 stay quarantined.  Also maintains the persisted
    recheck run counter that drives the probe cadence.

    ``base_config`` must be the config dict captured *before* the early
    ``session.commit()`` in :func:`_execute_sync_source_inner` — passing it
    explicitly avoids a synchronous lazy-load on the expired ORM instance.
    The function assigns a brand-new dict to ``source.config`` so SQLAlchemy
    detects the mutation on this JSON column.

    Returns the page IDs that were newly quarantined by THIS run so the
    caller can surface an owner-visible alert (pages already in the skip
    list are not reported again).
    """
    config = dict(base_config)
    # Tolerate corrupted (wrong-type) JSON values the same way the
    # stale_child_databases reader does — fall back to empty tracking
    # state instead of raising and failing an otherwise successful sync.
    # Item-level corruption is dropped too: non-int counter values would
    # blow up the increment below, and non-string skip-list items would
    # blow up sorted() at write-back — this runs on the SUCCESS path, so
    # it must never turn a good sync into a scheduler error.
    _raw_counts = config.get(_NOTION_CFG_NOT_FOUND_COUNTS)
    counts: dict[str, int] = (
        # ``type(v) is int`` (not isinstance): bool is an int subclass, and a
        # corrupted ``true`` counter would otherwise count as 1 and skew the
        # consecutive threshold.
        {k: v for k, v in _raw_counts.items() if isinstance(k, str) and type(v) is int}
        if isinstance(_raw_counts, dict)
        else {}
    )
    _raw_skipped = config.get(_NOTION_CFG_SKIPPED_PAGE_IDS)
    skipped: set[str] = (
        {p for p in _raw_skipped if isinstance(p, str)}
        if isinstance(_raw_skipped, list)
        else set()
    )

    not_found_set = set(not_found_page_ids)
    newly_quarantined: list[str] = []
    had_skips = bool(skipped)

    # Periodic recovery probe: when this run bypassed the quarantine,
    # any skipped page that did NOT come back as 404 has recovered — remove
    # it from the skip list (and drop its stale counter) so future syncs
    # index it again.  Pages that still 404 remain in ``skipped`` and are
    # ignored by the counting loop below (unchanged behaviour).
    if rechecked and skipped:
        recovered = sorted(p for p in skipped if p not in not_found_set)
        for page_id in recovered:
            skipped.discard(page_id)
            counts.pop(page_id, None)
        if recovered:
            logger.info(
                "KB sync source %s (notion, kb_id=%s): %d previously "
                "quarantined page(s) recovered on periodic recheck and "
                "will be synced again: %s",
                source_id,
                kb_id,
                len(recovered),
                _preview_page_ids(recovered),
            )

    # Sorted for deterministic counter/log/alert ordering across runs.
    for page_id in sorted(not_found_set):
        if page_id in skipped:
            continue  # already quarantined; counter no longer matters
        new_count = (counts.get(page_id, 0) or 0) + 1
        counts[page_id] = new_count
        # DEBUG, not WARNING: sync_notion_pages already logged the per-page
        # failure at WARNING — a second WARNING per run would ADD to the log
        # noise this feature exists to remove. The threshold crossing has its
        # own ERROR log + owner alert below.
        logger.debug(
            "KB sync: Notion page %s in source %s returned 404/410 "
            "(%d/%d consecutive — will be quarantined at threshold)",
            page_id,
            source_id,
            new_count,
            _NOTION_PAGE_STALE_THRESHOLD,
        )
        if new_count >= _NOTION_PAGE_STALE_THRESHOLD:
            skipped.add(page_id)
            newly_quarantined.append(page_id)

    # Reset counts for pages that did NOT 404 this run (consecutive contract).
    for page_id in list(counts.keys()):
        if page_id not in not_found_set and page_id not in skipped:
            del counts[page_id]

    # Drop the keys entirely when empty (instead of storing ``null``) so the
    # user-visible source config doesn't accumulate dead tracking keys.
    if counts:
        config[_NOTION_CFG_NOT_FOUND_COUNTS] = counts
    else:
        config.pop(_NOTION_CFG_NOT_FOUND_COUNTS, None)
    if skipped:
        config[_NOTION_CFG_SKIPPED_PAGE_IDS] = sorted(skipped)
    else:
        config.pop(_NOTION_CFG_SKIPPED_PAGE_IDS, None)

    # Advance the periodic-recheck run counter.  It only exists
    # while there is an active quarantine to recheck: incremented on every
    # run that STARTED with a non-empty skip list (a cadence tick), left
    # absent when the quarantine was created this very run (the cadence
    # starts counting from the next run), dropped once the skip list is
    # empty so the config doesn't accumulate a dead key.
    if skipped:
        if had_skips:
            raw_counter = config.get(_NOTION_CFG_PAGE_RECHECK_COUNTER)
            prior_counter = raw_counter if type(raw_counter) is int else 0
            config[_NOTION_CFG_PAGE_RECHECK_COUNTER] = (
                prior_counter + 1
            ) % _NOTION_PAGE_RECHECK_EVERY
    else:
        config.pop(_NOTION_CFG_PAGE_RECHECK_COUNTER, None)

    # Reassign the whole dict so SQLAlchemy detects the JSON column mutation.
    source.config = config

    if newly_quarantined:
        logger.error(
            "KB sync source %s (notion, kb_id=%s): %d page(s) quarantined "
            "after %d consecutive 404/410 responses and will be skipped in "
            "future syncs: %s — remove the stale reference(s) from the sync "
            "source configuration or use Force Re-sync to reset the counter.",
            source_id,
            kb_id,
            len(newly_quarantined),
            _NOTION_PAGE_STALE_THRESHOLD,
            _preview_page_ids(newly_quarantined),
        )
    return newly_quarantined


# How many page IDs to spell out in quarantine logs / alerts — the count is
# always included separately, so a bounded preview keeps a bulk-staleness
# event (up to _MAX_NOTION_PAGES_SYNC pages) from emitting oversized log
# lines or allocating a full joined string.
_STALE_PAGE_ID_PREVIEW_LIMIT = 5


def _preview_page_ids(page_ids: list[str], limit: int = _STALE_PAGE_ID_PREVIEW_LIMIT) -> str:
    """Join at most *limit* page IDs, appending a ``(+N more)`` suffix."""
    preview = ", ".join(page_ids[:limit])
    remaining = len(page_ids) - limit
    return f"{preview} (+{remaining} more)" if remaining > 0 else preview


async def _emit_stale_page_quarantine_alert(
    db: AsyncSession,
    *,
    kb_id,
    source_type: str | None,
    page_ids: list[str],
    source_user_id: str | None = None,
) -> None:
    """Surface a per-page stale-reference quarantine to the KB owner.

    Records a durable ``sync_alert`` KB activity event and broadcasts a
    company-wide SSE notification so the stale Notion reference is visible
    in the UI rather than only in the logs. Unlike the source-level
    quarantine alerts, the sync source itself stays enabled — only the
    listed pages are skipped in future syncs.
    """
    # Bounded preview (never join the full list): a bulk-staleness event can
    # quarantine hundreds of pages at once and the count is already in the
    # summary below.
    pages = _preview_page_ids(page_ids)
    await _emit_sync_notification(
        db,
        kb_id=kb_id,
        source_type=source_type,
        activity_type="sync_alert",
        detail=(
            f"{STALE_PAGES_PREFIX}{_NOTION_PAGE_STALE_THRESHOLD} consecutive 404/410 responses "
            f"and skipped in future syncs: {pages}. Remove the "
            "reference(s) from the sync source or use Force Re-sync to retry."
        ),
        summary=(
            f"{len(page_ids)} stale Notion page reference(s) in a "
            "knowledge-base sync source no longer exist and will be skipped. "
            "Remove the reference(s) or use Force Re-sync to retry them."
        ),
        source_user_id=source_user_id,
    )


def _reset_notion_stale_page_config(source: KBSyncSource) -> None:
    """Remove per-page stale-tracking state from a Notion sync source config.

    Called on force-resync and re-enable to give all pages a fresh start so
    previously-quarantined pages are retried after the operator resolves the
    underlying issue.
    """
    config = source.__dict__.get("config") or {}
    if not (
        _NOTION_CFG_NOT_FOUND_COUNTS in config
        or _NOTION_CFG_SKIPPED_PAGE_IDS in config
        or _NOTION_CFG_PAGE_RECHECK_COUNTER in config
    ):
        return  # nothing to clear
    config = dict(config)
    config.pop(_NOTION_CFG_NOT_FOUND_COUNTS, None)
    config.pop(_NOTION_CFG_SKIPPED_PAGE_IDS, None)
    config.pop(_NOTION_CFG_PAGE_RECHECK_COUNTER, None)
    source.config = config


async def _emit_quarantine_alert(
    db: AsyncSession,
    *,
    kb_id,
    source_type: str | None,
    stale_count: int,
    quarantine_message: str,
    source_user_id: str | None = None,
) -> None:
    """Surface a stale-reset quarantine event to the KB owner.

    Records a durable ``sync_quarantined`` KB activity event and broadcasts a
    company-wide SSE notification.
    """
    await _emit_sync_notification(
        db,
        kb_id=kb_id,
        source_type=source_type,
        activity_type="sync_quarantined",
        detail=quarantine_message[:500],
        summary=(
            f"A {source_type or 'knowledge-base'} sync source has been "
            f"quarantined after {stale_count} consecutive hangs. "
            "Use Force Re-sync to retry or narrow the sync scope."
        ),
        source_user_id=source_user_id,
        quarantine_alert=True,
    )


async def _emit_failure_quarantine_alert(
    db: AsyncSession,
    *,
    kb_id,
    source_type: str | None,
    failure_count: int,
    error_message: str,
    source_user_id: str | None = None,
) -> None:
    """Surface an error-based quarantine event to the KB owner.

    Records a durable ``sync_quarantined`` KB activity event and broadcasts a
    company-wide SSE notification.
    """
    await _emit_sync_notification(
        db,
        kb_id=kb_id,
        source_type=source_type,
        activity_type="sync_quarantined",
        detail=(
            f"Source disabled after {failure_count} consecutive failures "
            f"({source_type or 'unknown'}). "
            "The source configuration may be permanently invalid "
            "(e.g. a renamed folder or deleted path). "
            f"Last error: {error_message[:200]}"
        ),
        summary=(
            f"A {source_type or 'knowledge-base'} sync source has been "
            f"disabled after {failure_count} consecutive errors. "
            "Update the source configuration and use Force Re-sync to retry."
        ),
        source_user_id=source_user_id,
        quarantine_alert=True,
    )


async def _emit_perm_fail_quarantine_alert(
    db: AsyncSession,
    *,
    kb_id,
    source_type: str | None,
    perm_fail_count: int,
    quarantine_message: str,
    source_user_id: str | None = None,
) -> None:
    """Surface a permanent-failure quarantine event to the KB owner.

    Records a durable ``sync_quarantined`` KB activity event and broadcasts a
    company-wide SSE notification so the "needs reconfiguration" event is
    visible in the UI rather than only in the logs.
    """
    await _emit_sync_notification(
        db,
        kb_id=kb_id,
        source_type=source_type,
        activity_type="sync_quarantined",
        detail=quarantine_message[:500],
        summary=(
            f"A {source_type or 'knowledge-base'} sync source has been "
            f"disabled after {perm_fail_count} consecutive 'not found' "
            "errors and needs to be reconfigured. "
            "Update the source path and use Force Re-sync to retry."
        ),
        source_user_id=source_user_id,
        quarantine_alert=True,
    )


async def _emit_partial_fail_alert(
    db: AsyncSession,
    *,
    kb_id,
    source_type: str | None,
    partial_fail_count: int,
    source_user_id: str | None = None,
) -> None:
    """Surface a partial-page-failure alert to the KB owner.

    Emitted when a sync source has produced page-level failures in N
    consecutive sync runs (``kb_sync_partial_fail_alert_threshold``), even
    though the overall sync still returned some documents.  This surfaces
    stale Notion page references that would otherwise fail silently forever.

    Records a durable ``sync_alert`` KB activity event and broadcasts a
    company-wide SSE notification.  The source is NOT disabled — the alert
    is informational so the operator can prune the stale page references.
    """
    await _emit_sync_notification(
        db,
        kb_id=kb_id,
        source_type=source_type,
        activity_type="sync_alert",
        detail=(
            f"{PARTIAL_FAILURES_PREFIX}{partial_fail_count} consecutive run(s) "
            f"({source_type or 'unknown'}). "
            "Some page references may be stale or inaccessible "
            "(e.g. deleted Notion pages). "
            "Review the source configuration and remove broken page IDs."
        ),
        summary=(
            f"A {source_type or 'knowledge-base'} sync source has "
            f"had stale page-reference failures in "
            f"{partial_fail_count} consecutive syncs. "
            "Review and prune broken page references."
        ),
        source_user_id=source_user_id,
    )


async def reset_stale_syncs(db: AsyncSession) -> int:
    """Reset sync sources stuck in 'syncing' state for too long.

    A sync is considered stale if ``last_sync_status == 'syncing'`` and
    its liveness heartbeat (``sync_heartbeat_at``, falling back to
    ``last_synced_at`` for legacy rows or a sync that died before its
    first beat) is either NULL or older than ``kb_sync_stale_after_secs``.

    This window is **decoupled** from the per-attempt execution timeout
    (``kb_sync_timeout_secs``): a legitimately long sync keeps bumping its
    heartbeat and is never falsely reset, while an orphaned sync (worker
    restarted/OOM-killed, so the heartbeat went silent) recovers within
    minutes instead of waiting ``timeout + 5 min`` (~35 min).

    Uses ``FOR UPDATE SKIP LOCKED`` so that rows still held by an active
    (stuck) sync transaction are silently skipped instead of blocking
    until the statement timeout fires.

    Sources that are stale-reset ``kb_sync_stale_quarantine_threshold``
    consecutive times are quarantined (disabled) with a clear error message
    to stop the hung-sync cycle from consuming worker capacity indefinitely.
    Recovery is via Force Re-sync or by re-enabling the source in settings.

    Returns the number of sources reset.
    """
    stale_window = _stale_sync_window()
    cutoff = datetime.now(UTC) - stale_window
    # Prefer the dedicated heartbeat; fall back to ``last_synced_at`` so
    # rows written before the heartbeat column existed (or a sync that
    # died before its first beat) are still recoverable.
    liveness = func.coalesce(
        KBSyncSource.sync_heartbeat_at, KBSyncSource.last_synced_at
    )
    result = await db.execute(
        select(KBSyncSource)
        .where(
            KBSyncSource.last_sync_status == "syncing",
            (liveness.is_(None)) | (liveness < cutoff),
        )
        .with_for_update(skip_locked=True)
    )
    stale_sources = list(result.scalars().all())
    now = datetime.now(UTC)
    # Round up so the persisted error message never under-reports the actual
    # window (e.g. a 91s window must read "> 2 min", not "> 1 min" via
    # floor division).  Clamp to 1 so sub-minute windows still produce a
    # human-readable message.
    stale_minutes = max(1, math.ceil(stale_window.total_seconds() / 60))
    stale_error_message = (
        f"{SYNC_STALLED_PREFIX}{stale_minutes} min) — "
        "reset automatically. Use Force Re-sync to try again."
    )
    stale_cap = settings.kb_sync_stale_retry_max_delay_seconds
    quarantine_threshold = settings.kb_sync_stale_quarantine_threshold
    alerts: list[tuple] = []
    quarantine_alerts: list[tuple] = []
    for source in stale_sources:
        # Bind KB / source identifiers so the reset + quarantine records
        # below (and the escalation log inside ``_record_sync_failure``)
        # are actionable without a DB lookup.
        with kb_sync_log_context(
            kb_id=source.knowledge_base_id,
            sync_source_id=source.id,
            source_type=source.source_type,
        ):
            # Increment the dedicated stale-reset counter (distinct from the
            # general ``sync_failure_count`` which also counts clean
            # exceptions). This counter specifically tracks the "hangs
            # without heartbeat" failure pattern.
            new_stale_count = (source.sync_stale_count or 0) + 1
            source.sync_stale_count = new_stale_count

            # A hang is not a permanent (404/410) error — it breaks the chain
            # of consecutive 'not found' failures, so reset the perm-fail
            # budget to keep the "consecutive" contract of the
            # quarantine strict.
            source.sync_perm_fail_count = 0
            # Same reset for the partial-failure counter — a stale/hung run
            # is not a page-level failure.
            source.sync_partial_fail_count = 0

            # Treat a stuck/stale sync as a failure: increment the
            # consecutive-failure counter and schedule the next backoff retry
            # so the scheduler picks it up promptly rather than
            # waiting the full interval. Cap the backoff lower than a genuine
            # error (``stale_cap``) so a transient worker restart doesn't jump
            # straight to a multi-hour delay.
            escalated = _record_sync_failure(
                source,
                prior_failure_count=source.sync_failure_count or 0,
                error_message=stale_error_message,
                now=now,
                source_id=str(source.id),
                source_type=source.source_type,
                max_delay_override=stale_cap,
            )
            logger.warning(
                "Reset stale sync source %s (type=%s, kb_id=%s) — was stuck "
                "in 'syncing' (stale_count=%d)",
                source.id,
                source.source_type,
                source.knowledge_base_id,
                new_stale_count,
            )

            # Quarantine: if this source has been stale-reset too many
            # consecutive times it is systematically hanging — disable it so
            # it stops consuming worker capacity on every retry.  The KB
            # owner is notified via a KB activity event.  Recovery is via
            # Force Re-sync (which re-enables the source and resets the
            # counter) or by manually toggling the enabled flag in settings.
            if quarantine_threshold > 0 and new_stale_count >= quarantine_threshold:
                source.enabled = False
                quarantine_message = (
                    f"{QUARANTINE_ERROR_PREFIX} {new_stale_count} consecutive stale "
                    f"resets (type={source.source_type}). The sync repeatedly hangs "
                    "without completing — consider narrowing the sync scope (e.g. a "
                    "smaller repo path or fewer folders). Use Force Re-sync to retry "
                    "or re-enable the source in settings."
                )
                source.last_sync_error = quarantine_message[:500]
                logger.error(
                    "Quarantining KB sync source %s (type=%s, kb_id=%s) after "
                    "%d consecutive stale resets — disabling to stop the "
                    "hung-sync cycle",
                    source.id,
                    source.source_type,
                    source.knowledge_base_id,
                    new_stale_count,
                )
                quarantine_alerts.append(
                    (
                        source.knowledge_base_id,
                        source.source_type,
                        new_stale_count,
                        quarantine_message,
                        getattr(source, "user_id", None),
                    )
                )

            if escalated:
                alerts.append(
                    (
                        source.knowledge_base_id,
                        source.source_type,
                        source.sync_failure_count,
                        getattr(source, "user_id", None),
                    )
                )
    if stale_sources:
        await db.flush()
    # Emit admin alerts after the flush so a best-effort notification can
    # never roll back the resets themselves.
    for kb_id, source_type, failure_count, src_user_id in alerts:
        await _emit_persistent_failure_alert(
            db,
            kb_id=kb_id,
            source_type=source_type,
            failure_count=failure_count,
            error_message=stale_error_message,
            source_user_id=str(src_user_id) if src_user_id else None,
        )
    for (
        kb_id,
        source_type,
        stale_count,
        quarantine_message,
        src_user_id,
    ) in quarantine_alerts:
        await _emit_quarantine_alert(
            db,
            kb_id=kb_id,
            source_type=source_type,
            stale_count=stale_count,
            quarantine_message=quarantine_message,
            source_user_id=str(src_user_id) if src_user_id else None,
        )
    return len(stale_sources)


def _claim_source_stmt(source_id: uuid.UUID) -> Select[tuple[KBSyncSource]]:
    """Build the atomic claim query for one due sync source.

    ``FOR UPDATE SKIP LOCKED`` makes a concurrent worker replica that already
    holds the row lock get no row back (instead of blocking), and the
    ``IS DISTINCT FROM 'syncing'`` predicate excludes rows another replica
    already committed as ``"syncing"`` between this tick's snapshot and the
    re-fetch — together closing the double-pickup TOCTOU window.

    ``IS DISTINCT FROM`` (not ``!=``) so rows with a NULL status — never
    synced — remain claimable; PostgreSQL's ``IS NOT`` only accepts
    NULL/boolean operands, so ``is_not("syncing")`` would be a server-side
    syntax error.

    Module-level so tests can compile it against the PostgreSQL dialect and
    assert the rendered SQL (mocked-session tests cannot catch SQL validity).
    """
    return (
        select(KBSyncSource)
        .where(
            KBSyncSource.id == source_id,
            KBSyncSource.last_sync_status.is_distinct_from("syncing"),
        )
        .with_for_update(skip_locked=True)
    )


async def process_scheduled_kb_syncs(db: AsyncSession) -> int:
    """Check all enabled sync sources and trigger overdue ones.

    Called from the autonomous tick (every 60s).
    Returns the number of syncs triggered.
    """
    count = 0

    # Find all enabled, non-manual sync sources
    result = await db.execute(
        select(KBSyncSource).where(
            KBSyncSource.enabled.is_(True),
            KBSyncSource.sync_interval != "manual",
        )
    )
    sources = result.scalars().all()

    now = datetime.now(UTC)

    auth_free_source_types = {"website", "github_public"}
    timeout_secs = _kb_sync_timeout_secs()

    # Pre-extract scheduling attributes into plain Python values.
    # Inside the loop, db.commit() / db.rollback() expire all ORM objects
    # in the session.  Accessing an expired attribute (e.g. sync_interval)
    # triggers a lazy load through SQLAlchemy's synchronous attribute
    # machinery, which fails with MissingGreenlet when the underlying
    # connection was left in a bad state by asyncio.wait_for cancellation.
    source_infos = [
        (
            s.id,
            s.sync_interval,
            s.last_synced_at,
            s.last_sync_status,
            s.knowledge_base_id,
            s.user_id,
            s.source_type,
            s.sync_next_retry_at,
        )
        for s in sources
    ]

    for (
        src_id,
        sync_interval,
        last_synced_at,
        last_status,
        kb_id,
        src_user_id,
        src_source_type,
        sync_next_retry_at,
    ) in source_infos:
        delta = INTERVAL_DELTAS.get(sync_interval)
        if not delta:
            continue

        # Skip sources that are already being synced (prevents concurrent
        # execution of the same source from overlapping scheduler ticks).
        if last_status == "syncing":
            continue

        if last_status == "error":
            # Failed / timed-out sources retry on the exponential-backoff
            # schedule (``sync_next_retry_at``) rather than waiting the
            # full interval — a daily/weekly source must not stay stale
            # for a day after a transient failure. A
            # persistently-failing source backs off because
            # ``sync_next_retry_at`` grows with each consecutive failure.
            if sync_next_retry_at is not None:
                if now < sync_next_retry_at:
                    continue
            elif last_synced_at and (now - last_synced_at) < _ERROR_COOLDOWN:
                # Legacy rows with no scheduled retry fall
                # back to the flat cooldown so they retry promptly without
                # hammering the source every 60s tick.
                continue
        else:
            # Healthy sources use the configured interval.
            if last_synced_at and (now - last_synced_at) < delta:
                continue  # Not due yet

        source_id_str = str(src_id)

        # Re-fetch source and KB fresh each iteration to avoid using
        # expired ORM objects from a prior commit/rollback cycle.
        # The claim query (see _claim_source_stmt) atomically claims the row
        # with FOR UPDATE SKIP LOCKED + an IS DISTINCT FROM 'syncing' filter,
        # so a concurrent worker replica either sees no row (lock held) or is
        # filtered out (status already committed) instead of launching a
        # duplicate 1800 s sync. The lock is released
        # milliseconds later by execute_sync_source's early "syncing" commit.
        try:
            source_result = await db.execute(_claim_source_stmt(src_id))
            source = source_result.scalar_one_or_none()
            if not source:
                # Row is either locked by a concurrent worker (SKIP LOCKED
                # returned nothing) or another replica already committed
                # "syncing" between our snapshot and this re-fetch.
                logger.debug(
                    "KB sync source %s is locked or already claimed — skipping",
                    source_id_str,
                )
                continue

            kb_result = await db.execute(
                select(KnowledgeBase).where(KnowledgeBase.id == kb_id)
            )
            kb = kb_result.scalar_one_or_none()
            if not kb:
                continue
        except Exception:
            logger.warning(
                "Failed to load source/KB %s — skipping (session may need recovery)",
                source_id_str,
            )
            with contextlib.suppress(Exception):
                await db.rollback()
            continue

        # Captured as a plain string BEFORE the sync runs: the TimeoutError
        # handler below must never touch the ORM object after an
        # asyncio.wait_for cancellation possibly degraded the session
        # (expired-attribute access would lazy-load → MissingGreenlet).
        kb_name = kb.name

        # Bind KB / source identifiers for every record emitted while this
        # source is being scheduled (timeouts, failures, skips) so operators
        # can act on a log line without a DB lookup.
        with kb_sync_log_context(
            kb_id=kb_id,
            kb_name=kb_name,
            sync_source_id=src_id,
            source_type=src_source_type,
        ):
            logger.info(
                "Scheduled KB sync: %s (%s) for KB '%s' — last synced %s",
                source_id_str,
                src_source_type,
                kb_name,
                last_synced_at.isoformat() if last_synced_at else "never",
            )

            try:
                user_id_str = (
                    str(src_user_id)
                    if src_user_id
                    else str(kb.user_id) if kb.user_id else None
                )
                if not user_id_str:
                    if src_source_type in auth_free_source_types:
                        user_id_str = "system"
                    else:
                        logger.warning(
                            "Skipping sync source %s: no user_id on source or KB",
                            source_id_str,
                        )
                        continue
                source_timeout_secs = _source_timeout_secs(
                    source, default_timeout_secs=timeout_secs
                )

                try:
                    await asyncio.wait_for(
                        execute_sync_source(
                            db,
                            source,
                            kb,
                            user_id_str,
                            timeout_secs=source_timeout_secs,
                        ),
                        timeout=source_timeout_secs,
                    )
                except TimeoutError:
                    logger.error(
                        "KB sync for source %s (%s) on KB '%s' (%s) timed out "
                        "after %ss",
                        source_id_str,
                        src_source_type,
                        kb_name,
                        kb_id,
                        source_timeout_secs,
                    )
                    with contextlib.suppress(Exception):
                        await db.rollback()
                    timeout_error = f"{SYNC_TIMEOUT_PREFIX}{source_timeout_secs}s"
                    # Re-fetch on a clean session: the asyncio.wait_for
                    # cancellation can leave the prior ORM object / session in
                    # a bad state, so load a fresh instance to record against.
                    try:
                        src_result = await db.execute(
                            select(KBSyncSource).where(KBSyncSource.id == src_id)
                        )
                        timed_out_src = src_result.scalar_one_or_none()
                    except Exception:
                        timed_out_src = None
                        with contextlib.suppress(Exception):
                            await db.rollback()
                        logger.warning(
                            "Failed to load source %s to record timeout",
                            source_id_str,
                            exc_info=True,
                        )
                    if timed_out_src is not None:
                        # Same handling as any other failure: schedule the
                        # next backoff retry AND write the
                        # user-visible ``sync_failed`` activity event that the
                        # timeout path previously omitted.
                        # timed_out_src is freshly loaded so its attributes
                        # are available; pass prior_perm_fail_count and
                        # source_user_id explicitly to match the same
                        # contract as the exception path.
                        _timeout_src_user = getattr(timed_out_src, "user_id", None)
                        await _handle_sync_failure(
                            db,
                            timed_out_src,
                            kb_id=kb_id,
                            prior_failure_count=timed_out_src.sync_failure_count
                            or 0,
                            prior_perm_fail_count=getattr(
                                timed_out_src, "sync_perm_fail_count", 0
                            )
                            or 0,
                            error_message=timeout_error,
                            activity_detail=timeout_error,
                            source_id=source_id_str,
                            source_type=src_source_type,
                            source_user_id=(
                                str(_timeout_src_user) if _timeout_src_user else None
                            ),
                        )
                await db.commit()
                count += 1
            except Exception as e:
                logger.error(
                    "Scheduled KB sync failed for %s: %s",
                    source_id_str,
                    format_exception_message(e),
                    exc_info=True,
                )
                try:
                    await db.commit()
                except Exception:
                    with contextlib.suppress(Exception):
                        await db.rollback()

    return count
