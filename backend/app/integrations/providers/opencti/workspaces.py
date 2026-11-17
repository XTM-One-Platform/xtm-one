"""OpenCTI workspace tools - investigations and dashboards.

Investigations and dashboards are both ``Workspace`` entities distinguished
by ``type`` ("investigation" | "dashboard"). Exposes a single consolidated
``manage_opencti_workspace`` tool (``src/modules/workspace/workspace.graphql``).
Not draft-gated (workspaces are not STIX knowledge).
"""

from __future__ import annotations

import logging

from app.tools.base import CallableToolExecutor, ToolDefinition

from ._common import _client, _edges, _graphql

logger = logging.getLogger(__name__)

_SOURCE = "integration:opencti"
_WS_PATH = {"investigation": "investigations", "dashboard": "dashboards"}


def register(
    base_url: str, headers: dict[str, str], executor: CallableToolExecutor
) -> list[ToolDefinition]:
    async def _gql(query: str, variables: dict | None = None) -> tuple[dict, str | None]:
        async with _client(headers, timeout=30) as client:
            return await _graphql(client, base_url, headers, query, variables)

    def _link(node: dict) -> str:
        if not base_url:
            return ""
        path = _WS_PATH.get(node.get("type") or "", "")
        if not path:
            return ""
        return f"  link: {base_url}/dashboard/workspaces/{path}/{node.get('id')}"

    async def manage_opencti_workspace(
        action: str,
        type: str = "",
        workspace_id: str = "",
        name: str = "",
        description: str = "",
        tags: list[str] | None = None,
        entity_ids: list[str] | None = None,
        search: str = "",
        limit: int = 20,
    ) -> str:
        action = (action or "").strip().lower()
        wtype = (type or "").strip().lower()

        if action == "list":
            filters = None
            if wtype:
                filters = {
                    "mode": "and",
                    "filters": [{"key": ["type"], "values": [wtype]}],
                    "filterGroups": [],
                }
            query = (
                "query Workspaces($first: Int, $search: String, $filters: FilterGroup) {"
                "  workspaces(first: $first, search: $search, filters: $filters,"
                "    orderBy: updated_at, orderMode: desc) {"
                "    edges { node { id type name description tags updated_at"
                "      owner { name } } }"
                "  }"
                "}"
            )
            data, err = await _gql(
                query,
                {
                    "first": min(max(limit, 1), 200),
                    "search": search or None,
                    "filters": filters,
                },
            )
            if err:
                return err
            nodes = _edges(data, "workspaces")
            if not nodes:
                return "No workspaces found."
            lines = [f"Workspaces ({len(nodes)}):"]
            for n in nodes:
                owner = (n.get("owner") or {}).get("name", "?")
                lines.append(
                    f"- [{n.get('type')}] {n.get('name')} (id: {n.get('id')}) "
                    f"- owner: {owner}"
                )
                if n.get("description"):
                    lines.append(f"    {n['description']}")
            return "\n".join(lines)

        if action == "get":
            if not workspace_id:
                return "workspace_id is required for action 'get'."
            query = (
                "query Workspace($id: String!) {"
                "  workspace(id: $id) { id type name description tags"
                "    investigated_entities_ids created_at updated_at"
                "    owner { name } currentUserAccessRight }"
                "}"
            )
            data, err = await _gql(query, {"id": workspace_id})
            if err:
                return err
            ws = data.get("workspace")
            if not ws:
                return f"Workspace {workspace_id} not found."
            lines = [
                f"[{ws.get('type')}] {ws.get('name')} (id: {ws.get('id')})",
                f"  owner: {(ws.get('owner') or {}).get('name', '?')}",
                f"  access: {ws.get('currentUserAccessRight', '?')}",
            ]
            if ws.get("description"):
                lines.append(f"  description: {ws['description']}")
            if ws.get("tags"):
                lines.append(f"  tags: {', '.join(ws['tags'])}")
            ents = ws.get("investigated_entities_ids") or []
            if ents:
                lines.append(f"  investigated entities: {len(ents)}")
            link = _link(ws)
            if link:
                lines.append(link)
            return "\n".join(lines)

        if action == "create":
            if not wtype or wtype not in _WS_PATH:
                return "type must be 'investigation' or 'dashboard' for action 'create'."
            if not name:
                return "name is required for action 'create'."
            query = (
                "mutation WorkspaceAdd($input: WorkspaceAddInput!) {"
                "  workspaceAdd(input: $input) { id type name }"
                "}"
            )
            inp: dict = {"type": wtype, "name": name}
            if description:
                inp["description"] = description
            if tags:
                inp["tags"] = tags
            if entity_ids:
                inp["investigated_entities_ids"] = entity_ids
            data, err = await _gql(query, {"input": inp})
            if err:
                return err
            ws = data.get("workspaceAdd") or {}
            out = f"Created {ws.get('type')} '{ws.get('name')}' (id: {ws.get('id')})."
            link = _link(ws)
            return out + ("\n" + link if link else "")

        if action == "update":
            if not workspace_id:
                return "workspace_id is required for action 'update'."
            edits = []
            if name:
                edits.append({"key": "name", "value": [name]})
            if description:
                edits.append({"key": "description", "value": [description]})
            if tags is not None:
                edits.append({"key": "tags", "value": tags})
            if not edits:
                return "Provide at least one of name, description, or tags to update."
            query = (
                "mutation WorkspacePatch($id: ID!, $input: [EditInput!]!) {"
                "  workspaceFieldPatch(id: $id, input: $input) { id name }"
                "}"
            )
            data, err = await _gql(query, {"id": workspace_id, "input": edits})
            if err:
                return err
            ws = data.get("workspaceFieldPatch") or {}
            return f"Updated workspace '{ws.get('name')}' (id: {ws.get('id')})."

        if action in ("add_entities", "remove_entities"):
            if not workspace_id or not entity_ids:
                return f"workspace_id and entity_ids are required for action '{action}'."
            op = "add" if action == "add_entities" else "remove"
            query = (
                "mutation WorkspacePatch($id: ID!, $input: [EditInput!]!) {"
                "  workspaceFieldPatch(id: $id, input: $input) {"
                "    id investigated_entities_ids } }"
            )
            data, err = await _gql(
                query,
                {
                    "id": workspace_id,
                    "input": [
                        {
                            "key": "investigated_entities_ids",
                            "value": entity_ids,
                            "operation": op,
                        }
                    ],
                },
            )
            if err:
                return err
            ws = data.get("workspaceFieldPatch") or {}
            total = len(ws.get("investigated_entities_ids") or [])
            verb = "Added" if op == "add" else "Removed"
            return f"{verb} {len(entity_ids)} entities. Workspace now has {total} entities."

        if action == "delete":
            if not workspace_id:
                return "workspace_id is required for action 'delete'."
            query = "mutation WorkspaceDelete($id: ID!) { workspaceDelete(id: $id) }"
            _, err = await _gql(query, {"id": workspace_id})
            if err:
                return err
            return f"Deleted workspace {workspace_id}."

        if action == "duplicate":
            if not workspace_id:
                return "workspace_id is required for action 'duplicate'."
            src_q = (
                "query Workspace($id: String!) {"
                "  workspace(id: $id) { type name description manifest tags } }"
            )
            data, err = await _gql(src_q, {"id": workspace_id})
            if err:
                return err
            src = data.get("workspace")
            if not src:
                return f"Workspace {workspace_id} not found."
            dup_input = {
                "type": src.get("type"),
                "name": name or f"Copy of {src.get('name')}",
                "description": description or src.get("description"),
                "manifest": src.get("manifest"),
                "tags": tags if tags is not None else src.get("tags"),
            }
            query = (
                "mutation WorkspaceDuplicate($input: WorkspaceDuplicateInput!) {"
                "  workspaceDuplicate(input: $input) { id type name } }"
            )
            data, err = await _gql(query, {"input": dup_input})
            if err:
                return err
            ws = data.get("workspaceDuplicate") or {}
            return f"Duplicated to '{ws.get('name')}' (id: {ws.get('id')})."

        return (
            f"Unknown action '{action}'. Valid actions: list, get, create, update, "
            "delete, duplicate, add_entities, remove_entities."
        )

    executor.register("manage_opencti_workspace", manage_opencti_workspace)

    return [
        ToolDefinition(
            name="manage_opencti_workspace",
            description=(
                "Manage OpenCTI investigations and custom dashboards (both are "
                "'workspaces' distinguished by type). Actions: 'list' (optional "
                "type='investigation'|'dashboard', search), 'get' (workspace_id), "
                "'create' (type + name, optional description/tags/entity_ids), "
                "'update' (workspace_id + name/description/tags), 'delete' "
                "(workspace_id), 'duplicate' (workspace_id, optional new name), "
                "'add_entities' / 'remove_entities' (workspace_id + entity_ids - for "
                "investigations, adds/removes the investigated STIX entities)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": [
                            "list", "get", "create", "update", "delete",
                            "duplicate", "add_entities", "remove_entities",
                        ],
                        "description": "The operation to perform.",
                    },
                    "type": {
                        "type": "string",
                        "enum": ["investigation", "dashboard"],
                        "description": "Workspace type (required for 'create'; filters 'list').",
                    },
                    "workspace_id": {"type": "string", "description": "Workspace ID."},
                    "name": {"type": "string", "description": "Workspace name (create/update/duplicate)."},
                    "description": {"type": "string", "description": "Workspace description (create/update)."},
                    "tags": {"type": "array", "items": {"type": "string"}, "description": "Tags (create/update)."},
                    "entity_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "STIX entity IDs for create/add_entities/remove_entities (investigations).",
                    },
                    "search": {"type": "string", "description": "Search term (list)."},
                    "limit": {"type": "integer", "description": "Max results for 'list' (default 20, max 200).", "default": 20},
                },
                "required": ["action"],
            },
            source=_SOURCE,
        ),
    ]
