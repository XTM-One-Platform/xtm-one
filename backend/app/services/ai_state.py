# SPDX-License-Identifier: Apache-2.0

"""Is AI enabled on this platform?

The only module that reads ``platform_mode`` to answer that question. Everything
else calls in here, so a second reader is a defect findable by grep (FR-004).

Resolution: an explicit administrator choice wins, otherwise follow
``PLATFORM_MODE``. The stored column is negative (``ai_disabled``) to match the
mode name; the wire is positive (``ai_enabled``) in both directions and the
inversion happens here, once.
"""

from __future__ import annotations

import logging
import time

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.platform_settings import PlatformSettings

logger = logging.getLogger(__name__)

AI_DISABLED_MODE = "ai_disabled"

#: How long a resolved answer is reused before re-reading the singleton.
#:
#: The gate runs on ~83 route call sites, and ``require_chat_enabled`` is tested
#: against a DB that raises on any access — the request path is expected to do
#: no database work. A per-request read would break both. Five seconds bounds
#: staleness per replica to less than an administrator notices while flipping a
#: switch by hand, without a cross-replica invalidation.
CACHE_TTL_SECONDS = 5.0

# (value, monotonic expiry). Process-local: each replica converges within one
# TTL of a change, which is why no pub/sub is needed.
_cache: tuple[bool, float] | None = None

#: Returned in the ``code`` field of every refusal, so clients branch on a value
#: rather than parsing prose (FR-009).
AI_DISABLED_CODE = "ai_disabled"


def ai_enabled_by_mode() -> bool:
    """What ``PLATFORM_MODE`` alone says. No database, never raises."""
    return settings.platform_mode != AI_DISABLED_MODE


def override_to_wire(stored: bool | None) -> bool | None:
    """The stored ``ai_disabled`` column as the ``ai_enabled`` clients see.

    With :func:`override_from_wire`, the only two places the polarity flips.
    ``None`` stays ``None``: "follow ``PLATFORM_MODE``" is a third state, not a
    ``False`` (``contracts/ai-state-api.md``).
    """
    return None if stored is None else not stored


def override_from_wire(enabled: bool | None) -> bool | None:
    """The ``ai_enabled`` a client sent as the stored ``ai_disabled`` column."""
    return None if enabled is None else not enabled


async def _stored_override(db: AsyncSession) -> bool | None:
    """The administrator's choice, or ``None`` for "follow the mode".

    Read-only on purpose: ``get_platform_settings`` creates the row when absent,
    and this runs on every gated request and every scheduler tick.
    """
    result = await db.execute(
        select(PlatformSettings.ai_disabled)
        .order_by(PlatformSettings.created_at.asc(), PlatformSettings.id.asc())
        .limit(1)
    )
    return result.scalar_one_or_none()


def resolve(enabled: bool | None) -> bool:
    """Collapse a wire tri-state into the single boolean a gate needs.

    An explicit choice wins; ``None`` follows ``PLATFORM_MODE`` (FR-003).
    Pure, so a caller holding a value it just wrote can resolve it without a
    second read.
    """
    return ai_enabled_by_mode() if enabled is None else enabled


def _resolve(stored: bool | None) -> bool:
    """As :func:`resolve`, from the stored column polarity."""
    return resolve(override_to_wire(stored))


def prime_cache(value: bool) -> None:
    """Seed the cache with an answer resolved elsewhere — boot does this.

    Without it the first request after startup pays the read, so the
    "no database work in the gate" invariant would only hold from the second
    request onwards.
    """
    global _cache
    _cache = (value, time.monotonic() + CACHE_TTL_SECONDS)


# The last value that was read successfully, for :func:`ai_enabled_for_display`.
# Deliberately not cleared by ``invalidate_cache``: the cache exists to stop a
# stale answer being *gated on*, while this exists to stop a blip being
# *reported*, and those want opposite lifetimes.
_last_known: bool | None = None


def invalidate_cache() -> None:
    """Drop the cached answer. The runtime toggle calls this so the replica that
    served the change reflects it immediately; the others follow within one TTL.
    """
    global _cache
    _cache = None


# Whether the last read of the switch by :func:`is_ai_enabled` failed. Cleared by
# the next read that succeeds. For a caller that reports rather than gates (the
# A2A pre-flight): a ``False`` answered right after a failed read is "could not
# be read", not "switched off".
_last_read_failed = False


