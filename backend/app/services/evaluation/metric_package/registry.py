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

"""Every metric the package declares, read in one walk.

The directions (``directions.py``), the selectable catalogue (``catalog.py``) and the units
(``units.py``) are three views of the same metadata. One walk and one cache feed all three,
so a metric cannot be found by one view and missed by another; each view applies its own
rule:

- the directions drop a metric whose direction is not one of ``VALID_DIRECTIONS``: an
  unoriented metric must never compose a headline (fail-closed);
- the catalogue keeps it, with ``direction: None``, so the author is told the orientation is
  unknown;
- the units drop a performance metric declared with two different units, rather than format
  it in either.

A quality metric's family is the family whose sub-package declares it (``FAMILY_MODULES``),
read from the module the walk found it in, never inferred from its key.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType, ModuleType
from typing import Final, Literal

from app.services.evaluation.metric_package import families
from app.services.evaluation.metric_package.availability import guard_package_call
from app.services.evaluation.metric_package.traversal import walk_metric_metadata_by_module

logger = logging.getLogger(__name__)

HIGHER_IS_BETTER = "higher_is_better"
LOWER_IS_BETTER = "lower_is_better"
VALID_DIRECTIONS = frozenset({HIGHER_IS_BETTER, LOWER_IS_BETTER})

# Quality metrics the package computes and the platform hides: no view offers them, and
# `quality_adapter.run_quality` drops their results, so they are never stored. The
# two phrase metrics need phrase lists no evaluation can configure: offered, they
# would be `skipped` on every result, noise to every author. REMOVE THEM ONCE AN
# EVALUATION CAN CONFIGURE PHRASE LISTS.
HIDDEN_QUALITY_KEYS: Final[frozenset[str]] = frozenset(
    {"prose.required_phrase_coverage", "prose.forbidden_phrase_absence"}
)

# Platform family id -> the quality sub-package holding that family's metrics. Keyed on the
# id constants, never retyped literals: a lowercase spelling once missed every uppercase id
# and left the catalogue empty, silently. Every family in ``families.FAMILY_IDS`` can be
# scored; ``tests/test_evaluation_metric_catalog.py`` pins the two together.
#
# A family is listed only once the adapter can compute it, so no metric is advertised that
# no run can populate. PROSE is computable because the adapter supplies the required
# ``ProseConfig.policy`` (``quality_adapter.PROSE_NORMALIZATION_POLICY``).
FAMILY_MODULES: Final[dict[str, str]] = {
    families.STIX_BUNDLE: "stix_bundle",
    families.STRUCTURED_JSON: "structured_json",
    families.CLASSIFICATION: "classification",
    families.PROSE: "prose",
}

_FAMILY_PACKAGES = "app.evaluation_metrics.quality.families."

Half = Literal["quality", "performance"]


@dataclass(frozen=True)
class MetricDefinition:
    """One metric as the package declares it."""

    key: str
    label: str
    half: Half
    #: The direction every declaration of the metric gives, valid or not; ``None`` when they
    #: disagree or give none.
    direction: str | None
    #: The declared unit of a performance metric; ``None`` for a quality one, or when two
    #: declarations disagree.
    unit: str | None
    #: How the run combines the metric's entry values (``mean``, ``pooled`` or ``macro``), as
    #: every declaration gives it; ``None`` when they disagree.
    rollup: str | None
    #: The families whose sub-package declares this quality metric; empty for a performance
    #: metric and for a quality metric outside every family sub-package.
    families: frozenset[str]
    #: Whether one entry's value means anything on its own; ``True`` unless every declaration
    #: of the metric says otherwise.
    shown_on_entry: bool = True
    #: The metric's name on a single entry, when the metric declares one.
    entry_label: str | None = None


@dataclass(frozen=True)
class Registry:
    """The walk's result and the views built from it, computed once."""

    definitions: tuple[MetricDefinition, ...]
    quality_directions: Mapping[str, str]
    performance_directions: Mapping[str, str]
    performance_units: Mapping[str, str]
    #: The keys of the metrics whose run value pools their entries' counts.
    pooled_keys: frozenset[str]
    #: The keys of the metrics whose run value is the macro F1 of their entries' per-class
    #: counts, summed over the run.
    macro_keys: frozenset[str]
    #: ``{key: (shown_on_entry, entry_label)}`` for the quality metrics that read differently
    #: on a single entry; a key absent here is shown under its own name.
    entry_views: Mapping[str, tuple[bool, str | None]] = MappingProxyType({})


