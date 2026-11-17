# SPDX-License-Identifier: Apache-2.0

"""urlscan.io integration - URL and website scanning sandbox.

Covers the core urlscan.io API v1 surfaces (search, submit, result, DOM,
Pro structure search, quotas), designed as the URL-analysis building block
for an agentic SOC/CTI/phishing-triage team:

  - Search (*hunting / pivoting*): query the historical scan corpus with
    Elasticsearch query-string syntax (``GET /api/v1/search/``) - by domain,
    IP, hash, ASN, tags, dates - with ``search_after`` deep pagination
  - Submit (*write*): submit a URL for scanning (``POST /api/v1/scan/``) with
    explicit public/unlisted/private visibility (defaults to **unlisted** so
    potentially sensitive URLs never land on the public front page)
  - Result (*enrichment*): retrieve the finished scan
    (``GET /api/v1/result/{uuid}/``) - verdicts, page info, detected
    technologies, request/transfer stats, contacted IPs/domains, plus the
    screenshot and DOM snapshot URLs
  - DOM (*investigation*): download the recorded DOM snapshot
    (``GET /dom/{uuid}/``) for content inspection (phishing kit markers,
    obfuscated JS, form actions)
  - Structure search (*urlscan Pro*): find structurally similar scans
    (``GET /api/v1/pro/result/{uuid}/similar/``) to expand from one phishing
    page to the whole campaign
  - Quotas (*status*): per-action rate limits and remaining quota
    (``GET /user/quotas/``), also the connectivity probe

Authentication
--------------
urlscan.io authenticates with a single **API key** sent in the ``API-Key``
HTTP header (the vendor explicitly documents this exact header name). The key
is stored in ``api_key_enc`` and surfaces here as the plain token - no
compound assembly needed. Unauthenticated calls work for some endpoints but
receive only minimal quotas, so the integration is designed for keyed usage.

Rate limits are per-action fixed windows (minute/hour/day, reset at the full
minute/hour/midnight UTC) that depend on the subscription; every response
carries ``X-Rate-Limit-*`` headers and HTTP 429 responses are retried by the
shared ``safe_request`` helper. Scans typically take 10-30 seconds to finish;
until then the Result API returns HTTP 404.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

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

_PROVIDER = "urlscan.io"
_SOURCE = "integration:urlscan"

# All API endpoints live under the single public host (no self-hosted variant).
URLSCAN_BASE = "https://urlscan.io"
URLSCAN_API = f"{URLSCAN_BASE}/api/v1"

_DEFAULT_TIMEOUT = 30
# The API accepts up to 10,000 results per page, but each hit renders as a
# multi-line block in the tool output - cap the page size to keep LLM
# context sane and rely on ``search_after`` pagination for deeper retrieval.
_SEARCH_MAX_SIZE = 100
_DOM_MAX_CHARS = 20_000

_VISIBILITIES: frozenset[str] = frozenset({"public", "unlisted", "private"})


def _trunc(text: Any, max_len: int = 200) -> str:
    if text is None:
        return ""
    text = str(text).replace("\r", " ").replace("\n", " ").strip()
    return text[:max_len] + ("…" if len(text) > max_len else "")


def _clamp(value: Any, lo: int, hi: int, default: int) -> int:
    try:
        return max(lo, min(int(value), hi))
    except (TypeError, ValueError):
        return default


def _verdict_icon(score: Any, malicious: Any) -> str:
    """Verdict icon: malicious flag wins, then the 0-100 score bands."""
    if malicious:
        return "🔴"
    try:
        value = int(score)
    except (TypeError, ValueError):
        return "⚪"
    if value >= 75:
        return "🔴"
    if value >= 25:
        return "🟠"
    if value > 0:
        return "🟡"
    return "🟢"


# ---------------------------------------------------------------------------
# Auth helper (shared with integration_testing.py)
# ---------------------------------------------------------------------------


def build_urlscan_headers(api_key: str) -> dict[str, str]:
    """Build urlscan.io request headers (``API-Key`` auth + JSON Accept).

    The key is stripped so a value pasted with trailing/leading whitespace can
    never produce a malformed header. The vendor documents the exact header
    name ``API-Key`` (not ``x-api-key``). This is the single chokepoint shared
    by the tool builder and the connectivity test.
    """
    return {
        "API-Key": (api_key or "").strip(),
        "Accept": "application/json",
    }


# ---------------------------------------------------------------------------
# Response formatting helpers
# ---------------------------------------------------------------------------


def _format_search_hit(hit: dict) -> str:
    """One compact block per Search API result."""
    page = hit.get("page") or {}
    task = hit.get("task") or {}
    stats = hit.get("stats") or {}
    tags = task.get("tags") or []
    parts = [
        f"  {hit.get('_id', '?')} - {str(task.get('time', ''))[:16]} "
        f"[{task.get('visibility', '?')}]",
        f"    URL: {_trunc(page.get('url') or task.get('url'), 140)}",
        f"    Domain: {page.get('domain') or '?'} | IP: {page.get('ip') or '?'} "
        f"({page.get('asn') or '?'}, {page.get('country') or '?'}) | "
        f"Server: {page.get('server') or '?'}",
    ]
    extras: list[str] = []
    if stats.get("requests") is not None:
        extras.append(f"requests: {stats.get('requests')}")
    if stats.get("uniqIPs") is not None:
        extras.append(f"unique IPs: {stats.get('uniqIPs')}")
    if tags:
        extras.append(f"tags: {', '.join(str(t) for t in tags[:6])}")
    if extras:
        parts.append(f"    {' | '.join(extras)}")
    return "\n".join(parts)


def _format_verdicts(verdicts: dict) -> list[str]:
    """Compact lines for the result ``verdicts`` object."""
    lines: list[str] = []
    overall = verdicts.get("overall") or {}
    icon = _verdict_icon(overall.get("score"), overall.get("malicious"))
    line = (
        f"Verdict: {icon} score {overall.get('score', 0)}/100 | "
        f"malicious: {overall.get('malicious', False)}"
    )
    categories = overall.get("categories") or []
    if categories:
        line += f" | categories: {', '.join(str(c) for c in categories[:6])}"
    brands = overall.get("brands") or []
    if brands:
        names = [
            str(b.get("name") or b) if isinstance(b, dict) else str(b)
            for b in brands[:6]
        ]
        line += f" | targeted brands: {', '.join(names)}"
    lines.append(line)
    community = verdicts.get("community") or {}
    if community.get("votesTotal"):
        lines.append(
            f"Community: {community.get('votesMalicious', 0)} malicious / "
            f"{community.get('votesBenign', 0)} benign of "
            f"{community.get('votesTotal', 0)} votes"
        )
    engines = verdicts.get("engines") or {}
    if engines.get("enginesTotal"):
        lines.append(
            f"Engines: {engines.get('maliciousTotal', 0)} of "
            f"{engines.get('enginesTotal', 0)} flagged malicious"
        )
    return lines


def _format_technologies(meta: dict) -> str:
    """Detected technologies (Wappalyzer processor) as one line, or ``""``."""
    wappa = ((meta.get("processors") or {}).get("wappa") or {}).get("data") or []
    if not wappa:
        return ""
    techs: list[str] = []
    for entry in wappa[:12]:
        app = entry.get("app") or "?"
        cats = entry.get("categories") or []
        cat = cats[0].get("name") if cats and isinstance(cats[0], dict) else None
        techs.append(f"{app} ({cat})" if cat else str(app))
    extra = f" (+{len(wappa) - 12} more)" if len(wappa) > 12 else ""
    return "Technologies: " + ", ".join(techs) + extra


# ---------------------------------------------------------------------------
# Provider class
# ---------------------------------------------------------------------------


class UrlscanIntegration(BaseIntegration):
    """urlscan.io website scanning and URL intelligence integration."""

    provider_name = "urlscan"
    display_name = "urlscan.io"
    # "SearchCheck" exists in the frontend ICON_MAP (constants.ts) - an
    # unknown key would silently fall back to the generic Bot icon.
    icon = "SearchCheck"
    description = (
        "Connect urlscan.io to scan and analyse suspicious websites: search "
        "historical scans by domain/IP/hash/ASN, submit URLs for sandboxed "
        "scanning, and retrieve verdicts, page details, technologies, "
        "screenshots, and DOM snapshots."
    )
    auth_types = ["api_key"]

    def get_oauth_defaults(self) -> OAuthDefaults | None:
        return None

    async def validate_connection(self, token: str) -> bool:
        # GET /user/quotas/ answers 200 even WITHOUT a key (anonymous
        # IP-scoped quotas) but 400 for an invalid key, so a blank key must
        # be rejected up front or it would validate as "connected".
        api_key = (token or "").strip()
        if not api_key:
            return False
        # ``safe_request`` converts ``httpx.HTTPError`` into a ``(resp, err)``
        # tuple and never raises, so no surrounding try/except is needed.
        async with egress_client(timeout=15, follow_redirects=True) as client:
            _resp, err = await safe_request(
                client,
                "get",
                f"{URLSCAN_BASE}/user/quotas/",
                _PROVIDER,
                "validate connection",
                headers=build_urlscan_headers(api_key),
            )
        return err is None

    async def get_user_info(self, token: str) -> dict:
        api_key = (token or "").strip()
        if not api_key:
            return {}
        async with egress_client(timeout=15, follow_redirects=True) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{URLSCAN_BASE}/user/quotas/",
                _PROVIDER,
                "get user info",
                headers=build_urlscan_headers(api_key),
            )
        if err or resp is None:
            return {}
        return resp.json() or {}


# ---------------------------------------------------------------------------
# Tool builder
# ---------------------------------------------------------------------------


def build_tools(
    token: str,
    executor: CallableToolExecutor,
    **_kwargs: Any,
) -> list[ToolDefinition]:
    """Build urlscan.io tools from the plain API-key token.

    The tools always build (the catalog introspects providers with a dummy
    token); a missing/blank key is surfaced as a call-time error instead.
    """
    api_key = (token or "").strip()
    headers = build_urlscan_headers(api_key)

    def _client(timeout: int = _DEFAULT_TIMEOUT) -> httpx.AsyncClient:
        # urlscan.io documents that clients MUST follow HTTP 301/302
        # redirects (e.g. /user/quotas and some result assets redirect).
        return egress_client(timeout=timeout, follow_redirects=True)

    def _credentials_error() -> str | None:
        if not api_key:
            return (
                "Error: urlscan.io credentials are not configured - set the "
                "API key on the integration and test the connection."
            )
        return None

    # ══════════════════════════════════════════════════════════════════════
    # 1. SEARCH (hunting / pivoting)
    # ══════════════════════════════════════════════════════════════════════

    async def search_urlscan_scans(
        query: str,
        size: int = 20,
        search_after: str = "",
    ) -> str:
        """Search historical urlscan.io scans with ES query-string syntax."""
        if err := _credentials_error():
            return err
        query = str(query or "").strip()
        if not query:
            return (
                "Error: a search query is required (Elasticsearch query "
                "string), e.g. 'domain:example.com', 'page.ip:\"1.2.3.4\"', "
                "'hash:SHA256', 'page.asn:AS24940', or "
                "'task.tags:phishing AND date:>now-7d'."
            )
        params: dict[str, Any] = {
            "q": query,
            "size": _clamp(size, 1, _SEARCH_MAX_SIZE, 20),
        }
        search_after = str(search_after or "").strip()
        if search_after:
            params["search_after"] = search_after
        async with _client(timeout=60) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{URLSCAN_API}/search/",
                _PROVIDER,
                "search scans",
                headers=headers,
                params=params,
            )
            if err:
                return err
            data = resp.json() or {}
        results = data.get("results") or []
        total = data.get("total", len(results))
        out = format_entity_list(
            results,
            _format_search_hit,
            noun="scan",
            total=total,
            empty_msg=f"No scans found for query: {query}",
        )
        has_more = bool(data.get("has_more")) or (
            bool(results) and total > len(results)
        )
        if results and has_more:
            last_sort = (results[-1].get("sort") or []) if results else []
            cursor = ",".join(str(s) for s in last_sort)
            out += format_pagination_hint(cursor, param_name="search_after")
        if results:
            out += (
                "\n[Use get_urlscan_result with a scan UUID for full verdicts "
                "and page details]"
            )
        return out

    # ══════════════════════════════════════════════════════════════════════
    # 2. SUBMIT (write) + RESULT / DOM (enrichment, investigation)
    # ══════════════════════════════════════════════════════════════════════

    async def submit_urlscan_scan(
        url: str,
        visibility: str = "unlisted",
        tags: list[str] | None = None,
        country: str = "",
        custom_user_agent: str = "",
        referer: str = "",
    ) -> str:
        """Submit a URL for scanning (returns the scan UUID to poll)."""
        if err := _credentials_error():
            return err
        url = str(url or "").strip()
        if not url:
            return "Error: url is required (the URL to scan, e.g. https://example.com/path)."
        if "://" not in url:
            # urlscan rejects non-HTTP schemes; a bare hostname is the common
            # agent input, so default it to https.
            url = f"https://{url}"
        scheme = urlparse(url).scheme.lower()
        if scheme not in ("http", "https"):
            return (
                f"Error: unsupported URL scheme '{scheme}' - urlscan.io only "
                "scans http/https URLs."
            )
        visibility = str(visibility or "unlisted").strip().lower()
        if visibility not in _VISIBILITIES:
            return "Error: visibility must be one of public, unlisted, private."
        body: dict[str, Any] = {"url": url, "visibility": visibility}
        clean_tags = [str(t).strip() for t in (tags or []) if str(t or "").strip()]
        if clean_tags:
            if len(clean_tags) > 10:
                return "Error: at most 10 tags are allowed per scan."
            body["tags"] = clean_tags
        country = str(country or "").strip().lower()
        if country:
            if len(country) != 2 or not country.isalpha():
                return (
                    "Error: country must be a 2-letter ISO-3166-1 alpha-2 code "
                    "(e.g. 'de', 'us')."
                )
            body["country"] = country
        if str(custom_user_agent or "").strip():
            body["customagent"] = str(custom_user_agent).strip()
        if str(referer or "").strip():
            body["referer"] = str(referer).strip()
        async with _client() as client:
            resp, err = await safe_request(
                client,
                "post",
                f"{URLSCAN_API}/scan/",
                _PROVIDER,
                "submit scan",
                headers=headers,
                json=body,
            )
            if err:
                return err
            data = resp.json() or {}
        uuid = data.get("uuid", "?")
        lines = [
            f"Scan submitted ({visibility}): {data.get('message', 'Submission successful')}",
            f"Scan UUID: {uuid}",
            f"Scanned URL: {_trunc(data.get('url', url), 140)}",
            f"Report: {data.get('result') or f'{URLSCAN_BASE}/result/{uuid}/'}",
        ]
        if data.get("country"):
            lines.append(f"Scanning from country: {data['country']}")
        lines.append(
            "The scan typically finishes in 10-30 seconds - wait, then call "
            f"get_urlscan_result with scan_id='{uuid}' (retry if it reports "
            "the scan is still in progress)."
        )
        return "\n".join(lines)

    async def get_urlscan_result(scan_id: str) -> str:
        """Retrieve a finished scan: verdicts, page info, technologies, stats."""
        if err := _credentials_error():
            return err
        scan_id = str(scan_id or "").strip()
        if not is_valid_uuid(scan_id):
            return (
                "Error: scan_id must be the scan UUID returned by "
                "submit_urlscan_scan or search_urlscan_scans."
            )
        async with _client(timeout=60) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{URLSCAN_API}/result/{scan_id}/",
                _PROVIDER,
                f"get result {scan_id}",
                headers=headers,
                expected_statuses={404, 410},
            )
            if resp is not None and resp.status_code == 404:
                return (
                    f"Scan {scan_id} is not finished yet (or does not exist). "
                    "Scans typically take 10-30 seconds - wait a few seconds "
                    "and retry."
                )
            if resp is not None and resp.status_code == 410:
                return f"Scan {scan_id} has been deleted from urlscan.io."
            if err:
                return err
            data = resp.json() or {}

        task = data.get("task") or {}
        page = data.get("page") or {}
        stats = data.get("stats") or {}
        lists = data.get("lists") or {}
        requests = (data.get("data") or {}).get("requests") or []

        lines = [
            f"Scan {task.get('uuid', scan_id)} - {str(task.get('time', ''))[:16]} "
            f"[{task.get('visibility', '?')}]",
            f"Submitted URL: {_trunc(task.get('url'), 140)}",
            f"Final page URL: {_trunc(page.get('url'), 140)}",
            f"Page: domain {page.get('domain') or '?'} | IP {page.get('ip') or '?'} "
            f"({page.get('asn') or '?'} {page.get('asnname') or ''}) | "
            f"country {page.get('country') or '?'} | server {page.get('server') or '?'}",
        ]
        lines.extend(_format_verdicts(data.get("verdicts") or {}))
        tech_line = _format_technologies(data.get("meta") or {})
        if tech_line:
            lines.append(tech_line)
        lines.append(
            f"Requests: {len(requests) or stats.get('requests', '?')} total | "
            f"{stats.get('secureRequests', '?')} secure "
            f"({stats.get('securePercentage', '?')}%) | "
            f"{stats.get('malicious', 0)} flagged malicious | "
            f"{stats.get('adBlocked', 0)} ad-blocked"
        )
        domains = lists.get("domains") or []
        ips = lists.get("ips") or []
        if domains or ips:
            shown_domains = ", ".join(str(d) for d in domains[:10])
            extra_d = f" (+{len(domains) - 10} more)" if len(domains) > 10 else ""
            lines.append(
                f"Contacted: {len(ips)} IP(s), {len(domains)} domain(s)"
                + (f" - domains: {shown_domains}{extra_d}" if domains else "")
            )
        hashes = lists.get("hashes") or []
        if hashes:
            lines.append(
                f"Resource hashes (SHA-256): {len(hashes)} - pivot with "
                f"search_urlscan_scans query 'hash:{hashes[0]}'"
            )
        tags = task.get("tags") or []
        if tags:
            lines.append(f"Tags: {', '.join(str(t) for t in tags[:10])}")
        lines.append(
            f"Screenshot: {task.get('screenshotURL') or f'{URLSCAN_BASE}/screenshots/{scan_id}.png'}"
        )
        lines.append(
            f"DOM snapshot: {task.get('domURL') or f'{URLSCAN_BASE}/dom/{scan_id}/'} "
            "(fetch text with get_urlscan_dom)"
        )
        lines.append(
            f"Report: {task.get('reportURL') or f'{URLSCAN_BASE}/result/{scan_id}/'}"
        )
        return "\n".join(lines)

    async def get_urlscan_dom(scan_id: str, max_chars: int = 5000) -> str:
        """Download the recorded DOM snapshot of a finished scan (truncated)."""
        if err := _credentials_error():
            return err
        scan_id = str(scan_id or "").strip()
        if not is_valid_uuid(scan_id):
            return (
                "Error: scan_id must be the scan UUID returned by "
                "submit_urlscan_scan or search_urlscan_scans."
            )
        max_chars = _clamp(max_chars, 200, _DOM_MAX_CHARS, 5000)
        async with _client(timeout=60) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{URLSCAN_BASE}/dom/{scan_id}/",
                _PROVIDER,
                f"get DOM {scan_id}",
                headers=headers,
                expected_statuses={404},
            )
            if resp is not None and resp.status_code == 404:
                return (
                    f"No DOM snapshot is stored for scan {scan_id} (the scan "
                    "may still be running, or the snapshot was not saved)."
                )
            if err:
                return err
            dom = resp.text or ""
        if not dom.strip():
            return f"The DOM snapshot for scan {scan_id} is empty."
        truncated = len(dom) > max_chars
        out = f"DOM snapshot for scan {scan_id} ({len(dom)} chars"
        out += f", showing first {max_chars}):\n" if truncated else "):\n"
        out += dom[:max_chars]
        if truncated:
            out += "\n[...truncated - raise max_chars (cap 20000) for more]"
        return out

    # ══════════════════════════════════════════════════════════════════════
    # 3. STRUCTURE SEARCH (urlscan Pro)
    # ══════════════════════════════════════════════════════════════════════

    async def find_urlscan_similar_scans(
        scan_id: str,
        query: str = "",
        size: int = 20,
        search_after: str = "",
    ) -> str:
        """Find scans structurally similar to a given scan (urlscan Pro)."""
        if err := _credentials_error():
            return err
        scan_id = str(scan_id or "").strip()
        if not is_valid_uuid(scan_id):
            return (
                "Error: scan_id must be the UUID of the reference scan to "
                "compare against."
            )
        params: dict[str, Any] = {"size": _clamp(size, 1, _SEARCH_MAX_SIZE, 20)}
        query = str(query or "").strip()
        if query:
            params["q"] = query
        search_after = str(search_after or "").strip()
        if search_after:
            params["search_after"] = search_after
        async with _client(timeout=60) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{URLSCAN_API}/pro/result/{scan_id}/similar/",
                _PROVIDER,
                f"find similar scans {scan_id}",
                headers=headers,
                params=params,
                expected_statuses={403},
            )
            if resp is not None and resp.status_code == 403:
                return (
                    "Structure search requires a urlscan Pro subscription - "
                    "the configured API key does not have access. Use "
                    "search_urlscan_scans for regular pivoting instead."
                )
            if err:
                return err
            data = resp.json() or {}
        results = data.get("results") or []
        total = data.get("total", len(results))
        out = format_entity_list(
            results,
            _format_search_hit,
            noun="similar scan",
            total=total,
            empty_msg=f"No structurally similar scans found for {scan_id}.",
        )
        if results and data.get("has_more"):
            last_sort = results[-1].get("sort") or []
            cursor = ",".join(str(s) for s in last_sort)
            out += format_pagination_hint(cursor, param_name="search_after")
        return out

    # ══════════════════════════════════════════════════════════════════════
    # 4. QUOTAS (status)
    # ══════════════════════════════════════════════════════════════════════

    async def get_urlscan_quotas() -> str:
        """Validate the connection and report per-action quotas and usage."""
        if err := _credentials_error():
            return err
        async with _client() as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{URLSCAN_BASE}/user/quotas/",
                _PROVIDER,
                "get quotas",
                headers=headers,
            )
            if err:
                return err
            data = resp.json() or {}
        limits = data.get("limits") or {}
        if not limits:
            return "Connected to urlscan.io, but no quota details were returned."
        lines = ["Connected to urlscan.io. Per-action quotas (used/limit):"]
        for action in sorted(limits):
            windows = limits.get(action) or {}
            if not isinstance(windows, dict):
                continue
            parts = []
            for window in ("minute", "hour", "day"):
                w = windows.get(window) or {}
                if isinstance(w, dict) and w.get("limit") is not None:
                    parts.append(f"{window}: {w.get('used', 0)}/{w.get('limit')}")
            if parts:
                lines.append(f"  {action}: {' | '.join(parts)}")
        return "\n".join(lines)

    # ══════════════════════════════════════════════════════════════════════
    # REGISTER ALL TOOLS
    # ══════════════════════════════════════════════════════════════════════

    executor.register("search_urlscan_scans", search_urlscan_scans)
    executor.register("submit_urlscan_scan", submit_urlscan_scan)
    executor.register("get_urlscan_result", get_urlscan_result)
    executor.register("get_urlscan_dom", get_urlscan_dom)
    executor.register("find_urlscan_similar_scans", find_urlscan_similar_scans)
    executor.register("get_urlscan_quotas", get_urlscan_quotas)

    return [
        ToolDefinition(
            name="search_urlscan_scans",
            description=(
                "Search urlscan.io's corpus of historical website scans using "
                "Elasticsearch query-string syntax. Use this FIRST when "
                "investigating a URL/domain/IP - someone may already have "
                "scanned it. Searchable fields include domain:example.com "
                "(any contacted domain), page.domain:example.com (primary "
                'page domain), page.ip:"1.2.3.4", page.asn:AS24940, '
                "page.url:\"login\", hash:SHA256 (any resource hash), "
                "server:nginx, filename:app.js, task.tags:phishing, and "
                "date ranges like date:>now-7d. Combine with AND/OR/NOT and "
                "parentheses, e.g. 'page.domain:paypal.com AND date:>now-30d'. "
                "Returns scan UUIDs (use get_urlscan_result for full detail), "
                "page URL/domain/IP/ASN, and tags. Finds public scans by "
                "anyone plus your own unlisted/private scans."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": (
                            "Elasticsearch query string, e.g. "
                            "'domain:example.com AND date:>now-7d'. Limit by "
                            "date when possible to keep results relevant."
                        ),
                    },
                    "size": {
                        "type": "integer",
                        "default": 20,
                        "description": "Number of results to return (1-100); page deeper with search_after.",
                    },
                    "search_after": {
                        "type": "string",
                        "description": (
                            "Deep-pagination cursor: the comma-separated sort "
                            "value of the last (oldest) result from the "
                            "previous page."
                        ),
                    },
                },
                "required": ["query"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="get_urlscan_result",
            description=(
                "Retrieve the full result of a finished urlscan.io scan by its "
                "UUID: security verdicts (overall score, malicious flag, "
                "targeted brands, engine/community votes), final page info "
                "(domain, IP, ASN, country, server), detected technologies "
                "(Wappalyzer), request/transfer statistics, contacted "
                "IPs/domains, resource hashes for pivoting, plus the "
                "screenshot URL and DOM snapshot URL. If the scan was just "
                "submitted it may still be in progress (scans take ~10-30 "
                "seconds) - the tool tells you to retry in that case."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "scan_id": {
                        "type": "string",
                        "description": (
                            "Scan UUID from submit_urlscan_scan or "
                            "search_urlscan_scans."
                        ),
                    },
                },
                "required": ["scan_id"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="get_urlscan_dom",
            description=(
                "Download the recorded DOM snapshot (rendered HTML) of a "
                "finished urlscan.io scan for content inspection - e.g. to "
                "look for phishing kit markers, credential forms and their "
                "action URLs, obfuscated JavaScript, or brand assets. Returns "
                "the first max_chars characters (default 5000, cap 20000). "
                "Use get_urlscan_result first for the structured summary."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "scan_id": {
                        "type": "string",
                        "description": "Scan UUID of a finished scan.",
                    },
                    "max_chars": {
                        "type": "integer",
                        "default": 5000,
                        "description": "Max characters of DOM text to return (200-20000).",
                    },
                },
                "required": ["scan_id"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="find_urlscan_similar_scans",
            description=(
                "urlscan Pro structure search: find scans that are "
                "STRUCTURALLY similar to a reference scan - pages built from "
                "the same template/kit even when hosted on different domains. "
                "Use to expand from one confirmed phishing page to the whole "
                "campaign. Optionally refine with an additional Elasticsearch "
                "query filter (same syntax as search_urlscan_scans). Requires "
                "a urlscan Pro subscription; without it the tool reports the "
                "missing entitlement."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "scan_id": {
                        "type": "string",
                        "description": "UUID of the reference scan to compare against.",
                    },
                    "query": {
                        "type": "string",
                        "description": (
                            "Optional additional filter (Elasticsearch query "
                            "string), e.g. 'date:>now-30d'."
                        ),
                    },
                    "size": {
                        "type": "integer",
                        "default": 20,
                        "description": "Number of results to return (1-100); page deeper with search_after.",
                    },
                    "search_after": {
                        "type": "string",
                        "description": (
                            "Deep-pagination cursor from the previous page "
                            "(comma-separated sort value of the last result)."
                        ),
                    },
                },
                "required": ["scan_id"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="submit_urlscan_scan",
            description=(
                "Submit a URL to urlscan.io for sandboxed scanning (WRITE - "
                "consumes scan quota; the sandbox will visit the URL). "
                "Visibility controls who can see the scan: 'public' appears "
                "on the urlscan.io front page and public search - NEVER use "
                "it for URLs that may contain PII, tokens, or non-public "
                "info; 'unlisted' (the safe default) is only visible to "
                "vetted security researchers; 'private' is visible only to "
                "your account. Consider search_urlscan_scans first - the URL "
                "may already have been scanned. Returns the scan UUID; the "
                "scan takes ~10-30 seconds, then fetch it with "
                "get_urlscan_result. Submissions can be rejected (blacklisted "
                "domain, non-resolvable hostname, non-http scheme)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "The http/https URL to scan.",
                    },
                    "visibility": {
                        "type": "string",
                        "enum": ["public", "unlisted", "private"],
                        "default": "unlisted",
                        "description": (
                            "Scan visibility. Keep 'unlisted' (default) or "
                            "'private' unless the user explicitly asks for a "
                            "public scan."
                        ),
                    },
                    "tags": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Up to 10 annotation tags, e.g. ['phishing', "
                            "'case-1234']."
                        ),
                    },
                    "country": {
                        "type": "string",
                        "description": (
                            "2-letter ISO-3166-1 alpha-2 country to scan from "
                            "(e.g. 'de', 'us'). Omit for automatic selection."
                        ),
                    },
                    "custom_user_agent": {
                        "type": "string",
                        "description": "Override the browser User-Agent for this scan.",
                    },
                    "referer": {
                        "type": "string",
                        "description": "Override the HTTP Referer header for this scan.",
                    },
                },
                "required": ["url"],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="get_urlscan_quotas",
            description=(
                "Validate the urlscan.io connection and report the per-action "
                "quotas and current usage (searches, public/unlisted/private "
                "scans, etc. - per minute/hour/day). Use first if other "
                "urlscan tools fail or return rate-limit errors."
            ),
            input_schema={"type": "object", "properties": {}, "required": []},
            source=_SOURCE,
            priority=2,
        ),
    ]
