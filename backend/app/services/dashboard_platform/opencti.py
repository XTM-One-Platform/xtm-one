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

"""OpenCTI dashboard widgets kept natively in an XTM One dashboard.

The configuration is the widget of an OpenCTI dashboard manifest (the
``widgets`` map of a workspace of type ``dashboard``, or the decoded
``configuration`` of a widget export) without its ``id`` and ``layout``, which
are the dashboard's: ``type``, ``perspective``, ``dataSelection`` and
``parameters``. The schema mirrors ``opencti-front/src/utils/widget/widget.d.ts``
and ``widgetUtils.tsx`` (``widgetVisualizationTypes``).

Filter values may hold STIX standard ids (an export carries them, so a
dashboard moves between instances); OpenCTI resolves them on import and on
queries alike. A saved filter (``filters_id``, ``dynamicFrom_id``,
``dynamicTo_id``) names an object of the integration's OpenCTI: it is read
there, with the viewer's access, each time the widget is (as OpenCTI's own
dashboards do), and a widget bound to another instance reads nothing.
"""

from __future__ import annotations

import re

from . import NativeCheck, NativeError
from ._filters import KEY_RE, check_filter_group, check_key, empty_group

#: OpenCTI's filter operators (``enum FilterOperator``), without ``script``,
#: which runs a script on the platform and has no place in a portable widget.
OPERATORS: frozenset[str] = frozenset(
    {
        "eq", "not_eq", "lt", "lte", "gt", "gte", "match", "wildcard", "contains", "not_contains",
        "ends_with", "not_ends_with", "starts_with", "not_starts_with", "nil", "not_nil", "search",
        "within", "only_eq_to", "not_only_eq_to", "has_changed", "not_has_changed",
    }
)  # fmt: skip

PERSPECTIVES: tuple[str, ...] = ("entities", "relationships", "audits")

#: Widget type -> (max data selections, the shape XTM One draws or None, perspectives allowed).
WIDGET_TYPES: dict[str, tuple[int, str | None, tuple[str, ...]]] = {
    "number": (1, "number", PERSPECTIVES),
    "list": (1, "rows", PERSPECTIVES),
    "distribution-list": (1, "distribution", PERSPECTIVES),
    "vertical-bar": (5, "series", PERSPECTIVES),
    "line": (5, "series", PERSPECTIVES),
    "area": (5, "series", PERSPECTIVES),
    "timeline": (1, "rows", PERSPECTIVES),
    "donut": (1, "distribution", PERSPECTIVES),
    "horizontal-bar": (2, "distribution", PERSPECTIVES),
    # OpenCTI's tree is a treemap of a distribution; its map, the countries and
    # cities of a relationship distribution.
    "tree": (1, "distribution", PERSPECTIVES),
    "map": (1, "geo", ("relationships",)),
    # The same distribution as a donut, drawn as OpenCTI draws it.
    "radar": (1, "distribution", PERSPECTIVES),
    "polar-area": (1, "distribution", PERSPECTIVES),
    "wordcloud": (1, "distribution", PERSPECTIVES),
    # The same time series as the bars and lines, one row per selection.
    "heatmap": (5, "series", PERSPECTIVES),
    # The reader's own bookmarks in OpenCTI.
    "bookmark": (1, "rows", ("entities",)),
}

#: How XTM One draws each type the way OpenCTI does.
PREFERRED_VIEWS: dict[str, str] = {
    "donut": "donut", "horizontal-bar": "hbar", "vertical-bar": "bar", "line": "line", "area": "area",
    "list": "list", "timeline": "timeline", "distribution-list": "distribution", "number": "number",
    "tree": "treemap", "map": "map", "radar": "radar", "polar-area": "polar", "wordcloud": "wordcloud",
    "heatmap": "heatmap", "bookmark": "list",
}  # fmt: skip

INTERVALS: tuple[str, ...] = ("day", "week", "month", "quarter", "year")
MAX_TEXT = 200
MAX_COLUMNS = 20

_SAVED_FILTER_KEYS = ("filters_id", "dynamicFrom_id", "dynamicTo_id")
#: What an OpenCTI id looks like (an internal UUID, or a standard id).
_SAVED_FILTER_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,99}$")
_SELECTION_KEYS = {
    "label", "number", "attribute", "date_attribute", "isTo", "perspective", "filters", "dynamicFrom",
    "dynamicTo", "columns", "sort_by", "sort_mode", "instance_id", "centerLat", "centerLng", "zoom", "unique",
}  # fmt: skip


def _text(value: object, where: str, *, limit: int = MAX_TEXT) -> str:
    if not isinstance(value, str) or len(value) > limit or "\x00" in value:
        raise NativeError(f"{where} must be text of at most {limit} characters.")
    return value


def _number(value: object, where: str, *, low: float, high: float) -> float | int:
    if isinstance(value, str):
        # OpenCTI's map widget stores its zoom as text ("2").
        try:
            value = float(value) if "." in value else int(value)
        except ValueError:
            raise NativeError(f"{where} must be a number between {low:g} and {high:g}.") from None
    if isinstance(value, bool) or not isinstance(value, int | float) or not low <= value <= high:
        raise NativeError(f"{where} must be a number between {low:g} and {high:g}.")
    return value


