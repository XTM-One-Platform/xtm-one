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

"""One walk over the vendored suite's declared metric metadata.

``registry.py`` builds the directions, the catalogue and the units from every metadata
constant the package declares. This module is its mechanical half: it finds the
constants and the module each is bound in, and leaves every rule to the registry, since
the views disagree on purpose (the catalogue keeps a metric with no direction, the
directions drop it).

Most metadata constants are module-private (``_META``), so scanning module namespaces is
the only way to see them all.
"""

from __future__ import annotations

import importlib
import logging
import pkgutil
from collections.abc import Callable, Iterable, Iterator, Mapping
from types import ModuleType
from typing import Any

from app.services.evaluation.metric_package.availability import (
    MetadataNestedTooDeeplyError,
)

logger = logging.getLogger(__name__)

__all__ = ["MetadataNestedTooDeeplyError", "walk_metric_metadata_by_module"]

# Metadata is grouped a level or two deep at most. This is NOT the cycle guard (`_seen`
# in `_flatten` is): it bounds the case id()-marking cannot, a `Mapping` whose `values()`
# fabricates a fresh container per call, where every id is new and the walk would recurse
# until the stack blows.
#
# Breaching it raises rather than truncating: an incomplete catalogue must fail closed,
# not be served as complete. `guard_package_call` converts the raise to
# `MetricPackageUnavailableError` at the seam.
_MAX_NESTING = 8


def _flatten(
    value: Any,
    metadata_type: type,
    _depth: int = 0,
    _seen: dict[int, Any] | None = None,
) -> Iterator[Any]:
    """Every *metadata_type* instance reachable inside *value*.

    Metadata is declared both as bare constants and inside containers
    (``METRIC_METADATA``, ``_DEPENDENT_METAS``, ...); a metric declared only inside a
    container would otherwise vanish from the catalogue silently.

    Recursive over every ordinary collection, not ``tuple`` alone, since nothing keeps
    authors to flat tuples. ``str``/``bytes`` are excluded because they iterate into
    characters; mappings are walked by value so a ``{key: metadata}`` registry is seen.

    Containers already visited are skipped, so a self-referential structure terminates.
    Deduplication is by container, not by metric: a constant bound in two places is
    yielded twice, which is why callers key their result on ``candidate.key``.
    """
    if isinstance(value, metadata_type):
        yield value
        return
    if isinstance(value, str | bytes):
        return
    if isinstance(value, Mapping):
        members: Iterable[Any] = value.values()
    elif isinstance(value, tuple | list | set | frozenset):
        members = value
    else:
        return
    # Checked before the depth bound: a benign structure that loops back at depth 8
    # must take this lossless exit, not the raise. A fabricating Mapping yields a new
    # object per call, so its id is never in `seen` and it still raises.
    seen = {} if _seen is None else _seen
    if id(value) in seen:
        return
    # Only containers reach the bound: a scalar at that depth loses nothing.
    if _depth >= _MAX_NESTING:
        raise MetadataNestedTooDeeplyError(
            f"metric metadata is nested more than {_MAX_NESTING} levels deep; "
            "collecting it would be incomplete, so the walk fails rather than "
            "silently returning a partial catalogue"
        )
    # Holds a strong reference: `id()` is unique only among live objects, so a freed
    # container's address could be reused by a distinct one and skip a real subtree.
    seen[id(value)] = value
    for member in members:
        yield from _flatten(member, metadata_type, _depth + 1, seen)


def _declared_in(namespace: Any, metadata_type: type) -> Iterator[Any]:
    """Every *metadata_type* instance bound in *namespace*, containers unpacked."""
    for value in getattr(namespace, "__dict__", {}).values():
        yield from _flatten(value, metadata_type)


def walk_metric_metadata_by_module(
    root: ModuleType,
    metadata_type: type,
    *,
    _walk: Callable[..., Iterable[Any]] = pkgutil.walk_packages,
    _import: Callable[[str], ModuleType] = importlib.import_module,
) -> Iterator[tuple[str, Any]]:
    """Yield ``(module name, candidate)`` for every *metadata_type* constant under *root*.

    The module is where the constant is bound, which is how the registry learns a quality
    metric's family from the sub-package that declares it rather than from its key.
    A constant bound in two modules is yielded once per module.

    The root's own namespace is yielded first, then each sub-module in
    ``pkgutil.walk_packages`` order, so keying a dict on ``candidate.key`` resolves
    last-one-wins.

    ``_walk`` and ``_import`` are a test seam, not part of the contract: they let a test
    drive the skip path without patching ``importlib.import_module``, which would break
    every lazy import in the interpreter meanwhile.
    """
    for candidate in _declared_in(root, metadata_type):
        yield root.__name__, candidate
    for module in _walk(root.__path__, f"{root.__name__}."):
        try:
            imported = _import(module.name)
        except ImportError as exc:
            logger.info("skipping unimportable metric module %s: %s", module.name, exc)
            continue
        for candidate in _declared_in(imported, metadata_type):
            yield module.name, candidate
