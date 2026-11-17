"""Splunk Enterprise Security notable events + correlation searches.

Notable events are the ES incident queue. They are read by running SPL over the
``notable`` index (via the `` `notable` `` macro, which enriches each row with
human-readable status/owner labels) and triaged/updated through the dedicated
``/services/notable_update`` endpoint. Correlation searches are the ES detection
rules that generate them.

These tools require Splunk Enterprise Security; without it the underlying search
or endpoint returns a formatted error the agent can relay to the user.
"""

from __future__ import annotations

from typing import Any

from app.integrations.http import format_entity_list
from app.tools.base import CallableToolExecutor, ToolDefinition

from ._common import (
    DEFAULT_NOTABLE_STATUSES,
    DEFAULT_SEARCH_LATEST,
    MAX_PAGE,
    MAX_RESULTS,
    NOTABLE_URGENCIES,
    SOURCE,
    ApiFn,
    content,
    dt,
    entries,
    execute_search,
    safe_int,
    spl_quote,
    trunc,
    urgency_icon,
)

# Reverse map: status label (lowercased) -> default reviewstatuses.conf id.
_STATUS_NAME_TO_ID = {v.lower(): k for k, v in DEFAULT_NOTABLE_STATUSES.items()}

# Fields surfaced for each notable event (constrains the `| table` projection).
_NOTABLE_FIELDS = (
    "_time event_id rule_name search_name urgency status status_label owner "
    "security_domain src dest signature"
)


def _resolve_status(value: str) -> tuple[str, str | None]:
    """Map a notable status (numeric id or label) to its reviewstatuses id."""
    raw = ("" if value is None else str(value)).strip()
    if not raw:
        return "", None
    if raw.isdigit():
        return raw, None
    key = raw.lower()
    if key in _STATUS_NAME_TO_ID:
        return _STATUS_NAME_TO_ID[key], None
    valid = ", ".join(f"{k}={v}" for k, v in DEFAULT_NOTABLE_STATUSES.items())
    return "", (
        f"Error: invalid notable status '{raw}'. Pass a status id or one of the "
        f"default labels ({valid}); call list_splunk_es_notable_statuses for the "
        "live list configured on this deployment."
    )


