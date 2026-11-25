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

"""OpenCTI widgets: the GraphQL query OpenCTI's own dashboard sends, and its answer.

One query per widget, to ``{base_url}/graphql``, with the widget's FilterGroups
as variables (never spliced into the query text). Entities and relationships
perspectives map to OpenCTI's statistics fields, the ones its dashboard widgets
use (``opencti-front/src/private/components/workspaces/dashboards``):

==============  ==================================  ======================================
shape           entities                            relationships
==============  ==================================  ======================================
number          ``stixCoreObjectsNumber``           ``stixRelationshipsNumber``
distribution    ``stixCoreObjectsDistribution``     ``stixRelationshipsDistribution``
series          ``stixCoreObjectsMultiTimeSeries``  ``stixRelationshipsMultiTimeSeries``
rows            ``stixCoreObjects``                 ``stixRelationships``
geo             -                                   ``stixRelationshipsDistribution``
==============  ==================================  ======================================

A map (``geo``) is OpenCTI's own: the relationship distribution with the
countries and cities it counts, a country as a region by the ISO 3166-1
alpha-2 code among its aliases (what OpenCTI's flags read) and, like a city,
as a point where it has coordinates. A relationships list carries each
relationship's source and target, so it can be drawn as a graph.

A distribution and a list draw their first data selection; a series draws up
to five, one line each. A data selection's ``filters`` (and, for
relationships, ``dynamicFrom`` / ``dynamicTo``) are sent as they are stored,
without the ``id`` the OpenCTI front gives a filter, which the API does not
take.
"""

from __future__ import annotations

import copy
import logging
import math
import re
from datetime import UTC, datetime, timedelta
from urllib.parse import quote

from app.services.country_codes import ISO_3166_ALPHA2, ISO_3166_ALPHA3_TO_ALPHA2

from . import (
    MAX_COLUMNS,
    MAX_ITEMS,
    MAX_REGIONS,
    MAX_ROWS,
    LiveError,
    LiveRequest,
    distribution_data,
    geo_data,
    link,
    number,
    number_data,
    rows_data,
    series_data,
)
from ._http import request_json
from .opencti_colors import item_color
from .opencti_entities import entity_chip, entity_color, entity_icon, entity_label, marking_chip

logger = logging.getLogger(__name__)

#: OpenCTI's ``StixCoreObjectsOrdering`` / ``StixRelationshipsOrdering`` dates a list may sort on.
_ENTITY_ORDERS = frozenset(
    {"created", "modified", "created_at", "updated_at", "start_time", "stop_time", "published", "valid_from",
     "valid_until", "first_seen", "last_seen", "name", "entity_type"}
)  # fmt: skip
_RELATIONSHIP_ORDERS = frozenset(
    {"created", "modified", "created_at", "updated_at", "start_time", "stop_time", "entity_type", "relationship_type"}
)
_DEFAULT_SERIES_DAYS = 365
#: OpenCTI's top N when a widget's number was left empty (saved as null).
_DEFAULT_TOP = 10

_ENTITY_LABEL = """
      entity {
        ... on BasicObject { id entity_type }
        ... on BasicRelationship { id entity_type }
        ... on StixObject { representative { main } }
        ... on StixRelationship { representative { main } objectMarking { id } }
        ... on StixCoreObject { objectMarking { id } }
        ... on Label { color }
        ... on MarkingDefinition { x_opencti_color }
        ... on Creator { name }
        ... on Group { name }
        ... on Label { value color }
        ... on MarkingDefinition { definition x_opencti_color }
        ... on Status { template { name color } }
      }"""

_GEO_ENTITY = """
      entity {
        ... on BasicObject { id entity_type }
        ... on StixCoreObject { objectMarking { id } }
        ... on Country { name x_opencti_aliases latitude longitude }
        ... on City { name latitude longitude }
      }"""

_ENDS = """
        from { ... on BasicObject { id entity_type } ... on StixObject { representative { main } } ... on StixCoreObject { objectMarking { id } } }
        to { ... on BasicObject { id entity_type } ... on StixObject { representative { main } } ... on StixCoreObject { objectMarking { id } } }"""

#: What a list, a timeline and their cells read of an entity (``StixCoreObjectsList``, ``StixCoreObjectsTimeline``).
_ENTITY_NODE = """
      id entity_type created_at updated_at
      representative { main secondary }
      createdBy { ... on Identity { name } ... on StixCoreObject { objectMarking { id } } }
      creators { name }
      objectLabel { id value color }
      objectMarking { id definition x_opencti_color }
      ... on StixDomainObject { created modified status { template { name color } } workflowEnabled }
      ... on Report { published }
      ... on AttackPattern { x_mitre_id killChainPhases { kill_chain_name phase_name x_opencti_order } }"""

#: ... and of a relationship (``StixRelationshipsList``, ``StixRelationshipsTimeline``).
_RELATIONSHIP_NODE = """
      id entity_type relationship_type created_at updated_at created modified is_inferred
      representative { main secondary }
      createdBy { ... on Identity { name } ... on StixCoreObject { objectMarking { id } } }
      objectMarking { id definition x_opencti_color }
      ... on StixCoreRelationship { start_time stop_time objectLabel { id value color } }
      ... on StixSightingRelationship { first_seen last_seen }"""

