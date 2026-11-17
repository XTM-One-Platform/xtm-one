"""VirusTotal search + metadata tools.

* ``search_vt`` - the universal lookup (``GET /search``): a hash returns a
  file, a URL returns a URL object, a domain/IP returns its object, and
  ``tag:foo`` style queries return comments.
* ``search_vt_intelligence`` - the premium corpus search
  (``GET /intelligence/search``) with the full modifier language
  (``type:peexe positives:5+ fs:2024-01-01+`` ...).
* ``list_vt_popular_threat_categories`` - the canonical category names used
  in ``popular_threat_classification`` labels.
"""

from __future__ import annotations

from typing import Any

from app.integrations.http import format_pagination_hint, safe_request
from app.tools.base import CallableToolExecutor, ToolDefinition

from ._common import (
    _PROVIDER,
    _SOURCE,
    as_int,
    attributes_of,
    clamp,
    client,
    cursor_of,
    data_of,
    trunc,
)


def _format_hit(obj: dict[str, Any]) -> str:
    """One-line summary of a search hit (any object type)."""
    obj_type = trunc(obj.get("type"), 40) or "?"
    obj_id = str(obj.get("id", "?"))
    attrs = attributes_of(obj)
    label = (
        attrs.get("meaningful_name")
        or attrs.get("url")
        or attrs.get("name")
        or (trunc(attrs.get("text"), 80) if obj_type == "comment" else "")
        or ""
    )
    stats = attrs.get("last_analysis_stats")
    verdict = ""
    if isinstance(stats, dict):
        malicious = as_int(stats.get("malicious"))
        total = sum(as_int(v) for v in stats.values())
        if total:
            verdict = f" [{malicious} malicious of {total} engines]"
    head = f"  {obj_type}: {trunc(label, 100)} " if label else f"  {obj_type}: "
    return f"{head}(id: {trunc(obj_id, 80)}){verdict}"


def register(
    api: str, headers: dict[str, str], executor: CallableToolExecutor
) -> list[ToolDefinition]:
    """Register search + metadata tools and return their definitions."""

    async def search_vt(query: str) -> str:
        """Look up a hash, URL, domain, IP, or comment tag on VirusTotal."""
        query = (query or "").strip()
        if not query:
            return "Error: a search query is required."
        async with client() as c:
            resp, err = await safe_request(
                c,
                "get",
                f"{api}/search",
                _PROVIDER,
                f"search for {trunc(query, 80)}",
                headers=headers,
                params={"query": query},
            )
            if err:
                return err
            payload = resp.json() or {}
        items = data_of(payload)
        items = items if isinstance(items, list) else []
        if not items:
            return (
                f"No VirusTotal results for '{trunc(query, 100)}'. Simple "
                "search only matches exact hashes, URLs, domains, IPs, and "
                "comment tags - use search_vt_intelligence for modifier "
                "queries (premium)."
            )
        lines = [f"VirusTotal results for '{trunc(query, 100)}' ({len(items)}):"]
        lines.extend(_format_hit(o) for o in items if isinstance(o, dict))
        return "\n".join(lines)

    async def search_vt_intelligence(
        query: str,
        limit: int = 10,
        cursor: str = "",
        order: str = "",
        descriptors_only: bool = False,
    ) -> str:
        """Advanced VT Intelligence corpus search with the full modifier language (premium)."""
        query = (query or "").strip()
        if not query:
            return "Error: a search query is required."
        params: dict[str, Any] = {"query": query, "limit": clamp(limit, hi=300)}
        if cursor.strip():
            params["cursor"] = cursor.strip()
        if order.strip():
            params["order"] = order.strip()
        if descriptors_only:
            params["descriptors_only"] = "true"
        async with client(timeout=60) as c:
            resp, err = await safe_request(
                c,
                "get",
                f"{api}/intelligence/search",
                _PROVIDER,
                f"intelligence search for {trunc(query, 80)}",
                headers=headers,
                params=params,
            )
            if err:
                return err
            payload = resp.json() or {}
        items = data_of(payload)
        items = items if isinstance(items, list) else []
        if not items:
            return f"No VT Intelligence results for '{trunc(query, 100)}'."
        lines = [f"VT Intelligence results for '{trunc(query, 100)}' ({len(items)} shown):"]
        lines.extend(_format_hit(o) for o in items if isinstance(o, dict))
        return "\n".join(lines) + format_pagination_hint(cursor_of(payload), "cursor")

    async def list_vt_popular_threat_categories() -> str:
        """List the canonical popular-threat-category names VirusTotal uses in threat labels."""
        async with client() as c:
            resp, err = await safe_request(
                c,
                "get",
                f"{api}/popular_threat_categories",
                _PROVIDER,
                "list popular threat categories",
                headers=headers,
            )
            if err:
                return err
            payload = resp.json() or {}
        items = data_of(payload)
        items = items if isinstance(items, list) else []
        if not items:
            return "No popular threat categories returned."
        names: list[str] = []
        for item in items:
            if isinstance(item, dict):
                names.append(trunc(item.get("value") or item.get("id"), 60))
            else:
                names.append(trunc(item, 60))
        names = [n for n in names if n]
        return "Popular threat categories (" + str(len(names)) + "):\n" + ", ".join(names)

    executor.register("search_vt", search_vt)
    executor.register("search_vt_intelligence", search_vt_intelligence)
    executor.register("list_vt_popular_threat_categories", list_vt_popular_threat_categories)

    return [
        ToolDefinition(
            name="search_vt",
            description=(
                "Universal VirusTotal lookup: pass a file hash, URL, domain, "
                "IP address, or a comment tag (e.g. tag:phishing) and get the "
                "matching object(s) with detection counts. Prefer the "
                "dedicated get_vt_*_report tools when you already know the "
                "IOC type - they return richer detail. Use "
                "search_vt_intelligence for modifier-based corpus queries."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Hash, URL, domain, IP, or tag:NAME query",
                    },
                },
                "required": ["query"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="search_vt_intelligence",
            description=(
                "Advanced VirusTotal Intelligence corpus search using the "
                "full modifier language - e.g. 'type:peexe positives:10+ "
                "fs:2024-01-01+' (files), 'entity:url url:login "
                "p:5+' (URLs), 'entity:domain category:phishing'. Requires a "
                "premium (VT Enterprise) API key. Returns matching objects "
                "with ids for follow-up report lookups; paginate with limit + "
                "cursor; order by e.g. first_submission_date, positives, "
                "size. Set descriptors_only for ids without full objects "
                "(cheaper)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "VT Intelligence query with search modifiers",
                    },
                    "limit": {
                        "type": "integer",
                        "default": 10,
                        "description": "Max results to return (1-300)",
                    },
                    "cursor": {
                        "type": "string",
                        "description": "Pagination cursor from a previous call",
                    },
                    "order": {
                        "type": "string",
                        "description": (
                            "Sort order, e.g. first_submission_date, "
                            "last_submission_date, positives, times_submitted, size"
                        ),
                    },
                    "descriptors_only": {
                        "type": "boolean",
                        "default": False,
                        "description": "Return object descriptors (ids) instead of full objects",
                    },
                },
                "required": ["query"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="list_vt_popular_threat_categories",
            description=(
                "List the canonical malware category names VirusTotal uses in "
                "popular_threat_classification labels (trojan, ransomware, "
                "dropper, ...). Use to normalise classification terms when "
                "building queries or reports."
            ),
            input_schema={"type": "object", "properties": {}},
            source=_SOURCE,
            priority=0,
        ),
    ]
