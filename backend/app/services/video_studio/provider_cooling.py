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

"""A video provider that refused for its quota or its rate limit cools down, for the whole batch.

A shot asked of a provider that answers HTTP 429 - or a refusal its adapter reads as a quota
(``VideoProviderBusyError.quota``) - met the provider's state, not its own: every shot queued on that provider
would meet it too. So the refusal puts the provider configuration in a cooling state (:func:`note_refusal`) until
the wait it asked for (``VideoProviderBusyError.retry_after``: ``Retry-After``, or the delay its error carries),
else a backoff that doubles with each refusal in a row (:data:`FIRST_BACKOFF_SECONDS` up to
:data:`MAX_BACKOFF_SECONDS`). A job the provider then creates ends the streak (:func:`clear`).

A quota is usually the model's (Veo counts each model apart): a refusal read as a quota cools that model
of the provider alone, a plain 429 the whole provider; every reader asks for the model it would use and meets
either. "In a row" is per refusal event: the shots of a batch refused while the cooling lasts are the
same event and add nothing to the backoff, and only a refusal after the cooling ended, inside
:data:`STREAK_SECONDS`, doubles it. Every write is a compare-and-set on the key (:data:`_CAS_SCRIPT`), retried
on a lost race, so two workers never shorten a cooling nor count one refusal twice.

It is read before anything is paid: ``video_studio_capabilities`` (each model's ``cooling``),
``generate_video_clip(validate_only)`` (``provider_state``), ``clips.start_clip`` / ``restart_clip`` (a shot queued
on a cooling provider is due once it cools, its row saying why: :func:`hold`), and the worker, which holds a shot
it would ask of a cooling provider for the cooling's end (``runners.JobOutcome("hold")``, the adapter's
``orchestration_engine.defer``: no attempt spent) - so a quota is never written as a shot's failure. A
shot held longer than :data:`MAX_HOLD` for a quota ends, saying why.

One Redis key per provider configuration, and one per model that refused for its quota, so every worker and the
API read the same state. With Redis unreachable no cooling is known: the shot is asked as before, and a refusal
meets the job's own retry.
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from app.core.redis import redis_client
from app.core.utils import format_exception_message

logger = logging.getLogger(__name__)

_PREFIX = "video_studio:provider_cooling:"
#: The wait after a refusal that named none, doubling with each refusal in a row, up to the cap.
FIRST_BACKOFF_SECONDS = 60.0
MAX_BACKOFF_SECONDS = 15 * 60.0
#: How long a model whose quota refused cools when the refusal named no wait: credits come back by the hour,
#: never by the minute a rate limit does.
QUOTA_COOLING_SECONDS = 3600.0
#: The longest wait a provider may ask for that is honoured as it is (a day's quota).
MAX_RETRY_AFTER_SECONDS = 24 * 3600.0
#: How long a refusal is remembered once its cooling is over: the next one in that span is in a row.
STREAK_SECONDS = 15 * 60.0
#: How long a shot waits in the queue for a provider that cools for its quota before it ends, saying why
#: (the adapter's ``_held``; a plain rate limit is waited out ``RATE_LIMIT_HOLD_SECONDS``).
MAX_HOLD = timedelta(hours=24)
#: ``provider_state`` of the row of a shot held in the queue for its provider (``jobs.job_state``: queued, held).
HELD_BY_PROVIDER = "cooling"
#: Why a provider cools: its quota, else its rate limit.
QUOTA = "quota exceeded"
RATE_LIMITED = "rate limited"
#: Writes a cooling only when the key still holds what the writer read (``""`` for nothing): 1 written, 0 lost.
_CAS_SCRIPT = """
local current = redis.call('GET', KEYS[1])
if (current == false and ARGV[1] == '') or current == ARGV[1] then
  redis.call('SET', KEYS[1], ARGV[2], 'EX', ARGV[3])
  return 1
