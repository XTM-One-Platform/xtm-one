# SPDX-License-Identifier: Apache-2.0

"""Web session lifetime and idle timeout.

A web session is what a sign-in hands the browser, or XTM One Desktop: a
session JWT minted by ``core.security.create_access_token`` - a password or
TOTP sign-in, OIDC, SAML, a registration, a password reset, the Desktop
browser handoff, an impersonation. Two security options bound it
(``core/security_options.py``), both read on every verification, so a change
in Settings > Access and security applies without a restart:

* ``jwt_expire_minutes`` - the **absolute lifetime**. A session ends that long
  after its sign-in, however much it is used. It is measured from the
  ``auth_time`` claim, the moment the person signed in, which every token
  that carries a session on (a password change, an impersonation, the
  Desktop handoff) copies instead of resetting it. Lowering the value ends
  every session already older on its next request; the token's own ``exp``
  is set at sign-in from the value then in force, so raising it applies from
  the next sign-in. A token issued before ``auth_time`` existed has no ``auth_time``: its
  ``iat`` is its sign-in, and the 7-day default is the ``exp`` it already
  carries, so nobody is signed out by the upgrade.
* ``session_idle_timeout_minutes`` - the **idle timeout**, ``0`` (the default)
  for none. A session ends that long after the last sign of its user's
  activity. The activity of a session is kept in Redis
  (``session:activity:<sid>``, unix seconds, expiring with the token), keyed
  by the ``sid`` claim - shared by an impersonation and the administrator's
  session it was started from - or by a digest of a token issued before ``auth_time`` existed.
  Only an explicit signal writes it: ``POST /auth/session/activity``, which the
  web app sends when the person clicks, types, scrolls or comes back to the
  tab. Nothing else does - a request the app makes on its own (a refresh, a
  poll, the activity stream, a chat keepalive) reads the deadline and never
  moves it, so a tab left open goes idle. The signal only moves the deadline
  of the session that sends it, of a session still alive, and never past its
  absolute lifetime. A session whose activity was never recorded (signed in
  while Redis was down, or before the timeout was turned on, or a Redis that
  lost its data) starts counting at the first request that reads it.

**Redis unreachable: the idle timeout fails open.** The deadline cannot be
read, so sessions are not ended for inactivity until Redis answers; the
absolute lifetime, which the token carries, still ends them. Refusing every
session instead would sign everyone out, and refuse every sign-in, for as long
as the cache is down. After a read or write that failed or timed out, the
process leaves Redis aside for :data:`_REDIS_RETRY_SECONDS`, then lets one
request try it again, so a Redis that does not answer never holds every
request for the full timeout. A warning is logged (at most every five minutes
per process) and the Security Audit reports the idle timeout as not enforced
while Redis is unreachable.

Out of scope: personal API keys (``fcp-``), MCP keys and tokens, the
cross-platform JWTs of OpenCTI / OpenAEV / OpenCRQ, and every confined token
(``is_confined_token``): none of them is a web session.

Every surface that accepts a web session goes through :func:`verify_session`:
the REST dependency (``deps.resolve_session_jwt``, which the activity stream
and the web chat's streams call too) and the MCP OAuth consent page. An open
stream (activity, a web chat send or its events) re-checks its session every
:data:`SESSION_RECHECK_SECONDS` and ends with a ``session_expired`` event when
it ends, so the web app signs out instead of reconnecting.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import secrets
import time
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any, Literal

from app.core.utils import format_exception_message

logger = logging.getLogger(__name__)

#: When the person signed in (unix seconds): what the absolute lifetime counts from.
AUTH_TIME_CLAIM = "auth_time"
#: The session the token belongs to: what its activity is kept under.
SESSION_ID_CLAIM = "sid"

SessionEndReason = Literal["lifetime", "idle"]
REASON_LIFETIME: SessionEndReason = "lifetime"
REASON_IDLE: SessionEndReason = "idle"

#: The ``code`` of a 401 whose session ended (``lib/session.ts`` reads it), and
#: the type of the stream event that ends a stream for the same reason.
SESSION_EXPIRED_CODE = "session_expired"
#: How often an open stream checks its session again.
SESSION_RECHECK_SECONDS = 15

_ACTIVITY_KEY_PREFIX = "session:activity:"
_REDIS_TIMEOUT_SECONDS = 2.0
_REDIS_RETRY_SECONDS = 30.0
_UNENFORCED_WARNING_INTERVAL_SECONDS = 300.0
_last_unenforced_warning: float | None = None
#: Until when (``time.monotonic()``) this process leaves Redis aside; ``0`` while it answers.
_redis_unreachable_until = 0.0

_MESSAGES: Mapping[str, str] = {
    REASON_LIFETIME: "Your session has expired. Sign in again to continue.",
    REASON_IDLE: "Your session ended after a period of inactivity. Sign in again to continue.",
}


class SessionEndedError(Exception):
    """The session is over: past its absolute lifetime, or idle too long."""

    def __init__(self, reason: SessionEndReason) -> None:
        super().__init__(_MESSAGES[reason])
        self.reason: SessionEndReason = reason


def session_ended_detail(reason: SessionEndReason) -> dict[str, str]:
    """The ``detail`` of the 401 that tells the web app its session ended, and why."""
    return {"code": SESSION_EXPIRED_CODE, "reason": reason, "message": _MESSAGES[reason]}


# ---------------------------------------------------------------------------
# The settings
# ---------------------------------------------------------------------------


def lifetime_seconds() -> int:
    """The absolute session lifetime in force, in seconds."""
    from app.core.security_options import security_option

    return int(security_option("jwt_expire_minutes")) * 60


def idle_timeout_seconds() -> int:
    """The idle timeout in force, in seconds; ``0`` when there is none."""
    from app.core.security_options import security_option

    return max(0, int(security_option("session_idle_timeout_minutes"))) * 60


# ---------------------------------------------------------------------------
# The claims
# ---------------------------------------------------------------------------


def _int_claim(claims: Mapping[str, Any], name: str) -> int | None:
    value = claims.get(name)
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return int(value)


def session_started_at(claims: Mapping[str, Any]) -> int | None:
    """When the session's sign-in happened: ``auth_time``, else ``iat`` (a token issued before ``auth_time`` existed)."""
    started = _int_claim(claims, AUTH_TIME_CLAIM)
    return started if started is not None else _int_claim(claims, "iat")


