# SPDX-License-Identifier: Apache-2.0

"""PagerDuty integration - on-call escalation of critical exposures.

Incidents (list, read, create, acknowledge / resolve / reassign), their notes,
the log entries of the account or of one incident, and the on-call context an
escalation needs: services, escalation policies, who is on call, a user.

Authentication is a REST API key sent as ``Authorization: Token token=<key>``
(no OAuth). The REST API lives on ``https://api.pagerduty.com`` or, for an
account in the EU service region, ``https://api.eu.pagerduty.com``: the host is
picked from that fixed pair, never typed. PagerDuty requires a ``From`` header
naming a user of the account on every incident write; it is the integration's
``from_email``, and the write tools refuse without it.

Token format: ``region||from_email||api_key`` (assembled by ``_get_token``).
"""

from __future__ import annotations

import re
from typing import Any

import httpx

from app.core.egress import egress_client
from app.integrations.http import (
    TRANSIENT_RETRY_STATUSES,
    clip_text,
    parse_compound_token,
    safe_request,
)
from app.tools.base import CallableToolExecutor, ToolDefinition

_PROVIDER = "PagerDuty"
_SOURCE = "integration:pagerduty"

#: REST API host per service region.
PAGERDUTY_APIS = {
    "us": "https://api.pagerduty.com",
    "eu": "https://api.eu.pagerduty.com",
}

_TIMEOUT = 30
_DEFAULT_LIMIT = 25
_MAX_LIMIT = 100

_ID_RE = re.compile(r"[A-Za-z0-9]{1,32}")
_EMAIL_RE = re.compile(r"[^@\s]{1,64}@[^@\s]{1,255}\.[^@\s]{2,63}")

INCIDENT_STATUSES = ("triggered", "acknowledged", "resolved")
_URGENCIES = ("high", "low")


def pagerduty_api(region: str) -> str:
    """The REST host of *region* (``us`` when unknown or blank)."""
    return PAGERDUTY_APIS.get((region or "").strip().lower(), PAGERDUTY_APIS["us"])


def build_pagerduty_headers(api_key: str, from_email: str = "") -> dict[str, str]:
    """The one header builder, shared with the connectivity test."""
    headers = {
        "Authorization": f"Token token={(api_key or '').strip()}",
        "Accept": "application/vnd.pagerduty+json;version=2",
        "Content-Type": "application/json",
    }
    if from_email:
        headers["From"] = from_email
    return headers


def parse_pagerduty_token(token: str) -> tuple[str, str, str]:
    """``(region, from_email, api_key)``; a bare key is a US account with no From."""
    if "||" not in (token or ""):
        return "us", "", (token or "").strip()
    region, from_email, api_key = parse_compound_token(token, 3, greedy_last=True)
    email = from_email.strip()
    return (
        region.strip().lower() or "us",
        email if _EMAIL_RE.fullmatch(email) else "",
        api_key.strip(),
    )


def _clean(value: Any, limit: int = 200) -> str:
    text = " ".join(str(value if value is not None else "").split())
    return clip_text(text, limit) if text else "-"


def _json(resp: httpx.Response) -> dict[str, Any]:
    try:
        data = resp.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _limit(value: Any) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return _DEFAULT_LIMIT
    return max(1, min(n, _MAX_LIMIT))


