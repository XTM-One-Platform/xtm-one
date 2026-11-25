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

"""Live data of platform widgets: ask OpenCTI, OpenAEV or OpenCRQ, as the viewer.

A ``platform`` widget (``custom_dashboard_spec.PlatformWidget``) keeps a native
configuration; drawing it means turning that configuration into the platform's
own aggregate query, sending it through the viewer's access to the integration
(:mod:`app.services.platform_access`), and normalizing the answer into one of
the closed shapes the dashboard draws:

* ``number``       - ``{"value": float, "change": float | None}``
* ``series``       - ``{"series": [{"name", "points": [{"x", "y"}]}]}``
* ``distribution`` - ``{"items": [{"label", "value"}]}``
* ``rows``         - ``{"columns": [{"key", "label"}], "rows": [{...}], "total": int | None}``

and four drawn by the maps, graphs and flows (static data, or a platform
answer that has their form - an OpenCTI map is ``geo``):

* ``geo``          - ``{"points": [{"label", "lat", "lon", "value", "group"}], "regions": [{"code", "label", "value"}]}``
* ``graph``        - ``{"nodes": [{"id", "label", "group", "value"}], "edges": [{"source", "target", "label", "value"}]}``
* ``events``       - ``{"events": [{"label", "start", "end", "lane", "value"}]}``
* ``tree``         - ``{"items": [{"path": [str, ...], "value"}]}``

and one drawn by the validation views (an OpenAEV security posture widget):

* ``outcomes``     - ``{"items": [{"label", "success", "failed", "pending", "parts": [{"label", "success", "failed", "pending"}], "group"?, "id"?, "links"?}]}``
  and, optionally (:func:`outcomes_data`): an item's ``code``, ``icon``, ``color``, ``key``,
  ``link``, ``successLabel``, ``phases`` and ``subIds``, a part's ``icon``, ``key``, ``total``
  and status ``rows``; the data's ``chains`` (named sequences of groups), ``reference`` (every
  label the widget lists, outcome or not), ``timeline``, ``actions``, ``orbit`` and ``needsScope``

The shapes carry, optionally, what a module's own widget draws besides its
values (``dev-docs/dashboard-rendering-reference.md``), in the platform's
vocabulary, never a module's: a distribution item, a series or an event its
``color`` (strict hex or a named colour, :func:`color`), its ``entityType``
(the entity vocabulary the platform shares) and its ``link``; a series point
or a distribution item its ``key`` (what a click filters on); a number its ``previous`` value, the
``period`` it is compared over and an ``icon`` (a Lucide name, with a named
``color``); a rows column its cell ``kind`` (:data:`CELL_KINDS`), a row its
``_entityType``, ``_icon`` and ``_color``; an item or an event its ``icon``; an
event its ``secondary`` text; a region its ``level`` (0-9). A field that does not validate is left out, never passed.

Everything the platform answers is untrusted: numbers are checked finite,
strings are cut to :data:`MAX_STRING` characters, lists to :data:`MAX_POINTS`
points, :data:`MAX_ITEMS` items and :data:`MAX_ROWS` rows, and links are built
here from the integration's base URL and an id, never taken from the answer.
A failure is one of the fixed :data:`ERROR_CODES`; what the platform said goes
to the logs only.

The query never comes from the browser: the data route reads the widget from
the stored dashboard, the preview route from a body checked like any write.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

#: The answers a widget can get; the frontend has a sentence for each.
ERROR_CODES: tuple[str, ...] = (
    "integration_unavailable",  # not visible, wrong provider, no usable credential
    "access_denied",  # the platform refused this person (401/403)
    "timeout",
    "upstream_error",  # the platform failed or answered something unreadable
    "unsupported",  # a type XTM One does not draw (drawn as a link)
    "rate_limited",
    "tenant_required",  # the account reaches several tenants and the integration names none
)

#: The time ranges a viewer may pick, in days (``None``: the widget's own): ``Nd`` counts days, ``Nm`` and ``Ny``
#: calendar months and years - the platforms' own "last 3 months" (an adapter reads the key when its platform
#: counts in months, the days otherwise).
RANGES: dict[str, int] = {"1d": 1, "7d": 7, "30d": 30, "1m": 30, "90d": 90, "3m": 91, "6m": 182, "1y": 365}
#: Every date, the widget's own period set aside (OpenCTI's no period, OpenAEV's ``ALL_TIME``).
ALL_TIME = "all"
#: A fixed period, as an OpenCTI dashboard's start and end dates: ``YYYY-MM-DD/YYYY-MM-DD``, either end left out.
PERIOD_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})?/(\d{4}-\d{2}-\d{2})?$")


def fixed_period(value: object) -> tuple[datetime | None, datetime | None] | None:
    """The start and end (UTC midnights) of a fixed period ``YYYY-MM-DD/YYYY-MM-DD``, or ``None`` when it is not one."""
    match = PERIOD_RE.fullmatch(value) if isinstance(value, str) else None
    if match is None or not (match.group(1) or match.group(2)):
        return None
    try:
        start, end = (datetime.fromisoformat(d).replace(tzinfo=UTC) if d else None for d in match.groups())
    except ValueError:
        return None
    if start and end and end < start:
        return None
    return start, end


def is_range(value: object) -> bool:
    """Whether *value* is a period a platform widget can be read over: a key of :data:`RANGES`, :data:`ALL_TIME`
    or a fixed period."""
    return value in RANGES or value == ALL_TIME or fixed_period(value) is not None

MAX_POINTS = 60
MAX_ITEMS = 50
MAX_ROWS = 100
#: OpenCTI's default entities list has ten columns.
MAX_COLUMNS = 12
MAX_SERIES = 5
MAX_STRING = 200
MAX_TEXT = 20_000
MAX_GEO_POINTS = 300
#: ISO 3166-1 has 249 codes; an uploaded SVG names its own regions.
MAX_REGIONS = 300
#: Each shape's form, as an agent writes it (a static source's data, the JSON an assignment answers).
SHAPE_FORMS: dict[str, str] = {
    "number": '{"value": n, "change"?: n}',
    "series": '{"series": [{"name", "points": [{"x", "y"}]}]}',
    "distribution": '{"items": [{"label", "value"}]}',
    "rows": '{"columns": [{"key", "label"}], "rows": [{<key>: value}]}',
    "geo": '{"points": [{"label", "lat", "lon", "value"}], "regions": [{"code", "label", "value"}]}',
    "graph": (
        '{"nodes": [{"id", "label", "group"?, "value"?, "kind"?: start|action|condition|operator|asset, "technique"?, '
        '"tactic"?, "description"?, "operator"?: AND|OR}], "edges": [{"source", "target", "label"?}]}; a STIX bundle '
        "holding a MITRE Attack Flow (attack-flow, attack-action...) is read as one; rows of techniques become one "
        "through a derived source's step {op: flow, stage, label, technique?}: each row an action in the stage(s) its "
        "stage cell names (chips ordered by their rank), stage after stage - a platform list of techniques with their "
        "tactics, read live, drawn as a flow"
    ),
    "events": '{"events": [{"label", "start", "end"?, "lane"?}]}',
    "tree": '{"items": [{"path": [str], "value"}]}',
    "text": '{"text": str}',
}
MAX_GRAPH_NODES = 300
#: A node of a flow (an Attack Flow, the stages of a kill chain) is one of these kinds; its card shows
#: these details, each cut to this many characters.
GRAPH_NODE_KINDS = ("start", "action", "condition", "operator", "asset")
_GRAPH_NODE_DETAILS = {"technique": 20, "tactic": 60, "description": 400}
MAX_GRAPH_EDGES = 500
MAX_EVENTS = 300
MAX_TREE_ITEMS = 300
MAX_TREE_DEPTH = 3
#: Validated labels (a technique, a security domain, an expectation type) and their parts;
#: a technique grid by tactic lists a kill chain's techniques, some under several tactics.
MAX_OUTCOMES = 600
MAX_OUTCOME_PARTS = 12
#: The labels one outcome leads to (an attack path's next techniques).
MAX_OUTCOME_LINKS = 30
#: Every label a widget lists, outcome or not (a kill chain's techniques, the security domains).
MAX_OUTCOME_REFERENCE = 1000
#: The ids rolled up into one label (a technique's sub-techniques), and the groups it sits under.
MAX_OUTCOME_SUB_IDS = 60
MAX_OUTCOME_PHASES = 20
#: Named sequences of groups (kill chains and their phases).
MAX_OUTCOME_CHAINS = 10
MAX_OUTCOME_GROUPS = 40
#: A part's status rows, the links offered beside a console, what orbits its score.
MAX_OUTCOME_ROWS = 8
MAX_OUTCOME_ACTIONS = 6
MAX_OUTCOME_ORBIT = 8
#: What a count or a status row is.
OUTCOME_STATUSES: tuple[str, ...] = ("success", "failed", "pending")

CACHE_SECONDS = 60
#: Data reads per person per minute, and at once.
READS_PER_MINUTE = 120
CONCURRENT_READS = 4
#: How long a read waits for one of its person's places, and how many may wait
#: or read at once: a page of widgets asks for all of them together.
QUEUE_SECONDS = 10.0
MAX_QUEUED = 32


class LiveError(Exception):
    """A widget that cannot be drawn now; ``code`` is one of :data:`ERROR_CODES`."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code if code in ERROR_CODES else "upstream_error"


