# SPDX-License-Identifier: Apache-2.0

"""Quota enforcement service – four-tier (license / platform / group / user) quotas.

Provides rate-limiting for:
  - **LLM proxy requests** (per user, optionally per model)
  - **Agentic executions** (per user, and platform-wide - a run with no
    account behind it counts against the platform-wide tiers alone)

Quota resolution order: user > group (most permissive) > global > Filigran license.
When in XTM One mode, the Filigran license quotas act as the platform-level
fallback for agentic executions.  If no Filigran license is present but an EE
platform is registered, a hardcoded daily quota applies.

Usage counters are held in Redis for fast check/increment and periodically
flushed to the ``quota_usage`` PostgreSQL table for durability, the
platform-wide ones to ``quota_global_usage``, read back when Redis lost them.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import secrets
import uuid
from collections.abc import AsyncIterable, Iterable
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import Any

from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.redis import decr_if_positive, redis_client
from app.core.utils import format_exception_message
from app.models.group import Group, GroupMember
from app.models.platform_settings import PlatformSettings
from app.models.user import User

logger = logging.getLogger(__name__)

# Redis key prefixes
_USAGE_PREFIX = "quota:usage:"
_CONFIG_CACHE_PREFIX = "quota:config:"
# Usage type of the per-(user, agent, window) counter of failed runs already
# forgiven by the agent failure pool. It rides the ordinary ``quota:usage:``
# key space ON PURPOSE: the pool's window is a YEAR, far longer than any Redis
# instance is guaranteed to keep a key, and ``flush_quota_usage`` already turns
# every key under that prefix into a durable ``quota_usage`` row with a
# monotonic ``max()`` upsert — exactly the durability a year-long counter
# needs, for free. Every read path filters on an explicit usage type
# (``_QUOTA_TYPES``, the monitoring scans), so this one is invisible to them
# and can never be mistaken for action-quota usage.
_POOL_USAGE_TYPE = "agentic_failure_pool"
# Companion counter for the OTHER kind of credit-back: a run that never reached
# a model at all (provider unreachable, down, or not configured — see
# ``llm.router.is_provider_unavailable_error``). Those are credited back FREE,
# without spending a pool slot, because there is nothing to bound: the platform
# failed to deliver, and refusing to forgive it would charge users for outages
# they cannot see. This counter exists so "free" is still *visible* — the admin
# card reports the two reasons separately — and rides the same durable key
# space for the same reason the pool counter does.
_FREE_REFUND_USAGE_TYPE = "agentic_free_refund"

# Metering receipts. A run's action is charged INSIDE the agent turn, but some
# runs are failed from a different process entirely — the orchestration engine
# gives up on a job whose worker died mid-turn, and nothing in that turn will
# ever run again. Assignment runs solve this with a flag on the run row, written
# by the engine itself around the charge. A sub-agent task cannot: the charge
# happens inside ``run_agent_chat``, which knows nothing about the task.
#
# So the charge writes its own receipt, keyed by whatever work item the caller
# names (``sub_agent_task:<id>``). A receipt exists if and only if an action was
# actually charged — the abandonment path can therefore credit back without
# GUESSING that a charge happened, which is the difference between correct and
# nearly-correct here: crediting a charge that never occurred takes an action
# from somebody else's run in the same period.
#
# Redis, not Postgres: the hot path already writes counters there, so this costs
# one script call; and losing receipts to a Redis wipe fails SAFE (no credit,
# exactly today's behaviour) rather than granting free actions.
#
# A receipt names ONE attempt's charge (``sub_agent_task:<id>:<attempt>``):
# a re-run after a lost lease charges under its own token, so the lost
# attempt's charge stays creditable. Whoever credits a charge redeems its
# receipt first, and a redeemed receipt is not deleted but overwritten by
# ``_RECEIPT_REDEEMED`` for the rest of its lifetime (``_REDEEM_RECEIPT_LUA``):
# the dead-letter, the re-run that replaces a lost attempt and that attempt's
# own failure handler - still alive after a heartbeat outage - then all see it
# redeemed, so each charge is credited at most once. Redeeming a token nothing
# was charged under yet closes it too, and a charge landing under a closed
# token is given straight back (``_WRITE_RECEIPT_LUA``): a stale attempt that
# only reaches its charge after its replacement started runs uncharged.
_RECEIPT_PREFIX = "quota:receipt:"
# Abandonment is detected within minutes (the reconciler's deadline sweep), so
# a week is already absurd headroom. It is not a lifetime: an unredeemed receipt
# is meant to expire.
_RECEIPT_TTL = 86_400 * 7
# What a redeemed receipt holds until it expires. Not JSON, so a release that
# still redeems with ``GETDEL`` reads it as a receipt it cannot parse and
# credits nothing.
_RECEIPT_REDEEMED = "redeemed"

# The charge writes its receipt, unless the token was already closed: then
# nothing is written and the caller gives the charge back (0).
_WRITE_RECEIPT_LUA = """
if redis.call('GET', KEYS[1]) == ARGV[3] then
  return 0
end
redis.call('SET', KEYS[1], ARGV[1], 'EX', ARGV[2])
return 1
"""

# Redeem a receipt: {1, receipt} when this call took it, {2} when it was
# already redeemed, {0} when none was written - the token is closed either way.
_REDEEM_RECEIPT_LUA = """
local receipt = redis.call('GET', KEYS[1])
if receipt == ARGV[1] then
  return {2}
end
if receipt then
  redis.call('SET', KEYS[1], ARGV[1], 'KEEPTTL')
  return {1, receipt}
end
redis.call('SET', KEYS[1], ARGV[1], 'EX', ARGV[2])
return {0}
"""
RECEIPT_ABSENT = 0
RECEIPT_TAKEN = 1
RECEIPT_ALREADY_REDEEMED = 2

# Credit back, at most once, a charge that has no receipt written at charge
# time - an assignment run's, recorded on its row: KEYS[1] is its
# settlement receipt, KEYS[2..] the counters the charge added to. One script,
# so the receipt and the counters move together or not at all: 2 when the
# receipt already reads credited (nothing moves), otherwise every counter is
# decremented by one, floored at zero (a missing or empty counter is left
# alone), and the receipt is set to credited with its expiry, 1.
_CREDIT_CHARGE_ONCE_LUA = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return 2
end
for i = 2, #KEYS do
  local current = tonumber(redis.call('GET', KEYS[i]) or '0')
  if current and current > 0 then
    redis.call('DECR', KEYS[i])
  end
end
redis.call('SET', KEYS[1], ARGV[1], 'EX', ARGV[2])
return 1
"""
# What a settlement receipt holds once its charge was credited back.
_CHARGE_CREDITED = "credited"
# What :func:`credit_agentic_charge_once` answers.
CHARGE_CREDIT_NOT_APPLIED = 0
CHARGE_CREDIT_LANDED = 1
CHARGE_CREDIT_ALREADY = 2

# Multiple of the pool size at which a run of free credits stops looking like
# an incident and starts looking like an unattended one. Alerting only — see
# ``_apply_free_refund``.
_FREE_CREDIT_ALERT_FACTOR = 100

# TTLs
_CONFIG_CACHE_TTL = 300  # 5 minutes
_USAGE_KEY_TTL = 86_400 * 35  # 35 days – covers monthly + buffer
# The failure pool's window is a full year (a license anniversary), so its
# counter has to outlive every usage bucket. The extra ~5 weeks cover a
# window whose anniversary has passed but whose row has not been read back.
_POOL_KEY_TTL = 86_400 * 400  # ~13 months

# The display counter of the composer's quota panel: agentic plus sandbox
# actions of one user, counted in every period at once (today, this month, this
# year), whatever period the user's own limit is set in. The enforced counters
# (``agentic``, ``sandbox``) live in the resolved period only, so they cannot
# say how much of today or of this year is spent. A usage type of its own, so
# the admin views that sum ``agentic`` rows by period prefix never count it.
_VIEW_USAGE_TYPE = "agentic_view"
#: The periods the quota panel shows, in its order.
QUOTA_VIEW_PERIODS = ("daily", "monthly", "yearly")
# A yearly bucket must outlive a month without a charge.
_VIEW_KEY_TTLS = {
    "daily": 86_400 * 2,
    "monthly": 86_400 * 35,
    "yearly": 86_400 * 400,
}

# Outcomes of a failure credit-back, reported through the ``outcome_out`` dict
# the refund helpers accept (the ``quota_out`` pattern of
# ``agent_chat.enforce_agentic_quota_ex``). The boolean they return only says
# whether an action came back; a caller writing a trace has to tell "the pool
# is spent, the charge stands" apart from "there was never anything to give
# back", which are opposite readings of the same ``False``.
REFUND_OUTCOME_CREDITED = "credited"
REFUND_OUTCOME_POOL_EXHAUSTED = "pool_exhausted"
REFUND_OUTCOME_POOL_DISABLED = "pool_disabled"
# The user's agentic quota is unlimited at every tier: the action the
# run spent is a counter nobody enforces, so the pool - a ration on refunds -
# is not consumed and nothing is credited back. ``has_enforceable_agentic_limit``
# is the predicate.
REFUND_OUTCOME_QUOTA_UNLIMITED = "quota_unlimited"
# No account behind the run: the pool is a per-user allowance, so a
# failure of an account-less run keeps its platform-wide charge.
REFUND_OUTCOME_NO_ACCOUNT = "no_account"
REFUND_OUTCOME_FAILED = "refund_failed"


def _report_refund_outcome(outcome_out: dict | None, outcome: str) -> None:
    if outcome_out is not None:
        outcome_out["outcome"] = outcome


# User-facing refusal shown when the quota backend is unreachable and quotas
# fail closed (``quota_fail_closed``). Shared by all three enforcement sites
# (agent chat, general chat, assignment engine) so the wording cannot drift.
QUOTA_UNAVAILABLE_MESSAGE = (
    "Unable to verify your usage quota because the quota backend is "
    "currently unavailable. Please try again later or contact your "
    "administrator."
)

# User-facing refusal for an exceeded agentic quota, for callers that don't
# already have their own wording (e.g. the sandbox-dispatch call sites, spec
# 002-sandbox-quota-usage-tracking FR-018) — same text as agent_chat.py's
# local QUOTA_EXCEEDED_MESSAGE, kept as a separate constant here rather than
# imported from agent_chat.py to avoid a service-layer -> chat-layer
# dependency for a plain string.
QUOTA_EXCEEDED_MESSAGE = (
    "You have reached your agentic execution quota for the current period. "
    "Please contact your administrator or wait for the quota to reset."
)

# The same two refusals for a run with no account behind it: an
# anonymous channel sender, a public widget visitor, an MCP client on the
# platform key. There is no "your quota" to speak of and no administrator the
# reader can reach, so they say the service is at capacity or unavailable -
# never the quota, the licence or the store behind it.
ACCOUNTLESS_QUOTA_EXCEEDED_MESSAGE = (
    "This service is at capacity right now. Please try again later."
)
ACCOUNTLESS_QUOTA_UNAVAILABLE_MESSAGE = (
    "This service is temporarily unavailable. Please try again later."
)


def quota_fail_closed() -> bool:
    """True when the quota fail mode is ``closed`` (refuse execution when the
    quota backend cannot be reached, instead of proceeding unmetered).
    Always in XTM One mode, where quotas are what the licence sells;
    in Copilot / dev mode Settings > Access and security, or ``QUOTA_FAIL_MODE``, which
    wins."""
    from app.core.security_options import security_option

    return security_option("quota_fail_mode") == "closed"


# Where the quota store is probed for a write; never a usage counter,
# so the usage flush and scans never read it.
_STORE_PROBE_KEY = "quota:store_probe"


def is_quota_store_outage(exc: BaseException) -> bool:
    """True when *exc* says the quota store cannot be reached or written - a
    refused, reset or timed-out connection, a store still loading or failing
    over (``LOADING``, ``MASTERDOWN``, ``CLUSTERDOWN``, ``TRYAGAIN``), a
    replica refusing writes, a store out of memory - rather than a fault of
    the quota code or its data. The one quota failure a due background run
    waits out instead of failing: a fault would hold every run for the
    whole bound. A store refusing the platform's credentials or permissions
    (a wrong password, ``NOAUTH``, an ACL denial) is a configuration error,
    refused at once like a credential an LLM provider rejects
    (``llm.router.is_provider_unavailable_error``). The built-in socket
    errors are only the connection and timeout ones: any other ``OSError`` (a
    file, a permission) comes from the rest of the check, never the store."""
    from redis.exceptions import (
        AuthenticationError,
        AuthenticationWrongNumberOfArgsError,
        AuthorizationError,
        ClusterDownError,
        ExternalAuthProviderError,
        NoPermissionError,
        OutOfMemoryError,
        ReadOnlyError,
        TryAgainError,
    )
    from redis.exceptions import ConnectionError as RedisConnectionError
    from redis.exceptions import TimeoutError as RedisTimeoutError

    if isinstance(
        exc,
        (
            AuthenticationError,
            AuthenticationWrongNumberOfArgsError,
            AuthorizationError,
            ExternalAuthProviderError,
            NoPermissionError,
        ),
    ):
        return False
    return isinstance(
        exc,
        (
            RedisConnectionError,
            RedisTimeoutError,
            ReadOnlyError,
            OutOfMemoryError,
            ClusterDownError,
            TryAgainError,
            ConnectionError,
            TimeoutError,
        ),
    )


async def quota_store_outage() -> BaseException | None:
    """The outage a write to the quota store meets
    (:func:`is_quota_store_outage`), or ``None`` when the store takes it - or
    fails for another reason, which the quota code meets and reports itself.
    A write, not a ping: a replica refusing writes or a store out of memory
    still answers the reads of a quota check. Never raises."""
    try:
        redis = await redis_client()
        await redis.set(_STORE_PROBE_KEY, "1", ex=60)
    except Exception as exc:
        return exc if is_quota_store_outage(exc) else None
    return None


async def quota_store_out() -> bool:
    """Whether the quota store is out (:func:`quota_store_outage`). What a
    hold on the store asks before its work starts again; never
    raises."""
    return await quota_store_outage() is not None


def agent_failure_pool_size() -> int:
    """Refunded failures allowed per (user, agent, quota period).

    ``0`` means the feature is off — either because
    ``AGENT_FAILURE_QUOTA_REFUND_ENABLED`` is false, or because the pool
    itself is sized at (or below) zero. Read on every call rather than
    captured at import time so the two settings stay a single source of
    truth for both the refund path and anything that reports on it.
    """
    if not settings.agent_failure_quota_refund_enabled:
        return 0
    return max(0, settings.agent_failure_quota_pool)


class QuotaExceededError(Exception):
    """Raised when a user exceeds their quota."""

    def __init__(
        self,
        quota_type: str,
        limit: int,
        current: int,
        period: str,
        model: str | None = None,
        note: str | None = None,
    ):
        self.quota_type = quota_type
        self.limit = limit
        self.current = current
        self.period = period
        self.model = model
        detail = f"{quota_type} quota exceeded"
        if model:
            detail += f" for model {model}"
        detail += f": {current}/{limit} ({period})"
        if note:
            detail += f" — {note}"
        super().__init__(detail)


#: What an admin reads when a quota reset or a support-code recharge could not
#: take the flush lock in time (:class:`QuotaClearBusyError`).
QUOTA_CLEAR_BUSY_MESSAGE = (
    "Quota usage is being saved right now, so nothing was changed. Try again "
    "in a few seconds."
)


class QuotaClearBusyError(Exception):
    """A quota reset or a pool recharge could not take the flush lock in time.

    Raised before anything is cleared: going ahead without the lock let
    the flush holding it write back counters read before the clear.
    """


# ---------------------------------------------------------------------------
# Period helpers
# ---------------------------------------------------------------------------


def _utcnow() -> datetime:
    """The UTC wall clock every quota period is read from (one seam: the tests freeze it)."""
    return datetime.now(UTC)


def _current_period_key(period: str, now: datetime | None = None) -> str:
    """Return the period key (``2026``, ``2026-02``, or ``2026-02-18``) of ``now``, the current instant by default."""
    now = now or _utcnow()
    if period == "daily":
        return now.strftime("%Y-%m-%d")
    if period == "yearly":
        return now.strftime("%Y")
    return now.strftime("%Y-%m")


def seconds_until_period_reset(period: str, now: datetime | None = None) -> int:
    """Seconds until the current UTC period resets (for Retry-After and counter TTLs), at least 60."""
    now = now or _utcnow()
    if period == "daily":
        tomorrow = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
        return max(int((tomorrow - now).total_seconds()), 60)
    if period == "yearly":
        next_year = now.replace(
            year=now.year + 1,
            month=1,
            day=1,
            hour=0,
            minute=0,
            second=0,
            microsecond=0,
        )
        return max(int((next_year - now).total_seconds()), 60)
    next_month = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if now.month == 12:
        next_month = next_month.replace(year=now.year + 1, month=1)
    else:
        next_month = next_month.replace(month=now.month + 1)
    return max(int((next_month - now).total_seconds()), 60)


# ---------------------------------------------------------------------------
# Redis key builders
# ---------------------------------------------------------------------------


_GLOBAL_USER = "_global"


def _usage_key(
    user_id: str | uuid.UUID,
    usage_type: str,
    model: str | None,
    period_key: str,
    agent_id: str | uuid.UUID | None = None,
) -> str:
    model_seg = model or "_total"
    agent_seg = str(agent_id) if agent_id is not None else "_all"
    return f"{_USAGE_PREFIX}{user_id}:{usage_type}:{model_seg}:{period_key}:{agent_seg}"


def _legacy_usage_key(
    user_id: str | uuid.UUID,
    usage_type: str,
    model: str | None,
    period_key: str,
) -> str:
    """Pre-agent-attribution 4-segment key format.

    Kept only for the rolling-deploy transition window: counters written by
    an older replica must keep being enforced and must not be lost. Reads sum
    both shapes (see ``_get_user_count``) and increments fold the legacy key
    into its 5-segment successor (see ``_merge_legacy_usage_key``), so legacy
    keys disappear organically; the 35-day key TTL bounds the window.
    """
    model_seg = model or "_total"
    return f"{_USAGE_PREFIX}{user_id}:{usage_type}:{model_seg}:{period_key}"


def _global_usage_key(usage_type: str, period_key: str) -> str:
    return f"{_USAGE_PREFIX}{_GLOBAL_USER}:{usage_type}:_total:{period_key}"


def _failure_pool_key(
    user_id: str | uuid.UUID,
    agent_id: str | uuid.UUID,
    window_key: str,
) -> str:
    """Key of the failure-pool counter for one user/agent/window triple.

    Scoped per *user* as well as per agent because the quota being refunded
    is the user's: a company-managed agent is shared, and one user's failed
    runs must not eat another user's pool. Built with ``_usage_key`` so the
    counter is shaped like every other usage counter and is picked up by
    ``flush_quota_usage`` (see ``_POOL_USAGE_TYPE``).
    """
    return _usage_key(user_id, _POOL_USAGE_TYPE, None, window_key, agent_id)


def _free_refund_key(
    user_id: str | uuid.UUID,
    agent_id: str | uuid.UUID,
    window_key: str,
) -> str:
    """Key of the free (provider-outage) credit-back tally for one triple.

    Same shape and window as :func:`_failure_pool_key` so the admin card can
    show the two reasons side by side, but this one is a tally, not an
    allowance: nothing is ever refused because it is high.
    """
    return _usage_key(user_id, _FREE_REFUND_USAGE_TYPE, None, window_key, agent_id)


#: Redis hashes holding, per credit-back kind, pool window and user, how many
#: credits each :class:`~app.services.failure_causes.FailureCause` accounted
#: for. A HASH rather than a key per cause: one ``HINCRBY`` to write,
#: and a user's whole breakdown expires as a unit with its window.
#:
#: Deliberately OUTSIDE ``_USAGE_PREFIX``, and this is load-bearing rather than
#: cosmetic: ``flush_quota_usage`` scans ``{_USAGE_PREFIX}*`` and pipelines a
#: ``GET`` over every key it finds. ``GET`` on a hash is a ``WRONGTYPE`` error,
#: and the pipeline raises on the first one — so a single cause hash living in
#: that keyspace would break the flush for EVERY counter, silently stopping all
#: quota persistence. Nothing under ``quota:usage:`` may be anything but a
#: string. ``test_the_cause_hash_never_breaks_the_quota_flush`` pins this.
_REFUND_CAUSE_PREFIX = "quota:refund_causes:"

# How many cause hashes one pipelined read or one ``DEL`` covers. There is one
# hash per user credited back in a window, so a single command over all of them
# would grow with the install.
_CAUSE_KEY_BATCH = 500


#: Epoch of the credit-back tallies, per user and platform-wide. A
#: quota reset advances the user's once it has deleted their counters, a
#: support-code recharge the platform-wide one once it has swept every pool
#: counter. A credit reads the epochs in the same script as its counter
#: increment and tallies its cause under them, and the card only reads the
#: current epochs' tallies: a credit whose counter an admin action deleted
#: therefore tallies where nothing reads, however its tally interleaves with
#: that action. Outside ``_USAGE_PREFIX`` like the tallies (the flush GETs only
#: counters there).
_REFUND_EPOCH_PREFIX = "quota:refund_epoch:"
_GLOBAL_REFUND_EPOCH = "_global"


def _refund_epoch_key(scope: str | uuid.UUID) -> str:
    """Epoch key of one user, or of the platform (``_GLOBAL_REFUND_EPOCH``)."""
    return f"{_REFUND_EPOCH_PREFIX}{scope}"


def _cause_epoch_token(usage_type: str, user_epoch: int, global_epoch: int) -> str:
    """The epochs a tally is filed under, as its key suffix.

    Empty while nothing was ever reset: the key is then the per-user key,
    so tallies written before epochs existed stay readable. The
    free-credit kind ignores the platform epoch - a recharge never touches free
    credits.
    """
    if usage_type == _POOL_USAGE_TYPE:
        return f"{user_epoch}.{global_epoch}" if user_epoch or global_epoch else ""
    return str(user_epoch) if user_epoch else ""


def _cause_key_epochs(key: str) -> tuple[str, int, int] | None:
    """``(usage_type, user epoch, platform epoch)`` of a per-user tally key.

    ``None`` for the platform-wide shape (no user) and for anything unreadable.
    """
    parts = key.removeprefix(_REFUND_CAUSE_PREFIX).split(":")
    if len(parts) not in (3, 4):
        return None
    usage_type, token = parts[0], parts[3] if len(parts) == 4 else ""
    try:
        if not token:
            return usage_type, 0, 0
        if usage_type == _POOL_USAGE_TYPE:
            user_epoch, global_epoch = token.split(".")
            return usage_type, int(user_epoch), int(global_epoch)
        return usage_type, int(token), 0
    except ValueError:
        return None


# One script, so an epoch is never left without an expiry by a failure between
# the increment and the EXPIRE.
_ADVANCE_REFUND_EPOCH_LUA = """
local epoch = redis.call('INCR', KEYS[1])
redis.call('EXPIRE', KEYS[1], ARGV[1])
return epoch
"""


async def _advance_refund_epoch(redis: Redis, scope: str | uuid.UUID) -> int | None:
    """Advance one epoch; ``None`` when Redis refused (the caller falls back)."""
    try:
        return int(
            await redis.eval(
                _ADVANCE_REFUND_EPOCH_LUA,
                1,
                _refund_epoch_key(scope),
                _POOL_KEY_TTL,
            )
        )
    except Exception:
        logger.warning(
            "Could not advance the credit-back epoch of %s", scope, exc_info=True
        )
        return None


def _refund_cause_key(
    usage_type: str,
    window_key: str,
    user_id: str | uuid.UUID | None = None,
    epoch_token: str = "",
) -> str:
    """Key of a per-cause breakdown hash for one credit-back kind + window.

    Tallied per USER, although the question it answers - "*what* are we
    forgiving?" - is about the install and the admin card reports the sum. The
    totals it decomposes can lose one user's credits (an admin's quota reset
    deletes that user's counters), and a platform-wide tally could not lose the
    same user's causes: it went on decomposing a total smaller than itself for
    the rest of the window. ``agent_failure_pool.get_pool_status`` sums the
    tallies of exactly the users whose counters make each total.

    Without *user_id*: the platform-wide hash an older release tallied causes
    into. Still read, never written - it cannot say whose credits it holds.
    *epoch_token* (``_cause_epoch_token``) files a tally under the epochs its
    credit was counted in.

    Deliberately Redis-only, with no durable ``QuotaUsage`` mirror. This is a
    diagnostic breakdown, never an allowance: losing it to a cache wipe costs a
    chart, not a refund, and the totals it decomposes stay durable on their own.
    The read path surfaces any shortfall as unattributed rather than quietly
    under-reporting — see ``agent_failure_pool.get_pool_status``.
    """
    key = f"{_REFUND_CAUSE_PREFIX}{usage_type}:{window_key}"
    if user_id is None:
        return key
    key = f"{key}:{user_id}"
    return f"{key}:{epoch_token}" if epoch_token else key


