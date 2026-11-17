# SPDX-License-Identifier: Apache-2.0

"""AbuseIPDB integration - crowdsourced IP reputation and abuse reporting.

Full AbuseIPDB APIv2 coverage, designed as the IP-reputation building block for
an agentic SOC/CTI team:

  - Check (*enrichment*): abuse confidence score, geolocation, ISP, usage type,
    Tor flag, and report statistics for a single IPv4/IPv6 address, with
    optional recent report details (``verbose``)
  - Reports (*investigation*): page through the individual abuse reports filed
    against an IP (reporter country, categories, comments)
  - Check-block (*network assessment*): assess an entire CIDR subnet and its
    reported member addresses before blocking a range
  - Blacklist (*blocklist generation / hunting*): the crowdsourced list of the
    most-reported IPs, filtered by confidence, country, and IP version
  - Report + bulk-report (*creating intelligence*): report abusive IPs back to
    the community, one at a time or as a CSV batch
  - Clear-address (*cleanup*, destructive): delete all reports the connected
    account previously submitted for an IP (e.g. a false positive)
  - Categories: a deterministic local reference of the 23 official abuse
    categories used by both check output and report input

Authentication
--------------
AbuseIPDB authenticates with a single **API key** sent in the ``Key`` HTTP
header (the header form is vendor-recommended over the ``?key=`` query
parameter, which leaks into server logs). The key is stored in ``api_key_enc``
and surfaces here as the plain token - no compound assembly needed.

Rate limits are per-endpoint DAILY quotas by account tier (e.g. standard tier:
check 1,000/day, blacklist 5/day, clear-address 5/day). HTTP 429 responses
carry ``Retry-After`` and are retried by the shared ``safe_request`` helper.
"""

from __future__ import annotations

import ipaddress
from typing import Any

import httpx

from app.core.egress import egress_client
from app.integrations.http import (
    format_entity_list,
    safe_request,
)
from app.tools.base import CallableToolExecutor, ToolDefinition

_PROVIDER = "AbuseIPDB"
_SOURCE = "integration:abuseipdb"

# All APIv2 endpoints live under this single host (no self-hosted variant).
ABUSEIPDB_API = "https://api.abuseipdb.com/api/v2"

_DEFAULT_TIMEOUT = 30
_MAX_AGE_MIN, _MAX_AGE_MAX = 1, 365
_REPORTS_MAX_PER_PAGE = 100

# The official report categories (https://www.abuseipdb.com/categories).
# Used to render category ids in check/reports output and to validate the
# ``categories`` input of the report tools.
ABUSEIPDB_CATEGORIES: dict[int, tuple[str, str]] = {
    1: ("DNS Compromise", "Altering DNS records resulting in improper redirection."),
    2: ("DNS Poisoning", "Falsifying domain server cache (cache poisoning)."),
    3: ("Fraud Orders", "Fraudulent orders."),
    4: ("DDoS Attack", "Participating in distributed denial-of-service (usually part of botnet)."),
    5: ("FTP Brute-Force", "FTP credential brute-force attacks."),
    6: ("Ping of Death", "Oversized IP packet."),
    7: ("Phishing", "Phishing websites and/or email."),
    8: ("Fraud VoIP", "Fraudulent VoIP activity."),
    9: ("Open Proxy", "Open proxy, open relay, or Tor exit node."),
    10: ("Web Spam", "Comment/forum spam, HTTP referer spam, or other CMS spam."),
    11: ("Email Spam", "Spam email content, infected attachments, and phishing emails."),
    12: ("Blog Spam", "CMS blog comment spam."),
    13: ("VPN IP", "Conjunctive category for VPN endpoints."),
    14: ("Port Scan", "Scanning for open ports and vulnerable services."),
    15: ("Hacking", "General hacking attempts."),
    16: ("SQL Injection", "Attempts at SQL injection."),
    17: ("Spoofing", "Email sender spoofing."),
    18: ("Brute-Force", "Credential brute-force on logins and services (SSH, FTP, SIP, SMTP, RDP, ...)."),
    19: ("Bad Web Bot", "Scraping and crawlers ignoring robots.txt; excessive requests / UA spoofing."),
    20: ("Exploited Host", "Host likely infected with malware and used for other attacks."),
    21: ("Web App Attack", "Probing/exploiting web apps (CMS, e-commerce, phpMyAdmin, plugins...)."),
    22: ("SSH", "Secure Shell (SSH) abuse; combine with more specific categories."),
    23: ("IoT Targeted", "Abuse targeted at an Internet of Things device."),
}


