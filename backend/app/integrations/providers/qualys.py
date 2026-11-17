# SPDX-License-Identifier: Apache-2.0

"""Qualys VMDR integration - Agentic vulnerability management.

Full-fledged Qualys VMDR (Vulnerability Management, Detection and Response,
formerly QualysGuard VM) integration covering the vulnerability-management loop
for an agentic security team, built on the classic Qualys VM API (``/api/2.0/fo``):

  - Host detections (*exposure*): list vulnerabilities detected on hosts with
    severity / QID / IP filters (the core VMDR finding stream)
  - KnowledgeBase (*context*): full QID detail (title, severity, CVSS v2/v3,
    CVE list, diagnosis, consequence, solution)
  - Assets: list hosts and get a single host's detail (tracking, OS, last scan)
  - Scans: list, fetch results, launch, and pause/resume/cancel/delete
  - Asset groups, scanner appliances, and reports (*context*)

Authentication
--------------
The classic Qualys VM API uses **HTTP Basic authentication** (username +
password) and *requires* the ``X-Requested-With`` header on every
``/api/2.0/fo/`` request (Qualys rejects calls without it). Responses are
**XML**, parsed here with :mod:`xml.etree.ElementTree`.

``integration_tools._get_token`` assembles the compound
``base_url||username||password`` token this builder parses. The base URL is the
customer's Qualys API server for their platform/POD (e.g.
``https://qualysapi.qualys.com`` for US POD1,
``https://qualysapi.qg2.apps.qualys.com`` for US POD2,
``https://qualysapi.qg1.apps.qualys.eu`` for EU POD1). The password (the
credential) lives in ``api_key_enc``; the username and base URL live in
``extra_config``. All calls target ``{base_url}/api/2.0/fo/...``.
"""

from __future__ import annotations

import base64
import logging
import re
import xml.etree.ElementTree as ET
from typing import Any

import httpx

from app.core.egress import egress_client
from app.integrations.http import (
    format_entity_list,
    parse_compound_token,
    safe_request,
)
from app.services.branding import get_cached_branding
from app.tools.base import CallableToolExecutor, ToolDefinition

logger = logging.getLogger(__name__)

_PROVIDER = "Qualys"
_SOURCE = "integration:qualys"

# Fallback Qualys API server (US Platform 1). The real POD URL is customer
# specific and is always stored as base_url; this default only keeps tool
# introspection (the dummy token) from producing an empty host.
_DEFAULT_BASE_URL = "https://qualysapi.qualys.com"

# Upper bound on the number of records requested in a single call via Qualys'
# ``truncation_limit`` so an unbounded caller limit can't pull an enormous XML
# document (the host detection stream can be very large).
_MAX_TRUNCATION = 1000

# Qualys severity is a 1..5 scale (5 = most severe) shared by host detections
# (DETECTION/SEVERITY) and the KnowledgeBase (VULN/SEVERITY_LEVEL).
_SEVERITY_NAMES: dict[int, str] = {1: "Info", 2: "Low", 3: "Medium", 4: "High", 5: "Critical"}
_SEVERITY_ICONS: dict[int, str] = {1: "⚪", 2: "🔵", 3: "🟡", 4: "🟠", 5: "🔴"}

# Allowed scan lifecycle actions for the consolidated manage tool.
_SCAN_ACTIONS: frozenset[str] = frozenset({"pause", "resume", "cancel", "delete"})


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


def _clean(text: Any, max_len: int = 300) -> str:
    """Strip HTML tags / whitespace from a Qualys CDATA field and truncate.

    KnowledgeBase fields (DIAGNOSIS, CONSEQUENCE, SOLUTION) and some results are
    HTML inside CDATA; render them as compact plain text for the agent.
    """
    if text is None:
        return ""
    text = re.sub(r"<[^>]+>", " ", str(text))
    text = re.sub(r"\s+", " ", text).strip()
    return text[:max_len] + ("…" if len(text) > max_len else "")


def _clamp(value: Any, default: int, lo: int = 1, hi: int = _MAX_TRUNCATION) -> int:
    try:
        return max(lo, min(int(value), hi))
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# Auth + URL helpers (shared with integration_testing.py)
# ---------------------------------------------------------------------------


def qualys_api_base(base_url: str) -> str:
    """Return the Qualys API host base URL (no trailing slash).

    The value is stripped first: a base URL pasted with a trailing newline/space
    would otherwise yield an invalid host. Falls back to the default POD when
    empty/whitespace. Endpoints are appended as ``/api/2.0/fo/...``.
    """
    return (base_url or "").strip().rstrip("/") or _DEFAULT_BASE_URL


