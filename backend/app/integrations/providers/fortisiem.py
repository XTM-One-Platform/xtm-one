# SPDX-License-Identifier: Apache-2.0

"""Fortinet FortiSIEM integration – Agentic SOC SIEM for the SecOps team.

Full-fledged Fortinet FortiSIEM integration covering the detection →
investigation → hunting → response → intelligence loop, designed as a
best-in-class building block for an agentic security team:

  - Incidents (*gathering incidents*): list (filterable by status, category,
    resolution, severity, time window), get full detail, triggering events
    (raw logs), and update (comments / clear / external ticket sync)
  - Event query (*threat hunting*): run a FortiSIEM event query (submit XML →
    poll progress → fetch results), fetch results for an existing query id
  - Watch lists (*creating intelligence*): list/get/find-by-value, add IOC
    entries, and a consolidated manage tool (create / enable-disable entry /
    delete entry / delete watch list)
  - Lookup tables (enrichment data): list tables, query rows
  - CMDB context: device inventory (with IP-range filters) and device detail
  - Connectivity / status check

Authentication
--------------
FortiSIEM authenticates every REST call with **HTTP Basic** credentials where
the login is ``organization/username`` (e.g. ``super/apiuser`` for the
Super/Global org — also used by Enterprise deployments — or ``ACME/apiuser``
for a Service Provider organization). ``integration_tools._get_token``
assembles the compound ``base_url||organization||username||password`` token
this builder parses (base URL = the FortiSIEM Supervisor host, e.g.
``https://fortisiem.example.com``). All calls target ``{base_url}/phoenix/rest``.

Payload formats are MIXED by design: the pub incident, watchlist, and lookup
table APIs speak JSON, while the event query API takes an XML ``<Reports>``
document and returns XML event chunks (parsed here with stdlib ElementTree,
which does not resolve external entities/DTDs). Watchlist APIs signal logical
failures with HTTP 200 + ``{"status": "Failed"}`` — handled explicitly.
"""

from __future__ import annotations

import asyncio
import logging
import re
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from typing import Any
from xml.sax.saxutils import escape

import httpx

from app.core.egress import egress_client
from app.integrations.http import (
    format_entity_list,
    parse_compound_token,
    safe_request,
)
from app.tools.base import CallableToolExecutor, ToolDefinition

logger = logging.getLogger(__name__)

_PROVIDER = "Fortinet FortiSIEM"
_SOURCE = "integration:fortisiem"

# Event-query polling budget (submit XML -> poll progress -> fetch results).
_QUERY_POLL_INTERVAL = 3.0
_QUERY_MAX_POLLS = 10

# Event-query ids are interpolated into URL path segments (progress/results),
# so they must stay a single safe segment. Real FortiSIEM query ids include a
# comma (``<id>,<timestamp>``, e.g. ``10412,1753450073522``) — allow it, but
# nothing that could traverse the path ("/", ".", "%", whitespace).
_QUERY_ID_RE = re.compile(r"[A-Za-z0-9_,-]+")

# Upper bound on items requested in a single paginated call, so an unbounded
# caller-supplied limit can't request an enormous response or stress the
# Supervisor (FortiSIEM caps lookup-table pages at 1000 itself).
_MAX_PAGE = 1000

# User-facing message (not a secret) shown when the credential is blank/missing.
_CREDS_ERROR = (
    "Error: FortiSIEM credentials are not configured. Set the Supervisor base "
    "URL, username (and optional organization) in the integration's extra "
    "config and the password in the API Key field."
)

# Integer code -> label mappings from the FortiSIEM Integration API Guide.
INCIDENT_STATUS_LABELS: dict[int, str] = {
    0: "Active",
    1: "Auto Cleared",
    2: "Manually Cleared",
    3: "System Cleared",
}
INCIDENT_RESOLUTION_LABELS: dict[int, str] = {
    0: "None",
    1: "Open",
    2: "True Positive",
    3: "False Positive",
    4: "In Progress",
}
INCIDENT_CATEGORY_LABELS: dict[int, str] = {
    1: "Availability",
    2: "Performance",
    3: "Change",
    4: "Security",
    5: "Other",
}
_CATEGORY_IDS = {label.lower(): code for code, label in INCIDENT_CATEGORY_LABELS.items()}
_RESOLUTION_IDS = {
    "none": 0,
    "open": 1,
    "true_positive": 2,
    "false_positive": 3,
    "in_progress": 4,
}

# Default incident fields requested from the pub incident API (must include
# every field the formatters read; orderBy must be in this list).
_INCIDENT_FIELDS: list[str] = [
    "incidentId",
    "incidentTitle",
    "eventSeverity",
    "eventSeverityCat",
    "eventType",
    "eventName",
    "incidentStatus",
    "incidentReso",
    "phIncidentCategory",
    "phSubIncidentCategory",
    "incidentFirstSeen",
    "incidentLastSeen",
    "incidentSrc",
    "incidentTarget",
    "incidentDetail",
    "incidentRptIp",
    "incidentRptDevName",
    "incidentComments",
    "incidentClearedTime",
    "incidentClearedUser",
    "incidentClearedReason",
    "incidentExtUser",
    "incidentExtTicketId",
    "incidentExtTicketState",
    "incidentExtTicketType",
    "count",
    "customer",
    "attackTactic",
    "attackTechnique",
]

# Default attribute list for the event query API (comma-separated in the XML).
_DEFAULT_EVENT_ATTRS = "phRecvTime,reptDevIpAddr,eventType,eventName,rawEventMsg"

_VALID_WATCHLIST_ACTIONS = frozenset(
    {"create", "set_entry_state", "delete_entry", "delete_watchlist"}
)


def _sev_icon(severity: Any, category: Any = "") -> str:
    cat = str(category or "").upper()
    if cat == "HIGH":
        return "🔴"
    if cat == "MEDIUM":
        return "🟠"
    if cat == "LOW":
        return "🔵"
    try:
        s = int(severity)
    except (TypeError, ValueError):
        return "⚪"
    if s >= 9:
        return "🔴"
    if s >= 5:
        return "🟠"
    return "🔵"


def _ts(value: Any) -> str:
    """Render an epoch-milliseconds timestamp as a UTC ISO second-precision string."""
    if value in (None, "", 0, "0"):
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


def _cell(value: Any, max_len: int = 80) -> str:
    """Render a query-result cell value safe for a pipe-delimited table.

    Strips embedded newlines/tabs and replaces literal ``|`` with ``¦``
    (raw log messages — CEF, FortiGate key-value — routinely contain pipes
    that would otherwise break the column structure of the rendered table).
    """
    if value is None:
        return "null"
    text = (
        str(value)
        .replace("\r", " ")
        .replace("\n", " ")
        .replace("\t", " ")
        .replace("|", "¦")
    )
    return text[:max_len]


def _clamp(value: Any, default: int, lo: int = 1, hi: int = _MAX_PAGE) -> int:
    try:
        return max(lo, min(int(value), hi))
    except (TypeError, ValueError):
        return default