QUERIES: dict[tuple[str, str], str] = {
    ("entities", "number"): """
query XtmOneWidgetNumber($filters: FilterGroup, $startDate: DateTime, $endDate: DateTime, $dateAttribute: String) {
  stixCoreObjectsNumber(filters: $filters, startDate: $startDate, endDate: $endDate, dateAttribute: $dateAttribute) {
    total
    count
  }
}""",
    ("relationships", "number"): """
query XtmOneWidgetNumber($filters: FilterGroup, $dynamicFrom: FilterGroup, $dynamicTo: FilterGroup,
                         $endDate: DateTime, $dateAttribute: String) {
  stixRelationshipsNumber(filters: $filters, dynamicFrom: $dynamicFrom, dynamicTo: $dynamicTo, endDate: $endDate,
                          dateAttribute: $dateAttribute) {
    total
    count
  }
}""",
    ("entities", "distribution"): """
query XtmOneWidgetDistribution($field: String!, $limit: Int, $filters: FilterGroup, $startDate: DateTime,
                               $endDate: DateTime, $dateAttribute: String) {
  stixCoreObjectsDistribution(field: $field, operation: count, limit: $limit, filters: $filters,
                              startDate: $startDate, endDate: $endDate, dateAttribute: $dateAttribute) {
    label
    value"""
    + _ENTITY_LABEL
    + """
  }
}""",
    ("relationships", "distribution"): """
query XtmOneWidgetDistribution($field: String!, $limit: Int, $isTo: Boolean, $filters: FilterGroup,
                               $dynamicFrom: FilterGroup, $dynamicTo: FilterGroup, $startDate: DateTime,
                               $endDate: DateTime, $dateAttribute: String) {
  stixRelationshipsDistribution(field: $field, operation: count, limit: $limit, isTo: $isTo, filters: $filters,
                                dynamicFrom: $dynamicFrom, dynamicTo: $dynamicTo, startDate: $startDate,
                                endDate: $endDate, dateAttribute: $dateAttribute) {
    label
    value"""
    + _ENTITY_LABEL
    + """
  }
}""",
    ("relationships", "geo"): """
query XtmOneWidgetMap($field: String!, $limit: Int, $isTo: Boolean, $filters: FilterGroup,
                      $dynamicFrom: FilterGroup, $dynamicTo: FilterGroup, $startDate: DateTime,
                      $endDate: DateTime, $dateAttribute: String) {
  stixRelationshipsDistribution(field: $field, operation: count, limit: $limit, isTo: $isTo, filters: $filters,
                                dynamicFrom: $dynamicFrom, dynamicTo: $dynamicTo, startDate: $startDate,
                                endDate: $endDate, dateAttribute: $dateAttribute) {
    label
    value"""
    + _GEO_ENTITY
    + """
  }
}""",
    ("entities", "series"): """
query XtmOneWidgetSeries($startDate: DateTime!, $endDate: DateTime, $interval: String!,
                         $timeSeriesParameters: [StixCoreObjectsTimeSeriesParameters]) {
  stixCoreObjectsMultiTimeSeries(startDate: $startDate, endDate: $endDate, interval: $interval,
                                 timeSeriesParameters: $timeSeriesParameters) {
    data { date value }
  }
}""",
    ("relationships", "series"): """
query XtmOneWidgetSeries($startDate: DateTime!, $endDate: DateTime, $interval: String!,
                         $timeSeriesParameters: [StixRelationshipsTimeSeriesParameters]) {
  stixRelationshipsMultiTimeSeries(operation: count, startDate: $startDate, endDate: $endDate, interval: $interval,
                                   timeSeriesParameters: $timeSeriesParameters) {
    data { date value }
  }
}""",
    ("entities", "rows"): """
query XtmOneWidgetList($first: Int, $orderBy: StixCoreObjectsOrdering, $orderMode: OrderingMode, $filters: FilterGroup) {
  stixCoreObjects(first: $first, orderBy: $orderBy, orderMode: $orderMode, filters: $filters) {
    pageInfo { globalCount }
    edges { node {"""
    + _ENTITY_NODE
    + """
    } }
  }
}""",
    ("relationships", "rows"): """
query XtmOneWidgetList($first: Int, $orderBy: StixRelationshipsOrdering, $orderMode: OrderingMode, $filters: FilterGroup,
                       $dynamicFrom: FilterGroup, $dynamicTo: FilterGroup) {
  stixRelationships(first: $first, orderBy: $orderBy, orderMode: $orderMode, filters: $filters,
                    dynamicFrom: $dynamicFrom, dynamicTo: $dynamicTo) {
    pageInfo { globalCount }
    edges { node {"""
    + _RELATIONSHIP_NODE
    + _ENDS
    + """
    } }
  }
}""",
}


#: The operators a filter is complete without values (``NO_VALUES_FILTER_OPERATORS``).
_NO_VALUES_OPERATORS = frozenset({"nil", "not_nil", "has_changed", "not_has_changed"})


def _pruned_values(item: dict) -> list:
    """A filter's values, the filter groups a ``dynamic`` value of ``regardingOf`` holds pruned too."""
    values = item.get("values") or []
    return [
        {**value, "values": [_without_ids(g) for g in value.get("values") or []]}
        if isinstance(value, dict) and value.get("key") == "dynamic"
        else value
        for value in values
    ]


def _without_ids(group: object) -> object:
    """A FilterGroup as OpenCTI's widgets send it (``removeIdAndIncorrectKeysFromFilterGroupObject``).

    Without the front's filter ``id``s, the filters with no value (but for the
    operators that need none) and the groups left empty: a filter a person left
    blank in OpenCTI is not sent, and the API refuses some of them
    (``regardingOf`` without a value). The root group is kept, even empty.
    """
    if not isinstance(group, dict):
        return group
    out = copy.deepcopy(group)
    kept = []
    for item in out.get("filters") or []:
        if not isinstance(item, dict):
            continue
        item.pop("id", None)
        item["values"] = _pruned_values(item)
        if item["values"] or item.get("operator") in _NO_VALUES_OPERATORS:
            kept.append(item)
    out["filters"] = kept
    groups = [_without_ids(g) for g in out.get("filterGroups") or []]
    out["filterGroups"] = [g for g in groups if isinstance(g, dict) and (g.get("filters") or g.get("filterGroups"))]
    return out


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")


#: A bookmark widget: the bookmarks of the person OpenCTI answers for, filtered by entity type only.
BOOKMARKS = """
query XtmOneWidgetBookmarks($first: Int, $orderBy: StixDomainObjectsOrdering, $orderMode: OrderingMode, $filters: FilterGroup) {
  bookmarks(first: $first, orderBy: $orderBy, orderMode: $orderMode, filters: $filters) {
    pageInfo { globalCount }
    edges { node { id entity_type created_at modified representative { main } } }
  }
}"""
_BOOKMARK_ORDERS = ("created_at", "created", "modified", "updated_at", "name", "entity_type")


