# SPDX-License-Identifier: Apache-2.0

"""n8n workflow automation integration – Workflows & Executions.

Provides 11 tools across 2 categories:
  1. Workflows: list, get, create, update, delete, set active, get tags
  2. Executions: list, get, delete, retry

Uses n8n public REST API v1 via:
  {base_url}/api/v1/...

Authentication: API key via ``X-N8N-API-KEY`` header.

Token format: "base_url||api_key" (compound, split by ||).
"""

from __future__ import annotations

import json
from typing import Any

from app.core.egress import egress_client
from app.integrations.base import BaseIntegration, OAuthDefaults
from app.integrations.http import (
    format_entity_list,
    format_pagination_hint,
    parse_compound_token,
    safe_request,
)
from app.tools.base import CallableToolExecutor, ToolDefinition

_PROVIDER = "n8n"


class N8nIntegration(BaseIntegration):
    provider_name = "n8n"
    display_name = "n8n"
    icon = "Workflow"
    description = "Connect n8n for workflow automation — create, manage, execute, and monitor n8n workflows programmatically."
    auth_types = ["api_key"]

    def get_oauth_defaults(self) -> OAuthDefaults | None:
        return None

    async def validate_connection(self, token: str) -> bool:
        _base_raw, api_key = parse_compound_token(token, 2)
        base_url = _base_raw.rstrip("/")
        if not base_url:
            return False
        async with egress_client(timeout=15) as client:
            resp, _err = await safe_request(
                client,
                "get",
                f"{base_url}/api/v1/workflows",
                _PROVIDER,
                "validate connection",
                headers=_hdrs(api_key),
                params={"limit": 1},
            )
            return resp is not None

    async def get_user_info(self, token: str) -> dict:
        _base_raw, api_key = parse_compound_token(token, 2)
        base_url = _base_raw.rstrip("/")
        async with egress_client(timeout=15) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{base_url}/api/v1/workflows",
                _PROVIDER,
                "get user info",
                headers=_hdrs(api_key),
                params={"limit": 1},
            )
            if resp is None:
                return {"error": err}
            return {"status": "connected", "base_url": base_url}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _hdrs(api_key: str) -> dict[str, str]:
    return {"X-N8N-API-KEY": api_key, "Accept": "application/json"}


def _format_workflow(wf: dict, *, detail: bool = False) -> str:
    """Format a workflow dict into a readable string."""
    wf_id = wf.get("id", "?")
    name = wf.get("name", "Unnamed")
    active = "Active" if wf.get("active") else "Inactive"
    created = wf.get("createdAt", "?")
    updated = wf.get("updatedAt", "?")

    lines = [f"#{wf_id}: {name} [{active}]"]

    tags = wf.get("tags", [])
    if tags:
        tag_names = [
            t.get("name", "?") if isinstance(t, dict) else str(t) for t in tags
        ]
        lines.append(f"  Tags: {', '.join(tag_names)}")

    if detail:
        lines.append(f"  Created: {created}")
        lines.append(f"  Updated: {updated}")

        nodes = wf.get("nodes", [])
        if nodes:
            node_names = [n.get("name", n.get("type", "?")) for n in nodes[:20]]
            lines.append(f"  Nodes ({len(nodes)}): {', '.join(node_names)}")
            if len(nodes) > 20:
                lines.append(f"    ... and {len(nodes) - 20} more nodes")

        connections = wf.get("connections", {})
        if connections:
            lines.append(f"  Connections: {len(connections)} node connection groups")

        settings = wf.get("settings", {})
        if settings:
            save_exec = settings.get("saveExecutionProgress")
            save_data = settings.get("saveDataSuccessExecution")
            if save_exec is not None:
                lines.append(f"  Save execution progress: {save_exec}")
            if save_data is not None:
                lines.append(f"  Save success data: {save_data}")

    return "\n".join(lines)


