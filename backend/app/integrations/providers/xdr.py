# SPDX-License-Identifier: Apache-2.0

"""Palo Alto Cortex XDR integration – Agentic SOC for the SecOps team.

Full-fledged Cortex XDR (and Cortex XSIAM, which shares the same API) integration
covering the detection → investigation → hunting → response → intelligence loop,
designed as a best-in-class building block for an agentic security team:

  - Incidents (*gathering incidents*): list, get extra data (alerts + artifacts),
    update (status / severity / assignee / resolve)
  - Alerts: list with MITRE mapping
  - Endpoints: list, get, and response actions — isolate / unisolate, scan,
    quarantine file, retrieve file, action status (*response*)
  - Retrieved files: the per-endpoint links of a file retrieval and of the
    files a script retrieved - linked, never stored, since Cortex serves them
    in an unencrypted ZIP and they can be live malware
  - Scripts / Live Terminal: list scripts, run a script on endpoints, get results;
    a script's package (its YAML in a ZIP) stored as a file of the run and
    inserted from one
  - XQL (XDR Query Language): run a query + poll results (*threat hunting*)
  - Threat Intelligence / IOCs: block / allow file hashes (*creating intelligence*)
  - Endpoint tagging and management audit logs

Authentication
--------------
Cortex XDR uses an **API key** (``Authorization``) together with an **API key ID**
(``x-xdr-auth-id``) against the tenant API host under ``/public_api/v1``
(``https://api-<fqdn>``). Both **Standard** keys (key sent as-is) and **Advanced**
keys (per-request SHA-256 of ``api_key + nonce + timestamp`` with ``x-xdr-nonce`` /
``x-xdr-timestamp`` to defeat replay) are supported.

``integration_tools._get_token`` assembles the compound
``base_url||api_key||api_key_id||auth_mode`` token this builder parses.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
import secrets
import string
import zipfile
from datetime import UTC, datetime
from typing import Any

from app.core.egress import egress_client
from app.integrations.file_delivery import (
    PersistFileFn,
    deliver_file,
    fetch_file,
    live_artefact_links,
    read_pipe_file,
    uploaded_file_answer,
)
from app.integrations.http import (
    format_entity_list,
    parse_compound_token,
    safe_request,
)
from app.tools.base import CallableToolExecutor, FileContext, ToolDefinition, ToolResult

logger = logging.getLogger(__name__)

_PROVIDER = "Cortex XDR"
_SOURCE = "integration:xdr"

# Files taken from an endpoint can be live malware, and Cortex hands them out
# as a plain ZIP (the File Retrieval Details and Get Script Execution Result
# Files APIs): the platform keeps such a file only inside a password-protected
# archive, so it links them without fetching them.
_RETRIEVED_FILES_REASON = (
    "Cortex XDR serves files taken from an endpoint in an unencrypted ZIP, and "
    "the platform keeps a live-malicious file only inside a password-protected "
    "archive."
)
# A script package (``/scripts/get`` / ``/scripts/insert``) is its YAML in a ZIP.
_MAX_SCRIPT_BYTES = 10 * 1024 * 1024
_MAX_SCRIPT_YAML_CHARS = 20_000

# All Cortex XDR REST calls live under this prefix on the tenant API host.
XDR_API_PREFIX = "/public_api/v1"

# XQL query polling budget (start query -> poll results).
_XQL_POLL_INTERVAL = 3.0
_XQL_MAX_POLLS = 8


def _sev_icon(value: Any) -> str:
    v = str(value or "").lower()
    if "critical" in v:
        return "🔴"
    if "high" in v:
        return "🔴"
    if "medium" in v:
        return "🟠"
    if "low" in v:
        return "🟡"
    if "info" in v:
        return "🔵"
    return "⚪"


def _sev_label(value: Any) -> str:
    """Normalise XDR severities (e.g. 'SEV_040_HIGH' or 'high') to a clean label."""
    v = str(value or "").lower()
    for name in ("critical", "high", "medium", "low"):
        if name in v:
            return name.capitalize()
    if "info" in v:
        return "Informational"
    return str(value or "?")


def _ts(value: Any) -> str:
    """Render an epoch-milliseconds timestamp as a UTC ISO second-precision string.

    Formats directly to ``YYYY-MM-DDTHH:MM:SSZ``; the value is always UTC, so the
    trailing ``Z`` is retained (the previous ``.replace(...)[:19]`` truncated it
    off, dropping the timezone indicator).
    """
    if value in (None, "", 0):
        return ""
    try:
        return datetime.fromtimestamp(int(value) / 1000, UTC).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
    except (TypeError, ValueError, OSError):
        return str(value)


def _trunc(text: Any, max_len: int = 300) -> str:
    if text is None:
        return ""
    text = str(text)
    return text[:max_len] + ("…" if len(text) > max_len else "")


def _stringify(value: Any, max_len: int = 1200) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return _trunc(value, max_len)
    try:
        return _trunc(json.dumps(value, default=str), max_len)
    except (TypeError, ValueError):
        return _trunc(str(value), max_len)


# ---------------------------------------------------------------------------
# Auth + URL helpers (shared with integration_testing.py)
# ---------------------------------------------------------------------------


def xdr_api_base(base_url: str) -> str:
    """Return the Cortex XDR API base URL with the public prefix."""
    return f"{base_url.rstrip('/')}{XDR_API_PREFIX}"


def build_xdr_headers(
    api_key: str, api_key_id: str, auth_mode: str = "standard"
) -> dict[str, str]:
    """Build Cortex XDR request headers for Standard or Advanced API keys.

    * Standard: ``Authorization`` (key as-is) + ``x-xdr-auth-id``.
    * Advanced: a per-request SHA-256 of ``api_key + nonce + timestamp`` plus
      ``x-xdr-nonce`` / ``x-xdr-timestamp`` (replay protection). A fresh
      nonce/timestamp is generated on every call, so this must be invoked per
      request rather than once at build time.
    """
    headers: dict[str, str] = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "x-xdr-auth-id": api_key_id,
    }
    if (auth_mode or "standard").lower() == "advanced":
        nonce = "".join(
            secrets.choice(string.ascii_letters + string.digits) for _ in range(64)
        )
        timestamp = str(int(datetime.now(UTC).timestamp()) * 1000)
        api_key_hash = hashlib.sha256(
            f"{api_key}{nonce}{timestamp}".encode()
        ).hexdigest()
        headers["x-xdr-nonce"] = nonce
        headers["x-xdr-timestamp"] = timestamp
        headers["Authorization"] = api_key_hash
    else:
        headers["Authorization"] = api_key
    return headers


def _endpoint_filters(endpoint_ids: list[str]) -> list[dict[str, Any]]:
    return [{"field": "endpoint_id_list", "operator": "in", "value": endpoint_ids}]


def _script_yaml_preview(data: bytes) -> list[str]:
    """The YAML of a script package, bounded - a ZIP reads as no text inline."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            for entry in archive.infolist():
                if entry.filename.lower().endswith((".yml", ".yaml")) and entry.file_size <= _MAX_SCRIPT_YAML_CHARS * 4:
                    text = archive.read(entry).decode("utf-8", errors="replace")
                    cut = len(text) > _MAX_SCRIPT_YAML_CHARS
                    return [
                        f"{entry.filename}:",
                        text[:_MAX_SCRIPT_YAML_CHARS] + ("\n[... truncated]" if cut else ""),
                    ]
    except (zipfile.BadZipFile, ValueError, OSError):
        pass
    return []


