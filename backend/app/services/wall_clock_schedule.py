# SPDX-License-Identifier: Apache-2.0

"""Wall-clock schedule of the scheduler tick's periodic jobs, shared by every process.

The tick runs every 60 s in each web process. A job that must run "once an
hour" cannot count ticks in memory: the count restarts at 0 on every boot and
each process counts only the ticks it wins, so a platform that redeploys more
often than the interval never reaches it and the job never runs (#768).

A :class:`Job` is due when its last COMPLETED pass, stored in Redis, is older
than its interval (never completed: due at once). :func:`claim` takes a
cluster-wide lock (``SET NX`` with a TTL) so exactly one process runs a due
pass; :func:`complete` records the end and releases the lock. A pass killed
before :func:`complete` (a deploy, an OOM) records nothing: its lock expires
after ``lock_ttl`` and the next process that ticks runs it again.

:func:`status` is what Settings and Platform Health read: the last completed
pass, the next one, and ``overdue`` when no pass completed in two intervals
since the job was first scheduled on this platform (``since``).

A backlog flag (:func:`set_backlog`, :func:`backlog_pending`) lets a bounded
pass that stopped at its cap say "more to do", so a drain step runs again on
the next tick instead of the next interval.

Redis unreachable: each process falls back to its own memory (a job runs at
most once per interval per process), never to a run on every tick.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

logger = logging.getLogger(__name__)

_PREFIX = "schedule:"
#: How long a backlog flag lives without a pass confirming it.
BACKLOG_TTL = timedelta(days=1)
#: Release the lock only when it is still ours (a slow pass whose lock expired
#: must not drop the lock of the process that took it over).
_RELEASE_LUA = "if redis.call('GET', KEYS[1]) == ARGV[1] then\n    return redis.call('DEL', KEYS[1])\nend\nreturn 0"


@dataclass(frozen=True)
class Job:
    """A periodic job of the scheduler tick: its name, interval and lock lifetime."""

    name: str
    interval: timedelta
    #: How long a claim holds the lock: a pass killed mid-way is retried after it.
    lock_ttl: timedelta


@dataclass(frozen=True)
class Claim:
    """The right to run one pass of a job, held until :func:`complete`."""

    job: Job
    token: str
    started_at: datetime


@dataclass(frozen=True)
class JobStatus:
    """What a job's schedule says now (read by Settings and Platform Health)."""

    last_completed_at: datetime | None
    last_started_at: datetime | None
    #: When the job was first scheduled on this platform.
    since: datetime | None
    #: ``None``: due now (never completed, or the interval has passed).
    next_due_at: datetime | None
    #: No pass completed in two intervals since the job was scheduled.
    overdue: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "last_completed_at": _iso(self.last_completed_at),
            "last_started_at": _iso(self.last_started_at),
            "since": _iso(self.since),
            "next_due_at": _iso(self.next_due_at),
            "overdue": self.overdue,
        }


#: The hourly maintenance block of the tick (memory, shared state, work items,
#: worksets, flow events, Video Studio media, check files, file retention...).
HOURLY_MAINTENANCE = Job("hourly_maintenance", timedelta(hours=1), timedelta(minutes=15))
#: A retention pass that stopped at its cap: one bounded pass per tick until empty.
RETENTION_DRAIN = Job("retention_drain", timedelta(0), timedelta(minutes=5))
#: The check for stale URL-based skills.
SKILL_REFRESH = Job("skill_refresh", timedelta(hours=1), timedelta(minutes=15))
#: The AI adoption metrics sync.
AI_METRICS_SYNC = Job("ai_metrics_sync", timedelta(hours=1), timedelta(minutes=15))


def model_prices_git_sync(minutes: int) -> Job:
    """The model prices git sync, at the cadence its setting gives."""
    return Job("model_prices_git_sync", timedelta(minutes=max(1, int(minutes))), timedelta(minutes=10))


#: Per-process memory used when Redis cannot be read.
_LOCAL_STARTED: dict[str, datetime] = {}
_LOCAL_COMPLETED: dict[str, datetime] = {}
_LOCAL_BACKLOG: set[str] = set()


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _parse(raw: object) -> datetime | None:
    if not raw:
        return None
    text = raw.decode() if isinstance(raw, bytes) else str(raw)
    try:
        value = datetime.fromisoformat(text)
    except ValueError:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _key(job: Job, part: str) -> str:
    return f"{_PREFIX}{job.name}:{part}"