@dataclass(frozen=True, slots=True)
class LiveRequest:
    """What to draw: the stored (or previewed) widget's source, integration and native configuration."""

    source: Literal["opencti", "openaev", "opencrq"]
    integration_id: uuid.UUID
    native: dict
    shape: str | None
    range_days: int | None = None
    #: The period's key (``3m``), for an adapter whose platform counts in calendar months.
    range_key: str | None = None
    #: A fixed period's start and end (``fixed_period``); ``range_days`` is then its length.
    range_start: datetime | None = None
    range_end: datetime | None = None
    #: A published link's highest markings (``TLP:AMBER``...): what carries a marking above them is left out.
    marking_ceiling: tuple[str, ...] = ()


# ── Normalizing helpers (shared by the per-source modules) ───────────────────


def number(value: object) -> float | None:
    """A finite number from an answer, or ``None``."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float) and math.isfinite(value):
        return float(value)
    if isinstance(value, str):
        try:
            parsed = float(value)
        except ValueError:
            return None
        return parsed if math.isfinite(parsed) else None
    return None


def text(value: object, limit: int = MAX_STRING) -> str:
    """A display string from an answer: cut, without control characters."""
    if value is None:
        return ""
    # A whole number read as a float (JSON, a closed number) is written as the number it is: 131072, not 131072.0.
    if isinstance(value, float) and value.is_integer() and abs(value) < 1e15:
        value = int(value)
    raw = value if isinstance(value, str) else str(value)
    cleaned = "".join(ch if ch.isprintable() else " " for ch in raw[: limit * 2])
    return cleaned.strip()[:limit]


def link(base_url: str, path: str) -> str | None:
    """A link into the platform, from its base URL and a path built here (ids quoted)."""
    if not base_url.startswith(("https://", "http://")):
        return None
    return f"{base_url}{path}"


_HEX_COLOR = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")
#: An entity type, as the platform names them (``Intrusion-Set``, ``attack-pattern``, ``stix-sighting-relationship``).
_ENTITY_TYPE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,59}$")
MAX_URL = 2000
#: An event's second line, as a timeline cuts it.
MAX_SECONDARY = 150
#: The chips of one cell (labels, markings, tags).
MAX_CHIPS = 10
#: How a rows column draws its cells: ``text`` and ``number`` as written, ``date`` / ``datetime``
#: an ISO moment, ``entityType`` an entity type, ``chip`` one ``{"label", "color"?, "icon"?}``,
#: ``chips`` a list of them.
CELL_KINDS: tuple[str, ...] = ("text", "number", "date", "datetime", "entityType", "chip", "chips")
#: What a number's ``previous`` value is: a day before, or the interval before the widget's.
PERIODS: tuple[str, ...] = ("day", "interval")
#: What a number counts, when its module says it beside the change (a noun the reader's language writes).
NUMBER_SUBJECTS: tuple[str, ...] = ("endpoint", "expectation-inject", "finding", "inject", "scenario", "simulation", "vulnerable-endpoint")
#: Over what span a number's change is measured: the last day, week, month, 3 or 6 months, year, all time, a range.
NUMBER_SPANS: tuple[str, ...] = ("1d", "7d", "1m", "3m", "6m", "1y", "all", "custom")


def color(value: object) -> str | None:
    """A colour an answer gives an item: a strict ``#rgb`` / ``#rrggbb``, or a named colour of the engine."""
    from app.services.dashboard_visualization_options import NAMED_COLORS

    if not isinstance(value, str):
        return None
    raw = value.strip()
    return raw if raw in NAMED_COLORS or _HEX_COLOR.match(raw) else None


