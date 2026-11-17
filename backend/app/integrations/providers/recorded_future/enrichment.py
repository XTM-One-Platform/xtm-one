"""Recorded Future entity enrichment + SOAR triage tools.

The reason CTI/SOC teams reach for Recorded Future: turn a raw indicator into a
risk verdict with evidence. This module exposes:

* Per-type Intelligence Card lookups (``lookup_rf_ip`` / ``_domain`` / ``_url``
  / ``_hash`` / ``_vulnerability`` / ``_malware`` / ``_company``) - full
  enrichment for a single entity via the Connect API v2 ``/v2/{type}/{id}``.
* ``enrich_rf_indicators`` - one SOAR call (``/soar/v3/enrichment``) that risk-
  scores up to 1,000 mixed indicators at once (bulk alert triage).
* ``triage_rf_indicators`` - SOAR verdict triage (``/soar/v3/triage/contexts/
  {context}``) returning a boolean malicious/clean verdict per indicator for a
  chosen risk context (malware / phishing / c2).
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
    TRIAGE_CONTEXTS,
    band_from_score,
    client,
    csv_list,
    format_related_entities,
    format_risk_block,
    format_timestamps,
    intel_card_url,
    risk_emoji,
    trunc,
)

# SOAR accepts at most 1,000 indicators per request, summed across all types.
_SOAR_MAX = 1000

# Maps the friendly tool argument names to the SOAR request body keys.
_SOAR_GROUPS: tuple[tuple[str, str], ...] = (
    ("ips", "ip"),
    ("domains", "domain"),
    ("urls", "url"),
    ("hashes", "hash"),
    ("vulnerabilities", "vulnerability"),
)


def register(
    api: str, headers: dict[str, str], executor: CallableToolExecutor
) -> list[ToolDefinition]:
    """Register enrichment + triage tools and return their definitions."""

    # ── single-entity Intelligence Card lookup ────────────────────────────
    def _cvss(record: dict[str, Any]) -> str:
        v3 = record.get("cvssv3") or {}
        v2 = record.get("cvss") or {}
        v3s = (v3.get("baseScore") or v3.get("score")) if isinstance(v3, dict) else None
        v2s = v2.get("score") if isinstance(v2, dict) else None
        bits = []
        if v3s is not None:
            bits.append(f"CVSSv3 {v3s}")
        if v2s is not None:
            bits.append(f"CVSSv2 {v2s}")
        return " | ".join(bits)

    def _format_lookup(record: dict[str, Any], entity_type: str) -> str:
        if not isinstance(record, dict) or not record:
            return f"No Recorded Future intelligence available for this {entity_type}."
        entity = record.get("entity") if isinstance(record.get("entity"), dict) else {}
        name = entity.get("name", "?")
        lines = [f"{entity.get('type', entity_type)}: {name}"]
        if entity.get("id"):
            lines.append(f"RF entity id: {entity.get('id')}")

        if entity_type == "vulnerability":
            cvss = _cvss(record)
            if cvss:
                lines.append(cvss)
            if record.get("lifecycleStage"):
                lines.append(f"Exploit lifecycle stage: {record.get('lifecycleStage')}")

        risk = record.get("risk")
        if risk:
            lines.extend(format_risk_block(risk))
        elif entity_type != "malware":
            lines.append("Risk: no Recorded Future risk coverage (score 0).")

        if entity_type == "vulnerability" and record.get("nvdDescription"):
            lines.append(f"\nNVD: {trunc(record.get('nvdDescription'), 500)}")
        elif entity.get("description"):
            lines.append(f"\n{trunc(entity.get('description'), 400)}")

        related = format_related_entities(record)
        if related:
            lines.append(related)
        seen = format_timestamps(record)
        if seen:
            lines.append(seen)
        card = intel_card_url(record)
        if card:
            lines.append(f"Intelligence Card: {card}")
        return "\n".join(lines)

    async def _lookup(entity_type: str, value: str, fields: str) -> str:
        value = (value or "").strip()
        if not value:
            return f"Error: a {entity_type} value is required."
        params = {"fields": fields.strip() or DEFAULT_FIELDS.get(entity_type, "")}
        # ``safe=""`` percent-encodes slashes/colons so a URL or weird id can't
        # break out of the path segment.
        url = f"{api}/v2/{entity_type}/{quote(value, safe='')}"
        async with client() as c:
            resp, err = await safe_request(
                c,
                "get",
                url,
                _PROVIDER,
                f"lookup {entity_type} {value}",
                headers=headers,
                params=params,
                expected_statuses={404},
            )
            if err:
                if resp is not None and resp.status_code == 404:
                    return (
                        f"No Recorded Future intelligence available for "
                        f"{entity_type} '{value}'."
                    )
                return err
            body = resp.json() or {}
        return _format_lookup(body.get("data", body), entity_type)

    async def lookup_rf_ip(ip: str, fields: str = "") -> str:
        """Enrich an IP address: risk score, triggered rules, geolocation, C2/threat-actor links, Intelligence Card."""
        return await _lookup("ip", ip, fields)

    async def lookup_rf_domain(domain: str, fields: str = "") -> str:
        """Enrich a domain: risk score, triggered rules, related infrastructure, Intelligence Card."""
        return await _lookup("domain", domain, fields)

    async def lookup_rf_url(url: str, fields: str = "") -> str:
        """Enrich a URL: risk score, triggered rules, malware associations, Intelligence Card."""
        return await _lookup("url", url, fields)

    async def lookup_rf_hash(hash: str, fields: str = "") -> str:
        """Enrich a file hash (MD5/SHA-1/SHA-256): risk score, malware family, Intelligence Card."""
        return await _lookup("hash", hash, fields)

    async def lookup_rf_vulnerability(cve: str, fields: str = "") -> str:
        """Enrich a CVE: CVSS, exploit lifecycle/active exploitation, linked malware & actors, NVD summary."""
        return await _lookup("vulnerability", cve, fields)

    async def lookup_rf_malware(malware_id: str, fields: str = "") -> str:
        """Enrich a malware family by its RF entity id (use resolve_rf_entity to get the id from a name)."""
        return await _lookup("malware", malware_id, fields)

    async def lookup_rf_company(company_id: str, fields: str = "") -> str:
        """Enrich a company by its RF entity id: risk score, threat context (third-party risk)."""
        return await _lookup("company", company_id, fields)

    # ── SOAR batch enrichment ─────────────────────────────────────────────
    def _collect(values: dict[str, str]) -> tuple[dict[str, list[str]], int]:
        body: dict[str, list[str]] = {}
        total = 0
        for arg, key in _SOAR_GROUPS:
            items = csv_list(values.get(arg, ""))
            if items:
                body[key] = items
                total += len(items)
        companies = csv_list(values.get("companies", ""))
        if companies:
            body["companybydomain"] = companies
            total += len(companies)
        return body, total

    def _format_soar_result(item: dict[str, Any]) -> str:
        entity = item.get("entity") or {}
        risk = item.get("risk") or {}
        score = risk.get("score", 0)
        label = band_from_score(score)
        rule = risk.get("rule") or {}
        most_critical = rule.get("mostCritical") or "no rules triggered"
        count = rule.get("count", 0)
        return (
            f"  {risk_emoji(label)} {entity.get('name', '?')} "
            f"({entity.get('type', '?')}) - score {score}/99 ({label}), "
            f"{count} rule(s); top: {trunc(most_critical, 90)}"
        )

    async def enrich_rf_indicators(
        ips: str = "",
        domains: str = "",
        urls: str = "",
        hashes: str = "",
        vulnerabilities: str = "",
        companies: str = "",
    ) -> str:
        """Batch risk-score many indicators in a single SOAR call (bulk alert triage).

        Each argument is a comma-separated list. Mix types freely; up to 1,000
        indicators total per call. Returns each entity's risk score, criticality
        band, and most-critical triggered rule - ideal for triaging a list of
        IOCs pulled from an alert, SIEM export, or threat feed.
        """
        body, total = _collect(
            {
                "ips": ips,
                "domains": domains,
                "urls": urls,
                "hashes": hashes,
                "vulnerabilities": vulnerabilities,
                "companies": companies,
            }
        )
        if not body:
            return (
                "Error: provide at least one indicator "
                "(ips, domains, urls, hashes, vulnerabilities, or companies)."
            )
        if total > _SOAR_MAX:
            return f"Error: {total} indicators exceeds the SOAR limit of {_SOAR_MAX} per call."
        async with client(timeout=60) as c:
            resp, err = await safe_request(
                c,
                "post",
                f"{api}/soar/v3/enrichment",
                _PROVIDER,
                "enrich indicators",
                headers=headers,
                json=body,
            )
            if err:
                return err
            payload = resp.json() or {}
        results = ((payload.get("data") or {}).get("results")) or []
        counts = payload.get("counts") or {}
        return format_entity_list(
            results,
            _format_soar_result,
            noun="indicator",
            total=counts.get("returned", len(results)),
            empty_msg="No enrichment results returned.",
        )

    # ── SOAR verdict triage ───────────────────────────────────────────────
    def _format_triage_result(item: dict[str, Any]) -> str:
        entity = item.get("entity") or {}
        verdict = item.get("verdict")
        score = item.get("score", item.get("risk", {}).get("score", "?"))
        # Distinguish a definitive clean (verdict False) from a missing/null
        # verdict (unknown) - never assert "clean" when the API omitted the
        # verdict, which would be misleading in an allow/block decision.
        if verdict is True:
            mark = "\U0001f534 MALICIOUS"
        elif verdict is False:
            mark = "\u26aa clean"
        else:
            mark = "\u2754 unknown"
        return (
            f"  {mark} - {entity.get('name', '?')} "
            f"({entity.get('type', '?')}), score {score}"
        )

    async def triage_rf_indicators(
        context: str,
        ips: str = "",
        domains: str = "",
        urls: str = "",
        hashes: str = "",
        vulnerabilities: str = "",
    ) -> str:
        """Get a boolean malicious/clean verdict for indicators against a risk context.

        ``context`` is one of malware / phishing / c2. Recorded Future evaluates
        each indicator's context-specific sub-score against the configured
        threshold and returns a verdict - use this for automated allow/block
        decisions where you want a yes/no rather than a raw score. Arguments are
        comma-separated lists (up to 1,000 indicators total).
        """
        context = (context or "").strip().lower()
        if context not in TRIAGE_CONTEXTS:
            return f"Error: context must be one of {', '.join(sorted(TRIAGE_CONTEXTS))}."
        body, total = _collect(
            {
                "ips": ips,
                "domains": domains,
                "urls": urls,
                "hashes": hashes,
                "vulnerabilities": vulnerabilities,
            }
        )
        if not body:
            return (
                "Error: provide at least one indicator "
                "(ips, domains, urls, hashes, or vulnerabilities)."
            )
        if total > _SOAR_MAX:
            return f"Error: {total} indicators exceeds the SOAR limit of {_SOAR_MAX} per call."
        async with client(timeout=60) as c:
            resp, err = await safe_request(
                c,
                "post",
                f"{api}/soar/v3/triage/contexts/{context}",
                _PROVIDER,
                f"triage indicators ({context})",
                headers=headers,
                json=body,
            )
            if err:
                return err
            payload = resp.json() or {}
        results = ((payload.get("data") or {}).get("results")) or payload.get(
            "results", []
        )
        threshold = payload.get("threshold")
        header = (
            f"Triage verdicts for context '{context}'"
            + (f" (threshold {threshold})" if threshold is not None else "")
            + ":"
        )
        if not results:
            return f"{header}\nNo results returned."
        body_lines = "\n".join(_format_triage_result(r) for r in results)
        return f"{header}\n{body_lines}"

    executor.register("lookup_rf_ip", lookup_rf_ip)
    executor.register("lookup_rf_domain", lookup_rf_domain)
    executor.register("lookup_rf_url", lookup_rf_url)
    executor.register("lookup_rf_hash", lookup_rf_hash)
    executor.register("lookup_rf_vulnerability", lookup_rf_vulnerability)
    executor.register("lookup_rf_malware", lookup_rf_malware)
    executor.register("lookup_rf_company", lookup_rf_company)
    executor.register("enrich_rf_indicators", enrich_rf_indicators)
    executor.register("triage_rf_indicators", triage_rf_indicators)

    _lookup_schema = {
        "type": "object",
        "properties": {
            "fields": {
                "type": "string",
                "description": (
                    "Optional comma-separated Connect API fields to return "
                    "(defaults to a compact high-signal set)."
                ),
            },
        },
    }

    def _single(value_name: str, value_desc: str) -> dict[str, Any]:
        schema = {
            "type": "object",
            "properties": {
                value_name: {"type": "string", "description": value_desc},
                "fields": _lookup_schema["properties"]["fields"],
            },
            "required": [value_name],
        }
        return schema

    return [
        ToolDefinition(
            name="lookup_rf_ip",
            description=(
                "Enrich an IP address with Recorded Future: risk score (0-99), "
                "criticality band, triggered risk rules with evidence, "
                "geolocation, C2 / threat-actor links, and the Intelligence Card "
                "URL. Use for SOC alert triage and investigation."
            ),
            input_schema=_single("ip", "IP address, e.g. 8.8.8.8"),
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="lookup_rf_domain",
            description=(
                "Enrich a domain with Recorded Future: risk score, triggered "
                "rules, related infrastructure and threat actors, and the "
                "Intelligence Card URL."
            ),
            input_schema=_single("domain", "Domain name, e.g. example.com"),
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="lookup_rf_url",
            description=(
                "Enrich a URL with Recorded Future: risk score, triggered rules, "
                "malware associations, and the Intelligence Card URL."
            ),
            input_schema=_single("url", "Full URL, e.g. https://bad.example/path"),
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="lookup_rf_hash",
            description=(
                "Enrich a file hash (MD5/SHA-1/SHA-256) with Recorded Future: "
                "risk score, malware family attribution, and the Intelligence "
                "Card URL."
            ),
            input_schema=_single("hash", "File hash (MD5, SHA-1, or SHA-256)"),
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="lookup_rf_vulnerability",
            description=(
                "Enrich a CVE with Recorded Future: CVSS scores, exploit "
                "lifecycle / active-exploitation status, linked malware and "
                "threat actors, NVD summary, and the Intelligence Card URL. Use "
                "to prioritise patching."
            ),
            input_schema=_single("cve", "CVE identifier, e.g. CVE-2021-44228"),
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="lookup_rf_malware",
            description=(
                "Enrich a malware family by its Recorded Future entity id "
                "(resolve a name to an id first with resolve_rf_entity): "
                "classification, related actors and infrastructure, "
                "Intelligence Card URL."
            ),
            input_schema=_single("malware_id", "RF malware entity id, e.g. JLHNoH"),
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="lookup_rf_company",
            description=(
                "Enrich a company by its Recorded Future entity id: risk score "
                "and third-party threat context. Resolve a company name to an id "
                "with resolve_rf_entity first."
            ),
            input_schema=_single("company_id", "RF company entity id"),
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="enrich_rf_indicators",
            description=(
                "Batch-enrich up to 1,000 mixed indicators in ONE Recorded "
                "Future SOAR call and get each one's risk score, criticality "
                "band, and most-critical triggered rule. Prefer this over many "
                "single lookups when triaging a list of IOCs (from an alert, "
                "SIEM export, or feed). Each argument is a comma-separated list."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "ips": {"type": "string", "description": "Comma-separated IPs"},
                    "domains": {
                        "type": "string",
                        "description": "Comma-separated domains",
                    },
                    "urls": {"type": "string", "description": "Comma-separated URLs"},
                    "hashes": {
                        "type": "string",
                        "description": "Comma-separated file hashes",
                    },
                    "vulnerabilities": {
                        "type": "string",
                        "description": "Comma-separated CVE identifiers",
                    },
                    "companies": {
                        "type": "string",
                        "description": "Comma-separated company domains",
                    },
                },
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="triage_rf_indicators",
            description=(
                "Return a boolean malicious/clean verdict for indicators against "
                "a Recorded Future risk context (malware, phishing, or c2). Use "
                "for automated allow/block decisions where a yes/no is wanted "
                "instead of a raw score. Arguments are comma-separated lists "
                "(up to 1,000 indicators total)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "context": {
                        "type": "string",
                        "enum": ["malware", "phishing", "c2"],
                    },
                    "ips": {"type": "string", "description": "Comma-separated IPs"},
                    "domains": {
                        "type": "string",
                        "description": "Comma-separated domains",
                    },
                    "urls": {"type": "string", "description": "Comma-separated URLs"},
                    "hashes": {
                        "type": "string",
                        "description": "Comma-separated file hashes",
                    },
                    "vulnerabilities": {
                        "type": "string",
                        "description": "Comma-separated CVE identifiers",
                    },
                },
                "required": ["context"],
            },
            source=_SOURCE,
            priority=0,
        ),
    ]
