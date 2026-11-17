# SPDX-License-Identifier: Apache-2.0

"""Prometheus collector helpers shared across services.

Registration is idempotent: ``prometheus_client`` raises ``Duplicated
timeseries`` when the same metric name is registered twice in a process, and
service modules ARE re-imported in practice (tests pop them from
``sys.modules``; ``importlib.reload`` would do the same), so a plain
module-level ``Counter(...)`` call crashes the re-import.  On a duplicate we
reuse the collector already in the default ``REGISTRY``.

Values are PER-PROCESS: with multiple web workers (``WEB_WORKERS``, default 2)
a ``/metrics`` scrape only sees the handling worker's own counts, and the next
scrape of the same pod may reach another worker. A series is a sample of one
process, not a counter ``rate()`` / ``increase()`` can follow: the jump from
one worker's value to another's reads as a counter reset and is over-counted.
Logs remain the authoritative per-event record.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from prometheus_client import REGISTRY, Counter, Histogram


def _get_or_create(cls: type, name: str, documentation: str, labelnames: list[str], **kwargs: Any) -> Any:
    try:
        return cls(name, documentation, labelnames, **kwargs)
    except ValueError:
        # The registry keeps no public accessor, hence the private-map lookup.
        existing = REGISTRY._names_to_collectors.get(name)
        if not isinstance(existing, cls) or tuple(existing._labelnames) != tuple(labelnames):
            raise
        return existing


def get_or_create_counter(
    name: str, documentation: str, labelnames: list[str]
) -> Counter:
    """Return the ``Counter`` named *name*, creating it on first call.

    Anything other than an identical, compatible ``Counter`` already in the
    registry (missing collector, different collector type, different label
    set) is a DIFFERENT failure — the original ``ValueError`` is re-raised so
    the import/startup error stays explicit instead of surfacing later as a
    broken ``.labels(...).inc()``.
    """
    return _get_or_create(Counter, name, documentation, labelnames)


def get_or_create_histogram(
    name: str, documentation: str, labelnames: list[str], buckets: Sequence[float]
) -> Histogram:
    """The ``Histogram`` counterpart of :func:`get_or_create_counter`.

    A ``Histogram`` registers its ``_bucket`` / ``_count`` / ``_sum`` series
    under its own *name*, so the duplicate lookup is the same.
    """
    return _get_or_create(Histogram, name, documentation, labelnames, buckets=tuple(buckets))
