# SPDX-License-Identifier: Apache-2.0

"""Microsoft Defender XDR integration – Agentic SOC for the SecOps team.

Full-fledged Microsoft Defender (Microsoft 365 Defender / Defender XDR)
integration covering the whole detection → investigation → response →
intelligence loop, designed as a best-in-class building block for an
agentic security team.

Tool categories (≈50 tools):
  - Incidents (Graph): list, get (with alerts), update, comment
  - Alerts (Graph): list, get, update, comment
  - Advanced Hunting (Graph): run KQL hunting query across Defender XDR tables
  - Threat Intelligence — Defender TI (Graph): articles, intel profiles
    (threat actors), host enrichment (whois, reputation, passive DNS),
    vulnerability intel (CVE)
  - Threat Indicators / IOCs (MDE): list, create (block/alert/allow), delete
  - Custom detection rules (Graph beta): list, get, create, update, delete
    scheduled KQL detections (CustomDetection.Read.All / ReadWrite.All)
  - Devices / Machines (MDE): list, get, vulnerabilities, logged-on users
  - Response Actions (MDE): isolate / release, AV scan, collect investigation
    package, restrict / unrestrict app execution, stop & quarantine file,
    Live Response library scripts (run, approval-gated, list, result),
    list / get / cancel machine actions
  - Threat & Vulnerability Management (MDE): vulnerabilities, software,
    security recommendations, exposure score, device secure score
  - Entity investigation (MDE): file / IP / domain reputation & stats
  - Secure Score (Graph): tenant secure score + control profiles

Two authentication modes are supported (resolved in ``integration_tools.py``):

* **App-only client credentials** (the default, most robust for unattended
  assignments). The ``tenant_id||client_id||client_secret`` compound token is
  assembled from the integration's credentials by ``integration_tools._get_token()``
  and the module-local ``_get_token`` below exchanges it for a per-audience Azure
  AD access token.
* **Delegated OAuth** (user sign-in). ``build_tools`` receives a ``token_acquirer``
  that mints each audience token by redeeming the integration's refresh token for
  that resource's ``/.default`` scope.

Either way two audiences are used depending on the API surface:
  - Microsoft Graph       → ``https://graph.microsoft.com/.default``
  - Defender for Endpoint → ``https://api.securitycenter.microsoft.com/.default``
"""

from __future__ import annotations

import io
import json
import logging
import re
import zipfile
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import quote as _url_encode

import httpx

from app.core.egress import egress_client
from app.core.utils import format_exception_message
from app.integrations.file_content import (
    keep_file,
    kept_file_line,
    kilobytes,
    rest_of_file_hint,
)
from app.integrations.file_delivery import FetchError
from app.integrations.http import (
    app_token_cache_key,
    format_entity_list,
    get_cached_app_token,
    parse_compound_token,
    safe_request,
    set_cached_app_token,
)
from app.integrations.providers.social_media import fetch_public_media
from app.services.branding import get_cached_branding
from app.tools.base import CallableToolExecutor, FileContext, ToolDefinition, ToolResult

logger = logging.getLogger(__name__)

_PROVIDER = "Microsoft Defender"
_GRAPH_BASE = "https://graph.microsoft.com/v1.0"
# Custom detection rules are only served by the Graph beta endpoint.
_GRAPH_BETA = "https://graph.microsoft.com/beta"
_DETECTION_RULES_URL = f"{_GRAPH_BETA}/security/rules/detectionRules"

# The Graph permission each side of the custom detection rule tools needs.
_DETECTION_READ_PERMISSION = "CustomDetection.Read.All"
_DETECTION_WRITE_PERMISSION = "CustomDetection.ReadWrite.All"

# Schedule periods Graph accepts for a custom detection rule ("0" = near real time).
_DETECTION_PERIODS = ("0", "1H", "3H", "12H", "24H")
_DETECTION_SEVERITIES = ("informational", "low", "medium", "high")

# Impacted asset kind (tool argument) to the ``alertTemplate.entityMappings``
# list it goes in and the mapping properties that list accepts, the first one
# being the default. Graph removed ``alertTemplate.impactedAssets`` on
# 2026-10-01 (it is dropped on write, never read back): ``entityMappings``
# maps a column of the query to an entity of the alert.
_IMPACTED_ASSET_TYPES: dict[str, tuple[str, tuple[str, ...]]] = {
    "device": (
        "hosts",
        ("deviceIdColumn", "nameColumn", "dnsDomainColumn", "netBiosNameColumn", "ntDomainColumn"),
    ),
    "user": (
        "accounts",
        (
            "aadUserIdColumn",
            "upnColumn",
            "nameColumn",
            "sidColumn",
            "dnsDomainColumn",
            "ntDomainColumn",
            "upnSuffixColumn",
        ),
    ),
    "mailbox": ("mailboxes", ("primaryAddressColumn",)),
}
_ENTITY_MAPPING_FIELDS = sorted({f for _, fields in _IMPACTED_ASSET_TYPES.values() for f in fields})
_MDE_BASE = "https://api.securitycenter.microsoft.com/api"

# An investigation package is kept as a conversation file up to the
# Microsoft file tools' download limit; its entries are listed up to this many.
_MAX_PACKAGE_BYTES = 100 * 1024 * 1024
# A Live Response result is a small JSON document (exit code, output, errors).
_MAX_LIVE_RESPONSE_BYTES = 5 * 1024 * 1024
_MAX_LIVE_RESPONSE_TEXT = 20_000
_MAX_LISTED_PACKAGE_ENTRIES = 60
_MAX_LISTED_ENTRY_NAME = 160

_GRAPH_SCOPE = "https://graph.microsoft.com/.default"
_MDE_SCOPE = "https://api.securitycenter.microsoft.com/.default"

_AUTH_ERROR = (
    "Error: Failed to authenticate with Azure AD. In OAuth (sign-in) mode, "
    "re-authorize this integration — its refresh token may be missing or "
    "expired. In app-only (API key) mode, check the tenant ID, client ID, and "
    "client secret, and ensure admin consent was granted for the required "
    "Microsoft Graph / Defender for Endpoint permissions."
)

_SEVERITY_ICONS: dict[str, str] = {
    "high": "🔴",
    "medium": "🟠",
    "low": "🟡",
    "informational": "🔵",
    "none": "⚪",
}

_STATUS_ICONS: dict[str, str] = {
    "new": "🆕",
    "active": "⚡",
    "inprogress": "🛠️",
    "resolved": "✅",
    "redirected": "↪️",
}

# Reputation classification icons (Defender TI host reputation).
_REPUTATION_ICONS: dict[str, str] = {
    "malicious": "🔴",
    "suspicious": "🟠",
    "neutral": "🟢",
    "unknown": "⚪",
}


# ---------------------------------------------------------------------------
# Auth helper – Azure AD client credentials
# ---------------------------------------------------------------------------


async def _get_token(
    tenant_id: str, client_id: str, client_secret: str, scope: str
) -> str | None:
    """Acquire an app-only bearer token for the requested *scope* audience.

    App-only tokens are cached in-process (keyed by a hash of the credentials +
    scope) until shortly before they expire, so a multi-tool agentic run does
    not mint a fresh token from Azure AD on every single call (which risks
    throttling). The delegated-OAuth path already caches per-audience tokens in
    Redis; this gives the app-only path parity.
    """
    cache_key = app_token_cache_key("defender", tenant_id, client_id, client_secret, scope)
    cached = get_cached_app_token(cache_key)
    if cached:
        return cached

    url = f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"
    try:
        async with egress_client(timeout=15) as client:
            resp = await client.post(
                url,
                data={
                    "grant_type": "client_credentials",
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "scope": scope,
                },
            )
            if resp.is_success:
                data = resp.json()
                access = data.get("access_token")
                if access:
                    set_cached_app_token(cache_key, access, data.get("expires_in"))
                return access
            logger.warning(
                "Azure AD token request failed (scope=%s): %s",
                scope,
                resp.text[:300],
            )
    except httpx.HTTPError as exc:
        logger.warning("Azure AD token request error (scope=%s): %s", scope, format_exception_message(exc))
    return None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_token(token: str) -> tuple[str, str, str]:
    return parse_compound_token(token, 3)  # type: ignore[return-value]


def _headers(access_token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }


def _sev(severity: str | None) -> str:
    return _SEVERITY_ICONS.get((severity or "").lower(), "⚪")


def _st(status: str | None) -> str:
    return _STATUS_ICONS.get((status or "").lower(), "❓")


def _trunc(text: str | None, max_len: int = 300) -> str:
    if not text:
        return ""
    text = str(text)
    return text[:max_len] + ("…" if len(text) > max_len else "")


def _dt(value: str | None) -> str:
    """Trim an ISO timestamp to second precision for display."""
    return (value or "")[:19]


def _package_contents(data: bytes) -> str:
    """The entries of an investigation package ZIP, as the answer lists them."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            entries = [info for info in archive.infolist() if not info.is_dir()]
    except (zipfile.BadZipFile, zipfile.LargeZipFile, OSError, ValueError):
        return "Contents: not a readable ZIP archive."
    shown = entries[:_MAX_LISTED_PACKAGE_ENTRIES]
    lines = [f"Contents ({len(entries)} files):"]
    # A ZIP entry name can be 64 KB long: each one is cut so the listing stays small.
    lines.extend(
        f"- {_trunc(info.filename, _MAX_LISTED_ENTRY_NAME)} ({round(info.file_size / 1024, 1)} KB)"
        for info in shown
    )
    if len(entries) > len(shown):
        lines.append(f"- ... and {len(entries) - len(shown)} more")
    return "\n".join(lines)


def _detection_permission_error(action: str, *, write: bool) -> str:
    """The answer to a 403 on the custom detection rules API, naming the permission."""
    permission = _DETECTION_WRITE_PERMISSION if write else _DETECTION_READ_PERMISSION
    reads = "" if write else f" (or {_DETECTION_WRITE_PERMISSION})"
    return (
        f"Error: {_PROVIDER} refused to {action}: the integration's Entra app lacks "
        f"the Microsoft Graph permission {permission}{reads}. Grant it on the app "
        "(Application permission in app-only API Key mode, Delegated in OAuth "
        "sign-in mode) and grant admin consent, then retry."
    )


def _impacted_assets(assets: list[dict[str, Any]] | None) -> tuple[dict[str, Any], str | None]:
    """Map ``[{type, identifier, field?}]`` to Graph ``entityMappings``, or an error.

    ``identifier`` is the query column holding the entity (e.g. DeviceId);
    ``field`` the mapping property it fills, by default the first of the kind
    (deviceIdColumn, aadUserIdColumn, primaryAddressColumn).
    """
    mappings: dict[str, list[dict[str, str]]] = {}
    for asset in assets or []:
        kind = str((asset or {}).get("type", "")).lower()
        column = (asset or {}).get("identifier")
        target = _IMPACTED_ASSET_TYPES.get(kind)
        if not target or not column:
            return {}, (
                "Error: each impacted asset needs a type (device, user or mailbox) "
                "and an identifier (the query column holding it, e.g. DeviceId)."
            )
        list_name, fields = target
        field = (asset or {}).get("field") or fields[0]
        if field not in fields:
            return {}, f"Error: a {kind} impacted asset takes field {', '.join(fields)}."
        mappings.setdefault(list_name, []).append({field: str(column)})
    return mappings, None


def _fmt_impacted_assets(template: dict[str, Any]) -> str:
    """The entity mappings of an alert template, plus legacy impacted assets."""
    parts: list[str] = []
    for list_name, entries in (template.get("entityMappings") or {}).items():
        if not isinstance(entries, list):
            continue
        for entry in entries:
            fields = ", ".join(f"{k}={v}" for k, v in (entry or {}).items() if v and not str(k).startswith("@"))
            if fields:
                parts.append(f"{list_name}({fields})")
    for a in template.get("impactedAssets") or []:
        parts.append(f"{(a.get('@odata.type') or '').rsplit('.', 1)[-1]}={a.get('identifier', '?')}")
    return ", ".join(parts)


def _fmt_detection_rule(rule: dict[str, Any]) -> str:
    """One detection rule as the list and get tools show it."""
    template = (rule.get("detectionAction") or {}).get("alertTemplate") or {}
    schedule = rule.get("schedule") or {}
    run = rule.get("lastRunDetails") or {}
    techniques = ", ".join(template.get("mitreTechniques") or []) or "none"
    return (
        f"  {rule.get('displayName', '?')} (id: {rule.get('id', '?')})\n"
        f"    Enabled: {rule.get('isEnabled', '?')} | Period: {schedule.get('period', '?')} | "
        f"Severity: {template.get('severity', '?')} | Category: {template.get('category', '?')}\n"
        f"    Alert title: {template.get('title', '?')} | MITRE: {techniques}\n"
        f"    Last run: {_dt(run.get('lastRunDateTime'))} {run.get('status') or ''}".rstrip()
    )


def _outcome_unknown(err: str) -> bool:
    """Whether a write's error leaves open that Graph applied it (a timeout, a 5xx, a broken transfer)."""
    return bool(re.search(r"request timed out|server error \(5\d\d\)|request failed while trying to", err or ""))


def _odata_quote(value: str) -> str:
    """Escape a string for use inside a single-quoted OData string literal.

    Per the OData spec, a literal single quote is escaped by doubling it. Values
    such as display names, UPNs, or service-source names can legitimately contain
    an apostrophe (e.g. ``O'Brien``), which would otherwise terminate the literal
    early and break the ``$filter`` (or allow filter injection). This does not
    apply to raw caller-supplied ``odata_filter`` expressions, which are passed
    through verbatim by design.
    """
    return value.replace("'", "''")


# ---------------------------------------------------------------------------
# Tool builder
# ---------------------------------------------------------------------------