def last_read_failed() -> bool:
    """Whether :func:`is_ai_enabled`'s last read of the switch failed.

    Process-local and best effort: another caller's successful read clears it,
    and the caller then reports the generic refusal, as before.
    """
    return _last_read_failed


def cached_state() -> bool | None:
    """The answer :func:`is_ai_enabled` holds from a read, still within its TTL,
    or ``None`` when it would have to read again.

    For a caller that must tell "switched off" from "could not be read":
    :func:`is_ai_enabled` answers ``False`` to both and caches only the first
    (``agentic_gate.agentic_verdict``).
    """
    cached = _cache
    if cached is not None and cached[1] > time.monotonic():
        return cached[0]
    return None


async def _read_override_isolated(db: AsyncSession) -> bool | None:
    """Read the stored override without touching the caller's ORM state.

    A **Core** savepoint (``connection().begin_nested()``), not the session's.
    ``Session.begin_nested`` looks like the natural choice and is the wrong one:
    ``SessionTransaction._take_snapshot`` calls ``session.flush()`` for a nested
    transaction, so every cache miss flushed the caller's pending entities — and
    if the read then failed, ``_restore_snapshot(dirty_only=True)`` expired their
    modified instances, so the next attribute access raised ``MissingGreenlet``
    instead of the graceful "gate as disabled" promised here. Measured, not
    assumed: a pending row went 1 -> 0 with the session savepoint and stayed at 1
    with this one (/code-review, 2026-08-28).

    ``no_autoflush`` matters as much as the savepoint: ``session.execute`` would
    otherwise autoflush the same pending state before the SELECT.

    What the savepoint is *for* survives — Postgres aborts the whole transaction
    on a failed SELECT, and a Core savepoint absorbs that, so the caller's next
    statement still works (verified the same way).
    """
    connection = getattr(db, "connection", None)
    no_autoflush = getattr(db, "no_autoflush", None)
    if connection is None or no_autoflush is None:
        # A minimal session-like object, which several callers' tests pass. Read
        # plainly rather than let an AttributeError escape a *gate*, where it
        # would surface as a 500.
        return await _stored_override(db)
    conn = await connection()
    with no_autoflush:
        async with conn.begin_nested():
            return await _stored_override(db)


async def is_ai_enabled(db: AsyncSession) -> bool:
    """Resolve for **gating a request**. Never raises; unreadable means disabled.

    Cached for :data:`CACHE_TTL_SECONDS`, so the steady-state request path does
    no database work at all.

    Fail-closed per Constitution II: refusing when the answer is unknown is
    recoverable, whereas assuming "enabled" runs a model on a deployment that
    may forbid it by policy.
    """
    global _cache, _last_read_failed
    if _cache is not None and _cache[1] > time.monotonic():
        return _cache[0]

    try:
        # Inside a SAVEPOINT, so a failed read rolls back only this read.
        # Postgres aborts the whole transaction on a failed SELECT: without the
        # savepoint the caller's next statement fails with
        # InFailedSQLTransaction and blames whatever runs next (the autonomous
        # loop reported it as a token-refresh error). A plain ``db.rollback()``
        # fixed that but threw away the caller's staged work, which is wrong
        # for the mid-request callers — ``is_chat_enabled``,
        # ``is_agentic_ai_enabled`` — that ask this in the middle of a handler.
        stored = await _read_override_isolated(db)
    except (SQLAlchemyError, OSError):
        # Database trouble only. A bare ``except Exception`` here swallowed a
        # programming error and reported it as "AI disabled" — a bug in this
        # query would have silently disabled AI platform-wide instead of
        # surfacing.
        logger.warning(
            "Could not resolve the AI state; gating as disabled", exc_info=True
        )
        # Deliberately not cached: a blip must not keep AI off for a whole TTL
        # after the database recovers.
        _last_read_failed = True
        return False

    global _last_known
    _last_read_failed = False
    resolved = _resolve(stored)
    _last_known = resolved
    _cache = (resolved, time.monotonic() + CACHE_TTL_SECONDS)
    return resolved


async def is_ai_enabled_strict(db: AsyncSession) -> bool:
    """Resolve for **boot and provisioning**. Raises if it cannot be read.

    The opposite failure policy to :func:`is_ai_enabled`, and deliberately so.
    Failing closed here would let a transient database blip skip seeding on a
    normal platform and leave it silently empty — an outcome far worse than the
    startup error that makes the real problem visible.
    """
    global _last_known

    stored = await _stored_override(db)
    resolved = _resolve(stored)
    # ``_last_known`` too, not just the cache. ``ai_enabled_for_display`` falls
    # back to it when a read fails and answers **True** when it has never been
    # set — so on a fresh ``ai_disabled`` boot, where this is the first resolver
    # to run, one failed read made every client render as AI-enabled: nav
    # unlocked, no badge, the normal dashboard instead of the landing screen.
    # The exact inversion that fallback exists to prevent (/code-review,
    # 2026-08-28).
    _last_known = resolved
    prime_cache(resolved)
    return resolved