def _offset(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _ids(values: Any) -> list[str] | None:
    """*values* as PagerDuty ids, ``None`` when one of them is not an id."""
    if values in (None, "", []):
        return []
    items = values if isinstance(values, list) else [values]
    out = [str(v).strip() for v in items]
    return out if all(_ID_RE.fullmatch(v) for v in out) else None


def _summary(ref: Any) -> str:
    return _clean((ref or {}).get("summary") if isinstance(ref, dict) else None, 80)


def _format_incident(inc: dict[str, Any], *, detail: bool = False) -> str:
    assignees = ", ".join(
        _summary(a.get("assignee")) for a in inc.get("assignments") or [] if isinstance(a, dict)
    )
    lines = [
        f"#{inc.get('incident_number', '?')} {_clean(inc.get('title'))} (id: {_clean(inc.get('id'), 32)})",
        f"  Status: {_clean(inc.get('status'), 20)} | Urgency: {_clean(inc.get('urgency'), 10)}"
        f" | Service: {_summary(inc.get('service'))}"
        + (f" | Priority: {_summary(inc.get('priority'))}" if inc.get("priority") else ""),
        f"  Created: {_clean(inc.get('created_at'), 40)} | Assigned to: {assignees or '-'}",
    ]
    if detail:
        lines.append(f"  Escalation policy: {_summary(inc.get('escalation_policy'))}")
        if inc.get("last_status_change_at"):
            lines.append(f"  Last status change: {_clean(inc.get('last_status_change_at'), 40)}")
        body = (inc.get("body") or {}).get("details") if isinstance(inc.get("body"), dict) else None
        if body:
            lines.append(f"  Details: {_clean(body, 1500)}")
        if inc.get("html_url"):
            lines.append(f"  Link: {_clean(inc.get('html_url'), 300)}")
    return "\n".join(lines)


def _more(data: dict[str, Any], offset: int, count: int) -> str:
    if data.get("more"):
        return f"\n[More results: pass offset={offset + count}]"
    return ""


def build_tools(
    token: str, executor: CallableToolExecutor, **_kwargs: Any
) -> list[ToolDefinition]:
    region, from_email, api_key = parse_pagerduty_token(token)
    if not api_key:
        return []
    base = pagerduty_api(region)
    read_headers = build_pagerduty_headers(api_key)
    write_headers = build_pagerduty_headers(api_key, from_email)

    async def _get(path: str, action: str, params: Any = None):
        async with egress_client(timeout=_TIMEOUT) as client:
            return await safe_request(
                client,
                "get",
                f"{base}{path}",
                _PROVIDER,
                action,
                headers=read_headers,
                params=params,
                retry_statuses=TRANSIENT_RETRY_STATUSES,
            )

    async def _write(method: str, path: str, action: str, body: dict):
        async with egress_client(timeout=_TIMEOUT) as client:
            return await safe_request(
                client, method, f"{base}{path}", _PROVIDER, action, headers=write_headers, json=body
            )

    def _no_from() -> str | None:
        if from_email:
            return None
        return (
            "Error: PagerDuty requires the email of a user of the account on every "
            "incident change. Edit the integration and set 'from_email'."
        )

    def _bad_id(name: str) -> str:
        return f"Error: {name} must be a PagerDuty id (letters and digits, e.g. PABC123)."

    # -- Incidents --------------------------------------------------------

    async def list_pagerduty_incidents(
        statuses: list[str] | None = None,
        urgency: str = "",
        service_ids: list[str] | None = None,
        since: str = "",
        until: str = "",
        limit: int = _DEFAULT_LIMIT,
        offset: int = 0,
    ) -> str:
        wanted = [s for s in (statuses or []) if s]
        if any(s not in INCIDENT_STATUSES for s in wanted):
            return f"Error: statuses must be among {', '.join(INCIDENT_STATUSES)}."
        if urgency and urgency not in _URGENCIES:
            return "Error: urgency must be high or low."
        services = _ids(service_ids)
        if services is None:
            return _bad_id("service_ids")
        n, start = _limit(limit), _offset(offset)
        params: list[tuple[str, Any]] = [("limit", n), ("offset", start), ("sort_by", "created_at:desc")]
        params += [("statuses[]", s) for s in wanted]
        params += [("service_ids[]", s) for s in services]
        if urgency:
            params.append(("urgencies[]", urgency))
        if since:
            params.append(("since", str(since)[:40]))
        if until:
            params.append(("until", str(until)[:40]))
        resp, err = await _get("/incidents", "list incidents", params)
        if err:
            return err
        data = _json(resp)
        incidents = [i for i in data.get("incidents") or [] if isinstance(i, dict)]
        if not incidents:
            return "No incidents found."
        body = "\n".join(_format_incident(i) for i in incidents)
        return f"Found {len(incidents)} incident(s):\n{body}{_more(data, start, len(incidents))}"

    async def get_pagerduty_incident(incident_id: str) -> str:
        iid = str(incident_id or "").strip()
        if not _ID_RE.fullmatch(iid):
            return _bad_id("incident_id")
        resp, err = await _get(f"/incidents/{iid}", f"get incident {iid}", {"include[]": "body"})
        if err:
            return err
        return _format_incident(_json(resp).get("incident") or {}, detail=True)

    async def create_pagerduty_incident(
        service_id: str,
        title: str,
        details: str = "",
        urgency: str = "high",
        escalation_policy_id: str = "",
        assignee_ids: list[str] | None = None,
        incident_key: str = "",
    ) -> str:
        if err := _no_from():
            return err
        sid = str(service_id or "").strip()
        if not _ID_RE.fullmatch(sid):
            return _bad_id("service_id")
        head = " ".join(str(title or "").split())
        if not head:
            return "Error: title is required."
        if urgency not in _URGENCIES:
            return "Error: urgency must be high or low."
        assignees = _ids(assignee_ids)
        if assignees is None:
            return _bad_id("assignee_ids")
        incident: dict[str, Any] = {
            "type": "incident",
            "title": head[:1024],
            "service": {"id": sid, "type": "service_reference"},
            "urgency": urgency,
        }
        if details:
            incident["body"] = {"type": "incident_body", "details": str(details)[:65000]}
        if escalation_policy_id:
            if not _ID_RE.fullmatch(str(escalation_policy_id)):
                return _bad_id("escalation_policy_id")
            incident["escalation_policy"] = {
                "id": escalation_policy_id,
                "type": "escalation_policy_reference",
            }
        if assignees:
            incident["assignments"] = [
                {"assignee": {"id": a, "type": "user_reference"}} for a in assignees
            ]
        if incident_key:
            # PagerDuty de-duplicates open incidents on this key.
            incident["incident_key"] = str(incident_key)[:255]
        resp, err = await _write("post", "/incidents", f"create incident {head[:60]!r}", {"incident": incident})
        if err:
            return err
        return "Created incident:\n" + _format_incident(_json(resp).get("incident") or {})

    async def update_pagerduty_incident(
        incident_id: str,
        status: str = "",
        urgency: str = "",
        title: str = "",
        escalation_level: int | None = None,
        assignee_ids: list[str] | None = None,
        resolution: str = "",
    ) -> str:
        if err := _no_from():
            return err
        iid = str(incident_id or "").strip()
        if not _ID_RE.fullmatch(iid):
            return _bad_id("incident_id")
        change: dict[str, Any] = {"type": "incident_reference"}
        if status:
            if status not in ("acknowledged", "resolved"):
                return "Error: status must be acknowledged or resolved."
            change["status"] = status
        if urgency:
            if urgency not in _URGENCIES:
                return "Error: urgency must be high or low."
            change["urgency"] = urgency
        if title:
            change["title"] = " ".join(str(title).split())[:1024]
        if escalation_level is not None:
            try:
                change["escalation_level"] = max(1, int(escalation_level))
            except (TypeError, ValueError):
                return "Error: escalation_level must be a number (1 = first level)."
        assignees = _ids(assignee_ids)
        if assignees is None:
            return _bad_id("assignee_ids")
        if assignees:
            change["assignments"] = [
                {"assignee": {"id": a, "type": "user_reference"}} for a in assignees
            ]
        if resolution:
            if change.get("status") != "resolved":
                return "Error: a resolution note goes with status='resolved'."
            change["resolution"] = str(resolution)[:65000]
        if len(change) == 1:
            return "Error: nothing to change - give a status, urgency, title, escalation level or assignees."
        resp, err = await _write("put", f"/incidents/{iid}", f"update incident {iid}", {"incident": change})
        if err:
            return err
        return "Updated incident:\n" + _format_incident(_json(resp).get("incident") or {})

    # -- Notes and log entries -------------------------------------------

    async def list_pagerduty_incident_notes(incident_id: str) -> str:
        iid = str(incident_id or "").strip()
        if not _ID_RE.fullmatch(iid):
            return _bad_id("incident_id")
        resp, err = await _get(f"/incidents/{iid}/notes", f"list the notes of incident {iid}")
        if err:
            return err
        notes = [n for n in _json(resp).get("notes") or [] if isinstance(n, dict)]
        if not notes:
            return f"Incident {iid} has no notes."
        lines = [
            f"- {_clean(n.get('created_at'), 40)} {_summary(n.get('user'))}: {_clean(n.get('content'), 1000)}"
            for n in notes
        ]
        return f"{len(notes)} note(s) on incident {iid}:\n" + "\n".join(lines)

    async def add_pagerduty_incident_note(incident_id: str, content: str) -> str:
        if err := _no_from():
            return err
        iid = str(incident_id or "").strip()
        if not _ID_RE.fullmatch(iid):
            return _bad_id("incident_id")
        text = str(content or "").strip()
        if not text:
            return "Error: content is required."
        _resp, err = await _write(
            "post", f"/incidents/{iid}/notes", f"add a note to incident {iid}", {"note": {"content": text[:25000]}}
        )
        return err or f"Added a note to incident {iid}."

    async def list_pagerduty_log_entries(
        incident_id: str = "", since: str = "", until: str = "", limit: int = _DEFAULT_LIMIT, offset: int = 0
    ) -> str:
        path = "/log_entries"
        if incident_id:
            iid = str(incident_id).strip()
            if not _ID_RE.fullmatch(iid):
                return _bad_id("incident_id")
            path = f"/incidents/{iid}/log_entries"
        n, start = _limit(limit), _offset(offset)
        params: dict[str, Any] = {"limit": n, "offset": start}
        if since:
            params["since"] = str(since)[:40]
        if until:
            params["until"] = str(until)[:40]
        resp, err = await _get(path, "list log entries", params)
        if err:
            return err
        data = _json(resp)
        entries = [e for e in data.get("log_entries") or [] if isinstance(e, dict)]
        if not entries:
            return "No log entries found."
        lines = [
            f"- {_clean(e.get('created_at'), 40)} {_clean(e.get('type'), 40)} "
            f"(id: {_clean(e.get('id'), 32)}) on {_summary(e.get('incident'))}: {_clean(e.get('summary'), 300)}"
            for e in entries
        ]
        return f"Log entries ({len(entries)}):\n" + "\n".join(lines) + _more(data, start, len(entries))

    async def get_pagerduty_log_entry(log_entry_id: str) -> str:
        lid = str(log_entry_id or "").strip()
        if not _ID_RE.fullmatch(lid):
            return _bad_id("log_entry_id")
        resp, err = await _get(f"/log_entries/{lid}", f"get log entry {lid}")
        if err:
            return err
        e = _json(resp).get("log_entry") or {}
        return (
            f"{_clean(e.get('type'), 40)} at {_clean(e.get('created_at'), 40)} on {_summary(e.get('incident'))}\n"
            f"  By: {_summary(e.get('agent'))} | Channel: {_clean((e.get('channel') or {}).get('type'), 40)}\n"
            f"  {_clean(e.get('summary'), 1000)}"
        )

    # -- On-call context --------------------------------------------------

    async def list_pagerduty_services(query: str = "", limit: int = _DEFAULT_LIMIT) -> str:
        params: dict[str, Any] = {"limit": _limit(limit)}
        if query:
            params["query"] = str(query)[:100]
        resp, err = await _get("/services", "list services", params)
        if err:
            return err
        services = [s for s in _json(resp).get("services") or [] if isinstance(s, dict)]
        if not services:
            return "No services found."
        lines = [
            f"- {_clean(s.get('name'), 120)} (id: {_clean(s.get('id'), 32)}): status {_clean(s.get('status'), 20)}, "
            f"escalation policy {_summary(s.get('escalation_policy'))}"
            for s in services
        ]
        return f"Services ({len(services)}):\n" + "\n".join(lines)

    async def list_pagerduty_escalation_policies(query: str = "", limit: int = _DEFAULT_LIMIT) -> str:
        params: dict[str, Any] = {"limit": _limit(limit)}
        if query:
            params["query"] = str(query)[:100]
        resp, err = await _get("/escalation_policies", "list escalation policies", params)
        if err:
            return err
        policies = [p for p in _json(resp).get("escalation_policies") or [] if isinstance(p, dict)]
        if not policies:
            return "No escalation policies found."
        lines = [
            f"- {_clean(p.get('name'), 120)} (id: {_clean(p.get('id'), 32)}): "
            f"{len(p.get('escalation_rules') or [])} level(s)"
            for p in policies
        ]
        return f"Escalation policies ({len(policies)}):\n" + "\n".join(lines)

    async def list_pagerduty_oncalls(
        escalation_policy_ids: list[str] | None = None, schedule_ids: list[str] | None = None
    ) -> str:
        policies, schedules = _ids(escalation_policy_ids), _ids(schedule_ids)
        if policies is None:
            return _bad_id("escalation_policy_ids")
        if schedules is None:
            return _bad_id("schedule_ids")
        params: list[tuple[str, Any]] = [("limit", _MAX_LIMIT)]
        params += [("escalation_policy_ids[]", p) for p in policies]
        params += [("schedule_ids[]", s) for s in schedules]
        resp, err = await _get("/oncalls", "list who is on call", params)
        if err:
            return err
        oncalls = [o for o in _json(resp).get("oncalls") or [] if isinstance(o, dict)]
        if not oncalls:
            return "Nobody is on call for that selection."
        lines = [
            f"- Level {o.get('escalation_level', '?')} of {_summary(o.get('escalation_policy'))}: "
            f"{_summary(o.get('user'))} (user id: {_clean((o.get('user') or {}).get('id'), 32)})"
            + (f", until {_clean(o.get('end'), 40)}" if o.get("end") else "")
            for o in oncalls
        ]
        return f"On call now ({len(oncalls)}):\n" + "\n".join(lines)

    async def get_pagerduty_user(user_id: str) -> str:
        uid = str(user_id or "").strip()
        if not _ID_RE.fullmatch(uid):
            return _bad_id("user_id")
        resp, err = await _get(f"/users/{uid}", f"get user {uid}", {"include[]": "contact_methods"})
        if err:
            return err
        u = _json(resp).get("user") or {}
        return (
            f"{_clean(u.get('name'), 120)} (id: {_clean(u.get('id'), 32)})\n"
            f"  Email: {_clean(u.get('email'), 120)} | Role: {_clean(u.get('role'), 40)} | "
            f"Time zone: {_clean(u.get('time_zone'), 60)}\n"
            f"  Teams: {', '.join(_summary(t) for t in u.get('teams') or []) or '-'}"
        )

    handlers = {
        "list_pagerduty_incidents": list_pagerduty_incidents,
        "get_pagerduty_incident": get_pagerduty_incident,
        "create_pagerduty_incident": create_pagerduty_incident,
        "update_pagerduty_incident": update_pagerduty_incident,
        "list_pagerduty_incident_notes": list_pagerduty_incident_notes,
        "add_pagerduty_incident_note": add_pagerduty_incident_note,
        "list_pagerduty_log_entries": list_pagerduty_log_entries,
        "get_pagerduty_log_entry": get_pagerduty_log_entry,
        "list_pagerduty_services": list_pagerduty_services,
        "list_pagerduty_escalation_policies": list_pagerduty_escalation_policies,
        "list_pagerduty_oncalls": list_pagerduty_oncalls,
        "get_pagerduty_user": get_pagerduty_user,
    }
    for name, fn in handlers.items():
        executor.register(name, fn)

    def _str(description: str) -> dict:
        return {"type": "string", "description": description}

    def _id_list(description: str) -> dict:
        return {"type": "array", "items": {"type": "string"}, "description": description}

    limit_prop = {"type": "integer", "description": f"Most items returned (default {_DEFAULT_LIMIT}, max {_MAX_LIMIT})"}
    offset_prop = {"type": "integer", "description": "Items to skip, from the previous page's hint"}
    incident_prop = _str("Incident id (e.g. PT4KHLK), not its number")

    def _tool(name: str, description: str, properties: dict, required: list[str]) -> ToolDefinition:
        return ToolDefinition(
            name=name,
            description=description,
            input_schema={"type": "object", "properties": properties, "required": required},
            source=_SOURCE,
        )

    return [
        _tool(
            "list_pagerduty_incidents",
            "List PagerDuty incidents, newest first, filtered by status, urgency, service and time range.",
            {
                "statuses": {
                    "type": "array",
                    "items": {"type": "string", "enum": list(INCIDENT_STATUSES)},
                    "description": "Statuses to keep (default: all)",
                },
                "urgency": {"type": "string", "enum": list(_URGENCIES)},
                "service_ids": _id_list("Service ids from list_pagerduty_services"),
                "since": _str("Start of the range, ISO 8601"),
                "until": _str("End of the range, ISO 8601"),
                "limit": limit_prop,
                "offset": offset_prop,
            },
            [],
        ),
        _tool(
            "get_pagerduty_incident",
            "Read one PagerDuty incident: status, urgency, service, assignees, escalation policy, details and link.",
            {"incident_id": incident_prop},
            ["incident_id"],
        ),
        _tool(
            "create_pagerduty_incident",
            "Open a PagerDuty incident on a service so its on-call responders are paged, e.g. for a "
            "critical exposure that needs action now. incident_key de-duplicates: a second call with "
            "the same key while the first incident is open adds nothing.",
            {
                "service_id": _str("Service id from list_pagerduty_services"),
                "title": _str("Incident title"),
                "details": _str("Incident body: what was found and what to do"),
                "urgency": {"type": "string", "enum": list(_URGENCIES), "description": "Default high"},
                "escalation_policy_id": _str("Escalation policy to use instead of the service's"),
                "assignee_ids": _id_list("User ids to assign instead of escalating"),
                "incident_key": _str("De-duplication key"),
            },
            ["service_id", "title"],
        ),
        _tool(
            "update_pagerduty_incident",
            "Acknowledge or resolve a PagerDuty incident (with a resolution note), change its urgency "
            "or title, escalate it to a level, or reassign it.",
            {
                "incident_id": incident_prop,
                "status": {"type": "string", "enum": ["acknowledged", "resolved"]},
                "urgency": {"type": "string", "enum": list(_URGENCIES)},
                "title": _str("New title"),
                "escalation_level": {"type": "integer", "description": "Escalation level to move to (1 = first)"},
                "assignee_ids": _id_list("User ids to reassign to"),
                "resolution": _str("Resolution note, with status=resolved"),
            },
            ["incident_id"],
        ),
        _tool(
            "list_pagerduty_incident_notes",
            "List the notes on a PagerDuty incident.",
            {"incident_id": incident_prop},
            ["incident_id"],
        ),
        _tool(
            "add_pagerduty_incident_note",
            "Add a note to a PagerDuty incident (findings, remediation progress).",
            {"incident_id": incident_prop, "content": _str("Note text")},
            ["incident_id", "content"],
        ),
        _tool(
            "list_pagerduty_log_entries",
            "List PagerDuty log entries (triggers, notifications, acknowledgements, escalations...) "
            "of the account or of one incident.",
            {
                "incident_id": _str("Only this incident's entries"),
                "since": _str("Start of the range, ISO 8601"),
                "until": _str("End of the range, ISO 8601"),
                "limit": limit_prop,
                "offset": offset_prop,
            },
            [],
        ),
        _tool(
            "get_pagerduty_log_entry",
            "Read one PagerDuty log entry.",
            {"log_entry_id": _str("Log entry id")},
            ["log_entry_id"],
        ),
        _tool(
            "list_pagerduty_services",
            "List PagerDuty services (the id create_pagerduty_incident needs), optionally by name.",
            {"query": _str("Part of the service name"), "limit": limit_prop},
            [],
        ),
        _tool(
            "list_pagerduty_escalation_policies",
            "List PagerDuty escalation policies, optionally by name.",
            {"query": _str("Part of the policy name"), "limit": limit_prop},
            [],
        ),
        _tool(
            "list_pagerduty_oncalls",
            "List who is on call now, per escalation policy level, optionally for some policies or schedules.",
            {
                "escalation_policy_ids": _id_list("Escalation policy ids"),
                "schedule_ids": _id_list("Schedule ids"),
            },
            [],
        ),
        _tool(
            "get_pagerduty_user",
            "Read a PagerDuty user: name, email, role, time zone and teams.",
            {"user_id": _str("User id")},
            ["user_id"],
        ),
    ]