def _check_selection(selection: object, where: str, default_perspective: str) -> dict:
    if not isinstance(selection, dict):
        raise NativeError(f"{where} must be an object.")
    unknown = set(selection) - _SELECTION_KEYS - set(_SAVED_FILTER_KEYS)
    if unknown:
        raise NativeError(f"{where} has unknown keys: {', '.join(sorted(unknown))}.")
    perspective = selection.get("perspective") or default_perspective
    if perspective not in PERSPECTIVES:
        raise NativeError(f"{where}.perspective must be one of: {', '.join(PERSPECTIVES)}.")
    # OpenCTI saves an emptied top N as null, and reads it as its default.
    number = selection.get("number", 10)
    out: dict = {
        "label": _text(selection.get("label", ""), f"{where}.label"),
        "number": None if number is None else int(_number(number, f"{where}.number", low=1, high=1000)),
        "attribute": check_key(selection.get("attribute", "entity_type"), f"{where}.attribute"),
        "date_attribute": check_key(selection.get("date_attribute", "created_at"), f"{where}.date_attribute"),
        "isTo": bool(selection.get("isTo", True)),
        "perspective": perspective,
    }
    for key in _SAVED_FILTER_KEYS:
        saved = selection.get(key)
        if saved in (None, ""):
            continue
        if not isinstance(saved, str) or not _SAVED_FILTER_ID.fullmatch(saved):
            raise NativeError(f"{where}.{key} must be the id of an OpenCTI saved filter.")
        out[key] = saved
    for key in ("filters", "dynamicFrom", "dynamicTo"):
        out[key] = (
            check_filter_group(selection.get(key), style="opencti", operators=OPERATORS, where=f"{where}.{key}")
            if selection.get(key) is not None
            else empty_group("opencti")
        )
    if "columns" in selection:
        columns = selection["columns"]
        if not isinstance(columns, list) or len(columns) > MAX_COLUMNS:
            raise NativeError(f"{where}.columns must be a list of at most {MAX_COLUMNS} columns.")
        checked: list[dict] = []
        for index, column in enumerate(columns):
            at = f"{where}.columns[{index}]"
            if not isinstance(column, dict) or set(column) - {"attribute", "label", "displayStyle", "variableName", "attributeType"}:
                raise NativeError(f"{at} must be an object with attribute, label, displayStyle, variableName, attributeType.")
            item = {"attribute": check_key(column.get("attribute"), f"{at}.attribute")}
            # attributeType: the type of a custom field's column.
            for key in ("label", "displayStyle", "variableName", "attributeType"):
                if column.get(key) is not None:
                    item[key] = _text(column[key], f"{at}.{key}")
            checked.append(item)
        out["columns"] = checked
    if selection.get("sort_by") is not None:
        out["sort_by"] = check_key(selection["sort_by"], f"{where}.sort_by")
    if selection.get("sort_mode") is not None:
        if selection["sort_mode"] not in ("asc", "desc"):
            raise NativeError(f"{where}.sort_mode must be 'asc' or 'desc'.")
        out["sort_mode"] = selection["sort_mode"]
    if "unique" in selection:
        # Audits' "Distinct": counts distinct values of the attribute.
        out["unique"] = bool(selection["unique"])
    if selection.get("instance_id") is not None:
        out["instance_id"] = _text(selection["instance_id"], f"{where}.instance_id", limit=100)
    for key, low, high in (("centerLat", -90, 90), ("centerLng", -180, 180), ("zoom", 0, 20)):
        if selection.get(key) is not None:
            out[key] = _number(selection[key], f"{where}.{key}", low=low, high=high)
    return out


