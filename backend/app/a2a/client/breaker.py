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

"""Stop hammering a partner that keeps failing — and only for the right kind.

FR-030. Consecutive **transient** failures open the circuit for a cooldown
that lengthens on repeat. Durable failures never touch it: retrying a rejected
credential cannot succeed, so counting one toward a cooldown would delay the
operator's only real signal — the registration going unusable with a reason.

State lives in Postgres, on the target row, not in Redis. Two in-repo
patterns existed and this one wins on the requirement that made FR-053 exist:
the health verdict must state WHY a registration is unusable, and a Redis key
with a TTL cannot be surfaced as a durable reason or survive a restart. One
row, one classification, both surfaces reading it.

Circuit state is per target — one row per partner agent. Two partners bound
to one agent must not suppress each other: a failing partner is a fact about
that partner, not about the agent.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from app.a2a.client.failures import Classification
from app.core.config import settings as app_settings

logger = logging.getLogger(__name__)

#: How much longer each successive cooldown lasts. A partner down for an hour
#: should not be probed every five minutes for that hour, and a partner that
#: blipped once should not be locked out for an hour.
_BACKOFF_FACTOR = 2
#: Ceiling on the backoff, so a long outage cannot push the next retry beyond
#: the point where an operator would reasonably expect the platform to try
#: again on its own.
_MAX_COOLDOWN_SECONDS = 3600


def is_open(target) -> bool:
    """Whether delegation to this partner is currently paused."""
    until = target.circuit_open_until
    if until is None:
        return False
    if until.tzinfo is None:
        until = until.replace(tzinfo=UTC)
    return datetime.now(UTC) < until


def record_failure(target, classification: Classification) -> bool:
    """Account one failure. Returns ``True`` if this opened the circuit.

    A durable failure returns ``False`` and leaves the counter untouched —
    the caller marks the registration unusable instead. Splitting that
    decision across two places would be the disagreement FR-053 forbids, so
    it is made here, once, from the classification both surfaces read.
    """
    if classification.is_durable:
        target.last_failure_kind = classification.kind
        return False

    target.last_failure_kind = classification.kind
    target.consecutive_transient_failures += 1
    threshold = app_settings.a2a_client_breaker_threshold
    if target.consecutive_transient_failures < threshold:
        return False

    # How many cooldowns deep we are: the first breach is one base cooldown,
    # each further breach doubles it.
    breaches = target.consecutive_transient_failures - threshold
    seconds = min(
        app_settings.a2a_client_breaker_cooldown_seconds * (_BACKOFF_FACTOR**breaches),
        _MAX_COOLDOWN_SECONDS,
    )
    target.circuit_open_until = datetime.now(UTC) + timedelta(seconds=seconds)
    logger.info(
        "a2a.client: circuit open for integration %s for %ss after %s consecutive "
        "transient failures",
        target.integration_id,
        seconds,
        target.consecutive_transient_failures,
    )
    return True


def record_success(target) -> None:
    """A delegation succeeded: forget the run of failures.

    Consecutive means consecutive. A partner that fails four times and then
    answers has not accumulated four strikes toward a future cooldown — it is
    working, and the next failure starts a new run.
    """
    target.consecutive_transient_failures = 0
    target.circuit_open_until = None
    target.last_failure_kind = None


def close(target) -> None:
    """Close the circuit now, on a successful on-demand verification.

    FR-030's escape hatch. An operator who has just corrected a credential
    must not be made to wait out a cooldown that was counting a symptom of
    the thing they fixed.
    """
    record_success(target)
