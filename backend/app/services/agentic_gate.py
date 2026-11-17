# SPDX-License-Identifier: Apache-2.0

"""May agentic AI run here right now? The answer background work asks.

Nobody waits on background work - the scheduler tick, webhook, integration
event and flow triggers, channel bots, delegated tasks, queued runs and the
worker's AI jobs - so no request gate stands in front of it. Those paths used
to ask only the administrator's AI switch (``ai_state.is_ai_enabled``), never
the edition: a deployment that dropped to Community Edition (its Enterprise
Edition platform lost its license or was disconnected, the Filigran license was
removed) kept every automation configured while it was Enterprise running AI
and spending the included allowance.

:func:`agentic_refusal` is the one background gate. It means exactly what
``platform.is_agentic_ai_enabled`` means - the AI switch AND a licensed edition,
the switch asked first - and it is not a second licence rule: the licence half
IS ``platform.is_agentic_ai_licensed`` (Enterprise Edition in XTM One mode, a
valid license in Copilot mode). What it adds is the cost profile of a caller
that asks once per message, per tick or per execution:

* Cached, process-local, for :data:`CACHE_TTL_SECONDS` - the AI switch's own
  window, so an edition change reaches every replica in the time a switch flip
  does - and a miss is ONE read, however many callers arrive meanwhile (a
  burst of channel messages or worker jobs joins it). The replica serving the
  change converges at once: installing or removing the license and
  registering, unregistering or purging a platform call
  :func:`invalidate_cache` after they commit.
* Read on a session of its own, never the caller's: the caller may be a tick or
  a run mid-transaction, and resolving the edition loads the settings singleton
  and every platform registration.
* Never raises. A licence read that fails answers with the last value read - a
  licence does not change in a database blip, and refusing on one would fail
  queued work nothing is wrong with - and fails closed when none was read since
  the last :func:`invalidate_cache` (an edition change, or a platform
  registration, which is also each platform's heartbeat), the old answer being
  the one thing that may be stale. The AI switch keeps its own, stricter,
  policy.
* Says when a refusal is only the failure policy speaking.
  :func:`agentic_verdict` flags a refusal nothing was read for as
  ``transient``, so the entry points that would FAIL work on a refusal - a
  queued run, a queued delegation, a fresh evaluation - leave it for the
  orchestration engine to retry instead of recording a database blip on a cold
  worker as "not licensed".

A refusal is non-destructive by construction: callers skip, hold back or refuse
the one piece of work in front of them and never disable, pause or delete the
automation behind it, so everything resumes by itself once agentic AI is
available again. Work that had not started yet - a run, delegation or
evaluation that comes due while the refusal is definitive - is held by the
orchestration engine (``ai_hold``) and failed with the reason as its
cause only once its hold reaches ``AI_UNAVAILABLE_HOLD_MAX_SECONDS``.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import NoReturn

from fastapi import HTTPException, status
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.services import ai_state
from app.services.ai_state import AI_DISABLED_CODE, CACHE_TTL_SECONDS

logger = logging.getLogger(__name__)

__all__ = [
    "AI_DISABLED_CODE",
    "CACHE_TTL_SECONDS",
    "LICENSE_REQUIRED_CODE",
    "REFUSAL_CODES",
    "UNKNOWN_STATE_DETAIL",
    "UNKNOWN_STATE_RETRY_SECONDS",
    "AgenticVerdict",
    "agentic_refusal",
    "agentic_verdict",
    "group_verdict",
    "invalidate_cache",
    "is_licensed",
    "refusal_code",
    "refusal_is_personal",
    "refusal_message",
    "refuse_ai_disabled",
    "refuse_ai_disabled_for_group",
    "refuse_licence_required",
]

#: The refusal code for "not licensed": Community Edition in XTM One mode, no
#: valid license in Copilot mode. Carried in ``detail.code`` beside
#: ``ai_disabled``, and a ``FailureCause`` of the same value.
LICENSE_REQUIRED_CODE = "license_required"

#: Every reason this gate refuses for.
REFUSAL_CODES: frozenset[str] = frozenset({AI_DISABLED_CODE, LICENSE_REQUIRED_CODE})

#: Why an entry point did not act on a transient refusal. The orchestration
#: execution carries it while the engine retries the work, and the work itself
#: carries it should the engine give up.
UNKNOWN_STATE_DETAIL = (
    "Not started: whether agentic AI may run on this deployment could not be "
    "read from the database. The work was left as it was for the "
    "orchestration engine to retry, and nothing was charged."
)

#: The backoff of that retry. Within the engine's default attempts it rides
#: out about a minute of database trouble, and work that then starts keeps
#: its deadline's grace (``orchestration_grace_seconds``).
UNKNOWN_STATE_RETRY_SECONDS = 30.0


@dataclass(frozen=True, slots=True)
class AgenticVerdict:
    """:func:`agentic_verdict`'s answer.

    ``refusal`` is :func:`agentic_refusal`'s answer. ``transient`` marks a
    refusal that comes from the failure policy rather than from a state read
    successfully: the AI switch could not be read, or the licence was never
    read in this process, or its read failed and the last answer kept is a
    refusal.
    """

    refusal: str | None
    transient: bool = False
    #: The refusal is the person's group (``group_ai_access``), the platform
    #: switch being on: it is worded for them, never as a platform setting.
    by_group: bool = False


# (licensed, monotonic expiry). Process-local, like the AI switch's cache.
_cache: tuple[bool, float] | None = None
# The last licence answer read successfully - the fallback for a failed read -
# and the generation it was read in. A licence does not change in a database
# blip, so the fallback outlives the cache's TTL; but it does change with the
# edition, so it is only a fallback within the generation it was read in: once
# ``invalidate_cache`` announced a possible edition change (a license install or
# removal, or any platform registration - also each platform's heartbeat), a
# failed read has no answer to fall back on and refuses, transiently, until the
# edition is read again.
_last_known: bool | None = None
_last_known_generation = 0
# The read in progress, joined by every caller that misses the cache meanwhile
# (one read per miss, not one per caller), with the generation it began in. It
# answers ``(licensed, read)``: ``read`` is False for the failure fallback.
_inflight: tuple[int, asyncio.Task[tuple[bool, bool]]] | None = None
# Bumped by every invalidation: a read that began before it may carry the old
# edition, so it caches nothing and nobody joins it.
_generation = 0


def invalidate_cache() -> None:
    """Drop the cached licence answer so the next caller re-reads it.

    Call it after the commit that changes the edition: invalidating first
    would let a concurrent reader re-cache the old edition for a whole TTL.
    """
    global _cache, _generation
    _cache = None
    _generation += 1


async def _read_licence() -> bool:
    from app.api.v1.platform import is_agentic_ai_licensed
    from app.core.database import async_session
    from app.services import licence_verdict

    async with async_session() as own:
        licensed = await is_agentic_ai_licensed(own)
    if licensed:
        await licence_verdict.publish_licensed_verdict()
        return True
    # A worker without ENTERPRISE_LICENSE of its own, on a deployment whose
    # platform process holds it: see ``licence_verdict``.
    return await licence_verdict.peer_licensed_verdict()


async def _refresh(generation: int) -> tuple[bool, bool]:
    global _cache, _last_known, _last_known_generation
    try:
        licensed = await _read_licence()
    except (SQLAlchemyError, OSError):
        # Database trouble only, as in ``ai_state``: a programming error must
        # surface, not read as "unlicensed". Not cached, so the next caller
        # retries the read.
        current = _last_known_generation == _generation
        fallback = bool(_last_known) and current
        logger.warning(
            "Could not resolve the licence for background work; %s",
            "keeping the last answer read (licensed)"
            if fallback
            else "refusing agentic AI until it can be read"
            + ("" if current else " (the edition changed since the last read)"),
            exc_info=True,
        )
        return fallback, False
    if generation == _generation:
        _last_known = licensed
        _last_known_generation = generation
        _cache = (licensed, time.monotonic() + CACHE_TTL_SECONDS)
    return licensed, True


async def _licence_answer() -> tuple[bool, bool]:
    """``(licensed, read)``, cached; ``read`` is False for the failure fallback."""
    global _inflight
    cached = _cache
    if cached is not None and cached[1] > time.monotonic():
        return cached[0], True
    loop = asyncio.get_running_loop()
    flight = _inflight
    if (
        flight is None
        or flight[0] != _generation
        or flight[1].done()
        or flight[1].get_loop() is not loop
    ):
        flight = (_generation, loop.create_task(_refresh(_generation)))
        _inflight = flight
    # Shielded: a caller cancelled while waiting must not cancel the read the
    # others are waiting on.
    return await asyncio.shield(flight[1])


async def is_licensed() -> bool:
    """``platform.is_agentic_ai_licensed``, cached. Never raises."""
    licensed, _read = await _licence_answer()
    return licensed


async def agentic_verdict(db: AsyncSession) -> AgenticVerdict:
    """:func:`agentic_refusal`, and whether a refusal is only transient.

    For the entry points that would FAIL work on a refusal: they record a
    definitive refusal and defer a transient one. ``ai_state.is_ai_enabled``
    answers ``False`` to a read error and caches only what it read, so its
    ``False`` is definitive when the switch's cache holds that ``False``.

    The platform's answer only: work done for a person then asks
    :func:`group_verdict` too, when this one does not refuse.
    """
    if not await ai_state.is_ai_enabled(db):
        return AgenticVerdict(
            AI_DISABLED_CODE, transient=ai_state.cached_state() is not False
        )
    licensed, read = await _licence_answer()
    if not licensed:
        return AgenticVerdict(LICENSE_REQUIRED_CODE, transient=not read)
    return AgenticVerdict(None)


async def group_verdict(db: AsyncSession, user_id: object) -> AgenticVerdict:
    """The person's half of the gate: ``ai_disabled`` with ``by_group`` when a
    group of *user_id* switches AI off (``group_ai_access``), else no refusal.

    Asked after :func:`agentic_verdict` (or :func:`agentic_refusal`) did not
    refuse, by every entry point of work done for one person - the user an
    assignment run acts as, a flow's owner, a delegated task's user - with the
    platform switch's own code, so the work is held, recorded and worded the
    same way. A refusal here concerns that one person: a loop over several
    people's work skips theirs and goes on (``refusal_is_personal``). Kept
    apart from the platform half so the many callers and doubles of that one
    keep their shape. Read in an isolated savepoint
    (``group_ai_access.member_of_disabled_group_isolated``) and free while no
    group switches AI off; a read that fails is a transient refusal, never a
    definitive one. ``None`` asks nothing.
    """
    if user_id is None:
        return AgenticVerdict(None)
    from app.services import group_ai_access

    try:
        if await group_ai_access.member_of_disabled_group_isolated(db, user_id):
            return AgenticVerdict(AI_DISABLED_CODE, by_group=True)
    except (SQLAlchemyError, OSError):
        logger.warning(
            "Could not read the AI-disabled groups of user %s; "
            "refusing their work for now",
            user_id,
            exc_info=True,
        )
        return AgenticVerdict(AI_DISABLED_CODE, transient=True, by_group=True)
    return AgenticVerdict(None)


async def agentic_refusal(db: AsyncSession) -> str | None:
    """Why agentic AI may not run here right now, or ``None`` when it may.

    :data:`AI_DISABLED_CODE` when an administrator switched AI off - asked
    first, as the request gates do, because it is the deterministic and more
    actionable answer - then :data:`LICENSE_REQUIRED_CODE` when the deployment
    is not licensed. ``db`` serves the AI switch only (read in a savepoint, see
    ``ai_state``); the licence is read on a session of its own. A person's
    groups are :func:`group_verdict`'s.
    """
    return (await agentic_verdict(db)).refusal


def refusal_message(code: str) -> str:
    """The sentence a refusal is recorded with: the REST refusals' own words.

    The AI switch's sentence, or ``deps.licence_required_detail()`` - the two
    ways to activate Enterprise Edition (Copilot mode: the license), plus the
    trial and sales paths.
    """
    from app.api.deps import AI_DISABLED_DETAIL, licence_required_detail

    if code == AI_DISABLED_CODE:
        return AI_DISABLED_DETAIL
    return licence_required_detail()


def refuse_ai_disabled() -> NoReturn:
    """Refuse with a code a client can branch on rather than parse (FR-009).

    ``detail`` is a dict because FastAPI nests it — the repo already uses this
    shape (see ``flow_runs.py``), so clients read ``detail.code``. The message
    stays human-readable for anything that only displays it.
    """
    logger.warning("Request rejected (403): AI is disabled on this deployment.")
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail={"code": AI_DISABLED_CODE, "message": refusal_message(AI_DISABLED_CODE)},
    )


def refuse_ai_disabled_for_group() -> NoReturn:
    """:func:`refuse_ai_disabled` for a person whose group switches AI off.

    The same ``ai_disabled`` code, so every client that branches on it keeps
    working, and ``scope: "group"`` for the one that words it differently: the
    platform switch is Settings > Platform, this one is the person's group.
    """
    from app.services.group_ai_access import AI_DISABLED_BY_GROUP_MESSAGE

    logger.warning("Request rejected (403): AI is disabled for the caller's group.")
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail={
            "code": AI_DISABLED_CODE,
            "message": AI_DISABLED_BY_GROUP_MESSAGE,
            "scope": "group",
        },
    )


def refuse_licence_required() -> NoReturn:
    """The licence counterpart of :func:`refuse_ai_disabled`, same shape.

    For a service refusing work that no request asked for (``trigger_assignment``,
    reached from schedules, webhooks and provider pushes): its callers must tell
    the two reasons apart by value (:func:`refusal_code`) to answer in their own
    terms - a push receiver still owes its provider a 200. Request gates keep
    ``deps.require_agentic_ai``'s plain sentence, except ``POST /mcp/xtm``,
    which refuses here so its two refusals share one shape.
    """
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail={
            "code": LICENSE_REQUIRED_CODE,
            "message": refusal_message(LICENSE_REQUIRED_CODE),
        },
    )


def refusal_is_personal(exc: BaseException) -> bool:
    """Whether *exc* is this gate's refusal for ONE person's group.

    A loop over several people's work (the scheduled sweep, the per-user
    fan-out) stops on a platform-wide refusal - everything left would be
    refused too - but goes on past a personal one: only that person's work is
    off.
    """
    detail = getattr(exc, "detail", None)
    return isinstance(detail, dict) and detail.get("scope") == "group"


def refusal_code(exc: BaseException) -> str | None:
    """The reason when *exc* is this gate's refusal, else ``None``.

    How a caller that caught ``trigger_assignment``'s refusal - a
    change-notification receiver (Microsoft Graph, Slack, LinkedIn) that owes
    its provider a 200, a flow node, a delegation tool, the scheduler - answers
    in its own terms without asking the gate a second time. The webhook
    receivers do not catch it: their senders get the 403.
    """
    detail = getattr(exc, "detail", None)
    if isinstance(detail, dict) and detail.get("code") in REFUSAL_CODES:
        return detail["code"]
    return None
