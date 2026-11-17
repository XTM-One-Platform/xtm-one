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

"""One answer per inbound message on an agent, across its pipelines.

An agent hears one email through two pipelines that know nothing of each
other: an ``integration_event`` assignment on the mailbox (the engine, which
refuses a second run of the same dedup key under an agent-wide advisory lock,
``services/event_run_dedup.py``) and an agent account on the same mailbox
(``services/agent_accounts/pipeline.py``, with its own per-account seen set).
Each one's dedup is blind to the other, so both answered the same mail within
a minute (2 Nov 2026: a calendar invite from the assignment run, a reply from
the agent account).

This module is the claim both take before acting: one Redis key per agent and
dedup key (``event_run_dedup.mail_dedup_key``, the mail's RFC 5322
Message-ID), written ``SET NX EX`` so exactly one holder wins it, whichever
pipeline arrives first. The value names the holder:

- :data:`ASSIGNMENT_HOLDER` for the engine: every assignment of the agent
  shares it, the engine's own run dedup already settles which assignment runs;
- ``agent_account:<id>`` (:func:`account_holder`) for an agent account.

A claim is re-entrant for its holder (a parked agent-account message taken back
after its grace period, a poll of a mail a notification already started) and
lasts the dedup window. It is released only by its holder, and only when that
holder ended without acting (a delivery that created no run, an agent-account
turn that posted nothing), so the other pipeline may still answer.

Redis unreachable: the claim fails open (``None``, "acquired") and each
pipeline falls back on what it had before; the engine's database dedup still
stands for assignment runs.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

#: The holder every assignment of an agent claims under.
ASSIGNMENT_HOLDER = "assignment"
#: How an agent account's holder value starts.
ACCOUNT_HOLDER_PREFIX = "agent_account:"

_KEY_PREFIX = "inbound_claim"

# Delete the key only while it still names the caller: a claim that expired
# and was taken by the other pipeline meanwhile is never released by mistake.
_RELEASE_SCRIPT = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('DEL', KEYS[1])
end
return 0
"""


def account_holder(account_id: Any) -> str:
    """The holder value of the agent account *account_id*."""
    return f"{ACCOUNT_HOLDER_PREFIX}{account_id}"


def claim_key(agent_id: Any, dedup_key: str) -> str:
    """The Redis key of the claim of *dedup_key* on *agent_id*."""
    return f"{_KEY_PREFIX}:{agent_id}:{dedup_key}"


def holder_parts(holder: str | None) -> tuple[str | None, str | None]:
    """*holder* as data: ``("assignment", None)``, ``("agent_account", <id>)`` or ``(None, None)``.

    What the activity page renders in the viewer's language.
    """
    if not holder:
        return None, None
    if holder.startswith(ACCOUNT_HOLDER_PREFIX):
        return "agent_account", holder[len(ACCOUNT_HOLDER_PREFIX) :] or None
    if holder == ASSIGNMENT_HOLDER:
        return "assignment", None
    return None, None


def describe_holder(holder: str) -> str:
    """A holder in words, for a skipped run's summary or a log line."""
    if holder.startswith(ACCOUNT_HOLDER_PREFIX):
        return f"the agent account {holder[len(ACCOUNT_HOLDER_PREFIX):]}"
    if holder == ASSIGNMENT_HOLDER:
        return "an assignment run of this agent"
    return holder


async def _redis() -> Any | None:
    try:
        from app.core.redis import redis_client

        return await redis_client()
    except Exception:
        logger.debug("Redis unavailable for inbound claims", exc_info=True)
        return None


def _text(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return "" if value is None else str(value)


async def claim(agent_id: Any, dedup_key: str | None, holder: str, *, window_seconds: int) -> str | None:
    """Claim *dedup_key* on *agent_id* for *holder*; the other holder when it already has it.

    ``None`` means *holder* may act: the claim was taken, it already held it,
    there is nothing to claim (no agent, no key, a window of 0), or Redis did
    not answer (fail open). A string is the holder that got there first:
    *holder* must stop before any side effect.
    """
    if agent_id is None or not dedup_key or window_seconds <= 0:
        return None
    redis = await _redis()
    if redis is None:
        return None
    key = claim_key(agent_id, dedup_key)
    # Two attempts: a claim that expires between SET NX and GET is taken again.
    for _attempt in range(2):
        try:
            if await redis.set(key, holder, nx=True, ex=int(window_seconds)):
                return None
            current = _text(await redis.get(key))
        except Exception:
            logger.debug("Inbound claim failed open for %s", key, exc_info=True)
            return None
        if current:
            return None if current == holder else current
    return None


async def release(agent_id: Any, dedup_key: str | None, holder: str) -> None:
    """Give back *holder*'s claim of *dedup_key* on *agent_id*. Never raises."""
    if agent_id is None or not dedup_key:
        return
    redis = await _redis()
    if redis is None:
        return
    try:
        await redis.eval(_RELEASE_SCRIPT, 1, claim_key(agent_id, dedup_key), holder)
    except Exception:
        logger.debug("Inbound claim release failed for %s", dedup_key, exc_info=True)