def build_qualys_headers(
    username: str, password: str, requested_with: str = ""
) -> dict[str, str]:
    """Build Qualys request headers: HTTP Basic auth + the mandatory X-Requested-With.

    Every classic Qualys VM API (``/api/2.0/fo/``) request requires the
    ``X-Requested-With`` header; Qualys only checks that it is present (the value
    identifies the calling application in Qualys' logs). The username and password
    are stripped: a credential pasted with a trailing newline/space would otherwise
    produce a Basic auth header that always fails. The ``X-Requested-With`` value is
    coerced to ASCII (with a fixed fallback) because the admin-configurable branding
    name may contain characters httpx/h11 cannot encode into an HTTP header value,
    which would otherwise break every request on a branded instance.
    """
    basic = base64.b64encode(f"{username.strip()}:{password.strip()}".encode()).decode("ascii")
    # HTTP header values must be latin-1/ASCII-encodable; the branding name is
    # admin-configurable and may not be, so coerce to ASCII with a safe fallback.
    requested_with = (requested_with or "").encode("ascii", "ignore").decode("ascii").strip() or "XTM One"
    return {
        "Authorization": f"Basic {basic}",
        "X-Requested-With": requested_with,
        "Accept": "application/xml",
    }


# ---------------------------------------------------------------------------
# XML parsing helpers
# ---------------------------------------------------------------------------


def _xml_root(content: str) -> ET.Element | None:
    """Parse a Qualys XML response body into a root element, or None on failure.

    ``ElementTree`` does not resolve external DTDs/entities, so the DOCTYPE
    Qualys prepends to every response is ignored (and there is no XXE risk).
    """
    try:
        # S314: the body comes from an authenticated, TLS-protected Qualys API
        # (trusted), and stdlib ElementTree does not resolve external
        # entities/DTDs, so there is no practical XXE vector here.
        return ET.fromstring(content)  # noqa: S314
    except ET.ParseError:
        return None


def _txt(elem: ET.Element | None, tag: str, default: str = "") -> str:
    """Return the stripped text of a direct child *tag* of *elem*."""
    if elem is None:
        return default
    child = elem.find(tag)
    if child is None or child.text is None:
        return default
    return child.text.strip()


def _api_error(root: ET.Element | None) -> str | None:
    """Return a formatted error string if the Qualys response is an error envelope.

    Qualys signals API-level errors with a ``<RESPONSE><CODE>..</CODE>
    <TEXT>..</TEXT></RESPONSE>`` (typically inside ``SIMPLE_RETURN``). A
    successful list output has no direct ``RESPONSE/CODE`` child, and a
    successful action (e.g. launch) returns a ``SIMPLE_RETURN`` with ``TEXT`` but
    no ``CODE`` - so keying on ``CODE`` distinguishes errors from successes.
    """
    if root is None:
        return f"Error: {_PROVIDER} returned a non-XML or unparseable response."
    resp = root.find("RESPONSE")
    code = _txt(resp, "CODE")
    if code:
        text = _txt(resp, "TEXT") or "unknown error"
        return f"Error: {_PROVIDER} API error {code}: {text}"
    return None


def _pagination_hint(root: ET.Element) -> str:
    """Return a hint when Qualys truncated the result set (WARNING/URL with id_min)."""
    warning = root.find(".//WARNING")
    if warning is not None and _txt(warning, "URL"):
        return (
            "\n[More results available - Qualys truncated the output. Narrow the "
            "filters (severities/ids/ips) or raise limit to see more.]"
        )
    return ""


# ---------------------------------------------------------------------------
# Tool builder
# ---------------------------------------------------------------------------


