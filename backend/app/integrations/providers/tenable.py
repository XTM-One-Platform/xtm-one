# SPDX-License-Identifier: Apache-2.0

"""Tenable Vulnerability Management integration - Agentic exposure management.

Full-fledged Tenable Vulnerability Management (formerly Tenable.io) integration
covering the vulnerability-management loop for an agentic security team:

  - Vulnerabilities (*exposure*): list aggregated findings (workbenches), get
    plugin detail (CVSS, CVEs, solution), and per-asset vulnerabilities
  - Assets: list and get assets with their exposure (severity counts, OS, IPs)
  - Plugins: look up a Nessus plugin's metadata (CVE, CVSS, solution)
  - Bulk export (*threat hunting / reporting*): async vulnerability export with
    severity/state filters (request -> poll -> first chunk, stored as a JSON
    file of the run)
  - Scans: list, get, launch, and pause/resume/stop scans; export a scan's
    report (PDF / HTML / CSV / .nessus) as a file of the run and import a
    .nessus file of the run as a scan
  - Context: scanners and tags

Authentication
--------------
Tenable authenticates with a pair of **API keys** sent in the ``X-ApiKeys`` HTTP
header (``accessKey=<ak>;secretKey=<sk>``). ``integration_tools._get_token``
assembles the compound ``base_url||access_key||secret_key`` token this builder
parses (base URL = the Tenable cloud host, default ``https://cloud.tenable.com``;
FedRAMP uses ``https://fedcloud.tenable.com``). All calls target ``{base_url}``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

import httpx

from app.core.egress import egress_client
from app.integrations.file_delivery import (
    PersistFileFn,
    deliver_file,
    fetch_file,
    read_pipe_file,
    uploaded_file_answer,
)
from app.integrations.http import (
    format_entity_list,
    parse_compound_token,
    safe_request,
)
from app.services.content_safety import single_line
from app.tools.base import CallableToolExecutor, FileContext, ToolDefinition, ToolResult

logger = logging.getLogger(__name__)

_PROVIDER = "Tenable"
_SOURCE = "integration:tenable"

# Default Tenable Vulnerability Management cloud host. Stored base_url overrides
# this (e.g. https://fedcloud.tenable.com for FedRAMP).
_DEFAULT_BASE_URL = "https://cloud.tenable.com"

# Vulnerability export polling budget (request -> poll status -> fetch chunk).
_EXPORT_POLL_INTERVAL = 3.0
_EXPORT_MAX_POLLS = 12

# Severity name <-> numeric level (Tenable uses 0..4).
_SEVERITY_NAMES: dict[int, str] = {0: "Info", 1: "Low", 2: "Medium", 3: "High", 4: "Critical"}
_SEVERITY_LEVELS: dict[str, int] = {v.lower(): k for k, v in _SEVERITY_NAMES.items()}
_SEVERITY_ICONS: dict[int, str] = {0: "⚪", 1: "🔵", 2: "🟡", 3: "🟠", 4: "🔴"}

# Allowed values for the bulk vulnerability export filters.
_EXPORT_SEVERITIES: frozenset[str] = frozenset({"info", "low", "medium", "high", "critical"})
_EXPORT_STATES: frozenset[str] = frozenset({"open", "reopened", "fixed"})
_SCAN_ACTIONS: frozenset[str] = frozenset({"pause", "resume", "stop"})

# Scan export (``POST /scans/{id}/export``): format -> (extension, content
# type). ``db`` needs a password and is not offered. html / pdf need chapters
# (https://developer.tenable.com/reference/scans-export-request).
_SCAN_EXPORT_FORMATS: dict[str, tuple[str, str]] = {
    "pdf": ("pdf", "application/pdf"),
    "html": ("html", "text/html"),
    "csv": ("csv", "text/csv"),
    "nessus": ("nessus", "application/xml"),
}
_SCAN_EXPORT_CHAPTERS = frozenset(
    {"vuln_hosts_summary", "vuln_by_host", "compliance_exec", "remediations", "vuln_by_plugin", "compliance"}
)
_MAX_FILE_BYTES = 100 * 1024 * 1024


def _sev_name(level: Any) -> str:
    try:
        return _SEVERITY_NAMES.get(int(level), str(level))
    except (TypeError, ValueError):
        return str(level or "?")


def _sev_icon(level: Any) -> str:
    try:
        return _SEVERITY_ICONS.get(int(level), "⚪")
    except (TypeError, ValueError):
        return "⚪"


def _trunc(text: Any, max_len: int = 300) -> str:
    if text is None:
        return ""
    text = str(text).replace("\r", " ").replace("\n", " ").strip()
    return text[:max_len] + ("…" if len(text) > max_len else "")


def _first(value: Any) -> str:
    """Return the first item of a list field (Tenable returns many fields as lists)."""
    if isinstance(value, list):
        return str(value[0]) if value else ""
    return str(value or "")


def _join(value: Any, limit: int = 4) -> str:
    if isinstance(value, list):
        items = [str(v) for v in value[:limit] if v]
        extra = f" (+{len(value) - limit})" if len(value) > limit else ""
        return ", ".join(items) + extra
    return str(value or "")


# ---------------------------------------------------------------------------
# Auth + URL helpers (shared with integration_testing.py)
# ---------------------------------------------------------------------------


def tenable_api_base(base_url: str) -> str:
    """Return the Tenable API base URL (the configured host, no trailing slash)."""
    return (base_url or _DEFAULT_BASE_URL).strip().rstrip("/")


def build_tenable_headers(access_key: str, secret_key: str) -> dict[str, str]:
    """Build Tenable request headers (X-ApiKeys access/secret key pair).

    The keys are stripped so trailing/leading whitespace (common when pasting
    credentials) can never produce a malformed ``X-ApiKeys`` header. This is the
    single chokepoint shared by the tool builder and the connectivity test.
    """
    return {
        "X-ApiKeys": f"accessKey={access_key.strip()};secretKey={secret_key.strip()}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


# ---------------------------------------------------------------------------
# Tool builder
# ---------------------------------------------------------------------------


def build_tools(
    token: str,
    executor: CallableToolExecutor,
    persist_file_fn: PersistFileFn | None = None,
    **_kwargs: Any,
) -> list[ToolDefinition]:
    """Build Tenable tools from a ``base_url||access_key||secret_key`` compound token.

    ``persist_file_fn`` (injected by ``integration_tools``) stores an export
    (a vulnerability export chunk, a scan report) as a file of the run.
    """
    # ``parse_compound_token`` does not normalise - strip each segment so pasted
    # whitespace can't yield a malformed base URL / ``X-ApiKeys`` header, and so the
    # empty-credential guard below treats a whitespace-only key as missing.
    base_url, access_key, secret_key = (
        part.strip() for part in parse_compound_token(token, 3, greedy_last=True)
    )
    if not access_key or not secret_key:
        return []

    api = tenable_api_base(base_url)
    headers = build_tenable_headers(access_key, secret_key)

    def _client(timeout: int = 30) -> httpx.AsyncClient:
        return egress_client(timeout=timeout)

    # ══════════════════════════════════════════════════════════════════════
    # 0. CONNECTION
    # ══════════════════════════════════════════════════════════════════════

    async def get_tenable_status() -> str:
        """Validate the Tenable connection and report the authenticated user."""
        async with _client() as client:
            resp, err = await safe_request(
                client, "get", f"{api}/session", _PROVIDER, "get session", headers=headers
            )
            if err:
                return err
            s = resp.json() or {}
            return (
                f"Connected to Tenable at {api}.\n"
                f"  User: {s.get('username', '?')} ({s.get('name', '?')}) | "
                f"Type: {s.get('type', '?')} | Container: {s.get('container_name', '?')}"
            )

    # ══════════════════════════════════════════════════════════════════════
    # 1. VULNERABILITIES (exposure)
    # ══════════════════════════════════════════════════════════════════════

    async def list_tenable_vulnerabilities(
        severity: str = "",
        plugin_family: str = "",
        days: int = 30,
        limit: int = 25,
    ) -> str:
        """List aggregated vulnerabilities (workbenches), sorted by severity then instance count."""
        params: dict[str, Any] = {"date_range": max(1, int(days or 30))}
        async with _client(timeout=60) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{api}/workbenches/vulnerabilities",
                _PROVIDER,
                "list vulnerabilities",
                headers=headers,
                params=params,
            )
            if err:
                return err
            vulns = (resp.json() or {}).get("vulnerabilities", [])

        sev_level = _SEVERITY_LEVELS.get(severity.lower().strip()) if severity else None
        if sev_level is not None:
            vulns = [v for v in vulns if v.get("severity") == sev_level]
        if plugin_family:
            pf = plugin_family.lower()
            vulns = [v for v in vulns if pf in str(v.get("plugin_family", "")).lower()]
        vulns.sort(
            key=lambda v: (v.get("severity", 0), v.get("count", 0)), reverse=True
        )
        total = len(vulns)
        shown = vulns[: max(1, int(limit or 25))]

        def _fmt(v: dict) -> str:
            return (
                f"  {_sev_icon(v.get('severity'))} [{_sev_name(v.get('severity'))}] "
                f"plugin {v.get('plugin_id', '?')}: {_trunc(v.get('plugin_name'), 90)}\n"
                f"    Family: {v.get('plugin_family', '?')} | Instances: {v.get('count', 0)} | "
                f"State: {v.get('vulnerability_state', '?')}"
            )

        out = format_entity_list(shown, _fmt, noun="vulnerability", total=total)
        if total > len(shown):
            out += f"\n[Showing top {len(shown)} of {total} - refine with severity/plugin_family or raise limit]"
        return out

    async def get_tenable_vulnerability(plugin_id: int) -> str:
        """Get full detail for a vulnerability/plugin (CVSS, CVEs, synopsis, solution)."""
        async with _client() as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{api}/workbenches/vulnerabilities/{plugin_id}/info",
                _PROVIDER,
                f"get vulnerability {plugin_id}",
                headers=headers,
            )
            if err:
                return err
            info = (resp.json() or {}).get("info", {})
        pd = info.get("plugin_details", {})
        risk = info.get("risk_information", {})
        refs = info.get("reference_information", []) or []
        cves = []
        for r in refs:
            if str(r.get("name", "")).lower() == "cve":
                cves.extend(r.get("values", []) or [])
        counts = info.get("count", info.get("vuln_count", "?"))
        lines = [
            f"Plugin {plugin_id}: {pd.get('name', info.get('plugin_name', '?'))}",
            f"Severity: {_sev_icon(pd.get('severity'))} {_sev_name(pd.get('severity'))} | "
            f"Family: {pd.get('family', '?')} | Type: {pd.get('type', '?')}",
            f"CVSSv3: {risk.get('cvss3_base_score', 'N/A')} | CVSSv2: {risk.get('cvss_base_score', 'N/A')} | "
            f"Risk factor: {risk.get('risk_factor', 'N/A')}",
            f"Affected instances: {counts}",
        ]
        if cves:
            lines.append(f"CVEs: {', '.join(cves[:15])}")
        if info.get("synopsis"):
            lines.append(f"\nSynopsis: {_trunc(info.get('synopsis'), 400)}")
        if info.get("description"):
            lines.append(f"\nDescription: {_trunc(info.get('description'), 600)}")
        if info.get("solution"):
            lines.append(f"\nSolution: {_trunc(info.get('solution'), 400)}")
        return "\n".join(lines)

    async def get_tenable_plugin(plugin_id: int) -> str:
        """Look up a Nessus plugin's metadata (CVE, CVSS, risk factor, solution)."""
        async with _client() as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{api}/plugins/plugin/{plugin_id}",
                _PROVIDER,
                f"get plugin {plugin_id}",
                headers=headers,
            )
            if err:
                return err
            data = resp.json() or {}
        attrs = {
            a.get("attribute_name"): a.get("attribute_value")
            for a in (data.get("attributes") or [])
        }
        lines = [
            f"Plugin {data.get('id', plugin_id)}: {data.get('name', '?')}",
            f"Family: {data.get('family_name', '?')} | Risk: {attrs.get('risk_factor', 'N/A')} | "
            f"CVSSv3: {attrs.get('cvss3_base_score', 'N/A')} | CVSSv2: {attrs.get('cvss_base_score', 'N/A')}",
        ]
        if attrs.get("cve"):
            lines.append(f"CVE: {attrs.get('cve')}")
        if attrs.get("synopsis"):
            lines.append(f"\nSynopsis: {_trunc(attrs.get('synopsis'), 400)}")
        if attrs.get("solution"):
            lines.append(f"\nSolution: {_trunc(attrs.get('solution'), 400)}")
        return "\n".join(lines)

    # ══════════════════════════════════════════════════════════════════════
    # 2. ASSETS
    # ══════════════════════════════════════════════════════════════════════

    async def list_tenable_assets(days: int = 30, limit: int = 25) -> str:
        """List assets (workbenches) with their exposure summary."""
        params = {"date_range": max(1, int(days or 30))}
        async with _client(timeout=60) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{api}/workbenches/assets",
                _PROVIDER,
                "list assets",
                headers=headers,
                params=params,
            )
            if err:
                return err
            data = resp.json() or {}
        assets = data.get("assets", [])
        total = data.get("total", len(assets))
        shown = assets[: max(1, int(limit or 25))]

        def _fmt(a: dict) -> str:
            sev = {s.get("name", "?"): s.get("count", 0) for s in (a.get("severities") or [])}
            host = _first(a.get("fqdn")) or _first(a.get("netbios_name")) or _first(a.get("ipv4")) or "?"
            return (
                f"  asset {a.get('id', '?')} {host}\n"
                f"    IPs: {_join(a.get('ipv4')) or 'N/A'} | OS: {_trunc(_first(a.get('operating_system')), 50) or 'N/A'}\n"
                f"    Vulns - Critical: {sev.get('Critical', 0)} High: {sev.get('High', 0)} "
                f"Medium: {sev.get('Medium', 0)} Low: {sev.get('Low', 0)} | Last seen: {str(a.get('last_seen', ''))[:19]}"
            )

        return format_entity_list(shown, _fmt, noun="asset", total=total)

    async def get_tenable_asset(asset_id: str) -> str:
        """Get full detail of an asset by its UUID (identity, OS, exposure counts, tags)."""
        async with _client() as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{api}/workbenches/assets/{asset_id}/info",
                _PROVIDER,
                f"get asset {asset_id}",
                headers=headers,
            )
            if err:
                return err
            info = (resp.json() or {}).get("info", {})
        counts = (info.get("counts", {}).get("vulnerabilities", {}) or {})
        sev = {s.get("name", "?"): s.get("count", 0) for s in (counts.get("severities") or [])}
        tags = ", ".join(
            f"{t.get('tag_key')}={t.get('tag_value')}" for t in (info.get("tags") or [])[:8]
        )
        lines = [
            f"Asset {asset_id}",
            f"FQDN: {_join(info.get('fqdn')) or 'N/A'} | IPv4: {_join(info.get('ipv4')) or 'N/A'}",
            f"OS: {_trunc(_first(info.get('operating_system')), 80) or 'N/A'}",
            f"System type: {_first(info.get('system_type')) or '?'} | "
            f"Last seen: {str(info.get('last_seen', ''))[:19]} | "
            f"Last scanned: {str(info.get('last_scan_time', ''))[:19]}",
            f"Vulnerabilities - Total: {counts.get('total', 0)} | "
            f"Critical: {sev.get('Critical', 0)} High: {sev.get('High', 0)} "
            f"Medium: {sev.get('Medium', 0)} Low: {sev.get('Low', 0)}",
        ]
        if tags:
            lines.append(f"Tags: {tags}")
        return "\n".join(lines)

    async def list_tenable_asset_vulnerabilities(
        asset_id: str, severity: str = "", limit: int = 25
    ) -> str:
        """List the vulnerabilities found on a specific asset (by UUID)."""
        async with _client(timeout=60) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{api}/workbenches/assets/{asset_id}/vulnerabilities",
                _PROVIDER,
                f"list vulnerabilities for asset {asset_id}",
                headers=headers,
            )
            if err:
                return err
            vulns = (resp.json() or {}).get("vulnerabilities", [])
        sev_level = _SEVERITY_LEVELS.get(severity.lower().strip()) if severity else None
        if sev_level is not None:
            vulns = [v for v in vulns if v.get("severity") == sev_level]
        vulns.sort(key=lambda v: (v.get("severity", 0), v.get("count", 0)), reverse=True)
        total = len(vulns)
        shown = vulns[: max(1, int(limit or 25))]

        def _fmt(v: dict) -> str:
            return (
                f"  {_sev_icon(v.get('severity'))} [{_sev_name(v.get('severity'))}] "
                f"plugin {v.get('plugin_id', '?')}: {_trunc(v.get('plugin_name'), 90)} "
                f"(x{v.get('count', 0)}, {v.get('vulnerability_state', '?')})"
            )

        return format_entity_list(shown, _fmt, noun="vulnerability", total=total)

    # ══════════════════════════════════════════════════════════════════════
    # 3. BULK EXPORT (threat hunting / reporting)
    # ══════════════════════════════════════════════════════════════════════

    async def export_tenable_vulnerabilities(
        severities: str = "critical,high",
        state: str = "open,reopened",
        days: int = 30,
        limit: int = 50,
    ) -> str | ToolResult:
        """Bulk-export vulnerabilities matching filters (severity/state/age) and return the first chunk.

        Use for environment-wide queries like "all critical and high open
        findings". severities/state are comma-separated. Runs an async export
        (request -> poll -> download first chunk); the chunk is stored as a
        JSON file of the run.
        """
        sev_list = [
            s.strip().lower() for s in severities.split(",") if s.strip().lower() in _EXPORT_SEVERITIES
        ] or ["critical", "high"]
        state_list = [
            s.strip().lower() for s in state.split(",") if s.strip().lower() in _EXPORT_STATES
        ] or ["open", "reopened"]
        since = int(time.time()) - max(1, int(days or 30)) * 86400
        body = {
            "num_assets": 100,
            "filters": {"severity": sev_list, "state": state_list, "since": since},
        }
        async with _client(timeout=60) as client:
            start_resp, start_err = await safe_request(
                client,
                "post",
                f"{api}/vulns/export",
                _PROVIDER,
                "start vulnerability export",
                headers=headers,
                json=body,
            )
            if start_err:
                return start_err
            export_uuid = (start_resp.json() or {}).get("export_uuid")
            if not export_uuid:
                return "Error: Tenable did not return an export_uuid."

            chunks: list[int] = []
            status = ""
            for _ in range(_EXPORT_MAX_POLLS):
                await asyncio.sleep(_EXPORT_POLL_INTERVAL)
                st_resp, st_err = await safe_request(
                    client,
                    "get",
                    f"{api}/vulns/export/{export_uuid}/status",
                    _PROVIDER,
                    "poll export status",
                    headers=headers,
                )
                if st_err:
                    return st_err
                st = st_resp.json() or {}
                status = st.get("status", "")
                chunks = st.get("chunks_available", []) or []
                if status in ("ERROR", "CANCELLED"):
                    return f"Tenable export {export_uuid} ended with status {status}."
                if chunks:
                    break

            if not chunks:
                return (
                    f"Tenable export {export_uuid} is still {status or 'processing'} - "
                    "no chunks ready yet. Try a narrower filter or retry shortly."
                )

            chunk_resp, chunk_err = await safe_request(
                client,
                "get",
                f"{api}/vulns/export/{export_uuid}/chunks/{chunks[0]}",
                _PROVIDER,
                "download export chunk",
                headers=headers,
            )
            if chunk_err:
                return chunk_err
            findings = chunk_resp.json() or []

        shown = findings[: max(1, int(limit or 50))]

        def _fmt(f: dict) -> str:
            plugin = f.get("plugin", {})
            asset = f.get("asset", {})
            host = asset.get("hostname") or asset.get("fqdn") or asset.get("ipv4") or asset.get("uuid", "?")
            return (
                f"  {_sev_icon(plugin.get('severity'))} [{_sev_name(plugin.get('severity'))}] "
                f"{host} - plugin {plugin.get('id', '?')}: {_trunc(plugin.get('name'), 80)} "
                f"({f.get('state', '?')})"
            )

        out = format_entity_list(
            shown, _fmt, noun="finding", total=len(findings),
            empty_msg="Export completed with 0 matching findings.",
        )
        if len(findings) > len(shown):
            out += f"\n[Showing {len(shown)} of {len(findings)} in the first chunk; {len(chunks)} chunk(s) available]"
        if not findings:
            return out
        return await deliver_file(
            data=json.dumps(findings, ensure_ascii=False).encode("utf-8"),
            filename=f"tenable-vulns-{export_uuid}-chunk-{chunks[0]}.json",
            content_type="application/json",
            source=_PROVIDER,
            persist_file_fn=persist_file_fn,
            links=[("Download URL", f"{api}/vulns/export/{export_uuid}/chunks/{chunks[0]}")],
            details=[out],
        )

    # ══════════════════════════════════════════════════════════════════════
    # 4. SCANS
    # ══════════════════════════════════════════════════════════════════════

    async def list_tenable_scans(limit: int = 25) -> str:
        """List configured scans with their status and last run time."""
        async with _client() as client:
            resp, err = await safe_request(
                client, "get", f"{api}/scans", _PROVIDER, "list scans", headers=headers
            )
            if err:
                return err
            scans = (resp.json() or {}).get("scans", []) or []
        scans.sort(key=lambda s: s.get("last_modification_date", 0), reverse=True)
        shown = scans[: max(1, int(limit or 25))]

        def _fmt(s: dict) -> str:
            return (
                f"  scan {s.get('id', '?')} {_trunc(s.get('name'), 70)} - "
                f"status: {s.get('status', '?')} | owner: {s.get('owner', '?')} | "
                f"enabled: {s.get('enabled', '?')}"
            )

        return format_entity_list(shown, _fmt, noun="scan", total=len(scans))

    async def get_tenable_scan(scan_id: int) -> str:
        """Get a scan's status and a summary of its results (host/severity counts)."""
        async with _client() as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{api}/scans/{scan_id}",
                _PROVIDER,
                f"get scan {scan_id}",
                headers=headers,
            )
            if err:
                return err
            data = resp.json() or {}
        info = data.get("info", {})
        hosts = data.get("hosts", []) or []
        lines = [
            f"Scan {scan_id}: {info.get('name', '?')}",
            f"Status: {info.get('status', '?')} | Policy: {info.get('policy', '?')} | "
            f"Targets: {_trunc(info.get('targets'), 80)}",
            f"Hosts scanned: {info.get('hostcount', len(hosts))} | "
            f"Start: {str(info.get('scan_start', ''))[:19]} | End: {str(info.get('scan_end', ''))[:19]}",
        ]
        top = sorted(
            hosts, key=lambda h: h.get("critical", 0) + h.get("high", 0), reverse=True
        )[:8]
        if top:
            lines.append("Top hosts by exposure:")
            for h in top:
                lines.append(
                    f"  {h.get('hostname', '?')} - C:{h.get('critical', 0)} H:{h.get('high', 0)} "
                    f"M:{h.get('medium', 0)} L:{h.get('low', 0)}"
                )
        return "\n".join(lines)

    async def launch_tenable_scan(scan_id: int) -> str:
        """Launch (run) a configured scan now. Returns the new scan run UUID."""
        async with _client() as client:
            resp, err = await safe_request(
                client,
                "post",
                f"{api}/scans/{scan_id}/launch",
                _PROVIDER,
                f"launch scan {scan_id}",
                headers=headers,
            )
            if err:
                return err
            uuid = (resp.json() or {}).get("scan_uuid", "?")
        return f"Scan {scan_id} launched - run UUID: {uuid}."

    async def manage_tenable_scan(scan_id: int, action: str) -> str:
        """Control a running scan: pause, resume, or stop it."""
        act = action.lower().strip()
        if act not in _SCAN_ACTIONS:
            return f"Error: action must be one of {', '.join(sorted(_SCAN_ACTIONS))}."
        async with _client() as client:
            _, err = await safe_request(
                client,
                "post",
                f"{api}/scans/{scan_id}/{act}",
                _PROVIDER,
                f"{act} scan {scan_id}",
                headers=headers,
            )
            if err:
                return err
        return f"Scan {scan_id} {act} request accepted."

    async def export_tenable_scan(
        scan_id: int,
        format: str = "pdf",
        chapters: str = "vuln_hosts_summary",
        history_id: int = 0,
    ) -> str | ToolResult:
        """Export a scan's results (PDF / HTML / CSV / .nessus) and store the report as a file."""
        fmt = (format or "pdf").strip().lower()
        if fmt not in _SCAN_EXPORT_FORMATS:
            return f"Error: format must be one of {', '.join(_SCAN_EXPORT_FORMATS)}."
        body: dict[str, Any] = {"format": fmt}
        if fmt in ("pdf", "html"):
            wanted = [c.strip() for c in (chapters or "").replace(",", ";").split(";") if c.strip()]
            unknown = [c for c in wanted if c not in _SCAN_EXPORT_CHAPTERS]
            if unknown or not wanted:
                return (
                    "Error: chapters must be one or more of "
                    f"{', '.join(sorted(_SCAN_EXPORT_CHAPTERS))} (semicolon-separated)."
                )
            body["chapters"] = ";".join(wanted)
        params = {"history_id": int(history_id)} if int(history_id or 0) > 0 else None
        base = f"{api}/scans/{int(scan_id)}/export"
        async with _client(timeout=120) as client:
            resp, err = await safe_request(
                client,
                "post",
                base,
                _PROVIDER,
                f"export scan {scan_id}",
                headers=headers,
                json=body,
                params=params,
            )
            if err:
                return err
            file_ref = str((resp.json() or {}).get("file") or "")
            if not file_ref:
                return "Error: Tenable did not return an export file id."
            status = ""
            for _ in range(_EXPORT_MAX_POLLS):
                await asyncio.sleep(_EXPORT_POLL_INTERVAL)
                st_resp, st_err = await safe_request(
                    client,
                    "get",
                    f"{base}/{file_ref}/status",
                    _PROVIDER,
                    "poll scan export status",
                    headers=headers,
                )
                if st_err:
                    return st_err
                status = str((st_resp.json() or {}).get("status") or "")
                if status == "ready":
                    break
                if status == "error":
                    return f"Tenable could not export scan {scan_id} (status: error)."
            else:
                return (
                    f"The {fmt} export of scan {scan_id} is still {status or 'loading'} - "
                    "call the tool again shortly."
                )
            download_url = f"{base}/{file_ref}/download"
            fetched = await fetch_file(
                client,
                "get",
                download_url,
                provider=_PROVIDER,
                action="download scan export",
                max_bytes=_MAX_FILE_BYTES,
                headers={**headers, "Accept": "*/*"},
            )
        if isinstance(fetched, str):
            return f"{fetched}\nDownload URL: {download_url}"
        extension, content_type = _SCAN_EXPORT_FORMATS[fmt]
        return await deliver_file(
            data=fetched.data,
            filename=f"tenable-scan-{int(scan_id)}.{extension}",
            content_type=content_type,
            source=_PROVIDER,
            persist_file_fn=persist_file_fn,
            links=[("Download URL", download_url)],
        )

    async def import_tenable_scan(
        file_id: str, folder_id: int = 0, file_context: FileContext | None = None
    ) -> str | ToolResult:
        """Import a scan file of the run (.nessus) into Tenable (upload, then import)."""
        piped = await read_pipe_file(
            file_context, file_id, max_bytes=_MAX_FILE_BYTES, target=_PROVIDER
        )
        if isinstance(piped, str):
            return ToolResult(tool_call_id="", content=piped, is_error=True)
        data, filename, content_type = piped
        file_id = file_id.strip()
        upload_headers = {k: v for k, v in headers.items() if k != "Content-Type"}
        async with _client(timeout=120) as client:
            resp, err = await safe_request(
                client,
                "post",
                f"{api}/file/upload",
                _PROVIDER,
                f"upload file {file_id}",
                headers=upload_headers,
                files={"Filedata": (filename, data, content_type)},
            )
            if err:
                return err
            uploaded = str((resp.json() or {}).get("fileuploaded") or "")
            if not uploaded:
                return "Error: Tenable did not return the uploaded file name."
            body: dict[str, Any] = {"file": uploaded}
            if int(folder_id or 0) > 0:
                body["folder_id"] = int(folder_id)
            resp, err = await safe_request(
                client,
                "post",
                f"{api}/scans/import",
                _PROVIDER,
                f"import the scan of file {file_id}",
                headers=headers,
                json=body,
            )
            if err:
                return err
        scan = (resp.json() or {}).get("scan") or {}
        # Tenable names the upload and the scan after the file: both go in
        # the block with the stored name.
        return uploaded_file_answer(
            f"Imported file {file_id} into Tenable as scan {scan.get('id', '?')}. "
            "Read it with get_tenable_scan.",
            target=_PROVIDER,
            filename=filename,
            details=[
                f"Uploaded as: {single_line(uploaded)}",
                f"Scan name: {single_line(str(scan.get('name') or uploaded))}",
            ],
        )

    # ══════════════════════════════════════════════════════════════════════
    # 5. CONTEXT - scanners, tags
    # ══════════════════════════════════════════════════════════════════════

    async def list_tenable_scanners() -> str:
        """List the scanners attached to the Tenable container."""
        async with _client() as client:
            resp, err = await safe_request(
                client, "get", f"{api}/scanners", _PROVIDER, "list scanners", headers=headers
            )
            if err:
                return err
            scanners = (resp.json() or {}).get("scanners", []) or []

        def _fmt(s: dict) -> str:
            return (
                f"  scanner {s.get('id', '?')} {s.get('name', '?')} - "
                f"status: {s.get('status', '?')} | type: {s.get('type', '?')} | "
                f"engine: {s.get('engine_version', '?')}"
            )

        return format_entity_list(scanners, _fmt, noun="scanner")

    async def list_tenable_tags(limit: int = 50) -> str:
        """List asset tag values (category:value) used to scope assets and scans."""
        async with _client() as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{api}/tags/values",
                _PROVIDER,
                "list tags",
                headers=headers,
                params={"limit": max(1, min(int(limit or 50), 1000))},
            )
            if err:
                return err
            values = (resp.json() or {}).get("values", []) or []

        def _fmt(t: dict) -> str:
            return (
                f"  {t.get('category_name', '?')}:{t.get('value', '?')} "
                f"(uuid {t.get('uuid', '?')})"
            )

        return format_entity_list(values, _fmt, noun="tag")

    # ══════════════════════════════════════════════════════════════════════
    # REGISTER ALL TOOLS
    # ══════════════════════════════════════════════════════════════════════

    executor.register("get_tenable_status", get_tenable_status)
    executor.register("list_tenable_vulnerabilities", list_tenable_vulnerabilities)
    executor.register("get_tenable_vulnerability", get_tenable_vulnerability)
    executor.register("get_tenable_plugin", get_tenable_plugin)
    executor.register("list_tenable_assets", list_tenable_assets)
    executor.register("get_tenable_asset", get_tenable_asset)
    executor.register("list_tenable_asset_vulnerabilities", list_tenable_asset_vulnerabilities)
    executor.register("export_tenable_vulnerabilities", export_tenable_vulnerabilities)
    executor.register("list_tenable_scans", list_tenable_scans)
    executor.register("get_tenable_scan", get_tenable_scan)
    executor.register("launch_tenable_scan", launch_tenable_scan)
    executor.register("manage_tenable_scan", manage_tenable_scan)
    executor.register("export_tenable_scan", export_tenable_scan)
    executor.register("import_tenable_scan", import_tenable_scan)
    executor.register("list_tenable_scanners", list_tenable_scanners)
    executor.register("list_tenable_tags", list_tenable_tags)

    return [
        ToolDefinition(
            name="get_tenable_status",
            description=(
                "Validate the Tenable Vulnerability Management connection and report "
                "the authenticated user/container. Use first if other Tenable tools fail."
            ),
            input_schema={"type": "object", "properties": {}, "required": []},
            source=_SOURCE,
            priority=2,
        ),
        ToolDefinition(
            name="list_tenable_vulnerabilities",
            description=(
                "List aggregated vulnerabilities across the environment (Tenable "
                "workbenches), sorted by severity then instance count. Filter by "
                "severity (Critical/High/Medium/Low/Info), plugin_family, and a "
                "lookback window (days)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "severity": {
                        "type": "string",
                        "enum": ["Critical", "High", "Medium", "Low", "Info"],
                        "description": "Filter to a single severity",
                    },
                    "plugin_family": {"type": "string", "description": "Substring match on plugin family"},
                    "days": {"type": "integer", "default": 30, "description": "Lookback window in days"},
                    "limit": {"type": "integer", "default": 25},
                },
                "required": [],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="get_tenable_vulnerability",
            description=(
                "Get full detail for a vulnerability by its plugin_id (CVSS v2/v3, "
                "CVEs, risk factor, synopsis, description, remediation/solution)."
            ),
            input_schema={
                "type": "object",
                "properties": {"plugin_id": {"type": "integer"}},
                "required": ["plugin_id"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="get_tenable_plugin",
            description="Look up a Nessus plugin's metadata by id (CVE, CVSS, risk factor, synopsis, solution).",
            input_schema={
                "type": "object",
                "properties": {"plugin_id": {"type": "integer"}},
                "required": ["plugin_id"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="list_tenable_assets",
            description=(
                "List assets (Tenable workbenches) with their exposure summary "
                "(per-severity vuln counts, OS, IPs, last seen)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "days": {"type": "integer", "default": 30, "description": "Lookback window in days"},
                    "limit": {"type": "integer", "default": 25},
                },
                "required": [],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="get_tenable_asset",
            description=(
                "Get full detail of an asset by its UUID (FQDN/IPs, OS, system type, "
                "last seen/scanned, total + per-severity vulnerability counts, tags)."
            ),
            input_schema={
                "type": "object",
                "properties": {"asset_id": {"type": "string", "description": "Asset UUID"}},
                "required": ["asset_id"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="list_tenable_asset_vulnerabilities",
            description="List the vulnerabilities found on a specific asset (by UUID), optionally filtered by severity.",
            input_schema={
                "type": "object",
                "properties": {
                    "asset_id": {"type": "string", "description": "Asset UUID"},
                    "severity": {
                        "type": "string",
                        "enum": ["Critical", "High", "Medium", "Low", "Info"],
                    },
                    "limit": {"type": "integer", "default": 25},
                },
                "required": ["asset_id"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="export_tenable_vulnerabilities",
            description=(
                "Bulk-export vulnerabilities matching filters and return the first chunk "
                "of findings (asset + plugin + state), the whole chunk also stored as "
                "a JSON file of the conversation (download card + file_id to forward). "
                "Use for environment-wide hunts like 'all open critical and high "
                "findings in the last 7 days'. severities/state are comma-separated; "
                "runs an async export job."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "severities": {
                        "type": "string",
                        "default": "critical,high",
                        "description": "Comma-separated: critical,high,medium,low,info",
                    },
                    "state": {
                        "type": "string",
                        "default": "open,reopened",
                        "description": "Comma-separated: open,reopened,fixed",
                    },
                    "days": {"type": "integer", "default": 30, "description": "Only findings seen in the last N days"},
                    "limit": {"type": "integer", "default": 50},
                },
                "required": [],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="list_tenable_scans",
            description="List configured scans with status, owner, and enabled state (most recently modified first).",
            input_schema={
                "type": "object",
                "properties": {"limit": {"type": "integer", "default": 25}},
                "required": [],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="get_tenable_scan",
            description="Get a scan's status and a summary of its results, including the top hosts by exposure.",
            input_schema={
                "type": "object",
                "properties": {"scan_id": {"type": "integer"}},
                "required": ["scan_id"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="launch_tenable_scan",
            description="Launch (run) a configured scan now. Returns the new scan run UUID.",
            input_schema={
                "type": "object",
                "properties": {"scan_id": {"type": "integer"}},
                "required": ["scan_id"],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="manage_tenable_scan",
            description="Control a running scan: pause, resume, or stop it.",
            input_schema={
                "type": "object",
                "properties": {
                    "scan_id": {"type": "integer"},
                    "action": {"type": "string", "enum": ["pause", "resume", "stop"]},
                },
                "required": ["scan_id", "action"],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="export_tenable_scan",
            description=(
                "Export a scan's results and store the report as a file of the "
                "conversation (download card + file_id to forward, read inline): "
                "pdf or html (with chapters, up to 60 days after the scan), csv, "
                "or nessus (the format another Tenable or Nessus imports). "
                "history_id picks a past run of the scan."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "scan_id": {"type": "integer"},
                    "format": {"type": "string", "enum": list(_SCAN_EXPORT_FORMATS), "default": "pdf"},
                    "chapters": {
                        "type": "string",
                        "default": "vuln_hosts_summary",
                        "description": (
                            "pdf / html only, semicolon-separated: vuln_hosts_summary, "
                            "vuln_by_host, vuln_by_plugin, remediations, compliance_exec, compliance"
                        ),
                    },
                    "history_id": {"type": "integer", "default": 0, "description": "A past run (0 = latest)"},
                },
                "required": ["scan_id"],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="import_tenable_scan",
            description=(
                "Import a scan file of the conversation (a .nessus export - "
                "attached, generated or downloaded, by file_id) into Tenable "
                "Vulnerability Management as a new scan (uploaded, then "
                "imported), optionally into a folder."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "file_id": {
                        "type": "string",
                        "description": "A file attached, generated or downloaded in this conversation",
                    },
                    "folder_id": {"type": "integer", "default": 0},
                },
                "required": ["file_id"],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="list_tenable_scanners",
            description="List the scanners attached to the Tenable container (status, type, engine version).",
            input_schema={"type": "object", "properties": {}, "required": []},
            source=_SOURCE,
            priority=2,
        ),
        ToolDefinition(
            name="list_tenable_tags",
            description="List asset tag values (category:value) used to scope assets and scans.",
            input_schema={
                "type": "object",
                "properties": {"limit": {"type": "integer", "default": 50}},
                "required": [],
            },
            source=_SOURCE,
            priority=2,
        ),
    ]
