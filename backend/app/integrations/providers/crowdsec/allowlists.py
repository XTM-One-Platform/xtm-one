"""CrowdSec Service API tools - centralized allowlists.

Allowlists prevent trusted IPs/networks (scanners, offices, partners) from
being remediated by subscribed Security Engines. The full lifecycle is
consolidated into one ``manage_crowdsec_allowlist`` tool.
"""

from __future__ import annotations

from typing import Any

from app.integrations.http import format_entity_list, safe_request
from app.tools.base import CallableToolExecutor, ToolDefinition

from ._common import (
    _PROVIDER,
    _SOURCE,
    MISSING_SERVICE_KEY_MSG,
    SERVICE_API,
    clamp,
    client,
    dt,
    is_valid_ip_or_network,
    is_valid_object_id,
    page_footer,
    trunc,
    validate_iso_datetime,
)

_ACTIONS = (
    "list",
    "get",
    "create",
    "update",
    "delete",
    "items",
    "add_items",
    "update_item",
    "delete_item",
    "subscribe",
    "unsubscribe",
)

_ENTITY_TYPES = (
    "org",
    "tag",
    "engine",
    "firewall_integration",
    "remediation_component_integration",
    "remediation_component",
    "log_processor",
)

# Actions that require an allowlist_id.
_NEEDS_LIST_ID = frozenset(_ACTIONS) - {"list", "create"}
# Actions that require an item_id.
_NEEDS_ITEM_ID = frozenset({"update_item", "delete_item"})


def _format_allowlist(a: dict[str, Any]) -> str:
    subscribers = a.get("subscribers") or []
    return (
        f"  {a.get('name', '?')} [{a.get('id', '?')}]"
        + (
            f" | items: {a['total_items']}"
            if a.get("total_items") is not None
            else ""
        )
        + (
            f" | subscribers: {len(subscribers)}"
            if isinstance(subscribers, list) and subscribers
            else ""
        )
        + (f" | updated: {dt(a.get('updated_at'))}" if a.get("updated_at") else "")
        + (f"\n    {trunc(a.get('description'), 140)}" if a.get("description") else "")
    )


def _format_item(i: dict[str, Any]) -> str:
    return (
        f"  {i.get('value', '?')} [{i.get('id', '?')}]"
        + (f" | expires: {dt(i.get('expiration'))}" if i.get("expiration") else "")
        + (f" | {trunc(i.get('description'), 100)}" if i.get("description") else "")
    )


