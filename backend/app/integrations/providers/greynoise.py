# SPDX-License-Identifier: Apache-2.0

"""GreyNoise integration - internet scanner + business service intelligence.

Full-fledged GreyNoise (https://www.greynoise.io) v3 API integration for the
agentic SOC/CTI loop: "is this IP background internet noise or a targeted
threat?".

Tool categories:
  - IP intelligence: single full/quick lookup (``/v3/ip/{ip}`` - combined
    Internet Scanner Intelligence + Business Service Intelligence, formerly
    RIOT), batch multi-IP enrichment (``POST /v3/ip``, alert triage / log
    sweeps), and the free-tier Community lookup (``/v3/community/{ip}``).
  - GNQL: query the noise dataset with the GreyNoise Query Language
    (``/v3/gnql``) and aggregate stats for a query (``/v3/gnql/stats``).
  - IP timeline: daily/hourly behavior breakdown for an IP over up to 90 days
    (``/v3/noise/ips/{ip}/timeline``).
  - CVE intelligence: exploitation detail for one CVE (``/v1/cve/{id}``) and
    the tracked-CVE catalog (``/v3/cves/list``).
  - Tags: the GreyNoise tag catalog with intentions/categories/CVEs
    (``/v3/tags``).
  - Blocklists (enterprise): a consolidated manage tool for GNQL-driven,
    auto-refreshing blocklists (list/get/create/update/delete/ips).

Authentication is a single **API key** sent in the ``key`` HTTP header (no
OAuth). All calls target ``https://api.greynoise.io``. The v2 API is
decommissioned and the v3 ``/v3/ip`` endpoint returns the combined
noise + RIOT response; the deprecated IP-similarity endpoints are not
implemented.
"""

from __future__ import annotations

import ipaddress
import logging
import re
from typing import Any

import httpx

from app.core.egress import egress_client
from app.core.utils import is_valid_uuid
from app.integrations.base import BaseIntegration, OAuthDefaults
from app.integrations.http import (
    format_entity_list,
    format_pagination_hint,
    safe_request,
)
from app.tools.base import CallableToolExecutor, ToolDefinition

logger = logging.getLogger(__name__)

_PROVIDER = "GreyNoise"
_SOURCE = "integration:greynoise"

GREYNOISE_API = "https://api.greynoise.io"

_CLASSIFICATION_ICONS: dict[str, str] = {
    "malicious": "🔴",
    "suspicious": "🟠",
    "benign": "🟢",
    "unknown": "⚪",
}

_TIMELINE_FIELDS: frozenset[str] = frozenset(
    {
        "destination_port",
        "http_path",
        "http_user_agent",
        "source_asn",
        "source_org",
        "source_rdns",
        "tag_ids",
        "classification",
    }
)

_BLOCKLIST_ACTIONS: frozenset[str] = frozenset(
    {"list", "get", "create", "update", "delete", "ips"}
)

_MAX_MULTI_IPS = 1000


def _class_icon(classification: Any) -> str:
    return _CLASSIFICATION_ICONS.get(str(classification or "").lower(), "⚪")


def _trunc(text: Any, max_len: int = 300) -> str:
    if text is None:
        return ""
    text = str(text).replace("\r", " ").replace("\n", " ").strip()
    return text[:max_len] + ("…" if len(text) > max_len else "")


_CVE_ID_RE = re.compile(r"CVE-\d{4}-\d{4,}", re.IGNORECASE)


def _is_valid_ip(value: Any) -> bool:
    """True if *value* parses as an IP address (v4 or v6).

    IPs are dropped into request paths, so strict parsing doubles as the
    path-injection guard: a parseable address can never contain ``/``,
    ``?``, ``#``, whitespace, or traversal tokens.
    """
    try:
        ipaddress.ip_address(str(value or "").strip())
        return True
    except ValueError:
        return False


def _is_valid_cve_id(value: str) -> bool:
    """True if *value* is a well-formed CVE id (``CVE-YYYY-NNNN...``).

    Strict matching doubles as the path-injection guard for the
    ``/v1/cve/{id}`` endpoint.
    """
    return bool(_CVE_ID_RE.fullmatch(value))


# ---------------------------------------------------------------------------
# Auth + URL helpers (shared with integration_testing.py)
# ---------------------------------------------------------------------------


def build_greynoise_headers(api_key: str) -> dict[str, str]:
    """Build GreyNoise request headers (the API key goes in the ``key`` header).

    The key is stripped so trailing/leading whitespace (common when pasting
    credentials) can never produce a malformed header. This is the single
    chokepoint shared by the tool builder and the connectivity test.
    """
    return {
        "key": api_key.strip(),
        "Accept": "application/json",
    }


# ---------------------------------------------------------------------------
# Response formatting helpers
# ---------------------------------------------------------------------------