# Tallies a credit-back's cause only while the counter that granted the credit
# still holds it. A quota reset deletes the user's counters, then their
# tallies; a refund still between its claim and its tally when that happens
# would otherwise write its cause after the reset, against no counter - a stray
# cause that overshoots the total once the user is credited back again. One
# script, so the reset has a linearization point: a tally before the counter
# goes is forgotten with the rest, a tally after it writes nothing.
_RECORD_REFUND_CAUSE_LUA = """
local current = tonumber(redis.call('GET', KEYS[1]) or '0') or 0
if current < tonumber(ARGV[1]) then
    return 0
end
redis.call('HINCRBY', KEYS[2], ARGV[2], 1)
redis.call('EXPIRE', KEYS[2], ARGV[3])
return 1
"""

# Claims one failure-pool slot atomically against the pool size. A key
# missing from Redis (evicted, a restart without persistence) is NOT created
# here - the script answers -1 and the caller comes back with the durable
# high-water mark as ARGV[3], which seeds the key before the increment. A claim
# is therefore never decided against a counter that has not had its baseline
# folded in: two cold claims both come back, the first seeds, and both count
# from the durable value. The epochs are read in the same script, so the claim
# knows which epochs its credit was counted in (``_REFUND_EPOCH_PREFIX``), and
# their expiry moves with the counter's: an epoch set by an admin action long
# ago must outlive the tallies still being filed under it. ``EXPIRE`` leaves a
# missing epoch (epoch 0) missing.
_CLAIM_POOL_SLOT_LUA = """
if redis.call('EXISTS', KEYS[1]) == 0 then
    if ARGV[3] == '' then
        return {-1, 0, 0}
    end
    redis.call('SET', KEYS[1], ARGV[3])
end
local claimed = redis.call('INCR', KEYS[1])
redis.call('EXPIRE', KEYS[1], ARGV[2])
if claimed > tonumber(ARGV[1]) then
    redis.call('DECR', KEYS[1])
end
redis.call('EXPIRE', KEYS[2], ARGV[2])
redis.call('EXPIRE', KEYS[3], ARGV[2])
return {
    claimed,
    tonumber(redis.call('GET', KEYS[2]) or '0') or 0,
    tonumber(redis.call('GET', KEYS[3]) or '0') or 0,
}
"""

# Counts one free credit and reads the user's epoch in the same script, keeping
# that epoch alive as the claim does. A missing key is not created from zero
# either: the caller comes back with the durable row as ARGV[2], so a counter
# Redis lost after its flush keeps counting from what the card already shows.
_COUNT_FREE_CREDIT_LUA = """
if redis.call('EXISTS', KEYS[1]) == 0 then
    if ARGV[2] == '' then
        return {-1, 0}
    end
    redis.call('SET', KEYS[1], ARGV[2])
end
local counted = redis.call('INCR', KEYS[1])
redis.call('EXPIRE', KEYS[1], ARGV[1])
redis.call('EXPIRE', KEYS[2], ARGV[1])
return {counted, tonumber(redis.call('GET', KEYS[2]) or '0') or 0}
"""


async def _claim_pool_slot(
    redis: Redis,
    pool_key: str,
    user_id: uuid.UUID,
    pool: int,
    baseline: int | None,
) -> tuple[int, int, int]:
    """``(claimed, user epoch, platform epoch)``; ``claimed`` is -1 when cold.

    *baseline* is ``None`` for the first, cache-only attempt.
    """
    result = await redis.eval(
        _CLAIM_POOL_SLOT_LUA,
        3,
        pool_key,
        _refund_epoch_key(user_id),
        _refund_epoch_key(_GLOBAL_REFUND_EPOCH),
        pool,
        _POOL_KEY_TTL,
        "" if baseline is None else baseline,
    )
    claimed, user_epoch, global_epoch = (int(value) for value in result)
    return claimed, user_epoch, global_epoch


async def _count_free_credit(
    redis: Redis, free_key: str, user_id: uuid.UUID, baseline: int | None = None
) -> tuple[int, int]:
    """``(free credits counted in the window, user epoch)``; -1 when cold.

    *baseline* is ``None`` for the first, cache-only attempt.
    """
    result = await redis.eval(
        _COUNT_FREE_CREDIT_LUA,
        2,
        free_key,
        _refund_epoch_key(user_id),
        _POOL_KEY_TTL,
        "" if baseline is None else baseline,
    )
    counted, user_epoch = (int(value) for value in result)
    return counted, user_epoch


async def _record_refund_cause(
    redis: Redis,
    usage_type: str,
    window_key: str,
    cause: str | None,
    *,
    user_id: str | uuid.UUID,
    counter_key: str,
    granted_at: int,
    epoch_token: str = "",
) -> None:
    """Tally one credit-back of *user_id* against the cause that produced it.

    *counter_key* is the counter that granted the credit and *granted_at* the
    value that grant took it to: the cause is written only while the counter
    still counts the credit (``_RECORD_REFUND_CAUSE_LUA``). *epoch_token* is the
    epochs the credit was counted in (``_cause_epoch_token``).

    Best-effort by contract: a refund that has already been granted must never
    be undone by a bookkeeping failure, so every error here is swallowed. The
    breakdown going stale is strictly better than a user being charged for a
    run the platform failed to deliver.
    """
    if not cause:
        return
    try:
        await redis.eval(
            _RECORD_REFUND_CAUSE_LUA,
            2,
            counter_key,
            _refund_cause_key(usage_type, window_key, user_id, epoch_token),
            granted_at,
            cause,
            _POOL_KEY_TTL,
        )
    except Exception:
        logger.debug(
            "Could not tally credit-back cause %s for %s", cause, usage_type,
            exc_info=True,
        )


async def _delete_in_batches(redis: Redis, keys: AsyncIterable[str]) -> int:
    """``DEL`` *keys* as they come, ``_CAUSE_KEY_BATCH`` at a time.

    Streamed rather than collected first: *keys* is a keyspace scan with one
    hash per credited user, run inside the flush lock. Returns how many keys
    Redis removed.
    """
    removed = 0
    batch: list[str] = []
    async for key in keys:
        batch.append(key)
        if len(batch) >= _CAUSE_KEY_BATCH:
            removed += await redis.delete(*batch)
            batch.clear()
    if batch:
        removed += await redis.delete(*batch)
    return removed


async def clear_refund_causes(
    redis: Redis, usage_type: str, below_global_epoch: int | None = None
) -> int:
    """Drop every window's cause breakdown for one credit-back kind.

    With *below_global_epoch* (the platform epoch the recharge just advanced
    to), a user's tally filed under that epoch or a later one is kept: it was
    counted after the recharge swept the counters.

    Called by the pool recharge, which zeroes the counters these decompose. The
    breakdown does NOT live in the counters' keyspace (see
    ``_REFUND_CAUSE_PREFIX``), so it is not caught by that sweep and has to be
    cleared explicitly — otherwise the card renders a full pre-recharge
    breakdown against a freshly zeroed headline, at the exact moment an admin
    is looking at it hardest. Every user's tally goes, and the platform-wide
    one written before causes were tallied per user.

    Returns how many hashes were removed. Never raises: a recharge that has
    already cleared the counters must not fail on its bookkeeping.
    """

    async def _cleared():
        async for raw in redis.scan_iter(
            match=f"{_REFUND_CAUSE_PREFIX}{usage_type}:*", count=500
        ):
            key = raw if isinstance(raw, str) else raw.decode()
            if (
                below_global_epoch is None
                or (epochs := _cause_key_epochs(key)) is None
                or epochs[2] < below_global_epoch
            ):
                yield key

    try:
        return await _delete_in_batches(redis, _cleared())
    except Exception:
        logger.warning(
            "Could not clear the credit-back cause breakdown for %s — the card "
            "may show a stale decomposition until the window rolls over",
            usage_type,
            exc_info=True,
        )
        return 0


async def forget_user_refund_causes(
    redis: Redis,
    user_id: str | uuid.UUID,
    credited_windows: Iterable[tuple[str, str]] = (),
    below_user_epoch: int | None = None,
) -> int:
    """Drop one user's share of the credit-back cause breakdown.

    With *below_user_epoch* (the epoch the reset just advanced the user to), a
    tally filed under that epoch or a later one is kept: it was counted after
    the reset deleted the counters.

    Called by :func:`reset_user_quota_usage`, which deletes that user's pool and
    free counters: the card's totals lose the user's credits, so the breakdown
    decomposing them must lose the user's causes too. Their own tally
    goes, in every window and for both kinds.

    *credited_windows* are the ``(usage_type, window)`` pairs the reset removed
    credits from. The platform-wide tally of each - written before causes were
    tallied per user, it cannot say which of its causes were this user's - goes
    as well: its credits then read ``not_recorded``, which is true, rather than
    leaving a breakdown larger than the total it decomposes.

    Returns how many hashes were removed. Never raises: the counters are already
    gone by this point, and the reset must not fail on its bookkeeping.
    """

    async def _forgotten():
        async for raw in redis.scan_iter(
            match=f"{_REFUND_CAUSE_PREFIX}*:{user_id}*", count=500
        ):
            key = raw if isinstance(raw, str) else raw.decode()
            if (
                below_user_epoch is None
                or (epochs := _cause_key_epochs(key)) is None
                or epochs[1] < below_user_epoch
            ):
                yield key
        for usage_type, window_key in sorted(set(credited_windows)):
            yield _refund_cause_key(usage_type, window_key)

    try:
        return await _delete_in_batches(redis, _forgotten())
    except Exception:
        logger.warning(
            "Could not drop the credit-back causes of user %s - the card may "
            "show a breakdown larger than its total until the window rolls over",
            user_id,
            exc_info=True,
        )
        return 0


async def read_refund_causes(
    usage_type: str,
    window_key: str,
    user_ids: Iterable[str | uuid.UUID] = (),
) -> dict[str, int]:
    """Per-cause credit-back counts for one kind + window, newest state.

    Sums the tallies of *user_ids* - the users whose counters make the total
    this decomposes, so a user whose counters are gone takes their causes out
    with them - and the platform-wide tally written before causes were tallied
    per user.

    Each user's tally is read under the current epochs only: a tally
    filed under an older one belongs to a credit whose counter a reset or a
    recharge deleted.

    Returns ``{}`` when nothing has been tallied or Redis is unreachable — the
    caller reports the whole total as unattributed rather than failing the card.
    """
    users = sorted({str(user) for user in user_ids})
    raws: list = []
    try:
        redis = await redis_client()
        epoch_keys = [_refund_epoch_key(_GLOBAL_REFUND_EPOCH)] + [
            _refund_epoch_key(user) for user in users
        ]
        epochs: list[int] = []
        for start in range(0, len(epoch_keys), _CAUSE_KEY_BATCH):
            pipe = redis.pipeline(transaction=False)
            for key in epoch_keys[start : start + _CAUSE_KEY_BATCH]:
                pipe.get(key)
            epochs.extend(int(value or 0) for value in await pipe.execute())
        global_epoch, user_epochs = epochs[0], epochs[1:]
        keys = [_refund_cause_key(usage_type, window_key)] + [
            _refund_cause_key(
                usage_type,
                window_key,
                user,
                _cause_epoch_token(usage_type, user_epoch, global_epoch),
            )
            for user, user_epoch in zip(users, user_epochs, strict=True)
        ]
        for start in range(0, len(keys), _CAUSE_KEY_BATCH):
            pipe = redis.pipeline(transaction=False)
            for key in keys[start : start + _CAUSE_KEY_BATCH]:
                pipe.hgetall(key)
            raws.extend(await pipe.execute())
    except Exception:
        logger.warning(
            "Could not read the credit-back cause breakdown for %s", usage_type,
            exc_info=True,
        )
        return {}
    counts: dict[str, int] = {}
    for raw in raws:
        for key, value in (raw or {}).items():
            name = key if isinstance(key, str) else key.decode()
            try:
                counts[name] = counts.get(name, 0) + int(value)
            except (TypeError, ValueError):
                continue
    return counts


def _receipt_key(token: str) -> str:
    """Key of the metering receipt for one work item (see ``_RECEIPT_PREFIX``)."""
    return f"{_RECEIPT_PREFIX}{token}"


async def _redeem_receipt(redis: Redis, token: str) -> tuple[int, str | None]:
    """Redeem the receipt *token* names: ``(RECEIPT_TAKEN, receipt)`` when this
    call took it, ``(RECEIPT_ALREADY_REDEEMED, None)`` or
    ``(RECEIPT_ABSENT, None)`` otherwise. The token is closed afterwards in
    every case (``_REDEEM_RECEIPT_LUA``)."""
    reply = await redis.eval(
        _REDEEM_RECEIPT_LUA, 1, _receipt_key(token), _RECEIPT_REDEEMED, _RECEIPT_TTL
    )
    state = int(reply[0])
    if state != RECEIPT_TAKEN:
        return state, None
    raw = reply[1]
    return state, raw.decode() if isinstance(raw, bytes | bytearray) else str(raw)


def _config_cache_key(user_id: str | uuid.UUID) -> str:
    return f"{_CONFIG_CACHE_PREFIX}{user_id}"


#: Replaced by every invalidation once the change it follows has committed, and
#: BEFORE it deletes. A resolver reads it before the database and writes
#: its resolution only while it is unchanged, so an invalidation that has run is
#: never followed by the config it replaced. It gates writes only: a cached entry
#: is served without it. Not under ``_CONFIG_CACHE_PREFIX``, which the
#: invalidate-all scan deletes.
_CONFIG_GENERATION_KEY = "quota:config-generation"

#: KEYS: cache key, generation key. ARGV: resolution, TTL, generation read
#: before the database ('' when unset).
_SET_IF_GENERATION_LUA = """
if (redis.call('GET', KEYS[2]) or '') == ARGV[3] then
  redis.call('SET', KEYS[1], ARGV[1], 'EX', ARGV[2])
  return 1
end
return 0
"""


def _as_text(value: object) -> str:
    if value is None:
        return ""
    return value.decode() if isinstance(value, bytes) else str(value)


async def _replace_config_generation(redis: Redis) -> None:
    """First step of every invalidation. Never raises: the delete still runs, and
    a resolution in flight is left to its second read. A random token rather
    than a counter, which an eviction would restart at a value already read."""
    try:
        await redis.set(_CONFIG_GENERATION_KEY, secrets.token_hex(8))
    except Exception as exc:
        logger.debug("Quota config generation not replaced: %s", format_exception_message(exc))


# ---------------------------------------------------------------------------
# What a number means
# ---------------------------------------------------------------------------
# Two kinds of number, one reading each - spelled here so no reader re-derives
# them:
#
# * A LIMIT - the per-user / per-group / platform-default ``default`` - is what
#   one user may spend: ``None`` is unlimited and ``0`` is a hard stop.
# * A CEILING - ``platform_limit``, a license period - caps the whole platform:
#   ``None`` is uncapped. Inside the license certificate an uncapped period is
#   written as ``0`` (or omitted); that is how a quota-free license is issued,
#   so the parser keeps it, and :func:`ceiling_or_none` turns it into ``None``
#   wherever a ceiling leaves this module. A stored ``platform_limit`` of ``0``
#   is read the same way (:func:`normalize_quota_config`).
#
#   A license claim of ``0`` and a missing claim are both uncapped for EVERY
#   certificate, whatever its ``not_valid_before``: that is the validity start
#   the issuer chose, not the signing date, so a renewal signed earlier with a
#   ``0`` (then Unlimited) may start later. Never read a ``0`` claim as a zero
#   allowance and never split on the start date - the issuer stopped writing
#   ``0`` instead (27 Sep 2026: Unlimited is no claim).


def ceiling_or_none(value: object) -> int | None:
    """A ceiling as the platform reports it: a positive int, or ``None`` (uncapped)."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return int(value) if value > 0 else None


def normalize_quota_config(cfg: dict | None, *, for_write: bool = False) -> dict | None:
    """A platform quota config with every non-positive ``platform_limit`` as ``None``.

    The platform default is the last tier, with nothing to inherit from, so the
    user / group :data:`UNLIMITED_KEY` marker has no meaning there and is dropped.
    *for_write* stamps every type config with the current rules
    (:data:`RULES_VERSION_KEY`); a read never adds it.

    Copy-on-write; anything that is not a per-type dict is passed through.
    """
    if not isinstance(cfg, dict):
        return cfg
    normalized: dict = {}
    for quota_type, type_cfg in cfg.items():
        if isinstance(type_cfg, dict) and (
            for_write or "platform_limit" in type_cfg or UNLIMITED_KEY in type_cfg
        ):
            type_cfg = {k: v for k, v in type_cfg.items() if k != UNLIMITED_KEY}
            if "platform_limit" in type_cfg:
                type_cfg["platform_limit"] = ceiling_or_none(
                    type_cfg.get("platform_limit")
                )
            if for_write:
                type_cfg[RULES_VERSION_KEY] = RULES_VERSION
        normalized[quota_type] = type_cfg
    return normalized


# ---------------------------------------------------------------------------
# User / group overrides: empty inherits, Unlimited is explicit
# ---------------------------------------------------------------------------
# A user or group override of one quota type means exactly one of:
#
# * nothing - no limit at all in it (no numeric ``default``, ``platform_limit``
#   or ``per_model`` entry): the tier is skipped and the user INHERITS from the
#   next one. The editor asks for it by omitting the type (or sending
#   ``"unlimited": false``); the read path ignores such a stamped config.
# * a limit - the numbers it carries (``0`` is a hard stop).
# * Unlimited - ``{"unlimited": True, "default": None, ...}``. ``default`` stays
#   ``None`` so every reader of a limit (the gate, the ranking, the API wire,
#   where ``null`` is uncapped) already reads it as unlimited; the
#   marker is what tells it apart from an emptied field.
#
# Those rules apply to what is saved from now on, never to what an existing
# deployment already stored. Every type config written by a user, group or
# platform save carries ``rules_version: 2`` (:data:`RULES_VERSION_KEY`); one
# without it was saved before and is read exactly as it always was
# (:func:`is_legacy_type_config` is the one test, decided at read time - no
# migration, no backfill):
#
# * any non-empty type config is a value at its tier (one with no limit at all
#   is Unlimited there, whatever keys it has);
# * a group ranks by ``default or 0``, so one with no limit yields to any
#   sibling group with a number;
# * under the license pool, a ``0`` or an unlimited value is replaced by the
#   pool.
#
# Saving a value stamps it and applies the current rules; the admin screens
# say so on every legacy value whose meaning would change
# (:func:`legacy_quota_notices`).

UNLIMITED_KEY = "unlimited"
RULES_VERSION_KEY = "rules_version"
RULES_VERSION = 2

# Where a legacy value is read with a rule the current ones would not apply;
# each is logged once per process (:func:`_warn_legacy_rule_once`).
_LEGACY_RULE_MESSAGES = {
    "zero_under_pool": "a user, group or per-user default agentic limit of 0 saved before the quota rules update is replaced by the license pool",
    "group_no_limit": "a group without a limit saved before the quota rules update yields to a sibling group with a number",
    "no_default_key": "a user or group quota type saved before the quota rules update with no limit counts as Unlimited",
}
_legacy_rules_warned: set[str] = set()

# Codes the admin screens turn into a notice on a legacy value.
LEGACY_NOTICE_ZERO_UNDER_POOL = "legacy_zero_under_pool"
LEGACY_NOTICE_GROUP_NO_LIMIT = "legacy_group_no_limit"


def _is_number(value: object) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def is_legacy_type_config(cfg: object) -> bool:
    """True for a stored type config saved before the current quota rules."""
    return isinstance(cfg, dict) and cfg.get(RULES_VERSION_KEY) != RULES_VERSION


def _warn_legacy_rule_once(rule: str) -> None:
    if rule in _legacy_rules_warned:
        return
    _legacy_rules_warned.add(rule)
    logger.warning(
        "Quota: %s, as it was before the update. Saving the value in the quota "
        "admin screens applies the current rules.",
        _LEGACY_RULE_MESSAGES[rule],
    )


def _type_config_has_limit(cfg: dict) -> bool:
    per_model = cfg.get("per_model")
    return (
        _is_number(cfg.get("default"))
        or _is_number(cfg.get("platform_limit"))
        or (isinstance(per_model, dict) and bool(per_model))
    )


def _unlimited_type_config(cfg: dict) -> dict:
    return {**cfg, UNLIMITED_KEY: True, "default": None}


def normalize_override_for_write(cfg: dict | None) -> dict | None:
    """A user / group ``quota_config`` as it must be stored.

    An omitted or empty type inherits, and so does one holding no limit with an
    explicit ``"unlimited": false``. One holding no limit and no ``unlimited``
    key - what API clients and older editors send for Unlimited, e.g.
    ``{"default": null}`` - keeps that meaning and is written as the explicit
    Unlimited. A limited one loses any stray ``unlimited`` flag, and every kept
    one is stamped with the current rules. ``None`` when nothing is left, so
    the whole override inherits.
    """
    if not isinstance(cfg, dict):
        return None
    normalized: dict = {}
    for quota_type, type_cfg in cfg.items():
        if not isinstance(type_cfg, dict) or not type_cfg:
            continue
        # Only the JSON boolean ``true`` lifts a limit: the body is a raw dict,
        # and ``"false"`` or ``1`` must not.
        if type_cfg.get(UNLIMITED_KEY) is True:
            # Unlimited means no limit at all: per-model caps would still be
            # enforced under it.
            kept = {
                k: v for k, v in _unlimited_type_config(type_cfg).items() if k != "per_model"
            }
        elif _type_config_has_limit(type_cfg):
            kept = {k: v for k, v in type_cfg.items() if k != UNLIMITED_KEY}
        elif UNLIMITED_KEY in type_cfg:
            continue
        else:
            kept = _unlimited_type_config(type_cfg)
        normalized[quota_type] = {**kept, RULES_VERSION_KEY: RULES_VERSION}
    return normalized or None


def read_override_type_config(cfg: object) -> dict | None:
    """One stored user / group type config as the resolver reads it.

    ``None`` means the tier holds nothing for this type and the user inherits.
    A legacy value with no limit is served as Unlimited, which is what it
    always meant at its tier; it stays unstamped, so it keeps the legacy
    ranking and license rules.
    """
    if not isinstance(cfg, dict) or not cfg:
        return None
    if is_legacy_type_config(cfg):
        if _type_config_has_limit(cfg):
            return cfg
        if "default" not in cfg:
            _warn_legacy_rule_once("no_default_key")
        return _unlimited_type_config(cfg)
    if cfg.get(UNLIMITED_KEY) is True:
        return _unlimited_type_config(cfg)
    if _type_config_has_limit(cfg):
        return cfg
    return None


def read_override_config(cfg: dict | None) -> dict | None:
    """A stored user / group ``quota_config`` read type by type, ``None`` if empty."""
    if not isinstance(cfg, dict):
        return None
    normalized: dict = {}
    for quota_type, type_cfg in cfg.items():
        read = read_override_type_config(type_cfg)
        if read is not None:
            normalized[quota_type] = read
    return normalized or None


def _limit_generosity(cfg: dict) -> float:
    """Sort key for "the most permissive group wins": ``None`` is unlimited.

    A legacy value keeps the ranking it always had, ``default or 0``.
    """
    value = cfg.get("default")
    if value is None:
        return 0.0 if is_legacy_type_config(cfg) else math.inf
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _most_permissive(candidates: list[dict]) -> dict:
    """The group type config that applies among a user's groups."""
    if len(candidates) > 1 and any(
        is_legacy_type_config(c) and not _is_number(c.get("default"))
        for c in candidates
    ):
        _warn_legacy_rule_once("group_no_limit")
    return max(candidates, key=_limit_generosity)


def _license_ceiling_is_tighter(existing: dict, xtm_limit: int) -> bool:
    """True when the license ceiling must stand in for a configured limit.

    It does when the limit is unlimited (``None``) or looser than the ceiling.
    A ``0`` is a hard stop, tighter than any ceiling, so it is kept - except in
    a legacy value, where the pool always replaced a ``0``.
    """
    current = existing.get("default")
    if xtm_limit <= 0:
        return False
    if is_legacy_type_config(existing) and _is_number(current) and current == 0:
        _warn_legacy_rule_once("zero_under_pool")
        return True
    return current is None or current > xtm_limit


async def license_pool_active(db: AsyncSession) -> bool:
    """True when a license quota or the included allowance caps agentic actions."""
    return settings.is_xtm_one and await _resolve_xtm_agentic_config(db) is not None


