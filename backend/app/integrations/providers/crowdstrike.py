# SPDX-License-Identifier: Apache-2.0

"""CrowdStrike Falcon integration – Agentic SOC for endpoint security.

Full-fledged CrowdStrike Falcon endpoint-security integration covering the
detection → investigation → response → intelligence loop, designed as a
best-in-class building block for an agentic security team. (Falcon Next-Gen
SIEM - LogScale searches, correlation rules, NGSIEM detections, saved queries
and lookup files - is its own provider, ``crowdstrike_ngsiem.py``, sharing
this module's cloud resolution and token exchange.)

Tool categories (30 tools):
  - Detections / Alerts (Alerts API v2/v3): list, get, triage (status,
    assignment, comment, tag)
  - Incidents (CrowdScore): list, get, update (status/tag/name/assign),
    environment CrowdScore, behaviors
  - Hosts / Devices: list, get, network-contain / lift-containment, host groups
  - Vulnerabilities (Spotlight): combined vulnerability search with CVE +
    host context
  - Custom IOC management (IOC API): list, create, update, delete indicators
  - Threat Intelligence (Falcon Intel): adversaries (actors), intel
    indicators, finished intelligence reports (+ their PDF as a file)
  - Falcon Intelligence Sandbox: submit URL/hash/file of the run for
    detonation, fetch the report with its artefacts, query reports, store an
    artefact (IOC packs, PCAP, memory strings, screenshots) or a sample (only
    as CrowdStrike's password-protected ZIP) as a file of the run
  - Real Time Response (RTR): run a read-only live-response command on a host
    (init → execute → poll → close), restricted to a safe read-only allowlist

Authentication is **OAuth2 client credentials** (Falcon API key). The
integration stores a client ID + client secret + cloud region; the compound
``base_url||client_id||client_secret`` token is assembled by
``integration_tools._get_token`` and exchanged for a short-lived bearer token
(~30 min) via the cloud's ``/oauth2/token`` endpoint. Tokens are cached for
the lifetime of the tool set so repeated tool calls within one agentic loop
reuse a single token.
"""

from __future__ import annotations

import asyncio
import logging
import time
import zlib
from datetime import UTC, datetime
from typing import Any

import httpx

from app.core.egress import egress_client
from app.core.utils import format_exception_message
from app.integrations.file_delivery import (
    PersistFileFn,
    deliver_file,
    deliver_live_artefact,
    fetch_file,
    read_pipe_file,
)
from app.integrations.http import (
    format_entity_list,
    parse_compound_token,
    safe_request,
)
from app.services.branding import get_cached_branding
from app.tools.base import CallableToolExecutor, FileContext, ToolDefinition, ToolResult

logger = logging.getLogger(__name__)

_PROVIDER = "CrowdStrike Falcon"
_DEFAULT_TIMEOUT = 30
_RTR_TIMEOUT = 60
_TOKEN_SAFETY_WINDOW = 60  # refresh this many seconds before the token expires

# ---------------------------------------------------------------------------
# Cloud regions – the API base URL depends on which Falcon cloud the customer's
# CID lives in (https://developer.crowdstrike.com/sdks/python/configuration/).
# ---------------------------------------------------------------------------
_CLOUD_BASE_URLS: dict[str, str] = {
    "us-1": "https://api.crowdstrike.com",
    "us1": "https://api.crowdstrike.com",
    "us": "https://api.crowdstrike.com",
    "us-2": "https://api.us-2.crowdstrike.com",
    "us2": "https://api.us-2.crowdstrike.com",
    "eu-1": "https://api.eu-1.crowdstrike.com",
    "eu1": "https://api.eu-1.crowdstrike.com",
    "eu": "https://api.eu-1.crowdstrike.com",
    "us-gov-1": "https://api.laggar.gcw.crowdstrike.com",
    "usgov1": "https://api.laggar.gcw.crowdstrike.com",
    "gov1": "https://api.laggar.gcw.crowdstrike.com",
    "us-gov-2": "https://api.us-gov-2.crowdstrike.mil",
    "usgov2": "https://api.us-gov-2.crowdstrike.mil",
    "gov2": "https://api.us-gov-2.crowdstrike.mil",
}
_DEFAULT_BASE_URL = "https://api.crowdstrike.com"

_AUTH_ERROR = (
    "Error: Failed to authenticate with CrowdStrike Falcon. Check that the API "
    "client ID and secret are correct, that the integration's cloud region "
    "matches your tenant (US-1 / US-2 / EU-1 / US-GOV-1 / US-GOV-2), and that "
    "the API client has the required scopes for this operation."
)

_SEVERITY_ICONS: dict[str, str] = {
    "critical": "🔴",
    "high": "🔴",
    "medium": "🟠",
    "low": "🟡",
    "informational": "🔵",
    "none": "⚪",
}

# Incident status is a numeric code in the API; map to/from friendly labels.
_INCIDENT_STATUS_TO_CODE: dict[str, str] = {
    "new": "20",
    "reopened": "25",
    "in_progress": "30",
    "closed": "40",
}
_INCIDENT_CODE_TO_LABEL: dict[str, str] = {
    "20": "New",
    "25": "Reopened",
    "30": "In Progress",
    "40": "Closed",
}

# Falcon Intelligence Sandbox environment IDs by friendly name.
_SANDBOX_ENVIRONMENTS: dict[str, int] = {
    "windows11": 400,
    "windows10": 160,
    "windows7": 110,
    "linux": 300,
    "ubuntu": 300,
    "android": 200,
}

# Real Time Response read-only base commands that are safe to expose to the
# agent. Active-responder / admin commands (put, run, runscript, rm, …) are
# intentionally excluded so the agent can investigate but never modify a host.
_RTR_READONLY_COMMANDS: frozenset[str] = frozenset(
    {
        "cat",
        "cd",
        "env",
        "eventlog",
        "filehash",
        "getsid",
        "help",
        "history",
        "ipconfig",
        "ls",
        "mount",
        "netstat",
        "ps",
        "reg",
        "users",
    }
)

# Falcon Sandbox artefacts of a full report (``GET /falconx/entities/reports/v1``),
# downloaded with ``GET /falconx/entities/artifacts/v1``: artifact -> (report
# field, file name suffix, content type). The IOC packs sit on the report,
# the others on its ``sandbox`` entry.
_SANDBOX_REPORT_ARTIFACTS: dict[str, tuple[str, str, str]] = {
    f"ioc_{scope}_{fmt}": (
        f"ioc_report_{scope}_{fmt}_artifact_id",
        f"iocs-{scope}.{'csv' if fmt == 'csv' else f'{fmt}.json'}",
        "text/csv" if fmt == "csv" else "application/json",
    )
    for scope in ("strict", "broad")
    for fmt in ("csv", "json", "stix", "maec")
}
_SANDBOX_ENTRY_ARTIFACTS: dict[str, tuple[str, str, str]] = {
    "pcap": ("pcap_report_artifact_id", "traffic.pcap", "application/vnd.tcpdump.pcap"),
    "memory_strings": ("memory_strings_artifact_id", "memory-strings.txt", "text/plain"),
    "screenshot": ("screenshots_artifact_ids", "screenshot.png", "image/png"),
}
_SANDBOX_ARTIFACTS = {**_SANDBOX_REPORT_ARTIFACTS, **_SANDBOX_ENTRY_ARTIFACTS}
_MAX_DOWNLOAD_BYTES = 100 * 1024 * 1024
_DOWNLOAD_TIMEOUT = 120


# ---------------------------------------------------------------------------
# Cloud + auth helpers
# ---------------------------------------------------------------------------


def resolve_base_url(cloud: str) -> str:
    """Resolve a cloud region short-name (or raw URL) to an API base URL.

    Accepts the documented short names (``us-1``, ``us-2``, ``eu-1``,
    ``us-gov-1``, ``us-gov-2`` and common aliases) or a full ``https://`` URL.
    Falls back to the US-1 commercial cloud when the value is empty/unknown.

    Only the cloud short-name lookup is case-insensitive; a raw URL is returned
    with its original case preserved so a case-sensitive path component isn't
    silently mangled. A raw ``http://`` URL is upgraded to ``https://`` because
    the OAuth2 token exchange carries the API client secret and must never be
    sent in cleartext.
    """
    value = (cloud or "").strip()
    if not value:
        return _DEFAULT_BASE_URL
    lowered = value.lower()
    if lowered.startswith("https://"):
        return value.rstrip("/")
    if lowered.startswith("http://"):
        logger.warning(
            "CrowdStrike base URL was supplied over http://; upgrading to "
            "https:// so the API client secret is not sent in cleartext."
        )
        return ("https://" + value[len("http://") :]).rstrip("/")
    return _CLOUD_BASE_URLS.get(lowered, _DEFAULT_BASE_URL)


async def _request_token_with_ttl(
    base_url: str, client_id: str, client_secret: str
) -> tuple[str | None, int]:
    """Exchange client credentials for a Falcon OAuth2 bearer token.

    Returns ``(access_token, expires_in_seconds)`` on success, or
    ``(None, 0)`` on failure. The bearer token is short-lived (~30 min).
    """
    url = f"{base_url}/oauth2/token"
    try:
        async with egress_client(timeout=20) as client:
            resp = await client.post(
                url,
                data={"client_id": client_id, "client_secret": client_secret},
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Accept": "application/json",
                },
            )
            if resp.is_success:
                data = resp.json()
                return data.get("access_token"), int(data.get("expires_in") or 1799)
            logger.warning(
                "CrowdStrike token request failed (%s): %s",
                resp.status_code,
                resp.text[:300],
            )
    except httpx.HTTPError as exc:
        logger.warning("CrowdStrike token request error: %s", format_exception_message(exc))
    return None, 0


async def request_falcon_token(
    base_url: str, client_id: str, client_secret: str
) -> tuple[str | None, int]:
    """Public form of the Falcon token exchange, shared by the NGSIEM provider.

    Same contract as ``_request_token_with_ttl``: ``(access_token,
    expires_in_seconds)``, or ``(None, 0)`` on failure. Looked up at call time
    so a test that patches ``_request_token_with_ttl`` still applies.
    """
    return await _request_token_with_ttl(base_url, client_id, client_secret)


