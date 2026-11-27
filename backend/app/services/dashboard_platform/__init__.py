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

"""Platform widgets: OpenCTI, OpenAEV and OpenCRQ widgets inside an XTM One dashboard.

A ``platform`` widget (``custom_dashboard_spec.PlatformWidget``) keeps the
platform's own configuration (``native``) and is drawn from data the platform
returns, read with the viewer's own access. This package is the one place that
knows what a native configuration may hold, per source:

* :mod:`.opencti` - an OpenCTI dashboard widget (``type``, ``perspective``,
  ``dataSelection`` with FilterGroups, ``parameters``), as OpenCTI exports it;
* :mod:`.openaev` - an OpenAEV custom dashboard widget (``widget_type`` and its
  polymorphic ``widget_config``);
* :mod:`.opencrq` - a metric of OpenCRQ's dashboard aggregates (OpenCRQ has no
  custom dashboards of its own, so there is nothing native to keep: a widget
  names one metric of a fixed catalog).

Every check is pure (no I/O) and strict: a native configuration is stored,
sent back to the platform when the dashboard is published, and turned into a
query when it is drawn, so whatever it holds is validated here first. What the
platform answers is just as untrusted, and normalized by the live data layer.

A checked configuration says which ``shape`` of data it produces (``number``,
``series``, ``distribution``, ``rows``, ``geo``...), or ``None`` for a type this platform
draws and XTM One does not (an OpenCTI heatmap, an OpenAEV attack path): such a
widget is kept, for packs and publishing, and drawn as a link to the platform.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Literal

PlatformSource = Literal["opencti", "openaev", "opencrq"]
PLATFORM_SOURCES: tuple[str, ...] = ("opencti", "openaev", "opencrq")

#: The platforms' names, as they spell them.
PLATFORM_NAMES: dict[str, str] = {"opencti": "OpenCTI", "openaev": "OpenAEV", "opencrq": "OpenCRQ"}

#: Where a dashboard is published: the platforms with custom dashboards of their
#: own. OpenCRQ has none (its dashboards are pages of its app).
PUBLISH_TARGETS: tuple[str, ...] = ("opencti", "openaev")

#: What a platform widget's ``native`` configuration is, per source.
NATIVE_FORMS: dict[str, str] = {
    "opencti": "an OpenCTI dashboard widget {type, perspective, dataSelection, parameters}",
    "openaev": "an OpenAEV custom dashboard widget {widget_type, widget_config}",
    "opencrq": "an OpenCRQ metric {metric, organization_id?, window_days?}",
}

#: What a widget pack is imported from.
IMPORT_FORMS = "an XTM One pack, an OpenCTI dashboard or widget export, or an OpenAEV dashboard export (ZIP)"

#: What publishing sends, and where it cannot go.
PUBLISH_NOTE = (
    "Only the widgets that platform can draw are sent (its own widgets; OpenCTI also takes text); the answer lists "
    "the rest. OpenCRQ has no custom dashboards."
)


def native_forms() -> list[str]:
    """What ``native`` is for each source, in :data:`PLATFORM_SOURCES` order (OpenCRQ's metrics named)."""
    from .opencrq import METRICS

    return [
        NATIVE_FORMS[source] + (f" with metric one of {', '.join(METRICS)}" if source == "opencrq" else "")
        for source in PLATFORM_SOURCES
    ]


def platform_list(sources: tuple[str, ...] | list[str] = PLATFORM_SOURCES, *, last: str = "or") -> str:
    """The platforms' names in prose: "OpenCTI, OpenAEV or OpenCRQ"."""
    names = [PLATFORM_NAMES.get(s, s) for s in sources]
    return names[0] if len(names) == 1 else f"{', '.join(names[:-1])} {last} {names[-1]}"


#: How a platform widget is drawn. The views a widget may take follow the shape
#: of its data (:data:`SHAPE_VIEWS`).
PLATFORM_VIEWS: tuple[str, ...] = (
    "number", "list", "distribution", "bar", "hbar", "line", "area", "donut", "table",
    "scatter", "bubble", "punchcard", "icon", "radial", "filler", "marker",
    "map", "choroplethsvg", "linkgraph", "networkgraph", "sankey", "parallel", "timeline", "treemap", "attackflow",
    "radar", "polar", "wordcloud", "heatmap",
    "outcomematrix", "pathgraph", "exposureconsole", "scoregauge", "ringgauge", "domaincards",
)  # fmt: skip

DataShape = Literal["number", "series", "distribution", "rows", "geo", "graph", "events", "tree", "text", "outcomes"]

#: The views each shape can be drawn with; a table's columns feed a graph
#: (source and target columns), a timeline (a date column) or parallel axes.
SHAPE_VIEWS: dict[str, tuple[str, ...]] = {
    "number": ("number", "icon", "radial", "filler", "marker"),
    "series": ("line", "area", "bar", "scatter", "heatmap", "radar"),
    "distribution": ("distribution", "donut", "hbar", "bar", "treemap", "radar", "polar", "wordcloud"),
    "rows": ("list", "table", "scatter", "bubble", "punchcard", "timeline", "linkgraph", "networkgraph", "sankey", "parallel"),
    "geo": ("map", "choroplethsvg"),
    "graph": ("networkgraph", "linkgraph", "sankey", "attackflow"),
    "events": ("timeline",),
    "tree": ("treemap",),
    # Validation outcomes: a technique matrix, a path graph, an exposure console, a score gauge, a ring gauge, domain cards.
    "outcomes": ("outcomematrix", "pathgraph", "exposureconsole", "scoregauge", "ringgauge", "domaincards"),
}

#: A stored native configuration, as canonical JSON, may not exceed this.
MAX_NATIVE_BYTES = 16 * 1024

#: A FilterGroup may nest this deep and hold this many filters in all.
MAX_FILTER_DEPTH = 4
MAX_FILTERS = 30


class NativeError(ValueError):
    """A native configuration that cannot be kept, with a reason an agent can act on."""


@dataclass(frozen=True, slots=True)
class NativeCheck:
    """A validated native configuration and what it produces."""

    #: The configuration as stored (unknown or unsafe parts refused, defaults filled).
    native: dict
    #: The data it produces, or ``None`` when XTM One does not draw this type.
    shape: DataShape | None
    #: The platform's own name for its type ("donut", "vertical-barchart", "findings_trend").
    native_type: str
    #: The title the platform gives it, if any.
    title: str | None = None
    #: How the platform itself draws it, when XTM One has that view (a donut is drawn as a donut).
    preferred_view: str | None = None
    #: The parts the platform does not read, each with what to write instead: kept as written (a widget made in the
    #: platform comes back as it was), told to whoever writes the widget.
    ignored: tuple[str, ...] = ()


def canonical_size(native: object) -> int:
    """The size of *native* as canonical JSON, in bytes."""
    return len(json.dumps(native, separators=(",", ":"), sort_keys=True, ensure_ascii=False).encode("utf-8"))


def check_native(source: str, native: object) -> NativeCheck:
    """Validate *native* for *source* (``opencti`` / ``openaev`` / ``opencrq``)."""
    if source == "opencti":
        from . import opencti

        check = opencti.check_native(native)
    elif source == "openaev":
        from . import openaev

        check = openaev.check_native(native)
    elif source == "opencrq":
        from . import opencrq

        check = opencrq.check_native(native)
    else:
        raise NativeError(f"source must be one of: {', '.join(PLATFORM_SOURCES)}.")
    if canonical_size(check.native) > MAX_NATIVE_BYTES:
        raise NativeError(f"The {source} configuration is too large (more than {MAX_NATIVE_BYTES // 1024} KB).")
    return check


def describe_widget(source: str, widget_type: str | None = None) -> dict:
    """What *source*'s widgets are (without *widget_type*), or one type with an example configuration that
    passes :func:`check_native` and the rules it follows: what an agent reads before writing one."""
    if source not in PLATFORM_SOURCES:
        raise NativeError(f"source must be one of: {', '.join(PLATFORM_SOURCES)}.")
    from importlib import import_module

    return {"source": source, **import_module(f"{__name__}.{source}").describe(widget_type)}


def resolve_view(check: NativeCheck, view: str | None) -> str | None:
    """The view a widget is drawn with: *view* when it suits the data, else the shape's default.

    ``None`` for a type XTM One does not draw. A view that does not suit the
    data is an error naming the ones that do.
    """
    if check.shape is None:
        return None
    allowed = SHAPE_VIEWS[check.shape]
    if view is None:
        return check.preferred_view if check.preferred_view in allowed else allowed[0]
    if view not in allowed:
        raise NativeError(
            f"view '{view}' cannot draw {check.shape} data from a {check.native_type} widget; "
            f"use one of: {', '.join(allowed)}."
        )
    return view
