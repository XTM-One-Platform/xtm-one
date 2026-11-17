# SPDX-License-Identifier: Apache-2.0

"""VulnCheck integration - vulnerability and exploit intelligence.

Full-fledged VulnCheck (https://vulncheck.com) integration for the agentic
SOC/CTI loop: "is this CVE actually being exploited in the wild, by whom, and
is my product or package affected?".

VulnCheck aggregates vulnerability and exploit intelligence: VulnCheck KEV
(known exploited vulnerabilities), NVD++ / NVD-2 (early + enriched CVE data),
exploit intelligence (PoC and weaponized maturity, threat-actor / ransomware /
botnet exploitation), EPSS, Initial Access Intelligence (RCE detection rules +
IP intel), IP / infrastructure intelligence (C2, honeypot, initial-access IPs),
and package / product-to-CVE mapping (CPE + PURL).

Tool categories (all tools are READ-only - VulnCheck's API is an intelligence
service with no write or destructive operations):
  - Vulnerability + exploit intelligence: enriched CVE lookup, VulnCheck KEV
    membership, exploit intelligence, EPSS, and the v4 advisory query.
  - Threat intelligence: threat actors, ransomware groups, botnets, initial
    access.
  - IP / infrastructure intelligence: IP intel lookups, C2/attacker IP tags,
    and Protective DNS hostname lists.
  - Package / product mapping: CPE-to-CVE, CPE attribute search, PURL batch
    lookup, and (vendor, product, version) -> CPE/PURL identification.
  - Detection content: Initial Access Intelligence Suricata/Snort rules.
  - Discovery / generic: list the licensed indexes and a consolidated escape
    hatch for querying any index with the full filter set.

Authentication is a single **API key** sent as ``Authorization: Bearer
<token>`` (generated in the VulnCheck dashboard). All calls target
``https://api.vulncheck.com`` (v3 index/search + v4 advisory). There is no
OAuth and no compound token. Index/source availability depends on the key's
license tier (community vs commercial); out-of-plan reads return 402/403 with a
clear message that the shared error formatter surfaces.
"""

from __future__ import annotations

import logging
import re
from typing import Any

import httpx

from app.core.egress import egress_client
from app.integrations.base import BaseIntegration, OAuthDefaults
from app.integrations.http import (
    bearer_auth_headers,
    format_entity_list,
    format_pagination_hint,
    safe_request,
)
from app.tools.base import CallableToolExecutor, ToolDefinition

logger = logging.getLogger(__name__)

_PROVIDER = "VulnCheck"
_SOURCE = "integration:vulncheck"

VULNCHECK_API = "https://api.vulncheck.com"

# The enriched (commercial) NVD-2 index and the community NIST NVD-2 index share
# the same CVE record shape; lookup_vulncheck_cve prefers the enriched one and
# falls back to the community index when the key is not licensed for it.
_NVD2_ENRICHED_INDEX = "vulncheck-nvd2"
_NVD2_COMMUNITY_INDEX = "nist-nvd2"

_CVE_ID_RE = re.compile(r"CVE-\d{4}-\d{4,}", re.IGNORECASE)
# Index names are lowercase slugs (e.g. "a10", "vulncheck-kev", "ipintel-3d").
# Strict matching doubles as the path-injection guard for /v3/index/{index}:
# a matching value can never contain "/", "?", "#", whitespace, or traversal.
_INDEX_NAME_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
# IP tag / Protective DNS list names are slugs (e.g. "vulncheck-c2"). Strict
# matching is the path-injection guard for /v3/tags/{filter} and /v3/pdns/{filter}.
_TAG_FILTER_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")

# Valid detection-id filter values for the IP Intelligence indexes.
_IPINTEL_IDS: frozenset[str] = frozenset({"c2", "initial-access", "honeypot"})

# IP Intelligence rolling-window indexes (documented by VulnCheck).
_IPINTEL_WINDOWS: frozenset[str] = frozenset({"3d", "10d", "30d", "90d"})

_MAX_LIMIT = 200
# The tag / PDNS denylist endpoints return flat line-based bodies, so a larger
# display cap than the index page size stays legible.
_TAG_MAX_LIMIT = 1000


def _is_valid_cve_id(value: str) -> bool:
    """True if *value* is a well-formed CVE id (``CVE-YYYY-NNNN...``)."""
    return bool(_CVE_ID_RE.fullmatch(str(value or "").strip()))


def _is_valid_index_name(value: str) -> bool:
    """True if *value* is a safe VulnCheck index name (path-injection guard)."""
    return bool(_INDEX_NAME_RE.fullmatch(str(value or "").strip().lower()))


def _is_valid_tag_filter(value: str) -> bool:
    """True if *value* is a safe IP-tag / PDNS list name (path-injection guard)."""
    return bool(_TAG_FILTER_RE.fullmatch(str(value or "").strip().lower()))


def _trunc(text: Any, max_len: int = 300) -> str:
    if text is None:
        return ""
    text = str(text).replace("\r", " ").replace("\n", " ").strip()
    return text[:max_len] + ("..." if len(text) > max_len else "")


def _clamp_limit(limit: Any, default: int = 25, max_value: int = _MAX_LIMIT) -> int:
    try:
        value = int(limit)
    except (TypeError, ValueError):
        value = default
    return max(1, min(value, max_value))


def _clamp_page(page: Any) -> int:
    try:
        value = int(page)
    except (TypeError, ValueError):
        value = 1
    return max(1, value)


def _next_cursor(payload: dict) -> str | None:
    """Return the ``_meta.next_cursor`` pagination token, if any."""
    meta = payload.get("_meta") or {}
    cursor = meta.get("next_cursor")
    return str(cursor) if cursor else None


def _meta_total(payload: dict) -> int | None:
    meta = payload.get("_meta") or {}
    total = meta.get("total_documents")
    if total is None:
        total = meta.get("total")
    return total if isinstance(total, int) else None


# ---------------------------------------------------------------------------
# Auth helper (shared with integration_testing.py)
# ---------------------------------------------------------------------------


def build_vulncheck_headers(token: str) -> dict[str, str]:
    """Build VulnCheck request headers (``Authorization: Bearer <token>``).

    The token is routed through ``bearer_auth_headers`` which strips it and
    guards against a blank value (never produces an illegal ``"Bearer "``).
    This is the single chokepoint shared by the tool builder and the
    connectivity test.
    """
    return bearer_auth_headers(token, extra={"Accept": "application/json"})


# ---------------------------------------------------------------------------
# CVE / document formatting helpers
# ---------------------------------------------------------------------------


def _nvd2_description(record: dict) -> str:
    descriptions = record.get("descriptions") or []
    for entry in descriptions:
        if (entry or {}).get("lang", "en") == "en":
            return _trunc(entry.get("value"), 400)
    if descriptions:
        return _trunc((descriptions[0] or {}).get("value"), 400)
    return ""


def _nvd2_cvss(record: dict) -> str:
    """Best-effort CVSS score/severity from an NVD 2.0 metrics block."""
    metrics = record.get("metrics") or {}
    for key in ("cvssMetricV31", "cvssMetricV30", "cvssMetricV2"):
        rows = metrics.get(key) or []
        if rows:
            data = (rows[0] or {}).get("cvssData") or {}
            score = data.get("baseScore")
            severity = data.get("baseSeverity") or (rows[0] or {}).get("baseSeverity")
            if score is not None:
                return f"{score}" + (f" ({severity})" if severity else "")
    return "N/A"


def _format_cve_detail(record: dict) -> str:
    cve_id = record.get("id") or record.get("cve") or "?"
    if isinstance(cve_id, list):
        cve_id = cve_id[0] if cve_id else "?"
    lines = [
        f"{cve_id}",
        f"CVSS: {_nvd2_cvss(record)} | "
        f"Published: {record.get('published') or '?'} | "
        f"Last modified: {record.get('lastModified') or '?'}",
    ]
    desc = _nvd2_description(record)
    if desc:
        lines.append(desc)
    vc_cpes = record.get("vcVulnerableCPEs") or []
    if vc_cpes:
        shown = ", ".join(vc_cpes[:5])
        extra = f" (+{len(vc_cpes) - 5} more)" if len(vc_cpes) > 5 else ""
        lines.append(f"Vulnerable CPEs: {shown}{extra}")
    refs = record.get("references") or []
    if refs:
        # References are normally dicts with a "url" key, but normalize
        # defensively: a bare-string ref is used as-is, and the example URL is
        # routed through _trunc so newlines/control chars can't break the
        # single-line output format.
        first = refs[0]
        url = first.get("url") if isinstance(first, dict) else first
        lines.append(f"References: {len(refs)} (e.g. {_trunc(url, 200) or '?'})")
    return "\n".join(lines)


