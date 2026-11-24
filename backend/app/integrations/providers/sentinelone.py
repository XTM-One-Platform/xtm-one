# SPDX-License-Identifier: Apache-2.0

"""SentinelOne (Singularity) integration – Agentic SOC for endpoint security.

Full-fledged SentinelOne endpoint-security integration covering the
detection → investigation → response → intelligence loop, designed as a
best-in-class building block for an agentic security team.

Tool categories (≈25 tools):
  - Threats: list, get, timeline, notes (list/add), mitigate (kill / quarantine
    / un-quarantine / remediate / rollback-remediation), analyst-verdict,
    incident-status
  - Agents / Endpoints: list, get, installed applications, network isolate
    (disconnect) / reconnect (connect), initiate full disk scan
  - Activities: console activity / audit log
  - Deep Visibility hunting: run a DV query (init → poll → fetch events)
  - Threat Intelligence (IOCs): list, create, delete database indicators
  - Blocklist (restrictions): list, add a SHA1 black-hash
  - Exclusions: list
  - Inventory: sites, groups
  - Application vulnerabilities: installed-application CVEs

Authentication is a **management API token** (``Authorization: ApiToken <token>``)
against the tenant-specific management console URL. The integration stores the
console base URL + API token; the compound ``base_url||api_token`` token is
assembled by ``integration_tools._get_token`` and every call targets
``{base_url}/web/api/v2.1``.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from app.core.egress import egress_client
from app.integrations.http import (
    format_entity_list,
    parse_compound_token,
    safe_request,
)
from app.services.branding import get_cached_branding
from app.tools.base import CallableToolExecutor, ToolDefinition

logger = logging.getLogger(__name__)

_PROVIDER = "SentinelOne"
_API_PREFIX = "/web/api/v2.1"
_DEFAULT_TIMEOUT = 30
_DV_TIMEOUT = 60

# Threat mitigation actions (POST /threats/mitigate/{action}).
_MITIGATION_ACTIONS: frozenset[str] = frozenset(
    {"kill", "quarantine", "un-quarantine", "remediate", "rollback-remediation"}
)
_ANALYST_VERDICTS: frozenset[str] = frozenset(
    {"true_positive", "false_positive", "suspicious", "undefined"}
)
_INCIDENT_STATUSES: frozenset[str] = frozenset(
    {"in_progress", "resolved", "unresolved"}
)

_VERDICT_ICONS: dict[str, str] = {
    "true_positive": "🔴",
    "suspicious": "🟠",
    "false_positive": "🟢",
    "undefined": "⚪",
}


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def _trunc(text: Any, max_len: int = 300) -> str:
    if not text:
        return ""
    text = str(text)
    return text[:max_len] + ("…" if len(text) > max_len else "")


def _dt(value: Any) -> str:
    """Trim an ISO timestamp to second precision for display."""
    return str(value or "")[:19]


def _verdict_icon(verdict: str | None) -> str:
    return _VERDICT_ICONS.get((verdict or "").lower(), "⚪")


def _data(resp: dict | None) -> Any:
    """Return the ``data`` payload from a SentinelOne response envelope."""
    if not isinstance(resp, dict):
        return None
    return resp.get("data")


def _data_list(resp: dict | None) -> list:
    """Return the list payload from a SentinelOne response envelope.

    Most list endpoints return ``data`` as a list; a few (e.g. ``/sites``)
    nest the list one level deeper (``data.sites``).
    """
    d = _data(resp)
    if isinstance(d, list):
        return d
    if isinstance(d, dict):
        for key in ("sites", "groups"):
            if isinstance(d.get(key), list):
                return d[key]
    return []


def _total(resp: dict | None) -> int | None:
    if not isinstance(resp, dict):
        return None
    pagination = resp.get("pagination")
    if isinstance(pagination, dict):
        return pagination.get("totalItems")
    return None


def _envelope_error(resp: dict | None) -> str:
    """Return a human-readable string from a SentinelOne ``errors`` array."""
    if not isinstance(resp, dict):
        return ""
    errors = resp.get("errors") or []
    msgs = []
    for e in errors:
        if isinstance(e, dict):
            msgs.append(str(e.get("detail") or e.get("title") or e.get("code") or e))
    return "; ".join(msgs)[:400]


def _affected(resp: dict | None) -> Any:
    """Return ``data.affected`` from an action response."""
    d = _data(resp)
    if isinstance(d, dict):
        return d.get("affected")
    return None


# ---------------------------------------------------------------------------
# Tool builder
# ---------------------------------------------------------------------------


def build_tools(
    token: str,
    executor: CallableToolExecutor,
    **_kwargs: Any,
) -> list[ToolDefinition]:
    """Build SentinelOne tools.

    ``token`` is the compound ``base_url||api_token`` assembled by
    ``integration_tools._get_token`` from the integration's stored console URL
    and management API token.
    """
    base_url, api_token = parse_compound_token(token, 2)
    base_url = (base_url or "").rstrip("/")
    api_base = f"{base_url}{_API_PREFIX}"
    headers = {
        "Authorization": f"ApiToken {api_token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    async def _api(
        method: str,
        path: str,
        action: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any | None = None,
        timeout: int = _DEFAULT_TIMEOUT,
    ) -> tuple[dict | None, str | None]:
        """Authenticated request helper returning ``(json, error)``."""
        if not base_url:
            return None, (
                "Error: SentinelOne management console URL is not configured. "
                "Edit the integration and set the console base URL."
            )
        if not api_token:
            return None, (
                "Error: SentinelOne API token is not configured. Edit the "
                "integration and set the management API token."
            )
        async with egress_client(timeout=timeout) as client:
            resp, err = await safe_request(
                client,
                method,
                f"{api_base}{path}",
                _PROVIDER,
                action,
                headers=headers,
                params=params,
                json=json,
            )
            if err:
                return None, err
            try:
                return resp.json(), None
            except Exception:
                return {}, None

    # ══════════════════════════════════════════════════════════════════════
    # 0. THREATS
    # ══════════════════════════════════════════════════════════════════════

    async def list_sentinelone_threats(
        query: str = "",
        resolved: str = "",
        mitigation_status: str = "",
        analyst_verdict: str = "",
        incident_status: str = "",
        count: int = 25,
    ) -> str:
        """List SentinelOne threats with optional filters.

        ``mitigation_status``: mitigated, active, blocked, suspicious, pending,
        suspicious_resolved (comma-separated).
        ``analyst_verdict``: true_positive, false_positive, suspicious, undefined.
        ``incident_status``: in_progress, resolved, unresolved.
        """
        params: dict[str, Any] = {
            "limit": min(count, 100),
            "sortBy": "createdAt",
            "sortOrder": "desc",
        }
        if query:
            params["query"] = query
        if resolved:
            params["resolved"] = str(resolved).lower()
        if mitigation_status:
            params["mitigationStatuses"] = mitigation_status
        if analyst_verdict:
            params["analystVerdicts"] = analyst_verdict
        if incident_status:
            params["incidentStatuses"] = incident_status

        resp, err = await _api("get", "/threats", "list threats", params=params)
        if err:
            return err
        threats = _data_list(resp)

        def _fmt(t: dict) -> str:
            ti = t.get("threatInfo") or {}
            ar = t.get("agentRealtimeInfo") or {}
            name = ti.get("threatName") or t.get("threatName") or "Unknown"
            verdict = ti.get("analystVerdict") or t.get("analystVerdict") or "N/A"
            return (
                f"  {_verdict_icon(verdict)} {name} "
                f"[{ti.get('classification') or t.get('classification', 'N/A')}]\n"
                f"    id: {t.get('id', '?')} | "
                f"Agent: {ar.get('agentComputerName') or t.get('agentComputerName', 'N/A')} | "
                f"Mitigation: {ti.get('mitigationStatus') or t.get('mitigationStatus', 'N/A')} | "
                f"Verdict: {verdict} | "
                f"Incident: {ti.get('incidentStatus') or t.get('incidentStatus', 'N/A')}\n"
                f"    Created: {_dt(ti.get('createdAt') or t.get('createdAt'))}"
            )

        return format_entity_list(
            threats, _fmt, noun="threat", total=_total(resp)
        )

    async def get_sentinelone_threat(threat_id: str) -> str:
        """Get full details of a SentinelOne threat by ID."""
        resp, err = await _api(
            "get",
            "/threats",
            f"get threat {threat_id}",
            params={"ids": threat_id},
        )
        if err:
            return err
        threats = _data_list(resp)
        if not threats:
            return f"Threat {threat_id} not found."
        t = threats[0]
        ti = t.get("threatInfo") or {}
        ar = t.get("agentRealtimeInfo") or {}
        ad = t.get("agentDetectionInfo") or {}
        lines = [
            f"Threat: {ti.get('threatName') or t.get('threatName', 'Unknown')}",
            f"ID: {t.get('id', '?')}",
            f"Classification: {ti.get('classification') or t.get('classification', 'N/A')} "
            f"({ti.get('classificationSource', 'N/A')})",
            f"Confidence: {ti.get('confidenceLevel') or t.get('confidenceLevel', 'N/A')} | "
            f"Analyst verdict: {ti.get('analystVerdict') or t.get('analystVerdict', 'N/A')}",
            f"Mitigation status: {ti.get('mitigationStatus') or t.get('mitigationStatus', 'N/A')} | "
            f"Incident status: {ti.get('incidentStatus') or t.get('incidentStatus', 'N/A')}",
            f"Agent: {ar.get('agentComputerName') or t.get('agentComputerName', 'N/A')} "
            f"({ar.get('agentOsType') or t.get('agentOsType', 'N/A')}) | "
            f"Agent ID: {ar.get('agentId') or t.get('agentId', 'N/A')}",
            f"Detection: {ti.get('detectionType', 'N/A')} engine "
            f"{ti.get('engines') or 'N/A'}",
            f"File: {ti.get('threatName', 'N/A')} | "
            f"SHA1: {ti.get('sha1') or t.get('fileSha1', 'N/A')}",
            f"File path: {_trunc(ti.get('filePath') or ad.get('filePath'), 200)}",
            f"Process user: {ad.get('processUser', 'N/A')}",
            f"Created: {_dt(ti.get('createdAt') or t.get('createdAt'))} | "
            f"Identified: {_dt(ti.get('identifiedAt'))}",
        ]
        return "\n".join(lines)

    async def get_sentinelone_threat_timeline(threat_id: str, count: int = 30) -> str:
        """Get the event timeline (forensic activity) of a SentinelOne threat."""
        resp, err = await _api(
            "get",
            f"/threats/{threat_id}/timeline",
            f"get timeline for threat {threat_id}",
            params={"limit": min(count, 100), "sortOrder": "desc"},
        )
        if err:
            return err
        events = _data_list(resp)
        if not events:
            return f"No timeline events found for threat {threat_id}."
        lines = [f"Timeline for threat {threat_id} ({len(events)} events):"]
        for e in events[:count]:
            data = e.get("data") or {}
            lines.append(
                f"  [{_dt(e.get('createdAt'))}] {e.get('activityType', '?')} — "
                f"{_trunc(e.get('primaryDescription') or data.get('description'), 200)}"
            )
        return "\n".join(lines)

    async def list_sentinelone_threat_notes(threat_id: str) -> str:
        """List analyst notes attached to a SentinelOne threat."""
        resp, err = await _api(
            "get",
            f"/threats/{threat_id}/notes",
            f"list notes for threat {threat_id}",
            params={"limit": 100},
        )
        if err:
            return err
        notes = _data_list(resp)
        if not notes:
            return f"No notes found for threat {threat_id}."
        lines = [f"Notes for threat {threat_id} ({len(notes)}):"]
        for n in notes:
            lines.append(
                f"  - [{_dt(n.get('createdAt'))}] "
                f"{n.get('creator') or n.get('creatorId', '?')}: "
                f"{_trunc(n.get('text'), 300)}"
            )
        return "\n".join(lines)

    async def add_sentinelone_threat_note(threat_id: str, text: str) -> str:
        """Add an analyst note to a SentinelOne threat."""
        resp, err = await _api(
            "post",
            "/threats/notes",
            f"add note to threat {threat_id}",
            json={"data": {"text": text}, "filter": {"ids": [threat_id]}},
        )
        if err:
            return err
        env_err = _envelope_error(resp)
        if env_err:
            return f"Error adding note to threat {threat_id}: {env_err}"
        return f"Note added to threat {threat_id}."

    async def mitigate_sentinelone_threat(threat_id: str, action: str) -> str:
        """Apply a mitigation action to a SentinelOne threat.

        ``action`` is one of: kill, quarantine, un-quarantine, remediate,
        rollback-remediation.
        """
        action = (action or "").strip().lower()
        if action not in _MITIGATION_ACTIONS:
            return (
                f"Error: invalid mitigation action '{action}'. Use one of: "
                f"{', '.join(sorted(_MITIGATION_ACTIONS))}."
            )
        resp, err = await _api(
            "post",
            f"/threats/mitigate/{action}",
            f"{action} threat {threat_id}",
            json={"filter": {"ids": [threat_id]}},
            timeout=_DV_TIMEOUT,
        )
        if err:
            return err
        env_err = _envelope_error(resp)
        if env_err:
            return f"Error applying '{action}' to threat {threat_id}: {env_err}"
        affected = _affected(resp)
        return (
            f"Mitigation '{action}' applied to threat {threat_id} "
            f"(affected: {affected if affected is not None else 'N/A'})."
        )

    async def update_sentinelone_threat_verdict(
        threat_id: str, verdict: str
    ) -> str:
        """Set the analyst verdict on a SentinelOne threat.

        ``verdict``: true_positive, false_positive, suspicious, undefined.
        """
        verdict = (verdict or "").strip().lower()
        if verdict not in _ANALYST_VERDICTS:
            return (
                f"Error: invalid analyst verdict '{verdict}'. Use one of: "
                f"{', '.join(sorted(_ANALYST_VERDICTS))}."
            )
        resp, err = await _api(
            "post",
            "/threats/analyst-verdict",
            f"set verdict on threat {threat_id}",
            json={
                "data": {"analystVerdict": verdict},
                "filter": {"ids": [threat_id]},
            },
        )
        if err:
            return err
        env_err = _envelope_error(resp)
        if env_err:
            return f"Error setting verdict on threat {threat_id}: {env_err}"
        return f"Analyst verdict for threat {threat_id} set to '{verdict}'."

    async def update_sentinelone_threat_status(
        threat_id: str, status: str
    ) -> str:
        """Set the incident status on a SentinelOne threat.

        ``status``: in_progress, resolved, unresolved.
        """
        status = (status or "").strip().lower()
        if status not in _INCIDENT_STATUSES:
            return (
                f"Error: invalid incident status '{status}'. Use one of: "
                f"{', '.join(sorted(_INCIDENT_STATUSES))}."
            )
        resp, err = await _api(
            "post",
            "/threats/incident",
            f"set incident status on threat {threat_id}",
            json={
                "data": {"incidentStatus": status},
                "filter": {"ids": [threat_id]},
            },
        )
        if err:
            return err
        env_err = _envelope_error(resp)
        if env_err:
            return f"Error setting incident status on threat {threat_id}: {env_err}"
        return f"Incident status for threat {threat_id} set to '{status}'."

    # ══════════════════════════════════════════════════════════════════════
    # 1. AGENTS / ENDPOINTS
    # ══════════════════════════════════════════════════════════════════════

    async def list_sentinelone_agents(
        query: str = "",
        computer_name: str = "",
        infected: str = "",
        count: int = 25,
    ) -> str:
        """List SentinelOne agents (endpoints), optionally filtered."""
        params: dict[str, Any] = {"limit": min(count, 100)}
        if query:
            params["query"] = query
        if computer_name:
            params["computerName__contains"] = computer_name
        if infected:
            params["infected"] = str(infected).lower()

        resp, err = await _api("get", "/agents", "list agents", params=params)
        if err:
            return err
        agents = _data_list(resp)

        def _fmt(a: dict) -> str:
            net = a.get("networkStatus", "N/A")
            return (
                f"  {a.get('computerName', '?')} "
                f"[{a.get('osType', '?')} {a.get('osName', '')}]\n"
                f"    id: {a.get('id', '?')} | "
                f"Agent: {a.get('agentVersion', 'N/A')} | "
                f"Active: {a.get('isActive', '?')} | Infected: {a.get('infected', '?')} | "
                f"Network: {net} | "
                f"IP: {a.get('lastIpToMgmt', 'N/A')} | "
                f"Last active: {_dt(a.get('lastActiveDate'))}"
            )

        return format_entity_list(agents, _fmt, noun="agent", total=_total(resp))

    async def get_sentinelone_agent(agent_id: str) -> str:
        """Get full details of a SentinelOne agent (endpoint) by ID."""
        resp, err = await _api(
            "get", "/agents", f"get agent {agent_id}", params={"ids": agent_id}
        )
        if err:
            return err
        agents = _data_list(resp)
        if not agents:
            return f"Agent {agent_id} not found."
        a = agents[0]
        lines = [
            f"Agent: {a.get('computerName', '?')}",
            f"ID: {a.get('id', '?')} | UUID: {a.get('uuid', 'N/A')}",
            f"OS: {a.get('osType', '?')} {a.get('osName', '')} ({a.get('osArch', 'N/A')})",
            f"Agent version: {a.get('agentVersion', 'N/A')} | "
            f"Active: {a.get('isActive', '?')} | Up to date: {a.get('isUpToDate', '?')}",
            f"Infected: {a.get('infected', '?')} | "
            f"Active threats: {a.get('activeThreats', 0)} | "
            f"Network status: {a.get('networkStatus', 'N/A')}",
            f"IP (mgmt): {a.get('lastIpToMgmt', 'N/A')} | "
            f"External IP: {a.get('externalIp', 'N/A')}",
            f"Domain: {a.get('domain', 'N/A')} | "
            f"Last logged-in user: {a.get('lastLoggedInUserName', 'N/A')}",
            f"Site: {a.get('siteName', 'N/A')} | Group: {a.get('groupName', 'N/A')}",
            f"Scan status: {a.get('scanStatus', 'N/A')} | "
            f"Registered: {_dt(a.get('registeredAt'))} | "
            f"Last active: {_dt(a.get('lastActiveDate'))}",
        ]
        return "\n".join(lines)

    async def list_sentinelone_agent_applications(agent_id: str, count: int = 50) -> str:
        """List installed applications (software inventory) for an agent."""
        resp, err = await _api(
            "get",
            "/agents/applications",
            f"list applications for agent {agent_id}",
            params={"ids": agent_id, "limit": min(count, 100)},
        )
        if err:
            return err
        apps = _data_list(resp)
        if not apps:
            return f"No applications found for agent {agent_id}."
        lines = [f"Applications on agent {agent_id} ({len(apps)}):"]
        for app in apps[:count]:
            lines.append(
                f"  - {app.get('name', '?')} {app.get('version', '')} "
                f"({app.get('publisher', 'N/A')}) | "
                f"Installed: {_dt(app.get('installedDate'))}"
            )
        return "\n".join(lines)

    async def _agent_action(agent_id: str, action_path: str, verb: str) -> str:
        resp, err = await _api(
            "post",
            f"/agents/actions/{action_path}",
            f"{verb} agent {agent_id}",
            json={"filter": {"ids": [agent_id]}},
            timeout=_DV_TIMEOUT,
        )
        if err:
            return err
        env_err = _envelope_error(resp)
        if env_err:
            return f"Error: failed to {verb} agent {agent_id}: {env_err}"
        affected = _affected(resp)
        return (
            f"Agent {agent_id}: {verb} request accepted "
            f"(affected: {affected if affected is not None else 'N/A'})."
        )

    async def isolate_sentinelone_agent(agent_id: str) -> str:
        """Network-isolate (disconnect) an agent from the network."""
        return await _agent_action(agent_id, "disconnect", "network-isolate")

    async def reconnect_sentinelone_agent(agent_id: str) -> str:
        """Reconnect a network-isolated agent to the network."""
        return await _agent_action(agent_id, "connect", "reconnect")

    async def scan_sentinelone_agent(agent_id: str) -> str:
        """Initiate a full disk scan on an agent."""
        return await _agent_action(agent_id, "initiate-scan", "scan")

    # ══════════════════════════════════════════════════════════════════════
    # 2. ACTIVITIES
    # ══════════════════════════════════════════════════════════════════════

    async def list_sentinelone_activities(
        query: str = "", count: int = 25
    ) -> str:
        """List console activities (audit log) with an optional free-text query."""
        params: dict[str, Any] = {
            "limit": min(count, 100),
            "sortBy": "createdAt",
            "sortOrder": "desc",
        }
        if query:
            params["query"] = query
        resp, err = await _api(
            "get", "/activities", "list activities", params=params
        )
        if err:
            return err
        activities = _data_list(resp)

        def _fmt(a: dict) -> str:
            return (
                f"  [{_dt(a.get('createdAt'))}] {a.get('activityType', '?')} — "
                f"{_trunc(a.get('primaryDescription'), 220)}"
            )

        return format_entity_list(
            activities, _fmt, noun="activity", total=_total(resp)
        )

    # ══════════════════════════════════════════════════════════════════════
    # 3. DEEP VISIBILITY HUNTING
    # ══════════════════════════════════════════════════════════════════════

    async def run_sentinelone_dv_query(
        query: str,
        from_date: str = "",
        to_date: str = "",
        count: int = 50,
    ) -> str:
        """Run a SentinelOne Deep Visibility hunting query.

        Initiates the query, polls until it finishes, then returns matching
        events. ``query`` is Deep Visibility query syntax (e.g.
        ``ProcessName containscis "powershell"``). ``from_date`` / ``to_date``
        are ISO-8601 UTC timestamps; when omitted the last 24 hours is used.
        """
        if not query or not query.strip():
            return "Error: a Deep Visibility query string is required."
        now = datetime.now(UTC)
        from_date = from_date or (now - timedelta(days=1)).strftime(
            "%Y-%m-%dT%H:%M:%S.000000Z"
        )
        to_date = to_date or now.strftime("%Y-%m-%dT%H:%M:%S.000000Z")

        init, err = await _api(
            "post",
            "/dv/init-query",
            "initiate Deep Visibility query",
            json={
                "query": query,
                "fromDate": from_date,
                "toDate": to_date,
                "queryType": ["events"],
                "limit": min(count, 1000),
            },
            timeout=_DV_TIMEOUT,
        )
        if err:
            return err
        data = _data(init)
        query_id = data.get("queryId") if isinstance(data, dict) else None
        if not query_id:
            return (
                "Error: Deep Visibility query did not return a query ID "
                f"({_envelope_error(init) or 'unknown error'})."
            )

        # Poll for completion (status endpoint rate limit is ~1 call/sec).
        status = ""
        for _attempt in range(20):
            await asyncio.sleep(2)
            st, sterr = await _api(
                "get",
                "/dv/query-status",
                "check Deep Visibility query status",
                params={"queryId": query_id},
            )
            if sterr:
                return sterr
            sd = _data(st)
            status = (sd.get("responseState") or sd.get("state") or "") if isinstance(
                sd, dict
            ) else ""
            if status in ("FINISHED", "QUERY_FINISHED"):
                break
            if status in ("FAILED", "QUERY_CANCELED", "CANCELED", "ERROR"):
                return f"Deep Visibility query {query_id} ended with status {status}."
        else:
            return (
                f"Deep Visibility query {query_id} is still running "
                f"(status: {status or 'unknown'}). Try again shortly."
            )

        events_resp, eerr = await _api(
            "get",
            "/dv/events",
            "fetch Deep Visibility events",
            params={"queryId": query_id, "limit": min(count, 1000)},
        )
        if eerr:
            return eerr
        events = _data_list(events_resp)
        if not events:
            return f"Deep Visibility query {query_id} returned no events."

        lines = [f"Deep Visibility results ({len(events)} events):"]
        for e in events[:count]:
            endpoint = e.get("agentName") or e.get("endpointName") or "N/A"
            proc = e.get("processName") or e.get("srcProcName") or e.get("objectType")
            lines.append(
                f"  [{_dt(e.get('eventTime') or e.get('createdAt'))}] "
                f"{e.get('eventType', '?')} on {endpoint} — "
                f"{_trunc(proc, 160)}"
            )
        return "\n".join(lines)

    # ══════════════════════════════════════════════════════════════════════
    # 4. THREAT INTELLIGENCE (IOCs)
    # ══════════════════════════════════════════════════════════════════════

    async def list_sentinelone_iocs(
        query: str = "",
        ioc_type: str = "",
        count: int = 25,
    ) -> str:
        """List Threat Intelligence IOCs in the SentinelOne database."""
        params: dict[str, Any] = {"limit": min(count, 100)}
        if query:
            params["query"] = query
        if ioc_type:
            params["type"] = ioc_type.upper()
        resp, err = await _api(
            "get", "/threat-intelligence/iocs", "list IOCs", params=params
        )
        if err:
            return err
        iocs = _data_list(resp)

        def _fmt(i: dict) -> str:
            return (
                f"  [{i.get('type', '?')}] {i.get('value', '?')}\n"
                f"    uuid: {i.get('uuid') or i.get('id', '?')} | "
                f"Source: {i.get('source', 'N/A')} | "
                f"Method: {i.get('method', 'N/A')} | "
                f"Valid until: {_dt(i.get('validUntil'))}"
            )

        return format_entity_list(iocs, _fmt, noun="IOC", total=_total(resp))

    async def create_sentinelone_ioc(
        ioc_type: str,
        value: str,
        *,
        source: str = "",
        external_id: str = "",
        description: str = "",
        valid_until: str = "",
        account_ids: str = "",
    ) -> str:
        """Add an IOC to the SentinelOne Threat Intelligence database.

        ``ioc_type``: DNS, IPV4, IPV6, SHA1, SHA256, MD5, URL (upper-cased).
        ``value`` is the indicator value. ``account_ids`` (comma-separated) may
        be required to scope the IOC to specific accounts.
        """
        brand = get_cached_branding().name
        now = datetime.now(UTC)
        indicator: dict[str, Any] = {
            "type": ioc_type.upper(),
            "value": value,
            "method": "EQUALS",
            "source": source or brand,
            "externalId": external_id or f"{brand}-{int(now.timestamp())}",
        }
        if description:
            indicator["description"] = description
        indicator["validUntil"] = valid_until or (
            now + timedelta(days=30)
        ).strftime("%Y-%m-%dT%H:%M:%S.000000Z")

        params: dict[str, Any] = {}
        if account_ids:
            params["accountIds"] = account_ids

        resp, err = await _api(
            "post",
            "/threat-intelligence/iocs",
            "create IOC",
            params=params or None,
            json={"data": [indicator]},
        )
        if err:
            return err
        env_err = _envelope_error(resp)
        if env_err:
            return f"Error creating IOC: {env_err}"
        return f"IOC created: {indicator['type']} {value}."

    async def delete_sentinelone_ioc(ioc_id: str) -> str:
        """Delete a Threat Intelligence IOC by its UUID."""
        resp, err = await _api(
            "delete",
            "/threat-intelligence/iocs",
            f"delete IOC {ioc_id}",
            json={"filter": {"uuids": [ioc_id]}},
        )
        if err:
            return err
        env_err = _envelope_error(resp)
        if env_err:
            return f"Error deleting IOC {ioc_id}: {env_err}"
        return f"IOC {ioc_id} deleted."

    # ══════════════════════════════════════════════════════════════════════
    # 5. BLOCKLIST (restrictions) + EXCLUSIONS
    # ══════════════════════════════════════════════════════════════════════

    async def list_sentinelone_blocklist(
        query: str = "", count: int = 25
    ) -> str:
        """List blocklist items (black-hash restrictions)."""
        params: dict[str, Any] = {"type": "black_hash", "limit": min(count, 100)}
        if query:
            params["query"] = query
        resp, err = await _api(
            "get", "/restrictions", "list blocklist", params=params
        )
        if err:
            return err
        items = _data_list(resp)

        def _fmt(i: dict) -> str:
            return (
                f"  {i.get('value', '?')} [{i.get('osType', 'N/A')}]\n"
                f"    id: {i.get('id', '?')} | Scope: {i.get('scope', 'N/A')} | "
                f"{_trunc(i.get('description'), 120) or 'No description'}"
            )

        return format_entity_list(items, _fmt, noun="blocklist item", total=_total(resp))

    async def add_sentinelone_blocklist_hash(
        sha1: str,
        *,
        os_type: str = "windows",
        description: str = "",
        site_ids: str = "",
    ) -> str:
        """Add a SHA1 hash to the blocklist (black-hash restriction).

        Applies tenant-wide by default; pass ``site_ids`` (comma-separated) to
        restrict the scope to specific sites. ``os_type``: windows, macos, linux.
        """
        data: dict[str, Any] = {
            "type": "black_hash",
            "value": sha1,
            "osType": os_type,
            "description": description or f"Added via {get_cached_branding().name}",
        }
        filt: dict[str, Any] = {"siteIds": site_ids} if site_ids else {"tenant": True}
        resp, err = await _api(
            "post",
            "/restrictions",
            "add blocklist hash",
            json={"data": data, "filter": filt},
        )
        if err:
            return err
        env_err = _envelope_error(resp)
        if env_err:
            return f"Error adding blocklist hash: {env_err}"
        return f"SHA1 {sha1} added to the blocklist ({os_type})."

    async def list_sentinelone_exclusions(
        exclusion_type: str = "",
        query: str = "",
        count: int = 25,
    ) -> str:
        """List exclusions (allowlist). Optionally filter by type."""
        params: dict[str, Any] = {"limit": min(count, 100)}
        if exclusion_type:
            params["type"] = exclusion_type
        if query:
            params["query"] = query
        resp, err = await _api(
            "get", "/exclusions", "list exclusions", params=params
        )
        if err:
            return err
        items = _data_list(resp)

        def _fmt(i: dict) -> str:
            return (
                f"  [{i.get('type', '?')}] {_trunc(i.get('value'), 120)}\n"
                f"    id: {i.get('id', '?')} | OS: {i.get('osType', 'N/A')} | "
                f"Mode: {i.get('mode', 'N/A')} | Scope: {i.get('scope', 'N/A')}"
            )

        return format_entity_list(items, _fmt, noun="exclusion", total=_total(resp))

    # ══════════════════════════════════════════════════════════════════════
    # 6. INVENTORY (sites / groups) + APPLICATION VULNERABILITIES
    # ══════════════════════════════════════════════════════════════════════

    async def list_sentinelone_sites(count: int = 25) -> str:
        """List sites in the SentinelOne tenant."""
        resp, err = await _api(
            "get", "/sites", "list sites", params={"limit": min(count, 100)}
        )
        if err:
            return err
        sites = _data_list(resp)

        def _fmt(s: dict) -> str:
            return (
                f"  {s.get('name', '?')} (id: {s.get('id', '?')}) — "
                f"State: {s.get('state', 'N/A')} | "
                f"Agents: {s.get('activeLicenses', s.get('totalLicenses', 'N/A'))} | "
                f"Account: {s.get('accountName', 'N/A')}"
            )

        return format_entity_list(sites, _fmt, noun="site")

    async def list_sentinelone_groups(count: int = 25) -> str:
        """List agent groups in the SentinelOne tenant."""
        resp, err = await _api(
            "get", "/groups", "list groups", params={"limit": min(count, 100)}
        )
        if err:
            return err
        groups = _data_list(resp)

        def _fmt(g: dict) -> str:
            return (
                f"  {g.get('name', '?')} (id: {g.get('id', '?')}) — "
                f"Type: {g.get('type', 'N/A')} | "
                f"Agents: {g.get('totalAgents', 'N/A')} | "
                f"Site: {g.get('siteId', 'N/A')}"
            )

        return format_entity_list(groups, _fmt, noun="group", total=_total(resp))

    async def list_sentinelone_application_cves(
        query: str = "", count: int = 25
    ) -> str:
        """List application vulnerabilities (CVEs) found across installed software."""
        params: dict[str, Any] = {"limit": min(count, 100)}
        if query:
            params["query"] = query
        resp, err = await _api(
            "get",
            "/installed-applications/cves",
            "list application CVEs",
            params=params,
        )
        if err:
            return err
        cves = _data_list(resp)

        def _fmt(c: dict) -> str:
            return (
                f"  {c.get('cveId', c.get('nvdId', '?'))} "
                f"[severity {c.get('severity', 'N/A')}, CVSS {c.get('score', 'N/A')}]\n"
                f"    App: {c.get('applicationName', 'N/A')} "
                f"{c.get('applicationVersion', '')} | "
                f"Endpoint: {c.get('endpointName', c.get('agentComputerName', 'N/A'))}"
            )

        return format_entity_list(
            cves, _fmt, noun="application CVE", total=_total(resp)
        )

    # ══════════════════════════════════════════════════════════════════════
    # REGISTER ALL TOOLS
    # ══════════════════════════════════════════════════════════════════════

    executor.register("list_sentinelone_threats", list_sentinelone_threats)
    executor.register("get_sentinelone_threat", get_sentinelone_threat)
    executor.register(
        "get_sentinelone_threat_timeline", get_sentinelone_threat_timeline
    )
    executor.register("list_sentinelone_threat_notes", list_sentinelone_threat_notes)
    executor.register("add_sentinelone_threat_note", add_sentinelone_threat_note)
    executor.register("mitigate_sentinelone_threat", mitigate_sentinelone_threat)
    executor.register(
        "update_sentinelone_threat_verdict", update_sentinelone_threat_verdict
    )
    executor.register(
        "update_sentinelone_threat_status", update_sentinelone_threat_status
    )
    executor.register("list_sentinelone_agents", list_sentinelone_agents)
    executor.register("get_sentinelone_agent", get_sentinelone_agent)
    executor.register(
        "list_sentinelone_agent_applications", list_sentinelone_agent_applications
    )
    executor.register("isolate_sentinelone_agent", isolate_sentinelone_agent)
    executor.register("reconnect_sentinelone_agent", reconnect_sentinelone_agent)
    executor.register("scan_sentinelone_agent", scan_sentinelone_agent)
    executor.register("list_sentinelone_activities", list_sentinelone_activities)
    executor.register("run_sentinelone_dv_query", run_sentinelone_dv_query)
    executor.register("list_sentinelone_iocs", list_sentinelone_iocs)
    executor.register("create_sentinelone_ioc", create_sentinelone_ioc)
    executor.register("delete_sentinelone_ioc", delete_sentinelone_ioc)
    executor.register("list_sentinelone_blocklist", list_sentinelone_blocklist)
    executor.register(
        "add_sentinelone_blocklist_hash", add_sentinelone_blocklist_hash
    )
    executor.register("list_sentinelone_exclusions", list_sentinelone_exclusions)
    executor.register("list_sentinelone_sites", list_sentinelone_sites)
    executor.register("list_sentinelone_groups", list_sentinelone_groups)
    executor.register(
        "list_sentinelone_application_cves", list_sentinelone_application_cves
    )

    src = "integration:sentinelone"
    return [
        # ── 0. Threats ────────────────────────────────────────────────────
        ToolDefinition(
            name="list_sentinelone_threats",
            description=(
                "List SentinelOne threats. Filter by free-text query, resolved "
                "(true/false), mitigation_status (mitigated, active, blocked, "
                "pending, …), analyst_verdict (true_positive, false_positive, "
                "suspicious, undefined), or incident_status (in_progress, "
                "resolved, unresolved)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "resolved": {"type": "string", "description": "true or false"},
                    "mitigation_status": {"type": "string"},
                    "analyst_verdict": {"type": "string"},
                    "incident_status": {"type": "string"},
                    "count": {"type": "integer", "default": 25},
                },
                "required": [],
            },
            source=src,
            priority=1,
        ),
        ToolDefinition(
            name="get_sentinelone_threat",
            description=(
                "Get full details of a SentinelOne threat by ID: classification, "
                "confidence, analyst verdict, mitigation/incident status, agent, "
                "file hash/path, and timestamps."
            ),
            input_schema={
                "type": "object",
                "properties": {"threat_id": {"type": "string"}},
                "required": ["threat_id"],
            },
            source=src,
            priority=1,
        ),
        ToolDefinition(
            name="get_sentinelone_threat_timeline",
            description=(
                "Get the forensic event timeline of a SentinelOne threat "
                "(detection, mitigation, and analysis activity)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "threat_id": {"type": "string"},
                    "count": {"type": "integer", "default": 30},
                },
                "required": ["threat_id"],
            },
            source=src,
            priority=2,
        ),
        ToolDefinition(
            name="list_sentinelone_threat_notes",
            description="List analyst notes attached to a SentinelOne threat.",
            input_schema={
                "type": "object",
                "properties": {"threat_id": {"type": "string"}},
                "required": ["threat_id"],
            },
            source=src,
            priority=2,
        ),
        ToolDefinition(
            name="add_sentinelone_threat_note",
            description="Add an analyst note to a SentinelOne threat.",
            input_schema={
                "type": "object",
                "properties": {
                    "threat_id": {"type": "string"},
                    "text": {"type": "string"},
                },
                "required": ["threat_id", "text"],
            },
            source=src,
            priority=2,
        ),
        ToolDefinition(
            name="mitigate_sentinelone_threat",
            description=(
                "Apply a mitigation action to a SentinelOne threat. action: "
                "kill, quarantine, un-quarantine, remediate, rollback-remediation."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "threat_id": {"type": "string"},
                    "action": {"type": "string"},
                },
                "required": ["threat_id", "action"],
            },
            source=src,
            priority=1,
        ),
        ToolDefinition(
            name="update_sentinelone_threat_verdict",
            description=(
                "Set the analyst verdict on a SentinelOne threat: true_positive, "
                "false_positive, suspicious, or undefined."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "threat_id": {"type": "string"},
                    "verdict": {"type": "string"},
                },
                "required": ["threat_id", "verdict"],
            },
            source=src,
            priority=1,
        ),
        ToolDefinition(
            name="update_sentinelone_threat_status",
            description=(
                "Set the incident status on a SentinelOne threat: in_progress, "
                "resolved, or unresolved."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "threat_id": {"type": "string"},
                    "status": {"type": "string"},
                },
                "required": ["threat_id", "status"],
            },
            source=src,
            priority=1,
        ),
        # ── 1. Agents / Endpoints ─────────────────────────────────────────
        ToolDefinition(
            name="list_sentinelone_agents",
            description=(
                "List SentinelOne agents (endpoints). Filter by free-text query, "
                "computer_name substring, or infected (true/false). Returns OS, "
                "agent version, active/infected state, network status, and IP."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "computer_name": {"type": "string"},
                    "infected": {"type": "string", "description": "true or false"},
                    "count": {"type": "integer", "default": 25},
                },
                "required": [],
            },
            source=src,
            priority=1,
        ),
        ToolDefinition(
            name="get_sentinelone_agent",
            description=(
                "Get full details of a SentinelOne agent (endpoint) by ID: OS, "
                "agent version, active threats, network status, IPs, site/group, "
                "scan status, and timestamps."
            ),
            input_schema={
                "type": "object",
                "properties": {"agent_id": {"type": "string"}},
                "required": ["agent_id"],
            },
            source=src,
            priority=1,
        ),
        ToolDefinition(
            name="list_sentinelone_agent_applications",
            description="List installed applications (software inventory) for an agent.",
            input_schema={
                "type": "object",
                "properties": {
                    "agent_id": {"type": "string"},
                    "count": {"type": "integer", "default": 50},
                },
                "required": ["agent_id"],
            },
            source=src,
            priority=2,
        ),
        ToolDefinition(
            name="isolate_sentinelone_agent",
            description=(
                "Network-isolate (disconnect) an agent from the network to "
                "contain an active threat."
            ),
            input_schema={
                "type": "object",
                "properties": {"agent_id": {"type": "string"}},
                "required": ["agent_id"],
            },
            source=src,
            priority=1,
        ),
        ToolDefinition(
            name="reconnect_sentinelone_agent",
            description="Reconnect a network-isolated agent to the network.",
            input_schema={
                "type": "object",
                "properties": {"agent_id": {"type": "string"}},
                "required": ["agent_id"],
            },
            source=src,
            priority=2,
        ),
        ToolDefinition(
            name="scan_sentinelone_agent",
            description="Initiate a full disk scan on a SentinelOne agent.",
            input_schema={
                "type": "object",
                "properties": {"agent_id": {"type": "string"}},
                "required": ["agent_id"],
            },
            source=src,
            priority=2,
        ),
        # ── 2. Activities ─────────────────────────────────────────────────
        ToolDefinition(
            name="list_sentinelone_activities",
            description=(
                "List console activities (audit log) — administrative actions, "
                "threat events, and agent events. Supports a free-text query."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "count": {"type": "integer", "default": 25},
                },
                "required": [],
            },
            source=src,
            priority=2,
        ),
        # ── 3. Deep Visibility hunting ────────────────────────────────────
        ToolDefinition(
            name="run_sentinelone_dv_query",
            description=(
                "Run a SentinelOne Deep Visibility threat-hunting query. Uses "
                "Deep Visibility query syntax (e.g. "
                "ProcessName containscis \"powershell\"). Optional from_date / "
                "to_date are ISO-8601 UTC; defaults to the last 24 hours. "
                "Initiates the query, waits for completion, and returns events."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "from_date": {"type": "string"},
                    "to_date": {"type": "string"},
                    "count": {"type": "integer", "default": 50},
                },
                "required": ["query"],
            },
            source=src,
            priority=1,
            # "run" reads as a write to the name classifier; a Deep Visibility query only reads events.
            read_only=True,
        ),
        # ── 4. Threat Intelligence IOCs ───────────────────────────────────
        ToolDefinition(
            name="list_sentinelone_iocs",
            description=(
                "List Threat Intelligence IOCs in the SentinelOne database. "
                "Filter by free-text query or type (DNS, IPV4, IPV6, SHA1, "
                "SHA256, MD5, URL)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "ioc_type": {"type": "string"},
                    "count": {"type": "integer", "default": 25},
                },
                "required": [],
            },
            source=src,
            priority=2,
        ),
        ToolDefinition(
            name="create_sentinelone_ioc",
            description=(
                "Add an IOC to the SentinelOne Threat Intelligence database. "
                "ioc_type: DNS, IPV4, IPV6, SHA1, SHA256, MD5, URL. value is the "
                "indicator. account_ids (comma-separated) may be required to "
                "scope the IOC."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "ioc_type": {"type": "string"},
                    "value": {"type": "string"},
                    "source": {"type": "string"},
                    "external_id": {"type": "string"},
                    "description": {"type": "string"},
                    "valid_until": {
                        "type": "string",
                        "description": "ISO-8601 expiry (default 30 days)",
                    },
                    "account_ids": {"type": "string"},
                },
                "required": ["ioc_type", "value"],
            },
            source=src,
            priority=2,
        ),
        ToolDefinition(
            name="delete_sentinelone_ioc",
            description="Delete a Threat Intelligence IOC by its UUID.",
            input_schema={
                "type": "object",
                "properties": {"ioc_id": {"type": "string"}},
                "required": ["ioc_id"],
            },
            source=src,
            priority=3,
        ),
        # ── 5. Blocklist + Exclusions ─────────────────────────────────────
        ToolDefinition(
            name="list_sentinelone_blocklist",
            description=(
                "List blocklist items (black-hash restrictions) that prevent "
                "specified SHA1 hashes from executing."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "count": {"type": "integer", "default": 25},
                },
                "required": [],
            },
            source=src,
            priority=2,
        ),
        ToolDefinition(
            name="add_sentinelone_blocklist_hash",
            description=(
                "Add a SHA1 hash to the blocklist so it is blocked from "
                "executing. Applies tenant-wide unless site_ids (comma-separated) "
                "is provided. os_type: windows, macos, linux."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "sha1": {"type": "string"},
                    "os_type": {"type": "string", "default": "windows"},
                    "description": {"type": "string"},
                    "site_ids": {"type": "string"},
                },
                "required": ["sha1"],
            },
            source=src,
            priority=2,
        ),
        ToolDefinition(
            name="list_sentinelone_exclusions",
            description=(
                "List exclusions (allowlist). Optionally filter by exclusion_type "
                "(white_hash, path, certificate, browser, file_type) or a query."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "exclusion_type": {"type": "string"},
                    "query": {"type": "string"},
                    "count": {"type": "integer", "default": 25},
                },
                "required": [],
            },
            source=src,
            priority=3,
        ),
        # ── 6. Inventory + Application vulnerabilities ─────────────────────
        ToolDefinition(
            name="list_sentinelone_sites",
            description="List sites in the SentinelOne tenant with agent counts and state.",
            input_schema={
                "type": "object",
                "properties": {"count": {"type": "integer", "default": 25}},
                "required": [],
            },
            source=src,
            priority=3,
        ),
        ToolDefinition(
            name="list_sentinelone_groups",
            description="List agent groups in the SentinelOne tenant.",
            input_schema={
                "type": "object",
                "properties": {"count": {"type": "integer", "default": 25}},
                "required": [],
            },
            source=src,
            priority=3,
        ),
        ToolDefinition(
            name="list_sentinelone_application_cves",
            description=(
                "List application vulnerabilities (CVEs) discovered across "
                "installed software (Application Risk / Singularity Vulnerability "
                "Management). Supports a free-text query."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "count": {"type": "integer", "default": 25},
                },
                "required": [],
            },
            source=src,
            priority=2,
        ),
    ]