def _parse_id(value: Any, label: str) -> tuple[int, str | None]:
    """Coerce a required numeric id defensively.

    Tool args are not schema-coerced at execution time, so ids may arrive
    as strings (fine for ``int()``) or garbage (must yield an actionable
    error instead of a raw ``ValueError``). Returns ``(id, None)`` on
    success or ``(0, error_message)`` on failure.
    """
    try:
        return int(value), None
    except (TypeError, ValueError):
        return 0, f"Error: {label} must be a numeric FortiSIEM id, got {value!r}."


def _to_bool(value: Any, default: bool = True) -> bool:
    """Coerce a tool-arg boolean defensively.

    Tool args are not schema-coerced at execution time, so ``state`` may
    arrive as a string — and ``bool("false")`` is ``True``, which would
    silently ENABLE an entry the caller asked to disable.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ("true", "1", "yes", "on", "enabled"):
            return True
        if lowered in ("false", "0", "no", "off", "disabled"):
            return False
    return default


def _offset(value: Any) -> int:
    """Coerce a pagination offset safely (tool args may arrive as strings).

    Unlike :func:`_clamp` there is no upper bound — offsets legitimately
    exceed the per-page cap when paging deep result sets.
    """
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _status_label(code: Any) -> str:
    try:
        return INCIDENT_STATUS_LABELS.get(int(code), str(code))
    except (TypeError, ValueError):
        return str(code or "?")


def _reso_label(code: Any) -> str:
    try:
        return INCIDENT_RESOLUTION_LABELS.get(int(code), str(code))
    except (TypeError, ValueError):
        return str(code or "?")


def _category_label(code: Any) -> str:
    try:
        return INCIDENT_CATEGORY_LABELS.get(int(code), str(code))
    except (TypeError, ValueError):
        return str(code or "?")


# ---------------------------------------------------------------------------
# Auth + URL helpers (shared with integration_testing.py)
# ---------------------------------------------------------------------------


def fortisiem_api_base(base_url: str) -> str:
    """Return the FortiSIEM REST API base URL (``{base_url}/phoenix/rest``)."""
    return f"{(base_url or '').strip().rstrip('/')}/phoenix/rest"


def build_fortisiem_headers(
    organization: str, username: str, password: str
) -> dict[str, str]:
    """Build FortiSIEM request headers (HTTP Basic with ``org/user`` login).

    FortiSIEM expects the Basic auth login as ``organization/username`` (e.g.
    ``super/apiuser``). When no organization is configured the bare username is
    sent (Enterprise deployments accept both forms). Credentials are stripped so
    a value pasted with a trailing newline/space can't silently break auth.
    """
    import base64

    organization = (organization or "").strip()
    username = (username or "").strip()
    login = f"{organization}/{username}" if organization else username
    basic = base64.b64encode(f"{login}:{(password or '').strip()}".encode()).decode(
        "ascii"
    )
    return {
        "Authorization": f"Basic {basic}",
        "Accept": "application/json",
    }


# ---------------------------------------------------------------------------
# XML helpers (event query + CMDB — FortiSIEM mixes XML and JSON by design)
# ---------------------------------------------------------------------------


def _xml_root(content: str) -> ET.Element | None:
    """Parse a FortiSIEM XML response body into a root element, or None."""
    try:
        # S314: the body comes from an authenticated, TLS-protected FortiSIEM
        # Supervisor (trusted), and stdlib ElementTree does not resolve
        # external entities/DTDs, so there is no practical XXE vector here.
        return ET.fromstring(content)  # noqa: S314
    except ET.ParseError:
        return None


def build_event_query_xml(constraint: str, attributes: str, low: int, high: int) -> str:
    """Build the ``<Reports>`` XML document the event query API expects.

    ``constraint`` is a FortiSIEM single-event constraint (e.g.
    ``eventSeverity >= 9 AND srcIpAddr = 10.0.0.5``); ``low``/``high`` are the
    report interval bounds in epoch SECONDS (the event query API uses seconds,
    unlike the incident API's milliseconds). User-supplied text is XML-escaped.
    """
    return (
        "<Reports><Report id=\"\" group=\"report\">"
        "<Name>XTM One Event Query</Name>"
        "<CustomerScope groupByEachCustomer=\"true\">"
        "<Include all=\"true\"/><Exclude/></CustomerScope>"
        "<description/>"
        f"<SelectClause numEntries=\"All\"><AttrList>{escape(attributes)}</AttrList>"
        "</SelectClause>"
        f"<ReportInterval><Low>{int(low)}</Low><High>{int(high)}</High>"
        "</ReportInterval>"
        "<PatternClause window=\"3600\">"
        "<SubPattern displayName=\"Events\" name=\"Events\">"
        f"<SingleEvtConstr>{escape(constraint)}</SingleEvtConstr>"
        "</SubPattern></PatternClause>"
        "<RelevantFilterAttr/>"
        "</Report></Reports>"
    )


def _parse_event_results(content: str, attributes: str, max_rows: int) -> str:
    """Render the event query results XML as a compact pipe-delimited table."""
    root = _xml_root(content)
    if root is None:
        return "Error: FortiSIEM returned a non-XML or unparseable results body."
    total = ""
    for elem in root.iter():
        if "totalCount" in elem.attrib:
            total = elem.attrib["totalCount"]
            break
    rows: list[dict[str, str]] = []
    for event in root.iter("event"):
        mapping: dict[str, str] = {}
        for attr in event.iter("attribute"):
            name = attr.attrib.get("name", "")
            if name:
                mapping[name] = (attr.text or "").strip()
        if mapping:
            rows.append(mapping)
    if not rows:
        return f"Query completed: 0 events{f' (totalCount {total})' if total else ''}."
    columns = [a.strip() for a in attributes.split(",") if a.strip()]
    if not columns:
        columns = list(rows[0].keys())
    lines = [
        f"Event query results ({len(rows)} shown"
        + (f", totalCount {total}" if total else "")
        + "):",
        " | ".join(columns),
        "-" * min(len(" | ".join(columns)), 120),
    ]
    for row in rows[:max_rows]:
        lines.append(" | ".join(_cell(row.get(c)) for c in columns))
    if len(rows) > max_rows:
        lines.append(f"\n... [{len(rows) - max_rows} more events not shown]")
    return "\n".join(lines)


def _progress_pct(text: str) -> int:
    """Extract the progress percentage from the polling response.

    The body is either the bare number (``100``) or a small XML document
    wrapping it; take the last integer found.
    """
    matches = re.findall(r"\d+", text or "")
    return int(matches[-1]) if matches else -1


# ---------------------------------------------------------------------------
# JSON envelope helper (watchlist APIs return HTTP 200 + status: Failed)
# ---------------------------------------------------------------------------


def _watchlist_payload(resp: httpx.Response) -> tuple[Any, str | None]:
    """Unwrap the watchlist ``{"response": ..., "status": ...}`` envelope."""
    try:
        data = resp.json() or {}
    except Exception:
        return None, "Error: FortiSIEM returned a non-JSON watchlist response."
    if isinstance(data, dict) and data.get("status") == "Failed":
        return None, f"Error: FortiSIEM watchlist API failed: {data.get('response')}"
    if isinstance(data, dict):
        return data.get("response"), None
    return data, None


def _format_watchlist(wl: dict, *, max_entries: int = 15) -> str:
    entries = wl.get("entries") or []
    shown = min(len(entries), max_entries)
    lines = [
        f"  Watch list #{wl.get('id', '?')} {wl.get('displayName') or '(unnamed)'} "
        f"[{wl.get('valueType', '?')}] — {len(entries)} entries ({shown} shown) | "
        f"ageOut: {wl.get('ageOut') or 'none'} | "
        f"created by: {wl.get('dataCreationType') or '?'}"
    ]
    if wl.get("description"):
        lines.append(f"    {_trunc(wl.get('description'), 120)}")
    for e in entries[:max_entries]:
        lines.append(
            f"    entry #{e.get('id', '?')} {_trunc(e.get('entryValue'), 60)} "
            f"[{e.get('state', '?')}] count: {e.get('count') or 0} | "
            f"last seen: {_ts(e.get('lastSeen')) or 'N/A'}"
        )
    if len(entries) > max_entries:
        lines.append(f"    ... [{len(entries) - max_entries} more entries not shown]")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Tool builder
# ---------------------------------------------------------------------------


def build_tools(
    token: str,
    executor: CallableToolExecutor,
    **_kwargs: Any,
) -> list[ToolDefinition]:
    """Build FortiSIEM tools from a ``base_url||organization||username||password`` compound token."""
    base_url, organization, username, password = parse_compound_token(
        token, 4, greedy_last=True
    )
    base_url = (base_url or "").strip().rstrip("/")
    if not base_url:
        logger.debug(
            "FortiSIEM base_url is empty - returning no tools. Edit the "
            "integration and set the Supervisor base URL."
        )
        return []

    api = fortisiem_api_base(base_url)
    headers = build_fortisiem_headers(organization, username, password)
    has_creds = bool((username or "").strip() and (password or "").strip())

    def _incident_fmt(i: dict) -> str:
        return (
            f"  {_sev_icon(i.get('eventSeverity'), i.get('eventSeverityCat'))} "
            f"incident #{i.get('incidentId', '?')} "
            f"[sev {i.get('eventSeverity', '?')}/{i.get('eventSeverityCat', '?')}] "
            f"{_status_label(i.get('incidentStatus'))} — "
            f"{_trunc(i.get('incidentTitle') or i.get('eventName'), 90)}\n"
            f"    Category: {_category_label(i.get('phIncidentCategory'))} | "
            f"Resolution: {_reso_label(i.get('incidentReso'))} | "
            f"Count: {i.get('count', 0)} | Org: {i.get('customer') or '?'}\n"
            f"    Target: {_trunc(i.get('incidentTarget'), 80) or 'N/A'} | "
            f"Reporting: {i.get('incidentRptDevName') or i.get('incidentRptIp') or '?'}\n"
            f"    First seen: {_ts(i.get('incidentFirstSeen'))} | "
            f"Last seen: {_ts(i.get('incidentLastSeen'))}"
        )

    async def _fetch_incidents(
        client: httpx.AsyncClient, payload: dict, action: str
    ) -> tuple[dict | None, str | None]:
        resp, err = await safe_request(
            client,
            "post",
            f"{api}/pub/incident",
            _PROVIDER,
            action,
            headers={**headers, "Content-Type": "application/json"},
            json=payload,
        )
        if err:
            return None, err
        try:
            return resp.json() or {}, None
        except Exception:
            return None, "Error: FortiSIEM returned a non-JSON incidents response."

    # ══════════════════════════════════════════════════════════════════════
    # 0. CONNECTION
    # ══════════════════════════════════════════════════════════════════════

    async def get_fortisiem_status() -> str:
        """Validate the FortiSIEM connection and report reachability."""
        if not has_creds:
            return _CREDS_ERROR
        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{api}/watchlist/cnt",
                _PROVIDER,
                "check connectivity",
                headers=headers,
            )
            if err:
                return err
            payload, wl_err = _watchlist_payload(resp)
            if wl_err:
                return wl_err
            count = (payload or {}).get("entry_count", "?") if isinstance(payload, dict) else "?"
            return (
                f"Connected to Fortinet FortiSIEM at {base_url}.\n"
                f"  Watch list entries visible to this account: {count}"
            )

    # ══════════════════════════════════════════════════════════════════════
    # 1. INCIDENTS (gathering incidents)
    # ══════════════════════════════════════════════════════════════════════

    async def list_fortisiem_incidents(
        hours: int = 24,
        status: str = "",
        category: str = "",
        resolution: str = "",
        min_severity: int = 0,
        limit: int = 25,
        start: int = 0,
    ) -> str:
        """List FortiSIEM incidents in a time window, filterable by status/category/resolution/severity."""
        if not has_creds:
            return _CREDS_ERROR
        now_ms = int(datetime.now(UTC).timestamp() * 1000)
        try:
            hours = max(1, int(hours))
        except (TypeError, ValueError):
            hours = 24
        try:
            min_severity = int(min_severity)
        except (TypeError, ValueError):
            min_severity = 0
        filters: dict[str, Any] = {}
        if status:
            status_ids = {
                "active": 0,
                "auto_cleared": 1,
                "manually_cleared": 2,
                "system_cleared": 3,
            }
            code = status_ids.get(status.strip().lower())
            if code is None:
                return (
                    "Error: status must be one of active, auto_cleared, "
                    "manually_cleared, system_cleared."
                )
            filters["status"] = [code]
        if category:
            code = _CATEGORY_IDS.get(category.strip().lower())
            if code is None:
                return (
                    "Error: category must be one of availability, performance, "
                    "change, security, other."
                )
            filters["phIncidentCategory"] = [code]
        if resolution:
            code = _RESOLUTION_IDS.get(resolution.strip().lower())
            if code is None:
                return (
                    "Error: resolution must be one of none, open, true_positive, "
                    "false_positive, in_progress."
                )
            filters["incidentReso"] = [code]
        payload: dict[str, Any] = {
            "filters": filters,
            "start": _offset(start),
            "size": _clamp(limit, 25),
            "timeFrom": now_ms - hours * 3_600_000,
            "timeTo": now_ms,
            "orderBy": "incidentLastSeen",
            "descending": True,
            "fields": _INCIDENT_FIELDS,
        }
        async with egress_client(timeout=60) as client:
            data, err = await _fetch_incidents(client, payload, "list incidents")
            if err:
                return err
            incidents = data.get("data") or []
            fetched = len(incidents)
            if min_severity > 0:
                incidents = [
                    i
                    for i in incidents
                    if isinstance(i.get("eventSeverity"), (int, float))
                    and i["eventSeverity"] >= min_severity
                ]
            total = data.get("total")
            listing = format_entity_list(incidents, _incident_fmt, noun="incident")
            if isinstance(total, int) and total > fetched + payload["start"]:
                # ``total`` counts the SERVER-side matches; min_severity is
                # applied client-side per page, so qualify the hint to avoid
                # implying every one of those incidents passes the filter.
                qualifier = (
                    " the server-side filters (min_severity is applied per page)"
                    if min_severity > 0
                    else ""
                )
                listing += (
                    f"\n[{total} incidents match{qualifier} in total — pass "
                    f"start={payload['start'] + payload['size']} for the next page.]"
                )
            return listing

    async def get_fortisiem_incident(incident_id: int) -> str:
        """Get full details of a FortiSIEM incident by id."""
        if not has_creds:
            return _CREDS_ERROR
        incident_id, id_err = _parse_id(incident_id, "incident_id")
        if id_err:
            return id_err
        payload = {
            "filters": {"incidentId": [incident_id]},
            "size": 1,
            "fields": _INCIDENT_FIELDS,
        }
        async with egress_client(timeout=30) as client:
            data, err = await _fetch_incidents(
                client, payload, f"get incident {incident_id}"
            )
            if err:
                return err
            incidents = data.get("data") or []
            if not incidents:
                return f"Incident {incident_id} not found."
            i = incidents[0]
            lines = [
                f"Incident #{i.get('incidentId', incident_id)}: "
                f"{_trunc(i.get('incidentTitle') or i.get('eventName'), 200)}",
                f"Severity: {_sev_icon(i.get('eventSeverity'), i.get('eventSeverityCat'))} "
                f"{i.get('eventSeverity', '?')} ({i.get('eventSeverityCat', '?')}) | "
                f"Status: {_status_label(i.get('incidentStatus'))} | "
                f"Resolution: {_reso_label(i.get('incidentReso'))}",
                f"Category: {_category_label(i.get('phIncidentCategory'))}"
                f"{' / ' + str(i.get('phSubIncidentCategory')) if i.get('phSubIncidentCategory') else ''} | "
                f"Rule: {i.get('eventName') or i.get('eventType') or '?'} | "
                f"Count: {i.get('count', 0)}",
                f"Source: {_trunc(i.get('incidentSrc'), 120) or 'N/A'}",
                f"Target: {_trunc(i.get('incidentTarget'), 120) or 'N/A'}",
                f"Detail: {_trunc(i.get('incidentDetail'), 300) or 'N/A'}",
                f"Reporting device: {i.get('incidentRptDevName') or '?'} "
                f"({i.get('incidentRptIp') or '?'}) | Org: {i.get('customer') or '?'}",
                f"First seen: {_ts(i.get('incidentFirstSeen'))} | "
                f"Last seen: {_ts(i.get('incidentLastSeen'))}",
            ]
            if i.get("attackTactic") or i.get("attackTechnique"):
                lines.append(
                    f"MITRE ATT&CK: tactic {i.get('attackTactic') or '?'} | "
                    f"technique(s): {_trunc(i.get('attackTechnique'), 200) or '?'}"
                )
            if i.get("incidentClearedTime"):
                lines.append(
                    f"Cleared: {_ts(i.get('incidentClearedTime'))} by "
                    f"{i.get('incidentClearedUser') or '?'} "
                    f"({_trunc(i.get('incidentClearedReason'), 120) or 'no reason'})"
                )
            if i.get("incidentComments"):
                lines.append(f"Comments: {_trunc(i.get('incidentComments'), 300)}")
            if i.get("incidentExtTicketId"):
                lines.append(
                    f"External ticket: {i.get('incidentExtTicketId')} "
                    f"[{i.get('incidentExtTicketState') or '?'}] "
                    f"assigned to {i.get('incidentExtUser') or '?'}"
                )
            return "\n".join(lines)

    async def get_fortisiem_incident_events(incident_id: int, limit: int = 10) -> str:
        """Get the triggering events (raw logs) behind a FortiSIEM incident."""
        if not has_creds:
            return _CREDS_ERROR
        incident_id, id_err = _parse_id(incident_id, "incident_id")
        if id_err:
            return id_err
        async with egress_client(timeout=60) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{api}/pub/incident/triggeringEvents",
                _PROVIDER,
                f"get triggering events for incident {incident_id}",
                headers=headers,
                params={"incidentId": incident_id, "size": _clamp(limit, 10, hi=100)},
            )
            if err:
                return err
            try:
                events = resp.json() or []
            except Exception:
                return "Error: FortiSIEM returned a non-JSON triggering-events response."
            if not isinstance(events, list) or not events:
                return f"No triggering events found for incident {incident_id}."

            def _fmt(e: dict) -> str:
                return (
                    f"  event {e.get('id', '?')} [{e.get('eventType', '?')}] "
                    f"received {_ts(e.get('receiveTime'))}\n"
                    f"    {_trunc(e.get('rawMessage'), 400) or 'N/A'}"
                )

            return format_entity_list(events, _fmt, noun="triggering event")

    async def update_fortisiem_incident(
        incident_id: int,
        comments: str = "",
        status: str = "",
        external_ticket_id: str = "",
        external_ticket_state: str = "",
        external_assigned_user: str = "",
    ) -> str:
        """Update a FortiSIEM incident: add comments, clear it, or sync external ticket fields."""
        if not has_creds:
            return _CREDS_ERROR
        incident_id, id_err = _parse_id(incident_id, "incident_id")
        if id_err:
            return id_err
        payload: dict[str, Any] = {"incidentId": str(incident_id)}
        if comments:
            payload["comments"] = comments
        if status:
            status_ids = {"active": 0, "cleared": 2, "manually_cleared": 2, "system_cleared": 3}
            code = status_ids.get(status.strip().lower())
            if code is None:
                return (
                    "Error: status must be one of active, cleared "
                    "(alias: manually_cleared), system_cleared."
                )
            payload["incidentStatus"] = str(code)
        if external_ticket_id:
            payload["externalTicketId"] = external_ticket_id
        if external_ticket_state:
            payload["externalTicketState"] = external_ticket_state
        if external_assigned_user:
            payload["externalAssignedUser"] = external_assigned_user
        if len(payload) == 1:
            return "Error: No fields provided to update."
        async with egress_client(timeout=30) as client:
            _, err = await safe_request(
                client,
                "post",
                f"{api}/incident/external",
                _PROVIDER,
                f"update incident {incident_id}",
                headers={**headers, "Content-Type": "application/json"},
                json=payload,
            )
            if err:
                return err
            changed = ", ".join(k for k in payload if k != "incidentId")
            return f"Incident {incident_id} updated ({changed})."

    # ══════════════════════════════════════════════════════════════════════
    # 2. EVENT QUERY (threat hunting)
    # ══════════════════════════════════════════════════════════════════════

    async def _fetch_query_results(
        client: httpx.AsyncClient, query_id: str, attributes: str, limit: int
    ) -> str:
        resp, err = await safe_request(
            client,
            "get",
            f"{api}/query/events/{query_id}/0/{_clamp(limit, 50)}",
            _PROVIDER,
            f"get query results {query_id}",
            headers=headers,
        )
        if err:
            return err
        return _parse_event_results(resp.text, attributes, _clamp(limit, 50))

    async def run_fortisiem_event_query(
        query: str,
        attributes: str = _DEFAULT_EVENT_ATTRS,
        hours: int = 24,
        limit: int = 50,
    ) -> str:
        """Run a FortiSIEM event query (threat hunting) and wait for results.

        `query` is a single-event constraint, e.g.
        `eventSeverity >= 9 AND srcIpAddr = 10.0.0.5`.
        """
        if not has_creds:
            return _CREDS_ERROR
        try:
            hours = max(1, int(hours))
        except (TypeError, ValueError):
            hours = 24
        now_s = int(datetime.now(UTC).timestamp())
        xml_body = build_event_query_xml(
            query, attributes or _DEFAULT_EVENT_ATTRS, now_s - hours * 3600, now_s
        )
        async with egress_client(timeout=60) as client:
            resp, err = await safe_request(
                client,
                "post",
                f"{api}/query/eventQuery",
                _PROVIDER,
                "submit event query",
                headers={**headers, "Content-Type": "text/xml"},
                content=xml_body,
            )
            if err:
                return err
            query_id = (resp.text or "").strip()
            if not query_id or "error code=" in query_id:
                return (
                    f"Error: FortiSIEM rejected the event query: "
                    f"{_trunc(query_id, 200) or 'no query id returned'}. "
                    "Check the constraint syntax (e.g. eventSeverity >= 9)."
                )
            # The id is interpolated into the progress/results URL paths —
            # never trust even a server-returned value as a path segment.
            if not _QUERY_ID_RE.fullmatch(query_id):
                return (
                    "Error: FortiSIEM returned an unexpected event query id: "
                    f"{_trunc(query_id, 200)}."
                )

            for _ in range(_QUERY_MAX_POLLS):
                poll_resp, poll_err = await safe_request(
                    client,
                    "get",
                    f"{api}/query/progress/{query_id}",
                    _PROVIDER,
                    f"poll query {query_id}",
                    headers=headers,
                )
                if poll_err:
                    return poll_err
                if _progress_pct(poll_resp.text) >= 100:
                    break
                await asyncio.sleep(_QUERY_POLL_INTERVAL)
            else:
                return (
                    f"Event query {query_id} is still running. Use "
                    f"get_fortisiem_event_query_results with this id shortly."
                )

            results = await _fetch_query_results(
                client, query_id, attributes or _DEFAULT_EVENT_ATTRS, limit
            )
            return f"Event query {query_id}:\n{results}"

    async def get_fortisiem_event_query_results(
        query_id: str,
        attributes: str = _DEFAULT_EVENT_ATTRS,
        limit: int = 50,
    ) -> str:
        """Fetch results for an existing FortiSIEM event query by its query id."""
        if not has_creds:
            return _CREDS_ERROR
        query_id = (query_id or "").strip()
        if not _QUERY_ID_RE.fullmatch(query_id):
            return "Error: query_id must be the id returned by run_fortisiem_event_query."
        async with egress_client(timeout=60) as client:
            poll_resp, poll_err = await safe_request(
                client,
                "get",
                f"{api}/query/progress/{query_id}",
                _PROVIDER,
                f"poll query {query_id}",
                headers=headers,
            )
            if poll_err:
                return poll_err
            pct = _progress_pct(poll_resp.text)
            if pct < 100:
                return f"Event query {query_id} progress: {max(pct, 0)}% (not yet complete)."
            return await _fetch_query_results(
                client, query_id, attributes or _DEFAULT_EVENT_ATTRS, limit
            )

    # ══════════════════════════════════════════════════════════════════════
    # 3. CMDB (device context)
    # ══════════════════════════════════════════════════════════════════════

    async def list_fortisiem_devices(
        include_ips: str = "", exclude_ips: str = "", limit: int = 30
    ) -> str:
        """List CMDB devices, optionally restricted to an IP range (e.g. 10.0.0.1-10.0.0.100)."""
        if not has_creds:
            return _CREDS_ERROR
        params: dict[str, Any] = {}
        if include_ips:
            params["includeIps"] = include_ips
        if exclude_ips:
            params["excludeIps"] = exclude_ips
        async with egress_client(timeout=60) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{api}/cmdbDeviceInfo/devices",
                _PROVIDER,
                "list CMDB devices",
                headers={**headers, "Accept": "application/xml"},
                params=params or None,
            )
            if err:
                return err
            root = _xml_root(resp.text)
            if root is None:
                return "Error: FortiSIEM returned a non-XML or unparseable CMDB response."
            devices = list(root.iter("device"))
            if not devices:
                return "No CMDB devices found."
            max_rows = _clamp(limit, 30)
            lines = [f"Found {len(devices)} CMDB devices:"]
            for d in devices[:max_rows]:
                name = d.findtext("name") or "?"
                ip = d.findtext("accessIp") or "?"
                dtype = d.find("deviceType")
                vendor = dtype.findtext("vendor") if dtype is not None else ""
                model = dtype.findtext("model") if dtype is not None else ""
                lines.append(
                    f"  {name} — IP: {ip} | "
                    f"Type: {(vendor or '?')} {(model or '')}".rstrip()
                )
            if len(devices) > max_rows:
                lines.append(
                    f"  ... [{len(devices) - max_rows} more devices not shown — "
                    f"narrow with include_ips or raise limit]"
                )
            return "\n".join(lines)

    async def get_fortisiem_device(ip: str) -> str:
        """Get full CMDB details of one device by its access IP."""
        if not has_creds:
            return _CREDS_ERROR
        ip = (ip or "").strip()
        if not ip:
            return "Error: ip is required."
        async with egress_client(timeout=60) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{api}/cmdbDeviceInfo/device",
                _PROVIDER,
                f"get CMDB device {ip}",
                headers={**headers, "Accept": "application/xml"},
                params={"ip": ip, "loadDepend": "true"},
            )
            if err:
                return err
            root = _xml_root(resp.text)
            if root is None:
                return "Error: FortiSIEM returned a non-XML or unparseable CMDB response."
            device = root if root.tag == "device" else root.find(".//device")
            if device is None:
                return f"Device with IP {ip} not found in the CMDB."
            lines = [f"CMDB device {device.findtext('name') or ip}:"]
            for child in device:
                # Render simple scalar children; summarise nested sections.
                if len(child) == 0:
                    text = (child.text or "").strip()
                    if text:
                        lines.append(f"  {child.tag}: {_trunc(text, 160)}")
                else:
                    lines.append(f"  {child.tag}: [{len(child)} nested items]")
            return "\n".join(lines)

    # ══════════════════════════════════════════════════════════════════════
    # 4. WATCH LISTS (creating intelligence / IOC management)
    # ══════════════════════════════════════════════════════════════════════

    async def list_fortisiem_watchlists(limit: int = 25) -> str:
        """List FortiSIEM watch list groups (name, type, entry samples)."""
        if not has_creds:
            return _CREDS_ERROR
        async with egress_client(timeout=60) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{api}/watchlist/all",
                _PROVIDER,
                "list watch lists",
                headers=headers,
            )
            if err:
                return err
            payload, wl_err = _watchlist_payload(resp)
            if wl_err:
                return wl_err
            watchlists = payload if isinstance(payload, list) else []
            max_rows = _clamp(limit, 25)
            if not watchlists:
                return "No watch lists found."
            lines = [f"Found {len(watchlists)} watch lists:"]
            for wl in watchlists[:max_rows]:
                lines.append(_format_watchlist(wl, max_entries=3))
            if len(watchlists) > max_rows:
                lines.append(f"... [{len(watchlists) - max_rows} more watch lists not shown]")
            return "\n".join(lines)

    async def get_fortisiem_watchlist(watchlist_id: int) -> str:
        """Get one FortiSIEM watch list with its entries by watch list id."""
        if not has_creds:
            return _CREDS_ERROR
        watchlist_id, id_err = _parse_id(watchlist_id, "watchlist_id")
        if id_err:
            return id_err
        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{api}/watchlist/{watchlist_id}",
                _PROVIDER,
                f"get watch list {watchlist_id}",
                headers=headers,
            )
            if err:
                return err
            payload, wl_err = _watchlist_payload(resp)
            if wl_err:
                return wl_err
            watchlists = payload if isinstance(payload, list) else [payload]
            watchlists = [wl for wl in watchlists if isinstance(wl, dict)]
            if not watchlists:
                return f"Watch list {watchlist_id} not found."
            return "\n".join(_format_watchlist(wl, max_entries=50) for wl in watchlists)

    async def find_fortisiem_watchlist_entry(entry_value: str) -> str:
        """Find which FortiSIEM watch lists contain a given entry value (e.g. an IOC)."""
        if not has_creds:
            return _CREDS_ERROR
        entry_value = (entry_value or "").strip()
        if not entry_value:
            return "Error: entry_value is required."
        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{api}/watchlist/value",
                _PROVIDER,
                "find watch list entry",
                headers=headers,
                params={"entryValue": entry_value},
            )
            if err:
                return err
            payload, wl_err = _watchlist_payload(resp)
            if wl_err:
                # "No such Watch List with entry value" comes back as Failed.
                if "no such watch list" in wl_err.lower():
                    return f"'{entry_value}' is not on any watch list."
                return wl_err
            watchlists = payload if isinstance(payload, list) else [payload]
            watchlists = [wl for wl in watchlists if isinstance(wl, dict)]
            if not watchlists:
                return f"'{entry_value}' is not on any watch list."
            names = ", ".join(
                f"#{wl.get('id', '?')} {wl.get('displayName') or '(unnamed)'}"
                for wl in watchlists
            )
            return f"'{entry_value}' is on {len(watchlists)} watch list(s): {names}"

    async def add_fortisiem_watchlist_entries(
        watchlist_id: int,
        values: list[str],
        description: str = "",
        age_out: str = "",
    ) -> str:
        """Add entries (e.g. IOCs) to a FortiSIEM watch list so rules using it take effect."""
        if not has_creds:
            return _CREDS_ERROR
        watchlist_id, id_err = _parse_id(watchlist_id, "watchlist_id")
        if id_err:
            return id_err
        cleaned = [str(v).strip() for v in (values or []) if str(v).strip()]
        if not cleaned:
            return "Error: values must contain at least one non-empty entry value."
        skipped = max(0, len(cleaned) - 200)
        now_ms = int(datetime.now(UTC).timestamp() * 1000)
        entries: list[dict[str, Any]] = []
        for value in cleaned[:200]:
            entry: dict[str, Any] = {
                "entryValue": value,
                "dataCreationType": "USER",
                "inclusive": True,
                "firstSeen": now_ms,
                "lastSeen": now_ms,
            }
            if description:
                entry["description"] = description
            if age_out:
                entry["ageOut"] = age_out
            entries.append(entry)
        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "post",
                f"{api}/watchlist/addTo",
                _PROVIDER,
                f"add entries to watch list {watchlist_id}",
                headers={**headers, "Content-Type": "application/json"},
                params={"watchlistId": watchlist_id},
                json=entries,
            )
            if err:
                return err
            _, wl_err = _watchlist_payload(resp)
            if wl_err:
                return wl_err
            return (
                f"Added {len(entries)} entr{'y' if len(entries) == 1 else 'ies'} "
                f"to watch list {watchlist_id}: {', '.join(cleaned[:10])}"
                + (" ..." if len(cleaned) > 10 else "")
                + (
                    f"\n[{skipped} values beyond the 200-per-call cap were NOT "
                    "added — call again with the remainder.]"
                    if skipped
                    else ""
                )
            )

    async def manage_fortisiem_watchlist(
        action: str,
        watchlist_id: int = 0,
        entry_id: int = 0,
        name: str = "",
        description: str = "",
        age_out: str = "",
        state: bool = True,
    ) -> str:
        """Manage FortiSIEM watch lists: create, set_entry_state, delete_entry, or delete_watchlist."""
        if not has_creds:
            return _CREDS_ERROR
        action = (action or "").strip().lower()
        if action not in _VALID_WATCHLIST_ACTIONS:
            return (
                "Error: action must be one of "
                f"{', '.join(sorted(_VALID_WATCHLIST_ACTIONS))}."
            )
        async with egress_client(timeout=30) as client:
            if action == "create":
                if not name:
                    return "Error: name is required to create a watch list."
                body: dict[str, Any] = {
                    "displayName": name,
                    "description": description,
                    "type": "DyWatchList",
                    "isCaseSensitive": False,
                    "dataCreationType": "USER",
                    "topGroup": False,
                    "valuePattern": None,
                    "valueType": "STRING",
                    "entries": [],
                }
                if age_out:
                    body["ageOut"] = age_out
                resp, err = await safe_request(
                    client,
                    "post",
                    f"{api}/watchlist/save",
                    _PROVIDER,
                    f"create watch list {name}",
                    headers={**headers, "Content-Type": "application/json"},
                    json=body,
                )
                if err:
                    return err
                payload, wl_err = _watchlist_payload(resp)
                if wl_err:
                    return wl_err
                created = payload[0] if isinstance(payload, list) and payload else {}
                return (
                    f"Watch list '{name}' created"
                    f"{' with id ' + str(created.get('id')) if isinstance(created, dict) and created.get('id') else ''}."
                )
            if action == "set_entry_state":
                if not entry_id:
                    return "Error: entry_id is required for set_entry_state."
                entry_id, id_err = _parse_id(entry_id, "entry_id")
                if id_err:
                    return id_err
                desired = _to_bool(state)
                resp, err = await safe_request(
                    client,
                    "post",
                    f"{api}/watchlist/entry/active/{entry_id}",
                    _PROVIDER,
                    f"set watch list entry {entry_id} state",
                    headers=headers,
                    params={"state": str(desired).lower()},
                )
                if err:
                    return err
                _, wl_err = _watchlist_payload(resp)
                if wl_err:
                    return wl_err
                return (
                    f"Watch list entry {entry_id} is now "
                    f"{'enabled' if desired else 'disabled'}."
                )
            if action == "delete_entry":
                if not entry_id:
                    return "Error: entry_id is required for delete_entry."
                entry_id, id_err = _parse_id(entry_id, "entry_id")
                if id_err:
                    return id_err
                resp, err = await safe_request(
                    client,
                    "post",
                    f"{api}/watchlist/entry/delete",
                    _PROVIDER,
                    f"delete watch list entry {entry_id}",
                    headers={**headers, "Content-Type": "application/json"},
                    json=[entry_id],
                )
                if err:
                    return err
                _, wl_err = _watchlist_payload(resp)
                if wl_err:
                    return wl_err
                return f"Watch list entry {entry_id} deleted (permanent)."
            # action == "delete_watchlist"
            if not watchlist_id:
                return "Error: watchlist_id is required for delete_watchlist."
            watchlist_id, id_err = _parse_id(watchlist_id, "watchlist_id")
            if id_err:
                return id_err
            resp, err = await safe_request(
                client,
                "post",
                f"{api}/watchlist/delete",
                _PROVIDER,
                f"delete watch list {watchlist_id}",
                headers={**headers, "Content-Type": "application/json"},
                json=[watchlist_id],
            )
            if err:
                return err
            _, wl_err = _watchlist_payload(resp)
            if wl_err:
                return wl_err
            return f"Watch list {watchlist_id} deleted (permanent)."

    # ══════════════════════════════════════════════════════════════════════
    # 5. LOOKUP TABLES (enrichment data)
    # ══════════════════════════════════════════════════════════════════════

    async def list_fortisiem_lookup_tables(limit: int = 25, start: int = 0) -> str:
        """List FortiSIEM lookup tables (name, columns)."""
        if not has_creds:
            return _CREDS_ERROR
        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{api}/pub/lookupTable",
                _PROVIDER,
                "list lookup tables",
                headers=headers,
                params={"size": _clamp(limit, 25), "start": _offset(start)},
            )
            if err:
                return err
            try:
                data = resp.json() or {}
            except Exception:
                return "Error: FortiSIEM returned a non-JSON lookup-table response."
            tables = data.get("data") or []

            def _fmt(t: dict) -> str:
                columns = ", ".join(
                    f"{c.get('name', '?')}({c.get('type', '?')})"
                    + ("*" if c.get("key") else "")
                    for c in (t.get("columnList") or [])[:10]
                )
                return (
                    f"  table #{t.get('id', '?')} {t.get('name', 'Unnamed')} "
                    f"(org: {t.get('organizationName') or '?'})\n"
                    f"    Columns (* = key): {columns or 'N/A'}"
                )

            listing = format_entity_list(tables, _fmt, noun="lookup table")
            total = data.get("total")
            if isinstance(total, int) and total > len(tables) + _offset(start):
                listing += f"\n[{total} tables in total — pass start to page further.]"
            return listing

    async def get_fortisiem_lookup_table_data(
        table_id: int,
        search_text: str = "",
        limit: int = 25,
        start: int = 0,
    ) -> str:
        """Query rows of a FortiSIEM lookup table (optionally filtered by search text)."""
        if not has_creds:
            return _CREDS_ERROR
        table_id, id_err = _parse_id(table_id, "table_id")
        if id_err:
            return id_err
        params: dict[str, Any] = {
            "size": _clamp(limit, 25),
            "start": _offset(start),
        }
        if search_text:
            params["searchText"] = search_text
        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{api}/pub/lookupTable/{table_id}/data",
                _PROVIDER,
                f"get lookup table {table_id} data",
                headers=headers,
                params=params,
            )
            if err:
                return err
            try:
                data = resp.json() or {}
            except Exception:
                return "Error: FortiSIEM returned a non-JSON lookup-table response."
            rows = data.get("data") or []
            if not rows:
                return f"Lookup table {table_id}: 0 matching rows."
            columns = list(rows[0].keys())
            lines = [
                f"Lookup table {table_id} rows ({len(rows)} shown, "
                f"total {data.get('total', '?')}):",
                " | ".join(columns),
                "-" * min(len(" | ".join(columns)), 120),
            ]
            for row in rows:
                lines.append(" | ".join(_cell(row.get(c)) for c in columns))
            return "\n".join(lines)

    # ══════════════════════════════════════════════════════════════════════
    # REGISTER ALL TOOLS
    # ══════════════════════════════════════════════════════════════════════

    executor.register("get_fortisiem_status", get_fortisiem_status)
    executor.register("list_fortisiem_incidents", list_fortisiem_incidents)
    executor.register("get_fortisiem_incident", get_fortisiem_incident)
    executor.register("get_fortisiem_incident_events", get_fortisiem_incident_events)
    executor.register("update_fortisiem_incident", update_fortisiem_incident)
    executor.register("run_fortisiem_event_query", run_fortisiem_event_query)
    executor.register(
        "get_fortisiem_event_query_results", get_fortisiem_event_query_results
    )
    executor.register("list_fortisiem_devices", list_fortisiem_devices)
    executor.register("get_fortisiem_device", get_fortisiem_device)
    executor.register("list_fortisiem_watchlists", list_fortisiem_watchlists)
    executor.register("get_fortisiem_watchlist", get_fortisiem_watchlist)
    executor.register("find_fortisiem_watchlist_entry", find_fortisiem_watchlist_entry)
    executor.register("add_fortisiem_watchlist_entries", add_fortisiem_watchlist_entries)
    executor.register("manage_fortisiem_watchlist", manage_fortisiem_watchlist)
    executor.register("list_fortisiem_lookup_tables", list_fortisiem_lookup_tables)
    executor.register(
        "get_fortisiem_lookup_table_data", get_fortisiem_lookup_table_data
    )

    return [
        # ── 0. Connection ──────────────────────────────────────────────────
        ToolDefinition(
            name="get_fortisiem_status",
            description=(
                "Validate the Fortinet FortiSIEM connection and report "
                "reachability. Use first if other FortiSIEM tools fail."
            ),
            input_schema={"type": "object", "properties": {}, "required": []},
            source=_SOURCE,
            priority=2,
        ),
        # ── 1. Incidents ───────────────────────────────────────────────────
        ToolDefinition(
            name="list_fortisiem_incidents",
            description=(
                "List FortiSIEM incidents in a recent time window (gathering "
                "incidents). Filter by status (active/auto_cleared/"
                "manually_cleared/system_cleared), category (availability/"
                "performance/change/security/other), resolution (none/open/"
                "true_positive/false_positive/in_progress), or min_severity "
                "(0-10). Sorted by last-seen descending."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "hours": {
                        "type": "integer",
                        "description": "Look-back window in hours",
                        "default": 24,
                    },
                    "status": {
                        "type": "string",
                        "enum": [
                            "active",
                            "auto_cleared",
                            "manually_cleared",
                            "system_cleared",
                        ],
                    },
                    "category": {
                        "type": "string",
                        "enum": [
                            "availability",
                            "performance",
                            "change",
                            "security",
                            "other",
                        ],
                    },
                    "resolution": {
                        "type": "string",
                        "enum": [
                            "none",
                            "open",
                            "true_positive",
                            "false_positive",
                            "in_progress",
                        ],
                    },
                    "min_severity": {
                        "type": "integer",
                        "description": "Minimum event severity (0-10)",
                        "default": 0,
                    },
                    "limit": {"type": "integer", "default": 25},
                    "start": {
                        "type": "integer",
                        "description": "Pagination offset",
                        "default": 0,
                    },
                },
                "required": [],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="get_fortisiem_incident",
            description=(
                "Get full details of a FortiSIEM incident by id (severity, "
                "status, resolution, source/target, MITRE ATT&CK mapping, "
                "comments, external ticket)."
            ),
            input_schema={
                "type": "object",
                "properties": {"incident_id": {"type": "integer"}},
                "required": ["incident_id"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="get_fortisiem_incident_events",
            description=(
                "Get the triggering events (raw log messages) behind a "
                "FortiSIEM incident — the evidence for the investigation."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "incident_id": {"type": "integer"},
                    "limit": {"type": "integer", "default": 10},
                },
                "required": ["incident_id"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="update_fortisiem_incident",
            description=(
                "Update a FortiSIEM incident: add analyst comments, clear it "
                "(status: active/cleared/system_cleared; manually_cleared is "
                "an alias for cleared), or sync external ticket fields (id, "
                "state, assigned user)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "incident_id": {"type": "integer"},
                    "comments": {"type": "string"},
                    "status": {
                        "type": "string",
                        "enum": [
                            "active",
                            "cleared",
                            "manually_cleared",
                            "system_cleared",
                        ],
                    },
                    "external_ticket_id": {"type": "string"},
                    "external_ticket_state": {
                        "type": "string",
                        "description": "e.g. New, Assigned, In Progress, Closed",
                    },
                    "external_assigned_user": {"type": "string"},
                },
                "required": ["incident_id"],
            },
            source=_SOURCE,
            priority=1,
        ),
        # ── 2. Event query (threat hunting) ────────────────────────────────
        ToolDefinition(
            name="run_fortisiem_event_query",
            description=(
                "Run a FortiSIEM event query for threat hunting and wait for "
                "results. `query` is a single-event constraint, e.g. "
                "'eventSeverity >= 9 AND srcIpAddr = 10.0.0.5' or "
                "'eventType CONTAIN \"Logon_Failure\"'. `attributes` is the "
                "comma-separated attribute list to return."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "FortiSIEM event constraint",
                    },
                    "attributes": {
                        "type": "string",
                        "description": "Comma-separated event attributes to return",
                        "default": _DEFAULT_EVENT_ATTRS,
                    },
                    "hours": {
                        "type": "integer",
                        "description": "Look-back window in hours",
                        "default": 24,
                    },
                    "limit": {"type": "integer", "default": 50},
                },
                "required": ["query"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="get_fortisiem_event_query_results",
            description=(
                "Fetch results for an existing FortiSIEM event query by id "
                "(use when run_fortisiem_event_query was still running)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "query_id": {"type": "string"},
                    "attributes": {
                        "type": "string",
                        "description": "Comma-separated attributes (same as the original query)",
                        "default": _DEFAULT_EVENT_ATTRS,
                    },
                    "limit": {"type": "integer", "default": 50},
                },
                "required": ["query_id"],
            },
            source=_SOURCE,
            priority=2,
        ),
        # ── 3. CMDB ────────────────────────────────────────────────────────
        ToolDefinition(
            name="list_fortisiem_devices",
            description=(
                "List FortiSIEM CMDB devices (name, IP, vendor/model). "
                "Optionally restrict to IP ranges like '10.0.0.1-10.0.0.100' "
                "or comma-separated addresses."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "include_ips": {
                        "type": "string",
                        "description": "IPs/ranges to include (e.g. 10.0.0.1-10.0.0.100)",
                    },
                    "exclude_ips": {
                        "type": "string",
                        "description": "IPs/ranges to exclude",
                    },
                    "limit": {"type": "integer", "default": 30},
                },
                "required": [],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="get_fortisiem_device",
            description="Get full CMDB details of one FortiSIEM device by its access IP address.",
            input_schema={
                "type": "object",
                "properties": {
                    "ip": {"type": "string", "description": "Device access IP"}
                },
                "required": ["ip"],
            },
            source=_SOURCE,
            priority=1,
        ),
        # ── 4. Watch lists (creating intelligence) ─────────────────────────
        ToolDefinition(
            name="list_fortisiem_watchlists",
            description=(
                "List FortiSIEM watch list groups (dynamic IOC/value "
                "collections referenced by rules), with a sample of entries."
            ),
            input_schema={
                "type": "object",
                "properties": {"limit": {"type": "integer", "default": 25}},
                "required": [],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="get_fortisiem_watchlist",
            description="Get one FortiSIEM watch list with its entries by watch list id.",
            input_schema={
                "type": "object",
                "properties": {"watchlist_id": {"type": "integer"}},
                "required": ["watchlist_id"],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="find_fortisiem_watchlist_entry",
            description=(
                "Check whether a value (e.g. an IP/domain/hash IOC) is already "
                "on any FortiSIEM watch list, and which one(s)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "entry_value": {
                        "type": "string",
                        "description": "Exact entry value to look up",
                    }
                },
                "required": ["entry_value"],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="add_fortisiem_watchlist_entries",
            description=(
                "Add entries (e.g. malicious IP/domain/hash IOCs) to a "
                "FortiSIEM watch list so rules acting on that list take effect "
                "(CREATING INTELLIGENCE). Use list_fortisiem_watchlists to "
                "find the watchlist_id."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "watchlist_id": {"type": "integer"},
                    "values": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Entry values / IOCs to add",
                    },
                    "description": {"type": "string"},
                    "age_out": {
                        "type": "string",
                        "description": "Expiry, e.g. 1d, 1w (blank = watch list default)",
                    },
                },
                "required": ["watchlist_id", "values"],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="manage_fortisiem_watchlist",
            description=(
                "Manage FortiSIEM watch lists (long-tail operations): 'create' "
                "a new watch list (name required), 'set_entry_state' to "
                "enable/disable an entry (entry_id + state), 'delete_entry' "
                "(entry_id) or 'delete_watchlist' (watchlist_id) — deletions "
                "are permanent and cannot be undone."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": [
                            "create",
                            "set_entry_state",
                            "delete_entry",
                            "delete_watchlist",
                        ],
                    },
                    "watchlist_id": {"type": "integer"},
                    "entry_id": {"type": "integer"},
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                    "age_out": {"type": "string", "description": "e.g. 1d, 1w"},
                    "state": {
                        "type": "boolean",
                        "description": "For set_entry_state: true = enabled",
                        "default": True,
                    },
                },
                "required": ["action"],
            },
            source=_SOURCE,
            priority=2,
        ),
        # ── 5. Lookup tables ───────────────────────────────────────────────
        ToolDefinition(
            name="list_fortisiem_lookup_tables",
            description="List FortiSIEM lookup tables (enrichment data sets) with their column definitions.",
            input_schema={
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "default": 25},
                    "start": {"type": "integer", "default": 0},
                },
                "required": [],
            },
            source=_SOURCE,
            priority=2,
        ),
        ToolDefinition(
            name="get_fortisiem_lookup_table_data",
            description=(
                "Query rows of a FortiSIEM lookup table, optionally filtered "
                "by search text. Use list_fortisiem_lookup_tables to find the "
                "table_id."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "table_id": {"type": "integer"},
                    "search_text": {"type": "string"},
                    "limit": {"type": "integer", "default": 25},
                    "start": {"type": "integer", "default": 0},
                },
                "required": ["table_id"],
            },
            source=_SOURCE,
            priority=2,
        ),
    ]