async def legacy_quota_notices(
    db: AsyncSession, cfg: dict | None, *, tier: str
) -> dict[str, str]:
    """``{quota_type: notice code}`` for the legacy values of one stored config
    whose meaning the current rules would change on the next save.

    *tier* is ``"user"``, ``"group"`` or ``"platform"``. The license is only
    read when a legacy agentic ``0`` makes it matter.
    """
    notices: dict[str, str] = {}
    if not isinstance(cfg, dict):
        return notices
    pool: bool | None = None
    for quota_type in _QUOTA_TYPES:
        type_cfg = cfg.get(quota_type)
        if not type_cfg or not is_legacy_type_config(type_cfg):
            continue
        default = type_cfg.get("default")
        if quota_type == "agentic" and _is_number(default) and default == 0:
            if pool is None:
                pool = await license_pool_active(db)
            if pool:
                notices[quota_type] = LEGACY_NOTICE_ZERO_UNDER_POOL
        elif tier == "group" and not _is_number(default):
            notices[quota_type] = LEGACY_NOTICE_GROUP_NO_LIMIT
    return notices


# ---------------------------------------------------------------------------
# Quota config resolution
# ---------------------------------------------------------------------------


async def _load_user_groups_with_quotas(
    db: AsyncSession,
    user_id: uuid.UUID,
) -> list[dict]:
    """Return quota_config dicts for all groups the user belongs to.

    Read through :func:`read_override_config`, so an emptied group override is
    no candidate at all and a legacy one reads as the explicit Unlimited.
    """
    result = await db.execute(
        select(Group.quota_config)
        .join(GroupMember, GroupMember.group_id == Group.id)
        .where(
            GroupMember.user_id == user_id,
            Group.quota_config.isnot(None),
        )
    )
    configs = (read_override_config(row[0]) for row in result.all())
    return [cfg for cfg in configs if cfg]


async def _load_global_quota_config(db: AsyncSession) -> dict | None:
    result = await db.execute(select(PlatformSettings.quota_config).limit(1))
    row = result.scalar_one_or_none()
    return normalize_quota_config(row) if row else None


async def _resolve_xtm_agentic_config(db: AsyncSession) -> dict | None:
    """Build an agentic quota config from the Filigran license or EE platform fallback.

    Returns a config dict like ``{"default": 30, "period": "daily"}`` or
    ``None`` if no Filigran license or EE platform is available.

    We pick the shortest non-zero period (daily ▸ monthly ▸ yearly) so the
    per-user gauge in Settings → Usage and quotas → Quotas surfaces the *tightest* enforced
    limit — the period the user is most likely to hit first. Yearly is
    only used when neither daily nor monthly is configured (rare but
    valid for low-volume licenses).
    """
    from app.api.v1.platform_settings_helpers import (
        get_platform_settings as _load_ps,
    )
    from app.enterprise.licensing import (
        DEFAULT_AGENTIC_DAILY_QUOTA,
        get_xtm_license_info,
    )
    from app.enterprise.xtm_ca import XTM_CA_PEM
    from app.models.platform_registration import PlatformRegistration

    ps = await _load_ps(db)
    xtm_info = get_xtm_license_info(
        pem_from_env=settings.enterprise_license,
        pem_from_db=ps.enterprise_license,
        ca_pem=XTM_CA_PEM,
        platform_created_at=ps.created_at,
    )

    if xtm_info.license_validated:
        if xtm_info.daily_quota > 0:
            return {"default": xtm_info.daily_quota, "period": "daily", "global": True}
        if xtm_info.monthly_quota > 0:
            return {
                "default": xtm_info.monthly_quota,
                "period": "monthly",
                "global": True,
            }
        if xtm_info.yearly_quota > 0:
            return {
                "default": xtm_info.yearly_quota,
                "period": "yearly",
                "global": True,
            }
        return None

    result = await db.execute(
        select(PlatformRegistration).where(
            PlatformRegistration.license_validated.is_(True),
            PlatformRegistration.status == "active",
        )
    )
    if result.first() is not None:
        return {
            "default": DEFAULT_AGENTIC_DAILY_QUOTA,
            "period": "daily",
            "global": True,
        }

    return None


async def get_xtm_agentic_ceilings(db: AsyncSession) -> dict[str, int] | None:
    """Return per-period agentic execution limits from the Filigran license.

    Returns ``{"daily": N, "monthly": M, "yearly": Y}`` (only non-zero entries)
    or ``None`` if not in XTM One mode or no license quotas are available.
    """
    if settings.is_copilot_like:
        return None

    from app.api.v1.platform_settings_helpers import (
        get_platform_settings as _load_ps,
    )
    from app.enterprise.licensing import (
        DEFAULT_AGENTIC_DAILY_QUOTA,
        DEFAULT_AGENTIC_MONTHLY_QUOTA,
        DEFAULT_AGENTIC_YEARLY_QUOTA,
        get_xtm_license_info,
    )
    from app.enterprise.xtm_ca import XTM_CA_PEM
    from app.models.platform_registration import PlatformRegistration

    ps = await _load_ps(db)
    xtm_info = get_xtm_license_info(
        pem_from_env=settings.enterprise_license,
        pem_from_db=ps.enterprise_license,
        ca_pem=XTM_CA_PEM,
        platform_created_at=ps.created_at,
    )

    if xtm_info.license_validated:
        limits: dict[str, int] = {}
        if xtm_info.daily_quota > 0:
            limits["daily"] = xtm_info.daily_quota
        if xtm_info.monthly_quota > 0:
            limits["monthly"] = xtm_info.monthly_quota
        if xtm_info.yearly_quota > 0:
            limits["yearly"] = xtm_info.yearly_quota
        return limits if limits else None

    result = await db.execute(
        select(PlatformRegistration).where(
            PlatformRegistration.license_validated.is_(True),
            PlatformRegistration.status == "active",
        )
    )
    if result.first() is not None:
        return {
            "daily": DEFAULT_AGENTIC_DAILY_QUOTA,
            "monthly": DEFAULT_AGENTIC_MONTHLY_QUOTA,
            "yearly": DEFAULT_AGENTIC_YEARLY_QUOTA,
        }

    return None


# Quota types resolved by resolve_quota_config's generic user/group/global
# loops below. Deliberately excludes "sandbox": that usage type has no
# config of its own to resolve — no enforceable limit, and no independent
# period (spec 002-sandbox-quota-usage-tracking FR-011/FR-017, revised) — it
# always shares the agentic entry's limit and resolved period. It still
# appears as its own row in get_user_quota_usage's *usage* display below,
# just without a config-resolution entry here.
_QUOTA_TYPES = ("llm_proxy", "agentic")


async def resolve_quota_config(
    db: AsyncSession,
    redis: Redis,
    user_id: uuid.UUID,
    quota_type: str,
) -> dict | None:
    """Resolve the effective quota config for *quota_type* (``llm_proxy``, ``agentic``, or ``sandbox``).

    Resolution: user > group (highest ``default``) > global.
    Result is cached in Redis for ``_CONFIG_CACHE_TTL`` seconds.
    Returns the quota-type sub-dict or ``None`` (no quota).
    """
    cache_key = _config_cache_key(user_id)
    cached = await redis.get(cache_key)
    if cached is not None:
        full = json.loads(cached)
        return full.get(quota_type)

    generation = _as_text(await redis.get(_CONFIG_GENERATION_KEY))
    resolved = await _resolve_quota_config_uncached(db, user_id)

    # Cache full resolved config, unless an invalidation ran meanwhile.
    if not await _cache_resolution(redis, cache_key, resolved, generation):
        return resolved.get(quota_type)
    # Then read it again: an invalidation that only deletes - the previous
    # release's, during a rolling deploy - leaves the generation alone. It
    # commits before it deletes, so if it deleted before the write above, this
    # read sees its change. Unconfirmed, the entry goes.
    try:
        current = await _resolve_quota_config_uncached(db, user_id)
    except BaseException:
        await _drop_cached_resolution(redis, cache_key)
        raise
    if current != resolved:
        await _drop_cached_resolution(redis, cache_key)
    return current.get(quota_type)


async def _resolve_quota_config_uncached(db: AsyncSession, user_id: uuid.UUID) -> dict:
    """The full resolution ``resolve_quota_config`` caches, read from the database."""
    # 1. User level
    result = await db.execute(select(User.quota_config).where(User.id == user_id))
    user_cfg = read_override_config(result.scalar_one_or_none())

    resolved: dict = {}

    if user_cfg:
        for qt in _QUOTA_TYPES:
            if user_cfg.get(qt):
                resolved[qt] = user_cfg[qt]

    # 2. Group level – fill in missing quota types
    if not all(qt in resolved for qt in _QUOTA_TYPES):
        group_cfgs = await _load_user_groups_with_quotas(db, user_id)
        for qt in _QUOTA_TYPES:
            if qt in resolved:
                continue
            candidates = [g[qt] for g in group_cfgs if g.get(qt)]
            if candidates:
                resolved[qt] = _most_permissive(candidates)

    # 3. Global level – fill in remaining
    if not all(qt in resolved for qt in _QUOTA_TYPES):
        global_cfg = await _load_global_quota_config(db)
        if global_cfg:
            for qt in _QUOTA_TYPES:
                if qt not in resolved and global_cfg.get(qt):
                    resolved[qt] = global_cfg[qt]

    # 4. Filigran license for agentic quotas — acts as ceiling in XTM One mode
    if settings.is_xtm_one:
        xtm_agentic = await _resolve_xtm_agentic_config(db)
        if xtm_agentic:
            existing = resolved.get("agentic")
            if not existing or _license_ceiling_is_tighter(
                existing, xtm_agentic.get("default") or 0
            ):
                resolved["agentic"] = xtm_agentic

    return resolved


async def _cache_resolution(redis: Redis, cache_key: str, resolved: dict, generation: str) -> bool:
    """Write *resolved* while the generation is still *generation*; ``False``
    when an invalidation replaced it meanwhile.

    A Redis that refuses the script gets the plain write, which the second read
    confirms like any other.
    """
    payload = json.dumps(resolved)
    try:
        written = await redis.eval(
            _SET_IF_GENERATION_LUA,
            2,
            cache_key,
            _CONFIG_GENERATION_KEY,
            payload,
            _CONFIG_CACHE_TTL,
            generation,
        )
    except Exception as exc:
        logger.debug("Quota config cached without the generation check: %s", format_exception_message(exc))
        await redis.set(cache_key, payload, ex=_CONFIG_CACHE_TTL)
        return True
    return bool(written)


async def _drop_cached_resolution(redis: Redis, cache_key: str) -> None:
    """Delete a resolution that could not be confirmed. Never raises: a failure
    leaves it cached until its TTL, like a failed invalidation."""
    try:
        await redis.delete(cache_key)
    except Exception as exc:
        _log_quota_cache_not_invalidated(exc)


async def resolve_full_quota_info(
    db: AsyncSession,
    redis: Redis,
    user_id: uuid.UUID,
) -> tuple[dict, dict[str, str]]:
    """Return ``(resolved_config, sources)`` where *sources* maps each quota
    type to ``"user"`` / ``"group"`` / ``"global"`` / ``"none"``.

    XTM One mode also surfaces the XTM-license / EE-platform-default
    agentic ceiling as the effective fallback when no per-user, group
    or PlatformSettings quota is configured — otherwise the per-user
    quota overview would report "unlimited usage" for an account that
    is actually capped at the platform-wide ``30/100/300`` defaults
    (or whatever the Filigran license carries). The reported source for
    that case is still ``"global"`` so the badge already shipped on
    the user detail page ("Platform default") stays accurate.
    """
    result = await db.execute(select(User.quota_config).where(User.id == user_id))
    user_cfg = read_override_config(result.scalar_one_or_none()) or {}

    group_cfgs = await _load_user_groups_with_quotas(db, user_id)
    global_cfg = await _load_global_quota_config(db) or {}

    resolved: dict = {}
    sources: dict[str, str] = {}

    # XTM One: build a synthetic agentic config from the license /
    # EE-platform ceilings so it can fill the gap when nothing else
    # is configured. We pick the shortest non-zero period (daily ▸
    # monthly ▸ yearly) so the per-user gauge shows the *tightest*
    # enforced limit — the period the user is most likely to hit.
    xtm_agentic_cfg: dict | None = None
    if settings.is_xtm_one:
        xtm_agentic_cfg = await _resolve_xtm_agentic_config(db)

    for qt in _QUOTA_TYPES:
        if user_cfg.get(qt):
            resolved[qt] = user_cfg[qt]
            sources[qt] = "user"
        else:
            candidates = [g[qt] for g in group_cfgs if g.get(qt)]
            if candidates:
                resolved[qt] = _most_permissive(candidates)
                sources[qt] = "group"
            elif global_cfg.get(qt):
                resolved[qt] = global_cfg[qt]
                sources[qt] = "global"
            elif qt == "agentic" and xtm_agentic_cfg:
                # XTM-license / EE-platform default agentic ceiling fills
                # in when no admin-configured quota exists. Strip the
                # internal ``"global": True`` flag — that's a marker for
                # the enforcement path only.
                resolved[qt] = {
                    k: v for k, v in xtm_agentic_cfg.items() if k != "global"
                }
                sources[qt] = "global"
            else:
                sources[qt] = "none"

    # XTM One: the license / EE-platform agentic ceiling is a HARD cap. When it
    # is tighter than the resolved per-user/group/platform limit (or that limit
    # is unlimited), the ceiling — not the configured limit — is what actually
    # gates the user: ``check_agentic_quota`` enforces the shared global pool
    # and returns early on ``config.get("global")``, skipping the per-user
    # limit. Mirror ``resolve_quota_config``'s ceiling-replacement (step 4)
    # exactly here so the *displayed* limit/period match what actually gates and
    # what ``get_user_quota_usage`` reports as the shared global-pool count.
    # Without this, the two resolvers diverge and the gauge pairs a shared-pool
    # numerator against a looser per-user denominator (and a mismatched period),
    # reintroducing the very misalignment this display path exists to prevent.
    # Report the source as ``"global"`` so the UI labels it platform-wide.
    if settings.is_xtm_one and xtm_agentic_cfg:
        existing = resolved.get("agentic")
        if existing is not None and _license_ceiling_is_tighter(
            existing, xtm_agentic_cfg.get("default") or 0
        ):
            resolved["agentic"] = {
                k: v for k, v in xtm_agentic_cfg.items() if k != "global"
            }
            sources["agentic"] = "global"

    return resolved, sources


# ---------------------------------------------------------------------------
# Usage counter operations (Redis)
# ---------------------------------------------------------------------------


async def _get_count(redis: Redis, key: str) -> int:
    val = await redis.get(key)
    return int(val) if val else 0


async def _increment(redis: Redis, key: str, ttl: int = _USAGE_KEY_TTL) -> int:
    """Bump a counter and refresh its expiry.

    *ttl* is a parameter for one reason: the agent failure pool's window is a
    year, so its counter must outlive the 35 days that cover a monthly usage
    bucket.
    """
    count = await redis.incr(key)
    await redis.expire(key, ttl)
    return count


async def _get_user_count(
    redis: Redis,
    user_id: str | uuid.UUID,
    usage_type: str,
    model: str | None,
    period_key: str,
) -> int:
    """Current-period per-user counter, summing new + legacy key shapes.

    A counter written before the agent-attribution deploy lives under the
    4-segment legacy key; post-deploy increments live under the 5-segment
    ``_all`` key (the legacy value is folded in on the first increment, see
    ``_merge_legacy_usage_key``). Summing both shapes keeps enforcement
    correct across rolling deploys — the two keys never hold overlapping
    usage, so the sum can never double-count.
    """
    new_count = await _get_count(
        redis, _usage_key(user_id, usage_type, model, period_key)
    )
    legacy_count = await _get_count(
        redis, _legacy_usage_key(user_id, usage_type, model, period_key)
    )
    return new_count + legacy_count


async def _merge_legacy_usage_key(
    redis: Redis,
    user_id: str | uuid.UUID,
    usage_type: str,
    model: str | None,
    period_key: str,
) -> None:
    """Fold a legacy 4-segment counter into its 5-segment successor.

    ``GETDEL`` is atomic, so concurrent increments cannot fold the same
    legacy value twice; a failure between GETDEL and INCRBY could at worst
    lose the not-yet-flushed Redis delta for one legacy key, the same
    exposure any Redis-buffered counter already has.
    """
    legacy_key = _legacy_usage_key(user_id, usage_type, model, period_key)
    try:
        val = await redis.getdel(legacy_key)
    except AttributeError:
        # Very old Redis client without GETDEL support — leave the legacy
        # key alone; reads still sum both shapes.
        return
    if val:
        key = _usage_key(user_id, usage_type, model, period_key)
        await redis.incrby(key, int(val))
        await redis.expire(key, _USAGE_KEY_TTL)


# How long reading a platform-wide counter's saved mark waits for a write of the
# platform-wide counters in flight to commit, before it gives up like an
# unreachable database.
_GLOBAL_BASELINE_FLUSH_WAIT_SECONDS = 10.0

# The saved-mark read in progress per counter, joined by every caller of the
# process that finds the same counter missing meanwhile.
_global_baseline_reads: dict[tuple[str, str], asyncio.Task[int]] = {}


def _forget_global_baseline_read(slot: tuple[str, str], task: asyncio.Task[int]) -> None:
    if _global_baseline_reads.get(slot) is task:
        del _global_baseline_reads[slot]
    if not task.cancelled():
        # Retrieved, so a read every caller stopped waiting for does not log
        # its failure as never retrieved.
        task.exception()


async def _load_global_baseline(usage_type: str, period_key: str) -> int:
    """Durable count of one platform-wide counter, from ``quota_global_usage``.

    ``flush_quota_usage`` persists the ``_global`` counters with a monotonic
    ``max()``, so the row is a high-water mark, exactly like the pool's
    (``_load_pool_baseline``). The callers of the process that miss the same
    counter meanwhile share one read: after a Redis loss every gate check misses
    at once, each holding its own request's connection, and one read per caller
    would each want a second one from the same pool. Nothing is cached past the
    read: Redis holds the seed.
    """
    loop = asyncio.get_running_loop()
    slot = (usage_type, period_key)
    reading = _global_baseline_reads.get(slot)
    if reading is None or reading.done() or reading.get_loop() is not loop:
        reading = loop.create_task(_read_global_baseline(usage_type, period_key))
        _global_baseline_reads[slot] = reading
        reading.add_done_callback(partial(_forget_global_baseline_read, slot))
    # Shielded: a caller cancelled while waiting must not cancel the read the
    # others are waiting on.
    return await asyncio.shield(reading)


async def _read_global_baseline(usage_type: str, period_key: str) -> int:
    """The row behind :func:`_load_global_baseline`.

    Read once no write of the platform-wide counters is in flight
    (``_QUOTA_GLOBAL_FLUSH_LOCK_KEY``): one may be raising the row to a count
    Redis has lost since, and a counter seeded from the older row would stay
    below its saved mark for the rest of the period. That lock is held for the
    one statement of :func:`_flush_global_usage`, never for the per-user rows:
    waiting on the flush lock, every read-back of a period rollover - when
    every platform-wide counter is missing at once - outwaited the per-user
    flush and refused the turn. It waits to share the lock, up to
    ``_GLOBAL_BASELINE_FLUSH_WAIT_SECONDS``, rolling back between tries so the
    wait holds neither a lock nor a connection. The one clear that deletes a
    row, a support-code reset (``reset_platform_quota_usage``), holds
    the same lock exclusively until it commits, so this reads the row it left
    - none once committed, the old one if it rolled back. Read on
    a session of its own, never the caller's: a failed read must not abort the
    caller's transaction. Raises when the database cannot answer or the write
    outlasts the wait, which the callers treat as a quota store they cannot
    read. The wait's ``TimeoutError`` is a store outage
    (:func:`is_quota_store_outage`): a due background run whose quota check
    meets it is held and retried, never failed for it.
    """
    from sqlalchemy import text as sa_text

    from app.core.database import async_session
    from app.models.quota import QuotaGlobalUsage

    async with async_session() as db:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + _GLOBAL_BASELINE_FLUSH_WAIT_SECONDS
        while not (
            await db.execute(
                sa_text("SELECT pg_try_advisory_xact_lock_shared(CAST(:k AS bigint))"),
                {"k": _QUOTA_GLOBAL_FLUSH_LOCK_KEY},
            )
        ).scalar():
            if loop.time() >= deadline:
                raise TimeoutError(
                    "A write of the platform-wide quota counters held its lock "
                    "past the read-back wait"
                )
            await db.rollback()
            await asyncio.sleep(_RESET_FLUSH_LOCK_POLL_SECONDS)
        count = (
            await db.execute(
                select(QuotaGlobalUsage.count).where(
                    QuotaGlobalUsage.usage_type == usage_type,
                    QuotaGlobalUsage.period == period_key,
                )
            )
        ).scalar_one_or_none()
    return count or 0


async def _get_global_count(redis: Redis, usage_type: str, period_key: str) -> int:
    """A platform-wide counter, put back at its durable count if Redis lost it.

    The platform-wide ceilings are enforced against these counters alone, and
    Redis can lose one mid-period: an eviction, a restart without persistence,
    a failover to a cold replica, or a yearly key that lapsed after
    ``_USAGE_KEY_TTL`` without a run. Counting on from zero would reopen the
    whole ceiling, so a missing key is seeded from the row before it is compared
    with a ceiling (a charge seeds its own, ``_count_global``). ``SET NX``:
    whoever seeds first wins and a charge that landed meanwhile is kept, so
    racing callers seed the baseline once. Seeded at 0 too, or a counter nothing
    has saved yet (a sandbox never used) would be read from the database on
    every request. Up to one flush cycle of increments Redis held alone is still
    lost with it.
    """
    key = _global_usage_key(usage_type, period_key)
    held = await redis.get(key)
    if held is not None:
        return int(held)
    baseline = await _load_global_baseline(usage_type, period_key)
    if await redis.set(key, baseline, nx=True, ex=_USAGE_KEY_TTL):
        return baseline
    held = await redis.get(key)
    return int(held) if held is not None else baseline


# Counts one charge in the platform-wide counters of one quota type:
# KEYS the counters of every period it reaches, ARGV[1] their expiry, ARGV[1+i]
# the saved mark of KEYS[i] ('' when not read back). A counter Redis lost is
# never recreated from zero: while one has no saved mark the script answers -1
# and counts nothing, and the caller comes back with the marks - the rule of
# ``_CLAIM_POOL_SLOT_LUA``. One script, so a counter lost between a read and
# its increment cannot restart at 1, and the periods of a charge count together.
_COUNT_GLOBAL_LUA = """
for i = 1, #KEYS do
    if ARGV[i + 1] == '' and redis.call('EXISTS', KEYS[i]) == 0 then
        return -1
    end
end
for i = 1, #KEYS do
    if redis.call('EXISTS', KEYS[i]) == 0 then
        redis.call('SET', KEYS[i], ARGV[i + 1])
    end
    redis.call('INCR', KEYS[i])
    redis.call('EXPIRE', KEYS[i], ARGV[1])
end
return #KEYS
"""


async def _count_global(redis: Redis, usage_type: str, periods: Iterable[str]) -> None:
    """Count one charge in the platform-wide counters of *usage_type* for every
    period in *periods*.

    One round trip while Redis holds them. A counter it lost is seeded from its
    saved mark (``_load_global_baseline``) in the same script as the increment:
    an ``INCR`` on a missing key would create it at 1, and the mark would never
    be read again for the rest of the period. The marks are read before anything
    is counted, so a read-back that fails raises with nothing charged. A refund
    never seeds: it only finds a key missing when Redis lost it after the charge,
    which most likely went with it, so it leaves the key to the next read.
    """
    period_keys = sorted({_current_period_key(period) for period in periods})
    counters = [_global_usage_key(usage_type, key) for key in period_keys]
    unread = [""] * len(counters)
    if int(await redis.eval(_COUNT_GLOBAL_LUA, len(counters), *counters, _USAGE_KEY_TTL, *unread)) >= 0:
        return
    saved = [await _load_global_baseline(usage_type, key) for key in period_keys]
    await redis.eval(_COUNT_GLOBAL_LUA, len(counters), *counters, _USAGE_KEY_TTL, *saved)


# ---------------------------------------------------------------------------
# Quota checks
# ---------------------------------------------------------------------------


async def _load_platform_wide_config(
    db: AsyncSession,
    quota_type: str,
) -> dict | None:
    """Load the platform-wide quota config from PlatformSettings.

    Returns the raw config sub-dict (with ``platform_limit``, ``period``, etc.)
    or ``None`` if no platform-wide limit is set for this quota type.
    """
    global_cfg = await _load_global_quota_config(db)
    if not global_cfg:
        return None
    qt_cfg = global_cfg.get(quota_type)
    if not qt_cfg or ceiling_or_none(qt_cfg.get("platform_limit")) is None:
        return None
    return qt_cfg


