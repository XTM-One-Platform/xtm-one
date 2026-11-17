"""ServiceNow generic task queue (the base ``task`` table).

The ``task`` table is the parent of every task type (incident, change, problem,
sc_task, change_task, ...), so it is the single place to answer "what is on my
plate?" across all of ITSM. list / get any task and append a work note.
"""

from __future__ import annotations

from typing import Any

from app.tools.base import CallableToolExecutor, ToolDefinition

from ._common import (
    _SOURCE,
    build_encoded_query,
    disp,
    get_record,
    list_table,
    truncate,
    update_record,
)

_TABLE = "task"
_LIST_FIELDS = (
    "number,short_description,state,priority,assigned_to,assignment_group,"
    "sys_class_name,sys_updated_on,sys_id"
)


def _format_line(rec: dict[str, Any]) -> str:
    return (
        f"  {disp(rec.get('number'))} [{disp(rec.get('sys_class_name')) or 'task'}]: "
        f"{truncate(disp(rec.get('short_description')), 80)}\n"
        f"    State: {disp(rec.get('state')) or '?'} | "
        f"Priority: {disp(rec.get('priority')) or '?'} | "
        f"Assigned: {disp(rec.get('assigned_to')) or 'Unassigned'} "
        f"({disp(rec.get('assignment_group')) or 'no group'}) | "
        f"sys_id: {disp(rec.get('sys_id'))}"
    )


def _detail(rec: dict[str, Any]) -> str:
    cls = disp(rec.get("sys_class_name")) or "task"
    return (
        f"{disp(rec.get('number'))} [{cls}]: {disp(rec.get('short_description'))}\n"
        f"  State: {disp(rec.get('state')) or '?'} | "
        f"Priority: {disp(rec.get('priority')) or '?'}\n"
        f"  Assigned to: {disp(rec.get('assigned_to')) or 'Unassigned'} | "
        f"Group: {disp(rec.get('assignment_group')) or 'none'}\n"
        f"  Updated: {disp(rec.get('sys_updated_on'))} | sys_id: {disp(rec.get('sys_id'))}\n"
        f"  Note: this is the base task view. For type-specific fields use the "
        f"matching tool (get_servicenow_incident/change/problem) with this number."
    )


def register(
    base: str, headers: dict[str, str], executor: CallableToolExecutor
) -> list[ToolDefinition]:
    async def list_servicenow_tasks(
        assigned_to: str = "",
        assignment_group: str = "",
        state: str = "",
        active: bool | None = True,
        query: str = "",
        text: str = "",
        limit: int = 20,
        offset: int = 0,
    ) -> str:
        """List tasks across ALL task types (incident, change, problem, sc_task...).

        The single "my work queue" view. ``assigned_to`` / ``assignment_group``
        take a sys_id (resolve via search_servicenow_users / _groups). ``active``
        defaults to true (open work only). ``sys_class_name`` in each row shows
        the concrete type. Use the type-specific get tool for full detail.
        """
        encoded = build_encoded_query(
            {
                "assigned_to": assigned_to,
                "assignment_group": assignment_group,
                "state": state,
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
            "list tasks",
            _format_line,
            noun="task",
            query=encoded,
            limit=limit,
            offset=offset,
            fields=_LIST_FIELDS,
        )

    async def get_servicenow_task(task: str) -> str:
        """Get the base-task view of any task by number or sys_id (any task type)."""
        return await get_record(
            base, headers, _TABLE, task, f"get task {task}", _detail
        )

    async def add_servicenow_task_work_note(
        task: str, note: str, public: bool = False
    ) -> str:
        """Add a work note (internal) or comment (public=true) to any task."""
        if not note or not note.strip():
            return "Error: a non-empty note is required."
        field = "comments" if public else "work_notes"
        return await update_record(
            base,
            headers,
            _TABLE,
            task,
            f"add note to task {task}",
            {field: note.strip()},
            lambda rec: (
                f"Added {'comment' if public else 'work note'} to "
                f"{disp(rec.get('number')) or task}."
            ),
        )

    executor.register("list_servicenow_tasks", list_servicenow_tasks)
    executor.register("get_servicenow_task", get_servicenow_task)
    executor.register("add_servicenow_task_work_note", add_servicenow_task_work_note)

    return [
        ToolDefinition(
            name="list_servicenow_tasks",
            description=(
                "List ServiceNow tasks across ALL task types (incident, change, "
                "problem, sc_task, change_task...) - the unified 'my work queue'. "
                "Filter by assigned_to / assignment_group (sys_ids), state, active "
                "(default true = open work), text, or a raw encoded query. Each row "
                "shows sys_class_name (the concrete type)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "assigned_to": {"type": "string", "description": "Assignee sys_id"},
                    "assignment_group": {"type": "string", "description": "Group sys_id"},
                    "state": {"type": "string", "description": "State number"},
                    "active": {
                        "type": "boolean",
                        "default": True,
                        "description": "true = only open tasks",
                    },
                    "query": {"type": "string", "description": "Raw encoded query"},
                    "text": {"type": "string", "description": "Match short description"},
                    "limit": {"type": "integer", "default": 20},
                    "offset": {"type": "integer", "default": 0},
                },
                "required": [],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="get_servicenow_task",
            description=(
                "Get the base-task view of any task by number or sys_id (works for "
                "every task type). For type-specific fields use the matching "
                "get_servicenow_incident/change/problem tool."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "task": {"type": "string", "description": "Task number or sys_id"}
                },
                "required": ["task"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="add_servicenow_task_work_note",
            description=(
                "Add a work note (internal, default) or customer-visible comment "
                "(public=true) to any task by number or sys_id."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "task": {"type": "string", "description": "Number or sys_id"},
                    "note": {"type": "string"},
                    "public": {"type": "boolean", "default": False},
                },
                "required": ["task", "note"],
            },
            source=_SOURCE,
            priority=1,
        ),
    ]
