"""Google TI IoC Stream tools - the unified notification feed.

The IoC Stream (``/ioc_stream``) merges IoC notifications from every origin
(Livehunt rulesets, Retrohunt jobs, followed threats / collections, threat
profiles, saved searches) into one feed. Notifications auto-expire after 30
days.

* ``get_gti_ioc_stream`` - ``GET /ioc_stream`` with the documented filters
  (date, origin, entity_type, source_type, source_id, notification_tag).
* ``manage_gti_ioc_stream_notification`` - read one notification
  (``GET /ioc_stream_notifications/{id}``), delete one
  (``DELETE /ioc_stream_notifications/{id}``), or delete a filtered set
  (``DELETE /ioc_stream?filter=...``). Deletions are permanent.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from app.integrations.http import format_pagination_hint, safe_request
from app.tools.base import CallableToolExecutor, ToolDefinition

from ._common import (
    _PROVIDER,
    _SOURCE,
    as_int,
    attributes_of,
    clamp,
    client,
    context_of,
    cursor_of,
    data_of,
    trunc,
    ts,
)

_STREAM_ACTIONS = frozenset({"get", "delete", "delete_filtered"})


def _format_stream_object(obj: dict[str, Any]) -> str:
    attrs = attributes_of(obj)
    ctx = context_of(obj)
    kind = trunc(obj.get("type"), 30) or "?"
    obj_id = trunc(obj.get("id"), 100) or "?"
    bits: list[str] = []
    origin = ctx.get("origin")
    if origin:
        bits.append(f"origin {trunc(origin, 30)}")
    notification_date = ctx.get("notification_date")
    when = ts(notification_date)
    if when:
        bits.append(when)
    hunting = ctx.get("hunting_info")
    if isinstance(hunting, dict) and hunting.get("rule_name"):
        bits.append(f"rule {trunc(hunting.get('rule_name'), 60)}")
    sources = ctx.get("sources")
    if isinstance(sources, list) and sources:
        names = ", ".join(
            trunc(s.get("label") or s.get("id"), 50)
            for s in sources[:3]
            if isinstance(s, dict)
        )
        if names:
            bits.append(f"from {names}")
    stats = attrs.get("last_analysis_stats")
    verdict = ""
    if isinstance(stats, dict):
        verdict = f" [{as_int(stats.get('malicious'))} malicious]"
    suffix = f" ({'; '.join(bits)})" if bits else ""
    return f"  {kind}: {obj_id}{verdict}{suffix}"


def register(
    api: str, headers: dict[str, str], executor: CallableToolExecutor
) -> list[ToolDefinition]:
    """Register IoC Stream tools and return their definitions."""

    async def get_gti_ioc_stream(
        filter: str = "",
        order: str = "",
        limit: int = 10,
        cursor: str = "",
        descriptors_only: bool = False,
    ) -> str:
        """Read the unified IoC Stream (Livehunt, followed threats, profiles, saved searches)."""
        params: dict[str, Any] = {"limit": clamp(limit)}
        if filter.strip():
            params["filter"] = filter.strip()
        if order.strip():
            params["order"] = order.strip()
        if cursor.strip():
            params["cursor"] = cursor.strip()
        if descriptors_only:
            params["descriptors_only"] = "true"
        async with client() as c:
            resp, err = await safe_request(
                c,
                "get",
                f"{api}/ioc_stream",
                _PROVIDER,
                f"read IoC stream ({trunc(filter, 100) or 'all'})",
                headers=headers,
                params=params,
            )
            if err:
                return err
            payload = resp.json() or {}
        items = data_of(payload)
        items = items if isinstance(items, list) else []
        if not items:
            return (
                "The IoC Stream has no notifications matching "
                f"'{trunc(filter, 120) or 'all'}'. Notifications expire after "
                "30 days; check the origin/source_type filters."
            )
        lines = [f"IoC Stream objects ({len(items)} shown):"]
        lines.extend(_format_stream_object(o) for o in items if isinstance(o, dict))
        return "\n".join(lines) + format_pagination_hint(cursor_of(payload), "cursor")

    async def manage_gti_ioc_stream_notification(
        action: str,
        notification_id: str = "",
        filter: str = "",
    ) -> str:
        """Read or permanently delete IoC Stream notifications."""
        action = (action or "").strip().lower()
        if action not in _STREAM_ACTIONS:
            return f"Error: action must be one of {', '.join(sorted(_STREAM_ACTIONS))}."
        notification_id = (notification_id or "").strip()
        if action in {"get", "delete"} and not notification_id:
            return f"Error: notification_id is required for action '{action}'."
        async with client() as c:
            if action == "get":
                resp, err = await safe_request(
                    c,
                    "get",
                    f"{api}/ioc_stream_notifications/{quote(notification_id, safe='')}",
                    _PROVIDER,
                    f"get IoC stream notification {trunc(notification_id, 60)}",
                    headers=headers,
                    expected_statuses={404},
                )
                if err:
                    if resp is not None and resp.status_code == 404:
                        return (
                            f"Notification '{trunc(notification_id, 80)}' not "
                            "found (notifications expire after 30 days)."
                        )
                    return err
                payload = resp.json() or {}
                obj = data_of(payload)
                if not isinstance(obj, dict):
                    return f"Notification '{trunc(notification_id, 80)}' not found."
                attrs = attributes_of(obj)
                lines = [f"Notification {trunc(obj.get('id'), 100) or notification_id}:"]
                origin = attrs.get("origin")
                if origin:
                    lines.append(f"Origin: {trunc(origin, 60)}")
                when = ts(attrs.get("date"))
                if when:
                    lines.append(f"Date: {when} UTC")
                tags = attrs.get("tags")
                if isinstance(tags, list) and tags:
                    lines.append(f"Tags: {trunc(', '.join(str(t) for t in tags[:10]), 300)}")
                hunting = attrs.get("hunting_info")
                if isinstance(hunting, dict) and hunting.get("rule_name"):
                    lines.append(f"Matched rule: {trunc(hunting.get('rule_name'), 80)}")
                return "\n".join(lines)
            if action == "delete":
                _resp, err = await safe_request(
                    c,
                    "delete",
                    f"{api}/ioc_stream_notifications/{quote(notification_id, safe='')}",
                    _PROVIDER,
                    f"delete IoC stream notification {trunc(notification_id, 60)}",
                    headers=headers,
                )
                if err:
                    return err
                return (
                    f"Notification '{trunc(notification_id, 80)}' permanently "
                    "deleted from the IoC Stream."
                )
            # delete_filtered
            if not filter.strip():
                return (
                    "Error: filter is required for delete_filtered - it uses "
                    "the same filter language as get_gti_ioc_stream (e.g. "
                    "'origin:hunting date:2026-01-01-'). Refusing to delete "
                    "the whole stream without a filter."
                )
            _resp, err = await safe_request(
                c,
                "delete",
                f"{api}/ioc_stream",
                _PROVIDER,
                f"delete IoC stream notifications matching {trunc(filter, 100)}",
                headers=headers,
                params={"filter": filter.strip()},
            )
            if err:
                return err
            return (
                f"All IoC Stream notifications matching '{trunc(filter, 150)}' "
                "were permanently deleted."
            )

    executor.register("get_gti_ioc_stream", get_gti_ioc_stream)
    executor.register(
        "manage_gti_ioc_stream_notification", manage_gti_ioc_stream_notification
    )

    return [
        ToolDefinition(
            name="get_gti_ioc_stream",
            description=(
                "Read the Google TI IoC Stream - the unified feed of IoC "
                "notifications from Livehunt rulesets, Retrohunt jobs, "
                "followed threats, threat profiles, and saved searches. "
                "Filter with e.g. 'origin:hunting', 'entity_type:file', "
                "'source_type:threat_profile', 'source_id:ID', "
                "'notification_tag:RULE', 'date:2026-07-01T00:00:00+' "
                "(combinable). Order date- (newest first, default) or date+. "
                "Notifications expire after 30 days. Returns object ids for "
                "the get_gti_*_assessment tools."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "filter": {
                        "type": "string",
                        "description": (
                            "Filter string - date:, origin:(hunting|subscriptions), "
                            "entity_type:(file|domain|url|ip_address), "
                            "source_type:(hunting_ruleset|retrohunt_job|collection|"
                            "threat_profile|saved_search), source_id:, notification_tag:"
                        ),
                    },
                    "order": {
                        "type": "string",
                        "enum": ["date-", "date+"],
                        "description": "Sort order (date- = newest first, default)",
                    },
                    "limit": {
                        "type": "integer",
                        "default": 10,
                        "description": "Max objects to return (1-40)",
                    },
                    "cursor": {
                        "type": "string",
                        "description": "Pagination cursor from a previous call",
                    },
                    "descriptors_only": {
                        "type": "boolean",
                        "default": False,
                        "description": "Return object descriptors (ids) instead of full objects",
                    },
                },
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="manage_gti_ioc_stream_notification",
            description=(
                "Manage IoC Stream notifications: 'get' one notification's "
                "detail (origin, date, tags, matched rule), 'delete' one "
                "notification by id, or 'delete_filtered' to remove every "
                "notification matching a filter (same filter language as "
                "get_gti_ioc_stream - a filter is mandatory). Deletions are "
                "permanent and cannot be undone."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": sorted(_STREAM_ACTIONS),
                    },
                    "notification_id": {
                        "type": "string",
                        "description": "Notification id (get / delete)",
                    },
                    "filter": {
                        "type": "string",
                        "description": (
                            "Filter for delete_filtered, e.g. 'origin:hunting "
                            "date:2026-01-01-' (required for that action)"
                        ),
                    },
                },
                "required": ["action"],
            },
            source=_SOURCE,
            priority=2,
        ),
    ]