async def _check_platform_wide_limit(
    db: AsyncSession,
    redis: Redis,
    quota_type: str,
    model: str | None = None,
) -> None:
    """Check the platform-wide (non per-user) limit.  Raises ``QuotaExceededError``.

    In XTM One mode, the license provides the platform-wide agentic limit
    (already handled via the ``"global": True`` flag in ``check_agentic_quota``).
    In Copilot mode, admins can set ``platform_limit`` in PlatformSettings.

    For ``quota_type="agentic"``, also sums in the global sandbox counter
    (spec 002-sandbox-quota-usage-tracking FR-015/FR-016) — this is the only
    remaining gate for sandbox dispatches with no per-user identity at all
    (e.g. anonymous chat sessions), since the per-user combined check that
    normally shares this ceiling can't run without a user to resolve a
    config for.
    """
    platform_cfg = await _load_platform_wide_config(db, quota_type)
    if not platform_cfg:
        return
    platform_limit = ceiling_or_none(platform_cfg.get("platform_limit"))
    if platform_limit is None:
        return
    period = platform_cfg.get("period", "monthly")
    period_key = _current_period_key(period)
    current = await _get_global_count(redis, quota_type, period_key)
    note = None
    if quota_type == "agentic":
        sandbox_current = await _get_global_count(redis, "sandbox", period_key)
        note = f"{current} agentic + {sandbox_current} sandbox" if sandbox_current else None
        current += sandbox_current
    if current >= platform_limit:
        raise QuotaExceededError(quota_type, platform_limit, current, period, note=note)


async def check_llm_proxy_quota(
    db: AsyncSession,
    user_id: uuid.UUID,
    model: str | None,
) -> None:
    """Check LLM proxy quota. Raises ``QuotaExceededError`` if exceeded."""
    redis = await redis_client()

    # Platform-wide hard cap (cannot be overridden)
    await _check_platform_wide_limit(db, redis, "llm_proxy", model)

    config = await resolve_quota_config(db, redis, user_id, "llm_proxy")
    if not config:
        return

    period = config.get("period", "monthly")
    period_key = _current_period_key(period)

    # Check default (total) limit
    default_limit = config.get("default")
    if default_limit is not None:
        current = await _get_user_count(redis, user_id, "llm_proxy", None, period_key)
        if current >= default_limit:
            raise QuotaExceededError("llm_proxy", default_limit, current, period)

    # Check model-specific limit
    if model:
        model_limit = (config.get("per_model") or {}).get(model)
        if model_limit is not None:
            current = await _get_user_count(
                redis, user_id, "llm_proxy", model, period_key
            )
            if current >= model_limit:
                raise QuotaExceededError(
                    "llm_proxy", model_limit, current, period, model=model
                )


async def _check_combined_agentic_limit(
    redis: Redis,
    user_id: uuid.UUID,
    config: dict,
) -> None:
    """Raise ``QuotaExceededError`` if agentic+sandbox usage has hit the
    per-user agentic limit. Shared by both branches of ``check_agentic_quota``
    so their combined-ceiling comparison can't drift apart.
    """
    default_limit = config.get("default")
    if default_limit is None:
        return
    period = config.get("period", "monthly")
    period_key = _current_period_key(period)
    current = await _get_user_count(redis, user_id, "agentic", None, period_key)
    sandbox_current = await _get_user_count(redis, user_id, "sandbox", None, period_key)
    total = current + sandbox_current
    if total >= default_limit:
        note = f"{current} agentic + {sandbox_current} sandbox" if sandbox_current else None
        raise QuotaExceededError("agentic", default_limit, total, period, note=note)


async def check_agentic_quota(
    db: AsyncSession,
    user_id: uuid.UUID | None,
) -> None:
    """Check agentic execution quota. Raises ``QuotaExceededError`` if exceeded.

    Two checks are performed in order:
    1. **Platform-wide hard cap** — in Copilot mode from ``platform_limit`` in
       PlatformSettings; in XTM One mode from the license global quota. This
       cap applies to the total across ALL users and cannot be overridden,
       and also sums in the *global* sandbox counter (spec
       002-sandbox-quota-usage-tracking FR-015/FR-016) — this is the only
       gate that still applies when *user_id* is ``None`` (a sandbox
       dispatch with no attributable user, e.g. an anonymous chat session —
       see 3 below).
    2. **Per-user limit** — resolved via user > group > platform default.
       Compared against ``agentic_count + sandbox_count`` combined, not
       agentic usage alone (spec FR-015/FR-016): sandbox usage has no
       enforceable limit of its own, so it shares this one as a combined
       ceiling. The sandbox term is looked up under *this* period's key —
       the one agentic's own config resolved — rather than re-resolving
       sandbox's own independently-configured period (FR-011/FR-017): the
       limit being enforced here is the agentic limit, so the usage window
       being measured against it must be the same window that limit means,
       even though sandbox's own period may differ (it still governs
       sandbox's own display/metrics rows unaffected by this).
    3. **No per-user identity** — *user_id* is ``None`` for sandbox
       dispatches that can't be attributed to a specific user (spec Edge
       Cases), and for every agentic run with no account behind it: an
       anonymous channel sender, a public widget visitor, the ``/mcp/global``
       platform key. The per-user limit above requires a user to
       resolve a config for, so it's skipped entirely in that case — only the
       platform-wide hard cap (1) still applies, which is why that check now
       also counts sandbox usage: it's the last remaining gate for this
       scenario.
    """
    redis = await redis_client()

    if settings.is_xtm_one:
        # XTM One: check all platform-wide period caps (daily/monthly/yearly),
        # then per-user.
        ceilings = await get_xtm_agentic_ceilings(db)
        if ceilings:
            for xtm_period, xtm_limit in ceilings.items():
                if xtm_limit <= 0:
                    continue
                xtm_period_key = _current_period_key(xtm_period)
                xtm_current = await _get_global_count(redis, "agentic", xtm_period_key)
                sandbox_current = await _get_global_count(redis, "sandbox", xtm_period_key)
                total = xtm_current + sandbox_current
                if total >= xtm_limit:
                    note = (
                        f"{xtm_current} agentic + {sandbox_current} sandbox"
                        if sandbox_current
                        else None
                    )
                    raise QuotaExceededError(
                        "agentic", xtm_limit, total, xtm_period, note=note
                    )

        if user_id is None:
            return

        # Per-user limit (from user > group > platform default)
        config = await resolve_quota_config(db, redis, user_id, "agentic")
        if not config:
            return
        if config.get("global"):
            return
        await _check_combined_agentic_limit(redis, user_id, config)
    else:
        # Copilot mode: check platform-wide hard cap first, then per-user
        await _check_platform_wide_limit(db, redis, "agentic")

        if user_id is None:
            return

        config = await resolve_quota_config(db, redis, user_id, "agentic")
        if not config:
            return
        await _check_combined_agentic_limit(redis, user_id, config)


async def agentic_platform_cap_enforced(db: AsyncSession) -> bool:
    """The platform-wide tier of :func:`check_agentic_quota`, as a predicate.

    True when a cap applies to everyone regardless of their own config: the
    license ceilings in XTM One mode (``get_xtm_agentic_ceilings`` already
    drops the zero, i.e. unlimited, periods), the ``platform_limit`` of
    ``PlatformSettings`` in Copilot mode.
    """
    if settings.is_xtm_one:
        return bool(await get_xtm_agentic_ceilings(db))
    platform_cfg = await _load_platform_wide_config(db, "agentic")
    return bool(platform_cfg and ceiling_or_none(platform_cfg.get("platform_limit")))


async def has_enforceable_agentic_limit(
    db: AsyncSession,
    redis: Redis,
    user_id: uuid.UUID,
    *,
    platform_cap_enforced: bool | None = None,
) -> bool:
    """True when at least one tier actually caps this user's agentic actions.

    Mirrors :func:`check_agentic_quota` gate for gate - the platform-wide cap
    (:func:`agentic_platform_cap_enforced`), then the user > group > platform
    default config through the SAME :func:`resolve_quota_config` the gate
    reads - so the two cannot drift: a user this returns False for is a user
    that gate never refuses.

    It is the question the agent failure pool has to ask before spending a
    slot. The usage counters are incremented whether or not anything
    caps them (they double as consumption telemetry), so from the pool's point
    of view every failed run looks "charged". On an unlimited quota that charge
    costs nothing, a refund gives back nothing, and a pool that rations refunds
    rations nothing - claiming a slot there only ever produces "pool exhausted,
    failed run stays charged" on a platform that has no quota, which reads as
    the platform having started to ration the user.

    ``None`` is the unlimited value throughout the quota config (the UI's
    ``Unlimited``); ``0`` is a limit like any other, exactly as it is for the
    gate. *platform_cap_enforced* lets a caller that resolves many users pass
    the platform tier once instead of re-reading the license per user.
    """
    if platform_cap_enforced is None:
        platform_cap_enforced = await agentic_platform_cap_enforced(db)
    if platform_cap_enforced:
        return True
    config = await resolve_quota_config(db, redis, user_id, "agentic")
    if not config:
        return False
    if config.get("global"):
        # The license ceiling stood in for the per-user config (step 4 of the
        # resolution). It is enforced through the platform tier above; when
        # that tier read empty just now, the cached config is the older of the
        # two readings - trust it, i.e. fail toward "enforced", which is the
        # behaviour the pool always had.
        return True
    return config.get("default") is not None


async def platform_agentic_quota_is_unlimited(
    db: AsyncSession,
    *,
    platform_cap_enforced: bool | None = None,
) -> bool:
    """True when nothing PLATFORM-WIDE caps agentic actions.

    No license ceiling / ``platform_limit`` and no platform default per user -
    the state Settings > Usage and quotas > Quotas renders as ``No platform limit`` plus
    ``Unlimited``. Users and groups may still carry a limit of their own, which
    is why the failure pool card reads this as "the pool only applies to users
    with their own limit" rather than "off"; the per-user truth is
    :func:`has_enforceable_agentic_limit`.
    """
    if platform_cap_enforced is None:
        platform_cap_enforced = await agentic_platform_cap_enforced(db)
    if platform_cap_enforced:
        return False
    global_cfg = await _load_global_quota_config(db) or {}
    agentic_cfg = global_cfg.get("agentic") or {}
    return agentic_cfg.get("default") is None


class QuotaCheckUnavailableError(Exception):
    """Raised by ``enforce_combined_sandbox_quota`` when the quota backend
    itself is unreachable and quotas fail closed — distinct from
    ``QuotaExceededError`` so callers can tell "quota exceeded" apart from
    "couldn't check the quota" and surface the right message for each.
    """


async def enforce_combined_sandbox_quota(user_id: uuid.UUID | None) -> None:
    """Run the combined agentic+sandbox quota check for an OpenSandbox
    dispatch, in a dedicated DB session.

    Shared by the three OpenSandbox-routed call sites (code interpreter,
    JS tool, Python tool) so their fail-open/fail-closed policy and logging
    can't drift apart (spec 002-sandbox-quota-usage-tracking FR-015/FR-018/
    FR-019). Opens its own session rather than reusing the caller's: these
    call sites can run concurrently within one agent turn (agent_loop.py's
    asyncio.gather batch), all sharing that turn's one AsyncSession, and
    SQLAlchemy forbids concurrent operations on the same session.

    *user_id* may be ``None`` (no attributable user — e.g. an anonymous
    chat session): ``check_agentic_quota`` still runs and still enforces
    the platform-wide hard cap in that case, just skips the per-user limit
    it can't resolve without a user. Call this unconditionally — do not
    gate the call itself on ``user_id is not None``, or the platform-wide
    cap silently stops applying to unattributed dispatches too.

    Raises ``QuotaExceededError`` if the combined limit is hit, or
    ``QuotaCheckUnavailableError`` if the check itself failed and quotas fail
    closed (``quota_fail_closed``). Otherwise returns normally — including
    when the check failed and fail-mode is open (logged, dispatch proceeds
    unmetered for this check).
    """
    from app.core.database import async_session

    try:
        async with async_session() as own_db:
            await check_agentic_quota(own_db, user_id)
    except QuotaExceededError:
        raise
    except Exception as exc:
        if quota_fail_closed():
            logger.error(
                "Combined agentic+sandbox quota check failed for user "
                "%s — refusing dispatch (quotas fail closed)",
                user_id,
                exc_info=True,
            )
            raise QuotaCheckUnavailableError() from exc
        logger.error(
            "Combined agentic+sandbox quota check failed for user %s — "
            "dispatch proceeds unmetered for this check",
            user_id,
            exc_info=True,
        )


# ---------------------------------------------------------------------------
# Usage counter increments
# ---------------------------------------------------------------------------


async def increment_llm_proxy_usage(
    user_id: uuid.UUID,
    model: str | None,
    period: str = "monthly",
    db: AsyncSession | None = None,
) -> None:
    """Increment LLM proxy usage counters (total + per-model + global).

    When *db* is provided the period is resolved from the user's effective
    quota config so that daily / yearly quotas are tracked correctly.

    The global counter is always incremented for the user's resolved period
    **and** for the platform-wide period (if different) so that the
    ``platform_limit`` hard cap is enforced correctly even when user/group
    quotas use a different period.
    """
    redis = await redis_client()
    platform_period: str | None = None
    if db is not None:
        config = await resolve_quota_config(db, redis, user_id, "llm_proxy")
        if config:
            period = config.get("period", period)
        platform_cfg = await _load_platform_wide_config(db, "llm_proxy")
        if platform_cfg:
            platform_period = platform_cfg.get("period", "monthly")
    period_key = _current_period_key(period)
    await _count_global(redis, "llm_proxy", {period, platform_period or period})
    await _merge_legacy_usage_key(redis, user_id, "llm_proxy", None, period_key)
    total_key = _usage_key(user_id, "llm_proxy", None, period_key)
    await _increment(redis, total_key)
    if model:
        await _merge_legacy_usage_key(redis, user_id, "llm_proxy", model, period_key)
        model_key = _usage_key(user_id, "llm_proxy", model, period_key)
        await _increment(redis, model_key)


async def _resolve_agentic_periods(
    redis: Redis,
    db: AsyncSession | None,
    user_id: uuid.UUID | None,
    period: str,
) -> tuple[str, set[str]]:
    """Resolve the effective agentic ``(period, extra_global_periods)``.

    Single source of truth shared by :func:`increment_agentic_usage`,
    :func:`refund_agentic_usage`, and :func:`increment_sandbox_usage` so all
    three touch the *exact same* counter set: the user's resolved period
    plus every platform-wide / license-ceiling global period. Keeping this
    in one place is what guarantees a refund reverses precisely what the
    matching increment added — a divergence here would leave usage
    permanently skewed. Returns ``period`` unchanged (and no extra periods)
    when ``db`` is None. *user_id* may be ``None`` (no user to attribute
    usage to — spec Edge Cases) — the per-user resolution step is skipped in
    that case, but the platform-wide/license ``extra_global_periods`` below
    are user-independent and still resolved.
    """
    # Every panel period is counted platform-wide, so the shared pool of each
    # one is known whether or not a ceiling is set in it (the composer's quota
    # panel). ``quota_global_usage`` is keyed by exact period: nothing sums two.
    extra_global_periods: set[str] = set(QUOTA_VIEW_PERIODS)
    if db is not None:
        if user_id is not None:
            config = await resolve_quota_config(db, redis, user_id, "agentic")
            if config:
                period = config.get("period", period)
        if settings.is_xtm_one:
            ceilings = await get_xtm_agentic_ceilings(db)
            if ceilings:
                extra_global_periods |= set(ceilings.keys())
        else:
            platform_cfg = await _load_platform_wide_config(db, "agentic")
            if platform_cfg:
                extra_global_periods.add(platform_cfg.get("period", "monthly"))
    return period, extra_global_periods


def _view_usage_keys(user_id: str | uuid.UUID) -> list[tuple[str, int]]:
    """The display counters one charge of *user_id* adds to, with their expiry."""
    return [
        (
            _usage_key(user_id, _VIEW_USAGE_TYPE, None, _current_period_key(period)),
            _VIEW_KEY_TTLS[period],
        )
        for period in QUOTA_VIEW_PERIODS
    ]


async def _count_view(redis: Redis, user_id: uuid.UUID | None) -> None:
    """Count one agentic or sandbox action in the user's panel counters."""
    if user_id is None:
        return
    for key, ttl in _view_usage_keys(user_id):
        await _increment(redis, key, ttl)


async def increment_agentic_usage(
    user_id: uuid.UUID | None,
    period: str = "monthly",
    db: AsyncSession | None = None,
    agent_id: uuid.UUID | None = None,
    receipt_token: str | None = None,
) -> str:
    """Increment agentic execution usage counter.

    Returns the key of the period the charge was counted in - the ``period``
    of the entry ``get_user_quota_usage`` reports it under - so a live
    counter can tell which bucket an in-flight charge belongs to.

    When *db* is provided the period is resolved from the user's effective
    quota config (cached in Redis after a preceding ``check_agentic_quota``
    call) so that daily / yearly quotas are tracked correctly.

    The global counter is always incremented for the user's resolved period
    **and** for the platform-wide / license period (if different) so that
    the ``platform_limit`` (Copilot) or license cap (XTM One) is enforced
    correctly even when user/group quotas use a different period.  The
    per-user counter is kept for attribution and top-consumers reporting.

    *user_id* ``None`` is a run with no account behind it - an anonymous
    channel sender, a public widget visitor, the ``/mcp/global`` platform key:
    only the global counters are charged, the ones the license
    ceilings and ``platform_limit`` read, and nothing is attributed to a user
    or to a user's agent.

    *receipt_token* names the work item this charge belongs to (e.g.
    ``"sub_agent_task:<id>:<attempt>"``). When given, a metering receipt is
    written alongside the counters so a process that never sees this turn
    again — the orchestration engine dead-lettering a job whose worker died —
    can credit the action back knowing for a fact that it was charged. Omit it
    for work that cannot be abandoned out-of-process (an ordinary chat turn
    dies with its own handlers). A token already closed when the charge lands
    (its attempt was replaced before it charged) gets no receipt, and
    the charge is given straight back. See ``_RECEIPT_PREFIX``.
    """
    redis = await redis_client()
    period, extra_global_periods = await _resolve_agentic_periods(
        redis, db, user_id, period
    )
    await _count_global(redis, "agentic", {period, *extra_global_periods})
    period_key = _current_period_key(period)
    if user_id is not None:
        await _merge_legacy_usage_key(redis, user_id, "agentic", None, period_key)
        key = _usage_key(user_id, "agentic", None, period_key)
        await _increment(redis, key)
        if agent_id is not None:
            # Per-agent attribution counter for observability; kept separate
            # from the aggregate per-user counter used by quota checks.
            agent_key = _usage_key(user_id, "agentic", None, period_key, agent_id)
            await _increment(redis, agent_key)
        await _count_view(redis, user_id)
    if receipt_token:
        # Written LAST, deliberately: a receipt must never outlive the charge it
        # attests to. If anything above fails, no receipt exists and the
        # abandonment path credits nothing — the same outcome as today.
        # Best-effort by contract: a metered request must not fail because its
        # receipt could not be written (the cost is a lost credit later, not a
        # wrong charge now).
        try:
            written = await redis.eval(
                _WRITE_RECEIPT_LUA,
                1,
                _receipt_key(receipt_token),
                json.dumps(
                    {
                        # ``None``: a charge with no account behind it.
                        "user_id": str(user_id) if user_id is not None else None,
                        "agent_id": str(agent_id) if agent_id else None,
                        "period": period,
                    }
                ),
                _RECEIPT_TTL,
                _RECEIPT_REDEEMED,
            )
        except Exception:
            logger.debug(
                "Failed to write metering receipt %s", receipt_token, exc_info=True
            )
            return period_key
        if not int(written):
            # Its attempt was replaced before this charge landed: the
            # attempt that replaced it is charged for the work, so this one runs
            # uncharged.
            logger.info(
                "Agentic charge for %s given back: the orchestration engine "
                "replaced its attempt before it was charged",
                receipt_token,
            )
            try:
                await refund_agentic_usage(
                    user_id, period=period, db=db, agent_id=agent_id
                )
            except Exception:
                logger.warning(
                    "Could not give back the charge of replaced attempt %s - "
                    "it stays charged",
                    receipt_token,
                    exc_info=True,
                )
    return period_key


async def increment_sandbox_usage(
    user_id: uuid.UUID | None,
    period: str = "monthly",
    db: AsyncSession | None = None,
) -> None:
    """Best-effort increment of the sandbox usage counter(s).

    Writes into the same ``quota_usage`` table/Redis-counter pattern as
    ``increment_llm_proxy_usage``/``increment_agentic_usage``, with
    ``usage_type="sandbox"``. Deliberately diverges from those two siblings:

    - Never raises. Callers (the three OpenSandbox-routed execution
      functions) invoke this *after* a sandbox call has already completed
      successfully — a failure to record usage must not fail an
      already-completed call, so any error here is caught and logged rather
      than propagated (spec FR-010, `/speckit-clarify` 2026-07-31 Q1). This
      mirrors the existing precedent for the same class of action:
      ``increment_llm_proxy_usage``'s caller, ``_track_model_usage`` in
      ``app/api/v1/llm_proxy.py``, already treats a usage-counter increment
      as best-effort. Building the swallow into this function itself (rather
      than requiring each of the three call sites to remember to wrap it)
      avoids duplicating the same try/except three times.
    - Sandbox has no period of its own: it always uses the same period(s)
      as the *agentic* quota (spec FR-011, revised) — sandbox has no
      enforceable limit of its own and always shares the agentic limit and
      window as its enforcement ceiling (FR-014/FR-017), so giving it an
      independently-resolved period served no purpose other than making the
      combined check's read key and this function's write key drift apart
      whenever an admin configured them differently. Reuses
      ``_resolve_agentic_periods`` — the same single source of truth
      ``increment_agentic_usage`` uses — so both stay in lockstep.
    - *user_id* may be ``None`` (a sandbox call that can't be attributed to
      a specific user — spec Edge Cases, e.g. an anonymous chat session):
      the per-user counter is skipped in that case (nothing to attribute it
      to), but the *global* counter(s) below are always written regardless
      — they're what ``check_agentic_quota``'s platform-wide hard cap reads,
      and that cap is the only gate left for this scenario (FR-015/FR-016).

    No legacy-key merge (unlike its siblings): ``"sandbox"`` is a brand-new
    usage_type with no prior 4-segment key shape to fold in.

    Respects ``settings.disable_sandbox_quota_usage`` (default ``False`` —
    counting is on by default; set ``True``, e.g. for self-hosted/on-prem
    deployments, to opt out). Checked once here rather than at each of the
    three call sites — a single choke point.
    """
    if settings.disable_sandbox_quota_usage:
        return
    try:
        redis = await redis_client()
        extra_global_periods: set[str] = set()
        if db is not None:
            # Sandbox-routed tool calls can run concurrently within a single
            # agent turn (agent_loop.py executes multiple tool calls via
            # asyncio.gather), all sharing that turn's one AsyncSession.
            # SQLAlchemy's AsyncSession forbids concurrent operations on the
            # same session — two simultaneous sandbox calls both resolving
            # their period here would race on the caller's shared `db` and
            # risk "this session is provisioning a new connection; concurrent
            # operations are not permitted" (the same class of bug
            # sub_agent.py's count_active_sub_agent_executions documents and
            # avoids the same way). Resolve on a dedicated, independent
            # session instead of the caller's shared one — `db` here is only
            # a signal that period resolution is possible, not the session
            # actually queried.
            from app.core.database import async_session

            async with async_session() as own_db:
                period, extra_global_periods = await _resolve_agentic_periods(
                    redis, own_db, user_id, period
                )
        await _count_global(redis, "sandbox", {period, *extra_global_periods})
        if user_id is not None:
            key = _usage_key(user_id, "sandbox", None, _current_period_key(period))
            await _increment(redis, key)
            await _count_view(redis, user_id)
    except Exception:
        logger.debug("Failed to increment sandbox quota usage for user %s", user_id, exc_info=True)


async def _decrement(redis: Redis, key: str) -> None:
    """Atomically decrement a usage counter, never dropping below zero.

    A refund must never underflow an existing counter (which would grant free
    usage). Delegates to the shared :func:`app.core.redis.decr_if_positive`
    primitive (also used by the agentic-execution refund) so the atomicity
    guarantee has a single implementation; the key's TTL is preserved, so the
    counter still resets on its period boundary.
    """
    await decr_if_positive(redis, key)


async def refund_agentic_usage(
    user_id: uuid.UUID | None,
    period: str = "monthly",
    db: AsyncSession | None = None,
    agent_id: uuid.UUID | None = None,
) -> None:
    """Reverse a prior :func:`increment_agentic_usage` for one logical request.

    Used when a metered request is ultimately *refused before doing any work*
    (e.g. a Filigran AI extraction with no resolvable enterprise licence), so a
    no-op call is not charged against the agentic quota. Mirrors the exact set
    of counters ``increment_agentic_usage`` touches - the global ones alone for
    a run with no account (*user_id* ``None``) - and floors every counter at
    zero so a double refund (or a refund after the period reset) is harmless.
    """
    redis = await redis_client()
    for key in await _agentic_charge_keys(redis, db, user_id, agent_id, period):
        await _decrement(redis, key)


