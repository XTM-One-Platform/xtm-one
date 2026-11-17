"""ServiceNow incident management (the ``incident`` table) - the ITSM core.

list / get / create / update incidents, add work notes or customer comments, and
resolve / close with a close code + close notes. Reference fields (assigned_to,
assignment_group, caller_id) take a sys_id - resolve names via
``search_servicenow_users`` / ``search_servicenow_groups``. Choice fields take
their numeric value.
"""

from __future__ import annotations

from typing import Any

from app.tools.base import CallableToolExecutor, ToolDefinition

from ._common import (
    _MAX_BODY_CHARS,
    _SOURCE,
    build_encoded_query,
    create_record,
    disp,
    field_lines,
    get_record,
    list_table,
    record_url,
    truncate,
    update_record,
)

_TABLE = "incident"

# OOB incident state choice values (the agent passes the number).
_STATES = "1=New, 2=In Progress, 3=On Hold, 6=Resolved, 7=Closed, 8=Canceled"
# Compact column set for list views (keeps the payload + LLM context small).
_LIST_FIELDS = (
    "number,short_description,state,priority,assigned_to,assignment_group,"
    "caller_id,opened_at,sys_updated_on,sys_id"
)


def _format_line(rec: dict[str, Any]) -> str:
    return (
        f"  {disp(rec.get('number'))}: {truncate(disp(rec.get('short_description')), 90)}\n"
        f"    State: {disp(rec.get('state')) or '?'} | "
        f"Priority: {disp(rec.get('priority')) or '?'} | "
        f"Assigned: {disp(rec.get('assigned_to')) or 'Unassigned'} "
        f"({disp(rec.get('assignment_group')) or 'no group'})\n"
        f"    Caller: {disp(rec.get('caller_id')) or '?'} | "
        f"Updated: {disp(rec.get('sys_updated_on'))} | sys_id: {disp(rec.get('sys_id'))}"
    )


def _detail_formatter(base: str) -> Any:
    def _format(rec: dict[str, Any]) -> str:
        lines = [
            f"{disp(rec.get('number'))}: {disp(rec.get('short_description'))}",
        ]
        lines += field_lines(
            rec,
            [
                ("State", "state"),
                ("Priority", "priority"),
                ("Urgency", "urgency"),
                ("Impact", "impact"),
                ("Category", "category"),
                ("Assigned to", "assigned_to"),
                ("Assignment group", "assignment_group"),
                ("Caller", "caller_id"),
                ("Opened", "opened_at"),
                ("Updated", "sys_updated_on"),
                ("Close code", "close_code"),
            ],
        )
        description = disp(rec.get("description"))
        if description:
            lines.append(f"\n  Description: {truncate(description, _MAX_BODY_CHARS)}")
        close_notes = disp(rec.get("close_notes"))
        if close_notes:
            lines.append(f"  Close notes: {truncate(close_notes, _MAX_BODY_CHARS)}")
        sys_id = disp(rec.get("sys_id"))
        link = record_url(base, _TABLE, sys_id)
        if link:
            lines.append(f"  Link: {link}")
        lines.append(f"  sys_id: {sys_id}")
        return "\n".join(lines)

    return _format


