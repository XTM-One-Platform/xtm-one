# SPDX-License-Identifier: Apache-2.0

"""Palo Alto Cortex XSOAR integration – Agentic SOC orchestration & response.

Full-fledged Cortex XSOAR (formerly Demisto) integration covering the SOAR loop —
**gathering incidents**, **creating intelligence**, **threat hunting/enrichment**,
and **orchestration & response** — designed as a best-in-class building block for
an agentic security team.

Tool categories (16 tools):
  - Incidents: list/search (Lucene query + structured filters), get, create
    (optionally auto-running the type's playbook), update, close
  - War Room / orchestration: add an investigation entry (note), read the war
    room, and **run any XSOAR command or automation** (`!command args`) via the
    synchronous execute endpoint — this exposes the *entire* installed
    integration/automation command surface (e.g. `!ip`, `!domain`, `!file`,
    `!url` enrichment) for threat hunting and response
  - Playbooks: list/search (auto-run on incident creation)
  - Threat Intelligence: search indicators, create indicator (*creating
    intelligence*), get one by value, whitelist/delete (exclusion)
  - Content & RBAC: lists, users

Authentication
--------------
Two Cortex XSOAR generations are supported transparently:

* **Cortex XSOAR 8 / Cortex platform** — requires an **API key** (``Authorization``)
  *and* an **API key ID** (``x-xdr-auth-id``). Requests target the public API
  under ``/xsoar/public/v1`` on the tenant API host
  (``https://api-<fqdn>``). Both **Standard** keys (key sent as-is) and
  **Advanced** keys (per-request SHA-256 of ``api_key + nonce + timestamp`` with
  ``x-xdr-nonce`` / ``x-xdr-timestamp``, to defeat replay) are supported.
* **Cortex XSOAR 6 (on-prem)** — a single **API key** in the ``Authorization``
  header against the server root (``https://<server>``).

``integration_tools._get_token`` assembles the compound
``base_url||api_key||api_key_id||auth_mode`` token this builder parses. ``api_key_id``
empty ⇒ XSOAR 6 mode; present ⇒ XSOAR 8 (``standard`` or ``advanced`` ``auth_mode``).
"""

from __future__ import annotations

import hashlib
import json
import logging
import secrets
import string
from datetime import UTC, datetime
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

_PROVIDER = "Cortex XSOAR"
_SOURCE = "integration:xsoar"

# Public API prefix for Cortex XSOAR 8 (the v8 public API mirrors the v6 server
# API under this path). XSOAR 6 uses the server root with no prefix.
XSOAR_PUBLIC_PREFIX = "/xsoar/public/v1"

# Incident severity name <-> Cortex XSOAR numeric level.
_SEVERITY_TO_NUM: dict[str, float] = {
    "unknown": 0,
    "informational": 0.5,
    "info": 0.5,
    "low": 1,
    "medium": 2,
    "high": 3,
    "critical": 4,
}
_NUM_TO_SEVERITY: dict[float, str] = {
    0: "Unknown",
    0.5: "Informational",
    1: "Low",
    2: "Medium",
    3: "High",
    4: "Critical",
}
_SEVERITY_ICONS: dict[float, str] = {
    4: "🔴",
    3: "🔴",
    2: "🟠",
    1: "🟡",
    0.5: "🔵",
    0: "⚪",
}

# Incident status name <-> Cortex XSOAR numeric status.
_STATUS_TO_NUM: dict[str, int] = {
    "pending": 0,
    "active": 1,
    "closed": 2,
    "archived": 3,
}
_NUM_TO_STATUS: dict[int, str] = {0: "Pending", 1: "Active", 2: "Closed", 3: "Archived"}

# Indicator reputation score (verdict).
_SCORE_TO_VERDICT: dict[int, str] = {
    0: "⚪ Unknown",
    1: "🟢 Good",
    2: "🟠 Suspicious",
    3: "🔴 Bad",
}


# ---------------------------------------------------------------------------
# Auth + URL helpers (shared with integration_testing.py)
# ---------------------------------------------------------------------------


def xsoar_api_base(base_url: str, api_key_id: str) -> str:
    """Return the API base URL — adds the public prefix for XSOAR 8."""
    base = base_url.rstrip("/")
    return f"{base}{XSOAR_PUBLIC_PREFIX}" if api_key_id else base