def _summarize_doc(doc: dict) -> str:
    """Generic one-line summary for an arbitrary index document.

    Index documents vary by source, so this picks the most useful common
    identifier/label fields defensively rather than dumping raw JSON.
    """
    if not isinstance(doc, dict):
        return f"  {_trunc(doc, 160)}"
    ident = (
        doc.get("id")
        or doc.get("cve")
        or doc.get("cveId")
        or doc.get("name")
        or doc.get("_id")
        or "?"
    )
    if isinstance(ident, list):
        ident = ", ".join(str(i) for i in ident[:3]) or "?"
    label = (
        doc.get("shortDescription")
        or doc.get("vulnerabilityName")
        or doc.get("description")
        or doc.get("title")
        or doc.get("threat_actor_name")
        or doc.get("ransomware_family")
        or ""
    )
    ts = doc.get("_timestamp") or doc.get("date_added") or doc.get("published") or ""
    parts = [f"  {ident}"]
    if label:
        parts.append(_trunc(label, 120))
    if ts:
        parts.append(f"[{ts}]")
    return " | ".join(parts)


# ---------------------------------------------------------------------------
# Provider class
# ---------------------------------------------------------------------------


class VulnCheckIntegration(BaseIntegration):
    """VulnCheck vulnerability and exploit intelligence integration."""

    provider_name = "vulncheck"
    display_name = "VulnCheck"
    icon = "ShieldAlert"
    description = (
        "Connect VulnCheck for vulnerability and exploit intelligence: VulnCheck "
        "KEV, enriched NVD-2 CVE data, exploit intelligence, EPSS, threat actor / "
        "ransomware / botnet exploitation, IP and infrastructure intelligence, "
        "and CPE/PURL product-to-CVE mapping."
    )
    auth_types = ["api_key"]

    def get_oauth_defaults(self) -> OAuthDefaults | None:
        return None

    async def validate_connection(self, token: str) -> bool:
        api_key = (token or "").strip()
        if not api_key:
            return False
        # ``safe_request`` converts ``httpx.HTTPError`` into a ``(resp, err)``
        # tuple and never raises, so no surrounding try/except is needed - a
        # broad ``except Exception`` here would only mask programming errors
        # and swallow task cancellation.
        async with egress_client(timeout=15) as client:
            _resp, err = await safe_request(
                client,
                "get",
                f"{VULNCHECK_API}/v3/index",
                _PROVIDER,
                "validate connection",
                headers=build_vulncheck_headers(api_key),
            )
        return err is None

    async def get_user_info(self, token: str) -> dict:
        api_key = (token or "").strip()
        if not api_key:
            return {}
        async with egress_client(timeout=15) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{VULNCHECK_API}/v3/index",
                _PROVIDER,
                "get account info",
                headers=build_vulncheck_headers(api_key),
            )
            if err or resp is None:
                return {}
            data = resp.json() or {}
        indexes = data.get("data") or []
        return {"licensed_indexes": len(indexes)}


# ---------------------------------------------------------------------------
# Tool builder
# ---------------------------------------------------------------------------