def register(
    service_headers: dict[str, str] | None,
    executor: CallableToolExecutor,
) -> list[ToolDefinition]:
    """Register the consolidated allowlist tool."""

    async def manage_crowdsec_allowlist(
        action: str,
        allowlist_id: str = "",
        item_id: str = "",
        name: str = "",
        description: str = "",
        items: list[str] | None = None,
        expiration: str = "",
        subscriber_ids: list[str] | None = None,
        entity_type: str = "",
        entity_id: str = "",
        page: int = 1,
        size: int = 50,
    ) -> str:
        """Full allowlist lifecycle: lists, items, and fleet subscriptions."""
        if service_headers is None:
            return MISSING_SERVICE_KEY_MSG
        act = str(action or "").strip().lower()
        if act not in _ACTIONS:
            return f"Error: action must be one of {', '.join(_ACTIONS)}."
        allowlist_id = str(allowlist_id or "").strip()
        if act in _NEEDS_LIST_ID and not is_valid_object_id(allowlist_id):
            return (
                f"Error: allowlist_id (24-char id, from action='list') is "
                f"required for action '{act}'."
            )
        item_id = str(item_id or "").strip()
        if act in _NEEDS_ITEM_ID and not is_valid_object_id(item_id):
            return (
                f"Error: item_id (24-char id, from action='items') is required "
                f"for action '{act}'."
            )
        base = f"{SERVICE_API}/allowlists"
        page_params = {
            "page": clamp(page, default=1, hi=10_000),
            "size": clamp(size, default=50, hi=100),
        }

        async with client(timeout=60) as http:
            if act == "list":
                resp, err = await safe_request(
                    http,
                    "get",
                    base,
                    _PROVIDER,
                    "list allowlists",
                    headers=service_headers,
                    params=page_params,
                )
                if err:
                    return err
                data = resp.json() or {}
                out = format_entity_list(
                    data.get("items") or [],
                    _format_allowlist,
                    noun="allowlist",
                    total=data.get("total"),
                    empty_msg="No allowlists exist yet - create one with action='create'.",
                )
                return out + page_footer(data)

            if act == "get":
                resp, err = await safe_request(
                    http,
                    "get",
                    f"{base}/{allowlist_id}",
                    _PROVIDER,
                    f"get allowlist {allowlist_id}",
                    headers=service_headers,
                )
                if err:
                    return err
                a = resp.json() or {}
                lines = [_format_allowlist(a).strip()]
                subscribers = a.get("subscribers") or []
                if isinstance(subscribers, list) and subscribers:
                    lines.append("Subscribers:")
                    lines += [
                        f"  {s.get('entity_type', '?')} {s.get('id', '?')}"
                        for s in subscribers[:10]
                        if isinstance(s, dict)
                    ]
                lines.append("Use action='items' to list the allowlisted values.")
                return "\n".join(lines)

            if act == "create":
                name = str(name or "").strip()
                if not name:
                    return "Error: 'name' is required for action 'create'."
                body: dict[str, Any] = {"name": name}
                if str(description or "").strip():
                    body["description"] = str(description).strip()
                resp, err = await safe_request(
                    http,
                    "post",
                    base,
                    _PROVIDER,
                    f"create allowlist '{name}'",
                    headers=service_headers,
                    json=body,
                )
                if err:
                    return err
                created = resp.json() or {}
                return (
                    f"Allowlist created: {name} [{created.get('id', '?')}]. Add "
                    "values with action='add_items', then attach engines with "
                    "action='subscribe'."
                )

            if act == "update":
                body = {}
                if str(name or "").strip():
                    body["name"] = str(name).strip()
                if str(description or "").strip():
                    body["description"] = str(description).strip()
                if not body:
                    return "Error: nothing to update - pass name and/or description."
                _, err = await safe_request(
                    http,
                    "patch",
                    f"{base}/{allowlist_id}",
                    _PROVIDER,
                    f"update allowlist {allowlist_id}",
                    headers=service_headers,
                    json=body,
                )
                if err:
                    return err
                return f"Allowlist {allowlist_id} updated."

            if act == "delete":
                _, err = await safe_request(
                    http,
                    "delete",
                    f"{base}/{allowlist_id}",
                    _PROVIDER,
                    f"delete allowlist {allowlist_id}",
                    headers=service_headers,
                )
                if err:
                    return err
                return (
                    f"Allowlist {allowlist_id} permanently deleted - subscribed "
                    "engines stop honoring it on their next sync."
                )

            if act == "items":
                resp, err = await safe_request(
                    http,
                    "get",
                    f"{base}/{allowlist_id}/items",
                    _PROVIDER,
                    f"list allowlist items {allowlist_id}",
                    headers=service_headers,
                    params=page_params,
                )
                if err:
                    return err
                data = resp.json() or {}
                out = format_entity_list(
                    data.get("items") or [],
                    _format_item,
                    noun="allowlist item",
                    total=data.get("total"),
                    empty_msg=f"Allowlist {allowlist_id} has no items.",
                )
                return out + page_footer(data)

            if act == "add_items":
                candidates = [
                    str(i).strip() for i in (items or []) if str(i or "").strip()
                ]
                cleaned = [i for i in candidates if is_valid_ip_or_network(i)]
                invalid = [i for i in candidates if not is_valid_ip_or_network(i)]
                if not cleaned:
                    return (
                        "Error: provide at least one valid IP or CIDR network "
                        "in 'items'."
                    )
                if not str(description or "").strip():
                    return (
                        "Error: 'description' is required when adding allowlist "
                        "items (why these values are trusted)."
                    )
                body = {"items": cleaned, "description": str(description).strip()}
                expiration = str(expiration or "").strip()
                if expiration:
                    exp_err = validate_iso_datetime(expiration, "expiration")
                    if exp_err:
                        return exp_err
                    body["expiration"] = expiration
                _, err = await safe_request(
                    http,
                    "post",
                    f"{base}/{allowlist_id}/items",
                    _PROVIDER,
                    f"add {len(cleaned)} items to allowlist",
                    headers=service_headers,
                    json=body,
                )
                if err:
                    return err
                out = f"Added {len(cleaned)} value(s) to allowlist {allowlist_id}"
                out += f" (expire {expiration})." if expiration else " (no expiration)."
                if invalid:
                    out += f"\nSkipped {len(invalid)} invalid value(s): " + ", ".join(
                        invalid[:20]
                    )
                return out

            if act == "update_item":
                body = {}
                if str(description or "").strip():
                    body["description"] = str(description).strip()
                expiration = str(expiration or "").strip()
                if expiration:
                    exp_err = validate_iso_datetime(expiration, "expiration")
                    if exp_err:
                        return exp_err
                    body["expiration"] = expiration
                if not body:
                    return (
                        "Error: nothing to update - pass description and/or "
                        "expiration."
                    )
                _, err = await safe_request(
                    http,
                    "patch",
                    f"{base}/{allowlist_id}/items/{item_id}",
                    _PROVIDER,
                    f"update allowlist item {item_id}",
                    headers=service_headers,
                    json=body,
                )
                if err:
                    return err
                return f"Allowlist item {item_id} updated."

            if act == "delete_item":
                _, err = await safe_request(
                    http,
                    "delete",
                    f"{base}/{allowlist_id}/items/{item_id}",
                    _PROVIDER,
                    f"delete allowlist item {item_id}",
                    headers=service_headers,
                )
                if err:
                    return err
                return f"Allowlist item {item_id} removed from {allowlist_id}."

            if act == "subscribe":
                etype = str(entity_type or "").strip().lower()
                if etype not in _ENTITY_TYPES:
                    return (
                        f"Error: entity_type must be one of {', '.join(_ENTITY_TYPES)}."
                    )
                ids = [
                    str(i).strip()
                    for i in (subscriber_ids or [])
                    if str(i or "").strip()
                ]
                if not ids:
                    return "Error: subscriber_ids is required for 'subscribe'."
                _, err = await safe_request(
                    http,
                    "post",
                    f"{base}/{allowlist_id}/subscribers",
                    _PROVIDER,
                    f"subscribe to allowlist {allowlist_id}",
                    headers=service_headers,
                    json={"ids": ids, "entity_type": etype},
                )
                if err:
                    return err
                return f"Subscribed {len(ids)} {etype}(s) to allowlist {allowlist_id}."

            # unsubscribe
            eid = str(entity_id or "").strip()
            if not eid or any(c in eid for c in "/?#% \t"):
                return "Error: entity_id is required for 'unsubscribe'."
            _, err = await safe_request(
                http,
                "delete",
                f"{base}/{allowlist_id}/subscribers/{eid}",
                _PROVIDER,
                f"unsubscribe {eid} from allowlist {allowlist_id}",
                headers=service_headers,
            )
            if err:
                return err
            return f"Unsubscribed {eid} from allowlist {allowlist_id}."

    executor.register("manage_crowdsec_allowlist", manage_crowdsec_allowlist)

    return [
        ToolDefinition(
            name="manage_crowdsec_allowlist",
            description=(
                "Manage CrowdSec centralized allowlists - trusted IPs/networks "
                "that subscribed Security Engines must never remediate "
                "(scanners, offices, partners). Actions: 'list', 'get', "
                "'create' (name + optional description), 'update', 'delete' (permanent, "
                "cannot be undone), 'items' (list values), 'add_items' (IPs/"
                "CIDRs with a mandatory description and optional per-entry "
                "expiration), 'update_item', 'delete_item', 'subscribe' "
                "(attach engines/orgs/tags/integrations), 'unsubscribe'. Use "
                "when a blocked IP turns out to be legitimate."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": list(_ACTIONS),
                        "description": "Operation to perform",
                    },
                    "allowlist_id": {
                        "type": "string",
                        "description": "Allowlist id (required for everything except list/create)",
                    },
                    "item_id": {
                        "type": "string",
                        "description": "Item id (update_item/delete_item; from action='items')",
                    },
                    "name": {"type": "string", "description": "Allowlist name (create/update)"},
                    "description": {
                        "type": "string",
                        "description": (
                            "Allowlist description (create/update) or the "
                            "mandatory justification for add_items/update_item"
                        ),
                    },
                    "items": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "IPs or CIDR networks to allowlist (add_items)",
                    },
                    "expiration": {
                        "type": "string",
                        "description": (
                            "ISO 8601 expiration for the entries, e.g. "
                            "2026-12-31T00:00:00Z (add_items/update_item; omit "
                            "for permanent)"
                        ),
                    },
                    "subscriber_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Entity ids to subscribe (subscribe)",
                    },
                    "entity_type": {
                        "type": "string",
                        "enum": list(_ENTITY_TYPES),
                        "description": "Type of the subscriber entities (subscribe)",
                    },
                    "entity_id": {
                        "type": "string",
                        "description": "Subscriber entity id to detach (unsubscribe)",
                    },
                    "page": {"type": "integer", "default": 1, "description": "Page (list/items)"},
                    "size": {"type": "integer", "default": 50, "description": "Page size (list/items)"},
                },
                "required": ["action"],
            },
            source=_SOURCE,
            priority=1,
        ),
    ]
