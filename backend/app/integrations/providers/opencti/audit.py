"""OpenCTI activity / history audit query tool (read-only).

Queries the platform audit indices via ``audits`` / ``auditsNumber``
(``src/resolvers/log.js``), which need one of SETTINGS_SETACCESSES /
SETTINGS_SECURITYACTIVITY / VIRTUAL_ORGANIZATION_ADMIN whatever the stream.
Two streams, each with its own capability on top:

* ``History``  - knowledge / entity change history (KNOWLEDGE capability)
* ``Activity`` - security activity audit (SETTINGS_SECURITYACTIVITY)

For entity-scoped change history, use ``get_opencti_entity_history`` instead.
"""

from __future__ import annotations

import json
import logging

from app.tools.base import CallableToolExecutor, ToolDefinition

from ._common import _client, _edges, _graphql

logger = logging.getLogger(__name__)

_SOURCE = "integration:opencti"


def _normalize_stream(stream: str | None) -> str:
    """The audit type a ``query_opencti_activity`` call sends as ``types``."""
    value = (stream or "History").strip()
    if value.lower() in ("history", "knowledge"):
        return "History"
    if value.lower() in ("activity", "security"):
        return "Activity"
    return value


def _normalize_mode(mode: str | None) -> str:
    return (mode or "list").strip().lower()


