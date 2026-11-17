"""CrowdSec CTI API tools - crowd-sourced IP reputation (smoke + fire).

Covers the whole CTI API v2 surface (https://crowdsecurity.github.io/cti-api/):

* ``lookup_crowdsec_ip``      - GET /smoke/{ip} (freemium; 404 = IP unknown to
                                the CrowdSec network, surfaced as a clean answer)
* ``search_crowdsec_ips``     - GET /smoke/search (freemium; Lucene query +
                                since window, HAL ``_links.next`` pagination)
* ``lookup_crowdsec_ips``     - GET /smoke?ips=... (premium batch lookup)
* ``get_crowdsec_fire_ips``   - GET /fire (premium; the community blocklist
                                with state + expiration)

Plus the Service API key smoke test:

* ``get_crowdsec_service_info`` - GET {SERVICE_API}/info (org id, subscription
                                  type, key name - the org id is needed to
                                  target org-wide console decisions)
"""

from __future__ import annotations

from typing import Any

from app.integrations.http import format_entity_list, safe_request
from app.tools.base import CallableToolExecutor, ToolDefinition

from ._common import (
    _PROVIDER,
    _SOURCE,
    CTI_API,
    MISSING_CTI_KEY_MSG,
    MISSING_SERVICE_KEY_MSG,
    SERVICE_API,
    clamp,
    client,
    format_cti_ip,
    format_cti_ip_brief,
    is_valid_ip,
    next_page_hint,
    validate_cti_duration,
)

# The CTI batch endpoint accepts comma-separated IPs; keep the batch bounded so
# a single call can never produce an unbounded URL / response.
_MAX_BATCH_IPS = 100

_PREMIUM_HINT = (
    " This endpoint requires a premium CrowdSec CTI plan (community keys get "
    "403) - see crowdsec.net/pricing."
)