async def ai_enabled_for_display(db: AsyncSession) -> bool:
    """Resolve for **telling a client**, not for gating.

    Same answer as :func:`is_ai_enabled` whenever the state can be read. The
    difference is the failure policy: a gate must fail closed, but *reporting*
    closed is a lie that the whole interface acts on. ``/platform/config`` is
    unauthenticated and polled on every page load, so one transient
    ``SQLAlchemyError`` there made every client render the AI-disabled landing,
    the "AI off" badge and locked navigation on a perfectly healthy platform —
    the exact flash ``useAiEnabled()``'s optimistic default exists to avoid.

    So a failed read reports the last value successfully resolved, and only
    falls back to "enabled" when there has never been one. Gating still refuses
    in that window: ``is_ai_enabled`` is unchanged, so no AI runs while the
    database is unreadable — the platform refuses the work without announcing
    itself as switched off.
    """
    global _last_known
    try:
        # Inside a SAVEPOINT, exactly as ``is_ai_enabled`` does, and for the
        # same reason: Postgres aborts the whole transaction on a failed SELECT.
        # Without it this function survived the blip and then the caller's next
        # statement raised InFailedSqlTransaction — so the unauthenticated
        # /platform/config answered 500, which is the very case this exists for.
        stored = await _read_override_isolated(db)
    except (SQLAlchemyError, OSError):
        logger.warning(
            "Could not resolve the AI state for display; reporting the last "
            "known value (%r)",
            _last_known,
            exc_info=True,
        )
        return True if _last_known is None else _last_known

    resolved = _resolve(stored)
    _last_known = resolved
    prime_cache(resolved)
    return resolved


def content_mode() -> str:
    """Which built-in content set to create.

    The ``seed_builtin_*`` functions seed per platform mode, most of their
    content in ``xtm_one`` and ``dev`` only, so passing ``ai_disabled`` straight
    through would make them no-ops — and an administrator who later turns AI on
    would get an empty platform. An
    ai_disabled deployment is an XTM One platform with AI switched off, so it
    seeds the xtm_one set. Passed explicitly rather than read from settings by
    the seeders, so they stay ignorant of this feature.
    """
    if settings.platform_mode == "ai_disabled":
        return "xtm_one"
    return settings.platform_mode


async def set_ai_enabled(db: AsyncSession, enabled: bool | None) -> bool | None:
    """Record an administrator's choice. Returns the previous wire value.

    ``None`` clears the override, so the platform follows ``PLATFORM_MODE``
    again — not the same operation as ``False`` (``contracts/ai-state-api.md``).

    Takes a row lock rather than writing through the caller's already-loaded
    instance: two administrators saving at once must converge on one value by
    construction (FR-026), not because Postgres happened to order the UPDATEs.
    The caller commits, then calls :func:`invalidate_cache` — invalidating
    first would let a concurrent request re-cache the pre-commit value for a
    whole TTL.
    """
    from app.api.v1.platform_settings_helpers import get_platform_settings

    await get_platform_settings(db)  # the singleton must exist to be locked
    ps = (
        await db.execute(
            select(PlatformSettings)
            .order_by(PlatformSettings.created_at.asc(), PlatformSettings.id.asc())
            .limit(1)
            .with_for_update()
            # Belt on the lock: the waiter must read the row as it is *after*
            # the winner commits, not the copy its session loaded earlier in
            # the request. SQLAlchemy happens to refresh under ``FOR UPDATE``
            # already, so this states the requirement rather than supplying
            # it — ``previous`` ends up in the audit trail, and a value the
            # platform had already moved off is exactly what someone
            # reconstructing a race must not be told.
            #
            # Safe here only because autoflush is on: the same request may have
            # edited branding or language on this row already, and
            # ``populate_existing`` overwrites loaded attributes — without the
            # flush those pending edits would be silently discarded.
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    previous = override_to_wire(ps.ai_disabled)
    ps.ai_disabled = override_from_wire(enabled)
    await db.flush()
    return previous
