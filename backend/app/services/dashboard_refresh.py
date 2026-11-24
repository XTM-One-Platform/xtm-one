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

"""Data sources refreshed on the server, whoever has the page open (``refreshMode`` ``server`` / ``manual``).

A source's ``refreshMode`` says where it is read (``dashboard_definition.REFRESH_MODES``):

* unset (``live``): by each viewer's page, with their own access, every
  ``refresh`` while it is open - as before;
* on the server, whoever has the page open: ``server`` every ``refresh`` (5
  minutes at least), ``schedule`` on a ``cron`` (UTC, 5 minutes apart at least),
  ``event`` when an event of its ``on`` happens (:func:`mark_event`: a platform
  event, or an event an integration raises; read once per minute at most however
  many arrive), ``manual`` when someone presses Refresh now (or an agent asks).
  Each is also read once when it is saved. Viewers are shown the snapshot
  (``CustomDashboardSourceSnapshot``).

**Whose access** (decision D1 of ``dev-docs/dashboard-studio-next.md``): a
snapshot is read with the access of its publisher, the person who last set
what the source reads, as a published link is read with its publisher's. A
save that changes such a source (:func:`sync_snapshots`) makes its writer the
publisher and reads it again at once; a save that leaves it as it was keeps
both. The publisher must still be able to open the dashboard: otherwise the
snapshot reads ``access_denied`` until someone who can sets the source again.
A page under a published link's marking ceiling never reads a snapshot (it
reads the source with the link's own rules).

**The read** (:func:`refresh_due`): the scheduler takes the due rows ``FOR
UPDATE SKIP LOCKED``, leases them, reads each source as its publisher
(``dashboard_sources.read_source``, the platform's answer cache skipped),
stores what it read and tells the open pages (:func:`notify_updated`). No
model is ever called: a source that needs one (an ``ask``) is not refreshed on
the server, so the AI gate does not apply.
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.custom_dashboard import CustomDashboard, CustomDashboardSourceSnapshot

logger = logging.getLogger(__name__)

#: How long the scheduler holds a row it is reading before another worker may take it.
LEASE = timedelta(minutes=5)
#: When a ``manual`` source is due: only when someone asks.
NEVER = datetime(9999, 1, 1, tzinfo=UTC)
#: Rows read per run of the scheduler.
BATCH = 20
#: An event source is read at most this often, however many events arrive (60 an hour at most).
EVENT_COALESCE = timedelta(seconds=60)
#: Rows an event marks due at once.
MAX_MARKED = 200


def _mode(source: object) -> str | None:
    from app.services.dashboard_definition import SERVER_REFRESH_MODES

    options = source.get("options") if isinstance(source, dict) and isinstance(source.get("options"), dict) else {}
    mode = options.get("refreshMode")
    return mode if mode in SERVER_REFRESH_MODES else None


def trigger_of(source: dict) -> str | None:
    """The event an ``event`` source is read on, as stored on its snapshot (``trigger``); ``None`` otherwise."""
    if _mode(source) != "event":
        return None
    on = source["options"].get("on") or {}
    if on.get("platformEvent"):
        return f"platform:{on['platformEvent']}"
    return f"integration:{on.get('integrationEvent')}:{on.get('integrationId') or '*'}"


def _key(source: dict) -> str:
    return hashlib.sha256(json.dumps(source, sort_keys=True, default=str).encode()).hexdigest()


def _next_due(source: dict, now: datetime) -> datetime:
    from app.services.dashboard_definition import refresh_seconds

    mode = _mode(source)
    if mode == "server":
        return now + timedelta(seconds=refresh_seconds(source["options"].get("refresh")) or 900)
    if mode == "schedule":
        from croniter import croniter

        return croniter(source["options"]["cron"], now).get_next(datetime)
    return NEVER


async def sync_snapshots(db: AsyncSession, dashboard: CustomDashboard, definition: dict, writer_id: uuid.UUID | None) -> None:
    """After a save: one row per source refreshed on the server, a changed one read again by its writer."""
    sources = {sid: s for sid, s in (definition.get("dataSources") or {}).items() if _mode(s)}
    rows = {
        row.source_id: row
        for row in (
            await db.execute(select(CustomDashboardSourceSnapshot).where(CustomDashboardSourceSnapshot.dashboard_id == dashboard.id))
        ).scalars()
    }
    gone = [sid for sid in rows if sid not in sources]
    if gone:
        await db.execute(
            delete(CustomDashboardSourceSnapshot).where(
                CustomDashboardSourceSnapshot.dashboard_id == dashboard.id, CustomDashboardSourceSnapshot.source_id.in_(gone)
            )
        )
    now = datetime.now(UTC)
    for sid, source in sources.items():
        key = _key(source)
        row = rows.get(sid)
        if row is not None and row.source_key == key:
            continue
        publisher = writer_id or (row.publisher_id if row is not None else None) or dashboard.user_id
        if row is None:
            db.add(
                CustomDashboardSourceSnapshot(
                    dashboard_id=dashboard.id, source_id=sid, publisher_id=publisher, source_key=key, due_at=now, trigger=trigger_of(source)
                )
            )
        else:
            row.publisher_id, row.source_key, row.due_at, row.trigger = publisher, key, now, trigger_of(source)


async def mark_event(db_factory, triggers: list[str]) -> int:
    """An event happened: the ``event`` sources read on it are due - now, or a minute after their last read.

    *triggers* are the event's spellings (:func:`trigger_of`: a platform event,
    or an integration event with its integration's id and with ``*``). How many
    sources were marked; never raises.
    """
    if not triggers:
        return 0
    now = datetime.now(UTC)
    try:
        async with db_factory() as db:
            rows = (
                await db.execute(
                    select(CustomDashboardSourceSnapshot)
                    .where(CustomDashboardSourceSnapshot.trigger.in_(triggers), CustomDashboardSourceSnapshot.due_at > now)
                    .limit(MAX_MARKED)
                    .with_for_update(skip_locked=True)
                )
            ).scalars().all()
            for row in rows:
                soonest = row.refreshed_at + EVENT_COALESCE if row.refreshed_at else now
                row.due_at = min(row.due_at, max(now, soonest))
            await db.commit()
            return len(rows)
    except Exception as exc:
        logger.warning("Marking dashboard sources due on %s failed: %r", triggers[:2], str(exc)[:200])
        return 0


async def hear_integration_event(trigger_context: dict) -> None:
    """An integration event reached the platform (an assignment's poll or webhook): its ``event`` sources are due."""
    from app.core.database import async_session
    from app.core.feature_flags import is_feature_enabled

    key = trigger_context.get("event") or trigger_context.get("event_key")
    if not key or not is_feature_enabled("DASHBOARD_STUDIO"):
        return
    integration = trigger_context.get("integration_id")
    triggers = [f"integration:{key}:*"] + ([f"integration:{key}:{integration}"] if integration else [])
    await mark_event(async_session, triggers)


async def hear_platform_event(name: str, payload: dict) -> None:
    """A platform event happened: its ``event`` sources are due; a completed run's answer reaches the open pages of
    the dashboards reading its assignment. Never raises."""
    from app.core.database import async_session
    from app.core.feature_flags import is_feature_enabled

    if not is_feature_enabled("DASHBOARD_STUDIO"):
        return
    await mark_event(async_session, [f"platform:{name}"])
    if name == "run.completed" and payload.get("assignment_id") and not payload.get("parent_run_id"):
        try:
            for dashboard_id, source_id in await _reading_assignment(async_session, str(payload["assignment_id"])):
                await notify_updated(dashboard_id, source_id)
        except Exception as exc:
            logger.warning("Telling the dashboards of assignment %s failed: %r", payload.get("assignment_id"), str(exc)[:200])


#: Dashboards a completed run's answer is pushed to, at most.
MAX_PUSHED_DASHBOARDS = 50


async def _reading_assignment(db_factory, assignment_id: str) -> list[tuple[uuid.UUID, str]]:
    """The ``(dashboard, source)`` pairs reading *assignment_id*'s answers."""
    from sqlalchemy import Text, cast

    from app.services.dashboard_definition import upgrade

    async with db_factory() as db:
        rows = (
            await db.execute(
                select(CustomDashboard.id, CustomDashboard.spec)
                .where(cast(CustomDashboard.spec, Text).like(f"%{assignment_id}%"))
                .limit(MAX_PUSHED_DASHBOARDS)
            )
        ).all()
    found = []
    for dashboard_id, spec in rows:
        for sid, source in (upgrade(spec).get("dataSources") or {}).items():
            if source.get("type") == "assignment" and str((source.get("options") or {}).get("assignment_id")) == assignment_id:
                found.append((dashboard_id, sid))
    return found


async def snapshot_of(db: AsyncSession, dashboard_id: object, source_id: str, source: dict) -> dict | None:
    """What a viewer is shown of a source refreshed on the server: its last read, or ``None`` for a live source."""
    from app.models.user import User

    mode = _mode(source)
    if mode is None:
        return None
    try:
        dashboard_uuid = uuid.UUID(str(dashboard_id))
    except ValueError:
        return None
    row = (
        await db.execute(
            select(CustomDashboardSourceSnapshot).where(
                CustomDashboardSourceSnapshot.dashboard_id == dashboard_uuid, CustomDashboardSourceSnapshot.source_id == source_id
            )
        )
    ).scalar_one_or_none()
    publisher = await db.get(User, row.publisher_id) if row is not None and row.publisher_id else None
    about = {
        "mode": mode,
        "every": source["options"].get("refresh") if mode == "server" else source["options"].get("cron") if mode == "schedule" else None,
        "refreshed_at": row.refreshed_at.isoformat() if row is not None and row.refreshed_at else None,
        "publisher": (publisher.full_name or publisher.email) if publisher is not None else None,
    }
    if row is None or row.result is None:
        # Saved, not read yet: the scheduler reads it on its next run.
        return {"status": "error", "code": "running", "snapshot": about}
    return {**row.result, "snapshot": about}


async def _read_one(db: AsyncSession, row: CustomDashboardSourceSnapshot) -> tuple[dict | None, dict | None]:
    """What *row*'s source reads now as its publisher: ``(result, source)``; ``(None, None)`` when it is gone."""
    from app.services.custom_dashboards import load_dashboard_for
    from app.services.dashboard_definition import upgrade
    from app.services.dashboard_sources import read_source

    dashboard = await db.get(CustomDashboard, row.dashboard_id)
    if dashboard is None:
        return None, None
    definition = upgrade(dashboard.spec)
    source = (definition.get("dataSources") or {}).get(row.source_id)
    if not _mode(source) or _key(source) != row.source_key:  # type: ignore[arg-type]
        return None, None  # changed since: the save that changed it set the row again
    if row.publisher_id is None or await load_dashboard_for(db, dashboard.id, user_id=row.publisher_id) is None:
        return {"status": "error", "code": "access_denied"}, source
    return await read_source(db, row.publisher_id, definition, row.source_id, fresh=True, use_snapshots=False), source


async def refresh_due(db_factory, *, now: datetime | None = None) -> int:
    """Read the sources that are due, each as its publisher; how many were read. One scheduler run."""
    now = now or datetime.now(UTC)
    async with db_factory() as db:
        rows = (
            await db.execute(
                select(CustomDashboardSourceSnapshot)
                .where(CustomDashboardSourceSnapshot.due_at <= now)
                .order_by(CustomDashboardSourceSnapshot.due_at)
                .limit(BATCH)
                .with_for_update(skip_locked=True)
            )
        ).scalars().all()
        taken = [(row.id, row.dashboard_id, row.source_id) for row in rows]
        for row in rows:
            row.due_at = now + LEASE
        await db.commit()
    done = 0
    for row_id, dashboard_id, source_id in taken:
        if await refresh_row(db_factory, row_id):
            done += 1
        else:
            logger.info("Dashboard source %s/%s was not read (changed or gone since it was due)", dashboard_id, source_id)
    return done


async def refresh_row(db_factory, row_id: uuid.UUID) -> bool:
    """Read one snapshot's source as its publisher now, store it and tell the open pages; whether it was read."""
    try:
        async with db_factory() as db:
            row = await db.get(CustomDashboardSourceSnapshot, row_id)
            if row is None:
                return False
            result, source = await _read_one(db, row)
            if source is None:
                return False
            read_at = datetime.now(UTC)
            row.result, row.refreshed_at, row.due_at = result, read_at, _next_due(source, read_at)
            dashboard_id, source_id = row.dashboard_id, row.source_id
            await db.commit()
    except Exception as exc:
        logger.warning("Refreshing dashboard source %s failed: %r", row_id, str(exc)[:200])
        return False
    await notify_updated(dashboard_id, source_id)
    return True


async def snapshot_row_id(db: AsyncSession, dashboard_id: uuid.UUID, source_id: str) -> uuid.UUID | None:
    """The snapshot row of a source refreshed on the server, ``None`` for a live one."""
    return (
        await db.execute(
            select(CustomDashboardSourceSnapshot.id).where(
                CustomDashboardSourceSnapshot.dashboard_id == dashboard_id, CustomDashboardSourceSnapshot.source_id == source_id
            )
        )
    ).scalar_one_or_none()


async def notify_updated(dashboard_id: object, source_id: str) -> None:
    """Tell the open pages of the dashboard that a source has a new snapshot."""
    from app.services.dashboard_refresh_events import publish_source_updated

    await publish_source_updated(str(dashboard_id), source_id)


__all__ = [
    "hear_integration_event",
    "hear_platform_event",
    "mark_event",
    "refresh_due",
    "refresh_row",
    "snapshot_of",
    "snapshot_row_id",
    "sync_snapshots",
    "trigger_of",
]
