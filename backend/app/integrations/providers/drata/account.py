"""Drata account tools - Custom connections and the audit-log event stream."""

from __future__ import annotations

from typing import Any

from app.core.egress import egress_client
from app.integrations.http import format_entity_list
from app.tools.base import CallableToolExecutor, ToolDefinition

from . import _common
from ._common import (
    _empty,
    _full_name,
)

#: ``GET /events`` (EventsPublicV2Controller_listEvents) answers only ``userId``
#: and ``connectionId`` unless the related objects are expanded; ``metadata``
#: carries the event's target entity.
_EVENT_EXPAND: tuple[str, ...] = ("user", "connection", "metadata")
_EVENTS_DEFAULT_LIMIT = 50
_EVENTS_MAX_LIMIT = 500
_DESCRIPTION_CHARS = 200
_TARGET_LABEL_KEYS: tuple[str, ...] = ("name", "title", "displayName", "code", "id")


def _event_actor(event: dict[str, Any]) -> str:
    """Who acted: the expanded user, else the connection, else the source."""
    user = event.get("user") or {}
    if user:
        email = user.get("email") or ""
        name = _full_name(user) if (user.get("firstName") or user.get("lastName")) else ""
        label = f"{name} <{email}>" if name and email else (name or email or "user")
        return f"{label} (user #{user.get('id', event.get('userId'))})"
    connection = event.get("connection") or {}
    if connection:
        kind = connection.get("clientType") or "connection"
        alias = connection.get("clientAlias") or connection.get("clientId") or ""
        label = f"{kind} {alias}".strip()
        return f"{label} (connection #{connection.get('id', event.get('connectionId'))})"
    if event.get("userId") is not None:
        return f"user #{event['userId']}"
    if event.get("connectionId") is not None:
        return f"connection #{event['connectionId']}"
    return f"system ({event.get('source') or 'unknown source'})"


def _event_target(event: dict[str, Any]) -> str:
    """The entity the event is about, from ``metadata.targetEntity`` when present."""
    metadata = event.get("metadata") or {}
    target = metadata.get("targetEntity") if isinstance(metadata, dict) else None
    if not isinstance(target, dict) or not target:
        targets = metadata.get("targetEntities") if isinstance(metadata, dict) else None
        if isinstance(targets, list) and targets:
            return f"{len(targets)} entities"
        return ""
    for key in _TARGET_LABEL_KEYS:
        value = target.get(key)
        if value not in (None, ""):
            if key == "id":
                return f"#{value}"
            ident = target.get("id")
            return f"{value} (#{ident})" if ident not in (None, "") else str(value)
    return ""


def _format_event(event: dict[str, Any]) -> str:
    """One line per event: type, category and source, actor, target, time, description."""
    kind = event.get("type") or "UNKNOWN_TYPE"
    origin = "/".join(str(v) for v in (event.get("category"), event.get("source")) if v)
    parts = [f"[{event.get('id')}] {kind}" + (f" ({origin})" if origin else "")]
    parts.append(f"Actor: {_event_actor(event)}")
    target = _event_target(event)
    if target:
        parts.append(f"Target: {target}")
    if event.get("status"):
        parts.append(f"Result: {event['status']}")
    parts.append(f"At: {event.get('createdAt', 'N/A')}")
    line = " | ".join(parts)
    description = " ".join(str(event.get("description") or "").split())
    if description:
        if len(description) > _DESCRIPTION_CHARS:
            description = description[: _DESCRIPTION_CHARS - 3].rstrip() + "..."
        line += f"\n    {description}"
    return line


def register(
    base_url: str,
    headers: dict[str, str],
    upload_headers: dict[str, str],
    executor: CallableToolExecutor,
) -> list[ToolDefinition]:
    """Build the Drata account tools and register their callables."""

    async def list_drata_custom_connections() -> str:
        """List Drata custom connections (integrations)."""
        async with egress_client(timeout=30) as client:
            items, err = await _common._paginated_fetch(client, f"{base_url}/custom-connections", headers, {})
            if err:
                return err
            return format_entity_list(
                items,
                lambda c: (
                    f"[{c.get('id')}] {c.get('name', 'Unnamed')} — "
                    f"Type: {c.get('type', 'N/A')} | "
                    f"State: {c.get('state', 'N/A')} | "
                    f"Created: {c.get('createdAt', 'N/A')}"
                ),
                noun="custom connection",
            )

    async def list_drata_audit_events(
        event_type: str = "",
        actor_id: int = 0,
        category: str = "",
        source: str = "",
        connection_id: int = 0,
        since: str = "",
        until: str = "",
        limit: int = _EVENTS_DEFAULT_LIMIT,
    ) -> str:
        """List Drata audit-log events, newest first, with type, actor and target."""
        params: dict[str, Any] = {
            "expand[]": list(_EVENT_EXPAND),
            "sort": "createdAt",
            "sortDir": "DESC",
        }
        if event_type:
            params["type"] = event_type.strip().upper()
        if category:
            params["category"] = category.strip().upper()
        if source:
            params["source"] = source.strip().upper()
        if actor_id:
            params["userId"] = actor_id
        if connection_id:
            params["connectionId"] = connection_id
        if since:
            params["createdAtStartDate"] = since
        if until:
            params["createdAtEndDate"] = until
        cap = max(1, min(int(limit or _EVENTS_DEFAULT_LIMIT), _EVENTS_MAX_LIMIT))
        async with egress_client(timeout=30) as client:
            items, err, truncated = await _common._paginated_fetch_all(
                client, f"{base_url}/events", headers, params, max_items=cap, page_size=cap
            )
            if err:
                return err
        listing = format_entity_list(items, _format_event, noun="audit event")
        if truncated:
            listing += (
                f"\n[More events match: showing the newest {len(items)}. Narrow with since/until, "
                "event_type, category or source, or raise limit (max "
                f"{_EVENTS_MAX_LIMIT}).]"
            )
        return listing

    executor.register("list_drata_custom_connections", list_drata_custom_connections)
    executor.register("list_drata_audit_events", list_drata_audit_events)

    return [
        ToolDefinition(
            name="list_drata_custom_connections",
            description="List Drata custom connections (integrations).",
            input_schema=_empty,
            source="integration:drata",
            priority=2,
        ),
        ToolDefinition(
            name="list_drata_audit_events",
            description=(
                "List Drata audit-log events, newest first: type, category and source, actor "
                "(user or connection), target entity, time and description. Filter by type, "
                "category, source, user, connection or a creation-time window."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "event_type": {
                        "type": "string",
                        "description": "Drata event type (EventTypeEnum), e.g. COMPANY_DATA_UPDATED",
                    },
                    "actor_id": {"type": "integer", "description": "Drata user id of the actor"},
                    "category": {
                        "type": "string",
                        "description": "Event category (EventCategoryEnum), e.g. POLICY, PERSONNEL, CONNECTION",
                    },
                    "source": {
                        "type": "string",
                        "description": "Event source (EventSourceEnum), e.g. APP, AUTOPILOT, PUBLIC_API",
                    },
                    "connection_id": {"type": "integer", "description": "Drata connection id"},
                    "since": {
                        "type": "string",
                        "description": "ISO 8601 date-time: events created at or after it",
                    },
                    "until": {
                        "type": "string",
                        "description": "ISO 8601 date-time: events created at or before it",
                    },
                    "limit": {
                        "type": "integer",
                        "description": f"Newest events returned (default {_EVENTS_DEFAULT_LIMIT}, max {_EVENTS_MAX_LIMIT})",
                    },
                },
                "required": [],
            },
            source="integration:drata",
            priority=2,
        ),
    ]