def build_xsoar_headers(
    api_key: str, api_key_id: str, auth_mode: str = "standard"
) -> dict[str, str]:
    """Build request headers for the right Cortex XSOAR generation / key type.

    * XSOAR 6 (no ``api_key_id``): single ``Authorization`` header.
    * XSOAR 8 Standard: ``Authorization`` + ``x-xdr-auth-id``.
    * XSOAR 8 Advanced: a per-request SHA-256 of ``api_key + nonce + timestamp``
      plus ``x-xdr-nonce`` / ``x-xdr-timestamp`` (replay protection). A fresh
      nonce/timestamp is generated on every call, so this must be invoked per
      request rather than once at build time.
    """
    headers: dict[str, str] = {
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    if not api_key_id:
        headers["Authorization"] = api_key
        return headers

    headers["x-xdr-auth-id"] = api_key_id
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


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def _sev_label(level: Any) -> str:
    try:
        return _NUM_TO_SEVERITY.get(float(level), str(level))
    except (TypeError, ValueError):
        return str(level)


def _sev_icon(level: Any) -> str:
    try:
        return _SEVERITY_ICONS.get(float(level), "⚪")
    except (TypeError, ValueError):
        return "⚪"


def _status_label(status: Any) -> str:
    try:
        return _NUM_TO_STATUS.get(int(status), str(status))
    except (TypeError, ValueError):
        return str(status)


def _trunc(text: Any, max_len: int = 300) -> str:
    if text is None:
        return ""
    text = str(text)
    return text[:max_len] + ("…" if len(text) > max_len else "")


def _dt(value: str | None) -> str:
    return (value or "")[:19]


def _lucene_phrase(value: str) -> str:
    """Escape a value for use inside a Lucene double-quoted phrase.

    Backslash and double-quote are the metacharacters that can terminate or
    corrupt a ``field:"…"`` phrase, so they are backslash-escaped (backslash
    first, to avoid double-escaping the quotes we add). Without this an
    indicator value containing a ``"`` or ``\\`` would produce an invalid query
    or silently change its semantics, so the lookup fails for otherwise valid
    values.
    """
    return str(value).replace("\\", "\\\\").replace('"', '\\"')


def _stringify(value: Any, max_len: int = 1500) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return _trunc(value, max_len)
    try:
        return _trunc(json.dumps(value, default=str), max_len)
    except (TypeError, ValueError):
        return _trunc(str(value), max_len)


# ---------------------------------------------------------------------------
# Tool builder
# ---------------------------------------------------------------------------


def build_tools(
    token: str,
    executor: CallableToolExecutor,
    **_kwargs: Any,
) -> list[ToolDefinition]:
    """Build Cortex XSOAR tools from a ``base_url||api_key||api_key_id||auth_mode`` token."""
    base_url, api_key, api_key_id, auth_mode = parse_compound_token(token, 4)
    if not base_url or not api_key:
        return []

    api_base = xsoar_api_base(base_url, api_key_id)

    def _headers() -> dict[str, str]:
        # Rebuilt per call so Advanced keys get a fresh nonce/timestamp.
        return build_xsoar_headers(api_key, api_key_id, auth_mode)

    # ══════════════════════════════════════════════════════════════════════
    # 0. CONNECTION
    # ══════════════════════════════════════════════════════════════════════

    async def get_xsoar_status() -> str:
        """Validate the Cortex XSOAR connection and report accessible incidents."""
        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "post",
                f"{api_base}/incidents/search",
                _PROVIDER,
                "validate connection",
                headers=_headers(),
                json={"filter": {"size": 1}},
            )
            if err:
                return err
            body = resp.json()
            total = body.get("total", "?")
            mode = "XSOAR 8 (Cortex platform)" if api_key_id else "XSOAR 6 (on-prem)"
            return (
                f"Connected to Cortex XSOAR at {base_url} [{mode}].\n"
                f"  Accessible incidents: {total}"
            )

    # ══════════════════════════════════════════════════════════════════════
    # 1. INCIDENTS (gathering incidents)
    # ══════════════════════════════════════════════════════════════════════

    async def list_xsoar_incidents(
        query: str = "",
        status: str = "",
        severity: str = "",
        incident_type: str = "",
        size: int = 25,
        page: int = 0,
    ) -> str:
        """Search Cortex XSOAR incidents. Use `query` (Lucene) OR the structured filters."""
        ifilter: dict[str, Any] = {"size": min(max(size, 1), 100), "page": max(page, 0)}
        if query:
            # Per the API, when `query` is set the other filter fields are ignored.
            ifilter["query"] = query
        else:
            if status and status.lower() in _STATUS_TO_NUM:
                ifilter["status"] = [_STATUS_TO_NUM[status.lower()]]
            if severity and severity.lower() in _SEVERITY_TO_NUM:
                # The incidents/search *filter* keys severity as ``level`` (an
                # array of numeric severities), which is distinct from the
                # incident object's ``severity`` field read elsewhere. This is
                # per the XSOAR API (SearchIncidentsData.level / SearchIncidents
                # "Filter by Severity"), not a typo — do not rename to severity.
                ifilter["level"] = [_SEVERITY_TO_NUM[severity.lower()]]
            if incident_type:
                ifilter["type"] = [incident_type]

        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "post",
                f"{api_base}/incidents/search",
                _PROVIDER,
                "search incidents",
                headers=_headers(),
                json={"filter": ifilter},
            )
            if err:
                return err
            body = resp.json()
            incidents = body.get("data") or []
            total = body.get("total")

            def _fmt(inc: dict) -> str:
                lvl = inc.get("severity", 0)
                return (
                    f"  {_sev_icon(lvl)} #{inc.get('id', '?')} "
                    f"[{_sev_label(lvl)}] {_status_label(inc.get('status'))} — "
                    f"{inc.get('name', 'Untitled')}\n"
                    f"    Type: {inc.get('type', 'N/A')} | "
                    f"Owner: {inc.get('owner') or 'Unassigned'} | "
                    f"Created: {_dt(inc.get('created'))}"
                )

            return format_entity_list(incidents, _fmt, noun="incident", total=total)

    async def get_xsoar_incident(incident_id: str) -> str:
        """Get full details of a Cortex XSOAR incident by id."""
        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{api_base}/incident/load/{incident_id}",
                _PROVIDER,
                f"get incident {incident_id}",
                headers=_headers(),
            )
            if err:
                return err
            inc = resp.json() or {}
            labels = inc.get("labels") or []
            label_str = ", ".join(
                f"{lb.get('type')}={lb.get('value')}" for lb in labels[:15]
            )
            lines = [
                f"Incident #{inc.get('id', incident_id)}: {inc.get('name', 'Untitled')}",
                f"Severity: {_sev_icon(inc.get('severity'))} "
                f"{_sev_label(inc.get('severity'))} | "
                f"Status: {_status_label(inc.get('status'))}",
                f"Type: {inc.get('type', 'N/A')} | Owner: {inc.get('owner') or 'Unassigned'}",
                f"Playbook: {inc.get('playbookId') or 'None'}",
                f"Created: {_dt(inc.get('created'))} | Modified: {_dt(inc.get('modified'))}",
                f"Closed: {_dt(inc.get('closed'))} | "
                f"Close reason: {inc.get('closeReason') or 'N/A'}",
            ]
            if label_str:
                lines.append(f"Labels: {label_str}")
            if inc.get("details"):
                lines.append(f"\nDetails:\n{_trunc(inc.get('details'), 1500)}")
            return "\n".join(lines)

    async def create_xsoar_incident(
        name: str,
        incident_type: str = "Unclassified",
        severity: str = "medium",
        details: str = "",
        labels: dict[str, str] | None = None,
        create_investigation: bool = False,
        playbook_id: str = "",
    ) -> str:
        """Create a Cortex XSOAR incident, optionally auto-running its playbook.

        Set `create_investigation` to true to start the investigation and run the
        incident type's playbook (or `playbook_id` if given) automatically.
        """
        payload: dict[str, Any] = {
            "name": name,
            "type": incident_type,
            "severity": _SEVERITY_TO_NUM.get(severity.lower(), 2),
            "createInvestigation": create_investigation,
        }
        if details:
            payload["details"] = details
        if playbook_id:
            payload["playbookId"] = playbook_id
        if labels:
            payload["labels"] = [{"type": k, "value": v} for k, v in labels.items()]

        async with egress_client(timeout=60) as client:
            resp, err = await safe_request(
                client,
                "post",
                f"{api_base}/incident",
                _PROVIDER,
                "create incident",
                headers=_headers(),
                json=payload,
            )
            if err:
                return err
            inc = resp.json() or {}
            ran = " (investigation started)" if create_investigation else ""
            return (
                f"Incident created (id: {inc.get('id', '?')}): "
                f"{inc.get('name', name)}{ran}"
            )

    async def update_xsoar_incident(
        incident_id: str,
        name: str = "",
        severity: str = "",
        incident_type: str = "",
        owner: str = "",
        details: str = "",
    ) -> str:
        """Update fields on an existing Cortex XSOAR incident. Only provided fields change."""
        async with egress_client(timeout=30) as client:
            load_resp, load_err = await safe_request(
                client,
                "get",
                f"{api_base}/incident/load/{incident_id}",
                _PROVIDER,
                f"load incident {incident_id} for update",
                headers=_headers(),
            )
            if load_err:
                return load_err
            existing = load_resp.json() or {}

            payload: dict[str, Any] = {
                "id": incident_id,
                "version": existing.get("version", 0),
                "name": name or existing.get("name", ""),
                "type": incident_type or existing.get("type", ""),
            }
            if severity and severity.lower() in _SEVERITY_TO_NUM:
                payload["severity"] = _SEVERITY_TO_NUM[severity.lower()]
            if owner:
                payload["owner"] = owner
            if details:
                payload["details"] = details

            _, err = await safe_request(
                client,
                "post",
                f"{api_base}/incident",
                _PROVIDER,
                f"update incident {incident_id}",
                headers=_headers(),
                json=payload,
            )
            if err:
                return err
            return f"Incident {incident_id} updated."

    async def close_xsoar_incident(
        incident_id: str, close_reason: str = "Resolved", close_notes: str = ""
    ) -> str:
        """Close a Cortex XSOAR incident with a reason and optional notes."""
        payload: dict[str, Any] = {
            "id": incident_id,
            "closeReason": close_reason,
        }
        if close_notes:
            payload["closeNotes"] = close_notes
        async with egress_client(timeout=30) as client:
            _, err = await safe_request(
                client,
                "post",
                f"{api_base}/incident/close",
                _PROVIDER,
                f"close incident {incident_id}",
                headers=_headers(),
                json=payload,
            )
            if err:
                return err
            return f"Incident {incident_id} closed ({close_reason})."

    # ══════════════════════════════════════════════════════════════════════
    # 2. WAR ROOM / ORCHESTRATION (response)
    # ══════════════════════════════════════════════════════════════════════

    async def add_xsoar_incident_entry(investigation_id: str, note: str) -> str:
        """Add a note entry to an incident's War Room (investigation)."""
        async with egress_client(timeout=30) as client:
            _, err = await safe_request(
                client,
                "post",
                f"{api_base}/entry",
                _PROVIDER,
                f"add entry to investigation {investigation_id}",
                headers=_headers(),
                json={"investigationId": investigation_id, "data": note},
            )
            if err:
                return err
            return f"Note added to investigation {investigation_id}."

    async def get_xsoar_incident_war_room(investigation_id: str, count: int = 25) -> str:
        """Read recent War Room entries for an incident (investigation)."""
        count = max(count, 1)
        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "post",
                f"{api_base}/investigation/{investigation_id}",
                _PROVIDER,
                f"get war room for investigation {investigation_id}",
                headers=_headers(),
                json={"pageSize": min(count, 50)},
            )
            if err:
                return err
            body = resp.json() or {}
            entries = body.get("entries") or []
            if not entries:
                return f"No War Room entries for investigation {investigation_id}."
            lines = [f"War Room entries for {investigation_id} ({len(entries)}):"]
            for e in entries[:count]:
                user = e.get("user") or "system"
                cat = e.get("category") or e.get("type") or "entry"
                lines.append(
                    f"\n  [{_dt(e.get('created'))}] {user} ({cat}):\n"
                    f"  {_stringify(e.get('contents'), 600)}"
                )
            return "\n".join(lines)

    async def run_xsoar_command(command: str, investigation_id: str = "") -> str:
        """Run any Cortex XSOAR command or automation and return the result.

        `command` is War Room syntax, e.g. `!ip ip=1.2.3.4`, `!domain domain=evil.com`,
        `!file file=SHA256`, `!url url=...`, or any installed automation. When
        `investigation_id` is omitted the command runs in the playground. This
        exposes the full installed integration/automation command surface for
        enrichment, hunting, and response.
        """
        payload: dict[str, Any] = {"data": command}
        if investigation_id:
            payload["investigationId"] = investigation_id
        async with egress_client(timeout=120) as client:
            resp, err = await safe_request(
                client,
                "post",
                f"{api_base}/entry/execute/sync",
                _PROVIDER,
                f"run command '{command[:60]}'",
                headers=_headers(),
                json=payload,
            )
            if err:
                return err
            entries = resp.json()
            if not isinstance(entries, list):
                entries = [entries] if entries else []
            if not entries:
                return f"Command executed: {command} (no output returned)."
            lines = [f"Result of `{command}` ({len(entries)} entr(y/ies)):"]
            for e in entries[:10]:
                if isinstance(e, dict):
                    lines.append(f"\n{_stringify(e.get('contents', e), 1800)}")
                else:
                    lines.append(f"\n{_stringify(e, 1800)}")
            return "\n".join(lines)

    # ══════════════════════════════════════════════════════════════════════
    # 3. PLAYBOOKS
    # ══════════════════════════════════════════════════════════════════════

    async def list_xsoar_playbooks(query: str = "", size: int = 30) -> str:
        """List/search Cortex XSOAR playbooks (run via create_xsoar_incident)."""
        size = max(size, 1)
        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "post",
                f"{api_base}/playbook/search",
                _PROVIDER,
                "search playbooks",
                headers=_headers(),
                json={"query": query, "size": min(size, 100)},
            )
            if err:
                return err
            body = resp.json() or {}
            playbooks = body.get("playbooks") or body.get("data") or []

            def _fmt(p: dict) -> str:
                return (
                    f"  {p.get('name', 'Unnamed')} (id: {p.get('id', '?')})\n"
                    f"    {_trunc(p.get('description'), 150) or 'No description'}"
                )

            return format_entity_list(playbooks[:size], _fmt, noun="playbook")

    # ══════════════════════════════════════════════════════════════════════
    # 4. THREAT INTELLIGENCE — Indicators (creating intelligence)
    # ══════════════════════════════════════════════════════════════════════

    async def list_xsoar_indicators(query: str = "", size: int = 25) -> str:
        """Search Cortex XSOAR threat-intel indicators (Lucene `query`)."""
        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "post",
                f"{api_base}/indicators/search",
                _PROVIDER,
                "search indicators",
                headers=_headers(),
                json={"query": query, "size": min(max(size, 1), 100)},
            )
            if err:
                return err
            body = resp.json() or {}
            indicators = body.get("iocObjects") or body.get("data") or []
            total = body.get("total")

            def _fmt(ind: dict) -> str:
                brands = ind.get("sourceBrands") or []
                return (
                    f"  {_SCORE_TO_VERDICT.get(ind.get('score', 0), '⚪ Unknown')} "
                    f"{ind.get('value', '?')} "
                    f"[{ind.get('indicator_type', ind.get('indicatorType', '?'))}]\n"
                    f"    Sources: {', '.join(str(b) for b in brands) or 'N/A'} | "
                    f"Last seen: {_dt(ind.get('lastSeen'))}"
                )

            return format_entity_list(indicators, _fmt, noun="indicator", total=total)

    async def get_xsoar_indicator(value: str) -> str:
        """Look up a single indicator by exact value (reputation / verdict)."""
        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "post",
                f"{api_base}/indicators/search",
                _PROVIDER,
                f"get indicator {value}",
                headers=_headers(),
                json={"query": f'value:"{_lucene_phrase(value)}"', "size": 1},
            )
            if err:
                return err
            indicators = (resp.json() or {}).get("iocObjects") or []
            if not indicators:
                return f"No indicator found for value '{value}'."
            ind = indicators[0]
            lines = [
                f"Indicator: {ind.get('value', value)}",
                f"Type: {ind.get('indicator_type', ind.get('indicatorType', '?'))}",
                f"Verdict: {_SCORE_TO_VERDICT.get(ind.get('score', 0), '⚪ Unknown')}",
                f"Sources: {', '.join(str(b) for b in (ind.get('sourceBrands') or [])) or 'N/A'}",
                f"First seen: {_dt(ind.get('firstSeen'))} | "
                f"Last seen: {_dt(ind.get('lastSeen'))}",
                f"Expiration: {ind.get('expirationStatus', 'N/A')}",
            ]
            if ind.get("comment"):
                lines.append(f"Comment: {_trunc(ind.get('comment'), 300)}")
            return "\n".join(lines)

    async def create_xsoar_indicator(
        value: str,
        indicator_type: str,
        reputation: str = "",
        comment: str = "",
    ) -> str:
        """Create a Cortex XSOAR threat-intel indicator (CREATING INTELLIGENCE).

        `indicator_type` is the XSOAR type (e.g. IP, Domain, URL, File, Email).
        `reputation` optionally sets the verdict: good, suspicious, or bad.
        """
        indicator: dict[str, Any] = {
            "value": value,
            "indicator_type": indicator_type,
            "source": get_cached_branding().name,
        }
        verdict_map = {"good": 1, "suspicious": 2, "bad": 3, "malicious": 3}
        if reputation and reputation.lower() in verdict_map:
            indicator["score"] = verdict_map[reputation.lower()]
        if comment:
            indicator["comment"] = comment

        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "post",
                f"{api_base}/indicator/create",
                _PROVIDER,
                "create indicator",
                headers=_headers(),
                json={"indicator": indicator, "seenNow": True},
            )
            if err:
                return err
            created = resp.json() or {}
            return (
                f"Indicator created (id: {created.get('id', '?')}): "
                f"{indicator_type}={value}"
            )

    async def exclude_xsoar_indicator(
        value: str, action: str = "whitelist", reason: str = ""
    ) -> str:
        """Whitelist (exclude) or delete a Cortex XSOAR indicator by value."""
        delete = action.lower() == "delete"
        payload: dict[str, Any] = {"value": value, "doNotWhitelist": delete}
        if reason:
            payload["reason"] = reason
        async with egress_client(timeout=30) as client:
            _, err = await safe_request(
                client,
                "post",
                f"{api_base}/indicator/whitelist",
                _PROVIDER,
                f"{action} indicator {value}",
                headers=_headers(),
                json=payload,
            )
            if err:
                return err
            verb = "deleted" if delete else "whitelisted (excluded)"
            return f"Indicator '{value}' {verb}."

    # ══════════════════════════════════════════════════════════════════════
    # 5. CONTENT & RBAC
    # ══════════════════════════════════════════════════════════════════════

    async def list_xsoar_lists() -> str:
        """List Cortex XSOAR lists (reusable content used by playbooks)."""
        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{api_base}/lists",
                _PROVIDER,
                "list lists",
                headers=_headers(),
            )
            if err:
                return err
            data = resp.json()
            lists = data if isinstance(data, list) else (data.get("data") or [])

            def _fmt(lst: dict) -> str:
                return (
                    f"  {lst.get('name', lst.get('id', '?'))} "
                    f"(type: {lst.get('type', '?')})"
                )

            return format_entity_list(lists, _fmt, noun="list")

    async def list_xsoar_users() -> str:
        """List Cortex XSOAR users (for incident assignment)."""
        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{api_base}/users",
                _PROVIDER,
                "list users",
                headers=_headers(),
            )
            if err:
                return err
            data = resp.json()
            users = data if isinstance(data, list) else (data.get("data") or [])

            def _fmt(u: dict) -> str:
                roles = u.get("roles") or {}
                role_names = (
                    roles.get("roles")
                    if isinstance(roles, dict)
                    else roles if isinstance(roles, list) else []
                )
                return (
                    f"  {u.get('username', u.get('id', '?'))} "
                    f"({u.get('name', '')}) — "
                    f"{u.get('email', 'no email')} | "
                    f"Roles: {', '.join(str(r) for r in (role_names or [])) or 'N/A'}"
                )

            return format_entity_list(users, _fmt, noun="user")

    # ══════════════════════════════════════════════════════════════════════
    # REGISTER ALL TOOLS
    # ══════════════════════════════════════════════════════════════════════

    executor.register("get_xsoar_status", get_xsoar_status)
    executor.register("list_xsoar_incidents", list_xsoar_incidents)
    executor.register("get_xsoar_incident", get_xsoar_incident)
    executor.register("create_xsoar_incident", create_xsoar_incident)
    executor.register("update_xsoar_incident", update_xsoar_incident)
    executor.register("close_xsoar_incident", close_xsoar_incident)
    executor.register("add_xsoar_incident_entry", add_xsoar_incident_entry)
    executor.register("get_xsoar_incident_war_room", get_xsoar_incident_war_room)
    executor.register("run_xsoar_command", run_xsoar_command)
    executor.register("list_xsoar_playbooks", list_xsoar_playbooks)
    executor.register("list_xsoar_indicators", list_xsoar_indicators)
    executor.register("get_xsoar_indicator", get_xsoar_indicator)
    executor.register("create_xsoar_indicator", create_xsoar_indicator)
    executor.register("exclude_xsoar_indicator", exclude_xsoar_indicator)
    executor.register("list_xsoar_lists", list_xsoar_lists)
    executor.register("list_xsoar_users", list_xsoar_users)

    return [
        # ── 0. Connection ──────────────────────────────────────────────────
        ToolDefinition(
            name="get_xsoar_status",
            description=(
                "Validate the Cortex XSOAR connection and report the number of "
                "accessible incidents. Use first if other XSOAR tools fail."
            ),
            input_schema={"type": "object", "properties": {}, "required": []},
            source=_SOURCE,
            priority=2,
        ),
        # ── 1. Incidents ───────────────────────────────────────────────────
        ToolDefinition(
            name="list_xsoar_incidents",
            description=(
                "Search Cortex XSOAR incidents (gathering incidents). Provide a "
                "Lucene `query` (e.g. 'type:Phishing and -status:closed') OR the "
                "structured filters status (pending/active/closed/archived), "
                "severity (low/medium/high/critical), incident_type. When `query` "
                "is set the structured filters are ignored."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Lucene query (optional)"},
                    "status": {
                        "type": "string",
                        "enum": ["pending", "active", "closed", "archived"],
                    },
                    "severity": {
                        "type": "string",
                        "enum": ["informational", "low", "medium", "high", "critical"],
                    },
                    "incident_type": {"type": "string"},
                    "size": {"type": "integer", "default": 25},
                    "page": {"type": "integer", "default": 0},
                },
                "required": [],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="get_xsoar_incident",
            description="Get full details of a Cortex XSOAR incident by id (fields, labels, playbook, timeline).",
            input_schema={
                "type": "object",
                "properties": {"incident_id": {"type": "string"}},
                "required": ["incident_id"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="create_xsoar_incident",
            description=(
                "Create a Cortex XSOAR incident. Set create_investigation=true to "
                "automatically start the investigation and run the incident type's "
                "playbook (or the given playbook_id) — the primary way to trigger "
                "SOAR orchestration."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "incident_type": {"type": "string", "default": "Unclassified"},
                    "severity": {
                        "type": "string",
                        "enum": ["informational", "low", "medium", "high", "critical"],
                        "default": "medium",
                    },
                    "details": {"type": "string"},
                    "labels": {
                        "type": "object",
                        "description": 'Label map, e.g. {"source": "EDR", "sha256": "..."}',
                        "additionalProperties": {"type": "string"},
                    },
                    "create_investigation": {"type": "boolean", "default": False},
                    "playbook_id": {"type": "string"},
                },
                "required": ["name"],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="update_xsoar_incident",
            description="Update fields on an existing Cortex XSOAR incident (name, severity, type, owner, details). Only provided fields change.",
            input_schema={
                "type": "object",
                "properties": {
                    "incident_id": {"type": "string"},
                    "name": {"type": "string"},
                    "severity": {
                        "type": "string",
                        "enum": ["informational", "low", "medium", "high", "critical"],
                    },
                    "incident_type": {"type": "string"},
                    "owner": {"type": "string", "description": "Username to assign"},
                    "details": {"type": "string"},
                },
                "required": ["incident_id"],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="close_xsoar_incident",
            description="Close a Cortex XSOAR incident with a close reason and optional notes.",
            input_schema={
                "type": "object",
                "properties": {
                    "incident_id": {"type": "string"},
                    "close_reason": {"type": "string", "default": "Resolved"},
                    "close_notes": {"type": "string"},
                },
                "required": ["incident_id"],
            },
            source=_SOURCE,
            priority=1,
        ),
        # ── 2. War Room / orchestration ────────────────────────────────────
        ToolDefinition(
            name="add_xsoar_incident_entry",
            description="Add a note (markdown) to an incident's War Room for the investigation trail.",
            input_schema={
                "type": "object",
                "properties": {
                    "investigation_id": {
                        "type": "string",
                        "description": "Investigation id (same as the incident id)",
                    },
                    "note": {"type": "string"},
                },
                "required": ["investigation_id", "note"],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="get_xsoar_incident_war_room",
            description="Read recent War Room entries for an incident investigation (notes, command outputs, playbook steps).",
            input_schema={
                "type": "object",
                "properties": {
                    "investigation_id": {"type": "string"},
                    "count": {"type": "integer", "default": 25},
                },
                "required": ["investigation_id"],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="run_xsoar_command",
            description=(
                "Run ANY Cortex XSOAR command or automation and return its output "
                "(threat hunting, enrichment, response). Use War Room syntax, e.g. "
                "'!ip ip=1.2.3.4', '!domain domain=evil.com', '!file file=SHA256', "
                "'!url url=...', or any installed integration command/automation. "
                "Omit investigation_id to run in the playground. This exposes the "
                "platform's entire installed command surface."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "War Room command, e.g. '!ip ip=8.8.8.8'",
                    },
                    "investigation_id": {
                        "type": "string",
                        "description": "Incident/investigation id to run within (optional — defaults to playground)",
                    },
                },
                "required": ["command"],
            },
            source=_SOURCE,
            priority=0,
        ),
        # ── 3. Playbooks ───────────────────────────────────────────────────
        ToolDefinition(
            name="list_xsoar_playbooks",
            description="List/search Cortex XSOAR playbooks. Run a playbook by creating an incident with create_investigation=true and its playbook_id.",
            input_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "size": {"type": "integer", "default": 30},
                },
                "required": [],
            },
            source=_SOURCE,
            priority=2,
        ),
        # ── 4. Threat Intelligence ─────────────────────────────────────────
        ToolDefinition(
            name="list_xsoar_indicators",
            description=(
                "Search Cortex XSOAR threat-intel indicators with a Lucene `query` "
                "(e.g. 'type:IP and score:3' for malicious IPs). Returns value, "
                "type, verdict, and sources."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "size": {"type": "integer", "default": 25},
                },
                "required": [],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="get_xsoar_indicator",
            description="Look up a single indicator by its exact value and return its verdict, sources, and timeline.",
            input_schema={
                "type": "object",
                "properties": {"value": {"type": "string"}},
                "required": ["value"],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="create_xsoar_indicator",
            description=(
                "Create a Cortex XSOAR threat-intel indicator (CREATING "
                "INTELLIGENCE). Provide the value and XSOAR indicator_type "
                "(IP, Domain, URL, File, Email, etc.) and optionally a reputation "
                "(good/suspicious/bad)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "value": {"type": "string"},
                    "indicator_type": {
                        "type": "string",
                        "description": "XSOAR indicator type, e.g. IP, Domain, URL, File, Email",
                    },
                    "reputation": {
                        "type": "string",
                        "enum": ["good", "suspicious", "bad"],
                    },
                    "comment": {"type": "string"},
                },
                "required": ["value", "indicator_type"],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="exclude_xsoar_indicator",
            description="Whitelist (exclude) or delete a Cortex XSOAR indicator by value. action='whitelist' excludes it; action='delete' removes it.",
            input_schema={
                "type": "object",
                "properties": {
                    "value": {"type": "string"},
                    "action": {
                        "type": "string",
                        "enum": ["whitelist", "delete"],
                        "default": "whitelist",
                    },
                    "reason": {"type": "string"},
                },
                "required": ["value"],
            },
            source=_SOURCE,
            priority=2,
        ),
        # ── 5. Content & RBAC ──────────────────────────────────────────────
        ToolDefinition(
            name="list_xsoar_lists",
            description="List Cortex XSOAR lists (reusable content referenced by playbooks).",
            input_schema={"type": "object", "properties": {}, "required": []},
            source=_SOURCE,
            priority=2,
        ),
        ToolDefinition(
            name="list_xsoar_users",
            description="List Cortex XSOAR users and their roles (for incident assignment).",
            input_schema={"type": "object", "properties": {}, "required": []},
            source=_SOURCE,
            priority=2,
        ),
    ]