def build_tools(
    token: str,
    executor: CallableToolExecutor,
    **_kwargs: Any,
) -> list[ToolDefinition]:
    """Build VulnCheck tools. ``token`` is the VulnCheck API key."""
    api_key = (token or "").strip()
    if not api_key:
        return []

    headers = build_vulncheck_headers(api_key)

    def _client(timeout: int = 30) -> httpx.AsyncClient:
        return egress_client(timeout=timeout)

    async def _get_index(
        index: str,
        action: str,
        params: dict[str, Any],
        *,
        timeout: int = 30,
    ) -> tuple[dict | None, str | None]:
        """GET /v3/index/{index} with the given params. Returns (payload, err)."""
        async with _client(timeout) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{VULNCHECK_API}/v3/index/{index}",
                _PROVIDER,
                action,
                headers=headers,
                params=params,
            )
            if err:
                return None, err
            return (resp.json() or {}), None

    # ======================================================================
    # 0. DISCOVERY
    # ======================================================================

    async def list_vulncheck_indexes(filter: str = "") -> str:
        """List the VulnCheck indexes the API key is licensed for."""
        async with _client() as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{VULNCHECK_API}/v3/index",
                _PROVIDER,
                "list indexes",
                headers=headers,
            )
            if err:
                return err
            data = resp.json() or {}
        indexes = data.get("data") or []
        needle = (filter or "").strip().lower()
        if needle:
            indexes = [
                i
                for i in indexes
                if needle in str(i.get("name", "")).lower()
                or needle in str(i.get("description", "")).lower()
            ]
        # The tool contract promises alphabetical order - do not rely on the
        # API's (undocumented) ordering.
        indexes = sorted(indexes, key=lambda i: str(i.get("name", "")).lower())

        def _fmt(i: dict) -> str:
            return f"  {i.get('name', '?')}: {_trunc(i.get('description'), 100)}"

        return format_entity_list(
            indexes,
            _fmt,
            noun="licensed index",
            total=len(indexes),
            empty_msg=(
                "No indexes matched." if needle else "No indexes licensed for this key."
            ),
        )

    async def query_vulncheck_index(
        index: str,
        cve: str = "",
        alias: str = "",
        threat_actor: str = "",
        mitre_id: str = "",
        ransomware: str = "",
        botnet: str = "",
        published: str = "",
        last_mod_start_date: str = "",
        pub_start_date: str = "",
        sort: str = "",
        order: str = "",
        limit: int = 25,
        cursor: str = "",
    ) -> str:
        """Query any licensed VulnCheck index with the full documented filter set."""
        index = (index or "").strip().lower()
        if not _is_valid_index_name(index):
            return (
                "Error: a valid index name is required (see list_vulncheck_indexes), "
                "e.g. 'vulncheck-kev', 'exploits', 'ipintel-3d'."
            )
        cve = str(cve or "").strip().upper()
        if cve and not _is_valid_cve_id(cve):
            return "Error: 'cve' must be a valid CVE id, e.g. CVE-2024-3400."
        params: dict[str, Any] = {"limit": _clamp_limit(limit)}
        # Strip BEFORE the truthiness check so a whitespace-only input is
        # treated as absent instead of being sent as an empty-string filter.
        for key, value in (
            ("cve", cve),
            ("alias", alias),
            ("threat_actor", threat_actor),
            ("mitre_id", mitre_id),
            ("ransomware", ransomware),
            ("botnet", botnet),
            ("published", published),
            ("lastModStartDate", last_mod_start_date),
            ("pubStartDate", pub_start_date),
            ("sort", sort),
            ("order", order),
        ):
            value = str(value or "").strip()
            if value:
                params[key] = value
        cursor = str(cursor or "").strip()
        if cursor:
            params["cursor"] = cursor
        else:
            params["start_cursor"] = "true"

        payload, err = await _get_index(index, f"query index {index}", params, timeout=45)
        if err:
            return err
        docs = payload.get("data") or []
        out = format_entity_list(
            docs,
            _summarize_doc,
            noun="document",
            total=_meta_total(payload),
            empty_msg=f"No documents in index '{index}' for the given filters.",
        )
        nxt = _next_cursor(payload)
        if nxt:
            out += format_pagination_hint(nxt, param_name="cursor")
        return out

    # ======================================================================
    # 1. VULNERABILITY + EXPLOIT INTELLIGENCE
    # ======================================================================

    async def lookup_vulncheck_cve(cve: str, prefer_community: bool = False) -> str:
        """Full enriched CVE record from VulnCheck NVD-2 (falls back to NIST NVD-2)."""
        cve = str(cve or "").strip().upper()
        if not _is_valid_cve_id(cve):
            return "Error: a valid CVE id is required, e.g. CVE-2024-3400."
        order = (
            [_NVD2_COMMUNITY_INDEX, _NVD2_ENRICHED_INDEX]
            if prefer_community
            else [_NVD2_ENRICHED_INDEX, _NVD2_COMMUNITY_INDEX]
        )
        last_err: str | None = None
        any_success = False
        for index in order:
            payload, err = await _get_index(
                index, f"lookup {cve}", {"cve": cve, "limit": 1}
            )
            if err:
                last_err = err
                continue
            any_success = True
            docs = payload.get("data") or []
            if docs:
                detail = _format_cve_detail(docs[0])
                return f"[source: {index}]\n{detail}"
        # Only surface an error when EVERY index failed - if one index
        # answered (even with 0 results), "not found" is the truthful outcome
        # and e.g. a 402 from the unlicensed enriched index must not mask it.
        if last_err and not any_success:
            return last_err
        return f"{cve} was not found in the VulnCheck NVD-2 indexes."

    async def get_vulncheck_kev(cve: str = "", limit: int = 25) -> str:
        """Check the VulnCheck KEV (known exploited vulnerabilities) index."""
        cve = str(cve or "").strip().upper()
        if cve and not _is_valid_cve_id(cve):
            return "Error: 'cve' must be a valid CVE id, e.g. CVE-2024-3400."
        params: dict[str, Any] = {"limit": _clamp_limit(limit)}
        if cve:
            params["cve"] = cve
        payload, err = await _get_index("vulncheck-kev", "get KEV", params)
        if err:
            return err
        docs = payload.get("data") or []
        if cve and not docs:
            return f"{cve} is NOT in the VulnCheck KEV."

        def _fmt(d: dict) -> str:
            cves = d.get("cve") or []
            cve_str = ", ".join(cves) if isinstance(cves, list) else str(cves)
            ransom = d.get("knownRansomwareCampaignUse")
            return (
                f"  {cve_str or '?'}: {_trunc(d.get('vulnerabilityName') or d.get('shortDescription'), 100)}\n"
                f"    Vendor/product: {d.get('vendorProject') or '?'} / {d.get('product') or '?'} | "
                f"Added: {d.get('date_added') or d.get('dateAdded') or '?'}"
                + (f" | Ransomware use: {ransom}" if ransom else "")
            )

        header = f"{cve} IS in the VulnCheck KEV:\n" if cve and docs else ""
        return header + format_entity_list(
            docs, _fmt, noun="KEV entry", total=_meta_total(payload)
        )

    async def get_vulncheck_exploits(cve: str, limit: int = 25) -> str:
        """Exploit intelligence for a CVE (maturity, PoC URLs, TA/ransomware/botnet)."""
        cve = str(cve or "").strip().upper()
        if not _is_valid_cve_id(cve):
            return "Error: a valid CVE id is required, e.g. CVE-2024-3400."
        payload, err = await _get_index(
            "exploits", f"exploits for {cve}", {"cve": cve, "limit": _clamp_limit(limit)}
        )
        if err:
            return err
        docs = payload.get("data") or []
        if not docs:
            return f"VulnCheck has no exploit intelligence for {cve}."
        record = docs[0]
        counts = record.get("counts") or {}
        timeline = record.get("timeline") or {}
        epss = record.get("epss") or {}
        lines = [
            f"{record.get('id', cve)} exploit intelligence:",
            f"  Public exploit: {record.get('public_exploit_found')} | "
            f"Weaponized: {record.get('weaponized_exploit_found')} | "
            f"Max maturity: {record.get('max_exploit_maturity') or '?'}",
            f"  Reported exploited: {record.get('reported_exploited')} | "
            f"by threat actors: {record.get('reported_exploited_by_threat_actors')} | "
            f"by ransomware: {record.get('reported_exploited_by_ransomware')} | "
            f"by botnets: {record.get('reported_exploited_by_botnets')}",
            f"  In CISA KEV: {record.get('inKEV')} | In VulnCheck KEV: {record.get('inVCKEV')}",
        ]
        if epss:
            lines.append(
                f"  EPSS: {epss.get('epss_score', 'N/A')} "
                f"(percentile {epss.get('epss_percentile', 'N/A')})"
            )
        if counts:
            lines.append(
                f"  Counts - exploits: {counts.get('exploits', 0)} | "
                f"threat actors: {counts.get('threat_actors', 0)} | "
                f"botnets: {counts.get('botnets', 0)} | "
                f"ransomware families: {counts.get('ransomware_families', 0)}"
            )
        if timeline:
            lines.append(
                f"  First exploit published: {timeline.get('first_exploit_published') or '?'} | "
                f"Most recent: {timeline.get('most_recent_exploit_published') or '?'}"
            )
        exploits = record.get("exploits") or []
        if exploits:
            lines.append(f"  Exploits ({len(exploits)}):")
            for ex in exploits[:10]:
                lines.append(
                    f"    - {_trunc(ex.get('name') or ex.get('url'), 90)} "
                    f"[{ex.get('exploit_maturity') or '?'}] {ex.get('url') or ''}"
                )
            if len(exploits) > 10:
                lines.append(f"    (+{len(exploits) - 10} more)")
        return "\n".join(lines)

    async def get_vulncheck_epss(cve: str) -> str:
        """EPSS score and percentile for a CVE (exploit-probability prioritization)."""
        cve = str(cve or "").strip().upper()
        if not _is_valid_cve_id(cve):
            return "Error: a valid CVE id is required, e.g. CVE-2024-3400."
        payload, err = await _get_index("epss", f"EPSS for {cve}", {"cve": cve, "limit": 1})
        if err:
            return err
        docs = payload.get("data") or []
        if not docs:
            return f"VulnCheck has no EPSS data for {cve}."
        record = docs[0]
        return (
            f"{record.get('cve', cve)} EPSS:\n"
            f"  Score: {record.get('epss_score', 'N/A')} "
            f"(probability of exploitation in the next 30 days)\n"
            f"  Percentile: {record.get('epss_percentile', 'N/A')} | "
            f"Last modified: {record.get('_timestamp') or record.get('date') or '?'}"
        )

    async def query_vulncheck_advisory(
        cve_id: str = "",
        vendor: str = "",
        product: str = "",
        version: str = "",
        cpe: str = "",
        purl: str = "",
        name: str = "",
        updated_after: str = "",
        limit: int = 10,
        page: int = 1,
    ) -> str:
        """Query the VulnCheck v4 advisory index (CVE 5.0 records) across all feeds."""
        cve_id = str(cve_id or "").strip().upper()
        if cve_id and not _is_valid_cve_id(cve_id):
            return "Error: 'cve_id' must be a valid CVE id, e.g. CVE-2024-1234."
        params: dict[str, Any] = {
            "limit": _clamp_limit(limit, default=10, max_value=100),
            "page": _clamp_page(page),
        }
        # Strip BEFORE the truthiness check so a whitespace-only input can
        # neither be sent as an empty-string filter nor satisfy the
        # at-least-one-filter guard below.
        for key, value in (
            ("cve_id", cve_id),
            ("vendor", vendor),
            ("product", product),
            ("version", version),
            ("cpe", cpe),
            ("purl", purl),
            ("name", name),
            ("updatedAfter", updated_after),
        ):
            value = str(value or "").strip()
            if value:
                params[key] = value
        if len(params) <= 2:
            return (
                "Error: provide at least one filter (cve_id, vendor, product, "
                "version, cpe, purl, name, or updated_after)."
            )
        async with _client(timeout=45) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{VULNCHECK_API}/v4/advisory",
                _PROVIDER,
                "query advisory",
                headers=headers,
                params=params,
            )
            if err:
                return err
            payload = resp.json() or {}
        docs = payload.get("data") or []
        meta = payload.get("_meta") or {}

        def _fmt(d: dict) -> str:
            meta_c = d.get("cveMetadata") or {}
            cna = (d.get("containers") or {}).get("cna") or {}
            descs = cna.get("descriptions") or []
            desc = ""
            for entry in descs:
                if (entry or {}).get("lang", "en").startswith("en"):
                    desc = _trunc(entry.get("value"), 160)
                    break
            title = cna.get("title") or ""
            cve_id_val = meta_c.get("cveId") or "?"
            head = f"  {cve_id_val} [{meta_c.get('state') or '?'}]"
            if title:
                head += f" - {_trunc(title, 90)}"
            return head + (f"\n    {desc}" if desc else "")

        out = format_entity_list(
            docs, _fmt, noun="advisory", total=meta.get("total")
        )
        pages = meta.get("pages")
        if isinstance(pages, int) and params["page"] < pages:
            out += (
                f"\n[Page {params['page']} of {pages} - pass page={params['page'] + 1} "
                "for more.]"
            )
        return out

    # ======================================================================
    # 2. THREAT INTELLIGENCE
    # ======================================================================

    async def list_vulncheck_threat_actors(
        threat_actor: str = "",
        mitre_id: str = "",
        misp_id: str = "",
        cve: str = "",
        limit: int = 25,
        cursor: str = "",
    ) -> str:
        """List VulnCheck threat actors and the CVEs they target."""
        cve = str(cve or "").strip().upper()
        if cve and not _is_valid_cve_id(cve):
            return "Error: 'cve' must be a valid CVE id, e.g. CVE-2024-3400."
        params: dict[str, Any] = {"limit": _clamp_limit(limit)}
        # Strip BEFORE the truthiness check so a whitespace-only input is
        # treated as absent instead of being sent as an empty-string filter.
        for key, value in (
            ("threat_actor", threat_actor),
            ("mitre_id", mitre_id),
            ("misp_id", misp_id),
            ("cve", cve),
        ):
            value = str(value or "").strip()
            if value:
                params[key] = value
        cursor = str(cursor or "").strip()
        if cursor:
            params["cursor"] = cursor
        else:
            params["start_cursor"] = "true"
        payload, err = await _get_index(
            "threat-actors", "list threat actors", params, timeout=45
        )
        if err:
            return err
        docs = payload.get("data") or []

        def _fmt(d: dict) -> str:
            refs = d.get("cve_references") or []
            cves: list[str] = []
            for ref in refs:
                cves.extend((ref or {}).get("cve") or [])
            cve_str = ", ".join(dict.fromkeys(cves))
            misp_meta = (d.get("misp_threat_actor") or {}).get("meta") or {}
            mitre = misp_meta.get("mitre-attack")
            return (
                f"  {d.get('threat_actor_name', '?')} | "
                f"first added: {d.get('date_added') or '?'}"
                + (f" | MITRE: {mitre}" if mitre else "")
                + (f"\n    Targets CVEs: {_trunc(cve_str, 200)}" if cve_str else "")
            )

        out = format_entity_list(
            docs, _fmt, noun="threat actor", total=_meta_total(payload)
        )
        nxt = _next_cursor(payload)
        if nxt:
            out += format_pagination_hint(nxt, param_name="cursor")
        return out

    async def list_vulncheck_ransomware(
        ransomware: str = "",
        cve: str = "",
        limit: int = 25,
        cursor: str = "",
    ) -> str:
        """List VulnCheck ransomware groups and the CVEs they exploit."""
        cve = str(cve or "").strip().upper()
        if cve and not _is_valid_cve_id(cve):
            return "Error: 'cve' must be a valid CVE id, e.g. CVE-2024-3400."
        params: dict[str, Any] = {"limit": _clamp_limit(limit)}
        ransomware = str(ransomware or "").strip()
        if ransomware:
            params["ransomware"] = ransomware
        if cve:
            params["cve"] = cve
        cursor = str(cursor or "").strip()
        if cursor:
            params["cursor"] = cursor
        else:
            params["start_cursor"] = "true"
        payload, err = await _get_index(
            "ransomware", "list ransomware", params, timeout=45
        )
        if err:
            return err
        docs = payload.get("data") or []

        def _fmt(d: dict) -> str:
            cves = d.get("cve") or []
            cve_str = ", ".join(cves) if isinstance(cves, list) else str(cves)
            return (
                f"  {d.get('ransomware_family', '?')} | "
                f"added: {d.get('date_added') or '?'}"
                + (f"\n    Exploits CVEs: {_trunc(cve_str, 200)}" if cve_str else "")
            )

        out = format_entity_list(
            docs, _fmt, noun="ransomware family", total=_meta_total(payload)
        )
        nxt = _next_cursor(payload)
        if nxt:
            out += format_pagination_hint(nxt, param_name="cursor")
        return out

    async def list_vulncheck_botnets(
        botnet: str = "",
        cve: str = "",
        limit: int = 25,
        cursor: str = "",
    ) -> str:
        """List VulnCheck botnets and the CVEs they exploit."""
        cve = str(cve or "").strip().upper()
        if cve and not _is_valid_cve_id(cve):
            return "Error: 'cve' must be a valid CVE id, e.g. CVE-2024-3400."
        params: dict[str, Any] = {"limit": _clamp_limit(limit)}
        botnet = str(botnet or "").strip()
        if botnet:
            params["botnet"] = botnet
        if cve:
            params["cve"] = cve
        cursor = str(cursor or "").strip()
        if cursor:
            params["cursor"] = cursor
        else:
            params["start_cursor"] = "true"
        payload, err = await _get_index("botnets", "list botnets", params, timeout=45)
        if err:
            return err
        docs = payload.get("data") or []

        def _fmt(d: dict) -> str:
            cves = d.get("cve") or []
            cve_str = ", ".join(cves) if isinstance(cves, list) else str(cves)
            return (
                f"  {d.get('botnet_name') or d.get('botnet') or '?'} | "
                f"added: {d.get('date_added') or '?'}"
                + (f"\n    Exploits CVEs: {_trunc(cve_str, 200)}" if cve_str else "")
            )

        out = format_entity_list(
            docs, _fmt, noun="botnet", total=_meta_total(payload)
        )
        nxt = _next_cursor(payload)
        if nxt:
            out += format_pagination_hint(nxt, param_name="cursor")
        return out

    async def get_vulncheck_initial_access(
        cve: str = "",
        limit: int = 25,
        cursor: str = "",
    ) -> str:
        """List VulnCheck Initial Access Intelligence entries (CVE + vulnerable CPEs)."""
        cve = str(cve or "").strip().upper()
        if cve and not _is_valid_cve_id(cve):
            return "Error: 'cve' must be a valid CVE id, e.g. CVE-2024-3400."
        params: dict[str, Any] = {"limit": _clamp_limit(limit)}
        if cve:
            params["cve"] = cve
        cursor = str(cursor or "").strip()
        if cursor:
            params["cursor"] = cursor
        else:
            params["start_cursor"] = "true"
        payload, err = await _get_index(
            "initial-access", "initial access intelligence", params, timeout=45
        )
        if err:
            return err
        docs = payload.get("data") or []

        def _fmt(d: dict) -> str:
            cpes = d.get("vulnerable_cpes") or []
            cpe_str = ", ".join(cpes[:3]) + (f" (+{len(cpes) - 3})" if len(cpes) > 3 else "")
            return (
                f"  {d.get('cve', '?')} | in CISA KEV: {d.get('in_kev')}"
                + (f"\n    Vulnerable CPEs: {cpe_str}" if cpes else "")
            )

        out = format_entity_list(
            docs, _fmt, noun="initial-access entry", total=_meta_total(payload)
        )
        nxt = _next_cursor(payload)
        if nxt:
            out += format_pagination_hint(nxt, param_name="cursor")
        return out

    # ======================================================================
    # 3. IP / INFRASTRUCTURE INTELLIGENCE
    # ======================================================================

    async def lookup_vulncheck_ip_intel(
        window: str = "3d",
        cidr: str = "",
        asn: str = "",
        country: str = "",
        country_code: str = "",
        hostname: str = "",
        detection_id: str = "",
        limit: int = 25,
        cursor: str = "",
    ) -> str:
        """Query VulnCheck IP Intelligence (C2 / honeypot / initial-access IPs)."""
        window = str(window or "3d").strip().lower() or "3d"
        if window not in _IPINTEL_WINDOWS:
            return f"Error: window must be one of {', '.join(sorted(_IPINTEL_WINDOWS))}."
        detection_id = str(detection_id or "").strip().lower()
        if detection_id and detection_id not in _IPINTEL_IDS:
            return f"Error: detection_id must be one of {', '.join(sorted(_IPINTEL_IDS))}."
        params: dict[str, Any] = {"limit": _clamp_limit(limit)}
        # Strip BEFORE the truthiness check so a whitespace-only input is
        # treated as absent instead of being sent as an empty-string filter.
        for key, value in (
            ("cidr", cidr),
            ("asn", asn),
            ("country", country),
            ("country_code", country_code),
            ("hostname", hostname),
            ("id", detection_id),
        ):
            value = str(value or "").strip()
            if value:
                params[key] = value
        cursor = str(cursor or "").strip()
        if cursor:
            params["cursor"] = cursor
        else:
            params["start_cursor"] = "true"
        payload, err = await _get_index(
            f"ipintel-{window}", "IP intelligence", params, timeout=45
        )
        if err:
            return err
        docs = payload.get("data") or []

        def _fmt(d: dict) -> str:
            matches = d.get("matches") or []
            match_str = ", ".join(str(m) for m in matches[:5])
            return (
                f"  {d.get('ip', '?')} | "
                f"{d.get('country') or '?'} ({d.get('country_code') or '?'}) | "
                f"ASN: {d.get('asn') or '?'} | last seen: {d.get('last_seen') or '?'}"
                + (f" | matches: {match_str}" if match_str else "")
                + (f" | hostname: {d.get('hostname')}" if d.get("hostname") else "")
            )

        out = format_entity_list(
            docs,
            _fmt,
            noun="IP intel record",
            total=_meta_total(payload),
            empty_msg="No IP intelligence records for the given filters.",
        )
        nxt = _next_cursor(payload)
        if nxt:
            out += format_pagination_hint(nxt, param_name="cursor")
        return out

    async def get_vulncheck_ip_tag(tag: str, limit: int = 100) -> str:
        """Get the attacker IP denylist for a VulnCheck IP tag (e.g. vulncheck-c2)."""
        tag = (tag or "").strip().lower()
        if not _is_valid_tag_filter(tag):
            return (
                "Error: a valid IP tag name is required (e.g. 'vulncheck-c2'). "
                "Tag names are lowercase slugs."
            )
        # No ``format`` param on purpose: the documented default response is
        # newline-separated (built for firewall EDL/DBL consumption), which is
        # exactly what the line parser below expects. ``format=json`` is an
        # optional alternative representation we do not need.
        async with _client(timeout=45) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{VULNCHECK_API}/v3/tags/{tag}",
                _PROVIDER,
                f"IP tag {tag}",
                headers=headers,
            )
            if err:
                return err
            body = resp.text or ""
        ips = [line.strip() for line in body.splitlines() if line.strip()]
        if not ips:
            return f"The IP tag '{tag}' currently lists no IP addresses."
        cap = _clamp_limit(limit, default=100, max_value=_TAG_MAX_LIMIT)
        shown = ips[:cap]
        out = f"IP tag '{tag}' - {len(ips)} IP address(es) (for firewall / EDL blocking):\n"
        out += "\n".join(f"  {ip}" for ip in shown)
        if len(ips) > len(shown):
            out += f"\n[Showing {len(shown)} of {len(ips)} - raise limit for more.]"
        return out

    async def get_vulncheck_pdns_list(tag: str, limit: int = 100) -> str:
        """Get the Protective DNS hostname denylist for a VulnCheck tag."""
        tag = (tag or "").strip().lower()
        if not _is_valid_tag_filter(tag):
            return (
                "Error: a valid list name is required (e.g. 'vulncheck-c2'). "
                "List names are lowercase slugs."
            )
        # No ``format`` param on purpose: the documented default response is
        # newline-separated (built for Protective DNS denylist consumption),
        # which is exactly what the line parser below expects.
        async with _client(timeout=45) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{VULNCHECK_API}/v3/pdns/{tag}",
                _PROVIDER,
                f"PDNS list {tag}",
                headers=headers,
            )
            if err:
                return err
            body = resp.text or ""
        hosts = [line.strip() for line in body.splitlines() if line.strip()]
        if not hosts:
            return f"The Protective DNS list '{tag}' currently lists no hostnames."
        cap = _clamp_limit(limit, default=100, max_value=_TAG_MAX_LIMIT)
        shown = hosts[:cap]
        out = (
            f"Protective DNS list '{tag}' - {len(hosts)} hostname(s) "
            "(for DNS-layer blocking):\n"
        )
        out += "\n".join(f"  {h}" for h in shown)
        if len(hosts) > len(shown):
            out += f"\n[Showing {len(shown)} of {len(hosts)} - raise limit for more.]"
        return out

    # ======================================================================
    # 4. PACKAGE / PRODUCT MAPPING
    # ======================================================================

    async def get_vulncheck_cpe_cves(cpe: str, only_vulnerable: bool = False) -> str:
        """List the CVEs related to a CPE 2.2/2.3 URI string."""
        cpe = str(cpe or "").strip()
        if not cpe.lower().startswith("cpe:"):
            return (
                "Error: 'cpe' must be a CPE 2.2 or 2.3 URI, e.g. "
                "cpe:2.3:o:fortinet:fortios:*:*:*:*:*:*:*:* or "
                "cpe:/a:microsoft:internet_explorer:8.0.6001:beta."
            )
        params: dict[str, Any] = {"cpe": cpe}
        if only_vulnerable:
            params["isVulnerable"] = "true"
        async with _client(timeout=45) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{VULNCHECK_API}/v3/cpe",
                _PROVIDER,
                "CPE lookup",
                headers=headers,
                params=params,
            )
            if err:
                return err
            payload = resp.json() or {}
        cves = payload.get("data") or []
        total = _meta_total(payload) or len(cves)
        if not cves:
            return f"No CVEs found for CPE: {cpe}"
        shown = cves[:100]
        out = (
            f"{total} CVE(s) related to {cpe}"
            + (" (only where the CPE is vulnerable)" if only_vulnerable else "")
            + ":\n"
            + ", ".join(str(c) for c in shown)
        )
        if len(cves) > len(shown):
            out += f"\n[Showing {len(shown)} of {len(cves)}.]"
        return out

    async def search_vulncheck_cpe(
        vendor: str = "",
        product: str = "",
        version: str = "",
        part: str = "",
        only_vulnerable: bool = False,
    ) -> str:
        """Search for CVEs by CPE attributes (part / vendor / product / version)."""
        part = (part or "").strip().lower()
        if part and part not in ("a", "o", "h"):
            return "Error: part must be one of 'a' (application), 'o' (OS), or 'h' (hardware)."
        params: dict[str, Any] = {}
        # Strip BEFORE the truthiness check so a whitespace-only input can
        # neither be sent as an empty-string param nor satisfy the
        # at-least-one-field guard below.
        for key, value in (
            ("part", part),
            ("vendor", vendor),
            ("product", product),
            ("version", version),
        ):
            value = str(value or "").strip()
            if value:
                params[key] = value
        if not params:
            return (
                "Error: provide at least one of part, vendor, product, or version. "
                "Product/vendor names use underscores for spaces (no slashes/spaces)."
            )
        if only_vulnerable:
            params["isVulnerable"] = "true"
        async with _client(timeout=45) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{VULNCHECK_API}/v3/search/cpe",
                _PROVIDER,
                "CPE search",
                headers=headers,
                params=params,
            )
            if err:
                return err
            payload = resp.json() or {}
        cves = payload.get("data") or []
        total = _meta_total(payload) or len(cves)
        criteria = ", ".join(f"{k}={v}" for k, v in params.items() if k != "isVulnerable")
        if not cves:
            return f"No CVEs found for {criteria}."
        shown = cves[:100]
        out = (
            f"{total} CVE(s) for {criteria}"
            + (" (only where the CPE is vulnerable)" if only_vulnerable else "")
            + ":\n"
            + ", ".join(str(c) for c in shown)
        )
        if len(cves) > len(shown):
            out += f"\n[Showing {len(shown)} of {len(cves)}.]"
        return out

    async def lookup_vulncheck_purls(purls: list[str] | str) -> str:
        """Look up known vulnerabilities for a batch of package URLs (PURLs)."""
        # A bare string would otherwise be iterated character-by-character.
        if isinstance(purls, str):
            purls = [purls]
        cleaned = [str(p).strip() for p in (purls or []) if str(p or "").strip()]
        if not cleaned:
            return (
                "Error: provide at least one PURL, e.g. pkg:npm/lodash@4.17.20 or "
                "pkg:pypi/django@3.2.0."
            )
        if len(cleaned) > 100:
            return f"Error: too many PURLs ({len(cleaned)}). Submit at most 100 per call."
        async with _client(timeout=45) as client:
            resp, err = await safe_request(
                client,
                "post",
                f"{VULNCHECK_API}/v3/purls",
                _PROVIDER,
                "PURL lookup",
                headers=headers,
                json=cleaned,
            )
            if err:
                return err
            payload = resp.json() or {}
        entries = payload.get("data") or []

        def _fmt(d: dict) -> str:
            vulns = d.get("vulnerabilities") or []
            cves = d.get("cves") or []
            if vulns:
                detail = "; ".join(
                    f"{(v or {}).get('detection', '?')}"
                    + (f" (fixed in {v['fixed_version']})" if (v or {}).get("fixed_version") else "")
                    for v in vulns[:8]
                )
                extra = f" (+{len(vulns) - 8} more)" if len(vulns) > 8 else ""
                return f"  {d.get('purl', '?')}: {len(vulns)} vuln(s) - {detail}{extra}"
            if cves:
                return f"  {d.get('purl', '?')}: {', '.join(cves[:10])}"
            return f"  {d.get('purl', '?')}: no known vulnerabilities"

        return format_entity_list(
            entries,
            _fmt,
            noun="package",
            total=_meta_total(payload),
            empty_msg="None of the submitted PURLs matched a known vulnerability.",
        )

    async def identify_vulncheck_product(
        vendor: str = "", product: str = "", version: str = ""
    ) -> str:
        """Resolve a (vendor, product, version) query to best-match CPE + PURL identifiers."""
        params: dict[str, Any] = {}
        # Strip BEFORE the truthiness check (same rationale as search_vulncheck_cpe).
        for key, value in (("vendor", vendor), ("product", product), ("version", version)):
            value = str(value or "").strip()
            if value:
                params[key] = value
        if not params:
            return "Error: provide at least one of vendor, product, or version."
        async with _client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{VULNCHECK_API}/v3/identify",
                _PROVIDER,
                "identify product",
                headers=headers,
                params=params,
            )
            if err:
                return err
            payload = resp.json() or {}
        results = payload if isinstance(payload, list) else (payload.get("data") or [])
        if not results:
            return f"No identifiers matched {params}."

        def _identifier(item: Any) -> str:
            """Render one identifier entry (dict with value/confidence, or a bare string)."""
            if not isinstance(item, dict):
                return str(item)
            value = item.get("value") or "?"
            level = (item.get("confidence") or {}).get("level")
            return f"{value} [confidence: {level}]" if level else str(value)

        lines = [f"Identifiers for {params}:"]
        for entry in results[:10]:
            ident = (entry or {}).get("identity") or {}
            identifiers = (entry or {}).get("identifiers") or {}
            cpes = [_identifier(c) for c in (identifiers.get("cpe") or []) if c]
            purls = [_identifier(p) for p in (identifiers.get("purl") or []) if p]
            label = " ".join(
                str(ident.get(k)) for k in ("vendor", "product", "version") if ident.get(k)
            )
            lines.append(f"  {label or '?'}")
            if cpes:
                lines.append(f"    CPE: {', '.join(cpes[:5])}")
            if purls:
                lines.append(f"    PURL: {', '.join(purls[:5])}")
        return "\n".join(lines)

    # ======================================================================
    # 5. DETECTION CONTENT
    # ======================================================================

    async def get_vulncheck_initial_access_rules(rule_type: str, limit: int = 50) -> str:
        """Download VulnCheck Initial Access Intelligence Suricata or Snort rules."""
        rule_type = (rule_type or "").strip().lower()
        if rule_type not in ("suricata", "snort"):
            return "Error: rule_type must be 'suricata' or 'snort'."
        async with _client(timeout=60) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{VULNCHECK_API}/v3/rules/initial-access/{rule_type}",
                _PROVIDER,
                f"{rule_type} rules",
                headers=headers,
            )
            if err:
                return err
            body = resp.text or ""
        rules = [
            line.strip()
            for line in body.splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        if not rules:
            return f"No {rule_type} Initial Access Intelligence rules are available for this key."
        cap = _clamp_limit(limit, default=50, max_value=500)
        shown = rules[:cap]
        out = (
            f"VulnCheck Initial Access Intelligence {rule_type} rules "
            f"({len(rules)} total):\n"
        )
        # Rules must stay deployable: Suricata/Snort signatures routinely exceed
        # 300 chars, so cap generously instead of using the default _trunc limit.
        out += "\n".join(f"  {_trunc(r, 2000)}" for r in shown)
        if len(rules) > len(shown):
            out += f"\n[Showing {len(shown)} of {len(rules)} rules - raise limit for more.]"
        return out

    # ======================================================================
    # REGISTER TOOLS
    # ======================================================================

    executor.register("list_vulncheck_indexes", list_vulncheck_indexes)
    executor.register("query_vulncheck_index", query_vulncheck_index)
    executor.register("lookup_vulncheck_cve", lookup_vulncheck_cve)
    executor.register("get_vulncheck_kev", get_vulncheck_kev)
    executor.register("get_vulncheck_exploits", get_vulncheck_exploits)
    executor.register("get_vulncheck_epss", get_vulncheck_epss)
    executor.register("query_vulncheck_advisory", query_vulncheck_advisory)
    executor.register("list_vulncheck_threat_actors", list_vulncheck_threat_actors)
    executor.register("list_vulncheck_ransomware", list_vulncheck_ransomware)
    executor.register("list_vulncheck_botnets", list_vulncheck_botnets)
    executor.register("get_vulncheck_initial_access", get_vulncheck_initial_access)
    executor.register("lookup_vulncheck_ip_intel", lookup_vulncheck_ip_intel)
    executor.register("get_vulncheck_ip_tag", get_vulncheck_ip_tag)
    executor.register("get_vulncheck_pdns_list", get_vulncheck_pdns_list)
    executor.register("get_vulncheck_cpe_cves", get_vulncheck_cpe_cves)
    executor.register("search_vulncheck_cpe", search_vulncheck_cpe)
    executor.register("lookup_vulncheck_purls", lookup_vulncheck_purls)
    executor.register("identify_vulncheck_product", identify_vulncheck_product)
    executor.register(
        "get_vulncheck_initial_access_rules", get_vulncheck_initial_access_rules
    )

    return [
        ToolDefinition(
            name="lookup_vulncheck_cve",
            description=(
                "Look up the full enriched CVE record from VulnCheck's NVD-2 index: "
                "description, CVSS score/severity, published and last-modified dates, "
                "vulnerable CPEs, and references. This is the first tool to use when "
                "you need the details of a specific CVE. Prefers the commercial "
                "vulncheck-nvd2 index (early + enriched data) and falls back to the "
                "community nist-nvd2 index if the key is not licensed for it. For "
                "exploitation status use get_vulncheck_exploits or get_vulncheck_kev; "
                "for exploit probability use get_vulncheck_epss."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "cve": {"type": "string", "description": "CVE id, e.g. CVE-2024-3400"},
                    "prefer_community": {
                        "type": "boolean",
                        "default": False,
                        "description": "Query the community nist-nvd2 index first (default: enriched vulncheck-nvd2 first)",
                    },
                },
                "required": ["cve"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="get_vulncheck_kev",
            description=(
                "Check the VulnCheck KEV (Known Exploited Vulnerabilities) index - "
                "VulnCheck's broader, faster-moving superset of the CISA KEV. Pass a "
                "'cve' to get a clear IS / IS NOT in the KEV verdict plus the entry "
                "detail (vendor/product, date added, known ransomware campaign use), "
                "or omit it to browse recent KEV additions. Use this for "
                "vulnerability prioritization - KEV membership means the CVE is "
                "confirmed exploited in the wild."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "cve": {
                        "type": "string",
                        "description": "CVE id to check, e.g. CVE-2024-3400 (omit to list recent KEV entries)",
                    },
                    "limit": {
                        "type": "integer",
                        "default": 25,
                        "description": "Max entries when browsing (1-200)",
                    },
                },
                "required": [],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="get_vulncheck_exploits",
            description=(
                "Get VulnCheck exploit intelligence for a CVE: whether a public or "
                "weaponized exploit exists, the maximum exploit maturity, whether it "
                "is reported exploited by threat actors / ransomware / botnets, CISA "
                "and VulnCheck KEV membership, EPSS, exploit counts, the exploit "
                "timeline, and the list of known exploit references (name, maturity, "
                "URL). Use this to answer 'is this CVE actually being exploited and "
                "how mature are the exploits?'."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "cve": {"type": "string", "description": "CVE id, e.g. CVE-2024-3400"},
                    "limit": {"type": "integer", "default": 25},
                },
                "required": ["cve"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="get_vulncheck_epss",
            description=(
                "Get the EPSS (Exploit Prediction Scoring System) score and "
                "percentile for a CVE from VulnCheck. The score is the probability "
                "the CVE will be exploited in the next 30 days; the percentile ranks "
                "it against all scored CVEs. Use to prioritize remediation by "
                "likelihood of exploitation. Combine with get_vulncheck_kev "
                "(confirmed exploited) and get_vulncheck_exploits (exploit maturity)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "cve": {"type": "string", "description": "CVE id, e.g. CVE-2024-3400"},
                },
                "required": ["cve"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="query_vulncheck_advisory",
            description=(
                "Query VulnCheck's v4 advisory index (structured CVE 5.0 records) "
                "across all vendor advisory feeds. Filter by cve_id, vendor, product, "
                "version (semver-aware), cpe (2.3 string), purl, feed name, or "
                "updated_after (RFC3339). Returns advisory records with CVE id, "
                "state, title, and description. Use this for vendor advisory lookups "
                "and 'what advisories affect vendor/product X?' queries; for the "
                "exploitation rollup on a single CVE prefer lookup_vulncheck_cve / "
                "get_vulncheck_exploits. At least one filter is required."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "cve_id": {"type": "string", "description": "CVE id, e.g. CVE-2024-1234"},
                    "vendor": {"type": "string", "description": "Vendor name, e.g. rockwell"},
                    "product": {"type": "string", "description": "Product name, e.g. windows"},
                    "version": {"type": "string", "description": "Product version (semver-aware)"},
                    "cpe": {"type": "string", "description": "CPE 2.3 string, e.g. cpe:2.3:a:vendor:product:*"},
                    "purl": {"type": "string", "description": "Package URL, e.g. pkg:npm/lodash@4.17.20"},
                    "name": {"type": "string", "description": "Advisory feed name, e.g. rockwell"},
                    "updated_after": {
                        "type": "string",
                        "description": "RFC3339 timestamp, e.g. 2026-01-01T00:00:00Z",
                    },
                    "limit": {"type": "integer", "default": 10, "description": "Results per page (1-100)"},
                    "page": {"type": "integer", "default": 1, "description": "Page number"},
                },
                "required": [],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="list_vulncheck_indexes",
            description=(
                "List the VulnCheck indexes the API key is licensed for (name + "
                "description), in alphabetical order. Use this to discover which data "
                "sources are available (KEV, exploits, epss, threat-actors, "
                "ransomware, botnets, initial-access, ipintel-*, and hundreds of "
                "vendor advisory indexes) before calling query_vulncheck_index. "
                "Optionally filter by a substring of the name or description."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "filter": {
                        "type": "string",
                        "description": "Optional substring to filter index name/description, e.g. 'ipintel'",
                    },
                },
                "required": [],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="query_vulncheck_index",
            description=(
                "Query ANY licensed VulnCheck index by name with the full documented "
                "filter set - the consolidated escape hatch for indexes without a "
                "dedicated tool (e.g. the hundreds of per-vendor advisory indexes "
                "like 'adobe', 'cisco', 'moxa'). Filters: cve, alias, threat_actor, "
                "mitre_id, ransomware, botnet, published, last_mod_start_date, "
                "pub_start_date, sort (_timestamp|date_added), order (asc|desc). "
                "Paginates with a cursor (pass the returned cursor to continue). Call "
                "list_vulncheck_indexes first to find the index name. For common "
                "indexes prefer the dedicated tools (get_vulncheck_kev, "
                "get_vulncheck_exploits, get_vulncheck_epss)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "index": {
                        "type": "string",
                        "description": "Index name, e.g. 'vulncheck-kev', 'exploits', 'adobe'",
                    },
                    "cve": {"type": "string", "description": "Filter by CVE id"},
                    "alias": {"type": "string", "description": "Filter by alias, e.g. PrintNightmare"},
                    "threat_actor": {"type": "string", "description": "Filter by threat actor name"},
                    "mitre_id": {"type": "string", "description": "Filter by MITRE ATT&CK group id, e.g. G0013"},
                    "ransomware": {"type": "string", "description": "Filter by ransomware group name"},
                    "botnet": {"type": "string", "description": "Filter by botnet name"},
                    "published": {"type": "string", "description": "Filter by published flag/date"},
                    "last_mod_start_date": {
                        "type": "string",
                        "description": "Only records modified on/after this date (YYYY-MM-DD)",
                    },
                    "pub_start_date": {
                        "type": "string",
                        "description": "Only records published on/after this date (YYYY-MM-DD)",
                    },
                    "sort": {
                        "type": "string",
                        "enum": ["_timestamp", "date_added"],
                        "description": "Sort field",
                    },
                    "order": {"type": "string", "enum": ["asc", "desc"], "description": "Sort order"},
                    "limit": {"type": "integer", "default": 25, "description": "Page size (1-200)"},
                    "cursor": {
                        "type": "string",
                        "description": "Pagination cursor from a previous response",
                    },
                },
                "required": ["index"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="list_vulncheck_threat_actors",
            description=(
                "List VulnCheck threat actors and the CVEs they target. Filter by "
                "threat_actor name, mitre_id (MITRE ATT&CK group id, e.g. G0013), "
                "misp_id, or cve (which actors target a given CVE). Use this to "
                "answer 'who is exploiting CVE-X?' or to profile a named actor's "
                "targeted vulnerabilities."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "threat_actor": {"type": "string", "description": "Threat actor name, e.g. UNC2630"},
                    "mitre_id": {"type": "string", "description": "MITRE ATT&CK group id, e.g. G0013"},
                    "misp_id": {"type": "string", "description": "MISP threat actor id (UUID)"},
                    "cve": {"type": "string", "description": "Only actors targeting this CVE, e.g. CVE-2024-3400"},
                    "limit": {"type": "integer", "default": 25, "description": "Page size (1-200)"},
                    "cursor": {"type": "string", "description": "Pagination cursor from a previous response"},
                },
                "required": [],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="list_vulncheck_ransomware",
            description=(
                "List ransomware families tracked by VulnCheck and the CVEs they "
                "exploit. Filter by ransomware group name or by cve (which "
                "ransomware families exploit a given CVE). Use for ransomware "
                "exposure analysis and 'is CVE-X used in ransomware campaigns?'."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "ransomware": {"type": "string", "description": "Ransomware group name"},
                    "cve": {"type": "string", "description": "Only families exploiting this CVE"},
                    "limit": {"type": "integer", "default": 25, "description": "Page size (1-200)"},
                    "cursor": {"type": "string", "description": "Pagination cursor from a previous response"},
                },
                "required": [],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="list_vulncheck_botnets",
            description=(
                "List botnets tracked by VulnCheck and the CVEs they exploit. Filter "
                "by botnet name or by cve. Use to understand which botnets weaponize "
                "a vulnerability and for botnet-driven exploitation exposure."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "botnet": {"type": "string", "description": "Botnet name, e.g. Fbot"},
                    "cve": {"type": "string", "description": "Only botnets exploiting this CVE"},
                    "limit": {"type": "integer", "default": 25, "description": "Page size (1-200)"},
                    "cursor": {"type": "string", "description": "Pagination cursor from a previous response"},
                },
                "required": [],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="get_vulncheck_initial_access",
            description=(
                "List VulnCheck Initial Access Intelligence entries - remotely "
                "exploitable (typically unauthenticated RCE) vulnerabilities used for "
                "initial access, with their vulnerable CPEs and CISA KEV status. "
                "Filter by cve. Use to identify internet-facing initial-access "
                "risks; the matching Suricata/Snort detection rules are part of the "
                "same Initial Access Intelligence product."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "cve": {"type": "string", "description": "Only the entry for this CVE"},
                    "limit": {"type": "integer", "default": 25, "description": "Page size (1-200)"},
                    "cursor": {"type": "string", "description": "Pagination cursor from a previous response"},
                },
                "required": [],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="lookup_vulncheck_ip_intel",
            description=(
                "Query VulnCheck IP Intelligence for malicious infrastructure over a "
                "rolling window (3d / 10d / 30d / 90d): C2, honeypot, and "
                "initial-access source IPs with country, ASN, matched threat "
                "signatures, hostname, and last-seen. Filter by cidr (an IP or CIDR "
                "block), asn (e.g. AS394107), country / country_code, hostname, or "
                "detection_id (c2, initial-access, honeypot). Use to enrich an IP or "
                "to pull attacker infrastructure for a region/ASN. For a ready-made "
                "blocklist use get_vulncheck_ip_tag."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "window": {
                        "type": "string",
                        "enum": ["3d", "10d", "30d", "90d"],
                        "default": "3d",
                        "description": "Rolling detection window",
                    },
                    "cidr": {"type": "string", "description": "IP address or CIDR block, e.g. 77.68.0.0/16"},
                    "asn": {"type": "string", "description": "ASN, e.g. AS394107"},
                    "country": {"type": "string", "description": "Country name, e.g. China"},
                    "country_code": {"type": "string", "description": "Country code, e.g. CN"},
                    "hostname": {"type": "string", "description": "Hostname, e.g. example.com"},
                    "detection_id": {
                        "type": "string",
                        "enum": ["c2", "initial-access", "honeypot"],
                        "description": "Detection category to filter by",
                    },
                    "limit": {"type": "integer", "default": 25, "description": "Page size (1-200)"},
                    "cursor": {"type": "string", "description": "Pagination cursor from a previous response"},
                },
                "required": [],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="get_vulncheck_ip_tag",
            description=(
                "Get the list of IP addresses for a VulnCheck IP tag (e.g. "
                "'vulncheck-c2') - a ready-to-use attacker/C2 denylist for "
                "Next-Generation firewall External/Dynamic Block Lists (EBL/DBL). "
                "Returns the IPs (capped by limit). Use when you need a blockable IP "
                "list rather than per-IP context (for context use "
                "lookup_vulncheck_ip_intel)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "tag": {
                        "type": "string",
                        "description": "IP tag name (lowercase slug), e.g. vulncheck-c2",
                    },
                    "limit": {
                        "type": "integer",
                        "default": 100,
                        "description": "Max IPs to return (1-1000)",
                    },
                },
                "required": ["tag"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="get_vulncheck_pdns_list",
            description=(
                "Get the list of hostnames for a VulnCheck Protective DNS list (e.g. "
                "'vulncheck-c2') - a ready-to-use denylist for Protective DNS / "
                "DNS-layer blocking of C2 and attacker infrastructure. Returns the "
                "hostnames (capped by limit). The DNS-hostname counterpart of "
                "get_vulncheck_ip_tag."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "tag": {
                        "type": "string",
                        "description": "PDNS list name (lowercase slug), e.g. vulncheck-c2",
                    },
                    "limit": {
                        "type": "integer",
                        "default": 100,
                        "description": "Max hostnames to return (1-1000)",
                    },
                },
                "required": ["tag"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="get_vulncheck_cpe_cves",
            description=(
                "List the CVEs related to a specific CPE 2.2 or 2.3 URI string. By "
                "default returns every CVE that mentions the CPE (including as part "
                "of a vulnerable configuration); set only_vulnerable=true to return "
                "only CVEs where the CPE itself is the vulnerable component. Use when "
                "you already have a full CPE URI; to search by vendor/product/version "
                "attributes use search_vulncheck_cpe."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "cpe": {
                        "type": "string",
                        "description": "CPE 2.2/2.3 URI, e.g. cpe:2.3:o:fortinet:fortios:*:*:*:*:*:*:*:*",
                    },
                    "only_vulnerable": {
                        "type": "boolean",
                        "default": False,
                        "description": "Only CVEs where the CPE is the vulnerable component",
                    },
                },
                "required": ["cpe"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="search_vulncheck_cpe",
            description=(
                "Search for CVEs by CPE attributes without building a full CPE URI: "
                "part ('a' application, 'o' OS, 'h' hardware), vendor, product, and "
                "version (supports a trailing '*' wildcard, e.g. version='8.0.6*'). "
                "Product/vendor names cannot contain spaces or slashes - use "
                "underscores for spaces. Set only_vulnerable=true to restrict to CVEs "
                "where the CPE is vulnerable. Use this for 'what CVEs affect vendor X "
                "product Y version Z?'."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "vendor": {"type": "string", "description": "Vendor, e.g. mediatek"},
                    "product": {"type": "string", "description": "Product (underscores for spaces), e.g. mt6890"},
                    "version": {"type": "string", "description": "Version, supports trailing '*', e.g. 8.0.6*"},
                    "part": {
                        "type": "string",
                        "enum": ["a", "o", "h"],
                        "description": "CPE part: a=application, o=OS, h=hardware",
                    },
                    "only_vulnerable": {
                        "type": "boolean",
                        "default": False,
                        "description": "Only CVEs where the CPE is the vulnerable component",
                    },
                },
                "required": [],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="lookup_vulncheck_purls",
            description=(
                "Look up known vulnerabilities for a batch of package URLs (PURLs, "
                "up to 100). Returns, per package, the associated CVEs and any "
                "fixed_version. Use for software-composition / dependency analysis - "
                "'is this package version vulnerable, and what fixes it?'. PURLs look "
                "like pkg:npm/lodash@4.17.20, pkg:pypi/django@3.2.0, "
                "pkg:maven/org.apache.commons/commons-text@1.9."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "purls": {
                        # The handler also accepts a bare string (wrapped in a
                        # list) - keep the schema in sync with that contract.
                        "oneOf": [
                            {"type": "array", "items": {"type": "string"}},
                            {"type": "string"},
                        ],
                        "description": "Package URLs to check (max 100); a single PURL string is also accepted",
                    },
                },
                "required": ["purls"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="identify_vulncheck_product",
            description=(
                "Resolve a free-form (vendor, product, version) query into the "
                "best-match CPE and PURL identifiers with confidence levels. Use this "
                "first to turn a human product name (e.g. vendor='Microsoft "
                "Corporation', product='Office 2016') into a canonical CPE or PURL, "
                "then feed that into get_vulncheck_cpe_cves or lookup_vulncheck_purls."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "vendor": {"type": "string", "description": "Vendor name, e.g. microsoft"},
                    "product": {"type": "string", "description": "Product name, e.g. office"},
                    "version": {"type": "string", "description": "Version, e.g. 2016"},
                },
                "required": [],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="get_vulncheck_initial_access_rules",
            description=(
                "Download VulnCheck's Initial Access Intelligence Suricata or Snort "
                "detection rules - network signatures that detect remote exploitation "
                "of initial-access (typically unauthenticated RCE) vulnerabilities. "
                "Returns the rule text (capped by limit). Use to deploy or review "
                "network detections for the vulnerabilities surfaced by "
                "get_vulncheck_initial_access. Requires the Initial Access "
                "Intelligence entitlement."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "rule_type": {
                        "type": "string",
                        "enum": ["suricata", "snort"],
                        "description": "Which rule format to fetch",
                    },
                    "limit": {
                        "type": "integer",
                        "default": 50,
                        "description": "Max rules to return (1-500)",
                    },
                },
                "required": ["rule_type"],
            },
            source=_SOURCE,
            priority=0,
        ),
    ]
