# SPDX-License-Identifier: Apache-2.0

"""Scheduled syncs of the organization profile sections from the platforms (#11913).

A sync is an import of ``organization_context_import`` (one section, one
platform, its filters, batch type and removal policy) saved with a schedule:
hourly, daily or weekly at a time in a time zone. The assignment scheduler tick
runs the due ones (:func:`run_due_syncs`, no new daemon); section ``all`` is
the global schedule that runs every other enabled sync in order.

Rules:

- schedulable sections are the import's only (:data:`SYNCABLE_SECTIONS`):
  never a typed field such as the name, sector, regions, size or currency;
- a run reads with the token of the administrator who saved the sync; an
  owner gone, no longer an administrator, or whose platform token is missing
  pauses the sync with its reason (``paused_reason``) until it is saved again;
- a run saves a new profile version only when something changed, with the
  provenance of the import plus the sync that wrote it; rows typed by hand
  are never overwritten (the import's merge rules);
- a Redis lock per sync: two runs of one sync never overlap;
- an audit log entry for each run that changed the profile or failed;
- no model call.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.utils import format_exception_message
from app.models.organization_context import OrganizationContextSync
from app.models.user import User

logger = logging.getLogger(__name__)

#: The global schedule's section: it runs every other enabled sync in order.
ALL_SECTIONS = "all"
#: The sections a sync may keep up to date: the import's, never a typed field.
SYNCABLE_SECTIONS: tuple[str, ...] = ("assets", "asset_groups", "ai_systems", "tech_stack", "crown_jewels", "pirs")
#: Where each section's syncs are configured (#1134 S2, Sam 22 Nov 2026): the business profile
#: in Settings > Organization, the inventory in CTEM > Inventory. The global ``all`` schedule is in both.
SECTION_SCOPES: dict[str, tuple[str, ...]] = {
    "profile": ("crown_jewels", "pirs"),
    "inventory": ("assets", "asset_groups", "tech_stack", "ai_systems"),
}
#: How long a run may hold its lock before another run may take it.
LOCK_SECONDS = 900
#: Read errors that mean the owner cannot read the platform any more: the sync pauses.
_PAUSING_ERRORS = ("not connected", "no token")


class SyncRefusedError(ValueError):
    """A sync definition the API refuses (an unknown section, source or schedule)."""


#: Most source ids one sync may exclude.
MAX_EXCLUSIONS = 1000


def clean_exclusions(ids: list[str]) -> list[str]:
    """Source ids stripped, deduplicated in order, empty ones dropped, bounded (#1134)."""
    return [i for i in dict.fromkeys(str(v).strip()[:200] for v in ids) if i][:MAX_EXCLUSIONS]


def zone(name: str) -> ZoneInfo:
    """The time zone *name*, or UTC when it is unknown."""
    try:
        return ZoneInfo(name or "UTC")
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def validate(
    *,
    section: str,
    source: str,
    frequency: str,
    hour: int,
    minute: int,
    weekday: int,
    timezone: str,
    direction: str = "inbound",
) -> None:
    """Refuse a definition the scheduler could not run; nothing returned when it is valid."""
    from app.services import organization_context_import as imp
    from app.services import organization_context_outbound as outbound

    if section != ALL_SECTIONS and section not in SYNCABLE_SECTIONS:
        raise SyncRefusedError(f"section must be one of {', '.join((*SYNCABLE_SECTIONS, ALL_SECTIONS))}")
    if section != ALL_SECTIONS and source not in imp.SECTION_SOURCES[section]:
        raise SyncRefusedError(f"{source or 'no source'} cannot feed {section}")
    if direction not in outbound.SYNC_DIRECTIONS:
        raise SyncRefusedError("direction must be inbound or outbound")
    if direction == outbound.OUTBOUND and (
        section == ALL_SECTIONS or not outbound.supports(section, source, direction)
    ):
        reason = outbound.directions(section, source)[1] if section != ALL_SECTIONS else "Sync all has no direction."
        raise SyncRefusedError(f"{section} to {source or 'no source'} cannot be outbound: {reason}")
    if frequency not in ("hourly", "daily", "weekly"):
        raise SyncRefusedError("frequency must be hourly, daily or weekly")
    if not (0 <= hour <= 23 and 0 <= minute <= 59 and 0 <= weekday <= 6):
        raise SyncRefusedError("hour 0-23, minute 0-59, weekday 0 (Monday) to 6 (Sunday)")
    try:
        ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError):
        raise SyncRefusedError(f"unknown time zone {timezone!r}") from None


