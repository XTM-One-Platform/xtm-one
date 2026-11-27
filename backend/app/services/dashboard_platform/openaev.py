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

"""OpenAEV custom dashboard widgets kept natively in an XTM One dashboard.

The configuration is an OpenAEV widget without its id and layout:
``widget_type`` and ``widget_config``, the Jackson-polymorphic configuration
keyed on ``widget_configuration_type`` (``openaev-model/.../engine/api``):
``flat`` and ``average`` (a figure), ``temporal-histogram`` (a series over
time), ``structural-histogram`` (a breakdown by a field) and ``list`` (rows).

OpenAEV silently coerces an unknown filter operator or mode to ``eq`` /
``and`` (v3), so a typo would return the wrong rows: operators and modes are
checked here, with the list the XTM One OpenAEV tools already enforce.

A widget of an OpenAEV dashboard may filter on the dashboard's own parameters
(the simulation or scenario it is opened on), by putting the parameter's id in
a filter's values. Such a reference means nothing outside that dashboard, so
the importer (``dashboard_interop.openaev``) resolves or drops it; a stored
widget never holds one.
"""

from __future__ import annotations

from app.integrations.providers.openaev._common import FILTER_OPERATORS

from . import NativeCheck, NativeError
from ._filters import check_filter_group, check_key

OPERATORS: frozenset[str] = frozenset(FILTER_OPERATORS)

CONFIG_TYPES: tuple[str, ...] = ("flat", "average", "temporal-histogram", "structural-histogram", "list")

#: Widget type -> (configuration types accepted, max series, drawn by XTM One).
WIDGET_TYPES: dict[str, tuple[tuple[str, ...], int, bool]] = {
    "number": (("flat",), 1, True),
    # Per-security-domain averages: an outcome per domain, its expectation types as parts.
    "average": (("average",), 1, True),
    "line": (("temporal-histogram",), 5, True),
    "vertical-barchart": (("temporal-histogram", "structural-histogram"), 5, True),
    "horizontal-barchart": (("structural-histogram",), 2, True),
    "donut": (("structural-histogram",), 1, True),
    "list": (("list",), 1, True),
    # Its security posture widgets: SUCCESS / FAILED (/ PENDING) series broken down by a field.
    "security-coverage": (("structural-histogram",), 2, True),
    "attack-path": (("structural-histogram",), 2, True),
    "exposure-score": (("structural-histogram",), 2, True),
    "posture-radar": (("structural-histogram",), 5, True),
    # OpenAEV's own home dashboard gives it three series (SUCCESS, FAILED, PENDING).
    "command-center": (("structural-histogram",), 3, True),
    "resilience-gauge": (("structural-histogram",), 1, True),
}

_SHAPES = {
    "flat": "number",
    "average": "number",
    "temporal-histogram": "series",
    "structural-histogram": "distribution",
    "list": "rows",
}

#: The security posture widgets' data: validation outcomes by label, or a posture radar's series.
TYPE_SHAPES: dict[str, str] = {
    "command-center": "outcomes", "exposure-score": "outcomes", "resilience-gauge": "outcomes", "average": "outcomes",
    "security-coverage": "outcomes", "attack-path": "outcomes", "posture-radar": "series",
}  # fmt: skip

#: How XTM One draws each type the way OpenAEV does.
PREFERRED_VIEWS: dict[str, str] = {
    "donut": "donut", "horizontal-barchart": "hbar", "vertical-barchart": "bar", "line": "line",
    "list": "list", "number": "number", "command-center": "exposureconsole", "exposure-score": "scoregauge",
    "resilience-gauge": "ringgauge", "average": "domaincards", "security-coverage": "outcomematrix",
    "attack-path": "pathgraph", "posture-radar": "radar",
}  # fmt: skip

TIME_RANGES: tuple[str, ...] = (
    "DEFAULT", "ALL_TIME", "CUSTOM", "LAST_DAY", "LAST_WEEK", "LAST_MONTH", "LAST_QUARTER", "LAST_SEMESTER", "LAST_YEAR",
)  # fmt: skip
INTERVALS: tuple[str, ...] = ("year", "quarter", "month", "week", "day", "hour")
MAX_TEXT = 200

_COMMON = {"widget_configuration_type", "title", "start", "end", "time_range", "date_attribute"}
_ALLOWED: dict[str, set[str]] = {
    "flat": _COMMON | {"series"},
    "average": _COMMON | {"series"},
    "temporal-histogram": _COMMON | {"series", "mode", "interval", "stacked", "display_legend"},
    "structural-histogram": _COMMON | {"series", "mode", "field", "limit", "stacked", "display_legend"},
    # OpenAEV exports a list with an empty ``series`` (inherited): accepted when empty.
    "list": _COMMON | {"perspective", "columns", "sorts", "limit", "series"},
}


