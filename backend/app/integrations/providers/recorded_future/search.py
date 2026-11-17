"""Recorded Future search + entity-resolution tools.

* ``search_rf_entities`` - Connect API v2 ``/v2/{type}/search``: find the
  highest-risk IPs / domains / URLs / hashes / vulnerabilities / malware /
  companies matching risk-score, risk-rule, and date filters (proactive hunting
  and watchlisting).
* ``list_rf_risk_rules`` - ``/v2/{type}/riskrules``: discover the valid risk-
  rule names to feed ``search_rf_entities``'s ``risk_rule`` filter.
* ``resolve_rf_entity`` - ``/entity-match/match``: turn a human-readable name
  (e.g. "Log4Shell", "Cobalt Strike") into the RF entity id required by the
  malware / company lookups, links search, and detection-rule search.
* ``lookup_rf_entity`` - ``/entity-match/entity/{id}``: the reverse direction -
  translate an opaque RF entity id from another response back into its name,
  type, and aliases.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from app.integrations.http import format_entity_list, safe_request
from app.tools.base import CallableToolExecutor, ToolDefinition

from ._common import (
    _PROVIDER,
    _SOURCE,
    DEFAULT_FIELDS,
    ENTITY_TYPES,
    RISK_RULE_ENTITY_TYPES,
    clamp,
    client,
    csv_list,
    risk_emoji,
    trunc,
)

_ORDER_BY = frozenset(
    {
        "riskscore",
        "criticality",
        "created",
        "modified",
        "firstseen",
        "lastseen",
        "totalhits",
        "rules",
    }
)


def register(
    api: str, headers: dict[str, str], executor: CallableToolExecutor
) -> list[ToolDefinition]:
    """Register search + resolution tools and return their definitions."""

    def _fmt_search_hit(item: dict[str, Any]) -> str:
        entity = item.get("entity") or {}
        risk = item.get("risk") or {}
        score = risk.get("score")
        label = risk.get("criticalityLabel")
        risk_part = ""
        if score is not None:
            risk_part = f" - {risk_emoji(label)} {score}/99"
            if label:
                risk_part += f" ({label})"
        return (
            f"  {entity.get('name', '?')} ({entity.get('type', '?')}){risk_part}"
            f"\n    id: {entity.get('id', '?')}"
        )

    async def search_rf_entities(
        entity_type: str,
        min_risk_score: int = 65,
        risk_rule: str = "",
        first_seen: str = "",
        last_seen: str = "",
        order_by: str = "riskscore",
        direction: str = "desc",
        limit: int = 20,
    ) -> str:
        """Search a Recorded Future entity type by risk filters (proactive hunting).

        ``entity_type`` is one of ip / domain / url / hash / vulnerability /
        malware / company. By default returns entities with a risk score >=
        ``min_risk_score`` (set 0 to disable), ordered by risk score descending.
        Optionally filter by a single ``risk_rule`` (discover valid names with
        list_rf_risk_rules) and first/last-seen date windows (absolute
        YYYY-MM-DD or relative like -7d). Returns entity ids you can feed to the
        lookup tools.
        """
        entity_type = (entity_type or "").strip().lower()
        if entity_type not in ENTITY_TYPES:
            return f"Error: entity_type must be one of {', '.join(sorted(ENTITY_TYPES))}."
        order_by = (order_by or "riskscore").strip().lower()
        if order_by not in _ORDER_BY:
            order_by = "riskscore"
        params: dict[str, Any] = {
            "limit": clamp(limit, 20, hi=100),
            "fields": DEFAULT_FIELDS.get(entity_type, "entity,risk"),
            "orderBy": order_by,
            "direction": "asc" if str(direction).lower() == "asc" else "desc",
        }
        # Malware entities carry no risk score, so the risk filter is skipped.
        if entity_type != "malware":
            # Clamp into Recorded Future's documented 0-99 band: an out-of-range
            # floor (e.g. 1000) would otherwise be forwarded as "[1000,]" and
            # yield API errors or confusingly empty results. Garbage -> 0 (off).
            floor = clamp(min_risk_score, default=0, hi=99, lo=0)
            if floor > 0:
                params["riskScore"] = f"[{floor},]"
        if risk_rule.strip():
            params["riskRule"] = risk_rule.strip()
        if first_seen.strip():
            params["firstSeen"] = first_seen.strip()
        if last_seen.strip():
            params["lastSeen"] = last_seen.strip()
        async with client(timeout=60) as c:
            resp, err = await safe_request(
                c,
                "get",
                f"{api}/v2/{entity_type}/search",
                _PROVIDER,
                f"search {entity_type} entities",
                headers=headers,
                params=params,
            )
            if err:
                return err
            payload = resp.json() or {}
        results = ((payload.get("data") or {}).get("results")) or []
        counts = payload.get("counts") or {}
        total = counts.get("total", len(results))
        out = format_entity_list(
            results,
            _fmt_search_hit,
            noun=f"{entity_type} entity",
            total=total,
            empty_msg=f"No {entity_type} entities matched the filters.",
        )
        if total and total > len(results):
            out += f"\n[Showing {len(results)} of {total}; raise limit or page with the API's from offset.]"
        return out

    async def list_rf_risk_rules(entity_type: str) -> str:
        """List the risk rules available for an entity type (names for search_rf_entities).

        ``entity_type`` is one of ip / domain / url / hash / vulnerability /
        company (malware has no risk-rule catalogue). Returns each rule's name,
        criticality, and how many entities currently trigger it.
        """
        entity_type = (entity_type or "").strip().lower()
        if entity_type not in RISK_RULE_ENTITY_TYPES:
            return (
                "Error: entity_type must be one of "
                f"{', '.join(sorted(RISK_RULE_ENTITY_TYPES))} (malware has no "
                "risk rules)."
            )
        async with client() as c:
            resp, err = await safe_request(
                c,
                "get",
                f"{api}/v2/{entity_type}/riskrules",
                _PROVIDER,
                f"list {entity_type} risk rules",
                headers=headers,
            )
            if err:
                return err
            payload = resp.json() or {}
        data = payload.get("data") or {}
        rules = data.get("results") or data.get("rules") or []

        def _fmt(rule: dict[str, Any]) -> str:
            return (
                f"  {rule.get('name', '?')} "
                f"(criticality {rule.get('criticality', '?')}, "
                f"{rule.get('count', '?')} entities)"
                + (f": {trunc(rule.get('description'), 120)}" if rule.get("description") else "")
            )

        return format_entity_list(
            rules,
            _fmt,
            noun="risk rule",
            total=(payload.get("counts") or {}).get("total", len(rules)),
            empty_msg=f"No risk rules returned for {entity_type}.",
        )

    async def resolve_rf_entity(
        name: str, entity_types: str = "", limit: int = 10
    ) -> str:
        """Resolve a name/alias to Recorded Future entity ids via fuzzy matching.

        Use this when you have a human-readable name (a malware family like
        "Cobalt Strike", a vulnerability alias like "Log4Shell", a threat actor,
        a product) and need the RF entity id required by lookup_rf_malware,
        lookup_rf_company, search_rf_links, or search_rf_detection_rules.
        Optionally narrow with ``entity_types`` (comma-separated RF types like
        "Malware,CyberVulnerability"). Results are ranked best-match first.
        """
        name = (name or "").strip()
        if not name:
            return "Error: name is required."
        body: dict[str, Any] = {"name": name, "limit": clamp(limit, 10, hi=100)}
        types = csv_list(entity_types)
        if types:
            body["type"] = types
        async with client() as c:
            resp, err = await safe_request(
                c,
                "post",
                f"{api}/entity-match/match",
                _PROVIDER,
                f"resolve entity '{name}'",
                headers=headers,
                json=body,
            )
            if err:
                return err
            matches = resp.json() or []
        if not isinstance(matches, list) or not matches:
            return f"No Recorded Future entities matched '{name}'."

        def _fmt(m: dict[str, Any]) -> str:
            return f"  {m.get('name', '?')} ({m.get('type', '?')}) - id: {m.get('id', '?')}"

        return format_entity_list(matches, _fmt, noun="match", total=len(matches))

    async def lookup_rf_entity(entity_id: str) -> str:
        """Resolve an RF entity id to its name, type, and aliases (reverse of resolve_rf_entity).

        Use when another tool's response contains an opaque Recorded Future
        entity id (e.g. from search_rf_collective_insights or search_rf_links)
        and you need the human-readable label. Accepts only RF entity ids -
        for names/IPs/CVEs use resolve_rf_entity or the lookup tools.
        """
        eid = (entity_id or "").strip()
        if not eid:
            return "Error: entity_id is required."
        async with client() as c:
            resp, err = await safe_request(
                c,
                "get",
                f"{api}/entity-match/entity/{quote(eid, safe='')}",
                _PROVIDER,
                f"lookup entity {eid}",
                headers=headers,
                expected_statuses={404},
            )
            if err:
                if resp is not None and resp.status_code == 404:
                    return f"No Recorded Future entity found with id '{eid}'."
                return err
            payload = resp.json() or {}
        data = payload.get("data") or {}
        attrs = data.get("attributes") or {}
        name = attrs.get("name", "?")
        lines = [f"{name} ({data.get('type', '?')}) - id: {data.get('id', eid)}"]
        aliases = attrs.get("alias") or attrs.get("common_names") or []
        if isinstance(aliases, list) and aliases:
            lines.append("Aliases: " + ", ".join(str(a) for a in aliases[:10]))
        if attrs.get("is_threat_actor"):
            lines.append("Flagged as a threat actor.")
        return "\n".join(lines)

    executor.register("search_rf_entities", search_rf_entities)
    executor.register("list_rf_risk_rules", list_rf_risk_rules)
    executor.register("resolve_rf_entity", resolve_rf_entity)
    executor.register("lookup_rf_entity", lookup_rf_entity)

    return [
        ToolDefinition(
            name="search_rf_entities",
            description=(
                "Search a Recorded Future entity type (ip / domain / url / hash "
                "/ vulnerability / malware / company) by risk filters to surface "
                "the highest-risk entities for hunting or watchlisting. Filter by "
                "minimum risk score, a single risk rule (see list_rf_risk_rules), "
                "and first/last-seen date windows. Returns entity ids for the "
                "lookup tools."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "entity_type": {
                        "type": "string",
                        "enum": sorted(ENTITY_TYPES),
                    },
                    "min_risk_score": {
                        "type": "integer",
                        "default": 65,
                        "description": "Minimum risk score 0-99 (0 disables the filter)",
                    },
                    "risk_rule": {
                        "type": "string",
                        "description": "Single risk rule name to filter by (see list_rf_risk_rules)",
                    },
                    "first_seen": {
                        "type": "string",
                        "description": "First-seen lower bound (YYYY-MM-DD or relative like -7d)",
                    },
                    "last_seen": {
                        "type": "string",
                        "description": "Last-seen lower bound (YYYY-MM-DD or relative like -7d)",
                    },
                    "order_by": {
                        "type": "string",
                        "enum": sorted(_ORDER_BY),
                        "default": "riskscore",
                    },
                    "direction": {
                        "type": "string",
                        "enum": ["asc", "desc"],
                        "default": "desc",
                    },
                    "limit": {"type": "integer", "default": 20},
                },
                "required": ["entity_type"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="list_rf_risk_rules",
            description=(
                "List the risk rules for a Recorded Future entity type (ip / "
                "domain / url / hash / vulnerability / company) with their "
                "criticality and entity counts. Use to discover valid risk_rule "
                "names for search_rf_entities."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "entity_type": {
                        "type": "string",
                        "enum": sorted(RISK_RULE_ENTITY_TYPES),
                    },
                },
                "required": ["entity_type"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="resolve_rf_entity",
            description=(
                "Resolve a human-readable name or alias (malware family, "
                "vulnerability alias, threat actor, product) to Recorded Future "
                "entity ids via fuzzy matching. Use to get the id required by "
                "lookup_rf_malware, lookup_rf_company, search_rf_links, and "
                "search_rf_detection_rules. Optionally narrow with RF entity "
                "types."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Name/alias to resolve, e.g. 'Log4Shell'",
                    },
                    "entity_types": {
                        "type": "string",
                        "description": (
                            "Optional comma-separated RF entity types, e.g. "
                            "'Malware,CyberVulnerability'"
                        ),
                    },
                    "limit": {"type": "integer", "default": 10},
                },
                "required": ["name"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="lookup_rf_entity",
            description=(
                "Resolve an opaque Recorded Future entity id back to its name, "
                "type, and aliases - the reverse of resolve_rf_entity. Use when "
                "another tool's response contains a bare RF id (e.g. from "
                "search_rf_links or search_rf_collective_insights). Does NOT "
                "accept IPs/CVEs/names directly."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "entity_id": {
                        "type": "string",
                        "description": "RF entity id, e.g. kvXvR5 or ip:1.2.3.4",
                    },
                },
                "required": ["entity_id"],
            },
            source=_SOURCE,
            priority=0,
        ),
    ]