def define(
    sync: Any,
    *,
    owner_id: uuid.UUID,
    section: str,
    source: str = "",
    filters: dict[str, Any] | None = None,
    batch_type: str = "",
    remove_gone: bool = True,
    excluded_external_ids: list[str] | None = None,
    frequency: str = "daily",
    hour: int = 6,
    minute: int = 0,
    weekday: int = 0,
    timezone: str = "UTC",
    enabled: bool = True,
    direction: str | None = None,
) -> None:
    """Write a validated definition on *sync*, saved by *owner_id*; refused with :class:`SyncRefusedError`.

    Shared by the REST routes and the agent tools, so both save the same rows.
    Saving again is how a paused sync resumes: it then reads with this owner's token.
    *direction* omitted keeps the stored one (inbound for a new sync) and the rows an outbound run created.
    """
    stored_direction = getattr(sync, "direction", None) or "inbound"
    direction = direction or stored_direction
    all_sections = section == ALL_SECTIONS
    source = "" if all_sections else source
    validate(
        section=section,
        source=source,
        frequency=frequency,
        hour=hour,
        minute=minute,
        weekday=weekday,
        timezone=timezone,
        direction=direction,
    )
    if stored_direction != direction:
        # The rows created on the platform belong to the outbound run that made them: a new direction starts clean.
        sync.outbound_ids = {}
    elif getattr(sync, "outbound_ids", None) is None:
        sync.outbound_ids = {}
    sync.direction = direction
    sync.section = section
    sync.source = source
    sync.filters = {} if all_sections else dict(filters or {})
    sync.batch_type = "" if all_sections else (batch_type or "")[:64]
    sync.remove_gone = False if all_sections else bool(remove_gone)
    if excluded_external_ids is not None:
        sync.excluded_external_ids = [] if all_sections else clean_exclusions(excluded_external_ids)
    elif getattr(sync, "excluded_external_ids", None) is None:
        sync.excluded_external_ids = []
    sync.frequency = frequency
    sync.hour, sync.minute, sync.weekday = hour, minute, weekday
    sync.timezone = timezone
    sync.enabled = enabled
    sync.owner_user_id = owner_id
    sync.paused_reason = ""
    sync.next_run_at = next_run(sync, datetime.now(UTC)) if enabled else None


def in_scope(section: str, scope: str) -> bool:
    """Whether a sync of *section* shows in *scope* (empty: every scope)."""
    return not scope or section == ALL_SECTIONS or section in SECTION_SCOPES.get(scope, ())


async def all_syncs(db: AsyncSession, scope: str = "") -> list[OrganizationContextSync]:
    """Every sync of *scope*, the global ``all`` schedule first, then in the order they were saved."""
    rows = (
        (await db.execute(select(OrganizationContextSync).order_by(OrganizationContextSync.created_at))).scalars().all()
    )
    return sorted((r for r in rows if in_scope(r.section, scope)), key=lambda r: r.section != ALL_SECTIONS)


async def global_sync_exists(db: AsyncSession) -> bool:
    """Whether the one ``all`` schedule is saved already."""
    found = await db.execute(select(OrganizationContextSync.id).where(OrganizationContextSync.section == ALL_SECTIONS))
    return found.first() is not None