# ---------------------------------------------------------------------------
# Tool builder
# ---------------------------------------------------------------------------


def build_tools(
    token: str,
    executor: CallableToolExecutor,
    persist_file_fn: PersistFileFn | None = None,
    **_kwargs: Any,
) -> list[ToolDefinition]:
    """Build Cortex XDR tools from a ``base_url||api_key||api_key_id||auth_mode`` token.

    ``persist_file_fn`` (injected by ``integration_tools``) stores a script
    package as a file of the run.
    """
    base_url, api_key, api_key_id, auth_mode = parse_compound_token(token, 4)
    if not base_url or not api_key or not api_key_id:
        return []

    api = xdr_api_base(base_url)

    def _headers() -> dict[str, str]:
        # Rebuilt per call so Advanced keys get a fresh nonce/timestamp.
        return build_xdr_headers(api_key, api_key_id, auth_mode)

    async def _post(path: str, action: str, request_data: dict, *, timeout: int = 30):
        """POST a request_data envelope; returns ``(reply, error_str)``."""
        async with egress_client(timeout=timeout) as client:
            resp, err = await safe_request(
                client,
                "post",
                f"{api}/{path}",
                _PROVIDER,
                action,
                headers=_headers(),
                json={"request_data": request_data},
            )
            if err:
                return None, err
            return (resp.json() or {}).get("reply", {}), None

    # ══════════════════════════════════════════════════════════════════════
    # 0. CONNECTION
    # ══════════════════════════════════════════════════════════════════════

    async def get_xdr_status() -> str:
        """Validate the Cortex XDR connection and report accessible incidents."""
        reply, err = await _post(
            "incidents/get_incidents/",
            "validate connection",
            {"search_from": 0, "search_to": 1},
        )
        if err:
            return err
        total = reply.get("total_count", "?") if isinstance(reply, dict) else "?"
        return (
            f"Connected to Cortex XDR at {base_url}.\n"
            f"  Accessible incidents: {total}"
        )

    # ══════════════════════════════════════════════════════════════════════
    # 1. INCIDENTS (gathering incidents)
    # ══════════════════════════════════════════════════════════════════════

    async def list_xdr_incidents(
        status: str = "",
        severity: str = "",
        since_hours: int = 0,
        limit: int = 25,
    ) -> str:
        """List Cortex XDR incidents, optionally filtered by status and recency."""
        filters: list[dict[str, Any]] = []
        if status:
            filters.append({"field": "status", "operator": "eq", "value": status})
        if since_hours > 0:
            since_ms = int(datetime.now(UTC).timestamp() * 1000) - since_hours * 3600000
            filters.append(
                {"field": "creation_time", "operator": "gte", "value": since_ms}
            )
        request_data: dict[str, Any] = {
            "search_from": 0,
            "search_to": min(max(limit, 1), 100),
            "sort": {"field": "creation_time", "keyword": "desc"},
        }
        if filters:
            request_data["filters"] = filters

        reply, err = await _post(
            "incidents/get_incidents/", "list incidents", request_data
        )
        if err:
            return err
        incidents = reply.get("incidents", []) if isinstance(reply, dict) else []
        # Severity is not a server-side incident filter, so apply it client-side.
        if severity:
            want = severity.lower()
            incidents = [
                i for i in incidents if want in str(i.get("severity", "")).lower()
            ]
        total = reply.get("total_count") if isinstance(reply, dict) else None

        def _fmt(inc: dict) -> str:
            return (
                f"  {_sev_icon(inc.get('severity'))} #{inc.get('incident_id', '?')} "
                f"[{_sev_label(inc.get('severity'))}] {inc.get('status', '?')} — "
                f"{_trunc(inc.get('description') or inc.get('incident_name'), 90)}\n"
                f"    Alerts: {inc.get('alert_count', 0)} | "
                f"Assignee: {inc.get('assigned_user_pretty_name') or inc.get('assigned_user_mail') or 'Unassigned'} | "
                f"Created: {_ts(inc.get('creation_time'))}"
            )

        return format_entity_list(incidents, _fmt, noun="incident", total=total)

    async def get_xdr_incident(incident_id: str, alerts_limit: int = 30) -> str:
        """Get full Cortex XDR incident detail: alerts, network & file artifacts."""
        reply, err = await _post(
            "incidents/get_incident_extra_data/",
            f"get incident {incident_id}",
            {"incident_id": incident_id, "alerts_limit": min(alerts_limit, 100)},
        )
        if err:
            return err
        inc = reply.get("incident", {}) if isinstance(reply, dict) else {}
        alerts = (reply.get("alerts") or {}).get("data", []) if isinstance(reply, dict) else []
        net = (reply.get("network_artifacts") or {}).get("data", []) if isinstance(reply, dict) else []
        files = (reply.get("file_artifacts") or {}).get("data", []) if isinstance(reply, dict) else []

        lines = [
            f"Incident #{inc.get('incident_id', incident_id)}: "
            f"{inc.get('description') or inc.get('incident_name', 'Untitled')}",
            f"Severity: {_sev_icon(inc.get('severity'))} {_sev_label(inc.get('severity'))} | "
            f"Status: {inc.get('status', '?')}",
            f"Assignee: {inc.get('assigned_user_pretty_name') or inc.get('assigned_user_mail') or 'Unassigned'}",
            f"Alerts: {inc.get('alert_count', 0)} | "
            f"Hosts: {', '.join(inc.get('hosts', []) or []) or 'N/A'}",
            f"Created: {_ts(inc.get('creation_time'))} | "
            f"Modified: {_ts(inc.get('modification_time'))}",
        ]
        if alerts:
            lines.append(f"\nAlerts ({len(alerts)}):")
            for a in alerts[:alerts_limit]:
                tactic = a.get("mitre_tactic_id_and_name") or "N/A"
                lines.append(
                    f"  {_sev_icon(a.get('severity'))} [{_sev_label(a.get('severity'))}] "
                    f"{a.get('name', 'Unnamed')} "
                    f"(host: {a.get('host_name', '?')}, action: {a.get('action', '?')})\n"
                    f"    MITRE: {tactic}"
                )
        if net:
            arts = ", ".join(
                f"{n.get('network_domain') or n.get('network_remote_ip', '?')}"
                for n in net[:15]
            )
            lines.append(f"\nNetwork artifacts: {arts}")
        if files:
            arts = ", ".join(
                f"{f.get('file_name', '?')} ({f.get('file_sha256', '')[:12]})"
                for f in files[:15]
            )
            lines.append(f"File artifacts: {arts}")
        return "\n".join(lines)

    async def update_xdr_incident(
        incident_id: str,
        status: str = "",
        severity: str = "",
        assigned_user_mail: str = "",
        resolve_comment: str = "",
    ) -> str:
        """Update a Cortex XDR incident (status, manual severity, assignee, resolve note)."""
        update_data: dict[str, Any] = {}
        if status:
            update_data["status"] = status
        if severity:
            update_data["manual_severity"] = severity.lower()
        if assigned_user_mail:
            update_data["assigned_user_mail"] = assigned_user_mail
        if resolve_comment:
            update_data["resolve_comment"] = resolve_comment
        if not update_data:
            return "Error: No fields provided to update."

        _, err = await _post(
            "incidents/update_incident/",
            f"update incident {incident_id}",
            {"incident_id": incident_id, "update_data": update_data},
        )
        if err:
            return err
        changed = ", ".join(f"{k}={v}" for k, v in update_data.items())
        return f"Incident {incident_id} updated — {changed}"

    # ══════════════════════════════════════════════════════════════════════
    # 2. ALERTS
    # ══════════════════════════════════════════════════════════════════════

    async def list_xdr_alerts(
        severity: str = "", since_hours: int = 0, limit: int = 25
    ) -> str:
        """List Cortex XDR alerts with MITRE mapping, filtered by severity/recency."""
        filters: list[dict[str, Any]] = []
        if severity:
            filters.append(
                {"field": "severity", "operator": "in", "value": [severity.lower()]}
            )
        if since_hours > 0:
            since_ms = int(datetime.now(UTC).timestamp() * 1000) - since_hours * 3600000
            filters.append(
                {"field": "creation_time", "operator": "gte", "value": since_ms}
            )
        request_data: dict[str, Any] = {
            "search_from": 0,
            "search_to": min(max(limit, 1), 100),
            "sort": {"field": "creation_time", "keyword": "desc"},
        }
        if filters:
            request_data["filters"] = filters

        reply, err = await _post(
            "alerts/get_alerts_multi_events/", "list alerts", request_data
        )
        if err:
            return err
        alerts = reply.get("alerts", []) if isinstance(reply, dict) else []
        total = reply.get("total_count") if isinstance(reply, dict) else None

        def _fmt(a: dict) -> str:
            return (
                f"  {_sev_icon(a.get('severity'))} [{_sev_label(a.get('severity'))}] "
                f"{a.get('name', 'Unnamed')} (id: {a.get('alert_id', '?')})\n"
                f"    Source: {a.get('source', '?')} | Action: {a.get('action', '?')} | "
                f"Host: {a.get('host_name', '?')} | "
                f"MITRE: {a.get('mitre_technique_id_and_name') or 'N/A'}\n"
                f"    Detected: {_ts(a.get('detection_timestamp'))}"
            )

        return format_entity_list(alerts, _fmt, noun="alert", total=total)

    # ══════════════════════════════════════════════════════════════════════
    # 3. ENDPOINTS
    # ══════════════════════════════════════════════════════════════════════

    def _fmt_endpoint(e: dict) -> str:
        ips = ", ".join(e.get("ip", []) or []) if isinstance(e.get("ip"), list) else e.get("ip", "?")
        return (
            f"  {e.get('endpoint_name', '?')} (id: {e.get('endpoint_id', '?')})\n"
            f"    OS: {e.get('os_type', '?')} | Status: {e.get('endpoint_status', '?')} | "
            f"Isolated: {e.get('is_isolated', '?')}\n"
            f"    IP: {ips} | Group: {e.get('group_name', 'N/A')} | "
            f"Last seen: {_ts(e.get('last_seen'))}"
        )

    async def list_xdr_endpoints(
        hostname: str = "",
        platform: str = "",
        endpoint_status: str = "",
        isolate: str = "",
        limit: int = 25,
    ) -> str:
        """List Cortex XDR endpoints (agents) with optional filters."""
        filters: list[dict[str, Any]] = []
        if hostname:
            filters.append({"field": "hostname", "operator": "in", "value": [hostname]})
        if platform:
            filters.append(
                {"field": "platform", "operator": "in", "value": [platform.lower()]}
            )
        if endpoint_status:
            filters.append(
                {
                    "field": "endpoint_status",
                    "operator": "in",
                    "value": [endpoint_status.lower()],
                }
            )
        if isolate:
            filters.append(
                {"field": "isolate", "operator": "in", "value": [isolate.lower()]}
            )
        request_data: dict[str, Any] = {
            "search_from": 0,
            "search_to": min(max(limit, 1), 100),
            "filters": filters,
        }
        reply, err = await _post(
            "endpoints/get_endpoint/", "list endpoints", request_data
        )
        if err:
            return err
        endpoints = reply.get("endpoints", []) if isinstance(reply, dict) else []
        total = reply.get("total_count") if isinstance(reply, dict) else None
        return format_entity_list(endpoints, _fmt_endpoint, noun="endpoint", total=total)

    async def get_xdr_endpoint(endpoint_id: str) -> str:
        """Get full details of a single Cortex XDR endpoint by id."""
        reply, err = await _post(
            "endpoints/get_endpoint/",
            f"get endpoint {endpoint_id}",
            {"filters": _endpoint_filters([endpoint_id]), "search_from": 0, "search_to": 1},
        )
        if err:
            return err
        endpoints = reply.get("endpoints", []) if isinstance(reply, dict) else []
        if not endpoints:
            return f"No endpoint found with id {endpoint_id}."
        e = endpoints[0]
        lines = [
            _fmt_endpoint(e),
            f"    Alias: {e.get('alias', 'N/A')} | Domain: {e.get('domain', 'N/A')}",
            f"    Agent version: {e.get('endpoint_version', '?')} | "
            f"Operational: {e.get('operational_status', '?')}",
            f"    First seen: {_ts(e.get('first_seen'))} | "
            f"Public IP: {e.get('public_ip', 'N/A')}",
            f"    Users: {', '.join(e.get('users', []) or []) or 'N/A'}",
        ]
        return "\n".join(lines)

    # ── Response actions ───────────────────────────────────────────────────

    async def _action_reply(reply: Any, label: str) -> str:
        if isinstance(reply, dict):
            action_id = reply.get("action_id") or reply.get("group_action_id")
            count = reply.get("endpoints_count")
            return (
                f"{label} requested (action id: {action_id or '?'}"
                + (f", endpoints: {count}" if count is not None else "")
                + "). Track with get_xdr_action_status."
            )
        return f"{label} requested (action id: {reply})."

    async def isolate_xdr_endpoint(endpoint_id: str, incident_id: str = "") -> str:
        """Isolate a Cortex XDR endpoint from the network (containment)."""
        request_data: dict[str, Any] = {"endpoint_id": endpoint_id}
        if incident_id:
            request_data["incident_id"] = incident_id
        reply, err = await _post(
            "endpoints/isolate/", f"isolate endpoint {endpoint_id}", request_data, timeout=60
        )
        if err:
            return err
        return await _action_reply(reply, "Isolation")

    async def unisolate_xdr_endpoint(endpoint_id: str, incident_id: str = "") -> str:
        """Release a Cortex XDR endpoint from network isolation."""
        request_data: dict[str, Any] = {"endpoint_id": endpoint_id}
        if incident_id:
            request_data["incident_id"] = incident_id
        reply, err = await _post(
            "endpoints/unisolate/",
            f"unisolate endpoint {endpoint_id}",
            request_data,
            timeout=60,
        )
        if err:
            return err
        return await _action_reply(reply, "Release from isolation")

    async def scan_xdr_endpoints(
        endpoint_ids: list[str] | None = None, incident_id: str = ""
    ) -> str:
        """Run a malware scan on specific endpoints, or all endpoints if none given."""
        request_data: dict[str, Any] = {}
        if endpoint_ids:
            request_data["filters"] = _endpoint_filters(endpoint_ids)
        else:
            request_data["filters"] = "all"
        if incident_id:
            request_data["incident_id"] = incident_id
        reply, err = await _post(
            "endpoints/scan/", "scan endpoints", request_data, timeout=60
        )
        if err:
            return err
        return await _action_reply(reply, "Scan")

    async def quarantine_xdr_file(
        endpoint_id: str, file_hash: str, file_path: str, incident_id: str = ""
    ) -> str:
        """Quarantine a malicious file (by SHA256 + path) on a Cortex XDR endpoint."""
        request_data: dict[str, Any] = {
            "filters": _endpoint_filters([endpoint_id]),
            "file_hash": file_hash,
            "file_path": file_path,
        }
        if incident_id:
            request_data["incident_id"] = incident_id
        reply, err = await _post(
            "endpoints/quarantine/",
            f"quarantine file on {endpoint_id}",
            request_data,
            timeout=60,
        )
        if err:
            return err
        return await _action_reply(reply, "Quarantine")

    async def retrieve_xdr_file(
        endpoint_id: str, platform: str, file_path: str
    ) -> str:
        """Retrieve a file from a Cortex XDR endpoint for forensic analysis."""
        platform_key = platform.lower()
        if platform_key not in ("windows", "linux", "macos"):
            return "Error: platform must be one of windows, linux, macos."
        request_data: dict[str, Any] = {
            "filters": _endpoint_filters([endpoint_id]),
            "files": {platform_key: [file_path]},
        }
        reply, err = await _post(
            "endpoints/file_retrieval/",
            f"retrieve file from {endpoint_id}",
            request_data,
            timeout=60,
        )
        if err:
            return err
        return (
            await _action_reply(reply, "File retrieval")
            + "\nOnce it completed, get_xdr_file_retrieval_links(group_action_id) "
            "gives the download links."
        )

    async def get_xdr_file_retrieval_links(group_action_id: str) -> str:
        """The download links of the files a Retrieve File action took from its endpoints."""
        reply, err = await _post(
            "actions/file_retrieval_details/",
            f"get file retrieval details {group_action_id}",
            {"group_action_id": group_action_id},
        )
        if err:
            return err
        data = reply.get("data", {}) if isinstance(reply, dict) else {}
        if not isinstance(data, dict) or not data:
            return (
                f"No retrieved files yet for action {group_action_id} - check "
                "get_xdr_action_status until the retrieval completed."
            )
        return live_artefact_links(
            filename=f"files retrieved by action {group_action_id}",
            source=_PROVIDER,
            links=[(f"Endpoint {endpoint_id}", link) for endpoint_id, link in data.items()],
            reason=_RETRIEVED_FILES_REASON,
        )

    async def get_xdr_action_status(group_action_id: str) -> str:
        """Get the per-endpoint status of a Cortex XDR response action."""
        reply, err = await _post(
            "actions/get_action_status/",
            f"get action status {group_action_id}",
            {"group_action_id": group_action_id},
        )
        if err:
            return err
        data = reply.get("data", {}) if isinstance(reply, dict) else {}
        if not data:
            return f"No status found for action {group_action_id}."
        lines = [f"Action {group_action_id} status:"]
        for endpoint_id, status in data.items():
            lines.append(f"  {endpoint_id}: {status}")
        return "\n".join(lines)

    # ══════════════════════════════════════════════════════════════════════
    # 4. SCRIPTS / LIVE TERMINAL
    # ══════════════════════════════════════════════════════════════════════

    async def list_xdr_scripts(limit: int = 30) -> str:
        """List Cortex XDR scripts available to run on endpoints."""
        reply, err = await _post("scripts/get_scripts/", "list scripts", {"filters": []})
        if err:
            return err
        scripts = reply.get("scripts", []) if isinstance(reply, dict) else []

        def _fmt(s: dict) -> str:
            risk = " ⚠️ high-risk" if s.get("is_high_risk") else ""
            return (
                f"  {s.get('name', 'Unnamed')}{risk} (uid: {s.get('script_uid', '?')})\n"
                f"    {_trunc(s.get('description'), 120) or 'No description'}"
            )

        return format_entity_list(scripts[: max(limit, 1)], _fmt, noun="script")

    async def download_xdr_script(script_id: str = "", name: str = "") -> str | ToolResult:
        """Store a script's definition (its YAML in a ZIP) as a file of the run."""
        if bool(script_id) == bool(name):
            return "Error: provide exactly one of 'script_id' or 'name'."
        field, value = ("id", script_id) if script_id else ("name", name)
        async with egress_client(timeout=60) as client:
            fetched = await fetch_file(
                client,
                "post",
                f"{api}/scripts/get",
                provider=_PROVIDER,
                action=f"get script {value}",
                max_bytes=_MAX_SCRIPT_BYTES,
                headers={**_headers(), "Accept": "*/*"},
                json={"request_data": {"filter": {"field": field, "value": value}}},
            )
        if isinstance(fetched, str):
            return fetched
        safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in value) or "script"
        return await deliver_file(
            data=fetched.data,
            filename=fetched.filename or f"xdr-script-{safe}.zip",
            content_type="application/zip",
            source=_PROVIDER,
            persist_file_fn=persist_file_fn,
            details=_script_yaml_preview(fetched.data),
        )

    async def upload_xdr_script(
        file_id: str, file_context: FileContext | None = None
    ) -> str | ToolResult:
        """Insert or update a script from a file of the run (its YAML in a ZIP)."""
        piped = await read_pipe_file(
            file_context, file_id, max_bytes=_MAX_SCRIPT_BYTES, target=_PROVIDER
        )
        if isinstance(piped, str):
            return ToolResult(tool_call_id="", content=piped, is_error=True)
        data, filename, _ = piped
        file_id = file_id.strip()
        if not data.startswith(b"PK"):
            return ToolResult(
                tool_call_id="",
                content=(
                    f"Error: file {file_id} is not a ZIP - Cortex XDR takes "
                    "a script as its YAML in a zipped file."
                ),
                is_error=True,
            )
        upload_headers = {k: v for k, v in _headers().items() if k.lower() != "content-type"}
        async with egress_client(timeout=60) as client:
            resp, err = await safe_request(
                client,
                "post",
                f"{api}/scripts/insert",
                _PROVIDER,
                f"insert the script of file {file_id}",
                headers=upload_headers,
                files={"file": (filename, data, "application/zip")},
            )
        if err:
            return err
        reply = (resp.json() or {}).get("reply", resp.json() or {})
        objects = reply.get("objects") if isinstance(reply, dict) else None
        failures = (objects or {}).get("failures_items") or []
        if failures:
            reasons = "; ".join(
                f"{f.get('id', '?')}: {_trunc(f.get('error'), 160)}" for f in failures if isinstance(f, dict)
            )
            return f"Cortex XDR refused the script: {reasons}"
        return uploaded_file_answer(
            f"Inserted file {file_id} into Cortex XDR "
            f"({reply.get('objects_count', '?') if isinstance(reply, dict) else '?'} script(s)). "
            "list_xdr_scripts shows it.",
            target=_PROVIDER,
            filename=filename,
        )

    async def run_xdr_script(
        script_uid: str,
        endpoint_ids: list[str],
        timeout_seconds: int = 600,
        parameters: dict[str, Any] | None = None,
    ) -> str:
        """Run a Cortex XDR script on one or more endpoints (live response)."""
        if not endpoint_ids:
            return "Error: at least one endpoint_id is required."
        request_data: dict[str, Any] = {
            "script_uid": script_uid,
            "timeout": timeout_seconds,
            "filters": _endpoint_filters(endpoint_ids),
        }
        if parameters:
            request_data["parameters_values"] = parameters
        reply, err = await _post(
            "scripts/run_script/", f"run script {script_uid}", request_data, timeout=60
        )
        if err:
            return err
        return await _action_reply(reply, "Script execution")

    async def get_xdr_script_results(action_id: str) -> str:
        """Get the results of a Cortex XDR script execution by action id."""
        reply, err = await _post(
            "scripts/get_script_execution_results/",
            f"get script results {action_id}",
            {"action_id": action_id},
        )
        if err:
            return err
        results = reply.get("results", []) if isinstance(reply, dict) else []
        if not results:
            return f"No results yet for script action {action_id}."
        lines = [f"Script results for action {action_id} ({len(results)} endpoint(s)):"]
        retrieved = False
        for r in results[:20]:
            lines.append(
                f"\n  Endpoint {r.get('endpoint_name', r.get('endpoint_id', '?'))} "
                f"— status: {r.get('execution_status', '?')}\n"
                f"  {_stringify(r.get('standard_output') or r.get('return_value') or r, 800)}"
            )
            if not (isinstance(r.get("retrieved_files"), int) and r["retrieved_files"] > 0):
                continue
            files_reply, files_err = await _post(
                "scripts/get_script_execution_results_files/",
                f"get script result files {action_id}",
                {"action_id": action_id, "endpoint_id": r.get("endpoint_id", "")},
            )
            link = files_reply.get("data") if isinstance(files_reply, dict) else None
            if files_err or not link:
                lines.append(f"  Retrieved files: {r['retrieved_files']} (link unavailable: {files_err or 'none returned'})")
            else:
                retrieved = True
                lines.append(
                    f"  Retrieved files: {r['retrieved_files']} - Download URL (signed, "
                    f"expires after 10 minutes): {link}"
                )
        if retrieved:
            lines.append(f"\nThe retrieved files are not stored: {_RETRIEVED_FILES_REASON}")
        return "\n".join(lines)

    # ══════════════════════════════════════════════════════════════════════
    # 5. XQL (threat hunting)
    # ══════════════════════════════════════════════════════════════════════

    def _format_xql(reply: dict) -> str:
        status = reply.get("status", "?")
        if status != "SUCCESS":
            return f"XQL query status: {status} (try get_xdr_xql_results with the query id shortly)."
        results = (reply.get("results") or {}).get("data", []) or []
        count = reply.get("number_of_results", len(results))
        if not results:
            return f"XQL query completed: 0 results (of {count})."
        lines = [f"XQL results ({len(results)} of {count} shown):"]
        for row in results[:50]:
            lines.append(_stringify(row, 400))
        if len(results) > 50:
            lines.append(f"\n... [{len(results) - 50} more rows not shown]")
        return "\n".join(lines)

    async def run_xdr_xql_query(query: str, since_hours: int = 24) -> str:
        """Run an XQL (XDR Query Language) query for threat hunting and wait for results.

        Example: `dataset = xdr_data | filter action_process_image_name = "powershell.exe"
        | fields agent_hostname, action_process_image_command_line | limit 50`.
        """
        relative_ms = max(since_hours, 1) * 3600000
        reply, err = await _post(
            "xql/start_xql_query/",
            "start XQL query",
            {"query": query, "tenants": [], "timeframe": {"relativeTime": relative_ms}},
            timeout=60,
        )
        if err:
            return err
        # The start endpoint returns the execution id directly as `reply`.
        query_id = reply if isinstance(reply, str) else (
            reply.get("execution_id") if isinstance(reply, dict) else None
        )
        if not query_id:
            return "Error: Cortex XDR did not return an XQL execution id."

        last: dict[str, Any] = {}
        for _ in range(_XQL_MAX_POLLS):
            await asyncio.sleep(_XQL_POLL_INTERVAL)
            poll, perr = await _post(
                "xql/get_query_results/",
                f"get XQL results {query_id}",
                {"query_id": query_id, "pending_flag": True, "limit": 100, "format": "json"},
                timeout=60,
            )
            if perr:
                return perr
            last = poll if isinstance(poll, dict) else {}
            if last.get("status") == "SUCCESS":
                break
        return f"XQL query {query_id}:\n{_format_xql(last)}"

    async def get_xdr_xql_results(query_id: str) -> str:
        """Fetch results for a previously started XQL query by its execution id."""
        reply, err = await _post(
            "xql/get_query_results/",
            f"get XQL results {query_id}",
            {"query_id": query_id, "pending_flag": True, "limit": 100, "format": "json"},
            timeout=60,
        )
        if err:
            return err
        return _format_xql(reply if isinstance(reply, dict) else {})

    # ══════════════════════════════════════════════════════════════════════
    # 6. THREAT INTELLIGENCE — file hash block / allow (creating intelligence)
    # ══════════════════════════════════════════════════════════════════════

    async def blocklist_xdr_files(
        hashes: list[str], comment: str = "", incident_id: str = ""
    ) -> str:
        """Add file SHA256 hashes to the Cortex XDR block list (blocks execution)."""
        request_data: dict[str, Any] = {"hash_list": hashes}
        if comment:
            request_data["comment"] = comment
        if incident_id:
            request_data["incident_id"] = incident_id
        _, err = await _post(
            "hash_exceptions/blocklist/", "blocklist file hashes", request_data
        )
        if err:
            return err
        return f"Added {len(hashes)} hash(es) to the Cortex XDR block list."

    async def allowlist_xdr_files(
        hashes: list[str], comment: str = "", incident_id: str = ""
    ) -> str:
        """Add file SHA256 hashes to the Cortex XDR allow list (permits execution)."""
        request_data: dict[str, Any] = {"hash_list": hashes}
        if comment:
            request_data["comment"] = comment
        if incident_id:
            request_data["incident_id"] = incident_id
        _, err = await _post(
            "hash_exceptions/allowlist/", "allowlist file hashes", request_data
        )
        if err:
            return err
        return f"Added {len(hashes)} hash(es) to the Cortex XDR allow list."

    # ══════════════════════════════════════════════════════════════════════
    # 7. ENDPOINT TAGS + AUDIT
    # ══════════════════════════════════════════════════════════════════════

    async def manage_xdr_endpoint_tag(
        endpoint_ids: list[str], tag: str, action: str = "assign"
    ) -> str:
        """Assign or remove a tag on Cortex XDR endpoints (for grouping/policy)."""
        path = (
            "tags/agents/assign/"
            if action.lower() == "assign"
            else "tags/agents/remove/"
        )
        _, err = await _post(
            path,
            f"{action} tag '{tag}'",
            {"filters": _endpoint_filters(endpoint_ids), "tag": tag},
        )
        if err:
            return err
        verb = "assigned to" if action.lower() == "assign" else "removed from"
        return f"Tag '{tag}' {verb} {len(endpoint_ids)} endpoint(s)."

    async def get_xdr_audit_logs(limit: int = 25) -> str:
        """Get recent Cortex XDR management audit logs."""
        reply, err = await _post(
            "audits/management_logs/",
            "get audit logs",
            {
                "search_from": 0,
                "search_to": min(max(limit, 1), 100),
                "sort": {"field": "TIMESTAMP", "keyword": "desc"},
            },
        )
        if err:
            return err
        data = reply.get("data", []) if isinstance(reply, dict) else []

        def _fmt(log: dict) -> str:
            return (
                f"  [{_ts(log.get('AUDIT_INSERT_TIME') or log.get('TIMESTAMP'))}] "
                f"{log.get('AUDIT_OWNER_NAME', log.get('AUDIT_OWNER_EMAIL', '?'))} — "
                f"{log.get('AUDIT_ASSET_TYPE', '')} "
                f"{log.get('AUDIT_CASE_ID', '')} "
                f"{_trunc(log.get('AUDIT_DESCRIPTION'), 120)}"
            )

        return format_entity_list(data, _fmt, noun="audit log entry")

    # ══════════════════════════════════════════════════════════════════════
    # REGISTER ALL TOOLS
    # ══════════════════════════════════════════════════════════════════════

    executor.register("get_xdr_status", get_xdr_status)
    executor.register("list_xdr_incidents", list_xdr_incidents)
    executor.register("get_xdr_incident", get_xdr_incident)
    executor.register("update_xdr_incident", update_xdr_incident)
    executor.register("list_xdr_alerts", list_xdr_alerts)
    executor.register("list_xdr_endpoints", list_xdr_endpoints)
    executor.register("get_xdr_endpoint", get_xdr_endpoint)
    executor.register("isolate_xdr_endpoint", isolate_xdr_endpoint)
    executor.register("unisolate_xdr_endpoint", unisolate_xdr_endpoint)
    executor.register("scan_xdr_endpoints", scan_xdr_endpoints)
    executor.register("quarantine_xdr_file", quarantine_xdr_file)
    executor.register("retrieve_xdr_file", retrieve_xdr_file)
    executor.register("get_xdr_file_retrieval_links", get_xdr_file_retrieval_links)
    executor.register("get_xdr_action_status", get_xdr_action_status)
    executor.register("list_xdr_scripts", list_xdr_scripts)
    executor.register("download_xdr_script", download_xdr_script)
    executor.register("upload_xdr_script", upload_xdr_script)
    executor.register("run_xdr_script", run_xdr_script)
    executor.register("get_xdr_script_results", get_xdr_script_results)
    executor.register("run_xdr_xql_query", run_xdr_xql_query)
    executor.register("get_xdr_xql_results", get_xdr_xql_results)
    executor.register("blocklist_xdr_files", blocklist_xdr_files)
    executor.register("allowlist_xdr_files", allowlist_xdr_files)
    executor.register("manage_xdr_endpoint_tag", manage_xdr_endpoint_tag)
    executor.register("get_xdr_audit_logs", get_xdr_audit_logs)

    return [
        # ── 0. Connection ──────────────────────────────────────────────────
        ToolDefinition(
            name="get_xdr_status",
            description=(
                "Validate the Cortex XDR connection and report the number of "
                "accessible incidents. Use first if other XDR tools fail."
            ),
            input_schema={"type": "object", "properties": {}, "required": []},
            source=_SOURCE,
            priority=2,
        ),
        # ── 1. Incidents ───────────────────────────────────────────────────
        ToolDefinition(
            name="list_xdr_incidents",
            description=(
                "List Cortex XDR incidents (gathering incidents). Filter by status "
                "(new, under_investigation, resolved_threat_handled, "
                "resolved_false_positive, …), severity (low/medium/high/critical), "
                "and recency (since_hours)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "status": {
                        "type": "string",
                        "description": "e.g. new, under_investigation, resolved_threat_handled",
                    },
                    "severity": {
                        "type": "string",
                        "enum": ["informational", "low", "medium", "high", "critical"],
                    },
                    "since_hours": {
                        "type": "integer",
                        "description": "Only incidents created in the last N hours",
                        "default": 0,
                    },
                    "limit": {"type": "integer", "default": 25},
                },
                "required": [],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="get_xdr_incident",
            description=(
                "Get full Cortex XDR incident detail by id: alerts (with MITRE), "
                "and network + file artifacts for IOC extraction."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "incident_id": {"type": "string"},
                    "alerts_limit": {"type": "integer", "default": 30},
                },
                "required": ["incident_id"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="update_xdr_incident",
            description=(
                "Update a Cortex XDR incident: status, manual severity, assignee "
                "(assigned_user_mail), or resolve comment. Only provided fields change."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "incident_id": {"type": "string"},
                    "status": {
                        "type": "string",
                        "description": "new, under_investigation, or a resolved_* status",
                    },
                    "severity": {
                        "type": "string",
                        "enum": ["low", "medium", "high"],
                    },
                    "assigned_user_mail": {"type": "string"},
                    "resolve_comment": {"type": "string"},
                },
                "required": ["incident_id"],
            },
            source=_SOURCE,
            priority=1,
        ),
        # ── 2. Alerts ──────────────────────────────────────────────────────
        ToolDefinition(
            name="list_xdr_alerts",
            description="List Cortex XDR alerts with MITRE technique mapping. Filter by severity and recency (since_hours).",
            input_schema={
                "type": "object",
                "properties": {
                    "severity": {
                        "type": "string",
                        "enum": ["informational", "low", "medium", "high", "critical"],
                    },
                    "since_hours": {"type": "integer", "default": 0},
                    "limit": {"type": "integer", "default": 25},
                },
                "required": [],
            },
            source=_SOURCE,
            priority=1,
        ),
        # ── 3. Endpoints ───────────────────────────────────────────────────
        ToolDefinition(
            name="list_xdr_endpoints",
            description=(
                "List Cortex XDR endpoints (agents). Filter by hostname, platform "
                "(windows/linux/macos/android), endpoint_status "
                "(connected/disconnected/lost/uninstalled), or isolate "
                "(isolated/unisolated)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "hostname": {"type": "string"},
                    "platform": {"type": "string"},
                    "endpoint_status": {"type": "string"},
                    "isolate": {
                        "type": "string",
                        "enum": ["isolated", "unisolated"],
                    },
                    "limit": {"type": "integer", "default": 25},
                },
                "required": [],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="get_xdr_endpoint",
            description="Get full details of a single Cortex XDR endpoint by endpoint id.",
            input_schema={
                "type": "object",
                "properties": {"endpoint_id": {"type": "string"}},
                "required": ["endpoint_id"],
            },
            source=_SOURCE,
            priority=1,
        ),
        # ── Response actions ───────────────────────────────────────────────
        ToolDefinition(
            name="isolate_xdr_endpoint",
            description="Isolate a Cortex XDR endpoint from the network (containment). Reversible. Optionally link to an incident_id.",
            input_schema={
                "type": "object",
                "properties": {
                    "endpoint_id": {"type": "string"},
                    "incident_id": {"type": "string"},
                },
                "required": ["endpoint_id"],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="unisolate_xdr_endpoint",
            description="Release a Cortex XDR endpoint from network isolation.",
            input_schema={
                "type": "object",
                "properties": {
                    "endpoint_id": {"type": "string"},
                    "incident_id": {"type": "string"},
                },
                "required": ["endpoint_id"],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="scan_xdr_endpoints",
            description="Run a malware scan on specific Cortex XDR endpoints (by id) — or ALL endpoints when no ids are given. Use deliberately.",
            input_schema={
                "type": "object",
                "properties": {
                    "endpoint_ids": {"type": "array", "items": {"type": "string"}},
                    "incident_id": {"type": "string"},
                },
                "required": [],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="quarantine_xdr_file",
            description="Quarantine a malicious file (by SHA256 and path) on a Cortex XDR endpoint.",
            input_schema={
                "type": "object",
                "properties": {
                    "endpoint_id": {"type": "string"},
                    "file_hash": {"type": "string", "description": "File SHA256"},
                    "file_path": {"type": "string"},
                    "incident_id": {"type": "string"},
                },
                "required": ["endpoint_id", "file_hash", "file_path"],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="retrieve_xdr_file",
            description="Retrieve a file from a Cortex XDR endpoint for forensic analysis (specify the platform and absolute file path).",
            input_schema={
                "type": "object",
                "properties": {
                    "endpoint_id": {"type": "string"},
                    "platform": {
                        "type": "string",
                        "enum": ["windows", "linux", "macos"],
                    },
                    "file_path": {"type": "string"},
                },
                "required": ["endpoint_id", "platform", "file_path"],
            },
            source=_SOURCE,
            priority=2,
        ),
        ToolDefinition(
            name="get_xdr_file_retrieval_links",
            description=(
                "Get the per-endpoint download links of the files a "
                "retrieve_xdr_file action took, once it completed. Files taken "
                "from an endpoint can be live malware and Cortex serves them in "
                "an unencrypted ZIP, so they are linked, never stored or read "
                "inline: fetch them into an isolated analysis environment."
            ),
            input_schema={
                "type": "object",
                "properties": {"group_action_id": {"type": "string"}},
                "required": ["group_action_id"],
            },
            source=_SOURCE,
            priority=2,
        ),
        ToolDefinition(
            name="get_xdr_action_status",
            # Polled: the same call later answers a state that moved on its own.
            live_state=True,
            description="Get the per-endpoint status of a Cortex XDR response action (isolate, scan, script, etc.) by its action id.",
            input_schema={
                "type": "object",
                "properties": {"group_action_id": {"type": "string"}},
                "required": ["group_action_id"],
            },
            source=_SOURCE,
            priority=2,
        ),
        # ── 4. Scripts ─────────────────────────────────────────────────────
        ToolDefinition(
            name="list_xdr_scripts",
            description="List Cortex XDR scripts available to run on endpoints (live response / remediation).",
            input_schema={
                "type": "object",
                "properties": {"limit": {"type": "integer", "default": 30}},
                "required": [],
            },
            source=_SOURCE,
            priority=2,
        ),
        ToolDefinition(
            name="download_xdr_script",
            workspace_ingest=True,
            description=(
                "Store a Cortex XDR script's definition (its YAML in a ZIP, by "
                "script id or name) as a file of the conversation (download card "
                "+ file_id to forward, e.g. to another tenant), with the YAML "
                "shown inline."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "script_id": {"type": "string"},
                    "name": {"type": "string", "description": "Script name (instead of script_id)"},
                },
                "required": [],
            },
            source=_SOURCE,
            priority=2,
        ),
        ToolDefinition(
            name="upload_xdr_script",
            description=(
                "Insert or update a Cortex XDR script from a file of the "
                "conversation (attached, generated or downloaded - by file_id): "
                "the script's YAML in a ZIP, as download_xdr_script stores it."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "file_id": {
                        "type": "string",
                        "description": "A ZIP of the script's YAML attached, generated or downloaded in this conversation",
                    },
                },
                "required": ["file_id"],
            },
            source=_SOURCE,
            priority=2,
        ),
        ToolDefinition(
            name="run_xdr_script",
            description="Run a Cortex XDR script (by script_uid from list_xdr_scripts) on one or more endpoints. Returns an action id to track.",
            input_schema={
                "type": "object",
                "properties": {
                    "script_uid": {"type": "string"},
                    "endpoint_ids": {"type": "array", "items": {"type": "string"}},
                    "timeout_seconds": {"type": "integer", "default": 600},
                    "parameters": {
                        "type": "object",
                        "description": "Script parameter values (name → value)",
                    },
                },
                "required": ["script_uid", "endpoint_ids"],
            },
            source=_SOURCE,
            priority=2,
        ),
        ToolDefinition(
            name="get_xdr_script_results",
            description=(
                "Get the results (output) of a Cortex XDR script execution by its "
                "action id, with the signed download link (10 minutes) of the "
                "files the script retrieved from each endpoint. Those files can "
                "be live malware and Cortex serves them in an unencrypted ZIP, so "
                "they are linked, never stored or read inline."
            ),
            input_schema={
                "type": "object",
                "properties": {"action_id": {"type": "string"}},
                "required": ["action_id"],
            },
            source=_SOURCE,
            priority=2,
        ),
        # ── 5. XQL (threat hunting) ────────────────────────────────────────
        ToolDefinition(
            name="run_xdr_xql_query",
            description=(
                "Run an XQL (XDR Query Language) query for threat hunting across "
                "XDR data and wait for results. Example: 'dataset = xdr_data | "
                "filter action_process_image_name = \"powershell.exe\" | fields "
                "agent_hostname, action_process_image_command_line | limit 50'. "
                "since_hours sets the lookback window."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "XQL query string"},
                    "since_hours": {
                        "type": "integer",
                        "description": "Lookback window in hours",
                        "default": 24,
                    },
                },
                "required": ["query"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="get_xdr_xql_results",
            description="Fetch results for a previously started XQL query by its execution id (use when run_xdr_xql_query was still pending).",
            input_schema={
                "type": "object",
                "properties": {"query_id": {"type": "string"}},
                "required": ["query_id"],
            },
            source=_SOURCE,
            priority=2,
        ),
        # ── 6. Threat Intelligence — hash block / allow ────────────────────
        ToolDefinition(
            name="blocklist_xdr_files",
            description=(
                "Add file SHA256 hashes to the Cortex XDR BLOCK list so they are "
                "blocked from executing across endpoints (CREATING INTELLIGENCE / "
                "response). Optionally tie to an incident."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "hashes": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "File SHA256 hashes",
                    },
                    "comment": {"type": "string"},
                    "incident_id": {"type": "string"},
                },
                "required": ["hashes"],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="allowlist_xdr_files",
            description="Add file SHA256 hashes to the Cortex XDR ALLOW list (permit execution / suppress false positives).",
            input_schema={
                "type": "object",
                "properties": {
                    "hashes": {"type": "array", "items": {"type": "string"}},
                    "comment": {"type": "string"},
                    "incident_id": {"type": "string"},
                },
                "required": ["hashes"],
            },
            source=_SOURCE,
            priority=2,
        ),
        # ── 7. Tags + audit ────────────────────────────────────────────────
        ToolDefinition(
            name="manage_xdr_endpoint_tag",
            description="Assign or remove a tag on Cortex XDR endpoints (for grouping and policy targeting).",
            input_schema={
                "type": "object",
                "properties": {
                    "endpoint_ids": {"type": "array", "items": {"type": "string"}},
                    "tag": {"type": "string"},
                    "action": {
                        "type": "string",
                        "enum": ["assign", "remove"],
                        "default": "assign",
                    },
                },
                "required": ["endpoint_ids", "tag"],
            },
            source=_SOURCE,
            priority=2,
        ),
        ToolDefinition(
            name="get_xdr_audit_logs",
            description="Get recent Cortex XDR management audit logs (who did what, when).",
            input_schema={
                "type": "object",
                "properties": {"limit": {"type": "integer", "default": 25}},
                "required": [],
            },
            source=_SOURCE,
            priority=2,
        ),
    ]
