# SPDX-License-Identifier: Apache-2.0

"""LogRhythm SIEM integration - Agentic SOC for the LogRhythm platform.

Full-fledged LogRhythm SIEM integration covering the detection ->
investigation -> response loop for an agentic security team, built on the
LogRhythm REST APIs exposed by the Platform Manager API gateway:

  - Cases (*incident management*): list (filterable), get, create, update,
    change status, and add an investigation note (evidence)
  - Alarms (*detection triage*): list, get detail, update status + Risk Based
    Priority (RBP), add a comment, and drill into the contributing events
  - Search (*log/event hunting*): submit an indexed search task, poll it, and
    return the matching logs (async request -> poll -> results, bounded like
    QRadar's AQL flow)
  - Context (*admin*): list hosts and entities for asset/segmentation context
  - Connectivity / status

Authentication
--------------
LogRhythm authenticates with a **third-party API token** (a JSON Web Token
generated in the Client Console) sent in the standard
``Authorization: Bearer <api_token>`` HTTP header. The LogRhythm REST APIs are
served by the Platform Manager API gateway over HTTPS on port ``8501``, so the
stored base URL is the gateway host (e.g. ``https://logrhythm-pm.example.com:8501``).
``integration_tools._get_token`` assembles the compound ``base_url||api_token``
token this builder parses; every call targets ``{base_url}/lr-<service>-api/...``
(``lr-case-api``, ``lr-alarm-api``, ``lr-search-api``, ``lr-admin-api``).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from app.core.egress import egress_client
from app.integrations.http import (
    bearer_auth_headers,
    format_entity_list,
    parse_compound_token,
    safe_request,
)
from app.tools.base import CallableToolExecutor, ToolDefinition

logger = logging.getLogger(__name__)

_PROVIDER = "LogRhythm"
_SOURCE = "integration:logrhythm"

# Search polling budget (submit search task -> poll search result -> read items).
# Bounded so a slow/large hunt can't block the agentic loop indefinitely.
_SEARCH_POLL_INTERVAL = 3.0
_SEARCH_MAX_POLLS = 10

# Default request timeout (seconds); searches get a longer ceiling.
_DEFAULT_TIMEOUT = 30
_SEARCH_TIMEOUT = 60

# Case status: numeric identifier <-> display name (LogRhythm Case workflow).
_CASE_STATUS_NAMES: dict[int, str] = {
    1: "Created",
    2: "Completed",
    3: "Incident",
    4: "Mitigated",
    5: "Resolved",
}
_CASE_STATUS_NUMBERS: dict[str, int] = {v.lower(): k for k, v in _CASE_STATUS_NAMES.items()}

# Case priority (1 = highest). Used for display icons and create/update validation.
_PRIORITY_ICONS: dict[int, str] = {1: "🔴", 2: "🟠", 3: "🟡", 4: "🔵", 5: "⚪"}

# Alarm status: numeric identifier <-> canonical name (LogRhythm Alarm API).
_ALARM_STATUS_NAMES: dict[int, str] = {
    0: "New",
    1: "Opened",
    2: "Working",
    3: "Escalated",
    4: "Closed",
    5: "Closed_FalseAlarm",
    6: "Closed_Resolved",
    7: "Closed_Unresolved",
    8: "Closed_Reported",
    9: "Closed_Monitor",
}
_ALARM_STATUS_NUMBERS: dict[str, int] = {
    v.lower(): k for k, v in _ALARM_STATUS_NAMES.items()
}


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def _trunc(text: Any, max_len: int = 300) -> str:
    if text is None:
        return ""
    text = str(text).replace("\r", " ").replace("\n", " ").strip()
    return text[:max_len] + ("…" if len(text) > max_len else "")


def _dt(value: Any) -> str:
    """Trim an ISO-8601 timestamp to second precision for compact display."""
    return str(value or "")[:19]


def _safe_int(value: Any, default: int, *, lo: int, hi: int | None = None) -> int:
    """Coerce a (possibly string/None) tool argument to a clamped int.

    Tool arguments are not schema-validated at runtime - an LLM may pass a
    numeric string, a float, or ``None`` - so any ``int()`` applied to a
    caller-supplied value must never raise. Non-numeric input and non-positive
    values fall back to *default* (matching the historical ``int(x or default)``
    idiom); the result is then floored at *lo* and (when given) capped at *hi*.
    """
    try:
        result = int(value)
    except (TypeError, ValueError):
        result = default
    if result <= 0:
        result = default
    result = max(lo, result)
    if hi is not None:
        result = min(result, hi)
    return result


def _priority_icon(value: Any) -> str:
    try:
        return _PRIORITY_ICONS.get(int(value), "⚪")
    except (TypeError, ValueError):
        return "⚪"


def _case_status_text(status: Any) -> str:
    """Render a case ``status`` field, which the API returns as ``{name, number}``."""
    if isinstance(status, dict):
        return str(status.get("name") or _CASE_STATUS_NAMES.get(status.get("number"), "?"))
    try:
        return _CASE_STATUS_NAMES.get(int(status), str(status))
    except (TypeError, ValueError):
        return str(status or "?")


def _entity_name(entity: Any) -> str:
    if isinstance(entity, dict):
        return str(entity.get("fullName") or entity.get("name") or entity.get("number") or "?")
    return str(entity or "")


def _as_list(payload: Any, *keys: str) -> list:
    """Return a list from a LogRhythm response.

    Several endpoints return a bare JSON array (e.g. ``GET /cases``) while
    others wrap the array in an envelope (e.g. ``{"alarmsSearchDetails": [...]}``).
    Returns the payload itself when it is already a list, otherwise the first
    matching envelope key that holds a list, otherwise an empty list.
    """
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in keys:
            value = payload.get(key)
            if isinstance(value, list):
                return value
    return []


def _envelope(payload: Any, *keys: str) -> dict:
    """Return the first dict envelope value (e.g. ``alarmDetails``) or the payload."""
    if isinstance(payload, dict):
        for key in keys:
            value = payload.get(key)
            if isinstance(value, dict):
                return value
        return payload
    return {}


def _resolve_alarm_status(value: Any) -> tuple[str, str | None]:
    """Map a user-supplied alarm status (name or number) to its canonical name.

    Returns ``(canonical_name, error)``; ``error`` is non-None when the input
    is not a valid LogRhythm alarm status. ``value`` is coerced to a string
    first because tool arguments are not runtime schema-validated - an LLM may
    pass the status number as an int (e.g. ``0`` for New), which would raise
    ``AttributeError`` on ``.strip()``.
    """
    raw = ("" if value is None else str(value)).strip()
    if not raw:
        return "", "Error: an alarm status is required."
    if raw.isdigit():
        name = _ALARM_STATUS_NAMES.get(int(raw))
        if name is None:
            return "", (
                f"Error: invalid alarm status number '{raw}'. Valid: "
                + ", ".join(f"{k}={v}" for k, v in _ALARM_STATUS_NAMES.items())
            )
        return name, None
    key = raw.lower()
    if key in _ALARM_STATUS_NUMBERS:
        return _ALARM_STATUS_NAMES[_ALARM_STATUS_NUMBERS[key]], None
    return "", (
        f"Error: invalid alarm status '{raw}'. Valid: "
        + ", ".join(_ALARM_STATUS_NAMES.values())
    )


def _resolve_case_status_number(value: Any) -> tuple[int, str | None]:
    """Map a user-supplied case status (name or number 1-5) to its number.

    ``value`` is coerced to a string first because tool arguments are not
    runtime schema-validated - an LLM may pass the status number as an int,
    which would raise ``AttributeError`` on ``.strip()``.
    """
    raw = ("" if value is None else str(value)).strip()
    if not raw:
        return 0, "Error: a case status is required."
    if raw.isdigit():
        num = int(raw)
        if num not in _CASE_STATUS_NAMES:
            return 0, (
                f"Error: invalid case status number '{raw}'. Valid: "
                + ", ".join(f"{k}={v}" for k, v in _CASE_STATUS_NAMES.items())
            )
        return num, None
    key = raw.lower()
    if key in _CASE_STATUS_NUMBERS:
        return _CASE_STATUS_NUMBERS[key], None
    return 0, (
        f"Error: invalid case status '{raw}'. Valid: "
        + ", ".join(_CASE_STATUS_NAMES.values())
    )


# ---------------------------------------------------------------------------
# Auth + URL helpers (shared with integration_testing.py)
# ---------------------------------------------------------------------------


def logrhythm_api_base(base_url: str) -> str:
    """Return the LogRhythm API gateway base URL (the configured host, no slash).

    The REST services live at ``{base}/lr-case-api``, ``{base}/lr-alarm-api``,
    ``{base}/lr-search-api`` and ``{base}/lr-admin-api`` - typically the
    Platform Manager host on port 8501 (e.g. ``https://logrhythm-pm:8501``).
    Surrounding whitespace is trimmed (a pasted URL often carries trailing
    spaces) so the gateway URL can never carry an illegal leading/trailing
    space into a request.
    """
    return (base_url or "").strip().rstrip("/")


def build_logrhythm_headers(api_token: str) -> dict[str, str]:
    """Build LogRhythm request headers (Bearer API token).

    Routes the Bearer header through the shared ``bearer_auth_headers`` guard,
    which strips the token and raises ``EmptyBearerTokenError`` on a blank /
    whitespace-only value. This prevents an illegal ``Authorization: Bearer ``
    header (which httpx/h11 reject with an opaque ``LocalProtocolError``) from
    ever being constructed when a credential is missing or mis-pasted.
    """
    return bearer_auth_headers(
        api_token,
        extra={"Accept": "application/json", "Content-Type": "application/json"},
    )


# ---------------------------------------------------------------------------
# Tool builder
# ---------------------------------------------------------------------------


def build_tools(
    token: str,
    executor: CallableToolExecutor,
    **_kwargs: Any,
) -> list[ToolDefinition]:
    """Build LogRhythm tools from a ``base_url||api_token`` compound token."""
    base_url, api_token = parse_compound_token(token, 2)
    base_url = logrhythm_api_base(base_url)
    # parse_compound_token can yield whitespace-only segments (a pasted
    # credential with trailing spaces); strip so a blank token degrades to "no
    # tools" rather than an illegal Bearer header at request time.
    api_token = api_token.strip()
    if not base_url or not api_token:
        return []

    headers = build_logrhythm_headers(api_token)

    async def _call(
        method: str,
        path: str,
        action: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any | None = None,
        extra_headers: dict[str, str] | None = None,
        timeout: int = _DEFAULT_TIMEOUT,
    ) -> tuple[Any, str | None]:
        """Authenticated request helper returning ``(parsed_json, error)``.

        Routes every call through the shared ``safe_request`` (standardized
        errors + automatic 429 retry). ``path`` is appended to the gateway
        base URL (e.g. ``/lr-case-api/cases``).
        """
        req_headers = {**headers, **extra_headers} if extra_headers else headers
        async with egress_client(timeout=timeout) as client:
            resp, err = await safe_request(
                client,
                method,
                f"{base_url}{path}",
                _PROVIDER,
                action,
                headers=req_headers,
                params=params,
                json=json,
            )
            if err:
                return None, err
            try:
                return resp.json(), None
            except Exception:
                # An empty body (e.g. 204 No Content, or an empty 200 from a
                # mutation) is a legitimate "no JSON" success -> {}. A non-empty
                # body that will not parse as JSON (e.g. an HTML auth/error page
                # served behind a 200) is a real failure - surface it instead of
                # silently returning empty results that downstream formatting
                # would render as "nothing found".
                if not (resp.content or b"").strip():
                    return {}, None
                return None, (
                    f"Error: {_PROVIDER} returned a non-JSON response while "
                    f"trying to {action} (HTTP {resp.status_code}). The base URL "
                    f"may point at a non-API endpoint or an auth gateway."
                )

    def _page_headers(count: int, offset: int = 0) -> dict[str, str]:
        """LogRhythm Case/Admin APIs paginate via ``count``/``offset`` headers."""
        return {
            "count": str(_safe_int(count, 25, lo=1, hi=1000)),
            "offset": str(_safe_int(offset, 0, lo=0)),
        }

    # ══════════════════════════════════════════════════════════════════════
    # 0. CONNECTION
    # ══════════════════════════════════════════════════════════════════════

    async def get_logrhythm_status() -> str:
        """Validate the LogRhythm connection and report reachable context."""
        data, err = await _call(
            "get",
            "/lr-admin-api/entities",
            "get entities",
            extra_headers=_page_headers(5),
        )
        if err:
            return err
        entities = _as_list(data, "entities")
        names = ", ".join(_entity_name(e) for e in entities[:5]) or "none visible"
        return (
            f"Connected to LogRhythm at {base_url}.\n"
            f"  Reachable: lr-admin-api | Entities (sample): {names}"
        )

    # ══════════════════════════════════════════════════════════════════════
    # 1. CASES (incident management)
    # ══════════════════════════════════════════════════════════════════════

    async def list_logrhythm_cases(
        status: str = "",
        priority: int = 0,
        text: str = "",
        limit: int = 25,
    ) -> str:
        """List LogRhythm cases. Filter by status (Created/Completed/Incident/Mitigated/Resolved or 1-5), priority (1-5), or a text search on name/number."""
        params: dict[str, Any] = {"orderBy": "dateCreated", "direction": "desc"}
        if status:
            num, serr = _resolve_case_status_number(status)
            if serr:
                return serr
            params["statusNumber"] = num
        # priority may arrive as an int or a numeric string; 0 / "0" / "" is the
        # schema default meaning "no priority filter", so coerce first and only
        # validate/apply a non-zero value (a truthy "0" string must not error).
        try:
            prio = int(priority or 0)
        except (TypeError, ValueError):
            return "Error: priority filter must be an integer 1 (highest) to 5."
        if prio:
            if prio not in _PRIORITY_ICONS:
                return "Error: priority filter must be an integer 1 (highest) to 5."
            params["priority"] = prio
        if text:
            params["text"] = text

        data, err = await _call(
            "get",
            "/lr-case-api/cases",
            "list cases",
            params=params,
            extra_headers=_page_headers(limit),
        )
        if err:
            return err
        cases = _as_list(data, "cases")
        shown = cases[: _safe_int(limit, 25, lo=1, hi=1000)]

        def _fmt(c: dict) -> str:
            return (
                f"  {_priority_icon(c.get('priority'))} case {c.get('number', c.get('id', '?'))} "
                f"[{_case_status_text(c.get('status'))}] - {_trunc(c.get('name'), 90)}\n"
                f"    id: {c.get('id', '?')} | Priority: {c.get('priority', '?')} | "
                f"Entity: {_entity_name(c.get('entity'))} | "
                f"Owner: {(c.get('owner') or {}).get('name', 'Unassigned') if isinstance(c.get('owner'), dict) else 'Unassigned'}\n"
                f"    Created: {_dt(c.get('dateCreated'))} | Updated: {_dt(c.get('dateUpdated'))}"
            )

        return format_entity_list(shown, _fmt, noun="case", total=len(cases))

    async def get_logrhythm_case(case_id: str) -> str:
        """Get full detail of a LogRhythm case by its id (number or RFC 4122 string)."""
        data, err = await _call(
            "get", f"/lr-case-api/cases/{case_id}", f"get case {case_id}"
        )
        if err:
            return err
        c = data if isinstance(data, dict) else {}
        collaborators = ", ".join(
            (p.get("name") or "?")
            for p in (c.get("collaborators") or [])
            if isinstance(p, dict)
        )
        tags = ", ".join(
            (t.get("text") or "?")
            for t in (c.get("tags") or [])
            if isinstance(t, dict)
        )
        lines = [
            f"Case {c.get('number', case_id)}: {_trunc(c.get('name'), 200)}",
            f"id: {c.get('id', case_id)} | "
            f"Status: {_case_status_text(c.get('status'))} | "
            f"Priority: {_priority_icon(c.get('priority'))} {c.get('priority', '?')}",
            f"Entity: {_entity_name(c.get('entity'))} | "
            f"Owner: {(c.get('owner') or {}).get('name', 'Unassigned') if isinstance(c.get('owner'), dict) else 'Unassigned'}",
            f"Created: {_dt(c.get('dateCreated'))} | Updated: {_dt(c.get('dateUpdated'))} | "
            f"Due: {_dt(c.get('dueDate'))} | Closed: {_dt(c.get('dateClosed')) or 'N/A'}",
        ]
        if c.get("externalId"):
            lines.append(f"External id: {c.get('externalId')}")
        if collaborators:
            lines.append(f"Collaborators: {collaborators}")
        if tags:
            lines.append(f"Tags: {tags}")
        if c.get("summary"):
            lines.append(f"\nSummary: {_trunc(c.get('summary'), 600)}")
        if c.get("resolution"):
            lines.append(f"\nResolution: {_trunc(c.get('resolution'), 400)}")
        return "\n".join(lines)

    async def create_logrhythm_case(
        name: str,
        priority: int = 3,
        summary: str = "",
        due_date: str = "",
    ) -> str:
        """Create a LogRhythm case. priority is 1 (highest) to 5; summary is optional; due_date is an RFC 3339 timestamp."""
        if not name or not name.strip():
            return "Error: a case name is required."
        try:
            prio = int(priority)
        except (TypeError, ValueError):
            prio = 3
        if prio not in _PRIORITY_ICONS:
            return "Error: priority must be an integer 1 (highest) to 5."
        body: dict[str, Any] = {"name": name.strip(), "priority": prio}
        if summary:
            body["summary"] = summary
        if due_date:
            body["dueDate"] = due_date

        data, err = await _call(
            "post", "/lr-case-api/cases", "create case", json=body
        )
        if err:
            return err
        c = data if isinstance(data, dict) else {}
        return (
            f"Case created: #{c.get('number', '?')} '{c.get('name', name)}' "
            f"(id: {c.get('id', '?')}, status: {_case_status_text(c.get('status'))}, "
            f"priority: {c.get('priority', prio)})."
        )

    async def update_logrhythm_case(
        case_id: str,
        name: str = "",
        priority: int = 0,
        summary: str = "",
        resolution: str = "",
        due_date: str = "",
    ) -> str:
        """Update a LogRhythm case's name, priority (1-5), summary, resolution, or due date. Use update_logrhythm_case_status to change the workflow status."""
        body: dict[str, Any] = {}
        if name:
            stripped_name = name.strip()
            if not stripped_name:
                return "Error: case name cannot be blank."
            body["name"] = stripped_name
        # 0 / "0" / "" means "leave the priority unchanged" (schema default), so
        # coerce first and only validate/apply a non-zero value (a truthy "0"
        # string must not error).
        try:
            prio = int(priority or 0)
        except (TypeError, ValueError):
            return "Error: priority must be an integer 1 (highest) to 5."
        if prio:
            if prio not in _PRIORITY_ICONS:
                return "Error: priority must be an integer 1 (highest) to 5."
            body["priority"] = prio
        if summary:
            body["summary"] = summary
        if resolution:
            body["resolution"] = resolution
        if due_date:
            body["dueDate"] = due_date
        if not body:
            return "Error: no fields provided to update."

        data, err = await _call(
            "put", f"/lr-case-api/cases/{case_id}", f"update case {case_id}", json=body
        )
        if err:
            return err
        c = data if isinstance(data, dict) else {}
        return (
            f"Case {c.get('number', case_id)} updated - "
            f"status: {_case_status_text(c.get('status'))}, "
            f"priority: {c.get('priority', '?')}."
        )

    async def update_logrhythm_case_status(case_id: str, status: str) -> str:
        """Change a LogRhythm case's workflow status: Created, Completed, Incident, Mitigated, or Resolved (or the number 1-5)."""
        num, serr = _resolve_case_status_number(status)
        if serr:
            return serr
        data, err = await _call(
            "put",
            f"/lr-case-api/cases/{case_id}/actions/changeStatus",
            f"change status of case {case_id}",
            json={"statusNumber": num},
        )
        if err:
            return err
        c = data if isinstance(data, dict) else {}
        return (
            f"Case {c.get('number', case_id)} status changed to "
            f"{_case_status_text(c.get('status')) or _CASE_STATUS_NAMES[num]}."
        )

    async def add_logrhythm_case_note(case_id: str, note: str) -> str:
        """Add an investigation note (text evidence) to a LogRhythm case for the audit trail."""
        if not note or not note.strip():
            return "Error: note text is required."
        _, err = await _call(
            "post",
            f"/lr-case-api/cases/{case_id}/evidence/note",
            f"add note to case {case_id}",
            json={"text": note.strip()},
        )
        if err:
            return err
        return f"Note added to case {case_id}."

    # ══════════════════════════════════════════════════════════════════════
    # 2. ALARMS (detection triage)
    # ══════════════════════════════════════════════════════════════════════

    async def list_logrhythm_alarms(
        status: str = "",
        entity_name: str = "",
        alarm_rule_name: str = "",
        limit: int = 25,
    ) -> str:
        """List LogRhythm alarms. Filter by status (New/Opened/Working/Escalated/Closed*), entity_name, or alarm_rule_name."""
        params: dict[str, Any] = {"count": _safe_int(limit, 25, lo=1, hi=1000), "offset": 0}
        if status:
            name, serr = _resolve_alarm_status(status)
            if serr:
                return serr
            params["alarmStatus"] = name
        if entity_name:
            params["entityName"] = entity_name
        if alarm_rule_name:
            params["alarmRuleName"] = alarm_rule_name

        data, err = await _call(
            "get", "/lr-alarm-api/alarms", "list alarms", params=params
        )
        if err:
            return err
        alarms = _as_list(data, "alarmsSearchDetails", "alarms")
        shown = alarms[: _safe_int(limit, 25, lo=1, hi=1000)]

        def _fmt(a: dict) -> str:
            cases = a.get("associatedCases") or []
            return (
                f"  alarm {a.get('alarmId', '?')} [{a.get('alarmStatus', '?')}] - "
                f"{_trunc(a.get('alarmRuleName'), 90)}\n"
                f"    Entity: {a.get('entityName', '?')} | "
                f"Inserted: {_dt(a.get('dateInserted'))} | "
                f"Associated cases: {len(cases) if isinstance(cases, list) else 0}"
            )

        total = data.get("alarmsCount") if isinstance(data, dict) else None
        return format_entity_list(shown, _fmt, noun="alarm", total=total or len(alarms))

    async def get_logrhythm_alarm(alarm_id: int) -> str:
        """Get full detail of a LogRhythm alarm by its numeric id."""
        data, err = await _call(
            "get", f"/lr-alarm-api/alarms/{alarm_id}", f"get alarm {alarm_id}"
        )
        if err:
            return err
        a = _envelope(data, "alarmDetails")
        cases = a.get("associatedCases") or []
        lines = [
            f"Alarm {a.get('alarmId', alarm_id)}: {_trunc(a.get('alarmRuleName'), 200)}",
            f"Status: {a.get('alarmStatus', '?')} | "
            f"Entity: {a.get('entityName', '?')} | "
            f"RBP max/avg: {a.get('rBPMax', '?')}/{a.get('rBPAvg', '?')}",
            f"Events: {a.get('eventCount', '?')} | "
            f"First event: {_dt(a.get('eventDateFirst'))} | "
            f"Last event: {_dt(a.get('eventDateLast'))}",
            f"Inserted: {_dt(a.get('dateInserted'))} | "
            f"Updated: {_dt(a.get('dateUpdated'))} | "
            f"Last person id: {a.get('lastPersonID', 'N/A')}",
        ]
        if isinstance(cases, list) and cases:
            lines.append(f"Associated cases: {', '.join(str(x) for x in cases[:10])}")
        return "\n".join(lines)

    async def update_logrhythm_alarm_status(
        alarm_id: int, status: str, rbp: int = -1
    ) -> str:
        """Update a LogRhythm alarm's status (New/Opened/Working/Escalated/Closed/Closed_FalseAlarm/Closed_Resolved/Closed_Unresolved/Closed_Reported/Closed_Monitor) and optionally its Risk Based Priority (rbp, 0-100)."""
        name, serr = _resolve_alarm_status(status)
        if serr:
            return serr
        body: dict[str, Any] = {"alarmStatus": name}
        # ``rbp`` may arrive as a string/None from the LLM (tool args are not
        # runtime schema-validated), so coerce before comparing - ``"50" >= 0``
        # would raise TypeError. Sentinel -1 (the default) means "leave the RBP
        # unchanged"; 0-100 sets it (0 is a valid Risk Based Priority).
        try:
            rbp_val = int(rbp)
        except (TypeError, ValueError):
            rbp_val = -1
        if rbp_val >= 0:
            body["rBP"] = max(0, min(rbp_val, 100))
        _, err = await _call(
            "patch",
            f"/lr-alarm-api/alarms/{alarm_id}",
            f"update alarm {alarm_id}",
            json=body,
        )
        if err:
            return err
        rbp_note = f", RBP {body['rBP']}" if "rBP" in body else ""
        return f"Alarm {alarm_id} status set to {name}{rbp_note}."

    async def add_logrhythm_alarm_comment(alarm_id: int, comment: str) -> str:
        """Add a comment to a LogRhythm alarm for the investigation audit trail."""
        if not comment or not comment.strip():
            return "Error: comment text is required."
        _, err = await _call(
            "post",
            f"/lr-alarm-api/alarms/{alarm_id}/comments",
            f"add comment to alarm {alarm_id}",
            json={"alarmComment": comment.strip()},
        )
        if err:
            return err
        return f"Comment added to alarm {alarm_id}."

    async def list_logrhythm_alarm_events(alarm_id: int, limit: int = 30) -> str:
        """List the underlying log events that contributed to a LogRhythm alarm (forensic drill-down)."""
        data, err = await _call(
            "get",
            f"/lr-alarm-api/alarms/{alarm_id}/events",
            f"get events for alarm {alarm_id}",
        )
        if err:
            return err
        events = _as_list(data, "alarmEventsDetails", "events")
        if not events:
            return f"No events found for alarm {alarm_id}."
        shown = events[: _safe_int(limit, 30, lo=1, hi=1000)]
        lines = [f"Events for alarm {alarm_id} ({len(shown)} of {len(events)}):"]
        for e in shown:
            who = e.get("login") or e.get("account") or e.get("user") or ""
            host = e.get("impactedHostName") or e.get("originHostName") or e.get("host") or ""
            lines.append(
                f"  [{_dt(e.get('normalDate') or e.get('logDate'))}] "
                f"{_trunc(e.get('commonEventName') or e.get('classificationName') or e.get('action'), 80)} "
                f"| {e.get('logSourceName', '?')}"
                + (f" | host: {host}" if host else "")
                + (f" | user: {who}" if who else "")
                + (f"\n    {_trunc(e.get('message') or e.get('originalMessage'), 200)}" if (e.get('message') or e.get('originalMessage')) else "")
            )
        return "\n".join(lines)

    # ══════════════════════════════════════════════════════════════════════
    # 3. SEARCH (log / event hunting) - async request -> poll -> results
    # ══════════════════════════════════════════════════════════════════════

    def _build_search_task(
        keyword: str,
        number_of_days: int,
        max_messages: int,
        log_source_ids: list[int],
        query_filter: dict | None,
    ) -> dict[str, Any]:
        """Build a LogRhythm Search API ``search-task`` request body.

        Searches the Web Indexer over the last ``number_of_days`` days. When a
        ``query_filter`` object is supplied it is used verbatim (advanced use);
        otherwise a free-text ``keyword`` is matched against the raw log message.
        The LogRhythm filter taxonomy (filterType codes) is deployment/version
        specific - pass ``query_filter`` for precise field-level hunts.
        """
        body: dict[str, Any] = {
            "maxMsgsToQuery": max_messages,
            "logCacheSize": 10000,
            "aggregateLogsCacheSize": 10000,
            "queryTimeout": 300,
            "isOriginatedFromWeb": False,
            "webLayoutId": 0,
            "queryRawLog": True,
            "queryEventManager": False,
            "dateCriteria": {
                "useInsertedDate": False,
                "lastIntervalValue": max(1, number_of_days),
                "lastIntervalUnit": "Day",
            },
            "queryLogSources": log_source_ids,
            "queryFilter": query_filter
            or {
                "msgFilterType": 2,
                "filterGroup": {
                    "filterItemType": 0,
                    "fieldOperator": 0,
                    "filterMode": 1,
                    "filterGroupOperator": 0,
                    "filterItems": (
                        [
                            {
                                "filterItemType": 1,
                                "fieldOperator": 0,
                                "filterMode": 1,
                                "values": [
                                    {
                                        "filterType": 9,
                                        "valueType": 4,
                                        "value": keyword,
                                    }
                                ],
                                "name": "Message",
                            }
                        ]
                        if keyword
                        else []
                    ),
                    "name": "Filter Group",
                },
            },
        }
        return body

    async def run_logrhythm_search(
        keyword: str = "",
        number_of_days: int = 1,
        max_messages: int = 100,
        log_source_ids: str = "",
        query_filter: dict | None = None,
    ) -> str:
        """Hunt LogRhythm logs/events: submit an indexed search, wait for it, and return matches.

        Runs an async search (submit search-task -> poll search-result ->
        return logs), bounded by a polling budget. ``keyword`` matches the raw
        log message; ``number_of_days`` is the lookback window; ``log_source_ids``
        is an optional comma-separated list to scope the search. Advanced
        callers can pass a raw ``query_filter`` object (LogRhythm queryFilter
        schema) for precise field-level hunts.
        """
        sources: list[int] = []
        for piece in (log_source_ids or "").split(","):
            piece = piece.strip()
            if piece.isdigit():
                sources.append(int(piece))
        body = _build_search_task(
            keyword.strip(),
            _safe_int(number_of_days, 1, lo=1),
            _safe_int(max_messages, 100, lo=1, hi=1000),
            sources,
            query_filter if isinstance(query_filter, dict) else None,
        )

        start, serr = await _call(
            "post",
            "/lr-search-api/actions/search-task",
            "submit search task",
            json=body,
            timeout=_SEARCH_TIMEOUT,
        )
        if serr:
            return serr
        start = start if isinstance(start, dict) else {}
        task_id = start.get("TaskId") or start.get("taskId")
        if not task_id and isinstance(start.get("Data"), dict):
            task_id = start["Data"].get("TaskId")
        if not task_id:
            return "Error: LogRhythm did not return a search TaskId."

        result_body = {
            "data": {
                "searchGuid": task_id,
                "search": {"sort": [], "groupBy": [], "fields": []},
                "paginator": {"origin": 1, "page_size": body["maxMsgsToQuery"]},
            }
        }

        last_status = ""
        for _ in range(_SEARCH_MAX_POLLS):
            res, rerr = await _call(
                "post",
                "/lr-search-api/actions/search-result",
                f"poll search result {task_id}",
                json=result_body,
                timeout=_SEARCH_TIMEOUT,
            )
            if rerr:
                return rerr
            res = res if isinstance(res, dict) else {}
            last_status = str(res.get("TaskStatus") or res.get("taskStatus") or "")
            if last_status.lower() in ("failed", "cancelled", "canceled", "error"):
                return f"LogRhythm search {task_id} ended with status {last_status}."
            items = _as_list(
                res.get("Items") or res.get("items") or res.get("Data") or res,
                "Items",
                "items",
            )
            if last_status.lower() in ("completed", "complete") or items:
                return _format_search_items(task_id, items)
            await asyncio.sleep(_SEARCH_POLL_INTERVAL)

        return (
            f"LogRhythm search {task_id} is still running (status "
            f"{last_status or 'unknown'}). Retry shortly with a narrower window."
        )

    def _format_search_items(task_id: str, items: list) -> str:
        if not items:
            return f"LogRhythm search {task_id} completed with 0 matching logs."
        lines = [f"LogRhythm search {task_id} ({len(items)} logs):"]
        for it in items:
            if not isinstance(it, dict):
                continue
            lines.append(
                f"  [{_dt(it.get('normalDate') or it.get('logDate'))}] "
                f"{_trunc(it.get('commonEventName') or it.get('classificationName'), 70)} "
                f"| {it.get('logSourceName', '?')} | "
                f"host: {it.get('impactedHostName') or it.get('originHostName') or 'N/A'}\n"
                f"    {_trunc(it.get('message') or it.get('originalMessage'), 200)}"
            )
        return "\n".join(lines)

    # ══════════════════════════════════════════════════════════════════════
    # 4. CONTEXT - hosts, entities (admin)
    # ══════════════════════════════════════════════════════════════════════

    async def list_logrhythm_hosts(name: str = "", limit: int = 25) -> str:
        """List LogRhythm hosts (the asset model) for endpoint/asset context. Optionally filter by host name."""
        params: dict[str, Any] = {}
        if name:
            params["name"] = name
        data, err = await _call(
            "get",
            "/lr-admin-api/hosts",
            "list hosts",
            params=params or None,
            extra_headers=_page_headers(limit),
        )
        if err:
            return err
        hosts = _as_list(data, "hosts")
        shown = hosts[: _safe_int(limit, 25, lo=1, hi=1000)]

        def _fmt(h: dict) -> str:
            return (
                f"  host {h.get('id', '?')} {h.get('name', '?')} - "
                f"OS: {h.get('osType', h.get('os', 'N/A'))} | "
                f"Risk: {h.get('riskLevel', 'N/A')} | "
                f"Threat: {h.get('threatLevel', 'N/A')} | "
                f"Status: {h.get('recordStatusName', h.get('hostStatus', 'N/A'))}"
            )

        return format_entity_list(shown, _fmt, noun="host", total=len(hosts))

    async def list_logrhythm_entities(name: str = "", limit: int = 50) -> str:
        """List LogRhythm entities (the network/organisation segmentation used to scope cases, alarms, and hosts)."""
        params: dict[str, Any] = {}
        if name:
            params["name"] = name
        data, err = await _call(
            "get",
            "/lr-admin-api/entities",
            "list entities",
            params=params or None,
            extra_headers=_page_headers(limit),
        )
        if err:
            return err
        entities = _as_list(data, "entities")
        shown = entities[: _safe_int(limit, 50, lo=1, hi=1000)]

        def _fmt(e: dict) -> str:
            return (
                f"  entity {e.get('id', '?')} {_entity_name(e)} - "
                f"Status: {e.get('recordStatusName', 'N/A')}"
                + (f" | Parent: {e.get('parentEntityName')}" if e.get("parentEntityName") else "")
            )

        return format_entity_list(shown, _fmt, noun="entity", total=len(entities))

    # ══════════════════════════════════════════════════════════════════════
    # REGISTER ALL TOOLS
    # ══════════════════════════════════════════════════════════════════════

    executor.register("get_logrhythm_status", get_logrhythm_status)
    executor.register("list_logrhythm_cases", list_logrhythm_cases)
    executor.register("get_logrhythm_case", get_logrhythm_case)
    executor.register("create_logrhythm_case", create_logrhythm_case)
    executor.register("update_logrhythm_case", update_logrhythm_case)
    executor.register("update_logrhythm_case_status", update_logrhythm_case_status)
    executor.register("add_logrhythm_case_note", add_logrhythm_case_note)
    executor.register("list_logrhythm_alarms", list_logrhythm_alarms)
    executor.register("get_logrhythm_alarm", get_logrhythm_alarm)
    executor.register("update_logrhythm_alarm_status", update_logrhythm_alarm_status)
    executor.register("add_logrhythm_alarm_comment", add_logrhythm_alarm_comment)
    executor.register("list_logrhythm_alarm_events", list_logrhythm_alarm_events)
    executor.register("run_logrhythm_search", run_logrhythm_search)
    executor.register("list_logrhythm_hosts", list_logrhythm_hosts)
    executor.register("list_logrhythm_entities", list_logrhythm_entities)

    return [
        # ── 0. Connection ──────────────────────────────────────────────────
        ToolDefinition(
            name="get_logrhythm_status",
            description=(
                "Validate the LogRhythm SIEM connection and report reachable "
                "context (entities). Use first if other LogRhythm tools fail."
            ),
            input_schema={"type": "object", "properties": {}, "required": []},
            source=_SOURCE,
            priority=2,
        ),
        # ── 1. Cases (incident management) ─────────────────────────────────
        ToolDefinition(
            name="list_logrhythm_cases",
            description=(
                "List LogRhythm cases (incident records), newest first. Filter by "
                "status (Created/Completed/Incident/Mitigated/Resolved, or 1-5), "
                "priority (1 highest - 5), or a text search on case name/number."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "status": {
                        "type": "string",
                        "description": "Created, Completed, Incident, Mitigated, Resolved, or 1-5",
                    },
                    "priority": {
                        "type": "integer",
                        "description": "Filter to a single priority (1 highest - 5)",
                        "default": 0,
                    },
                    "text": {"type": "string", "description": "Search case name/number"},
                    "limit": {"type": "integer", "default": 25},
                },
                "required": [],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="get_logrhythm_case",
            description=(
                "Get full detail of a LogRhythm case by id (status, priority, "
                "entity, owner, collaborators, tags, summary, resolution, timeline)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "case_id": {
                        "type": "string",
                        "description": "Case number or RFC 4122 id",
                    }
                },
                "required": ["case_id"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="create_logrhythm_case",
            description=(
                "Create a LogRhythm case (incident record). priority is 1 (highest) "
                "to 5; summary and due_date (RFC 3339) are optional."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Case name"},
                    "priority": {
                        "type": "integer",
                        "enum": [1, 2, 3, 4, 5],
                        "default": 3,
                    },
                    "summary": {"type": "string"},
                    "due_date": {
                        "type": "string",
                        "description": "RFC 3339 timestamp, e.g. 2026-01-31T17:00:00Z",
                    },
                },
                "required": ["name"],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="update_logrhythm_case",
            description=(
                "Update a LogRhythm case's name, priority (1-5), summary, "
                "resolution, or due_date. To change the workflow status use "
                "update_logrhythm_case_status instead."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "case_id": {"type": "string", "description": "Case number or id"},
                    "name": {"type": "string"},
                    "priority": {"type": "integer", "enum": [1, 2, 3, 4, 5]},
                    "summary": {"type": "string"},
                    "resolution": {"type": "string"},
                    "due_date": {"type": "string", "description": "RFC 3339 timestamp"},
                },
                "required": ["case_id"],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="update_logrhythm_case_status",
            description=(
                "Change a LogRhythm case's workflow status: Created, Completed, "
                "Incident, Mitigated, or Resolved (or the number 1-5)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "case_id": {"type": "string", "description": "Case number or id"},
                    "status": {
                        "type": "string",
                        "description": "Created, Completed, Incident, Mitigated, Resolved, or 1-5",
                    },
                },
                "required": ["case_id", "status"],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="add_logrhythm_case_note",
            description=(
                "Add an investigation note (text evidence) to a LogRhythm case "
                "for the audit trail."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "case_id": {"type": "string", "description": "Case number or id"},
                    "note": {"type": "string"},
                },
                "required": ["case_id", "note"],
            },
            source=_SOURCE,
            priority=1,
        ),
        # ── 2. Alarms (detection triage) ───────────────────────────────────
        ToolDefinition(
            name="list_logrhythm_alarms",
            description=(
                "List LogRhythm alarms (rule detections). Filter by status "
                "(New/Opened/Working/Escalated/Closed and Closed_* variants), "
                "entity_name, or alarm_rule_name."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "status": {
                        "type": "string",
                        "description": "New, Opened, Working, Escalated, Closed, Closed_Resolved, ...",
                    },
                    "entity_name": {"type": "string"},
                    "alarm_rule_name": {"type": "string"},
                    "limit": {"type": "integer", "default": 25},
                },
                "required": [],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="get_logrhythm_alarm",
            description=(
                "Get full detail of a LogRhythm alarm by numeric id (rule, status, "
                "entity, RBP, event counts/dates, associated cases)."
            ),
            input_schema={
                "type": "object",
                "properties": {"alarm_id": {"type": "integer"}},
                "required": ["alarm_id"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="update_logrhythm_alarm_status",
            description=(
                "Update a LogRhythm alarm's status (New, Opened, Working, "
                "Escalated, Closed, Closed_FalseAlarm, Closed_Resolved, "
                "Closed_Unresolved, Closed_Reported, Closed_Monitor) and "
                "optionally its Risk Based Priority (rbp, 0-100)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "alarm_id": {"type": "integer"},
                    "status": {"type": "string"},
                    "rbp": {
                        "type": "integer",
                        "description": "Risk Based Priority 0-100 (omit to leave unchanged)",
                        "default": -1,
                    },
                },
                "required": ["alarm_id", "status"],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="add_logrhythm_alarm_comment",
            description="Add a comment to a LogRhythm alarm for the investigation audit trail.",
            input_schema={
                "type": "object",
                "properties": {
                    "alarm_id": {"type": "integer"},
                    "comment": {"type": "string"},
                },
                "required": ["alarm_id", "comment"],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="list_logrhythm_alarm_events",
            description=(
                "List the underlying log events that contributed to a LogRhythm "
                "alarm (forensic drill-down: timestamps, log source, host, user, "
                "message)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "alarm_id": {"type": "integer"},
                    "limit": {"type": "integer", "default": 30},
                },
                "required": ["alarm_id"],
            },
            source=_SOURCE,
            priority=0,
        ),
        # ── 3. Search (log / event hunting) ────────────────────────────────
        ToolDefinition(
            name="run_logrhythm_search",
            description=(
                "Hunt LogRhythm logs/events: submit an indexed search, wait for it "
                "to finish, and return the matching logs. keyword matches the raw "
                "log message; number_of_days is the lookback window; log_source_ids "
                "(comma-separated) scopes the search. Runs an async search "
                "(submit -> poll -> results). For precise field-level hunts pass a "
                "raw query_filter object (LogRhythm queryFilter schema)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "keyword": {
                        "type": "string",
                        "description": "Free text matched against the raw log message",
                    },
                    "number_of_days": {
                        "type": "integer",
                        "default": 1,
                        "description": "Lookback window in days",
                    },
                    "max_messages": {"type": "integer", "default": 100},
                    "log_source_ids": {
                        "type": "string",
                        "description": "Comma-separated log source IDs to scope the search",
                    },
                    "query_filter": {
                        "type": "object",
                        "description": "Advanced: raw LogRhythm queryFilter object (overrides keyword)",
                    },
                },
                "required": [],
            },
            source=_SOURCE,
            priority=0,
        ),
        # ── 4. Context (admin) ─────────────────────────────────────────────
        ToolDefinition(
            name="list_logrhythm_hosts",
            description=(
                "List LogRhythm hosts (the asset model) for endpoint/asset context "
                "- OS, risk/threat level, and status. Optionally filter by name."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Filter by host name"},
                    "limit": {"type": "integer", "default": 25},
                },
                "required": [],
            },
            source=_SOURCE,
            priority=2,
        ),
        ToolDefinition(
            name="list_logrhythm_entities",
            description=(
                "List LogRhythm entities (the network/organisation segmentation "
                "used to scope cases, alarms, and hosts). Optionally filter by name."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Filter by entity name"},
                    "limit": {"type": "integer", "default": 50},
                },
                "required": [],
            },
            source=_SOURCE,
            priority=2,
        ),
    ]
