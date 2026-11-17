# SPDX-License-Identifier: Apache-2.0

"""Microsoft Sentinel integration – Agentic SOC foundation.

Provides 16 tools across 6 categories:
  - Incidents: list, get, update, comment, list alerts, list entities
  - Threat Intelligence: list indicators, create indicator, query indicators
  - Watchlists: list, get items
  - Hunting: run KQL query
  - Analytics Rules: list rules
  - Automation: list automation rules, run playbook on incident
  - Data Connectors: list connectors

Two authentication modes are supported (resolved in ``integration_tools.py``):

* **App-only client credentials** (service principal) — the default and most
  robust choice for unattended assignments. ``token`` is the compound
  ``tenant_id||client_id||client_secret||subscription_id||resource_group||workspace_name``
  and each audience token is fetched via the client-credentials grant.
* **Delegated OAuth** (user sign-in) — ``build_tools`` receives a ``token_acquirer``
  that redeems the integration's refresh token per audience; ``token`` then carries
  only ``subscription_id||resource_group||workspace_name``.

Both modes target the Azure Resource Manager API
(``https://management.azure.com``) and the Log Analytics API
(``https://api.loganalytics.io``) — Sentinel is not a Microsoft Graph API.
"""

from __future__ import annotations

import json
import logging
import uuid as _uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

import httpx

from app.core.egress import egress_client
from app.core.utils import format_exception_message
from app.integrations.http import (
    app_token_cache_key,
    format_entity_list,
    get_cached_app_token,
    parse_compound_token,
    safe_request,
    set_cached_app_token,
)
from app.services.branding import get_cached_branding
from app.tools.base import CallableToolExecutor, ToolDefinition

logger = logging.getLogger(__name__)


_PROVIDER = "Microsoft Sentinel"
_MGMT_BASE = "https://management.azure.com"
_API_VERSION = "2024-03-01"
_LA_BASE = "https://api.loganalytics.io"

# Delegated-OAuth audience scopes (one resource each). Used when the integration
# authenticates via user sign-in: the orchestrator redeems the refresh token for
# whichever of these a tool needs.
_MGMT_SCOPE = "https://management.azure.com/.default"
_LA_SCOPE = "https://api.loganalytics.io/.default"

_SEVERITY_ICONS: dict[str, str] = {
    "High": "🔴",
    "Medium": "🟠",
    "Low": "🟡",
    "Informational": "🔵",
}

_STATUS_ICONS: dict[str, str] = {
    "New": "🆕",
    "Active": "⚡",
    "Closed": "✅",
}


# ---------------------------------------------------------------------------
# Auth helper – Azure AD client credentials
# ---------------------------------------------------------------------------


async def _get_app_token(
    tenant_id: str, client_id: str, client_secret: str, scope: str, label: str
) -> str | None:
    """Acquire (and cache) an app-only bearer token for *scope*.

    App-only tokens are cached in-process (keyed by a hash of the credentials +
    scope) until shortly before they expire so a multi-tool agentic run does not
    re-mint a token from Azure AD on every call. The delegated-OAuth path
    already caches per-audience tokens in Redis; this gives app-only parity.
    """
    cache_key = app_token_cache_key("sentinel", tenant_id, client_id, client_secret, scope)
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
            logger.warning("%s token request failed: %s", label, resp.text[:300])
    except httpx.HTTPError as exc:
        logger.warning("%s token request error: %s", label, format_exception_message(exc))
    return None


async def _get_mgmt_token(
    tenant_id: str, client_id: str, client_secret: str
) -> str | None:
    """Acquire a bearer token for Azure Management API."""
    return await _get_app_token(
        tenant_id,
        client_id,
        client_secret,
        "https://management.azure.com/.default",
        "Azure AD mgmt",
    )


