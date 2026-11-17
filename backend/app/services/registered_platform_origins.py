# SPDX-License-Identifier: Apache-2.0

"""Origins of the platforms registered with this instance.

A platform the operator connected (OpenCTI, OpenAEV, OpenCRQ) is the one
embedder the platform already trusts, so it may frame the platform's pages
once the Content-Security-Policy is enforced (``frame-ancestors``,
``app/core/csp.py``) - enforcing never breaks an embedding of XTM One by a
connected platform. The middleware asks on every HTML document and must never
wait on the database, so the answer is a per-process snapshot of the active
registrations: primed at startup, refreshed after a registration that brings
or moves an origin, an unregistration or a purge, and re-read in the
background at most every :data:`REFRESH_SECONDS` for the registrations
another replica took.
"""

from __future__ import annotations

import asyncio
import logging
import time

from sqlalchemy import select

from app.core.csp import origin_source
from app.core.registration_token import registration_token_state

logger = logging.getLogger(__name__)

REFRESH_SECONDS = 300
REFRESH_TIMEOUT_SECONDS = 5.0
"""A read that takes longer keeps the last snapshot: the refresh is awaited
before the readiness gate and by a registration, each platform's heartbeat."""
MAX_FRAMING_ORIGINS = 20
"""Registered origins named in ``frame-ancestors``, the first in sorted order:
each lengthens the policy header of every page, and a response header larger
than a reverse proxy's buffer is refused there."""

_origins: tuple[str, ...] = ()
_refreshed_at: float | None = None
_refresh_task: asyncio.Task | None = None
_cap_warned_for: int | None = None
# Refreshes overlap (a background re-read and the one a registration starts
# after its commit): each takes a number when it starts, and a result only
# lands when no refresh started later has landed already.
_started_generation = 0
_applied_generation = 0


def registered_platforms_may_frame() -> bool:
    """Whether the registered platforms may frame the platform at all.

    Only behind a registration token the operator chose: with none, or the
    published default that authenticates nobody, anyone who can reach
    ``/platform/register`` could register an origin of their own, so a
    registration proves no trust and no registered origin may frame.
    """
    return registration_token_state() == "configured"


def platform_origin(url: object) -> str | None:
    """``scheme://host[:port]`` of a registered ``platform_url``, or ``None``.

    Only an ``http(s)`` URL with a plain host yields an origin: the value is
    written into a response header, so anything else is dropped.
    """
    return origin_source(url)


async def refresh_registered_platform_origins() -> tuple[str, ...]:
    """Re-read the active registrations on a session of its own.

    Never raises - a failure keeps the last snapshot - and never touches a
    caller's session, so a request handler may call it after its commit. A
    read that resolves after a refresh started later has landed is dropped: it
    may predate a registration change.
    """
    global _origins, _refreshed_at, _started_generation, _applied_generation
    from app.core.database import async_session
    from app.models.platform_registration import PlatformRegistration

    _started_generation += 1
    generation = _started_generation
    query = select(PlatformRegistration.platform_url).where(PlatformRegistration.status == "active")
    try:
        async with asyncio.timeout(REFRESH_TIMEOUT_SECONDS), async_session() as db:
            urls = list((await db.execute(query)).scalars().all())
    except Exception:
        logger.debug("Registered platform origins could not be refreshed", exc_info=True)
    else:
        if generation > _applied_generation:
            _applied_generation = generation
            _origins = tuple(sorted({origin for origin in map(platform_origin, urls) if origin}))
            _warn_when_capped(len(_origins))
    # Stamped on failure too, so an unreachable database is retried once per
    # interval rather than on every page.
    _refreshed_at = time.monotonic()
    return _origins


def _warn_when_capped(count: int) -> None:
    global _cap_warned_for
    if count <= MAX_FRAMING_ORIGINS or count == _cap_warned_for:
        return
    _cap_warned_for = count
    logger.warning(
        "%d platforms are registered, but only the first %d origins (sorted) may frame the platform's "
        "pages once the Content-Security-Policy is enforced: each one lengthens the policy header of "
        "every page. List the others, or a wildcard such as https://*.example.com, in "
        "CONTENT_SECURITY_POLICY_FRAME_ANCESTORS.",
        count,
        MAX_FRAMING_ORIGINS,
    )


async def refresh_for_registration(platform_url: object, *, previous_url: object = None) -> None:
    """After a registration, which is also each platform's heartbeat: re-read
    only when it brings an origin the snapshot lacks, or moves the platform
    off one the snapshot holds (``previous_url``, its stored URL before the
    registration), and only while registered platforms may frame at all -
    otherwise a heartbeat costs no query."""
    if not registered_platforms_may_frame():
        return
    origin = platform_origin(platform_url)
    left = platform_origin(previous_url)
    if (origin is not None and origin not in _origins) or (left is not None and left != origin and left in _origins):
        await refresh_registered_platform_origins()


def registered_platform_origins() -> tuple[str, ...]:
    """The snapshot, scheduling a background refresh when it is older than the interval.

    Empty unless :func:`registered_platforms_may_frame`.
    """
    global _refresh_task
    if not registered_platforms_may_frame():
        return ()
    stale = _refreshed_at is None or time.monotonic() - _refreshed_at >= REFRESH_SECONDS
    if stale and (_refresh_task is None or _refresh_task.done()):
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return _origins[:MAX_FRAMING_ORIGINS]
        _refresh_task = loop.create_task(refresh_registered_platform_origins())
    return _origins[:MAX_FRAMING_ORIGINS]
