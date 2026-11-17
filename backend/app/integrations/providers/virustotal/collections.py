"""VirusTotal collections tools - curated IOC sets (files, URLs, domains, IPs)
shared for campaign tracking and reporting.

* ``list_vt_collections`` (``GET /collections``, premium) and
  ``get_vt_collection`` (``GET /collections/{id}``).
* ``create_vt_collection`` (``POST /collections``) - IOCs can be supplied as
  raw text (VirusTotal extracts hashes/URLs/domains/IPs automatically).
* ``manage_vt_collection`` - update metadata, add/remove items, or delete
  (``PATCH/DELETE /collections/{id}``, ``POST/DELETE
  /collections/{id}/{relationship}``).

Browse a collection's contents with ``get_vt_related_objects``
(entity_type='collection', relationship='files' / 'urls' / 'domains' /
'ip_addresses').
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
    csv_list,
    cursor_of,
    data_of,
    gui_link,
    trunc,
    ts,
    url_identifier,
)

_COLLECTION_ACTIONS = frozenset({"update", "add_items", "remove_items", "delete"})
_ITEM_RELATIONSHIPS = {
    "files": "file",
    "urls": "url",
    "domains": "domain",
    "ip_addresses": "ip_address",
}


def _format_collection(obj: dict[str, Any]) -> str:
    attrs = attributes_of(obj)
    counters = attrs.get("counters")
    counts = ""
    if isinstance(counters, dict):
        parts = [
            f"{as_int(counters.get(k))} {label}"
            for k, label in (
                ("files", "files"),
                ("urls", "urls"),
                ("domains", "domains"),
                ("ip_addresses", "IPs"),
            )
            if as_int(counters.get(k))
        ]
        if parts:
            counts = f" [{trunc(', '.join(parts), 200)}]"
    return (
        f"  {trunc(attrs.get('name'), 100) or '?'}{counts} "
        f"(id: {trunc(obj.get('id'), 80) or '?'}, "
        f"updated {ts(attrs.get('last_modification_date')) or '?'})"
    )


def _descriptors(relationship: str, items: list[str]) -> list[dict[str, str]]:
    """Build item descriptors for add/remove-items calls."""
    obj_type = _ITEM_RELATIONSHIPS[relationship]
    descriptors: list[dict[str, str]] = []
    for raw in items:
        value = raw.strip()
        if not value:
            continue
        if relationship == "urls":
            # URLs may be passed raw; the API accepts {"type": "url", "url": ...}
            descriptors.append({"type": obj_type, "url": value})
        else:
            descriptors.append({"type": obj_type, "id": value})
    return descriptors


def register(
    api: str, headers: dict[str, str], executor: CallableToolExecutor
) -> list[ToolDefinition]:
    """Register collection tools and return their definitions."""

    async def list_vt_collections(
        filter: str = "",
        order: str = "",
        limit: int = 10,
        cursor: str = "",
    ) -> str:
        """List IOC collections visible to your account (premium)."""
        params: dict[str, Any] = {"limit": clamp(limit)}
        if filter.strip():
            params["filter"] = filter.strip()
        if order.strip():
            params["order"] = order.strip()
        if cursor.strip():
            params["cursor"] = cursor.strip()
        async with client() as c:
            resp, err = await safe_request(
                c,
                "get",
                f"{api}/collections",
                _PROVIDER,
                "list collections",
                headers=headers,
                params=params,
            )
            if err:
                return err
            payload = resp.json() or {}
        items = data_of(payload)
        items = items if isinstance(items, list) else []
        if not items:
            return "No collections found."
        lines = [f"Collections ({len(items)} shown):"]
        lines.extend(_format_collection(o) for o in items if isinstance(o, dict))
        return "\n".join(lines) + format_pagination_hint(cursor_of(payload), "cursor")

    async def get_vt_collection(collection_id: str) -> str:
        """Read a collection's metadata, description, and IOC counters."""
        collection_id = (collection_id or "").strip()
        if not collection_id:
            return "Error: collection_id is required."
        async with client() as c:
            resp, err = await safe_request(
                c,
                "get",
                f"{api}/collections/{quote(collection_id, safe='')}",
                _PROVIDER,
                f"get collection {trunc(collection_id, 60)}",
                headers=headers,
                expected_statuses={404},
            )
            if err:
                if resp is not None and resp.status_code == 404:
                    return f"Collection '{trunc(collection_id, 60)}' not found."
                return err
            payload = resp.json() or {}
        obj = data_of(payload)
        if not isinstance(obj, dict):
            return f"Collection '{trunc(collection_id, 60)}' not found."
        attrs = attributes_of(obj)
        lines = [
            f"Collection: {trunc(attrs.get('name'), 100) or '?'} "
            f"(id: {trunc(obj.get('id'), 80) or trunc(collection_id, 80)})"
        ]
        if attrs.get("description"):
            lines.append(f"Description: {trunc(attrs.get('description'), 500)}")
        counters = attrs.get("counters")
        if isinstance(counters, dict):
            lines.append(
                "Contents: "
                + trunc(
                    ", ".join(f"{v} {k}" for k, v in counters.items() if isinstance(v, int) and v),
                    300,
                )
            )
        created = ts(attrs.get("creation_date"))
        if created:
            lines.append(f"Created: {created} UTC")
        modified = ts(attrs.get("last_modification_date"))
        if modified:
            lines.append(f"Last modified: {modified} UTC")
        safe_id = trunc(obj.get("id"), 80) or trunc(collection_id, 80)
        lines.append(
            "Browse items with get_vt_related_objects(entity_type='collection', "
            f"entity_id='{safe_id}', relationship='files'|'urls'|'domains'|'ip_addresses')."
        )
        lines.append(f"Link: {gui_link('collections', safe_id)}")
        return "\n".join(lines)

    async def create_vt_collection(
        name: str,
        description: str = "",
        raw_items: str = "",
    ) -> str:
        """Create an IOC collection; IOCs are extracted automatically from raw_items text."""
        name = (name or "").strip()
        if not name:
            return "Error: a collection name is required."
        attributes: dict[str, Any] = {"name": name}
        if description.strip():
            attributes["description"] = description.strip()
        data: dict[str, Any] = {"type": "collection", "attributes": attributes}
        raw = (raw_items or "").strip()
        if raw:
            # The API extracts file hashes, URLs, domains, and IPs from free
            # text passed in data.raw_items (sibling of attributes, per the
            # documented "create from raw text" body) - the simplest
            # agent-friendly path.
            data["raw_items"] = raw
        body: dict[str, Any] = {"data": data}
        async with client() as c:
            resp, err = await safe_request(
                c,
                "post",
                f"{api}/collections",
                _PROVIDER,
                f"create collection '{trunc(name, 80)}'",
                headers=headers,
                json=body,
            )
            if err:
                return err
            payload = resp.json() or {}
        obj = data_of(payload)
        new_id = (trunc(obj.get("id"), 80) if isinstance(obj, dict) else "") or "?"
        return f"Collection '{trunc(name, 100)}' created (id: {new_id})."

    async def manage_vt_collection(
        action: str,
        collection_id: str,
        name: str = "",
        description: str = "",
        relationship: str = "",
        items: str = "",
    ) -> str:
        """Update metadata, add/remove IOC items, or delete a collection."""
        action = (action or "").strip().lower()
        if action not in _COLLECTION_ACTIONS:
            return f"Error: action must be one of {', '.join(sorted(_COLLECTION_ACTIONS))}."
        collection_id = (collection_id or "").strip()
        if not collection_id:
            return "Error: collection_id is required."
        base_url = f"{api}/collections/{quote(collection_id, safe='')}"
        async with client() as c:
            if action == "delete":
                _resp, err = await safe_request(
                    c,
                    "delete",
                    base_url,
                    _PROVIDER,
                    f"delete collection {trunc(collection_id, 60)}",
                    headers=headers,
                )
                if err:
                    return err
                return f"Collection '{trunc(collection_id, 80)}' permanently deleted."
            if action == "update":
                attributes: dict[str, Any] = {}
                if name.strip():
                    attributes["name"] = name.strip()
                if description.strip():
                    attributes["description"] = description.strip()
                if not attributes:
                    return "Error: update requires name and/or description."
                body = {
                    "data": {
                        "type": "collection",
                        "id": collection_id,
                        "attributes": attributes,
                    }
                }
                _resp, err = await safe_request(
                    c,
                    "patch",
                    base_url,
                    _PROVIDER,
                    f"update collection {trunc(collection_id, 60)}",
                    headers=headers,
                    json=body,
                )
                if err:
                    return err
                return f"Collection '{trunc(collection_id, 80)}' updated."
            # add_items / remove_items
            relationship = (relationship or "").strip().lower()
            if relationship not in _ITEM_RELATIONSHIPS:
                return f"Error: relationship must be one of {', '.join(sorted(_ITEM_RELATIONSHIPS))}."
            values = csv_list(items)
            if not values:
                return "Error: items (comma-separated IOC values) is required."
            if relationship == "urls" and action == "remove_items":
                # Removal requires object ids - compute the URL identifier.
                descriptors = [
                    {"type": "url", "id": url_identifier(v)} for v in values
                ]
            else:
                descriptors = _descriptors(relationship, values)
            verb = "post" if action == "add_items" else "delete"
            _resp, err = await safe_request(
                c,
                verb,
                f"{base_url}/{relationship}",
                _PROVIDER,
                f"{action} on collection {trunc(collection_id, 60)}",
                headers=headers,
                json={"data": descriptors},
            )
            if err:
                return err
        did = "added to" if action == "add_items" else "removed from"
        return (
            f"{len(values)} {relationship} item(s) {did} "
            f"collection '{trunc(collection_id, 80)}'."
        )

    executor.register("list_vt_collections", list_vt_collections)
    executor.register("get_vt_collection", get_vt_collection)
    executor.register("create_vt_collection", create_vt_collection)
    executor.register("manage_vt_collection", manage_vt_collection)

    return [
        ToolDefinition(
            name="list_vt_collections",
            description=(
                "List VirusTotal IOC collections visible to your account "
                "(your own and, with a premium key, threat-actor/campaign "
                "collections). Filter e.g. 'owner:me' or by name; order by "
                "e.g. creation_date, last_modification_date. Returns "
                "collection ids for get_vt_collection."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "filter": {
                        "type": "string",
                        "description": "Optional filter, e.g. owner:me or a name substring",
                    },
                    "order": {
                        "type": "string",
                        "description": "Sort order, e.g. creation_date or last_modification_date",
                    },
                    "limit": {
                        "type": "integer",
                        "default": 10,
                        "description": "Max collections to return (1-40)",
                    },
                    "cursor": {
                        "type": "string",
                        "description": "Pagination cursor from a previous call",
                    },
                },
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="get_vt_collection",
            description=(
                "Read a collection's metadata: name, description, IOC "
                "counters, and dates. Browse its contents with "
                "get_vt_related_objects (entity_type='collection', "
                "relationship='files', 'urls', 'domains', or 'ip_addresses')."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "collection_id": {
                        "type": "string",
                        "description": "Collection id from list_vt_collections or a report link",
                    },
                },
                "required": ["collection_id"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="create_vt_collection",
            description=(
                "Create a VirusTotal IOC collection for campaign tracking or "
                "report sharing. Pass the IOCs as free text in raw_items - "
                "VirusTotal automatically extracts file hashes, URLs, "
                "domains, and IP addresses from it. Add or remove items later "
                "with manage_vt_collection."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Collection name"},
                    "description": {
                        "type": "string",
                        "description": "What this collection tracks (optional)",
                    },
                    "raw_items": {
                        "type": "string",
                        "description": (
                            "Free text containing the IOCs to include - "
                            "hashes, URLs, domains, IPs (auto-extracted)"
                        ),
                    },
                },
                "required": ["name"],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="manage_vt_collection",
            description=(
                "Manage an existing collection: 'update' name/description, "
                "'add_items' or 'remove_items' (specify relationship = files "
                "/ urls / domains / ip_addresses and a comma-separated items "
                "list - hashes for files, raw URLs for urls), or 'delete' the "
                "collection permanently (cannot be undone)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": sorted(_COLLECTION_ACTIONS),
                    },
                    "collection_id": {
                        "type": "string",
                        "description": "Collection id",
                    },
                    "name": {"type": "string", "description": "New name (update)"},
                    "description": {
                        "type": "string",
                        "description": "New description (update)",
                    },
                    "relationship": {
                        "type": "string",
                        "enum": sorted(_ITEM_RELATIONSHIPS),
                        "description": "Item kind for add_items/remove_items",
                    },
                    "items": {
                        "type": "string",
                        "description": (
                            "Comma-separated IOC values - hashes, raw URLs, "
                            "domains, or IPs (add_items/remove_items)"
                        ),
                    },
                },
                "required": ["action", "collection_id"],
            },
            source=_SOURCE,
            priority=2,
        ),
    ]