def register(
    cti_headers: dict[str, str] | None,
    service_headers: dict[str, str] | None,
    executor: CallableToolExecutor,
) -> list[ToolDefinition]:
    """Register the CTI reputation tools + the Service API info probe.

    ``cti_headers`` / ``service_headers`` are ``None`` when the matching key is
    not configured - the tools still register and return an actionable error.
    """

    async def lookup_crowdsec_ip(ip: str) -> str:
        """Full CTI report for one IP (reputation, behaviors, MITRE, CVEs, scores)."""
        if cti_headers is None:
            return MISSING_CTI_KEY_MSG
        ip = str(ip or "").strip()
        if not is_valid_ip(ip):
            return f"Error: '{ip}' is not a valid IP address (e.g. 8.8.8.8)."
        async with client() as http:
            resp, err = await safe_request(
                http,
                "get",
                f"{CTI_API}/smoke/{ip}",
                _PROVIDER,
                f"lookup IP {ip}",
                headers=cti_headers,
                expected_statuses={404},
            )
            if resp is not None and resp.status_code == 404:
                return (
                    f"{ip} is unknown to the CrowdSec network - it has never "
                    "been reported by the community. This is not an error; it "
                    "simply means no CrowdSec deployment has flagged this IP."
                )
            if err:
                return err
            data = resp.json() or {}
        return format_cti_ip(data)

    async def search_crowdsec_ips(
        query: str, since: str = "7d", page: int = 1, limit: int = 10
    ) -> str:
        """Hunt the CrowdSec smoke database with a Lucene query."""
        if cti_headers is None:
            return MISSING_CTI_KEY_MSG
        query = str(query or "").strip()
        if not query:
            return (
                "Error: a Lucene query is required (e.g. "
                "'behaviors.label:\"SSH Bruteforce\" AND location.country:US')."
            )
        since = str(since or "7d").strip()
        duration_err = validate_cti_duration(since)
        if duration_err:
            return duration_err
        page = clamp(page, default=1, hi=10_000)
        params: dict[str, Any] = {
            "query": query,
            "since": since,
            "page": page,
            # Community keys are capped at 10 IPs per page - requesting more
            # with a community key returns an error, so default conservatively.
            "limit": clamp(limit, default=10, hi=100),
        }
        async with client(timeout=60) as http:
            resp, err = await safe_request(
                http,
                "get",
                f"{CTI_API}/smoke/search",
                _PROVIDER,
                "search smoke database",
                headers=cti_headers,
                params=params,
            )
            if err:
                return err
            data = resp.json() or {}
        items = data.get("items") or []
        out = format_entity_list(
            items,
            format_cti_ip_brief,
            noun="matching IP",
            empty_msg=(
                f"No IPs matched the query '{query}' within the last {since}."
            ),
        )
        if items:
            out += (
                "\nUse lookup_crowdsec_ip on any of these IPs for the full "
                "report."
            )
        return out + next_page_hint(data, page)

    async def lookup_crowdsec_ips(ips: list[str]) -> str:
        """Batch CTI lookup for up to 100 IPs in one call (premium)."""
        if cti_headers is None:
            return MISSING_CTI_KEY_MSG
        candidates = [str(i).strip() for i in (ips or []) if str(i or "").strip()]
        cleaned = [i for i in candidates if is_valid_ip(i)]
        invalid = [i for i in candidates if not is_valid_ip(i)]
        if not cleaned:
            return "Error: provide at least one valid IP address in 'ips'."
        if len(cleaned) > _MAX_BATCH_IPS:
            return (
                f"Error: too many IPs ({len(cleaned)}). Submit at most "
                f"{_MAX_BATCH_IPS} per call."
            )
        async with client(timeout=60) as http:
            resp, err = await safe_request(
                http,
                "get",
                f"{CTI_API}/smoke",
                _PROVIDER,
                f"batch lookup {len(cleaned)} IPs",
                headers=cti_headers,
                params={"ips": ",".join(cleaned)},
                expected_statuses={403},
            )
            if resp is not None and resp.status_code == 403:
                return (
                    "Error: CrowdSec rejected the batch lookup (403)."
                    + _PREMIUM_HINT
                    + " Fall back to lookup_crowdsec_ip for single IPs."
                )
            if err:
                return err
            data = resp.json() or {}
        items = data.get("items") or []
        out = format_entity_list(
            items,
            format_cti_ip_brief,
            noun="IP record",
            total=data.get("total"),
            empty_msg="None of the submitted IPs are known to the CrowdSec network.",
        )
        not_found = data.get("not_found")
        if isinstance(not_found, (int, float)) and not_found:
            out += f"\n{int(not_found)} IP(s) were unknown to the CrowdSec network."
        if invalid:
            shown = ", ".join(invalid[:20])
            extra = f" (+{len(invalid) - 20} more)" if len(invalid) > 20 else ""
            out += f"\nSkipped {len(invalid)} invalid IP(s): {shown}{extra}"
        return out

    async def get_crowdsec_fire_ips(
        since: str = "", page: int = 1, limit: int = 50
    ) -> str:
        """Pull the CrowdSec community blocklist (fire) - premium."""
        if cti_headers is None:
            return MISSING_CTI_KEY_MSG
        page = clamp(page, default=1, hi=10_000)
        params: dict[str, Any] = {
            "page": page,
            "limit": clamp(limit, default=50, hi=100),
        }
        since = str(since or "").strip()
        if since:
            duration_err = validate_cti_duration(since)
            if duration_err:
                return duration_err
            params["since"] = since
        async with client(timeout=60) as http:
            resp, err = await safe_request(
                http,
                "get",
                f"{CTI_API}/fire",
                _PROVIDER,
                "get community blocklist",
                headers=cti_headers,
                params=params,
                expected_statuses={403},
            )
            if resp is not None and resp.status_code == 403:
                return (
                    "Error: CrowdSec rejected the community blocklist pull "
                    "(403)." + _PREMIUM_HINT
                )
            if err:
                return err
            data = resp.json() or {}
        items = data.get("items") or []
        out = format_entity_list(
            items,
            format_cti_ip_brief,
            noun="community blocklist IP",
            empty_msg=(
                "No community blocklist entries"
                + (f" updated in the last {since}" if since else "")
                + "."
            ),
        )
        if items:
            out += (
                "\nStates: validated = currently in the community blocklist; "
                "refused = manually purged (false positive)."
            )
        return out + next_page_hint(data, page)

    async def get_crowdsec_service_info() -> str:
        """Verify the Service API key: organization id, subscription, key name."""
        if service_headers is None:
            return MISSING_SERVICE_KEY_MSG
        async with client() as http:
            resp, err = await safe_request(
                http,
                "get",
                f"{SERVICE_API}/info",
                _PROVIDER,
                "get Service API info",
                headers=service_headers,
            )
            if err:
                return err
            data = resp.json() or {}
        return (
            "Connected to the CrowdSec Service API.\n"
            f"  Organization id: {data.get('organization_id') or '?'}\n"
            f"  Subscription: {data.get('subscription_type') or '?'}\n"
            f"  API key name: {data.get('api_key_name') or '?'}"
        )

    executor.register("lookup_crowdsec_ip", lookup_crowdsec_ip)
    executor.register("search_crowdsec_ips", search_crowdsec_ips)
    executor.register("lookup_crowdsec_ips", lookup_crowdsec_ips)
    executor.register("get_crowdsec_fire_ips", get_crowdsec_fire_ips)
    executor.register("get_crowdsec_service_info", get_crowdsec_service_info)

    return [
        ToolDefinition(
            name="lookup_crowdsec_ip",
            description=(
                "Look up one IP in the CrowdSec CTI (crowd-sourced reputation "
                "from 100k+ real deployments). Returns the full report: "
                "reputation (malicious/suspicious/unknown/known/safe/benign) "
                "with confidence, AS/geo/rDNS context, /16 and /24 range "
                "reputation, background-noise level, first/last seen, threat "
                "scores (0-5 per time window), reported behaviors and attack "
                "scenarios, MITRE techniques, linked CVEs, classifications "
                "(incl. false-positive tags), and top targeted countries. Use "
                "this first when triaging an alert about an external IP. A "
                "'unknown to the CrowdSec network' answer means the community "
                "has never reported the IP - not an error."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "ip": {
                        "type": "string",
                        "description": "IP address (IPv4 or IPv6) to look up",
                    },
                },
                "required": ["ip"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="search_crowdsec_ips",
            description=(
                "Hunt the CrowdSec smoke database with a Lucene query over the "
                "CTI taxonomy. Useful fields: behaviors.label (e.g. \"SSH "
                "Bruteforce\", \"HTTP Scan\"), attack_details.name (scenario, "
                "e.g. crowdsecurity/ssh-slow-bf), classifications.classifications"
                ".name, cves (e.g. CVE-2024-3400), reputation, confidence, "
                "background_noise, as_name, as_num, location.country, "
                "ip_range, reverse_dns. Combine with AND/OR/NOT. Examples: "
                "'behaviors.label:\"SSH Bruteforce\" AND location.country:US', "
                "'cves:CVE-2024-3400', 'as_num:14061 AND reputation:malicious'. "
                "'since' bounds the search to recently-updated IPs (duration "
                "like 24h/7d). Community API keys are limited to 10 IPs per "
                "page. Returns one line per IP - follow up with "
                "lookup_crowdsec_ip for the full report."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Lucene query over the CTI fields",
                    },
                    "since": {
                        "type": "string",
                        "default": "7d",
                        "description": (
                            "Only IPs updated within this window - duration in "
                            "m (minutes), h (hours) or d (days), e.g. 24h, 7d"
                        ),
                    },
                    "page": {
                        "type": "integer",
                        "default": 1,
                        "description": "Page to fetch (1-based)",
                    },
                    "limit": {
                        "type": "integer",
                        "default": 10,
                        "description": (
                            "Results per page (max 10 on community keys, up to "
                            "100 on premium plans)"
                        ),
                    },
                },
                "required": ["query"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="lookup_crowdsec_ips",
            description=(
                "Batch CTI lookup: enrich up to 100 IPs in a single CrowdSec "
                "call, one summary line per IP (reputation, confidence, AS, "
                "country, noise, behaviors). Use for alert-triage sweeps and "
                "log analysis instead of calling lookup_crowdsec_ip "
                "repeatedly. Requires a premium CTI plan - community keys get "
                "a 403 (then fall back to lookup_crowdsec_ip per IP)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "ips": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "IP addresses (IPv4 or IPv6) to enrich (max 100)",
                    },
                },
                "required": ["ips"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="get_crowdsec_fire_ips",
            description=(
                "Pull the CrowdSec community blocklist (fire) - the curated "
                "list of validated malevolent IPs the community currently "
                "blocks. Each entry carries the full CTI context plus a state "
                "(validated = active, refused = purged false positive) and an "
                "expiration date. Filter with 'since' (e.g. 3d) to fetch only "
                "recently-updated entries for incremental syncs. Requires a "
                "premium CTI plan - community keys get a 403."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "since": {
                        "type": "string",
                        "description": (
                            "Only entries updated within this window - duration "
                            "in m/h/d, e.g. 3d (omit for the full list)"
                        ),
                    },
                    "page": {
                        "type": "integer",
                        "default": 1,
                        "description": "Page to fetch (1-based)",
                    },
                    "limit": {
                        "type": "integer",
                        "default": 50,
                        "description": "Results per page (max 100)",
                    },
                },
                "required": [],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="get_crowdsec_service_info",
            description=(
                "Verify the CrowdSec Service API key and return the console "
                "context: organization id, subscription type, and the API key "
                "name. Run this first if console tools (decisions, blocklists, "
                "allowlists, exploit tracker) fail - and to get the "
                "organization id needed for org-wide decisions."
            ),
            input_schema={"type": "object", "properties": {}, "required": []},
            source=_SOURCE,
            priority=0,
        ),
    ]
