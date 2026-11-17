# SPDX-License-Identifier: Apache-2.0

"""Shared helpers for loading the PlatformSettings singleton.

Every service/route that previously read platform-wide fields from
``GeneralAssistantConfig`` should import ``get_platform_settings`` from
here instead.

The singleton's primary key doubles as the platform's anonymous telemetry
``instance_id``.  It can be forced via the ``PLATFORM_ID`` env var
(``settings.platform_id``) for identity continuity across clones/restores,
mirroring OpenCTI's ``PLATFORM_ID`` and OpenAEV's ``OPENAEV_INSTANCEID``:

* fresh install  -> the row is created with the forced UUID;
* existing install -> ``reconcile_platform_id`` (called once at startup)
  re-keys the row to the forced UUID.
"""

from __future__ import annotations

import logging
import uuid

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.platform_settings import PlatformSettings

logger = logging.getLogger(__name__)


def configured_platform_id() -> uuid.UUID | None:
    """Return the forced platform instance ID from ``PLATFORM_ID``, if set.

    The value is already validated/normalized by the ``Settings`` field
    validator, so parsing here can only fail if ``settings.platform_id`` is
    mutated at runtime with a malformed value (tests) — let that raise.
    """
    raw = (settings.platform_id or "").strip()
    if not raw:
        return None
    return uuid.UUID(raw)


async def peek_platform_settings(db: AsyncSession) -> PlatformSettings | None:
    """Read-only load of the PlatformSettings singleton (``None`` when absent).

    Unlike :func:`get_platform_settings`, this NEVER creates the row, so it is
    safe on paths that must stay side-effect free: the periodic telemetry
    refresh (which shares the scheduler's session), the per-request Terms of
    Use gate, and other read paths where an INSERT+FLUSH would be a surprising
    write. Callers treat ``None`` the same as "field not set" (a fresh install
    that has not bootstrapped the singleton yet has no acceptance either way).
    """
    result = await db.execute(
        select(PlatformSettings)
        .order_by(PlatformSettings.created_at.asc(), PlatformSettings.id.asc())
        .limit(1)
    )
    return result.scalar_one_or_none()


_TRUTHY = {"1", "true", "yes", "on"}
_FALSY = {"0", "false", "no", "off"}


def _seed_tool_approvals() -> dict:
    """The approval kill switch's value for a row being created.

    Returned as kwargs so the call site reads as what it is: a seed applied at
    creation, never a rule consulted later. An empty dict leaves the column's
    own default (approvals on), which is also where an unrecognised value
    lands — a typo in a deployment variable must not quietly disable an
    approval control (FR-023's fail-closed reading, FR-044).
    """
    configured = (settings.tool_approvals_enabled or "").strip().lower()
    if configured in _FALSY:
        return {"tool_approvals_enabled": False}
    if configured and configured not in _TRUTHY:
        logger.warning(
            "Unrecognised TOOL_APPROVALS_ENABLED %r - leaving tool approvals on",
            configured,
        )
    return {}


async def get_platform_settings(db: AsyncSession) -> PlatformSettings:
    """Load the PlatformSettings singleton, creating it if absent.

    ``platform_name = NULL`` intentionally means "use the mode-aware runtime
    default" so switching ``PLATFORM_MODE`` does not leave a stale product name
    in the singleton row.

    When ``PLATFORM_ID`` is configured, a newly created row uses it as its
    primary key so the anonymous telemetry ``instance_id`` is deterministic
    from the very first boot (no transient random ID).

    A newly created row also seeds the approval kill switch from
    ``TOOL_APPROVALS_ENABLED`` (FR-044). This path reaches a **fresh install**
    only; an upgraded deployment already has the row, and its one-time seed is
    applied by the ``i1seed3311`` migration instead — where the schema arrives,
    which is the only other moment that happens exactly once. Between them the
    variable is read once on any install and ignored thereafter.

    A unique index on ``platform_settings`` (see the
    ``n2s3g4l5e6t7`` migration) enforces the singleton at the DB level, so
    two requests racing this check-then-insert on a fresh install can no
    longer both succeed — the loser's ``flush`` raises ``IntegrityError``
    and falls back to reading the row the winner created.  The insert runs
    inside a SAVEPOINT (``begin_nested``) so only the failed INSERT is
    rolled back: many callers invoke this helper mid-transaction with
    their own uncommitted work in the session, and a full ``rollback()``
    would silently discard it.
    """
    singleton_query = (
        select(PlatformSettings)
        .order_by(PlatformSettings.created_at.asc(), PlatformSettings.id.asc())
        .limit(1)
    )
    result = await db.execute(singleton_query)
    ps = result.scalar_one_or_none()
    if ps is None:
        forced_id = configured_platform_id()
        seed = _seed_tool_approvals()
        ps = (
            PlatformSettings(id=forced_id, **seed)
            if forced_id
            else PlatformSettings(**seed)
        )
        try:
            async with db.begin_nested():
                db.add(ps)
                await db.flush()
        except IntegrityError:
            result = await db.execute(singleton_query)
            ps = result.scalar_one()
    return ps


async def reconcile_platform_id(db: AsyncSession) -> None:
    """Force the singleton's ID to the configured ``PLATFORM_ID`` (startup).

    No-op when ``PLATFORM_ID`` is unset or already matches.  Otherwise the
    existing row is re-keyed in place (plain PK update — nothing references
    ``platform_settings.id`` via FK), preserving every other column including
    ``created_at``.  Mirrors OpenCTI's ``patchPlatformId`` which re-indexes
    the settings document under the new ID on every boot.

    Must run under the startup ``bootstrap_lock`` (single writer) before
    anything else loads/creates the singleton.
    """
    forced_id = configured_platform_id()
    if forced_id is None:
        return

    ps = await get_platform_settings(db)
    if ps.id == forced_id:
        return

    logger.warning(
        "Switching platform instance ID from [%s] to [%s] (PLATFORM_ID)",
        ps.id,
        forced_id,
    )
    await db.execute(
        update(PlatformSettings)
        .where(PlatformSettings.id == ps.id)
        .values(id=forced_id)
        # Skip ORM session synchronization: it cannot remap a changed primary
        # key anyway, and the stale instance is expunged right below (same
        # convention as the other bulk UPDATEs in main.py / mcp_oauth.py).
        .execution_options(synchronize_session=False)
    )
    # The loaded ORM instance is still registered in the session's identity
    # map under the OLD primary key — a bulk UPDATE does not remap it, so a
    # later expired-attribute refresh would SELECT by the old PK, find no
    # row, and raise ObjectDeletedError.  Expunge it so any subsequent read
    # in this session loads the row cleanly under the new PK.
    db.expunge(ps)