def _format_ip_summary(entry: dict) -> str:
    """One compact block for a combined ``/v3/ip`` response entry."""
    ip = entry.get("ip", "?")
    isi = entry.get("internet_scanner_intelligence") or {}
    bsi = entry.get("business_service_intelligence") or {}
    lines: list[str] = [f"IP {ip}"]

    if isi.get("found"):
        meta = isi.get("metadata") or {}
        tags = isi.get("tags") or []
        cls = isi.get("classification", "unknown")
        lines.append(
            f"  Internet scanner: {_class_icon(cls)} {cls} | "
            f"actor: {isi.get('actor') or 'unknown'} | "
            f"first seen: {isi.get('first_seen') or '?'} | "
            f"last seen: {isi.get('last_seen') or '?'}"
        )
        flags = [
            name
            for name, active in (
                ("spoofable", isi.get("spoofable")),
                ("bot", isi.get("bot")),
                ("vpn", isi.get("vpn")),
                ("tor", isi.get("tor")),
            )
            if active
        ]
        details = (
            f"  Org: {meta.get('organization') or '?'} ({meta.get('asn') or '?'}) | "
            f"Country: {meta.get('source_country') or '?'} | "
            f"Category: {meta.get('category') or '?'} | rDNS: {meta.get('rdns') or '-'}"
        )
        if flags:
            details += f" | Flags: {', '.join(flags)}"
        lines.append(details)
        if tags:
            shown = ", ".join(
                f"{t.get('name', '?')} ({t.get('intention', '?')})" for t in tags[:8]
            )
            extra = f" (+{len(tags) - 8} more)" if len(tags) > 8 else ""
            lines.append(f"  Tags: {shown}{extra}")
        if isi.get("cves"):
            cves = isi["cves"]
            lines.append(
                f"  CVEs: {', '.join(cves[:10])}"
                + (f" (+{len(cves) - 10} more)" if len(cves) > 10 else "")
            )
    elif isi.get("classification") or isi.get("found") is not None:
        lines.append("  Internet scanner: not observed scanning the internet")

    if bsi.get("found"):
        lines.append(
            f"  Business service (RIOT): {bsi.get('name', '?')} "
            f"[{bsi.get('category', '?')}] trust level {bsi.get('trust_level', '?')}"
            + (f" - {_trunc(bsi.get('explanation'), 150)}" if bsi.get("explanation") else "")
        )
    else:
        lines.append("  Business service (RIOT): no match")

    return "\n".join(lines)


def _format_quick_summary(entry: dict) -> str:
    """One line for a ``quick=true`` combined lookup entry."""
    isi = entry.get("internet_scanner_intelligence") or {}
    bsi = entry.get("business_service_intelligence") or {}
    cls = isi.get("classification", "unknown") if isi.get("found") else "not seen"
    riot = (
        f"business service (trust {bsi.get('trust_level', '?')})"
        if bsi.get("found")
        else "no business-service match"
    )
    return f"  {_class_icon(cls)} {entry.get('ip', '?')}: {cls} | {riot}"


# ---------------------------------------------------------------------------
# Provider class
# ---------------------------------------------------------------------------


class GreyNoiseIntegration(BaseIntegration):
    """GreyNoise internet scanner / business service intelligence integration."""

    provider_name = "greynoise"
    display_name = "GreyNoise"
    icon = "Radar"
    description = (
        "Connect GreyNoise to separate internet background noise from targeted "
        "threats: IP context and batch enrichment, GNQL search, IP timelines, "
        "CVE exploitation intelligence, tags, and blocklists."
    )
    auth_types = ["api_key"]

    def get_oauth_defaults(self) -> OAuthDefaults | None:
        return None

    async def validate_connection(self, token: str) -> bool:
        headers = build_greynoise_headers(token)
        try:
            async with egress_client(timeout=15) as client:
                resp = await client.get(f"{GREYNOISE_API}/ping", headers=headers)
                return resp.status_code == 200
        except httpx.HTTPError:
            return False

    async def get_user_info(self, token: str) -> dict:
        headers = build_greynoise_headers(token)
        async with egress_client(timeout=15) as client:
            resp = await client.get(f"{GREYNOISE_API}/ping", headers=headers)
            resp.raise_for_status()
            return resp.json()


# ---------------------------------------------------------------------------
# Tool builder
# ---------------------------------------------------------------------------