def build_tools(
    token: str,
    executor: CallableToolExecutor,
    **_kwargs: Any,
) -> list[ToolDefinition]:
    """Build Qualys tools from a ``base_url||username||password`` compound token."""
    base_url, username, password = parse_compound_token(token, 3, greedy_last=True)
    # ``base_url`` and ``username`` are populated by the 2-part introspection
    # dummy (``https://dummy.example.com||__dummy__``); the password is supplied
    # only at runtime (``_get_token`` returns None unless both username and
    # password are set), so guarding on these two keeps catalog introspection
    # working while still refusing a half-configured integration at runtime.
    if not base_url or not username:
        return []

    api = qualys_api_base(base_url)
    headers = build_qualys_headers(username, password, get_cached_branding().name)

    def _client(timeout: int = 30) -> httpx.AsyncClient:
        return egress_client(timeout=timeout)

    # ══════════════════════════════════════════════════════════════════════
    # 0. CONNECTION
    # ══════════════════════════════════════════════════════════════════════

    async def get_qualys_status() -> str:
        """Validate the Qualys connection and confirm the API host is reachable."""
        async with _client() as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{api}/api/2.0/fo/knowledge_base/vuln/",
                _PROVIDER,
                "validate connection",
                headers=headers,
                params={"action": "list", "ids": "0", "details": "None"},
            )
            if err:
                return err
            root = _xml_root(resp.text)
            api_err = _api_error(root)
            if api_err:
                return api_err
            return (
                f"Connected to Qualys VMDR at {api} (authenticated as '{username}').\n"
                "  Credentials and the X-Requested-With requirement are satisfied."
            )

    # ══════════════════════════════════════════════════════════════════════
    # 1. HOST DETECTIONS (exposure) + KNOWLEDGEBASE (context)
    # ══════════════════════════════════════════════════════════════════════

    async def list_qualys_host_detections(
        severities: str = "",
        qids: str = "",
        ips: str = "",
        ids: str = "",
        limit: int = 20,
    ) -> str:
        """List vulnerabilities detected on hosts (the core VMDR finding stream).

        Filter by severities (Qualys 1-5 scale, e.g. '4,5' or '3-5'), qids, ips,
        or host ids. Each host lists its detected QIDs with severity and status.
        """
        host_limit = _clamp(limit, 20)
        params: dict[str, Any] = {
            "action": "list",
            "show_results": "0",
            "truncation_limit": host_limit,
        }
        if severities:
            params["severities"] = severities
        if qids:
            params["qids"] = qids
        if ips:
            params["ips"] = ips
        if ids:
            params["ids"] = ids

        async with _client(timeout=60) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{api}/api/2.0/fo/asset/host/vm/detection/",
                _PROVIDER,
                "list host detections",
                headers=headers,
                params=params,
            )
            if err:
                return err
        root = _xml_root(resp.text)
        api_err = _api_error(root)
        if api_err:
            return api_err
        hosts = root.findall(".//HOST_LIST/HOST") if root is not None else []

        def _fmt(host: ET.Element) -> str:
            detections = host.findall("DETECTION_LIST/DETECTION")
            detections.sort(key=lambda d: _clamp(_txt(d, "SEVERITY"), 0, lo=0), reverse=True)
            host_id = _txt(host, "ID", "?")
            ip = _txt(host, "IP", "?")
            dns = _txt(host, "DNS")
            os_name = _clean(_txt(host, "OS"), 50)
            head = (
                f"  host #{host_id} {ip}{f' ({dns})' if dns else ''} - "
                f"{len(detections)} detection(s) | OS: {os_name or 'N/A'}"
            )
            lines = [head]
            for det in detections[:5]:
                lines.append(
                    f"    {_sev_icon(_txt(det, 'SEVERITY'))} QID {_txt(det, 'QID', '?')} "
                    f"[{_sev_name(_txt(det, 'SEVERITY'))}] {_txt(det, 'TYPE', '?')} | "
                    f"status: {_txt(det, 'STATUS', '?')} | "
                    f"port: {_txt(det, 'PORT') or '-'}/{_txt(det, 'PROTOCOL') or '-'} | "
                    f"last found: {_txt(det, 'LAST_FOUND_DATETIME') or '?'}"
                )
            if len(detections) > 5:
                lines.append(f"    ... and {len(detections) - 5} more detection(s)")
            return "\n".join(lines)

        out = format_entity_list(
            hosts,
            _fmt,
            noun="host",
            empty_msg="No host detections found for the given filters.",
        )
        if root is not None:
            out += _pagination_hint(root)
        return out

    async def get_qualys_vulnerability(qid: int) -> str:
        """Get full KnowledgeBase detail for a QID (title, severity, CVSS, CVEs, solution)."""
        async with _client() as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{api}/api/2.0/fo/knowledge_base/vuln/",
                _PROVIDER,
                f"get vulnerability QID {qid}",
                headers=headers,
                params={"action": "list", "ids": str(qid), "details": "All"},
            )
            if err:
                return err
        root = _xml_root(resp.text)
        api_err = _api_error(root)
        if api_err:
            return api_err
        vuln = root.find(".//VULN_LIST/VULN") if root is not None else None
        if vuln is None:
            return f"No KnowledgeBase entry found for QID {qid}."

        cves = [
            (cve.findtext("ID") or "").strip()
            for cve in vuln.findall("CVE_LIST/CVE")
            if (cve.findtext("ID") or "").strip()
        ]
        cvss2 = _txt(vuln.find("CVSS"), "BASE") if vuln.find("CVSS") is not None else ""
        cvss3 = _txt(vuln.find("CVSS_V3"), "BASE") if vuln.find("CVSS_V3") is not None else ""
        lines = [
            f"QID {_txt(vuln, 'QID', str(qid))}: {_txt(vuln, 'TITLE', '?')}",
            f"Severity: {_sev_icon(_txt(vuln, 'SEVERITY_LEVEL'))} "
            f"{_sev_name(_txt(vuln, 'SEVERITY_LEVEL'))} | "
            f"Type: {_txt(vuln, 'VULN_TYPE', '?')} | "
            f"Category: {_txt(vuln, 'CATEGORY') or 'N/A'} | "
            f"Patchable: {_txt(vuln, 'PATCHABLE') or '?'}",
            f"CVSS v3: {cvss3 or 'N/A'} | CVSS v2: {cvss2 or 'N/A'}",
            f"Published: {_txt(vuln, 'PUBLISHED_DATETIME') or 'N/A'}",
        ]
        if cves:
            lines.append(f"CVEs: {', '.join(cves[:20])}")
        if _txt(vuln, "DIAGNOSIS"):
            lines.append(f"\nDiagnosis: {_clean(_txt(vuln, 'DIAGNOSIS'), 500)}")
        if _txt(vuln, "CONSEQUENCE"):
            lines.append(f"\nConsequence: {_clean(_txt(vuln, 'CONSEQUENCE'), 400)}")
        if _txt(vuln, "SOLUTION"):
            lines.append(f"\nSolution: {_clean(_txt(vuln, 'SOLUTION'), 500)}")
        return "\n".join(lines)

    # ══════════════════════════════════════════════════════════════════════
    # 2. ASSETS (hosts)
    # ══════════════════════════════════════════════════════════════════════

    def _fmt_host(host: ET.Element) -> str:
        host_id = _txt(host, "ID", "?")
        ip = _txt(host, "IP", "?")
        dns = _txt(host, "DNS")
        netbios = _txt(host, "NETBIOS")
        return (
            f"  host #{host_id} {ip}"
            f"{f' ({dns})' if dns else ''}"
            f"{f' [{netbios}]' if netbios else ''} - "
            f"OS: {_clean(_txt(host, 'OS'), 50) or 'N/A'} | "
            f"tracking: {_txt(host, 'TRACKING_METHOD') or '?'} | "
            f"last scan: {_txt(host, 'LAST_SCAN_DATETIME') or 'never'}"
        )

    async def list_qualys_hosts(ips: str = "", limit: int = 25) -> str:
        """List hosts/assets in the Qualys subscription (optionally filtered by ips)."""
        params: dict[str, Any] = {
            "action": "list",
            "truncation_limit": _clamp(limit, 25),
        }
        if ips:
            params["ips"] = ips
        async with _client(timeout=60) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{api}/api/2.0/fo/asset/host/",
                _PROVIDER,
                "list hosts",
                headers=headers,
                params=params,
            )
            if err:
                return err
        root = _xml_root(resp.text)
        api_err = _api_error(root)
        if api_err:
            return api_err
        hosts = root.findall(".//HOST_LIST/HOST") if root is not None else []
        out = format_entity_list(hosts, _fmt_host, noun="host")
        if root is not None:
            out += _pagination_hint(root)
        return out

    async def get_qualys_host(host_id: int) -> str:
        """Get a single host's detail by its Qualys host ID (identity, OS, tags)."""
        async with _client() as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{api}/api/2.0/fo/asset/host/",
                _PROVIDER,
                f"get host {host_id}",
                headers=headers,
                params={
                    "action": "list",
                    "ids": str(host_id),
                    "details": "All",
                    "show_tags": "1",
                },
            )
            if err:
                return err
        root = _xml_root(resp.text)
        api_err = _api_error(root)
        if api_err:
            return api_err
        host = root.find(".//HOST_LIST/HOST") if root is not None else None
        if host is None:
            return f"No host found with ID {host_id}."
        tags = [
            (tag.findtext("TAG_NAME") or "").strip()
            for tag in host.findall("TAGS/TAG")
            if (tag.findtext("TAG_NAME") or "").strip()
        ]
        lines = [
            f"Host #{_txt(host, 'ID', str(host_id))}: {_txt(host, 'IP', '?')}",
            f"DNS: {_txt(host, 'DNS') or 'N/A'} | NetBIOS: {_txt(host, 'NETBIOS') or 'N/A'}",
            f"OS: {_clean(_txt(host, 'OS'), 80) or 'N/A'}",
            f"Tracking: {_txt(host, 'TRACKING_METHOD') or '?'} | "
            f"Last scan: {_txt(host, 'LAST_SCAN_DATETIME') or 'never'} | "
            f"Last VM scan: {_txt(host, 'LAST_VM_SCANNED_DATE') or 'never'}",
        ]
        if tags:
            lines.append(f"Tags: {', '.join(tags[:15])}")
        return "\n".join(lines)

    # ══════════════════════════════════════════════════════════════════════
    # 3. SCANS
    # ══════════════════════════════════════════════════════════════════════

    async def list_qualys_scans(state: str = "", limit: int = 25) -> str:
        """List VM scans (most recent first). Optionally filter by state (e.g. Running, Finished)."""
        params: dict[str, Any] = {"action": "list"}
        if state:
            params["state"] = state
        async with _client() as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{api}/api/2.0/fo/scan/",
                _PROVIDER,
                "list scans",
                headers=headers,
                params=params,
            )
            if err:
                return err
        root = _xml_root(resp.text)
        api_err = _api_error(root)
        if api_err:
            return api_err
        scans = root.findall(".//SCAN_LIST/SCAN") if root is not None else []
        # Qualys' scan-list API has no server-side row-limit param (only filters
        # like `state`), so the caller's limit is applied client-side here.
        shown = scans[: _clamp(limit, 25)]

        def _fmt(scan: ET.Element) -> str:
            return (
                f"  scan {_txt(scan, 'REF', '?')} - {_txt(scan, 'TITLE', 'Untitled')}\n"
                f"    type: {_txt(scan, 'TYPE') or '?'} | "
                f"state: {_txt(scan.find('STATUS'), 'STATE') or '?'} | "
                f"launched: {_txt(scan, 'LAUNCH_DATETIME') or '?'} | "
                f"by: {_txt(scan, 'USER_LOGIN') or '?'}\n"
                f"    target: {_clean(_txt(scan, 'TARGET'), 120) or 'N/A'}"
            )

        return format_entity_list(shown, _fmt, noun="scan", total=len(scans))

    async def get_qualys_scan_results(scan_ref: str, limit: int = 50) -> str:
        """Fetch the findings of a finished VM scan by its scan reference (e.g. 'scan/123.45').

        Qualys only serves scan results as CSV/JSON (not XML); this fetches the
        JSON form and summarizes the per-host vulnerability findings.
        """
        async with _client(timeout=60) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{api}/api/2.0/fo/scan/",
                _PROVIDER,
                f"fetch scan results {scan_ref}",
                # This call fetches JSON (output_format=json), so override the
                # shared Accept: application/xml to make the intent explicit and
                # avoid any content-negotiation ambiguity.
                headers={**headers, "Accept": "application/json"},
                params={
                    "action": "fetch",
                    "scan_ref": scan_ref,
                    "output_format": "json",
                    "mode": "brief",
                },
            )
            if err:
                return err
            try:
                data = resp.json()
            except (ValueError, TypeError):
                # Could be an XML SIMPLE_RETURN error (e.g. scan not finished).
                api_err = _api_error(_xml_root(resp.text))
                if api_err:
                    return api_err
                text = (resp.text or "").strip()
                return (
                    f"Scan {scan_ref} returned no parseable JSON results "
                    f"(it may still be running or empty)."
                    + (f"\n{text[:500]}" if text else "")
                )

        rows = data if isinstance(data, list) else []
        findings: list[dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            low = {str(k).lower(): v for k, v in row.items()}
            if "qid" in low:
                findings.append(low)

        shown = findings[: _clamp(limit, 50, hi=500)]

        def _fmt(f: dict[str, Any]) -> str:
            host = f.get("ip") or f.get("dns") or f.get("fqdn") or "?"
            return (
                f"  {_sev_icon(f.get('severity'))} [{_sev_name(f.get('severity'))}] "
                f"{host} - QID {f.get('qid', '?')}: {_clean(f.get('title'), 80)} "
                f"(port {f.get('port') or '-'}/{f.get('protocol') or '-'})"
            )

        return format_entity_list(
            shown,
            _fmt,
            noun="finding",
            total=len(findings),
            empty_msg=f"Scan {scan_ref} has no vulnerability findings (or is not finished).",
        )

    async def launch_qualys_scan(
        option_id: int = 0,
        option_title: str = "",
        ip: str = "",
        asset_group_ids: str = "",
        scan_title: str = "",
        iscanner_name: str = "",
    ) -> str:
        """Launch a new VM scan.

        Requires an option profile (option_id OR option_title) and a target
        (ip - comma-separated IPs/ranges - OR asset_group_ids). For internal
        scans, pass iscanner_name (the scanner appliance) or it uses the default.
        Returns the new scan reference (scan/ID) used by get_qualys_scan_results.
        """
        if not option_id and not option_title:
            return "Error: provide an option profile via option_id or option_title."
        if not ip and not asset_group_ids:
            return "Error: provide a target via ip or asset_group_ids."
        data: dict[str, Any] = {"action": "launch"}
        if scan_title:
            data["scan_title"] = scan_title
        if option_id:
            data["option_id"] = option_id
        elif option_title:
            data["option_title"] = option_title
        if ip:
            data["ip"] = ip
        if asset_group_ids:
            data["asset_group_ids"] = asset_group_ids
        if iscanner_name:
            data["iscanner_name"] = iscanner_name

        async with _client(timeout=60) as client:
            resp, err = await safe_request(
                client,
                "post",
                f"{api}/api/2.0/fo/scan/",
                _PROVIDER,
                "launch scan",
                headers=headers,
                data=data,
            )
            if err:
                return err
        root = _xml_root(resp.text)
        api_err = _api_error(root)
        if api_err:
            return api_err
        items = {
            _txt(item, "KEY"): _txt(item, "VALUE")
            for item in (root.findall(".//ITEM_LIST/ITEM") if root is not None else [])
        }
        ref = items.get("REFERENCE", "")
        scan_id = items.get("ID", "")
        text = _txt(root.find("RESPONSE") if root is not None else None, "TEXT")
        if ref:
            return f"Scan launched - reference: {ref}{f' (ID {scan_id})' if scan_id else ''}."
        return text or "Scan launch request accepted."

    async def manage_qualys_scan(action: str, scan_ref: str) -> str:
        """Control a VM scan by reference: pause, resume, cancel, or delete it.

        'delete' permanently removes the scan and its results - this cannot be
        undone. Use list_qualys_scans to find the scan reference (scan/ID).
        """
        # Normalize defensively: the tool executor does not enforce JSON Schema
        # types, so a null/non-string action would otherwise raise AttributeError.
        act = (action or "").strip().lower()
        if act not in _SCAN_ACTIONS:
            return f"Error: action must be one of {', '.join(sorted(_SCAN_ACTIONS))}."
        async with _client() as client:
            resp, err = await safe_request(
                client,
                "post",
                f"{api}/api/2.0/fo/scan/",
                _PROVIDER,
                f"{act} scan {scan_ref}",
                headers=headers,
                data={"action": act, "scan_ref": scan_ref},
            )
            if err:
                return err
        root = _xml_root(resp.text)
        api_err = _api_error(root)
        if api_err:
            return api_err
        text = _txt(root.find("RESPONSE") if root is not None else None, "TEXT")
        return text or f"Scan {scan_ref} {act} request accepted."

    # ══════════════════════════════════════════════════════════════════════
    # 4. CONTEXT - asset groups, scanner appliances, reports
    # ══════════════════════════════════════════════════════════════════════

    async def list_qualys_asset_groups(limit: int = 50) -> str:
        """List asset groups (named host collections used to scope scans and reports)."""
        async with _client() as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{api}/api/2.0/fo/asset/group/",
                _PROVIDER,
                "list asset groups",
                headers=headers,
                params={"action": "list", "truncation_limit": _clamp(limit, 50)},
            )
            if err:
                return err
        root = _xml_root(resp.text)
        api_err = _api_error(root)
        if api_err:
            return api_err
        groups = root.findall(".//ASSET_GROUP_LIST/ASSET_GROUP") if root is not None else []

        def _fmt(grp: ET.Element) -> str:
            ip_count = len(grp.findall("IP_SET/IP")) + len(grp.findall("IP_SET/IP_RANGE"))
            return (
                f"  group #{_txt(grp, 'ID', '?')} {_txt(grp, 'TITLE', 'Untitled')} - "
                f"{ip_count} IP entr{'y' if ip_count == 1 else 'ies'} | "
                f"last update: {_txt(grp, 'LAST_UPDATE') or '?'}"
            )

        out = format_entity_list(groups, _fmt, noun="asset group")
        if root is not None:
            out += _pagination_hint(root)
        return out

    async def list_qualys_scanner_appliances(limit: int = 50) -> str:
        """List scanner appliances (used as the iscanner_name when launching internal scans)."""
        async with _client() as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{api}/api/2.0/fo/appliance/",
                _PROVIDER,
                "list scanner appliances",
                headers=headers,
                params={"action": "list", "truncation_limit": _clamp(limit, 50)},
            )
            if err:
                return err
        root = _xml_root(resp.text)
        api_err = _api_error(root)
        if api_err:
            return api_err
        appliances = root.findall(".//APPLIANCE_LIST/APPLIANCE") if root is not None else []

        def _fmt(app: ET.Element) -> str:
            return (
                f"  appliance #{_txt(app, 'ID', '?')} {_txt(app, 'NAME', 'Unnamed')} - "
                f"status: {_txt(app, 'STATUS') or '?'} | "
                f"running scans: {_txt(app, 'RUNNING_SCAN_COUNT') or '0'} | "
                f"software: {_txt(app, 'SOFTWARE_VERSION') or '?'}"
            )

        return format_entity_list(appliances, _fmt, noun="scanner appliance")

    async def list_qualys_reports(limit: int = 25) -> str:
        """List generated reports (requires the report share feature to be enabled)."""
        async with _client() as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{api}/api/2.0/fo/report/",
                _PROVIDER,
                "list reports",
                headers=headers,
                params={"action": "list"},
            )
            if err:
                return err
        root = _xml_root(resp.text)
        api_err = _api_error(root)
        if api_err:
            return api_err
        reports = root.findall(".//REPORT_LIST/REPORT") if root is not None else []
        # Qualys' report-list API has no server-side row-limit param, so the
        # caller's limit is applied client-side here.
        shown = reports[: _clamp(limit, 25)]

        def _fmt(rep: ET.Element) -> str:
            return (
                f"  report #{_txt(rep, 'ID', '?')} {_txt(rep, 'TITLE', 'Untitled')} - "
                f"type: {_txt(rep, 'TYPE') or '?'} | "
                f"format: {_txt(rep, 'OUTPUT_FORMAT') or '?'} | "
                f"state: {_txt(rep.find('STATUS'), 'STATE') or '?'} | "
                f"launched: {_txt(rep, 'LAUNCH_DATETIME') or '?'}"
            )

        return format_entity_list(shown, _fmt, noun="report", total=len(reports))

    # ══════════════════════════════════════════════════════════════════════
    # REGISTER ALL TOOLS
    # ══════════════════════════════════════════════════════════════════════

    executor.register("get_qualys_status", get_qualys_status)
    executor.register("list_qualys_host_detections", list_qualys_host_detections)
    executor.register("get_qualys_vulnerability", get_qualys_vulnerability)
    executor.register("list_qualys_hosts", list_qualys_hosts)
    executor.register("get_qualys_host", get_qualys_host)
    executor.register("list_qualys_scans", list_qualys_scans)
    executor.register("get_qualys_scan_results", get_qualys_scan_results)
    executor.register("launch_qualys_scan", launch_qualys_scan)
    executor.register("manage_qualys_scan", manage_qualys_scan)
    executor.register("list_qualys_asset_groups", list_qualys_asset_groups)
    executor.register("list_qualys_scanner_appliances", list_qualys_scanner_appliances)
    executor.register("list_qualys_reports", list_qualys_reports)

    return [
        # ── 0. Connection ──────────────────────────────────────────────────
        ToolDefinition(
            name="get_qualys_status",
            description=(
                "Validate the Qualys VMDR connection (HTTP Basic auth + the "
                "required X-Requested-With header) and confirm the API POD is "
                "reachable. Use first if other Qualys tools fail."
            ),
            input_schema={"type": "object", "properties": {}, "required": []},
            source=_SOURCE,
            priority=2,
        ),
        # ── 1. Host detections + KnowledgeBase ─────────────────────────────
        ToolDefinition(
            name="list_qualys_host_detections",
            description=(
                "List vulnerabilities detected on hosts - the core Qualys VMDR "
                "finding stream (Host List VM Detection). Filter by severities "
                "(Qualys 1-5 scale where 5 is most severe, e.g. '5' or '4,5' or "
                "'3-5'), qids (comma-separated QIDs), ips (comma-separated IPs/"
                "ranges), or ids (host IDs). Each host lists its detected QIDs "
                "with severity, type, and status."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "severities": {
                        "type": "string",
                        "description": "Qualys severity 1-5 filter, e.g. '5', '4,5', or '3-5'",
                    },
                    "qids": {"type": "string", "description": "Comma-separated QIDs to filter on"},
                    "ips": {
                        "type": "string",
                        "description": "Comma-separated IPs/ranges, e.g. '10.0.0.1,10.0.0.10-10.0.0.20'",
                    },
                    "ids": {"type": "string", "description": "Comma-separated Qualys host IDs"},
                    "limit": {"type": "integer", "default": 20, "description": "Max hosts to return"},
                },
                "required": [],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="get_qualys_vulnerability",
            description=(
                "Get full Qualys KnowledgeBase detail for a vulnerability by its "
                "QID: title, severity, CVSS v2/v3 base scores, CVE list, "
                "diagnosis, consequence, and remediation/solution."
            ),
            input_schema={
                "type": "object",
                "properties": {"qid": {"type": "integer", "description": "Qualys vulnerability ID (QID)"}},
                "required": ["qid"],
            },
            source=_SOURCE,
            priority=0,
        ),
        # ── 2. Assets ──────────────────────────────────────────────────────
        ToolDefinition(
            name="list_qualys_hosts",
            description=(
                "List hosts/assets in the Qualys subscription (identity, OS, "
                "tracking method, last scan time). Optionally filter by ips "
                "(comma-separated IPs/ranges)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "ips": {"type": "string", "description": "Comma-separated IPs/ranges to filter on"},
                    "limit": {"type": "integer", "default": 25},
                },
                "required": [],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="get_qualys_host",
            description=(
                "Get a single host's detail by its Qualys host ID (IP, DNS, "
                "NetBIOS, OS, tracking method, last scan times, and asset tags)."
            ),
            input_schema={
                "type": "object",
                "properties": {"host_id": {"type": "integer", "description": "Qualys host ID"}},
                "required": ["host_id"],
            },
            source=_SOURCE,
            priority=0,
        ),
        # ── 3. Scans ───────────────────────────────────────────────────────
        ToolDefinition(
            name="list_qualys_scans",
            description=(
                "List VM scans with their reference (scan/ID), title, state, "
                "launch time, owner, and target. Optionally filter by state "
                "(e.g. Running, Finished, Queued, Error, Canceled)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "state": {
                        "type": "string",
                        "description": "Scan state filter, e.g. Running, Finished, Queued, Error, Canceled",
                    },
                    "limit": {"type": "integer", "default": 25},
                },
                "required": [],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="get_qualys_scan_results",
            description=(
                "Fetch the per-host vulnerability findings of a finished VM scan "
                "by its scan reference (e.g. 'scan/1234567890.12345', from "
                "list_qualys_scans). Returns QID, severity, host, and port for "
                "each finding."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "scan_ref": {"type": "string", "description": "Scan reference, e.g. scan/1234567890.12345"},
                    "limit": {"type": "integer", "default": 50},
                },
                "required": ["scan_ref"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="launch_qualys_scan",
            description=(
                "Launch a new VM scan. Requires an option profile (option_id or "
                "option_title) and a target (ip - comma-separated IPs/ranges - or "
                "asset_group_ids). For internal scans, pass iscanner_name (a "
                "scanner appliance from list_qualys_scanner_appliances). Returns "
                "the new scan reference for get_qualys_scan_results."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "option_id": {"type": "integer", "description": "Option profile ID (or use option_title)"},
                    "option_title": {"type": "string", "description": "Option profile title (or use option_id)"},
                    "ip": {"type": "string", "description": "Comma-separated target IPs/ranges"},
                    "asset_group_ids": {
                        "type": "string",
                        "description": "Comma-separated asset group IDs to scan (alternative to ip)",
                    },
                    "scan_title": {"type": "string", "description": "Optional title for the scan"},
                    "iscanner_name": {
                        "type": "string",
                        "description": "Scanner appliance name for internal scans",
                    },
                },
                "required": [],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="manage_qualys_scan",
            description=(
                "Control a VM scan by reference: pause, resume, cancel, or "
                "delete it. 'delete' permanently removes the scan and its results "
                "and cannot be undone."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["pause", "resume", "cancel", "delete"]},
                    "scan_ref": {"type": "string", "description": "Scan reference, e.g. scan/1234567890.12345"},
                },
                "required": ["action", "scan_ref"],
            },
            source=_SOURCE,
            priority=1,
        ),
        # ── 4. Context ─────────────────────────────────────────────────────
        ToolDefinition(
            name="list_qualys_asset_groups",
            description=(
                "List asset groups (named host collections, with their IDs, used "
                "to scope scans and reports via asset_group_ids)."
            ),
            input_schema={
                "type": "object",
                "properties": {"limit": {"type": "integer", "default": 50}},
                "required": [],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="list_qualys_scanner_appliances",
            description=(
                "List scanner appliances (their names are used as iscanner_name "
                "when launching internal scans) with status and software version."
            ),
            input_schema={
                "type": "object",
                "properties": {"limit": {"type": "integer", "default": 50}},
                "required": [],
            },
            source=_SOURCE,
            priority=2,
        ),
        ToolDefinition(
            name="list_qualys_reports",
            description=(
                "List generated reports (ID, title, type, output format, state, "
                "launch time). Requires the Qualys report share feature to be "
                "enabled on the subscription."
            ),
            input_schema={
                "type": "object",
                "properties": {"limit": {"type": "integer", "default": 25}},
                "required": [],
            },
            source=_SOURCE,
            priority=1,
        ),
    ]
