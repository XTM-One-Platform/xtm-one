"""ServiceNow problem management (the ``problem`` table).

list / get / create / update problem records (root-cause / known-error tracking).
Reference fields take a sys_id.
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

_TABLE = "problem"
_LIST_FIELDS = (
    "number,short_description,state,priority,assignment_group,assigned_to,"
    "sys_updated_on,sys_id"
)


def _format_line(rec: dict[str, Any]) -> str:
    return (
        f"  {disp(rec.get('number'))}: {truncate(disp(rec.get('short_description')), 90)}\n"
        f"    State: {disp(rec.get('state')) or '?'} | "
        f"Priority: {disp(rec.get('priority')) or '?'} | "
        f"Assigned: {disp(rec.get('assigned_to')) or 'Unassigned'} "
        f"({disp(rec.get('assignment_group')) or 'no group'}) | "
        f"sys_id: {disp(rec.get('sys_id'))}"
    )


def _detail_formatter(base: str) -> Any:
    def _format(rec: dict[str, Any]) -> str:
        lines = [f"{disp(rec.get('number'))}: {disp(rec.get('short_description'))}"]
        lines += field_lines(
            rec,
            [
                ("State", "state"),
                ("Priority", "priority"),
                ("Assignment group", "assignment_group"),
                ("Assigned to", "assigned_to"),
                ("Related incident", "first_reported_by_task"),
                ("Updated", "sys_updated_on"),
            ],
        )
        for label, key in (
            ("Description", "description"),
            ("Cause notes", "cause_notes"),
            ("Workaround", "workaround"),
            ("Fix notes", "fix_notes"),
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

    async def list_servicenow_problems(
        query: str = "",
        state: str = "",
        priority: str = "",
        assignment_group: str = "",
        assigned_to: str = "",
        active: bool | None = None,
        text: str = "",
        limit: int = 20,
        offset: int = 0,
    ) -> str:
        """List/search problem records (newest-first).

        Reference fields take a sys_id; state/priority are numeric. ``query`` is
        a raw encoded query for advanced filters.
        """
        encoded = build_encoded_query(
            {
                "state": state,
                "priority": priority,
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
            "list problems",
            _format_line,
            noun="problem",
            query=encoded,
            limit=limit,
            offset=offset,
            fields=_LIST_FIELDS,
        )

    async def get_servicenow_problem(problem: str) -> str:
        """Get full detail of a problem by number (PRB...) or sys_id."""
        return await get_record(
            base, headers, _TABLE, problem, f"get problem {problem}", detail
        )

    async def create_servicenow_problem(
        short_description: str,
        description: str = "",
        assignment_group: str = "",
        assigned_to: str = "",
        extra_fields: dict[str, Any] | None = None,
    ) -> str:
        """Create a problem record. short_description is required.

        Reference fields take a sys_id. ``extra_fields`` sets any other column
        (e.g. cmdb_ci, cause_notes, workaround).
        """
        if not short_description or not short_description.strip():
            return "Error: short_description is required to create a problem."
        body: dict[str, Any] = {"short_description": short_description.strip()}
        for key, value in {
            "description": description,
            "assignment_group": assignment_group,
            "assigned_to": assigned_to,
        }.items():
            if value:
                body[key] = value
        if extra_fields:
            body.update(extra_fields)
        return await create_record(
            base, headers, _TABLE, "create problem", body, detail
        )

    async def update_servicenow_problem(
        problem: str,
        short_description: str = "",
        state: str = "",
        assignment_group: str = "",
        assigned_to: str = "",
        work_notes: str = "",
        extra_fields: dict[str, Any] | None = None,
    ) -> str:
        """Update a problem by number or sys_id.

        ``work_notes`` appends to the activity log. ``extra_fields`` sets any
        other column (cause_notes, workaround, fix_notes, ...).
        """
        body: dict[str, Any] = {}
        for key, value in {
            "short_description": short_description,
            "state": state,
            "assignment_group": assignment_group,
            "assigned_to": assigned_to,
            "work_notes": work_notes,
        }.items():
            if value:
                body[key] = value
        if extra_fields:
            body.update(extra_fields)
        return await update_record(
            base, headers, _TABLE, problem, f"update problem {problem}", body, detail
        )

    executor.register("list_servicenow_problems", list_servicenow_problems)
    executor.register("get_servicenow_problem", get_servicenow_problem)
    executor.register("create_servicenow_problem", create_servicenow_problem)
    executor.register("update_servicenow_problem", update_servicenow_problem)

    _limit = {"type": "integer", "default": 20, "description": "Max records (<=100)"}
    _offset = {"type": "integer", "default": 0, "description": "Pagination offset"}
    return [
        ToolDefinition(
            name="list_servicenow_problems",
            description=(
                "List/search ServiceNow problem records (newest-first) by state, "
                "priority, assignment_group, assigned_to, active, text, or a raw "
                "encoded query."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Raw encoded query"},
                    "state": {"type": "string"},
                    "priority": {"type": "string"},
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
            name="get_servicenow_problem",
            description="Get full detail of one problem by number (PRB...) or sys_id.",
            input_schema={
                "type": "object",
                "properties": {
                    "problem": {"type": "string", "description": "Problem number or sys_id"}
                },
                "required": ["problem"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="create_servicenow_problem",
            description=(
                "Create a ServiceNow problem record. short_description required; "
                "reference fields take a sys_id; extra_fields for any other column."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "short_description": {"type": "string"},
                    "description": {"type": "string"},
                    "assignment_group": {"type": "string", "description": "Group sys_id"},
                    "assigned_to": {"type": "string", "description": "Assignee sys_id"},
                    "extra_fields": {
                        "type": "object",
                        "description": "Any other problem columns (raw values)",
                    },
                },
                "required": ["short_description"],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="update_servicenow_problem",
            description=(
                "Update a problem by number or sys_id. work_notes appends to the "
                "activity log; extra_fields sets any other column (cause_notes, "
                "workaround, fix_notes, ...)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "problem": {"type": "string", "description": "Number or sys_id"},
                    "short_description": {"type": "string"},
                    "state": {"type": "string"},
                    "assignment_group": {"type": "string", "description": "Group sys_id"},
                    "assigned_to": {"type": "string", "description": "Assignee sys_id"},
                    "work_notes": {"type": "string"},
                    "extra_fields": {
                        "type": "object",
                        "description": "Any other problem columns (raw values)",
                    },
                },
                "required": ["problem"],
            },
            source=_SOURCE,
            priority=1,
        ),
    ]