def next_run(sync: Any, after: datetime) -> datetime:
    """The first run time of *sync* strictly after *after* (UTC), in the sync's time zone."""
    tz = zone(sync.timezone)
    local = after.astimezone(tz)
    if sync.frequency == "hourly":
        candidate = local.replace(minute=sync.minute, second=0, microsecond=0)
        if candidate <= local:
            candidate += timedelta(hours=1)
        return candidate.astimezone(UTC)
    candidate = local.replace(hour=sync.hour, minute=sync.minute, second=0, microsecond=0)
    if sync.frequency == "weekly":
        candidate += timedelta(days=(sync.weekday - candidate.weekday()) % 7)
        if candidate <= local:
            candidate += timedelta(days=7)
    elif candidate <= local:
        candidate += timedelta(days=1)
    return candidate.astimezone(UTC)


def _pausing(error: str | None) -> bool:
    return bool(error) and any(marker in (error or "").lower() for marker in _PAUSING_ERRORS)


async def _owner(db: AsyncSession, sync: OrganizationContextSync) -> tuple[User | None, str]:
    """The administrator a run reads as, or the reason it cannot run."""
    if sync.owner_user_id is None:
        return None, "The administrator who saved this sync no longer exists. Save it again."
    owner = await db.get(User, sync.owner_user_id)
    if owner is None or not owner.is_active:
        return None, "The administrator who saved this sync is no longer active. Save it again."
    if not owner.is_admin:
        return None, "The administrator who saved this sync is no longer an administrator. Save it again."
    return owner, ""


async def _take_lock(sync_id: uuid.UUID) -> bool:
    """One run of a sync at a time, across workers; True when this run holds it."""
    from app.core.redis import redis_client

    try:
        redis = await redis_client()
        return bool(await redis.set(f"copilot:org_context_sync:{sync_id}", "1", nx=True, ex=LOCK_SECONDS))
    except Exception as exc:  # no Redis: the tick lock still serialises the scheduler
        logger.warning("Organization sync lock unavailable: %s", format_exception_message(exc))
        return True


async def _release_lock(sync_id: uuid.UUID) -> None:
    from app.core.redis import redis_client

    try:
        redis = await redis_client()
        await redis.delete(f"copilot:org_context_sync:{sync_id}")
    except Exception as exc:
        logger.warning("Organization sync lock release failed: %s", format_exception_message(exc))


def sync_provenance(plan: Any, sync: OrganizationContextSync, owner: User) -> dict[str, dict[str, str]]:
    """The import's provenance, naming the sync that wrote it and who saved it."""
    from app.services import organization_context_import as imp

    sources = imp.provenance(plan, sync.filters or {})
    for entry in sources.values():
        entry["basis"] = f"scheduled sync {sync.id} saved by {owner.email}; {entry['basis']}"[:500]
    return sources