async def _agentic_charge_keys(
    redis: Redis,
    db: AsyncSession | None,
    user_id: uuid.UUID | None,
    agent_id: uuid.UUID | None,
    period: str,
) -> list[str]:
    """The counters one :func:`increment_agentic_usage` added to, for its
    reversal: the user's (and the agent's) in the resolved period and the
    user's panel counters, when there is a user, then every global period."""
    period, extra_global_periods = await _resolve_agentic_periods(
        redis, db, user_id, period
    )
    period_key = _current_period_key(period)
    keys: list[str] = []
    if user_id is not None:
        keys.append(_usage_key(user_id, "agentic", None, period_key))
        if agent_id is not None:
            keys.append(_usage_key(user_id, "agentic", None, period_key, agent_id))
        keys.extend(key for key, _ttl in _view_usage_keys(user_id))
    keys.append(_global_usage_key("agentic", period_key))
    for ep in sorted(extra_global_periods):
        if ep != period:
            keys.append(_global_usage_key("agentic", _current_period_key(ep)))
    return keys


# Namespace of the pool-clear advisory lock (the first key of
# ``pg_advisory_xact_lock(int4, int4)``). A quota reset holds it for its user and
# a support-code recharge for the platform, from before they delete a pool
# counter until they commit or roll back. A cold claim shares it before it reads
# the durable row (``_load_pool_baseline``).
_POOL_CLEAR_LOCK_NS = 0x5146504C  # "QFPL" - Quota Failure PooL

# How long a cold claim waits for a clear of its pool to end, and how often it
# tries the lock meanwhile. Until the clear commits or rolls back, the pool it
# leaves is unknown (empty, or the durable row); a clear lasting longer leaves
# the claim ungranted, never decided against a guess.
_POOL_CLEAR_WAIT_SECONDS = 30.0
_POOL_CLEAR_POLL_SECONDS = 0.1


def _pool_clear_scope(scope: str | uuid.UUID) -> str:
    return f"{_POOL_USAGE_TYPE}:{scope}"


async def _lock_pool_clear(db: AsyncSession, scope: str | uuid.UUID) -> None:
    """Hold the pool-clear lock of one user, or of the platform
    (``_GLOBAL_REFUND_EPOCH``), until *db* commits."""
    from sqlalchemy import text as sa_text

    await db.execute(
        sa_text("SELECT pg_advisory_xact_lock(CAST(:ns AS integer), hashtext(:scope))"),
        {"ns": _POOL_CLEAR_LOCK_NS, "scope": _pool_clear_scope(scope)},
    )


async def _load_pool_baseline(
    db: AsyncSession | None,
    user_id: uuid.UUID,
    agent_id: uuid.UUID,
    window_key: str,
    usage_type: str = _POOL_USAGE_TYPE,
) -> int | None:
    """Durable count of pool slots already spent in this window, from the DB.

    With *usage_type* ``_FREE_REFUND_USAGE_TYPE``, the free credits counted
    instead: only a quota reset clears those, so only its lock is shared.

    The pool's window is a YEAR. A Redis counter cannot be trusted to survive
    that long — an eviction, a restart without persistence, a failover to a
    cold replica — and a lost counter would silently hand the agent a fresh
    pool. ``flush_quota_usage`` persists the counter with a monotonic
    ``max()``, so the row is a high-water mark: reading it back whenever the
    Redis key is missing is what makes the year-long window real rather than
    best-effort.

    Read only once no quota reset or support-code recharge is clearing this
    pool. A clear deletes the row in a transaction that has not ended, so the
    row still holds the count being cleared, and the pool the clear leaves
    depends on how that transaction ends: empty if it commits, that count if it
    rolls back. Counting from zero meanwhile warmed a key the restored row is
    never read into again, and a rolled-back clear then handed out refunds past
    a spent pool; seeding the row meanwhile left a committed clear's pool spent.
    The clear holds the pool-clear lock until its transaction ends, so this
    waits to share it, up to ``_POOL_CLEAR_WAIT_SECONDS``, rolling *db* back
    between tries so the wait holds neither a lock nor a connection - *db* is
    the refund path's private session, which never writes. ``None`` when the
    clear outlasted the wait: the pool is unknown. Once shared, the lock stays
    with *db*'s transaction, so a clear starting meanwhile waits for this
    claim's seed and sweeps it.
    """
    if db is None:
        return 0
    from sqlalchemy import text as sa_text

    from app.models.quota import QuotaUsage

    user_lock = (
        "pg_try_advisory_xact_lock_shared(CAST(:ns AS integer), hashtext(:user))"
    )
    params = {"ns": _POOL_CLEAR_LOCK_NS, "user": _pool_clear_scope(user_id)}
    if usage_type == _POOL_USAGE_TYPE:
        # A recharge clears every pool, never a free-credit tally.
        lock_sql = (
            "SELECT pg_try_advisory_xact_lock_shared("
            f"CAST(:ns AS integer), hashtext(:platform)) AND {user_lock}"
        )
        params["platform"] = _pool_clear_scope(_GLOBAL_REFUND_EPOCH)
    else:
        lock_sql = f"SELECT {user_lock}"
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _POOL_CLEAR_WAIT_SECONDS
    while True:
        shared = (await db.execute(sa_text(lock_sql), params)).scalar()
        if shared:
            break
        if loop.time() >= deadline:
            return None
        await db.rollback()
        await asyncio.sleep(_POOL_CLEAR_POLL_SECONDS)
    result = await db.execute(
        select(QuotaUsage.count).where(
            QuotaUsage.user_id == user_id,
            QuotaUsage.usage_type == usage_type,
            QuotaUsage.model.is_(None),
            QuotaUsage.period == window_key,
            QuotaUsage.agent_id == agent_id,
        )
    )
    return result.scalar_one_or_none() or 0


async def _claim_failure_pool_slot_and_refund(
    redis: Redis,
    user_id: uuid.UUID,
    agent_id: uuid.UUID,
    db: AsyncSession | None,
    period: str,
    pool: int,
    reason: str,
    cause: str | None = None,
    outcome_out: dict | None = None,
) -> bool:
    """Claim one failure-pool slot and, if granted, refund the run.

    Split out of :func:`refund_agentic_usage_for_failure` so the caller can
    hand it either no session or a private one without duplicating the
    claim logic. The caller has already established that the user's quota is
    one the pool applies to (or could not tell, and defaulted to applying it).
    """
    from app.services.agent_failure_pool import resolve_pool_window

    # The POOL's window is the yearly license anniversary — deliberately not
    # the agentic quota's own daily/monthly period, which is what the refunded
    # counters below live in. Two different clocks on purpose: usage resets
    # every month, the allowance of forgiven failures resets once a year.
    window = await resolve_pool_window(db)
    pool_key = _failure_pool_key(user_id, agent_id, window.key)
    # One script decides the claim against the pool size and hands an
    # over-limit claim straight back, so the counter never settles above it
    # (``_CLAIM_POOL_SLOT_LUA``). A key Redis does not hold is the first claim
    # of the window *as far as Redis knows* - which is also what a lost key
    # looks like - so the durable high-water mark is folded in before any
    # claim is decided, and a wiped cache cannot refill the pool, not even for
    # two claims arriving together.
    claimed, user_epoch, global_epoch = await _claim_pool_slot(
        redis, pool_key, user_id, pool, None
    )
    if claimed < 0:
        baseline = await _load_pool_baseline(db, user_id, agent_id, window.key)
        if baseline is None:
            logger.warning(
                "Agent %s: failed run for user %s is not credited back - a quota "
                "reset or a support-code recharge has been clearing its failure "
                "pool for over %.0fs, so the pool it leaves is not known yet; "
                "the run stays charged%s",
                agent_id,
                user_id,
                _POOL_CLEAR_WAIT_SECONDS,
                f" ({reason})" if reason else "",
            )
            _report_refund_outcome(outcome_out, REFUND_OUTCOME_FAILED)
            return False
        claimed, user_epoch, global_epoch = await _claim_pool_slot(
            redis, pool_key, user_id, pool, baseline
        )
    if claimed > pool:
        logger.info(
            "Agent %s has exhausted its failure quota pool (%d per year, "
            "window %s, renews %s) for user %s — failed run stays charged%s",
            agent_id,
            pool,
            window.key,
            window.end.date().isoformat(),
            user_id,
            f" ({reason})" if reason else "",
        )
        _report_refund_outcome(outcome_out, REFUND_OUTCOME_POOL_EXHAUSTED)
        return False
    await refund_agentic_usage(user_id, period=period, db=db, agent_id=agent_id)
    # Only tallied once the slot is actually granted — an exhausted pool
    # forgives nothing, so counting it here would inflate the breakdown with
    # credits that were never given.
    await _record_refund_cause(
        redis,
        _POOL_USAGE_TYPE,
        window.key,
        cause,
        user_id=user_id,
        counter_key=pool_key,
        granted_at=claimed,
        epoch_token=_cause_epoch_token(_POOL_USAGE_TYPE, user_epoch, global_epoch),
    )
    logger.info(
        "Refunded agentic quota for a failed run of agent %s (user %s, "
        "failure %d/%d of the pool window starting %s)%s",
        agent_id,
        user_id,
        claimed,
        pool,
        window.key,
        f" ({reason})" if reason else "",
    )
    _report_refund_outcome(outcome_out, REFUND_OUTCOME_CREDITED)
    return True


async def refund_agentic_usage_for_failure(
    user_id: uuid.UUID | None,
    agent_id: uuid.UUID,
    db: AsyncSession | None = None,
    period: str = "monthly",
    reason: str = "",
    cause: str | None = None,
    outcome_out: dict | None = None,
) -> bool:
    """Refund a *failed* agent run from that agent's bounded failure pool.

    An agentic execution is metered before the agent runs, so a run that ends
    in failure has already spent one action of the user's agentic action quota.
    This credits that action back for the first ``AGENT_FAILURE_QUOTA_POOL``
    failures of this ``(user, agent)`` pair in the current pool window, and
    lets every failure after that spend its action — a bounded pool rather than
    an attempt to tell a provider outage from a hopeless prompt, which no
    signal available at these call sites can do reliably. A permanently broken
    agent therefore burns its pool once and consumes the quota normally from
    then on.

    The pool window is one YEAR, on the Filigran license anniversary
    (``agent_failure_pool.resolve_pool_window``) — not the agentic quota's own
    daily/monthly period, which is what the refunded counters live in. An admin
    can recharge every pool before that anniversary by redeeming a
    Filigran-signed support code (Settings -> Quotas).

    Returns True when the refund was granted (callers log/trace it), False
    when the feature is off, the pool is exhausted, or the refund could not
    be applied.

    Never raises. Every call site is already handling a failure, and losing
    the real error — or breaking the caller's session — because of a hiccup in
    the refund path would be strictly worse than not refunding.

    *db* is only a **signal** that period resolution is possible, never the
    session used: every caller is on an error path where its session may hold
    a failed transaction (an assignment run about to be marked ``failed``, a
    chat turn unwinding), so resolution runs on a private session — the same
    reason, and the same shape, as ``increment_sandbox_usage``.

    The pool slot is claimed *before* the refund and is deliberately NOT
    handed back if the refund itself then fails: a returned slot plus a
    half-applied refund could over-refund on the next failure, and a lost slot
    cannot.

    *cause* is the structured :class:`~app.services.failure_causes.FailureCause`
    behind the failure. Recorded against the credit so Settings ->
    Quotas can break its headline totals down by what was actually forgiven,
    instead of showing two unexplained numbers. It never changes whether the
    credit is granted — phase 1 measures only.

    **A user whose agentic quota is unlimited is never pooled**. The
    counters are incremented whether or not anything caps them, so a failed run
    always LOOKS charged - but when no tier enforces a limit
    (:func:`has_enforceable_agentic_limit`) that charge cost nothing, a refund
    would give back nothing, and a pool rationing refunds rations nothing. The
    old behaviour claimed the slot anyway and, five failures later, logged
    "exhausted its failure quota pool - failed run stays charged" on a
    platform that has no quota, which is a quota alarm about a quota that does
    not exist. Such a run returns False WITHOUT touching the pool (outcome
    ``quota_unlimited``). The check needs a session: with ``db=None`` the
    question cannot be answered and the pool applies as it always did.

    **A run with no account behind it is never pooled either**
    *(user_id* ``None``): the pool is a per-user allowance, so the failure
    keeps the platform-wide charge it made (outcome ``no_account``). A run of
    that kind that never reached a model goes through
    :func:`refund_agentic_usage_for_provider_outage` like any other.

    *outcome_out*, when provided, is mutated in place with
    ``{"outcome": <REFUND_OUTCOME_*>}`` so a caller writing a trace can tell
    "the pool is spent, the charge stands" apart from "there was nothing to
    give back" - two opposite readings of the same ``False``.
    """
    if user_id is None:
        logger.info(
            "Agent %s: failed run with no account behind it is not credited "
            "back - the failure pool is a per-user allowance, so the run keeps "
            "its platform-wide charge%s",
            agent_id,
            f" ({reason})" if reason else "",
        )
        _report_refund_outcome(outcome_out, REFUND_OUTCOME_NO_ACCOUNT)
        return False
    pool = agent_failure_pool_size()
    if pool <= 0:
        _report_refund_outcome(outcome_out, REFUND_OUTCOME_POOL_DISABLED)
        return False
    try:
        redis = await redis_client()
        if db is None:
            return await _claim_failure_pool_slot_and_refund(
                redis,
                user_id,
                agent_id,
                None,
                period,
                pool,
                reason,
                cause,
                outcome_out=outcome_out,
            )
        from app.core.database import async_session

        async with async_session() as own_db:
            if not await has_enforceable_agentic_limit(own_db, redis, user_id):
                logger.info(
                    "Agent %s: failed run for user %s is not credited back - "
                    "the user's agentic quota is unlimited, so the action cost "
                    "nothing and the failure pool is not consumed%s",
                    agent_id,
                    user_id,
                    f" ({reason})" if reason else "",
                )
                _report_refund_outcome(outcome_out, REFUND_OUTCOME_QUOTA_UNLIMITED)
                return False
            return await _claim_failure_pool_slot_and_refund(
                redis,
                user_id,
                agent_id,
                own_db,
                period,
                pool,
                reason,
                cause,
                outcome_out=outcome_out,
            )
    except Exception:
        logger.warning(
            "Failure-pool refund failed for agent %s / user %s — the failed "
            "run stays charged",
            agent_id,
            user_id,
            exc_info=True,
        )
        _report_refund_outcome(outcome_out, REFUND_OUTCOME_FAILED)
        return False


async def refund_agentic_usage_for_provider_outage(
    user_id: uuid.UUID | None,
    agent_id: uuid.UUID,
    db: AsyncSession | None = None,
    period: str = "monthly",
    reason: str = "",
    cause: str | None = None,
) -> bool:
    """Credit an action back for a run that never reached a model — for free.

    The sibling of :func:`refund_agentic_usage_for_failure`, and deliberately
    NOT bounded by the per-agent failure pool: when the provider is unreachable,
    down, or not configured (``llm.router.is_provider_unavailable_error``), no
    model was called, so there is nothing to charge and nothing to ration. A
    pool slot spent here would let a provider outage exhaust an agent's whole
    allowance — the run's own failures would then start costing the user actions
    for an incident on the platform's side.

    Free is still counted: the tally is written to
    ``_FREE_REFUND_USAGE_TYPE`` so Settings -> Quotas can report the two credit
    reasons apart ("agent failure" vs "provider unavailable") instead of a
    single unexplained number. A run with no account behind it (*user_id*
    ``None``) gets the global counters it was charged to back and is
    logged, not tallied: the card counts credits per user.

    Returns True when the action was credited back. Never raises, and never
    touches the caller's session — same contract, and same reasons, as its
    sibling.

    *cause* is the structured :class:`~app.services.failure_causes.FailureCause`
    behind the failure. Recorded against the credit so Settings ->
    Quotas can break its headline totals down by what was actually forgiven,
    instead of showing two unexplained numbers. It never changes whether the
    credit is granted — phase 1 measures only.
    """
    try:
        redis = await redis_client()
        if db is None:
            return await _apply_free_refund(
                redis, user_id, agent_id, None, period, reason, cause
            )
        from app.core.database import async_session

        async with async_session() as own_db:
            return await _apply_free_refund(
                redis, user_id, agent_id, own_db, period, reason, cause
            )
    except Exception:
        logger.warning(
            "Free (provider-outage) refund failed for agent %s / user %s — the "
            "failed run keeps its action charged",
            agent_id,
            user_id,
            exc_info=True,
        )
        return False


async def _apply_free_refund(
    redis: Redis,
    user_id: uuid.UUID | None,
    agent_id: uuid.UUID,
    db: AsyncSession | None,
    period: str,
    reason: str,
    cause: str | None = None,
) -> bool:
    """Reverse the action and bump the free-credit tally."""
    await refund_agentic_usage(user_id, period=period, db=db, agent_id=agent_id)
    return await _tally_free_credit(redis, user_id, agent_id, db, reason, cause)


async def _tally_free_credit(
    redis: Redis,
    user_id: uuid.UUID | None,
    agent_id: uuid.UUID,
    db: AsyncSession | None,
    reason: str,
    cause: str | None,
) -> bool:
    """Count one free credit, its counters already given back, on the card."""
    from app.services.agent_failure_pool import resolve_pool_window

    if user_id is None:
        logger.info(
            "Credited an action back for free (no model reached) on agent %s "
            "for a run with no account behind it - its platform-wide charge is "
            "given back%s",
            agent_id,
            f" ({reason})" if reason else "",
        )
        return True
    # Tallied in the POOL's window, not the quota period: the card shows both
    # reasons in one place, so they have to be counted over the same span.
    window = await resolve_pool_window(db)
    free_key = _free_refund_key(user_id, agent_id, window.key)
    tally, user_epoch = await _count_free_credit(redis, free_key, user_id)
    if tally < 0:
        # A counter Redis lost after its flush restarts from its durable row,
        # or the card's total stops counting while the cause tally does not.
        baseline = await _load_pool_baseline(
            db, user_id, agent_id, window.key, usage_type=_FREE_REFUND_USAGE_TYPE
        )
        if baseline is None:
            # A reset of this user outlasted the wait, so the row it leaves is
            # unknown. The action is already credited back; the counter stays
            # cold and no cause is tallied, so the card is one credit short
            # rather than stalled below a tally that keeps counting.
            logger.warning(
                "Credited an action back for free on agent %s for user %s, but "
                "a quota reset has been clearing their counters for over %.0fs: "
                "this credit is not counted on the card%s",
                agent_id,
                user_id,
                _POOL_CLEAR_WAIT_SECONDS,
                f" ({reason})" if reason else "",
            )
            return True
        tally, user_epoch = await _count_free_credit(
            redis, free_key, user_id, baseline
        )
    await _record_refund_cause(
        redis,
        _FREE_REFUND_USAGE_TYPE,
        window.key,
        cause,
        user_id=user_id,
        counter_key=free_key,
        granted_at=tally,
        epoch_token=_cause_epoch_token(_FREE_REFUND_USAGE_TYPE, user_epoch, 0),
    )
    logger.info(
        "Credited an action back for free (no model reached) on agent %s "
        "for user %s — %d such credit(s) in the window starting %s%s",
        agent_id,
        user_id,
        tally,
        window.key,
        f" ({reason})" if reason else "",
    )
    # Soft ceiling. The free path is unbounded BY DESIGN — one outage must not
    # be able to exhaust an agent's allowance — but "unbounded" and "unwatched"
    # are different things: a provider stuck down for a week means unlimited
    # free actions for that week, and nothing would say so until someone
    # happened to open Settings -> Quotas. Log loudly instead of refusing;
    # refusing would charge users for an incident that is still ours.
    ceiling = agent_failure_pool_size() * _FREE_CREDIT_ALERT_FACTOR
    if ceiling and tally in (ceiling, ceiling * 10):
        logger.error(
            "Agent %s has had %d actions credited back for free this window "
            "(%dx the pool size) for user %s — a provider or worker problem "
            "has been running unattended. Free credits are deliberately "
            "unbounded; this is an alert, not a refusal.",
            agent_id,
            tally,
            tally // max(agent_failure_pool_size(), 1),
            user_id,
        )
    return True


async def refund_agentic_usage_by_receipt(
    receipt_token: str,
    reason: str = "",
    db: AsyncSession | None = None,
) -> bool:
    """Credit back the action a receipt attests to, exactly once.

    The entry point for abandonment paths that run in a different process than
    the charge: a sub-agent task whose worker died mid-turn is failed by the
    orchestration engine, and nothing in that turn will ever run again.

    Redeeming the receipt is what makes it exactly-once
    (``_REDEEM_RECEIPT_LUA``): it is taken and marked redeemed atomically, so
    two dead-letter passes over the same job (a retried reconciler cycle, two
    replicas), the re-run that replaces a lost attempt and that attempt's own
    failure handler cannot credit it twice, and a job with no receipt — never
    charged, or already credited — quietly gets nothing. A token with no
    receipt is closed as well: a charge made under it later is given back as
    it lands.

    Always FREE, never from the agent's failure pool: every route to an
    abandoned job is infrastructure (lease expired, process killed, deadline
    blown). The agent did not fail; it was never allowed to finish.

    Never raises. Returns True when an action was credited back.
    """
    try:
        redis = await redis_client()
        state, raw = await _redeem_receipt(redis, receipt_token)
        if state != RECEIPT_TAKEN or not raw:
            return False
        receipt = json.loads(raw)
        # ``null`` is a charge with no account behind it: the global
        # counters alone are given back.
        user_raw = receipt["user_id"]
        user_id = uuid.UUID(str(user_raw)) if user_raw is not None else None
        agent_raw = receipt.get("agent_id")
        agent_id = uuid.UUID(str(agent_raw)) if agent_raw else None
        period = str(receipt.get("period") or "monthly")
        if agent_id is None:
            # Nothing to attribute the free tally to; still reverse the charge,
            # which is the part that matters to the user.
            await refund_agentic_usage(user_id, period=period, db=db)
            logger.info(
                "Credited an action back for abandoned work %s (user %s)%s",
                receipt_token,
                user_id,
                f" — {reason}" if reason else "",
            )
            return True
        # *db* is forwarded only so the pool WINDOW resolves against the real
        # licence anniversary rather than the calendar-year fallback — the
        # helper never touches the session itself (it opens a private one),
        # which is what keeps this safe on a failure path.
        await refund_agentic_usage_for_provider_outage(
            user_id,
            agent_id,
            db=db,
            period=period,
            reason=reason or f"abandoned work {receipt_token}",
        )
        return True
    except Exception:
        logger.warning(
            "Receipt-based refund failed for %s — the action stays charged",
            receipt_token,
            exc_info=True,
        )
        return False


async def claim_own_receipt(receipt_token: str) -> bool:
    """The turn that made a receipted charge is about to credit it back itself
    (its failure handler): redeem the receipt first, so no other path can.

    ``False`` when another path already redeemed it - the re-run that replaced
    this attempt after its lease was lost, or the dead-letter: the
    charge was credited there and the caller must not credit it again.
    ``True`` when the caller took it, or when none was ever written (the
    charge still happened, and nothing else can credit it).

    Never raises: a store that cannot be read answers ``False``, so the charge
    stays as it is rather than risk being credited twice.
    """
    try:
        redis = await redis_client()
        state, _ = await _redeem_receipt(redis, receipt_token)
    except Exception:
        logger.warning(
            "Could not redeem metering receipt %s - the failed turn is not "
            "credited back",
            receipt_token,
            exc_info=True,
        )
        return False
    if state == RECEIPT_ALREADY_REDEEMED:
        logger.info(
            "Failed turn of %s not credited back again: its charge was already "
            "credited by the orchestration engine",
            receipt_token,
        )
        return False
    return True


