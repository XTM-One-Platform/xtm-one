"""Google TI enrichment tools - IoC assessments, batch summaries, Code Insight.

The GTI-specific enrichment surface on top of the classic IoC reports:

* ``get_gti_file_assessment`` / ``get_gti_url_assessment`` /
  ``get_gti_domain_assessment`` / ``get_gti_ip_assessment`` - single-IoC
  reports via ``GET /{type}/{id}`` sent with the ``x-tool`` header so the
  response carries the curated ``gti_assessment`` (verdict, threat score
  0-100, severity) plus Mandiant/VirusTotal context.
* ``get_gti_ioc_summaries`` - batch enrichment via ``POST /iocs/summary``
  (up to 1,000 descriptors per call; this tool caps at 100 per call to keep
  responses readable).
* ``analyse_gti_code`` - Code Insight (``POST /codeinsights/analyse-binary``)
  explains disassembled/decompiled code from a malware-analysis point of
  view. Hard-capped by Google at 50 requests/day.
"""

from __future__ import annotations

import base64
from typing import Any
from urllib.parse import quote

from app.integrations.http import safe_request
from app.tools.base import CallableToolExecutor, ToolDefinition

from ._common import (
    _PROVIDER,
    _SOURCE,
    as_int,
    attributes_of,
    classify_ioc,
    client,
    csv_list,
    data_of,
    format_analysis_stats,
    format_gti_assessment,
    gui_link,
    trunc,
    ts,
    url_identifier,
)

_SUMMARY_BATCH_MAX = 100
_CODE_TYPES = frozenset({"decompiled", "disassembled"})


def _format_relationship_names(obj: dict[str, Any], key: str, label: str) -> str:
    """Render an inlined relationship block (malware families, actors, ...)."""
    relationships = obj.get("relationships")
    if not isinstance(relationships, dict):
        return ""
    rel = relationships.get(key)
    data = rel.get("data") if isinstance(rel, dict) else None
    if not isinstance(data, list) or not data:
        return ""
    names: list[str] = []
    for item in data[:6]:
        if not isinstance(item, dict):
            continue
        attrs = attributes_of(item)
        name = trunc(attrs.get("name"), 80) or trunc(item.get("id"), 80)
        if name:
            names.append(name)
    if not names:
        return ""
    more = f" (+{len(data) - 6} more)" if len(data) > 6 else ""
    return f"{label}: {', '.join(names)}{more}"