def entity_type(value: object) -> str | None:
    return value if isinstance(value, str) and _ENTITY_TYPE.match(value) else None


def safe_link(value: object) -> str | None:
    """A link built by an adapter (:func:`link`) or a path of this platform; nothing else is kept."""
    if not isinstance(value, str) or len(value) > MAX_URL or any(ch.isspace() or not ch.isprintable() for ch in value):
        return None
    if value.startswith(("https://", "http://")) or (value.startswith("/") and not value.startswith("//")):
        return value
    return None


def icon(value: object) -> str | None:
    from app.services.presentation_icons import normalize_icon_name

    return normalize_icon_name(value) if isinstance(value, str) and len(value) <= 60 else None


def _optional(fields: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in fields.items() if value is not None}


def extras(raw: object) -> dict[str, Any]:
    """The optional fields of an item, point, series or event (``color``, ``entityType``, ``link``,
    ``key``, ``secondary``, ``icon``), each validated; a field that does not validate is left out."""
    if not isinstance(raw, dict):
        return {}
    return _optional(
        {
            "color": color(raw.get("color")),
            "entityType": entity_type(raw.get("entityType")),
            "link": safe_link(raw.get("link")),
            "key": text(raw.get("key"), 100) or None if raw.get("key") is not None else None,
            "secondary": text(raw.get("secondary"), MAX_SECONDARY) or None if raw.get("secondary") is not None else None,
            "icon": icon(raw.get("icon")),
        }
    )


def number_data(
    value: object,
    change: object = None,
    *,
    previous: object = None,
    period: object = None,
    icon_name: object = None,
    icon_color: object = None,
    subject: object = None,
    span: object = None,
) -> dict:
    """A value, and optionally its ``change``, the ``previous`` value it is compared with and over which
    ``period`` (:data:`PERIODS`), an ``icon`` (a Lucide name and a named colour), what it counts
    (:data:`NUMBER_SUBJECTS`) and over what ``span`` its change is measured (:data:`NUMBER_SPANS`)."""
    parsed = number(value)
    if parsed is None:
        raise LiveError("upstream_error")
    name = icon(icon_name)
    return {
        "value": parsed,
        "change": number(change),
        **_optional(
            {
                "previous": number(previous),
                "period": period if period in PERIODS else None,
                "icon": _optional({"name": name, "color": color(icon_color)}) if name else None,
                "subject": subject if subject in NUMBER_SUBJECTS else None,
                "span": span if span in NUMBER_SPANS else None,
            }
        ),
    }


_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}")