async def credit_agentic_charge_once(
    receipt_token: str,
    user_id: uuid.UUID | None,
    agent_id: uuid.UUID,
    db: AsyncSession | None = None,
    period: str = "monthly",
    reason: str = "",
    cause: str | None = None,
) -> int:
    """Credit back, free and at most once, the charge *receipt_token* names.

    For a charge with no receipt written at charge time - an assignment run's,
    recorded on its row: the engine and the
    dead letter may both reach it, and a worker may die at any point between
    them. The counters and the receipt move in ONE script
    (``_CREDIT_CHARGE_ONCE_LUA``), so there is no state in which the credit
    applied and the receipt does not say so, or the reverse:

    - ``CHARGE_CREDIT_LANDED``: this call gave the counters back and closed
      the receipt; the free credit is then tallied, best-effort (a tally that
      fails leaves the card one credit short, never the user charged).
    - ``CHARGE_CREDIT_ALREADY``: the receipt reads credited, nothing moved.
    - ``CHARGE_CREDIT_NOT_APPLIED``: the script did not run, or its answer was
      lost. Nothing moved, or (an answer lost after the script ran) the
      receipt reads credited, and the next call answers ``ALREADY``: the
      caller keeps its record and the next path tries again.

    Never raises.
    """
    try:
        redis = await redis_client()
        if db is None:
            keys = await _agentic_charge_keys(redis, None, user_id, agent_id, period)
        else:
            from app.core.database import async_session

            async with async_session() as own_db:
                keys = await _agentic_charge_keys(redis, own_db, user_id, agent_id, period)
        state = int(
            await redis.eval(
                _CREDIT_CHARGE_ONCE_LUA,
                1 + len(keys),
                _receipt_key(receipt_token),
                *keys,
                _CHARGE_CREDITED,
                _RECEIPT_TTL,
            )
        )
    except Exception:
        logger.warning(
            "Credit of charge %s failed before it applied - the charge stays "
            "recorded for the next path",
            receipt_token,
            exc_info=True,
        )
        return CHARGE_CREDIT_NOT_APPLIED
    if state == CHARGE_CREDIT_ALREADY:
        logger.info("Charge %s was already credited back, not again", receipt_token)
        return CHARGE_CREDIT_ALREADY
    try:
        if db is None:
            await _tally_free_credit(redis, user_id, agent_id, None, reason, cause)
        else:
            from app.core.database import async_session

            async with async_session() as own_db:
                await _tally_free_credit(redis, user_id, agent_id, own_db, reason, cause)
    except Exception:
        logger.warning(
            "Charge %s was credited back, but its free credit could not be "
            "counted on the card",
            receipt_token,
            exc_info=True,
        )
    return CHARGE_CREDIT_LANDED


async def get_user_quota_usage(
    db: AsyncSession,
    user_id: uuid.UUID,
) -> list[dict]:
    """Return current-period usage entries for a user from Redis.

    Redis is the sole source for this read path — missing keys read as 0
    via ``_get_count`` (every increment refreshes the ``_USAGE_KEY_TTL``
    expiry, which covers daily/monthly buckets with buffer). The
    ``quota_usage`` DB table is a durability sink fed by
    ``flush_quota_usage`` and serves history / admin monitoring, not this
    per-user display. The one exception is the platform-wide pool, read back
    from ``quota_global_usage`` like the gate reads it. Returns
    ``{usage_type, model, period, count}`` dicts.
    """
    redis = await redis_client()
    entries: list[dict] = []
    agentic_period_key = _current_period_key("monthly")

    # Resolve the effective config to know which periods to query.
    # When no quota config exists (source=none), counters can still be
    # incremented in Redis using the default monthly bucket. Return those
    # values so UI usage indicators keep working in unlimited/PoC mode.
    for qt in _QUOTA_TYPES:
        config = await resolve_quota_config(db, redis, user_id, qt)
        period = "monthly" if not config else config.get("period", "monthly")
        period_key = _current_period_key(period)
        if qt == "agentic":
            agentic_period_key = period_key

        # A ``global`` agentic config (the included allowance of an Enterprise
        # Edition inherited from a connected EE platform / any Filigran license
        # that ships platform-wide quotas) is enforced as a SHARED TOTAL across
        # all users and every entry point — chat, assignment runs, and
        # connector-driven runs such as OpenCTI import-doc — against the global
        # counter; the per-user limit is deliberately skipped (see
        # ``check_agentic_quota``: ``if config.get("global"): return``).
        # Report that shared-pool usage rather than this user's slice, so the
        # gauge reflects what actually gates the user and stays aligned with the
        # admin monitoring view (which sums all users). ``scope`` lets the UI
        # label it platform-wide.
        #
        # The pool is the exact numerator ``check_agentic_quota`` compares
        # against the ceiling: global agentic + global sandbox, read
        # the way the gate reads it, from its durable count when Redis lost
        # the counter. Sandbox has no row of its own in the usage
        # display (spec 002-sandbox-quota-usage-tracking FR-013/FR-014), so it
        # is folded in here as it is per user; the admin monitoring view keeps
        # the two apart.
        if qt == "agentic" and config and config.get("global"):
            pool_count = await _get_global_count(
                redis, "agentic", period_key
            ) + await _get_global_count(redis, "sandbox", period_key)
            entries.append(
                {
                    "usage_type": qt,
                    "model": None,
                    "period": period_key,
                    "count": pool_count,
                    "scope": "global",
                }
            )
            continue

        # Total counter (sums new + legacy key shapes across the deploy window)
        total_count = await _get_user_count(redis, user_id, qt, None, period_key)
        entries.append(
            {
                "usage_type": qt,
                "model": None,
                "period": period_key,
                "count": total_count,
                "scope": "user",
            }
        )

        # Per-model counters (only for llm_proxy)
        if qt == "llm_proxy" and config and config.get("per_model"):
            for model_name in config["per_model"]:
                model_count = await _get_user_count(
                    redis, user_id, qt, model_name, period_key
                )
                entries.append(
                    {
                        "usage_type": qt,
                        "model": model_name,
                        "period": period_key,
                        "count": model_count,
                    }
                )

    # Sandbox has no config/period of its own — it always shares agentic's
    # resolved period (spec FR-011/FR-017, revised), so the counter written
    # by increment_sandbox_usage is always found under agentic_period_key.
    sandbox_count = await _get_user_count(
        redis, user_id, "sandbox", None, agentic_period_key
    )
    entries.append(
        {
            "usage_type": "sandbox",
            "model": None,
            "period": agentic_period_key,
            "count": sandbox_count,
        }
    )

    return entries


async def get_agentic_quota_periods(
    db: AsyncSession,
    user_id: uuid.UUID,
    now: datetime | None = None,
) -> list[dict]:
    """The rows of the composer's quota panel: today, this month, this year.

    One ``{period, used, limit, scope, resets_at}`` per :data:`QUOTA_VIEW_PERIODS`,
    read the way :func:`check_agentic_quota` reads what gates the user in that
    period, agentic and sandbox together:

    * the user's own limit (user > group > platform default), in the period it
      is set in: ``used`` is the gate's numerator, scope ``user``;
    * else a platform-wide cap in that period (the license ceiling in XTM One
      mode, ``platform_limit`` in Copilot mode, or the ``global`` config that
      stands in for the per-user one): ``used`` is the shared pool, scope
      ``global``;
    * else no limit (``limit`` ``None``): ``used`` is the user's own count in
      that period.

    A pure read: no counter moves. The per-user count of a period with no
    limit comes from the panel counters (``agentic_view``), which start at the
    deploy that added them; the resolved period is read from the enforced
    counters, which are exact.
    """
    redis = await redis_client()
    now = now or _utcnow()
    config = await resolve_quota_config(db, redis, user_id, "agentic") or {}
    resolved_period = config.get("period", "monthly")
    own_limit = None if config.get("global") else config.get("default")

    platform_caps: dict[str, int] = {}
    if settings.is_xtm_one:
        platform_caps = dict(await get_xtm_agentic_ceilings(db) or {})
    else:
        platform_cfg = await _load_platform_wide_config(db, "agentic")
        if platform_cfg:
            cap = ceiling_or_none(platform_cfg.get("platform_limit"))
            if cap is not None:
                platform_caps[platform_cfg.get("period", "monthly")] = cap
    if config.get("global") and config.get("default") is not None:
        platform_caps.setdefault(resolved_period, config["default"])

    rows: list[dict] = []
    for period in QUOTA_VIEW_PERIODS:
        period_key = _current_period_key(period, now)
        resets = now + timedelta(seconds=seconds_until_period_reset(period, now))
        if own_limit is not None and period == resolved_period:
            used = await _get_user_count(redis, user_id, "agentic", None, period_key) + await _get_user_count(
                redis, user_id, "sandbox", None, period_key
            )
            limit, scope = own_limit, "user"
        elif period in platform_caps:
            used = await _get_global_count(redis, "agentic", period_key) + await _get_global_count(
                redis, "sandbox", period_key
            )
            limit, scope = platform_caps[period], "global"
        else:
            if period == resolved_period:
                used = await _get_user_count(redis, user_id, "agentic", None, period_key) + await _get_user_count(
                    redis, user_id, "sandbox", None, period_key
                )
            else:
                used = await _get_count(redis, _usage_key(user_id, _VIEW_USAGE_TYPE, None, period_key))
            limit, scope = None, "user"
        rows.append(
            {
                "period": period,
                "used": int(used),
                "limit": limit,
                "scope": scope,
                "resets_at": resets.strftime("%Y-%m-%dT%H:%M:%SZ"),
            }
        )
    return rows


async def platform_agentic_usage(db: AsyncSession, now: datetime | None = None) -> list[dict[str, Any]]:
    """The platform-wide agentic ceilings and their use: one ``{period, used, limit}`` per capped period.

    The ceilings :func:`check_agentic_quota` enforces for the whole platform (the
    license ceilings in XTM One mode, ``platform_limit`` in Copilot mode), each
    with the shared agentic plus sandbox count of its current period. Empty when
    no platform-wide ceiling is set. A pure read: no counter moves (a counter
    Redis lost is put back at its durable count, as every gate reads it).
    """
    now = now or _utcnow()
    caps: dict[str, int] = {}
    if settings.is_xtm_one:
        caps = dict(await get_xtm_agentic_ceilings(db) or {})
    else:
        platform_cfg = await _load_platform_wide_config(db, "agentic")
        if platform_cfg:
            cap = ceiling_or_none(platform_cfg.get("platform_limit"))
            if cap is not None:
                caps[platform_cfg.get("period", "monthly")] = cap
    if not caps:
        return []
    redis = await redis_client()
    rows: list[dict[str, Any]] = []
    for period, limit in caps.items():
        if limit <= 0:
            continue
        period_key = _current_period_key(period, now)
        used = await _get_global_count(redis, "agentic", period_key) + await _get_global_count(
            redis, "sandbox", period_key
        )
        rows.append({"period": period, "used": int(used), "limit": int(limit)})
    return rows


async def llm_proxy_quota_usage(
    db: AsyncSession,
    user_id: uuid.UUID,
    now: datetime | None = None,
) -> dict:
    """The caller's current ``llm_proxy`` usage, read the way ``check_llm_proxy_quota`` reads it.

    ``{used, limit, period, period_key, resets_at, per_model}``: ``limit`` is the
    resolved ``default`` (``None``: no limit), ``per_model`` maps each model with a
    limit of its own to ``{used, limit}``. A pure read: no counter moves, so a lab
    retest can prove that a step spent no LLM call (``GET /v1/quota`` on the proxy).
    """
    redis = await redis_client()
    config = await resolve_quota_config(db, redis, user_id, "llm_proxy") or {}
    period = config.get("period", "monthly")
    now = now or _utcnow()
    period_key = _current_period_key(period, now)
    per_model = {
        model: {
            "used": await _get_user_count(redis, user_id, "llm_proxy", model, period_key),
            "limit": limit,
        }
        for model, limit in (config.get("per_model") or {}).items()
    }
    resets = now + timedelta(seconds=seconds_until_period_reset(period, now))
    return {
        "used": await _get_user_count(redis, user_id, "llm_proxy", None, period_key),
        "limit": config.get("default"),
        "period": period,
        "period_key": period_key,
        "resets_at": resets.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "per_model": per_model,
    }


# ---------------------------------------------------------------------------
# Flush Redis counters to PostgreSQL
# ---------------------------------------------------------------------------

# Retry policy for transient lock/timeout contention during flush.
# Up to _FLUSH_MAX_ATTEMPTS attempts. The wait before retry N is the
# exponential base BASE * 2**(N-1) (capped at _FLUSH_RETRY_MAX_DELAY), then
# jittered to a random point in [0.75x, 1.25x] of that base and re-clamped
# to the cap, so concurrent workers don't retry in synchronized waves (see
# _compute_flush_retry_delay). With 5 attempts that is up to four waits
# before the final attempt (the final attempt is not followed by a sleep).
# Redis data is persistent between retries, so no usage data is lost while
# retrying - the flush just runs again on the same snapshot.
_FLUSH_MAX_ATTEMPTS = 5
_FLUSH_RETRY_BASE_DELAY = 1.0
_FLUSH_RETRY_MAX_DELAY = 8.0
_FLUSH_RETRY_JITTER_MIN_FACTOR = 0.75
_FLUSH_RETRY_JITTER_MAX_FACTOR = 1.25
_FLUSH_RETRY_RANDOM = secrets.SystemRandom()

# PostgreSQL SQLSTATEs that indicate transient lock/timeout contention and
# are therefore safe to retry:
#   * ``55P03`` lock_not_available — SELECT FOR UPDATE NOWAIT couldn't lock.
#   * ``57014`` query_canceled     — statement_timeout fired on UPDATE.
_RETRYABLE_FLUSH_SQLSTATES = frozenset({"55P03", "57014"})


def _is_retryable_flush_error(exc: BaseException) -> bool:
    """Return ``True`` when *exc* is transient lock/timeout contention.

    Walks the full exception chain looking for a PostgreSQL SQLSTATE
    (asyncpg exposes it as ``sqlstate``/``pgcode``; SQLAlchemy wraps it) and
    matches ``55P03`` (lock_not_available) or ``57014`` (query_canceled).
    Falls back to a string probe for robustness across driver/wrapper versions.
    """
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        for attr in ("sqlstate", "pgcode"):
            code = getattr(cur, attr, None)
            if isinstance(code, str) and code in _RETRYABLE_FLUSH_SQLSTATES:
                return True
        cur = cur.__cause__ or cur.__context__
    blob = str(exc).lower()
    return (
        "lock_not_available" in blob
        or "55p03" in blob
        or "57014" in blob
        or "canceling statement due to statement timeout" in blob
        or "canceling statement due to lock timeout" in blob
    )


def _compute_flush_retry_delay(attempt: int) -> float:
    """Return exponential backoff delay with jitter for a retry attempt.

    The jitter window is clamped to ``_FLUSH_RETRY_MAX_DELAY`` *before*
    sampling rather than clamping the sampled value afterwards: when the
    exponential base is at the cap, ``base * MAX_FACTOR`` exceeds the cap, and
    a post-sampling ``min(..., cap)`` would collapse every over-cap sample
    onto an exactly-cap point mass — losing the spreading that the jitter is
    there to provide. Sampling within a pre-clamped window keeps the spread.
    """
    base_delay = min(
        _FLUSH_RETRY_BASE_DELAY * (2 ** (attempt - 1)),
        _FLUSH_RETRY_MAX_DELAY,
    )
    low = max(0.0, base_delay * _FLUSH_RETRY_JITTER_MIN_FACTOR)
    high = min(base_delay * _FLUSH_RETRY_JITTER_MAX_FACTOR, _FLUSH_RETRY_MAX_DELAY)
    high = max(low, high)
    return _FLUSH_RETRY_RANDOM.uniform(low, high)


# Cluster-wide advisory-lock key that serializes quota flushes across replicas.
# Every web replica runs the autonomous scheduler and flushes the SAME Redis
# counters on the same cadence; without serialization their per-row
# ``SELECT ... FOR UPDATE NOWAIT`` upserts collide and raise 55P03
# (lock_not_available) on the losers every cycle. We grab a transaction-scoped
# advisory lock so only ONE replica flushes at a time; the ``_xact`` scope is
# released automatically on COMMIT/ROLLBACK, so the lock can never leak back
# into the pooled connection. Single-key ``pg_advisory_xact_lock(bigint)`` space
# (separate from the (int4, int4) locks taken elsewhere); derived from an ASCII
# tag so it can't collide with the small migration/bootstrap lock IDs.
_QUOTA_FLUSH_LOCK_KEY = int.from_bytes(b"qtaflsh", "big")

# Advisory-lock key of the write of the platform-wide counters to
# ``quota_global_usage`` (``_flush_global_usage``), the one their read-back
# waits on (``_read_global_baseline``). Not the flush lock: the per-user rows
# hold that one for as long as they take, and the read-back runs inside a quota
# check, which cannot wait that long.
_QUOTA_GLOBAL_FLUSH_LOCK_KEY = int.from_bytes(b"qtaglbl", "big")

# Set by a quota reset or a support-code recharge while it waits for the flush
# lock. A flush holding the lock checks it once it has the lock and
# every ``_FLUSH_YIELD_CHECK_ROWS`` rows, and rolls back when it is set: the
# counters are durable in Redis, so the next cycle writes them, and the clear
# gets the lock instead of outwaiting a long flush. Outside ``_USAGE_PREFIX``
# (the flush GETs every key there), and it expires on its own, so a clear that
# stopped waiting holds no flush off for long.
_FLUSH_YIELD_KEY = "quota:flush_yield"
_FLUSH_YIELD_TTL_MS = 2_000
_FLUSH_YIELD_CHECK_ROWS = 50


class _FlushYieldError(Exception):
    """A quota reset or a support-code recharge is waiting for the flush lock."""


async def _clear_waiting(redis: Redis) -> bool:
    return bool(await redis.exists(_FLUSH_YIELD_KEY))


# Counters a quota reset or a support-code recharge took out of the live
# keyspace and has not committed yet, one hash per clear: the field is
# the counter's key, the value ``<count>:<pttl ms>:<seed>``, the seed being the
# durable row a cold claim seeds that counter from once the clear's locks are
# gone (0 for a counter nothing seeds). A clear moves its counters here instead
# of deleting them, drops the hash once its transaction has committed and puts
# them back when it has not: deleted before a commit that then failed, the
# increments only Redis held were lost. ``_CLEAR_STASH_INDEX`` maps each stash
# to the transaction that made it, so whoever next holds the flush lock settles
# a stash its clear could not (a dead process, a Redis error on the way out)
# from PostgreSQL's own record of how that transaction ended. Outside
# ``_USAGE_PREFIX``, like every hash.
_CLEAR_STASH_PREFIX = "quota:clear_stash:"
_CLEAR_STASH_INDEX = "quota:clear_stashes"
_CLEAR_STASH_TTL = 86_400 * 30
_CLEAR_STASH_BATCH = 500

# KEYS[1] the stash, KEYS[2..] counters; ARGV[1] the stash TTL, ARGV[i] the seed
# of KEYS[i]. A counter moved twice (a scan may return a key twice, and a usage
# counter can be recreated meanwhile) adds what it gained over its seed to what
# the stash already holds: the seed is in the first count already.
_STASH_COUNTERS_LUA = """
local moved = 0
for i = 2, #KEYS do
    local count = tonumber(redis.call('GET', KEYS[i]) or '')
    if count then
        local pttl, seed = redis.call('PTTL', KEYS[i]), tonumber(ARGV[i]) or 0
        local held = redis.call('HGET', KEYS[1], KEYS[i])
        if held then
            local prior, prior_pttl, prior_seed = string.match(
                held, '^(-?%d+):(-?%d+):(-?%d+)$'
            )
            if prior then
                count = tonumber(prior) + count - seed
                pttl, seed = tonumber(prior_pttl), tonumber(prior_seed)
            end
        end
        redis.call(
            'HSET', KEYS[1], KEYS[i],
            string.format('%d:%d:%d', count, pttl, seed)
        )
        moved = moved + 1
    end
    redis.call('DEL', KEYS[i])
end
redis.call('EXPIRE', KEYS[1], ARGV[1])
return moved
"""

# KEYS[1] the stash, KEYS[2..] counters it holds. A counter that exists again
# was recreated after the clear (a usage increment) or seeded from its durable
# row once the clear's locks were gone: the stashed count less that seed is
# what it is missing.
_RESTORE_STASHED_LUA = """
local restored = 0
for i = 2, #KEYS do
    local held = redis.call('HGET', KEYS[1], KEYS[i])
    if held then
        local count, pttl, seed = string.match(held, '^(-?%d+):(-?%d+):(-?%d+)$')
        if count then
            count, pttl, seed = tonumber(count), tonumber(pttl), tonumber(seed)
            if redis.call('EXISTS', KEYS[i]) == 1 then
                redis.call('INCRBY', KEYS[i], count - seed)
            elseif pttl > 0 then
                redis.call('SET', KEYS[i], count, 'PX', pttl)
            else
                redis.call('SET', KEYS[i], count)
            end
            restored = restored + 1
        end
        redis.call('HDEL', KEYS[1], KEYS[i])
    end
end
return restored
"""

# How a stashing transaction ended: 'committed', 'aborted', 'in progress', or
# NULL once it is older than PostgreSQL still records. An id from the future
# (never one of this cluster's) reads NULL instead of raising, which would
# abort the transaction asking.
_XACT_STATUS_SQL = (
    "SELECT CASE WHEN CAST(CAST(:xid AS text) AS xid8)"
    " < pg_snapshot_xmax(pg_current_snapshot())"
    " THEN pg_xact_status(CAST(CAST(:xid AS text) AS xid8)) END"
)


def _clear_stash_key(token: str) -> str:
    return f"{_CLEAR_STASH_PREFIX}{token}"


async def _open_clear_stash(redis: Redis, db: AsyncSession) -> str:
    """Register the stash of the clear running in *db*'s transaction."""
    from sqlalchemy import text as sa_text

    xid = (
        await db.execute(sa_text("SELECT CAST(pg_current_xact_id() AS text)"))
    ).scalar()
    token = uuid.uuid4().hex
    await redis.hset(_CLEAR_STASH_INDEX, token, str(xid))
    return token


async def _stash_counters(
    redis: Redis, token: str, keys: list[str], seeds: dict[str, int]
) -> int:
    """Move *keys* into the stash *token*; how many held a count."""
    if not keys:
        return 0
    return int(
        await redis.eval(
            _STASH_COUNTERS_LUA,
            1 + len(keys),
            _clear_stash_key(token),
            *keys,
            _CLEAR_STASH_TTL,
            *(seeds.get(key, 0) for key in keys),
        )
    )


async def _drop_clear_stash(redis: Redis, token: str) -> None:
    await redis.delete(_clear_stash_key(token))
    await redis.hdel(_CLEAR_STASH_INDEX, token)


async def _restore_clear_stash(redis: Redis, token: str) -> int:
    """Put every counter of the stash *token* back; how many were."""
    stash = _clear_stash_key(token)
    restored = 0
    batch: list[str] = []
    async for field, _held in redis.hscan_iter(stash, count=_CLEAR_STASH_BATCH):
        batch.append(_as_text(field))
        if len(batch) >= _CLEAR_STASH_BATCH:
            restored += int(
                await redis.eval(_RESTORE_STASHED_LUA, 1 + len(batch), stash, *batch)
            )
            batch.clear()
    if batch:
        restored += int(
            await redis.eval(_RESTORE_STASHED_LUA, 1 + len(batch), stash, *batch)
        )
    await _drop_clear_stash(redis, token)
    return restored


async def _settle_clear_stashes(redis: Redis, db: AsyncSession) -> None:
    """Drop the stashes of clears that committed, put back the others'.

    The caller holds the flush lock, which a clear holds from before it stashes
    until its transaction ends: every stash seen here belongs to a clear that
    has ended, or whose process died with its connection. Settled before
    anything else under the lock, so a later clear never has an earlier one's
    stash put back over what it cleared.
    """
    from sqlalchemy import text as sa_text

    for raw_token, raw_xid in (await redis.hgetall(_CLEAR_STASH_INDEX)).items():
        token, xid = _as_text(raw_token), _as_text(raw_xid)
        try:
            status = None
            if xid.isdigit() and len(xid) <= 20:
                status = (
                    await db.execute(sa_text(_XACT_STATUS_SQL), {"xid": xid})
                ).scalar()
            if status == "in progress":
                continue
            if status == "committed":
                await _drop_clear_stash(redis, token)
                continue
            if restored := await _restore_clear_stash(redis, token):
                logger.warning(
                    "Put back %d quota counter(s) that a quota reset or a pool "
                    "recharge took out of Redis before its transaction %s",
                    restored,
                    "rolled back" if status == "aborted" else "could not be traced",
                )
        except Exception:
            logger.warning(
                "Could not settle the quota clear stash %s; the next flush "
                "tries again",
                token,
                exc_info=True,
            )


