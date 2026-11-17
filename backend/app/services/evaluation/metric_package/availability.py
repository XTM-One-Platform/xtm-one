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

"""Fail-closed guard around the ``evaluation_metrics`` package seam.

The Overall Quality / Overall Performance headlines come entirely from metrics the
package produces. If the package is absent, or a name this adapter calls no longer
exists, the only safe outcome is to stop, never to fall back to a partial or
improvised metric set.

An improvised headline still renders as a 0-100 number on the run row, the leader
card and the trend chart, and nothing on those surfaces can falsify it. An error
naming the package is better than a plausible wrong number.

Detection is by observed surface, not by declared version: the declared version is
written by hand, so it can agree while the call surface has drifted. This
module watches for what actually breaks: ``ImportError`` (the package or one of its
modules is gone) and ``AttributeError`` (a called name is gone). The declared
version is recorded on each run for lineage (``metric_suite_version``), never used
to decide availability.

It lives inside the adapter package, the sole importer of ``evaluation_metrics``
(``tests/test_evaluation_guards.py``), so knowledge of the dependency does not
leak into the engine, the API or the serializers.
"""

from __future__ import annotations

from collections.abc import Callable

# Used only to compose the operator message: nothing imports through this
# constant, so it cannot become a second import site.
_PACKAGE_NAME = "app.evaluation_metrics"


class MetricPackageUnavailableError(RuntimeError):
    """The metric package is missing, or no longer offers a name the adapter calls.

    Raised instead of returning a partial metric set. Callers must let it propagate:
    the run fails to score, and no headline is produced.
    """


class MetadataNestedTooDeeplyError(RecursionError):
    """Metric metadata was nested past the traversal bound and could not be collected.

    Defined here rather than in ``traversal.py`` so ``guard_package_call`` can convert
    it without a lazy import: a package-native failure must never escape the seam raw.
    """


def _message(detail: str) -> str:
    return (
        f"The {_PACKAGE_NAME} package is unavailable or incompatible: {detail}. "
        f"Quality and performance headlines cannot be computed and no partial or "
        f"improvised metric set will be substituted. Verify the vendored suite in "
        f"backend/app/evaluation_metrics/ matches the adapter in "
        f"app/services/evaluation/metric_package/."
    )


def guard_package_call[T](operation: str, call: Callable[[], T]) -> T:
    """Run ``call``, converting a package absence/drift into a fail-closed error.

    ``operation`` names what was being attempted, so the operator-facing message
    points at the specific seam call rather than at the package in general.
    """
    try:
        return call()
    except ImportError as exc:
        raise MetricPackageUnavailableError(
            _message(f"{operation} could not import from the package ({exc})")
        ) from exc
    except AttributeError as exc:
        raise MetricPackageUnavailableError(
            _message(f"{operation} called a name the package no longer provides ({exc})")
        ) from exc
    except MetadataNestedTooDeeplyError as exc:
        # A catalogue that cannot be collected completely is an unusable package,
        # so the boundary keeps a single failure type.
        raise MetricPackageUnavailableError(
            _message(f"{operation} could not collect the package's metadata ({exc})")
        ) from exc


__all__ = [
    "MetadataNestedTooDeeplyError",
    "MetricPackageUnavailableError",
    "guard_package_call",
]


def metric_suite_version() -> str | None:
    """Return the vendored suite's declared version, for score lineage.

    Recorded on each ``EvaluationRun`` so a score change is attributable: a metric
    whose definition moved and an agent that regressed otherwise look the same.

    Not fail-closed: a missing version is a gap in provenance, not a fabricated
    score, so it must never fail a run that scored correctly. ``None`` means
    "unknown"; the column is nullable for the same reason.
    """
    try:
        from app.evaluation_metrics import __version__
    except ImportError:  # pragma: no cover - the suite is vendored, so absent only if deleted
        return None

    return __version__ or None
