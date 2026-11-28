# SPDX-License-Identifier: Apache-2.0

"""The durable inbox of verified CTEM signal-source deliveries (#2570).

OpenCTI never retries a webhook: a signal-source delivery answered 429/503, or held
past the sender's timeout, is lost. During a case-opened storm the recorders
(``ctem_signal_listeners.note_event``, ``ctem_analyst_mark.note_event``,
``ctem_openaev_events.note_event``) waited on a saturated pool and loop while the
request held its slot. The webhook now writes the verified delivery here in one
short transaction and answers ``202`` at once (:func:`accept`); :func:`drain`, run by
the autonomous tick, records the rows oldest first, a bounded batch per pass, and
deletes each once recorded. A row whose recording raises is kept and retried up to
:data:`MAX_ATTEMPTS`, then dropped with a log line naming it.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from prometheus_client import Gauge
from sqlalchemy import delete, select, update

from app.core.utils import format_exception_message

logger = logging.getLogger(__name__)

#: Rows recorded per drain pass: one tick never holds the loop for a whole storm.
DRAIN_BATCH = 200
#: Recording attempts before a row is dropped.
MAX_ATTEMPTS = 5
#: Seconds one tick spends draining, batch after batch (#2584 gate line 2): one batch per 60 s tick
#: recorded 10,000 catch-up updates in about 50 minutes in the lab; about 1,100 rows fit in 20 s
#: there, a third of the tick's window, so the scheduler's other steps keep theirs.
DRAIN_BUDGET_SECONDS = 20.0


async def accept(
    provider: str, integration_id: str, event_key: str, payload: Mapping[str, Any], *, verified: bool
) -> None:
    """Write one verified delivery to the inbox and commit; the request answers right after."""
    from app.core.database import async_session
    from app.models.ctem_signal_inbox import CtemSignalInbox

    async with async_session() as db:
        db.add(
            CtemSignalInbox(
                provider=provider,
                integration_id=str(integration_id),
                event_key=(event_key or "")[:200],
                payload=dict(payload),
                verified=verified,
            )
        )
        await db.commit()


async def record(
    provider: str, integration_id: str, event_key: str, payload: dict[str, Any], *, verified: bool
) -> bool:
    """What the webhook used to do inline: hand one delivery to its recorders. ``True`` when a signal was recorded.

    #2584: the signal is recorded on the intake engine (``database.intake_session``), its own
    connection budget, so draining an OpenCTI catch-up never takes the web pool the UI waits on.
    """
    from app.core.database import intake_session
    from app.services import ctem_analyst_mark, ctem_openaev_events, ctem_signal_listeners

    if provider == "openaev":
        await ctem_openaev_events.stamp_reachability_assertion(
            event_key, payload, integration_id=str(integration_id), verified=verified
        )
    recorded = await ctem_signal_listeners.note_event(
        provider, event_key, payload, verified=verified, session_factory=intake_session
    )
    if provider == "opencti":
        await ctem_analyst_mark.note_event(provider, payload, verified=verified)
        named = payload.get(TRACKED_KEY)
        if isinstance(named, list) and named:
            # #2583 point 5: path 11, one signal per tracked organization the report or incident names.
            recorded = bool(
                await ctem_signal_listeners.note_tracked_organizations(
                    payload, [str(n) for n in named], verified=verified, session_factory=intake_session
                )
            ) or bool(recorded)
    if provider == "openaev":
        await ctem_openaev_events.note_event(event_key, payload, verified=verified)
    return bool(recorded)


#: Where the receiver records the tracked organizations an OpenCTI delivery names (#2583 point 5).
TRACKED_KEY = "_tracked_organizations"
#: The path whose source state holds the tracked organizations' resolved ids.
_THIRD_PARTY_PATH = "third_party_event"


def tracked_named(source_state: Any, payload: Mapping[str, Any]) -> list[str]:
    """The XTM One ids of the tracked organizations an OpenCTI delivery names by identity. Pure.

    Read from the organizations the source's path 11 trigger resolved at provisioning
    (internal and standard ids, ``opencti_trigger_provisioning._organization_scope``).
    """
    from app.services.organization_events import named_organizations

    paths = source_state.get("paths") if isinstance(source_state, dict) else None
    sub = paths.get(_THIRD_PARTY_PATH) if isinstance(paths, dict) else None
    if not isinstance(sub, dict):
        return []
    return named_organizations(payload.get("data"), sub.get("organizations"))


def _object_key(provider: str, integration_id: str, event_key: str, payload: Any) -> str | None:
    """The key repeated updates of one OpenCTI object share; ``None`` for a delivery never folded."""
    if provider != "opencti" or "delete" in (event_key or "").casefold() or not isinstance(payload, Mapping):
        return None
    from app.services.ctem_signal_listeners import opencti_internal_id

    data = payload.get("data")
    object_id = opencti_internal_id(data) if isinstance(data, Mapping) else ""
    return f"{integration_id}:{event_key}:{object_id}" if object_id else None


def coalesce(batch: list[tuple[Any, ...]]) -> tuple[list[tuple[Any, ...]], list[Any]]:
    """Fold repeated updates of one OpenCTI object into the latest one (#2584). Pure.

    *batch* is oldest first; the latest delivery of each object keeps its place, the earlier
    ones are returned apart (their row ids), recorded never: during a catch-up OpenCTI sends one
    update per field written, and only the object as last delivered matters. A delete, an
    OpenAEV or OpenCRQ delivery, or one without an object id is never folded.
    """
    latest: dict[str, int] = {}
    for index, row in enumerate(batch):
        key = _object_key(row[1], row[2], row[3], row[4])
        if key is not None:
            latest[key] = index
    kept, folded = [], []
    for index, row in enumerate(batch):
        key = _object_key(row[1], row[2], row[3], row[4])
        if key is None or latest[key] == index:
            kept.append(row)
        else:
            folded.append(row[0])
    return kept, folded


async def drain(limit: int = DRAIN_BATCH, *, deadline: float | None = None) -> dict[str, int]:
    """Record up to *limit* waiting deliveries, oldest first; the counts recorded, retried and dropped.

    Rows are read with ``SKIP LOCKED`` so two pods never record one delivery twice, and
    each row is deleted in its own short transaction once recorded: no connection is
    held while a recorder works.

    *deadline* (a ``time.monotonic()`` value, #2774) is read before every row: past it the
    batch stops, what was recorded is already deleted, and the rows left stay in the inbox
    for the next pass (counted ``deferred``, their claimed attempt given back).
    """
    import time

    from app.core.database import async_session
    from app.models.ctem_signal_inbox import CtemSignalInbox

    counts = {"recorded": 0, "retried": 0, "dropped": 0, "coalesced": 0, "deferred": 0}
    async with async_session() as db:
        rows = (
            (
                await db.execute(
                    select(CtemSignalInbox)
                    .order_by(CtemSignalInbox.created_at)
                    .limit(limit)
                    .with_for_update(skip_locked=True)
                )
            )
            .scalars()
            .all()
        )
        if not rows:
            return counts
        ids = [row.id for row in rows]
        batch = [
            (row.id, row.provider, row.integration_id, row.event_key, row.payload, row.verified, row.attempts)
            for row in rows
        ]
        # Claimed by bumping attempts first: a pod killed mid-batch leaves a counted attempt, never a loop.
        await db.execute(
            update(CtemSignalInbox).where(CtemSignalInbox.id.in_(ids)).values(attempts=CtemSignalInbox.attempts + 1)
        )
        await db.commit()
    batch, folded = coalesce(batch)
    for row_id in folded:
        await _delete(row_id)
    counts["coalesced"] = len(folded)
    for index, (row_id, provider, integration_id, event_key, payload, verified, attempts) in enumerate(batch):
        if deadline is not None and time.monotonic() >= deadline:
            left = [row[0] for row in batch[index:]]
            counts["deferred"] = len(left)
            await _give_back(left)
            break
        try:
            await record(provider, integration_id, event_key, payload or {}, verified=verified)
        except Exception as exc:
            if attempts + 1 >= MAX_ATTEMPTS:
                logger.error(
                    "CTEM signal inbox: delivery %s (%s %s) dropped after %d attempts: %s",
                    row_id,
                    provider,
                    event_key,
                    attempts + 1,
                    format_exception_message(exc),
                )
                counts["dropped"] += 1
                await _delete(row_id)
            else:
                counts["retried"] += 1
            continue
        counts["recorded"] += 1
        await _delete(row_id)
    return counts


async def drain_within(
    budget_seconds: float = DRAIN_BUDGET_SECONDS, limit: int = DRAIN_BATCH, *, deadline: float | None = None
) -> dict[str, int]:
    """Drain batch after batch until the inbox is empty or the deadline has passed; the summed counts.

    The deadline is *deadline* when given, else now plus *budget_seconds*. It is read before
    every row (#2774), so a slow recorder never holds the pass past it by more than one row.
    """
    import time

    totals = {"recorded": 0, "retried": 0, "dropped": 0, "coalesced": 0, "deferred": 0}
    stop = deadline if deadline is not None else time.monotonic() + budget_seconds
    while True:
        counts = await drain(limit, deadline=stop)
        for key, value in counts.items():
            totals[key] = totals.get(key, 0) + value
        handled = counts["recorded"] + counts["dropped"] + counts["coalesced"]
        # Empty, or only retries left (a batch that recorded nothing): wait for the next pass.
        if handled == 0 or counts.get("deferred") or time.monotonic() >= stop:
            return totals


#: Waiting deliveries past which a growing inbox is logged (#2584 point 4): a catch-up the drain does not keep up with.
DEPTH_WARNING = 1000
#: The depth last logged; a new line only when the inbox grew past it, reset once it drains under the threshold.
_last_logged_depth = 0


QUEUE_DEPTH_GAUGE = Gauge(
    "xtmone_ctem_signal_inbox_depth",
    "Verified signal source deliveries accepted and waiting to be recorded (#2584)",
)


async def depth() -> int | None:
    """Deliveries waiting in the inbox (sets the gauge); ``None`` when the database cannot be read."""
    from sqlalchemy import func

    from app.core.database import intake_session
    from app.models.ctem_signal_inbox import CtemSignalInbox

    try:
        async with intake_session() as db:
            value = int((await db.execute(select(func.count()).select_from(CtemSignalInbox))).scalar_one())
    except Exception:
        return None
    QUEUE_DEPTH_GAUGE.set(value)
    note_depth(value)
    return value


def note_depth(value: int) -> bool:
    """Log a warning when the inbox depth grows past :data:`DEPTH_WARNING`; ``True`` when a line was written.

    One line per growth step (the depth at least doubled since the last line, or crossed the
    threshold again after draining under it), so a storm leaves a readable trail, never a line per tick.
    """
    global _last_logged_depth
    if value < DEPTH_WARNING:
        _last_logged_depth = 0
        return False
    if _last_logged_depth and value < 2 * _last_logged_depth:
        return False
    logger.warning(
        "CTEM signal inbox: %d verified deliveries waiting to be recorded (warning at %d): the drain is behind",
        value,
        DEPTH_WARNING,
    )
    _last_logged_depth = value
    return True


async def _give_back(row_ids: list[Any]) -> None:
    """The rows a batch claimed and never reached (#2774): their attempt given back, so a deadline never drops a row."""
    if not row_ids:
        return
    from app.core.database import async_session
    from app.models.ctem_signal_inbox import CtemSignalInbox

    async with async_session() as db:
        await db.execute(
            update(CtemSignalInbox)
            .where(CtemSignalInbox.id.in_(row_ids), CtemSignalInbox.attempts > 0)
            .values(attempts=CtemSignalInbox.attempts - 1)
        )
        await db.commit()


async def _delete(row_id: Any) -> None:
    from app.core.database import async_session
    from app.models.ctem_signal_inbox import CtemSignalInbox

    async with async_session() as db:
        await db.execute(delete(CtemSignalInbox).where(CtemSignalInbox.id == row_id))
        await db.commit()