async def _read_usage_rows(redis: Redis, keys: list[str]) -> list[dict]:
    """Read *keys* in one pipeline and parse the usage counters that exist.

    A platform-wide counter (``_global``) is a row with ``user_id`` ``None``.
    """
    pipe = redis.pipeline()
    for key in keys:
        pipe.get(key)
    values = await pipe.execute()

    rows: list[dict] = []
    for key, val in zip(keys, values, strict=True):
        if val is None:
            continue
        parts = key.removeprefix(_USAGE_PREFIX).split(":", 4)
        if len(parts) not in (4, 5):
            continue
        uid_str, usage_type, model_or_total = parts[0], parts[1], parts[2]
        period_key = parts[3]
        agent_seg = parts[4] if len(parts) == 5 else "_all"
        uid: uuid.UUID | None
        if uid_str == _GLOBAL_USER:
            if len(parts) != 4 or model_or_total != "_total":
                continue
            uid = None
        else:
            try:
                uid = uuid.UUID(uid_str)
            except ValueError:
                continue
        agent_val: uuid.UUID | None
        if agent_seg == "_all":
            agent_val = None
        else:
            try:
                agent_val = uuid.UUID(agent_seg)
            except ValueError:
                continue
        model_val = None if model_or_total == "_total" else model_or_total
        rows.append(
            {
                "key": key,
                "user_id": uid,
                "usage_type": usage_type,
                "model": model_val,
                "period": period_key,
                "agent_id": agent_val,
                "count": int(val),
            }
        )
    return rows


async def _upsert_global_usage(db: AsyncSession, rows: list[dict], now: datetime) -> int:
    """Raise the ``quota_global_usage`` rows to the platform-wide *rows* read
    from Redis; how many counters that covers.

    One ``INSERT ... ON CONFLICT``, which only ever raises a count: a refund
    lowers the Redis counter below the row, and the row keeps the high-water
    mark the gate reads back. Collapsed per row first, since a scan can return
    a key twice and one statement may not update a row twice. A counter at 0
    (seeded from nothing saved) is not written: a missing row reads as 0.
    """
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    from app.models.quota import QuotaGlobalUsage

    counts: dict[tuple[str, str], int] = {}
    for row in rows:
        if row["count"] <= 0:
            continue
        slot = (row["usage_type"], row["period"])
        counts[slot] = max(counts.get(slot, 0), row["count"])
    if not counts:
        return 0
    stmt = pg_insert(QuotaGlobalUsage).values(
        [
            {"usage_type": usage_type, "period": period, "count": count, "updated_at": now}
            for (usage_type, period), count in counts.items()
        ]
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=[QuotaGlobalUsage.usage_type, QuotaGlobalUsage.period],
        set_={"count": stmt.excluded.count, "updated_at": stmt.excluded.updated_at},
        where=QuotaGlobalUsage.count < stmt.excluded.count,
    )
    await db.execute(stmt)
    return len(counts)


# Attempts at the platform-wide save within one flush cycle. Its statement
# takes no row lock, so only a statement timeout is worth a second try before
# the next cycle; any other failure waits for that cycle.
_GLOBAL_FLUSH_MAX_ATTEMPTS = 2
# Consecutive cycles whose platform-wide save failed in this process: only the
# first of a run of failures logs its traceback, so an outage does not print
# one every cycle.
_global_save_failures = 0


async def _save_global_usage(redis: Redis, keys: list[str]) -> int:
    """:func:`_flush_global_usage`, a statement timeout retried once; 0 when
    the save does not land, the counters staying in Redis for the next cycle."""
    global _global_save_failures
    for attempt in range(1, _GLOBAL_FLUSH_MAX_ATTEMPTS + 1):
        try:
            flushed = await _flush_global_usage(redis, keys)
        except Exception as exc:
            retryable = _is_retryable_flush_error(exc)
            if retryable and attempt < _GLOBAL_FLUSH_MAX_ATTEMPTS:
                await asyncio.sleep(_compute_flush_retry_delay(attempt))
                continue
            _global_save_failures += 1
            logger.warning(
                "flush_quota_usage: could not save the platform-wide counters "
                "(%d consecutive cycle(s)): %s; they stay in Redis for the next cycle",
                _global_save_failures,
                format_exception_message(exc),
                exc_info=not retryable and _global_save_failures == 1,
            )
            return 0
        _global_save_failures = 0
        return flushed
    return 0


async def _flush_global_usage(redis: Redis, keys: list[str]) -> int:
    """Save the platform-wide counters *keys* to ``quota_global_usage``, in a
    transaction of their own; how many counters that covers.

    Under ``_QUOTA_GLOBAL_FLUSH_LOCK_KEY``, held for this one statement: their
    read-back waits on it (:func:`_read_global_baseline`). The counters are
    re-read under the lock, so a counter a read-back seeded meanwhile is
    written at its seeded value, never at an older one above it. Skipped while
    another replica writes them, or while a read-back holds the lock shared:
    the counters stay in Redis for the next cycle.
    """
    from sqlalchemy import text as sa_text

    from app.core.database import async_session

    async with async_session() as db:
        got_lock = (
            await db.execute(
                sa_text("SELECT pg_try_advisory_xact_lock(CAST(:k AS bigint))"),
                {"k": _QUOTA_GLOBAL_FLUSH_LOCK_KEY},
            )
        ).scalar()
        if not got_lock:
            return 0
        rows = await _read_usage_rows(redis, keys)
        flushed = await _upsert_global_usage(db, rows, datetime.now(UTC))
        await db.commit()
    return flushed


async def flush_quota_usage() -> int:
    """Flush Redis quota counters to the ``quota_usage`` DB table, and the
    platform-wide ones to ``quota_global_usage``.

    The platform-wide counters go first, in a short transaction of their own
    (:func:`_flush_global_usage`), and never ride the per-user one below: their
    read-back waits for their write, and a per-user flush runs for as long as
    its rows take.

    Uses a NULL-safe upsert (manual SELECT FOR UPDATE NOWAIT + UPDATE/INSERT)
    because the ``model`` column is NULL for aggregate counters and PostgreSQL
    treats NULLs as distinct in unique constraints, which would create duplicate
    rows on every flush.

    Lock contention resilience
    --------------------------
    Every web replica runs the autonomous scheduler, so multiple replicas would
    otherwise flush the SAME ``quota_usage`` rows on the same cadence and the
    per-row ``SELECT ... FOR UPDATE NOWAIT`` upserts would collide, raising
    SQLSTATE ``55P03`` (lock_not_available) on the losers every cycle. To avoid
    that, the whole dedup+upsert runs under a transaction-scoped advisory lock
    (``pg_try_advisory_xact_lock``): only one replica flushes at a time and the
    rest skip the cycle. The Redis counters are durable, so the lock holder (or
    the next cycle) flushes them and nothing is lost. The advisory lock is held
    for the single dedup+upsert transaction and released automatically on
    COMMIT/ROLLBACK, so it can never leak back into the pooled connection.

    As a secondary safety net (e.g. a concurrent admin quota reset), the SELECT
    still uses ``FOR UPDATE NOWAIT`` and the whole block is retried up to
    ``_FLUSH_MAX_ATTEMPTS`` times with jittered exponential back-off on both
    ``55P03`` and ``57014``. The values written are read under the lock on
    every attempt, and the Redis keys are durable between retries, so no usage
    data is lost when a retry fires.

    A quota reset or a support-code recharge waiting for the lock is let
    through (``_FLUSH_YIELD_KEY``): the flush rolls back and leaves its cycle
    to the next one. Once it has the lock, it first settles the stashes of
    clears that ended without settling them (``_settle_clear_stashes``).

    Returns the number of counters flushed.
    """
    from sqlalchemy import func as sa_func
    from sqlalchemy import update as sa_update

    from app.core.database import async_session
    from app.models.quota import QuotaUsage

    redis = await redis_client()
    keys: list[str] = []
    async for key in redis.scan_iter(match=f"{_USAGE_PREFIX}*", count=200):
        keys.append(key if isinstance(key, str) else key.decode())
    unsettled = bool(await redis.exists(_CLEAR_STASH_INDEX))

    # A first read decides whether there is anything to flush at all, without
    # a database round trip. It is NOT what gets written: see the re-read
    # under the lock below.
    candidates = await _read_usage_rows(redis, keys) if keys else []
    if not candidates and not unsettled:
        return 0

    global_keys = [row["key"] for row in candidates if row["user_id"] is None]
    candidates = [row for row in candidates if row["user_id"] is not None]
    global_flushed = await _save_global_usage(redis, global_keys) if global_keys else 0
    if not candidates and not unsettled:
        return global_flushed

    # Bulk maintenance: dedup pass + per-row upsert across (potentially)
    # hundreds of users routinely runs longer than the default 30s
    # statement timeout configured globally in core/database.py — especially
    # when the cluster is under load.  Bump the per-statement timeout for
    # this session only via ``SET LOCAL`` (auto-reset at COMMIT/ROLLBACK,
    # so it never leaks to other sessions sharing this connection from
    # the pool).  The dedup + upsert now share a single transaction (the
    # advisory lock must span the whole flush), so a single ``SET LOCAL`` at
    # the top of that transaction covers both phases.
    from sqlalchemy import and_ as sa_and
    from sqlalchemy import delete as sa_delete
    from sqlalchemy import text as _sa_text

    _bump_timeout_sql = _sa_text("SET LOCAL statement_timeout = '120000'")

    last_exc: BaseException | None = None
    for attempt in range(1, _FLUSH_MAX_ATTEMPTS + 1):
        try:
            async with async_session() as db:
                # Serialize flushes cluster-wide. Every web replica runs the
                # autonomous scheduler and would otherwise flush the SAME
                # quota_usage rows concurrently, making the per-row
                # ``FOR UPDATE NOWAIT`` upserts collide with 55P03 every cycle.
                # The lock is transaction-scoped, so it is released automatically
                # on COMMIT/ROLLBACK (including this ``async with`` exit) and can
                # never leak back into the pooled connection. ``try`` variant:
                # if another replica is already flushing, skip this cycle — the
                # Redis counters are durable and get flushed by the holder.
                got_lock = (
                    await db.execute(
                        _sa_text(
                            "SELECT pg_try_advisory_xact_lock(CAST(:k AS bigint))"
                        ),
                        {"k": _QUOTA_FLUSH_LOCK_KEY},
                    )
                ).scalar()
                if not got_lock:
                    logger.debug(
                        "flush_quota_usage: another replica holds the flush "
                        "lock; skipping this cycle (Redis counters are durable)"
                    )
                    return global_flushed
                if await _clear_waiting(redis):
                    raise _FlushYieldError
                await _settle_clear_stashes(redis, db)

                # Re-read under the lock. A quota reset and a pool
                # recharge delete their counters under this same lock, so a
                # value read before it was taken may belong to a counter that
                # is gone, and upserting it would bring the deleted count back.
                # One pipelined read of the known keys - never the keyspace
                # scan - so the lock does not wait on Redis. An increment that
                # lands after it stays in Redis for the next cycle, as always.
                rows = (
                    await _read_usage_rows(redis, [row["key"] for row in candidates])
                    if candidates
                    else []
                )
                if not rows:
                    return global_flushed

                await db.execute(_bump_timeout_sql)

                # --- Deduplicate existing rows created by the old NULL-unsafe upsert ---
                # For each (user_id, usage_type, model IS NULL, period) group that has
                # more than one row, keep the one with the highest count and delete rest.
                dup_query = (
                    select(
                        QuotaUsage.user_id,
                        QuotaUsage.usage_type,
                        QuotaUsage.period,
                        QuotaUsage.agent_id,
                        sa_func.count().label("cnt"),
                    )
                    .where(QuotaUsage.model.is_(None), QuotaUsage.agent_id.is_(None))
                    .group_by(
                        QuotaUsage.user_id,
                        QuotaUsage.usage_type,
                        QuotaUsage.period,
                        QuotaUsage.agent_id,
                    )
                    .having(sa_func.count() > 1)
                )
                dup_groups = (await db.execute(dup_query)).all()
                for grp in dup_groups:
                    keep_query = (
                        select(QuotaUsage.id)
                        .where(
                            sa_and(
                                QuotaUsage.user_id == grp.user_id,
                                QuotaUsage.usage_type == grp.usage_type,
                                QuotaUsage.model.is_(None),
                                QuotaUsage.period == grp.period,
                                QuotaUsage.agent_id.is_(None),
                            )
                        )
                        .order_by(QuotaUsage.count.desc())
                        .limit(1)
                    )
                    keep_id = (await db.execute(keep_query)).scalar_one()
                    await db.execute(
                        sa_delete(QuotaUsage).where(
                            sa_and(
                                QuotaUsage.user_id == grp.user_id,
                                QuotaUsage.usage_type == grp.usage_type,
                                QuotaUsage.model.is_(None),
                                QuotaUsage.period == grp.period,
                                QuotaUsage.agent_id.is_(None),
                                QuotaUsage.id != keep_id,
                            )
                        )
                    )
                if dup_groups:
                    # NOTE: deliberately NO intermediate commit here. The dedup
                    # and the upsert below share ONE transaction so the
                    # transaction-scoped advisory lock acquired above is held for
                    # the whole flush and released only by the final commit. An
                    # intermediate commit would drop the lock mid-flush and
                    # re-open the cross-replica race. ``SET LOCAL`` applied once
                    # above therefore also covers the upsert loop.
                    logger.info("Deduplicated %d quota_usage groups", len(dup_groups))

                # --- NULL-safe upsert: SELECT FOR UPDATE NOWAIT then UPDATE or INSERT ---
                # FOR UPDATE NOWAIT acquires the row lock immediately at SELECT time so
                # the subsequent UPDATE never blocks.  If the row is already locked by
                # another session, NOWAIT raises 55P03 instantly — far preferable to
                # waiting up to 120 s before hitting the 57014 statement-timeout.
                now = datetime.now(UTC)

                # Pre-fetch existing user IDs to avoid FK violations from stale Redis keys
                all_uids = {row["user_id"] for row in rows}
                valid_result = await db.execute(
                    select(User.id).where(User.id.in_(list(all_uids)))
                )
                valid_uids = {row[0] for row in valid_result.all()}

                # Same guard for agent-attributed rows: a per-agent Redis key
                # can outlive its agent (35-day TTL vs. CASCADE row deletion),
                # and an INSERT with a stale agent_id would FK-violate and
                # abort the whole flush transaction on every cycle.
                from app.models.agent import Agent

                all_agent_ids = {
                    row["agent_id"] for row in rows if row["agent_id"] is not None
                }
                valid_agent_ids: set[uuid.UUID] = set()
                if all_agent_ids:
                    agent_result = await db.execute(
                        select(Agent.id).where(Agent.id.in_(list(all_agent_ids)))
                    )
                    valid_agent_ids = {row[0] for row in agent_result.all()}

                flushed = 0
                for index, row in enumerate(rows):
                    if (
                        index
                        and index % _FLUSH_YIELD_CHECK_ROWS == 0
                        and await _clear_waiting(redis)
                    ):
                        raise _FlushYieldError
                    if row["user_id"] not in valid_uids:
                        logger.debug(
                            "Skipping stale quota key for user %s (not in DB)",
                            row["user_id"],
                        )
                        continue
                    if (
                        row["agent_id"] is not None
                        and row["agent_id"] not in valid_agent_ids
                    ):
                        logger.debug(
                            "Skipping stale quota key for agent %s (not in DB)",
                            row["agent_id"],
                        )
                        continue

                    model_filter = (
                        QuotaUsage.model == row["model"]
                        if row["model"] is not None
                        else QuotaUsage.model.is_(None)
                    )
                    agent_filter = (
                        QuotaUsage.agent_id == row["agent_id"]
                        if row["agent_id"] is not None
                        else QuotaUsage.agent_id.is_(None)
                    )
                    existing = (
                        await db.execute(
                            select(QuotaUsage.id, QuotaUsage.count)
                            .where(
                                QuotaUsage.user_id == row["user_id"],
                                QuotaUsage.usage_type == row["usage_type"],
                                model_filter,
                                QuotaUsage.period == row["period"],
                                agent_filter,
                            )
                            .limit(1)
                            .with_for_update(nowait=True)
                        )
                    ).first()

                    if existing:
                        new_count = max(existing.count, row["count"])
                        await db.execute(
                            sa_update(QuotaUsage)
                            .where(QuotaUsage.id == existing.id)
                            .values(count=new_count, updated_at=now)
                        )
                    else:
                        db.add(
                            QuotaUsage(
                                user_id=row["user_id"],
                                usage_type=row["usage_type"],
                                model=row["model"],
                                period=row["period"],
                                agent_id=row["agent_id"],
                                count=row["count"],
                                updated_at=now,
                            )
                        )
                    flushed += 1
                await db.commit()

            flushed += global_flushed
            logger.debug("Flushed %d quota usage counters to DB", flushed)
            return flushed

        except _FlushYieldError:
            logger.info(
                "flush_quota_usage: a quota reset or a pool recharge is waiting "
                "for the flush lock; this cycle stands aside (Redis counters are "
                "durable)"
            )
            return global_flushed
        except Exception as exc:
            last_exc = exc
            if not _is_retryable_flush_error(exc):
                raise
            if attempt >= _FLUSH_MAX_ATTEMPTS:
                break
            delay = _compute_flush_retry_delay(attempt)
            logger.warning(
                "flush_quota_usage: transient lock/timeout contention "
                "(attempt %d/%d, SQLSTATE hint: %s) — retrying in %.1fs",
                attempt,
                _FLUSH_MAX_ATTEMPTS,
                format_exception_message(exc),
                delay,
            )
            await asyncio.sleep(delay)

    logger.error(
        "flush_quota_usage: still failing after %d attempts under sustained "
        "lock contention — quota usage may be stale until the next flush cycle: %s",
        _FLUSH_MAX_ATTEMPTS,
        last_exc,
        exc_info=last_exc,
    )
    if last_exc is not None:
        raise last_exc
    # Defensive fallback: the loop always runs at least once and only reaches
    # this point via ``break``, which fires only after ``last_exc`` has been
    # assigned, so in practice this is unreachable. It guarantees the function
    # can never ``raise None`` (a TypeError) if that invariant ever changes.
    raise RuntimeError(
        "flush_quota_usage exhausted all retry attempts without capturing an exception"
    )


# ---------------------------------------------------------------------------
# Admin actions
# ---------------------------------------------------------------------------


def _credit_back_window(key: str | bytes) -> tuple[str, str] | None:
    """``(usage_type, window)`` of a pool or free-credit counter key, else None."""
    name = key if isinstance(key, str) else key.decode()
    parts = name.removeprefix(_USAGE_PREFIX).split(":", 4)
    if len(parts) == 5 and parts[1] in (_POOL_USAGE_TYPE, _FREE_REFUND_USAGE_TYPE):
        return parts[1], parts[3]
    return None


# How long a quota reset or a support-code recharge waits for the flush lock.
# A blocking wait behind a long flush would be cancelled by the
# connection's statement timeout; a flush that sees the clear waiting stands
# aside within a few rows (``_FLUSH_YIELD_KEY``), so a lock still taken past
# this is refused with ``QuotaClearBusyError``, never cleared without.
_RESET_FLUSH_LOCK_WAIT_SECONDS = 10.0
_RESET_FLUSH_LOCK_POLL_SECONDS = 0.1


async def _wait_for_flush_lock(db: AsyncSession, redis: Redis) -> bool:
    """Take the flush's advisory lock on *db*; False if it stayed taken.

    Asks the flush holding it to stand aside while it waits. Whatever the
    clock says, the lock is tried again after the flush was asked to yield: a
    slow first round trip must not refuse the reset before the flush had the
    chance to stand aside.
    """
    from sqlalchemy import text as sa_text

    loop = asyncio.get_running_loop()
    deadline = loop.time() + _RESET_FLUSH_LOCK_WAIT_SECONDS
    tries = 0
    while True:
        taken = (
            await db.execute(
                sa_text("SELECT pg_try_advisory_xact_lock(CAST(:k AS bigint))"),
                {"k": _QUOTA_FLUSH_LOCK_KEY},
            )
        ).scalar()
        tries += 1
        if taken or (tries > 1 and loop.time() >= deadline):
            return bool(taken)
        try:
            await redis.set(_FLUSH_YIELD_KEY, "1", px=_FLUSH_YIELD_TTL_MS)
        except Exception:
            logger.debug("Could not ask the quota flush to stand aside", exc_info=True)
        await asyncio.sleep(_RESET_FLUSH_LOCK_POLL_SECONDS)


async def _abandon_clear(db: AsyncSession, redis: Redis) -> None:
    """Roll back a quota reset or a pool recharge that failed, and put back
    the counters it stashed.

    Settled under the flush lock like any stash: a clear that took the lock in
    the meantime has already put them back, and cleared them again. When the
    lock cannot be had, the next flush settles it.
    """
    try:
        await db.rollback()
        if await _wait_for_flush_lock(db, redis):
            await _settle_clear_stashes(redis, db)
        await db.rollback()
    except Exception:
        logger.warning(
            "A quota reset or pool recharge failed and its counters are not put "
            "back yet; the next flush puts them back",
            exc_info=True,
        )


async def _drop_committed_stash(redis: Redis, token: str) -> None:
    try:
        await _drop_clear_stash(redis, token)
    except Exception:
        logger.debug(
            "Could not drop the committed quota clear stash %s; the next flush "
            "drops it",
            token,
            exc_info=True,
        )


async def reset_user_quota_usage(
    db: AsyncSession,
    user_id: uuid.UUID,
    quota_type: str | None = None,
) -> None:
    """Reset quota usage counters for a user (Redis + DB), and commit *db*.

    A reset that clears the user's failure-pool and free-credit counters also
    drops the user's share of their cause breakdown, which the admin card reads
    against the totals those counters make.

    Runs under the flush's advisory lock, like the pool recharge: the
    flush upserts what it reads under that lock, so it either finishes before
    this starts (its rows are deleted here) or reads after it commits and finds
    the counters gone - it can no longer write back the counters this reset
    deletes. A flush holding the lock is asked to stand aside; one that keeps
    it past ``_RESET_FLUSH_LOCK_WAIT_SECONDS`` fails the reset with
    ``QuotaClearBusyError`` before anything is cleared.

    It also holds the user's pool-clear lock while it clears their pools, so a
    cold claim of one of them waits for this transaction to end and reads the
    pool it leaves, rather than seeding back a row this deletes or counting
    from zero before a reset that may still roll back (``_load_pool_baseline``).

    The counters leave Redis through a stash (``_CLEAR_STASH_PREFIX``), dropped
    once the reset has committed and put back if it does not, so a reset that
    fails loses no increment only Redis held.
    """
    from sqlalchemy import delete

    from app.models.quota import QuotaUsage

    redis = await redis_client()
    if not await _wait_for_flush_lock(db, redis):
        raise QuotaClearBusyError(QUOTA_CLEAR_BUSY_MESSAGE)
    clears_pools = quota_type in (None, "agentic")
    if clears_pools:
        await _lock_pool_clear(db, user_id)
    await _settle_clear_stashes(redis, db)
    stash = await _open_clear_stash(redis, db)
    try:
        # The windows this reset removes pool or free credits from, Redis and
        # DB alike: ``forget_user_refund_causes`` needs them.
        credited_windows: set[tuple[str, str]] = set()
        # The durable rows a cold claim seeds these counters from once this
        # transaction is over; a rolled-back reset puts them back.
        seeds: dict[str, int] = {}
        if clears_pools:
            removed = await db.execute(
                delete(QuotaUsage)
                .where(
                    QuotaUsage.user_id == user_id,
                    QuotaUsage.usage_type.in_(
                        [_POOL_USAGE_TYPE, _FREE_REFUND_USAGE_TYPE]
                    ),
                )
                .returning(
                    QuotaUsage.usage_type,
                    QuotaUsage.period,
                    QuotaUsage.agent_id,
                    QuotaUsage.count,
                )
            )
            for row in removed.all():
                credited_windows.add((row.usage_type, row.period))
                seeds[
                    _usage_key(user_id, row.usage_type, None, row.period, row.agent_id)
                ] = row.count

        patterns = [
            f"{_USAGE_PREFIX}{user_id}:{quota_type}:*"
            if quota_type
            else f"{_USAGE_PREFIX}{user_id}:*"
        ]
        # Clear the agent failure pools too. A full reset (quota_type None)
        # sweeps them with everything else via the pattern above; an "agentic"
        # reset does not (``agentic:`` never matches ``agentic_failure_pool:``)
        # and needs this explicit pass — a reset that left the pools behind
        # would hand the user a clean counter but an already-spent pool, so
        # their next failure would be charged. The durable rows go with the
        # Redis keys: the DB is where a year-long pool actually lives, and the
        # ``max()`` flush would otherwise push the old count straight back into
        # Redis on the next failure.
        if quota_type == "agentic":
            patterns += [
                f"{_USAGE_PREFIX}{user_id}:{extra_type}:*"
                for extra_type in (
                    _POOL_USAGE_TYPE,
                    _FREE_REFUND_USAGE_TYPE,
                    # The panel counters show what this reset clears.
                    _VIEW_USAGE_TYPE,
                )
            ]
        for pattern in patterns:
            batch: list[str] = []
            async for raw in redis.scan_iter(match=pattern, count=200):
                key = _as_text(raw)
                batch.append(key)
                if credited := _credit_back_window(key):
                    credited_windows.add(credited)
                if len(batch) >= _CLEAR_STASH_BATCH:
                    await _stash_counters(redis, stash, batch, seeds)
                    batch.clear()
            await _stash_counters(redis, stash, batch, seeds)

        if clears_pools:
            # The card's totals no longer count this user's credits, so its
            # cause breakdown must not count their causes either: left behind,
            # they overshoot the total for the rest of the window. The
            # epoch moves only now that the counters are gone, so a credit
            # still in flight tallies under the old one, where nothing reads.
            await forget_user_refund_causes(
                redis,
                user_id,
                credited_windows,
                below_user_epoch=await _advance_refund_epoch(redis, user_id),
            )

        # Clear DB rows
        conditions = [QuotaUsage.user_id == user_id]
        if quota_type == "agentic":
            conditions.append(QuotaUsage.usage_type.in_([quota_type, _VIEW_USAGE_TYPE]))
        elif quota_type:
            conditions.append(QuotaUsage.usage_type == quota_type)
        await db.execute(delete(QuotaUsage).where(*conditions))
        await db.commit()
    except Exception:
        await _abandon_clear(db, redis)
        raise
    await _drop_committed_stash(redis, stash)