def session_claims(continues: Mapping[str, Any] | None = None, *, now: int) -> dict[str, Any]:
    """The ``auth_time`` and ``sid`` of a session token about to be minted.

    *continues*: the claims of the session the token carries on - the caller's
    own on a password change, the administrator's on an impersonation, the
    confirming browser's on the Desktop handoff. Its sign-in time is kept,
    never reset, so no re-mint extends the absolute lifetime, and so is its
    ``sid`` when it has one, so the idle deadline stays the session's.
    ``None`` starts a new session: a sign-in.
    """
    started = session_started_at(continues) if continues else None
    sid = continues.get(SESSION_ID_CLAIM) if continues else None
    if not isinstance(sid, str) or not sid:
        sid = secrets.token_urlsafe(16)
    return {AUTH_TIME_CLAIM: min(started, now) if started is not None else now, SESSION_ID_CLAIM: sid}


def session_expires_at(claims: Mapping[str, Any]) -> int | None:
    """When the session's absolute lifetime ends, under the value in force now.

    The earlier of the sign-in plus the lifetime and the token's own ``exp``
    (set at sign-in): lowering the lifetime ends older sessions at once,
    raising it reaches the sessions signed in afterwards.
    """
    started = session_started_at(claims)
    expiry = _int_claim(claims, "exp")
    if started is None:
        return expiry
    end = started + lifetime_seconds()
    return min(end, expiry) if expiry is not None else end


def activity_key(claims: Mapping[str, Any], token: str) -> str:
    """Where the session's last activity is kept: its ``sid``, or a digest of a token issued before ``auth_time`` existed."""
    sid = claims.get(SESSION_ID_CLAIM)
    if isinstance(sid, str) and 0 < len(sid) <= 64:
        return f"{_ACTIVITY_KEY_PREFIX}{sid}"
    digest = hashlib.sha256(token.encode("utf-8", "surrogatepass")).hexdigest()[:32]
    return f"{_ACTIVITY_KEY_PREFIX}t:{digest}"


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class WebSession:
    """A session token the platform accepted, and its deadlines (unix seconds)."""

    claims: Mapping[str, Any]
    activity_key: str
    #: When the absolute lifetime ends; ``None`` for a token that says nothing of its sign-in.
    expires_at: int | None
    #: The idle timeout in force, in seconds; ``0`` when there is none.
    idle_timeout: int
    #: When the session ends unless its user does something; ``None`` without
    #: an idle timeout, or while it cannot be enforced.
    idle_expires_at: int | None
    #: False while Redis cannot be read: the idle timeout is not enforced then.
    idle_enforced: bool = True


