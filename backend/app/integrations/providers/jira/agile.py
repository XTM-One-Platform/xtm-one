"""Jira Software (Agile) - boards, sprints, backlog, epics.

Uses the Agile REST API (``/rest/agile/1.0``). Sprint and epic write operations
are consolidated into ``manage_jira_sprint`` / ``manage_jira_epic`` to keep the
tool list legible.
"""

from __future__ import annotations

from typing import Any

from app.tools.base import CallableToolExecutor, ToolDefinition

from ._common import _SOURCE, agile, clamp, format_issue, jira_request

# Agile move operations are capped at 50 issues per request by Jira.
_MAX_MOVE = 50


def _issue_list(issues: Any) -> list[str]:
    if isinstance(issues, list):
        return [str(i) for i in issues if i]
    if isinstance(issues, str) and issues.strip():
        return [part.strip() for part in issues.split(",") if part.strip()]
    return []


def register(
    base: str, headers: dict[str, str], executor: CallableToolExecutor
) -> list[ToolDefinition]:
    api = agile(base)

    async def list_jira_boards(
        project_key: str = "", board_type: str = "", count: int = 30
    ) -> str:
        """List agile boards. Optionally filter by project key or type (scrum/kanban)."""
        params: dict[str, Any] = {"maxResults": clamp(count, 30, 50)}
        if project_key:
            params["projectKeyOrId"] = project_key
        if board_type:
            params["type"] = board_type
        resp, err = await jira_request(
            "get", f"{api}/board", "list boards", headers=headers, params=params
        )
        if err:
            return err
        boards = resp.json().get("values", [])
        if not boards:
            return "No boards found."
        lines = [f"Boards ({len(boards)}):"]
        for b in boards:
            project = (b.get("location") or {}).get("projectKey", "?")
            lines.append(
                f"  #{b['id']} {b.get('name', '?')} [{b.get('type', '?')}] (project: {project})"
            )
        return "\n".join(lines)

    async def get_jira_board_configuration(board_id: int) -> str:
        """Get a board's configuration: filter, columns, estimation, and ranking field."""
        resp, err = await jira_request(
            "get",
            f"{api}/board/{board_id}/configuration",
            f"get board {board_id} configuration",
            headers=headers,
        )
        if err:
            return err
        cfg = resp.json()
        lines = [
            f"Board #{cfg.get('id', board_id)}: {cfg.get('name', '?')} [{cfg.get('type', '?')}]"
        ]
        flt = cfg.get("filter") or {}
        if flt:
            lines.append(f"  Filter id: {flt.get('id', '?')}")
        columns = (cfg.get("columnConfig") or {}).get("columns") or []
        if columns:
            lines.append(f"  Columns: {', '.join(c.get('name', '?') for c in columns)}")
        estimation = (cfg.get("estimation") or {}).get("field") or {}
        if estimation:
            lines.append(
                f"  Estimation field: {estimation.get('displayName', '?')} "
                f"(id: {estimation.get('fieldId', '?')})"
            )
        ranking = cfg.get("ranking") or {}
        if ranking:
            lines.append(f"  Ranking custom field id: {ranking.get('rankCustomFieldId', '?')}")
        return "\n".join(lines)

    async def list_jira_board_backlog(board_id: int, count: int = 30) -> str:
        """List the issues in a board's backlog (not assigned to an active/future sprint)."""
        resp, err = await jira_request(
            "get",
            f"{api}/board/{board_id}/backlog",
            f"get backlog for board {board_id}",
            headers=headers,
            params={
                "maxResults": clamp(count, 30, 50),
                "fields": "summary,status,issuetype,priority,assignee,labels",
            },
        )
        if err:
            return err
        issues = resp.json().get("issues", [])
        if not issues:
            return f"Board {board_id} backlog is empty."
        lines = [f"Backlog for board {board_id} ({len(issues)} shown):"]
        for issue in issues:
            lines.append(format_issue(issue))
        return "\n\n".join(lines)

    async def list_jira_sprints(
        board_id: int, state: str = "active", count: int = 20
    ) -> str:
        """List sprints for a board. State: active, future, closed (comma-separable)."""
        params: dict[str, Any] = {"maxResults": clamp(count, 20, 50)}
        if state:
            params["state"] = state
        resp, err = await jira_request(
            "get",
            f"{api}/board/{board_id}/sprint",
            f"list sprints for board {board_id}",
            headers=headers,
            params=params,
        )
        if err:
            return err
        sprints = resp.json().get("values", [])
        if not sprints:
            return f"No {state} sprints found for board {board_id}."
        lines = [f"Sprints for board {board_id}:"]
        for s in sprints:
            start = (s.get("startDate") or "?")[:10]
            end = (s.get("endDate") or "?")[:10]
            lines.append(
                f"  #{s['id']} {s.get('name', '?')} [{s.get('state', '?')}] ({start} -> {end})"
            )
            if s.get("goal"):
                lines.append(f"    Goal: {s['goal'][:200]}")
        return "\n".join(lines)

    async def get_jira_sprint_issues(sprint_id: int, count: int = 30) -> str:
        """Get the issues in a sprint with status, assignee, priority and type."""
        resp, err = await jira_request(
            "get",
            f"{api}/sprint/{sprint_id}/issue",
            f"get sprint {sprint_id} issues",
            headers=headers,
            params={
                "maxResults": clamp(count, 30, 50),
                "fields": "summary,status,issuetype,priority,assignee,labels",
            },
        )
        if err:
            return err
        issues = resp.json().get("issues", [])
        if not issues:
            return f"No issues in sprint {sprint_id}."
        lines = [f"Sprint {sprint_id} ({len(issues)} issues shown):"]
        for issue in issues:
            lines.append(format_issue(issue))
        return "\n\n".join(lines)

    async def list_jira_epics(board_id: int, count: int = 30) -> str:
        """List the epics on a board with their key, name and done status."""
        resp, err = await jira_request(
            "get",
            f"{api}/board/{board_id}/epic",
            f"list epics for board {board_id}",
            headers=headers,
            params={"maxResults": clamp(count, 30, 50)},
        )
        if err:
            return err
        epics = resp.json().get("values", [])
        if not epics:
            return f"No epics found on board {board_id}."
        lines = [f"Epics on board {board_id} ({len(epics)}):"]
        for e in epics:
            done = "done" if e.get("done") else "open"
            lines.append(
                f"  {e.get('key', '?')} - {e.get('name', '?')} [{done}] (id: {e.get('id', '?')})"
            )
        return "\n".join(lines)

    async def manage_jira_sprint(
        action: str,
        sprint_id: int = 0,
        board_id: int = 0,
        name: str = "",
        goal: str = "",
        start_date: str = "",
        end_date: str = "",
        issues: Any = None,
    ) -> str:
        """Create / update / start / complete / delete a sprint, or move issues.

        Actions:
        - 'create' (board_id + name [+ goal/start_date/end_date])
        - 'update' (sprint_id + any of name/goal/start_date/end_date)
        - 'start' (sprint_id + start_date + end_date)
        - 'complete' (sprint_id)
        - 'delete' (sprint_id)
        - 'move_issues' (sprint_id + issues: list of keys, max 50)
        - 'move_to_backlog' (issues: list of keys, max 50)
        """
        action = (action or "").strip().lower()
        if action == "create":
            if not (board_id and name):
                return "board_id and name are required for 'create'."
            payload: dict[str, Any] = {"name": name, "originBoardId": board_id}
            if goal:
                payload["goal"] = goal
            if start_date:
                payload["startDate"] = start_date
            if end_date:
                payload["endDate"] = end_date
            resp, err = await jira_request(
                "post", f"{api}/sprint", "create sprint", headers=headers, json=payload
            )
            if err:
                return err
            s = resp.json()
            return f"Created sprint '{s.get('name')}' (id: {s.get('id')})."
        if action in ("update", "start", "complete"):
            if not sprint_id:
                return f"sprint_id is required for '{action}'."
            payload = {}
            if action == "start":
                payload["state"] = "active"
                if start_date:
                    payload["startDate"] = start_date
                if end_date:
                    payload["endDate"] = end_date
            elif action == "complete":
                payload["state"] = "closed"
            if name:
                payload["name"] = name
            if goal:
                payload["goal"] = goal
            if start_date and action != "start":
                payload["startDate"] = start_date
            if end_date and action != "start":
                payload["endDate"] = end_date
            if not payload:
                return "Provide at least one field to update."
            _, err = await jira_request(
                "post",
                f"{api}/sprint/{sprint_id}",
                f"{action} sprint {sprint_id}",
                headers=headers,
                json=payload,
            )
            if err:
                return err
            past = {"start": "started", "complete": "completed", "update": "updated"}
            return f"Sprint {sprint_id} {past[action]}."
        if action == "delete":
            if not sprint_id:
                return "sprint_id is required for 'delete'."
            _, err = await jira_request(
                "delete",
                f"{api}/sprint/{sprint_id}",
                f"delete sprint {sprint_id}",
                headers=headers,
            )
            if err:
                return err
            return f"Sprint {sprint_id} deleted (open issues moved to backlog)."
        if action == "move_issues":
            keys = _issue_list(issues)
            if not (sprint_id and keys):
                return "sprint_id and issues (list of keys) are required for 'move_issues'."
            if len(keys) > _MAX_MOVE:
                return f"At most {_MAX_MOVE} issues can be moved per call."
            _, err = await jira_request(
                "post",
                f"{api}/sprint/{sprint_id}/issue",
                f"move issues to sprint {sprint_id}",
                headers=headers,
                json={"issues": keys},
            )
            if err:
                return err
            return f"Moved {len(keys)} issue(s) to sprint {sprint_id}."
        if action == "move_to_backlog":
            keys = _issue_list(issues)
            if not keys:
                return "issues (list of keys) is required for 'move_to_backlog'."
            if len(keys) > _MAX_MOVE:
                return f"At most {_MAX_MOVE} issues can be moved per call."
            _, err = await jira_request(
                "post",
                f"{api}/backlog/issue",
                "move issues to backlog",
                headers=headers,
                json={"issues": keys},
            )
            if err:
                return err
            return f"Moved {len(keys)} issue(s) to the backlog."
        return (
            f"Unknown action '{action}'. Valid: create, update, start, complete, "
            "delete, move_issues, move_to_backlog."
        )

    async def manage_jira_epic(
        action: str, epic_key: str = "", issues: Any = None, count: int = 30
    ) -> str:
        """Inspect an epic or move issues into / out of it.

        Actions:
        - 'get' (epic_key)
        - 'list_issues' (epic_key)
        - 'move_issues' (epic_key + issues: list of keys, max 50)
        - 'remove_from_epic' (issues: list of keys, max 50)
        """
        action = (action or "").strip().lower()
        if action == "get":
            if not epic_key:
                return "epic_key is required for 'get'."
            resp, err = await jira_request(
                "get", f"{api}/epic/{epic_key}", f"get epic {epic_key}", headers=headers
            )
            if err:
                return err
            e = resp.json()
            done = "done" if e.get("done") else "open"
            return (
                f"Epic {e.get('key', epic_key)}: {e.get('name', '?')} [{done}]\n"
                f"  Summary: {e.get('summary', '?')}"
            )
        if action == "list_issues":
            if not epic_key:
                return "epic_key is required for 'list_issues'."
            resp, err = await jira_request(
                "get",
                f"{api}/epic/{epic_key}/issue",
                f"list issues in epic {epic_key}",
                headers=headers,
                params={
                    "maxResults": clamp(count, 30, 50),
                    "fields": "summary,status,issuetype,priority,assignee",
                },
            )
            if err:
                return err
            issue_rows = resp.json().get("issues", [])
            if not issue_rows:
                return f"No issues in epic {epic_key}."
            lines = [f"Issues in epic {epic_key} ({len(issue_rows)} shown):"]
            for issue in issue_rows:
                lines.append(format_issue(issue))
            return "\n\n".join(lines)
        if action == "move_issues":
            keys = _issue_list(issues)
            if not (epic_key and keys):
                return "epic_key and issues (list of keys) are required for 'move_issues'."
            if len(keys) > _MAX_MOVE:
                return f"At most {_MAX_MOVE} issues can be moved per call."
            _, err = await jira_request(
                "post",
                f"{api}/epic/{epic_key}/issue",
                f"move issues to epic {epic_key}",
                headers=headers,
                json={"issues": keys},
            )
            if err:
                return err
            return f"Moved {len(keys)} issue(s) to epic {epic_key}."
        if action == "remove_from_epic":
            keys = _issue_list(issues)
            if not keys:
                return "issues (list of keys) is required for 'remove_from_epic'."
            _, err = await jira_request(
                "post",
                f"{api}/epic/none/issue",
                "remove issues from epic",
                headers=headers,
                json={"issues": keys},
            )
            if err:
                return err
            return f"Removed {len(keys)} issue(s) from their epic."
        return (
            f"Unknown action '{action}'. Valid: get, list_issues, move_issues, "
            "remove_from_epic."
        )

    executor.register("list_jira_boards", list_jira_boards)
    executor.register("get_jira_board_configuration", get_jira_board_configuration)
    executor.register("list_jira_board_backlog", list_jira_board_backlog)
    executor.register("list_jira_sprints", list_jira_sprints)
    executor.register("get_jira_sprint_issues", get_jira_sprint_issues)
    executor.register("list_jira_epics", list_jira_epics)
    executor.register("manage_jira_sprint", manage_jira_sprint)
    executor.register("manage_jira_epic", manage_jira_epic)

    return [
        ToolDefinition(
            name="list_jira_boards",
            description="List agile boards (Scrum, Kanban). Optionally filter by project key or board type.",
            input_schema={
                "type": "object",
                "properties": {
                    "project_key": {"type": "string", "description": "Filter by project key", "default": ""},
                    "board_type": {"type": "string", "enum": ["scrum", "kanban", ""], "description": "Filter by board type", "default": ""},
                    "count": {"type": "integer", "description": "Max boards (max 50)", "default": 30},
                },
                "required": [],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="get_jira_board_configuration",
            description="Get a board's configuration: filter id, columns, estimation field and the ranking custom field id. Use list_jira_boards for board ids.",
            input_schema={
                "type": "object",
                "properties": {
                    "board_id": {"type": "integer", "description": "Board id"},
                },
                "required": ["board_id"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="list_jira_board_backlog",
            description="List the issues in a board's backlog (issues not in an active or future sprint).",
            input_schema={
                "type": "object",
                "properties": {
                    "board_id": {"type": "integer", "description": "Board id"},
                    "count": {"type": "integer", "description": "Max issues (max 50)", "default": 30},
                },
                "required": ["board_id"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="list_jira_sprints",
            description="List sprints for a board (name, state, dates, goal). State: active, future, closed. Use list_jira_boards for board ids.",
            input_schema={
                "type": "object",
                "properties": {
                    "board_id": {"type": "integer", "description": "Board id"},
                    "state": {"type": "string", "description": "active, future, closed (comma-separable)", "default": "active"},
                    "count": {"type": "integer", "description": "Max sprints (max 50)", "default": 20},
                },
                "required": ["board_id"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="get_jira_sprint_issues",
            description="Get the issues in a sprint with status, assignee, priority and type. Use list_jira_sprints for sprint ids.",
            input_schema={
                "type": "object",
                "properties": {
                    "sprint_id": {"type": "integer", "description": "Sprint id"},
                    "count": {"type": "integer", "description": "Max issues (max 50)", "default": 30},
                },
                "required": ["sprint_id"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="list_jira_epics",
            description="List the epics on a board with key, name and done status. Use list_jira_boards for board ids.",
            input_schema={
                "type": "object",
                "properties": {
                    "board_id": {"type": "integer", "description": "Board id"},
                    "count": {"type": "integer", "description": "Max epics (max 50)", "default": 30},
                },
                "required": ["board_id"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="manage_jira_sprint",
            description=(
                "Create, update, start, complete or delete a sprint, or move issues "
                "into a sprint / to the backlog. Actions: 'create' (board_id + name), "
                "'update', 'start' (start_date + end_date), 'complete', 'delete', "
                "'move_issues' (sprint_id + issues), 'move_to_backlog' (issues). Max 50 "
                "issues per move."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["create", "update", "start", "complete", "delete", "move_issues", "move_to_backlog"],
                    },
                    "sprint_id": {"type": "integer", "description": "Sprint id (most actions)", "default": 0},
                    "board_id": {"type": "integer", "description": "Origin board id (create)", "default": 0},
                    "name": {"type": "string", "description": "Sprint name (create/update)", "default": ""},
                    "goal": {"type": "string", "description": "Sprint goal (create/update)", "default": ""},
                    "start_date": {"type": "string", "description": "ISO 8601 start (create/update/start)", "default": ""},
                    "end_date": {"type": "string", "description": "ISO 8601 end (create/update/start)", "default": ""},
                    "issues": {"type": "array", "items": {"type": "string"}, "description": "Issue keys (move_issues/move_to_backlog)"},
                },
                "required": ["action"],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="manage_jira_epic",
            description=(
                "Inspect an epic or move issues into / out of it. Actions: 'get' "
                "(epic_key), 'list_issues' (epic_key), 'move_issues' (epic_key + "
                "issues), 'remove_from_epic' (issues). Max 50 issues per move."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["get", "list_issues", "move_issues", "remove_from_epic"]},
                    "epic_key": {"type": "string", "description": "Epic issue key (e.g. 'PROJ-1')", "default": ""},
                    "issues": {"type": "array", "items": {"type": "string"}, "description": "Issue keys (move_issues/remove_from_epic)"},
                    "count": {"type": "integer", "description": "Max issues for list_issues (max 50)", "default": 30},
                },
                "required": ["action"],
            },
            source=_SOURCE,
            priority=1,
        ),
    ]