def register(base_url: str, api: ApiFn, executor: CallableToolExecutor) -> list[ToolDefinition]:
    """Register the Splunk Enterprise Security notable-event tools."""

    def _fmt_notable(r: dict) -> str:
        return (
            f"  {urgency_icon(r.get('urgency'))} [{r.get('urgency', '?')}] "
            f"{trunc(r.get('rule_name') or r.get('search_name'), 90) or 'Notable'}\n"
            f"    event_id: {r.get('event_id', '?')}\n"
            f"    status: {r.get('status_label') or r.get('status', '?')} | "
            f"owner: {r.get('owner') or 'unassigned'} | "
            f"domain: {r.get('security_domain', '?')}\n"
            f"    time: {dt(r.get('_time'))} | src: {r.get('src', '?')} | "
            f"dest: {r.get('dest', '?')}"
        )

    async def list_splunk_es_notable_events(
        status: str = "",
        urgency: str = "",
        owner: str = "",
        search_name: str = "",
        earliest_time: str = "-24h",
        limit: int = 25,
    ) -> str:
        """List Enterprise Security notable events (the incident queue / gathering incidents).

        Filter by status (label or id), urgency, owner, and/or the correlation
        search_name that created them. ``earliest_time`` is a Splunk relative time
        (default -24h). Returns each notable's event_id (use it with
        update_splunk_es_notable_event).
        """
        limit = safe_int(limit, 25, lo=1, hi=MAX_RESULTS)
        clauses: list[str] = ["`notable`"]
        if status:
            status_id, serr = _resolve_status(status)
            # status filter accepts either the numeric status or its label
            if serr:
                clauses.append(f'status_label="{spl_quote(status)}"')
            else:
                clauses.append(f"status={status_id}")
        if urgency:
            clauses.append(f'urgency="{spl_quote(urgency.lower())}"')
        if owner:
            clauses.append(f'owner="{spl_quote(owner)}"')
        if search_name:
            clauses.append(f'search_name="{spl_quote(search_name)}"')
        spl = (
            " ".join(clauses)
            + f" | head {limit} | table {_NOTABLE_FIELDS}"
        )
        results, statusmsg = await execute_search(
            api, spl, earliest_time, DEFAULT_SEARCH_LATEST, limit
        )
        if results is None:
            return statusmsg or "Error: the notable-event search returned no results."
        return format_entity_list(results, _fmt_notable, noun="notable event")

    async def get_splunk_es_notable_event(event_id: str) -> str:
        """Get full detail of a single Enterprise Security notable event by event_id."""
        if not event_id or not event_id.strip():
            return "Error: a notable event_id is required."
        spl = f'`notable` event_id="{spl_quote(event_id.strip())}" | head 1'
        results, statusmsg = await execute_search(api, spl, "-7d", DEFAULT_SEARCH_LATEST, 1)
        if results is None:
            return statusmsg or "Error: the notable lookup returned no results."
        if not results:
            return f"Notable event '{event_id}' not found in the last 7 days."
        r = results[0]
        # Show the salient triage fields first, then the raw event.
        lines = [
            f"Notable event: {trunc(r.get('rule_name') or r.get('search_name'), 200)}",
            f"event_id: {r.get('event_id', event_id)}",
            f"Urgency: {urgency_icon(r.get('urgency'))} {r.get('urgency', '?')} | "
            f"Status: {r.get('status_label') or r.get('status', '?')} | "
            f"Owner: {r.get('owner') or 'unassigned'}",
            f"Security domain: {r.get('security_domain', '?')} | "
            f"Time: {dt(r.get('_time'))}",
            f"src: {r.get('src', '?')} | dest: {r.get('dest', '?')} | "
            f"user: {r.get('user', r.get('src_user', '?'))}",
        ]
        if r.get("signature"):
            lines.append(f"Signature: {trunc(r.get('signature'), 200)}")
        if r.get("_raw"):
            lines.append(f"\nRaw event:\n{trunc(r.get('_raw'), 1500)}")
        return "\n".join(lines)

    async def update_splunk_es_notable_event(
        event_id: str,
        status: str = "",
        urgency: str = "",
        owner: str = "",
        comment: str = "",
        disposition: str = "",
    ) -> str:
        """Update an ES notable event: status, urgency, owner, comment, disposition.

        Triage and response action on a notable. Provide at least one of status,
        urgency, owner, or comment. ``status`` accepts a label (New, In Progress,
        Pending, Resolved, Closed) or a numeric id; ``urgency`` is informational/
        low/medium/high/critical; ``disposition`` is a disposition id (e.g.
        'disposition:2') used when closing as a (non-)true-positive.
        """
        if not event_id or not event_id.strip():
            return "Error: a notable event_id is required."
        if not any([status, urgency, owner, comment, disposition]):
            return (
                "Error: provide at least one of status, urgency, owner, comment, or "
                "disposition to update the notable event."
            )
        body: dict[str, Any] = {"ruleUIDs": event_id.strip()}
        if status:
            status_id, serr = _resolve_status(status)
            if serr:
                return serr
            body["status"] = status_id
        if urgency:
            urg = urgency.strip().lower()
            if urg not in NOTABLE_URGENCIES:
                return f"Error: urgency must be one of {', '.join(NOTABLE_URGENCIES)}."
            body["urgency"] = urg
        if owner:
            body["newOwner"] = owner.strip()
        if comment:
            body["comment"] = comment
        if disposition:
            body["disposition"] = disposition.strip()
        data, err = await api(
            "post", "/services/notable_update", "update notable event", data=body
        )
        if err:
            return err
        if isinstance(data, dict):
            if data.get("success") is False:
                return f"Error: Splunk rejected the notable update — {data.get('message', 'unknown error')}."
            if data.get("message"):
                return f"Notable event {event_id} updated: {data.get('message')}"
        return f"Notable event {event_id} updated."

    async def list_splunk_es_notable_statuses() -> str:
        """List the Enterprise Security notable review statuses (id -> label).

        Needed to map a status name to the id expected by
        update_splunk_es_notable_event.
        """
        data, err = await api(
            "get",
            "/servicesNS/nobody/SplunkEnterpriseSecuritySuite/configs/conf-reviewstatuses",
            "list notable statuses",
            params={"count": 0},
        )
        if err:
            # Fall back to the documented defaults so the agent still has a map.
            defaults = "\n".join(
                f"  {k} = {v}" for k, v in DEFAULT_NOTABLE_STATUSES.items()
            )
            return (
                "Could not read the live review statuses (Enterprise Security may "
                "not be installed or the token lacks access). Default statuses:\n"
                f"{defaults}"
            )
        ents = entries(data)
        if not ents:
            defaults = "\n".join(
                f"  {k} = {v}" for k, v in DEFAULT_NOTABLE_STATUSES.items()
            )
            return f"No review statuses returned. Default statuses:\n{defaults}"

        def _fmt(e: dict) -> str:
            c = content(e)
            label = c.get("label") or e.get("name", "?")
            disabled = str(c.get("disabled", "0")) in ("1", "true", "True")
            return (
                f"  {e.get('name', '?')} = {label}"
                + (" (disabled)" if disabled else "")
                + (" [default]" if str(c.get("default", "0")) in ("1", "true", "True") else "")
            )

        return format_entity_list(ents, _fmt, noun="review status")

    async def list_splunk_es_correlation_searches(
        name_filter: str = "", limit: int = 25
    ) -> str:
        """List Enterprise Security correlation searches (the detection rules).

        Correlation searches are the saved searches that generate notable events.
        """
        params: dict[str, Any] = {
            "count": safe_int(limit, 25, lo=1, hi=MAX_PAGE),
            "search": "action.correlationsearch.enabled=1",
        }
        if name_filter:
            params["search"] += f" name=*{name_filter}*"
        data, err = await api(
            "get", "/services/saved/searches", "list correlation searches", params=params
        )
        if err:
            return err

        def _fmt(e: dict) -> str:
            c = content(e)
            disabled = str(c.get("disabled", "0")) in ("1", "true", "True")
            return (
                f"  {e.get('name', '?')}"
                + (" [disabled]" if disabled else "")
                + f" | severity: {c.get('alert.severity') or '?'}\n"
                f"    {trunc(c.get('action.correlationsearch.label') or c.get('description') or c.get('search'), 160)}"
            )

        return format_entity_list(entries(data), _fmt, noun="correlation search")

    # ── register ────────────────────────────────────────────────────────
    executor.register("list_splunk_es_notable_events", list_splunk_es_notable_events)
    executor.register("get_splunk_es_notable_event", get_splunk_es_notable_event)
    executor.register("update_splunk_es_notable_event", update_splunk_es_notable_event)
    executor.register("list_splunk_es_notable_statuses", list_splunk_es_notable_statuses)
    executor.register(
        "list_splunk_es_correlation_searches", list_splunk_es_correlation_searches
    )

    return [
        ToolDefinition(
            name="list_splunk_es_notable_events",
            description=(
                "List Splunk Enterprise Security notable events (the incident queue). "
                "Filter by status (label or id), urgency (informational/low/medium/"
                "high/critical), owner, and/or the correlation search_name. Returns "
                "each notable's event_id for follow-up updates. Requires Enterprise "
                "Security."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "status": {"type": "string", "description": "Status label or id"},
                    "urgency": {
                        "type": "string",
                        "enum": list(NOTABLE_URGENCIES),
                    },
                    "owner": {"type": "string"},
                    "search_name": {
                        "type": "string",
                        "description": "Correlation search that created the notable",
                    },
                    "earliest_time": {"type": "string", "default": "-24h"},
                    "limit": {"type": "integer", "default": 25},
                },
                "required": [],
            },
            source=SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="get_splunk_es_notable_event",
            description=(
                "Get full detail of a single Enterprise Security notable event by "
                "event_id (urgency, status, owner, security domain, src/dest, and the "
                "raw event)."
            ),
            input_schema={
                "type": "object",
                "properties": {"event_id": {"type": "string"}},
                "required": ["event_id"],
            },
            source=SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="update_splunk_es_notable_event",
            description=(
                "Update / triage an Enterprise Security notable event: set status "
                "(New, In Progress, Pending, Resolved, Closed, or a numeric id), "
                "urgency, owner (newOwner), add a comment, and/or set a disposition "
                "id when closing. Provide at least one field. This is the core ES "
                "triage & response action."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "event_id": {"type": "string"},
                    "status": {"type": "string", "description": "Status label or id"},
                    "urgency": {"type": "string", "enum": list(NOTABLE_URGENCIES)},
                    "owner": {"type": "string", "description": "New owner (analyst)"},
                    "comment": {"type": "string", "description": "Investigation note"},
                    "disposition": {
                        "type": "string",
                        "description": "Disposition id, e.g. 'disposition:2'",
                    },
                },
                "required": ["event_id"],
            },
            source=SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="list_splunk_es_notable_statuses",
            description=(
                "List the Enterprise Security notable review statuses (id -> label) so "
                "you can map a status name to the id update_splunk_es_notable_event "
                "expects."
            ),
            input_schema={"type": "object", "properties": {}, "required": []},
            source=SOURCE,
            priority=2,
        ),
        ToolDefinition(
            name="list_splunk_es_correlation_searches",
            description=(
                "List Enterprise Security correlation searches (the detection rules "
                "that generate notable events), with severity and enabled state."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "name_filter": {"type": "string"},
                    "limit": {"type": "integer", "default": 25},
                },
                "required": [],
            },
            source=SOURCE,
            priority=1,
        ),
    ]