def _format_execution(ex: dict, *, detail: bool = False) -> str:
    """Format an execution dict into a readable string."""
    ex_id = ex.get("id", "?")
    status = ex.get("status", "?")
    mode = ex.get("mode", "?")
    started = ex.get("startedAt", "?")
    finished = ex.get("stoppedAt", "?")
    wf_id = ex.get("workflowId", "?")
    wf_name = ""
    wf_data = ex.get("workflowData") or {}
    if isinstance(wf_data, dict):
        wf_name = wf_data.get("name", "")

    wf_label = f" (workflow: {wf_name})" if wf_name else f" (workflowId: {wf_id})"

    lines = [f"Execution #{ex_id} [{status}]{wf_label}"]
    lines.append(f"  Mode: {mode} | Started: {started} | Finished: {finished}")

    if detail:
        data = ex.get("data", {})
        if data and isinstance(data, dict):
            result_data = data.get("resultData", {})
            if result_data:
                run_data = result_data.get("runData", {})
                if run_data:
                    lines.append(
                        f"  Executed nodes: {', '.join(list(run_data.keys())[:15])}"
                    )
                error = result_data.get("error")
                if error:
                    msg = (
                        error.get("message", str(error))
                        if isinstance(error, dict)
                        else str(error)
                    )
                    lines.append(f"  Error: {str(msg)[:500]}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Tool builder
# ---------------------------------------------------------------------------


def build_tools(
    token: str, executor: CallableToolExecutor, **_kwargs: Any
) -> list[ToolDefinition]:
    _base_raw, api_key = parse_compound_token(token, 2)
    base_url = _base_raw.rstrip("/")

    if not base_url:
        return []  # No base_url — can't make API calls

    api = f"{base_url}/api/v1"
    headers = _hdrs(api_key)

    # ══════════════════════════════════════════════════════════════════════
    # 1. WORKFLOWS
    # ══════════════════════════════════════════════════════════════════════

    async def list_n8n_workflows(
        active: str = "",
        tags: str = "",
        name: str = "",
        limit: int = 25,
        cursor: str = "",
    ) -> str:
        """List workflows in the n8n instance.

        Supports filtering by active status, tags, and name.
        Use cursor from previous response for pagination.
        """
        async with egress_client(timeout=20) as client:
            params: dict[str, Any] = {"limit": min(limit, 100)}
            if active:
                params["active"] = active.lower() == "true"
            if tags:
                params["tags"] = tags
            if name:
                params["name"] = name
            if cursor:
                params["cursor"] = cursor

            resp, err = await safe_request(
                client,
                "get",
                f"{api}/workflows",
                _PROVIDER,
                "list workflows",
                headers=headers,
                params=params,
            )
            if err:
                return err
            data = resp.json()
            workflows = data.get("data", [])
            result = format_entity_list(workflows, _format_workflow, noun="workflow")
            return result + format_pagination_hint(
                data.get("nextCursor"),
                param_name="cursor",
            )

    async def get_n8n_workflow(workflow_id: str) -> str:
        """Get full details of a specific workflow including nodes and connections."""
        async with egress_client(timeout=20) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{api}/workflows/{workflow_id}",
                _PROVIDER,
                f"get workflow {workflow_id}",
                headers=headers,
            )
            if err:
                return err
            return _format_workflow(resp.json(), detail=True)

    async def create_n8n_workflow(
        name: str,
        nodes: str = "[]",
        connections: str = "{}",
        settings: str = "{}",
        static_data: str = "null",
    ) -> str:
        """Create a new workflow in n8n.

        nodes: JSON array of node objects. Each node needs at minimum:
          - name: display name
          - type: node type (e.g. 'n8n-nodes-base.httpRequest', 'n8n-nodes-base.set')
          - position: [x, y] coordinates
          - parameters: node-specific configuration

        connections: JSON object mapping source node names to their output connections.
        settings: JSON object with workflow settings (optional).

        Example minimal workflow with a manual trigger and HTTP request:
        nodes: [
          {"name": "Start", "type": "n8n-nodes-base.manualTrigger", "position": [250, 300], "parameters": {}},
          {"name": "HTTP Request", "type": "n8n-nodes-base.httpRequest", "position": [450, 300],
           "parameters": {"url": "https://api.example.com/data", "method": "GET"}}
        ]
        connections: {"Start": {"main": [[{"node": "HTTP Request", "type": "main", "index": 0}]]}}
        """
        payload: dict[str, Any] = {"name": name}
        try:
            payload["nodes"] = json.loads(nodes) if isinstance(nodes, str) else nodes
        except json.JSONDecodeError:
            return "Error: 'nodes' must be valid JSON array."
        try:
            payload["connections"] = (
                json.loads(connections) if isinstance(connections, str) else connections
            )
        except json.JSONDecodeError:
            return "Error: 'connections' must be valid JSON object."
        try:
            parsed_settings = (
                json.loads(settings) if isinstance(settings, str) else settings
            )
            if parsed_settings:
                payload["settings"] = parsed_settings
        except json.JSONDecodeError:
            pass  # Optional field — skip if not valid JSON
        try:
            parsed_static = (
                json.loads(static_data) if isinstance(static_data, str) else static_data
            )
            if parsed_static is not None:
                payload["staticData"] = parsed_static
        except json.JSONDecodeError:
            pass  # Optional field — skip if not valid JSON

        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "post",
                f"{api}/workflows",
                _PROVIDER,
                f"create workflow '{name}'",
                headers={**headers, "Content-Type": "application/json"},
                json=payload,
            )
            if err:
                return err
            return f"Workflow created successfully:\n{_format_workflow(resp.json(), detail=True)}"

    async def update_n8n_workflow(
        workflow_id: str,
        name: str = "",
        nodes: str = "",
        connections: str = "",
        settings: str = "",
    ) -> str:
        """Update an existing workflow. Only provided fields are changed.

        If the workflow is active, it will be automatically reactivated with the updated version.
        Pass nodes and connections as JSON strings.
        """
        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{api}/workflows/{workflow_id}",
                _PROVIDER,
                f"get workflow {workflow_id} for update",
                headers=headers,
            )
            if err:
                return err
            current = resp.json()

            payload: dict[str, Any] = {
                "name": current.get("name"),
                "nodes": current.get("nodes", []),
                "connections": current.get("connections", {}),
                "settings": current.get("settings", {}),
            }
            if name:
                payload["name"] = name
            if nodes:
                try:
                    payload["nodes"] = json.loads(nodes)
                except json.JSONDecodeError:
                    return "Error: 'nodes' must be valid JSON."
            if connections:
                try:
                    payload["connections"] = json.loads(connections)
                except json.JSONDecodeError:
                    return "Error: 'connections' must be valid JSON."
            if settings:
                try:
                    payload["settings"] = json.loads(settings)
                except json.JSONDecodeError:
                    return "Error: 'settings' must be valid JSON."

            resp, err = await safe_request(
                client,
                "put",
                f"{api}/workflows/{workflow_id}",
                _PROVIDER,
                f"update workflow {workflow_id}",
                headers={**headers, "Content-Type": "application/json"},
                json=payload,
            )
            if err:
                return err
            return f"Workflow updated:\n{_format_workflow(resp.json(), detail=True)}"

    async def delete_n8n_workflow(workflow_id: str) -> str:
        """Delete a workflow permanently. Use with caution."""
        async with egress_client(timeout=20) as client:
            _resp, err = await safe_request(
                client,
                "delete",
                f"{api}/workflows/{workflow_id}",
                _PROVIDER,
                f"delete workflow {workflow_id}",
                headers=headers,
            )
            if err:
                return err
            return f"Workflow {workflow_id} deleted successfully."

    async def set_n8n_workflow_active(workflow_id: str, active: bool) -> str:
        """Activate or deactivate a workflow."""
        action = "activate" if active else "deactivate"
        async with egress_client(timeout=20) as client:
            resp, err = await safe_request(
                client,
                "post",
                f"{api}/workflows/{workflow_id}/{action}",
                _PROVIDER,
                f"{action} workflow {workflow_id}",
                headers=headers,
            )
            if err:
                return err
            return f"Workflow {action}d:\n{_format_workflow(resp.json())}"

    async def get_n8n_workflow_tags(workflow_id: str) -> str:
        """Get the tags associated with a workflow."""
        async with egress_client(timeout=15) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{api}/workflows/{workflow_id}/tags",
                _PROVIDER,
                f"get tags for workflow {workflow_id}",
                headers=headers,
            )
            if err:
                return err
            tags = resp.json()
            if not tags:
                return f"Workflow {workflow_id} has no tags."
            lines = [f"Tags for workflow {workflow_id}:"]
            tag_list = tags if isinstance(tags, list) else tags.get("data", tags)
            for t in tag_list:
                if isinstance(t, dict):
                    lines.append(f"  #{t.get('id', '?')} {t.get('name', '?')}")
                else:
                    lines.append(f"  {t}")
            return "\n".join(lines)

    # ══════════════════════════════════════════════════════════════════════
    # 2. EXECUTIONS
    # ══════════════════════════════════════════════════════════════════════

    async def list_n8n_executions(
        workflow_id: str = "",
        status: str = "",
        limit: int = 20,
        cursor: str = "",
        include_data: bool = False,
    ) -> str:
        """List workflow executions. Filter by workflow ID and/or status.

        status options: canceled, error, running, success, waiting.
        Set include_data=True to include full execution data (larger response).
        """
        async with egress_client(timeout=30) as client:
            params: dict[str, Any] = {"limit": min(limit, 100)}
            if workflow_id:
                params["workflowId"] = workflow_id
            if status:
                params["status"] = status
            if cursor:
                params["cursor"] = cursor
            if include_data:
                params["includeData"] = "true"

            resp, err = await safe_request(
                client,
                "get",
                f"{api}/executions",
                _PROVIDER,
                "list executions",
                headers=headers,
                params=params,
            )
            if err:
                return err
            data = resp.json()
            executions = data.get("data", [])
            result = format_entity_list(
                executions,
                lambda ex: _format_execution(ex, detail=include_data),
                noun="execution",
            )
            return result + format_pagination_hint(
                data.get("nextCursor"),
                param_name="cursor",
            )

    async def get_n8n_execution(execution_id: str, include_data: bool = True) -> str:
        """Get details of a specific execution including node results and errors."""
        async with egress_client(timeout=30) as client:
            params: dict[str, Any] = {}
            if include_data:
                params["includeData"] = "true"
            resp, err = await safe_request(
                client,
                "get",
                f"{api}/executions/{execution_id}",
                _PROVIDER,
                f"get execution {execution_id}",
                headers=headers,
                params=params,
            )
            if err:
                return err
            return _format_execution(resp.json(), detail=True)

    async def delete_n8n_execution(execution_id: str) -> str:
        """Delete a specific execution record."""
        async with egress_client(timeout=20) as client:
            _resp, err = await safe_request(
                client,
                "delete",
                f"{api}/executions/{execution_id}",
                _PROVIDER,
                f"delete execution {execution_id}",
                headers=headers,
            )
            if err:
                return err
            return f"Execution {execution_id} deleted."

    async def retry_n8n_execution(
        execution_id: str, load_workflow: bool = False
    ) -> str:
        """Retry a failed execution.

        load_workflow=False: retry with the original workflow version.
        load_workflow=True: retry with the current (latest) workflow version.
        """
        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "post",
                f"{api}/executions/{execution_id}/retry",
                _PROVIDER,
                f"retry execution {execution_id}",
                headers={**headers, "Content-Type": "application/json"},
                json={"loadWorkflow": load_workflow},
            )
            if err:
                return err
            data = resp.json()
            return f"Execution {execution_id} retry initiated. Response: {json.dumps(data, default=str)[:500]}"

    # ══════════════════════════════════════════════════════════════════════
    # REGISTER ALL TOOLS
    # ══════════════════════════════════════════════════════════════════════

    # 1. Workflows
    executor.register("list_n8n_workflows", list_n8n_workflows)
    executor.register("get_n8n_workflow", get_n8n_workflow)
    executor.register("create_n8n_workflow", create_n8n_workflow)
    executor.register("update_n8n_workflow", update_n8n_workflow)
    executor.register("delete_n8n_workflow", delete_n8n_workflow)
    executor.register("set_n8n_workflow_active", set_n8n_workflow_active)
    executor.register("get_n8n_workflow_tags", get_n8n_workflow_tags)
    # 2. Executions
    executor.register("list_n8n_executions", list_n8n_executions)
    executor.register("get_n8n_execution", get_n8n_execution)
    executor.register("delete_n8n_execution", delete_n8n_execution)
    executor.register("retry_n8n_execution", retry_n8n_execution)

    return [
        # ── 1. Workflows ──────────────────────────────────────────────────
        ToolDefinition(
            name="list_n8n_workflows",
            description=(
                "List workflows in the n8n instance. Supports filtering by active status, "
                "tags (comma-separated), and name. Uses cursor-based pagination."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "active": {
                        "type": "string",
                        "description": "Filter by active status: 'true' or 'false' (empty = all)",
                        "default": "",
                    },
                    "tags": {
                        "type": "string",
                        "description": "Filter by tag names, comma-separated (e.g. 'production,important')",
                        "default": "",
                    },
                    "name": {
                        "type": "string",
                        "description": "Filter by workflow name (partial match)",
                        "default": "",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max results (max 100)",
                        "default": 25,
                    },
                    "cursor": {
                        "type": "string",
                        "description": "Pagination cursor from previous response",
                        "default": "",
                    },
                },
                "required": [],
            },
            source="integration:n8n",
            priority=0,
        ),
        ToolDefinition(
            name="get_n8n_workflow",
            description=(
                "Get full details of a specific n8n workflow including all nodes, connections, "
                "settings, and tags. Use list_n8n_workflows first to find workflow IDs."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "workflow_id": {"type": "string", "description": "Workflow ID"},
                },
                "required": ["workflow_id"],
            },
            source="integration:n8n",
            priority=0,
        ),
        ToolDefinition(
            name="create_n8n_workflow",
            description=(
                "Create a new workflow in n8n. Provide the workflow name and optionally define "
                "nodes and connections as JSON. Nodes require: name, type (e.g. 'n8n-nodes-base.httpRequest'), "
                "position [x,y], and parameters. Connections map source nodes to their outputs. "
                "The workflow is created in inactive state — use set_n8n_workflow_active to start it."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Workflow name"},
                    "nodes": {
                        "type": "string",
                        "description": "JSON array of node objects",
                        "default": "[]",
                    },
                    "connections": {
                        "type": "string",
                        "description": "JSON object mapping source nodes to outputs",
                        "default": "{}",
                    },
                    "settings": {
                        "type": "string",
                        "description": "JSON workflow settings (optional)",
                        "default": "{}",
                    },
                    "static_data": {
                        "type": "string",
                        "description": "JSON static data (optional)",
                        "default": "null",
                    },
                },
                "required": ["name"],
            },
            source="integration:n8n",
            priority=0,
        ),
        ToolDefinition(
            name="update_n8n_workflow",
            description=(
                "Update an existing n8n workflow. Only provided fields are changed. "
                "If the workflow is currently active, it is automatically reactivated with the new version. "
                "Pass nodes and connections as JSON strings."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "workflow_id": {
                        "type": "string",
                        "description": "Workflow ID to update",
                    },
                    "name": {
                        "type": "string",
                        "description": "New workflow name (optional)",
                        "default": "",
                    },
                    "nodes": {
                        "type": "string",
                        "description": "JSON array of nodes (optional, replaces all nodes)",
                        "default": "",
                    },
                    "connections": {
                        "type": "string",
                        "description": "JSON object of connections (optional)",
                        "default": "",
                    },
                    "settings": {
                        "type": "string",
                        "description": "JSON workflow settings (optional)",
                        "default": "",
                    },
                },
                "required": ["workflow_id"],
            },
            source="integration:n8n",
            priority=1,
        ),
        ToolDefinition(
            name="delete_n8n_workflow",
            description="Delete an n8n workflow permanently. This action cannot be undone.",
            input_schema={
                "type": "object",
                "properties": {
                    "workflow_id": {
                        "type": "string",
                        "description": "Workflow ID to delete",
                    },
                },
                "required": ["workflow_id"],
            },
            source="integration:n8n",
            priority=1,
        ),
        ToolDefinition(
            name="set_n8n_workflow_active",
            description=(
                "Activate or deactivate an n8n workflow. When active, the workflow runs on its "
                "configured triggers (schedule, webhook, etc.). The workflow must have at least "
                "one trigger node to be activated."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "workflow_id": {
                        "type": "string",
                        "description": "Workflow ID",
                    },
                    "active": {
                        "type": "boolean",
                        "description": "True to activate, False to deactivate",
                    },
                },
                "required": ["workflow_id", "active"],
            },
            source="integration:n8n",
            priority=0,
        ),
        ToolDefinition(
            name="get_n8n_workflow_tags",
            description="Get the tags associated with an n8n workflow.",
            input_schema={
                "type": "object",
                "properties": {
                    "workflow_id": {"type": "string", "description": "Workflow ID"},
                },
                "required": ["workflow_id"],
            },
            source="integration:n8n",
            priority=2,
        ),
        # ── 2. Executions ─────────────────────────────────────────────────
        ToolDefinition(
            name="list_n8n_executions",
            description=(
                "List workflow executions in n8n. Filter by workflow ID, status "
                "(canceled, error, running, success, waiting), and pagination. "
                "Use to monitor workflow health and troubleshoot failures."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "workflow_id": {
                        "type": "string",
                        "description": "Filter by workflow ID (optional)",
                        "default": "",
                    },
                    "status": {
                        "type": "string",
                        "enum": [
                            "",
                            "canceled",
                            "error",
                            "running",
                            "success",
                            "waiting",
                        ],
                        "description": "Filter by execution status",
                        "default": "",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max results (max 100)",
                        "default": 20,
                    },
                    "cursor": {
                        "type": "string",
                        "description": "Pagination cursor",
                        "default": "",
                    },
                    "include_data": {
                        "type": "boolean",
                        "description": "Include full execution data (larger response)",
                        "default": False,
                    },
                },
                "required": [],
            },
            source="integration:n8n",
            priority=0,
        ),
        ToolDefinition(
            name="get_n8n_execution",
            description=(
                "Get full details of a specific execution including node results, errors, "
                "and timing. Use list_n8n_executions to find execution IDs."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "execution_id": {"type": "string", "description": "Execution ID"},
                    "include_data": {
                        "type": "boolean",
                        "description": "Include execution data",
                        "default": True,
                    },
                },
                "required": ["execution_id"],
            },
            source="integration:n8n",
            priority=1,
        ),
        ToolDefinition(
            name="delete_n8n_execution",
            description="Delete an execution record from n8n.",
            input_schema={
                "type": "object",
                "properties": {
                    "execution_id": {
                        "type": "string",
                        "description": "Execution ID to delete",
                    },
                },
                "required": ["execution_id"],
            },
            source="integration:n8n",
            priority=1,
        ),
        ToolDefinition(
            name="retry_n8n_execution",
            description=(
                "Retry a failed execution. Use load_workflow=True to retry with the latest "
                "workflow version, or False to use the original version from the failed execution."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "execution_id": {
                        "type": "string",
                        "description": "Execution ID to retry",
                    },
                    "load_workflow": {
                        "type": "boolean",
                        "description": "True = use latest workflow version, False = use original",
                        "default": False,
                    },
                },
                "required": ["execution_id"],
            },
            source="integration:n8n",
            priority=1,
        ),
    ]
