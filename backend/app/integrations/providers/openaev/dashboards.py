# SPDX-License-Identifier: Apache-2.0

"""OpenAEV analytics tools - ad-hoc widget queries and saved dashboards.

``query_openaev_analytics`` drives ``POST /api/dashboards/adhoc/*``, the v3
endpoints where the CALLER supplies a full widget configuration inline instead
of referencing a stored widget. That makes them the most useful analytics
surface for an agent: any aggregate the OpenAEV dashboards can draw can be
asked for directly, without an admin first creating a widget.

``manage_openaev_dashboard`` covers the saved custom dashboards and their
widgets, including running a stored widget by id.

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

from ._common import _api, _search_body, _validate_filter_group

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
        def _walk(node: object) -> str:
            if isinstance(node, dict):
                for key, value in node.items():
                    if key in ("filter", "filters", "filterGroup") and isinstance(
                        value, dict
                    ):
                        err = _validate_filter_group(value)
                        if err:
                            return err
                    err = _walk(value)
                    if err:
                        return err
            elif isinstance(node, list):
                for item in node:
                    err = _walk(item)
                    if err:
                        return err
            return ""

        config_err = _walk(config)
        if config_err:
            return f"widget_config contains an invalid filter. {config_err}"

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
        return f"Ad-hoc {kind} result:\n{rendered}"

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
            return json.dumps(resp.json(), default=str)[:3000]

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
            return json.dumps(items, default=str)[:2000]

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
                    "Widgets are not written through this action."
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
            return json.dumps(resp.json(), default=str)[:3000]

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
            return json.dumps(resp.json(), default=str)[:3000]

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
            return json.dumps(resp.json(), default=str)[:4000]

        if action == "search_engine":
            if not search:
                return "search is required for action 'search_engine'."
            resp, err = await _api(
                base_url, headers, "get", f"/api/dashboards/search/{search}",
                "engine quick search",
            )
            if err:
                return err
            return json.dumps(resp.json(), default=str)[:3000]

        return (
            f"Unknown action '{action}'. Valid actions: list, get, for_resource, "
            "create, update, delete, list_widgets, get_widget, run_count, "
            "run_average, run_series, run_entities, search_engine."
        )

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
                "injects, expectations, findings, endpoints, ...). kind: 'count' "
                "(a single count with its interval), 'average', 'series' (time "
                "series), 'entities' (the matching entities, paginated), "
                "'entities_runtime' (runtime-resolved entities). widget_config is "
                "the same JSON structure a stored widget holds - read one with "
                "manage_openaev_dashboard action='get_widget' to copy the shape. "
                "Filter operators inside it are validated locally because "
                "OpenAEV silently coerces an unknown operator to 'eq'."
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
                "query_openaev_analytics), 'run_count' / 'run_average' / "
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
                            "delete", "list_widgets", "get_widget", "run_count",
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
                    "config": {"type": "string", "description": "JSON object of raw fields: custom_dashboard_name, custom_dashboard_description, custom_dashboard_parameters (replaces the parameters whole). Any other field (widgets included) is refused, as OpenAEV would drop it."},
                    "search": {"type": "string"},
                    "limit": {"type": "integer", "default": 20},
                },
                "required": ["action"],
            },
            source=_SOURCE,
        ),
    ]
