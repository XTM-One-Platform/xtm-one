"""Google TI Threat Lists tools - hourly curated IoC feeds.

Categorised Threat Lists are hourly generated IoC packages (ransomware,
malicious network infrastructure, malware, threat actor, trending, mobile,
OS X, Linux, IoT, cryptominer, phishing, first-stage delivery vectors,
vulnerability weaponization, infostealer) - availability depends on the
Google TI license tier.

* ``list_gti_threat_lists`` - ``GET /threat_lists`` (the lists provisioned
  for this key).
* ``get_gti_threat_list`` - ``GET /threat_lists/{id}/latest`` (or
  ``/{id}/{time}`` for up to 7 days of history) with ``type`` and ``query``
  filtering (gti_score / positives / has: modifiers).
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from app.integrations.http import safe_request
from app.tools.base import CallableToolExecutor, ToolDefinition

from ._common import (
    _PROVIDER,
    _SOURCE,
    as_int,
    attributes_of,
    clamp,
    client,
    trunc,
)


def register(
    api: str, headers: dict[str, str], executor: CallableToolExecutor
) -> list[ToolDefinition]:
    """Register Threat List tools and return their definitions."""

    async def list_gti_threat_lists() -> str:
        """List the categorised Threat Lists provisioned for this API key."""
        async with client() as c:
            resp, err = await safe_request(
                c,
                "get",
                f"{api}/threat_lists",
                _PROVIDER,
                "list provisioned threat lists",
                headers=headers,
            )
            if err:
                return err
            payload = resp.json() or {}
        items = payload.get("data") if isinstance(payload, dict) else None
        items = items if isinstance(items, list) else []
        if not items:
            return (
                "No Threat Lists are provisioned for this API key - Threat "
                "Lists require a Google TI license (tier determines which "
                "lists are available)."
            )
        lines = [f"Provisioned Threat Lists ({len(items)}):"]
        for item in items:
            if not isinstance(item, dict):
                continue
            attrs = attributes_of(item)
            desc = trunc(attrs.get("description"), 120)
            lines.append(
                f"  {trunc(item.get('id'), 60) or '?'}"
                + (f" - {desc}" if desc else "")
            )
        lines.append("Fetch IoCs with get_gti_threat_list(threat_list_id=...).")
        return "\n".join(lines)

    def _format_ioc(entry: Any) -> str | None:
        obj = entry.get("data") if isinstance(entry, dict) and "data" in entry else entry
        if not isinstance(obj, dict):
            return None
        attrs = attributes_of(obj)
        kind = trunc(obj.get("type"), 30) or "?"
        ioc_id = trunc(obj.get("id"), 100) or "?"
        bits: list[str] = []
        assessment = attrs.get("gti_assessment")
        if isinstance(assessment, dict):
            score = assessment.get("threat_score")
            score_value = score.get("value") if isinstance(score, dict) else score
            if score_value is not None:
                bits.append(f"score {as_int(score_value)}")
        positives = attrs.get("positives")
        if positives is not None:
            bits.append(f"{as_int(positives)} detections")
        name = trunc(attrs.get("meaningful_name") or attrs.get("url"), 80)
        suffix = f" [{', '.join(bits)}]" if bits else ""
        head = f"  {kind}: {ioc_id}"
        if name and name != ioc_id:
            head += f" ({name})"
        return head + suffix

    async def get_gti_threat_list(
        threat_list_id: str,
        time: str = "",
        ioc_types: str = "",
        query: str = "",
        limit: int = 20,
    ) -> str:
        """Fetch the latest (or a historical) hourly IoC package of a Threat List."""
        threat_list_id = (threat_list_id or "").strip().lower()
        if not threat_list_id:
            return (
                "Error: threat_list_id is required - use list_gti_threat_lists "
                "to see what this key is provisioned for (e.g. ransomware, "
                "malicious-network-infrastructure, phishing)."
            )
        slot = (time or "").strip() or "latest"
        params: dict[str, Any] = {}
        ioc_types = (ioc_types or "").strip()
        if ioc_types:
            params["type"] = ioc_types
        if query.strip():
            params["query"] = query.strip()
        url = (
            f"{api}/threat_lists/{quote(threat_list_id, safe='')}/"
            f"{quote(slot, safe='')}"
        )
        async with client(timeout=90) as c:
            resp, err = await safe_request(
                c,
                "get",
                url,
                _PROVIDER,
                f"get threat list {trunc(threat_list_id, 60)} ({trunc(slot, 20)})",
                headers=headers,
                params=params,
            )
            if err:
                return err
            payload = resp.json() or {}
        iocs = payload.get("iocs") if isinstance(payload, dict) else None
        iocs = iocs if isinstance(iocs, list) else []
        if not iocs:
            return (
                f"Threat list '{trunc(threat_list_id, 60)}' ({trunc(slot, 20)}) "
                "contains no IoCs matching the filters."
            )
        shown = clamp(limit, default=20, hi=100)
        lines = [
            f"Threat list {trunc(threat_list_id, 60)} ({trunc(slot, 20)}) - "
            f"{len(iocs)} IoCs in the package, showing {min(shown, len(iocs))}:"
        ]
        for entry in iocs[:shown]:
            rendered = _format_ioc(entry)
            if rendered:
                lines.append(rendered)
        if len(iocs) > shown:
            lines.append(
                f"  (+{len(iocs) - shown} more in this package - narrow with "
                "query/ioc_types or raise limit)"
            )
        return "\n".join(lines)

    executor.register("list_gti_threat_lists", list_gti_threat_lists)
    executor.register("get_gti_threat_list", get_gti_threat_list)

    return [
        ToolDefinition(
            name="list_gti_threat_lists",
            description=(
                "List the categorised Google TI Threat Lists provisioned for "
                "this API key (e.g. ransomware, "
                "malicious-network-infrastructure, malware, threat-actor, "
                "trending, phishing, cryptominer, infostealer). Availability "
                "is license-tiered. Returns the threat_list_id values for "
                "get_gti_threat_list."
            ),
            input_schema={"type": "object", "properties": {}},
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="get_gti_threat_list",
            description=(
                "Fetch a Google TI Threat List IoC package - the latest "
                "hourly package by default, or a historical one (up to 7 "
                "days) by passing time as YYYYMMDDhh (UTC, packages lag ~2h). "
                "Filter with ioc_types (file,url,domain,ip_address - comma "
                "separated) and query modifiers ('gti_score:60+', "
                "'positives:5+', 'has:malware_families', 'has:threat_actors' "
                "- combinable). Returns each IoC's type, id, GTI score, and "
                "detection count."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "threat_list_id": {
                        "type": "string",
                        "description": (
                            "Threat list id from list_gti_threat_lists, e.g. "
                            "ransomware, phishing, trending"
                        ),
                    },
                    "time": {
                        "type": "string",
                        "description": "Historical package hour as YYYYMMDDhh (empty = latest)",
                    },
                    "ioc_types": {
                        "type": "string",
                        "description": "Comma-separated IoC types: file, url, domain, ip_address",
                    },
                    "query": {
                        "type": "string",
                        "description": (
                            "Filter modifiers, e.g. 'gti_score:60+ positives:5+ "
                            "has:malware_families'"
                        ),
                    },
                    "limit": {
                        "type": "integer",
                        "default": 20,
                        "description": "Max IoCs to display (1-100; the package itself may be larger)",
                    },
                },
                "required": ["threat_list_id"],
            },
            source=_SOURCE,
            priority=0,
        ),
    ]