def _seconds(delta: timedelta) -> int:
    return max(1, int(delta.total_seconds()))


async def _redis() -> Any:
    from app.core.redis import redis_client

    return await redis_client()


async def _read_last(redis: Any, job: Job) -> tuple[datetime | None, datetime | None]:
    raw = await redis.get(_key(job, "last"))
    if not raw:
        return None, None
    try:
        body = json.loads(raw)
    except (TypeError, ValueError):
        return None, None
    return _parse(body.get("completed_at")), _parse(body.get("started_at"))


def _due(last_completed: datetime | None, job: Job, now: datetime) -> bool:
    return last_completed is None or now - last_completed >= job.interval


async def claim(job: Job, *, now: datetime | None = None) -> Claim | None:
    """The claim of a due pass of *job* for this process, ``None`` when not due or taken elsewhere."""
    now = now or _now()
    token = uuid.uuid4().hex
    try:
        redis = await _redis()
        await redis.set(_key(job, "since"), now.isoformat(), nx=True)
        last_completed, _ = await _read_last(redis, job)
        if not _due(last_completed, job, now):
            return None
        if not await redis.set(_key(job, "lock"), token, nx=True, ex=_seconds(job.lock_ttl)):
            return None
        await redis.set(_key(job, "started"), now.isoformat())
    except Exception as exc:  # Redis down: this process alone decides, at most once per interval
        logger.debug("Schedule of %s read locally: %s", job.name, type(exc).__name__)
        started = _LOCAL_STARTED.get(job.name)
        if started is not None and now - started < max(job.interval, job.lock_ttl):
            return None
        _LOCAL_STARTED[job.name] = now
    return Claim(job=job, token=token, started_at=now)


async def complete(claim_: Claim, *, now: datetime | None = None) -> None:
    """Record the end of the claimed pass and release its lock."""
    now = now or _now()
    job = claim_.job
    _LOCAL_COMPLETED[job.name] = now
    try:
        redis = await _redis()
        body = {"completed_at": now.isoformat(), "started_at": claim_.started_at.isoformat()}
        await redis.set(_key(job, "last"), json.dumps(body))
        await redis.eval(_RELEASE_LUA, 1, _key(job, "lock"), claim_.token)
    except Exception as exc:  # the next process re-runs the pass once the lock expires
        logger.warning("Schedule of %s: the end of the pass was not recorded (%s)", job.name, type(exc).__name__)


async def status(job: Job, *, now: datetime | None = None) -> JobStatus:
    """The job's last pass, next pass and whether it is overdue."""
    now = now or _now()
    try:
        redis = await _redis()
        last_completed, last_started = await _read_last(redis, job)
        since = _parse(await redis.get(_key(job, "since")))
        started = _parse(await redis.get(_key(job, "started")))
        last_started = max((t for t in (last_started, started) if t), default=None)
    except Exception:
        last_completed = _LOCAL_COMPLETED.get(job.name)
        last_started = _LOCAL_STARTED.get(job.name)
        since = None
    next_due = last_completed + job.interval if last_completed else None
    if next_due is not None and next_due <= now:
        next_due = None
    reference = last_completed or since
    overdue = reference is not None and now - reference > 2 * job.interval
    return JobStatus(
        last_completed_at=last_completed,
        last_started_at=last_started,
        since=since,
        next_due_at=next_due,
        overdue=overdue,
    )


async def set_backlog(job: Job, pending: bool) -> None:
    """Say whether the last bounded pass of *job* left work behind."""
    if pending:
        _LOCAL_BACKLOG.add(job.name)
    else:
        _LOCAL_BACKLOG.discard(job.name)
    try:
        redis = await _redis()
        if pending:
            await redis.set(_key(job, "backlog"), "1", ex=_seconds(BACKLOG_TTL))
        else:
            await redis.delete(_key(job, "backlog"))
    except Exception as exc:
        logger.debug("Backlog of %s kept locally: %s", job.name, type(exc).__name__)


async def backlog_pending(job: Job) -> bool:
    """Whether a pass of *job* left work behind (a drain step runs while it does)."""
    try:
        redis = await _redis()
        return bool(await redis.get(_key(job, "backlog")))
    except Exception:
        return job.name in _LOCAL_BACKLOG