def check_native(native: object) -> NativeCheck:
    """Validate an OpenCTI widget configuration (no ``id``, no ``layout``)."""
    if not isinstance(native, dict):
        raise NativeError("An OpenCTI widget must be an object with 'type', 'perspective' and 'dataSelection'.")
    unknown = set(native) - {"type", "perspective", "dataSelection", "parameters", "id", "layout"}
    if unknown:
        raise NativeError(f"The OpenCTI widget has unknown keys: {', '.join(sorted(unknown))}.")
    widget_type = native.get("type")
    if widget_type == "text":
        raise NativeError("An OpenCTI text widget becomes a 'text' widget of the dashboard, not a platform widget.")
    if widget_type not in WIDGET_TYPES:
        raise NativeError(f"OpenCTI widget type must be one of: {', '.join(WIDGET_TYPES)}.")
    max_selections, shape, perspectives = WIDGET_TYPES[widget_type]
    perspective = native.get("perspective") or perspectives[0]
    if perspective not in perspectives:
        raise NativeError(f"A {widget_type} widget's perspective must be one of: {', '.join(perspectives)}.")
    selections = native.get("dataSelection")
    if not isinstance(selections, list) or not 1 <= len(selections) <= max_selections:
        raise NativeError(f"A {widget_type} widget takes 1 to {max_selections} dataSelection entries.")
    checked = [_check_selection(s, f"dataSelection[{i}]", perspective) for i, s in enumerate(selections)]
    parameters = native.get("parameters") or {}
    if not isinstance(parameters, dict):
        raise NativeError("parameters must be an object.")
    extra = set(parameters) - {"title", "description", "interval", "stacked", "legend", "distributed", "content"}
    if extra:
        raise NativeError(f"parameters has unknown keys: {', '.join(sorted(extra))}.")
    params: dict = {}
    for key in ("title", "description"):
        if isinstance(parameters.get(key), str) and parameters[key].strip():
            params[key] = _text(parameters[key], f"parameters.{key}").strip()
    if parameters.get("interval") is not None:
        if parameters["interval"] not in INTERVALS:
            raise NativeError(f"parameters.interval must be one of: {', '.join(INTERVALS)}.")
        params["interval"] = parameters["interval"]
    for key in ("stacked", "legend", "distributed"):
        if key in parameters:
            params[key] = bool(parameters[key])
    # Audit data is not queried from a dashboard (it needs an administrator of
    # the platform): such a widget is kept and drawn as a link.
    drawn = shape if perspective != "audits" else None
    return NativeCheck(
        native={"type": widget_type, "perspective": perspective, "dataSelection": checked, "parameters": params},
        shape=drawn,  # type: ignore[arg-type]
        native_type=widget_type,
        title=params.get("title"),
        preferred_view=PREFERRED_VIEWS.get(widget_type),
    )


# ── What an agent reads before writing a widget (``describe_widget``) ──────


def _group(*filters: dict) -> dict:
    return {"mode": "and", "filters": list(filters), "filterGroups": []}


def _eq(key: str, *values: str) -> dict:
    return {"key": [key], "values": list(values), "operator": "eq", "mode": "or"}


def _example(widget_type: str) -> dict:
    """A configuration of *widget_type* OpenCTI's editor could have written: valid, and a sensible start."""
    _max, shape, perspectives = WIDGET_TYPES[widget_type]
    perspective = perspectives[0]
    if perspective == "relationships":
        target = "Country" if widget_type == "map" else "Sector"
        selection: dict = {
            "label": f"Intrusion sets targeting a {target.lower()}", "perspective": "relationships", "isTo": True,
            "attribute": "internal_id", "date_attribute": "created_at", "number": 10,
            "filters": _group(_eq("relationship_type", "targets")),
            "dynamicFrom": _group(_eq("entity_type", "Intrusion-Set")), "dynamicTo": _group(_eq("entity_type", target)),
        }  # fmt: skip
    else:
        selection = {
            "label": "Threats", "perspective": "entities", "isTo": True, "date_attribute": "created_at", "number": 10,
            "attribute": "entity_type", "filters": _group(_eq("entity_type", "Intrusion-Set", "Malware", "Threat-Actor-Group")),
        }  # fmt: skip
        if widget_type in ("list", "timeline", "bookmark", "number"):
            selection["filters"] = _group(_eq("entity_type", "Report"))
            selection["label"] = "Reports"
    parameters: dict = {"title": selection["label"]}
    if shape == "series":
        parameters["interval"] = "month"
    editor = EDITOR_TYPES.get(widget_type, {})
    for flag in ("stacked", "legend"):
        if editor.get(flag):
            parameters[flag] = flag == "legend"
    if editor.get("distributed"):
        parameters["distributed"] = False
    return {"type": widget_type, "perspective": perspective, "dataSelection": [selection], "parameters": parameters}


def describe(widget_type: str | None = None) -> dict:
    """OpenCTI's widget types, or one of them with an example configuration and the rules it follows."""
    if widget_type is None:
        return {
            "types": [
                {"type": t, "draws": shape, "view": PREFERRED_VIEWS.get(t), "dataSelections": f"1-{limit}", "perspectives": list(persp)}
                for t, (limit, shape, persp) in WIDGET_TYPES.items()
            ]
        }
    if widget_type not in WIDGET_TYPES:
        raise NativeError(f"OpenCTI widget type must be one of: {', '.join(WIDGET_TYPES)}.")
    limit, shape, perspectives = WIDGET_TYPES[widget_type]
    return {
        "type": widget_type,
        "draws": shape,
        "view": PREFERRED_VIEWS.get(widget_type),
        "perspectives": list(perspectives),
        "dataSelections": f"1-{limit}",
        "example": _example(widget_type),
        "rules": [
            "dataSelection entries: label, perspective, filters (a filter group), attribute (what a breakdown counts), "
            "date_attribute, number (top N), isTo; relationships also take dynamicFrom / dynamicTo (filter groups on "
            "their source and target) and isTo (count the target side).",
            "A filter group is {mode: and|or, filters: [{key: [key], values: [...], operator, mode: or|and}], "
            f"filterGroups: [nested groups]}}; operators: {', '.join(sorted(OPERATORS))}.",
            "The keys, the entity types, the attributes and the values come from the platform: look them up "
            "(kinds entity_types, filter_keys, attributes, values) instead of guessing them.",
            f"parameters: title, description, interval ({', '.join(INTERVALS)}) for a series, stacked, legend, distributed.",
            "A dashboard input is read as '$token$' in a filter value.",
        ],
    }