def register(
    api: str, headers: dict[str, str], executor: CallableToolExecutor
) -> list[ToolDefinition]:
    """Register enrichment tools and return their definitions."""

    async def _fetch_object(
        path: str, object_id: str, action: str
    ) -> tuple[dict[str, Any] | None, str | None]:
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
        if not isinstance(obj, dict):
            # A 200 whose ``data`` is not an object (list / string / null)
            # means the response shape is not what this tool expects -
            # surface that instead of rendering a misleading empty report.
            return None, (
                f"Error: {_PROVIDER} returned an unexpected response shape "
                f"while trying to {action}. Try again or check the "
                "identifier."
            )
        return obj, None

    def _common_lines(attrs: dict[str, Any]) -> list[str]:
        lines = format_gti_assessment(attrs)
        lines.append(format_analysis_stats(attrs))
        tags = attrs.get("tags")
        if isinstance(tags, list) and tags:
            lines.append(f"Tags: {trunc(', '.join(str(t) for t in tags[:12]), 300)}")
        last_analysis = ts(attrs.get("last_analysis_date"))
        if last_analysis:
            lines.append(f"Last analysed: {last_analysis} UTC")
        return lines

    # -- file assessment ------------------------------------------------------
    async def get_gti_file_assessment(hash: str) -> str:
        """Get the Google TI assessment (verdict, threat score, severity) for a file hash."""
        value = (hash or "").strip()
        if not value:
            return "Error: a file hash (MD5, SHA-1, or SHA-256) is required."
        obj, err = await _fetch_object(
            "files", value, f"get file assessment for {trunc(value, 80)}"
        )
        if err == "__not_found__":
            return f"Google TI has never seen file '{trunc(value, 100)}'."
        if err:
            return err
        attrs = attributes_of(obj)
        lines = [f"File: {trunc(attrs.get('meaningful_name'), 120) or trunc(value, 120)}"]
        sha256 = trunc(attrs.get("sha256"), 80)
        if sha256:
            lines.append(f"SHA-256: {sha256}")
        type_desc = attrs.get("type_description")
        size = attrs.get("size")
        if type_desc or size:
            bits = [trunc(type_desc, 80) or "unknown type"]
            if size:
                bits.append(f"{trunc(size, 20)} bytes")
            lines.append("Type: " + ", ".join(bits))
        lines.extend(_common_lines(attrs))
        ptc = attrs.get("popular_threat_classification")
        if isinstance(ptc, dict) and ptc.get("suggested_threat_label"):
            lines.append(
                f"Suggested threat label: {trunc(ptc.get('suggested_threat_label'), 120)}"
            )
        for key, label in (
            ("malware_families", "Associated malware families"),
            ("threat_actors", "Associated threat actors"),
            ("campaigns", "Associated campaigns"),
        ):
            rendered = _format_relationship_names(obj or {}, key, label)
            if rendered:
                lines.append(rendered)
        first_seen = ts(attrs.get("first_submission_date"))
        if first_seen:
            lines.append(f"First submitted: {first_seen} UTC")
        lines.append(f"Link: {gui_link('file', sha256 or value)}")
        lines.append(
            "Sample: download_gti_file_sample(hash) stores it as Google TI's "
            "password-protected ZIP."
        )
        return "\n".join(lines)

    # -- URL assessment -------------------------------------------------------
    async def get_gti_url_assessment(url: str) -> str:
        """Get the Google TI assessment (verdict, threat score, severity) for a URL."""
        object_id = url_identifier(url)
        if not object_id:
            return "Error: a URL (or GTI URL identifier) is required."
        obj, err = await _fetch_object(
            "urls", object_id, f"get URL assessment for {trunc(url, 120)}"
        )
        if err == "__not_found__":
            return f"Google TI has never analysed URL '{trunc(url, 200)}'."
        if err:
            return err
        attrs = attributes_of(obj)
        lines = [f"URL: {trunc(attrs.get('url'), 200) or trunc(url, 200)}"]
        lines.extend(_common_lines(attrs))
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
        object_key = obj.get("id") if isinstance(obj, dict) else None
        lines.append(f"Link: {gui_link('url', trunc(object_key, 100) or object_id)}")
        return "\n".join(lines)

    # -- domain assessment ----------------------------------------------------
    async def get_gti_domain_assessment(domain: str) -> str:
        """Get the Google TI assessment (verdict, threat score, severity) for a domain."""
        value = (domain or "").strip().lower()
        if not value:
            return "Error: a domain name is required."
        obj, err = await _fetch_object(
            "domains", value, f"get domain assessment for {trunc(value, 120)}"
        )
        if err == "__not_found__":
            return f"Google TI has no report for domain '{trunc(value, 120)}'."
        if err:
            return err
        attrs = attributes_of(obj)
        lines = [f"Domain: {trunc(value, 120)}"]
        lines.extend(_common_lines(attrs))
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
        lines.append(f"Link: {gui_link('domain', value)}")
        return "\n".join(lines)

    # -- IP assessment --------------------------------------------------------
    async def get_gti_ip_assessment(ip: str) -> str:
        """Get the Google TI assessment (verdict, threat score, severity) for an IP address."""
        value = (ip or "").strip()
        if not value:
            return "Error: an IP address is required."
        obj, err = await _fetch_object(
            "ip_addresses", value, f"get IP assessment for {trunc(value, 60)}"
        )
        if err == "__not_found__":
            return f"Google TI has no report for IP address '{trunc(value, 60)}'."
        if err:
            return err
        attrs = attributes_of(obj)
        lines = [f"IP address: {trunc(value, 60)}"]
        lines.extend(_common_lines(attrs))
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
        lines.append(f"Link: {gui_link('ip-address', value)}")
        return "\n".join(lines)

    # -- batch IoC summaries --------------------------------------------------
    def _summary_entry(entry: Any) -> str | None:
        obj = entry.get("data") if isinstance(entry, dict) and "data" in entry else entry
        if not isinstance(obj, dict):
            return None
        attrs = attributes_of(obj)
        ioc_id = trunc(obj.get("id"), 100) or "?"
        kind = trunc(obj.get("type"), 30) or "?"
        assessment = attrs.get("gti_assessment")
        verdict = ""
        if isinstance(assessment, dict):
            v = assessment.get("verdict")
            v_value = v.get("value") if isinstance(v, dict) else v
            s = assessment.get("threat_score")
            s_value = s.get("value") if isinstance(s, dict) else s
            bits = []
            if v_value:
                bits.append(str(v_value).replace("VERDICT_", "").lower())
            if s_value is not None:
                bits.append(f"score {as_int(s_value)}")
            if bits:
                verdict = f" [{', '.join(bits)}]"
        positives = attrs.get("positives")
        detections = f" ({as_int(positives)} detections)" if positives is not None else ""
        return f"  {kind}: {ioc_id}{verdict}{detections}"

    async def get_gti_ioc_summaries(iocs: str) -> str:
        """Batch-enrich up to 100 mixed IoCs (hashes, URLs, domains, IPs) in one call."""
        values = csv_list(iocs)
        if not values:
            return (
                "Error: iocs is required - a comma or newline separated list "
                "of file hashes, URLs, domains, and IP addresses."
            )
        if len(values) > _SUMMARY_BATCH_MAX:
            return (
                f"Error: too many IoCs ({len(values)}). This tool accepts up "
                f"to {_SUMMARY_BATCH_MAX} per call - split the list and call "
                "again."
            )
        descriptors = []
        for value in values:
            kind = classify_ioc(value)
            ioc_id = url_identifier(value) if kind == "url" else value
            descriptors.append({"type": kind, "id": ioc_id})
        async with client(timeout=60) as c:
            resp, err = await safe_request(
                c,
                "post",
                f"{api}/iocs/summary",
                _PROVIDER,
                f"get IoC summaries for {len(descriptors)} IoCs",
                headers=headers,
                json={"iocs": descriptors},
            )
            if err:
                return err
            payload = resp.json() or {}
        entries = data_of(payload)
        if isinstance(entries, dict):
            # Some deployments key entries by IoC id - flatten to a list.
            entries = list(entries.values())
        entries = entries if isinstance(entries, list) else []
        if not entries:
            return "No summaries returned for the requested IoCs."
        lines = [f"IoC summaries ({len(entries)}):"]
        for entry in entries:
            rendered = _summary_entry(entry)
            if rendered:
                lines.append(rendered)
        lines.append(
            "Use the get_gti_*_assessment tools for the full report on any "
            "single IoC."
        )
        return "\n".join(lines)

    # -- Code Insight ----------------------------------------------------------
    async def analyse_gti_code(code: str, code_type: str = "decompiled") -> str:
        """Explain disassembled/decompiled code with Google TI Code Insight (50 calls/day)."""
        value = (code or "").strip()
        if not value:
            return "Error: code is required (the disassembled or decompiled code block)."
        code_type = (code_type or "").strip().lower()
        if code_type not in _CODE_TYPES:
            return f"Error: code_type must be one of {', '.join(sorted(_CODE_TYPES))}."
        code_b64 = base64.b64encode(value.encode("utf-8")).decode("ascii")
        async with client(timeout=120) as c:
            resp, err = await safe_request(
                c,
                "post",
                f"{api}/codeinsights/analyse-binary",
                _PROVIDER,
                "analyse code with Code Insight",
                headers=headers,
                json={"data": {"code": code_b64, "code_type": code_type}},
            )
            if err:
                return err
            payload = resp.json() or {}
        encoded = data_of(payload)
        if not isinstance(encoded, str) or not encoded:
            return "Code Insight returned no analysis for this code block."
        try:
            decoded = base64.b64decode(encoded).decode("utf-8", errors="replace")
        except (ValueError, TypeError):
            return "Code Insight returned an unreadable (non-base64) analysis payload."
        return "Code Insight analysis:\n" + trunc(decoded, 4000)

    executor.register("get_gti_file_assessment", get_gti_file_assessment)
    executor.register("get_gti_url_assessment", get_gti_url_assessment)
    executor.register("get_gti_domain_assessment", get_gti_domain_assessment)
    executor.register("get_gti_ip_assessment", get_gti_ip_assessment)
    executor.register("get_gti_ioc_summaries", get_gti_ioc_summaries)
    executor.register("analyse_gti_code", analyse_gti_code)

    return [
        ToolDefinition(
            name="get_gti_file_assessment",
            description=(
                "Enrich a file hash (MD5, SHA-1, or SHA-256) with Google "
                "Threat Intelligence: the curated GTI assessment (verdict, "
                "threat score 0-100, severity), multi-engine detections, "
                "suggested threat label, associated malware families / threat "
                "actors / campaigns, and the report link. Use for malware "
                "triage when you need Google's curated verdict rather than "
                "raw engine counts."
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
            name="get_gti_url_assessment",
            description=(
                "Enrich a URL with Google Threat Intelligence: the curated "
                "GTI assessment (verdict, threat score, severity), "
                "multi-engine detections, content categories, final redirect "
                "target, and the report link. Pass the raw URL - the "
                "identifier is computed automatically."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "Full URL, e.g. https://example.com/path (or a GTI URL identifier)",
                    },
                },
                "required": ["url"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="get_gti_domain_assessment",
            description=(
                "Enrich a domain with Google Threat Intelligence: the curated "
                "GTI assessment (verdict, threat score, severity), "
                "multi-engine detections, content categories, registrar and "
                "registration date, and the report link."
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
            name="get_gti_ip_assessment",
            description=(
                "Enrich an IP address with Google Threat Intelligence: the "
                "curated GTI assessment (verdict, threat score, severity), "
                "multi-engine detections, AS number and owner, country, and "
                "the report link."
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
            name="get_gti_ioc_summaries",
            description=(
                "Batch-enrich up to 100 mixed IoCs (file hashes, URLs, "
                "domains, IP addresses) in a single Google Threat "
                "Intelligence call. IoC types are auto-detected. Returns a "
                "compact per-IoC summary with the GTI verdict / threat score "
                "and detection count - ideal for triaging an IoC list before "
                "drilling into individual get_gti_*_assessment reports. Note: "
                "the batch is all-or-nothing (one malformed IoC rejects the "
                "whole request)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "iocs": {
                        "type": "string",
                        "description": (
                            "Comma or newline separated IoC values - hashes, "
                            "raw URLs, domains, IPs (max 100)"
                        ),
                    },
                },
                "required": ["iocs"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="analyse_gti_code",
            description=(
                "Analyse a disassembled or decompiled code block with Google "
                "Threat Intelligence Code Insight (Gemini-powered): returns a "
                "natural-language explanation of what the code does, focused "
                "on malware-analysis-relevant behaviour (persistence, "
                "injection, C2, evasion). Pass the raw code text - it is "
                "base64-encoded automatically. CAUTION: Google hard-caps this "
                "endpoint at 50 requests per day - use it sparingly."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "code": {
                        "type": "string",
                        "description": "The disassembled or decompiled code block (raw text)",
                    },
                    "code_type": {
                        "type": "string",
                        "enum": ["decompiled", "disassembled"],
                        "default": "decompiled",
                        "description": "Whether the code is decompiled (C-like) or disassembled (asm)",
                    },
                },
                "required": ["code"],
            },
            source=_SOURCE,
            priority=0,
        ),
    ]