def _category_names(ids: Any) -> str:
    """Render a list of category ids as ``Name (id)`` pairs."""
    if not isinstance(ids, list):
        return ""
    parts = []
    for raw in ids:
        try:
            cid = int(raw)
        except (TypeError, ValueError):
            parts.append(str(raw))
            continue
        name = ABUSEIPDB_CATEGORIES.get(cid, ("?",))[0]
        parts.append(f"{name} ({cid})")
    return ", ".join(parts)


def _score_icon(score: Any) -> str:
    """Confidence-of-abuse band icon (0-100)."""
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


def _is_valid_ip(value: str) -> bool:
    """True if *value* parses as a single IPv4 or IPv6 address.

    Validating locally fails fast with a clear message (instead of a
    harder-to-read upstream error) and never burns daily API quota on a
    malformed input.
    """
    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        return False


def _parse_cidr(value: str) -> str | None:
    """Parse a CIDR string -> normalized network, or None if invalid.

    ``strict=False`` tolerates host bits being set (203.0.113.5/24 is
    normalized to 203.0.113.0/24) - agent-supplied networks are often
    derived from a member address.
    """
    try:
        return str(ipaddress.ip_network(value, strict=False))
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Auth helper (shared with integration_testing.py)
# ---------------------------------------------------------------------------


def build_abuseipdb_headers(api_key: str) -> dict[str, str]:
    """Build AbuseIPDB request headers (``Key`` auth + JSON Accept).

    The key is stripped so a value pasted with trailing/leading whitespace can
    never produce a malformed header. ``Accept: application/json`` is required
    - without it some AbuseIPDB error paths return a full HTML page. This is
    the single chokepoint shared by the tool builder and the connectivity test.
    """
    return {
        "Key": (api_key or "").strip(),
        "Accept": "application/json",
    }


# ---------------------------------------------------------------------------
# Tool builder
# ---------------------------------------------------------------------------