#: The quota types and periods a platform-wide reset may name: every
#: type ``_count_global`` counts, every period a ceiling is set in.
PLATFORM_RESETTABLE_QUOTA_TYPES = ("agentic", "sandbox", "llm_proxy")
PLATFORM_RESETTABLE_PERIODS = ("daily", "monthly", "yearly")

# How long a platform-wide reset waits for the write of the platform-wide
# counters (``_QUOTA_GLOBAL_FLUSH_LOCK_KEY``). That write holds the lock for one
# statement, so a lock still taken past this is a stuck write, refused like a
# stuck flush.
_PLATFORM_RESET_GLOBAL_LOCK_WAIT_SECONDS = 5.0


async def _wait_for_global_flush_lock(db: AsyncSession) -> bool:
    """Take the platform-wide counters' write lock on *db*, exclusively; False
    if it stayed taken."""
    from sqlalchemy import text as sa_text

    loop = asyncio.get_running_loop()
    deadline = loop.time() + _PLATFORM_RESET_GLOBAL_LOCK_WAIT_SECONDS
    while True:
        taken = (
            await db.execute(
                sa_text("SELECT pg_try_advisory_xact_lock(CAST(:k AS bigint))"),
                {"k": _QUOTA_GLOBAL_FLUSH_LOCK_KEY},
            )
        ).scalar()
        if taken or loop.time() >= deadline:
            return bool(taken)
        await asyncio.sleep(_RESET_FLUSH_LOCK_POLL_SECONDS)


async def reset_platform_quota_usage(
    db: AsyncSession,
    quota_types: Iterable[str],
    periods: Iterable[str],
) -> list[dict]:
    """Zero the platform-wide counters of *quota_types* in the CURRENT window
    of each of *periods*, in Redis and in ``quota_global_usage``, and commit
    *db*.

    These are the counters the license ceilings (XTM One) and ``platform_limit``
    (Copilot) are enforced against, which no admin action resets: in XTM One
    mode the quota is what the license sells. It is reached through a
    Filigran-signed support code (``support_codes.redeem_support_code``), which
    the caller burns in the same transaction. The ceilings themselves are left
    as they are, and so are past windows and every per-user counter.

    Both stores go: the gate reads ``quota_global_usage`` back whenever Redis
    has lost a counter (``_get_global_count``), so a counter cleared in Redis
    alone would come back at its saved mark.

    Locks, in the order every other holder takes them, so none can deadlock:

    * the flush lock (``_wait_for_flush_lock``), like every clear: it is what
      settles the stashes, and a flush asked to stand aside does;
    * then the write lock of the platform-wide counters, exclusively. Their
      write (``_flush_global_usage``) re-reads Redis under it, so it either
      finished before this (its rows are deleted here) or runs after the
      commit and finds the counters gone. A read-back of a counter this
      removes (``_read_global_baseline``) waits on it too, and once this
      commits reads no row, so the counter starts again from 0; if this rolls
      back it reads the restored row, and the stash adds back what Redis held
      over it.

    Either lock still taken past its bounded wait raises
    ``QuotaClearBusyError`` before anything is cleared. The counters leave
    Redis through a stash, dropped once committed and put back otherwise.

    A refund of a run charged before the reset lowers the fresh counter by one
    (never below zero, ``_decrement``): at most one action per run in flight.

    Returns one entry per counter, with the count it held before the reset -
    the larger of the live counter and its saved mark, which is what the gate
    was comparing with the ceiling.
    """
    from sqlalchemy import delete

    from app.models.quota import QuotaGlobalUsage

    slots = sorted(
        {
            (quota_type, period, _current_period_key(period))
            for quota_type in quota_types
            for period in periods
        }
    )
    redis = await redis_client()
    if not await _wait_for_flush_lock(db, redis):
        raise QuotaClearBusyError(QUOTA_CLEAR_BUSY_MESSAGE)
    if not await _wait_for_global_flush_lock(db):
        await db.rollback()
        raise QuotaClearBusyError(QUOTA_CLEAR_BUSY_MESSAGE)
    await _settle_clear_stashes(redis, db)
    stash = await _open_clear_stash(redis, db)
    try:
        saved: dict[tuple[str, str], int] = {}
        for quota_type, _period, period_key in slots:
            row = (
                await db.execute(
                    delete(QuotaGlobalUsage)
                    .where(
                        QuotaGlobalUsage.usage_type == quota_type,
                        QuotaGlobalUsage.period == period_key,
                    )
                    .returning(QuotaGlobalUsage.count)
                )
            ).scalar_one_or_none()
            if row is not None:
                saved[(quota_type, period_key)] = int(row)

        keys = [_global_usage_key(quota_type, key) for quota_type, _p, key in slots]
        live = {row["key"]: row["count"] for row in await _read_usage_rows(redis, keys)}
        seeds = {
            _global_usage_key(quota_type, key): saved.get((quota_type, key), 0)
            for quota_type, _p, key in slots
        }
        keys_cleared = await _stash_counters(redis, stash, keys, seeds)
        await db.commit()
    except Exception:
        await _abandon_clear(db, redis)
        raise
    await _drop_committed_stash(redis, stash)

    counters: list[dict] = []
    for quota_type, period, period_key in slots:
        counters.append(
            {
                "quota_type": quota_type,
                "period": period,
                "period_key": period_key,
                "previous_count": max(
                    live.get(_global_usage_key(quota_type, period_key), 0),
                    saved.get((quota_type, period_key), 0),
                ),
            }
        )
    logger.info(
        "Platform-wide quota counters reset: %d live counter(s) and %d saved "
        "row(s) cleared (%s)",
        keys_cleared,
        len(saved),
        ", ".join(f"{c['quota_type']}/{c['period_key']}" for c in counters),
    )
    return counters


async def invalidate_quota_cache(user_id: uuid.UUID | None = None) -> None:
    """Clear cached quota config. If *user_id* is None, clears all caches.

    Callers run it once the change it follows has COMMITTED (dropped earlier, a
    quota check landing before the commit re-caches the old config), so it never
    raises: a failure leaves the previous resolution cached until its TTL and
    must not answer a change that already landed with an error. The generation
    is replaced first, so a resolution already reading the old config is not
    written after the delete.
    """
    try:
        redis = await redis_client()
        await _replace_config_generation(redis)
        if user_id:
            await redis.delete(_config_cache_key(user_id))
        else:
            async for key in redis.scan_iter(match=f"{_CONFIG_CACHE_PREFIX}*", count=200):
                await redis.delete(key)
    except Exception as exc:
        _log_quota_cache_not_invalidated(exc)


#: Keys per ``DEL`` when a membership change drops many resolutions at once.
_CONFIG_CACHE_DELETE_BATCH = 500


async def invalidate_membership_quota_cache(
    user_ids: Iterable[uuid.UUID | str],
) -> None:
    """Drop the cached quota resolution of users whose group memberships changed.

    A user's resolution includes the quotas of their groups, so every path that
    adds or removes a membership calls this once that change has COMMITTED: the
    group membership routes, a group deletion, SAML group sync on sign-in (and
    its merge of a UUID-named group), an account deletion. The group's quota as
    the request read it is no reason to skip a user - a concurrent quota change
    can have made that read stale - and the drop is one ``DEL`` per batch, after
    the generation is replaced. Never raises, like
    ``invalidate_quota_cache``.
    """
    keys: set[str] = set()
    for user_id in user_ids:
        try:
            keys.add(_config_cache_key(uuid.UUID(str(user_id))))
        except (AttributeError, TypeError, ValueError):
            continue
    if not keys:
        return
    ordered = sorted(keys)
    try:
        redis = await redis_client()
        await _replace_config_generation(redis)
        for start in range(0, len(ordered), _CONFIG_CACHE_DELETE_BATCH):
            await redis.delete(*ordered[start : start + _CONFIG_CACHE_DELETE_BATCH])
    except Exception as exc:
        _log_quota_cache_not_invalidated(exc)


def _log_quota_cache_not_invalidated(exc: Exception) -> None:
    logger.warning(
        "Quota config cache not invalidated (%s): the previous resolution "
        "stays cached for up to %ds",
        format_exception_message(exc),
        _CONFIG_CACHE_TTL,
    )


async def _resolve_per_user_limits(
    db: AsyncSession,
    qt: str,
    user_ids: Iterable[str],
    platform_default: int | None,
) -> dict[str, int | None]:
    """Resolve effective per-user limit for *qt* using the full chain:
    user override → group (highest) → platform default.

    Returns ``{uid: resolved_limit}`` where ``None`` means unlimited.
    """
    from app.core.utils import safe_uuid

    uid_list = list(user_ids)
    valid_uuids = {uid: safe_uuid(uid) for uid in uid_list}
    valid_uuids = {k: v for k, v in valid_uuids.items() if v is not None}

    if not valid_uuids:
        return {uid: platform_default for uid in uid_list}

    user_result = await db.execute(
        select(User.id, User.quota_config).where(
            User.id.in_(list(valid_uuids.values()))
        )
    )
    user_cfgs: dict[str, dict | None] = {
        str(row.id): read_override_config(row.quota_config)
        for row in user_result.all()
    }

    group_result = await db.execute(
        select(
            GroupMember.user_id,
            Group.quota_config,
        )
        .join(Group, Group.id == GroupMember.group_id)
        .where(
            GroupMember.user_id.in_(list(valid_uuids.values())),
            Group.quota_config.isnot(None),
        )
    )
    user_group_cfgs: dict[str, list[dict]] = {}
    for row in group_result.all():
        uid = str(row.user_id)
        cfg = read_override_config(row.quota_config)
        if cfg and cfg.get(qt):
            user_group_cfgs.setdefault(uid, []).append(cfg)

    resolved: dict[str, int | None] = {}
    for uid in uid_list:
        # Mirrors ``resolve_quota_config`` step for step, so the
        # dashboard never disagrees with the gate: the user's own config wins
        # whatever it holds (``None`` = unlimited, ``0`` = hard stop), then the
        # most permissive group (Unlimited the most permissive of all), then
        # the platform default.
        ucfg = user_cfgs.get(uid) or {}
        if ucfg.get(qt):
            resolved[uid] = ucfg[qt].get("default")
            continue
        candidates = [g[qt] for g in user_group_cfgs.get(uid, []) if g.get(qt)]
        if candidates:
            resolved[uid] = _most_permissive(candidates).get("default")
            continue
        resolved[uid] = platform_default

    return resolved


# ---------------------------------------------------------------------------
# Platform-wide quota monitoring (admin dashboard)
# ---------------------------------------------------------------------------


async def get_platform_quota_monitoring(
    db: AsyncSession,
) -> dict:
    """Return platform-wide quota monitoring data for the admin dashboard.

    Returns current-period summary, historical usage, and top consumers.
    """
    from sqlalchemy import func as sa_func

    from app.models.quota import QuotaUsage

    redis = await redis_client()

    platform_cfg = await _load_global_quota_config(db) or {}

    user_count_res = await db.execute(
        select(sa_func.count()).select_from(User).where(User.is_active.is_(True))
    )
    total_users = user_count_res.scalar() or 0

    xtm_agentic_cfg = None
    xtm_info = None
    if settings.is_xtm_one:
        xtm_agentic_cfg = await _resolve_xtm_agentic_config(db)
        from app.api.v1.platform_settings_helpers import (
            get_platform_settings as _load_ps,
        )
        from app.enterprise.licensing import get_xtm_license_info
        from app.enterprise.xtm_ca import XTM_CA_PEM

        ps = await _load_ps(db)
        xtm_info = get_xtm_license_info(
            pem_from_env=settings.enterprise_license,
            pem_from_db=ps.enterprise_license,
            ca_pem=XTM_CA_PEM,
            platform_created_at=ps.created_at,
        )

    quotas_summary: list[dict] = []

    async def _scan_period(
        qt: str,
        period: str,
        limit_val: int | None,
        *,
        is_global: bool = False,
    ) -> dict:
        period_key = _current_period_key(period)
        users_near = 0
        users_over = 0

        # Use DB as source of truth — matches history and top-consumers queries.
        # For coarser periods (monthly/yearly) we aggregate finer-grained rows
        # using a prefix match (e.g. "2026-02%" catches both "2026-02" and
        # "2026-02-25" rows).
        db_query = (
            select(
                QuotaUsage.user_id,
                sa_func.sum(QuotaUsage.count).label("total"),
            )
            .where(
                QuotaUsage.usage_type == qt,
                QuotaUsage.model.is_(None),
                QuotaUsage.agent_id.is_(None),
                QuotaUsage.period.like(period_key + "%"),
            )
            .group_by(QuotaUsage.user_id)
        )
        db_result = await db.execute(db_query)
        user_totals: dict[str, int] = {
            str(row.user_id): row.total or 0 for row in db_result.all()
        }

        # Supplement with Redis for any not-yet-flushed increments.
        # Per user: take MAX(db, redis) since the flush replaces DB with Redis.
        redis_user_totals: dict[str, int] = {}
        # Scan both the 5-segment ":_all" aggregate keys and the legacy
        # 4-segment keys (pre-agent-attribution) — counters written before the
        # deploy live under the legacy shape until the first post-deploy
        # increment folds them in, and the two shapes never overlap, so the
        # per-user sum across patterns cannot double-count.
        scan_patterns: list[str] = [
            f"{_USAGE_PREFIX}*:{qt}:_total:{period_key}:_all",
            f"{_USAGE_PREFIX}*:{qt}:_total:{period_key}",
        ]
        if period == "monthly":
            scan_patterns.append(f"{_USAGE_PREFIX}*:{qt}:_total:{period_key}-??:_all")
            scan_patterns.append(f"{_USAGE_PREFIX}*:{qt}:_total:{period_key}-??")
        elif period == "yearly":
            scan_patterns.append(f"{_USAGE_PREFIX}*:{qt}:_total:{period_key}-??-??:_all")
            scan_patterns.append(f"{_USAGE_PREFIX}*:{qt}:_total:{period_key}-??:_all")
            scan_patterns.append(f"{_USAGE_PREFIX}*:{qt}:_total:{period_key}-??-??")
            scan_patterns.append(f"{_USAGE_PREFIX}*:{qt}:_total:{period_key}-??")

        for pattern in scan_patterns:
            async for key in redis.scan_iter(match=pattern, count=500):
                key_str = key if isinstance(key, str) else key.decode()
                parts = key_str.removeprefix(_USAGE_PREFIX).split(":")
                uid = parts[0] if parts else ""
                if uid == _GLOBAL_USER:
                    continue
                val = await redis.get(key_str)
                count = int(val) if val else 0
                if count > 0:
                    redis_user_totals[uid] = redis_user_totals.get(uid, 0) + count

        # Merge: per user take MAX(db, redis)
        all_uids = set(user_totals.keys()) | set(redis_user_totals.keys())
        merged: dict[str, int] = {}
        for uid in all_uids:
            merged[uid] = max(
                user_totals.get(uid, 0),
                redis_user_totals.get(uid, 0),
            )

        current_total = sum(merged.values())
        if is_global and limit_val is not None:
            # A platform-wide ceiling is enforced against the global counter,
            # which every charged run reaches - a run with no account behind
            # it included, which has no user row above, read the way
            # the gate reads it, from its saved mark when Redis lost it.
            # The per-user total stays the floor.
            current_total = max(
                current_total,
                await _get_global_count(redis, qt, period_key),
            )

        over_quota_uids: dict[str, tuple[int, int]] = {}
        near_quota_uids: dict[str, tuple[int, int]] = {}

        if not is_global and merged:
            resolved_limits = await _resolve_per_user_limits(
                db,
                qt,
                merged.keys(),
                limit_val,
            )
            for uid, count in merged.items():
                user_limit = resolved_limits.get(uid)
                if user_limit is None:
                    continue
                if user_limit <= 0:
                    # A hard stop: the gate refuses every call, so the user is
                    # at their limit whatever they spent before it was set.
                    users_over += 1
                    over_quota_uids[uid] = (count, user_limit)
                    continue
                ratio = count / user_limit
                if ratio >= 1.0:
                    users_over += 1
                    over_quota_uids[uid] = (count, user_limit)
                elif ratio >= 0.8:
                    users_near += 1
                    near_quota_uids[uid] = (count, user_limit)

        return {
            "type": qt,
            "limit": limit_val,
            "period": period,
            "current_usage": current_total,
            "active_users": len(merged),
            "total_users": total_users,
            "users_near_limit": users_near,
            "users_over_limit": users_over,
            "is_global": is_global,
            "_over_quota_uids": over_quota_uids,
            "_near_quota_uids": near_quota_uids,
        }

    if settings.is_xtm_one and xtm_info and xtm_info.license_validated:
        for period, quota_val in (
            ("daily", xtm_info.daily_quota),
            ("monthly", xtm_info.monthly_quota),
            ("yearly", xtm_info.yearly_quota),
        ):
            limit_val = quota_val if quota_val > 0 else None
            quotas_summary.append(
                await _scan_period("agentic", period, limit_val, is_global=True)
            )
    elif settings.is_xtm_one and xtm_agentic_cfg:
        from app.enterprise.licensing import (
            DEFAULT_AGENTIC_DAILY_QUOTA as _EE_DAILY,
        )
        from app.enterprise.licensing import (
            DEFAULT_AGENTIC_MONTHLY_QUOTA as _EE_MONTHLY,
        )
        from app.enterprise.licensing import (
            DEFAULT_AGENTIC_YEARLY_QUOTA as _EE_YEARLY,
        )

        for period, quota_val in (
            ("daily", _EE_DAILY),
            ("monthly", _EE_MONTHLY),
            ("yearly", _EE_YEARLY),
        ):
            quotas_summary.append(
                await _scan_period("agentic", period, quota_val, is_global=True)
            )
    else:
        for qt in ("agentic", "llm_proxy"):
            cfg = platform_cfg.get(qt) or {}
            if qt == "agentic" and xtm_agentic_cfg:
                cfg = xtm_agentic_cfg

            # Platform-wide (global) summary entry
            platform_limit = cfg.get("platform_limit")
            if platform_limit and platform_limit > 0:
                period = cfg.get("period", "monthly")
                quotas_summary.append(
                    await _scan_period(
                        qt,
                        period,
                        platform_limit,
                        is_global=True,
                    )
                )

            # Per-user summary entry
            limit_val = cfg.get("default")
            period = cfg.get("period", "monthly")
            is_global = bool(cfg.get("global"))
            quotas_summary.append(
                await _scan_period(qt, period, limit_val, is_global=is_global)
            )

    # Sandbox usage has no enforceable limit or configurable period of its
    # own (spec 002-sandbox-quota-usage-tracking FR-011/FR-014, revised —
    # it always shares the agentic entry's limit and resolved period for
    # per-user enforcement/display), so it doesn't fit the agentic/llm_proxy
    # branches above, which all assume a limit-bearing, independently
    # periodable type. This platform-wide summary row is a separate,
    # global-only, limit-less observability series with no single "the"
    # agentic period to mirror here (XTM One mode scans agentic under up to
    # three periods — daily/monthly/yearly — simultaneously above), so it
    # keeps its own fixed monthly bucket rather than trying to track one.
    quotas_summary.append(
        await _scan_period("sandbox", "monthly", None, is_global=True)
    )

    period_lengths: set[int] = set()
    for qs in quotas_summary:
        p = qs["period"]
        if p == "daily":
            period_lengths.add(10)
        elif p == "yearly":
            period_lengths.add(4)
        else:
            period_lengths.add(7)

    history_query = (
        select(
            QuotaUsage.period,
            QuotaUsage.usage_type,
            sa_func.sum(QuotaUsage.count).label("total"),
        )
        .where(QuotaUsage.model.is_(None))
        .where(QuotaUsage.agent_id.is_(None))
        # The panel counters repeat agentic + sandbox in every period.
        .where(QuotaUsage.usage_type != _VIEW_USAGE_TYPE)
        .group_by(QuotaUsage.period, QuotaUsage.usage_type)
        .order_by(QuotaUsage.period.asc())
    )
    if period_lengths:
        length_filters = [
            sa_func.length(QuotaUsage.period) == pl for pl in period_lengths
        ]
        from sqlalchemy import or_ as sa_or

        history_query = history_query.where(sa_or(*length_filters))

    history_result = await db.execute(history_query)
    history_raw = history_result.all()

    history_map: dict[str, dict[str, int]] = {}
    for row in history_raw:
        period_val, usage_type, total = row
        if period_val not in history_map:
            history_map[period_val] = {"period": period_val}
        history_map[period_val][usage_type] = total or 0
    history = sorted(history_map.values(), key=lambda h: h.get("period", ""))

    top_consumers: dict[str, list[dict]] = {}
    for qt in ("agentic", "llm_proxy"):
        cfg = platform_cfg.get(qt) or {}
        if qt == "agentic" and xtm_agentic_cfg:
            cfg = xtm_agentic_cfg
        period = cfg.get("period", "monthly")
        period_key = _current_period_key(period)

        tc_query = (
            select(
                User.full_name,
                User.email,
                sa_func.sum(QuotaUsage.count).label("total"),
            )
            .join(User, User.id == QuotaUsage.user_id)
            .where(
                QuotaUsage.usage_type == qt,
                QuotaUsage.model.is_(None),
                QuotaUsage.agent_id.is_(None),
            )
        )
        if period == "daily":
            tc_query = tc_query.where(
                sa_func.length(QuotaUsage.period) == 10,
                QuotaUsage.period >= period_key[:7],
            )
        else:
            tc_query = tc_query.where(QuotaUsage.period == period_key)

        tc_result = await db.execute(
            tc_query.group_by(User.id, User.full_name, User.email)
            .order_by(sa_func.sum(QuotaUsage.count).desc())
            .limit(10)
        )
        top_consumers[qt] = [
            {"name": r.full_name, "email": r.email, "count": r.total or 0}
            for r in tc_result.all()
        ]

    all_flagged_uids: set[str] = set()
    for qs in quotas_summary:
        all_flagged_uids.update(qs.get("_over_quota_uids", {}).keys())
        all_flagged_uids.update(qs.get("_near_quota_uids", {}).keys())

    uid_details: dict[str, dict[str, str]] = {}
    if all_flagged_uids:
        from app.core.utils import safe_uuid

        valid_uuids = [safe_uuid(u) for u in all_flagged_uids if safe_uuid(u)]
        if valid_uuids:
            detail_result = await db.execute(
                select(User.id, User.full_name, User.email).where(
                    User.id.in_(valid_uuids)
                )
            )
            for row in detail_result.all():
                uid_details[str(row.id)] = {
                    "name": row.full_name,
                    "email": row.email,
                }

    over_quota_users: dict[str, list[dict]] = {}
    for qs in quotas_summary:
        over_uids = qs.pop("_over_quota_uids", {})
        near_uids = qs.pop("_near_quota_uids", {})
        if not over_uids and not near_uids:
            continue
        qt = qs["type"]
        entries: list[dict] = []
        for uid, (count, user_limit) in sorted(
            over_uids.items(), key=lambda x: -x[1][0]
        ):
            info = uid_details.get(uid, {})
            entries.append(
                {
                    "name": info.get("name", "Unknown"),
                    "email": info.get("email", ""),
                    "usage": count,
                    "limit": user_limit,
                    "status": "over",
                }
            )
        for uid, (count, user_limit) in sorted(
            near_uids.items(), key=lambda x: -x[1][0]
        ):
            info = uid_details.get(uid, {})
            entries.append(
                {
                    "name": info.get("name", "Unknown"),
                    "email": info.get("email", ""),
                    "usage": count,
                    "limit": user_limit,
                    "status": "near",
                }
            )
        if qt not in over_quota_users:
            over_quota_users[qt] = []
        over_quota_users[qt].extend(entries)

    return {
        "quotas": quotas_summary,
        "history": history,
        "top_consumers": top_consumers,
        "over_quota_users": over_quota_users,
    }