end
return 0
"""
#: How often a write lost to another worker is read and computed again before it gives up.
_CAS_ATTEMPTS = 8


def _now() -> datetime:
    return datetime.now(UTC)


def _clock(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%H:%M:%S UTC")


def retry_line(until: datetime, now: datetime | None = None) -> str:
    """``retry after 17:45:12 UTC (in 37 s)``."""
    left = max(0, math.ceil((until - (now or _now())).total_seconds()))
    return f"retry after {_clock(until)} (in {left} s)"


@dataclass(frozen=True)
class Cooling:
    """A provider configuration that refused: until when it is not asked, why, since when."""

    provider_id: str
    label: str
    reason: str
    until: datetime
    since: datetime
    refusals: int = 1
    status_code: int | None = None
    #: The provider's own words, short.
    detail: str | None = None
    #: The model whose quota refused; ``None``: the whole provider cools.
    model: str | None = None
    #: A quota refusal's credits: those the account had left, and those the request needed, when it said them.
    remaining: int | None = None
    needed: int | None = None

    def seconds_left(self, now: datetime | None = None) -> float:
        return max(0.0, (self.until - (now or _now())).total_seconds())

    @property
    def who(self) -> str:
        return f"{self.label} ({self.model})" if self.model else self.label

    @property
    def why(self) -> str:
        """``quota exceeded (quota: 3448 remaining, 7272 needed)``: the reason, with the credits a quota refusal said."""
        if self.reason == QUOTA and self.remaining is not None:
            needed = f", {self.needed} needed" if self.needed is not None else ""
            return f"{self.reason} (quota: {self.remaining} remaining{needed})"
        return self.reason

    def sentence(self, now: datetime | None = None) -> str:
        """``Gemini (veo-3.1-generate-preview): quota exceeded, retry after 17:45:12 UTC (in 37 s)``."""
        return f"{self.who}: {self.why}, {retry_line(self.until, now)}"

    def as_dict(self, now: datetime | None = None) -> dict[str, Any]:
        return {
            "state": "cooling",
            "provider_id": self.provider_id,
            "provider": self.label,
            "model": self.model,
            "reason": self.reason,
            "retry_after": self.until.isoformat(),
            "retry_after_seconds": math.ceil(self.seconds_left(now)),
            "since": self.since.isoformat(),
            "refusals": self.refusals,
            "status_code": self.status_code,
            "detail": self.detail,
            **({"remaining": self.remaining, "needed": self.needed} if self.remaining is not None or self.needed is not None else {}),
            "message": self.sentence(now),
        }

    def dumps(self) -> str:
        return json.dumps(
            {
                "provider_id": self.provider_id,
                "label": self.label,
                "reason": self.reason,
                "until": self.until.isoformat(),
                "since": self.since.isoformat(),
                "refusals": self.refusals,
                "status_code": self.status_code,
                "detail": self.detail,
                "model": self.model,
                "remaining": self.remaining,
                "needed": self.needed,
            }
        )

    @classmethod
    def loads(cls, raw: Any) -> Cooling | None:
        try:
            data = json.loads(raw)
            return cls(
                provider_id=str(data["provider_id"]),
                label=str(data.get("label") or "The video provider"),
                reason=str(data.get("reason") or RATE_LIMITED),
                until=datetime.fromisoformat(data["until"]),
                since=datetime.fromisoformat(data.get("since") or data["until"]),
                refusals=int(data.get("refusals") or 1),
                status_code=data.get("status_code"),
                detail=data.get("detail"),
                model=data.get("model") or None,
                remaining=_count(data.get("remaining")),
                needed=_count(data.get("needed")),
            )
        except (TypeError, ValueError, KeyError):
            return None


def _count(value: Any) -> int | None:
    return int(value) if isinstance(value, int | float) and not isinstance(value, bool) else None


def _key(provider_id: object, model: str | None = None) -> str:
    return f"{_PREFIX}{provider_id}:{model}" if model else f"{_PREFIX}{provider_id}"


def _keys(provider_id: object, model: str | None) -> list[str]:
    """What a shot asked of *model* on the provider meets: the provider's cooling, and its model's."""
    return [_key(provider_id), *([_key(provider_id, model)] if model else [])]


def _latest(raws: Any, moment: datetime) -> Cooling | None:
    """The cooling among *raws* that lasts the longest, while it lasts."""
    found = [cooling for raw in raws or () if raw and (cooling := Cooling.loads(raw)) is not None]
    live = [cooling for cooling in found if cooling.until > moment]
    return max(live, key=lambda cooling: cooling.until) if live else None


def is_refusal(exc: BaseException) -> bool:
    """Whether a provider's busy answer is a refusal the whole batch shares: a quota its adapter read, or HTTP 429
    refusing to start the job - never a job it started and then ended for its load (``started``), which the shot
    waits out on its own schedule (``provider_waits``)."""
    if getattr(exc, "quota", False):
        return True
    return getattr(exc, "status_code", None) == 429 and not getattr(exc, "started", False)


async def cooling_of(provider_id: object, *, model: str | None = None, now: datetime | None = None) -> Cooling | None:
    """What a shot asked of *model* on the provider meets while it lasts: the provider's or that model's cooling."""
    if not provider_id:
        return None
    try:
        raws = await (await redis_client()).mget(_keys(provider_id, model))
    except Exception as exc:
        logger.debug("The cooling of video provider %s was not read: %s", provider_id, format_exception_message(exc))
        return None
    return _latest(raws, now or _now())


async def coolings(asked: Any, *, now: datetime | None = None) -> dict[tuple[str, str | None], Cooling]:
    """``{(provider_id, model): cooling}`` of the ``(provider_id, model)`` pairs in *asked* that cool now."""
    pairs = sorted({(str(pid), model or None) for pid, model in asked or () if pid}, key=lambda p: (p[0], p[1] or ""))
    if not pairs:
        return {}
    keys = [_keys(pid, model) for pid, model in pairs]
    try:
        values = await (await redis_client()).mget([key for group in keys for key in group])
    except Exception as exc:
        logger.debug("The cooling of the video providers was not read: %s", format_exception_message(exc))
        return {}
    moment = now or _now()
    found: dict[tuple[str, str | None], Cooling] = {}
    values = list(values or [])
    for pair, group in zip(pairs, keys, strict=True):
        raws, values = values[: len(group)], values[len(group) :]
        if (cooling := _latest(raws, moment)) is not None:
            found[pair] = cooling
    return found


def _backoff(refusals: int) -> float:
    return min(FIRST_BACKOFF_SECONDS * 2 ** (refusals - 1), MAX_BACKOFF_SECONDS)


def next_cooling(
    prior: Cooling | None,
    *,
    provider_id: str,
    label: str,
    model: str | None,
    quota: bool,
    status_code: int | None,
    retry_after: float | None,
    detail: str | None,
    moment: datetime,
    remaining: int | None = None,
    needed: int | None = None,
) -> Cooling:
    """The cooling a refusal at *moment* leaves, *prior* being what the key held.

    While *prior* lasts the refusal is the same event as the one that set it - a batch refused together: the
    count stays, and the end moves only further, to a wait the provider names. After it, inside
    :data:`STREAK_SECONDS`, it is the next refusal in a row: the backoff doubles. Else the streak starts again.
    A quota naming no wait cools :data:`QUOTA_COOLING_SECONDS`, the credits it said kept.
    """
    asked = min(float(retry_after), MAX_RETRY_AFTER_SECONDS) if retry_after is not None and retry_after > 0 else None
    if asked is None and quota:
        asked = QUOTA_COOLING_SECONDS
    active = prior is not None and prior.until > moment
    in_a_row = prior is not None and not active and prior.until + timedelta(seconds=STREAK_SECONDS) > moment
    if active:
        refusals = prior.refusals
        until = max(prior.until, moment + timedelta(seconds=asked)) if asked is not None else prior.until
    else:
        refusals = prior.refusals + 1 if in_a_row else 1
        until = moment + timedelta(seconds=asked if asked is not None else _backoff(refusals))
    if remaining is None and needed is None and active:
        remaining, needed = prior.remaining, prior.needed
    return Cooling(
        provider_id=provider_id,
        label=label or "The video provider",
        reason=QUOTA if quota or (active and prior.reason == QUOTA) else RATE_LIMITED,
        until=until,
        since=prior.since if active or in_a_row else moment,
        refusals=refusals,
        status_code=status_code,
        detail=(detail or "")[:300] or None,
        model=model,
        remaining=remaining,
        needed=needed,
    )


async def note_refusal(
    provider_id: object,
    *,
    label: str,
    model: str | None = None,
    status_code: int | None = None,
    retry_after: float | None = None,
    quota: bool = False,
    detail: str | None = None,
    remaining: int | None = None,
    needed: int | None = None,
    now: datetime | None = None,
) -> Cooling | None:
    """Record that the provider refused for its quota or its rate limit: its cooling, or ``None`` (Redis unreachable).

    A quota cools *model* on the provider, a plain rate limit the whole provider (:func:`next_cooling`
    decides the wait); a quota's credits (*remaining*, *needed*) are kept with it - what a shot asked of that
    model next is refused for before it is paid. The write is a compare-and-set, computed again from what another
    worker wrote meanwhile.
    """
    if not provider_id:
        return None
    moment = now or _now()
    cooled = model if quota and model else None
    key = _key(provider_id, cooled)
    try:
        client = await redis_client()
        for _attempt in range(_CAS_ATTEMPTS):
            raw = await client.get(key)
            cooling = next_cooling(
                Cooling.loads(raw) if raw else None,
                provider_id=str(provider_id),
                label=label,
                model=cooled,
                quota=quota,
                status_code=status_code,
                retry_after=retry_after,
                detail=detail,
                moment=moment,
                remaining=remaining,
                needed=needed,
            )
            ttl = max(1, math.ceil((cooling.until - moment).total_seconds() + STREAK_SECONDS))
            if int(await client.eval(_CAS_SCRIPT, 1, key, raw or "", cooling.dumps(), ttl) or 0):
                logger.info("Video provider %s cools until %s: %s", cooling.who, cooling.until.isoformat(), cooling.reason)
                return cooling
    except Exception as exc:
        logger.warning("The cooling of video provider %s was not recorded: %s", provider_id, format_exception_message(exc))
        return None
    logger.warning("The cooling of video provider %s was not recorded: every write met another one", provider_id)
    return None


async def clear(provider_id: object, *, model: str | None = None) -> None:
    """The provider took a request for *model*: its refusals, and that model's, are no longer in a row."""
    if not provider_id:
        return
    try:
        await (await redis_client()).delete(*_keys(provider_id, model))
    except Exception as exc:
        logger.debug("The cooling of video provider %s was not cleared: %s", provider_id, format_exception_message(exc))


def held_changes(cooling: Cooling) -> dict[str, Any]:
    """The row of a shot waiting in the queue for its provider to cool (``jobs.job_state``: queued, held)."""
    return {
        "stage": f"Waiting in the queue: {cooling.who}: {cooling.why}"[:200],
        "provider_state": HELD_BY_PROVIDER,
        "provider_eta_at": cooling.until,
        "progress_at": None,
    }


def hold(row: Any, cooling: Cooling) -> None:
    """Say on a queued shot's row that it waits for its provider to cool (:func:`held_changes`)."""
    for key, value in held_changes(cooling).items():
        setattr(row, key, value)


def held_line(stage: str | None, until: datetime | None, now: datetime | None = None) -> str:
    """A held shot's job line, read at every call: ``waiting in the queue: Gemini: quota exceeded, retry after ...``."""
    text = stage or "Waiting in the queue for the provider"
    text = text[:1].lower() + text[1:]
    return f"{text}, {retry_line(until, now)}" if until is not None else text
