# SPDX-License-Identifier: Apache-2.0

"""CTEM cases kept current with no user action (dev-docs/ctem-cases.md section 4.3).

Cases are inferred from the source products, so nobody presses a button to
see them move. Two paths run the source recompute
(``ctem_cases_source.recompute_for_user``, unchanged: merge only, a narrower
view never closes or shrinks a case, an outage writes nothing):

- the scheduled pass, on the autonomous tick, once per
  ``cases_recompute.interval_minutes`` of the organization profile (default
  15, minimum 5): with the identity that owns the CTEM loop, the run-as users
  of the CTEM loop fleet's automations, else each user who opened Cases in the
  last ``VIEWER_DAYS`` days, each with their own stored credentials;
- the page open: a viewer whose last recompute is older than the interval gets
  one in the background with their own credentials (the same one-per-minute
  slot as the route), and the list re-reads it in place.

Every recompute writes its freshness (when it ran, the products that did not
answer) so the list shows "Updated 4 min ago" and, after an outage, a muted
line instead of a toast. Rules only, no model call.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.utils import format_exception_message

logger = logging.getLogger(__name__)

#: A viewer of Cases is a recompute identity of the scheduled pass for this many days.
VIEWER_DAYS = 14
#: Most identities one scheduled pass recomputes with (the newest viewers first).
MAX_IDENTITIES = 20
#: The interval when the profile sets none.
DEFAULT_INTERVAL_MINUTES = 15
MIN_INTERVAL_MINUTES = 5

_PASS_SLOT = "ctem:cases:recompute-pass:{vertical}"  # noqa: S105 (a Redis key, not a secret)
_VIEWERS = "ctem:cases:viewers:{vertical}"
_FRESHNESS = "ctem:cases:freshness:{vertical}"
_VIEWER_LAST = "ctem:cases:viewer-recompute:{user_id}:{vertical}"

#: Background recomputes started on page open, kept referenced until they end.
_running: set[asyncio.Task[Any]] = set()


async def _redis() -> Any:
    from app.core.redis import redis_client

    return await redis_client()


async def interval_minutes(db: AsyncSession) -> int:
    """The profile's ``cases_recompute.interval_minutes`` (at least five), else the default."""
    from app.services import organization_context as org_context
    from app.services.organizations import get_primary

    try:
        primary = await get_primary(db)
        if primary is None:
            return DEFAULT_INTERVAL_MINUTES
        row = await org_context.current_version(db, primary.id)
        profile = org_context.parse_profile(row.profile if row else {})
        return max(MIN_INTERVAL_MINUTES, int(profile.cases_recompute.interval_minutes))
    except Exception as exc:  # an unreadable profile keeps the default cadence
        logger.info("CTEM cases refresh: interval unreadable: %s", format_exception_message(exc))
        return DEFAULT_INTERVAL_MINUTES


# ── who the scheduled pass reads as ─────────────────────────────────────


async def loop_fleet_run_as(db: AsyncSession) -> list[str]:
    """The run-as users of the enabled automations of the CTEM loop fleet's members."""
    from app.agents.builtin_fleets import CTEM_LOOP_SLUG
    from app.models.assignment import Assignment
    from app.models.fleet import Fleet, FleetMember

    rows = await db.execute(
        select(Assignment.run_as_user_id)
        .join(FleetMember, FleetMember.agent_id == Assignment.agent_id)
        .join(Fleet, Fleet.id == FleetMember.fleet_id)
        .where(
            Fleet.slug == CTEM_LOOP_SLUG,
            Assignment.enabled.is_(True),
            Assignment.run_as_user_id.is_not(None),
        )
    )
    return list(dict.fromkeys(str(user_id) for user_id in rows.scalars().all() if user_id))


async def note_viewer(user_id: str, vertical: str, now: float | None = None) -> None:
    """Remember that *user_id* opened Cases (the fallback identities of the scheduled pass)."""
    try:
        client = await _redis()
        key = _VIEWERS.format(vertical=vertical)
        stamp = now if now is not None else time.time()
        await client.zadd(key, {str(user_id): stamp})
        await client.zremrangebyscore(key, 0, stamp - VIEWER_DAYS * 86400)
    except Exception:  # no Redis: the scheduled pass falls back to the fleet's run-as only
        logger.debug("CTEM cases refresh: viewer not noted, Redis unavailable")


async def recent_viewers(vertical: str, now: float | None = None) -> list[str]:
    """The users who opened Cases in the last ``VIEWER_DAYS`` days, newest first."""
    try:
        client = await _redis()
        stamp = now if now is not None else time.time()
        found = await client.zrevrangebyscore(_VIEWERS.format(vertical=vertical), stamp, stamp - VIEWER_DAYS * 86400)
        return [v.decode() if isinstance(v, bytes) else str(v) for v in found]
    except Exception:
        return []


async def pass_identities(db: AsyncSession, vertical: str) -> list[str]:
    """The fleet's run-as users when set, else the recent viewers; at most ``MAX_IDENTITIES``."""
    owners = await loop_fleet_run_as(db)
    return (owners or await recent_viewers(vertical))[:MAX_IDENTITIES]


# ── freshness ────────────────────────────────────────────────────────────


