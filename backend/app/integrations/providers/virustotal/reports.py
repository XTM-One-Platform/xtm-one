"""VirusTotal IOC report + relationship tools.

The reason SOC/CTI teams reach for VirusTotal: turn a raw indicator into a
multi-engine verdict with context. This module exposes:

* ``get_vt_file_report`` / ``get_vt_url_report`` / ``get_vt_domain_report`` /
  ``get_vt_ip_report`` - full enrichment for a single IOC via
  ``GET /{type}/{id}``.
* ``get_vt_file_behaviour`` - the merged sandbox behaviour summary
  (``GET /files/{hash}/behaviour_summary``).
* ``get_vt_related_objects`` - pivot on any documented relationship
  (``GET /{type}/{id}/{relationship}``) with cursor pagination.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from app.integrations.http import format_pagination_hint, safe_request
from app.tools.base import CallableToolExecutor, ToolDefinition

from ._common import (
    _PROVIDER,
    _SOURCE,
    RELATIONSHIP_ENTITY_PATHS,
    as_int,
    attributes_of,
    clamp,
    client,
    cursor_of,
    data_of,
    format_analysis_stats,
    format_common_attributes,
    format_threat_classification,
    gui_link,
    trunc,
    ts,
    url_identifier,
)

# Frequently useful relationships per entity type, surfaced in the tool
# description so the model does not have to guess. The API accepts many more;
# the relationship argument is passed through verbatim.
_COMMON_RELATIONSHIPS = (
    "file: contacted_domains, contacted_ips, contacted_urls, behaviours, "
    "bundled_files, dropped_files, execution_parents, itw_urls, "
    "collections, comments, votes | "
    "url: last_serving_ip_address, network_location, downloaded_files, "
    "redirects_to, communicating_files, collections | "
    "domain: resolutions, subdomains, siblings, communicating_files, "
    "downloaded_files, historical_whois, historical_ssl_certificates, "
    "urls, collections | "
    "ip: resolutions, communicating_files, downloaded_files, urls, "
    "historical_whois, historical_ssl_certificates, collections | "
    "collection: files, urls, domains, ip_addresses, references, "
    "threat_actors, comments"
)


def register(
    api: str, headers: dict[str, str], executor: CallableToolExecutor
) -> list[ToolDefinition]:
    """Register IOC report + relationship tools and return their definitions."""

    async def _fetch_object(path: str, object_id: str, action: str) -> tuple[dict[str, Any] | None, str | None]:
        url = f"{api}/{path}/{quote(object_id, safe='')}"
        async with client() as c:
            resp, err = await safe_request(
                c,
                "get",
                url,
                _PROVIDER,
                action,
                headers=headers,
                expected_statuses={404},
            )
            if err:
                if resp is not None and resp.status_code == 404:
                    return None, "__not_found__"
                return None, err
            payload = resp.json() or {}
        obj = data_of(payload)
        return (obj if isinstance(obj, dict) else {}), None

    # -- file report --------------------------------------------------------
    async def get_vt_file_report(hash: str) -> str:
        """Enrich a file hash (MD5/SHA-1/SHA-256): multi-engine detections, threat label, sandbox verdicts."""
        value = (hash or "").strip()
        if not value:
            return "Error: a file hash (MD5, SHA-1, or SHA-256) is required."
        obj, err = await _fetch_object("files", value, f"get file report for {trunc(value, 80)}")
        if err == "__not_found__":
            return (
                f"VirusTotal has never seen file '{trunc(value, 100)}'. Upload "
                "it with scan_vt_file to get a verdict."
            )
        if err:
            return err
        attrs = attributes_of(obj)
        # meaningful_name is community-submitted (whoever uploaded the file
        # picked it) - sanitise it like every other untrusted echoed value,
        # and sanitise the user-supplied hash fallback the same way.
        lines = [f"File: {trunc(attrs.get('meaningful_name'), 120) or trunc(value, 120)}"]
        # Hashes are usually hex, but every attribute is untrusted API data -
        # sanitise them like any other echoed value.
        sha256 = trunc(attrs.get("sha256"), 80)
        if sha256:
            lines.append(f"SHA-256: {sha256}")
        md5 = trunc(attrs.get("md5"), 40)
        if md5:
            lines.append(f"MD5: {md5}")
        type_desc = attrs.get("type_description")
        size = attrs.get("size")
        if type_desc or size:
            bits = [trunc(type_desc, 80) or "unknown type"]
            if size:
                bits.append(f"{trunc(size, 20)} bytes")
            lines.append("Type: " + ", ".join(bits))
        lines.append(format_analysis_stats(attrs))
        lines.extend(format_threat_classification(attrs))
        lines.extend(format_common_attributes(attrs))
        first_seen = ts(attrs.get("first_submission_date"))
        if first_seen:
            lines.append(f"First submitted: {first_seen} UTC")
        signature = attrs.get("signature_info")
        if isinstance(signature, dict) and signature.get("verified"):
            lines.append(
                f"Signature: {trunc(signature.get('verified'), 60)} "
                f"({trunc(signature.get('signers'), 120)})"
            )
        lines.append(f"Link: {gui_link('files', sha256 or value)}")
        return "\n".join(lines)

    # -- URL report ---------------------------------------------------------
    async def get_vt_url_report(url: str) -> str:
        """Enrich a URL: multi-engine detections, categories, final redirect target."""
        object_id = url_identifier(url)
        if not object_id:
            return "Error: a URL (or VirusTotal URL identifier) is required."
        obj, err = await _fetch_object("urls", object_id, f"get URL report for {trunc(url, 120)}")
        if err == "__not_found__":
            return (
                f"VirusTotal has never analysed URL '{trunc(url, 200)}'. Submit "
                "it with scan_vt_url to get a verdict."
            )
        if err:
            return err
        attrs = attributes_of(obj)
        lines = [f"URL: {trunc(attrs.get('url'), 200) or trunc(url, 200)}"]
        lines.append(format_analysis_stats(attrs))
        title = attrs.get("title")
        if title:
            lines.append(f"Page title: {trunc(title, 150)}")
        final_url = attrs.get("last_final_url")
        if final_url and final_url != attrs.get("url"):
            lines.append(f"Final URL after redirects: {trunc(final_url, 200)}")
        categories = attrs.get("categories")
        if isinstance(categories, dict) and categories:
            cats = ", ".join(sorted({str(v) for v in categories.values()})[:8])
            lines.append(f"Categories: {trunc(cats, 300)}")
        lines.extend(format_common_attributes(attrs))
        object_key = obj.get("id") if isinstance(obj, dict) else None
        lines.append(f"Link: {gui_link('urls', trunc(object_key, 100) or object_id)}")
        return "\n".join(lines)

    # -- domain report ------------------------------------------------------
    async def get_vt_domain_report(domain: str) -> str:
        """Enrich a domain: multi-engine detections, categories, WHOIS, DNS context."""
        value = (domain or "").strip().lower()
        if not value:
            return "Error: a domain name is required."
        obj, err = await _fetch_object("domains", value, f"get domain report for {trunc(value, 120)}")
        if err == "__not_found__":
            return f"VirusTotal has no report for domain '{trunc(value, 120)}'."
        if err:
            return err
        attrs = attributes_of(obj)
        lines = [f"Domain: {trunc(value, 120)}"]
        lines.append(format_analysis_stats(attrs))
        categories = attrs.get("categories")
        if isinstance(categories, dict) and categories:
            cats = ", ".join(sorted({str(v) for v in categories.values()})[:8])
            lines.append(f"Categories: {trunc(cats, 300)}")
        registrar = attrs.get("registrar")
        if registrar:
            lines.append(f"Registrar: {trunc(registrar, 120)}")
        created = ts(attrs.get("creation_date"))
        if created:
            lines.append(f"Registered: {created} UTC")
        lines.extend(format_common_attributes(attrs))
        whois = attrs.get("whois")
        if whois:
            lines.append(f"WHOIS (excerpt): {trunc(whois, 300)}")
        lines.append(f"Link: {gui_link('domains', value)}")
        return "\n".join(lines)

    # -- IP report ----------------------------------------------------------
    async def get_vt_ip_report(ip: str) -> str:
        """Enrich an IP address: multi-engine detections, ASN/owner, geolocation."""
        value = (ip or "").strip()
        if not value:
            return "Error: an IP address is required."
        obj, err = await _fetch_object("ip_addresses", value, f"get IP report for {trunc(value, 60)}")
        if err == "__not_found__":
            return f"VirusTotal has no report for IP address '{trunc(value, 60)}'."
        if err:
            return err
        attrs = attributes_of(obj)
        lines = [f"IP address: {trunc(value, 60)}"]
        lines.append(format_analysis_stats(attrs))
        owner = attrs.get("as_owner")
        asn = attrs.get("asn")
        if owner or asn:
            lines.append(
                f"AS: {'AS' + trunc(asn, 20) if asn else '?'} "
                f"({trunc(owner, 120) or 'unknown owner'})"
            )
        country = attrs.get("country")
        if country:
            lines.append(f"Country: {trunc(country, 60)}")
        network = attrs.get("network")
        if network:
            lines.append(f"Network: {trunc(network, 60)}")
        lines.extend(format_common_attributes(attrs))
        lines.append(f"Link: {gui_link('ip_addresses', value)}")
        return "\n".join(lines)

    # -- sandbox behaviour summary -------------------------------------------
    async def get_vt_file_behaviour(hash: str) -> str:
        """Get the merged sandbox behaviour summary for a file hash (processes, network, MITRE)."""
        value = (hash or "").strip()
        if not value:
            return "Error: a file hash (MD5, SHA-1, or SHA-256) is required."
        url = f"{api}/files/{quote(value, safe='')}/behaviour_summary"
        async with client(timeout=60) as c:
            resp, err = await safe_request(
                c,
                "get",
                url,
                _PROVIDER,
                f"get behaviour summary for {trunc(value, 80)}",
                headers=headers,
                expected_statuses={404},
            )
            if err:
                if resp is not None and resp.status_code == 404:
                    return f"No sandbox behaviour reports available for '{trunc(value, 80)}'."
                return err
            payload = resp.json() or {}
        summary = data_of(payload)
        if not isinstance(summary, dict) or not summary:
            return f"No sandbox behaviour reports available for '{trunc(value, 80)}'."
        lines = [f"Sandbox behaviour summary for {trunc(value, 80)}:"]

        def _sample(label: str, key: str, formatter: Any = None, limit: int = 8) -> None:
            values = summary.get(key)
            if not isinstance(values, list) or not values:
                return
            rendered: list[str] = []
            for item in values[:limit]:
                rendered.append(trunc(formatter(item) if formatter else item, 120))
            more = f" (+{len(values) - limit} more)" if len(values) > limit else ""
            lines.append(f"{label}: {'; '.join(rendered)}{more}")

        _sample("Verdicts", "verdicts")
        _sample("Behaviour tags", "tags", limit=12)
        _sample(
            "Processes created",
            "processes_created",
        )
        _sample(
            "Commands executed",
            "command_executions",
        )
        _sample("Files written", "files_written")
        _sample("Registry keys set", "registry_keys_set", lambda i: i.get("key") if isinstance(i, dict) else i)
        _sample("Contacted domains", "dns_lookups", lambda i: i.get("hostname") if isinstance(i, dict) else i)
        _sample(
            "IP traffic",
            "ip_traffic",
            lambda i: (
                f"{i.get('destination_ip')}:{i.get('destination_port')}"
                if isinstance(i, dict)
                else i
            ),
        )
        _sample(
            "MITRE ATT&CK techniques",
            "mitre_attack_techniques",
            lambda i: (
                f"{i.get('id')} {trunc(i.get('signature_description'), 60)}"
                if isinstance(i, dict)
                else i
            ),
            limit=10,
        )
        if len(lines) == 1:
            lines.append("(reports exist but contain no summarisable activity)")
        return "\n".join(lines)

    # -- generic relationship pivot ------------------------------------------
    def _format_related(obj: dict[str, Any]) -> str:
        obj_type = trunc(obj.get("type"), 40) or "?"
        obj_id = str(obj.get("id", "?"))
        attrs = attributes_of(obj)
        label = (
            attrs.get("meaningful_name")
            or attrs.get("url")
            or attrs.get("host_name")
            or attrs.get("ip_address")
            or attrs.get("name")
            or ""
        )
        stats = attrs.get("last_analysis_stats")
        verdict = ""
        if isinstance(stats, dict):
            verdict = f" [{as_int(stats.get('malicious'))} malicious]"
        display_id = trunc(obj_id, 80)
        head = f"  {obj_type}: {trunc(label, 100)} " if label else f"  {obj_type}: "
        return f"{head}(id: {display_id}){verdict}"

    async def get_vt_related_objects(
        entity_type: str,
        entity_id: str,
        relationship: str,
        limit: int = 10,
        cursor: str = "",
    ) -> str:
        """Pivot from an IOC or collection to its related objects (one relationship per call)."""
        entity_type = (entity_type or "").strip().lower()
        path = RELATIONSHIP_ENTITY_PATHS.get(entity_type)
        if not path:
            return (
                "Error: entity_type must be one of "
                f"{', '.join(sorted(RELATIONSHIP_ENTITY_PATHS))}."
            )
        relationship = (relationship or "").strip()
        if not relationship:
            return "Error: a relationship name is required (e.g. contacted_ips)."
        object_id = url_identifier(entity_id) if entity_type == "url" else (entity_id or "").strip()
        if not object_id:
            return "Error: entity_id is required."
        params: dict[str, Any] = {"limit": clamp(limit)}
        if cursor.strip():
            params["cursor"] = cursor.strip()
        url = f"{api}/{path}/{quote(object_id, safe='')}/{quote(relationship, safe='')}"
        async with client() as c:
            resp, err = await safe_request(
                c,
                "get",
                url,
                _PROVIDER,
                f"list {trunc(relationship, 60)} of {entity_type} {trunc(entity_id, 80)}",
                headers=headers,
                params=params,
            )
            if err:
                return err
            payload = resp.json() or {}
        items = data_of(payload)
        items = items if isinstance(items, list) else []
        if not items:
            return f"No {trunc(relationship, 60)} found for this {entity_type}."
        lines = [
            f"{trunc(relationship, 60)} of {entity_type} "
            f"{trunc(entity_id, 80)} ({len(items)} shown):"
        ]
        lines.extend(_format_related(o) for o in items if isinstance(o, dict))
        return "\n".join(lines) + format_pagination_hint(cursor_of(payload), "cursor")

    executor.register("get_vt_file_report", get_vt_file_report)
    executor.register("get_vt_url_report", get_vt_url_report)
    executor.register("get_vt_domain_report", get_vt_domain_report)
    executor.register("get_vt_ip_report", get_vt_ip_report)
    executor.register("get_vt_file_behaviour", get_vt_file_behaviour)
    executor.register("get_vt_related_objects", get_vt_related_objects)

    return [
        ToolDefinition(
            name="get_vt_file_report",
            description=(
                "Enrich a file hash (MD5, SHA-1, or SHA-256) with VirusTotal: "
                "detections across 70+ antivirus engines, suggested threat "
                "label and categories, community reputation and votes, file "
                "type/size, signature info, and the report link. Use for "
                "malware triage; if the file is unknown, upload it with "
                "scan_vt_file instead."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "hash": {
                        "type": "string",
                        "description": "File hash - MD5, SHA-1, or SHA-256",
                    },
                },
                "required": ["hash"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="get_vt_url_report",
            description=(
                "Enrich a URL with VirusTotal: detections across 70+ URL "
                "scanners/blocklists, content categories, page title, final "
                "redirect target, community reputation, and the report link. "
                "Pass the raw URL - the identifier is computed automatically. "
                "If the URL was never analysed, submit it with scan_vt_url."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "Full URL, e.g. https://example.com/path (or a VirusTotal URL identifier)",
                    },
                },
                "required": ["url"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="get_vt_domain_report",
            description=(
                "Enrich a domain with VirusTotal: detections across engines, "
                "content categories, registrar + registration date, WHOIS "
                "excerpt, community reputation, and the report link."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "domain": {
                        "type": "string",
                        "description": "Domain name, e.g. example.com",
                    },
                },
                "required": ["domain"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="get_vt_ip_report",
            description=(
                "Enrich an IP address with VirusTotal: detections across "
                "engines, AS number and owner, country, network range, "
                "community reputation, and the report link."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "ip": {
                        "type": "string",
                        "description": "IPv4 or IPv6 address, e.g. 8.8.8.8",
                    },
                },
                "required": ["ip"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="get_vt_file_behaviour",
            description=(
                "Get the merged dynamic-analysis (sandbox) behaviour summary "
                "for a file hash: sandbox verdicts, behaviour tags, processes "
                "and commands, files written, registry keys, DNS lookups, IP "
                "traffic, and MITRE ATT&CK techniques. Use after "
                "get_vt_file_report when you need execution behaviour detail."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "hash": {
                        "type": "string",
                        "description": "File hash - MD5, SHA-1, or SHA-256",
                    },
                },
                "required": ["hash"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="get_vt_related_objects",
            description=(
                "Pivot from a VirusTotal object to its related objects via a "
                "named relationship (infrastructure mapping, campaign "
                "expansion). Common relationships - "
                f"{_COMMON_RELATIONSHIPS}. Returns each related object's type, "
                "id, name, and malicious-detection count so you can chain "
                "further report lookups. Paginate with limit + cursor."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "entity_type": {
                        "type": "string",
                        "enum": sorted(RELATIONSHIP_ENTITY_PATHS),
                        "description": "Type of the source object",
                    },
                    "entity_id": {
                        "type": "string",
                        "description": (
                            "Source object id - hash for files, raw URL for "
                            "urls, domain name, IP address, or collection id"
                        ),
                    },
                    "relationship": {
                        "type": "string",
                        "description": "Relationship name, e.g. contacted_ips, resolutions, subdomains",
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
                "required": ["entity_type", "entity_id", "relationship"],
            },
            source=_SOURCE,
            priority=0,
        ),
    ]