def _family_of(module_name: str) -> str | None:
    if not module_name.startswith(_FAMILY_PACKAGES):
        return None
    package = module_name.removeprefix(_FAMILY_PACKAGES).split(".", 1)[0]
    return next((fid for fid, pkg in FAMILY_MODULES.items() if pkg == package), None)


def _raw_direction(candidate: object) -> str | None:
    raw = getattr(candidate, "direction", None)
    raw = getattr(raw, "value", raw)
    return raw if isinstance(raw, str) else None


def build_registry(quality_root: ModuleType, performance_root: ModuleType) -> Registry:
    """Walk both roots once and build every view. Pure: no cache, no lock.

    A metric is identified by its half and its key, so a quality and a performance metric
    can never overwrite each other. When a metric is declared more than once, the rules
    do not depend on the walk order:

    - its direction is the one every declaration gives; declarations that disagree leave
      it with none, so it never composes a headline (fail-closed) and the catalogue offers
      it with a null direction;
    - a performance metric's unit is the one every declaration gives; declarations that
      disagree leave it with none, since formatting it in either could be wrong by a
      factor of a thousand;
    - how it reads on one entry is the one every declaration gives; declarations that
      disagree leave it shown under its own name;
    - its label is the last declaration's, and its families are every family whose
      sub-package declares it.
    """
    from app.evaluation_metrics.performance.results import PerformanceMetadata
    from app.evaluation_metrics.quality.results import MetricMetadata

    labels: dict[tuple[Half, str], str] = {}
    entry_views_seen: dict[tuple[Half, str], set[tuple[bool, str | None]]] = {}
    directions_seen: dict[tuple[Half, str], set[str | None]] = {}
    rollups_seen: dict[tuple[Half, str], set[str | None]] = {}
    units_seen: dict[str, set[str]] = {}
    family_sets: dict[str, set[str]] = {}

    def declare(half: Half, candidate: object) -> tuple[Half, str]:
        ident = (half, candidate.key)  # type: ignore[attr-defined]
        labels[ident] = candidate.label  # type: ignore[attr-defined]
        # Only quality metadata declares how it reads on one entry; anything else is shown.
        entry_views_seen.setdefault(ident, set()).add(
            (getattr(candidate, "shown_on_entry", True), getattr(candidate, "entry_label", None))
        )
        directions_seen.setdefault(ident, set()).add(_raw_direction(candidate))
        raw_rollup = getattr(candidate, "rollup", None)
        rollups_seen.setdefault(ident, set()).add(getattr(raw_rollup, "value", raw_rollup))
        return ident

    for module_name, candidate in walk_metric_metadata_by_module(quality_root, MetricMetadata):
        if candidate.key in HIDDEN_QUALITY_KEYS:
            continue
        declare("quality", candidate)
        family = _family_of(module_name)
        if family is not None:
            family_sets.setdefault(candidate.key, set()).add(family)

    for _module, candidate in walk_metric_metadata_by_module(performance_root, PerformanceMetadata):
        declare("performance", candidate)
        unit = str(getattr(candidate.unit, "value", candidate.unit))
        units_seen.setdefault(candidate.key, set()).add(unit)

    for (half, key), seen in sorted(directions_seen.items()):
        if len(seen) > 1:
            logger.warning(
                "evaluation_metrics declares the %s metric %s with conflicting directions %s; it composes no headline",
                half,
                key,
                sorted(str(d) for d in seen),
            )
    for (half, key), seen in sorted(rollups_seen.items()):
        if len(seen) > 1:
            logger.warning(
                "evaluation_metrics declares the %s metric %s with conflicting roll-ups %s; "
                "it composes no headline",
                half,
                key,
                sorted(str(r) for r in seen),
            )
    for key, seen in sorted(units_seen.items()):
        if len(seen) > 1:
            logger.warning(
                "evaluation_metrics declares %s with more than one unit; it is shown unformatted",
                key,
            )

    for (half, key), seen in sorted(entry_views_seen.items()):
        if len(seen) > 1:
            logger.warning(
                "evaluation_metrics declares the %s metric %s with conflicting entry views %s; "
                "it is shown on an entry under its own name",
                half,
                key,
                sorted(seen, key=str),
            )

    def single(values: set) -> str | None:
        return next(iter(values)) if len(values) == 1 else None

    def entry_view(ident: tuple[Half, str]) -> tuple[bool, str | None]:
        # Declarations that disagree fall back to the default, never to the walk order.
        seen = entry_views_seen[ident]
        return next(iter(seen)) if len(seen) == 1 else (True, None)

    definitions = tuple(
        MetricDefinition(
            key=key,
            label=labels[(half, key)],
            half=half,
            direction=single(directions_seen[(half, key)]),
            rollup=single(rollups_seen[(half, key)]),
            unit=single(units_seen.get(key, set())) if half == "performance" else None,
            families=frozenset(family_sets.get(key, ())) if half == "quality" else frozenset(),
            shown_on_entry=entry_view((half, key))[0],
            entry_label=entry_view((half, key))[1],
        )
        for half, key in sorted(labels, key=lambda ident: (ident[1], ident[0]))
    )

    def oriented(half: str) -> dict[str, str]:
        # A metric whose declarations disagree on how the run combines it cannot be combined
        # honestly either way, so it composes no headline (fail-closed).
        return {
            d.key: d.direction
            for d in definitions
            if d.half == half and d.direction in VALID_DIRECTIONS and d.rollup is not None
        }

    result = Registry(
        definitions=definitions,
        quality_directions=MappingProxyType(oriented("quality")),
        performance_directions=MappingProxyType(oriented("performance")),
        performance_units=MappingProxyType(
            {d.key: d.unit for d in definitions if d.half == "performance" and d.unit is not None}
        ),
        pooled_keys=frozenset(d.key for d in definitions if d.rollup == "pooled"),
        macro_keys=frozenset(d.key for d in definitions if d.rollup == "macro"),
        entry_views=MappingProxyType(
            {
                d.key: (d.shown_on_entry, d.entry_label)
                for d in definitions
                if d.half == "quality" and (not d.shown_on_entry or d.entry_label is not None)
            }
        ),
    )
    for half, view in (
        ("quality", result.quality_directions),
        ("performance", result.performance_directions),
    ):
        if not view:
            # Downstream, an empty registry looks like "every metric lacks a direction",
            # which fail-closed turns into an N/A headline everywhere. Say so loudly
            # rather than let it read as "this evaluation had no data".
            logger.warning(
                "evaluation_metrics exposed no %s metric directions; the %s headline will be "
                "not-applicable for every run",
                half,
                half,
            )
    return result


