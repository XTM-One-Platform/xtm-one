"""Microsoft To Do tools: task lists and tasks."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from app.integrations.http import format_entity_list
from app.services.agent_posts import record_todo_write
from app.tools.base import CallableToolExecutor, ToolDefinition

from ._common import (
    _BASE_URL,
    _PROVIDER,
    MicrosoftContext,
)

#: The statuses a To Do task can be given (``completed`` through complete_microsoft_task).
TASK_STATUSES = ("notStarted", "inProgress", "waitingOnOthers", "deferred", "completed")

_STATUS_MARKS = {"completed": "✓", "inProgress": "▶", "waitingOnOthers": "⏸", "deferred": "↷"}


def task_line(t: dict) -> str:
    """One task as the listings show it: status, title, due date, times and id."""
    status = t.get("status") or "notStarted"
    mark = _STATUS_MARKS.get(status, "☐")
    due = (t.get("dueDateTime") or {}).get("dateTime", "")[:10] if t.get("dueDateTime") else ""
    importance = "!" if t.get("importance") == "high" else ""
    return (
        f"{mark}{importance} {t.get('title', '')} | Status: {status} | Due: {due or 'none'} "
        f"| Created: {str(t.get('createdDateTime', ''))[:19] or 'unknown'} "
        f"| Modified: {str(t.get('lastModifiedDateTime', ''))[:19] or 'unknown'} | ID: {t.get('id', '')}"
    )


def _json_or_empty(resp: object) -> dict:
    """The JSON object of a Graph answer, ``{}`` when it has none (a 204, a text body)."""
    try:
        data = resp.json()  # type: ignore[attr-defined]
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def appended_body(current: str, note: str, *, now: datetime | None = None) -> str:
    """*current* with *note* added as a dated last line (the body is never rewritten)."""
    stamp = (now or datetime.now(UTC)).strftime("%Y-%m-%d %H:%M UTC")
    line = f"[{stamp}] {note.strip()}"
    base = (current or "").rstrip()
    return f"{base}\n{line}" if base else line


def register(ctx: MicrosoftContext, executor: CallableToolExecutor) -> list[ToolDefinition]:
    """Register the tasks tools on *executor* and return their definitions."""
    _client = ctx.client
    _throttled_request = ctx.request

    # ── Microsoft To Do / Tasks ────────────────────────────────────────

    async def list_microsoft_task_lists() -> str:
        """List all Microsoft To Do task lists."""
        async with _client() as client:
            resp, err = await _throttled_request(
                client,
                "get",
                f"{_BASE_URL}/me/todo/lists",
                _PROVIDER,
                "list task lists",
            )
            if err:
                return err
            lists = resp.json().get("value", [])

            def _fmt_list(tl: dict) -> str:
                return f"{tl.get('displayName', 'Untitled')} | ID: {tl.get('id', '')} | Default: {'yes' if tl.get('isDefaultList') else 'no'}"

            return format_entity_list(lists, _fmt_list, noun="task list")

    async def list_microsoft_tasks(
        list_id: str = "", count: int = 20, include_completed: bool = False, status: str = ""
    ) -> str:
        """List tasks from a Microsoft To Do list. If no list_id, uses the default list.

        By default only incomplete tasks are returned.  Pass
        ``include_completed=true`` to include completed tasks as well, or
        ``status`` to list the tasks of one status only.
        For a full survey across all lists in one call use
        ``list_all_microsoft_tasks`` instead.
        """
        if status and status not in TASK_STATUSES:
            return f"Error: status must be one of {', '.join(TASK_STATUSES)}."

        async with _client() as client:
            if not list_id:
                # Find default list
                lists_resp, err = await _throttled_request(
                    client,
                    "get",
                    f"{_BASE_URL}/me/todo/lists",
                    _PROVIDER,
                    "list task lists",
                )
                if err:
                    return err
                lists = lists_resp.json().get("value", [])
                default = next(
                    (tl for tl in lists if tl.get("isDefaultList")),
                    lists[0] if lists else None,
                )
                if not default:
                    return "No task lists found."
                list_id = default["id"]

            params: dict = {"$top": str(count), "$orderby": "createdDateTime desc"}
            if status:
                params["$filter"] = f"status eq '{status}'"
            elif not include_completed:
                params["$filter"] = "status ne 'completed'"
            resp, err = await _throttled_request(
                client,
                "get",
                f"{_BASE_URL}/me/todo/lists/{list_id}/tasks",
                _PROVIDER,
                "list tasks",
                params=params,
            )
            if err:
                return err
            tasks = resp.json().get("value", [])
            return format_entity_list(tasks, task_line, noun="task")

    async def create_microsoft_task(
        title: str,
        list_id: str = "",
        due_date: str = "",
        body: str = "",
        importance: str = "normal",
    ) -> str:
        """Create a task in Microsoft To Do."""
        async with _client() as client:
            if not list_id:
                lists_resp, err = await _throttled_request(
                    client,
                    "get",
                    f"{_BASE_URL}/me/todo/lists",
                    _PROVIDER,
                    "create task",
                )
                if err:
                    return err
                lists = lists_resp.json().get("value", [])
                default = next(
                    (tl for tl in lists if tl.get("isDefaultList")),
                    lists[0] if lists else None,
                )
                if not default:
                    return "No task lists found."
                list_id = default["id"]

            payload: dict = {"title": title, "importance": importance}
            if due_date:
                payload["dueDateTime"] = {
                    "dateTime": due_date + "T00:00:00",
                    "timeZone": "UTC",
                }
            if body:
                payload["body"] = {"content": body, "contentType": "text"}

            resp, err = await _throttled_request(
                client,
                "post",
                f"{_BASE_URL}/me/todo/lists/{list_id}/tasks",
                _PROVIDER,
                "create task",
                json=payload,
            )
            if err:
                return err
            task = resp.json()
            await record_todo_write(task)
            return f"Task created: '{task.get('title', '')}' (ID: {task.get('id', '')})"

    async def complete_microsoft_task(list_id: str, task_id: str) -> str:
        """Mark a task as completed in Microsoft To Do."""
        async with _client() as client:
            resp, err = await _throttled_request(
                client,
                "patch",
                f"{_BASE_URL}/me/todo/lists/{list_id}/tasks/{task_id}",
                _PROVIDER,
                "complete task",
                json={"status": "completed"},
            )
            if err:
                return err
            await record_todo_write(_json_or_empty(resp))
            return f"Task {task_id} marked as completed."

    async def list_all_microsoft_tasks(count_per_list: int = 20) -> str:
        """List incomplete tasks from all Microsoft To Do lists in a single call.

        Fetches the task lists (up to 100 — Graph pagination is not
        followed) and retrieves their incomplete tasks concurrently,
        returning a consolidated view grouped by list.  Use this instead
        of calling ``list_microsoft_task_lists`` followed by a separate
        ``list_microsoft_tasks`` call for each list — it reduces the
        number of tool calls from O(N lists) to O(1) and prevents
        iteration-budget exhaustion in To-Do manager assignments.

        ``count_per_list`` caps results per list (default 20, max 100).
        """
        # Clamp to a safe maximum to avoid oversized responses.
        count_per_list = max(1, min(count_per_list, 100))

        async with _client() as client:
            # Step 1: fetch the task lists in a single request.  $top is
            # pinned high so accounts with many lists are not silently
            # truncated to Graph's default page size — a missed list would
            # recreate the exact "incomplete survey" bug this tool fixes.
            # (Accounts with >100 lists would need pagination; 100 is far
            # beyond typical To Do usage.)
            lists_resp, err = await _throttled_request(
                client,
                "get",
                f"{_BASE_URL}/me/todo/lists",
                _PROVIDER,
                "list task lists",
                params={"$top": "100"},
            )
            if err:
                return err
            lists = lists_resp.json().get("value", [])
            if not lists:
                return "No task lists found."

            # Step 2: fetch incomplete tasks from every list concurrently.
            # The shared _api_sem semaphore caps concurrent in-flight
            # requests to _MICROSOFT_CONCURRENCY so we never trigger a
            # thundering-herd rate-limit storm.
            async def _fetch_list_tasks(
                list_id: str,
            ) -> tuple[str, list[dict], str | None]:
                resp, fetch_err = await _throttled_request(
                    client,
                    "get",
                    f"{_BASE_URL}/me/todo/lists/{list_id}/tasks",
                    _PROVIDER,
                    "list all tasks",
                    params={
                        "$top": str(count_per_list),
                        "$orderby": "createdDateTime desc",
                        "$filter": "status ne 'completed'",
                    },
                )
                if fetch_err:
                    return list_id, [], fetch_err
                return list_id, resp.json().get("value", []), None

            task_results = await asyncio.gather(*[_fetch_list_tasks(tl["id"]) for tl in lists])

        # Step 3: format results grouped by list.
        def _fmt_task(t: dict) -> str:
            return f"  {task_line(t)}"

        list_by_id = {tl["id"]: tl for tl in lists}
        sections: list[str] = []
        failed_lists: list[str] = []
        total_tasks = 0
        for list_id, tasks, fetch_err in task_results:
            name = list_by_id.get(list_id, {}).get("displayName", "Untitled")
            if fetch_err:
                failed_lists.append(name)
                continue
            if not tasks:
                continue
            total_tasks += len(tasks)
            sections.append(f"[{name}]\n" + "\n".join(_fmt_task(t) for t in tasks))

        # A failed per-list fetch must never masquerade as "no tasks" —
        # the agent would wrongly conclude there is nothing to do.
        failure_note = (
            f"Warning: could not fetch tasks from {len(failed_lists)} list(s): " + ", ".join(failed_lists)
            if failed_lists
            else ""
        )

        if not sections:
            if failed_lists:
                return f"No incomplete tasks found in the lists that loaded successfully. {failure_note}"
            return "No incomplete tasks found across all lists."

        header = f"Found {total_tasks} incomplete task(s) across {len(sections)} list(s) with tasks:"
        result = header + "\n\n" + "\n\n".join(sections)
        if failure_note:
            result += "\n\n" + failure_note
        return result

    # ── Task Management Tools ──────────────────────────────────────────

    async def update_microsoft_task(
        list_id: str,
        task_id: str,
        title: str = "",
        due_date: str = "",
        body: str = "",
        importance: str = "",
        status: str = "",
        append_note: str = "",
    ) -> str:
        """Update a task in Microsoft To Do.

        Only the provided fields are updated; omitted fields remain
        unchanged. ``append_note`` adds a dated line at the end of the body
        (read first, never rewritten); it cannot be combined with ``body``.
        """
        if status and status not in TASK_STATUSES:
            return f"Error: status must be one of {', '.join(TASK_STATUSES)}."
        if body and append_note.strip():
            return "Error: pass body (replace the notes) or append_note (add a line), not both."
        payload: dict = {}
        if status:
            payload["status"] = status
        if title:
            payload["title"] = title
        if due_date:
            payload["dueDateTime"] = {
                "dateTime": due_date + "T00:00:00",
                "timeZone": "UTC",
            }
        if body:
            payload["body"] = {"content": body, "contentType": "text"}
        if importance:
            payload["importance"] = importance
        if not payload and not append_note.strip():
            return "Error: no fields to update. Provide at least one field."

        async with _client() as client:
            if append_note.strip():
                current, err = await _throttled_request(
                    client,
                    "get",
                    f"{_BASE_URL}/me/todo/lists/{list_id}/tasks/{task_id}",
                    _PROVIDER,
                    "read task",
                )
                if err:
                    return err
                existing = (_json_or_empty(current).get("body") or {}).get("content") or ""
                payload["body"] = {"content": appended_body(existing, append_note), "contentType": "text"}
            resp, err = await _throttled_request(
                client,
                "patch",
                f"{_BASE_URL}/me/todo/lists/{list_id}/tasks/{task_id}",
                _PROVIDER,
                "update task",
                json=payload,
            )
            if err:
                return err
            task = resp.json()
            await record_todo_write(task)
            status_note = f" Status: {task.get('status', '')}." if status else ""
            return f"Task updated: '{task.get('title', '')}' (ID: {task.get('id', '')}).{status_note}"

    async def delete_microsoft_task(list_id: str, task_id: str) -> str:
        """Delete a task from Microsoft To Do."""
        async with _client() as client:
            _, err = await _throttled_request(
                client,
                "delete",
                f"{_BASE_URL}/me/todo/lists/{list_id}/tasks/{task_id}",
                _PROVIDER,
                "delete task",
            )
            if err:
                return err
            await record_todo_write({"id": task_id, "lastModifiedDateTime": "deleted"})
            return f"Task {task_id} deleted from list {list_id}."

    executor.register("list_microsoft_task_lists", list_microsoft_task_lists)
    executor.register("list_microsoft_tasks", list_microsoft_tasks)
    executor.register("list_all_microsoft_tasks", list_all_microsoft_tasks)
    executor.register("create_microsoft_task", create_microsoft_task)
    executor.register("complete_microsoft_task", complete_microsoft_task)
    executor.register("update_microsoft_task", update_microsoft_task)
    executor.register("delete_microsoft_task", delete_microsoft_task)

    return [
        # ── Tasks / To Do ──
        ToolDefinition(
            name="list_microsoft_task_lists",
            description="List all Microsoft To Do task lists.",
            input_schema={"type": "object", "properties": {}, "required": []},
            source="integration:microsoft",
            priority=2,
        ),
        ToolDefinition(
            name="list_microsoft_tasks",
            description=(
                "List tasks from a Microsoft To Do list. Uses default list if no "
                "list_id provided. By default returns only incomplete tasks; pass "
                "include_completed=true to include completed tasks, or status to list one "
                "status only. Each task shows its status, created and last modified times. "
                "For a survey across all lists in one call use list_all_microsoft_tasks."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "list_id": {
                        "type": "string",
                        "description": "Task list ID (optional, uses default)",
                        "default": "",
                    },
                    "count": {"type": "integer", "default": 20},
                    "include_completed": {
                        "type": "boolean",
                        "description": "Include completed tasks (default false)",
                        "default": False,
                    },
                    "status": {
                        "type": "string",
                        "enum": ["", *TASK_STATUSES],
                        "description": "Only the tasks of this status (overrides include_completed)",
                        "default": "",
                    },
                },
                "required": [],
            },
            source="integration:microsoft",
            priority=1,
        ),
        ToolDefinition(
            name="list_all_microsoft_tasks",
            description=(
                "List incomplete tasks from all Microsoft To Do lists (up to 100 "
                "lists) in a single call. Fetches the task lists and their "
                "incomplete tasks concurrently. Use this for To-Do manager "
                "assignments instead of calling list_microsoft_task_lists + "
                "list_microsoft_tasks for each list separately — reduces tool "
                "call iterations from O(N lists) to O(1)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "count_per_list": {
                        "type": "integer",
                        "description": "Max incomplete tasks to return per list (default 20, max 100)",
                        "default": 20,
                    },
                },
                "required": [],
            },
            source="integration:microsoft",
            # Standard priority (not extended): this compound tool is the
            # O(1) replacement that prevents iteration-budget exhaustion in
            # To-Do manager assignments — it must survive max_tools
            # truncation alongside list_microsoft_tasks.
            priority=1,
        ),
        ToolDefinition(
            name="create_microsoft_task",
            description="Create a task in Microsoft To Do.",
            input_schema={
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Task title"},
                    "list_id": {
                        "type": "string",
                        "description": "Task list ID (optional)",
                        "default": "",
                    },
                    "due_date": {
                        "type": "string",
                        "description": "Due date (YYYY-MM-DD)",
                        "default": "",
                    },
                    "body": {
                        "type": "string",
                        "description": "Task body/notes",
                        "default": "",
                    },
                    "importance": {
                        "type": "string",
                        "enum": ["low", "normal", "high"],
                        "default": "normal",
                    },
                },
                "required": ["title"],
            },
            source="integration:microsoft",
            priority=1,
        ),
        ToolDefinition(
            name="complete_microsoft_task",
            description="Mark a Microsoft To Do task as completed.",
            input_schema={
                "type": "object",
                "properties": {
                    "list_id": {"type": "string", "description": "Task list ID"},
                    "task_id": {"type": "string", "description": "Task ID"},
                },
                "required": ["list_id", "task_id"],
            },
            source="integration:microsoft",
            priority=1,
        ),
        ToolDefinition(
            name="update_microsoft_task",
            description=(
                "Update a task in Microsoft To Do. Only provided fields are updated. status sets "
                "notStarted, inProgress, waitingOnOthers or deferred (complete_microsoft_task "
                "completes it); append_note adds a dated line to the notes without rewriting them."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "list_id": {"type": "string", "description": "Task list ID"},
                    "task_id": {"type": "string", "description": "Task ID"},
                    "title": {
                        "type": "string",
                        "description": "New task title",
                        "default": "",
                    },
                    "due_date": {
                        "type": "string",
                        "description": "New due date (YYYY-MM-DD)",
                        "default": "",
                    },
                    "body": {
                        "type": "string",
                        "description": "New task body/notes",
                        "default": "",
                    },
                    "importance": {
                        "type": "string",
                        "enum": ["low", "normal", "high"],
                        "description": "New importance level",
                        "default": "",
                    },
                    "status": {
                        "type": "string",
                        "enum": ["", *TASK_STATUSES],
                        "description": "New status",
                        "default": "",
                    },
                    "append_note": {
                        "type": "string",
                        "description": "A line added, dated, at the end of the task notes (not with body)",
                        "default": "",
                    },
                },
                "required": ["list_id", "task_id"],
            },
            source="integration:microsoft",
            priority=1,
        ),
        ToolDefinition(
            name="delete_microsoft_task",
            description="Delete a task from Microsoft To Do.",
            input_schema={
                "type": "object",
                "properties": {
                    "list_id": {"type": "string", "description": "Task list ID"},
                    "task_id": {"type": "string", "description": "Task ID"},
                },
                "required": ["list_id", "task_id"],
            },
            source="integration:microsoft",
            priority=1,
        ),
    ]