# ── The widget editor (``forms``): OpenCTI's own, ``opencti-front/src/private/components/widgets/*`` ──

#: Widget type -> what OpenCTI's editor offers for it (``widgetUtils.tsx``), in its order: data
#: selections at most, perspectives, category, and the parameters it shows.
EDITOR_TYPES: dict[str, dict] = {
    "number": {"limit": 1, "category": "number"},
    "list": {"limit": 1, "category": "list"},
    "distribution-list": {"limit": 1, "category": "distribution", "attribute": True},
    "vertical-bar": {"limit": 5, "category": "timeseries", "stacked": True, "legend": True},
    "line": {"limit": 5, "category": "timeseries", "legend": True},
    "area": {"limit": 5, "category": "timeseries", "stacked": True, "legend": True},
    "timeline": {"limit": 1, "category": "list", "perspectives": ["entities", "relationships"]},
    "donut": {"limit": 1, "category": "distribution", "attribute": True},
    "horizontal-bar": {"limit": 2, "category": "distribution", "attribute": True},
    "radar": {"limit": 1, "category": "distribution", "attribute": True},
    "polar-area": {"limit": 1, "category": "distribution", "attribute": True},
    "heatmap": {"limit": 5, "category": "timeseries"},
    "tree": {"limit": 1, "category": "distribution", "attribute": True, "distributed": True},
    "map": {"limit": 1, "category": "distribution", "attribute": True, "perspectives": ["relationships"]},
    "bookmark": {"limit": 1, "category": "list", "perspectives": ["entities"]},
    "wordcloud": {"limit": 1, "category": "distribution", "attribute": True},
}

_TYPE_LABELS = {
    "number": "Number", "list": "List", "distribution-list": "Distribution list", "vertical-bar": "Vertical bars",
    "line": "Line", "area": "Area", "timeline": "Timeline", "donut": "Donut", "horizontal-bar": "Horizontal bars",
    "radar": "Radar", "polar-area": "Polar area", "heatmap": "Heatmap", "tree": "Tree", "map": "Map",
    "bookmark": "Bookmarks", "wordcloud": "Word cloud",
}  # fmt: skip
#: Each type's tile in the editor, as a Lucide icon.
_TYPE_ICONS = {
    "number": "hash", "list": "list", "distribution-list": "list-ordered", "vertical-bar": "chart-column",
    "line": "chart-line", "area": "chart-area", "timeline": "git-commit-vertical", "donut": "chart-pie",
    "horizontal-bar": "chart-bar", "radar": "radar", "polar-area": "chart-pie", "heatmap": "grid-3x3", "tree": "layout-dashboard",
    "map": "map", "bookmark": "bookmark", "wordcloud": "cloud",
}  # fmt: skip
_PERSPECTIVE_LABELS = {"entities": "Entities", "relationships": "Knowledge graph", "audits": "Activity & history"}

#: Breakdowns of relationships (``RELATIONSHIPS_WIDGET_ATTRIBUTES``), of entities with no type
#: (``ENTITIES_WIDGET_COMMON_ATTRIBUTES``), of activity and history.
_RELATIONSHIP_ATTRIBUTES = [
    "internal_id", "entity_type", "relationship_type", "created-by.internal_id", "object-marking.internal_id",
    "kill-chain-phase.internal_id", "creator_id", "x_opencti_workflow_id",
]  # fmt: skip
_COMMON_ATTRIBUTES = [
    "entity_type", "created-by.internal_id", "object-label.internal_id", "object-assignee.internal_id",
    "object-marking.internal_id", "kill-chain-phase.internal_id", "x_opencti_workflow_id",
]  # fmt: skip
_AUDIT_ATTRIBUTES = [
    "entity_type", "context_data.id", "context_data.created_by_ref_id", "context_data.labels_ids",
    "context_data.marking_definitions", "context_data.creator_ids", "context_data.search", "event_type", "event_scope",
    "user_id", "group_ids", "organization_ids",
]  # fmt: skip
_SORT_FIELDS = [
    "created", "created_at", "modified", "updated_at", "name", "valid_from", "valid_until", "entity_type", "value",
    "x_opencti_workflow_id", "opinions_metrics_mean", "opinions_metrics_max", "opinions_metrics_min", "opinions_metrics_total",
]  # fmt: skip
#: Activity and history count these types; bookmarks list these.
_AUDIT_TYPES = ["History", "Activity"]
_BOOKMARK_TYPES = ["Malware", "Threat-Actor-Individual", "Threat-Actor-Group", "Intrusion-Set", "Campaign"]