def _months_back(moment: datetime, months: int) -> datetime:
    """*moment* so many calendar months earlier (the day kept, or the month's last), as moment's ``subtract``."""
    month = moment.month - 1 - months
    year, month = moment.year + month // 12, month % 12 + 1
    last = (datetime(year + month // 12, month % 12 + 1, 1, tzinfo=moment.tzinfo) - timedelta(days=1)).day
    return moment.replace(year=year, month=month, day=min(moment.day, last))


def period(
    range_days: int | None,
    *,
    now: datetime,
    range_key: str | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
) -> tuple[datetime | None, datetime | None]:
    """The start and end OpenCTI reads a dashboard's period over (``computeStartEndDates``).

    A fixed period as it is; a relative one from the start of today back so many
    days, calendar months or years (``daysAgo`` / ``monthsAgo`` / ``yearsAgo``),
    up to now; none, without a period.
    """
    if start or end:
        return start, end
    if not range_days:
        return None, None
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    count = int(range_key[:-1]) if range_key and range_key[:-1].isdigit() else None
    if range_key and range_key.endswith("m") and count:
        return _months_back(today, count), now
    if range_key and range_key.endswith("y") and count:
        return _months_back(today, 12 * count), now
    return today - timedelta(days=range_days), now


def _dated(group: object, attribute: str, start: datetime | None, end: datetime | None) -> object:
    """A selection's filters with the period as filters on its date (``buildFiltersAndOptionsForWidgets``)."""
    filters = _without_ids(group)
    dates = [
        {"key": [attribute], "values": [_iso(moment)], "operator": operator, "mode": "or"}
        for moment, operator in ((start, "gt"), (end, "lt"))
        if moment is not None
    ]
    if not dates:
        return filters
    kept = isinstance(filters, dict) and (filters.get("filters") or filters.get("filterGroups"))
    return {"mode": "and", "filters": dates, "filterGroups": [filters] if kept else []}


def build_query(
    native: dict,
    shape: str,
    *,
    range_days: int | None,
    now: datetime | None = None,
    range_key: str | None = None,
    range_start: datetime | None = None,
    range_end: datetime | None = None,
) -> tuple[str, dict]:
    """The GraphQL query and variables drawing *native* as *shape*, over the dashboard's period when it has one."""
    if native.get("type") == "bookmark" and shape == "rows":
        # As OpenCTI reads them: no date range, its own order (ascending unless the widget says).
        first = native["dataSelection"][0]
        order = first.get("sort_by") or "created_at"
        return BOOKMARKS, {
            "first": min(int(first.get("number") or _DEFAULT_TOP), MAX_ROWS),
            "orderBy": order if order in _BOOKMARK_ORDERS else "created_at",
            "orderMode": "desc" if first.get("sort_mode") == "desc" else "asc",
            "filters": _without_ids(first["filters"]),
        }
    perspective = native.get("perspective")
    if perspective not in ("entities", "relationships") or (perspective, shape) not in QUERIES:
        raise LiveError("unsupported")
    relationships = perspective == "relationships"
    selections = native["dataSelection"]
    first = selections[0]
    now = now or datetime.now(UTC)
    since, until = period(range_days, now=now, range_key=range_key, start=range_start, end=range_end)
    start = _iso(since) if since else None
    # Every widget reads the period as filters on its own date, and as its query's dates (``computeWidgetFiltersForSelection``).
    variables: dict = {"filters": _dated(first["filters"], first["date_attribute"], since, until)}
    if relationships:
        variables["dynamicFrom"] = _without_ids(first["dynamicFrom"])
        variables["dynamicTo"] = _without_ids(first["dynamicTo"])
    if shape == "number":
        # As OpenCTI counts: ``count`` up to a day ago (``dayAgo()``), ``total`` now; the difference is the last 24 hours.
        variables["dateAttribute"] = first["date_attribute"]
        variables["endDate"] = _iso(now - timedelta(days=1))
        if not relationships:
            variables["startDate"] = start
    elif shape in ("distribution", "geo"):
        variables.update(
            field=first["attribute"],
            limit=min(int(first["number"] or _DEFAULT_TOP), MAX_ITEMS if shape == "distribution" else MAX_REGIONS),
            dateAttribute=first["date_attribute"],
            startDate=start,
            endDate=_iso(until or now),
        )
        if relationships:
            variables["isTo"] = bool(first["isTo"])
    elif shape == "series":
        variables = {
            "startDate": start or _iso(now - timedelta(days=_DEFAULT_SERIES_DAYS)),
            "endDate": _iso(until or now),
            "interval": native.get("parameters", {}).get("interval") or "day",
            "timeSeriesParameters": [
                {
                    "field": s["date_attribute"],
                    "filters": _dated(s["filters"], s["date_attribute"], since, until),
                    **(
                        {"dynamicFrom": _without_ids(s["dynamicFrom"]), "dynamicTo": _without_ids(s["dynamicTo"])}
                        if relationships
                        else {}
                    ),
                }
                for s in selections
            ],
        }
    else:  # rows: a list or a timeline, ordered and cut as OpenCTI's own
        orders = _RELATIONSHIP_ORDERS if relationships else _ENTITY_ORDERS
        entity_list = native.get("type") == "list" and not relationships
        # An entities list sorts on its sort field (created_at), ascending; a timeline and a
        # relationships list on their date, descending. A relationships list shows 50.
        order = first.get("sort_by") or ("created_at" if entity_list else first["date_attribute"])
        top = 50 if relationships and native.get("type") == "list" else _DEFAULT_TOP
        variables["first"] = min(int(first["number"] or top), MAX_ROWS)
        variables["orderBy"] = order if order in orders else "created_at"
        default_mode = "asc" if entity_list else "desc"
        variables["orderMode"] = first.get("sort_mode") if first.get("sort_mode") in ("asc", "desc") else default_mode
    return QUERIES[(perspective, shape)], variables


def _distribution_label(item: dict) -> object:
    entity = item.get("entity")
    if isinstance(entity, dict):
        representative = entity.get("representative")
        if isinstance(representative, dict) and representative.get("main"):
            return representative["main"]
        if entity.get("name"):
            return entity["name"]
        template = entity.get("template")
        if isinstance(template, dict) and template.get("name"):
            return template["name"]
    return item.get("label")


# ── What a distribution's items carry, and how each widget type looks ─────────
#
# OpenCTI's distribution widgets colour, link and label their items by these
# rules (``useDistributionGraphData.ts``, ``Charts.jsx``;
# ``dev-docs/dashboard-rendering-reference.md``): an item's entity colour (its
# type's family, or a label's, a marking's, a status' own colour) on the
# horizontal bars, an item's own colour on the donut and the polar area when
# the first item has one (true and false in green and red otherwise), a link
# to the entity counted, and the look of each type as generic options.

_IDENTIFIER = re.compile(r"(^id$|\.id$|_id$|_ids$)")
#: Material green and red 400, OpenCTI's true and false in a donut (600 in the light theme: the engine's twin).
_GREEN, _RED = "#66bb6a", "#ef5350"


def _own_color(entity: dict) -> str | None:
    """A label's, a marking's or a status' own colour (the later rule wins, as OpenCTI's)."""
    template = entity.get("template") if isinstance(entity.get("template"), dict) else {}
    found = None
    for value in (entity.get("color"), entity.get("x_opencti_color"), template.get("color")):
        if isinstance(value, str) and value:
            found = value
    return found


def _distribution_items(native: dict, items: list[dict], base_url: str) -> list[tuple]:
    """``(label, value, extras)`` per item: its entity type, a link to it, and the colour its widget type draws it in."""
    widget_type = native.get("type")
    attribute = str((native.get("dataSelection") or [{}])[0].get("attribute") or "entity_type")
    labels = [_distribution_label(i) for i in items]
    entities = [i.get("entity") if isinstance(i.get("entity"), dict) else {} for i in items]
    raws = list(labels)
    if attribute == "entity_type":
        # As ``useDistributionGraphData``: a type is shown by its name (``entity_<type>``), not its key - the key
        # a click filters on stays the type.
        labels = [entity_label(str(label)) if label and label != "unknown" else label for label in labels]
    colors: list[str | None] = [None] * len(items)
    if widget_type == "horizontal-bar":
        # Every bar its entity colour: the type's family (the label's for a field that is not an id), then its own.
        for index, (item, entity) in enumerate(zip(items, entities, strict=True)):
            kind = entity.get("entity_type") if _IDENTIFIER.search(attribute) else item.get("label")
            colors[index] = _own_color(entity) or item_color(kind if isinstance(kind, str) else None)
    elif widget_type in ("donut", "polar-area"):
        if entities and _own_color(entities[0]):
            colors = [_own_color(entity) for entity in entities]
        elif len(labels) == 2 and labels[0] in ("true", "false"):
            colors = [_GREEN, _RED] if labels[0] == "true" else [_RED, _GREEN]
    out = []
    for item, entity, label, raw, color in zip(items, entities, labels, raws, colors, strict=True):
        node_id = entity.get("id")
        out.append(
            (
                "Unknown" if label in (None, "", "unknown") else label,
                item.get("value"),
                {
                    "color": color,
                    "entityType": entity.get("entity_type"),
                    "key": raw if raw != label and isinstance(raw, str) else None,
                    # A restricted entity is not opened, as OpenCTI's bars do not.
                    "link": link(base_url, f"/dashboard/id/{quote(str(node_id), safe='')}") if node_id and label != "Restricted" else None,
                },
            )
        )
    return out


#: The interval a series is bucketed by, and how its dates are written.
_DATE_FORMATS = {"month": "monthYear", "quarter": "monthYear", "year": "year"}
#: What every OpenCTI chart shares: the accent then Material 400 / 600 in OpenCTI's order, SI numbers with a space,
#: axis labels in the text colour, dashed grid lines at 0.1.
_CHART = {
    "paletteLead": "accent", "palette": "materialMixed", "numberFormat": "siSpaced", "axisLabelColor": "text", "gridOpacity": 0.1,
    # ApexCharts' value axis and legend: square markers, labels in the text colour.
    "niceTicks": True, "legendMarker": "square", "legendLabelColor": "text",
}  # fmt: skip


def _chart_preset(native: dict) -> dict | None:
    """How OpenCTI draws a chart widget, as a visualization type and its generic options."""
    widget_type = native.get("type")
    parameters = native.get("parameters") if isinstance(native.get("parameters"), dict) else {}
    interval = parameters.get("interval") or "day"
    legend = "bottom" if parameters.get("legend") else "off"
    stack = {"stackMode": "stacked"} if parameters.get("stacked") else {}
    axes = {"dateFormat": _DATE_FORMATS.get(interval, "date"), "hideAxisLines": True, "gridLines": "horizontal", "xAxisFontSize": 12}
    if widget_type in ("line", "area"):
        options = {
            **_CHART, **axes, "yAxisFontSize": 14, "legendDisplay": legend, "lineWidth": 2, "markerDisplay": "off",
            # Days and weeks on a time axis, longer intervals one tick per bucket.
            "xAxisType": "time" if interval in ("day", "week") else "category",
        }  # fmt: skip
        if widget_type == "area":
            options.update(stack, fill="gradient", fillOpacityFrom=0.7, fillOpacityTo=0.1)
        return {"view": f"xtm.{widget_type}", "options": options}
    if widget_type == "vertical-bar":
        # A category axis whatever the interval, every label written (at -45 degrees when they would overlap);
        # the column width is the chart library's default (70%).
        return {
            "view": "xtm.column",
            "options": {**_CHART, **axes, **stack, "legendDisplay": legend, "barRadius": 4, "barSize": 70, "crowdedLabels": "rotate"},
        }
    if widget_type == "horizontal-bar":
        options = {
            **_CHART, "hideAxisLines": True, "gridLines": "horizontal", "legendDisplay": "off", "barRadius": 4, "barSize": 30,
            "valueTickCount": 1, "openLinks": True,
        }  # fmt: skip
        if parameters.get("distributed"):
            options["colorByPoint"] = True
        return {"view": "xtm.bar", "options": options}
    if widget_type == "donut":
        return {
            "view": "xtm.pie",
            "options": {
                "palette": "materialMixed", "separatorWidth": 3, "tooltipContent": "label", "labelDisplay": "percentage",
                "showDonutHole": True, "donutHoleSize": 70, "legendDisplay": "bottom", "collapseThreshold": 0,
                "legendMarker": "circle", "legendLabelColor": "text", "pieAlign": "top",
            },
        }  # fmt: skip
    if widget_type == "polar-area":
        return {
            "view": "xtm.polararea",
            "options": {
                "palette": "materialMixed", "areaOpacity": 0.5, "separatorWidth": 2, "tooltipContent": "label",
                "labelDisplay": "off", "legendDisplay": "bottom", "collapseThreshold": 0, "axisLabelColor": "text", "gridOpacity": 0.1,
            },
        }  # fmt: skip
    if widget_type == "radar":
        return {
            "view": "xtm.radar",
            "options": {
                "paletteLead": "accent", "areaOpacity": 0.2, "lineWidth": 1, "markerSize": 5, "labelMaxLength": 25,
                "labelCut": "words", "axisLabelColor": "text", "numberFormat": "siSpaced", "gridOpacity": 0.1, "legendDisplay": "bottom",
                "niceTicks": True,
            },
        }  # fmt: skip
    if widget_type == "tree":
        # Distributed: one Material colour per cell, the near-white ones left out; else the accent in shades of the value.
        palette = (
            {"colorByPoint": True, "palette": "materialMixed", "paletteNoPale": True}
            if parameters.get("distributed")
            else {"paletteLead": "accent", "palette": "materialMixed", "valueShades": True}
        )
        return {"view": "xtm.treemap", "options": {**palette, "separatorWidth": 3, "numberFormat": "siSpaced"}}
    if widget_type == "wordcloud":
        return {"view": "xtm.wordcloud", "options": {"palette": "materialMixed", "fontSizeMin": 20, "fontSizeMax": 50}}
    if widget_type == "heatmap":
        return {
            "view": "xtm.heatmap",
            "options": {
                "colorRanges": "heat", "separatorWidth": 1, "legendDisplay": "bottom", "dateFormat": "date",
                "xAxisFontSize": 12, "yAxisFontSize": 14, "axisLabelColor": "text", "dataValuesDisplay": "off",
            },
        }  # fmt: skip
    return None


def normalize(native: dict, shape: str, answer: object, base_url: str = "") -> dict:
    """The closed shape of an OpenCTI GraphQL answer (links from *base_url*)."""
    if not isinstance(answer, dict):
        raise LiveError("upstream_error")
    errors = answer.get("errors")
    data = answer.get("data")
    if errors or not isinstance(data, dict):
        raise LiveError("access_denied" if _is_auth_error(errors) else "upstream_error")
    value = next(iter(data.values()), None)
    if shape == "number":
        if not isinstance(value, dict):
            raise LiveError("upstream_error")
        total, before = number(value.get("total")), number(value.get("count"))
        change = total - before if total is not None and before is not None else None
        return number_data(total, change, previous=before, period="day")
    if shape == "distribution":
        items = [i for i in value if isinstance(i, dict)] if isinstance(value, list) else []
        if native.get("type") == "distribution-list":
            return distribution_data([_distribution_item(native, i, base_url) for i in items])
        return distribution_data(_distribution_items(native, items, base_url))
    if shape == "geo":
        return _geo([i for i in value if isinstance(i, dict)] if isinstance(value, list) else [])
    if shape == "series":
        lines = value if isinstance(value, list) else []
        selections = native["dataSelection"]
        series = []
        for index, line in enumerate(lines):
            points = line.get("data") if isinstance(line, dict) else None
            label = selections[index].get("label") if index < len(selections) else ""
            series.append(
                (
                    label or f"Series {index + 1}",
                    [(str(p.get("date", ""))[:10], p.get("value")) for p in points or [] if isinstance(p, dict)],
                )
            )
        return series_data(series)
    if not isinstance(value, dict):
        raise LiveError("upstream_error")
    return value


def _is_auth_error(errors: object) -> bool:
    if not isinstance(errors, list):
        return False
    for error in errors:
        if not isinstance(error, dict):
            continue
        name = str((error.get("extensions") or {}).get("code") or error.get("name") or "").upper()
        message = str(error.get("message") or "").lower()
        if name in ("FORBIDDEN_ACCESS", "AUTH_REQUIRED", "FORBIDDEN") or "you are not allowed" in message:
            return True
        if "you must be logged in" in message:
            return True
    return False


def _country_code(entity: dict) -> str | None:
    """A country's ISO 3166-1 alpha-2 code from its aliases: OpenCTI's map matches an alpha-3 alias, an alpha-2 one is taken too."""
    aliases = entity.get("x_opencti_aliases")
    for alias in aliases if isinstance(aliases, list) else []:
        code = alias.strip().upper() if isinstance(alias, str) else ""
        if code in ISO_3166_ALPHA2:
            return code
        if code in ISO_3166_ALPHA3_TO_ALPHA2:
            return ISO_3166_ALPHA3_TO_ALPHA2[code]
    return None


def _level(value: float, lowest: float, highest: float) -> int:
    """OpenCTI's map level of a country (``computeLevel(value, last, first + 1)`` over its descending distribution): 0-9."""
    return max(0, min(9, math.trunc(9 * (value - lowest) / (highest + 1 - lowest))))


def _geo(items: list[dict]) -> dict:
    """OpenCTI's map: its countries as regions ranked by level (0-9), its cities as points, as ``LocationMiniMapTargets``.

    The levels run over every value of the distribution, cities included, from the
    last (the least) to the first plus one, as ``StixRelationshipsMap`` computes them.
    """
    values = [v for v in (number(i.get("value")) for i in items) if v is not None]
    lowest, highest = (values[-1], values[0]) if values else (0.0, 0.0)
    points: list[tuple[object, ...]] = []
    regions: list[tuple[object, ...]] = []
    for item in items:
        entity = item.get("entity") if isinstance(item.get("entity"), dict) else {}
        kind = entity.get("entity_type")
        name = entity.get("name") or item.get("label")
        value = number(item.get("value"))
        if kind == "Country" and (code := _country_code(entity)) and value is not None:
            regions.append((code, name, value, _level(value, lowest, highest)))
        elif kind == "City" and entity.get("latitude") is not None and entity.get("longitude") is not None:
            points.append((name, entity.get("latitude"), entity.get("longitude"), value, kind))
    return geo_data(points, regions)


def _representative(node: object) -> str | None:
    if not isinstance(node, dict):
        return None
    representative = node.get("representative")
    if isinstance(representative, dict) and representative.get("main"):
        return representative["main"]
    return node.get("name") or node.get("value") or node.get("definition")


def _entity_link(base: str, entity_id: object) -> str | None:
    return link(base, f"/dashboard/id/{quote(str(entity_id), safe='')}") if entity_id else None


def _is_identifier(attribute: str) -> bool:
    """OpenCTI's ``isFieldForIdentifier``: the field names entities (``id``, ``*.id``, ``*_id``, ``*_ids``)."""
    return attribute == "id" or attribute.endswith((".id", "_id", "_ids"))


def _distribution_item(native: dict, item: dict, base_url: str) -> tuple:
    """One item of OpenCTI's distribution list (``StixCoreObjectsDistributionList`` / ``WidgetDistributionList``).

    Its label is the entity's representative for an identifier field, the type's name for
    ``entity_type`` (the translation OpenCTI means to show, not its raw ``entity_<type>`` key),
    else the raw label; its colour the entity's own, else that of its icon's type; its icon the
    entity type's when the item is an entity (a ``*_id`` field), else OpenCTI's default circle.
    """
    attribute = native["dataSelection"][0].get("attribute") or "entity_type"
    entity = item.get("entity") if isinstance(item.get("entity"), dict) else None
    raw = item.get("label")
    if _is_identifier(attribute):
        label = _representative(entity) or ("Unknown" if raw in (None, "unknown") else raw)
    elif attribute == "entity_type" and raw:
        label = entity_label(str(raw))
    else:
        label = raw
    template = (entity or {}).get("template") if isinstance((entity or {}).get("template"), dict) else {}
    own = (entity or {}).get("color") or (entity or {}).get("x_opencti_color") or template.get("color")
    kind = (entity or {}).get("entity_type") or (str(raw) if raw else None)
    entity_id = (entity or {}).get("id") if attribute.endswith("_id") else None
    shown = kind if entity_id else "default"
    linked = entity_id and label != "Restricted" and kind != "User"
    extras = {
        "color": own or entity_color(shown),
        "entityType": kind,
        "icon": entity_icon(shown),
        "link": _entity_link(base_url, entity_id) if linked else None,
    }
    return (label, item.get("value"), extras)


# ── Lists, timelines and bookmarks, as OpenCTI draws their cells ───────────────

#: The columns of a list that configures none (``WidgetListsDefaultColumns.ts``).
_ENTITY_COLUMNS: tuple[tuple[str, str], ...] = (
    ("entity_type", "Type"), ("name", "Name"), ("created", "Original creation date"),
    ("created_at", "Platform creation date"), ("modified", "Modification date"), ("createdBy", "Author"),
    ("creators", "Creators"), ("x_opencti_workflow_id", "Processing status"), ("objectLabel", "Labels"),
    ("objectMarking", "Marking"),
)  # fmt: skip
_RELATIONSHIP_COLUMNS: tuple[tuple[str, str], ...] = (
    ("entity_type", "Type"), ("from_entity_type", "Source type"), ("from_relationship_type", "Source name"),
    ("to_entity_type", "Target type"), ("to_relationship_type", "Target name"), ("created", "Original creation date"),
    ("created_at", "Platform creation date"), ("createdBy", "Author"), ("objectMarking", "Marking"),
)  # fmt: skip
_DATES = frozenset(
    {"created", "modified", "created_at", "updated_at", "published", "start_time", "stop_time", "first_seen", "last_seen",
     "valid_from", "valid_until"}
)  # fmt: skip
_CHIP_COLUMNS = frozenset({"entity_type", "from_entity_type", "to_entity_type", "x_opencti_workflow_id"})


def _kind(attribute: str) -> str:
    if attribute in _CHIP_COLUMNS:
        return "chip"
    if attribute in ("objectLabel", "objectMarking", "killChainPhases"):
        return "chips"
    return "date" if attribute in _DATES else "text"


def _cell(node: dict, attribute: str) -> object:
    """A list cell as OpenCTI's ``dataTableUtils`` draws it (a chip, chips, a date or text)."""
    if attribute == "entity_type":
        return entity_chip(node.get("entity_type"))
    if attribute in ("from_entity_type", "to_entity_type"):
        end = node.get(attribute[: -len("_entity_type")])
        return entity_chip(end.get("entity_type")) if isinstance(end, dict) else entity_chip("Restricted")
    if attribute in ("from_relationship_type", "to_relationship_type"):
        return _representative(node.get(attribute.split("_", 1)[0])) or "Restricted"
    if attribute in ("name", "representative"):
        return _representative(node)
    if attribute == "createdBy":
        return (node.get("createdBy") or {}).get("name") if isinstance(node.get("createdBy"), dict) else None
    if attribute == "creators":
        return ", ".join(str(c.get("name")) for c in node.get("creators") or [] if isinstance(c, dict) and c.get("name"))
    if attribute == "x_opencti_workflow_id":
        # ``ItemStatus``: the status' template, else "Disabled" when the type has no workflow, else "Unknown".
        template = (node.get("status") or {}).get("template") if isinstance(node.get("status"), dict) else None
        if isinstance(template, dict) and template.get("name"):
            return {"label": template.get("name"), "color": template.get("color")}
        return {"label": "Disabled" if node.get("workflowEnabled") is not True else "Unknown"}
    if attribute == "objectLabel":
        return [{"label": lab.get("value"), "color": lab.get("color")} for lab in node.get("objectLabel") or [] if isinstance(lab, dict)]
    if attribute == "objectMarking":
        return [c for c in (marking_chip(m) for m in node.get("objectMarking") or []) if c]
    if attribute == "killChainPhases":
        # A technique's tactics, in their kill chain's order (``rank``): what a flow step lays out stage by stage.
        # Each tactic with its kill chain (``group``) and its order there (``rank``): orders compare within a chain only.
        phases = {
            (p["phase_name"], p.get("kill_chain_name")): p.get("x_opencti_order")
            for p in node.get("killChainPhases") or []
            if isinstance(p, dict) and p.get("phase_name")
        }
        ordered = sorted(phases.items(), key=lambda item: (str(item[0][1] or ""), item[1] if isinstance(item[1], int) else 1_000))
        return [{"label": name, "group": chain, "rank": rank if isinstance(rank, int) else None} for (name, chain), rank in ordered]
    value = node.get(attribute)
    if isinstance(value, list):
        return ", ".join(str(v) for v in value if isinstance(v, str | int | float))
    return value if isinstance(value, str | int | float) else None


def _columns(native: dict, relationships: bool) -> list[tuple[str, str, str]]:
    """The list's columns: its own (``selection.columns``) or OpenCTI's defaults, at most :data:`MAX_COLUMNS`."""
    configured = native["dataSelection"][0].get("columns") or []
    defaults = dict(_RELATIONSHIP_COLUMNS if relationships else _ENTITY_COLUMNS)
    pairs = [(c["attribute"], c.get("label") or defaults.get(c["attribute"]) or c["attribute"]) for c in configured if c.get("attribute")]
    chosen = list(dict(pairs or (_RELATIONSHIP_COLUMNS if relationships else _ENTITY_COLUMNS)).items())
    return [(key, label, _kind(key)) for key, label in chosen[:MAX_COLUMNS]]


def _from_ids(group: object) -> set[str]:
    """The ids a relationship widget's ``fromId`` filters name."""
    found: set[str] = set()
    if isinstance(group, dict):
        for item in group.get("filters") or []:
            if isinstance(item, dict) and item.get("key") in ("fromId", ["fromId"]):
                found.update(str(v) for v in item.get("values") or [] if isinstance(v, str))
        for inner in group.get("filterGroups") or []:
            found |= _from_ids(inner)
    return found


def _nodes(value: dict) -> list[dict]:
    edges = value.get("edges") if isinstance(value.get("edges"), list) else []
    return [e["node"] for e in edges if isinstance(e, dict) and isinstance(e.get("node"), dict)]


def _timeline(client_base: str, native: dict, value: dict, relationships: bool) -> dict:
    """OpenCTI's timeline: one row per entity - for a relationship, its remote end - dated, with its second line."""
    first = native["dataSelection"][0]
    date_attribute = first.get("date_attribute") or "created_at"
    from_ids = _from_ids(first.get("filters"))
    rows = []
    for node in _nodes(value):
        shown = node
        if relationships:
            source, target = node.get("from"), node.get("to")
            # The remote end: the target when the widget's fromId names the source, else the source.
            to_side = isinstance(source, dict) and source.get("id") in from_ids and first.get("isTo") is not False
            shown = (target if to_side else source) or target or source
            if not isinstance(shown, dict):
                continue
        representative = shown.get("representative") if isinstance(shown.get("representative"), dict) else {}
        kind = shown.get("entity_type")
        rows.append(
            {
                "date": node.get(date_attribute) or node.get("created_at"),
                "name": _representative(shown) or shown.get("id"),
                "secondary": representative.get("secondary"),
                "_entityType": kind,
                "_icon": entity_icon(kind),
                "_color": entity_color(kind),
                "_url": _entity_link(client_base, shown.get("id")),
            }
        )
    columns = [("date", "Date", "datetime"), ("name", "Name"), ("secondary", "Description")]
    page = value.get("pageInfo") if isinstance(value.get("pageInfo"), dict) else {}
    return rows_data(columns, rows, page.get("globalCount"))


def _rows(client_base: str, native: dict, value: dict) -> dict:
    """A list, a timeline or the bookmarks, their cells typed as OpenCTI draws them, each row linked to its entity."""
    relationships = native.get("perspective") == "relationships"
    page = value.get("pageInfo") if isinstance(value.get("pageInfo"), dict) else {}
    if native.get("type") == "timeline":
        return _timeline(client_base, native, value, relationships)
    if native.get("type") == "bookmark":
        rows = [
            {
                "name": _representative(node) or node.get("id"),
                "entity_type": entity_chip(node.get("entity_type")),
                # "Updated on": a bookmark card says when its entity was last modified.
                "modified": node.get("modified") or node.get("updated_at"),
                "_entityType": node.get("entity_type"),
                "_icon": entity_icon(node.get("entity_type")),
                "_url": _entity_link(client_base, node.get("id")),
            }
            for node in _nodes(value)
        ]
        columns = [("name", "Name"), ("entity_type", "Type", "chip"), ("modified", "Updated", "date")]
        return rows_data(columns, rows, page.get("globalCount"))
    columns = _columns(native, relationships)
    rows = []
    for node in _nodes(value):
        row: dict = {key: _cell(node, key) for key, _, _ in columns}
        if relationships:
            # A relationship row opens its source (else its target), and leads with its type's icon
            # (or the inference wand) in the primary colour, as OpenCTI's relationships list.
            end = node.get("from") if isinstance(node.get("from"), dict) else node.get("to")
            row["_url"] = _entity_link(client_base, end.get("id")) if isinstance(end, dict) else None
            row["_icon"] = "wand-sparkles" if node.get("is_inferred") else entity_icon(node.get("entity_type"))
        else:
            row["_url"] = _entity_link(client_base, node.get("id"))
        row["_entityType"] = node.get("entity_type")
        rows.append(row)
    return rows_data(columns, rows, page.get("globalCount"))


# ── Presentation: how OpenCTI draws each widget, as generic options ──────────

#: Where OpenCTI's map opens when its widget names no centre and has no city (``GeoMap.tsx``: Paris).
_MAP_CENTER = (48.8566969, 2.3514616)
#: OpenCTI's city marker (``city_orange.png``): a 25 px building in the threat scale's level-5 orange.
_CITY_MARKER = {"markerIcon": "building-2", "markerColor": "#d95f00", "markerSize": 25}


#: The title OpenCTI's number card always shows (``WidgetNumber``'s container), by perspective.
_NUMBER_TITLES = {"entities": "Entities number", "relationships": "Relationships number", "audits": "Audits number"}


def _preset(native: dict) -> dict | None:
    """OpenCTI's look of a widget type as a visualization's options (``presentation``)."""
    widget_type = native.get("type")
    if widget_type == "number":
        # A 32 px figure under the difference over the last 24 hours (``WidgetNumber``); the card is headed by its
        # type ("Entities number"), its own title written in the body beside the difference.
        header = _NUMBER_TITLES.get(str(native.get("perspective")), "Entities number")
        label = ((native.get("parameters") or {}).get("title") or "").strip() or header
        return {"view": "xtm.singlevalue", "options": {"valueStyle": "difference", "headerTitle": header, "topLabel": label[:120]}}
    if widget_type == "list":
        # The DataTable's widget variant: a 42 px header, 48 px rows, an arrow column (a tertiary icon button,
        # in the primary colour).
        return {"view": "xtm.list", "options": {"listStyle": "table", "rowHeight": 48, "arrowColor": "accent"}}
    if widget_type == "bookmark":
        return {"view": "xtm.list", "options": {"listStyle": "cards", "cardsPerRow": 3}}
    if widget_type == "distribution-list":
        return {"view": "xtm.distribution", "options": {"listStyle": "rows"}}
    if widget_type == "timeline":
        return {"view": "xtm.timeline", "options": {"timelineStyle": "alternate"}}
    if widget_type == "map":
        first = (native.get("dataSelection") or [{}])[0]
        lat, lng = first.get("centerLat"), first.get("centerLng")
        centred = bool(lat) and bool(lng)
        zoom = first.get("zoom")
        return {
            "view": "xtm.map",
            "options": {
                "layers": ["choropleth", "marker"],
                "regionColors": "level",
                "basemap": "contrast",
                **_CITY_MARKER,
                # maplibre opens one zoom level below the widget's (``zoom - 1``), on its centre,
                # else on its first city, else on Paris.
                "zoom": max(0.0, float(zoom if zoom is not None else 2) - 1),
                "centerLat": float(lat) if centred else _MAP_CENTER[0],
                "centerLng": float(lng) if centred else _MAP_CENTER[1],
                **({} if centred else {"centerOnFirstMarker": True}),
            },
        }
    return _chart_preset(native)



def presentation(native: dict) -> dict | None:
    """OpenCTI's look of a widget type as a visualization's options (``dashboard_live.presentation_of``), its
    title written as OpenCTI writes it: each word's first letter up (``CardTitle``)."""
    from app.services.dashboard_definition import VIEW_TO_TYPE
    from app.services.dashboard_platform import NativeError, check_native

    preset = _preset(native)
    if preset is None:
        try:
            view = VIEW_TO_TYPE.get(check_native("opencti", native).preferred_view or "")
        except NativeError:
            return None
        if view is None:
            return None
        preset = {"view": view, "options": {}}
    return {**preset, "options": {**preset["options"], "titleStyle": "capitalize", "emptyMessage": "found", "accentColor": "brand"}}

_SAVED_FILTER = "query XtmOneSavedFilter($id: ID!) { savedFilter(id: $id) { filters } }"
#: A selection's saved filter ids, and the filters each stands for.
_SAVED_FILTERS = (("filters_id", "filters"), ("dynamicFrom_id", "dynamicFrom"), ("dynamicTo_id", "dynamicTo"))


async def resolve_saved_filters(client, native: dict, cache: dict[str, dict] | None = None, *, keep_unreadable: bool = False) -> dict:
    """*native* with each saved filter it names read and put in its place (``resolveSavedFiltersInDataSelection``),
    through *client* - the viewer's own access. A saved filter they cannot read stops the widget (``access_denied``);
    one OpenCTI answers unreadably, or with filters a widget may not hold, is an ``upstream_error``. *cache* keeps
    the filters read, by id, for the other widgets read through the same client. With *keep_unreadable* (a file
    written for another OpenCTI), one that cannot be read keeps its id instead, the others written in place."""
    import json

    from app.services.dashboard_platform import NativeError
    from app.services.dashboard_platform._filters import check_filter_group
    from app.services.dashboard_platform.opencti import OPERATORS

    selections = [s for s in native.get("dataSelection") or [] if isinstance(s, dict)]
    wanted = sorted({str(s[key]) for s in selections for key, _ in _SAVED_FILTERS if s.get(key)})
    if not wanted:
        return native
    found: dict[str, dict] = cache if cache is not None else {}
    for saved_id in wanted:
        if saved_id in found:
            continue
        try:
            answer = await request_json(
                "POST", f"{client.base_url}/graphql", client.headers,
                json_body={"query": _SAVED_FILTER, "variables": {"id": saved_id}}, what="OpenCTI",
            )  # fmt: skip
            data = answer.get("data") if isinstance(answer, dict) and isinstance(answer.get("data"), dict) else {}
            record = data.get("savedFilter")
            if not isinstance(record, dict) or not isinstance(record.get("filters"), str):
                raise LiveError("access_denied")
            try:
                found[saved_id] = check_filter_group(json.loads(record["filters"][:100_000]), style="opencti", operators=OPERATORS, where="savedFilter")
            except (ValueError, NativeError):
                raise LiveError("upstream_error") from None
        except LiveError:
            if not keep_unreadable:
                raise
    resolved = copy.deepcopy(native)
    for selection in resolved["dataSelection"]:
        for key, target in _SAVED_FILTERS:
            saved_id = selection.get(key) if isinstance(selection, dict) else None
            if saved_id and str(saved_id) in found:
                selection.pop(key)
                selection[target] = found[str(saved_id)]
    return resolved


_MARKINGS = """query XtmOneMarkings {
  markingDefinitions(first: 500) { edges { node { id definition definition_type x_opencti_order } } }
}"""


def marking_exclusions(markings: list[dict], ceiling: tuple[str, ...]) -> list[str]:
    """The markings a published link's *ceiling* leaves out (OpenCTI's public dashboards' ``max_markings``): each
    of a type the ceiling names above its marking's order, and every one of a type it does not name."""
    highest: dict[str, float] = {}
    for marking in markings:
        if marking.get("definition") in ceiling:
            kind, order = str(marking.get("definition_type") or ""), number(marking.get("x_opencti_order"))
            if order is not None:
                highest[kind] = max(highest.get(kind, order), order)
    excluded = []
    for marking in markings:
        kind, order = str(marking.get("definition_type") or ""), number(marking.get("x_opencti_order"))
        if kind not in highest or order is None or order > highest[kind]:
            excluded.append(str(marking.get("id")))
    return sorted({m for m in excluded if m and m != "None"})


def under_ceiling(native: dict, excluded: list[str]) -> dict:
    """*native* with every selection leaving out what carries one of the *excluded* markings."""
    if not excluded:
        return native
    limited = copy.deepcopy(native)
    for selection in limited.get("dataSelection") or []:
        if not isinstance(selection, dict):
            continue
        group = selection.get("filters") if isinstance(selection.get("filters"), dict) else {"mode": "and", "filters": [], "filterGroups": []}
        selection["filters"] = {
            "mode": "and",
            "filters": [{"key": ["objectMarking"], "values": excluded, "operator": "not_eq", "mode": "and"}],
            "filterGroups": [group],
        }
    return limited


#: What an object above a published link's ceiling is drawn as: OpenCTI's "Restricted" end, never its name or link.
_RESTRICTED = {"id": None, "entity_type": "Restricted", "representative": {"main": "Restricted"}}


def mask_restricted(answer: object, excluded: set[str]) -> object:
    """*answer* with every object carrying one of the *excluded* markings drawn as :data:`_RESTRICTED`.

    The ceiling's filter keeps what a widget counts or lists below it; what those
    reach - a relationship's ends, a distribution's or a map's entities, a row's
    author - is read with the publisher's access, so it is masked here, as OpenCTI
    shows an end its reader cannot see. A relationship with a masked end is named from its ends again.
    """

    def marked(value: object) -> bool:
        markings = value.get("objectMarking") if isinstance(value, dict) else None
        return isinstance(markings, list) and any(isinstance(m, dict) and str(m.get("id")) in excluded for m in markings)

    def walk(value: object) -> object:
        if isinstance(value, list):
            return [walk(item) for item in value]
        if not isinstance(value, dict):
            return value
        out = {
            key: dict(_RESTRICTED, name="Restricted") if key in ("from", "to", "entity", "createdBy") and marked(item) else walk(item)
            for key, item in value.items()
        }
        ends = [out.get("from"), out.get("to")]
        masked = [isinstance(end, dict) and end.get("id") is None and end.get("entity_type") == "Restricted" for end in ends]
        if any(masked) and isinstance(out.get("representative"), dict):
            names = [(end.get("representative") or {}).get("main") if isinstance(end, dict) else None for end in ends]
            out["representative"] = {"main": f"{names[0] or 'Restricted'} \u27a1\ufe0f {names[1] or 'Restricted'}", "secondary": None}
        return out

    return walk(answer)


async def _excluded_markings(client, ceiling: tuple[str, ...]) -> list[str]:
    """The markings to leave out under *ceiling*, read from the platform; every one when they cannot be read."""
    answer = await request_json("POST", f"{client.base_url}/graphql", client.headers, json_body={"query": _MARKINGS}, what="OpenCTI")
    data = answer.get("data") if isinstance(answer, dict) and isinstance(answer.get("data"), dict) else {}
    edges = (data.get("markingDefinitions") or {}).get("edges") if isinstance(data.get("markingDefinitions"), dict) else None
    if not isinstance(edges, list):
        # Never more than the ceiling allows: without the list, nothing marked is read.
        raise LiveError("upstream_error")
    markings = [e["node"] for e in edges if isinstance(e, dict) and isinstance(e.get("node"), dict)]
    return marking_exclusions(markings, ceiling)


async def fetch(client, request: LiveRequest) -> dict:
    native = await resolve_saved_filters(client, request.native)
    excluded = await _excluded_markings(client, request.marking_ceiling) if request.marking_ceiling else []
    native = under_ceiling(native, excluded)
    if standard_ids([native]):
        native = replace_ids(native, await resolve_standard_ids(client, [native]))
    query, variables = build_query(
        native,
        request.shape or "",
        range_days=request.range_days,
        range_key=request.range_key,
        range_start=request.range_start,
        range_end=request.range_end,
    )
    answer = await request_json(
        "POST",
        f"{client.base_url}/graphql",
        client.headers,
        json_body={"query": query, "variables": variables},
        what="OpenCTI",
    )
    if excluded:
        answer = mask_restricted(answer, set(excluded))
    data = normalize(request.native, request.shape or "", answer, client.base_url)
    if request.shape == "rows":
        return _rows(client.base_url, request.native, data)
    return data


def open_url(client, native: dict) -> str | None:
    return link(client.base_url, "/dashboard/workspaces/dashboards")


# ── STIX standard ids ───────────────────────────────────────────────────────
#
# An OpenCTI export carries the STIX standard ids of the objects its filters
# name (``identity--...``, ``marking-definition--...``), so it moves between
# instances; OpenCTI's own import converts them to the target's internal ids.
# A widget keeps them as exported - so it stays portable when the dashboard is
# exported, published or saved as a pack - and they are converted when it is
# read, against the integration it reads, with the viewer's own access:
# aliased queries of at most :data:`IDS_PER_QUERY` ids resolve them all, and
# the answer is cached with the data. An id the platform does not know (or
# does not show this person) is kept as it is.

STIX_ID_RE = re.compile(r"^[a-z][a-z0-9-]*--[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
#: Ids resolved by one query.
IDS_PER_QUERY = 100


def _walk_values(group: object, visit) -> object:
    """*group* with every filter value passed through *visit* (nested groups and ``{key, values}`` too)."""
    if isinstance(group, dict):
        out = dict(group)
        if isinstance(out.get("values"), list):
            out["values"] = [_walk_values(v, visit) for v in out["values"]]
        for key in ("filters", "filterGroups"):
            if isinstance(out.get(key), list):
                out[key] = [_walk_values(item, visit) for item in out[key]]
        return out
    return visit(group)


def _map_native(native: dict, visit) -> dict:
    out = copy.deepcopy(native)
    for selection in out.get("dataSelection") or []:
        for key in ("filters", "dynamicFrom", "dynamicTo"):
            if isinstance(selection.get(key), dict):
                selection[key] = _walk_values(selection[key], visit)
    return out


def standard_ids(natives: list[dict]) -> list[str]:
    """The STIX standard ids the widgets' filters name, in order, once each."""
    found: dict[str, None] = {}

    def visit(value: object) -> object:
        if isinstance(value, str) and STIX_ID_RE.match(value):
            found.setdefault(value, None)
        return value

    for native in natives:
        _map_native(native, visit)
    return list(found)


def resolution_query(ids: list[str]) -> tuple[str, dict]:
    """One aliased query resolving each standard id to the platform's internal id."""
    aliases = {f"i{index}": sid for index, sid in enumerate(ids)}
    definitions = ", ".join(f"${alias}: String!" for alias in aliases)
    fields = "\n".join(
        f"  {alias}: stixObjectOrStixRelationship(id: ${alias}) {{ ... on BasicObject {{ id }} }}" for alias in aliases
    )
    return f"query XtmOneResolveIds({definitions}) {{\n{fields}\n}}", aliases


def replace_ids(native: dict, mapping: dict[str, str]) -> dict:
    return _map_native(native, lambda value: mapping.get(value, value) if isinstance(value, str) else value)


async def resolve_standard_ids(client, natives: list[dict]) -> dict[str, str]:
    """``{standard_id: internal_id}`` for the ids the platform shows this person; what it cannot say is left out."""
    ids = standard_ids(natives)
    mapping: dict[str, str] = {}
    for start in range(0, len(ids), IDS_PER_QUERY):
        query, variables = resolution_query(ids[start : start + IDS_PER_QUERY])
        try:
            answer = await request_json(
                "POST", f"{client.base_url}/graphql", client.headers,
                json_body={"query": query, "variables": variables}, what="OpenCTI",
            )  # fmt: skip
        except LiveError as exc:
            logger.info("OpenCTI standard ids not resolved (%s): kept as exported", exc.code)
            return mapping
        data = answer.get("data") if isinstance(answer, dict) and isinstance(answer.get("data"), dict) else {}
        for alias, sid in variables.items():
            node = data.get(alias)
            internal = node.get("id") if isinstance(node, dict) else None
            if isinstance(internal, str) and 0 < len(internal) <= 100:
                mapping[sid] = internal
    return mapping


__all__ = [
    "QUERIES", "build_query", "fetch", "marking_exclusions", "mask_restricted", "normalize", "open_url", "presentation", "replace_ids", "resolve_saved_filters",
    "resolve_standard_ids", "standard_ids", "under_ceiling",
]  # fmt: skip
