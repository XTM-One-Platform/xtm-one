# SPDX-License-Identifier: Apache-2.0

"""OpenAEV analytics tools - ad-hoc widget queries and saved dashboards.

``query_openaev_analytics`` drives ``POST /api/dashboards/adhoc/*``, the v3
endpoints where the CALLER supplies a full widget configuration inline instead
of referencing a stored widget. That makes them the most useful analytics
surface for an agent: any aggregate the OpenAEV dashboards can draw can be
asked for directly, without an admin first creating a widget.

``manage_openaev_dashboard`` covers the saved custom dashboards and their
widgets: create, update, move and delete a widget, and run a stored widget by
id. OpenAEV's widget PUT replaces the widget whole, so an update reads the
stored widget and carries over what the call does not name.

The widget configuration is passed through as-is (it is an engine-level
structure that evolves with the platform), but the entity type and the filter
group inside it are validated first: OpenAEV silently coerces an unknown filter
operator to ``eq``, so an unvalidated config returns plausible-looking numbers
that answer the wrong question.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from app.tools.base import CallableToolExecutor, ToolDefinition

from ._common import _api, _search_body, _validate_filter_group, json_answer
from .engine_schema import engine_filter_keys, widget_entities, widget_key_error

logger = logging.getLogger(__name__)

_SOURCE = "integration:openaev"

_ADHOC = "/api/dashboards/adhoc"

#: The ad-hoc query kinds, mapped to their endpoint suffix.
_QUERY_KINDS = {
    "series": "series",
    "count": "count",
    "average": "average",
    "entities": "entities",
    "entities_runtime": "entities-runtime",
}

#: OpenAEV's ``widget_config`` is a Jackson polymorphic type keyed on this
#: EXISTING_PROPERTY discriminator. Omitting it is the "missing discriminator"
#: 400 the operator hit - Jackson cannot pick a concrete class, so the whole
#: request is rejected before any query runs.
_WIDGET_CONFIG_DISCRIMINATOR = "widget_configuration_type"

#: The discriminator token(s) each ad-hoc kind actually accepts. OpenAEV's
#: DashboardService throws UnsupportedOperationException for anything else, so
#: enforce the mapping locally with a precise message rather than round-tripping
#: a guaranteed failure. A kind with exactly one valid token can be auto-filled
#: when the model omits the discriminator; 'series' is ambiguous (temporal vs
#: structural) and must be stated.
_KIND_TO_CONFIG_TYPES: dict[str, tuple[str, ...]] = {
    "count": ("flat",),
    "average": ("average",),
    "entities": ("list",),
    "entities_runtime": ("list",),
    "series": ("temporal-histogram", "structural-histogram"),
}


#: The ``base_entity`` values the engine indexes (the EsBase discriminator
#: mapping of OpenAEV main). A term filter on any other value matches no
#: document, so the query answers 0 with no error.
_BASE_ENTITIES = (
    "asset",
    "asset-group",
    "attack-pattern",
    "expectation-inject",
    "finding",
    "inject",
    "scenario",
    "security-domain",
    "security-platform",
    "simulation",
    "snapshot-attack-observation",
    "snapshot-vulnerability-observation",
    "tag",
    "team",
    "vulnerable-endpoint",
)
#: Retired engine models, with what replaces them.
_RETIRED_BASE_ENTITIES = {
    "endpoint": (
        "'endpoint' was retired on 25 Jul 2026: the index is now 'asset' "
        "(the whole inventory). For hosts only, add the filter "
        '{"key": "asset_category", "operator": "eq", "values": ["HOST"]}.'
    ),
}

#: OpenAEV's CustomDashboardTimeRange values. On an ad-hoc query there is no
#: dashboard to inherit a range from, so DEFAULT (and an absent time_range,
#: which deserialises to DEFAULT) resolves to LAST_QUARTER: the last 90 days.
_TIME_RANGES = (
    "ALL_TIME",
    "CUSTOM",
    "DEFAULT",
    "LAST_DAY",
    "LAST_WEEK",
    "LAST_MONTH",
    "LAST_QUARTER",
    "LAST_SEMESTER",
    "LAST_YEAR",
)


def _filter_error(node: object) -> str:
    """Validate every filter group nested anywhere in a widget configuration.

    OpenAEV coerces an unknown operator to ``eq``, so a typo would answer a
    different question without an error.
    """
    if isinstance(node, dict):
        for key, value in node.items():
            if key in ("filter", "filters", "filterGroup") and isinstance(value, dict):
                err = _validate_filter_group(value)
                if err:
                    return err
            err = _filter_error(value)
            if err:
                return err
    elif isinstance(node, list):
        for item in node:
            err = _filter_error(item)
            if err:
                return err
    return ""


def _base_entity_error(node: object) -> str:
    """Refuse a ``base_entity`` filter value the engine does not index."""
    if isinstance(node, dict):
        if node.get("key") == "base_entity":
            values = node.get("values")
            if values is None:
                values = []
            elif not isinstance(values, list):
                values = [values]
            for value in values:
                token = str(value).strip()
                retired = _RETIRED_BASE_ENTITIES.get(token.lower())
                if retired:
                    return f"base_entity '{value}' is not indexed any more, so the query would answer 0. {retired}"
                if token not in _BASE_ENTITIES:
                    return (
                        f"base_entity '{value}' is not an OpenAEV engine entity, "
                        "so the query would answer 0. Valid values: "
                        f"{', '.join(_BASE_ENTITIES)}."
                    )
        for value in node.values():
            err = _base_entity_error(value)
            if err:
                return err
    elif isinstance(node, list):
        for item in node:
            err = _base_entity_error(item)
            if err:
                return err
    return ""


def _apply_time_range(config: dict) -> tuple[str, str]:
    """Default an absent time_range to ALL_TIME; answer (note, error)."""
    raw = config.get("time_range")
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        config["time_range"] = "ALL_TIME"
        return (
            "time_range not set: ALL_TIME applied (OpenAEV's DEFAULT resolves "
            "to LAST_QUARTER, the last 90 days, on an ad-hoc query)",
            "",
        )
    time_range = str(raw).strip().upper()
    if time_range not in _TIME_RANGES:
        return "", (f"time_range '{raw}' is not an OpenAEV time range. Valid values: {', '.join(_TIME_RANGES)}.")
    config["time_range"] = time_range
    if time_range == "DEFAULT":
        return (
            "time_range DEFAULT resolves to LAST_QUARTER (the last 90 days) on "
            "an ad-hoc query; pass ALL_TIME for every document",
            "",
        )
    return f"time_range {time_range}", ""


# The fields OpenAEV's CustomDashboardInput binds on POST and PUT; any other
# field is dropped silently by the endpoint.
_DASHBOARD_WRITABLE_FIELDS = (
    "custom_dashboard_name",
    "custom_dashboard_description",
    "custom_dashboard_parameters",
)
# The scalar fields an update carries over from the stored dashboard when the
# call does not name them (parameters are left out: OpenAEV keeps them when the
# PUT omits them, and replaces them whole when it carries them).
_DASHBOARD_MERGED_FIELDS = ("custom_dashboard_name", "custom_dashboard_description")

#: OpenAEV's WidgetType values (``widget_type``); any other is a 400.
_WIDGET_TYPES = (
    "attack-path",
    "average",
    "command-center",
    "donut",
    "exposure-score",
    "horizontal-barchart",
    "line",
    "list",
    "number",
    "posture-radar",
    "resilience-gauge",
    "security-coverage",
    "vertical-barchart",
)
#: The fields of OpenAEV's WidgetInput, the body of a widget POST and PUT.
_WIDGET_FIELDS = ("widget_type", "widget_config", "widget_layout")
#: WidgetLayout, with the short names a call may use for each field.
_LAYOUT_FIELDS = {
    "widget_layout_x": "x",
    "widget_layout_y": "y",
    "widget_layout_w": "w",
    "widget_layout_h": "h",
}
#: Size of a widget created without a layout (the grid is 12 columns wide).
_DEFAULT_WIDGET_SIZE = {"widget_layout_w": 6, "widget_layout_h": 4}


def _layout_from(raw: object) -> tuple[dict[str, int], str]:
    """Read a widget layout given with long or short field names.

    Answers (fields given, error). OpenAEV binds the layout to primitive ints,
    so a field left out of a body would be stored as 0, never refused: the
    caller merges what this returns over the stored layout.
    """
    if not isinstance(raw, dict):
        return {}, "widget_layout must be a JSON object of x, y, w and h."
    short_to_long = {short: long for long, short in _LAYOUT_FIELDS.items()}
    layout: dict[str, int] = {}
    for key, value in raw.items():
        field = short_to_long.get(key, key)
        if field not in _LAYOUT_FIELDS:
            return {}, f"widget_layout field '{key}' is not a layout field (x, y, w, h)."
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return {}, f"widget_layout {_LAYOUT_FIELDS[field]} must be a whole number of 0 or more."
        if field in ("widget_layout_w", "widget_layout_h") and value < 1:
            return {}, f"widget_layout {_LAYOUT_FIELDS[field]} must be 1 or more."
        layout[field] = value
    return layout, ""


def _widget_config_error(config: object) -> str:
    """Refuse a widget configuration OpenAEV would reject or answer wrong."""
    if not isinstance(config, dict):
        return "widget_config must be a JSON object."
    declared = config.get(_WIDGET_CONFIG_DISCRIMINATOR)
    if not isinstance(declared, str) or not declared.strip():
        return (
            f"widget_config must carry the '{_WIDGET_CONFIG_DISCRIMINATOR}' discriminator "
            "(flat, average, list, temporal-histogram, structural-histogram...): copy "
            "the shape of a stored widget with action 'get_widget'."
        )
    err = _filter_error(config)
    if err:
        return f"widget_config contains an invalid filter. {err}"
    return _base_entity_error(config)


def _layout_text(layout: object) -> str:
    if not isinstance(layout, dict):
        return "layout not returned"
    values = {short: layout.get(long) for long, short in _LAYOUT_FIELDS.items()}
    return f"x {values['x']}, y {values['y']}, {values['w']} wide, {values['h']} high"


def register(
    base_url: str, headers: dict[str, str], executor: CallableToolExecutor
) -> list[ToolDefinition]:
    async def query_openaev_analytics(
        kind: str = "",
        widget_config: str = "",
        parameters: str = "",
        page: int = 0,
        limit: int = 20,
    ) -> str:
        kind = (kind or "").strip().lower()
        suffix = _QUERY_KINDS.get(kind)
        if suffix is None:
            return (
                f"Unknown kind '{kind}'. Valid kinds: "
                f"{', '.join(sorted(_QUERY_KINDS))}."
            )
        if not widget_config:
            return (
                "widget_config is required: a JSON object describing the "
                "aggregate to compute. It must at least carry the widget type "
                "and its series/entity configuration - mirror the shape used by "
                "an existing dashboard widget (read one with "
                "manage_openaev_dashboard action='get_widget')."
            )
        try:
            config = json.loads(widget_config)
        except ValueError as exc:
            return f"widget_config must be valid JSON: {exc}"
        if not isinstance(config, dict):
            return "widget_config must be a JSON object."

        # Validate (and, where unambiguous, auto-fill) the Jackson discriminator.
        # A missing/wrong `widget_configuration_type` is the exact 400 the
        # operator hit: OpenAEV cannot deserialize the polymorphic widget_config
        # without it, and it rejects a token the kind does not support.
        allowed_types = _KIND_TO_CONFIG_TYPES.get(kind, ())
        declared_type = config.get(_WIDGET_CONFIG_DISCRIMINATOR)
        # Treat a present-but-empty/whitespace string as "missing": Jackson
        # cannot resolve the polymorphic type from a blank token either, so
        # auto-fill/prompt exactly as we do for an absent discriminator.
        if isinstance(declared_type, str) and not declared_type.strip():
            declared_type = None
            config.pop(_WIDGET_CONFIG_DISCRIMINATOR, None)
        if declared_type is None:
            if len(allowed_types) == 1:
                config[_WIDGET_CONFIG_DISCRIMINATOR] = allowed_types[0]
            else:
                return (
                    f"widget_config is missing the '{_WIDGET_CONFIG_DISCRIMINATOR}' "
                    "discriminator, so OpenAEV cannot tell which widget shape it is "
                    f"(this is the 'missing discriminator' 400). For kind='{kind}' "
                    f"set it to one of: {', '.join(allowed_types)} "
                    "('temporal-histogram' groups over time and needs an 'interval'; "
                    "'structural-histogram' groups by a field and needs a 'field')."
                )
        elif not isinstance(declared_type, str):
            # A non-string discriminator (number, object, list, bool) can never
            # match a Jackson subtype id and would round-trip to the same opaque
            # 400 - reject it locally with an actionable message instead.
            return (
                f"widget_config '{_WIDGET_CONFIG_DISCRIMINATOR}' must be a string, "
                f"got {type(declared_type).__name__}. For kind='{kind}' set it to "
                f"one of: {', '.join(allowed_types)}."
                if allowed_types
                else f"widget_config '{_WIDGET_CONFIG_DISCRIMINATOR}' must be a string, "
                f"got {type(declared_type).__name__}."
            )
        elif allowed_types and declared_type not in allowed_types:
            return (
                f"widget_config '{_WIDGET_CONFIG_DISCRIMINATOR}'='{declared_type}' is "
                f"not valid for kind='{kind}'. This endpoint only accepts: "
                f"{', '.join(allowed_types)}. Either fix the discriminator or call "
                "the kind that matches your widget shape."
            )

        # Validate every filter group nested anywhere in the configuration, so a
        # typo in an operator cannot silently degrade to `eq`.
        config_err = _filter_error(config)
        if config_err:
            return f"widget_config contains an invalid filter. {config_err}"
        entity_err = _base_entity_error(config)
        if entity_err:
            return f"widget_config refused, nothing queried. {entity_err}"
        time_note, time_err = _apply_time_range(config)
        if time_err:
            return f"widget_config refused, nothing queried. {time_err}"
        # The engine filters on its own indexed fields: a JPA attribute name
        # (asset_tags) is a 400 'Unknown field' after the call, so check the keys
        # first. An unreadable schema leaves the query unchecked, never refused.
        entities = widget_entities(config)
        engine_keys, _schema_err = await engine_filter_keys(_api, base_url, headers, entities)
        key_err = widget_key_error(config, engine_keys, entities)
        if key_err:
            return f"widget_config refused, nothing queried. {key_err}"

        params: dict = {}
        if parameters:
            try:
                params = json.loads(parameters)
            except ValueError as exc:
                return f"parameters must be valid JSON: {exc}"
            if not isinstance(params, dict):
                return "parameters must be a JSON object of string->string."
            params = {str(k): str(v) for k, v in params.items()}

        body: dict = {"widget_config": config}
        if params:
            body["parameters"] = params
        if kind == "entities":
            body["pagination"] = {"page": max(page, 0), "size": min(limit, 100)}

        resp, err = await _api(
            base_url, headers, "post", f"{_ADHOC}/{suffix}",
            f"ad-hoc {kind} query", json=body,
        )
        if err:
            return err
        payload = resp.json()
        rendered = json.dumps(payload, default=str, indent=None)
        if len(rendered) > 6000:
            rendered = rendered[:6000] + " ...[truncated]"
        return f"Ad-hoc {kind} result ({time_note}):\n{rendered}"

    async def manage_openaev_dashboard(
        action: str = "",
        dashboard_id: str = "",
        widget_id: str = "",
        resource_id: str = "",
        name: str = "",
        description: str = "",
        parameters: str = "",
        config: str = "",
        search: str = "",
        limit: int = 20,
    ) -> str:
        action = (action or "").strip().lower()

        extra: dict = {}
        if config:
            try:
                extra = json.loads(config)
            except ValueError as exc:
                return f"config must be a valid JSON object: {exc}"
            if not isinstance(extra, dict):
                return "config must be a JSON object of field->value pairs."

        if action == "list":
            resp, err = await _api(
                base_url, headers, "post", "/api/custom-dashboards/search",
                "search custom dashboards",
                json=_search_body(size=min(limit, 50), text_search=search or None),
            )
            if err:
                return err
            data = resp.json()
            items = data.get("content", data) if isinstance(data, dict) else data
            if not items:
                return "No custom dashboards found."
            lines = [f"Custom dashboards ({len(items)}):"]
            for d in items:
                lines.append(
                    f"- {d.get('custom_dashboard_name', '?')} "
                    f"(id: {d.get('custom_dashboard_id')})"
                )
            return "\n".join(lines)

        if action == "get":
            if not dashboard_id:
                return "dashboard_id is required for action 'get'."
            resp, err = await _api(
                base_url, headers, "get", f"/api/custom-dashboards/{dashboard_id}",
                "get custom dashboard",
            )
            if err:
                return err
            return json_answer(resp.json())

        if action == "for_resource":
            if not resource_id:
                return (
                    "resource_id (a simulation or scenario id) is required for "
                    "'for_resource'."
                )
            resp, err = await _api(
                base_url, headers, "get",
                f"/api/custom-dashboards/resource/{resource_id}/options",
                "list dashboards for resource",
            )
            if err:
                return err
            items = resp.json() or []
            if not items:
                return f"No dashboards can be bound to resource {resource_id}."
            return json_answer(items)

        if action in ("create", "update"):
            ignored = sorted(set(extra) - set(_DASHBOARD_WRITABLE_FIELDS))
            if ignored:
                # OpenAEV's dashboard endpoint drops any other field without an
                # error (widgets live under /widgets), so a write carrying one
                # would report a success that did not happen.
                return (
                    f"Nothing was written: config field(s) {', '.join(ignored)} "
                    "are not stored by OpenAEV's custom dashboard endpoint, which "
                    "keeps only " + ", ".join(_DASHBOARD_WRITABLE_FIELDS) + ". "
                    "Widgets are written with create_widget, update_widget, "
                    "update_layout and delete_widget."
                )
            changes: dict[str, Any] = dict(extra)
            if name:
                changes["custom_dashboard_name"] = name
            if description:
                changes["custom_dashboard_description"] = description
            if action == "create" and not changes.get("custom_dashboard_name"):
                return "name is required for action 'create'."
            kept: list[str] = []
            if action == "update":
                if not dashboard_id:
                    return "dashboard_id is required for action 'update'."
                if not changes:
                    return (
                        "Provide at least one field to update (name, description, "
                        "or config)."
                    )
                # The PUT replaces the entity whole: a field it does not carry is
                # cleared (description) or refused (name). Read the dashboard and
                # merge the change over it so an update only changes what it names.
                current_resp, cerr = await _api(
                    base_url,
                    headers,
                    "get",
                    f"/api/custom-dashboards/{dashboard_id}",
                    "read custom dashboard before update",
                )
                if cerr or current_resp is None:
                    return cerr or f"Nothing was written: dashboard {dashboard_id} could not be read."
                current = current_resp.json()
                if not isinstance(current, dict):
                    return f"Nothing was written: dashboard {dashboard_id} could not be read before the update."
                body: dict[str, Any] = {}
                for field in _DASHBOARD_MERGED_FIELDS:
                    if field not in changes and current.get(field) is not None:
                        body[field] = current[field]
                        kept.append(field)
                body.update(changes)
                resp, err = await _api(
                    base_url, headers, "put",
                    f"/api/custom-dashboards/{dashboard_id}",
                    "update custom dashboard", json=body,
                )
            else:
                body = changes
                resp, err = await _api(
                    base_url,
                    headers,
                    "post",
                    "/api/custom-dashboards",
                    "create custom dashboard",
                    json=body,
                )
            if err:
                return err
            d = resp.json()
            if not isinstance(d, dict):
                d = {}
            not_applied = [
                field
                for field in _DASHBOARD_MERGED_FIELDS
                if field in body and field in d and d.get(field) != body[field]
            ]
            verb = "Created" if action == "create" else "Updated"
            answer = (
                f"{verb} custom dashboard '{d.get('custom_dashboard_name')}' "
                f"(id: {d.get('custom_dashboard_id') or dashboard_id})."
            )
            if kept:
                answer += " Kept as they were: " + ", ".join(kept) + "."
            if not_applied:
                answer += (
                    " WARNING: OpenAEV answered with different values for "
                    + ", ".join(not_applied)
                    + " than the ones sent; read the dashboard back with action 'get'."
                )
            return answer

        if action == "delete":
            if not dashboard_id:
                return "dashboard_id is required for action 'delete'."
            _, err = await _api(
                base_url, headers, "delete",
                f"/api/custom-dashboards/{dashboard_id}", "delete custom dashboard",
            )
            if err:
                return err
            return f"Deleted custom dashboard {dashboard_id}."

        if action == "list_widgets":
            if not dashboard_id:
                return "dashboard_id is required for action 'list_widgets'."
            resp, err = await _api(
                base_url, headers, "get",
                f"/api/custom-dashboards/{dashboard_id}/widgets", "list widgets",
            )
            if err:
                return err
            return json_answer(resp.json())

        if action == "get_widget":
            if not dashboard_id or not widget_id:
                return "dashboard_id and widget_id are required for 'get_widget'."
            resp, err = await _api(
                base_url, headers, "get",
                f"/api/custom-dashboards/{dashboard_id}/widgets/{widget_id}",
                "get widget",
            )
            if err:
                return err
            return json_answer(resp.json())

        if action in ("create_widget", "update_widget", "update_layout", "delete_widget"):
            return await _write_widget(action, dashboard_id, widget_id, extra, bool(config))

        if action in ("run_count", "run_average", "run_series", "run_entities"):
            if not widget_id:
                return f"widget_id is required for action '{action}'."
            params: dict = {}
            if parameters:
                try:
                    params = json.loads(parameters)
                except ValueError as exc:
                    return f"parameters must be valid JSON: {exc}"
                if not isinstance(params, dict):
                    return "parameters must be a JSON object of string->string."
            suffix = action[len("run_"):]
            resp, err = await _api(
                base_url, headers, "post", f"/api/dashboards/{suffix}/{widget_id}",
                f"run widget {suffix}", json=params or None,
            )
            if err:
                return err
            return json_answer(resp.json())

        if action == "search_engine":
            if not search:
                return "search is required for action 'search_engine'."
            resp, err = await _api(
                base_url, headers, "get", f"/api/dashboards/search/{search}",
                "engine quick search",
            )
            if err:
                return err
            return json_answer(resp.json())

        return (
            f"Unknown action '{action}'. Valid actions: list, get, for_resource, "
            "create, update, delete, list_widgets, get_widget, create_widget, "
            "update_widget, update_layout, delete_widget, run_count, "
            "run_average, run_series, run_entities, search_engine."
        )

    async def _read_widget(dashboard_id: str, widget_id: str) -> tuple[dict[str, Any], str]:
        resp, err = await _api(
            base_url,
            headers,
            "get",
            f"/api/custom-dashboards/{dashboard_id}/widgets/{widget_id}",
            "read widget before the write",
        )
        if err or resp is None:
            return {}, err or f"widget {widget_id} could not be read"
        stored = resp.json()
        if not isinstance(stored, dict):
            return {}, f"widget {widget_id} could not be read"
        return stored, ""

    async def _stored_layout_text(dashboard_id: str, widget_id: str, sent: dict[str, Any]) -> tuple[str, str]:
        """The widget's layout as stored after a write, read back, and a note when it differs from what was sent."""
        stored, rerr = await _read_widget(dashboard_id, widget_id)
        layout = stored.get("widget_layout") if not rerr else None
        if not isinstance(layout, dict):
            return _layout_text(sent), " The stored layout could not be read back: this is the layout sent."
        differs = any(layout.get(field) != sent.get(field) for field in _LAYOUT_FIELDS)
        return _layout_text(layout), (" OpenAEV stored a layout other than the one sent." if differs else "")

    async def _next_free_row(dashboard_id: str) -> tuple[int, str]:
        """The first grid row under every stored widget of the dashboard."""
        resp, err = await _api(
            base_url,
            headers,
            "get",
            f"/api/custom-dashboards/{dashboard_id}/widgets",
            "list widgets",
        )
        if err or resp is None:
            return 0, err or f"dashboard {dashboard_id} widgets could not be read"
        bottom = 0
        for stored in resp.json() or []:
            layout = stored.get("widget_layout") if isinstance(stored, dict) else None
            if isinstance(layout, dict):
                bottom = max(
                    bottom,
                    int(layout.get("widget_layout_y") or 0) + int(layout.get("widget_layout_h") or 0),
                )
        return bottom, ""

    async def _write_widget(
        action: str, dashboard_id: str, widget_id: str, extra: dict[str, Any], has_config: bool
    ) -> str:
        if not dashboard_id:
            return f"dashboard_id is required for action '{action}'."
        if action != "create_widget" and not widget_id:
            return f"widget_id is required for action '{action}'."
        base = f"/api/custom-dashboards/{dashboard_id}/widgets"

        if action == "delete_widget":
            _, err = await _api(base_url, headers, "delete", f"{base}/{widget_id}", "delete widget")
            if err:
                return err
            return f"Deleted widget {widget_id} from dashboard {dashboard_id}."

        if not has_config:
            return f"config is required for action '{action}': " + (
                "a JSON object of x, y, w and h (any of them)."
                if action == "update_layout"
                else "a JSON object of widget_type, widget_config and widget_layout."
            )

        if action == "update_layout":
            layout, lerr = _layout_from(extra)
            if lerr:
                return f"Nothing was written: {lerr}"
            stored, rerr = await _read_widget(dashboard_id, widget_id)
            if rerr:
                return f"Nothing was written: {rerr}"
            moved = {**(stored.get("widget_layout") or {}), **layout}
            missing = [short for long, short in _LAYOUT_FIELDS.items() if long not in moved]
            if missing:
                return f"Nothing was written: the stored layout has no {', '.join(missing)}; pass every one of x, y, w and h."
            _, err = await _api(
                base_url,
                headers,
                "put",
                f"{base}/{widget_id}/layout",
                "move widget",
                json=moved,
            )
            if err:
                return err
            layout_text, note = await _stored_layout_text(dashboard_id, widget_id, moved)
            return f"Moved widget {widget_id}: {layout_text}.{note}"

        ignored = sorted(set(extra) - set(_WIDGET_FIELDS))
        if ignored:
            return (
                f"Nothing was written: config field(s) {', '.join(ignored)} are not widget "
                "fields; a widget keeps only " + ", ".join(_WIDGET_FIELDS) + "."
            )
        kept: list[str] = []
        stored_layout: dict[str, Any] = {}
        body: dict[str, Any]
        if action == "update_widget":
            # OpenAEV's widget PUT replaces type, configuration and layout whole:
            # carry over the stored values the call does not name.
            stored, rerr = await _read_widget(dashboard_id, widget_id)
            if rerr:
                return f"Nothing was written: {rerr}"
            if isinstance(stored.get("widget_layout"), dict):
                stored_layout = stored["widget_layout"]
            body = {}
            for field in _WIDGET_FIELDS:
                if field not in extra and stored.get(field) is not None:
                    body[field] = stored[field]
                    kept.append(field)
            body.update(extra)
        else:
            body = dict(extra)
            if "widget_type" not in body or "widget_config" not in body:
                return "Nothing was written: create_widget needs widget_type and widget_config in config."

        widget_type = body.get("widget_type")
        if widget_type not in _WIDGET_TYPES:
            return (
                f"Nothing was written: widget_type '{widget_type}' is not an OpenAEV "
                f"widget type. Valid values: {', '.join(_WIDGET_TYPES)}."
            )
        if "widget_config" in extra:
            config_err = _widget_config_error(body.get("widget_config"))
            if config_err:
                return f"Nothing was written: {config_err}"
        if "widget_layout" in extra:
            layout, lerr = _layout_from(extra["widget_layout"])
            if lerr:
                return f"Nothing was written: {lerr}"
            # A layout field left out keeps its stored value (OpenAEV would store 0).
            body["widget_layout"] = {**stored_layout, **layout}
        placed = ""
        if action == "create_widget":
            layout = dict(body.get("widget_layout") or {})
            for field, size in _DEFAULT_WIDGET_SIZE.items():
                layout.setdefault(field, size)
            layout.setdefault("widget_layout_x", 0)
            if "widget_layout_y" not in layout:
                row, rerr = await _next_free_row(dashboard_id)
                if rerr:
                    return f"Nothing was written: {rerr}"
                layout["widget_layout_y"] = row
                placed = " Placed under the existing widgets."
            body["widget_layout"] = layout
        missing = [short for long, short in _LAYOUT_FIELDS.items() if long not in (body.get("widget_layout") or {})]
        if missing:
            return f"Nothing was written: widget_layout has no {', '.join(missing)}; pass every one of x, y, w and h."

        if action == "create_widget":
            resp, err = await _api(base_url, headers, "post", base, "create widget", json=body)
        else:
            resp, err = await _api(
                base_url,
                headers,
                "put",
                f"{base}/{widget_id}",
                "update widget",
                json=body,
            )
        if err:
            return err
        saved = resp.json() if resp is not None else {}
        saved = saved if isinstance(saved, dict) else {}
        verb = "Created" if action == "create_widget" else "Updated"
        if action == "create_widget":
            layout_text, note = _layout_text(saved.get("widget_layout") or body["widget_layout"]), ""
        else:
            # The write's answer may echo the request rather than the row: report the layout read back.
            layout_text, note = await _stored_layout_text(dashboard_id, widget_id, body["widget_layout"])
        answer = (
            f"{verb} {saved.get('widget_type') or body['widget_type']} widget "
            f"{saved.get('widget_id') or widget_id} on dashboard {dashboard_id}: "
            f"{layout_text}.{placed}{note}"
        )
        if kept:
            answer += " Kept as they were: " + ", ".join(kept) + "."
        return answer

    executor.register("query_openaev_analytics", query_openaev_analytics)
    executor.register("manage_openaev_dashboard", manage_openaev_dashboard)

    return [
        ToolDefinition(
            name="query_openaev_analytics",
            description=(
                "Run an AD-HOC analytics query against OpenAEV without needing a "
                "saved dashboard widget: you supply the widget configuration "
                "inline and get the computed aggregate back. Use this for "
                "quantitative questions the dedicated tools do not answer "
                "directly (counts, averages and time series over simulations, "
                "injects, expectations, findings, assets, ...). kind: 'count' "
                "(a single count with its interval), 'average', 'series' (time "
                "series), 'entities' (the matching entities, paginated), "
                "'entities_runtime' (runtime-resolved entities). widget_config is "
                "the same JSON structure a stored widget holds - read one with "
                "manage_openaev_dashboard action='get_widget' to copy the shape. "
                "Filter operators inside it are validated locally because "
                "OpenAEV silently coerces an unknown operator to 'eq'. The "
                "entity is a filter {key: 'base_entity', values: [...]}: valid "
                f"values are {', '.join(_BASE_ENTITIES)} ('endpoint' was "
                "retired for 'asset'); any other value is refused, as OpenAEV "
                "would answer 0. Filter keys are engine fields (base_tags_side, "
                "not asset_tags), checked before the query. time_range "
                "(ALL_TIME, LAST_DAY, LAST_WEEK, LAST_MONTH, LAST_QUARTER, "
                "LAST_SEMESTER, LAST_YEAR, CUSTOM, DEFAULT) defaults to ALL_TIME when left out, because OpenAEV's "
                "DEFAULT means LAST_QUARTER (90 days) on an ad-hoc query; the "
                "answer names the range applied."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "kind": {
                        "type": "string",
                        "enum": sorted(_QUERY_KINDS),
                        "description": "Which aggregate to compute.",
                    },
                    "widget_config": {
                        "type": "string",
                        "description": (
                            "JSON widget configuration (series, filters, "
                            "grouping). MUST carry a 'widget_configuration_type' "
                            "discriminator matching the kind, or OpenAEV rejects "
                            "it with a 400: 'flat' for count, 'average' for "
                            "average, 'list' for entities, and 'temporal-histogram' "
                            "or 'structural-histogram' for series. Mirror an "
                            "existing widget's configuration (read one with "
                            "manage_openaev_dashboard action='get_widget') rather "
                            "than inventing the shape."
                        ),
                    },
                    "parameters": {"type": "string", "description": "Optional JSON object of string parameters substituted into the widget configuration."},
                    "page": {"type": "integer", "default": 0, "description": "Page index for kind='entities'."},
                    "limit": {"type": "integer", "default": 20, "description": "Page size for kind='entities' (max 100)."},
                },
                "required": ["kind", "widget_config"],
            },
            source=_SOURCE,
        ),
        ToolDefinition(
            name="manage_openaev_dashboard",
            description=(
                "Manage OpenAEV custom dashboards and run their saved widgets. "
                "Actions: 'list' (search dashboards), 'get' (dashboard_id), "
                "'for_resource' (resource_id - dashboards bindable to a "
                "simulation or scenario), 'create' (name), 'update' (dashboard_id; "
                "only the fields passed change, the rest is read from the stored "
                "dashboard and kept), 'delete', "
                "'list_widgets' (dashboard_id), 'get_widget' (dashboard_id + "
                "widget_id - also the best way to learn a widget_config shape for "
                "query_openaev_analytics), 'create_widget' (dashboard_id + config "
                "widget_type, widget_config, optional widget_layout; placed under "
                "the existing widgets when no y is given), 'update_widget' "
                "(dashboard_id + widget_id + config; fields left out are kept), "
                "'update_layout' (config x, y, w, h on a 12-column grid), "
                "'delete_widget', 'run_count' / 'run_average' / "
                "'run_series' / 'run_entities' (widget_id + optional parameters - "
                "execute a stored widget), 'search_engine' (search - quick "
                "cross-entity engine search)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": [
                            "list", "get", "for_resource", "create", "update",
                            "delete", "list_widgets", "get_widget", "create_widget",
                            "update_widget", "update_layout", "delete_widget", "run_count",
                            "run_average", "run_series", "run_entities",
                            "search_engine",
                        ],
                    },
                    "dashboard_id": {"type": "string"},
                    "widget_id": {"type": "string"},
                    "resource_id": {"type": "string", "description": "Simulation or scenario id (for_resource)."},
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                    "parameters": {"type": "string", "description": "JSON object of string parameters for the run_* actions."},
                    "config": {"type": "string", "description": "JSON object. create/update: custom_dashboard_name, custom_dashboard_description, custom_dashboard_parameters. Widget actions: widget_type, widget_config, widget_layout. update_layout: x, y, w, h. Any other field is refused."},
                    "search": {"type": "string"},
                    "limit": {"type": "integer", "default": 20},
                },
                "required": ["action"],
            },
            source=_SOURCE,
        ),
    ]