#: The columns a new list shows (``defaultWidgetColumns``), and those one entity type adds
#: (``availableWidgetColumns``). A column's label is OpenCTI's own, shown as it is.
_DEFAULT_COLUMNS = {
    "relationships": [
        {"attribute": "entity_type", "label": "Type"},
        {"attribute": "from_entity_type", "label": "Source type"},
        {"attribute": "from_relationship_type", "label": "Source name"},
        {"attribute": "to_entity_type", "label": "Target type"},
        {"attribute": "to_relationship_type", "label": "Target name"},
        {"attribute": "created", "label": "Original creation date"},
        {"attribute": "created_at", "label": "Platform creation date"},
        {"attribute": "createdBy"},
        {"attribute": "objectMarking"},
    ],
    "entities": [
        {"attribute": "entity_type", "label": "Type"},
        {"attribute": "name", "label": "Name"},
        {"attribute": "created", "label": "Original creation date"},
        {"attribute": "created_at", "label": "Platform creation date"},
        {"attribute": "modified", "label": "Modification date"},
        {"attribute": "createdBy"},
        {"attribute": "creators", "label": "Creators"},
        {"attribute": "x_opencti_workflow_id", "label": "Processing status"},
        {"attribute": "objectLabel"},
        {"attribute": "objectMarking"},
    ],
}
_TYPE_COLUMNS = {
    "Report": [{"attribute": "report_types", "label": "Report type"}, {"attribute": "objectAssignee"}, {"attribute": "objectParticipant"}],
    "Grouping": [{"attribute": "context"}],
    "Malware-Analysis": [{"attribute": "product", "label": "Product"}, {"attribute": "objectAssignee"}],
    "Note": [{"attribute": "note_types", "label": "Note type"}],
    "Case-Incident": [
        {"attribute": "priority"}, {"attribute": "severity", "label": "Severity"}, {"attribute": "response_types", "label": "Response type"},
        {"attribute": "objectAssignee"}, {"attribute": "objectParticipant"},
    ],
    "Case-Rfi": [
        {"attribute": "priority"}, {"attribute": "severity", "label": "Severity"},
        {"attribute": "information_types", "label": "Request for information types"}, {"attribute": "objectAssignee"}, {"attribute": "objectParticipant"},
    ],
    "Case-Rft": [
        {"attribute": "priority"}, {"attribute": "severity", "label": "Severity"},
        {"attribute": "takedown_types", "label": "Request for takedown types"}, {"attribute": "objectAssignee"}, {"attribute": "objectParticipant"},
    ],
    "Task": [{"attribute": "due_date", "label": "Due date"}, {"attribute": "objectAssignee"}, {"attribute": "objectParticipant"}],
    "Incident": [{"attribute": "incident_type"}, {"attribute": "severity", "label": "Severity"}],
    "Indicator": [
        {"attribute": "pattern_type", "label": "Pattern type"}, {"attribute": "valid_from", "label": "Valid from"},
        {"attribute": "valid_until", "label": "Valid until"}, {"attribute": "x_opencti_score"},
    ],
    "Threat-Actor": [{"attribute": "threat_actor_types", "label": "Threat actor types"}],
    "Threat-Actor-Individual": [{"attribute": "threat_actor_types", "label": "Threat actor types"}],
    "Threat-Actor-Group": [{"attribute": "threat_actor_types", "label": "Threat actor types"}],
    "Intrusion-Set": [{"attribute": "resource_level", "label": "Resource level"}],
    "Malware": [{"attribute": "malware_types", "label": "Malware type"}],
    "Channel": [{"attribute": "channel_types", "label": "Channel type"}],
    "Tool": [{"attribute": "tool_types", "label": "Tool types"}],
    "Vulnerability": [
        {"attribute": "x_opencti_cvss_base_score", "label": "CVSS3 - Score"}, {"attribute": "x_opencti_cvss_base_severity", "label": "CVSS3 - Severity"},
        {"attribute": "x_opencti_cvss_v4_base_score", "label": "CVSS4 - Score"}, {"attribute": "x_opencti_cvss_v4_base_severity", "label": "CVSS4 - Severity"},
        {"attribute": "x_opencti_cisa_kev", "label": "CISA - KEV"}, {"attribute": "x_opencti_epss_score", "label": "EPSS Score"},
        {"attribute": "x_opencti_epss_percentile", "label": "EPSS Percentile"},
    ],
    "Attack-Pattern": [{"attribute": "x_mitre_id", "label": "External ID"}],
    "Course-Of-Action": [{"attribute": "x_mitre_id", "label": "External ID"}],
    "Event": [{"attribute": "event_types", "label": "Event type"}],
    "Organization": [{"attribute": "x_opencti_organization_type", "label": "Organization type"}, {"attribute": "x_opencti_score", "label": "Score"}],
}  # fmt: skip