def register(
    base_url: str, headers: dict[str, str], executor: CallableToolExecutor
) -> list[ToolDefinition]:
    async def _gql(query: str, variables: dict | None = None) -> tuple[dict, str | None]:
        async with _client(headers, timeout=30) as client:
            return await _graphql(client, base_url, headers, query, variables)

    async def query_opencti_activity(
        stream: str = "History",
        mode: str = "list",
        search: str = "",
        filters: str = "",
        start_date: str = "",
        end_date: str = "",
        limit: int = 50,
    ) -> str:
        stream = _normalize_stream(stream)
        mode = _normalize_mode(mode)

        # Parse the optional FilterGroup JSON string.
        base_filters: dict | None = None
        if filters:
            try:
                parsed = json.loads(filters)
            except ValueError as exc:
                return f"filters must be a valid JSON string (FilterGroup): {exc}"
            if not isinstance(parsed, dict):
                return (
                    "filters must be a FilterGroup object - a JSON object with "
                    "'mode', 'filters', and 'filterGroups' - not a bare array or "
                    "scalar."
                )
            # Normalize list fields defensively: a caller-supplied FilterGroup may
            # carry null or non-list 'filters'/'filterGroups'. Done here at parse
            # time so BOTH the list path (via _with_dates) and the count path
            # (which forwards base_filters straight to GraphQL) send a well-formed
            # FilterGroup the API will accept.
            if not isinstance(parsed.get("filters"), list):
                parsed["filters"] = []
            if not isinstance(parsed.get("filterGroups"), list):
                parsed["filterGroups"] = []
            parsed.setdefault("mode", "and")
            base_filters = parsed

        def _with_dates() -> dict | None:
            date_filters: list[dict] = []
            if start_date:
                date_filters.append(
                    {"key": "timestamp", "values": [start_date], "operator": "gte"}
                )
            if end_date:
                date_filters.append(
                    {"key": "timestamp", "values": [end_date], "operator": "lte"}
                )

            # base_filters is already a normalized FilterGroup (see parse step).
            caller_group: dict | None = base_filters

            if caller_group and date_filters:
                # Wrap the caller's group and the date predicates in an outer AND
                # so the date range always NARROWS the result set. Appending the
                # dates into the caller's own group would join its mode — for an
                # 'or' group that would broaden the query instead of constraining
                # it to the requested range.
                return {
                    "mode": "and",
                    "filters": date_filters,
                    "filterGroups": [caller_group],
                }
            if date_filters:
                return {"mode": "and", "filters": date_filters, "filterGroups": []}
            if caller_group and (caller_group["filters"] or caller_group["filterGroups"]):
                return caller_group
            return None

        if mode == "count":
            query = (
                "query AuditsNumber($types: [String], $startDate: DateTime,"
                " $endDate: DateTime, $filters: FilterGroup, $search: String) {"
                "  auditsNumber(types: $types, startDate: $startDate,"
                "    endDate: $endDate, filters: $filters, search: $search)"
                "    { count total } }"
            )
            data, err = await _gql(
                query,
                {
                    "types": [stream],
                    "startDate": start_date or None,
                    "endDate": end_date or None,
                    "filters": base_filters,
                    "search": search or None,
                },
            )
            if err:
                return err
            num = data.get("auditsNumber") or {}
            return (
                f"{stream} audit count: {num.get('count')} in period "
                f"(total {num.get('total')})."
            )

        query = (
            "query Audits($first: Int, $types: [String!], $filters: FilterGroup,"
            " $search: String, $orderBy: LogsOrdering, $orderMode: OrderingMode) {"
            "  audits(first: $first, types: $types, filters: $filters,"
            "    search: $search, orderBy: $orderBy, orderMode: $orderMode) {"
            "    edges { node { id event_type event_scope event_status timestamp"
            "      user { name } context_data { entity_name entity_type message } } } }"
            "}"
        )
        data, err = await _gql(
            query,
            {
                "first": min(max(limit, 1), 200),
                "types": [stream],
                "filters": _with_dates(),
                "search": search or None,
                "orderBy": "timestamp",
                "orderMode": "desc",
            },
        )
        if err:
            return err
        nodes = _edges(data, "audits")
        if not nodes:
            return f"No {stream} audit entries found for the given criteria."
        lines = [f"{stream} audit entries ({len(nodes)}):"]
        for n in nodes:
            ts = n.get("timestamp", "?")
            user = (n.get("user") or {}).get("name", "system")
            action = n.get("event_type", "?")
            if n.get("event_scope"):
                action += f"/{n['event_scope']}"
            ctx = n.get("context_data") or {}
            msg = ctx.get("message") or ""
            target = ctx.get("entity_name") or ""
            line = f"- [{ts}] {user} {action}"
            if target:
                line += f" on '{target}'"
            if msg:
                line += f": {msg}"
            lines.append(line)
        return "\n".join(lines)

    executor.register("query_opencti_activity", query_opencti_activity)

    return [
        ToolDefinition(
            name="query_opencti_activity",
            description=(
                "Query the OpenCTI platform audit log (read-only). stream='History' "
                "(knowledge/entity change history) or stream='Activity' (security "
                "activity audit - logins, config changes, file ops; requires "
                "SETTINGS_SECURITYACTIVITY). mode='list' (default, recent entries) or "
                "mode='count' (number of entries in the period). Optional search, "
                "start_date/end_date (ISO 8601), and filters (JSON string of a "
                "FilterGroup, e.g. by user_id, event_type, or context_data.entity_type). "
                "For the change history of ONE specific entity, prefer "
                "get_opencti_entity_history."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "stream": {
                        "type": "string",
                        "enum": ["History", "Activity"],
                        "description": "Which audit stream to query.",
                        "default": "History",
                    },
                    "mode": {
                        "type": "string",
                        "enum": ["list", "count"],
                        "default": "list",
                    },
                    "search": {"type": "string", "description": "Full-text search across audit entries."},
                    "filters": {"type": "string", "description": "JSON string of a FilterGroup (advanced filtering)."},
                    "start_date": {"type": "string", "description": "ISO 8601 start of period (optional)."},
                    "end_date": {"type": "string", "description": "ISO 8601 end of period (optional)."},
                    "limit": {"type": "integer", "description": "Max entries for 'list' (default 50, max 200).", "default": 50},
                },
                "required": [],
            },
            source=_SOURCE,
        ),
    ]