async def run_sync(db: AsyncSession, sync: OrganizationContextSync, *, trigger: str) -> dict[str, Any]:
    """Run one section sync now and record its result on the row (the caller commits).

    Answers ``{added, updated, removed, version, error, skipped}``; a run that
    finds another run of the same sync in progress is ``skipped`` and writes
    nothing.
    """
    from app.services import organization_context as org_context
    from app.services import organization_context_import as imp
    from app.services.audit import log_audit

    result: dict[str, Any] = {"added": 0, "updated": 0, "removed": 0, "version": None, "error": None}
    if not await _take_lock(sync.id):
        return {**result, "skipped": "another run of this sync is in progress"}
    try:
        owner, reason = await _owner(db, sync)
        if owner is None:
            sync.paused_reason = reason
            result["error"] = reason
            return result
        if (getattr(sync, "direction", None) or "inbound") == "outbound":
            await _run_outbound(db, sync, owner, result, trigger=trigger)
            return result
        plan, _version = await imp.preview_import(
            db,
            user_id=owner.id,
            section=sync.section,
            source=sync.source,
            filters=sync.filters or {},
            batch_type=sync.batch_type,
            remove_gone=sync.remove_gone,
            excluded_ids=list(getattr(sync, "excluded_external_ids", None) or []),
            sync_id=str(sync.id),
        )
        result["added"], result["updated"] = len(plan.new), len(plan.updated)
        result["removed"] = (len(plan.gone) - len(plan.gone_kept) if plan.remove_gone else 0) + len(
            plan.excluded_removed
        )
        result["excluded"] = len(plan.excluded)
        if plan.error:
            result["error"] = plan.error[:500]
            if _pausing(plan.error):
                sync.paused_reason = plan.error[:500]
        elif plan.on_records:
            # Assets and asset groups (#1543): row writes by (source, external_id), no profile version.
            if plan.has_writes:
                result["rows_written"] = await imp.apply_record_import(
                    db, plan, actor=f"sync:{sync.id}:{owner.id}"[:120]
                )
        elif (plan.new or plan.updated or result["removed"]) and plan.profile is not None:
            saved = await org_context.save_version(
                db, plan.profile, source="draft", author_user_id=owner.id, sources=sync_provenance(plan, sync, owner)
            )
            result["version"] = saved.version
        if not plan.error and sync.section == "crown_jewels" and sync.source == "opencrq" and plan.profile is not None:
            # Supporting asset -> primary asset links (#1591), on unless the sync's filter says "false".
            from app.services import organization_context_opencrq_links as opencrq_links

            links = await opencrq_links.sync_links_for(
                db, user_id=owner.id, filters=sync.filters or {}, jewels=list(plan.profile.crown_jewels)
            )
            if links is not None:
                result["links"] = links
        if not plan.error and plan.software:
            # An endpoint source (#1251): the software follows its devices even when no device row changed.
            result["software_changed"] = await imp.store_endpoint_software(db, plan, user_id=owner.id)
        if plan.software_rows:
            result["software_rows"] = plan.software_rows
        if plan.software_error:
            # Devices synced but their software could not be read: said, never a silent zero (#1251).
            result["software_unread"] = len(plan.software_unread)
            result["software_error"] = plan.software_error[:500]
        if (
            result["version"] is not None
            or result.get("rows_written")
            or result["error"]
            or result.get("software_error")
        ):
            await log_audit(
                db,
                user=owner,
                action="trigger",
                entity_type="organization_context_sync",
                entity_id=sync.id,
                entity_name=f"{sync.section} from {sync.source}",
                summary=(
                    f"Organization sync of {sync.section} from {sync.source} ({trigger}): "
                    + (
                        f"failed: {result['error']}"
                        if result["error"]
                        else f"{result['added']} added, {result['updated']} updated, {result['removed']} removed, "
                        + (
                            f"version {result['version']}"
                            if result["version"] is not None
                            else "written to the asset records"
                        )
                        + (
                            f"; software of {result['software_unread']} device(s) unreadable: {result['software_error']}"
                            if result.get("software_error")
                            else ""
                        )
                    )
                )[:500],
                details={**result, "trigger": trigger},
            )
        return result
    finally:
        sync.last_run_at = datetime.now(UTC)
        sync.last_result = dict(result)
        await _release_lock(sync.id)