def _warn_unenforced(exc: BaseException) -> None:
    global _last_unenforced_warning
    now = time.monotonic()
    if _last_unenforced_warning is not None and now - _last_unenforced_warning < _UNENFORCED_WARNING_INTERVAL_SECONDS:
        return
    _last_unenforced_warning = now
    logger.warning(
        "The session idle timeout is not enforced: Redis cannot be read (%s). Sessions are not ended "
        "for inactivity until it answers; their absolute lifetime still ends them.",
        format_exception_message(exc),
    )


def _redis_left_aside() -> bool:
    """Whether to fail open without asking Redis: it failed recently and is not due another try."""
    global _redis_unreachable_until
    if not _redis_unreachable_until:
        return False
    now = time.monotonic()
    if now < _redis_unreachable_until:
        return True
    # This request tries Redis again; the others keep leaving it aside until
    # it answers or its own timeout ends.
    _redis_unreachable_until = now + _REDIS_TIMEOUT_SECONDS
    return False


def _redis_failed() -> None:
    global _redis_unreachable_until
    _redis_unreachable_until = time.monotonic() + _REDIS_RETRY_SECONDS


def _redis_answered() -> None:
    global _redis_unreachable_until
    _redis_unreachable_until = 0.0


def _key_ttl(claims: Mapping[str, Any], now: int) -> int:
    # The activity of a session is kept as long as the session can last.
    expiry = _int_claim(claims, "exp")
    end = expiry if expiry is not None else now + lifetime_seconds()
    return max(60, end - now + 60)


async def _last_activity(key: str, claims: Mapping[str, Any], now: int) -> int:
    from app.core.redis import redis_client

    redis = await redis_client()
    raw = await redis.get(key)
    if raw is not None:
        try:
            return int(raw)
        except (TypeError, ValueError):
            pass
    # Never recorded (or unreadable): the session starts counting now. NX so
    # a concurrent first request, or an activity recorded meanwhile, wins.
    await redis.set(key, str(now), ex=_key_ttl(claims, now), nx=raw is None)
    return now


async def _verify(claims: Mapping[str, Any], key: str, now: int | None) -> WebSession:
    now = int(time.time()) if now is None else now
    expires_at = session_expires_at(claims)
    if expires_at is not None and now >= expires_at:
        raise SessionEndedError(REASON_LIFETIME)
    idle = idle_timeout_seconds()
    if idle <= 0:
        return WebSession(claims, key, expires_at, 0, None)
    if _redis_left_aside():
        return WebSession(claims, key, expires_at, idle, None, idle_enforced=False)
    try:
        last = await asyncio.wait_for(_last_activity(key, claims, now), _REDIS_TIMEOUT_SECONDS)
    except Exception as exc:
        _redis_failed()
        _warn_unenforced(exc)
        return WebSession(claims, key, expires_at, idle, None, idle_enforced=False)
    _redis_answered()
    if now >= last + idle:
        raise SessionEndedError(REASON_IDLE)
    return WebSession(claims, key, expires_at, idle, last + idle)


async def verify_session(claims: Mapping[str, Any], token: str, *, now: int | None = None) -> WebSession:
    """The deadlines of a decoded session token, or :class:`SessionEndedError` when it is over.

    Reads the idle deadline and never moves it: a request is not activity.
    The caller has already refused a confined token and checked the
    signature and ``exp``.
    """
    return await _verify(claims, activity_key(claims, token), now)


async def recheck_session(session: WebSession, *, now: int | None = None) -> WebSession:
    """:func:`verify_session` again, for a stream that outlives the request that opened it."""
    return await _verify(session.claims, session.activity_key, now)


async def record_activity(session: WebSession, *, now: int | None = None) -> WebSession:
    """The session's user did something: its idle deadline moves to now plus the idle timeout.

    Only for a session :func:`verify_session` just accepted, so an ended
    session is never revived. Recorded even without an idle timeout, so the
    deadline is right the moment an administrator turns one on.
    """
    now = int(time.time()) if now is None else now
    idle = idle_timeout_seconds()
    unrecorded = replace(session, idle_timeout=idle, idle_expires_at=None, idle_enforced=idle <= 0)
    if _redis_left_aside():
        return unrecorded
    try:
        from app.core.redis import redis_client

        redis = await redis_client()
        await asyncio.wait_for(
            redis.set(session.activity_key, str(now), ex=_key_ttl(session.claims, now)),
            _REDIS_TIMEOUT_SECONDS,
        )
    except Exception as exc:
        _redis_failed()
        if idle > 0:
            _warn_unenforced(exc)
        else:
            logger.debug("Could not record session activity: %s", format_exception_message(exc))
        return unrecorded
    _redis_answered()
    return replace(session, idle_timeout=idle, idle_expires_at=now + idle if idle > 0 else None, idle_enforced=True)