def _headers(access_token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


async def validate_connection(
    base_url: str, client_id: str, client_secret: str
) -> tuple[bool, str]:
    """Verify Falcon credentials by performing an OAuth2 token exchange.

    Returns ``(ok, message)``. A successful token exchange against the tenant's
    cloud confirms the client ID/secret are valid and the cloud region matches.
    Used by the integration connectivity test.
    """
    if not client_id or not client_secret:
        return False, "Missing API client ID or secret."
    token, _ = await _request_token_with_ttl(base_url, client_id, client_secret)
    if token:
        return True, "CrowdStrike Falcon is connected and authenticated."
    return False, (
        "Authentication failed — check the client ID/secret and confirm the "
        "cloud region matches your Falcon tenant."
    )


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def _sev(severity: str | None) -> str:
    return _SEVERITY_ICONS.get((severity or "").lower(), "⚪")


def _trunc(text: Any, max_len: int = 300) -> str:
    if not text:
        return ""
    text = str(text)
    return text[:max_len] + ("…" if len(text) > max_len else "")


def _dt(value: str | None) -> str:
    """Trim an ISO timestamp to second precision for display."""
    return (value or "")[:19]


def _ts(value: Any) -> str:
    """Render a Falcon timestamp for display.

    Falcon Intelligence dates (actor/indicator/report) are Unix epoch seconds,
    while most platform timestamps are ISO strings. Normalise both to a short
    human-readable date.
    """
    if value in (None, "", 0):
        return "N/A"
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value, tz=UTC).strftime("%Y-%m-%d")
        except (OverflowError, OSError, ValueError):
            return str(value)
    return str(value)[:19]


def _fql_quote(value: str) -> str:
    """Escape a value for safe inclusion inside an FQL single-quoted literal.

    FQL string literals are wrapped in single quotes (``field:'value'``). A
    literal backslash or single quote in the value would otherwise terminate
    the literal early or allow filter injection, so both are backslash-escaped.
    """
    return value.replace("\\", "\\\\").replace("'", "\\'")


def _fql_and(parts: list[str]) -> str:
    """Join FQL clauses with the ``+`` (logical AND) operator."""
    return "+".join(p for p in parts if p)


def _resources(data: dict | None) -> list:
    """Return the ``resources`` list from a Falcon response envelope."""
    if not isinstance(data, dict):
        return []
    return data.get("resources") or []


def _envelope_error(data: dict | None) -> str:
    """Return a human-readable string from a Falcon ``errors`` array, if any."""
    if not isinstance(data, dict):
        return ""
    errors = data.get("errors") or []
    if not errors:
        return ""
    msgs = []
    for e in errors:
        if isinstance(e, dict):
            msgs.append(str(e.get("message") or e.get("code") or e))
    return "; ".join(msgs)[:400]


# ---------------------------------------------------------------------------
# Tool builder
# ---------------------------------------------------------------------------


