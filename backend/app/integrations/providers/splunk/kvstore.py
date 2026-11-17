"""Splunk KV Store tools (collections, querying, and pushing records).

The KV Store is Splunk's app-key-value store - the same mechanism the OpenCTI
Splunk connector uses to push enrichment/IOC data into Splunk for SPL lookups.
These tools let an agent discover collections, query records, and push/update/
delete records (the "pushing info into Splunk" use case).

Records are JSON documents addressed under
``/servicesNS/nobody/{app}/storage/collections/data/{collection}``.
"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import quote

from app.integrations.http import format_entity_list
from app.tools.base import CallableToolExecutor, ToolDefinition

from ._common import (
    MAX_RESULTS,
    SOURCE,
    ApiFn,
    content,
    entries,
    safe_int,
    trunc,
)

_KV_ACTIONS = ("add", "update", "delete")


def _collections_path(app: str) -> str:
    return f"/servicesNS/nobody/{quote((app or 'search').strip(), safe='')}/storage/collections"


def _coerce_record(record: Any) -> tuple[dict | None, str | None]:
    """Accept a record as a dict (from the LLM) or a JSON-object string."""
    if isinstance(record, dict):
        return (record, None) if record else (None, "Error: record must contain at least one field.")
    if isinstance(record, str):
        s = record.strip()
        if not s:
            return None, "Error: a record (JSON object) is required."
        try:
            parsed = json.loads(s)
        except (ValueError, TypeError):
            return None, "Error: record must be a JSON object, e.g. {\"ip\": \"1.2.3.4\", \"note\": \"...\"}."
        if not isinstance(parsed, dict) or not parsed:
            return None, "Error: record must be a non-empty JSON object."
        return parsed, None
    return None, "Error: record must be a JSON object."


def register(base_url: str, api: ApiFn, executor: CallableToolExecutor) -> list[ToolDefinition]:
    """Register the Splunk KV Store tools."""

    async def list_splunk_kvstore_collections(app: str = "search", limit: int = 50) -> str:
        """List KV Store collections in an app (default 'search')."""
        data, err = await api(
            "get",
            f"{_collections_path(app)}/config",
            "list kvstore collections",
            params={"count": safe_int(limit, 50, lo=1, hi=MAX_RESULTS)},
        )
        if err:
            return err

        def _fmt(e: dict) -> str:
            c = content(e)
            fields = [k for k in c if k.startswith("field.")]
            return (
                f"  {e.get('name', '?')}"
                + (f" | fields: {', '.join(f.split('.', 1)[1] for f in fields[:10])}" if fields else "")
            )

        return format_entity_list(
            entries(data),
            _fmt,
            noun="collection",
            empty_msg=f"No KV Store collections found in app '{app or 'search'}'.",
        )

    async def query_splunk_kvstore(
        collection: str, app: str = "search", query: str = "", limit: int = 25
    ) -> str:
        """Query records in a KV Store collection.

        ``query`` is an optional MongoDB-style JSON filter (e.g.
        '{"ip": "1.2.3.4"}' or '{"score": {"$gte": 80}}'); omit it to list
        records. Returns each record's _key for follow-up update/delete.
        """
        if not collection or not collection.strip():
            return "Error: a KV Store collection name is required."
        params: dict[str, Any] = {"limit": safe_int(limit, 25, lo=1, hi=MAX_RESULTS)}
        if query and query.strip():
            params["query"] = query.strip()
        data, err = await api(
            "get",
            f"{_collections_path(app)}/data/{quote(collection.strip(), safe='')}",
            f"query kvstore {collection}",
            params=params,
        )
        if err:
            return err
        records = data if isinstance(data, list) else []
        records = [r for r in records if isinstance(r, dict)]

        def _fmt(r: dict) -> str:
            fields = ", ".join(
                f"{k}={trunc(v, 60)}" for k, v in r.items() if not k.startswith("_")
            )
            return f"  _key: {r.get('_key', '?')} | {fields or '(no fields)'}"

        return format_entity_list(
            records,
            _fmt,
            noun="record",
            empty_msg=f"No records found in collection '{collection}'.",
        )

    async def manage_splunk_kvstore_record(
        action: str,
        collection: str,
        app: str = "search",
        record: Any = None,
        key: str = "",
    ) -> str:
        """Add, update, or delete a KV Store record (push info into Splunk).

        action='add' inserts a new record (requires record JSON; returns its _key).
        action='update' replaces a record (requires key + record).
        action='delete' permanently removes a record (requires key; cannot be undone).
        """
        act = (action or "").strip().lower()
        if act not in _KV_ACTIONS:
            return f"Error: action must be one of {', '.join(_KV_ACTIONS)}."
        if not collection or not collection.strip():
            return "Error: a KV Store collection name is required."
        collection = collection.strip()
        data_path = f"{_collections_path(app)}/data/{quote(collection, safe='')}"

        if act == "delete":
            if not key or not key.strip():
                return "Error: a record key (_key) is required to delete."
            _, err = await api(
                "delete",
                f"{data_path}/{quote(key.strip(), safe='')}",
                f"delete kvstore record {key}",
            )
            return err or f"Record {key} deleted from '{collection}'."

        parsed, rerr = _coerce_record(record)
        if rerr:
            return rerr

        if act == "update":
            if not key or not key.strip():
                return "Error: a record key (_key) is required to update."
            _, err = await api(
                "post",
                f"{data_path}/{quote(key.strip(), safe='')}",
                f"update kvstore record {key}",
                json_body=parsed,
            )
            return err or f"Record {key} updated in '{collection}'."

        # add
        result, err = await api(
            "post", data_path, f"add kvstore record to {collection}", json_body=parsed
        )
        if err:
            return err
        new_key = result.get("_key") if isinstance(result, dict) else None
        return (
            f"Record added to '{collection}'"
            + (f" (_key: {new_key})." if new_key else ".")
        )

    # ── register ────────────────────────────────────────────────────────
    executor.register("list_splunk_kvstore_collections", list_splunk_kvstore_collections)
    executor.register("query_splunk_kvstore", query_splunk_kvstore)
    executor.register("manage_splunk_kvstore_record", manage_splunk_kvstore_record)

    return [
        ToolDefinition(
            name="list_splunk_kvstore_collections",
            description=(
                "List KV Store collections in a Splunk app (default 'search'). The KV "
                "Store holds lookup/enrichment data that SPL searches join against."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "app": {"type": "string", "default": "search"},
                    "limit": {"type": "integer", "default": 50},
                },
                "required": [],
            },
            source=SOURCE,
            priority=2,
        ),
        ToolDefinition(
            name="query_splunk_kvstore",
            description=(
                "Query records in a KV Store collection. Pass an optional MongoDB-style "
                "JSON filter in 'query' (e.g. '{\"ip\": \"1.2.3.4\"}' or "
                "'{\"score\": {\"$gte\": 80}}'); omit it to list records. Returns each "
                "record's _key for follow-up update/delete."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "collection": {"type": "string"},
                    "app": {"type": "string", "default": "search"},
                    "query": {"type": "string", "description": "MongoDB-style JSON filter"},
                    "limit": {"type": "integer", "default": 25},
                },
                "required": ["collection"],
            },
            source=SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="manage_splunk_kvstore_record",
            description=(
                "Add, update, or delete a KV Store record - push enrichment/IOC data "
                "into Splunk for SPL lookups. action='add' inserts (record JSON, "
                "returns _key); 'update' replaces a record (key + record); 'delete' "
                "permanently removes a record (key; cannot be undone)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": list(_KV_ACTIONS)},
                    "collection": {"type": "string"},
                    "app": {"type": "string", "default": "search"},
                    "record": {
                        "type": "object",
                        "description": "Record fields (add/update)",
                        "additionalProperties": True,
                    },
                    "key": {"type": "string", "description": "Record _key (update/delete)"},
                },
                "required": ["action", "collection"],
            },
            source=SOURCE,
            priority=1,
        ),
    ]