def build_tools(
    token: str,
    executor: CallableToolExecutor,
    **_kwargs: Any,
) -> list[ToolDefinition]:
    """Build GreyNoise tools. ``token`` is the GreyNoise API key."""
    api_key = (token or "").strip()
    if not api_key:
        return []

    headers = build_greynoise_headers(api_key)

    def _client(timeout: int = 30) -> httpx.AsyncClient:
        return egress_client(timeout=timeout)

    # ══════════════════════════════════════════════════════════════════════
    # 0. CONNECTION
    # ══════════════════════════════════════════════════════════════════════

    async def get_greynoise_status() -> str:
        """Validate the GreyNoise connection and report the plan + modules."""
        async with _client() as client:
            resp, err = await safe_request(
                client, "get", f"{GREYNOISE_API}/ping", _PROVIDER, "ping", headers=headers
            )
            if err:
                return err
            data = resp.json() or {}
        return (
            f"Connected to GreyNoise ({data.get('message', 'pong')}).\n"
            f"  Plan: {data.get('plan') or 'unknown'} | "
            f"Offering: {data.get('offering') or '?'} | "
            f"Key expiration: {data.get('expiration') or '?'}\n"
            f"  Licensed modules: {data.get('modules') or 'none reported'}"
        )

    # ══════════════════════════════════════════════════════════════════════
    # 1. IP INTELLIGENCE
    # ══════════════════════════════════════════════════════════════════════

    async def lookup_greynoise_ip(ip: str, quick: bool = False) -> str:
        """Full combined lookup for one IP (internet scanner + business service intel)."""
        ip = str(ip or "").strip()
        if not _is_valid_ip(ip):
            return f"Error: '{ip}' is not a valid IP address (e.g. 8.8.8.8)."
        params = {"quick": "true"} if quick else {}
        async with _client() as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{GREYNOISE_API}/v3/ip/{ip}",
                _PROVIDER,
                f"lookup IP {ip}",
                headers=headers,
                params=params,
            )
            if err:
                return err
            data = resp.json() or {}
        if quick:
            return _format_quick_summary(data)
        out = _format_ip_summary(data)
        restricted = (data.get("request_metadata") or {}).get("restricted_fields") or []
        if restricted:
            out += (
                f"\n[Note: {len(restricted)} field(s) restricted by the current "
                "GreyNoise plan]"
            )
        return out

    async def lookup_greynoise_ips(ips: list[str], quick: bool = True) -> str:
        """Batch-enrich up to 1000 IPs in one call (returns one line per IP)."""
        candidates = [str(i).strip() for i in (ips or []) if str(i or "").strip()]
        cleaned = [i for i in candidates if _is_valid_ip(i)]
        invalid = [i for i in candidates if not _is_valid_ip(i)]
        if not cleaned:
            return "Error: provide at least one valid IP address in 'ips'."
        if len(cleaned) > _MAX_MULTI_IPS:
            return (
                f"Error: too many IPs ({len(cleaned)}). Submit at most "
                f"{_MAX_MULTI_IPS} per call."
            )
        params = {"quick": "true"} if quick else {}
        async with _client(timeout=60) as client:
            resp, err = await safe_request(
                client,
                "post",
                f"{GREYNOISE_API}/v3/ip",
                _PROVIDER,
                f"lookup {len(cleaned)} IPs",
                headers=headers,
                params=params,
                json={"ips": cleaned},
            )
            if err:
                return err
            data = resp.json() or {}
        entries = data.get("data") or []
        formatter = _format_quick_summary if quick else _format_ip_summary
        out = format_entity_list(
            entries,
            formatter,
            noun="IP record",
            total=len(entries),
            empty_msg="None of the submitted IPs were found in GreyNoise.",
        )
        not_found = (data.get("request_metadata") or {}).get("ips_not_found") or []
        if not_found:
            shown = ", ".join(not_found[:20])
            extra = f" (+{len(not_found) - 20} more)" if len(not_found) > 20 else ""
            out += f"\nNot found in GreyNoise ({len(not_found)}): {shown}{extra}"
        if invalid:
            shown = ", ".join(invalid[:20])
            extra = f" (+{len(invalid) - 20} more)" if len(invalid) > 20 else ""
            out += f"\nSkipped {len(invalid)} invalid IP(s): {shown}{extra}"
        return out

    async def lookup_greynoise_community_ip(ip: str) -> str:
        """Free Community API lookup: noise/business-service flags + classification."""
        ip = str(ip or "").strip()
        if not _is_valid_ip(ip):
            return f"Error: '{ip}' is not a valid IP address (e.g. 8.8.8.8)."
        async with _client() as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{GREYNOISE_API}/v3/community/{ip}",
                _PROVIDER,
                f"community lookup {ip}",
                headers=headers,
                expected_statuses={404},
            )
            if resp is not None and resp.status_code == 404:
                return (
                    f"{ip} has not been observed scanning the internet and is not "
                    "a known business service."
                )
            if err:
                return err
            data = resp.json() or {}
        cls = data.get("classification", "unknown")
        parts = [
            f"{_class_icon(cls)} {data.get('ip', ip)}: {cls}",
            f"noise: {data.get('noise')}",
            f"business service (RIOT): {data.get('riot')}",
        ]
        if data.get("name"):
            parts.append(f"actor/provider: {data['name']}")
        if data.get("last_seen"):
            parts.append(f"last seen: {data['last_seen']}")
        out = " | ".join(parts)
        if data.get("link"):
            out += f"\nVisualizer: {data['link']}"
        return out

    # ══════════════════════════════════════════════════════════════════════
    # 2. GNQL SEARCH
    # ══════════════════════════════════════════════════════════════════════

    async def query_greynoise_gnql(
        query: str, size: int = 25, scroll: str = ""
    ) -> str:
        """Run a GNQL query against the GreyNoise noise dataset."""
        query = str(query or "").strip()
        if not query:
            return "Error: a GNQL query is required (e.g. 'classification:malicious tags:\"RDP Scanner\"')."
        params: dict[str, Any] = {
            "query": query,
            "size": max(1, min(int(size or 25), 10_000)),
            # Raw scan payloads (ja3/hassh/http bodies) balloon the response;
            # the summary formatter never uses them.
            "exclude": "raw_data",
        }
        if scroll:
            params["scroll"] = scroll
        async with _client(timeout=60) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{GREYNOISE_API}/v3/gnql",
                _PROVIDER,
                "GNQL query",
                headers=headers,
                params=params,
            )
            if err:
                return err
            data = resp.json() or {}
        meta = data.get("request_metadata") or {}
        entries = data.get("data") or []

        def _fmt(entry: dict) -> str:
            isi = entry.get("internet_scanner_intelligence") or {}
            m = isi.get("metadata") or {}
            tags = isi.get("tags") or []
            tag_str = ", ".join(t.get("name", "?") for t in tags[:4])
            if len(tags) > 4:
                tag_str += f" (+{len(tags) - 4})"
            cls = isi.get("classification", "unknown")
            return (
                f"  {_class_icon(cls)} {entry.get('ip', '?')} [{cls}] "
                f"actor: {isi.get('actor') or 'unknown'} | "
                f"{m.get('organization') or '?'} ({m.get('source_country_code') or '?'}) | "
                f"last seen: {isi.get('last_seen') or '?'}"
                + (f" | tags: {tag_str}" if tag_str else "")
            )

        total = meta.get("count", len(entries))
        out = format_entity_list(
            entries,
            _fmt,
            noun="matching IP",
            total=total,
            empty_msg=f"No results for GNQL query: {query}",
        )
        if meta.get("adjusted_query"):
            out += f"\n[Query adjusted by plan limits to: {meta['adjusted_query']}]"
        if not meta.get("complete", True):
            out += format_pagination_hint(meta.get("scroll"), param_name="scroll")
        return out

    async def get_greynoise_gnql_stats(query: str, count: int = 10) -> str:
        """Aggregate stats (top classifications/tags/actors/ASNs/countries/orgs/OS) for a GNQL query."""
        query = str(query or "").strip()
        if not query:
            return "Error: a GNQL query is required (e.g. 'cve:CVE-2024-3400 last_seen:7d')."
        params = {"query": query, "count": max(1, min(int(count or 10), 10_000))}
        async with _client(timeout=60) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{GREYNOISE_API}/v3/gnql/stats",
                _PROVIDER,
                "GNQL stats",
                headers=headers,
                params=params,
            )
            if err:
                return err
            data = resp.json() or {}
        stats = data.get("stats") or {}
        lines = [f"GNQL stats for: {data.get('query', query)}"]
        lines.append(f"Total matching IPs: {data.get('count', 0)}")
        if data.get("adjusted_query"):
            lines.append(f"[Query adjusted by plan limits to: {data['adjusted_query']}]")

        sections: list[tuple[str, str, str]] = [
            ("classifications", "classification", "Classifications"),
            ("tags", "tag", "Top tags"),
            ("actors", "actor", "Top actors"),
            ("source_countries", "country", "Top source countries"),
            ("organizations", "organization", "Top organizations"),
            ("asns", "asn", "Top ASNs"),
            ("operating_systems", "operating_system", "Top operating systems"),
            ("categories", "category", "Top categories"),
        ]
        for stat_key, value_key, label in sections:
            rows = stats.get(stat_key) or []
            if not rows:
                continue
            top = ", ".join(
                f"{r.get(value_key, '?')} ({r.get('count', 0)})" for r in rows[:8]
            )
            lines.append(f"{label}: {top}")
        return "\n".join(lines)

    # ══════════════════════════════════════════════════════════════════════
    # 3. IP TIMELINE
    # ══════════════════════════════════════════════════════════════════════

    async def get_greynoise_ip_timeline(
        ip: str,
        field: str = "classification",
        days: int = 7,
        granularity: str = "1d",
    ) -> str:
        """Behavior timeline for an IP: activity breakdown over a field per time bucket."""
        ip = str(ip or "").strip()
        if not _is_valid_ip(ip):
            return f"Error: '{ip}' is not a valid IP address (e.g. 8.8.8.8)."
        field = str(field or "classification").strip().lower()
        if field not in _TIMELINE_FIELDS:
            return f"Error: field must be one of {', '.join(sorted(_TIMELINE_FIELDS))}."
        params = {
            "field": field,
            "days": str(max(1, min(int(days or 7), 90))),
            "granularity": (granularity or "1d").strip(),
        }
        async with _client(timeout=60) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{GREYNOISE_API}/v3/noise/ips/{ip}/timeline",
                _PROVIDER,
                f"IP timeline {ip}",
                headers=headers,
                params=params,
                expected_statuses={404},
            )
            if resp is not None and resp.status_code == 404:
                return f"No timeline data: {ip} has no recorded noise activity."
            if err:
                return err
            data = resp.json() or {}
        meta = data.get("metadata") or {}
        results = data.get("results") or []
        lines = [
            f"Timeline for {meta.get('ip', ip)} - field: {meta.get('field', field)} | "
            f"granularity: {meta.get('granularity', params['granularity'])} | "
            f"first seen: {meta.get('first_seen') or '?'}"
        ]
        if not results:
            lines.append("No activity in the requested window.")
            return "\n".join(lines)
        # Group by time bucket so each line reads "<bucket>: label (count), …".
        buckets: dict[str, list[str]] = {}
        for row in results:
            ts = str(row.get("timestamp", ""))[:16]
            buckets.setdefault(ts, []).append(
                f"{row.get('label', '?')} ({row.get('data', 0)})"
            )
        for ts in sorted(buckets):
            lines.append(f"  {ts}: {', '.join(buckets[ts][:10])}")
        return "\n".join(lines)

    # ══════════════════════════════════════════════════════════════════════
    # 4. CVE INTELLIGENCE
    # ══════════════════════════════════════════════════════════════════════

    async def lookup_greynoise_cve(cve_id: str) -> str:
        """CVE exploitation intelligence: details, KEV/timeline, exploit stats, observed activity."""
        cve_id = str(cve_id or "").strip().upper()
        if not _is_valid_cve_id(cve_id):
            return "Error: a valid CVE id is required (e.g. CVE-2024-3400)."
        async with _client() as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{GREYNOISE_API}/v1/cve/{cve_id}",
                _PROVIDER,
                f"lookup {cve_id}",
                headers=headers,
                expected_statuses={404},
            )
            if resp is not None and resp.status_code == 404:
                return f"{cve_id} is not tracked by GreyNoise."
            if err:
                return err
            data = resp.json() or {}
        details = data.get("details") or {}
        timeline = data.get("timeline") or {}
        exploit = data.get("exploitation_details") or {}
        stats = data.get("exploitation_stats") or {}
        activity = data.get("exploitation_activity") or {}
        lines = [
            f"{data.get('id', cve_id)}: {details.get('vulnerability_name') or '?'}",
            f"Vendor/product: {details.get('vendor') or '?'} / {details.get('product') or '?'} | "
            f"CVSS: {details.get('cve_cvss_score', 'N/A')}",
        ]
        if details.get("vulnerability_description"):
            lines.append(_trunc(details["vulnerability_description"], 400))
        if timeline:
            lines.append(
                f"Published: {timeline.get('cve_published_date') or '?'} | "
                f"Updated: {timeline.get('cve_last_updated_date') or '?'} | "
                f"CISA KEV added: {timeline.get('cisa_kev_date_added') or 'not in KEV'}"
            )
        if exploit:
            lines.append(
                f"Attack vector: {exploit.get('attack_vector') or '?'} | "
                f"Exploit found: {exploit.get('exploit_found')} | "
                f"In KEV: {exploit.get('exploitation_registered_in_kev')} | "
                f"EPSS: {exploit.get('epss_score', 'N/A')}"
            )
        if stats:
            lines.append(
                f"Available exploits: {stats.get('number_of_available_exploits', 0)} | "
                f"Threat actors exploiting: "
                f"{stats.get('number_of_threat_actors_exploiting_vulnerability', 0)} | "
                f"Botnets: {stats.get('number_of_botnets_exploiting_vulnerability', 0)}"
            )
        if activity:
            lines.append(
                f"Activity seen: {activity.get('activity_seen')} | "
                f"Threat IPs (1d/10d/30d): {activity.get('threat_ip_count_1d', 0)}/"
                f"{activity.get('threat_ip_count_10d', 0)}/"
                f"{activity.get('threat_ip_count_30d', 0)} | "
                f"Benign IPs (30d): {activity.get('benign_ip_count_30d', 0)}"
            )
        lines.append(
            f"Tip: run query_greynoise_gnql with 'cve:{cve_id}' to list the IPs "
            "actively exploiting it."
        )
        return "\n".join(lines)

    async def list_greynoise_cves(
        sort: str = "published",
        order: str = "desc",
        exploitable_only: bool = False,
        limit: int = 25,
    ) -> str:
        """Browse CVEs tracked by GreyNoise ordered by published/updated/CVSS."""
        sort = (sort or "published").strip().lower()
        if sort not in ("published", "updated", "cvss"):
            return "Error: sort must be one of published, updated, cvss."
        order = (order or "desc").strip().lower()
        if order not in ("desc", "asc"):
            return "Error: order must be desc or asc."
        params: dict[str, Any] = {
            "sort": sort,
            "order": order,
            "limit": max(1, min(int(limit or 25), 1000)),
        }
        if exploitable_only:
            params["exploitable_only"] = "true"
        async with _client(timeout=60) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{GREYNOISE_API}/v3/cves/list",
                _PROVIDER,
                "list CVEs",
                headers=headers,
                params=params,
            )
            if err:
                return err
            cves = (resp.json() or {}).get("cves") or []

        def _fmt(c: dict) -> str:
            d = c.get("details") or {}
            e = c.get("exploitation_details") or {}
            return (
                f"  {c.get('id', '?')}: {_trunc(d.get('vulnerability_name'), 70)} | "
                f"CVSS: {d.get('cve_cvss_score', 'N/A')} | "
                f"{d.get('vendor') or '?'}/{d.get('product') or '?'}"
                + (f" | EPSS: {e.get('epss_score')}" if e.get("epss_score") is not None else "")
                + (" | exploit found" if e.get("exploit_found") else "")
            )

        return format_entity_list(cves, _fmt, noun="CVE")

    # ══════════════════════════════════════════════════════════════════════
    # 5. TAGS
    # ══════════════════════════════════════════════════════════════════════

    async def list_greynoise_tags(
        name: str = "", slug: str = "", cve: str = "", limit: int = 25
    ) -> str:
        """List GreyNoise tags (intention, category, CVEs); filter by name/slug/CVE."""
        params: dict[str, Any] = {}
        if name:
            params["name"] = str(name).strip()
        if slug:
            params["slug"] = str(slug).strip()
        if cve:
            params["cve"] = str(cve).strip().upper()
        async with _client(timeout=60) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{GREYNOISE_API}/v3/tags",
                _PROVIDER,
                "list tags",
                headers=headers,
                params=params,
            )
            if err:
                return err
            tags = (resp.json() or {}).get("tags") or []
        total = len(tags)
        shown = tags[: max(1, int(limit or 25))]

        def _fmt(t: dict) -> str:
            cves = t.get("cves") or []
            return (
                f"  {t.get('name', '?')} (slug: {t.get('slug', '?')}) | "
                f"intention: {t.get('intention', '?')} | category: {t.get('category', '?')}"
                + (" | recommend block" if t.get("recommend_block") else "")
                + (
                    f" | CVEs: {', '.join(cves[:5])}"
                    + (f" (+{len(cves) - 5})" if len(cves) > 5 else "")
                    if cves
                    else ""
                )
                + f"\n    {_trunc(t.get('description'), 160)}"
            )

        out = format_entity_list(shown, _fmt, noun="tag", total=total)
        if total > len(shown):
            out += f"\n[Showing {len(shown)} of {total} - filter by name/slug/cve or raise limit]"
        return out

    # ══════════════════════════════════════════════════════════════════════
    # 6. BLOCKLISTS (enterprise)
    # ══════════════════════════════════════════════════════════════════════

    async def manage_greynoise_blocklist(
        action: str,
        workspace_id: str,
        blocklist_id: str = "",
        query: str = "",
        name: str = "",
        ip_limit: int = 0,
        enabled: bool = True,
        size: int = 50,
    ) -> str:
        """Manage GNQL-driven enterprise blocklists: list/get/create/update/delete/ips."""
        act = (action or "").strip().lower()
        if act not in _BLOCKLIST_ACTIONS:
            return f"Error: action must be one of {', '.join(sorted(_BLOCKLIST_ACTIONS))}."
        workspace_id = str(workspace_id or "").strip()
        if not is_valid_uuid(workspace_id):
            return "Error: workspace_id must be the GreyNoise workspace UUID."
        blocklist_id = str(blocklist_id or "").strip()
        if act in ("get", "update", "delete", "ips") and not is_valid_uuid(blocklist_id):
            return f"Error: blocklist_id (a UUID) is required for action '{act}'."

        base = f"{GREYNOISE_API}/v3/workspaces/{workspace_id}/blocklists"

        def _fmt_blocklist(b: dict) -> str:
            return (
                f"  {b.get('name') or '(unnamed)'} [{b.get('id', '?')}] - "
                f"{'enabled' if b.get('enabled') else 'disabled'} | "
                f"query: {_trunc(b.get('query'), 90)} | "
                f"IPs on last refresh: {b.get('last_ip_count', 0)}"
                + (f" | limit: {b.get('ip_limit')}" if b.get("ip_limit") else "")
            )

        async with _client(timeout=60) as client:
            if act == "list":
                resp, err = await safe_request(
                    client,
                    "get",
                    base,
                    _PROVIDER,
                    "list blocklists",
                    headers=headers,
                    params={"limit": 100},
                )
                if err:
                    return err
                data = resp.json() or {}
                return format_entity_list(
                    data.get("blocklists") or [],
                    _fmt_blocklist,
                    noun="blocklist",
                    total=data.get("total"),
                )

            if act == "get":
                resp, err = await safe_request(
                    client,
                    "get",
                    f"{base}/{blocklist_id}",
                    _PROVIDER,
                    f"get blocklist {blocklist_id}",
                    headers=headers,
                )
                if err:
                    return err
                return _fmt_blocklist(resp.json() or {})

            if act == "ips":
                resp, err = await safe_request(
                    client,
                    "get",
                    f"{base}/{blocklist_id}/ips",
                    _PROVIDER,
                    f"get blocklist IPs {blocklist_id}",
                    headers=headers,
                    params={"size": max(1, int(size or 50))},
                )
                if err:
                    return err
                ips = (resp.json() or {}).get("ips") or []
                if not ips:
                    return "The blocklist currently matches no IPs."
                shown = ips[: max(1, int(size or 50))]
                out = f"Blocklist {blocklist_id} - {len(ips)} IP(s):\n" + "\n".join(
                    f"  {i}" for i in shown
                )
                if len(ips) > len(shown):
                    out += f"\n[Showing {len(shown)} of {len(ips)} - raise size]"
                return out

            if act == "create":
                if not query.strip():
                    return "Error: a GNQL query is required to create a blocklist."
                body: dict[str, Any] = {"query": query.strip()}
                if name:
                    body["name"] = name.strip()
                if ip_limit:
                    body["ip_limit"] = max(1, int(ip_limit))
                resp, err = await safe_request(
                    client,
                    "post",
                    base,
                    _PROVIDER,
                    "create blocklist",
                    headers=headers,
                    json=body,
                )
                if err:
                    return err
                created = resp.json() or {}
                return (
                    f"Blocklist created: {created.get('name') or '(unnamed)'} "
                    f"[{created.get('id', '?')}]."
                )

            if act == "update":
                if not query.strip():
                    return "Error: the GNQL query is required when updating a blocklist."
                body = {"query": query.strip(), "enabled": bool(enabled)}
                if name:
                    body["name"] = name.strip()
                if ip_limit:
                    body["ip_limit"] = max(1, int(ip_limit))
                resp, err = await safe_request(
                    client,
                    "put",
                    f"{base}/{blocklist_id}",
                    _PROVIDER,
                    f"update blocklist {blocklist_id}",
                    headers=headers,
                    json=body,
                )
                if err:
                    return err
                return f"Blocklist {blocklist_id} updated."

            # delete
            _, err = await safe_request(
                client,
                "delete",
                f"{base}/{blocklist_id}",
                _PROVIDER,
                f"delete blocklist {blocklist_id}",
                headers=headers,
            )
            if err:
                return err
            return f"Blocklist {blocklist_id} permanently deleted."

    # ══════════════════════════════════════════════════════════════════════
    # REGISTER ALL TOOLS
    # ══════════════════════════════════════════════════════════════════════

    executor.register("get_greynoise_status", get_greynoise_status)
    executor.register("lookup_greynoise_ip", lookup_greynoise_ip)
    executor.register("lookup_greynoise_ips", lookup_greynoise_ips)
    executor.register("lookup_greynoise_community_ip", lookup_greynoise_community_ip)
    executor.register("query_greynoise_gnql", query_greynoise_gnql)
    executor.register("get_greynoise_gnql_stats", get_greynoise_gnql_stats)
    executor.register("get_greynoise_ip_timeline", get_greynoise_ip_timeline)
    executor.register("lookup_greynoise_cve", lookup_greynoise_cve)
    executor.register("list_greynoise_cves", list_greynoise_cves)
    executor.register("list_greynoise_tags", list_greynoise_tags)
    executor.register("manage_greynoise_blocklist", manage_greynoise_blocklist)

    return [
        ToolDefinition(
            name="lookup_greynoise_ip",
            description=(
                "Look up one IP in GreyNoise and get the full combined context: "
                "internet scanner intelligence (classification "
                "malicious/suspicious/benign/unknown, actor, tags, CVEs, ASN/org/"
                "geo, spoofable/bot/vpn/tor flags, first/last seen) plus business "
                "service intelligence (formerly RIOT: known-good providers with a "
                "trust level). Use this first when triaging an alert about an "
                "external IP. Set quick=true for a fast one-line verdict."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "ip": {"type": "string", "description": "IP address (IPv4 or IPv6) to look up"},
                    "quick": {
                        "type": "boolean",
                        "default": False,
                        "description": "Return only the classification/trust-level verdict (faster)",
                    },
                },
                "required": ["ip"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="lookup_greynoise_ips",
            description=(
                "Batch-enrich a list of IPs (up to 1000) in a single GreyNoise "
                "call. Returns one line per IP (classification + business-service "
                "trust) plus the list of IPs GreyNoise has never observed. Use for "
                "alert triage sweeps and log analysis instead of calling "
                "lookup_greynoise_ip repeatedly. Set quick=false for full detail "
                "per IP (slower, verbose - prefer only for small batches)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "ips": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "IP addresses (IPv4 or IPv6) to enrich (max 1000)",
                    },
                    "quick": {
                        "type": "boolean",
                        "default": True,
                        "description": "One-line verdict per IP (recommended for batches)",
                    },
                },
                "required": ["ips"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="query_greynoise_gnql",
            description=(
                "Search the GreyNoise noise dataset with GNQL (GreyNoise Query "
                "Language, Lucene-style). Facets include classification, actor, "
                "tags, cve, first_seen/last_seen (supports 'today', '1d', '7d'), "
                "spoofable, vpn, tor, metadata.country / organization / asn / rdns "
                "/ category, raw_data.scan.port, raw_data.web.useragents, JA3/JA4 "
                "fingerprints, and CIDR blocks. Examples: 'classification:malicious "
                "metadata.country:Belgium', 'tags:\"RDP Scanner\" last_seen:1d', "
                "'cve:CVE-2024-3400', '8.0.0.0/8'. Returns matching IPs with actor/"
                "org/tags; pass the scroll token to page through large result sets."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "GNQL query string"},
                    "size": {
                        "type": "integer",
                        "default": 25,
                        "description": "Results per page (1-10000)",
                    },
                    "scroll": {
                        "type": "string",
                        "description": "Scroll token from a previous page to continue pagination",
                    },
                },
                "required": ["query"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="get_greynoise_gnql_stats",
            description=(
                "Get aggregate statistics for a GNQL query: total matching IPs and "
                "the top classifications, tags, actors, source countries, "
                "organizations, ASNs, operating systems, and categories. Use to "
                "understand the shape of a campaign (e.g. 'who is scanning for "
                "CVE-2024-3400?') before pulling individual IPs with "
                "query_greynoise_gnql."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "GNQL query string"},
                    "count": {
                        "type": "integer",
                        "default": 10,
                        "description": "Number of top aggregates per facet",
                    },
                },
                "required": ["query"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="get_greynoise_ip_timeline",
            description=(
                "Get the behavior timeline of an IP over up to 90 days: activity "
                "counts per time bucket broken down by one field (classification, "
                "destination_port, http_path, http_user_agent, source_asn, "
                "source_org, source_rdns, or tag_ids). Use to see when an IP "
                "turned malicious, what ports it targets, or what schedule it "
                "operates on."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "ip": {"type": "string", "description": "IP address (IPv4 or IPv6)"},
                    "field": {
                        "type": "string",
                        "enum": sorted(_TIMELINE_FIELDS),
                        "default": "classification",
                        "description": "Field to break activity down by",
                    },
                    "days": {
                        "type": "integer",
                        "default": 7,
                        "description": "Lookback window in days (1-90)",
                    },
                    "granularity": {
                        "type": "string",
                        "default": "1d",
                        "description": "Bucket size: hours (1h-24h) or days (1d-90d)",
                    },
                },
                "required": ["ip"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="lookup_greynoise_cve",
            description=(
                "Get GreyNoise exploitation intelligence for a CVE: vulnerability "
                "details and CVSS, publication/KEV timeline, attack vector, EPSS "
                "score, exploit availability, threat actor/botnet counts, and "
                "observed exploitation activity (threat vs benign IP counts over "
                "1/10/30 days). Use for vulnerability prioritization - it tells "
                "you whether a CVE is actually being exploited in the wild right "
                "now. Detail level depends on the GreyNoise plan."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "cve_id": {
                        "type": "string",
                        "description": "CVE id, e.g. CVE-2024-3400",
                    },
                },
                "required": ["cve_id"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="list_greynoise_cves",
            description=(
                "Browse the CVEs tracked by GreyNoise, ordered by publication "
                "date, last-update date, or CVSS score. Set exploitable_only=true "
                "to restrict to network-exploitable CVEs. Use to discover what is "
                "newly tracked or currently severe; follow up per CVE with "
                "lookup_greynoise_cve. Requires the bulk CVE search entitlement."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "sort": {
                        "type": "string",
                        "enum": ["published", "updated", "cvss"],
                        "default": "published",
                    },
                    "order": {"type": "string", "enum": ["desc", "asc"], "default": "desc"},
                    "exploitable_only": {
                        "type": "boolean",
                        "default": False,
                        "description": "Only CVEs with a Network attack vector",
                    },
                    "limit": {"type": "integer", "default": 25, "description": "Max records (1-1000)"},
                },
                "required": [],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="list_greynoise_tags",
            description=(
                "List GreyNoise tags - the labeled behaviors/actors/tools applied "
                "to scanning IPs (e.g. 'Mirai', 'RDP Scanner'). Each tag includes "
                "its intention (benign/suspicious/malicious), category, associated "
                "CVEs, and a block recommendation. Filter by partial name, exact "
                "slug, or CVE. Use to find the right tag name for a "
                "query_greynoise_gnql 'tags:' filter."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Partial tag name match, e.g. 'Mirai'"},
                    "slug": {"type": "string", "description": "Exact tag slug match, e.g. 'mirai'"},
                    "cve": {"type": "string", "description": "Filter tags associated with a CVE"},
                    "limit": {"type": "integer", "default": 25},
                },
                "required": [],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="lookup_greynoise_community_ip",
            description=(
                "Free-tier GreyNoise Community lookup for one IP: noise / "
                "business-service flags, classification, actor name, last seen, "
                "and a Visualizer link. Use only when the API key is a free "
                "community key or the full lookup_greynoise_ip is not available - "
                "the full lookup returns strictly more detail."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "ip": {"type": "string", "description": "IP address (IPv4 or IPv6) to look up"},
                },
                "required": ["ip"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="get_greynoise_status",
            description=(
                "Validate the GreyNoise connection and report the subscription "
                "plan, API key expiration, and licensed add-on modules. Use first "
                "if other GreyNoise tools fail or return permission errors."
            ),
            input_schema={"type": "object", "properties": {}, "required": []},
            source=_SOURCE,
            priority=2,
        ),
        ToolDefinition(
            name="manage_greynoise_blocklist",
            description=(
                "Manage GreyNoise enterprise blocklists - GNQL-driven, "
                "auto-refreshing IP lists for firewall/EDL blocking. Actions: "
                "'list' (all blocklists in a workspace), 'get' (one blocklist), "
                "'ips' (the IPs currently matching), 'create' (new blocklist from "
                "a GNQL query), 'update' (change query/name/ip_limit/enabled), "
                "'delete' (permanent, cannot be undone). Requires the Blocklists "
                "entitlement and the workspace UUID."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": sorted(_BLOCKLIST_ACTIONS),
                        "description": "Operation to perform",
                    },
                    "workspace_id": {
                        "type": "string",
                        "description": "GreyNoise workspace UUID (required for all actions)",
                    },
                    "blocklist_id": {
                        "type": "string",
                        "description": "Blocklist UUID (required for get/update/delete/ips)",
                    },
                    "query": {
                        "type": "string",
                        "description": "GNQL query defining the blocklist (required for create/update)",
                    },
                    "name": {"type": "string", "description": "Human-readable blocklist name"},
                    "ip_limit": {
                        "type": "integer",
                        "description": "Maximum number of IPs the blocklist returns",
                    },
                    "enabled": {
                        "type": "boolean",
                        "default": True,
                        "description": "Whether the blocklist is active (update only)",
                    },
                    "size": {
                        "type": "integer",
                        "default": 50,
                        "description": "Max IPs to return for the 'ips' action",
                    },
                },
                "required": ["action", "workspace_id"],
            },
            source=_SOURCE,
            priority=1,
        ),
    ]