def series_data(
    series: list[tuple], *, aggregate: Literal["sum", "mean"] = "sum"
) -> dict:
    """At most :data:`MAX_POINTS` points per series over the WHOLE range, on one axis for all series.

    Past that many points, consecutive buckets of the shared axis are merged:
    summed for counts per interval (the default: OpenCTI, OpenAEV, findings
    trends), averaged for levels (``mean``: an annual loss expectancy, a loss
    event frequency) - summing a level would multiply it. Every series is cut on
    the same buckets, each labelled with its first one, so they stay aligned.
    Dropping the oldest points instead would draw a year as its last two months.

    A series is ``(name, points[, extras])`` (:func:`extras`: its ``color``) and
    a point ``(x, y[, key])``. A point keeps its key (what a click filters on)
    only while it is drawn alone: a bucket of several points has none, since
    no one key filters on all of them.
    """
    kept = [
        (s[0], [(text(p[0], 40), number(p[1]), p[2] if len(p) > 2 else None) for p in s[1]], extras(s[2]) if len(s) > 2 else {})
        for s in series[:MAX_SERIES]
    ]
    axis = list(dict.fromkeys(x for _, points, _ in kept for x, _, _ in points))
    if axis and all(_ISO_DATE.match(x) for x in axis):
        axis.sort()
    size = max(1, -(-len(axis) // MAX_POINTS))
    bucket_of = {x: index // size for index, x in enumerate(axis)}
    labels = axis[::size]
    out = []
    for name, points, extra in kept:
        totals: dict[int, float] = {}
        counts: dict[int, int] = {}
        keys: dict[int, str] = {}
        for x, y, key in points:
            if y is None:
                continue
            bucket = bucket_of[x]
            totals[bucket] = totals.get(bucket, 0.0) + y
            counts[bucket] = counts.get(bucket, 0) + 1
            if key is not None and size == 1:
                keys[bucket] = text(key, 100)
        clean = [
            {"x": labels[b], "y": totals[b] if aggregate == "sum" else totals[b] / counts[b], **({"key": keys[b]} if keys.get(b) else {})}
            for b in sorted(totals)
        ]
        color_of = {"color": extra["color"]} if "color" in extra else {}
        out.append({"name": text(name, 80) or f"Series {len(out) + 1}", "points": clean, **color_of})
    return {"series": out}


def distribution_data(items: list[tuple]) -> dict:
    """Values by label, each ``(label, value[, extras])`` (:func:`extras`: ``color``, ``entityType``, ``link``, ``icon``,
    and the ``key`` a click filters on)."""
    return {
        "items": [
            {
                "label": text(item[0], 120) or "-",
                "value": number(item[1]) or 0.0,
                **{k: v for k, v in (extras(item[2]) if len(item) > 2 else {}).items() if k in ("color", "entityType", "link", "key", "icon")},
            }
            for item in items[:MAX_ITEMS]
        ]
    }


def _chip(value: object) -> dict | None:
    if isinstance(value, str):
        value = {"label": value}
    if not isinstance(value, dict):
        return None
    label = text(value.get("label"), 80)
    # ``rank``: where the chip stands in its ``group`` (a kill chain phase's order in its kill chain), for a step
    # that orders by it.
    rank = value.get("rank") if isinstance(value.get("rank"), int) and not isinstance(value.get("rank"), bool) else None
    fields = {"label": label, "color": color(value.get("color")), "icon": icon(value.get("icon")), "rank": rank, "group": text(value.get("group"), 60) or None}
    return _optional(fields) if label else None


def cell(kind: str, value: object) -> object:
    """A cell drawn as *kind* (:data:`CELL_KINDS`); a value that is not one is ``None`` (an empty cell)."""
    if kind == "number":
        return number(value)
    if kind in ("date", "datetime"):
        return moment(value)
    if kind == "entityType":
        return entity_type(value)
    if kind == "chip":
        return _chip(value)
    if kind == "chips":
        chips = [_chip(v) for v in value[: MAX_CHIPS * 2]] if isinstance(value, list) else []
        return [c for c in chips if c][:MAX_CHIPS]
    return text(value)


def rows_data(columns: list[tuple[str, ...]], rows: list[dict], total: object = None) -> dict:
    """Rows of cells by column, each column ``(key, label[, kind])`` (:data:`CELL_KINDS`, ``text`` by default).

    A row may carry ``_url`` (a link built by the adapter), ``_entityType``, ``_icon`` (a Lucide name)
    and ``_color`` (what the row's entity is drawn in: its icon, its dot).
    """
    cols = [(c[0], c[1], c[2] if len(c) > 2 and c[2] in CELL_KINDS else "text") for c in columns[:MAX_COLUMNS]]
    clean_rows = []
    for row in rows[:MAX_ROWS]:
        item: dict[str, Any] = {key: cell(kind, row.get(key)) for key, _, kind in cols}
        url = safe_link(row.get("_url"))
        if url is not None:
            item["_url"] = url
        item.update(
            _optional({"_entityType": entity_type(row.get("_entityType")), "_icon": icon(row.get("_icon")), "_color": color(row.get("_color"))})
        )
        clean_rows.append(item)
    parsed_total = number(total)
    return {
        "columns": [{"key": key, "label": text(label, 60), **({"kind": kind} if kind != "text" else {})} for key, label, kind in cols],
        "rows": clean_rows,
        "total": int(parsed_total) if parsed_total is not None else None,
    }


def text_data(value: object) -> dict:
    """An answer written in words (an agent's, a variable's): cut, without control characters but new lines."""
    raw = value if isinstance(value, str) else ("" if value is None else str(value))
    lines = [text(line, MAX_TEXT) for line in raw[: MAX_TEXT * 2].splitlines()]
    return {"text": "\n".join(lines).strip()[:MAX_TEXT]}


def _maybe_number(value: object) -> float | None:
    return None if value is None or value == "" else number(value)


def geo_data(points: list[tuple[object, ...]], regions: list[tuple[object, ...]]) -> dict:
    """Points on the world (``label, lat, lon, value, group``) and regions by code (``code, label, value[, level]``).

    A point off the globe is dropped; a region without a value or a code is
    too, and a code named twice keeps its first value. A region's code is an
    ISO 3166-1 alpha-2 code on the bundled atlas, or an element id of an
    uploaded SVG. A region's ``level`` (an integer 0-9) ranks it among the others,
    as a threat map colours it.
    """
    clean_points = []
    for label, lat, lon, value, group in points:
        if len(clean_points) >= MAX_GEO_POINTS:
            break
        y, x = number(lat), number(lon)
        if y is None or x is None or not -90 <= y <= 90 or not -180 <= x <= 180:
            continue
        clean_points.append(
            {"label": text(label, 120), "lat": y, "lon": x, "value": _maybe_number(value), "group": text(group, 60) or None}
        )
    clean_regions: dict[str, dict] = {}
    for code, label, value, *rest in regions:
        key = text(code, 60)
        parsed = _maybe_number(value)
        if not key or parsed is None or key in clean_regions:
            continue
        if len(clean_regions) >= MAX_REGIONS:
            break
        level = number(rest[0]) if rest else None
        clean_regions[key] = {
            "code": key,
            "label": text(label, 120) or key,
            "value": parsed,
            **({"level": int(level)} if level is not None and level == int(level) and 0 <= level <= 9 else {}),
        }
    return {"points": clean_points, "regions": list(clean_regions.values())}


def _node_details(extra: object) -> dict:
    """A flow node's kind and card details (:data:`GRAPH_NODE_KINDS`, :data:`_GRAPH_NODE_DETAILS`), the rest dropped."""
    if not isinstance(extra, dict):
        return {}
    out: dict[str, object] = {key: text(extra.get(key), size) for key, size in _GRAPH_NODE_DETAILS.items()}
    out["kind"] = extra.get("kind") if extra.get("kind") in GRAPH_NODE_KINDS else None
    operator = str(extra.get("operator") or "").upper()
    out["operator"] = operator if operator in ("AND", "OR") else None
    return {key: value for key, value in out.items() if value}


def graph_data(nodes: list[tuple[object, ...]], edges: list[tuple[object, ...]]) -> dict:
    """Nodes (``id, label, group, value[, details]``) and the edges between them (``source, target, label, value``).

    A node named once is kept once; an edge's end that names no node becomes
    one (labelled by its id) while there is room, else the edge is dropped. A
    node of a flow also says its ``kind`` and its card's details (:func:`_node_details`).
    """
    clean_nodes: dict[str, dict] = {}

    def add(node_id: object, label: object = None, group: object = None, value: object = None, extra: object = None) -> str | None:
        key = text(node_id, 120)
        if not key:
            return None
        if key not in clean_nodes:
            if len(clean_nodes) >= MAX_GRAPH_NODES:
                return None
            clean_nodes[key] = {
                "id": key,
                "label": text(label, 120) or key,
                "group": text(group, 60) or None,
                "value": _maybe_number(value),
                **_node_details(extra),
            }
        return key

    for node in nodes:
        add(*node[:5])
    clean_edges = []
    for source, target, label, value in edges:
        if len(clean_edges) >= MAX_GRAPH_EDGES:
            break
        start, end = add(source), add(target)
        if start is None or end is None:
            continue
        clean_edges.append({"source": start, "target": end, "label": text(label, 80) or None, "value": _maybe_number(value)})
    return {"nodes": list(clean_nodes.values()), "edges": clean_edges}


def moment(value: object) -> str | None:
    """An ISO 8601 date (kept a date) or date-time (UTC, to the second), or ``None``."""
    if not isinstance(value, str):
        return None
    raw = value.strip()[:40]
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
        try:
            return datetime.strptime(raw, "%Y-%m-%d").strftime("%Y-%m-%d")
        except ValueError:
            return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    parsed = parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
    return parsed.strftime("%Y-%m-%dT%H:%M:%SZ")


def events_data(events: list[tuple[object, ...]]) -> dict:
    """Events in time (``label, start, end, lane, value[, extras]``), by start.

    An event without a readable start is dropped; an end before its start is.
    Its extras (:func:`extras`) are its ``secondary`` text, ``entityType``, ``color``, ``icon`` and ``link``.
    """
    clean = []
    for label, start, end, lane, value, *rest in events:
        if len(clean) >= MAX_EVENTS:
            break
        begin = moment(start)
        if begin is None:
            continue
        finish = moment(end)
        clean.append(
            {
                "label": text(label, 120) or "-",
                "start": begin,
                "end": finish if finish is not None and finish >= begin else None,
                "lane": text(lane, 60) or None,
                "value": _maybe_number(value),
                **{k: v for k, v in (extras(rest[0]) if rest else {}).items() if k != "key"},
            }
        )
    clean.sort(key=lambda event: event["start"])
    return {"events": clean}


def tree_data(items: list[tuple[object, object]]) -> dict:
    """Values on a hierarchy of at most :data:`MAX_TREE_DEPTH` levels (``path, value``).

    A path named twice is summed; a value below zero, or none, is dropped.
    """
    totals: dict[tuple[str, ...], float] = {}
    for path, value in items:
        parts = path if isinstance(path, list) else [path]
        key = tuple(part for part in (text(p, 120) for p in parts[:MAX_TREE_DEPTH]) if part)
        parsed = number(value)
        if not key or parsed is None or parsed < 0:
            continue
        if key not in totals and len(totals) >= MAX_TREE_ITEMS:
            continue
        totals[key] = totals.get(key, 0.0) + parsed
    return {"items": [{"path": list(key), "value": value} for key, value in totals.items()]}


def _count(value: object) -> float:
    parsed = number(value)
    return parsed if parsed is not None and parsed > 0 else 0.0


def _ids(values: object, limit: int) -> list[str]:
    """Distinct ids from an answer, cut, at most *limit*."""
    raw = values if isinstance(values, list | tuple) else []
    return list(dict.fromkeys(t for t in (text(x, 100) for x in raw[: limit * 2]) if t))[:limit]


def _outcome_extras(raw: object) -> dict[str, Any]:
    """An outcome's optional fields, each validated: a short ``code`` (a technique's external id),
    an ``icon`` (a Lucide name), a ``color``, the ``key`` a click filters on, a ``link`` built by
    the adapter, what a success is called (``successLabel``: "prevented"), the ids of the groups it
    sits under (``phases``) and the ids rolled up into it (``subIds``)."""
    if not isinstance(raw, dict):
        return {}
    return _optional(
        {
            "code": text(raw.get("code"), 40) or None,
            "icon": icon(raw.get("icon")),
            "color": color(raw.get("color")),
            "key": text(raw.get("key"), 100) or None,
            "link": safe_link(raw.get("link")),
            "successLabel": text(raw.get("successLabel"), 40) or None,
            "phases": _ids(raw.get("phases"), MAX_OUTCOME_PHASES) or None,
            "subIds": _ids(raw.get("subIds"), MAX_OUTCOME_SUB_IDS) or None,
        }
    )


def _status_rows(raw: object) -> list[dict]:
    """A part's rows by status (``label, value, status, key``), as the part's answer lists them."""
    rows: list[dict] = []
    for row in (raw if isinstance(raw, list | tuple) else [])[: MAX_OUTCOME_ROWS * 2]:
        if not isinstance(row, dict) or row.get("status") not in OUTCOME_STATUSES:
            continue
        rows.append(
            _optional(
                {
                    "label": text(row.get("label"), 80) or "-",
                    "value": _count(row.get("value")),
                    "status": row["status"],
                    "key": text(row.get("key"), 100) or None,
                }
            )
        )
        if len(rows) >= MAX_OUTCOME_ROWS:
            break
    return rows


def _part(part: object) -> dict | None:
    """A part (``label, success, failed, pending[, extras]``); its extras are its ``icon``, ``key``,
    the ``total`` its rows are shares of and its status ``rows``."""
    if not isinstance(part, tuple | list) or len(part) not in (4, 5):
        return None
    name, ok, ko, wait, *rest = part
    extra = rest[0] if rest and isinstance(rest[0], dict) else {}
    total = _maybe_number(extra.get("total"))
    return {
        "label": text(name, 80) or "-",
        "success": _count(ok),
        "failed": _count(ko),
        "pending": _count(wait),
        **_optional(
            {
                "icon": icon(extra.get("icon")),
                "key": text(extra.get("key"), 100) or None,
                "total": total if total is not None and total >= 0 else None,
                "rows": _status_rows(extra.get("rows")) or None,
            }
        ),
    }


def _chains(raw: object) -> list[dict]:
    """Named sequences of groups (``id, label, default?, groups: [{id, label}]``), each id once."""
    chains: list[dict] = []
    seen: set[str] = set()
    for chain in (raw if isinstance(raw, list | tuple) else [])[: MAX_OUTCOME_CHAINS * 2]:
        cid = text(chain.get("id"), 100) if isinstance(chain, dict) else ""
        if not cid or cid in seen:
            continue
        seen.add(cid)
        groups: list[dict] = []
        known: set[str] = set()
        for group in (chain.get("groups") if isinstance(chain.get("groups"), list) else [])[: MAX_OUTCOME_GROUPS * 2]:
            gid = text(group.get("id"), 100) if isinstance(group, dict) else ""
            if not gid or gid in known:
                continue
            known.add(gid)
            groups.append({"id": gid, "label": text(group.get("label"), 120) or "-"})
            if len(groups) >= MAX_OUTCOME_GROUPS:
                break
        chains.append(
            {"id": cid, "label": text(chain.get("label"), 120), **({"default": True} if chain.get("default") is True else {}), "groups": groups}
        )
        if len(chains) >= MAX_OUTCOME_CHAINS:
            break
    return chains


def _reference(raw: object) -> list[dict]:
    """Every label a widget lists, outcome or not (``id, label`` and an outcome's extras, and the
    ``parts`` - ``label, icon`` - drawn while it has none), each id once."""
    entries: list[dict] = []
    seen: set[str] = set()
    for entry in (raw if isinstance(raw, list | tuple) else [])[: MAX_OUTCOME_REFERENCE * 2]:
        rid = text(entry.get("id"), 100) if isinstance(entry, dict) else ""
        if not rid or rid in seen:
            continue
        seen.add(rid)
        extra = {k: v for k, v in _outcome_extras(entry).items() if k in ("code", "icon", "phases", "subIds")}
        parts = [
            _optional({"label": text(p.get("label"), 80) or "-", "icon": icon(p.get("icon"))})
            for p in (entry.get("parts") if isinstance(entry.get("parts"), list) else [])[:MAX_OUTCOME_PARTS]
            if isinstance(p, dict)
        ]
        entries.append({"id": rid, "label": text(entry.get("label"), 160) or "-", **extra, **({"parts": parts} if parts else {})})
        if len(entries) >= MAX_OUTCOME_REFERENCE:
            break
    return entries


def _timeline(raw: object) -> dict | None:
    """When the outcomes ran (``start``, ``end``: ISO moments); an end before its start is dropped."""
    if not isinstance(raw, dict):
        return None
    start, end = moment(raw.get("start")), moment(raw.get("end"))
    if start is not None and end is not None and end < start:
        end = None
    return {"start": start, "end": end} if start is not None or end is not None else None


def _actions(raw: object) -> list[dict]:
    """The links offered beside the outcomes (``label, caption?, icon?, color?, link``); one without a link is dropped."""
    actions: list[dict] = []
    for action in (raw if isinstance(raw, list | tuple) else [])[: MAX_OUTCOME_ACTIONS * 2]:
        target = safe_link(action.get("link")) if isinstance(action, dict) else None
        if target is None:
            continue
        actions.append(
            {
                "label": text(action.get("label"), 80) or "-",
                **_optional({"caption": text(action.get("caption"), 120) or None, "icon": icon(action.get("icon")), "color": color(action.get("color"))}),
                "link": target,
            }
        )
        if len(actions) >= MAX_OUTCOME_ACTIONS:
            break
    return actions


def _orbit(raw: object) -> list[dict]:
    """What circles a score (``label, type?, color?``): the tools that answer the checks."""
    orbit: list[dict] = []
    for member in (raw if isinstance(raw, list | tuple) else [])[: MAX_OUTCOME_ORBIT * 2]:
        if not isinstance(member, dict):
            continue
        label = text(member.get("label"), 80)
        if not label:
            continue
        orbit.append({"label": label, **_optional({"type": text(member.get("type"), 40) or None, "color": color(member.get("color"))})})
        if len(orbit) >= MAX_OUTCOME_ORBIT:
            break
    return orbit


def outcomes_data(
    items: list[tuple],
    *,
    chains: object = None,
    reference: object = None,
    timeline: object = None,
    actions: object = None,
    orbit: object = None,
    needs_scope: bool = False,
) -> dict:
    """Validation outcomes by label (``label, success, failed, pending, parts[, group[, id, links[, extras]]]``).

    Each part is ``label, success, failed, pending[, extras]`` (a security domain's
    expectation types, :func:`_part`); ``group`` places the label under a heading (a
    technique under its tactic), in the order groups come; ``id`` names the
    outcome and ``links`` the ids of those it leads to (an attack path's next
    techniques), at most :data:`MAX_OUTCOME_LINKS`; ``extras`` are its optional
    fields (:func:`_outcome_extras`). A count that is not a positive number is
    zero; a label named twice in a group (an id, when it has one) is summed; past
    :data:`MAX_OUTCOMES` labels (or :data:`MAX_OUTCOME_PARTS` parts) the rest
    is dropped.

    Besides the items, all optional: ``chains`` (:func:`_chains`), ``reference``
    (:func:`_reference`), ``timeline`` (:func:`_timeline`), ``actions``
    (:func:`_actions`), ``orbit`` (:func:`_orbit`), and ``needs_scope`` when the
    widget cannot be drawn until it is told which run it reads (an attack path
    without its simulation).
    """
    merged: dict[tuple[str, str], dict] = {}
    for label, success, failed, pending, parts, *rest in items:
        group = text(rest[0], 120) if rest and rest[0] else ""
        outcome_id = text(rest[1], 100) if len(rest) > 1 and rest[1] else ""
        links = rest[2] if len(rest) > 2 and isinstance(rest[2], list | tuple) else []
        extra = _outcome_extras(rest[3]) if len(rest) > 3 else {}
        name = text(label, 160) or "-"
        key = (group, outcome_id or name)
        if key not in merged:
            if len(merged) >= MAX_OUTCOMES:
                continue
            merged[key] = {"label": name, "success": 0.0, "failed": 0.0, "pending": 0.0, "parts": [], **({"group": group} if group else {})}
            if outcome_id:
                merged[key]["id"] = outcome_id
                merged[key]["links"] = _ids(links, MAX_OUTCOME_LINKS)
            merged[key].update(extra)
        item = merged[key]
        item["success"] += _count(success)
        item["failed"] += _count(failed)
        item["pending"] += _count(pending)
        for raw_part in parts or []:
            if len(item["parts"]) >= MAX_OUTCOME_PARTS:
                break
            part = _part(raw_part)
            if part is not None:
                item["parts"].append(part)
    data: dict[str, Any] = {"items": list(merged.values())}
    for name, value in (
        ("chains", _chains(chains)),
        ("reference", _reference(reference)),
        ("timeline", _timeline(timeline)),
        ("actions", _actions(actions)),
        ("orbit", _orbit(orbit)),
    ):
        if value:
            data[name] = value
    if needs_scope:
        data["needsScope"] = True
    return data


def _records(data: dict, key: str) -> list[dict]:
    value = data.get(key)
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def closed_static_data(shape: str, data: dict, *, reclosing: bool = False) -> dict:
    """Data written in a dashboard (a ``static`` data source) in its closed shape.

    The same builders as an upstream answer: cut, coerced, never a raw value
    passed through. A link (a row's ``_url``, an item's or an event's ``link``)
    must stay inside the platform - unless the data is *reclosing*: already
    closed by a builder (a derived source's parent), whose links an adapter built.
    """
    from app.services.custom_dashboard_spec import check_internal_path

    from .attack_flow import flow_graph, is_flow_bundle

    def internal(record: dict, key: str = "link") -> dict:
        if record.get(key) is not None and not reclosing:
            check_internal_path(str(record[key]))
        return record

    if shape == "number":
        mark = data.get("icon") if isinstance(data.get("icon"), dict) else {}
        return number_data(
            data.get("value"), data.get("change"),
            previous=data.get("previous"), period=data.get("period"), icon_name=mark.get("name"), icon_color=mark.get("color"),
        )  # fmt: skip
    if shape == "text":
        return text_data(data.get("text"))
    if shape == "series":
        series = data.get("series") if isinstance(data.get("series"), list) else []
        return series_data(
            [
                (
                    s.get("name"),
                    [(p.get("x"), p.get("y"), p.get("key")) for p in s.get("points") or [] if isinstance(p, dict)],
                    s,
                )
                for s in series
                if isinstance(s, dict)
            ]
        )
    if shape == "distribution":
        items = data.get("items") if isinstance(data.get("items"), list) else []
        return distribution_data([(i.get("label"), i.get("value"), internal(i)) for i in items if isinstance(i, dict)])
    if shape == "rows":
        columns = data.get("columns") if isinstance(data.get("columns"), list) else []
        rows = [internal(dict(r), "_url") for r in data.get("rows") or [] if isinstance(r, dict)]
        return rows_data(
            [
                (str(c.get("key")), str(c.get("label") or c.get("key")), str(c.get("kind") or "text"))
                for c in columns
                if isinstance(c, dict) and c.get("key")
            ],
            rows,
            data.get("total"),
        )
    if shape == "geo":
        return geo_data(
            [(p.get("label"), p.get("lat"), p.get("lon"), p.get("value"), p.get("group")) for p in _records(data, "points")],
            [(r.get("code"), r.get("label"), r.get("value"), r.get("level")) for r in _records(data, "regions")],
        )
    if shape == "graph":
        if is_flow_bundle(data):
            # An Attack Flow, as its builder exports it: read into the graph's nodes and edges.
            data = flow_graph(data)
        return graph_data(
            [(n.get("id"), n.get("label"), n.get("group"), n.get("value"), n) for n in _records(data, "nodes")],
            [(e.get("source"), e.get("target"), e.get("label"), e.get("value")) for e in _records(data, "edges")],
        )
    if shape == "events":
        return events_data(
            [(e.get("label"), e.get("start"), e.get("end"), e.get("lane"), e.get("value"), internal(e)) for e in _records(data, "events")]
        )
    if shape == "tree":
        return tree_data([(i.get("path", i.get("label")), i.get("value")) for i in _records(data, "items")])
    if shape == "outcomes":
        return outcomes_data(
            [
                (
                    i.get("label"), i.get("success"), i.get("failed"), i.get("pending"),
                    [(p.get("label"), p.get("success"), p.get("failed"), p.get("pending"), p) for p in _records(i, "parts")],
                    i.get("group"), i.get("id"), i.get("links") if isinstance(i.get("links"), list) else [], internal(i),
                )
                for i in _records(data, "items")
            ],
            chains=data.get("chains"), reference=data.get("reference"), timeline=data.get("timeline"),
            actions=[internal(a) for a in _records(data, "actions")], orbit=data.get("orbit"), needs_scope=data.get("needsScope") is True,
        )  # fmt: skip
    raise ValueError(f"unknown shape {shape!r}")


# ── Limits ───────────────────────────────────────────────────────────────────

_slots: dict[str, tuple[asyncio.Semaphore, int]] = {}


class _Slot:
    """At most :data:`CONCURRENT_READS` reads at once per person (this process).

    The others wait their turn, up to :data:`QUEUE_SECONDS` and
    :data:`MAX_QUEUED` of them, then answer ``rate_limited``.
    """

    def __init__(self, key: str) -> None:
        self.key = key

    def _leave(self) -> None:
        semaphore, users = _slots[self.key]
        if users <= 1:
            _slots.pop(self.key, None)
        else:
            _slots[self.key] = (semaphore, users - 1)

    async def __aenter__(self) -> None:
        semaphore, users = _slots.get(self.key) or (asyncio.Semaphore(CONCURRENT_READS), 0)
        if users >= MAX_QUEUED:
            raise LiveError("rate_limited")
        _slots[self.key] = (semaphore, users + 1)
        try:
            await asyncio.wait_for(semaphore.acquire(), QUEUE_SECONDS)
        except TimeoutError:
            self._leave()
            raise LiveError("rate_limited") from None
        except BaseException:
            self._leave()
            raise
        self.semaphore = semaphore

    async def __aexit__(self, *_exc: object) -> None:
        self.semaphore.release()
        self._leave()


async def _enforce_rate(user_id: uuid.UUID) -> None:
    from fastapi import HTTPException

    from app.core.rate_limit import enforce_rate_limit

    try:
        await enforce_rate_limit(
            None, "dashboard-widget-data", limit=READS_PER_MINUTE, window_seconds=60, bucket=str(user_id)
        )
    except HTTPException as exc:
        if exc.status_code == 429:
            raise LiveError("rate_limited") from None
        raise


# ── Cache ────────────────────────────────────────────────────────────────────


def cache_key(user_id: uuid.UUID, request: LiveRequest) -> str:
    digest = hashlib.sha256(
        json.dumps(
            {
                "source": request.source,
                "native": request.native,
                "range": request.range_days,
                "key": request.range_key,
                "period": [d.isoformat() if d else None for d in (request.range_start, request.range_end)],
                "ceiling": list(request.marking_ceiling),
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()[:32]
    return f"dashboard-live:{user_id}:{request.integration_id}:{digest}"


async def _cache_get(key: str) -> dict | None:
    try:
        from app.core.redis import redis_client

        raw = await (await redis_client()).get(key)
    except Exception:
        return None
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


async def _cache_set(key: str, value: dict, ttl: int = CACHE_SECONDS) -> None:
    try:
        from app.core.redis import redis_client

        await (await redis_client()).set(key, json.dumps(value, separators=(",", ":")), ex=ttl)
    except Exception:
        return


# ── Entry point ──────────────────────────────────────────────────────────────


def request_for(widget: dict, range_key: str | None = None, marking_ceiling: tuple[str, ...] = ()) -> LiveRequest:
    """The request drawing a stored (or validated) platform widget; under a published link's *marking_ceiling*."""
    from app.services.dashboard_platform import check_native

    check = check_native(widget["source"], widget["native"])
    start, end = fixed_period(range_key) or (None, None)
    days = RANGES.get(range_key) if range_key else None
    if start and end:
        days = max(1, (end - start).days)
    return LiveRequest(
        source=widget["source"],
        integration_id=uuid.UUID(str(widget["integration_id"])),
        native=check.native,
        shape=check.shape,
        range_days=days,
        range_key=range_key if range_key in RANGES or range_key == ALL_TIME else None,
        range_start=start,
        range_end=end,
        marking_ceiling=tuple(marking_ceiling),
    )



def presentation_of(source: str, native: dict) -> dict | None:
    """How the module draws this widget, as generic options: ``{"view", "options"}`` or ``None``.

    The source's adapter knows its widget types (``presentation(native)``); the
    options are any visualization's, checked by the view's own model, so a
    preset can only say what a person could set. The dashboard lays them under
    the visualization's own options when it is drawn with ``view``. A preset
    that does not validate is a bug of the adapter: logged, and left out.
    """
    from app.services.dashboard_visualization_options import OPTIONS_BY_TYPE, validate_options

    preset = getattr(_module(source), "presentation", None)
    try:
        value = preset(native) if preset is not None else None
        if value is None:
            return None
        view, options = value.get("view"), value.get("options")
        if view not in OPTIONS_BY_TYPE or not isinstance(options, dict):
            raise ValueError("not a view and its options")
        return {"view": view, "options": validate_options(view, options)}
    except (ValueError, TypeError, AttributeError, KeyError) as exc:
        logger.warning("Dashboard presentation of %s dropped: %r", source, str(exc)[:200])
        return None


async def read_widget_data(db: AsyncSession, user_id: uuid.UUID, request: LiveRequest, *, fresh: bool = False) -> dict:
    """The data of one platform widget as *user_id* sees it.

    ``{"status": "ok", "shape", "data", "fetched_at", "open_url", "presentation"}``
    or ``{"status": "error", "code", "open_url"}``. The access checks run on
    every call, before the cache is read, so a revoked integration or share
    stops the data at once. The presentation (:func:`presentation_of`) is not
    cached: it follows the code that draws the data.
    """
    from app.core.egress import egress_route
    from app.services.platform_access import PlatformAccessError, resolve_platform_client

    try:
        client = await resolve_platform_client(db, user_id, request.integration_id, request.source)
    except PlatformAccessError as exc:
        return {"status": "error", "code": exc.code, "open_url": None}
    module = _module(request.source)
    open_url = module.open_url(client, request.native)
    if request.shape is None:
        return {"status": "error", "code": "unsupported", "open_url": open_url}
    key = cache_key(user_id, request)
    # A Refresh reads the platform again; the answer it gets is kept for the next readers.
    cached = None if fresh else await _cache_get(key)
    if cached is not None:
        return {**cached, "open_url": open_url, "presentation": presentation_of(request.source, request.native)}
    try:
        await _enforce_rate(user_id)
        async with _Slot(str(user_id)):
            with egress_route(client.route):
                data = await asyncio.wait_for(module.fetch(client, request), timeout=20)
    except LiveError as exc:
        return {"status": "error", "code": exc.code, "open_url": open_url}
    except TimeoutError:
        return {"status": "error", "code": "timeout", "open_url": open_url}
    except Exception as exc:
        logger.warning("Dashboard live data failed (%s %s): %r", request.source, request.integration_id, str(exc)[:200])
        return {"status": "error", "code": "upstream_error", "open_url": open_url}
    result = {"status": "ok", "shape": request.shape, "data": data, "fetched_at": datetime.now(UTC).isoformat()}
    await _cache_set(key, result)
    return {**result, "open_url": open_url, "presentation": presentation_of(request.source, request.native)}


def _module(source: str):
    if source == "opencti":
        from . import opencti

        return opencti
    if source == "openaev":
        from . import openaev

        return openaev
    from . import opencrq

    return opencrq


__all__ = [
    "ERROR_CODES",
    "PERIOD_RE",
    "RANGES",
    "LiveError",
    "LiveRequest",
    "fixed_period",
    "is_range",
    "read_widget_data",
    "request_for",
]
