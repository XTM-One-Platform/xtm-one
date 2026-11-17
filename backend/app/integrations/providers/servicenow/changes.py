"""ServiceNow change management (the ``change_request`` table).

list / get / create / update change requests and add work notes. Reference
fields take a sys_id; choice fields take their numeric/string value.
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

_TABLE = "change_request"
# State model values differ per instance; these are the common OOB "state model"
# values surfaced to the agent as guidance.
_STATES = (
    "-5=New, -4=Assess, -3=Authorize, -2=Scheduled, -1=Implement, 0=Review, "
    "3=Closed, 4=Canceled"
)
_TYPES = "normal, standard, emergency"
_LIST_FIELDS = (
    "number,short_description,state,type,risk,assignment_group,assigned_to,"
    "start_date,end_date,sys_updated_on,sys_id"
)


def _format_line(rec: dict[str, Any]) -> str:
    return (
        f"  {disp(rec.get('number'))}: {truncate(disp(rec.get('short_description')), 90)}\n"
        f"    State: {disp(rec.get('state')) or '?'} | "
        f"Type: {disp(rec.get('type')) or '?'} | Risk: {disp(rec.get('risk')) or '?'} | "
        f"Assigned: {disp(rec.get('assigned_to')) or 'Unassigned'}\n"
        f"    Window: {disp(rec.get('start_date')) or '?'} -> {disp(rec.get('end_date')) or '?'} | "
        f"sys_id: {disp(rec.get('sys_id'))}"
    )


def _detail_formatter(base: str) -> Any:
    def _format(rec: dict[str, Any]) -> str:
        lines = [f"{disp(rec.get('number'))}: {disp(rec.get('short_description'))}"]
        lines += field_lines(
            rec,
            [
                ("State", "state"),
                ("Type", "type"),
                ("Risk", "risk"),
                ("Impact", "impact"),
                ("Priority", "priority"),
                ("Category", "category"),
                ("Assignment group", "assignment_group"),
                ("Assigned to", "assigned_to"),
                ("Requested by", "requested_by"),
                ("Planned start", "start_date"),
                ("Planned end", "end_date"),
                ("Updated", "sys_updated_on"),
            ],
        )
        for label, key in (
            ("Description", "description"),
            ("Justification", "justification"),
            ("Implementation plan", "implementation_plan"),
            ("Backout plan", "backout_plan"),
            ("Test plan", "test_plan"),
        ):
            value = disp(rec.get(key))
            if value:
                lines.append(f"\n  {label}: {truncate(value, _MAX_BODY_CHARS)}")
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

    async def list_servicenow_changes(
        query: str = "",
        state: str = "",
        type: str = "",
        assignment_group: str = "",
        assigned_to: str = "",
        active: bool | None = None,
        text: str = "",
        limit: int = 20,
        offset: int = 0,
    ) -> str:
        """List/search change requests (newest-first).

        ``type`` is one of normal/standard/emergency. ``state`` is the numeric
        state-model value. Reference fields take a sys_id. ``query`` is a raw
        encoded query for advanced filters.
        """
        encoded = build_encoded_query(
            {
                "state": state,
                "type": type,
                "assignment_group": assignment_group,
                "assigned_to": assigned_to,
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
            "list changes",
            _format_line,
            noun="change request",
            query=encoded,
            limit=limit,
            offset=offset,
            fields=_LIST_FIELDS,
        )

    async def get_servicenow_change(change: str) -> str:
        """Get full detail of a change request by number (CHG...) or sys_id."""
        return await get_record(
            base, headers, _TABLE, change, f"get change {change}", detail
        )

    async def create_servicenow_change(
        short_description: str,
        description: str = "",
        type: str = "normal",
        assignment_group: str = "",
        assigned_to: str = "",
        risk: str = "",
        impact: str = "",
        extra_fields: dict[str, Any] | None = None,
    ) -> str:
        """Create a change request.

        ``short_description`` is required. ``type`` is normal/standard/emergency.
        Reference fields take a sys_id. ``extra_fields`` sets any other column
        (e.g. start_date/end_date as 'YYYY-MM-DD HH:MM:SS', implementation_plan,
        cmdb_ci).
        """
        if not short_description or not short_description.strip():
            return "Error: short_description is required to create a change."
        body: dict[str, Any] = {
            "short_description": short_description.strip(),
            "type": type or "normal",
        }
        for key, value in {
            "description": description,
            "assignment_group": assignment_group,
            "assigned_to": assigned_to,
            "risk": risk,
            "impact": impact,
        }.items():
            if value:
                body[key] = value
        if extra_fields:
            body.update(extra_fields)
        return await create_record(
            base, headers, _TABLE, "create change", body, detail
        )

    async def update_servicenow_change(
        change: str,
        short_description: str = "",
        state: str = "",
        assignment_group: str = "",
        assigned_to: str = "",
        risk: str = "",
        work_notes: str = "",
        extra_fields: dict[str, Any] | None = None,
    ) -> str:
        """Update a change request by number or sys_id (drives state transitions).

        ``state`` is the numeric state-model value (-5=New, -4=Assess,
        -3=Authorize, -2=Scheduled, -1=Implement, 0=Review, 3=Closed,
        4=Canceled). ``work_notes`` appends to the activity log. ``extra_fields``
        sets any other column (start_date/end_date, implementation_plan, ...).
        """
        body: dict[str, Any] = {}
        for key, value in {
            "short_description": short_description,
            "state": state,
            "assignment_group": assignment_group,
            "assigned_to": assigned_to,
            "risk": risk,
            "work_notes": work_notes,
        }.items():
            if value:
                body[key] = value
        if extra_fields:
            body.update(extra_fields)
        return await update_record(
            base, headers, _TABLE, change, f"update change {change}", body, detail
        )

    executor.register("list_servicenow_changes", list_servicenow_changes)
    executor.register("get_servicenow_change", get_servicenow_change)
    executor.register("create_servicenow_change", create_servicenow_change)
    executor.register("update_servicenow_change", update_servicenow_change)

    _limit = {"type": "integer", "default": 20, "description": "Max records (<=100)"}
    _offset = {"type": "integer", "default": 0, "description": "Pagination offset"}
    return [
        ToolDefinition(
            name="list_servicenow_changes",
            description=(
                "List/search ServiceNow change requests (newest-first) by state, "
                "type (normal/standard/emergency), assignment_group, assigned_to, "
                "active, text, or a raw encoded query."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Raw encoded query"},
                    "state": {"type": "string", "description": _STATES},
                    "type": {"type": "string", "enum": ["normal", "standard", "emergency"]},
                    "assignment_group": {"type": "string", "description": "Group sys_id"},
                    "assigned_to": {"type": "string", "description": "Assignee sys_id"},
                    "active": {"type": "boolean"},
                    "text": {"type": "string", "description": "Match short description"},
                    "limit": _limit,
                    "offset": _offset,
                },
                "required": [],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="get_servicenow_change",
            description="Get full detail of one change request by number (CHG...) or sys_id.",
            input_schema={
                "type": "object",
                "properties": {
                    "change": {"type": "string", "description": "Change number or sys_id"}
                },
                "required": ["change"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="create_servicenow_change",
            description=(
                "Create a ServiceNow change request. short_description required; "
                f"type one of {_TYPES}; reference fields take a sys_id. Use "
                "extra_fields for planning columns (start_date, end_date, "
                "implementation_plan, ...)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "short_description": {"type": "string"},
                    "description": {"type": "string"},
                    "type": {
                        "type": "string",
                        "enum": ["normal", "standard", "emergency"],
                        "default": "normal",
                    },
                    "assignment_group": {"type": "string", "description": "Group sys_id"},
                    "assigned_to": {"type": "string", "description": "Assignee sys_id"},
                    "risk": {"type": "string"},
                    "impact": {"type": "string", "description": "1=High,2=Medium,3=Low"},
                    "extra_fields": {
                        "type": "object",
                        "description": "Any other change columns (raw values)",
                    },
                },
                "required": ["short_description"],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="update_servicenow_change",
            description=(
                "Update a change request by number or sys_id (the way to drive "
                f"state transitions). state is numeric ({_STATES}). work_notes "
                "appends to the activity log; extra_fields sets any other column."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "change": {"type": "string", "description": "Number or sys_id"},
                    "short_description": {"type": "string"},
                    "state": {"type": "string", "description": _STATES},
                    "assignment_group": {"type": "string", "description": "Group sys_id"},
                    "assigned_to": {"type": "string", "description": "Assignee sys_id"},
                    "risk": {"type": "string"},
                    "work_notes": {"type": "string"},
                    "extra_fields": {
                        "type": "object",
                        "description": "Any other change columns (raw values)",
                    },
                },
                "required": ["change"],
            },
            source=_SOURCE,
            priority=1,
        ),
    ]