def build_tools(
    token: str,
    executor: CallableToolExecutor,
    persist_file_fn: PersistFileFn | None = None,
    **_kwargs: Any,
) -> list[ToolDefinition]:
    """Build CrowdStrike Falcon tools.

    ``token`` is the compound ``base_url||client_id||client_secret`` assembled
    by ``integration_tools._get_token`` from the integration's stored API
    client credentials and cloud region. ``persist_file_fn`` (injected by
    ``integration_tools``) stores a downloaded report, artefact or sample as a
    file of the run.
    """
    base_url, client_id, client_secret = parse_compound_token(token, 3)
    base_url = (base_url or _DEFAULT_BASE_URL).rstrip("/")

    # Bearer-token cache shared across every tool call in this tool set, so a
    # single agentic loop reuses one token instead of re-authenticating per
    # call (CrowdStrike rate-limits the token endpoint).
    _token_state: dict[str, Any] = {"token": None, "exp": 0.0}

    async def _token(*, force: bool = False) -> str | None:
        now = time.monotonic()
        cached = _token_state["token"]
        if not force and cached and _token_state["exp"] > now:
            return cached
        access, ttl = await _request_token_with_ttl(base_url, client_id, client_secret)
        if access:
            _token_state["token"] = access
            _token_state["exp"] = now + max(ttl - _TOKEN_SAFETY_WINDOW, 30)
        return access

    async def _api(
        method: str,
        path: str,
        action: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any | None = None,
        timeout: int = _DEFAULT_TIMEOUT,
    ) -> tuple[dict | None, str | None]:
        """Authenticated request helper returning ``(json, error)``.

        Acquires (cached) bearer token, performs the request via the shared
        ``safe_request`` (rate-limit aware), and parses the JSON envelope.
        """
        access_token = await _token()
        if not access_token:
            return None, _AUTH_ERROR
        async with egress_client(timeout=timeout) as client:
            resp, err = await safe_request(
                client,
                method,
                f"{base_url}{path}",
                _PROVIDER,
                action,
                headers=_headers(access_token),
                params=params,
                json=json,
            )
            if err:
                return None, err
            try:
                return resp.json(), None
            except Exception:
                # Empty body (e.g. 202/204) – treat as success with no payload.
                return {}, None

    async def _download(
        path: str, action: str, *, params: dict[str, Any]
    ) -> tuple[bytes | None, str | None]:
        """Authenticated binary GET returning ``(bytes, error)``.

        The artifacts endpoint serves gzip (``Accept-Encoding: gzip``); a body
        still gzipped after transport decoding is inflated here.
        """
        access_token = await _token()
        if not access_token:
            return None, _AUTH_ERROR
        async with egress_client(timeout=_DOWNLOAD_TIMEOUT) as client:
            fetched = await fetch_file(
                client,
                "get",
                f"{base_url}{path}",
                provider=_PROVIDER,
                action=action,
                max_bytes=_MAX_DOWNLOAD_BYTES,
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Accept": "*/*",
                    "Accept-Encoding": "gzip",
                },
                params=params,
                follow_redirects=True,
            )
        if isinstance(fetched, str):
            return None, fetched
        data = fetched.data
        if data[:2] == b"\x1f\x8b":
            try:
                inflater = zlib.decompressobj(16 + zlib.MAX_WBITS)
                data = inflater.decompress(data, _MAX_DOWNLOAD_BYTES + 1)
            except zlib.error:
                pass
        if len(data) > _MAX_DOWNLOAD_BYTES:
            return None, (
                f"Error: the file is over the {_MAX_DOWNLOAD_BYTES // 1024 // 1024} MB "
                "download limit."
            )
        return data, None

    # ══════════════════════════════════════════════════════════════════════
    # 0. DETECTIONS / ALERTS (Alerts API)
    # ══════════════════════════════════════════════════════════════════════

    async def list_crowdstrike_alerts(
        status: str = "",
        severity: str = "",
        filter_query: str = "",
        count: int = 25,
    ) -> str:
        """List Falcon detections/alerts, optionally filtered by status/severity."""
        clauses: list[str] = []
        if status:
            clauses.append(f"status:'{_fql_quote(status)}'")
        if severity:
            clauses.append(f"severity_name:'{_fql_quote(severity)}'")
        if filter_query:
            clauses.append(f"({filter_query})")
        params: dict[str, Any] = {
            "limit": min(count, 100),
            "sort": "created_timestamp|desc",
        }
        fql = _fql_and(clauses)
        if fql:
            params["filter"] = fql

        data, err = await _api(
            "get", "/alerts/queries/alerts/v2", "list alerts", params=params
        )
        if err:
            return err
        ids = _resources(data)
        if not ids:
            return "No alerts found matching the filters."

        detail, derr = await _api(
            "post",
            "/alerts/entities/alerts/v2",
            "get alert details",
            json={"composite_ids": ids[: min(count, 100)]},
        )
        if derr:
            return derr
        alerts = _resources(detail)

        def _fmt(a: dict) -> str:
            sev = a.get("severity_name") or "?"
            dev = a.get("device") or {}
            host = dev.get("hostname") or a.get("hostname") or "?"
            return (
                f"  {_sev(sev)} [{sev}] {a.get('status', '?')} — "
                f"{a.get('display_name') or a.get('name', 'Unnamed')}\n"
                f"    id: {a.get('composite_id') or a.get('id', '?')}\n"
                f"    Host: {host} | Tactic: {a.get('tactic', 'N/A')} | "
                f"Technique: {a.get('technique', 'N/A')} | "
                f"Assigned: {a.get('assigned_to_name') or 'Unassigned'} | "
                f"Created: {_dt(a.get('created_timestamp'))}"
            )

        return format_entity_list(alerts, _fmt, noun="alert", total=len(ids))

    async def get_crowdstrike_alert(alert_id: str) -> str:
        """Get full details of a Falcon alert by its composite ID."""
        data, err = await _api(
            "post",
            "/alerts/entities/alerts/v2",
            f"get alert {alert_id}",
            json={"composite_ids": [alert_id]},
        )
        if err:
            return err
        alerts = _resources(data)
        if not alerts:
            return f"Alert {alert_id} not found."
        a = alerts[0]
        dev = a.get("device") or {}
        lines = [
            f"Alert: {a.get('display_name') or a.get('name', 'Unnamed')}",
            f"ID: {a.get('composite_id') or a.get('id', '?')}",
            f"Severity: {_sev(a.get('severity_name'))} {a.get('severity_name', '?')} "
            f"({a.get('severity', '?')})",
            f"Status: {a.get('status', '?')} | "
            f"Assigned: {a.get('assigned_to_name') or 'Unassigned'}",
            f"Tactic: {a.get('tactic', 'N/A')} ({a.get('tactic_id', 'N/A')}) | "
            f"Technique: {a.get('technique', 'N/A')} ({a.get('technique_id', 'N/A')})",
            f"Objective: {a.get('objective', 'N/A')} | "
            f"Pattern: {a.get('pattern_disposition_description') or a.get('pattern_id', 'N/A')}",
            f"Product: {a.get('product', '?')} | "
            f"Confidence: {a.get('confidence', 'N/A')}",
            f"Host: {dev.get('hostname', '?')} ({dev.get('platform_name', '?')} "
            f"{dev.get('os_version', '')}) | Device ID: {dev.get('device_id', '?')}",
        ]
        if a.get("filename"):
            lines.append(f"File: {a.get('filename')} | SHA256: {a.get('sha256', 'N/A')}")
        if a.get("cmdline"):
            lines.append(f"Command line: {_trunc(a.get('cmdline'), 400)}")
        if a.get("description"):
            lines.append(f"\nDescription:\n{_trunc(a.get('description'), 600)}")
        lines.append(
            f"\nCreated: {_dt(a.get('created_timestamp'))} | "
            f"Updated: {_dt(a.get('updated_timestamp'))}"
        )
        return "\n".join(lines)

    async def update_crowdstrike_alert(
        alert_id: str,
        *,
        status: str = "",
        assigned_to: str = "",
        comment: str = "",
        add_tag: str = "",
    ) -> str:
        """Triage a Falcon alert: set status, assign, comment, or add a tag.

        ``status`` is one of new, in_progress, reopened, closed.
        ``assigned_to`` is the analyst's user ID (email/UPN).
        """
        action_parameters: list[dict[str, str]] = []
        if status:
            action_parameters.append({"name": "update_status", "value": status})
        if assigned_to:
            action_parameters.append(
                {"name": "assign_to_user_id", "value": assigned_to}
            )
        if comment:
            action_parameters.append({"name": "append_comment", "value": comment})
        if add_tag:
            action_parameters.append({"name": "add_tag", "value": add_tag})
        if not action_parameters:
            return (
                "Error: nothing to update — provide at least one of status, "
                "assigned_to, comment, or add_tag."
            )

        data, err = await _api(
            "patch",
            "/alerts/entities/alerts/v3",
            f"update alert {alert_id}",
            json={"composite_ids": [alert_id], "action_parameters": action_parameters},
        )
        if err:
            return err
        env_err = _envelope_error(data)
        if env_err:
            return f"Error updating alert {alert_id}: {env_err}"
        applied = ", ".join(p["name"] for p in action_parameters)
        return f"Alert {alert_id} updated ({applied})."

    # ══════════════════════════════════════════════════════════════════════
    # 1. INCIDENTS (CrowdScore)
    # ══════════════════════════════════════════════════════════════════════

    async def list_crowdstrike_incidents(
        status: str = "",
        filter_query: str = "",
        count: int = 25,
    ) -> str:
        """List CrowdScore incidents, optionally filtered by status."""
        clauses: list[str] = []
        if status:
            code = _INCIDENT_STATUS_TO_CODE.get(status.lower().replace(" ", "_"))
            if code:
                clauses.append(f"status:'{code}'")
            else:
                clauses.append(f"state:'{_fql_quote(status)}'")
        if filter_query:
            clauses.append(f"({filter_query})")
        params: dict[str, Any] = {"limit": min(count, 100), "sort": "start|desc"}
        fql = _fql_and(clauses)
        if fql:
            params["filter"] = fql

        data, err = await _api(
            "get", "/incidents/queries/incidents/v1", "list incidents", params=params
        )
        if err:
            return err
        ids = _resources(data)
        if not ids:
            return "No incidents found matching the filters."

        detail, derr = await _api(
            "post",
            "/incidents/entities/incidents/GET/v1",
            "get incident details",
            json={"ids": ids[: min(count, 100)]},
        )
        if derr:
            return derr
        incidents = _resources(detail)

        def _fmt(inc: dict) -> str:
            score = inc.get("fine_score")
            sev = round(score / 10) if isinstance(score, (int, float)) else "?"
            status_label = _INCIDENT_CODE_TO_LABEL.get(
                str(inc.get("status", "")), inc.get("state", "?")
            )
            hosts = inc.get("hosts") or []
            tactics = ", ".join(inc.get("tactics", []) or []) or "N/A"
            return (
                f"  #{inc.get('incident_id', '?')} [score {sev}] {status_label} — "
                f"{inc.get('name') or 'Untitled'}\n"
                f"    Hosts: {len(hosts)} | Tactics: {tactics} | "
                f"Assigned: {inc.get('assigned_to_name') or 'Unassigned'} | "
                f"Start: {_dt(inc.get('start'))}"
            )

        return format_entity_list(
            incidents, _fmt, noun="incident", total=len(ids)
        )

    async def get_crowdstrike_incident(incident_id: str) -> str:
        """Get full details of a CrowdScore incident, including hosts and TTPs."""
        data, err = await _api(
            "post",
            "/incidents/entities/incidents/GET/v1",
            f"get incident {incident_id}",
            json={"ids": [incident_id]},
        )
        if err:
            return err
        incidents = _resources(data)
        if not incidents:
            return f"Incident {incident_id} not found."
        inc = incidents[0]
        score = inc.get("fine_score")
        sev = round(score / 10) if isinstance(score, (int, float)) else "?"
        status_label = _INCIDENT_CODE_TO_LABEL.get(
            str(inc.get("status", "")), inc.get("state", "?")
        )
        hosts = inc.get("hosts") or []
        lines = [
            f"Incident #{inc.get('incident_id', '?')}: {inc.get('name') or 'Untitled'}",
            f"CrowdScore severity: {sev} (fine_score {inc.get('fine_score', '?')})",
            f"Status: {status_label} | State: {inc.get('state', '?')}",
            f"Assigned to: {inc.get('assigned_to_name') or 'Unassigned'}",
            f"Tactics: {', '.join(inc.get('tactics', []) or []) or 'N/A'}",
            f"Techniques: {', '.join(inc.get('techniques', []) or []) or 'N/A'}",
            f"Objectives: {', '.join(inc.get('objectives', []) or []) or 'N/A'}",
            f"Tags: {', '.join(inc.get('tags', []) or []) or 'None'}",
            f"Start: {_dt(inc.get('start'))} | End: {_dt(inc.get('end'))}",
        ]
        if inc.get("description"):
            lines.append(f"\nDescription:\n{_trunc(inc.get('description'), 600)}")
        if hosts:
            lines.append(f"\nHosts ({len(hosts)}):")
            for h in hosts[:25]:
                lines.append(
                    f"  - {h.get('hostname', '?')} ({h.get('platform_name', '?')}) "
                    f"| device_id: {h.get('device_id', '?')} "
                    f"| {h.get('local_ip', 'N/A')}"
                )
        return "\n".join(lines)

    async def update_crowdstrike_incident(
        incident_id: str,
        *,
        status: str = "",
        add_tag: str = "",
        name: str = "",
        description: str = "",
    ) -> str:
        """Update a CrowdScore incident: set status, add a tag, rename, or describe.

        ``status`` is one of new, reopened, in_progress, closed.
        """
        action_parameters: list[dict[str, str]] = []
        if status:
            code = _INCIDENT_STATUS_TO_CODE.get(status.lower().replace(" ", "_"))
            if not code:
                return (
                    f"Error: invalid status '{status}'. Use one of: "
                    f"{', '.join(_INCIDENT_STATUS_TO_CODE)}."
                )
            action_parameters.append({"name": "update_status", "value": code})
        if add_tag:
            action_parameters.append({"name": "add_tag", "value": add_tag})
        if name:
            action_parameters.append({"name": "update_name", "value": name})
        if description:
            action_parameters.append(
                {"name": "update_description", "value": description}
            )
        if not action_parameters:
            return (
                "Error: nothing to update — provide at least one of status, "
                "add_tag, name, or description."
            )

        data, err = await _api(
            "post",
            "/incidents/entities/incident-actions/v1",
            f"update incident {incident_id}",
            json={"action_parameters": action_parameters, "ids": [incident_id]},
        )
        if err:
            return err
        env_err = _envelope_error(data)
        if env_err:
            return f"Error updating incident {incident_id}: {env_err}"
        applied = ", ".join(p["name"] for p in action_parameters)
        return f"Incident {incident_id} updated ({applied})."

    async def get_crowdstrike_crowdscore() -> str:
        """Get the environment-wide CrowdScore (current security risk score)."""
        data, err = await _api(
            "get",
            "/incidents/combined/crowdscores/v1",
            "get crowdscore",
            params={"limit": 1, "sort": "timestamp|desc"},
        )
        if err:
            return err
        scores = _resources(data)
        if not scores:
            return "No CrowdScore data available."
        s = scores[0]
        return (
            f"CrowdScore: {s.get('score', '?')} "
            f"(adjusted: {s.get('adjusted_score', 'N/A')}) "
            f"as of {_dt(s.get('timestamp'))}"
        )

    async def list_crowdstrike_behaviors(
        filter_query: str = "", count: int = 25
    ) -> str:
        """List incident behaviors (detected TTP events), optional FQL filter."""
        params: dict[str, Any] = {"limit": min(count, 100), "sort": "timestamp|desc"}
        if filter_query:
            params["filter"] = filter_query

        data, err = await _api(
            "get",
            "/incidents/queries/behaviors/v1",
            "list behaviors",
            params=params,
        )
        if err:
            return err
        ids = _resources(data)
        if not ids:
            return "No behaviors found matching the filters."

        detail, derr = await _api(
            "post",
            "/incidents/entities/behaviors/GET/v1",
            "get behavior details",
            json={"ids": ids[: min(count, 100)]},
        )
        if derr:
            return derr
        behaviors = _resources(detail)

        def _fmt(b: dict) -> str:
            return (
                f"  {b.get('display_name') or b.get('behavior_id', '?')} — "
                f"Tactic: {b.get('tactic', 'N/A')} | "
                f"Technique: {b.get('technique', 'N/A')}\n"
                f"    Host: {b.get('hostname', '?')} | "
                f"cmdline: {_trunc(b.get('cmdline'), 120)} | "
                f"Timestamp: {_dt(b.get('timestamp'))}"
            )

        return format_entity_list(
            behaviors, _fmt, noun="behavior", total=len(ids)
        )

    # ══════════════════════════════════════════════════════════════════════
    # 2. HOSTS / DEVICES
    # ══════════════════════════════════════════════════════════════════════

    async def list_crowdstrike_hosts(
        hostname: str = "",
        platform: str = "",
        filter_query: str = "",
        count: int = 25,
    ) -> str:
        """List managed hosts (devices), filtered by hostname/platform/FQL."""
        clauses: list[str] = []
        if hostname:
            # FQL substring match: the wildcards belong *inside* the quoted
            # literal (``hostname:'*value*'``). A ``*`` placed before the
            # opening quote is a malformed Devices-API filter and breaks the
            # query.
            clauses.append(f"hostname:'*{_fql_quote(hostname)}*'")
        if platform:
            clauses.append(f"platform_name:'{_fql_quote(platform)}'")
        if filter_query:
            clauses.append(f"({filter_query})")
        params: dict[str, Any] = {
            "limit": min(count, 100),
            "sort": "last_seen|desc",
        }
        fql = _fql_and(clauses)
        if fql:
            params["filter"] = fql

        data, err = await _api(
            "get", "/devices/queries/devices/v1", "list hosts", params=params
        )
        if err:
            return err
        ids = _resources(data)
        if not ids:
            return "No hosts found matching the filters."

        detail, derr = await _api(
            "post",
            "/devices/entities/devices/v2",
            "get host details",
            json={"ids": ids[: min(count, 100)]},
        )
        if derr:
            return derr
        hosts = _resources(detail)

        def _fmt(h: dict) -> str:
            return (
                f"  {h.get('hostname', '?')} [{h.get('platform_name', '?')} "
                f"{h.get('os_version', '')}] — {h.get('status', '?')}\n"
                f"    device_id: {h.get('device_id', '?')} | "
                f"IP: {h.get('local_ip', 'N/A')} (ext {h.get('external_ip', 'N/A')}) | "
                f"Last seen: {_dt(h.get('last_seen'))}"
            )

        return format_entity_list(hosts, _fmt, noun="host", total=len(ids))

    async def get_crowdstrike_host(device_id: str) -> str:
        """Get full details of a managed host by device ID (AID)."""
        data, err = await _api(
            "post",
            "/devices/entities/devices/v2",
            f"get host {device_id}",
            json={"ids": [device_id]},
        )
        if err:
            return err
        hosts = _resources(data)
        if not hosts:
            return f"Host {device_id} not found."
        h = hosts[0]
        policies = h.get("device_policies") or {}
        prevention = (policies.get("prevention") or {}).get("policy_id", "N/A")
        lines = [
            f"Host: {h.get('hostname', '?')}",
            f"Device ID (AID): {h.get('device_id', '?')}",
            f"Platform: {h.get('platform_name', '?')} {h.get('os_version', '')} "
            f"({h.get('product_type_desc', '?')})",
            f"Containment status: {h.get('status', '?')}",
            f"Local IP: {h.get('local_ip', 'N/A')} | "
            f"External IP: {h.get('external_ip', 'N/A')} | "
            f"MAC: {h.get('mac_address', 'N/A')}",
            f"Manufacturer: {h.get('system_manufacturer', 'N/A')} "
            f"{h.get('system_product_name', '')}",
            f"Serial: {h.get('serial_number', 'N/A')} | "
            f"Agent: {h.get('agent_version', 'N/A')} | "
            f"RFM: {h.get('reduced_functionality_mode', 'N/A')}",
            f"OU: {', '.join(h.get('ou', []) or []) or 'N/A'} | "
            f"Site: {h.get('site_name', 'N/A')}",
            f"Tags: {', '.join(h.get('tags', []) or []) or 'None'}",
            f"Prevention policy: {prevention}",
            f"First seen: {_dt(h.get('first_seen'))} | "
            f"Last seen: {_dt(h.get('last_seen'))}",
        ]
        return "\n".join(lines)

    async def _host_action(device_id: str, action_name: str, verb: str) -> str:
        data, err = await _api(
            "post",
            "/devices/entities/devices-actions/v2",
            f"{verb} host {device_id}",
            params={"action_name": action_name},
            json={"ids": [device_id]},
        )
        if err:
            return err
        env_err = _envelope_error(data)
        if env_err:
            return f"Error: failed to {verb} host {device_id}: {env_err}"
        return f"Host {device_id}: {verb} request accepted."

    async def contain_crowdstrike_host(device_id: str) -> str:
        """Network-contain a host (isolate it from the network except CrowdStrike)."""
        return await _host_action(device_id, "contain", "network-contain")

    async def lift_crowdstrike_host_containment(device_id: str) -> str:
        """Lift network containment on a host (restore normal connectivity)."""
        return await _host_action(device_id, "lift_containment", "lift containment on")

    async def list_crowdstrike_host_groups(count: int = 25) -> str:
        """List host groups defined in the Falcon tenant."""
        data, err = await _api(
            "get",
            "/devices/combined/host-groups/v1",
            "list host groups",
            params={"limit": min(count, 100), "sort": "name|asc"},
        )
        if err:
            return err
        groups = _resources(data)

        def _fmt(g: dict) -> str:
            return (
                f"  {g.get('name', 'Unnamed')} [{g.get('group_type', '?')}] — "
                f"id: {g.get('id', '?')}\n"
                f"    {_trunc(g.get('description'), 150) or 'No description'} | "
                f"Rule: {_trunc(g.get('assignment_rule'), 120) or 'N/A'}"
            )

        return format_entity_list(groups, _fmt, noun="host group")

    # ══════════════════════════════════════════════════════════════════════
    # 3. VULNERABILITIES (Spotlight)
    # ══════════════════════════════════════════════════════════════════════

    async def list_crowdstrike_vulnerabilities(
        filter_query: str = "",
        count: int = 25,
    ) -> str:
        """List Spotlight vulnerabilities with CVE + host context.

        Defaults to open vulnerabilities. Pass a raw FQL ``filter_query`` to
        scope further (e.g. ``cve.severity:'CRITICAL'`` or
        ``cve.id:'CVE-2024-3094'``).
        """
        fql = filter_query or "status:'open'"
        data, err = await _api(
            "get",
            "/spotlight/combined/vulnerabilities/v1",
            "list vulnerabilities",
            params={
                "filter": fql,
                "limit": min(count, 100),
                "sort": "created_timestamp|desc",
                "facet": ["cve", "host_info"],
            },
        )
        if err:
            return err
        vulns = _resources(data)

        def _fmt(v: dict) -> str:
            cve = v.get("cve") or {}
            host = v.get("host_info") or {}
            return (
                f"  {_sev(cve.get('severity'))} {cve.get('id', 'N/A')} "
                f"[{cve.get('severity', '?')}] base score "
                f"{cve.get('base_score', 'N/A')} — {v.get('status', '?')}\n"
                f"    Host: {host.get('hostname', '?')} "
                f"({host.get('platform', '?')}) | "
                f"Exploit: {cve.get('exploit_status', 'N/A')} | "
                f"id: {v.get('id', '?')}"
            )

        return format_entity_list(vulns, _fmt, noun="vulnerability")

    # ══════════════════════════════════════════════════════════════════════
    # 4. CUSTOM IOC MANAGEMENT
    # ══════════════════════════════════════════════════════════════════════

    async def list_crowdstrike_iocs(
        ioc_type: str = "",
        value: str = "",
        filter_query: str = "",
        count: int = 25,
    ) -> str:
        """List custom IOCs (indicators) configured in the Falcon tenant."""
        clauses: list[str] = []
        if ioc_type:
            clauses.append(f"type:'{_fql_quote(ioc_type)}'")
        if value:
            clauses.append(f"value:'{_fql_quote(value)}'")
        if filter_query:
            clauses.append(f"({filter_query})")
        params: dict[str, Any] = {
            "limit": min(count, 100),
            "sort": "created_on|desc",
        }
        fql = _fql_and(clauses)
        if fql:
            params["filter"] = fql

        data, err = await _api(
            "get", "/iocs/combined/indicator/v1", "list IOCs", params=params
        )
        if err:
            return err
        iocs = _resources(data)

        def _fmt(i: dict) -> str:
            return (
                f"  {_sev(i.get('severity'))} [{i.get('type', '?')}] "
                f"{i.get('value', '?')} → action: {i.get('action', '?')}\n"
                f"    id: {i.get('id', '?')} | "
                f"Platforms: {', '.join(i.get('platforms', []) or []) or 'N/A'} | "
                f"Global: {i.get('applied_globally', '?')} | "
                f"Created: {_dt(i.get('created_on'))}"
            )

        return format_entity_list(iocs, _fmt, noun="IOC")

    async def create_crowdstrike_ioc(
        ioc_type: str,
        value: str,
        *,
        action: str = "detect",
        severity: str = "medium",
        platforms: str = "windows,mac,linux",
        description: str = "",
        applied_globally: bool = True,
        expiration: str = "",
    ) -> str:
        """Create a custom IOC (indicator) to detect/prevent on endpoints.

        ``ioc_type``: sha256, sha1, md5, domain, ipv4, ipv6.
        ``action``: detect, prevent, no_action, allow, prevent_no_ui.
        ``severity``: critical, high, medium, low, informational.
        ``platforms``: comma-separated subset of windows, mac, linux.
        """
        indicator: dict[str, Any] = {
            "type": ioc_type,
            "value": value,
            "action": action,
            "severity": severity,
            "applied_globally": applied_globally,
            "platforms": [p.strip() for p in platforms.split(",") if p.strip()],
            "description": description
            or f"Created via {get_cached_branding().name}",
        }
        if expiration:
            indicator["expiration"] = expiration

        data, err = await _api(
            "post",
            "/iocs/entities/indicators/v1",
            "create IOC",
            json={
                "comment": f"Created via {get_cached_branding().name}",
                "indicators": [indicator],
            },
        )
        if err:
            return err
        env_err = _envelope_error(data)
        if env_err:
            return f"Error creating IOC: {env_err}"
        created = _resources(data)
        new_id = created[0].get("id") if created else "?"
        return f"IOC created: {ioc_type} {value} (action: {action}, id: {new_id})"

    async def update_crowdstrike_ioc(
        ioc_id: str,
        *,
        action: str = "",
        severity: str = "",
        description: str = "",
        expiration: str = "",
    ) -> str:
        """Update an existing custom IOC by ID. Only provided fields are changed."""
        indicator: dict[str, Any] = {"id": ioc_id}
        if action:
            indicator["action"] = action
        if severity:
            indicator["severity"] = severity
        if description:
            indicator["description"] = description
        if expiration:
            indicator["expiration"] = expiration
        if len(indicator) == 1:
            return (
                "Error: nothing to update — provide at least one of action, "
                "severity, description, or expiration."
            )

        data, err = await _api(
            "patch",
            "/iocs/entities/indicators/v1",
            f"update IOC {ioc_id}",
            json={
                "comment": f"Updated via {get_cached_branding().name}",
                "indicators": [indicator],
            },
        )
        if err:
            return err
        env_err = _envelope_error(data)
        if env_err:
            return f"Error updating IOC {ioc_id}: {env_err}"
        return f"IOC {ioc_id} updated."

    async def delete_crowdstrike_ioc(ioc_id: str, comment: str = "") -> str:
        """Delete a custom IOC by ID."""
        data, err = await _api(
            "delete",
            "/iocs/entities/indicators/v1",
            f"delete IOC {ioc_id}",
            params={
                "ids": ioc_id,
                "comment": comment or f"Deleted via {get_cached_branding().name}",
            },
        )
        if err:
            return err
        env_err = _envelope_error(data)
        if env_err:
            return f"Error deleting IOC {ioc_id}: {env_err}"
        return f"IOC {ioc_id} deleted."

    # ══════════════════════════════════════════════════════════════════════
    # 5. THREAT INTELLIGENCE (Falcon Intel)
    # ══════════════════════════════════════════════════════════════════════

    async def list_crowdstrike_actors(
        query: str = "",
        filter_query: str = "",
        count: int = 10,
    ) -> str:
        """List CrowdStrike threat actors (adversaries) from Falcon Intelligence."""
        params: dict[str, Any] = {
            "limit": min(count, 50),
            "sort": "last_activity_date|desc",
        }
        if query:
            params["q"] = query
        if filter_query:
            params["filter"] = filter_query

        data, err = await _api(
            "get", "/intel/combined/actors/v1", "list actors", params=params
        )
        if err:
            return err
        actors = _resources(data)

        def _fmt(a: dict) -> str:
            origins = ", ".join(o.get("value", "") for o in a.get("origins", []) or [])
            industries = ", ".join(
                t.get("value", "") for t in a.get("target_industries", []) or []
            )
            return (
                f"  {a.get('name', 'Unknown')} (slug: {a.get('slug', '?')})\n"
                f"    Origins: {origins or 'N/A'} | "
                f"Target industries: {industries or 'N/A'}\n"
                f"    {_trunc(a.get('short_description'), 200)}"
            )

        return format_entity_list(actors, _fmt, noun="actor")

    async def get_crowdstrike_actor(actor_id: str) -> str:
        """Get full details of a threat actor by numeric ID or slug."""
        data, err = await _api(
            "get",
            "/intel/entities/actors/v1",
            f"get actor {actor_id}",
            params={"ids": actor_id, "fields": "__full__"},
        )
        if err:
            return err
        actors = _resources(data)
        if not actors:
            return f"Actor {actor_id} not found."
        a = actors[0]
        origins = ", ".join(o.get("value", "") for o in a.get("origins", []) or [])
        industries = ", ".join(
            t.get("value", "") for t in a.get("target_industries", []) or []
        )
        countries = ", ".join(
            c.get("value", "") for c in a.get("target_countries", []) or []
        )
        motivations = ", ".join(
            m.get("value", "") for m in a.get("motivations", []) or []
        )
        cap = a.get("capability") or {}
        lines = [
            f"Threat actor: {a.get('name', 'Unknown')} (slug: {a.get('slug', '?')})",
            f"Origins: {origins or 'N/A'}",
            f"Capability: {cap.get('value', 'N/A')}",
            f"Motivations: {motivations or 'N/A'}",
            f"Target industries: {industries or 'N/A'}",
            f"Target countries: {countries or 'N/A'}",
            f"First activity: {_ts(a.get('first_activity_date'))} | "
            f"Last activity: {_ts(a.get('last_activity_date'))}",
            f"Known as: {a.get('known_as', 'N/A')}",
        ]
        if a.get("description"):
            lines.append(f"\nDescription:\n{_trunc(a.get('description'), 800)}")
        return "\n".join(lines)

    async def list_crowdstrike_intel_indicators(
        filter_query: str = "",
        count: int = 25,
    ) -> str:
        """List Falcon Intelligence indicators (published threat IOCs)."""
        params: dict[str, Any] = {
            "limit": min(count, 100),
            "sort": "published_date|desc",
        }
        if filter_query:
            params["filter"] = filter_query

        data, err = await _api(
            "get",
            "/intel/combined/indicators/v1",
            "list intel indicators",
            params=params,
        )
        if err:
            return err
        indicators = _resources(data)

        def _fmt(i: dict) -> str:
            actors = ", ".join(i.get("actors", []) or []) or "N/A"
            families = ", ".join(i.get("malware_families", []) or []) or "N/A"
            return (
                f"  [{i.get('type', '?')}] {i.get('indicator', '?')} "
                f"(confidence: {i.get('malicious_confidence', '?')})\n"
                f"    Actors: {actors} | Malware: {families} | "
                f"Published: {_ts(i.get('published_date'))}"
            )

        return format_entity_list(indicators, _fmt, noun="intel indicator")

    async def list_crowdstrike_intel_reports(
        query: str = "",
        filter_query: str = "",
        count: int = 10,
    ) -> str:
        """List Falcon Intelligence finished intelligence reports."""
        params: dict[str, Any] = {
            "limit": min(count, 50),
            "sort": "created_date|desc",
        }
        if query:
            params["q"] = query
        if filter_query:
            params["filter"] = filter_query

        data, err = await _api(
            "get",
            "/intel/combined/reports/v1",
            "list intel reports",
            params=params,
        )
        if err:
            return err
        reports = _resources(data)

        def _fmt(r: dict) -> str:
            actors = ", ".join(act.get("name", "") for act in r.get("actors", []) or [])
            line = (
                f"  {r.get('name', 'Untitled')} [{r.get('type', {}).get('name', '?') if isinstance(r.get('type'), dict) else r.get('type', '?')}]\n"
                f"    id: {r.get('id', '?')} | slug: {r.get('slug', '?')} | "
                f"Actors: {actors or 'N/A'}\n"
                f"    {_trunc(r.get('short_description'), 200)} "
                f"(Created: {_ts(r.get('created_date'))})"
            )
            if r.get("url"):
                line += f"\n    Web URL: {r['url']}"
            return line

        out = format_entity_list(reports, _fmt, noun="report")
        if reports:
            out += "\n[download_crowdstrike_intel_report(report_id) stores a report's PDF as a file]"
        return out

    async def download_crowdstrike_intel_report(report_id: str) -> str | ToolResult:
        """Store the PDF of a Falcon Intelligence report as a file of the run."""
        report_id = str(report_id or "").strip()
        if not report_id:
            return "Error: report_id is required (from list_crowdstrike_intel_reports)."
        meta, err = await _api(
            "get",
            "/intel/entities/reports/v1",
            f"get intel report {report_id}",
            params={"ids": report_id},
        )
        if err:
            return err
        entities = _resources(meta)
        report = entities[0] if entities and isinstance(entities[0], dict) else {}
        data, err = await _download(
            "/intel/entities/report-files/v1",
            f"download intel report {report_id} PDF",
            params={"id": report_id},
        )
        if err:
            return err
        slug = report.get("slug") or report_id
        return await deliver_file(
            data=data,
            filename=f"{slug}.pdf",
            content_type="application/pdf",
            source=_PROVIDER,
            persist_file_fn=persist_file_fn,
            links=[
                ("Web URL", report.get("url")),
                ("Download URL", f"{base_url}/intel/entities/report-files/v1?id={report_id}"),
            ],
            details=[f"Report: {_trunc(report.get('name'), 200)}"] if report.get("name") else [],
        )

    # ══════════════════════════════════════════════════════════════════════
    # 6. FALCON INTELLIGENCE SANDBOX
    # ══════════════════════════════════════════════════════════════════════

    async def _upload_sample(
        data: bytes, filename: str, content_type: str
    ) -> tuple[str | None, str | None]:
        """Upload a sample (``POST /samples/entities/samples/v2``); ``(sha256, error)``."""
        access_token = await _token()
        if not access_token:
            return None, _AUTH_ERROR
        async with egress_client(timeout=_DOWNLOAD_TIMEOUT) as client:
            resp, err = await safe_request(
                client,
                "post",
                f"{base_url}/samples/entities/samples/v2",
                _PROVIDER,
                "upload the sample",
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Accept": "application/json",
                },
                data={"file_name": filename, "is_confidential": "true"},
                files={"sample": (filename, data, content_type)},
            )
        if err:
            return None, err
        try:
            uploaded = _resources(resp.json())
        except ValueError:
            uploaded = []
        sha = uploaded[0].get("sha256") if uploaded and isinstance(uploaded[0], dict) else None
        if not sha:
            return None, "Error: CrowdStrike accepted the upload but returned no SHA256."
        return str(sha), None

    async def submit_crowdstrike_sandbox_analysis(
        url: str = "",
        sha256: str = "",
        environment: str = "windows10",
        action_script: str = "default",
        file_id: str = "",
        file_context: FileContext | None = None,
    ) -> str | ToolResult:
        """Submit a URL, an existing sample (by SHA256) or a file of the run for sandbox detonation.

        ``environment`` is one of windows11, windows10, windows7, linux, android.
        A ``file_id`` is uploaded to the sample store first (confidential).
        Returns a submission ID — poll it with ``get_crowdstrike_sandbox_report``.
        """
        if sum(map(bool, (url, sha256, file_id))) != 1:
            return "Error: provide exactly one of url, sha256 or file_id to submit for analysis."
        if file_id:
            piped = await read_pipe_file(
                file_context, file_id, max_bytes=_MAX_DOWNLOAD_BYTES, target=_PROVIDER
            )
            if isinstance(piped, str):
                return ToolResult(tool_call_id="", content=piped, is_error=True)
            sample, name, content_type = piped
            sha256, err = await _upload_sample(sample, name, content_type)
            if err:
                return err
        env_id = _SANDBOX_ENVIRONMENTS.get(
            environment.lower().replace(" ", ""), 160
        )
        sandbox_entry: dict[str, Any] = {
            "environment_id": env_id,
            "action_script": action_script,
        }
        if url:
            sandbox_entry["url"] = url
        if sha256:
            sandbox_entry["sha256"] = sha256

        data, err = await _api(
            "post",
            "/falconx/entities/submissions/v1",
            "submit sandbox analysis",
            json={"sandbox": [sandbox_entry]},
        )
        if err:
            return err
        env_err = _envelope_error(data)
        if env_err:
            return f"Error submitting to sandbox: {env_err}"
        submissions = _resources(data)
        if not submissions:
            return "Sandbox submission accepted but no submission ID returned."
        sub = submissions[0]
        uploaded = f"Uploaded sample SHA256: {sha256}. " if file_id else ""
        return (
            f"{uploaded}Submitted to sandbox (environment: {environment}). "
            f"Submission ID: {sub.get('id', '?')} — state: {sub.get('state', 'created')}. "
            f"Use get_crowdstrike_sandbox_report with this ID to fetch the verdict."
        )

    async def _full_sandbox_report(report_id: str) -> tuple[dict | None, str | None]:
        """The full sandbox report (``GET /falconx/entities/reports/v1``)."""
        data, err = await _api(
            "get",
            "/falconx/entities/reports/v1",
            f"get sandbox report {report_id}",
            params={"ids": report_id},
        )
        if err:
            return None, err
        reports = _resources(data)
        return (reports[0] if reports and isinstance(reports[0], dict) else None), None

    def _sandbox_entry(report: dict) -> dict:
        entries = report.get("sandbox")
        return entries[0] if isinstance(entries, list) and entries and isinstance(entries[0], dict) else {}

    def _format_sandbox_artifacts(report: dict) -> list[str]:
        sandbox = _sandbox_entry(report)
        available = [name for name, (field, _, _) in _SANDBOX_REPORT_ARTIFACTS.items() if report.get(field)]
        available += [
            name
            for name, (field, _, _) in _SANDBOX_ENTRY_ARTIFACTS.items()
            if name != "screenshot" and sandbox.get(field)
        ]
        screenshots = sandbox.get("screenshots_artifact_ids") or []
        if isinstance(screenshots, list) and screenshots:
            available.append(f"screenshot (index 1-{len(screenshots)})")
        lines: list[str] = []
        if available:
            lines.append(
                "Artifacts (download_crowdstrike_sandbox_artifact): " + ", ".join(available)
            )
        extracted = [f for f in (sandbox.get("extracted_files") or []) if isinstance(f, dict)]
        if extracted:
            lines.append(f"Extracted files ({len(extracted)}, download_crowdstrike_sample(sha256)):")
            for f in extracted[:15]:
                available_flag = "" if f.get("file_available_to_download") else " [not downloadable]"
                lines.append(
                    f"  {_trunc(f.get('name'), 100)} | sha256: {f.get('sha256', '?')} | "
                    f"threat: {f.get('threat_level_readable', 'N/A')}{available_flag}"
                )
        if sandbox.get("sha256"):
            lines.append(
                "The submitted sample: download_crowdstrike_sample(sha256) stores it as "
                "CrowdStrike's password-protected ZIP."
            )
        return lines

    async def get_crowdstrike_sandbox_report(report_id: str) -> str:
        """Get the report (verdict, scores, artefacts) for a sandbox submission."""
        r, err = await _full_sandbox_report(report_id)
        if err:
            return err
        if not r:
            return (
                f"No sandbox report available yet for {report_id} — "
                "analysis may still be running (usually < 15 min)."
            )
        sandbox = _sandbox_entry(r)
        lines = [
            f"Sandbox report: {report_id}",
            f"Verdict: {r.get('verdict', 'pending')}",
            f"Threat score: {sandbox.get('threat_score', 'N/A')}",
            f"Environment: {sandbox.get('environment_description', 'N/A')}",
            f"File type: {sandbox.get('file_type', 'N/A')} | "
            f"SHA256: {sandbox.get('sha256', 'N/A')}",
            f"Submission: {sandbox.get('submission_type', 'N/A')} | "
            f"Submit name: {sandbox.get('submit_name', 'N/A')}",
        ]
        classifications = sandbox.get("classification") or []
        if classifications:
            lines.append(f"Classifications: {', '.join(classifications)}")
        lines.extend(_format_sandbox_artifacts(r))
        return "\n".join(lines)

    async def download_crowdstrike_sandbox_artifact(
        report_id: str, artifact: str, index: int = 1
    ) -> str | ToolResult:
        """Store one artefact of a sandbox report (IOC pack, PCAP, memory strings, screenshot)."""
        artifact = (artifact or "").strip().lower()
        if artifact not in _SANDBOX_ARTIFACTS:
            return f"Error: artifact must be one of {', '.join(_SANDBOX_ARTIFACTS)}."
        report, err = await _full_sandbox_report(report_id)
        if err:
            return err
        if not report:
            return f"No sandbox report available for {report_id}."
        field, suffix, content_type = _SANDBOX_ARTIFACTS[artifact]
        holder = report if artifact in _SANDBOX_REPORT_ARTIFACTS else _sandbox_entry(report)
        artifact_id = holder.get(field)
        if artifact == "screenshot":
            ids = artifact_id if isinstance(artifact_id, list) else []
            try:
                position = int(index or 1)
            except (TypeError, ValueError):
                position = 1
            artifact_id = ids[position - 1] if 1 <= position <= len(ids) else None
            suffix = f"screenshot-{position}.png"
        if not artifact_id:
            return f"Sandbox report {report_id} has no {artifact} artifact."
        filename = f"falcon-sandbox-{report_id.split('_')[-1]}-{suffix}"
        data, err = await _download(
            "/falconx/entities/artifacts/v1",
            f"download {artifact} of sandbox report {report_id}",
            params={"id": artifact_id, "name": filename},
        )
        if err:
            return err
        return await deliver_file(
            data=data,
            filename=filename,
            content_type=content_type,
            source=_PROVIDER,
            persist_file_fn=persist_file_fn,
            links=[("Download URL", f"{base_url}/falconx/entities/artifacts/v1?id={artifact_id}")],
        )

    async def download_crowdstrike_sample(sha256: str) -> str | ToolResult:
        """Store a sample of the sandbox store as CrowdStrike's password-protected ZIP."""
        sha256 = (sha256 or "").strip().lower()
        if len(sha256) != 64 or any(c not in "0123456789abcdef" for c in sha256):
            return "Error: sha256 must be a 64-character hex SHA256."
        data, err = await _download(
            "/samples/entities/samples/v2",
            f"download sample {sha256}",
            params={"ids": sha256, "password_protected": "true"},
        )
        if err:
            return err
        return await deliver_live_artefact(
            data=data,
            filename=f"{sha256}.zip",
            source=_PROVIDER,
            persist_file_fn=persist_file_fn,
            links=[
                (
                    "Download URL",
                    f"{base_url}/samples/entities/samples/v2?ids={sha256}&password_protected=true",
                )
            ],
        )

    async def list_crowdstrike_sandbox_reports(
        filter_query: str = "", count: int = 25
    ) -> str:
        """List sandbox report IDs matching an optional FQL filter."""
        params: dict[str, Any] = {"limit": min(count, 100)}
        if filter_query:
            params["filter"] = filter_query
        data, err = await _api(
            "get",
            "/falconx/queries/reports/v1",
            "list sandbox reports",
            params=params,
        )
        if err:
            return err
        ids = _resources(data)
        if not ids:
            return "No sandbox reports found."
        lines = [f"Found {len(ids)} sandbox report ID(s):"]
        lines.extend(f"  - {rid}" for rid in ids)
        return "\n".join(lines)

    # ══════════════════════════════════════════════════════════════════════
    # 7. REAL TIME RESPONSE (read-only)
    # ══════════════════════════════════════════════════════════════════════

    async def run_crowdstrike_rtr_command(device_id: str, command: str) -> str:
        """Run a read-only Real Time Response command on a host for live triage.

        Only read-only base commands are allowed (e.g. ls, cat, ps, netstat,
        ipconfig, reg query, eventlog, filehash). Active-responder/admin
        commands that modify the host are rejected. The tool opens a session,
        runs the command, polls for the result, and closes the session.
        """
        command = (command or "").strip()
        if not command:
            return "Error: a command string is required."
        base_command = command.split()[0].lower()
        if base_command not in _RTR_READONLY_COMMANDS:
            return (
                f"Error: '{base_command}' is not an allowed read-only RTR command. "
                f"Allowed: {', '.join(sorted(_RTR_READONLY_COMMANDS))}."
            )
        # ``reg`` is only read-only in its ``query`` form.
        if base_command == "reg" and not command.lower().startswith("reg query"):
            return "Error: only 'reg query ...' is permitted for the reg command."

        # 1. Open a session on the host.
        session, serr = await _api(
            "post",
            "/real-time-response/entities/sessions/v1",
            f"open RTR session on {device_id}",
            json={"device_id": device_id, "queue_offline": False},
            timeout=_RTR_TIMEOUT,
        )
        if serr:
            return serr
        sessions = _resources(session)
        if not sessions:
            return (
                f"Error: could not open an RTR session on host {device_id} "
                f"({_envelope_error(session) or 'host may be offline or RTR-ineligible'})."
            )
        session_id = sessions[0].get("session_id")
        if not session_id:
            return f"Error: RTR session response for {device_id} had no session ID."

        try:
            # 2. Execute the read-only command.
            exec_data, eerr = await _api(
                "post",
                "/real-time-response/entities/command/v1",
                f"run RTR command on {device_id}",
                json={
                    "base_command": base_command,
                    "command_string": command,
                    "session_id": session_id,
                },
                timeout=_RTR_TIMEOUT,
            )
            if eerr:
                return eerr
            exec_resources = _resources(exec_data)
            if not exec_resources:
                return (
                    f"Error: RTR command did not start "
                    f"({_envelope_error(exec_data) or 'unknown error'})."
                )
            cloud_request_id = exec_resources[0].get("cloud_request_id")
            if not cloud_request_id:
                return "Error: RTR command response had no cloud_request_id."

            # 3. Poll for the result.
            for _attempt in range(8):
                await asyncio.sleep(2)
                status_data, sterr = await _api(
                    "get",
                    "/real-time-response/entities/command/v1",
                    "check RTR command status",
                    params={
                        "cloud_request_id": cloud_request_id,
                        "sequence_id": 0,
                    },
                    timeout=_RTR_TIMEOUT,
                )
                if sterr:
                    return sterr
                results = _resources(status_data)
                if results and results[0].get("complete"):
                    res = results[0]
                    stdout = res.get("stdout") or ""
                    stderr = res.get("stderr") or ""
                    out = f"RTR `{command}` on {device_id}:\n"
                    if stdout:
                        out += f"\nstdout:\n{_trunc(stdout, 4000)}"
                    if stderr:
                        out += f"\nstderr:\n{_trunc(stderr, 1000)}"
                    if not stdout and not stderr:
                        out += "\n(command completed with no output)"
                    return out
            return (
                f"RTR command `{command}` on {device_id} is still running "
                f"(cloud_request_id: {cloud_request_id}). Try again shortly."
            )
        finally:
            # 4. Always close the session.
            await _api(
                "delete",
                "/real-time-response/entities/sessions/v1",
                "close RTR session",
                params={"session_id": session_id},
                timeout=_RTR_TIMEOUT,
            )

    # ══════════════════════════════════════════════════════════════════════
    # REGISTER ALL TOOLS
    # ══════════════════════════════════════════════════════════════════════

    executor.register("list_crowdstrike_alerts", list_crowdstrike_alerts)
    executor.register("get_crowdstrike_alert", get_crowdstrike_alert)
    executor.register("update_crowdstrike_alert", update_crowdstrike_alert)
    executor.register("list_crowdstrike_incidents", list_crowdstrike_incidents)
    executor.register("get_crowdstrike_incident", get_crowdstrike_incident)
    executor.register("update_crowdstrike_incident", update_crowdstrike_incident)
    executor.register("get_crowdstrike_crowdscore", get_crowdstrike_crowdscore)
    executor.register("list_crowdstrike_behaviors", list_crowdstrike_behaviors)
    executor.register("list_crowdstrike_hosts", list_crowdstrike_hosts)
    executor.register("get_crowdstrike_host", get_crowdstrike_host)
    executor.register("contain_crowdstrike_host", contain_crowdstrike_host)
    executor.register(
        "lift_crowdstrike_host_containment", lift_crowdstrike_host_containment
    )
    executor.register("list_crowdstrike_host_groups", list_crowdstrike_host_groups)
    executor.register(
        "list_crowdstrike_vulnerabilities", list_crowdstrike_vulnerabilities
    )
    executor.register("list_crowdstrike_iocs", list_crowdstrike_iocs)
    executor.register("create_crowdstrike_ioc", create_crowdstrike_ioc)
    executor.register("update_crowdstrike_ioc", update_crowdstrike_ioc)
    executor.register("delete_crowdstrike_ioc", delete_crowdstrike_ioc)
    executor.register("list_crowdstrike_actors", list_crowdstrike_actors)
    executor.register("get_crowdstrike_actor", get_crowdstrike_actor)
    executor.register(
        "list_crowdstrike_intel_indicators", list_crowdstrike_intel_indicators
    )
    executor.register(
        "list_crowdstrike_intel_reports", list_crowdstrike_intel_reports
    )
    executor.register(
        "download_crowdstrike_intel_report", download_crowdstrike_intel_report
    )
    executor.register(
        "submit_crowdstrike_sandbox_analysis", submit_crowdstrike_sandbox_analysis
    )
    executor.register(
        "get_crowdstrike_sandbox_report", get_crowdstrike_sandbox_report
    )
    executor.register(
        "download_crowdstrike_sandbox_artifact", download_crowdstrike_sandbox_artifact
    )
    executor.register("download_crowdstrike_sample", download_crowdstrike_sample)
    executor.register(
        "list_crowdstrike_sandbox_reports", list_crowdstrike_sandbox_reports
    )
    executor.register("run_crowdstrike_rtr_command", run_crowdstrike_rtr_command)

    src = "integration:crowdstrike"
    return [
        # ── 0. Detections / Alerts ────────────────────────────────────────
        ToolDefinition(
            name="list_crowdstrike_alerts",
            description=(
                "List CrowdStrike Falcon detections/alerts. Filter by status "
                "(new, in_progress, reopened, closed), severity_name "
                "(Critical, High, Medium, Low, Informational), or a raw FQL "
                "filter. Returns severity, status, host, tactic/technique, and "
                "assignment."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "status": {
                        "type": "string",
                        "description": "Filter by alert status (optional)",
                    },
                    "severity": {
                        "type": "string",
                        "description": "Filter by severity name e.g. High (optional)",
                    },
                    "filter_query": {
                        "type": "string",
                        "description": "Raw Falcon Query Language (FQL) filter (optional)",
                    },
                    "count": {"type": "integer", "default": 25},
                },
                "required": [],
            },
            source=src,
            priority=1,
        ),
        ToolDefinition(
            name="get_crowdstrike_alert",
            description=(
                "Get full details of a Falcon alert by its composite ID: "
                "severity, status, tactic/technique, host, file/command line, "
                "and description."
            ),
            input_schema={
                "type": "object",
                "properties": {"alert_id": {"type": "string"}},
                "required": ["alert_id"],
            },
            source=src,
            priority=1,
        ),
        ToolDefinition(
            name="update_crowdstrike_alert",
            description=(
                "Triage a Falcon alert: set status (new, in_progress, reopened, "
                "closed), assign to an analyst (user ID/email), append a "
                "comment, and/or add a tag."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "alert_id": {"type": "string"},
                    "status": {"type": "string"},
                    "assigned_to": {
                        "type": "string",
                        "description": "Analyst user ID (email/UPN)",
                    },
                    "comment": {"type": "string"},
                    "add_tag": {"type": "string"},
                },
                "required": ["alert_id"],
            },
            source=src,
            priority=1,
        ),
        # ── 1. Incidents ──────────────────────────────────────────────────
        ToolDefinition(
            name="list_crowdstrike_incidents",
            description=(
                "List CrowdScore incidents. Filter by status (new, reopened, "
                "in_progress, closed) or a raw FQL filter. Returns CrowdScore "
                "severity, status, host count, tactics, and assignment."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "status": {"type": "string"},
                    "filter_query": {"type": "string"},
                    "count": {"type": "integer", "default": 25},
                },
                "required": [],
            },
            source=src,
            priority=1,
        ),
        ToolDefinition(
            name="get_crowdstrike_incident",
            description=(
                "Get full details of a CrowdScore incident, including affected "
                "hosts, tactics, techniques, objectives, and tags."
            ),
            input_schema={
                "type": "object",
                "properties": {"incident_id": {"type": "string"}},
                "required": ["incident_id"],
            },
            source=src,
            priority=1,
        ),
        ToolDefinition(
            name="update_crowdstrike_incident",
            description=(
                "Update a CrowdScore incident: set status (new, reopened, "
                "in_progress, closed), add a tag, rename, or set the description."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "incident_id": {"type": "string"},
                    "status": {"type": "string"},
                    "add_tag": {"type": "string"},
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                },
                "required": ["incident_id"],
            },
            source=src,
            priority=2,
        ),
        ToolDefinition(
            name="get_crowdstrike_crowdscore",
            description=(
                "Get the environment-wide CrowdScore — CrowdStrike's real-time "
                "aggregate security risk score for the tenant."
            ),
            input_schema={"type": "object", "properties": {}, "required": []},
            source=src,
            priority=2,
        ),
        ToolDefinition(
            name="list_crowdstrike_behaviors",
            description=(
                "List incident behaviors (detected TTP events) with tactic, "
                "technique, host, and command line. Supports a raw FQL filter."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "filter_query": {"type": "string"},
                    "count": {"type": "integer", "default": 25},
                },
                "required": [],
            },
            source=src,
            priority=2,
        ),
        # ── 2. Hosts / Devices ────────────────────────────────────────────
        ToolDefinition(
            name="list_crowdstrike_hosts",
            description=(
                "List managed hosts (devices). Filter by hostname substring, "
                "platform (Windows, Mac, Linux), or a raw FQL filter. Returns "
                "hostname, platform, containment status, IPs, and last-seen time."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "hostname": {"type": "string"},
                    "platform": {"type": "string"},
                    "filter_query": {"type": "string"},
                    "count": {"type": "integer", "default": 25},
                },
                "required": [],
            },
            source=src,
            priority=1,
        ),
        ToolDefinition(
            name="get_crowdstrike_host",
            description=(
                "Get full details of a managed host by device ID (AID): "
                "platform, OS, containment status, IPs/MAC, agent version, "
                "policies, tags, and timestamps."
            ),
            input_schema={
                "type": "object",
                "properties": {"device_id": {"type": "string"}},
                "required": ["device_id"],
            },
            source=src,
            priority=1,
        ),
        ToolDefinition(
            name="contain_crowdstrike_host",
            description=(
                "Network-contain a host — isolate it from the network while "
                "keeping the CrowdStrike connection, to stop an active threat."
            ),
            input_schema={
                "type": "object",
                "properties": {"device_id": {"type": "string"}},
                "required": ["device_id"],
            },
            source=src,
            priority=1,
        ),
        ToolDefinition(
            name="lift_crowdstrike_host_containment",
            description="Lift network containment on a host (restore connectivity).",
            input_schema={
                "type": "object",
                "properties": {"device_id": {"type": "string"}},
                "required": ["device_id"],
            },
            source=src,
            priority=2,
        ),
        ToolDefinition(
            name="list_crowdstrike_host_groups",
            description="List host groups (dynamic/static) defined in the Falcon tenant.",
            input_schema={
                "type": "object",
                "properties": {"count": {"type": "integer", "default": 25}},
                "required": [],
            },
            source=src,
            priority=2,
        ),
        # ── 3. Vulnerabilities (Spotlight) ────────────────────────────────
        ToolDefinition(
            name="list_crowdstrike_vulnerabilities",
            description=(
                "List Spotlight vulnerabilities with CVE and host context "
                "(defaults to open). Pass a raw FQL filter to scope, e.g. "
                "\"cve.severity:'CRITICAL'\" or \"cve.id:'CVE-2024-3094'\"."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "filter_query": {"type": "string"},
                    "count": {"type": "integer", "default": 25},
                },
                "required": [],
            },
            source=src,
            priority=1,
        ),
        # ── 4. Custom IOC management ───────────────────────────────────────
        ToolDefinition(
            name="list_crowdstrike_iocs",
            description=(
                "List custom IOCs (indicators) configured in the tenant. Filter "
                "by type (sha256, sha1, md5, domain, ipv4, ipv6), value, or a "
                "raw FQL filter."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "ioc_type": {"type": "string"},
                    "value": {"type": "string"},
                    "filter_query": {"type": "string"},
                    "count": {"type": "integer", "default": 25},
                },
                "required": [],
            },
            source=src,
            priority=2,
        ),
        ToolDefinition(
            name="create_crowdstrike_ioc",
            description=(
                "Create a custom IOC to detect or prevent on endpoints. "
                "ioc_type: sha256, sha1, md5, domain, ipv4, ipv6. action: "
                "detect, prevent, no_action, allow, prevent_no_ui. severity: "
                "critical, high, medium, low, informational. platforms: "
                "comma-separated subset of windows, mac, linux."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "ioc_type": {"type": "string"},
                    "value": {"type": "string"},
                    "action": {"type": "string", "default": "detect"},
                    "severity": {"type": "string", "default": "medium"},
                    "platforms": {"type": "string", "default": "windows,mac,linux"},
                    "description": {"type": "string"},
                    "applied_globally": {"type": "boolean", "default": True},
                    "expiration": {
                        "type": "string",
                        "description": "RFC3339 expiration timestamp (optional)",
                    },
                },
                "required": ["ioc_type", "value"],
            },
            source=src,
            priority=2,
        ),
        ToolDefinition(
            name="update_crowdstrike_ioc",
            description=(
                "Update an existing custom IOC by ID. Only provided fields "
                "(action, severity, description, expiration) are changed."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "ioc_id": {"type": "string"},
                    "action": {"type": "string"},
                    "severity": {"type": "string"},
                    "description": {"type": "string"},
                    "expiration": {"type": "string"},
                },
                "required": ["ioc_id"],
            },
            source=src,
            priority=2,
        ),
        ToolDefinition(
            name="delete_crowdstrike_ioc",
            description="Delete a custom IOC (indicator) by ID.",
            input_schema={
                "type": "object",
                "properties": {
                    "ioc_id": {"type": "string"},
                    "comment": {"type": "string"},
                },
                "required": ["ioc_id"],
            },
            source=src,
            priority=2,
        ),
        # ── 5. Threat Intelligence ────────────────────────────────────────
        ToolDefinition(
            name="list_crowdstrike_actors",
            description=(
                "List CrowdStrike threat actors (adversaries) from Falcon "
                "Intelligence. Supports a free-text query and a raw FQL filter "
                "(e.g. origins, target_industries)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "filter_query": {"type": "string"},
                    "count": {"type": "integer", "default": 10},
                },
                "required": [],
            },
            source=src,
            priority=2,
        ),
        ToolDefinition(
            name="get_crowdstrike_actor",
            description=(
                "Get full details of a threat actor by numeric ID or slug: "
                "origins, capability, motivations, targeting, and description."
            ),
            input_schema={
                "type": "object",
                "properties": {"actor_id": {"type": "string"}},
                "required": ["actor_id"],
            },
            source=src,
            priority=2,
        ),
        ToolDefinition(
            name="list_crowdstrike_intel_indicators",
            description=(
                "List Falcon Intelligence indicators (published threat IOCs) "
                "with malicious confidence, associated actors, and malware "
                "families. Supports a raw FQL filter."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "filter_query": {"type": "string"},
                    "count": {"type": "integer", "default": 25},
                },
                "required": [],
            },
            source=src,
            priority=2,
        ),
        ToolDefinition(
            name="list_crowdstrike_intel_reports",
            description=(
                "List Falcon Intelligence finished intelligence reports with "
                "their id and web link. Supports a free-text query and a raw FQL "
                "filter."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "filter_query": {"type": "string"},
                    "count": {"type": "integer", "default": 10},
                },
                "required": [],
            },
            source=src,
            priority=2,
        ),
        ToolDefinition(
            name="download_crowdstrike_intel_report",
            workspace_ingest=True,
            description=(
                "Store the PDF of a Falcon Intelligence report as a file of the "
                "conversation (download card + file_id to forward, read inline) "
                "with its web link. report_id comes from "
                "list_crowdstrike_intel_reports."
            ),
            input_schema={
                "type": "object",
                "properties": {"report_id": {"type": "string"}},
                "required": ["report_id"],
            },
            source=src,
            priority=2,
        ),
        # ── 6. Falcon Intelligence Sandbox ────────────────────────────────
        ToolDefinition(
            name="submit_crowdstrike_sandbox_analysis",
            description=(
                "Submit exactly one of a URL, an existing sample (by SHA256) or a "
                "file of the conversation (file_id - uploaded to the sample store "
                "as confidential first) to the Falcon Intelligence Sandbox for "
                "detonation. environment: windows11, windows10, windows7, "
                "linux, android. Returns a submission ID to poll for the verdict."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "url": {"type": "string"},
                    "sha256": {"type": "string"},
                    "file_id": {
                        "type": "string",
                        "description": "A file attached, generated or downloaded in this conversation",
                    },
                    "environment": {"type": "string", "default": "windows10"},
                    "action_script": {"type": "string", "default": "default"},
                },
                "required": [],
            },
            source=src,
            priority=2,
        ),
        ToolDefinition(
            name="get_crowdstrike_sandbox_report",
            description=(
                "Get the sandbox report (verdict, threat score, "
                "classifications) for a submission/report ID, with its "
                "downloadable artifacts (IOC packs, PCAP, memory strings, "
                "screenshots) and extracted files."
            ),
            input_schema={
                "type": "object",
                "properties": {"report_id": {"type": "string"}},
                "required": ["report_id"],
            },
            source=src,
            priority=2,
        ),
        ToolDefinition(
            name="download_crowdstrike_sandbox_artifact",
            workspace_ingest=True,
            description=(
                "Store one artifact of a Falcon Sandbox report as a file of the "
                "conversation (download card + file_id to forward, read inline): "
                "an IOC pack (strict or broad, as CSV, JSON, STIX or MAEC), the "
                "PCAP, the memory strings, or a screenshot (index)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "report_id": {"type": "string"},
                    "artifact": {"type": "string", "enum": list(_SANDBOX_ARTIFACTS)},
                    "index": {
                        "type": "integer",
                        "default": 1,
                        "description": "1-based screenshot number",
                    },
                },
                "required": ["report_id", "artifact"],
            },
            source=src,
            priority=2,
        ),
        ToolDefinition(
            name="download_crowdstrike_sample",
            workspace_ingest=True,
            description=(
                "Store a sample of the Falcon sandbox store (a submitted or "
                "extracted file, by SHA256) as a file of the conversation. It is "
                "live malware: it is fetched only as CrowdStrike's ZIP encrypted "
                "with the password 'infected', stored exactly as it came (download "
                "card + file_id to forward, e.g. to another sandbox) and never "
                "read inline."
            ),
            input_schema={
                "type": "object",
                "properties": {"sha256": {"type": "string"}},
                "required": ["sha256"],
            },
            source=src,
            priority=2,
        ),
        ToolDefinition(
            name="list_crowdstrike_sandbox_reports",
            description="List sandbox report IDs matching an optional FQL filter.",
            input_schema={
                "type": "object",
                "properties": {
                    "filter_query": {"type": "string"},
                    "count": {"type": "integer", "default": 25},
                },
                "required": [],
            },
            source=src,
            priority=3,
        ),
        # ── 7. Real Time Response (read-only) ─────────────────────────────
        ToolDefinition(
            name="run_crowdstrike_rtr_command",
            description=(
                "Run a read-only Real Time Response command on a host for live "
                "triage (e.g. 'ls C:\\\\Windows', 'ps', 'netstat', 'ipconfig', "
                "'reg query ...', 'filehash PATH'). Only read-only commands "
                "are permitted; the tool opens a session, runs the command, "
                "returns stdout/stderr, and closes the session."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "device_id": {
                        "type": "string",
                        "description": "Host device ID (AID)",
                    },
                    "command": {
                        "type": "string",
                        "description": "Read-only RTR command to execute",
                    },
                },
                "required": ["device_id", "command"],
            },
            source=src,
            priority=2,
        ),
    ]
