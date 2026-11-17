# SPDX-License-Identifier: Apache-2.0

"""Rapid7 Insight Platform integration - Agentic SOC + exposure management.

Full-fledged Rapid7 Insight Platform integration spanning detection & response
(InsightIDR), vulnerability management (InsightVM), and threat intelligence,
designed as a best-in-class building block for an agentic security team:

  - Investigations (*detection & response*, InsightIDR): list, search, get,
    update (status / priority / disposition / assignee), add a comment, and
    list the alerts associated with an investigation
  - Log search (*threat hunting*, InsightIDR / InsightOps): list logs and
    logsets, then run an async LEQL query (start -> poll -> events)
  - Vulnerabilities & assets (*exposure*, InsightVM Cloud Integrations API v4):
    search assets with their exposure, get a single asset, search vulnerability
    definitions (CVSS / CVEs / solution), and get one vulnerability by id
  - Threat intelligence (*creating intelligence*, InsightIDR Community Threats):
    a consolidated tool to create a custom threat and add/replace its IOCs

Authentication
--------------
The Rapid7 Insight Platform authenticates with a single **API key** sent in the
``X-Api-Key`` HTTP header. The platform is region-scoped: every product API
lives under ``https://{region}.api.insight.rapid7.com`` (regions: ``us``,
``us2``, ``us3``, ``eu``, ``ca``, ``au``, ``ap``). ``integration_tools._get_token``
assembles the compound ``base_url||api_key`` token this builder parses, where
``base_url`` is the full region API host (default
``https://us.api.insight.rapid7.com``). Credentials are validated with
``GET {base_url}/validate``.

The InsightIDR investigations and comments APIs are version-gated behind an
``Accept-version`` header (``investigations-preview`` / ``comments-preview``);
the InsightVM (``/vm/v4``), Log Search (``/log_search``) and Community Threats
(``/idr/v1/customthreats``) APIs use only the ``X-Api-Key`` header.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx

from app.core.egress import egress_client
from app.integrations.http import (
    format_entity_list,
    parse_compound_token,
    safe_request,
)
from app.tools.base import CallableToolExecutor, ToolDefinition

logger = logging.getLogger(__name__)

_PROVIDER = "Rapid7"
_SOURCE = "integration:rapid7"

# Default Insight Platform region host. The stored base_url overrides this
# (e.g. https://eu.api.insight.rapid7.com for the EU region).
_DEFAULT_BASE_URL = "https://us.api.insight.rapid7.com"

# Accept-version headers that opt into the InsightIDR preview APIs.
_INVESTIGATIONS_VERSION = "investigations-preview"
_COMMENTS_VERSION = "comments-preview"

# Log Search async query polling budget (start -> poll links[].href -> events).
_LOG_POLL_INTERVAL = 2.0
_LOG_MAX_POLLS = 12

# Investigation enums (sent verbatim in the URL path / assignee body).
_INV_STATUSES: frozenset[str] = frozenset(
    {"OPEN", "INVESTIGATING", "WAITING", "CLOSED"}
)
_INV_PRIORITIES: frozenset[str] = frozenset(
    {"UNSPECIFIED", "LOW", "MEDIUM", "HIGH", "CRITICAL"}
)
_INV_DISPOSITIONS: frozenset[str] = frozenset(
    {"BENIGN", "MALICIOUS", "NOT_APPLICABLE"}
)

# Consolidated threat-indicator management actions.
_THREAT_ACTIONS: frozenset[str] = frozenset({"create", "add", "replace"})

_PRIORITY_ICONS: dict[str, str] = {
    "CRITICAL": "🔴",
    "HIGH": "🟠",
    "MEDIUM": "🟡",
    "LOW": "🔵",
    "UNSPECIFIED": "⚪",
}

# InsightVM grades severity as Critical / Severe / Moderate; tolerate the
# High / Medium / Low spelling too so the icon never falls through to blank.
_VULN_SEVERITY_ICONS: dict[str, str] = {
    "critical": "🔴",
    "severe": "🟠",
    "high": "🟠",
    "moderate": "🟡",
    "medium": "🟡",
    "low": "🔵",
}


def _trunc(text: Any, max_len: int = 300) -> str:
    if text is None:
        return ""
    text = str(text).replace("\r", " ").replace("\n", " ").strip()
    return text[:max_len] + ("…" if len(text) > max_len else "")


def _dt(value: Any) -> str:
    """Trim an ISO timestamp to second precision for display."""
    return str(value or "")[:19]


def _ts_ms(value: Any) -> str:
    """Render an epoch-milliseconds timestamp as a UTC ISO second-precision string."""
    if value in (None, "", 0):
        return ""
    try:
        return datetime.fromtimestamp(int(value) / 1000, UTC).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
    except (TypeError, ValueError, OSError):
        return str(value)


def _prio_icon(value: Any) -> str:
    return _PRIORITY_ICONS.get(str(value or "").upper(), "⚪")


def _sev_icon(value: Any) -> str:
    return _VULN_SEVERITY_ICONS.get(str(value or "").lower(), "⚪")


def _csv_list(value: str) -> list[str]:
    """Split a comma-separated string into a list of trimmed, non-empty items."""
    return [item.strip() for item in (value or "").split(",") if item.strip()]


def _same_origin_url(base: str, href: str) -> str | None:
    """Resolve a Log Search poll ``href`` against ``base``, keeping it only if
    it stays on the same scheme + host.

    The async Log Search flow follows ``links[].href`` from the API response
    while sending the integration's API key. A relative href is resolved
    against the API base; an absolute href must match the base origin or it is
    rejected (returns ``None``) - defence-in-depth so an unexpected or injected
    URL can never turn the authenticated poll into an SSRF request to another
    host.
    """
    if not href:
        return None
    base_parts = urlparse(base)
    target = urljoin(base + "/", href)
    target_parts = urlparse(target)
    if (target_parts.scheme, target_parts.netloc) != (
        base_parts.scheme,
        base_parts.netloc,
    ):
        return None
    return target


def _cvss_scores(vuln: dict) -> tuple[str, str]:
    """Extract (cvss_v3, cvss_v2) base scores from a vulnerability definition.

    The InsightVM Cloud API nests CVSS under ``cvss_v3`` / ``cvss_v2`` objects
    (each with a ``score``); fall back to the alternate ``cvss.v3.score`` shape
    so the score is surfaced regardless of which response variant is returned.
    """
    cvss = vuln.get("cvss") or {}
    v3 = vuln.get("cvss_v3") or cvss.get("v3") or {}
    v2 = vuln.get("cvss_v2") or cvss.get("v2") or {}
    v3_score = v3.get("score") if isinstance(v3, dict) else None
    v2_score = v2.get("score") if isinstance(v2, dict) else None
    return (
        str(v3_score) if v3_score is not None else "N/A",
        str(v2_score) if v2_score is not None else "N/A",
    )


# ---------------------------------------------------------------------------
# Auth + URL helpers (shared with integration_testing.py)
# ---------------------------------------------------------------------------


def rapid7_api_base(base_url: str) -> str:
    """Return the Rapid7 Insight Platform API base URL (region host, no trailing slash).

    Whitespace is stripped before the default fallback and the trailing-slash
    trim, so a pasted ``base_url`` with surrounding whitespace (or an entirely
    whitespace-only value) normalises cleanly to a valid host (or the default
    region host) - matching ``tenable_api_base`` and keeping the log-search
    same-origin guard's host comparison accurate.
    """
    return ((base_url or "").strip() or _DEFAULT_BASE_URL).rstrip("/")


def build_rapid7_headers(api_key: str) -> dict[str, str]:
    """Build Rapid7 request headers (X-Api-Key + JSON content negotiation).

    The API key is stripped so trailing/leading whitespace or newlines (common
    when pasting credentials) can never produce a malformed ``X-Api-Key`` header
    (httpx/h11 reject header values containing newlines). This is the single
    chokepoint shared by the tool builder and the connectivity test.
    """
    return {
        "X-Api-Key": (api_key or "").strip(),
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


# ---------------------------------------------------------------------------
# Tool builder
# ---------------------------------------------------------------------------


def build_tools(
    token: str,
    executor: CallableToolExecutor,
    **_kwargs: Any,
) -> list[ToolDefinition]:
    """Build Rapid7 tools from a ``base_url||api_key`` compound token."""
    # ``parse_compound_token`` does not normalise - strip each segment so pasted
    # whitespace can't yield a malformed base URL / ``X-Api-Key`` header, and so
    # the empty-key guard below treats a whitespace-only key as missing.
    base_url, api_key = (part.strip() for part in parse_compound_token(token, 2))
    # Only the API key is strictly required. ``base_url`` is optional: a blank
    # region host falls back to the default (https://us.api.insight.rapid7.com)
    # via ``rapid7_api_base()``, matching the catalog default and the
    # connectivity test - so a Rapid7 integration configured without an explicit
    # region still exposes its tools instead of silently disappearing.
    if not api_key:
        return []

    api = rapid7_api_base(base_url)
    headers = build_rapid7_headers(api_key)
    # InsightIDR investigations / comments are gated behind Accept-version
    # preview headers; the VM, log-search and threats APIs ignore it.
    idr_headers = {**headers, "Accept-version": _INVESTIGATIONS_VERSION}
    comment_headers = {**headers, "Accept-version": _COMMENTS_VERSION}

    def _client(timeout: int = 30) -> httpx.AsyncClient:
        return egress_client(timeout=timeout)

    def _clamp(limit: Any, default: int = 25, hi: int = 100) -> int:
        try:
            return max(1, min(int(limit), hi))
        except (TypeError, ValueError):
            return default

    # ══════════════════════════════════════════════════════════════════════
    # 0. CONNECTION
    # ══════════════════════════════════════════════════════════════════════

    async def get_rapid7_status() -> str:
        """Validate the Rapid7 Insight Platform connection (GET /validate)."""
        async with _client() as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{api}/validate",
                _PROVIDER,
                "validate API key",
                headers=headers,
            )
            if err:
                return err
            data = resp.json() or {}
            return (
                f"Connected to Rapid7 Insight Platform at {api}.\n"
                f"  Authorization: {data.get('message', 'Authorized')}"
            )

    # ══════════════════════════════════════════════════════════════════════
    # 1. INVESTIGATIONS (detection & response - InsightIDR)
    # ══════════════════════════════════════════════════════════════════════

    def _fmt_investigation(inv: dict) -> str:
        assignee = inv.get("assignee") or {}
        who = assignee.get("email") or assignee.get("name") or "Unassigned"
        return (
            f"  {_prio_icon(inv.get('priority'))} [{inv.get('priority', '?')}] "
            f"{inv.get('status', '?')} - {_trunc(inv.get('title'), 90)}\n"
            f"    RRN: {inv.get('rrn', inv.get('id', '?'))} | "
            f"Disposition: {inv.get('disposition', '?')} | Assignee: {who}\n"
            f"    Created: {_dt(inv.get('created_time'))} | "
            f"Source: {inv.get('source', '?')}"
        )

    async def list_rapid7_investigations(
        statuses: str = "",
        priorities: str = "",
        assignee_email: str = "",
        start_time: str = "",
        end_time: str = "",
        sort: str = "",
        limit: int = 20,
    ) -> str:
        """List InsightIDR investigations filtered by status, priority, assignee, or time window."""
        params: dict[str, Any] = {"size": _clamp(limit, 20)}
        # Normalise the comma-separated filters through _csv_list so embedded
        # spaces (e.g. "open, closed") are stripped before upper-casing - a
        # value like "OPEN, CLOSED" with a space would otherwise be rejected by
        # the InsightIDR API.
        status_list = _csv_list(statuses)
        if status_list:
            params["statuses"] = ",".join(s.upper() for s in status_list)
        priority_list = _csv_list(priorities)
        if priority_list:
            params["priorities"] = ",".join(p.upper() for p in priority_list)
        if assignee_email:
            params["assignee.email"] = assignee_email
        if start_time:
            params["start_time"] = start_time
        if end_time:
            params["end_time"] = end_time
        if sort:
            params["sort"] = sort
        async with _client() as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{api}/idr/v2/investigations",
                _PROVIDER,
                "list investigations",
                headers=idr_headers,
                params=params,
            )
            if err:
                return err
            body = resp.json() or {}
        items = body.get("data", [])
        total = (body.get("metadata") or {}).get("total_data", len(items))
        return format_entity_list(
            items, _fmt_investigation, noun="investigation", total=total
        )

    async def search_rapid7_investigations(
        search: str,
        sort: str = "",
        start_time: str = "",
        end_time: str = "",
        limit: int = 20,
    ) -> str:
        """Search InsightIDR investigations with a free-form query plus an optional time window.

        Use this instead of list_rapid7_investigations when you need to match on
        text/fields beyond the simple status/priority/assignee filters.
        """
        body: dict[str, Any] = {"search": search}
        if sort:
            body["sort"] = sort
        if start_time:
            body["start_time"] = start_time
        if end_time:
            body["end_time"] = end_time
        async with _client() as client:
            resp, err = await safe_request(
                client,
                "post",
                f"{api}/idr/v2/investigations/_search",
                _PROVIDER,
                "search investigations",
                headers=idr_headers,
                params={"size": _clamp(limit, 20)},
                json=body,
            )
            if err:
                return err
            payload = resp.json() or {}
        items = payload.get("data", [])
        total = (payload.get("metadata") or {}).get("total_data", len(items))
        return format_entity_list(
            items, _fmt_investigation, noun="investigation", total=total
        )

    async def get_rapid7_investigation(investigation_id: str) -> str:
        """Get a single InsightIDR investigation by its id or RRN."""
        async with _client() as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{api}/idr/v2/investigations/{investigation_id}",
                _PROVIDER,
                f"get investigation {investigation_id}",
                headers=idr_headers,
            )
            if err:
                return err
            body = resp.json() or {}
        inv = body.get("data", body) if isinstance(body, dict) else {}
        assignee = inv.get("assignee") or {}
        who = assignee.get("email") or assignee.get("name") or "Unassigned"
        lines = [
            f"Investigation: {_trunc(inv.get('title'), 200)}",
            f"RRN: {inv.get('rrn', inv.get('id', investigation_id))}",
            f"Status: {inv.get('status', '?')} | "
            f"Priority: {_prio_icon(inv.get('priority'))} {inv.get('priority', '?')} | "
            f"Disposition: {inv.get('disposition', '?')}",
            f"Assignee: {who} | Source: {inv.get('source', '?')}",
            f"Created: {_dt(inv.get('created_time'))} | "
            f"First alert: {_dt(inv.get('first_alert_time'))} | "
            f"Latest alert: {_dt(inv.get('latest_alert_time'))}",
        ]
        if inv.get("organization_id"):
            lines.append(f"Organization: {inv.get('organization_id')}")
        return "\n".join(lines)

    async def update_rapid7_investigation(
        investigation_id: str,
        status: str = "",
        priority: str = "",
        disposition: str = "",
        assignee_email: str = "",
    ) -> str:
        """Update an InsightIDR investigation: set status, priority, disposition, and/or assignee.

        Each provided field is applied with its dedicated endpoint. Closing an
        investigation (status=CLOSED) is typically paired with a disposition
        (BENIGN / MALICIOUS / NOT_APPLICABLE).
        """
        status = status.upper().strip()
        priority = priority.upper().strip()
        disposition = disposition.upper().strip()
        if status and status not in _INV_STATUSES:
            return f"Error: status must be one of {', '.join(sorted(_INV_STATUSES))}."
        if priority and priority not in _INV_PRIORITIES:
            return (
                f"Error: priority must be one of {', '.join(sorted(_INV_PRIORITIES))}."
            )
        if disposition and disposition not in _INV_DISPOSITIONS:
            return (
                "Error: disposition must be one of "
                f"{', '.join(sorted(_INV_DISPOSITIONS))}."
            )
        if not any([status, priority, disposition, assignee_email]):
            return "Error: No fields provided to update."

        base = f"{api}/idr/v2/investigations/{investigation_id}"
        applied: list[str] = []
        async with _client() as client:
            for label, url, kwargs in (
                (
                    f"status={status}",
                    f"{base}/status/{status}" if status else "",
                    {},
                ),
                (
                    f"priority={priority}",
                    f"{base}/priority/{priority}" if priority else "",
                    {},
                ),
                (
                    f"disposition={disposition}",
                    f"{base}/disposition/{disposition}" if disposition else "",
                    {},
                ),
                (
                    f"assignee={assignee_email}",
                    f"{base}/assignee" if assignee_email else "",
                    {"json": {"user_email_address": assignee_email}},
                ),
            ):
                if not url:
                    continue
                _, err = await safe_request(
                    client,
                    "put",
                    url,
                    _PROVIDER,
                    f"update investigation ({label})",
                    headers=idr_headers,
                    **kwargs,
                )
                if err:
                    return (
                        f"Updated {', '.join(applied) or 'nothing'} before failing on "
                        f"{label}: {err}"
                        if applied
                        else err
                    )
                applied.append(label)
        return f"Investigation {investigation_id} updated: {', '.join(applied)}."

    async def add_rapid7_investigation_comment(
        investigation_rrn: str, body: str
    ) -> str:
        """Add a comment to an InsightIDR investigation (target is the investigation RRN)."""
        async with _client() as client:
            _, err = await safe_request(
                client,
                "post",
                f"{api}/idr/v1/comments",
                _PROVIDER,
                f"comment on investigation {investigation_rrn}",
                headers=comment_headers,
                json={"target": investigation_rrn, "body": body, "attachments": []},
            )
            if err:
                return err
        return f"Comment added to investigation {investigation_rrn}."

    async def list_rapid7_investigation_alerts(
        investigation_id: str, limit: int = 20
    ) -> str:
        """List the alerts associated with a specific InsightIDR investigation."""
        async with _client() as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{api}/idr/v2/investigations/{investigation_id}/alerts",
                _PROVIDER,
                f"list alerts for investigation {investigation_id}",
                headers=idr_headers,
                params={"size": _clamp(limit, 20)},
            )
            if err:
                return err
            body = resp.json() or {}
        items = body.get("data", []) if isinstance(body, dict) else []

        def _fmt(a: dict) -> str:
            return (
                f"  {_prio_icon(a.get('priority'))} {_trunc(a.get('title'), 90)}\n"
                f"    RRN: {a.get('rrn', a.get('id', '?'))} | "
                f"Type: {a.get('type', a.get('alert_type', '?'))} | "
                f"Created: {_dt(a.get('created_time', a.get('detection_created_time')))}"
            )

        total = (body.get("metadata") or {}).get("total_data", len(items))
        return format_entity_list(items, _fmt, noun="alert", total=total)

    # ══════════════════════════════════════════════════════════════════════
    # 2. LOG SEARCH (threat hunting - InsightIDR / InsightOps)
    # ══════════════════════════════════════════════════════════════════════

    async def list_rapid7_logs(limit: int = 50) -> str:
        """List the available logs (data sources) and their ids for log search."""
        async with _client() as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{api}/log_search/management/logs",
                _PROVIDER,
                "list logs",
                headers=headers,
            )
            if err:
                return err
            body = resp.json() or {}
        logs = body.get("logs", []) if isinstance(body, dict) else []
        shown = logs[: _clamp(limit, 50, hi=200)]

        def _fmt(log: dict) -> str:
            logsets = ", ".join(
                ls.get("name", "?") for ls in (log.get("logsets_info") or [])[:3]
            )
            return (
                f"  {log.get('name', 'Unnamed')} (id {log.get('id', '?')})"
                + (f" | Logsets: {logsets}" if logsets else "")
            )

        return format_entity_list(shown, _fmt, noun="log", total=len(logs))

    async def list_rapid7_logsets(limit: int = 50) -> str:
        """List the logsets (named collections of logs) used to scope log search."""
        async with _client() as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{api}/log_search/management/logsets",
                _PROVIDER,
                "list logsets",
                headers=headers,
            )
            if err:
                return err
            body = resp.json() or {}
        logsets = body.get("logsets", []) if isinstance(body, dict) else []
        shown = logsets[: _clamp(limit, 50, hi=200)]

        def _fmt(ls: dict) -> str:
            return (
                f"  {ls.get('name', 'Unnamed')} (id {ls.get('id', '?')}) - "
                f"{len(ls.get('logs_info') or [])} logs"
            )

        return format_entity_list(shown, _fmt, noun="logset", total=len(logsets))

    async def run_rapid7_log_search(
        log_id: str,
        query: str,
        time_range: str = "Last 24 Hours",
        limit: int = 50,
    ) -> str:
        """Run a LEQL query against an InsightIDR log and return the matching events.

        Provide a log_id from list_rapid7_logs and a LEQL query
        (e.g. 'where(source_ip = "10.0.0.1")' or
        'where(FAILED_BAD_LOGIN) calculate(count) timeslice(1h)'). time_range is
        a relative window like 'Last 24 Hours', 'Today', or 'Last 7 Days'. The
        query runs asynchronously: it is started, then polled until complete.
        """
        params = {"time_range": time_range, "query": query}
        async with _client(timeout=60) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{api}/log_search/query/logs/{log_id}",
                _PROVIDER,
                "run log search",
                headers=headers,
                params=params,
            )
            if err:
                return err
            data = resp.json() or {}
            # Poll links[0].href until the query completes (no more links).
            for _ in range(_LOG_MAX_POLLS):
                links = data.get("links") or []
                raw_href = links[0].get("href") if links else ""
                if not raw_href:
                    break
                # Only follow a poll URL that stays on the configured Rapid7
                # host - never send the API key to an unexpected/injected host.
                href = _same_origin_url(api, raw_href)
                if not href:
                    return (
                        "Error: Rapid7 log search returned a poll URL on an "
                        "unexpected host; refusing to follow it."
                    )
                await asyncio.sleep(_LOG_POLL_INTERVAL)
                poll_resp, poll_err = await safe_request(
                    client,
                    "get",
                    href,
                    _PROVIDER,
                    "poll log search",
                    headers=headers,
                )
                if poll_err:
                    return poll_err
                data = poll_resp.json() or {}

        events = data.get("events")
        if events:
            shown = events[: _clamp(limit, 50, hi=500)]

            def _fmt(ev: dict) -> str:
                return f"  [{_ts_ms(ev.get('timestamp'))}] {_trunc(ev.get('message'), 240)}"

            return format_entity_list(shown, _fmt, noun="event", total=len(events))
        stats = data.get("statistics")
        if stats:
            return f"Log search complete (aggregated result):\n{_trunc(stats, 1500)}"
        if data.get("links"):
            return (
                "Log search is still running (polling budget exhausted). "
                "Narrow the time_range or query and try again."
            )
        return "Log search complete: 0 events."

    # ══════════════════════════════════════════════════════════════════════
    # 3. VULNERABILITIES & ASSETS (exposure - InsightVM Cloud API v4)
    # ══════════════════════════════════════════════════════════════════════

    def _fmt_asset(a: dict) -> str:
        host = a.get("host_name") or a.get("ip") or a.get("id", "?")
        os_name = a.get("os_name") or a.get("os_description") or "?"
        return (
            f"  {host} (id {a.get('id', '?')}) - {_trunc(os_name, 50)}\n"
            f"    Vulns total: {a.get('total_vulnerabilities', '?')} | "
            f"Critical: {a.get('critical_vulnerabilities', 0)} "
            f"Severe: {a.get('severe_vulnerabilities', 0)} "
            f"Moderate: {a.get('moderate_vulnerabilities', 0)} | "
            f"Last assessed: {_dt(a.get('last_assessed_for_vulnerabilities'))}"
        )

    async def list_rapid7_assets(
        asset_query: str = "",
        vulnerability_query: str = "",
        limit: int = 25,
    ) -> str:
        """Search InsightVM assets and their exposure summary.

        asset_query / vulnerability_query are InsightVM search expressions
        (e.g. asset_query="asset.name CONTAINS 'web'",
        vulnerability_query="cves IN ['CVE-2021-44228']"). Leave both empty to
        list all assets.
        """
        body: dict[str, Any] = {}
        if asset_query:
            body["asset"] = asset_query
        if vulnerability_query:
            body["vulnerability"] = vulnerability_query
        async with _client(timeout=60) as client:
            resp, err = await safe_request(
                client,
                "post",
                f"{api}/vm/v4/integration/assets",
                _PROVIDER,
                "search assets",
                headers=headers,
                params={"size": _clamp(limit, 25, hi=100)},
                json=body,
            )
            if err:
                return err
            payload = resp.json() or {}
        items = payload.get("data", [])
        total = (payload.get("metadata") or {}).get("total_resources", len(items))
        return format_entity_list(items, _fmt_asset, noun="asset", total=total)

    async def get_rapid7_asset(asset_id: str) -> str:
        """Get a single InsightVM asset by id (identity, OS, exposure counts)."""
        async with _client() as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{api}/vm/v4/integration/assets/{asset_id}",
                _PROVIDER,
                f"get asset {asset_id}",
                headers=headers,
            )
            if err:
                return err
            a = resp.json() or {}
        if isinstance(a, dict) and isinstance(a.get("data"), dict):
            a = a["data"]
        tags = ", ".join(
            str(t.get("name", t)) for t in (a.get("tags") or [])[:8]
        )
        lines = [
            f"Asset {a.get('id', asset_id)}: {a.get('host_name') or a.get('ip') or '?'}",
            f"IP: {a.get('ip', 'N/A')} | MAC: {a.get('mac', 'N/A')}",
            f"OS: {_trunc(a.get('os_description') or a.get('os_name'), 80) or 'N/A'}",
            f"Vulnerabilities - Total: {a.get('total_vulnerabilities', '?')} | "
            f"Critical: {a.get('critical_vulnerabilities', 0)} "
            f"Severe: {a.get('severe_vulnerabilities', 0)} "
            f"Moderate: {a.get('moderate_vulnerabilities', 0)}",
            f"Exploits: {a.get('exploits', 0)} | Malware kits: {a.get('malware_kits', 0)} | "
            f"Last assessed: {_dt(a.get('last_assessed_for_vulnerabilities'))}",
        ]
        if tags:
            lines.append(f"Tags: {tags}")
        return "\n".join(lines)

    def _fmt_vuln(v: dict) -> str:
        v3, v2 = _cvss_scores(v)
        cves = ", ".join((v.get("cves") or [])[:6])
        return (
            f"  {_sev_icon(v.get('severity'))} [{v.get('severity', '?')}] "
            f"{v.get('id', '?')}: {_trunc(v.get('title'), 80)}\n"
            f"    CVSSv3: {v3} | CVSSv2: {v2}"
            + (f" | CVEs: {cves}" if cves else "")
        )

    async def list_rapid7_vulnerabilities(
        vulnerability_query: str = "", limit: int = 25
    ) -> str:
        """Search InsightVM vulnerability definitions (CVSS, CVEs, severity).

        vulnerability_query is an InsightVM expression
        (e.g. "vulnerability.severity = 'Critical'" or "cves IN ['CVE-2021-44228']").
        Leave empty to list vulnerability definitions broadly.
        """
        body: dict[str, Any] = {}
        if vulnerability_query:
            body["vulnerability"] = vulnerability_query
        async with _client(timeout=60) as client:
            resp, err = await safe_request(
                client,
                "post",
                f"{api}/vm/v4/integration/vulnerabilities",
                _PROVIDER,
                "search vulnerabilities",
                headers=headers,
                params={"size": _clamp(limit, 25, hi=100)},
                json=body,
            )
            if err:
                return err
            payload = resp.json() or {}
        items = payload.get("data", [])
        total = (payload.get("metadata") or {}).get("total_resources", len(items))
        return format_entity_list(items, _fmt_vuln, noun="vulnerability", total=total)

    async def get_rapid7_vulnerability(vulnerability_id: str) -> str:
        """Get full detail for a single InsightVM vulnerability by its id (vulnKey).

        Looks up the vulnerability definition (CVSS v2/v3, CVEs, description,
        solution) via the vulnerabilities search filtered to this id.
        """
        # The id is interpolated into a single-quoted InsightVM search literal.
        # A vulnKey is a machine identifier ([a-z0-9._-]) that never contains a
        # quote, so reject one rather than risk a malformed query or an injected
        # filter clause changing the intended semantics.
        if "'" in vulnerability_id:
            return (
                "Error: vulnerability_id must not contain a single quote. "
                "Provide a valid InsightVM vulnerability id (vulnKey)."
            )
        async with _client() as client:
            resp, err = await safe_request(
                client,
                "post",
                f"{api}/vm/v4/integration/vulnerabilities",
                _PROVIDER,
                f"get vulnerability {vulnerability_id}",
                headers=headers,
                json={"vulnerability": f"vulnerability.vulnKey = '{vulnerability_id}'"},
            )
            if err:
                return err
            payload = resp.json() or {}
        items = payload.get("data", [])
        if not items:
            return f"No vulnerability found for id '{vulnerability_id}'."
        v = items[0]
        v3, v2 = _cvss_scores(v)
        lines = [
            f"Vulnerability {v.get('id', vulnerability_id)}: {v.get('title', '?')}",
            f"Severity: {_sev_icon(v.get('severity'))} {v.get('severity', '?')} | "
            f"Risk score: {v.get('risk_score', v.get('severity_score', 'N/A'))}",
            f"CVSSv3: {v3} | CVSSv2: {v2}",
        ]
        if v.get("cves"):
            lines.append(f"CVEs: {', '.join(v.get('cves')[:15])}")
        if v.get("categories"):
            lines.append(f"Categories: {', '.join(str(c) for c in v['categories'][:8])}")
        if v.get("published"):
            lines.append(
                f"Published: {_dt(v.get('published'))} | Added: {_dt(v.get('added'))}"
            )
        if v.get("description"):
            lines.append(f"\nDescription: {_trunc(v.get('description'), 600)}")
        solutions = v.get("solutions") or v.get("solution")
        if isinstance(solutions, list) and solutions:
            first = solutions[0]
            sol = first.get("fix") if isinstance(first, dict) else first
            lines.append(f"\nSolution: {_trunc(sol, 400)}")
        elif solutions:
            lines.append(f"\nSolution: {_trunc(solutions, 400)}")
        return "\n".join(lines)

    # ══════════════════════════════════════════════════════════════════════
    # 4. THREAT INTELLIGENCE (creating intelligence - Community Threats)
    # ══════════════════════════════════════════════════════════════════════

    async def manage_rapid7_threat_indicators(
        action: str,
        threat_name: str = "",
        note: str = "",
        threat_key: str = "",
        ips: str = "",
        hashes: str = "",
        domain_names: str = "",
        urls: str = "",
    ) -> str:
        """Manage InsightIDR Community Threat indicators (IOCs).

        action="create": create a new custom threat (requires threat_name);
          returns its threat key.
        action="add": add the given IOCs to an existing threat (requires
          threat_key).
        action="replace": replace all IOCs of an existing threat (requires
          threat_key).
        Indicator arguments (ips, hashes, domain_names, urls) are
        comma-separated lists.
        """
        action = (action or "").lower().strip()
        if action not in _THREAT_ACTIONS:
            return f"Error: action must be one of {', '.join(sorted(_THREAT_ACTIONS))}."
        indicators = {
            "ips": _csv_list(ips),
            "hashes": _csv_list(hashes),
            "domain_names": _csv_list(domain_names),
            "urls": _csv_list(urls),
        }
        has_iocs = any(indicators.values())

        async with _client() as client:
            if action == "create":
                if not threat_name:
                    return "Error: threat_name is required to create a threat."
                resp, err = await safe_request(
                    client,
                    "post",
                    f"{api}/idr/v1/customthreats",
                    _PROVIDER,
                    f"create threat {threat_name}",
                    headers=headers,
                    json={"threat": threat_name, "note": note, "indicators": indicators},
                )
                if err:
                    return err
                created = resp.json() or {}
                key = created.get("key") or created.get("threat_key") or "?"
                return (
                    f"Threat '{threat_name}' created (key {key}). "
                    f"Use this key with action=add to append more IOCs."
                )

            if not threat_key:
                return f"Error: threat_key is required to {action} indicators."
            if not has_iocs:
                return (
                    "Error: provide at least one indicator "
                    "(ips, hashes, domain_names, or urls)."
                )
            _, err = await safe_request(
                client,
                "post",
                f"{api}/idr/v1/customthreats/key/{threat_key}/indicators/{action}",
                _PROVIDER,
                f"{action} threat indicators",
                headers=headers,
                params={"format": "json"},
                json=indicators,
            )
            if err:
                return err
        counts = ", ".join(f"{len(v)} {k}" for k, v in indicators.items() if v)
        verb = "added to" if action == "add" else "replaced on"
        return f"Indicators ({counts}) {verb} threat {threat_key}."

    # ══════════════════════════════════════════════════════════════════════
    # REGISTER ALL TOOLS
    # ══════════════════════════════════════════════════════════════════════

    executor.register("get_rapid7_status", get_rapid7_status)
    executor.register("list_rapid7_investigations", list_rapid7_investigations)
    executor.register("search_rapid7_investigations", search_rapid7_investigations)
    executor.register("get_rapid7_investigation", get_rapid7_investigation)
    executor.register("update_rapid7_investigation", update_rapid7_investigation)
    executor.register(
        "add_rapid7_investigation_comment", add_rapid7_investigation_comment
    )
    executor.register(
        "list_rapid7_investigation_alerts", list_rapid7_investigation_alerts
    )
    executor.register("list_rapid7_logs", list_rapid7_logs)
    executor.register("list_rapid7_logsets", list_rapid7_logsets)
    executor.register("run_rapid7_log_search", run_rapid7_log_search)
    executor.register("list_rapid7_assets", list_rapid7_assets)
    executor.register("get_rapid7_asset", get_rapid7_asset)
    executor.register("list_rapid7_vulnerabilities", list_rapid7_vulnerabilities)
    executor.register("get_rapid7_vulnerability", get_rapid7_vulnerability)
    executor.register(
        "manage_rapid7_threat_indicators", manage_rapid7_threat_indicators
    )

    return [
        # -- 0. Connection --------------------------------------------------
        ToolDefinition(
            name="get_rapid7_status",
            description=(
                "Validate the Rapid7 Insight Platform connection and confirm the "
                "API key is authorized. Use first if other Rapid7 tools fail."
            ),
            input_schema={"type": "object", "properties": {}, "required": []},
            source=_SOURCE,
            priority=2,
        ),
        # -- 1. Investigations (InsightIDR) ---------------------------------
        ToolDefinition(
            name="list_rapid7_investigations",
            description=(
                "List InsightIDR investigations (detection & response queue). "
                "Filter by statuses (OPEN/INVESTIGATING/WAITING/CLOSED), priorities "
                "(UNSPECIFIED/LOW/MEDIUM/HIGH/CRITICAL), assignee_email, or a time "
                "window (ISO start_time/end_time)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "statuses": {
                        "type": "string",
                        "description": "Comma-separated statuses, e.g. 'OPEN,INVESTIGATING'",
                    },
                    "priorities": {
                        "type": "string",
                        "description": "Comma-separated priorities, e.g. 'HIGH,CRITICAL'",
                    },
                    "assignee_email": {"type": "string"},
                    "start_time": {
                        "type": "string",
                        "description": "ISO-8601 timestamp lower bound on created_time",
                    },
                    "end_time": {
                        "type": "string",
                        "description": "ISO-8601 timestamp upper bound on created_time",
                    },
                    "sort": {
                        "type": "string",
                        "description": "Field,direction (e.g. 'priority,DESC')",
                    },
                    "limit": {"type": "integer", "default": 20},
                },
                "required": [],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="search_rapid7_investigations",
            description=(
                "Search InsightIDR investigations with a free-form query string "
                "plus an optional ISO time window. Use when the simple list filters "
                "are not enough."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "search": {"type": "string", "description": "Search query string"},
                    "sort": {"type": "string"},
                    "start_time": {"type": "string"},
                    "end_time": {"type": "string"},
                    "limit": {"type": "integer", "default": 20},
                },
                "required": ["search"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="get_rapid7_investigation",
            description=(
                "Get a single InsightIDR investigation by id or RRN (status, "
                "priority, disposition, assignee, alert timeline)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "investigation_id": {
                        "type": "string",
                        "description": "Investigation id or RRN",
                    }
                },
                "required": ["investigation_id"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="update_rapid7_investigation",
            description=(
                "Update an InsightIDR investigation: set status (OPEN/INVESTIGATING/"
                "WAITING/CLOSED), priority (UNSPECIFIED/LOW/MEDIUM/HIGH/CRITICAL), "
                "disposition (BENIGN/MALICIOUS/NOT_APPLICABLE), and/or assignee_email. "
                "Provide only the fields you want to change."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "investigation_id": {
                        "type": "string",
                        "description": "Investigation id or RRN",
                    },
                    "status": {
                        "type": "string",
                        "enum": ["OPEN", "INVESTIGATING", "WAITING", "CLOSED"],
                    },
                    "priority": {
                        "type": "string",
                        "enum": ["UNSPECIFIED", "LOW", "MEDIUM", "HIGH", "CRITICAL"],
                    },
                    "disposition": {
                        "type": "string",
                        "enum": ["BENIGN", "MALICIOUS", "NOT_APPLICABLE"],
                    },
                    "assignee_email": {
                        "type": "string",
                        "description": "Email of the user to assign",
                    },
                },
                "required": ["investigation_id"],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="add_rapid7_investigation_comment",
            description=(
                "Add a comment to an InsightIDR investigation (audit trail / "
                "analyst note). The target is the investigation RRN."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "investigation_rrn": {
                        "type": "string",
                        "description": "Investigation RRN (rrn:investigation:...)",
                    },
                    "body": {"type": "string", "description": "Comment text"},
                },
                "required": ["investigation_rrn", "body"],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="list_rapid7_investigation_alerts",
            description="List the alerts associated with a specific InsightIDR investigation.",
            input_schema={
                "type": "object",
                "properties": {
                    "investigation_id": {
                        "type": "string",
                        "description": "Investigation id or RRN",
                    },
                    "limit": {"type": "integer", "default": 20},
                },
                "required": ["investigation_id"],
            },
            source=_SOURCE,
            priority=0,
        ),
        # -- 2. Log search (threat hunting) ---------------------------------
        ToolDefinition(
            name="list_rapid7_logs",
            description=(
                "List the available InsightIDR logs (data sources) with their ids. "
                "Use to find the log_id needed by run_rapid7_log_search."
            ),
            input_schema={
                "type": "object",
                "properties": {"limit": {"type": "integer", "default": 50}},
                "required": [],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="list_rapid7_logsets",
            description="List InsightIDR logsets (named collections of logs).",
            input_schema={
                "type": "object",
                "properties": {"limit": {"type": "integer", "default": 50}},
                "required": [],
            },
            source=_SOURCE,
            priority=2,
        ),
        ToolDefinition(
            name="run_rapid7_log_search",
            description=(
                "Run a LEQL query against an InsightIDR log for threat hunting and "
                "return the matching events. Provide a log_id (from list_rapid7_logs), "
                "a LEQL query (e.g. 'where(source_ip = \"10.0.0.1\")'), and a "
                "time_range like 'Last 24 Hours', 'Today', or 'Last 7 Days'. Runs "
                "asynchronously (start -> poll -> events)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "log_id": {"type": "string", "description": "Log id to search"},
                    "query": {"type": "string", "description": "LEQL query"},
                    "time_range": {
                        "type": "string",
                        "default": "Last 24 Hours",
                        "description": "Relative window, e.g. 'Last 24 Hours'",
                    },
                    "limit": {"type": "integer", "default": 50},
                },
                "required": ["log_id", "query"],
            },
            source=_SOURCE,
            priority=0,
        ),
        # -- 3. Vulnerabilities & assets (InsightVM) ------------------------
        ToolDefinition(
            name="list_rapid7_assets",
            description=(
                "Search InsightVM assets and their exposure summary (per-severity "
                "vuln counts, OS, last assessed). asset_query / vulnerability_query "
                "are InsightVM search expressions (e.g. asset_query=\"asset.name "
                "CONTAINS 'web'\", vulnerability_query=\"cves IN ['CVE-2021-44228']\"). "
                "Leave both empty to list all assets."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "asset_query": {
                        "type": "string",
                        "description": "InsightVM asset search expression",
                    },
                    "vulnerability_query": {
                        "type": "string",
                        "description": "InsightVM vulnerability search expression",
                    },
                    "limit": {"type": "integer", "default": 25},
                },
                "required": [],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="get_rapid7_asset",
            description=(
                "Get a single InsightVM asset by id (identity, OS, IP/MAC, "
                "per-severity vulnerability counts, exploits, tags)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "asset_id": {"type": "string", "description": "InsightVM asset id"}
                },
                "required": ["asset_id"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="list_rapid7_vulnerabilities",
            description=(
                "Search InsightVM vulnerability definitions (CVSS v2/v3, CVEs, "
                "severity). vulnerability_query is an InsightVM expression "
                "(e.g. \"vulnerability.severity = 'Critical'\"). Leave empty to list "
                "broadly."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "vulnerability_query": {
                        "type": "string",
                        "description": "InsightVM vulnerability search expression",
                    },
                    "limit": {"type": "integer", "default": 25},
                },
                "required": [],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="get_rapid7_vulnerability",
            description=(
                "Get full detail for a single InsightVM vulnerability by its id "
                "(vulnKey): CVSS v2/v3, CVEs, categories, description, and solution."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "vulnerability_id": {
                        "type": "string",
                        "description": "InsightVM vulnerability id (vulnKey)",
                    }
                },
                "required": ["vulnerability_id"],
            },
            source=_SOURCE,
            priority=0,
        ),
        # -- 4. Threat intelligence (Community Threats) ---------------------
        ToolDefinition(
            name="manage_rapid7_threat_indicators",
            description=(
                "Manage InsightIDR Community Threat indicators (IOCs). "
                "action='create' makes a new threat (needs threat_name) and returns "
                "its key; action='add' appends IOCs to an existing threat (needs "
                "threat_key); action='replace' replaces all IOCs of a threat (needs "
                "threat_key). ips/hashes/domain_names/urls are comma-separated."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["create", "add", "replace"],
                    },
                    "threat_name": {
                        "type": "string",
                        "description": "Name for the new threat (action=create)",
                    },
                    "note": {
                        "type": "string",
                        "description": "Optional description (action=create)",
                    },
                    "threat_key": {
                        "type": "string",
                        "description": "Threat key (action=add/replace)",
                    },
                    "ips": {"type": "string", "description": "Comma-separated IPs"},
                    "hashes": {
                        "type": "string",
                        "description": "Comma-separated file hashes",
                    },
                    "domain_names": {
                        "type": "string",
                        "description": "Comma-separated domains",
                    },
                    "urls": {"type": "string", "description": "Comma-separated URLs"},
                },
                "required": ["action"],
            },
            source=_SOURCE,
            priority=1,
        ),
    ]