def _text(value: object, where: str, *, limit: int = MAX_TEXT) -> str:
    if not isinstance(value, str) or len(value) > limit or "\x00" in value:
        raise NativeError(f"{where} must be text of at most {limit} characters.")
    return value


def _limit(value: object, where: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 1000:
        raise NativeError(f"{where} must be a whole number between 1 and 1000.")
    return value


def _series(value: object, where: str, max_series: int) -> list[dict]:
    if not isinstance(value, list) or not 1 <= len(value) <= max_series:
        raise NativeError(f"{where} must hold 1 to {max_series} series.")
    out = []
    for index, item in enumerate(value):
        at = f"{where}[{index}]"
        if not isinstance(item, dict) or set(item) - {"name", "filter"}:
            raise NativeError(f"{at} must be an object with 'name' and 'filter'.")
        out.append(
            {
                "name": _text(item.get("name", ""), f"{at}.name"),
                "filter": check_filter_group(item.get("filter"), style="openaev", operators=OPERATORS, where=f"{at}.filter"),
            }
        )
    return out


def check_native(native: object) -> NativeCheck:
    """Validate an OpenAEV widget configuration (no id, no layout)."""
    if not isinstance(native, dict):
        raise NativeError("An OpenAEV widget must be an object with 'widget_type' and 'widget_config'.")
    unknown = set(native) - {"widget_type", "widget_config", "widget_id", "widget_layout"}
    if unknown:
        raise NativeError(f"The OpenAEV widget has unknown keys: {', '.join(sorted(unknown))}.")
    widget_type = native.get("widget_type")
    if widget_type not in WIDGET_TYPES:
        raise NativeError(f"OpenAEV widget_type must be one of: {', '.join(WIDGET_TYPES)}.")
    config_types, max_series, drawn = WIDGET_TYPES[widget_type]
    config = native.get("widget_config")
    if not isinstance(config, dict):
        raise NativeError("widget_config must be an object.")
    config_type = config.get("widget_configuration_type")
    if config_type not in config_types:
        raise NativeError(
            f"A {widget_type} widget needs widget_config.widget_configuration_type "
            f"{' or '.join(repr(t) for t in config_types)}."
        )
    extra = set(config) - _ALLOWED[config_type]
    if extra:
        raise NativeError(f"widget_config has unknown keys for {config_type}: {', '.join(sorted(extra))}.")
    out: dict = {"widget_configuration_type": config_type}
    if config.get("title"):
        out["title"] = _text(config["title"], "widget_config.title")
    time_range = config.get("time_range", "DEFAULT")
    if time_range not in TIME_RANGES:
        raise NativeError(f"widget_config.time_range must be one of: {', '.join(TIME_RANGES)}.")
    out["time_range"] = time_range
    out["date_attribute"] = check_key(config.get("date_attribute", "base_created_at"), "widget_config.date_attribute")
    for key in ("start", "end"):
        if config.get(key) is not None:
            out[key] = _text(config[key], f"widget_config.{key}", limit=40)
    if config_type == "list":
        if config.get("series"):
            raise NativeError("A list widget reads its rows from widget_config.perspective, not from series.")
        perspective = config.get("perspective") or {}
        if not isinstance(perspective, dict) or set(perspective) - {"name", "filter"}:
            raise NativeError("widget_config.perspective must be an object with 'name' and 'filter'.")
        out["perspective"] = {
            "name": _text(perspective.get("name", ""), "widget_config.perspective.name"),
            "filter": check_filter_group(
                perspective.get("filter"), style="openaev", operators=OPERATORS, where="widget_config.perspective.filter"
            ),
        }
        columns = config.get("columns", [])
        if not isinstance(columns, list) or len(columns) > 30:
            raise NativeError("widget_config.columns must be a list of at most 30 attribute names.")
        out["columns"] = [check_key(c, f"widget_config.columns[{i}]") for i, c in enumerate(columns)]
        sorts = config.get("sorts", [])
        if not isinstance(sorts, list) or len(sorts) > 5:
            raise NativeError("widget_config.sorts must be a list of at most 5 sorts.")
        out["sorts"] = []
        for index, sort in enumerate(sorts):
            at = f"widget_config.sorts[{index}]"
            if not isinstance(sort, dict) or set(sort) - {"fieldName", "direction"}:
                raise NativeError(f"{at} must be an object with 'fieldName' and 'direction'.")
            direction = sort.get("direction", "DESC")
            if direction not in ("ASC", "DESC"):
                raise NativeError(f"{at}.direction must be 'ASC' or 'DESC'.")
            out["sorts"].append({"fieldName": check_key(sort.get("fieldName"), f"{at}.fieldName"), "direction": direction})
        out["limit"] = _limit(config.get("limit", 100), "widget_config.limit")
    else:
        out["series"] = _series(config.get("series"), "widget_config.series", max_series)
    if config_type == "temporal-histogram":
        out["mode"] = "temporal"
        interval = config.get("interval", "week")
        if interval not in INTERVALS:
            raise NativeError(f"widget_config.interval must be one of: {', '.join(INTERVALS)}.")
        out["interval"] = interval
    if config_type == "structural-histogram":
        out["mode"] = "structural"
        out["field"] = check_key(config.get("field"), "widget_config.field")
        out["limit"] = _limit(config.get("limit", 100), "widget_config.limit")
    if config_type in ("temporal-histogram", "structural-histogram"):
        out["stacked"] = bool(config.get("stacked", False))
        out["display_legend"] = bool(config.get("display_legend", False))
    return NativeCheck(
        native={"widget_type": widget_type, "widget_config": out},
        shape=TYPE_SHAPES.get(widget_type, _SHAPES[config_type]) if drawn else None,  # type: ignore[arg-type]
        native_type=widget_type,
        title=out.get("title"),
        preferred_view=PREFERRED_VIEWS.get(widget_type),
    )


# ── What an agent reads before writing a widget (``describe_widget``) ──────


def _entity(entity: str) -> dict:
    return {"key": "base_entity", "operator": "eq", "mode": "or", "values": [entity]}


def _example(widget_type: str) -> dict:
    """A configuration of *widget_type* OpenAEV's editor could have written: valid, and a sensible start."""
    spec = EDITOR_TYPES[widget_type]
    config_type = spec["configs"][0]
    config: dict = {"widget_configuration_type": config_type, "title": "", "date_attribute": "base_created_at", "time_range": "DEFAULT"}
    if spec["kind"] == "perspective":
        config.update(perspective={"name": "Findings", "filter": {"mode": "and", "filters": [_entity("finding")]}},
                      columns=["finding_type", "base_created_at"], sorts=[{"fieldName": "base_created_at", "direction": "DESC"}], limit=20)  # fmt: skip
    elif spec["kind"] == "coverage":
        config["series"] = [
            {"name": status, "filter": {"mode": "and", "filters": [
                _entity("expectation-inject"),
                {"key": "inject_expectation_status", "operator": "eq", "mode": "or", "values": [status]},
                {"key": "inject_expectation_type", "operator": "eq", "mode": "or", "values": ["DETECTION"]},
            ]}}
            for status in ("SUCCESS", "FAILED")
        ]  # fmt: skip
    elif spec["kind"] == "domains":
        config["series"] = [{"name": "", "filter": {"mode": "and", "filters": [_entity("expectation-inject")]}}]
    else:
        entity = "expectation-inject" if widget_type in ("command-center", "exposure-score", "resilience-gauge", "posture-radar") else "finding"
        config["series"] = [{"name": "", "filter": {"mode": "and", "filters": [_entity(entity)]}}]
    if config_type == "temporal-histogram":
        config.update(mode="temporal", interval="week")
    if config_type == "structural-histogram":
        config["mode"] = "structural"
        config["field"] = (
            "base_attack_patterns_side" if spec["kind"] == "coverage"
            else "inject_expectation_status" if config["series"][0]["filter"]["filters"][0]["values"] == ["expectation-inject"]
            else "finding_type"
        )  # fmt: skip
        if spec["limit"]:
            config["limit"] = 10
    return {"widget_type": widget_type, "widget_config": config}


def describe(widget_type: str | None = None) -> dict:
    """OpenAEV's widget types, or one of them with an example configuration and the rules it follows."""
    if widget_type is None:
        return {
            "types": [
                {"type": t, "label": _TYPE_LABELS.get(t), "configurations": spec["configs"], "series": f"1-{spec['maxSeries']}"}
                for t, spec in EDITOR_TYPES.items()
            ]
        }
    if widget_type not in EDITOR_TYPES:
        raise NativeError(f"OpenAEV widget_type must be one of: {', '.join(EDITOR_TYPES)}.")
    spec = EDITOR_TYPES[widget_type]
    return {
        "type": widget_type,
        "label": _TYPE_LABELS.get(widget_type),
        "configurations": spec["configs"],
        "series": f"1-{spec['maxSeries']}",
        "simulationRequired": spec.get("simulationRequired", False),
        "example": _example(widget_type),
        "rules": [
            "A series is {name, filter}; its filter's base_entity says what is counted: " + ", ".join(EDITOR_ENTITIES) + ".",
            "A filter group is {mode: and|or, filters: [{key, operator, mode, values}]}, without nested groups; "
            f"operators: {', '.join(sorted(OPERATORS))}.",
            "Filter keys by entity: " + "; ".join(f"{e}: {', '.join(keys)}" for e, keys in _FILTER_KEYS.items()) + ".",
            f"time_range: {', '.join(TIME_RANGES)}; a temporal histogram's interval: {', '.join(INTERVALS)}; a "
            "structural histogram's field is the attribute it breaks down by.",
            "A simulation or scenario input is read as '$token$' in a base_simulation_side / base_scenario_side value.",
        ],
    }


# ── The widget editor (``forms``): OpenAEV's own, ``openaev-front/.../custom_dashboards/widgets/configuration/*`` ──

#: Widget type -> what OpenAEV's editor offers for it (``WidgetUtils.tsx``), in its order: its
#: configuration types, series at most, how its data is chosen (generic ``series``, one list
#: ``perspective``, or the fixed series of a ``coverage`` / ``domains`` widget), whether a breakdown
#: takes a number of results, the breakdown fields it restricts to, whether a simulation is required.
EDITOR_TYPES: dict[str, dict] = {
    "command-center": {"configs": ["structural-histogram"], "maxSeries": 3, "kind": "series", "limit": True},
    "exposure-score": {"configs": ["structural-histogram"], "maxSeries": 2, "kind": "series", "limit": True},
    "resilience-gauge": {"configs": ["structural-histogram"], "maxSeries": 1, "kind": "series", "limit": True},
    "posture-radar": {"configs": ["structural-histogram"], "maxSeries": 5, "kind": "series", "limit": True},
    "average": {"configs": ["average"], "maxSeries": 1, "kind": "domains", "limit": False},
    "security-coverage": {"configs": ["structural-histogram"], "maxSeries": 2, "kind": "coverage", "limit": False},
    "attack-path": {"configs": ["structural-histogram"], "maxSeries": 2, "kind": "coverage", "limit": False, "simulationRequired": True},
    "vertical-barchart": {"configs": ["structural-histogram", "temporal-histogram"], "maxSeries": 5, "kind": "series", "limit": True},
    "horizontal-barchart": {"configs": ["structural-histogram"], "maxSeries": 2, "kind": "series", "limit": True},
    "line": {"configs": ["temporal-histogram"], "maxSeries": 5, "kind": "series", "limit": False},
    "donut": {"configs": ["structural-histogram"], "maxSeries": 1, "kind": "series", "limit": True},
    "list": {"configs": ["list"], "maxSeries": 1, "kind": "perspective", "limit": False},
    "number": {"configs": ["flat"], "maxSeries": 1, "kind": "series", "limit": False},
}  # fmt: skip
_COVERAGE_FIELDS = ["base_attack_patterns_side", "base_created_at", "base_updated_at"]

_TYPE_LABELS = {
    "command-center": "Command Center", "exposure-score": "Exposure Score", "resilience-gauge": "Resilience Gauge",
    "posture-radar": "Posture Radar", "average": "Security Domains", "security-coverage": "Security Coverage",
    "attack-path": "Attack Paths", "vertical-barchart": "Vertical bars", "horizontal-barchart": "Horizontal bars",
    "line": "Line", "donut": "Donut", "list": "List", "number": "Number",
}  # fmt: skip
#: Each type's tile in the editor, as a Lucide icon.
_TYPE_ICONS = {
    "command-center": "orbit", "exposure-score": "gauge", "resilience-gauge": "shield-check", "posture-radar": "radar",
    "average": "layout-grid", "security-coverage": "grid-3x3", "attack-path": "route", "vertical-barchart": "chart-column",
    "horizontal-barchart": "chart-bar", "line": "chart-line", "donut": "chart-pie", "list": "list", "number": "hash",
}  # fmt: skip
_TIME_RANGE_LABELS = {
    "DEFAULT": "Dashboard time range", "ALL_TIME": "All time", "CUSTOM": "Custom range", "LAST_DAY": "Last 24 hours",
    "LAST_WEEK": "Last 7 days", "LAST_MONTH": "Last month", "LAST_QUARTER": "Last 3 months",
    "LAST_SEMESTER": "Last 6 months", "LAST_YEAR": "Last year",
}  # fmt: skip

#: The engine entities its editor offers (``FilterFieldBaseEntity``), and the filter keys it offers
#: for each (``AuthorizedPerspectives``).
EDITOR_ENTITIES = ["expectation-inject", "finding", "asset", "vulnerable-endpoint", "inject", "scenario", "simulation"]
_FILTER_KEYS = {
    "expectation-inject": [
        "base_created_at", "inject_expectation_status", "inject_expectation_type", "base_updated_at", "base_simulation_side",
        "base_scenario_side", "base_asset_side", "base_asset_group_side", "base_security_platforms_side",
    ],
    "finding": ["base_created_at", "finding_type", "base_updated_at", "base_endpoint_side", "base_simulation_side", "base_scenario_side"],
    "asset": ["base_created_at", "asset_category", "endpoint_arch", "endpoint_platform", "asset_ips", "asset_hostname", "base_simulation_side", "base_scenario_side"],
    "vulnerable-endpoint": [
        "base_created_at", "vulnerable_endpoint_architecture", "vulnerable_endpoint_agents_active_status",
        "vulnerable_endpoint_agents_privileges", "vulnerable_endpoint_platform", "base_simulation_side", "base_scenario_side",
    ],
    "inject": [
        "base_created_at", "inject_status", "base_tags_side", "base_assets_side", "base_asset_groups_side", "base_teams_side",
        "base_platforms_side_denormalized", "base_simulation_side", "base_scenario_side",
    ],
    "simulation": [
        "base_created_at", "status", "base_tags_side", "base_assets_side", "base_asset_groups_side", "base_teams_side",
        "base_platforms_side_denormalized", "base_scenario_side",
    ],
    "scenario": [
        "base_created_at", "status", "base_tags_side", "base_assets_side", "base_asset_groups_side", "base_teams_side",
        "base_platforms_side_denormalized",
    ],
}  # fmt: skip
#: A security domains widget's series is narrowed by a simulation or a scenario only.
_DOMAIN_KEYS = ["base_simulation_side", "base_scenario_side"]
_NUMERIC = ["double", "float", "integer", "int", "long", "short", "number", "bigdecimal"]


def _new_widget(widget_type: str) -> dict:
    """The widget OpenAEV writes for *widget_type*, its series of the ``entity`` parameter."""
    from .forms import param, template

    spec = EDITOR_TYPES[widget_type]
    config_type = spec["configs"][0]
    config: dict = {"widget_configuration_type": config_type, "title": "", "date_attribute": "base_created_at", "time_range": "DEFAULT"}
    if spec["kind"] == "perspective":
        config["perspective"] = template("series", entity=param("entity"))
    elif spec["kind"] == "coverage":
        config["series"] = template("coverageSeries", expectation="DETECTION", simulation=None)
    elif spec["kind"] == "domains":
        config["series"] = [template("series", entity="expectation-inject", mode="or")]
    else:
        config["series"] = [template("series", entity=param("entity"))]
    if config_type == "temporal-histogram":
        config.update(mode="temporal", interval="week")
    if config_type == "structural-histogram":
        config["mode"] = "structural"
        config["field"] = "base_attack_patterns_side" if spec["kind"] == "coverage" else ""
        if spec["limit"]:
            config["limit"] = 10
    return {"widget_type": widget_type, "widget_config": config}


def form() -> dict:
    """OpenAEV's widget editor as a form description (:mod:`.forms`), edited in place."""
    from .forms import (
        INTERVAL_LABELS,
        absent,
        all_,
        any_,
        attr,
        coalesce,
        concat,
        cond,
        either,
        ends_with,
        eq,
        field,
        filter_values,
        get,
        gt,
        in_,
        include_if,
        is_array,
        length,
        lower,
        map_,
        match,
        named,
        ne,
        not_,
        old,
        old_attr,
        options,
        param,
        ref,
        root,
        source,
        template,
        truthy,
        unique,
        uuid4,
        value,
        var,
    )

    config_type = ref("widget_config.widget_configuration_type")
    kind = attr("kind")
    props = source("props")
    base_entity = match("base_entity")

    def entity_of(group: object) -> dict:
        return get(filter_values(group, "base_entity"), 0)

    # One series (or a list's perspective): its label, its entity, its filters.
    series_scope = {
        "vars": {"entity": coalesce(entity_of(ref("filter")), "")},
        "sources": {"keys": {"kind": "filter_keys", "types": [var("entity")], "when": truthy(var("entity"))}},
        "fields": [
            field(
                "row",
                fields=[
                    field("text", label="Label", path="name", max=MAX_TEXT, empty="keep", ariaLabel="Label of series {n}"),
                    field(
                        "select",
                        id="entity",
                        label="Entity type",
                        default="",
                        options=EDITOR_ENTITIES,
                        # Another entity: its filters start over (each entity has its own keys); the entity filter keeps its id.
                        bind={
                            "filterValues": {
                                "group": "filter",
                                "match": base_entity,
                                "single": True,
                                "clearOthers": True,
                                "new": template("entityFilter", entity=get(param("values"), 0)),
                            }
                        },
                    ),
                ],
            ),
            field(
                "filters",
                label="Filters",
                path="filter",
                hidden=[base_entity],
                keys=map_(
                    source("keys"),
                    where=all_(
                        any_(not_(truthy(var("item.entity"))), eq(var("item.entity"), var("entity"))),
                        in_(var("item.key"), coalesce(get(template("filterKeys"), var("entity")), [])),
                    ),
                ),
            ),
        ],
    }

    histogram = in_(config_type, ["temporal-histogram", "structural-histogram"])
    structural = eq(config_type, "structural-histogram")
    fields = [
        field(
            "select",
            id="type",
            label="Widget",
            path="widget_type",
            options=options(list(EDITOR_TYPES), _TYPE_LABELS, _TYPE_ICONS),
            display="tiles",
            # Another type: its data is kept where the two types read it the same way, otherwise it starts over.
            onChange=[
                {
                    "if": any_(absent(old_attr("kind")), ne(old_attr("kind"), kind), ne(kind, "series")),
                    "then": [
                        {
                            "rebuild": {
                                "entity": either(entity_of(coalesce(old("widget_config.series.0.filter"), old("widget_config.perspective.filter"))), "finding")
                            },
                            "carry": [f"widget_config.{key}" for key in ("title", "time_range", "start", "end", "date_attribute")],
                        }
                    ],
                    # Series past the new type's limit are dropped; a breakdown keeps its field.
                    "else": [{"change": "mode", "value": cond(in_(config_type, attr("configs")), config_type, attr("configs.0"))}],
                }
            ],
        ),
        field("text", label="Title", path="widget_config.title", max=MAX_TEXT, empty="keep"),
        field(
            "list",
            path="widget_config.series",
            when=eq(kind, "series"),
            title="Series {n}",
            removeLabel="Remove series {n}",
            min=1,
            max=attr("maxSeries"),
            count="{count} of {max} series",
            footerWhen=gt(attr("maxSeries"), 1),
            add=[{"label": "Add a series", "item": template("series", entity=either(var("entities.0"), "finding"))}],
            **series_scope,
        ),
        field("section", path="widget_config.perspective", title="Rows listed", when=all_(eq(kind, "perspective"), truthy(ref("widget_config.perspective"))), **series_scope),
        # Security coverage and attack paths: detection or prevention, and the simulation; the series follow.
        field(
            "compose",
            path="widget_config.series",
            when=eq(kind, "coverage"),
            params={
                "expectation": cond(
                    eq(get(filter_values(ref("widget_config.series.0.filter"), "inject_expectation_type"), 0), "PREVENTION"), "PREVENTION", "DETECTION"
                ),
                "simulation": coalesce(get(filter_values(ref("widget_config.series.0.filter"), "base_simulation_side"), 0), None),
            },
            build=template("coverageSeries", expectation=param("expectation"), simulation=param("simulation")),
            fields=[
                field(
                    "row",
                    fields=[
                        field(
                            "segmented",
                            label="Expectation",
                            bind={"param": "expectation"},
                            options=options(["DETECTION", "PREVENTION"], {"DETECTION": "Detection", "PREVENTION": "Prevention"}),
                        ),
                        field(
                            "values",
                            label=cond(truthy(attr("simulationRequired")), "Simulation", "Simulation (optional)"),
                            hint={"text": "An attack path needs a simulation.", "when": all_(truthy(attr("simulationRequired")), not_(truthy(param("simulation"))))},
                            bind={"param": "simulation"},
                            filterKey="base_simulation_side",
                            single=True,
                        ),
                    ],
                )
            ],
        ),
        field(
            "filters",
            label="Filters",
            path="widget_config.series.0.filter",
            when=all_(eq(kind, "domains"), truthy(ref("widget_config.series.0"))),
            hidden=[base_entity],
            keys=map_(props, where=in_(var("item.key"), _DOMAIN_KEYS)),
        ),
        field(
            "row",
            fields=[
                field(
                    "select",
                    label="Time range",
                    path="widget_config.time_range",
                    default="DEFAULT",
                    options=options(list(TIME_RANGES), _TIME_RANGE_LABELS),
                    onChange=[{"if": ne(value(), "CUSTOM"), "then": [{"remove": "widget_config.start"}, {"remove": "widget_config.end"}]}],
                ),
                field(
                    "select",
                    label="Date",
                    path="widget_config.date_attribute",
                    default="base_created_at",
                    options=named(unique(concat(["base_created_at"], map_(props, var("item.key"), where=eq(var("item.type"), "instant")))), props),
                ),
                field("day", label="Start", path="widget_config.start", when=eq(ref("widget_config.time_range"), "CUSTOM")),
                field("day", label="End", path="widget_config.end", when=eq(ref("widget_config.time_range"), "CUSTOM")),
                field(
                    "segmented",
                    id="mode",
                    label="Mode",
                    path="widget_config.widget_configuration_type",
                    when=gt(length(attr("configs")), 1),
                    options=options(["structural-histogram", "temporal-histogram"], {"structural-histogram": "Breakdown", "temporal-histogram": "Over time"}),
                    # Structural or temporal: the keys of the other mode go; a number has no mode, breakdown, interval or switches.
                    onChange=[
                        {"truncate": "widget_config.series", "max": coalesce(attr("maxSeries"), 5)},
                        {
                            "if": eq(value(), "temporal-histogram"),
                            "then": [
                                {"set": "widget_config.mode", "value": "temporal"},
                                {"set": "widget_config.interval", "value": coalesce(ref("widget_config.interval"), "week")},
                                {"remove": "widget_config.field"},
                                {"remove": "widget_config.limit"},
                            ],
                            "else": [
                                {
                                    "if": eq(value(), "structural-histogram"),
                                    "then": [
                                        {"set": "widget_config.mode", "value": "structural"},
                                        {"remove": "widget_config.interval"},
                                        {"set": "widget_config.field", "value": coalesce(ref("widget_config.field"), "")},
                                        {"set": "widget_config.limit", "value": cond(truthy(attr("limit")), coalesce(ref("widget_config.limit"), 10), ref("widget_config.limit"))},
                                    ],
                                    "else": [{"remove": f"widget_config.{key}"} for key in ("mode", "field", "limit", "interval", "stacked", "display_legend")],
                                }
                            ],
                        },
                    ],
                ),
                field(
                    "select",
                    label="Interval",
                    path="widget_config.interval",
                    default="day",
                    options=concat(
                        options(["day", "week", "month", "quarter", "year"], INTERVAL_LABELS),
                        cond(eq(ref("widget_config.interval"), "hour"), options(["hour"], INTERVAL_LABELS), []),
                    ),
                    when=eq(config_type, "temporal-histogram"),
                ),
                field(
                    "select",
                    label="Breakdown by",
                    path="widget_config.field",
                    default="",
                    when=all_(structural, ne(kind, "coverage")),
                    options=named(cond(truthy(attr("fields")), attr("fields"), unique(map_(props, var("item.key"), where=ne(var("item.type"), "instant")))), props),
                ),
                field(
                    "number", label="Number of results", path="widget_config.limit", default=10, min=1, max=100, empty="default",
                    when=all_(structural, truthy(attr("limit"))),
                ),
                field(
                    "number", label="Number of results", path="widget_config.limit", default=100, min=1, max=1000, empty="default",
                    when=eq(config_type, "list"),
                ),
            ],
        ),
        field(
            "row",
            when=histogram,
            fields=[
                field("switch", label="Stacked", path="widget_config.stacked"),
                field("switch", label="Display legend", path="widget_config.display_legend"),
            ],
        ),
        field(
            "columns",
            path="widget_config.columns",
            when=eq(config_type, "list"),
            items="strings",
            available=unique(
                map_(props, var("item.key"), where=any_(not_(ends_with(var("item.key"), "_side")), in_(var("item.key"), ["base_tags_side", "base_attack_patterns_side"])))
            ),
            names=map_(props, {"value": var("item.key"), "text": var("item.label")}),
            reset=[],
        ),
        field("note", text="With no column chosen, OpenAEV shows them all.", when=all_(eq(config_type, "list"), eq(length(ref("widget_config.columns")), 0))),
        field(
            "sorts",
            label="Sort",
            path="widget_config.sorts",
            when=eq(config_type, "list"),
            fieldKey="fieldName",
            directionKey="direction",
            choices=named(unique(map_(props, var("item.key"))), props),
            directions=options(["DESC", "ASC"], {"DESC": "Descending", "ASC": "Ascending"}),
            new={"fieldName": "base_created_at", "direction": "DESC"},
            max=1,
        ),
    ]

    def status_filter(key: str, filter_value: object) -> dict:
        return {"id": uuid4(), "key": key, "mode": "and", "operator": "eq", "values": [filter_value]}

    templates = {
        "entityFilter": {"id": uuid4(), "key": "base_entity", "mode": coalesce(param("mode"), "and"), "values": [param("entity")], "operator": "eq"},
        "series": {"name": "", "filter": {"mode": "and", "filters": [template("entityFilter", entity=param("entity"), mode=param("mode"))]}},
        # The fixed series of a security coverage or attack path widget (``getSeries``).
        "coverageSeries": [
            {
                "name": status,
                "filter": {
                    "mode": "and",
                    "filters": [
                        status_filter("base_entity", "expectation-inject"),
                        status_filter("inject_expectation_status", status),
                        status_filter("inject_expectation_type", param("expectation")),
                        include_if(truthy(param("simulation")), status_filter("base_simulation_side", param("simulation"))),
                    ],
                },
            }
            for status in ("SUCCESS", "FAILED")
        ],
        "filterKeys": _FILTER_KEYS,
    }

    def type_attrs(widget_type: str) -> dict:
        spec = EDITOR_TYPES[widget_type]
        return {
            "configs": spec["configs"],
            "maxSeries": spec["maxSeries"],
            "kind": spec["kind"],
            "limit": spec["limit"],
            "fields": _COVERAGE_FIELDS if spec["kind"] == "coverage" else None,
            "simulationRequired": spec.get("simulationRequired", False),
        }

    lowered = lower(var("key.type"))
    return {
        "version": 1,
        "typePath": "widget_type",
        "types": [
            {"value": t, "label": _TYPE_LABELS[t], "attrs": type_attrs(t), "new": _new_widget(t)} for t in EDITOR_TYPES
        ],
        "default": {"type": "number", "params": {"entity": "finding"}},
        "carries": all_(
            in_(root("widget_config.widget_configuration_type"), attr("configs")),
            any_(absent(root("widget_config.series")), is_array(root("widget_config.series"))),
        ),
        "templates": templates,
        "filters": {
            "keyForm": "string",
            "nested": False,
            "maxDepth": 1,
            "ids": True,
            "newFilterMode": "and",
            "groupMode": False,
            # OpenAEV's operators for a property (``FilterUtils.tsx``).
            "operators": [
                {"when": gt(length(var("key.operators")), 0), "value": var("key.operators")},
                {"when": eq(lowered, "instant"), "value": ["gt", "gte", "lt", "lte", "empty", "not_empty"]},
                {"when": in_(lowered, _NUMERIC), "value": ["gte", "lte", "gt", "lt", "eq", "not_eq", "empty", "not_empty"]},
                {
                    "when": all_(not_(truthy(var("key.multiple"))), any_(gt(length(var("key.values")), 0), truthy(var("key.dynamic")))),
                    "value": ["eq", "not_eq", "empty", "not_empty"],
                },
                {"when": truthy(var("key.multiple")), "value": ["contains", "not_contains", "empty", "not_empty"]},
                {"value": ["eq", "not_eq", "contains", "not_contains", "starts_with", "not_starts_with"]},
            ],
            "dateInput": "datetime",
            # Values searched in OpenAEV (``useSearchOptions``).
            "searched": truthy(var("key.dynamic")),
        },
        "vars": {
            "entities": unique(
                map_(cond(eq(kind, "perspective"), [ref("widget_config.perspective")], coalesce(ref("widget_config.series"), [])), entity_of(var("item.filter")))
            ),
        },
        "sources": {"props": {"kind": "filter_keys", "types": var("entities"), "when": gt(length(var("entities")), 0)}},
        "fields": fields,
    }


__all__ = ["CONFIG_TYPES", "EDITOR_TYPES", "INTERVALS", "OPERATORS", "TIME_RANGES", "WIDGET_TYPES", "check_native", "form"]
