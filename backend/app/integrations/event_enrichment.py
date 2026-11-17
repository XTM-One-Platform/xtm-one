# SPDX-License-Identifier: Apache-2.0

"""Facts a provider adds to an event before the trigger filter reads it.

A webhook body says what the sender chose to put in it. A GitHub CI result
(``workflow_run``, ``check_suite``...) names the commit and who pushed it, never
the author of the pull request the commit belongs to, so a trigger cannot
filter "a CI result on a pull request opened by X" on the body alone. A
provider registers an :class:`EventEnricher` that reads what is missing with
the integration's token; the generic layer (``services/event_enrichment.py``)
calls it only when a matched filter names the ``enriched.`` namespace, caches
the answer, and evaluates the filter on the payload with the answer under
``enriched``. The run receives the delivery as it came: the answer was read
with one assignment's credentials and is never handed to another. A provider
without an enricher is unchanged.

Light on purpose (no model, no service import): provider packages register
their enricher when they are imported, like ``pull_request_events``.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass


@dataclass(frozen=True)
class EventEnricher:
    """How one provider enriches its events."""

    #: Whether the event (``workflow_run.completed`` form) is enriched at all.
    applies: Callable[[str, dict], bool]
    #: A key naming what the answer depends on (``owner/repo@sha``), ``None``
    #: when the event cannot be enriched (no head commit): nothing is read.
    cache_key: Callable[[str, dict], str | None]
    #: The facts, read with *token*: a JSON object the filter reads under
    #: ``enriched``. Raises :class:`EnrichmentError` (or anything) when they
    #: cannot be read. With :attr:`facets` set, it is called with the
    #: ``facets=`` keyword too (``None``: every facet).
    fetch: Callable[..., Awaitable[dict]]
    #: Whether a failed read of this event is worth evaluating again later
    #: (a CI completion: no later delivery may come for its head), ``None``
    #: when no event of the provider is. A :class:`PermanentEnrichmentError`
    #: is never evaluated again.
    retryable: Callable[[str, dict], bool] | None = None
    #: The optional parts of the facts a filter reads, from the event and the
    #: payload keys the filter names (``integration_event_filters.filter_keys``):
    #: a costly part no key reads (GitHub: the head's check runs) is not read,
    #: so its outage or cap never drops a filter that does not need it. The
    #: answer is part of the cache key. ``None``: every part, always.
    facets: Callable[[str, dict, tuple[str, ...]], frozenset[str]] | None = None
    #: The subject a retryable event's facts belong to, without the delivery's
    #: own identity (GitHub: ``owner/name@sha``), ``None`` when it names none.
    #: With :attr:`settled` and :attr:`best_case`, a retryable event the
    #: filter dropped while its facts may still change, and that the filter
    #: would admit once they settle at best, is watched on that subject
    #: (``services/event_enrichment_retry.py``): the delivery that would have
    #: changed them can be lost (a rollout), and nothing else re-reads them.
    watch_subject: Callable[[str, dict], str | None] | None = None
    #: Whether *facts* (the enricher's answer) can no longer change without
    #: a further delivery (GitHub: every check of the head completed).
    settled: Callable[[dict], bool] | None = None
    #: *facts* as they would read once settled at best (GitHub: every running
    #: check completed successfully): a filter that drops even this view is
    #: never watched, so only a subject that may still match is read again.
    best_case: Callable[[dict], dict] | None = None


class EnrichmentError(Exception):
    """The provider did not answer what the enrichment needs; the message says why."""


class PermanentEnrichmentError(EnrichmentError):
    """An enrichment a second read would not answer either (a cap, an event naming no head)."""


_ENRICHERS: dict[str, EventEnricher] = {}


def register_event_enricher(provider: str, enricher: EventEnricher) -> None:
    """Register *provider*'s enricher. Idempotent (last wins)."""
    _ENRICHERS[provider] = enricher


def event_enricher(provider: str) -> EventEnricher | None:
    """The enricher *provider* registered, ``None`` when it has none."""
    return _ENRICHERS.get(provider)