async def _get_la_token(
    tenant_id: str, client_id: str, client_secret: str
) -> str | None:
    """Acquire a bearer token for Log Analytics API."""
    return await _get_app_token(
        tenant_id,
        client_id,
        client_secret,
        "https://api.loganalytics.io/.default",
        "Log Analytics",
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_AUTH_ERROR = (
    "Error: Failed to authenticate with Azure AD. In OAuth (sign-in) mode, "
    "re-authorize this integration — its refresh token may be missing or "
    "expired. In app-only (API key) mode, check the tenant ID, client ID, and "
    "client secret."
)


def _parse_token(token: str) -> tuple[str, str, str, str, str, str]:
    return parse_compound_token(token, 6)  # type: ignore[return-value]


def _ws_base(sub_id: str, rg: str, ws: str) -> str:
    return (
        f"{_MGMT_BASE}/subscriptions/{sub_id}/resourceGroups/{rg}"
        f"/providers/Microsoft.OperationalInsights/workspaces/{ws}"
        f"/providers/Microsoft.SecurityInsights"
    )


def _headers(access_token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }


def _sev(severity: str) -> str:
    return _SEVERITY_ICONS.get(severity, "⚪")


def _st(status: str) -> str:
    return _STATUS_ICONS.get(status, "❓")


def _trunc(text: str | None, max_len: int = 300) -> str:
    if not text:
        return ""
    return text[:max_len] + ("…" if len(text) > max_len else "")


def _odata_quote(value: str) -> str:
    """Escape a string for use inside a single-quoted OData string literal.

    Per the OData spec, a literal single quote is escaped by doubling it.
    Free-text filter values (indicator source, pattern type, status, …) can
    legitimately contain an apostrophe, which would otherwise terminate the
    ``$filter`` literal early and break the query (or allow filter injection).
    Mirrors the same guard in the Defender provider.
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
    """Build Microsoft Sentinel tools.

    Two authentication modes are supported, transparently to the tools:

    * **Delegated OAuth** (``token_acquirer`` provided): ``token`` carries only the
      workspace coordinates (``subscription_id||resource_group||workspace_name``)
      and each audience token (Azure Resource Manager + Log Analytics) is minted by
      redeeming the integration's refresh token for that resource's ``/.default``
      scope — the orchestrator wires this up for OAuth integrations.
    * **App-only client credentials** (default): ``token`` is the full compound
      ``tenant_id||client_id||client_secret||subscription_id||resource_group||workspace_name``
      and each audience token is fetched via the client-credentials grant.
    """
    if token_acquirer is not None:
        sub_id, rg, ws = parse_compound_token(token, 3)
        if not all([sub_id, rg, ws]):
            return []

        async def _mgmt_token() -> str | None:
            return await token_acquirer(_MGMT_SCOPE)

        async def _la_token() -> str | None:
            return await token_acquirer(_LA_SCOPE)
    else:
        tenant_id, client_id, client_secret, sub_id, rg, ws = _parse_token(token)
        if not all([tenant_id, client_id, client_secret, sub_id, rg, ws]):
            return []

        async def _mgmt_token() -> str | None:
            return await _get_mgmt_token(tenant_id, client_id, client_secret)

        async def _la_token() -> str | None:
            return await _get_la_token(tenant_id, client_id, client_secret)

    base = _ws_base(sub_id, rg, ws)

    # ══════════════════════════════════════════════════════════════════════
    # 0. INCIDENTS
    # ══════════════════════════════════════════════════════════════════════

    async def list_sentinel_incidents(
        status: str = "",
        severity: str = "",
        count: int = 25,
    ) -> str:
        """List Sentinel incidents with optional status and severity filters."""
        access_token = await _mgmt_token()
        if not access_token:
            return _AUTH_ERROR

        params: dict[str, Any] = {"api-version": _API_VERSION}
        filters: list[str] = []
        if status:
            filters.append(f"properties/status eq '{_odata_quote(status)}'")
        if severity:
            filters.append(f"properties/severity eq '{_odata_quote(severity)}'")
        if filters:
            params["$filter"] = " and ".join(filters)
        params["$top"] = min(count, 50)
        params["$orderby"] = "properties/createdTimeUtc desc"

        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{base}/incidents",
                _PROVIDER,
                "list incidents",
                headers=_headers(access_token),
                params=params,
            )
            if err:
                return err
            incidents = resp.json().get("value", [])
            if not incidents:
                return "No incidents found matching the filters."

            def _fmt(inc: dict) -> str:
                p = inc.get("properties", {})
                owner = (p.get("owner") or {}).get("assignedTo") or "Unassigned"
                alerts = (p.get("additionalData") or {}).get("alertsCount", 0)
                return (
                    f"  {_sev(p.get('severity', '?'))} #{p.get('incidentNumber', '?')} "
                    f"[{p.get('severity', '?')}] {_st(p.get('status', '?'))} "
                    f"{p.get('status', '?')} — {p.get('title', 'Untitled')}\n"
                    f"    Owner: {owner} | Alerts: {alerts} | "
                    f"Created: {(p.get('createdTimeUtc') or '')[:19]}"
                )

            return format_entity_list(incidents, _fmt, noun="incident")

    async def get_sentinel_incident(incident_id: str) -> str:
        """Get full details of a specific Sentinel incident."""
        access_token = await _mgmt_token()
        if not access_token:
            return _AUTH_ERROR

        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{base}/incidents/{incident_id}",
                _PROVIDER,
                f"get incident {incident_id}",
                headers=_headers(access_token),
                params={"api-version": _API_VERSION},
            )
            if err:
                return err
            p = resp.json().get("properties", {})
            add = p.get("additionalData", {})
            owner = (p.get("owner") or {}).get("assignedTo") or "Unassigned"
            labels = p.get("labels", [])

            lines = [
                f"Incident #{p.get('incidentNumber', '?')}: {p.get('title', 'Untitled')}",
                f"Severity: {_sev(p.get('severity', '?'))} {p.get('severity', '?')}",
                f"Status: {_st(p.get('status', '?'))} {p.get('status', '?')}",
                f"Classification: {p.get('classification', 'N/A')} — {p.get('classificationReason', 'N/A')}",
                f"Owner: {owner}",
                f"Created: {(p.get('createdTimeUtc') or '')[:19]}",
                f"Last updated: {(p.get('lastModifiedTimeUtc') or '')[:19]}",
                f"First activity: {(p.get('firstActivityTimeUtc') or '')[:19]}",
                f"Last activity: {(p.get('lastActivityTimeUtc') or '')[:19]}",
                f"Alerts: {add.get('alertsCount', 0)} | Bookmarks: {add.get('bookmarksCount', 0)}",
                f"Alert products: {', '.join(add.get('alertProductNames', [])) or 'N/A'}",
                f"Tactics: {', '.join(add.get('tactics', [])) or 'N/A'}",
                f"Techniques: {', '.join(add.get('techniques', [])) or 'N/A'}",
            ]
            if labels:
                lines.append(
                    f"Labels: {', '.join(lb.get('labelName', '') for lb in labels)}"
                )
            lines.append(f"\nDescription:\n{p.get('description') or 'No description'}")
            lines.append(f"\nIncident URL: {p.get('incidentUrl', 'N/A')}")
            return "\n".join(lines)

    async def update_sentinel_incident(
        incident_id: str,
        *,
        status: str = "",
        severity: str = "",
        title: str = "",
        description: str = "",
        classification: str = "",
        classification_reason: str = "",
        classification_comment: str = "",
        owner_upn: str = "",
        labels: list[str] | None = None,
    ) -> str:
        """Update a Sentinel incident. Only provided fields are changed."""
        access_token = await _mgmt_token()
        if not access_token:
            return _AUTH_ERROR

        url = f"{base}/incidents/{incident_id}"
        async with egress_client(timeout=30) as client:
            get_resp, get_err = await safe_request(
                client,
                "get",
                url,
                _PROVIDER,
                f"get incident {incident_id} for update",
                headers=_headers(access_token),
                params={"api-version": _API_VERSION},
            )
            if get_err:
                return get_err

            existing = get_resp.json()
            props = existing.get("properties", {})

            if status:
                props["status"] = status
            if severity:
                props["severity"] = severity
            if title:
                props["title"] = title
            if description:
                props["description"] = description
            if classification:
                props["classification"] = classification
            if classification_reason:
                props["classificationReason"] = classification_reason
            if classification_comment:
                props["classificationComment"] = classification_comment
            if owner_upn:
                props["owner"] = {"userPrincipalName": owner_upn}
            if labels is not None:
                props["labels"] = [{"labelName": lb} for lb in labels]

            existing["properties"] = props

            _, err = await safe_request(
                client,
                "put",
                url,
                _PROVIDER,
                f"update incident {incident_id}",
                headers=_headers(access_token),
                params={"api-version": _API_VERSION},
                json=existing,
            )
            if err:
                return err
            return (
                f"Incident updated — Status: {props.get('status', '?')}, "
                f"Severity: {props.get('severity', '?')}"
            )

    async def add_sentinel_incident_comment(incident_id: str, message: str) -> str:
        """Add a comment to a Sentinel incident."""
        access_token = await _mgmt_token()
        if not access_token:
            return _AUTH_ERROR

        comment_id = str(_uuid.uuid4())
        url = f"{base}/incidents/{incident_id}/comments/{comment_id}"
        async with egress_client(timeout=30) as client:
            _, err = await safe_request(
                client,
                "put",
                url,
                _PROVIDER,
                f"add comment to incident {incident_id}",
                headers=_headers(access_token),
                params={"api-version": _API_VERSION},
                json={"properties": {"message": message}},
            )
            if err:
                return err
            return f"Comment added to incident {incident_id}."

    async def list_sentinel_incident_alerts(incident_id: str) -> str:
        """List all alerts associated with a Sentinel incident."""
        access_token = await _mgmt_token()
        if not access_token:
            return _AUTH_ERROR

        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "post",
                f"{base}/incidents/{incident_id}/alerts",
                _PROVIDER,
                f"list alerts for incident {incident_id}",
                headers=_headers(access_token),
                params={"api-version": _API_VERSION},
            )
            if err:
                return err
            alerts = resp.json().get("value", [])
            if not alerts:
                return f"No alerts found for incident {incident_id}."

            lines = [f"Alerts for incident {incident_id} ({len(alerts)}):"]
            for a in alerts:
                p = a.get("properties", {})
                name = p.get("alertDisplayName") or p.get("displayName", "Unnamed")
                tactics = ", ".join(p.get("tactics", [])) or "N/A"
                lines.append(
                    f"\n  {_sev(p.get('severity', '?'))} [{p.get('severity', '?')}] {name}\n"
                    f"    Product: {p.get('productName', '?')} | Status: {p.get('status', '?')} | "
                    f"Created: {(p.get('timeGenerated') or p.get('startTimeUtc') or '')[:19]}\n"
                    f"    Tactics: {tactics}\n"
                    f"    {_trunc(p.get('description'), 200)}"
                )
            return "\n".join(lines)

    async def list_sentinel_incident_entities(incident_id: str) -> str:
        """List all entities (IPs, accounts, hosts, etc.) related to a Sentinel incident."""
        access_token = await _mgmt_token()
        if not access_token:
            return _AUTH_ERROR

        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "post",
                f"{base}/incidents/{incident_id}/entities",
                _PROVIDER,
                f"list entities for incident {incident_id}",
                headers=_headers(access_token),
                params={"api-version": _API_VERSION},
            )
            if err:
                return err
            entities = resp.json().get("entities", [])
            if not entities:
                return f"No entities found for incident {incident_id}."

            lines = [f"Entities for incident {incident_id} ({len(entities)}):"]
            for ent in entities:
                kind = ent.get("kind", "Unknown")
                p = ent.get("properties", {})
                detail = p.get("friendlyName") or ""
                if kind == "Account":
                    detail = f"{p.get('accountName', '?')}@{p.get('upnSuffix', '?')}"
                elif kind == "Host":
                    detail = p.get("hostName", "?")
                elif kind == "Ip":
                    detail = p.get("address", "?")
                elif kind == "Url":
                    detail = p.get("url", "?")
                elif kind == "FileHash":
                    detail = f"{p.get('algorithm', '?')}: {p.get('hashValue', '?')}"
                elif kind == "File":
                    detail = p.get("fileName", "?")
                elif kind == "Process":
                    detail = f"PID {p.get('processId', '?')} — {_trunc(p.get('commandLine'), 100)}"
                elif kind == "MailMessage":
                    detail = f"{p.get('subject', '?')} from {p.get('sender', '?')}"
                elif kind == "DnsResolution":
                    detail = p.get("domainName", "?")
                lines.append(f"  [{kind}] {detail}")
            return "\n".join(lines)

    # ══════════════════════════════════════════════════════════════════════
    # 1. THREAT INTELLIGENCE
    # ══════════════════════════════════════════════════════════════════════

    async def list_sentinel_threat_indicators(
        count: int = 25,
        indicator_type: str = "",
        source: str = "",
        min_confidence: int = 0,
    ) -> str:
        """List threat intelligence indicators in the workspace."""
        access_token = await _mgmt_token()
        if not access_token:
            return _AUTH_ERROR

        params: dict[str, Any] = {
            "api-version": _API_VERSION,
            "$top": min(count, 50),
        }
        filters: list[str] = []
        if indicator_type:
            filters.append(f"properties/patternType eq '{_odata_quote(indicator_type)}'")
        if source:
            filters.append(f"properties/source eq '{_odata_quote(source)}'")
        if min_confidence > 0:
            filters.append(f"properties/confidence ge {min_confidence}")
        if filters:
            params["$filter"] = " and ".join(filters)

        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{base}/threatIntelligence/main/indicators",
                _PROVIDER,
                "list threat indicators",
                headers=_headers(access_token),
                params=params,
            )
            if err:
                return err
            indicators = resp.json().get("value", [])
            if not indicators:
                return "No threat indicators found."

            def _fmt(ind: dict) -> str:
                p = ind.get("properties", {})
                return (
                    f"  {p.get('displayName', 'Unnamed')} (confidence: {p.get('confidence', '?')}%)\n"
                    f"    Pattern: {p.get('pattern', '?')}\n"
                    f"    Source: {p.get('source', '?')} | "
                    f"Types: {', '.join(p.get('threatTypes', [])) or 'N/A'} | "
                    f"Valid until: {(p.get('validUntil') or '')[:10]}"
                )

            return format_entity_list(indicators, _fmt, noun="indicator")

    async def create_sentinel_threat_indicator(
        display_name: str,
        pattern: str,
        pattern_type: str,
        *,
        description: str = "",
        confidence: int = 50,
        threat_types: list[str] | None = None,
        kill_chain_phases: list[str] | None = None,
        labels: list[str] | None = None,
        valid_from: str = "",
        valid_until: str = "",
    ) -> str:
        """Create a new threat intelligence indicator."""
        access_token = await _mgmt_token()
        if not access_token:
            return _AUTH_ERROR

        # The ``source`` field is permanently stored in Sentinel alongside
        # the indicator and surfaced in the list/query tools, so it must
        # reflect the admin-customised platform name (resolved via
        # :func:`get_cached_branding`) rather than the deployment-mode
        # default.
        payload: dict[str, Any] = {
            "kind": "indicator",
            "properties": {
                "displayName": display_name,
                "pattern": pattern,
                "patternType": pattern_type,
                "source": get_cached_branding().name,
                "confidence": confidence,
            },
        }
        props = payload["properties"]
        if description:
            props["description"] = description
        # ``threatTypes`` and ``validFrom`` are REQUIRED by the Sentinel
        # createIndicator API — without them the request is rejected with a 400.
        # Default them so the tool works for the common "just a name + pattern"
        # case instead of failing unless the caller happens to supply both.
        props["threatTypes"] = threat_types or ["unknown"]
        if kill_chain_phases:
            props["killChainPhases"] = [
                {"killChainName": "mitre-attack", "phaseName": p}
                for p in kill_chain_phases
            ]
        if labels:
            props["labels"] = labels
        props["validFrom"] = valid_from or datetime.now(UTC).isoformat()
        if valid_until:
            props["validUntil"] = valid_until

        async with egress_client(timeout=30) as client:
            _, err = await safe_request(
                client,
                "post",
                f"{base}/threatIntelligence/main/createIndicator",
                _PROVIDER,
                "create threat indicator",
                headers=_headers(access_token),
                params={"api-version": _API_VERSION},
                json=payload,
            )
            if err:
                return err
            return f"Threat indicator created: {display_name} (pattern: {pattern})"

    async def query_sentinel_threat_indicators(
        pattern_types: list[str] | None = None,
        keywords: list[str] | None = None,
        threat_types: list[str] | None = None,
        min_confidence: int = 0,
        count: int = 25,
    ) -> str:
        """Query threat indicators with advanced filters."""
        access_token = await _mgmt_token()
        if not access_token:
            return _AUTH_ERROR

        payload: dict[str, Any] = {"pageSize": min(count, 100)}
        if pattern_types:
            payload["patternTypes"] = pattern_types
        if keywords:
            payload["keywords"] = keywords
        if threat_types:
            payload["threatTypes"] = threat_types
        if min_confidence > 0:
            payload["minConfidence"] = min_confidence

        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "post",
                f"{base}/threatIntelligence/main/queryIndicators",
                _PROVIDER,
                "query threat indicators",
                headers=_headers(access_token),
                params={"api-version": _API_VERSION},
                json=payload,
            )
            if err:
                return err
            indicators = resp.json().get("value", [])
            if not indicators:
                return "No threat indicators matched the query."

            lines = [f"Found {len(indicators)} matching indicators:"]
            for ind in indicators:
                p = ind.get("properties", {})
                lines.append(
                    f"  {p.get('displayName', 'Unnamed')} "
                    f"(confidence: {p.get('confidence', '?')}%) — {p.get('pattern', '?')}\n"
                    f"    Source: {p.get('source', '?')}"
                )
            return "\n".join(lines)

    # ══════════════════════════════════════════════════════════════════════
    # 2. WATCHLISTS
    # ══════════════════════════════════════════════════════════════════════

    async def list_sentinel_watchlists() -> str:
        """List all watchlists in the Sentinel workspace."""
        access_token = await _mgmt_token()
        if not access_token:
            return _AUTH_ERROR

        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{base}/watchlists",
                _PROVIDER,
                "list watchlists",
                headers=_headers(access_token),
                params={"api-version": _API_VERSION},
            )
            if err:
                return err
            watchlists = resp.json().get("value", [])
            if not watchlists:
                return "No watchlists found."

            def _fmt(wl: dict) -> str:
                p = wl.get("properties", {})
                return (
                    f"  {p.get('displayName', 'Unnamed')} "
                    f"(alias: {p.get('watchlistAlias', '?')}) — "
                    f"Provider: {p.get('provider', '?')}"
                )

            return format_entity_list(watchlists, _fmt, noun="watchlist")

    async def get_sentinel_watchlist_items(
        watchlist_alias: str, count: int = 50
    ) -> str:
        """Get items from a specific watchlist by alias."""
        access_token = await _mgmt_token()
        if not access_token:
            return _AUTH_ERROR

        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{base}/watchlists/{watchlist_alias}/watchlistItems",
                _PROVIDER,
                f"get items from watchlist '{watchlist_alias}'",
                headers=_headers(access_token),
                params={"api-version": _API_VERSION},
            )
            if err:
                return err
            items = resp.json().get("value", [])
            if not items:
                return f"No items found in watchlist '{watchlist_alias}'."

            lines = [f"Watchlist '{watchlist_alias}' items ({len(items)}):"]
            for item in items[:count]:
                kv = item.get("properties", {}).get("itemsKeyValue", {})
                lines.append(f"  {json.dumps(kv, default=str)[:300]}")
            return "\n".join(lines)

    # ══════════════════════════════════════════════════════════════════════
    # 3. HUNTING (KQL)
    # ══════════════════════════════════════════════════════════════════════

    async def run_sentinel_kql_query(query: str, timespan: str = "P1D") -> str:
        """Run a KQL query against the Sentinel Log Analytics workspace."""
        mgmt_token = await _mgmt_token()
        if not mgmt_token:
            return _AUTH_ERROR

        ws_url = (
            f"{_MGMT_BASE}/subscriptions/{sub_id}/resourceGroups/{rg}"
            f"/providers/Microsoft.OperationalInsights/workspaces/{ws}"
        )
        async with egress_client(timeout=30) as client:
            ws_resp, ws_err = await safe_request(
                client,
                "get",
                ws_url,
                _PROVIDER,
                "get workspace ID",
                headers=_headers(mgmt_token),
                params={"api-version": "2023-09-01"},
            )
            if ws_err:
                return ws_err
            workspace_id = ws_resp.json().get("properties", {}).get("customerId", "")
            if not workspace_id:
                return "Error: Could not resolve Log Analytics workspace ID."

        la_token = await _la_token()
        if not la_token:
            return "Error: Failed to authenticate with Log Analytics API."

        async with egress_client(timeout=120) as client:
            resp, err = await safe_request(
                client,
                "post",
                f"{_LA_BASE}/v1/workspaces/{workspace_id}/query",
                _PROVIDER,
                "run KQL query",
                headers=_headers(la_token),
                json={"query": query, "timespan": timespan},
            )
            if err:
                return err
            tables = resp.json().get("tables", [])
            if not tables:
                return "Query returned no results."

            table = tables[0]
            columns = [col["name"] for col in table.get("columns", [])]
            rows = table.get("rows", [])

            if not rows:
                return f"Query returned 0 rows. Columns: {', '.join(columns)}"

            lines = [f"Query results ({len(rows)} rows):"]
            lines.append(" | ".join(columns))
            lines.append("-" * min(len(" | ".join(columns)), 120))

            max_rows = 50
            for row in rows[:max_rows]:
                # Strip embedded newlines/tabs so a value (e.g. a process command
                # line or registry path) can't break the pipe-delimited table into
                # misaligned visual rows.
                row_strs = [
                    str(v).replace("\r", " ").replace("\n", " ").replace("\t", " ")[
                        :80
                    ]
                    if v is not None
                    else "null"
                    for v in row
                ]
                lines.append(" | ".join(row_strs))

            if len(rows) > max_rows:
                lines.append(f"\n... [{len(rows) - max_rows} more rows not shown]")
            return "\n".join(lines)

    # ══════════════════════════════════════════════════════════════════════
    # 4. ANALYTICS RULES
    # ══════════════════════════════════════════════════════════════════════

    async def list_sentinel_analytics_rules(count: int = 25) -> str:
        """List analytics (detection) rules in the Sentinel workspace."""
        access_token = await _mgmt_token()
        if not access_token:
            return _AUTH_ERROR

        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{base}/alertRules",
                _PROVIDER,
                "list analytics rules",
                headers=_headers(access_token),
                params={"api-version": _API_VERSION},
            )
            if err:
                return err
            rules = resp.json().get("value", [])
            if not rules:
                return "No analytics rules found."

            lines = [f"Analytics rules ({len(rules)}):"]
            for rule in rules[:count]:
                p = rule.get("properties", {})
                enabled = "✓ Enabled" if p.get("enabled") else "✗ Disabled"
                tactics = ", ".join(p.get("tactics", [])) or "N/A"
                lines.append(
                    f"\n  [{rule.get('kind', '?')}] {p.get('displayName', 'Unnamed')} — {enabled}\n"
                    f"    Severity: {p.get('severity', 'N/A')} | Tactics: {tactics}\n"
                    f"    {_trunc(p.get('description'), 150)}"
                )
            return "\n".join(lines)

    # ══════════════════════════════════════════════════════════════════════
    # 5. AUTOMATION
    # ══════════════════════════════════════════════════════════════════════

    async def list_sentinel_automation_rules() -> str:
        """List automation rules in the Sentinel workspace."""
        access_token = await _mgmt_token()
        if not access_token:
            return _AUTH_ERROR

        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{base}/automationRules",
                _PROVIDER,
                "list automation rules",
                headers=_headers(access_token),
                params={"api-version": _API_VERSION},
            )
            if err:
                return err
            rules = resp.json().get("value", [])
            if not rules:
                return "No automation rules found."

            lines = [f"Automation rules ({len(rules)}):"]
            for rule in rules:
                p = rule.get("properties", {})
                enabled = "✓ Enabled" if p.get("enabled") else "✗ Disabled"
                trigger = (p.get("triggeringLogic") or {}).get("triggersOn", "?")
                actions = p.get("actions", [])
                action_types = (
                    ", ".join(a.get("actionType", "?") for a in actions) or "N/A"
                )
                lines.append(
                    f"  {p.get('displayName', 'Unnamed')} (order: {p.get('order', '?')}) — {enabled}\n"
                    f"    Trigger: {trigger} | Actions: {action_types}"
                )
            return "\n".join(lines)

    async def run_sentinel_playbook(incident_id: str, resource_id: str) -> str:
        """Trigger a playbook (Logic App) on a specific incident."""
        access_token = await _mgmt_token()
        if not access_token:
            return _AUTH_ERROR

        async with egress_client(timeout=60) as client:
            _, err = await safe_request(
                client,
                "post",
                f"{base}/incidents/{incident_id}/runPlaybook",
                _PROVIDER,
                f"run playbook on incident {incident_id}",
                headers=_headers(access_token),
                params={"api-version": _API_VERSION},
                json={"logicAppsResourceId": resource_id},
            )
            if err:
                return err
            return f"Playbook triggered successfully on incident {incident_id}."

    # ══════════════════════════════════════════════════════════════════════
    # 6. DATA CONNECTORS
    # ══════════════════════════════════════════════════════════════════════

    async def list_sentinel_data_connectors() -> str:
        """List configured data connectors in the Sentinel workspace."""
        access_token = await _mgmt_token()
        if not access_token:
            return _AUTH_ERROR

        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{base}/dataConnectors",
                _PROVIDER,
                "list data connectors",
                headers=_headers(access_token),
                params={"api-version": _API_VERSION},
            )
            if err:
                return err
            connectors = resp.json().get("value", [])
            if not connectors:
                return "No data connectors found."

            lines = [f"Data connectors ({len(connectors)}):"]
            for conn in connectors:
                kind = conn.get("kind", "?")
                name = conn.get("name", "?")
                t_id = conn.get("properties", {}).get("tenantId", "")
                lines.append(
                    f"  [{kind}] {name}" + (f" (tenant: {t_id})" if t_id else "")
                )
            return "\n".join(lines)

    # ══════════════════════════════════════════════════════════════════════
    # REGISTER ALL TOOLS
    # ══════════════════════════════════════════════════════════════════════

    executor.register("list_sentinel_incidents", list_sentinel_incidents)
    executor.register("get_sentinel_incident", get_sentinel_incident)
    executor.register("update_sentinel_incident", update_sentinel_incident)
    executor.register("add_sentinel_incident_comment", add_sentinel_incident_comment)
    executor.register("list_sentinel_incident_alerts", list_sentinel_incident_alerts)
    executor.register(
        "list_sentinel_incident_entities", list_sentinel_incident_entities
    )
    executor.register(
        "list_sentinel_threat_indicators", list_sentinel_threat_indicators
    )
    executor.register(
        "create_sentinel_threat_indicator", create_sentinel_threat_indicator
    )
    executor.register(
        "query_sentinel_threat_indicators", query_sentinel_threat_indicators
    )
    executor.register("list_sentinel_watchlists", list_sentinel_watchlists)
    executor.register("get_sentinel_watchlist_items", get_sentinel_watchlist_items)
    executor.register("run_sentinel_kql_query", run_sentinel_kql_query)
    executor.register("list_sentinel_analytics_rules", list_sentinel_analytics_rules)
    executor.register("list_sentinel_automation_rules", list_sentinel_automation_rules)
    executor.register("run_sentinel_playbook", run_sentinel_playbook)
    executor.register("list_sentinel_data_connectors", list_sentinel_data_connectors)

    return [
        # ── 0. Incidents ──────────────────────────────────────────────────
        ToolDefinition(
            name="list_sentinel_incidents",
            description=(
                "List Microsoft Sentinel security incidents. Filter by status "
                "(New, Active, Closed) and severity (High, Medium, Low, Informational). "
                "Returns incident number, severity, status, title, owner, and alert count."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "status": {
                        "type": "string",
                        "enum": ["New", "Active", "Closed"],
                        "description": "Filter by incident status (optional)",
                    },
                    "severity": {
                        "type": "string",
                        "enum": ["High", "Medium", "Low", "Informational"],
                        "description": "Filter by severity (optional)",
                    },
                    "count": {
                        "type": "integer",
                        "description": "Number of incidents to return (max 50)",
                        "default": 25,
                    },
                },
                "required": [],
            },
            source="integration:sentinel",
            priority=0,
        ),
        ToolDefinition(
            name="get_sentinel_incident",
            description=(
                "Get full details of a Sentinel incident including severity, status, "
                "classification, owner, MITRE tactics/techniques, alert products, "
                "description, and timeline."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "incident_id": {
                        "type": "string",
                        "description": "Sentinel incident ID (GUID)",
                    },
                },
                "required": ["incident_id"],
            },
            source="integration:sentinel",
            priority=0,
        ),
        ToolDefinition(
            name="update_sentinel_incident",
            description=(
                "Update a Sentinel incident. Change status (New/Active/Closed), "
                "severity, classification, owner, labels, or description. "
                "Only provided fields are changed."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "incident_id": {
                        "type": "string",
                        "description": "Sentinel incident ID (GUID)",
                    },
                    "status": {"type": "string", "enum": ["New", "Active", "Closed"]},
                    "severity": {
                        "type": "string",
                        "enum": ["High", "Medium", "Low", "Informational"],
                    },
                    "title": {"type": "string", "description": "New title (optional)"},
                    "description": {
                        "type": "string",
                        "description": "New description (optional)",
                    },
                    "classification": {
                        "type": "string",
                        "enum": [
                            "BenignPositive",
                            "FalsePositive",
                            "TruePositive",
                            "Undetermined",
                        ],
                    },
                    "classification_reason": {
                        "type": "string",
                        "enum": [
                            "InaccurateData",
                            "IncorrectAlertLogic",
                            "SuspiciousActivity",
                            "SuspiciousButExpected",
                        ],
                    },
                    "classification_comment": {"type": "string"},
                    "owner_upn": {
                        "type": "string",
                        "description": "User principal name to assign as owner",
                    },
                    "labels": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Replace all labels",
                    },
                },
                "required": ["incident_id"],
            },
            source="integration:sentinel",
            priority=0,
        ),
        ToolDefinition(
            name="add_sentinel_incident_comment",
            description="Add an investigation comment to a Sentinel incident for audit trail and collaboration.",
            input_schema={
                "type": "object",
                "properties": {
                    "incident_id": {
                        "type": "string",
                        "description": "Sentinel incident ID (GUID)",
                    },
                    "message": {"type": "string", "description": "Comment text"},
                },
                "required": ["incident_id", "message"],
            },
            source="integration:sentinel",
            priority=0,
        ),
        ToolDefinition(
            name="list_sentinel_incident_alerts",
            description="List all alerts associated with a Sentinel incident. Shows alert name, severity, product, tactics.",
            input_schema={
                "type": "object",
                "properties": {
                    "incident_id": {
                        "type": "string",
                        "description": "Sentinel incident ID (GUID)",
                    },
                },
                "required": ["incident_id"],
            },
            source="integration:sentinel",
            priority=0,
        ),
        ToolDefinition(
            name="list_sentinel_incident_entities",
            description=(
                "List all entities (IPs, accounts, hosts, URLs, file hashes, processes, "
                "mail messages) related to a Sentinel incident. Essential for IOC extraction."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "incident_id": {
                        "type": "string",
                        "description": "Sentinel incident ID (GUID)",
                    },
                },
                "required": ["incident_id"],
            },
            source="integration:sentinel",
            priority=0,
        ),
        # ── 1. Threat Intelligence ────────────────────────────────────────
        ToolDefinition(
            name="list_sentinel_threat_indicators",
            description="List threat intelligence indicators. Filter by type, source, and confidence.",
            input_schema={
                "type": "object",
                "properties": {
                    "count": {"type": "integer", "default": 25},
                    "indicator_type": {
                        "type": "string",
                        "description": "e.g. ipv4-addr, domain-name, url, file",
                    },
                    "source": {"type": "string"},
                    "min_confidence": {"type": "integer", "default": 0},
                },
                "required": [],
            },
            source="integration:sentinel",
            priority=1,
        ),
        ToolDefinition(
            name="create_sentinel_threat_indicator",
            description="Create a new threat intelligence indicator with STIX pattern.",
            input_schema={
                "type": "object",
                "properties": {
                    "display_name": {"type": "string"},
                    "pattern": {
                        "type": "string",
                        "description": "STIX pattern, e.g. [ipv4-addr:value = '1.2.3.4']",
                    },
                    "pattern_type": {
                        "type": "string",
                        "description": "ipv4-addr, domain-name, url, email-addr, file",
                    },
                    "description": {"type": "string", "default": ""},
                    "confidence": {"type": "integer", "default": 50},
                    "threat_types": {"type": "array", "items": {"type": "string"}},
                    "kill_chain_phases": {"type": "array", "items": {"type": "string"}},
                    "labels": {"type": "array", "items": {"type": "string"}},
                    "valid_from": {"type": "string"},
                    "valid_until": {"type": "string"},
                },
                "required": ["display_name", "pattern", "pattern_type"],
            },
            source="integration:sentinel",
            priority=1,
        ),
        ToolDefinition(
            name="query_sentinel_threat_indicators",
            description="Query threat indicators with advanced filters: pattern types, keywords, threat types, confidence.",
            input_schema={
                "type": "object",
                "properties": {
                    "pattern_types": {"type": "array", "items": {"type": "string"}},
                    "keywords": {"type": "array", "items": {"type": "string"}},
                    "threat_types": {"type": "array", "items": {"type": "string"}},
                    "min_confidence": {"type": "integer", "default": 0},
                    "count": {"type": "integer", "default": 25},
                },
                "required": [],
            },
            source="integration:sentinel",
            priority=1,
        ),
        # ── 2. Watchlists ─────────────────────────────────────────────────
        ToolDefinition(
            name="list_sentinel_watchlists",
            description="List all watchlists in the Sentinel workspace.",
            input_schema={"type": "object", "properties": {}, "required": []},
            source="integration:sentinel",
            priority=2,
        ),
        ToolDefinition(
            name="get_sentinel_watchlist_items",
            description="Get items from a Sentinel watchlist by alias.",
            input_schema={
                "type": "object",
                "properties": {
                    "watchlist_alias": {"type": "string"},
                    "count": {"type": "integer", "default": 50},
                },
                "required": ["watchlist_alias"],
            },
            source="integration:sentinel",
            priority=2,
        ),
        # ── 3. Hunting (KQL) ──────────────────────────────────────────────
        ToolDefinition(
            name="run_sentinel_kql_query",
            description=(
                "Run a KQL query against the Sentinel Log Analytics workspace. "
                "Common tables: SecurityEvent, SecurityAlert, SecurityIncident, "
                "SigninLogs, AuditLogs, CommonSecurityLog, Syslog, "
                "ThreatIntelligenceIndicator, OfficeActivity, AzureActivity."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "KQL query to execute"},
                    "timespan": {
                        "type": "string",
                        "default": "P1D",
                        "description": "ISO 8601 duration (PT1H, P1D, P7D)",
                    },
                },
                "required": ["query"],
            },
            source="integration:sentinel",
            priority=0,
        ),
        # ── 4. Analytics Rules ────────────────────────────────────────────
        ToolDefinition(
            name="list_sentinel_analytics_rules",
            description="List analytics (detection) rules with severity, MITRE tactics, and enabled status.",
            input_schema={
                "type": "object",
                "properties": {"count": {"type": "integer", "default": 25}},
                "required": [],
            },
            source="integration:sentinel",
            priority=2,
        ),
        # ── 5. Automation ─────────────────────────────────────────────────
        ToolDefinition(
            name="list_sentinel_automation_rules",
            description="List automation rules with trigger, actions, and enabled status.",
            input_schema={"type": "object", "properties": {}, "required": []},
            source="integration:sentinel",
            priority=2,
        ),
        ToolDefinition(
            name="run_sentinel_playbook",
            description="Trigger a playbook (Azure Logic App) on a Sentinel incident.",
            input_schema={
                "type": "object",
                "properties": {
                    "incident_id": {
                        "type": "string",
                        "description": "Sentinel incident ID (GUID)",
                    },
                    "resource_id": {
                        "type": "string",
                        "description": "Full Azure resource ID of the Logic App",
                    },
                },
                "required": ["incident_id", "resource_id"],
            },
            source="integration:sentinel",
            priority=1,
        ),
        # ── 6. Data Connectors ────────────────────────────────────────────
        ToolDefinition(
            name="list_sentinel_data_connectors",
            description="List configured data connectors in the Sentinel workspace.",
            input_schema={"type": "object", "properties": {}, "required": []},
            source="integration:sentinel",
            priority=2,
        ),
    ]