#: OpenCTI's operators for a filter key (``getAvailableOperatorForFilter``), and the one a new filter takes.
_OPERATORS_BY_TYPE = [
    (("date",), ["gt", "gte", "lt", "lte", "nil", "not_nil", "within"]),
    (("integer", "float"), ["gt", "gte", "lt", "lte", "eq", "not_eq"]),
    (("boolean",), ["eq", "not_eq"]),
    (("string",), ["eq", "not_eq", "nil", "not_nil", "contains", "not_contains", "starts_with", "not_starts_with", "ends_with", "not_ends_with", "search"]),
    (("text",), ["search", "nil", "not_nil"]),
]  # fmt: skip
_DEFAULT_OPERATORS = {"date": "within", "integer": "gt", "float": "gt", "boolean": "eq", "string": "starts_with", "text": "search"}


def form() -> dict:
    """OpenCTI's widget editor as a form description (:mod:`.forms`), edited in place."""
    from .forms import (
        INTERVAL_LABELS,
        all_,
        any_,
        attr,
        coalesce,
        concat,
        cond,
        eq,
        field,
        filter_values,
        get,
        gt,
        in_,
        is_array,
        length,
        map_,
        match,
        ne,
        not_,
        options,
        param,
        ref,
        root,
        source,
        template,
        truthy,
        value,
        var,
    )

    def editor_perspectives(widget_type: str) -> list[str]:
        return EDITOR_TYPES[widget_type].get("perspectives", list(PERSPECTIVES))

    perspective = var("perspective")
    relationships = eq(perspective, "relationships")
    audits = eq(perspective, "audits")
    bookmark = eq(root("type"), "bookmark")
    # The types a selection counts: its top-level ``entity_type`` (``relationship_type``) equals filter.
    types_match = match(template("typeKey", perspective=perspective), operator="eq", exact=True)
    empty_group = template("emptyGroup")

    selection_fields = [
        field("text", label="Label", path="label", max=MAX_TEXT, empty="keep", ariaLabel="Label of data selection {n}"),
        field(
            "multiselect",
            id="types",
            label=cond(relationships, "Relationship types", "Entity types"),
            bind={"filterValues": {"group": "filters", "match": types_match, "new": {"key": [param("key")], "values": param("values"), "operator": "eq", "mode": "or"}}},
            options=cond(audits, _AUDIT_TYPES, cond(bookmark, _BOOKMARK_TYPES, map_(source("types"), {"value": var("item.value"), "text": var("item.label")}))),
            placeholder="All types",
            emptyLabel="No type found",
        ),
        # One of the person's saved filters stands for the selection's filters (OpenCTI's "saved filters"); "" is none.
        field(
            "select", label="Saved filter", path="filters_id", default="",
            options=concat(
                [{"value": "", "label": "No saved filter"}],
                map_(coalesce(source("savedFilters"), []), {"value": var("item.value"), "text": var("item.label")}),
            ),
            onChange=[{"if": truthy(value()), "then": [{"set": "filters", "value": template("emptyGroup")}]}],
            when=all_(not_(bookmark), any_(truthy(ref("filters_id")), gt(length(coalesce(source("savedFilters"), [])), 0))),
        ),
        field(
            "filters", label="Filters", path="filters", keys=coalesce(source("keys"), []), hidden=[types_match], perspective=perspective,
            when=all_(not_(bookmark), not_(truthy(ref("filters_id")))),
        ),
        # A relationship's source and target are entities: filtered with the entities' keys.
        field("filters", label="Source filters", path="dynamicFrom", keys=coalesce(source("endKeys"), []), perspective="entities", when=relationships),
        field("filters", label="Target filters", path="dynamicTo", keys=coalesce(source("endKeys"), []), perspective="entities", when=relationships),
        field(
            "row",
            fields=[
                field(
                    "select", label="Date", path="date_attribute", default="created_at",
                    options=template("dates", perspective=perspective, types=var("types")), when=all_(not_(audits), not_(bookmark)),
                ),
                field(
                    "select", label="Counted by", path="attribute", default="entity_type", when=truthy(attr("attribute")),
                    options=cond(
                        relationships, _RELATIONSHIP_ATTRIBUTES,
                        cond(audits, _AUDIT_ATTRIBUTES, cond(
                            all_(gt(length(var("types")), 0), truthy(source("attributes"))),
                            map_(source("attributes"), var("item.value"), where=truthy(var("item.value"))),
                            _COMMON_ATTRIBUTES,
                        )),
                    ),
                ),
                field(
                    "number", label="Number of results", path="number", default=10, min=1, max=cond(eq(root("type"), "list"), 500, 100),
                    empty="null", when=in_(attr("category"), ["distribution", "list"]),
                ),
                field(
                    "select", label="Sort by", path="sort_by", default="created_at", options=_SORT_FIELDS,
                    when=all_(eq(attr("category"), "list"), eq(perspective, "entities")),
                ),
                field(
                    "select", label="Sort order", path="sort_mode", default="desc",
                    options=options(["desc", "asc"], {"desc": "Descending", "asc": "Ascending"}), when=eq(attr("category"), "list"),
                ),
            ],
        ),
        field(
            "switch", label="Display the source", hint="Count the source of the relationships instead of their target.",
            path="isTo", invert=True, when=all_(relationships, truthy(attr("attribute"))),
        ),
        field(
            "switch", label="Distinct", path="unique",
            when=all_(audits, in_(root("type"), ["number", "line", "area"]), not_(truthy(attr("attribute")))),
        ),
        field(
            "row",
            columns=3,
            when=all_(eq(root("type"), "map"), relationships),
            fields=[
                field("number", label="Zoom", path="zoom", default=2, min=0, max=20, empty="remove"),
                # Empty until set, as OpenCTI centres the map itself.
                field("number", label="Center latitude", path="centerLat", min=-90, max=90, empty="remove", decimal=True),
                field("number", label="Center longitude", path="centerLng", min=-180, max=180, empty="remove", decimal=True),
            ],
        ),
        field(
            "columns", path="columns", items="objects", valueKey="attribute", labelKey="label",
            available=template("availableColumns", perspective=perspective, types=var("types")),
            reset=get(template("defaultColumns"), cond(relationships, "relationships", "entities")),
            when=all_(eq(root("type"), "list"), not_(audits)),
        ),
    ]

    root_perspective = ref("perspective")
    fields = [
        field(
            "row",
            fields=[
                field(
                    "select",
                    id="type",
                    label="Widget",
                    path="type",
                    options=options(list(EDITOR_TYPES), _TYPE_LABELS, _TYPE_ICONS),
                    display="tiles",
                    # Another type: selections past its limit dropped, a perspective it does not take changed.
                    onChange=[
                        {"truncate": "dataSelection", "max": attr("limit")},
                        {"if": not_(in_(root_perspective, attr("perspectives"))), "then": [{"change": "perspective", "value": attr("perspectives.0")}]},
                    ],
                ),
                field(
                    "select",
                    id="perspective",
                    label="Perspective",
                    path="perspective",
                    options=map_(attr("perspectives"), {"value": var("item"), "label": get(_PERSPECTIVE_LABELS, var("item"))}),
                    # Each selection on another perspective starts its filters and columns over, its
                    # attribute too, and a date the perspective does not have.
                    onChange=[
                        {
                            "each": "dataSelection",
                            "when": ne(ref("perspective"), value()),
                            "do": [
                                {"set": "perspective", "value": value()},
                                {"set": "filters", "value": empty_group},
                                {"set": "dynamicFrom", "value": empty_group},
                                {"set": "dynamicTo", "value": empty_group},
                                {"set": "columns", "value": cond(eq(value(), "audits"), [], get(template("defaultColumns"), value()))},
                                {"remove": "filters_id"},
                                {"set": "attribute", "value": "entity_type"},
                                {
                                    "set": "date_attribute",
                                    "value": cond(in_(ref("date_attribute"), template("dates", perspective=value(), types=[])), ref("date_attribute"), "created_at"),
                                },
                            ],
                        }
                    ],
                ),
            ],
        ),
        field("text", label="Title", path="parameters.title", max=MAX_TEXT, empty="remove"),
        field(
            "list",
            path="dataSelection",
            title="Data selection {n}",
            removeLabel="Remove data selection {n}",
            min=1,
            max=attr("limit"),
            count="{count} of {max} data selections",
            footerWhen=gt(attr("limit"), 1),
            badge={"text": get(_PERSPECTIVE_LABELS, perspective), "when": gt(var("count"), 1)},
            add=[
                {"label": "Add relationships", "item": template("addedSelection", perspective="relationships"), "when": eq(root_perspective, "relationships")},
                {"label": "Add entities", "item": template("addedSelection", perspective="entities"), "when": eq(root_perspective, "relationships")},
                {
                    "label": "Add a data selection",
                    "item": template("addedSelection", perspective=root_perspective),
                    "when": ne(root_perspective, "relationships"),
                    "disabled": all_(eq(root_perspective, "audits"), eq(attr("category"), "distribution")),
                },
            ],
            vars={
                "perspective": coalesce(ref("perspective"), root("perspective")),
                "types": filter_values(ref("filters"), template("typeKey", perspective=var("perspective")), operator="eq", exact=True),
            },
            sources={
                "types": {"kind": "entity_types", "perspective": perspective, "when": all_(not_(audits), not_(bookmark))},
                "keys": {"kind": "filter_keys", "perspective": perspective, "when": not_(bookmark)},
                "savedFilters": {"kind": "saved_filters", "perspective": perspective, "when": not_(bookmark)},
                "endKeys": {"kind": "filter_keys", "perspective": "entities", "when": relationships},
                "attributes": {
                    "kind": "attributes", "perspective": perspective, "types": var("types"),
                    "when": all_(truthy(attr("attribute")), eq(perspective, "entities"), gt(length(var("types")), 0)),
                },
            },
            fields=selection_fields,
        ),
        field(
            "row",
            fields=[
                field("select", label="Interval", path="parameters.interval", default="day", options=options(INTERVALS, INTERVAL_LABELS), when=eq(attr("category"), "timeseries")),
                field("switch", label="Stacked", path="parameters.stacked", when=truthy(attr("stacked"))),
                field("switch", label="Display legend", path="parameters.legend", when=truthy(attr("legend"))),
                field("switch", label="Distributed", path="parameters.distributed", when=truthy(attr("distributed"))),
            ],
        ),
        field(
            "note",
            text="Activity and history are read by OpenCTI administrators only: XTM One shows this widget as a link to OpenCTI.",
            when=eq(root_perspective, "audits"),
        ),
    ]

    sightings = eq(param("types"), ["stix-sighting-relationship"])
    templates = {
        "emptyGroup": {"mode": "and", "filters": [], "filterGroups": []},
        "typeKey": cond(eq(param("perspective"), "relationships"), "relationship_type", "entity_type"),
        "defaultColumns": _DEFAULT_COLUMNS,
        # The dates a selection may count by, for its perspective and the types it counts.
        "dates": concat(
            ["created_at", "updated_at", "created", "modified"],
            cond(all_(eq(param("perspective"), "relationships"), not_(sightings)), ["start_time", "stop_time"], []),
            cond(any_(eq(param("perspective"), "entities"), sightings), ["first_seen", "last_seen"], []),
        ),
        # The columns a list may show: the defaults, a relationship's dates, the columns of its one entity type.
        "availableColumns": cond(
            eq(param("perspective"), "relationships"),
            concat(get(template("defaultColumns"), "relationships"), [{"attribute": "start_time"}, {"attribute": "stop_time"}]),
            cond(
                eq(param("perspective"), "entities"),
                concat(get(template("defaultColumns"), "entities"), cond(eq(length(param("types")), 1), coalesce(get(_TYPE_COLUMNS, param("types.0")), []), [])),
                [],
            ),
        ),
        # The selection a new widget starts with, once its perspective is chosen.
        "newSelection": {
            "label": "", "number": 10, "sort_by": "created_at", "sort_mode": "desc", "attribute": "entity_type",
            "date_attribute": "created_at", "isTo": True, "perspective": param("perspective"),
            "filters": empty_group, "dynamicFrom": empty_group, "dynamicTo": empty_group,
            "columns": cond(eq(param("perspective"), "audits"), [], get(template("defaultColumns"), param("perspective"))),
        },
        # A selection added after the first, with the keys OpenCTI gives it.
        "addedSelection": {
            "label": "", "attribute": "entity_type", "date_attribute": "created_at", "perspective": param("perspective"),
            "filters": empty_group, "dynamicFrom": empty_group, "dynamicTo": empty_group,
        },
        "widget": {
            "type": param("type"),
            "perspective": param("perspective"),
            "dataSelection": [template("newSelection", perspective=param("perspective"))],
            "parameters": {},
        },
    }

    def operator_rules() -> list[dict]:
        rules = [{"when": in_(var("key.type"), list(types)), "value": operators} for types, operators in _OPERATORS_BY_TYPE]
        rules.append({"when": truthy(var("key.multiple")), "value": ["eq", "not_eq", "only_eq_to", "not_only_eq_to", "nil", "not_nil"]})
        rules.append({"value": ["eq", "not_eq", "nil", "not_nil"]})
        return rules

    return {
        "version": 1,
        "typePath": "type",
        "types": [
            {
                "value": widget_type,
                "label": _TYPE_LABELS[widget_type],
                "attrs": {
                    "limit": spec["limit"],
                    "perspectives": editor_perspectives(widget_type),
                    "category": spec["category"],
                    "attribute": spec.get("attribute", False),
                    "stacked": spec.get("stacked", False),
                    "legend": spec.get("legend", False),
                    "distributed": spec.get("distributed", False),
                },
                # As OpenCTI writes it once its type and perspective are chosen (its first by default).
                "new": template(
                    "widget",
                    type=widget_type,
                    perspective=cond(in_(param("perspective"), editor_perspectives(widget_type)), param("perspective"), editor_perspectives(widget_type)[0]),
                ),
            }
            for widget_type, spec in EDITOR_TYPES.items()
        ],
        "default": {"type": "number"},
        "carries": is_array(root("dataSelection"), of="object", min=1),
        "templates": templates,
        "filters": {
            "keyForm": "list",
            "nested": True,
            "maxDepth": 3,
            "ids": False,
            "newFilterMode": "or",
            "groupMode": True,
            "operators": operator_rules(),
            "defaultOperator": [{"when": eq(var("key.type"), t), "value": op} for t, op in _DEFAULT_OPERATORS.items()] + [{"value": "eq"}],
            "dateInput": "text",
            "dateHint": "A date, or relative to now: now-7d",
            # Values listed by the platform, except text compared as text.
            "searched": not_(in_(var("type"), ["string", "text"])),
            # "In regards of": an entity and the relationship types to it, each a value ``{key, values}``.
            "pairs": {
                "regardingOf": [
                    {"key": "id", "label": "Entities", "search": "regardingOf.id"},
                    {"key": "relationship_type", "label": "Relationship types", "search": "regardingOf.relationship_type"},
                ]
            },
        },
        "fields": fields,
    }


__all__ = ["EDITOR_TYPES", "INTERVALS", "KEY_RE", "OPERATORS", "PERSPECTIVES", "WIDGET_TYPES", "check_native", "form"]