async def _run_outbound(
    db: AsyncSession, sync: OrganizationContextSync, owner: User, result: dict[str, Any], *, trigger: str
) -> None:
    """An outbound run (#1134 S3): the platform follows the profile; counts are the platform's rows."""
    from app.services import organization_context_outbound as outbound
    from app.services.audit import log_audit

    plan, ids = await outbound.run_outbound(db, sync, user_id=owner.id, apply=True)
    sync.outbound_ids = ids
    result["direction"] = "outbound"
    result["added"], result["updated"], result["removed"] = len(plan.create), len(plan.update), len(plan.delete)
    result["skipped_platform_rows"] = len(plan.skipped)
    if plan.error:
        result["error"] = plan.error[:500]
        if _pausing(plan.error):
            sync.paused_reason = plan.error[:500]
    if result["error"] or result["added"] or result["updated"] or result["removed"]:
        await log_audit(
            db,
            user=owner,
            action="trigger",
            entity_type="organization_context_sync",
            entity_id=sync.id,
            entity_name=f"{sync.section} to {sync.source}",
            summary=(
                f"Outbound organization sync of {sync.section} to {sync.source} ({trigger}): "
                + (
                    f"failed: {result['error']}"
                    if result["error"]
                    else f"{result['added']} created, {result['updated']} updated, {result['removed']} deleted"
                )
            )[:500],
            details={**result, "trigger": trigger, "plan": plan.summary()},
        )


async def section_syncs(db: AsyncSession) -> list[OrganizationContextSync]:
    """Every enabled, unpaused section sync, in the order they were saved (what ``all`` runs)."""
    return list(
        (
            await db.execute(
                select(OrganizationContextSync)
                .where(
                    OrganizationContextSync.enabled.is_(True),
                    OrganizationContextSync.paused_reason == "",
                    OrganizationContextSync.section != ALL_SECTIONS,
                )
                .order_by(OrganizationContextSync.created_at)
            )
        )
        .scalars()
        .all()
    )


async def run_now(db: AsyncSession, sync: OrganizationContextSync, *, trigger: str) -> dict[str, Any]:
    """Run *sync* now: one section, or every enabled section sync for ``all`` (the caller commits)."""
    if sync.section != ALL_SECTIONS:
        return await run_sync(db, sync, trigger=trigger)
    totals: dict[str, Any] = {"added": 0, "updated": 0, "removed": 0, "version": None, "error": None, "runs": 0}
    errors: list[str] = []
    for member in await section_syncs(db):
        outcome = await run_sync(db, member, trigger=f"{trigger}, sync all")
        if outcome.get("skipped"):
            continue
        totals["runs"] += 1
        for key in ("added", "updated", "removed"):
            totals[key] += outcome[key]
        totals["version"] = outcome["version"] or totals["version"]
        if outcome["error"]:
            errors.append(f"{member.section} from {member.source}: {outcome['error']}")
    totals["error"] = "; ".join(errors)[:500] or None
    sync.last_run_at = datetime.now(UTC)
    sync.last_result = totals
    return totals


async def run_due_syncs(db: AsyncSession, *, now: datetime | None = None) -> int:
    """Run every due sync, called by the scheduler tick; answers how many ran.

    Each sync commits on its own, so one that fails never undoes another.
    """
    now = now or datetime.now(UTC)
    due_ids = list(
        (
            await db.execute(
                select(OrganizationContextSync.id)
                .where(
                    OrganizationContextSync.enabled.is_(True),
                    OrganizationContextSync.paused_reason == "",
                    OrganizationContextSync.next_run_at.is_not(None),
                    OrganizationContextSync.next_run_at <= now,
                )
                .order_by(OrganizationContextSync.next_run_at)
            )
        )
        .scalars()
        .all()
    )
    ran = 0
    for sync_id in due_ids:
        sync = await db.get(OrganizationContextSync, sync_id)
        if sync is None:
            continue
        try:
            await run_now(db, sync, trigger="schedule")
            sync.next_run_at = next_run(sync, now)
            await db.commit()
            ran += 1
        except Exception as exc:  # one sync never stops the others
            logger.error("Organization sync %s failed: %s", sync_id, format_exception_message(exc), exc_info=True)
            await db.rollback()
            failed = await db.get(OrganizationContextSync, sync_id)
            if failed is not None:
                failed.next_run_at = next_run(failed, now)
                failed.last_run_at = now
                failed.last_result = {"error": type(exc).__name__}
                await db.commit()
    return ran