def register(
    base: str, headers: dict[str, str], executor: CallableToolExecutor
) -> list[ToolDefinition]:
    detail = _detail_formatter(base)

    async def list_servicenow_incidents(
        query: str = "",
        state: str = "",
        priority: str = "",
        assignment_group: str = "",
        assigned_to: str = "",
        caller: str = "",
        active: bool | None = None,
        text: str = "",
        limit: int = 20,
        offset: int = 0,
    ) -> str:
        """List/search incidents with structured filters.

        Prefer the structured params over a raw query. ``state`` / ``priority``
        are numeric choice values; ``assignment_group`` / ``assigned_to`` /
        ``caller`` take a sys_id (resolve names with search_servicenow_groups /
        search_servicenow_users). ``active=true`` hides resolved/closed records.
        ``text`` matches the short description (contains). ``query`` is a raw
        ServiceNow encoded query (e.g. ``priority=1^assigned_toISEMPTY``) for
        anything the structured params cannot express. Results are newest-first.
        """
        encoded = build_encoded_query(
            {
                "state": state,
                "priority": priority,
                "assignment_group": assignment_group,
                "assigned_to": assigned_to,
                "caller_id": caller,
                "active": (
                    None if active is None else ("true" if active else "false")
                ),
            },
            raw_query=query,
            text=text,
            order_by="sys_updated_on",
        )
        return await list_table(
            base,
            headers,
            _TABLE,
            "list incidents",
            _format_line,
            noun="incident",
            query=encoded,
            limit=limit,
            offset=offset,
            fields=_LIST_FIELDS,
        )

    async def get_servicenow_incident(incident: str) -> str:
        """Get full detail of an incident by number (INC0010001) or sys_id."""
        return await get_record(
            base, headers, _TABLE, incident, f"get incident {incident}", detail
        )

    async def create_servicenow_incident(
        short_description: str,
        description: str = "",
        caller_id: str = "",
        assignment_group: str = "",
        assigned_to: str = "",
        urgency: str = "",
        impact: str = "",
        category: str = "",
        extra_fields: dict[str, Any] | None = None,
    ) -> str:
        """Open a new incident.

        ``short_description`` is required. Reference fields (caller_id,
        assignment_group, assigned_to) take a sys_id. urgency/impact are numeric
        (1=High, 2=Medium, 3=Low) and together drive the computed priority.
        ``extra_fields`` sets any other column directly (raw values), e.g.
        ``{"cmdb_ci": "<sys_id>", "subcategory": "email"}``.
        """
        if not short_description or not short_description.strip():
            return "Error: short_description is required to create an incident."
        body: dict[str, Any] = {"short_description": short_description.strip()}
        for key, value in {
            "description": description,
            "caller_id": caller_id,
            "assignment_group": assignment_group,
            "assigned_to": assigned_to,
            "urgency": urgency,
            "impact": impact,
            "category": category,
        }.items():
            if value:
                body[key] = value
        if extra_fields:
            body.update(extra_fields)
        return await create_record(
            base, headers, _TABLE, "create incident", body, detail
        )

    async def update_servicenow_incident(
        incident: str,
        short_description: str = "",
        state: str = "",
        priority: str = "",
        urgency: str = "",
        impact: str = "",
        assignment_group: str = "",
        assigned_to: str = "",
        category: str = "",
        extra_fields: dict[str, Any] | None = None,
    ) -> str:
        """Update incident fields by number or sys_id.

        Only the provided fields change. ``state`` is numeric (1=New, 2=In
        Progress, 3=On Hold, 6=Resolved, 7=Closed, 8=Canceled). Reference fields
        take a sys_id. Use add_servicenow_incident_work_note for work notes/
        comments and resolve_servicenow_incident / close_servicenow_incident to
        close out. ``extra_fields`` sets any other column directly.
        """
        body: dict[str, Any] = {}
        for key, value in {
            "short_description": short_description,
            "state": state,
            "priority": priority,
            "urgency": urgency,
            "impact": impact,
            "assignment_group": assignment_group,
            "assigned_to": assigned_to,
            "category": category,
        }.items():
            if value:
                body[key] = value
        if extra_fields:
            body.update(extra_fields)
        return await update_record(
            base, headers, _TABLE, incident, f"update incident {incident}", body, detail
        )

    async def add_servicenow_incident_work_note(
        incident: str, note: str, public: bool = False
    ) -> str:
        """Add a work note (internal) or customer-visible comment to an incident.

        ``public=false`` (default) posts an internal work note; ``public=true``
        posts an "Additional comments" entry visible to the caller. The note is
        appended to the incident's activity stream (journal field).
        """
        if not note or not note.strip():
            return "Error: a non-empty note is required."
        field = "comments" if public else "work_notes"
        body = {field: note.strip()}
        result = await update_record(
            base,
            headers,
            _TABLE,
            incident,
            f"add {'comment' if public else 'work note'} to incident {incident}",
            body,
            lambda rec: (
                f"Added {'comment' if public else 'work note'} to "
                f"{disp(rec.get('number')) or incident}."
            ),
        )
        return result

    async def resolve_servicenow_incident(
        incident: str, close_notes: str, close_code: str = "Solved (Permanently)"
    ) -> str:
        """Resolve an incident (state -> Resolved) with a close code + notes.

        ``close_notes`` (the resolution) is required. ``close_code`` defaults to
        "Solved (Permanently)"; other OOB codes include "Solved (Workaround)",
        "Solved Remotely (Permanently)", "Not Solved (Not Reproducible)",
        "Not Solved (Too Costly)", "Closed/Resolved by Caller".
        """
        if not close_notes or not close_notes.strip():
            return "Error: close_notes (the resolution) is required to resolve."
        body = {
            "state": "6",
            "close_code": close_code or "Solved (Permanently)",
            "close_notes": close_notes.strip(),
        }
        return await update_record(
            base,
            headers,
            _TABLE,
            incident,
            f"resolve incident {incident}",
            body,
            lambda rec: f"Resolved incident {disp(rec.get('number')) or incident}.",
        )

    async def close_servicenow_incident(
        incident: str, close_notes: str, close_code: str = "Solved (Permanently)"
    ) -> str:
        """Close an incident (state -> Closed) with a close code + notes.

        Permanent state transition. ``close_notes`` is required.
        """
        if not close_notes or not close_notes.strip():
            return "Error: close_notes is required to close the incident."
        body = {
            "state": "7",
            "close_code": close_code or "Solved (Permanently)",
            "close_notes": close_notes.strip(),
        }
        return await update_record(
            base,
            headers,
            _TABLE,
            incident,
            f"close incident {incident}",
            body,
            lambda rec: f"Closed incident {disp(rec.get('number')) or incident}.",
        )

    executor.register("list_servicenow_incidents", list_servicenow_incidents)
    executor.register("get_servicenow_incident", get_servicenow_incident)
    executor.register("create_servicenow_incident", create_servicenow_incident)
    executor.register("update_servicenow_incident", update_servicenow_incident)
    executor.register(
        "add_servicenow_incident_work_note", add_servicenow_incident_work_note
    )
    executor.register("resolve_servicenow_incident", resolve_servicenow_incident)
    executor.register("close_servicenow_incident", close_servicenow_incident)

    _limit = {"type": "integer", "default": 20, "description": "Max records (<=100)"}
    _offset = {"type": "integer", "default": 0, "description": "Pagination offset"}
    return [
        ToolDefinition(
            name="list_servicenow_incidents",
            description=(
                "List/search ServiceNow incidents with structured filters (state, "
                "priority, assignment_group, assigned_to, caller, active, text) or "
                "a raw encoded query. Newest-first. state/priority are numeric; "
                "group/assignee/caller are sys_ids (resolve via search_servicenow_* "
                "tools)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Raw ServiceNow encoded query (advanced)",
                    },
                    "state": {
                        "type": "string",
                        "description": f"Incident state number ({_STATES})",
                    },
                    "priority": {
                        "type": "string",
                        "description": "Priority number (1=Critical..5=Planning)",
                    },
                    "assignment_group": {
                        "type": "string",
                        "description": "Assignment group sys_id",
                    },
                    "assigned_to": {"type": "string", "description": "Assignee sys_id"},
                    "caller": {"type": "string", "description": "Caller sys_id"},
                    "active": {
                        "type": "boolean",
                        "description": "true = only open incidents",
                    },
                    "text": {
                        "type": "string",
                        "description": "Match the short description (contains)",
                    },
                    "limit": _limit,
                    "offset": _offset,
                },
                "required": [],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="get_servicenow_incident",
            description=(
                "Get full detail of one incident by number (e.g. INC0010001) or "
                "sys_id, including a deep link to the record in ServiceNow."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "incident": {
                        "type": "string",
                        "description": "Incident number or sys_id",
                    }
                },
                "required": ["incident"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="create_servicenow_incident",
            description=(
                "Open a new ServiceNow incident. short_description is required; "
                "reference fields (caller_id, assignment_group, assigned_to) take "
                "a sys_id; urgency/impact are numeric (1=High,2=Medium,3=Low). Use "
                "extra_fields for any other column."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "short_description": {"type": "string"},
                    "description": {"type": "string"},
                    "caller_id": {"type": "string", "description": "Caller sys_id"},
                    "assignment_group": {
                        "type": "string",
                        "description": "Assignment group sys_id",
                    },
                    "assigned_to": {"type": "string", "description": "Assignee sys_id"},
                    "urgency": {"type": "string", "description": "1=High,2=Medium,3=Low"},
                    "impact": {"type": "string", "description": "1=High,2=Medium,3=Low"},
                    "category": {"type": "string"},
                    "extra_fields": {
                        "type": "object",
                        "description": "Any other incident columns (raw values)",
                    },
                },
                "required": ["short_description"],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="update_servicenow_incident",
            description=(
                "Update fields on an incident by number or sys_id (only the fields "
                f"you pass change). state is numeric ({_STATES}). Reference fields "
                "take a sys_id. For notes use add_servicenow_incident_work_note; to "
                "close out use resolve_servicenow_incident or close_servicenow_incident."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "incident": {"type": "string", "description": "Number or sys_id"},
                    "short_description": {"type": "string"},
                    "state": {"type": "string", "description": _STATES},
                    "priority": {"type": "string"},
                    "urgency": {"type": "string"},
                    "impact": {"type": "string"},
                    "assignment_group": {"type": "string", "description": "Group sys_id"},
                    "assigned_to": {"type": "string", "description": "Assignee sys_id"},
                    "category": {"type": "string"},
                    "extra_fields": {
                        "type": "object",
                        "description": "Any other incident columns (raw values)",
                    },
                },
                "required": ["incident"],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="add_servicenow_incident_work_note",
            description=(
                "Add a work note (internal, default) or a customer-visible comment "
                "(public=true) to an incident's activity log."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "incident": {"type": "string", "description": "Number or sys_id"},
                    "note": {"type": "string"},
                    "public": {
                        "type": "boolean",
                        "default": False,
                        "description": "true = customer-visible comment",
                    },
                },
                "required": ["incident", "note"],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="resolve_servicenow_incident",
            description=(
                "Resolve an incident (state -> Resolved) with a required close_notes "
                "(resolution) and a close_code (default 'Solved (Permanently)')."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "incident": {"type": "string", "description": "Number or sys_id"},
                    "close_notes": {"type": "string", "description": "The resolution"},
                    "close_code": {
                        "type": "string",
                        "default": "Solved (Permanently)",
                    },
                },
                "required": ["incident", "close_notes"],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="close_servicenow_incident",
            description=(
                "Close an incident (state -> Closed) with a required close_notes and "
                "a close_code. Permanent state transition."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "incident": {"type": "string", "description": "Number or sys_id"},
                    "close_notes": {"type": "string"},
                    "close_code": {
                        "type": "string",
                        "default": "Solved (Permanently)",
                    },
                },
                "required": ["incident", "close_notes"],
            },
            source=_SOURCE,
            priority=1,
        ),
    ]