def build_tools(
    token: str,
    executor: CallableToolExecutor,
    *,
    token_acquirer: Callable[[str], Awaitable[str | None]] | None = None,
    **_kwargs: Any,
) -> list[ToolDefinition]:
    """Build Defender XDR tools.

    Two authentication modes are supported, transparently to the tools:

    * **Delegated OAuth** (``token_acquirer`` provided): each audience token is
      minted by redeeming the integration's refresh token for that resource's
      ``/.default`` scope — the orchestrator wires this up for OAuth integrations.
    * **App-only client credentials** (default): ``token`` is the compound
      ``tenant_id||client_id||client_secret`` and each audience token is fetched
      via the client-credentials grant.
    """
    # Stores a downloaded investigation package as a file of the run
    # (injected by integration_tools; None keeps it in memory for the run).
    persist_file_fn = _kwargs.get("persist_file_fn")
    if token_acquirer is not None:

        async def _graph_token() -> str | None:
            return await token_acquirer(_GRAPH_SCOPE)

        async def _mde_token() -> str | None:
            return await token_acquirer(_MDE_SCOPE)
    else:
        tenant_id, client_id, client_secret = _parse_token(token)

        async def _graph_token() -> str | None:
            return await _get_token(tenant_id, client_id, client_secret, _GRAPH_SCOPE)

        async def _mde_token() -> str | None:
            return await _get_token(tenant_id, client_id, client_secret, _MDE_SCOPE)

    async def _read_after_write(client: Any, access_token: str, url: str) -> dict[str, Any] | None:
        """The resource as stored now, read once after a write whose outcome is unknown; None when unreadable."""
        resp, err = await safe_request(
            client, "get", url, _PROVIDER, "read back after the write", headers=_headers(access_token)
        )
        if err or resp is None:
            return None
        data = resp.json()
        return data if isinstance(data, dict) else None

    async def _update_read_back(
        client: Any, access_token: str, url: str, what: str, payload: dict[str, Any], err: str
    ) -> str:
        """After a PATCH answered by a timeout or a 5xx: say from a read-back whether it applied."""
        stored = await _read_after_write(client, access_token, url)
        if stored is None:
            return f"{err} Outcome unknown: the read-back failed; read {what} before writing again."
        differing = sorted(k for k, v in payload.items() if stored.get(k) != v)
        if not differing:
            changed = ", ".join(f"{k}={v}" for k, v in payload.items())
            return f"{what} updated — {changed} (the write timed out; the read-back shows it applied, do not retry)"
        return f"{err} Read-back: {what} does not show {', '.join(differing)} yet; it is safe to retry."

    async def _comment_read_back(client: Any, access_token: str, url: str, what: str, comment: str, err: str) -> str:
        """After a comment POST answered by a timeout or a 5xx: say from a read-back whether it was added."""
        stored = await _read_after_write(client, access_token, url)
        if stored is None:
            return f"{err} Outcome unknown: the read-back failed; read the comments of {what} before posting again."
        if any(isinstance(c, dict) and c.get("comment") == comment for c in stored.get("comments") or []):
            return f"Comment added to {what} (the write timed out; the read-back shows it, do not post it again)."
        return f"{err} Read-back: {what} has no such comment; it is safe to post it again."

    # ══════════════════════════════════════════════════════════════════════
    # 0. INCIDENTS (Microsoft Graph security)
    # ══════════════════════════════════════════════════════════════════════

    async def list_defender_incidents(
        status: str = "",
        severity: str = "",
        assigned_to: str = "",
        count: int = 25,
    ) -> str:
        """List Defender XDR incidents with optional filters."""
        access_token = await _graph_token()
        if not access_token:
            return _AUTH_ERROR

        params: dict[str, Any] = {
            "$top": min(count, 50),
            "$orderby": "createdDateTime desc",
        }
        filters: list[str] = []
        if status:
            filters.append(f"status eq '{_odata_quote(status)}'")
        if severity:
            filters.append(f"severity eq '{_odata_quote(severity)}'")
        if assigned_to:
            filters.append(f"assignedTo eq '{_odata_quote(assigned_to)}'")
        if filters:
            params["$filter"] = " and ".join(filters)

        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{_GRAPH_BASE}/security/incidents",
                _PROVIDER,
                "list incidents",
                headers=_headers(access_token),
                params=params,
            )
            if err:
                return err
            incidents = resp.json().get("value", [])

            def _fmt(inc: dict) -> str:
                sev = inc.get("severity", "?")
                st = inc.get("status", "?")
                tags = ", ".join(inc.get("customTags", []) or []) or "—"
                return (
                    f"  {_sev(sev)} #{inc.get('id', '?')} [{sev}] {_st(st)} {st} — "
                    f"{inc.get('displayName', 'Untitled')}\n"
                    f"    Assigned: {inc.get('assignedTo') or 'Unassigned'} | "
                    f"Classification: {inc.get('classification') or 'N/A'} | "
                    f"Tags: {tags} | Created: {_dt(inc.get('createdDateTime'))}"
                )

            return format_entity_list(incidents, _fmt, noun="incident")

    async def get_defender_incident(
        incident_id: str, include_alerts: bool = True
    ) -> str:
        """Get full details of a Defender XDR incident, optionally with alerts."""
        access_token = await _graph_token()
        if not access_token:
            return _AUTH_ERROR

        params: dict[str, Any] = {}
        if include_alerts:
            params["$expand"] = "alerts"

        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{_GRAPH_BASE}/security/incidents/{incident_id}",
                _PROVIDER,
                f"get incident {incident_id}",
                headers=_headers(access_token),
                params=params,
            )
            if err:
                return err
            inc = resp.json()
            lines = [
                f"Incident #{inc.get('id', '?')}: {inc.get('displayName', 'Untitled')}",
                f"Severity: {_sev(inc.get('severity'))} {inc.get('severity', '?')}",
                f"Status: {_st(inc.get('status'))} {inc.get('status', '?')}",
                f"Classification: {inc.get('classification') or 'N/A'} — "
                f"Determination: {inc.get('determination') or 'N/A'}",
                f"Assigned to: {inc.get('assignedTo') or 'Unassigned'}",
                f"Custom tags: {', '.join(inc.get('customTags', []) or []) or 'None'}",
                f"Created: {_dt(inc.get('createdDateTime'))} | "
                f"Last update: {_dt(inc.get('lastUpdateDateTime'))}",
                f"Portal URL: {inc.get('incidentWebUrl', 'N/A')}",
            ]
            if inc.get("summary"):
                lines.append(f"\nSummary:\n{_trunc(inc.get('summary'), 600)}")
            if inc.get("description"):
                lines.append(f"\nDescription:\n{_trunc(inc.get('description'), 600)}")

            comments = inc.get("comments", []) or []
            if comments:
                lines.append(f"\nComments ({len(comments)}):")
                for c in comments[-5:]:
                    lines.append(
                        f"  - [{_dt(c.get('createdDateTime'))}] "
                        f"{c.get('createdByDisplayName', '?')}: "
                        f"{_trunc(c.get('comment'), 200)}"
                    )

            alerts = inc.get("alerts", []) or []
            if alerts:
                lines.append(f"\nAlerts ({len(alerts)}):")
                for a in alerts[:25]:
                    tactics = ", ".join(a.get("mitreTechniques", []) or []) or "N/A"
                    lines.append(
                        f"  {_sev(a.get('severity'))} [{a.get('severity', '?')}] "
                        f"{a.get('title', 'Unnamed')} "
                        f"(id: {a.get('id', '?')})\n"
                        f"    Source: {a.get('serviceSource', '?')} | "
                        f"Status: {a.get('status', '?')} | Techniques: {tactics}"
                    )
            return "\n".join(lines)

    async def update_defender_incident(
        incident_id: str,
        *,
        status: str = "",
        severity: str = "",
        classification: str = "",
        determination: str = "",
        assigned_to: str = "",
        display_name: str = "",
        resolving_comment: str = "",
        custom_tags: list[str] | None = None,
    ) -> str:
        """Update a Defender XDR incident. Only provided fields are changed."""
        access_token = await _graph_token()
        if not access_token:
            return _AUTH_ERROR

        payload: dict[str, Any] = {}
        if status:
            payload["status"] = status
        if severity:
            payload["severity"] = severity
        if classification:
            payload["classification"] = classification
        if determination:
            payload["determination"] = determination
        if assigned_to:
            payload["assignedTo"] = assigned_to
        if display_name:
            payload["displayName"] = display_name
        if resolving_comment:
            payload["resolvingComment"] = resolving_comment
        if custom_tags is not None:
            payload["customTags"] = custom_tags
        if not payload:
            return "Error: No fields provided to update."

        url = f"{_GRAPH_BASE}/security/incidents/{incident_id}"
        async with egress_client(timeout=30) as client:
            _, err = await safe_request(
                client,
                "patch",
                url,
                _PROVIDER,
                f"update incident {incident_id}",
                headers=_headers(access_token),
                json=payload,
            )
            changed = ", ".join(f"{k}={v}" for k, v in payload.items())
            if err and _outcome_unknown(err):
                return await _update_read_back(client, access_token, url, f"Incident {incident_id}", payload, err)
            if err:
                return err
            return f"Incident {incident_id} updated — {changed}"

    async def add_defender_incident_comment(incident_id: str, comment: str) -> str:
        """Add a comment to a Defender XDR incident for the investigation trail."""
        access_token = await _graph_token()
        if not access_token:
            return _AUTH_ERROR

        async with egress_client(timeout=30) as client:
            _, err = await safe_request(
                client,
                "post",
                f"{_GRAPH_BASE}/security/incidents/{incident_id}/comments",
                _PROVIDER,
                f"add comment to incident {incident_id}",
                headers=_headers(access_token),
                json={
                    "@odata.type": "microsoft.graph.security.alertComment",
                    "comment": comment,
                },
            )
            if err and _outcome_unknown(err):
                return await _comment_read_back(
                    client,
                    access_token,
                    f"{_GRAPH_BASE}/security/incidents/{incident_id}",
                    f"incident {incident_id}",
                    comment,
                    err,
                )
            if err:
                return err
            return f"Comment added to incident {incident_id}."

    # ══════════════════════════════════════════════════════════════════════
    # 1. ALERTS (Microsoft Graph security — alerts_v2)
    # ══════════════════════════════════════════════════════════════════════

    async def list_defender_alerts(
        status: str = "",
        severity: str = "",
        service_source: str = "",
        count: int = 25,
    ) -> str:
        """List Defender XDR alerts (alerts_v2) with optional filters."""
        access_token = await _graph_token()
        if not access_token:
            return _AUTH_ERROR

        params: dict[str, Any] = {
            "$top": min(count, 50),
            "$orderby": "createdDateTime desc",
        }
        filters: list[str] = []
        if status:
            filters.append(f"status eq '{_odata_quote(status)}'")
        if severity:
            filters.append(f"severity eq '{_odata_quote(severity)}'")
        if service_source:
            filters.append(f"serviceSource eq '{_odata_quote(service_source)}'")
        if filters:
            params["$filter"] = " and ".join(filters)

        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{_GRAPH_BASE}/security/alerts_v2",
                _PROVIDER,
                "list alerts",
                headers=_headers(access_token),
                params=params,
            )
            if err:
                return err
            alerts = resp.json().get("value", [])

            def _fmt(a: dict) -> str:
                sev = a.get("severity", "?")
                st = a.get("status", "?")
                techniques = ", ".join(a.get("mitreTechniques", []) or []) or "N/A"
                return (
                    f"  {_sev(sev)} [{sev}] {_st(st)} {st} — {a.get('title', 'Unnamed')}\n"
                    f"    id: {a.get('id', '?')} | Incident: {a.get('incidentId', '—')} | "
                    f"Source: {a.get('serviceSource', '?')} | "
                    f"Detection: {a.get('detectionSource', '?')}\n"
                    f"    Category: {a.get('category', 'N/A')} | "
                    f"Techniques: {techniques} | Created: {_dt(a.get('createdDateTime'))}"
                )

            return format_entity_list(alerts, _fmt, noun="alert")

    async def get_defender_alert(alert_id: str) -> str:
        """Get full details of a Defender XDR alert including evidence (IOCs)."""
        access_token = await _graph_token()
        if not access_token:
            return _AUTH_ERROR

        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{_GRAPH_BASE}/security/alerts_v2/{alert_id}",
                _PROVIDER,
                f"get alert {alert_id}",
                headers=_headers(access_token),
            )
            if err:
                return err
            a = resp.json()
            lines = [
                f"Alert: {a.get('title', 'Unnamed')} (id: {a.get('id', '?')})",
                f"Severity: {_sev(a.get('severity'))} {a.get('severity', '?')}",
                f"Status: {_st(a.get('status'))} {a.get('status', '?')}",
                f"Incident: {a.get('incidentId', '—')}",
                f"Classification: {a.get('classification') or 'N/A'} — "
                f"Determination: {a.get('determination') or 'N/A'}",
                f"Service source: {a.get('serviceSource', '?')} | "
                f"Detection: {a.get('detectionSource', '?')}",
                f"Category: {a.get('category', 'N/A')}",
                f"MITRE techniques: {', '.join(a.get('mitreTechniques', []) or []) or 'N/A'}",
                f"Assigned to: {a.get('assignedTo') or 'Unassigned'}",
                f"First activity: {_dt(a.get('firstActivityDateTime'))} | "
                f"Last activity: {_dt(a.get('lastActivityDateTime'))}",
            ]
            if a.get("description"):
                lines.append(f"\nDescription:\n{_trunc(a.get('description'), 500)}")
            if a.get("recommendedActions"):
                lines.append(
                    f"\nRecommended actions:\n{_trunc(a.get('recommendedActions'), 500)}"
                )

            evidence = a.get("evidence", []) or []
            if evidence:
                lines.append(f"\nEvidence ({len(evidence)}):")
                for ev in evidence[:30]:
                    kind = (ev.get("@odata.type") or "").split(".")[-1] or "evidence"
                    detail = (
                        ev.get("fileDetails", {}).get("fileName")
                        or ev.get("imageFile", {}).get("fileName")
                        or ev.get("ipAddress")
                        or ev.get("url")
                        or ev.get("userAccount", {}).get("accountName")
                        or ev.get("deviceDnsName")
                        or ev.get("domainName")
                        or ""
                    )
                    verdict = ev.get("verdict", "")
                    lines.append(
                        f"  [{kind}] {detail}"
                        + (f" — verdict: {verdict}" if verdict else "")
                    )
            return "\n".join(lines)

    async def update_defender_alert(
        alert_id: str,
        *,
        status: str = "",
        classification: str = "",
        determination: str = "",
        assigned_to: str = "",
    ) -> str:
        """Update a Defender XDR alert. Only provided fields are changed."""
        access_token = await _graph_token()
        if not access_token:
            return _AUTH_ERROR

        payload: dict[str, Any] = {}
        if status:
            payload["status"] = status
        if classification:
            payload["classification"] = classification
        if determination:
            payload["determination"] = determination
        if assigned_to:
            payload["assignedTo"] = assigned_to
        if not payload:
            return "Error: No fields provided to update."

        url = f"{_GRAPH_BASE}/security/alerts_v2/{alert_id}"
        async with egress_client(timeout=30) as client:
            _, err = await safe_request(
                client,
                "patch",
                url,
                _PROVIDER,
                f"update alert {alert_id}",
                headers=_headers(access_token),
                json=payload,
            )
            if err and _outcome_unknown(err):
                return await _update_read_back(client, access_token, url, f"Alert {alert_id}", payload, err)
            if err:
                return err
            changed = ", ".join(f"{k}={v}" for k, v in payload.items())
            return f"Alert {alert_id} updated — {changed}"

    async def add_defender_alert_comment(alert_id: str, comment: str) -> str:
        """Add a comment to a Defender XDR alert."""
        access_token = await _graph_token()
        if not access_token:
            return _AUTH_ERROR

        async with egress_client(timeout=30) as client:
            _, err = await safe_request(
                client,
                "post",
                f"{_GRAPH_BASE}/security/alerts_v2/{alert_id}/comments",
                _PROVIDER,
                f"add comment to alert {alert_id}",
                headers=_headers(access_token),
                json={
                    "@odata.type": "microsoft.graph.security.alertComment",
                    "comment": comment,
                },
            )
            if err and _outcome_unknown(err):
                return await _comment_read_back(
                    client,
                    access_token,
                    f"{_GRAPH_BASE}/security/alerts_v2/{alert_id}",
                    f"alert {alert_id}",
                    comment,
                    err,
                )
            if err:
                return err
            return f"Comment added to alert {alert_id}."

    # ══════════════════════════════════════════════════════════════════════
    # 2. ADVANCED HUNTING (Microsoft Graph security — runHuntingQuery)
    # ══════════════════════════════════════════════════════════════════════

    async def run_defender_hunting_query(query: str, timespan: str = "P30D") -> str:
        """Run a KQL advanced hunting query across Defender XDR tables."""
        access_token = await _graph_token()
        if not access_token:
            return _AUTH_ERROR

        payload: dict[str, Any] = {"Query": query}
        if timespan:
            payload["Timespan"] = timespan

        async with egress_client(timeout=210) as client:
            resp, err = await safe_request(
                client,
                "post",
                f"{_GRAPH_BASE}/security/runHuntingQuery",
                _PROVIDER,
                "run advanced hunting query",
                headers=_headers(access_token),
                json=payload,
            )
            if err:
                return err
            data = resp.json()
            schema = data.get("schema", []) or []
            results = data.get("results", []) or []
            if not results:
                cols = ", ".join(c.get("name", "?") for c in schema)
                return f"Query returned 0 rows. Columns: {cols or 'N/A'}"

            columns = [c.get("name", "?") for c in schema] or list(results[0].keys())
            lines = [f"Hunting results ({len(results)} rows):"]
            lines.append(" | ".join(columns))
            lines.append("-" * min(len(" | ".join(columns)), 120))
            max_rows = 50
            for row in results[:max_rows]:
                vals = []
                for col in columns:
                    v = row.get(col)
                    if v is None:
                        vals.append("null")
                    else:
                        # Strip embedded newlines/tabs so a value (e.g. a process
                        # command line) can't break the pipe-delimited table into
                        # misaligned visual rows.
                        vals.append(
                            str(v)
                            .replace("\r", " ")
                            .replace("\n", " ")
                            .replace("\t", " ")[:80]
                        )
                lines.append(" | ".join(vals))
            if len(results) > max_rows:
                lines.append(f"\n... [{len(results) - max_rows} more rows not shown]")
            return "\n".join(lines)

    # ══════════════════════════════════════════════════════════════════════
    # 3. THREAT INTELLIGENCE — Defender TI (Microsoft Graph security)
    # ══════════════════════════════════════════════════════════════════════

    async def list_defender_ti_articles(count: int = 15) -> str:
        """List Microsoft Defender Threat Intelligence articles (finished intel)."""
        access_token = await _graph_token()
        if not access_token:
            return _AUTH_ERROR

        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{_GRAPH_BASE}/security/threatIntelligence/articles",
                _PROVIDER,
                "list threat intelligence articles",
                headers=_headers(access_token),
                params={"$top": min(count, 50)},
            )
            if err:
                return err
            articles = resp.json().get("value", [])

            def _fmt(art: dict) -> str:
                tags = ", ".join(art.get("tags", []) or []) or "—"
                summary = (art.get("summary") or {}).get("content", "")
                featured = " ⭐" if art.get("isFeatured") else ""
                return (
                    f"  {art.get('title', 'Untitled')}{featured} (id: {art.get('id', '?')})\n"
                    f"    Published: {_dt(art.get('createdDateTime'))} | Tags: {tags}\n"
                    f"    {_trunc(summary, 200)}"
                )

            return format_entity_list(articles, _fmt, noun="article")

    async def get_defender_ti_article(article_id: str) -> str:
        """Get the full content of a Defender Threat Intelligence article."""
        access_token = await _graph_token()
        if not access_token:
            return _AUTH_ERROR

        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{_GRAPH_BASE}/security/threatIntelligence/articles/{article_id}",
                _PROVIDER,
                f"get threat intelligence article {article_id}",
                headers=_headers(access_token),
            )
            if err:
                return err
            art = resp.json()
            body = (art.get("body") or {}).get("content", "")
            summary = (art.get("summary") or {}).get("content", "")
            lines = [
                f"Article: {art.get('title', 'Untitled')}",
                f"Published: {_dt(art.get('createdDateTime'))} | "
                f"Updated: {_dt(art.get('lastUpdatedDateTime'))}",
                f"Tags: {', '.join(art.get('tags', []) or []) or 'None'}",
            ]
            if summary:
                lines.append(f"\nSummary:\n{summary}")
            if body:
                lines.append(f"\nBody:\n{_trunc(body, 4000)}")
            return "\n".join(lines)

    async def list_defender_intel_profiles(count: int = 15, kind: str = "") -> str:
        """List Defender TI intel profiles (threat actors and tools)."""
        access_token = await _graph_token()
        if not access_token:
            return _AUTH_ERROR

        params: dict[str, Any] = {"$top": min(count, 50)}
        if kind:
            params["$filter"] = f"kind eq '{_odata_quote(kind)}'"

        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{_GRAPH_BASE}/security/threatIntelligence/intelProfiles",
                _PROVIDER,
                "list intel profiles",
                headers=_headers(access_token),
                params=params,
            )
            if err:
                return err
            profiles = resp.json().get("value", [])

            def _fmt(p: dict) -> str:
                aliases = ", ".join(p.get("aliases", []) or []) or "—"
                summary = (p.get("summary") or {}).get("content", "")
                return (
                    f"  [{p.get('kind', '?')}] {p.get('title', 'Untitled')} "
                    f"(id: {p.get('id', '?')})\n"
                    f"    Aliases: {aliases} | "
                    f"First active: {_dt(p.get('firstActiveDateTime'))}\n"
                    f"    {_trunc(summary, 200)}"
                )

            return format_entity_list(profiles, _fmt, noun="intel profile")

    async def get_defender_intel_profile(profile_id: str) -> str:
        """Get a Defender TI intel profile (threat actor) with tradecraft details."""
        access_token = await _graph_token()
        if not access_token:
            return _AUTH_ERROR

        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{_GRAPH_BASE}/security/threatIntelligence/intelProfiles/{profile_id}",
                _PROVIDER,
                f"get intel profile {profile_id}",
                headers=_headers(access_token),
            )
            if err:
                return err
            p = resp.json()
            lines = [
                f"Intel profile: {p.get('title', 'Untitled')} [{p.get('kind', '?')}]",
                f"Aliases: {', '.join(p.get('aliases', []) or []) or 'None'}",
                f"First active: {_dt(p.get('firstActiveDateTime'))}",
            ]
            targets = p.get("targets", []) or []
            if targets:
                lines.append(f"Targets: {', '.join(str(t) for t in targets)}")
            origins = p.get("countriesOrRegionsOfOrigin", []) or []
            if origins:
                names = [
                    o.get("label") or o.get("code", "?")
                    for o in origins
                    if isinstance(o, dict)
                ]
                lines.append(f"Origin: {', '.join(names) or 'N/A'}")
            summary = (p.get("summary") or {}).get("content", "")
            if summary:
                lines.append(f"\nSummary:\n{_trunc(summary, 1500)}")
            description = (p.get("description") or {}).get("content", "")
            if description:
                lines.append(f"\nDescription:\n{_trunc(description, 2000)}")
            tradecraft = (p.get("tradecraft") or {}).get("content", "")
            if tradecraft:
                lines.append(f"\nTradecraft:\n{_trunc(tradecraft, 1500)}")
            return "\n".join(lines)

    async def get_defender_ti_host(host: str) -> str:
        """Enrich a host/domain/IP with Defender TI (registrar, first/last seen)."""
        access_token = await _graph_token()
        if not access_token:
            return _AUTH_ERROR

        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{_GRAPH_BASE}/security/threatIntelligence/hosts/{_url_encode(host, safe='')}",
                _PROVIDER,
                f"get TI host {host}",
                headers=_headers(access_token),
            )
            if err:
                return err
            h = resp.json()
            kind = (h.get("@odata.type") or "").split(".")[-1] or "host"
            lines = [
                f"Host: {h.get('id', host)} [{kind}]",
                f"First seen: {_dt(h.get('firstSeenDateTime'))} | "
                f"Last seen: {_dt(h.get('lastSeenDateTime'))}",
            ]
            if h.get("registrar"):
                lines.append(f"Registrar: {h.get('registrar')}")
            if h.get("registrant"):
                lines.append(f"Registrant: {h.get('registrant')}")
            return "\n".join(lines)

    async def get_defender_ti_host_reputation(host: str) -> str:
        """Get the Defender TI reputation verdict and scoring rules for a host."""
        access_token = await _graph_token()
        if not access_token:
            return _AUTH_ERROR

        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{_GRAPH_BASE}/security/threatIntelligence/hosts/{_url_encode(host, safe='')}/reputation",
                _PROVIDER,
                f"get TI reputation for {host}",
                headers=_headers(access_token),
            )
            if err:
                return err
            r = resp.json()
            classification = r.get("classification", "unknown")
            icon = _REPUTATION_ICONS.get(str(classification).lower(), "⚪")
            lines = [
                f"Reputation for {host}: {icon} {classification} "
                f"(score: {r.get('score', '?')}/100)",
            ]
            rules = r.get("rules", []) or []
            if rules:
                lines.append(f"\nScoring rules ({len(rules)}):")
                for rule in rules[:20]:
                    lines.append(
                        f"  [{rule.get('severity', '?')}] {rule.get('name', '?')} "
                        f"— {_trunc(rule.get('description'), 150)}"
                    )
            return "\n".join(lines)

    async def get_defender_ti_passive_dns(host: str, count: int = 25) -> str:
        """Get passive DNS resolutions for a host from Defender TI."""
        access_token = await _graph_token()
        if not access_token:
            return _AUTH_ERROR

        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{_GRAPH_BASE}/security/threatIntelligence/hosts/{_url_encode(host, safe='')}/passiveDns",
                _PROVIDER,
                f"get passive DNS for {host}",
                headers=_headers(access_token),
                params={"$top": min(count, 50)},
            )
            if err:
                return err
            records = resp.json().get("value", [])

            def _fmt(rec: dict) -> str:
                artifact = rec.get("artifact", {}) or {}
                value = artifact.get("id") or artifact.get("address") or "?"
                return (
                    f"  {rec.get('recordType', '?')} → {value} "
                    f"(first: {_dt(rec.get('firstSeenDateTime'))}, "
                    f"last: {_dt(rec.get('lastSeenDateTime'))})"
                )

            return format_entity_list(records, _fmt, noun="passive DNS record")

    async def get_defender_ti_vulnerability(cve_id: str) -> str:
        """Get Defender TI intelligence about a vulnerability (CVE)."""
        access_token = await _graph_token()
        if not access_token:
            return _AUTH_ERROR

        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{_GRAPH_BASE}/security/threatIntelligence/vulnerabilities/{cve_id}",
                _PROVIDER,
                f"get TI vulnerability {cve_id}",
                headers=_headers(access_token),
            )
            if err:
                return err
            v = resp.json()
            cvss3 = v.get("cvss3Summary", {}) or {}
            lines = [
                f"Vulnerability: {v.get('id', cve_id)}",
                f"Priority score: {v.get('priorityScore', '?')}/100 | "
                f"CVSS v3: {cvss3.get('score', '?')} ({cvss3.get('severity', '?')})",
                f"Exploits available: {v.get('exploitsAvailable', '?')} | "
                f"Active exploits observed: {v.get('activeExploitsObserved', '?')} | "
                f"Chatter: {v.get('hasChatter', '?')}",
                f"CWE: {', '.join(v.get('commonWeaknessEnumerationIds', []) or []) or 'N/A'}",
                f"Published: {_dt(v.get('publishedDateTime'))} | "
                f"Updated: {_dt(v.get('lastModifiedDateTime'))}",
            ]
            description = (v.get("description") or {}).get("content", "")
            if description:
                lines.append(f"\nDescription:\n{_trunc(description, 1500)}")
            remediation = (v.get("remediation") or {}).get("content", "")
            if remediation:
                lines.append(f"\nRemediation:\n{_trunc(remediation, 800)}")
            return "\n".join(lines)

    # ══════════════════════════════════════════════════════════════════════
    # 4. THREAT INDICATORS / IOCs (Defender for Endpoint)
    # ══════════════════════════════════════════════════════════════════════

    async def list_defender_indicators(count: int = 50) -> str:
        """List custom threat indicators (IOCs) in Defender for Endpoint."""
        access_token = await _mde_token()
        if not access_token:
            return _AUTH_ERROR

        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{_MDE_BASE}/indicators",
                _PROVIDER,
                "list indicators",
                headers=_headers(access_token),
                params={"$top": min(count, 100)},
            )
            if err:
                return err
            indicators = resp.json().get("value", [])

            def _fmt(ind: dict) -> str:
                return (
                    f"  [{ind.get('indicatorType', '?')}] {ind.get('indicatorValue', '?')} "
                    f"→ action: {ind.get('action', '?')} "
                    f"(severity: {ind.get('severity', '?')})\n"
                    f"    id: {ind.get('id', '?')} | Title: {ind.get('title', '—')} | "
                    f"Created by: {ind.get('createdBy', '?')} | "
                    f"Expires: {_dt(ind.get('expirationTime'))}"
                )

            return format_entity_list(indicators, _fmt, noun="indicator")

    async def create_defender_indicator(
        indicator_value: str,
        indicator_type: str,
        action: str,
        title: str,
        description: str = "",
        *,
        severity: str = "Medium",
        expiration_time: str = "",
        recommended_actions: str = "",
        generate_alert: bool = True,
    ) -> str:
        """Create a custom threat indicator (IOC) in Defender for Endpoint."""
        access_token = await _mde_token()
        if not access_token:
            return _AUTH_ERROR

        # ``description`` is surfaced in Defender alerts when the indicator
        # fires, so default it to a branded, self-documenting note when the
        # caller omits one (the MDE API requires a non-empty description).
        payload: dict[str, Any] = {
            "indicatorValue": indicator_value,
            "indicatorType": indicator_type,
            "action": action,
            "title": title,
            "description": description
            or f"Indicator created via {get_cached_branding().name}",
            "severity": severity,
            "generateAlert": generate_alert,
        }
        if expiration_time:
            payload["expirationTime"] = expiration_time
        if recommended_actions:
            payload["recommendedActions"] = recommended_actions

        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "post",
                f"{_MDE_BASE}/indicators",
                _PROVIDER,
                "create indicator",
                headers=_headers(access_token),
                json=payload,
            )
            if err:
                return err
            created = resp.json()
            return (
                f"Indicator created (id: {created.get('id', '?')}): "
                f"{indicator_type}={indicator_value} → {action}"
            )

    async def delete_defender_indicator(indicator_id: str) -> str:
        """Delete a custom threat indicator (IOC) from Defender for Endpoint."""
        access_token = await _mde_token()
        if not access_token:
            return _AUTH_ERROR

        async with egress_client(timeout=30) as client:
            _, err = await safe_request(
                client,
                "delete",
                f"{_MDE_BASE}/indicators/{indicator_id}",
                _PROVIDER,
                f"delete indicator {indicator_id}",
                headers=_headers(access_token),
            )
            if err:
                return err
            return f"Indicator {indicator_id} deleted."

    # ══════════════════════════════════════════════════════════════════════
    # 4b. CUSTOM DETECTION RULES (Microsoft Graph beta security/rules)
    # ══════════════════════════════════════════════════════════════════════

    async def _detection_request(
        method: str, url: str, action: str, *, write: bool, **kwargs: Any
    ) -> tuple[httpx.Response | None, str | None]:
        """One call to the detection rules API; a 403 names the permission to grant."""
        access_token = await _graph_token()
        if not access_token:
            return None, _AUTH_ERROR
        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                method,
                url,
                _PROVIDER,
                action,
                headers=_headers(access_token),
                expected_statuses={403},
                **kwargs,
            )
        if err:
            if resp is not None and resp.status_code == 403:
                return None, _detection_permission_error(action, write=write)
            return None, err
        return resp, None

    async def list_defender_detection_rules(odata_filter: str = "", count: int = 50) -> str:
        """List the custom detection rules of the tenant."""
        params: dict[str, Any] = {"$top": max(1, min(count, 100))}
        if odata_filter:
            params["$filter"] = odata_filter
        resp, err = await _detection_request(
            "get", _DETECTION_RULES_URL, "list custom detection rules", write=False, params=params
        )
        if err or resp is None:
            return err or "Error: no response."
        rules = resp.json().get("value", []) or []
        return format_entity_list(rules, _fmt_detection_rule, noun="custom detection rule")

    async def get_defender_detection_rule(rule_id: str) -> str:
        """Get one custom detection rule with its query and alert template."""
        resp, err = await _detection_request(
            "get",
            f"{_DETECTION_RULES_URL}/{_url_encode(rule_id, safe='')}",
            f"get custom detection rule {rule_id}",
            write=False,
        )
        if err or resp is None:
            return err or "Error: no response."
        rule = resp.json()
        template = (rule.get("detectionAction") or {}).get("alertTemplate") or {}
        query = (rule.get("queryCondition") or {}).get("queryText", "")
        assets = _fmt_impacted_assets(template)
        actions = (rule.get("detectionAction") or {}).get("responseActions") or []
        lines = [
            _fmt_detection_rule(rule).strip(),
            f"Created by: {rule.get('createdBy', '?')} at {_dt(rule.get('createdDateTime'))}",
            f"Description: {_trunc(template.get('description'), 800) or 'none'}",
            f"Recommended actions: {_trunc(template.get('recommendedActions'), 800) or 'none'}",
            f"Impacted assets: {assets or 'none'}",
            f"Response actions: {len(actions)}",
            f"Query:\n{query}",
        ]
        return "\n".join(lines)

    def _alert_template(
        title: str,
        description: str,
        severity: str,
        category: str,
        mitre_techniques: list[str] | None,
        recommended_actions: str,
        impacted: dict[str, Any],
    ) -> dict[str, Any]:
        template: dict[str, Any] = {
            "title": title,
            "description": description,
            "severity": severity.lower(),
            "category": category,
            "mitreTechniques": list(mitre_techniques or []),
        }
        if impacted:
            template["entityMappings"] = impacted
        if recommended_actions:
            template["recommendedActions"] = recommended_actions
        return template

    def _check_rule_values(period: str | None, severity: str | None) -> str | None:
        if period is not None and period not in _DETECTION_PERIODS:
            return f"Error: period must be one of {', '.join(_DETECTION_PERIODS)}."
        if severity is not None and severity.lower() not in _DETECTION_SEVERITIES:
            return f"Error: severity must be one of {', '.join(_DETECTION_SEVERITIES)}."
        return None

    async def create_defender_detection_rule(
        display_name: str,
        query: str,
        alert_title: str,
        *,
        period: str = "24H",
        enabled: bool = True,
        description: str = "",
        severity: str = "medium",
        category: str = "SuspiciousActivity",
        mitre_techniques: list[str] | None = None,
        impacted_assets: list[dict[str, Any]] | None = None,
        recommended_actions: str = "",
        response_actions: list[dict[str, Any]] | None = None,
    ) -> str:
        """Create a scheduled custom detection rule from a KQL hunting query."""
        problem = _check_rule_values(period, severity)
        if problem:
            return problem
        impacted, problem = _impacted_assets(impacted_assets)
        if problem:
            return problem
        payload: dict[str, Any] = {
            "displayName": display_name,
            "isEnabled": enabled,
            "queryCondition": {"queryText": query},
            "schedule": {"period": period},
            "detectionAction": {
                "alertTemplate": _alert_template(
                    alert_title,
                    description or f"Custom detection rule created via {get_cached_branding().name}",
                    severity,
                    category,
                    mitre_techniques,
                    recommended_actions,
                    impacted,
                ),
                "responseActions": list(response_actions or []),
            },
        }
        resp, err = await _detection_request(
            "post", _DETECTION_RULES_URL, "create custom detection rule", write=True, json=payload
        )
        if err and _outcome_unknown(err):
            # The POST may have created the rule: look it up by name before the
            # caller retries, so a timeout never leaves two rules.
            found, find_err = await _detection_request(
                "get",
                _DETECTION_RULES_URL,
                "find the custom detection rule after the write",
                write=False,
                params={"$filter": f"displayName eq '{_odata_quote(display_name)}'"},
            )
            if find_err or found is None:
                return f"{err} Outcome unknown: list the rules named {display_name!r} before creating it again."
            existing = found.json().get("value") or []
            if existing:
                return (
                    f"Custom detection rule created (id: {existing[0].get('id', '?')}): {display_name} "
                    "(the write timed out; the read-back shows it, do not create it again)."
                )
            return f"{err} Read-back: no rule named {display_name!r}; it is safe to create it again."
        if err or resp is None:
            return err or "Error: no response."
        created = resp.json()
        return (
            f"Custom detection rule created (id: {created.get('id', '?')}): "
            f"{display_name}, period {period}, severity {severity.lower()}, "
            f"enabled {enabled}."
        )

    async def update_defender_detection_rule(
        rule_id: str,
        *,
        display_name: str | None = None,
        enabled: bool | None = None,
        query: str | None = None,
        period: str | None = None,
        alert_title: str | None = None,
        description: str | None = None,
        severity: str | None = None,
        category: str | None = None,
        mitre_techniques: list[str] | None = None,
        impacted_assets: list[dict[str, Any]] | None = None,
        recommended_actions: str | None = None,
        response_actions: list[dict[str, Any]] | None = None,
    ) -> str:
        """Change the given fields of a custom detection rule, the others kept."""
        problem = _check_rule_values(period, severity)
        if problem:
            return problem
        url = f"{_DETECTION_RULES_URL}/{_url_encode(rule_id, safe='')}"
        payload: dict[str, Any] = {}
        if display_name is not None:
            payload["displayName"] = display_name
        if enabled is not None:
            payload["isEnabled"] = enabled
        if query is not None:
            payload["queryCondition"] = {"queryText": query}
        if period is not None:
            payload["schedule"] = {"period": period}
        template_changes: dict[str, Any] = {}
        for key, value in (
            ("title", alert_title),
            ("description", description),
            ("severity", severity.lower() if severity is not None else None),
            ("category", category),
            ("mitreTechniques", mitre_techniques),
            ("recommendedActions", recommended_actions),
        ):
            if value is not None:
                template_changes[key] = value
        if impacted_assets is not None:
            impacted, problem = _impacted_assets(impacted_assets)
            if problem:
                return problem
            template_changes["entityMappings"] = impacted
        if template_changes or response_actions is not None:
            # A PATCH replaces a complex property whole: read the stored
            # detection action and change only the fields given.
            current, err = await _detection_request(
                "get", url, f"get custom detection rule {rule_id}", write=False
            )
            if err or current is None:
                return err or "Error: no response."
            action = dict(current.json().get("detectionAction") or {})
            template = {**(action.get("alertTemplate") or {}), **template_changes}
            # Removed from the Graph resource on 2026-10-01: never sent back.
            template.pop("impactedAssets", None)
            action["alertTemplate"] = template
            if response_actions is not None:
                action["responseActions"] = list(response_actions)
            payload["detectionAction"] = action
        if not payload:
            return "Error: give at least one field to change."
        _, err = await _detection_request(
            "patch", url, f"update custom detection rule {rule_id}", write=True, json=payload
        )
        if err:
            return err
        changed = ", ".join(sorted([*payload.keys() - {"detectionAction"}, *template_changes]))
        if response_actions is not None:
            changed = ", ".join(filter(None, [changed, "responseActions"]))
        return f"Custom detection rule {rule_id} updated: {changed}."

    async def delete_defender_detection_rule(rule_id: str) -> str:
        """Delete a custom detection rule."""
        _, err = await _detection_request(
            "delete",
            f"{_DETECTION_RULES_URL}/{_url_encode(rule_id, safe='')}",
            f"delete custom detection rule {rule_id}",
            write=True,
        )
        if err:
            return err
        return f"Custom detection rule {rule_id} deleted."

    # ══════════════════════════════════════════════════════════════════════
    # 5. DEVICES / MACHINES (Defender for Endpoint)
    # ══════════════════════════════════════════════════════════════════════

    def _fmt_machine(m: dict) -> str:
        tags = ", ".join(m.get("machineTags", []) or []) or "—"
        return (
            f"  {m.get('computerDnsName', '?')} (id: {m.get('id', '?')})\n"
            f"    OS: {m.get('osPlatform', '?')} {m.get('osVersion', '') or m.get('version', '')} | "
            f"Health: {m.get('healthStatus', '?')} | "
            f"Risk: {m.get('riskScore', '?')} | Exposure: {m.get('exposureLevel', '?')}\n"
            f"    Last IP: {m.get('lastIpAddress', '?')} | "
            f"Last seen: {_dt(m.get('lastSeen'))} | Tags: {tags}"
        )

    async def list_defender_machines(
        count: int = 25,
        health_status: str = "",
        risk_score: str = "",
        os_platform: str = "",
        odata_filter: str = "",
    ) -> str:
        """List onboarded devices (machines) in Defender for Endpoint."""
        access_token = await _mde_token()
        if not access_token:
            return _AUTH_ERROR

        params: dict[str, Any] = {"$top": min(count, 100)}
        filters: list[str] = []
        if odata_filter:
            filters.append(odata_filter)
        if health_status:
            filters.append(f"healthStatus eq '{_odata_quote(health_status)}'")
        if risk_score:
            filters.append(f"riskScore eq '{_odata_quote(risk_score)}'")
        if os_platform:
            filters.append(f"osPlatform eq '{_odata_quote(os_platform)}'")
        if filters:
            params["$filter"] = " and ".join(filters)

        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{_MDE_BASE}/machines",
                _PROVIDER,
                "list machines",
                headers=_headers(access_token),
                params=params,
            )
            if err:
                return err
            machines = resp.json().get("value", [])
            return format_entity_list(machines, _fmt_machine, noun="machine")

    async def get_defender_machine(machine_id: str) -> str:
        """Get full details of a Defender for Endpoint device."""
        access_token = await _mde_token()
        if not access_token:
            return _AUTH_ERROR

        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{_MDE_BASE}/machines/{machine_id}",
                _PROVIDER,
                f"get machine {machine_id}",
                headers=_headers(access_token),
            )
            if err:
                return err
            m = resp.json()
            # ``ipAddresses`` is a collection of IpAddress *objects*
            # ({"ipAddress", "macAddress", "operationalStatus", …}) per the MDE
            # Machine resource — not bare strings — so pull the address field
            # out of each before joining (joining the dicts directly would raise
            # TypeError). Fall back to ``lastIpAddress`` when the collection is
            # absent or yields nothing.
            ip_values = [
                (ip.get("ipAddress") if isinstance(ip, dict) else ip)
                for ip in (m.get("ipAddresses") or [])
            ]
            ip_values = [str(v) for v in ip_values if v]
            ips = ", ".join(ip_values)[:300] if ip_values else m.get("lastIpAddress", "?")
            lines = [
                _fmt_machine(m),
                f"    First seen: {_dt(m.get('firstSeen'))}",
                f"    AAD device ID: {m.get('aadDeviceId', '—')}",
                f"    Onboarding: {m.get('onboardingStatus', '?')} | "
                f"AV status: {m.get('defenderAvStatus', '?')}",
                f"    Last external IP: {m.get('lastExternalIpAddress', '?')}",
                f"    IPs: {ips}",
            ]
            return "\n".join(lines)

    async def list_defender_machine_vulnerabilities(machine_id: str) -> str:
        """List vulnerabilities (CVEs) discovered on a specific device."""
        access_token = await _mde_token()
        if not access_token:
            return _AUTH_ERROR

        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{_MDE_BASE}/machines/{machine_id}/vulnerabilities",
                _PROVIDER,
                f"list vulnerabilities for machine {machine_id}",
                headers=_headers(access_token),
            )
            if err:
                return err
            vulns = resp.json().get("value", [])

            def _fmt(v: dict) -> str:
                return (
                    f"  {v.get('id', '?')} [{v.get('severity', '?')}] "
                    f"CVSS {v.get('cvssV3', '?')} — {_trunc(v.get('name'), 80)}\n"
                    f"    Public exploit: {v.get('publicExploit', '?')} | "
                    f"Published: {_dt(v.get('publishedOn'))}"
                )

            return format_entity_list(vulns, _fmt, noun="vulnerability")

    async def get_defender_machine_logged_on_users(machine_id: str) -> str:
        """List users that logged on to a specific device."""
        access_token = await _mde_token()
        if not access_token:
            return _AUTH_ERROR

        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{_MDE_BASE}/machines/{machine_id}/logonusers",
                _PROVIDER,
                f"list logged-on users for machine {machine_id}",
                headers=_headers(access_token),
            )
            if err:
                return err
            users = resp.json().get("value", [])

            def _fmt(u: dict) -> str:
                flags = []
                if u.get("isDomainAdmin"):
                    flags.append("domain-admin")
                if u.get("isOnlyNetworkUser"):
                    flags.append("network-only")
                flag_str = f" ({', '.join(flags)})" if flags else ""
                return (
                    f"  {u.get('accountDomain', '?')}\\{u.get('accountName', '?')}{flag_str}\n"
                    f"    Logon types: {', '.join(u.get('logonTypes', '').split(',')) if u.get('logonTypes') else 'N/A'} | "
                    f"First: {_dt(u.get('firstSeen'))} | Last: {_dt(u.get('lastSeen'))}"
                )

            return format_entity_list(users, _fmt, noun="logged-on user")

    # ══════════════════════════════════════════════════════════════════════
    # 6. RESPONSE ACTIONS / MACHINE ACTIONS (Defender for Endpoint)
    # ══════════════════════════════════════════════════════════════════════

    async def _machine_action(
        machine_id: str, endpoint: str, action_label: str, body: dict[str, Any]
    ) -> str:
        access_token = await _mde_token()
        if not access_token:
            return _AUTH_ERROR
        async with egress_client(timeout=60) as client:
            resp, err = await safe_request(
                client,
                "post",
                f"{_MDE_BASE}/machines/{machine_id}/{endpoint}",
                _PROVIDER,
                f"{action_label} on machine {machine_id}",
                headers=_headers(access_token),
                json=body,
            )
            if err:
                return err
            action = resp.json()
            return (
                f"{action_label} initiated on machine {machine_id} "
                f"(action id: {action.get('id', '?')}, status: {action.get('status', 'Pending')})."
            )

    async def isolate_defender_machine(
        machine_id: str, comment: str, isolation_type: str = "Full"
    ) -> str:
        """Isolate a device from the network (containment). Reversible."""
        return await _machine_action(
            machine_id,
            "isolate",
            "Isolation",
            {"Comment": comment, "IsolationType": isolation_type},
        )

    async def unisolate_defender_machine(machine_id: str, comment: str) -> str:
        """Release a device from network isolation."""
        return await _machine_action(
            machine_id, "unisolate", "Release from isolation", {"Comment": comment}
        )

    async def run_defender_antivirus_scan(
        machine_id: str, comment: str, scan_type: str = "Quick"
    ) -> str:
        """Run a Microsoft Defender Antivirus scan (Quick or Full) on a device."""
        return await _machine_action(
            machine_id,
            "runAntiVirusScan",
            "Antivirus scan",
            {"Comment": comment, "ScanType": scan_type},
        )

    async def collect_defender_investigation_package(
        machine_id: str, comment: str
    ) -> str:
        """Collect a forensic investigation package from a device."""
        return await _machine_action(
            machine_id,
            "collectInvestigationPackage",
            "Investigation package collection",
            {"Comment": comment},
        )

    async def restrict_defender_app_execution(machine_id: str, comment: str) -> str:
        """Restrict application execution on a device (only signed Microsoft apps)."""
        return await _machine_action(
            machine_id,
            "restrictCodeExecution",
            "App execution restriction",
            {"Comment": comment},
        )

    async def unrestrict_defender_app_execution(machine_id: str, comment: str) -> str:
        """Remove application execution restrictions from a device."""
        return await _machine_action(
            machine_id,
            "unrestrictCodeExecution",
            "App execution restriction removal",
            {"Comment": comment},
        )

    async def stop_and_quarantine_file(
        machine_id: str, sha1: str, comment: str
    ) -> str:
        """Stop and quarantine a file (by SHA1) on a device."""
        return await _machine_action(
            machine_id,
            "StopAndQuarantineFile",
            "Stop & quarantine file",
            {"Comment": comment, "Sha1": sha1},
        )

    async def run_defender_live_response_script(machine_id: str, script_name: str, comment: str, args: str = "") -> str:
        """Run a Live Response library script on a device (approval-gated).

        Only a script already uploaded to the tenant's Live Response library
        runs: the tool never sends code of its own. The run is a machine
        action, cancelled by ``cancel_defender_machine_action`` while it is
        pending; its output is read with ``get_defender_live_response_result``.
        """
        name = (script_name or "").strip()
        if not name or "/" in name or "\\" in name:
            return (
                "Error: script_name is the file name of a script of the Live "
                "Response library (list_defender_live_response_library lists them)."
            )
        if not (comment or "").strip():
            return "Error: comment (the reason of the run) is required."
        params = [{"key": "ScriptName", "value": name}]
        if (args or "").strip():
            params.append({"key": "Args", "value": args.strip()})
        result = await _machine_action(
            _url_encode((machine_id or "").strip(), safe=""),
            "runliveresponse",
            "Live Response script run",
            {"Commands": [{"type": "RunScript", "params": params}], "Comment": comment},
        )
        if result.startswith("Error"):
            return result
        return (
            f"{result} Undo while it is pending: cancel_defender_machine_action. "
            "Output once it succeeded: get_defender_live_response_result."
        )

    async def list_defender_live_response_library() -> str:
        """List the scripts of the tenant's Live Response library."""
        access_token = await _mde_token()
        if not access_token:
            return _AUTH_ERROR
        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{_MDE_BASE}/libraryfiles",
                _PROVIDER,
                "list the Live Response library",
                headers=_headers(access_token),
            )
        if err:
            return err
        files = (resp.json() or {}).get("value", []) or []

        def _fmt(f: dict) -> str:
            params = " (takes parameters)" if f.get("hasParameters") else ""
            return f"  {f.get('fileName', '?')}{params}: {_trunc(f.get('description'), 160)}"

        return format_entity_list(files, _fmt, noun="library script")

    async def get_defender_live_response_result(action_id: str) -> str:
        """The exit code, output and errors of a Live Response script run."""
        aid = (action_id or "").strip()
        if not aid:
            return "Error: action_id is required (the id run_defender_live_response_script returned)."
        access_token = await _mde_token()
        if not access_token:
            return _AUTH_ERROR
        action_url = f"{_MDE_BASE}/machineactions/{_url_encode(aid, safe='')}"
        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "get",
                action_url,
                _PROVIDER,
                f"get machine action {aid}",
                headers=_headers(access_token),
            )
            if err:
                return err
            action = resp.json() or {}
            kind = action.get("type") or "unknown"
            if kind != "LiveResponse":
                return f"Error: machine action {aid} is of type {kind}, not a Live Response run."
            status = action.get("status") or "unknown"
            device = action.get("computerDnsName") or action.get("machineId") or "the device"
            if status in ("Pending", "InProgress"):
                return f"The Live Response run on {device} is not finished (status: {status}). Ask again later."
            if status != "Succeeded":
                return f"The Live Response run on {device} ended with status {status}: no output."
            link_resp, err = await safe_request(
                client,
                "get",
                f"{action_url}/GetLiveResponseResultDownloadLink(index=0)",
                _PROVIDER,
                f"get the result link of machine action {aid}",
                headers=_headers(access_token),
            )
            if err:
                return err
        link = str((link_resp.json() or {}).get("value") or "").strip().strip('"')
        download = await fetch_public_media(
            link, source=_PROVIDER, max_bytes=_MAX_LIVE_RESPONSE_BYTES, what="Live Response result"
        )
        if isinstance(download, FetchError):
            return str(download)
        try:
            data = json.loads(download.data.decode("utf-8", "replace") or "{}")
        except ValueError:
            return "Error: the Live Response result is not the JSON Defender documents."
        if not isinstance(data, dict):
            data = {}
        output = str(data.get("script_output") or "")
        errors = str(data.get("script_errors") or "")
        lines = [
            f"Live Response run {aid} on {device}: script "
            f"{data.get('script_name', '?')}, exit code {data.get('exit_code', '?')}.",
            "Output:",
            output[:_MAX_LIVE_RESPONSE_TEXT] or "(empty)",
        ]
        if len(output) > _MAX_LIVE_RESPONSE_TEXT:
            lines.append(f"[output cut at {_MAX_LIVE_RESPONSE_TEXT} of {len(output)} characters]")
        if errors.strip():
            lines += ["Errors:", errors[:_MAX_LIVE_RESPONSE_TEXT]]
        return "\n".join(lines)

    async def list_defender_machine_actions(count: int = 25) -> str:
        """List recent response/machine actions across Defender for Endpoint."""
        access_token = await _mde_token()
        if not access_token:
            return _AUTH_ERROR

        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{_MDE_BASE}/machineactions",
                _PROVIDER,
                "list machine actions",
                headers=_headers(access_token),
                params={"$top": min(count, 100)},
            )
            if err:
                return err
            actions = resp.json().get("value", [])

            def _fmt(a: dict) -> str:
                return (
                    f"  [{a.get('type', '?')}] {a.get('status', '?')} — "
                    f"{a.get('computerDnsName', a.get('machineId', '?'))}\n"
                    f"    id: {a.get('id', '?')} | Requestor: {a.get('requestor', '?')} | "
                    f"Created: {_dt(a.get('creationDateTime'))}\n"
                    f"    Comment: {_trunc(a.get('requestorComment'), 120)}"
                )

            return format_entity_list(actions, _fmt, noun="machine action")

    async def get_defender_machine_action(action_id: str) -> str:
        """Get the status and details of a specific machine action."""
        access_token = await _mde_token()
        if not access_token:
            return _AUTH_ERROR

        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{_MDE_BASE}/machineactions/{action_id}",
                _PROVIDER,
                f"get machine action {action_id}",
                headers=_headers(access_token),
            )
            if err:
                return err
            a = resp.json()
            lines = [
                f"Machine action {a.get('id', '?')}: {a.get('type', '?')}",
                f"Status: {a.get('status', '?')}",
                f"Device: {a.get('computerDnsName', a.get('machineId', '?'))}",
                f"Requestor: {a.get('requestor', '?')}",
                f"Comment: {a.get('requestorComment', '—')}",
                f"Created: {_dt(a.get('creationDateTime'))} | "
                f"Last updated: {_dt(a.get('lastUpdateDateTime'))}",
            ]
            return "\n".join(lines)

    async def get_defender_investigation_package(
        action_id: str, file_context: FileContext | None = None
    ) -> ToolResult | str:
        """Hand over a collected investigation package: its link and the ZIP.

        The time-limited SAS link Defender issues, and the ZIP itself stored as
        a file of the conversation (or run) within the download limit, like
        every other downloaded file.
        """
        aid = (action_id or "").strip()
        if not aid:
            return (
                "Error: action_id is required (the id collect_defender_investigation_package "
                "returned, also listed by list_defender_machine_actions)."
            )
        access_token = await _mde_token()
        if not access_token:
            return _AUTH_ERROR
        action_url = f"{_MDE_BASE}/machineactions/{_url_encode(aid, safe='')}"
        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "get",
                action_url,
                _PROVIDER,
                f"get machine action {aid}",
                headers=_headers(access_token),
            )
            if err:
                return err
            action = resp.json() or {}
            kind = action.get("type") or "unknown"
            if kind != "CollectInvestigationPackage":
                return (
                    f"Error: machine action {aid} is of type {kind}, not an "
                    "investigation package collection."
                )
            status = action.get("status") or "unknown"
            device = action.get("computerDnsName") or action.get("machineId") or "the device"
            if status in ("Pending", "InProgress"):
                return (
                    f"The investigation package of {device} is not ready yet (status: "
                    f"{status}). Ask again later with get_defender_investigation_package."
                )
            if status != "Succeeded":
                return (
                    f"The investigation package collection on {device} ended with status "
                    f"{status}: there is no package to download."
                )
            uri_resp, err = await safe_request(
                client,
                "get",
                f"{action_url}/GetPackageUri",
                _PROVIDER,
                f"get the package link of machine action {aid}",
                headers=_headers(access_token),
            )
            if err:
                return err
        link = str((uri_resp.json() or {}).get("value") or "").strip().strip('"')
        if not link.lower().startswith("https://"):
            return "Error: Defender returned no download link for the investigation package."
        link_line = f"Download link (time-limited, valid for a short while only): {link}"
        # The link comes from the API answer and is pre-authenticated: fetched
        # without the token, every request (redirects included) held to the
        # outbound URL policy.
        download = await fetch_public_media(
            link,
            source=_PROVIDER,
            max_bytes=_MAX_PACKAGE_BYTES,
            what="investigation package",
        )
        limit_mb = _MAX_PACKAGE_BYTES // 1024 // 1024
        header = f"Investigation package of {device} (machine action {aid})."
        if isinstance(download, FetchError) and download.too_large:
            size = (
                f"{round(download.size / 1024 / 1024, 1)} MB"
                if download.size
                else f"over {limit_mb} MB"
            )
            return (
                f"{header}\nThe package is {size}, over the {limit_mb} MB download "
                f"limit, so it was not saved in the conversation.\n{link_line}"
            )
        if isinstance(download, FetchError):
            expired = (
                "\nThe link may have expired already: call "
                "get_defender_investigation_package again for a fresh one."
                if download.status in (401, 403)
                else ""
            )
            return f"{download}\n{header}{expired}"
        if not download.data:
            return f"Error: the investigation package is empty.\n{link_line}"
        safe_device = re.sub(r"[^A-Za-z0-9._-]+", "-", str(device)).strip("-.") or "device"
        name = f"investigation-package-{safe_device}.zip"
        file_ref = await keep_file(persist_file_fn, file_context, name, download.data, "application/zip")
        lines = [
            f"{header} {kilobytes(len(download.data))}.",
            link_line,
            kept_file_line(file_ref),
            _package_contents(download.data),
            rest_of_file_hint(file_ref),
        ]
        # The ZIP stays off the tool result: the model reads no archive, and a
        # file on the result is fetched again on every request of the run.
        return ToolResult(tool_call_id="", content="\n".join(lines))

    async def cancel_defender_machine_action(action_id: str, comment: str) -> str:
        """Cancel a pending/in-progress machine action."""
        access_token = await _mde_token()
        if not access_token:
            return _AUTH_ERROR

        async with egress_client(timeout=30) as client:
            _, err = await safe_request(
                client,
                "post",
                f"{_MDE_BASE}/machineactions/{action_id}/cancel",
                _PROVIDER,
                f"cancel machine action {action_id}",
                headers=_headers(access_token),
                json={"Comment": comment},
            )
            if err:
                return err
            return f"Machine action {action_id} cancelled."

    # ══════════════════════════════════════════════════════════════════════
    # 7. THREAT & VULNERABILITY MANAGEMENT (Defender for Endpoint)
    # ══════════════════════════════════════════════════════════════════════

    async def list_defender_vulnerabilities(
        count: int = 25, odata_filter: str = ""
    ) -> str:
        """List vulnerabilities (CVEs) known across the organization (TVM)."""
        access_token = await _mde_token()
        if not access_token:
            return _AUTH_ERROR

        params: dict[str, Any] = {"$top": min(count, 100)}
        if odata_filter:
            params["$filter"] = odata_filter

        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{_MDE_BASE}/vulnerabilities",
                _PROVIDER,
                "list vulnerabilities",
                headers=_headers(access_token),
                params=params,
            )
            if err:
                return err
            vulns = resp.json().get("value", [])

            def _fmt(v: dict) -> str:
                return (
                    f"  {v.get('id', '?')} [{v.get('severity', '?')}] "
                    f"CVSS {v.get('cvssV3', '?')} — {_trunc(v.get('name'), 70)}\n"
                    f"    Exposed devices: {v.get('exposedMachines', '?')} | "
                    f"Public exploit: {v.get('publicExploit', '?')} | "
                    f"Published: {_dt(v.get('publishedOn'))}"
                )

            return format_entity_list(vulns, _fmt, noun="vulnerability")

    async def get_defender_vulnerability(cve_id: str) -> str:
        """Get TVM details for a specific vulnerability (CVE)."""
        access_token = await _mde_token()
        if not access_token:
            return _AUTH_ERROR

        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{_MDE_BASE}/vulnerabilities/{cve_id}",
                _PROVIDER,
                f"get vulnerability {cve_id}",
                headers=_headers(access_token),
            )
            if err:
                return err
            v = resp.json()
            lines = [
                f"Vulnerability: {v.get('id', cve_id)} — {v.get('name', '')}",
                f"Severity: {v.get('severity', '?')} | CVSS v3: {v.get('cvssV3', '?')}",
                f"Exposed devices: {v.get('exposedMachines', '?')}",
                f"Public exploit: {v.get('publicExploit', '?')} | "
                f"Exploit verified: {v.get('exploitVerified', '?')}",
                f"Published: {_dt(v.get('publishedOn'))} | "
                f"Updated: {_dt(v.get('updatedOn'))}",
            ]
            if v.get("description"):
                lines.append(f"\nDescription:\n{_trunc(v.get('description'), 1500)}")
            return "\n".join(lines)

    async def list_defender_vulnerability_machines(cve_id: str) -> str:
        """List devices affected by a specific vulnerability (CVE)."""
        access_token = await _mde_token()
        if not access_token:
            return _AUTH_ERROR

        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{_MDE_BASE}/vulnerabilities/{cve_id}/machineReferences",
                _PROVIDER,
                f"list devices for vulnerability {cve_id}",
                headers=_headers(access_token),
            )
            if err:
                return err
            machines = resp.json().get("value", [])

            def _fmt(m: dict) -> str:
                return (
                    f"  {m.get('computerDnsName', '?')} (id: {m.get('id', '?')}) — "
                    f"OS: {m.get('osPlatform', '?')}"
                )

            return format_entity_list(machines, _fmt, noun="affected device")

    async def list_defender_software(count: int = 25, odata_filter: str = "") -> str:
        """List software inventory across the organization (TVM)."""
        access_token = await _mde_token()
        if not access_token:
            return _AUTH_ERROR

        params: dict[str, Any] = {"$top": min(count, 100)}
        if odata_filter:
            params["$filter"] = odata_filter

        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{_MDE_BASE}/Software",
                _PROVIDER,
                "list software",
                headers=_headers(access_token),
                params=params,
            )
            if err:
                return err
            software = resp.json().get("value", [])

            def _fmt(s: dict) -> str:
                return (
                    f"  {s.get('vendor', '?')} {s.get('name', '?')} "
                    f"(id: {s.get('id', '?')})\n"
                    f"    Weaknesses: {s.get('weaknesses', '?')} | "
                    f"Public exploit: {s.get('publicExploit', '?')} | "
                    f"Active alert: {s.get('activeAlert', '?')} | "
                    f"Installed: {s.get('installedMachines', '?')} devices"
                )

            return format_entity_list(software, _fmt, noun="software")

    async def list_defender_security_recommendations(
        count: int = 25, odata_filter: str = ""
    ) -> str:
        """List security recommendations from Defender Vulnerability Management."""
        access_token = await _mde_token()
        if not access_token:
            return _AUTH_ERROR

        params: dict[str, Any] = {"$top": min(count, 100)}
        if odata_filter:
            params["$filter"] = odata_filter

        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{_MDE_BASE}/recommendations",
                _PROVIDER,
                "list security recommendations",
                headers=_headers(access_token),
                params=params,
            )
            if err:
                return err
            recs = resp.json().get("value", [])

            def _fmt(r: dict) -> str:
                return (
                    f"  {r.get('recommendationName', '?')} "
                    f"(id: {r.get('id', '?')})\n"
                    f"    Severity score: {r.get('severityScore', '?')} | "
                    f"Status: {r.get('status', '?')} | "
                    f"Exposed: {r.get('exposedMachinesCount', '?')} devices\n"
                    f"    Public exploit: {r.get('publicExploit', '?')} | "
                    f"Active alert: {r.get('activeAlert', '?')} | "
                    f"Remediation: {r.get('remediationType', '?')}"
                )

            return format_entity_list(recs, _fmt, noun="recommendation")

    async def get_defender_recommendation(recommendation_id: str) -> str:
        """Get full details of a security recommendation."""
        access_token = await _mde_token()
        if not access_token:
            return _AUTH_ERROR

        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{_MDE_BASE}/recommendations/{recommendation_id}",
                _PROVIDER,
                f"get recommendation {recommendation_id}",
                headers=_headers(access_token),
            )
            if err:
                return err
            r = resp.json()
            lines = [
                f"Recommendation: {r.get('recommendationName', '?')}",
                f"Product: {r.get('vendor', '?')} {r.get('productName', '?')} | "
                f"Recommended version: {r.get('recommendedVersion', 'N/A')}",
                f"Severity score: {r.get('severityScore', '?')} | "
                f"Status: {r.get('status', '?')}",
                f"Category: {r.get('recommendationCategory', '?')} / {r.get('subCategory', '?')}",
                f"Exposed devices: {r.get('exposedMachinesCount', '?')} / "
                f"{r.get('totalMachineCount', '?')} total",
                f"Public exploit: {r.get('publicExploit', '?')} | "
                f"Active alert: {r.get('activeAlert', '?')}",
                f"Remediation type: {r.get('remediationType', '?')}",
            ]
            threats = r.get("associatedThreats", []) or []
            if threats:
                lines.append(f"Associated threats: {', '.join(str(t) for t in threats)}")
            return "\n".join(lines)

    async def get_defender_exposure_score() -> str:
        """Get the organization's TVM exposure score."""
        access_token = await _mde_token()
        if not access_token:
            return _AUTH_ERROR

        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{_MDE_BASE}/exposureScore",
                _PROVIDER,
                "get exposure score",
                headers=_headers(access_token),
            )
            if err:
                return err
            data = resp.json()
            return (
                f"Organization exposure score: {data.get('score', '?')} "
                f"(as of {_dt(data.get('time'))})"
            )

    async def get_defender_device_secure_score() -> str:
        """Get the organization's Microsoft Secure Score for Devices (config score)."""
        access_token = await _mde_token()
        if not access_token:
            return _AUTH_ERROR

        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{_MDE_BASE}/configurationScore",
                _PROVIDER,
                "get device secure score",
                headers=_headers(access_token),
            )
            if err:
                return err
            data = resp.json()
            return (
                f"Microsoft Secure Score for Devices: {data.get('score', '?')} "
                f"(as of {_dt(data.get('time'))})"
            )

    # ══════════════════════════════════════════════════════════════════════
    # 8. ENTITY INVESTIGATION — File / IP / Domain (Defender for Endpoint)
    # ══════════════════════════════════════════════════════════════════════

    async def get_defender_file_info(file_hash: str) -> str:
        """Get file reputation/metadata by SHA1 or SHA256 (Defender for Endpoint)."""
        access_token = await _mde_token()
        if not access_token:
            return _AUTH_ERROR

        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{_MDE_BASE}/files/{file_hash}",
                _PROVIDER,
                f"get file info {file_hash}",
                headers=_headers(access_token),
            )
            if err:
                return err
            f = resp.json()
            lines = [
                f"File: {f.get('sha256', f.get('sha1', file_hash))}",
                f"SHA1: {f.get('sha1', '?')} | MD5: {f.get('md5', '?')}",
                f"Type: {f.get('fileType', '?')} | Size: {f.get('size', '?')} bytes | "
                f"PE file: {f.get('isPeFile', '?')}",
                f"Publisher: {f.get('filePublisher', '?')} | "
                f"Product: {f.get('fileProductName', '?')}",
                f"Signer: {f.get('signer', '?')} | Issuer: {f.get('issuer', '?')} | "
                f"Valid certificate: {f.get('isValidCertificate', '?')}",
                f"Global prevalence: {f.get('globalPrevalence', '?')} | "
                f"First observed: {_dt(f.get('globalFirstObserved'))} | "
                f"Last observed: {_dt(f.get('globalLastObserved'))}",
                f"Determination: {f.get('determinationType', '?')} "
                f"({f.get('determinationValue', '—')})",
            ]
            return "\n".join(lines)

    async def get_defender_file_stats(file_hash: str) -> str:
        """Get organization + global prevalence stats for a file (by SHA1)."""
        access_token = await _mde_token()
        if not access_token:
            return _AUTH_ERROR

        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{_MDE_BASE}/files/{file_hash}/stats",
                _PROVIDER,
                f"get file stats {file_hash}",
                headers=_headers(access_token),
            )
            if err:
                return err
            s = resp.json()
            top_names = ", ".join(s.get("topFileNames", []) or []) or "—"
            return (
                f"File stats for {s.get('sha1', file_hash)}:\n"
                f"  Org prevalence: {s.get('orgPrevalence', '?')} | "
                f"First seen: {_dt(s.get('orgFirstSeen'))} | "
                f"Last seen: {_dt(s.get('orgLastSeen'))}\n"
                f"  Global prevalence: {s.get('globalPrevalence', '?')} | "
                f"First observed: {_dt(s.get('globalFirstObserved'))} | "
                f"Last observed: {_dt(s.get('globalLastObserved'))}\n"
                f"  Top file names: {top_names}"
            )

    async def get_defender_ip_stats(ip_address: str) -> str:
        """Get organization prevalence stats for an IP address."""
        access_token = await _mde_token()
        if not access_token:
            return _AUTH_ERROR

        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{_MDE_BASE}/ips/{ip_address}/stats",
                _PROVIDER,
                f"get IP stats {ip_address}",
                headers=_headers(access_token),
            )
            if err:
                return err
            s = resp.json()
            return (
                f"IP stats for {s.get('ipAddress', ip_address)}:\n"
                f"  Org prevalence: {s.get('orgPrevalence', '?')} | "
                f"First seen: {_dt(s.get('orgFirstSeen'))} | "
                f"Last seen: {_dt(s.get('orgLastSeen'))}"
            )

    async def get_defender_domain_stats(domain: str) -> str:
        """Get organization prevalence stats for a domain."""
        access_token = await _mde_token()
        if not access_token:
            return _AUTH_ERROR

        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{_MDE_BASE}/domains/{domain}/stats",
                _PROVIDER,
                f"get domain stats {domain}",
                headers=_headers(access_token),
            )
            if err:
                return err
            s = resp.json()
            return (
                f"Domain stats for {s.get('host', domain)}:\n"
                f"  Org prevalence: {s.get('orgPrevalence', '?')} | "
                f"First seen: {_dt(s.get('orgFirstSeen'))} | "
                f"Last seen: {_dt(s.get('orgLastSeen'))}"
            )

    # ══════════════════════════════════════════════════════════════════════
    # 9. SECURE SCORE (Microsoft Graph security)
    # ══════════════════════════════════════════════════════════════════════

    async def get_defender_secure_score(count: int = 5) -> str:
        """Get the most recent Microsoft Secure Score snapshots for the tenant."""
        access_token = await _graph_token()
        if not access_token:
            return _AUTH_ERROR

        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{_GRAPH_BASE}/security/secureScores",
                _PROVIDER,
                "get secure scores",
                headers=_headers(access_token),
                params={"$top": min(count, 10)},
            )
            if err:
                return err
            scores = resp.json().get("value", [])
            if not scores:
                return "No secure score data found."

            lines = [f"Secure Score snapshots ({len(scores)}):"]
            for s in scores:
                current = s.get("currentScore", "?")
                maximum = s.get("maxScore", "?")
                pct = ""
                if isinstance(current, (int, float)) and isinstance(
                    maximum, (int, float)
                ) and maximum:
                    pct = f" ({current * 100 / maximum:.1f}%)"
                lines.append(
                    f"  {_dt(s.get('createdDateTime'))}: {current}/{maximum}{pct} | "
                    f"Active users: {s.get('activeUserCount', '?')}"
                )
            return "\n".join(lines)

    async def list_defender_secure_score_controls(count: int = 25) -> str:
        """List Secure Score control profiles (improvement actions)."""
        access_token = await _graph_token()
        if not access_token:
            return _AUTH_ERROR

        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{_GRAPH_BASE}/security/secureScoreControlProfiles",
                _PROVIDER,
                "list secure score controls",
                headers=_headers(access_token),
                params={"$top": min(count, 100)},
            )
            if err:
                return err
            controls = resp.json().get("value", [])

            def _fmt(c: dict) -> str:
                return (
                    f"  {c.get('title', c.get('id', '?'))} "
                    f"(id: {c.get('id', '?')})\n"
                    f"    Service: {c.get('service', '?')} | "
                    f"Category: {c.get('controlCategory', '?')} | "
                    f"Max score: {c.get('maxScore', '?')} | Rank: {c.get('rank', '?')}\n"
                    f"    Action: {c.get('actionType', '?')} | "
                    f"User impact: {c.get('userImpact', '?')} | "
                    f"Cost: {c.get('implementationCost', '?')}"
                )

            return format_entity_list(controls, _fmt, noun="control profile")

    # ══════════════════════════════════════════════════════════════════════
    # REGISTER ALL TOOLS
    # ══════════════════════════════════════════════════════════════════════

    executor.register("list_defender_incidents", list_defender_incidents)
    executor.register("get_defender_incident", get_defender_incident)
    executor.register("update_defender_incident", update_defender_incident)
    executor.register("add_defender_incident_comment", add_defender_incident_comment)
    executor.register("list_defender_alerts", list_defender_alerts)
    executor.register("get_defender_alert", get_defender_alert)
    executor.register("update_defender_alert", update_defender_alert)
    executor.register("add_defender_alert_comment", add_defender_alert_comment)
    executor.register("run_defender_hunting_query", run_defender_hunting_query)
    executor.register("list_defender_ti_articles", list_defender_ti_articles)
    executor.register("get_defender_ti_article", get_defender_ti_article)
    executor.register("list_defender_intel_profiles", list_defender_intel_profiles)
    executor.register("get_defender_intel_profile", get_defender_intel_profile)
    executor.register("get_defender_ti_host", get_defender_ti_host)
    executor.register(
        "get_defender_ti_host_reputation", get_defender_ti_host_reputation
    )
    executor.register("get_defender_ti_passive_dns", get_defender_ti_passive_dns)
    executor.register("get_defender_ti_vulnerability", get_defender_ti_vulnerability)
    executor.register("list_defender_indicators", list_defender_indicators)
    executor.register("create_defender_indicator", create_defender_indicator)
    executor.register("delete_defender_indicator", delete_defender_indicator)
    executor.register("list_defender_detection_rules", list_defender_detection_rules)
    executor.register("get_defender_detection_rule", get_defender_detection_rule)
    executor.register("create_defender_detection_rule", create_defender_detection_rule)
    executor.register("update_defender_detection_rule", update_defender_detection_rule)
    executor.register("delete_defender_detection_rule", delete_defender_detection_rule)
    executor.register("list_defender_machines", list_defender_machines)
    executor.register("get_defender_machine", get_defender_machine)
    executor.register(
        "list_defender_machine_vulnerabilities", list_defender_machine_vulnerabilities
    )
    executor.register(
        "get_defender_machine_logged_on_users", get_defender_machine_logged_on_users
    )
    executor.register("isolate_defender_machine", isolate_defender_machine)
    executor.register("unisolate_defender_machine", unisolate_defender_machine)
    executor.register("run_defender_antivirus_scan", run_defender_antivirus_scan)
    executor.register(
        "collect_defender_investigation_package",
        collect_defender_investigation_package,
    )
    executor.register(
        "restrict_defender_app_execution", restrict_defender_app_execution
    )
    executor.register(
        "unrestrict_defender_app_execution", unrestrict_defender_app_execution
    )
    executor.register("stop_and_quarantine_file", stop_and_quarantine_file)
    executor.register("run_defender_live_response_script", run_defender_live_response_script)
    executor.register("list_defender_live_response_library", list_defender_live_response_library)
    executor.register("get_defender_live_response_result", get_defender_live_response_result)
    executor.register("list_defender_machine_actions", list_defender_machine_actions)
    executor.register("get_defender_machine_action", get_defender_machine_action)
    executor.register(
        "get_defender_investigation_package", get_defender_investigation_package
    )
    executor.register(
        "cancel_defender_machine_action", cancel_defender_machine_action
    )
    executor.register("list_defender_vulnerabilities", list_defender_vulnerabilities)
    executor.register("get_defender_vulnerability", get_defender_vulnerability)
    executor.register(
        "list_defender_vulnerability_machines", list_defender_vulnerability_machines
    )
    executor.register("list_defender_software", list_defender_software)
    executor.register(
        "list_defender_security_recommendations",
        list_defender_security_recommendations,
    )
    executor.register("get_defender_recommendation", get_defender_recommendation)
    executor.register("get_defender_exposure_score", get_defender_exposure_score)
    executor.register(
        "get_defender_device_secure_score", get_defender_device_secure_score
    )
    executor.register("get_defender_file_info", get_defender_file_info)
    executor.register("get_defender_file_stats", get_defender_file_stats)
    executor.register("get_defender_ip_stats", get_defender_ip_stats)
    executor.register("get_defender_domain_stats", get_defender_domain_stats)
    executor.register("get_defender_secure_score", get_defender_secure_score)
    executor.register(
        "list_defender_secure_score_controls", list_defender_secure_score_controls
    )

    # ──────────────────────────────────────────────────────────────────────
    # TOOL DEFINITIONS
    # ──────────────────────────────────────────────────────────────────────

    sev_enum = ["informational", "low", "medium", "high"]
    classification_enum = [
        "unknown",
        "falsePositive",
        "truePositive",
        "informationalExpectedActivity",
    ]
    determination_enum = [
        "unknown",
        "apt",
        "malware",
        "securityPersonnel",
        "securityTesting",
        "unwantedSoftware",
        "other",
        "multiStagedAttack",
        "compromisedUser",
        "phishing",
        "maliciousUserActivity",
        "clean",
        "insufficientData",
        "confirmedUserActivity",
        "lineOfBusinessApplication",
    ]

    src = "integration:defender"

    return [
        # ── 0. Incidents ──────────────────────────────────────────────────
        ToolDefinition(
            name="list_defender_incidents",
            description=(
                "List Microsoft Defender XDR incidents. Filter by status "
                "(active, resolved, redirected), severity (high/medium/low/"
                "informational), and assignee. Returns incident id, severity, "
                "status, title, owner, classification, tags."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "status": {
                        "type": "string",
                        "enum": ["active", "resolved", "redirected"],
                    },
                    "severity": {"type": "string", "enum": sev_enum},
                    "assigned_to": {
                        "type": "string",
                        "description": "Filter by owner UPN/email",
                    },
                    "count": {"type": "integer", "default": 25},
                },
                "required": [],
            },
            source=src,
            priority=0,
        ),
        ToolDefinition(
            name="get_defender_incident",
            description=(
                "Get full details of a Defender XDR incident including severity, "
                "status, classification, determination, owner, summary, comments, "
                "and (optionally) the correlated alerts."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "incident_id": {"type": "string"},
                    "include_alerts": {"type": "boolean", "default": True},
                },
                "required": ["incident_id"],
            },
            source=src,
            priority=0,
        ),
        ToolDefinition(
            name="update_defender_incident",
            description=(
                "Update a Defender XDR incident: status, severity, classification, "
                "determination, owner, display name, resolving comment, or custom "
                "tags. Only provided fields change."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "incident_id": {"type": "string"},
                    "status": {
                        "type": "string",
                        "enum": ["active", "resolved", "redirected"],
                    },
                    "severity": {"type": "string", "enum": sev_enum},
                    "classification": {
                        "type": "string",
                        "enum": classification_enum,
                    },
                    "determination": {"type": "string", "enum": determination_enum},
                    "assigned_to": {
                        "type": "string",
                        "description": "Owner UPN/email",
                    },
                    "display_name": {"type": "string"},
                    "resolving_comment": {"type": "string"},
                    "custom_tags": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["incident_id"],
            },
            source=src,
            priority=0,
        ),
        ToolDefinition(
            name="add_defender_incident_comment",
            description="Add an investigation comment to a Defender XDR incident.",
            input_schema={
                "type": "object",
                "properties": {
                    "incident_id": {"type": "string"},
                    "comment": {"type": "string"},
                },
                "required": ["incident_id", "comment"],
            },
            source=src,
            priority=0,
        ),
        # ── 1. Alerts ─────────────────────────────────────────────────────
        ToolDefinition(
            name="list_defender_alerts",
            description=(
                "List Defender XDR alerts (alerts_v2) across all Defender "
                "products (Endpoint, Office 365, Identity, Cloud Apps, Entra ID "
                "Protection). Filter by status (new, inProgress, resolved), "
                "severity, and service source."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "status": {
                        "type": "string",
                        "enum": ["new", "inProgress", "resolved"],
                    },
                    "severity": {"type": "string", "enum": sev_enum},
                    "service_source": {
                        "type": "string",
                        "description": (
                            "e.g. microsoftDefenderForEndpoint, "
                            "microsoftDefenderForOffice365, "
                            "microsoftDefenderForIdentity, "
                            "microsoftDefenderForCloudApps"
                        ),
                    },
                    "count": {"type": "integer", "default": 25},
                },
                "required": [],
            },
            source=src,
            priority=0,
        ),
        ToolDefinition(
            name="get_defender_alert",
            description=(
                "Get full details of a Defender XDR alert including MITRE "
                "techniques, recommended actions, and evidence (files, IPs, URLs, "
                "accounts, devices) — essential for IOC extraction."
            ),
            input_schema={
                "type": "object",
                "properties": {"alert_id": {"type": "string"}},
                "required": ["alert_id"],
            },
            source=src,
            priority=0,
        ),
        ToolDefinition(
            name="update_defender_alert",
            description=(
                "Update a Defender XDR alert: status (new, inProgress, resolved), "
                "classification, determination, or owner."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "alert_id": {"type": "string"},
                    "status": {
                        "type": "string",
                        "enum": ["new", "inProgress", "resolved"],
                    },
                    "classification": {
                        "type": "string",
                        "enum": classification_enum,
                    },
                    "determination": {"type": "string", "enum": determination_enum},
                    "assigned_to": {"type": "string"},
                },
                "required": ["alert_id"],
            },
            source=src,
            priority=1,
        ),
        ToolDefinition(
            name="add_defender_alert_comment",
            description="Add a comment to a Defender XDR alert.",
            input_schema={
                "type": "object",
                "properties": {
                    "alert_id": {"type": "string"},
                    "comment": {"type": "string"},
                },
                "required": ["alert_id", "comment"],
            },
            source=src,
            priority=1,
        ),
        # ── 2. Advanced Hunting ───────────────────────────────────────────
        ToolDefinition(
            name="run_defender_hunting_query",
            description=(
                "Run a KQL advanced hunting query across Microsoft Defender XDR "
                "tables (up to 30 days of raw data). Common tables: "
                "DeviceProcessEvents, DeviceNetworkEvents, DeviceFileEvents, "
                "DeviceLogonEvents, DeviceRegistryEvents, DeviceImageLoadEvents, "
                "EmailEvents, EmailAttachmentInfo, EmailUrlInfo, "
                "IdentityLogonEvents, IdentityDirectoryEvents, "
                "CloudAppEvents, AlertInfo, AlertEvidence, DeviceInfo, "
                "DeviceTvmSoftwareVulnerabilities."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "KQL query to run"},
                    "timespan": {
                        "type": "string",
                        "default": "P30D",
                        "description": "ISO 8601 duration, max 30 days (PT1H, P1D, P30D)",
                    },
                },
                "required": ["query"],
            },
            source=src,
            priority=0,
            # "run" reads as a write to the name classifier; KQL hunting has no mutating command.
            read_only=True,
        ),
        # ── 3. Threat Intelligence (Defender TI) ──────────────────────────
        ToolDefinition(
            name="list_defender_ti_articles",
            description=(
                "List Microsoft Defender Threat Intelligence articles (finished "
                "intel on campaigns, threats, and actors). Requires a Defender "
                "Threat Intelligence license."
            ),
            input_schema={
                "type": "object",
                "properties": {"count": {"type": "integer", "default": 15}},
                "required": [],
            },
            source=src,
            priority=1,
        ),
        ToolDefinition(
            name="get_defender_ti_article",
            description="Get the full body of a Defender Threat Intelligence article.",
            input_schema={
                "type": "object",
                "properties": {"article_id": {"type": "string"}},
                "required": ["article_id"],
            },
            source=src,
            priority=1,
        ),
        ToolDefinition(
            name="list_defender_intel_profiles",
            description=(
                "List Defender TI intel profiles (threat actors and tools) with "
                "aliases and summaries. Filter by kind ('actor' or 'tool')."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "count": {"type": "integer", "default": 15},
                    "kind": {"type": "string", "enum": ["actor", "tool"]},
                },
                "required": [],
            },
            source=src,
            priority=1,
        ),
        ToolDefinition(
            name="get_defender_intel_profile",
            description=(
                "Get a Defender TI intel profile (threat actor) with targets, "
                "origin, summary, description, and tradecraft."
            ),
            input_schema={
                "type": "object",
                "properties": {"profile_id": {"type": "string"}},
                "required": ["profile_id"],
            },
            source=src,
            priority=1,
        ),
        ToolDefinition(
            name="get_defender_ti_host",
            description=(
                "Enrich a host/domain/IP with Defender TI base data (registrar, "
                "registrant, first/last seen)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "host": {
                        "type": "string",
                        "description": "Hostname, domain, or IP address",
                    }
                },
                "required": ["host"],
            },
            source=src,
            priority=1,
        ),
        ToolDefinition(
            name="get_defender_ti_host_reputation",
            description=(
                "Get the Defender TI reputation verdict (unknown/neutral/"
                "suspicious/malicious), score, and scoring rules for a host/IP/"
                "domain. Ideal for IOC triage."
            ),
            input_schema={
                "type": "object",
                "properties": {"host": {"type": "string"}},
                "required": ["host"],
            },
            source=src,
            priority=1,
        ),
        ToolDefinition(
            name="get_defender_ti_passive_dns",
            description="Get passive DNS resolution history for a host/IP from Defender TI.",
            input_schema={
                "type": "object",
                "properties": {
                    "host": {"type": "string"},
                    "count": {"type": "integer", "default": 25},
                },
                "required": ["host"],
            },
            source=src,
            priority=2,
        ),
        ToolDefinition(
            name="get_defender_ti_vulnerability",
            description=(
                "Get Defender TI intelligence about a vulnerability (CVE): CVSS, "
                "exploit availability, active exploitation, chatter, priority "
                "score, and remediation."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "cve_id": {
                        "type": "string",
                        "description": "CVE identifier, e.g. CVE-2021-44228",
                    }
                },
                "required": ["cve_id"],
            },
            source=src,
            priority=1,
        ),
        # ── 4. Threat Indicators / IOCs ───────────────────────────────────
        ToolDefinition(
            name="list_defender_indicators",
            description="List custom threat indicators (IOCs) configured in Defender for Endpoint.",
            input_schema={
                "type": "object",
                "properties": {"count": {"type": "integer", "default": 50}},
                "required": [],
            },
            source=src,
            priority=1,
        ),
        ToolDefinition(
            name="create_defender_indicator",
            description=(
                "Create a custom threat indicator (IOC) in Defender for Endpoint "
                "to block, alert on, or allow a file hash, IP, domain, URL, or "
                "certificate across all onboarded devices."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "indicator_value": {
                        "type": "string",
                        "description": "The IOC value (hash, IP, domain, URL, thumbprint)",
                    },
                    "indicator_type": {
                        "type": "string",
                        "enum": [
                            "FileSha1",
                            "FileSha256",
                            "FileMd5",
                            "CertificateThumbprint",
                            "IpAddress",
                            "DomainName",
                            "Url",
                        ],
                    },
                    "action": {
                        "type": "string",
                        "enum": [
                            "Alert",
                            "AlertAndBlock",
                            "Block",
                            "BlockAndRemediate",
                            "Audit",
                            "Warn",
                            "Allowed",
                        ],
                    },
                    "title": {"type": "string"},
                    "description": {"type": "string", "default": ""},
                    "severity": {
                        "type": "string",
                        "enum": ["Informational", "Low", "Medium", "High"],
                        "default": "Medium",
                    },
                    "expiration_time": {
                        "type": "string",
                        "description": "ISO 8601 timestamp (optional)",
                    },
                    "recommended_actions": {"type": "string"},
                    "generate_alert": {"type": "boolean", "default": True},
                },
                "required": ["indicator_value", "indicator_type", "action", "title"],
            },
            source=src,
            priority=1,
        ),
        ToolDefinition(
            name="delete_defender_indicator",
            description="Delete a custom threat indicator (IOC) from Defender for Endpoint by ID.",
            input_schema={
                "type": "object",
                "properties": {"indicator_id": {"type": "string"}},
                "required": ["indicator_id"],
            },
            source=src,
            priority=2,
        ),
        # ── 4b. Custom detection rules ───────────────────────────────────
        ToolDefinition(
            name="list_defender_detection_rules",
            description=(
                "List Defender XDR custom detection rules (scheduled KQL detections): "
                "name, id, enabled, period, severity, last run. Needs CustomDetection.Read.All."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "odata_filter": {
                        "type": "string",
                        "description": "Raw OData $filter, e.g. isEnabled eq true",
                    },
                    "count": {"type": "integer", "default": 50},
                },
                "required": [],
            },
            source=src,
            priority=1,
        ),
        ToolDefinition(
            name="get_defender_detection_rule",
            description=(
                "Get one Defender XDR custom detection rule by ID: KQL query, schedule, "
                "alert template, impacted assets, last run."
            ),
            input_schema={
                "type": "object",
                "properties": {"rule_id": {"type": "string"}},
                "required": ["rule_id"],
            },
            source=src,
            priority=1,
        ),
        ToolDefinition(
            name="create_defender_detection_rule",
            description=(
                "Create a Defender XDR custom detection rule that runs a tested KQL "
                "hunting query on a schedule and raises alerts. Needs "
                "CustomDetection.ReadWrite.All. Returns the rule id."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "display_name": {"type": "string"},
                    "query": {
                        "type": "string",
                        "description": "KQL query; it must project Timestamp, ReportId and the asset columns",
                    },
                    "alert_title": {"type": "string"},
                    "period": {
                        "type": "string",
                        "enum": list(_DETECTION_PERIODS),
                        "default": "24H",
                        "description": "Run frequency; 0 is near real time",
                    },
                    "enabled": {"type": "boolean", "default": True},
                    "description": {"type": "string", "default": ""},
                    "severity": {
                        "type": "string",
                        "enum": list(_DETECTION_SEVERITIES),
                        "default": "medium",
                    },
                    "category": {
                        "type": "string",
                        "default": "SuspiciousActivity",
                        "description": "Alert category, e.g. Execution, Persistence, LateralMovement",
                    },
                    "mitre_techniques": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "MITRE ATT&CK technique ids, e.g. T1059.001",
                    },
                    "impacted_assets": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "type": {"type": "string", "enum": list(_IMPACTED_ASSET_TYPES)},
                                "identifier": {
                                    "type": "string",
                                    "description": "Query column holding the entity, e.g. DeviceId",
                                },
                                "field": {
                                    "type": "string",
                                    "enum": _ENTITY_MAPPING_FIELDS,
                                    "description": "Mapping field; default deviceIdColumn, aadUserIdColumn, primaryAddressColumn",
                                },
                            },
                            "required": ["type", "identifier"],
                        },
                        "description": "Entity mapping of the alert",
                    },
                    "recommended_actions": {"type": "string"},
                    "response_actions": {
                        "type": "array",
                        "items": {"type": "object"},
                        "description": "Graph responseAction objects, each with its @odata.type",
                    },
                },
                "required": ["display_name", "query", "alert_title"],
            },
            source=src,
            priority=2,
        ),
        ToolDefinition(
            name="update_defender_detection_rule",
            description=(
                "Change the given fields of a Defender XDR custom detection rule "
                "(name, enabled, query, period, alert fields); the others are kept."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "rule_id": {"type": "string"},
                    "display_name": {"type": "string"},
                    "enabled": {"type": "boolean"},
                    "query": {"type": "string"},
                    "period": {"type": "string", "enum": list(_DETECTION_PERIODS)},
                    "alert_title": {"type": "string"},
                    "description": {"type": "string"},
                    "severity": {"type": "string", "enum": list(_DETECTION_SEVERITIES)},
                    "category": {"type": "string"},
                    "mitre_techniques": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Replaces the stored list whole; [] clears it",
                    },
                    "impacted_assets": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "type": {"type": "string", "enum": list(_IMPACTED_ASSET_TYPES)},
                                "identifier": {"type": "string"},
                                "field": {"type": "string", "enum": _ENTITY_MAPPING_FIELDS},
                            },
                            "required": ["type", "identifier"],
                        },
                    },
                    "recommended_actions": {"type": "string"},
                    "response_actions": {"type": "array", "items": {"type": "object"}},
                },
                "required": ["rule_id"],
            },
            source=src,
            priority=2,
        ),
        ToolDefinition(
            name="delete_defender_detection_rule",
            description="Delete a Defender XDR custom detection rule by ID.",
            input_schema={
                "type": "object",
                "properties": {"rule_id": {"type": "string"}},
                "required": ["rule_id"],
            },
            source=src,
            priority=2,
        ),
        # ── 5. Devices / Machines ─────────────────────────────────────────
        ToolDefinition(
            name="list_defender_machines",
            description=(
                "List onboarded devices in Defender for Endpoint. Filter by health "
                "status, risk score, OS platform, or a raw OData $filter. Returns "
                "device name, OS, health, risk, exposure, last IP, last seen."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "count": {"type": "integer", "default": 25},
                    "health_status": {
                        "type": "string",
                        "enum": [
                            "Active",
                            "Inactive",
                            "ImpairedCommunication",
                            "NoSensorData",
                            "NoSensorDataImpairedCommunication",
                        ],
                    },
                    "risk_score": {
                        "type": "string",
                        "enum": ["None", "Informational", "Low", "Medium", "High"],
                    },
                    "os_platform": {
                        "type": "string",
                        "description": "e.g. Windows11, Windows10, Linux, macOS, Android, iOS",
                    },
                    "odata_filter": {
                        "type": "string",
                        "description": "Raw OData $filter expression (advanced)",
                    },
                },
                "required": [],
            },
            source=src,
            priority=0,
        ),
        ToolDefinition(
            name="get_defender_machine",
            description="Get full details of a Defender for Endpoint device by ID.",
            input_schema={
                "type": "object",
                "properties": {"machine_id": {"type": "string"}},
                "required": ["machine_id"],
            },
            source=src,
            priority=1,
        ),
        ToolDefinition(
            name="list_defender_machine_vulnerabilities",
            description="List vulnerabilities (CVEs) discovered on a specific device.",
            input_schema={
                "type": "object",
                "properties": {"machine_id": {"type": "string"}},
                "required": ["machine_id"],
            },
            source=src,
            priority=1,
        ),
        ToolDefinition(
            name="get_defender_machine_logged_on_users",
            description="List users that logged on to a device (with domain-admin flags).",
            input_schema={
                "type": "object",
                "properties": {"machine_id": {"type": "string"}},
                "required": ["machine_id"],
            },
            source=src,
            priority=1,
        ),
        # ── 6. Response actions ───────────────────────────────────────────
        ToolDefinition(
            name="isolate_defender_machine",
            description=(
                "Isolate a device from the network to contain a threat. Reversible "
                "via unisolate_defender_machine. IsolationType: Full (block all), "
                "Selective (limited apps), or UnManagedDevice."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "machine_id": {"type": "string"},
                    "comment": {"type": "string", "description": "Reason (required)"},
                    "isolation_type": {
                        "type": "string",
                        "enum": ["Full", "Selective", "UnManagedDevice"],
                        "default": "Full",
                    },
                },
                "required": ["machine_id", "comment"],
            },
            source=src,
            priority=1,
        ),
        ToolDefinition(
            name="unisolate_defender_machine",
            description="Release a device from network isolation.",
            input_schema={
                "type": "object",
                "properties": {
                    "machine_id": {"type": "string"},
                    "comment": {"type": "string", "description": "Reason (required)"},
                },
                "required": ["machine_id", "comment"],
            },
            source=src,
            priority=1,
        ),
        ToolDefinition(
            name="run_defender_antivirus_scan",
            description="Run a Microsoft Defender Antivirus scan (Quick or Full) on a device.",
            input_schema={
                "type": "object",
                "properties": {
                    "machine_id": {"type": "string"},
                    "comment": {"type": "string", "description": "Reason (required)"},
                    "scan_type": {
                        "type": "string",
                        "enum": ["Quick", "Full"],
                        "default": "Quick",
                    },
                },
                "required": ["machine_id", "comment"],
            },
            source=src,
            priority=1,
        ),
        ToolDefinition(
            name="collect_defender_investigation_package",
            description=(
                "Collect a forensic investigation package from a device for "
                "deeper analysis. Returns the machine action id: once the action "
                "has succeeded, get_defender_investigation_package hands over the "
                "package."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "machine_id": {"type": "string"},
                    "comment": {"type": "string", "description": "Reason (required)"},
                },
                "required": ["machine_id", "comment"],
            },
            source=src,
            priority=1,
        ),
        ToolDefinition(
            name="restrict_defender_app_execution",
            description=(
                "Restrict application execution on a device — only Microsoft-signed "
                "binaries are allowed to run. Reversible."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "machine_id": {"type": "string"},
                    "comment": {"type": "string", "description": "Reason (required)"},
                },
                "required": ["machine_id", "comment"],
            },
            source=src,
            priority=2,
        ),
        ToolDefinition(
            name="unrestrict_defender_app_execution",
            description="Remove application execution restrictions from a device.",
            input_schema={
                "type": "object",
                "properties": {
                    "machine_id": {"type": "string"},
                    "comment": {"type": "string", "description": "Reason (required)"},
                },
                "required": ["machine_id", "comment"],
            },
            source=src,
            priority=2,
        ),
        ToolDefinition(
            name="stop_and_quarantine_file",
            description=(
                "Stop and quarantine a file (by SHA1) on a specific device. Use to "
                "remediate a confirmed malicious file."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "machine_id": {"type": "string"},
                    "sha1": {"type": "string", "description": "SHA1 of the file"},
                    "comment": {"type": "string", "description": "Reason (required)"},
                },
                "required": ["machine_id", "sha1", "comment"],
            },
            source=src,
            priority=2,
        ),
        ToolDefinition(
            name="run_defender_live_response_script",
            description=(
                "Run a script of the tenant's Live Response library on a device "
                "(each call asks a person). Undo while pending: "
                "cancel_defender_machine_action; output: get_defender_live_response_result."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "machine_id": {"type": "string"},
                    "script_name": {
                        "type": "string",
                        "description": "File name in the Live Response library",
                    },
                    "args": {"type": "string", "description": "Script arguments (optional)"},
                    "comment": {"type": "string", "description": "Reason (required)"},
                },
                "required": ["machine_id", "script_name", "comment"],
            },
            source=src,
            priority=2,
            always_ask=True,
        ),
        ToolDefinition(
            name="list_defender_live_response_library",
            description="List the scripts of the tenant's Live Response library.",
            input_schema={"type": "object", "properties": {}, "required": []},
            source=src,
            priority=2,
        ),
        ToolDefinition(
            name="get_defender_live_response_result",
            description="Read the exit code, output and errors of a finished Live Response script run.",
            input_schema={
                "type": "object",
                "properties": {
                    "action_id": {
                        "type": "string",
                        "description": "Machine action id of the run",
                    }
                },
                "required": ["action_id"],
            },
            source=src,
            priority=2,
        ),
        ToolDefinition(
            name="list_defender_machine_actions",
            description="List recent response/machine actions (isolate, scan, etc.) and their status.",
            input_schema={
                "type": "object",
                "properties": {"count": {"type": "integer", "default": 25}},
                "required": [],
            },
            source=src,
            priority=2,
        ),
        ToolDefinition(
            name="get_defender_machine_action",
            description="Get the status and details of a specific machine action by ID.",
            input_schema={
                "type": "object",
                "properties": {"action_id": {"type": "string"}},
                "required": ["action_id"],
            },
            source=src,
            priority=2,
        ),
        ToolDefinition(
            name="get_defender_investigation_package",
            description=(
                "Get the investigation package a collect_defender_investigation_package "
                "action gathered (a ZIP of the device's forensic data): its time-limited "
                "download link, and the ZIP saved as a file of this conversation "
                "([[FILE:id]] + file_id, forwardable to any tool that takes a file) with "
                "its list of entries - up to the 100 MB download limit, past which only "
                "the link is given. Says so when the collection is still running. "
                "App-only (API key) mode needs the Machine.ReadWrite.All application "
                "permission of WindowsDefenderATP, granted with admin consent; OAuth "
                "mode uses Machine.CollectForensics."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "action_id": {
                        "type": "string",
                        "description": "The machine action id of the package collection",
                    }
                },
                "required": ["action_id"],
            },
            source=src,
            priority=1,
        ),
        ToolDefinition(
            name="cancel_defender_machine_action",
            description="Cancel a pending or in-progress machine action.",
            input_schema={
                "type": "object",
                "properties": {
                    "action_id": {"type": "string"},
                    "comment": {"type": "string", "description": "Reason (required)"},
                },
                "required": ["action_id", "comment"],
            },
            source=src,
            priority=2,
        ),
        # ── 7. Threat & Vulnerability Management ──────────────────────────
        ToolDefinition(
            name="list_defender_vulnerabilities",
            description=(
                "List vulnerabilities (CVEs) known across the organization from "
                "Defender Vulnerability Management. Supports a raw OData $filter "
                "(e.g. \"cvssV3 ge 8\" or \"severity eq 'Critical'\")."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "count": {"type": "integer", "default": 25},
                    "odata_filter": {"type": "string"},
                },
                "required": [],
            },
            source=src,
            priority=1,
        ),
        ToolDefinition(
            name="get_defender_vulnerability",
            description="Get TVM details for a specific vulnerability (CVE).",
            input_schema={
                "type": "object",
                "properties": {"cve_id": {"type": "string"}},
                "required": ["cve_id"],
            },
            source=src,
            priority=1,
        ),
        ToolDefinition(
            name="list_defender_vulnerability_machines",
            description="List devices affected by a specific vulnerability (CVE).",
            input_schema={
                "type": "object",
                "properties": {"cve_id": {"type": "string"}},
                "required": ["cve_id"],
            },
            source=src,
            priority=2,
        ),
        ToolDefinition(
            name="list_defender_software",
            description=(
                "List software inventory across the organization with exposure "
                "metrics (weaknesses, public exploit, active alert, installed "
                "devices). Supports a raw OData $filter."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "count": {"type": "integer", "default": 25},
                    "odata_filter": {"type": "string"},
                },
                "required": [],
            },
            source=src,
            priority=2,
        ),
        ToolDefinition(
            name="list_defender_security_recommendations",
            description=(
                "List security recommendations (improvement actions) from Defender "
                "Vulnerability Management, ranked by severity and exposure. "
                "Supports a raw OData $filter."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "count": {"type": "integer", "default": 25},
                    "odata_filter": {"type": "string"},
                },
                "required": [],
            },
            source=src,
            priority=1,
        ),
        ToolDefinition(
            name="get_defender_recommendation",
            description="Get full details of a security recommendation by ID.",
            input_schema={
                "type": "object",
                "properties": {"recommendation_id": {"type": "string"}},
                "required": ["recommendation_id"],
            },
            source=src,
            priority=2,
        ),
        ToolDefinition(
            name="get_defender_exposure_score",
            description="Get the organization's overall TVM exposure score (lower is better).",
            input_schema={"type": "object", "properties": {}, "required": []},
            source=src,
            priority=2,
        ),
        ToolDefinition(
            name="get_defender_device_secure_score",
            description="Get the Microsoft Secure Score for Devices (configuration hardening score).",
            input_schema={"type": "object", "properties": {}, "required": []},
            source=src,
            priority=2,
        ),
        # ── 8. Entity investigation ───────────────────────────────────────
        ToolDefinition(
            name="get_defender_file_info",
            description=(
                "Get file metadata and reputation by SHA1 or SHA256: type, size, "
                "signer, publisher, prevalence, and Microsoft determination."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "file_hash": {"type": "string", "description": "SHA1 or SHA256"}
                },
                "required": ["file_hash"],
            },
            source=src,
            priority=1,
        ),
        ToolDefinition(
            name="get_defender_file_stats",
            description="Get organization + global prevalence statistics for a file (by SHA1).",
            input_schema={
                "type": "object",
                "properties": {"file_hash": {"type": "string", "description": "SHA1"}},
                "required": ["file_hash"],
            },
            source=src,
            priority=2,
        ),
        ToolDefinition(
            name="get_defender_ip_stats",
            description="Get organization prevalence statistics for an IP address.",
            input_schema={
                "type": "object",
                "properties": {"ip_address": {"type": "string"}},
                "required": ["ip_address"],
            },
            source=src,
            priority=2,
        ),
        ToolDefinition(
            name="get_defender_domain_stats",
            description="Get organization prevalence statistics for a domain.",
            input_schema={
                "type": "object",
                "properties": {"domain": {"type": "string"}},
                "required": ["domain"],
            },
            source=src,
            priority=2,
        ),
        # ── 9. Secure Score ───────────────────────────────────────────────
        ToolDefinition(
            name="get_defender_secure_score",
            description=(
                "Get recent Microsoft Secure Score snapshots for the tenant "
                "(current vs max score, active users)."
            ),
            input_schema={
                "type": "object",
                "properties": {"count": {"type": "integer", "default": 5}},
                "required": [],
            },
            source=src,
            priority=2,
        ),
        ToolDefinition(
            name="list_defender_secure_score_controls",
            description=(
                "List Microsoft Secure Score control profiles (improvement "
                "actions) with category, max score, rank, and user impact."
            ),
            input_schema={
                "type": "object",
                "properties": {"count": {"type": "integer", "default": 25}},
                "required": [],
            },
            source=src,
            priority=2,
        ),
    ]