async def record_freshness(vertical: str, answer: dict[str, Any], at: datetime) -> None:
    """Keep when the cases were last recomputed and, after an outage, which product did not answer."""
    try:
        client = await _redis()
        key = _FRESHNESS.format(vertical=vertical)
        raw = await client.get(key)
        current = json.loads(raw) if raw else {}
        if answer.get("applied"):
            current["updated_at"] = at.isoformat()
            current["missing"] = []
            current["missing_at"] = None
        else:
            current["missing"] = list(answer.get("missing") or [])
            current["missing_at"] = at.isoformat()
        await client.set(key, json.dumps(current))
    except Exception:
        logger.debug("CTEM cases refresh: freshness not recorded, Redis unavailable")


async def freshness(vertical: str) -> dict[str, Any]:
    """``{updated_at, missing, missing_at}``: the last applied recompute and the last outage after it."""
    empty: dict[str, Any] = {"updated_at": None, "missing": [], "missing_at": None}
    try:
        client = await _redis()
        raw = await client.get(_FRESHNESS.format(vertical=vertical))
        return {**empty, **(json.loads(raw) if raw else {})}
    except Exception:
        return empty


# ── the two paths ────────────────────────────────────────────────────────


async def recompute_as(db: AsyncSession, *, organization_id: uuid.UUID, user_id: str, vertical: str) -> dict[str, Any]:
    """One source recompute as *user_id*, its freshness and the viewer's stamp recorded."""
    from app.services import ctem_cases_source as source

    now = datetime.now(UTC)
    answer = await source.recompute_for_user(db, organization_id=organization_id, user_id=user_id, vertical=vertical)
    await record_freshness(vertical, answer, now)
    try:
        client = await _redis()
        await client.set(_VIEWER_LAST.format(user_id=user_id, vertical=vertical), str(now.timestamp()))
    except Exception:
        logger.debug("CTEM cases refresh: viewer stamp not kept, Redis unavailable")
    return answer


async def pass_due(vertical: str, minutes: int) -> bool:
    """Take the pass slot for this interval; True when this tick runs it (or Redis is unavailable)."""
    try:
        client = await _redis()
        return bool(await client.set(_PASS_SLOT.format(vertical=vertical), "1", nx=True, ex=minutes * 60))
    except Exception:  # no Redis: the pass runs; recompute is merge-only and idempotent
        return True


async def scheduled_pass(db: AsyncSession, *, vertical: str = "cyber") -> dict[str, Any] | None:
    """The tick's recompute of the shared cases; ``None`` when not due or not XTM One."""
    from app.core.config import settings
    from app.services.organizations import get_primary

    if settings.is_copilot_like:
        return None
    primary = await get_primary(db)
    if primary is None:
        return None
    if not await pass_due(vertical, await interval_minutes(db)):
        return None
    counts: dict[str, Any] = {"identities": 0, "applied": 0, "skipped": 0}
    for user_id in await pass_identities(db, vertical):
        counts["identities"] += 1
        try:
            answer = await recompute_as(db, organization_id=primary.id, user_id=user_id, vertical=vertical)
        except Exception as exc:  # one identity failing never stops the others
            await db.rollback()
            logger.warning("CTEM cases scheduled recompute as %s failed: %s", user_id, format_exception_message(exc))
            counts["skipped"] += 1
            continue
        counts["applied" if answer.get("applied") else "skipped"] += 1
    return counts


async def viewer_due(user_id: str, vertical: str, minutes: int, now: float | None = None) -> bool:
    """Whether the viewer's last recompute is older than the interval (or never happened)."""
    try:
        client = await _redis()
        raw = await client.get(_VIEWER_LAST.format(user_id=user_id, vertical=vertical))
    except Exception:
        return False  # no Redis: no stamp to trust, the scheduled pass keeps the cases current
    if raw is None:
        return True
    stamp = now if now is not None else time.time()
    try:
        return stamp - float(raw) >= minutes * 60
    except ValueError:
        return True


async def _refresh_in_background(organization_id: uuid.UUID, user_id: str, vertical: str) -> None:
    from app.core.database import async_session
    from app.services.ctem_loop_stream import case_frame, publish_case_updates

    try:
        async with async_session() as db:
            answer = await recompute_as(db, organization_id=organization_id, user_id=user_id, vertical=vertical)
        if answer.get("applied"):
            # The open list re-reads at once on the frame, in place (no toast, no spinner).
            now = datetime.now(UTC).isoformat()
            await publish_case_updates([case_frame("", "", "recomputed", now)])
    except Exception as exc:
        logger.warning("CTEM cases refresh on open as %s failed: %s", user_id, format_exception_message(exc))


async def refresh_on_open(db: AsyncSession, *, organization_id: uuid.UUID, user_id: str, vertical: str) -> bool:
    """Start a background recompute for a viewer whose last one is older than the interval; True when started."""
    from app.api.v1.ctem_cases import recompute_slot

    await note_viewer(user_id, vertical)
    if not await viewer_due(user_id, vertical, await interval_minutes(db)):
        return False
    if not await recompute_slot(user_id, vertical):
        return False
    task = asyncio.create_task(_refresh_in_background(organization_id, user_id, vertical))
    _running.add(task)
    task.add_done_callback(_running.discard)
    return True
