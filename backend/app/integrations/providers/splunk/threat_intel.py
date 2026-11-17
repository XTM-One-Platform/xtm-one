"""Splunk Enterprise Security threat intelligence (IOC management).

Create, list, update, and delete indicators in the ES threat-intel collections
(``ip_intel``, ``file_intel``, ``domain``/``http_intel``, ...) via the
``/services/data/threat_intel/item/{collection}`` endpoint. This is the
"creating intelligence" surface: push IOCs into ES so correlation searches and
the threat-matching framework act on them.

Requires Splunk Enterprise Security.
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
    THREAT_INTEL_COLLECTIONS,
    ApiFn,
    safe_int,
    trunc,
)

_TI_ACTIONS = ("update", "delete")
_TI_PATH = "/services/data/threat_intel/item"


def _validate_collection(collection: str) -> str | None:
    if (collection or "").strip() not in THREAT_INTEL_COLLECTIONS:
        valid = ", ".join(THREAT_INTEL_COLLECTIONS)
        return f"Error: collection must be one of: {valid}."
    return None


def _coerce_item(item: Any) -> tuple[dict | None, str | None]:
    """Accept the indicator as a dict (from the LLM) or a JSON-object string."""
    if isinstance(item, dict):
        return (item, None) if item else (None, "Error: item must contain at least one field.")
    if isinstance(item, str):
        s = item.strip()
        if not s:
            return None, "Error: item is required (the indicator fields)."
        try:
            parsed = json.loads(s)
        except (ValueError, TypeError):
            return None, "Error: item must be a JSON object of indicator fields, e.g. {\"ip\": \"1.2.3.4\"}."
        if not isinstance(parsed, dict) or not parsed:
            return None, "Error: item must be a non-empty JSON object."
        return parsed, None
    return None, "Error: item must be a JSON object of indicator fields."


def _rows(data: Any) -> list[dict]:
    """Extract indicator rows from a threat-intel response (list or envelope)."""
    if isinstance(data, list):
        return [r for r in data if isinstance(r, dict)]
    if isinstance(data, dict):
        for key in ("items", "entry", "rows"):
            value = data.get(key)
            if isinstance(value, list):
                return [r for r in value if isinstance(r, dict)]
    return []


def register(base_url: str, api: ApiFn, executor: CallableToolExecutor) -> list[ToolDefinition]:
    """Register the Splunk Enterprise Security threat-intel tools."""

    async def list_splunk_es_threat_intel(collection: str, limit: int = 25) -> str:
        """List indicators in an ES threat-intel collection (e.g. ip_intel, file_intel)."""
        cerr = _validate_collection(collection)
        if cerr:
            return cerr
        collection = collection.strip()
        data, err = await api(
            "get",
            f"{_TI_PATH}/{quote(collection, safe='')}",
            f"list threat intel {collection}",
            params={"count": safe_int(limit, 25, lo=1, hi=MAX_RESULTS)},
        )
        if err:
            return err
        rows = _rows(data)

        def _fmt(r: dict) -> str:
            fields = ", ".join(
                f"{k}={trunc(v, 60)}" for k, v in r.items() if not k.startswith("_")
            )
            return f"  _key: {r.get('_key', '?')} | {fields or '(no indicator fields)'}"

        return format_entity_list(
            rows,
            _fmt,
            noun="indicator",
            empty_msg=f"No indicators found in '{collection}'.",
        )

    async def add_splunk_es_threat_intel(collection: str, item: Any) -> str:
        """Add an indicator (IOC) to an ES threat-intel collection (creating intelligence).

        ``item`` is the indicator as a JSON object whose fields match the
        collection (e.g. ip_intel -> {"ip": "1.2.3.4", "domain": "evil.com"};
        file_intel -> {"file_hash": "..."}). The new row is matched by ES
        correlation searches and the threat-matching framework.
        """
        cerr = _validate_collection(collection)
        if cerr:
            return cerr
        parsed, ierr = _coerce_item(item)
        if ierr:
            return ierr
        collection = collection.strip()
        data, err = await api(
            "post",
            f"{_TI_PATH}/{quote(collection, safe='')}",
            f"add threat intel to {collection}",
            data={"item": json.dumps(parsed)},
        )
        if err:
            return err
        if isinstance(data, dict) and data.get("status") is False:
            return f"Error: Splunk rejected the indicator — {data.get('message', 'unknown error')}."
        msg = data.get("message") if isinstance(data, dict) else ""
        return f"Indicator added to '{collection}'." + (f" {msg}" if msg else "")

    async def manage_splunk_es_threat_intel(
        action: str,
        collection: str,
        item_key: str = "",
        item: Any = None,
    ) -> str:
        """Update or delete an existing ES threat-intel indicator (long tail).

        action='update' replaces an indicator's fields (requires item_key + item).
        action='delete' permanently removes an indicator (requires item_key;
        cannot be undone). Get an indicator's _key from list_splunk_es_threat_intel.
        """
        act = (action or "").strip().lower()
        if act not in _TI_ACTIONS:
            return f"Error: action must be one of {', '.join(_TI_ACTIONS)}."
        cerr = _validate_collection(collection)
        if cerr:
            return cerr
        if not item_key or not item_key.strip():
            return "Error: item_key is required (get it from list_splunk_es_threat_intel)."
        collection = collection.strip()
        path = f"{_TI_PATH}/{quote(collection, safe='')}/{quote(item_key.strip(), safe='')}"

        if act == "delete":
            _, err = await api(
                "delete", path, f"delete threat intel {item_key} from {collection}"
            )
            return err or f"Indicator {item_key} deleted from '{collection}'."

        # update
        parsed, ierr = _coerce_item(item)
        if ierr:
            return ierr
        data, err = await api(
            "post",
            path,
            f"update threat intel {item_key} in {collection}",
            data={"item": json.dumps(parsed)},
        )
        if err:
            return err
        if isinstance(data, dict) and data.get("status") is False:
            return f"Error: Splunk rejected the update — {data.get('message', 'unknown error')}."
        return f"Indicator {item_key} updated in '{collection}'."

    # ── register ────────────────────────────────────────────────────────
    executor.register("list_splunk_es_threat_intel", list_splunk_es_threat_intel)
    executor.register("add_splunk_es_threat_intel", add_splunk_es_threat_intel)
    executor.register("manage_splunk_es_threat_intel", manage_splunk_es_threat_intel)

    _collections_desc = "; ".join(
        f"{name} ({fields})" for name, fields in THREAT_INTEL_COLLECTIONS.items()
    )

    return [
        ToolDefinition(
            name="list_splunk_es_threat_intel",
            description=(
                "List indicators (IOCs) in a Splunk Enterprise Security threat-intel "
                "collection. Collections and their key fields: " + _collections_desc + ". "
                "Returns each indicator's _key for follow-up update/delete."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "collection": {
                        "type": "string",
                        "enum": list(THREAT_INTEL_COLLECTIONS.keys()),
                    },
                    "limit": {"type": "integer", "default": 25},
                },
                "required": ["collection"],
            },
            source=SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="add_splunk_es_threat_intel",
            description=(
                "Add an indicator (IOC) to a Splunk Enterprise Security threat-intel "
                "collection so ES correlation searches match on it (creating "
                "intelligence). Provide 'item' as a JSON object matching the "
                "collection's fields, e.g. ip_intel -> {\"ip\": \"1.2.3.4\"}, "
                "file_intel -> {\"file_hash\": \"...\"}, domain via http_intel/ip_intel "
                "-> {\"domain\": \"evil.com\"}."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "collection": {
                        "type": "string",
                        "enum": list(THREAT_INTEL_COLLECTIONS.keys()),
                    },
                    "item": {
                        "type": "object",
                        "description": "Indicator fields matching the collection",
                        "additionalProperties": True,
                    },
                },
                "required": ["collection", "item"],
            },
            source=SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="manage_splunk_es_threat_intel",
            description=(
                "Update or delete an existing ES threat-intel indicator. "
                "action='update' replaces fields (needs item_key + item); "
                "action='delete' permanently removes the indicator (needs item_key; "
                "cannot be undone). Get item_key from list_splunk_es_threat_intel."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": list(_TI_ACTIONS)},
                    "collection": {
                        "type": "string",
                        "enum": list(THREAT_INTEL_COLLECTIONS.keys()),
                    },
                    "item_key": {"type": "string", "description": "Indicator _key"},
                    "item": {
                        "type": "object",
                        "description": "Replacement fields (update only)",
                        "additionalProperties": True,
                    },
                },
                "required": ["action", "collection", "item_key"],
            },
            source=SOURCE,
            priority=2,
        ),
    ]