def _collect() -> Registry:
    from app.evaluation_metrics import quality
    from app.evaluation_metrics.performance import metrics as performance_metrics

    return build_registry(quality, performance_metrics)


_cache: Registry | None = None

# Guards every read and write of `_cache`: one `to_thread` upstream is enough to race the
# synchronous accessors. It covers the populate AND the return, since a `reset_cache()`
# between the two would hand back `None`. Not `functools.cache`: it does not serialise
# misses, so every concurrent first caller would walk. A plain `Lock` is enough: the
# populate only walks the package, which never imports platform code, so it cannot re-enter.
_cache_lock = threading.Lock()


def registry() -> Registry:
    """The registry, walked on first use and cached for the process.

    Raises ``MetricPackageUnavailableError`` (through ``guard_package_call``) when the
    package cannot be imported or no longer provides what the walk reads. One walk feeds
    every view, so such a failure reaches all of them: the directions, the catalogue (both
    halves) and the units.
    """
    global _cache
    with _cache_lock:
        if _cache is None:
            _cache = guard_package_call("reading the metric package's metadata", _collect)
        return _cache


def reset_cache() -> None:
    """Drop the cached registry, for tests that swap the package surface.

    Takes the populate lock so a reset cannot land inside a populate.
    """
    global _cache
    with _cache_lock:
        _cache = None


__all__ = [
    "FAMILY_MODULES",
    "HIDDEN_QUALITY_KEYS",
    "HIGHER_IS_BETTER",
    "LOWER_IS_BETTER",
    "VALID_DIRECTIONS",
    "Half",
    "MetricDefinition",
    "Registry",
    "build_registry",
    "registry",
    "reset_cache",
]
