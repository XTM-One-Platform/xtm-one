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

"""The ``enriched`` facts a trigger filter reads, for one assignment.

A provider registers an enricher (``integrations/event_enrichment.py``); this
module runs it for an ``integration_event`` assignment whose filter names the
``enriched.`` namespace (``integration_event_filters.needs_enrichment``), with
the credential that assignment's integration reads with - the event poller's
rule: ``trigger_config.integration_id`` else the agent's first binding, the
personal-token and ``per_user`` rules of the agent's owner. Nothing else pays
for it: an assignment whose filter does not name ``enriched.``, or a provider
without an enricher, reads nothing.

The answer is cached in Redis per integration and ``cache_key`` (a repository
and head commit for GitHub) for :data:`CACHE_TTL_SECONDS`, so a burst of CI
events on one commit reads GitHub once. A provider declaring
``EventEnricher.facets`` reads only the optional parts the filter's keys need
(GitHub: the head's check runs only for a filter on ``enriched.head.checks``);
those parts are in the cache key, so an answer without a part never answers a
filter that reads it. When the facts cannot be read (no
credential, GitHub refusing, a timeout), the filter sees
``enriched.unavailable`` and every predicate on an ``enriched.`` key drops the
event naming why: a filter written to narrow never widens on an outage.

The facts are filter input only: the run receives the delivery as it came.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.utils import format_exception_message, safe_uuid
from app.integrations.event_enrichment import PermanentEnrichmentError, event_enricher
from app.models.integration import Integration
from app.services.integration_credentials import agent_acting_user_id
from app.services.integration_event_filters import (
    ENRICHED_KEY,
    ENRICHMENT_UNAVAILABLE_KEY,
    filter_keys,
    needs_enrichment,
    trigger_filter,
)
from app.services.integration_user_credentials import connected_for_clause, reader_clause, resolve_token_holder
from app.services.trigger_integration_access import trigger_integration_id

logger = logging.getLogger(__name__)

#: Set under ``enriched`` next to ``unavailable`` when a second read may
#: answer (a timeout, a provider error) and the provider marks the event
#: retryable: the receiver then evaluates the delivery again later
#: (``services/event_enrichment_retry.py``) instead of dropping it for good.
ENRICHMENT_RETRYABLE_KEY = "retryable"

CACHE_KEY_PREFIX = "integration_event:enriched:"
CACHE_TTL_SECONDS = 600
#: The whole enrichment of one event, the provider read included.
FETCH_TIMEOUT_SECONDS = 10.0
_REDIS_TIMEOUT_SECONDS = 0.5


def _same_id(a: Any, b: Any) -> bool:
    """Whether two integration ids name one integration (any UUID spelling)."""
    ua, ub = safe_uuid(str(a)), safe_uuid(str(b))
    if ua is not None and ub is not None:
        return ua == ub
    return str(a).strip().lower() == str(b).strip().lower()


def _unavailable(why: str) -> dict[str, Any]:
    return {ENRICHMENT_UNAVAILABLE_KEY: why}


async def _redis() -> Any | None:
    try:
        from app.core.redis import redis_client

        return await asyncio.wait_for(redis_client(), _REDIS_TIMEOUT_SECONDS)
    except Exception as exc:
        logger.debug("Event enrichment cache unavailable: %s", format_exception_message(exc))
        return None


async def _cached(key: str) -> dict[str, Any] | None:
    redis = await _redis()
    if redis is None:
        return None
    try:
        raw = await asyncio.wait_for(redis.get(key), _REDIS_TIMEOUT_SECONDS)
        value = json.loads(raw) if raw else None
    except Exception as exc:
        logger.debug("Event enrichment cache not read: %s", format_exception_message(exc))
        return None
    return value if isinstance(value, dict) else None


async def _remember(key: str, facts: dict[str, Any]) -> None:
    redis = await _redis()
    if redis is None:
        return
    try:
        await asyncio.wait_for(redis.set(key, json.dumps(facts), ex=CACHE_TTL_SECONDS), _REDIS_TIMEOUT_SECONDS)
    except Exception as exc:
        logger.debug("Event enrichment cache not written: %s", format_exception_message(exc))


async def _credential(db: AsyncSession, provider: str, integration_id: Any, agent: Any) -> tuple[str, str] | None:
    """``(token, holder key)`` the assignment's integration reads with, ``None`` when there is none it may use.

    The integration must be one of *provider*'s (a token is never sent to
    another provider's API), readable and connected for the agent's acting
    user. The holder key names whose credential answers (the integration's
    own, or one user's on a per-user integration): the cache is keyed on it.
    """
    from app.services.vault import reveal_field

    parsed = safe_uuid(str(integration_id)) if integration_id else None
    if parsed is None:
        return None
    acting = agent_acting_user_id(agent)
    integration = (
        await db.execute(
            select(Integration).where(
                Integration.id == parsed,
                Integration.provider == provider,
                connected_for_clause(acting),
                reader_clause(acting),
            )
        )
    ).scalar_one_or_none()
    if integration is None:
        return None
    holder = await resolve_token_holder(db, integration, acting)
    if holder is None:
        return None
    token = await reveal_field(holder, "access_token_enc")
    if not token and holder is integration:
        token = await reveal_field(integration, "api_key_enc")
    if not token:
        return None
    holder_key = "integration" if holder is integration else f"user:{getattr(holder, 'user_id', None) or acting}"
    return token, holder_key


async def enrichment_for(
    db: AsyncSession,
    provider: str,
    event_key: str,
    payload: dict,
    *,
    trigger_config: Any,
    agent: Any,
    receiver_integration_id: Any = None,
    memo: dict[str, dict[str, Any]] | None = None,
    fresh: bool = False,
) -> dict[str, Any] | None:
    """The ``enriched`` facts of *payload* for one assignment, ``None`` when its filter needs none.

    *receiver_integration_id* is the integration the delivery was received on
    (the integration URL, the per-assignment URL): it reads the facts, and an
    assignment pinned to another integration gets none. Without it (the
    provider URL), the assignment's own integration reads them.
    The credential is authorised for this assignment before the cache is read,
    and the cache is keyed on whose credential answered, so one user's view is
    never served to another. *memo* (one per delivery) spares a second read.
    *fresh* reads the provider whatever the cache holds (a watched head read
    again, ``services/event_enrichment_retry.py``) and caches the answer.
    """
    filter_config = trigger_filter(trigger_config)
    if not needs_enrichment(filter_config) or not isinstance(payload, dict):
        return None
    enricher = event_enricher(provider)
    if enricher is None or not enricher.applies(event_key, payload):
        return _unavailable(f"{provider} {event_key.split('.', 1)[0]} events are not enriched")
    subject = enricher.cache_key(event_key, payload)
    if subject is None:
        return _unavailable("the event names no head commit")
    if receiver_integration_id:
        configured = trigger_config.get("integration_id") if isinstance(trigger_config, dict) else None
        if configured and not _same_id(configured, receiver_integration_id):
            return _unavailable("the delivery came through another integration than the assignment's")
        integration_id: Any = receiver_integration_id
    else:
        integration_id = trigger_integration_id(trigger_config, agent)
    if not integration_id:
        return _unavailable("the assignment reads through no integration")
    try:
        credential = await _credential(db, provider, integration_id, agent)
    except Exception as exc:
        logger.info(
            "Event enrichment credential of integration %s not read: %s",
            integration_id,
            format_exception_message(exc),
        )
        credential = None
    if credential is None:
        return _unavailable("the integration holds no credential the assignment may use")
    token, holder_key = credential
    canonical = safe_uuid(str(integration_id)) or str(integration_id).lower()
    key = f"{CACHE_KEY_PREFIX}{provider}:{canonical}:{holder_key}:{subject}"
    parts = _facets(enricher, event_key, payload, filter_config)
    if parts is not None:
        key = f"{key}|facets={','.join(sorted(parts))}"
    if memo is not None and key in memo:
        return memo[key]
    facts = None if fresh else await _cached(key)
    if facts is None:
        try:
            read = (
                enricher.fetch(event_key, payload, token)
                if parts is None
                else enricher.fetch(event_key, payload, token, facets=parts)
            )
            facts = await asyncio.wait_for(read, FETCH_TIMEOUT_SECONDS)
            if not isinstance(facts, dict):
                facts = _unavailable("the provider answered no facts")
            else:
                await _remember(key, facts)
        except Exception as exc:
            logger.info(
                "Event enrichment of %s %s (integration %s) failed: %s",
                provider,
                event_key,
                integration_id,
                format_exception_message(exc),
            )
            facts = _unavailable(format_exception_message(exc))
            if not isinstance(exc, PermanentEnrichmentError) and _retryable(enricher, event_key, payload):
                facts[ENRICHMENT_RETRYABLE_KEY] = True
    if memo is not None:
        memo[key] = facts
    return facts


def _facets(enricher: Any, event_key: str, payload: dict[str, Any], filter_config: Any) -> frozenset[str] | None:
    """The optional parts the filter reads, ``None`` when the provider reads every part (or cannot say)."""
    pick = getattr(enricher, "facets", None)
    if pick is None:
        return None
    try:
        return frozenset(pick(event_key, payload, tuple(filter_keys(filter_config))))
    except Exception as exc:
        logger.debug("Event enrichment facets not read: %s", format_exception_message(exc))
        return None


def _retryable(enricher: Any, event_key: str, payload: dict[str, Any]) -> bool:
    check = getattr(enricher, "retryable", None)
    if check is None:
        return False
    try:
        return bool(check(event_key, payload))
    except Exception:
        return False


def watch_subject(provider: str, event_key: str, payload: Any) -> str | None:
    """The subject a retryable event of *provider* is watched on, ``None`` when it is not watched."""
    enricher = event_enricher(provider)
    pick = getattr(enricher, "watch_subject", None)
    if pick is None or getattr(enricher, "settled", None) is None or not isinstance(payload, dict):
        return None
    if not _retryable(enricher, event_key, payload):
        return None
    try:
        subject = pick(event_key, payload)
    except Exception:
        return None
    return subject if isinstance(subject, str) and subject else None


def facts_settled(provider: str, facts: dict[str, Any] | None) -> bool:
    """Whether *facts* can no longer change without a further delivery; true when unknown or unread."""
    if not isinstance(facts, dict) or facts.get(ENRICHMENT_UNAVAILABLE_KEY):
        return True
    check = getattr(event_enricher(provider), "settled", None)
    if check is None:
        return True
    try:
        return bool(check(facts))
    except Exception:
        return True


def may_still_match(provider: str, event: str, payload: dict, facts: dict[str, Any] | None, filter_config: Any) -> bool:
    """Whether the filter that dropped *payload* would admit it once *facts* settle at best.

    False when the facts are settled or unread, or the provider cannot say:
    only a delivery whose own facts may still turn the filter true is watched.
    """
    from app.services.integration_event_filters import event_filter_reason

    if facts_settled(provider, facts):
        return False
    best = getattr(event_enricher(provider), "best_case", None)
    if best is None or not isinstance(facts, dict):
        return False
    try:
        hoped = best(facts)
    except Exception:
        return False
    return event_filter_reason(event, with_enrichment(payload, hoped), filter_config) is None


def as_watched_delivery(provider: str, facts: dict[str, Any] | None) -> dict[str, Any] | None:
    """Settled *facts* as a watched head reads them: the watch stands in for its lost last delivery.

    The provider's ``best_case`` view of settled facts (GitHub: ``last_to_complete``
    true); *facts* as they are when unsettled, unread or the provider cannot say.
    """
    if not isinstance(facts, dict) or facts.get(ENRICHMENT_UNAVAILABLE_KEY) or not facts_settled(provider, facts):
        return facts
    best = getattr(event_enricher(provider), "best_case", None)
    if best is None:
        return facts
    try:
        view = best(facts)
    except Exception:
        return facts
    return view if isinstance(view, dict) else facts


def enrichment_retryable(facts: dict[str, Any] | None) -> bool:
    """Whether *facts* failed to read for a reason a later read may not meet."""
    return (
        isinstance(facts, dict)
        and bool(facts.get(ENRICHMENT_UNAVAILABLE_KEY))
        and bool(facts.get(ENRICHMENT_RETRYABLE_KEY))
    )


def with_enrichment(payload: dict, facts: dict[str, Any] | None) -> dict:
    """*payload* as the filter reads it: a shallow copy with *facts* under ``enriched``."""
    if facts is None or not isinstance(payload, dict):
        return payload
    return {**payload, ENRICHED_KEY: facts}
