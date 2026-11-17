"""Google TI Threat Landscape tools - curated threat objects.

The Threat Landscape lives under ``/collections``: every curated object
(threat actor, malware family, software/toolkit, campaign, IoC collection,
report, vulnerability, country profile, industry profile) is a collection
with a ``collection_type`` discriminator.

* ``search_gti_threats`` - ``GET /collections?filter=...`` with the full
  documented filter language (collection_type, name, motivation,
  source_region, targeted_region, targeted_industry, origin, dates, ...).
* ``get_gti_threat`` - ``GET /collections/{id}``.
* ``get_gti_threat_related_objects`` - ``GET /collections/{id}/{relationship}``
  (files, urls, domains, ip_addresses, associations, attack_techniques, ...).
* ``search_gti_threat_iocs`` - ``GET /collections/{id}/search`` with the
  intelligence query language, scoped to one threat.
* ``get_gti_threat_mitre_tree`` - ``GET /collections/{id}/mitre_tree``.
* ``get_gti_threat_timeline`` - ``GET /collections/{id}/timeline/events``
  (Mandiant observed actions; campaigns mainly).

Threat Actors, Campaigns, Country and Industry Profiles require a Google TI
Enterprise or Enterprise Plus license - permission errors are surfaced
verbatim so the agent can explain the gap.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from app.integrations.http import format_pagination_hint, safe_request
from app.tools.base import CallableToolExecutor, ToolDefinition

from ._common import (
    _PROVIDER,
    _SOURCE,
    COLLECTION_TYPES,
    as_int,
    attributes_of,
    clamp,
    client,
    cursor_of,
    data_of,
    format_threat_summary,
    gui_link,
    trunc,
    ts,
)

_MITRE_NAMESPACES = frozenset({"enterprise", "mobile", "ics"})
_TTP_SOURCES = frozenset({"all", "operational", "seen_in_iocs"})

# Frequently useful relationships, surfaced in the tool description so the
# model does not have to guess. The API accepts more; the relationship
# argument is passed through verbatim.
_COMMON_RELATIONSHIPS = (
    "files, urls, domains, ip_addresses (the threat's IoCs), associations "
    "(related threat objects), attack_techniques, malware_families, "
    "threat_actors, campaigns, software_toolkits, reports, vulnerabilities, "
    "comments, hunting_rulesets"
)


def register(
    api: str, headers: dict[str, str], executor: CallableToolExecutor
) -> list[ToolDefinition]:
    """Register Threat Landscape tools and return their definitions."""

    async def search_gti_threats(
        query: str = "",
        collection_type: str = "",
        order: str = "",
        limit: int = 10,
        cursor: str = "",
    ) -> str:
        """Search the Google TI Threat Landscape (actors, malware, campaigns, reports, ...)."""
        collection_type = (collection_type or "").strip().lower()
        if collection_type and collection_type not in COLLECTION_TYPES:
            return (
                "Error: collection_type must be one of "
                f"{', '.join(COLLECTION_TYPES)} (or empty for all types)."
            )
        filters = (query or "").strip()
        if collection_type:
            type_clause = f"collection_type:{collection_type}"
            filters = f"{type_clause} {filters}".strip()
        params: dict[str, Any] = {"limit": clamp(limit)}
        if filters:
            params["filter"] = filters
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
                f"search threats ({trunc(filters, 120) or 'all'})",
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
                f"No Threat Landscape objects match '{trunc(filters, 150) or 'all'}'. "
                "Try a broader filter, a wildcard (name:Ransom*), or drop the "
                "collection_type."
            )
        lines = [f"Threat Landscape results ({len(items)} shown):"]
        lines.extend(format_threat_summary(o) for o in items if isinstance(o, dict))
        return "\n".join(lines) + format_pagination_hint(cursor_of(payload), "cursor")

    async def get_gti_threat(threat_id: str) -> str:
        """Read one Threat Landscape object: description, targets, motivations, counters."""
        threat_id = (threat_id or "").strip()
        if not threat_id:
            return "Error: threat_id is required (from search_gti_threats)."
        async with client() as c:
            resp, err = await safe_request(
                c,
                "get",
                f"{api}/collections/{quote(threat_id, safe='')}",
                _PROVIDER,
                f"get threat {trunc(threat_id, 80)}",
                headers=headers,
                expected_statuses={404},
            )
            if err:
                if resp is not None and resp.status_code == 404:
                    return f"Threat object '{trunc(threat_id, 80)}' not found."
                return err
            payload = resp.json() or {}
        obj = data_of(payload)
        if not isinstance(obj, dict):
            return f"Threat object '{trunc(threat_id, 80)}' not found."
        attrs = attributes_of(obj)
        safe_id = trunc(obj.get("id"), 100) or trunc(threat_id, 100)
        lines = [
            f"Threat: {trunc(attrs.get('name'), 120) or '?'} "
            f"[{trunc(attrs.get('collection_type'), 40) or 'unknown type'}] "
            f"(id: {safe_id})"
        ]
        if attrs.get("alt_names_details"):
            alt = attrs.get("alt_names_details")
            if isinstance(alt, list):
                names = ", ".join(
                    trunc(a.get("value"), 60)
                    for a in alt[:8]
                    if isinstance(a, dict) and a.get("value")
                )
                if names:
                    lines.append(f"Aliases: {names}")
        if attrs.get("description"):
            lines.append(f"Description: {trunc(attrs.get('description'), 800)}")
        origin = attrs.get("origin")
        if origin:
            lines.append(f"Origin: {trunc(origin, 80)}")
        for key, label in (
            ("motivations", "Motivations"),
            ("source_regions_hierarchy", "Source regions"),
            ("targeted_regions_hierarchy", "Targeted regions"),
            ("targeted_industries_tree", "Targeted industries"),
        ):
            values = attrs.get(key)
            if isinstance(values, list) and values:
                rendered: list[str] = []
                for item in values[:8]:
                    if isinstance(item, dict):
                        rendered.append(
                            trunc(
                                item.get("value")
                                or item.get("country")
                                or item.get("region")
                                or item.get("industry_group"),
                                60,
                            )
                        )
                    else:
                        rendered.append(trunc(item, 60))
                rendered = [r for r in rendered if r]
                if rendered:
                    lines.append(f"{label}: {', '.join(rendered)}")
        counters = attrs.get("counters")
        if isinstance(counters, dict):
            counts = ", ".join(
                f"{as_int(v)} {k}" for k, v in counters.items() if isinstance(v, int) and v
            )
            if counts:
                lines.append(f"Contents: {trunc(counts, 300)}")
        created = ts(attrs.get("creation_date"))
        if created:
            lines.append(f"Created: {created} UTC")
        modified = ts(attrs.get("last_modification_date"))
        if modified:
            lines.append(f"Last modified: {modified} UTC")
        lines.append(
            "Pivot with get_gti_threat_related_objects / search_gti_threat_iocs "
            "/ get_gti_threat_mitre_tree."
        )
        files = "Files: export_gti_collection_iocs(collection_id, format) stores its IoCs"
        if attrs.get("collection_type") == "report":
            files += "; download_gti_report(report_id) stores the report PDF"
        lines.append(files + ".")
        lines.append(f"Link: {gui_link('collection', safe_id)}")
        return "\n".join(lines)

    def _format_related(obj: dict[str, Any]) -> str:
        obj_type = trunc(obj.get("type"), 40) or "?"
        attrs = attributes_of(obj)
        label = (
            attrs.get("name")
            or attrs.get("meaningful_name")
            or attrs.get("url")
            or attrs.get("host_name")
            or ""
        )
        stats = attrs.get("last_analysis_stats")
        verdict = ""
        if isinstance(stats, dict):
            verdict = f" [{as_int(stats.get('malicious'))} malicious]"
        head = f"  {obj_type}: {trunc(label, 100)} " if label else f"  {obj_type}: "
        return f"{head}(id: {trunc(obj.get('id'), 100)}){verdict}"

    async def get_gti_threat_related_objects(
        threat_id: str,
        relationship: str,
        limit: int = 10,
        cursor: str = "",
    ) -> str:
        """Pivot from a threat object to its related objects (one relationship per call)."""
        threat_id = (threat_id or "").strip()
        if not threat_id:
            return "Error: threat_id is required."
        relationship = (relationship or "").strip()
        if not relationship:
            return "Error: a relationship name is required (e.g. files, associations)."
        params: dict[str, Any] = {"limit": clamp(limit)}
        if cursor.strip():
            params["cursor"] = cursor.strip()
        url = (
            f"{api}/collections/{quote(threat_id, safe='')}/"
            f"{quote(relationship, safe='')}"
        )
        async with client() as c:
            resp, err = await safe_request(
                c,
                "get",
                url,
                _PROVIDER,
                f"list {trunc(relationship, 60)} of threat {trunc(threat_id, 80)}",
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
                f"No {trunc(relationship, 60)} found for threat "
                f"'{trunc(threat_id, 80)}'."
            )
        lines = [
            f"{trunc(relationship, 60)} of threat {trunc(threat_id, 80)} "
            f"({len(items)} shown):"
        ]
        lines.extend(_format_related(o) for o in items if isinstance(o, dict))
        return "\n".join(lines) + format_pagination_hint(cursor_of(payload), "cursor")

    async def search_gti_threat_iocs(
        threat_id: str,
        query: str = "",
        order: str = "",
        limit: int = 10,
        cursor: str = "",
    ) -> str:
        """Search IoCs inside one threat object with the intelligence query language."""
        threat_id = (threat_id or "").strip()
        if not threat_id:
            return "Error: threat_id is required."
        params: dict[str, Any] = {
            # The endpoint requires a query; "p:0+" matches every IoC.
            "query": (query or "").strip() or "p:0+",
            "limit": clamp(limit),
        }
        if order.strip():
            params["order"] = order.strip()
        if cursor.strip():
            params["cursor"] = cursor.strip()
        url = f"{api}/collections/{quote(threat_id, safe='')}/search"
        async with client(timeout=60) as c:
            resp, err = await safe_request(
                c,
                "get",
                url,
                _PROVIDER,
                f"search IoCs in threat {trunc(threat_id, 80)}",
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
                f"No IoCs in threat '{trunc(threat_id, 80)}' match "
                f"'{trunc(params['query'], 100)}'."
            )
        total = None
        meta = payload.get("meta") if isinstance(payload, dict) else None
        if isinstance(meta, dict) and meta.get("total_hits") is not None:
            total = as_int(meta.get("total_hits"))
        header = f"IoCs in threat {trunc(threat_id, 80)} ({len(items)} shown"
        if total is not None:
            header += f" of ~{total}"
        header += "):"
        lines = [header]
        lines.extend(_format_related(o) for o in items if isinstance(o, dict))
        return "\n".join(lines) + format_pagination_hint(cursor_of(payload), "cursor")

    async def get_gti_threat_mitre_tree(
        threat_id: str,
        mitre_namespace: str = "",
        ttp_source: str = "",
    ) -> str:
        """Get the MITRE ATT&CK tactics and techniques associated with a threat."""
        threat_id = (threat_id or "").strip()
        if not threat_id:
            return "Error: threat_id is required."
        filters: list[str] = []
        mitre_namespace = (mitre_namespace or "").strip().lower()
        if mitre_namespace:
            if mitre_namespace not in _MITRE_NAMESPACES:
                return (
                    "Error: mitre_namespace must be one of "
                    f"{', '.join(sorted(_MITRE_NAMESPACES))}."
                )
            filters.append(f"mitre_namespace:{mitre_namespace}")
        ttp_source = (ttp_source or "").strip().lower()
        if ttp_source:
            if ttp_source not in _TTP_SOURCES:
                return f"Error: ttp_source must be one of {', '.join(sorted(_TTP_SOURCES))}."
            filters.append(f"ttp_source:{ttp_source}")
        params: dict[str, Any] = {}
        if filters:
            params["filter"] = " ".join(filters)
        url = f"{api}/collections/{quote(threat_id, safe='')}/mitre_tree"
        async with client() as c:
            resp, err = await safe_request(
                c,
                "get",
                url,
                _PROVIDER,
                f"get MITRE tree of threat {trunc(threat_id, 80)}",
                headers=headers,
                params=params,
            )
            if err:
                return err
            payload = resp.json() or {}
        data = data_of(payload)
        tactics = data.get("tactics") if isinstance(data, dict) else None
        tactics = tactics if isinstance(tactics, list) else []
        if not tactics:
            return f"No MITRE ATT&CK data for threat '{trunc(threat_id, 80)}'."
        lines = [f"MITRE ATT&CK tree for threat {trunc(threat_id, 80)}:"]
        for tactic in tactics[:14]:
            if not isinstance(tactic, dict):
                continue
            lines.append(
                f"  {trunc(tactic.get('id'), 20)} {trunc(tactic.get('name'), 80)}"
            )
            techniques = tactic.get("techniques")
            if isinstance(techniques, list):
                for technique in techniques[:10]:
                    if not isinstance(technique, dict):
                        continue
                    sources = technique.get("source")
                    src = ""
                    if isinstance(sources, list) and sources:
                        src = f" ({', '.join(trunc(s, 20) for s in sources[:2])})"
                    lines.append(
                        f"    {trunc(technique.get('id'), 20)} "
                        f"{trunc(technique.get('name'), 90)}{src}"
                    )
                if len(techniques) > 10:
                    lines.append(f"    (+{len(techniques) - 10} more techniques)")
        return "\n".join(lines)

    async def get_gti_threat_timeline(threat_id: str) -> str:
        """Get the observed-actions timeline of a threat (campaigns mainly)."""
        threat_id = (threat_id or "").strip()
        if not threat_id:
            return "Error: threat_id is required."
        url = f"{api}/collections/{quote(threat_id, safe='')}/timeline/events"
        async with client() as c:
            resp, err = await safe_request(
                c,
                "get",
                url,
                _PROVIDER,
                f"get timeline of threat {trunc(threat_id, 80)}",
                headers=headers,
            )
            if err:
                return err
            payload = resp.json() or {}
        events = data_of(payload)
        events = events if isinstance(events, list) else []
        if not events:
            return f"No timeline events for threat '{trunc(threat_id, 80)}'."
        lines = [f"Timeline of threat {trunc(threat_id, 80)} ({len(events)} events):"]
        for event in events[:25]:
            if not isinstance(event, dict):
                continue
            when = ts(event.get("event_at")) or "?"
            name = trunc(event.get("name"), 100) or "?"
            category = trunc(event.get("event_category"), 40)
            desc = trunc(event.get("description"), 160)
            line = f"  {when} [{category or '?'}] {name}"
            if desc and desc != name:
                line += f" - {desc}"
            lines.append(line)
        if len(events) > 25:
            lines.append(f"  (+{len(events) - 25} more events)")
        return "\n".join(lines)

    executor.register("search_gti_threats", search_gti_threats)
    executor.register("get_gti_threat", get_gti_threat)
    executor.register("get_gti_threat_related_objects", get_gti_threat_related_objects)
    executor.register("search_gti_threat_iocs", search_gti_threat_iocs)
    executor.register("get_gti_threat_mitre_tree", get_gti_threat_mitre_tree)
    executor.register("get_gti_threat_timeline", get_gti_threat_timeline)

    return [
        ToolDefinition(
            name="search_gti_threats",
            description=(
                "Search the Google TI Threat Landscape - curated threat "
                "actors, malware families, software/toolkits, campaigns, IoC "
                "collections, reports, vulnerabilities, country and industry "
                "profiles. Filter with the documented language, e.g. "
                "'motivation:espionage targeted_region:FR', "
                "'name:Ransom* origin:\"Google Threat Intelligence\"', "
                "'creation_date:30d+'. Set collection_type to narrow to one "
                "object type. Order by e.g. relevance-, creation_date-, "
                "last_modification_date-. Returns ids for get_gti_threat. "
                "Threat actors and campaigns need an Enterprise license."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": (
                            "Filter string - free text and/or modifiers like "
                            "motivation:, source_region:, targeted_region:, "
                            "targeted_industry:, origin:, creation_date:"
                        ),
                    },
                    "collection_type": {
                        "type": "string",
                        "enum": list(COLLECTION_TYPES),
                        "description": "Restrict to one threat object type (empty = all)",
                    },
                    "order": {
                        "type": "string",
                        "description": (
                            "Sort order, e.g. relevance-, name+, creation_date-, "
                            "last_modification_date-"
                        ),
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
                },
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="get_gti_threat",
            description=(
                "Read one Threat Landscape object by id: name, aliases, "
                "description, origin, motivations, source/targeted regions, "
                "targeted industries, IoC counters, and dates. Use after "
                "search_gti_threats; pivot deeper with "
                "get_gti_threat_related_objects, search_gti_threat_iocs, and "
                "get_gti_threat_mitre_tree."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "threat_id": {
                        "type": "string",
                        "description": (
                            "Threat object id, e.g. threat-actor--..., "
                            "malware--..., campaign--..., report--..., or an "
                            "IoC collection id"
                        ),
                    },
                },
                "required": ["threat_id"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="get_gti_threat_related_objects",
            description=(
                "Pivot from a Threat Landscape object to its related objects "
                "via a named relationship. Common relationships - "
                f"{_COMMON_RELATIONSHIPS}. Returns each related object's "
                "type, id, name, and malicious-detection count so you can "
                "chain further lookups. Paginate with limit + cursor."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "threat_id": {
                        "type": "string",
                        "description": "Threat object id from search_gti_threats",
                    },
                    "relationship": {
                        "type": "string",
                        "description": "Relationship name, e.g. files, associations, attack_techniques",
                    },
                    "limit": {
                        "type": "integer",
                        "default": 10,
                        "description": "Max related objects to return (1-40)",
                    },
                    "cursor": {
                        "type": "string",
                        "description": "Pagination cursor from a previous call",
                    },
                },
                "required": ["threat_id", "relationship"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="search_gti_threat_iocs",
            description=(
                "Search the IoCs inside one Threat Landscape object using the "
                "GTI intelligence query language - e.g. 'p:5+' (5+ "
                "detections), 'entity:domain', 'tag:exploit', "
                "'gti_score:60+'. Defaults to files; use entity:domain / "
                "entity:ip / entity:url for other IoC types. Empty query "
                "returns every IoC. Order by e.g. gti_score, positives, "
                "first_submission_date."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "threat_id": {
                        "type": "string",
                        "description": "Threat object id from search_gti_threats",
                    },
                    "query": {
                        "type": "string",
                        "description": "Intelligence query (empty = all IoCs)",
                    },
                    "order": {
                        "type": "string",
                        "description": "Sort order, e.g. gti_score, positives, last_submission_date",
                    },
                    "limit": {
                        "type": "integer",
                        "default": 10,
                        "description": "Max IoCs to return (1-40)",
                    },
                    "cursor": {
                        "type": "string",
                        "description": "Pagination cursor from a previous call",
                    },
                },
                "required": ["threat_id"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="get_gti_threat_mitre_tree",
            description=(
                "Get the MITRE ATT&CK tactics and techniques associated with "
                "a Threat Landscape object (threat actor, malware family, "
                "campaign, toolkit, or IoC collection). Filter by "
                "mitre_namespace (enterprise / mobile / ics) and ttp_source "
                "(operational = analyst-linked, seen_in_iocs = observed in "
                "sandbox detonations, all = both)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "threat_id": {
                        "type": "string",
                        "description": "Threat object id from search_gti_threats",
                    },
                    "mitre_namespace": {
                        "type": "string",
                        "enum": ["enterprise", "mobile", "ics"],
                        "description": "MITRE matrix namespace (default enterprise)",
                    },
                    "ttp_source": {
                        "type": "string",
                        "enum": ["all", "operational", "seen_in_iocs"],
                        "description": "TTP association origin (default all)",
                    },
                },
                "required": ["threat_id"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="get_gti_threat_timeline",
            description=(
                "Get the Mandiant observed-actions timeline of a Threat "
                "Landscape object (mainly campaigns): first-observed date, "
                "key events, observed techniques, phishing emails, and "
                "malicious executables, each with a timestamp and category. "
                "Use to reconstruct how a campaign unfolded."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "threat_id": {
                        "type": "string",
                        "description": "Threat object id (campaign ids work best)",
                    },
                },
                "required": ["threat_id"],
            },
            source=_SOURCE,
            priority=0,
        ),
    ]