def build_tools(
    token: str,
    executor: CallableToolExecutor,
    **_kwargs: Any,
) -> list[ToolDefinition]:
    """Build AbuseIPDB tools from the plain API-key token.

    The tools always build (the catalog introspects providers with a dummy
    token); a missing/blank key is surfaced as a call-time error instead.
    """
    api_key = (token or "").strip()
    headers = build_abuseipdb_headers(api_key)

    def _client(timeout: int = _DEFAULT_TIMEOUT) -> httpx.AsyncClient:
        return egress_client(timeout=timeout)

    def _credentials_error() -> str | None:
        if not api_key:
            return (
                "Error: AbuseIPDB credentials are not configured - set the API "
                "key on the integration and test the connection."
            )
        return None

    # ══════════════════════════════════════════════════════════════════════
    # 1. CHECK / REPORTS / CHECK-BLOCK (enrichment + investigation)
    # ══════════════════════════════════════════════════════════════════════

    async def check_abuseipdb_ip(
        ip_address: str,
        max_age_in_days: int = 30,
        verbose: bool = False,
    ) -> str:
        """Check a single IP's abuse reputation (score, geo, ISP, report stats)."""
        if err := _credentials_error():
            return err
        ip_address = (ip_address or "").strip()
        if not ip_address:
            return "Error: ip_address is required (a single IPv4 or IPv6 address)."
        if not _is_valid_ip(ip_address):
            return (
                f"Error: '{ip_address}' is not a valid IPv4 or IPv6 address. "
                "Use check_abuseipdb_network for CIDR blocks."
            )
        params: dict[str, Any] = {
            "ipAddress": ip_address,
            "maxAgeInDays": _clamp(max_age_in_days, _MAX_AGE_MIN, _MAX_AGE_MAX, 30),
        }
        if verbose:
            # The API enables verbose on parameter *presence*, but send an
            # explicit truthy value so frameworks that drop empty query
            # values can never silently disable it.
            params["verbose"] = "true"
        async with _client() as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{ABUSEIPDB_API}/check",
                _PROVIDER,
                f"check IP {ip_address}",
                headers=headers,
                params=params,
            )
            if err:
                return err
            data = (resp.json() or {}).get("data", {})

        score = data.get("abuseConfidenceScore", 0)
        lines = [
            f"{_score_icon(score)} {data.get('ipAddress', ip_address)} - "
            f"abuse confidence score: {score}/100",
            f"Country: {data.get('countryCode') or 'N/A'}"
            + (f" ({data.get('countryName')})" if data.get("countryName") else "")
            + f" | ISP: {data.get('isp') or 'N/A'} | Domain: {data.get('domain') or 'N/A'}",
            f"Usage type: {data.get('usageType') or 'N/A'} | "
            f"IP version: {data.get('ipVersion', '?')} | "
            f"Public: {data.get('isPublic', '?')} | Tor exit node: {data.get('isTor', '?')} | "
            f"Whitelisted: {data.get('isWhitelisted')}",
            f"Reports (last {params['maxAgeInDays']} days): {data.get('totalReports', 0)} "
            f"from {data.get('numDistinctUsers', 0)} distinct reporters | "
            f"Last reported: {data.get('lastReportedAt') or 'never'}",
        ]
        hostnames = data.get("hostnames") or []
        if hostnames:
            lines.append(f"Hostnames: {', '.join(str(h) for h in hostnames[:5])}")
        reports = data.get("reports") or []
        if reports:
            lines.append(f"\nMost recent reports ({min(len(reports), 10)} of {len(reports)}):")
            for r in reports[:10]:
                lines.append(
                    f"  {str(r.get('reportedAt', ''))[:16]} "
                    f"[{r.get('reporterCountryCode', '?')}] "
                    f"{_category_names(r.get('categories'))} - "
                    f"{_trunc(r.get('comment'), 140) or 'no comment'}"
                )
        elif verbose:
            lines.append("No individual reports in the lookback window.")
        else:
            lines.append(
                "(Pass verbose=true to include recent report details, or use "
                "list_abuseipdb_ip_reports to page through all reports.)"
            )
        return "\n".join(lines)

    async def list_abuseipdb_ip_reports(
        ip_address: str,
        max_age_in_days: int = 30,
        page: int = 1,
        per_page: int = 25,
    ) -> str:
        """Page through the individual abuse reports filed against an IP."""
        if err := _credentials_error():
            return err
        ip_address = (ip_address or "").strip()
        if not ip_address:
            return "Error: ip_address is required (a single IPv4 or IPv6 address)."
        if not _is_valid_ip(ip_address):
            return f"Error: '{ip_address}' is not a valid IPv4 or IPv6 address."
        params = {
            "ipAddress": ip_address,
            "maxAgeInDays": _clamp(max_age_in_days, _MAX_AGE_MIN, _MAX_AGE_MAX, 30),
            "page": max(1, _clamp(page, 1, 10_000, 1)),
            "perPage": _clamp(per_page, 1, _REPORTS_MAX_PER_PAGE, 25),
        }
        async with _client() as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{ABUSEIPDB_API}/reports",
                _PROVIDER,
                f"list reports for {ip_address}",
                headers=headers,
                params=params,
            )
            if err:
                return err
            data = (resp.json() or {}).get("data", {})

        results = data.get("results") or []
        total = data.get("total", len(results))

        def _fmt(r: dict) -> str:
            return (
                f"  {str(r.get('reportedAt', ''))[:16]} "
                f"[{r.get('reporterCountryCode', '?')} reporter {r.get('reporterId', '?')}] "
                f"{_category_names(r.get('categories'))}\n"
                f"    {_trunc(r.get('comment'), 160) or 'no comment'}"
            )

        out = format_entity_list(
            results,
            _fmt,
            noun="report",
            total=total,
            empty_msg=f"No abuse reports for {ip_address} in the last "
            f"{params['maxAgeInDays']} days.",
        )
        last_page = data.get("lastPage")
        if last_page and data.get("page", 1) < last_page:
            out += (
                f"\n[Page {data.get('page')} of {last_page} - increase page to "
                "see more reports]"
            )
        return out

    async def check_abuseipdb_network(
        network: str,
        max_age_in_days: int = 30,
        limit: int = 25,
    ) -> str:
        """Assess a CIDR subnet: address space info + reported member IPs."""
        if err := _credentials_error():
            return err
        network = (network or "").strip()
        if not network or "/" not in network:
            return (
                "Error: network is required in CIDR notation (e.g. 203.0.113.0/24). "
                "Subnet size is capped by plan tier (standard up to /24)."
            )
        parsed = _parse_cidr(network)
        if parsed is None:
            return (
                f"Error: '{network}' is not valid CIDR notation (e.g. "
                "203.0.113.0/24 or 2001:db8::/32)."
            )
        network = parsed
        params = {
            "network": network,
            "maxAgeInDays": _clamp(max_age_in_days, _MAX_AGE_MIN, _MAX_AGE_MAX, 30),
        }
        async with _client(timeout=60) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{ABUSEIPDB_API}/check-block",
                _PROVIDER,
                f"check network {network}",
                headers=headers,
                params=params,
            )
            if err:
                return err
            data = (resp.json() or {}).get("data", {})

        reported = data.get("reportedAddress") or []
        # _clamp coerces so an explicit null / non-numeric score from the API
        # can never make the sort raise TypeError.
        reported.sort(
            key=lambda a: (
                _clamp(a.get("abuseConfidenceScore"), 0, 100, 0),
                _clamp(a.get("numReports"), 0, 10**9, 0),
            ),
            reverse=True,
        )
        shown = reported[: max(1, _clamp(limit, 1, 500, 25))]
        lines = [
            f"Network {network}: {data.get('networkAddress', '?')} "
            f"(netmask {data.get('netmask', '?')})",
            f"Address range: {data.get('minAddress', '?')} - {data.get('maxAddress', '?')} | "
            f"Possible hosts: {data.get('numPossibleHosts', '?')} | "
            f"Address space: {data.get('addressSpaceDesc') or 'N/A'}",
        ]
        if not reported:
            lines.append(
                f"No reported addresses in this block in the last "
                f"{params['maxAgeInDays']} days."
            )
        else:
            lines.append(
                f"Reported addresses ({len(shown)} of {len(reported)}, worst first):"
            )
            for a in shown:
                lines.append(
                    f"  {_score_icon(a.get('abuseConfidenceScore'))} "
                    f"{a.get('ipAddress', '?')} - score {a.get('abuseConfidenceScore', 0)}/100, "
                    f"{a.get('numReports', 0)} reports, last "
                    f"{str(a.get('mostRecentReport', ''))[:16] or 'N/A'} "
                    f"[{a.get('countryCode') or '?'}]"
                )
            if len(reported) > len(shown):
                lines.append(
                    f"[{len(reported) - len(shown)} more reported addresses - raise "
                    "limit or use check_abuseipdb_ip for details]"
                )
        return "\n".join(lines)

    # ══════════════════════════════════════════════════════════════════════
    # 2. BLACKLIST (blocklist generation / hunting)
    # ══════════════════════════════════════════════════════════════════════

    async def get_abuseipdb_blacklist(
        confidence_minimum: int = 90,
        limit: int = 100,
        only_countries: str = "",
        except_countries: str = "",
        ip_version: int = 0,
    ) -> str:
        """Get the blacklist of the most-reported IPs (highest confidence first)."""
        if err := _credentials_error():
            return err
        only_countries = (only_countries or "").strip()
        except_countries = (except_countries or "").strip()
        params: dict[str, Any] = {
            "confidenceMinimum": _clamp(confidence_minimum, 25, 100, 90),
            # The API caps the limit by subscription tier and truncates
            # gracefully; keep the agent-facing default small for readability.
            "limit": max(1, _clamp(limit, 1, 10_000, 100)),
        }
        if only_countries:
            params["onlyCountries"] = ",".join(
                c.strip().upper() for c in only_countries.split(",") if c.strip()
            )
        if except_countries:
            params["exceptCountries"] = ",".join(
                c.strip().upper() for c in except_countries.split(",") if c.strip()
            )
        # Coerce so a string "4"/"6" from the LLM is not silently dropped.
        try:
            ip_version = int(ip_version)
        except (TypeError, ValueError):
            ip_version = 0
        if ip_version in (4, 6):
            params["ipVersion"] = ip_version
        async with _client(timeout=60) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{ABUSEIPDB_API}/blacklist",
                _PROVIDER,
                "get blacklist",
                headers=headers,
                params=params,
            )
            if err:
                return err
            body = resp.json() or {}

        items = body.get("data") or []
        generated_at = (body.get("meta") or {}).get("generatedAt", "")

        def _fmt(e: dict) -> str:
            country = f" [{e.get('countryCode')}]" if e.get("countryCode") else ""
            return (
                f"  {e.get('ipAddress', '?')} - score "
                f"{e.get('abuseConfidenceScore', '?')}/100{country}, last reported "
                f"{str(e.get('lastReportedAt', ''))[:16] or 'N/A'}"
            )

        out = format_entity_list(
            items,
            _fmt,
            noun="blacklisted IP",
            empty_msg="The blacklist returned no entries for these filters.",
        )
        if generated_at:
            out += f"\n[List generated at {generated_at}]"
        return out

    # ══════════════════════════════════════════════════════════════════════
    # 3. CATEGORIES (local reference - no API call, no quota)
    # ══════════════════════════════════════════════════════════════════════

    async def list_abuseipdb_categories() -> str:
        """List the official abuse report categories (local reference)."""
        lines = ["AbuseIPDB report categories (use the numeric ids when reporting):"]
        for cid, (name, desc) in ABUSEIPDB_CATEGORIES.items():
            lines.append(f"  {cid}: {name} - {desc}")
        return "\n".join(lines)

    # ══════════════════════════════════════════════════════════════════════
    # 4. REPORT / BULK-REPORT (write) + CLEAR-ADDRESS (destructive)
    # ══════════════════════════════════════════════════════════════════════

    def _parse_categories(raw: str) -> tuple[str | None, str | None]:
        """Validate a comma-separated category id list -> (canonical, error)."""
        ids: list[int] = []
        for part in str(raw or "").split(","):
            part = part.strip()
            if not part:
                continue
            try:
                cid = int(part)
            except ValueError:
                return None, (
                    f"Error: '{part}' is not a numeric category id. Use "
                    "list_abuseipdb_categories to see the valid ids (1-23)."
                )
            if cid not in ABUSEIPDB_CATEGORIES:
                return None, (
                    f"Error: {cid} is not a valid AbuseIPDB category id. Use "
                    "list_abuseipdb_categories to see the valid ids (1-23)."
                )
            ids.append(cid)
        if not ids:
            return None, (
                "Error: at least one category id is required (e.g. '18,22' for "
                "Brute-Force + SSH). Use list_abuseipdb_categories for the list."
            )
        return ",".join(str(i) for i in ids), None

    async def report_abuseipdb_ip(
        ip: str,
        categories: str,
        comment: str = "",
        timestamp: str = "",
    ) -> str:
        """Report an abusive IP to AbuseIPDB (public write; 15-min per-IP cap)."""
        if err := _credentials_error():
            return err
        ip = (ip or "").strip()
        if not ip:
            return "Error: ip is required (a single IPv4 or IPv6 address)."
        if not _is_valid_ip(ip):
            return f"Error: '{ip}' is not a valid IPv4 or IPv6 address."
        cats, cat_err = _parse_categories(categories)
        if cat_err:
            return cat_err
        comment = (comment or "").strip()
        timestamp = (timestamp or "").strip()
        data: dict[str, Any] = {"ip": ip, "categories": cats}
        if comment:
            data["comment"] = comment
        if timestamp:
            data["timestamp"] = timestamp
        async with _client() as client:
            resp, err = await safe_request(
                client,
                "post",
                f"{ABUSEIPDB_API}/report",
                _PROVIDER,
                f"report IP {ip}",
                headers=headers,
                data=data,
            )
            if err:
                return err
            result = (resp.json() or {}).get("data", {})
        return (
            f"Reported {result.get('ipAddress', ip)} with categories "
            f"{_category_names([int(c) for c in cats.split(',')])}. Updated abuse "
            f"confidence score: {result.get('abuseConfidenceScore', '?')}/100."
        )

    async def bulk_report_abuseipdb_ips(csv_content: str) -> str:
        """Bulk-report many IPs from CSV content (header: IP,Categories,ReportDate,Comment)."""
        if err := _credentials_error():
            return err
        # Strip a leading UTF-8 BOM first (CSVs exported from Excel/Sheets
        # commonly carry one) so an otherwise-correct header is not rejected
        # and the BOM never reaches the API upload.
        csv_content = (csv_content or "").lstrip("\ufeff").strip()
        if not csv_content:
            return (
                "Error: csv_content is required. Provide CSV text with the header "
                "'IP,Categories,ReportDate,Comment' and one row per report "
                "(categories separated by commas inside quotes, ReportDate as "
                "ISO 8601)."
            )
        # Normalise the header (case, spaces, quotes) and require all four
        # documented columns so a malformed header fails here with a clear
        # message instead of a harder-to-read API error after the upload.
        first_line = (
            csv_content.splitlines()[0].lower().replace(" ", "").replace('"', "")
        )
        if first_line != "ip,categories,reportdate,comment":
            return (
                "Error: the first CSV line must be the header "
                "'IP,Categories,ReportDate,Comment'."
            )
        async with _client(timeout=120) as client:
            resp, err = await safe_request(
                client,
                "post",
                f"{ABUSEIPDB_API}/bulk-report",
                _PROVIDER,
                "bulk-report IPs",
                headers=headers,
                files={"csv": ("report.csv", csv_content.encode("utf-8"), "text/csv")},
            )
            if err:
                return err
            data = (resp.json() or {}).get("data", {})
        saved = data.get("savedReports", 0)
        invalid = data.get("invalidReports") or []
        lines = [f"Bulk report accepted: {saved} report(s) saved."]
        if invalid:
            lines.append(f"{len(invalid)} row(s) rejected:")
            for item in invalid[:20]:
                lines.append(
                    f"  row {item.get('rowNumber', '?')}: {item.get('error', '?')} "
                    f"(input: {_trunc(item.get('input'), 60)})"
                )
            if len(invalid) > 20:
                lines.append(f"  ... and {len(invalid) - 20} more rejected rows")
        return "\n".join(lines)

    async def clear_abuseipdb_address(ip_address: str) -> str:
        """Delete ALL reports this account submitted for an IP (cannot be undone)."""
        if err := _credentials_error():
            return err
        ip_address = (ip_address or "").strip()
        if not ip_address:
            return "Error: ip_address is required (a single IPv4 or IPv6 address)."
        if not _is_valid_ip(ip_address):
            return f"Error: '{ip_address}' is not a valid IPv4 or IPv6 address."
        async with _client() as client:
            resp, err = await safe_request(
                client,
                "delete",
                f"{ABUSEIPDB_API}/clear-address",
                _PROVIDER,
                f"clear address {ip_address}",
                headers=headers,
                params={"ipAddress": ip_address},
            )
            if err:
                return err
            data = (resp.json() or {}).get("data", {})
        deleted = data.get("numReportsDeleted", 0)
        return (
            f"Cleared {ip_address}: {deleted} report(s) previously submitted by "
            "this account were permanently deleted."
        )

    # ══════════════════════════════════════════════════════════════════════
    # REGISTER ALL TOOLS
    # ══════════════════════════════════════════════════════════════════════

    executor.register("check_abuseipdb_ip", check_abuseipdb_ip)
    executor.register("list_abuseipdb_ip_reports", list_abuseipdb_ip_reports)
    executor.register("check_abuseipdb_network", check_abuseipdb_network)
    executor.register("get_abuseipdb_blacklist", get_abuseipdb_blacklist)
    executor.register("list_abuseipdb_categories", list_abuseipdb_categories)
    executor.register("report_abuseipdb_ip", report_abuseipdb_ip)
    executor.register("bulk_report_abuseipdb_ips", bulk_report_abuseipdb_ips)
    executor.register("clear_abuseipdb_address", clear_abuseipdb_address)

    return [
        ToolDefinition(
            name="check_abuseipdb_ip",
            description=(
                "Check a single IP address's abuse reputation on AbuseIPDB: abuse "
                "confidence score (0-100), country, ISP, domain, usage type, Tor "
                "exit-node flag, and report statistics. Use this first when "
                "triaging a suspicious IP. Set verbose=true to include the most "
                "recent individual reports (categories + comments); use "
                "list_abuseipdb_ip_reports to page through all reports."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "ip_address": {
                        "type": "string",
                        "description": "A single IPv4 or IPv6 address (e.g. 118.25.6.39).",
                    },
                    "max_age_in_days": {
                        "type": "integer",
                        "default": 30,
                        "description": "Only consider reports from the last N days (1-365).",
                    },
                    "verbose": {
                        "type": "boolean",
                        "default": False,
                        "description": "Include the most recent individual reports in the output.",
                    },
                },
                "required": ["ip_address"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="list_abuseipdb_ip_reports",
            description=(
                "Page through the individual abuse reports filed against an IP "
                "address (report date, reporter country, categories, comment). "
                "Use for deep investigation after check_abuseipdb_ip shows a "
                "non-zero report count."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "ip_address": {
                        "type": "string",
                        "description": "A single IPv4 or IPv6 address.",
                    },
                    "max_age_in_days": {
                        "type": "integer",
                        "default": 30,
                        "description": "Only consider reports from the last N days (1-365).",
                    },
                    "page": {"type": "integer", "default": 1},
                    "per_page": {
                        "type": "integer",
                        "default": 25,
                        "description": "Reports per page (1-100).",
                    },
                },
                "required": ["ip_address"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="check_abuseipdb_network",
            description=(
                "Assess an entire CIDR subnet on AbuseIPDB: netmask, address "
                "range, address-space description, and the reported member IPs "
                "sorted worst-first. Use before blocking a range or to gauge how "
                "dirty a hosting block is. Subnet size is capped by plan tier "
                "(standard up to /24, premium up to /16)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "network": {
                        "type": "string",
                        "description": "CIDR subnet, IPv4 or IPv6 (e.g. 203.0.113.0/24).",
                    },
                    "max_age_in_days": {
                        "type": "integer",
                        "default": 30,
                        "description": (
                            "Only consider reports from the last N days "
                            "(capped by plan tier: standard 30, premium 365)."
                        ),
                    },
                    "limit": {
                        "type": "integer",
                        "default": 25,
                        "description": "Max reported addresses to show (1-500).",
                    },
                },
                "required": ["network"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="get_abuseipdb_blacklist",
            description=(
                "Get AbuseIPDB's blacklist - the most-reported IP addresses, "
                "ordered by abuse confidence score (highest first). Use for "
                "blocklist generation or hunting for known-bad infrastructure. "
                "NOTE: heavily rate-limited (5 calls/day on the free tier); "
                "confidence_minimum below 100 and country filters are "
                "subscriber features."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "confidence_minimum": {
                        "type": "integer",
                        "default": 90,
                        "description": (
                            "Minimum abuse confidence score (25-100). 75-100 is the "
                            "recommended range for denial-of-service decisions."
                        ),
                    },
                    "limit": {
                        "type": "integer",
                        "default": 100,
                        "description": "Max entries to return (the API caps by subscription tier).",
                    },
                    "only_countries": {
                        "type": "string",
                        "description": (
                            "Comma-separated ISO 3166 alpha-2 codes to include "
                            "(e.g. 'US,CN'). Subscriber feature."
                        ),
                    },
                    "except_countries": {
                        "type": "string",
                        "description": (
                            "Comma-separated ISO 3166 alpha-2 codes to exclude. "
                            "Subscriber feature."
                        ),
                    },
                    "ip_version": {
                        "type": "integer",
                        "enum": [4, 6],
                        "description": "Filter to IPv4 or IPv6 only (omit for both).",
                    },
                },
                "required": [],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="list_abuseipdb_categories",
            description=(
                "List the 23 official AbuseIPDB abuse categories (id, name, "
                "description). Local reference - costs no API quota. Use to "
                "interpret category ids in check/report output and to pick the "
                "right ids before calling report_abuseipdb_ip."
            ),
            input_schema={"type": "object", "properties": {}, "required": []},
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="report_abuseipdb_ip",
            description=(
                "Report an abusive IP address to AbuseIPDB (WRITE - the report is "
                "public and affects the IP's community abuse score). Requires at "
                "least one numeric category id (see list_abuseipdb_categories, "
                "e.g. '18,22' for Brute-Force + SSH). Keep the comment factual "
                "(log excerpts, ports) and strip any PII. The same IP can only "
                "be reported once per 15 minutes per account."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "ip": {
                        "type": "string",
                        "description": "The abusive IPv4 or IPv6 address to report.",
                    },
                    "categories": {
                        "type": "string",
                        "description": (
                            "Comma-separated category ids (1-23), e.g. '18,22'. "
                            "See list_abuseipdb_categories."
                        ),
                    },
                    "comment": {
                        "type": "string",
                        "description": (
                            "Descriptive evidence (server logs, ports, timestamps). "
                            "Public - remove any PII."
                        ),
                    },
                    "timestamp": {
                        "type": "string",
                        "description": (
                            "ISO 8601 datetime of the attack (e.g. "
                            "2026-07-04T11:25:11-04:00). Defaults to now."
                        ),
                    },
                },
                "required": ["ip", "categories"],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="bulk_report_abuseipdb_ips",
            description=(
                "Report many abusive IPs to AbuseIPDB in one shot from CSV "
                "content (WRITE - reports are public). The CSV must start with "
                "the header 'IP,Categories,ReportDate,Comment' with one row per "
                "report (quote multi-category values, ReportDate as ISO 8601). "
                "Returns the saved count plus row-level rejections. Heavily "
                "rate-limited (5 calls/day on the free tier) - prefer "
                "report_abuseipdb_ip for a handful of IPs."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "csv_content": {
                        "type": "string",
                        "description": (
                            "CSV text, e.g.:\n"
                            "IP,Categories,ReportDate,Comment\n"
                            '203.0.113.5,"18,22",2026-07-04T11:25:11-04:00,SSH brute force'
                        ),
                    },
                },
                "required": ["csv_content"],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="clear_abuseipdb_address",
            description=(
                "DESTRUCTIVE: permanently delete ALL abuse reports that THIS "
                "account previously submitted for an IP address (e.g. to retract "
                "a false positive). This cannot be undone, only affects your own "
                "account's reports, and is heavily rate-limited (5 calls/day on "
                "the free tier). Returns the number of reports deleted."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "ip_address": {
                        "type": "string",
                        "description": "The IPv4 or IPv6 address to clear your reports for.",
                    },
                },
                "required": ["ip_address"],
            },
            source=_SOURCE,
            priority=2,
        ),
    ]
