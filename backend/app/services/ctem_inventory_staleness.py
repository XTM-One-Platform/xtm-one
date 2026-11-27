# SPDX-License-Identifier: Apache-2.0
"""The inventory evaluation of trigger path 6, stale or orphaned asset (#2642).

The two other sources of path 6 fire on a change: the OpenAEV listener when an
asset turns stale there, the attack-surface pass on a difference between two of
its reads. Neither looks at the inventory as it stands, so after a purge and a
factory reset the path reads on with 0 signals while the inventory holds assets
that no source has seen for weeks, or whose sync is gone.

This pass reads the inventory records of the primary organization once per day,
and once at the resync step of a purge (``ctem_signal_resync.resync_passes``):

- ``unseen``: a record whose ``last_seen_at`` is older than the profile's
  ``attack_surface.stale_after_days`` (14 by default). A record whose sync failed
  its last run is withheld and counted apart: its age is the sync's failure, not
  the asset's.
- ``orphaned``: a record written by a sync that no longer exists.

Each finding goes through ``ctem_signals.record_signal`` like every other source
(same exclusions, dedupe key, scope gate). The outcome of the last evaluation is
kept so the signal sources view says why the path records nothing
(:func:`last_outcome`, ``ctem_signals.silent_reason``).
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

#: The source of this pass in ``ctem_signal_sources.PATH_SOURCES``.
PROVIDER = "xtm_one"
KIND = "pass"
PATH = "stale_asset"
#: The default age past which an asset unseen by its source is stale (the attack-surface default).
DEFAULT_STALE_DAYS = 14
#: Records one evaluation reads at most; the rest is counted as ``truncated`` and said by the view.
MAX_RECORDS = 100_000
#: Records read per page (keyset on the record id, so every record is reached up to the cap).
PAGE_SIZE = 5000

_SLOT_KEY = "ctem:inventory-staleness:pass"
_LAST_KEY = "ctem:inventory-staleness:last"
_SLOT_SECONDS = 24 * 3600
_LAST_SECONDS = 30 * 24 * 3600


def _failed(sync: Any) -> bool:
    """A sync whose last run failed or that stopped by itself."""
    result = sync.last_result if isinstance(sync.last_result, dict) else {}
    return bool(sync.paused_reason or result.get("error"))


def findings(
    records: list[Any], syncs: list[Any], *, now: datetime, stale_days: int
) -> tuple[list[tuple[Any, str, dict[str, Any]]], dict[str, int]]:
    """The ``(record, detail, evidence)`` findings of *records*, and the counts the outcome reports.

    *syncs* are every organization sync row (``OrganizationContextSync``); a record's
    ``sync_id`` names one of them, or none when the record was typed or imported once.
    """
    by_id = {str(s.id): s for s in syncs}
    failed = {sid for sid, s in by_id.items() if _failed(s)}
    limit = now - timedelta(days=stale_days)
    out: list[tuple[Any, str, dict[str, Any]]] = []
    counts = {"evaluated": len(records), "unseen": 0, "orphaned": 0, "withheld_sync_failed": 0}
    for record in records:
        sync_id = str(record.sync_id or "")
        if sync_id and sync_id not in by_id:
            counts["orphaned"] += 1
            out.append((record, "orphaned", {"sync_id": sync_id}))
            continue
        seen = record.last_seen_at
        if seen is None:
            continue
        if seen.tzinfo is None:
            seen = seen.replace(tzinfo=UTC)
        if seen >= limit:
            continue
        if sync_id in failed:
            counts["withheld_sync_failed"] += 1
            continue
        counts["unseen"] += 1
        days = max(stale_days, (now - seen).days)
        out.append((record, "unseen", {"last_seen_at": seen.isoformat(), "days_unseen": days}))
    counts["failed_syncs"] = len(failed)
    return out, counts


def _reason(detail: str, evidence: dict[str, Any], stale_days: int) -> str:
    if detail == "orphaned":
        return "no sync owns this asset any more: the sync that wrote it was deleted"
    return f"not seen by its source for {evidence.get('days_unseen', stale_days)} days (threshold {stale_days})"


async def evaluate(
    db: AsyncSession, *, organization_id: uuid.UUID, profile: Any, now: datetime | None = None
) -> dict[str, Any]:
    """Evaluate the inventory once and record its path 6 signals; the caller commits."""
    from app.models.organization_asset_record import OrganizationAssetRecord
    from app.models.organization_context import OrganizationContextSync
    from app.services import ctem_case_loop, ctem_scope_gate_store, ctem_signals
    from app.services.ctem_signal_sources import is_excluded, parse_excluded

    at = now or datetime.now(UTC)
    surface = getattr(profile, "attack_surface", None)
    stale_days = int(getattr(surface, "stale_after_days", DEFAULT_STALE_DAYS) or DEFAULT_STALE_DAYS)
    if is_excluded(parse_excluded(profile), PATH, PROVIDER, KIND):
        return {"at": at.isoformat(), "stale_after_days": stale_days, "recorded": 0, "skipped": "excluded"}
    syncs = list((await db.execute(select(OrganizationContextSync))).scalars())
    found: list[tuple[Any, str, dict[str, Any]]] = []
    counts = {"evaluated": 0, "unseen": 0, "orphaned": 0, "withheld_sync_failed": 0}
    after: uuid.UUID | None = None
    while counts["evaluated"] < MAX_RECORDS:
        size = min(PAGE_SIZE, MAX_RECORDS - counts["evaluated"])
        query = select(OrganizationAssetRecord).where(OrganizationAssetRecord.organization_id == organization_id)
        if after is not None:
            query = query.where(OrganizationAssetRecord.id > after)
        page = list((await db.execute(query.order_by(OrganizationAssetRecord.id).limit(size))).scalars())
        if not page:
            break
        page_found, page_counts = findings(page, syncs, now=at, stale_days=stale_days)
        found.extend(page_found)
        for key in counts:
            counts[key] += page_counts[key]
        after = page[-1].id
        if len(page) < size:
            break
    total = (
        await db.execute(
            select(func.count())
            .select_from(OrganizationAssetRecord)
            .where(OrganizationAssetRecord.organization_id == organization_id)
        )
    ).scalar_one()
    counts["truncated"] = max(0, int(total or 0) - counts["evaluated"])
    counts["failed_syncs"] = sum(1 for s in syncs if _failed(s))
    pending = ctem_case_loop.PendingEvents()
    recorded = 0
    for record, detail, evidence in found:
        signal, created = await ctem_signals.record_signal(
            db,
            organization_id=organization_id,
            signal=ctem_signals.SignalInput(
                trigger_path=PATH,
                source_provider=PROVIDER,
                subject_kind="asset",
                subject_ref=str(record.name)[:300],
                lens="outside",
                subject_label=f"{record.name}: {_reason(detail, evidence, stale_days)}",
                observed_at=at,
                evidence={
                    **evidence,
                    "detail": detail,
                    "record_id": str(record.id),
                    "source": str(record.source),
                    ctem_signals.SOURCE_KEY_EVIDENCE: f"{PROVIDER}:{KIND}",
                },
                dedupe_key=f"{PATH}:{PROVIDER}:{detail}:{record.id}",
            ),
            recorded_via="listener",
            recorded_by=ctem_signals.SYSTEM_ACTOR,
            now=at,
        )
        events = await ctem_scope_gate_store.evaluate_after_record(db, signal, created=created)
        if events:
            pending.items.extend(events.items)
            pending.verticals |= events.verticals
            pending.frames.extend(events.frames)
        recorded += 1
    await db.flush()
    return {
        "at": at.isoformat(),
        "stale_after_days": stale_days,
        "recorded": recorded,
        **counts,
        "pending": pending,
    }


async def _redis() -> Any:
    from app.core.redis import redis_client

    return await redis_client()


async def release_pass_slot() -> None:
    """Give the daily slot back (the purge resync evaluates now)."""
    try:
        await (await _redis()).delete(_SLOT_KEY)
    except Exception:  # no Redis: there is no slot to give back
        logger.debug("CTEM inventory evaluation slot not released: Redis unavailable")


async def _due() -> bool:
    try:
        return bool(await (await _redis()).set(_SLOT_KEY, "1", nx=True, ex=_SLOT_SECONDS))
    except Exception:  # no Redis: the evaluation runs; signals dedupe on their key
        return True


async def _keep(outcome: dict[str, Any]) -> None:
    try:
        await (await _redis()).set(_LAST_KEY, json.dumps(outcome), ex=_LAST_SECONDS)
    except Exception:  # no Redis: the view says the path was never evaluated
        logger.debug("CTEM inventory evaluation outcome not kept: Redis unavailable")


async def last_outcome() -> dict[str, Any] | None:
    """The last evaluation's counts (``at``, ``evaluated``, ``unseen``, ``orphaned``, ...), or ``None``."""
    try:
        raw = await (await _redis()).get(_LAST_KEY)
    except Exception:
        return None
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


async def scheduled_pass(db: AsyncSession) -> dict[str, Any] | None:
    """The daily evaluation on the primary organization; ``None`` when not due or no organization."""
    from app.services import ctem_case_loop
    from app.services import organization_context as org_context
    from app.services.organizations import get_primary

    primary = await get_primary(db)
    if primary is None or not await _due():
        return None
    try:
        _row, profile = await org_context.current_profile(db, primary.id)
        result = await evaluate(db, organization_id=primary.id, profile=profile)
        await db.commit()
    except Exception:
        # The pass did not complete: give the slot back so the next tick evaluates again (as
        # ctem_evidence_health.scheduled_pass does), instead of a day with no evaluation.
        await release_pass_slot()
        raise
    pending = result.pop("pending", None)
    if pending is not None:
        await ctem_case_loop.publish_pending(pending)
    await _keep(result)
    return result
